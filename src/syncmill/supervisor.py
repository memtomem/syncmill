"""Orchestration — Strategy pattern + Supervisor.

Supervisor is the sole mediator. Agents don't know about each other or about memory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from syncmill.agents import AgentRegistry, AgentRunner
from syncmill.config import SyncmillConfig
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.worktree import Worktree, cleanup_worktree, create_worktree


# ── Strategy Protocol ────────────────────────────────────────────────────


@runtime_checkable
class Strategy(Protocol):
    """Orchestration strategy — determines how agents are selected and run."""

    def execute(
        self,
        task: Task,
        agents: list[AgentRunner],
        memory: SyncmillMemory,
        workdir: Path,
        config: SyncmillConfig,
    ) -> TaskResult: ...


def _enrich_prompt(prompt: str, context: str) -> str:
    """Prepend shared-memory context to a task prompt, if any.

    Shared by every strategy so the enrichment format stays identical across
    them (RouteStrategy, PipelineStrategy, …).
    """
    if context:
        return f"Context from previous work:\n{context}\n\n---\n\nTask: {prompt}"
    return prompt


# ── RouteStrategy (v0.1 MVP) ────────────────────────────────────────────


class RouteStrategy:
    """Priority-based fallback. Try agents in order, fall back on failure."""

    def execute(
        self,
        task: Task,
        agents: list[AgentRunner],
        memory: SyncmillMemory,
        workdir: Path,
        config: SyncmillConfig,
    ) -> TaskResult:
        # Search memory for relevant context
        enriched_prompt = _enrich_prompt(
            task.prompt, run_async(memory.search_context(task.prompt))
        )

        errors: list[TaskResult] = []
        for agent in agents:
            timeout = config.agent_config(agent.agent_id).timeout_seconds
            result = agent.execute(
                prompt=enriched_prompt,
                workdir=workdir,
                timeout=timeout,
            )
            if result.status == TaskStatus.COMPLETED:
                return result
            errors.append(result)

        # All agents failed — return aggregate error
        return _aggregate_failure(task, errors)


def _aggregate_failure(task: Task, errors: list[TaskResult]) -> TaskResult:
    """Combine all failure reasons into a single TaskResult."""
    lines = []
    for e in errors:
        reason = e.error or f"exit_code={e.exit_code}"
        lines.append(f"  [{e.agent_id}] {reason}")
    combined_error = "All agents failed:\n" + "\n".join(lines)

    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=-1,
        duration_seconds=sum(e.duration_seconds for e in errors),
        error=combined_error,
    )


# ── PipelineStrategy (v0.2) ─────────────────────────────────────────────


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
    * **Files** — every stage shares the one task worktree (``workdir``), so a
      stage edits the working tree the next stage inherits. (Per-*stage*
      worktrees arrive with the per-agent worktrees work.)

    Fail-fast: a stage failure breaks the chain — later stages would have
    nothing valid to build on — so the pipeline stops and reports which stage
    broke.
    """

    def execute(
        self,
        task: Task,
        agents: list[AgentRunner],
        memory: SyncmillMemory,
        workdir: Path,
        config: SyncmillConfig,
    ) -> TaskResult:
        if not agents:  # Supervisor guards this; stay safe if run standalone
            return _no_stages_failure(task)

        results: list[TaskResult] = []
        last = len(agents) - 1
        for stage, agent in enumerate(agents):
            # Each stage reads shared memory first, so it sees the prior stages'
            # outputs (plus any context from earlier syncmill runs).
            prompt = _enrich_prompt(
                task.prompt, run_async(memory.search_context(task.prompt))
            )
            timeout = config.agent_config(agent.agent_id).timeout_seconds
            result = agent.execute(prompt=prompt, workdir=workdir, timeout=timeout)
            results.append(result)

            if result.status != TaskStatus.COMPLETED:
                return _pipeline_failure(task, stage + 1, len(agents), results)

            # Hand this stage's output to the next one via shared memory
            # (best-effort). The final stage is shared by the Supervisor, so
            # don't double-write it here.
            if stage != last:
                run_async(memory.share_result(task, result))

        # The chain's product is the last stage's result; report the whole
        # pipeline's wall-clock as its duration.
        final = results[-1]
        final.duration_seconds = round(sum(r.duration_seconds for r in results), 2)
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


def _no_stages_failure(task: Task) -> TaskResult:
    """Defensive failure when a strategy is handed no agents to run."""
    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=-1,
        duration_seconds=0.0,
        error="No agents available",
    )


# ── Supervisor ───────────────────────────────────────────────────────────

_STRATEGIES: dict[str, type[Strategy]] = {
    "route": RouteStrategy,
    "pipeline": PipelineStrategy,
}


