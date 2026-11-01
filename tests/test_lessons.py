"""Promoted project lessons (#174 PR 2): board storage, promotion from a judged
room, project scoping, and what a run shows and injects."""

from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner

from conftest import FakeAgent, FakeBoard, FakeMemory, FakeRegistry
from test_cli import _install_supervisor, _result
from test_room_judgments import _applied_room, _room_with_candidates
from test_rooms import WritingAgent, _config
from test_supervisor import _item, _supervisor, patched_worktree  # noqa: F401
from syncmill.board import (
    LESSONS_MAX_BYTES,
    LESSONS_MAX_COUNT,
    Board,
    BoardError,
    Decision,
    render_decision,
    render_lessons_section,
    select_lessons,
)
from syncmill.cli import _inner_argv, cli
from syncmill.models import Task
from syncmill.rooms import LESSON_MAX_BYTES, RoomCoordinator, RoomError

PROV = {"kind": "room-judgment", "room_id": "r1", "judgment_id": "j1", "verdict": "accepted",
        "candidate_id": "c1", "patch_sha256": "a" * 64, "applied_run_id": "run1"}


@pytest.fixture
def board(tmp_path):
    value = Board(str(tmp_path / "board.db"))
    yield value
    value.close()


@pytest.fixture
def coordinator(git_repo, tmp_path):
    value = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )
    yield value
    value.close()


def _promote(board, text="prefer small diffs", *, project="p1", key="k1", **kw):
    return board.promote_lesson(text, project_id=project, provenance=PROV, source_key=key, **kw)


# ------------------------------------------------------------------ board


def test_a_promoted_lesson_is_a_project_bound_decision_with_provenance(board):
    lesson, created = _promote(board)
    assert created
    assert lesson.project_id == "p1" and lesson.scope == ""
    [stored] = board.project_lessons("p1")
    assert stored.id == lesson.id and stored.provenance == PROV
    assert "lesson from room r1, candidate c1 (accepted by a human)" in render_decision(stored)


def test_a_retried_promotion_returns_the_same_lesson(board):
    first, _ = _promote(board)
    again, created = _promote(board)
    assert not created and again.id == first.id
    assert len(board.project_lessons("p1")) == 1


def test_a_retry_cannot_bring_back_a_superseded_lesson(board):
    first, _ = _promote(board)
    replacement = board.add_decision("prefer tiny diffs", supersedes=first.id)
    # The hand-written replacement stays bound to the lesson's project.
    assert replacement.project_id == "p1"
    again, created = _promote(board)
    assert not created
    assert again.id == first.id and again.superseded_by == replacement.id
    assert [d.id for d in board.project_lessons("p1")] == [replacement.id]


def test_lessons_never_leak_into_another_project(board):
    board.add_decision("unbound team rule")
    _promote(board, project="p1", key="a")
    _promote(board, "other project's lesson", project="p2", key="b")
    texts = lambda ds: sorted(d.text for d in ds)  # noqa: E731
    assert texts(board.active_decisions(project_id="p1")) == [
        "prefer small diffs",
        "unbound team rule",
    ]
    assert texts(board.active_decisions(project_id=None)) == ["unbound team rule"]
    assert len(board.active_decisions()) == 3  # the full `board decisions` view
    assert texts(board.project_lessons("p2")) == ["other project's lesson"]


def test_project_lessons_hold_only_active_project_wide_lessons(board):
    _promote(board, "project wide", key="a")
    _promote(board, "goal only", key="b", scope="backend")
    board.add_decision("unbound")
    assert [d.text for d in board.project_lessons("p1")] == ["project wide"]
    assert [d.text for d in board.active_decisions(goal="backend", project_id="p1")] == [
        "project wide",
        "goal only",
        "unbound",
    ]


def test_supersede_never_crosses_projects(board):
    unbound = board.add_decision("unbound")
    other, _ = _promote(board, project="p2", key="b")
    with pytest.raises(BoardError, match="different project"):
        _promote(board, key="c", supersedes=unbound.id)
    with pytest.raises(BoardError, match="different project"):
        _promote(board, key="d", supersedes=other.id)
    assert board.project_lessons("p1") == []
    assert other.id in {d.id for d in board.active_decisions()}


def test_promote_lesson_requires_a_project_and_a_key(board):
    with pytest.raises(BoardError, match="project"):
        board.promote_lesson("x", project_id="", provenance=PROV, source_key="k")
    with pytest.raises(BoardError, match="key"):
        board.promote_lesson("x", project_id="p", provenance=PROV, source_key="")


