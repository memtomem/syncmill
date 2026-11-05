"""A room's goal (#174 follow-up 3): fixed when the room is created, stored as
typed, and every round of the room gets the project's lessons promoted for
exactly that goal next to the project-wide ones."""

from __future__ import annotations

import asyncio
import json
import shlex
import sqlite3

import pytest
from click.testing import CliRunner
from textual import events
from textual.widgets import Button, Input

import syncmill.rooms as rooms_module
from syncmill.board import Board, render_lessons_section
from syncmill.cli import cli
from syncmill.config import SyncmillConfig
from syncmill.room_compare import (
    build_room_comparison,
    comparison_to_dict,
    render_review_markdown,
    render_room_comparison,
)
from syncmill.rooms import (
    ROOM_GOAL_MAX_BYTES,
    RoomCoordinator,
    RoomError,
    RoomStore,
    RoundLesson,
    normalize_goal,
)
from syncmill.tui_collaboration import RoomCreateModal, RoomRoundModal, render_room
from syncmill.tui_render import ExactInput, PlainStatic
from test_lessons import judged_room  # noqa: F401
from test_room_lessons import _cli_config, _prompts, _round_cli
from test_rooms import SessionAgent, WritingAgent, _config
from test_tui_room_review import _app, _judged, _launches, _press_apply

PROV = {"kind": "room-judgment", "room_id": "r", "judgment_id": "j", "verdict": "accepted",
        "candidate_id": "c", "patch_sha256": "a" * 64, "applied_run_id": "run"}


def _promote(board, text, *, project, key, scope=""):
    return board.promote_lesson(
        text, project_id=project, provenance=PROV, source_key=key, scope=scope
    )[0]


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (b"g", "must be text"),
        ("", "must not be blank"),
        ("   ", "must not be blank"),
        ("a\nb", "control characters"),
        ("a\tb", "control characters"),
        # A strip-first check would let these through: str.strip() eats them.
        ("\ng", "control characters"),
        ("g\t", "control characters"),
        ("\rg", "control characters"),
        ("\x85g", "control characters"),
        ("g\x1f", "control characters"),
        ("g\x7f", "control characters"),
        ("\ud800", "not valid UTF-8"),
        ("가" * 67, "201 bytes; the limit is 200"),
    ],
)
def test_a_bad_goal_is_refused(value, message):
    with pytest.raises(RoomError, match=message):
        normalize_goal(value)


@pytest.mark.parametrize(
    "value",
    ["backend", " g ", "a\u2028b", "가" * 66 + "xx", "x" * ROOM_GOAL_MAX_BYTES, "-dash"],
)
def test_a_goal_is_kept_exactly_as_typed(value):
    assert normalize_goal(value) == value


def test_no_goal_is_none():
    assert normalize_goal(None) is None


# --------------------------------------------------------------------- store


