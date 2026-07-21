from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeAgent
from syncmill.artifacts import ArtifactError
from syncmill.config import RoomsConfig, SyncmillConfig
from syncmill.models import TaskResult, TaskStatus
from syncmill.rooms import RoomCoordinator, RoomError, RoomStore
from syncmill.worktree import repository_fingerprint
import syncmill.rooms as rooms_module


class WritingAgent(FakeAgent):
    def __init__(self, agent_id: str, *, status: TaskStatus = TaskStatus.COMPLETED):
        super().__init__(
            agent_id,
            status=status,
            output=f"result from {agent_id}",
            error="scripted failure" if status == TaskStatus.FAILED else None,
            exit_code=1 if status == TaskStatus.FAILED else 0,
        )

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        (workdir / f"{self.agent_id}.txt").write_text(f"{prompt}\n", encoding="utf-8")
        return self.execute(prompt, workdir, timeout)


def _config(tmp_path: Path) -> SyncmillConfig:
    return SyncmillConfig(
        project={"id": "room-tests"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True, "max_members": 3, "max_parallel": 3},
    )


def test_rooms_config_rejects_parallel_above_members():
    with pytest.raises(ValueError, match="max_parallel"):
        RoomsConfig(max_members=2, max_parallel=3)


def test_create_allows_only_one_active_room(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        coordinator.create("first")
        with pytest.raises(RoomError, match="active room"):
            coordinator.create("second")
    finally:
        coordinator.close()


def test_round_preserves_every_candidate_and_failed_partial_patch(git_repo, tmp_path):
    agents = [WritingAgent("claude"), WritingAgent("codex", status=TaskStatus.FAILED)]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("change both", ["claude", "codex"])
        completed = coordinator.run_round(room["id"])
        assert completed["state"] == "awaiting_decision"
        candidates = coordinator.store.list_candidates(room["id"])
        assert len(candidates) == 2
        by_member = {candidate["member_id"]: candidate for candidate in candidates}
        assert by_member["claude"]["eligible"] == 1
        assert by_member["codex"]["eligible"] == 0
        failed = coordinator.store.candidates.load(by_member["codex"]["artifact_path"])
        assert failed["patch"]
        assert b"codex.txt" in failed["patch"]
        with pytest.raises(RoomError, match="not eligible"):
            coordinator.select(room["id"], by_member["codex"]["id"])
    finally:
        coordinator.close()


def test_round_quality_gate_receives_worktree_and_candidate_is_eligible(
    git_repo, tmp_path
):
    config = _config(tmp_path)
    config.gate.commands = [
        f'{sys.executable} -c "from pathlib import Path; '
        'assert Path(\'claude.txt\').exists()"'
    ]
    coordinator = RoomCoordinator(config, git_repo, agents=[WritingAgent("claude")])
    try:
        room = coordinator.create("gated")
        completed = coordinator.run_round(room["id"])
        assert completed["state"] == "awaiting_decision"
        candidate = coordinator.store.load_candidate(
            coordinator.store.list_candidates(room["id"])[0]["id"]
        )
        assert candidate["eligible"] is True
        assert candidate["gate"]["passed"] is True
    finally:
        coordinator.close()


def test_select_is_separate_from_apply_and_reuses_run_artifact(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        selected = coordinator.select(room["id"], candidate["id"])
        assert selected["state"] == "selected"
        assert not (git_repo / "claude.txt").exists()

        run_id, receipt = coordinator.apply(room["id"])
        assert receipt.exists()
        assert (git_repo / "claude.txt").read_text() == "implement it\n"
        applied = coordinator.store.get_room(room["id"])
        assert applied["state"] == "applied"
        assert applied["applied_run_id"] == run_id
        run_manifest = (
            Path(coordinator.config.runs.root)
            / coordinator.project_id
            / "runs"
            / run_id
            / "manifest.json"
        ).read_text(encoding="utf-8")
        assert '"origin": "room"' in run_manifest
    finally:
        coordinator.close()


def test_candidate_digest_tampering_is_rejected(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        path = Path(candidate["artifact_path"]) / "candidate.patch"
        path.write_bytes(path.read_bytes() + b"tamper")
        with pytest.raises(RoomError, match="digest mismatch"):
            coordinator.store.candidates.load(candidate["artifact_path"])
    finally:
        coordinator.close()


def test_apply_tampering_returns_room_to_selected(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        path = Path(candidate["artifact_path"]) / "candidate.patch"
        path.write_bytes(path.read_bytes() + b"tamper")

        with pytest.raises(RoomError, match="digest mismatch"):
            coordinator.apply(room["id"])
        recovered = coordinator.store.get_room(room["id"])
        assert recovered["state"] == "selected"
        assert any(
            event["kind"] == "apply.failed"
            for event in coordinator.store.events(room["id"])
        )
    finally:
        coordinator.close()


def test_patch_and_manifest_tampering_is_rejected_by_database_digest(
    git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        row = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], row["id"])
        directory = Path(row["artifact_path"])
        patch_path = directory / "candidate.patch"
        patch = patch_path.read_bytes().replace(b"implement it", b"tampered code")
        patch_path.write_bytes(patch)
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["patch_sha256"] = hashlib.sha256(patch).hexdigest()
        manifest["patch_bytes"] = len(patch)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(RoomError, match="database digest mismatch"):
            coordinator.apply(room["id"])
        assert not (git_repo / "claude.txt").exists()
        assert coordinator.store.get_room(room["id"])["state"] == "selected"
    finally:
        coordinator.close()


@pytest.mark.parametrize("field", ["source_fingerprint", "base_commit"])
def test_candidate_source_metadata_tampering_is_rejected_by_database_binding(
    field, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        row = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], row["id"])
        manifest_path = Path(row["artifact_path"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest[field] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(RoomError, match=rf"candidate {field} binding mismatch"):
            coordinator.apply(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "selected"
        assert not (git_repo / "claude.txt").exists()
    finally:
        coordinator.close()


def test_manifest_cannot_replace_source_fingerprint_to_bypass_drift(
    git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("old source")
        coordinator.run_round(room["id"])
        row = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], row["id"])
        (git_repo / "advance.txt").write_text("new head\n", encoding="utf-8")
        subprocess.run(["git", "add", "advance.txt"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-qm", "advance"], cwd=git_repo, check=True)

        manifest_path = Path(row["artifact_path"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source_fingerprint"] = repository_fingerprint(git_repo)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(RoomError, match="source_fingerprint binding mismatch"):
            coordinator.apply(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "selected"
        assert not (git_repo / "claude.txt").exists()
    finally:
        coordinator.close()


def test_clean_head_change_blocks_old_room_candidate(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("old head")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        (git_repo / "new-head.txt").write_text("new commit\n", encoding="utf-8")
        subprocess.run(["git", "add", "new-head.txt"], cwd=git_repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "advance head"], cwd=git_repo, check=True
        )

        with pytest.raises(RoomError, match="working tree changed"):
            coordinator.apply(room["id"])
        assert not (git_repo / "claude.txt").exists()
        assert coordinator.store.get_room(room["id"])["state"] == "stale"
    finally:
        coordinator.close()


def test_room_finalization_failure_marks_recovery_after_source_apply(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])

        def fail_applied(room_id: str, run_id: str) -> None:
            raise RoomError("injected finalization failure")

        monkeypatch.setattr(coordinator.store, "applied", fail_applied)
        with pytest.raises(RoomError, match="room finalization failed"):
            coordinator.apply(room["id"])
        recovered = coordinator.store.get_room(room["id"])
        assert recovered["state"] == "recovery_required"
        assert recovered["apply_run_id"]
        assert recovered["applied_run_id"] is None
        assert (git_repo / "claude.txt").exists()
    finally:
        coordinator.close()


def test_room_apply_failure_freezes_orphan_run_before_reselecting(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])

        def fail_apply(*_args, **_kwargs):
            raise ArtifactError("injected apply preparation failure")

        monkeypatch.setattr(rooms_module.ArtifactStore, "apply", fail_apply)
        with pytest.raises(ArtifactError, match="injected apply"):
            coordinator.apply(room["id"])

        recovered = coordinator.store.get_room(room["id"])
        assert recovered["state"] == "selected"
        assert recovered["apply_run_id"] is None
        run_dirs = list(
            (
                Path(coordinator.config.runs.root)
                / coordinator.project_id
                / "runs"
            ).glob("*/manifest.json")
        )
        assert len(run_dirs) == 1
        manifest = json.loads(run_dirs[0].read_text(encoding="utf-8"))
        assert manifest["state"] == "recovery_required"
    finally:
        coordinator.close()


def test_room_stays_recovery_required_when_orphan_run_cannot_be_frozen(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])

        def fail_apply(*_args, **_kwargs):
            raise ArtifactError("injected apply failure")

        def fail_freeze(*_args, **_kwargs):
            raise ArtifactError("injected freeze failure")

        monkeypatch.setattr(rooms_module.ArtifactStore, "apply", fail_apply)
        monkeypatch.setattr(
            rooms_module.ArtifactStore, "recovery_required", fail_freeze
        )

        with pytest.raises(RoomError, match="could not be frozen"):
            coordinator.apply(room["id"])
        recovered = coordinator.store.get_room(room["id"])
        assert recovered["state"] == "recovery_required"
        assert recovered["apply_run_id"]
        assert recovered["applied_run_id"] is None
    finally:
        coordinator.close()


def test_persistent_round_reuses_workspace_and_publishes_bounded_checkpoint(
    git_repo, tmp_path
):
    agent = WritingAgent("claude")
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[agent])
    try:
        room = coordinator.create("first")
        coordinator.run_round(room["id"])
        first_workspace = coordinator.store.list_workspaces(room["id"])[0]["path"]
        coordinator.run_round(room["id"], "second")
        second_workspace = coordinator.store.list_workspaces(room["id"])[0]["path"]
        assert second_workspace == first_workspace
        assert "UNTRUSTED DATA" in agent.calls[1]["prompt"]
        assert "--- CURRENT USER TASK ---\nsecond" in agent.calls[1]["prompt"]
        assert coordinator.store.get_room(room["id"])["round_count"] == 2
        checkpoints = [
            event
            for event in coordinator.store.events(room["id"])
            if event["kind"] == "checkpoint.published"
        ]
        assert len(checkpoints) == 2
        coordinator.close_room(room["id"])
        assert not Path(first_workspace).exists()
    finally:
        coordinator.close()


def test_checkpoint_prompt_has_deterministic_utf8_aggregate_byte_cap(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    rows = [
        {
            "id": f"candidate-{index:03d}",
            "created_at": f"2026-01-01T00:00:{index:02d}+00:00",
            "member_id": f"member-{index % 3}",
        }
        for index in range(40)
    ]
    loaded: list[str] = []

    def load_checkpoint(candidate_id: str, max_output_bytes: int):
        loaded.append(candidate_id)
        index = int(candidate_id.rsplit("-", 1)[1])
        output = ("한🙂" * 600).encode("utf-8")[:max_output_bytes].decode(
            "utf-8", errors="ignore"
        )
        return {
            "candidate_id": candidate_id,
            "member_id": f"member-{index % 3}",
            "state": "completed",
            "output": output,
        }

    monkeypatch.setattr(coordinator.store, "list_candidates", lambda _room_id: rows)
    monkeypatch.setattr(
        coordinator.store, "load_candidate_checkpoint", load_checkpoint
    )
    try:
        first = coordinator._prompt_with_checkpoint("room", "현재 작업🙂")
        first_loaded = list(loaded)
        loaded.clear()
        second = coordinator._prompt_with_checkpoint("room", "현재 작업🙂")

        encoded = first.encode("utf-8")
        assert len(encoded) <= rooms_module._ROOM_PROMPT_MAX_BYTES
        assert encoded.decode("utf-8") == first
        assert first == second
        assert first_loaded == loaded
        assert len(first_loaded) < len(rows)
        assert rooms_module._CHECKPOINT_TRUNCATION_MARKER in first
        assert "candidate candidate-039" in first
        assert "candidate candidate-000" not in first
        assert first.endswith("--- CURRENT USER TASK ---\n현재 작업🙂")
    finally:
        coordinator.close()


def test_checkpoint_rejects_an_oversized_current_task(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("first")
        with pytest.raises(RoomError, match="process argument byte limit"):
            coordinator.run_round(
                room["id"], "한" * rooms_module._ROOM_PROMPT_MAX_BYTES
            )
        assert coordinator.store.get_room(room["id"])["state"] == "draft"
    finally:
        coordinator.close()


def test_detached_worker_rejects_oversized_prompt_before_popen(
    monkeypatch, git_repo
):
    called = False

    def fake_popen(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(rooms_module.subprocess, "Popen", fake_popen)
    with pytest.raises(RoomError, match="process argument byte limit"):
        rooms_module.launch_room_worker(
            git_repo,
            "room-id",
            "🙂" * rooms_module._ROOM_PROMPT_MAX_BYTES,
        )
    assert called is False


def test_fresh_lineage_replaces_member_workspace(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("first")
        coordinator.run_round(room["id"])
        first = coordinator.store.list_workspaces(room["id"])[0]["path"]
        coordinator.run_round(room["id"], "fresh", lineage="fresh")
        second = coordinator.store.list_workspaces(room["id"])[0]["path"]
        assert second != first
        assert not Path(first).exists()
    finally:
        coordinator.close()


def test_branch_from_seeds_all_members_from_eligible_candidate(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path),
        git_repo,
        agents=[WritingAgent("claude"), WritingAgent("codex")],
    )
    try:
        room = coordinator.create("first", ["claude", "codex"])
        coordinator.run_round(room["id"])
        base = next(
            candidate
            for candidate in coordinator.store.list_candidates(room["id"])
            if candidate["member_id"] == "claude"
        )

        coordinator.run_round(
            room["id"],
            "build on the selected base",
            lineage="branch-from",
            base_candidate_id=base["id"],
        )
        latest_codex = [
            candidate
            for candidate in coordinator.store.list_candidates(room["id"])
            if candidate["member_id"] == "codex"
        ][-1]
        artifact = coordinator.store.candidates.load(latest_codex["artifact_path"])
        assert b"claude.txt" in artifact["patch"]
        assert b"codex.txt" in artifact["patch"]
    finally:
        coordinator.close()


def test_room_cleanup_rejects_tampered_workspace_paths(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("first")
        coordinator.run_round(room["id"])
        coordinator.store._conn.execute(
            "UPDATE room_workspaces SET run_root=? WHERE room_id=?",
            (str(tmp_path), room["id"]),
        )
        with pytest.raises(RoomError, match="unsafe paths"):
            coordinator.close_room(room["id"])
        assert tmp_path.exists()
    finally:
        coordinator.close()


def test_store_cas_prevents_select_while_running(tmp_path):
    store = RoomStore(tmp_path / "rooms")
    try:
        room = store.create_room(repo_root=tmp_path, prompt="p", agents=["claude"])
        with pytest.raises(RoomError, match="cannot select"):
            store.select_candidate(room["id"], "missing")
    finally:
        store.close()


def test_apply_confirmation_cas_rejects_reselected_candidate(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path),
        git_repo,
        agents=[WritingAgent("claude"), WritingAgent("codex")],
    )
    try:
        room = coordinator.create("compare", ["claude", "codex"])
        coordinator.run_round(room["id"])
        candidates = coordinator.store.list_candidates(room["id"])
        first = coordinator.select(room["id"], candidates[0]["id"])
        coordinator.select(room["id"], candidates[1]["id"])

        with pytest.raises(RoomError, match="selected candidate changed"):
            coordinator.apply(
                room["id"],
                expected_candidate_id=candidates[0]["id"],
                expected_revision=first["revision"],
            )
        current = coordinator.store.get_room(room["id"])
        assert current["selected_candidate_id"] == candidates[1]["id"]
        assert not (git_repo / "claude.txt").exists()
        assert not (git_repo / "codex.txt").exists()
    finally:
        coordinator.close()


def test_missing_provider_after_lease_claim_enters_recovery(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("provider disappears")
        coordinator.agents = []
        with pytest.raises(RoomError, match="no longer enabled"):
            coordinator.run_round(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"
    finally:
        coordinator.close()


def test_expired_dead_worker_lease_requires_explicit_recovery(tmp_path):
    store = RoomStore(tmp_path / "rooms")
    try:
        room = store.create_room(repo_root=tmp_path, prompt="p", agents=["claude"])
        store.start_round(
            room["id"],
            prompt="p",
            source_fingerprint="fingerprint",
            snapshot_commit="commit",
            lease_owner="99999999-dead",
            lease_timeout_seconds=-1,
            max_rounds=2,
        )
        assert store.get_room(room["id"])["state"] == "running"
        assert store.recover_expired_rounds(room["id"]) == [room["id"]]
        assert store.get_room(room["id"])["state"] == "recovery_required"
    finally:
        store.close()


def test_dead_interrupted_apply_recovers_without_manual_database_repair(
    git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("interrupted")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        coordinator.store.begin_apply(
            room["id"],
            run_id="interrupted-before-artifact",
            lease_owner="99999999-dead",
        )

        assert coordinator.store.get_room(room["id"])["state"] == "applying"
        assert coordinator.recover(room["id"]) == [room["id"]]
        recovered = coordinator.store.get_room(room["id"])
        assert recovered["state"] == "recovery_required"
        assert recovered["apply_run_id"] == "interrupted-before-artifact"
        assert recovered["applied_run_id"] is None
        event = coordinator.store.events(room["id"])[-1]
        assert event["kind"] == "apply.interrupted_recovered"
        assert event["payload"]["artifact_state"] == "missing_or_corrupt"

        coordinator.close_room(room["id"])
        replacement = coordinator.create("replacement")
        assert replacement["state"] == "draft"
    finally:
        coordinator.close()


@pytest.mark.parametrize("artifact_state", ["running", "pending", "applying"])
def test_recover_interrupted_apply_freezes_mutable_run_artifact(
    artifact_state, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("interrupted")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        run_id = f"interrupted-{artifact_state}"
        coordinator.store.begin_apply(
            room["id"], run_id=run_id, lease_owner="99999999-dead"
        )
        run_dir = (
            Path(coordinator.config.runs.root)
            / coordinator.project_id
            / "runs"
            / run_id
        )
        run_dir.mkdir(parents=True)
        run_manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "state": artifact_state,
            "repo_root": str(git_repo.resolve()),
            "task_id": room["task_id"],
            "strategy": "room",
            "source_fingerprint": candidate["source_fingerprint"],
            "snapshot_commit": candidate["base_commit"],
            "plan": {
                "origin": "room",
                "room_id": room["id"],
                "candidate_id": candidate["id"],
                "candidate_sha256": candidate["patch_sha256"],
            },
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(run_manifest), encoding="utf-8"
        )

        assert coordinator.recover(room["id"]) == [room["id"]]
        frozen = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert frozen["state"] == "recovery_required"
        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"
    finally:
        coordinator.close()


def test_recover_does_not_interrupt_a_live_apply_owner(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("live")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        coordinator.store.begin_apply(
            room["id"],
            run_id="live-run",
            lease_owner=f"{rooms_module.os.getpid()}-live",
        )

        assert coordinator.recover(room["id"]) == []
        assert coordinator.store.get_room(room["id"])["state"] == "applying"
    finally:
        coordinator.close()


def test_prune_removes_only_terminal_rooms_beyond_retention(tmp_path):
    store = RoomStore(tmp_path / "rooms")
    try:
        terminal: list[str] = []
        for prompt in ("one", "two", "three"):
            room = store.create_room(repo_root=tmp_path, prompt=prompt, agents=["claude"])
            store.close_room(room["id"])
            terminal.append(room["id"])
        active = store.create_room(repo_root=tmp_path, prompt="active", agents=["claude"])

        planned = store.prune_terminal(keep=1, retention_days=999, dry_run=True)
        assert len(planned) == 2
        assert active["id"] not in planned
        assert len(store.list_rooms()) == 4

        assert store.prune_terminal(keep=1, retention_days=999) == planned
        remaining = {room["id"] for room in store.list_rooms()}
        assert active["id"] in remaining
        assert terminal[-1] in remaining
        assert not remaining.intersection(planned)
    finally:
        store.close()


def test_prune_cleans_stale_persistent_workspace_before_metadata(
    git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("stale")
        coordinator.run_round(room["id"])
        workspace = coordinator.store.list_workspaces(room["id"])[0]
        (git_repo / "head.txt").write_text("advance\n", encoding="utf-8")
        subprocess.run(["git", "add", "head.txt"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-qm", "advance"], cwd=git_repo, check=True)
        with pytest.raises(RoomError, match="working tree changed"):
            coordinator.run_round(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "stale"

        assert coordinator.prune_terminal(keep=0, retention_days=999) == [room["id"]]
        assert not Path(workspace["run_root"]).exists()
        with pytest.raises(RoomError, match="unknown room"):
            coordinator.store.get_room(room["id"])
    finally:
        coordinator.close()


def test_detached_worker_receives_private_resolved_config(
    monkeypatch, git_repo, tmp_path
):
    config = SyncmillConfig(
        project={"id": "resolved-worker"},
        runs={"root": "relative-runs"},
        workspace={"root": "relative-workspaces"},
        rooms={"enabled": True},
    )
    captured: dict = {}
    reaped = threading.Event()

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=4242, wait=lambda: reaped.set())

    monkeypatch.setattr(rooms_module.subprocess, "Popen", fake_popen)
    pid = rooms_module.launch_room_worker(
        git_repo,
        "room-id",
        resolved_config=config,
        config_base=tmp_path,
    )
    assert pid == 4242
    argv = captured["argv"]
    snapshot = Path(argv[argv.index("--resolved-config") + 1])
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    assert payload["repo_root"] == str(git_repo.resolve())
    assert payload["runs"]["root"] == str((tmp_path / "relative-runs").resolve())
    assert payload["workspace"]["root"] == str(
        (tmp_path / "relative-workspaces").resolve()
    )
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert captured["kwargs"]["start_new_session"] is True
    assert reaped.wait(1)


def test_worker_liveness_treats_reapable_zombie_as_dead(monkeypatch):
    monkeypatch.setattr(rooms_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        rooms_module.os, "waitpid", lambda pid, _options: (pid, 0)
    )

    assert rooms_module._worker_alive("4242-worker") is False


def test_worker_liveness_keeps_running_child_alive(monkeypatch):
    monkeypatch.setattr(rooms_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        rooms_module.os, "waitpid", lambda _pid, _options: (0, 0)
    )

    assert rooms_module._worker_alive("4242-worker") is True
