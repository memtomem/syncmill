"""Approved project lessons in Room rounds (#174 follow-up): where they sit in
a member's prompt, how the prompt budget makes room for them, the v2
conversation record, what compare shows, the detached worker hand-off and the
CLI preview."""

from __future__ import annotations

import json
import os
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import syncmill.cli as cli_module
import syncmill.rooms as rooms_module
from conftest import ready_room_worker
from syncmill.board import Board, render_lessons_section
from syncmill.cli import cli
from syncmill.config import SyncmillConfig
from syncmill.room_compare import (
    comparison_to_dict,
    conversation_label,
    round_conversation_label,
)
from syncmill.rooms import (
    RoomCoordinator,
    RoomError,
    RoundLesson,
    launch_room_worker,
    round_conversation_status,
    round_lessons,
    round_lessons_fit,
)
from test_round_conversation import (
    _MEMBERS,
    _ROWS,
    _VALID,
    _frozen_room,
    _full_record,
    _patcher,
    _record,
)
from test_rooms import SessionAgent, WritingAgent, _config

L1 = RoundLesson("11111111-1111-4111-8111-111111111111", "prefer small diffs")
L2 = RoundLesson("22222222-2222-4222-8222-222222222222", "never edit vendored code")
TEXTS = [L1.text, L2.text]
BLOCK = render_lessons_section(TEXTS)
TAIL = f"--- APPROVED PROJECT LESSONS ---\n{BLOCK}\n\n--- CURRENT USER TASK ---\nnext"


@pytest.fixture
def frozen(git_repo, tmp_path, monkeypatch):
    clock: list[int] = []
    monkeypatch.setattr(
        rooms_module, "_now",
        lambda: f"2026-01-01T00:00:{clock[-1] if clock else 0:02d}+00:00",
    )
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("a"), WritingAgent("b")]
    )
    room_id = _frozen_room(coordinator.store, clock)
    yield coordinator, room_id
    coordinator.close()


# ------------------------------------------------------------- composition


def test_lessons_sit_after_the_untrusted_checkpoint_next_to_the_task(frozen):
    coordinator, room_id = frozen
    prompt, included, omitted = coordinator._checkpoint(room_id, "next", lessons=TEXTS)
    plain, plain_included, _ = coordinator._checkpoint(room_id, "next")
    assert included == plain_included and not omitted
    assert prompt.startswith(rooms_module._CHECKPOINT_PREAMBLE_WITH_LESSONS)
    assert "Continue to follow only the task" not in prompt
    assert prompt.endswith("\n\n" + TAIL)
    # Every peer excerpt comes before the lessons, the task last.
    last_excerpt = prompt.rindex("| candidate ")
    assert last_excerpt < prompt.index("--- APPROVED PROJECT LESSONS ---")
    assert prompt.index("- prefer small diffs") < prompt.index("--- CURRENT USER TASK ---")
    # Without lessons the prompt keeps its old framing.
    assert plain.startswith(rooms_module._CHECKPOINT_PREAMBLE)
    assert "APPROVED PROJECT LESSONS" not in plain


def test_without_candidates_the_lessons_still_lead_the_task(frozen):
    coordinator, room_id = frozen
    prompt, included, omitted = coordinator._checkpoint(
        room_id, "next", min_round_number=9, lessons=TEXTS
    )
    assert (prompt, included, omitted) == (TAIL, [], False)
    assert coordinator._checkpoint(room_id, "next", min_round_number=9) == ("next", [], False)


def test_the_checkpoint_makes_room_for_the_lessons(frozen, monkeypatch):
    coordinator, room_id = frozen
    full, every, _ = coordinator._checkpoint(room_id, "next")
    # Room for the whole checkpoint, not for the checkpoint plus the lessons.
    cap = len(full.encode()) + 10
    monkeypatch.setattr(rooms_module, "_ROOM_PROMPT_MAX_BYTES", cap)
    prompt, included, omitted = coordinator._checkpoint(room_id, "next", lessons=TEXTS)
    assert omitted and included == every[len(every) - len(included):]
    assert len(included) < len(every)
    assert len(prompt.encode()) <= cap
    assert prompt.endswith(TAIL)


