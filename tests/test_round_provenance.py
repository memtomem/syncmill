"""Round provenance (#172 PR 4): each round's effective starting tree and gate
settings, recorded in the round's own INSERT and judged by one validator that
the writer and every reader share."""

from __future__ import annotations

import json
import sqlite3

import pytest

from syncmill.config import SyncmillConfig
from syncmill.rooms import (
    RoomCoordinator,
    RoomError,
    RoomStore,
    round_provenance_status,
)
import syncmill.rooms as rooms_module

from test_rooms import WritingAgent

DIGEST = "a" * 64


def _config(tmp_path, **gate) -> SyncmillConfig:
    return SyncmillConfig(
        project={"id": "provenance-tests"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True, "max_members": 3, "max_parallel": 3},
        gate=gate or {},
    )


def _record(tree: dict, *, lineage: str = "continue-own", gate: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "requested_lineage": lineage,
        "tree": tree,
        "gate": gate or {"kind": "none"},
    }


def _check(record, *, round_number=2, members=("a", "b"), candidates=None):
    raw = record if record is None or isinstance(record, str) else json.dumps(record)
    return round_provenance_status(
        raw,
        round_number=round_number,
        member_ids=list(members),
        candidates=candidates if candidates is not None else _CANDIDATES,
    )


_CANDIDATES = {
    "ca1": {"id": "ca1", "member_id": "a", "round_number": 1, "eligible": 1, "patch_sha256": DIGEST},
    "cb1": {"id": "cb1", "member_id": "b", "round_number": 1, "eligible": 0, "patch_sha256": "b" * 64},
    "ca2": {"id": "ca2", "member_id": "a", "round_number": 2, "eligible": 1, "patch_sha256": "c" * 64},
}
_BRANCH = {
    "kind": "branch",
    "base_candidate_id": "ca1",
    "base_patch_sha256": DIGEST,
    "base_round_number": 1,
}
_CONTINUED = {
    "kind": "continued",
    "members": {
        "a": {"parent_candidate_id": "ca1", "parent_round_number": 1},
        "b": None,
    },
}


# ---------------------------------------------------------------- validator


@pytest.mark.parametrize(
    "record, round_number",
    [
        (_record({"kind": "snapshot"}), 1),
        (_record({"kind": "snapshot"}, lineage="fresh"), 3),
        (_record(_BRANCH, lineage="branch-from"), 2),
        (_record(_CONTINUED), 2),
        (
            _record(
                {
                    "kind": "continued",
                    "members": {
                        "a": {"parent_candidate_id": "ca2", "parent_round_number": 2},
                        "b": None,
                    },
                }
            ),
            3,
        ),
        (_record({"kind": "snapshot"}, gate={"kind": "commands", "commands": [""], "timeout_seconds": 0}), 1),
    ],
)
def test_valid_records_are_recorded(record, round_number):
    check = _check(record, round_number=round_number)
    assert check.status == "recorded", check.reason
    assert check.record == record


def test_only_sql_null_is_not_recorded():
    assert _check(None).status == "not_recorded"
    for raw in ("", "null", "{}", "[]", "x"):
        assert _check(raw).status == "malformed"


def _with(base: dict, path: tuple, value) -> dict:
    record = json.loads(json.dumps(base))
    target = record
    for key in path[:-1]:
        target = target[key]
    if value is _DROP:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return record


_DROP = object()
_BRANCH_RECORD = _record(_BRANCH, lineage="branch-from")
_CONTINUED_RECORD = _record(_CONTINUED)


