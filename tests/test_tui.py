from __future__ import annotations

import asyncio

from textual.widgets import DataTable, OptionList

from syncmill.board import Board
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
            await pilot.pause()
            assert app.query_one("#tabs").active == "runs-tab"
            assert app._runs_pane() == "artifacts-pane"
            assert app._selected_run_id == "run-checkpoint"
            assert "Run: run-checkpoint" in app.artifact_detail_text

    drive(scenario())


class _FakeCandidateRoomStore:
    """Rooms store fake covering the candidate three-view read surface."""

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
            await pilot.pause()
            assert "[Details]" in app.candidate_detail_text
            assert "Round: 3" in app.candidate_detail_text
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
    from syncmill.tui_collaboration import (
        _patch_is_binary,
        render_candidate_details,
        render_candidate_diff,
    )

    assert _patch_is_binary(b"GIT binary patch")
    assert _patch_is_binary(b"Binary files a and b differ")
    assert _patch_is_binary(b"x\x00y")
    assert not _patch_is_binary(b"diff --git a/x b/x\n+line\n")
    manifest = {
        "candidate_id": "cand-1",
        "member_id": "claude",
        "provider_id": "claude",
        "attempt": 1,
        "state": "completed",
        "eligible": True,
        "patch_bytes": 4,
        "patch_sha256": "p",
        "output_bytes": 0,
        "output_sha256": "o",
        "output_truncated": True,
        "files_changed": [],
        "exit_code": 0,
        "duration_seconds": 1.0,
        "gate": {"passed": False, "score": 0.0, "checks": [
            {"command": "pytest -q", "passed": False, "exit_code": 1,
             "duration_seconds": 2.0},
        ]},
    }
    diff = render_candidate_diff(manifest, b"\x00bin", truncated=True)
    assert "[contains binary patch content]" in diff
    assert "load more" in diff
    details = render_candidate_details(manifest, None, patch_binary=True)
    assert "binary: yes" in details
    assert "Round: ?" in details
    assert "× pytest -q (exit 1" in details
    assert "truncated: True" in details


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
            controlled: list[tuple] = []
            monkeypatch.setattr(
                app, "_control_selected_job", lambda action: controlled.append(action)
            )
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("K")
            await pilot.pause()
            assert controlled == []
            await pilot.press("v")
            await pilot.pause()
            await pilot.press("K")
            await pilot.pause()
            assert controlled == ["force"]

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

    assert _read_artifact_result(tmp_path) is None
    (tmp_path / "result.json").write_text("{not json", encoding="utf-8")
    corrupt = _read_artifact_result(tmp_path)
    assert corrupt is not None and "unreadable" in corrupt["status"]
    monkeypatch.setattr("syncmill.tui._RESULT_LOAD_BYTES", 4)
    (tmp_path / "result.json").write_text('{"status": "ok"}', encoding="utf-8")
    oversized = _read_artifact_result(tmp_path)
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
            await pilot.press("7")
            await pilot.pause()
            options = app.query_one("#candidate-list", OptionList)
            options.highlighted = 1
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
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
