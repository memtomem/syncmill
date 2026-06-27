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
from uuid import uuid4

from syncmill.agents import AgentRegistry, AgentRunner
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

        # The work-board (Phase 1) is LOAD-BEARING and built LAZILY — NOT here, so
        # an enabled-but-corrupt board can never break a plain `run` (which builds a
        # Supervisor but never touches `run_item`). `_require_board` opens it on
        # first board use; `close()` tears it down.
        self._board: Board | None = None

        self._strategy: Strategy = _make_strategy(config.default_strategy)

    def run(
        self,
        task: Task,
        agent_id: str | None = None,
        exclude: set[str] | None = None,
        memory: SyncmillMemory | None = None,
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
        """
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

        # Start memory session (best-effort; None if memory is unavailable)
        session_id = run_async(
            mem.start_task_session(agents[0].agent_id, task)
        )

        result: TaskResult | None = None
        try:
            # Each acquired worktree starts clean at HEAD; the strategy diffs the
            # one that produced its result (via _collect_files_changed) so
            # files_changed reflects only that agent's work.
            result = self._strategy.execute(
                task=task,
                agents=agents,
                memory=mem,
                pool=pool,
                config=self._config,
                gate=gate,
                breaker=self._breaker,
                monitor=monitor,
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
            if monitor is not None:
                try:
                    monitor.finish(result if result is not None else _crash_result(task))
                except Exception:
                    logger.warning(
                        "monitor finalization failed; continuing cleanup",
                        exc_info=True,
                    )
            if result is not None:
                run_async(mem.end_task_session(session_id, result))
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
        plain ``run`` selects its strategy in ``__init__``."""
        board = self._require_board()
        board.recover_stale_running()
        run_id = str(uuid4())
        item = (
            board.claim(item_id, run_id)
            if item_id is not None
            else board.next_runnable(run_id)
        )
        if item is None:
            return None  # unclaimable id / nothing runnable → caller's loop stops

        try:
            # Decisions are injected deterministically (Phase 2 will populate them;
            # v1 has none). The board-scoped memory keeps the enriched prompt out of
            # global `shared` / `agent-runtime:*`.
            task = Task(prompt=render_item_prompt(item, active_decisions=None))
            board_memory = self._memory.scoped(run_id)
            result = self.run(
                task, agent_id=agent_id, exclude=exclude, memory=board_memory
            )
            parsed = parse_board_update(result.output)
            # Pass run_id so apply_update is lease-fenced: if this item was reclaimed
            # + re-dispatched while we ran (a slow run past the lease, with a second
            # concurrent `board run`), the stale write is dropped, never clobbering
            # the new owner or reverting a verified state.
            _, applied = board.apply_update(
                item.id, parsed, result, actor=result.agent_id, run_id=run_id
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
            board.transition(
                item_id, WorkState.BLOCKED.value, actor="system",
                note=f"orchestration crashed: {type(exc).__name__}: {exc}"[:500],
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
