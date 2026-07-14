"""supervisor.py — RouteStrategy, Supervisor.run lifecycle, graceful memory
degradation, the unbound-result fix, and git change detection."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import uuid
from pathlib import Path

import pytest

from memtomem.constants import SHARED_NAMESPACE

import syncmill.supervisor as S
from syncmill.config import (
    CouncilConfig,
    DecomposeConfig,
    MemoryConfig,
    SyncmillConfig,
)
from syncmill.memory import SyncmillMemory
from syncmill.models import (
    Task,
    TaskResult,
    TaskStatus,
    ToolCallRecord,
    ToolOutcome,
    ToolTelemetry,
)
from syncmill.strategies import (
    CompeteStrategy,
    CouncilStrategy,
    DecomposeStrategy,
    PipelineStrategy,
    RouteStrategy,
    _aggregate_failure,
    _git_changed_files,
    _git_diff_patch,
    _make_strategy,
    _select_winner,
    available_strategies,
)
from syncmill.supervisor import Supervisor

from syncmill.board import BoardError, Decision, WorkItem, WorkState

from conftest import (
    FakeAgent,
    FakeBoard,
    FakeBreaker,
    FakeGate,
    FakeMatcher,
    FakeMemory,
    FakeMonitor,
    FakeRegistry,
    FakeBackend,
    FakeTracer,
    FakeWorktreePool,
    make_memory,
)


# ── RouteStrategy ────────────────────────────────────────────────────────


def _run_route(
    agents, memory, *, config=None, task=None, pool=None, gate=None, breaker=None,
    monitor=None, tracer=None
):
    return RouteStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        pool=pool or FakeWorktreePool(),
        config=config or SyncmillConfig(),
        gate=gate,
        breaker=breaker,
        monitor=monitor,
        tracer=tracer,
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
    mem = make_memory(FakeBackend(raise_on={"search", "ensure_ready"}))
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
        "syncmill.strategies.base._git_changed_files",
        lambda p: {"codex_only.py"} if "codex" in str(p) else {"claude_partial.py"},
    )
    r = _run_route([a, b], FakeMemory(), pool=pool)
    assert r.files_changed == ["codex_only.py"]


# ── RouteStrategy + quality gate (#10) ───────────────────────────────────


def test_route_accepts_gate_passing_result():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="win")
    r = _run_route([a], FakeMemory(), gate=FakeGate(passes=True))
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "claude"
    assert r.gate is not None and r.gate.passed is True  # verdict attached


def test_route_falls_back_when_first_agent_fails_gate():
    # acceptance criterion: an agent whose diff fails the gate is NOT accepted;
    # Route falls back to the next agent — even though the first agent COMPLETED.
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="claude-work")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="codex-work")
    gate = FakeGate(by_agent={"claude": False, "codex": True})
    r = _run_route([a, b], FakeMemory(), gate=gate)
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"  # claude completed but failed the gate
    assert r.output == "codex-work"
    assert gate.evaluated == ["claude", "codex"]  # both diffs were gated, in order
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_route_all_fail_gate_aggregates_with_gate_errors():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="a")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="b")
    r = _run_route([a, b], FakeMemory(), gate=FakeGate(passes=False))
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "none"
    assert r.error.startswith("All agents failed:")
    # the gate failure (not a bogus exit_code) is what each line reports
    assert "[claude] quality gate failed" in r.error
    assert "[codex] quality gate failed" in r.error


def test_route_does_not_gate_a_failed_agent():
    # an agent that fails outright is never gated — nothing trustworthy to check.
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="crashed")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="ok")
    gate = FakeGate(passes=True)
    r = _run_route([a, b], FakeMemory(), gate=gate)
    assert r.agent_id == "codex"
    assert gate.evaluated == ["codex"]  # claude's failure skipped the gate


def test_route_aggregates_mixed_agent_failure_and_gate_failure():
    # claude fails outright (not gated); codex completes but fails the gate. The
    # aggregate names each distinct reason — an outright failure and a gate failure.
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="crashed")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="b")
    gate = FakeGate(by_agent={"codex": False})
    r = _run_route([a, b], FakeMemory(), gate=gate)
    assert r.status is TaskStatus.FAILED
    assert "[claude] crashed" in r.error
    assert "[codex] quality gate failed" in r.error
    assert gate.evaluated == ["codex"]  # only the completer was gated


def test_route_gate_failure_skips_files_changed(monkeypatch):
    # a rejected result must not carry a files_changed diff (it wasn't accepted)
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr("syncmill.strategies.base._git_changed_files", lambda p: {"should_not_appear.py"})
    r = _run_route([a], FakeMemory(), gate=FakeGate(passes=False), pool=pool)
    assert r.status is TaskStatus.FAILED
    assert r.files_changed == []


# ── PipelineStrategy ─────────────────────────────────────────────────────


def _run_pipeline(agents, memory, *, config=None, task=None, pool=None, gate=None, breaker=None, monitor=None, tracer=None):
    return PipelineStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        pool=pool or FakeWorktreePool(),
        config=config or SyncmillConfig(),
        gate=gate,
        breaker=breaker,
        monitor=monitor,
        tracer=tracer,
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
    # real SyncmillMemory + FakeBackend: proves the actual search_context →
    # _enrich_prompt plumbing, not just the FakeMemory shortcut.
    chunk = {"id": "c1", "score": 0.9, "content": "prior pipeline output", "source": "codex"}
    mem = make_memory(FakeBackend(search_results={SHARED_NAMESPACE: [chunk]}))
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
    c = FakeAgent("kimi-code", output="s3", duration=0.3)  # never reached
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
    c = FakeAgent("kimi-code", duration=0.3)
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
        FakeBackend(raise_on={"ensure_ready", "search", "add", "log_event"})
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
    monkeypatch.setattr("syncmill.strategies.base._git_changed_files", lambda p: {"built.py"})
    a = FakeAgent("claude", output="s1")
    b = FakeAgent("codex", output="s2")
    r = _run_pipeline([a, b], FakeMemory(), pool=pool)
    assert r.files_changed == ["built.py"]


# ── PipelineStrategy + quality gate (#10) ────────────────────────────────


def test_pipeline_gates_only_the_final_stage():
    # the gate runs once, against the final cumulative tree — not per stage
    # (intermediate stages may leave the tree broken mid-chain).
    a = FakeAgent("claude", output="s1")
    b = FakeAgent("codex", output="s2")
    gate = FakeGate(passes=True)
    r = _run_pipeline([a, b], FakeMemory(), gate=gate)
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "codex"
    # gated exactly once — on the single shared worktree (acquired for stage 0),
    # not once per stage.
    assert len(gate.evaluated) == 1
    assert r.gate is not None and r.gate.passed is True


def test_pipeline_final_gate_failure_fails_the_chain(monkeypatch):
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr("syncmill.strategies.base._git_changed_files", lambda p: {"built.py"})
    a = FakeAgent("claude", output="s1")
    b = FakeAgent("codex", output="s2")
    r = _run_pipeline([a, b], FakeMemory(), gate=FakeGate(passes=False), pool=pool)
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "codex"  # the final stage, now downgraded
    assert "quality gate failed" in r.error
    assert r.files_changed == []  # rejected → no diff collected


def test_pipeline_no_gate_collects_files(monkeypatch):
    # explicit gate=None (disabled): the final result is untouched and its diff
    # is collected, exactly as before gates existed.
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr("syncmill.strategies.base._git_changed_files", lambda p: {"built.py"})
    a = FakeAgent("claude", output="s1")
    b = FakeAgent("codex", output="s2")
    r = _run_pipeline([a, b], FakeMemory(), gate=None, pool=pool)
    assert r.status is TaskStatus.COMPLETED
    assert r.gate is None
    assert r.files_changed == ["built.py"]


# ── CompeteStrategy ──────────────────────────────────────────────────────


def _run_compete(agents, memory, *, config=None, task=None, pool=None, gate=None, breaker=None,
                 monitor=None, tracer=None):
    return CompeteStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        pool=pool or FakeWorktreePool(),
        config=config or SyncmillConfig(),
        gate=gate,
        breaker=breaker,
        monitor=monitor,
        tracer=tracer,
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

    agents = [_ProbeAgent("claude"), _ProbeAgent("codex"), _ProbeAgent("kimi-code")]
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
        "syncmill.strategies.base._git_changed_files",
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
        "syncmill.strategies.base._git_changed_files",
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
    mem = make_memory(FakeBackend(raise_on={"search", "ensure_ready"}))
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


# ── CompeteStrategy + quality gate (#10) ─────────────────────────────────


def test_compete_prefers_gate_passer_over_higher_priority_failer():
    # acceptance criterion: Compete ranks candidates using gate results. The
    # higher-priority claude COMPLETED but fails the gate, so the gate-passing
    # codex wins — selection downranks the failer.
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="claude-work")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="codex-work")
    gate = FakeGate(by_agent={"claude": False, "codex": True})
    r = _run_compete([a, b], FakeMemory(), gate=gate)
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"
    assert r.output == "codex-work"
    assert r.gate is not None and r.gate.passed is True
    # claude was gated (and failed) before codex was gated (and won)
    assert gate.evaluated == ["claude", "codex"]


def test_compete_gating_walks_priority_and_stops_at_first_passer():
    # 3 completers, [fails, passes, would-pass]: gating walks priority order, stops
    # at the first passer (codex), and never touches the lower-priority kimi.
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="claude")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="codex-win")
    c = FakeAgent("kimi-code", status=TaskStatus.COMPLETED, output="kimi")
    gate = FakeGate(by_agent={"claude": False, "codex": True})  # kimi defaults pass
    r = _run_compete([a, b, c], FakeMemory(), gate=gate)
    assert r.agent_id == "codex"
    assert gate.evaluated == ["claude", "codex"]  # kimi never gated (lazy)


def test_compete_priority_breaks_tie_among_gate_passers():
    # when several completers pass the gate, the highest-priority one still wins —
    # priority is the deterministic tie-break, and gating is lazy (stops at the
    # first passer, so the lower-priority codex is never gated).
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="claude-win")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="codex")
    gate = FakeGate(passes=True)
    r = _run_compete([a, b], FakeMemory(), gate=gate)
    assert r.agent_id == "claude"
    assert gate.evaluated == ["claude"]  # lazy: stopped at the first passer


def test_compete_all_fail_gate_aggregates_with_gate_errors():
    # every completer fails the gate → no winner → aggregate failure naming the
    # gate failures (not bogus exit codes), like Route's all-fail-gate case.
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="a")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="b")
    r = _run_compete([a, b], FakeMemory(), gate=FakeGate(passes=False))
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "none"
    assert "[claude] quality gate failed" in r.error
    assert "[codex] quality gate failed" in r.error


def test_compete_gate_failer_does_not_block_a_real_failure_report():
    # a genuinely failed agent (not gated) and a gate-failer both appear in the
    # aggregate, each with its own reason.
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="crashed")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="b")
    gate = FakeGate(by_agent={"codex": False})
    r = _run_compete([a, b], FakeMemory(), gate=gate)
    assert r.status is TaskStatus.FAILED
    assert "[claude] crashed" in r.error
    assert "[codex] quality gate failed" in r.error
    assert gate.evaluated == ["codex"]  # claude's outright failure skipped the gate


def test_select_winner_with_gate_picks_first_passer(monkeypatch):
    from conftest import FakeWorktree

    wts = [FakeWorktree(Path("/tmp/a")), FakeWorktree(Path("/tmp/b"))]
    wts[0].agent_id, wts[1].agent_id = "claude", "codex"
    results = [
        TaskResult(Task(prompt="").id, "claude", TaskStatus.COMPLETED, "a", 0, 0.0),
        TaskResult(Task(prompt="").id, "codex", TaskStatus.COMPLETED, "b", 0, 0.0),
    ]
    gate = FakeGate(by_agent={"claude": False, "codex": True})
    winner = _select_winner(wts, results, gate)
    assert winner is not None
    res, wt = winner
    assert res.agent_id == "codex" and wt is wts[1]
    # the rejected claude was downgraded in place so the caller can report it
    assert results[0].status is TaskStatus.FAILED
    assert "quality gate failed" in results[0].error


def test_select_winner_with_gate_none_when_no_passer():
    from conftest import FakeWorktree

    wts = [FakeWorktree(Path("/tmp/a"))]
    wts[0].agent_id = "claude"
    results = [TaskResult(Task(prompt="").id, "claude", TaskStatus.COMPLETED, "a", 0, 0.0)]
    assert _select_winner(wts, results, FakeGate(passes=False)) is None
    assert results[0].status is TaskStatus.FAILED  # downgraded


def test_select_winner_priority_does_not_fall_through_gate_failure():
    from conftest import FakeWorktree

    wts = [FakeWorktree(Path("/tmp/a")), FakeWorktree(Path("/tmp/b"))]
    wts[0].agent_id, wts[1].agent_id = "claude", "codex"
    results = [
        TaskResult(Task(prompt="").id, "claude", TaskStatus.COMPLETED, "a", 0, 0.0),
        TaskResult(Task(prompt="").id, "codex", TaskStatus.COMPLETED, "b", 0, 0.0),
    ]
    gate = FakeGate(by_agent={"claude": False, "codex": True})
    assert _select_winner(wts, results, gate, mode="priority") is None
    assert gate.evaluated == ["claude"]


def test_select_winner_judge_honors_structured_verdict():
    from conftest import FakeWorktree

    wts = [FakeWorktree(Path("/tmp/a")), FakeWorktree(Path("/tmp/b"))]
    wts[0].agent_id, wts[1].agent_id = "claude", "codex"
    results = [
        TaskResult(Task(prompt="").id, "claude", TaskStatus.COMPLETED, "a", 0, 0.0),
        TaskResult(Task(prompt="").id, "codex", TaskStatus.COMPLETED, "b", 0, 0.0),
    ]
    judge = FakeAgent("claude", output="analysis\nWINNER: codex")
    pool = FakeWorktreePool()
    config = SyncmillConfig()
    config.selection.mode = "judge"
    config.selection.judge_agent = "claude"
    winner = _select_winner(
        wts,
        results,
        FakeGate(passes=True),
        mode="judge",
        agents=[judge],
        pool=pool,
        config=config,
    )
    assert winner is not None
    result, worktree = winner
    assert result.agent_id == "codex"
    assert worktree is wts[1]
    assert pool.acquired == ["claude-judge"]


# ── CouncilStrategy ──────────────────────────────────────────────────────

# Stable substrings from supervisor.py's council prompt templates, used to tell
# which phase a recorded agent call belongs to.
_PROPOSE_MARK = "Draft and implement your"
_CRITIQUE_MARK = "Critique those ideas"
_SYNTH_MARK = "Synthesize them into a single best FINAL"


def _phase_of(prompt: str) -> str:
    """Classify a council prompt into its phase by template marker. Each phase is
    anchored to its OWN marker (propose included) and an unrecognized prompt raises
    — so a drifted template fails loudly here instead of being silently bucketed as
    'propose'."""
    if _SYNTH_MARK in prompt:
        return "synthesize"
    if _CRITIQUE_MARK in prompt:
        return "critique"
    if _PROPOSE_MARK in prompt:
        return "propose"
    raise AssertionError(f"prompt matches no known council phase marker: {prompt!r}")


def _council_phases(agent) -> list[str]:
    """The phase sequence an agent was driven through, from its recorded prompts."""
    return [_phase_of(c["prompt"]) for c in agent.calls]


class _PhaseAgent:
    """An AgentRunner whose status is scripted PER PHASE (propose / critique /
    synthesize), detected from the council prompt markers — so a test can make an
    agent propose+critique fine but fail only at synthesis. Records calls like
    FakeAgent. Council drives the async path, so execute() must never be reached."""

    def __init__(
        self,
        agent_id: str,
        *,
        propose: TaskStatus = TaskStatus.COMPLETED,
        critique: TaskStatus = TaskStatus.COMPLETED,
        synthesize: TaskStatus = TaskStatus.COMPLETED,
        output: str = "ok",
        error: str | None = None,
        duration: float = 0.01,
    ):
        self._agent_id = agent_id
        self._status = {"propose": propose, "critique": critique, "synthesize": synthesize}
        self._output = output
        self._error = error
        self._duration = duration
        self.calls: list[dict] = []

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @staticmethod
    def _phase(prompt: str) -> str:
        return _phase_of(prompt)

    def execute(self, prompt, workdir, timeout):  # pragma: no cover - async path only
        raise AssertionError("council must run agents via aexecute, not execute")

    async def aexecute(self, prompt, workdir, timeout):
        self.calls.append({"prompt": prompt, "workdir": workdir, "timeout": timeout})
        status = self._status[self._phase(prompt)]
        ok = status is TaskStatus.COMPLETED
        return TaskResult(
            task_id=uuid.uuid4(),
            agent_id=self._agent_id,
            status=status,
            output=self._output,
            exit_code=0 if ok else 1,
            duration_seconds=self._duration,
            error=None if ok else self._error,
        )


def _run_council(agents, memory, *, config=None, task=None, pool=None, gate=None, breaker=None, monitor=None, tracer=None):
    return CouncilStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        pool=pool or FakeWorktreePool(),
        config=config or SyncmillConfig(),
        gate=gate,
        breaker=breaker,
        monitor=monitor,
        tracer=tracer,
    )


def test_council_runs_propose_critique_synthesize():
    # rounds=1 → every member runs propose + 1 critique + synthesize (3 phases),
    # each REUSING its own worktree across all three.
    a = FakeAgent("claude", output="A")
    b = FakeAgent("codex", output="B")
    pool = FakeWorktreePool()
    r = _run_council([a, b], FakeMemory(), pool=pool)
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "claude"  # priority winner among the synthesizers
    assert _council_phases(a) == ["propose", "critique", "synthesize"]
    assert _council_phases(b) == ["propose", "critique", "synthesize"]
    assert pool.acquired == ["claude", "codex"]  # one worktree per agent, up front
    # each agent reused its single worktree across all phases; trees stay distinct
    assert len({c["workdir"] for c in a.calls}) == 1
    assert a.calls[0]["workdir"] != b.calls[0]["workdir"]


def test_council_result_reflects_more_than_one_agent():
    # ACCEPTANCE (a): the synthesized result reflects >1 agent's input — the winning
    # synthesizer's prompt carried the council's shared proposals (echo_shared
    # replays everything share_result wrote).
    a = FakeAgent("claude", output="claude-proposal")
    b = FakeAgent("codex", output="codex-proposal")
    r = _run_council([a, b], FakeMemory(echo_shared=True))
    assert r.agent_id == "claude"
    synth_prompt = a.calls[-1]["prompt"]  # claude's synthesize-phase prompt
    assert _SYNTH_MARK in synth_prompt
    assert "claude-proposal" in synth_prompt and "codex-proposal" in synth_prompt


def test_council_multi_round_runs_extra_critique_rounds():
    # ACCEPTANCE (b): rounds knob drives a genuine multi-round council.
    cfg = SyncmillConfig(council=CouncilConfig(rounds=2))
    a = FakeAgent("claude")
    b = FakeAgent("codex")
    _run_council([a, b], FakeMemory(), config=cfg)
    assert _council_phases(a) == ["propose", "critique", "critique", "synthesize"]
    assert _council_phases(b) == ["propose", "critique", "critique", "synthesize"]


def test_council_critique_round_sees_proposals():
    # each critique round reads the council's proposals back from `shared` — the
    # deliberation channel — so a member's refinement prompt carries its peers'
    # proposals (not just the synthesizer's, which a later test covers).
    a = FakeAgent("claude", output="claude-proposal")
    b = FakeAgent("codex", output="codex-proposal")
    _run_council([a, b], FakeMemory(echo_shared=True))
    critique_prompt = a.calls[1]["prompt"]  # claude's first critique-phase prompt
    assert _CRITIQUE_MARK in critique_prompt
    assert "claude-proposal" in critique_prompt and "codex-proposal" in critique_prompt


def test_council_critique_failure_tolerated():
    # documented contract: a member whose CRITIQUE fails is tolerated — the failed
    # critique is not shared, the member is NOT dropped, and it still synthesizes
    # (and can even win). Synthesis re-runs everyone, so a failed critique round is
    # a soft hiccup, not a chain-breaker.
    a = _PhaseAgent("claude", critique=TaskStatus.FAILED, error="crit-down", output="claude-fin")
    b = _PhaseAgent("codex", output="codex-fin")
    mem = FakeMemory()
    r = _run_council([a, b], mem)
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "claude"  # synthesized + won
    # both members ran all 3 phases — claude was not dropped after its failed critique
    assert _council_phases(a) == ["propose", "critique", "synthesize"]
    assert _council_phases(b) == ["propose", "critique", "synthesize"]
    # claude's failed critique was NOT shared (only its proposal); codex shared both
    shared_agents = [res.agent_id for _, res in mem.shared]
    assert shared_agents.count("claude") == 1  # proposal only — failed critique skipped
    assert shared_agents.count("codex") == 2  # proposal + critique


def test_council_winner_is_priority_not_duration():
    # determinism: the higher-priority synthesizer wins even when it is the slower
    # one — selection never reads duration (same rule as Compete's _select_winner).
    a = FakeAgent("claude", output="slow", duration=9.0)
    b = FakeAgent("codex", output="fast", duration=0.01)
    r = _run_council([a, b], FakeMemory())
    assert r.agent_id == "claude"
    assert r.output == "slow"


def test_council_runs_each_phase_concurrently():
    # headline property: a council phase OVERLAPS its members (max_active == N), and
    # it drives the async path (execute must never run).
    state = {"active": 0, "max_active": 0}

    class _ProbeAgent:
        def __init__(self, agent_id):
            self._agent_id = agent_id
            self.calls: list[str] = []

        @property
        def agent_id(self):
            return self._agent_id

        def execute(self, prompt, workdir, timeout):  # pragma: no cover
            raise AssertionError("council must run agents via aexecute, not execute")

        async def aexecute(self, prompt, workdir, timeout):
            self.calls.append(prompt)
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.02)
            state["active"] -= 1
            return TaskResult(
                uuid.uuid4(), self._agent_id, TaskStatus.COMPLETED, "ok", 0, 0.02
            )

    agents = [_ProbeAgent("claude"), _ProbeAgent("codex"), _ProbeAgent("kimi-code")]
    r = _run_council(agents, FakeMemory())
    assert state["max_active"] == 3  # a phase's members were all inside aexecute at once
    assert r.agent_id == "claude"


def test_council_proposer_failure_excluded_from_deliberation():
    # an agent that fails to PROPOSE is not a council member — it never critiques or
    # synthesizes, and its (absent) proposal is never shared.
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="cant propose")
    b = FakeAgent("codex", output="B")
    mem = FakeMemory()
    r = _run_council([a, b], mem)
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "codex"
    assert _council_phases(a) == ["propose"]  # dropped after a failed propose
    assert _council_phases(b) == ["propose", "critique", "synthesize"]
    assert "claude" not in [res.agent_id for _, res in mem.shared]


def test_council_all_propose_fail_aggregates():
    # nobody proposes → no members → aggregate failure; agents ran concurrently so
    # the wall-clock is the slowest, not the sum (like Compete).
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="a-down", duration=2.0)
    b = FakeAgent("codex", status=TaskStatus.FAILED, error="b-down", duration=5.0)
    r = _run_council([a, b], FakeMemory())
    assert r.status is TaskStatus.FAILED and r.agent_id == "none"
    assert r.error.startswith("All agents failed:")
    assert "[claude] a-down" in r.error and "[codex] b-down" in r.error
    assert r.duration_seconds == 5.0  # max(2,5), not 7
    assert _council_phases(a) == ["propose"] and _council_phases(b) == ["propose"]


def test_council_synthesis_all_fail_aggregates():
    # members propose + critique fine but every synthesis fails → no winner →
    # aggregate failure naming the synthesis errors.
    a = _PhaseAgent("claude", synthesize=TaskStatus.FAILED, error="synth-down")
    b = _PhaseAgent("codex", synthesize=TaskStatus.FAILED, error="synth-down")
    r = _run_council([a, b], FakeMemory())
    assert r.status is TaskStatus.FAILED and r.agent_id == "none"
    assert "[claude] synth-down" in r.error and "[codex] synth-down" in r.error
    assert _council_phases(a) == ["propose", "critique", "synthesize"]


def test_council_duration_is_sum_of_phase_maxes():
    # whole-council wall-clock = slowest propose + slowest critique + slowest synth.
    a = FakeAgent("claude", output="A", duration=1.0)
    b = FakeAgent("codex", output="B", duration=2.0)
    r = _run_council([a, b], FakeMemory())  # rounds=1
    assert r.duration_seconds == 6.0  # 2.0 + 2.0 + 2.0


def test_council_duration_accumulates_every_critique_round():
    # rounds=2 → the per-round `elapsed += max(...)` runs once PER critique round,
    # so the wall-clock counts BOTH rounds (propose + 2 critiques + synth). Guards a
    # regression that hoisted the accumulation out of the loop (invisible at rounds=1).
    cfg = SyncmillConfig(council=CouncilConfig(rounds=2))
    a = FakeAgent("claude", output="A", duration=1.0)
    b = FakeAgent("codex", output="B", duration=2.0)
    r = _run_council([a, b], FakeMemory(), config=cfg)
    assert r.duration_seconds == 8.0  # 2.0 * (propose + critique + critique + synth)


def test_council_winner_files_changed_from_its_own_worktree(monkeypatch):
    a = FakeAgent("claude", output="A")
    b = FakeAgent("codex", output="B")
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr(
        "syncmill.strategies.base._git_changed_files",
        lambda p: {"claude_file.py"} if "claude" in str(p) else {"codex_file.py"},
    )
    r = _run_council([a, b], FakeMemory(), pool=pool)
    assert r.agent_id == "claude"
    assert r.files_changed == ["claude_file.py"]  # the winner's OWN worktree


def test_council_non_git_degrades_cleanly():
    a = FakeAgent("claude", output="A")
    r = _run_council([a], FakeMemory(), pool=FakeWorktreePool(is_git=False))
    assert r.status is TaskStatus.COMPLETED
    assert r.files_changed == []


def test_council_passes_per_agent_timeout():
    cfg = SyncmillConfig()
    cfg.claude.timeout_seconds = 11
    cfg.codex.timeout_seconds = 22
    a, b = FakeAgent("claude"), FakeAgent("codex")
    _run_council([a, b], FakeMemory(), config=cfg)
    assert all(c["timeout"] == 11 for c in a.calls)
    assert all(c["timeout"] == 22 for c in b.calls)


def test_council_survives_dead_memory():
    # a fully-failing memtomem must degrade, not abort the council
    mem = make_memory(FakeBackend(raise_on={"search", "ensure_ready", "add"}))
    a = FakeAgent("claude", output="A")
    r = _run_council([a], mem)
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "claude"
    assert mem.degraded is True
    assert len(a.calls) == 3  # all phases still ran (propose + critique + synthesize)
    # NO context injected at any phase — degradation applies to propose AND synthesize
    assert a.calls[0]["prompt"].startswith("You are one member")  # propose
    assert a.calls[-1]["prompt"].startswith("You are one member")  # synthesize
    assert _SYNTH_MARK in a.calls[-1]["prompt"]


def test_council_no_agents_is_defensive_failure():
    r = _run_council([], FakeMemory())
    assert r.status is TaskStatus.FAILED and r.agent_id == "none"
    assert r.error == "No agents available"


# ── CouncilStrategy + quality gate (#10) ─────────────────────────────────


def test_council_gate_selects_passing_synthesizer():
    # the gate ranks the synthesized candidates via _select_winner: the
    # higher-priority claude completes but fails the gate, so codex wins.
    a = FakeAgent("claude", output="claude-final")
    b = FakeAgent("codex", output="codex-final")
    gate = FakeGate(by_agent={"claude": False, "codex": True})
    r = _run_council([a, b], FakeMemory(), gate=gate)
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "codex"
    assert r.gate is not None and r.gate.passed is True
    assert gate.evaluated == ["claude", "codex"]  # gated only at synthesis, in order


def test_council_all_fail_gate_aggregates():
    a = FakeAgent("claude", output="a")
    b = FakeAgent("codex", output="b")
    r = _run_council([a, b], FakeMemory(), gate=FakeGate(passes=False))
    assert r.status is TaskStatus.FAILED and r.agent_id == "none"
    assert "[claude] quality gate failed" in r.error
    assert "[codex] quality gate failed" in r.error


# ── CouncilStrategy + circuit breaker (#8) ───────────────────────────────


def test_council_breaker_records_once_at_propose():
    # THE council breaker rule: each agent is recorded EXACTLY ONCE (its propose
    # outcome) even though members run 2+rounds times — preserving the
    # one-record-per-run semantics the cross-run threshold relies on.
    a = FakeAgent("claude")
    b = FakeAgent("codex")
    breaker = FakeBreaker()
    _run_council([a, b], FakeMemory(), breaker=breaker)
    assert breaker.recorded == [
        ("claude", TaskStatus.COMPLETED),
        ("codex", TaskStatus.COMPLETED),
    ]
    # sanity: they really ran 3 phases each, yet were recorded once
    assert len(a.calls) == 3 and len(b.calls) == 3


def test_council_breaker_records_once_even_with_multiple_rounds():
    # The recording-once rule must hold as `rounds` scales: with rounds=2 each
    # member runs 4 phases (propose + 2 critiques + synth), yet the breaker is still
    # recorded EXACTLY once per agent (its propose outcome). Guards a rounds-scaling
    # recording regression — e.g. a stray record inside the critique loop — that a
    # rounds=1 run cannot distinguish.
    cfg = SyncmillConfig(council=CouncilConfig(rounds=2))
    a = FakeAgent("claude")
    b = FakeAgent("codex")
    breaker = FakeBreaker()
    _run_council([a, b], FakeMemory(), config=cfg, breaker=breaker)
    assert breaker.recorded == [
        ("claude", TaskStatus.COMPLETED),
        ("codex", TaskStatus.COMPLETED),
    ]
    # sanity: they really ran 4 phases each, yet were recorded once
    assert len(a.calls) == 4 and len(b.calls) == 4


def test_council_skips_open_agent_and_deliberates_with_rest():
    a = FakeAgent("claude")
    b = FakeAgent("codex")
    breaker = FakeBreaker(open_agents={"claude"})
    pool = FakeWorktreePool()
    r = _run_council([a, b], FakeMemory(), pool=pool, breaker=breaker)
    assert r.agent_id == "codex"
    assert a.calls == []  # claude's circuit OPEN → never ran
    assert pool.acquired == ["codex"]  # only the runnable agent got a worktree
    assert breaker.recorded == [("codex", TaskStatus.COMPLETED)]


def test_council_all_open_returns_breaker_failure():
    a = FakeAgent("claude")
    b = FakeAgent("codex")
    breaker = FakeBreaker(open_agents={"claude", "codex"})
    r = _run_council([a, b], FakeMemory(), breaker=breaker)
    assert r.status is TaskStatus.FAILED
    assert "circuit-open" in r.error
    assert breaker.recorded == []  # nothing ran
    assert a.calls == [] and b.calls == []


def test_council_propose_failure_recorded_as_failed():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="x")
    b = FakeAgent("codex")
    breaker = FakeBreaker()
    _run_council([a, b], FakeMemory(), breaker=breaker)
    assert breaker.recorded == [
        ("claude", TaskStatus.FAILED),  # raw propose outcome
        ("codex", TaskStatus.COMPLETED),
    ]


def test_council_gate_failure_does_not_trip_breaker():
    # a working agent whose SYNTHESIS is gate-rejected is still recorded COMPLETED
    # (raw propose status, pre-gate) — a quality verdict never trips the breaker.
    a = FakeAgent("claude", output="work")
    breaker = FakeBreaker()
    r = _run_council([a], FakeMemory(), gate=FakeGate(passes=False), breaker=breaker)
    assert r.status is TaskStatus.FAILED  # gate rejected → no winner
    assert breaker.recorded == [("claude", TaskStatus.COMPLETED)]  # but it RAN


# ── DecomposeStrategy ────────────────────────────────────────────────────

# Stable substrings from decompose.py's prompt templates, used to tell which phase
# a recorded agent call belongs to.
_PLAN_MARK = "You are the planner"
_SUBTASK_MARK = "implementing subtask"
_DSYNTH_MARK = "Integrate that work into a single"


def _decompose_phase_of(prompt: str) -> str:
    """Classify a decompose prompt into its phase by template marker; an
    unrecognized prompt raises, so a drifted template fails loudly here."""
    if _DSYNTH_MARK in prompt:
        return "synthesize"
    if _SUBTASK_MARK in prompt:
        return "subtask"
    if _PLAN_MARK in prompt:
        return "plan"
    raise AssertionError(f"prompt matches no known decompose phase marker: {prompt!r}")


def _decompose_phases(agent) -> list[str]:
    """The phase sequence an agent was driven through, from its recorded prompts."""
    return [_decompose_phase_of(c["prompt"]) for c in agent.calls]


class _DecomposeAgent:
    """AgentRunner whose status is scripted PER PHASE (plan / subtask / synthesize),
    detected from the decompose prompt markers.

    The planner runs via the SYNC ``execute`` path (a single sequential step);
    subtasks + synthesis run via the async ``aexecute`` fan-outs — so this honors
    both paths (unlike council's _PhaseAgent, which forbids sync ``execute``).
    ``plan_output`` is what the plan phase returns (the JSON the planner emits);
    ``output`` is what subtask/synth phases return. Records every call so a test can
    assert the phase sequence + which agent got which subtask."""

    def __init__(
        self,
        agent_id: str,
        *,
        plan: TaskStatus = TaskStatus.COMPLETED,
        subtask: TaskStatus = TaskStatus.COMPLETED,
        synthesize: TaskStatus = TaskStatus.COMPLETED,
        plan_output: str = '["alpha", "beta"]',
        output: str = "ok",
        error: str | None = None,
        duration: float = 0.01,
    ):
        self._agent_id = agent_id
        self._status = {"plan": plan, "subtask": subtask, "synthesize": synthesize}
        self._plan_output = plan_output
        self._output = output
        self._error = error
        self._duration = duration
        self.calls: list[dict] = []

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def _result(self, prompt: str) -> TaskResult:
        phase = _decompose_phase_of(prompt)
        status = self._status[phase]
        ok = status is TaskStatus.COMPLETED
        out = self._plan_output if phase == "plan" else self._output
        return TaskResult(
            task_id=uuid.uuid4(),
            agent_id=self._agent_id,
            status=status,
            output=out,
            exit_code=0 if ok else 1,
            duration_seconds=self._duration,
            error=None if ok else self._error,
        )

    def execute(self, prompt, workdir, timeout):
        self.calls.append({"prompt": prompt, "workdir": workdir, "timeout": timeout})
        return self._result(prompt)

    async def aexecute(self, prompt, workdir, timeout):
        self.calls.append({"prompt": prompt, "workdir": workdir, "timeout": timeout})
        return self._result(prompt)


def _run_decompose(agents, memory, *, config=None, task=None, pool=None, gate=None, breaker=None, monitor=None, tracer=None):
    return DecomposeStrategy().execute(
        task=task or Task(prompt="do it"),
        agents=agents,
        memory=memory,
        pool=pool or FakeWorktreePool(),
        config=config or SyncmillConfig(),
        gate=gate,
        breaker=breaker,
        monitor=monitor,
        tracer=tracer,
    )


def test_decompose_runs_plan_subtasks_synthesize():
    # the headline flow: plan → run subtasks across agents → synthesize → converge.
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="A")
    b = _DecomposeAgent("codex", output="B")
    pool = FakeWorktreePool()
    r = _run_decompose([a, b], FakeMemory(), pool=pool)
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "claude"  # priority winner among the synthesizers
    # claude planned, did subtask 1, then synthesized; codex did subtask 2 + synth
    assert _decompose_phases(a) == ["plan", "subtask", "synthesize"]
    assert _decompose_phases(b) == ["subtask", "synthesize"]
    # one worktree per step: plan(claude), sub0(claude), sub1(codex), synth(claude,codex)
    assert pool.acquired == ["claude", "claude", "codex", "claude", "codex"]


def test_decompose_distributes_subtasks_round_robin_across_agents():
    # ACCEPTANCE: a compound task is split into subtasks, each executed — and they
    # are distributed ACROSS agents (round-robin over the runnable agents).
    a = _DecomposeAgent("claude", plan_output='["one", "two", "three"]', output="A")
    b = _DecomposeAgent("codex", output="B")
    _run_decompose([a, b], FakeMemory())
    # 3 subtasks, 2 agents → claude gets subtasks 1 & 3, codex gets subtask 2
    assert _decompose_phases(a) == ["plan", "subtask", "subtask", "synthesize"]
    assert _decompose_phases(b) == ["subtask", "synthesize"]
    a_subtasks = [c["prompt"] for c in a.calls if _decompose_phase_of(c["prompt"]) == "subtask"]
    b_subtasks = [c["prompt"] for c in b.calls if _decompose_phase_of(c["prompt"]) == "subtask"]
    assert "subtask 1 of 3" in a_subtasks[0] and "one" in a_subtasks[0]
    assert "subtask 3 of 3" in a_subtasks[1] and "three" in a_subtasks[1]
    assert "subtask 2 of 3" in b_subtasks[0] and "two" in b_subtasks[0]


def test_decompose_result_reflects_more_than_one_agents_subtask():
    # ACCEPTANCE: the subtask results are combined into one outcome — the winning
    # synthesizer's prompt carries every completed subtask's summary (echo_shared
    # replays whatever was share_result'd, i.e. the subtask outputs).
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="claude-piece")
    b = _DecomposeAgent("codex", output="codex-piece")
    r = _run_decompose([a, b], FakeMemory(echo_shared=True))
    assert r.agent_id == "claude"
    synth_prompt = a.calls[-1]["prompt"]  # claude's synthesize-phase prompt
    assert _DSYNTH_MARK in synth_prompt
    assert "claude-piece" in synth_prompt and "codex-piece" in synth_prompt


def test_decompose_unparseable_plan_degrades_to_whole_task():
    # a planner that returns non-JSON degrades to a single whole-task subtask, so a
    # flaky planner never aborts the run (best-effort, like memory).
    a = _DecomposeAgent("claude", plan_output="sorry, I can't produce JSON", output="A")
    b = _DecomposeAgent("codex", output="B")
    r = _run_decompose([a, b], FakeMemory(), task=Task(prompt="build the thing"))
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "claude"
    # exactly one (whole-task) subtask ran, on the planner; codex only synthesized
    assert _decompose_phases(a) == ["plan", "subtask", "synthesize"]
    assert _decompose_phases(b) == ["synthesize"]
    subtask_prompt = a.calls[1]["prompt"]
    assert "subtask 1 of 1" in subtask_prompt and "build the thing" in subtask_prompt


def test_decompose_object_wrapped_array_degrades_not_silently_wrong():
    # the planner was told "no wrapping object"; a wrapping object must DEGRADE to
    # the whole-task subtask, NOT have its inner array greedily extracted (which
    # would be a silently-wrong breakdown).
    a = _DecomposeAgent("claude", plan_output='{"subtasks": ["x", "y"]}', output="A")
    r = _run_decompose([a], FakeMemory(), task=Task(prompt="the whole thing"))
    assert r.status is TaskStatus.COMPLETED
    assert _decompose_phases(a) == ["plan", "subtask", "synthesize"]  # one whole-task
    assert "subtask 1 of 1" in a.calls[1]["prompt"]  # not "x"/"y" → not the inner array
    assert "the whole thing" in a.calls[1]["prompt"]


def test_decompose_mixed_type_array_degrades_not_salvaged():
    # STRICT contract: a malformed array mixing non-strings/blanks with strings is
    # NOT salvaged down to its string subset (that would silently run a wrong/
    # partial plan) — it degrades to the single whole-task subtask.
    for plan_output in (
        '[{"task": "parser"}, "tests"]',  # object + string
        '["parser", 123, "tests"]',  # string + number + string
        '["parser", "", "tests"]',  # string + blank + string
    ):
        a = _DecomposeAgent("claude", plan_output=plan_output, output="A")
        r = _run_decompose([a], FakeMemory(), task=Task(prompt="the whole thing"))
        assert r.status is TaskStatus.COMPLETED
        # one whole-task subtask, not the salvaged "parser"/"tests" subset
        assert _decompose_phases(a) == ["plan", "subtask", "synthesize"]
        assert "subtask 1 of 1" in a.calls[1]["prompt"]
        assert "the whole thing" in a.calls[1]["prompt"]


def test_decompose_refusal_with_example_array_degrades():
    # a refusal whose PROSE happens to contain an example array must NOT have that
    # example lifted out as the plan — it degrades to the whole-task subtask.
    a = _DecomposeAgent(
        "claude", plan_output='I cannot break this down. Format would be ["x", "y"].',
    )
    r = _run_decompose([a], FakeMemory(), task=Task(prompt="the whole thing"))
    assert r.status is TaskStatus.COMPLETED
    assert _decompose_phases(a) == ["plan", "subtask", "synthesize"]
    assert "subtask 1 of 1" in a.calls[1]["prompt"]


def test_decompose_fenced_json_array_is_parsed():
    # a ```json … ``` fence around the array is tolerated (LLMs love fences).
    a = _DecomposeAgent("claude", plan_output='```json\n["sa", "sb"]\n```', output="A")
    b = _DecomposeAgent("codex", output="B")
    _run_decompose([a, b], FakeMemory())
    # two real subtasks parsed out of the fenced array
    assert _decompose_phases(a) == ["plan", "subtask", "synthesize"]
    assert _decompose_phases(b) == ["subtask", "synthesize"]


def test_decompose_caps_subtasks_at_max_subtasks(caplog):
    # the planner over-produces; _parse_subtasks truncates to max_subtasks, keeps
    # the FIRST max_subtasks in order, and LOGS what it dropped (no silent cap).
    cfg = SyncmillConfig(decompose=DecomposeConfig(max_subtasks=2))
    a = _DecomposeAgent("claude", plan_output='["aa", "bb", "cc", "dd", "ee"]')
    with caplog.at_level(logging.INFO, logger="syncmill.strategies.decompose"):
        r = _run_decompose([a], FakeMemory(), config=cfg)
    assert r.status is TaskStatus.COMPLETED
    # only 2 subtasks ran (single agent → both routed to it): plan + 2 sub + synth
    assert _decompose_phases(a) == ["plan", "subtask", "subtask", "synthesize"]
    # exactly two subtasks ran, and they are the FIRST two in order — each subtask
    # prompt ends with "...implement now: <item>", so the suffix is the kept item
    # (a tail-slice regression would end with "dd"/"ee" instead).
    subs = [c["prompt"] for c in a.calls if _decompose_phase_of(c["prompt"]) == "subtask"]
    assert [s.rstrip().rsplit("now: ", 1)[1] for s in subs] == ["aa", "bb"]
    assert "capping 5 planned subtasks to max_subtasks=2 (dropping 3)" in caplog.text


def test_decompose_subtask_failure_tolerated():
    # a failed subtask is tolerated (not shared, not fatal): synthesis still runs
    # over the survivors and produces a winner.
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="A")
    b = _DecomposeAgent("codex", subtask=TaskStatus.FAILED, error="sub-down", output="B")
    mem = FakeMemory()
    r = _run_decompose([a, b], mem)
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "claude"
    assert _decompose_phases(b) == ["subtask", "synthesize"]  # codex still synthesized
    # only the COMPLETED subtask (claude's) was shared for synthesis
    assert [res.agent_id for _, res in mem.shared] == ["claude"]


def test_decompose_all_subtasks_fail_aggregates():
    # nothing decomposed completed → nothing to integrate → aggregate failure, and
    # synthesis never runs. Concurrent, so wall-clock is the slowest subtask.
    a = _DecomposeAgent(
        "claude", plan_output='["sa", "sb"]', subtask=TaskStatus.FAILED,
        error="a-sub-down", duration=2.0,
    )
    b = _DecomposeAgent("codex", subtask=TaskStatus.FAILED, error="b-sub-down", duration=5.0)
    r = _run_decompose([a, b], FakeMemory())
    assert r.status is TaskStatus.FAILED and r.agent_id == "none"
    assert "[claude] a-sub-down" in r.error and "[codex] b-sub-down" in r.error
    assert _decompose_phases(a) == ["plan", "subtask"]  # no synthesize
    assert _decompose_phases(b) == ["subtask"]
    assert r.duration_seconds == 7.0  # plan(2.0) + max(2.0, 5.0) subtask


def test_decompose_winner_is_priority_not_duration():
    # determinism: the higher-priority synthesizer wins even when it is the slower
    # one — selection never reads duration (same _select_winner rule as Compete).
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="slow", duration=9.0)
    b = _DecomposeAgent("codex", output="fast", duration=0.01)
    r = _run_decompose([a, b], FakeMemory())
    assert r.agent_id == "claude" and r.output == "slow"


def test_decompose_duration_is_plan_plus_subtask_plus_synth_maxes():
    # whole-decompose wall-clock = planner + slowest subtask + slowest synthesizer.
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', duration=1.0)
    b = _DecomposeAgent("codex", duration=2.0)
    r = _run_decompose([a, b], FakeMemory())
    assert r.duration_seconds == 5.0  # plan 1.0 + max(1.0,2.0) + max(1.0,2.0)


def test_decompose_runs_each_concurrent_phase_concurrently():
    # both the subtask AND synthesis fan-outs OVERLAP their agents — pinned PER
    # PHASE so a regression that serialized only synthesis (which uses a different
    # primitive, aexecute_isolated, than the subtask aexecute_each) still fails.
    # The planner runs sync (alone), so it's not in the concurrency buckets.
    active = {"subtask": 0, "synthesize": 0}
    peak = {"subtask": 0, "synthesize": 0}

    class _ProbeAgent:
        def __init__(self, agent_id):
            self._agent_id = agent_id

        @property
        def agent_id(self):
            return self._agent_id

        def execute(self, prompt, workdir, timeout):
            # the planner step — sync, runs alone, returns a 3-subtask plan
            return TaskResult(
                uuid.uuid4(), self._agent_id, TaskStatus.COMPLETED,
                '["a", "b", "c"]', 0, 0.0,
            )

        async def aexecute(self, prompt, workdir, timeout):
            phase = _decompose_phase_of(prompt)
            active[phase] += 1
            peak[phase] = max(peak[phase], active[phase])
            await asyncio.sleep(0.02)
            active[phase] -= 1
            return TaskResult(
                uuid.uuid4(), self._agent_id, TaskStatus.COMPLETED, "ok", 0, 0.02
            )

    agents = [_ProbeAgent("claude"), _ProbeAgent("codex"), _ProbeAgent("kimi-code")]
    r = _run_decompose(agents, FakeMemory())
    assert peak["subtask"] == 3  # 3 subtasks overlapped (aexecute_each fan-out)
    assert peak["synthesize"] == 3  # 3 synthesizers overlapped (aexecute_isolated)
    assert r.agent_id == "claude"


def test_decompose_winner_files_changed_from_its_own_worktree(monkeypatch):
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="A")
    b = _DecomposeAgent("codex", output="B")
    pool = FakeWorktreePool(is_git=True)
    monkeypatch.setattr(
        "syncmill.strategies.base._git_changed_files",
        lambda p: {"claude_file.py"} if "claude" in str(p) else {"codex_file.py"},
    )
    r = _run_decompose([a, b], FakeMemory(), pool=pool)
    assert r.agent_id == "claude"
    assert r.files_changed == ["claude_file.py"]  # the winner's OWN (synth) worktree


def test_decompose_real_worktrees_no_collision_and_files_changed(git_repo):
    # REAL WorktreePool over a git repo (not FakeWorktreePool): decompose acquires
    # the SAME agent several times (planner + subtask + synth). The per-acquire slot
    # must keep each a distinct REAL git worktree — the collision bug silently
    # degraded the 2nd+ acquire to an empty tempdir, so the winner synthesized in an
    # empty non-git dir and files_changed came back []. Each agent writes a real
    # file so the winner's diff is observable end-to-end against real git.
    from syncmill.worktree import WorktreePool

    class _WritingAgent:
        def __init__(self, agent_id, plan_output):
            self._agent_id = agent_id
            self._plan_output = plan_output

        @property
        def agent_id(self):
            return self._agent_id

        def _run(self, prompt, workdir):
            phase = _decompose_phase_of(prompt)
            if phase == "plan":
                return TaskResult(
                    uuid.uuid4(), self._agent_id, TaskStatus.COMPLETED,
                    self._plan_output, 0, 0.01,
                )
            # subtask/synth: write a real file so the worktree has a git diff
            (Path(workdir) / f"{self._agent_id}_{phase}.py").write_text("x = 1\n")
            return TaskResult(
                uuid.uuid4(), self._agent_id, TaskStatus.COMPLETED,
                f"{self._agent_id} did {phase}", 0, 0.01,
            )

        def execute(self, prompt, workdir, timeout):
            return self._run(prompt, workdir)

        async def aexecute(self, prompt, workdir, timeout):
            return self._run(prompt, workdir)

    a = _WritingAgent("claude", '["sa", "sb"]')
    b = _WritingAgent("codex", '["sa"]')
    pool = WorktreePool(git_repo, "abcdef1234")
    try:
        r = DecomposeStrategy().execute(
            task=Task(prompt="go"),
            agents=[a, b],
            memory=FakeMemory(),
            pool=pool,
            config=SyncmillConfig(),
        )
        wts = pool.worktrees  # snapshot before cleanup clears the tracking
        # every acquired worktree is a REAL git worktree (no tempdir fallback) and
        # they are all distinct (the collision would have produced fewer + tempdirs)
        assert all(w.is_git for w in wts)
        assert len({str(w.path) for w in wts}) == len(wts)
    finally:
        pool.cleanup_all()
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "claude"
    # the winner synthesized in its own real git worktree → real diff, not []
    assert r.files_changed == ["claude_synthesize.py"]


def test_decompose_non_git_degrades_cleanly():
    a = _DecomposeAgent("claude", plan_output='["sa"]', output="A")
    r = _run_decompose([a], FakeMemory(), pool=FakeWorktreePool(is_git=False))
    assert r.status is TaskStatus.COMPLETED
    assert r.files_changed == []


def test_decompose_passes_per_agent_timeout():
    cfg = SyncmillConfig()
    cfg.claude.timeout_seconds = 11
    cfg.codex.timeout_seconds = 22
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]')
    b = _DecomposeAgent("codex")
    _run_decompose([a, b], FakeMemory(), config=cfg)
    assert all(c["timeout"] == 11 for c in a.calls)
    assert all(c["timeout"] == 22 for c in b.calls)


def test_decompose_survives_dead_memory():
    # a fully-failing memtomem must degrade, not abort decompose
    mem = make_memory(FakeBackend(raise_on={"search", "ensure_ready", "add"}))
    a = _DecomposeAgent("claude", plan_output='["only one"]', output="A")
    r = _run_decompose([a], mem)
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "claude"
    assert mem.degraded is True
    assert _decompose_phases(a) == ["plan", "subtask", "synthesize"]
    # NO context injected at any phase — degradation applies to plan, subtask, synth
    assert a.calls[0]["prompt"].startswith("You are the planner")
    assert a.calls[-1]["prompt"].startswith("Independent coding agents")


def test_decompose_no_agents_is_defensive_failure():
    r = _run_decompose([], FakeMemory())
    assert r.status is TaskStatus.FAILED and r.agent_id == "none"
    assert r.error == "No agents available"


# ── DecomposeStrategy + quality gate (#10) ───────────────────────────────


def test_decompose_gate_selects_passing_synthesizer():
    # the gate ranks the synthesized candidates via _select_winner, ONLY at
    # synthesis: higher-priority claude completes but fails the gate → codex wins.
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="claude-final")
    b = _DecomposeAgent("codex", output="codex-final")
    gate = FakeGate(by_agent={"claude": False, "codex": True})
    r = _run_decompose([a, b], FakeMemory(), gate=gate)
    assert r.status is TaskStatus.COMPLETED and r.agent_id == "codex"
    assert r.gate is not None and r.gate.passed is True
    # gated only at synthesis (not subtasks), in priority order
    assert gate.evaluated == ["claude", "codex"]


def test_decompose_all_fail_gate_aggregates():
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="a")
    b = _DecomposeAgent("codex", output="b")
    r = _run_decompose([a, b], FakeMemory(), gate=FakeGate(passes=False))
    assert r.status is TaskStatus.FAILED and r.agent_id == "none"
    assert "[claude] quality gate failed" in r.error
    assert "[codex] quality gate failed" in r.error


# ── DecomposeStrategy + circuit breaker (#8) ─────────────────────────────


def test_decompose_breaker_records_once_per_agent_at_first_execution():
    # THE decompose breaker rule: each agent is recorded EXACTLY ONCE at its first
    # execution — claude at its plan step, codex at its subtask — even though they
    # run several phases, preserving one-record-per-agent-per-run.
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]')
    b = _DecomposeAgent("codex")
    breaker = FakeBreaker()
    _run_decompose([a, b], FakeMemory(), breaker=breaker)
    assert breaker.recorded == [
        ("claude", TaskStatus.COMPLETED),
        ("codex", TaskStatus.COMPLETED),
    ]
    # sanity: claude ran 3 phases, codex 2, yet each recorded once
    assert len(a.calls) == 3 and len(b.calls) == 2


def test_decompose_planner_failure_recorded_as_failed():
    # the planner's RAW outcome is recorded as claude's first execution; even though
    # claude later completes the (degraded) subtask + synth, the dedup keeps the
    # FAILED plan status. codex's first execution is its synthesis.
    a = _DecomposeAgent("claude", plan=TaskStatus.FAILED, error="cant-plan")
    b = _DecomposeAgent("codex")
    breaker = FakeBreaker()
    r = _run_decompose([a, b], FakeMemory(), breaker=breaker)
    assert r.status is TaskStatus.COMPLETED  # degraded to whole-task, still synthesized
    assert breaker.recorded == [
        ("claude", TaskStatus.FAILED),
        ("codex", TaskStatus.COMPLETED),
    ]


def test_decompose_skips_open_agent_and_runs_with_rest():
    a = _DecomposeAgent("claude")
    b = _DecomposeAgent("codex", plan_output='["sb"]', output="B")
    breaker = FakeBreaker(open_agents={"claude"})
    pool = FakeWorktreePool()
    r = _run_decompose([a, b], FakeMemory(), pool=pool, breaker=breaker)
    assert r.agent_id == "codex"  # only runnable agent → planner + sole synthesizer
    assert a.calls == []  # claude's circuit OPEN → never ran
    assert breaker.recorded == [("codex", TaskStatus.COMPLETED)]


def test_decompose_all_open_returns_breaker_failure():
    a = _DecomposeAgent("claude")
    b = _DecomposeAgent("codex")
    breaker = FakeBreaker(open_agents={"claude", "codex"})
    r = _run_decompose([a, b], FakeMemory(), breaker=breaker)
    assert r.status is TaskStatus.FAILED
    assert "circuit-open" in r.error
    assert breaker.recorded == []
    assert a.calls == [] and b.calls == []


def test_decompose_gate_failure_does_not_trip_breaker():
    # a working agent whose SYNTHESIS is gate-rejected is still recorded COMPLETED
    # (raw plan status, pre-gate) — a quality verdict never trips the breaker.
    a = _DecomposeAgent("claude", plan_output='["sa"]', output="work")
    breaker = FakeBreaker()
    r = _run_decompose([a], FakeMemory(), gate=FakeGate(passes=False), breaker=breaker)
    assert r.status is TaskStatus.FAILED  # gate rejected → no winner
    assert breaker.recorded == [("claude", TaskStatus.COMPLETED)]  # but it RAN


# ── Strategy selection ───────────────────────────────────────────────────


def test_available_strategies_sorted():
    assert available_strategies() == [
        "compete",
        "council",
        "decompose",
        "pipeline",
        "route",
    ]


def test_make_strategy_selects_each():
    assert isinstance(_make_strategy("route"), RouteStrategy)
    assert isinstance(_make_strategy("pipeline"), PipelineStrategy)
    assert isinstance(_make_strategy("compete"), CompeteStrategy)
    assert isinstance(_make_strategy("council"), CouncilStrategy)
    assert isinstance(_make_strategy("decompose"), DecomposeStrategy)


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


def _supervisor(registry, memory, strategy=None, config=None, breaker=None, matcher=None, board=None):
    sup = Supervisor.__new__(Supervisor)
    sup._config = config or SyncmillConfig()
    sup._registry = registry
    sup._memory = memory
    sup._breaker = breaker
    sup._matcher = matcher
    sup._board = board  # lazy in __init__; tests set it directly for run_item
    sup._strategy = strategy or RouteStrategy()
    sup.last_report_path = None  # set in __init__; run() resets it per run
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
    monkeypatch.setattr("syncmill.strategies.base._git_changed_files", lambda p: {"changed.py"})
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


def test_run_marks_artifact_recovery_required_when_final_save_fails(
    git_repo, tmp_path, monkeypatch
):
    config = SyncmillConfig(
        repo_root=str(git_repo),
        workspace={"root": str(tmp_path / "workspaces")},
        runs={"root": str(tmp_path / "runs")},
        agent_priority=["codex"],
    )
    sup = _supervisor(
        FakeRegistry([FakeAgent("codex")]),
        FakeMemory(),
        config=config,
    )

    def fail_save(self, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(S.ArtifactStore, "save", fail_save)
    result = sup.run(Task(prompt="go"))
    assert result.status is TaskStatus.FAILED
    assert "failed to persist mandatory run artifact" in result.error
    manifests = list((tmp_path / "runs").glob("*/runs/*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    assert manifest["state"] == "recovery_required"
    assert "disk full" in manifest["recovery_reason"]
    assert sup.recovery_workspace is not None
    assert sup.recovery_workspace.exists()


def test_run_resolves_run_id_via_single_resolver(patched_worktree, monkeypatch):
    # Direct run: no run_id kwarg → the resolver is entered with None (and mints
    # fresh). Lookup-site patch on syncmill.supervisor, where run() resolves.
    calls = []

    def recorder(explicit=None):
        calls.append(explicit)
        return "rid-resolved"

    monkeypatch.setattr(S, "resolve_run_id", recorder)
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert calls == [None]


def test_run_explicit_run_id_reaches_resolver(patched_worktree, monkeypatch):
    # An explicit run(run_id=) rides through the resolver unchanged (explicit wins).
    calls = []

    def recorder(explicit=None):
        calls.append(explicit)
        return explicit or "rid-fresh"

    monkeypatch.setattr(S, "resolve_run_id", recorder)
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    sup.run(Task(prompt="go"), run_id="rid-explicit")
    assert calls == ["rid-explicit"]


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


def test_run_with_council_strategy_shares_winner_and_cleans_all(patched_worktree):
    # end-to-end: a council deliberates, the Supervisor shares the winner once at
    # the end (on top of the proposals/critiques the strategy shares mid-run), and
    # every member's worktree is created and cleaned up.
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", output="cwin"), FakeAgent("codex", output="x")]),
        mem,
        strategy=CouncilStrategy(),
    )
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "claude"  # priority synthesizer wins
    assert r.files_changed == ["changed.py"]
    # the strategy publishes proposals + critiques mid-run, then the Supervisor
    # shares the winner LAST — so both members appear and the final share is the winner
    assert {res.agent_id for _, res in mem.shared} >= {"claude", "codex"}
    assert len(mem.shared) >= 3  # ≥2 proposals + the Supervisor's final winner share
    assert mem.shared[-1][1].agent_id == "claude"
    assert patched_worktree["pool"].acquired == ["claude", "codex"]  # one tree per agent
    assert patched_worktree["pool"].cleaned
    assert "register:claude" in mem.events and "register:codex" in mem.events


def test_run_with_decompose_strategy_shares_winner_and_cleans_all(patched_worktree):
    # end-to-end: decompose splits the task (planner emits 2 subtasks), runs them
    # across both agents, synthesizes, and the Supervisor shares the winner once at
    # the end (on top of the subtask summaries the strategy shares mid-run); every
    # worktree (planner + subtasks + synthesizers) is created and cleaned up.
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([
            _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="cwin"),
            _DecomposeAgent("codex", output="x"),
        ]),
        mem,
        strategy=DecomposeStrategy(),
    )
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "claude"  # priority synthesizer wins
    assert r.files_changed == ["changed.py"]
    # the strategy shares both completed subtasks mid-run, then the Supervisor shares
    # the winner LAST — so both agents appear and the final share is the winner
    assert {res.agent_id for _, res in mem.shared} >= {"claude", "codex"}
    assert len(mem.shared) >= 3  # 2 subtask summaries + the Supervisor's final share
    assert mem.shared[-1][1].agent_id == "claude"
    # plan(claude), sub0(claude), sub1(codex), synth(claude, codex) — all cleaned up
    assert patched_worktree["pool"].acquired == ["claude", "claude", "codex", "claude", "codex"]
    assert patched_worktree["pool"].cleaned
    assert "register:claude" in mem.events and "register:codex" in mem.events


def test_run_decompose_matched_agent_plans_and_leads(patched_worktree):
    # capability matching re-orders ONCE at the Supervisor, so the promoted agent
    # becomes decompose's planner (runnable[0]) AND the synthesis winner-priority —
    # the "free consequence" of re-ordering at the source, no Strategy change.
    claude = _DecomposeAgent("claude", plan_output='["sa"]', output="c")
    codex = _DecomposeAgent("codex", plan_output='["sa"]', output="x")
    sup = _supervisor(
        FakeRegistry([claude, codex]),
        FakeMemory(),
        strategy=DecomposeStrategy(),
        matcher=FakeMatcher(promote=["codex"]),
    )
    r = sup.run(Task(prompt="go"))
    assert r.agent_id == "codex"  # promoted → planned and won synthesis
    assert _decompose_phases(codex)[0] == "plan"  # the promoted agent planned
    assert "plan" not in _decompose_phases(claude)  # claude did not plan


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


def test_run_passes_gate_to_strategy_and_shares_gated_winner(patched_worktree, monkeypatch):
    # the Supervisor builds the gate from config and injects it; a gate-passing
    # winner is shared with its verdict attached.
    gate = FakeGate(passes=True)
    monkeypatch.setattr(S, "build_gate", lambda cfg: gate)
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="done")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.gate is not None and r.gate.passed is True
    assert gate.evaluated == ["claude"]  # gate ran against the agent's worktree
    assert [res.agent_id for _, res in mem.shared] == ["claude"]  # winner shared


def test_run_gate_rejected_result_is_not_shared_but_cleans_up(patched_worktree, monkeypatch):
    # a result that fails the gate must not reach shared memory — it's a failure —
    # but the session still ends and worktrees are still torn down.
    monkeypatch.setattr(S, "build_gate", lambda cfg: FakeGate(passes=False))
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="done")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.FAILED
    assert mem.shared == []
    assert any(e.startswith("end:") for e in mem.events)
    assert patched_worktree["pool"].cleaned


def test_run_unconfigured_gate_is_off(patched_worktree):
    # default config has no gate commands → build_gate returns None → ungated,
    # behavior unchanged (the disabled default).
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="done")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert r.gate is None  # no gate ran
    assert [res.agent_id for _, res in mem.shared] == ["claude"]


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
                      FakeAgent("kimi-code", output="from-kimi")]),
        mem,
    )
    r = sup.run(Task(prompt="go"), exclude={"claude"})
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "kimi-code"  # claude (first priority) was excluded
    assert r.output == "from-kimi"
    assert patched_worktree["pool"].acquired == ["kimi-code"]  # only the survivor got a worktree
    # excluded agents are never registered in memory either
    assert "register:claude" not in mem.events
    assert "register:kimi-code" in mem.events


def test_run_all_agents_excluded(patched_worktree):
    # two excluded agents → exercises the sorted() multi-element error rendering
    sup = _supervisor(FakeRegistry([FakeAgent("kimi-code"), FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"), exclude={"kimi-code", "claude"})
    assert r.status is TaskStatus.FAILED
    assert r.error == "No agents available (excluded: claude, kimi-code)"  # sorted
    assert patched_worktree["created"] == {}  # no worktree created


def test_run_forced_agent_also_excluded_errors(patched_worktree):
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"), agent_id="claude", exclude={"claude"})
    assert r.status is TaskStatus.FAILED
    assert "both forced" in r.error
    assert patched_worktree["created"] == {}  # rejected before any workspace


def test_run_forced_agent_with_unrelated_exclude(patched_worktree):
    # forcing kimi-code while excluding a *different* agent (claude) is valid, not a conflict
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude"), FakeAgent("kimi-code", output="g"),
                      FakeAgent("codex")]),
        mem,
    )
    r = sup.run(Task(prompt="go"), agent_id="kimi-code", exclude={"claude"})
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "kimi-code"  # the guard only fires when the forced agent IS excluded
    assert "register:kimi-code" in mem.events and "register:claude" not in mem.events


def test_run_survives_dead_memory(patched_worktree):
    # the whole point: a broken memtomem must not crash the task
    mem = make_memory(
        FakeBackend(raise_on={"ensure_ready", "start_session", "search", "add",
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
    assert mem._backend is None and mem.degraded is True
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


# ── _git_diff_patch (real git — the --emit-patch artifact) ────────────────


def test_git_diff_patch_applies_to_fresh_clone(git_repo, tmp_path):
    # the collected patch is a real, applyable artifact: a modified file AND an
    # untracked new file both round-trip through `git apply` onto a fresh clone
    # of the same HEAD — the exact recovery flow --emit-patch promises.
    (git_repo / "README.md").write_text("# changed\n")
    (git_repo / "new.txt").write_text("hi\n")
    patch = _git_diff_patch(git_repo)
    assert patch and "new.txt" in patch and "README.md" in patch

    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(git_repo), str(clone)],
        check=True, capture_output=True,
    )
    patch_file = tmp_path / "win.patch"
    patch_file.write_text(patch, encoding="utf-8", errors="surrogateescape")
    subprocess.run(
        ["git", "apply", str(patch_file)],
        check=True, capture_output=True, cwd=str(clone),
    )
    assert (clone / "README.md").read_text() == "# changed\n"
    assert (clone / "new.txt").read_text() == "hi\n"


def test_git_diff_patch_clean_repo_empty_string(git_repo):
    # "" = the agent changed nothing (distinct from None = collection failed)
    assert _git_diff_patch(git_repo) == ""


def test_git_diff_patch_non_git_dir_none(tmp_path):
    assert _git_diff_patch(tmp_path) is None


def test_collect_files_changed_lists_untracked_before_staging(git_repo):
    # ordering guard: _git_diff_patch stages the tree (git add -A), which would
    # empty _git_changed_files's untracked listing if it ran FIRST — the winner
    # must carry BOTH the untracked file in files_changed AND it in the patch.
    from syncmill.strategies.base import _collect_files_changed
    from syncmill.worktree import Worktree

    (git_repo / "new.txt").write_text("hi\n")
    result = TaskResult(
        Task(prompt="p").id, "claude", TaskStatus.COMPLETED, "", 0, 0.1
    )
    wt = Worktree(path=git_repo, branch=None, agent_id="claude", is_git=True)
    _collect_files_changed(result, wt)
    assert "new.txt" in result.files_changed  # untracked listed BEFORE add -A
    assert result.patch is not None and "new.txt" in result.patch


def test_route_winner_carries_patch(monkeypatch):
    # the strategy layer attaches the winner's patch via the single
    # _collect_files_changed seam (lookup-site: syncmill.strategies.base)
    monkeypatch.setattr(
        "syncmill.strategies.base._git_changed_files", lambda p: {"a.py"}
    )
    monkeypatch.setattr(
        "syncmill.strategies.base._git_diff_patch", lambda p: "FAKE PATCH"
    )
    agent = FakeAgent("claude", status=TaskStatus.COMPLETED)
    r = _run_route([agent], FakeMemory(), pool=FakeWorktreePool(is_git=True))
    assert r.patch == "FAKE PATCH"


def test_route_non_git_worktree_patch_none(monkeypatch):
    # tempdir fallback (is_git=False) → no diff collection at all
    agent = FakeAgent("claude", status=TaskStatus.COMPLETED)
    r = _run_route([agent], FakeMemory(), pool=FakeWorktreePool(is_git=False))
    assert r.patch is None and r.files_changed == []


# ── Quality gate, real stack (real WorktreePool + real CommandGate) ───────


class _WritingAgent:
    """An agent that actually writes a file into its worktree, so a real gate
    command can inspect a real diff — proves the gate runs in the agent's cwd,
    which the FakeGate/FakeWorktreePool tests can't."""

    def __init__(self, agent_id: str, content: str):
        self._agent_id = agent_id
        self._content = content
        self.calls: list[dict] = []

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def execute(self, prompt, workdir, timeout):
        self.calls.append({"prompt": prompt, "workdir": workdir, "timeout": timeout})
        (Path(workdir) / "result.txt").write_text(self._content)
        return TaskResult(
            uuid.uuid4(), self._agent_id, TaskStatus.COMPLETED, "wrote result.txt", 0, 0.01
        )

    async def aexecute(self, prompt, workdir, timeout):
        # so the same agent drives Compete's async fan-out as well as Route/Pipeline
        return self.execute(prompt, workdir, timeout)


def test_route_real_gate_falls_back_then_accepts(git_repo):
    # End-to-end: a real grep gate over a real worktree. The first agent writes a
    # file the gate rejects (Route falls back); the second writes one it accepts.
    from syncmill.gates import build_gate
    from syncmill.config import GateConfig
    from syncmill.worktree import WorktreePool

    gate = build_gate(GateConfig(commands=["grep -q GOOD result.txt"]))
    pool = WorktreePool(git_repo, "gatetask")
    a = _WritingAgent("claude", "this is BAD\n")
    b = _WritingAgent("codex", "this is GOOD\n")
    try:
        r = RouteStrategy().execute(
            task=Task(prompt="do it"),
            agents=[a, b],
            memory=FakeMemory(),
            pool=pool,
            config=SyncmillConfig(),
            gate=gate,
        )
        assert r.status is TaskStatus.COMPLETED
        assert r.agent_id == "codex"  # claude's diff failed the real gate
        assert r.gate is not None and r.gate.passed is True
        assert r.files_changed == ["result.txt"]  # accepted winner's real diff
    finally:
        pool.cleanup_all()


def test_route_real_gate_all_fail_aggregates(git_repo):
    from syncmill.gates import build_gate
    from syncmill.config import GateConfig
    from syncmill.worktree import WorktreePool

    gate = build_gate(GateConfig(commands=["grep -q GOOD result.txt"]))
    pool = WorktreePool(git_repo, "gatetask2")
    a = _WritingAgent("claude", "BAD\n")
    try:
        r = RouteStrategy().execute(
            task=Task(prompt="do it"),
            agents=[a],
            memory=FakeMemory(),
            pool=pool,
            config=SyncmillConfig(),
            gate=gate,
        )
        assert r.status is TaskStatus.FAILED
        assert "[claude] quality gate failed" in r.error
    finally:
        pool.cleanup_all()


def test_compete_real_gate_missing_binary_aggregates(git_repo):
    # real CommandGate whose command doesn't exist: the agent COMPLETES but the
    # gate's "command not found" failure must flow into the Compete aggregate.
    from syncmill.gates import build_gate
    from syncmill.config import GateConfig
    from syncmill.worktree import WorktreePool

    gate = build_gate(GateConfig(commands=["definitely-not-a-real-binary-xyz"]))
    pool = WorktreePool(git_repo, "competegate")
    a = _WritingAgent("claude", "anything\n")
    try:
        r = CompeteStrategy().execute(
            task=Task(prompt="do it"),
            agents=[a],
            memory=FakeMemory(),
            pool=pool,
            config=SyncmillConfig(),
            gate=gate,
        )
        assert r.status is TaskStatus.FAILED
        assert "[claude] quality gate failed" in r.error
        # the real failing command is named (not a bogus exit_code=0)
        assert "definitely-not-a-real-binary-xyz" in r.error
    finally:
        pool.cleanup_all()


def test_pipeline_real_gate_sees_final_cumulative_tree(git_repo):
    # two stages share one worktree; stage 1 writes GOOD, stage 2 overwrites with
    # BAD. The gate runs once on the FINAL tree (BAD) → fails — proving it gates
    # the cumulative result, not an intermediate stage.
    from syncmill.gates import build_gate
    from syncmill.config import GateConfig
    from syncmill.worktree import WorktreePool

    gate = build_gate(GateConfig(commands=["grep -q GOOD result.txt"]))
    pool = WorktreePool(git_repo, "pipegate")
    a = _WritingAgent("claude", "intermediate GOOD\n")
    b = _WritingAgent("codex", "final BAD\n")
    try:
        r = PipelineStrategy().execute(
            task=Task(prompt="do it"),
            agents=[a, b],
            memory=FakeMemory(),
            pool=pool,
            config=SyncmillConfig(),
            gate=gate,
        )
        assert r.status is TaskStatus.FAILED  # final BAD fails the gate
        assert "quality gate failed" in r.error
    finally:
        pool.cleanup_all()


def test_pipeline_real_gate_passes_on_good_final(git_repo):
    from syncmill.gates import build_gate
    from syncmill.config import GateConfig
    from syncmill.worktree import WorktreePool

    gate = build_gate(GateConfig(commands=["grep -q GOOD result.txt"]))
    pool = WorktreePool(git_repo, "pipegate2")
    a = _WritingAgent("claude", "intermediate BAD\n")
    b = _WritingAgent("codex", "final GOOD\n")
    try:
        r = PipelineStrategy().execute(
            task=Task(prompt="do it"),
            agents=[a, b],
            memory=FakeMemory(),
            pool=pool,
            config=SyncmillConfig(),
            gate=gate,
        )
        assert r.status is TaskStatus.COMPLETED
        assert r.agent_id == "codex"
        assert r.gate is not None and r.gate.passed is True
        assert r.files_changed == ["result.txt"]
    finally:
        pool.cleanup_all()


def test_council_real_gate_selects_passing_synthesizer(git_repo):
    # End-to-end with REAL git worktrees + a REAL grep gate: every member writes
    # result.txt in its OWN worktree across all phases (concurrent fan-out + reuse),
    # and the gate runs on the synthesizers' real diffs — claude's BAD synthesis is
    # rejected, so the gate-passing codex wins with its real file diff.
    from syncmill.gates import build_gate
    from syncmill.config import GateConfig
    from syncmill.worktree import WorktreePool

    gate = build_gate(GateConfig(commands=["grep -q GOOD result.txt"]))
    pool = WorktreePool(git_repo, "counciltask")
    a = _WritingAgent("claude", "this is BAD\n")
    b = _WritingAgent("codex", "this is GOOD\n")
    try:
        r = CouncilStrategy().execute(
            task=Task(prompt="do it"),
            agents=[a, b],
            memory=FakeMemory(),
            pool=pool,
            config=SyncmillConfig(),
            gate=gate,
        )
        assert r.status is TaskStatus.COMPLETED
        assert r.agent_id == "codex"  # claude's synthesized diff failed the real gate
        assert r.gate is not None and r.gate.passed is True
        assert r.files_changed == ["result.txt"]  # the winner's OWN real worktree diff
        # each member ran all 3 phases against its own worktree (concurrent + reused)
        assert len(a.calls) == 3 and len(b.calls) == 3
        assert len({c["workdir"] for c in b.calls}) == 1
    finally:
        pool.cleanup_all()


# ── RouteStrategy + circuit breaker (#8) ─────────────────────────────────


def test_route_skips_open_agent_and_falls_back():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="win")
    breaker = FakeBreaker(open_agents={"claude"})  # claude's circuit is OPEN
    r = _run_route([a, b], FakeMemory(), breaker=breaker)
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"
    assert a.calls == []  # skipped reactively — never run, no liveness probe
    assert len(b.calls) == 1


