"""The Supervisor — the sole mediator between agents and memory.

Agents don't know about each other or about memory; the Supervisor wires them
to a ``Strategy`` (the orchestration topology — see ``syncmill.strategies``) and
to ``SyncmillMemory``. Strategies were extracted into the ``strategies`` package
(#12) once the four of them made this module unwieldy; the Supervisor keeps only
the run lifecycle, strategy selection, and the capability re-ordering it applies
before dispatch.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from uuid import uuid4

from syncmill.platform_support import require_supported_platform
from syncmill.agents import (
    AgentRegistry,
    AgentRunner,
    capture_tool_calls,
    model_routing_scope,
    provider_session_scope,
)
from syncmill.artifacts import ApplyLockBusy, ArtifactStore, _cross_process_lock
from syncmill.board import (
    Board,
    BoardError,
    WorkState,
    build_board,
    parse_board_update,
    render_item_prompt,
    strip_board_block,
)
from syncmill.breaker import CircuitBreaker, build_breaker
from syncmill.config import SyncmillConfig
from syncmill.gates import build_gate
from syncmill.matcher import CapabilityMatcher, build_matcher
from syncmill.memory import SyncmillMemory, run_memory_bounded
from syncmill.model_routing import (
    AllUnmappedError,
    ClassifierError,
    ModelRoutingDecision,
    build_decision,
    inherit_decision,
)
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.monitor import build_monitor, inner_dir
from syncmill.preflight import run_preflight
from syncmill.planning import build_run_plan
from syncmill.project import project_id
from syncmill.report import (
    RunReporter,
    build_reporter,
    patch_envelope_board,
    resolve_run_id,
    sweep_runs_dir,
)
from syncmill.strategies import Strategy, _make_strategy
from syncmill.tracer import build_tracer, flush_trace, run_span
from syncmill.worktree import (
    PatchCaptureError,
    WorktreePool,
    WorkspaceError,
    capture_repository_snapshot,
)

logger = logging.getLogger(__name__)

_SCRATCH_GC_BATCH_SIZE = 100


class _LeaseHeartbeat:
    """Renew a real Board claim from a connection owned by this thread."""

    def __init__(
        self, board: Board, item_id: str, run_id: str, interval: float | None = None
    ):
        self._board = board
        self._item_id = item_id
        self._run_id = run_id
        default_interval = max(0.25, min(60.0, board.lease_timeout_seconds / 3))
        self._interval = default_interval if interval is None else max(0.001, interval)
        self._stop = threading.Event()
        self._renewed = threading.Event()
        self._started = False
        self._warning: str | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"syncmill-lease-{item_id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        self._stop.set()
        # A never-started thread can't be joined (RuntimeError). `start()` is inside
        # the caller's crash guard, so the failed-to-start path reaches this stop().
        if not self._started:
            return
        self._thread.join(timeout=max(1.0, min(5.0, self._interval + 0.5)))

    def wait_for_renewal(self, timeout: float) -> bool:
        return self._renewed.wait(timeout)

    @property
    def warning(self) -> str | None:
        """Heartbeat health: why renewal stopped, or ``None``.

        Set when the claim was gone on a renewal (the item's state or lease owner
        changed under us) or when renewal itself broke. This is NOT a verdict on the
        run's board write — a renewal that merely broke transiently can still end
        with the fenced write landing. ``apply_update``'s ``applied`` decides that;
        this only supplies the reason when it comes back False.
        """
        return self._warning

    #: Consecutive renewal failures tolerated before the heartbeat gives up. A
    #: single transient sqlite error (a concurrent BEGIN IMMEDIATE held past
    #: busy_timeout) must not silently kill the heartbeat for the rest of a long
    #: run — the lease would expire under a HEALTHY run, stale recovery would
    #: reclaim it, and a second runner would redo (and fence-drop) the work.
    #: Retries reconnect (a broken connection stays broken) and are paced by
    #: ``_RETRY_DELAY_SECONDS``, so persistent breakage still surfaces within
    #: well under one lease timeout.
    _MAX_RENEWAL_FAILURES = 5
    _RETRY_DELAY_SECONDS = 5.0

    def _record_failure(self, exc: Exception, failures: int) -> None:
        logger.warning(
            "board lease renewal failed (attempt %d/%d)",
            failures,
            self._MAX_RENEWAL_FAILURES,
            exc_info=True,
        )
        # Provisional: a later successful renewal CLEARS it (the lease survived
        # — no false alarm), while a stop() before recovery leaves it standing
        # so the caller still learns renewal was broken when the run ended.
        self._warning = (
            f"board lease renewal failed for item {self._item_id} "
            f"({type(exc).__name__}: {exc}) — the lease may have expired"
        )

    def _run(self) -> None:
        conn: Board | None = None
        failures = 0
        try:
            # Prime the connection eagerly (the pre-retry behavior): a board
            # that is broken from the start surfaces on the first tick's fast
            # retry schedule instead of a full interval later.
            try:
                conn = self._board.new_connection()
            except Exception as exc:
                failures = 1
                self._record_failure(exc, failures)
            while True:
                delay = (
                    min(self._RETRY_DELAY_SECONDS, self._interval)
                    if failures
                    else self._interval
                )
                if self._stop.wait(delay):
                    return
                try:
                    if conn is None:
                        conn = self._board.new_connection()
                    renewed = conn.renew_claim(self._item_id, self._run_id)
                except Exception as exc:
                    failures += 1
                    self._record_failure(exc, failures)
                    # The connection may be what broke — rebuild it on retry.
                    if conn is not None:
                        with contextlib.suppress(Exception):
                            conn.close()
                        conn = None
                    if failures >= self._MAX_RENEWAL_FAILURES:
                        return
                    continue
                if not renewed:
                    # The renewal CAS matched no row: the item left `running` or its
                    # lease owner changed. Stale recovery + re-dispatch is the usual
                    # cause, but a manual transition does it too — so name what was
                    # observed, not a mechanism that may not have happened.
                    logger.warning(
                        "board: lost the lease on item %s (run %s) — its state or "
                        "lease owner changed while this run was working",
                        self._item_id, self._run_id,
                    )
                    self._warning = (
                        f"lost the board lease on item {self._item_id} — its state or "
                        f"lease owner changed while this run was working"
                    )
                    return
                failures = 0
                self._warning = None  # a successful renewal clears provisional trouble
                self._renewed.set()
        finally:
            if conn is not None:
                conn.close()


def _matched_order(
    agents: list[AgentRunner], task: Task, matcher: CapabilityMatcher | None
) -> list[AgentRunner]:
    """Re-order agents by capability fit for this task; identity when no matcher.

    Capability matching (#9) layered OVER priority: a matching agent moves ahead
    of raw ``agent_priority``, and equal-fit agents keep priority order (the
    matcher's sort is stable). Returns a permutation — it never drops an agent, so
    Route's fallback chain / Pipeline's stages / Compete's competitors / Council's
    members / Decompose's workers stay intact and the ``if not agents`` guard still
    covers the empty case.

    Applied ONCE in the Supervisor (not threaded into ``Strategy.execute`` like
    the gate/breaker) because re-ordering has no topology-specific meaning: every
    strategy consumes the candidate list as a priority sequence, so re-ordering at
    the source makes all five capability-aware with no strategy change.
    """
    if matcher is None:
        return list(agents)
    return matcher.order(agents, task)


def _crash_result(task: Task, run_id: str | None = None) -> TaskResult:
    """A FAILED result standing in for an orchestration that raised before
    producing one — so a monitored run's OUTER reads a real failure (and a
    non-zero exit) instead of mistaking the crash for a clean detach. Carries
    ``run_id`` like the success path, so consumers (the CLI's `run: <id>`
    line) can still name the run whose artifacts to inspect."""
    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=1,
        duration_seconds=0.0,
        error="orchestration crashed before producing a result",
        run_id=run_id,
    )


def _preflight_block_result(
    task: Task, reasons: tuple[str, ...], run_id: str | None = None
) -> TaskResult:
    rendered = ", ".join(reasons) or "policy rejected the run"
    return TaskResult(
        task_id=task.id,
        agent_id="preflight",
        status=TaskStatus.FAILED,
        output="",
        exit_code=1,
        duration_seconds=0.0,
        error=f"strict toolgraph preflight blocked execution: {rendered}",
        run_id=run_id,
    )


def _degraded_sources(mem: SyncmillMemory, breaker: CircuitBreaker | None) -> list[str]:
    """The envelope's ``degraded`` array: which best-effort subsystems degraded
    during this run. ``mem`` is the RUN's memory (the board-scoped view on a
    board run), not necessarily ``self._memory``. Open vocabulary — v1 knows
    memory and the breaker (the two carriers of a ``degraded`` flag)."""
    sources = []
    if mem.degraded:
        sources.append("memory")
    if breaker is not None and breaker.degraded:
        sources.append("breaker")
    return sources


def _append_trace_degradation(degraded: list[str], trace: dict | None) -> None:
    """Add file and streaming trace failures to the open degraded vocabulary."""
    if trace is None:
        return
    if trace.get("status") == "error":
        degraded.append("trace")
    stream = trace.get("stream")
    if isinstance(stream, dict) and stream.get("status") == "error":
        degraded.append("phoenix")


class Supervisor:
    """Central orchestrator. The only component that touches both agents and memory."""

    def __init__(self, config: SyncmillConfig):
        require_supported_platform()
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

        # The work-board (Phase 1) is LOAD-BEARING and built LAZILY — NOT here, so
        # an enabled-but-corrupt board can never break a plain `run` (which builds a
        # Supervisor but never touches `run_item`). `_require_board` opens it on
        # first board use; `close()` tears it down.
        self._board: Board | None = None

        self._strategy: Strategy = _make_strategy(config.default_strategy)
        # Where the last run's envelope landed (None when reporting is disabled
        # or the write degraded) — the CLI's read seam for the report-path line,
        # like `supervisor.memory` / `supervisor.breaker`.
        self.last_report_path: Path | None = None
        # The last run's advisory-preflight warnings — the CLI surfaces them as
        # yellow notices (the memory.degraded pattern). Reset per run.
        self.preflight_warnings: list[str] = []
        self.last_preflight_blocked = False
        self.last_run_id: str | None = None
        self.last_artifact_path: Path | None = None
        self.recovery_workspace: Path | None = None
        # The last run's model-routing decision (None when routing was off/
        # disabled for the run) + the classifier-degrade warning — the CLI's
        # read seams for the compact banner and the yellow degrade notice.
        self.last_routing_decision: ModelRoutingDecision | None = None
        self.last_routing_warning: str | None = None
        # Set by `run_item` when a board run's work was discarded (its lease was
        # lost / its fenced write dropped) — the CLI's read seam for that notice.
        # Reset per `run_item`, not per `run`: a plain run never touches the board.
        self.last_board_warning: str | None = None

    def _finalize_early_failure(
        self, failed: TaskResult, reporter: RunReporter | None
    ) -> TaskResult:
        """Close the observability surfaces for a failure that aborts a run before
        the strategy starts, then hand the result back.

        Both surfaces are observability, so both are guarded: a report or monitor
        failure must never replace the real cause with an unrelated traceback. The
        monitor is the load-bearing half — a monitored INNER that returns without
        writing result.json leaves the OUTER reporting an attach error instead of
        this failure (the same discipline as the routing- and strict-preflight-
        failure paths).
        """
        if reporter is not None:
            try:
                self.last_report_path = reporter.finish(failed, degraded=[])
            except Exception:
                logger.warning(
                    "early-failure report finalization failed", exc_info=True
                )
        monitor = build_monitor(self._config.monitor, monitor_dir=inner_dir())
        if monitor is not None:
            try:
                monitor.finish(failed)
            except Exception:
                logger.warning(
                    "early-failure monitor finalization failed", exc_info=True
                )
        return failed

    def run(
        self,
        task: Task,
        agent_id: str | None = None,
        exclude: set[str] | None = None,
        memory: SyncmillMemory | None = None,
        run_id: str | None = None,
        resume_session_id: str | None = None,
        parent_run_id: str | None = None,
        seed_patch: bytes | None = None,
        fork_session: bool = False,
        difficulty: str | None = None,
        inherited_routing: dict | None = None,
        enable_routing: bool = True,
    ) -> TaskResult:
        """Execute a task through the orchestration pipeline.

        Args:
            task: The task to execute.
            agent_id: If specified, force this agent only (no fallback).
            exclude: Agent ids to drop from the priority fallback — e.g. run one
                agent interactively yourself while syncmill orchestrates the
                rest. Forcing an excluded agent via ``agent_id`` is an error.
            memory: Override the memory used for THIS run (default ``self._memory``).
                ``run_item`` passes a board-scoped view so a board run's session
                prompt-log AND every strategy-internal share/search stay off the
                global ``shared`` namespace (decision #6). Plain ``run`` passes
                ``None`` → behavior-identical to before.
            run_id: The run's stable identity (the run-envelope join key),
                resolved through the single ``resolve_run_id`` seam: ``run_item``
                passes its board lease/scope id (one identity, no double
                issuance); a monitored inner picks up the OUTER-minted id from
                the snapshot env; a direct run mints a fresh uuid4.
            difficulty: The ``--difficulty`` request (``auto|low|medium|high``)
                for model routing; ``None`` defers to ``config.model_routing``.
            inherited_routing: A continuation's parent ``plan.model_routing``
                block (selector affinity). Only consulted when
                ``parent_run_id`` marks this run as a continuation — a legacy
                parent (``None``) then resumes with no model flag even under
                ``mode=observe``.
            enable_routing: ``run_item`` passes ``False`` — board runs are a
                PR-2 surface, and an observe-mode config must not classify the
                enriched board prompt (raw ``item.task`` is the contract input).
        """
        # The one identity every artifact of this run shares — the log, and (when
        # [integration.report] is enabled) the run-envelope under runs_dir/{run_id}.
        run_id = resolve_run_id(run_id)
        self.last_run_id = run_id
        self.last_artifact_path = None
        self.recovery_workspace = None
        logger.info("run %s: task %s", run_id, task.id)
        # Reset up front so an early argument-error return can't leave a PRIOR
        # run's report path / preflight warnings dangling on this Supervisor.
        self.last_report_path = None
        self.preflight_warnings = []
        self.last_preflight_blocked = False
        self.last_routing_decision = None
        self.last_routing_warning = None
        # Use `mem` for EVERY memory touchpoint below — register, session start, the
        # strategy's `memory=`, the success share, and session end — so a board run
        # (which passes a scoped view) can't leak the enriched prompt through any of
        # them via `self._memory`.
        mem = memory or self._memory
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

        # Difficulty-based model routing: ONE immutable decision per top-level
        # run, computed right after the lineup is final (post-matcher) and
        # before any side effect. A continuation (parent_run_id set) NEVER
        # classifies — it inherits the parent's effective selector policy
        # (selector affinity), a legacy parent meaning "no model flag" even
        # under mode=observe. Failures here are argument/metadata errors, not
        # runs (no reporter/memory/worktree has been touched yet).
        routing: ModelRoutingDecision | None = None
        if enable_routing:
            try:
                if parent_run_id is not None:
                    routing = inherit_decision(
                        inherited_routing, agent_id=agents[0].agent_id
                    )
                else:
                    routing = build_decision(
                        self._config.model_routing,
                        prompt=task.prompt,
                        lineup=[agent.agent_id for agent in agents],
                        requested=difficulty,
                    )
            except ClassifierError as exc:
                # Observe-only path: nothing would be applied, so a classifier
                # bug degrades (warn + no decision) instead of failing the run.
                self.last_routing_warning = (
                    f"model routing classifier failed ({exc}); "
                    "continuing without a recommendation"
                )
                logger.warning("run %s: %s", run_id, self.last_routing_warning)
            except (AllUnmappedError, ValueError) as exc:
                # An applied tier with no mapping anywhere, or a corrupt
                # inherited block — both fail closed before any provider call.
                failed = TaskResult(
                    task_id=task.id,
                    agent_id="routing",
                    status=TaskStatus.FAILED,
                    output="",
                    exit_code=-1,
                    duration_seconds=0.0,
                    error=str(exc),
                )
                # A monitored INNER must still write result.json (the strict-
                # preflight discipline) — without it the OUTER's detach path
                # reports an attach error instead of this routing failure.
                monitor = build_monitor(
                    self._config.monitor, monitor_dir=inner_dir()
                )
                if monitor is not None:
                    try:
                        monitor.finish(failed)
                    except Exception:
                        logger.warning(
                            "routing-failure monitor finalization failed",
                            exc_info=True,
                        )
                return failed
        self.last_routing_decision = routing

        # The run-report envelope writer (S3) — None when [integration.report] is
        # disabled (the default), so the disabled path touches no filesystem. Runs
        # start once a candidate lineup exists (the early returns above are
        # argument errors, not runs). Best-effort throughout: start() writes a
        # schema-valid skeleton (a SIGKILL'd run still leaves an honest "failed"
        # artifact); finish() in the finally below rewrites it with the truth.
        reporter = build_reporter(self._config.integration.report)
        if reporter is not None:
            try:
                reporter.start(
                    run_id=run_id,
                    task_id=str(task.id),
                    strategy=self._config.default_strategy,
                    team=self._config.applied_team,
                    agents=[a.agent_id for a in agents],
                )
            except Exception:
                # RunReporter.start has its own degrade net; this outer guard is
                # belt-and-suspenders (the monitor.finish discipline) so NOTHING
                # report-shaped can ever abort a run.
                logger.warning("report start failed; continuing run", exc_info=True)

        # Advisory preflight (S5) — run ONCE for the lead matched agent, BEFORE the
        # strategy and OUTSIDE the try/finally below (so it can never affect
        # cleanup). None when disabled; never raises — every failure degrades to a
        # warning and the run continues. The artifact lands in the run dir when
        # reporting is enabled; otherwise it runs (its identity warning is the
        # tier-1 value) and the artifact is discarded.
        preflight_out_dir: Path | None = None
        if reporter is not None:
            try:
                preflight_out_dir = (
                    Path(self._config.integration.report.runs_dir).expanduser() / run_id
                )
            except RuntimeError:  # unresolvable ~ — fall back to no artifact copy
                preflight_out_dir = None
        preflight = run_preflight(
            self._config.integration.toolgraph,
            run_id=run_id,
            agent_ids=[a.agent_id for a in agents],
            out_dir=preflight_out_dir,
        )
        self.preflight_warnings = list(preflight.warnings) if preflight else []

        # P3.1 strict enforcement is a normal failed run, not an exception.  It
        # stops before memory registration, worktree creation, or any agent
        # subprocess, but still completes every observability surface so monitor
        # outers and report consumers never see a stranded skeleton.
        if preflight is not None and preflight.should_block:
            self.last_preflight_blocked = True
            result = _preflight_block_result(task, preflight.block_reasons, run_id)
            monitor = build_monitor(self._config.monitor, monitor_dir=inner_dir())
            now = time.time()
            tracer = build_tracer(
                self._config.integration.trace,
                run_id=run_id,
                fallback_dir=self._config.integration.report.runs_dir,
                run_start=now,
            )
            trace_block: dict | None = None
            if tracer is not None:
                try:
                    tracer.record(
                        run_span(
                            strategy=self._config.default_strategy,
                            status="failed",
                            start=now,
                            end=now,
                        )
                    )
                except Exception:
                    logger.warning("strict preflight trace record failed", exc_info=True)
                trace_block = flush_trace(tracer)
            if reporter is not None:
                try:
                    degraded = _degraded_sources(mem, self._breaker)
                    _append_trace_degradation(degraded, trace_block)
                    self.last_report_path = reporter.finish(
                        result,
                        degraded=degraded,
                        trace=trace_block,
                        preflight=preflight.block,
                    )
                except Exception:
                    logger.warning("strict preflight report finalization failed", exc_info=True)
            if monitor is not None:
                try:
                    monitor.finish(result)
                except Exception:
                    logger.warning("strict preflight monitor finalization failed", exc_info=True)
            return result

        # Register agents in memory (best-effort)
        for agent in agents:
            run_memory_bounded(
                mem.register_agent(agent.agent_id),
                memory=mem, label="register_agent",
            )

        # The task's worktree pool — the strategy acquires from it (one worktree
        # per agent, or a single shared one for a pipeline); the Supervisor owns
        # its lifetime and tears every worktree down in the finally below.
        repo_root = Path(self._config.repo_root).resolve()
        supports_snapshot = bool(
            getattr(WorktreePool, "supports_snapshots", False)
        )
        snapshot = None
        if supports_snapshot:
            try:
                snapshot = capture_repository_snapshot(
                    repo_root,
                    run_id,
                    Path(self._config.workspace.root),
                    # A run's workspace is disposable, so claim it for the sweep
                    # before the clone starts — an interrupt in there would
                    # otherwise leave a directory nothing can ever reclaim.
                    sweepable=True,
                )
            except WorkspaceError as exc:
                failed = TaskResult(
                    task_id=task.id,
                    agent_id="workspace",
                    status=TaskStatus.FAILED,
                    output="",
                    exit_code=1,
                    duration_seconds=0.0,
                    error=str(exc),
                    run_id=run_id,
                )
                return self._finalize_early_failure(failed, reporter)
            repo_root = snapshot.source_root
            pool = WorktreePool(
                repo_root, str(task.id), snapshot=snapshot, seed_patch=seed_patch
            )
        else:
            pool = WorktreePool(repo_root, str(task.id))

        # From here the pool owns a real workspace on disk (a control clone under
        # workspace.root), so every path out of this setup block must tear it
        # down. The strategy's own try/finally starts further below, after several
        # constructions that can raise (the artifact store, the monitor's tmux
        # setup, the tracer's directory creation) — without this guard any of them
        # would leak the whole workspace permanently.
        try:
            artifact_store: ArtifactStore | None = None
            run_plan = build_run_plan(
                self._config,
                [agent.agent_id for agent in agents],
                model_routing=routing.as_dict() if routing is not None else None,
            ).as_dict()
            if snapshot is not None:
                try:
                    artifact_store = ArtifactStore(
                        Path(self._config.runs.root),
                        project_id(repo_root, self._config.project.id),
                    )
                    running_artifact = artifact_store.begin(
                        run_id=run_id,
                        task=task,
                        repo_root=repo_root,
                        snapshot_commit=snapshot.commit,
                        source_fingerprint=snapshot.source_fingerprint,
                        strategy=self._config.default_strategy,
                        agents=[agent.agent_id for agent in agents],
                        plan=run_plan,
                        parent_run_id=parent_run_id,
                    )
                    self.last_artifact_path = running_artifact.path
                except Exception as exc:
                    # No agent has run yet, so there is nothing to recover: the
                    # workspace holds only the empty control clone. Retaining it
                    # would promise the user a recovery workspace with no work in
                    # it AND leak it forever (a retained pool is never swept).
                    pool.cleanup_all()
                    failed = TaskResult(
                        task_id=task.id,
                        agent_id="artifact",
                        status=TaskStatus.FAILED,
                        output="",
                        exit_code=1,
                        duration_seconds=0.0,
                        error=f"failed to create mandatory run artifact: {exc}",
                        run_id=run_id,
                    )
                    return self._finalize_early_failure(failed, reporter)

            # The quality gate (#10), built per-run from config like the pool — None
            # when no commands are configured, so gating is off and behavior is
            # unchanged. The strategy decides what a verdict means for its topology.
            gate = build_gate(self._config.gate)

            # The live tmux monitor (#16) — non-None only when this is the in-tmux
            # INNER process (inner_dir() set), so normal runs are unaffected. The
            # concurrent strategies tee per-agent output to it; finish() writes the
            # result.json the OUTER launcher reads back. Built per run like the gate.
            monitor = build_monitor(self._config.monitor, monitor_dir=inner_dir())

            # The trace recorder (S3′) — None when [integration.trace] is disabled
            # (the default): no spans, no filesystem, byte-identical behavior. The
            # strategies append completed spans retrospectively; the finally below
            # records the run root span and flushes the OTLP artifact. fallback_dir
            # is the report runs_dir, so dir="" lands the trace beside the envelope.
            run_start = time.time()
            tracer = build_tracer(
                self._config.integration.trace,
                run_id=run_id,
                fallback_dir=self._config.integration.report.runs_dir,
                run_start=run_start,
            )

            # Start memory session (best-effort; None if memory is unavailable)
            session_id = run_memory_bounded(
                mem.start_task_session(agents[0].agent_id, task),
                memory=mem, label="start_task_session",
            )
        except BaseException:
            # Setup failed outside the run's own lifecycle try/finally. Clean the
            # workspace here so it can't leak, then let the failure propagate
            # unchanged — this guard adds cleanup, never swallowing.
            pool.cleanup_all()
            raise

        result: TaskResult | None = None
        try:
            # Retention for a trace-only run: with reporting disabled nobody has
            # swept {runs_dir} (RunReporter.start owns it otherwise), yet the
            # tracer's default dir IS that tree — so its run dirs would pile up
            # forever. Placed FIRST inside this try, not before it: an interrupt
            # mid-sweep must still enter finalization (the trace flush, and the
            # normal recovery-retention handling of the workspace) rather than
            # escaping it. A custom trace.dir is user-owned and never swept.
            trace_cfg = self._config.integration.trace
            if reporter is None and tracer is not None and not trace_cfg.dir:
                sweep_runs_dir(
                    self._config.integration.report.runs_dir,
                    self._config.integration.report.keep_last,
                    run_id,
                )
            # Each acquired worktree starts clean at HEAD; the strategy diffs the
            # one that produced its result (via _collect_files_changed) so
            # files_changed reflects only that agent's work.
            seed_sessions = (
                {agents[0].agent_id: resume_session_id}
                if resume_session_id and len(agents) == 1
                else None
            )
            # Off-path invariant: no decision → no scope at all (nullcontext),
            # so a disabled run's ContextVar surface is byte-identical to before.
            routing_cm = (
                model_routing_scope(routing.effective_selectors)
                if routing is not None
                else contextlib.nullcontext()
            )
            with routing_cm, provider_session_scope(
                self._config.conversations.enabled, seed_sessions, fork_session
            ):
                with capture_tool_calls(
                    tracer is not None
                    and self._config.integration.trace.tool_calls_enabled,
                    self._config.integration.trace.tool_name_map,
                ):
                    result = self._strategy.execute(
                        task=task,
                        agents=agents,
                        memory=mem,
                        pool=pool,
                        config=self._config,
                        gate=gate,
                        breaker=self._breaker,
                        monitor=monitor,
                        tracer=tracer,
                    )

            result.run_id = run_id
            # A successful coding result is not disposable telemetry. Persist it
            # before memory sharing and before any worktree is removed.
            if snapshot is not None:
                try:
                    if artifact_store is None:
                        raise RuntimeError("run artifact store was not initialized")
                    artifact = artifact_store.save(
                        run_id=run_id,
                        task=task,
                        result=result,
                        repo_root=repo_root,
                        snapshot_commit=snapshot.commit,
                        source_fingerprint=snapshot.source_fingerprint,
                        strategy=self._config.default_strategy,
                        parent_run_id=parent_run_id,
                        plan=run_plan,
                    )
                    result.artifact_path = str(artifact.path)
                    self.last_artifact_path = artifact.path
                    # Auto keep_last retention (#118): the one point a new run
                    # dir was just added, so pruning here bounds the store on
                    # every producer path. prune() only removes TERMINAL runs
                    # (never pending/applying/recovery_required) under the
                    # apply lock; belt-and-suspenders guard — retention must
                    # never fail a healthy, already-persisted run.
                    try:
                        # protect=run_id: `created_at` ranks by run START, so a
                        # long run finishing after `keep_last` newer runs began
                        # would otherwise prune the artifact it just saved.
                        artifact_store.prune(
                            self._config.runs.keep_last, protect=run_id
                        )
                    except Exception:
                        logger.warning(
                            "run-artifact retention sweep failed", exc_info=True
                        )
                except Exception as exc:
                    pool.retain()
                    self.recovery_workspace = pool.recovery_path
                    if artifact_store is not None:
                        try:
                            artifact_store.recovery_required(run_id, str(exc))
                        except Exception:
                            logger.warning(
                                "could not mark artifact save failure as recovery-required",
                                exc_info=True,
                            )
                    result.status = TaskStatus.FAILED
                    result.exit_code = result.exit_code or 1
                    result.error = (
                        f"failed to persist mandatory run artifact: {exc}; "
                        f"recovery workspace: {pool.recovery_path}"
                    )

            # Share successful results to memory
            if result.status == TaskStatus.COMPLETED:
                run_memory_bounded(
                    mem.share_result(task, result),
                    memory=mem, label="share_result",
                )

            return result
        except PatchCaptureError as exc:
            pool.retain()
            self.recovery_workspace = pool.recovery_path
            if artifact_store is not None:
                try:
                    artifact_store.recovery_required(run_id, str(exc), blocking=False)
                except Exception:
                    logger.warning("could not mark patch failure recovery-required", exc_info=True)
            result = _crash_result(task, run_id)
            result.error = f"{exc}; recovery workspace: {pool.recovery_path}"
            return result
        except BaseException as exc:
            if artifact_store is not None:
                try:
                    # retain() FIRST: it is a local suppress-guarded file write and
                    # exempts the workspace from the finally's cleanup_all().
                    # recovery_required() takes the blocking cross-process apply
                    # lock — if it raises (or a second Ctrl-C lands in it), the
                    # workspace must already be retained, or the finally would
                    # delete the crashed run's worktrees out from under recovery.
                    pool.retain()
                    self.recovery_workspace = pool.recovery_path
                    # Non-blocking: a Ctrl-C handler must never hang behind
                    # another process's long apply/verify holding the lock. The
                    # workspace is already retained above; an unmarked manifest
                    # is later reconciled by `syncmill recover`.
                    artifact_store.recovery_required(run_id, str(exc), blocking=False)
                except ApplyLockBusy:
                    logger.warning(
                        "apply lock busy; run %s not marked recovery-required "
                        "(workspace retained — run `syncmill recover %s`)",
                        run_id,
                        run_id,
                    )
                except Exception:
                    logger.warning("could not mark run recovery-required", exc_info=True)
            raise
        finally:
            # Hand the final result to the monitor first (writes result.json for the
            # OUTER, keeps the control pane's result on screen), then end the memory
            # session and tear every worktree down. If the strategy RAISED (result is
            # None), still finalize the monitor with a synthetic FAILED result so the
            # OUTER reads a real failure + non-zero exit code rather than mistaking the
            # crash for a clean detach ("exit 0"). Monitor finalization is
            # observability — guard it so a failure (e.g. result.json write error) can
            # NEVER skip the memory-session close or worktree cleanup below.
            # The envelope first — BEFORE monitor.finish (whose result.json write
            # unblocks the OUTER, whose report-path print is existence-gated: the
            # final envelope must already be on disk) and BEFORE pool.cleanup_all()
            # (the spec's ordering; no worktree race — patch/files_changed were
            # already captured by _collect_files_changed). Reporting is
            # observability — guard it like the monitor so a failure can NEVER
            # skip the memory-session close or worktree cleanup below.
            # The trace flush comes first — the reporter's finish digests the
            # artifact into the envelope's trace block, so the file must already
            # be on disk. Tracing is observability: both steps are guarded so a
            # tracer failure can NEVER skip the envelope, the memory-session
            # close, or the worktree cleanup below.
            trace_block: dict | None = None
            if tracer is not None:
                try:
                    final = result if result is not None else _crash_result(task, run_id)
                    tracer.record(
                        run_span(
                            strategy=self._config.default_strategy,
                            status=(
                                "completed"
                                if final.status == TaskStatus.COMPLETED
                                else "failed"
                            ),
                            start=run_start,
                            end=time.time(),
                        )
                    )
                except Exception:
                    logger.warning(
                        "trace run-span record failed; continuing cleanup",
                        exc_info=True,
                    )
                trace_block = flush_trace(tracer)  # never raises
            if reporter is not None:
                try:
                    degraded = _degraded_sources(mem, self._breaker)
                    _append_trace_degradation(degraded, trace_block)
                    self.last_report_path = reporter.finish(
                        result if result is not None else _crash_result(task, run_id),
                        degraded=degraded,
                        trace=trace_block,
                        preflight=preflight.block if preflight is not None else None,
                    )
                except Exception:
                    logger.warning(
                        "report finalization failed; continuing cleanup",
                        exc_info=True,
                    )
            if monitor is not None:
                try:
                    monitor.finish(result if result is not None else _crash_result(task, run_id))
                except Exception:
                    logger.warning(
                        "monitor finalization failed; continuing cleanup",
                        exc_info=True,
                    )
            # Ending the memory session is best-effort like everything else in
            # this finalizer: guard it so a memtomem hiccup can never strand the
            # worktrees. Cleanup is the last, unconditional step.
            try:
                if result is not None:
                    run_memory_bounded(
                        mem.end_task_session(session_id, result),
                        memory=mem, label="end_task_session",
                    )
            except Exception:
                logger.warning(
                    "memory session close failed; continuing cleanup",
                    exc_info=True,
                )
            pool.cleanup_all()

    # ── Board run lifecycle (Phase 1) ────────────────────────────────────────

    def _require_board(self) -> Board:
        """The board, built lazily on first use. Raises ``BoardError`` if the board
        is disabled (so a board CLI command run without ``SYNCMILL_BOARD__ENABLED``
        gets a clear message) — or if the db is corrupt (``build_board`` →
        ``Board.__init__`` raises). Stored on ``self._board`` so it's opened once
        per Supervisor and closed in ``close()``."""
        if self._board is None:
            self._board = build_board(self._config.board)
        if self._board is None:
            raise BoardError(
                "the work-board is disabled — enable it with "
                "SYNCMILL_BOARD__ENABLED=true (or [board] enabled=true in syncmill.toml)"
            )
        return self._board

    def run_item(
        self,
        item_id: str | None = None,
        agent_id: str | None = None,
        exclude: set[str] | None = None,
        goal: str | None = None,
    ) -> TaskResult | None:
        """Claim a board item, run it through the normal pipeline, and record the
        outcome back onto the board. The board's run-lifecycle wrapper around
        :meth:`run` — the ``Strategy`` protocol is unchanged.

        Returns the run's ``TaskResult``, or ``None`` when there is nothing to
        dispatch (an unclaimable id, or no runnable item) so a ``--loop`` caller
        stops. Flow: recover stale claims → atomically claim (``claim`` for an
        explicit id, else ``next_runnable``) → render the item DETERMINISTICALLY
        into the prompt → ``run`` it with a board-scoped memory (no global-``shared``
        leak) → parse the agent's optional board block → ``apply_update`` (advisory:
        an invalid agent ``done`` clamps to ``review``) → write ONE sanitized
        summary to global ``shared``. A genuine orchestration CRASH transitions the
        item ``→ blocked`` and re-raises (a FAILED result is NOT a crash —
        ``apply_update`` records it).

        ``--strategy`` is intentionally NOT a parameter: the board CLI mutates
        ``config.default_strategy`` BEFORE constructing the Supervisor, exactly as
        plain ``run`` selects its strategy in ``__init__``. A ``goal`` scopes the
        ``next_runnable`` claim to that goal (the ``board run --goal`` filter,
        Phase 4); the claim-by-id path ignores it — the CLI makes ``--id`` and
        ``--goal`` mutually exclusive."""
        board = self._require_board()
        self.last_board_warning = None
        board.recover_stale_running()
        run_id = str(uuid4())
        try:
            # Side-effect-free view construction validates the FULL backend scope
            # before any load-bearing board mutation. A missing backend remains
            # advisory and still returns a degraded view.
            board_memory = self._memory.scoped(run_id)
        except ValueError as exc:
            raise BoardError(str(exc)) from exc
        item = (
            board.claim(item_id, run_id)
            if item_id is not None
            else board.next_runnable(run_id, goal=goal)
        )
        if item is None:
            # Nothing to dispatch — housekeeping can afford to run NOW (no item
            # is waiting behind it). The scratch-namespace sweep used to run
            # BEFORE every claim, where a contended purge stalled dispatch by up
            # to its full budget per iteration; it now runs only when the claim
            # drained (here) or after an item's outcome is recorded (below).
            self._gc_board_run_namespaces(board)
            return None  # unclaimable id / nothing runnable → caller's loop stops

        heartbeat = (
            _LeaseHeartbeat(board, item.id, run_id)
            if isinstance(board, Board)
            else None
        )
        try:
            if heartbeat is not None:
                # Started INSIDE the crash guard: the claim already succeeded, so a
                # failure spawning the thread has to strand-proof the item like any
                # other crash instead of leaving it `running` until stale recovery
                # reclaims it a lease-timeout later.
                heartbeat.start()
            # Active team decisions for this item's goal are injected DETERMINISTICALLY
            # (in full, never via top-k recall) so the agent can't re-litigate them
            # (Phase 2, D1). Best-effort: a decisions read failure degrades to none
            # rather than blocking the item — decision injection is advisory prompt
            # enrichment (like memory context), not part of the load-bearing dispatch.
            try:
                decisions = [d.text for d in board.active_decisions(goal=item.goal)]
            except BoardError:
                logger.warning(
                    "board: failed to load decisions for item %s — running without",
                    item.id, exc_info=True,
                )
                decisions = []
            # The prevalidated board-scoped view keeps enriched writes out of global
            # memory and UNION-READs shared cross-run context (D4).
            task = Task(prompt=render_item_prompt(item, active_decisions=decisions))
            # Pass the lease/scope run_id through — lease, `board-run:{run_id}`
            # memory scope, and the run's own identity are ONE id (no double
            # issuance; the run-envelope contract's single-resolver rule).
            # Board runs are a PR-2 model-routing surface: routing is disabled
            # here so an observe-mode config can't classify the ENRICHED board
            # prompt (the contract input is the raw item.task, §4.1).
            result = self.run(
                task,
                agent_id=agent_id,
                exclude=exclude,
                memory=board_memory,
                run_id=run_id,
                enable_routing=False,
            )
            # The agent is finished. Stop renewing before the final board write,
            # which intentionally moves the item out of ``running``.
            lease_warning: str | None = None
            if heartbeat is not None:
                heartbeat.stop()
                lease_warning = heartbeat.warning
                heartbeat = None
            parsed = parse_board_update(result.output)
            # Pass run_id so apply_update is lease-fenced: if this item was reclaimed
            # + re-dispatched while we ran (a slow run past the lease, with a second
            # concurrent `board run`), the stale write is dropped, never clobbering
            # the new owner or reverting a verified state.
            _, applied = board.apply_update(
                item.id, parsed, result, actor=result.agent_id, run_id=run_id
            )
            if not applied:
                # The fence dropped this run's write: the item is no longer `running`
                # under our lease, so everything below (the envelope board block
                # aside) is deliberately suppressed and the agent's work is discarded.
                # Surface it — the run otherwise exits 0 looking like a normal success,
                # and the operator never learns the work was thrown away.
                # `last_board_warning` is the CLI seam (`last_report_path` /
                # `preflight_warnings` precedent).
                self.last_board_warning = lease_warning or (
                    f"board update for item {item.id} was dropped — this run no "
                    f"longer owns the lease (its state or lease owner changed)"
                )
                logger.warning("%s", self.last_board_warning)
            # Deliberately NOT set when `applied` is True, even if the heartbeat
            # reported a failure: a renewal that merely timed out behind a writer
            # (with no recovery in between) still ends with the fenced write landing,
            # and claiming that result "was NOT recorded" would contradict the board.
            # `applied` is the authoritative signal; the heartbeat's own trouble is
            # already logged where it happened.
            # Best-effort: stamp the finished envelope's `board` block (run()'s
            # own finally already finalized it — this is a read-modify-write).
            # Deliberately NOT gated on the lease fence: the fence protects the
            # board/shared system of record, while the envelope records what THIS
            # run worked on — its own identity, true either way. The enabled check
            # keeps the disabled path at zero filesystem touches.
            if self._config.integration.report.enabled:
                patch_envelope_board(
                    self._config.integration.report.runs_dir,
                    run_id,
                    item_id=item.id,
                    goal=item.goal or None,
                )
            # The ONE board-derived write to GLOBAL shared (cross-link tag), built
            # from the sanitized output — never the enriched prompt. Gated on the
            # SAME lease fence as the board write: if this run lost its lease (the
            # item's state or lease owner changed while we ran) apply_update dropped
            # the board update, so we must NOT publish this superseded run's output
            # to global `shared` either — otherwise the `board-result:{item_id}`
            # cross-link would carry discarded work beside the new owner's summary,
            # breaking the "single sanitized summary" / system-of-record contract.
            if applied:
                run_memory_bounded(
                    self._memory.share_board_result(
                        item.id, item.task, strip_board_block(result.output)
                    ),
                    memory=self._memory, label="share_board_result",
                )
            # Housekeeping AFTER the item's outcome is on the board — never on
            # the claim path (dispatch latency) and never on the crash path
            # (a Ctrl-C must not wait out a contended purge).
            self._gc_board_run_namespaces(board, exclude_run_id=run_id)
            return result
        except BaseException as exc:
            # A genuine crash (in run / parse / apply_update / share) — NOT a FAILED
            # result, which apply_update already recorded. Strand-proof the item by
            # moving it OUT of running, then re-raise.
            self._block_on_crash(board, item.id, run_id, exc)
            raise
        finally:
            if heartbeat is not None:
                heartbeat.stop()
            # A concurrent runner may have reclaimed this lease and purged its
            # scope while it was still finishing. Re-arm the durable tombstone
            # after all scoped-memory activity so a later sweep catches any
            # recreation. Best-effort: cleanup bookkeeping never masks a result.
            try:
                board.reactivate_board_run_namespace(run_id)
            except Exception:
                logger.warning(
                    "board: failed to reactivate scratch ledger for run %s",
                    run_id,
                    exc_info=True,
                )

    def _gc_board_run_namespaces(
        self, board: Board, *, exclude_run_id: str | None = None
    ) -> None:
        """Best-effort cleanup of board-owned, non-live scratch namespaces.

        Board SQLite now owns discovery and liveness in one snapshot: claims
        commit their lease and ledger row atomically. Only successful per-run
        backend calls are tombstoned; failures are scheduled for backoff retry
        (``record_board_run_purge_failures``) so one poisoned namespace can't
        head-of-line-block the sweep forever. A cross-process advisory lock
        (non-blocking, keyed by the board db) lets concurrent ``--loop`` workers
        SKIP a sweep another worker is already running instead of queueing on
        the same namespace locks to redo its work. ``exclude_run_id`` keeps the
        just-finished run's own namespace out of its post-run sweep (the finally
        re-arms its ledger row right after; purging it here would only mint a
        no-op re-purge next sweep).
        """
        try:
            db_path = getattr(board, "db_path", None)
            gc_lock = (
                _cross_process_lock(
                    Path(f"{db_path}.gc-lock"), blocking=False
                )
                if db_path is not None
                else contextlib.nullcontext()
            )
            try:
                with gc_lock:
                    self._gc_board_run_namespaces_locked(
                        board, exclude_run_id=exclude_run_id
                    )
            except ApplyLockBusy:
                logger.debug(
                    "board-run namespace GC already running in another process"
                )
        except Exception:
            logger.warning(
                "board-run namespace GC failed; continuing dispatch",
                exc_info=True,
            )

    def _gc_board_run_namespaces_locked(
        self, board: Board, *, exclude_run_id: str | None
    ) -> None:
        candidates = [
            rid
            for rid in board.collectible_board_run_ids(limit=_SCRATCH_GC_BATCH_SIZE)
            if rid != exclude_run_id
        ]
        if not candidates:
            return
        successful = run_memory_bounded(
            self._memory.delete_board_runs(candidates),
            memory=self._memory, label="delete_board_runs",
        )
        if successful is None:
            # Backend unavailable: no per-namespace verdict was reached — don't
            # penalize rows for an outage that would delay collection after the
            # backend recovers.
            return
        marked = board.mark_board_run_namespaces_purged(successful)
        if marked:
            logger.info("board: purged %d stale board-run namespace(s)", marked)
        failed = [rid for rid in candidates if rid not in set(successful)]
        if failed and hasattr(board, "record_board_run_purge_failures"):
            quarantined = board.record_board_run_purge_failures(failed)
            for rid in quarantined:
                logger.warning(
                    "board: scratch namespace for run %s failed to purge %d "
                    "times in a row — quarantined to a slow retry schedule; "
                    "its store residue likely needs operator attention "
                    "(symlinked source, unmounted index root, or legacy day "
                    "file)",
                    rid,
                    Board.PURGE_QUARANTINE_ATTEMPTS,
                )

    def _block_on_crash(
        self, board: Board, item_id: str, run_id: str, exc: BaseException
    ) -> None:
        """Best-effort, LEASE-FENCED: move a crashed run's item to ``blocked`` so it
        isn't stranded in ``running`` — but ONLY while this run still owns the claim.
        Passing ``run_id`` fences the transition exactly like ``apply_update``: if the
        item was reclaimed + re-dispatched while we ran (a slow run past its lease with
        a second concurrent ``board run``), or already left ``running`` (a crash AFTER
        ``apply_update`` resolved it — e.g. Ctrl-C during the shared-memory write), the
        fenced write matches no rows and is dropped, so a stale crashed run never
        clobbers the new owner or reverts a verified state. Never raises — a board
        failure here must not mask the original crash."""
        try:
            note = (
                "orchestration cancelled by operator (SIGINT)"
                if isinstance(exc, KeyboardInterrupt)
                else f"orchestration crashed: {type(exc).__name__}: {exc}"[:500]
            )
            board.transition(
                item_id, WorkState.BLOCKED.value, actor="system",
                note=note,
                # Also onto the ROW, not just the audit note: `board show` reads
                # `blocked_reason`, and a crashed item is exactly the one an operator
                # opens first.
                blocked_reason=note,
                human_required=True, run_id=run_id,
            )
        except Exception:
            logger.warning(
                "failed to mark item %s blocked after a crash", item_id, exc_info=True
            )

    def close(self) -> None:
        # The board close is in a `finally`: a RuntimeError out of the memory close
        # (a closed/foreign event loop) would otherwise skip it and leak the SQLite
        # connection — and in a long-lived process (`tui`, `board run --loop`) that
        # accumulates.
        try:
            # Bounded (cooperatively — see run_memory_bounded): an
            # await-shaped hang in the backend close must not hang shutdown.
            run_memory_bounded(self._memory.close(), label="memory close")
        finally:
            if self._board is not None:
                self._board.close()

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