@pytest.mark.parametrize(
    "record, round_number, reason",
    [
        (_with(_CONTINUED_RECORD, ("schema_version",), 2), 2, "version"),
        (_with(_CONTINUED_RECORD, ("schema_version",), True), 2, "version"),
        (_with(_CONTINUED_RECORD, ("extra",), 1), 2, "keys"),
        (_with(_CONTINUED_RECORD, ("gate",), _DROP), 2, "keys"),
        (_with(_CONTINUED_RECORD, ("requested_lineage",), "rebase"), 2, "lineage"),
        (_with(_CONTINUED_RECORD, ("gate",), {"kind": "none", "commands": []}), 2, "gate keys"),
        (_with(_CONTINUED_RECORD, ("gate",), {"kind": "commands", "commands": [], "timeout_seconds": 1}), 2, "commands"),
        (_with(_CONTINUED_RECORD, ("gate",), {"kind": "commands", "commands": [1], "timeout_seconds": 1}), 2, "commands"),
        (_with(_CONTINUED_RECORD, ("gate",), {"kind": "commands", "commands": ["x"], "timeout_seconds": True}), 2, "timeout"),
        (_with(_CONTINUED_RECORD, ("gate",), {"kind": "maybe"}), 2, "gate kind"),
        (_with(_CONTINUED_RECORD, ("gate",), {"kind": []}), 2, "gate kind"),
        (_with(_CONTINUED_RECORD, ("tree", "kind"), []), 2, "tree kind"),
        (_with(_CONTINUED_RECORD, ("tree", "kind"), {}), 2, "tree kind"),
        # lineage/tree compatibility
        (_record({"kind": "snapshot"}, lineage="branch-from"), 2, "cannot result"),
        (_record(_BRANCH, lineage="fresh"), 2, "cannot result"),
        (_record(_CONTINUED, lineage="fresh"), 2, "cannot result"),
        (_record(_CONTINUED), 1, "cannot result|round 1"),
        (_with(_record({"kind": "snapshot"}), ("tree", "extra"), 1), 1, "tree keys"),
        # branch binding
        (_with(_BRANCH_RECORD, ("tree", "base_candidate_id"), "missing"), 2, "not a candidate"),
        (_with(_BRANCH_RECORD, ("tree", "base_patch_sha256"), "b" * 64), 2, "digest"),
        (_with(_BRANCH_RECORD, ("tree", "base_round_number"), 2), 3, "round"),
        (_with(_with(_BRANCH_RECORD, ("tree", "base_candidate_id"), "ca2"), ("tree", "base_round_number"), 2), 2, "earlier"),
        (
            _with(
                _with(_BRANCH_RECORD, ("tree", "base_candidate_id"), "cb1"),
                ("tree", "base_patch_sha256"),
                "b" * 64,
            ),
            2,
            "eligible",
        ),
        # continued binding
        (_with(_CONTINUED_RECORD, ("tree", "members", "c"), None), 2, "members"),
        (_with(_CONTINUED_RECORD, ("tree", "members", "b"), _DROP), 2, "members"),
        (_with(_CONTINUED_RECORD, ("tree", "members", "b"), {"parent_candidate_id": "ca1", "parent_round_number": 1}), 2, "that member"),
        (_with(_CONTINUED_RECORD, ("tree", "members", "a"), {"parent_candidate_id": "ca2", "parent_round_number": 1}), 2, "previous round"),
        (_with(_CONTINUED_RECORD, ("tree", "members", "a"), {"parent_candidate_id": "ca1", "parent_round_number": 1}), 3, "previous round"),
        (_with(_CONTINUED_RECORD, ("tree", "members", "a"), {"parent_candidate_id": "ca1"}), 2, "malformed"),
    ],
)
def test_malformed_or_unbound_records_are_never_recorded(record, round_number, reason):
    check = _check(record, round_number=round_number)
    assert check.status == "malformed"
    assert check.record is None
    assert any(part in check.reason for part in reason.split("|")), check.reason


# -------------------------------------------------------------------- store


def _store_room(tmp_path):
    store = RoomStore(tmp_path / "rooms")
    room = store.create_room(repo_root=tmp_path, prompt="p", agents=["a", "b"])
    return store, room


def _start(store, room, **kwargs):
    return store.start_round(
        room["id"],
        prompt="p",
        source_fingerprint="fp",
        snapshot_commit="base",
        lease_owner="w",
        lease_timeout_seconds=60,
        max_rounds=5,
        **kwargs,
    )


def test_start_round_writes_the_record_in_its_insert(tmp_path):
    store, room = _store_room(tmp_path)
    try:
        record = _record({"kind": "snapshot"})
        row = _start(store, room, provenance=record)
        assert json.loads(row["provenance_json"]) == record
        assert store.get_round(row["id"])["provenance_json"] == row["provenance_json"]
    finally:
        store.close()


def test_start_round_without_provenance_leaves_legacy_null(tmp_path):
    store, room = _store_room(tmp_path)
    try:
        assert _start(store, room)["provenance_json"] is None
    finally:
        store.close()