def test_multibyte_lessons_are_budgeted_in_bytes(frozen, monkeypatch):
    coordinator, room_id = frozen
    korean = ["작은 diff를 선호한다 " * 40, "벤더 코드는 고치지 않는다 " * 40]
    full, _, _ = coordinator._checkpoint(room_id, "next")
    cap = len(full.encode()) + 10
    monkeypatch.setattr(rooms_module, "_ROOM_PROMPT_MAX_BYTES", cap)
    prompt, _, omitted = coordinator._checkpoint(room_id, "next", lessons=korean)
    assert omitted
    assert len(prompt.encode()) <= cap  # a character count would overshoot
    assert prompt.endswith("--- CURRENT USER TASK ---\nnext")


@pytest.mark.parametrize("branch", ["framing", "marker", "units"])
def test_every_excerpt_fallback_keeps_the_lessons_and_the_task(frozen, monkeypatch, branch):
    coordinator, room_id = frozen
    patch = _patcher(monkeypatch, rooms_module)
    suffix_bytes = len(f"\n\n{TAIL}".encode())
    if branch == "framing":
        # Below the framing itself, still room for the lessons and the task.
        cap = len(TAIL.encode()) + 5
    elif branch == "marker":
        preamble = rooms_module._CHECKPOINT_PREAMBLE_WITH_LESSONS.encode()
        cap = len(preamble) + suffix_bytes + 1
    else:
        cap = rooms_module._ROOM_PROMPT_MAX_BYTES
    real_units = rooms_module._prompt_arg_units
    units = (
        (lambda text, name: cap + 1 if text.startswith("Room checkpoint") else real_units(text, name))
        if branch == "units"
        else real_units
    )
    with patch("_ROOM_PROMPT_MAX_BYTES", cap), patch("_prompt_arg_units", units):
        assert round_lessons_fit("next", TEXTS)
        prompt, included, omitted = coordinator._checkpoint(room_id, "next", lessons=TEXTS)
    # The excerpts are dropped together with their ids; eligible ones existed.
    assert (prompt, included, omitted) == (TAIL, [], True)


def test_lessons_fit_is_measured_on_the_lessons_and_the_task():
    assert round_lessons_fit("anything", [])
    assert round_lessons_fit("next", TEXTS)
    room = rooms_module._ROOM_PROMPT_MAX_BYTES - len(TAIL.encode()) + len(b"next")
    assert round_lessons_fit("x" * room, TEXTS)
    assert not round_lessons_fit("x" * (room + 1), TEXTS)


@pytest.mark.parametrize(
    "value, message",
    [
        ("not a list", "must be a list"),
        ([{"id": L1.id}], "an id and a text"),
        ([{"id": "L1", "text": "x"}], "canonical uuid"),
        ([{"id": "ABCDEF01-1111-4111-8111-111111111111", "text": "x"}], "canonical uuid"),
        ([L1, L1], "listed twice"),
        ([{"id": L1.id, "text": "  "}], "must not be empty"),
        ([{"id": L1.id, "text": 7}], "must not be empty"),
    ],
)
def test_round_lessons_are_validated(value, message):
    with pytest.raises(RoomError, match=message):
        round_lessons(value)


def test_round_lessons_accept_both_shapes():
    assert round_lessons(None) == ()
    assert round_lessons([L1, {"id": L2.id, "text": L2.text}]) == (L1, L2)


# ------------------------------------------------------------- coordinator


def _prompts(agent):
    return [scope["prompt"] for scope in agent.seen_scopes]


