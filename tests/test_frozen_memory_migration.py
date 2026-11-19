"""`memory migration` artifacts written by released syncmill, checked without an SDK (#271).

The artifacts, and the source and destination stores they were planned from
and written into, are frozen by ``fixtures/persisted/memory/migration/freeze.py``
under the release's own CLI. This module holds them to HEAD's loader and its
pure functions (record digests, lineage keys, target ids, the destination
binding) and checks each state's end against the stores read with the standard
library. ``test_frozen_memory_migration_real.py`` runs HEAD's commands on them.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest
from click.testing import CliRunner

from syncmill.cli import cli
from syncmill.config import SyncmillConfig
from syncmill.memory_migration import (
    MigrationError,
    MigrationRecord,
    _aggregate,
    _load,
    _record,
    _selected,
    _target_id,
    _target_storage_digest,
)

from frozen_memory_migration_support import (
    FREEZE,
    LEGACY_DIR,
    LEGACY_RELEASES,
    MIGRATION_RELEASES,
    MigrationFrozen,
    memory_settings,
    memtomem_created_at,
    release_memtomem,
    selected_by_default,
    snapshot_created_at,
)

release_param = pytest.mark.parametrize("release", MIGRATION_RELEASES)


def _labels(frozen: MigrationFrozen, *states: str) -> list[str]:
    return [label for label in frozen.labels if not states or frozen.entry(label)["state"] in states]


@release_param
def test_every_fixture_file_is_pinned(release):
    MigrationFrozen(release).verify_pins()


@release_param
def test_the_generator_covers_every_state_for_every_destination(release):
    frozen = MigrationFrozen(release)
    got = {(e["source"], e["target"], e["state"]) for e in frozen.index["artifacts"].values()}
    want = {(s, t, state) for (s, t), states in FREEZE.DIRECTIONS.items() for state in states}
    assert got == want
    for target in ("memtomem", "mem0", "langgraph"):
        assert {state for (_, t, state) in got if t == target} == set(FREEZE.STATES), target


@release_param
def test_only_memtomem_stores_hold_the_generating_path(release):
    """Mem0 and LangGraph stores and the artifacts bind no path; memtomem's
    ``source_file`` does (rebound by the real tests)."""
    frozen = MigrationFrozen(release)
    needle = frozen.generating_root.encode()
    holders = sorted(
        str(p.relative_to(frozen.root)) for p in frozen.root.rglob("*")
        if p.is_file() and p.name not in ("index.json", "bindings.json")
        and needle in p.read_bytes())
    memtomem = ["sources/memtomem/mm.db"] + [
        f"{spec['path']}/mm.db" for spec in frozen.index["destinations"].values()
        if spec["target"] == "memtomem"]
    assert holders and all(any(h.startswith(m) for m in memtomem) for h in holders), holders


@release_param
def test_head_loads_every_released_artifact(release):
    frozen = MigrationFrozen(release)
    for label in frozen.labels:
        doc = frozen.doc(label)
        manifest, records, state = _load(frozen.root / "mig" / frozen.entry(label)["migration_id"])
        assert manifest == doc["manifest"], label
        assert state == doc["state"], label
        assert [asdict(r) for r in records] == doc["snapshot"]["records"], label
        assert manifest["schema_version"] == doc["snapshot"]["schema_version"] == 2
        assert manifest["storage_binding_version"] == 1
        assert (manifest["source"], manifest["target"]) == (
            frozen.entry(label)["source"], frozen.entry(label)["target"])
        assert manifest["namespaces"] == frozen.entry(label)["namespaces"]
        floor = 1 if frozen.entry(label)["state"] == "verified" else 3
        assert manifest["record_count"] == len(records) >= floor


@release_param
def test_snapshot_digests_and_lineages_recompute_with_head(release):
    frozen = MigrationFrozen(release)
    for label in frozen.labels:
        doc = frozen.doc(label)
        source = doc["manifest"]["source"]
        records = [MigrationRecord(**r) for r in doc["snapshot"]["records"]]
        for r in records:
            assert _record(source, r.source_id, r.namespace, r.content, r.tags,
                           r.created_at) == r, (label, r.source_id)
        assert _aggregate(records) == doc["manifest"]["source_digest"], label


@release_param
def test_released_destination_bindings_recompute_with_head(release):
    frozen = MigrationFrozen(release)
    for label in frozen.labels:
        entry = frozen.entry(label)
        relative = frozen.index["destinations"][entry["direction"]]["path"]
        config = SyncmillConfig(memory=memory_settings(entry["target"], frozen.original(relative)))
        assert _target_storage_digest(config, entry["target"]) == \
            frozen.doc(label)["manifest"]["target_storage_digest"], label


@release_param
def test_snapshots_are_the_source_rows_each_plan_selected(release):
    """Against the frozen source store read with the standard library, and the
    selection rule written out: the snapshot holds every selected row, sorted
    by namespace then id, and never the finished board run's scratch row."""
    frozen = MigrationFrozen(release)
    for label in frozen.labels:
        entry = frozen.entry(label)
        source = entry["source"]
        stored = FREEZE.rows(source, frozen.source_path(source))
        # v0.1.2's memtomem store re-chunks one shared day file: the scratch
        # row's text ends up in a later write's row (#318), so none is left.
        has_scratch = any((r["namespace"] or "").startswith("board-run:")
                          for r in stored.values())
        assert has_scratch == frozen.index["sources"][source]["scratch_row"]
        assert has_scratch or (release, source) == ("v0.1.2", "memtomem")
        wanted = [
            {"source_id": i, "namespace": r["namespace"], "content": r["content"],
             "tags": r["tags"], "created_at": snapshot_created_at(source, r["created_at"])}
            for i, r in stored.items()
            if (r["namespace"] in entry["namespaces"] if entry["namespaces"]
                else selected_by_default(r["namespace"]))
        ]
        wanted.sort(key=lambda r: (r["namespace"], r["source_id"]))
        got = [{k: r[k] for k in ("source_id", "namespace", "content", "tags", "created_at")}
               for r in frozen.records(label)]
        assert got == wanted, label