def test_an_old_board_gains_the_lesson_columns(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE decisions (id TEXT PRIMARY KEY, text TEXT NOT NULL,
        scope TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
        superseded_by TEXT, superseded_at REAL);
        INSERT INTO decisions VALUES ('old', 'legacy rule', '', 1.0, NULL, NULL);"""
    )
    conn.commit()
    conn.close()
    board = Board(str(path))
    try:
        [legacy] = board.active_decisions(project_id="p1")
        assert legacy.text == "legacy rule" and legacy.project_id is None
        assert render_decision(legacy).count("\n") == 0  # no provenance line
        _promote(board)
        assert len(board.project_lessons("p1")) == 1
    finally:
        board.close()


def test_a_damaged_provenance_reads_as_not_recorded(board):
    lesson, _ = _promote(board)
    board._conn.execute(
        "UPDATE decisions SET provenance_json='{oops' WHERE id=?", (lesson.id,)
    )
    [stored] = board.project_lessons("p1")
    assert stored.provenance is None
    assert "provenance not recorded" in render_decision(stored)


def _decision(i, text=None):
    return Decision(id=f"d{i}", text=text or f"lesson {i}", project_id="p")


def test_select_lessons_skips_and_keeps_the_newest_within_budget():
    lessons = [_decision(i) for i in range(LESSONS_MAX_COUNT + 3)]
    used, skipped, over = select_lessons(lessons, skip=frozenset({"d0"}))
    assert [d.id for d in skipped] == ["d0"]
    assert len(used) == LESSONS_MAX_COUNT
    assert used[-1].id == lessons[-1].id  # newest kept, oldest-first order
    assert [d.id for d in over] == ["d1", "d2"]
    big = [_decision(i, "x" * (LESSONS_MAX_BYTES // 2)) for i in range(3)]
    used, _, over = select_lessons(big)
    assert [d.id for d in used] == ["d2"] and [d.id for d in over] == ["d0", "d1"]


def test_render_lessons_section():
    assert render_lessons_section([]) == ""
    text = render_lessons_section(["a", "b"])
    assert text.startswith("Project lessons a human approved for reuse")
    assert text.endswith("\n- a\n- b")


# ----------------------------------------------------------- room promotion


def test_only_a_judged_room_can_promote(coordinator, board):
    room_id, _, _ = _applied_room(coordinator)
    with pytest.raises(RoomError, match="applied_unreviewed.*room judge"):
        coordinator.promote_lesson(room_id, board, "keep diffs small")
    unselected, _ = _room_with_candidates(coordinator)
    with pytest.raises(RoomError, match="not_selected"):
        coordinator.promote_lesson(unselected, board, "x")
    assert board.active_decisions() == []


@pytest.mark.parametrize("verdict", ["accepted", "rejected"])
def test_promotion_records_where_the_lesson_came_from(coordinator, board, verdict):
    room_id, candidate, run_id = _applied_room(coordinator)
    judgment, _ = coordinator.judge(room_id, verdict)
    lesson, created = coordinator.promote_lesson(room_id, board, "  keep diffs small  ")
    assert created and lesson.text == "keep diffs small"
    assert lesson.project_id == coordinator.project_id
    assert lesson.provenance == {
        "kind": "room-judgment",
        "room_id": room_id,
        "judgment_id": judgment["judgment_id"],
        "verdict": verdict,
        "candidate_id": candidate["id"],
        "patch_sha256": candidate["patch_sha256"],
        "applied_run_id": run_id,
    }
    again, created = coordinator.promote_lesson(room_id, board, "keep diffs small")
    assert not created and again.id == lesson.id


def test_a_new_verdict_makes_a_new_promotion(coordinator, board):
    room_id, _, _ = _applied_room(coordinator)
    coordinator.judge(room_id, "accepted")
    first, _ = coordinator.promote_lesson(room_id, board, "x")
    coordinator.judge(room_id, "rejected")
    second, created = coordinator.promote_lesson(room_id, board, "x", supersedes=first.id)
    assert created and second.provenance["verdict"] == "rejected"
    assert [d.id for d in board.project_lessons(coordinator.project_id)] == [second.id]


@pytest.mark.parametrize(
    "text, message",
    [("   ", "must not be empty"), ("a\x1bb", "control characters"),
     ("x" * (LESSON_MAX_BYTES + 1), "limit")],
)
def test_lesson_text_is_validated(coordinator, board, text, message):
    room_id, _, _ = _applied_room(coordinator)
    coordinator.judge(room_id, "accepted")
    with pytest.raises(RoomError, match=message):
        coordinator.promote_lesson(room_id, board, text)


def test_promoting_from_another_project_is_refused(coordinator, board, tmp_path):
    room_id, _, _ = _applied_room(coordinator)
    coordinator.judge(room_id, "accepted")
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
    other = RoomCoordinator(_config(tmp_path), other_repo, agents=[WritingAgent("claude")])
    try:
        with pytest.raises(RoomError, match="different repository"):
            other.promote_lesson(room_id, board, "x")
    finally:
        other.close()


# --------------------------------------------------------------- supervisor


@pytest.mark.usefixtures("patched_worktree")
def test_supervisor_puts_lessons_ahead_of_the_task():
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    task = Task(prompt="fix the parser")
    sup.run(task, lessons=["prefer small diffs", "never touch vendored code"])
    prompt = sup._registry.get("claude").calls[0]["prompt"]
    lessons_at = prompt.index("Project lessons a human approved")
    assert lessons_at < prompt.index("- prefer small diffs") < prompt.index("fix the parser")
    assert "- never touch vendored code" in prompt
    assert task.prompt.endswith("fix the parser")  # the artifact sees what agents saw


@pytest.mark.usefixtures("patched_worktree")
def test_supervisor_without_lessons_leaves_the_prompt_alone():
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory())
    sup.run(Task(prompt="fix the parser"), lessons=[])
    assert "Project lessons" not in sup._registry.get("claude").calls[0]["prompt"]


@pytest.mark.usefixtures("patched_worktree")
def test_board_items_get_decisions_only_for_their_project():
    board = FakeBoard(item=_item(goal="epic"))
    sup = _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), board=board)
    sup._config.project.id = "proj-x"
    sup.run_item(item_id="item-1")
    assert board.decision_project_ids == ["proj-x"]


# --------------------------------------------------------------------- CLI


@pytest.fixture
def lesson_env(monkeypatch, tmp_path):
    db = tmp_path / "board.db"
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(db))
    monkeypatch.setenv("SYNCMILL_PROJECT__ID", "proj-cli")
    board = Board(str(db))
    yield board
    board.close()


def test_run_shows_and_injects_the_projects_lessons(monkeypatch, lesson_env):
    used, _ = _promote(lesson_env, "prefer small diffs", project="proj-cli", key="a")
    skipped, _ = _promote(lesson_env, "skip me", project="proj-cli", key="b")
    _promote(lesson_env, "someone else's", project="other", key="c")
    holder = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = CliRunner().invoke(cli, ["run", "do it", "--skip-lesson", skipped.id])
    assert res.exit_code == 0, res.output
    assert "1 approved project lesson(s) go into the prompt" in res.output
    assert f"{used.id}  prefer small diffs" in res.output
    assert "lesson from room r1" in res.output
    assert f"skipped: {skipped.id}" in res.output
    assert "someone else's" not in res.output
    assert "--supersedes ID" in res.output
    assert holder["run"]["lessons"] == ["prefer small diffs"]
    # The preview comes before the run starts.
    assert res.output.index("lessons:") < res.output.index("Executing task")


def test_run_with_no_lessons_uses_none(monkeypatch, lesson_env):
    _promote(lesson_env, project="proj-cli")
    holder = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = CliRunner().invoke(cli, ["run", "do it", "--no-lessons", "--skip-lesson", "nope"])
    assert res.exit_code == 0, res.output
    assert "1 approved, none used (--no-lessons)" in res.output
    assert "--skip-lesson nope: no active lesson" in res.output
    assert holder["run"]["lessons"] == []


def test_run_without_the_board_says_nothing_about_lessons(monkeypatch):
    monkeypatch.delenv("SYNCMILL_BOARD__ENABLED", raising=False)
    holder = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = CliRunner().invoke(cli, ["run", "do it"])
    assert res.exit_code == 0, res.output
    assert "lesson" not in res.output
    assert holder["run"]["lessons"] == []


def test_an_unreadable_board_warns_and_runs_without_lessons(monkeypatch, tmp_path):
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    bad = tmp_path / "board.db"
    bad.write_bytes(b"not a sqlite database at all" * 10)
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(bad))
    holder = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = CliRunner().invoke(cli, ["run", "do it"])
    assert res.exit_code == 0, res.output
    assert "lessons unavailable" in res.output
    assert holder["run"]["lessons"] == []


def test_monitored_runs_forward_lesson_choices():
    argv = _inner_argv(
        prompt="p", agent=None, exclude=[], timeout=None, repo=None, capability=[],
        strategy=None, skip_lessons=["b", "a"], no_lessons=True,
    )
    assert argv[-2:] == ["--", "p"]
    assert argv[argv.index("--no-lessons") :][:5] == [
        "--no-lessons", "--skip-lesson", "a", "--skip-lesson", "b",
    ]
    plain = _inner_argv(
        prompt="p", agent=None, exclude=[], timeout=None, repo=None, capability=[],
        strategy=None,
    )
    assert "--no-lessons" not in plain and "--skip-lesson" not in plain


def test_millflow_refuses_lesson_flags():
    res = CliRunner().invoke(cli, ["run", "millflow: plan", "--no-lessons"])
    assert res.exit_code == 2 and "--no-lessons" in res.output


@pytest.fixture
def judged_room(monkeypatch, git_repo, tmp_path):
    from syncmill.config import SyncmillConfig

    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "promote-e2e"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "ws")},
        rooms={"enabled": True},
    )
    seed = RoomCoordinator(
        config, git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )
    room_id, _, _ = _applied_room(seed)
    seed.judge(room_id, "accepted")
    for key, value in {
        "SYNCMILL_ROOMS__ENABLED": "true",
        "SYNCMILL_PROJECT__ID": "promote-e2e",
        "SYNCMILL_RUNS__ROOT": str(tmp_path / "runs"),
        "SYNCMILL_WORKSPACE__ROOT": str(tmp_path / "ws"),
    }.items():
        monkeypatch.setenv(key, value)
    yield room_id
    seed.close()


def test_room_promote_needs_the_board(judged_room, git_repo, monkeypatch):
    monkeypatch.delenv("SYNCMILL_BOARD__ENABLED", raising=False)
    res = CliRunner().invoke(
        cli, ["room", "promote", judged_room, "--lesson", "x", "--repo", str(git_repo)]
    )
    assert res.exit_code != 0 and "SYNCMILL_BOARD__ENABLED=true" in res.output


def test_room_promote_end_to_end(judged_room, git_repo, monkeypatch, tmp_path):
    db = tmp_path / "board.db"
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(db))
    note = tmp_path / "lesson.txt"
    note.write_bytes(b"keep migrations reversible\n")
    args = ["room", "promote", judged_room, "--lesson-file", str(note), "--repo", str(git_repo)]
    res = CliRunner().invoke(cli, args)
    assert res.exit_code == 0, res.output
    assert "Promoted lesson" in res.output and "project promote-e2e" in res.output
    assert f"lesson from room {judged_room}" in res.output
    again = CliRunner().invoke(cli, args)
    assert again.exit_code == 0 and "Already promoted" in again.output
    board = Board(str(db))
    try:
        [lesson] = board.project_lessons("promote-e2e")
        assert lesson.text == "keep migrations reversible"
    finally:
        board.close()
    listed = CliRunner().invoke(cli, ["board", "decisions"])
    assert listed.exit_code == 0, listed.output
    assert "[project promote-e2e] keep migrations reversible" in listed.output


@pytest.mark.parametrize(
    "extra", [[], ["--lesson", "a", "--lesson-file", "f"]]
)
def test_room_promote_needs_exactly_one_lesson_source(extra):
    res = CliRunner().invoke(cli, ["room", "promote", "r", *extra])
    assert res.exit_code == 2


def test_a_run_from_a_subdirectory_finds_its_projects_lessons(
    monkeypatch, git_repo, tmp_path
):
    from syncmill.project import project_id

    monkeypatch.delenv("SYNCMILL_PROJECT__ID", raising=False)
    db = tmp_path / "board.db"
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(db))
    board = Board(str(db))
    try:
        _promote(board, "repo lesson", project=project_id(git_repo.resolve()))
    finally:
        board.close()
    sub = git_repo / "pkg"
    sub.mkdir()
    monkeypatch.chdir(sub)
    holder = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = CliRunner().invoke(cli, ["run", "do it"])
    assert res.exit_code == 0, res.output
    assert holder["run"]["lessons"] == ["repo lesson"]