def test_a_round_sends_its_lessons_to_every_member_and_records_them(git_repo, tmp_path):
    agents = [SessionAgent("claude"), SessionAgent("codex")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude", "codex"])
        assert not coordinator.resumes_with_lesson_history(room["id"])
        coordinator.run_round(room["id"], "one", lessons=[L1, L2])
        for agent in agents:
            assert _prompts(agent)[-1] == (
                f"--- APPROVED PROJECT LESSONS ---\n{BLOCK}\n\n--- CURRENT USER TASK ---\none"
            )
        _record(coordinator, room["id"], 1, lessons=[L1.id, L2.id])
        # The stored round prompt keeps the user's text.
        [first] = coordinator.store.list_rounds(room["id"])
        assert first["prompt"] == "one"

        # Both members resume now: their conversations may hold L1 and L2.
        assert coordinator.resumes_with_lesson_history(room["id"])
        assert not coordinator.resumes_with_lesson_history(room["id"], fresh_session=True)
        assert not coordinator.resumes_with_lesson_history(room["id"], lineage="fresh")

        # A resuming member still gets this round's lessons.
        coordinator.run_round(room["id"], "two", lessons=[L2])
        for agent in agents:
            prompt = _prompts(agent)[-1]
            assert prompt.endswith(
                f"--- APPROVED PROJECT LESSONS ---\n{render_lessons_section([L2.text])}"
                "\n\n--- CURRENT USER TASK ---\ntwo"
            )
            assert "prefer small diffs" not in prompt
        _record(coordinator, room["id"], 2, lessons=[L2.id])

        # No lessons this round: nothing is sent, the record says none.
        coordinator.run_round(room["id"], "three")
        assert all("APPROVED PROJECT LESSONS" not in _prompts(a)[-1] for a in agents)
        _record(coordinator, room["id"], 3)
    finally:
        coordinator.close()


def test_history_needs_a_stored_session(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("codex")])
    try:
        room = coordinator.create("task", ["codex"])
        coordinator.run_round(room["id"], "one", lessons=[L1])
        assert not coordinator.resumes_with_lesson_history(room["id"])  # cold next time
    finally:
        coordinator.close()


def test_history_is_off_when_conversations_are(git_repo, tmp_path):
    config = _config(tmp_path)
    coordinator = RoomCoordinator(config, git_repo, agents=[SessionAgent("claude")])
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "one", lessons=[L1])
        assert coordinator.resumes_with_lesson_history(room["id"])
        config.conversations.enabled = False
        assert not coordinator.resumes_with_lesson_history(room["id"])
    finally:
        coordinator.close()


def test_lessons_the_task_leaves_no_room_for_are_dropped_whole(git_repo, tmp_path, monkeypatch):
    agent = SessionAgent("claude")
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[agent])
    try:
        room = coordinator.create("task", ["claude"])
        prompt = "y" * 200
        monkeypatch.setattr(rooms_module, "_ROOM_PROMPT_MAX_BYTES", 300)
        assert not round_lessons_fit(prompt, TEXTS)
        coordinator.run_round(room["id"], prompt, lessons=[L1, L2])
        assert _prompts(agent)[-1] == prompt
        _record(coordinator, room["id"], 1, lessons=[L1.id, L2.id], delivery="dropped")
    finally:
        coordinator.close()


def test_malformed_lessons_are_refused_before_the_round_starts(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("codex")])
    try:
        room = coordinator.create("task", ["codex"])
        with pytest.raises(RoomError, match="canonical uuid"):
            coordinator.run_round(room["id"], "one", lessons=[{"id": "x", "text": "y"}])
        after = coordinator.store.get_room(room["id"])
        assert after["round_count"] == 0 and after["state"] == "draft"
        assert coordinator.store.list_rounds(room["id"]) == []
    finally:
        coordinator.close()