def _dest_row(frozen, label, record, target_id):
    entry = frozen.entry(label)
    rows = FREEZE.rows(entry["target"], frozen.dest_path(entry["direction"]))
    return rows.get(target_id)


def _check_target(release, frozen, entry, record, target_id, row, *, changed=False):
    """A destination row the release wrote for ``record``, recorded or not:
    the record's namespace, tags and content (``changed``: plus the in-place
    suffix), the deterministic id where the backend takes one, the timestamp as
    that destination stores it, and the migration metadata or the materialized
    source file."""
    target, lineage = entry["target"], record["lineage_key"]
    want = record["content"] + (FREEZE.CHANGED_SUFFIX if changed else "")
    assert (row["namespace"], row["tags"], row["content"]) == (
        record["namespace"], record["tags"], want), (entry["migration_id"], lineage)
    if target in ("memtomem", "langgraph"):
        assert target_id == _target_id(lineage)
    if target == "memtomem":
        assert row["created_at"] == memtomem_created_at(
            record["created_at"], release_memtomem(release))
        source_file = Path(row["raw"]["source_file"])
        if release == "v0.1.2":
            # A relative path, resolved by memtomem against the apply cwd and
            # never written (v0.1.3 began writing the file).
            assert source_file == Path(frozen.bindings["cwd"]) / ".syncmill-migration" / (
                entry["migration_id"]) / f"{lineage}.md"
            assert not (frozen.root / "mig" / entry["migration_id"] / "sources").exists()
            return
        relative = source_file.relative_to(frozen.original("mig"))
        assert relative == Path(entry["migration_id"]) / "sources" / (
            hashlib.sha256(lineage.encode()).hexdigest()[:32] + ".md")
        # Bytes, not read_text: that would turn the CRLF pair into LF. The
        # file keeps what was migrated, also after an in-place rewrite.
        assert (frozen.root / "mig" / relative).read_bytes() == record["content"].encode("utf-8")
        return
    stored = row["raw"]["payload"] if target == "mem0" else row["raw"]["value"]
    assert (stored.get("syncmill_migration_id"), stored.get("syncmill_migration_lineage"),
            stored.get("syncmill_migration_digest")) == (entry["migration_id"], lineage,
                                                         record["digest"])
    if target == "mem0":
        assert row["created_at"] == record["created_at"]
    else:
        # LangGraph's put never passes the record's timestamp.
        assert row["created_at"] != record["created_at"]


@release_param
def test_every_recorded_target_is_its_record_in_the_destination(release):
    frozen = MigrationFrozen(release)
    for label in frozen.labels:
        entry = frozen.entry(label)
        doc = frozen.doc(label)
        by_lineage = {r["lineage_key"]: r for r in doc["snapshot"]["records"]}
        for lineage, target_id in doc["state"]["targets"].items():
            row = _dest_row(frozen, label, by_lineage[lineage], target_id)
            if entry["state"] == "verification_failed" and target_id == entry["tampered"]:
                # Deleted by the release before its verify (the state test).
                assert row is None
                continue
            _check_target(release, frozen, entry, by_lineage[lineage], target_id, row,
                          changed=entry["state"] == "rollback_partial")


