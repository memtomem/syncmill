from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import threading

import pytest
from textual.widgets import DataTable, OptionList

from syncmill.artifacts import ArtifactError, ArtifactStore
from syncmill.board import Board
from syncmill.isolated_session import IsolatedSessionError
from syncmill.config import SyncmillConfig
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.tui import (
    TAB_CHOICES,
    SyncmillTuiApp,
    _action_bar_text,
    _artifact_label,
    _job_label,
    _log_signature,
    _read_log_tail,
    _resolve_initial_tab,
    render_artifact,
)
from syncmill.tui_jobs import ArtifactApplySpec, JobError, RoomApplySpec, project_key


def drive(coro):
    return asyncio.run(coro)


def test_log_tail_is_bounded_across_rotated_segments(tmp_path):
    (tmp_path / "console.log.1").write_bytes(b"a" * 20)
    (tmp_path / "console.log").write_bytes(b"b" * 20)
    before = _log_signature(tmp_path)
    assert _read_log_tail(tmp_path, 25) == b"a" * 5 + b"b" * 20
    assert _log_signature(tmp_path) == before
    (tmp_path / "console.log").write_bytes(b"c" * 3)
    assert _log_signature(tmp_path) != before


def test_resolve_initial_tab_auto_priority_and_explicit_override():
    # auto: Rooms → Board → Runs → Status, gated on the store actually opening.
    assert (
        _resolve_initial_tab("auto", rooms_ok=True, board_ok=True, jobs_ok=True)
        == "rooms-tab"
    )
    assert (
        _resolve_initial_tab("auto", rooms_ok=False, board_ok=True, jobs_ok=True)
        == "board-tab"
    )
    assert (
        _resolve_initial_tab("auto", rooms_ok=False, board_ok=False, jobs_ok=True)
        == "runs-tab"
    )
    assert (
        _resolve_initial_tab("auto", rooms_ok=False, board_ok=False, jobs_ok=False)
        == "status-tab"
    )
    # An explicit tab is always honored, even when its store is unavailable.
    assert (
        _resolve_initial_tab("board", rooms_ok=True, board_ok=False, jobs_ok=True)
        == "board-tab"
    )
    assert (
        _resolve_initial_tab("rooms", rooms_ok=False, board_ok=False, jobs_ok=False)
        == "rooms-tab"
    )


def test_action_bar_text_is_tab_specific():
    board = _action_bar_text("board-tab")
    runs = _action_bar_text("runs-tab")
    rooms = _action_bar_text("rooms-tab")
    assert "d decisions" in board and "f filter" in board
    assert "f follow" in runs and "R recover" in runs
    assert "t terminal" in rooms and "g apply" in rooms
    # Every bar carries the shared keys; unknown tabs degrade to just those.
    for text in (board, runs, rooms, _action_bar_text("unknown")):
        assert "1-7 tabs" in text and "q quit" in text
    # The common keys are their own line so a narrow terminal wraps the
    # tab-specific line instead of clipping `? help | q quit`.
    assert board.splitlines()[-1] == _action_bar_text("unknown")
    # The Artifacts sub-pane has its own keys; an unavailable store shows only
    # the common keys (the bar must not advertise dead actions).
    artifacts = _action_bar_text("runs-tab", runs_pane="artifacts-pane")
    assert "g apply" in artifacts and "k discard" in artifacts
    assert _action_bar_text("board-tab", available=False) == _action_bar_text(
        "unknown"
    )


def test_cli_tab_choices_match_the_tui():
    from syncmill.cli import cli

    param = next(p for p in cli.commands["tui"].params if p.name == "tab")
    assert tuple(param.type.choices) == TAB_CHOICES