def test_memory_gives_way_before_the_lessons(git_repo, tmp_path, monkeypatch):
    agent = SessionAgent("claude")
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[agent])
    try:
        room = coordinator.create("task", ["claude"])
        monkeypatch.setattr(coordinator, "_memory_context", lambda room_id, prompt: "m" * 400)
        cap = len(f"--- APPROVED PROJECT LESSONS ---\n{BLOCK}\n\n--- CURRENT USER TASK ---\none".encode()) + 50
        monkeypatch.setattr(rooms_module, "_ROOM_PROMPT_MAX_BYTES", cap)
        coordinator.run_round(room["id"], "one", lessons=[L1, L2])
        sent = _prompts(agent)[-1]
        assert "Context from previous work" not in sent and sent.endswith("one")
        assert "prefer small diffs" in sent
        record = _full_record(coordinator, room["id"], 1)
        assert record["members"]["claude"]["memory"] == "dropped"
        assert record["members"]["claude"]["lessons"] == "included"
    finally:
        coordinator.close()


# --------------------------------------------------------------- validator


def _v2(members=None, selected=(L1.id,), **top):
    record = {
        "schema_version": 2,
        "members": members or {
            m: {**entry, "lessons": "included"} for m, entry in _VALID.items()
        },
        "lessons": {"selected": list(selected)},
    }
    record.update(top)
    return record


def _status(record):
    return round_conversation_status(
        json.dumps(record), round_number=3, members=_MEMBERS, candidates=_ROWS
    )


def test_a_v2_record_is_recorded():
    assert _status(_v2()).status == "recorded"
    none = {m: {**e, "lessons": "none"} for m, e in _VALID.items()}
    assert _status(_v2(none, selected=())).status == "recorded"
    mixed = {"a": {**_VALID["a"], "lessons": "included"}, "b": {**_VALID["b"], "lessons": "dropped"}}
    assert _status(_v2(mixed, selected=(L1.id, L2.id))).status == "recorded"
    # A v1 record stays readable.
    assert _status({"schema_version": 1, "members": _VALID}).status == "recorded"


def _members(**deliveries):
    return {m: {**e, "lessons": deliveries.get(m, "included")} for m, e in _VALID.items()}


@pytest.mark.parametrize(
    "record, reason",
    [
        (_v2(selected=("not-a-uuid",)), "canonical uuids"),
        (_v2(selected=(L1.id, L1.id)), "twice"),
        (_v2(_members(a="none")), "do not match"),
        (_v2(_members(a="none", b="none")), "do not match"),
        (_v2(selected=()), "do not match"),
        (_v2(_members(a="sent")), "are unknown"),
        (_v2(lessons={"selected": [L1.id], "extra": 1}), "lessons keys"),
        (_v2(lessons=[L1.id]), "lessons keys"),
        ({"schema_version": 2, "members": _members()}, "v2 set"),
        ({"schema_version": 2, "members": _VALID, "lessons": {"selected": []}}, "v2 set"),
        ({"schema_version": 1, "members": _members()}, "v1 set"),
        ({"schema_version": 1, "members": _VALID, "lessons": {"selected": []}}, "v1 set"),
        ({"schema_version": 3, "members": _VALID}, "unsupported"),
        ({"members": _VALID}, "no schema_version"),
    ],
)
def test_malformed_v2_records(record, reason):
    check = _status(record)
    assert check.status == "malformed" and reason in check.reason, check.reason


# ----------------------------------------------------------------- compare