def test_a_room_keeps_its_goal_verbatim(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        room = coordinator.create("task", ["claude"], goal=" g ")
        assert room["goal"] == " g "
        assert coordinator.store.get_room(room["id"])["goal"] == " g "
        assert [r["goal"] for r in coordinator.store.list_rooms()] == [" g "]
        [created] = [e for e in coordinator.store.events(room["id"]) if e["kind"] == "room.created"]
        assert created["payload"]["goal"] == " g "
    finally:
        coordinator.close()


def test_a_room_without_a_goal_records_none(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        room = coordinator.create("task", ["claude"])
        assert room["goal"] is None
        [created] = [e for e in coordinator.store.events(room["id"]) if e["kind"] == "room.created"]
        assert "goal" not in created["payload"]
    finally:
        coordinator.close()


def test_a_bad_goal_creates_no_room(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        with pytest.raises(RoomError, match="control characters"):
            coordinator.create("task", ["claude"], goal="g\nother")
        # The store refuses it too, for callers that bypass the coordinator.
        with pytest.raises(RoomError, match="must not be blank"):
            coordinator.store.create_room(
                repo_root=git_repo, prompt="task", agents=["claude"], goal=""
            )
        assert coordinator.store.list_rooms() == []
    finally:
        coordinator.close()


def test_a_populated_pre_goal_store_gains_the_column(tmp_path):
    root = tmp_path / "rooms"
    store = RoomStore(root)
    room = store.create_room(repo_root=tmp_path, prompt="old task", agents=["claude"])
    store.close()
    conn = sqlite3.connect(RoomStore(root).db_path)
    conn.execute("ALTER TABLE rooms DROP COLUMN goal")
    conn.commit()
    assert "goal" not in {r[1] for r in conn.execute("PRAGMA table_info(rooms)")}
    conn.close()

    for _ in range(2):  # the upgrade, then an ordinary reopen
        reopened = RoomStore(root)
        try:
            assert reopened.get_room(room["id"])["goal"] is None
            assert reopened.get_room(room["id"])["prompt"] == "old task"
        finally:
            reopened.close()


def test_a_concurrent_first_open_adding_the_goal_column_is_tolerated(tmp_path, monkeypatch):
    root = tmp_path / "rooms"
    RoomStore(root).close()  # the winner added the column
    real_connect = rooms_module.sqlite3.connect

    class StalePragma:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def execute(self, sql, *args):
            cursor = self._real.execute(sql, *args)
            if sql.strip().startswith("PRAGMA table_info(rooms)"):
                rows = [r for r in cursor.fetchall() if r["name"] != "goal"]
                return type("Stale", (), {"fetchall": staticmethod(lambda: rows)})()
            return cursor

    monkeypatch.setattr(
        rooms_module.sqlite3, "connect", lambda *a, **k: StalePragma(real_connect(*a, **k))
    )
    RoomStore(root).close()  # the loser: must open, not raise


# --------------------------------------------------------------------- board


@pytest.fixture
def board(tmp_path):
    value = Board(str(tmp_path / "board.db"))
    yield value
    value.close()


def test_project_lessons_for_a_goal_add_exactly_that_goals_lessons(board):
    wide = _promote(board, "project wide", project="p1", key="a")
    goal = _promote(board, "for g", project="p1", key="b", scope="g")
    _promote(board, "for padded g", project="p1", key="c", scope=" g ")
    _promote(board, "other goal", project="p1", key="d", scope="h")
    _promote(board, "other project", project="p2", key="e", scope="g")
    gone = _promote(board, "superseded", project="p1", key="f", scope="g")
    board.add_decision("its replacement", scope="g", supersedes=gone.id)
    board.add_decision("unbound", scope="g")
    later = _promote(board, "project wide, later", project="p1", key="g")

    texts = lambda ds: [d.text for d in ds]  # noqa: E731
    # Oldest first across both scopes; the replacement inherits p1.
    assert texts(board.project_lessons("p1", goal="g")) == [
        "project wide", "for g", "its replacement", "project wide, later",
    ]
    assert texts(board.project_lessons("p1", goal=" g ")) == [
        "project wide", "for padded g", "project wide, later",
    ]
    # Without a goal: today's project-wide lessons only.
    assert [d.id for d in board.project_lessons("p1")] == [wide.id, later.id]
    assert goal.id not in {d.id for d in board.project_lessons("p1", goal="h")}


# ----------------------------------------------------------------------- CLI


def _env(monkeypatch, git_repo, tmp_path, db=None):
    for key, value in {
        "SYNCMILL_ROOMS__ENABLED": "true",
        "SYNCMILL_PROJECT__ID": "goal-cli",
        "SYNCMILL_RUNS__ROOT": str(tmp_path / "runs"),
        "SYNCMILL_WORKSPACE__ROOT": str(tmp_path / "ws"),
    }.items():
        monkeypatch.setenv(key, value)
    if db is not None:
        monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
        monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(db))


def test_room_create_stores_and_shows_the_goal(monkeypatch, git_repo, tmp_path):
    _env(monkeypatch, git_repo, tmp_path)
    monkeypatch.setenv("SYNCMILL_CODEX__ENABLED", "false")
    monkeypatch.setenv("SYNCMILL_KIMI_CODE__ENABLED", "false")
    res = CliRunner().invoke(
        cli, ["room", "create", "task", "-a", "claude", "--goal", "back end", "--repo", str(git_repo)]
    )
    assert res.exit_code == 0, res.output
    room_id = res.output.splitlines()[0]
    assert "Goal: back end" in res.output
    listed = CliRunner().invoke(cli, ["room", "list", "--repo", str(git_repo)])
    assert listed.exit_code == 0, listed.output
    assert f"{room_id}" in listed.output and "goal:back end" in listed.output
    shown = CliRunner().invoke(cli, ["room", "show", room_id, "--repo", str(git_repo)])
    assert json.loads(shown.output)["goal"] == "back end"


def test_room_create_refuses_a_bad_goal(monkeypatch, git_repo, tmp_path):
    _env(monkeypatch, git_repo, tmp_path)
    res = CliRunner().invoke(
        cli, ["room", "create", "task", "--goal", "a\x1b[31mb", "--repo", str(git_repo)]
    )
    assert res.exit_code == 1
    assert "room goal contains control characters" in res.output
    listed = CliRunner().invoke(cli, ["room", "list", "--repo", str(git_repo)])
    assert listed.output.strip() == ""


def _goal_round_cli(monkeypatch, tmp_path, config, goal):
    captured = _round_cli(monkeypatch, tmp_path, config)
    import syncmill.cli as cli_module

    coordinator = cli_module._room_coordinator(None)[2]
    original = coordinator.store.get_room

    def get_room(room_id):
        return {**original(room_id), "goal": goal}

    coordinator.store.get_room = get_room
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator", lambda repo: (tmp_path, config, coordinator)
    )
    return captured


@pytest.fixture
def goal_board(tmp_path):
    db = tmp_path / "board.db"
    board = Board(str(db))
    wide = _promote(board, "prefer small diffs", project="proj-room", key="a")
    goal = _promote(board, "keep the API stable", project="proj-room", key="b", scope="api")
    other = _promote(board, "other goal", project="proj-room", key="c", scope="ui")
    board.close()
    return db, wide, goal, other


def test_a_round_of_a_goal_room_shows_and_sends_its_goal_lessons(
    monkeypatch, tmp_path, goal_board
):
    db, wide, goal, other = goal_board
    captured = _goal_round_cli(monkeypatch, tmp_path, _cli_config(db), "api")
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "next"])
    assert res.exit_code == 0, res.output
    assert (
        "lessons: 2 approved lesson(s) of this project and goal api go into "
        "every member's prompt this round"
    ) in res.output
    assert f"{wide.id}  prefer small diffs" in res.output
    assert f"{goal.id} (goal api)  keep the API stable" in res.output
    assert "other goal" not in res.output
    assert "(add --scope GOAL for a goal lesson)" in res.output
    assert captured["lessons"] == (
        RoundLesson(wide.id, wide.text), RoundLesson(goal.id, goal.text),
    )


