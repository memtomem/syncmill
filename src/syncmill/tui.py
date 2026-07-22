"""Seven-tab SyncMill command center (`syncmill tui`)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from pathlib import Path

from pydantic import ValidationError
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    OptionList,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)
from textual.widgets.option_list import Option

from syncmill.board import (
    Board,
    BoardError,
    Evidence,
    WorkItem,
    WorkState,
    build_board,
    render_board_stats,
    render_decisions,
    render_item,
)
from syncmill.artifacts import ArtifactError
from syncmill.project import ProjectError, git_root, resolve_project_state_paths
from syncmill.config import SyncmillConfig
from syncmill.dashboard import DEFAULT_REFRESH_SECONDS, MIN_REFRESH_SECONDS
from syncmill.isolated_session import IsolatedSessionError, IsolatedSessionStore
from syncmill.report import list_reports, render_report
from syncmill.room_viewer import (
    RoomViewerError,
    attach_viewer,
    ensure_viewer,
    in_tmux,
    sweep_orphan_viewers,
)
from syncmill.rooms import RoomCoordinator, RoomError, launch_room_worker
from syncmill.session import SessionLaunchError
from syncmill.review_candidates import (
    DEFAULT_GOAL as REVIEW_DEFAULT_GOAL,
    ReviewCandidateError,
    item_specs as review_item_specs,
    load_report as load_review_report,
)
from syncmill.strategies import available_strategies
from syncmill.teamfile import (
    TeamFileError,
    build_team,
    load_config_at,
    parse_capability_specs,
    render_team,
    save_team,
    set_default_team,
    validate_team_save,
)
from syncmill.tui_jobs import (
    BoardJobSpec,
    JobError,
    JobStore,
    RoomApplySpec,
    canonical_board_path,
)
from syncmill.worktree import WorkspaceError
from syncmill.tui_collaboration import (
    CollaborationConfirmModal,
    RoomCreateModal,
    SessionOpenModal,
    render_candidate,
    render_room,
    render_session,
)

_CLI_SHIM = "from syncmill.cli import cli\ncli(prog_name='syncmill')"
_FILTERS: list[str | None] = [None, *(state.value for state in WorkState)]
_LOG_DISPLAY_BYTES = 1024 * 1024


def _log_signature(directory: Path) -> tuple[tuple[int, int, int] | None, ...]:
    values = []
    for name in ("console.log.1", "console.log"):
        try:
            info = (directory / name).stat()
            values.append((info.st_ino, info.st_size, info.st_mtime_ns))
        except OSError:
            values.append(None)
    return tuple(values)


def _read_log_tail(directory: Path, limit: int = _LOG_DISPLAY_BYTES) -> bytes:
    parts: list[bytes] = []
    remaining = limit
    for name in ("console.log", "console.log.1"):
        if remaining <= 0:
            break
        path = directory / name
        try:
            with path.open("rb") as fh:
                size = path.stat().st_size
                fh.seek(max(0, size - remaining))
                chunk = fh.read(remaining)
        except OSError:
            continue
        parts.insert(0, chunk)
        remaining -= len(chunk)
    return b"".join(parts)


def _job_label(row: dict) -> str:
    """One Runs-tab row label. Board-run rows keep the historical shape; apply
    ops show the op plus the fenced target's id prefix."""
    request = row.get("request", {})
    op = row.get("op") or "board-run"  # legacy schema-1 rows carry no op
    if op == "board-run":
        detail = request.get("mode")
    else:
        target = str(request.get("room_id") or request.get("run_id") or "")
        detail = f"{op} {target[:8]}".rstrip()
    return f"{row['job_id'][:8]} {row.get('status')} {detail}"


