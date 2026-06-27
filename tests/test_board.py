"""board.py — the persistent work-board store, state machine, atomic claims,
done-gate, stale-claim recovery, and the pure parse/strip/render helpers.

Drives the REAL ``Board`` against ``tmp_path/"board.db"`` with an injected clock
(like ``test_breaker.py``) — hermetic: SQLite in a tmp dir touches nothing real.
"""

from __future__ import annotations

import uuid

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
    updated = board.apply_update(it.id, {}, _result(output="done it"), actor="claude")
    assert updated.state is WorkState.REVIEW
    assert updated.owner_agent == "claude"  # record-only owner stamped
    assert updated.lease_run_id is None  # lease cleared on leaving running


def test_apply_update_maps_passing_gate_to_evidence(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    gate = GateReport(passed=True, score=1.0, checks=[GateCheck("pytest", True, 0)])
    updated = board.apply_update(it.id, {}, _result(gate=gate), actor="claude")
    assert any(e.kind == "gate" and e.passed for e in updated.evidence)


def test_apply_update_agent_done_with_gate_promotes(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    gate = GateReport(passed=True, score=1.0, checks=[GateCheck("pytest", True, 0)])
    parsed = {"state": "done"}
    updated = board.apply_update(it.id, parsed, _result(gate=gate), actor="claude")
    assert updated.state is WorkState.DONE  # verified → honored


def test_apply_update_agent_done_without_evidence_clamps_to_review(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    updated = board.apply_update(it.id, {"state": "done"}, _result(), actor="claude")
    assert updated.state is WorkState.REVIEW  # advisory done clamped, NEVER raised
    assert any("clamped" in t["note"] for t in board.transitions(it.id))


def test_apply_update_agent_blocked_request(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    parsed = {"state": "blocked", "blocked_reason": "needs creds"}
    updated = board.apply_update(it.id, parsed, _result(), actor="claude")
    assert updated.state is WorkState.BLOCKED
    assert updated.blocked_reason == "needs creds"
    assert updated.human_required is True


def test_apply_update_failed_result_blocks(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    updated = board.apply_update(
        it.id, {}, _result(status=TaskStatus.FAILED, error="boom"), actor="codex"
    )
    assert updated.state is WorkState.BLOCKED
    assert updated.blocked_reason == "boom"
    assert updated.human_required is True


def test_apply_update_merges_outputs(board):
    it = board.add("x")
    board.claim(it.id, "r1")
    parsed = {"outputs": ["doc.md"], "handoff": "wire it up"}
    updated = board.apply_update(
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
    board.apply_update(it.id, {}, _result(agent_id="bee"), actor="bee", run_id="RUN-B")
    assert board.get(it.id).state is WorkState.REVIEW and board.get(it.id).owner_agent == "bee"
    # now A's stale write-back arrives with the OLD lease — it must be a no-op
    board.apply_update(it.id, {}, _result(agent_id="ayy"), actor="ayy", run_id="RUN-A")
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
    updated = board.apply_update(it.id, {}, _result(), actor="claude")  # no run_id
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
    updated = board.apply_update(it.id, {}, _result(), actor="claude")  # succeeds
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
    updated = board.apply_update(it.id, {"human_required": True}, _result(), actor="c")
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
