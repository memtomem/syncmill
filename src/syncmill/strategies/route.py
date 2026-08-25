"""RouteStrategy (v0.1 MVP) — priority-based fallback."""

from __future__ import annotations

import time

from syncmill.agents import AgentRunner
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.memory import SyncmillMemory, run_async, run_memory_bounded
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
    _gate_accepts,
    _gate_span,
    _trace_attempt,
    _trace_record,
)
from syncmill.tracer import Tracer
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
        monitor: Monitor | None = None,  # sequential: nothing concurrent to watch
        tracer: Tracer | None = None,
    ) -> TaskResult:
        # Drop agents whose circuit is OPEN before doing any work; if the breaker
        # has tripped them all, say so clearly rather than searching memory or
        # acquiring worktrees for nobody.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        # Search memory for relevant context
        enriched_prompt = _enrich_prompt(
            task.prompt,
            run_memory_bounded(
                memory.search_context(task.prompt),
                default="", memory=memory, label="search_context",
            ),
        )

        errors: list[TaskResult] = []
        # The fallback causal chain: each attempt after the first is CAUSED BY
        # the previous failed/rejected one (graph.node.parent_id), so the trace
        # reads "codex failed, therefore claude ran".
        prev_node: str | None = None
        for i, agent in enumerate(runnable):
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
            # The RAW execution snapshot, taken before gating mutates the result
            # in place: the breaker AND the attempt span both report execution
            # reliability, not the gate's quality verdict.
            attempt_end = time.time()
            raw_status, raw_error = result.status, result.error
            # Record the RAW execution outcome before gating: a completed run
            # closes the agent's circuit even if its diff is later gate-rejected
            # (a quality verdict, not an execution failure).
            _breaker_record(breaker, agent.agent_id, result.status)
            node_id = f"attempt:{agent.agent_id}:{i}"
            # Accept only a completed result that also clears the quality gate;
            # a gate failure downgrades the result and falls through to the next
            # agent (recorded in errors), so an unverified diff is never accepted.
            accepted = result.status == TaskStatus.COMPLETED and _gate_accepts(
                result, worktree, gate
            )
            if accepted:
                _collect_files_changed(result, worktree)
            _trace_attempt(
                tracer,
                _attempt_span(
                    agent.agent_id,
                    node_id=node_id,
                    parent=prev_node if prev_node is not None else "run",
                    attempt=i,
                    raw_status=raw_status,
                    error=raw_error,
                    exit_code=result.exit_code,
                    end=attempt_end,
                    duration=result.duration_seconds,
                    winner=accepted,
                    files_changed_count=len(result.files_changed) if accepted else None,
                ),
                result,
            )
            if result.gate is not None:
                _trace_record(
                    tracer,
                    _gate_span(
                        agent.agent_id,
                        node_id=f"gate:{agent.agent_id}:{i}",
                        parent=node_id,
                        report=result.gate,
                        end=time.time(),
                    ),
                )
            if accepted:
                return result
            errors.append(result)
            prev_node = node_id

        # All agents failed (or failed the gate) — return aggregate error
        return _aggregate_failure(task, errors)