def test_a_skipped_goal_lesson_is_not_sent(monkeypatch, tmp_path, goal_board):
    db, wide, goal, _ = goal_board
    captured = _goal_round_cli(monkeypatch, tmp_path, _cli_config(db), "api")
    res = CliRunner().invoke(
        cli, ["room", "round", "room-1", "next", "--detach", "--skip-lesson", goal.id]
    )
    assert res.exit_code == 0, res.output
    assert f"skipped: {goal.id} (goal api) (--skip-lesson)" in res.output
    assert "no active lesson with that id" not in res.output
    # Exactly the pairs the detached worker is handed over its lessons fd.
    assert captured["launched"] == (RoundLesson(wide.id, wide.text),)


def test_a_detached_round_hands_the_goal_lesson_to_the_worker(
    monkeypatch, git_repo, tmp_path, goal_board
):
    import os
    import threading
    from types import SimpleNamespace

    db, wide, goal, _ = goal_board
    _goal_round_cli(monkeypatch, git_repo, _cli_config(db), "api")
    # The real launcher, so what is checked is the --lessons-fd payload the
    # worker reads, resolved from the board through the room's goal.
    monkeypatch.setattr("syncmill.cli.launch_room_worker", rooms_module.launch_room_worker)
    payload: dict = {}
    reaped = threading.Event()

    def fake_popen(argv, **kwargs):
        fd = int(argv[argv.index("--lessons-fd") + 1])
        payload["lessons"] = json.loads(os.read(fd, 200_000))
        return SimpleNamespace(pid=4242, wait=lambda: reaped.set())

    monkeypatch.setattr(rooms_module.subprocess, "Popen", fake_popen)
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "next", "--detach"])
    assert res.exit_code == 0, res.output
    assert payload["lessons"] == [
        {"id": wide.id, "text": wide.text}, {"id": goal.id, "text": goal.text},
    ]
    assert reaped.wait(1)


