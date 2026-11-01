"""Human judgments of applied room candidates and the room outcome (#174 PR 1)."""

from __future__ import annotations

import json
import pytest

from test_rooms import WritingAgent, _config
from syncmill.room_compare import (
    build_room_comparison,
    comparison_to_dict,
    render_review_markdown,
    render_room_comparison,
)
from syncmill.rooms import (
    ROOM_OUTCOMES,
    RoomCoordinator,
    RoomError,
    RoomStore,
    room_outcome_status,
)


@pytest.fixture
def coordinator(git_repo, tmp_path):
    value = RoomCoordinator(
        _config(tmp_path),
        git_repo,
        agents=[WritingAgent("claude"), WritingAgent("codex")],
    )
    yield value
    value.close()


def _room_with_candidates(coordinator):
    room = coordinator.create("compare", ["claude", "codex"])
    coordinator.run_round(room["id"])
    return room["id"], coordinator.store.list_candidates(room["id"])


def _applied_room(coordinator):
    room_id, candidates = _room_with_candidates(coordinator)
    coordinator.select(room_id, candidates[0]["id"])
    run_id, _ = coordinator.apply(room_id)
    return room_id, candidates[0], run_id


def _rewrite_judgment(store, judgment_id, mutate):
    row = store._conn.execute(
        "SELECT record_json FROM room_judgments WHERE id=?", (judgment_id,)
    ).fetchone()
    with store._tx() as conn:
        conn.execute(
            "UPDATE room_judgments SET record_json=? WHERE id=?",
            (mutate(row["record_json"]), judgment_id),
        )


# ------------------------------------------------------------- outcome states


def test_outcome_moves_through_selection_apply_and_judgment(coordinator):
    room_id, candidates = _room_with_candidates(coordinator)
    store = coordinator.store
    assert store.outcome(room_id).status == "not_selected"

    coordinator.select(room_id, candidates[0]["id"])
    assert store.outcome(room_id).status == "selected_not_applied"

    run_id, _ = coordinator.apply(room_id)
    applied = store.outcome(room_id)
    assert applied.status == "applied_unreviewed"
    assert applied.applied_run_id == run_id
    assert applied.candidate_id == candidates[0]["id"]

    first, created = coordinator.judge(room_id, "accepted", rationale="matches the spec")
    assert created
    accepted = store.outcome(room_id)
    assert accepted.status == "accepted"
    assert accepted.judgment == first
    assert first["patch_sha256"] == candidates[0]["patch_sha256"]
    assert first["applied_run_id"] == run_id
    assert first["sequence"] == 1 and first["supersedes"] is None

    second, created = coordinator.judge(room_id, "rejected", rationale="broke prod")
    assert created
    rejected = store.outcome(room_id)
    assert rejected.status == "rejected"
    assert rejected.judgment_count == 2
    assert second["sequence"] == 2 and second["supersedes"] == first["judgment_id"]
    # Append-only: the first verdict is still in the history, unchanged.
    rows = store.list_judgments(room_id)
    assert [json.loads(r["record_json"])["verdict"] for r in rows] == [
        "accepted",
        "rejected",
    ]
    assert {o for o in ROOM_OUTCOMES} >= {
        "not_selected",
        "selected_not_applied",
        "applied_unreviewed",
        "accepted",
        "rejected",
        "unknown",
    }