def test_route_records_each_run_agents_raw_outcome():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="x")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    breaker = FakeBreaker()
    _run_route([a, b], FakeMemory(), breaker=breaker)
    # both ran (claude failed → fell back to codex); both outcomes recorded
    assert breaker.recorded == [
        ("claude", TaskStatus.FAILED),
        ("codex", TaskStatus.COMPLETED),
    ]


def test_route_does_not_record_skipped_open_agent():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    breaker = FakeBreaker(open_agents={"claude"})
    _run_route([a, b], FakeMemory(), breaker=breaker)
    # claude was skipped (not run), so nothing to record for it
    assert breaker.recorded == [("codex", TaskStatus.COMPLETED)]


def test_route_all_open_returns_breaker_failure():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    breaker = FakeBreaker(open_agents={"claude", "codex"})
    r = _run_route([a, b], FakeMemory(), breaker=breaker)
    assert r.status is TaskStatus.FAILED
    assert r.agent_id == "none"
    assert "circuit-open" in r.error
    assert "claude" in r.error and "codex" in r.error
    assert a.calls == [] and b.calls == []  # nobody ran


def test_route_gate_failure_does_not_trip_breaker():
    # the design decision: the breaker records RAW execution status (pre-gate),
    # so a gate rejection (a quality verdict on a working agent) is NOT a failure
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    breaker = FakeBreaker()
    r = _run_route([a], FakeMemory(), gate=FakeGate(passes=False), breaker=breaker)
    assert r.status is TaskStatus.FAILED  # gate rejected the diff
    assert breaker.recorded == [("claude", TaskStatus.COMPLETED)]  # but it RAN


