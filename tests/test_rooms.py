from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeAgent
from syncmill.artifacts import ArtifactError
from syncmill.config import RoomsConfig, SyncmillConfig
from syncmill.models import TaskResult, TaskStatus
from syncmill.rooms import RoomCoordinator, RoomError, RoomStore
from syncmill.worktree import repository_fingerprint
import syncmill.artifacts as artifacts_module
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


def test_round_records_per_member_progress_and_relative_log_paths(git_repo, tmp_path):
    agents = [WritingAgent("claude"), WritingAgent("codex", status=TaskStatus.FAILED)]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("progress", ["claude", "codex"])
        coordinator.run_round(room["id"])
        candidates = coordinator.store.list_candidates(room["id"])
        round_id = candidates[0]["round_id"]
        members = {m["member_id"]: m for m in coordinator.store.list_round_members(round_id)}
        # Terminal states mirror _attempt_state, so progress and candidate agree.
        assert members["claude"]["state"] == "completed"
        assert members["codex"]["state"] == "failed"
        for member_id, member in members.items():
            # log_path is RELATIVE to the artifact root, round-isolated, and
            # resolves back under the room's own tree — never an absolute path.
            log_path = member["log_path"]
            assert not Path(log_path).is_absolute()
            assert log_path.startswith(f"{room['id']}/logs/{round_id}/")
            assert log_path.endswith(".log")
            resolved = coordinator.store.candidates.root / log_path
            assert coordinator.store.candidates.root in resolved.parents
    finally:
        coordinator.close()


def test_round_member_transitions_pass_through_queued_and_running(git_repo, tmp_path):
    # Pin the ORDER, not just the terminal state: every member must be seen
    # queued, then running, then a terminal state, and terminal is set only
    # AFTER the candidate is saved.
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    seen: list[tuple[str, str]] = []
    saved: list[str] = []
    store = coordinator.store
    real_set = store.set_member_state
    real_mark = store.mark_members_running
    real_init = store.init_round_members
    real_add = store.add_candidate

    def spy_init(room_id, round_id, members):
        for m in members:
            seen.append((m["member_id"], "queued"))
        return real_init(room_id, round_id, members)

    def spy_mark(round_id, member_ids):
        for mid in member_ids:
            seen.append((mid, "running"))
        return real_mark(round_id, member_ids)

    def spy_set(round_id, member_id, state):
        # a terminal set must come after the candidate for that member exists
        assert "claude" in saved, "terminal set before candidate saved"
        seen.append((member_id, state))
        return real_set(round_id, member_id, state)

    def spy_add(manifest):
        saved.append(manifest["member_id"])
        return real_add(manifest)

    store.init_round_members = spy_init
    store.mark_members_running = spy_mark
    store.set_member_state = spy_set
    store.add_candidate = spy_add
    try:
        room = coordinator.create("order")
        coordinator.run_round(room["id"])
        assert seen == [("claude", "queued"), ("claude", "running"), ("claude", "completed")]
    finally:
        coordinator.close()


def test_round_reconciles_members_to_interrupted_on_failure(git_repo, tmp_path):
    # If candidate persistence fails after the fan-out, the member is left
    # non-terminal and the recovery boundary settles it to `interrupted`,
    # never leaving it "running" forever.
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    round_ids: list[str] = []

    def boom(manifest):
        round_ids.append(manifest["round_id"])
        raise RoomError("scripted candidate-save failure")

    coordinator.store.add_candidate = boom
    try:
        room = coordinator.create("crash")
        with pytest.raises(RoomError, match="scripted candidate-save failure"):
            coordinator.run_round(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"
        members = coordinator.store.list_round_members(round_ids[0])
        assert [m["state"] for m in members] == ["interrupted"]
    finally:
        coordinator.close()


def test_round_terminal_set_failure_retains_candidate_and_recovers(git_repo, tmp_path):
    # If the terminal progress set fails AFTER the candidate is durably saved,
    # the candidate must survive and the room enters recovery_required with the
    # member reconciled — the saved candidate is never lost to a bookkeeping slip.
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    round_ids: list[str] = []
    real_set = coordinator.store.set_member_state

    def fail_terminal(round_id, member_id, state):
        if state == "completed":
            round_ids.append(round_id)
            raise RoomError("scripted terminal-set failure")
        return real_set(round_id, member_id, state)

    coordinator.store.set_member_state = fail_terminal
    try:
        room = coordinator.create("post-save crash")
        with pytest.raises(RoomError, match="scripted terminal-set failure"):
            coordinator.run_round(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"
        # The candidate saved before the failing set is retained and durable.
        candidates = coordinator.store.list_candidates(room["id"])
        assert len(candidates) == 1
        assert coordinator.store.load_candidate(candidates[0]["id"])["member_id"] == "claude"
        # The member, left running by the failed set, is reconciled.
        members = coordinator.store.list_round_members(round_ids[0])
        assert [m["state"] for m in members] == ["interrupted"]
    finally:
        coordinator.close()


def test_recover_expired_rounds_reconciles_members(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        room = coordinator.create("stale")
        # Seed a running round whose lease has expired under a dead worker.
        round_row = coordinator.store.start_round(
            room["id"],
            prompt="p",
            source_fingerprint=repository_fingerprint(git_repo),
            snapshot_commit="c",
            lease_owner="0-deadbeef",  # a pid that is not alive
            lease_timeout_seconds=-1,  # already expired
            max_rounds=8,
        )
        coordinator.store.init_round_members(
            room["id"], round_row["id"], [{"member_id": "claude", "provider_id": "claude"}]
        )
        coordinator.store.mark_members_running(round_row["id"], ["claude"])
        recovered = coordinator.store.recover_expired_rounds(room["id"])
        assert room["id"] in recovered
        members = coordinator.store.list_round_members(round_row["id"])
        assert [m["state"] for m in members] == ["interrupted"]
    finally:
        coordinator.close()


def test_round_members_table_added_to_legacy_db(git_repo, tmp_path):
    # Simulate a DB that predates round_members: build the real schema, run a
    # real round, then drop the table (as if it had never existed) and reopen.
    # It must be recreated on open, and the pre-existing round reads as no
    # members (the viewer's "terminal unavailable" signal).
    from syncmill.rooms import RoomStore

    config = _config(tmp_path)
    coordinator = RoomCoordinator(config, git_repo, agents=[WritingAgent("claude")])
    room = coordinator.create("legacy")
    coordinator.run_round(room["id"])
    round_id = coordinator.store.list_candidates(room["id"])[0]["round_id"]
    root = coordinator.store.root
    coordinator.store._conn.execute("DROP TABLE round_members")
    coordinator.close()

    store = RoomStore(root, output_max_bytes=config.rooms.output_max_bytes)
    try:
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(round_members)")}
        assert {"round_id", "member_id", "state", "log_path"} <= cols
        assert store.list_round_members(round_id) == []
    finally:
        store.close()


def test_round_member_state_rejects_unknown_state(git_repo, tmp_path):
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        with pytest.raises(RoomError, match="invalid round-member state"):
            coordinator.store.set_member_state("r", "claude", "bogus")
    finally:
        coordinator.close()


def test_stale_transition_reclaims_viewers(git_repo, tmp_path, monkeypatch):
    # A room going stale (working tree changed) is terminal, so its prior-round
    # viewers are reclaimed at the transition, not left for a later sweep.
    import syncmill.room_viewer as V

    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    reclaimed: list[str] = []
    monkeypatch.setattr(
        V, "close_room_viewers",
        lambda project_id, room_id: reclaimed.append(room_id),
    )
    try:
        room = coordinator.create("stale me")
        coordinator.run_round(room["id"])
        # Mutate the working tree so the next round detects drift → stale.
        (git_repo / "drift.txt").write_text("changed", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "drift"], cwd=git_repo, check=True)
        with pytest.raises(RoomError):
            coordinator.run_round(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "stale"
        assert room["id"] in reclaimed
    finally:
        coordinator.close()


def test_close_room_reclaims_viewers_at_lifecycle_boundary(git_repo, tmp_path, monkeypatch):
    # Teardown is coupled to the terminal transition in the coordinator (not the
    # CLI), so programmatic close reclaims viewers too.
    import syncmill.room_viewer as V

    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    reclaimed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        V, "close_room_viewers",
        lambda project_id, room_id: reclaimed.append((project_id, room_id)),
    )
    try:
        room = coordinator.create("close me")
        coordinator.run_round(room["id"])
        coordinator.select(room["id"], coordinator.store.list_candidates(room["id"])[0]["id"])
        coordinator.close_room(room["id"])
        assert reclaimed == [(coordinator.project_id, room["id"])]
    finally:
        coordinator.close()


def test_member_state_updates_raise_on_missing_progress_row(git_repo, tmp_path):
    # A missing progress row is an inconsistency, surfaced rather than silently
    # advancing the round through candidate publication.
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[WritingAgent("claude")])
    try:
        with pytest.raises(RoomError, match="no progress row for member"):
            coordinator.store.set_member_state("missing-round", "claude", "completed")
        with pytest.raises(RoomError, match="expected 1 member rows, updated 0"):
            coordinator.store.mark_members_running("missing-round", ["claude"])
    finally:
        coordinator.close()


def test_round_log_sink_is_bounded_and_rotates(tmp_path):
    from syncmill.rooms import RoundLogSink, _ROUND_LOG_SEGMENT_BYTES

    sink = RoundLogSink(tmp_path / "logs")
    try:
        active = sink.path_for("claude")
        rotated = active.with_suffix(active.suffix + ".1")
        # Under the cap: one segment, no rotation.
        sink.feed("claude", b"a" * 1024)
        assert active.exists() and not rotated.exists()
        # Cross the cap: the active segment rolls to .1 and a fresh one starts.
        sink.feed("claude", b"b" * _ROUND_LOG_SEGMENT_BYTES)
        assert rotated.exists()
        assert active.stat().st_size <= _ROUND_LOG_SEGMENT_BYTES
        # Total retained is bounded to two segments regardless of volume.
        for _ in range(4):
            sink.feed("claude", b"c" * _ROUND_LOG_SEGMENT_BYTES)
        assert active.stat().st_size <= _ROUND_LOG_SEGMENT_BYTES
        assert rotated.stat().st_size <= _ROUND_LOG_SEGMENT_BYTES
    finally:
        sink.close()


def test_round_log_sink_sanitizes_member_id(tmp_path):
    from syncmill.rooms import RoundLogSink

    sink = RoundLogSink(tmp_path / "logs")
    try:
        # A traversal-shaped id must not escape the log directory.
        path = sink.path_for("../../etc/passwd")
        assert path.parent == (tmp_path / "logs")
        sink.feed("../../etc/passwd", b"x")
        assert path.exists()
    finally:
        sink.close()


def test_round_log_filename_is_injective_and_bounded(tmp_path):
    from syncmill.rooms import RoundLogSink

    sink = RoundLogSink(tmp_path / "logs")
    try:
        # The collision pair that a pure substitution collapsed onto one file.
        assert sink.path_for("a/b") != sink.path_for("a_b")
        # A very long id cannot produce an over-length filename.
        long_id = "x" * 5000
        assert len(sink.path_for(long_id).name) < 255
        # Two long ids sharing a prefix still resolve to distinct files.
        assert sink.path_for("x" * 5000) != sink.path_for("x" * 4999 + "y")
    finally:
        sink.close()


def test_round_log_sink_stays_bounded_when_rotation_fails(tmp_path, monkeypatch):
    from syncmill.rooms import RoundLogSink, _ROUND_LOG_SEGMENT_BYTES
    import syncmill.rooms as rooms_mod

    sink = RoundLogSink(tmp_path / "logs")
    try:
        active = sink.path_for("claude")
        sink.feed("claude", b"a" * _ROUND_LOG_SEGMENT_BYTES)
        # Rotation can no longer roll the full segment aside.
        monkeypatch.setattr(
            rooms_mod.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        )
        for _ in range(8):
            sink.feed("claude", b"b" * _ROUND_LOG_SEGMENT_BYTES)
        # The active segment never grows past the cap despite repeated failure.
        assert active.stat().st_size <= _ROUND_LOG_SEGMENT_BYTES
        # A transient failure recovers: once rotation works again, the next feed
        # rolls the full segment aside and writes into a fresh one.
        monkeypatch.undo()
        sink.feed("claude", b"c" * 1024)
        rotated = active.with_suffix(active.suffix + ".1")
        assert rotated.exists()
        assert active.stat().st_size <= _ROUND_LOG_SEGMENT_BYTES
    finally:
        sink.close()


def test_round_log_sink_tolerates_short_and_zero_writes(tmp_path, monkeypatch):
    from syncmill.rooms import RoundLogSink
    import syncmill.rooms as rooms_mod

    sink = RoundLogSink(tmp_path / "logs")
    try:
        # os.write reporting a short count must not drift the tracked size.
        monkeypatch.setattr(rooms_mod.os, "write", lambda fd, data: len(data) // 2)
        sink.feed("claude", b"a" * 100)
        assert sink._written["claude"] == 50
        # A zero-byte write leaves the accounting untouched and never raises.
        monkeypatch.setattr(rooms_mod.os, "write", lambda fd, data: 0)
        sink.feed("claude", b"b" * 100)
        assert sink._written["claude"] == 50
    finally:
        sink.close()


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


def test_completed_no_change_candidate_is_preserved_but_not_eligible(
    git_repo, tmp_path
):
    agent = FakeAgent("claude", output="review-only result")
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[agent])
    try:
        room = coordinator.create("review without changes")
        coordinator.run_round(room["id"])
        row = coordinator.store.list_candidates(room["id"])[0]
        artifact = coordinator.store.load_candidate(row["id"])

        assert row["eligible"] == 0
        assert artifact["state"] == "completed"
        assert artifact["eligible"] is False
        assert artifact["patch"] == b""
        assert artifact["files_changed"] == []
        assert artifact["output"] == "review-only result"
        with pytest.raises(RoomError, match="not eligible"):
            coordinator.select(room["id"], row["id"])
    finally:
        coordinator.close()


def test_completed_patch_capture_failure_is_preserved_but_not_eligible(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    from syncmill.worktree import PatchCaptureError

    def fail(*args):
        raise PatchCaptureError("injected patch failure")

    monkeypatch.setattr(rooms_module, "capture_changes", fail)
    try:
        room = coordinator.create("capture fails")
        with pytest.raises(PatchCaptureError, match="injected patch failure"):
            coordinator.run_round(room["id"])
        assert coordinator.store.list_candidates(room["id"]) == []
        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"
        assert list(Path(coordinator.config.workspace.root).glob("room-*/worktrees/*/claude.txt"))
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


def test_apply_rejects_room_recorded_for_another_checkout(git_repo, tmp_path):
    config = _config(tmp_path)
    coordinator = RoomCoordinator(
        config, git_repo, agents=[WritingAgent("claude")]
    )
    other_repo = tmp_path / "other-checkout"
    subprocess.run(
        ["git", "clone", "-q", str(git_repo), str(other_repo)], check=True
    )
    other = RoomCoordinator(
        config,
        other_repo,
        store=coordinator.store,
        agents=[WritingAgent("claude")],
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])

        with pytest.raises(RoomError, match="different repository"):
            other.preflight_round(room["id"])
        with pytest.raises(RoomError, match="different repository"):
            other.apply(room["id"])

        assert coordinator.store.get_room(room["id"])["state"] == "selected"
        assert not (other_repo / "claude.txt").exists()
    finally:
        other.close()
        coordinator.close()


def test_round_preflight_rejects_missing_and_ineligible_branch_candidates(
    git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[FakeAgent("claude")]
    )
    try:
        room = coordinator.create("review only")
        with pytest.raises(RoomError, match="unknown candidate"):
            coordinator.preflight_round(
                room["id"],
                lineage="branch-from",
                base_candidate_id="missing",
            )

        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        assert candidate["eligible"] == 0
        with pytest.raises(RoomError, match="base candidate must be eligible"):
            coordinator.preflight_round(
                room["id"],
                lineage="branch-from",
                base_candidate_id=candidate["id"],
            )
    finally:
        coordinator.close()


def test_round_preflight_rejects_nonrunnable_room_state(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])

        with pytest.raises(RoomError, match="cannot start a round from selected"):
            coordinator.preflight_round(room["id"])
    finally:
        coordinator.close()