def test_judgment_does_not_move_the_room_state_or_revision(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    before = coordinator.store.get_room(room_id)
    coordinator.judge(room_id, "accepted")
    after = coordinator.store.get_room(room_id)
    assert (after["state"], after["revision"], after["updated_at"]) == (
        before["state"],
        before["revision"],
        before["updated_at"],
    )
    kinds = [event["kind"] for event in coordinator.store.events(room_id)]
    assert kinds[-1] == "candidate.judged"


def test_a_closed_applied_room_can_still_be_judged(coordinator):
    room_id, _, run_id = _applied_room(coordinator)
    coordinator.close_room(room_id)
    assert coordinator.store.get_room(room_id)["state"] == "closed"
    record, _ = coordinator.judge(room_id, "rejected")
    assert record["applied_run_id"] == run_id
    assert coordinator.store.outcome(room_id).status == "rejected"


@pytest.mark.parametrize("state", ["applying", "recovery_required"])
def test_an_unresolved_apply_is_unknown_not_unreviewed(coordinator, state):
    room_id, candidates = _room_with_candidates(coordinator)
    coordinator.select(room_id, candidates[0]["id"])
    with coordinator.store._tx() as conn:
        conn.execute(
            "UPDATE rooms SET state=?, apply_run_id='run-x' WHERE id=?",
            (state, room_id),
        )
    outcome = coordinator.store.outcome(room_id)
    assert outcome.status == "unknown"
    assert outcome.reason


def test_an_apply_without_a_selected_candidate_is_unknown():
    room = {"id": "r", "state": "applied", "applied_run_id": "run", "selected_candidate_id": None}
    assert room_outcome_status(room, None, []).status == "unknown"


def test_judgments_without_an_apply_are_unknown_never_believed():
    room = {"id": "r", "state": "selected", "selected_candidate_id": "c"}
    outcome = room_outcome_status(room, None, [{"id": "j", "sequence": 1, "record_json": "{}"}])
    assert outcome.status == "unknown"


# ---------------------------------------------------------- refusal / fences


@pytest.mark.parametrize("stage", ["created", "selected"])
def test_only_an_applied_change_can_be_judged(coordinator, stage):
    room_id, candidates = _room_with_candidates(coordinator)
    if stage == "selected":
        coordinator.select(room_id, candidates[0]["id"])
    with pytest.raises(RoomError, match="only an applied change"):
        coordinator.judge(room_id, "accepted")
    assert coordinator.store.list_judgments(room_id) == []


def test_unknown_verdict_and_control_characters_are_refused(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    with pytest.raises(RoomError, match="unknown verdict"):
        coordinator.judge(room_id, "approved")
    with pytest.raises(RoomError, match="control characters"):
        coordinator.judge(room_id, "accepted", rationale="bad\x1b[31m")
    with pytest.raises(RoomError, match="unknown judgment surface"):
        coordinator.store.record_judgment(room_id, "accepted", surface="web")
    assert coordinator.store.list_judgments(room_id) == []


def test_digest_fence_refuses_a_change_you_did_not_review(coordinator):
    room_id, candidate, _ = _applied_room(coordinator)
    with pytest.raises(RoomError, match="differs from the one you reviewed"):
        coordinator.judge(room_id, "accepted", expected_patch_sha256="0" * 64)
    assert coordinator.store.list_judgments(room_id) == []
    record, _ = coordinator.judge(
        room_id, "accepted", expected_patch_sha256=candidate["patch_sha256"]
    )
    assert record["patch_sha256"] == candidate["patch_sha256"]


def test_an_inconsistent_selection_record_cannot_bind_a_judgment(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    with coordinator.store._tx() as conn:
        conn.execute("UPDATE rooms SET selection_json='{}' WHERE id=?", (room_id,))
    with pytest.raises(RoomError, match="selection record is inconsistent"):
        coordinator.judge(room_id, "accepted")


def test_a_legacy_selection_without_a_record_can_still_be_judged(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    with coordinator.store._tx() as conn:
        conn.execute("UPDATE rooms SET selection_json=NULL WHERE id=?", (room_id,))
    record, created = coordinator.judge(room_id, "accepted")
    assert created and coordinator.store.outcome(room_id).status == "accepted"


def test_judging_from_another_project_is_refused(coordinator, git_repo, tmp_path):
    room_id, _, _ = _applied_room(coordinator)
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(other_repo)], check=True)
    other = RoomCoordinator(_config(tmp_path), other_repo, agents=[WritingAgent("claude")])
    try:
        with pytest.raises(RoomError, match="different repository"):
            other.judge(room_id, "accepted")
    finally:
        other.close()
    assert coordinator.store.list_judgments(room_id) == []


# ------------------------------------------------- duplicates, crashes, memory


def test_a_repeated_verdict_is_idempotent(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    first, created = coordinator.judge(room_id, "accepted", rationale="  good  ")
    again, created_again = coordinator.judge(room_id, "accepted", rationale="good")
    assert created and not created_again
    assert again == first
    assert len(coordinator.store.list_judgments(room_id)) == 1
    # A changed rationale is a new judgment, not a duplicate.
    _, created_new = coordinator.judge(room_id, "accepted", rationale="still good")
    assert created_new
    assert len(coordinator.store.list_judgments(room_id)) == 2


def test_a_crash_inside_the_write_leaves_no_partial_judgment(coordinator, monkeypatch):
    room_id, _, _ = _applied_room(coordinator)
    events_before = len(coordinator.store.events(room_id))

    def boom(*args, **kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(RoomStore, "_event", staticmethod(boom))
    with pytest.raises(RuntimeError, match="disk gone"):
        coordinator.judge(room_id, "accepted")
    monkeypatch.undo()
    assert coordinator.store.list_judgments(room_id) == []
    assert len(coordinator.store.events(room_id)) == events_before
    assert coordinator.store.outcome(room_id).status == "applied_unreviewed"


def test_judging_never_touches_memory(coordinator, git_repo, tmp_path):
    """A judgment is durable even while the memory backend is down: it never
    reads, writes or opens memory at all."""

    class ExplodingMemory:
        def __getattr__(self, name):
            raise AssertionError(f"memory.{name} must not be used by a judgment")

    room_id, _, _ = _applied_room(coordinator)
    judging = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")], memory=ExplodingMemory()
    )
    try:
        record, created = judging.judge(room_id, "accepted")
    finally:
        judging.close()
    assert created and record["verdict"] == "accepted"
    assert coordinator.store.outcome(room_id).status == "accepted"


# ---------------------------------------------------------- corrupted records


@pytest.mark.parametrize(
    "mutate, reason",
    [
        (lambda raw: "not json", "not valid JSON"),
        (lambda raw: "[]", "not an object"),
        (lambda raw: json.dumps({**json.loads(raw), "schema_version": 2}), "version"),
        (lambda raw: json.dumps({**json.loads(raw), "verdict": "maybe"}), "verdict"),
        (lambda raw: json.dumps({**json.loads(raw), "patch_sha256": "0" * 64}), "patch digest"),
        (lambda raw: json.dumps({**json.loads(raw), "applied_run_id": "other"}), "apply run"),
        (lambda raw: json.dumps({**json.loads(raw), "candidate_id": "other"}), "candidate"),
        (lambda raw: json.dumps({**json.loads(raw), "room_id": "other"}), "room"),
        (lambda raw: json.dumps({**json.loads(raw), "surface": "web"}), "surface"),
        (lambda raw: json.dumps({**json.loads(raw), "rationale": " x "}), "stored form"),
        (
            lambda raw: json.dumps({k: v for k, v in json.loads(raw).items() if k != "supersedes"}),
            "supersedes",
        ),
    ],
)
def test_a_damaged_latest_judgment_is_unknown_and_blocks_new_ones(
    coordinator, mutate, reason
):
    room_id, _, _ = _applied_room(coordinator)
    record, _ = coordinator.judge(room_id, "accepted")
    _rewrite_judgment(coordinator.store, record["judgment_id"], mutate)
    outcome = coordinator.store.outcome(room_id)
    assert outcome.status == "unknown"
    assert reason in outcome.reason
    with pytest.raises(RoomError, match="cannot be trusted"):
        coordinator.judge(room_id, "rejected")


def test_a_damaged_latest_never_falls_back_to_an_older_verdict(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    coordinator.judge(room_id, "accepted")
    latest, _ = coordinator.judge(room_id, "rejected")
    _rewrite_judgment(coordinator.store, latest["judgment_id"], lambda raw: "{}")
    assert coordinator.store.outcome(room_id).status == "unknown"


def test_a_missing_history_entry_is_unknown(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    first, _ = coordinator.judge(room_id, "accepted")
    coordinator.judge(room_id, "rejected")
    with coordinator.store._tx() as conn:
        conn.execute("DELETE FROM room_judgments WHERE id=?", (first["judgment_id"],))
    outcome = coordinator.store.outcome(room_id)
    assert outcome.status == "unknown"
    assert "missing or extra" in outcome.reason


def test_a_judgment_row_whose_id_differs_from_its_record_is_unknown(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    record, _ = coordinator.judge(room_id, "accepted")
    with coordinator.store._tx() as conn:
        conn.execute(
            "UPDATE room_judgments SET id='forged' WHERE id=?", (record["judgment_id"],)
        )
    assert coordinator.store.outcome(room_id).status == "unknown"


# ------------------------------------------------------------ prune, schema


def test_prune_removes_a_judged_room_with_its_judgments(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    coordinator.judge(room_id, "accepted")
    assert coordinator.prune_terminal(keep=0, retention_days=999) == [room_id]
    assert coordinator.store.list_judgments(room_id) == []


def test_a_store_created_before_judgments_gains_the_table(tmp_path):
    store = RoomStore(tmp_path / "rooms")
    with store._tx() as conn:
        conn.execute("DROP TABLE room_judgments")
    store.close()
    reopened = RoomStore(tmp_path / "rooms")
    try:
        assert reopened.list_judgments("missing") == []
    finally:
        reopened.close()


# ---------------------------------------------------------- comparison views


def test_comparison_reports_the_outcome_in_every_form(coordinator):
    room_id, candidate, run_id = _applied_room(coordinator)
    cmp = build_room_comparison(coordinator.store, room_id)
    assert comparison_to_dict(cmp)["room"]["outcome"]["status"] == "applied_unreviewed"
    assert f"Outcome: applied as run {run_id}, not reviewed" in render_room_comparison(cmp)

    coordinator.judge(room_id, "rejected", rationale="regressed\n```\nnot code")
    cmp = build_room_comparison(coordinator.store, room_id)
    data = comparison_to_dict(cmp)["room"]["outcome"]
    assert data["status"] == "rejected"
    assert data["applied_run_id"] == run_id
    assert data["judgment"]["verdict"] == "rejected"
    assert data["judgment"]["patch_sha256"] == candidate["patch_sha256"]
    assert data["judgment"]["rationale"] == "regressed\n```\nnot code"
    text = render_room_comparison(cmp)
    assert "Outcome: REJECTED (human)" in text
    assert "    regressed" in text
    markdown = render_review_markdown(cmp, generated_at="2026-10-31T00:00:00+00:00")
    assert "## Outcome" in markdown
    assert "| Verdict (human) | rejected |" in markdown
    # The rationale is fenced with a fence longer than any backtick run in it.
    assert "````\nregressed\n```\nnot code\n````" in markdown


def test_comparison_shows_unknown_with_its_reason(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    record, _ = coordinator.judge(room_id, "accepted")
    _rewrite_judgment(coordinator.store, record["judgment_id"], lambda raw: "{}")
    cmp = build_room_comparison(coordinator.store, room_id)
    assert comparison_to_dict(cmp)["room"]["outcome"]["status"] == "unknown"
    assert "Outcome: unknown — latest judgment cannot be trusted" in render_room_comparison(
        cmp
    )


def test_an_unreadable_judgment_history_degrades_the_comparison(coordinator, monkeypatch):
    room_id, _, _ = _applied_room(coordinator)

    def broken(room_id):
        raise RoomError("db gone")

    monkeypatch.setattr(coordinator.store, "list_judgments", broken)
    cmp = build_room_comparison(coordinator.store, room_id)
    assert cmp.outcome is not None and cmp.outcome.status == "unknown"
    assert "db gone" in cmp.outcome.reason


def test_outcome_text_is_escaped(coordinator):
    room_id, _, _ = _applied_room(coordinator)
    coordinator.judge(room_id, "accepted", rationale="ok \u202e evil")
    text = render_room_comparison(build_room_comparison(coordinator.store, room_id))
    assert "\u202e" not in text
