"""board.py — the persistent work-board store, state machine, atomic claims,
done-gate, stale-claim recovery, and the pure parse/strip/render helpers.

Drives the REAL ``Board`` against ``tmp_path/"board.db"`` with an injected clock
(like ``test_breaker.py``) — hermetic: SQLite in a tmp dir touches nothing real.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from syncmill.board import (
    Board,
    BoardError,
    Evidence,
    WorkItem,
    WorkState,
    build_board,
    parse_board_update,
    render_board,
    render_item,
    render_item_prompt,
    strip_board_block,
)
from syncmill.config import BoardConfig
from syncmill.models import GateCheck, GateReport, TaskResult, TaskStatus
from syncmill.review_candidates import ReviewCandidate, item_spec


class Clock:
    """An injectable monotonic-ish clock so lease/recovery timing is deterministic."""

    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def board(tmp_path, clock) -> Board:
    bd = Board(str(tmp_path / "board.db"), now=clock, lease_timeout_seconds=100)
    yield bd
    bd.close()


def _result(
    agent_id="claude",
    status=TaskStatus.COMPLETED,
    output="ok",
    error=None,
    files=None,
    gate=None,
):
    return TaskResult(
        task_id=uuid.uuid4(),
        agent_id=agent_id,
        status=status,
        output=output,
        exit_code=0 if status is TaskStatus.COMPLETED else 1,
        duration_seconds=1.0,
        files_changed=files or [],
        error=error,
        gate=gate,
    )


# ── CRUD + audit ─────────────────────────────────────────────────────────────


def test_add_creates_planned_item_with_audit(board):
    it = board.add("do the thing", goal="epic", depends_on=[], human_required=False)
    assert it.state is WorkState.PLANNED
    assert it.task == "do the thing"
    assert it.goal == "epic"
    got = board.get(it.id)
    assert got is not None and got.id == it.id and got.state is WorkState.PLANNED
    # a creation audit row exists
    rows = board.transitions(it.id)
    assert rows[0]["from_state"] is None and rows[0]["to_state"] == "planned"


def test_get_missing_returns_none(board):
    assert board.get("no-such-id") is None


def test_add_rolls_back_write_lock_on_non_sqlite_error(board):
    # codex-found blocker: _item_to_row's json.dumps raises a NON-sqlite TypeError on
    # an unserializable input AFTER BEGIN IMMEDIATE. A narrow `except sqlite3.Error`
    # would leak the open transaction → the next write fails with "cannot start a
    # transaction within a transaction". The broad catch must roll back so the lock is
    # released. Reverting add()'s catch to sqlite3.Error fails this test (kills the mut).
    with pytest.raises(BoardError, match="failed to add item"):
        board.add("bad", inputs=[object()])  # object() is not JSON-serializable
    # the write lock was released — a subsequent transactional write succeeds
    assert board.add("after the rollback") is not None


def _review_spec(**updates):
    values = {
        "run_id": "run-1",
        "pattern_id": "tool-retry-failure",
        "pattern_version": 1,
        "tool_key": "syncmill::board_stats",
        "artifact_digest": "sha256:" + "a" * 64,
    }
    values.update(updates)
    return item_spec(ReviewCandidate.model_validate(values))


def test_import_review_candidate_is_human_only_and_audited(board):
    spec = _review_spec()

    result = board.import_review_candidates([spec], goal="tracegraph-review")
    item = board.get(spec.item_id)

    assert result.imported == 1 and result.skipped == 0
    assert item is not None and item.state is WorkState.REVIEW
    assert item.human_required is True
    assert item.inputs == list(spec.inputs)
    assert item.evidence == [
        Evidence("tracegraph", spec.artifact_digest, False, 1000.0, "tracegraph")
    ]
    assert board.next_runnable("run") is None
    with pytest.raises(BoardError, match="without verification"):
        board.transition(item.id, "done", actor="human")
    assert board.transitions(item.id) == [
        {
            "from_state": None,
            "to_state": "review",
            "actor": "tracegraph-import",
            "note": "imported review candidate",
            "at": 1000.0,
        }
    ]


def test_import_is_idempotent_and_never_reopens_resolved_item(board):
    spec = _review_spec()
    board.import_review_candidates([spec], goal="first-goal")
    board.transition(
        spec.item_id,
        "done",
        actor="human",
        evidence=Evidence("human", "sign-off", True, 1001.0, "human"),
        human_required=False,
    )

    result = board.import_review_candidates([spec], goal="different-goal")
    item = board.get(spec.item_id)

    assert result.imported == 0 and result.skipped == 1
    assert item is not None and item.state is WorkState.DONE
    assert item.goal == "first-goal"
    # Goal is assigned only on first import (by design) — the result counts the
    # goal-mismatched skip so the CLI can say the requested goal was not applied.
    assert result.goal_kept == 1
    same_goal = board.import_review_candidates([spec], goal="first-goal")
    assert same_goal.skipped == 1 and same_goal.goal_kept == 0
    assert len(item.evidence) == 2


def test_import_collision_rolls_back_entire_batch(board):
    fresh = _review_spec(run_id="fresh")
    collision = _review_spec(run_id="collision")
    board.add("unrelated", item_id=collision.item_id)

    with pytest.raises(BoardError, match="provenance collision"):
        board.import_review_candidates([fresh, collision], goal="tracegraph-review")

    assert board.get(fresh.item_id) is None
    assert board.get(collision.item_id).task == "unrelated"


def test_two_board_handles_import_exact_candidate_once(tmp_path, clock):
    db = str(tmp_path / "review.db")
    first = Board(db, now=clock)
    second = Board(db, now=clock)
    spec = _review_spec()
    try:
        one = first.import_review_candidates([spec], goal="tracegraph-review")
        two = second.import_review_candidates([spec], goal="tracegraph-review")
        assert (one.imported, one.skipped) == (1, 0)
        assert (two.imported, two.skipped) == (0, 1)
        assert len(first.list()) == 1
    finally:
        first.close()
        second.close()


def test_list_filters_by_state_and_goal(board):
    a = board.add("A", goal="g1")
    board.add("B", goal="g2")
    board.claim(a.id, "r1")  # A → running
    assert [i.task for i in board.list()] == ["A", "B"]
    assert [i.task for i in board.list(state="running")] == ["A"]
    assert [i.task for i in board.list(state="planned")] == ["B"]
    assert [i.task for i in board.list(goal="g2")] == ["B"]


def test_list_rejects_unknown_state(board):
    with pytest.raises(BoardError, match="unknown state"):
        board.list(state="bogus")


# ── atomic claim / next_runnable dedup (BOTH paths) ──────────────────────────


def test_claim_is_one_shot_cas(board):
    it = board.add("x")
    first = board.claim(it.id, "run-1")
    assert first is not None and first.state is WorkState.RUNNING
    assert first.lease_run_id == "run-1"
    # second claim of an already-running item → None (CAS rejected)
    assert board.claim(it.id, "run-2") is None


def test_claim_missing_item_returns_none(board):
    assert board.claim("ghost", "run-1") is None
    assert board._conn.execute("SELECT COUNT(*) FROM board_run_namespaces").fetchone()[0] == 0


def test_both_claim_paths_atomically_register_scratch_ownership(board):
    a = board.add("A")
    b = board.add("B")
    assert board.claim(a.id, "run-a") is not None
    assert board.next_runnable("run-b") is not None
    rows = board._conn.execute(
        "SELECT run_id, item_id, purged_at FROM board_run_namespaces ORDER BY claimed_at"
    ).fetchall()
    assert [(r["run_id"], r["item_id"], r["purged_at"]) for r in rows] == [
        ("run-a", a.id, None),
        ("run-b", b.id, None),
    ]


def test_duplicate_run_id_rolls_back_second_claim_and_audit(board):
    a = board.add("A")
    b = board.add("B")
    assert board.claim(a.id, "same-run") is not None
    with pytest.raises(BoardError, match="claim failed"):
        board.claim(b.id, "same-run")
    assert board.get(b.id).state is WorkState.PLANNED
    assert not any(t["to_state"] == "running" for t in board.transitions(b.id))


def test_scratch_ledger_insert_failure_rolls_back_claim_and_audit(board):
    item = board.add("A")
    board._conn.execute(
        "CREATE TRIGGER fail_scratch_insert BEFORE INSERT ON board_run_namespaces "
        "BEGIN SELECT RAISE(ABORT, 'scratch insert failed'); END"
    )
    with pytest.raises(BoardError, match="claim failed"):
        board.claim(item.id, "run-a")
    assert board.get(item.id).state is WorkState.PLANNED
    assert not any(t["to_state"] == "running" for t in board.transitions(item.id))


def test_two_boards_one_db_only_one_claims_by_id(tmp_path, clock):
    """Two Board handles on one db: exactly one wins the claim (atomic CAS)."""
    db = str(tmp_path / "shared.db")
    a = Board(db, now=clock)
    b = Board(db, now=clock)
    try:
        it = a.add("x")
        r1 = a.claim(it.id, "A")
        r2 = b.claim(it.id, "B")
        assert (r1 is not None) ^ (r2 is not None)  # exactly one
    finally:
        a.close()
        b.close()


def test_two_boards_one_db_only_one_next_runnable(tmp_path, clock):
    db = str(tmp_path / "shared2.db")
    a = Board(db, now=clock)
    b = Board(db, now=clock)
    try:
        a.add("only")
        r1 = a.next_runnable("A")
        r2 = b.next_runnable("B")
        assert (r1 is not None) ^ (r2 is not None)
    finally:
        a.close()
        b.close()


# ── dependency gating + human_required skip ──────────────────────────────────


def test_next_runnable_respects_dependencies(board):
    a = board.add("A")
    b = board.add("B", depends_on=[a.id])
    # B blocked on A → next is A
    assert board.next_runnable("r1").id == a.id
    # nothing else runnable while A is running and B depends on it
    assert board.next_runnable("r2") is None
    # finish A (done via sign-off) → B becomes runnable
    board.transition(
        a.id, "done", actor="human",
        evidence=[Evidence("human", "ok", True, 0.0, "me")],
    )
    assert board.next_runnable("r3").id == b.id


def test_next_runnable_missing_dependency_never_dispatches(board):
    board.add("needs-ghost", depends_on=["does-not-exist"])
    assert board.next_runnable("r1") is None  # a missing dep is unsatisfied


def test_next_runnable_skips_human_required(board):
    board.add("manual", human_required=True)
    b = board.add("auto")
    assert board.next_runnable("r1").id == b.id  # skipped the human-required one


def test_next_runnable_empty_board_returns_none(board):
    assert board.next_runnable("r1") is None


# ── goal-scoped claim (board run --goal, Phase 4) ─────────────────────────────


def test_next_runnable_goal_filter_skips_earlier_nonmatching(board):
    other = board.add("other work", goal="frontend")  # earlier in FIFO
    target = board.add("auth work", goal="auth")
    got = board.next_runnable("r1", goal="auth")
    assert got.id == target.id
    # the skipped non-matching item is untouched, still planned
    assert board.get(other.id).state is WorkState.PLANNED


def test_next_runnable_goal_filter_no_match_returns_none(board):
    it = board.add("x", goal="frontend")
    assert board.next_runnable("r1", goal="auth") is None
    assert board.get(it.id).state is WorkState.PLANNED  # nothing claimed


def test_next_runnable_goal_none_regression(board):
    # goal=None (the default) considers every item — exactly the old behavior
    a = board.add("A", goal="frontend")
    assert board.next_runnable("r1").id == a.id


def test_two_boards_one_db_only_one_next_runnable_goal_scoped(tmp_path, clock):
    # the atomic-dedup guarantee holds under the goal filter too
    db = str(tmp_path / "shared3.db")
    a = Board(db, now=clock)
    b = Board(db, now=clock)
    try:
        a.add("only", goal="auth")
        r1 = a.next_runnable("A", goal="auth")
        r2 = b.next_runnable("B", goal="auth")
        assert (r1 is not None) ^ (r2 is not None)
    finally:
        a.close()
        b.close()


def test_next_runnable_goal_filter_still_gates_deps_and_human(board):
    dep = board.add("dep", goal="auth")
    board.add("blocked", goal="auth", depends_on=[dep.id])
    board.add("manual", goal="auth", human_required=True)
    # dep itself is the only dispatchable auth item
    assert board.next_runnable("r1", goal="auth").id == dep.id
    # blocked (dep running) + human_required are both excluded
    assert board.next_runnable("r2", goal="auth") is None


# ── stale-claim recovery ─────────────────────────────────────────────────────


def test_recover_stale_running_reclaims_past_lease(board, clock):
    it = board.add("x")
    board.claim(it.id, "run-1")  # claimed at t=1000, lease=100
    clock.t = 1000 + 100 + 1  # past the lease
    assert board.recover_stale_running() == 1
    recovered = board.get(it.id)
    assert recovered.state is WorkState.PLANNED
    assert recovered.lease_run_id is None and recovered.claimed_at is None
    # an audit row records the recovery
    assert any(t["note"] == "stale-claim recovery" for t in board.transitions(it.id))


def test_recover_stale_running_leaves_fresh_claims(board, clock):
    it = board.add("x")
    board.claim(it.id, "run-1")
    clock.t = 1000 + 50  # still within the lease
    assert board.recover_stale_running() == 0
    assert board.get(it.id).state is WorkState.RUNNING


def test_recover_respects_explicit_lease_override(board, clock):
    it = board.add("x")
    board.claim(it.id, "run-1")
    clock.t = 1000 + 10
    assert board.recover_stale_running(lease_timeout=5) == 1  # tighter lease → stale


# ── board-owned scratch namespace ledger (memory-backend P2) ────────────────


def test_scratch_ledger_protects_live_and_collects_resolved(board):
    a = board.add("A")
    board.add("B")  # planned, never claimed — no ledger row
    board.claim(a.id, "r1")
    assert board.collectible_board_run_ids() == []

    board.transition(a.id, "review", actor="human")
    assert board.collectible_board_run_ids() == ["r1"]
    assert board.mark_board_run_namespaces_purged(["r1"]) == 1
    assert board.collectible_board_run_ids() == []
    assert board.reactivate_board_run_namespace("r1") is True
    assert board.collectible_board_run_ids() == ["r1"]


def test_scratch_ledger_stale_recovery_grants_a_grace_period(board, clock):
    # A reclaim means "presumed dead", not "known dead". Collecting the scratch
    # namespace right away would delete it out from under a run that is merely
    # slow — and that run is still using it for its stage-to-stage handoff.
    item = board.add("A")
    board.claim(item.id, "r1")
    clock.t += 101
    board.recover_stale_running()
    assert board.collectible_board_run_ids() == []

    clock.t += 101  # one further lease timeout: the reclaim itself has aged out
    assert board.collectible_board_run_ids() == ["r1"]


def test_scratch_ledger_normal_resolution_collects_immediately(board):
    # No reclaim involved: the run finished and moved the item on, so there is
    # no live writer to protect and the grace period does not apply.
    item = board.add("A")
    board.claim(item.id, "r1")
    board.transition(item.id, "review", actor="human")
    assert board.collectible_board_run_ids() == ["r1"]


def test_scratch_ledger_reclaimed_column_is_added_to_an_existing_board(tmp_path):
    # CREATE TABLE IF NOT EXISTS is a no-op on a board that already exists, so
    # the column needs an explicit migration on open.
    import sqlite3

    db = tmp_path / "board.db"
    Board(db).close()
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE board_run_namespaces DROP COLUMN reclaimed_at")
    raw.commit()
    raw.close()

    board = Board(db)
    try:
        columns = {
            r[1] for r in board._conn.execute("PRAGMA table_info(board_run_namespaces)")
        }
        assert "reclaimed_at" in columns
    finally:
        board.close()


def test_concurrent_first_open_of_an_old_board_does_not_fail(tmp_path):
    # The column check and the ALTER are two statements, so several processes
    # opening an old board at once all see it missing and all try to add it. The
    # losers must not get "duplicate column name" as a fatal BoardError.
    import sqlite3
    import threading

    db = tmp_path / "board.db"
    Board(db).close()
    raw = sqlite3.connect(db)
    raw.execute("ALTER TABLE board_run_namespaces DROP COLUMN reclaimed_at")
    raw.commit()
    raw.close()

    errors: list[Exception] = []

    def _open():
        try:
            Board(db).close()
        except Exception as exc:  # noqa: BLE001 - the assertion is "none at all"
            errors.append(exc)

    threads = [threading.Thread(target=_open) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_scratch_ledger_candidate_limit_and_idempotent_mark(board):
    for i in range(3):
        item = board.add(str(i))
        board.claim(item.id, f"r{i}")
        board.transition(item.id, "review", actor="human")
    assert board.collectible_board_run_ids(limit=2) == ["r0", "r1"]
    assert board.mark_board_run_namespaces_purged(["r0", "r0", "missing"]) == 1
    assert board.mark_board_run_namespaces_purged(["r0"]) == 0


def test_scratch_ledger_reads_raise_boarderror(board):
    board.close()
    with pytest.raises(BoardError, match="collectible_board_run_ids failed"):
        board.collectible_board_run_ids()


# ── done-gate (the load-bearing review-vs-done distinction) ──────────────────


def test_done_gate_rejects_bare_done(board):
    it = board.add("x")
    board.transition(it.id, "review", actor="human")
    with pytest.raises(BoardError, match="without verification"):
        board.transition(it.id, "done", actor="human")
    assert board.get(it.id).state is WorkState.REVIEW  # unchanged after rejection


def test_done_gate_accepts_human_signoff(board):
    it = board.add("x")
    done = board.transition(
        it.id, "done", actor="human",
        evidence=[Evidence("human", "sign-off", True, 0.0, "me")],
    )
    assert done.state is WorkState.DONE


def test_done_gate_accepts_passing_evidence(board):
    it = board.add("x")
    done = board.transition(
        it.id, "done", actor="human",
        evidence=[Evidence("test", "ci#42", True, 0.0, "ci")],
    )
    assert done.state is WorkState.DONE


def test_done_gate_uses_pre_existing_evidence(board):
    # evidence attached earlier (e.g. a passing gate from a run) lets a later bare
    # `--state done` succeed without re-supplying it.
    it = board.add("x")
    board.transition(
        it.id, "review", actor="claude",
        evidence=[Evidence("gate", "1/1 passed", True, 0.0, "claude")],
    )
    done = board.transition(it.id, "done", actor="human")  # no new evidence needed
    assert done.state is WorkState.DONE


def test_failing_evidence_does_not_satisfy_done_gate(board):
    it = board.add("x")
    with pytest.raises(BoardError, match="without verification"):
        board.transition(
            it.id, "done", actor="human",
            evidence=[Evidence("gate", "0/1 passed", False, 0.0, "claude")],
        )


def test_transition_rejects_unknown_state(board):
    it = board.add("x")
    with pytest.raises(BoardError, match="unknown state"):
        board.transition(it.id, "bogus", actor="human")


def test_transition_clear_human_required(board):
    it = board.add("x", human_required=True)
    updated = board.transition(
        it.id, it.state.value, actor="human", human_required=False
    )
    assert updated.human_required is False


# ── apply_update: merge + gate→evidence + advisory state ─────────────────────


def test_apply_update_completed_defaults_to_review(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    updated, _ = board.apply_update(it.id, {}, _result(output="done it"), actor="claude")
    assert updated.state is WorkState.REVIEW
    assert updated.owner_agent == "claude"  # record-only owner stamped
    assert updated.lease_run_id is None  # lease cleared on leaving running


def test_apply_update_maps_passing_gate_to_evidence(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    gate = GateReport(passed=True, score=1.0, checks=[GateCheck("pytest", True, 0)])
    updated, _ = board.apply_update(it.id, {}, _result(gate=gate), actor="claude")
    assert any(e.kind == "gate" and e.passed for e in updated.evidence)


def test_apply_update_agent_done_with_gate_promotes(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    gate = GateReport(passed=True, score=1.0, checks=[GateCheck("pytest", True, 0)])
    parsed = {"state": "done"}
    updated, _ = board.apply_update(it.id, parsed, _result(gate=gate), actor="claude")
    assert updated.state is WorkState.DONE  # verified → honored


def test_apply_update_agent_done_without_evidence_clamps_to_review(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    updated, _ = board.apply_update(it.id, {"state": "done"}, _result(), actor="claude")
    assert updated.state is WorkState.REVIEW  # advisory done clamped, NEVER raised
    assert any("clamped" in t["note"] for t in board.transitions(it.id))


def test_apply_update_agent_blocked_request(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    parsed = {"state": "blocked", "blocked_reason": "needs creds"}
    updated, _ = board.apply_update(it.id, parsed, _result(), actor="claude")
    assert updated.state is WorkState.BLOCKED
    assert updated.blocked_reason == "needs creds"
    assert updated.human_required is True


def test_apply_update_failed_result_blocks(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    updated, _ = board.apply_update(
        it.id, {}, _result(status=TaskStatus.FAILED, error="boom"), actor="codex"
    )
    assert updated.state is WorkState.BLOCKED
    assert updated.blocked_reason == "boom"
    assert updated.human_required is True


def test_apply_update_merges_outputs(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    parsed = {"outputs": ["doc.md"], "handoff": "wire it up"}
    updated, _ = board.apply_update(
        it.id, parsed, _result(files=["a.py", "doc.md"]), actor="claude"
    )
    assert updated.outputs == ["doc.md", "a.py"]  # parsed first, files unique-merged
    assert updated.handoff == "wire it up"


def test_apply_update_missing_item_raises(board):
    with pytest.raises(BoardError, match="no such item"):
        board.apply_update("ghost", {}, _result(), actor="claude")


# ── parse_board_update (own parser; degrade-to-{}) ───────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("no block here", {}),
        ("", {}),
        ("```syncmill-board\n{\"state\": \"review\"}\n```", {"state": "review"}),
        (
            "text\n```syncmill-board\n{\"state\": \"done\", \"handoff\": \"h\"}\n```\nmore",
            {"state": "done", "handoff": "h"},
        ),
        # outputs all-or-nothing: clean list kept
        ("```syncmill-board\n{\"outputs\": [\"a\", \"b\"]}\n```", {"outputs": ["a", "b"]}),
        # mixed outputs list dropped (not salvaged), other fields kept
        (
            "```syncmill-board\n{\"state\": \"review\", \"outputs\": [\"a\", 3]}\n```",
            {"state": "review"},
        ),
        # malformed JSON → {}
        ("```syncmill-board\nnot json\n```", {}),
        # non-object JSON → {}
        ("```syncmill-board\n[1, 2]\n```", {}),
        # no closing fence → {}
        ("```syncmill-board\n{\"state\": \"review\"}", {}),
        # unknown fields dropped, known kept
        (
            "```syncmill-board\n{\"state\": \"review\", \"nope\": 1, \"human_required\": true}\n```",
            {"state": "review", "human_required": True},
        ),
        # a plain ``` code block is NOT a board block
        ("```\n{\"state\": \"review\"}\n```", {}),
    ],
)
def test_parse_board_update_table(raw, expected):
    assert parse_board_update(raw) == expected


def test_parse_board_update_never_raises_on_garbage():
    # fuzzy-ish: a pile of fences/braces must degrade, never raise
    for raw in ["```syncmill-board", "```syncmill-board\n```", "{}{}{}", "```syncmill-board\n{"]:
        assert parse_board_update(raw) == {}


# ── strip_board_block ────────────────────────────────────────────────────────


def test_strip_board_block_removes_only_the_board_block():
    raw = "hello\n```syncmill-board\n{\"state\": \"review\"}\n```\nworld"
    assert strip_board_block(raw) == "hello\nworld"


def test_strip_board_block_keeps_other_code_blocks():
    raw = "see:\n```python\nprint(1)\n```\nend"
    assert strip_board_block(raw) == raw.strip()


def test_strip_board_block_no_block_is_identity():
    assert strip_board_block("just text") == "just text"


def test_strip_board_block_empty():
    assert strip_board_block("") == ""


# ── render helpers ───────────────────────────────────────────────────────────


def test_render_item_prompt_includes_task_and_update_convention(board):
    it = board.add("build the parser", goal="parsing")
    prompt = render_item_prompt(it)
    assert "Task: build the parser" in prompt
    assert "parsing" in prompt
    assert "syncmill-board" in prompt  # the update-block convention is advertised


def test_render_item_prompt_injects_decisions_and_handoff():
    it = WorkItem(id="x", goal="g", task="t", handoff="prior note")
    prompt = render_item_prompt(it, active_decisions=["never use eval"])
    assert "never use eval" in prompt
    assert "prior note" in prompt


def test_render_board_and_item_smoke(board):
    assert "empty" in render_board([])
    it = board.add("task one", goal="g")
    assert it.id in render_board([it])
    assert "task one" in render_item(it)


# ── build_board / corrupt db ─────────────────────────────────────────────────


def test_build_board_disabled_is_none():
    assert build_board(BoardConfig(enabled=False)) is None


def test_build_board_enabled(tmp_path):
    cfg = BoardConfig(enabled=True, db_path=str(tmp_path / "b.db"))
    bd = build_board(cfg)
    assert bd is not None
    bd.close()


def test_corrupt_db_raises_board_error(tmp_path):
    bad = tmp_path / "corrupt.db"
    bad.write_text("this is definitely not a sqlite database " * 50)
    with pytest.raises(BoardError, match="failed to open board"):
        Board(str(bad))


def test_unopenable_db_path_raises_board_error(tmp_path):
    # an unwritable parent (a FILE in the path) makes mkdir/connect raise OSError —
    # which must surface as BoardError, not an uncaught traceback.
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir")
    with pytest.raises(BoardError, match="failed to open board"):
        Board(str(blocker / "nested" / "board.db"))


@pytest.mark.skipif(os.name != "posix", reason="exact private modes are POSIX-only")
def test_new_board_storage_is_private_with_permissive_umask(tmp_path):
    parent = tmp_path / "private-board"
    path = parent / "board.db"
    previous = os.umask(0o022)
    try:
        board = Board(str(path))
        board.add("private task")
        assert parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        assert Path(f"{path}-wal").stat().st_mode & 0o777 == 0o600
        assert Path(f"{path}-shm").stat().st_mode & 0o777 == 0o600
        board.close()
    finally:
        os.umask(previous)


@pytest.mark.skipif(os.name != "posix", reason="exact private modes are POSIX-only")
def test_existing_board_and_sidecars_are_tightened_without_data_loss(tmp_path):
    path = tmp_path / "legacy.db"
    first = Board(str(path))
    item = first.add("keep me")
    live_files = [path, Path(f"{path}-wal"), Path(f"{path}-shm")]
    for file_path in live_files:
        os.chmod(file_path, 0o644)
    second = Board(str(path))
    try:
        assert second.get(item.id).task == "keep me"
        assert all(file_path.stat().st_mode & 0o777 == 0o600 for file_path in live_files)
    finally:
        second.close()
        first.close()
    reopened = Board(str(path))
    try:
        assert reopened.get(item.id).task == "keep me"
        assert path.stat().st_mode & 0o777 == 0o600
    finally:
        reopened.close()


@pytest.mark.skipif(os.name != "posix", reason="fchmod hardening is POSIX-only")
def test_board_permission_hardening_failure_is_board_error(tmp_path, monkeypatch):
    def fail_fchmod(fd, mode):
        raise PermissionError("hardening denied")

    monkeypatch.setattr("syncmill.board.os.fchmod", fail_fchmod)
    with pytest.raises(BoardError, match="failed to open board"):
        Board(str(tmp_path / "private.db"))


def test_unresolvable_home_raises_board_error(tmp_path, monkeypatch):
    # expanduser() raises RuntimeError (NOT an OSError) when ~ can't resolve because no
    # home dir is set (cron/systemd). Board.__init__'s contract is BoardError-ONLY, so
    # `serve` (which catches just BoardError around build_board, then closes memory in a
    # separate finally) never crashes + leaks the memory close on this path.
    # MUTATION GUARD: moving expanduser() back OUTSIDE the try, or dropping RuntimeError
    # from the (sqlite3.Error, OSError, RuntimeError) catch, lets the RuntimeError escape
    # UN-wrapped (not a BoardError) → this test fails.
    import pathlib

    def _boom(self):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(pathlib.Path, "expanduser", _boom)
    with pytest.raises(BoardError, match="failed to open board"):
        Board(str(tmp_path / "board.db"))


# ── value-type round-trips ───────────────────────────────────────────────────


def test_evidence_round_trips_through_sqlite(board):
    it = board.add("x")
    ev = Evidence(kind="gate", ref="3/3 passed", passed=True, at=12.5, by="claude")
    board.transition(it.id, "review", actor="claude", evidence=[ev])
    reread = board.get(it.id).evidence
    assert len(reread) == 1
    assert reread[0] == ev


def test_inputs_depends_on_round_trip(board):
    a = board.add("dep")
    it = board.add("x", depends_on=[a.id], inputs=["spec.md"])
    got = board.get(it.id)
    assert got.depends_on == [a.id]
    assert got.inputs == ["spec.md"]


# ── lease handling (adversarial-review fixes) ────────────────────────────────


def test_transition_annotate_running_preserves_lease(board, clock):
    # FIX: a same-state annotate of a RUNNING item must NOT clear the lease, or
    # recover_stale_running would treat the live item as abandoned and re-dispatch it.
    it = board.add("x")
    board.claim(it.id, "run-1")
    # annotate-only (target == current state == running), e.g. attach evidence
    board.transition(
        it.id, "running", actor="human", note="fyi",
        evidence=[Evidence("manual", "note", True, 0.0, "human")],
    )
    still = board.get(it.id)
    assert still.state is WorkState.RUNNING
    assert still.lease_run_id == "run-1" and still.claimed_at is not None
    # within the lease, recovery must NOT reclaim it
    clock.t = 1000 + 50
    assert board.recover_stale_running() == 0
    assert board.get(it.id).state is WorkState.RUNNING


def test_apply_update_lease_fence_drops_stale_write(board, clock):
    # FIX: a slow run whose item was reclaimed + re-dispatched must not clobber the
    # new owner. claim(A) → past lease → recover → next_runnable(B) → B records review
    # → A's stale apply_update is dropped (B's state preserved).
    it = board.add("x")
    board.claim(it.id, "RUN-A")
    clock.t = 1000 + 100 + 1
    assert board.recover_stale_running() == 1  # A's lease reclaimed → planned
    b = board.next_runnable("RUN-B")           # B re-claims → running, lease=RUN-B
    assert b.id == it.id and b.lease_run_id == "RUN-B"
    _, b_applied = board.apply_update(
        it.id, {}, _result(agent_id="bee"), actor="bee", run_id="RUN-B"
    )
    assert b_applied is True  # the live owner's write took effect
    assert board.get(it.id).state is WorkState.REVIEW and board.get(it.id).owner_agent == "bee"
    # now A's stale write-back arrives with the OLD lease — it must be a no-op, and
    # it must report `applied=False` so run_item suppresses the global-shared share.
    _, a_applied = board.apply_update(
        it.id, {}, _result(agent_id="ayy"), actor="ayy", run_id="RUN-A"
    )
    assert a_applied is False  # the stale write was dropped — signal exposed to caller
    after = board.get(it.id)
    assert after.state is WorkState.REVIEW and after.owner_agent == "bee"  # not clobbered


def test_apply_update_lease_fence_cannot_revert_verified_done(board, clock):
    # the worst case: a stale run reverting a human-verified `done` back to review.
    it = board.add("x")
    board.claim(it.id, "RUN-A")
    clock.t = 1000 + 200
    board.recover_stale_running()
    board.next_runnable("RUN-B")
    board.apply_update(it.id, {}, _result(agent_id="bee"), actor="bee", run_id="RUN-B")
    board.transition(it.id, "done", actor="human",
                     evidence=[Evidence("human", "ok", True, 0.0, "me")])
    assert board.get(it.id).state is WorkState.DONE
    # A's stale completion must NOT revert the verified done
    board.apply_update(it.id, {}, _result(agent_id="ayy"), actor="ayy", run_id="RUN-A")
    assert board.get(it.id).state is WorkState.DONE


def test_apply_update_unfenced_without_run_id_still_writes(board):
    # back-compat: with no run_id the write is unfenced (the in-process single-owner
    # path), so the existing claim+apply_update flow is unchanged.
    it = board.add("x")
    board.claim(it.id, "r1")
    updated, _ = board.apply_update(it.id, {}, _result(), actor="claude")  # no run_id
    assert updated.state is WorkState.REVIEW


# ── done-gate: human rejection ───────────────────────────────────────────────


def test_human_rejection_signoff_does_not_satisfy_done_gate(board):
    # FIX: a RECORDED human rejection (kind=human, passed=False) must NOT pass the gate.
    it = board.add("x")
    with pytest.raises(BoardError, match="without verification"):
        board.transition(
            it.id, "done", actor="human",
            evidence=[Evidence("human", "rejected", False, 0.0, "me")],
        )


# ── blocked_reason cleared on re-run to review ───────────────────────────────


def test_apply_update_clears_stale_blocked_reason_on_rerun(board):
    # FIX: an item that was blocked, re-planned, and re-run to success must not carry
    # the stale blocked_reason into review.
    it = board.add("x")
    board.claim(it.id, "r1")
    board.apply_update(it.id, {}, _result(status=TaskStatus.FAILED, error="needs creds"), actor="c")
    assert board.get(it.id).blocked_reason == "needs creds"
    board.transition(it.id, "planned", actor="human", human_required=False)
    board.claim(it.id, "r2")
    updated, _ = board.apply_update(it.id, {}, _result(), actor="claude")  # succeeds
    assert updated.state is WorkState.REVIEW
    assert updated.blocked_reason == ""  # stale reason cleared


# ── strip_board_block edge cases ─────────────────────────────────────────────


def test_strip_board_block_double_open_fence_no_leak():
    # FIX: two adjacent board blocks (first not separately closed) must BOTH be
    # removed — the second's open fence must not be mis-consumed as the first's close.
    raw = (
        "```syncmill-board\n{\"state\": \"done\"}\n"
        "```syncmill-board\n{\"state\": \"review\"}\n```"
    )
    out = strip_board_block(raw)
    assert "syncmill-board" not in out and "{" not in out  # no board JSON leaks


def test_strip_board_block_unterminated_removes_to_eof():
    raw = "real result\n```syncmill-board\n{\"state\": \"review\"}"
    assert strip_board_block(raw) == "real result"


# ── dependency gating: review != done ────────────────────────────────────────


def test_next_runnable_dep_in_review_does_not_satisfy(board):
    # the review-vs-done distinction at the dependency level: a dep in `review`
    # (finished but unverified) must NOT unblock a dependent — only `done` does.
    a = board.add("A")
    b = board.add("B", depends_on=[a.id])
    board.claim(a.id, "r1")
    board.apply_update(a.id, {}, _result(), actor="c")  # A → review (unverified)
    assert board.get(a.id).state is WorkState.REVIEW
    assert board.next_runnable("r2") is None  # B still blocked: dep is review, not done
    board.transition(a.id, "done", actor="human",
                     evidence=[Evidence("human", "ok", True, 0.0, "me")])
    assert board.next_runnable("r3").id == b.id  # now A is done → B dispatchable


# ── advisory human_required on a completed→review item ───────────────────────


def test_apply_update_agent_human_required_on_review(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    updated, _ = board.apply_update(it.id, {"human_required": True}, _result(), actor="c")
    assert updated.state is WorkState.REVIEW and updated.human_required is True
    assert board.next_runnable("r2") is None  # human-required items skip auto-dispatch


# ── corrupt-state row read path ──────────────────────────────────────────────


def test_get_on_corrupt_state_row_raises_board_error(tmp_path):
    # a hand-edited/corrupt out-of-enum `state` must surface as BoardError (the
    # contract), not a raw ValueError traceback. Inject one past the CHECK constraint.
    import sqlite3
    db = str(tmp_path / "b.db")
    bd = Board(db)
    it = bd.add("x")
    bd.close()
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA ignore_check_constraints=ON")
    raw.execute("UPDATE work_items SET state='bogus' WHERE id=?", (it.id,))
    raw.commit()
    raw.close()
    bd2 = Board(db)
    try:
        with pytest.raises(BoardError):
            bd2.get(it.id)
        with pytest.raises(BoardError):
            bd2.list()
    finally:
        bd2.close()


# ── crash-block lease fence (W1) ─────────────────────────────────────────────


def test_transition_lease_fence_drops_stale_block(board, clock):
    # FIX: the crash-block path is lease-fenced — a stale crashed run must NOT block
    # an item that was reclaimed + re-dispatched to a new owner.
    it = board.add("x")
    board.claim(it.id, "RUN-A")
    clock.t = 1000 + 100 + 1
    assert board.recover_stale_running() == 1
    b = board.next_runnable("RUN-B")
    assert b.lease_run_id == "RUN-B" and b.state is WorkState.RUNNING
    n_before = len(board.transitions(it.id))
    # A crashes and tries to block the item with its OLD lease → dropped (no-op)
    board.transition(it.id, "blocked", actor="system", human_required=True, run_id="RUN-A")
    after = board.get(it.id)
    assert after.state is WorkState.RUNNING and after.lease_run_id == "RUN-B"
    assert len(board.transitions(it.id)) == n_before  # dropped write logged nothing


def test_transition_crash_block_cannot_revert_verified_done(board, clock):
    # the worst case: a stale crashed run reverting a human-verified `done`.
    it = board.add("x")
    board.claim(it.id, "RUN-A")
    clock.t = 1000 + 200
    board.recover_stale_running()
    board.next_runnable("RUN-B")
    board.apply_update(it.id, {}, _result(agent_id="bee"), actor="bee", run_id="RUN-B")
    board.transition(it.id, "done", actor="human",
                     evidence=[Evidence("human", "ok", True, 0.0, "me")])
    assert board.get(it.id).state is WorkState.DONE
    board.transition(it.id, "blocked", actor="system", human_required=True, run_id="RUN-A")
    assert board.get(it.id).state is WorkState.DONE  # stale crash did NOT revert it


def test_transition_block_without_run_id_is_unfenced(board):
    # back-compat: a human transition (no run_id) of a running item is unfenced.
    it = board.add("x")
    board.claim(it.id, "r1")
    blocked = board.transition(it.id, "blocked", actor="human")
    assert blocked.state is WorkState.BLOCKED


# ── manual move into running is rejected (W3) ────────────────────────────────


def test_transition_rejects_manual_move_to_running(board):
    # FIX: `running` is owned by the claim lease — a manual move INTO it would mint a
    # lease-less, undispatchable item.
    it = board.add("x")
    with pytest.raises(BoardError, match="claimed by"):
        board.transition(it.id, "running", actor="human")
    assert board.get(it.id).state is WorkState.PLANNED  # unchanged


# ── annotate-only (to_state=None) resolved in-txn (W5) ───────────────────────


def test_transition_none_state_annotates_current(board):
    # FIX: to_state=None keeps the CURRENT state (resolved INSIDE the txn, not from a
    # stale pre-read), attaching evidence without a state change.
    it = board.add("x")
    board.transition(it.id, "review", actor="claude")
    updated = board.transition(
        it.id, None, actor="human",
        evidence=[Evidence("manual", "note", True, 0.0, "me")],
    )
    assert updated.state is WorkState.REVIEW
    assert any(e.ref == "note" for e in updated.evidence)


# ── transition clears stale blocked_reason leaving blocked (W6) ──────────────


def test_transition_clears_blocked_reason_leaving_blocked(board):
    # FIX: moving OUT of blocked via the human path clears the stale reason (mirrors
    # apply_update), so `board show` doesn't render a stale `blocked:` line.
    it = board.add("x")
    board.claim(it.id, "r1")
    board.apply_update(it.id, {}, _result(status=TaskStatus.FAILED, error="needs creds"), actor="c")
    assert board.get(it.id).blocked_reason == "needs creds"
    moved = board.transition(it.id, "planned", actor="human", human_required=False)
    assert moved.blocked_reason == ""


# ── transactional rollback on non-sqlite error (W4 / C5) ─────────────────────


def test_scratch_ledger_upgrade_backfills_history_and_is_idempotent(tmp_path, clock):
    import sqlite3

    db = str(tmp_path / "legacy.db")
    legacy = Board(db, now=clock, lease_timeout_seconds=100)
    done = legacy.add("done")
    done_too = legacy.add("done too")
    live = legacy.add("live")
    legacy.claim(done.id, "old-done")
    legacy.transition(done.id, "review", actor="human")
    legacy.claim(done_too.id, "old-done-too")
    legacy.transition(done_too.id, "review", actor="human")
    legacy.claim(live.id, "old-live")
    legacy.close()

    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE board_run_namespaces")
    raw.commit()
    raw.close()

    upgraded = Board(db, now=clock, lease_timeout_seconds=100)
    assert upgraded.collectible_board_run_ids() == ["old-done", "old-done-too"]
    assert upgraded.mark_board_run_namespaces_purged(["old-done", "old-done-too"]) == 2
    upgraded.close()

    reopened = Board(db, now=clock, lease_timeout_seconds=100)
    try:
        assert reopened.collectible_board_run_ids() == []
        rows = reopened._conn.execute(
            "SELECT run_id, purged_at FROM board_run_namespaces ORDER BY claimed_at"
        ).fetchall()
        assert [r["run_id"] for r in rows] == ["old-done", "old-done-too", "old-live"]
        assert rows[0]["purged_at"] is not None
        assert rows[1]["purged_at"] is not None
        assert rows[2]["purged_at"] is None  # live lease remains protected
    finally:
        reopened.close()


def test_next_runnable_corrupt_depends_on_raises_board_error_no_dangling_txn(tmp_path):
    # FIX: a corrupt depends_on JSON surfaces as BoardError (like get/list) AND rolls
    # back the BEGIN IMMEDIATE — else the lock leaks and the next op fails with
    # "cannot start a transaction within a transaction".
    import sqlite3
    db = str(tmp_path / "b.db")
    bd = Board(db)
    it = bd.add("x")
    bd.close()
    raw = sqlite3.connect(db)
    raw.execute("UPDATE work_items SET depends_on='not json' WHERE id=?", (it.id,))
    raw.commit()
    raw.close()
    bd2 = Board(db)
    try:
        with pytest.raises(BoardError):
            bd2.next_runnable("r1")
        # rolled back cleanly → a fresh BEGIN IMMEDIATE op works (no dangling txn)
        assert bd2.recover_stale_running() == 0
    finally:
        bd2.close()


# ── __init__ closes the connection on open failure (W7) ──────────────────────


def test_board_init_closes_connection_on_open_failure(tmp_path, monkeypatch):
    # FIX: when a PRAGMA/schema step fails after connect() succeeds, the partial
    # connection must be closed, not leaked.
    import syncmill.board as board_mod

    closed: list[bool] = []

    class _BoomConn:
        row_factory = None

        def execute(self, sql, *a):
            if "journal_mode" in sql:
                raise board_mod.sqlite3.OperationalError("file is not a database")
            return None

        def executescript(self, sql):
            return None

        def close(self):
            closed.append(True)

    monkeypatch.setattr(board_mod.sqlite3, "connect", lambda *a, **k: _BoomConn())
    with pytest.raises(BoardError, match="failed to open board"):
        Board(str(tmp_path / "b.db"))
    assert closed == [True]  # the partial connection was closed, not leaked


# ── agent `done` needs CURRENT-run verification (W8) ─────────────────────────


def test_apply_update_agent_done_with_stale_evidence_clamps_to_review(board):
    # FIX: an agent's advisory `done` must be verified by THIS run — stale passing
    # evidence from an earlier (now-discarded) run must NOT certify the current work.
    it = board.add("x")
    board.claim(it.id, "r1")
    gate = GateReport(passed=True, score=1.0, checks=[GateCheck("pytest", True, 0)])
    board.apply_update(it.id, {}, _result(gate=gate), actor="claude")  # run 1: gate passed
    assert any(e.kind == "gate" and e.passed for e in board.get(it.id).evidence)
    board.transition(it.id, "planned", actor="human")  # human sends it back for rework
    board.claim(it.id, "r2")
    # run 2: NO gate, agent self-reports done → must clamp to review (stale gate ≠ now)
    updated, _ = board.apply_update(it.id, {"state": "done"}, _result(), actor="claude")
    assert updated.state is WorkState.REVIEW


# ── parser robustness (W2 RecursionError, C4 double-open) ────────────────────


def test_parse_board_update_deeply_nested_does_not_raise():
    # FIX: deeply-nested JSON makes json.loads raise RecursionError (NOT a ValueError),
    # which must still degrade to {} — the parser's "NEVER raises" contract.
    raw = "```syncmill-board\n" + "[" * 50000 + "]" * 50000 + "\n```"
    assert parse_board_update(raw) == {}


def test_parse_board_update_double_board_open_not_misextracted():
    # FIX: a 2nd ```syncmill-board open fence must not be mis-read as the first block's
    # close (the parser now agrees with strip_board_block on block bounds).
    raw = (
        '```syncmill-board\n{"state": "review"}\n'
        '```syncmill-board\n{"state": "done"}\n```'
    )
    assert parse_board_update(raw) == {}  # ambiguous double block degrades, not "pick one"


# ── FIFO dispatch order (coverage) ───────────────────────────────────────────


def test_next_runnable_fifo_oldest_first(board, clock):
    # COVERAGE: two independently-runnable items dispatch oldest-first (created_at).
    clock.t = 1000
    a = board.add("A")
    clock.t = 1001
    b = board.add("B")
    assert board.next_runnable("r1").id == a.id  # oldest first
    assert board.next_runnable("r2").id == b.id


def test_next_runnable_fifo_rowid_tiebreak(board):
    # COVERAGE: same created_at (constant clock) → insertion order via the rowid tie-break.
    a = board.add("A")
    board.add("B")
    assert board.next_runnable("r1").id == a.id  # first-inserted wins the same-tick tie


# ── FAILED result with error=None (coverage) ─────────────────────────────────


def test_apply_update_failed_none_error_blocks_with_default_reason(board):
    # COVERAGE: a FAILED result with error=None must still set a NON-NULL blocked_reason
    # (the `or` fallback), not violate the NOT NULL column.
    it = board.add("x")
    board.claim(it.id, "r1")
    updated, _ = board.apply_update(
        it.id, {}, _result(status=TaskStatus.FAILED, error=None), actor="c"
    )
    assert updated.state is WorkState.BLOCKED
    assert updated.blocked_reason.startswith("run failed (exit")


# ── recover reclaims a leaseless running row (coverage) ──────────────────────


def test_recover_stale_running_reclaims_leaseless_running(tmp_path, clock):
    # COVERAGE: a running row with NULL claimed_at (corrupt/legacy) is reclaimed,
    # exercising recover's `ca is not None` guard (else `None > cutoff` → TypeError).
    import sqlite3
    db = str(tmp_path / "b.db")
    bd = Board(db, now=clock, lease_timeout_seconds=100)
    it = bd.add("x")
    bd.close()
    raw = sqlite3.connect(db)
    raw.execute(
        "UPDATE work_items SET state='running', lease_run_id=NULL, claimed_at=NULL WHERE id=?",
        (it.id,),
    )
    raw.commit()
    raw.close()
    bd2 = Board(db, now=clock, lease_timeout_seconds=100)
    try:
        assert bd2.recover_stale_running() == 1  # NULL claimed_at → reclaimed
        assert bd2.get(it.id).state is WorkState.PLANNED
    finally:
        bd2.close()


# ── lease-fence drop records no audit row (coverage) ─────────────────────────


def test_apply_update_lease_fence_drop_records_no_audit(board, clock):
    # COVERAGE: a dropped stale apply_update must append NO phantom audit row.
    it = board.add("x")
    board.claim(it.id, "RUN-A")
    clock.t = 1000 + 101
    board.recover_stale_running()
    board.next_runnable("RUN-B")
    board.apply_update(it.id, {}, _result(agent_id="bee"), actor="bee", run_id="RUN-B")
    n_before = len(board.transitions(it.id))
    board.apply_update(it.id, {}, _result(agent_id="ayy"), actor="ayy", run_id="RUN-A")
    assert len(board.transitions(it.id)) == n_before  # dropped write logged nothing


# ── Phase 2 (D1): decisions — record, scope, supersede ───────────────────────


def test_add_decision_global_and_scoped_and_active_filter(board):
    g = board.add_decision("always run pytest", scope="")          # global
    s = board.add_decision("use sqlite for the board", scope="epic")  # goal-scoped
    board.add_decision("ship on fridays", scope="other")
    # goal=None → every active decision regardless of scope
    assert {d.text for d in board.active_decisions()} == {
        "always run pytest", "use sqlite for the board", "ship on fridays"
    }
    # goal="epic" → global + epic-scoped only (never the "other"-scoped one)
    assert [d.id for d in board.active_decisions(goal="epic")] == [g.id, s.id]
    # goal="" (an item with no goal) → only the global decision
    assert [d.text for d in board.active_decisions(goal="")] == ["always run pytest"]


def test_active_decisions_are_oldest_first(board, clock):
    first = board.add_decision("first", scope="epic")
    clock.t += 1
    second = board.add_decision("second", scope="epic")
    assert [d.id for d in board.active_decisions(goal="epic")] == [first.id, second.id]


def test_supersede_retires_old_decision(board, clock):
    old = board.add_decision("use postgres", scope="epic")
    assert [d.text for d in board.active_decisions(goal="epic")] == ["use postgres"]
    clock.t += 5
    new = board.add_decision("use sqlite", scope="epic", supersedes=old.id)
    # the old decision drops out of the active set; only the new one applies
    assert [d.text for d in board.active_decisions(goal="epic")] == ["use sqlite"]
    # the superseded row survives (append-only history), now marked retired
    refreshed = next(
        d for d in board._conn.execute("SELECT * FROM decisions").fetchall()
        if d["id"] == old.id
    )
    assert refreshed["superseded_by"] == new.id
    assert refreshed["superseded_at"] == clock.t


def test_supersede_missing_decision_raises_and_inserts_nothing(board):
    with pytest.raises(BoardError, match="no such decision"):
        board.add_decision("x", supersedes="00000000-0000-0000-0000-000000000000")
    assert board.active_decisions() == []  # the new decision was NOT inserted


def test_supersede_already_superseded_raises(board):
    a = board.add_decision("a", scope="epic")
    board.add_decision("b", scope="epic", supersedes=a.id)
    with pytest.raises(BoardError, match="already superseded"):
        board.add_decision("c", scope="epic", supersedes=a.id)


def test_supersede_cross_scope_raises_and_retires_nothing(board):
    # supersede must only retire a decision in the SAME scope. A cross-scope id is a
    # caller mistake — it must NOT silently retire an unrelated goal's active decision
    # while inserting here. MUTATION GUARD: dropping `AND scope=?` from the UPDATE lets
    # the goalB decision get retired (and the goalA one inserted) → this test fails.
    other = board.add_decision("keep me", scope="goalB")
    with pytest.raises(BoardError, match="same scope"):
        board.add_decision("new one", scope="goalA", supersedes=other.id)
    # goalB's decision is untouched (still active) and the new goalA one was NOT inserted
    assert [d.text for d in board.active_decisions(goal="goalB")] == ["keep me"]
    assert board.active_decisions(goal="goalA") == []


def test_supersede_same_scope_still_works_after_scope_guard(board):
    # the same-scope path the guard protects still retires + replaces in place (global
    # and goal-scoped alike) — the guard narrows cross-scope, never same-scope.
    g = board.add_decision("old global", scope="")
    board.add_decision("new global", scope="", supersedes=g.id)
    assert [d.text for d in board.active_decisions(goal="")] == ["new global"]


def test_add_decision_rejects_empty_text(board):
    for bad in ("", "   "):
        with pytest.raises(BoardError, match="non-empty"):
            board.add_decision(bad)


def test_add_decision_rolls_back_write_lock_on_non_sqlite_error(board):
    # MUTATION GUARD for the txn-safety invariant: a NON-sqlite error inside the
    # BEGIN IMMEDIATE block must still roll back, releasing the RESERVED write lock.
    # If add_decision caught only sqlite3.Error (the pre-fix narrow catch), a ValueError
    # would escape UN-rolled-back → not a BoardError AND the next BEGIN IMMEDIATE fails
    # with "cannot start a transaction within a transaction". This kills that mutation.
    class _BoomConn:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **k):
            if sql.startswith("INSERT INTO decisions"):
                raise ValueError("non-sqlite boom mid-transaction")
            return self._real.execute(sql, *a, **k)

        def __getattr__(self, n):
            return getattr(self._real, n)

    board._conn = _BoomConn(board._conn)
    with pytest.raises(BoardError, match="add_decision failed"):
        board.add_decision("doomed")
    # the write lock was released — a fresh transactional write succeeds
    assert board.add("after the rollback") is not None
    assert board.active_decisions() == []  # the doomed decision was not inserted


def test_decisions_injected_into_item_prompt_via_active_decisions(board):
    board.add_decision("never force-push", scope="")
    board.add_decision("target python 3.12", scope="epic")
    it = board.add("refactor", goal="epic")
    texts = [d.text for d in board.active_decisions(goal=it.goal)]
    prompt = render_item_prompt(it, active_decisions=texts)
    assert "never force-push" in prompt and "target python 3.12" in prompt
    assert "do not re-litigate" in prompt.lower()


# ── Phase 2 (D2): board stats ────────────────────────────────────────────────


def test_stats_counts_states_and_false_done(board, clock):
    board.add("planned-one")
    done = board.add("done-one")
    board.claim(done.id, "r1")
    # mark done WITH a passing gate → legitimate done (not false-done)
    gate = GateReport(passed=True, score=1.0, checks=[GateCheck("pytest", True, 0)])
    board.apply_update(done.id, {"state": "done"}, _result(gate=gate), actor="claude",
                       run_id="r1")
    s = board.stats()
    assert s.total == 2
    assert s.by_state == {"planned": 1, "done": 1}
    assert s.false_done == []  # the done item is verified


def test_stats_flags_false_done_when_unverified(board):
    # Forge a done row with NO passing evidence (bypassing the gate) → integrity alarm.
    bad = board.add("forged")
    board._conn.execute(
        "UPDATE work_items SET state='done' WHERE id=?", (bad.id,)
    )
    s = board.stats()
    assert s.false_done == [bad.id]


def test_stats_counts_redispatched_and_reclaimed(board, clock):
    it = board.add("flaky")
    board.claim(it.id, "RUN-A")          # claim #1 (planned→running)
    clock.t += 101                        # past the 100s lease
    board.recover_stale_running()         # reclaim #1 (running→planned, audited)
    board.claim(it.id, "RUN-B")          # claim #2 → redispatched
    s = board.stats()
    assert s.redispatched == [it.id]      # claimed (→running) more than once
    assert s.reclaimed_stale == 1         # one stale-lease recovery audited


def test_stats_annotating_a_running_item_is_not_redispatched(board):
    # codex-found major: a same-state running→running annotation (a human `board
    # update` on a running item) ALSO writes to_state='running' but is NOT a claim, so
    # it must NOT be counted as redispatched work. Only repeated planned→running claims
    # are. Reverting the redispatched query to a bare to_state filter fails this test.
    it = board.add("worked-on")
    board.claim(it.id, "RUN-A")                       # 1 claim: planned→running
    board.transition(it.id, None, actor="human", note="checking in")  # running→running
    s = board.stats()
    assert s.redispatched == []  # one claim + one annotation ≠ redispatch


def test_stats_aged_signals_respect_stall_window(board, clock):
    blocked = board.add("blk")
    board.claim(blocked.id, "r1")
    board.apply_update(blocked.id, {}, _result(status=TaskStatus.FAILED, error="boom"),
                       actor="claude", run_id="r1")  # → blocked, human_required
    hr = board.add("needs-human", human_required=True)
    hand = board.add("handed-off")
    board._conn.execute(
        "UPDATE work_items SET handoff='please continue' WHERE id=?", (hand.id,)
    )
    # within the window → nothing aged yet
    fresh = board.stats(stall_seconds=100)
    assert fresh.blocked_aged == [] and fresh.human_required_aged == []
    # advance past the window → all three age out
    clock.t += 200
    aged = board.stats(stall_seconds=100)
    assert aged.blocked_aged == [blocked.id]
    assert hr.id in aged.human_required_aged
    assert aged.dropped_handoffs == [hand.id]


def test_stats_uses_configured_stall_seconds_default(tmp_path, clock):
    bd = Board(str(tmp_path / "b.db"), now=clock, stall_seconds=50)
    try:
        b = bd.add("blk")
        bd.claim(b.id, "r1")
        bd.apply_update(b.id, {}, _result(status=TaskStatus.FAILED, error="x"),
                        actor="c", run_id="r1")
        clock.t += 60  # past the board's configured 50s default
        assert bd.stats().blocked_aged == [b.id]
    finally:
        bd.close()


# ── Phase 2 (D3): get() id validation (gateway single-coupling guard) ─────────


def test_get_malformed_id_returns_none_without_query(board):
    # a non-uuid id is a client typo: not-found, never a probing lookup
    assert board.get("not-a-uuid") is None
    assert board.get("") is None


def test_get_malformed_id_short_circuits_before_any_query(board):
    # MUTATION GUARD: prove the uuid guard skips the query (not just that it returns
    # None — a real query on a non-uuid would ALSO return None). Close the connection:
    # now a query would raise, so a malformed id returning None proves no query ran,
    # while a well-formed (but missing) id DOES hit the closed conn and raises. If the
    # guard were neutered (`_is_valid_item_id` → True), the malformed call would raise
    # BoardError too — so this test fails, killing the mutation.
    board.close()
    assert board.get("not-a-uuid") is None          # guarded → no query → None
    with pytest.raises(BoardError):
        board.get("11111111-2222-3333-4444-555555555555")  # valid id → query → raises


def test_get_valid_but_missing_uuid_returns_none(board):
    assert board.get("11111111-2222-3333-4444-555555555555") is None


# ── Phase 2: build_board threads stall_seconds ───────────────────────────────


def test_build_board_threads_stall_seconds(tmp_path):
    cfg = BoardConfig(enabled=True, db_path=str(tmp_path / "b.db"), stall_seconds=42)
    bd = build_board(cfg)
    try:
        assert bd._stall_seconds == 42
    finally:
        bd.close()


def test_renew_claim_is_fenced_and_does_not_add_transition(board, clock):
    item = board.add("long task")
    assert board.claim(item.id, "run-1") is not None
    before = board.transitions(item.id)
    clock.t += 50
    assert board.renew_claim(item.id, "wrong-run") is False
    assert board.renew_claim(item.id, "run-1") is True
    renewed = board.get(item.id)
    assert renewed.claimed_at == clock.t
    assert renewed.updated_at != clock.t
    assert board.transitions(item.id) == before


def test_heartbeat_prevents_stale_recovery_deterministically(tmp_path, clock):
    from syncmill.supervisor import _LeaseHeartbeat

    board = Board(
        str(tmp_path / "heartbeat.db"), now=clock, lease_timeout_seconds=1
    )
    item = board.add("slow task")
    assert board.claim(item.id, "run-live") is not None
    heartbeat = _LeaseHeartbeat(board, item.id, "run-live", interval=0.01)
    clock.t += 2
    heartbeat.start()
    try:
        assert heartbeat.wait_for_renewal(1)
        observer = board.new_connection()
        try:
            assert observer.recover_stale_running() == 0
            heartbeat.stop()
            clock.t += 2
            assert observer.recover_stale_running() == 1
        finally:
            observer.close()
    finally:
        heartbeat.stop()
        board.close()


# ── open path: no write in steady state (lease-heartbeat starvation) ─────────


def test_opening_a_board_takes_no_write_lock(tmp_path, clock):
    """A steady-state open must be READ-ONLY.

    The scratch-ledger backfills used to run unconditionally on every open, and an
    INSERT takes the write lock whether or not its SELECT matches. Every `Board(...)`
    was therefore a writer — including the per-run lease heartbeat's own connection,
    which then waited out `busy_timeout` behind a concurrent `BEGIN IMMEDIATE` and
    raised, killing the heartbeat and letting a live run's lease expire.
    """
    db = str(tmp_path / "locked.db")
    owner = Board(db, now=clock)
    item = owner.add("A")
    assert owner.claim(item.id, "run-a") is not None
    try:
        owner._conn.execute("BEGIN IMMEDIATE")  # hold the write lock
        try:
            # 50ms busy_timeout: if the open needed the write lock this raises.
            other = Board(db, now=clock, busy_timeout_ms=50)
            other.close()
        finally:
            owner._conn.execute("ROLLBACK")
    finally:
        owner.close()


def test_opening_a_never_claimed_board_takes_no_write_lock(tmp_path, clock):
    """The empty case is the one an emptiness guard gets wrong.

    A board that has never been claimed has an empty ledger forever, so a guard that
    only asks "is the ledger empty?" runs the zero-row audit INSERT — and takes the
    write lock — on EVERY open, which is the starvation the guard exists to remove.
    """
    db = str(tmp_path / "unclaimed.db")
    owner = Board(db, now=clock)
    owner.add("A")  # items, but no claim has ever been made
    try:
        owner._conn.execute("BEGIN IMMEDIATE")
        try:
            other = Board(db, now=clock, busy_timeout_ms=50)
            other.close()
        finally:
            owner._conn.execute("ROLLBACK")
    finally:
        owner.close()


def test_open_backfills_ledger_for_a_legacy_running_lease(tmp_path, clock):
    """The read guard must not disable the backfill it guards.

    The audit row is deleted along with the ledger, so ONLY the live-lease backfill
    can restore this row — with the claim's audit row left in place the audit
    migration restores it first and this test says nothing about the second backfill.
    """
    db = str(tmp_path / "legacy.db")
    board = Board(db, now=clock)
    item = board.add("A")
    assert board.claim(item.id, "run-a") is not None
    board._conn.execute("DELETE FROM board_run_namespaces")  # simulate a legacy db
    board._conn.execute("DELETE FROM transitions WHERE to_state='running'")
    board.close()

    reopened = Board(db, now=clock)
    try:
        rows = reopened._conn.execute(
            "SELECT run_id, item_id FROM board_run_namespaces"
        ).fetchall()
        assert [(r["run_id"], r["item_id"]) for r in rows] == [("run-a", item.id)]
    finally:
        reopened.close()


def test_open_backfills_ledger_from_audit_when_empty(tmp_path, clock):
    """The audit-derived migration still runs when the ledger is empty."""
    db = str(tmp_path / "audit.db")
    board = Board(db, now=clock)
    item = board.add("A")
    assert board.claim(item.id, "run-a") is not None
    # Resolve the item (leaves `running`), then drop the ledger: only the audit
    # remembers the claim, exactly the pre-P2 shape.
    board.transition(item.id, WorkState.REVIEW.value, actor="human")
    board._conn.execute("DELETE FROM board_run_namespaces")
    board.close()

    reopened = Board(db, now=clock)
    try:
        rows = reopened._conn.execute(
            "SELECT run_id, item_id FROM board_run_namespaces"
        ).fetchall()
        assert [(r["run_id"], r["item_id"]) for r in rows] == [("run-a", item.id)]
    finally:
        reopened.close()


def test_reused_run_id_names_the_owning_item(board):
    """The collision message must read as a caller error, not board corruption."""
    a, b = board.add("A"), board.add("B")
    assert board.claim(a.id, "same-run") is not None
    with pytest.raises(BoardError, match=f"already owns the scratch namespace for item {a.id}"):
        board.claim(b.id, "same-run")


def test_null_byte_db_path_raises_board_error(tmp_path):
    """`Board.__init__` raises ONLY BoardError — `serve` relies on that contract."""
    with pytest.raises(BoardError):
        Board(str(tmp_path / "bo\x00ard.db"))


# ── stats: one read snapshot ─────────────────────────────────────────────────


def test_stats_reads_one_snapshot_across_its_queries(tmp_path, clock, monkeypatch):
    """The three reads must not straddle a concurrent claim.

    Deterministic: a second connection commits a re-claim of the same item at the
    exact moment `stats()` sits between its `work_items` read and its audit reads.
    In one snapshot the item is `planned` with one claim in its history. Without the
    transaction the state count comes from the pre-claim world while `redispatched`
    comes from the post-claim one — an integrity alarm for a state that never was.
    """
    import syncmill.board as B

    db = str(tmp_path / "snapshot.db")
    reader = Board(db, now=clock, lease_timeout_seconds=100)
    writer = Board(db, now=clock, lease_timeout_seconds=100)
    try:
        item = reader.add("A")
        assert reader.claim(item.id, "run-1") is not None  # claim #1
        clock.t += 1000
        assert reader.recover_stale_running() == 1  # back to planned

        real = B._row_to_item
        calls = {"n": 0}

        def hooked(row):
            calls["n"] += 1
            if calls["n"] == 1:
                # Straddle point: commit claim #2 (a re-dispatch) from another
                # connection, after stats() has read work_items.
                writer.claim(item.id, "run-2")
            return real(row)

        monkeypatch.setattr(B, "_row_to_item", hooked)
        st = reader.stats()
        monkeypatch.undo()

        assert st.by_state == {"planned": 1}  # pre-claim world
        assert st.redispatched == []          # ...and the audit agrees with it
        # The concurrent claim IS visible to a fresh call — the snapshot was a
        # snapshot, not a stale cache.
        assert reader.stats().redispatched == [item.id]
    finally:
        writer.close()
        reader.close()


def test_stats_leaves_no_open_transaction(board):
    item = board.add("A")
    assert board.claim(item.id, "run-a") is not None
    board.stats()
    assert not board._conn.in_transaction
    # A second write still works — the read transaction was committed, not leaked.
    board.add("B")


def test_stats_rolls_back_on_a_non_sqlite_error(board, monkeypatch):
    """A corrupt-row ValueError mid-snapshot must release the read transaction —
    a leaked one makes every later write fail with "cannot start a transaction
    within a transaction"."""
    board.add("A")
    monkeypatch.setattr(
        "syncmill.board._row_to_item",
        lambda row: (_ for _ in ()).throw(ValueError("corrupt row")),
    )
    with pytest.raises(BoardError, match="stats failed"):
        board.stats()
    assert not board._conn.in_transaction
    monkeypatch.undo()
    board.add("B")  # the connection is still usable


# ── transition: blocked_reason lands on the row ──────────────────────────────


def test_transition_records_blocked_reason(board):
    item = board.add("A")
    board.transition(
        item.id, WorkState.BLOCKED.value, actor="system",
        note="crashed", blocked_reason="orchestration crashed: RuntimeError: boom",
    )
    assert board.get(item.id).blocked_reason == "orchestration crashed: RuntimeError: boom"


def test_transition_keeps_the_existing_reason_when_none_given(board):
    item = board.add("A")
    board.transition(
        item.id, WorkState.BLOCKED.value, actor="system", blocked_reason="first",
    )
    board.transition(item.id, WorkState.BLOCKED.value, actor="human", note="still stuck")
    assert board.get(item.id).blocked_reason == "first"


def test_transition_clears_the_reason_when_leaving_blocked(board):
    item = board.add("A")
    board.transition(
        item.id, WorkState.BLOCKED.value, actor="system", blocked_reason="first",
    )
    board.transition(item.id, WorkState.PLANNED.value, actor="human")
    assert board.get(item.id).blocked_reason == ""