def test_compare_shows_the_selected_lessons_and_each_delivery(git_repo, tmp_path):
    agents = [SessionAgent("claude"), SessionAgent("codex")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude", "codex"])
        coordinator.run_round(room["id"], "one", lessons=[L1])
        coordinator.run_round(room["id"], "two")
        from syncmill.room_compare import build_room_comparison

        cmp = build_room_comparison(coordinator.store, room["id"], all_rounds=True)
        data = comparison_to_dict(cmp)
        first, second = data["rounds"]
        assert first["conversation"]["lessons"] == [L1.id]
        assert {c["conversation"]["lessons"] for c in first["candidates"]} == {"included"}
        assert second["conversation"]["lessons"] == []
        assert {c["conversation"]["lessons"] for c in second["candidates"]} == {"none"}

        g1, g2 = cmp.groups
        assert f"approved lessons: 1 selected ({L1.id}); included 2" in round_conversation_label(g1)
        assert "approved lessons: none selected" in round_conversation_label(g2)
        assert conversation_label(g1, g1.rows[0]).endswith("; approved lessons included")
        assert conversation_label(g2, g2.rows[0]).endswith("; no approved lessons")
    finally:
        coordinator.close()


# ------------------------------------------------------------ worker hand-off


def test_worker_launch_hands_the_lessons_over_a_second_anonymous_fd(monkeypatch, git_repo):
    import tempfile

    captured: dict = {}
    reaped = threading.Event()

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        fd = int(argv[argv.index("--lessons-fd") + 1])
        captured["lessons"] = os.read(fd, 200_000)
        return SimpleNamespace(pid=4242, wait=lambda: reaped.set())

    monkeypatch.setattr(rooms_module.subprocess, "Popen", fake_popen)
    launch_room_worker(git_repo, "room-id", "the prompt", lessons=[L1, L2]).waiter.close()
    argv = captured["argv"]
    assert not any(L1.text in arg or L1.id in arg for arg in argv)
    assert argv[-2:] == ["--", "room-id"]
    prompt_fd = int(argv[argv.index("--prompt-fd") + 1])
    lessons_fd = int(argv[argv.index("--lessons-fd") + 1])
    ready_fd = int(argv[argv.index("--ready-fd") + 1])
    assert captured["kwargs"]["pass_fds"] == (prompt_fd, lessons_fd, ready_fd)
    assert json.loads(captured["lessons"]) == [
        {"id": L1.id, "text": L1.text}, {"id": L2.id, "text": L2.text},
    ]
    assert not list(Path(tempfile.gettempdir()).glob("syncmill-room-lessons-*"))
    for fd in (prompt_fd, lessons_fd):
        with pytest.raises(OSError):
            os.fstat(fd)  # the launcher closed its copies
    assert reaped.wait(1)


def test_worker_launch_without_lessons_passes_no_lessons_fd(monkeypatch, git_repo):
    captured: dict = {}
    monkeypatch.setattr(
        rooms_module.subprocess, "Popen",
        lambda argv, **kwargs: captured.update(argv=argv) or SimpleNamespace(pid=1, wait=lambda: None),
    )
    launch_room_worker(git_repo, "room-id", "p").waiter.close()
    assert "--lessons-fd" not in captured["argv"]


def test_a_failed_lessons_stage_closes_the_prompt_fd_and_the_snapshot(
    monkeypatch, git_repo, tmp_path
):
    opened: list[int] = []
    real = rooms_module._anonymous_fd

    def stage(data, prefix):
        if prefix == "syncmill-room-lessons-":
            raise OSError("disk full")
        fd = real(data, prefix)
        opened.append(fd)
        return fd

    monkeypatch.setattr(rooms_module, "_anonymous_fd", stage)
    monkeypatch.setattr(
        rooms_module.subprocess, "Popen",
        lambda *a, **k: pytest.fail("must not spawn"),
    )
    config = _config(tmp_path)
    with pytest.raises(OSError, match="disk full"):
        launch_room_worker(git_repo, "room-id", "p", resolved_config=config, lessons=[L1])
    [fd] = opened
    with pytest.raises(OSError):
        os.fstat(fd)
    assert not list((tmp_path / "runs").rglob("worker-configs/*.json"))


def test_a_failed_spawn_closes_both_fds(monkeypatch, git_repo):
    opened: list[int] = []
    real = rooms_module._anonymous_fd

    def stage(data, prefix):
        fd = real(data, prefix)
        opened.append(fd)
        return fd

    def boom(*args, **kwargs):
        raise OSError("no fork")

    monkeypatch.setattr(rooms_module, "_anonymous_fd", stage)
    monkeypatch.setattr(rooms_module.subprocess, "Popen", boom)
    with pytest.raises(OSError, match="no fork"):
        launch_room_worker(git_repo, "room-id", "p", lessons=[L1])
    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


def _fd_with(data: bytes) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, data)
    os.close(write_fd)
    return read_fd


