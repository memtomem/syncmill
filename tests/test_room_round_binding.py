"""A candidate is read with its own room's round, or with no round at all.

A candidate row whose ``round_id`` names a round that is gone, belongs to
another room, or was renumbered must never lend a borrowed number to the
readers that order candidates by round. These tests damage a real store the
way a direct ``rooms.db`` edit would.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from syncmill.room_compare import build_room_comparison
from syncmill.rooms import (
    RoomCoordinator,
    RoomError,
    _repair_tree_problem,
    round_conversation_status,
)
from test_rooms import SessionAgent, WritingAgent, _config


def _sql(coordinator: RoomCoordinator, statement: str, *params) -> None:
    conn = coordinator.store._conn
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(statement, params)
    conn.commit()


def _candidates(coordinator, room_id, member_id):
    """The member's candidates, oldest first."""
    return [
        row
        for row in coordinator.store.list_candidates(room_id)
        if row["member_id"] == member_id
    ]


def _round_id(coordinator, room_id, number):
    return next(
        r["id"] for r in coordinator.store.list_rounds(room_id) if r["number"] == number
    )


@pytest.fixture
def coordinator(git_repo, tmp_path):
    made = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )
    yield made
    made.close()


# ------------------------------------------------------------- read model


def test_a_candidate_never_borrows_another_rooms_round_number(coordinator):
    """The issue's reproduction: room A's candidate pointed at room B's round 2."""
    room_a = coordinator.create("a", ["claude"])["id"]
    coordinator.run_round(room_a, "one")
    (candidate,) = _candidates(coordinator, room_a, "claude")
    assert candidate["round_number"] == 1
    coordinator.close_room(room_a)
    room_b = coordinator.create("b", ["claude"])["id"]
    coordinator.run_round(room_b, "one")
    coordinator.run_round(room_b, "two")
    _sql(
        coordinator,
        "UPDATE candidates SET round_id=?, attempt=99 WHERE id=?",
        _round_id(coordinator, room_b, 2),
        candidate["id"],
    )

    rows = coordinator.store.candidate_rows(room_a)
    assert rows[candidate["id"]]["round_number"] is None
    assert candidate["id"] not in coordinator.store.candidate_rows(room_b)
    with pytest.raises(RoomError):
        coordinator.store.load_candidate(candidate["id"])


def test_a_candidate_of_a_vanished_round_stays_listed_without_a_number(coordinator):
    room = coordinator.create("a", ["claude"])["id"]
    coordinator.run_round(room, "one")
    (candidate,) = _candidates(coordinator, room, "claude")
    _sql(
        coordinator,
        "UPDATE candidates SET round_id=? WHERE id=?",
        str(uuid.uuid4()),
        candidate["id"],
    )
    rows = coordinator.store.candidate_rows(room)
    assert rows[candidate["id"]]["round_number"] is None


@pytest.mark.parametrize("damage", ["moved", "deleted"])
def test_the_artifact_refuses_a_candidate_whose_round_left_the_room(coordinator, damage):
    """The manifest still matches the candidate row; the round does not."""
    room = coordinator.create("a", ["claude"])["id"]
    coordinator.run_round(room, "one")
    (candidate,) = _candidates(coordinator, room, "claude")
    coordinator.store.load_candidate(candidate["id"])  # readable before
    round_id = _round_id(coordinator, room, 1)
    if damage == "moved":
        _sql(coordinator, "UPDATE rounds SET room_id='elsewhere' WHERE id=?", round_id)
    else:
        _sql(coordinator, "DELETE FROM rounds WHERE id=?", round_id)
    with pytest.raises(RoomError, match="round binding mismatch"):
        coordinator.store.load_candidate(candidate["id"])
    with pytest.raises(RoomError):
        coordinator.select(room, candidate["id"])


# ---------------------------------------------------- generator == validator


def _round_ran_cleanly(coordinator, room_id, number):
    room = coordinator.store.get_room(room_id)
    assert room["state"] == "awaiting_decision", room.get("recovery_path")
    round_row = next(
        r for r in coordinator.store.list_rounds(room_id) if r["number"] == number
    )
    check = round_conversation_status(
        round_row["conversation_json"],
        round_number=number,
        members=room["members"],
        candidates=coordinator.store.candidate_rows(room_id),
    )
    assert check.status == "recorded", check.reason


