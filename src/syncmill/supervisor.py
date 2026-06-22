"""The Supervisor — the sole mediator between agents and memory.

Agents don't know about each other or about memory; the Supervisor wires them
to a ``Strategy`` (the orchestration topology — see ``syncmill.strategies``) and
to ``SyncmillMemory``. Strategies were extracted into the ``strategies`` package
(#12) once the four of them made this module unwieldy; the Supervisor keeps only
the run lifecycle, strategy selection, and the capability re-ordering it applies
before dispatch.
"""

from __future__ import annotations

import logging
from pathlib import Path

from syncmill.agents import AgentRegistry, AgentRunner
from syncmill.breaker import CircuitBreaker, build_breaker
from syncmill.config import SyncmillConfig
from syncmill.gates import build_gate
from syncmill.matcher import CapabilityMatcher, build_matcher
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.monitor import build_monitor, inner_dir
from syncmill.strategies import Strategy, _make_strategy
from syncmill.worktree import WorktreePool

logger = logging.getLogger(__name__)


def _matched_order(
    agents: list[AgentRunner], task: Task, matcher: CapabilityMatcher | None
) -> list[AgentRunner]:
    """Re-order agents by capability fit for this task; identity when no matcher.

    Capability matching (#9) layered OVER priority: a matching agent moves ahead
    of raw ``agent_priority``, and equal-fit agents keep priority order (the
    matcher's sort is stable). Returns a permutation — it never drops an agent, so
    Route's fallback chain / Pipeline's stages / Compete's competitors / Council's
    members stay intact and the ``if not agents`` guard still covers the empty case.

    Applied ONCE in the Supervisor (not threaded into ``Strategy.execute`` like
    the gate/breaker) because re-ordering has no topology-specific meaning: every
    strategy consumes the candidate list as a priority sequence, so re-ordering at
    the source makes all four capability-aware with no strategy change.
    """
    if matcher is None:
        return list(agents)
    return matcher.order(agents, task)


def _crash_result(task: Task) -> TaskResult:
    """A FAILED result standing in for an orchestration that raised before
    producing one — so a monitored run's OUTER reads a real failure (and a
    non-zero exit) instead of mistaking the crash for a clean detach."""
    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=1,
        duration_seconds=0.0,
        error="orchestration crashed before producing a result",
    )


class Supervisor:
    """Central orchestrator. The only component that touches both agents and memory."""

    def __init__(self, config: SyncmillConfig):
        self._config = config
        self._registry = AgentRegistry(config)
        self._memory = SyncmillMemory(config.memory)
        # Built once (like memory) because it carries cross-run state loaded from
        # disk and a `degraded` flag the CLI surfaces; None when disabled, so the
        # default path never touches the filesystem. (The gate, being stateless,
        # is built per-run inside `run`.)
        self._breaker = build_breaker(config.breaker)
        # Capability matcher (#9) — None when no agent declares a capability (the
        # default), so ordering stays at raw priority. Stateless/config-derived,
        # so it's built once here; the known agent ids surface a config typo.
        self._matcher = build_matcher(config.capabilities, config.agent_priority)

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
            # Capability matching (#9): re-order the candidate list by fit for
            # this task BEFORE the strategy runs, so every strategy sees a
            # capability-preferred priority sequence. No-op (identity) when
            # no matcher is configured or the task carries no hint. Not applied on
            # the forced-`agent_id` path above — forcing one agent has no ordering
            # to influence. Re-orders before the breaker's per-strategy OPEN-agent
            # filter (preference first, then availability), and never empties the
            # pool (it's a permutation), so the guard below still holds.
            agents = _matched_order(agents, task, self._matcher)

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

        # The task's worktree pool — the strategy acquires from it (one worktree
        # per agent, or a single shared one for a pipeline); the Supervisor owns
        # its lifetime and tears every worktree down in the finally below.
        repo_root = Path(self._config.repo_root).resolve()
        pool = WorktreePool(repo_root, str(task.id))

        # The quality gate (#10), built per-run from config like the pool — None
        # when no commands are configured, so gating is off and behavior is
        # unchanged. The strategy decides what a verdict means for its topology.
        gate = build_gate(self._config.gate)

        # The live tmux monitor (#16) — non-None only when this is the in-tmux
        # INNER process (inner_dir() set), so normal runs are unaffected. The
        # concurrent strategies tee per-agent output to it; finish() writes the
        # result.json the OUTER launcher reads back. Built per run like the gate.
        monitor = build_monitor(self._config.monitor, monitor_dir=inner_dir())

        # Start memory session (best-effort; None if memory is unavailable)
        session_id = run_async(
            self._memory.start_task_session(agents[0].agent_id, task)
        )

        result: TaskResult | None = None
        try:
            # Each acquired worktree starts clean at HEAD; the strategy diffs the
            # one that produced its result (via _collect_files_changed) so
            # files_changed reflects only that agent's work.
            result = self._strategy.execute(
                task=task,
                agents=agents,
                memory=self._memory,
                pool=pool,
                config=self._config,
                gate=gate,
                breaker=self._breaker,
                monitor=monitor,
            )

            # Share successful results to memory
            if result.status == TaskStatus.COMPLETED:
                run_async(self._memory.share_result(task, result))

            return result
        finally:
            # Hand the final result to the monitor first (writes result.json for the
            # OUTER, keeps the control pane's result on screen), then end the memory
            # session and tear every worktree down. If the strategy RAISED (result is
            # None), still finalize the monitor with a synthetic FAILED result so the
            # OUTER reads a real failure + non-zero exit code rather than mistaking the
            # crash for a clean detach ("exit 0"). Monitor finalization is
            # observability — guard it so a failure (e.g. result.json write error) can
            # NEVER skip the memory-session close or worktree cleanup below.
            if monitor is not None:
                try:
                    monitor.finish(result if result is not None else _crash_result(task))
                except Exception:
                    logger.warning(
                        "monitor finalization failed; continuing cleanup",
                        exc_info=True,
                    )
            if result is not None:
                run_async(self._memory.end_task_session(session_id, result))
            pool.cleanup_all()

    def close(self) -> None:
        run_async(self._memory.close())

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def memory(self) -> SyncmillMemory:
        return self._memory

    @property
    def breaker(self) -> CircuitBreaker | None:
        return self._breaker

    @property
    def matcher(self) -> CapabilityMatcher | None:
        return self._matcher