def available_strategies() -> list[str]:
    """Registered strategy names, sorted — used by the CLI to validate the
    ``--strategy`` option."""
    return sorted(_STRATEGIES)


def _make_strategy(name: str) -> Strategy:
    """Instantiate a strategy by name, raising on an unknown name.

    Mirrors ``AgentRegistry``'s hard failure on unknown agent ids: a typo'd
    strategy should be a clear error, not a silent fall back to RouteStrategy.
    """
    if name not in _STRATEGIES:
        valid = ", ".join(sorted(_STRATEGIES))
        raise ValueError(f"Unknown strategy: {name!r}. Valid strategies: {valid}.")
    return _STRATEGIES[name]()


class Supervisor:
    """Central orchestrator. The only component that touches both agents and memory."""

    def __init__(self, config: SyncmillConfig):
        self._config = config
        self._registry = AgentRegistry(config)
        self._memory = SyncmillMemory(config.memory)

        self._strategy: Strategy = _make_strategy(config.default_strategy)

    def run(
        self,
        task: Task,
        agent_id: str | None = None,
        exclude: set[str] | None = None,
    ) -> TaskResult:
        """Execute a task through the orchestration pipeline.

        Args:
            task: The task to execute.
            agent_id: If specified, force this agent only (no fallback).
            exclude: Agent ids to drop from the priority fallback — e.g. run one
                agent interactively yourself while syncmill orchestrates the
                rest. Forcing an excluded agent via ``agent_id`` is an error.
        """
        exclude = exclude or set()
        # Determine which agents to use
        if agent_id:
            if agent_id in exclude:
                return TaskResult(
                    task_id=task.id,
                    agent_id=agent_id,
                    status=TaskStatus.FAILED,
                    output="",
                    exit_code=-1,
                    duration_seconds=0.0,
                    error=f"Agent '{agent_id}' is both forced (agent_id) and excluded",
                )
            agent = self._registry.get(agent_id)
            if agent is None:
                return TaskResult(
                    task_id=task.id,
                    agent_id=agent_id,
                    status=TaskStatus.FAILED,
                    output="",
                    exit_code=-1,
                    duration_seconds=0.0,
                    error=f"Agent '{agent_id}' not found or not enabled",
                )
            agents = [agent]
        else:
            agents = [
                a for a in self._registry.available() if a.agent_id not in exclude
            ]

        if not agents:
            error = "No agents available"
            if exclude:
                error += f" (excluded: {', '.join(sorted(exclude))})"
            return TaskResult(
                task_id=task.id,
                agent_id="none",
                status=TaskStatus.FAILED,
                output="",
                exit_code=-1,
                duration_seconds=0.0,
                error=error,
            )

        # Register agents in memory (best-effort)
        for agent in agents:
            run_async(self._memory.register_agent(agent.agent_id))

        # Create the task workspace — one worktree shared by every agent this
        # run. RouteStrategy's fallback reuses it sequentially (single winner);
        # PipelineStrategy's stages build on each other inside it. Per-agent
        # worktrees (for concurrent strategies) arrive with v0.2's later work.
        repo_root = Path(self._config.repo_root).resolve()
        worktree = create_worktree(repo_root, agents[0].agent_id, str(task.id))

        # Start memory session (best-effort; None if memory is unavailable)
        session_id = run_async(
            self._memory.start_task_session(agents[0].agent_id, task)
        )

        result: TaskResult | None = None
        try:
            # The worktree starts clean at HEAD, so diffing HEAD afterwards
            # yields exactly the files this task changed.
            result = self._strategy.execute(
                task=task,
                agents=agents,
                memory=self._memory,
                workdir=worktree.path,
                config=self._config,
            )

            if worktree.is_git and result.status == TaskStatus.COMPLETED:
                result.files_changed = list(_git_changed_files(worktree.path))

            # Share successful results to memory
            if result.status == TaskStatus.COMPLETED:
                run_async(self._memory.share_result(task, result))

            return result
        finally:
            if result is not None:
                run_async(self._memory.end_task_session(session_id, result))
            cleanup_worktree(repo_root, worktree)

    def close(self) -> None:
        run_async(self._memory.close())

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def memory(self) -> SyncmillMemory:
        return self._memory


def _git_changed_files(workdir: Path) -> set[str]:
    """Get files changed in worktree (staged + unstaged + untracked)."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=str(workdir),
        )
        changed = set(proc.stdout.strip().splitlines()) if proc.returncode == 0 else set()
        # Also check untracked
        proc2 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=str(workdir),
        )
        if proc2.returncode == 0:
            changed.update(proc2.stdout.strip().splitlines())
        return changed
    except Exception:
        return set()
