from __future__ import annotations

import asyncio

from syncmill.board import Board
from syncmill.config import SyncmillConfig
from syncmill.tui import SyncmillTuiApp, _log_signature, _read_log_tail
from syncmill.tui_jobs import JobError, board_key


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
        assert app._jobs.db_path == new_db.resolve()
        assert app._jobs.key == board_key(new_db.resolve())
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


def test_room_apply_confirmation_captures_exact_candidate_and_revision(
    tmp_path, git_repo, monkeypatch
):
    async def scenario():
        calls: list[dict] = []

        class Store:
            def get_room(self, room_id):
                return {
                    "id": room_id,
                    "state": "selected",
                    "selected_candidate_id": "candidate-a",
                    "revision": 7,
                }

            def get_candidate(self, candidate_id):
                return {"id": candidate_id}

            def load_candidate(self, candidate_id):
                return {
                    "candidate_id": candidate_id,
                    "patch_sha256": "abc123",
                }

        class Rooms:
            store = Store()

            def apply(self, room_id, **kwargs):
                calls.append({"room_id": room_id, **kwargs})
                return "run-12345678", tmp_path / "receipt.json"

            def close(self):
                return None

        app = SyncmillTuiApp(
            SyncmillConfig(
                repo_root=str(git_repo),
                runs={"root": str(tmp_path / "runs")},
            ),
            board=None,
            board_error=None,
            toml_path=git_repo / "syncmill.toml",
            refresh_seconds=3600,
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = Rooms()
            app._selected_room_id = "room-a"
            captured: dict = {}

            def capture_screen(screen, callback):
                captured["callback"] = callback

            monkeypatch.setattr(app, "push_screen", capture_screen)
            monkeypatch.setattr(app, "_refresh_all", lambda: None)
            app._confirm_room_apply()
            app._selected_room_id = "room-b"
            captured["callback"](True)
            assert calls == [
                {
                    "room_id": "room-a",
                    "verify": False,
                    "expected_candidate_id": "candidate-a",
                    "expected_revision": 7,
                }
            ]

    drive(scenario())


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
            await pilot.press("6", "a")
            await pilot.pause()
            assert app.query_one("#tabs").active == "sessions-tab"

    drive(scenario())
