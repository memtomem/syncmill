"""A promoted lesson's scope (#257): one line, no control characters, checked
before anything is read or written; otherwise kept exactly as typed."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256

import pytest
from click.testing import CliRunner
from textual import events
from textual.widgets import Checkbox, TextArea

from syncmill.board import Board, BoardError, has_control_characters
from syncmill.cli import cli
from syncmill.rooms import RoomError
from syncmill.tui_collaboration import LessonPromoteModal
from syncmill.tui_render import ExactInput
from test_lessons import PROV, board, coordinator, judged_room  # noqa: F401
from test_room_judgments import _applied_room
from test_tui_room_review import _app, _coordinator, _judged, _press_apply

BAD = ["a\nb", "g\t", "\rg", "\x85g", "\x1b[31m", "g\x7f", "\x9f"]


def _judged_room(rooms):
    room_id, _, _ = _applied_room(rooms)
    judgment, _ = rooms.judge(room_id, "accepted")
    return room_id, judgment


@pytest.mark.parametrize("value", BAD)
def test_the_predicate_matches_the_goal_rule(value):
    assert has_control_characters(value)


@pytest.mark.parametrize("value", ["", " g ", "a\u2028b", "가나", "back end"])
def test_one_line_text_has_no_control_characters(value):
    assert not has_control_characters(value)


@pytest.mark.parametrize("scope", BAD)
def test_a_scope_with_control_characters_is_refused(coordinator, board, scope):  # noqa: F811
    room_id, _ = _judged_room(coordinator)
    with pytest.raises(RoomError, match="lesson scope contains control characters"):
        coordinator.promote_lesson(room_id, board, "keep it small", scope=scope)
    assert board.active_decisions() == []


@pytest.mark.parametrize("scope", ["", " g ", "a\u2028b"])
def test_a_one_line_scope_is_kept_as_typed(coordinator, board, scope):  # noqa: F811
    room_id, _ = _judged_room(coordinator)
    lesson, created = coordinator.promote_lesson(room_id, board, "keep it small", scope=scope)
    assert created and lesson.scope == scope


def test_the_scope_is_refused_before_anything_is_read(coordinator, board, monkeypatch):  # noqa: F811
    def no_read(*args, **kwargs):
        raise AssertionError("read before the scope was checked")

    monkeypatch.setattr(coordinator.store, "get_room", no_read)
    monkeypatch.setattr(coordinator.store, "outcome", no_read)
    with pytest.raises(RoomError, match="lesson scope contains control characters"):
        coordinator.promote_lesson("any-room", board, "x", scope="a\nb")


def test_the_board_refuses_it_from_a_direct_caller(board):  # noqa: F811
    with pytest.raises(BoardError, match="lesson scope contains control characters"):
        board.promote_lesson("x", project_id="p", provenance=PROV, source_key="k",
                             scope="a\nb")
    assert board.active_decisions() == []
    # Hand-written decisions are not checked (unchanged on purpose).
    decided = board.add_decision("hand written", scope="a\nb")
    assert decided.scope == "a\nb"


def test_a_legacy_control_scope_lesson_is_left_alone_and_not_retried(coordinator, board):  # noqa: F811
    room_id, judgment = _judged_room(coordinator)
    scope, text = "a\nb", "keep it small"
    key = sha256(
        json.dumps([coordinator.project_id, judgment["judgment_id"], scope, text],
                   ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    legacy, _ = board._record_decision(
        text, scope=scope, supersedes=None, decision_id=None,
        project_id=coordinator.project_id, provenance=PROV, source_key=key,
    )
    with pytest.raises(RoomError, match="control characters"):
        coordinator.promote_lesson(room_id, board, text, scope=scope)
    [row] = board.active_decisions()
    assert (row.id, row.scope, row.text) == (legacy.id, scope, text)
    # It still reaches board items of exactly that goal.
    assert [d.id for d in board.active_decisions(goal=scope,
                                                 project_id=coordinator.project_id)] == [legacy.id]


def test_a_padded_scope_retry_after_supersede_returns_the_original(coordinator, board):  # noqa: F811
    room_id, _ = _judged_room(coordinator)
    first, _ = coordinator.promote_lesson(room_id, board, "keep it small", scope=" g ")
    board.add_decision("keep it tiny", scope=" g ", supersedes=first.id)
    again, created = coordinator.promote_lesson(room_id, board, "keep it small", scope=" g ")
    assert not created and again.id == first.id and again.superseded_by is not None


def test_room_promote_refuses_a_multi_line_scope(judged_room, git_repo, monkeypatch, tmp_path):  # noqa: F811
    db = tmp_path / "board.db"
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(db))
    res = CliRunner().invoke(
        cli, ["room", "promote", judged_room, "--lesson", "x", "--scope", "a\nb",
              "--repo", str(git_repo)],
    )
    assert res.exit_code == 1
    assert "lesson scope contains control characters" in res.output
    value = Board(str(db))
    try:
        assert value.active_decisions() == []
    finally:
        value.close()


def drive(coro):
    return asyncio.run(coro)


def test_the_tui_dialog_keeps_a_bad_scope_open_with_the_lesson(git_repo, tmp_path, monkeypatch):
    rooms = _coordinator(git_repo, tmp_path)
    room_id, _ = _judged(rooms)
    store = Board(str(tmp_path / "board.db"))
    warnings: list[str] = []

    async def scenario():
        app = _app(tmp_path, git_repo, board=store)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = rooms
            app._selected_room_id = room_id
            app._confirm_lesson_promote()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, LessonPromoteModal)
            real = app.notify

            def notify(message, **kwargs):
                warnings.append(message)
                real(message, **kwargs)

            monkeypatch.setattr(app, "notify", notify)
            modal.query_one("#promote-lesson", TextArea).text = "keep it small"
            field = modal.query_one("#promote-scope", ExactInput)
            field.focus()
            field.post_message(events.Paste("g\nother"))
            await pilot.pause()
            modal.query_one("#confirmed", Checkbox).value = True
            await _press_apply(pilot, modal)
            assert app.screen is modal  # still open
            assert any("a scope is one line" in w for w in warnings)
            assert modal.query_one("#promote-lesson", TextArea).text == "keep it small"
            assert store.active_decisions() == []

            field.value = "g"
            await _press_apply(pilot, modal)
            [lesson] = store.active_decisions()
            assert (lesson.scope, lesson.text) == ("g", "keep it small")

    try:
        drive(scenario())
    finally:
        store.close()
        rooms.close()
