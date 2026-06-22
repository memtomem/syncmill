"""Strategy Protocol + the helpers shared across every strategy.

This is the foundation layer of the ``strategies`` package: the ``Strategy``
Protocol every strategy structurally satisfies, plus the cross-cutting helpers
(prompt enrichment, gate/breaker plumbing, failure builders, winner selection,
per-worktree diff collection) that more than one strategy reuses. The concrete
strategy modules (``route``/``pipeline``/``compete``/``council``) import from
here; nothing here imports them, so the package stays acyclic. The Supervisor
sits *above* the strategy modules (it imports the registry), so diff collection
lives here — below the strategies — rather than in ``supervisor.py``, which
would create an import cycle.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from syncmill.agents import AgentRunner
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.memory import SyncmillMemory
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.monitor import Monitor
from syncmill.worktree import Worktree, WorktreePool

logger = logging.getLogger(__name__)


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

    The optional ``gate`` (#10) is a post-finish quality check the strategy runs
    against an agent's worktree before accepting its result; ``None`` means no
    gate is configured and the strategy accepts a completed result as today.

    The optional ``breaker`` (#8) is a reactive circuit breaker the strategy
    consults to skip agents whose circuit is OPEN (too many recent failures) and
    to record each agent's raw execution outcome; ``None`` means no breaker is
    configured and every agent runs as today.

    The optional ``monitor`` (#16) is a live tmux monitor the CONCURRENT strategies
    drive — announcing each phase, opening the output-streaming seam around each
    fan-out, and reporting per-agent status; the sequential strategies accept it
    and ignore it (there is nothing concurrent to watch). ``None`` means no monitor,
    so behavior is unchanged.
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


def _gate_accepts(
    result: TaskResult, worktree: Worktree, gate: QualityGate | None
) -> bool:
    """Run the quality gate against a COMPLETED result's worktree and judge it.

    Returns ``True`` when the result is accepted — no gate configured, or the gate
    passed. On a gate failure it returns ``False`` and **downgrades the result in
    place** to FAILED with a gate error, so the rejection is reported through the
    existing failure plumbing (RouteStrategy's aggregate, the Compete aggregate,
    the pipeline failure) rather than silently accepted. The ``GateReport`` is
    attached to ``result.gate`` either way (pass or fail) — an annotation like
    ``files_changed``, so callers/CLI can surface the verdict.

    Shared by every strategy so "passed the gate" means exactly one thing across
    them. Call only on a COMPLETED result — a failed agent is never gated (there's
    nothing trustworthy to check).
    """
    if gate is None:
        return True
    report = gate.evaluate(worktree)
    result.gate = report
    if report.passed:
        return True
    result.status = TaskStatus.FAILED
    result.error = f"quality gate failed: {report.summary()}"
    return False


def _breaker_runnable(
    agents: list[AgentRunner], breaker: CircuitBreaker | None
) -> list[AgentRunner]:
    """Agents whose circuit allows a run (CLOSED or HALF-OPEN); all of them when
    no breaker is configured.

    Skipping an OPEN agent is reactive back-off from its *recorded* past failures
    — never a liveness probe (no ``health_check``). Shared by every strategy so
    "is this agent currently allowed to run" means one thing across them.
    """
    if breaker is None:
        return list(agents)
    runnable: list[AgentRunner] = []
    for agent in agents:
        if breaker.allows(agent.agent_id):
            runnable.append(agent)
        else:
            logger.info(
                "circuit breaker open for %s — skipping this run", agent.agent_id
            )
    return runnable


def _breaker_record(
    breaker: CircuitBreaker | None, agent_id: str, status: TaskStatus
) -> None:
    """Record an agent's RAW execution outcome (no-op without a breaker).

    Called immediately after the agent runs and **before** the quality gate, so
    the breaker tracks *execution reliability* (did it run to completion?) — a
    gate rejection is a quality verdict on a working agent and must not trip the
    breaker. Shared by every strategy so recording stays identical across them.
    """
    if breaker is not None:
        breaker.record(agent_id, status)


def _breaker_all_open_failure(
    task: Task, agents: list[AgentRunner]
) -> TaskResult:
    """Failure returned when every candidate agent's circuit is OPEN.

    Distinct from ``_no_stages_failure``/"no agents available" so the user knows
    it's the breaker cooling down after repeated failures, not a config gap.

    Failing fast here is deliberate, not the breaker "aborting" work: every agent
    is within its cooldown after recent recorded failures, so honoring the
    back-off means there is nothing to run this invocation — the same shape as the
    quality gate failing the run when every output is rejected. (The *advisory*
    property — never blocking work — applies to a DEGRADED breaker, whose
    ``allows`` returns True for everyone; an OPEN circuit is the back-off working
    as designed.) The error tells the user how to recover: wait for a cooldown,
    or disable the breaker.
    """
    names = ", ".join(a.agent_id for a in agents)
    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=-1,
        duration_seconds=0.0,
        error=(
            f"All agents are circuit-open (cooling down after repeated failures): "
            f"{names}. Retry once a cooldown elapses, or disable the breaker."
        ),
    )


def _monitor_phase(monitor: Monitor | None, name: str) -> None:
    """Announce a phase to the live monitor (no-op without one). Shared so every
    concurrent strategy labels its phases through one seam."""
    if monitor is not None:
        monitor.phase(name)


def _monitor_streaming(monitor: Monitor | None, agent_ids: list[str]):
    """Open the output-streaming seam around a fan-out for ``agent_ids`` (and ensure
    each has a live pane); a plain no-op context when no monitor is configured.

    Wrap a concurrent strategy's ``run_async(aexecute_*)`` in this: every agent
    task the fan-out creates inherits the monitor's ``OutputSink`` (via the
    ``stream_to`` ContextVar), so its output tees to the agent's tmux pane live.
    """
    if monitor is None:
        return contextlib.nullcontext()
    return monitor.streaming(agent_ids)


def _monitor_status(
    monitor: Monitor | None, agent_id: str, status: TaskStatus
) -> None:
    """Report an agent's final outcome to the live monitor (no-op without one)."""
    if monitor is not None:
        monitor.status(agent_id, status)


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


