"""HEAD operating released work-boards (#271, persisted contracts D, Board part 2).

``test_frozen_board.py`` reads the released databases; this file runs HEAD's
operational paths over copies of the same databases: ``board run`` dispatch
and prompt injection (``Supervisor.run_item``), stale-claim recovery, the
lease fences, ``renew_claim``, the scratch-namespace GC ledger, ``board
compact``, lesson promotion, and the gateway, dashboard and TUI board readers.

No fixture is added and nothing is regenerated. Expected values are written
from what the release's own interpreter read (``bindings.json``), from the raw
cells, or as literals; a HEAD function is never its own oracle.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid

import pytest
from textual.widgets import DataTable

import syncmill.supervisor as S
from syncmill.board import Board, BoardError, WorkState
from syncmill.config import SyncmillConfig
from syncmill.dashboard import take_snapshot
from syncmill.gateway import build_gateway
from syncmill.memory import run_async
from syncmill.models import TaskResult, TaskStatus
from syncmill.strategies import RouteStrategy
from syncmill.supervisor import Supervisor
from syncmill.tui import SyncmillTuiApp
from syncmill.worktree import CapturedChanges

from conftest import (
    FakeAgent,
    FakeBackend,
    FakeGate,
    FakeMemory,
    FakeRegistry,
    FakeWorktreePool,
    make_memory,
)
from test_frozen_board import (
    CASES,
    RELEASES,
    Frozen,
    _cells,
    _printable,
    _show_lines,
    plain,
)

#: HEAD's default lease, the one ``Board`` and ``build_board`` use.
LEASE = 1800
#: The first retry delay after a failed scratch purge.
FIRST_RETRY = 60.0
CHANGED = "changed.py"  # what the patched capture reports for every run


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    def install(release: str, scenario: str) -> Frozen:
        return Frozen(release, scenario, tmp_path, monkeypatch)
    return install


@pytest.fixture
def pool(monkeypatch):
    """No real worktree or git: one fake pool, and every run reports CHANGED."""
    fake = FakeWorktreePool(is_git=True)
    monkeypatch.setattr(S, "WorktreePool", lambda repo_root, task_id: fake)
    monkeypatch.setattr(
        "syncmill.strategies.base.capture_changes",
        lambda p, base=None, *, git_dir=None: CapturedChanges((CHANGED,), ""),
    )
    return fake


class Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _open(f: Frozen, clock) -> Board:
    return Board(str(f.db), now=clock, stall_seconds=f.stall)


def _supervisor(board: Board, agent: FakeAgent, memory: FakeMemory | None = None) -> Supervisor:
    sup = Supervisor.__new__(Supervisor)
    sup._config = SyncmillConfig()
    sup._registry = FakeRegistry([agent])
    sup._memory = memory if memory is not None else FakeMemory()
    sup._breaker = None
    sup._matcher = None
    sup._board = board
    sup._strategy = RouteStrategy()
    sup.last_report_path = None
    return sup


def _released(f: Frozen, role: str) -> dict:
    return f.read["items"][f.items[role]]


def _task_of(prompt: str) -> str:
    return prompt.split("\nTask: ", 1)[1].split("\n\n", 1)[0]


def _claimed_roles(f: Frozen, agent: FakeAgent) -> list[str]:
    """The role of every item the agent was run on, from the prompts it got."""
    by_task = {_released(f, role)["task"]: role for role in f.items}
    assert len(by_task) == len(f.items), "released tasks must be distinct"
    return [by_task[_task_of(call["prompt"])] for call in agent.calls]


def _row(f: Frozen, table: str, key: str, value) -> dict:
    rows = f.rows(f"SELECT * FROM {table} WHERE {key} = ?", value)
    assert len(rows) == 1, (table, key, value)
    return dict(rows[0])


def _ledger(f: Frozen) -> list[dict]:
    return [dict(r) for r in f.rows("SELECT * FROM board_run_namespaces ORDER BY rowid")]


def _run_of(f: Frozen, item_id: str, claimed_at: float) -> str:
    """The run id HEAD claimed ``item_id`` under at ``claimed_at`` (its ledger row)."""
    rows = [r for r in _ledger(f) if r["item_id"] == item_id and r["claimed_at"] == claimed_at]
    assert len(rows) == 1, (item_id, rows)
    return rows[0]["run_id"]


def _history(transitions: list[dict]) -> list[tuple]:
    return [(t["from_state"], t["to_state"], t["actor"], t["note"]) for t in transitions]


_DONE_BLOCK = 'done\n```syncmill-board\n{"state": "done"}\n```\n'


# ── board run: dispatch order over released items ─────────────────────────


@pytest.mark.parametrize("release", RELEASES)
def test_board_run_claims_released_items_first_in_first_out(release, frozen, pool):
    f = frozen(release, "lifecycle")
    clock = Clock(f.frozen_at + 10)
    board = _open(f, clock)
    before = {r["id"]: dict(r) for r in f.rows("SELECT * FROM work_items")}
    agent = FakeAgent("codex", output="ok")
    sup = _supervisor(board, agent)
    try:
        while sup.run_item() is not None:
            pass
        claimed = _claimed_roles(f, agent)
        # Planned, not human-required, every dependency done: oldest first.
        # Skipped: human-required, a missing dependency, a planned dependency,
        # and everything not planned.
        assert claimed == ["bare-done-refused", "annotated", "human-cleared", "needs-done"]
        for role in claimed:
            item = board.get(f.items[role])
            assert item.state == WorkState.REVIEW
            assert item.owner_agent == "codex"
            assert item.outputs == _released(f, role)["outputs"] + [CHANGED]
            assert item.lease_run_id is None
            assert _history(board.transitions(item.id))[-2:] == [
                ("planned", "running", _run_of(f, item.id, clock.now), "claimed"),
                ("running", "review", "codex", "agent completed; awaiting review"),
            ]
    finally:
        board.close()
    after = {r["id"]: dict(r) for r in f.rows("SELECT * FROM work_items")}
    untouched = set(f.items) - set(claimed)
    assert {"human-required", "needs-missing", "needs-planned", "needs-two"} <= untouched
    for role in untouched:
        assert after[f.items[role]] == before[f.items[role]], role


@pytest.mark.parametrize("release", RELEASES)
def test_a_released_dependency_releases_its_dependents_once_done(release, frozen, pool):
    f = frozen(release, "lifecycle")
    # The dependencies the release stored, by role.
    deps = {role: [next(r for r, i in f.items.items() if i == d)
                   for d in _released(f, role)["depends_on"]]
            for role in ("needs-done", "needs-planned", "needs-two")}
    assert deps == {"needs-done": ["verified"], "needs-planned": ["annotated"],
                    "needs-two": ["verified", "annotated"]}
    clock = Clock(f.frozen_at + 10)
    board = _open(f, clock)
    agent = FakeAgent("codex", output="ok")
    sup = _supervisor(board, agent)
    try:
        while sup.run_item() is not None:
            pass
        first = len(agent.calls)
        # A human signs off the shared dependency on the released row.
        f.clock(clock.now)
        signed = f.cli("board", "update", f.items["annotated"], "--state", "done", "--sign-off")
        assert signed.exit_code == 0, signed.output
        while sup.run_item() is not None:
            pass
        assert _claimed_roles(f, agent)[first:] == ["needs-planned", "needs-two"]
    finally:
        board.close()


def _running_facts(f: Frozen) -> tuple[str, str, float]:
    killed = _released(f, "killed")
    assert killed["state"] == "running"
    return f.items["killed"], killed["lease_run_id"], killed["claimed_at"]


@pytest.mark.parametrize("release", RELEASES)
@pytest.mark.parametrize("goal", [None, "backend"])
def test_goal_scopes_the_claim_of_a_reclaimed_released_item(release, goal, frozen, pool):
    f = frozen(release, "running")
    _, _, claimed_at = _running_facts(f)
    # The waiting item is older; only the goal filter reaches the killed one.
    assert _released(f, "waiting")["created_at"] < _released(f, "killed")["created_at"]
    assert (_released(f, "waiting")["goal"], _released(f, "killed")["goal"]) == ("", "backend")
    board = _open(f, Clock(claimed_at + LEASE))
    agent = FakeAgent("codex", output="ok")
    try:
        assert _supervisor(board, agent).run_item(goal=goal) is not None
    finally:
        board.close()
    assert _claimed_roles(f, agent) == ["waiting" if goal is None else "killed"]


# ── board run --id: what a released item puts in the prompt ───────────────


@pytest.mark.parametrize("release", RELEASES)
def test_a_released_handoff_and_decisions_reach_the_prompt(release, frozen, pool):
    f = frozen(release, "lifecycle")
    item_id = f.items["handed-off"]
    released = f.read["items"][item_id]
    assert (released["state"], released["goal"]) == ("review", "backend")
    clock = Clock(f.frozen_at + 10)
    f.clock(clock.now)
    back = f.cli("board", "update", item_id, "--state", "planned")
    assert back.exit_code == 0, back.output
    board = _open(f, clock)
    agent = FakeAgent("codex", output="ok")
    try:
        assert _supervisor(board, agent).run_item(item_id=item_id) is not None
        item = board.get(item_id)
        history = board.transitions(item_id)
    finally:
        board.close()
    prompt = agent.calls[0]["prompt"]
    active = [d["text"] for d in f.read["active_decisions"]["backend"]]
    assert active, "the released board has backend decisions"
    assert "You are working on a tracked work-board item.\nGoal: backend\n\n" in prompt
    assert ("Team decisions you MUST respect (do not re-litigate):\n"
            + "\n".join(f"- {t}" for t in active) + "\n\n") in prompt
    retired = [d["text"] for d in f.read["decisions_mapped"]
               if d["superseded_by"] or d["scope"] not in ("", "backend")]
    assert retired
    for text in retired:
        assert text not in prompt, text
    assert f"Handoff note from prior work on this item:\n{released['handoff']}\n\n" in prompt
    assert f"\nTask: {released['task']}\n\n" in prompt
    # The run's outcome is added to the released row, never replacing it.
    assert item.state == WorkState.REVIEW
    assert item.owner_agent == "codex"
    assert item.outputs == ["notes/plan.md", "calc.py", CHANGED]
    assert plain(item.evidence) == released["evidence"]
    for field in ("task", "goal", "inputs", "handoff", "depends_on", "created_at"):
        assert getattr(item, field) == released[field], field
    assert history[:len(f.read["transitions"][item_id])] == f.read["transitions"][item_id]
    assert _history(history[len(f.read["transitions"][item_id]):]) == [
        ("review", "planned", "human", ""),
        ("planned", "running", _run_of(f, item_id, clock.now), "claimed"),
        ("running", "review", "codex", "agent completed; awaiting review"),
    ]
    assert _row(f, "board_run_namespaces", "run_id",
                _run_of(f, item_id, clock.now))["purged_at"] is None


@pytest.mark.parametrize("release", RELEASES)
def test_released_inputs_reach_the_prompt_and_survive_the_run(release, frozen, pool):
    f = frozen(release, "lifecycle")
    item_id = f.items["imported-1"]
    released = f.read["items"][item_id]
    assert len(released["inputs"]) == 5 and released["human_required"]
    clock = Clock(f.frozen_at + 10)
    f.clock(clock.now)
    back = f.cli("board", "update", item_id, "--state", "planned", "--clear-human-required")
    assert back.exit_code == 0, back.output
    board = _open(f, clock)
    agent = FakeAgent("codex", output="ok")
    try:
        assert _supervisor(board, agent).run_item(item_id=item_id) is not None
        item = board.get(item_id)
    finally:
        board.close()
    assert ("Inputs / prior artifacts:\n"
            + "\n".join(f"- {x}" for x in released["inputs"]) + "\n\n") in agent.calls[0]["prompt"]
    assert item.inputs == released["inputs"]
    assert plain(item.evidence) == released["evidence"]


@pytest.mark.parametrize("release", RELEASES)
@pytest.mark.parametrize("gated", [True, False], ids=["this-run-gated", "no-gate"])
def test_only_this_runs_verification_lets_an_agent_close_a_released_item(
    release, gated, frozen, pool, monkeypatch
):
    # The released row already carries a passing gate; an agent's `done` still
    # needs a passing check from THIS run.
    f = frozen(release, "lifecycle")
    item_id = f.items["handed-off"]
    released = f.read["items"][item_id]
    assert [(e["kind"], e["passed"]) for e in released["evidence"]] == [("gate", True)]
    gate = FakeGate(passes=True)
    monkeypatch.setattr(S, "build_gate", lambda config: gate if gated else None)
    clock = Clock(f.frozen_at + 10)
    f.clock(clock.now)
    assert f.cli("board", "update", item_id, "--state", "planned").exit_code == 0
    board = _open(f, clock)
    try:
        _supervisor(board, FakeAgent("codex", output=_DONE_BLOCK)).run_item(item_id=item_id)
        item = board.get(item_id)
        last = board.transitions(item_id)[-1]
    finally:
        board.close()
    if gated:
        assert gate.evaluated == ["codex"]
        assert item.state == WorkState.DONE
        assert (last["to_state"], last["note"]) == ("done", "agent completed; verified")
        assert plain(item.evidence) == released["evidence"] + [{
            "kind": "gate", "ref": "1/1 checks passed", "passed": True,
            "at": clock.now, "by": "codex"}]
    else:
        assert item.state == WorkState.REVIEW
        assert (last["to_state"], last["note"]) == (
            "review", "agent requested done without verification — clamped to review")
        assert plain(item.evidence) == released["evidence"]


# ── a released running lease ──────────────────────────────────────────────


@pytest.mark.parametrize("release", RELEASES)
@pytest.mark.parametrize("side", ["before", "at", "after"])
def test_a_released_lease_is_reclaimed_exactly_at_its_timeout(release, side, frozen, pool):
    f = frozen(release, "running")
    item_id, lease, claimed_at = _running_facts(f)
    expiry = claimed_at + LEASE
    now = {"before": math.nextafter(expiry, -math.inf), "at": expiry,
           "after": math.nextafter(expiry, math.inf)}[side]
    board = _open(f, Clock(now))
    agent = FakeAgent("codex", output="ok")
    sup = _supervisor(board, agent)
    released_reclaims = f.read["stats"]["fresh"]["reclaimed_stale"]
    try:
        assert sup.run_item() is not None
        item = board.get(item_id)
        stats = board.stats(now=now)
    finally:
        board.close()
    # FIFO dispatch takes the older waiting item either way.
    assert _claimed_roles(f, agent) == ["waiting"]
    ledger = _row(f, "board_run_namespaces", "run_id", lease)
    if side == "before":
        assert plain(item) == f.read["items"][item_id]
        assert ledger["reclaimed_at"] is None
        assert stats.reclaimed_stale == released_reclaims
        return
    assert (item.state, item.lease_run_id, item.claimed_at) == (WorkState.PLANNED, None, None)
    assert item.updated_at == now
    assert _history(f.read["transitions"][item_id]) + [
        ("running", "planned", "system", "stale-claim recovery")] == _history(
        [dict(r) for r in f.rows(
            "SELECT * FROM transitions WHERE item_id = ? ORDER BY id", item_id)])
    # v0.1.2 had no reclaimed_at column: HEAD added it and stamps it now.
    assert ledger["reclaimed_at"] == now and ledger["purged_at"] is None
    assert stats.reclaimed_stale == released_reclaims + 1


@pytest.mark.parametrize("release", RELEASES)
def test_a_reclaimed_released_item_counts_as_redispatched(release, frozen, pool):
    f = frozen(release, "running")
    item_id, _, claimed_at = _running_facts(f)
    assert f.read["stats"]["fresh"]["redispatched"] == []
    board = _open(f, Clock(claimed_at + LEASE))
    agent = FakeAgent("codex", output="ok")
    sup = _supervisor(board, agent)
    try:
        sup.run_item()
        sup.run_item()
        stats = board.stats(now=claimed_at + LEASE)
    finally:
        board.close()
    assert _claimed_roles(f, agent) == ["waiting", "killed"]
    assert stats.redispatched == [item_id]


def _result(agent_id: str = "codex") -> TaskResult:
    return TaskResult(task_id=uuid.uuid4(), agent_id=agent_id, status=TaskStatus.COMPLETED,
                      output="ok", exit_code=0, duration_seconds=0.1)


@pytest.mark.parametrize("release", RELEASES)
def test_apply_update_is_fenced_by_the_released_lease(release, frozen):
    f = frozen(release, "running")
    item_id, lease, claimed_at = _running_facts(f)
    board = _open(f, Clock(claimed_at + 5))
    try:
        before = _cells(f.db)
        _, applied = board.apply_update(item_id, {}, _result(), actor="codex", run_id="other-run")
        assert applied is False
        assert _cells(f.db) == before
        # The released run itself, had it come back, still owns the item.
        _, applied = board.apply_update(item_id, {}, _result(), actor="codex", run_id=lease)
        assert applied is True
        assert board.get(item_id).state == WorkState.REVIEW
    finally:
        board.close()


@pytest.mark.parametrize("release", RELEASES)
def test_a_crash_block_is_fenced_by_the_released_lease(release, frozen):
    f = frozen(release, "running")
    item_id, lease, claimed_at = _running_facts(f)
    board = _open(f, Clock(claimed_at + 5))
    try:
        before = _cells(f.db)
        board.transition(item_id, "blocked", actor="system", note="crash",
                         human_required=True, run_id="other-run")
        assert _cells(f.db) == before
        board.transition(item_id, "blocked", actor="system", note="crash",
                         human_required=True, run_id=lease)
        row = _row(f, "work_items", "id", item_id)
    finally:
        board.close()
    assert (row["state"], row["human_required"], row["lease_run_id"]) == ("blocked", 1, None)


@pytest.mark.parametrize("release", RELEASES)
def test_renew_claim_renews_only_the_released_lease(release, frozen):
    f = frozen(release, "running")
    item_id, lease, claimed_at = _running_facts(f)
    # The fixture was frozen at the claim, so only a later clock shows a renewal.
    assert f.frozen_at == claimed_at
    clock = Clock(claimed_at + 5)
    board = _open(f, clock)
    try:
        before = _cells(f.db)
        assert board.renew_claim(item_id, "other-run") is False
        assert _cells(f.db) == before
        assert board.renew_claim(item_id, lease) is True
        row = _row(f, "work_items", "id", item_id)
        assert (row["state"], row["lease_run_id"], row["claimed_at"]) == (
            "running", lease, claimed_at + 5)
        assert board.transitions(item_id) == f.read["transitions"][item_id]
        # Once reclaimed, the released lease renews nothing.
        clock.now = claimed_at + 5 + LEASE
        assert board.recover_stale_running() == 1
        assert board.renew_claim(item_id, lease) is False
    finally:
        board.close()


# ── the scratch GC over the released ledger ───────────────────────────────


def _runs_by_role(f: Frozen) -> dict[str, list[dict]]:
    roles = {i: r for r, i in f.items.items()}
    out: dict[str, list[dict]] = {}
    for row in _ledger(f):
        out.setdefault(roles[row["item_id"]], []).append(row)
    return out


def _collectible(f: Frozen, now: float) -> list[str]:
    board = _open(f, Clock(now))
    try:
        return board.collectible_board_run_ids()
    finally:
        board.close()


@pytest.mark.parametrize("release", RELEASES)
def test_gc_collects_only_the_released_runs_it_may(release, frozen):
    f = frozen(release, "lifecycle")
    f.board().close()
    runs = _runs_by_role(f)
    last = runs["signed-off"][0]
    assert last["purged_at"] is None
    purged = {r["run_id"] for rows in runs.values() for r in rows if r["purged_at"] is not None}
    assert _collectible(f, f.frozen_at) == [last["run_id"]]
    # A purged run is never collected again, however late.
    assert purged.isdisjoint(_collectible(f, f.frozen_at + 10**7))
    reclaimed = [r for r in runs["reclaimed"] if r["purged_at"] is None]
    if release == "v0.1.2":
        # v0.1.2 had no grace: its own GC purged both reclaimed runs.
        assert reclaimed == []
        return
    # From v0.2.0 a reclaimed run waits one lease timeout past reclaimed_at.
    (row,) = reclaimed
    grace_end = row["reclaimed_at"] + LEASE
    assert grace_end - LEASE == row["reclaimed_at"]  # the boundary is exact in floats
    assert _collectible(f, math.nextafter(grace_end, -math.inf)) == [last["run_id"]]
    assert _collectible(f, grace_end) == [row["run_id"], last["run_id"]]


@pytest.mark.parametrize("release", RELEASES)
def test_gc_waits_out_a_live_released_lease_and_its_reclaim_grace(release, frozen):
    f = frozen(release, "running")
    _, lease, claimed_at = _running_facts(f)
    assert _collectible(f, claimed_at + 10**7) == []  # still leased: never collected
    board = _open(f, Clock(claimed_at + LEASE))
    try:
        assert board.recover_stale_running() == 1
    finally:
        board.close()
    reclaimed_at = _row(f, "board_run_namespaces", "run_id", lease)["reclaimed_at"]
    grace_end = reclaimed_at + LEASE
    assert grace_end - LEASE == reclaimed_at
    assert _collectible(f, math.nextafter(grace_end, -math.inf)) == []
    assert _collectible(f, grace_end) == [lease]


def _gc(f: Frozen, now: float, memory: FakeMemory) -> None:
    board = _open(f, Clock(now))
    try:
        _supervisor(board, FakeAgent("codex"), memory)._gc_board_run_namespaces(board)
    finally:
        board.close()


@pytest.mark.parametrize("release", RELEASES)
def test_gc_schedules_then_clears_a_failed_purge_of_a_released_run(release, frozen):
    f = frozen(release, "lifecycle")
    f.board().close()
    target = _runs_by_role(f)["signed-off"][0]["run_id"]
    others = [r for r in _ledger(f) if r["run_id"] != target]
    memory = FakeMemory()
    memory.delete_fail_ids = {target}
    t = f.frozen_at
    _gc(f, t, memory)
    assert memory.purged == [[target]]
    row = _row(f, "board_run_namespaces", "run_id", target)
    # v0.1.2 had neither column: HEAD added them and records the failure there.
    assert (row["purged_at"], row["failed_attempts"], row["next_attempt_at"]) == (
        None, 1, t + FIRST_RETRY)
    assert _collectible(f, math.nextafter(t + FIRST_RETRY, -math.inf)) == []
    assert _collectible(f, t + FIRST_RETRY) == [target]
    memory.delete_fail_ids = set()
    _gc(f, t + FIRST_RETRY, memory)
    row = _row(f, "board_run_namespaces", "run_id", target)
    assert (row["purged_at"], row["failed_attempts"], row["next_attempt_at"]) == (
        t + FIRST_RETRY, None, None)
    assert [r for r in _ledger(f) if r["run_id"] != target] == others


@pytest.mark.parametrize("release", RELEASES)
def test_gc_with_memory_unavailable_leaves_the_released_ledger_alone(release, frozen):
    f = frozen(release, "lifecycle")
    f.board().close()
    target = _runs_by_role(f)["signed-off"][0]["run_id"]
    before = _cells(f.db)
    memory = FakeMemory()
    memory.delete_unavailable = True
    _gc(f, f.frozen_at, memory)
    assert memory.purged == [[target]]  # the sweep reached the backend
    assert _cells(f.db) == before


# ── board compact over released history ───────────────────────────────────


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_compact_folds_released_history_without_changing_stats(release, scenario, frozen):
    f = frozen(release, scenario)
    f.board().close()
    t = f.frozen_at
    old = f.rows("SELECT * FROM transitions WHERE at < ?", t)
    kept = [dict(r) for r in f.rows("SELECT * FROM transitions WHERE at >= ? ORDER BY id", t)]
    rollups = {r["item_id"]: r["claim_count"] for r in f.rows("SELECT * FROM transition_rollups")}
    counters = {r["name"]: r["value"] for r in f.rows("SELECT * FROM transition_counters")}
    for r in old:
        if (r["from_state"], r["to_state"]) == ("planned", "running"):
            rollups[r["item_id"]] = rollups.get(r["item_id"], 0) + 1
    reclaims = sum(1 for r in old
                   if (r["to_state"], r["note"]) == ("planned", "stale-claim recovery"))
    if reclaims:
        counters["reclaimed_stale"] = counters.get("reclaimed_stale", 0) + reclaims
    f.clock(t)
    done = f.cli("board", "compact", "--older-than", "0")
    assert done.exit_code == 0, done.output
    assert done.output == f"Compacted {len(old)} audit row(s)\n"
    assert len(old) == {"lifecycle": 44, "compacted": 5, "running": 2, "crash-blocked": 5}[scenario]
    assert [dict(r) for r in f.rows("SELECT * FROM transitions ORDER BY id")] == kept
    assert {r["item_id"]: r["claim_count"]
            for r in f.rows("SELECT * FROM transition_rollups")} == rollups
    assert {r["name"]: r["value"] for r in f.rows("SELECT * FROM transition_counters")} == counters
    board = f.board()
    try:
        for label, now in (("fresh", t), ("aged", t + f.stall + 1)):
            assert plain(board.stats(now=now)) == f.read["stats"][label], label
    finally:
        board.close()
    # A second compaction folds nothing twice.
    folded = _cells(f.db)
    again = f.cli("board", "compact", "--older-than", "0")
    assert again.output == "Compacted 0 audit row(s)\n"
    assert _cells(f.db) == folded
    board = f.board()
    try:
        for label, now in (("fresh", t), ("aged", t + f.stall + 1)):
            assert plain(board.stats(now=now)) == f.read["stats"][label], label
    finally:
        board.close()


@pytest.mark.parametrize("release", RELEASES)
def test_compact_between_two_released_claims_keeps_both_halves(release, frozen):
    f = frozen(release, "lifecycle")
    f.board().close()
    item_id = f.items["reclaimed"]
    claims = [r["at"] for r in f.rows(
        "SELECT at FROM transitions WHERE item_id = ? AND from_state = 'planned' "
        "AND to_state = 'running' ORDER BY id", item_id)]
    assert len(claims) == 2  # killed, then claimed again after recovery
    cutoff = (claims[0] + claims[1]) / 2
    kept = [dict(r) for r in f.rows("SELECT * FROM transitions WHERE at >= ? ORDER BY id", cutoff)]
    board = f.board()
    try:
        board.compact_transitions(older_than_seconds=0, now=cutoff)
        assert [dict(r) for r in f.rows("SELECT * FROM transitions ORDER BY id")] == kept
        assert plain(board.stats(now=f.frozen_at)) == f.read["stats"]["fresh"]
    finally:
        board.close()
    # The released item's history now starts after the cutoff.
    shown = f.cli("board", "show", item_id).output.splitlines()
    history = shown[shown.index("history:") + 1:]
    expected = [r for r in f.read["transitions"][item_id] if r["at"] >= cutoff]
    assert expected and len(expected) < len(f.read["transitions"][item_id])
    assert history == [_printable(line) for line in _show_lines(
        f.read["items"][item_id], expected)[-len(expected):]]


# ── the gateway, dashboard and TUI readers ────────────────────────────────


def _call(mcp, name: str, args: dict) -> str:
    return run_async(mcp.call_tool(name, args)).structured_content["result"]


def _board_rows(items: list[dict]) -> list[str]:
    """Every `board list` row for these released items, written from their values."""
    rows = []
    for item in items:
        flags = (["human-required"] if item["human_required"] else []) + (
            [f"deps={len(item['depends_on'])}"] if item["depends_on"] else [])
        goal = f"[{item['goal']}] " if item["goal"] else ""
        task = item["task"] if len(item["task"]) <= 60 else item["task"][:57] + "..."
        flag = f"  ({', '.join(flags)})" if flags else ""
        rows.append(f"  {item['id']}  {item['state']:<8}  {goal}{task}{flag}")
    return rows


def _assert_stats_lines(lines: list[str], expected: dict, stall: int) -> None:
    """The literal `render_board_stats` lines, written from the release's read."""
    assert lines[0] == f"items: {expected['total']}"
    by_state = "  ".join(f"{s}={n}" for s, n in sorted(expected["by_state"].items()))
    assert f"  by state:  {by_state}" in lines
    assert f"  reclaimed (stale-lease):  {expected['reclaimed_stale']}" in lines
    for title, key in (("redispatched (work redone)", "redispatched"),
                       ("false-done (done, unverified)", "false_done"),
                       (f"blocked > {stall}s", "blocked_aged"),
                       (f"human-required > {stall}s", "human_required_aged"),
                       (f"dropped handoffs > {stall}s", "dropped_handoffs")):
        (line,) = [x for x in lines if x.startswith(f"  {title}:  ")]
        assert line.split(":  ")[1].split()[0] == str(len(expected[key])), line
        for item_id in expected[key]:
            assert item_id in line, title


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_gateway_board_tools_read_the_released_board(release, scenario, frozen):
    f = frozen(release, scenario)
    board = f.board()
    try:
        mcp = build_gateway(make_memory(FakeBackend()), board=board)
        newest = [f.read["items"][i] for i in reversed(f.read["order"])]

        def listed(**args) -> list[str] | str:
            out = _call(mcp, "board_list", args)
            return out if out == "No board items match." else out.splitlines()

        assert listed() == _board_rows(newest)
        for limit in (1, 2):
            assert listed(limit=limit) == _board_rows(newest[:limit]), limit
        for state in WorkState:
            want = [i for i in newest if i["state"] == state.value]
            assert listed(state=state.value) == (
                _board_rows(want) if want else "No board items match."), state
        for goal in sorted({i["goal"] for i in newest}):
            assert listed(goal=goal) == _board_rows(
                [i for i in newest if i["goal"] == goal]), goal
        newest = [i["id"] for i in newest]
        for item_id in newest:
            shown = _call(mcp, "board_get", {"item_id": item_id})
            assert shown.splitlines() == [_printable(line) for line in _show_lines(
                f.read["items"][item_id], f.read["transitions"][item_id])], item_id
        _assert_stats_lines(_call(mcp, "board_stats", {}).splitlines(),
                            f.read["stats"]["fresh"], f.stall)
    finally:
        board.close()


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_dashboard_snapshot_reads_the_released_board(release, scenario, frozen):
    f = frozen(release, scenario)
    board = f.board()
    try:
        expected = [f.read["items"][i] for i in f.read["order"]]
        for item_id in f.read["order"]:
            snap = take_snapshot(board, selected_id=item_id, now=lambda: 0.0)
            assert plain(snap.items) == expected
            assert plain(snap.stats) == f.read["stats"]["fresh"]
            assert snap.decisions_count == len(f.read["active_decisions"]["*"])
            assert plain(snap.detail) == f.read["items"][item_id]
            assert snap.detail_transitions == f.read["transitions"][item_id]
        for state in WorkState:
            snap = take_snapshot(board, state=state.value, now=lambda: 0.0)
            assert plain(snap.items) == [i for i in expected if i["state"] == state.value]
    finally:
        board.close()