class _FormModal(ModalScreen[dict | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddItemModal(_FormModal):
    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Label("Add board item")
            yield TextArea(id="task", language=None)
            yield Input(placeholder="goal (optional)", id="goal")
            yield Input(placeholder="dependency ids, comma-separated", id="deps")
            yield Checkbox("human required", id="human")
            with Horizontal(classes="form-buttons"):
                yield Button("Add", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        self.dismiss(
            {
                "task": self.query_one("#task", TextArea).text,
                "goal": self.query_one("#goal", Input).value,
                "deps": self.query_one("#deps", Input).value,
                "human": self.query_one("#human", Checkbox).value,
            }
        )


class ImportModal(_FormModal):
    def compose(self) -> ComposeResult:
        with Vertical(classes="form-dialog"):
            yield Label("Import Tracegraph review candidates")
            yield Input(placeholder="report path", id="path")
            yield Input(value=REVIEW_DEFAULT_GOAL, placeholder="goal", id="goal")
            with Horizontal(classes="form-buttons"):
                yield Button("Import", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        self.dismiss(
            {
                "path": self.query_one("#path", Input).value,
                "goal": self.query_one("#goal", Input).value,
            }
        )


class DecisionModal(_FormModal):
    def __init__(self, board: Board, goal: str | None):
        super().__init__()
        self._board = board
        self._goal = goal
        self._decisions = board.active_decisions(goal=goal)

    def compose(self) -> ComposeResult:
        with Vertical(classes="wide-dialog"):
            yield Static(render_decisions(self._decisions), id="decision-list")
            yield TextArea(id="decision-text")
            yield Input(value=self._goal or "", placeholder="scope (blank = global)", id="scope")
            supersedable = [
                decision
                for decision in self._decisions
                if decision.scope == (self._goal or "")
            ]
            choices = [
                ("(none)", ""),
                *((f"{d.id[:8]} {d.text[:50]}", d.id) for d in supersedable),
            ]
            yield Select(choices, value="", allow_blank=False, id="supersedes")
            with Horizontal(classes="form-buttons"):
                yield Button("Record", variant="primary", id="apply")
                yield Button("Close", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        supersedes = self.query_one("#supersedes", Select).value
        self.dismiss(
            {
                "text": self.query_one("#decision-text", TextArea).text,
                "scope": self.query_one("#scope", Input).value,
                "supersedes": supersedes if isinstance(supersedes, str) and supersedes else None,
            }
        )


class BoardUpdateModal(_FormModal):
    def __init__(self, item: WorkItem):
        super().__init__()
        self._item = item

    def compose(self) -> ComposeResult:
        states = [
            ("(keep state)", ""),
            *((s.value, s.value) for s in WorkState if s is not WorkState.RUNNING),
        ]
        with Vertical(classes="form-dialog"):
            yield Label(f"Update {self._item.id[:8]}")
            yield Select(states, value="", allow_blank=False, id="state")
            yield Input(placeholder="audit note", id="note")
            yield Input(placeholder="evidence ref", id="evidence")
            yield Checkbox("sign off", id="sign-off")
            yield Checkbox("clear human-required", id="clear-human")
            with Horizontal(classes="form-buttons"):
                yield Button("Apply", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        state = self.query_one("#state", Select).value
        self.dismiss(
            {
                "state": state if isinstance(state, str) and state else None,
                "note": self.query_one("#note", Input).value,
                "evidence": self.query_one("#evidence", Input).value,
                "sign_off": self.query_one("#sign-off", Checkbox).value,
                "clear_human": self.query_one("#clear-human", Checkbox).value,
            }
        )


class JobModal(_FormModal):
    def __init__(self, config: SyncmillConfig, *, item: WorkItem | None = None):
        super().__init__()
        self._config = config
        self._item = item

    def compose(self) -> ComposeResult:
        teams = [("Auto", ""), *((name, name) for name in sorted(self._config.teams))]
        strategies = [("Inherit", ""), *((name, name) for name in available_strategies())]
        agents = [("Auto", ""), *((name, name) for name in self._config.agent_priority)]
        with Vertical(classes="wide-dialog"):
            if self._item is not None:
                yield Static(
                    f"Run selected item {self._item.id[:8]}\n{self._item.task}\n\n"
                    "A targeted run may bypass dependency/human-required queue gates.",
                    id="job-summary",
                )
            else:
                yield Static(
                    "Drain the board queue. Choose an exact goal or leave blank for All.",
                    id="job-summary",
                )
                yield Input(placeholder="goal (blank = All)", id="job-goal")
            yield Select(teams, value="", allow_blank=False, id="job-team")
            yield Select(strategies, value="", allow_blank=False, id="job-strategy")
            yield Select(agents, value="", allow_blank=False, id="job-agent")
            yield Static(
                "This starts real agent CLIs and may consume quota or incur cost. "
                "Programmatic runs use isolated worktrees and finish in review/blocked.",
                classes="warning",
            )
            yield Checkbox("I understand and want to start", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button("Start", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the cost and execution warning", severity="warning")
            return

        def selected(name: str) -> str | None:
            value = self.query_one(name, Select).value
            return value if isinstance(value, str) and value else None

        self.dismiss(
            {
                "goal": (
                    None
                    if self._item is not None
                    else self.query_one("#job-goal", Input).value
                ),
                "team": selected("#job-team"),
                "strategy": selected("#job-strategy"),
                "agent": selected("#job-agent"),
            }
        )


class TeamEditModal(_FormModal):
    def __init__(self, name: str, config: SyncmillConfig):
        super().__init__()
        self._name = name
        self._team = config.teams.get(name)

    def compose(self) -> ComposeResult:
        team = self._team
        caps = "; ".join(
            f"{agent}={','.join(values)}"
            for agent, values in (team.capabilities if team else {}).items()
        )
        with Vertical(classes="wide-dialog"):
            yield Input(value=self._name, placeholder="team name", id="name")
            yield Input(
                value=", ".join(team.agents) if team else "",
                placeholder="agents",
                id="agents",
            )
            yield Select(
                [("Inherit", ""), *((name, name) for name in available_strategies())],
                value=team.strategy if team and team.strategy else "",
                allow_blank=False,
                id="strategy",
            )
            yield Input(
                value=", ".join(team.goals) if team else "",
                placeholder="goals",
                id="goals",
            )
            yield Input(value=caps, placeholder="codex=python,cli; claude=docs", id="caps")
            with Horizontal(classes="form-buttons"):
                yield Button("Save", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        strategy = self.query_one("#strategy", Select).value
        self.dismiss(
            {
                "name": self.query_one("#name", Input).value,
                "agents": self.query_one("#agents", Input).value,
                "strategy": strategy if isinstance(strategy, str) and strategy else None,
                "goals": self.query_one("#goals", Input).value,
                "caps": self.query_one("#caps", Input).value,
            }
        )


class SyncmillTuiApp(App[None]):
    TITLE = "syncmill tui"
    BINDINGS = [
        ("1", "tab('board-tab')", "Board"),
        ("2", "tab('runs-tab')", "Runs"),
        ("3", "tab('reports-tab')", "Reports"),
        ("4", "tab('teams-tab')", "Teams"),
        ("5", "tab('status-tab')", "Status"),
        ("6", "tab('sessions-tab')", "Sessions"),
        ("7", "tab('rooms-tab')", "Rooms"),
        ("r", "refresh_current", "Refresh"),
        ("f", "context_f", "Filter/follow"),
        ("a", "context_add", "Add"),
        ("i", "import_candidates", "Import"),
        ("d", "decisions", "Decisions"),
        ("e", "edit", "Edit"),
        ("u", "unblock", "Unblock"),
        ("h", "clear_human", "Clear human"),
        ("s", "stats", "Stats"),
        ("x", "run_selected", "Run selected"),
        ("g", "run_loop", "Run queue"),
        ("t", "terminal", "Terminal"),
        ("k", "safe_stop", "Safe stop"),
        ("c", "checkpoint", "Checkpoint"),
        ("K", "force_stop", "Force stop"),
        ("R", "recover_runs", "Recover"),
        ("?", "help", "Help"),
        ("q", "quit", "Quit UI"),
    ]

    CSS = """
    #tabs { height: 1fr; }
    .split { height: 1fr; }
    .left { width: 3fr; }
    .right { width: 2fr; border-left: solid $primary; padding: 0 1; }
    .status-line { height: 2; padding: 0 1; background: $panel; }
    .form-dialog {
        width: 72; height: auto; border: thick $primary;
        padding: 1 2; background: $surface;
    }
    .wide-dialog {
        width: 90%; max-width: 100; height: 85%; border: thick $primary;
        padding: 1 2; background: $surface;
    }
    .form-buttons { height: auto; align-horizontal: right; }
    .form-buttons Button { margin-left: 2; }
    _FormModal { align: center middle; }
    .warning { color: $warning; margin: 1 0; }
    #board-table, #job-list, #report-list, #team-list, #session-list, #room-list { height: 1fr; }
    #candidate-list { height: 2fr; border-top: solid $primary; }
    #job-log { height: 1fr; }
    """

    def __init__(
        self,
        config: SyncmillConfig,
        *,
        board: Board | None,
        board_error: str | None,
        toml_path: Path,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ):
        super().__init__()
        self._config = config
        self._board = board
        self._board_error = board_error
        self._toml_path = toml_path
        self._refresh_seconds = max(MIN_REFRESH_SECONDS, refresh_seconds)
        self._filter: str | None = None
        self._selected_item_id: str | None = None
        self._selected_job_dir: str | None = None
        self._selected_report_id: str | None = None
        self._selected_team: str | None = None
        self._selected_session_id: str | None = None
        self._selected_room_id: str | None = None
        self._selected_candidate_id: str | None = None
        self._follow_log = True
        self._refreshing_jobs = False
        self._job_log_signature: tuple[tuple[int, int, int] | None, ...] | None = None
        self._job_log_dir: str | None = None
        self._memory_status = "(press r to probe memory)"
        self._jobs: JobStore | None = None
        self._jobs_error: str | None = None
        self._session_store: IsolatedSessionStore | None = None
        self._rooms: RoomCoordinator | None = None
        self._collaboration_error: str | None = None
        self._rebind_jobs()
        self._rebind_collaboration()

    def _project_root(self) -> Path:
        config_root = git_root(self._toml_path.parent.resolve())
        value = Path(self._config.repo_root).expanduser()
        if not value.is_absolute():
            value = config_root / value
        return git_root(value.resolve())

    def _job_project_root(self) -> Path:
        """The job store's project anchor. Resolve the CONFIGURED repo_root against
        the config location (never cwd), then git-canonicalize when it is a git
        repo; otherwise use the resolved target verbatim (syncmill supports
        non-git repos). Never discards the configured repo_root."""
        anchor = self._toml_path.parent.resolve()
        value = Path(self._config.repo_root).expanduser()
        target = (value if value.is_absolute() else anchor / value).resolve()
        try:
            return git_root(target)
        except ProjectError:
            return target

    def _rebind_collaboration(self) -> None:
        if self._rooms is not None:
            self._rooms.close()
        try:
            config_root = git_root(self._toml_path.parent.resolve())
            root = self._project_root()
            runtime_config = self._config.model_copy(deep=True)
            runtime_config.repo_root = str(root)
            resolve_project_state_paths(runtime_config, config_root)
            self._session_store = IsolatedSessionStore(runtime_config, root)
            self._rooms = (
                RoomCoordinator(runtime_config, root)
                if runtime_config.rooms.enabled
                else None
            )
            self._collaboration_error = None
        except Exception as exc:
            self._session_store = None
            self._rooms = None
            self._collaboration_error = str(exc)

    def _rebind_jobs(self) -> None:
        previous = self._jobs
        try:
            db_path = (
                self._board.db_path
                if self._board is not None
                else canonical_board_path(self._config.board.db_path)
            )
            # The store anchors on the canonical project root (its project_key) —
            # the SAME root rooms/sessions use — NOT the board db, so room/artifact
            # ops (which need not have a board) key coherently; the board db is
            # store context for board-run keying/env. syncmill supports non-git
            # repos (worktree.py's tempdir fallback): resolve the CONFIGURED
            # repo_root against the config anchor, git-canonicalize when possible,
            # else use that resolved target (never cwd, never a discarded config).
            root = self._job_project_root()
            replacement = JobStore(self._config.tui, root, board_db=db_path)
        except (JobError, ProjectError) as exc:
            replacement = None
            self._jobs_error = str(exc)
        else:
            self._jobs_error = None
        changed = (
            previous is None
            or replacement is None
            or previous.project_root != replacement.project_root
            or previous.board_db != replacement.board_db
        )
        self._jobs = replacement
        if changed:
            self._selected_job_dir = None
            self._job_log_dir = None
            self._job_log_signature = None

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("Board", id="board-tab"):
                with Horizontal(classes="split"):
                    yield DataTable(id="board-table", classes="left")
                    with VerticalScroll(classes="right"):
                        yield Static("", id="board-detail")
                yield Static("", id="board-status", classes="status-line")
            with TabPane("Runs", id="runs-tab"):
                with Horizontal(classes="split"):
                    yield OptionList(id="job-list", classes="left")
                    yield Log(id="job-log", classes="right", highlight=False)
                yield Static("", id="job-status", classes="status-line")
            with TabPane("Reports", id="reports-tab"):
                with Horizontal(classes="split"):
                    yield OptionList(id="report-list", classes="left")
                    with VerticalScroll(classes="right"):
                        yield Static("", id="report-detail")
            with TabPane("Teams", id="teams-tab"):
                with Horizontal(classes="split"):
                    yield OptionList(id="team-list", classes="left")
                    with VerticalScroll(classes="right"):
                        yield Static("", id="team-detail")
            with TabPane("Status", id="status-tab"):
                yield Static("", id="agent-status")
                yield Static("", id="memory-status")
            with TabPane("Sessions", id="sessions-tab"):
                with Horizontal(classes="split"):
                    yield OptionList(id="session-list", classes="left")
                    with VerticalScroll(classes="right"):
                        yield Static("", id="session-detail")
                yield Static("", id="session-status", classes="status-line")
            with TabPane("Rooms", id="rooms-tab"):
                with Horizontal(classes="split"):
                    with Vertical(classes="left"):
                        yield OptionList(id="room-list")
                        yield OptionList(id="candidate-list")
                    with VerticalScroll(classes="right"):
                        yield Static("", id="room-detail")
                        yield Static("", id="candidate-detail")
                yield Static("", id="room-status", classes="status-line")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#board-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("id", "state", "goal", "task", "owner", "flags")
        self._refresh_all()
        self.set_interval(self._refresh_seconds, self._poll)

    def on_unmount(self) -> None:
        if self._board is not None:
            # The CLI also closes its latest connection; this one may have been
            # replaced by retry, and Board.close() is intentionally idempotent.
            self._board.close()
        if self._rooms is not None:
            self._rooms.close()

    def _active(self) -> str:
        return self.query_one("#tabs", TabbedContent).active

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab
        self.action_refresh_current()

    def action_quit(self) -> None:
        if self._jobs is not None and self._jobs.active_job() is not None:
            self.notify("TUI closed; the worker continues in the background")
        self.exit()

    def action_help(self) -> None:
        self.notify(
            "1-7 tabs; Sessions a/x/c/k; Rooms a/x/e/g; "
            "Board a/i/d/e/u/h/s/x/g; Runs f/k/K/R; q detaches"
        )

    def _poll(self) -> None:
        active = self._active()
        if active == "board-tab":
            self._refresh_board()
        elif active == "runs-tab":
            self._refresh_jobs()
        elif active == "sessions-tab":
            self._refresh_sessions()
        elif active == "rooms-tab":
            self._refresh_rooms()

    def _refresh_all(self) -> None:
        self._refresh_board()
        self._refresh_jobs()
        self._refresh_reports()
        self._refresh_teams()
        self._refresh_agents()
        self._refresh_sessions()
        self._refresh_rooms()

    def action_refresh_current(self) -> None:
        active = self._active()
        if active == "board-tab":
            if self._board is None:
                self._retry_board()
            self._refresh_board()
        elif active == "runs-tab":
            self._refresh_jobs()
        elif active == "reports-tab":
            self._refresh_reports()
        elif active == "teams-tab":
            self._reload_config()
            self._refresh_teams()
        elif active == "sessions-tab":
            self._refresh_sessions()
        elif active == "rooms-tab":
            if not self._reload_config():
                return
            self._refresh_rooms()
        else:
            self._refresh_agents()
            self._start_memory_probe()

    def _board_unavailable(self) -> str | None:
        if self._board is not None:
            return None
        return self._board_error or (
            "the work-board is disabled — enable it with SYNCMILL_BOARD__ENABLED=true "
            "or [board] enabled=true in syncmill.toml; press r to retry"
        )

    def _refresh_board(self) -> None:
        table = self.query_one("#board-table", DataTable)
        error = self._board_unavailable()
        if error:
            table.clear()
            self.query_one("#board-detail", Static).update(error)
            self.query_one("#board-status", Static).update("LIMITED MODE | Board unavailable")
            return
        assert self._board is not None
        try:
            items = self._board.list(state=self._filter)
            stats = self._board.stats()
        except BoardError as exc:
            self._board_error = str(exc)
            self.query_one("#board-status", Static).update(f"DEGRADED: {exc}")
            return
        table.clear()
        for item in items:
            flags = "H!" if item.human_required else ""
            if item.depends_on:
                flags += f" deps={len(item.depends_on)}"
            table.add_row(
                item.id[:8], item.state.value, item.goal or "-", item.task[:50],
                item.owner_agent or "-", flags.strip(), key=item.id,
            )
        if self._selected_item_id and self._board.get(self._selected_item_id):
            self._show_item(self._selected_item_id)
        self.query_one("#board-status", Static).update(
            f"items:{stats.total} | filter:{self._filter or 'all'} | "
            f"decisions:{len(self._board.active_decisions())}"
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "board-table" or event.row_key is None:
            return
        self._selected_item_id = str(event.row_key.value)
        self._show_item(self._selected_item_id)

    def _show_item(self, item_id: str) -> None:
        if self._board is None:
            return
        try:
            item = self._board.get(item_id)
            text = render_item(item, self._board.transitions(item_id)) if item else "(not found)"
        except BoardError as exc:
            text = str(exc)
        self.query_one("#board-detail", Static).update(text)

    def _selected_item(self) -> WorkItem | None:
        if self._board is None or self._selected_item_id is None:
            return None
        return self._board.get(self._selected_item_id)

    def action_context_f(self) -> None:
        if self._active() == "runs-tab":
            self._follow_log = not self._follow_log
            self.notify(f"log follow {'on' if self._follow_log else 'off'}")
        elif self._active() == "board-tab":
            index = _FILTERS.index(self._filter)
            self._filter = _FILTERS[(index + 1) % len(_FILTERS)]
            self._refresh_board()

    def action_context_add(self) -> None:
        active = self._active()
        if active == "board-tab":
            if self._board is None:
                self.notify(self._board_unavailable() or "Board unavailable", severity="warning")
                return
            self.push_screen(AddItemModal(), self._apply_add)
        elif active == "teams-tab":
            self.push_screen(TeamEditModal("", self._config), self._apply_team)
        elif active == "sessions-tab":
            if not self._config.agent_priority:
                self.notify("no interactive agents are configured", severity="warning")
                return
            self.push_screen(SessionOpenModal(self._config), self._apply_session_open)
        elif active == "rooms-tab":
            self.push_screen(RoomCreateModal(self._config), self._apply_room_create)

    def _apply_add(self, fields: dict | None) -> None:
        if not fields or self._board is None:
            return
        task = fields["task"].strip()
        if not task:
            self.notify("task must not be empty", severity="error")
            return
        try:
            item = self._board.add(
                task,
                goal=fields["goal"].strip(),
                depends_on=[part.strip() for part in fields["deps"].split(",") if part.strip()],
                human_required=fields["human"],
            )
        except BoardError as exc:
            self.notify(str(exc), severity="error")
            return
        self._selected_item_id = item.id
        self._refresh_board()

    def action_import_candidates(self) -> None:
        if self._active() != "board-tab" or self._board is None:
            return
        self.push_screen(ImportModal(), self._apply_import)

    def _apply_import(self, fields: dict | None) -> None:
        if not fields or self._board is None:
            return
        try:
            path = Path(fields["path"]).expanduser()
            if not path.is_absolute():
                path = Path.cwd() / path
            report = load_review_report(path)
            result = self._board.import_review_candidates(
                review_item_specs(report), goal=fields["goal"].strip() or REVIEW_DEFAULT_GOAL
            )
        except (ReviewCandidateError, BoardError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"imported {result.imported}; skipped {result.skipped}")
        self._refresh_board()

    def action_decisions(self) -> None:
        if self._active() != "board-tab" or self._board is None:
            return
        item = self._selected_item()
        self.push_screen(
            DecisionModal(self._board, item.goal if item else None),
            self._apply_decision,
        )

    def _apply_decision(self, fields: dict | None) -> None:
        if not fields or self._board is None:
            return
        text = fields["text"].strip()
        if not text:
            self.notify("decision text must not be empty", severity="error")
            return
        try:
            self._board.add_decision(
                text, scope=fields["scope"].strip(), supersedes=fields["supersedes"]
            )
        except BoardError as exc:
            self.notify(str(exc), severity="error")
            return
        self._refresh_board()

    def action_edit(self) -> None:
        if self._active() == "board-tab":
            item = self._selected_item()
            if item:
                self.push_screen(BoardUpdateModal(item), self._apply_update)
        elif self._active() == "teams-tab":
            self.push_screen(
                TeamEditModal(self._selected_team or "", self._config),
                self._apply_team,
            )
        elif self._active() == "rooms-tab":
            self._confirm_candidate_selection()

    def _apply_update(self, fields: dict | None) -> None:
        if not fields or self._board is None or not self._selected_item_id:
            return
        evidence: list[Evidence] = []
        now = time.time()
        if fields["evidence"]:
            evidence.append(
                Evidence(
                    kind="manual",
                    ref=fields["evidence"],
                    passed=True,
                    at=now,
                    by="human",
                )
            )
        if fields["sign_off"]:
            evidence.append(Evidence(kind="human", ref="sign-off", passed=True, at=now, by="human"))
        try:
            self._board.transition(
                self._selected_item_id,
                fields["state"],
                actor="human",
                note=fields["note"],
                evidence=evidence or None,
                human_required=False if fields["clear_human"] else None,
            )
        except BoardError as exc:
            self.notify(str(exc), severity="error")
            return
        self._refresh_board()

    def action_unblock(self) -> None:
        if self._active() == "teams-tab":
            self.action_toggle_default_team()
            return
        self._quick_update(to_state=WorkState.PLANNED.value, note="unblocked")

    def action_clear_human(self) -> None:
        self._quick_update(human_required=False)

    def _quick_update(self, *, to_state: str | None = None, **values: object) -> None:
        if self._active() != "board-tab" or self._board is None or not self._selected_item_id:
            return
        try:
            self._board.transition(
                self._selected_item_id, to_state, actor="human", **values
            )
        except BoardError as exc:
            self.notify(str(exc), severity="error")
            return
        self._refresh_board()

    def action_stats(self) -> None:
        if self._active() == "board-tab" and self._board is not None:
            self.notify(render_board_stats(self._board.stats()))

    def action_run_selected(self) -> None:
        if self._active() == "sessions-tab":
            self._attach_selected_session()
            return
        if self._active() == "rooms-tab":
            self._confirm_room_round()
            return
        if self._active() != "board-tab":
            return
        item = self._selected_item()
        if item is None or item.state is not WorkState.PLANNED:
            self.notify("select a planned item", severity="warning")
            return
        self.push_screen(
            JobModal(self._config, item=item),
            lambda fields: self._launch_job(fields, item),
        )

    def action_terminal(self) -> None:
        """Rooms `t`: open the read-only tmux viewer for the selected room's
        latest round. Manual (a round never auto-attaches), and shares the
        room_viewer validation/transition layer with `syncmill room attach`.
        """
        if self._active() != "rooms-tab":
            return
        if self._rooms is None or not self._selected_room_id:
            self.notify("select a room", severity="warning")
            return
        room_id = self._selected_room_id
        project_id = self._rooms.project_id
        try:
            # Reclaim viewers left by since-closed/pruned rooms (best-effort).
            with contextlib.suppress(Exception):
                sweep_orphan_viewers(self._rooms.store, project_id)
            name = ensure_viewer(self._rooms.store, project_id, room_id)
        except (RoomViewerError, RoomError) as exc:
            self.notify(str(exc), severity="error")
            return
        try:
            if in_tmux():
                # Non-blocking switch — no Textual suspend needed; the user
                # returns to the TUI with Prefix + L.
                rc = attach_viewer(name)
            else:
                # External foreground attach blocks; hand the terminal over.
                with self.suspend():
                    rc = attach_viewer(name)
            if rc != 0:
                self.notify(f"could not attach to viewer (tmux exit {rc})", severity="error")
        finally:
            self._refresh_all()

    def action_run_loop(self) -> None:
        if self._active() == "rooms-tab":
            self._confirm_room_apply()
            return
        if self._active() != "board-tab" or self._board is None:
            return
        self.push_screen(JobModal(self._config), lambda fields: self._launch_job(fields, None))

    def _launch_job(self, fields: dict | None, item: WorkItem | None) -> None:
        if not fields:
            return
        try:
            if self._jobs is None:
                raise JobError(self._jobs_error or "TUI job registry is unavailable")
            spec = BoardJobSpec(
                mode="selected" if item else "loop",
                item_id=item.id if item else None,
                goal=(fields["goal"].strip() or None) if fields["goal"] is not None else None,
                team=fields["team"],
                strategy=fields["strategy"],
                agent=fields["agent"],
            )
            self._jobs.launch(spec)
        except (JobError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.action_tab("runs-tab")

    def _refresh_jobs(self) -> None:
        options = self.query_one("#job-list", OptionList)
        if self._jobs is None:
            options.clear_options()
            self.query_one("#job-log", Log).clear()
            self.query_one("#job-status", Static).update(
                f"DEGRADED | Runs unavailable: {self._jobs_error or 'unknown error'}"
            )
            return
        rows = self._jobs.list_jobs()
        highlighted = self._selected_job_dir
        self._refreshing_jobs = True
        options.clear_options()
        if not rows:
            self.query_one("#job-status", Static).update("no TUI jobs")
            self.query_one("#job-log", Log).clear()
            self._refreshing_jobs = False
            return
        # Preserve an existing valid selection (so a user can inspect/stop ANY job
        # among several active targets); only auto-select an active job when there
        # is no valid current selection.
        known = {row["job_dir"] for row in rows}
        if highlighted not in known:
            active = self._jobs.active_job()
            highlighted = active["job_dir"] if active else None
        for row in rows:
            options.add_option(Option(_job_label(row), id=row["job_dir"]))
        target = next((i for i, row in enumerate(rows) if row["job_dir"] == highlighted), 0)
        try:
            options.highlighted = target
            self._selected_job_dir = rows[target]["job_dir"]
        finally:
            self._refreshing_jobs = False
        self._show_job(rows[target])

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "job-list":
            if self._refreshing_jobs or self._jobs is None:
                return
            self._selected_job_dir = str(event.option.id)
            row = next(
                (
                    row
                    for row in self._jobs.list_jobs()
                    if row["job_dir"] == self._selected_job_dir
                ),
                None,
            )
            if row:
                self._show_job(row)
        elif event.option_list.id == "report-list":
            self._selected_report_id = str(event.option.id)
            self._show_report(self._selected_report_id)
        elif event.option_list.id == "team-list":
            self._selected_team = str(event.option.id)
            self._show_team(self._selected_team)
        elif event.option_list.id == "session-list":
            self._selected_session_id = str(event.option.id)
            self._show_session(self._selected_session_id)
        elif event.option_list.id == "room-list":
            self._selected_room_id = str(event.option.id)
            self._selected_candidate_id = None
            self._show_room(self._selected_room_id)
        elif event.option_list.id == "candidate-list":
            self._selected_candidate_id = str(event.option.id)
            self._show_candidate(self._selected_candidate_id)

    def _show_job(self, row: dict) -> None:
        directory = Path(row["job_dir"])
        signature = _log_signature(directory)
        self.query_one("#job-status", Static).update(
            f"{row.get('status')} | exit:{row.get('exit_code')} | cwd:{row.get('cwd')}"
        )
        if self._job_log_dir == str(directory) and self._job_log_signature == signature:
            return
        log = self.query_one("#job-log", Log)
        log.clear()
        text = _read_log_tail(directory).decode("utf-8", errors="replace")
        for line in text.splitlines() or [""]:
            log.write_line(line)
        if self._follow_log:
            log.scroll_end(animate=False)
        self._job_log_dir = str(directory)
        self._job_log_signature = signature

    def _refresh_sessions(self) -> None:
        options = self.query_one("#session-list", OptionList)
        options.clear_options()
        if self._session_store is None:
            self.query_one("#session-detail", Static).update(
                self._collaboration_error or "Sessions unavailable"
            )
            self.query_one("#session-status", Static).update("DEGRADED")
            return
        values = self._session_store.list()
        if not values:
            self._selected_session_id = None
            self.query_one("#session-detail", Static).update(
                "No project-scoped sessions. Press a to open one."
            )
            self.query_one("#session-status", Static).update("sessions:0")
            return
        ids = [value["session_id"] for value in values]
        if self._selected_session_id not in ids:
            self._selected_session_id = ids[0]
        for value in values:
            live = "live" if value.get("live") else "stopped"
            stale = " STALE" if value.get("source_stale") else ""
            options.add_option(
                Option(
                    f"{value['session_id'][:8]} {value['agent_id']} "
                    f"{value['state']} {live}{stale}",
                    id=value["session_id"],
                )
            )
        options.highlighted = ids.index(self._selected_session_id)
        self._show_session(self._selected_session_id)
        self.query_one("#session-status", Static).update(f"sessions:{len(values)}")

    def _show_session(self, session_id: str) -> None:
        if self._session_store is None:
            return
        value = next(
            (
                item
                for item in self._session_store.list()
                if item["session_id"] == session_id
            ),
            None,
        )
        self.query_one("#session-detail", Static).update(
            render_session(value) if value else "(session unavailable)"
        )

    def _apply_session_open(self, fields: dict | bool | None) -> None:
        if not isinstance(fields, dict) or self._session_store is None:
            return
        try:
            value = self._session_store.create(str(fields["agent"]))
        except (IsolatedSessionError, SessionLaunchError, WorkspaceError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self._selected_session_id = value["session_id"]
        self._refresh_sessions()
        self.notify(f"isolated session {value['session_id'][:8]} started")

    def _attach_selected_session(self) -> None:
        if self._session_store is None or not self._selected_session_id:
            self.notify("select a session", severity="warning")
            return
        try:
            with self.suspend():
                self._session_store.attach(self._selected_session_id)
        except (IsolatedSessionError, OSError) as exc:
            self.notify(str(exc), severity="error")
        finally:
            self._refresh_all()

    def action_checkpoint(self) -> None:
        if self._active() != "sessions-tab":
            return
        if self._session_store is None or not self._selected_session_id:
            self.notify("select a session", severity="warning")
            return
        try:
            run_id = self._session_store.checkpoint(self._selected_session_id)
        except (IsolatedSessionError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"checkpoint {run_id[:8]} ready; review in Runs/artifacts")
        self._refresh_sessions()

    def _confirm_session_close(self) -> None:
        if self._session_store is None or not self._selected_session_id:
            self.notify("select a session", severity="warning")
            return
        try:
            value = self._session_store.load(self._selected_session_id)
        except IsolatedSessionError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            CollaborationConfirmModal(
                "Close isolated session",
                f"Session: {value['session_id']}\nAgent: {value['agent_id']}\n"
                f"Worktree: {value['worktree_path']}\n\n"
                "Uncheckpointed changes will be discarded with the worktree.",
                "Close",
            ),
            self._apply_session_close,
        )

    def _apply_session_close(self, confirmed: dict | bool | None) -> None:
        if not confirmed or self._session_store is None or not self._selected_session_id:
            return
        try:
            self._session_store.close_session(self._selected_session_id)
        except (IsolatedSessionError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self._refresh_sessions()

    def _refresh_rooms(self) -> None:
        room_options = self.query_one("#room-list", OptionList)
        candidate_options = self.query_one("#candidate-list", OptionList)
        room_options.clear_options()
        candidate_options.clear_options()
        if self._rooms is None:
            message = self._collaboration_error or (
                "Rooms are disabled. Enable [rooms] enabled=true and press r."
            )
            self.query_one("#room-detail", Static).update(message)
            self.query_one("#candidate-detail", Static).update("")
            self.query_one("#room-status", Static).update("LIMITED MODE")
            return
        rooms = self._rooms.store.list_rooms()
        if not rooms:
            self._selected_room_id = None
            self.query_one("#room-detail", Static).update(
                "No rooms. Press a to create a comparison room."
            )
            self.query_one("#candidate-detail", Static).update("")
            self.query_one("#room-status", Static).update("rooms:0")
            return
        ids = [room["id"] for room in rooms]
        if self._selected_room_id not in ids:
            self._selected_room_id = ids[0]
        for room in rooms:
            room_options.add_option(
                Option(
                    f"{room['id'][:8]} {room['state']} r{room['round_count']}",
                    id=room["id"],
                )
            )
        room_options.highlighted = ids.index(self._selected_room_id)
        self._show_room(self._selected_room_id)
        self.query_one("#room-status", Static).update(f"rooms:{len(rooms)}")

    def _show_room(self, room_id: str) -> None:
        if self._rooms is None:
            return
        try:
            room = self._rooms.store.get_room(room_id)
            candidates = self._rooms.store.list_candidates(room_id)
            events = self._rooms.store.events(room_id)
        except RoomError as exc:
            self.query_one("#room-detail", Static).update(str(exc))
            return
        self.query_one("#room-detail", Static).update(
            render_room(room, candidates, events)
        )
        options = self.query_one("#candidate-list", OptionList)
        options.clear_options()
        if not candidates:
            self._selected_candidate_id = None
            self.query_one("#candidate-detail", Static).update("(no candidates yet)")
            return
        ids = [candidate["id"] for candidate in candidates]
        if self._selected_candidate_id not in ids:
            self._selected_candidate_id = ids[0]
        for candidate in candidates:
            options.add_option(
                Option(
                    f"{candidate['id'][:8]} {candidate['member_id']} "
                    f"{candidate['state']} {'✓' if candidate['eligible'] else '×'}",
                    id=candidate["id"],
                )
            )
        options.highlighted = ids.index(self._selected_candidate_id)
        self._show_candidate(self._selected_candidate_id)

    def _show_candidate(self, candidate_id: str) -> None:
        if self._rooms is None:
            return
        try:
            row = self._rooms.store.get_candidate(candidate_id)
            manifest = self._rooms.store.load_candidate(row["id"])
        except RoomError as exc:
            self.query_one("#candidate-detail", Static).update(str(exc))
            return
        self.query_one("#candidate-detail", Static).update(render_candidate(manifest))

    def _apply_room_create(self, fields: dict | bool | None) -> None:
        if not isinstance(fields, dict) or self._rooms is None:
            return
        agents = [part.strip() for part in str(fields["agents"]).split(",") if part.strip()]
        try:
            room = self._rooms.create(str(fields["prompt"]), agents)
        except RoomError as exc:
            self.notify(str(exc), severity="error")
            return
        self._selected_room_id = room["id"]
        self._refresh_rooms()

    def _confirm_room_round(self) -> None:
        if self._rooms is None or not self._selected_room_id:
            self.notify("select a room", severity="warning")
            return
        try:
            room = self._rooms.store.get_room(self._selected_room_id)
        except RoomError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            CollaborationConfirmModal(
                "Start parallel room round",
                f"Room: {room['id']}\nMembers: {len(room['members'])}\n"
                f"Maximum provider calls: {len(room['members'])}\n"
                "The detached manager will continue if the TUI closes.",
                "Run round",
            ),
            self._apply_room_round,
        )

    def _apply_room_round(self, confirmed: dict | bool | None) -> None:
        if not confirmed or self._rooms is None or not self._selected_room_id:
            return
        try:
            self._rooms.preflight_round(self._selected_room_id)
            pid = launch_room_worker(
                self._project_root(),
                self._selected_room_id,
                resolved_config=self._rooms.config,
                config_base=self._toml_path.parent,
            )
            self._rooms.store.record_event(
                self._selected_room_id, "worker.launched", {"pid": pid}
            )
        except (RoomError, ArtifactError, WorkspaceError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"room worker started (pid {pid})")
        self._refresh_rooms()

    def _confirm_candidate_selection(self) -> None:
        if (
            self._rooms is None
            or not self._selected_room_id
            or not self._selected_candidate_id
        ):
            self.notify("select a candidate", severity="warning")
            return
        try:
            room = self._rooms.store.get_room(self._selected_room_id)
            row = self._rooms.store.get_candidate(self._selected_candidate_id)
            manifest = self._rooms.store.load_candidate(row["id"])
        except RoomError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            CollaborationConfirmModal(
                "Select candidate (source remains unchanged)",
                f"Candidate: {manifest['candidate_id']}\n"
                f"Member: {manifest['member_id']}\n"
                f"Patch digest: {manifest['patch_sha256']}\n"
                f"Eligible: {manifest['eligible']}",
                "Select",
            ),
            lambda confirmed, room_id=room["id"], candidate_id=manifest["candidate_id"], revision=room["revision"]: self._apply_candidate_selection(
                confirmed, room_id, candidate_id, revision
            ),
        )

    def _apply_candidate_selection(
        self,
        confirmed: dict | bool | None,
        room_id: str,
        candidate_id: str,
        expected_revision: int,
    ) -> None:
        if not confirmed or self._rooms is None:
            return
        try:
            self._rooms.select(
                room_id, candidate_id, expected_revision=expected_revision
            )
        except RoomError as exc:
            self.notify(str(exc), severity="error")
            return
        self._refresh_rooms()

    def _confirm_room_apply(self) -> None:
        if self._rooms is None or not self._selected_room_id:
            self.notify("select a room", severity="warning")
            return
        if self._jobs is None:
            self.notify(
                self._jobs_error or "TUI job registry is unavailable", severity="error"
            )
            return
        try:
            room = self._rooms.store.get_room(self._selected_room_id)
            candidate_id = room.get("selected_candidate_id")
            if not candidate_id:
                raise RoomError("select an eligible candidate first")
            row = self._rooms.store.get_candidate(candidate_id)
            manifest = self._rooms.store.load_candidate(row["id"])
        except RoomError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            CollaborationConfirmModal(
                "Apply selected candidate to source",
                f"Room: {room['id']}\nCandidate: {candidate_id}\n"
                f"Patch digest: {manifest['patch_sha256']}\n"
                f"Target: {self._project_root()}\n\n"
                "Source fingerprint and patch applicability will be checked again.\n"
                "The detached worker will continue if the TUI closes.",
                "Apply",
            ),
            lambda confirmed, room_id=room["id"], candidate_id=candidate_id, revision=int(
                room["revision"]
            ): self._apply_room_apply(confirmed, room_id, candidate_id, revision),
        )

    def _apply_room_apply(
        self,
        confirmed: dict | bool | None,
        room_id: str,
        candidate_id: str,
        expected_revision: int,
    ) -> None:
        if not confirmed or self._rooms is None:
            return
        try:
            if self._jobs is None:
                raise JobError(self._jobs_error or "TUI job registry is unavailable")
            # Orchestration never runs on Textual's event loop (the R-recover
            # invariant): the apply runs in a durable detached JobStore worker,
            # fenced to the candidate/revision captured at confirmation time and
            # pinned to the coordinator's resolved config (the room-round freeze).
            spec = RoomApplySpec(
                room_id=room_id,
                verify=bool(self._config.gate.commands),
                expected_candidate_id=candidate_id,
                expected_revision=expected_revision,
            )
            row = self._jobs.launch(spec, resolved_config=self._rooms.config)
        except (JobError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"detached room-apply job {row['job_id'][:8]}")
        self.action_tab("runs-tab")

    def _selected_job_id(self) -> str | None:
        """The job_id of the Runs-tab highlighted row (its option id is the
        job_dir), resolved through list_jobs — no second selection state."""
        if self._jobs is None or self._selected_job_dir is None:
            return None
        for row in self._jobs.list_jobs():
            if row.get("job_dir") == self._selected_job_dir:
                return row.get("job_id")
        return None

    def _control_selected_job(self, action: str) -> None:
        try:
            if self._jobs is None:
                raise JobError(self._jobs_error or "TUI job registry is unavailable")
            job_id = self._selected_job_id()
            if job_id is None:
                raise JobError("select a job in the Runs tab first")
            self._jobs.control(job_id, action)
        except JobError as exc:
            self.notify(str(exc), severity="error")

    def action_safe_stop(self) -> None:
        if self._active() == "sessions-tab":
            self._confirm_session_close()
            return
        if self._active() != "runs-tab":
            return
        self._control_selected_job("stop")

    def action_force_stop(self) -> None:
        if self._active() != "runs-tab":
            return
        self._control_selected_job("force")

    def action_recover_runs(self) -> None:
        if self._active() != "runs-tab":
            return
        self.run_worker(
            self._recover_runs(), group="recover-runs", exclusive=True
        )

    async def _recover_runs(self) -> None:
        # Reconcile via a detached ``syncmill recover`` subprocess, never in
        # process: it takes the repository apply lock, and orchestration must
        # never run on Textual's event loop.  The subprocess also reuses the
        # exact CLI project-identity path, so it addresses the same runs an
        # ``apply`` would.  A live apply owner is skipped before the lock, so
        # this can never block on a healthy detached worker.
        root = self._job_project_root()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CLI_SHIM,
            "recover",
            "--repo",
            str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            raw, _ = await proc.communicate()
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), 2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            raise
        summary = raw.decode("utf-8", errors="replace").strip() or "nothing to recover"
        self.notify(
            summary,
            severity="error" if proc.returncode else "information",
            timeout=8,
        )
        self._refresh_jobs()

    def _refresh_reports(self) -> None:
        docs = list_reports(self._config.integration.report.runs_dir)
        options = self.query_one("#report-list", OptionList)
        selected = self._selected_report_id
        options.clear_options()
        if not docs:
            self.query_one("#report-detail", Static).update("(no run reports found)")
            return
        for doc in docs:
            result = doc.get("result") if isinstance(doc.get("result"), dict) else {}
            run_id = str(doc.get("run_id"))
            options.add_option(
                Option(
                    f"{run_id} {doc.get('strategy')} {result.get('status')}",
                    id=run_id,
                )
            )
        ids = [str(doc.get("run_id")) for doc in docs]
        self._selected_report_id = selected if selected in ids else ids[0]
        options.highlighted = ids.index(self._selected_report_id)
        self._show_report(self._selected_report_id)

    def _show_report(self, run_id: str) -> None:
        doc = next(
            (
                doc
                for doc in list_reports(self._config.integration.report.runs_dir)
                if str(doc.get("run_id")) == run_id
            ),
            None,
        )
        detail = render_report(doc) if doc else "(report unavailable)"
        self.query_one("#report-detail", Static).update(detail)

    def _refresh_teams(self) -> None:
        options = self.query_one("#team-list", OptionList)
        selected = self._selected_team
        options.clear_options()
        names = sorted(self._config.teams)
        if not names:
            self.query_one("#team-detail", Static).update("(no teams configured; press a)")
            return
        for name in names:
            marker = " *" if name == self._config.default_team else ""
            options.add_option(Option(f"{name}{marker}", id=name))
        self._selected_team = selected if selected in names else names[0]
        options.highlighted = names.index(self._selected_team)
        self._show_team(self._selected_team)

    def _show_team(self, name: str) -> None:
        team = self._config.teams.get(name)
        detail = render_team(name, team, self._config) if team else ""
        self.query_one("#team-detail", Static).update(detail)

    def _apply_team(self, fields: dict | None) -> None:
        if not fields:
            return
        try:
            name = fields["name"].strip()
            agents = [part.strip() for part in fields["agents"].split(",") if part.strip()]
            goals = [part.strip() for part in fields["goals"].split(",") if part.strip()]
            caps = parse_capability_specs(
                [part.strip() for part in fields["caps"].split(";") if part.strip()]
            )
            team = build_team(
                agents=agents,
                strategy=fields["strategy"],
                goals=goals,
                capabilities=caps,
            )
            validate_team_save(self._config, name, team, path=self._toml_path)
            save_team(self._toml_path, name, team)
            self._reload_config()
        except TeamFileError as exc:
            self.notify(str(exc), severity="error")
            return
        self._selected_team = name
        self._refresh_teams()

    def action_toggle_default_team(self) -> None:
        if self._active() != "teams-tab" or not self._selected_team:
            return
        target = None if self._config.default_team == self._selected_team else self._selected_team
        try:
            set_default_team(self._toml_path, target)
            self._reload_config()
        except TeamFileError as exc:
            self.notify(str(exc), severity="error")
        self._refresh_teams()

    def _reload_config(self) -> bool:
        try:
            config = load_config_at(self._toml_path)
        except ValidationError as exc:
            self.notify(f"config reload failed: {exc}", severity="error")
            return False
        self._config = config
        self._rebind_jobs()
        self._rebind_collaboration()
        return True

    def _retry_board(self) -> None:
        try:
            config = load_config_at(self._toml_path)
        except ValidationError as exc:
            self.notify(f"config reload failed: {exc}", severity="error")
            return
        if self._board is not None:
            self._board.close()
        self._config = config
        self._board = None
        if not config.board.enabled:
            self._board_error = None
            self._rebind_jobs()
            return
        try:
            self._board = build_board(config.board)
            self._board_error = None
        except BoardError as exc:
            self._board_error = str(exc)
        self._rebind_jobs()
        self._rebind_collaboration()

    def _refresh_agents(self) -> None:
        lines = ["Agents (priority order):"]
        for index, agent_id in enumerate(self._config.agent_priority, 1):
            cfg = self._config.agent_config(agent_id)
            lines.append(
                f"{index}. {agent_id} [{'enabled' if cfg.enabled else 'disabled'}] "
                f"timeout={cfg.timeout_seconds}s max_turns={cfg.max_turns}"
            )
        self.query_one("#agent-status", Static).update("\n".join(lines))
        self.query_one("#memory-status", Static).update(f"\nMemory:\n{self._memory_status}")

    def _start_memory_probe(self) -> None:
        if self._jobs is not None and self._jobs.active_job() is not None:
            self._memory_status = "probe deferred while a TUI worker is active"
            self._refresh_agents()
            return
        self._memory_status = "probing…"
        self._refresh_agents()
        self.run_worker(self._probe_memory(), group="memory-status", exclusive=True)

    async def _probe_memory(self) -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CLI_SHIM,
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            raw, _ = await proc.communicate()
        except asyncio.CancelledError:
            try:
                os.killpg(proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), 2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
            raise
        self._memory_status = raw.decode("utf-8", errors="replace").strip()
        if proc.returncode:
            self._memory_status = f"[exit {proc.returncode}]\n{self._memory_status}"
        self._refresh_agents()


def run_tui(
    config: SyncmillConfig,
    *,
    board: Board | None,
    board_error: str | None,
    toml_path: Path,
    refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
) -> None:
    SyncmillTuiApp(
        config,
        board=board,
        board_error=board_error,
        toml_path=toml_path,
        refresh_seconds=refresh_seconds,
    ).run()
