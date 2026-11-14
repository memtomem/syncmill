"""The TUI Rooms tab's human review actions (#174): recording a verdict (j),
promoting a lesson (p), and choosing approved lessons for a round. Driven
against a real RoomCoordinator and a real Board."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from textual.widgets import Button, Checkbox, Input, Select, TextArea

from test_room_judgments import _applied_room
from test_rooms import WritingAgent, _config
from conftest import ready_room_worker
from syncmill.board import Board, BoardError
from syncmill.config import SyncmillConfig
from syncmill.rooms import _ROOM_PROMPT_MAX_BYTES, RoomCoordinator, RoomError
from syncmill.tui import SyncmillTuiApp
from syncmill.tui_collaboration import (
    LessonPromoteModal,
    RoomJudgeModal,
    RoomRoundModal,
    RoundLessonsView,
    round_prompt,
)


def drive(coro):
    return asyncio.run(coro)


def _app(tmp_path, git_repo, *, board: Board | None, enabled: bool = True,
         board_error: str | None = None) -> SyncmillTuiApp:
    return SyncmillTuiApp(
        SyncmillConfig(
            repo_root=str(git_repo),
            runs={"root": str(tmp_path / "runs")},
            board={"enabled": enabled, "db_path": str(tmp_path / "board.db")},
        ),
        board=board,
        board_error=board_error,
        toml_path=git_repo / "syncmill.toml",
        refresh_seconds=3600,
        initial_tab="board",
    )


def _coordinator(git_repo, tmp_path) -> RoomCoordinator:
    return RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )


def _notes(app, monkeypatch) -> list[tuple[str, dict]]:
    notes: list[tuple[str, dict]] = []
    real = app.notify

    def notify(message, **kwargs):
        notes.append((message, kwargs))
        real(message, **kwargs)

    monkeypatch.setattr(app, "notify", notify)
    return notes


def _captured(app, monkeypatch) -> dict:
    staged: dict = {}
    monkeypatch.setattr(
        app, "push_screen",
        lambda screen, callback: staged.update(screen=screen, cb=callback),
    )
    return staged


async def _press_apply(pilot, modal) -> None:
    modal.on_button_pressed(Button.Pressed(modal.query_one("#apply", Button)))
    await pilot.pause()


# ------------------------------------------------------------------ verdict


def test_a_verdict_recorded_in_the_tui_is_bound_and_marked_tui(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, candidate, run_id = _applied_room(coordinator)
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            notes = _notes(app, monkeypatch)
            app._confirm_room_judge()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, RoomJudgeModal)
            assert candidate["patch_sha256"] in modal._detail
            assert f"Outcome: applied as run {run_id}, not reviewed" in modal._detail

            # No default verdict: confirming without one records nothing.
            modal.query_one("#confirmed", Checkbox).value = True
            await _press_apply(pilot, modal)
            assert app.screen is modal
            assert coordinator.store.outcome(room_id).status == "applied_unreviewed"

            modal.query_one("#judge-verdict", Select).value = "accepted"
            modal.query_one("#judge-rationale", TextArea).text = "  matches the spec  "
            await _press_apply(pilot, modal)
            outcome = coordinator.store.outcome(room_id)
            assert outcome.status == "accepted"
            assert outcome.judgment["surface"] == "tui"
            assert outcome.judgment["patch_sha256"] == candidate["patch_sha256"]
            # Normalized by the store, as for `room judge --rationale`.
            assert outcome.judgment["rationale"] == "matches the spec"
            assert notes[-1][0] == f"recorded accepted for candidate {candidate['id'][:8]}"
            assert "Outcome: ACCEPTED (human)" in app.room_detail_text

    drive(scenario())


@pytest.mark.parametrize("stage", ["created", "ran", "selected"])
def test_a_verdict_needs_an_applied_change(git_repo, tmp_path, monkeypatch, stage):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    if stage != "created":
        coordinator.run_round(room["id"])
    if stage == "selected":
        first = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], first["id"])
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            notes = _notes(app, monkeypatch)
            staged = _captured(app, monkeypatch)
            app._confirm_room_judge()
            assert staged == {}
            assert "only an applied change can be accepted or rejected" in notes[-1][0]

    drive(scenario())


def test_a_repeated_verdict_says_nothing_changed(git_repo, tmp_path, monkeypatch):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, candidate, _ = _applied_room(coordinator)
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            notes = _notes(app, monkeypatch)
            root = str(coordinator.store.root)
            fields = {"verdict": "rejected", "rationale": ""}
            app._apply_room_judge(fields, room_id, root, candidate["patch_sha256"])
            app._apply_room_judge(fields, room_id, root, candidate["patch_sha256"])
            assert notes[-1][0] == "already recorded: rejected (judgment 1); nothing changed"
            assert coordinator.store.outcome(room_id).judgment_count == 1

    drive(scenario())


def test_a_verdict_is_fenced_on_the_digest_and_store_it_was_reviewed_on(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, candidate, _ = _applied_room(coordinator)
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            notes = _notes(app, monkeypatch)
            fields = {"verdict": "accepted", "rationale": ""}
            app._apply_room_judge(fields, room_id, str(coordinator.store.root), "0" * 64)
            assert notes[-1][1].get("severity") == "error"
            assert "differs from the one you reviewed" in notes[-1][0]
            app._apply_room_judge(fields, room_id, "/another/store", candidate["patch_sha256"])
            assert "Rooms store changed while the dialog was open" in notes[-1][0]
            assert coordinator.store.outcome(room_id).judgment_count == 0

    drive(scenario())


def test_a_verdict_dialog_targets_the_room_it_was_opened_on(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, candidate, _ = _applied_room(coordinator)
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            staged = _captured(app, monkeypatch)
            app._confirm_room_judge()
            app._selected_room_id = "some-other-room"  # a poll moved the selection
            staged["cb"]({"verdict": "accepted", "rationale": ""})
            assert coordinator.store.outcome(room_id).status == "accepted"

    drive(scenario())


def test_a_broken_judgment_table_never_kills_the_app(git_repo, tmp_path, monkeypatch):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, candidate, _ = _applied_room(coordinator)
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            notes = _notes(app, monkeypatch)
            staged = _captured(app, monkeypatch)
            app._confirm_room_judge()  # opens on a healthy store
            coordinator.store._conn.execute("DROP TABLE room_judgments")
            staged["cb"]({"verdict": "accepted", "rationale": ""})
            assert notes[-1][1].get("severity") == "error"
            app._confirm_room_judge()
            assert notes[-1][1].get("severity") == "error"
            assert "no such table" in notes[-1][0]
            # The room pane stays up and says why the outcome is missing.
            app._refresh_rooms()
            assert "Outcome: unavailable (" in app.room_detail_text
            assert app.is_running

    drive(scenario())


# ------------------------------------------------------------------ promote


def _judged(coordinator, verdict="accepted"):
    room_id, candidate, run_id = _applied_room(coordinator)
    record, _ = coordinator.judge(room_id, verdict)
    return room_id, record


def test_promoting_needs_the_board(git_repo, tmp_path, monkeypatch):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, _ = _judged(coordinator)
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            notes = _notes(app, monkeypatch)
            app._confirm_lesson_promote()
            assert "set SYNCMILL_BOARD__ENABLED=true" in notes[-1][0]
            app._config.board.enabled = True
            app._board_error = "board.db is not a database"
            app._confirm_lesson_promote()
            assert notes[-1][0] == (
                "promoting a lesson needs the board, which did not open: "
                "board.db is not a database"
            )

    drive(scenario())


def test_promoting_needs_a_verdict(git_repo, tmp_path, monkeypatch):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, _, _ = _applied_room(coordinator)
    board = Board(str(tmp_path / "board.db"))
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            notes = _notes(app, monkeypatch)
            staged = _captured(app, monkeypatch)
            app._confirm_lesson_promote()
            assert staged == {}
            assert "room outcome is applied_unreviewed" in notes[-1][0]
            assert "record a verdict with j" in notes[-1][0]

    drive(scenario())


def test_a_lesson_promoted_in_the_tui_carries_the_judgment_it_was_reviewed_with(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, record = _judged(coordinator)
    board = Board(str(tmp_path / "board.db"))
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            notes = _notes(app, monkeypatch)
            app._confirm_lesson_promote()
            await pilot.pause()
            modal = app.screen
            assert isinstance(modal, LessonPromoteModal)
            assert record["judgment_id"] in modal._detail
            modal.query_one("#promote-lesson", TextArea).text = "prefer small diffs"
            modal.query_one("#confirmed", Checkbox).value = True
            await _press_apply(pilot, modal)
            [lesson] = board.project_lessons(coordinator.project_id)
            assert lesson.text == "prefer small diffs"
            assert lesson.provenance["judgment_id"] == record["judgment_id"]
            assert notes[-1][0] == f"promoted lesson {lesson.id[:8]} for the whole project"

            # The same promotion again changes nothing.
            app._apply_lesson_promote(
                {"lesson": "prefer small diffs", "scope": "", "supersedes": None},
                room_id, str(coordinator.store.root), record["judgment_id"],
            )
            assert notes[-1][0] == (
                f"already promoted as lesson {lesson.id[:8]} (active); nothing changed"
            )
            assert len(board.project_lessons(coordinator.project_id)) == 1

    drive(scenario())


def test_a_verdict_changed_during_the_dialog_is_not_promoted(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, _ = _judged(coordinator, "accepted")
    board = Board(str(tmp_path / "board.db"))
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            notes = _notes(app, monkeypatch)
            staged = _captured(app, monkeypatch)
            app._confirm_lesson_promote()
            coordinator.judge(room_id, "rejected")  # another terminal
            staged["cb"]({"lesson": "keep it", "scope": "", "supersedes": None})
            assert "verdict changed since you reviewed it" in notes[-1][0]
            assert board.project_lessons(coordinator.project_id) == []

    drive(scenario())


def test_only_this_projects_lessons_can_be_replaced(git_repo, tmp_path, monkeypatch):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, _ = _judged(coordinator)
    board = Board(str(tmp_path / "board.db"))
    project = coordinator.project_id
    prov = {"kind": "room-judgment", "room_id": "r", "judgment_id": "j",
            "verdict": "accepted", "candidate_id": "c", "patch_sha256": "a" * 64,
            "applied_run_id": "run"}
    own, _ = board.promote_lesson("own", project_id=project, provenance=prov,
                                  source_key="k1")
    goal, _ = board.promote_lesson("goal", project_id=project, provenance=prov,
                                   source_key="k2", scope="backend")
    board.promote_lesson("foreign", project_id="other", provenance=prov, source_key="k3")
    board.add_decision("unbound")
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            app._confirm_lesson_promote()
            await pilot.pause()
            modal = app.screen
            assert [(i, s) for _, i, s in modal._supersedable] == [
                (own.id, ""), (goal.id, "backend")
            ]
            # A replacement must share the scope: refused in the dialog.
            modal.query_one("#promote-lesson", TextArea).text = "better"
            modal.query_one("#promote-supersedes", Select).value = goal.id
            modal.query_one("#confirmed", Checkbox).value = True
            await _press_apply(pilot, modal)
            assert app.screen is modal
            modal.query_one("#promote-scope", Input).value = "backend"
            await _press_apply(pilot, modal)
            [replacement] = [
                d for d in board.active_decisions(project_id=project) if d.text == "better"
            ]
            assert replacement.scope == "backend"
            assert board.active_decisions(project_id=project).count(goal) == 0

    drive(scenario())


def test_a_board_that_fails_after_promotion_degrades_instead_of_crashing(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, record = _judged(coordinator)
    board = Board(str(tmp_path / "board.db"))
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id

            def broken(*args, **kwargs):
                raise BoardError("active_decisions failed: disk I/O error")

            monkeypatch.setattr(board, "active_decisions", broken)
            app._apply_lesson_promote(
                {"lesson": "prefer small diffs", "scope": "", "supersedes": None},
                room_id, str(coordinator.store.root), record["judgment_id"],
            )
            assert len(board.project_lessons(coordinator.project_id)) == 1
            assert "DEGRADED: active_decisions failed" in str(
                app.query_one("#board-status").render()
            )
            app.action_tab("board-tab")
            await pilot.pause()
            app._poll()
            assert app.is_running

    drive(scenario())


def test_the_board_tab_survives_an_unreadable_selected_item(
    git_repo, tmp_path, monkeypatch
):
    board = Board(str(tmp_path / "board.db"))
    item = board.add("a task")
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._selected_item_id = item.id

            def broken(*args, **kwargs):
                raise BoardError("get failed: database is locked")

            monkeypatch.setattr(board, "get", broken)
            app._refresh_board()
            assert "DEGRADED: get failed" in str(app.query_one("#board-status").render())
            assert app.is_running

    drive(scenario())


# ------------------------------------------------------------ round lessons


def _lessons(board, project, count, *, text="lesson {i}"):
    prov = {"kind": "room-judgment", "room_id": "r", "judgment_id": "j",
            "verdict": "accepted", "candidate_id": "c", "patch_sha256": "a" * 64,
            "applied_run_id": "run"}
    return [
        board.promote_lesson(text.format(i=i), project_id=project, provenance=prov,
                             source_key=f"k{i}")[0]
        for i in range(count)
    ]


def _launches(monkeypatch) -> list[dict]:
    launched: list[dict] = []

    def fake_launch(root, room_id, prompt=None, **kwargs):
        launched.append({"room_id": room_id, "prompt": prompt, **kwargs})
        return ready_room_worker(4242)

    monkeypatch.setattr("syncmill.tui.launch_room_worker", fake_launch)
    return launched


def test_a_round_sends_exactly_the_checked_lessons_with_the_clis_budget(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    lessons = _lessons(board, coordinator.project_id, 21)  # the budget is 20
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
            status = str(modal.query_one("#round-lessons-status").render())
            assert "Lessons this round: 20 of 21" in status
            assert f"left out: {lessons[0].id[:8]} (over the per-run lesson budget)" in status

            # Unchecking one of the newest frees budget for the oldest, as
            # --skip-lesson does.
            modal.query_one("#round-lesson-20", Checkbox).value = False
            await pilot.pause()
            status = str(modal.query_one("#round-lessons-status").render())
            assert f"left out: {lessons[20].id[:8]} (unchecked)" in status
            assert "over the per-run lesson budget" not in status

            modal.query_one("#confirmed", Checkbox).value = True
            await _press_apply(pilot, modal)
            [launch] = launched
            assert [(x.id, x.text) for x in launch["lessons"]] == [
                (x.id, x.text) for x in lessons[:20]
            ]
            assert launch["room_id"] == room["id"]

    drive(scenario())


def test_a_round_that_cannot_fit_its_lessons_is_refused_until_it_does(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    _lessons(board, coordinator.project_id, 1, text="x" * 900 + "{i}")
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            notes = _notes(app, monkeypatch)
            launched = _launches(monkeypatch)
            app._confirm_room_round()
            await pilot.pause()
            modal = app.screen
            modal.query_one("#round-prompt", TextArea).text = "p" * (_ROOM_PROMPT_MAX_BYTES - 200)
            modal.query_one("#confirmed", Checkbox).value = True
            await _press_apply(pilot, modal)
            assert app.screen is modal and launched == []
            assert "leaves no room for the checked lessons" in notes[-1][0]

            # Shortening the prompt lets it through with the lesson.
            modal.query_one("#round-prompt", TextArea).text = "short"
            await _press_apply(pilot, modal)
            assert len(launched) == 1 and len(launched[0]["lessons"]) == 1

    drive(scenario())


def test_the_fit_check_and_the_launch_read_a_blank_prompt_the_same_way(
    git_repo, tmp_path
):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            huge = {"prompt": "p" * (_ROOM_PROMPT_MAX_BYTES - 200)}
            view = app._round_lessons(room["id"], huge)
            assert round_prompt("  \n\t") is None
            assert round_prompt("  keep  ") == "  keep  "
            # None = the creation prompt, which here leaves no room.
            assert not view.fits(round_prompt("   "), ["x" * 900])
            assert view.fits(round_prompt("short"), ["x" * 900])

    drive(scenario())


def test_a_round_without_usable_lessons_says_why_and_sends_none(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            r = coordinator.store.get_room(room["id"])
            assert app._round_lessons(room["id"], r).lessons == ()

            def broken(project, goal=None):
                raise BoardError("project_lessons failed: disk I/O error")

            monkeypatch.setattr(board, "project_lessons", broken)
            view = app._round_lessons(room["id"], r)
            assert view.unavailable == "project_lessons failed: disk I/O error"
            app._board = None
            app._board_error = "board.db is locked"
            assert app._round_lessons(room["id"], r).unavailable == "board.db is locked"
            app._config.board.enabled = False
            view = app._round_lessons(room["id"], r)
            assert view.unavailable is None and view.lessons == ()

            launched = _launches(monkeypatch)
            app._apply_room_round(
                {"prompt": "go", "lineage": "fresh", "base_candidate": None, "lessons": []},
                room["id"], str(coordinator.store.root),
            )
            assert launched[0]["lessons"] == ()

    drive(scenario())


def test_a_lesson_id_the_dialog_did_not_show_is_refused(git_repo, tmp_path, monkeypatch):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            launched = _launches(monkeypatch)
            notes = _notes(app, monkeypatch)
            app._apply_room_round(
                {"prompt": "go", "lineage": "fresh", "base_candidate": None,
                 "lessons": ["00000000-0000-4000-8000-000000000000"]},
                room["id"], str(coordinator.store.root),
            )
            assert launched == [] and notes[-1][1].get("severity") == "error"

    drive(scenario())


def test_lesson_text_in_the_round_dialog_is_never_markup(git_repo, tmp_path):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    [lesson] = _lessons(
        board, coordinator.project_id, 1, text="[b]bold[/b] [oops \u202eRTL{i}"
    )

    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            app._confirm_room_round()
            await pilot.pause()
            label = app.screen.query_one("#round-lesson-0", Checkbox).label
            assert label.plain == f"lesson {lesson.id[:8]}" and not label.spans
            shown = app.screen.query_one("#round-lesson-text-0").render()
            assert "[b]bold[/b] [oops \\u202eRTL0" in str(shown)
            assert not getattr(shown, "spans", None)

    drive(scenario())


def test_a_long_lesson_is_shown_whole_in_the_round_dialog_at_80x24(git_repo, tmp_path):
    """Every word of a lesson is sent, so every word must be readable: a long
    lesson wraps inside the dialog instead of running past its edge."""
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    _lessons(board, coordinator.project_id, 1, text="word " * 80 + "FINALWORDS{i}")

    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            app._confirm_room_round()
            await pilot.pause()
            body = app.screen.query_one("#round-body")
            text = app.screen.query_one("#round-lesson-text-0")
            assert body.max_scroll_x == 0
            assert text.region.right <= body.region.right
            assert text.region.height > 1  # wrapped, not cut
            text.scroll_visible(animate=False)
            await pilot.pause()
            lines = [
                "".join(segment.text for segment in strip)
                for strip in text.render_lines(text.region.reset_offset)
            ]
            assert "FINALWORDS0" in "".join(lines)

    drive(scenario())


def test_the_round_dialog_keeps_its_buttons_reachable_at_80x24(git_repo, tmp_path):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    _lessons(board, coordinator.project_id, 21)
    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            app._confirm_room_round()
            await pilot.pause()
            for selector in ("#confirmed", "#apply", "#cancel"):
                region = app.screen.query_one(selector).region
                assert region.height > 0 and region.bottom <= 24, selector

    drive(scenario())


def test_the_history_note_follows_the_chosen_lineage(git_repo, tmp_path):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.push_screen(
                RoomRoundModal(
                    coordinator.store.get_room(room["id"]), "p", [],
                    RoundLessonsView(history={"continue-own": True, "branch-from": False}),
                )
            )
            await pilot.pause()
            modal = app.screen
            status = lambda: str(modal.query_one("#round-lessons-status").render())  # noqa: E731
            assert "may still hold lessons sent in earlier rounds" in status()
            modal.query_one("#round-lineage", Select).value = "fresh"
            await pilot.pause()
            assert status() == ""

    drive(scenario())


# ---------------------------------------------------------------- freshness


def _snapshot(app, coordinator, room_id):
    from syncmill.tui import build_room_evidence

    result = build_room_evidence(
        coordinator.store.root, coordinator.store.candidates.output_max_bytes,
        coordinator.repo_root, room_id, 0.0,
    )
    app._apply_evidence(
        app._evidence_generation, room_id, str(coordinator.store.root), result, None, 0.0
    )


def test_a_verdict_from_anywhere_marks_the_compare_snapshot_stale(
    git_repo, tmp_path
):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, _, _ = _applied_room(coordinator)
    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            _snapshot(app, coordinator, room_id)

            def text():
                return app._stage_evidence_view(room_id, None, "compare", 1)[1]

            assert "room changed since this snapshot" not in text()
            coordinator.judge(room_id, "accepted")  # e.g. `room judge` elsewhere
            assert "room changed since this snapshot" in text()
            _snapshot(app, coordinator, room_id)
            coordinator.judge(room_id, "accepted")  # idempotent: no new row
            assert "room changed since this snapshot" not in text()

            coordinator.store._conn.execute("DROP TABLE room_judgments")
            shown = text()
            assert "cannot tell whether a verdict changed since this snapshot" in shown
            assert "Outcome:" in shown  # the snapshot still renders

    drive(scenario())


# ------------------------------------------------------------- coordinator


def test_promote_lesson_refuses_a_verdict_other_than_the_one_reviewed(
    git_repo, tmp_path
):
    coordinator = _coordinator(git_repo, tmp_path)
    board = Board(str(tmp_path / "board.db"))
    try:
        room_id, first = _judged(coordinator, "accepted")
        coordinator.judge(room_id, "rejected")
        with pytest.raises(RoomError, match="verdict changed since you reviewed it"):
            coordinator.promote_lesson(
                room_id, board, "a lesson", expected_judgment_id=first["judgment_id"]
            )
        assert board.project_lessons(coordinator.project_id) == []
        latest = coordinator.store.outcome(room_id).judgment
        lesson, created = coordinator.promote_lesson(
            room_id, board, "a lesson", expected_judgment_id=latest["judgment_id"]
        )
        assert created and lesson.provenance["judgment_id"] == latest["judgment_id"]
    finally:
        board.close()
        coordinator.close()


def test_judgment_count_counts_rows(git_repo, tmp_path):
    coordinator = _coordinator(git_repo, tmp_path)
    try:
        room_id, _, _ = _applied_room(coordinator)
        assert coordinator.store.judgment_count(room_id) == 0
        coordinator.judge(room_id, "accepted")
        coordinator.judge(room_id, "rejected")
        assert coordinator.store.judgment_count(room_id) == 2
        assert coordinator.store.judgment_count("no-such-room") == 0
    finally:
        coordinator.close()


def test_a_blank_round_prompt_is_checked_against_the_creation_prompt(
    git_repo, tmp_path, monkeypatch
):
    """A whitespace-only prompt reuses the creation prompt, so the dialog's
    fit check must measure that one, not the whitespace."""
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("c" * (_ROOM_PROMPT_MAX_BYTES - 200), ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    _lessons(board, coordinator.project_id, 1, text="x" * 900 + "{i}")

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
            modal.query_one("#round-prompt", TextArea).text = "   \n"
            modal.query_one("#confirmed", Checkbox).value = True
            await _press_apply(pilot, modal)
            assert app.screen is modal and launched == []

    drive(scenario())


def test_a_room_that_turns_unreadable_after_the_round_dialog_opened_is_reported(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])

    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            notes = _notes(app, monkeypatch)
            staged = _captured(app, monkeypatch)
            launched = _launches(monkeypatch)
            app._confirm_room_round()
            with coordinator.store._tx() as conn:
                conn.execute(
                    "UPDATE rooms SET members_json='{not json' WHERE id=?", (room["id"],)
                )
            staged["cb"]({"prompt": "go", "lineage": "fresh", "base_candidate": None,
                          "lessons": []})
            await pilot.pause()
            assert launched == []
            assert notes[-1][1].get("severity") == "error"
            assert app.is_running

    drive(scenario())


def test_a_snapshot_that_could_not_count_verdicts_never_claims_it_is_current(
    git_repo, tmp_path, monkeypatch
):
    from syncmill.rooms import RoomStore

    coordinator = _coordinator(git_repo, tmp_path)
    room_id, _, _ = _applied_room(coordinator)
    real = RoomStore.judgment_count

    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id

            def failing(self, room_id):
                raise sqlite3.OperationalError("database is locked")

            monkeypatch.setattr(RoomStore, "judgment_count", failing)
            _snapshot(app, coordinator, room_id)  # the build cannot count
            monkeypatch.setattr(RoomStore, "judgment_count", real)
            coordinator.judge(room_id, "accepted")
            text = app._stage_evidence_view(room_id, None, "compare", 1)[1]
            assert "cannot tell whether a verdict changed since this snapshot" in text
            assert "this snapshot could not count verdicts" in text

    drive(scenario())


def test_the_round_dialog_says_when_the_board_is_off(git_repo, tmp_path):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])

    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            app._confirm_room_round()
            await pilot.pause()
            shown = " ".join(
                str(widget.render()) for widget in app.screen.query("PlainStatic")
            )
            assert "the board is off, so this round sends no approved lessons" in shown

    drive(scenario())


def test_the_verdict_dialog_shows_the_verdict_it_would_supersede(git_repo, tmp_path):
    coordinator = _coordinator(git_repo, tmp_path)
    room_id, _, _ = _applied_room(coordinator)
    coordinator.judge(room_id, "rejected", rationale="broke the parser")

    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room_id
            app._confirm_room_judge()
            await pilot.pause()
            detail = app.screen._detail
            assert "Outcome: REJECTED (human)" in detail
            assert "judged via API" in detail  # coordinator.judge default surface
            assert "broke the parser" in detail

    drive(scenario())


def test_lessons_can_be_toggled_and_submitted_with_the_mouse_at_80x24(
    git_repo, tmp_path, monkeypatch
):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("compare", ["claude", "codex"])
    board = Board(str(tmp_path / "board.db"))
    lessons = _lessons(board, coordinator.project_id, 21)

    async def scenario():
        app = _app(tmp_path, git_repo, board=board)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            launched = _launches(monkeypatch)
            app._confirm_room_round()
            await pilot.pause()
            modal = app.screen
            box = modal.query_one("#round-lesson-20", Checkbox)
            box.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#round-lesson-20")
            await pilot.pause()
            assert box.value is False
            await pilot.click("#confirmed")
            await pilot.pause()
            await pilot.click("#apply")
            await pilot.pause()
            [launch] = launched
            assert [x.id for x in launch["lessons"]] == [x.id for x in lessons[:20]]

    drive(scenario())


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
@pytest.mark.parametrize("dialog", ["round", "judge", "promote"])
def test_text_typed_into_a_dialog_editor_stays_visible(tmp_path, git_repo, size, dialog):
    """A TextArea's default 1fr height collapses to its border inside a
    scrolling dialog body: typing changed the value but showed nothing."""
    board = Board(str(tmp_path / "board.db"))
    lessons = tuple(_lessons(board, "proj", 3))

    async def scenario():
        app = _app(tmp_path, git_repo, board=None, enabled=False)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            modal, selector = {
                "round": (
                    RoomRoundModal(
                        {"id": "r", "members": [], "round_count": 0}, "", [],
                        RoundLessonsView(lessons=lessons),
                    ),
                    "#round-prompt",
                ),
                "judge": (RoomJudgeModal("Room: r\n" * 8), "#judge-rationale"),
                "promote": (LessonPromoteModal("source\n" * 8, []), "#promote-lesson"),
            }[dialog]
            app.push_screen(modal)
            await pilot.pause()
            editor = app.screen.query_one(selector, TextArea)
            editor.focus()
            editor.scroll_visible(animate=False)
            await pilot.pause()
            await pilot.press(*"visible words")
            await pilot.pause()
            assert editor.text == "visible words"
            assert editor.content_region.height >= 3
            shown = "".join(
                "".join(segment.text for segment in editor.render_line(y))
                for y in range(editor.region.height)
            )
            assert "visible words" in shown

    drive(scenario())


def test_the_round_dialog_does_not_offer_a_repairs_prompt_to_every_member(git_repo, tmp_path):
    coordinator = _coordinator(git_repo, tmp_path)
    room = coordinator.create("the room prompt", ["claude", "codex"])
    coordinator.run_round(room["id"], "for every member")
    parent = next(
        row for row in coordinator.store.list_candidates(room["id"]) if row["member_id"] == "claude"
    )
    plan = coordinator.build_repair_plan(parent["id"], "fix your own file")
    coordinator.run_round(room["id"], plan.prompt, target=plan)

    async def scenario():
        app = _app(tmp_path, git_repo, board=None)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._rooms = coordinator
            app._selected_room_id = room["id"]
            app._confirm_room_round()
            await pilot.pause()
            assert isinstance(app.screen, RoomRoundModal)
            assert app.screen._default_prompt == "for every member"
            assert any("repaired one member" in n.message for n in app._notifications)

    asyncio.run(scenario())
    coordinator.close()

