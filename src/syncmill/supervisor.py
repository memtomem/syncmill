"""Orchestration — Strategy pattern + Supervisor.

Supervisor is the sole mediator. Agents don't know about each other or about memory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from syncmill.agents import AgentRegistry, AgentRunner, aexecute_isolated
from syncmill.config import SyncmillConfig
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.worktree import Worktree, WorktreePool


# ── Strategy Protocol ────────────────────────────────────────────────────


@runtime_checkable
class Strategy(Protocol):
    """Orchestration strategy — determines how agents are selected and run.

    A strategy is handed a ``WorktreePool`` rather than a single ``workdir``: it
    owns its worktree *topology* (one isolated worktree per agent for concurrent
    isolation, or a single shared one for a sequential pipeline), acquiring from
    the pool as it runs. The Supervisor owns the pool's *lifetime* and tears
    every acquired worktree down afterward. The strategy also sets
    ``files_changed`` on the result it returns (only it knows which worktree
    produced that result) via ``_collect_files_changed``.
    """

    def execute(
        self,
        task: Task,
        agents: list[AgentRunner],
        memory: SyncmillMemory,
        pool: WorktreePool,
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


def _collect_files_changed(result: TaskResult, worktree: Worktree) -> None:
    """Record the files a completed agent changed in its own worktree.

    Per-worktree (not whole-repo), so each result's ``files_changed`` reflects
    only that agent's edits — the point of per-agent isolation. No-op for a
    failed result or a non-git (tempdir) workspace. Strategies call this on the
    result they return; the Supervisor no longer diffs centrally because with
    per-agent worktrees only the strategy knows which one produced the result.
    """
    if worktree.is_git and result.status == TaskStatus.COMPLETED:
        result.files_changed = list(_git_changed_files(worktree.path))


# ── RouteStrategy (v0.1 MVP) ────────────────────────────────────────────


class RouteStrategy:
    """Priority-based fallback. Try agents in order, fall back on failure."""

    def execute(
        self,
        task: Task,
        agents: list[AgentRunner],
        memory: SyncmillMemory,
        pool: WorktreePool,
        config: SyncmillConfig,
    ) -> TaskResult:
        # Search memory for relevant context
        enriched_prompt = _enrich_prompt(
            task.prompt, run_async(memory.search_context(task.prompt))
        )

        errors: list[TaskResult] = []
        for agent in agents:
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
            if result.status == TaskStatus.COMPLETED:
                _collect_files_changed(result, worktree)
                return result
            errors.append(result)

        # All agents failed — return aggregate error
        return _aggregate_failure(task, errors)


def _aggregate_failure(
    task: Task, errors: list[TaskResult], *, duration_seconds: float | None = None
) -> TaskResult:
    """Combine all failure reasons into a single TaskResult.

    ``duration_seconds`` defaults to the *sum* of the attempts — correct for the
    sequential RouteStrategy, whose agents run one after another. A concurrent
    strategy (CompeteStrategy) overrides it with the *max*, since its agents ran
    at once and the wall-clock is the slowest one, not their sum.
    """
    lines = []
    for e in errors:
        reason = e.error or f"exit_code={e.exit_code}"
        lines.append(f"  [{e.agent_id}] {reason}")
    combined_error = "All agents failed:\n" + "\n".join(lines)

    if duration_seconds is None:
        duration_seconds = sum(e.duration_seconds for e in errors)

    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=-1,
        duration_seconds=duration_seconds,
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
    ) -> TaskResult:
        if not agents:  # Supervisor guards this; stay safe if run standalone
            return _no_stages_failure(task)

        # One worktree shared by every stage: a pipeline's whole point is that
        # each stage builds on the working tree the previous stage left behind,
        # so — unlike RouteStrategy/Compete — it acquires a single workspace.
        worktree = pool.acquire(agents[0].agent_id)

        results: list[TaskResult] = []
        last = len(agents) - 1
        for stage, agent in enumerate(agents):
            # Each stage reads shared memory first, so it sees the prior stages'
            # outputs (plus any context from earlier syncmill runs).
            prompt = _enrich_prompt(
                task.prompt, run_async(memory.search_context(task.prompt))
            )
            timeout = config.agent_config(agent.agent_id).timeout_seconds
            result = agent.execute(prompt=prompt, workdir=worktree.path, timeout=timeout)
            results.append(result)

            if result.status != TaskStatus.COMPLETED:
                return _pipeline_failure(task, stage + 1, len(agents), results)

            # Hand this stage's output to the next one via shared memory
            # (best-effort). The final stage is shared by the Supervisor, so
            # don't double-write it here.
            if stage != last:
                run_async(memory.share_result(task, result))

        # The chain's product is the last stage's result; report the whole
        # pipeline's wall-clock as its duration, and the cumulative worktree diff
        # as its files_changed.
        final = results[-1]
        final.duration_seconds = round(sum(r.duration_seconds for r in results), 2)
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


# ── CompeteStrategy (v0.2) ──────────────────────────────────────────────


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
    ) -> TaskResult:
        if not agents:  # Supervisor guards this; stay safe if run standalone
            return _no_stages_failure(task)

        # Memory read happens FIRST, fully sync — never call run_async from inside
        # the gather below (that would re-enter the running loop). Every agent
        # competes from the same enriched starting context.
        enriched_prompt = _enrich_prompt(
            task.prompt, run_async(memory.search_context(task.prompt))
        )

        # One isolated worktree per agent, acquired UP FRONT and SERIALLY:
        # acquire() is sync git (contends on the index lock) and must not run
        # inside the concurrent fan-out. The comprehension over `agents` keeps the
        # lists the same length, so results/worktrees/agents stay aligned.
        worktrees = [pool.acquire(agent.agent_id) for agent in agents]

        # One concurrent batch on the shared loop; gather preserves submission
        # order, so results line up with `agents` and `worktrees`.
        results = run_async(
            aexecute_isolated(
                agents,
                enriched_prompt,
                [wt.path for wt in worktrees],
                lambda aid: config.agent_config(aid).timeout_seconds,
            )
        )

        winner = _select_winner(worktrees, results)
        if winner is None:  # nobody completed — same all-failed shape as Route,
            # but the agents ran concurrently, so wall-clock is the slowest, not
            # the sum.
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


def _select_winner(
    worktrees: list[Worktree],
    results: list[TaskResult],
) -> tuple[TaskResult, Worktree] | None:
    """First COMPLETED result in priority order, paired with its worktree;
    ``None`` if every agent failed.

    Deterministic: ``results`` (and the aligned ``worktrees``) arrive in the
    caller-fixed priority order — gather preserves submission order, and the
    strategy acquired worktrees in that same order — so selection iterates
    priority, never gather's completion order and never ``duration_seconds``.
    Identical inputs always pick the same winner. Failed/timed-out candidates are
    never eligible; losing is observed by running, not probing (no
    ``health_check``).

    This is the seam quality gates (#10) extend: that work will rank the COMPLETED
    candidates by a verified score with priority index as the final deterministic
    tie-break, without touching ``CompeteStrategy.execute`` or the fan-out helper.
    Until then, priority order is syncmill's established preference signal — it
    already drives Route fallback and Pipeline staging.
    """
    for worktree, result in zip(worktrees, results):
        if result.status == TaskStatus.COMPLETED:
            return result, worktree
    return None


# ── Supervisor ───────────────────────────────────────────────────────────

_STRATEGIES: dict[str, type[Strategy]] = {
    "route": RouteStrategy,
    "pipeline": PipelineStrategy,
    "compete": CompeteStrategy,
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

        # The task's worktree pool — the strategy acquires from it (one worktree
        # per agent, or a single shared one for a pipeline); the Supervisor owns
        # its lifetime and tears every worktree down in the finally below.
        repo_root = Path(self._config.repo_root).resolve()
        pool = WorktreePool(repo_root, str(task.id))

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
            )

            # Share successful results to memory
            if result.status == TaskStatus.COMPLETED:
                run_async(self._memory.share_result(task, result))

            return result
        finally:
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