@pytest.mark.parametrize("damage", ["dangling", "renumbered"])
def test_a_cold_round_runs_beside_a_damaged_candidate(coordinator, damage):
    """The checkpoint used to include a candidate its validator excluded; the
    round then froze as recovery_required before any provider ran."""
    room = coordinator.create("a", ["claude", "codex"])["id"]
    coordinator.run_round(room, "one")
    if damage == "dangling":
        (candidate,) = _candidates(coordinator, room, "codex")
        _sql(
            coordinator,
            "UPDATE candidates SET round_id=? WHERE id=?",
            str(uuid.uuid4()),
            candidate["id"],
        )
    else:
        # Round 1 now claims to be round 100; the next round is still 2.
        _sql(coordinator, "UPDATE rounds SET number=100 WHERE id=?", _round_id(coordinator, room, 1))
    coordinator.run_round(room, "two")
    _round_ran_cleanly(coordinator, room, 2)


def test_a_resumed_round_ignores_a_round_numbered_past_it(git_repo, tmp_path):
    """The delta watermark used to take any completed round's number; a round
    renumbered to 100 made it 100, which the record's validator rejected."""
    made = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[SessionAgent("claude"), SessionAgent("codex")]
    )
    try:
        room = made.create("a", ["claude", "codex"])["id"]
        made.run_round(room, "one")
        made.run_round(room, "two")
        _sql(made, "UPDATE rounds SET number=100 WHERE id=?", _round_id(made, room, 2))
        made.run_round(room, "three")
        _round_ran_cleanly(made, room, 3)
    finally:
        made.close()


# ------------------------------------------------------------- repair


def test_repair_refuses_when_the_members_order_is_unknown(coordinator):
    room = coordinator.create("a", ["claude", "codex"])["id"]
    coordinator.run_round(room, "one")
    coordinator.run_round(room, "two")
    older, newer = _candidates(coordinator, room, "claude")
    codex_newest = _candidates(coordinator, room, "codex")[-1]
    _sql(
        coordinator,
        "UPDATE candidates SET round_id=? WHERE id=?",
        str(uuid.uuid4()),
        newer["id"],
    )
    problem = coordinator.store.repair_parent_problem(room, older["id"])
    assert problem is not None and "order" in problem
    assert coordinator.store.repair_parent_problem(room, newer["id"]) is not None
    # The other member's order is intact.
    assert coordinator.store.repair_parent_problem(room, codex_newest["id"]) is None


def test_repair_refuses_a_parent_numbered_past_the_next_round(coordinator):
    room = coordinator.create("a", ["claude"])["id"]
    coordinator.run_round(room, "one")
    coordinator.run_round(room, "two")
    newest = _candidates(coordinator, room, "claude")[-1]
    assert coordinator.store.repair_parent_problem(room, newest["id"]) is None
    _sql(coordinator, "UPDATE rounds SET number=100 WHERE id=?", _round_id(coordinator, room, 2))
    problem = coordinator.store.repair_parent_problem(room, newest["id"])
    assert problem == "repair parent is not from an earlier round"


def test_the_repair_guard_and_its_record_agree_past_the_next_round(coordinator):
    """A candidate numbered past the next round is not "newer" for either: the
    guard used to send the operator to it, a parent the record rejects."""
    room = coordinator.create("a", ["claude"])["id"]
    coordinator.run_round(room, "one")
    coordinator.run_round(room, "two")
    older, newer = _candidates(coordinator, room, "claude")
    _sql(coordinator, "UPDATE rounds SET number=100 WHERE id=?", _round_id(coordinator, room, 2))
    assert coordinator.store.repair_parent_problem(room, older["id"]) is None
    assert coordinator.store.repair_parent_problem(room, newer["id"]) == (
        "repair parent is not from an earlier round"
    )


def test_the_repair_record_validator_shares_the_unknown_order_rule():
    digest = "a" * 64
    tree = {
        "kind": "repair",
        "member_id": "claude",
        "parent_candidate_id": "p",
        "parent_patch_sha256": digest,
        "parent_round_number": 1,
        "feedback_ids": [],
        "agent_task_sha256": digest,
    }
    parent = {"member_id": "claude", "state": "completed", "round_number": 1, "patch_sha256": digest}
    assert _repair_tree_problem(tree, 3, ["claude"], {"p": parent}) is None
    lost = {"member_id": "claude", "state": "completed", "round_number": None}
    assert _repair_tree_problem(tree, 3, ["claude"], {"p": parent, "q": lost}) == (
        "repair parent's order among that member's candidates is unknown"
    )


# ------------------------------------------------------------- branch-from


def _workspace_state(coordinator, room_id):
    rows = coordinator.store.list_workspaces(room_id)
    return [(row["member_id"], row["path"]) for row in rows]


