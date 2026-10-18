"""room_compare: the read-only candidate comparison read model (#172 PR 1).

Candidates are seeded through the production write path
(``RoomCandidateStore.save`` + ``RoomStore.add_candidate``), never hand-written
rows, so every manifest/row shape here is one ``run_round`` can produce; the
damage cases then edit those real records.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from syncmill import room_compare
from syncmill.models import GateCheck, GateReport, TaskResult, TaskStatus, TokenUsage
from syncmill.room_compare import (
    build_room_comparison,
    comparison_to_dict,
    render_room_comparison,
)
from syncmill.rooms import RoomError, RoomStore
from syncmill.worktree import WorkspaceError

FP = "fp-1"
PATCH = "diff --git a/a.py b/a.py\n+SECRET-PATCH\n"


def _gate(*checks: tuple[str, bool]) -> GateReport:
    return GateReport.from_checks(
        [
            GateCheck(command=c, passed=p, exit_code=0 if p else 1, output="SECRET-GATE")
            for c, p in checks
        ]
    )


class Room:
    """A room in a real RoomStore, driven through the store's write API."""

    def __init__(self, tmp_path: Path, members: list[str]):
        self.store = RoomStore(tmp_path / "rooms")
        self.members = members
        self.room = self.store.create_room(
            repo_root=tmp_path, prompt="SECRET-PROMPT", agents=members
        )
        self.id = self.room["id"]

    def start(self, *, progress: list[str] | None = None, lease: int = 3600) -> dict:
        rnd = self.store.start_round(
            self.id,
            prompt="SECRET-PROMPT",
            source_fingerprint=FP,
            snapshot_commit="base",
            lease_owner="worker",
            lease_timeout_seconds=lease,
            max_rounds=10,
        )
        names = self.members if progress is None else progress
        self.store.init_round_members(
            self.id, rnd["id"], [{"member_id": m, "provider_id": m} for m in names]
        )
        return rnd

    def save(
        self,
        rnd: dict,
        member: str,
        *,
        status: TaskStatus = TaskStatus.COMPLETED,
        patch: str | None = PATCH,
        files: tuple[str, ...] = ("a.py",),
        gate: GateReport | None = None,
        usage: TokenUsage | None = None,
        duration: float = 1.5,
    ) -> dict:
        state = "completed" if status == TaskStatus.COMPLETED else "failed"
        result = TaskResult(
            task_id=uuid4(),
            agent_id=member,
            status=status,
            output="SECRET-OUTPUT",
            exit_code=0 if state == "completed" else 1,
            duration_seconds=duration,
            usage=usage,
        )
        manifest = self.store.candidates.save(
            candidate_id=str(uuid4()),
            room_id=self.id,
            round_id=rnd["id"],
            member_id=member,
            provider_id=member,
            attempt=1,
            result=result,
            attempt_state=state,
            patch=patch,
            files_changed=list(files),
            gate=gate if state == "completed" else None,
            base_commit="base",
            source_fingerprint=FP,
        )
        self.store.add_candidate(manifest)
        self.store.set_member_state(rnd["id"], member, state)
        return manifest

    def finish(self, rnd: dict) -> None:
        self.store.finish_round(self.id, rnd["id"])

    def compare(self, **kwargs):
        kwargs.setdefault("source_check", lambda: FP)
        return build_room_comparison(self.store, self.id, **kwargs)


def _gate_dict(passed: bool, *checks: tuple[str, bool]) -> dict:
    """A gate block in exactly the shape RoomCandidateStore.save writes."""
    return {
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "checks": [
            {"command": c, "passed": ok, "exit_code": 0 if ok else 1, "output": "", "duration_seconds": 0.1}
            for c, ok in checks
        ],
    }


def _edit_manifest(manifest: dict, **changes) -> None:
    path = Path(manifest["artifact_path"]) / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def _row(cmp, candidate_id: str):
    for group in cmp.groups:
        for row in group.rows:
            if row.candidate_id == candidate_id:
                return row
    raise AssertionError(candidate_id)


@pytest.fixture
def close_stores():
    stores: list[RoomStore] = []
    yield stores
    for store in stores:
        store.close()


@pytest.fixture
def make_room(tmp_path, close_stores):
    def factory(members: list[str]) -> Room:
        room = Room(tmp_path / uuid4().hex, members)
        close_stores.append(room.store)
        return room

    return factory


