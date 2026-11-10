"""Targeted repair: one member runs, from one of its own candidates."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from conftest import FakeMemory
from syncmill.agents import _PROVIDER_SESSIONS
from syncmill.cli import cli
from syncmill.config import SyncmillConfig
from syncmill.models import TaskResult, TaskStatus
from syncmill.room_compare import (
    build_room_comparison,
    comparison_to_dict,
    row_start_label,
    starting_tree_label,
    starts_from,
)
from syncmill.rooms import (
    RoomCoordinator,
    RoomError,
    RoundLesson,
    patch_has_gitlink,
    render_repair_task,
    round_conversation_status,
    round_expected_members,
    round_provenance_status,
)
from syncmill.worktree import capture_changes

from test_rooms import WritingAgent


LESSON_ID = "11111111-1111-4111-8111-111111111111"


def _config(tmp_path: Path, **overrides) -> SyncmillConfig:
    return SyncmillConfig(
        project={"id": "repair-tests"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True, "max_members": 3, "max_parallel": 3},
        **overrides,
    )


class Member(WritingAgent):
    """Writes ``<id>.txt`` naming its call number, and reports a new session id
    per call. ``on_call(number, workdir)`` runs before the write; ``fail_calls``
    end as a failed attempt that still leaves the write behind."""

    def __init__(self, agent_id: str, on_call=None, fail_calls=(), raise_calls=()):
        super().__init__(agent_id)
        self.scopes: list[dict] = []
        self.on_call = on_call
        self.fail_calls = set(fail_calls)
        self.raise_calls = set(raise_calls)

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        state = _PROVIDER_SESSIONS.get()
        number = len(self.scopes) + 1
        self.scopes.append(
            {"seed": dict(state["seed"]) if state else None, "prompt": prompt, "workdir": workdir}
        )
        if number in self.raise_calls:
            raise RuntimeError("the worker went away")
        if self.on_call is not None:
            self.on_call(number, workdir)
        (workdir / f"{self.agent_id}.txt").write_text(f"call {number}\n", encoding="utf-8")
        failed = number in self.fail_calls
        self._status = TaskStatus.FAILED if failed else TaskStatus.COMPLETED
        self._error = "scripted failure" if failed else None
        self._exit_code = 1 if failed else 0
        result = self.execute(prompt, workdir, timeout)
        result.provider_session_id = None if failed else f"{self.agent_id}-sid-{number}"
        return result


class Harness:
    def __init__(self, git_repo: Path, tmp_path: Path, agents: list[Member], **config):
        self.repo = git_repo
        self.agents = {agent.agent_id: agent for agent in agents}
        self.config = _config(tmp_path, **config)
        self.coordinator = RoomCoordinator(self.config, git_repo, agents=agents)
        self.store = self.coordinator.store
        self.room_id = self.coordinator.create("the room prompt", [a.agent_id for a in agents])["id"]

    def round(self, prompt: str = "a round", **kwargs) -> dict:
        return self.coordinator.run_round(self.room_id, prompt, **kwargs)

    def candidate(self, member: str, round_number: int | None = None) -> dict:
        rows = [
            row
            for row in self.store.list_candidates(self.room_id)
            if row["member_id"] == member
            and (round_number is None or row["round_number"] == round_number)
        ]
        return max(rows, key=lambda row: row["round_number"])

    def workspace(self, member: str) -> dict:
        return next(
            value
            for value in self.store.list_workspaces(self.room_id)
            if value["member_id"] == member
        )

    def worktree(self, member: str) -> Path:
        return Path(self.workspace(member)["path"])

    def live_patch(self, member: str) -> str:
        value = self.workspace(member)
        return capture_changes(Path(value["path"]), value["base_commit"]).patch

    def repair(self, candidate_id: str, prompt: str = "fix it", *, cold=False, lessons=None):
        plan = self.coordinator.build_repair_plan(candidate_id, prompt, cold=cold, lessons=lessons)
        room = self.coordinator.run_round(
            self.room_id, plan.prompt, lessons=lessons, target=plan
        )
        return plan, room

    def rounds(self) -> list[dict]:
        return self.store.list_rounds(self.room_id)

    def provenance(self, row: dict):
        room = self.store.get_room(self.room_id)
        return round_provenance_status(
            row["provenance_json"],
            round_number=int(row["number"]),
            member_ids=[m["member_id"] for m in room["members"]],
            candidates=self.store.candidate_rows(self.room_id),
        )

    def events(self, kind: str) -> list[dict]:
        return [e for e in self.store.events(self.room_id) if e["kind"] == kind]

    def close(self) -> None:
        self.coordinator.close()


@pytest.fixture
def make(git_repo, tmp_path):
    made: list[Harness] = []

    def build(*agents: Member, **config) -> Harness:
        harness = Harness(git_repo, tmp_path, list(agents), **config)
        made.append(harness)
        return harness

    yield build
    for harness in made:
        harness.close()


def _both():
    return Member("claude"), Member("codex")


# ------------------------------------------------------------------ one member


def test_a_repair_runs_only_its_member_and_leaves_its_peer_untouched(make):
    claude, codex = _both()
    room = make(claude, codex)
    room.round()
    peer = room.candidate("codex")
    peer_dir = Path(peer["artifact_path"])
    peer_bytes = {p.name: p.read_bytes() for p in peer_dir.iterdir() if p.is_file()}
    peer_status = room.live_patch("codex")
    parent = room.candidate("claude")

    plan, state = room.repair(parent["id"], "make it better")

    assert state["state"] == "awaiting_decision"
    assert len(claude.scopes) == 2
    assert len(codex.scopes) == 1
    new = room.candidate("claude")
    assert new["id"] != parent["id"] and new["round_number"] == 2
    assert [c["round_number"] for c in room.store.list_candidates(room.room_id) if c["member_id"] == "codex"] == [1]
    assert {p.name: p.read_bytes() for p in peer_dir.iterdir() if p.is_file()} == peer_bytes
    assert room.live_patch("codex") == peer_status
    assert room.workspace("codex")["provider_session_id"] == "codex-sid-1"
    assert room.workspace("claude")["provider_session_id"] == "claude-sid-2"
    # The original candidate stays as it was, and selectable by its own row.
    assert room.store.get_candidate(parent["id"])["patch_sha256"] == parent["patch_sha256"]


def test_a_repair_records_its_target_and_parent(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    plan, _ = room.repair(parent["id"], "make it better")

    row = room.rounds()[-1]
    assert row["prompt"] == "make it better"
    check = room.provenance(row)
    assert check.status == "recorded", check.reason
    assert check.record["schema_version"] == 3
    assert check.record["requested_lineage"] == "repair"
    assert check.record["tree"] == {
        "kind": "repair",
        "member_id": "claude",
        "parent_candidate_id": parent["id"],
        "parent_patch_sha256": parent["patch_sha256"],
        "parent_round_number": 1,
        "feedback_ids": [],
        "agent_task_sha256": plan.agent_task_sha256,
    }
    room_row = room.store.get_room(room.room_id)
    conversation = round_conversation_status(
        row["conversation_json"],
        round_number=2,
        members=round_expected_members(room_row["members"], check),
        candidates=room.store.candidate_rows(room.room_id),
    )
    assert conversation.status == "recorded", conversation.reason
    assert set(conversation.record["members"]) == {"claude"}
    assert [m["member_id"] for m in room.store.list_round_members(row["id"])] == ["claude"]


def test_a_repair_hands_its_member_the_task_and_resumes_its_session_alone(make):
    claude, codex = _both()
    room = make(claude, codex)
    room.round()
    parent = room.candidate("claude")
    room.repair(parent["id"], "make it better")

    scope = claude.scopes[-1]
    assert scope["seed"] == {"claude": "claude-sid-1"}
    assert render_repair_task(parent["id"], "make it better") in scope["prompt"]
    # Resuming: its own output is in the conversation, its peer's is new.
    assert "result from codex" in scope["prompt"]


def test_a_cold_repair_clears_only_its_members_session(make):
    claude, codex = _both()
    room = make(claude, codex)
    room.round()
    plan, _ = room.repair(room.candidate("claude")["id"], cold=True)

    assert plan.session_mode == "cold"
    assert claude.scopes[-1]["seed"] == {}
    assert room.workspace("codex")["provider_session_id"] == "codex-sid-1"
    assert room.store.member_sessions(room.room_id)["codex"] == "codex-sid-1"
    assert room.events("sessions.cleared") == []


def test_with_conversations_off_only_cold_drops_the_stored_session(make):
    claude = Member("claude", fail_calls={2, 3})
    room = make(claude)
    room.round()
    room.coordinator.config.conversations.enabled = False
    parent = room.candidate("claude")["id"]

    without = room.coordinator.build_repair_plan(parent, "fix it")
    with_cold = room.coordinator.build_repair_plan(parent, "fix it", cold=True)
    assert without.session_mode == with_cold.session_mode == "cold"
    assert without.session_sha256 == with_cold.session_sha256
    assert without.token != with_cold.token

    # Failed attempts report no session, so what remains is what the run left.
    room.coordinator.run_round(room.room_id, "fix it", target=without)
    assert room.store.member_sessions(room.room_id) == {"claude": "claude-sid-1"}
    with_cold = room.coordinator.build_repair_plan(parent, "fix it", cold=True)
    room.coordinator.run_round(room.room_id, "fix it", target=with_cold)
    assert room.store.member_sessions(room.room_id) == {}


# --------------------------------------------------------------- starting tree


def test_a_tree_that_matches_its_parent_is_kept_with_its_ignored_files(make):
    def write_ignored(number, workdir):
        if number == 1:
            (workdir / ".gitignore").write_text(".venv/\n", encoding="utf-8")
            (workdir / ".venv").mkdir()
            (workdir / ".venv" / "dep").write_text("dep\n", encoding="utf-8")

    seen = {}

    def observe(number, workdir):
        write_ignored(number, workdir)
        if number == 2:
            seen["dep"] = (workdir / ".venv" / "dep").exists()

    room = make(Member("claude", on_call=observe))
    room.round()
    room.repair(room.candidate("claude")["id"])

    assert seen["dep"] is True
    assert room.events("workspace.rebuilt") == []


def test_a_failed_attempts_changes_are_gone_when_the_repair_is_retried(make):
    seen = {}
    room_box = {}

    def observe(number, workdir):
        if number == 2:
            (workdir / "partial.txt").write_text("half done\n", encoding="utf-8")
        if number == 3:
            seen["partial"] = (workdir / "partial.txt").exists()
            seen["patch"] = capture_changes(
                workdir, room_box["room"].workspace("claude")["base_commit"]
            ).patch

    claude = Member("claude", on_call=observe, fail_calls={2})
    room = make(claude)
    room_box["room"] = room
    room.round()
    parent = room.candidate("claude")
    parent_patch = room.store.load_candidate(parent["id"])["patch"]

    _, state = room.repair(parent["id"])
    assert state["state"] == "awaiting_decision"
    assert room.candidate("claude", 2)["state"] == "failed"
    assert room.candidate("claude", 2)["patch_sha256"] != parent["patch_sha256"]

    # The failed attempt did not disqualify the parent.
    room.repair(parent["id"])

    assert seen["partial"] is False
    assert seen["patch"].encode("utf-8", "surrogateescape") == parent_patch
    assert [e["payload"]["reason"] for e in room.events("workspace.rebuilt")] == [
        "the worktree differed from the repair's parent"
    ]
    assert room.candidate("claude", 3)["state"] == "completed"


def test_a_gate_that_writes_a_file_does_not_reach_the_repair(make):
    seen = {}
    room_box = {}

    def observe(number, workdir):
        if number == 2:
            seen["patch"] = capture_changes(
                workdir, room_box["room"].workspace("claude")["base_commit"]
            ).patch

    room = make(
        Member("claude", on_call=observe),
        gate={"commands": ["python3 -c \"open('gate-debris.txt','w').write('x')\""]},
    )
    room_box["room"] = room
    room.round()
    parent = room.candidate("claude")
    room.repair(parent["id"])

    assert "gate-debris" not in seen["patch"]
    assert "gate-debris" not in room.store.load_candidate(room.candidate("claude")["id"])["patch"].decode()


def test_a_repair_can_start_from_an_empty_patch(make):
    class Idle(Member):
        async def aexecute(self, prompt, workdir, timeout):
            self.scopes.append({"seed": None, "prompt": prompt, "workdir": workdir})
            result = self.execute(prompt, workdir, timeout)
            result.provider_session_id = None
            return result

    idle = Idle("claude")
    room = make(idle)
    room.round()
    parent = room.candidate("claude")
    assert not room.store.load_candidate(parent["id"])["patch"]
    room.repair(parent["id"])
    assert room.candidate("claude")["round_number"] == 2


@pytest.mark.parametrize(
    "line,expected",
    [
        (b"new file mode 160000", True),
        (b"deleted file mode 160000", True),
        (b"old mode 160000", True),
        (b"new mode 160000", True),
        (b"index abc123..def456 160000", True),
        (b"new file mode 100644", False),
        (b"index abc123..def456 100644", False),
        (b"+new file mode 160000", False),
        (b" index abc123..def456 160000", False),
    ],
)
def test_a_gitlink_is_read_from_the_patch_headers(line, expected):
    assert patch_has_gitlink(b"diff --git a/x b/x\n" + line + b"\n") is expected


def test_a_candidate_holding_an_embedded_repository_is_refused(make):
    def embed(number, workdir):
        inner = workdir / "vendored"
        inner.mkdir()
        for args in (
            ["init", "-q"],
            ["-c", "user.email=t@t.test", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "x"],
        ):
            subprocess.run(["git", *args], cwd=inner, check=True, capture_output=True)

    room = make(Member("claude", on_call=embed))
    room.round()
    parent = room.candidate("claude")
    assert patch_has_gitlink(room.store.load_candidate(parent["id"])["patch"])
    before = room.store.get_room(room.room_id)["revision"]

    with pytest.raises(RoomError, match="embedded repository"):
        room.coordinator.build_repair_plan(parent["id"], "fix it")
    assert room.store.get_room(room.room_id)["revision"] == before


def test_a_damaged_peer_workspace_does_not_block_a_repair(make):
    room = make(*_both())
    room.round()
    shutil.rmtree(room.worktree("codex"))

    room.repair(room.candidate("claude")["id"])
    assert room.candidate("claude")["round_number"] == 2

    with pytest.raises(RoomError, match="missing or invalid"):
        room.round()


# ------------------------------------------------------------------- refusals


def _no_change(room: Harness, revision: int, rounds: int) -> None:
    assert room.store.get_room(room.room_id)["revision"] == revision
    assert len(room.rounds()) == rounds


@pytest.mark.parametrize("prompt", ["", "   \n"])
def test_a_repair_with_nothing_to_do_is_refused(make, prompt):
    room = make(Member("claude"))
    room.round()
    with pytest.raises(RoomError, match="nothing to repair"):
        room.coordinator.build_repair_plan(room.candidate("claude")["id"], prompt)


def test_a_prompt_that_only_overflows_as_a_repair_is_refused_at_the_preview(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    state = room.store.get_room(room.room_id)
    from syncmill.rooms import _ROOM_PROMPT_MAX_BYTES

    # Fits as a prompt on its own, not with the repair's opening lines.
    prompt = "x" * (_ROOM_PROMPT_MAX_BYTES - 10)
    room.coordinator.preflight_round(room.room_id, prompt)
    with pytest.raises(RoomError, match="exceeds"):
        room.coordinator.build_repair_plan(parent["id"], prompt)
    _no_change(room, state["revision"], 1)
    assert room.store.get_room(room.room_id)["state"] == "awaiting_decision"


def test_lessons_that_do_not_fit_are_refused_not_dropped(make):
    room = make(Member("claude"))
    room.round()
    from syncmill.rooms import _ROOM_PROMPT_MAX_BYTES

    lesson = RoundLesson(LESSON_ID, "x" * _ROOM_PROMPT_MAX_BYTES)
    with pytest.raises(RoomError, match="lessons do not fit"):
        room.coordinator.build_repair_plan(
            room.candidate("claude")["id"], "fix it", lessons=[lesson]
        )


def test_a_failed_candidate_is_refused(make):
    room = make(Member("claude", fail_calls={1}))
    room.round()
    with pytest.raises(RoomError, match="only a completed candidate"):
        room.coordinator.build_repair_plan(room.candidate("claude")["id"], "fix it")


def test_a_candidate_with_a_newer_completed_one_is_refused(make):
    room = make(Member("claude"))
    room.round()
    older = room.candidate("claude")
    room.round("again")
    newer = room.candidate("claude")
    with pytest.raises(RoomError, match=f"{newer['id']}.*newer completed"):
        room.coordinator.build_repair_plan(older["id"], "fix it")


def test_a_selected_room_is_refused_with_what_to_do(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    room.store.select_candidate(room.room_id, parent["id"])
    with pytest.raises(RoomError, match="room close"):
        room.coordinator.build_repair_plan(parent["id"], "fix it")


def test_a_disabled_provider_is_refused_before_any_state_changes(make):
    room = make(Member("claude"))
    room.round()
    room.coordinator.agents = []
    with pytest.raises(RoomError, match="no longer enabled"):
        room.coordinator.build_repair_plan(room.candidate("claude")["id"], "fix it")


def test_a_damaged_artifact_is_refused(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    patch = Path(parent["artifact_path"]) / "candidate.patch"
    patch.write_bytes(patch.read_bytes() + b"tampered\n")
    with pytest.raises(RoomError):
        room.coordinator.build_repair_plan(parent["id"], "fix it")


def test_max_rounds_bounds_repeated_repair(make):
    room = make(Member("claude"))
    room.coordinator.config.rooms.max_rounds = 2
    room.round()
    room.repair(room.candidate("claude")["id"])
    with pytest.raises(RoomError, match="max_rounds=2"):
        room.coordinator.build_repair_plan(room.candidate("claude")["id"], "fix it")


# ----------------------------------------------------------------- the token


def _token_of(room: Harness, candidate_id: str, **kwargs) -> str:
    return room.coordinator.build_repair_plan(candidate_id, kwargs.pop("prompt", "fix it"), **kwargs).token


def test_every_bound_field_changes_the_token(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")["id"]
    base = _token_of(room, parent)
    assert base == _token_of(room, parent), "a preview is deterministic"

    tokens = {
        "prompt": _token_of(room, parent, prompt="fix it differently"),
        "cold": _token_of(room, parent, cold=True),
        "lessons": _token_of(room, parent, lessons=[RoundLesson(LESSON_ID, "a lesson")]),
    }
    room.coordinator.config.claude.timeout_seconds += 1
    tokens["agent settings"] = _token_of(room, parent)
    room.coordinator.config.claude.timeout_seconds -= 1
    room.coordinator.config.gate.commands = ["true"]
    tokens["gate"] = _token_of(room, parent)
    room.coordinator.config.gate.commands = []
    room.coordinator.config.conversations.enabled = False
    tokens["conversations"] = _token_of(room, parent)
    room.coordinator.config.conversations.enabled = True
    room.store.update_workspace_session(room.room_id, "claude", "another-session")
    tokens["session"] = _token_of(room, parent)

    assert base not in tokens.values()
    assert len(set(tokens.values())) == len(tokens)


def test_a_used_plan_is_refused_the_second_time(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    plan = room.coordinator.build_repair_plan(parent["id"], "fix it")
    room.coordinator.run_round(room.room_id, plan.prompt, target=plan)
    rounds = len(room.rounds())

    with pytest.raises(RoomError, match="changed since this repair was confirmed"):
        room.coordinator.run_round(room.room_id, plan.prompt, target=plan)
    assert len(room.rounds()) == rounds


def test_a_session_change_between_confirm_and_claim_starts_nothing(make):
    claude = Member("claude")
    room = make(claude)
    room.round()
    plan = room.coordinator.build_repair_plan(room.candidate("claude")["id"], "fix it")
    room.store.update_workspace_session(room.room_id, "claude", "another-session")
    state = room.store.get_room(room.room_id)

    with pytest.raises(RoomError, match="provider session changed"):
        room.coordinator.run_round(room.room_id, plan.prompt, target=plan)

    _no_change(room, state["revision"], 1)
    assert len(claude.scopes) == 1
    assert room.store.get_room(room.room_id)["state"] == "awaiting_decision"


def test_a_plan_cannot_be_mixed_with_other_round_options(make):
    room = make(Member("claude"))
    room.round()
    plan = room.coordinator.build_repair_plan(room.candidate("claude")["id"], "fix it")
    for kwargs in (
        {"lineage": "fresh"},
        {"fresh_session": True},
        {"fork": True},
        {"base_candidate_id": room.candidate("claude")["id"]},
    ):
        with pytest.raises(RoomError, match="from its plan"):
            room.coordinator.run_round(room.room_id, plan.prompt, target=plan, **kwargs)
    with pytest.raises(RoomError, match="from its plan"):
        room.coordinator.run_round(room.room_id, "another prompt", target=plan)
    with pytest.raises(RoomError, match="lessons differ"):
        room.coordinator.run_round(
            room.room_id, plan.prompt, target=plan, lessons=[RoundLesson(LESSON_ID, "x")]
        )


# ------------------------------------------------------------- what comes next


def test_the_next_all_member_round_keeps_writing_v2_and_peers_see_the_repair(make):
    claude, codex = _both()
    room = make(claude, codex)
    room.round()
    room.repair(room.candidate("claude")["id"], "make it better")
    repaired = room.candidate("claude")

    room.round("everyone again")

    row = room.rounds()[-1]
    check = room.provenance(row)
    assert check.status == "recorded", check.reason
    assert check.record["schema_version"] == 2
    assert check.record["tree"]["members"] == {
        "claude": {"parent_candidate_id": repaired["id"], "parent_round_number": 2},
        "codex": None,
    }
    assert "result from claude" in codex.scopes[-1]["prompt"]
    assert set(codex.scopes[-1]["seed"]) == {"claude", "codex"}
    room_row = room.store.get_room(room.room_id)
    conversation = round_conversation_status(
        row["conversation_json"],
        round_number=3,
        members=round_expected_members(room_row["members"], check),
        candidates=room.store.candidate_rows(room.room_id),
    )
    assert conversation.status == "recorded", conversation.reason


def test_a_repair_candidate_can_itself_be_selected_and_repaired(make):
    room = make(Member("claude"))
    room.round()
    room.repair(room.candidate("claude")["id"], "first fix")
    second = room.candidate("claude")
    room.repair(second["id"], "second fix")
    third = room.candidate("claude")
    assert third["round_number"] == 3
    state = room.store.select_candidate(room.room_id, third["id"])
    assert state["selected_candidate_id"] == third["id"]


def test_the_next_rounds_prompt_passes_over_a_repair(make):
    room = make(*_both())
    assert room.coordinator.next_round_prompt(room.room_id) == ("the room prompt", False)
    room.round("every member")
    assert room.coordinator.next_round_prompt(room.room_id) == ("every member", False)
    room.repair(room.candidate("claude")["id"], "fix your own file")
    assert room.coordinator.next_round_prompt(room.room_id) == ("every member", True)
    room.round("every member again")
    assert room.coordinator.next_round_prompt(room.room_id) == ("every member again", False)


def test_memory_is_searched_with_the_rooms_prompt(git_repo, tmp_path):
    class RecordingMemory(FakeMemory):
        def __init__(self):
            super().__init__()
            self.queries: list[str] = []

        async def search_context(self, query, *args, **kwargs):
            self.queries.append(query)
            return await super().search_context(query, *args, **kwargs)

    memory = RecordingMemory()
    agent = Member("claude")
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[agent], memory=memory)
    try:
        room_id = coordinator.create("the room prompt", ["claude"])["id"]
        coordinator.run_round(room_id, "a round")
        parent = coordinator.store.list_candidates(room_id)[0]
        plan = coordinator.build_repair_plan(parent["id"], "the repair prompt")
        before = len(memory.queries)
        coordinator.run_round(room_id, plan.prompt, target=plan)
        assert memory.queries[before:] == ["the room prompt"]
    finally:
        coordinator.close()


def test_a_worker_failure_during_a_repair_freezes_the_room_with_the_parent_readable(make):
    room = make(Member("claude", raise_calls={2}))
    room.round()
    parent = room.candidate("claude")
    plan = room.coordinator.build_repair_plan(parent["id"], "fix it")

    with pytest.raises(RuntimeError, match="the worker went away"):
        room.coordinator.run_round(room.room_id, plan.prompt, target=plan)

    assert room.store.get_room(room.room_id)["state"] == "recovery_required"
    assert room.store.load_candidate(parent["id"])["patch"]
    with pytest.raises(RoomError):
        room.coordinator.build_repair_plan(parent["id"], "fix it")


def test_source_drift_after_the_preview_stales_the_room_and_starts_nothing(make):
    claude = Member("claude")
    room = make(claude)
    room.round()
    plan = room.coordinator.build_repair_plan(room.candidate("claude")["id"], "fix it")
    (room.repo / "README.md").write_text("changed\n", encoding="utf-8")

    with pytest.raises(RoomError, match="create a new room"):
        room.coordinator.run_round(room.room_id, plan.prompt, target=plan)

    assert room.store.get_room(room.room_id)["state"] == "stale"
    assert len(claude.scopes) == 1


def test_a_killed_repair_waits_for_its_lease_and_then_recovers(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    plan = room.coordinator.build_repair_plan(parent["id"], "fix it")
    state = room.store.get_room(room.room_id)
    # What a killed worker leaves: the round claimed, nothing after it.
    row = room.store.start_round(
        room.room_id,
        prompt=plan.prompt,
        source_fingerprint=state["source_fingerprint"],
        snapshot_commit=state["snapshot_commit"],
        lease_owner="99999999-dead",
        lease_timeout_seconds=900,
        max_rounds=20,
        repair_guard=plan,
    )
    assert room.store.get_room(room.room_id)["state"] == "running"
    with pytest.raises(RoomError):
        room.coordinator.close_room(room.room_id)

    room.store._conn.execute("UPDATE rounds SET lease_expires=1 WHERE id=?", (row["id"],))
    room.store._conn.commit()
    assert room.room_id in room.coordinator.recover(room.room_id)
    assert room.store.get_room(room.room_id)["state"] == "recovery_required"
    assert room.store.load_candidate(parent["id"])["patch"]


def test_a_one_entry_conversation_on_an_all_member_round_stays_malformed(make):
    room = make(*_both())
    room.round()
    row = room.rounds()[-1]
    record = json.loads(row["conversation_json"])
    del record["members"]["codex"]
    room_row = room.store.get_room(room.room_id)
    check = round_conversation_status(
        json.dumps(record),
        round_number=1,
        members=round_expected_members(room_row["members"], room.provenance(row)),
        candidates=room.store.candidate_rows(room.room_id),
    )
    assert check.status == "malformed"


# ---------------------------------------------------------------- read models


def _comparison(room: Harness, number: int):
    return build_room_comparison(
        room.store, room.room_id, round_number=number, source_check=lambda recorded: True
    )


def test_compare_expects_only_the_target_in_a_repair_round(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    room.repair(parent["id"])

    group = _comparison(room, 2).groups[0]
    assert group.missing == ()
    assert group.warnings == ()
    assert group.lineage.tree == "repair"
    assert group.lineage.requested == "repair"
    assert group.conversation.status == "recorded"
    assert starts_from(group, group.rows[0]) == {
        "kind": "repair",
        "parent_candidate_id": parent["id"],
        "parent_patch_sha256": parent["patch_sha256"],
        "parent_round_number": 1,
    }
    label = starting_tree_label(group)
    assert parent["id"] in label and "for claude alone" in label
    assert "requested lineage: repair" in label
    assert row_start_label(group, group.rows[0]) == f"a repair of candidate {parent['id']} (round 1)"
    document = comparison_to_dict(_comparison(room, 2))
    assert document["schema_version"] == 1
    assert document["rounds"][0]["lineage"]["members"] == {
        "claude": {"parent_candidate_id": parent["id"], "parent_round_number": 1}
    }
    assert "fix it" not in json.dumps(document)


def test_a_damaged_repair_record_keeps_every_member_expected(make):
    room = make(*_both())
    room.round()
    room.repair(room.candidate("claude")["id"])
    row = room.rounds()[-1]
    record = json.loads(row["provenance_json"])
    record["tree"]["member_id"] = "someone-else"
    room.store._conn.execute(
        "UPDATE rounds SET provenance_json=? WHERE id=?", (json.dumps(record), row["id"])
    )
    room.store._conn.commit()

    group = _comparison(room, 2).groups[0]
    assert [m.member_id for m in group.missing] == ["codex"]
    assert group.lineage.status == "malformed"


def test_a_repair_round_holding_another_members_row_warns(make):
    room = make(*_both())
    room.round()
    room.repair(room.candidate("claude")["id"])
    row = room.rounds()[-1]
    room.store.init_round_members(
        room.room_id, row["id"], [{"member_id": "codex", "provider_id": "codex"}]
    )
    group = _comparison(room, 2).groups[0]
    assert any("also holds records for codex" in warning for warning in group.warnings)


def test_a_repair_in_progress_expects_only_its_member(make):
    seen = {}
    room_box = {}

    def observe(number, workdir):
        if number == 2:
            seen["group"] = _comparison(room_box["room"], 2).groups[0]

    room = make(Member("claude", on_call=observe), Member("codex"))
    room_box["room"] = room
    room.round()
    room.repair(room.candidate("claude")["id"])

    group = seen["group"]
    assert [(m.member_id, m.status) for m in group.missing] == [("claude", "in_progress")]


def test_lesson_history_is_asked_about_the_target_alone(make):
    claude, codex = _both()
    room = make(claude, codex)
    room.round(lessons=[RoundLesson(LESSON_ID, "a lesson")])
    assert room.coordinator.resumes_with_lesson_history(room.room_id, member_id="claude")
    room.store.clear_member_session(room.room_id, "claude")
    assert not room.coordinator.resumes_with_lesson_history(room.room_id, member_id="claude")
    assert room.coordinator.resumes_with_lesson_history(room.room_id, member_id="codex")


# ------------------------------------------------------------------------ CLI


@pytest.fixture
def cli_room(monkeypatch, git_repo, tmp_path):
    claude, codex = _both()
    config = _config(tmp_path)
    agents = [claude, codex]

    def build() -> RoomCoordinator:
        return RoomCoordinator(config, git_repo, agents=agents)

    monkeypatch.setattr("syncmill.cli._room_coordinator", lambda repo: (git_repo, config, build()))
    coordinator = build()
    room_id = coordinator.create("the room prompt", ["claude", "codex"])["id"]
    coordinator.run_round(room_id, "a round")
    parent = next(
        c for c in coordinator.store.list_candidates(room_id) if c["member_id"] == "claude"
    )
    yield SimpleNamespace(
        coordinator=coordinator, room_id=room_id, parent=parent, claude=claude, codex=codex, config=config
    )
    coordinator.close()


def _invoke(*args: str):
    return CliRunner().invoke(cli, ["room", "repair", *args])


def _token(output: str) -> str:
    return next(line.split()[1] for line in output.splitlines() if line.startswith("Token:"))


@pytest.mark.parametrize(
    "flags", [[], ["--preview", "--confirm", "abc"]], ids=["neither", "both"]
)
def test_the_cli_needs_exactly_one_of_preview_and_confirm(cli_room, flags):
    res = _invoke(cli_room.parent["id"], "--prompt", "fix it", *flags)
    assert res.exit_code == 2
    assert "exactly one of --preview and --confirm" in res.output
    assert len(cli_room.claude.scopes) == 1


def test_the_preview_starts_nothing_and_says_what_would_run(cli_room):
    state = cli_room.coordinator.store.get_room(cli_room.room_id)
    res = _invoke(cli_room.parent["id"], "--prompt", "fix it", "--preview")
    assert res.exit_code == 0, res.output
    for expected in (
        "claude alone, 1 provider call",
        "resumes the member's stored conversation",
        "none; the result will not be verified",
        "ignored files",
        "--confirm TOKEN",
    ):
        assert expected in res.output
    assert len(cli_room.claude.scopes) == 1
    after = cli_room.coordinator.store.get_room(cli_room.room_id)
    assert after["revision"] == state["revision"]
    assert after["round_count"] == state["round_count"]


def test_the_preview_says_when_the_provider_starts_cold(cli_room):
    res = _invoke(cli_room.parent["id"], "--prompt", "fix it", "--preview", "--cold")
    assert "starts without the earlier conversation" in res.output


def test_confirm_runs_exactly_what_the_preview_showed(cli_room):
    token = _token(_invoke(cli_room.parent["id"], "--prompt", "fix it", "--preview").output)
    res = _invoke(cli_room.parent["id"], "--prompt", "fix it", "--confirm", token)
    assert res.exit_code == 0, res.output
    assert len(cli_room.claude.scopes) == 2
    assert len(cli_room.codex.scopes) == 1
    assert f"room compare {cli_room.room_id} --round 2" in res.output

    again = _invoke(cli_room.parent["id"], "--prompt", "fix it", "--confirm", token)
    assert again.exit_code == 1
    assert len(cli_room.claude.scopes) == 2


@pytest.mark.parametrize(
    "other",
    [["--prompt", "another prompt"], ["--prompt", "fix it", "--cold"]],
    ids=["prompt", "cold"],
)
def test_confirm_refuses_options_the_preview_did_not_have(cli_room, other):
    token = _token(_invoke(cli_room.parent["id"], "--prompt", "fix it", "--preview").output)
    res = _invoke(cli_room.parent["id"], *other, "--confirm", token)
    assert res.exit_code == 1
    assert "not what that token confirmed" in res.output
    assert "--preview again" in res.output
    assert len(cli_room.claude.scopes) == 1


def test_lessons_that_do_not_fit_stop_the_cli_before_a_round_exists(cli_room, monkeypatch):
    from syncmill.rooms import _ROOM_PROMPT_MAX_BYTES

    cli_room.config.board.enabled = True
    monkeypatch.setattr(
        "syncmill.cli._show_lessons",
        lambda *a, **k: [SimpleNamespace(id=LESSON_ID, text="x" * _ROOM_PROMPT_MAX_BYTES)],
    )
    res = _invoke(cli_room.parent["id"], "--prompt", "fix it", "--preview")
    assert res.exit_code == 1
    assert "lessons do not fit" in res.output
    assert len(cli_room.coordinator.store.list_rounds(cli_room.room_id)) == 1


def test_a_failed_repair_says_the_parent_can_still_be_repaired(cli_room):
    cli_room.claude.fail_calls = {2}
    token = _token(_invoke(cli_room.parent["id"], "--prompt", "fix it", "--preview").output)
    res = _invoke(cli_room.parent["id"], "--prompt", "fix it", "--confirm", token)
    assert res.exit_code == 0, res.output
    assert "is still repairable" in res.output
    assert "--cold" in res.output