def _rows_of(frozen, label):
    """The destination rows in this artifact's namespaces."""
    entry = frozen.entry(label)
    records = frozen.records(label)
    namespaces = {r["namespace"] for r in records}
    rows = FREEZE.rows(entry["target"], frozen.dest_path(entry["direction"]))
    return {i: r for i, r in rows.items() if r["namespace"] in namespaces}


def _unrecorded(frozen, label, lineage):
    """The destination row a crashed apply wrote for ``lineage`` without
    recording it: memtomem and LangGraph by the deterministic id, Mem0 (which
    assigns its own ids) by the lineage stored beside the row."""
    entry = frozen.entry(label)
    rows = _rows_of(frozen, label)
    if entry["target"] == "mem0":
        found = [i for i, r in rows.items()
                 if r["raw"]["payload"].get("syncmill_migration_lineage") == lineage]
        assert len(found) == 1, found
        return found[0]
    assert _target_id(lineage) in rows
    return _target_id(lineage)


@release_param
def test_each_state_is_what_its_release_left(release):
    frozen = MigrationFrozen(release)
    for label in frozen.labels:
        entry = frozen.entry(label)
        doc = frozen.doc(label)
        state, records = doc["state"], doc["snapshot"]["records"]
        lineages = [r["lineage_key"] for r in records]
        rows = _rows_of(frozen, label)
        contents = sorted(r["content"] for r in rows.values())
        exits = [s["exit"] for s in entry["steps"] if "exit" in s]
        name = entry["state"]
        if name == "planned":
            assert (state, rows, exits) == (
                {"status": "planned", "targets": {}, "errors": []}, {}, [0])
            assert set(doc) == {"manifest", "snapshot", "state", "files"}
        elif name == "crashed-first":
            assert state == {"status": "applying", "targets": {}, "errors": []}
            assert exits == [0, FREEZE.CRASH_EXIT]
            assert contents == [records[0]["content"]]
            row_id = _unrecorded(frozen, label, lineages[0])
            _check_target(release, frozen, entry, records[0], row_id, rows[row_id])
        elif name == "crashed":
            assert state["status"] == "applying" and list(state["targets"]) == [lineages[0]]
            assert exits == [0, FREEZE.CRASH_EXIT] and not state["errors"]
            assert contents == sorted([records[0]["content"], records[1]["content"]])
            row_id = _unrecorded(frozen, label, lineages[1])
            assert row_id not in state["targets"].values()
            _check_target(release, frozen, entry, records[1], row_id, rows[row_id])
        elif name == "partial":
            assert state["status"] == "partial" and list(state["targets"]) == [lineages[0]]
            assert [e["lineage"] for e in state["errors"]] == [lineages[1]]
            assert state["errors"][0]["error"] == f"RuntimeError: {FREEZE.PUT_ERROR}"
            assert exits == [0, 1] and contents == [records[0]["content"]]
        elif name == "verified":
            assert state["status"] == "verified" and set(state["targets"]) == set(lineages)
            assert contents == sorted(r["content"] for r in records) and exits == [0, 0]
            receipt = doc["receipt"]
            assert (receipt["status"], receipt["verified_count"], receipt["missing_target_count"],
                    receipt["failure_lineages"]) == ("verified", len(records), 0, [])
        elif name == "verification_failed":
            assert state["status"] == "verification_failed"
            assert set(state["targets"]) == set(lineages) and exits == [0, 0, 1]
            assert entry["tampered"] == state["targets"][lineages[1]] not in rows
            receipt = doc["receipt"]
            assert (receipt["status"], receipt["verified_count"], receipt["missing_target_count"],
                    receipt["failure_lineages"]) == (
                "verification_failed", len(records) - 1, 0, [lineages[1]])
        elif name == "rolled_back":
            assert state == {"status": "rolled_back", "targets": {}, "errors": []}
            assert rows == {} and exits == [0, 0, 0]
            assert doc["rollback"] == {"migration_id": entry["migration_id"],
                                       "status": "rolled_back", "deleted": len(records),
                                       "refused": []}
        else:
            assert state["status"] == "rollback_partial" and exits == [0, 0, 1]
            assert state["targets"] == {lineages[1]: entry["tampered"]}
            assert contents == [records[1]["content"] + FREEZE.CHANGED_SUFFIX]
            assert doc["rollback"] == {"migration_id": entry["migration_id"],
                                       "status": "rollback_partial",
                                       "deleted": len(records) - 1, "refused": [lineages[1]]}