def test_apply_uses_selected_candidates_round_prompt(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("original prompt")
        coordinator.run_round(room["id"])
        coordinator.run_round(room["id"], "replacement prompt")
        candidate = coordinator.store.list_candidates(room["id"])[-1]
        coordinator.select(room["id"], candidate["id"])

        run_id, _ = coordinator.apply(room["id"])

        manifest_path = (
            Path(coordinator.config.runs.root)
            / coordinator.project_id
            / "runs"
            / run_id
            / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["prompt"] == "replacement prompt"
    finally:
        coordinator.close()


def test_candidate_patch_head_is_bounded_and_digest_verified(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        full = coordinator.store.load_candidate(candidate["id"])
        head = coordinator.store.load_candidate_patch_head(candidate["id"], 1_000_000)
        assert head["patch_head"] == full["patch"]
        assert head["patch_truncated"] is False
        window = coordinator.store.load_candidate_patch_head(candidate["id"], 4)
        assert window["patch_head"] == full["patch"][:4]
        assert window["patch_truncated"] is True
        # The digest covers the WHOLE file even for a truncated window.
        path = Path(candidate["artifact_path"]) / "candidate.patch"
        path.write_bytes(path.read_bytes() + b"tamper")
        with pytest.raises(RoomError, match="patch digest mismatch"):
            coordinator.store.load_candidate_patch_head(candidate["id"], 4)
    finally:
        coordinator.close()


def _rebind_candidate_patch(store, candidate, patch: bytes) -> None:
    """Rewrite a candidate's patch AND rebind both digest copies (manifest +
    SQLite row), so loader tests can exercise arbitrary patch content without
    tripping the tamper guards."""
    directory = Path(candidate["artifact_path"])
    (directory / "candidate.patch").write_bytes(patch)
    digest = hashlib.sha256(patch).hexdigest()
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["patch_sha256"] = digest
    manifest["patch_bytes"] = len(patch)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    store._conn.execute(
        "UPDATE candidates SET patch_sha256=? WHERE id=?", (digest, candidate["id"])
    )


def test_candidate_patch_binary_detection_covers_the_whole_stream(
    git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        # Marker STRADDLING the 64 KiB chunk boundary, far beyond the head
        # window — the carry must still catch it.
        prefix = b"diff --git a/x b/x\n"
        marker = b"GIT binary patch"
        patch = prefix + b"a" * (64 * 1024 - len(prefix) - 8) + marker + b"\nrest\n"
        _rebind_candidate_patch(coordinator.store, candidate, patch)
        head = coordinator.store.load_candidate_patch_head(candidate["id"], 16)
        assert head["patch_truncated"] is True
        assert head["patch_binary"] is True
        # A NUL byte anywhere also counts as binary.
        _rebind_candidate_patch(
            coordinator.store, candidate, b"diff --git a/x b/x\n+data \x00 blob\n"
        )
        nul = coordinator.store.load_candidate_patch_head(candidate["id"], 8)
        assert nul["patch_binary"] is True
        # A patch that STARTS with a plain-binary marker (no leading newline).
        _rebind_candidate_patch(
            coordinator.store, candidate, b"Binary files a/x and b/x differ\n"
        )
        start = coordinator.store.load_candidate_patch_head(candidate["id"], 8)
        assert start["patch_binary"] is True
    finally:
        coordinator.close()


def test_candidate_loaders_reject_non_object_manifests(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        manifest_path = Path(candidate["artifact_path"]) / "manifest.json"
        manifest_path.write_text("[]", encoding="utf-8")
        # Valid JSON of the wrong shape stays inside the RoomError boundary
        # (never an AttributeError escaping into the caller).
        with pytest.raises(RoomError, match="corrupt candidate artifact"):
            coordinator.store.load_candidate_patch_head(candidate["id"], 8)
        with pytest.raises(RoomError, match="corrupt candidate artifact"):
            coordinator.store.load_candidate_checkpoint(candidate["id"], 8)
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


def test_candidate_output_digest_is_bound_to_database(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("preserve evidence")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        directory = Path(candidate["artifact_path"])
        forged = b"forged provider evidence\n"
        (directory / "output.txt").write_bytes(forged)
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["output_sha256"] = hashlib.sha256(forged).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(RoomError, match="output digest binding mismatch"):
            coordinator.store.load_candidate(candidate["id"])
        with pytest.raises(RoomError, match="output digest binding mismatch"):
            coordinator.store.load_candidate_checkpoint(candidate["id"], 4000)
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


def test_artifact_store_creation_failure_returns_room_to_selected(
    git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("implement it")
        coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        occupied_runs_root = tmp_path / "occupied-runs-root"
        occupied_runs_root.write_text("not a directory\n", encoding="utf-8")
        coordinator.config.runs.root = str(occupied_runs_root)

        with pytest.raises(OSError):
            coordinator.apply(room["id"])

        recovered = coordinator.store.get_room(room["id"])
        assert recovered["state"] == "selected"
        assert recovered["apply_run_id"] is None
        assert recovered["apply_owner"] is None
        assert coordinator.store.events(room["id"])[-1]["kind"] == "apply.failed"
    finally:
        coordinator.close()


def test_artifact_store_creation_failure_marks_recovery_if_rollback_fails(
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
        occupied_runs_root = tmp_path / "occupied-runs-root"
        occupied_runs_root.write_text("not a directory\n", encoding="utf-8")
        coordinator.config.runs.root = str(occupied_runs_root)

        def fail_rollback(*_args, **_kwargs):
            raise RoomError("injected apply rollback CAS failure")

        monkeypatch.setattr(coordinator.store, "apply_failed", fail_rollback)
        with pytest.raises(RoomError, match="room marked recovery_required"):
            coordinator.apply(room["id"])

        recovered = coordinator.store.get_room(room["id"])
        assert recovered["state"] == "recovery_required"
        assert recovered["apply_run_id"] is not None
        assert recovered["apply_owner"] is None
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


def test_continue_own_rejects_symlinked_retained_workspace_before_provider(
    git_repo, tmp_path
):
    agent = WritingAgent("claude")
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=[agent])
    room = coordinator.create("first")
    coordinator.run_round(room["id"])
    workspace = coordinator.store.list_workspaces(room["id"])[0]
    worktree_path = Path(workspace["path"])
    shutil.rmtree(worktree_path)
    worktree_path.symlink_to(git_repo, target_is_directory=True)
    try:
        with pytest.raises(RoomError, match="unsafe paths or symlinks"):
            coordinator.run_round(room["id"], "must stay isolated")
        assert len(agent.calls) == 1
        assert not (git_repo / "claude.txt").exists()
        assert coordinator.store.get_room(room["id"])["state"] == "awaiting_decision"
    finally:
        worktree_path.unlink(missing_ok=True)
        coordinator.close_room(room["id"])
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


def test_room_close_claim_blocks_new_workspace_reservation(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    room = coordinator.create("close atomically")
    planned = Path(coordinator.config.workspace.root) / f"room-{room['id']}-1"

    def guarded_cleanup(room_id: str) -> None:
        assert coordinator.store.get_room(room_id)["state"] == "closing"
        with pytest.raises(RoomError, match="cannot prepare a workspace"):
            coordinator.store.reserve_workspace(
                room_id, planned, f"{rooms_module.os.getpid()}-worker"
            )

    monkeypatch.setattr(coordinator, "_cleanup_workspaces", guarded_cleanup)
    try:
        coordinator.close_room(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "closed"
    finally:
        coordinator.close()


def test_room_close_refuses_live_workspace_preparation(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    room = coordinator.create("do not delete live workspace")
    planned = Path(coordinator.config.workspace.root) / f"room-{room['id']}-1"
    planned.mkdir(parents=True)
    sentinel = planned / "live.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    coordinator.store.reserve_workspace(
        room["id"], planned, f"{rooms_module.os.getpid()}-worker"
    )
    try:
        with pytest.raises(RoomError, match="workspace preparation is owned"):
            coordinator.close_room(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "draft"
        assert sentinel.read_text(encoding="utf-8") == "keep\n"

        coordinator.store.recovery_required(room["id"], "test cleanup", planned)
        coordinator.close_room(room["id"])
        assert not planned.exists()
    finally:
        coordinator.close()


def test_recover_interrupted_room_close(monkeypatch, git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    room = coordinator.create("recover close")
    coordinator.store.begin_close(room["id"], "99999999-dead")
    monkeypatch.setattr(rooms_module, "_worker_alive", lambda _owner: False)
    try:
        assert coordinator.recover(room["id"]) == [room["id"]]
        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"
        coordinator.close_room(room["id"])
        assert coordinator.store.get_room(room["id"])["state"] == "closed"
    finally:
        coordinator.close()


def test_room_cleanup_removes_validated_recovery_path_without_workspace_rows(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )

    def fail_workspace_persistence(*_args, **_kwargs):
        raise RoomError("injected workspace persistence failure")

    monkeypatch.setattr(
        coordinator.store, "replace_workspaces", fail_workspace_persistence
    )
    try:
        room = coordinator.create("retain private clone")
        with pytest.raises(RoomError, match="workspace persistence failure"):
            coordinator.run_round(room["id"])

        recovery = coordinator.store.get_room(room["id"])
        recovery_path = Path(recovery["recovery_path"])
        assert recovery["state"] == "recovery_required"
        assert recovery_path.exists()
        assert coordinator.store.list_workspaces(room["id"]) == []

        coordinator.close_room(room["id"])
        closed = coordinator.store.get_room(room["id"])
        assert closed["state"] == "closed"
        assert closed["recovery_path"] is None
        assert not recovery_path.exists()
    finally:
        coordinator.close()


def test_killed_snapshot_creator_keeps_recoverable_workspace_ownership(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    original_capture = rooms_module.capture_repository_snapshot

    def interrupted_after_snapshot(*args, **kwargs):
        original_capture(*args, **kwargs)
        raise KeyboardInterrupt("simulated process termination")

    monkeypatch.setattr(
        rooms_module, "capture_repository_snapshot", interrupted_after_snapshot
    )
    try:
        room = coordinator.create("retain snapshot ownership")
        with pytest.raises(KeyboardInterrupt, match="process termination"):
            coordinator.run_round(room["id"])

        interrupted = coordinator.store.get_room(room["id"])
        recovery_path = Path(interrupted["recovery_path"])
        assert interrupted["state"] == "draft"
        assert interrupted["workspace_owner"]
        assert recovery_path.exists()

        monkeypatch.setattr(rooms_module, "_worker_alive", lambda _owner: False)
        assert coordinator.recover(room["id"]) == [room["id"]]
        assert coordinator.store.get_room(room["id"])["state"] == "recovery_required"

        coordinator.close_room(room["id"])
        assert not recovery_path.exists()
    finally:
        coordinator.close()


def test_room_cleanup_rejects_untrusted_recovery_path(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("unsafe recovery path")
        coordinator.run_round(room["id"])
        workspace = coordinator.store.list_workspaces(room["id"])[0]
        worktree_path = Path(workspace["path"])
        run_root = Path(workspace["run_root"])
        assert worktree_path.exists()
        unrelated = tmp_path / "must-survive"
        unrelated.mkdir()
        (unrelated / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        coordinator.store.recovery_required(room["id"], "injected", unrelated)

        with pytest.raises(RoomError, match="unsafe paths"):
            coordinator.close_room(room["id"])
        current = coordinator.store.get_room(room["id"])
        assert current["state"] == "recovery_required"
        assert current["recovery_path"] == str(unrelated)
        assert worktree_path.exists()
        assert run_root.exists()
        assert (unrelated / "sentinel.txt").read_text(encoding="utf-8") == "keep\n"
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


def test_prune_uses_exact_ids_cleaned_before_terminal_set_changes(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    old = coordinator.create("old")
    coordinator.close_room(old["id"])
    keep = coordinator.create("keep")
    coordinator.close_room(keep["id"])
    active = coordinator.create("becomes terminal during prune")
    coordinator.store._conn.execute(
        "UPDATE rooms SET updated_at='2026-01-01T00:00:00+00:00' WHERE id=?",
        (old["id"],),
    )
    coordinator.store._conn.execute(
        "UPDATE rooms SET updated_at='2026-02-01T00:00:00+00:00' WHERE id=?",
        (keep["id"],),
    )
    original_cleanup = coordinator._cleanup_workspaces
    changed = False

    def close_active_during_cleanup(room_id: str) -> None:
        nonlocal changed
        if not changed:
            changed = True
            coordinator.store.close_room(active["id"])
        original_cleanup(room_id)

    monkeypatch.setattr(coordinator, "_cleanup_workspaces", close_active_during_cleanup)
    try:
        assert coordinator.prune_terminal(keep=1, retention_days=999) == [old["id"]]
        remaining = {value["id"] for value in coordinator.store.list_rooms()}
        assert remaining == {keep["id"], active["id"]}
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


def test_prune_skips_ineligible_room_without_touching_its_artifacts(
    monkeypatch, tmp_path
):
    # Eligibility is checked BEFORE the quarantine move: an active room's
    # candidate artifacts must never vanish (even transiently) because a prune
    # considered and rejected it — a concurrent reader would see a missing
    # artifact during the restore window.
    store = RoomStore(tmp_path / "rooms")
    try:
        active = store.create_room(repo_root=tmp_path, prompt="live", agents=["claude"])
        artifact_dir = store.candidates.root / active["id"]
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "manifest.json").write_text("{}", encoding="utf-8")
        moves: list = []
        original_replace = rooms_module.os.replace

        def recording_replace(src, dst):
            moves.append((src, dst))
            return original_replace(src, dst)

        monkeypatch.setattr(rooms_module.os, "replace", recording_replace)
        assert store.prune_rooms([active["id"]]) == []
        assert moves == []
        assert (artifact_dir / "manifest.json").exists()
    finally:
        store.close()


def test_prune_terminal_tolerates_a_malformed_updated_at_row(tmp_path):
    # One corrupt updated_at must not abort the whole prune; the malformed row
    # itself stays (no parseable age → only the count bound can claim it).
    store = RoomStore(tmp_path / "rooms")
    try:
        old = store.create_room(repo_root=tmp_path, prompt="old", agents=["claude"])
        store.close_room(old["id"])
        broken = store.create_room(repo_root=tmp_path, prompt="broken", agents=["claude"])
        store.close_room(broken["id"])
        store._conn.execute(
            "UPDATE rooms SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (old["id"],),
        )
        store._conn.execute(
            "UPDATE rooms SET updated_at='not-a-timestamp' WHERE id=?",
            (broken["id"],),
        )
        assert store.prune_terminal(keep=10, retention_days=1, dry_run=True) == [
            old["id"]
        ]
    finally:
        store.close()


def test_worker_launch_keeps_prompt_off_argv_and_id_after_separator(
    monkeypatch, git_repo
):
    # The prompt travels over an anonymous inherited fd — never argv (visible
    # in `ps`) and never a named file (a child killed before consuming a file
    # would strand the plaintext). The positional room id rides after `--`
    # with every option before it — the build_job_argv contract.
    captured: dict = {}
    reaped = threading.Event()

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        fd = int(argv[argv.index("--prompt-fd") + 1])
        captured["prompt"] = os.read(fd, 200_000)  # read it like the child would
        return SimpleNamespace(pid=4242, wait=lambda: reaped.set())

    monkeypatch.setattr(rooms_module.subprocess, "Popen", fake_popen)
    rooms_module.launch_room_worker(git_repo, "room-id", "secret prompt")
    argv = captured["argv"]
    assert "secret prompt" not in argv
    assert argv[-2:] == ["--", "room-id"]
    fd = int(argv[argv.index("--prompt-fd") + 1])
    assert captured["prompt"] == b"secret prompt"
    assert captured["kwargs"]["pass_fds"] == (fd,)
    # Anonymous by construction: the temp file was unlinked while still open,
    # so no plaintext path exists for ANY failure mode to strand.
    assert not list(Path(tempfile.gettempdir()).glob("syncmill-room-prompt-*"))
    assert reaped.wait(1)


def test_prompt_file_is_anonymous_before_any_plaintext_lands(monkeypatch, git_repo):
    # A launcher SIGKILLed between the write and a later unlink would strand a
    # named 0600 file holding the plaintext — the unlink must come FIRST, so
    # the fd is already anonymous when the first prompt byte is written.
    state: dict = {}
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, tmp = real_mkstemp(*args, **kwargs)
        state["tmp"] = tmp
        return fd, tmp

    monkeypatch.setattr(rooms_module.tempfile, "mkstemp", spy_mkstemp)
    real_write = os.write

    def spy_write(fd, data):
        state.setdefault("named_at_first_write", os.path.exists(state["tmp"]))
        return real_write(fd, data)

    monkeypatch.setattr(rooms_module.os, "write", spy_write)
    reaped = threading.Event()
    monkeypatch.setattr(
        rooms_module.subprocess,
        "Popen",
        lambda argv, **kwargs: SimpleNamespace(pid=4242, wait=lambda: reaped.set()),
    )
    rooms_module.launch_room_worker(git_repo, "room-id", "secret prompt")
    assert state["named_at_first_write"] is False
    assert reaped.wait(1)


def test_prompt_staging_failure_cleans_the_config_snapshot(
    monkeypatch, git_repo, tmp_path
):
    # Staging the prompt fd can fail BEFORE the spawn (temp volume full); the
    # config snapshot written just above must not linger in worker-configs —
    # its ordinary cleanup only covers the Popen failure path.
    config = SyncmillConfig(
        project={"id": "stage-fail"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        rooms={"enabled": True},
    )
    spawned: list = []
    monkeypatch.setattr(
        rooms_module.subprocess, "Popen", lambda *a, **k: spawned.append(1)
    )

    def boom(*args, **kwargs):
        raise OSError("temp volume full")

    monkeypatch.setattr(rooms_module.tempfile, "mkstemp", boom)
    with pytest.raises(OSError, match="temp volume full"):
        rooms_module.launch_room_worker(
            git_repo,
            "room-id",
            "prompt",
            resolved_config=config,
            config_base=tmp_path,
        )
    assert spawned == []
    assert not list((tmp_path / "runs").rglob("*.json"))


def test_worker_alive_probe_is_shared_with_the_artifact_reconciler():
    # The liveness parser moved into ``artifacts`` so the standalone apply
    # reconciler and the room reconciler share one implementation; rooms
    # re-exports the same object so its call sites (and their monkeypatches)
    # still resolve it.
    assert rooms_module._worker_alive is artifacts_module._worker_alive


def test_worker_liveness_treats_reapable_zombie_as_dead(monkeypatch):
    monkeypatch.setattr(artifacts_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        artifacts_module.os, "waitpid", lambda pid, _options: (pid, 0)
    )

    assert artifacts_module._worker_alive("4242-worker") is False


def test_worker_liveness_keeps_running_child_alive(monkeypatch):
    monkeypatch.setattr(artifacts_module.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        artifacts_module.os, "waitpid", lambda _pid, _options: (0, 0)
    )

    assert artifacts_module._worker_alive("4242-worker") is True


@pytest.mark.parametrize("alive", [True, False])
def test_worker_liveness_uses_non_destructive_windows_probe(monkeypatch, alive):
    # Deliberately omit kill/waitpid/WNOHANG: accessing any POSIX probe through
    # this simulated Windows module would fail the test.
    monkeypatch.setattr(artifacts_module, "os", SimpleNamespace(name="nt"))
    observed: list[int] = []

    def probe(pid: int) -> bool:
        observed.append(pid)
        return alive

    monkeypatch.setattr(artifacts_module, "_windows_worker_alive", probe)

    assert artifacts_module._worker_alive("4242-worker") is alive
    assert observed == [4242]


class _FakeWindowsCall:
    def __init__(self, result):
        self.result = result
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


@pytest.mark.parametrize(
    ("wait_result", "alive"),
    [(0x00000102, True), (0, False), (0xFFFFFFFF, True)],
)
def test_windows_worker_probe_distinguishes_running_exited_and_unknown(
    monkeypatch, wait_result, alive
):
    open_process = _FakeWindowsCall(123)
    wait_for_single_object = _FakeWindowsCall(wait_result)
    close_handle = _FakeWindowsCall(True)
    kernel32 = SimpleNamespace(
        OpenProcess=open_process,
        WaitForSingleObject=wait_for_single_object,
        CloseHandle=close_handle,
    )
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False
    )
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)

    assert artifacts_module._windows_worker_alive(4242) is alive
    assert wait_for_single_object.calls == [(123, 0)]
    assert close_handle.calls == [(123,)]


@pytest.mark.parametrize(
    ("last_error", "alive"),
    [(87, False), (5, True), (1234, True)],
)
def test_windows_worker_probe_fails_closed_for_uncertain_open_errors(
    monkeypatch, last_error, alive
):
    kernel32 = SimpleNamespace(
        OpenProcess=_FakeWindowsCall(0),
        WaitForSingleObject=_FakeWindowsCall(0),
        CloseHandle=_FakeWindowsCall(True),
    )
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False
    )
    monkeypatch.setattr(
        ctypes, "get_last_error", lambda: last_error, raising=False
    )

    assert artifacts_module._windows_worker_alive(4242) is alive


# ── provider-session continuity across rounds ──────────────────────────────


class SessionAgent(WritingAgent):
    """WritingAgent that observes the ambient session scope at execution time
    and mints a distinct provider session id per call — the room-level stand-in
    for a real CLI capturing `--resume`/`exec resume` session ids."""

    def __init__(self, agent_id: str, **kwargs):
        super().__init__(agent_id, **kwargs)
        self.seen_scopes: list[dict] = []

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        from syncmill.agents import _PROVIDER_SESSIONS, _SINK_LABEL

        state = _PROVIDER_SESSIONS.get()
        self.seen_scopes.append(
            {
                "seed": dict(state["seed"]) if state else None,
                "fork": state["fork"] if state else None,
                "label": _SINK_LABEL.get(),
                "prompt": prompt,
            }
        )
        result = await super().aexecute(prompt, workdir, timeout)
        result.provider_session_id = f"{self.agent_id}-sid-{len(self.seen_scopes)}"
        return result


def test_continue_own_round_seeds_member_sessions_and_persists(git_repo, tmp_path):
    agents = [SessionAgent("claude"), SessionAgent("codex")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude", "codex"])
        coordinator.run_round(room["id"], "round one")
        # Round 1: cold — no seed; the minted sids persist under MEMBER ids.
        assert agents[0].seen_scopes[0]["seed"] == {}
        stored = coordinator.store.member_sessions(room["id"])
        assert stored == {"claude": "claude-sid-1", "codex": "codex-sid-1"}

        coordinator.run_round(room["id"], "round two")
        # Round 2 (continue-own): seeded by member id; the sink label carries
        # the member id so the seed lookup and log stream are member-scoped.
        scope = agents[0].seen_scopes[1]
        assert scope["seed"] == {"claude": "claude-sid-1", "codex": "codex-sid-1"}
        assert scope["label"] == "claude"
        assert scope["fork"] is False
        assert coordinator.store.member_sessions(room["id"]) == {
            "claude": "claude-sid-2", "codex": "codex-sid-2",
        }
    finally:
        coordinator.close()


def test_session_ids_persist_even_when_candidate_save_fails(git_repo, tmp_path):
    # Gap 2: the provider turn happened even if the round later fails — its
    # session id must be durably recorded BEFORE the gate/candidate loop.
    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])

        def boom(_manifest):
            raise RoomError("candidate save exploded")

        original = coordinator.store.add_candidate
        coordinator.store.add_candidate = boom
        try:
            with pytest.raises(RoomError, match="candidate save exploded"):
                coordinator.run_round(room["id"], "round one")
        finally:
            coordinator.store.add_candidate = original
        assert coordinator.store.member_sessions(room["id"]) == {
            "claude": "claude-sid-1"
        }
    finally:
        coordinator.close()


def test_sessions_survive_workspace_cleanup_and_recovery(git_repo, tmp_path):
    # Gap 3: recovery deletes the workspace rows; the rooms.sessions_json copy
    # keeps the ids so the post-recovery round still resumes.
    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        coordinator.store.clear_workspaces(room["id"])
        assert coordinator.store.list_workspaces(room["id"]) == []
        assert coordinator.store.member_sessions(room["id"]) == {
            "claude": "claude-sid-1"
        }
        coordinator.run_round(room["id"], "round two")
        assert agents[0].seen_scopes[1]["seed"] == {"claude": "claude-sid-1"}
    finally:
        coordinator.close()


def test_fresh_lineage_is_cold_but_branch_from_keeps_sessions(git_repo, tmp_path):
    agents = [SessionAgent("claude"), SessionAgent("codex")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude", "codex"])
        coordinator.run_round(room["id"], "round one")
        base = next(
            candidate
            for candidate in coordinator.store.list_candidates(room["id"])
            if candidate["member_id"] == "claude"
        )

        # branch-from: new tree, SAME conversations — sessions seed through.
        coordinator.run_round(
            room["id"], "round two", lineage="branch-from",
            base_candidate_id=base["id"],
        )
        assert agents[0].seen_scopes[1]["seed"] == {
            "claude": "claude-sid-1", "codex": "codex-sid-1",
        }
        # ...and the replaced workspace rows carry the seeded ids forward.
        rows = {
            row["member_id"]: row["provider_session_id"]
            for row in coordinator.store.list_workspaces(room["id"])
        }
        assert rows == {"claude": "claude-sid-2", "codex": "codex-sid-2"}

        # fresh: a from-scratch attempt is deliberately cold.
        coordinator.run_round(room["id"], "round three", lineage="fresh")
        assert agents[0].seen_scopes[2]["seed"] == {}
    finally:
        coordinator.close()


def test_fresh_session_flag_drops_stored_ids(git_repo, tmp_path):
    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        coordinator.run_round(room["id"], "round two", fresh_session=True)
        assert agents[0].seen_scopes[1]["seed"] == {}
        # The stored copy was cleared, then re-populated by round two's sid.
        assert coordinator.store.member_sessions(room["id"]) == {
            "claude": "claude-sid-2"
        }
    finally:
        coordinator.close()


def test_fork_flag_reaches_the_session_scope(git_repo, tmp_path):
    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        coordinator.run_round(room["id"], "round two", fork=True)
        assert agents[0].seen_scopes[1]["fork"] is True
    finally:
        coordinator.close()


def test_upgrade_backfills_workspace_sessions_into_durable_copy(git_repo, tmp_path):
    # A db upgraded to sessions_json holds session ids only on its workspace
    # rows, which lineage swaps/recovery DELETE — reopening the store must
    # merge them into the durable per-room copy, or a crash mid-branch-from
    # permanently loses the sessions.
    import sqlite3 as _sqlite3

    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        room_id = room["id"]
        db_path = coordinator.store.db_path
    finally:
        coordinator.close()
    # Simulate the pre-upgrade shape: the durable copy never existed.
    raw = _sqlite3.connect(db_path)
    raw.execute("UPDATE rooms SET sessions_json=NULL WHERE id=?", (room_id,))
    raw.commit()
    raw.close()

    store = RoomStore(db_path.parent)
    try:
        # The reopen backfilled the durable copy: the id survives even after
        # the workspace rows (the only pre-upgrade holder) are deleted.
        store.clear_workspaces(room_id)
        assert store.member_sessions(room_id) == {"claude": "claude-sid-1"}
    finally:
        store.close()


def test_cold_completed_turn_without_new_sid_drops_stale_session(git_repo, tmp_path):
    # Conversations disabled → every turn is cold. A completed cold turn that
    # reports no session id advances the member's watermark, but the STORED
    # (old) conversation never saw this round — keeping it would make a later
    # re-enabled resume omit context. It must be dropped.
    class MuteSecondCallAgent(SessionAgent):
        async def aexecute(self, prompt: Path, workdir: Path, timeout: int):
            result = await super().aexecute(prompt, workdir, timeout)
            if len(self.seen_scopes) >= 2:
                result.provider_session_id = None
            return result

    config = _config(tmp_path)
    config.conversations.enabled = False
    agents = [MuteSecondCallAgent("claude")]
    coordinator = RoomCoordinator(config, git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        assert coordinator.store.member_sessions(room["id"]) == {
            "claude": "claude-sid-1"
        }
        coordinator.run_round(room["id"], "round two")
        assert coordinator.store.member_sessions(room["id"]) == {}
    finally:
        coordinator.close()


class FlakySessionAgent(SessionAgent):
    """SessionAgent whose Nth call (1-based) fails without minting a session id
    — the shape of a CLI that cannot launch / times out / rejects a session."""

    def __init__(self, agent_id: str, *, fail_calls: set[int], **kwargs):
        super().__init__(agent_id, **kwargs)
        self._fail_calls = fail_calls

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        result = await super().aexecute(prompt, workdir, timeout)
        if len(self.seen_scopes) in self._fail_calls:
            result.status = TaskStatus.FAILED
            result.error = "scripted failure"
            result.exit_code = 1
            result.provider_session_id = None
        return result


def test_failed_turn_does_not_advance_the_delta_watermark(git_repo, tmp_path):
    # A failed/timed-out member never consumed that round's prompt, but its
    # stored session id survives (update_workspace_session ignores None). If
    # the failed candidate advanced last_round_by_member, the next resume
    # would omit peer outputs its conversation never received.
    claude = FlakySessionAgent("claude", fail_calls={2})
    codex = SessionAgent("codex")
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[claude, codex]
    )
    try:
        room = coordinator.create("task", ["claude", "codex"])
        coordinator.run_round(room["id"], "round one")
        coordinator.run_round(room["id"], "round two")
        by_round: dict[int, dict[str, dict]] = {}
        for row in coordinator.store.list_candidates(room["id"]):
            by_round.setdefault(row["round_number"], {})[row["member_id"]] = row
        assert by_round[2]["claude"]["state"] == "failed"
        assert coordinator.store.member_sessions(room["id"])["claude"] == (
            "claude-sid-1"
        )
        coordinator.run_round(room["id"], "round three")
        prompt = claude.seen_scopes[2]["prompt"]
        # claude's conversation last CONSUMED a prompt at round one, so its
        # round-three delta must include codex's round-ONE candidate (published
        # after round one's prompts were built) as well as round two's.
        assert by_round[1]["codex"]["id"] in prompt
        assert by_round[2]["codex"]["id"] in prompt
    finally:
        coordinator.close()


def test_schema_migration_tolerates_concurrent_first_open(tmp_path, monkeypatch):
    # The column check and the ALTER are two statements, so two processes
    # opening an upgraded room db at once (TUI + CLI/worker) can both see
    # sessions_json missing before either adds it — the loser must swallow
    # "duplicate column name", not fail RoomStore initialization. Simulated
    # deterministically: the column already exists (the winner added it), but
    # the loser's PRAGMA read predates it.
    root = tmp_path / "rooms"
    RoomStore(root).close()  # the winner: db fully migrated

    real_connect = rooms_module.sqlite3.connect

    class _StalePragmaConnection:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def __setattr__(self, name, value):
            setattr(self._real, name, value)

        def __getattr__(self, name):
            return getattr(self._real, name)

        def execute(self, sql, *args):
            cursor = self._real.execute(sql, *args)
            if sql.strip().startswith("PRAGMA table_info(rooms)"):
                rows = [r for r in cursor.fetchall() if r["name"] != "sessions_json"]

                class _Stale:
                    @staticmethod
                    def fetchall():
                        return rows

                return _Stale()
            return cursor

    monkeypatch.setattr(
        rooms_module.sqlite3,
        "connect",
        lambda *a, **k: _StalePragmaConnection(real_connect(*a, **k)),
    )
    RoomStore(root).close()  # the loser: must open, not raise


def test_preflight_enforces_claude_only_fork(git_repo, tmp_path):
    # The Click-layer guard is bypassed by direct run_round /
    # launch_room_worker / room-worker calls, and the codex/kimi builders
    # ignore the fork flag — enforce claude-only at the coordinator boundary.
    agents = [SessionAgent("claude"), SessionAgent("codex")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude", "codex"])
        coordinator.run_round(room["id"], "round one")  # both sessions stored
        with pytest.raises(RoomError, match="only for claude members"):
            coordinator.run_round(room["id"], "round two", fork=True)
        assert len(agents[1].seen_scopes) == 1  # nothing launched
    finally:
        coordinator.close()


def test_fork_that_cannot_resume_is_rejected_before_launch(git_repo, tmp_path):
    # A fork with no resumable seed would silently run a fresh session while
    # reporting the fork succeeded (`--fork-session` rides only beside a
    # resume id) — every such case is rejected before any provider launches.
    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        # Round one: no stored session exists yet — nothing to fork.
        with pytest.raises(RoomError, match="stored provider session"):
            coordinator.run_round(room["id"], "round one", fork=True)
        assert agents[0].seen_scopes == []

        coordinator.run_round(room["id"], "round one")
        with pytest.raises(RoomError, match="cannot combine"):
            coordinator.run_round(
                room["id"], "round two", fork=True, fresh_session=True
            )
        with pytest.raises(RoomError, match="cannot combine"):
            coordinator.run_round(
                room["id"], "round two", fork=True, lineage="fresh"
            )
        # The rejections launched nothing and left the stored session intact.
        assert len(agents[0].seen_scopes) == 1
        assert coordinator.store.member_sessions(room["id"]) == {
            "claude": "claude-sid-1"
        }
    finally:
        coordinator.close()


def test_fork_with_conversations_disabled_is_rejected(git_repo, tmp_path):
    config = _config(tmp_path)
    config.conversations.enabled = False
    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(config, git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        with pytest.raises(RoomError, match="conversation continuation"):
            coordinator.run_round(room["id"], "round two", fork=True)
        assert len(agents[0].seen_scopes) == 1
    finally:
        coordinator.close()


def test_load_candidate_manifest_survives_missing_patch_file(git_repo, tmp_path):
    # Usage display is best-effort metadata: a historical candidate whose
    # patch file is gone must not fail the manifest-only read the way the
    # full-artifact `load_candidate` does.
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        candidate = coordinator.store.list_candidates(room["id"])[0]
        (Path(candidate["artifact_path"]) / "candidate.patch").unlink()
        with pytest.raises(RoomError):
            coordinator.store.load_candidate(candidate["id"])
        manifest = coordinator.store.load_candidate_manifest(candidate["id"])
        assert manifest["candidate_id"] == candidate["id"]
        assert "patch" not in manifest  # metadata only, no artifact I/O
    finally:
        coordinator.close()


def test_resuming_member_gets_delta_prefix_cold_member_gets_full(git_repo, tmp_path):
    # Gap 4: a resumed conversation already holds the member's own outputs and
    # everything published before its last round — only peers' NEW outputs are
    # re-sent. A cold member still gets the full checkpoint.
    class ColdAgent(WritingAgent):
        """Completes but never reports a provider session id."""

    agents = [SessionAgent("claude"), ColdAgent("codex")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude", "codex"])
        coordinator.run_round(room["id"], "round one")
        coordinator.run_round(room["id"], "round two")

        resumed_prompt = agents[0].seen_scopes[1]["prompt"]
        # Own prior output excluded; the peer's round-one output included.
        assert "result from claude" not in resumed_prompt
        assert "result from codex" in resumed_prompt
        assert "round two" in resumed_prompt

        # The cold member's prompt keeps the full cross-member checkpoint.
        cold_prompt = agents[1].calls[-1]["prompt"]
        assert "result from claude" in cold_prompt
        assert "result from codex" in cold_prompt
    finally:
        coordinator.close()


def test_delta_prefix_filters_by_round_and_member_directly(git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )
    try:
        room = coordinator.create("task", ["claude", "codex"])
        coordinator.run_round(room["id"], "one")
        coordinator.run_round(room["id"], "two")

        full = coordinator._prompt_with_checkpoint(room["id"], "next")
        assert full.count("[claude |") == 2 and full.count("[codex |") == 2

        delta = coordinator._prompt_with_checkpoint(
            room["id"], "next", exclude_member="claude", min_round_number=2
        )
        assert "[claude |" not in delta
        assert delta.count("[codex |") == 1

        # Nothing new for the member → the bare prompt, no checkpoint framing.
        bare = coordinator._prompt_with_checkpoint(
            room["id"], "next", exclude_member="claude", min_round_number=3
        )
        assert bare == "next"
    finally:
        coordinator.close()


def test_candidate_manifest_round_trips_usage(git_repo, tmp_path):
    from syncmill.models import TokenUsage

    class UsageAgent(WritingAgent):
        async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
            result = await super().aexecute(prompt, workdir, timeout)
            result.usage = TokenUsage(
                total_input_tokens=40_100,
                output_tokens=2_300,
                cache_read_tokens=34_200,
                total_cost_usd=0.42,
            )
            return result

    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[UsageAgent("claude")]
    )
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "one")
        candidate = coordinator.store.list_candidates(room["id"])[0]
        manifest = coordinator.store.load_candidate(candidate["id"])
        assert manifest["usage"] == {
            "total_input_tokens": 40_100,
            "output_tokens": 2_300,
            "cache_read_tokens": 34_200,
            "cache_creation_tokens": None,
            "total_cost_usd": 0.42,
        }
    finally:
        coordinator.close()


# ── shared-memory enrichment + write-back (rooms ↔ memory) ─────────────────


class _StubRoomMemory:
    def __init__(self, *, context: str = "", raises: bool = False):
        self._context = context
        self._raises = raises
        self.degraded = False
        self.last_error = None
        self.searches: list[str] = []
        self.shares: list[dict] = []

    async def search_context(self, query, agent_id=None):
        if self._raises:
            raise RuntimeError("memory backend exploded")
        self.searches.append(query)
        return self._context

    async def share_result(self, task, result, *, kind="task-result"):
        if self._raises:
            raise RuntimeError("memory backend exploded")
        self.shares.append({"task": task, "result": result, "kind": kind})


def test_round_prompt_enriched_with_memory_context_but_stored_raw(git_repo, tmp_path):
    memory = _StubRoomMemory(context="prior shared knowledge")
    agents = [WritingAgent("claude")]
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=agents, memory=memory
    )
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "user round prompt")
        prompt = agents[0].calls[-1]["prompt"]
        assert prompt.startswith("Context from previous work:\nprior shared knowledge")
        assert "user round prompt" in prompt
        # The STORED round prompt is the user's text — enrichment reaches only
        # the agents, never the history/`room rounds` surface.
        assert coordinator.store.list_rounds(room["id"])[-1]["prompt"] == (
            "user round prompt"
        )
        assert memory.searches == ["user round prompt"]
    finally:
        coordinator.close()


def test_memory_failure_degrades_round_never_blocks(git_repo, tmp_path):
    memory = _StubRoomMemory(raises=True)
    agents = [WritingAgent("claude")]
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=agents, memory=memory
    )
    try:
        room = coordinator.create("task", ["claude"])
        completed = coordinator.run_round(room["id"], "go")
        assert completed["state"] == "awaiting_decision"
        prompt = agents[0].calls[-1]["prompt"]
        assert "Context from previous work" not in prompt
        assert any(
            event["kind"] == "memory.degraded"
            for event in coordinator.store.events(room["id"])
        )
    finally:
        coordinator.close()


def test_memory_prefix_dropped_whole_when_over_byte_cap(git_repo, tmp_path):
    # The bounded (4 KB) context always fits a small prompt, so drive the cap
    # branch directly: a prefix that would cross the argv cap is dropped WHOLE,
    # keeping the checkpoint prompt verbatim.
    big_prompt = "x" * (rooms_module._ROOM_PROMPT_MAX_BYTES - 10)
    out = RoomCoordinator._with_memory_prefix("some context", big_prompt)
    assert out == big_prompt
    small = RoomCoordinator._with_memory_prefix("ctx", "task")
    assert small == "Context from previous work:\nctx\n\n---\n\ntask"
    assert RoomCoordinator._with_memory_prefix("", "task") == "task"


def test_apply_shares_room_result_best_effort(git_repo, tmp_path):
    memory = _StubRoomMemory()
    agents = [WritingAgent("claude")]
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=agents, memory=memory
    )
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "go")
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        coordinator.apply(room["id"])
        assert len(memory.shares) == 1
        assert memory.shares[0]["kind"] == "room-result"
        assert memory.shares[0]["result"].agent_id == "claude"
    finally:
        coordinator.close()


def test_apply_survives_share_failure(git_repo, tmp_path):
    agents = [WritingAgent("claude")]
    memory = _StubRoomMemory(context="")
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=agents, memory=memory
    )
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "go")
        memory._raises = True  # searches are done; only the share explodes
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        run_id, _receipt = coordinator.apply(room["id"])
        assert run_id
        assert coordinator.store.get_room(room["id"])["state"] == "applied"
        # ...but the lost write is VISIBLE: a durable memory.degraded event
        # (the only trail a detached apply job leaves) plus the foreground
        # warning seam — never a silent completion without the room-result.
        assert coordinator.last_share_warning is not None
        assert "room-result write failed" in coordinator.last_share_warning
        assert any(
            event["kind"] == "memory.degraded"
            for event in coordinator.store.events(room["id"])
        )
    finally:
        coordinator.close()


def test_apply_surfaces_internally_degraded_share_not_stale_degrade(
    git_repo, tmp_path
):
    # SyncmillMemory.share_result catches its own failures (sets last_error,
    # returns normally) — the apply must detect THAT shape too, while a stale
    # degrade from an earlier call must not be re-reported as a lost write.
    class _DegradingMemory(_StubRoomMemory):
        degrade_share = False

        async def share_result(self, task, result, *, kind="task-result"):
            if self.degrade_share:
                self.degraded = True
                self.last_error = "RuntimeError: backend down"
                return
            await super().share_result(task, result, kind=kind)

    def _applied_room(memory, prompt):
        # Distinct prompts per case: the first apply lands claude.txt in the
        # source repo, and an identical second round would diff to nothing.
        coordinator = RoomCoordinator(
            _config(tmp_path), git_repo, agents=[WritingAgent("claude")],
            memory=memory,
        )
        try:
            room = coordinator.create("task", ["claude"])
            coordinator.run_round(room["id"], prompt)
            candidate = coordinator.store.list_candidates(room["id"])[0]
            coordinator.select(room["id"], candidate["id"])
            coordinator.apply(room["id"])
            events = coordinator.store.events(room["id"])
            return coordinator.last_share_warning, events
        finally:
            coordinator.close()

    degrading = _DegradingMemory()
    degrading.degrade_share = True
    warning, events = _applied_room(degrading, "go degraded")
    assert warning is not None and "room-result write degraded" in warning
    assert any(event["kind"] == "memory.degraded" for event in events)

    # Sticky degrade from BEFORE the apply, healthy share: no false positive.
    stale = _StubRoomMemory()
    stale.degraded = True
    stale.last_error = "RuntimeError: an earlier search degraded"
    warning, events = _applied_room(stale, "go stale")
    assert warning is None
    assert stale.shares and stale.shares[0]["kind"] == "room-result"
    # The ROUND legitimately records the sticky degrade; the APPLY must not
    # re-report it as a lost room-result write.
    assert not any(
        "room-result" in event["payload"].get("reason", "")
        for event in events
        if event["kind"] == "memory.degraded"
    )


def test_close_bounds_owned_memory_teardown(git_repo, tmp_path, monkeypatch):
    # Every CLI/worker path calls close() after the operation completed or
    # degraded — a backend whose close() never returns must not hang room
    # round/apply or TUI shutdown.
    import asyncio as _asyncio
    import time as _time

    monkeypatch.setattr(rooms_module, "_ROOM_MEMORY_TIMEOUT_SECONDS", 0.05)

    class _HangingCloseMemory:
        async def close(self):
            await _asyncio.sleep(3600)

    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    coordinator._memory = _HangingCloseMemory()
    coordinator._owns_memory = True
    start = _time.monotonic()
    coordinator.close()
    assert _time.monotonic() - start < 5


def test_hung_memory_lookup_degrades_instead_of_blocking(
    git_repo, tmp_path, monkeypatch
):
    # The lookup runs AFTER the round lease is held: an unresponsive remote
    # backend (mem0/Qdrant, an embedder) must degrade to no context on a
    # deadline, never hold a live worker (and its lease) forever.
    import asyncio as _asyncio

    monkeypatch.setattr(rooms_module, "_ROOM_MEMORY_TIMEOUT_SECONDS", 0.05)

    class _HangingMemory(_StubRoomMemory):
        async def search_context(self, query, agent_id=None):
            await _asyncio.sleep(3600)

    memory = _HangingMemory()
    agents = [WritingAgent("claude")]
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=agents, memory=memory
    )
    try:
        room = coordinator.create("task", ["claude"])
        completed = coordinator.run_round(room["id"], "go")
        assert completed["state"] == "awaiting_decision"
        assert "Context from previous work" not in agents[0].calls[-1]["prompt"]
        assert any(
            event["kind"] == "memory.degraded"
            for event in coordinator.store.events(room["id"])
        )
    finally:
        coordinator.close()


def test_hung_room_result_share_does_not_stall_the_apply(
    git_repo, tmp_path, monkeypatch
):
    import asyncio as _asyncio

    monkeypatch.setattr(rooms_module, "_ROOM_MEMORY_TIMEOUT_SECONDS", 0.05)

    class _HangingShareMemory(_StubRoomMemory):
        async def share_result(self, task, result, *, kind="task-result"):
            await _asyncio.sleep(3600)

    memory = _HangingShareMemory()
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")], memory=memory
    )
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "go")
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        run_id, _receipt = coordinator.apply(room["id"])
        assert run_id
        assert coordinator.last_share_warning is not None
    finally:
        coordinator.close()


def test_prompt_cap_is_windows_safe():
    # Windows CreateProcess caps the whole command line at 32,767 UTF-16
    # chars; a 96 KiB prompt accepted on the POSIX budget would fail at
    # provider launch with WinError 206.
    assert rooms_module._prompt_max_bytes("posix") == 96 * 1024
    assert rooms_module._prompt_max_bytes("nt") <= 30_000


def test_windows_prompt_limit_counts_quoted_command_line(monkeypatch):
    # Windows re-quotes the whole command line: 20,000 quotation marks pass a
    # raw-byte check but expand to >40,000 chars via list2cmdline escaping.
    quoteful = '"' * 20_000
    assert rooms_module._prompt_arg_units(quoteful, "posix") == 20_000
    assert rooms_module._prompt_arg_units(quoteful, "nt") > 30_000

    monkeypatch.setattr(
        rooms_module, "_ROOM_PROMPT_MAX_BYTES", rooms_module._prompt_max_bytes("nt")
    )
    monkeypatch.setattr(rooms_module.os, "name", "nt")
    with pytest.raises(RoomError, match="byte limit"):
        rooms_module._validate_prompt_text(quoteful)
    # A quote-free prompt of the same length stays within budget.
    rooms_module._validate_prompt_text("x" * 20_000)


def test_memory_enrichment_measured_with_platform_accounting(monkeypatch):
    # The user prompt is validated BEFORE enrichment, so the composed prompt
    # must be re-measured with the platform accounting: a quote-heavy context
    # fits the raw-byte cap but doubles under Windows list2cmdline quoting.
    prompt = "x" * 25_000
    context = '"' * 4_000
    composed = f"Context from previous work:\n{context}\n\n---\n\n{prompt}"
    assert len(composed.encode()) < 30_000  # raw bytes fit...
    assert rooms_module._prompt_arg_units(composed, "nt") > 30_000  # ...units don't

    monkeypatch.setattr(
        rooms_module, "_ROOM_PROMPT_MAX_BYTES", rooms_module._prompt_max_bytes("nt")
    )
    monkeypatch.setattr(rooms_module.os, "name", "nt")
    # Over budget once quoted → the enrichment is dropped WHOLE.
    assert RoomCoordinator._with_memory_prefix(context, prompt) == prompt
    # A modest context still rides along.
    assert RoomCoordinator._with_memory_prefix("ctx", "task") != "task"


def test_nul_in_memory_context_is_scrubbed_not_fatal(git_repo, tmp_path):
    # Untrusted memory context (and checkpoint-embedded candidate output) can
    # carry a NUL; create_subprocess_exec would raise on the argv element and
    # freeze the round as recovery_required — best-effort enrichment must
    # scrub it instead.
    memory = _StubRoomMemory(context="poisoned\x00context")
    agents = [WritingAgent("claude")]
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=agents, memory=memory
    )
    try:
        room = coordinator.create("task", ["claude"])
        completed = coordinator.run_round(room["id"], "go")
        assert completed["state"] == "awaiting_decision"
        prompt = agents[0].calls[-1]["prompt"]
        assert "\x00" not in prompt
        assert "poisonedcontext" in prompt  # scrubbed, not dropped
    finally:
        coordinator.close()


def test_quote_heavy_checkpoint_degrades_under_windows_accounting(
    git_repo, tmp_path, monkeypatch
):
    # The checkpoint byte-budget loop cannot overshoot on POSIX, but Windows
    # quoting can double quote-heavy CANDIDATE OUTPUT past the CreateProcess
    # limit even when raw bytes fit — the composed checkpoint must degrade to
    # the validated bare prompt, never fail the round mid-fan-out.
    class QuoteyAgent(WritingAgent):
        def __init__(self, agent_id: str):
            super().__init__(agent_id)
            self._output = '"' * 20_000

    agents = [QuoteyAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        # A budget where the 4 KB-bounded quote block fits in BYTES but not in
        # quoted UTF-16 units (the nt doubling). os is proxied for rooms.py
        # ONLY — patching the real os.name would make bare Path() construct
        # WindowsPath on this host.
        import os as _os

        class _NtOS:
            name = "nt"

            def __getattr__(self, item):
                return getattr(_os, item)

        monkeypatch.setattr(rooms_module, "_ROOM_PROMPT_MAX_BYTES", 6_000)
        monkeypatch.setattr(rooms_module, "os", _NtOS())
        coordinator.run_round(room["id"], "round two")
        prompt = agents[0].calls[-1]["prompt"]
        assert prompt == "round two"  # checkpoint dropped whole, round ran
    finally:
        coordinator.close()


def test_prompt_rejects_terminal_control_characters(git_repo, tmp_path):
    # Central rejection (create + preflight): a stored prompt must never be
    # able to smuggle clipboard/title-rewriting sequences into the CLI/TUI
    # display paths. Tab/newline/CR remain allowed.
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        with pytest.raises(RoomError, match="control characters"):
            coordinator.create("evil \x1b]52;c;cGF5bG9hZA==\x07", ["claude"])
        room = coordinator.create("tabs\tand\nnewlines\r\nare fine", ["claude"])
        with pytest.raises(RoomError, match="control characters"):
            coordinator.run_round(room["id"], "csi \x9b31m")
        assert coordinator.store.get_room(room["id"])["round_count"] == 0
    finally:
        coordinator.close()


def test_preflight_rejects_nul_prompts_on_every_entry_point(git_repo, tmp_path):
    # The TUI TextArea bypasses the CLI's _resolve_prompt, so NUL must be
    # rejected at the coordinator boundary — before a lease/workspace exists
    # (a NUL argv element freezes the room as recovery_required mid-round).
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        with pytest.raises(RoomError, match="NUL byte"):
            coordinator.create("bad\x00create", ["claude"])
        room = coordinator.create("task", ["claude"])
        with pytest.raises(RoomError, match="NUL byte"):
            coordinator.run_round(room["id"], "bad\x00round")
        assert coordinator.store.get_room(room["id"])["round_count"] == 0
    finally:
        coordinator.close()


def test_coordinator_binds_project_memory_defaults(git_repo, tmp_path):
    # Project-scoped memory is the repository contract: an initialized project
    # leaving [memory] at its defaults must bind to <repo>/.syncmill/memory on
    # EVERY coordinator path — not only `run` — else round searches and
    # room-result writes target the ambient ~/.memtomem store.
    config = _config(tmp_path)
    assert config.project.id  # _config models an initialized project
    assert config.memory.memory_dir is None
    coordinator = RoomCoordinator(
        config, git_repo, agents=[WritingAgent("claude")]
    )
    try:
        expected = (git_repo / config.project.memory_dir).resolve()
        assert config.memory.memory_dir == str(expected)
        assert config.memory.db_path == str(expected / "memtomem.db")
    finally:
        coordinator.close()


def test_disabled_conversations_never_sends_a_delta_prompt(git_repo, tmp_path):
    # codex review BLOCKER: session ids are still captured with continuation
    # OFF, but the scope never resumes anyone. Seeding from them would hand a
    # COLD provider a resume-aware delta prompt whose omitted context it has
    # never seen. With conversations off every member gets the FULL checkpoint.
    config = _config(tmp_path)
    config.conversations.enabled = False
    agents = [SessionAgent("claude"), SessionAgent("codex")]
    coordinator = RoomCoordinator(config, git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude", "codex"])
        coordinator.run_round(room["id"], "round one")
        # The ids ARE persisted (a later enable can resume them)...
        assert coordinator.store.member_sessions(room["id"]) == {
            "claude": "claude-sid-1", "codex": "codex-sid-1",
        }
        coordinator.run_round(room["id"], "round two")
        # ...but round two is cold: no seed, and the FULL checkpoint (incl. the
        # member's own round-one output) is re-sent.
        assert agents[0].seen_scopes[1]["seed"] is None
        prompt = agents[0].seen_scopes[1]["prompt"]
        assert "result from claude" in prompt and "result from codex" in prompt
    finally:
        coordinator.close()


def test_fresh_lineage_durably_clears_sessions(git_repo, tmp_path):
    # codex review MAJOR: `fresh` used to suppress the seed for ONE round while
    # leaving rooms.sessions_json intact, so a cold attempt that reported no new
    # session let the NEXT continue-own round resurrect the pre-fresh
    # conversation — with last_round_by_member already advanced past what that
    # conversation saw.
    class SilentAgent(WritingAgent):
        """Completes but never reports a provider session id (a cold turn that
        yields nothing to store)."""

    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        assert coordinator.store.member_sessions(room["id"]) != {}
        coordinator.agents = [SilentAgent("claude")]
        coordinator.run_round(room["id"], "round two", lineage="fresh")
        # The pre-fresh id is GONE — nothing to resurrect on the next round.
        assert coordinator.store.member_sessions(room["id"]) == {}
    finally:
        coordinator.close()


def test_session_clear_happens_only_after_the_round_lease_is_held(git_repo, tmp_path):
    # codex review MAJOR: clearing before start_round let a competitor that
    # LOSES the lease (or any pre-lease failure) erase the accepted round's
    # sessions. The clear now sits after the lease is acquired.
    agents = [SessionAgent("claude")]
    coordinator = RoomCoordinator(_config(tmp_path), git_repo, agents=agents)
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "round one")
        stored = coordinator.store.member_sessions(room["id"])
        assert stored == {"claude": "claude-sid-1"}

        original = coordinator.store.start_round

        def refuse(*args, **kwargs):
            raise RoomError("lease lost to a competing worker")

        coordinator.store.start_round = refuse
        try:
            with pytest.raises(RoomError, match="lease lost"):
                coordinator.run_round(room["id"], "round two", fresh_session=True)
        finally:
            coordinator.store.start_round = original
        # The losing invocation left the accepted round's sessions untouched.
        assert coordinator.store.member_sessions(room["id"]) == stored
    finally:
        coordinator.close()


def test_create_caps_the_prompt_at_the_argv_limit(git_repo, tmp_path):
    # codex review MAJOR: an oversized CREATION prompt used to be accepted and
    # only fail later at round time (it is every round's fallback), and the cap
    # applied per-entry-point rather than in the coordinator.
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        with pytest.raises(RoomError, match="byte limit"):
            coordinator.create("x" * (rooms_module._ROOM_PROMPT_MAX_BYTES + 1))
    finally:
        coordinator.close()


def test_close_closes_an_owned_memory_handle_but_not_an_injected_one(
    git_repo, tmp_path
):
    # codex review MINOR: a lazily built SyncmillMemory holds backend handles;
    # close() must release what it owns (and still close the store).
    class _ClosableMemory(_StubRoomMemory):
        def __init__(self):
            super().__init__()
            self.closed = False

        async def close(self):
            self.closed = True

    injected = _ClosableMemory()
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")], memory=injected
    )
    coordinator.close()
    assert injected.closed is False  # an injected handle belongs to its owner

    owned = _ClosableMemory()
    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    coordinator._memory = owned
    coordinator._owns_memory = True
    coordinator.close()
    assert owned.closed is True


def test_apply_shares_after_workspace_cleanup(git_repo, tmp_path):
    # codex review MAJOR: the best-effort memory write-back sat between
    # `applied` and workspace cleanup, so a hanging/interrupted backend could
    # strand retained workspaces after the source was already mutated.
    order: list[str] = []

    class _OrderMemory(_StubRoomMemory):
        async def share_result(self, task, result, *, kind="task-result"):
            order.append("share")
            await super().share_result(task, result, kind=kind)

    coordinator = RoomCoordinator(
        _config(tmp_path), git_repo, agents=[WritingAgent("claude")],
        memory=_OrderMemory(),
    )
    original_cleanup = coordinator._cleanup_workspaces

    def traced(room_id):
        order.append("cleanup")
        return original_cleanup(room_id)

    coordinator._cleanup_workspaces = traced
    try:
        room = coordinator.create("task", ["claude"])
        coordinator.run_round(room["id"], "go")
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        coordinator.apply(room["id"])
        assert order == ["cleanup", "share"]
    finally:
        coordinator.close()


# ── store-open robustness (review 2026-09-10) ────────────────────────────


def test_opening_the_store_does_not_take_the_write_lock(tmp_path):
    # The sessions backfill ran unconditionally inside BEGIN IMMEDIATE, which
    # takes the write lock at statement start whether or not it has anything to
    # do — so EVERY open was a writer. Opening then waited out busy_timeout
    # behind any live writer (a running round, a prune) and raised "database is
    # locked" from the constructor, breaking `room list`, the TUI's poll
    # rebind, and a second worker. A steady-state open must be read-only.
    root = tmp_path / "rooms"
    RoomStore(root).close()

    holder = rooms_module.sqlite3.connect(
        root / "rooms.sqlite3", timeout=5, isolation_level=None
    )
    holder.execute("PRAGMA busy_timeout=200")
    holder.execute("BEGIN IMMEDIATE")  # a concurrent writer holds the lock
    try:
        started = time.time()
        RoomStore(root).close()  # must not block, must not raise
        assert time.time() - started < 2
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_opening_a_corrupt_store_raises_room_error(tmp_path):
    # Board wraps every open failure into BoardError because its callers catch
    # only that; RoomStore wrapped nothing, so every `syncmill room *` command
    # tracebacked raw on a corrupt/locked/unwritable store.
    root = tmp_path / "rooms"
    root.mkdir(parents=True)
    (root / "rooms.sqlite3").write_bytes(b"this is not a database")
    with pytest.raises(RoomError):
        RoomStore(root)


def test_rollback_does_not_mask_the_error_that_caused_it(tmp_path):
    # SQLite auto-rolls-back on some errors (SQLITE_FULL, SQLITE_IOERR); the
    # explicit ROLLBACK then raises "cannot rollback - no transaction is
    # active" and REPLACES the real failure — which is what a room's durable
    # recovery_required reason would have recorded.
    store = RoomStore(tmp_path / "rooms")
    try:
        with pytest.raises(RuntimeError, match="the real failure"):
            with store._tx() as conn:
                conn.execute("ROLLBACK")  # simulate SQLite's own auto-rollback
                raise RuntimeError("the real failure")
    finally:
        store.close()


def test_the_backfill_merges_every_member_in_one_pass(tmp_path):
    # Every row of a multi-member room carries the SAME sessions_json snapshot
    # from one query, so a per-row write has each member rebuild the dict from
    # that original and overwrite its siblings — a two-member room finished a
    # pass holding only the last. A later open can repair it only while the
    # workspace rows survive; a lineage swap or recovery cleanup deletes them
    # first, and the missing session is then gone for good.
    root = tmp_path / "rooms"
    store = RoomStore(root)
    try:
        store._conn.execute(
            "INSERT INTO rooms (id,task_id,repo_root,prompt,members_json,state,"
            "revision,created_at,updated_at,sessions_json) "
            "VALUES ('r1','t1','/repo','p','[]','open',1,'now','now','{}')"
        )
        for member in ("a", "b", "c"):
            store._conn.execute(
                "INSERT INTO room_workspaces (room_id,member_id,provider_id,path,"
                "control_repo,run_root,base_commit,provider_session_id,updated_at) "
                "VALUES ('r1',?,'codex','/wt','/cr','/rr','HEAD',?,'now')",
                (member, f"session-{member}"),
            )
    finally:
        store.close()

    store = RoomStore(root)  # exactly ONE backfill pass
    try:
        stored = json.loads(
            store._conn.execute(
                "SELECT sessions_json FROM rooms WHERE id='r1'"
            ).fetchone()["sessions_json"]
        )
    finally:
        store.close()
    assert stored == {"a": "session-a", "b": "session-b", "c": "session-c"}


def test_the_backfill_never_restores_a_cleared_session(tmp_path, monkeypatch):
    # The read guard that keeps a steady-state open read-only must be used ONLY
    # to decide whether to write. Carrying its provider_session_id into the
    # transaction let a value another process cleared in between (a
    # --fresh-session round, a lineage swap) be restored from the stale read.
    root = tmp_path / "rooms"
    store = RoomStore(root)
    try:
        store._conn.execute(
            "INSERT INTO rooms (id,task_id,repo_root,prompt,members_json,state,"
            "revision,created_at,updated_at,sessions_json) "
            "VALUES ('r1','t1','/repo','p','[]','open',1,'now','now','{}')"
        )
        store._conn.execute(
            "INSERT INTO room_workspaces (room_id,member_id,provider_id,path,"
            "control_repo,run_root,base_commit,provider_session_id,updated_at) "
            "VALUES ('r1','m','codex','/wt','/cr','/rr','HEAD','old-session','now')"
        )
    finally:
        store.close()

    # Between the unlocked scan and the transaction, both session sources are
    # cleared — exactly what a concurrent --fresh-session does.
    real_tx = RoomStore._tx

    def clearing_tx(self):
        self._conn.execute(
            "UPDATE room_workspaces SET provider_session_id='' WHERE room_id='r1'"
        )
        return real_tx(self)

    monkeypatch.setattr(RoomStore, "_tx", clearing_tx)
    store = RoomStore(root)
    try:
        row = store._conn.execute(
            "SELECT sessions_json FROM rooms WHERE id='r1'"
        ).fetchone()
        assert row["sessions_json"] in ("{}", None), (
            "a cleared session was resurrected from the pre-transaction read"
        )
    finally:
        store.close()