def test_a_round_of_a_room_without_a_goal_sends_no_goal_lessons(
    monkeypatch, tmp_path, goal_board
):
    db, wide, goal, _ = goal_board
    captured = _goal_round_cli(monkeypatch, tmp_path, _cli_config(db), None)
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "next", "--skip-lesson", goal.id])
    assert res.exit_code == 0, res.output
    assert "lessons: 1 approved project lesson(s) go into" in res.output
    assert "goal" not in res.output.replace("--scope GOAL", "")
    # A goal lesson is unknown to a goal-less room, like any other id.
    assert f"--skip-lesson {goal.id}: no active lesson" in res.output
    assert captured["lessons"] == (RoundLesson(wide.id, wide.text),)


def test_goal_lessons_reach_a_goal_room_end_to_end(monkeypatch, git_repo, tmp_path):
    db = tmp_path / "board.db"
    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "proj-goal"},
        board={"enabled": True, "db_path": str(db)},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "ws")},
        rooms={"enabled": True},
    )
    agent = SessionAgent("claude")
    coordinator = RoomCoordinator(config, git_repo, agents=[agent])
    board = Board(str(db))
    padded = _promote(board, "padded scope lesson", project=coordinator.project_id,
                      key="a", scope=" g ")
    _promote(board, "bare scope lesson", project=coordinator.project_id, key="b", scope="g")
    board.close()
    room = coordinator.create("task", ["claude"], goal=" g ")
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator", lambda repo: (git_repo, config, coordinator)
    )
    res = CliRunner().invoke(cli, ["room", "round", room["id"], "go"])
    assert res.exit_code == 0, res.output
    # Matched exactly: the padded scope reaches the padded goal, the bare one does not.
    assert _prompts(agent)[-1].endswith(
        f"--- APPROVED PROJECT LESSONS ---\n{render_lessons_section([padded.text])}"
        "\n\n--- CURRENT USER TASK ---\ngo"
    )
    assert "bare scope lesson" not in _prompts(agent)[-1]
    coordinator.close()


def test_a_plain_run_still_gets_no_goal_lessons(monkeypatch, tmp_path):
    from conftest import FakeAgent
    from test_cli import _install_supervisor, _result

    db = tmp_path / "board.db"
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(db))
    monkeypatch.setenv("SYNCMILL_PROJECT__ID", "proj-run")
    board = Board(str(db))
    _promote(board, "wide", project="proj-run", key="a")
    _promote(board, "goal only", project="proj-run", key="b", scope="api")
    board.close()
    holder = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = CliRunner().invoke(cli, ["run", "do it"])
    assert res.exit_code == 0, res.output
    assert holder["run"]["lessons"] == ["wide"]
    assert "goal only" not in res.output


def _set_goal(git_repo, tmp_path, room_id, goal):
    config = SyncmillConfig(
        repo_root=str(git_repo), project={"id": "promote-e2e"},
        runs={"root": str(tmp_path / "runs")}, workspace={"root": str(tmp_path / "ws")},
        rooms={"enabled": True},
    )
    coordinator = RoomCoordinator(config, git_repo, agents=[WritingAgent("claude")])
    with coordinator.store._tx() as conn:
        conn.execute("UPDATE rooms SET goal=? WHERE id=?", (goal, room_id))
    coordinator.close()


@pytest.mark.parametrize(
    ("goal", "expected"),
    [
        ("api", "--scope=api --lesson TEXT"),
        ("back end's", "'--scope=back end'\"'\"'s' --lesson TEXT"),
        ("-x", "--scope=-x --lesson TEXT"),
    ],
)
def test_promoting_for_the_whole_project_names_the_rooms_goal(
    judged_room, git_repo, monkeypatch, tmp_path, goal, expected  # noqa: F811
):
    _set_goal(git_repo, tmp_path, judged_room, goal)
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(tmp_path / "board.db"))
    res = CliRunner().invoke(
        cli, ["room", "promote", judged_room, "--lesson", "x", "--repo", str(git_repo)]
    )
    assert res.exit_code == 0, res.output
    assert (
        f"to limit a lesson to it: syncmill room promote {judged_room} {expected} "
        f"--repo {shlex.quote(str(git_repo))}"
    ) in res.output


