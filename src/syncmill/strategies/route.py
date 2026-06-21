"""RouteStrategy (v0.1 MVP) — priority-based fallback."""

from __future__ import annotations

from syncmill.agents import AgentRunner
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.strategies.base import (
    _aggregate_failure,
    _breaker_all_open_failure,
    _breaker_record,
    _breaker_runnable,
    _collect_files_changed,
    _enrich_prompt,
    _gate_accepts,
)
from syncmill.worktree import WorktreePool


class RouteStrategy:
    """Priority-based fallback. Try agents in order, fall back on failure."""

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
        # Drop agents whose circuit is OPEN before doing any work; if the breaker
        # has tripped them all, say so clearly rather than searching memory or
        # acquiring worktrees for nobody.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        # Search memory for relevant context
        enriched_prompt = _enrich_prompt(
            task.prompt, run_async(memory.search_context(task.prompt))
        )

        errors: list[TaskResult] = []
        for agent in runnable:
            # A fresh, isolated worktree per attempt: a failed agent's partial
            # edits never leak into the next agent's run, so the winner's
            # files_changed reflects only its own work.
            worktree = pool.acquire(agent.agent_id)
            timeout = config.agent_config(agent.agent_id).timeout_seconds
            result = agent.execute(
                prompt=enriched_prompt,
                workdir=worktree.path,
                timeout=timeout,
            )
            # Record the RAW execution outcome before gating: a completed run
            # closes the agent's circuit even if its diff is later gate-rejected
            # (a quality verdict, not an execution failure).
            _breaker_record(breaker, agent.agent_id, result.status)
            # Accept only a completed result that also clears the quality gate;
            # a gate failure downgrades the result and falls through to the next
            # agent (recorded in errors), so an unverified diff is never accepted.
            if result.status == TaskStatus.COMPLETED and _gate_accepts(
                result, worktree, gate
            ):
                _collect_files_changed(result, worktree)
                return result
            errors.append(result)

        # All agents failed (or failed the gate) — return aggregate error
        return _aggregate_failure(task, errors)
