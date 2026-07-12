from __future__ import annotations

import asyncio

from syncmill.board import Board
from syncmill.config import SyncmillConfig
from syncmill.tui import SyncmillTuiApp, _log_signature, _read_log_tail


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