def test_route_breaker_none_records_nothing_and_runs_all():
    # belt-and-suspenders: with no breaker, behavior is exactly as before
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="x")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    r = _run_route([a, b], FakeMemory(), breaker=None)
    assert r.agent_id == "codex"
    assert len(a.calls) == 1 and len(b.calls) == 1


def test_route_retries_open_agent_after_cooldown(tmp_path):
    # The headline criterion end-to-end through the strategy with a REAL breaker:
    # OPEN → skipped during cooldown → retried (half-open) once it elapses →
    # success closes the circuit. (FakeBreaker can't model time; a real breaker
    # with an injected clock can.)
    from syncmill.breaker import CircuitBreaker

    t = [1000.0]
    breaker = CircuitBreaker(1, 100, str(tmp_path / "b.json"), now=lambda: t[0])
    breaker.record("claude", TaskStatus.FAILED)  # OPEN at t=1000

    # still cooling down → the only agent is skipped, so the run reports all-open
    a_cooling = FakeAgent("claude", status=TaskStatus.COMPLETED)
    t[0] = 1099.0
    r_skip = _run_route([a_cooling], FakeMemory(), breaker=breaker)
    assert r_skip.status is TaskStatus.FAILED and "circuit-open" in r_skip.error
    assert a_cooling.calls == []  # never run

    # cooldown elapsed → half-open: the agent runs again and succeeds → closes
    a_retry = FakeAgent("claude", status=TaskStatus.COMPLETED, output="recovered")
    t[0] = 1100.0
    r_retry = _run_route([a_retry], FakeMemory(), breaker=breaker)
    assert r_retry.status is TaskStatus.COMPLETED
    assert len(a_retry.calls) == 1  # retried after the cooldown window
    assert breaker.allows("claude") is True  # the success reset/closed it


