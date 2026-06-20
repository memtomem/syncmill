"""supervisor.py — RouteStrategy, Supervisor.run lifecycle, graceful memory
degradation, the unbound-result fix, and git change detection."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from memtomem.constants import SHARED_NAMESPACE

import syncmill.supervisor as S
from syncmill.config import MemoryConfig, SyncmillConfig
from syncmill.memory import SyncmillMemory
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.supervisor import (
    CompeteStrategy,
    PipelineStrategy,
    RouteStrategy,
    Supervisor,
    _aggregate_failure,
    _git_changed_files,
    _make_strategy,
    _select_winner,
    available_strategies,
)

from conftest import (
    FakeAgent,
    FakeMemory,
    FakeRegistry,
    FakeStore,
    FakeWorktreePool,
    make_memory,
)


# ── RouteStrategy ────────────────────────────────────────────────────────


def _run_route(agents, memory, *, config=None, task=None, pool=None):
    return RouteStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        pool=pool or FakeWorktreePool(),
        config=config or SyncmillConfig(),
    )


def test_route_enriches_prompt_with_context():
    agent = FakeAgent("claude")
    _run_route([agent], FakeMemory(context="prior knowledge"))
    sent = agent.calls[0]["prompt"]
    assert sent == "Context from previous work:\nprior knowledge\n\n---\n\nTask: do it"


def test_route_no_context_passes_raw_prompt():
    agent = FakeAgent("claude")
    _run_route([agent], FakeMemory(context=""))
    assert agent.calls[0]["prompt"] == "do it"


def test_route_falls_back_to_second_agent():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="nope")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="win")
    r = _run_route([a, b], FakeMemory())
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_route_short_circuits_on_first_success():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex")
    _run_route([a, b], FakeMemory())
    assert b.calls == []


def test_route_all_fail_aggregates():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="auth")
    b = FakeAgent("codex", status=TaskStatus.FAILED, error=None, exit_code=3)
    r = _run_route([a, b], FakeMemory())
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "none"
    assert r.error.startswith("All agents failed:")
    assert "[claude] auth" in r.error
    assert "[codex] exit_code=3" in r.error


def test_route_passes_per_agent_timeout():
    cfg = SyncmillConfig()
    cfg.claude.timeout_seconds = 7
    agent = FakeAgent("claude")
    _run_route([agent], FakeMemory(), config=cfg)
    assert agent.calls[0]["timeout"] == 7


def test_aggregate_failure_sums_durations():
    errs = [
        TaskResult(Task(prompt="").id, "claude", TaskStatus.FAILED, "", 1, 2.0, error="a"),
        TaskResult(Task(prompt="").id, "codex", TaskStatus.FAILED, "", 1, 3.0, error="b"),
    ]
    r = _aggregate_failure(Task(prompt=""), errs)
    assert r.duration_seconds == 5.0


def test_route_survives_dead_memory():
    # real SyncmillMemory wrapping a fully-failing store must degrade, not raise
    mem = make_memory(FakeStore(raise_on={"search", "_ensure_init"}))
    agent = FakeAgent("claude", status=TaskStatus.COMPLETED)
    r = _run_route([agent], mem)
    assert r.status is TaskStatus.COMPLETED
    assert agent.calls[0]["prompt"] == "do it"  # no context injected
    assert mem.degraded is True


def test_route_acquires_isolated_worktree_per_agent():
    # each fallback attempt gets its OWN worktree — no shared workspace
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="x")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    pool = FakeWorktreePool()
    _run_route([a, b], FakeMemory(), pool=pool)
    assert pool.acquired == ["claude", "codex"]
    assert a.calls[0]["workdir"] != b.calls[0]["workdir"]  # distinct worktrees


def test_route_short_circuit_acquires_only_run_agents():
    # an agent that never runs (first wins) never gets a worktree
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex")
    pool = FakeWorktreePool()
    _run_route([a, b], FakeMemory(), pool=pool)
    assert pool.acquired == ["claude"]


def test_route_winner_files_changed_from_its_own_worktree(monkeypatch):
    # the winner's files_changed reflect ITS worktree only — a failed agent's
    # partial edits in a different worktree never leak in.
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="x")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr(
        S, "_git_changed_files",
        lambda p: {"codex_only.py"} if "codex" in str(p) else {"claude_partial.py"},
    )
    r = _run_route([a, b], FakeMemory(), pool=pool)
    assert r.files_changed == ["codex_only.py"]


# ── PipelineStrategy ─────────────────────────────────────────────────────


def _run_pipeline(agents, memory, *, config=None, task=None, pool=None):
    return PipelineStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        pool=pool or FakeWorktreePool(),
        config=config or SyncmillConfig(),
    )


def test_pipeline_runs_all_stages_in_order():
    a = FakeAgent("claude", output="s1")
    b = FakeAgent("codex", output="s2")
    r = _run_pipeline([a, b], FakeMemory())
    assert len(a.calls) == 1 and len(b.calls) == 1  # every stage runs
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"  # the chain's product is the last stage
    assert r.output == "s2"


def test_pipeline_handoff_via_shared_memory():
    # stage 1's output must reach stage 2 *through shared memory*, never threaded.
    mem = FakeMemory(echo_shared=True)
    a = FakeAgent("claude", output="STAGE1-OUTPUT")
    b = FakeAgent("codex", output="STAGE2-OUTPUT")
    r = _run_pipeline([a, b], mem)
    # stage 1 saw nothing yet → raw prompt
    assert a.calls[0]["prompt"] == "do it"
    # stage 2's prompt was enriched with stage 1's shared output
    assert b.calls[0]["prompt"].startswith("Context from previous work:")
    assert "STAGE1-OUTPUT" in b.calls[0]["prompt"]
    assert r.output == "STAGE2-OUTPUT"
    # only stage 1 was shared by the strategy (it feeds stage 2); the final
    # stage is left for the Supervisor to share — no duplicate.
    assert [res.agent_id for _, res in mem.shared] == ["claude"]


def test_pipeline_enriches_from_real_shared_memory():
    # real SyncmillMemory + FakeStore: proves the actual search_context →
    # _enrich_prompt plumbing, not just the FakeMemory shortcut.
    chunk = {"id": "c1", "score": 0.9, "content": "prior pipeline output", "source": "codex"}
    mem = make_memory(FakeStore(search_results={SHARED_NAMESPACE: [chunk]}))
    a, b = FakeAgent("claude"), FakeAgent("codex")
    _run_pipeline([a, b], mem)
    for agent in (a, b):
        assert agent.calls[0]["prompt"].startswith("Context from previous work:")
        assert "prior pipeline output" in agent.calls[0]["prompt"]


def test_pipeline_fails_fast_on_first_stage_failure():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="boom",
                  output="partial work", exit_code=7)
    b = FakeAgent("codex", output="never")
    r = _run_pipeline([a, b], FakeMemory())
    assert r.status is TaskStatus.FAILED
    assert b.calls == []  # chain stopped — stage 2 never ran
    assert r.agent_id == "claude"
    assert r.error == "Pipeline failed at stage 1/2 (claude): boom"
    # the broken stage's output + exit_code propagate to the failure result
    assert r.output == "partial work"
    assert r.exit_code == 7


def test_pipeline_fail_fast_after_partial_progress():
    a = FakeAgent("claude", output="ok")
    b = FakeAgent("codex", status=TaskStatus.FAILED, exit_code=2, output="oops")
    mem = FakeMemory()
    r = _run_pipeline([a, b], mem)
    assert r.status is TaskStatus.FAILED
    assert r.error == "Pipeline failed at stage 2/2 (codex): exit_code=2"
    assert r.output == "oops" and r.exit_code == 2  # failed stage's fields kept
    # stage 1 completed and was shared (it had a successor to feed)
    assert [res.agent_id for _, res in mem.shared] == ["claude"]


def test_pipeline_middle_stage_failure_in_three_stage():
    # the case that proves fail-fast AND the `if stage != last` share boundary AND
    # the failure-path duration sum, all at once.
    a = FakeAgent("claude", output="s1", duration=1.5)
    b = FakeAgent("codex", status=TaskStatus.FAILED, error="mid",
                  output="s2partial", exit_code=4, duration=2.25)
    c = FakeAgent("gemini", output="s3", duration=0.3)  # never reached
    mem = FakeMemory()
    r = _run_pipeline([a, b, c], mem)
    assert r.status is TaskStatus.FAILED
    assert c.calls == []  # stage 3 never ran
    assert r.error == "Pipeline failed at stage 2/3 (codex): mid"
    assert r.output == "s2partial" and r.exit_code == 4
    assert r.duration_seconds == 3.75  # 1.5 + 2.25 attempted; c's 0.3 excluded
    # stage 1 shared (it had a successor to feed); the failed stage 2 was NOT
    assert [res.agent_id for _, res in mem.shared] == ["claude"]


def test_pipeline_single_stage_is_not_shared_by_strategy():
    # a 1-stage pipeline has no successor → the strategy shares nothing;
    # the Supervisor shares the final result.
    mem = FakeMemory()
    a = FakeAgent("claude", output="solo")
    r = _run_pipeline([a], mem)
    assert r.status is TaskStatus.COMPLETED and r.output == "solo"
    assert mem.shared == []


def test_pipeline_reports_total_duration():
    # distinct per-stage durations prove a real sum (+ rounding), not a formula
    # that happens to match uniform values.
    a = FakeAgent("claude", duration=1.5)
    b = FakeAgent("codex", duration=2.25)
    c = FakeAgent("gemini", duration=0.3)
    r = _run_pipeline([a, b, c], FakeMemory())
    assert r.duration_seconds == 4.05


def test_pipeline_passes_per_agent_timeout():
    cfg = SyncmillConfig()
    cfg.claude.timeout_seconds = 11
    cfg.codex.timeout_seconds = 22
    a, b = FakeAgent("claude"), FakeAgent("codex")
    _run_pipeline([a, b], FakeMemory(), config=cfg)
    assert a.calls[0]["timeout"] == 11
    assert b.calls[0]["timeout"] == 22


def test_pipeline_survives_dead_memory():
    # a fully-failing memtomem must not abort the chain — stages just lose context
    mem = make_memory(
        FakeStore(raise_on={"_ensure_init", "search", "add", "log_event"})
    )
    a = FakeAgent("claude", output="s1")
    b = FakeAgent("codex", output="s2")
    r = _run_pipeline([a, b], mem)
    assert r.status is TaskStatus.COMPLETED and r.output == "s2"
    assert a.calls[0]["prompt"] == "do it"  # no context injected
    assert b.calls[0]["prompt"] == "do it"  # share failed, so stage 2 got nothing
    assert mem.degraded is True


def test_pipeline_no_agents_is_defensive_failure():
    # the Supervisor guards this, but the strategy must not raise standalone
    r = _run_pipeline([], FakeMemory())
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "none"
    assert r.error == "No agents available"


def test_pipeline_shares_single_worktree_across_stages():
    # unlike Route, a pipeline acquires ONE worktree (keyed off stage 1) that
    # every stage edits, so the file handoff works.
    a = FakeAgent("claude", output="s1")
    b = FakeAgent("codex", output="s2")
    pool = FakeWorktreePool()
    _run_pipeline([a, b], FakeMemory(), pool=pool)
    assert pool.acquired == ["claude"]  # one shared worktree, not one per stage
    assert a.calls[0]["workdir"] == b.calls[0]["workdir"]  # same tree for both


def test_pipeline_sets_files_changed_on_final(monkeypatch):
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr(S, "_git_changed_files", lambda p: {"built.py"})
    a = FakeAgent("claude", output="s1")
    b = FakeAgent("codex", output="s2")
    r = _run_pipeline([a, b], FakeMemory(), pool=pool)
    assert r.files_changed == ["built.py"]


# ── CompeteStrategy ──────────────────────────────────────────────────────


def _run_compete(agents, memory, *, config=None, task=None, pool=None):
    return CompeteStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        pool=pool or FakeWorktreePool(),
        config=config or SyncmillConfig(),
    )


def test_compete_runs_all_agents_in_isolated_worktrees():
    # every agent runs (no Route-style short-circuit) and each gets its OWN
    # worktree — unlike Pipeline's single shared tree.
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="A")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="B")
    pool = FakeWorktreePool()
    _run_compete([a, b], FakeMemory(), pool=pool)
    assert len(a.calls) == 1 and len(b.calls) == 1  # both ran
    assert pool.acquired == ["claude", "codex"]  # one worktree per agent
    assert a.calls[0]["workdir"] != b.calls[0]["workdir"]  # distinct trees


def test_compete_winner_is_first_completed_in_priority_order():
    # a higher-priority agent that FAILS yields to a lower-priority one that
    # completed — but both actually ran (Route would have short-circuited).
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="nope")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="win")
    r = _run_compete([a, b], FakeMemory())
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_compete_winner_is_priority_not_duration():
    # DETERMINISM: the higher-priority agent wins even when it is the SLOWER one —
    # selection must never prefer a shorter duration (nondeterministic wall-clock).
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="slow", duration=9.0)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="fast", duration=0.01)
    r = _run_compete([a, b], FakeMemory())
    assert r.agent_id == "claude"  # priority wins, not the faster codex
    assert r.output == "slow"


def test_compete_runs_agents_concurrently():
    # The headline property: compete actually OVERLAPS agents (max_active == N),
    # not just runs them all. A serialization regression in the fan-out must fail
    # here — a plain sequential loop would never reach max_active > 1. Uses a
    # probe agent (the strategy drives the async path, so execute() must NOT run).
    state = {"active": 0, "max_active": 0}

    class _ProbeAgent:
        def __init__(self, agent_id):
            self._agent_id = agent_id
            self.calls: list[dict] = []

        @property
        def agent_id(self) -> str:
            return self._agent_id

        def execute(self, prompt, workdir, timeout):  # pragma: no cover
            raise AssertionError("compete must run agents via aexecute, not execute")

        async def aexecute(self, prompt, workdir, timeout):
            self.calls.append({"prompt": prompt, "workdir": workdir, "timeout": timeout})
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.02)  # yield so the others enter too
            state["active"] -= 1
            return TaskResult(
                uuid.uuid4(), self._agent_id, TaskStatus.COMPLETED, "ok", 0, 0.02
            )

    agents = [_ProbeAgent("claude"), _ProbeAgent("gemini"), _ProbeAgent("codex")]
    r = _run_compete(agents, FakeMemory())
    assert state["max_active"] == 3  # all three were inside aexecute at once
    assert r.agent_id == "claude"  # first completed in priority order


def test_compete_winner_pairing_survives_out_of_order_completion(monkeypatch):
    # the priority winner finishes AFTER a faster loser (adelay), yet the strategy
    # still picks it AND pairs its OWN worktree's diff — positional alignment must
    # survive completion-order reordering at the strategy seam, not just in gather.
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="slow-winner", adelay=0.03)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="fast-loser", adelay=0.0)
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr(
        S, "_git_changed_files",
        lambda p: {"claude_file.py"} if "claude" in str(p) else {"codex_file.py"},
    )
    r = _run_compete([a, b], FakeMemory(), pool=pool)
    assert r.agent_id == "claude"  # priority winner, despite finishing last
    assert r.output == "slow-winner"
    assert r.files_changed == ["claude_file.py"]  # the winner's OWN worktree


def test_compete_all_fail_duration_is_max_not_sum():
    # agents ran concurrently, so the all-failed wall-clock is the SLOWEST one,
    # not the sum (which would overstate it, unlike sequential Route).
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="a", duration=2.0)
    b = FakeAgent("codex", status=TaskStatus.FAILED, error="b", duration=5.0)
    r = _run_compete([a, b], FakeMemory())
    assert r.status is TaskStatus.FAILED
    assert r.duration_seconds == 5.0  # max(2.0, 5.0), not 7.0


def test_compete_all_fail_aggregates():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="auth")
    b = FakeAgent("codex", status=TaskStatus.FAILED, error=None, exit_code=3)
    r = _run_compete([a, b], FakeMemory())
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "none"
    assert r.error.startswith("All agents failed:")
    assert "[claude] auth" in r.error
    assert "[codex] exit_code=3" in r.error


def test_compete_winner_files_changed_from_its_own_worktree(monkeypatch):
    # the winner's files_changed reflect ITS worktree only — a loser's diff in a
    # different worktree never leaks in.
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="x")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr(
        S, "_git_changed_files",
        lambda p: {"codex_only.py"} if "codex" in str(p) else {"claude_partial.py"},
    )
    r = _run_compete([a, b], FakeMemory(), pool=pool)
    assert r.files_changed == ["codex_only.py"]


def test_compete_non_git_degrades_cleanly():
    # tempdir fallback: no diffing → first-completed-by-priority still wins,
    # files_changed empty.
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="A")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="B")
    r = _run_compete([a, b], FakeMemory(), pool=FakeWorktreePool(is_git=False))
    assert r.agent_id == "claude"
    assert r.files_changed == []


def test_compete_enriches_prompt_with_context():
    a = FakeAgent("claude")
    _run_compete([a], FakeMemory(context="prior knowledge"))
    sent = a.calls[0]["prompt"]
    assert sent == "Context from previous work:\nprior knowledge\n\n---\n\nTask: do it"


def test_compete_passes_per_agent_timeout():
    # the timeout_for closure routes each agent's configured timeout correctly
    cfg = SyncmillConfig()
    cfg.claude.timeout_seconds = 11
    cfg.codex.timeout_seconds = 22
    a, b = FakeAgent("claude"), FakeAgent("codex")
    _run_compete([a, b], FakeMemory(), config=cfg)
    assert a.calls[0]["timeout"] == 11
    assert b.calls[0]["timeout"] == 22


def test_compete_survives_dead_memory():
    # a fully-failing memtomem must degrade, not abort the head-to-head
    mem = make_memory(FakeStore(raise_on={"search", "_ensure_init"}))
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    r = _run_compete([a], mem)
    assert r.status is TaskStatus.COMPLETED
    assert a.calls[0]["prompt"] == "do it"  # no context injected
    assert mem.degraded is True


def test_compete_no_agents_is_defensive_failure():
    # the Supervisor guards this, but the strategy must not raise standalone
    r = _run_compete([], FakeMemory())
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "none"
    assert r.error == "No agents available"


def test_select_winner_picks_first_completed_and_pairs_worktree():
    from conftest import FakeWorktree

    wts = [FakeWorktree(Path("/tmp/a")), FakeWorktree(Path("/tmp/b"))]
    results = [
        TaskResult(Task(prompt="").id, "claude", TaskStatus.FAILED, "", 1, 0.0),
        TaskResult(Task(prompt="").id, "codex", TaskStatus.COMPLETED, "ok", 0, 0.0),
    ]
    winner = _select_winner(wts, results)
    assert winner is not None
    res, wt = winner
    assert res.agent_id == "codex"  # first COMPLETED in order
    assert wt is wts[1]  # paired with ITS worktree, not the loser's


def test_select_winner_none_when_all_failed():
    from conftest import FakeWorktree

    wts = [FakeWorktree(Path("/tmp/a"))]
    results = [TaskResult(Task(prompt="").id, "claude", TaskStatus.FAILED, "", 1, 0.0)]
    assert _select_winner(wts, results) is None


# ── Strategy selection ───────────────────────────────────────────────────


def test_available_strategies_sorted():
    assert available_strategies() == ["compete", "pipeline", "route"]


def test_make_strategy_selects_each():
    assert isinstance(_make_strategy("route"), RouteStrategy)
    assert isinstance(_make_strategy("pipeline"), PipelineStrategy)
    assert isinstance(_make_strategy("compete"), CompeteStrategy)


def test_make_strategy_unknown_raises():
    with pytest.raises(ValueError, match="Unknown strategy: 'nope'"):
        _make_strategy("nope")


@pytest.fixture
def stub_supervisor_deps(monkeypatch):
    """Stub the heavy deps so a real ``Supervisor()`` constructs without touching
    the real memtomem DB or agent binaries — exercises ``__init__`` wiring."""
    monkeypatch.setattr(S, "SyncmillMemory", lambda cfg: FakeMemory())
    monkeypatch.setattr(S, "AgentRegistry", lambda cfg: FakeRegistry([]))


def test_supervisor_selects_strategy_from_config(stub_supervisor_deps):
    assert isinstance(Supervisor(SyncmillConfig())._strategy, RouteStrategy)
    assert isinstance(
        Supervisor(SyncmillConfig(default_strategy="pipeline"))._strategy,
        PipelineStrategy,
    )


def test_supervisor_selects_strategy_from_env(stub_supervisor_deps, monkeypatch):
    monkeypatch.setenv("SYNCMILL_DEFAULT_STRATEGY", "pipeline")
    assert isinstance(Supervisor(SyncmillConfig())._strategy, PipelineStrategy)


def test_supervisor_rejects_unknown_strategy(stub_supervisor_deps):
    # the env/config path bypasses click.Choice, so the hard-fail lives here
    with pytest.raises(ValueError, match="Unknown strategy"):
        Supervisor(SyncmillConfig(default_strategy="bogus"))


# ── Supervisor.run ───────────────────────────────────────────────────────


def _supervisor(registry, memory, strategy=None, config=None):
    sup = Supervisor.__new__(Supervisor)
    sup._config = config or SyncmillConfig()
    sup._registry = registry
    sup._memory = memory
    sup._strategy = strategy or RouteStrategy()
    return sup


@pytest.fixture
def patched_worktree(monkeypatch):
    """Swap in a FakeWorktreePool for the one Supervisor.run builds.

    Real flow: ``WorktreePool(repo_root, task_id)`` → the strategy acquires per
    its topology → ``cleanup_all()`` in finally. The fake lets tests assert
    acquisition order + cleanup without real git; ``_git_changed_files`` is
    stubbed so the strategy's per-worktree diff is deterministic.
    """
    pool = FakeWorktreePool(is_git=True)
    created = {}

    def make_pool(repo_root, task_id):
        created["repo_root"] = repo_root
        created["task_id"] = task_id
        return pool

    monkeypatch.setattr(S, "WorktreePool", make_pool)
    monkeypatch.setattr(S, "_git_changed_files", lambda p: {"changed.py"})
    return {"pool": pool, "created": created}


def test_run_happy_path(patched_worktree):
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem)
    task = Task(prompt="go")
    r = sup.run(task)
    assert r.status is TaskStatus.COMPLETED
    assert "register:claude" in mem.events
    assert any(e.startswith("start:") for e in mem.events)
    assert any(e.startswith("end:") for e in mem.events)
    assert len(mem.shared) == 1
    assert patched_worktree["pool"].cleaned
    assert patched_worktree["pool"].acquired == ["claude"]
    assert patched_worktree["created"]["task_id"] == str(task.id)


def test_run_collects_files_changed_when_git_and_completed(patched_worktree):
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"))
    assert r.files_changed == ["changed.py"]


def test_run_with_pipeline_strategy_shares_final_and_collects_files(patched_worktree):
    mem = FakeMemory(echo_shared=True)
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", output="s1"), FakeAgent("codex", output="s2")]),
        mem,
        strategy=PipelineStrategy(),
    )
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"  # last stage is the chain's product
    assert r.files_changed == ["changed.py"]  # cumulative worktree diff
    # strategy shared stage 1; Supervisor shared the final stage → both, no dup
    assert [res.agent_id for _, res in mem.shared] == ["claude", "codex"]
    # every stage registered; the shared worktree is keyed off the first stage
    assert "register:claude" in mem.events and "register:codex" in mem.events
    assert patched_worktree["pool"].acquired == ["claude"]  # one shared worktree


def test_run_with_pipeline_strategy_failure_skips_files_and_final_share(patched_worktree):
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", output="s1"),
                      FakeAgent("codex", status=TaskStatus.FAILED, error="x")]),
        mem,
        strategy=PipelineStrategy(),
    )
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.FAILED
    assert r.files_changed == []  # Supervisor only diffs on COMPLETED
    # only the strategy's stage-1 share; the Supervisor does NOT share the failed final
    assert [res.agent_id for _, res in mem.shared] == ["claude"]
    assert any(e.startswith("end:") for e in mem.events)  # session still ended
    assert patched_worktree["pool"].cleaned  # worktrees still cleaned up


def test_run_with_compete_strategy_shares_winner_and_cleans_all_worktrees(patched_worktree):
    # acceptance criteria end-to-end: 2 agents run head-to-head, the winner is
    # shared once by the Supervisor, files are collected, and ALL worktrees
    # (winner + loser) are created and cleaned up.
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", status=TaskStatus.FAILED, error="x"),
                      FakeAgent("codex", output="win")]),
        mem,
        strategy=CompeteStrategy(),
    )
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"  # higher-priority claude failed, codex wins
    assert r.files_changed == ["changed.py"]
    # the Supervisor shares the winner exactly once (compete itself shares nothing)
    assert [res.agent_id for _, res in mem.shared] == ["codex"]
    # both agents got a worktree (concurrent isolation) and all were cleaned up
    assert patched_worktree["pool"].acquired == ["claude", "codex"]
    assert patched_worktree["pool"].cleaned
    assert "register:claude" in mem.events and "register:codex" in mem.events


def test_run_no_files_changed_when_not_git(monkeypatch):
    pool = FakeWorktreePool(is_git=False)  # tempdir fallback → no diffing
    monkeypatch.setattr(S, "WorktreePool", lambda repo_root, task_id: pool)
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"))
    assert r.files_changed == []
    assert pool.cleaned


def test_run_failed_skips_share_but_cleans_up(patched_worktree):
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", status=TaskStatus.FAILED, error="x")]), mem
    )
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.FAILED
    assert mem.shared == []
    assert any(e.startswith("end:") for e in mem.events)
    assert patched_worktree["pool"].cleaned


def test_run_unknown_forced_agent(patched_worktree):
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"), agent_id="ghost")
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "ghost"
    assert "not found" in r.error
    assert patched_worktree["created"] == {}  # no worktree created


def test_run_no_agents_available(patched_worktree):
    sup = _supervisor(FakeRegistry([]), FakeMemory())
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.FAILED
    assert r.error == "No agents available"


def test_run_exclude_drops_agent_from_fallback(patched_worktree):
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", output="from-claude"),
                      FakeAgent("gemini", output="from-gemini")]),
        mem,
    )
    r = sup.run(Task(prompt="go"), exclude={"claude"})
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "gemini"  # claude (first priority) was excluded
    assert r.output == "from-gemini"
    assert patched_worktree["pool"].acquired == ["gemini"]  # only the survivor got a worktree
    # excluded agents are never registered in memory either
    assert "register:claude" not in mem.events
    assert "register:gemini" in mem.events


def test_run_all_agents_excluded(patched_worktree):
    # two excluded agents → exercises the sorted() multi-element error rendering
    sup = _supervisor(FakeRegistry([FakeAgent("gemini"), FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"), exclude={"gemini", "claude"})
    assert r.status is TaskStatus.FAILED
    assert r.error == "No agents available (excluded: claude, gemini)"  # sorted
    assert patched_worktree["created"] == {}  # no worktree created


def test_run_forced_agent_also_excluded_errors(patched_worktree):
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"), agent_id="claude", exclude={"claude"})
    assert r.status is TaskStatus.FAILED
    assert "both forced" in r.error
    assert patched_worktree["created"] == {}  # rejected before any workspace


def test_run_forced_agent_with_unrelated_exclude(patched_worktree):
    # forcing gemini while excluding a *different* agent (claude) is valid, not a conflict
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude"), FakeAgent("gemini", output="g"),
                      FakeAgent("codex")]),
        mem,
    )
    r = sup.run(Task(prompt="go"), agent_id="gemini", exclude={"claude"})
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "gemini"  # the guard only fires when the forced agent IS excluded
    assert "register:gemini" in mem.events and "register:claude" not in mem.events


def test_run_survives_dead_memory(patched_worktree):
    # the whole point: a broken memtomem must not crash the task
    mem = make_memory(
        FakeStore(raise_on={"_ensure_init", "start_session", "search", "add",
                            "log_event", "end_session"})
    )
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="done")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "done"
    assert mem.degraded is True


def test_run_survives_memtomem_construction_failure(monkeypatch, patched_worktree):
    # memtomem broken at construction time must still let the task run
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no memtomem")),
    )
    mem = SyncmillMemory(MemoryConfig())
    assert mem._store is None and mem.degraded is True
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="done")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "done"


def test_run_strategy_exception_does_not_mask_with_unbound_result(patched_worktree):
    class Boom:
        def execute(self, **kw):
            raise ValueError("strategy blew up")

    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), strategy=Boom())
    with pytest.raises(ValueError, match="strategy blew up"):
        sup.run(Task(prompt="go"))
    # finally still cleaned the worktrees, and did NOT raise UnboundLocalError
    assert patched_worktree["pool"].cleaned


def test_close_closes_memory():
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([]), mem)
    sup.close()
    assert "close" in mem.events


# ── _git_changed_files (real git) ────────────────────────────────────────


def test_git_changed_detects_modified_and_untracked(git_repo):
    (git_repo / "README.md").write_text("# changed\n")
    (git_repo / "new.txt").write_text("hi\n")
    changed = _git_changed_files(git_repo)
    assert "README.md" in changed
    assert "new.txt" in changed


def test_git_changed_clean_repo_empty(git_repo):
    assert _git_changed_files(git_repo) == set()


def test_git_changed_respects_gitignore(git_repo):
    (git_repo / ".gitignore").write_text("ignored.log\n")
    (git_repo / "ignored.log").write_text("secret\n")
    changed = _git_changed_files(git_repo)
    assert "ignored.log" not in changed


def test_git_changed_non_git_dir_empty(tmp_path):
    assert _git_changed_files(tmp_path) == set()
