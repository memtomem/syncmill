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
        context = run_async(memory.search_context(task.prompt))
        if context:
            enriched_prompt = f"Context from previous work:\n{context}\n\n---\n\nTask: {task.prompt}"
        else:
            enriched_prompt = task.prompt

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


# ── Supervisor ───────────────────────────────────────────────────────────

_STRATEGIES: dict[str, type[Strategy]] = {
    "route": RouteStrategy,
}


class Supervisor:
    """Central orchestrator. The only component that touches both agents and memory."""

    def __init__(self, config: SyncmillConfig):
        self._config = config
        self._registry = AgentRegistry(config)
        self._memory = SyncmillMemory(config.memory)

        strategy_cls = _STRATEGIES.get(config.default_strategy, RouteStrategy)
        self._strategy: Strategy = strategy_cls()

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

        # Create the task workspace. In RouteStrategy fallback the agents reuse
        # this one workspace sequentially (single winner); per-agent worktrees
        # arrive with v0.2's concurrent strategies.
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