def _table_cells(item: dict) -> list[str]:
    flags = "H!" if item["human_required"] else ""
    if item["depends_on"]:
        flags += f" deps={len(item['depends_on'])}"
    return [item["id"][:8], item["state"], item["goal"] or "-", item["task"][:50],
            item["owner_agent"] or "-", flags.strip()]


def _shown(app, selector: str) -> str:
    """What the widget displays (not the app's cache of what it last sent)."""
    return str(app.query_one(selector).render())


async def _settle(pilot, ready, *, tries: int = 200) -> None:
    for _ in range(tries):
        await pilot.pause(0)
        if ready():
            return
    raise AssertionError("the TUI never reached the expected state")


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_tui_board_tab_shows_the_released_board(release, scenario, frozen, tmp_path):
    f = frozen(release, scenario)
    order = f.read["order"]

    async def scenario_():
        board = f.board()
        app = SyncmillTuiApp(
            SyncmillConfig(board={"enabled": True, "db_path": str(f.db)},
                           tui={"state_dir": str(tmp_path / "jobs")}),
            board=board, board_error=None, toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600, initial_tab="board",
        )
        try:
            async with app.run_test() as pilot:
                table = app.query_one("#board-table", DataTable)
                await _settle(pilot, lambda: table.row_count == len(order))
                assert [str(k.value) for k in table.rows] == order
                for item_id in order:
                    assert [str(c) for c in table.get_row(item_id)] == \
                        _table_cells(f.read["items"][item_id]), item_id
                status = (f"items:{len(order)} | filter:all | "
                          f"decisions:{len(f.read['active_decisions']['*'])}")
                await _settle(pilot, lambda: _shown(app, "#board-status") == status)
                for row, item_id in enumerate(order):
                    table.move_cursor(row=row)
                    expected = [_printable(line) for line in _show_lines(
                        f.read["items"][item_id], f.read["transitions"][item_id])]
                    await _settle(pilot, lambda: _shown(
                        app, "#board-detail").splitlines() == expected)
        finally:
            board.close()

    asyncio.run(scenario_())


