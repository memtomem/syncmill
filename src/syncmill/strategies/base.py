"""Strategy Protocol + the helpers shared across every strategy.

This is the foundation layer of the ``strategies`` package: the ``Strategy``
Protocol every strategy structurally satisfies, plus the cross-cutting helpers
(prompt enrichment, gate/breaker plumbing, failure builders, winner selection,
per-worktree diff collection) that more than one strategy reuses. The concrete
strategy modules
(``route``/``pipeline``/``compete``/``council``/``decompose``) import from here;
nothing here imports them, so the package stays acyclic. The Supervisor
sits *above* the strategy modules (it imports the registry), so diff collection
lives here — below the strategies — rather than in ``supervisor.py``, which
would create an import cycle.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

import time

from syncmill.agents import AgentRunner, provider_session_scope
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.git_env import isolated_git_env
from syncmill.memory import SyncmillMemory
from syncmill.models import GateReport, Task, TaskResult, TaskStatus, ToolOutcome
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
        base_commit = getattr(worktree, "base_commit", None)
        if base_commit is None:
            result.files_changed = list(_git_changed_files(worktree.path))
            result.patch = _git_diff_patch(worktree.path)
        else:
            result.files_changed = list(
                _git_changed_files(worktree.path, base_commit)
            )
            result.patch = _git_diff_patch(worktree.path, base_commit)


def _git_changed_files(workdir: Path, base_commit: str | None = None) -> set[str]:
    """Get files changed in worktree (staged + unstaged + untracked)."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", base_commit or "HEAD"],
            capture_output=True, text=True, cwd=str(workdir),
            env=isolated_git_env(),
        )
        changed = set(proc.stdout.strip().splitlines()) if proc.returncode == 0 else set()
        # Also check untracked
        proc2 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=str(workdir),
            env=isolated_git_env(),
        )
        if proc2.returncode == 0:
            changed.update(proc2.stdout.strip().splitlines())
        return changed
    except Exception:
        return set()


