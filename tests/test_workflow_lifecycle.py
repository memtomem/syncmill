"""Reviewable plan lifecycle: validate, create, export, revise and lineage."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from conftest import FakeRegistry
from syncmill.cli import cli
from syncmill.workflow import (
    WORKFLOW_DOCUMENT_MAX_BYTES,
    WorkflowCoordinator,
    WorkflowError,
    WorkflowSpec,
    WorkflowStore,
    _unique_object,
    canonical_workflow_json,
    read_workflow_document,
    render_workflow,
    validate_workflow_policy,
)
from test_workflow import WorkflowAgent, _config, _control_config

# Built with chr() so no editor or tool can turn an escape into the character.
ASTRAL_FORMAT = chr(0x1D173)  # not printable, outside the BMP
BIDI_OVERRIDE = chr(0x202E)
C1_CSI = chr(0x9B)
EMOJI = chr(0x1F600)  # printable, outside the BMP


def _doc(**overrides) -> dict:
    document = {
        "schema_version": 1,
        "goal": "Ship the reviewed change",
        "nodes": [
            {"id": "inspect", "agent": "claude", "prompt": "Inspect the code."},
            {
                "id": "final",
                "agent": "claude",
                "prompt": "Implement the final integrated change.",
                "needs": ["inspect"],
            },
        ],
        "final_node": "final",
    }
    document.update(overrides)
    return document


def _raw(document: dict) -> bytes:
    return json.dumps(document).encode()


def _coordinator(git_repo: Path, tmp_path: Path, config=None):
    agent = WorkflowAgent("unused")
    config = config or _config(git_repo, tmp_path)
    return WorkflowCoordinator(config, FakeRegistry([agent])), agent


def _set(store: WorkflowStore, workflow_id: str, **columns) -> None:
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            f"UPDATE workflows SET {assignments} WHERE workflow_id = ?",
            (*columns.values(), workflow_id),
        )


def _prunable(store: WorkflowStore, workflow_id: str) -> None:
    store.cancel(workflow_id)
    stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    _set(store, workflow_id, updated_at=stale)


def _versions(store: WorkflowStore, workflow_id: str) -> list[tuple[str, int]]:
    return [(r.workflow_id, r.plan_version) for r in store.lineage(workflow_id)]


# ── the document contract ──────────────────────────────────────────────────


def test_digest_of_an_existing_plan_shape_is_unchanged():
    """Pinned on main before this change: persisted rows and final-artifact
    provenance are keyed by this digest."""
    spec = WorkflowSpec.model_validate(
        {
            "goal": "Pin the digest",
            "nodes": [
                {"id": "inspect", "agent": "claude", "prompt": "Look " + chr(0xE9) + " " + EMOJI},
                {
                    "id": "final",
                    "agent": "claude",
                    "prompt": "Do it",
                    "needs": ["inspect"],
                    "max_attempts": 2,
                    "timeout_seconds": 60,
                },
            ],
            "final_node": "final",
        }
    )
    assert spec.digest() == (
        "c892d5060bd75fb92fbc7bcb4535e012fdb133ccec249e8d3b4171719a6721e2"
    )


def test_canonical_form_round_trips_every_kind_of_character():
    # Prompts refuse C0/C1 controls (the rooms rule); a goal refuses only NUL.
    prompt = "a" + ASTRAL_FORMAT + BIDI_OVERRIDE + EMOJI + "\ttab\nline\r"
    document = _doc(goal="goal " + chr(0x1B) + C1_CSI + BIDI_OVERRIDE + EMOJI)
    document["nodes"][0]["prompt"] = prompt
    spec = read_workflow_document(_raw(document))

    canonical = canonical_workflow_json(spec)
    text = canonical.decode("utf-8")
    # Nothing a terminal interprets is left raw; only the indent's newlines.
    assert all(ch == "\n" or ch.isprintable() for ch in text)
    assert text.endswith("}\n")
    again = read_workflow_document(canonical)
    assert again == spec
    assert again.digest() == spec.digest()
    assert canonical_workflow_json(again) == canonical


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xef\xbb\xbf" + _raw(_doc()), "byte order mark"),
        (b'{"goal": "\xff"}', "not valid UTF-8 at byte 10"),
        (b"[1]", "one JSON object"),
        (b'{"goal": NaN}', "NaN is not a JSON number"),
        (b'{"goal": Infinity}', "Infinity is not a JSON number"),
        (b"[" * 100_000, "nested too deeply"),
        (b"{", "not valid JSON"),
    ],
)
def test_document_bytes_that_are_refused(raw, message):
    with pytest.raises(WorkflowError, match=message):
        read_workflow_document(raw)


def test_a_duplicated_key_is_refused_wherever_it_is():
    """A reader sees the first value and json keeps the last, so a reviewed
    document could carry a prompt nobody read."""
    node = '{"id": "final", "agent": "claude", "prompt": "safe", "prompt": "hidden"}'
    raw = ('{"goal": "g", "nodes": [' + node + '], "final_node": "final"}').encode()
    with pytest.raises(WorkflowError, match="duplicate key 'prompt'"):
        read_workflow_document(raw)
    top = _raw(_doc())[:-1] + b', "goal": "again"}'
    with pytest.raises(WorkflowError, match="duplicate key 'goal'"):
        read_workflow_document(top)


class _CountingKey(str):
    comparisons = 0

    def __eq__(self, other):
        type(self).comparisons += 1
        return str.__eq__(self, other)

    __hash__ = str.__hash__


def test_a_duplicate_after_many_keys_is_found_in_linear_time():
    """Counting each key again made this quadratic: about 1 s at 16,000 keys,
    and an 8 MiB document can hold far more. Counted, not timed, so a slow
    runner cannot fail it."""
    count = 2_000
    pairs = [(_CountingKey(f"k{index}"), 1) for index in range(count)]
    pairs += [(_CountingKey("tail"), 1), (_CountingKey("tail"), 2)]
    _CountingKey.comparisons = 0
    with pytest.raises(ValueError, match="duplicate key 'tail'"):
        _unique_object(pairs)
    assert _CountingKey.comparisons < 10 * count

    keys = ", ".join(f'"k{index}": 1' for index in range(60_000))
    raw = ("{" + keys + ', "tail": 1, "tail": 2}').encode()
    with pytest.raises(WorkflowError, match="duplicate key 'tail'"):
        read_workflow_document(raw)


@pytest.mark.parametrize("field", ["id", "needs", "prompt", "goal", "final_node"])
def test_an_unpaired_surrogate_escape_is_refused(field):
    lone = chr(92) + "ud800"
    node_id = "x" + lone if field in {"id", "needs", "final_node"} else "x"
    nodes = [{"id": "x", "agent": "claude", "prompt": "p"}]
    if field == "id":
        nodes[0]["id"] = node_id
    if field == "needs":
        nodes = [
            {"id": "a", "agent": "claude", "prompt": "p"},
            {"id": "x", "agent": "claude", "prompt": "p", "needs": ["a"]},
        ]
    if field == "prompt":
        nodes[0]["prompt"] = "p" + lone
    document = {"goal": "g" + lone if field == "goal" else "g", "nodes": nodes, "final_node": "x"}
    text = json.dumps(document)
    if field == "needs":
        text = text.replace('"needs": ["a"]', '"needs": ["a' + lone + '"]')
    if field == "final_node":
        text = text.replace('"final_node": "x"', '"final_node": "x' + lone + '"')
    # json.dumps escaped the backslash; undo that so the document holds the escape.
    raw = text.replace(chr(92) * 2 + "ud800", lone).encode()
    assert (chr(92) + "ud800").encode() in raw
    with pytest.raises(WorkflowError, match="unpaired surrogate"):
        read_workflow_document(raw)


def test_a_deeply_nested_document_is_a_workflow_error():
    depth = 80_000
    raw = (
        '{"goal":"g","nodes":[{"id":"x","agent":"claude","prompt":"p"}],'
        '"final_node":"x","extra":' + '{"a":' * depth + "0" + "}" * depth + "}"
    ).encode()
    with pytest.raises(WorkflowError):
        read_workflow_document(raw)


def test_cli_validate_reports_an_unpaired_surrogate(git_repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr("syncmill.cli._workflow_config", lambda repo, team: _config(git_repo, tmp_path))
    raw = _raw(_doc()).replace(b'"inspect"', b'"inspect' + (chr(92) + "ud800").encode() + b'"', 1)
    result = CliRunner().invoke(cli, ["workflow", "validate", "-"], input=raw)
    assert result.exit_code == 1
    assert "unpaired surrogate" in result.output


@pytest.mark.parametrize("version", [True, 1.0, "1", 2, 0, None])
def test_only_the_integer_one_is_schema_version_one(version):
    # Lax validation accepts True and 1.0 for Literal[1]; the document must not.
    with pytest.raises(WorkflowError, match="unsupported workflow schema_version"):
        read_workflow_document(_raw(_doc(schema_version=version)))


def test_schema_version_may_be_omitted():
    document = _doc()
    del document["schema_version"]
    assert read_workflow_document(_raw(document)).schema_version == 1


def test_fields_are_validated_strictly_not_coerced():
    document = _doc()
    document["nodes"][0]["max_attempts"] = "2"
    with pytest.raises(WorkflowError, match="invalid workflow document"):
        read_workflow_document(_raw(document))
    document = _doc(extra="field")
    with pytest.raises(WorkflowError, match="invalid workflow document"):
        read_workflow_document(_raw(document))


def test_a_nul_in_the_goal_is_refused_on_every_path():
    with pytest.raises(WorkflowError, match="must not contain NUL"):
        read_workflow_document(_raw(_doc(goal="a" + chr(0) + "b")))
    with pytest.raises(ValueError, match="must not contain NUL"):
        WorkflowSpec.model_validate(_doc(goal="a" + chr(0) + "b"))


def test_a_planner_goal_with_controls_still_exports_and_imports(
    git_repo: Path, tmp_path: Path
):
    """A planner goal comes from argv and may hold ESC; the document reader
    must accept whatever export can produce."""
    coordinator, _ = _coordinator(git_repo, tmp_path)
    planned = coordinator.plan("goal with " + chr(0x1B) + "[31m")
    canonical = canonical_workflow_json(planned.spec)
    assert read_workflow_document(canonical).digest() == planned.spec.digest()


def test_raw_size_limit():
    document = _doc(goal="g" * 40_000)
    raw = _raw(document)
    padded = raw + b" " * (WORKFLOW_DOCUMENT_MAX_BYTES - len(raw))
    assert len(padded) == WORKFLOW_DOCUMENT_MAX_BYTES
    assert read_workflow_document(padded).goal == "g" * 40_000
    with pytest.raises(WorkflowError, match="exceeds 8388608 bytes"):
        read_workflow_document(padded + b" ")


def _chain(count: int, prompt: str) -> dict:
    nodes = [{"id": "n0", "agent": "claude", "prompt": prompt}]
    for index in range(1, count):
        nodes.append(
            {"id": f"n{index}", "agent": "claude", "prompt": prompt, "needs": [f"n{index - 1}"]}
        )
    return {"goal": "g", "nodes": nodes, "final_node": f"n{count - 1}"}


def test_a_document_that_could_not_be_exported_again_is_refused():
    # Each character is 4 raw bytes and a 12-byte surrogate-pair escape once
    # canonical, so this fits raw and overflows canonical.
    document = _chain(95, ASTRAL_FORMAT * 20_000)
    raw = json.dumps(document, ensure_ascii=False).encode()
    assert len(raw) <= WORKFLOW_DOCUMENT_MAX_BYTES
    with pytest.raises(WorkflowError, match="once exported"):
        read_workflow_document(raw)


def test_a_near_limit_multibyte_document_round_trips():
    # Printable astral characters stay literal, so canonical size tracks raw.
    document = _chain(95, EMOJI * 20_000)
    raw = json.dumps(document, ensure_ascii=False).encode()
    assert len(raw) > WORKFLOW_DOCUMENT_MAX_BYTES * 0.9
    spec = read_workflow_document(raw)
    canonical = canonical_workflow_json(spec)
    assert len(canonical) <= WORKFLOW_DOCUMENT_MAX_BYTES
    assert read_workflow_document(canonical).digest() == spec.digest()


# ── create ─────────────────────────────────────────────────────────────────


def test_create_stores_a_planned_workflow_without_calling_anyone(
    git_repo: Path, tmp_path: Path
):
    config = _config(git_repo, tmp_path)
    config.applied_team = "core"
    coordinator, agent = _coordinator(git_repo, tmp_path, config)
    spec = read_workflow_document(_raw(_doc()))

    record = coordinator.create_from_spec(spec)

    assert agent.calls == []
    assert record.state == "planned"
    assert record.plan_origin == "file"
    assert record.manifest["calls_used"] == 0
    assert record.call_count == 0
    assert record.parent_workflow_id is None
    assert record.plan_version == 1
    assert record.lineage_id.startswith("wl-")
    assert record.team == "core"
    assert record.spec.digest() == spec.digest()
    assert record.source_fingerprint == repository_fingerprint_of(git_repo)
    directory = coordinator.store.workflow_dir(record.workflow_id)
    assert (directory / "spec.json").is_file()
    assert not (directory / "planner-output.txt").exists()
    assert not (tmp_path / "workspaces").exists()


def repository_fingerprint_of(path: Path) -> str:
    from syncmill.worktree import repository_fingerprint

    return repository_fingerprint(path)


def test_create_refuses_a_plan_naming_an_unavailable_agent(
    git_repo: Path, tmp_path: Path
):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    document = _doc()
    document["nodes"][0]["agent"] = "codex"
    with pytest.raises(WorkflowError, match="unavailable agent"):
        coordinator.create_from_spec(read_workflow_document(_raw(document)))
    assert coordinator.store.list() == []


def test_a_document_plan_may_spend_the_whole_budget_on_nodes(
    git_repo: Path, tmp_path: Path
):
    """Two nodes under max_agent_calls=2: a planner plan has already spent one
    call and is refused; a document plan spent none, runs, and stops at two."""
    config = _config(git_repo, tmp_path)
    config.workflow.max_agent_calls = 2
    coordinator, agent = _coordinator(git_repo, tmp_path, config)
    spec = read_workflow_document(_raw(_doc()))
    with pytest.raises(WorkflowError, match="needs up to 3 agent calls including planning"):
        validate_workflow_policy(spec, config, coordinator.registry, planning_calls=1)

    created = coordinator.create_from_spec(spec)
    completed = coordinator.run(created.workflow_id)

    assert completed.state == "completed"
    assert completed.manifest["calls_used"] == 2
    assert len(agent.calls) == 2
    assert b"implemented.txt" in coordinator.artifacts.patch(completed.final_run_id)


def test_a_row_without_an_origin_is_held_to_the_planner_budget(
    git_repo: Path, tmp_path: Path
):
    """An origin that cannot be read must narrow the budget, never widen it."""
    config = _config(git_repo, tmp_path)
    coordinator, _ = _coordinator(git_repo, tmp_path, config)
    created = coordinator.create_from_spec(read_workflow_document(_raw(_doc())))
    # What an older build would leave: the column default.
    _set(coordinator.store, created.workflow_id, plan_origin="planner")
    config.workflow.max_agent_calls = 2
    with pytest.raises(WorkflowError, match="needs up to 3 agent calls"):
        coordinator.run(created.workflow_id)


def test_a_file_workflow_carries_the_control_plan_a_planner_one_would(
    git_repo: Path, tmp_path: Path
):
    coordinator, _ = _coordinator(git_repo, tmp_path, _control_config(git_repo, tmp_path))
    created = coordinator.create_from_spec(read_workflow_document(_raw(_doc())))
    directory = coordinator.store.workflow_dir(created.workflow_id)
    assert (directory / "control-plan.json").is_file()
    assert created.manifest["control_plan"]["spec_sha256"] == created.spec.digest()
    assert set(created.manifest["execution_limits"]) == {
        "deadline_seconds",
        "max_parallel",
        "max_output_bytes",
    }


def test_create_from_spec_refuses_a_plan_export_could_not_write(
    git_repo: Path, tmp_path: Path
):
    """A spec handed to the API directly never passed the document reader, so
    the canonical size limit is checked again before anything is stored."""
    config = _config(git_repo, tmp_path)
    config.workflow.max_agent_calls = 64
    coordinator, _ = _coordinator(git_repo, tmp_path, config)
    parallel = [
        {"id": f"n{index}", "agent": "claude", "prompt": ASTRAL_FORMAT * 20_000}
        for index in range(39)
    ]
    final = {"id": "final", "agent": "claude", "prompt": "f", "needs": [n["id"] for n in parallel]}
    spec = WorkflowSpec.model_validate({"goal": "g", "nodes": [*parallel, final], "final_node": "final"})
    assert len(canonical_workflow_json(spec)) > WORKFLOW_DOCUMENT_MAX_BYTES

    with pytest.raises(WorkflowError, match="once exported"):
        coordinator.create_from_spec(spec)
    parent = coordinator.create_from_spec(read_workflow_document(_raw(_doc())))
    with pytest.raises(WorkflowError, match="once exported"):
        coordinator.create_from_spec(spec, parent_workflow_id=parent.workflow_id)
    assert [r.workflow_id for r in coordinator.store.list()] == [parent.workflow_id]


def test_store_create_rejects_inconsistent_origins(git_repo: Path, tmp_path: Path):
    store = WorkflowStore(tmp_path / "runs", "project")
    spec = read_workflow_document(_raw(_doc()))
    common = dict(spec=spec, repo_root=git_repo, source_fingerprint="a" * 64, planner_agent="-")
    with pytest.raises(WorkflowError, match="plan origin"):
        store.create(**common, planner_output=None, plan_origin="magic")
    with pytest.raises(WorkflowError, match="exactly when it is a revision"):
        store.create(**common, planner_output=None, plan_origin="revision")
    with pytest.raises(WorkflowError, match="exactly when it is a revision"):
        store.create(
            **common, planner_output=None, plan_origin="file",
            parent_workflow_id="wf-" + "0" * 16,
        )
    with pytest.raises(WorkflowError, match="only a planner workflow"):
        store.create(**common, planner_output="{}", plan_origin="file")
    with pytest.raises(WorkflowError, match="only a planner workflow"):
        store.create(**common, planner_output=None, plan_origin="planner")
    assert store.list() == []


# ── revise and lineage ─────────────────────────────────────────────────────


def test_revise_creates_a_new_version_and_leaves_the_parent_alone(
    git_repo: Path, tmp_path: Path
):
    coordinator, agent = _coordinator(git_repo, tmp_path)
    store = coordinator.store
    parent = coordinator.plan("Ship the reviewed change")
    parent_dir = store.workflow_dir(parent.workflow_id)
    files_before = {p.name: p.read_bytes() for p in parent_dir.iterdir() if p.is_file()}
    with sqlite3.connect(store.db_path) as connection:
        row_before = connection.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?", (parent.workflow_id,)
        ).fetchone()
    calls_before = len(agent.calls)

    document = _doc()
    document["nodes"][1]["prompt"] = "Implement it, revised."
    revision = coordinator.create_from_spec(
        read_workflow_document(_raw(document)), parent_workflow_id=parent.workflow_id
    )

    assert len(agent.calls) == calls_before
    assert revision.workflow_id != parent.workflow_id
    assert revision.plan_origin == "revision"
    assert revision.parent_workflow_id == parent.workflow_id
    assert revision.lineage_id == parent.lineage_id
    assert revision.plan_version == 2
    assert revision.manifest["calls_used"] == 0
    assert revision.spec.digest() != parent.spec.digest()
    with sqlite3.connect(store.db_path) as connection:
        row_after = connection.execute(
            "SELECT * FROM workflows WHERE workflow_id = ?", (parent.workflow_id,)
        ).fetchone()
    assert row_after == row_before
    assert {p.name: p.read_bytes() for p in parent_dir.iterdir() if p.is_file()} == files_before

    # Revising the same parent again continues the lineage, never forks v2.
    third = coordinator.create_from_spec(
        read_workflow_document(_raw(_doc())), parent_workflow_id=parent.workflow_id
    )
    assert third.plan_version == 3
    assert _versions(store, revision.workflow_id) == [
        (parent.workflow_id, 1),
        (revision.workflow_id, 2),
        (third.workflow_id, 3),
    ]


@pytest.mark.parametrize("state", ["running", "cancelling", "pruning"])
def test_revise_refuses_a_parent_in_flight(git_repo: Path, tmp_path: Path, state):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    parent = coordinator.create_from_spec(read_workflow_document(_raw(_doc())))
    _set(coordinator.store, parent.workflow_id, state=state)
    with pytest.raises(WorkflowError, match=f"while it is {state}"):
        coordinator.create_from_spec(
            read_workflow_document(_raw(_doc())), parent_workflow_id=parent.workflow_id
        )
    assert len(coordinator.store.list()) == 1
    assert sorted(p.name for p in coordinator.store.root.iterdir() if p.name.startswith("wf-")) == [
        parent.workflow_id
    ]


@pytest.mark.parametrize("state", ["planned", "completed", "failed", "cancelled", "recovery_required"])
def test_revise_accepts_a_settled_parent(git_repo: Path, tmp_path: Path, state):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    parent = coordinator.create_from_spec(read_workflow_document(_raw(_doc())))
    _set(coordinator.store, parent.workflow_id, state=state)
    revision = coordinator.create_from_spec(
        read_workflow_document(_raw(_doc())), parent_workflow_id=parent.workflow_id
    )
    assert revision.plan_version == 2


def test_revise_refuses_an_unknown_or_foreign_parent(git_repo: Path, tmp_path: Path):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    spec = read_workflow_document(_raw(_doc()))
    with pytest.raises(WorkflowError, match="unknown workflow"):
        coordinator.create_from_spec(spec, parent_workflow_id="wf-" + "0" * 16)
    with pytest.raises(WorkflowError, match="invalid workflow id"):
        coordinator.create_from_spec(spec, parent_workflow_id="../escape")
    parent = coordinator.create_from_spec(spec)
    _set(coordinator.store, parent.workflow_id, repo_root=str(tmp_path / "elsewhere"))
    with pytest.raises(WorkflowError, match="different repository"):
        coordinator.create_from_spec(spec, parent_workflow_id=parent.workflow_id)
    assert len(coordinator.store.list()) == 1


def test_versions_are_never_reused_after_prune(git_repo: Path, tmp_path: Path):
    """Allocating from the surviving rows would hand out v3 a second time."""
    coordinator, _ = _coordinator(git_repo, tmp_path)
    store = coordinator.store
    spec = read_workflow_document(_raw(_doc()))
    root = coordinator.create_from_spec(spec)
    second = coordinator.create_from_spec(spec, parent_workflow_id=root.workflow_id)
    third = coordinator.create_from_spec(spec, parent_workflow_id=root.workflow_id)
    assert third.plan_version == 3
    _prunable(store, third.workflow_id)
    assert store.prune(keep=0, retention_days=1, dry_run=False) == [third.workflow_id]

    fourth = coordinator.create_from_spec(spec, parent_workflow_id=second.workflow_id)

    assert fourth.plan_version == 4
    assert _versions(store, root.workflow_id) == [
        (root.workflow_id, 1),
        (second.workflow_id, 2),
        (fourth.workflow_id, 4),
    ]


def test_lineage_survives_its_root_being_pruned(git_repo: Path, tmp_path: Path):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    store = coordinator.store
    spec = read_workflow_document(_raw(_doc()))
    root = coordinator.create_from_spec(spec)
    second = coordinator.create_from_spec(spec, parent_workflow_id=root.workflow_id)
    _prunable(store, root.workflow_id)
    assert store.prune(keep=0, retention_days=1, dry_run=False) == [root.workflow_id]

    assert _versions(store, second.workflow_id) == [(second.workflow_id, 2)]
    third = coordinator.create_from_spec(spec, parent_workflow_id=second.workflow_id)
    assert third.plan_version == 3
    assert third.lineage_id == second.lineage_id


def _legacy_insert(store: WorkflowStore, record) -> None:
    """Insert a row the way a build without lineages does: none of the new
    columns named, so they take their defaults."""
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "INSERT INTO workflows (workflow_id, created_at, updated_at, state, "
            "repo_root, source_fingerprint, planner_agent, spec_json, "
            "manifest_json, final_run_id, lease_id, team, lease_owner, "
            "lease_heartbeat) VALUES (?, ?, ?, 'planned', ?, ?, ?, ?, ?, NULL, "
            "NULL, NULL, NULL, NULL)",
            (
                record.workflow_id,
                record.created_at,
                record.created_at,
                record.repo_root,
                record.source_fingerprint,
                record.planner_agent,
                record.spec.model_dump_json(),
                json.dumps(record.manifest),
            ),
        )


def test_a_legacy_row_is_a_lineage_of_its_own_until_revised(
    git_repo: Path, tmp_path: Path
):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    store = coordinator.store
    legacy = coordinator.plan("Ship the reviewed change")
    _set(store, legacy.workflow_id, lineage_id=None)

    loaded = store.load(legacy.workflow_id)
    assert loaded.lineage_id == legacy.workflow_id
    assert loaded.plan_origin == "planner"
    assert loaded.plan_version == 1
    assert _versions(store, legacy.workflow_id) == [(legacy.workflow_id, 1)]
    assert f"lineage: {legacy.workflow_id} version=1" in render_workflow(loaded)

    revision = coordinator.create_from_spec(
        read_workflow_document(_raw(_doc())), parent_workflow_id=legacy.workflow_id
    )

    assert revision.plan_version == 2
    assert revision.lineage_id.startswith("wl-")
    assert store.load(legacy.workflow_id).lineage_id == revision.lineage_id
    assert _versions(store, legacy.workflow_id) == [
        (legacy.workflow_id, 1),
        (revision.workflow_id, 2),
    ]


def test_an_older_build_reusing_a_pruned_id_does_not_join_its_lineage(
    git_repo: Path, tmp_path: Path
):
    """Round 2 of the design review: with the root's own id as the lineage id,
    an older build that recreated the pruned root joined the revisions."""
    coordinator, _ = _coordinator(git_repo, tmp_path)
    store = coordinator.store
    spec = read_workflow_document(_raw(_doc()))
    root = coordinator.create_from_spec(spec)
    second = coordinator.create_from_spec(spec, parent_workflow_id=root.workflow_id)
    _prunable(store, root.workflow_id)
    assert store.prune(keep=0, retention_days=1, dry_run=False) == [root.workflow_id]

    _legacy_insert(store, root)

    assert _versions(store, root.workflow_id) == [(root.workflow_id, 1)]
    assert _versions(store, second.workflow_id) == [(second.workflow_id, 2)]
    third = coordinator.create_from_spec(spec, parent_workflow_id=second.workflow_id)
    assert third.plan_version == 3
    assert _versions(store, root.workflow_id) == [(root.workflow_id, 1)]


def test_concurrent_revisions_get_distinct_versions(git_repo: Path, tmp_path: Path):
    config = _config(git_repo, tmp_path)
    coordinator, _ = _coordinator(git_repo, tmp_path, config)
    spec = read_workflow_document(_raw(_doc()))
    parent = coordinator.create_from_spec(spec)
    barrier = threading.Barrier(6)
    results: list[int] = []
    errors: list[BaseException] = []

    def revise() -> None:
        # Each thread its own coordinator and store, as separate processes are.
        mine = WorkflowCoordinator(config, coordinator.registry)
        barrier.wait()
        try:
            results.append(
                mine.create_from_spec(spec, parent_workflow_id=parent.workflow_id).plan_version
            )
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=revise) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(results) == [2, 3, 4, 5, 6, 7]


def test_a_revision_reads_and_writes_its_version_in_one_transaction(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """Deterministic interleaving: each revise waits, after choosing its
    version, for the other to choose one too. Under one write transaction the
    second cannot start choosing until the first has committed, so the wait
    times out and both versions differ; without it both choose v2."""
    config = _config(git_repo, tmp_path)
    coordinator, _ = _coordinator(git_repo, tmp_path, config)
    spec = read_workflow_document(_raw(_doc()))
    parent = coordinator.create_from_spec(spec)
    real = WorkflowStore._allocate_revision
    rendezvous = threading.Barrier(2)

    def allocate_and_wait(connection, parent_id, repo_root):
        allocated = real(connection, parent_id, repo_root)
        try:
            rendezvous.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return allocated

    monkeypatch.setattr(WorkflowStore, "_allocate_revision", staticmethod(allocate_and_wait))
    outcomes: list[object] = []

    def revise() -> None:
        mine = WorkflowCoordinator(config, coordinator.registry)
        try:
            outcomes.append(
                mine.create_from_spec(spec, parent_workflow_id=parent.workflow_id).plan_version
            )
        except Exception as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=revise) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [2, 3]


def test_a_failed_insert_leaves_no_row_directory_or_version_behind(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    store = coordinator.store
    spec = read_workflow_document(_raw(_doc()))
    parent = coordinator.create_from_spec(spec)
    real = WorkflowStore._allocate_revision

    def allocate_then_fail(connection, parent_id, repo_root):
        lineage_id, version = real(connection, parent_id, repo_root)
        # Collide with the parent's own version: the unique index refuses it.
        return lineage_id, 1

    with monkeypatch.context() as patch:
        patch.setattr(
            WorkflowStore, "_allocate_revision", staticmethod(allocate_then_fail)
        )
        with pytest.raises(sqlite3.IntegrityError):
            coordinator.create_from_spec(spec, parent_workflow_id=parent.workflow_id)

    assert [r.workflow_id for r in store.list()] == [parent.workflow_id]
    assert sorted(p.name for p in store.root.iterdir() if p.name.startswith("wf-")) == [
        parent.workflow_id
    ]
    revision = coordinator.create_from_spec(spec, parent_workflow_id=parent.workflow_id)
    assert revision.plan_version == 2


def test_render_shows_origin_lineage_and_parent(git_repo: Path, tmp_path: Path):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    spec = read_workflow_document(_raw(_doc()))
    parent = coordinator.create_from_spec(spec)
    revision = coordinator.create_from_spec(spec, parent_workflow_id=parent.workflow_id)
    lines = render_workflow(revision).splitlines()
    assert "plan_origin: revision" in lines
    assert f"lineage: {parent.lineage_id} version=2" in lines
    assert f"parent_workflow: {parent.workflow_id}" in lines
    assert not any(line.startswith("parent_workflow:") for line in render_workflow(parent).splitlines())


# ── CLI ────────────────────────────────────────────────────────────────────


def _cli(monkeypatch, coordinator):
    supervisor = SimpleNamespace(
        workflow_store=coordinator.store,
        registry=coordinator.registry,
        workflow_preflight_warnings=[],
        create_workflow=coordinator.create_from_spec,
        close=lambda: None,
    )
    monkeypatch.setattr(
        "syncmill.cli._workflow_supervisor",
        lambda repo, team: (supervisor, coordinator.config),
    )
    monkeypatch.setattr("syncmill.cli._peek_workflow_team", lambda repo, workflow_id: None)
    return supervisor


def test_cli_validate_writes_nothing(git_repo: Path, tmp_path: Path, monkeypatch):
    config = _config(git_repo, tmp_path)
    monkeypatch.setattr("syncmill.cli._workflow_config", lambda repo, team: config)

    def forbidden(*args, **kwargs):
        raise AssertionError("validate must not build state")

    for name in (
        "Supervisor",
        "WorkflowStore",
        "cleanup_orphaned_worktrees",
        "sweep_orphaned_workspaces",
        "_configure_project_memory",
    ):
        monkeypatch.setattr(f"syncmill.cli.{name}", forbidden)
    document = tmp_path / "plan.json"
    document.write_bytes(_raw(_doc()))

    result = CliRunner().invoke(cli, ["workflow", "validate", str(document)])

    assert result.exit_code == 0, result.output
    digest = read_workflow_document(_raw(_doc())).digest()
    assert result.output == f"valid workflow schema=1 digest={digest}\n"
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "workspaces").exists()


def test_cli_validate_reports_policy_and_document_errors(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    config = _config(git_repo, tmp_path)
    monkeypatch.setattr("syncmill.cli._workflow_config", lambda repo, team: config)
    document = _doc()
    document["nodes"][0]["agent"] = "codex"
    result = CliRunner().invoke(cli, ["workflow", "validate", "-"], input=_raw(document))
    assert result.exit_code == 1
    assert "unavailable agent(s): codex" in result.output

    result = CliRunner().invoke(cli, ["workflow", "validate", "-"], input=b"[]")
    assert result.exit_code == 1
    assert "one JSON object" in result.output

    result = CliRunner().invoke(cli, ["workflow", "validate", str(tmp_path / "missing.json")])
    assert result.exit_code == 1
    assert "cannot read workflow document" in result.output


def test_cli_create_revise_lineage(git_repo: Path, tmp_path: Path, monkeypatch):
    coordinator, agent = _coordinator(git_repo, tmp_path)
    _cli(monkeypatch, coordinator)
    runner = CliRunner()

    created = runner.invoke(cli, ["workflow", "create", "--file", "-"], input=_raw(_doc()))
    assert created.exit_code == 0, created.output
    assert "plan_origin: file" in created.output
    assert "No provider was called" in created.output
    (root,) = coordinator.store.list()

    revised = runner.invoke(
        cli, ["workflow", "revise", root.workflow_id, "--file", "-"], input=_raw(_doc())
    )
    assert revised.exit_code == 0, revised.output
    assert f"Revises {root.workflow_id}, which was not modified." in revised.output
    revision = next(r for r in coordinator.store.list() if r.workflow_id != root.workflow_id)

    lineage = runner.invoke(cli, ["workflow", "lineage", revision.workflow_id])
    assert lineage.exit_code == 0, lineage.output
    lines = lineage.output.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(f"  v1  {root.workflow_id}  file ")
    assert "parent=-" in lines[0]
    assert lines[1].startswith(f"* v2  {revision.workflow_id}  revision ")
    assert f"parent={root.workflow_id}" in lines[1]
    assert agent.calls == []


def test_cli_revise_uses_the_parents_team_by_default(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    parent = coordinator.create_from_spec(read_workflow_document(_raw(_doc())))
    _cli(monkeypatch, coordinator)
    seen: list[str | None] = []
    supervisor = SimpleNamespace(
        workflow_store=coordinator.store,
        create_workflow=coordinator.create_from_spec,
        close=lambda: None,
    )

    def capture(repo, team):
        seen.append(team)
        return supervisor, coordinator.config

    monkeypatch.setattr("syncmill.cli._workflow_supervisor", capture)
    monkeypatch.setattr("syncmill.cli._peek_workflow_team", lambda repo, wid: "parent-team")
    runner = CliRunner()
    args = ["workflow", "revise", parent.workflow_id, "--file", "-"]
    assert runner.invoke(cli, args, input=_raw(_doc())).exit_code == 0
    assert runner.invoke(cli, [*args, "--team", "other"], input=_raw(_doc())).exit_code == 0
    assert seen == ["parent-team", "other"]


def test_cli_export_stdout_and_file_are_the_same_bytes(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    document = _doc()
    document["goal"] = "g" + C1_CSI + chr(0x1B)
    document["nodes"][0]["prompt"] = "x" + ASTRAL_FORMAT + BIDI_OVERRIDE + EMOJI
    record = coordinator.create_from_spec(read_workflow_document(_raw(document)))
    _cli(monkeypatch, coordinator)
    runner = CliRunner()

    stdout = runner.invoke(cli, ["workflow", "export", record.workflow_id])
    assert stdout.exit_code == 0, stdout.output
    target = tmp_path / "plan.json"
    written = runner.invoke(cli, ["workflow", "export", record.workflow_id, "--output", str(target)])
    assert written.exit_code == 0, written.output

    canonical = canonical_workflow_json(record.spec)
    assert stdout.stdout_bytes == canonical
    assert target.read_bytes() == canonical
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert read_workflow_document(stdout.stdout_bytes).digest() == record.spec.digest()


def test_cli_export_to_stdout_ignores_the_stdout_encoding(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """Text written to a latin-1 stdout would turn the UTF-8 document into
    bytes that are not UTF-8 at all."""
    coordinator, _ = _coordinator(git_repo, tmp_path)
    record = coordinator.create_from_spec(
        read_workflow_document(_raw(_doc(goal="caf" + chr(0xE9) + " " + EMOJI)))
    )
    _cli(monkeypatch, coordinator)
    result = CliRunner(charset="latin-1").invoke(
        cli, ["workflow", "export", record.workflow_id]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == canonical_workflow_json(record.spec)


def test_cli_export_refuses_to_replace_without_force(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    record = coordinator.create_from_spec(read_workflow_document(_raw(_doc())))
    _cli(monkeypatch, coordinator)
    runner = CliRunner()
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"keep me")
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"untouched")
    link = tmp_path / "link.json"
    link.symlink_to(victim)
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "nowhere")

    for path in (existing, link, dangling):
        result = runner.invoke(cli, ["workflow", "export", record.workflow_id, "--output", str(path)])
        assert result.exit_code == 1
        assert "already exists; pass --force" in result.output
    assert existing.read_bytes() == b"keep me"
    assert victim.read_bytes() == b"untouched"
    assert not (tmp_path / "nowhere").exists()

    forced = runner.invoke(
        cli, ["workflow", "export", record.workflow_id, "--output", str(existing), "--force"]
    )
    assert forced.exit_code == 0, forced.output
    assert existing.read_bytes() == canonical_workflow_json(record.spec)
    assert os.stat(existing).st_mode & 0o777 == 0o600
    # --force replaces the link itself, never what it points at.
    forced = runner.invoke(
        cli, ["workflow", "export", record.workflow_id, "--output", str(link), "--force"]
    )
    assert forced.exit_code == 0, forced.output
    assert not link.is_symlink()
    assert victim.read_bytes() == b"untouched"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    usage = runner.invoke(cli, ["workflow", "export", record.workflow_id, "--force"])
    assert usage.exit_code == 2
    assert "--force needs --output" in usage.output


def test_cli_export_of_an_unknown_workflow(git_repo: Path, tmp_path: Path, monkeypatch):
    coordinator, _ = _coordinator(git_repo, tmp_path)
    _cli(monkeypatch, coordinator)
    result = CliRunner().invoke(cli, ["workflow", "export", "wf-" + "0" * 16])
    assert result.exit_code == 1
    assert "unknown workflow" in result.output