# ── a lesson promoted onto a released board ───────────────────────────────


_PROVENANCE = {"room_id": "room-1", "judgment_id": "judgment-1", "verdict": "accepted"}


@pytest.mark.parametrize("release", RELEASES)
def test_a_lesson_on_a_released_board_leaves_the_released_decisions_unbound(release, frozen):
    f = frozen(release, "lifecycle")
    f.board().close()
    released = {r["id"]: dict(r) for r in f.rows("SELECT * FROM decisions")}
    t = f.frozen_at + 10
    board = _open(f, Clock(t))
    try:
        lesson, created = board.promote_lesson(
            "Prefer small diffs.", project_id="P", provenance=_PROVENANCE,
            source_key="room:room-1:lesson", scope="backend")
        assert created is True
        row = _row(f, "decisions", "id", lesson.id)
        assert {k: row[k] for k in ("text", "scope", "created_at", "superseded_by",
                                    "superseded_at", "project_id", "source_key")} == {
            "text": "Prefer small diffs.", "scope": "backend", "created_at": t,
            "superseded_by": None, "superseded_at": None, "project_id": "P",
            "source_key": "room:room-1:lesson"}
        assert json.loads(row["provenance_json"]) == _PROVENANCE
        for decision_id, cells in released.items():
            assert _row(f, "decisions", "id", decision_id) == cells, decision_id
        # Every field, written from the release's read and the literals above.
        backend = [{**d, "project_id": None, "provenance": None}
                   for d in f.read["active_decisions"]["backend"]]
        promoted = {"id": lesson.id, "text": "Prefer small diffs.", "scope": "backend",
                    "created_at": t, "superseded_by": None, "superseded_at": None,
                    "project_id": "P", "provenance": _PROVENANCE}
        assert plain(board.active_decisions(goal="backend", project_id="P")) == \
            backend + [promoted]
        assert plain(board.active_decisions(goal="backend", project_id="Q")) == backend
        assert board.project_lessons("P") == []  # a goal-scoped lesson
        assert plain(board.project_lessons("P", goal="backend")) == [promoted]
        assert board.project_lessons("Q", goal="backend") == []
    finally:
        board.close()