def _git_diff_patch(workdir: Path, base_commit: str | None = None) -> str | None:
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
            ["git", "add", "-A"], capture_output=True, cwd=str(workdir),
            env=isolated_git_env(),
        )
        if add.returncode != 0:
            return None
        proc = subprocess.run(
            ["git", "diff", "--binary", "--cached", base_commit or "HEAD"],
            capture_output=True, cwd=str(workdir), env=isolated_git_env(),
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


def _trace_attempt(tracer: Tracer | None, span: SpanRecord, result: TaskResult) -> None:
    """Record one attempt and body-free observed MCP calls below it."""
    _trace_record(tracer, span)
    telemetry = result.tool_telemetry
    if tracer is None or telemetry is None:
        return
    calls_by_sequence = {}
    for call in telemetry.calls:
        if call.sequence in calls_by_sequence:
            logger.warning(
                "ignored duplicate tool call sequence=%d; keeping first observed call",
                call.sequence,
            )
            continue
        calls_by_sequence[call.sequence] = call

    # Parent-before-child recording is required by the streaming Phoenix sink. Sorting the
    # de-duplicated projection also keeps the buffered file artifact deterministic when a
    # controlled producer supplies otherwise valid calls out of order.
    for call in sorted(calls_by_sequence.values(), key=lambda item: item.sequence):
        tool_parent = span.node_id
        if call.retry_of_sequence is not None:
            prior = calls_by_sequence.get(call.retry_of_sequence)
            valid_retry = (
                telemetry.complete
                and prior is not None
                and prior.sequence < call.sequence
                and prior.tool_key == call.tool_key
            )
            if valid_retry:
                marker_id = f"{span.node_id}:retry:{call.sequence}"
                _trace_record(
                    tracer,
                    SpanRecord(
                        name=f"retry:{call.tool_key}",
                        kind="CHAIN",
                        node_id=marker_id,
                        node_parent=f"{span.node_id}:tool:{prior.sequence}",
                        start=span.end,
                        end=span.end,
                        status="completed",
                        attrs={
                            "phase": "retry",
                            "agent_id": result.agent_id,
                            "status": "completed",
                            "tool.key": call.tool_key,
                            "tool.sequence": call.sequence,
                        },
                    ),
                )
                tool_parent = marker_id
            else:
                logger.warning(
                    "ignored invalid explicit tool retry relation sequence=%d retry_of=%d; "
                    "recording call without retry causality",
                    call.sequence,
                    call.retry_of_sequence,
                )
        status = (
            "completed"
            if call.outcome == ToolOutcome.SUCCEEDED
            else "failed"
            if call.outcome == ToolOutcome.FAILED
            else "unknown"
        )
        _trace_record(
            tracer,
            SpanRecord(
                name=call.tool_key,
                kind="TOOL",
                node_id=f"{span.node_id}:tool:{call.sequence}",
                node_parent=tool_parent,
                start=span.end,
                end=span.end,
                status=status,
                error="tool_failed" if status == "failed" else None,
                attrs={
                    "phase": "tool",
                    "agent_id": result.agent_id,
                    "status": status,
                    "tool.key": call.tool_key,
                    "tool.sequence": call.sequence,
                    "telemetry.source": telemetry.source,
                    "telemetry.complete": telemetry.complete,
                },
            ),
        )


def _gate_span(
    agent_id: str,
    *,
    node_id: str,
    parent: str,
    report: GateReport,
    end: float,
) -> SpanRecord:
    """One gate evaluation as a CHAIN span caused by its attempt. Emitted for
    every candidate that was actually gated (``result.gate is not None``), pass
    or fail — the verdict is the point. The error message is the bounded
    ``gate: <summary>`` form (check names/counts, never check output)."""
    duration = sum(check.duration_seconds for check in report.checks)
    status = "completed" if report.passed else "failed"
    return SpanRecord(
        name=f"gate:{agent_id}",
        kind="CHAIN",
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
    *,
    mode: str = "verified",
    agents: list[AgentRunner] | None = None,
    pool: WorktreePool | None = None,
    config: SyncmillConfig | None = None,
    tracer: Tracer | None = None,
) -> tuple[TaskResult, Worktree] | None:
    """Select one COMPLETED candidate under ``mode`` and pair it with its worktree.

    Deterministic: ``results`` (and the aligned ``worktrees``) arrive in the
    caller-fixed priority order — gather preserves submission order, and the
    strategy acquired worktrees in that same order — so selection iterates
    priority, never gather's completion order and never ``duration_seconds``.
    Identical inputs always pick the same winner. Failed/timed-out candidates are
    never eligible; losing is observed by running, not probing (no
    ``health_check``).

    Three modes, all reading the same priority-ordered candidate list:
    ``verified`` (the default) walks gate-passing candidates in priority order;
    ``priority`` checks only the first completed candidate; ``judge`` gates every
    completer, lets an agent rank the verified pool, and degrades to verified
    priority if judging fails.

    This is the seam quality gates (#10) extend — the promised hook is now live.
    With a ``gate``, each COMPLETED candidate it examines is verified against that
    candidate's own worktree (via ``_gate_accepts``): a candidate that fails is
    downgraded to FAILED with a gate error *in place* (so the caller's aggregate
    failure names it) and is not eligible. Under ``verified`` the winner is then
    the first candidate that both completed and passed, in priority order — the
    ranking is "verified score (binary pass) with priority index as the final
    deterministic tie-break", all passers scoring 1.0 so priority breaks the tie —
    and evaluation is lazy: it stops at the first passer, so candidates after the
    winner aren't gated (losers, discarded anyway). ``judge`` deliberately is not
    lazy: it must gate every completer to know the verified pool it ranks. Without
    a gate (``None``) both reduce to the original "first COMPLETED in priority
    order".

    Introduced by CompeteStrategy as its convergence seam and reused verbatim by
    CouncilStrategy and DecomposeStrategy for synthesized candidates — the single,
    shared definition of "which finished candidate wins".
    """
    if mode not in {"verified", "priority", "judge"}:
        # SelectionConfig's Literal makes this unreachable from config; a
        # programmatic caller with a typo'd mode used to silently degrade to
        # `verified` via an implicit `!=` — fail like _make_strategy instead.
        raise ValueError(f"unknown selection mode: {mode!r}")
    completed = [
        (worktree, result)
        for worktree, result in zip(worktrees, results)
        if result.status == TaskStatus.COMPLETED
    ]
    if mode == "priority":
        if not completed:
            return None
        worktree, result = completed[0]
        return (result, worktree) if _gate_accepts(result, worktree, gate) else None

    if mode == "verified":
        for worktree, result in completed:
            if _gate_accepts(result, worktree, gate):
                return result, worktree
        return None

    verified = [
        (worktree, result)
        for worktree, result in completed
        if _gate_accepts(result, worktree, gate)
    ]
    if not verified:
        return None
    if len(verified) == 1 or not agents or pool is None or config is None:
        result_wt, result = verified[0]
        return result, result_wt

    judge_id = config.selection.judge_agent
    judge = next((agent for agent in agents if agent.agent_id == judge_id), None)
    if judge is None:
        judge = agents[0]
    candidate_ids = {result.agent_id for _, result in verified}
    # Judging is a preference between candidates that ALREADY passed the gate, so
    # every step of it is best-effort: acquiring the judge's worktree can raise
    # WorkspaceError (disk pressure, a git failure) and executing the judge is a
    # real subprocess. Letting either escape would unwind through the strategy
    # into the Supervisor's `except BaseException`, marking a run that has a
    # perfectly good verified winner as recovery-required. Any failure degrades to
    # the same verified-priority fallback a bad verdict already uses.
    try:
        sections: list[str] = []
        for worktree, result in verified:
            patch = _git_diff_patch(
                worktree.path, getattr(worktree, "base_commit", None)
            )
            sections.append(
                f"CANDIDATE {result.agent_id}\n"
                f"REPORT:\n{result.output[:6000]}\n"
                f"PATCH:\n{(patch or '')[:12000]}"
            )
        judge_prompt = (
            "Act as a read-only coding judge. Compare the verified candidates below "
            "for correctness, scope, maintainability, and test evidence. Do not edit "
            "files. End with exactly `WINNER: <agent-id>`.\n\n"
            + "\n\n---\n\n".join(sections)
        )
        judge_worktree = pool.acquire(f"{judge.agent_id}-judge")
        # No provider session for the judge: its worktree is a throwaway path,
        # so a session keyed on it would mint a fresh conversation per run —
        # and on a continuation (seed set) the seeded PARENT conversation would
        # be attached to the judge key, appending this read-only ranking prompt
        # to the user's continued conversation history.
        with provider_session_scope(enabled=False):
            judge_start = time.time()
            verdict = judge.execute(
                judge_prompt,
                judge_worktree.path,
                config.agent_config(judge.agent_id).timeout_seconds,
            )
            judge_end = time.time()
    except Exception:
        logger.warning(
            "LLM judge could not run; using verified priority", exc_info=True
        )
        worktree, result = verified[0]
        return result, worktree
    # The judge is a whole agent invocation — without a span the trace
    # under-reports the run's agent calls by exactly the +1 the RunPlan
    # promises. Parent = run (like any fan-out root); raw status, per the
    # attempt-span convention.
    _trace_attempt(
        tracer,
        _attempt_span(
            judge.agent_id,
            node_id=f"judge:{judge.agent_id}",
            parent="run",
            attempt=0,
            raw_status=verdict.status,
            error=verdict.error,
            exit_code=verdict.exit_code,
            end=judge_end,
            duration=verdict.duration_seconds or (judge_end - judge_start),
            phase="judge",
        ),
        verdict,
    )
    if verdict.status == TaskStatus.COMPLETED:
        matches = re.findall(r"(?im)^WINNER:\s*([^\s]+)\s*$", verdict.output)
        if matches and matches[-1] in candidate_ids:
            chosen = matches[-1]
            for worktree, result in verified:
                if result.agent_id == chosen:
                    return result, worktree
    logger.warning("LLM judge returned no valid winner; using verified priority")
    worktree, result = verified[0]
    return result, worktree