def test_the_promote_hint_omits_repo_when_it_was_not_given(
    judged_room, git_repo, monkeypatch, tmp_path  # noqa: F811
):
    _set_goal(git_repo, tmp_path, judged_room, "api")
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(tmp_path / "board.db"))
    monkeypatch.chdir(git_repo)
    res = CliRunner().invoke(cli, ["room", "promote", judged_room, "--lesson", "x"])
    assert res.exit_code == 0, res.output
    assert res.output.rstrip().endswith(
        f"syncmill room promote {judged_room} --scope=api --lesson TEXT"
    )


def test_a_goal_the_terminal_would_escape_gets_no_command(
    judged_room, git_repo, monkeypatch, tmp_path  # noqa: F811
):
    _set_goal(git_repo, tmp_path, judged_room, "a\u202eb")
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(tmp_path / "board.db"))
    res = CliRunner().invoke(
        cli, ["room", "promote", judged_room, "--lesson", "x", "--repo", str(git_repo)]
    )
    assert res.exit_code == 0, res.output
    assert "promote with --scope set to it" in res.output
    assert "syncmill room promote" not in res.output
    assert "\u202e" not in res.output


def test_a_goal_scoped_promotion_says_which_rooms_get_it(
    judged_room, git_repo, monkeypatch, tmp_path  # noqa: F811
):
    _set_goal(git_repo, tmp_path, judged_room, "api")
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(tmp_path / "board.db"))
    res = CliRunner().invoke(
        cli,
        ["room", "promote", judged_room, "--lesson", "x", "--scope", "api",
         "--repo", str(git_repo)],
    )
    assert res.exit_code == 0, res.output
    assert "rounds of rooms created with that goal get it" in res.output
    assert "to limit a lesson to it" not in res.output


# ------------------------------------------------------------ compare/export


HOSTILE = "``` [b]x[/b] \u202e end"


def test_compare_and_export_carry_the_goal_safely(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        room = coordinator.create("task", ["claude"], goal=HOSTILE)
        cmp = build_room_comparison(coordinator.store, room["id"])
        data = comparison_to_dict(cmp)
        assert data["room"]["goal"] == HOSTILE
        assert json.loads(json.dumps(data))["room"]["goal"] == HOSTILE

        text = render_room_comparison(cmp)
        [line] = [line for line in text.splitlines() if line.startswith("Goal:")]
        assert "\u202e" not in line and "[b]x[/b]" in line

        markdown = render_review_markdown(cmp, generated_at="now")
        lines = markdown.splitlines()
        at = lines.index("Goal (lessons promoted for it reach this room's rounds):")
        fence = lines[at + 2]
        assert set(fence) == {"`"} and len(fence) > 3  # longer than the goal's run
        assert lines[at + 4] == fence and "\u202e" not in lines[at + 3]
    finally:
        coordinator.close()


def test_a_room_without_a_goal_shows_no_goal_line(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        room = coordinator.create("task", ["claude"])
        cmp = build_room_comparison(coordinator.store, room["id"])
        assert comparison_to_dict(cmp)["room"]["goal"] is None
        assert "Goal" not in render_room_comparison(cmp)
        assert "Goal" not in render_review_markdown(cmp, generated_at="now")
    finally:
        coordinator.close()


# ----------------------------------------------------------------------- TUI


def drive(coro):
    return asyncio.run(coro)


def test_the_room_detail_shows_the_goal_as_plain_text():
    detail = render_room({"id": "r", "prompt": "p", "goal": "[b]x\u202e"}, [], [])
    assert "Goal: [b]x" in detail and "\u202e" not in detail
    assert "Goal" not in render_room({"id": "r", "prompt": "p", "goal": None}, [], [])


@pytest.mark.parametrize(
    ("pasted", "stored", "refused"),
    [
        ("a\u2028b", "a\u2028b", None),  # a plain Input keeps only "a"
        (" g ", " g ", None),
        ("g\nother", None, "control characters"),  # a plain Input stores "g"
    ],
)
def test_a_pasted_goal_reaches_the_coordinator_whole(
    git_repo, tmp_path, monkeypatch, pasted, stored, refused
):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    notes: list[str] = []

    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            monkeypatch.setattr(app, "notify", lambda message, **kw: notes.append(message))
            # What `a` on the Rooms tab opens, with its own callback.
            app.push_screen(RoomCreateModal(app._config), app._apply_room_create)
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RoomCreateModal)
            modal.query_one("#room-prompt").text = "task"
            modal.query_one("#room-agents", Input).value = "claude"
            field = modal.query_one("#room-goal", ExactInput)
            field.focus()
            field.post_message(events.Paste(pasted))
            await pilot.pause()
            assert field.value == pasted
            modal.on_button_pressed(Button.Pressed(modal.query_one("#apply", Button)))
            await pilot.pause()
            rooms = coordinator.store.list_rooms()
            if refused:
                assert rooms == [] and any(refused in note for note in notes)
            else:
                assert [r["goal"] for r in rooms] == [stored]

    try:
        drive(scenario())
    finally:
        coordinator.close()


def test_an_empty_goal_field_creates_a_room_without_a_goal(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])

    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._apply_room_create({"prompt": "task", "agents": "claude", "goal": ""})
            await pilot.pause()
            assert [r["goal"] for r in coordinator.store.list_rooms()] == [None]

    try:
        drive(scenario())
    finally:
        coordinator.close()


