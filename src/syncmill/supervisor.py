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
import threading
import time
from pathlib import Path
from uuid import uuid4

from syncmill.agents import AgentRegistry, AgentRunner, capture_tool_calls
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
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.monitor import build_monitor, inner_dir
from syncmill.preflight import run_preflight
from syncmill.report import build_reporter, patch_envelope_board, resolve_run_id
from syncmill.strategies import Strategy, _make_strategy
from syncmill.tracer import build_tracer, flush_trace, run_span
from syncmill.worktree import WorktreePool

logger = logging.getLogger(__name__)

_SCRATCH_GC_BATCH_SIZE = 100


class _LeaseHeartbeat:
    """Renew a real Board claim from a connection owned by this thread."""

    def __init__(self, board: Board, item_id: str, run_id: str):
        self._board = board
        self._item_id = item_id
        self._run_id = run_id
        self._interval = max(0.25, min(60.0, board.lease_timeout_seconds / 3))
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"syncmill-lease-{item_id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1.0, min(5.0, self._interval + 0.5)))

    def _run(self) -> None:
        conn: Board | None = None
        try:
            conn = self._board.new_connection()
            while not self._stop.wait(self._interval):
                if not conn.renew_claim(self._item_id, self._run_id):
                    logger.warning("board lease lost for item %s", self._item_id)
                    return
        except Exception:
            logger.warning("board lease heartbeat failed", exc_info=True)
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


def _preflight_block_result(task: Task, reasons: tuple[str, ...]) -> TaskResult:
    rendered = ", ".join(reasons) or "policy rejected the run"
    return TaskResult(
        task_id=task.id,
        agent_id="preflight",
        status=TaskStatus.FAILED,
        output="",
        exit_code=1,
        duration_seconds=0.0,
        error=f"strict toolgraph preflight blocked execution: {rendered}",
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

    def run(
        self,
        task: Task,
        agent_id: str | None = None,
        exclude: set[str] | None = None,
        memory: SyncmillMemory | None = None,
        run_id: str | None = None,
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
        """
        # The one identity every artifact of this run shares — the log, and (when
        # [integration.report] is enabled) the run-envelope under runs_dir/{run_id}.
        run_id = resolve_run_id(run_id)
        logger.info("run %s: task %s", run_id, task.id)
        # Reset up front so an early argument-error return can't leave a PRIOR
        # run's report path / preflight warnings dangling on this Supervisor.
        self.last_report_path = None
        self.preflight_warnings = []
        self.last_preflight_blocked = False
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
            result = _preflight_block_result(task, preflight.block_reasons)
            monitor = build_monitor(self._config.monitor, monitor_dir=inner_dir())
            tracer = build_tracer(
                self._config.integration.trace,
                run_id=run_id,
                fallback_dir=self._config.integration.report.runs_dir,
            )
            trace_block: dict | None = None
            if tracer is not None:
                try:
                    now = time.time()
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
                    if trace_block is not None and trace_block.get("status") == "error":
                        degraded.append("trace")
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
            run_async(mem.register_agent(agent.agent_id))

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

        # The trace recorder (S3′) — None when [integration.trace] is disabled
        # (the default): no spans, no filesystem, byte-identical behavior. The
        # strategies append completed spans retrospectively; the finally below
        # records the run root span and flushes the OTLP artifact. fallback_dir
        # is the report runs_dir, so dir="" lands the trace beside the envelope.
        tracer = build_tracer(
            self._config.integration.trace,
            run_id=run_id,
            fallback_dir=self._config.integration.report.runs_dir,
        )
        run_start = time.time()

        # Start memory session (best-effort; None if memory is unavailable)
        session_id = run_async(
            mem.start_task_session(agents[0].agent_id, task)
        )

        result: TaskResult | None = None
        try:
            # Each acquired worktree starts clean at HEAD; the strategy diffs the
            # one that produced its result (via _collect_files_changed) so
            # files_changed reflects only that agent's work.
            with capture_tool_calls(
                tracer is not None and self._config.integration.trace.tool_calls_enabled,
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

            # Share successful results to memory
            if result.status == TaskStatus.COMPLETED:
                run_async(mem.share_result(task, result))

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
                    final = result if result is not None else _crash_result(task)
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
                    if trace_block is not None and trace_block.get("status") == "error":
                        degraded.append("trace")
                    self.last_report_path = reporter.finish(
                        result if result is not None else _crash_result(task),
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
                    monitor.finish(result if result is not None else _crash_result(task))
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
                    run_async(mem.end_task_session(session_id, result))
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
        stops. Flow: recover stale claims → GC stale board-run scratch
        namespaces → atomically claim (``claim`` for an explicit id, else
        ``next_runnable``) → render the item DETERMINISTICALLY
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
        board.recover_stale_running()
        # Sweep before mint/claim; a drained `--loop` iteration still performs
        # housekeeping for earlier runs.
        self._gc_board_run_namespaces(board)
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
            return None  # unclaimable id / nothing runnable → caller's loop stops

        heartbeat = (
            _LeaseHeartbeat(board, item.id, run_id)
            if isinstance(board, Board)
            else None
        )
        if heartbeat is not None:
            heartbeat.start()

        try:
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
            result = self.run(
                task,
                agent_id=agent_id,
                exclude=exclude,
                memory=board_memory,
                run_id=run_id,
            )
            parsed = parse_board_update(result.output)
            # Pass run_id so apply_update is lease-fenced: if this item was reclaimed
            # + re-dispatched while we ran (a slow run past the lease, with a second
            # concurrent `board run`), the stale write is dropped, never clobbering
            # the new owner or reverting a verified state.
            _, applied = board.apply_update(
                item.id, parsed, result, actor=result.agent_id, run_id=run_id
            )
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
            # item was reclaimed + re-dispatched while we ran) apply_update dropped
            # the board update, so we must NOT publish this superseded run's output
            # to global `shared` either — otherwise the `board-result:{item_id}`
            # cross-link would carry discarded work beside the new owner's summary,
            # breaking the "single sanitized summary" / system-of-record contract.
            if applied:
                run_async(
                    self._memory.share_board_result(
                        item.id, item.task, strip_board_block(result.output)
                    )
                )
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

    def _gc_board_run_namespaces(self, board: Board) -> None:
        """Best-effort cleanup of board-owned, non-live scratch namespaces.

        Board SQLite now owns discovery and liveness in one snapshot: claims
        commit their lease and ledger row atomically. Only successful per-run
        backend calls are tombstoned; every failure remains pending for retry.
        """
        try:
            candidates = board.collectible_board_run_ids(
                limit=_SCRATCH_GC_BATCH_SIZE
            )
            if not candidates:
                return
            successful = run_async(self._memory.delete_board_runs(candidates))
            if not successful:
                # None = backend unavailable; empty = every individual delete failed.
                return
            marked = board.mark_board_run_namespaces_purged(successful)
            if marked:
                logger.info(
                    "board: purged %d stale board-run namespace(s)", marked
                )
        except Exception:
            logger.warning(
                "board-run namespace GC failed; continuing dispatch",
                exc_info=True,
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
                human_required=True, run_id=run_id,
            )
        except Exception:
            logger.warning(
                "failed to mark item %s blocked after a crash", item_id, exc_info=True
            )

    def close(self) -> None:
        run_async(self._memory.close())
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
