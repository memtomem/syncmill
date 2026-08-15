"""Circuit breaker (#8) — reactive back-off from repeatedly-failing agents.

`RouteStrategy` already falls back on a *single* failure, but keeps no memory of
*repeated* failures across runs. The breaker adds that memory: after an agent
fails ``failure_threshold`` times in a row its circuit OPENS and it's skipped for
``cooldown_seconds``; once the cooldown elapses the circuit goes HALF-OPEN (one
trial allowed); any success CLOSES it (resets the count).

REACTIVE, never a liveness probe — honoring the "no ``health_check`` on agents"
invariant. The breaker never calls an agent to test it. Skipping an OPEN agent is
"remember it kept failing when we actually ran it," not "ping it to see if it's
alive." ``allows()`` decides purely from *recorded past outcomes*; failures are
observed exactly as before (run, then fall back on failure).

Like quality gates, the breaker is plumbed through the **strategy layer only** —
agents stay pure ``execute(prompt, workdir, timeout)`` functions and never see it.
The Supervisor builds it from config and injects it; each strategy filters
OPEN agents out of its run and records each agent's raw execution outcome.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from syncmill.config import BreakerConfig
from syncmill.models import TaskStatus

logger = logging.getLogger(__name__)


@dataclass
class _AgentState:
    """Per-agent breaker state. ``failures`` is the consecutive-failure count;
    ``opened_at`` is the wall-clock of the most recent failure once the threshold
    was crossed (``None`` while the circuit is closed)."""

    failures: int = 0
    opened_at: float | None = None


class CircuitBreaker:
    """Per-agent reactive back-off with persistent, across-run state.

    The three states are DERIVED from ``(failures, opened_at)``, never stored:

      * CLOSED    — ``failures < threshold``                       → allowed
      * OPEN      — ``failures >= threshold`` and within cooldown   → skipped
      * HALF_OPEN — ``failures >= threshold`` and cooldown elapsed   → allowed (one trial)

    Transitions happen only via ``record`` (``allows`` is side-effect-free)::

        CLOSED  --record(FAILED) × threshold-->  OPEN
        OPEN    --cooldown elapses (no action)-->  HALF_OPEN
        HALF_OPEN --record(COMPLETED)--> CLOSED      (failures=0, opened_at=None)
        HALF_OPEN --record(FAILED)----> OPEN         (opened_at=now → cooldown restarts)

    **Persistence is what makes "N failures in a row" meaningful.** A one-shot
    ``syncmill run`` is a fresh process that runs an agent at most once, so the
    count only accumulates *across* runs. State is a JSON map
    ``{agent_id: {failures, opened_at}}`` at ``state_path``, loaded on
    construction and rewritten atomically (unique temp file + ``os.replace``)
    after each ``record`` — a crash can leak a temp file but never a
    half-written ``breaker.json``. A long-lived process (``board run --loop``
    holds one Supervisor — and one breaker — for the whole drain) would
    otherwise diverge from other processes' writes indefinitely, so ``allows``
    first does a best-effort mtime-guarded reload: when the file changed on
    disk since this instance last read or wrote it, the in-memory state is
    replaced from disk (still last-writer-wins; a stat/read failure keeps the
    current state).

    **Best-effort / advisory**, like ``SyncmillMemory``: every file error is
    caught, sets ``degraded``/``last_error``, and never raises — a broken state
    file must never abort a task. While degraded, ``allows`` returns ``True`` for
    everyone (permissive: don't block agents because the breaker's own bookkeeping
    broke). ``degraded`` reflects the LATEST persistence outcome, not a one-way
    latch: a later successful load or save clears it, so a transient failure at
    construction can't leave a long-lived process permissive forever.

    Uses WALL-CLOCK ``time.time`` (not ``monotonic``) so a cooldown survives
    process restarts; ``now`` is injectable for tests. Concurrency across
    simultaneous syncmill processes is last-writer-wins — the atomic write
    prevents a corrupt/torn file, not a lost increment, and an occasional lost
    increment for an advisory back-off self-corrects on the next run.
    """

    def __init__(
        self,
        failure_threshold: int,
        cooldown_seconds: int,
        state_path: str,
        *,
        now: Callable[[], float] = time.time,
    ):
        self._threshold = max(1, failure_threshold)
        self._cooldown = max(0, cooldown_seconds)
        # expanduser here: the config field is a raw string (so a `~` set via
        # SYNCMILL_BREAKER__STATE_PATH wouldn't be expanded at config-build time).
        self._path = Path(state_path).expanduser()
        self._now = now
        self.degraded = False
        self.last_error: str | None = None
        # Marker BEFORE read (the _reload_if_stale ordering): if another
        # process replaces the file between the two, we pair a NEWER read with
        # an older marker and merely reload once more — stat-after-read would
        # pair the OLD state with the new file's marker and mask that writer's
        # state from every later `allows` until the next write.
        self._mtime_ns: int | None = self._stat_mtime_ns()
        self._state: dict[str, _AgentState] = self._load()

    # ── decision (read-only) ──────────────────────────────────────────────

    def allows(self, agent_id: str) -> bool:
        """True if the agent may run now (CLOSED or HALF-OPEN); False if OPEN.

        Never records an outcome — safe to call repeatedly. In the one-shot CLI
        an agent runs at most once per invocation, so a single ``allows`` +
        later ``record`` yields the classic "one trial per cooldown" without
        ``allows`` recording anything. It does refresh: a best-effort reload
        picks up other processes' writes when the state file changed on disk
        (the ``board run --loop`` long-lived case). While ``degraded`` it
        returns ``True`` (don't block agents because the breaker's own state
        file is unreadable).
        """
        self._reload_if_stale()
        if self.degraded:
            return True
        st = self._state.get(agent_id)
        if st is None or st.failures < self._threshold:
            return True  # CLOSED
        if st.opened_at is None:
            # Defensive: threshold reached but no timestamp (e.g. hand-edited
            # state) — don't trap the agent OPEN forever with no way to recover.
            return True
        # Clamp a backward wall-clock jump to 0 so it can't make `elapsed`
        # negative and wedge the agent OPEN.
        elapsed = max(0.0, self._now() - st.opened_at)
        return elapsed >= self._cooldown  # HALF_OPEN (allow) vs OPEN (skip)

    def is_half_open(self, agent_id: str) -> bool:
        """True when this agent is allowed exactly ONE trial, not free running.

        ``allows`` deliberately cannot express this — it answers a single yes/no
        and records nothing, which is right for a caller that runs an agent at
        most once. A caller that may dispatch the SAME agent several times at
        once (a workflow batch) has to know the difference, or it spends one
        cooldown's single trial on every node in the batch simultaneously.
        """
        self._reload_if_stale()
        if self.degraded:
            return False
        st = self._state.get(agent_id)
        return (
            st is not None
            and st.failures >= self._threshold
            and st.opened_at is not None
            and max(0.0, self._now() - st.opened_at) >= self._cooldown
        )

    # ── outcome recording (mutates + persists) ────────────────────────────

    def record(self, agent_id: str, status: TaskStatus) -> None:
        """Record an agent's RAW execution outcome and persist.

        ``COMPLETED`` closes the circuit (resets the count); anything else counts
        as a failure (increments, and once the threshold is crossed (re)stamps
        ``opened_at`` so each failure restarts the cooldown). Pass the agent's
        *execution* status — taken right after the agent runs, BEFORE any quality
        gate — so a gate rejection (a verdict on a *working* agent's output) does
        not trip the breaker.
        """
        if status == TaskStatus.COMPLETED:
            self._state[agent_id] = _AgentState(failures=0, opened_at=None)
        else:
            st = self._state.setdefault(agent_id, _AgentState())
            st.failures += 1
            if st.failures >= self._threshold:
                st.opened_at = self._now()  # (re)start cooldown from the latest failure
        self._save()

    # ── persistence (best-effort) ─────────────────────────────────────────

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _reload_if_stale(self) -> None:
        """Replace in-memory state from disk when another process wrote it.

        Best-effort: a stat failure (file missing, transient I/O) keeps the
        current state and never degrades — only an actual re-read of a broken
        file does (inside ``_load``, same as construction). mtime_ns guards the
        common no-change case down to one ``stat`` per ``allows``.
        """
        mtime = self._stat_mtime_ns()
        if mtime is None or mtime == self._mtime_ns:
            return
        self._state = self._load()
        self._mtime_ns = mtime

    def _load(self) -> dict[str, _AgentState]:
        try:
            if not self._path.exists():
                self._recover()
                return {}
            raw = json.loads(self._path.read_text())
        except (OSError, ValueError) as exc:
            self._degrade(f"failed to load breaker state: {exc}")
            return {}
        if not isinstance(raw, dict):
            # Valid JSON but the wrong top-level shape (e.g. `[]` or `42`) is a
            # broken state file, not an empty one — surface it (degrade) rather
            # than silently treating it as healthy/empty.
            self._degrade(f"breaker state is not a JSON object: {type(raw).__name__}")
            return {}
        state: dict[str, _AgentState] = {}
        # Lenient per-entry parse: a single malformed agent entry is skipped, not
        # fatal — one bad record shouldn't wipe or block all the others.
        for agent_id, data in raw.items():
            try:
                # Clamp a negative count (corrupt/hand-edited) up to 0: a negative
                # failures stays < threshold no matter how many real failures
                # follow, silently suppressing the back-off.
                failures = max(0, int(data["failures"]))
                opened = data.get("opened_at")
                opened_at = float(opened) if opened is not None else None
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
            if opened_at is not None and (
                not math.isfinite(opened_at) or opened_at > self._now()
            ):
                # A bogus persisted timestamp slips past the `opened_at is None`
                # guard in allows() and wedges the agent OPEN forever, because
                # `max(0, now - opened_at)` never reaches the cooldown:
                #   * NaN/Infinity — json.loads accepts these tokens, and the
                #     arithmetic with them clamps to 0.
                #   * a finite value in the FUTURE (a huge corrupt number, or one
                #     written under clock skew) — now - opened_at is negative, also
                #     clamped to 0.
                # Normalize either to None so the defensive "no timestamp" path
                # (allow, don't trap) applies instead. record() only ever writes a
                # sane time.time(), so this can't reject a value we wrote.
                opened_at = None
            state[str(agent_id)] = _AgentState(failures=failures, opened_at=opened_at)
        self._recover()
        return state

    def _save(self) -> None:
        payload = {
            aid: {"failures": st.failures, "opened_at": st.opened_at}
            for aid, st in self._state.items()
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: write a unique temp file in the SAME directory (so
            # os.replace stays on one filesystem and is atomic), then replace.
            fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(payload, f)
                # The marker of the state WE wrote: os.replace preserves the
                # temp inode's mtime, so stat the temp file BEFORE the replace.
                # Stat'ing state_path afterwards would race a concurrent
                # writer — its replace between ours and the stat pairs OUR
                # in-memory state with ITS file marker, and every later
                # `allows` skips the reload that would have picked it up.
                written_mtime_ns = os.stat(tmp).st_mtime_ns
                os.replace(tmp, self._path)
            except BaseException:
                # Don't leak the temp file if the write/replace failed.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except (OSError, ValueError) as exc:
            self._degrade(f"failed to save breaker state: {exc}")
        else:
            # This instance is now the latest writer: remember our own mtime so
            # _reload_if_stale doesn't re-read a file we just wrote.
            self._mtime_ns = written_mtime_ns
            self._recover()

    def _degrade(self, msg: str) -> None:
        self.degraded = True
        self.last_error = msg
        logger.warning("circuit breaker degraded: %s", msg)

    def _recover(self) -> None:
        """Clear ``degraded`` after a successful load or save — the flag
        reflects the LATEST persistence outcome, not a one-way latch (a
        transient failure at construction must not leave a long-lived process
        permissive forever)."""
        if self.degraded:
            logger.info("circuit breaker recovered: %s", self.last_error)
        self.degraded = False
        self.last_error = None


def build_breaker(config: BreakerConfig) -> CircuitBreaker | None:
    """The breaker described by ``config``, or ``None`` when disabled
    (``enabled=False``, the default) — strategies skip breaker logic and behavior
    is unchanged. Mirrors ``build_gate``: a single place that decides "configured
    or not," so the disabled path never touches the filesystem."""
    if not config.enabled:
        return None
    return CircuitBreaker(
        failure_threshold=config.failure_threshold,
        cooldown_seconds=config.cooldown_seconds,
        state_path=config.state_path,
    )