# ── PipelineStrategy + circuit breaker (#8) ──────────────────────────────


def test_pipeline_skips_open_stage_and_chains_the_rest():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="s1")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="s2")
    pool = FakeWorktreePool()
    breaker = FakeBreaker(open_agents={"claude"})
    r = _run_pipeline([a, b], FakeMemory(), pool=pool, breaker=breaker)
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"  # claude's stage skipped; codex is the chain
    assert a.calls == []
    assert pool.acquired == ["codex"]  # shared worktree keyed off first RUNNABLE
    assert breaker.recorded == [("codex", TaskStatus.COMPLETED)]


def test_pipeline_records_each_stage_raw_outcome():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="s1")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="s2")
    breaker = FakeBreaker()
    _run_pipeline([a, b], FakeMemory(), breaker=breaker)
    assert breaker.recorded == [
        ("claude", TaskStatus.COMPLETED),
        ("codex", TaskStatus.COMPLETED),
    ]


def test_pipeline_records_broken_stage_then_stops():
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="boom")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    breaker = FakeBreaker()
    r = _run_pipeline([a, b], FakeMemory(), breaker=breaker)
    assert r.status is TaskStatus.FAILED
    assert b.calls == []  # fail-fast: stage 2 never ran
    assert breaker.recorded == [("claude", TaskStatus.FAILED)]  # only the run stage


