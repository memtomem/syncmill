"""Live TUI dashboard over the persistent work-board (Phase 3, `syncmill dashboard`).

A **separate observer/editor process** — never on the orchestration thread (the
same foreground contention the tmux monitor's outer/inner split exists to solve:
orchestration is sync-on-main-thread and a Textual app owns the foreground loop).
It is not an agent and not a strategy: it touches the board, reads run-envelope
reports, and — for the TEAM pane (P3) — writes the project ``syncmill.toml`` via
the ``teamfile.py`` helpers. It still never touches memory or agents, so the
"Supervisor is the only component that touches both agents and memory" invariant
is untouched. Every board write is an in-process ``board.py`` call (the same human
path as ``board update``), every team write is the same ``teamfile`` funnel ``team
save``/``team use`` call, and reports are read through the same ``report list`` /
``report show`` functions as the CLI; the read-only MCP gateway is never involved.

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
from pathlib import Path
from typing import Callable

from pydantic import ValidationError
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.notifications import SeverityLevel
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Input,
    OptionList,
    Select,
    Static,
)
from textual.widgets.data_table import RowDoesNotExist

from syncmill.artifacts import printable_name, printable_text
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
from syncmill.config import SyncmillConfig, TeamConfig
from syncmill.report import list_reports, render_report
from syncmill.strategies import available_strategies
from syncmill.teamfile import (
    TeamFileError,
    build_team,
    config_write_lock,
    load_config_at,
    parse_capability_specs,
    render_team,
    save_team,
    set_default_team,
    validate_team_save,
)
from syncmill.tui_render import (
    PlainLabel,
    PlainOption,
    PlainStatic,
    error_console,
    install_render_guard,
    plain,
    plain_select,
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
            yield PlainLabel(f"update {_short(self._item.id)} — {_trunc_task(self._item.task)}")
            yield plain_select(
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
        yield PlainStatic(self._text, id="stats-body")

    def action_cancel(self) -> None:
        self.dismiss(None)


class TeamEditModal(ModalScreen[dict | None]):
    """The ``team save`` surface as a modal — name / agents / strategy / goals /
    capabilities. Deliberately dumb like :class:`EditModal`: it never touches a
    file, it only ``dismiss``es the RAW string fields (or ``None`` on cancel) —
    all parsing/validation happens in the app's ``_apply_team_save`` funnel, so
    a rejected edit lands in ``last_team_error`` (the done-gate precedent)."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, name: str, team: TeamConfig | None):
        super().__init__()
        self._name = name
        self._team = team

    def compose(self) -> ComposeResult:
        team = self._team
        caps = "; ".join(
            f"{a}={','.join(toks)}" for a, toks in (team.capabilities if team else {}).items()
        )
        with Vertical(id="team-edit-dialog"):
            yield PlainLabel("edit team" if team else "new team")
            yield Input(value=self._name, placeholder="team name", id="team-name")
            yield Input(
                value=", ".join(team.agents) if team else "",
                placeholder="agents, priority order (e.g. codex, claude)",
                id="team-agents",
            )
            yield plain_select(
                (("(inherit default_strategy)", ""), *((s, s) for s in available_strategies())),
                value=(team.strategy if team and team.strategy else ""),
                allow_blank=False,
                id="team-strategy",
            )
            yield Input(
                value=", ".join(team.goals) if team else "",
                placeholder="goals (e.g. backend, api)",
                id="team-goals",
            )
            yield Input(
                value=caps,
                placeholder="capabilities (e.g. codex=python,cli; kimi-code=rust)",
                id="team-caps",
            )
            with Horizontal(id="team-edit-buttons"):
                yield Button("Save", variant="primary", id="team-edit-apply")
                yield Button("Cancel", id="team-edit-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "team-edit-apply":
            self.dismiss(None)
            return
        strategy = self.query_one("#team-strategy", Select).value
        self.dismiss(
            {
                "name": self.query_one("#team-name", Input).value,
                "agents": self.query_one("#team-agents", Input).value,
                "strategy": strategy if isinstance(strategy, str) and strategy else None,
                "goals": self.query_one("#team-goals", Input).value,
                "capabilities": self.query_one("#team-caps", Input).value,
            }
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class TeamScreen(ModalScreen[None]):
    """The TEAM pane — configured profiles with the default-team marker, detail
    byte-identical to ``team show`` (the shared :func:`render_team`). ``e`` edits
    the highlighted team (or composes a new one when none exist), ``u`` toggles
    it as the persisted default — both through the app's ``teamfile`` funnels,
    the same recipe as ``team save`` / ``team use``."""

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("t", "cancel", "Close"),
        ("e", "edit_team", "Edit/new team"),
        ("u", "toggle_default", "Toggle default"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="team-dialog"):
            yield PlainLabel("teams — e edit/new, u default, esc close", id="team-header")
            yield OptionList(id="team-list", markup=False)
            with VerticalScroll(id="team-detail-pane"):
                yield PlainStatic("", id="team-detail")

    def on_mount(self) -> None:
        self.refresh_teams()

    # ── rendering ────────────────────────────────────────────────────────────

    def refresh_teams(self) -> None:
        """(Re)populate from the app's CURRENT config snapshot — called on mount
        and by the app after every successful team write (post-reload)."""
        app = self.app
        config: SyncmillConfig = app._config
        option_list = self.query_one("#team-list", OptionList)
        highlighted = self._highlighted_name()
        option_list.clear_options()
        for name in sorted(config.teams):
            marker = " * (default)" if name == (config.default_team or None) else ""
            option_list.add_option(PlainOption(f"{name}{marker}", id=name))
        if not config.teams:
            self._show_detail("(no teams configured — press e to compose one)")
            return
        names = sorted(config.teams)
        target = highlighted if highlighted in config.teams else names[0]
        option_list.highlighted = names.index(target)
        self._show_detail(render_team(target, config.teams[target], config))

    def _highlighted_name(self) -> str | None:
        option_list = self.query_one("#team-list", OptionList)
        idx = option_list.highlighted
        if idx is None:
            return None
        option = option_list.get_option_at_index(idx)
        return option.id

    def _show_detail(self, text: str) -> None:
        # Mirrored onto the APP (team_detail_text) so tests assert app-owned
        # strings, never widget internals — the status_text/detail_text pattern.
        self.app.team_detail_text = text
        self.query_one("#team-detail", Static).update(text)

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        name = event.option.id
        config: SyncmillConfig = self.app._config
        if name in config.teams:
            self._show_detail(render_team(name, config.teams[name], config))

    # ── actions (funnel into the app's teamfile writes) ──────────────────────

    def action_edit_team(self) -> None:
        name = self._highlighted_name()
        config: SyncmillConfig = self.app._config
        team = config.teams.get(name) if name else None

        def _on_result(fields: dict | None) -> None:
            self.app._apply_team_save(fields)

        self.app.push_screen(TeamEditModal(name or "", team), _on_result)

    def action_toggle_default(self) -> None:
        name = self._highlighted_name()
        if name is None:
            self.app.notify("no team highlighted", severity="warning")
            return
        config: SyncmillConfig = self.app._config
        target = None if (config.default_team or None) == name else name
        self.app._apply_default_team(target)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── the app ──────────────────────────────────────────────────────────────────


class ReportScreen(ModalScreen[None]):
    """Read-only run-envelope browser with ``report show``-identical detail."""

    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("p", "cancel", "Close"),
        ("r", "refresh_reports", "Refresh"),
    ]

    def __init__(self, runs_dir: str | Path):
        super().__init__()
        self._runs_dir = runs_dir
        self._reports: list[dict] = []
        # Position-based ids keep malformed duplicate/missing embedded run_ids
        # from colliding inside Textual's OptionList.
        self._docs_by_option: dict[str, dict] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="report-dialog"):
            yield PlainLabel("reports — newest first; r refresh, esc close", id="report-header")
            yield OptionList(id="report-list", markup=False)
            with VerticalScroll(id="report-detail-pane"):
                yield PlainStatic("", id="report-detail")

    def on_mount(self) -> None:
        self.refresh_reports()

    def refresh_reports(self) -> None:
        """Reload reports, preserving the selected run when it still exists."""
        highlighted = self._highlighted_doc()
        highlighted_run_id = highlighted.get("run_id") if highlighted else None
        self._reports = list_reports(self._runs_dir)
        self._docs_by_option = {}
        option_list = self.query_one("#report-list", OptionList)
        option_list.clear_options()
        if not self._reports:
            self._show_detail("(no run reports found)")
            return

        target = 0
        for index, doc in enumerate(self._reports):
            option_id = f"report-{index}"
            self._docs_by_option[option_id] = doc
            option_list.add_option(PlainOption(self._summary(doc), id=option_id))
            if highlighted_run_id is not None and doc.get("run_id") == highlighted_run_id:
                target = index
        option_list.highlighted = target
        self._show_detail(render_report(self._reports[target]))

    @staticmethod
    def _summary(doc: dict) -> str:
        result = doc.get("result")
        result = result if isinstance(result, dict) else {}
        return (
            f"{doc.get('run_id')}  {doc.get('created_at')}  {doc.get('strategy')}  "
            f"{result.get('status')}  {result.get('agent_id') or '—'}"
        )

    def _highlighted_doc(self) -> dict | None:
        option_list = self.query_one("#report-list", OptionList)
        index = option_list.highlighted
        if index is None:
            return None
        option_id = option_list.get_option_at_index(index).id
        return self._docs_by_option.get(str(option_id))

    def _show_detail(self, text: str) -> None:
        self.app.report_detail_text = text
        self.query_one("#report-detail", Static).update(text)

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ) -> None:
        doc = self._docs_by_option.get(str(event.option.id))
        if doc is not None:
            self._show_detail(render_report(doc))

    def action_refresh_reports(self) -> None:
        self.refresh_reports()

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        ("t", "teams", "Teams"),
        ("p", "reports", "Reports"),
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
    TeamScreen { align: center middle; }
    #team-dialog {
        width: 80; height: 24; border: thick $primary;
        padding: 1 2; background: $surface;
    }
    #team-list { height: 8; }
    #team-detail-pane { height: 1fr; border-top: solid $primary; }
    ReportScreen { align: center middle; }
    #report-dialog {
        width: 95%; height: 90%; max-width: 110; max-height: 30;
        border: thick $primary;
        padding: 1 2; background: $surface;
    }
    #report-list { height: 8; }
    #report-detail-pane { height: 1fr; border-top: solid $primary; }
    TeamEditModal { align: center middle; }
    #team-edit-dialog {
        width: 72; height: auto; border: thick $primary;
        padding: 1 2; background: $surface;
    }
    #team-edit-buttons { height: auto; align-horizontal: right; }
    #team-edit-buttons Button { margin-left: 2; }
    """

    def __init__(
        self,
        board: Board,
        *,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
        now: Callable[[], float] = time.time,
        config: SyncmillConfig | None = None,
        toml_path: Path | None = None,
    ):
        super().__init__()
        self.error_console = error_console()
        self._board = board
        self._refresh_seconds = refresh_seconds
        self._now = now
        #: TEAM pane inputs — both optional so a board-only construction (every
        #: pre-P3 caller/test) is untouched; `t` degrades to a notice without them.
        #: `toml_path` is resolved ONCE at launch (the CLI passes teamfile.toml_path())
        #: and every team write AND the post-write reload (load_config_at) pin it,
        #: so an in-app cwd drift is impossible (codex design-review Major).
        self._config = config
        self._toml_path = toml_path
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
        #: TEAM-pane mirrors of the same two kinds: the pane's detail text and the
        #: last teamfile-write rejection.
        self.team_detail_text: str = ""
        self.last_team_error: str | None = None
        #: REPORT-pane detail mirror — byte-identical to `render_report` and
        #: therefore to `report show` (apart from Click's trailing newline).
        self.report_detail_text: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            yield DataTable(id="items")
            with VerticalScroll(id="detail-pane"):
                yield PlainStatic("(no item selected)", id="detail")
        yield PlainStatic("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#items", DataTable)
        table.cursor_type = "row"
        table.add_columns(*map(plain, ("id", "state", "goal", "task", "owner", "age", "flags")))
        self.action_refresh()
        self.set_interval(self._refresh_seconds, self.action_refresh)

    # ── polling (best-effort reads) ──────────────────────────────────────────

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = False,
    ) -> None:
        """Textual's notify with markup off by default: messages quote errors
        and stored names, and a ``[/x]`` in one raises while the toast renders.
        The message is escaped as the CLI shows a body, the title as one line."""
        super().notify(
            printable_text(str(message)),
            title=printable_name(title),
            severity=severity,
            timeout=timeout,
            markup=markup,
        )

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
                *map(plain, (
                    _short(it.id),
                    it.state.value,
                    it.goal or "-",
                    _trunc_task(it.task),
                    it.owner_agent or "-",
                    _age(snap.at, it.updated_at),
                    _item_flags(it),
                )),
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

    def action_teams(self) -> None:
        if self._config is None:
            self.notify(
                "teams unavailable — the dashboard was launched without config",
                severity="warning",
            )
            return
        self.push_screen(TeamScreen())

    def action_reports(self) -> None:
        if self._config is None:
            self.notify(
                "reports unavailable — the dashboard was launched without config",
                severity="warning",
            )
            return
        # Viewing existing reports is intentionally NOT gated on `enabled`, the
        # same read-only posture as `report list` / `report show`.
        self.push_screen(ReportScreen(self._config.integration.report.runs_dir))

    # ── the TEAM-pane writes (the teamfile funnels) ──────────────────────────

    def _apply_team_save(self, fields: dict | None) -> bool:
        """All team saves funnel through here — EXACTLY the ``team save`` CLI
        recipe (parse → build → validate both views → comment-preserving write),
        so the file reads identically whichever surface the human used. Any
        :class:`TeamFileError` becomes an error toast + ``last_team_error`` —
        the app never dies on a rejected edit (the ``_apply_update`` contract)."""
        if not fields:
            return False
        if self._toml_path is None or self._config is None:
            self.last_team_error = "teams unavailable — no syncmill.toml path"
            self.notify(self.last_team_error, severity="error")
            return False
        try:
            name = fields.get("name", "").strip()
            agents = [a.strip() for a in fields.get("agents", "").split(",") if a.strip()]
            goals = [g.strip() for g in fields.get("goals", "").split(",") if g.strip()]
            specs = [s.strip() for s in fields.get("capabilities", "").split(";") if s.strip()]
            caps = parse_capability_specs(specs)
            team = build_team(
                agents=agents, strategy=fields.get("strategy"), goals=goals, capabilities=caps
            )
            # validate→save as one transaction (the `team save` CLI recipe):
            # a concurrent writer must not land between the two views and the
            # write. A failed acquire is a TeamFileError like any other, so the
            # existing toast/last_team_error path already covers it.
            with config_write_lock(self._toml_path):
                validate_team_save(self._config, name, team, path=self._toml_path)
                save_team(self._toml_path, name, team)
        except TeamFileError as exc:
            self.last_team_error = str(exc)
            self.notify(str(exc), severity="error")
            return False
        self.last_team_error = None
        self._reload_teams()
        self.notify(f"saved team '{name}'")
        return True

    def _apply_default_team(self, name: str | None) -> bool:
        """Persist (or with ``None`` clear) ``default_team`` — the ``team use``
        recipe; same error/reload contract as :meth:`_apply_team_save`."""
        if self._toml_path is None or self._config is None:
            self.last_team_error = "teams unavailable — no syncmill.toml path"
            self.notify(self.last_team_error, severity="error")
            return False
        if name is not None and name not in self._config.teams:
            self.last_team_error = f"unknown team: {name!r}"
            self.notify(self.last_team_error, severity="error")
            return False
        try:
            set_default_team(self._toml_path, name)
        except TeamFileError as exc:
            self.last_team_error = str(exc)
            self.notify(str(exc), severity="error")
            return False
        self.last_team_error = None
        self._reload_teams()
        self.notify("cleared default team" if name is None else f"default team: {name}")
        return True

    def _reload_teams(self) -> None:
        """Best-effort post-write reload — :func:`load_config_at` pins the exact
        file the write just touched (never the ambient cwd), preserving env
        precedence. A failed reload keeps the old snapshot + mirrors the error
        (near-impossible after pre-write validation, but the app never crashes);
        then the open TEAM pane, if any, re-renders from the fresh snapshot."""
        try:
            self._config = load_config_at(self._toml_path)
        except (ValidationError, OSError, ValueError) as exc:
            # ValueError covers tomllib.TOMLDecodeError: a mid-edit malformed
            # toml must keep the cached view + toast, never kill the app.
            self.last_team_error = f"config reload failed: {exc}"
            self.notify(self.last_team_error, severity="error")
        if isinstance(self.screen, TeamScreen):
            self.screen.refresh_teams()

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


def run_dashboard(
    board: Board,
    *,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    config: SyncmillConfig | None = None,
    toml_path: Path | None = None,
) -> None:
    """Run the dashboard over an already-open ``board`` (the caller closes it).
    The one thing cli.py imports — and the seam tests patch (at the lookup site,
    ``syncmill.cli.run_dashboard``). ``config`` feeds the TEAM and REPORT panes;
    ``toml_path`` is the TEAM write target. Both remain optional for backwards-
    compatible board-only constructions, where ``t``/``p`` degrade to notices."""
    install_render_guard()
    DashboardApp(
        board,
        refresh_seconds=clamp_refresh(refresh_seconds),
        config=config,
        toml_path=toml_path,
    ).run()
