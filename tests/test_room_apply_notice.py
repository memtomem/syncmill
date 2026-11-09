"""``room apply`` says when the applied candidate has no gate behind it (#276)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from syncmill.cli import _room_apply_verification, cli
from syncmill.config import SyncmillConfig
from syncmill.rooms import RoomCoordinator
from test_rooms import WritingAgent

UNVERIFIED = (
    "[syncmill] Unverified: this candidate has no quality gate report, "
    "and --verify was not used."
)
NOT_RECORDED = (
    "[syncmill] Verification not recorded: this candidate has no readable gate "
    "record, and --verify was not used."
)
PASSING_GATE = f"{sys.executable} -c pass"
FAILED_REPORT = {
    "passed": False,
    "score": 0.0,
    "checks": [
        {"command": "x", "passed": False, "exit_code": 1, "output": "", "duration_seconds": 0.1}
    ],
}


class _Room:
    """One real room with a selected candidate, driven through the real commands."""

    def __init__(self, git_repo: Path, tmp_path: Path, monkeypatch, *, gate: list[str]):
        self.repo = git_repo
        self.config = SyncmillConfig(
            repo_root=str(git_repo),
            project={"id": "room-apply-notice"},
            runs={"root": str(tmp_path / "runs")},
            workspace={"root": str(tmp_path / "workspaces")},
            rooms={"enabled": True, "max_members": 3, "max_parallel": 3},
            gate={"commands": gate},
        )
        self.coordinator = RoomCoordinator(
            self.config, git_repo, agents=[WritingAgent("claude")]
        )
        room = self.coordinator.create("implement it")
        self.room_id = room["id"]
        self.coordinator.run_round(self.room_id)
        self.candidate = self.coordinator.store.list_candidates(self.room_id)[0]
        self.coordinator.select(self.room_id, self.candidate["id"])
        monkeypatch.setattr(
            "syncmill.cli._room_coordinator",
            lambda repo: (git_repo, self.config, self.coordinator),
        )

    def edit_gate(self, edit) -> None:
        path = Path(self.candidate["artifact_path"]) / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        edit(manifest)
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def apply(self, *flags: str, command: tuple[str, ...] = ("room", "apply")) -> list[str]:
        result = CliRunner().invoke(cli, [*command, self.room_id, "--repo", str(self.repo), *flags])
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        applied = next(
            i for i, line in enumerate(lines) if line.startswith(f"Applied room {self.room_id} ")
        )
        assert lines[applied + 1].startswith("Receipt: ")
        self.before_applied = lines[:applied]
        return lines[applied + 2:]


def _room(git_repo, tmp_path, monkeypatch, gate: list[str] | None = None) -> _Room:
    return _Room(git_repo, tmp_path, monkeypatch, gate=gate or [])


@pytest.mark.parametrize("command", [("room", "apply"), ("room-apply-worker",)], ids=["apply", "worker"])
def test_a_candidate_with_no_gate_says_so(command, git_repo, tmp_path, monkeypatch):
    room = _room(git_repo, tmp_path, monkeypatch)
    assert room.apply(command=command) == [UNVERIFIED]


@pytest.mark.parametrize("command", [("room", "apply"), ("room-apply-worker",)], ids=["apply", "worker"])
def test_a_candidate_with_a_passing_gate_says_nothing(command, git_repo, tmp_path, monkeypatch):
    room = _room(git_repo, tmp_path, monkeypatch, [PASSING_GATE])
    assert room.apply(command=command) == []


@pytest.mark.parametrize("command", [("room", "apply"), ("room-apply-worker",)], ids=["apply", "worker"])
def test_verify_says_nothing_about_a_candidate_with_no_gate(command, git_repo, tmp_path, monkeypatch):
    room = _room(git_repo, tmp_path, monkeypatch)
    room.config.gate.commands = [PASSING_GATE]  # with none, apply refuses before the helper
    assert room.apply("--verify", command=command) == []
    # The notice that does speak for --verify is the existing one, ahead of the result.
    assert room.before_applied == [
        "[syncmill] verify: this round ran without a gate; verify uses the current [gate]"
    ]


def test_the_line_claims_the_record_not_what_ran(git_repo, tmp_path, monkeypatch):
    # A manifest edited from a passing report to null still applies. "No report" stays
    # true; "no gate ran" would not.
    room = _room(git_repo, tmp_path, monkeypatch, [PASSING_GATE])
    room.edit_gate(lambda manifest: manifest.update(gate=None))
    assert room.apply() == [UNVERIFIED]


@pytest.mark.parametrize(
    "edit",
    [
        lambda manifest: manifest.pop("gate"),
        lambda manifest: manifest.update(gate="yes"),
        lambda manifest: manifest.update(gate=FAILED_REPORT),  # contradicts `eligible`
    ],
    ids=["no-key", "not-a-report", "failed-report-on-an-eligible-candidate"],
)
def test_an_unusable_gate_record_never_reads_as_verified(edit, git_repo, tmp_path, monkeypatch):
    room = _room(git_repo, tmp_path, monkeypatch, [PASSING_GATE])
    room.edit_gate(edit)
    assert room.apply() == [NOT_RECORDED]


def test_a_read_failure_after_the_apply_is_not_a_failed_apply(git_repo, tmp_path, monkeypatch):
    room = _room(git_repo, tmp_path, monkeypatch)
    store = room.coordinator.store
    real_apply = room.coordinator.apply
    reached = []

    def unreadable(candidate_id):
        reached.append(candidate_id)
        raise OSError("manifest went away")

    def apply_then_break_reads(*a, **k):
        applied = real_apply(*a, **k)
        monkeypatch.setattr(store, "load_candidate_manifest", unreadable)
        return applied

    monkeypatch.setattr(room.coordinator, "apply", apply_then_break_reads)
    assert room.apply() == [NOT_RECORDED]
    assert reached == [room.candidate["id"]]


def test_a_passing_report_on_an_ineligible_candidate_is_not_silent():
    # A passing report cannot sit on a candidate with a patch that is not eligible, so
    # the record contradicts itself. Silence here would read as verified.
    passing = {
        "passed": True,
        "score": 1.0,
        "checks": [
            {"command": "x", "passed": True, "exit_code": 0, "output": "", "duration_seconds": 0.1}
        ],
    }
    store = SimpleNamespace(
        get_room=lambda room_id: {"selected_candidate_id": "candidate"},
        get_candidate=lambda candidate_id: {"state": "completed", "eligible": False},
        load_candidate_manifest=lambda candidate_id: {
            "schema_version": 1, "state": "completed", "gate": passing,
        },
    )
    assert _room_apply_verification(SimpleNamespace(store=store), "room") == NOT_RECORDED