def test_pipeline_all_open_returns_breaker_failure():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    breaker = FakeBreaker(open_agents={"claude", "codex"})
    r = _run_pipeline([a, b], FakeMemory(), breaker=breaker)
    assert r.status is TaskStatus.FAILED
    assert "circuit-open" in r.error
    assert a.calls == [] and b.calls == []


def test_pipeline_gate_failure_does_not_trip_breaker():
    # the final-stage gate runs on a distinct code path from Route's; prove the
    # same invariant there — a gate rejection records the RAW (COMPLETED) status
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="s1")
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="s2")
    breaker = FakeBreaker()
    r = _run_pipeline([a, b], FakeMemory(), gate=FakeGate(passes=False), breaker=breaker)
    assert r.status is TaskStatus.FAILED  # final gate failed the chain
    assert breaker.recorded == [
        ("claude", TaskStatus.COMPLETED),
        ("codex", TaskStatus.COMPLETED),
    ]


# ── CompeteStrategy + circuit breaker (#8) ───────────────────────────────


def test_compete_fans_out_only_allowed_agents():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED, output="win")
    pool = FakeWorktreePool()
    breaker = FakeBreaker(open_agents={"claude"})
    r = _run_compete([a, b], FakeMemory(), pool=pool, breaker=breaker)
    assert r.agent_id == "codex"
    assert a.calls == []  # open agent never entered the fan-out
    assert pool.acquired == ["codex"]  # no worktree wasted on the open agent


def test_compete_records_all_competitors_winner_and_loser():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED, output="win")
    b = FakeAgent("codex", status=TaskStatus.FAILED, error="crash")
    breaker = FakeBreaker()
    r = _run_compete([a, b], FakeMemory(), breaker=breaker)
    assert r.agent_id == "claude"  # higher-priority completer wins
    # BOTH outcomes recorded (winner + loser), so a repeatedly-crashing loser
    # accumulates failures across runs even when a peer keeps winning
    assert sorted(breaker.recorded) == [
        ("claude", TaskStatus.COMPLETED),
        ("codex", TaskStatus.FAILED),
    ]


def test_compete_all_open_returns_breaker_failure():
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    pool = FakeWorktreePool()
    breaker = FakeBreaker(open_agents={"claude", "codex"})
    r = _run_compete([a, b], FakeMemory(), pool=pool, breaker=breaker)
    assert r.status is TaskStatus.FAILED
    assert "circuit-open" in r.error
    assert a.calls == [] and b.calls == []
    assert pool.acquired == []  # no worktrees acquired at all


def test_compete_gate_failure_does_not_trip_breaker():
    # the gate runs inside _select_winner (distinct path); recording happens
    # BEFORE it, so two gate-rejected completers are still recorded as COMPLETED
    a = FakeAgent("claude", status=TaskStatus.COMPLETED)
    b = FakeAgent("codex", status=TaskStatus.COMPLETED)
    breaker = FakeBreaker()
    r = _run_compete([a, b], FakeMemory(), gate=FakeGate(passes=False), breaker=breaker)
    assert r.status is TaskStatus.FAILED  # all gate-rejected → no winner
    assert sorted(breaker.recorded) == [
        ("claude", TaskStatus.COMPLETED),
        ("codex", TaskStatus.COMPLETED),
    ]


# ── Supervisor + real CircuitBreaker, end-to-end across runs ──────────────


def test_run_breaker_opens_across_runs_and_skips_agent(patched_worktree, tmp_path):
    # The headline acceptance criterion: an agent that fails N times IN A ROW is
    # skipped for the cooldown window. N only accumulates across separate runs
    # (each is a fresh process loading the persisted state file), so we rebuild
    # the breaker from config before each run to mimic that.
    from syncmill.breaker import build_breaker
    from syncmill.config import BreakerConfig

    cfg = SyncmillConfig()
    cfg.breaker = BreakerConfig(
        enabled=True,
        failure_threshold=2,
        cooldown_seconds=10_000,  # large: stays OPEN for the test
        state_path=str(tmp_path / "breaker.json"),
    )

    def run_failing():
        agent = FakeAgent("claude", status=TaskStatus.FAILED, error="x")
        sup = _supervisor(
            FakeRegistry([agent]), FakeMemory(),
            config=cfg, breaker=build_breaker(cfg.breaker),
        )
        return agent, sup.run(Task(prompt="go"))

    a1, r1 = run_failing()  # failure 1/2
    assert r1.status is TaskStatus.FAILED and len(a1.calls) == 1
    a2, r2 = run_failing()  # failure 2/2 → circuit opens, persisted
    assert r2.status is TaskStatus.FAILED and len(a2.calls) == 1

    a3, r3 = run_failing()  # now OPEN → skipped before running
    assert r3.status is TaskStatus.FAILED
    assert "circuit-open" in r3.error
    assert a3.calls == []  # the persisted OPEN state skipped it


def test_run_breaker_success_resets_across_runs(patched_worktree, tmp_path):
    from syncmill.breaker import build_breaker
    from syncmill.config import BreakerConfig

    cfg = SyncmillConfig()
    cfg.breaker = BreakerConfig(
        enabled=True, failure_threshold=2, cooldown_seconds=10_000,
        state_path=str(tmp_path / "breaker.json"),
    )

    def run_one(status):
        agent = FakeAgent("claude", status=status, error=None if status is TaskStatus.COMPLETED else "x")
        sup = _supervisor(
            FakeRegistry([agent]), FakeMemory(),
            config=cfg, breaker=build_breaker(cfg.breaker),
        )
        return agent, sup.run(Task(prompt="go"))

    run_one(TaskStatus.FAILED)  # 1/2
    run_one(TaskStatus.COMPLETED)  # success resets the consecutive count
    a3, r3 = run_one(TaskStatus.FAILED)  # 1/2 again, not open
    assert r3.status is TaskStatus.FAILED
    assert len(a3.calls) == 1  # still allowed to run — the reset stuck


# ── Capability matching (#9) — _matched_order + Supervisor.run integration ─


def test_matched_order_none_passthrough():
    # no matcher → identity (a fresh list in the same order)
    agents = [FakeAgent("claude"), FakeAgent("codex")]
    out = S._matched_order(agents, Task(prompt="go"), None)
    assert [a.agent_id for a in out] == ["claude", "codex"]
    assert out is not agents  # a copy, not the same object


def test_matched_order_delegates_to_matcher():
    agents = [FakeAgent("claude"), FakeAgent("codex")]
    matcher = FakeMatcher(promote=["codex"])
    out = S._matched_order(agents, Task(prompt="go"), matcher)
    assert [a.agent_id for a in out] == ["codex", "claude"]
    assert matcher.calls == [["claude", "codex"]]  # saw the candidate list


def test_run_reorders_agents_before_strategy(patched_worktree):
    # the integration point: Supervisor.run re-orders the candidate list with the
    # matcher BEFORE the strategy runs, so Route tries the promoted agent first.
    mem = FakeMemory()
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", output="A"), FakeAgent("codex", output="B")]),
        mem,
        matcher=FakeMatcher(promote=["codex"]),
    )
    r = sup.run(Task(prompt="go", tags=["python"]))
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"  # promoted ahead of higher-priority claude
    assert r.output == "B"
    assert patched_worktree["pool"].acquired == ["codex"]  # Route stopped at the winner


def test_run_no_matcher_leaves_priority_order(patched_worktree):
    # matcher=None (default) → raw registry order reaches the strategy.
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude", output="A"), FakeAgent("codex", output="B")]),
        FakeMemory(),
    )
    r = sup.run(Task(prompt="go", tags=["python"]))
    assert r.agent_id == "claude"  # priority order, hint ignored without a matcher
    assert patched_worktree["pool"].acquired == ["claude"]


def test_run_forced_agent_skips_matching(patched_worktree):
    # forcing one agent has no ordering to influence — the matcher is never called.
    matcher = FakeMatcher(promote=["kimi-code"])
    sup = _supervisor(
        FakeRegistry([FakeAgent("claude"), FakeAgent("codex", output="forced"),
                      FakeAgent("kimi-code")]),
        FakeMemory(),
        matcher=matcher,
    )
    r = sup.run(Task(prompt="go", tags=["python"]), agent_id="codex")
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "codex"
    assert matcher.calls == []  # forced path never reaches _matched_order


def test_run_matcher_composes_with_breaker(patched_worktree):
    # MATCH → BREAKER-FILTER: the matcher promotes codex to the front, but codex's
    # circuit is OPEN, so _breaker_runnable drops it and the next-in-order agent
    # runs. Proves preference is computed first, then availability prunes it.
    a_claude = FakeAgent("claude", output="claude-out")
    a_codex = FakeAgent("codex", output="codex-out")
    breaker = FakeBreaker(open_agents={"codex"})
    sup = _supervisor(
        FakeRegistry([a_claude, a_codex]),
        FakeMemory(),
        breaker=breaker,
        matcher=FakeMatcher(promote=["codex"]),
    )
    r = sup.run(Task(prompt="go", tags=["python"]))
    assert r.status is TaskStatus.COMPLETED
    assert r.agent_id == "claude"  # promoted codex was OPEN → fell through to claude
    assert a_codex.calls == []  # never ran (circuit open)
    # the breaker only recorded the agent that actually ran (the matched-but-open
    # one is skipped, never recorded)
    assert breaker.recorded == [("claude", TaskStatus.COMPLETED)]


def test_supervisor_builds_matcher_from_config(stub_supervisor_deps):
    from syncmill.config import CapabilityConfig
    from syncmill.matcher import DeclarativeMatcher

    # default config → no declared capabilities → None (disabled, like the gate)
    assert Supervisor(SyncmillConfig())._matcher is None
    # declared capabilities → a real matcher built once in __init__
    cfg = SyncmillConfig(capabilities=CapabilityConfig(agents={"codex": ["python"]}))
    assert isinstance(Supervisor(cfg)._matcher, DeclarativeMatcher)


# ── tmux monitor (#16): the strategy + Supervisor wiring ──────────────────


def _completed(agent_id="claude"):
    return TaskResult(
        task_id=uuid.uuid4(),
        agent_id=agent_id,
        status=TaskStatus.COMPLETED,
        output="ok",
        exit_code=0,
        duration_seconds=0.5,
    )


def test_compete_drives_monitor():
    mon = FakeMonitor()
    a, b = FakeAgent("claude"), FakeAgent("codex")
    _run_compete([a, b], FakeMemory(), pool=FakeWorktreePool(), monitor=mon)
    # the EXACT sequence: announce the phase, open streaming over both competitors
    # (the fan-out runs between enter and exit), close it, then report each raw
    # status — so the streamed fan-out is provably wrapped by the seam.
    assert mon.events == [
        ("phase", "compete"),
        ("stream-enter", ["claude", "codex"]),
        ("stream-exit", ["claude", "codex"]),
        ("status", "claude", TaskStatus.COMPLETED),
        ("status", "codex", TaskStatus.COMPLETED),
    ]
    assert mon.max_stream_depth == 1 and mon.stream_depth == 0


def test_compete_reports_raw_status_including_failer():
    mon = FakeMonitor()
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="boom")
    b = FakeAgent("codex")
    _run_compete([a, b], FakeMemory(), pool=FakeWorktreePool(), monitor=mon)
    # raw per-agent outcome, in runnable order — the loser is reported too
    assert mon.statuses == [
        ("claude", TaskStatus.FAILED),
        ("codex", TaskStatus.COMPLETED),
    ]


def test_council_drives_monitor_each_phase():
    mon = FakeMonitor()
    a = FakeAgent("claude", output="A")
    b = FakeAgent("codex", output="B")
    _run_council([a, b], FakeMemory(), pool=FakeWorktreePool(), monitor=mon)
    # propose → 1 critique round → synthesize, each a streamed phase
    assert mon.phases == ["propose", "critique 1/1", "synthesize"]
    # 2 members × 3 phases = 6 status reports; streaming opened once per phase, each
    # over BOTH members (asserting the id payload, not just the count).
    assert len(mon.statuses) == 6
    assert [e[1] for e in mon.events if e[0] == "stream-enter"] == [
        ["claude", "codex"],
        ["claude", "codex"],
        ["claude", "codex"],
    ]
    assert mon.stream_depth == 0


def test_decompose_plan_is_status_only_no_pane():
    mon = FakeMonitor()
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]', output="A")
    b = _DecomposeAgent("codex", output="B")
    _run_decompose([a, b], FakeMemory(), pool=FakeWorktreePool(), monitor=mon)
    assert mon.phases == ["plan", "subtasks", "synthesize"]
    # the sync planner phase is status-only (no streaming seam on execute) — so
    # streaming opened only for subtasks + synthesize, NOT for plan. Two subtasks
    # round-robin across [claude, codex]; synthesis runs all runnable.
    assert [e[1] for e in mon.events if e[0] == "stream-enter"] == [
        ["claude", "codex"],  # subtasks (round-robin assignment)
        ["claude", "codex"],  # synthesize (all runnable)
    ]
    # the planner's outcome is still reported (status-only, no pane)
    assert ("status", "claude", TaskStatus.COMPLETED) in mon.events


def test_route_ignores_monitor():
    mon = FakeMonitor()
    _run_route([FakeAgent("claude")], FakeMemory(), monitor=mon)
    assert mon.events == []  # sequential: nothing concurrent to watch


def test_pipeline_ignores_monitor():
    mon = FakeMonitor()
    _run_pipeline(
        [FakeAgent("claude", output="s1"), FakeAgent("codex", output="s2")],
        FakeMemory(),
        monitor=mon,
    )
    assert mon.events == []


def test_supervisor_builds_no_monitor_by_default(patched_worktree):
    # inner_dir() is None in a normal run (env stripped) → strategy gets monitor=None.
    seen = {}

    class _Rec:
        def execute(self, *, task, agents, memory, pool, config, gate=None,
                    breaker=None, monitor=None, tracer=None):
            seen["monitor"] = monitor
            return _completed()

    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), strategy=_Rec())
    sup.run(Task(prompt="go"))
    assert seen["monitor"] is None


def test_supervisor_injects_monitor_and_finishes_in_inner_mode(patched_worktree, monkeypatch):
    fake_mon = FakeMonitor()
    seen = {}
    monkeypatch.setattr(S, "inner_dir", lambda: "/run-dir")

    def fake_build(cfg, *, monitor_dir):
        seen["dir"] = monitor_dir
        return fake_mon

    monkeypatch.setattr(S, "build_monitor", fake_build)
    result = _completed()

    class _Rec:
        def execute(self, *, task, agents, memory, pool, config, gate=None,
                    breaker=None, monitor=None, tracer=None):
            seen["monitor"] = monitor
            return result

    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), strategy=_Rec())
    sup.run(Task(prompt="go"))
    assert seen["dir"] == "/run-dir"  # built from inner_dir()
    assert seen["monitor"] is fake_mon  # injected into the strategy
    assert fake_mon.finished == [result]  # finish() called with the winner


def test_supervisor_finishes_monitor_with_failure_when_strategy_crashes(
    patched_worktree, monkeypatch
):
    # A crashed INNER run (strategy raises) must still finalize the monitor with a
    # FAILED result.json, so the OUTER reads a real failure — not "detached, exit 0".
    fake_mon = FakeMonitor()
    monkeypatch.setattr(S, "inner_dir", lambda: "/run-dir")
    monkeypatch.setattr(S, "build_monitor", lambda cfg, *, monitor_dir: fake_mon)

    class _Boom:
        def execute(self, **kw):
            raise RuntimeError("kaboom")

    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), strategy=_Boom())
    with pytest.raises(RuntimeError):
        sup.run(Task(prompt="go"))
    assert len(fake_mon.finished) == 1
    assert fake_mon.finished[0].status is TaskStatus.FAILED


def test_concurrent_strategies_derived_from_registry():
    from syncmill.strategies import (
        CONCURRENT_STRATEGIES,
        SEQUENTIAL_STRATEGIES,
        _STRATEGIES,
    )

    # derived = every registered strategy minus the sequential ones, so a newly
    # registered concurrent strategy is monitorable automatically (no silent drift)
    assert CONCURRENT_STRATEGIES == set(_STRATEGIES) - SEQUENTIAL_STRATEGIES
    assert CONCURRENT_STRATEGIES == {"compete", "council", "decompose"}
    assert SEQUENTIAL_STRATEGIES.isdisjoint(CONCURRENT_STRATEGIES)


def test_supervisor_monitor_finish_failure_does_not_strand_cleanup(
    patched_worktree, monkeypatch
):
    # The monitor is observability: a finish() failure (e.g. result.json write
    # error) must NEVER skip the memory-session close or worktree cleanup.
    class _RaisingMonitor(FakeMonitor):
        def finish(self, result):
            raise OSError("disk full")

    mon = _RaisingMonitor()
    monkeypatch.setattr(S, "inner_dir", lambda: "/run-dir")
    monkeypatch.setattr(S, "build_monitor", lambda cfg, *, monitor_dir: mon)
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED  # the run result is unaffected
    assert patched_worktree["pool"].cleaned  # cleanup ran despite finish() raising
    assert any(e.startswith("end:") for e in mem.events)  # session still ended


# ── Supervisor.run_item (board run lifecycle, Phase 1) ─────────────────────


def _item(item_id="item-1", task="build the parser", goal="g", **kw):
    return WorkItem(id=item_id, goal=goal, task=task, **kw)


def test_run_item_claims_enriches_records_and_shares(patched_worktree):
    board = FakeBoard(item=_item())
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="ok")]), mem, board=board)
    r = sup.run_item(item_id="item-1")
    assert r is not None and r.status is TaskStatus.COMPLETED
    assert board.recovered == 1  # stale claims swept before claiming
    assert board.claimed and board.claimed[0][0] == "item-1"  # claimed by id
    assert len(board.applied) == 1  # the run outcome recorded once
    applied_id, parsed, result, actor, run_id = board.applied[0]
    assert applied_id == "item-1" and actor == "claude"
    assert run_id == board.claimed[0][1]  # lease-fenced with the claim's run_id
    # the agent saw the DETERMINISTIC item prompt (task + update convention)
    prompt = sup._registry.get("claude").calls[0]["prompt"]
    assert "Task: build the parser" in prompt
    assert "syncmill-board" in prompt


def test_run_item_passes_lease_run_id_into_run(patched_worktree, monkeypatch):
    # No double issuance: the run_id that stamped the board lease (and named the
    # `board-run:{run_id}` scope) is the SAME id run() resolves — run_item passes
    # it through run(run_id=), so lease/scope/envelope share one identity.
    resolved = []

    def recorder(explicit=None):
        resolved.append(explicit)
        return explicit or "rid-should-not-mint"

    monkeypatch.setattr(S, "resolve_run_id", recorder)
    board = FakeBoard(item=_item())
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    sup.run_item(item_id="item-1")
    assert resolved == [board.claimed[0][1]]  # board lease id == run kwarg id


