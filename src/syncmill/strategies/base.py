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
from syncmill.models import GateReport, Task, TaskResult, TaskStatus
from syncmill.monitor import Monitor
from syncmill.tracer import SpanRecord, Tracer
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

    The optional ``tracer`` (S3′, completed by S6) is a retrospective span
    recorder: a strategy appends completed span records right after each
    attempt/fan-out returns (the same shared-loop boundary as
    ``_breaker_record``), via the ``_trace_*`` helpers and span builders below
    — never from inside a fan-out, and never through the agents (agents stay
    pure). Unlike ``monitor``, the SEQUENTIAL strategies use it too
    (retrospective recording is sync-safe): all five strategies record at
    their own attempt/phase boundaries. ``None`` means no tracing, so behavior
    is unchanged.
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
        tracer: Tracer | None = None,
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

    Also captures the full applyable ``patch`` (the ``--emit-patch`` recovery
    path) — here, at the same single seam, because this is the last point the
    winner's worktree still exists (the Supervisor's ``finally`` tears every
    worktree down). Patch collection comes AFTER the file listing:
    ``_git_diff_patch`` stages the tree (``git add -A``), which would empty
    ``_git_changed_files``'s untracked listing if run first.
    """
    if worktree.is_git and result.status == TaskStatus.COMPLETED:
        result.files_changed = list(_git_changed_files(worktree.path))
        result.patch = _git_diff_patch(worktree.path)


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


def _git_diff_patch(workdir: Path) -> str | None:
    """Full applyable patch of the worktree vs HEAD, untracked files included.

    Stages everything (``git add -A``) so new files enter the diff, then diffs
    the index with ``--binary`` so binary content survives a later ``git apply``.
    Mutating the index is harmless: the worktree is disposable and torn down
    right after (call this only from ``_collect_files_changed``, AFTER the file
    listing — staging empties the untracked listing). Output is decoded with
    ``surrogateescape`` so non-UTF-8 hunks round-trip byte-exact (the
    ``--emit-patch`` writer encodes with the same handler); ``None`` on any
    failure, like ``_git_changed_files`` — patch capture is best-effort and must
    never fail the run.
    """
    try:
        add = subprocess.run(
            ["git", "add", "-A"], capture_output=True, cwd=str(workdir)
        )
        if add.returncode != 0:
            return None
        proc = subprocess.run(
            ["git", "diff", "--binary", "--cached"],
            capture_output=True, cwd=str(workdir),
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", errors="surrogateescape")
    except Exception:
        return None


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


def _trace_record(tracer: Tracer | None, span: SpanRecord) -> None:
    """Append one completed span to the tracer (no-op without one). Shared so
    every strategy records through one seam — like ``_monitor_status``."""
    if tracer is not None:
        tracer.record(span)


def _span_status(raw_status: TaskStatus, error: str | None) -> str:
    """Map a RAW (pre-gate) execution outcome to the span-status vocabulary.

    ``TaskStatus`` has no TIMEOUT member — a timed-out agent is FAILED with the
    ``"Timed out after {n}s"`` error (``agents._failure``), which is the only
    timeout signal there is. ``cancelled`` exists in the contract vocabulary but
    is unreachable here: a cancelled agent never becomes a ``TaskResult`` (the
    cancellation re-raises out of the fan-out).
    """
    if raw_status == TaskStatus.COMPLETED:
        return "completed"
    if error is not None and error.startswith("Timed out after"):
        return "timeout"
    return "failed"


def _attempt_span(
    agent_id: str,
    *,
    node_id: str,
    parent: str,
    attempt: int,
    raw_status: TaskStatus,
    error: str | None,
    exit_code: int,
    end: float,
    duration: float,
    phase: str = "attempt",
    name: str | None = None,
    round_no: int | None = None,
    links: tuple[str, ...] = (),
    winner: bool = False,
    files_changed_count: int | None = None,
    worktree_slot: int | None = None,
) -> SpanRecord:
    """One agent execution as a span record — EVERY strategy's agent-run spans
    (attempt/stage/propose/critique/synthesize/plan/subtask) flow through this
    one builder so naming/attribute semantics can't drift between strategies.

    ``raw_status``/``error`` are the PRE-GATE snapshot — a gate rejection is a
    quality verdict on a *working* agent, so the attempt span keeps the raw
    ``completed`` and the verdict rides the separate gate span (the same reason
    ``_breaker_record`` takes the raw status). The span ``start`` is approximated
    as ``end − duration`` — safe because tracegraph never promotes timestamps to
    causality (they are only a prerank tiebreak). The OTLP error message is a
    bounded literal (``timeout`` | ``exit_code=N``), never raw agent output.
    ``syncmill.exit_code`` is emitted only for a real process exit (the ``-1``
    no-clean-exit sentinel is noise, not an exit code).

    Cross-strategy conventions (S6):
    - ``name`` defaults to ``{phase}:{agent_id}`` — a diff-stable structural
      label (only decompose's ``subtask:{i}:{agent}`` overrides it, since the
      round-robin repeats agent ids and the label needs the index).
    - ``attempt`` is the index embedded in the node id (route ``i``, pipeline
      stage ``i``, decompose subtask ``i``) and ``0`` for index-free nodes
      (compete attempts, propose/critique/synthesize/plan).
    - ``round_no`` is the council critique round → ``syncmill.round``.
    - ``links`` are the nodes whose results were PUBLISHED to ``shared`` at the
      previous phase boundary — declared availability, never claimed
      consumption: the cross-agent handoff is a relevance-ranked best-effort
      search, so syncmill cannot know what was actually consumed (the design's
      finding-6 approximation).
    - ``worktree_slot`` only on spans whose phase concurrently acquired fresh
      worktrees (compete attempts, council propose, decompose subtask/
      synthesize) — sequential steps and reused-worktree phases omit it.
    """
    status = _span_status(raw_status, error)
    attrs: dict[str, str | int | bool] = {
        "phase": phase,
        "agent_id": agent_id,
        "attempt": attempt,
        "status": status,
    }
    if round_no is not None:
        attrs["round"] = round_no
    if exit_code >= 0:
        attrs["exit_code"] = exit_code
    if worktree_slot is not None:
        attrs["worktree_slot"] = worktree_slot
    if winner:
        attrs["winner"] = True
    if files_changed_count is not None:
        attrs["files_changed_count"] = files_changed_count
    return SpanRecord(
        name=name if name is not None else f"{phase}:{agent_id}",
        kind="AGENT",
        node_id=node_id,
        node_parent=parent,
        links=links,
        start=max(end - duration, 0.0),
        end=end,
        status=status,
        error=None if status == "completed" else (
            "timeout" if status == "timeout" else f"exit_code={exit_code}"
        ),
        attrs=attrs,
    )


def _gate_span(
    agent_id: str,
    *,
    node_id: str,
    parent: str,
    report: GateReport,
    end: float,
) -> SpanRecord:
    """One gate evaluation as a TOOL span caused by its attempt. Emitted for
    every candidate that was actually gated (``result.gate is not None``), pass
    or fail — the verdict is the point. The error message is the bounded
    ``gate: <summary>`` form (check names/counts, never check output)."""
    duration = sum(check.duration_seconds for check in report.checks)
    status = "completed" if report.passed else "failed"
    return SpanRecord(
        name=f"gate:{agent_id}",
        kind="TOOL",
        node_id=node_id,
        node_parent=parent,
        start=max(end - duration, 0.0),
        end=end,
        status=status,
        error=None if report.passed else f"gate: {report.summary()}",
        attrs={
            "phase": "gate",
            "agent_id": agent_id,
            "status": status,
            "gate.passed": report.passed,
        },
    )


def _select_span(
    *,
    winner_node: str | None,
    links: list[str],
    start: float,
    end: float,
) -> SpanRecord:
    """The convergence (``_select_winner``) as a span: parented on the winning
    attempt (the promoted single cause — the win path stays a clean tree), or on
    ``run`` when nobody won; ``links`` carry the other raw-COMPLETED candidates
    (fan-in: the pool the selection was made over — the T1-golden reading of
    "examined", computable without changing lazy ``_select_winner``)."""
    return SpanRecord(
        name="select",
        kind="CHAIN",
        node_id="select",
        node_parent=winner_node if winner_node is not None else "run",
        links=tuple(links),
        start=start,
        end=end,
        status="completed",
        attrs={"phase": "select", "agent_id": "supervisor", "status": "completed"},
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