def _worker_coordinator(monkeypatch, tmp_path):
    captured: dict = {}

    class Coordinator:
        def run_round(self, room_id, prompt, **kwargs):
            captured.update(room_id=room_id, prompt=prompt, **kwargs)

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, SimpleNamespace(), Coordinator()),
    )
    return captured


def test_the_worker_runs_exactly_the_lessons_it_was_handed(monkeypatch, tmp_path):
    captured = _worker_coordinator(monkeypatch, tmp_path)
    payload = json.dumps([{"id": L1.id, "text": L1.text}]).encode()
    prompt_fd, lessons_fd = _fd_with(b"the prompt"), _fd_with(payload)
    res = CliRunner().invoke(
        cli,
        ["room-worker", "--prompt-fd", str(prompt_fd), "--lessons-fd", str(lessons_fd), "--", "room-1"],
    )
    assert res.exit_code == 0, res.output
    assert captured["prompt"] == "the prompt"
    assert captured["lessons"] == (L1,)
    for fd in (prompt_fd, lessons_fd):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "payload", [b"not json", b"null", b'[{"id": "x", "text": "y"}]', b'{"id": 1}', "é".encode()]
)
def test_a_malformed_lessons_payload_stops_the_worker_and_cleans_up(
    monkeypatch, tmp_path, payload
):
    captured = _worker_coordinator(monkeypatch, tmp_path)
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")
    lessons_fd = _fd_with(payload)
    res = CliRunner().invoke(
        cli,
        ["room-worker", "--lessons-fd", str(lessons_fd), "--resolved-config", str(snapshot), "--", "room-1"],
    )
    assert res.exit_code != 0
    assert "lessons payload is malformed" in res.output
    assert "room_id" not in captured
    assert not snapshot.exists()
    with pytest.raises(OSError):
        os.fstat(lessons_fd)


def test_a_bad_prompt_fd_still_closes_the_lessons_fd(monkeypatch, tmp_path):
    _worker_coordinator(monkeypatch, tmp_path)
    lessons_fd = _fd_with(b"[]")
    bad = os.open(os.devnull, os.O_RDONLY)
    os.close(bad)  # a descriptor number that is no longer open
    res = CliRunner().invoke(
        cli,
        ["room-worker", "--prompt-fd", str(bad), "--lessons-fd", str(lessons_fd), "--", "room-1"],
    )
    assert res.exit_code != 0
    with pytest.raises(OSError):
        os.fstat(lessons_fd)


# --------------------------------------------------------------------- CLI


@pytest.fixture
def lesson_board(tmp_path):
    db = tmp_path / "board.db"
    board = Board(str(db))
    lessons = []
    for key, text in (("a", "prefer small diffs"), ("b", "never edit vendored code")):
        decision, _ = board.promote_lesson(
            text, project_id="proj-room", provenance={"kind": "test"}, source_key=key
        )
        lessons.append(decision)
    board.close()
    return db, lessons


def _cli_config(db: Path | None):
    board = {"enabled": True, "db_path": str(db)} if db else {"enabled": False}
    return SyncmillConfig(project={"id": "proj-room"}, board=board, rooms={"enabled": True})


def _round_cli(monkeypatch, tmp_path, config, *, history=False, room_prompt="create prompt"):
    captured: dict = {"launched": None}

    class Coordinator:
        project_id = "proj-room"

        class store:  # noqa: N801
            @staticmethod
            def get_room(room_id):
                return {"id": room_id, "prompt": room_prompt, "members": []}

            @staticmethod
            def list_candidates(room_id):
                return []

            @staticmethod
            def record_event(room_id, kind, payload):
                pass

        def preflight_round(self, room_id, prompt, **kwargs):
            return {}

        def resumes_with_lesson_history(self, room_id, **kwargs):
            captured["history_kwargs"] = kwargs
            return history

        def run_round(self, room_id, prompt, **kwargs):
            captured["lessons"] = kwargs.get("lessons")
            return {"state": "awaiting_decision"}

        def close(self):
            pass

    def launch(*args, **kwargs):
        captured["launched"] = kwargs.get("lessons")
        return ready_room_worker(4242)

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator", lambda repo: (tmp_path, config, Coordinator())
    )
    monkeypatch.setattr("syncmill.cli.launch_room_worker", launch)
    monkeypatch.setattr("syncmill.cli.git_root", lambda path: tmp_path)
    return captured