@pytest.mark.parametrize("release", RELEASES)
def test_a_lesson_cannot_supersede_a_released_decision(release, frozen):
    f = frozen(release, "lifecycle")
    f.board().close()
    scopes = {d["id"]: d["scope"] for d in f.read["decisions_mapped"]}
    board = _open(f, Clock(f.frozen_at + 10))
    try:
        before = _cells(f.db)
        attempts = [(role, scopes[f.decisions[role]])
                    for role in ("global", "replacement", "scoped")]
        assert [s for _, s in attempts] == ["", "", "backend"]
        for role, scope in attempts:
            target = f.decisions[role]
            with pytest.raises(BoardError) as refused:
                board.promote_lesson("x", project_id="P", provenance=_PROVENANCE,
                                     source_key=f"key-{role}", scope=scope, supersedes=target)
            assert str(refused.value) == (
                f"cannot supersede {target}: it belongs to a different project — "
                "supersede only retires a decision of the same project")
            assert _cells(f.db) == before, role
        # The scope is checked before the project.
        target = f.decisions["global"]
        with pytest.raises(BoardError) as refused:
            board.promote_lesson("x", project_id="P", provenance=_PROVENANCE,
                                 source_key="key-cross", scope="backend", supersedes=target)
        assert str(refused.value) == (
            f"cannot supersede {target}: it is scoped to '', not 'backend' — "
            "supersede only retires a decision in the same scope")
        assert _cells(f.db) == before
    finally:
        board.close()
