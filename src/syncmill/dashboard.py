"""Live TUI dashboard over the persistent work-board (Phase 3, `syncmill dashboard`).

A **separate observer/editor process** — never on the orchestration thread (the
same foreground contention the tmux monitor's outer/inner split exists to solve:
orchestration is sync-on-main-thread and a Textual app owns the foreground loop).
It is not an agent and not a strategy: it touches the board and ONLY the board —
not memory, not agents — so the "Supervisor is the only component that touches
both agents and memory" invariant is untouched. Every write is an in-process
``board.py`` call (the same human path as ``board update``); the read-only MCP
gateway is never involved.

Data source is **polling board.db** (WAL — readers never block against a live
``board run`` writer; ``Board``'s docstring anticipates exactly this reader).
Reads stay inline on Textual's event-loop thread: the sqlite connection is opened
with the default ``check_same_thread=True``, so moving Board calls into a Textual
thread worker would raise — a future async refactor needs a dedicated reader
connection, not a thread hop (the gateway precedent: quick sync reads, no offload).

Poll reads are best-effort: a transient :class:`BoardError` mid-run shows a
DEGRADED notice and keeps the last good snapshot on screen; only the *launch*
hard-fails on an unopenable board (the ``session`` no-fallback precedent).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    Label,
    Select,
    Static,
)
from textual.widgets.data_table import RowDoesNotExist

from syncmill.board import (
    Board,
    BoardError,
    BoardStats,
    Evidence,
    WorkItem,
    WorkState,
    _short,
    render_board_stats,
    render_item,
)

# ── constants + pure data layer ──────────────────────────────────────────────

DEFAULT_REFRESH_SECONDS = 2.0
MIN_REFRESH_SECONDS = 0.5

#: Human-path target states. Recomputed here rather than imported from cli.py
#: (cli imports this module — the import graph stays acyclic): `running` is owned
#: by the claim lease, never set by hand, same as `_BOARD_UPDATE_STATES`.
EDIT_STATES = [s.value for s in WorkState if s is not WorkState.RUNNING]

#: `f` cycles the list filter through: all → each state → all.
FILTER_CYCLE: list[str | None] = [None, *(s.value for s in WorkState)]

_TASK_TRUNC = 40


def clamp_refresh(value: float) -> float:
    """Floor the poll interval at :data:`MIN_REFRESH_SECONDS` so a typo'd
    ``--refresh 0.01`` can't busy-spin sqlite."""
    return max(MIN_REFRESH_SECONDS, value)


@dataclass(frozen=True)
class BoardSnapshot:
    """Everything one poll tick reads — the seam between Board I/O and Textual
    rendering, testable against a real ``Board`` with no app running."""

    items: list[WorkItem]
    stats: BoardStats
    decisions_count: int
    detail: WorkItem | None = None
    detail_transitions: list[dict] = field(default_factory=list)
    at: float = 0.0


def take_snapshot(
    board: Board,
    *,
    state: str | None = None,
    selected_id: str | None = None,
    now: Callable[[], float] = time.time,
) -> BoardSnapshot:
    """One consistent poll tick. Raises :class:`BoardError` — the degradation
    policy (keep last good data, show a notice) lives in the app, in one place."""
    items = board.list(state=state)
    stats = board.stats()
    decisions_count = len(board.active_decisions())
    detail: WorkItem | None = None
    detail_transitions: list[dict] = []
    if selected_id is not None:
        detail = board.get(selected_id)
        if detail is not None:
            detail_transitions = board.transitions(selected_id)
    return BoardSnapshot(
        items=items,
        stats=stats,
        decisions_count=decisions_count,
        detail=detail,
        detail_transitions=detail_transitions,
        at=now(),
    )


def _age(now: float, ts: float) -> str:
    """Compact age for the table's age column: ``42s`` / ``5m`` / ``3h`` / ``2d``."""
    delta = max(0, int(now - ts))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def _trunc_task(task: str) -> str:
    return task if len(task) <= _TASK_TRUNC else task[: _TASK_TRUNC - 3] + "..."