def test_a_tui_round_of_a_goal_room_sends_its_goal_lessons(git_repo, tmp_path, monkeypatch):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )
    room = coordinator.create("compare", ["claude", "codex"], goal="api")
    board = Board(str(tmp_path / "board.db"))
    wide = _promote(board, "wide", project=coordinator.project_id, key="a")
    goal = _promote(board, "goal lesson", project=coordinator.project_id, key="b", scope="api")
    _promote(board, "other goal", project=coordinator.project_id, key="c", scope="ui")

    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            launched = _launches(monkeypatch)
            app._confirm_room_round()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RoomRoundModal)
            shown = "\n".join(str(w.render()) for w in modal.query(".lesson-text"))
            assert "goal lesson" in shown and "for this room's goal api" in shown
            assert "other goal" not in shown
            hints = "\n".join(str(w.render()) for w in modal.query(PlainStatic))
            assert "(add --scope GOAL for a goal lesson)." in hints
            status = str(modal.query_one("#round-lessons-status").render())
            assert "Lessons this round: 2 of 2" in status
            modal.query_one("#confirmed").value = True
            await _press_apply(pilot, modal)
            [launch] = launched
            assert [(x.id, x.text) for x in launch["lessons"]] == [
                (wide.id, wide.text), (goal.id, goal.text),
            ]

    try:
        drive(scenario())
    finally:
        board.close()
        coordinator.close()


def test_a_pasted_promotion_scope_is_kept_whole(git_repo, tmp_path, monkeypatch):
    from textual.widgets import Checkbox, TextArea

    from syncmill.tui_collaboration import LessonPromoteModal

    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )
    room_id, _ = _judged(coordinator)
    with coordinator.store._tx() as conn:
        conn.execute("UPDATE rooms SET goal=? WHERE id=?", ("a\u2028b", room_id))
    board = Board(str(tmp_path / "board.db"))

    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            app._confirm_lesson_promote()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, LessonPromoteModal)
            assert "This room's goal is a" in modal._detail
            modal.query_one("#promote-lesson", TextArea).text = "keep it small"
            field = modal.query_one("#promote-scope", ExactInput)
            field.focus()
            field.post_message(events.Paste("a\u2028b"))
            await pilot.pause()
            modal.query_one("#confirmed", Checkbox).value = True
            await _press_apply(pilot, modal)
            # A plain Input would have stored scope "a", which reaches no such room.
            [lesson] = board.project_lessons(coordinator.project_id, goal="a\u2028b")
            assert lesson.scope == "a\u2028b"

    try:
        drive(scenario())
    finally:
        board.close()
        coordinator.close()