def test_room_round_shows_and_sends_the_projects_lessons(monkeypatch, tmp_path, lesson_board):
    db, (first, second) = lesson_board
    captured = _round_cli(monkeypatch, tmp_path, _cli_config(db))
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "next step"])
    assert res.exit_code == 0, res.output
    assert "lessons: 2 approved project lesson(s) go into every member's prompt this round" in res.output
    assert f"{first.id}  prefer small diffs" in res.output
    assert captured["lessons"] == (
        RoundLesson(first.id, first.text), RoundLesson(second.id, second.text),
    )
    assert "may still hold" not in res.output


def test_room_round_detached_hands_the_same_lessons_to_the_worker(
    monkeypatch, tmp_path, lesson_board
):
    db, (first, second) = lesson_board
    captured = _round_cli(monkeypatch, tmp_path, _cli_config(db))
    res = CliRunner().invoke(
        cli, ["room", "round", "room-1", "next", "--detach", "--skip-lesson", first.id]
    )
    assert res.exit_code == 0, res.output
    assert f"skipped: {first.id} (--skip-lesson)" in res.output
    assert captured["launched"] == (RoundLesson(second.id, second.text),)


def test_room_round_with_no_lessons_sends_none(monkeypatch, tmp_path, lesson_board):
    db, _ = lesson_board
    captured = _round_cli(monkeypatch, tmp_path, _cli_config(db))
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "next", "--no-lessons"])
    assert res.exit_code == 0, res.output
    assert "lessons: 2 approved, none used (--no-lessons)" in res.output
    assert captured["lessons"] == ()


def test_room_round_leaves_out_lessons_the_task_has_no_room_for(
    monkeypatch, tmp_path, lesson_board
):
    db, (first, second) = lesson_board
    captured = _round_cli(monkeypatch, tmp_path, _cli_config(db), room_prompt="z" * 200)
    monkeypatch.setattr(rooms_module, "_ROOM_PROMPT_MAX_BYTES", 300)
    res = CliRunner().invoke(cli, ["room", "round", "room-1"])  # the room prompt
    assert res.exit_code == 0, res.output
    assert "lessons: 0 approved project lesson(s)" in res.output
    assert f"left out: {first.id} (this round's prompt leaves no room for it)" in res.output
    assert f"left out: {second.id} (this round's prompt leaves no room for it)" in res.output
    assert captured["lessons"] == ()


@pytest.mark.parametrize("board", [True, False])
def test_room_round_notes_lessons_a_resumed_conversation_may_hold(
    monkeypatch, tmp_path, lesson_board, board
):
    db, _ = lesson_board
    config = _cli_config(db if board else None)
    captured = _round_cli(monkeypatch, tmp_path, config, history=True)
    res = CliRunner().invoke(
        cli, ["room", "round", "room-1", "next", "--no-lessons", "--lineage", "fresh"]
    )
    assert res.exit_code == 0, res.output
    assert "resumed conversations may still hold lessons sent in earlier rounds" in res.output
    assert "--fresh-session starts them without that history" in res.output
    assert captured["history_kwargs"] == {"lineage": "fresh", "fresh_session": False}


def test_room_round_with_the_board_off_says_nothing_about_lessons(monkeypatch, tmp_path):
    captured = _round_cli(monkeypatch, tmp_path, _cli_config(None))
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "next"])
    assert res.exit_code == 0, res.output
    assert "lessons" not in res.output
    assert captured["lessons"] == ()