# ------------------------------------------------------------------ gate


def test_gate_verdicts_are_distinct_and_never_fold_into_pass(make_room):
    room = make_room(["a", "b", "c"])
    rnd = room.start()
    passed = room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    failed = room.save(rnd, "b", gate=_gate(("pytest -q", False)))
    not_run = room.save(rnd, "c", status=TaskStatus.FAILED)
    room.finish(rnd)
    cmp = room.compare()

    assert _row(cmp, passed["candidate_id"]).gate.verdict == "passed"
    assert _row(cmp, failed["candidate_id"]).gate.verdict == "failed"
    assert _row(cmp, failed["candidate_id"]).not_eligible_reason == "quality gate failed"
    nr = _row(cmp, not_run["candidate_id"])
    assert (nr.gate.verdict, nr.gate.reason) == ("not_run", "agent did not complete")
    assert cmp.groups[0].gate_profile.kind == "commands"
    assert cmp.groups[0].gate_profile.commands == ("pytest -q",)

    verdicts = {
        r["member_id"]: r["gate"]["verdict"]
        for r in comparison_to_dict(cmp)["rounds"][0]["candidates"]
    }
    assert verdicts == {"a": "passed", "b": "failed", "c": "not_run"}
    text = render_room_comparison(cmp)
    assert "gate: not run (agent did not complete)" in text
    assert "✓ pytest -q" in text and "× pytest -q" in text


def test_null_gate_reads_no_report_never_pass_or_not_configured(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)
    cmp = room.compare()
    row = _row(cmp, saved["candidate_id"])
    assert row.gate.verdict == "no_report"
    assert cmp.groups[0].gate_profile.kind == "no_report"
    text = render_room_comparison(cmp)
    assert "gate: no gate report" in text
    assert "configured" not in text


def test_failed_attempt_whose_manifest_claims_completed_stays_not_run(make_room):
    # SQLite is authoritative for completion: editing the manifest state of a
    # failed attempt to "completed" (gate null) must not make a single-member
    # round read "no gate report on any completed candidate".
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", status=TaskStatus.FAILED)
    room.finish(rnd)
    _edit_manifest(saved, state="completed")
    cmp = room.compare()
    assert _row(cmp, saved["candidate_id"]).gate.verdict == "not_run"
    assert cmp.groups[0].gate_profile.kind == "unknown"