@release_param
def test_a_snapshot_edited_in_its_digest_is_refused_and_in_its_content_is_not(release, tmp_path):
    """HEAD's loader checks only the stored (lineage, digest) pairs. A content
    edit that keeps them passes (#322); pinned as measured."""
    frozen = MigrationFrozen(release)
    label = _labels(frozen, "planned")[0]
    source = frozen.root / "mig" / frozen.entry(label)["migration_id"]
    for field, value, refused in (("digest", "0" * 64, True),
                                  ("lineage_key", "0" * 64, True),
                                  ("content", "edited", False),
                                  ("namespace", "shared", False),
                                  ("tags", ["edited"], False),
                                  ("source_id", "edited", False),
                                  ("created_at", None, False)):
        copy = tmp_path / field
        shutil.copytree(source, copy)
        snapshot = json.loads((copy / "snapshot.json").read_text(encoding="utf-8"))
        snapshot["records"][0][field] = value
        (copy / "snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")
        if refused:
            with pytest.raises(MigrationError, match="migration snapshot digest mismatch"):
                _load(copy)
        else:
            _, records, _ = _load(copy)
            assert getattr(records[0], field) == value, field


@release_param
def test_a_scratch_record_is_never_selected_even_when_named(release):
    """Unreachable from the CLI, which refuses a `board-run:` namespace first;
    the selection rule refuses it on its own."""
    record = _record("mem0", "id", "board-run:run", "body", [], None)
    assert not _selected(record, ("board-run:run",))
    assert not _selected(record, ())


# ---- v0.1.1: schema 1 artifacts, refused before any store is opened ---------

LEGACY_MESSAGES = {
    "planned": "legacy migration artifact has no destination binding; create a new plan",
    "applied": ("legacy migration artifact has no destination binding; automatic recovery is "
                "disabled; use state.json targets with the original destination settings for "
                "manual recovery"),
}


def _legacy(release: str) -> tuple[Path, dict]:
    root = LEGACY_DIR / release
    return root, json.loads((root / "index.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("release", LEGACY_RELEASES)
def test_legacy_fixture_files_are_pinned(release):
    root, index = _legacy(release)
    present = {str(p.relative_to(root)) for p in root.rglob("*")
               if p.is_file() and p.name != "index.json"}
    assert present == set(index["files"])
    for relative, digest in index["files"].items():
        assert hashlib.sha256((root / relative).read_bytes()).hexdigest() == digest, relative
    assert sorted(index["artifacts"]) == sorted(LEGACY_MESSAGES)


@pytest.mark.parametrize("release", LEGACY_RELEASES)
@pytest.mark.parametrize("state", sorted(LEGACY_MESSAGES))
def test_head_loader_refuses_a_released_schema_1_artifact(release, state):
    root, index = _legacy(release)
    path = root / "mig" / index["artifacts"][state]["migration_id"]
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1 and "target_storage_digest" not in manifest
    with pytest.raises(MigrationError) as raised:
        _load(path)
    assert str(raised.value) == LEGACY_MESSAGES[state]


@pytest.mark.parametrize("release", LEGACY_RELEASES)
@pytest.mark.parametrize("state", sorted(LEGACY_MESSAGES))
@pytest.mark.parametrize("command", [["apply"], ["verify"], ["rollback", "--yes"]])
def test_head_commands_refuse_a_released_schema_1_artifact_and_write_only_their_lock(
        release, state, command, tmp_path):
    """Each command takes the migration's lock before it loads the artifact, so
    the one new file is an empty 0600 ``.lock``; the artifact's own bytes, and
    the absence of a receipt or rollback record, stay as the release left them."""
    root, index = _legacy(release)
    migration_id = index["artifacts"][state]["migration_id"]
    mig = tmp_path / "mig"
    shutil.copytree(root / "mig" / migration_id, mig / migration_id)
    os.chmod(mig, 0o700)
    os.chmod(mig / migration_id, 0o700)
    before = {p.name: p.read_bytes() for p in (mig / migration_id).iterdir()}
    result = CliRunner().invoke(cli, ["memory", "migration", *command, migration_id],
                                env={"SYNCMILL_MIGRATIONS_DIR": str(mig)})
    assert result.exit_code == 1
    assert LEGACY_MESSAGES[state] in " ".join(result.output.split())
    after = {p.name: p.read_bytes() for p in (mig / migration_id).iterdir()}
    assert {k: v for k, v in after.items() if k != ".lock"} == before
    assert after[".lock"] == b""
    assert (mig / migration_id / ".lock").stat().st_mode & 0o777 == 0o600
