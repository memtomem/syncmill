"""Review feedback on one candidate, and repairs that send it (#173 PR 2)."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from click.testing import CliRunner

from conftest import FakeMemory
from syncmill.cli import cli
from syncmill.rooms import (
    FEEDBACK_STATUSES,
    RoomCoordinator,
    RoomError,
    render_feedback_batch,
    render_repair_task,
)

from test_room_repair import Harness, Member, _config


SENTINEL = "SENTINEL-FEEDBACK-7f3a"


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


def _status(room: Harness, feedback_id: str) -> str:
    return room.store.feedback(room.room_id).checks[feedback_id].status


def _add(room: Harness, candidate_id: str, text: str = "rename it", **kwargs) -> dict:
    return room.coordinator.add_feedback(
        candidate_id, kwargs.pop("kind", "change"), text, **kwargs
    )


def _raw(room: Harness) -> sqlite3.Connection:
    return room.store._conn


# ------------------------------------------------------------------ writing


def test_a_request_is_recorded_without_touching_the_room(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    before = room.store.get_room(room.room_id)

    record = _add(room, parent["id"], f"rename it {SENTINEL}", path="claude.txt")

    assert record["candidate_id"] == parent["id"]
    assert record["patch_sha256"] == parent["patch_sha256"]
    assert record["anchor"] == {"path": "claude.txt"}
    assert record["carried_from"] is None
    after = room.store.get_room(room.room_id)
    assert (after["state"], after["revision"]) == (before["state"], before["revision"])
    assert _status(room, record["feedback_id"]) == "open"
    [event] = room.events("feedback.requested")
    assert event["payload"]["feedback_id"] == record["feedback_id"]
    assert event["payload"]["anchored"] is True


def test_feedback_text_never_enters_the_event_log(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"], SENTINEL)
    room.repair(parent["id"], None)
    new = room.candidate("claude")
    room.coordinator.resolve_feedback(request["feedback_id"], "not_addressed", on=new["id"])
    room.coordinator.carry_feedback(request["feedback_id"], new["id"])
    rows = _raw(room).execute("SELECT payload_json FROM room_events").fetchall()
    assert rows and all(SENTINEL not in row[0] for row in rows)


@pytest.mark.parametrize(
    "path, match",
    [("codex.txt", "not a file this candidate changed"), ("./claude.txt", "not a file")],
)
def test_an_anchor_must_name_a_file_the_candidate_changed(make, path, match):
    room = make(*_both())
    room.round()
    with pytest.raises(RoomError, match=match):
        _add(room, room.candidate("claude")["id"], path=path)
    assert room.store.feedback_count(room.room_id) == 0


@pytest.mark.parametrize(
    "text, match",
    [
        ("", "must not be empty"),
        ("  \n ", "must not be empty"),
        ("bad\x1bescape", "control characters"),
        ("x" * (8 * 1024 + 1), "the limit is 8192"),
    ],
)
def test_feedback_text_is_checked(make, text, match):
    room = make(Member("claude"))
    room.round()
    with pytest.raises(RoomError, match=match):
        _add(room, room.candidate("claude")["id"], text)


def test_a_request_needs_a_completed_candidate(make):
    room = make(Member("claude", fail_calls={1}))
    room.round()
    with pytest.raises(RoomError, match="ended failed"):
        _add(room, room.candidate("claude")["id"])


def test_feedback_is_writable_after_a_selection_but_not_after_close(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    room.coordinator.select(room.room_id, parent["id"])
    revision = room.store.get_room(room.room_id)["revision"]
    _add(room, parent["id"])
    assert room.store.get_room(room.room_id)["revision"] == revision
    room.coordinator.close_room(room.room_id)
    with pytest.raises(RoomError, match="room is closed"):
        _add(room, parent["id"])


# ---------------------------------------------------------------- resolving


def test_resolution_binds_to_a_later_completed_candidate_of_the_same_member(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    peer = room.candidate("codex")
    request = _add(room, parent["id"])["feedback_id"]

    with pytest.raises(RoomError, match="--on"):
        room.coordinator.resolve_feedback(request, "addressed")
    with pytest.raises(RoomError, match="drop --on"):
        room.coordinator.resolve_feedback(request, "withdrawn", on=parent["id"])
    with pytest.raises(RoomError, match="another member's"):
        room.coordinator.resolve_feedback(request, "addressed", on=peer["id"])
    with pytest.raises(RoomError, match="later round"):
        room.coordinator.resolve_feedback(request, "addressed", on=parent["id"])

    room.round()
    later = room.candidate("claude")
    record, created = room.coordinator.resolve_feedback(request, "addressed", on=later["id"])
    assert created
    assert record["evaluated_patch_sha256"] == later["patch_sha256"]
    assert _status(room, request) == "addressed"

    again, created = room.coordinator.resolve_feedback(request, "addressed", on=later["id"])
    assert not created and again == record
    room.coordinator.resolve_feedback(request, "withdrawn")
    assert _status(room, request) == "withdrawn"


def test_a_failed_later_attempt_is_no_evidence(make):
    room = make(Member("claude", fail_calls={2}))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    room.round()
    failed = room.candidate("claude", 2)
    with pytest.raises(RoomError, match="not a completed candidate"):
        room.coordinator.resolve_feedback(request, "addressed", on=failed["id"])


def test_evidence_from_another_room_is_refused(make, git_repo, tmp_path):
    room = make(Member("claude"))
    room.round()
    request = _add(room, room.candidate("claude")["id"])["feedback_id"]
    room.coordinator.close_room(room.room_id)
    other_id = room.coordinator.create("another room", ["claude"])["id"]
    room.coordinator.run_round(other_id, "go")
    foreign = room.store.list_candidates(other_id)[0]
    room.store._conn.execute(
        "UPDATE rooms SET state='awaiting_decision' WHERE id=?", (room.room_id,)
    )
    with pytest.raises(RoomError, match="not a candidate of this room"):
        room.coordinator.resolve_feedback(request, "addressed", on=foreign["id"])


def _insert_resolution(room: Harness, request: dict, **overrides) -> str:
    """A resolution row written directly, bypassing the writer."""
    conn = _raw(room)
    sequence = conn.execute(
        "SELECT MAX(sequence)+1 FROM room_feedback WHERE room_id=?", (room.room_id,)
    ).fetchone()[0]
    record = {
        "schema_version": 1,
        "feedback_id": str(uuid4()),
        "room_id": room.room_id,
        "request_id": request["feedback_id"],
        "status": "withdrawn",
        "evaluated_candidate_id": None,
        "evaluated_patch_sha256": None,
        "surface": "api",
        "sequence": sequence,
        "created_at": "2026-11-10T00:00:00+00:00",
    }
    row = {
        "candidate_id": request["candidate_id"],
        "request_id": request["feedback_id"],
        "sequence": sequence,
    }
    row.update({k: v for k, v in overrides.items() if k in row})
    record.update({k: v for k, v in overrides.items() if k in record and k not in row})
    if "record_sequence" in overrides:
        record["sequence"] = overrides["record_sequence"]
    body = overrides.get("record_json", json.dumps(record, sort_keys=True))
    conn.execute(
        """INSERT INTO room_feedback
        (id,room_id,candidate_id,record_type,request_id,sequence,record_json,created_at)
        VALUES (?,?,?,'resolution',?,?,?,'now')""",
        (record["feedback_id"], room.room_id, row["candidate_id"], row["request_id"],
         row["sequence"], body),
    )
    return record["feedback_id"]


def test_a_well_formed_direct_resolution_is_believed(make):
    # The control for the damaged cases below: the helper writes a valid row.
    room = make(*_both())
    room.round()
    request = _add(room, room.candidate("claude")["id"])
    _insert_resolution(room, request)
    assert _status(room, request["feedback_id"]) == "withdrawn"


@pytest.mark.parametrize(
    "damage",
    [
        {"record_json": "{not json"},
        {"status": "fixed"},
        {"status": "addressed"},
        {"record_sequence": 99},
    ],
    ids=["json", "status", "no-evidence", "body-sequence"],
)
def test_a_damaged_latest_resolution_body_reads_unknown_for_its_request_only(make, damage):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])
    other = _add(room, parent["id"], "another one")
    room.coordinator.resolve_feedback(request["feedback_id"], "withdrawn")
    _insert_resolution(room, request, **damage)
    view = room.store.feedback(room.room_id)
    assert view.problem is None
    assert view.checks[request["feedback_id"]].status == "unknown"
    assert "latest resolution" in view.checks[request["feedback_id"]].reason
    assert view.checks[other["feedback_id"]].status == "open"
    with pytest.raises(RoomError, match="cannot be trusted"):
        room.coordinator.resolve_feedback(request["feedback_id"], "withdrawn")


@pytest.mark.parametrize("field, value", [("kind", []), ("kind", {}), ("text", 5)])
def test_a_request_with_a_malformed_value_reads_unknown_alone(make, field, value):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])
    other = _add(room, parent["id"], "another")
    _raw(room).execute(
        "UPDATE room_feedback SET record_json=? WHERE id=?",
        (json.dumps(dict(request, **{field: value})), request["feedback_id"]),
    )
    checks = room.store.feedback(room.room_id).checks
    assert checks[request["feedback_id"]].status == "unknown"
    assert checks[other["feedback_id"]].status == "open"


@pytest.mark.parametrize("value", [[], {}, 3])
def test_a_resolution_with_a_malformed_status_reads_unknown_alone(make, value):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])
    other = _add(room, parent["id"], "another")
    _insert_resolution(room, request, status=value)
    checks = room.store.feedback(room.room_id).checks
    assert checks[request["feedback_id"]].status == "unknown"
    assert checks[other["feedback_id"]].status == "open"


def test_another_repository_cannot_write_this_rooms_feedback(make, tmp_path):
    import subprocess

    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
    other = RoomCoordinator(room.config, other_repo, agents=[Member("claude")])
    try:
        for attempt in (
            lambda: other.add_feedback(parent["id"], "change", "x"),
            lambda: other.resolve_feedback(request, "withdrawn"),
            lambda: other.carry_feedback(request, parent["id"]),
        ):
            with pytest.raises(RoomError, match="different repository"):
                attempt()
    finally:
        other.close()
    assert room.store.feedback_count(room.room_id) == 1


def _damaged_set(room: Harness):
    """A request with a withdrawal, then a second request, all on claude's
    candidate: the withdrawal is not the last row, so removing it leaves a gap."""
    parent = room.candidate("claude")
    first = _add(room, parent["id"], "first")
    room.coordinator.resolve_feedback(first["feedback_id"], "withdrawn")
    second = _add(room, parent["id"], "second")
    withdrawal = room.store.feedback(room.room_id).checks[first["feedback_id"]].resolution
    return parent, first, second, withdrawal["feedback_id"]


_SET_DAMAGE = {
    "resolution-request-null": "UPDATE room_feedback SET request_id=NULL WHERE id=:w",
    "resolution-retargeted": "UPDATE room_feedback SET request_id=:s WHERE id=:w",
    "resolution-other-candidate": "UPDATE room_feedback SET candidate_id=:peer WHERE id=:w",
    "resolution-type": "UPDATE room_feedback SET record_type='garbage' WHERE id=:w",
    "request-names-request": "UPDATE room_feedback SET request_id=:f WHERE id=:s",
    "request-id-null": "UPDATE room_feedback SET id=NULL WHERE id=:s",
    "deleted": "DELETE FROM room_feedback WHERE id=:w",
    "sequence-text": "UPDATE room_feedback SET sequence='broken' WHERE id=:w",
    "moved-to-another-room": "UPDATE room_feedback SET room_id='elsewhere' WHERE id=:w",
}


@pytest.mark.parametrize("damage", sorted(_SET_DAMAGE))
def test_a_damaged_record_set_makes_every_request_unknown_and_stops_writers(make, damage):
    room = make(*_both())
    room.round()
    parent, first, second, withdrawal = _damaged_set(room)
    conn = _raw(room)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        _SET_DAMAGE[damage],
        {
            "w": withdrawal,
            "f": first["feedback_id"],
            "s": second["feedback_id"],
            "peer": room.candidate("codex")["id"],
        },
    )
    conn.execute("PRAGMA foreign_keys=ON")

    view = room.store.feedback(room.room_id)
    assert view.problem is not None
    assert view.problem in view.warnings[0]
    assert view.checks and all(c.status == "unknown" for c in view.checks.values())
    assert all(c.request is None for c in view.checks.values())
    with pytest.raises(RoomError, match="cannot be trusted"):
        _add(room, parent["id"])
    with pytest.raises(RoomError, match="cannot be trusted|unknown feedback"):
        room.coordinator.resolve_feedback(first["feedback_id"], "withdrawn")
    with pytest.raises(RoomError, match="cannot be trusted"):
        room.coordinator.build_repair_plan(parent["id"], None)


@pytest.mark.parametrize("damage", ["moved-last", "retargeted-earlier"])
def test_damage_only_the_set_check_can_see(make, damage):
    # The damaged row is the room's last, so no gap shows; and the request
    # it is pointed at is an earlier one on the same candidate.
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    first = _add(room, parent["id"], "first")["feedback_id"]
    second = _add(room, parent["id"], "second")["feedback_id"]
    room.coordinator.resolve_feedback(second, "withdrawn")
    withdrawal = room.store.feedback(room.room_id).checks[second].resolution["feedback_id"]
    conn = _raw(room)
    if damage == "moved-last":
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "UPDATE room_feedback SET room_id='elsewhere' WHERE id=?", (withdrawal,)
        )
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        conn.execute(
            "UPDATE room_feedback SET request_id=? WHERE id=?", (first, withdrawal)
        )
    view = room.store.feedback(room.room_id)
    assert view.problem is not None
    assert {c.status for c in view.checks.values()} == {"unknown"}


def test_a_record_set_damaged_after_confirm_refuses_the_round(make):
    claude = Member("claude")
    room = make(claude)
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    plan = room.coordinator.build_repair_plan(parent["id"], None)
    _raw(room).execute("UPDATE room_feedback SET sequence=7 WHERE id=?", (request,))
    with pytest.raises(RoomError, match=f"feedback {request} changed"):
        room.coordinator.run_round(room.room_id, plan.prompt, target=plan)
    assert len(claude.scopes) == 1


def test_a_repair_record_that_cannot_be_believed_makes_its_requests_unknown(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    room.repair(parent["id"], None)
    assert _status(room, request) == "sent"
    row = room.rounds()[-1]
    record = json.loads(row["provenance_json"])
    record["tree"]["kind"] = []
    _raw(room).execute(
        "UPDATE rounds SET provenance_json=? WHERE id=?", (json.dumps(record), row["id"])
    )
    check = room.store.feedback(room.room_id).checks[request]
    assert check.status == "unknown"
    assert "cannot be believed" in check.reason


def test_a_request_on_a_candidate_of_no_member_reads_unknown(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    _raw(room).execute("UPDATE candidates SET member_id='ghost' WHERE id=?", (parent["id"],))
    assert _status(room, request) == "unknown"


def test_carry_refuses_to_return_an_untrusted_earlier_copy(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    room.repair(parent["id"], None)
    new = room.candidate("claude")
    copy = room.coordinator.carry_feedback(request, new["id"]).record["feedback_id"]
    room.coordinator.resolve_feedback(copy, "withdrawn")
    _raw(room).execute(
        "UPDATE room_feedback SET record_json='{damaged' WHERE request_id=?", (copy,)
    )
    assert _status(room, copy) == "unknown"
    with pytest.raises(RoomError, match="cannot be trusted"):
        room.coordinator.carry_feedback(request, new["id"])


def test_a_damaged_request_reads_unknown(make):
    room = make(Member("claude"))
    room.round()
    request = _add(room, room.candidate("claude")["id"])
    _raw(room).execute(
        "UPDATE room_feedback SET record_json=? WHERE id=?",
        (json.dumps(dict(request, patch_sha256="0" * 64)), request["feedback_id"]),
    )
    check = room.store.feedback(room.room_id).checks[request["feedback_id"]]
    assert check.status == "unknown" and "patch digest" in check.reason


# ------------------------------------------------------------------- repair


def test_render_pins_the_task_bytes():
    head = (
        "Repair your candidate C. Your working tree holds exactly that candidate's "
        "patch. Ignored files, such as installed dependencies, may be missing.\n\n"
    )
    assert render_repair_task("C", "free") == head + "free"
    requests = [
        {"kind": "question", "text": "why?", "anchor": {"path": "a.py"}},
        {"kind": "change", "text": "one\ntwo\n\nthree", "anchor": None},
        {"kind": "change", "text": "x", "anchor": {"path": "we]ird\n.py"}},
    ]
    assert render_repair_task("C", None, requests) == head + (
        "Change requests:\n"
        "1. one\n   two\n\n   three\n"
        '2. ["we]ird\\n.py"] x\n'
        "\n"
        "Questions (answer these in your response):\n"
        "1. [a.py] why?"
    )
    assert render_repair_task("C", "free", requests[:1]) == head + (
        "Questions (answer these in your response):\n1. [a.py] why?\n\nfree"
    )
    assert render_feedback_batch([]) == ""
    nel = render_feedback_batch([{"kind": "change", "text": "x", "anchor": {"path": "a\x85b.py"}}])
    assert nel == 'Change requests:\n1. ["a\\u0085b.py"] x'
    assert "\x85" not in render_repair_task("C", None, [{"kind": "change", "text": "x", "anchor": {"path": "a\x85b.py"}}])


def test_a_repair_sends_the_candidates_open_requests(make):
    claude, codex = _both()
    room = make(claude, codex)
    room.round()
    parent = room.candidate("claude")
    change = _add(room, parent["id"], f"rename {SENTINEL}", path="claude.txt")
    question = _add(room, parent["id"], "why this way?", kind="question")
    withdrawn = _add(room, parent["id"], "never mind")
    room.coordinator.resolve_feedback(withdrawn["feedback_id"], "withdrawn")
    _add(room, room.candidate("codex")["id"], "not for claude")

    plan, _ = room.repair(parent["id"], None)

    assert [item["id"] for item in plan.feedback] == [
        change["feedback_id"],
        question["feedback_id"],
    ]
    assert plan.agent_task == render_repair_task(parent["id"], None, [change, question])
    assert plan.agent_task in claude.scopes[-1]["prompt"]
    row = room.rounds()[-1]
    assert row["prompt"] == f"Repair candidate {parent['id']}"
    assert SENTINEL not in row["prompt"]
    tree = room.provenance(row).record["tree"]
    assert tree["feedback_ids"] == [change["feedback_id"], question["feedback_id"]]
    assert _status(room, change["feedback_id"]) == "sent"
    assert _status(room, question["feedback_id"]) == "sent"
    assert _status(room, withdrawn["feedback_id"]) == "withdrawn"
    assert room.store.feedback(room.room_id).warnings == []


def test_a_repair_with_neither_feedback_nor_prompt_is_refused(make):
    room = make(Member("claude"))
    room.round()
    with pytest.raises(RoomError, match="nothing to repair"):
        room.coordinator.build_repair_plan(room.candidate("claude")["id"], None)


def test_feedback_ids_narrow_the_batch_and_must_be_open_here(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    first = _add(room, parent["id"], "first")["feedback_id"]
    second = _add(room, parent["id"], "second")["feedback_id"]
    gone = _add(room, parent["id"], "gone")["feedback_id"]
    room.coordinator.resolve_feedback(gone, "withdrawn")
    peer = _add(room, room.candidate("codex")["id"])["feedback_id"]
    build = room.coordinator.build_repair_plan

    plan = build(parent["id"], None, feedback_ids=[second])
    assert [item["id"] for item in plan.feedback] == [second]
    assert build(parent["id"], None, feedback_ids=[second, first]).token == build(
        parent["id"], None, feedback_ids=[first, second]
    ).token
    for ids, match in (
        ([gone], "is withdrawn"),
        ([peer], "not on"),
        ([str(uuid4())], "not a feedback request"),
        ([first, first], "listed twice"),
    ):
        with pytest.raises(RoomError, match=match):
            build(parent["id"], None, feedback_ids=ids)


def test_a_failed_repair_leaves_its_requests_open(make):
    room = make(Member("claude", fail_calls={2}))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    room.repair(parent["id"], None)
    assert _status(room, request) == "open"
    plan = room.coordinator.build_repair_plan(parent["id"], None)
    assert [item["id"] for item in plan.feedback] == [request]


@pytest.mark.parametrize("change", ["withdraw", "resolve"])
def test_feedback_changed_after_confirm_refuses_the_round(make, change):
    claude = Member("claude")
    room = make(claude)
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    plan = room.coordinator.build_repair_plan(parent["id"], None)
    if change == "withdraw":
        room.coordinator.resolve_feedback(request, "withdrawn")
    else:
        # Make the request no longer open without a resolution: rewrite it.
        _raw(room).execute(
            "UPDATE room_feedback SET record_json=? WHERE id=?",
            (
                json.dumps(
                    dict(room.store.feedback(room.room_id).checks[request].request, text="changed"),
                    sort_keys=True,
                ),
                request,
            ),
        )
    calls = len(claude.scopes)
    with pytest.raises(RoomError, match=f"feedback {request} changed"):
        room.coordinator.run_round(room.room_id, plan.prompt, target=plan)
    assert len(claude.scopes) == calls
    assert len(room.rounds()) == 1
    assert room.store.get_room(room.room_id)["state"] == "awaiting_decision"


def test_a_request_added_after_confirm_stays_open(make):
    room = make(Member("claude"))
    room.round()
    parent = room.candidate("claude")
    sent = _add(room, parent["id"], "first")["feedback_id"]
    plan = room.coordinator.build_repair_plan(parent["id"], None)
    late = _add(room, parent["id"], "second")["feedback_id"]
    room.coordinator.run_round(room.room_id, plan.prompt, target=plan)
    assert _status(room, sent) == "sent"
    assert _status(room, late) == "open"


def test_after_a_repair_the_request_is_carried_to_the_new_candidate(make):
    room = make(Member("claude"), gate={"commands": ["false"]})
    room.round()
    parent = room.candidate("claude")
    anchored = _add(room, parent["id"], "keep the file", path="claude.txt")["feedback_id"]
    plain = _add(room, parent["id"], "no file")["feedback_id"]
    room.repair(parent["id"], None)
    new = room.candidate("claude")
    assert new["state"] == "completed" and not new["eligible"]  # the gate failed

    with pytest.raises(RoomError, match="newer completed candidate"):
        room.coordinator.build_repair_plan(parent["id"], None)
    carried = room.coordinator.carry_feedback(anchored, new["id"])
    assert carried.created
    assert carried.record["anchor"] == {"path": "claude.txt"}
    assert carried.record["carried_from"] == anchored
    assert _status(room, carried.record["feedback_id"]) == "open"
    assert _status(room, anchored) == "sent"
    assert room.coordinator.carry_feedback(anchored, new["id"]).created is False
    room.coordinator.carry_feedback(plain, new["id"])

    plan = room.coordinator.build_repair_plan(new["id"], None)
    assert len(plan.feedback) == 2


def test_carry_drops_an_anchor_the_new_candidate_does_not_change(make):
    class Mover(Member):
        async def aexecute(self, prompt, workdir, timeout):
            result = await super().aexecute(prompt, workdir, timeout)
            if len(self.scopes) == 2:
                (workdir / "claude.txt").rename(workdir / "moved.txt")
            return result

    room = make(Mover("claude"))
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"], path="claude.txt")["feedback_id"]
    room.repair(parent["id"], None)
    new = room.candidate("claude")
    assert "claude.txt" not in room.store.load_candidate(new["id"])["files_changed"]
    carried = room.coordinator.carry_feedback(request, new["id"])
    assert carried.record["anchor"] is None
    assert carried.source_anchor == {"path": "claude.txt"}


def test_carry_refuses_another_member_an_earlier_round_and_a_withdrawal(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    request = _add(room, parent["id"])["feedback_id"]
    with pytest.raises(RoomError, match="member it was written for"):
        room.coordinator.carry_feedback(request, room.candidate("codex")["id"])
    with pytest.raises(RoomError, match="not from a later round"):
        room.coordinator.carry_feedback(request, parent["id"])
    room.coordinator.resolve_feedback(request, "withdrawn")
    room.round()
    with pytest.raises(RoomError, match="withdrawn"):
        room.coordinator.carry_feedback(request, room.candidate("claude")["id"])


# ------------------------------------------------------ the round's side


def _rewrite_feedback_ids(room: Harness, row: dict, ids: list[str]) -> None:
    record = json.loads(row["provenance_json"])
    record["tree"]["feedback_ids"] = ids
    _raw(room).execute(
        "UPDATE rounds SET provenance_json=? WHERE id=?", (json.dumps(record), row["id"])
    )


def test_a_repair_round_naming_what_is_not_its_request_is_reported(make):
    room = make(*_both())
    room.round()
    parent = room.candidate("claude")
    peer_request = _add(room, room.candidate("codex")["id"])["feedback_id"]
    own = _add(room, parent["id"])["feedback_id"]
    room.coordinator.resolve_feedback(own, "withdrawn")
    resolution = room.store.feedback(room.room_id).checks[own].resolution["feedback_id"]
    room.repair(parent["id"], "free text only")
    missing = str(uuid4())
    _rewrite_feedback_ids(room, room.rounds()[-1], [missing, resolution, peer_request])

    view = room.store.feedback(room.room_id)
    assert view.warnings == [
        f"repair round 2 names feedback {missing}, which is not a request in this room",
        f"repair round 2 names feedback {resolution}, which is a resolution, not a request",
        f"repair round 2 names feedback {peer_request}, which is a request on another candidate",
    ]
    # Attributed to the wrong candidate, the request itself is not believed.
    assert view.checks[peer_request].status == "unknown"
    assert "repaired another candidate" in view.checks[peer_request].reason


# ------------------------------------------------------- memory and apply


class _Recording(FakeMemory):
    def __init__(self):
        super().__init__()
        self.queries: list[str] = []
        self.shared: list[str] = []

    async def search_context(self, query, *args, **kwargs):
        self.queries.append(query)
        return await super().search_context(query, *args, **kwargs)

    async def share_result(self, task, result, *, kind="task-result"):
        self.shared.append(task.prompt)
        return await super().share_result(task, result, kind=kind)


def test_no_feedback_text_reaches_memory_through_search_or_apply(git_repo, tmp_path):
    memory = _Recording()
    agent = Member("claude")
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[agent], memory=memory)
    try:
        room_id = coordinator.create("the room prompt", ["claude"])["id"]
        coordinator.run_round(room_id, "a round")
        parent = coordinator.store.list_candidates(room_id)[0]
        coordinator.add_feedback(parent["id"], "change", f"please {SENTINEL}")
        plan = coordinator.build_repair_plan(parent["id"], None)
        coordinator.run_round(room_id, plan.prompt, target=plan)
        assert SENTINEL in agent.scopes[-1]["prompt"]
        new = max(coordinator.store.list_candidates(room_id), key=lambda r: r["round_number"])
        assert SENTINEL not in coordinator.store.load_candidate(new["id"])["output"]
        coordinator.select(room_id, new["id"])
        coordinator.apply(room_id)
        assert memory.queries and all(SENTINEL not in q for q in memory.queries)
        assert memory.shared and all(SENTINEL not in p for p in memory.shared)
    finally:
        coordinator.close()


def test_prune_removes_a_rooms_feedback(make):
    room = make(Member("claude"))
    room.round()
    request = _add(room, room.candidate("claude")["id"])["feedback_id"]
    room.coordinator.resolve_feedback(request, "withdrawn")
    room.coordinator.close_room(room.room_id)
    assert room.coordinator.prune_terminal(keep=0, retention_days=0) == [room.room_id]
    assert _raw(room).execute("SELECT COUNT(*) FROM room_feedback").fetchone()[0] == 0


def test_every_status_is_listed():
    assert set(FEEDBACK_STATUSES) == {
        "open", "sent", "addressed", "not_addressed", "withdrawn", "unknown",
    }


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
    yield SimpleNamespace(coordinator=coordinator, room_id=room_id, parent=parent, claude=claude)
    coordinator.close()


def _run(*args: str):
    return CliRunner().invoke(cli, ["room", *args])


def _id_from(output: str, word: str) -> str:
    tokens = output.split()
    return tokens[tokens.index(word) + 1]


def test_cli_add_list_resolve_and_repair(cli_room, tmp_path):
    parent = cli_room.parent["id"]
    res = _run("feedback", "add", parent, "--kind", "change", "--file", "claude.txt",
               "--text", "line one\nline two")
    assert res.exit_code == 0, res.output
    assert "(file: claude.txt)" in res.output
    request = _id_from(res.output, "request")
    text_file = tmp_path / "q.txt"
    text_file.write_bytes(b"why?\r\n")
    refused = _run("feedback", "add", parent, "--kind", "question", "--text-file", str(text_file))
    assert refused.exit_code == 1 and "control characters" in refused.output

    listed = _run("feedback", "list", cli_room.room_id)
    assert listed.exit_code == 0, listed.output
    assert f"{request}  {parent}  change   open" in listed.output
    assert "    | line one\n    | line two" in listed.output

    preview = _run("repair", parent, "--preview")
    assert preview.exit_code == 0, preview.output
    assert "Feedback:     1 change request(s), 0 question(s)" in preview.output
    assert f"{request}  change" in preview.output
    assert "line one" not in preview.output
    token = next(
        line.split()[1] for line in preview.output.splitlines() if line.startswith("Token:")
    )
    ran = _run("repair", parent, "--confirm", token)
    assert ran.exit_code == 0, ran.output
    assert len(cli_room.claude.scopes) == 2
    assert "sent" in _run("feedback", "list", cli_room.room_id, "--candidate", parent).output

    new = max(
        (c for c in cli_room.coordinator.store.list_candidates(cli_room.room_id)
         if c["member_id"] == "claude"),
        key=lambda c: c["round_number"],
    )["id"]
    resolved = _run("feedback", "resolve", request, "not-addressed", "--on", new)
    assert resolved.exit_code == 0, resolved.output
    assert f"Recorded {request} not-addressed on candidate {new}" in resolved.output
    again = _run("feedback", "resolve", request, "not-addressed", "--on", new)
    assert "Already recorded" in again.output

    carried = _run("feedback", "carry", request, "--to", new)
    assert carried.exit_code == 0, carried.output
    assert "File: claude.txt, kept" in carried.output


def test_cli_list_never_prints_the_text_of_an_unknown_entry(cli_room):
    coordinator = cli_room.coordinator
    request = coordinator.add_feedback(cli_room.parent["id"], "change", SENTINEL)
    coordinator.resolve_feedback(request["feedback_id"], "withdrawn")
    coordinator.store._conn.execute(
        "UPDATE room_feedback SET record_json='{damaged' WHERE record_type='resolution'"
    )
    listed = _run("feedback", "list", cli_room.room_id)
    assert listed.exit_code == 0, listed.output
    assert "latest resolution" in listed.output
    assert SENTINEL not in listed.output


def test_cli_list_survives_a_null_feedback_id(cli_room):
    coordinator = cli_room.coordinator
    request = coordinator.add_feedback(cli_room.parent["id"], "change", SENTINEL)
    coordinator.store._conn.execute(
        "UPDATE room_feedback SET id=NULL WHERE id=?", (request["feedback_id"],)
    )
    listed = _run("feedback", "list", cli_room.room_id)
    assert listed.exit_code == 0, listed.output
    assert "cannot be trusted" in listed.output
    assert SENTINEL not in listed.output


def test_cli_refusals_are_one_line_errors(cli_room):
    res = _run("feedback", "add", cli_room.parent["id"], "--kind", "change",
               "--file", "nope.txt", "--text", "x")
    assert res.exit_code == 1 and "not a file this candidate changed" in res.output
    res = _run("feedback", "add", cli_room.parent["id"], "--kind", "change")
    assert res.exit_code == 2 and "exactly one of --text and --text-file" in res.output
    res = _run("feedback", "resolve", str(uuid4()), "withdrawn")
    assert res.exit_code == 1 and "unknown feedback" in res.output
    res = _run("repair", cli_room.parent["id"], "--preview")
    assert res.exit_code == 1 and "nothing to repair" in res.output
    res = _run("feedback", "list", cli_room.room_id)
    assert res.exit_code == 0 and "No feedback in room" in res.output