def test_manifest_state_disagreeing_with_sqlite_is_unrecognized(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    room.finish(rnd)
    _edit_manifest(saved, state="failed")
    gate = _row(room.compare(), saved["candidate_id"]).gate
    assert (gate.verdict, gate.reason) == ("unknown", "unrecognized record")


def test_failed_gate_edited_to_null_contradicts_stored_eligibility(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", gate=_gate(("pytest -q", False)))
    room.finish(rnd)
    _edit_manifest(saved, gate=None)
    cmp = room.compare()
    gate = _row(cmp, saved["candidate_id"]).gate
    assert (gate.verdict, gate.reason) == (
        "unknown",
        "record contradicts stored eligibility",
    )
    assert cmp.groups[0].gate_profile.kind == "unknown"


def test_passed_gate_edited_to_failed_contradicts_stored_eligibility(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    room.finish(rnd)
    _edit_manifest(
        saved,
        gate=_gate_dict(False, ("pytest -q", False)),
    )
    assert _row(room.compare(), saved["candidate_id"]).gate.verdict == "unknown"


def test_disagreeing_gate_commands_make_the_round_profile_mixed(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    edited = room.save(rnd, "b", gate=_gate(("pytest -q", True)))
    room.finish(rnd)
    _edit_manifest(
        edited,
        gate=_gate_dict(True, ("true", True)),
    )
    group = room.compare().groups[0]
    assert group.gate_profile.kind == "mixed"
    assert any("disagree" in w for w in group.warnings)


@pytest.mark.parametrize(
    "changes",
    [
        {"gate": "passed"},
        {"gate": {"passed": True, "checks": "pytest"}},
        {"gate": {"passed": "yes", "checks": []}},
        {"schema_version": 2},
        {"schema_version": True},
        {"gate": {"passed": True, "checks": []}},  # no score
        {"gate": {"passed": 1, "score": 1.0, "checks": []}},
    ],
)
def test_unrecognized_gate_records_read_unknown(make_room, changes):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    room.finish(rnd)
    _edit_manifest(saved, **changes)
    cmp = room.compare()
    gate = _row(cmp, saved["candidate_id"]).gate
    assert (gate.verdict, gate.reason) == ("unknown", "unrecognized record")
    assert cmp.groups[0].gate_profile.kind == "unknown"


@pytest.mark.parametrize(
    "field", ["command", "passed", "exit_code", "output", "duration_seconds"]
)
def test_a_gate_check_missing_any_field_is_unrecognized(make_room, field):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    room.finish(rnd)
    gate = _gate_dict(True, ("pytest -q", True))
    del gate["checks"][0][field]
    _edit_manifest(saved, gate=gate)
    verdict = _row(room.compare(), saved["candidate_id"]).gate
    assert (verdict.verdict, verdict.reason) == ("unknown", "unrecognized record")


def test_a_missing_gate_key_is_not_an_explicit_null(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)
    path = Path(saved["artifact_path"]) / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["gate"]
    path.write_text(json.dumps(data), encoding="utf-8")
    gate = _row(room.compare(), saved["candidate_id"]).gate
    assert (gate.verdict, gate.reason) == ("unknown", "unrecognized record")


# --------------------------------------------------------- usage / patch


def test_missing_usage_is_not_reported_never_zero(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    none = room.save(rnd, "a")
    some = room.save(
        rnd, "b", usage=TokenUsage(total_input_tokens=1200, output_tokens=34)
    )
    room.finish(rnd)
    cmp = room.compare()
    assert _row(cmp, none["candidate_id"]).usage is None
    data = comparison_to_dict(cmp)["rounds"][0]["candidates"]
    assert [c["usage"] for c in data if c["member_id"] == "a"] == [None]
    text = render_room_comparison(cmp)
    assert "usage: not reported" in text
    assert "input 1,200, output 34" in text
    assert _row(cmp, some["candidate_id"]).usage.total_input_tokens == 1200


def test_cache_write_alone_is_reported_usage(make_room):
    room = make_room(["a"])
    rnd = room.start()
    room.save(rnd, "a", usage=TokenUsage(cache_creation_tokens=123))
    room.finish(rnd)
    text = render_room_comparison(room.compare())
    assert "usage: cache write 123" in text
    assert "not reported" not in text


def test_malformed_usage_is_normalized_not_copied(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)
    _edit_manifest(saved, usage={"total_input_tokens": -5, "output_tokens": True, "x": 1})
    row = _row(room.compare(), saved["candidate_id"])
    assert row.usage is not None
    assert row.usage.total_input_tokens is None and row.usage.output_tokens is None
    assert "usage: not reported" in render_room_comparison(room.compare())


def test_patch_size_binary_and_large_come_from_the_verified_stream(
    make_room, monkeypatch
):
    monkeypatch.setattr(room_compare, "LARGE_PATCH_BYTES", 40)
    room = make_room(["a", "b"])
    rnd = room.start()
    binary = room.save(rnd, "a", patch="diff --git a/x b/x\nGIT binary patch\nliteral 3\n")
    small = room.save(rnd, "b", patch="d\n")
    room.finish(rnd)
    # A lying manifest size is ignored: the size comes from the digest-verified file.
    _edit_manifest(small, patch_bytes=10**9)
    cmp = room.compare()
    b = _row(cmp, binary["candidate_id"])
    assert (b.patch_binary, b.patch_large) == (True, True)
    s = _row(cmp, small["candidate_id"])
    assert (s.patch_bytes, s.patch_binary, s.patch_large) == (2, False, False)
    assert "binary, large" in render_room_comparison(cmp)


# ---------------------------------------------------- damage stays visible


def test_unreadable_candidate_keeps_its_row_and_an_apply_blocker(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    broken = room.save(rnd, "a")
    fine = room.save(rnd, "b")
    room.finish(rnd)
    patch = Path(broken["artifact_path"]) / "candidate.patch"
    patch.write_bytes(patch.read_bytes() + b"tamper")
    cmp = room.compare()
    row = _row(cmp, broken["candidate_id"])
    assert row.readable is False
    assert "digest mismatch" in row.unreadable_reason
    assert row.eligible is True  # the stored verdict is still shown ...
    assert row.apply_blockers[0].startswith("artifact unreadable:")  # ... but blocked
    assert (row.gate.verdict, row.gate.reason) == ("unknown", "artifact unreadable")
    assert row.files_changed is None
    assert _row(cmp, fine["candidate_id"]).apply_blockers == ()
    assert cmp.groups[0].overlap.unknown == (broken["candidate_id"],)
    data = comparison_to_dict(cmp)["rounds"][0]["candidates"]
    blockers = {c["candidate_id"]: c["apply_blockers"] for c in data}
    assert blockers[broken["candidate_id"]][0].startswith("artifact unreadable:")
    text = render_room_comparison(cmp)
    assert "unreadable:" in text and "artifact unreadable:" in text


def test_malformed_fields_degrade_one_row_field_by_field(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    odd = room.save(rnd, "a", files=("a.py",))
    fine = room.save(rnd, "b", files=("a.py", "b.py"))
    room.finish(rnd)
    _edit_manifest(odd, files_changed=None, duration_seconds="slow", patch_bytes=None)
    cmp = room.compare()
    row = _row(cmp, odd["candidate_id"])
    assert row.files_changed is None
    assert row.duration_seconds is None
    assert row.patch_bytes == len(PATCH)  # verified size, unaffected
    assert any("files_changed" in w for w in row.warnings)
    overlap = cmp.groups[0].overlap
    assert overlap.unknown == (odd["candidate_id"],)
    # An unknown file set is excluded, never treated as empty.
    assert overlap.compared == (fine["candidate_id"],)
    assert "file set unknown" in render_room_comparison(cmp)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"files_changed": None}, "files_changed"),
        ({"duration_seconds": "slow"}, "duration_seconds"),
        ({"exit_code": "x"}, "exit_code"),
    ],
)
def test_metadata_apply_cannot_convert_is_an_apply_blocker(make_room, changes, field):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)
    _edit_manifest(saved, **changes)
    cmp = room.compare()
    row = _row(cmp, saved["candidate_id"])
    assert row.readable is True  # the artifact itself verifies ...
    assert row.apply_blockers == (f"artifact metadata apply cannot use: {field}",)
    data = comparison_to_dict(cmp)["rounds"][0]["candidates"][0]
    assert data["apply_blockers"] == [f"artifact metadata apply cannot use: {field}"]
    assert f"- artifact metadata apply cannot use: {field}" in render_room_comparison(cmp)


def test_metadata_apply_accepts_is_not_a_blocker(make_room):
    # Display validation is stricter than apply (a numeric string is not a
    # duration here), but the blocker follows apply: float("1.5") succeeds.
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)
    _edit_manifest(saved, duration_seconds="1.5")
    row = _row(room.compare(), saved["candidate_id"])
    assert row.duration_seconds is None
    assert row.apply_blockers == ()


def test_member_without_candidate_reports_progress_evidence(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    room.save(rnd, "a")
    room.store.reconcile_round_members(rnd["id"])
    room.store.recovery_required(room.id, "boom", None)
    cmp = room.compare()
    [missing] = cmp.groups[0].missing
    assert missing.status == "no_candidate"
    assert missing.evidence == ("member state: interrupted", "room needs recovery")
    assert "b (b): no candidate retained (member state: interrupted; room needs recovery)" in (
        render_room_comparison(cmp)
    )


def test_member_without_progress_row_reads_not_recorded(make_room):
    # A round can fail before progress rows exist; expected members come from
    # the room, not from round_members.
    room = make_room(["a", "b"])
    rnd = room.start(progress=["a"])
    room.save(rnd, "a")
    room.store.recovery_required(room.id, "provider resolution failed", None)
    [missing] = room.compare().groups[0].missing
    assert missing.member_id == "b"
    assert missing.evidence[0] == "member state: not recorded"


def test_running_round_with_live_lease_reads_in_progress(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    room.save(rnd, "a")
    cmp = room.compare()
    [missing] = cmp.groups[0].missing
    assert missing.status == "in_progress"
    assert "b (b): in progress" in render_room_comparison(cmp)
    # The running room is not selectable yet: that is an apply blocker.
    assert "room is running" in cmp.groups[0].rows[0].apply_blockers


def test_running_round_with_expired_lease_is_not_in_progress(make_room):
    room = make_room(["a", "b"])
    rnd = room.start(lease=1)
    room.save(rnd, "a")
    [missing] = room.compare(now=time.time() + 60).groups[0].missing
    assert missing.status == "no_candidate"
    assert "lease expired" in missing.evidence


# ------------------------------------------------------- rounds / source


def test_default_is_latest_round_and_round_selects_another(make_room):
    room = make_room(["a"])
    first = room.start()
    one = room.save(first, "a")
    room.finish(first)
    second = room.start()
    two = room.save(second, "a")
    room.finish(second)

    latest = room.compare()
    assert [g.number for g in latest.groups] == [2]
    assert latest.groups[0].rows[0].candidate_id == two["candidate_id"]
    assert latest.groups[0].starting_tree == "not_recorded"
    earlier = room.compare(round_number=1)
    assert earlier.groups[0].rows[0].candidate_id == one["candidate_id"]
    assert earlier.groups[0].starting_tree == "snapshot"
    with pytest.raises(RoomError, match=r"no round 7 \(rounds: 1, 2\)"):
        room.compare(round_number=7)
    assert "lineage is not persisted" in render_room_comparison(latest)


def test_all_rounds_groups_rounds_by_gate_profile(make_room):
    room = make_room(["a"])
    for gate in (_gate(("pytest -q", True)), _gate(("pytest -q", True)), None):
        rnd = room.start()
        room.save(rnd, "a", gate=gate)
        room.finish(rnd)
    rnd = room.start()
    room.save(rnd, "a", status=TaskStatus.FAILED)
    room.finish(rnd)
    cmp = room.compare(all_rounds=True)
    assert [g.number for g in cmp.groups] == [1, 2, 3, 4]
    assert list(cmp.cross_round) == [
        {"rounds": [1, 2], "gate_profile": "commands", "commands": ["pytest -q"], "reason": None},
        # A missing report establishes nothing about the round's settings.
        {"rounds": [3], "gate_profile": "undecidable", "commands": [], "reason": "no gate report recorded"},
        {
            "rounds": [4],
            "gate_profile": "undecidable",
            "commands": [],
            "reason": "no completed candidate with a usable record",
        },
    ]
    text = render_room_comparison(cmp)
    assert "rounds 1, 2: same commands: pytest -q" in text
    assert "round 3: cannot establish equality (no gate report recorded)" in text


def test_round_and_all_rounds_are_mutually_exclusive(make_room):
    room = make_room(["a"])
    with pytest.raises(ValueError):
        room.compare(round_number=1, all_rounds=True)


def test_room_without_rounds_renders_a_notice(make_room):
    cmp = make_room(["a"]).compare()
    assert cmp.groups == () and cmp.source_status == "not_recorded"
    assert "No rounds have run" in render_room_comparison(cmp)


def test_changed_source_is_an_apply_blocker(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)
    cmp = room.compare(source_check=lambda: "fp-2")
    assert cmp.source_status == "changed"
    assert _row(cmp, saved["candidate_id"]).apply_blockers == (
        "source changed since the room started",
    )


@pytest.mark.parametrize(
    "error", [WorkspaceError("fatal: not a git repository"), OSError("git missing")]
)
def test_source_check_failure_is_unavailable_never_current(make_room, error):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)

    def check():
        raise error

    cmp = room.compare(source_check=check)
    assert cmp.source_status == "unavailable"
    assert str(error) in cmp.source_reason
    assert _row(cmp, saved["candidate_id"]).apply_blockers[0].startswith(
        "source check unavailable:"
    )
    assert "check unavailable" in render_room_comparison(cmp)


def test_selected_candidate_is_marked_as_a_human_choice(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    chosen = room.save(rnd, "a")
    other = room.save(rnd, "b")
    room.finish(rnd)
    room.store.select_candidate(room.id, chosen["candidate_id"])
    cmp = room.compare()
    assert _row(cmp, chosen["candidate_id"]).selected is True
    assert _row(cmp, other["candidate_id"]).selected is False
    assert "[selected (human)]" in render_room_comparison(cmp)


def test_ineligible_candidate_names_why(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", patch="", files=())
    room.finish(rnd)
    row = _row(room.compare(), saved["candidate_id"])
    assert row.not_eligible_reason == "no patch captured"
    assert row.apply_blockers == ("not eligible: no patch captured",)


# ------------------------------------------------------------- files


def test_file_overlap_splits_common_only_and_partial(make_room):
    room = make_room(["a", "b", "c"])
    rnd = room.start()
    a = room.save(rnd, "a", files=("shared.py", "a.py", "ab.py"))
    b = room.save(rnd, "b", files=("shared.py", "ab.py"))
    c = room.save(rnd, "c", files=("shared.py", "c.py"))
    room.finish(rnd)
    cmp = room.compare()
    overlap = cmp.groups[0].overlap
    assert overlap.common == ("shared.py",)
    assert overlap.only == {
        a["candidate_id"]: ("a.py",),
        b["candidate_id"]: (),
        c["candidate_id"]: ("c.py",),
    }
    assert overlap.partial == {"ab.py": (a["candidate_id"], b["candidate_id"])}
    text = render_room_comparison(cmp)
    assert "common to all (1): shared.py" in text
    assert "changed by some, not all:\n      ab.py: " in text


def test_a_candidate_that_changed_nothing_counts_against_common(make_room):
    # "Common to all" must be literally true: a known-empty file set is in the
    # denominator, so one candidate that changed nothing empties the common set.
    room = make_room(["a", "b", "c"])
    rnd = room.start()
    a = room.save(rnd, "a", files=("shared.py",))
    b = room.save(rnd, "b", files=("shared.py",))
    c = room.save(rnd, "c", patch="", files=())
    room.finish(rnd)
    overlap = room.compare().groups[0].overlap
    assert overlap.common == ()
    assert overlap.no_changes == (c["candidate_id"],)
    assert overlap.partial == {"shared.py": (a["candidate_id"], b["candidate_id"])}


# ------------------------------------------------ redaction / terminal


def test_json_and_text_carry_no_bodies(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    room.save(rnd, "b", status=TaskStatus.FAILED)
    room.finish(rnd)
    cmp = room.compare(all_rounds=True)
    data = comparison_to_dict(cmp)
    assert data["schema_version"] == 1 and data["kind"] == "syncmill.room-compare"
    blob = json.dumps(data) + render_room_comparison(cmp)
    for secret in ("SECRET-PROMPT", "SECRET-OUTPUT", "SECRET-PATCH", "SECRET-GATE"):
        assert secret not in blob


def test_untrusted_strings_cannot_drive_the_terminal_or_forge_lines(make_room):
    hostile = "x\x1b]52;c;AAAA\x07\x1b[2J\nforged: line"
    room = make_room(["a"])
    rnd = room.start()
    room.save(rnd, "a", files=(hostile,), gate=_gate((hostile, True)))
    room.finish(rnd)
    cmp = room.compare()
    text = render_room_comparison(cmp)
    assert "\x1b" not in text and "\x07" not in text
    assert not any(line.startswith("forged") for line in text.splitlines())
    assert "\\x1b]52" in text
    # JSON keeps the raw value (json.dumps escapes it on output).
    data = comparison_to_dict(cmp)
    assert data["rounds"][0]["candidates"][0]["files_changed"] == [hostile]
    assert "\x1b" not in json.dumps(data)


# ------------------------------------------- whole-record damage boundaries


def test_a_corrupt_candidate_row_degrades_only_that_row(make_room):
    room = make_room(["a", "b"])
    rnd = room.start()
    broken = room.save(rnd, "a")
    fine = room.save(rnd, "b")
    room.finish(rnd)
    room.store._conn.execute(
        "UPDATE candidates SET attempt='broken' WHERE id=?", (broken["candidate_id"],)
    )
    cmp = room.compare()
    row = _row(cmp, broken["candidate_id"])
    assert row.readable is False
    # The stored verdict survives and nothing is invented for the bad field.
    assert row.eligible is True and row.attempt is None
    assert row.unreadable_reason.startswith("candidate record unreadable:")
    assert row.apply_blockers[0].startswith("artifact unreadable:")
    assert _row(cmp, fine["candidate_id"]).readable is True
    data = comparison_to_dict(cmp)["rounds"][0]["candidates"]
    damaged = next(c for c in data if c["candidate_id"] == broken["candidate_id"])
    assert (damaged["attempt"], damaged["eligible"]) == (None, True)
    text = render_room_comparison(cmp)
    assert "candidate record unreadable" in text
    assert f"{broken['candidate_id']}  a (a) #?" in text
    assert "(None)" not in text


@pytest.mark.parametrize("bad", ["broken", 0, -1])
def test_a_damaged_round_is_listed_and_other_rounds_still_render(make_room, bad):
    room = make_room(["a"])
    for _ in range(3):
        rnd = room.start()
        room.save(rnd, "a")
        room.finish(rnd)
    second = room.store.list_rounds(room.id)[1]
    room.store._conn.execute(
        "UPDATE rounds SET number=? WHERE id=?", (bad, second["id"])
    )
    cmp = room.compare(all_rounds=True)
    assert [g.number for g in cmp.groups] == [1, 3]
    assert [d["round_id"] for d in cmp.damaged_rounds] == [second["id"]]
    # The default view picks the latest READABLE round, and still lists the damage.
    latest = room.compare()
    assert [g.number for g in latest.groups] == [3]
    assert latest.damaged_rounds == cmp.damaged_rounds
    assert f"Damaged round {second['id']}" in render_room_comparison(latest)
    assert comparison_to_dict(latest)["damaged_rounds"][0]["round_id"] == second["id"]


def test_a_zero_numbered_latest_round_is_reported_not_skipped_silently(make_room):
    room = make_room(["a"])
    for _ in range(3):
        rnd = room.start()
        room.save(rnd, "a")
        room.finish(rnd)
    third = room.store.list_rounds(room.id)[2]
    room.store._conn.execute("UPDATE rounds SET number=0 WHERE id=?", (third["id"],))
    cmp = room.compare()
    assert [g.number for g in cmp.groups] == [2]
    assert [d["round_id"] for d in cmp.damaged_rounds] == [third["id"]]
    assert "malformed round number 0" in render_room_comparison(cmp)


@pytest.mark.parametrize("damage", ["tamper", "delete"])
def test_output_file_damage_blocks_apply(make_room, damage):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)
    output = Path(saved["artifact_path"]) / "output.txt"
    if damage == "tamper":
        output.write_bytes(output.read_bytes() + b"x")
    else:
        output.unlink()
    row = _row(room.compare(), saved["candidate_id"])
    assert row.readable is False
    assert row.apply_blockers[0].startswith("artifact unreadable:")


def test_gate_report_contradicting_its_own_checks_is_unrecognized(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    room.finish(rnd)
    _edit_manifest(
        saved,
        gate=_gate_dict(True, ("pytest -q", False)),
    )
    gate = _row(room.compare(), saved["candidate_id"]).gate
    assert (gate.verdict, gate.reason) == ("unknown", "unrecognized record")


def test_recorded_base_commit_is_displayed(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a")
    room.finish(rnd)
    cmp = room.compare()
    assert _row(cmp, saved["candidate_id"]).base_commit == "base"
    assert comparison_to_dict(cmp)["rounds"][0]["candidates"][0]["base_commit"] == "base"
    assert "base: base" in render_room_comparison(cmp)


# ---------------------------------------------- Details metadata (#172 PR 2)


def test_details_metadata_reaches_the_row_but_error_text_stays_out_of_json(make_room):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    room.finish(rnd)
    _edit_manifest(saved, error="SECRET-ERROR quoting agent output", provider_session_id="sess-1")
    cmp = room.compare()
    row = _row(cmp, saved["candidate_id"])
    assert row.error == "SECRET-ERROR quoting agent output"
    assert row.provider_session_id == "sess-1"
    assert row.exit_code == 0 and row.output_bytes is not None
    assert row.output_sha256 == saved["output_sha256"]  # the SQLite-bound digest
    assert row.gate.checks[0].exit_code == 0
    data = comparison_to_dict(cmp)
    blob = json.dumps(data)
    assert "SECRET-ERROR" not in blob
    candidate = data["rounds"][0]["candidates"][0]
    assert candidate["provider_session_id"] == "sess-1"
    assert candidate["output"]["sha256"] == saved["output_sha256"]
    assert candidate["gate"]["checks"][0]["exit_code"] == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_malformed_check_durations_serialize_as_null(make_room, bad):
    room = make_room(["a"])
    rnd = room.start()
    saved = room.save(rnd, "a", gate=_gate(("pytest -q", True)))
    room.finish(rnd)
    gate = _gate_dict(True, ("pytest -q", True))
    gate["checks"][0]["duration_seconds"] = bad
    path = Path(saved["artifact_path"]) / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["gate"] = gate
    path.write_text(json.dumps(data), encoding="utf-8")  # NaN/Infinity tokens
    cmp = room.compare()
    check = _row(cmp, saved["candidate_id"]).gate.checks[0]
    assert check.duration_seconds is None
    json.dumps(comparison_to_dict(cmp), allow_nan=False)  # strict JSON holds
