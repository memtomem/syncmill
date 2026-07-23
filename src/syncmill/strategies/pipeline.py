"""PipelineStrategy (v0.2) — sequential chain of agents as stages."""

from __future__ import annotations

import time

from syncmill.agents import AgentRunner
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.monitor import Monitor
from syncmill.tracer import Tracer
from syncmill.strategies.base import (
    _attempt_span,
    _breaker_all_open_failure,
    _breaker_record,
    _breaker_runnable,
    _collect_files_changed,
    _enrich_prompt,
    _gate_accepts,
    _gate_span,
    _no_stages_failure,
    _trace_attempt,
    _trace_record,
)
from syncmill.worktree import WorktreePool


class PipelineStrategy:
    """Sequential chain. Agents run in priority order as pipeline stages, each
    building on the previous stage's work.

    Two handoff channels, both honoring agent isolation — agents stay pure
    ``execute(prompt, workdir, timeout)`` functions and never receive another
    stage's ``TaskResult``:

    * **Knowledge** — each completed stage writes its result to ``shared``
      (``share_result``); the next stage reads ``shared`` back
      (``search_context``) and prepends the most relevant matches to its prompt.
      This is best-effort and relevance-ranked, not a guaranteed point-to-point
      handoff — it surfaces the strongest shared context, which for a short
      pipeline is the recent stages — consistent with memory being advisory.
    * **Files** — every stage shares the one worktree acquired from the pool, so
      a stage edits the working tree the next stage inherits. Per-agent worktrees
      deliberately do *not* apply here: isolating stages would defeat the file
      handoff, so the pipeline acquires a single shared workspace (where
      RouteStrategy/Compete acquire one per agent).

    Fail-fast: a stage failure breaks the chain — later stages would have
    nothing valid to build on — so the pipeline stops and reports which stage
    broke.

    Tracing (S6): each stage is a ``stage:{i}:{agent}`` span parented on the
    previous stage's node (stage 0 on ``run``) — the chain IS the causality. A
    failed stage records its span and stops (later stages never ran, so they
    leave no spans). The final stage carries ``winner``/``files_changed_count``
    when gate-accepted (it is the result the pipeline returns — route's
    accepted-attempt rule), with its OWN duration snapshotted before the
    pipeline-sum mutation; its gate verdict rides a ``gate:{agent}:{i}`` span.
    No ``worktree_slot`` (sequential) and no select span (no convergence).
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
        monitor: Monitor | None = None,  # sequential: nothing concurrent to watch
        tracer: Tracer | None = None,
    ) -> TaskResult:
        if not agents:  # Supervisor guards this; stay safe if run standalone
            return _no_stages_failure(task)

        # A circuit-open stage is skipped; the pipeline chains the remaining
        # stages (consistent with Route skipping OPEN agents). If the breaker has
        # tripped every stage, report that rather than acquiring a worktree.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        # One worktree shared by every stage: a pipeline's whole point is that
        # each stage builds on the working tree the previous stage left behind,
        # so — unlike RouteStrategy/Compete — it acquires a single workspace.
        worktree = pool.acquire(runnable[0].agent_id)

        results: list[TaskResult] = []
        last = len(runnable) - 1
        prev_node: str | None = None
        for stage, agent in enumerate(runnable):
            # Each stage reads shared memory first, so it sees the prior stages'
            # outputs (plus any context from earlier syncmill runs).
            prompt = _enrich_prompt(
                task.prompt, run_async(memory.search_context(task.prompt))
            )
            timeout = config.agent_config(agent.agent_id).timeout_seconds
            result = agent.execute(prompt=prompt, workdir=worktree.path, timeout=timeout)
            # RAW snapshot for the stage span: status/error before any gate
            # mutation, duration before the post-loop pipeline-sum mutation.
            stage_end = time.time()
            raw_status, raw_error = result.status, result.error
            stage_duration = result.duration_seconds
            results.append(result)
            # Record each stage's RAW execution outcome (before the final gate).
            _breaker_record(breaker, agent.agent_id, result.status)

            node_id = f"stage:{stage}:{agent.agent_id}"
            parent = prev_node if prev_node is not None else "run"
            if result.status != TaskStatus.COMPLETED:
                # The broken stage still leaves its span; later stages never ran.
                _trace_attempt(tracer, _attempt_span(
                    agent.agent_id,
                    node_id=node_id,
                    parent=parent,
                    attempt=stage,
                    phase="stage",
                    raw_status=raw_status,
                    error=raw_error,
                    exit_code=result.exit_code,
                    end=stage_end,
                    duration=stage_duration,
                ), result)
                return _pipeline_failure(task, stage + 1, len(runnable), results)

            # Hand this stage's output to the next one via shared memory
            # (best-effort). The final stage is shared by the Supervisor, so
            # don't double-write it here. The FINAL stage's span is recorded
            # after the gate below, once winner/files_changed_count are known.
            if stage != last:
                _trace_attempt(tracer, _attempt_span(
                    agent.agent_id,
                    node_id=node_id,
                    parent=parent,
                    attempt=stage,
                    phase="stage",
                    raw_status=raw_status,
                    error=raw_error,
                    exit_code=result.exit_code,
                    end=stage_end,
                    duration=stage_duration,
                ), result)
                run_async(memory.share_result(task, result, kind="pipeline-stage"))
            prev_node = node_id

        # The chain's product is the last stage's result; report the whole
        # pipeline's wall-clock as its duration. The gate runs once, against the
        # FINAL cumulative worktree (intermediate stages may legitimately leave
        # the tree broken mid-chain) — a gate failure fails the pipeline.
        final = results[-1]
        final.duration_seconds = round(sum(r.duration_seconds for r in results), 2)
        accepted = _gate_accepts(final, worktree, gate)
        if accepted:
            _collect_files_changed(final, worktree)
        # The loop variables still hold the final stage's values: prev_node is
        # its node id, parent its predecessor, stage_end/raw_*/stage_duration
        # the pre-mutation snapshot.
        _trace_attempt(tracer, _attempt_span(
            final.agent_id,
            node_id=prev_node,
            parent=parent,
            attempt=last,
            phase="stage",
            raw_status=raw_status,
            error=raw_error,
            exit_code=final.exit_code,
            end=stage_end,
            duration=stage_duration,
            winner=accepted,
            files_changed_count=len(final.files_changed) if accepted else None,
        ), final)
        if final.gate is not None:
            _trace_record(tracer, _gate_span(
                final.agent_id,
                node_id=f"gate:{final.agent_id}:{last}",
                parent=prev_node,
                report=final.gate,
                end=time.time(),
            ))
        # Gate-rejected → already downgraded to FAILED with the gate error.
        return final


def _pipeline_failure(
    task: Task, stage: int, total_stages: int, results: list[TaskResult]
) -> TaskResult:
    """Build the failure result for a broken pipeline stage."""
    failed = results[-1]
    reason = failed.error or f"exit_code={failed.exit_code}"
    return TaskResult(
        task_id=task.id,
        agent_id=failed.agent_id,
        status=TaskStatus.FAILED,
        output=failed.output,
        exit_code=failed.exit_code,
        duration_seconds=round(sum(r.duration_seconds for r in results), 2),
        error=(
            f"Pipeline failed at stage {stage}/{total_stages} "
            f"({failed.agent_id}): {reason}"
        ),
    )