def _item_flags(item: WorkItem) -> str:
    flags = []
    if item.human_required:
        flags.append("H!")
    if item.depends_on:
        flags.append(f"deps={len(item.depends_on)}")
    return " ".join(flags)


def _status_line(
    snapshot: BoardSnapshot | None, state_filter: str | None, degraded: str | None
) -> str:
    """The one-line footer summary. When degraded, the last good snapshot stays on
    screen and the line says so."""
    if snapshot is None:
        parts = ["no data yet"]
    else:
        by = snapshot.stats.by_state
        counts = " ".join(f"{s}={by[s]}" for s in sorted(by))
        items = f"items:{snapshot.stats.total}" + (f"  {counts}" if counts else "")
        refreshed = time.strftime("%H:%M:%S", time.localtime(snapshot.at))
        parts = [items, f"decisions:{snapshot.decisions_count}", f"refreshed {refreshed}"]
    parts.insert(1, f"filter:{state_filter or 'all'}")
    line = " | ".join(parts)
    if degraded:
        line += f" | DEGRADED: {degraded} (showing last good data)"
    return line


# ── Textual widgets / screens ────────────────────────────────────────────────


class EditModal(ModalScreen[dict | None]):
    """The ``board update`` surface as a modal — state / note / evidence ref /
    sign-off / clear human-required. Deliberately dumb: it never touches the
    Board, it only ``dismiss``es the collected fields (or ``None`` on cancel)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, item: WorkItem):
        super().__init__()
        self._item = item

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(f"update {_short(self._item.id)} — {_trunc_task(self._item.task)}")
            yield Select(
                (("(keep state)", ""), *((s, s) for s in EDIT_STATES)),
                value="",
                allow_blank=False,
                id="edit-state",
            )
            yield Input(placeholder="note (audit)", id="edit-note")
            yield Input(placeholder="evidence ref (e.g. CI url)", id="edit-evidence")
            yield Checkbox("sign-off (verification for done)", id="edit-sign-off")
            yield Checkbox("clear human-required", id="edit-clear-hr")
            with Horizontal(id="edit-buttons"):
                yield Button("Apply", variant="primary", id="edit-apply")
                yield Button("Cancel", id="edit-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "edit-apply":
            self.dismiss(None)
            return
        state = self.query_one("#edit-state", Select).value
        self.dismiss(
            {
                "state": state if isinstance(state, str) and state else None,
                "note": self.query_one("#edit-note", Input).value,
                "evidence_ref": self.query_one("#edit-evidence", Input).value or None,
                "sign_off": self.query_one("#edit-sign-off", Checkbox).value,
                "clear_human_required": self.query_one("#edit-clear-hr", Checkbox).value,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class StatsScreen(ModalScreen[None]):
    """The stats overlay — :func:`render_board_stats` verbatim, so it reads
    byte-identically to ``board stats``."""

    BINDINGS = [("escape", "cancel", "Close"), ("s", "cancel", "Close")]

    def __init__(self, text: str):
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        yield Static(self._text, id="stats-body")

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── the app ──────────────────────────────────────────────────────────────────


class DashboardApp(App[None]):
    """Board 관제: a polling item table + detail pane + stats, with the human-path
    edits (transition / unblock / sign-off / annotate) bound to keys.

    The ``board`` is injected and owned by the caller (the CLI opens it and closes
    it in a ``finally`` — the ``serve``/memory ownership precedent); ``now`` is
    injectable (the ``Board(now=)`` precedent) so ages and Evidence timestamps are
    deterministic under test. The interval timer's callback IS ``action_refresh``,
    the same code path as the ``r`` key and the post-edit refresh — tests construct
    the app with a huge ``refresh_seconds`` and drive every tick explicitly.
    """

    TITLE = "syncmill board"

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("f", "cycle_filter", "Filter state"),
        ("e", "edit", "Update item"),
        ("u", "unblock", "Unblock"),
        ("h", "clear_human", "Clear human-req"),
        ("s", "stats", "Stats"),
        ("q", "quit", "Quit"),
    ]

    CSS = """
    #main { height: 1fr; }
    #items { width: 3fr; }
    #detail-pane { width: 2fr; border-left: solid $primary; padding: 0 1; }
    #status { height: 1; padding: 0 1; background: $panel; }
    EditModal { align: center middle; }
    #edit-dialog {
        width: 64; height: auto; border: thick $primary;
        padding: 1 2; background: $surface;
    }
    #edit-buttons { height: auto; align-horizontal: right; }
    #edit-buttons Button { margin-left: 2; }
    StatsScreen { align: center middle; }
    #stats-body {
        width: 72; height: auto; border: thick $primary;
        padding: 1 2; background: $surface;
    }
    """

    def __init__(
        self,
        board: Board,
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        now: Callable[[], float] = time.time,
    ):
        super().__init__()
        self._board = board
        self._refresh_seconds = refresh_seconds
        self._now = now
        self._filter: str | None = None
        self._selected_id: str | None = None
        self._snapshot: BoardSnapshot | None = None
        self._degraded: str | None = None
        #: The last board-write rejection (e.g. the done gate) — the test-assertable
        #: seam; `notify` toasts are UX-only (their internals shift across Textual
        #: versions).
        self.last_action_error: str | None = None
        #: The current status-line / detail-pane text, mirrored off the widgets so
        #: tests assert on app-owned strings (widget content APIs are version-shifty).
        self.status_text: str = ""
        self.detail_text: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield DataTable(id="items")
            with VerticalScroll(id="detail-pane"):
                yield Static("(no item selected)", id="detail")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#items", DataTable)
        table.cursor_type = "row"
        table.add_columns("id", "state", "goal", "task", "owner", "age", "flags")
        self.action_refresh()
        self.set_interval(self._refresh_seconds, self.action_refresh)

    # ── polling (best-effort reads) ──────────────────────────────────────────

    def action_refresh(self) -> None:
        try:
            snap = take_snapshot(
                self._board,
                state=self._filter,
                selected_id=self._selected_id,
                now=self._now,
            )
        except BoardError as exc:
            # Best-effort: keep the last good snapshot on screen, say so, live on.
            self._degraded = str(exc)
            self._update_status()
            return
        self._degraded = None
        self._snapshot = snap
        self._render_snapshot(snap)

    def _render_snapshot(self, snap: BoardSnapshot) -> None:
        table = self.query_one("#items", DataTable)
        table.clear()
        for it in snap.items:
            table.add_row(
                _short(it.id),
                it.state.value,
                it.goal or "-",
                _trunc_task(it.task),
                it.owner_agent or "-",
                _age(snap.at, it.updated_at),
                _item_flags(it),
                key=it.id,  # full id — the 8-char display could collide as a key
            )
        if self._selected_id is not None:
            try:
                table.move_cursor(row=table.get_row_index(self._selected_id))
            except RowDoesNotExist:
                self._selected_id = None  # filtered out / gone — drop the selection
        if snap.detail is not None and snap.detail.id == self._selected_id:
            self._update_detail(render_item(snap.detail, snap.detail_transitions))
        elif self._selected_id is None:
            self._update_detail("(no item selected)")
        self._update_status()

    def _update_status(self) -> None:
        self.status_text = _status_line(self._snapshot, self._filter, self._degraded)
        self.query_one("#status", Static).update(self.status_text)

    def _update_detail(self, text: str) -> None:
        self.detail_text = text
        self.query_one("#detail", Static).update(text)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = event.row_key.value if event.row_key is not None else None
        if key == self._selected_id:
            return
        self._selected_id = key
        # Detail-only refetch: re-rendering the whole table from inside a highlight
        # handler would emit further highlight events mid-render (cursor churn).
        self._refresh_detail()

    def _refresh_detail(self) -> None:
        if self._selected_id is None:
            self._update_detail("(no item selected)")
            return
        try:
            item = self._board.get(self._selected_id)
            transitions = self._board.transitions(self._selected_id) if item else []
        except BoardError as exc:
            self._degraded = str(exc)
            self._update_status()
            return
        self._update_detail(render_item(item, transitions) if item else "(item not found)")

    # ── actions (reads) ──────────────────────────────────────────────────────

    def action_cycle_filter(self) -> None:
        idx = FILTER_CYCLE.index(self._filter)
        self._filter = FILTER_CYCLE[(idx + 1) % len(FILTER_CYCLE)]
        self.action_refresh()

    def action_stats(self) -> None:
        if self._snapshot is None:
            self.notify("no stats yet", severity="warning")
            return
        self.push_screen(StatsScreen(render_board_stats(self._snapshot.stats)))

    # ── actions (the human-path writes) ──────────────────────────────────────

    def action_edit(self) -> None:
        item = self._selected_item()
        if item is None:
            self.notify("no item selected", severity="warning")
            return

        def _on_result(result: dict | None) -> None:
            if result:
                self._apply_update(item.id, **result)

        self.push_screen(EditModal(item), _on_result)

    def action_unblock(self) -> None:
        if self._selected_id is None:
            self.notify("no item selected", severity="warning")
            return
        self._apply_update(self._selected_id, state=WorkState.PLANNED.value, note="unblocked")

    def action_clear_human(self) -> None:
        if self._selected_id is None:
            self.notify("no item selected", severity="warning")
            return
        self._apply_update(self._selected_id, clear_human_required=True)

    def _selected_item(self) -> WorkItem | None:
        if self._snapshot is None or self._selected_id is None:
            return None
        for it in self._snapshot.items:
            if it.id == self._selected_id:
                return it
        detail = self._snapshot.detail
        return detail if detail is not None and detail.id == self._selected_id else None

    def _apply_update(
        self,
        item_id: str,
        *,
        state: str | None = None,
        note: str = "",
        evidence_ref: str | None = None,
        sign_off: bool = False,
        clear_human_required: bool = False,
    ) -> bool:
        """All board writes funnel through here — EXACTLY the ``board update`` CLI
        recipe (evidence/sign-off become Evidence rows, ``actor="human"``), so the
        audit trail reads identically whichever surface the human used. A
        :class:`BoardError` (incl. the done-gate rejection) becomes an error toast
        + ``last_action_error`` — the app never dies on a rejected edit."""
        if state is None and not evidence_ref and not sign_off and not clear_human_required:
            self.notify(
                "nothing to update — set a state, evidence/sign-off, or clear human-required",
                severity="warning",
            )
            return False
        now = self._now()
        rows: list[Evidence] = []
        if evidence_ref:
            rows.append(Evidence(kind="manual", ref=evidence_ref, passed=True, at=now, by="human"))
        if sign_off:
            rows.append(Evidence(kind="human", ref="sign-off", passed=True, at=now, by="human"))
        hr = False if clear_human_required else None
        try:
            self._board.transition(
                item_id, state, actor="human", note=note,
                evidence=rows or None, human_required=hr,
            )
        except BoardError as exc:
            self.last_action_error = str(exc)
            self.notify(str(exc), severity="error")
            return False
        self.last_action_error = None
        self.notify(f"updated {_short(item_id)}")
        self.action_refresh()
        return True


# ── CLI entry seam ───────────────────────────────────────────────────────────


def run_dashboard(board: Board, *, refresh_seconds: float = DEFAULT_REFRESH_SECONDS) -> None:
    """Run the dashboard over an already-open ``board`` (the caller closes it).
    The one thing cli.py imports — and the seam tests patch (at the lookup site,
    ``syncmill.cli.run_dashboard``)."""
    DashboardApp(board, refresh_seconds=clamp_refresh(refresh_seconds)).run()