def test_room_round_with_an_unreadable_board_goes_on_without_lessons(monkeypatch, tmp_path):
    bad = tmp_path / "not-a-dir" / "board.db"
    (tmp_path / "not-a-dir").write_text("a file, not a directory")
    captured = _round_cli(monkeypatch, tmp_path, _cli_config(bad))
    res = CliRunner().invoke(cli, ["room", "round", "room-1", "next"])
    assert res.exit_code == 0, res.output
    assert "lessons unavailable" in res.output
    assert captured["lessons"] == ()


def test_room_lessons_end_to_end_through_the_real_coordinator(
    monkeypatch, git_repo, tmp_path, lesson_board
):
    db, (first, second) = lesson_board
    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "proj-room"},
        board={"enabled": True, "db_path": str(db)},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "ws")},
        rooms={"enabled": True},
    )
    agent = SessionAgent("claude")
    coordinator = RoomCoordinator(config, git_repo, agents=[agent])
    room = coordinator.create("task", ["claude"])
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator", lambda repo: (git_repo, config, coordinator)
    )
    res = CliRunner().invoke(cli, ["room", "round", room["id"], "go", "--skip-lesson", second.id])
    assert res.exit_code == 0, res.output
    assert _prompts(agent)[-1].endswith(
        f"--- APPROVED PROJECT LESSONS ---\n{render_lessons_section([first.text])}"
        "\n\n--- CURRENT USER TASK ---\ngo"
    )
    reopened = RoomCoordinator(config, git_repo, agents=[agent])
    try:
        _record(reopened, room["id"], 1, lessons=[first.id])
    finally:
        reopened.close()


# ------------------------------------------------------------------- start


def test_start_refuses_lesson_flags_with_the_trial():
    res = CliRunner().invoke(cli, ["start", "--trial", "--no-lessons"])
    assert res.exit_code == 2 and "--trial cannot be combined" in res.output


def test_start_forwards_lesson_choices_to_the_single_agent_run(monkeypatch, git_repo):
    from test_start import _ready, _start

    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("SYNCMILL_REPO_ROOT", str(git_repo))
    seen: list[dict] = []

    def fake_run(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(cli_module.run, "callback", fake_run)
    res = _start(
        monkeypatch,
        ["--task", "t", "--yes", "--skip-lesson", L1.id, "--skip-lesson", L2.id],
        ready=_ready("codex"),
    )
    assert res.exit_code == 0, res.output
    assert seen[0]["skip_lesson"] == (L1.id, L2.id) and seen[0]["no_lessons"] is False

    seen.clear()
    res = _start(monkeypatch, ["--task", "t", "--yes", "--no-lessons"], ready=_ready("codex"))
    assert res.exit_code == 0, res.output
    assert seen[0]["no_lessons"] is True


def test_start_compare_shows_and_sends_the_lessons(monkeypatch, git_repo, tmp_path, lesson_board):
    from test_start import _ready, _start

    db, (first, second) = lesson_board
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("SYNCMILL_REPO_ROOT", str(git_repo))
    state: dict = {}

    class Coordinator:
        project_id = "proj-room"
        store = types.SimpleNamespace(
            get_room=lambda room_id: {"id": room_id, "prompt": "fix it", "members": []}
        )
        repo_root = "."

        def create(self, prompt, agent_ids):
            return {"id": "ROOM-1"}

        def resumes_with_lesson_history(self, room_id, **kwargs):
            return False

        def run_round(self, room_id, prompt, lessons=()):
            state["lessons"] = lessons
            raise RoomError("stop here")

        def close(self):
            pass

    monkeypatch.setattr(
        cli_module, "_room_coordinator",
        lambda repo: (repo, _cli_config(db), Coordinator()),
    )
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
    res = _start(
        monkeypatch, ["--task", "fix it", "--compare", "--yes", "--skip-lesson", first.id],
        ready=_ready("codex", "claude"),
    )
    assert "stop here" in res.output
    assert "go into every member's prompt this round" in res.output
    assert state["lessons"] == (RoundLesson(second.id, second.text),)