def test_run_item_board_scoped_memory_keeps_enriched_prompt_off_global_shared(
    patched_worktree,
):
    # THE leak guard: a board run's strategy-internal share + session prompt-log go
    # to the per-run SCOPED memory, never the parent's global `shared`; only the
    # sanitized board-result summary reaches global shared.
    board = FakeBoard(item=_item(task="secret enriched task"))
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="agent did it")]), mem, board=board)
    sup.run_item(item_id="item-1")
    # global (parent) shared got NOTHING from the run itself
    assert mem.shared == []
    # the run used a board-scoped child for register/session/share
    assert len(mem.scoped_children) == 1
    child = mem.scoped_children[0]
    assert child._scope == f"board-run:{board.claimed[0][1]}"
    assert any(e.startswith("register:") for e in child.events)
    assert len(child.shared) == 1  # the COMPLETED run shared to the per-run scope
    # exactly ONE sanitized summary reached global shared, built from task + output
    assert len(mem.board_results) == 1
    iid, task_text, stripped = mem.board_results[0]
    assert iid == "item-1" and task_text == "secret enriched task"
    assert stripped == "agent did it"  # the sanitized output, not the enriched prompt
    assert "syncmill-board" not in stripped  # update convention never leaks out


def test_run_item_no_id_uses_next_runnable(patched_worktree):
    board = FakeBoard(next_item=_item(item_id="auto"))
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    r = sup.run_item()
    assert r is not None and r.status is TaskStatus.COMPLETED
    assert board.dispatched and not board.claimed  # next_runnable, not claim
    assert board.dispatched[0][1] is None  # a bare call scopes to no goal
    assert board.applied[0][0] == "auto"


def test_run_item_goal_threads_to_next_runnable(patched_worktree):
    # the board run --goal filter (Phase 4): goal reaches the atomic claim
    board = FakeBoard(next_item=_item(item_id="auto"))
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    r = sup.run_item(goal="auth")
    assert r is not None
    assert board.dispatched == [(board.applied[0][4], "auth")]


def test_run_item_claim_by_id_ignores_goal(patched_worktree):
    # the CLI makes --id/--goal mutually exclusive; the id path never consults
    # next_runnable, so a stray goal can't change which item is claimed
    board = FakeBoard(item=_item(item_id="item-1"))
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    r = sup.run_item(item_id="item-1", goal="auth")
    assert r is not None
    assert board.claimed and not board.dispatched  # claim, never next_runnable


def test_run_item_nothing_to_claim_returns_none(patched_worktree):
    board = FakeBoard()  # claim/next_runnable both yield None
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    assert sup.run_item(item_id="ghost") is None
    assert sup.run_item() is None
    assert board.applied == []  # never ran anything
    assert patched_worktree["pool"].acquired == []  # no worktree acquired


def test_run_item_strips_board_block_from_global_share(patched_worktree):
    out = "did the work\n```syncmill-board\n{\"state\": \"done\"}\n```\nbye"
    board = FakeBoard(item=_item())
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output=out)]), mem, board=board)
    sup.run_item(item_id="item-1")
    # apply_update saw the PARSED block; the global share saw the STRIPPED output
    parsed = board.applied[0][1]
    assert parsed == {"state": "done"}
    _, _, stripped = mem.board_results[0]
    assert stripped == "did the work\nbye"


def test_run_item_injects_active_decisions_into_prompt(patched_worktree):
    # D1: run_item fetches the item-goal's active decisions and injects them
    # DETERMINISTICALLY into the prompt (the agent must respect, not re-litigate them).
    board = FakeBoard(item=_item(goal="epic"))
    board.decisions = [
        Decision(id="d1", text="target python 3.12", scope="epic"),
        Decision(id="d2", text="never force-push", scope=""),
    ]
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    sup.run_item(item_id="item-1")
    prompt = sup._registry.get("claude").calls[0]["prompt"]
    assert "target python 3.12" in prompt and "never force-push" in prompt
    assert "do not re-litigate" in prompt.lower()


def test_run_item_decision_read_failure_degrades_not_blocks(patched_worktree):
    # A decisions read failure is advisory enrichment: it degrades to no decisions and
    # the item STILL runs — it must never block the item or mask the run.
    class _DecisionsBoom(FakeBoard):
        def active_decisions(self, goal=None):
            raise BoardError("decisions table corrupt")

    board = _DecisionsBoom(item=_item(goal="epic"))
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="ok")]), FakeMemory(),
                      board=board)
    r = sup.run_item(item_id="item-1")
    assert r is not None and r.status is TaskStatus.COMPLETED  # ran without decisions
    assert board.transitioned == []  # NOT crash-blocked
    prompt = sup._registry.get("claude").calls[0]["prompt"]
    assert "re-litigate" not in prompt.lower()  # no decisions section


def test_run_item_lost_lease_does_not_share_to_global(patched_worktree):
    # CODEX FIX: a slow run whose item was reclaimed + re-dispatched while it ran
    # has its apply_update DROPPED by the lease fence (applied=False). run_item must
    # gate the global-`shared` cross-link on that signal — otherwise the superseded
    # run still publishes its discarded output to global shared under
    # `board-result:{item_id}`, polluting the cross-link beside the new owner's
    # summary (breaking the "single sanitized summary" contract).
    board = FakeBoard(item=_item(), apply_applied=False)  # lease lost → write dropped
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="stale work")]), mem, board=board)
    r = sup.run_item(item_id="item-1")
    assert r is not None and r.status is TaskStatus.COMPLETED  # the run still returns
    assert len(board.applied) == 1                             # it DID attempt the write
    assert mem.board_results == []                             # but NOTHING reached global shared


def test_run_item_applied_lease_shares_to_global(patched_worktree):
    # the complement: when apply_update reports applied=True (the normal, lease-held
    # path), the single sanitized summary DOES reach global shared.
    board = FakeBoard(item=_item(), apply_applied=True)
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude", output="good work")]), mem, board=board)
    sup.run_item(item_id="item-1")
    assert len(mem.board_results) == 1  # the live owner's summary reached global shared


def test_run_item_crash_blocks_and_reraises(patched_worktree):
    board = FakeBoard(item=_item(), apply_raises=RuntimeError("kaboom"))
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    with pytest.raises(RuntimeError, match="kaboom"):
        sup.run_item(item_id="item-1")
    # the crashed item is moved OUT of running → blocked, not stranded
    assert board.transitioned and board.transitioned[0][1] == WorkState.BLOCKED.value
    # the crash-block is LEASE-FENCED: the run's run_id is threaded into transition
    # (matching the claim/apply_update run_id) so a reclaimed item isn't clobbered
    crash_run_id = board.transitioned[0][3]
    assert crash_run_id is not None and crash_run_id == board.claimed[0][1]
    assert crash_run_id == board.applied[0][4]
    assert patched_worktree["pool"].cleaned  # the run's worktrees still torn down


def test_run_item_disabled_board_raises():
    # board disabled (default) → _require_board raises BoardError (clear message)
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=None)
    with pytest.raises(BoardError, match="disabled"):
        sup.run_item(item_id="x")


def test_plain_run_never_builds_the_board(patched_worktree, tmp_path):
    # the lazy-build guarantee: a plain run() must NOT touch the board, so even an
    # enabled-but-corrupt board can't break it. _board stays None after a run.
    cfg = SyncmillConfig()
    cfg.board.enabled = True
    cfg.board.db_path = str(tmp_path / "would-be-corrupt.db")
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), config=cfg, board=None)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert sup._board is None  # never built — run() never calls _require_board


def test_close_closes_lazy_board():
    board = FakeBoard(item=_item())
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem, board=board)
    sup.close()
    assert board.closed is True
    assert "close" in mem.events


def test_close_without_board_is_safe():
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem, board=None)
    sup.close()  # no board built → no-op, doesn't raise
    assert "close" in mem.events


def test_run_item_crash_block_failure_does_not_mask_original(patched_worktree):
    # _block_on_crash is best-effort: if the crash-blocking transition ITSELF fails,
    # the ORIGINAL orchestration exception must still propagate (not the board error).
    board = FakeBoard(
        item=_item(),
        apply_raises=RuntimeError("original-kaboom"),
        transition_raises=RuntimeError("board-also-down"),
    )
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    with pytest.raises(RuntimeError, match="original-kaboom"):
        sup.run_item(item_id="item-1")
    assert board.transitioned  # the crash-block transition was attempted
    assert patched_worktree["pool"].cleaned  # worktrees still torn down despite both failing


def test_plain_run_unaffected_by_a_genuinely_corrupt_board(patched_worktree, tmp_path):
    # the lazy-build guarantee, exercised with REAL corrupt bytes on disk: a plain
    # run never opens the (enabled, corrupt) board, so it completes and _board stays None.
    db = tmp_path / "corrupt.db"
    db.write_text("this is not a sqlite database " * 50)
    cfg = SyncmillConfig()
    cfg.board.enabled = True
    cfg.board.db_path = str(db)
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), config=cfg, board=None)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert sup._board is None  # the corrupt board was never opened


# ── run_item board-owned scratch namespace GC (memory-backend P2) ───────────


def test_run_item_gc_deletes_and_acknowledges_per_run_success(patched_worktree):
    board = FakeBoard(item=_item(), scratch_candidates=["dead1", "dead2"])
    mem = FakeMemory()
    mem.delete_fail_ids = {"dead2"}
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem, board=board)
    r = sup.run_item(item_id="item-1")
    assert r is not None and r.status is TaskStatus.COMPLETED
    assert mem.purged == [["dead1", "dead2"]]
    assert board.marked_purged == [{"dead1"}]
    assert board.last_scratch_limit == 100


def test_run_item_gc_order_precedes_scope_and_claim(patched_worktree):
    order: list[str] = []

    class _Mem(FakeMemory):
        async def delete_board_runs(self, run_ids):
            order.append("delete")
            return await super().delete_board_runs(run_ids)

        def scoped(self, run_id):
            order.append("scope")
            return super().scoped(run_id)

    class _Board(FakeBoard):
        def recover_stale_running(self, lease_timeout=None):
            order.append("recover")
            return super().recover_stale_running(lease_timeout)

        def collectible_board_run_ids(self, *, limit=100):
            order.append("candidates")
            return super().collectible_board_run_ids(limit=limit)

        def mark_board_run_namespaces_purged(self, run_ids):
            order.append("mark")
            return super().mark_board_run_namespaces_purged(run_ids)

        def claim(self, item_id, run_id):
            order.append("claim")
            return super().claim(item_id, run_id)

        def reactivate_board_run_namespace(self, run_id):
            order.append("reactivate")
            return super().reactivate_board_run_namespace(run_id)

    sup = _supervisor(
        FakeRegistry([FakeAgent("claude")]),
        _Mem(),
        board=_Board(item=_item(), scratch_candidates=["dead1"]),
    )
    sup.run_item(item_id="item-1")
    assert order == [
        "recover", "candidates", "delete", "mark", "scope", "claim", "reactivate"
    ]


def test_run_item_gc_empty_candidates_skips_memory(patched_worktree):
    board = FakeBoard(item=_item())
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem, board=board)
    sup.run_item(item_id="item-1")
    assert board.scratch_reads == 1
    assert mem.purged == []
    assert board.marked_purged == []


def test_run_item_gc_board_error_never_blocks_dispatch(patched_worktree):
    board = FakeBoard(
        item=_item(), scratch_raises=BoardError("scratch ledger boom")
    )
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem, board=board)
    r = sup.run_item(item_id="item-1")
    assert r is not None and r.status is TaskStatus.COMPLETED
    assert mem.purged == []


def test_run_item_gc_unavailable_memory_retains_candidates(patched_worktree):
    board = FakeBoard(item=_item(), scratch_candidates=["dead1"])
    mem = FakeMemory()
    mem.delete_unavailable = True
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem, board=board)
    r = sup.run_item(item_id="item-1")
    assert r is not None and r.status is TaskStatus.COMPLETED
    assert mem.purged == [["dead1"]]
    assert board.marked_purged == []


def test_run_item_gc_ack_failure_retries_without_blocking(patched_worktree):
    board = FakeBoard(
        item=_item(),
        scratch_candidates=["dead1"],
        mark_raises=BoardError("mark boom"),
    )
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem, board=board)
    r = sup.run_item(item_id="item-1")
    assert r is not None and r.status is TaskStatus.COMPLETED
    assert mem.purged == [["dead1"]]
    assert board.marked_purged == []


def test_run_item_nothing_to_claim_still_sweeps(patched_worktree):
    board = FakeBoard(scratch_candidates=["dead1"])
    mem = FakeMemory()
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem, board=board)
    assert sup.run_item() is None
    assert mem.purged == [["dead1"]]
    assert board.marked_purged == [{"dead1"}]
    assert board.reactivated == []


def test_run_item_invalid_scope_fails_before_claim(patched_worktree):
    class _Memory(FakeMemory):
        def scoped(self, run_id):
            raise ValueError("invalid board-run memory namespace")

    board = FakeBoard(item=_item())
    agent = FakeAgent("claude")
    sup = _supervisor(FakeRegistry([agent]), _Memory(), board=board)
    with pytest.raises(BoardError, match="invalid board-run memory namespace"):
        sup.run_item(item_id="item-1")
    assert board.claimed == []
    assert agent.calls == []


def test_run_item_reactivates_ledger_on_normal_and_crash_exit(patched_worktree):
    normal = FakeBoard(item=_item())
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=normal)
    sup.run_item(item_id="item-1")
    assert normal.reactivated == [normal.claimed[0][1]]

    crashed = FakeBoard(item=_item(), apply_raises=RuntimeError("boom"))
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=crashed)
    with pytest.raises(RuntimeError, match="boom"):
        sup.run_item(item_id="item-1")
    assert crashed.reactivated == [crashed.claimed[0][1]]


# ── Run-report envelope (S3) ────────────────────────────────────────────────


def _report_supervisor(tmp_path, *, registry=None, memory=None, strategy=None,
                       breaker=None, board=None, **config_kw):
    config = SyncmillConfig(
        integration={"report": {"enabled": True, "runs_dir": str(tmp_path / "runs")}},
        **config_kw,
    )
    return _supervisor(
        registry or FakeRegistry([FakeAgent("claude")]),
        memory or FakeMemory(),
        strategy=strategy,
        config=config,
        breaker=breaker,
        board=board,
    ), tmp_path / "runs"


def _read_envelope(runs_dir, run_id):
    import json

    return json.loads((runs_dir / run_id / "envelope.json").read_text())


def _validate_envelope(doc):
    from test_contracts import CONTRACTS, _scan, _validator

    _validator(CONTRACTS / "run-envelope.schema.json").validate(doc)
    assert _scan(doc) == []


def test_run_disabled_report_touches_no_filesystem(patched_worktree, isolated_home):
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert sup.last_report_path is None
    assert not (isolated_home / ".syncmill").exists()  # zero-fs invariant


def test_run_writes_envelope_direct_golden(patched_worktree, tmp_path, monkeypatch):
    run_ids = []
    real = S.resolve_run_id
    monkeypatch.setattr(S, "resolve_run_id", lambda e=None: run_ids.append(real(e)) or run_ids[-1])
    sup, runs_dir = _report_supervisor(tmp_path)
    task = Task(prompt="go")
    r = sup.run(task)
    assert r.status is TaskStatus.COMPLETED
    doc = _read_envelope(runs_dir, run_ids[0])
    _validate_envelope(doc)
    assert doc["run_id"] == run_ids[0]
    assert doc["strategy"] == "route"
    assert doc["team"] is None
    assert doc["task_id"] == str(task.id)
    assert doc["agents"] == ["claude"]
    assert doc["board"] is None
    assert doc["degraded"] == []
    assert doc["result"]["status"] == "completed"
    assert doc["result"]["agent_id"] == "claude"
    assert (runs_dir / run_ids[0] / "result.json").exists()
    assert sup.last_report_path == runs_dir / run_ids[0] / "envelope.json"


def test_run_envelope_team_from_applied_team(patched_worktree, tmp_path):
    sup, runs_dir = _report_supervisor(tmp_path)
    sup._config.applied_team = "python-team"  # what apply_team stamps pre-Supervisor
    sup.run(Task(prompt="go"), run_id="rid-team")
    assert _read_envelope(runs_dir, "rid-team")["team"] == "python-team"


def test_run_envelope_agents_reflect_matched_lineup(patched_worktree, tmp_path):
    # The envelope's agents list is the post-matcher candidate order, not raw priority.
    sup, runs_dir = _report_supervisor(
        tmp_path,
        registry=FakeRegistry([FakeAgent("claude"), FakeAgent("codex")]),
    )
    sup._matcher = FakeMatcher(promote=["codex"])
    sup.run(Task(prompt="go", tags=["python"]), run_id="rid-match")
    assert _read_envelope(runs_dir, "rid-match")["agents"] == ["codex", "claude"]


def test_run_inner_seam_writes_under_outer_minted_id(patched_worktree, tmp_path, monkeypatch):
    # The monitored INNER (env var + sentinel + SYNCMILL_RUN_ID) writes its envelope
    # under the OUTER-minted id — the 3-path unification's monitor leg.
    import syncmill.monitor as M

    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / M.SENTINEL_NAME).write_text("")
    monkeypatch.setenv(M.INNER_ENV, str(inner))
    monkeypatch.setenv(M.RUN_ID_ENV, "outer-minted")
    monkeypatch.setattr(S, "build_monitor", lambda cfg, *, monitor_dir: None)
    sup, runs_dir = _report_supervisor(tmp_path)
    sup.run(Task(prompt="go"))
    doc = _read_envelope(runs_dir, "outer-minted")
    _validate_envelope(doc)
    assert doc["run_id"] == "outer-minted"


def test_run_crash_finalizes_failed_envelope(patched_worktree, tmp_path):
    class _Boom:
        def execute(self, **kw):
            raise RuntimeError("kaboom")

    sup, runs_dir = _report_supervisor(tmp_path, strategy=_Boom())
    with pytest.raises(RuntimeError):
        sup.run(Task(prompt="go"), run_id="rid-crash")
    doc = _read_envelope(runs_dir, "rid-crash")
    _validate_envelope(doc)
    assert doc["result"]["status"] == "failed"
    assert doc["result"]["agent_id"] == "none"  # the _crash_result stand-in
    assert doc["finished_at"] >= doc["created_at"]
    # The finally still records where the (failed) envelope landed — truthful,
    # and harmless: the CLI never reaches its print when the run re-raises.
    assert sup.last_report_path == runs_dir / "rid-crash" / "envelope.json"


