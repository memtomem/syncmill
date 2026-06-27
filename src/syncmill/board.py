"""Persistent agent work-board (Phase 1) — the data foundation.

syncmill today is a single-run, stateless orchestrator: a ``Task`` is one
ephemeral prompt, gone after the run. The board adds durable, *structured* records
of a multi-agent team's work — the goal, who owns which piece, what's blocked and
why, what's done **and verified**, whether work is being duplicated, and where a
human must step in. For an orchestration harness this is the infrastructure that
makes collaboration *measurable*.

**Why SQLite, not memtomem.** memtomem stores markdown+embedding *knowledge*
chunks — no structured status/dependency fields, no state machine, no atomic
compare-and-swap. The board needs exactly those, so it gets its own SQLite store
at ``~/.syncmill/board.db`` (a flat module, gateway/monitor/session precedent —
don't pre-abstract into a subpackage). *State lives only on the board; knowledge
lives only in memtomem's ``shared``; the two cross-link by id* (the Supervisor
writes one sanitized ``board-result:{item_id}`` summary to ``shared`` after a run).

**Load-bearing, built LAZILY.** The board is the source of work and the system of
record, so — unlike memory/breaker, which degrade silently — a corrupt/unusable
board RAISES :class:`BoardError` rather than running untracked work or re-running
``done`` items. It is disabled by default (:func:`build_board` → ``None``) and the
Supervisor builds it lazily (NOT in ``__init__``), so an enabled-but-corrupt board
can never break a plain ``syncmill run`` that never touches it.

**Agents stay pure.** Orchestrated agents NEVER touch the board. The Supervisor
mediates: it renders an item into the prompt (:func:`render_item_prompt`) and
parses an OPTIONAL ``syncmill-board`` fenced block back out of the agent's output
(:func:`parse_board_update` — its own parser, strict, degrade-to-``{}``, never
raises, in the spirit of ``DecomposeStrategy._parse_subtasks``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable

from syncmill.config import BoardConfig
from syncmill.models import TaskResult, TaskStatus

logger = logging.getLogger(__name__)


class BoardError(RuntimeError):
    """A board operation failed in a way that must NOT be silently degraded.

    The board is the source of work and the system of record, so — unlike memory
    or the circuit breaker, which are advisory — a board failure (corrupt db, a
    rejected ``done`` transition, an unknown state) surfaces. The CLI converts it
    to a clean ``click.ClickException`` (the ``session.SessionLaunchError``
    "no fallback — it IS the feature" contract)."""


# ── State machine + value types ──────────────────────────────────────────────


class WorkState(StrEnum):
    """The work-item lifecycle: ``planned → running → (blocked | review) → done``.

    ``review`` vs ``done`` is the load-bearing distinction: ``review`` means the
    agent finished but the work is **not yet verified**; ``done`` means
    **done AND verified** — a transition to ``done`` requires structured passing
    :class:`Evidence` (or a human sign-off), enforced in :meth:`Board.transition`.
    """

    PLANNED = "planned"
    RUNNING = "running"
    BLOCKED = "blocked"
    REVIEW = "review"
    DONE = "done"


#: Terminal-ish states an item rests in once it leaves ``running``. Used to clear
#: the lease (a claim is consumed the moment the item stops running).
_STATES = frozenset(s.value for s in WorkState)


@dataclass
class Evidence:
    """One structured proof that an item's work was verified — the thing the
    ``done`` gate counts. ``kind`` is ``"gate"`` (a passing ``GateReport`` maps to
    one), ``"test"``, ``"manual"`` (a human-attached ref), or ``"human"`` (a
    sign-off). ``passed`` is the verdict; ``ref`` names the evidence (a gate
    summary, a CI url, …); ``by`` is who/what produced it; ``at`` is wall-clock."""

    kind: str
    ref: str
    passed: bool
    at: float
    by: str = ""


@dataclass
class WorkItem:
    """One tracked unit of team work. Lives here (not ``models.py``) because — like
    ``breaker._AgentState`` — it never flows through a strategy; only the board and
    the Supervisor's ``run_item`` touch it.

    ``owner_agent`` is RECORD-ONLY in v1 (stamped from ``result.agent_id`` after a
    run), not an assignment constraint — so ``board add`` has no ``--owner``.
    """

    id: str
    goal: str
    task: str
    state: WorkState = WorkState.PLANNED
    owner_agent: str | None = None
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    handoff: str = ""
    human_required: bool = False
    blocked_reason: str = ""
    depends_on: list[str] = field(default_factory=list)
    lease_run_id: str | None = None
    claimed_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


# ── SQLite schema ────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_items (
    id            TEXT PRIMARY KEY,
    goal          TEXT NOT NULL DEFAULT '',
    task          TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'planned'
                  CHECK (state IN ('planned','running','blocked','review','done')),
    owner_agent   TEXT,
    inputs        TEXT NOT NULL DEFAULT '[]',
    outputs       TEXT NOT NULL DEFAULT '[]',
    evidence      TEXT NOT NULL DEFAULT '[]',
    handoff       TEXT NOT NULL DEFAULT '',
    human_required INTEGER NOT NULL DEFAULT 0,
    blocked_reason TEXT NOT NULL DEFAULT '',
    depends_on    TEXT NOT NULL DEFAULT '[]',
    lease_run_id  TEXT,
    claimed_at    REAL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_items_state ON work_items(state);
CREATE INDEX IF NOT EXISTS idx_work_items_goal  ON work_items(goal);
CREATE INDEX IF NOT EXISTS idx_work_items_owner ON work_items(owner_agent);

CREATE TABLE IF NOT EXISTS transitions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT NOT NULL,
    actor      TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_item ON transitions(item_id);
"""

