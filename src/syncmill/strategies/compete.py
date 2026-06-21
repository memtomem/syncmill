"""CompeteStrategy (v0.2) — concurrent head-to-head, keep the winner."""

from __future__ import annotations

from syncmill.agents import AgentRunner, aexecute_isolated
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult
from syncmill.strategies.base import (
    _aggregate_failure,
    _breaker_all_open_failure,
    _breaker_record,
    _breaker_runnable,
    _collect_files_changed,
    _enrich_prompt,
    _no_stages_failure,
    _select_winner,
)
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
        # order, so results line up with `runnable` and `worktrees`.
        results = run_async(
            aexecute_isolated(
                runnable,
                enriched_prompt,
                [wt.path for wt in worktrees],
                lambda aid: config.agent_config(aid).timeout_seconds,
            )
        )

        # Record EVERY competitor's raw outcome (winner + losers), before gating,
        # so a repeatedly-crashing agent's failures accumulate across runs and a
        # working one's circuit closes — even when a higher-priority peer wins.
        for agent, result in zip(runnable, results):
            _breaker_record(breaker, agent.agent_id, result.status)

        winner = _select_winner(worktrees, results, gate)
        if winner is None:
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

        result, worktree = winner
        _collect_files_changed(result, worktree)  # the winner's OWN worktree diff
        return result