def test_run_reporter_failure_never_strands_cleanup(patched_worktree, tmp_path, monkeypatch):
    # Reporting is observability: a raising start() AND finish() must not affect
    # the result, the memory-session close, or worktree cleanup (monitor mirror).
    class _RaisingReporter:
        def start(self, **kw):
            raise OSError("disk full")

        def finish(self, result, *, degraded, trace=None, preflight=None):
            raise OSError("disk full")

    monkeypatch.setattr(S, "build_reporter", lambda cfg: _RaisingReporter())
    mem = FakeMemory()
    sup, _ = _report_supervisor(tmp_path, memory=mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert patched_worktree["pool"].cleaned
    assert any(e.startswith("end:") for e in mem.events)


def test_run_envelope_degraded_sources(patched_worktree, tmp_path):
    mem = FakeMemory()
    mem.degraded = True
    sup, runs_dir = _report_supervisor(
        tmp_path, memory=mem, breaker=FakeBreaker(degraded=True)
    )
    sup.run(Task(prompt="go"), run_id="rid-deg")
    assert _read_envelope(runs_dir, "rid-deg")["degraded"] == ["memory", "breaker"]


def test_run_reporter_finishes_before_monitor(patched_worktree, tmp_path, monkeypatch):
    # The envelope must be final BEFORE monitor.finish unblocks the OUTER (whose
    # report-path print is existence-gated on the finished envelope).
    order = []

    class _Reporter:
        degraded = False

        def start(self, **kw):
            pass

        def finish(self, result, *, degraded, trace=None, preflight=None):
            order.append("reporter")
            return None

    class _Monitor(FakeMonitor):
        def finish(self, result):
            order.append("monitor")

    monkeypatch.setattr(S, "inner_dir", lambda: "/run-dir")
    monkeypatch.setattr(S, "build_monitor", lambda cfg, *, monitor_dir: _Monitor())
    monkeypatch.setattr(S, "build_reporter", lambda cfg: _Reporter())
    sup, _ = _report_supervisor(tmp_path)
    sup.run(Task(prompt="go"))
    assert order == ["reporter", "monitor"]


def test_run_item_stamps_envelope_board_block(patched_worktree, tmp_path):
    board = FakeBoard(item=_item(goal="backend"))
    sup, runs_dir = _report_supervisor(tmp_path, board=board)
    r = sup.run_item(item_id="item-1")
    assert r is not None
    run_id = board.claimed[0][1]
    doc = _read_envelope(runs_dir, run_id)
    _validate_envelope(doc)
    assert doc["board"] == {"item_id": "item-1", "goal": "backend"}
    assert doc["run_id"] == run_id  # lease id == envelope id (no double issuance)


def test_run_item_goalless_item_normalizes_goal_to_null(patched_worktree, tmp_path):
    board = FakeBoard(item=_item(goal=""))
    sup, runs_dir = _report_supervisor(tmp_path, board=board)
    sup.run_item(item_id="item-1")
    doc = _read_envelope(runs_dir, board.claimed[0][1])
    _validate_envelope(doc)
    assert doc["board"] == {"item_id": "item-1", "goal": None}


def test_run_item_crash_leaves_board_block_null(patched_worktree, tmp_path):
    # A crash in apply_update happens BEFORE the board patch: the envelope (already
    # finalized by run()'s own finally) stays board:null — minimal per the design.
    board = FakeBoard(item=_item(), apply_raises=RuntimeError("kaboom"))
    sup, runs_dir = _report_supervisor(tmp_path, board=board)
    with pytest.raises(RuntimeError):
        sup.run_item(item_id="item-1")
    doc = _read_envelope(runs_dir, board.claimed[0][1])
    _validate_envelope(doc)
    assert doc["board"] is None
    assert doc["result"]["status"] == "completed"  # the run itself succeeded


def test_run_item_disabled_report_never_patches(patched_worktree, monkeypatch):
    # Disabled path: run_item must not even stat the runs dir.
    calls = []
    monkeypatch.setattr(S, "patch_envelope_board", lambda *a, **kw: calls.append(a))
    board = FakeBoard(item=_item())
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    sup.run_item(item_id="item-1")
    assert calls == []


# ── Tracer (S3′) — route/compete span recording + Supervisor wiring ─────────


_TRACE_ALLOWED_KEYS = {
    "openinference.span.kind",
    "graph.node.id",
    "graph.node.parent_id",
} | {
    f"syncmill.{k}"
    for k in (
        "schema_version", "run_id", "strategy", "phase", "agent_id", "attempt",
        "round", "status", "exit_code", "gate.passed", "worktree_slot", "winner",
        "files_changed_count",
        "tool.key", "tool.sequence", "telemetry.source", "telemetry.complete",
    )
}


def _otlp_spans(doc):
    return doc["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _span_attrs(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


def _mini_validate_otlp(doc):
    """Pure-python mirror of what tracegraph's OTLPSpanAdapter raises on —
    ingestability without importing tracegraph (the cross-repo contract rule)."""
    spans = _otlp_spans(doc)
    span_ids = [s["spanId"] for s in spans]
    assert len(set(span_ids)) == len(span_ids), "duplicate spanId"
    trace_ids = {s["traceId"] for s in spans}
    assert len(trace_ids) == 1
    node_ids = set()
    for span in spans:
        attrs = _span_attrs(span)
        assert "openinference.span.kind" in attrs
        node_ids.add(attrs["graph.node.id"]["stringValue"])
    for span in spans:
        attrs = _span_attrs(span)
        parent = attrs.get("graph.node.parent_id")
        if parent is not None:
            assert parent["stringValue"] in node_ids, "unresolvable logical parent"
        if "parentSpanId" in span:
            assert span["parentSpanId"] in set(span_ids), "dangling parentSpanId"
        for link in span.get("links", []):
            assert link["traceId"] in trace_ids and link["spanId"] in set(span_ids)


def _attrs_val(span_record, key):
    return span_record.attrs.get(key)


def test_route_traces_single_success():
    tr = FakeTracer()
    _run_route([FakeAgent("claude")], FakeMemory(), tracer=tr)
    assert tr.node_ids == ["attempt:claude:0"]
    span = tr.spans[0]
    assert span.name == "attempt:claude" and span.kind == "AGENT"
    assert span.node_parent == "run"
    assert span.status == "completed" and span.error is None
    assert _attrs_val(span, "winner") is True
    assert _attrs_val(span, "files_changed_count") == 0  # non-git fake worktree
    assert _attrs_val(span, "exit_code") == 0
    assert _attrs_val(span, "attempt") == 0
    assert "worktree_slot" not in span.attrs  # sequential: no meaningful slot


def test_route_traces_fallback_parent_chain():
    tr = FakeTracer()
    a = FakeAgent("codex", status=TaskStatus.FAILED, error="boom", exit_code=1)
    b = FakeAgent("claude")
    _run_route([a, b], FakeMemory(), tracer=tr)
    assert tr.node_ids == ["attempt:codex:0", "attempt:claude:1"]
    failed, winner = tr.spans
    assert failed.node_parent == "run"
    assert failed.status == "failed" and failed.error == "exit_code=1"
    assert "winner" not in failed.attrs
    # the fallback attempt is CAUSED BY the prior failure
    assert winner.node_parent == "attempt:codex:0"
    assert _attrs_val(winner, "winner") is True and _attrs_val(winner, "attempt") == 1


def test_route_traces_timeout_status():
    tr = FakeTracer()
    a = FakeAgent("codex", status=TaskStatus.FAILED,
                  error="Timed out after 5s", exit_code=-1)
    b = FakeAgent("claude")
    _run_route([a, b], FakeMemory(), tracer=tr)
    timed = tr.by_node("attempt:codex:0")
    assert timed.status == "timeout" and timed.error == "timeout"
    assert "exit_code" not in timed.attrs  # -1 sentinel is not a real exit code


def test_route_traces_gate_reject_keeps_raw_attempt_status():
    tr = FakeTracer()
    gate = FakeGate(by_agent={"codex": False, "claude": True})
    a, b = FakeAgent("codex"), FakeAgent("claude")
    _run_route([a, b], FakeMemory(), gate=gate, tracer=tr)
    assert tr.node_ids == [
        "attempt:codex:0", "gate:codex:0", "attempt:claude:1", "gate:claude:1",
    ]
    rejected = tr.by_node("attempt:codex:0")
    # the attempt span reports RAW execution (completed) — the verdict is the gate's
    assert rejected.status == "completed" and "winner" not in rejected.attrs
    gspan = tr.by_node("gate:codex:0")
    assert gspan.kind == "CHAIN" and gspan.node_parent == "attempt:codex:0"
    assert gspan.status == "failed"
    assert gspan.error == "gate: 0/1 checks passed; failed: fake-check"
    assert _attrs_val(gspan, "gate.passed") is False
    # the next attempt is caused by the gate-rejected one
    assert tr.by_node("attempt:claude:1").node_parent == "attempt:codex:0"
    passed = tr.by_node("gate:claude:1")
    assert passed.status == "completed" and _attrs_val(passed, "gate.passed") is True


def test_route_traces_body_free_qualified_tool_children():
    telemetry = ToolTelemetry(
        source="codex",
        complete=True,
        calls=(
            ToolCallRecord(1, "gate_e::always_ok", ToolOutcome.SUCCEEDED),
            ToolCallRecord(2, "gate_e::always_fail", ToolOutcome.FAILED),
        ),
    )
    tr = FakeTracer()
    _run_route([FakeAgent("codex", tool_telemetry=telemetry)], FakeMemory(), tracer=tr)
    assert tr.node_ids == [
        "attempt:codex:0", "attempt:codex:0:tool:1", "attempt:codex:0:tool:2"
    ]
    succeeded, failed = tr.spans[1:]
    assert succeeded.kind == failed.kind == "TOOL"
    assert succeeded.node_parent == failed.node_parent == "attempt:codex:0"
    assert succeeded.status == "completed" and succeeded.error is None
    assert failed.status == "failed" and failed.error == "tool_failed"


def test_compete_traces_fanout_select_and_links():
    tr = FakeTracer()
    a, b, c = FakeAgent("claude"), FakeAgent("codex"), FakeAgent("kimi-code")
    r = _run_compete([a, b, c], FakeMemory(), tracer=tr)
    assert r.agent_id == "claude"
    assert tr.node_ids == [
        "attempt:claude", "attempt:codex", "attempt:kimi-code", "select",
    ]
    # every attempt fans out from the common run parent — order is never causal
    for node in ("attempt:claude", "attempt:codex", "attempt:kimi-code"):
        assert tr.by_node(node).node_parent == "run"
    assert [_attrs_val(tr.by_node(n), "worktree_slot")
            for n in ("attempt:claude", "attempt:codex", "attempt:kimi-code")] == [0, 1, 2]
    assert _attrs_val(tr.by_node("attempt:claude"), "winner") is True
    assert "winner" not in tr.by_node("attempt:codex").attrs
    select = tr.by_node("select")
    assert select.kind == "CHAIN"
    assert select.node_parent == "attempt:claude"  # promoted single cause
    assert select.links == ("attempt:codex", "attempt:kimi-code")  # examined pool


def test_compete_traces_timeout_sibling_excluded_from_links():
    tr = FakeTracer()
    a = FakeAgent("claude")
    b = FakeAgent("codex", status=TaskStatus.FAILED,
                  error="Timed out after 9s", exit_code=-1)
    _run_compete([a, b], FakeMemory(), tracer=tr)
    timed = tr.by_node("attempt:codex")
    assert timed.status == "timeout" and timed.error == "timeout"
    assert tr.by_node("select").links == ()  # only raw-COMPLETED candidates link


def test_compete_traces_gate_reject_links_raw_completed():
    tr = FakeTracer()
    gate = FakeGate(by_agent={"claude": False})
    a, b, c = FakeAgent("claude"), FakeAgent("codex"), FakeAgent("kimi-code")
    r = _run_compete([a, b, c], FakeMemory(), gate=gate, tracer=tr)
    assert r.agent_id == "codex"
    # gate spans exist ONLY for the lazily-gated prefix (claude fail, codex pass)
    assert "gate:claude" in tr.node_ids and "gate:codex" in tr.node_ids
    assert "gate:kimi-code" not in tr.node_ids
    assert tr.by_node("gate:claude").node_parent == "attempt:claude"
    # the gate-rejected attempt keeps its RAW completed status…
    assert tr.by_node("attempt:claude").status == "completed"
    select = tr.by_node("select")
    assert select.node_parent == "attempt:codex"
    # …and stays a linked candidate (it raw-completed), alongside the ungated one
    assert select.links == ("attempt:claude", "attempt:kimi-code")


def test_compete_traces_all_fail_select_parents_run():
    tr = FakeTracer()
    a = FakeAgent("claude", status=TaskStatus.FAILED, error="x", exit_code=2)
    b = FakeAgent("codex", status=TaskStatus.FAILED, error="y", exit_code=3)
    r = _run_compete([a, b], FakeMemory(), tracer=tr)
    assert r.status is TaskStatus.FAILED and r.agent_id == "none"
    select = tr.by_node("select")
    assert select.node_parent == "run" and select.links == ()


def test_pipeline_traces_stage_chain():
    tr = FakeTracer()
    a = FakeAgent("claude", duration=2.0)
    b = FakeAgent("codex", duration=3.0)
    r = _run_pipeline([a, b], FakeMemory(), gate=FakeGate(passes=True), tracer=tr)
    assert r.status is TaskStatus.COMPLETED
    assert tr.node_ids == ["stage:0:claude", "stage:1:codex", "gate:codex:1"]
    first, final, gspan = tr.spans
    assert first.name == "stage:claude" and first.kind == "AGENT"
    assert first.node_parent == "run"
    assert _attrs_val(first, "phase") == "stage" and _attrs_val(first, "attempt") == 0
    assert "worktree_slot" not in first.attrs  # sequential: no meaningful slot
    assert "winner" not in first.attrs
    # the next stage is CAUSED BY the previous one — the chain is the causality
    assert final.node_parent == "stage:0:claude"
    assert _attrs_val(final, "attempt") == 1
    assert _attrs_val(final, "winner") is True
    assert _attrs_val(final, "files_changed_count") == 0  # non-git fake worktree
    # the final span carries the stage's OWN duration, snapshotted BEFORE the
    # result's duration is mutated to the pipeline sum (5.0)
    assert round(final.end - final.start, 2) == 3.0
    assert r.duration_seconds == 5.0
    assert gspan.kind == "CHAIN" and gspan.node_parent == "stage:1:codex"
    assert gspan.status == "completed" and _attrs_val(gspan, "gate.passed") is True


def test_pipeline_traces_fail_fast_mid_chain():
    tr = FakeTracer()
    a = FakeAgent("claude")
    b = FakeAgent("codex", status=TaskStatus.FAILED, error="boom", exit_code=1)
    c = FakeAgent("kimi-code")
    r = _run_pipeline([a, b, c], FakeMemory(), tracer=tr)
    assert r.status is TaskStatus.FAILED
    # the broken stage leaves its span; later stages never ran → no spans
    assert tr.node_ids == ["stage:0:claude", "stage:1:codex"]
    failed = tr.by_node("stage:1:codex")
    assert failed.node_parent == "stage:0:claude"
    assert failed.status == "failed" and failed.error == "exit_code=1"
    assert all("winner" not in s.attrs for s in tr.spans)


def test_pipeline_traces_gate_reject_keeps_raw_final_status():
    tr = FakeTracer()
    a, b = FakeAgent("claude"), FakeAgent("codex")
    r = _run_pipeline([a, b], FakeMemory(), gate=FakeGate(passes=False), tracer=tr)
    assert r.status is TaskStatus.FAILED  # the pipeline result is downgraded…
    final = tr.by_node("stage:1:codex")
    # …but the stage span reports RAW execution (completed) — the verdict is the gate's
    assert final.status == "completed" and "winner" not in final.attrs
    assert "files_changed_count" not in final.attrs
    gspan = tr.by_node("gate:codex:1")
    assert gspan.status == "failed"
    assert gspan.error == "gate: 0/1 checks passed; failed: fake-check"
    assert _attrs_val(gspan, "gate.passed") is False


def test_pipeline_traces_timeout_status():
    tr = FakeTracer()
    a = FakeAgent("claude", status=TaskStatus.FAILED,
                  error="Timed out after 5s", exit_code=-1)
    _run_pipeline([a, FakeAgent("codex")], FakeMemory(), tracer=tr)
    timed = tr.by_node("stage:0:claude")
    assert timed.status == "timeout" and timed.error == "timeout"
    assert "exit_code" not in timed.attrs  # -1 sentinel is not a real exit code


def test_council_traces_propose_critique_synthesize_chain():
    tr = FakeTracer()
    a, b = FakeAgent("claude"), FakeAgent("codex")
    r = _run_council([a, b], FakeMemory(), tracer=tr)
    assert r.agent_id == "claude"
    assert tr.node_ids == [
        "propose:claude", "propose:codex",
        "critique:claude:0", "critique:codex:0",
        "synthesize:claude", "synthesize:codex",
        "select",
    ]
    # propose fans out from run — the one phase acquiring fresh worktrees
    for node, slot in (("propose:claude", 0), ("propose:codex", 1)):
        span = tr.by_node(node)
        assert span.node_parent == "run" and span.kind == "AGENT"
        assert _attrs_val(span, "phase") == "propose"
        assert _attrs_val(span, "worktree_slot") == slot
        assert span.links == ()
    # each critique parents on the member's OWN propose (worktree continuity)
    # and links the previous phase's PUBLISHED nodes
    for aid in ("claude", "codex"):
        crit = tr.by_node(f"critique:{aid}:0")
        assert crit.node_parent == f"propose:{aid}"
        assert _attrs_val(crit, "phase") == "critique"
        assert _attrs_val(crit, "round") == 0
        assert crit.links == ("propose:claude", "propose:codex")
        assert "worktree_slot" not in crit.attrs  # reused worktree — no fresh slot
        synth = tr.by_node(f"synthesize:{aid}")
        assert synth.node_parent == f"critique:{aid}:0"
        assert synth.links == ("critique:claude:0", "critique:codex:0")
        assert "round" not in synth.attrs
    assert _attrs_val(tr.by_node("synthesize:claude"), "winner") is True
    assert _attrs_val(tr.by_node("synthesize:claude"), "files_changed_count") == 0
    assert "winner" not in tr.by_node("synthesize:codex").attrs
    select = tr.by_node("select")
    assert select.kind == "CHAIN"
    assert select.node_parent == "synthesize:claude"  # promoted single cause
    assert select.links == ("synthesize:codex",)


def test_council_traces_multi_round_previous_round_links():
    tr = FakeTracer()
    cfg = SyncmillConfig(council=CouncilConfig(rounds=2))
    _run_council([FakeAgent("claude"), FakeAgent("codex")], FakeMemory(),
                 config=cfg, tracer=tr)
    second = tr.by_node("critique:claude:1")
    assert second.node_parent == "critique:claude:0"
    assert _attrs_val(second, "round") == 1
    assert second.links == ("critique:claude:0", "critique:codex:0")
    synth = tr.by_node("synthesize:claude")
    assert synth.node_parent == "critique:claude:1"
    assert synth.links == ("critique:claude:1", "critique:codex:1")


def test_council_traces_failed_proposer_excluded():
    tr = FakeTracer()
    a = _PhaseAgent("claude", propose=TaskStatus.FAILED, error="boom")
    b = _PhaseAgent("codex")
    r = _run_council([a, b], FakeMemory(), tracer=tr)
    assert r.agent_id == "codex"
    failed = tr.by_node("propose:claude")
    assert failed.status == "failed" and failed.node_parent == "run"
    # the failed proposer never became a member — no later spans for it
    assert "critique:claude:0" not in tr.node_ids
    assert "synthesize:claude" not in tr.node_ids
    # peers link only the PUBLISHED (completed) proposal
    assert tr.by_node("critique:codex:0").links == ("propose:codex",)
    assert tr.by_node("select").node_parent == "synthesize:codex"


def test_council_traces_failed_critique_parent_continuity():
    tr = FakeTracer()
    a = _PhaseAgent("claude", critique=TaskStatus.FAILED, error="stumble")
    b = _PhaseAgent("codex")
    r = _run_council([a, b], FakeMemory(), tracer=tr)
    assert r.status is TaskStatus.COMPLETED
    failed = tr.by_node("critique:claude:0")
    assert failed.status == "failed"
    # worktree continuity: the member's synthesize still parents on its OWN
    # failed critique (the execution ran in that worktree)…
    assert tr.by_node("synthesize:claude").node_parent == "critique:claude:0"
    # …but the failed critique is never a link target (published only)
    assert tr.by_node("synthesize:claude").links == ("critique:codex:0",)
    assert tr.by_node("synthesize:codex").links == ("critique:codex:0",)


def test_council_traces_gate_reject_and_select_links():
    tr = FakeTracer()
    gate = FakeGate(by_agent={"claude": False})
    r = _run_council([FakeAgent("claude"), FakeAgent("codex")], FakeMemory(),
                     gate=gate, tracer=tr)
    assert r.agent_id == "codex"
    # gate spans exist ONLY for the lazily-gated prefix, parented on synthesis
    assert tr.by_node("gate:claude").node_parent == "synthesize:claude"
    assert tr.by_node("gate:claude").status == "failed"
    assert tr.by_node("gate:codex").status == "completed"
    # the gate-rejected synthesis keeps its RAW completed status…
    assert tr.by_node("synthesize:claude").status == "completed"
    select = tr.by_node("select")
    assert select.node_parent == "synthesize:codex"
    # …and stays a linked candidate (it raw-completed)
    assert select.links == ("synthesize:claude",)


def test_council_traces_no_members_records_propose_only():
    tr = FakeTracer()
    a = _PhaseAgent("claude", propose=TaskStatus.FAILED, error="x")
    b = _PhaseAgent("codex", propose=TaskStatus.FAILED, error="y")
    r = _run_council([a, b], FakeMemory(), tracer=tr)
    assert r.status is TaskStatus.FAILED
    # selection never ran → no select span (unlike ran-but-nobody-won)
    assert tr.node_ids == ["propose:claude", "propose:codex"]


def test_council_traces_synth_all_fail_select_parents_run():
    tr = FakeTracer()
    a = _PhaseAgent("claude", synthesize=TaskStatus.FAILED, error="x")
    b = _PhaseAgent("codex", synthesize=TaskStatus.FAILED, error="y")
    r = _run_council([a, b], FakeMemory(), tracer=tr)
    assert r.status is TaskStatus.FAILED
    select = tr.by_node("select")
    assert select.node_parent == "run" and select.links == ()


def test_decompose_traces_plan_subtasks_synthesize():
    tr = FakeTracer()
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]')
    b = _DecomposeAgent("codex")
    r = _run_decompose([a, b], FakeMemory(), tracer=tr)
    assert r.agent_id == "claude"
    assert tr.node_ids == [
        "plan", "subtask:0", "subtask:1",
        "synthesize:claude", "synthesize:codex", "select",
    ]
    plan = tr.by_node("plan")
    assert plan.name == "plan:claude" and plan.node_parent == "run"
    assert _attrs_val(plan, "phase") == "plan"
    assert "worktree_slot" not in plan.attrs  # sequential planner step
    # subtasks fan out from the plan; the NAME carries the index (round-robin
    # repeats agent ids, so the label alone must stay unique)
    for i, aid in ((0, "claude"), (1, "codex")):
        sub = tr.by_node(f"subtask:{i}")
        assert sub.name == f"subtask:{i}:{aid}"
        assert sub.node_parent == "plan"
        assert _attrs_val(sub, "attempt") == i
        assert _attrs_val(sub, "worktree_slot") == i
    # synthesis fans IN over the plan, linking the published subtask results
    for slot, aid in enumerate(("claude", "codex")):
        synth = tr.by_node(f"synthesize:{aid}")
        assert synth.node_parent == "plan"
        assert synth.links == ("subtask:0", "subtask:1")
        assert _attrs_val(synth, "worktree_slot") == slot  # fresh fan-out trees
    assert _attrs_val(tr.by_node("synthesize:claude"), "winner") is True
    assert _attrs_val(tr.by_node("synthesize:claude"), "files_changed_count") == 0
    assert "winner" not in tr.by_node("synthesize:codex").attrs
    select = tr.by_node("select")
    assert select.node_parent == "synthesize:claude"
    assert select.links == ("synthesize:codex",)


def test_decompose_traces_degraded_plan_still_parents_on_plan():
    tr = FakeTracer()
    a = _DecomposeAgent("claude", plan=TaskStatus.FAILED, error="no plan")
    b = _DecomposeAgent("codex")
    r = _run_decompose([a, b], FakeMemory(), tracer=tr)
    assert r.status is TaskStatus.COMPLETED
    plan = tr.by_node("plan")
    assert plan.status == "failed" and plan.error == "exit_code=1"
    # the degraded whole-task subtask still parents on the (failed) plan hub
    assert tr.by_node("subtask:0").node_parent == "plan"
    assert tr.by_node("subtask:0").name == "subtask:0:claude"
    assert tr.by_node("synthesize:codex").links == ("subtask:0",)


def test_decompose_traces_failed_subtask_excluded_from_synth_links():
    tr = FakeTracer()
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]')
    b = _DecomposeAgent("codex", subtask=TaskStatus.FAILED, error="broke")
    r = _run_decompose([a, b], FakeMemory(), tracer=tr)
    assert r.status is TaskStatus.COMPLETED
    failed = tr.by_node("subtask:1")  # codex's round-robin slot
    assert failed.status == "failed"
    # only the PUBLISHED (completed) subtask is a link target
    assert tr.by_node("synthesize:claude").links == ("subtask:0",)
    assert tr.by_node("synthesize:codex").links == ("subtask:0",)