def _select_winner(
    worktrees: list[Worktree],
    results: list[TaskResult],
    gate: QualityGate | None = None,
) -> tuple[TaskResult, Worktree] | None:
    """First gate-PASSING COMPLETED result in priority order, paired with its
    worktree; ``None`` if no candidate qualifies.

    Deterministic: ``results`` (and the aligned ``worktrees``) arrive in the
    caller-fixed priority order — gather preserves submission order, and the
    strategy acquired worktrees in that same order — so selection iterates
    priority, never gather's completion order and never ``duration_seconds``.
    Identical inputs always pick the same winner. Failed/timed-out candidates are
    never eligible; losing is observed by running, not probing (no
    ``health_check``).

    This is the seam quality gates (#10) extend — the promised hook is now live.
    With a ``gate``, each COMPLETED candidate is verified against its own worktree
    (via ``_gate_accepts``): a candidate that fails is downgraded to FAILED with a
    gate error *in place* (so the caller's aggregate failure names it) and is not
    eligible; the winner is the first candidate that both completed and passed, in
    priority order. So the ranking is "verified score (binary pass) with priority
    index as the final deterministic tie-break" — all passers score 1.0, so
    priority breaks the tie. Without a gate (``None``) this is the original
    "first COMPLETED in priority order". Evaluation is lazy: it stops at the first
    passer, so candidates after the winner aren't gated (losers, discarded anyway).

    Introduced by CompeteStrategy as its convergence seam and reused verbatim by
    CouncilStrategy to converge its synthesized candidates — the single, shared
    definition of "which finished candidate wins".
    """
    for worktree, result in zip(worktrees, results):
        if result.status == TaskStatus.COMPLETED and _gate_accepts(
            result, worktree, gate
        ):
            return result, worktree
    return None
