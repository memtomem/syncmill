"""CompeteStrategy (v0.2) — concurrent head-to-head, keep the winner."""

from __future__ import annotations

import time

from syncmill.agents import AgentRunner, aexecute_isolated
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.monitor import Monitor
from syncmill.strategies.base import (
    _aggregate_failure,
    _attempt_span,
    _breaker_all_open_failure,
    _breaker_record,
    _breaker_runnable,
    _collect_files_changed,
    _enrich_prompt,
    _gate_span,
    _monitor_phase,
    _monitor_status,
    _monitor_streaming,
    _no_stages_failure,
    _select_span,
    _select_winner,
    _trace_attempt,
    _trace_record,
)
from syncmill.tracer import Tracer
from syncmill.worktree import WorktreePool


class CompeteStrategy:
    """Concurrent head-to-head. Every enabled agent runs the SAME task at once,
    each in its own isolated worktree; the highest-priority agent that COMPLETED
    wins.

    syncmill's first *concurrent* strategy. It bridges async via ``run_async``
    (reusing the module event loop — never ``asyncio.run``) to fan agents out over
    ``aexecute``, one private worktree per agent so parallel edits never clobber
    (where Pipeline shares a single tree). Every agent's prompt is enriched
    identically from ``shared`` (``_enrich_prompt``): they compete from the same
    starting context, with no stage-to-stage handoff.

    Unlike RouteStrategy it does **not** stop at the first success — all agents
    run and each produces a real diff in its own worktree — but the winner is the
    same priority preference made *deterministic* by ignoring finish time: among
    the COMPLETED results, the first in priority (input ``agents``) order. The
    criterion never reads ``duration_seconds`` (nondeterministic wall-clock), so
    it's reproducible across runs and across git vs tempdir workspaces. Selection
    is funnelled through ``_select_winner`` — the seam where quality gates (#10)
    will later rank the COMPLETED candidates by a verified score. If no agent
    completes, it reports an aggregate failure (``_aggregate_failure``), the same
    shape Route uses.

    The Supervisor owns worktree lifetime — it shares the returned winner to
    ``shared`` and tears down EVERY worktree (winner + losers) in its ``finally``
    — so compete neither cleans up worktrees nor applies the winner's diff back to
    the repo; it only *surfaces* it (``files_changed`` + the Supervisor's share).
    """

    def execute(
        self,
        task: Task,
        agents: list[AgentRunner],
        memory: SyncmillMemory,
        pool: WorktreePool,
        config: SyncmillConfig,
        gate: QualityGate | None = None,
        breaker: CircuitBreaker | None = None,
        monitor: Monitor | None = None,
        tracer: Tracer | None = None,
    ) -> TaskResult:
        if not agents:  # Supervisor guards this; stay safe if run standalone
            return _no_stages_failure(task)

        # Drop circuit-open agents BEFORE acquiring worktrees / fanning out, so
        # `runnable`, `worktrees`, and `results` stay the same length and
        # positionally aligned (the alignment _select_winner and recording rely
        # on). If the breaker tripped them all, say so.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        # Memory read happens FIRST, fully sync — never call run_async from inside
        # the gather below (that would re-enter the running loop). Every agent
        # competes from the same enriched starting context.
        enriched_prompt = _enrich_prompt(
            task.prompt, run_async(memory.search_context(task.prompt))
        )

        # One isolated worktree per agent, acquired UP FRONT and SERIALLY:
        # acquire() is sync git (contends on the index lock) and must not run
        # inside the concurrent fan-out. The comprehension over `runnable` keeps
        # the lists the same length, so results/worktrees/agents stay aligned.
        worktrees = [pool.acquire(agent.agent_id) for agent in runnable]

        # One concurrent batch on the shared loop; gather preserves submission
        # order, so results line up with `runnable` and `worktrees`. The live
        # monitor (#16) opens a pane per competitor and tees its output as it runs.
        runnable_ids = [agent.agent_id for agent in runnable]
        _monitor_phase(monitor, "compete")
        with _monitor_streaming(monitor, runnable_ids):
            results = run_async(
                aexecute_isolated(
                    runnable,
                    enriched_prompt,
                    [wt.path for wt in worktrees],
                    lambda aid: config.agent_config(aid).timeout_seconds,
                )
            )

        # The fan-out's shared end-of-phase clock plus a RAW (pre-gate) snapshot:
        # _select_winner downgrades gate-rejected completers to FAILED in place,
        # but the attempt spans and the select links report execution outcomes —
        # a gate rejection is a quality verdict, not an execution failure.
        phase_end = time.time()
        raw = [(r.status, r.error) for r in results]

        # Record EVERY competitor's raw outcome (winner + losers), before gating,
        # so a repeatedly-crashing agent's failures accumulate across runs and a
        # working one's circuit closes — even when a higher-priority peer wins.
        for agent, result in zip(runnable, results):
            _breaker_record(breaker, agent.agent_id, result.status)
            _monitor_status(monitor, agent.agent_id, result.status)

        sel_start = time.time()
        winner = _select_winner(
            worktrees,
            results,
            gate,
            mode=config.selection.mode,
            agents=runnable,
            pool=pool,
            config=config,
        )
        winner_result: TaskResult | None = None
        if winner is not None:
            winner_result, winner_worktree = winner
            _collect_files_changed(winner_result, winner_worktree)  # winner's OWN diff
        sel_end = time.time()

        # Retrospective span recording, all AFTER selection: the tracer is
        # append-only, and winner/files_changed_count are only known now. Every
        # attempt fans out from the common `run` parent (completion order is
        # never causal); gate spans exist only for candidates _select_winner
        # actually gated (the lazy prefix); the select span is parented on the
        # winning attempt and linked to the other raw-COMPLETED candidates.
        if tracer is not None:
            for slot, (agent, result) in enumerate(zip(runnable, results)):
                raw_status, raw_error = raw[slot]
                is_winner = result is winner_result
                _trace_attempt(
                    tracer,
                    _attempt_span(
                        agent.agent_id,
                        node_id=f"attempt:{agent.agent_id}",
                        parent="run",
                        attempt=0,
                        raw_status=raw_status,
                        error=raw_error,
                        exit_code=result.exit_code,
                        end=phase_end,
                        duration=result.duration_seconds,
                        winner=is_winner,
                        files_changed_count=(
                            len(result.files_changed) if is_winner else None
                        ),
                        worktree_slot=slot,
                    ),
                    result,
                )
                if result.gate is not None:
                    _trace_record(
                        tracer,
                        _gate_span(
                            agent.agent_id,
                            node_id=f"gate:{agent.agent_id}",
                            parent=f"attempt:{agent.agent_id}",
                            report=result.gate,
                            end=sel_end,
                        ),
                    )
            links = [
                f"attempt:{agent.agent_id}"
                for agent, result, (raw_status, _) in zip(runnable, results, raw)
                if raw_status == TaskStatus.COMPLETED and result is not winner_result
            ]
            _trace_record(
                tracer,
                _select_span(
                    winner_node=(
                        f"attempt:{winner_result.agent_id}"
                        if winner_result is not None
                        else None
                    ),
                    links=links,
                    start=sel_start,
                    end=sel_end,
                ),
            )

        if winner_result is None:
            # Nobody completed, or completers all failed the gate (which downgrades
            # them to FAILED in `results`, so the aggregate names the gate failure).
            # Same all-failed shape as Route, but agents ran concurrently, so the
            # wall-clock is the slowest, not the sum.
            return _aggregate_failure(
                task,
                results,
                duration_seconds=max(
                    (r.duration_seconds for r in results), default=0.0
                ),
            )

        return winner_result