def test_tui_limited_mode_and_tab_navigation(tmp_path):
    async def scenario():
        config = SyncmillConfig(
            board={"enabled": False}, tui={"state_dir": str(tmp_path / "jobs")}
        )
        app = SyncmillTuiApp(
            config,
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "LIMITED MODE" in str(app.query_one("#board-status").render())
            assert not (tmp_path / "board.db").exists()
            await pilot.press("2")
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"

    drive(scenario())


@pytest.mark.parametrize("size", [(120, 40), (80, 24)])
def test_all_seven_tabs_remain_reachable_at_supported_viewports(
    tmp_path, git_repo, size
):
    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(
                board={"enabled": False},
                rooms={"enabled": False},
                runs={"root": str(tmp_path / "runs")},
                workspace={"root": str(tmp_path / "workspaces")},
                tui={"state_dir": str(tmp_path / "jobs")},
            ),
            board=None,
            board_error=None,
            toml_path=git_repo / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            for number, tab in enumerate(
                (
                    "board-tab",
                    "runs-tab",
                    "reports-tab",
                    "teams-tab",
                    "status-tab",
                    "sessions-tab",
                    "rooms-tab",
                ),
                start=1,
            ):
                await pilot.press(str(number))
                await pilot.pause()
                assert app.query_one("#tabs").active == tab
                assert "1-7 tabs" in app.action_bar_text

    drive(scenario())


def test_tui_path_failure_keeps_limited_mode_available(tmp_path, monkeypatch):
    async def scenario():
        def fail_path(*args, **kwargs):
            raise JobError("cannot resolve board database path")

        monkeypatch.setattr("syncmill.tui.canonical_board_path", fail_path)
        app = SyncmillTuiApp(
            SyncmillConfig(board={"enabled": False}),
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert "LIMITED MODE" in str(app.query_one("#board-status").render())
            await pilot.press("2")
            await pilot.pause()
            assert "Runs unavailable" in str(app.query_one("#job-status").render())
            await pilot.press("4")
            await pilot.pause()
            assert app.query_one("#tabs").active == "teams-tab"

    drive(scenario())


def test_auto_tab_falls_back_to_status_when_nothing_is_available(
    tmp_path, monkeypatch
):
    async def scenario():
        def fail_path(*args, **kwargs):
            raise JobError("cannot resolve board database path")

        monkeypatch.setattr("syncmill.tui.canonical_board_path", fail_path)
        app = SyncmillTuiApp(
            SyncmillConfig(board={"enabled": False}),
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#tabs").active == "status-tab"

    drive(scenario())


def test_auto_tab_prefers_runs_over_status_when_jobs_open(tmp_path):
    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(
                board={"enabled": False}, tui={"state_dir": str(tmp_path / "jobs")}
            ),
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._jobs is not None
            assert app.query_one("#tabs").active == "runs-tab"

    drive(scenario())


def test_auto_tab_prefers_rooms_when_the_store_opens(tmp_path, git_repo):
    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(
                repo_root=str(git_repo),
                rooms={"enabled": True},
                runs={"root": str(tmp_path / "runs")},
                tui={"state_dir": str(tmp_path / "jobs")},
            ),
            board=None,
            board_error=None,
            toml_path=git_repo / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._rooms is not None
            assert app.query_one("#tabs").active == "rooms-tab"

    drive(scenario())


def test_explicit_tab_is_honored_even_when_unavailable(tmp_path):
    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(
                board={"enabled": False}, tui={"state_dir": str(tmp_path / "jobs")}
            ),
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
            initial_tab="board",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#tabs").active == "board-tab"
            assert "LIMITED MODE" in str(app.query_one("#board-status").render())

    drive(scenario())


def test_tab_switch_updates_action_bar_and_focuses_primary_list(tmp_path):
    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(
                board={"enabled": False}, tui={"state_dir": str(tmp_path / "jobs")}
            ),
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            # Mount lands on runs-tab (auto) with its bar + focus applied.
            assert app.action_bar_text == _action_bar_text("runs-tab")
            assert app.focused is not None and app.focused.id == "job-list"
            await pilot.press("7")
            await pilot.pause()
            # Rooms are unavailable here, so the bar shows only common keys.
            assert app.action_bar_text == _action_bar_text(
                "rooms-tab", available=False
            )
            assert str(app.query_one("#action-bar").render()) == app.action_bar_text
            assert app.focused is not None and app.focused.id == "room-list"

    drive(scenario())


def test_switch_to_status_clears_focus_and_sticks(tmp_path):
    # Regression (codex R1 Major): Status has no primary list; leaving focus
    # inside the hidden previous pane made Textual reactivate that pane and
    # silently undo the switch.
    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(
                board={"enabled": False}, tui={"state_dir": str(tmp_path / "jobs")}
            ),
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"
            await pilot.press("5")
            await pilot.pause()
            assert app.query_one("#tabs").active == "status-tab"
            assert app.focused is None
            await pilot.press("3")
            await pilot.pause()
            assert app.query_one("#tabs").active == "reports-tab"
            await pilot.press("5")
            await pilot.pause()
            assert app.query_one("#tabs").active == "status-tab"
            assert app.focused is None

    drive(scenario())


def test_retry_rebinds_board_and_job_store(tmp_path):
    old_db = tmp_path / "old.db"
    new_db = tmp_path / "new.db"
    state_dir = tmp_path / "new-state"
    toml = tmp_path / "syncmill.toml"
    toml.write_text(
        "[board]\n"
        "enabled = true\n"
        f'db_path = "{new_db}"\n'
        "[tui]\n"
        f'state_dir = "{state_dir}"\n'
        "keep_jobs = 7\n",
        encoding="utf-8",
    )
    app = SyncmillTuiApp(
        SyncmillConfig(
            board={"enabled": False, "db_path": str(old_db)},
            tui={"state_dir": str(tmp_path / "old-state")},
        ),
        board=None,
        board_error=None,
        toml_path=toml,
    )
    app._retry_board()
    try:
        assert app._board is not None
        assert app._jobs is not None
        assert app._board.db_path == new_db.resolve()
        assert app._jobs.board_db == new_db.resolve()
        assert app._jobs.key == project_key(app._jobs.project_root)
        assert app._jobs.root.parent == state_dir
        assert app._jobs.config.keep_jobs == 7
    finally:
        if app._board is not None:
            app._board.close()


def test_failed_rebind_discards_stale_store(tmp_path, monkeypatch):
    app = SyncmillTuiApp(
        SyncmillConfig(
            board={"enabled": False, "db_path": str(tmp_path / "board.db")},
            tui={"state_dir": str(tmp_path / "state")},
        ),
        board=None,
        board_error=None,
        toml_path=tmp_path / "syncmill.toml",
    )
    assert app._jobs is not None

    def fail_store(*args, **kwargs):
        raise JobError("cannot resolve TUI state directory")

    monkeypatch.setattr("syncmill.tui.JobStore", fail_store)
    app._rebind_jobs()
    assert app._jobs is None
    assert "cannot resolve TUI state directory" in str(app._jobs_error)


def test_tui_collaboration_rejects_unignored_project_state(git_repo, tmp_path):
    app = SyncmillTuiApp(
        SyncmillConfig(
            repo_root=str(git_repo),
            runs={"root": ".state"},
            workspace={"root": ".workspaces"},
            rooms={"enabled": True},
            tui={"state_dir": str(tmp_path / "tui-state")},
        ),
        board=None,
        board_error=None,
        toml_path=git_repo / "syncmill.toml",
    )

    assert app._session_store is None
    assert app._rooms is None
    assert "must be Git-ignored" in str(app._collaboration_error)
    assert not (git_repo / ".state").exists()
    assert not (git_repo / ".workspaces").exists()


def test_tui_renders_real_board_and_adds_item(tmp_path):
    async def scenario():
        board = Board(str(tmp_path / "board.db"))
        config = SyncmillConfig(
            board={"enabled": True, "db_path": str(tmp_path / "board.db")},
            tui={"state_dir": str(tmp_path / "jobs")},
        )
        app = SyncmillTuiApp(
            config,
            board=board,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app._apply_add({"task": "ship tui", "goal": "ui", "deps": "", "human": False})
            await pilot.pause()
            assert app.query_one("#board-table").row_count == 1
            assert board.list()[0].task == "ship tui"

    drive(scenario())


def test_runs_recover_key_reconciles_via_a_detached_cli(tmp_path, monkeypatch):
    async def scenario():
        captured: dict = {}

        class FakeProc:
            pid = 424242
            returncode = 0

            async def communicate(self):
                return (
                    b"Run r1: not applied - restored to pending\n",
                    b"",
                )

            async def wait(self):
                return 0

        async def fake_exec(*args, **kwargs):
            captured["argv"] = args
            return FakeProc()

        monkeypatch.setattr(
            "syncmill.tui.asyncio.create_subprocess_exec", fake_exec
        )
        config = SyncmillConfig(
            board={"enabled": False},
            repo_root=str(tmp_path),
            tui={"state_dir": str(tmp_path / "jobs")},
        )
        app = SyncmillTuiApp(
            config,
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            notes: list = []
            monkeypatch.setattr(
                app, "notify", lambda *a, **k: notes.append((a, k))
            )
            await pilot.press("2")
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"
            await pilot.press("R")
            await app.workers.wait_for_complete()
            await pilot.pause()
            # The reconcile ran the recover subcommand out of process...
            assert "recover" in captured["argv"]
            assert "--repo" in captured["argv"]
            # ...and its summary surfaced as a notification.
            assert any("restored to pending" in a[0] for a, _ in notes)

    drive(scenario())


def test_memory_probe_pins_the_child_to_the_configured_toml_dir(
    tmp_path, monkeypatch
):
    async def scenario():
        captured: dict = {}

        class FakeProc:
            pid = 424242
            returncode = 0

            async def communicate(self):
                return (b"memtomem: connected\n", b"")

        async def fake_exec(*args, **kwargs):
            captured["argv"] = args
            captured["kwargs"] = kwargs
            return FakeProc()

        monkeypatch.setattr(
            "syncmill.tui.asyncio.create_subprocess_exec", fake_exec
        )
        toml_dir = tmp_path / "project"
        toml_dir.mkdir()
        config = SyncmillConfig(
            board={"enabled": False},
            repo_root=str(tmp_path),
            tui={"state_dir": str(tmp_path / "jobs")},
        )
        app = SyncmillTuiApp(
            config,
            board=None,
            board_error=None,
            toml_path=toml_dir / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._probe_memory()
            # `syncmill status` discovers syncmill.toml relative to its cwd —
            # the probe must pin the child to the TUI's config location, not
            # inherit the ambient cwd (a different project's config).
            assert captured["kwargs"]["cwd"] == str(toml_dir.resolve())
            assert "status" in captured["argv"]
            assert "connected" in app._memory_status

    drive(scenario())


def test_rooms_terminal_opens_viewer_for_selected_room(tmp_path, git_repo, monkeypatch):
    async def scenario():
        events: dict = {}

        class Rooms:
            store = object()
            project_id = "proj-1"

            def close(self):
                return None

        # Patch the room_viewer seams at the TUI lookup site.
        import syncmill.tui as T

        monkeypatch.setattr(T, "sweep_orphan_viewers", lambda store, project_id: events.__setitem__("swept", project_id))
        monkeypatch.setattr(
            T, "ensure_viewer",
            lambda store, project_id, room_id: events.__setitem__("ensured", (project_id, room_id)) or "sess",
        )
        monkeypatch.setattr(T, "in_tmux", lambda: True)  # non-blocking path, no suspend
        monkeypatch.setattr(
            T, "attach_viewer",
            lambda name: events.__setitem__("attached", name) or 0,
        )

        app = SyncmillTuiApp(
            SyncmillConfig(repo_root=str(git_repo), runs={"root": str(tmp_path / "runs")}),
            board=None,
            board_error=None,
            toml_path=git_repo / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = Rooms()
            app._selected_room_id = "room-a"
            monkeypatch.setattr(app, "_refresh_all", lambda: None)
            app.query_one("#tabs").active = "rooms-tab"
            app.action_terminal()
            assert events == {
                "swept": "proj-1",
                "ensured": ("proj-1", "room-a"),
                "attached": "sess",
            }

    drive(scenario())


class _FakeRoomStore:
    def __init__(self, selected_candidate_id="candidate-a"):
        self._selected = selected_candidate_id

    def get_room(self, room_id):
        return {
            "id": room_id,
            "state": "selected",
            "selected_candidate_id": self._selected,
            "revision": 7,
        }

    def get_candidate(self, candidate_id):
        return {"id": candidate_id}

    def load_candidate(self, candidate_id):
        return {"candidate_id": candidate_id, "patch_sha256": "abc123"}


class _FakeRooms:
    def __init__(self, store=None):
        self.store = store or _FakeRoomStore()
        self.config = object()
        self.repo_root = "."

    def close(self):
        return None


class _FakeJobs:
    def __init__(self, fail=None):
        self.launched: list[tuple] = []
        self._fail = fail

    def launch(self, spec, *, resolved_config=None):
        if self._fail is not None:
            raise self._fail
        self.launched.append((spec, resolved_config))
        return {"job_id": "abcd1234efgh"}

    def list_jobs(self):
        return []

    def active_job(self):
        return None


def _room_apply_app(tmp_path, git_repo):
    return SyncmillTuiApp(
        SyncmillConfig(
            repo_root=str(git_repo),
            runs={"root": str(tmp_path / "runs")},
        ),
        board=None,
        board_error=None,
        toml_path=git_repo / "syncmill.toml",
        refresh_seconds=3600,
        # Pin the historical start tab: these tests assert that a (failed)
        # launch does not navigate to Runs, which is meaningless if `auto`
        # already opened there.
        initial_tab="board",
    )


def test_room_apply_confirmation_captures_exact_candidate_and_revision(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        rooms = _FakeRooms()
        jobs = _FakeJobs()
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = rooms
            app._jobs = jobs
            app._selected_room_id = "room-a"
            captured: dict = {}

            def capture_screen(screen, callback):
                captured["callback"] = callback

            monkeypatch.setattr(app, "push_screen", capture_screen)
            app._confirm_room_apply()
            app._selected_room_id = "room-b"
            captured["callback"](True)
            await pilot.pause()
            assert len(jobs.launched) == 1
            spec, resolved = jobs.launched[0]
            assert isinstance(spec, RoomApplySpec)
            assert spec.room_id == "room-a"
            assert spec.verify is False
            assert spec.expected_candidate_id == "candidate-a"
            assert spec.expected_revision == 7
            assert resolved is rooms.config
            assert app.query_one("#tabs").active == "runs-tab"

    drive(scenario())


def test_room_apply_refuses_when_job_registry_unavailable(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms()
            app._jobs = None
            app._jobs_error = "cannot resolve TUI state directory"
            app._selected_room_id = "room-a"
            pushed: list = []
            notices: list[tuple] = []
            monkeypatch.setattr(
                app, "push_screen", lambda *args, **kwargs: pushed.append(args)
            )
            monkeypatch.setattr(
                app,
                "notify",
                lambda message, **kwargs: notices.append((message, kwargs)),
            )
            app._confirm_room_apply()
            assert pushed == []
            assert notices == [
                (
                    "cannot resolve TUI state directory",
                    {"severity": "error"},
                )
            ]

    drive(scenario())


def test_room_apply_duplicate_launch_surfaces_joberror(tmp_path, git_repo, monkeypatch):
    async def scenario():
        jobs = _FakeJobs(
            fail=JobError("a TUI-managed worker is already active for this target")
        )
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms()
            app._jobs = jobs
            app._selected_room_id = "room-a"
            captured: dict = {}
            notices: list[tuple] = []
            monkeypatch.setattr(
                app, "push_screen", lambda screen, callback: captured.update(cb=callback)
            )
            monkeypatch.setattr(
                app,
                "notify",
                lambda message, **kwargs: notices.append((message, kwargs)),
            )
            app._confirm_room_apply()
            captured["cb"](True)
            assert jobs.launched == []
            assert notices == [
                (
                    "a TUI-managed worker is already active for this target",
                    {"severity": "error"},
                )
            ]
            assert app.query_one("#tabs").active != "runs-tab"
            await pilot.press("2")
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"

    drive(scenario())


def test_room_apply_requires_a_selected_candidate(tmp_path, git_repo, monkeypatch):
    async def scenario():
        jobs = _FakeJobs()
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_FakeRoomStore(selected_candidate_id=None))
            app._jobs = jobs
            app._selected_room_id = "room-a"
            pushed: list = []
            notices: list[tuple] = []
            monkeypatch.setattr(
                app, "push_screen", lambda *args, **kwargs: pushed.append(args)
            )
            monkeypatch.setattr(
                app,
                "notify",
                lambda message, **kwargs: notices.append((message, kwargs)),
            )
            app._confirm_room_apply()
            assert pushed == []
            assert jobs.launched == []
            assert notices == [
                ("select an eligible candidate first", {"severity": "error"})
            ]

    drive(scenario())


def test_job_row_labels_render_apply_ops():
    board = {
        "job_id": "0123456789ab",
        "status": "running",
        "op": "board-run",
        "request": {"mode": "loop"},
    }
    assert _job_label(board) == "01234567 running loop"
    room = {
        "job_id": "fedcba987654",
        "status": "starting",
        "op": "room-apply",
        "request": {"room_id": "room-abcdef123"},
    }
    assert _job_label(room) == "fedcba98 starting room-apply room-abc"
    artifact = {
        "job_id": "aaaabbbbcccc",
        "status": "finished",
        "op": "artifact-apply",
        "request": {"run_id": "run-1234567890"},
    }
    assert _job_label(artifact) == "aaaabbbb finished artifact-apply run-1234"
    legacy = {"job_id": "1111222233334", "status": "lost", "request": {"mode": "loop"}}
    assert _job_label(legacy) == "11112222 lost loop"


def test_rooms_refresh_reloads_config_and_empty_session_priority_is_safe(
    tmp_path, git_repo
):
    async def scenario():
        toml = git_repo / "syncmill.toml"
        toml.write_text(
            "agent_priority = []\n"
            'repo_root = "."\n'
            "[rooms]\n"
            "enabled = true\n"
            "[runs]\n"
            f'root = "{tmp_path / "runs"}"\n'
            "[workspace]\n"
            f'root = "{tmp_path / "workspaces"}"\n',
            encoding="utf-8",
        )
        app = SyncmillTuiApp(
            SyncmillConfig(
                agent_priority=[],
                rooms={"enabled": False},
                runs={"root": str(tmp_path / "old-runs")},
            ),
            board=None,
            board_error=None,
            toml_path=toml,
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            assert app._config.rooms.enabled is True
            assert app._rooms is not None
            # The bar tracks availability changes from an in-place retry, not
            # just tab switches (codex R2 Minor).
            assert "t terminal" in app.action_bar_text
            await pilot.press("6", "a")
            await pilot.pause()
            assert app.query_one("#tabs").active == "sessions-tab"

    drive(scenario())


_ARTIFACT_PATCH = """diff --git a/winner.txt b/winner.txt
new file mode 100644
--- /dev/null
+++ b/winner.txt
@@ -0,0 +1 @@
+winner
"""


def _seed_artifact(store, repo, run_id, *, strategy="interactive", plan=None):
    task = Task(prompt="make winner")
    store.begin(
        run_id=run_id,
        task=task,
        repo_root=repo,
        snapshot_commit="head",
        source_fingerprint="fp",
        strategy=strategy,
        agents=["codex"],
        plan=plan or {},
    )
    result = TaskResult(
        task_id=task.id,
        agent_id="codex",
        status=TaskStatus.COMPLETED,
        output="done",
        exit_code=0,
        duration_seconds=0.1,
        files_changed=["winner.txt"],
        patch=_ARTIFACT_PATCH,
    )
    return store.save(
        run_id=run_id,
        task=task,
        result=result,
        repo_root=repo,
        snapshot_commit="head",
        source_fingerprint="fp",
        strategy=strategy,
        plan=plan or {},
    )


def _artifact_pane_app(tmp_path, git_repo):
    return SyncmillTuiApp(
        SyncmillConfig(
            repo_root=str(git_repo),
            runs={"root": str(tmp_path / "runs")},
            tui={"state_dir": str(tmp_path / "jobs")},
        ),
        board=None,
        board_error=None,
        toml_path=git_repo / "syncmill.toml",
        refresh_seconds=3600,
    )


def test_artifact_label_marks_room_bound_runs():
    manifest = {"state": "pending", "agent_id": "codex", "strategy": "route"}
    label = _artifact_label("abcdef012345", manifest)
    assert label.startswith("abcdef01 pending")
    assert "route" in label
    room = {"state": "applied", "agent_id": "claude", "strategy": "room"}
    assert "room" in _artifact_label("abcdef012345", room)
    plan_bound = {"state": "pending", "plan": {"origin": "room"}}
    assert "room" in _artifact_label("abcdef012345", plan_bound)
    assert "?" in _artifact_label("abcdef012345", {})


def test_render_artifact_shows_error_before_output_and_degrades():
    manifest = {
        "run_id": "r1",
        "state": "failed",
        "strategy": "route",
        "agent_id": "codex",
        "created_at": "t",
        "patch_bytes": 0,
        "patch_sha256": "s",
        "files_changed": [],
    }
    assert "(no result recorded)" in render_artifact(manifest, None)
    text = render_artifact(
        manifest,
        {
            "status": "failed",
            "exit_code": 2,
            "duration_seconds": 1.0,
            "error": "boom",
            "output": "provider tail",
        },
    )
    assert text.index("Error: boom") < text.index("provider tail")


def test_runs_view_toggle_lists_artifacts_and_diff_switch(tmp_path, git_repo):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-artifact-1")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"
            assert app._runs_pane() == "operations-pane"
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._runs_pane() == "artifacts-pane"
            assert app.focused is not None and app.focused.id == "artifact-list"
            assert "g apply" in app.action_bar_text
            assert app._selected_run_id == "run-artifact-1"
            assert "Run: run-artifact-1" in app.artifact_detail_text
            assert "State: pending" in app.artifact_detail_text
            await pilot.press("d")
            await pilot.pause()
            assert app.artifact_detail_text.startswith("Diff run-arti")
            assert "+winner" in app.artifact_detail_text
            await pilot.press("d")
            await pilot.pause()
            assert "Run: run-artifact-1" in app.artifact_detail_text
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._runs_pane() == "operations-pane"
            assert app.focused is not None and app.focused.id == "job-list"

    drive(scenario())


def test_artifact_apply_launches_a_detached_job(tmp_path, git_repo, monkeypatch):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-artifact-2")
        jobs = _FakeJobs()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = jobs
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            captured: dict = {}
            monkeypatch.setattr(
                app, "push_screen", lambda screen, callback: captured.update(cb=callback)
            )
            app._confirm_artifact_apply()
            captured["cb"](True)
            await pilot.pause()
            spec, resolved = jobs.launched[0]
            assert isinstance(spec, ArtifactApplySpec)
            assert spec.run_id == "run-artifact-2"
            assert spec.verify is False
            assert resolved is app._runtime_config
            assert app._runs_pane() == "operations-pane"

    drive(scenario())


def test_artifact_apply_guards_room_bound_and_non_pending(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-room", strategy="room")
        _seed_artifact(app._artifacts, git_repo, "run-done")
        app._artifacts.discard("run-done")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            notices: list[str] = []
            pushed: list = []
            monkeypatch.setattr(
                app, "notify", lambda message, **kwargs: notices.append(message)
            )
            monkeypatch.setattr(
                app, "push_screen", lambda *args, **kwargs: pushed.append(args)
            )
            app._selected_run_id = "run-room"
            app._confirm_artifact_apply()
            assert any("room-bound" in message for message in notices)
            app._selected_run_id = "run-done"
            app._confirm_artifact_apply()
            assert any("only a pending run" in message for message in notices)
            assert pushed == []

    drive(scenario())


def test_artifact_discard_runs_off_the_event_loop(tmp_path, git_repo, monkeypatch):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-artifact-3")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            captured: dict = {}
            monkeypatch.setattr(
                app, "push_screen", lambda screen, callback: captured.update(cb=callback)
            )
            app._confirm_artifact_discard()
            captured["cb"](True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            manifest = app._artifacts.load("run-artifact-3").manifest
            assert manifest["state"] == "discarded"
            assert "State: discarded" in app.artifact_detail_text

    drive(scenario())


class _FakeSessionStore:
    def __init__(self, run_id):
        self._run_id = run_id

    def list(self):
        return []

    def checkpoint(self, session_id):
        return self._run_id


def test_checkpoint_lands_on_artifacts_with_the_run_selected(tmp_path, git_repo):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-checkpoint")
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_store = _FakeSessionStore("run-checkpoint")
            await pilot.press("6")
            await pilot.pause()
            app._selected_session_id = "sess-1"
            app.action_checkpoint()
            # checkpoint now runs in a thread worker (flock + git diff must
            # never block the event loop); wait for it before asserting.
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"
            assert app._runs_pane() == "artifacts-pane"
            assert app._selected_run_id == "run-checkpoint"
            assert "Run: run-checkpoint" in app.artifact_detail_text

    drive(scenario())


def test_checkpoint_refuses_a_second_press_while_one_is_running(tmp_path, git_repo):
    # exclusive=True cancels only the asyncio wrapper — it cannot stop a
    # callable already in the executor thread, so a second `c` mid-checkpoint
    # would serialize on the store lock and mint a DUPLICATE artifact. The
    # in-flight guard must refuse it.
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-checkpoint")
        release = threading.Event()
        calls: list[str] = []

        class _SlowStore:
            def list(self):
                return []

            def checkpoint(self, session_id):
                calls.append(session_id)
                release.wait(5)
                return "run-checkpoint"

        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_store = _SlowStore()
            await pilot.press("6")
            await pilot.pause()
            app._selected_session_id = "sess-1"
            app.action_checkpoint()
            app.action_checkpoint()  # mid-flight — refused, never a second call
            # Session close mid-checkpoint would block the EVENT LOOP on the
            # same session lock the checkpoint thread holds — the guard must
            # refuse it (the fake store has no load/close_session, so reaching
            # either would raise).
            app._confirm_session_close()
            app._apply_session_close(True)
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == ["sess-1"]
            # cleared on the EVENT LOOP (in _after_checkpoint), never in the
            # worker thread — a queued keypress in the done-but-not-yet-
            # notified window must still be refused
            assert app._checkpoint_running is False

    drive(scenario())


def _candidate_row(**overrides):
    """A CandidateRow with harmless defaults — the read model Details and
    Compare render from."""
    from syncmill.room_compare import CandidateRow, GateView

    values = dict(
        candidate_id="cand-1", member_id="claude", provider_id="claude",
        attempt=1, state="completed", eligible=True, selected=False,
        patch_sha256="p", base_commit="base", readable=True,
        unreadable_reason=None, gate=GateView("no_report"),
        duration_seconds=1.0, usage=None, files_changed=(), patch_bytes=4,
        patch_binary=False, patch_large=False, not_eligible_reason=None,
        apply_blockers=(), warnings=(), created_at="t", output_sha256="o",
        exit_code=0, output_bytes=0, output_truncated=False,
    )
    values.update(overrides)
    return CandidateRow(**values)


def _comparison(rows, *, round_number=3, room_id="room-a", revision=1):
    from syncmill.room_compare import (
        FileOverlap,
        GateProfile,
        RoomComparison,
        RoundGroup,
    )

    group = RoundGroup(
        number=round_number, round_id="round-1", state="complete",
        starting_tree="not_recorded", gate_profile=GateProfile("no_report"),
        rows=tuple(rows), missing=(),
        overlap=FileOverlap((), (), {}, {}, (), ()), warnings=(),
    )
    return RoomComparison(
        room_id=room_id, room_state="awaiting_decision", revision=revision,
        selected_candidate_id=None, source_status="current", source_reason=None,
        round_count=round_number, groups=(group,),
    )


def _fake_evidence(monkeypatch, rows, *, calls=None, **kwargs):
    """Replace the thread-side evidence builder with a canned comparison."""
    def build(store_root, output_max, repo_root, room_id, now):
        if calls is not None:
            calls.append(room_id)
        comparison = _comparison(rows, room_id=room_id, **kwargs)
        ids = frozenset(r.candidate_id for r in rows)
        return comparison, comparison.revision, ids

    monkeypatch.setattr("syncmill.tui.build_room_evidence", build)


class _FakeCandidateRoomStore:
    """Rooms store fake covering the candidate three-view read surface."""

    # Read by the evidence request; the builder itself is replaced in tests
    # (`_fake_evidence`), so nothing opens this path.
    root = "/nonexistent-room-store"
    candidates = type("_Candidates", (), {"output_max_bytes": 1})()

    def __init__(self, patch: bytes = b"diff --git a/x b/x\n+line\n" * 20_000):
        self.patch = patch
        self.rows = {
            "cand-ok": {
                "id": "cand-ok",
                "room_id": "room-a",
                "member_id": "claude",
                "state": "completed",
                "eligible": 1,
                "patch_sha256": "p-ok",
                "output_sha256": "o-ok",
            },
            "cand-bad": {
                "id": "cand-bad",
                "room_id": "room-a",
                "member_id": "codex",
                "state": "failed",
                "eligible": 0,
                "patch_sha256": "p-bad",
                "output_sha256": "o-bad",
            },
        }

    def list_rooms(self):
        return [{"id": "room-a", "state": "collected", "round_count": 1}]

    def get_room(self, room_id):
        return {
            "id": room_id,
            "state": "collected",
            "revision": 1,
            "members": [],
            "round_count": 1,
            "selected_candidate_id": None,
            "apply_run_id": None,
            "applied_run_id": None,
        }

    def list_candidates(self, room_id):
        return [dict(row) for row in self.rows.values()]

    def events(self, room_id):
        return []

    def get_candidate(self, candidate_id):
        return dict(self.rows[candidate_id])

    def get_round(self, round_id):
        return {"id": round_id, "number": 3}

    def list_rounds(self, room_id):
        return [{"id": "round-1", "number": 3}]

    def _manifest(self, candidate_id):
        row = self.rows[candidate_id]
        return {
            "candidate_id": candidate_id,
            "round_id": "round-1",
            "member_id": row["member_id"],
            "provider_id": row["member_id"],
            "attempt": 1,
            "state": row["state"],
            "eligible": bool(row["eligible"]),
            "patch_bytes": len(self.patch),
            "patch_sha256": row["patch_sha256"],
            "output_bytes": 9,
            "output_sha256": row["output_sha256"],
            "output_truncated": False,
            "files_changed": ["x"],
            "exit_code": 0 if row["state"] == "completed" else 2,
            "duration_seconds": 1.5,
            "error": None if row["state"] == "completed" else "boom",
            "gate": None,
            "provider_session_id": None,
            "created_at": "t",
            "base_commit": "c",
        }

    def load_candidate_patch_head(self, candidate_id, max_patch_bytes):
        return {
            **self._manifest(candidate_id),
            "patch_head": self.patch[:max_patch_bytes],
            "patch_truncated": len(self.patch) > max_patch_bytes,
        }

    def load_candidate_checkpoint(self, candidate_id, max_output_bytes):
        # The trailing broken markup guards the markup=False contract of
        # #candidate-detail (raw provider output must never crash the view).
        return {**self._manifest(candidate_id), "output": "provider said hi [/oops]"}


def test_candidate_three_view_switching_and_paging(tmp_path, git_repo, monkeypatch):
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_FakeCandidateRoomStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            _fake_evidence(
                monkeypatch,
                [_candidate_row(candidate_id="cand-ok"), _candidate_row(candidate_id="cand-bad", member_id="codex", state="failed", eligible=False)],
            )
            await pilot.press("7")
            await pilot.pause()
            # A completed candidate opens on Diff (paged, truncation marked).
            assert app._selected_candidate_id == "cand-ok"
            assert "[Diff]" in app.candidate_detail_text
            assert "truncated" in app.candidate_detail_text
            await pilot.press("o")
            await pilot.pause()
            assert "[Output]" in app.candidate_detail_text
            assert "provider said hi" in app.candidate_detail_text
            await pilot.press("m")
            await _settle(app, pilot)
            assert "[Details]" in app.candidate_detail_text
            assert "Round: 3" in app.candidate_detail_text
            assert "evidence as of" in app.candidate_detail_text
            # d returns to Diff; a second d pages more of a truncated patch.
            await pilot.press("d")
            await pilot.pause()
            assert "[Diff]" in app.candidate_detail_text
            assert app._diff_pages == 1
            await pilot.press("d")
            await pilot.pause()
            assert app._diff_pages == 2

    drive(scenario())


def test_failed_candidate_defaults_to_output_view(tmp_path, git_repo, monkeypatch):
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_FakeCandidateRoomStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            options = app.query_one("#candidate-list", OptionList)
            options.highlighted = 1
            await pilot.pause()
            assert app._selected_candidate_id == "cand-bad"
            assert "[Output]" in app.candidate_detail_text
            # The failure is shown before the provider output.
            assert app.candidate_detail_text.index("Error: boom") < (
                app.candidate_detail_text.index("provider said hi")
            )

    drive(scenario())


def test_room_close_key_confirms_and_closes(tmp_path, git_repo, monkeypatch):
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            closed: list[str] = []
            rooms = _FakeRooms(store=_FakeCandidateRoomStore())
            rooms.close_room = closed.append
            app._rooms = rooms
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            captured: dict = {}
            monkeypatch.setattr(
                app, "push_screen", lambda screen, callback: captured.update(cb=callback)
            )
            await pilot.press("k")
            await pilot.pause()
            captured["cb"](True)
            assert closed == ["room-a"]

    drive(scenario())


def test_rooms_recover_key_uses_the_room_recover_cli(tmp_path, git_repo, monkeypatch):
    async def scenario():
        captured: dict = {}

        class FakeProc:
            pid = 424242
            returncode = 0

            async def communicate(self):
                return (b"No dead room worker or interrupted apply to recover\n", b"")

            async def wait(self):
                return 0

        async def fake_exec(*args, **kwargs):
            captured["argv"] = args
            return FakeProc()

        monkeypatch.setattr("syncmill.tui.asyncio.create_subprocess_exec", fake_exec)
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_FakeCandidateRoomStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("R")
            await app.workers.wait_for_complete()
            argv = captured["argv"]
            assert "room" in argv and "recover" in argv and "--repo" in argv

    drive(scenario())


def test_candidate_renderers_cover_binary_and_truncation():
    from syncmill.room_compare import GateCheckView, GateView
    from syncmill.tui_collaboration import (
        _patch_is_binary,
        render_candidate_details,
        render_candidate_diff,
    )

    assert _patch_is_binary(b"GIT binary patch")
    assert _patch_is_binary(b"Binary files a and b differ")
    assert _patch_is_binary(b"x\x00y")
    assert not _patch_is_binary(b"diff --git a/x b/x\n+line\n")
    manifest = {"candidate_id": "cand-1", "patch_bytes": 4, "patch_sha256": "p"}
    diff = render_candidate_diff(manifest, b"\x00bin", truncated=True)
    assert "[contains binary patch content]" in diff
    assert "load more" in diff
    row = _candidate_row(
        patch_binary=True,
        output_truncated=True,
        eligible=False,
        not_eligible_reason="quality gate failed",
        gate=GateView("failed", checks=(GateCheckView("pytest -q", False, 1, 2.0),)),
    )
    details = render_candidate_details(row, round_number=None)
    assert "Patch: 4 bytes (binary)" in details
    assert "Round: ?" in details
    assert "× pytest -q (exit 1, 2.0s)" in details
    assert "truncated: True" in details
    assert "eligible: no (quality gate failed)" in details

def test_candidate_renderers_tolerate_explicit_null_fields():
    # A manifest.json with explicit `"output": null` reaches the Output
    # renderer verbatim; len(None) would blank the pane via the poll's
    # TypeError catch. Details reads the read model, where an unknown file set
    # is None and must render as "unknown", never crash or read as empty.
    from syncmill.tui_collaboration import (
        render_candidate_details,
        render_candidate_output,
    )

    details = render_candidate_details(
        _candidate_row(files_changed=None, patch_bytes=None, exit_code=None,
                       output_sha256=None, created_at=None, attempt=None),
        round_number=None,
    )
    assert "Files: unknown" in details
    assert "Patch: unknown" in details
    assert "attempt: ?" in details
    output = render_candidate_output(
        {"candidate_id": "cand-null", "state": "failed", "exit_code": 1,
         "duration_seconds": 1.0, "output": None}
    )
    assert "(no output)" in output

def test_store_bindings_are_independent(tmp_path, git_repo, monkeypatch):
    # codex R2 Major: a corrupt Rooms store must not disable the
    # filesystem-only Artifacts surface (they shared one try block).
    def boom(*args, **kwargs):
        raise RuntimeError("rooms sqlite is corrupt")

    monkeypatch.setattr("syncmill.tui.RoomCoordinator", boom)
    app = SyncmillTuiApp(
        SyncmillConfig(
            repo_root=str(git_repo),
            rooms={"enabled": True},
            runs={"root": str(tmp_path / "runs")},
            tui={"state_dir": str(tmp_path / "jobs")},
        ),
        board=None,
        board_error=None,
        toml_path=git_repo / "syncmill.toml",
        refresh_seconds=3600,
    )
    assert app._rooms is None
    assert "rooms sqlite is corrupt" in (app._collaboration_error or "")
    assert app._artifacts is not None
    assert app._session_store is not None
    assert app._runtime_config is not None


def test_artifact_views_never_parse_markup(tmp_path, git_repo):
    # codex R2 Major: raw agent output/diffs (e.g. `[/oops]`) must never hit
    # Textual's markup parser.
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        task = Task(prompt="make winner")
        app._artifacts.begin(
            run_id="run-markup",
            task=task,
            repo_root=git_repo,
            snapshot_commit="head",
            source_fingerprint="fp",
            strategy="interactive",
            agents=["codex"],
            plan={},
        )
        app._artifacts.save(
            run_id="run-markup",
            task=task,
            result=TaskResult(
                task_id=task.id,
                agent_id="codex",
                status=TaskStatus.COMPLETED,
                output="raw [/oops] output [red]not markup",
                exit_code=0,
                duration_seconds=0.1,
                files_changed=["x"],
                patch="diff --git a/x b/x\n+[/oops] [bold]raw\n",
            ),
            repo_root=git_repo,
            snapshot_commit="head",
            source_fingerprint="fp",
            strategy="interactive",
            plan={},
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "[/oops]" in app.artifact_detail_text
            await pilot.press("d")
            await pilot.pause()
            assert "[/oops]" in app.artifact_detail_text

    drive(scenario())


def test_force_stop_is_scoped_to_the_operations_pane(tmp_path, git_repo, monkeypatch):
    # codex R2 Major: from Artifacts, K must not force-stop the hidden
    # Operations selection.
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-artifact-k")
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Button, Checkbox

            from syncmill.tui_collaboration import CollaborationConfirmModal

            controlled: list[tuple[str, str]] = []
            # Real schema-2 rows nest operation fields under "request"
            # (JobStore stores `"request": asdict(spec)`); the modal must read
            # the target from there, not from flattened top-level keys.
            monkeypatch.setattr(
                app,
                "_selected_job",
                lambda: {
                    "job_id": "job-force-1",
                    "op": "artifact-apply",
                    "request": {"run_id": "run-artifact-k"},
                },
            )
            monkeypatch.setattr(
                app,
                "_control_job",
                lambda job_id, action: controlled.append((job_id, action)),
            )
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("K")
            await pilot.pause()
            assert controlled == []
            assert not isinstance(app.screen, CollaborationConfirmModal)
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("K")
            await pilot.pause()
            assert isinstance(app.screen, CollaborationConfirmModal)
            detail = "\n".join(
                str(widget.render()) for widget in app.screen.query("Static")
            )
            assert "job-force-1" in detail
            assert "artifact-apply" in detail
            assert "run-artifact-k" in detail
            assert controlled == []
            app.screen.query_one("#confirmed", Checkbox).value = True
            app.screen.query_one("#apply", Button).press()
            await pilot.pause()
            assert controlled == [("job-force-1", "force")]

    drive(scenario())


def test_force_stop_without_selection_notifies_error_and_opens_no_modal(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            notices: list[tuple[str, dict]] = []
            monkeypatch.setattr(
                app,
                "notify",
                lambda message, **kwargs: notices.append((message, kwargs)),
            )
            await pilot.press("K")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert notices[-1][0] == "select a job in the Runs tab first"
            assert notices[-1][1]["severity"] == "error"

    drive(scenario())


def test_status_refresh_reloads_configuration(tmp_path, monkeypatch):
    # The guide documents Status `r` as re-reading configuration; without the
    # reload the displayed priority/enabled/timeouts stay cached and can
    # disagree with the memory probe.
    async def scenario():
        toml = tmp_path / "syncmill.toml"
        toml.write_text(
            'config_version = 2\nagent_priority = ["codex"]\n'
            "[codex]\ntimeout_seconds = 111\n",
            encoding="utf-8",
        )
        from syncmill.teamfile import load_config_at

        app = SyncmillTuiApp(
            load_config_at(toml),
            board=None,
            board_error=None,
            toml_path=toml,
            refresh_seconds=3600,
        )
        # Status `r` also launches the real `syncmill status` probe subprocess;
        # keep the test hermetic (and free of worker-teardown races).
        monkeypatch.setattr(app, "_start_memory_probe", lambda: None)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("5")
            await pilot.pause()
            from textual.widgets import Static

            assert "timeout=111s" in str(
                app.query_one("#agent-status", Static).render()
            )
            toml.write_text(
                'config_version = 2\nagent_priority = ["codex"]\n'
                "[codex]\ntimeout_seconds = 222\n",
                encoding="utf-8",
            )
            await pilot.press("r")
            await pilot.pause()
            assert "timeout=222s" in str(
                app.query_one("#agent-status", Static).render()
            )

    drive(scenario())


def test_status_refresh_survives_a_malformed_toml(tmp_path, monkeypatch):
    # A mid-edit syncmill.toml raises tomllib.TOMLDecodeError from
    # load_config_at; the reload must keep the cached view + error toast, not
    # propagate through Textual and kill the TUI.
    async def scenario():
        toml = tmp_path / "syncmill.toml"
        toml.write_text(
            'config_version = 2\nagent_priority = ["codex"]\n'
            "[codex]\ntimeout_seconds = 111\n",
            encoding="utf-8",
        )
        from syncmill.teamfile import load_config_at

        app = SyncmillTuiApp(
            load_config_at(toml),
            board=None,
            board_error=None,
            toml_path=toml,
            refresh_seconds=3600,
        )
        monkeypatch.setattr(app, "_start_memory_probe", lambda: None)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("5")
            await pilot.pause()
            toml.write_text("config_version = [broken\n", encoding="utf-8")
            await pilot.press("r")
            await pilot.pause()
            from textual.widgets import Static

            assert "timeout=111s" in str(
                app.query_one("#agent-status", Static).render()
            )

    drive(scenario())


def test_status_refresh_survives_an_unresolvable_repo_root(tmp_path, monkeypatch):
    # A syntactically VALID edit can still make the rebind blow up: a
    # symlink-loop repo_root raises RuntimeError (3.12) or surfaces as
    # OSError (3.13+) from _job_project_root's resolve. The reload must
    # degrade Runs to limited mode, never propagate and kill the TUI.
    async def scenario():
        toml = tmp_path / "syncmill.toml"
        toml.write_text(
            'config_version = 2\nagent_priority = ["codex"]\n', encoding="utf-8"
        )
        loop_a = tmp_path / "loop-a"
        loop_b = tmp_path / "loop-b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        from syncmill.teamfile import load_config_at

        app = SyncmillTuiApp(
            load_config_at(toml),
            board=None,
            board_error=None,
            toml_path=toml,
            refresh_seconds=3600,
        )
        monkeypatch.setattr(app, "_start_memory_probe", lambda: None)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("5")
            await pilot.pause()
            toml.write_text(
                'config_version = 2\nagent_priority = ["codex"]\n'
                f'repo_root = "{loop_a}"\n',
                encoding="utf-8",
            )
            await pilot.press("r")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            from textual.widgets import Static

            assert "codex" in str(app.query_one("#agent-status", Static).render())

    drive(scenario())


def test_unavailable_session_and_room_add_do_not_open_modals(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        config = SyncmillConfig(
            rooms={"enabled": False},
            runs={"root": str(tmp_path / "runs")},
            workspace={"root": str(tmp_path / "workspaces")},
            tui={"state_dir": str(tmp_path / "jobs")},
        )
        app = SyncmillTuiApp(
            config,
            board=None,
            board_error=None,
            toml_path=git_repo / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app._session_store = None
            app._rooms = None
            app._collaboration_error = "collaboration store unavailable"
            warnings = []
            monkeypatch.setattr(
                app,
                "notify",
                lambda message, **kwargs: warnings.append((message, kwargs)),
            )

            await pilot.press("6", "a")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert warnings[-1][0] == "collaboration store unavailable"

            await pilot.press("7", "a")
            await pilot.pause()
            assert len(app.screen_stack) == 1
            assert "unavailable" in warnings[-1][0].lower()

    drive(scenario())


def test_room_create_modal_explains_that_calls_start_with_round(
    tmp_path, git_repo
):
    from syncmill.tui_collaboration import RoomCreateModal

    async def scenario():
        config = SyncmillConfig(
            agent_priority=["codex"],
            rooms={"enabled": True},
            runs={"root": str(tmp_path / "runs")},
            workspace={"root": str(tmp_path / "workspaces")},
            tui={"state_dir": str(tmp_path / "jobs")},
        )
        app = SyncmillTuiApp(
            config,
            board=None,
            board_error=None,
            toml_path=git_repo / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(RoomCreateModal(config))
            await pilot.pause()
            text = "\n".join(
                str(widget.render()) for widget in app.screen.query("Static")
            )
            assert "without calling a provider" in text
            assert "Starting a round with x" in text
            assert not app.screen.query("#confirmed")

    drive(scenario())


def test_artifact_diff_marks_binary_content_beyond_the_display_cap(
    tmp_path, git_repo
):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        filler = "+filler line for a large textual diff\n" * 8000
        patch = f"diff --git a/x b/x\n{filler}GIT binary patch\nliteral 10\n"
        assert len(patch) > 256 * 1024
        _seed_artifact(app._artifacts, git_repo, "run-late-binary")
        # Rewrite the stored patch with the late-binary variant and rebind the
        # digest so patch() verification still passes.
        import hashlib
        import json as json_module

        run_dir = app._artifacts.root / "run-late-binary"
        (run_dir / "winner.patch").write_bytes(patch.encode())
        manifest_path = run_dir / "manifest.json"
        manifest = json_module.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["patch_sha256"] = hashlib.sha256(patch.encode()).hexdigest()
        manifest["patch_bytes"] = len(patch.encode())
        manifest_path.write_text(json_module.dumps(manifest), encoding="utf-8")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert "[contains binary patch content]" in app.artifact_detail_text
            assert "display truncated" in app.artifact_detail_text

    drive(scenario())


def test_read_artifact_result_distinguishes_oversized_and_corrupt(
    tmp_path, monkeypatch
):
    from syncmill.tui import _read_artifact_result
    from syncmill.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path, "project")
    run = store.root / "run-1"
    run.mkdir()
    (run / "manifest.json").write_text('{"run_id": "run-1"}')

    assert _read_artifact_result("run-1", store=store) is None
    (run / "result.json").write_text("{not json", encoding="utf-8")
    corrupt = _read_artifact_result("run-1", store=store)
    assert corrupt is not None and "unreadable" in corrupt["status"]
    monkeypatch.setattr("syncmill.tui._RESULT_LOAD_BYTES", 4)
    (run / "result.json").write_text('{"status": "ok"}', encoding="utf-8")
    oversized = _read_artifact_result("run-1", store=store)
    assert oversized is not None and "too large" in oversized["status"]
    # The sentinel renders as a status line, never "(no result recorded)".
    text = render_artifact({"run_id": "r", "files_changed": []}, oversized)
    assert "too large" in text and "no result recorded" not in text


class _FakeManyJobs:
    def __init__(self, count=30):
        self.rows = [
            {
                "job_id": f"job{i:04d}beef",
                "job_dir": f"/nonexistent/fake-jobs/{i}",
                "status": "finished",
                "exit_code": 0,
                "cwd": "/tmp",
                "op": "board-run",
                "request": {"mode": "loop"},
            }
            for i in range(count)
        ]

    def list_jobs(self):
        return [dict(row) for row in self.rows]

    def active_job(self):
        return None


def test_poll_preserves_selection_scroll_and_widget_identity(tmp_path):
    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(
                board={"enabled": False}, tui={"state_dir": str(tmp_path / "jobs")}
            ),
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"
            app._jobs = _FakeManyJobs()
            app._refresh_jobs()
            await pilot.pause()
            options = app.query_one("#job-list", OptionList)
            options.highlighted = 12
            await pilot.pause()
            options.scroll_to(y=8, animate=False)
            await pilot.pause()
            first_option = options.get_option_at_index(0)
            focused = app.focused
            # Two identical poll ticks: the widget must not be rebuilt, so
            # highlight, scroll offset, and focus survive untouched.
            app._poll()
            await pilot.pause()
            app._poll()
            await pilot.pause()
            assert options.get_option_at_index(0) is first_option
            assert options.highlighted == 12
            assert options.scroll_offset.y == 8
            assert app.focused is focused
            assert app._selected_job_dir == "/nonexistent/fake-jobs/12"
            # Changed content rebuilds but follows the selected id.
            app._jobs.rows.insert(
                0,
                {
                    "job_id": "jobnewbeef",
                    "job_dir": "/nonexistent/fake-jobs/new",
                    "status": "running",
                    "exit_code": None,
                    "cwd": "/tmp",
                    "op": "board-run",
                    "request": {"mode": "loop"},
                },
            )
            app._poll()
            await pilot.pause()
            assert options.get_option_at_index(0) is not first_option
            assert app._selected_job_dir == "/nonexistent/fake-jobs/12"
            assert options.highlighted == 13

    drive(scenario())


def test_board_cursor_and_scroll_survive_polling(tmp_path):
    async def scenario():
        board = Board(str(tmp_path / "board.db"))
        first = board.add("task one")
        second = board.add("task two")
        board.add("task three")
        app = SyncmillTuiApp(
            SyncmillConfig(
                board={"enabled": True, "db_path": str(tmp_path / "board.db")},
                tui={"state_dir": str(tmp_path / "jobs")},
            ),
            board=board,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
            initial_tab="board",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#board-table", DataTable)
            assert table.row_count == 3
            table.move_cursor(row=1)
            await pilot.pause()
            assert app._selected_item_id == second.id
            app._poll()
            await pilot.pause()
            app._poll()
            await pilot.pause()
            # Unchanged content: the cursor stays where the user put it.
            assert table.cursor_coordinate.row == 1
            assert app._selected_item_id == second.id
            # A content change rebuilds and restores the cursor by item id.
            board.transition(first.id, "blocked", actor="human", note="wait")
            app._poll()
            await pilot.pause()
            assert app._selected_item_id == second.id
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            assert str(row_key.value) == second.id

    drive(scenario())


def test_open_confirm_modal_survives_polling_and_keeps_the_captured_spec(
    tmp_path, git_repo, monkeypatch
):
    from textual.widgets import Button, Checkbox

    from syncmill.tui_collaboration import CollaborationConfirmModal

    class _PollableApplyStore(_FakeCandidateRoomStore):
        def get_room(self, room_id):
            return {
                **super().get_room(room_id),
                "revision": 7,
                "selected_candidate_id": "cand-ok",
            }

        def load_candidate(self, candidate_id):
            return {"candidate_id": candidate_id, "patch_sha256": "p-ok"}

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        jobs = _FakeJobs()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_PollableApplyStore())
            app._jobs = jobs
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            app._confirm_room_apply()
            await pilot.pause()
            assert isinstance(app.screen, CollaborationConfirmModal)
            app.screen.query_one("#confirmed", Checkbox).value = True
            # Polling continues beneath an open modal and must not disturb it.
            for _ in range(3):
                app._poll()
                await pilot.pause()
            assert isinstance(app.screen, CollaborationConfirmModal)
            assert app.screen.query_one("#confirmed", Checkbox).value is True
            app.screen.query_one("#apply", Button).press()
            await pilot.pause()
            spec, _resolved = jobs.launched[0]
            assert spec.room_id == "room-a"
            assert spec.expected_candidate_id == "cand-ok"
            assert spec.expected_revision == 7

    drive(scenario())


def test_room_reindex_during_polling_keeps_the_selected_candidate(
    tmp_path, git_repo, monkeypatch
):
    # codex R3 Major: a rebuild that re-highlights the SAME room at a new
    # index (another room inserted above it) must not reset the selected
    # candidate or its view.
    class _GrowingRoomStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.rooms = [{"id": "room-a", "state": "collected", "round_count": 1}]

        def list_rooms(self):
            return [dict(room) for room in self.rooms]

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _GrowingRoomStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            _fake_evidence(
                monkeypatch,
                [_candidate_row(candidate_id="cand-ok"), _candidate_row(candidate_id="cand-bad", member_id="codex", state="failed", eligible=False)],
            )
            await pilot.press("7")
            await pilot.pause()
            options = app.query_one("#candidate-list", OptionList)
            options.highlighted = 1
            await pilot.pause()
            await pilot.press("m")
            await _settle(app, pilot)
            assert app._selected_candidate_id == "cand-bad"
            assert "[Details]" in app.candidate_detail_text
            # A newer room lands ABOVE the selected one; polling rebuilds the
            # room list and re-highlights room-a at index 1.
            store.rooms.insert(
                0, {"id": "room-new", "state": "collected", "round_count": 0}
            )
            app._poll()
            await pilot.pause()
            assert app._selected_room_id == "room-a"
            assert app._selected_candidate_id == "cand-bad"
            assert app._candidate_view == "details"
            assert "[Details]" in app.candidate_detail_text

    drive(scenario())


def test_rooms_poll_degrades_and_keeps_snapshot_on_store_failure(
    tmp_path, git_repo, monkeypatch
):
    # PR #78 review pre-merge finding: a mid-session rooms-store failure
    # (corrupt SQLite page, corrupt members_json) must degrade the Rooms
    # surface — DEGRADED status, last snapshot kept — never raise through the
    # poll timer and kill the app (the _refresh_board invariant).
    class _FailingRoomStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.broken = False

        def list_rooms(self):
            if self.broken:
                raise sqlite3.OperationalError("database disk image is malformed")
            return super().list_rooms()

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _FailingRoomStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            detail_before = str(app.query_one("#room-detail").render())
            assert detail_before  # a room rendered
            assert "[Diff]" in app.candidate_detail_text
            # The store breaks mid-session; the next poll must not raise.
            store.broken = True
            app._poll()
            await pilot.pause()
            status = str(app.query_one("#room-status").render())
            assert "DEGRADED" in status and "malformed" in status
            # Last snapshot stays on screen.
            assert str(app.query_one("#room-detail").render()) == detail_before
            assert "[Diff]" in app.candidate_detail_text
            # Recovery: the store heals, the next poll re-renders normally.
            store.broken = False
            app._poll()
            await pilot.pause()
            assert "rooms:" in str(app.query_one("#room-status").render())

    drive(scenario())


def test_rooms_detail_read_failure_degrades_status_and_keeps_detail(
    tmp_path, git_repo, monkeypatch
):
    # codex review of the PR #78 fixes: when list_rooms succeeds but a
    # SECONDARY read (get_room / candidate load) fails, the caught error must
    # not overwrite the detail pane while the parent reports a healthy
    # rooms:N — the status flips to DEGRADED and both detail panes keep their
    # last snapshot.
    class _DetailFailingStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.broken = False

        def get_room(self, room_id):
            if self.broken:
                raise sqlite3.OperationalError("room row unreadable")
            return super().get_room(room_id)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _DetailFailingStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            detail_before = str(app.query_one("#room-detail").render())
            candidate_before = str(app.query_one("#candidate-detail").render())
            assert detail_before and candidate_before
            store.broken = True
            app._poll()
            await pilot.pause()
            status = str(app.query_one("#room-status").render())
            assert "DEGRADED" in status and "unreadable" in status
            assert "rooms:" not in status
            assert str(app.query_one("#room-detail").render()) == detail_before
            assert (
                str(app.query_one("#candidate-detail").render()) == candidate_before
            )

    drive(scenario())


def test_rooms_late_candidate_read_failure_keeps_both_panes(
    tmp_path, git_repo, monkeypatch
):
    # codex R2: _show_room used to commit the new room detail and candidate
    # list BEFORE the candidate's own reads — a late get_candidate/patch-head
    # failure left a fresh room pane beside a stale candidate pane. The whole
    # room+candidate refresh must commit atomically: on a late failure both
    # panes keep their snapshot even though the room data changed.
    class _LateFailingStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.broken = False

        def get_room(self, room_id):
            room = super().get_room(room_id)
            if self.broken:
                room["state"] = "applying"  # the room pane WOULD change
            return room

        def get_candidate(self, candidate_id):
            row = super().get_candidate(candidate_id)
            if self.broken:
                row["patch_sha256"] = "p-new"  # key changes → must re-load
            return row

        def load_candidate_patch_head(self, candidate_id, max_patch_bytes):
            if self.broken:
                raise sqlite3.OperationalError("candidate artifact unreadable")
            return super().load_candidate_patch_head(candidate_id, max_patch_bytes)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _LateFailingStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            detail_before = str(app.query_one("#room-detail").render())
            candidate_before = str(app.query_one("#candidate-detail").render())
            assert "collected" in detail_before and "[Diff]" in candidate_before
            store.broken = True
            app._poll()
            await pilot.pause()
            status = str(app.query_one("#room-status").render())
            assert "DEGRADED" in status and "unreadable" in status
            assert "rooms:" not in status
            # Neither pane moved: no fresh room detail beside a stale candidate.
            assert str(app.query_one("#room-detail").render()) == detail_before
            assert (
                str(app.query_one("#candidate-detail").render()) == candidate_before
            )

    drive(scenario())


def test_rooms_late_failure_keeps_selection_matching_visible_panes(
    tmp_path, git_repo, monkeypatch
):
    # codex R3: _refresh_rooms used to commit _selected_room_id (and _show_room
    # the candidate selection) BEFORE detail staging succeeded — a late read
    # failure left old panes visible while room/candidate actions targeted the
    # newly selected ids. Selection state must stay matching the visible panes.
    class _VanishingRoomStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.broken = False

        def list_rooms(self):
            if self.broken:
                return [{"id": "room-b", "state": "collected", "round_count": 1}]
            return [{"id": "room-a", "state": "collected", "round_count": 1}]

        def list_candidates(self, room_id):
            if self.broken:
                return [
                    {
                        "id": "cand-new",
                        "room_id": "room-b",
                        "member_id": "claude",
                        "state": "completed",
                        "eligible": 1,
                        "patch_sha256": "p-new",
                        "output_sha256": "o-new",
                    }
                ]
            return super().list_candidates(room_id)

        def get_candidate(self, candidate_id):
            if candidate_id == "cand-new":
                return dict(self.list_candidates("room-b")[0])
            return super().get_candidate(candidate_id)

        def load_candidate_patch_head(self, candidate_id, max_patch_bytes):
            if self.broken:
                raise sqlite3.OperationalError("candidate artifact unreadable")
            return super().load_candidate_patch_head(candidate_id, max_patch_bytes)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _VanishingRoomStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            assert app._selected_room_id == "room-a"
            assert app._selected_candidate_id == "cand-ok"
            detail_before = str(app.query_one("#room-detail").render())
            # room-a vanishes, room-b appears — but its candidate read fails.
            store.broken = True
            app._poll()
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#room-status").render())
            # Selection still matches what is on screen, not the failed stage.
            assert app._selected_room_id == "room-a"
            assert app._selected_candidate_id == "cand-ok"
            assert str(app.query_one("#room-detail").render()) == detail_before

    drive(scenario())


def test_room_rebuild_highlight_event_does_not_restage_the_room(
    tmp_path, git_repo, monkeypatch
):
    # codex R3: rebuilding the room OptionList queues an OptionHighlighted for
    # the SAME room id; the handler must short-circuit (the job-list rule)
    # instead of re-reading the store — a second snapshot right after an
    # atomic refresh could tear it or degrade a healthy surface.
    class _CountingRoomStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.get_room_calls = 0
            self.rooms = [{"id": "room-a", "state": "collected", "round_count": 1}]

        def list_rooms(self):
            return [dict(room) for room in self.rooms]

        def get_room(self, room_id):
            self.get_room_calls += 1
            return super().get_room(room_id)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _CountingRoomStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            baseline = store.get_room_calls
            # A newer room lands ABOVE the selected one: the list rebuilds and
            # re-highlights room-a. Exactly ONE staging read for the poll —
            # the queued same-id highlight must not re-stage the room.
            store.rooms.insert(
                0, {"id": "room-new", "state": "collected", "round_count": 0}
            )
            app._poll()
            await pilot.pause()
            assert store.get_room_calls == baseline + 1

    drive(scenario())


def test_candidates_returning_after_empty_re_render(tmp_path, git_repo, monkeypatch):
    # codex R3: committing "(no candidates yet)" replaced the candidate pane
    # WITHOUT invalidating _candidate_detail_key — when the same candidate
    # returned, staging matched the stale key and left "(no candidates yet)"
    # on screen forever.
    class _EmptyingStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.empty = False

        def list_candidates(self, room_id):
            return [] if self.empty else super().list_candidates(room_id)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _EmptyingStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            assert "[Diff]" in str(app.query_one("#candidate-detail").render())
            store.empty = True
            app._poll()
            await pilot.pause()
            assert "(no candidates yet)" in str(
                app.query_one("#candidate-detail").render()
            )
            # The same candidate returns — the pane must re-render, not stay
            # short-circuited on the stale cache key.
            store.empty = False
            app._poll()
            await pilot.pause()
            assert "[Diff]" in str(app.query_one("#candidate-detail").render())

    drive(scenario())


class _TwoRoomStore(_FakeCandidateRoomStore):
    """Two rooms; room-b's reads fail — the A→B navigation-failure fixture."""

    def __init__(self):
        super().__init__()
        self.broken_room = "room-b"

    def list_rooms(self):
        return [
            {"id": "room-a", "state": "collected", "round_count": 1},
            {"id": "room-b", "state": "collected", "round_count": 1},
        ]

    def get_room(self, room_id):
        if room_id == self.broken_room:
            raise sqlite3.OperationalError("room-b unreadable")
        return super().get_room(room_id)


def test_room_navigation_failure_keeps_selection_and_restores_highlight(
    tmp_path, git_repo, monkeypatch
):
    # codex R4: the room handler used to reset candidate state and assign the
    # new room BEFORE _show_room succeeded — with room A still visible, k/x/g
    # actions then targeted room B. Selection commits only after staging; a
    # failed stage restores the highlight to the visible room.
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _TwoRoomStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            assert app._selected_room_id == "room-a"
            assert app._selected_candidate_id == "cand-ok"
            options = app.query_one("#room-list", OptionList)
            options.highlighted = 1  # the user moves to room-b; its stage fails
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#room-status").render())
            # Actions still target what is on screen.
            assert app._selected_room_id == "room-a"
            assert app._selected_candidate_id == "cand-ok"
            assert options.highlighted == 0  # highlight restored

    drive(scenario())


def test_candidate_navigation_failure_keeps_selection_and_restores_highlight(
    tmp_path, git_repo, monkeypatch
):
    class _BrokenBadCandidateStore(_FakeCandidateRoomStore):
        def load_candidate_checkpoint(self, candidate_id, max_output_bytes):
            if candidate_id == "cand-bad":
                raise sqlite3.OperationalError("cand-bad output unreadable")
            return super().load_candidate_checkpoint(candidate_id, max_output_bytes)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _BrokenBadCandidateStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            assert app._selected_candidate_id == "cand-ok"
            options = app.query_one("#candidate-list", OptionList)
            # cand-bad is state=failed → opens on Output, whose read fails.
            options.highlighted = 1
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#room-status").render())
            assert app._selected_candidate_id == "cand-ok"
            assert options.highlighted == 0

    drive(scenario())


def test_session_navigation_failure_keeps_selection_and_restores_highlight(
    tmp_path, git_repo, monkeypatch
):
    class _TwoSessionStore:
        def __init__(self):
            self.fail_next_list = False

        def list(self):
            if self.fail_next_list:
                raise IsolatedSessionError("session index unreadable")
            return [
                {
                    "session_id": f"sess-{n}",
                    "agent_id": "claude",
                    "state": "open",
                    "live": True,
                    "source_stale": False,
                    "worktree_path": "/w",
                    "checkpoints": [],
                }
                for n in (1, 2)
            ]

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _TwoSessionStore()
            app._session_store = store
            app._refresh_sessions()
            await pilot.pause()
            assert app._selected_session_id == "sess-1"
            options = app.query_one("#session-list", OptionList)
            store.fail_next_list = True  # the navigation's own read fails
            options.highlighted = 1
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#session-status").render())
            assert app._selected_session_id == "sess-1"
            assert options.highlighted == 0

    drive(scenario())


def test_restore_highlight_clears_when_previous_selection_is_gone(
    tmp_path, git_repo, monkeypatch
):
    # codex R5: when the previous selection is None or its row vanished,
    # _restore_highlight must CLEAR the highlight — leaving the failed
    # destination highlighted would desync it from the committed selection.
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _TwoRoomStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            options = app.query_one("#room-list", OptionList)
            # Existing id → restored to its index.
            app._restore_highlight(options, "room-a")
            assert options.highlighted == 0
            # Vanished id → cleared, not left on the failed destination.
            options.highlighted = 1
            await pilot.pause()
            app._restore_highlight(options, "room-gone")
            assert options.highlighted is None
            # No previous selection → cleared as well.
            options.highlighted = 1
            await pilot.pause()
            app._restore_highlight(options, None)
            assert options.highlighted is None

    drive(scenario())


def test_room_switch_to_empty_room_resets_candidate_view(
    tmp_path, git_repo, monkeypatch
):
    # codex R5 minor: a committed room CHANGE landing on an empty candidate
    # list must apply the staged view reset (the old reset-before-show
    # semantics) — no diff paging state may leak from the previous room.
    class _EmptyRoomBStore(_TwoRoomStore):
        def get_room(self, room_id):  # room-b is healthy here
            return _FakeCandidateRoomStore.get_room(self, room_id)

        def list_candidates(self, room_id):
            if room_id == "room-b":
                return []
            return super().list_candidates(room_id)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _EmptyRoomBStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            assert app._candidate_view == "diff"  # room-a rendered a diff
            options = app.query_one("#room-list", OptionList)
            options.highlighted = 1
            await pilot.pause()
            assert app._selected_room_id == "room-b"
            assert "(no candidates yet)" in str(
                app.query_one("#candidate-detail").render()
            )
            assert app._candidate_view is None
            assert app._diff_pages == 1
            assert app._diff_truncated is False

    drive(scenario())


def test_diff_paging_failure_does_not_advance_view_state(
    tmp_path, git_repo, monkeypatch
):
    # codex R4 minor: `d` used to bump _diff_pages/_candidate_view BEFORE the
    # artifact read — a failure left the old pane visible with advanced view
    # state. The proposal commits only after staging succeeds.
    class _PagingFailStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.broken = False

        def load_candidate_patch_head(self, candidate_id, max_patch_bytes):
            if self.broken:
                raise sqlite3.OperationalError("patch stream failed")
            return super().load_candidate_patch_head(candidate_id, max_patch_bytes)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _PagingFailStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            assert app._candidate_view == "diff"
            assert app._diff_pages == 1
            assert app._diff_truncated  # the fake patch exceeds one page
            store.broken = True
            await pilot.press("d")  # page further; the read fails
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#room-status").render())
            assert app._diff_pages == 1  # the proposal was not committed
            assert app._candidate_view == "diff"
            # A view switch that fails likewise leaves the view untouched.
            monkeypatch.setattr(
                store,
                "load_candidate_checkpoint",
                lambda *a: (_ for _ in ()).throw(
                    sqlite3.OperationalError("output stream failed")
                ),
            )
            await pilot.press("o")
            await pilot.pause()
            assert app._candidate_view == "diff"

    drive(scenario())


def test_session_rebuild_highlight_event_does_not_reread_the_store(
    tmp_path, git_repo, monkeypatch
):
    # codex R3: same short-circuit rule for the session list.
    class _CountingSessions:
        def __init__(self):
            self.list_calls = 0
            self.state = "open"

        def list(self):
            self.list_calls += 1
            return [
                {
                    "session_id": "sess-1111",
                    "agent_id": "claude",
                    "state": self.state,
                    "live": True,
                    "source_stale": False,
                    "worktree_path": "/w",
                    "checkpoints": [],
                }
            ]

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _CountingSessions()
            app._session_store = store
            app._refresh_sessions()
            await pilot.pause()
            baseline = store.list_calls
            # Changed content rebuilds the list and queues a same-id highlight:
            # exactly ONE list() read for the refresh, none for the highlight.
            store.state = "closing"
            app._refresh_sessions()
            await pilot.pause()
            assert store.list_calls == baseline + 1

    drive(scenario())


def test_sessions_render_failure_keeps_list_and_detail(
    tmp_path, git_repo, monkeypatch
):
    # codex R2: the session list used to commit before render_session ran — a
    # record valid enough for the list but malformed for rendering (e.g.
    # checkpoints=None) degraded AFTER the list widget changed. List and
    # detail must commit together or not at all.
    class _RenderBreakingSessions:
        def __init__(self):
            self.broken = False

        def list(self):
            record = {
                "session_id": "sess-1111",
                "agent_id": "claude",
                "state": "open",
                "live": True,
                "source_stale": False,
                "worktree_path": "/w",
                "checkpoints": [],
            }
            if not self.broken:
                return [record]
            extra = dict(record, session_id="sess-2222")
            return [dict(record, checkpoints=None), extra]  # render TypeError

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _RenderBreakingSessions()
            app._session_store = store
            app._refresh_sessions()
            await pilot.pause()
            options = app.query_one("#session-list", OptionList)
            assert options.option_count == 1
            detail_before = str(app.query_one("#session-detail").render())
            assert "sess-1111" in detail_before
            store.broken = True
            app._refresh_sessions()
            await pilot.pause()
            status = str(app.query_one("#session-status").render())
            assert "DEGRADED" in status
            # The changed list content was NOT committed.
            assert app.query_one("#session-list", OptionList).option_count == 1
            assert str(app.query_one("#session-detail").render()) == detail_before

    drive(scenario())


def test_poll_degrades_on_symlink_loop_runtime_error(tmp_path, git_repo, monkeypatch):
    # codex R8: staged reads reach Path.resolve(), which raises RuntimeError
    # on a symlink loop — the same failure class fixed in artifact
    # reconciliation must degrade the poll, not kill the app.
    class _LoopingRoomStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.broken = False

        def list_rooms(self):
            if self.broken:
                raise RuntimeError("Symlink loop from '/workspaces/room-1'")
            return super().list_rooms()

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _LoopingRoomStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            store.broken = True
            app._poll()
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#room-status").render())

            class _LoopingSessions:
                def list(self):
                    raise RuntimeError("Symlink loop from '/workspaces/sess-1'")

            app._session_store = _LoopingSessions()
            app._refresh_sessions()
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#session-status").render())

    drive(scenario())


def test_sessions_poll_degrades_on_store_failure(tmp_path, git_repo, monkeypatch):
    # Same invariant for the Sessions surface: a store-list failure mid-poll
    # degrades the status instead of killing the app.
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()

            class _FailingSessions:
                def list(self):
                    raise IsolatedSessionError("session index unreadable")

            app._session_store = _FailingSessions()
            app._refresh_sessions()
            await pilot.pause()
            status = str(app.query_one("#session-status").render())
            assert "DEGRADED" in status and "unreadable" in status

    drive(scenario())


def test_candidate_detail_recovers_after_store_rebind(
    tmp_path, git_repo, monkeypatch
):
    # codex R4 Major: after Rooms became unavailable (detail blanked) and the
    # store came back, the stale _candidate_detail_key must not keep the
    # candidate pane blank.
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        store = _FakeCandidateRoomStore()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            assert "[Diff]" in app.candidate_detail_text
            # Store lost: the pane blanks.
            app._rooms = None
            app._refresh_rooms()
            await pilot.pause()
            assert str(app.query_one("#candidate-detail").render()) == ""
            # The reload path rebinds (dropping caches), then reconnects.
            app._rebind_collaboration()
            app._rooms = _FakeRooms(store=store)
            app._refresh_rooms()
            await pilot.pause()
            assert "[Diff]" in str(app.query_one("#candidate-detail").render())

    drive(scenario())


def test_job_rebuild_highlight_event_does_not_reread_the_store(tmp_path):
    # codex R4 Major: the rebuild's queued OptionHighlighted arrives after
    # _refreshing_jobs resets; the handler must not re-read list_jobs for the
    # row _refresh_jobs already showed.
    class _CountingJobs(_FakeManyJobs):
        def __init__(self):
            super().__init__(count=5)
            self.list_calls = 0

        def list_jobs(self):
            self.list_calls += 1
            return super().list_jobs()

    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(
                board={"enabled": False}, tui={"state_dir": str(tmp_path / "jobs")}
            ),
            board=None,
            board_error=None,
            toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            jobs = _CountingJobs()
            app._jobs = jobs
            app._refresh_jobs()
            await pilot.pause()
            baseline = jobs.list_calls
            # Changed content forces a rebuild; exactly ONE list_jobs read.
            jobs.rows[0]["status"] = "running"
            app._poll()
            await pilot.pause()
            assert jobs.list_calls == baseline + 1
            # A genuine user movement still reads the store to show the row.
            app.query_one("#job-list", OptionList).highlighted = 3
            await pilot.pause()
            assert jobs.list_calls == baseline + 2

    drive(scenario())


def test_jobs_poll_degrades_and_keeps_the_app_alive(tmp_path, git_repo):
    # The Runs poll reads a state directory and, when a manager released its
    # lock, rewrites job state — so a mid-session I/O failure must degrade the
    # pane like the rooms and sessions polls, never escape the interval callback
    # and take the app down.
    class _BrokenJobs(_FakeJobs):
        def __init__(self):
            super().__init__()
            self.broken = False

        def list_jobs(self):
            if self.broken:
                raise OSError("state directory vanished")
            return []

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        jobs = _BrokenJobs()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = jobs
            await pilot.press("2")  # Runs tab — _poll only refreshes what's visible
            await pilot.pause()
            jobs.broken = True
            app._poll()  # must not raise
            await pilot.pause()
            status = str(app.query_one("#job-status").render())
            assert "DEGRADED" in status and "state directory vanished" in status
            # Recovery: the store heals and the next poll renders normally.
            jobs.broken = False
            app._poll()
            await pilot.pause()
            assert "DEGRADED" not in str(app.query_one("#job-status").render())

    drive(scenario())


def test_jobs_poll_degrades_when_active_job_reconcile_fails(tmp_path, git_repo):
    # active_job() WRITES (it reconciles a released lock), so it has its own
    # failure mode distinct from the listing read.
    class _BrokenActive(_FakeJobs):
        def list_jobs(self):
            return [
                {
                    "job_dir": str(tmp_path / "j1"),
                    "job_id": "abcd1234efgh",
                    "op": "board-run",
                    "status": "running",
                    "created_at": 1.0,
                }
            ]

        def active_job(self):
            raise OSError("read-only state dir")

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = _BrokenActive()
            app._selected_job_dir = None
            await pilot.press("2")  # Runs tab
            await pilot.pause()
            app._poll()  # must not raise
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#job-status").render())

    drive(scenario())


def test_jobs_poll_degrades_on_a_malformed_row(tmp_path, git_repo):
    # Rows come from files this process does not own, so labelling and rendering
    # can raise just as readily as reading — a spec missing job_id used to reach
    # _job_label and kill the callback.
    class _MalformedJobs(_FakeJobs):
        def list_jobs(self):
            return [{"job_dir": str(tmp_path / "j1"), "status": "running"}]

        def active_job(self):
            return None

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._jobs = _MalformedJobs()
            await pilot.press("2")  # Runs tab
            await pilot.pause()
            app._poll()  # must not raise
            await pilot.pause()
            assert "DEGRADED" in str(app.query_one("#job-status").render())

    drive(scenario())


def test_tui_prompt_surfaces_escape_legacy_control_sequences():
    # New prompts are rejected at the coordinator boundary, but a row stored
    # BEFORE that validation can carry e.g. OSC 52 — Textual passes such
    # sequences through to the terminal even with markup=False, so every TUI
    # prompt display path must escape them visibly.
    from syncmill.tui_collaboration import _prompt_first_line, escape_controls

    line = _prompt_first_line("evil \x1b]52;c;cGF5bG9hZA==\x07 rest \x9b31m")
    assert "\x1b" not in line and "\x07" not in line and "\x9b" not in line
    assert "\\x1b" in line and "\\x9b" in line
    assert escape_controls("keep\ttabs\nand text") == "keep\ttabs\nand text"


def test_candidate_details_show_usage_and_output_does_not_interpret():
    from syncmill.models import TokenUsage
    from syncmill.room_compare import _usage_short
    from syncmill.tui_collaboration import (
        render_candidate_details,
        render_candidate_output,
    )

    usage = TokenUsage(
        total_input_tokens=40_100, output_tokens=2_300,
        cache_read_tokens=34_200, total_cost_usd=0.42,
    )
    details = render_candidate_details(_candidate_row(usage=usage), round_number=2)
    assert "Usage: input 40,100, output 2,300, cache read 34,200, cost $0.4200" in details
    # Missing usage is "not reported", never omitted silently, never zero.
    assert "Usage: not reported" in render_candidate_details(
        _candidate_row(usage=None), round_number=2
    )
    # A huge (valid) count must not overflow a float division anywhere.
    huge = TokenUsage(total_input_tokens=10**400, output_tokens=10**400)
    assert "Usage: input" in render_candidate_details(
        _candidate_row(usage=huge), round_number=2
    )
    assert _usage_short(huge).startswith("in ")
    # The Output view interprets nothing: one pointer, no gate/usage lines.
    output = render_candidate_output(
        {"candidate_id": "c", "state": "completed", "exit_code": 0,
         "duration_seconds": 1.0, "output": "ok", "gate": None,
         "usage": {"total_input_tokens": 5}}
    )
    assert "Gate, usage, eligibility: see Details (m) or Compare (c)" in output
    assert "Usage:" not in output and "not configured" not in output


class _FakeRoundRooms:
    """Duck-typed rooms coordinator for the round-modal funnel tests."""

    def __init__(self, *, rounds=None, candidates=None):
        self.config = object()
        self.preflights: list[tuple] = []
        self.events: list[tuple] = []
        outer = self

        class _Store:
            @staticmethod
            def get_room(room_id):
                return {
                    "id": room_id,
                    "prompt": "creation prompt",
                    "members": [{"member_id": "claude", "provider_id": "claude"}],
                    "round_count": len(rounds or []),
                }

            @staticmethod
            def list_rounds(room_id):
                return list(rounds or [])

            @staticmethod
            def list_candidates(room_id):
                return list(candidates or [])

            @staticmethod
            def record_event(room_id, kind, payload):
                outer.events.append((room_id, kind, payload))

        self.store = _Store()

    def preflight_round(self, room_id, prompt, *, lineage, base_candidate_id):
        self.preflights.append((room_id, prompt, lineage, base_candidate_id))
        return {}

    def close(self):
        return None


def test_room_round_modal_prefills_previous_round_prompt(tmp_path, git_repo, monkeypatch):
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRoundRooms(
                rounds=[{"number": 1, "prompt": "round one prompt"}]
            )
            app._selected_room_id = "room-a"
            staged: dict = {}
            monkeypatch.setattr(
                app, "push_screen",
                lambda screen, callback: staged.update(screen=screen, cb=callback),
            )
            app._confirm_room_round()
            assert staged["screen"]._default_prompt == "round one prompt"

            # A fresh room (no rounds yet) prefills the creation prompt.
            app._rooms = _FakeRoundRooms()
            app._confirm_room_round()
            assert staged["screen"]._default_prompt == "creation prompt"

    drive(scenario())


def test_room_round_funnel_threads_prompt_lineage_and_base(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            rooms = _FakeRoundRooms()
            app._rooms = rooms
            app._selected_room_id = "room-a"
            launched: dict = {}

            def fake_launch(root, room_id, prompt=None, **kwargs):
                launched.update(room_id=room_id, prompt=prompt, **kwargs)
                return 4242

            monkeypatch.setattr("syncmill.tui.launch_room_worker", fake_launch)
            monkeypatch.setattr(app, "_refresh_rooms", lambda: None)

            app._apply_room_round(
                {"prompt": "edited round prompt", "lineage": "fresh",
                 "base_candidate": None}
            )
            await pilot.pause()
            assert rooms.preflights == [
                ("room-a", "edited round prompt", "fresh", None)
            ]
            assert launched["prompt"] == "edited round prompt"
            assert launched["lineage"] == "fresh"
            assert launched["base_candidate_id"] is None
            assert rooms.events[-1][1] == "worker.launched"

            # An EMPTY prompt falls back (None) with a visible warning.
            app._apply_room_round(
                {"prompt": "   ", "lineage": "continue-own", "base_candidate": None}
            )
            await pilot.pause()
            assert launched["prompt"] is None
            # A bool (the legacy confirm shape) or None is ignored outright.
            before = len(rooms.preflights)
            app._apply_room_round(True)
            app._apply_room_round(None)
            assert len(rooms.preflights) == before

    drive(scenario())


def test_room_round_modal_blocks_branch_from_without_base(tmp_path, git_repo):
    async def scenario():
        from syncmill.tui_collaboration import RoomRoundModal

        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            results: list = []
            room = {"id": "room-a", "round_count": 1,
                    "members": [{"member_id": "claude"}]}
            eligible = [{"id": "cand-12345678", "member_id": "claude"}]
            app.push_screen(
                RoomRoundModal(room, "prefill", eligible), results.append
            )
            await pilot.pause()
            modal = app.screen
            from textual.widgets import Button, Checkbox, Select, TextArea

            apply_button = modal.query_one("#apply", Button)
            assert modal.query_one("#round-prompt", TextArea).text == "prefill"
            modal.query_one("#round-lineage", Select).value = "branch-from"
            modal.query_one("#confirmed", Checkbox).value = True
            modal.on_button_pressed(Button.Pressed(apply_button))
            await pilot.pause()
            assert results == []  # blocked: branch-from without a base stays open

            modal.query_one("#round-base", Select).value = "cand-12345678"
            modal.on_button_pressed(Button.Pressed(apply_button))
            await pilot.pause()
            assert results == [{
                "prompt": "prefill", "lineage": "branch-from",
                "base_candidate": "cand-12345678",
            }]

    drive(scenario())


def test_room_detail_shows_prompt_line(tmp_path, git_repo):
    from syncmill.tui_collaboration import render_room

    detail = render_room(
        {"id": "room-a", "prompt": "fix the parser\nsecond line", "state": "draft",
         "revision": 0, "round_count": 0, "members": []},
        [], [],
    )
    assert "Prompt: fix the parser" in detail
    assert "second line" not in detail

    # Candidate details carry the ROUND prompt when the store provides it.
    from syncmill.tui_collaboration import render_candidate_details

    details = render_candidate_details(
        _candidate_row(), round_number=2, round_prompt="round two prompt\nrest"
    )
    assert "Round prompt: round two prompt" in details
    assert render_candidate_details(_candidate_row(), round_number=2).count(
        "Round prompt"
    ) == 0


def test_room_detail_renders_markup_like_prompt_text_without_error(tmp_path, git_repo):
    # codex review MAJOR: #room-detail now carries the user-authored prompt, so
    # markup must be OFF — a prompt containing `[/]` would raise MarkupError
    # inside the poll and take the app down.
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            hostile = "fix the [/] parser [bold] case"
            app._update_static("#room-detail", f"Prompt: {hostile}")
            await pilot.pause()
            assert hostile in str(app.query_one("#room-detail").render())

    drive(scenario())


def test_room_round_funnel_preserves_prompt_verbatim(tmp_path, git_repo, monkeypatch):
    # codex review MAJOR: the funnel used to .strip() every prompt, mutating the
    # user's raw text (which the stored rounds.prompt is contracted to keep) and
    # diverging from --prompt-file.
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRoundRooms()
            app._selected_room_id = "room-a"
            launched: dict = {}
            monkeypatch.setattr(
                "syncmill.tui.launch_room_worker",
                lambda root, room_id, prompt=None, **kw: launched.update(prompt=prompt)
                or 1,
            )
            monkeypatch.setattr(app, "_refresh_rooms", lambda: None)
            app._apply_room_round({
                "prompt": "  keep\n  my indentation  \n",
                "lineage": "continue-own", "base_candidate": None,
            })
            await pilot.pause()
            assert launched["prompt"] == "  keep\n  my indentation  \n"

    drive(scenario())


@pytest.mark.parametrize("removed_widget", ["#agent-status", "#memory-status"])
def test_memory_probe_finishes_after_status_widget_removal(tmp_path, monkeypatch, removed_widget):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class FakeProc:
            returncode = 0

            async def communicate(self):
                started.set()
                await release.wait()
                return b"memtomem: connected\n", b""

        async def fake_exec(*args, **kwargs):
            return FakeProc()

        monkeypatch.setattr("syncmill.tui.asyncio.create_subprocess_exec", fake_exec)
        app = SyncmillTuiApp(
            SyncmillConfig(board={"enabled": False}, repo_root=str(tmp_path),
                           tui={"state_dir": str(tmp_path / "jobs")}),
            board=None, board_error=None, toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            probe = asyncio.create_task(app._probe_memory())
            try:
                await asyncio.wait_for(started.wait(), 2)
                await app.query_one(removed_widget).remove()
            finally:
                release.set()
                await asyncio.wait_for(probe, 2)
            assert app._memory_status == "memtomem: connected"

    drive(scenario())


def _corrupt_artifact_dir(store, name):
    bad = store.root / name
    bad.mkdir()
    (bad / "manifest.json").write_text("{broken")
    return bad


def test_artifacts_pane_reports_unreadable_entries_and_keeps_selection(
    tmp_path, git_repo
):
    # #170: a corrupt manifest is reported, never hidden; a still-valid
    # NON-first selection survives both the degraded refresh and the repair.
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-older")
        _seed_artifact(app._artifacts, git_repo, "run-newer")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._selected_run_id == "run-newer"  # newest first
            await pilot.press("down")
            await pilot.pause()
            assert app._selected_run_id == "run-older"
            assert "Run: run-older" in app.artifact_detail_text

            _corrupt_artifact_dir(app._artifacts, "zz-corrupt")
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._static_texts["#artifact-status"] == (
                "artifacts:2 | DEGRADED: 1 unreadable"
            )
            options = app.query_one("#artifact-list", OptionList)
            assert options.option_count == 2  # the corrupt entry is not selectable
            assert app._selected_run_id == "run-older"
            assert options.get_option_at_index(options.highlighted).id == "run-older"
            assert "Run: run-older" in app.artifact_detail_text

            shutil.rmtree(app._artifacts.root / "zz-corrupt")  # repaired by hand
            _seed_artifact(app._artifacts, git_repo, "run-repaired")
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._static_texts["#artifact-status"] == "artifacts:3"
            assert app._selected_run_id == "run-older"
            assert options.get_option_at_index(options.highlighted).id == "run-older"
            assert "Run: run-older" in app.artifact_detail_text

    drive(scenario())


def test_artifacts_pane_all_corrupt_is_not_a_healthy_empty_store(tmp_path, git_repo):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _corrupt_artifact_dir(app._artifacts, "evil\x1b[31m")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._static_texts["#artifact-status"] == (
                "artifacts:0 | DEGRADED: 1 unreadable"
            )
            detail = app._static_texts["#artifact-detail"]
            assert "No readable run artifacts. 1 run directory could not be read" in detail
            assert "evil\\x1b[31m — corrupt or unreadable manifest" in detail
            assert "\x1b" not in detail
            assert "Session checkpoints and run winners land here" not in detail
            assert "syncmill runs show" in detail
            assert app._selected_run_id is None

    drive(scenario())


# ── #168: artifact polls stop rereading unchanged manifests ──────────────────


def _count_artifact_reads(monkeypatch):
    from syncmill.artifact_io import ArtifactIO

    reads: list[str] = []
    original = ArtifactIO.read_if_changed

    def counting(self, path, signature, limit=-1):
        result = original(self, path, signature, limit)
        if result[1] is not None:  # None = signature hit, nothing was read
            reads.append(path.name)
        return result

    monkeypatch.setattr(ArtifactIO, "read_if_changed", counting)
    return reads


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_unchanged_artifact_refresh_reads_only_the_selected_detail(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        for n in range(5):
            _seed_artifact(app._artifacts, git_repo, f"run-{n}")
        reads = _count_artifact_reads(monkeypatch)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            assert reads.count("manifest.json") >= 5  # the cold listing
            selected = app._selected_run_id
            detail = app.artifact_detail_text
            reads.clear()
            for _ in range(3):
                await pilot.press("r")
                await _settle(app, pilot)
            # the listing reads nothing; only the ONE selected run's detail is
            # re-read (load + read_result), a constant independent of N
            assert reads == ["manifest.json", "manifest.json", "result.json"] * 3
            assert app._selected_run_id == selected
            assert app.artifact_detail_text == detail
            assert app._static_texts["#artifact-status"] == "artifacts:5"

    drive(scenario())


def test_artifact_refresh_rerenders_a_run_another_process_changed(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-a")
        _seed_artifact(app._artifacts, git_repo, "run-b")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            selected = app._selected_run_id
            assert "State: pending" in app.artifact_detail_text
            # another process: a second store instance discards the selected run
            ArtifactStore(tmp_path / "runs", app._artifacts.root.parent.name).discard(
                selected
            )
            reads = _count_artifact_reads(monkeypatch)
            await pilot.press("r")
            await _settle(app, pilot)
            assert "State: discarded" in app.artifact_detail_text
            assert app._selected_run_id == selected
            # the listing reads only the changed entry (1); the detail then
            # renders twice — once from the refresh and once from the rebuilt
            # OptionList's highlight event (its label changed with the state),
            # each render reading the manifest via load() and read_result()
            assert reads.count("manifest.json") == 1 + 2 * 2
            reads.clear()
            await pilot.press("r")
            await _settle(app, pilot)
            # unchanged tick: no listing read, only the selected detail
            assert reads.count("manifest.json") == 2

    drive(scenario())


def test_artifact_listing_stale_generation_is_dropped(tmp_path, git_repo):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-a")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            current = app._artifact_listing_generation
            status = app._static_texts["#artifact-status"]
            stale = app._artifacts.list_detailed()
            app._apply_artifact_listing(current - 1, None, "old store exploded")
            assert app._static_texts["#artifact-status"] == status  # ignored
            assert app._artifact_listing_inflight is False  # still released
            # an in-flight tick neither starts a second worker nor advances
            # the generation — it is coalesced; the running one still lands
            # and then triggers exactly one rerun
            app._artifact_listing_inflight = True
            app._refresh_artifacts()
            app._refresh_artifacts()
            assert app._artifact_listing_generation == current
            assert app._artifact_listing_dirty is True
            app._apply_artifact_listing(current, stale, None)
            assert app._static_texts["#artifact-status"] == "artifacts:1"
            assert app._artifact_listing_generation == current + 1  # one rerun
            await _settle(app, pilot)
            assert app._artifact_listing_inflight is False
            assert app._artifact_listing_dirty is False

    drive(scenario())


def test_artifact_listing_failure_degrades_and_allows_a_later_refresh(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-a")
        store = app._artifacts
        original = store.list_detailed
        broken = {"on": True}

        def flaky():
            if broken["on"]:
                raise ArtifactError("artifact store directory changed; restart")
            return original()

        monkeypatch.setattr(store, "list_detailed", flaky)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            assert app._static_texts["#artifact-status"].startswith(
                "DEGRADED | Artifacts unavailable: artifact store directory changed"
            )
            broken["on"] = False
            await pilot.press("r")
            await _settle(app, pilot)
            assert app._static_texts["#artifact-status"] == "artifacts:1"

    drive(scenario())


def test_rebinding_retires_an_in_flight_artifact_listing(tmp_path, git_repo):
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-old-store")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            old_listing = app._artifacts.list_detailed()
            generation = app._artifact_listing_generation
            app._artifact_listing_inflight = True  # a worker is still running
            app._invalidate_artifact_listing()  # what _rebind_collaboration does
            # the flag stays owned by the running thread (no second scan)
            assert app._artifact_listing_inflight is True
            assert app._artifact_listing_dirty is True
            status_before = app._static_texts["#artifact-status"]
            _seed_artifact(app._artifacts, git_repo, "run-later")
            app._apply_artifact_listing(generation, old_listing, None)  # late result
            # the stale result is not rendered; the coalesced rerun is
            assert app._static_texts["#artifact-status"] == status_before
            await _settle(app, pilot)
            assert app._static_texts["#artifact-status"] == "artifacts:2"

    drive(scenario())


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_artifact_detail_recovers_after_a_run_becomes_readable_again(
    tmp_path, git_repo
):
    # Review regression: the only run becomes unreadable (placeholder shown),
    # then readable again with an UNCHANGED manifest — the placeholder must be
    # replaced on the next tick, with no render-skip state to get stuck in.
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-a")
        run_dir = app._artifacts.root / "run-a"
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            assert "Run: run-a" in app._static_texts["#artifact-detail"]
            os.chmod(run_dir, 0)
            try:
                await pilot.press("r")
                await _settle(app, pilot)
                assert "No readable run artifacts" in app._static_texts["#artifact-detail"]
                assert app._static_texts["#artifact-status"] == (
                    "artifacts:0 | DEGRADED: 1 unreadable"
                )
            finally:
                os.chmod(run_dir, 0o700)
            await pilot.press("r")
            await _settle(app, pilot)
            assert app._static_texts["#artifact-status"] == "artifacts:1"
            assert "Run: run-a" in app._static_texts["#artifact-detail"]

    drive(scenario())


@pytest.mark.parametrize("view", ["summary", "diff"])
def test_artifact_detail_recovers_from_a_transient_sidecar_error(
    tmp_path, git_repo, monkeypatch, view
):
    # Review regression: a one-shot OSError from read_result (summary) or
    # patch (diff) is shown once, and the NEXT tick shows the real content —
    # a transient error never sticks across unchanged polls.
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-a")
        store = app._artifacts
        name = "read_result" if view == "summary" else "patch"
        original = getattr(store, name)
        fail = {"once": False}

        def flaky(*args, **kwargs):
            if fail["once"]:
                fail["once"] = False
                raise OSError(5, "Input/output error")
            return original(*args, **kwargs)

        monkeypatch.setattr(store, name, flaky)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            if view == "diff":
                await pilot.press("d")
                await pilot.pause()
            healthy = app.artifact_detail_text
            assert "unavailable" not in healthy and "unreadable" not in healthy
            fail["once"] = True
            await pilot.press("r")
            await _settle(app, pilot)
            broken = app.artifact_detail_text
            assert ("diff unavailable" if view == "diff" else "result unreadable") in broken
            await pilot.press("r")
            await _settle(app, pilot)
            assert app.artifact_detail_text == healthy

    drive(scenario())


def test_checkpoint_navigation_survives_a_listing_already_in_flight(
    tmp_path, git_repo
):
    # Review regression: a listing that started BEFORE the checkpoint lands
    # after it. It must be dropped, so the new run stays selected instead of
    # being replaced by an older run that later refreshes keep preserving.
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-old")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            # an "old worker" is still running, holding a pre-checkpoint view
            stale = app._artifacts.list_detailed()
            old_generation = app._artifact_listing_generation
            app._artifact_listing_inflight = True
            _seed_artifact(app._artifacts, git_repo, "run-new")
            app._session_store = _FakeSessionStore("run-new")
            app._after_checkpoint("run-new")
            await _settle(app, pilot)
            app._apply_artifact_listing(old_generation, stale, None)  # lands late
            await _settle(app, pilot)
            assert app._selected_run_id == "run-new"
            assert "Run: run-new" in app.artifact_detail_text
            await pilot.press("r")
            await _settle(app, pilot)
            assert app._selected_run_id == "run-new"
            assert "Run: run-new" in app.artifact_detail_text

    drive(scenario())


def test_artifact_listing_never_runs_two_scans_at_once(tmp_path, git_repo, monkeypatch):
    # Review regression: with a listing thread BLOCKED, refreshes, discards,
    # checkpoint-style invalidations and rebind-style invalidations must not
    # start a second scan beside it; they coalesce into exactly one rerun
    # after the blocked scan finishes.
    async def scenario():
        app = _artifact_pane_app(tmp_path, git_repo)
        _seed_artifact(app._artifacts, git_repo, "run-a")
        store = app._artifacts
        original = store.list_detailed
        gate = threading.Event()
        lock = threading.Lock()
        state = {"active": 0, "max": 0, "calls": 0}

        def blocking():
            with lock:
                state["active"] += 1
                state["calls"] += 1
                state["max"] = max(state["max"], state["active"])
            try:
                gate.wait(10)
                return original()
            finally:
                with lock:
                    state["active"] -= 1

        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("v")
            await _settle(app, pilot)
            monkeypatch.setattr(store, "list_detailed", blocking)
            app._refresh_artifacts()
            for _ in range(200):
                if state["calls"] == 1:
                    break
                await asyncio.sleep(0.01)
            assert state["calls"] == 1  # the blocked scan is running
            for _ in range(3):
                app._refresh_artifacts()
                app._invalidate_artifact_listing()
                app._after_artifact_discard("run-a")
            await asyncio.sleep(0.1)
            assert state["calls"] == 1  # nothing started beside it
            gate.set()
            await _settle(app, pilot)
            await _settle(app, pilot)
            assert state["max"] == 1
            assert state["calls"] == 2  # one coalesced rerun, not nine
            assert app._artifact_listing_inflight is False
            assert app._static_texts["#artifact-status"] == "artifacts:1"

    drive(scenario())


# ---------------------------------------------- rooms evidence snapshot (#172)


def _seeded_room(tmp_path):
    """A real RoomStore with one finished round: a passed and a failed gate
    plus an agent that did not complete."""
    from test_room_compare import Room, _gate

    room = Room(tmp_path / "seeded", ["claude", "codex", "kimi-code"])
    rnd = room.start()
    passed = room.save(rnd, "claude", gate=_gate(("pytest -q", True)))
    failed = room.save(rnd, "codex", gate=_gate(("pytest -q", False)))
    not_run = room.save(rnd, "kimi-code", status=TaskStatus.FAILED)
    room.finish(rnd)
    return room, passed, failed, not_run


class _RealRooms:
    def __init__(self, store, repo_root):
        self.store = store
        self.repo_root = repo_root
        self.config = object()

    def close(self):
        return None


def test_compare_builds_from_a_real_store_off_the_event_loop(
    tmp_path, git_repo, monkeypatch
):
    # No builder fake: the worker opens its OWN RoomStore on the same root —
    # the main-thread connection would raise if used from the thread.
    room, passed, failed, not_run = _seeded_room(tmp_path)
    monkeypatch.setattr(
        "syncmill.tui.repository_fingerprint", lambda root: "fp-1"
    )

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _RealRooms(room.store, git_repo)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            text = app.candidate_detail_text
            assert text.startswith("[Compare]")
            assert "evidence as of" in text
            assert "source: unchanged since the room started" in text
            for manifest in (passed, failed, not_run):
                assert manifest["candidate_id"][:8] in text
            assert "not run" in text  # the table's short gate label
            # m reads the same snapshot: the same wording for the same row.
            options = app.query_one("#candidate-list", OptionList)
            ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
            options.highlighted = ids.index(failed["candidate_id"])
            await pilot.pause()
            await pilot.press("m")
            await _settle(app, pilot)
            details = app.candidate_detail_text
            assert "[Details]" in details
            assert "Gate: failed" in details
            assert "eligible: no (quality gate failed)" in details
            assert "not configured" not in details
            assert "  - not eligible: quality gate failed" in details

    try:
        drive(scenario())
    finally:
        room.store.close()


def test_details_and_compare_share_wording_with_the_cli_json(tmp_path):
    from syncmill.room_compare import comparison_to_dict, gate_label, render_round_table
    from syncmill.tui import build_room_evidence
    from syncmill.tui_collaboration import render_candidate_details

    room, *_ = _seeded_room(tmp_path)
    try:
        comparison, _, _ = build_room_evidence(
            room.store.root, 10**6, tmp_path, room.id, 0.0
        )
    finally:
        room.store.close()
    data = {
        c["candidate_id"]: c
        for c in comparison_to_dict(comparison)["rounds"][0]["candidates"]
    }
    table = render_round_table(comparison, 1)
    for row in comparison.groups[0].rows:
        label = gate_label(row.gate)
        details = render_candidate_details(row, round_number=1)
        assert f"Gate: {label}" in details
        assert data[row.candidate_id]["gate"]["verdict"] == row.gate.verdict
        assert data[row.candidate_id]["apply_blockers"] == list(row.apply_blockers)
        for blocker in row.apply_blockers:
            assert blocker in details
        assert row.gate.verdict.replace("_", " ").split()[0] in table


def test_polling_never_builds_and_each_request_builds_once(
    tmp_path, git_repo, monkeypatch
):
    calls: list[str] = []

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_FakeCandidateRoomStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            _fake_evidence(monkeypatch, [_candidate_row(candidate_id="cand-ok")], calls=calls)
            await pilot.press("7")
            await pilot.pause()
            for _ in range(3):
                app._poll()
            await _settle(app, pilot)
            assert calls == []
            await pilot.press("c")
            await _settle(app, pilot)
            assert calls == ["room-a"]
            for _ in range(3):
                app._poll()
            await _settle(app, pilot)
            assert calls == ["room-a"]  # still open, still not rebuilt by polling
            await pilot.press("m")  # a snapshot exists: no new build
            await _settle(app, pilot)
            assert calls == ["room-a"]
            await pilot.press("r")
            await _settle(app, pilot)
            assert calls == ["room-a", "room-a"]

    drive(scenario())


def test_evidence_build_is_single_flight_and_never_flashes_an_intermediate(
    tmp_path, git_repo, monkeypatch
):
    gate = threading.Event()
    started = threading.Event()
    running = {"now": 0, "max": 0, "builds": 0}

    def build(store_root, output_max, repo_root, room_id, now):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        running["builds"] += 1
        started.set()
        gate.wait(5)
        running["now"] -= 1
        rows = [_candidate_row(candidate_id="cand-ok", member_id=f"build{running['builds']}")]
        return _comparison(rows), 1, frozenset({"cand-ok"})

    monkeypatch.setattr("syncmill.tui.build_room_evidence", build)
    rendered: list[str] = []

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_FakeCandidateRoomStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            original = app._update_static

            def spy(widget_id, text):
                if widget_id == "#candidate-detail":
                    rendered.append(text)
                return original(widget_id, text)

            monkeypatch.setattr(app, "_update_static", spy)
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert started.wait(5)
            # The build is blocked; the UI still answers keys.
            assert "building evidence snapshot" in app.candidate_detail_text
            await pilot.press("o")
            await pilot.pause()
            assert "[Output]" in app.candidate_detail_text
            await pilot.press("c")
            await pilot.press("r")
            await pilot.pause()
            gate.set()
            await _settle(app, pilot)
            await _settle(app, pilot)
            assert running["max"] == 1
            assert running["builds"] == 2  # two requests mid-build -> one rerun
            assert "build2" in app.candidate_detail_text
            assert not any("build1" in text for text in rendered)

    drive(scenario())


def test_a_store_root_change_retires_an_in_flight_build(tmp_path, git_repo, monkeypatch):
    gate = threading.Event()
    started = threading.Event()

    def build(store_root, output_max, repo_root, room_id, now):
        started.set()
        gate.wait(5)
        return _comparison([_candidate_row(candidate_id="cand-ok")]), 1, frozenset({"cand-ok"})

    monkeypatch.setattr("syncmill.tui.build_room_evidence", build)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _FakeCandidateRoomStore()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert started.wait(5)
            other = _FakeCandidateRoomStore()
            other.root = "/another-store"
            app._rooms = _FakeRooms(store=other)
            app._reconcile_evidence_store()
            gate.set()
            await _settle(app, pilot)
            assert app._evidence is None

    drive(scenario())


def test_a_failed_rebuild_keeps_the_last_snapshot_through_the_real_reload(
    tmp_path, git_repo, monkeypatch
):
    room, passed, _, _ = _seeded_room(tmp_path)
    monkeypatch.setattr("syncmill.tui.repository_fingerprint", lambda root: "fp-1")

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _RealRooms(room.store, git_repo)
            # The real `r` path rebinds the stores; keep this store bound.
            monkeypatch.setattr(app, "_rebind_collaboration", lambda: app._reconcile_evidence_store())
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            assert passed["candidate_id"][:8] in app.candidate_detail_text

            def broken(*args, **kwargs):
                raise RuntimeError("store went away")

            monkeypatch.setattr("syncmill.tui.build_room_evidence", broken)
            await pilot.press("r")
            await _settle(app, pilot)
            text = app.candidate_detail_text
            assert text.startswith("evidence unavailable: store went away")
            assert passed["candidate_id"][:8] in text  # last good snapshot kept

    try:
        drive(scenario())
    finally:
        room.store.close()


def test_room_change_hint_and_a_candidate_missing_from_the_snapshot(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _FakeCandidateRoomStore()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            # The snapshot only knows cand-ok (taken before cand-bad landed).
            _fake_evidence(monkeypatch, [_candidate_row(candidate_id="cand-ok")])
            monkeypatch.setattr(
                "syncmill.tui.build_room_evidence",
                lambda *a: (_comparison([_candidate_row(candidate_id="cand-ok")]), 1, frozenset({"cand-ok"})),
            )
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("m")
            await _settle(app, pilot)
            text = app.candidate_detail_text
            # The store already lists cand-bad, so the room changed since.
            assert "room changed since this snapshot — press r" in text
            assert "[Details]" in text
            options = app.query_one("#candidate-list", OptionList)
            options.highlighted = 1
            await pilot.pause()
            assert app._selected_candidate_id == "cand-bad"
            assert "not in the evidence snapshot" in app.candidate_detail_text
            assert "[Details]" not in app.candidate_detail_text

    drive(scenario())


def test_evidence_views_never_read_artifacts_on_the_event_loop(
    tmp_path, git_repo, monkeypatch
):
    reads: list[str] = []

    class _NoArtifactReads(_FakeCandidateRoomStore):
        # Counted, not raised: the first Rooms render legitimately opens on
        # Diff. What matters is that nothing reads once a snapshot view is up.
        def load_candidate_patch_head(self, *args):
            reads.append("patch")
            return super().load_candidate_patch_head(*args)

        def load_candidate_checkpoint(self, *args):
            reads.append("output")
            return super().load_candidate_checkpoint(*args)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_NoArtifactReads())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            _fake_evidence(
                monkeypatch,
                [_candidate_row(candidate_id="cand-ok"), _candidate_row(candidate_id="cand-bad")],
            )
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            assert app.candidate_detail_text.startswith("[Compare]")
            reads.clear()
            options = app.query_one("#candidate-list", OptionList)
            options.highlighted = 1  # Compare follows the cursor
            await pilot.pause()
            assert app.candidate_detail_text.startswith("[Compare]")
            await pilot.press("m")
            await _settle(app, pilot)
            assert "[Details]" in app.candidate_detail_text
            for _ in range(2):
                app._poll()
            await _settle(app, pilot)
            assert "[Details]" in app.candidate_detail_text
            assert reads == []

    drive(scenario())


def test_compare_works_for_a_room_without_candidates(tmp_path, git_repo, monkeypatch):
    from syncmill.room_compare import MissingMember

    class _EmptyRoom(_FakeCandidateRoomStore):
        def list_candidates(self, room_id):
            return []

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_EmptyRoom())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            comparison = _comparison([])
            group = comparison.groups[0]
            object.__setattr__(
                group,
                "missing",
                (MissingMember("claude", "claude", "no_candidate", ("member state: interrupted",)),),
            )
            monkeypatch.setattr(
                "syncmill.tui.build_room_evidence",
                lambda *a: (comparison, 1, frozenset()),
            )
            await pilot.press("7")
            await pilot.pause()
            assert app._selected_candidate_id is None
            await pilot.press("c")
            await _settle(app, pilot)
            text = app.candidate_detail_text
            assert text.startswith("[Compare]")
            assert "(no candidates retained in this round)" in text
            assert "claude: no candidate retained (member state: interrupted)" in text

    drive(scenario())


def test_compare_table_is_capped_and_terminal_safe():
    from syncmill.room_compare import FileOverlap, render_round_table

    hostile = "x\x1b]52;c;AAAA\x07\nforged"
    names = tuple(f"f{i:04}.py" for i in range(250))
    rows = [
        _candidate_row(candidate_id="a" * 36, member_id=hostile, files_changed=names),
        _candidate_row(candidate_id="b" * 36, files_changed=(hostile,)),
    ]
    comparison = _comparison(rows)
    group = comparison.groups[0]
    object.__setattr__(
        group,
        "overlap",
        FileOverlap(("a" * 36, "b" * 36), (), {"a" * 36: names, "b" * 36: (hostile,)}, {}, (), ()),
    )
    text = render_round_table(comparison, 3, highlighted_id="b" * 36)
    assert "... 50 more" in text
    assert "f0249.py" not in text
    assert "\x1b" not in text and "\x07" not in text
    assert not any(line.startswith("forged") for line in text.splitlines())
    assert any(line.startswith("> bbbbbbbb") for line in text.splitlines())


def test_rooms_action_bar_and_help_advertise_compare():
    assert "d/o/m/c diff/output/details/compare" in _action_bar_text("rooms-tab")


def test_a_room_change_after_render_shows_the_hint_on_the_next_poll(
    tmp_path, git_repo, monkeypatch
):
    # The hint must appear while Details stays open: the render key carries the
    # computed "room changed" flag, so an unchanged candidate/view/build still
    # re-renders when the poll sees a new candidate.
    class _ArrivingStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.late = self.rows.pop("cand-bad")

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _ArrivingStore()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            _fake_evidence(monkeypatch, [_candidate_row(candidate_id="cand-ok")])
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("m")
            await _settle(app, pilot)
            assert "[Details]" in app.candidate_detail_text
            assert "room changed" not in app.candidate_detail_text
            store.rows["cand-bad"] = store.late
            app._poll()
            await pilot.pause()
            assert "room changed since this snapshot — press r" in app.candidate_detail_text

    drive(scenario())


def _config_backed_room(tmp_path, git_repo):
    """A Rooms app whose REAL rebind path (`_reload_config` →
    `_rebind_collaboration`) reopens the store holding a seeded room."""
    from uuid import uuid4

    from syncmill.rooms import RoomCoordinator
    from syncmill.teamfile import load_config_at

    toml = git_repo / "syncmill.toml"
    toml.write_text(
        f'repo_root = "{git_repo}"\n[rooms]\nenabled = true\n'
        f'[runs]\nroot = "{tmp_path / "runs"}"\n',
        encoding="utf-8",
    )
    config = load_config_at(toml)
    coordinator = RoomCoordinator(config, git_repo, agents=[])
    store = coordinator.store
    room = store.create_room(repo_root=git_repo, prompt="p", agents=["claude"])
    rnd = store.start_round(
        room["id"], prompt="p", source_fingerprint="fp-1", snapshot_commit="b",
        lease_owner="w", lease_timeout_seconds=60, max_rounds=5,
    )
    store.init_round_members(
        room["id"], rnd["id"], [{"member_id": "claude", "provider_id": "claude"}]
    )
    result = TaskResult(
        task_id=uuid4(), agent_id="claude", status=TaskStatus.COMPLETED,
        output="o", exit_code=0, duration_seconds=1.0,
    )
    manifest = store.candidates.save(
        candidate_id=str(uuid4()), room_id=room["id"], round_id=rnd["id"],
        member_id="claude", provider_id="claude", attempt=1, result=result,
        attempt_state="completed", patch="diff --git a/f b/f\n", files_changed=["f"],
        gate=None, base_commit="b", source_fingerprint="fp-1",
    )
    store.add_candidate(manifest)
    store.set_member_state(rnd["id"], "claude", "completed")
    store.finish_round(room["id"], rnd["id"])
    coordinator.close()
    app = SyncmillTuiApp(
        config, board=None, board_error=None, toml_path=toml,
        refresh_seconds=3600, initial_tab="board",
    )
    return app, manifest["candidate_id"]


def _counting_builder(monkeypatch, gate=None, started=None):
    import syncmill.tui as tui

    real = tui.build_room_evidence
    calls: list[str] = []

    def build(*args):
        calls.append(args[3])
        if started is not None:
            started.set()
        if gate is not None:
            gate.wait(5)
        return real(*args)

    monkeypatch.setattr("syncmill.tui.build_room_evidence", build)
    monkeypatch.setattr("syncmill.tui.repository_fingerprint", lambda root: "fp-1")
    return calls


def test_real_rebind_path_keeps_the_snapshot_and_only_r_rebuilds(
    tmp_path, git_repo, monkeypatch
):
    app, candidate_id = _config_backed_room(tmp_path, git_repo)
    calls = _counting_builder(monkeypatch)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            assert calls and len(calls) == 1
            assert candidate_id[:8] in app.candidate_detail_text
            # Tab activation rebinds the stores but must not rebuild.
            await pilot.press("5")
            await pilot.pause()
            await pilot.press("7")
            await _settle(app, pilot)
            assert len(calls) == 1
            assert app.candidate_detail_text.startswith("[Compare]")
            # Pressing 7 on the active tab is a refresh, not an explicit r.
            await pilot.press("7")
            await _settle(app, pilot)
            assert len(calls) == 1

            def broken(*args):
                raise RuntimeError("store went away")

            monkeypatch.setattr("syncmill.tui.build_room_evidence", broken)
            await pilot.press("r")  # explicit: rebinds for real, then rebuilds
            await _settle(app, pilot)
            text = app.candidate_detail_text
            assert text.startswith("evidence unavailable: store went away")
            assert candidate_id[:8] in text

    drive(scenario())


def test_a_same_root_rebind_during_the_first_build_keeps_its_result(
    tmp_path, git_repo, monkeypatch
):
    app, candidate_id = _config_backed_room(tmp_path, git_repo)
    gate, started = threading.Event(), threading.Event()
    _counting_builder(monkeypatch, gate=gate, started=started)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert started.wait(5)
            await pilot.press("5")  # tab switches rebind the same store
            await pilot.pause()
            await pilot.press("7")
            await pilot.pause()
            gate.set()
            await _settle(app, pilot)
            assert app._evidence is not None
            assert candidate_id[:8] in app.candidate_detail_text

    drive(scenario())


class _TwoRoomEvidenceStore(_FakeCandidateRoomStore):
    def list_rooms(self):
        return [
            {"id": "room-a", "state": "collected", "round_count": 1},
            {"id": "room-b", "state": "collected", "round_count": 1},
        ]


def test_a_build_finishing_after_the_user_left_its_room_does_not_replace_the_slot(
    tmp_path, git_repo, monkeypatch
):
    gate, started = threading.Event(), threading.Event()

    def build(store_root, output_max, repo_root, room_id, now):
        if room_id == "room-b":
            started.set()
            gate.wait(5)
        rows = [_candidate_row(candidate_id="cand-ok", member_id=f"built-for-{room_id}")]
        return _comparison(rows, room_id=room_id), 1, frozenset({"cand-ok", "cand-bad"})

    monkeypatch.setattr("syncmill.tui.build_room_evidence", build)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_TwoRoomEvidenceStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            assert app._selected_room_id == "room-a"
            await pilot.press("m")
            await _settle(app, pilot)
            assert "built-for-room-a" in app.candidate_detail_text
            rooms = app.query_one("#room-list", OptionList)
            rooms.highlighted = 1
            await pilot.pause()
            assert app._selected_room_id == "room-b"
            await pilot.press("c")
            await pilot.pause()
            assert started.wait(5)
            rooms.highlighted = 0
            await pilot.pause()
            await pilot.press("m")  # the slot still holds room-a: no rebuild
            await pilot.pause()
            gate.set()
            await _settle(app, pilot)
            assert app._selected_room_id == "room-a"
            assert "built-for-room-a" in app.candidate_detail_text
            assert app._evidence["room_id"] == "room-a"

    drive(scenario())


def test_a_first_candidate_arriving_keeps_compare_without_reading_artifacts(
    tmp_path, git_repo, monkeypatch
):
    reads: list[str] = []

    class _FillingStore(_FakeCandidateRoomStore):
        def __init__(self):
            super().__init__()
            self.parked = dict(self.rows)
            self.rows.clear()

        def load_candidate_patch_head(self, *args):
            reads.append("patch")
            return super().load_candidate_patch_head(*args)

        def load_candidate_checkpoint(self, *args):
            reads.append("output")
            return super().load_candidate_checkpoint(*args)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _FillingStore()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            _fake_evidence(monkeypatch, [])
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            assert app.candidate_detail_text.startswith("[Compare]")
            store.rows.update(store.parked)
            app._poll()
            await pilot.pause()
            assert app._candidate_view == "compare"
            assert app._selected_candidate_id == "cand-ok"
            assert "not in the evidence snapshot" in app.candidate_detail_text
            assert reads == []

    drive(scenario())


def test_a_queued_request_for_a_room_since_left_is_dropped(
    tmp_path, git_repo, monkeypatch
):
    # Snapshot B; on A press c twice during a blocked build (one build, one
    # queued); go back to B and open Details. Leaving A drops its queued
    # request and retires its build; returning to B discarded B's old snapshot
    # (a room change always does), so Details asks for B again. A's result is
    # never shown and never lands in the slot.
    gate, started = threading.Event(), threading.Event()
    calls: list[str] = []

    def build(store_root, output_max, repo_root, room_id, now):
        calls.append(room_id)
        if room_id == "room-a":
            started.set()
            gate.wait(5)
        rows = [_candidate_row(candidate_id="cand-ok", member_id=f"built-for-{room_id}")]
        return _comparison(rows, room_id=room_id), 1, frozenset({"cand-ok", "cand-bad"})

    monkeypatch.setattr("syncmill.tui.build_room_evidence", build)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_TwoRoomEvidenceStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            rooms = app.query_one("#room-list", OptionList)
            rooms.highlighted = 1
            await pilot.pause()
            await pilot.press("m")
            await _settle(app, pilot)
            assert "built-for-room-b" in app.candidate_detail_text
            rooms.highlighted = 0
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert started.wait(5)
            await pilot.press("c")
            await pilot.pause()
            rooms.highlighted = 1
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            gate.set()
            await _settle(app, pilot)
            await _settle(app, pilot)
            assert calls == ["room-b", "room-a", "room-b"]  # no second A build
            assert app._evidence["room_id"] == "room-b"
            assert "built-for-room-b" in app.candidate_detail_text
            assert "built-for-room-a" not in app.candidate_detail_text
            assert "building" not in app.candidate_detail_text

    drive(scenario())


def test_r_after_a_store_change_builds_the_room_now_on_screen(
    tmp_path, git_repo, monkeypatch
):
    calls: list[str] = []
    _fake_evidence(monkeypatch, [_candidate_row(candidate_id="cand-ok")], calls=calls)

    class _OtherStore(_FakeCandidateRoomStore):
        root = "/other-room-store"

        def list_rooms(self):
            return [{"id": "room-z", "state": "collected", "round_count": 1}]

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_FakeCandidateRoomStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            assert calls == ["room-a"]

            def rebind_to_other() -> bool:
                app._rooms = _FakeRooms(store=_OtherStore())
                app._reconcile_evidence_store()
                return True

            monkeypatch.setattr(app, "_reload_config", rebind_to_other)
            await pilot.press("r")
            await _settle(app, pilot)
            assert app._selected_room_id == "room-z"
            assert calls == ["room-a", "room-z"]
            assert app._evidence["room_id"] == "room-z"

    drive(scenario())


def test_a_restored_store_shows_an_idle_prompt_not_a_stuck_building_state(
    tmp_path, git_repo, monkeypatch
):
    _fake_evidence(monkeypatch, [_candidate_row(candidate_id="cand-ok")])

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _FakeCandidateRoomStore()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            assert app._evidence is not None
            app._rooms = None
            app._reconcile_evidence_store()  # store unavailable: dropped
            app._rooms = _FakeRooms(store=store)
            app._reconcile_evidence_store()  # restored
            app._poll()
            await _settle(app, pilot)
            text = app.candidate_detail_text
            assert app._evidence is None and app._evidence_inflight is None
            assert "no evidence snapshot for this room — press c or r" in text
            assert "building" not in text

    drive(scenario())


def test_a_room_change_discards_the_snapshot(tmp_path, git_repo, monkeypatch):
    calls: list[str] = []
    _fake_evidence(monkeypatch, [_candidate_row(candidate_id="cand-ok")], calls=calls)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_TwoRoomEvidenceStore())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            assert app._evidence["room_id"] == "room-a"
            rooms = app.query_one("#room-list", OptionList)
            rooms.highlighted = 1
            await pilot.pause()
            rooms.highlighted = 0
            await pilot.pause()
            assert app._selected_room_id == "room-a"
            assert app._evidence is None  # A -> B -> A: the old snapshot is gone
            await pilot.press("c")
            await _settle(app, pilot)
            assert calls == ["room-a", "room-a"]

    drive(scenario())


def test_compare_without_a_candidate_names_a_round_missing_from_the_snapshot(
    tmp_path, git_repo, monkeypatch
):
    class _NewEmptyRound(_FakeCandidateRoomStore):
        rounds = [{"id": "round-1", "number": 3}]

        def list_candidates(self, room_id):
            return []

        def list_rounds(self, room_id):
            return list(self.rounds)

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            store = _NewEmptyRound()
            app._rooms = _FakeRooms(store=store)
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            monkeypatch.setattr(
                "syncmill.tui.build_room_evidence",
                lambda *a: (_comparison([]), 1, frozenset()),
            )
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            assert "Round 3 of 3" in app.candidate_detail_text
            # A later, empty round 4 lands: Compare must not present round 3
            # as the latest.
            store.rounds = [{"id": "round-1", "number": 3}, {"id": "round-2", "number": 4}]
            app._poll()
            await pilot.pause()
            text = app.candidate_detail_text
            assert "round 4 is not in the evidence snapshot" in text
            assert "press r" in text
            assert "Round 3 of 3" not in text

    drive(scenario())


def test_all_damaged_rounds_stay_visible_in_compare(tmp_path, git_repo, monkeypatch):
    from syncmill.room_compare import RoomComparison

    class _Damaged(_FakeCandidateRoomStore):
        def list_candidates(self, room_id):
            return []

    damaged = RoomComparison(
        room_id="room-a", room_state="awaiting_decision", revision=1,
        selected_candidate_id=None, source_status="current", source_reason=None,
        round_count=1, groups=(),
        damaged_rounds=({"round_id": "round-1", "reason": "malformed round number 'x'"},),
    )

    async def scenario():
        app = _room_apply_app(tmp_path, git_repo)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = _FakeRooms(store=_Damaged())
            monkeypatch.setattr(app, "_reload_config", lambda: True)
            monkeypatch.setattr(
                "syncmill.tui.build_room_evidence", lambda *a: (damaged, 1, frozenset())
            )
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("c")
            await _settle(app, pilot)
            text = app.candidate_detail_text
            assert "Damaged round round-1: malformed round number 'x'" in text
            assert "round 3 is not in the evidence snapshot" in text

    drive(scenario())