def test_decompose_traces_no_completed_subtasks_no_select():
    tr = FakeTracer()
    a = _DecomposeAgent("claude", plan_output='["sa", "sb"]',
                        subtask=TaskStatus.FAILED, error="x")
    b = _DecomposeAgent("codex", subtask=TaskStatus.FAILED, error="y")
    r = _run_decompose([a, b], FakeMemory(), tracer=tr)
    assert r.status is TaskStatus.FAILED
    # selection never ran → plan + subtask spans only, no synthesize/select
    assert tr.node_ids == ["plan", "subtask:0", "subtask:1"]


def test_decompose_traces_gate_reject_links_raw_completed():
    tr = FakeTracer()
    gate = FakeGate(by_agent={"claude": False})
    a = _DecomposeAgent("claude", plan_output='["sa"]')
    b = _DecomposeAgent("codex")
    r = _run_decompose([a, b], FakeMemory(), gate=gate, tracer=tr)
    assert r.agent_id == "codex"
    # gate spans only for the lazily-gated prefix, parented on synthesis
    assert tr.by_node("gate:claude").node_parent == "synthesize:claude"
    assert tr.by_node("gate:claude").status == "failed"
    assert tr.by_node("gate:codex").status == "completed"
    # the gate-rejected synthesis keeps its RAW completed status and stays linked
    assert tr.by_node("synthesize:claude").status == "completed"
    select = tr.by_node("select")
    assert select.node_parent == "synthesize:codex"
    assert select.links == ("synthesize:claude",)


def test_run_trace_disabled_writes_no_trace_file(patched_worktree, tmp_path):
    sup, runs_dir = _report_supervisor(tmp_path)
    sup.run(Task(prompt="go"), run_id="rid-notrace")
    doc = _read_envelope(runs_dir, "rid-notrace")
    _validate_envelope(doc)
    assert doc["trace"] == {"status": "disabled"}
    assert not (runs_dir / "rid-notrace" / "trace.otlp.json").exists()


def _traced_supervisor(tmp_path, **kw):
    config = SyncmillConfig(
        integration={
            "report": {"enabled": True, "runs_dir": str(tmp_path / "runs")},
            "trace": {"enabled": True},
        },
    )
    return _supervisor(
        kw.pop("registry", None) or FakeRegistry([FakeAgent("claude")]),
        kw.pop("memory", None) or FakeMemory(),
        config=config,
        **kw,
    ), tmp_path / "runs"


def test_run_traced_end_to_end(patched_worktree, tmp_path):
    import hashlib
    import json as _json

    sup, runs_dir = _traced_supervisor(tmp_path)
    r = sup.run(Task(prompt="TOP-SECRET-PROMPT-MARKER do it"), run_id="rid-traced")
    assert r.status is TaskStatus.COMPLETED

    trace_path = runs_dir / "rid-traced" / "trace.otlp.json"
    assert trace_path.exists()
    doc = _json.loads(trace_path.read_text())
    _mini_validate_otlp(doc)
    spans = _otlp_spans(doc)
    assert [s["name"] for s in spans] == ["attempt:claude", "syncmill.run"]
    run_root = spans[1]
    root_attrs = _span_attrs(run_root)
    assert root_attrs["syncmill.strategy"] == {"stringValue": "route"}
    assert root_attrs["graph.node.id"] == {"stringValue": "run"}
    assert "parentSpanId" not in run_root
    assert spans[0]["parentSpanId"] == run_root["spanId"]

    # redaction: only contract keys, and no prompt/output bodies anywhere
    serialized = trace_path.read_text()
    assert "TOP-SECRET-PROMPT-MARKER" not in serialized
    for span in spans:
        assert set(_span_attrs(span)) <= _TRACE_ALLOWED_KEYS

    envelope = _read_envelope(runs_dir, "rid-traced")
    _validate_envelope(envelope)
    trace_block = envelope["trace"]
    assert trace_block["status"] == "ok"
    assert trace_block["protocol"] == "otlp-json-file"
    assert trace_block["trace_id"] == spans[0]["traceId"]
    assert trace_block["artifact"] == "trace.otlp.json"
    digest = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    assert trace_block["digest"] == f"sha256:{digest}"
    assert envelope["degraded"] == []


def test_run_traced_decompose_end_to_end_flushes_ingestable_otlp(
    patched_worktree, tmp_path
):
    # The real FileTracer must accept a full multi-phase decompose document —
    # 3 subtasks round-robined over 2 agents is the node-uniqueness risk case
    # (claude runs plan + 2 subtasks + synthesis; agent ids repeat everywhere).
    import json as _json

    sup, runs_dir = _traced_supervisor(
        tmp_path,
        strategy=DecomposeStrategy(),
        registry=FakeRegistry([
            _DecomposeAgent("claude", plan_output='["a", "b", "c"]'),
            _DecomposeAgent("codex"),
        ]),
    )
    r = sup.run(Task(prompt="go"), run_id="rid-tdecomp")
    assert r.status is TaskStatus.COMPLETED

    trace_path = runs_dir / "rid-tdecomp" / "trace.otlp.json"
    doc = _json.loads(trace_path.read_text())
    _mini_validate_otlp(doc)
    spans = _otlp_spans(doc)
    node_ids = [
        _span_attrs(s)["graph.node.id"]["stringValue"] for s in spans
    ]
    assert len(set(node_ids)) == len(node_ids)  # unique under agent repeats
    assert node_ids == [
        "plan", "subtask:0", "subtask:1", "subtask:2",
        "synthesize:claude", "synthesize:codex", "select", "run",
    ]
    for span in spans:
        assert set(_span_attrs(span)) <= _TRACE_ALLOWED_KEYS
    envelope = _read_envelope(runs_dir, "rid-tdecomp")
    _validate_envelope(envelope)
    assert envelope["trace"]["status"] == "ok"  # nothing degraded at flush


def test_run_traced_crash_still_flushes_failed_run_span(patched_worktree, tmp_path):
    import json as _json

    class _Boom:
        def execute(self, **kw):
            raise RuntimeError("kaboom")

    sup, runs_dir = _traced_supervisor(tmp_path, strategy=_Boom())
    with pytest.raises(RuntimeError):
        sup.run(Task(prompt="go"), run_id="rid-tcrash")
    doc = _json.loads((runs_dir / "rid-tcrash" / "trace.otlp.json").read_text())
    spans = _otlp_spans(doc)
    assert [s["name"] for s in spans] == ["syncmill.run"]
    assert spans[0]["status"] == {"code": "STATUS_CODE_ERROR", "message": "failed"}
    envelope = _read_envelope(runs_dir, "rid-tcrash")
    _validate_envelope(envelope)
    assert envelope["trace"]["status"] == "ok"


def test_run_trace_flush_failure_degrades_never_strands_cleanup(
    patched_worktree, tmp_path, monkeypatch
):
    failing = FakeTracer(flush_raises=True)
    monkeypatch.setattr(S, "build_tracer", lambda cfg, *, run_id, fallback_dir: failing)
    sup, runs_dir = _report_supervisor(tmp_path)
    r = sup.run(Task(prompt="go"), run_id="rid-tfail")
    assert r.status is TaskStatus.COMPLETED  # the run itself is unaffected
    assert failing.flushed == 1
    assert patched_worktree["pool"].cleaned  # cleanup still ran
    envelope = _read_envelope(runs_dir, "rid-tfail")
    _validate_envelope(envelope)
    assert envelope["trace"] == {"status": "error"}
    assert "trace" in envelope["degraded"]


def test_run_tracer_injected_into_strategy(patched_worktree, tmp_path, monkeypatch):
    tr = FakeTracer()
    monkeypatch.setattr(S, "build_tracer", lambda cfg, *, run_id, fallback_dir: tr)
    seen = {}

    class _Rec:
        def execute(self, *, task, agents, memory, pool, config, gate=None,
                    breaker=None, monitor=None, tracer=None):
            seen["tracer"] = tracer
            return _completed()

    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), strategy=_Rec())
    sup.run(Task(prompt="go"))
    assert seen["tracer"] is tr
    assert [s.name for s in tr.spans] == ["syncmill.run"]  # root span in finally
    assert tr.flushed == 1


def test_run_session_close_failure_never_strands_cleanup(patched_worktree):
    # A raising end_task_session (memtomem hiccup) must not skip pool.cleanup_all
    # — cleanup is the last, unconditional finalizer step (codex R1 catch).
    mem = FakeMemory(fail_on={"end_task_session"})
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), mem)
    r = sup.run(Task(prompt="go"))
    assert r.status is TaskStatus.COMPLETED
    assert patched_worktree["pool"].cleaned


# ── Advisory preflight (S5) — Supervisor wiring ─────────────────────────────


def _preflight_supervisor(tmp_path, **cfg_overrides):
    """A report+toolgraph-enabled Supervisor; the test patches
    ``syncmill.preflight.subprocess.run`` to script the toolgraph binary.
    Returns (supervisor, runs_dir)."""
    tg = {"enabled": True, "agent_map": {"claude": "claude"}}
    tg.update(cfg_overrides)
    config = SyncmillConfig(
        integration={
            "report": {"enabled": True, "runs_dir": str(tmp_path / "runs")},
            "toolgraph": tg,
        },
    )
    return _supervisor(
        FakeRegistry([FakeAgent("claude")]), FakeMemory(), config=config
    ), tmp_path / "runs"


def _fake_preflight_run(golden_name, *, returncode=0, exc=None):
    def fake(argv, *a, **k):
        if exc is not None:
            raise exc
        out_path = Path(argv[argv.index("--out") + 1])
        payload = (Path(__file__).resolve().parent / "fixtures" / "toolgraph"
                   / f"{golden_name}.json").read_bytes()
        out_path.write_bytes(payload)
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="x")
    return fake


def test_run_records_preflight_ok_block(patched_worktree, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "syncmill.preflight.subprocess.run", _fake_preflight_run("preflight-allow")
    )
    sup, runs_dir = _preflight_supervisor(tmp_path)
    sup.run(Task(prompt="go"), run_id="rid-pf")
    doc = _read_envelope(runs_dir, "rid-pf")
    _validate_envelope(doc)
    assert doc["preflight"]["status"] == "ok"
    assert doc["preflight"]["graph_generation"] == 42
    assert (runs_dir / "rid-pf" / "preflight.json").exists()
    assert sup.preflight_warnings == []


def test_run_records_preflight_warn_surfaces_warnings(patched_worktree, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "syncmill.preflight.subprocess.run", _fake_preflight_run("preflight-warn")
    )
    sup, runs_dir = _preflight_supervisor(tmp_path)
    sup.run(Task(prompt="go"), run_id="rid-pfw")
    doc = _read_envelope(runs_dir, "rid-pfw")
    _validate_envelope(doc)
    assert doc["preflight"]["status"] == "ok"
    assert doc["preflight"]["warning_count"] == 3
    assert any("rejected" in w for w in sup.preflight_warnings)


@pytest.mark.parametrize("exc,status", [
    (FileNotFoundError(), "error"),
    (subprocess.TimeoutExpired("toolgraph", 5), "timeout"),
])
def test_run_preflight_failure_continues_and_cleans_up(
    patched_worktree, tmp_path, monkeypatch, exc, status
):
    monkeypatch.setattr(
        "syncmill.preflight.subprocess.run",
        _fake_preflight_run("preflight-allow", exc=exc),
    )
    sup, runs_dir = _preflight_supervisor(tmp_path)
    r = sup.run(Task(prompt="go"), run_id="rid-pfe")
    assert r.status is TaskStatus.COMPLETED  # advisory — run unaffected
    assert patched_worktree["pool"].cleaned  # cleanup ran
    doc = _read_envelope(runs_dir, "rid-pfe")
    _validate_envelope(doc)
    assert doc["preflight"]["status"] == status


def test_strict_preflight_blocks_before_agent_or_worktree_and_finalizes_report(
    patched_worktree, tmp_path, monkeypatch
):
    import json as _json

    def fake(argv, *a, **k):
        agent = argv[2]
        run_id = argv[argv.index("--run-id") + 1]
        candidate = "alpha::read"
        doc = {
            "schema_version": 1,
            "kind": "toolgraph.preflight",
            "run_id": run_id,
            "created_at": "2026-07-12T00:00:00+00:00",
            "graph_generation": 42,
            "agent": agent,
            "agent_found": True,
            "profile": "review",
            "mode": "advisory",
            "eligible": [],
            "rejected": [{
                "candidate": candidate,
                "tool_key": candidate,
                "reason": "DENY_VIOLATION",
                "paths": ["(read)-GOVERNED_BY->(deny:DENY)"],
            }],
            "features": [],
            "decision": "advisory_warn",
        }
        Path(argv[argv.index("--out") + 1]).write_text(_json.dumps(doc))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("syncmill.preflight.subprocess.run", fake)
    sup, runs_dir = _preflight_supervisor(
        tmp_path,
        mode="strict",
        candidate_tools=["alpha::read"],
    )
    agent = sup.registry.get("claude")
    result = sup.run(Task(prompt="must not execute"), run_id="rid-strict-block")
    assert result.status is TaskStatus.FAILED
    assert result.agent_id == "preflight"
    assert agent.calls == []
    assert patched_worktree["created"] == {}
    assert sup.last_preflight_blocked is True
    envelope = _read_envelope(runs_dir, "rid-strict-block")
    _validate_envelope(envelope)
    assert envelope["preflight"]["mode"] == "strict"
    assert envelope["preflight"]["enforcement_decision"] == "block"
    assert envelope["result"]["status"] == "failed"


def test_run_malformed_v1_keeps_envelope_valid(patched_worktree, tmp_path, monkeypatch):
    # A right-major but malformed artifact (no graph_generation) must degrade to
    # an "error" block, keeping the envelope schema-valid (codex R1).
    import json as _json

    def fake(argv, *a, **k):
        doc = _json.loads(
            (Path(__file__).resolve().parent / "fixtures" / "toolgraph"
             / "preflight-allow.json").read_text()
        )
        doc.pop("graph_generation")
        Path(argv[argv.index("--out") + 1]).write_text(_json.dumps(doc))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("syncmill.preflight.subprocess.run", fake)
    sup, runs_dir = _preflight_supervisor(tmp_path)
    sup.run(Task(prompt="go"), run_id="rid-pfm")
    doc = _read_envelope(runs_dir, "rid-pfm")
    _validate_envelope(doc)
    assert doc["preflight"]["status"] == "error"
    assert not (runs_dir / "rid-pfm" / "preflight.json").exists()


def test_run_preflight_disabled_skeleton_stays_disabled(patched_worktree, tmp_path, monkeypatch):
    # subprocess.run is a SHARED module object (git goes through it too), so
    # record only toolgraph-shaped argv to prove preflight never invoked it.
    tg_calls = []
    real_run = subprocess.run

    def spy(argv, *a, **k):
        if argv and argv[0] == "toolgraph":
            tg_calls.append(argv)
        return real_run(argv, *a, **k)

    monkeypatch.setattr("syncmill.preflight.subprocess.run", spy)
    # report enabled, toolgraph DISABLED
    config = SyncmillConfig(
        integration={"report": {"enabled": True, "runs_dir": str(tmp_path / "runs")}},
    )
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), config=config)
    sup.run(Task(prompt="go"), run_id="rid-pfd")
    assert tg_calls == []  # preflight disabled → toolgraph never spawned
    doc = _read_envelope(tmp_path / "runs", "rid-pfd")
    assert doc["preflight"] == {"status": "disabled"}
    assert sup.preflight_warnings == []
