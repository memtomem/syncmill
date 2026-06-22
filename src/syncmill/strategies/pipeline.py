"""PipelineStrategy (v0.2) — sequential chain of agents as stages."""

from __future__ import annotations

from syncmill.agents import AgentRunner
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.monitor import Monitor
from syncmill.strategies.base import (
    _breaker_all_open_failure,
    _breaker_record,
    _breaker_runnable,
    _collect_files_changed,
    _enrich_prompt,
    _gate_accepts,
    _no_stages_failure,
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
        for stage, agent in enumerate(runnable):
            # Each stage reads shared memory first, so it sees the prior stages'
            # outputs (plus any context from earlier syncmill runs).
            prompt = _enrich_prompt(
                task.prompt, run_async(memory.search_context(task.prompt))
            )
            timeout = config.agent_config(agent.agent_id).timeout_seconds
            result = agent.execute(prompt=prompt, workdir=worktree.path, timeout=timeout)
            results.append(result)
            # Record each stage's RAW execution outcome (before the final gate).
            _breaker_record(breaker, agent.agent_id, result.status)

            if result.status != TaskStatus.COMPLETED:
                return _pipeline_failure(task, stage + 1, len(runnable), results)

            # Hand this stage's output to the next one via shared memory
            # (best-effort). The final stage is shared by the Supervisor, so
            # don't double-write it here.
            if stage != last:
                run_async(memory.share_result(task, result))

        # The chain's product is the last stage's result; report the whole
        # pipeline's wall-clock as its duration. The gate runs once, against the
        # FINAL cumulative worktree (intermediate stages may legitimately leave
        # the tree broken mid-chain) — a gate failure fails the pipeline.
        final = results[-1]
        final.duration_seconds = round(sum(r.duration_seconds for r in results), 2)
        if not _gate_accepts(final, worktree, gate):
            return final  # downgraded to FAILED with the gate error
        _collect_files_changed(final, worktree)
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
