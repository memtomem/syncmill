"""Opt-in, paid/networked TUI canary against the real Codex CLI.

Run explicitly:

    SYNCMILL_TEST_TUI_LIVE_CODEX=1 \
    SYNCMILL_LIVE_HOME="$HOME" \
    uv run pytest -q tests/test_tui_live_codex.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from textual.widgets import Button, Checkbox, Input, Select, TextArea

from syncmill.board import Board
from syncmill.tui import JobModal, SyncmillTuiApp
from syncmill.tui_collaboration import CollaborationConfirmModal, RoomCreateModal
from syncmill.teamfile import load_config_at


LIVE_ENABLED = os.environ.get("SYNCMILL_TEST_TUI_LIVE_CODEX") == "1"
LIVE_HOME = os.environ.get("SYNCMILL_LIVE_HOME")
pytestmark = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="set SYNCMILL_TEST_TUI_LIVE_CODEX=1 for two real Codex calls",
)
_TERMINAL_JOBS = {
    "finished",
    "cancelled",
    "force_stopped",
    "launch_failed",
    "lost",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


async def _wait_for_job(app: SyncmillTuiApp, op: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = [row for row in app._jobs.list_jobs() if row.get("op") == op]
        if rows and rows[0].get("status") in _TERMINAL_JOBS:
            return rows[0]
        await asyncio.sleep(0.5)
    raise AssertionError(f"timed out waiting for {op} TUI job")


async def _confirm_current_modal(app: SyncmillTuiApp, pilot) -> None:
    assert isinstance(app.screen, CollaborationConfirmModal)
    app.screen.query_one("#confirmed", Checkbox).value = True
    app.screen.query_one("#apply", Button).press()
    await pilot.pause()


def test_real_codex_board_and_room_flows_apply_only_after_tui_confirmation(
    tmp_path, git_repo, monkeypatch
):
    assert LIVE_HOME, "set SYNCMILL_LIVE_HOME to the authenticated user's home"
    assert shutil.which("codex"), "codex CLI is not on PATH"
    monkeypatch.setenv("HOME", LIVE_HOME)
    monkeypatch.setenv("CODEX_HOME", str(Path(LIVE_HOME) / ".codex"))

    auth = subprocess.run(
        ["codex", "login", "status"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert auth.returncode == 0, auth.stdout + auth.stderr

    board_db = tmp_path / "state" / "board.db"
    toml = git_repo / "syncmill.toml"
    toml.write_text(
        "config_version = 2\n"
        'repo_root = "."\n'
        'agent_priority = ["codex"]\n\n'
        '[project]\nid = "live-tui-canary"\n\n'
        "[codex]\nenabled = true\ntimeout_seconds = 120\n\n"
        "[claude]\nenabled = false\n\n"
        "[kimi_code]\nenabled = false\n\n"
        f'[board]\nenabled = true\ndb_path = "{board_db}"\n\n'
        f'[runs]\nroot = "{tmp_path / "state" / "runs"}"\n\n'
        f'[workspace]\nroot = "{tmp_path / "state" / "workspaces"}"\n\n'
        f'[tui]\nstate_dir = "{tmp_path / "state" / "tui"}"\n\n'
        "[rooms]\nenabled = true\nmax_parallel = 1\n\n"
        f'[memory]\ndb_path = "{tmp_path / "state" / "memory.db"}"\n'
        f'memory_dir = "{tmp_path / "state" / "memory"}"\n\n'
        "[gate]\ncommands = []\n",
        encoding="utf-8",
    )
    _git(git_repo, "add", "syncmill.toml")
    _git(git_repo, "commit", "-qm", "configure live TUI canary")
    config = load_config_at(toml)
    board = Board(str(board_db))
    item = board.add(
        "Create exactly one repository-root file named board-smoke.txt with "
        "exact UTF-8 content `board smoke` followed by one newline. Do not "
        "modify, create, rename, or delete any other file. Run git status "
        "before finishing and report the changed file."
    )

    async def scenario():
        app = SyncmillTuiApp(
            config,
            board=board,
            board_error=None,
            toml_path=toml,
            refresh_seconds=3600,
            initial_tab="board",
        )
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_item_id = item.id
            app._show_item(item.id)

            # Real Codex call 1: Board x → Operations → Artifact Summary/Diff.
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, JobModal)
            app.screen.query_one("#job-agent", Select).value = "codex"
            app.screen.query_one("#confirmed", Checkbox).value = True
            app.screen.query_one("#apply", Button).press()
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"
            board_job = await _wait_for_job(app, "board-run")
            assert board_job["status"] == "finished", board_job
            assert board_job["exit_code"] == 0, board_job
            app._refresh_jobs()
            assert "codex" in (
                Path(board_job["job_dir"]) / "console.log"
            ).read_text(encoding="utf-8", errors="replace").lower()

            await pilot.press("v")
            await pilot.pause()
            artifacts = app._artifacts.list()
            assert len(artifacts) == 1
            artifact = artifacts[0]
            assert artifact.manifest["state"] == "pending"
            assert artifact.manifest["files_changed"] == ["board-smoke.txt"]
            app._selected_run_id = artifact.run_id
            app._show_artifact(artifact.run_id)
            assert "board-smoke.txt" in app.artifact_detail_text
            await pilot.press("d")
            await pilot.pause()
            assert "board smoke" in app.artifact_detail_text
            assert not (git_repo / "board-smoke.txt").exists()

            await pilot.press("g")
            await pilot.pause()
            await _confirm_current_modal(app, pilot)
            apply_job = await _wait_for_job(app, "artifact-apply")
            assert apply_job["status"] == "finished", apply_job
            assert apply_job["exit_code"] == 0, apply_job
            assert (git_repo / "board-smoke.txt").read_text() == "board smoke\n"
            assert _git(git_repo, "status", "--porcelain").stdout.splitlines() == [
                "?? board-smoke.txt"
            ]
            _git(git_repo, "add", "board-smoke.txt")
            _git(git_repo, "commit", "-qm", "accept board canary")

            # Real Codex call 2: Room create (no call) → x round → d/o/m → e → g.
            await pilot.press("7")
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, RoomCreateModal)
            app.screen.query_one("#room-prompt", TextArea).text = (
                "Create exactly one repository-root file named room-smoke.txt "
                "with exact UTF-8 content `room smoke` followed by one newline. "
                "Do not modify, create, rename, or delete any other file."
            )
            app.screen.query_one("#room-agents", Input).value = "codex"
            app.screen.query_one("#apply", Button).press()
            await pilot.pause()
            room_id = app._selected_room_id
            assert room_id
            assert app._rooms.store.list_candidates(room_id) == []

            await pilot.press("x")
            await pilot.pause()
            await _confirm_current_modal(app, pilot)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                room = app._rooms.store.get_room(room_id)
                if room["state"] == "awaiting_decision":
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError("timed out waiting for real Codex room round")

            app._refresh_rooms()
            await pilot.pause()
            candidates = app._rooms.store.list_candidates(room_id)
            assert len(candidates) == 1
            candidate_id = candidates[0]["id"]
            manifest = app._rooms.store.load_candidate(candidate_id)
            assert manifest["eligible"] is True
            assert manifest["files_changed"] == ["room-smoke.txt"]
            app._selected_candidate_id = candidate_id
            app._show_candidate(candidate_id)
            assert "room smoke" in app.candidate_detail_text
            await pilot.press("o")
            await pilot.pause()
            assert "Output" in app.candidate_detail_text
            await pilot.press("m")
            await pilot.pause()
            assert "Details" in app.candidate_detail_text
            assert not (git_repo / "room-smoke.txt").exists()

            await pilot.press("e")
            await pilot.pause()
            await _confirm_current_modal(app, pilot)
            assert (
                app._rooms.store.get_room(room_id)["selected_candidate_id"]
                == candidate_id
            )
            await pilot.press("g")
            await pilot.pause()
            await _confirm_current_modal(app, pilot)
            room_apply_job = await _wait_for_job(app, "room-apply")
            assert room_apply_job["status"] == "finished", room_apply_job
            assert room_apply_job["exit_code"] == 0, room_apply_job
            assert (git_repo / "room-smoke.txt").read_text() == "room smoke\n"
            assert _git(git_repo, "status", "--porcelain").stdout.splitlines() == [
                "?? room-smoke.txt"
            ]

    try:
        asyncio.run(scenario())
    finally:
        board.close()
