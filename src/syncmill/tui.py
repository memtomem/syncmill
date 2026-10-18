"""Seven-tab SyncMill command center (`syncmill tui`)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sqlite3
import sys
import time
from pathlib import Path

from pydantic import ValidationError
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
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
from syncmill.artifacts import (
    ArtifactError,
    ArtifactListing,
    ArtifactStore,
    SkippedRun,
    is_room_bound,
    printable_name,
)
from syncmill.project import (
    ProjectError,
    git_root,
    project_id,
    resolve_project_state_paths,
)
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
from syncmill.room_compare import (
    RoomComparison,
    build_room_comparison,
    find_round,
    find_row,
    render_round_table,
)
from syncmill.rooms import RoomCoordinator, RoomError, RoomStore, launch_room_worker
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
    config_write_lock,
    load_config_at,
    parse_capability_specs,
    render_team,
    save_team,
    set_default_team,
    validate_team_save,
)
from syncmill.tui_jobs import (
    ArtifactApplySpec,
    BoardJobSpec,
    JobError,
    JobStore,
    RoomApplySpec,
    canonical_board_path,
)
from syncmill.worktree import WorkspaceError, repository_fingerprint
from syncmill.tui_collaboration import (
    CandidateSelectModal,
    CollaborationConfirmModal,
    RoomCreateModal,
    RoomRoundModal,
    SessionOpenModal,
    _patch_is_binary,
    render_candidate_details,
    render_candidate_diff,
    render_candidate_output,
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
        target = str(
            request.get("workflow_id")
            or request.get("room_id")
            or request.get("run_id")
            or ""
        )
        detail = f"{op} {target[:8]}".rstrip()
    return f"{row['job_id'][:8]} {row.get('status')} {detail}"


_DIFF_DISPLAY_BYTES = 256 * 1024
# result.json must be parsed whole (it is one JSON document); the display
# itself truncates output at 100k, so this cap only guards against a
# pathological file.
_RESULT_LOAD_BYTES = 8 * 1024 * 1024
_CANDIDATE_OUTPUT_BYTES = 100_000
#: Candidate views rendered from the Rooms evidence snapshot rather than from
#: a per-candidate artifact read.
_EVIDENCE_VIEWS = ("compare", "details")
# Polling is best-effort by invariant (the _refresh_board precedent): a
# mid-session store failure degrades that surface and keeps the last snapshot,
# never killing the app. RoomStore leaks raw sqlite3.Error from bare SQL and
# ValueError (json.JSONDecodeError) / KeyError / TypeError / AttributeError
# from corrupt members_json / payload_json rows (null or a scalar decodes
# fine, then fails on attribute/key access or in the renderer);
# RuntimeError covers Path.resolve() on a symlink loop in the store paths AND
# RecursionError (its subclass) from pathologically-nested JSON; the session
# store reads run-dir files.
_ROOM_POLL_ERRORS = (
    RoomError,
    sqlite3.Error,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    RuntimeError,
)
_SESSION_POLL_ERRORS = (
    IsolatedSessionError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    RuntimeError,
)
# The jobs poll reads a state directory and, when a manager released its lock,
# rewrites job state — so it can fail the same ways, plus KeyError/TypeError on a
# foreign or half-written spec.json. Unguarded, any of these escapes the interval
# callback and kills the whole app.
_JOB_POLL_ERRORS = (
    JobError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
    AttributeError,
    RuntimeError,
)


def _artifact_empty_text(skipped: list[SkippedRun]) -> str:
    if not skipped:
        return "No run artifacts. Session checkpoints and run winners land here."
    noun = "directory" if len(skipped) == 1 else "directories"
    lines = [
        f"No readable run artifacts. {len(skipped)} run {noun} could not be read:",
        *(f"  {printable_name(entry.name)} — {entry.reason}" for entry in skipped),
        "",
        "Inspect with: syncmill runs show <run-id>. Nothing was changed.",
    ]
    return "\n".join(lines)


def _artifact_label(run_id: str, manifest: dict) -> str:
    """One Artifacts-pane row label: id, state, agent, and origin (room-bound
    runs are marked — they are applied/reconciled via the room, not here)."""
    origin = "room" if is_room_bound(manifest) else (manifest.get("strategy") or "?")
    agent = manifest.get("agent_id") or "?"
    return f"{run_id[:8]} {manifest.get('state', '?'):9.9} {agent:8.8} {origin}"


def _read_artifact_result(run_id: str, *, store: ArtifactStore) -> dict | None:
    """The run's result.json sidecar (status/output/error/exit/duration).

    ``None`` means MISSING; an oversized or corrupt file returns a sentinel
    status instead, so the view never mislabels a recorded result as
    "no result recorded". Degrades, never raises.
    """
    try:
        raw = store.read_result(run_id, limit=_RESULT_LOAD_BYTES + 1, missing_ok=True)
    except FileNotFoundError:
        return None
    except (OSError, ArtifactError) as exc:
        # A directory, permission failure, or I/O error is NOT "missing".
        return {"status": f"(result unreadable: {exc.__class__.__name__})"}
    if raw is None:
        return None
    if len(raw) > _RESULT_LOAD_BYTES:
        return {"status": f"(result too large to display: >{_RESULT_LOAD_BYTES} bytes)"}
    try:
        value = json.loads(raw)
    except ValueError:
        return {"status": "(result unreadable: corrupt result.json)"}
    if not isinstance(value, dict):
        return {"status": "(result unreadable: unexpected shape)"}
    return value


def render_artifact(manifest: dict, result: dict | None) -> str:
    """The Artifacts summary view: manifest metadata first, then the recorded
    result with error before output (review reads failures first)."""
    plan = manifest.get("plan") or {}
    origin = plan.get("origin") if isinstance(plan, dict) else None
    lines = [
        f"Run: {manifest.get('run_id')}",
        f"State: {manifest.get('state')}",
        f"Strategy: {manifest.get('strategy')} | agent: {manifest.get('agent_id')}"
        + (f" | origin: {origin}" if origin else ""),
        f"Created: {manifest.get('created_at')}",
        f"Patch: {manifest.get('patch_bytes')} bytes {manifest.get('patch_sha256')}",
        f"Files: {', '.join(manifest.get('files_changed') or []) or '-'}",
    ]
    if manifest.get("recovery_reason"):
        lines.append(f"Recovery: {manifest['recovery_reason']}")
    lines.append("")
    if result is None:
        lines.append("(no result recorded)")
    elif set(result) == {"status"}:
        # The reader's oversized/corrupt sentinel — status only, no fake
        # exit/duration fields.
        lines.append(f"Result: {result['status']}")
    else:
        lines.append(
            f"Result: {result.get('status')} | exit: {result.get('exit_code')} | "
            f"{result.get('duration_seconds')}s"
        )
        if result.get("error"):
            lines.append(f"Error: {result['error']}")
        output = str(result.get("output") or "")
        if len(output) > 100_000:
            output = (
                output[:50_000]
                + "\n\n[... display truncated ...]\n\n"
                + output[-50_000:]
            )
        if output:
            lines.extend(["", output])
    return "\n".join(lines)


TAB_CHOICES = (
    "auto",
    "board",
    "runs",
    "reports",
    "teams",
    "status",
    "sessions",
    "rooms",
)

# The tab whose list gets focus after a switch. Status has no list — nothing
# to focus there.
_PRIMARY_WIDGETS = {
    "board-tab": "#board-table",
    "runs-tab": "#job-list",
    "reports-tab": "#report-list",
    "teams-tab": "#team-list",
    "sessions-tab": "#session-list",
    "rooms-tab": "#room-list",
}

_ACTION_BAR_COMMON = "1-7 tabs | r refresh | ? help | q quit"

_ACTION_BARS = {
    "board-tab": (
        "a add | i import | d decisions | e edit | u unblock | h clear-human | "
        "s stats | x run item | g run queue | f filter"
    ),
    "runs-tab": "v artifacts | f follow | k stop | K force | R recover",
    "runs-tab:artifacts-pane": "v operations | d diff/summary | g apply | k discard | R recover",
    "reports-tab": "",
    "teams-tab": "a add | e edit | u default",
    "status-tab": "r probes agents+memory",
    "sessions-tab": "a open | x attach | c checkpoint | k close",
    "rooms-tab": (
        "a create | x round | t terminal | d/o/m/c diff/output/details/compare | "
        "e select | g apply | R recover | k close"
    ),
}


def _action_bar_text(
    tab: str, *, runs_pane: str = "operations-pane", available: bool = True
) -> str:
    """The contextual action bar for one tab — only that tab's live keys.

    The global Footer showed every binding with one static caption regardless
    of the active tab (misleading: `f` is filter on Board but follow on Runs),
    so the bar is derived per tab (and, for Runs, per sub-pane) instead. The
    per-tab keys and the common keys are separate lines and the bar wraps
    (height auto), so a narrow terminal never clips `? help | q quit`. When
    the tab's store is unavailable only the common keys are live — the bar
    must not advertise actions that silently do nothing.
    """
    if not available:
        return _ACTION_BAR_COMMON
    key = tab
    if tab == "runs-tab" and runs_pane == "artifacts-pane":
        key = "runs-tab:artifacts-pane"
    keys = _ACTION_BARS.get(key, "")
    return f"{keys}\n{_ACTION_BAR_COMMON}" if keys else _ACTION_BAR_COMMON


def _resolve_initial_tab(
    requested: str, *, rooms_ok: bool, board_ok: bool, jobs_ok: bool
) -> str:
    """Map the CLI `--tab` value to a TabPane id.

    An explicit tab is always honored — an unavailable choice shows that tab's
    existing degraded/limited banner. `auto` lands on the first surface that is
    enabled AND actually opened (Rooms → Board → Runs), else Status.
    """
    if requested != "auto":
        return f"{requested}-tab"
    if rooms_ok:
        return "rooms-tab"
    if board_ok:
        return "board-tab"
    if jobs_ok:
        return "runs-tab"
    return "status-tab"


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


def build_room_evidence(
    store_root: Path, output_max_bytes: int, repo_root: Path, room_id: str, now: float
) -> tuple[RoomComparison, int, frozenset]:
    """Build one room's evidence snapshot. Runs in a thread worker: SQLite
    connections are thread-affine, so it opens its own store and closes it.
    Returns the comparison plus the room revision and candidate ids read
    BEFORE the build, so anything that changes during it counts as a change."""
    store = RoomStore(store_root, output_max_bytes)
    try:
        room = store.get_room(room_id)
        candidate_ids = frozenset(c["id"] for c in store.list_candidates(room_id))
        comparison = build_room_comparison(
            store,
            room_id,
            all_rounds=True,
            source_check=lambda: repository_fingerprint(repo_root),
            now=now,
        )
    finally:
        store.close()
    return comparison, int(room["revision"]), candidate_ids


def select_room_candidate(
    store_root: Path,
    output_max_bytes: int,
    room_id: str,
    candidate_id: str,
    *,
    expected_revision: int,
    expected_patch_sha256: str,
    rationale: str | None,
) -> None:
    """Verify and select a candidate. Runs in a thread worker — it streams the
    whole artifact — so it opens its own store (SQLite is thread-affine)."""
    store = RoomStore(store_root, output_max_bytes)
    try:
        store.select_candidate(
            room_id,
            candidate_id,
            expected_revision=expected_revision,
            expected_patch_sha256=expected_patch_sha256,
            rationale=rationale,
            surface="tui",
        )
    finally:
        store.close()


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
        ("d", "context_d", "Diff/Decisions"),
        ("o", "candidate_output", "Output"),
        ("m", "candidate_details", "Details"),
        ("v", "runs_view", "Runs view"),
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
    #runs-subtabs { height: 1fr; }
    .split { height: 1fr; }
    .left { width: 3fr; }
    .right { width: 2fr; border-left: solid $primary; padding: 0 1; }
    .status-line { height: 2; padding: 0 1; background: $panel; }
    #action-bar { height: auto; padding: 0 1; background: $panel; }
    .form-dialog {
        width: 72; height: auto; border: thick $primary;
        padding: 1 2; background: $surface;
    }
    .wide-dialog {
        width: 90%; max-width: 100; height: 85%; border: thick $primary;
        padding: 1 2; background: $surface;
    }
    .form-buttons { height: auto; align-horizontal: right; }
    #selection-body { height: 1fr; }
    #selection-rationale { height: 6; }
    .form-buttons Button { margin-left: 2; }
    _FormModal { align: center middle; }
    .warning { color: $warning; margin: 1 0; }
    #board-table, #job-list, #artifact-list, #report-list, #team-list, #session-list, #room-list { height: 1fr; }
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
        initial_tab: str = "auto",
    ):
        super().__init__()
        self._config = config
        self._board = board
        self._board_error = board_error
        self._toml_path = toml_path
        self._refresh_seconds = max(MIN_REFRESH_SECONDS, refresh_seconds)
        self._initial_tab = initial_tab
        # Set at the end of on_mount; the TabActivated handler ignores events
        # until then (the initial activation must not double-refresh), and
        # _handled_tab dedupes re-activations of the already-handled tab.
        self._ui_ready = False
        self._handled_tab: str | None = None
        self._handled_runs_pane = "operations-pane"
        self.action_bar_text = ""
        self.artifact_detail_text = ""
        self._selected_run_id: str | None = None
        self._artifact_view = "summary"
        self.candidate_detail_text = ""
        # App-owned seam mirroring the "#room-detail" Static — tests assert on
        # this, never on widget internals (the candidate_detail_text pattern).
        self.room_detail_text = ""
        # None → per-candidate default in _show_candidate: Diff for a
        # completed candidate, Output for a failed/timeout one.
        self._candidate_view: str | None = None
        self._diff_pages = 1
        self._diff_truncated = False
        self._candidate_detail_key: tuple | None = None
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
        self._checkpoint_running = False
        self._job_log_signature: tuple[tuple[int, int, int] | None, ...] | None = None
        self._job_log_dir: str | None = None
        self._memory_status = "(press r to probe memory)"
        self._jobs: JobStore | None = None
        self._jobs_error: str | None = None
        self._session_store: IsolatedSessionStore | None = None
        self._rooms: RoomCoordinator | None = None
        self._collaboration_error: str | None = None
        self._artifacts: ArtifactStore | None = None
        self._artifacts_error: str | None = None
        # Artifacts listing runs in a thread worker (#168): one in flight at a
        # time, results tagged with the generation that started them so a
        # late result can never overwrite a newer one.
        self._artifact_listing_generation = 0
        self._artifact_listing_inflight = False
        # A refresh or invalidation that arrived while a listing was running:
        # coalesced into ONE rerun when that listing's thread reports back.
        self._artifact_listing_dirty = False
        # Rooms evidence snapshot (#172): one RoomComparison of every round of
        # one room, built only on request (c, first m, r in those views) in a
        # thread worker with its own RoomStore connection, and shown with its
        # build time — never refreshed by polling, never presented as live.
        self._evidence: dict | None = None
        self._evidence_error: tuple[str, str] | None = None
        self._evidence_generation = 0
        # The build running now and the one queued behind it, each bound to
        # the (room id, store root) it was requested for. No boolean flags:
        # "building" on screen is derived from these, so a view can never sit
        # on "building" with no worker behind it.
        self._evidence_inflight: tuple[str, str, int] | None = None
        #: (room, candidate, store root) of the selection a worker is verifying.
        self._select_inflight: tuple[str, str, str] | None = None
        self._evidence_pending: tuple[str, str] | None = None
        # Bumped on every accepted build result (success or failure), so an
        # open Compare/Details re-renders even when nothing else changed.
        self._evidence_build_id = 0
        # The store the snapshot (and any build in flight) belongs to, tracked
        # apart from the snapshot so a same-root rebind during the FIRST build
        # does not retire it.
        self._evidence_store_root: str | None = None
        self._runtime_config: SyncmillConfig | None = None
        # Content signatures per widget id (lists + the board table) and last
        # texts per detail Static: an unchanged poll tick must not rebuild a
        # widget, so highlight, scroll, and focus survive polling.
        self._option_signatures: dict[str, tuple] = {}
        self._static_texts: dict[str, str] = {}
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
        self._session_store = None
        self._rooms = None
        self._artifacts = None
        # A listing still running against the previous store must not land
        # in the new store's view: retire its generation (#168).
        self._invalidate_artifact_listing()
        self._runtime_config = None
        # The stores may now address different content — drop the rebuild-skip
        # caches so the next refresh renders from scratch. That includes the
        # candidate-detail key: after an unavailable interval blanked the
        # pane, a reconnect to the same store must re-render, not hit the
        # stale key and stay blank.
        self._option_signatures.clear()
        self._static_texts.clear()
        self._candidate_detail_key = None
        try:
            config_root = git_root(self._toml_path.parent.resolve())
            root = self._project_root()
            runtime_config = self._config.model_copy(deep=True)
            runtime_config.repo_root = str(root)
            resolve_project_state_paths(runtime_config, config_root)
        except Exception as exc:
            self._collaboration_error = str(exc)
            self._artifacts_error = str(exc)
            self._reconcile_evidence_store()
            return
        self._runtime_config = runtime_config
        # Bind the three stores INDEPENDENTLY: a corrupt/locked Rooms SQLite
        # store must not disable the filesystem-only Artifacts surface, and
        # vice versa.
        errors: list[str] = []
        try:
            self._session_store = IsolatedSessionStore(runtime_config, root)
        except Exception as exc:
            errors.append(str(exc))
        try:
            self._rooms = (
                RoomCoordinator(runtime_config, root)
                if runtime_config.rooms.enabled
                else None
            )
        except Exception as exc:
            self._rooms = None
            errors.append(str(exc))
        self._collaboration_error = "; ".join(errors) or None
        self._reconcile_evidence_store()
        try:
            # The session store's exact identity recipe, so a checkpoint's run
            # is visible here; also what `syncmill runs list` addresses.
            self._artifacts = ArtifactStore(
                Path(runtime_config.runs.root),
                project_id(root, runtime_config.project.id),
            )
            self._artifacts_error = None
        except Exception as exc:
            self._artifacts = None
            self._artifacts_error = str(exc)

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
        except (JobError, ProjectError, OSError, RuntimeError, ValueError) as exc:
            # OSError/RuntimeError cover an unresolvable configured repo_root
            # (symlink loop, dead HOME) surfacing from Path.resolve/expanduser
            # — a valid-toml reload must degrade Runs to limited mode with the
            # error, never propagate through Textual and kill the TUI.
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
            self._option_signatures.pop("job-list", None)
            self._static_texts.pop("#job-status", None)

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
                with TabbedContent(id="runs-subtabs"):
                    with TabPane("Operations", id="operations-pane"):
                        with Horizontal(classes="split"):
                            yield OptionList(id="job-list", classes="left")
                            yield Log(id="job-log", classes="right", highlight=False)
                        yield Static("", id="job-status", classes="status-line")
                    with TabPane("Artifacts", id="artifacts-pane"):
                        with Horizontal(classes="split"):
                            yield OptionList(id="artifact-list", classes="left")
                            with VerticalScroll(classes="right"):
                                # Raw agent output and diffs — content like
                                # `[/oops]` must never hit the markup parser.
                                yield Static("", id="artifact-detail", markup=False)
                        yield Static("", id="artifact-status", classes="status-line")
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
                        # Carries the user-authored room prompt verbatim, so
                        # markup must stay OFF: a prompt containing `[/]` would
                        # otherwise raise MarkupError and kill the poll.
                        yield Static("", id="room-detail", markup=False)
                        # Raw candidate output and diffs (see artifact-detail).
                        yield Static("", id="candidate-detail", markup=False)
                yield Static("", id="room-status", classes="status-line")
        yield Static("", id="action-bar")

    def on_mount(self) -> None:
        table = self.query_one("#board-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("id", "state", "goal", "task", "owner", "flags")
        initial = _resolve_initial_tab(
            self._initial_tab,
            rooms_ok=self._rooms is not None,
            board_ok=self._board is not None,
            jobs_ok=self._jobs is not None,
        )
        self.query_one("#tabs", TabbedContent).active = initial
        self._refresh_all()
        self._update_action_bar()
        self._focus_primary()
        self.set_interval(self._refresh_seconds, self._poll)
        self._handled_tab = initial
        self._ui_ready = True

    def on_unmount(self) -> None:
        if self._board is not None:
            # The CLI also closes its latest connection; this one may have been
            # replaced by retry, and Board.close() is intentionally idempotent.
            self._board.close()
        if self._rooms is not None:
            self._rooms.close()

    def _active(self) -> str:
        return self.query_one("#tabs", TabbedContent).active

    def _runs_pane(self) -> str:
        return self.query_one("#runs-subtabs", TabbedContent).active

    def action_tab(self, tab: str) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active == tab:
            self._refresh_current()
            return
        # The TabActivated handler refreshes, updates the bar, and focuses —
        # it also covers mouse clicks on tab headers, which bypass this action.
        tabs.active = tab

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        if not self._ui_ready:
            return
        if event.tabbed_content.id == "runs-subtabs":
            pane = event.pane.id
            if pane is None or pane == self._handled_runs_pane:
                return
            # Track the pane even for background switches (checkpoint
            # navigation flips the sub-tab before the main tab switch, which
            # then does the refresh/focus) so a later `v` is never deduped
            # against a stale value.
            self._handled_runs_pane = pane
            if self._active() != "runs-tab":
                return
            self._update_action_bar()
            self._refresh_current()
            self._focus_primary()
            return
        if event.tabbed_content.id != "tabs":
            return
        tab = event.pane.id
        # Skip already-handled tabs AND stale queued events (the initial
        # first-pane activation can be delivered after on_mount already moved
        # to the resolved initial tab).
        if tab is None or tab == self._handled_tab or tab != self._active():
            return
        self._handled_tab = tab
        self._refresh_current()
        self._update_action_bar()
        self._focus_primary()

    def _tab_available(self, tab: str) -> bool:
        if tab == "board-tab":
            return self._board is not None
        if tab == "runs-tab":
            store = (
                self._artifacts
                if self._runs_pane() == "artifacts-pane"
                else self._jobs
            )
            return store is not None
        if tab == "sessions-tab":
            return self._session_store is not None
        if tab == "rooms-tab":
            return self._rooms is not None
        return True

    def _update_action_bar(self) -> None:
        tab = self._active()
        self.action_bar_text = _action_bar_text(
            tab, runs_pane=self._runs_pane(), available=self._tab_available(tab)
        )
        self.query_one("#action-bar", Static).update(self.action_bar_text)

    def _focus_primary(self) -> None:
        """Auto-focus the active tab's primary list. Called only on tab switch
        and at mount — never from _poll/_refresh_* (polling must not steal
        focus)."""
        active = self._active()
        if active == "runs-tab":
            selector = (
                "#artifact-list"
                if self._runs_pane() == "artifacts-pane"
                else "#job-list"
            )
        else:
            selector = _PRIMARY_WIDGETS.get(active)
        if selector:
            self.query_one(selector).focus()
        else:
            # Status has no focusable list. Leaving focus on a widget inside
            # the now-hidden pane makes Textual reactivate that pane (its
            # TabPane.Focused handling), silently undoing the switch.
            self.screen.set_focus(None)

    def action_runs_view(self) -> None:
        if self._active() != "runs-tab":
            return
        subtabs = self.query_one("#runs-subtabs", TabbedContent)
        subtabs.active = (
            "artifacts-pane"
            if subtabs.active == "operations-pane"
            else "operations-pane"
        )

    def _sync_options(
        self,
        options: OptionList,
        entries: list[tuple[str, str]],
        selected_id: str | None,
    ) -> None:
        """Rebuild an OptionList only when its content changed.

        An unchanged poll tick returns without touching the widget, so
        highlight, scroll, and focus survive polling. A rebuild restores the
        logical selection by id and clamps the previous scroll offset.
        """
        signature = tuple(entries)
        widget_id = options.id or ""
        if self._option_signatures.get(widget_id) == signature:
            return
        scroll_y = options.scroll_offset.y
        options.clear_options()
        for label, option_id in entries:
            options.add_option(Option(label, id=option_id))
        self._option_signatures[widget_id] = signature
        if not entries:
            return
        ids = [option_id for _, option_id in entries]
        options.highlighted = ids.index(selected_id) if selected_id in ids else 0
        options.scroll_to(y=min(scroll_y, max(0, len(entries) - 1)), animate=False)

    def _update_static(self, selector: str, text: str) -> None:
        """update() a detail Static only when its text changed, so a poll tick
        never resets the surrounding pane's scroll position."""
        if self._static_texts.get(selector) == text:
            return
        self._static_texts[selector] = text
        self.query_one(selector, Static).update(text)

    def action_quit(self) -> None:
        if self._jobs is not None and self._jobs.active_job() is not None:
            self.notify("TUI closed; the worker continues in the background")
        self.exit()

    def action_help(self) -> None:
        self.notify(
            "1-7 tabs; Sessions a/x/c/k; Rooms a/x/t/d/o/m/c/e/g/R/k; "
            "Board a/i/d/e/u/h/s/x/g; Runs v/f/k/K/R (Artifacts: d/g/k); "
            "q detaches"
        )

    def _poll(self) -> None:
        active = self._active()
        if active == "board-tab":
            self._refresh_board()
        elif active == "runs-tab":
            self._refresh_runs_pane()
        elif active == "sessions-tab":
            self._refresh_sessions()
        elif active == "rooms-tab":
            self._refresh_rooms()

    def _refresh_runs_pane(self) -> None:
        """Refresh only the visible Runs sub-pane (Artifacts reads one
        manifest per run dir — don't pay that while Operations is showing)."""
        if self._runs_pane() == "artifacts-pane":
            self._refresh_artifacts()
        else:
            self._refresh_jobs()

    def _refresh_all(self) -> None:
        self._refresh_board()
        self._refresh_runs_pane()
        self._refresh_reports()
        self._refresh_teams()
        self._refresh_agents()
        self._refresh_sessions()
        self._refresh_rooms()

    def action_refresh_current(self) -> None:
        """The `r` key. Only an explicit refresh rebuilds a Rooms evidence
        snapshot; tab activation reuses the same refresh without it."""
        self._refresh_current(explicit=True)

    def _refresh_current(self, *, explicit: bool = False) -> None:
        active = self._active()
        if active == "board-tab":
            if self._board is None:
                self._retry_board()
            self._refresh_board()
        elif active == "runs-tab":
            self._refresh_runs_pane()
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
            # After the refresh: a rebound store may have changed the room on
            # screen, and the request must be for that room.
            if explicit and self._candidate_view in _EVIDENCE_VIEWS:
                self._request_evidence()
        else:
            # The guide documents Status `r` as re-reading configuration (the
            # teams/rooms refresh behavior): without the reload the displayed
            # priority/enabled/timeouts stay cached and can disagree with the
            # probe. Best-effort like teams — a broken toml keeps the cached
            # view plus the error toast.
            self._reload_config()
            self._refresh_agents()
            self._start_memory_probe()
        # Retries/rebinds above can change store availability — the bar's
        # advertised keys must track it, not just tab switches.
        if self._ui_ready:
            self._update_action_bar()

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
            self._option_signatures.pop("board-table", None)
            self._update_static("#board-detail", error)
            self._update_static("#board-status", "LIMITED MODE | Board unavailable")
            return
        assert self._board is not None
        try:
            items = self._board.list(state=self._filter)
            stats = self._board.stats()
        except BoardError as exc:
            self._board_error = str(exc)
            self._update_static("#board-status", f"DEGRADED: {exc}")
            return
        rows = []
        for item in items:
            flags = "H!" if item.human_required else ""
            if item.depends_on:
                flags += f" deps={len(item.depends_on)}"
            rows.append(
                (
                    item.id,
                    (
                        item.id[:8], item.state.value, item.goal or "-",
                        item.task[:50], item.owner_agent or "-", flags.strip(),
                    ),
                )
            )
        signature = tuple(rows)
        if self._option_signatures.get("board-table") != signature:
            # Content changed: rebuild, then restore the cursor by item id and
            # clamp the previous scroll (an unchanged tick never gets here, so
            # cursor/scroll/focus survive polling).
            scroll_y = table.scroll_offset.y
            table.clear()
            for item_id, cells in rows:
                table.add_row(*cells, key=item_id)
            self._option_signatures["board-table"] = signature
            if rows:
                index = next(
                    (
                        i
                        for i, (item_id, _) in enumerate(rows)
                        if item_id == self._selected_item_id
                    ),
                    0,
                )
                table.move_cursor(row=index)
                table.scroll_to(y=min(scroll_y, max(0, len(rows) - 1)), animate=False)
        if self._selected_item_id and self._board.get(self._selected_item_id):
            self._show_item(self._selected_item_id)
        self._update_static(
            "#board-status",
            f"items:{stats.total} | filter:{self._filter or 'all'} | "
            f"decisions:{len(self._board.active_decisions())}",
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
        self._update_static("#board-detail", text)

    def _selected_item(self) -> WorkItem | None:
        if self._board is None or self._selected_item_id is None:
            return None
        return self._board.get(self._selected_item_id)

    def action_context_f(self) -> None:
        if self._active() == "runs-tab":
            if self._runs_pane() != "operations-pane":
                return
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
            if self._session_store is None:
                self.notify(
                    self._collaboration_error or "Sessions are unavailable",
                    severity="warning",
                )
                return
            if not self._config.agent_priority:
                self.notify("no interactive agents are configured", severity="warning")
                return
            self.push_screen(SessionOpenModal(self._config), self._apply_session_open)
        elif active == "rooms-tab":
            if self._rooms is None:
                self.notify(
                    self._collaboration_error or "Rooms are unavailable",
                    severity="warning",
                )
                return
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

    def action_context_d(self) -> None:
        active = self._active()
        if active == "board-tab":
            self.action_decisions()
        elif active == "runs-tab" and self._runs_pane() == "artifacts-pane":
            if not self._selected_run_id:
                return
            self._artifact_view = "diff" if self._artifact_view != "diff" else "summary"
            self._show_artifact(self._selected_run_id)
        elif active == "rooms-tab":
            if not self._selected_candidate_id:
                return
            if self._candidate_view == "diff":
                # Already on Diff: page in more of a truncated patch.
                if not self._diff_truncated:
                    return
                pages = self._diff_pages + 1
            else:
                pages = 1
            # Proposed view state commits only after staging succeeds — a
            # failed artifact read must not advance pages/view under the
            # still-visible old pane.
            self._show_candidate_as(self._selected_candidate_id, "diff", pages)

    def action_candidate_output(self) -> None:
        self._set_candidate_view("output")

    def action_candidate_details(self) -> None:
        if self._active() != "rooms-tab" or not self._selected_candidate_id:
            return
        if self._room_evidence() is None:
            self._request_evidence()
        self._set_candidate_view("details")

    def _action_room_compare(self) -> None:
        # Works with no candidate selected: a round that kept no candidates
        # is exactly when its missing-member evidence matters.
        if self._rooms is None or not self._selected_room_id:
            return
        self._request_evidence()
        self._show_candidate_as(self._selected_candidate_id, "compare", self._diff_pages)

    def _set_candidate_view(self, view: str) -> None:
        if self._active() != "rooms-tab" or not self._selected_candidate_id:
            return
        self._show_candidate_as(self._selected_candidate_id, view, self._diff_pages)

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
        if self._active() == "runs-tab":
            if self._runs_pane() == "artifacts-pane":
                self._confirm_artifact_apply()
            return
        if self._active() != "board-tab" or self._board is None:
            return
        self.push_screen(JobModal(self._config), lambda fields: self._launch_job(fields, None))

    def _confirm_artifact_apply(self) -> None:
        if self._artifacts is None or not self._selected_run_id:
            self.notify("select a run artifact", severity="warning")
            return
        try:
            artifact = self._artifacts.load(self._selected_run_id)
        except (ArtifactError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return
        manifest = artifact.manifest
        if is_room_bound(manifest):
            self.notify(
                "room-bound run — apply it from the Rooms tab (g)", severity="warning"
            )
            return
        if manifest.get("state") != "pending":
            self.notify(
                f"only a pending run can be applied (state: {manifest.get('state')})",
                severity="warning",
            )
            return
        # _project_root shells out to git twice; a repository that vanished under
        # a long-running TUI raises ProjectError, and an unguarded raise inside an
        # event handler takes the app down with it.
        try:
            target = self._project_root()
        except ProjectError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            CollaborationConfirmModal(
                "Apply run artifact to source",
                f"Run: {artifact.run_id}\nState: {manifest.get('state')}\n"
                f"Patch digest: {manifest.get('patch_sha256')}\n"
                f"Target: {target}\n\n"
                "Source fingerprint and patch applicability will be checked again.\n"
                "The detached worker will continue if the TUI closes.",
                "Apply",
            ),
            lambda confirmed, run_id=artifact.run_id: self._apply_artifact_apply(
                confirmed, run_id
            ),
        )

    def _apply_artifact_apply(self, confirmed: dict | bool | None, run_id: str) -> None:
        if not confirmed:
            return
        try:
            if self._jobs is None:
                raise JobError(self._jobs_error or "TUI job registry is unavailable")
            if self._runtime_config is None:
                raise JobError(
                    self._artifacts_error or "resolved project config is unavailable"
                )
            # Orchestration never runs on Textual's event loop: the apply runs
            # in a durable detached JobStore worker, pinned to the resolved
            # config captured at rebind time (the room-apply pattern; the
            # worker re-verifies state/fingerprint/digest before mutating).
            spec = ArtifactApplySpec(
                run_id=run_id, verify=bool(self._config.gate.commands)
            )
            row = self._jobs.launch(spec, resolved_config=self._runtime_config)
        except (JobError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"detached artifact-apply job {row['job_id'][:8]}")
        if row.get("job_dir"):
            self._selected_job_dir = row["job_dir"]
        self.query_one("#runs-subtabs", TabbedContent).active = "operations-pane"

    def _confirm_artifact_discard(self) -> None:
        if self._artifacts is None or not self._selected_run_id:
            self.notify("select a run artifact", severity="warning")
            return
        run_id = self._selected_run_id
        self.push_screen(
            CollaborationConfirmModal(
                "Discard run artifact",
                f"Run: {run_id}\n\n"
                "Marks the pending run as intentionally discarded; the patch "
                "stays on disk until pruned.",
                "Discard",
            ),
            lambda confirmed, run_id=run_id: self._apply_artifact_discard(
                confirmed, run_id
            ),
        )

    def _apply_artifact_discard(
        self, confirmed: dict | bool | None, run_id: str
    ) -> None:
        if not confirmed or self._artifacts is None:
            return
        store = self._artifacts

        def work() -> None:
            # discard takes the BLOCKING repository apply lock, which a live
            # detached apply may hold for its whole run — never block the
            # event loop on it. ArtifactStore is file/flock-based (no sqlite
            # thread affinity).
            try:
                store.discard(run_id)
            except (ArtifactError, OSError) as exc:
                self.call_from_thread(self.notify, str(exc), severity="error")
                return
            self.call_from_thread(self._after_artifact_discard, run_id)

        self.run_worker(work, thread=True, group="artifact-discard", exclusive=True)

    def _after_artifact_discard(self, run_id: str) -> None:
        self.notify(f"discarded run {run_id[:8]}")
        # A listing started before the discard would show the old state.
        self._invalidate_artifact_listing()
        if self._active() == "runs-tab" and self._runs_pane() == "artifacts-pane":
            self._refresh_artifacts()

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
        self.query_one("#runs-subtabs", TabbedContent).active = "operations-pane"
        self.action_tab("runs-tab")

    def _refresh_jobs(self) -> None:
        options = self.query_one("#job-list", OptionList)
        if self._jobs is None:
            self._sync_options(options, [], None)
            self.query_one("#job-log", Log).clear()
            self._update_static(
                "#job-status",
                f"DEGRADED | Runs unavailable: {self._jobs_error or 'unknown error'}",
            )
            return
        # Best-effort like the room and session polls. The guard covers reading
        # and interpreting the store — rows come from files this process does not
        # own, so labelling a malformed or foreign row raises as readily as
        # listing it — but stops there: widget updates below are OUR code, and a
        # bug in them must surface as a crash rather than be reported forever as
        # a degraded store.
        try:
            rows = self._jobs.list_jobs()
            if rows:
                # Preserve an existing valid selection (so a user can inspect or
                # stop ANY job among several active targets); only auto-select an
                # active job when there is no valid current selection.
                highlighted = self._selected_job_dir
                if highlighted not in {row["job_dir"] for row in rows}:
                    active = self._jobs.active_job()
                    highlighted = active["job_dir"] if active else None
                target = next(
                    (i for i, row in enumerate(rows) if row["job_dir"] == highlighted),
                    0,
                )
                selected = rows[target]
                entries = [(_job_label(row), row["job_dir"]) for row in rows]
        except _JOB_POLL_ERRORS as exc:
            self._update_static("#job-status", f"DEGRADED: {exc}")
            return
        if not rows:
            self._sync_options(options, [], None)
            self._update_static("#job-status", "no TUI jobs")
            self.query_one("#job-log", Log).clear()
            return
        self._refreshing_jobs = True
        try:
            self._sync_options(options, entries, selected["job_dir"])
        finally:
            self._refreshing_jobs = False
        self._selected_job_dir = selected["job_dir"]
        # _show_job reads the job's log and receipt off disk, so it keeps a guard
        # of its own for the same reason the block above has one.
        try:
            self._show_job(selected)
        except _JOB_POLL_ERRORS as exc:
            self._update_static("#job-status", f"DEGRADED: {exc}")

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_list.id == "job-list":
            if self._refreshing_jobs or self._jobs is None:
                return
            job_dir = str(event.option.id)
            if job_dir == self._selected_job_dir:
                # A rebuild's re-highlight is DELIVERED after _refreshing_jobs
                # is already reset; _refresh_jobs showed this row itself, so
                # re-reading list_jobs here would double every rebuild's I/O.
                return
            self._selected_job_dir = job_dir
            # Guarded like the poll: the state directory can disappear between
            # the refresh that drew this list and the keypress that selects a
            # row, and an arrow key must not be able to end the session.
            try:
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
            except _JOB_POLL_ERRORS as exc:
                self._update_static("#job-status", f"DEGRADED: {exc}")
        elif event.option_list.id == "artifact-list":
            run_id = str(event.option.id)
            if run_id != self._selected_run_id:
                self._artifact_view = "summary"
            self._selected_run_id = run_id
            self._show_artifact(run_id)
        elif event.option_list.id == "report-list":
            self._selected_report_id = str(event.option.id)
            self._show_report(self._selected_report_id)
        elif event.option_list.id == "team-list":
            self._selected_team = str(event.option.id)
            self._show_team(self._selected_team)
        elif event.option_list.id == "session-list":
            session_id = str(event.option.id)
            if session_id == self._selected_session_id:
                # A rebuild's re-highlight of the already-shown selection (the
                # job-list rule): the refresh just rendered this row from its
                # own snapshot — a second store read here could tear an atomic
                # refresh (or degrade a surface the refresh showed healthy).
                return
            # Commit the selection only after the detail rendered — a failed
            # stage must not leave actions targeting an invisible session.
            if self._show_session(session_id):
                self._selected_session_id = session_id
            else:
                self._restore_highlight(event.option_list, self._selected_session_id)
        elif event.option_list.id == "room-list":
            room_id = str(event.option.id)
            if room_id == self._selected_room_id:
                # Same rule: the refresh already rendered this room.
                return
            if self._rooms is None:
                self._selected_room_id = room_id
                return
            # A LOGICAL room change proposes a candidate reset — staged, and
            # committed (selection included) only after every read succeeds,
            # so a failed stage leaves selection matching the visible panes.
            try:
                staged = self._stage_room(room_id, reset_candidate=True)
            except _ROOM_POLL_ERRORS as exc:
                self._update_static("#room-status", f"DEGRADED: {exc}")
                self._restore_highlight(event.option_list, self._selected_room_id)
                return
            self._selected_room_id = room_id
            self._commit_room(staged)
        elif event.option_list.id == "candidate-list":
            candidate_id = str(event.option.id)
            if candidate_id == self._selected_candidate_id:
                # Same rule: the refresh already rendered this candidate.
                return
            if self._rooms is None:
                self._selected_candidate_id = candidate_id
                return
            # view=None/pages=1: the reset proposal for a new candidate —
            # except the snapshot views, which follow the cursor (moving
            # through a round's candidates in Compare is the point of it).
            view = (
                self._candidate_view
                if self._candidate_view in _EVIDENCE_VIEWS
                else None
            )
            try:
                staged = self._stage_candidate(candidate_id, view, 1)
            except _ROOM_POLL_ERRORS as exc:
                self._update_static("#room-status", f"DEGRADED: {exc}")
                self._restore_highlight(
                    event.option_list, self._selected_candidate_id
                )
                return
            self._selected_candidate_id = candidate_id
            self._commit_candidate(staged)

    def _restore_highlight(self, options: OptionList, option_id: str | None) -> None:
        """Move the highlight back to the previous selection after a failed
        stage; the resulting re-highlight event short-circuits on the
        same-id guard, so no second store read follows."""
        if option_id is not None:
            for index in range(options.option_count):
                if options.get_option_at_index(index).id == option_id:
                    options.highlighted = index
                    return
        # The previous selection was never set or its row is gone: clear the
        # highlight so the failed destination is not left looking selected
        # while the committed selection points elsewhere.
        options.highlighted = None

    def _show_job(self, row: dict) -> None:
        """Render one job row. Reading the row and its files can raise on data
        this process does not own; rendering is ours and must not be guarded, so
        every read happens up front and the widget work below cannot fail on
        anything but a bug of ours."""
        directory = Path(row["job_dir"])
        signature = _log_signature(directory)
        status_line = (
            f"{row.get('status')} | exit:{row.get('exit_code')} | cwd:{row.get('cwd')}"
        )
        unchanged = (
            self._job_log_dir == str(directory)
            and self._job_log_signature == signature
        )
        text = (
            None
            if unchanged
            else _read_log_tail(directory).decode("utf-8", errors="replace")
        )
        self._update_static("#job-status", status_line)
        if text is None:
            return
        log = self.query_one("#job-log", Log)
        log.clear()
        for line in text.splitlines() or [""]:
            log.write_line(line)
        if self._follow_log:
            log.scroll_end(animate=False)
        self._job_log_dir = str(directory)
        self._job_log_signature = signature

    def _refresh_artifacts(self) -> None:
        options = self.query_one("#artifact-list", OptionList)
        if self._artifacts is None:
            self._sync_options(options, [], None)
            self._update_static("#artifact-detail", "")
            self._update_static(
                "#artifact-status",
                "DEGRADED | Artifacts unavailable: "
                f"{self._artifacts_error or 'unknown error'}",
            )
            return
        if self._artifact_listing_inflight:
            # Single flight: never a second scan while a thread is running —
            # remember the request, the running listing's completion reruns
            # once (however many requests arrived meanwhile).
            self._artifact_listing_dirty = True
            return
        self._artifact_listing_generation += 1
        generation = self._artifact_listing_generation
        self._artifact_listing_inflight = True
        self._artifact_listing_dirty = False
        store = self._artifacts

        def work() -> None:
            # Off the event loop: list_detailed stats every run (and reads
            # only changed manifests), which must not block input on a big
            # store. Anything raised degrades the pane rather than killing
            # the app; the result goes back through call_from_thread, which
            # fails once the app is gone — then there is nothing to apply to.
            try:
                outcome = (generation, store.list_detailed(), None)
            except Exception as exc:  # noqa: BLE001 — degrade, never crash
                outcome = (generation, None, f"{exc}" or type(exc).__name__)
            try:
                self.call_from_thread(self._apply_artifact_listing, *outcome)
            except RuntimeError:
                pass

        # Not exclusive: a second worker is never started while one runs, and
        # exclusive's cancel could drop a not-yet-started worker, leaving the
        # in-flight flag set forever.
        self.run_worker(work, thread=True, group="artifact-listing")

    def _invalidate_artifact_listing(self) -> None:
        """Retire the running listing's result (it fails the generation
        check) and ask for a fresh one. The in-flight flag stays owned by the
        running thread: clearing it here would let a second scan start
        beside the first."""
        self._artifact_listing_generation += 1
        if self._artifact_listing_inflight:
            self._artifact_listing_dirty = True

    def _apply_artifact_listing(
        self, generation: int, listing: ArtifactListing | None, error: str | None
    ) -> None:
        """Completion of one listing thread, on the event loop: the only
        place the in-flight flag is released. A current result is rendered,
        a stale one dropped; then one coalesced rerun if requests arrived."""
        self._artifact_listing_inflight = False
        if generation == self._artifact_listing_generation:
            self._render_artifact_listing(listing, error)
        if self._artifact_listing_dirty:
            self._artifact_listing_dirty = False
            if self._active() == "runs-tab" and self._runs_pane() == "artifacts-pane":
                self._refresh_artifacts()

    def _render_artifact_listing(
        self, listing: ArtifactListing | None, error: str | None
    ) -> None:
        if listing is None:
            self._update_static(
                "#artifact-status", f"DEGRADED | Artifacts unavailable: {error}"
            )
            return
        options = self.query_one("#artifact-list", OptionList)
        artifacts, skipped = listing.artifacts, listing.skipped
        status = f"artifacts:{len(artifacts)}"
        if skipped:
            # Unreadable entries are reported, never hidden as a healthy empty
            # store (#170); they are not selectable, so the detail pane can
            # never land on one.
            status += f" | DEGRADED: {len(skipped)} unreadable"
        if not artifacts:
            self._selected_run_id = None
            self._sync_options(options, [], None)
            self._update_static("#artifact-detail", _artifact_empty_text(skipped))
            self._update_static("#artifact-status", status)
            return
        ids = [artifact.run_id for artifact in artifacts]
        if self._selected_run_id not in ids:
            self._selected_run_id = ids[0]
            self._artifact_view = "summary"
        entries = [
            (_artifact_label(artifact.run_id, artifact.manifest), artifact.run_id)
            for artifact in artifacts
        ]
        self._sync_options(options, entries, self._selected_run_id)
        self._update_static("#artifact-status", status)
        self._show_artifact(self._selected_run_id)

    def _show_artifact(self, run_id: str) -> None:
        if self._artifacts is None:
            return
        # Rendered fresh on every tick: a handful of reads for ONE run, and
        # no render-skip state that a transient error could get stuck in
        # (#168 deliberately caches only the N-run listing).
        try:
            artifact = self._artifacts.load(run_id)
        except (ArtifactError, OSError) as exc:
            self._update_static("#artifact-detail", str(exc))
            return
        if self._artifact_view == "diff":
            text = self._artifact_diff_text(run_id, artifact.manifest)
        else:
            text = render_artifact(
                artifact.manifest, _read_artifact_result(run_id, store=self._artifacts)
            )
        self.artifact_detail_text = text
        self._update_static("#artifact-detail", text)

    def _artifact_diff_text(self, run_id: str, manifest: dict) -> str:
        try:
            patch = self._artifacts.patch(run_id)
        except (ArtifactError, OSError) as exc:
            return f"diff unavailable: {exc}"
        head = patch[:_DIFF_DISPLAY_BYTES]
        lines = [
            f"Diff {run_id[:8]} — {len(patch)} bytes "
            f"{manifest.get('patch_sha256')}",
        ]
        # Scan the WHOLE patch (it is already in memory): a binary hunk after
        # a large textual diff must still earn the banner.
        if _patch_is_binary(patch):
            lines.append("[contains binary patch content]")
        lines.extend(["", head.decode("utf-8", errors="replace")])
        if len(patch) > _DIFF_DISPLAY_BYTES:
            lines.append(
                f"\n[... display truncated at {_DIFF_DISPLAY_BYTES} bytes — "
                f"`syncmill diff {run_id}` prints the full patch ...]"
            )
        return "\n".join(lines)

    def _refresh_sessions(self) -> None:
        options = self.query_one("#session-list", OptionList)
        if self._session_store is None:
            self._sync_options(options, [], None)
            self._update_static(
                "#session-detail", self._collaboration_error or "Sessions unavailable"
            )
            self._update_static("#session-status", "DEGRADED")
            return
        # Stage every store read, row access, AND the selected detail render
        # before touching a widget: a failure — even a record valid enough for
        # the list but malformed for render_session — keeps the whole last
        # snapshot and only flips the status to DEGRADED (the _refresh_board
        # invariant). List and detail commit together or not at all.
        try:
            values = self._session_store.list()
            ids = [value["session_id"] for value in values]
            entries = []
            for value in values:
                live = "live" if value.get("live") else "stopped"
                stale = " STALE" if value.get("source_stale") else ""
                entries.append(
                    (
                        f"{value['session_id'][:8]} {value['agent_id']} "
                        f"{value['state']} {live}{stale}",
                        value["session_id"],
                    )
                )
            selected = self._selected_session_id
            if selected not in ids:
                selected = ids[0] if ids else None
            detail = None
            if selected is not None:
                value = next(
                    (item for item in values if item["session_id"] == selected),
                    None,
                )
                detail = render_session(value) if value else "(session unavailable)"
        except _SESSION_POLL_ERRORS as exc:
            self._update_static("#session-status", f"DEGRADED: {exc}")
            return
        if not values:
            self._selected_session_id = None
            self._sync_options(options, [], None)
            self._update_static(
                "#session-detail", "No project-scoped sessions. Press a to open one."
            )
            self._update_static("#session-status", "sessions:0")
            return
        self._selected_session_id = selected
        self._sync_options(options, entries, selected)
        self._update_static("#session-detail", detail)
        self._update_static("#session-status", f"sessions:{len(values)}")

    def _show_session(
        self, session_id: str, values: list[dict] | None = None
    ) -> bool:
        if self._session_store is None:
            return True
        try:
            if values is None:
                values = self._session_store.list()
            value = next(
                (item for item in values if item["session_id"] == session_id),
                None,
            )
            detail = render_session(value) if value else "(session unavailable)"
        except _SESSION_POLL_ERRORS as exc:
            # Touch nothing but the status: the last detail snapshot survives.
            self._update_static("#session-status", f"DEGRADED: {exc}")
            return False
        self._update_static("#session-detail", detail)
        return True

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
        if self._active() == "rooms-tab":
            self._action_room_compare()
            return
        if self._active() != "sessions-tab":
            return
        if self._session_store is None or not self._selected_session_id:
            self.notify("select a session", severity="warning")
            return
        # In-flight guard: `exclusive=True` cancels only the asyncio wrapper —
        # it cannot stop a callable already running in the executor thread, so
        # a second `c` mid-checkpoint would serialize on the store lock and
        # mint a DUPLICATE artifact. One checkpoint at a time.
        if self._checkpoint_running:
            self.notify("checkpoint already running", severity="warning")
            return
        self._checkpoint_running = True
        store = self._session_store
        session_id = self._selected_session_id

        def work() -> None:
            # checkpoint takes a BLOCKING flock, shells out for the diff, and
            # writes the artifact store — on a large worktree that freezes the
            # UI for the duration, so never run it on the event loop.
            # IsolatedSessionStore is file/flock-based (no sqlite thread
            # affinity), the same justification as the artifact discard worker.
            # The guard is cleared ONLY in the event-loop callbacks below —
            # clearing it here (worker thread) opened a window where the store
            # write was done but _after_checkpoint still queued, and a queued
            # keypress could mint the duplicate the guard exists to prevent.
            try:
                run_id = store.checkpoint(session_id)
            except (IsolatedSessionError, OSError) as exc:
                self.call_from_thread(self._checkpoint_failed, str(exc))
                return
            except BaseException as exc:
                # Unexpected — still release the guard on the app thread, then
                # let the worker surface the error.
                self.call_from_thread(self._checkpoint_failed, str(exc))
                raise
            self.call_from_thread(self._after_checkpoint, run_id)

        self.run_worker(work, thread=True, group="session-checkpoint", exclusive=True)

    def _checkpoint_failed(self, message: str) -> None:
        self._checkpoint_running = False
        self.notify(message, severity="error")

    def _after_checkpoint(self, run_id: str) -> None:
        self._checkpoint_running = False
        self.notify(f"checkpoint {run_id[:8]} ready; review in Runs → Artifacts")
        self._refresh_sessions()
        # Land the review where the artifact is: Runs → Artifacts with the new
        # run selected (the sub-tab flip is deduped-tracked by the handler; the
        # main tab switch performs the refresh + focus). A listing already in
        # flight predates the checkpoint: without retiring it, its result
        # lacks the new run, resets the selection to an older one, and every
        # later refresh would faithfully preserve that wrong selection.
        self._invalidate_artifact_listing()
        self._selected_run_id = run_id
        self._artifact_view = "summary"
        self.query_one("#runs-subtabs", TabbedContent).active = "artifacts-pane"
        self.action_tab("runs-tab")

    def _confirm_session_close(self) -> None:
        if self._session_store is None or not self._selected_session_id:
            self.notify("select a session", severity="warning")
            return
        if self._checkpoint_running:
            # close_session runs synchronously on the event loop and would
            # block on the SAME session lock the checkpoint thread holds —
            # freezing the UI for the checkpoint's whole duration.
            self.notify("checkpoint running — wait for it to finish", severity="warning")
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
        if self._checkpoint_running:
            # A checkpoint may have started while the confirm modal was open.
            self.notify("checkpoint running — wait for it to finish", severity="warning")
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
        if self._rooms is None:
            message = self._collaboration_error or (
                "Rooms are disabled. Enable [rooms] enabled=true and press r."
            )
            self._sync_options(room_options, [], None)
            self._sync_options(candidate_options, [], None)
            self.room_detail_text = message
            self._update_static("#room-detail", message)
            self._candidate_detail_key = None  # pane replaced outside commit
            self._update_static("#candidate-detail", "")
            self._update_static("#room-status", "LIMITED MODE")
            return
        # Stage every store read, row access, selection resolution, and render
        # — including the selected room's detail and its selected candidate's
        # detail — in LOCALS before touching any app or widget state: a late
        # failure must leave selection state matching the still-visible panes
        # (else room/candidate actions would target an invisible selection).
        try:
            rooms = self._rooms.store.list_rooms()
            ids = [room["id"] for room in rooms]
            entries = [
                (f"{room['id'][:8]} {room['state']} r{room['round_count']}", room["id"])
                for room in rooms
            ]
            selected_room = self._selected_room_id
            if rooms and selected_room not in ids:
                selected_room = ids[0]
            staged_room = self._stage_room(selected_room) if rooms else None
        except _ROOM_POLL_ERRORS as exc:
            self._update_static("#room-status", f"DEGRADED: {exc}")
            return
        if not rooms:
            self._selected_room_id = None
            self._sync_options(room_options, [], None)
            self._sync_options(candidate_options, [], None)
            self._update_static(
                "#room-detail", "No rooms. Press a to create a comparison room."
            )
            self._candidate_detail_key = None  # pane replaced outside commit
            self._update_static("#candidate-detail", "")
            self._update_static("#room-status", "rooms:0")
            return
        self._selected_room_id = selected_room
        self._sync_options(room_options, entries, selected_room)
        self._commit_room(staged_room)
        self._update_static("#room-status", f"rooms:{len(rooms)}")

    def _stage_room(self, room_id: str, *, reset_candidate: bool = False) -> tuple:
        """Stage room detail + candidate list + selected candidate detail.

        Pure: reads the store and renders into locals, mutating NOTHING —
        raises ``_ROOM_POLL_ERRORS`` on failure so the caller can degrade
        with app state still matching the on-screen panes.
        ``reset_candidate`` stages a room CHANGE: the candidate selection and
        view are proposed fresh instead of carried from the current room.
        """
        assert self._rooms is not None
        room = self._rooms.store.get_room(room_id)
        candidates = self._rooms.store.list_candidates(room_id)
        events = self._rooms.store.events(room_id)
        detail = render_room(room, candidates, events)
        ids = [candidate["id"] for candidate in candidates]
        entries = [
            (
                f"{candidate['id'][:8]} {candidate['member_id']} "
                f"{candidate['state']} {'✓' if candidate['eligible'] else '×'}",
                candidate["id"],
            )
            for candidate in candidates
        ]
        staged_candidate = None
        selected = None if reset_candidate else self._selected_candidate_id
        if candidates:
            view = None if reset_candidate else self._candidate_view
            pages = 1 if reset_candidate else self._diff_pages
            if selected not in ids:
                # Proposed selection reset — staged in locals, committed later.
                # A snapshot view survives it (a first candidate arriving in a
                # room shown in Compare must not flip to Diff and read the
                # artifact on the event loop).
                selected = ids[0]
                if view not in _EVIDENCE_VIEWS:
                    view = None
                pages = 1
            staged_candidate = self._stage_candidate(
                selected, view, pages, room_id=room_id
            )
        elif not reset_candidate and self._candidate_view == "compare":
            # Compare needs no candidate: a round that retained none still has
            # members and evidence to show.
            selected = None
            staged_candidate = self._stage_candidate(
                None, "compare", 1, room_id=room_id
            )
        return detail, entries, selected, staged_candidate, reset_candidate

    def _commit_room(self, staged: tuple) -> None:
        detail, entries, selected, staged_candidate, reset_candidate = staged
        self.room_detail_text = detail
        self._update_static("#room-detail", detail)
        options = self.query_one("#candidate-list", OptionList)
        if staged_candidate is None:  # no candidates
            self._selected_candidate_id = None
            self._candidate_detail_key = None  # pane replaced outside commit
            if reset_candidate:
                # A committed room CHANGE lands on an empty candidate list:
                # apply the staged reset so no view/paging state leaks from
                # the previous room (the old reset-before-show semantics).
                self._reset_candidate_view()
            self._sync_options(options, [], None)
            self._update_static("#candidate-detail", "(no candidates yet)")
            return
        self._selected_candidate_id = selected
        self._sync_options(options, entries, selected)
        self._commit_candidate(staged_candidate)

    def _show_room(self, room_id: str) -> bool:
        if self._rooms is None:
            return True
        # Stage everything before committing anything: a late candidate-read
        # failure keeps BOTH panes (and all selection state) and only flips
        # the status to DEGRADED.
        try:
            staged = self._stage_room(room_id)
        except _ROOM_POLL_ERRORS as exc:
            self._update_static("#room-status", f"DEGRADED: {exc}")
            return False
        self._commit_room(staged)
        return True

    def _reset_candidate_view(self) -> None:
        self._candidate_view = None
        self._diff_pages = 1
        self._diff_truncated = False

    def _stage_candidate(
        self,
        candidate_id: str | None,
        view: str | None,
        pages: int,
        *,
        room_id: str | None = None,
    ) -> tuple:
        """Read + render the candidate detail WITHOUT mutating anything.

        ``view``/``pages`` are the PROPOSED view state (``view=None`` derives
        the default from the candidate's state — the reset case). Returns
        ``(key, text, view, pages, truncated)`` for ``_commit_candidate``;
        ``text is None`` means the cached key is current and there is nothing
        to re-render. Raises ``_ROOM_POLL_ERRORS`` on a store/render failure
        so the caller can degrade atomically.
        """
        assert self._rooms is not None
        if view in _EVIDENCE_VIEWS:
            # The PROPOSED room (a refresh may be staging a replacement for a
            # room that was pruned), never an ambient read of the selection.
            return self._stage_evidence_view(
                room_id if room_id is not None else self._selected_room_id,
                candidate_id,
                view,
                pages,
            )
        assert candidate_id is not None
        reset = view is None
        row = self._rooms.store.get_candidate(candidate_id)
        if view is None:
            view = "diff" if row.get("state") == "completed" else "output"
        # Candidates are immutable (digest-bound), so this key makes a
        # poll tick a no-op instead of re-streaming the patch.
        key = (
            candidate_id,
            view,
            pages,
            row.get("patch_sha256"),
            row.get("output_sha256"),
            row.get("state"),
        )
        truncated = False if reset else self._diff_truncated
        if key == self._candidate_detail_key:
            return key, None, view, pages, truncated
        if view == "diff":
            manifest = self._rooms.store.load_candidate_patch_head(
                candidate_id, pages * _DIFF_DISPLAY_BYTES
            )
            truncated = bool(manifest["patch_truncated"])
            text = render_candidate_diff(
                manifest,
                manifest["patch_head"],
                truncated=truncated,
                binary=manifest.get("patch_binary"),
            )
        else:
            manifest = self._rooms.store.load_candidate_checkpoint(
                candidate_id, _CANDIDATE_OUTPUT_BYTES
            )
            text = render_candidate_output(manifest)
        return key, text, view, pages, truncated

    # ------------------------------------------------ rooms evidence snapshot

    @property
    def _selected_room_id(self) -> str | None:
        return self.__dict__.get("_selected_room_value")

    @_selected_room_id.setter
    def _selected_room_id(self, value: str | None) -> None:
        # Every site that changes the selected room goes through here, so a
        # room change always discards the snapshot: its rows, any error, and a
        # queued request. A build still running finishes (single flight) but
        # its generation is retired, so its result is never shown.
        if value != self.__dict__.get("_selected_room_value") and hasattr(
            self, "_evidence_generation"
        ):
            self._evidence = None
            self._evidence_error = None
            self._evidence_pending = None
            self._evidence_generation += 1
        self.__dict__["_selected_room_value"] = value

    def _room_evidence(self, room_id: str | None = None) -> dict | None:
        """The snapshot for ``room_id`` (default: the selected room), if the
        one slot holds it."""
        room_id = self._selected_room_id if room_id is None else room_id
        evidence = self._evidence
        if evidence is None or evidence["room_id"] != room_id:
            return None
        return evidence

    def _reconcile_evidence_store(self) -> None:
        """Tab activation and `r` rebind the stores, so a rebind alone must not
        discard the snapshot. It is dropped — and any build in flight retired —
        only when the store root changed or the store is gone."""
        root = str(self._rooms.store.root) if self._rooms is not None else None
        if root is not None and root == self._evidence_store_root:
            return
        self._evidence_store_root = root
        self._evidence = None
        self._evidence_error = None
        self._evidence_generation += 1

    def _evidence_target(self) -> tuple[str, str] | None:
        if self._rooms is None or not self._selected_room_id:
            return None
        return self._selected_room_id, str(self._rooms.store.root)

    def _request_evidence(self) -> None:
        """Ask for a snapshot of the selected room. Single flight: a request
        made during a build queues (the latest wins) and starts when that
        build reports back — if it still applies to the selected room."""
        target = self._evidence_target()
        if target is None:
            return
        if self._evidence_inflight is not None:
            self._evidence_pending = target
        else:
            self._start_evidence(target)
        if self._candidate_view in _EVIDENCE_VIEWS:
            # The "building" state is part of the render: show it now.
            self._show_candidate_as(
                self._selected_candidate_id, self._candidate_view, self._diff_pages
            )

    def _start_evidence(self, target: tuple[str, str]) -> None:
        assert self._rooms is not None
        room_id, root = target
        generation = self._evidence_generation
        self._evidence_inflight = (room_id, root, generation)
        store_root = self._rooms.store.root
        output_max = self._rooms.store.candidates.output_max_bytes
        repo_root = Path(self._rooms.repo_root)

        def work() -> None:
            built_at = time.time()
            try:
                result = build_room_evidence(
                    store_root, output_max, repo_root, room_id, built_at
                )
                outcome = (generation, room_id, root, result, None, built_at)
            except Exception as exc:  # degrade, never crash
                outcome = (
                    generation, room_id, root, None,
                    str(exc) or type(exc).__name__, built_at,
                )
            try:
                self.call_from_thread(self._apply_evidence, *outcome)
            except RuntimeError:
                pass  # the app is gone

        self.run_worker(work, thread=True, group="room-evidence")

    def _apply_evidence(
        self,
        generation: int,
        room_id: str,
        root: str,
        result: tuple[RoomComparison, int, frozenset] | None,
        error: str | None,
        built_at: float,
    ) -> None:
        # The in-flight slot is released only here, on the event loop.
        self._evidence_inflight = None
        pending, self._evidence_pending = self._evidence_pending, None
        current = self._evidence_target()
        if pending is not None and pending == current:
            # A newer request for the room on screen: never flash this result;
            # build again. A queued request for a room since left is dropped.
            self._start_evidence(pending)
        elif (
            generation == self._evidence_generation
            and current is not None
            and (room_id, root) == current
        ):
            if result is not None:
                comparison, revision, candidate_ids = result
                self._evidence = {
                    "root": root,
                    "room_id": room_id,
                    "comparison": comparison,
                    "revision": revision,
                    "candidate_ids": candidate_ids,
                    "as_of": time.strftime("%H:%M:%S", time.localtime(built_at)),
                }
                self._evidence_error = None
            else:
                # Keep the last good snapshot of this room; say the rebuild failed.
                self._evidence_error = (room_id, error or "unknown error")
            self._evidence_build_id += 1
        # Otherwise the result belongs to a room or store no longer shown and
        # is dropped. Either way the "building" state may have changed.
        if self._candidate_view in _EVIDENCE_VIEWS:
            self._show_candidate_as(
                self._selected_candidate_id, self._candidate_view, self._diff_pages
            )

    def _stage_evidence_view(
        self, room_id: str | None, candidate_id: str | None, view: str, pages: int
    ) -> tuple:
        """Compare/Details text from the snapshot: SQL reads only (the room
        row and candidate ids for the change hint, the round prompt), never an
        artifact read."""
        assert self._rooms is not None
        evidence = self._room_evidence(room_id)
        error = (
            self._evidence_error[1]
            if self._evidence_error and self._evidence_error[0] == room_id
            else None
        )
        building = (
            self._evidence_inflight is not None
            and self._evidence_inflight[0] == room_id
        ) or (
            self._evidence_pending is not None
            and self._evidence_pending[0] == room_id
        )
        target_round = None
        stored_rounds = 0
        if view == "compare" and candidate_id is None and room_id:
            listed = self._rooms.store.list_rounds(room_id)
            stored_rounds = len(listed)
            numbers = [
                r["number"]
                for r in listed
                if type(r.get("number")) is int and r["number"] > 0
            ]
            target_round = max(numbers) if numbers else None
        changed = False
        if evidence is not None and room_id:
            room = self._rooms.store.get_room(room_id)
            current_ids = frozenset(
                c["id"] for c in self._rooms.store.list_candidates(room_id)
            )
            changed = (
                int(room["revision"]) != evidence["revision"]
                or current_ids != evidence["candidate_ids"]
            )
        key = (
            "evidence",
            room_id,
            candidate_id,
            view,
            self._evidence_build_id,
            error,
            changed,
            building,
            target_round,
            stored_rounds,
        )
        if key == self._candidate_detail_key:
            return key, None, view, pages, False
        lines: list[str] = []
        if error is not None:
            lines.append(f"evidence unavailable: {printable_name(error)}")
        if building:
            lines.append("building evidence snapshot...")
        if evidence is None:
            if not building and error is None:
                lines.append("no evidence snapshot for this room — press c or r")
            return key, "\n".join(lines), view, pages, False
        if changed:
            lines.append("room changed since this snapshot — press r")
        comparison: RoomComparison = evidence["comparison"]
        as_of = evidence["as_of"]
        found = find_row(comparison, candidate_id) if candidate_id else None
        if candidate_id and found is None:
            # A candidate missing from the snapshot may sit in a damaged round.
            lines.extend(
                f"Damaged round {printable_name(entry['round_id'])}: "
                f"{printable_name(entry['reason'])}"
                for entry in comparison.damaged_rounds
            )
            lines.append(
                f"this candidate is not in the evidence snapshot (as of {as_of})"
                " — press r"
            )
            return key, "\n".join(lines), view, pages, False
        if view == "details" and candidate_id is None:
            # The selection can vanish under an open Details (a rebind to a
            # room without candidates): say so instead of rendering a row.
            lines.append("no candidate selected — Details needs a candidate")
            return key, "\n".join(lines), view, pages, False
        # Damage is evidence: the early returns below list it themselves
        # (render_round_table does on the normal path).
        damage = [
            f"Damaged round {printable_name(entry['round_id'])}: "
            f"{printable_name(entry['reason'])}"
            for entry in comparison.damaged_rounds
        ]
        if view == "compare":
            if found is not None:
                round_number = found[0].number
            elif target_round is None:
                lines.extend(damage)
                lines.append(
                    (
                        "no readable rounds in this room"
                        if stored_rounds
                        else "no rounds have run in this room yet"
                    )
                    + f" (evidence as of {as_of})"
                )
                return key, "\n".join(lines), view, pages, False
            elif find_round(comparison, target_round) is None:
                # The latest round per the store is not in this snapshot (it
                # arrived later, or its record is damaged): say so, never show
                # an older round as if it were the latest.
                lines.extend(damage)
                lines.append(
                    f"round {target_round} is not in the evidence snapshot "
                    f"(as of {as_of}) — press r"
                )
                return key, "\n".join(lines), view, pages, False
            else:
                round_number = target_round
            lines.append(
                render_round_table(
                    comparison,
                    round_number,
                    highlighted_id=candidate_id,
                    as_of=as_of,
                )
            )
        else:
            assert found is not None
            group, row = found
            round_prompt = None
            try:
                round_prompt = self._rooms.store.get_round(group.round_id).get("prompt")
            except (RoomError, KeyError, TypeError):
                pass
            lines.append(
                render_candidate_details(
                    row,
                    round_number=group.number,
                    round_prompt=round_prompt,
                    as_of=as_of,
                    selection=comparison.selection,
                    group=group,
                )
            )
        return key, "\n".join(lines), view, pages, False

    def _commit_candidate(self, staged: tuple | None) -> None:
        if staged is None:
            return
        key, text, view, pages, truncated = staged
        self._candidate_view = view
        self._diff_pages = pages
        self._diff_truncated = truncated
        if text is None:
            return  # cached key is current — nothing to re-render
        self._candidate_detail_key = key
        self.candidate_detail_text = text
        self._update_static("#candidate-detail", text)

    def _show_candidate(self, candidate_id: str) -> bool:
        return self._show_candidate_as(
            candidate_id, self._candidate_view, self._diff_pages
        )

    def _show_candidate_as(
        self, candidate_id: str | None, view: str | None, pages: int
    ) -> bool:
        """Stage-then-commit the candidate detail under a PROPOSED view state.

        On failure nothing is committed — not the widgets, not the view/pages
        proposal — so the old pane never sits over advanced view state.
        """
        if self._rooms is None:
            return True
        try:
            staged = self._stage_candidate(candidate_id, view, pages)
        except _ROOM_POLL_ERRORS as exc:
            # Touch nothing but the status: the last detail snapshot (and the
            # cache key describing it) stays accurate for what is on screen.
            self._update_static("#room-status", f"DEGRADED: {exc}")
            return False
        self._commit_candidate(staged)
        return True

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
            # Prefill with the PREVIOUS round's prompt (creation prompt on a
            # fresh room) — the modal is where per-round instructions land now.
            rounds = self._rooms.store.list_rounds(self._selected_room_id)
            default_prompt = rounds[-1]["prompt"] if rounds else room["prompt"]
            eligible = [
                row
                for row in self._rooms.store.list_candidates(self._selected_room_id)
                if row.get("eligible")
            ]
        except _ROOM_POLL_ERRORS as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            RoomRoundModal(room, default_prompt, eligible),
            self._apply_room_round,
        )

    def _apply_room_round(self, fields: dict | bool | None) -> None:
        if not isinstance(fields, dict) or self._rooms is None or not self._selected_room_id:
            return
        # Preserve the prompt VERBATIM (the raw-user-text contract the stored
        # rounds.prompt keeps, and what --prompt-file does): strip only to
        # decide whether an all-whitespace entry means "no prompt".
        raw_prompt = str(fields.get("prompt") or "")
        prompt = raw_prompt if raw_prompt.strip() else None
        lineage = str(fields.get("lineage") or "continue-own")
        base_candidate = fields.get("base_candidate")
        if prompt is None:
            self.notify(
                "no round prompt — reusing the room creation prompt",
                severity="warning",
            )
        try:
            self._rooms.preflight_round(
                self._selected_room_id,
                prompt,
                lineage=lineage,
                base_candidate_id=base_candidate,
            )
            pid = launch_room_worker(
                self._project_root(),
                self._selected_room_id,
                prompt,
                lineage=lineage,
                base_candidate_id=base_candidate,
                resolved_config=self._rooms.config,
                config_base=self._toml_path.parent,
            )
            self._rooms.store.record_event(
                self._selected_room_id, "worker.launched", {"pid": pid}
            )
        # ProjectError covers a repository that disappeared under us: git_root
        # reports an unusable path that way rather than letting subprocess's
        # OSError through, and an unguarded raise here kills the whole app.
        except (RoomError, ArtifactError, WorkspaceError, ProjectError, OSError) as exc:
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
        if self._select_inflight is not None:
            self.notify("a selection is still being verified", severity="warning")
            return
        try:
            room = self._rooms.store.get_room(self._selected_room_id)
            row = self._rooms.store.get_candidate(self._selected_candidate_id)
            # Metadata only: the full artifact is verified by the select
            # itself, off the event loop.
            manifest = self._rooms.store.load_candidate_manifest(row["id"])
        except RoomError as exc:
            self.notify(str(exc), severity="error")
            return
        p = printable_name
        self.push_screen(
            CandidateSelectModal(
                f"Candidate: {p(str(row['id']))}\n"
                f"Member: {p(str(manifest.get('member_id')))}\n"
                f"Patch sha256: {p(str(row['patch_sha256']))}\n"
                f"Eligible: {bool(row['eligible'])}\n"
                f"Room revision: {room['revision']}\n\n"
                "The artifact is verified against this digest before it is "
                "selected; apply verifies it again."
            ),
            lambda confirmed, room_id=room["id"], candidate_id=row["id"], revision=int(
                room["revision"]
            ), digest=row["patch_sha256"]: self._apply_candidate_selection(
                confirmed, room_id, candidate_id, revision, digest
            ),
        )

    def _apply_candidate_selection(
        self,
        confirmed: dict | bool | None,
        room_id: str,
        candidate_id: str,
        expected_revision: int,
        expected_patch_sha256: str,
    ) -> None:
        if not confirmed or self._rooms is None:
            return
        if self._select_inflight is not None:
            self.notify("a selection is still being verified", severity="warning")
            return
        rationale = confirmed.get("rationale") if isinstance(confirmed, dict) else None
        store_root = Path(self._rooms.store.root)
        output_max = self._rooms.store.candidates.output_max_bytes
        root = str(store_root)
        self._select_inflight = (room_id, candidate_id, root)

        def work() -> None:
            try:
                select_room_candidate(
                    store_root,
                    output_max,
                    room_id,
                    candidate_id,
                    expected_revision=expected_revision,
                    expected_patch_sha256=expected_patch_sha256,
                    rationale=rationale,
                )
                error = None
            except Exception as exc:  # report, never crash the app
                error = str(exc) or type(exc).__name__
            try:
                self.call_from_thread(
                    self._after_candidate_selection, candidate_id, root, error
                )
            except RuntimeError:
                pass  # the app is gone

        self.run_worker(work, thread=True, group="room-select")

    def _after_candidate_selection(
        self, candidate_id: str, root: str, error: str | None
    ) -> None:
        # Released only here, on the event loop (the evidence-worker rule).
        self._select_inflight = None
        if error is not None:
            self.notify(printable_name(error), severity="error")
            return
        self.notify(f"selected {printable_name(candidate_id[:8])}")
        if self._rooms is None or str(self._rooms.store.root) != root:
            return  # the store shown now is not the one the selection went to
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
        # Same guard as the artifact-apply confirmation: resolving the target can
        # raise ProjectError, which would escape this handler and kill the app.
        try:
            target = self._project_root()
        except ProjectError as exc:
            self.notify(str(exc), severity="error")
            return
        self.push_screen(
            CollaborationConfirmModal(
                "Apply selected candidate to source",
                f"Room: {room['id']}\nCandidate: {candidate_id}\n"
                f"Patch digest: {manifest['patch_sha256']}\n"
                f"Target: {target}\n\n"
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
        self.query_one("#runs-subtabs", TabbedContent).active = "operations-pane"
        self.action_tab("runs-tab")

    def _selected_job(self) -> dict | None:
        """The Runs-tab highlighted row, resolved through list_jobs."""
        if self._jobs is None or self._selected_job_dir is None:
            return None
        for row in self._jobs.list_jobs():
            if row.get("job_dir") == self._selected_job_dir:
                return row
        return None

    def _selected_job_id(self) -> str | None:
        """The selected job id, kept as a compatibility seam for tests."""
        row = self._selected_job()
        return str(row["job_id"]) if row and row.get("job_id") else None

    def _control_job(self, job_id: str, action: str) -> None:
        try:
            if self._jobs is None:
                raise JobError(self._jobs_error or "TUI job registry is unavailable")
            self._jobs.control(job_id, action)
        except JobError as exc:
            self.notify(str(exc), severity="error")

    def _control_selected_job(self, action: str) -> None:
        job_id = self._selected_job_id()
        if job_id is None:
            self.notify("select a job in the Runs tab first", severity="error")
            return
        self._control_job(job_id, action)

    def action_safe_stop(self) -> None:
        if self._active() == "sessions-tab":
            self._confirm_session_close()
            return
        if self._active() == "rooms-tab":
            self._confirm_room_close()
            return
        if self._active() != "runs-tab":
            return
        if self._runs_pane() == "artifacts-pane":
            self._confirm_artifact_discard()
            return
        self._control_selected_job("stop")

    def _confirm_room_close(self) -> None:
        if self._rooms is None or not self._selected_room_id:
            self.notify("select a room", severity="warning")
            return
        room_id = self._selected_room_id
        self.push_screen(
            CollaborationConfirmModal(
                "Close comparison room",
                f"Room: {room_id}\n\n"
                "Candidates and receipts are preserved; the room's isolated "
                "workspaces and read-only viewers are removed.",
                "Close",
            ),
            lambda confirmed, room_id=room_id: self._apply_room_close(
                confirmed, room_id
            ),
        )

    def _apply_room_close(self, confirmed: dict | bool | None, room_id: str) -> None:
        if not confirmed or self._rooms is None:
            return
        try:
            # The coordinator-level close (the CLI `room close` path): durable
            # claim, workspace cleanup, and viewer reclaim in-process — SQLite
            # + filesystem only, no orchestration.
            self._rooms.close_room(room_id)
        except RoomError as exc:
            self.notify(str(exc), severity="error")
            return
        self.notify(f"closed room {room_id[:8]}")
        self._refresh_rooms()

    def action_force_stop(self) -> None:
        # Operations-pane only: from Artifacts this would force-stop the
        # persisted, currently HIDDEN Operations selection.
        if self._active() != "runs-tab" or self._runs_pane() != "operations-pane":
            return
        row = self._selected_job()
        if row is None or not row.get("job_id"):
            self.notify("select a job in the Runs tab first", severity="error")
            return
        job_id = str(row["job_id"])
        op = str(row.get("op") or "unknown")
        # Schema-2 rows keep operation fields nested under "request" (the
        # _job_label contract); legacy rows may carry them at the top level.
        request = row.get("request") or {}
        target = "-"
        for key in ("item_id", "goal", "workflow_id", "run_id", "room_id"):
            value = request.get(key) or row.get(key)
            if value:
                target = str(value)
                break
        self.push_screen(
            CollaborationConfirmModal(
                "Force-stop exact job",
                f"Job: {job_id}\nOperation: {op}\nTarget: {target}\n\n"
                "SIGKILL is accepted only after the 10-second safe-stop grace "
                "period. It can leave a lease or dirty isolated worktree that "
                "requires later recovery.",
                "Force stop",
            ),
            lambda confirmed, job_id=job_id: self._apply_force_stop(
                confirmed, job_id
            ),
        )

    def _apply_force_stop(
        self, confirmed: dict | bool | None, job_id: str
    ) -> None:
        if confirmed:
            self._control_job(job_id, "force")

    def action_recover_runs(self) -> None:
        active = self._active()
        if active == "rooms-tab":
            if self._rooms is None:
                self.notify("Rooms are unavailable", severity="warning")
                return
            self.run_worker(
                self._recover_via_cli("room", "recover"),
                group="recover-runs",
                exclusive=True,
            )
            return
        if active != "runs-tab":
            return
        self.run_worker(
            self._recover_via_cli("recover"), group="recover-runs", exclusive=True
        )

    async def _recover_via_cli(self, *argv: str) -> None:
        # Reconcile via a detached CLI subprocess (`syncmill recover` /
        # `syncmill room recover`), never in process: recovery takes the
        # repository apply lock, and orchestration must never run on Textual's
        # event loop.  The subprocess also reuses the exact CLI
        # project-identity path, so it addresses the same runs an ``apply``
        # would.  A live apply/worker owner is skipped before the lock, so
        # this can never block on a healthy detached worker.
        root = self._job_project_root()
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CLI_SHIM,
            *argv,
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
        # Refresh what the user is actually looking at (Artifacts manifests /
        # room states change on recovery, not just the job list).
        active = self._active()
        if active == "runs-tab":
            self._refresh_runs_pane()
        elif active == "rooms-tab":
            self._refresh_rooms()
        else:
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
            # validate→save as one transaction (the `team save` CLI recipe).
            with config_write_lock(self._toml_path):
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
        except (ValidationError, OSError, ValueError) as exc:
            # ValueError covers tomllib.TOMLDecodeError: a mid-edit malformed
            # toml must keep the cached view + toast, never kill the TUI.
            self.notify(f"config reload failed: {exc}", severity="error")
            return False
        self._config = config
        self._rebind_jobs()
        self._rebind_collaboration()
        return True

    def _retry_board(self) -> None:
        try:
            config = load_config_at(self._toml_path)
        except (ValidationError, OSError, ValueError) as exc:
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
        # A status subprocess can finish while the screen is being removed.
        # Keep its result, but do not turn an advisory refresh into a worker
        # failure after the target widgets have gone away.
        try:
            agents = self.query_one("#agent-status", Static)
            memory = self.query_one("#memory-status", Static)
        except NoMatches:
            return
        agents.update("\n".join(lines))
        memory.update(f"\nMemory:\n{self._memory_status}")

    def _start_memory_probe(self) -> None:
        if self._jobs is not None and self._jobs.active_job() is not None:
            self._memory_status = "probe deferred while a TUI worker is active"
            self._refresh_agents()
            return
        self._memory_status = "probing…"
        self._refresh_agents()
        self.run_worker(self._probe_memory(), group="memory-status", exclusive=True)

    async def _probe_memory(self) -> None:
        # `syncmill status` has no --repo and discovers syncmill.toml relative
        # to its cwd — pin the child to the TUI's config location, or the probe
        # reports whatever project the ambient cwd happens to hold.
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _CLI_SHIM,
            "status",
            cwd=str(self._toml_path.parent.resolve()),
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
    initial_tab: str = "auto",
) -> None:
    SyncmillTuiApp(
        config,
        board=board,
        board_error=board_error,
        toml_path=toml_path,
        refresh_seconds=refresh_seconds,
        initial_tab=initial_tab,
    ).run()