# Column order for INSERT/row-mapping — kept in one place so the two stay in sync.
_COLUMNS = (
    "id", "goal", "task", "state", "owner_agent", "inputs", "outputs", "evidence",
    "handoff", "human_required", "blocked_reason", "depends_on", "lease_run_id",
    "claimed_at", "created_at", "updated_at",
)


# ── Board store ──────────────────────────────────────────────────────────────


class Board:
    """The SQLite-backed work-board. The dispatch + system-of-record core.

    Concurrency model: opened in **autocommit** mode (``isolation_level=None``) so
    writers issue an explicit ``BEGIN IMMEDIATE`` (a RESERVED lock taken up front),
    making :meth:`claim` / :meth:`next_runnable` an atomic compare-and-swap — two
    ``syncmill board run`` processes against one db can never both claim the same
    ``planned`` item. ``busy_timeout`` lets the loser wait for the winner's commit
    rather than erroring. WAL keeps readers (``board list``/``show``, the future
    dashboard) non-blocking against a writer.

    ``now`` is injectable (like the circuit breaker) so tests can drive the
    stale-claim clock deterministically.
    """

    def __init__(
        self,
        db_path: str,
        *,
        now: Callable[[], float] = time.time,
        lease_timeout_seconds: int = 1800,
        busy_timeout_ms: int = 5000,
    ):
        self._now = now
        self._lease_timeout = max(1, lease_timeout_seconds)
        path = Path(db_path).expanduser()
        try:
            if path.parent and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None → autocommit; we drive BEGIN IMMEDIATE/COMMIT by
            # hand for the atomic claims. row_factory=Row for name-based access.
            self._conn = sqlite3.connect(str(path), isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
        except (sqlite3.Error, OSError) as exc:
            # A corrupt / non-sqlite / unwritable db is fatal for a load-bearing
            # store — raise rather than degrade (the whole point of the board).
            # OSError covers an unwritable/missing parent dir (mkdir / connect);
            # sqlite3.Error covers a corrupt or non-sqlite file. `connect()` is lazy
            # (it succeeds before the first PRAGMA/schema access surfaces a corrupt
            # file), so close the partial connection here rather than leaking its fd.
            conn = getattr(self, "_conn", None)
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            raise BoardError(f"failed to open board at {path}: {exc}") from exc

    # ── writes (atomic) ──────────────────────────────────────────────────────

    def add(
        self,
        task: str,
        *,
        goal: str = "",
        depends_on: list[str] | None = None,
        human_required: bool = False,
        inputs: list[str] | None = None,
        item_id: str | None = None,
    ) -> WorkItem:
        """Insert a new ``planned`` item and return it (with an audit row)."""
        now = self._now()
        item = WorkItem(
            id=item_id or str(uuid.uuid4()),
            goal=goal,
            task=task,
            state=WorkState.PLANNED,
            inputs=list(inputs or []),
            human_required=human_required,
            depends_on=list(depends_on or []),
            created_at=now,
            updated_at=now,
        )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            self._conn.execute(
                f"INSERT INTO work_items ({','.join(_COLUMNS)}) "
                f"VALUES ({','.join('?' for _ in _COLUMNS)})",
                _item_to_row(item),
            )
            self._insert_transition(item.id, None, item.state.value, "human", "created", now)
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._safe_rollback()
            raise BoardError(f"failed to add item: {exc}") from exc
        return item

    def claim(self, item_id: str, run_id: str) -> WorkItem | None:
        """Atomically claim a specific ``planned`` item for ``run_id`` (CAS
        ``planned → running``). Returns the claimed item, or ``None`` if it doesn't
        exist or isn't ``planned`` (already claimed/done/blocked/etc.).

        The atomic, every-path no-duplicate-work guard: under ``BEGIN IMMEDIATE``
        the second concurrent claimer blocks until the first commits, then reads
        ``running`` and returns ``None``."""
        return self._claim_where("id=? AND state=?",
                                 (item_id, WorkState.PLANNED.value), run_id)

    def next_runnable(self, run_id: str) -> WorkItem | None:
        """Atomically claim the next dispatchable item for ``run_id``, or ``None``.

        Dispatchable = ``planned``, not ``human_required``, and every ``depends_on``
        item is ``done``. Items are considered in FIFO order (``created_at``). The
        whole select-then-claim runs in one ``BEGIN IMMEDIATE`` transaction, so it
        is as atomic as :meth:`claim`."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT * FROM work_items WHERE state=? AND human_required=0 "
                "ORDER BY created_at, rowid",
                (WorkState.PLANNED.value,),
            ).fetchall()
            chosen = next(
                (r for r in rows if self._deps_satisfied(json.loads(r["depends_on"]))),
                None,
            )
            if chosen is None:
                self._conn.execute("COMMIT")  # read-only; nothing to dispatch
                return None
            item = self._stamp_running(chosen, run_id)
            self._conn.execute("COMMIT")
            return item
        except Exception as exc:
            # Broad on purpose: a non-sqlite error mid-transaction (e.g. a corrupt
            # `depends_on` JSON failing `json.loads` above → ValueError) must still
            # roll back the open BEGIN IMMEDIATE — otherwise the RESERVED lock leaks
            # and the next op fails with "cannot start a transaction within a
            # transaction". Surfaces as BoardError, like get()/list() already do.
            self._safe_rollback()
            raise BoardError(f"next_runnable failed: {exc}") from exc

    def apply_update(
        self,
        item_id: str,
        parsed: dict,
        result: TaskResult,
        actor: str,
        run_id: str | None = None,
    ) -> tuple[WorkItem, bool]:
        """Record a finished run back onto the item, in one transaction.

        Returns ``(item, applied)``: ``applied`` is ``True`` when this run's write
        took effect, ``False`` when the lease fence dropped it (below). The caller
        (``run_item``) gates its follow-on global-``shared`` cross-link on
        ``applied`` so a superseded run never publishes discarded output.

        Merges the agent's optional parsed board block with inferred defaults —
        ``result.files_changed`` ∪ ``parsed['outputs']`` → ``outputs``; a
        ``GateReport`` (``result.gate``) → one :class:`Evidence` row; the run's
        status + the agent's advisory ``parsed['state']`` → the next state — and
        stamps ``owner_agent`` (record-only). ``blocked_reason`` is cleared on any
        non-blocked resolution, so a re-run that succeeds doesn't carry a stale
        reason from a prior block.

        **Lease-fenced** when ``run_id`` is given (the ``run_item`` path): the
        write is a compare-and-swap ``WHERE id=? AND state='running' AND
        lease_run_id=?``. If a slow run's item was reclaimed by
        ``recover_stale_running`` and re-dispatched to another runner while it ran,
        the fence matches 0 rows → this stale write is DROPPED (it neither clobbers
        the new owner's state nor reverts a verified ``done``), and the current item
        is returned untouched WITH ``applied=False`` so the caller can suppress any
        follow-on side effect (the global-``shared`` summary). Without ``run_id`` the
        write is unfenced (for non-dispatch callers/tests) and ``applied`` is True.

        **Advisory, never raises on a bad agent request** (decision #5): an agent
        that requests ``state:done`` without passing evidence is CLAMPED to
        ``review`` (+ an audit note), so a successful run is never turned into
        ``blocked`` by an invalid advisory field. A FAILED run (agent failure or a
        gate rejection) maps to ``blocked`` with the error as the reason. Only a
        genuine sqlite error raises :class:`BoardError`."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT * FROM work_items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise BoardError(f"no such item: {item_id}")
            item = _row_to_item(row)
            now = self._now()

            outputs = _merge_unique(item.outputs, parsed.get("outputs", []))
            outputs = _merge_unique(outputs, list(result.files_changed))

            evidence = list(item.evidence)
            run_evidence: list[Evidence] = []  # only THIS run's verification
            if result.gate is not None:
                # A passing gate is objective verification; a failing gate is the
                # evidence the work did NOT verify. Agents can't self-certify done.
                ev = Evidence(
                    kind="gate", ref=result.gate.summary(),
                    passed=result.gate.passed, at=now, by=result.agent_id,
                )
                evidence.append(ev)
                run_evidence.append(ev)

            handoff = parsed.get("handoff", item.handoff)
            human_required = bool(parsed.get("human_required", item.human_required))
            blocked_reason = ""  # cleared unless we resolve to blocked (no stale reason)
            requested = parsed.get("state")

            if result.status == TaskStatus.COMPLETED:
                # Gate the agent's advisory `done` on THIS run's verification only —
                # stale passing evidence from an earlier (now-discarded) run must not
                # certify the current work ("done AND verified" = the CURRENT work is
                # verified). The full accumulated `evidence` is still persisted below
                # and still backs the human review→done promotion via `transition`.
                target, note = _resolve_completed_state(requested, run_evidence)
                if target == WorkState.BLOCKED:
                    blocked_reason = (
                        str(parsed.get("blocked_reason", "")) or "agent reported blocked"
                    )
                    human_required = True  # a blocked item needs a human to unblock
            else:
                target = WorkState.BLOCKED
                blocked_reason = result.error or f"run failed (exit {result.exit_code})"
                human_required = True
                note = "run failed"

            set_params = [
                target.value, result.agent_id, json.dumps(outputs),
                _dump_evidence(evidence), handoff, int(human_required),
                blocked_reason, now,
            ]
            where, where_params = "id=?", [item_id]
            if run_id is not None:
                # Lease fence: only the run that still owns the running claim writes.
                where += " AND state=? AND lease_run_id=?"
                where_params += [WorkState.RUNNING.value, run_id]
            cur = self._conn.execute(
                "UPDATE work_items SET state=?, owner_agent=?, outputs=?, evidence=?, "
                "handoff=?, human_required=?, blocked_reason=?, lease_run_id=NULL, "
                f"claimed_at=NULL, updated_at=? WHERE {where}",
                set_params + where_params,
            )
            if run_id is not None and cur.rowcount == 0:
                # Lost lease: the item was reclaimed + re-dispatched while we ran.
                # Drop this stale write entirely (record nothing) so we never clobber
                # the new owner or revert a verified state.
                self._conn.execute("COMMIT")
                logger.warning(
                    "board: dropping stale apply_update for %s — run %s no longer "
                    "owns the lease (reclaimed/re-dispatched)", item_id, run_id,
                )
                return self.get(item_id), False  # type: ignore[return-value]
            self._insert_transition(item_id, item.state.value, target.value, actor, note, now)
            self._conn.execute("COMMIT")
            return self.get(item_id), True  # type: ignore[return-value]
        except BoardError:
            self._safe_rollback()
            raise
        except Exception as exc:
            # Broad: roll back the open transaction on ANY error (incl. a ValueError
            # from a corrupt row via _row_to_item), never leaking the write lock.
            self._safe_rollback()
            raise BoardError(f"apply_update failed: {exc}") from exc

    def transition(
        self,
        item_id: str,
        to_state: str | None,
        *,
        actor: str,
        note: str = "",
        evidence: list[Evidence] | Evidence | None = None,
        human_required: bool | None = None,
        run_id: str | None = None,
    ) -> WorkItem:
        """Change an item's state, with the ``done`` gate enforced.

        A transition **to** ``done`` requires the item to already carry, or to be
        given here, at least one passing :class:`Evidence` row OR a human sign-off
        (``Evidence{kind:"human"}``); otherwise it raises :class:`BoardError` — a
        bare ``board update ID --state done`` with nothing to back it is rejected.
        ``evidence`` rows are appended (counting toward the gate); ``human_required``
        is set when not ``None``.

        ``to_state=None`` is an annotate-only update: the target is the item's CURRENT
        state, resolved from THIS in-transaction read (never a stale pre-transaction
        read), so a concurrent claim can't be clobbered by a stale annotate target.
        The done gate only fires for an actual move to ``done``.

        ``run_id`` lease-fences the write (the crash-block path): when given, the
        UPDATE only matches while the item is still ``running`` under that lease, so a
        stale crashed run can't clobber a re-dispatched owner or revert a verified
        state — a 0-row match drops the write, exactly like ``apply_update``."""
        if to_state is not None and to_state not in _STATES:
            raise BoardError(f"unknown state: {to_state!r}")
        new_rows = (
            [] if evidence is None
            else [evidence] if isinstance(evidence, Evidence)
            else list(evidence)
        )
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT * FROM work_items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise BoardError(f"no such item: {item_id}")
            item = _row_to_item(row)
            target = item.state if to_state is None else WorkState(to_state)
            # `running` is owned by the claim lease, never set by hand: a manual move
            # INTO running from another state would mint a lease-less, undispatchable
            # item (next_runnable/claim both gate on `planned`). Only a same-state
            # annotate of an ALREADY-running item reaches the lease-preserving branch.
            if target == WorkState.RUNNING and item.state != WorkState.RUNNING:
                raise BoardError(
                    f"cannot move item {item_id} to 'running' by hand — it is claimed "
                    f"by `board run`"
                )
            merged_evidence = item.evidence + new_rows
            if target == WorkState.DONE and not _has_verification(merged_evidence):
                raise BoardError(
                    f"cannot mark item {item_id} done without verification — attach "
                    f"passing evidence (--evidence) or a human sign-off (--sign-off)"
                )
            now = self._now()
            hr = item.human_required if human_required is None else human_required
            # Leaving blocked drops the stale reason (mirrors apply_update); a move TO
            # blocked keeps whatever reason the row already carried.
            blocked_reason = item.blocked_reason if target == WorkState.BLOCKED else ""
            where, where_params = "id=?", [item_id]
            if run_id is not None:
                # Lease fence: only the run that still owns the running claim writes.
                where += " AND state=? AND lease_run_id=?"
                where_params += [WorkState.RUNNING.value, run_id]
            if target == WorkState.RUNNING:
                # Same-state annotate of a RUNNING item must PRESERVE the lease —
                # nulling it would let recover_stale_running treat the live item as
                # abandoned and re-dispatch it (duplicate work).
                cur = self._conn.execute(
                    "UPDATE work_items SET state=?, evidence=?, human_required=?, "
                    f"blocked_reason=?, updated_at=? WHERE {where}",
                    [target.value, _dump_evidence(merged_evidence), int(hr),
                     blocked_reason, now, *where_params],
                )
            else:
                # Leaving running → the claim is consumed; clear the lease.
                cur = self._conn.execute(
                    "UPDATE work_items SET state=?, evidence=?, human_required=?, "
                    "blocked_reason=?, lease_run_id=NULL, claimed_at=NULL, "
                    f"updated_at=? WHERE {where}",
                    [target.value, _dump_evidence(merged_evidence), int(hr),
                     blocked_reason, now, *where_params],
                )
            if run_id is not None and cur.rowcount == 0:
                # Lost lease: the item was reclaimed/re-dispatched (or already left
                # running) while we ran — drop this stale write, recording nothing.
                self._conn.execute("COMMIT")
                logger.warning(
                    "board: dropping stale transition for %s — run %s no longer owns "
                    "the lease (reclaimed/re-dispatched)", item_id, run_id,
                )
                return self.get(item_id)  # type: ignore[return-value]
            self._insert_transition(
                item_id, item.state.value, target.value, actor,
                note or ("annotated" if target == item.state else ""), now,
            )
            self._conn.execute("COMMIT")
            return self.get(item_id)  # type: ignore[return-value]
        except BoardError:
            self._safe_rollback()
            raise
        except Exception as exc:
            # Broad: roll back on ANY error (incl. a ValueError from a corrupt row)
            # so the open transaction / write lock is never leaked.
            self._safe_rollback()
            raise BoardError(f"transition failed: {exc}") from exc

    def recover_stale_running(self, lease_timeout: int | None = None) -> int:
        """Reclaim items stuck in ``running`` past the lease back to ``planned``.

        A ``kill -9`` between :meth:`claim` and :meth:`apply_update` would strand an
        item in ``running`` forever (``next_runnable`` skips ``running`` items). This
        is the analogue of ``cleanup_orphaned_worktrees`` /
        ``session._sweep_stale_session_dirs``: an item whose ``claimed_at`` is older
        than the lease (or missing) is moved back to ``planned`` with an audit row,
        so it can be redispatched. Returns the count reclaimed."""
        timeout = self._lease_timeout if lease_timeout is None else max(1, lease_timeout)
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            now = self._now()
            cutoff = now - timeout
            rows = self._conn.execute(
                "SELECT id, claimed_at FROM work_items WHERE state=?",
                (WorkState.RUNNING.value,),
            ).fetchall()
            reclaimed = 0
            for r in rows:
                ca = r["claimed_at"]
                if ca is not None and ca > cutoff:
                    continue  # still within its lease
                self._conn.execute(
                    "UPDATE work_items SET state=?, lease_run_id=NULL, claimed_at=NULL, "
                    "updated_at=? WHERE id=? AND state=?",
                    (WorkState.PLANNED.value, now, r["id"], WorkState.RUNNING.value),
                )
                self._insert_transition(
                    r["id"], WorkState.RUNNING.value, WorkState.PLANNED.value,
                    "system", "stale-claim recovery", now,
                )
                reclaimed += 1
            self._conn.execute("COMMIT")
            return reclaimed
        except Exception as exc:
            self._safe_rollback()
            raise BoardError(f"recover_stale_running failed: {exc}") from exc

    # ── reads ────────────────────────────────────────────────────────────────

    def get(self, item_id: str) -> WorkItem | None:
        """The item, or ``None`` if no such id."""
        try:
            row = self._conn.execute(
                "SELECT * FROM work_items WHERE id=?", (item_id,)
            ).fetchone()
            # _row_to_item is inside the try (and ValueError is caught) so a
            # corrupt/out-of-enum `state` from a hand-edited db surfaces as a
            # contracted BoardError, not a raw ValueError traceback.
            return _row_to_item(row) if row is not None else None
        except (sqlite3.Error, ValueError) as exc:
            raise BoardError(f"get failed: {exc}") from exc

    def list(self, state: str | None = None, goal: str | None = None) -> list[WorkItem]:
        """All items, optionally filtered by ``state`` and/or ``goal`` (FIFO order)."""
        clauses, params = [], []
        if state is not None:
            if state not in _STATES:
                raise BoardError(f"unknown state: {state!r}")
            clauses.append("state=?")
            params.append(state)
        if goal is not None:
            clauses.append("goal=?")
            params.append(goal)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        try:
            rows = self._conn.execute(
                f"SELECT * FROM work_items{where} ORDER BY created_at, rowid", params
            ).fetchall()
            return [_row_to_item(r) for r in rows]  # ValueError (bad state) → BoardError
        except (sqlite3.Error, ValueError) as exc:
            raise BoardError(f"list failed: {exc}") from exc

    def transitions(self, item_id: str) -> list[dict]:
        """The append-only audit trail for an item (oldest first)."""
        try:
            rows = self._conn.execute(
                "SELECT from_state, to_state, actor, note, at FROM transitions "
                "WHERE item_id=? ORDER BY id",
                (item_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise BoardError(f"transitions failed: {exc}") from exc
        return [dict(r) for r in rows]

    def close(self) -> None:
        """Close the connection (best-effort; idempotent)."""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ── internals ────────────────────────────────────────────────────────────

    def _claim_where(self, where: str, where_params: tuple, run_id: str) -> WorkItem | None:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                f"SELECT * FROM work_items WHERE {where}", where_params
            ).fetchone()
            if row is None:
                self._conn.execute("COMMIT")
                return None
            item = self._stamp_running(row, run_id)
            self._conn.execute("COMMIT")
            return item
        except Exception as exc:
            self._safe_rollback()
            raise BoardError(f"claim failed: {exc}") from exc

    def _stamp_running(self, row: sqlite3.Row, run_id: str) -> WorkItem:
        """Inside an open transaction: move ``row`` (a planned item) → running,
        stamping the lease. Returns the freshly-read item."""
        now = self._now()
        self._conn.execute(
            "UPDATE work_items SET state=?, lease_run_id=?, claimed_at=?, updated_at=? "
            "WHERE id=? AND state=?",
            (WorkState.RUNNING.value, run_id, now, now, row["id"], WorkState.PLANNED.value),
        )
        self._insert_transition(
            row["id"], WorkState.PLANNED.value, WorkState.RUNNING.value,
            run_id, "claimed", now,
        )
        return _row_to_item(
            self._conn.execute("SELECT * FROM work_items WHERE id=?", (row["id"],)).fetchone()
        )

    def _deps_satisfied(self, deps: list[str]) -> bool:
        """True iff every dependency exists and is ``done`` (a missing dependency is
        conservatively treated as unsatisfied — don't dispatch work whose declared
        prerequisite isn't there). Called inside the ``next_runnable`` transaction."""
        for dep_id in deps:
            r = self._conn.execute(
                "SELECT state FROM work_items WHERE id=?", (dep_id,)
            ).fetchone()
            if r is None or r["state"] != WorkState.DONE.value:
                return False
        return True

    def _insert_transition(
        self, item_id: str, from_state: str | None, to_state: str, actor: str,
        note: str, at: float,
    ) -> None:
        self._conn.execute(
            "INSERT INTO transitions (item_id, from_state, to_state, actor, note, at) "
            "VALUES (?,?,?,?,?,?)",
            (item_id, from_state, to_state, actor, note, at),
        )

    def _safe_rollback(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass  # no active transaction (e.g. BEGIN itself failed) — nothing to undo


# ── row <-> dataclass mapping ────────────────────────────────────────────────


def _item_to_row(item: WorkItem) -> tuple:
    return (
        item.id, item.goal, item.task, item.state.value, item.owner_agent,
        json.dumps(item.inputs), json.dumps(item.outputs), _dump_evidence(item.evidence),
        item.handoff, int(item.human_required), item.blocked_reason,
        json.dumps(item.depends_on), item.lease_run_id, item.claimed_at,
        item.created_at, item.updated_at,
    )


def _row_to_item(row: sqlite3.Row) -> WorkItem:
    return WorkItem(
        id=row["id"],
        goal=row["goal"],
        task=row["task"],
        state=WorkState(row["state"]),
        owner_agent=row["owner_agent"],
        inputs=_load_list(row["inputs"]),
        outputs=_load_list(row["outputs"]),
        evidence=_load_evidence(row["evidence"]),
        handoff=row["handoff"],
        human_required=bool(row["human_required"]),
        blocked_reason=row["blocked_reason"],
        depends_on=_load_list(row["depends_on"]),
        lease_run_id=row["lease_run_id"],
        claimed_at=row["claimed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _load_list(raw: str) -> list[str]:
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in v] if isinstance(v, list) else []


def _dump_evidence(evidence: list[Evidence]) -> str:
    return json.dumps([asdict(e) for e in evidence])


def _load_evidence(raw: str) -> list[Evidence]:
    try:
        rows = json.loads(raw)
    except (ValueError, TypeError):
        return []
    out: list[Evidence] = []
    if not isinstance(rows, list):
        return out
    for d in rows:
        if not isinstance(d, dict):
            continue
        try:
            out.append(Evidence(
                kind=str(d["kind"]), ref=str(d["ref"]), passed=bool(d["passed"]),
                at=float(d["at"]), by=str(d.get("by", "")),
            ))
        except (KeyError, TypeError, ValueError):
            continue  # skip a malformed evidence row, keep the rest
    return out


def _merge_unique(existing: list[str], new: list) -> list[str]:
    """``existing`` then any ``new`` not already present — order-preserving dedup."""
    out = list(existing)
    seen = set(out)
    for x in new:
        s = str(x)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _has_verification(evidence: list[Evidence]) -> bool:
    """The ``done`` gate predicate: at least one PASSING evidence row.

    A human sign-off is just an ``Evidence{kind:"human", passed=True}``, so it's
    covered by ``passed`` — and a RECORDED human REJECTION (``kind=="human",
    passed=False``) deliberately does NOT satisfy the gate (the ``passed`` field is
    the verdict; the gate must honor it, not any human row regardless of outcome)."""
    return any(e.passed for e in evidence)


def _resolve_completed_state(
    requested: object, evidence: list[Evidence]
) -> tuple[WorkState, str]:
    """Map a COMPLETED run + the agent's advisory ``state`` request → (state, note).

    A completed run defaults to ``review`` ("finished, awaiting verification"). The
    agent's request is advisory: ``done`` is honored only when the work is
    independently verified (passing evidence / a human sign-off — i.e. a passing
    quality gate), else it CLAMPS to ``review``; ``blocked`` is honored; anything
    else (``None``/unknown/``review``/``running``/``planned``) → ``review``."""
    req = requested if isinstance(requested, str) else None
    if req == WorkState.DONE.value:
        if _has_verification(evidence):
            return WorkState.DONE, "agent completed; verified"
        return WorkState.REVIEW, "agent requested done without verification — clamped to review"
    if req == WorkState.BLOCKED.value:
        return WorkState.BLOCKED, "agent reported blocked"
    return WorkState.REVIEW, "agent completed; awaiting review"


# ── prompt rendering + agent-output parsing (pure) ───────────────────────────

#: The fenced-block language tag agents use to (optionally) report a board update.
BOARD_FENCE = "syncmill-board"

_UPDATE_INSTRUCTIONS = (
    "When you finish, you MAY append ONE fenced block reporting the board update "
    "(every field optional):\n"
    "```" + BOARD_FENCE + "\n"
    '{"state": "review", "handoff": "<note for whoever picks this up next>", '
    '"outputs": ["<artifact or file you produced>"], "human_required": false}\n'
    "```\n"
    'Use "state": "blocked" with a "blocked_reason" if you cannot proceed, or '
    '"human_required": true if a human must step in. Requesting "done" only takes '
    "effect if the work is independently verified (a passing quality gate or a "
    'human sign-off); otherwise it is recorded as "review".'
)


def render_item_prompt(item: WorkItem, active_decisions: list[str] | None = None) -> str:
    """Build the deterministic prompt for a board item.

    Reuses the ``_enrich_prompt`` shape (sectioned context, the task labeled
    "Task:") plus the update-block convention. Decisions + the handoff baton are
    injected DETERMINISTICALLY here (guaranteed in the prompt), never via ``shared``
    top-k recall. ``active_decisions`` is the Phase-2 seam (v1 passes ``None``)."""
    parts: list[str] = []
    header = "You are working on a tracked work-board item."
    if item.goal:
        header += f"\nGoal: {item.goal}"
    parts.append(header)
    if active_decisions:
        parts.append(
            "Team decisions you MUST respect (do not re-litigate):\n"
            + "\n".join(f"- {d}" for d in active_decisions)
        )
    if item.inputs:
        parts.append("Inputs / prior artifacts:\n" + "\n".join(f"- {x}" for x in item.inputs))
    if item.handoff:
        parts.append(f"Handoff note from prior work on this item:\n{item.handoff}")
    parts.append(f"Task: {item.task}")
    parts.append(_UPDATE_INSTRUCTIONS)
    return "\n\n".join(parts)


def parse_board_update(raw: str) -> dict:
    """Extract the optional ``syncmill-board`` fenced JSON object from ``raw``.

    Its OWN parser (not a reuse of ``_extract_json_list``) applying the same
    robustness philosophy as ``DecomposeStrategy._parse_subtasks``: strict,
    degrade-to-``{}``, NEVER raises. No block, no closing fence, non-JSON, or a
    non-object → ``{}``. A recognised object is sanitized field-by-field
    (:func:`_sanitize_update`) — an unknown or malformed field is dropped, not
    fatal, so one bad field can't discard a valid ``state``."""
    if not raw:
        return {}
    block = _extract_board_block(raw)
    if block is None:
        return {}
    try:
        parsed = json.loads(block)
    except (ValueError, TypeError, RecursionError):
        # RecursionError (deeply-nested untrusted JSON) is a RuntimeError, not a
        # ValueError — catch it too so the "NEVER raises" contract holds and a
        # pathological board block degrades to {} like any other malformed input.
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _sanitize_update(parsed)


def _extract_board_block(raw: str) -> str | None:
    """The text between a ``` + BOARD_FENCE opening fence and the next ``` fence, or
    ``None`` if there is no such complete block. Deliberately requires the exact
    ``BOARD_FENCE`` tag (not a bare ```` ``` ````), so an unrelated code block in
    the agent's prose is never mistaken for a board update."""
    lines = raw.splitlines()
    start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("```") and s[3:].strip() == BOARD_FENCE:
            start = i + 1
            break
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start:]:
        s = line.strip()
        # A genuine close fence — NOT a second board OPEN fence. This mirrors
        # strip_board_block's guard, so the parser and the stripper agree on block
        # bounds; without it a 2nd ```syncmill-board would be mis-read as block 1's
        # close (and the two paths would disagree on what was a board update).
        if s.startswith("```") and s[3:].strip() != BOARD_FENCE:
            return "\n".join(body)
        body.append(line)
    return None  # no closing fence → malformed → degrade


def _sanitize_update(parsed: dict) -> dict:
    """Keep only the recognised update fields with correct types; drop the rest.

    ``state``/``handoff``/``blocked_reason``: strings. ``human_required``: bool.
    ``outputs``: a list of non-empty strings, ALL-OR-NOTHING (a mixed/partial list
    is dropped, never salvaged to a subset — the ``_parse_subtasks`` lesson). The
    requested ``state`` is NOT validated here (``apply_update`` treats an unknown
    request as "no request" → review), keeping this a pure shape filter."""
    out: dict = {}
    for key in ("state", "handoff", "blocked_reason"):
        v = parsed.get(key)
        if isinstance(v, str):
            out[key] = v
    if isinstance(parsed.get("human_required"), bool):
        out["human_required"] = parsed["human_required"]
    outputs = parsed.get("outputs")
    if isinstance(outputs, list) and outputs and all(
        isinstance(s, str) and s.strip() for s in outputs
    ):
        out["outputs"] = [s.strip() for s in outputs]
    return out


def strip_board_block(output: str) -> str:
    """Return ``output`` with the ``syncmill-board`` fenced block removed.

    Used to build the sanitized ``shared`` summary — the board-internal update
    instructions/state must not leak into global memtomem knowledge. Mirrors
    :func:`_extract_board_block`'s framing (the same exact-tag open fence + the next
    ``` close); a block with no closing fence drops to end-of-output (consistent
    with parse degrading it)."""
    if not output:
        return output
    lines = output.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if s.startswith("```") and s[3:].strip() == BOARD_FENCE:
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                i += 1
            # Consume the stop line as this block's CLOSING fence only if it isn't
            # itself a NEW board open fence — otherwise leave it for the outer loop
            # to strip as its own block. Without this, two adjacent board blocks
            # would mis-consume the second's OPEN fence as the first's close and
            # leak the second block's JSON into the sanitized share.
            if i < n:
                cs = lines[i].strip()
                if not (cs.startswith("```") and cs[3:].strip() == BOARD_FENCE):
                    i += 1  # ordinary closing fence — drop it too
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out).strip()


# ── human-facing rendering (CLI) ─────────────────────────────────────────────


def _short(item_id: str) -> str:
    return item_id[:8]


def render_board(items: list[WorkItem]) -> str:
    """One line per item for ``board list`` — id, state, goal, an abbreviated task."""
    if not items:
        return "[syncmill] board is empty"
    lines = []
    for it in items:
        flags = []
        if it.human_required:
            flags.append("human-required")
        if it.depends_on:
            flags.append(f"deps={len(it.depends_on)}")
        flag = f"  ({', '.join(flags)})" if flags else ""
        goal = f"[{it.goal}] " if it.goal else ""
        task = it.task if len(it.task) <= 60 else it.task[:57] + "..."
        lines.append(f"  {it.id}  {it.state.value:<8}  {goal}{task}{flag}")
    return "\n".join(lines)


def render_item(item: WorkItem, transitions: list[dict] | None = None) -> str:
    """A detailed multi-line view for ``board show`` / ``board add`` confirmation."""
    lines = [
        f"id:       {item.id}",
        f"state:    {item.state.value}",
        f"goal:     {item.goal or '-'}",
        f"task:     {item.task}",
        f"owner:    {item.owner_agent or '-'}",
    ]
    if item.depends_on:
        lines.append(f"depends:  {', '.join(item.depends_on)}")
    if item.inputs:
        lines.append(f"inputs:   {', '.join(item.inputs)}")
    if item.outputs:
        lines.append(f"outputs:  {', '.join(item.outputs)}")
    if item.handoff:
        lines.append(f"handoff:  {item.handoff}")
    if item.human_required:
        lines.append("human:    REQUIRED")
    if item.blocked_reason:
        lines.append(f"blocked:  {item.blocked_reason}")
    if item.evidence:
        lines.append("evidence:")
        for e in item.evidence:
            mark = "pass" if e.passed else "fail"
            lines.append(f"  - [{e.kind}/{mark}] {e.ref}" + (f" (by {e.by})" if e.by else ""))
    if transitions:
        lines.append("history:")
        for t in transitions:
            frm = t.get("from_state") or "·"
            note = f" — {t['note']}" if t.get("note") else ""
            lines.append(f"  {frm} → {t['to_state']}  [{t.get('actor') or '?'}]{note}")
    return "\n".join(lines)


def build_board(config: BoardConfig) -> Board | None:
    """The board described by ``config``, or ``None`` when disabled
    (``enabled=False``, the default) — so the disabled path never opens a file and
    plain ``syncmill run`` is unchanged. Mirrors ``build_breaker``/``build_gate``,
    except the board is LOAD-BEARING (a corrupt db raises :class:`BoardError`, it
    does not degrade)."""
    if not config.enabled:
        return None
    return Board(config.db_path, lease_timeout_seconds=config.lease_timeout_seconds)