def test_invalid_provenance_inserts_no_round(tmp_path):
    store, room = _store_room(tmp_path)
    try:
        with pytest.raises(RoomError, match="invalid round provenance: round 1"):
            _start(store, room, provenance=_record(_CONTINUED))
        after = store.get_room(room["id"])
        assert after["round_count"] == 0
        assert after["state"] == "draft"
        assert after["revision"] == room["revision"]
        assert store.list_rounds(room["id"]) == []
    finally:
        store.close()


def test_provenance_column_migrates_onto_an_old_database(tmp_path):
    root = tmp_path / "rooms"
    seed = RoomStore(root)
    db = seed.db_path
    seed.close()
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE rounds DROP COLUMN provenance_json")
    conn.commit()
    conn.close()
    store = RoomStore(root)
    try:
        columns = {r["name"] for r in store._conn.execute("PRAGMA table_info(rounds)")}
        assert "provenance_json" in columns
    finally:
        store.close()


# -------------------------------------------------------------- coordinator


def _provenance(coordinator, room_id, number):
    [row] = [r for r in coordinator.store.list_rounds(room_id) if r["number"] == number]
    check = round_provenance_status(
        row["provenance_json"],
        round_number=number,
        member_ids=[m["member_id"] for m in coordinator.store.get_room(room_id)["members"]],
        candidates=coordinator.store.candidate_rows(room_id),
    )
    assert check.status == "recorded", check.reason
    return check.record


def _round_candidates(coordinator, room_id, number):
    return {
        c["member_id"]: c
        for c in coordinator.store.list_candidates(room_id)
        if c["round_number"] == number
    }


def test_every_lineage_records_its_effective_tree(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )
    try:
        room = coordinator.create("task", ["claude", "codex"])
        # Round 1 under the default continue-own: no workspaces exist yet, so
        # the effective tree is a snapshot while the request stays recorded.
        coordinator.run_round(room["id"])
        assert _provenance(coordinator, room["id"], 1) == _record({"kind": "snapshot"})

        coordinator.run_round(room["id"], "two")
        first = _round_candidates(coordinator, room["id"], 1)
        assert _provenance(coordinator, room["id"], 2)["tree"] == {
            "kind": "continued",
            "members": {
                member: {"parent_candidate_id": first[member]["id"], "parent_round_number": 1}
                for member in ("claude", "codex")
            },
        }

        base = _round_candidates(coordinator, room["id"], 2)["claude"]
        coordinator.run_round(
            room["id"], "three", lineage="branch-from", base_candidate_id=base["id"]
        )
        assert _provenance(coordinator, room["id"], 3) == _record(
            {
                "kind": "branch",
                "base_candidate_id": base["id"],
                "base_patch_sha256": base["patch_sha256"],
                "base_round_number": 2,
            },
            lineage="branch-from",
        )

        coordinator.run_round(room["id"], "four", lineage="fresh")
        assert _provenance(coordinator, room["id"], 4) == _record(
            {"kind": "snapshot"}, lineage="fresh"
        )
    finally:
        coordinator.close()


def test_recorded_gate_is_exactly_what_build_gate_receives(git_repo, tmp_path, monkeypatch):
    received = []
    real = rooms_module.build_gate

    def spy(config, **kwargs):
        received.append(config.model_dump())
        return real(config, **kwargs)

    monkeypatch.setattr(rooms_module, "build_gate", spy)
    coordinator = RoomCoordinator(
        _config(tmp_path, commands=["true", "python -c pass"], timeout_seconds=17),
        git_repo,
        agents=[WritingAgent("claude")],
    )
    try:
        room = coordinator.create("task")
        # A config change after the round started must not reach its gate.
        real_build = coordinator.store.start_round

        def start_then_mutate(*args, **kwargs):
            row = real_build(*args, **kwargs)
            coordinator.config.gate.commands.append("false")
            coordinator.config.gate.timeout_seconds = 99
            return row

        monkeypatch.setattr(coordinator.store, "start_round", start_then_mutate)
        coordinator.run_round(room["id"])
        gate = _provenance(coordinator, room["id"], 1)["gate"]
        assert gate == {
            "kind": "commands",
            "commands": ["true", "python -c pass"],
            "timeout_seconds": 17,
        }
        assert received == [{"commands": ["true", "python -c pass"], "timeout_seconds": 17}]
    finally:
        coordinator.close()


def test_no_configured_gate_is_recorded_as_none(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        room = coordinator.create("task")
        coordinator.run_round(room["id"])
        assert _provenance(coordinator, room["id"], 1)["gate"] == {"kind": "none"}
    finally:
        coordinator.close()