def test_branch_from_a_renumbered_base_keeps_the_workspaces(coordinator):
    room = coordinator.create("a", ["claude"])["id"]
    coordinator.run_round(room, "one")
    coordinator.run_round(room, "two")
    base = _candidates(coordinator, room, "claude")[0]
    before = _workspace_state(coordinator, room)
    assert before and all(Path(path).is_dir() for _, path in before)
    _sql(coordinator, "UPDATE rounds SET number=100 WHERE id=?", _round_id(coordinator, room, 1))
    with pytest.raises(RoomError, match="not from an earlier round"):
        coordinator.preflight_round(
            room, "three", lineage="branch-from", base_candidate_id=base["id"]
        )
    with pytest.raises(RoomError, match="not from an earlier round"):
        coordinator.run_round(
            room, "three", lineage="branch-from", base_candidate_id=base["id"]
        )
    assert _workspace_state(coordinator, room) == before
    assert all(Path(path).is_dir() for _, path in before)


def test_branch_from_checks_the_base_again_before_removing_workspaces(
    coordinator, monkeypatch
):
    """A base damaged after the preflight passed is still refused before any
    workspace is removed."""
    room = coordinator.create("a", ["claude"])["id"]
    coordinator.run_round(room, "one")
    coordinator.run_round(room, "two")
    base = _candidates(coordinator, room, "claude")[0]
    before = _workspace_state(coordinator, room)
    real_preflight = coordinator.preflight_round

    def preflight_then_damage(*args, **kwargs):
        room_row = real_preflight(*args, **kwargs)
        _sql(coordinator, "UPDATE rounds SET number=100 WHERE id=?", _round_id(coordinator, room, 1))
        return room_row

    monkeypatch.setattr(coordinator, "preflight_round", preflight_then_damage)
    with pytest.raises(RoomError, match="not from an earlier round"):
        coordinator.run_round(
            room, "three", lineage="branch-from", base_candidate_id=base["id"]
        )
    assert _workspace_state(coordinator, room) == before
    assert all(Path(path).is_dir() for _, path in before)


# ------------------------------------------------------------- compare


def test_compare_reports_a_candidate_outside_the_rooms_rounds(coordinator):
    room = coordinator.create("a", ["claude", "codex"])["id"]
    coordinator.run_round(room, "one")
    (candidate,) = _candidates(coordinator, room, "codex")
    stray = str(uuid.uuid4())
    _sql(coordinator, "UPDATE candidates SET round_id=? WHERE id=?", stray, candidate["id"])
    comparison = build_room_comparison(coordinator.store, room, all_rounds=True)
    assert {
        "round_id": stray,
        "reason": f"candidate {candidate['id']} names a round outside this room",
    } in list(comparison.damaged_rounds)
    rows = [row.candidate_id for group in comparison.groups for row in group.rows]
    assert candidate["id"] not in rows


# ------------------------------------------------------------- CLI


def _cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_PROJECT__ID", "room-tests")
    monkeypatch.setenv("SYNCMILL_RUNS__ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("SYNCMILL_WORKSPACE__ROOT", str(tmp_path / "workspaces"))


def test_room_candidates_lists_the_readable_rest_and_fails(
    coordinator, monkeypatch, git_repo, tmp_path
):
    from click.testing import CliRunner

    from syncmill.cli import cli

    room = coordinator.create("a", ["claude", "codex"])["id"]
    coordinator.run_round(room, "one")
    _cli_env(monkeypatch, tmp_path)
    runner = CliRunner()
    healthy = runner.invoke(cli, ["room", "candidates", room, "--repo", str(git_repo)])
    assert healthy.exit_code == 0, healthy.output

    (damaged,) = _candidates(coordinator, room, "codex")
    _sql(
        coordinator,
        "UPDATE candidates SET round_id=? WHERE id=?",
        str(uuid.uuid4()),
        damaged["id"],
    )
    res = runner.invoke(cli, ["room", "candidates", room, "--repo", str(git_repo)])
    assert res.exit_code == 1, res.output
    lines = res.output.splitlines()
    codex_line = next(line for line in lines if line.startswith(damaged["id"]))
    assert "damaged:" in codex_line and "binding mismatch" in codex_line
    # The readable candidate still lists, in its usual shape.
    claude_line = next(line for line in lines if line.split()[1:2] == ["claude"])
    assert "gate:" in claude_line and "damaged" not in claude_line
    assert f"1 candidate(s) in room {room} could not be read" in res.output
