from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from syncmill.config import TuiConfig
from syncmill.tui_jobs import (
    ArtifactApplySpec,
    BoardJobSpec,
    JobError,
    JobStore,
    RoomApplySpec,
    board_key,
    build_job_argv,
    canonical_board_path,
    _MANAGER_DONE,
    _Manager,
    _RollingLog,
    _atomic_json,
    _guardian_main,
    _linux_process_group_has_live_members,
    _socket_path,
    _worker_main,
)
from syncmill.board import Board, WorkState


def test_board_job_spec_and_argv():
    selected = BoardJobSpec(
        mode="selected", item_id="abc", team="py", strategy="compete", agent="codex"
    )
    assert build_job_argv(selected) == [
        "board", "run", "--id", "abc", "--strategy", "compete",
        "--agent", "codex", "--team", "py",
    ]
    assert build_job_argv(BoardJobSpec(mode="loop", goal="backend")) == [
        "board", "run", "--loop", "--goal", "backend",
    ]
    with pytest.raises(ValueError):
        BoardJobSpec(mode="selected")
    with pytest.raises(ValueError):
        BoardJobSpec(mode="loop", item_id="abc")


def test_apply_op_specs_argv_and_repo_boundary():
    # Apply ops emit --repo <project_root> (the store owns the boundary) and route
    # to their dedicated detached worker entrypoints.
    art = ArtifactApplySpec(run_id="run-9", verify=True)
    assert art.op == "artifact-apply"
    assert build_job_argv(art, project_root="/p") == [
        "apply-worker", "run-9", "--repo", "/p", "--verify",
    ]
    assert build_job_argv(ArtifactApplySpec(run_id="run-9"), project_root="/p") == [
        "apply-worker", "run-9", "--repo", "/p",
    ]
    room = RoomApplySpec(
        room_id="room-3", verify=True, expected_candidate_id="c1", expected_revision=4
    )
    assert room.op == "room-apply"
    assert build_job_argv(room, project_root="/p") == [
        "room-apply-worker", "room-3", "--repo", "/p", "--verify",
        "--expected-candidate", "c1", "--expected-revision", "4",
    ]
    # An apply op with no project root is a programming error (the store passes it).
    with pytest.raises(JobError, match="project root"):
        build_job_argv(art)
    with pytest.raises(ValueError):
        ArtifactApplySpec(run_id="")
    with pytest.raises(ValueError):
        RoomApplySpec(room_id="")


def test_apply_target_keys_follow_the_d2_formulas():
    import hashlib

    from syncmill.tui_jobs import _target_lock_key

    art_key = _target_lock_key(
        ArtifactApplySpec(run_id="r1"), project_key="PK", board_db=None
    )
    assert art_key == hashlib.sha256(b"artifact-apply\0PK\0r1").hexdigest()
    room_key = _target_lock_key(
        RoomApplySpec(room_id="m1"), project_key="PK", board_db=None
    )
    assert room_key == hashlib.sha256(b"room-apply\0PK\0m1").hexdigest()
    # Distinct ids and ops never collide on a target.
    assert art_key != room_key


def test_canonical_board_key_is_stable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = canonical_board_path("state/board.db")
    assert path == (tmp_path / "state/board.db").resolve()
    assert board_key(path) == board_key(path)


def test_canonical_board_path_wraps_unresolvable_home(monkeypatch):
    def fail_expanduser(self):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(Path, "expanduser", fail_expanduser)
    with pytest.raises(JobError, match="cannot resolve board database path"):
        canonical_board_path("~/.syncmill/board.db")


def test_job_store_wraps_unresolvable_state_dir(tmp_path, monkeypatch):
    original = Path.expanduser

    def fail_tilde(self):
        if str(self).startswith("~"):
            raise RuntimeError("Could not determine home directory")
        return original(self)

    monkeypatch.setattr(Path, "expanduser", fail_tilde)
    with pytest.raises(JobError, match="cannot resolve TUI state directory"):
        JobStore(TuiConfig(), tmp_path / "board.db")


def test_job_store_launch_contract_and_permissions(tmp_path, monkeypatch):
    cfg = TuiConfig(state_dir=str(tmp_path / "tui"), keep_jobs=2, log_max_bytes=4096)
    # The store is now project-scoped; the board db is passed as store context.
    store = JobStore(cfg, tmp_path, board_db=tmp_path / "board.db")
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return object()

    monkeypatch.setattr("syncmill.tui_jobs.subprocess.Popen", fake_popen)
    row = store.launch(BoardJobSpec(mode="loop", goal="g"), cwd=tmp_path)
    job_dir = Path(row["job_dir"])
    assert captured["argv"][:3] == [sys.executable, "-m", "syncmill.tui_jobs"]
    assert captured["kwargs"]["start_new_session"] is True
    spec = json.loads((job_dir / "spec.json").read_text())
    assert spec["argv"] == ["board", "run", "--loop", "--goal", "g"]
    assert spec["op"] == "board-run" and spec["lock_key"]
    assert "environment" not in spec and "task" not in spec
    assert (job_dir.stat().st_mode & 0o777) == 0o700
    assert ((job_dir / "spec.json").stat().st_mode & 0o777) == 0o600
    # launch() must run ensure(): job_dir.mkdir(parents=True) applies its mode
    # only to the leaf, so without ensure() the store root and jobs/ silently
    # keep umask perms (PR #78 review).
    assert (store.root.stat().st_mode & 0o777) == 0o700
    assert (store.jobs_dir.stat().st_mode & 0o777) == 0o700


def test_launch_translates_lock_io_failure_to_job_error(tmp_path, monkeypatch):
    # codex R10: lock-file I/O failures (mkdir/open/pwrite — distinct from the
    # BlockingIOError contention case, which already maps to JobError) must
    # surface as JobError; callers handle only JobError.
    cfg = TuiConfig(state_dir=str(tmp_path / "tui"))
    store = JobStore(cfg, tmp_path, board_db=tmp_path / "board.db")

    def deny_open(path):
        raise PermissionError(f"open denied: {path}")

    monkeypatch.setattr(store, "_open_exclusive", deny_open)
    with pytest.raises(JobError, match="cannot acquire the target lock"):
        store.launch(BoardJobSpec(mode="loop", goal="g"))


def test_launch_translates_store_preparation_failure_to_job_error(
    tmp_path, monkeypatch
):
    # codex R5: ensure() runs on the launch path, and its filesystem failures
    # (mkdir/chmod) must surface as JobError like every other launch failure —
    # callers handle only JobError, so a raw OSError would traceback the CLI
    # or kill the TUI.
    cfg = TuiConfig(state_dir=str(tmp_path / "tui"))
    store = JobStore(cfg, tmp_path)

    def deny_chmod(path, mode):
        raise PermissionError(f"chmod denied: {path}")

    monkeypatch.setattr("syncmill.tui_jobs.os.chmod", deny_chmod)
    with pytest.raises(JobError, match="cannot prepare the job store"):
        store.launch(BoardJobSpec(mode="loop", goal="g"))


def test_launch_rejects_cwd_mismatching_project_root(tmp_path, monkeypatch):
    cfg = TuiConfig(state_dir=str(tmp_path / "tui"))
    store = JobStore(cfg, tmp_path, board_db=tmp_path / "board.db")
    monkeypatch.setattr("syncmill.tui_jobs.subprocess.Popen", lambda *a, **k: object())
    with pytest.raises(JobError, match="does not match the store project root"):
        store.launch(BoardJobSpec(mode="loop"), cwd=tmp_path / "elsewhere")


def test_second_launch_same_target_fails_while_lock_is_held(tmp_path, monkeypatch):
    cfg = TuiConfig(state_dir=str(tmp_path / "tui"))
    store = JobStore(cfg, tmp_path, board_db=tmp_path / "board.db")
    key = store._target_key_for(BoardJobSpec(mode="loop"))
    fd = store._try_target_lock(key, "job-1")
    try:
        with pytest.raises(JobError, match="already active for this target"):
            store._try_target_lock(key, "job-2")
    finally:
        os.close(fd)


def _job(
    store: JobStore, job_id: str, status: str, created_at: float,
    *, lock_key: str = "target-key",
) -> Path:
    store.ensure()
    directory = store.jobs_dir / job_id
    directory.mkdir(mode=0o700)
    _atomic_json(
        directory / "spec.json",
        {
            "schema_version": 2,
            "job_id": job_id,
            "project_key": store.key,
            "lock_key": lock_key,
            "created_at": created_at,
        },
    )
    _atomic_json(
        directory / "state.json",
        {
            "schema_version": 2,
            "job_id": job_id,
            "lock_key": lock_key,
            "status": status,
        },
    )
    return directory


def test_rolling_log_rotates_with_bounded_segments(tmp_path):
    log = _RollingLog(tmp_path, 1024)
    log.write(b"a" * 600)
    log.write(b"b" * 600)
    log.close()
    previous = (tmp_path / "console.log.1").read_bytes()
    current = (tmp_path / "console.log").read_bytes()
    assert len(previous) <= 512 and len(current) <= 512
    assert (previous + current).endswith(b"b" * 600)
    assert (tmp_path / "console.log").stat().st_mode & 0o777 == 0o600


def test_active_job_marks_released_worker_lost(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    # A running row whose per-target lock is NOT held (worker gone) reconciles to
    # lost, and active_job/active_jobs then report nothing running.
    directory = _job(store, "lost-job", "running", 1, lock_key="orphan-target")
    assert store.active_job() is None
    assert store.active_jobs() == []
    assert json.loads((directory / "state.json").read_text())["status"] == "lost"


def test_active_jobs_requires_held_lock_and_matching_owner(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    # Owner mismatch: the target lock is held, but by a DIFFERENT job id (a
    # replacement took the target) → the stale row is lost, not active.
    directory = _job(store, "stale-job", "running", 1, lock_key="shared-target")
    held_fd = store._try_target_lock("shared-target", "newer-job")
    try:
        assert store.active_jobs() == []
        assert json.loads((directory / "state.json").read_text())["status"] == "lost"
    finally:
        os.close(held_fd)


def test_prune_retains_newest_terminal_jobs(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state"), keep_jobs=2), tmp_path / "board.db"
    )
    for number in range(4):
        _job(store, f"job-{number}", "finished", float(number))
    store.prune()
    assert {path.name for path in store.jobs_dir.iterdir()} == {"job-2", "job-3"}


def test_prune_never_touches_non_schema2_rows(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state"), keep_jobs=1), tmp_path / "board.db"
    )
    store.ensure()
    # A stray schema-1 (or future-version) terminal row under the project store is
    # NOT pruned — prune touches schema_version==2 only.
    legacy = store.jobs_dir / "legacy-job"
    legacy.mkdir(mode=0o700)
    _atomic_json(legacy / "spec.json", {"schema_version": 1, "job_id": "legacy-job"})
    _atomic_json(legacy / "state.json", {"job_id": "legacy-job", "status": "finished"})
    for number in range(3):
        _job(store, f"job-{number}", "finished", float(number))
    store.prune()
    names = {path.name for path in store.jobs_dir.iterdir()}
    assert "legacy-job" in names  # never pruned
    assert "job-2" in names and "job-0" not in names  # schema-2 retention applied


def test_target_lock_file_is_permanent_and_records_owner(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    key = store._target_key_for(BoardJobSpec(mode="loop"))
    fd = store._try_target_lock(key, "owner-1")
    lock_file = store.locks_dir / f"{key}.lock"
    assert lock_file.exists()
    assert store._lock_state(key) == (True, "owner-1")
    os.close(fd)
    # Released: the lock FILE persists (never unlinked — unlinking a held inode
    # would let a second launcher win a second lock) and is no longer held.
    assert lock_file.exists()
    assert store._lock_state(key) == (False, None)
    # A new owner reuses the SAME file and overwrites the recorded owner.
    fd2 = store._try_target_lock(key, "owner-2")
    try:
        assert store._lock_state(key) == (True, "owner-2")
    finally:
        os.close(fd2)


def test_legacy_schema1_jobs_read_as_ephemeral_overlay(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    legacy_jobs = store.legacy_root / "jobs"
    legacy_jobs.mkdir(parents=True)
    for jid, status in (("done-job", "finished"), ("live-job", "running"),
                        ("stale-job", "running")):
        directory = legacy_jobs / jid
        directory.mkdir()
        _atomic_json(directory / "spec.json",
                     {"schema_version": 1, "job_id": jid, "created_at": 1.0})
        _atomic_json(directory / "state.json", {"job_id": jid, "status": status})
    # No legacy worker.lock held → every non-terminal schema-1 row overlays to
    # lost; the terminal row is kept as-is; the dirs are never rewritten.
    rows = {r["job_id"]: r["status"] for r in store.list_jobs()}
    assert rows == {"done-job": "finished", "live-job": "lost", "stale-job": "lost"}
    assert json.loads((legacy_jobs / "live-job" / "state.json").read_text())[
        "status"
    ] == "running"  # overlay is READ-only, not written back
    # active.json points at live-job AND a schema-1 worker holds the legacy lock:
    # only the pointer reads as running; the unpointed row stays lost.
    _atomic_json(store.legacy_root / "active.json", {"job_id": "live-job"})
    held = store._try_legacy_lock()
    try:
        rows = {r["job_id"]: r["status"] for r in store.list_jobs()}
        assert rows["live-job"] == "running" and rows["stale-job"] == "lost"
    finally:
        os.close(held)


def test_manager_force_grace_enforced_server_side(tmp_path, monkeypatch):
    directory = tmp_path / "job"
    directory.mkdir()
    _atomic_json(
        directory / "spec.json",
        {"job_id": "job-1", "project_key": "key", "cwd": str(tmp_path)},
    )
    _atomic_json(directory / "state.json", {"job_id": "job-1", "status": "running"})
    (directory / "control.token").write_text("secret", encoding="ascii")
    manager = _Manager(directory, -1)
    manager.child = SimpleNamespace(pid=999999, poll=lambda: None)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "syncmill.tui_jobs.os.killpg", lambda pid, sig: signals.append((pid, sig))
    )
    force = {"job_id": "job-1", "token": "secret", "action": "force"}
    # No safe-stop yet: force is rejected SERVER-SIDE, no SIGKILL sent.
    with pytest.raises(JobError, match="10 seconds"):
        manager._handle_control(force)
    # A safe-stop within the grace window: still rejected.
    manager.state["cancel_requested_at"] = time.time()
    with pytest.raises(JobError, match="10 seconds"):
        manager._handle_control(force)
    assert signals == []
    # A safe-stop older than the grace window: force accepted → SIGKILL.
    manager.state["cancel_requested_at"] = time.time() - 20
    reply = manager._handle_control(force)
    assert reply["ok"] is True
    assert signals and signals[-1] == (999999, signal.SIGKILL)


def test_launch_apply_writes_frozen_config_snapshot(tmp_path, monkeypatch):
    from syncmill.config import SyncmillConfig

    cfg = SyncmillConfig(
        repo_root=str(tmp_path),
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "ws")},
    )
    store = JobStore(cfg.tui, tmp_path)  # apply ops need no board db
    captured = {}
    monkeypatch.setattr(
        "syncmill.tui_jobs.subprocess.Popen",
        lambda argv, **kw: captured.update(argv=argv, kwargs=kw) or object(),
    )
    row = store.launch(ArtifactApplySpec(run_id="run-7", verify=True), resolved_config=cfg)
    job_dir = Path(row["job_dir"])
    spec = json.loads((job_dir / "spec.json").read_text())
    # Argv routes to the apply worker with --repo <project_root> and the pinned
    # config snapshot appended by launch.
    snapshot = job_dir / "config-snapshot.json"
    assert spec["argv"] == [
        "apply-worker", "run-7", "--repo", str(tmp_path.resolve()),
        "--verify", "--resolved-config", str(snapshot),
    ]
    # The 0600 snapshot exists with the correctness paths absolutized to the root.
    assert (snapshot.stat().st_mode & 0o777) == 0o600
    payload = json.loads(snapshot.read_text())
    assert payload["repo_root"] == str(tmp_path.resolve())
    assert payload["runs"]["root"] == str((tmp_path / "runs").resolve())
    assert payload["workspace"]["root"] == str((tmp_path / "ws").resolve())
    # An apply op takes NO legacy bridge lock (that is board-run only).
    assert len(captured["kwargs"]["pass_fds"]) == 1


def test_launch_failure_unlinks_config_snapshot(tmp_path, monkeypatch):
    from syncmill.config import SyncmillConfig

    cfg = SyncmillConfig(
        repo_root=str(tmp_path),
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "ws")},
    )
    store = JobStore(cfg.tui, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("syncmill.tui_jobs.subprocess.Popen", boom)
    with pytest.raises(JobError, match="failed to launch"):
        store.launch(ArtifactApplySpec(run_id="run-7", verify=True), resolved_config=cfg)

    job_dirs = [p for p in store.jobs_dir.iterdir() if p.is_dir()]
    assert len(job_dirs) == 1
    job_dir = job_dirs[0]
    # The full resolved config must never linger behind a launch that never ran.
    assert not (job_dir / "config-snapshot.json").exists()
    # The launch_failed receipt survives for the TUI to surface.
    state = json.loads((job_dir / "state.json").read_text())
    assert state["status"] == "launch_failed"
    assert "boom" in state["error"]


def test_post_popen_error_never_reaps_a_launched_worker(tmp_path, monkeypatch):
    """A failure AFTER Popen returns (e.g. the bootstrap-log close raising) must
    NOT be misread as a failed launch: the worker is already running and holds
    its own bootstrap fd, so its config snapshot must survive and its receipt
    must not be flipped to launch_failed."""
    from syncmill.config import SyncmillConfig

    cfg = SyncmillConfig(
        repo_root=str(tmp_path),
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "ws")},
    )
    store = JobStore(cfg.tui, tmp_path)
    monkeypatch.setattr("syncmill.tui_jobs.subprocess.Popen", lambda *a, **k: object())

    real_open = Path.open

    class ExplodingClose:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self._fh

        def __exit__(self, *exc):
            self._fh.close()
            raise OSError("bootstrap close boom")

    def fake_open(self, *args, **kwargs):
        fh = real_open(self, *args, **kwargs)
        # Only the append-mode bootstrap log; other opens (token write, atomic
        # json temp files) are untouched.
        if args and args[0] == "ab":
            return ExplodingClose(fh)
        return fh

    monkeypatch.setattr(Path, "open", fake_open)

    row = store.launch(ArtifactApplySpec(run_id="run-7", verify=True), resolved_config=cfg)
    job_dir = Path(row["job_dir"])
    # The snapshot survives for the running worker to consume+unlink...
    assert (job_dir / "config-snapshot.json").exists()
    # ...and the receipt is NOT flipped to launch_failed.
    assert row["status"] == "starting"
    state = json.loads((job_dir / "state.json").read_text())
    assert state["status"] == "starting"


def test_board_launch_threads_legacy_bridge_fd(tmp_path, monkeypatch):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "tui")), tmp_path, board_db=tmp_path / "board.db"
    )
    captured = {}

    def fake_popen(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return object()

    monkeypatch.setattr("syncmill.tui_jobs.subprocess.Popen", fake_popen)
    store.launch(BoardJobSpec(mode="loop"), cwd=tmp_path)
    # The worker inherits BOTH the target lock and the schema-1 bridge lock, and
    # is told the bridge fd so it can thread it to the child/guardian.
    assert "--legacy-lock-fd" in captured["argv"]
    assert len(captured["kwargs"]["pass_fds"]) == 2


def _control_manager(tmp_path) -> _Manager:
    directory = tmp_path / "job"
    directory.mkdir()
    _atomic_json(
        directory / "spec.json",
        {"job_id": "job-1", "project_key": "key", "cwd": str(tmp_path)},
    )
    _atomic_json(directory / "state.json", {"job_id": "job-1", "status": "running"})
    (directory / "control.token").write_text("secret", encoding="ascii")
    return _Manager(directory, -1)


def test_manager_rejects_wrong_job_id_and_token(tmp_path):
    manager = _control_manager(tmp_path)
    with pytest.raises(JobError, match="job id mismatch"):
        manager._handle_control({"job_id": "other", "token": "secret", "action": "stop"})
    with pytest.raises(JobError, match="invalid control token"):
        manager._handle_control({"job_id": "job-1", "token": "wrong", "action": "stop"})


def test_finalize_is_one_way_and_refuses_late_control(tmp_path, monkeypatch):
    # The ack-invariant race #1: once the run finalizes a terminal state, a late
    # control request must be REFUSED, never overwriting it and never signaling.
    manager = _control_manager(tmp_path)
    manager.child = SimpleNamespace(pid=1, poll=lambda: None)
    signals: list = []
    monkeypatch.setattr("syncmill.tui_jobs.os.killpg", lambda pid, sig: signals.append(sig))
    assert manager._finalize_completion(0) == 0
    assert manager.state["status"] == "finished"
    # _apply_control is refused post-finalize BEFORE any signal is delivered.
    with pytest.raises(JobError, match="finalizing"):
        manager._apply_control("stop")
    assert signals == []
    assert json.loads((manager.state_path).read_text())["status"] == "finished"


def test_control_before_finalize_is_honored_as_terminal(tmp_path, monkeypatch):
    # The other interleaving: control wins the race and sets the intent BEFORE the
    # run finalizes → the terminal state reflects it (cancelled), the flag is not
    # lost, and the intent is committed only AFTER the signal is delivered.
    manager = _control_manager(tmp_path)
    manager.child = SimpleNamespace(pid=7, poll=lambda: None)
    signals: list = []
    monkeypatch.setattr("syncmill.tui_jobs.os.killpg", lambda pid, sig: signals.append((pid, sig)))
    assert manager._apply_control("stop") == "cancel_requested"
    assert signals == [(7, signal.SIGINT)]
    assert manager.state["status"] == "cancel_requested"
    assert manager._finalize_completion(0) == 130
    assert manager.state["status"] == "cancelled"


def test_control_not_committed_when_signal_fails(tmp_path, monkeypatch):
    # A signal the kernel rejects must NOT leave a misleading cancelled receipt.
    manager = _control_manager(tmp_path)
    manager.child = SimpleNamespace(pid=7, poll=lambda: None)

    def deny(pid, sig):
        raise ProcessLookupError("gone")

    monkeypatch.setattr("syncmill.tui_jobs.os.killpg", deny)
    with pytest.raises(JobError, match="could not signal worker"):
        manager._apply_control("stop")
    assert manager.state["status"] == "running" or manager.state["status"] == "starting"
    assert manager._cancel_requested is False


def test_mark_running_does_not_clobber_an_accepted_control(tmp_path, monkeypatch):
    # A stop accepted between Popen and the running write must survive: _mark_running
    # keeps a non-'starting' control status.
    manager = _control_manager(tmp_path)
    manager.child = SimpleNamespace(pid=9, poll=lambda: None)
    monkeypatch.setattr("syncmill.tui_jobs.os.killpg", lambda pid, sig: None)
    assert manager._apply_control("stop") == "cancel_requested"
    manager._mark_running(9)  # the run's own running write, AFTER the control
    assert manager.state["status"] == "cancel_requested"
    assert manager.state["pid"] == 9  # the running metadata is still recorded
    # The cancel flag survives → the run finalizes as cancelled.
    assert manager._finalize_completion(0) == 130
    assert manager.state["status"] == "cancelled"


def test_worker_main_preserves_terminal_receipt_on_post_ack_cleanup_error(
    tmp_path, monkeypatch
):
    # The Blocker: a crash in run()'s finally AFTER a clean finalize/ack reaches
    # _worker_main, which must NOT overwrite the terminal receipt with launch_failed.
    directory = tmp_path / "job"
    directory.mkdir()
    _atomic_json(
        directory / "spec.json",
        {"job_id": "job-1", "project_key": "key", "cwd": str(tmp_path)},
    )
    _atomic_json(directory / "state.json", {"job_id": "job-1", "status": "starting"})
    (directory / "control.token").write_text("secret", encoding="ascii")

    def fake_run(self):
        self._finalize_completion(0)  # durable terminal + ack already sent
        raise RuntimeError("post-ack cleanup boom")

    monkeypatch.setattr(_Manager, "run", fake_run)
    with pytest.raises(RuntimeError, match="post-ack cleanup boom"):
        _worker_main(SimpleNamespace(job_dir=str(directory), lock_fd=-1, legacy_lock_fd=-1))
    # The finished receipt is preserved, NOT overwritten with launch_failed.
    assert json.loads((directory / "state.json").read_text())["status"] == "finished"


def test_crash_after_finalize_does_not_overwrite_terminal(tmp_path):
    # The ack-invariant race #2: an async exception after a clean finalize enters
    # the crash path, whose _finalize_crash must be a NO-OP — the terminal state
    # (and the already-sent ack's meaning) are preserved.
    manager = _control_manager(tmp_path)
    manager._finalize_completion(0)
    manager._finalize_crash(status="lost", finished_at=1.0, error="boom")
    assert manager.state["status"] == "finished"
    assert "error" not in manager.state


def test_guardian_records_lost_when_process_group_signals_are_denied(
    tmp_path, monkeypatch
):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    _atomic_json(job_dir / "state.json", {"job_id": "job-1", "status": "running"})
    manager_read, manager_write = os.pipe()
    child_read, child_write = os.pipe()
    os.close(manager_write)

    def deny_signal(pgid, sig):
        raise PermissionError("not our process group")

    monkeypatch.setattr("syncmill.tui_jobs.os.killpg", deny_signal)
    try:
        assert _guardian_main(
            SimpleNamespace(
                manager_fd=manager_read,
                child_fd=child_read,
                pgid=999999,
                grace=0,
                job_dir=str(job_dir),
            )
        ) == 0
    finally:
        os.close(manager_read)
        os.close(child_read)
        os.close(child_write)
    state = json.loads((job_dir / "state.json").read_text())
    assert state["status"] == "lost"
    # Never landed a kill and the group still reports present → honestly say it
    # may still be running (do NOT claim "force-stopped").
    assert "may still be running" in state["error"]


def test_guardian_preserves_terminal_receipt_on_eof_without_ack(
    tmp_path, monkeypatch
):
    # PR #78 review: the latch guarantees ack ⟹ durable-terminal, NOT the
    # converse — a manager SIGKILLed between _finalize_completion's atomic
    # "finished" write and the ack leaves a genuine terminal receipt with no
    # ack. The guardian's EOF-without-ack path must preserve it (the
    # _worker_main _TERMINAL rule), never clobber it to "lost".
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    _atomic_json(
        job_dir / "state.json",
        {"job_id": "job-1", "status": "finished", "exit_code": 0},
    )
    manager_read, manager_write = os.pipe()
    child_read, child_write = os.pipe()
    os.close(manager_write)  # manager EOF, no ack
    kills: list[tuple[int, int]] = []

    def record_kill(pgid, sig):
        kills.append((pgid, sig))
        raise ProcessLookupError  # group already gone

    monkeypatch.setattr("syncmill.tui_jobs.os.killpg", record_kill)
    try:
        assert _guardian_main(
            SimpleNamespace(
                manager_fd=manager_read,
                child_fd=child_read,
                pgid=999999,
                grace=0,
                job_dir=str(job_dir),
            )
        ) == 0
    finally:
        os.close(manager_read)
        os.close(child_read)
        os.close(child_write)
    # Group cleanup still ran — only the state write is guarded.
    assert kills
    state = json.loads((job_dir / "state.json").read_text())
    assert state["status"] == "finished"
    assert state["exit_code"] == 0
    assert "error" not in state


def test_guardian_re_sigkills_until_group_gone(tmp_path, monkeypatch):
    # Blocker B (deterministic): the group survives the first post-SIGKILL wait,
    # so the guardian must SIGKILL again and only publish state once the group
    # reports gone — never releasing (returning) while a killable member survives.
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    _atomic_json(job_dir / "state.json", {"job_id": "job-1", "status": "running"})
    manager_read, manager_write = os.pipe()
    child_read, child_write = os.pipe()
    os.close(manager_write)  # manager EOF, no ack -> cleanup

    # Report the group present for a few checks, then gone; count SIGKILLs.
    alive = {"checks": 3}
    signals: list[int] = []

    def fake_exists(pgid):
        if alive["checks"] > 0:
            alive["checks"] -= 1
            return True
        return False

    def fake_killpg(pgid, sig):
        signals.append(sig)

    monkeypatch.setattr("syncmill.tui_jobs._process_group_exists", fake_exists)
    monkeypatch.setattr("syncmill.tui_jobs.os.killpg", fake_killpg)
    # Make the grace wait a no-op that reports "still present" so the outer while
    # loop is the thing that drives the group-gone decision.
    monkeypatch.setattr("syncmill.tui_jobs._wait_for_group_exit", lambda pgid, t: False)
    try:
        assert _guardian_main(
            SimpleNamespace(
                manager_fd=manager_read, child_fd=child_read,
                pgid=4242, grace=0.01, job_dir=str(job_dir),
            )
        ) == 0
    finally:
        os.close(manager_read)
        os.close(child_read)
        os.close(child_write)
    assert signals[0] == signal.SIGINT
    # The group outlived the first SIGKILL → more than one SIGKILL was sent.
    assert signals.count(signal.SIGKILL) >= 2
    state = json.loads((job_dir / "state.json").read_text())
    assert state["status"] == "lost" and "force-stopped" in state["error"]


def test_linux_process_group_ignores_zombie_only_group(tmp_path, monkeypatch):
    monkeypatch.setattr("syncmill.tui_jobs.sys.platform", "linux")
    proc = tmp_path / "proc"
    zombie = proc / "101"
    zombie.mkdir(parents=True)
    (zombie / "stat").write_text(
        "101 (worker with ) parens) Z 1 4242 4242 0 0\n", encoding="utf-8"
    )

    assert _linux_process_group_has_live_members(4242, proc) is False

    live = proc / "102"
    live.mkdir()
    (live / "stat").write_text(
        "102 (survivor) S 1 4242 4242 0 0\n", encoding="utf-8"
    )

    assert _linux_process_group_has_live_members(4242, proc) is True


def test_force_stop_rejected_during_grace_period(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    directory = _job(store, "active-job", "cancel_requested", time.time())
    state = json.loads((directory / "state.json").read_text())
    state["cancel_requested_at"] = time.time()
    _atomic_json(directory / "state.json", state)
    # control now addresses a specific job id; the client-side grace gate rejects
    # before any socket connection.
    with pytest.raises(JobError, match="10 seconds"):
        store.control("active-job", "force")


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in Unix socket/process manager test",
)
def test_manager_runs_same_interpreter_cli_and_writes_receipt(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir(mode=0o700)
    key = board_key(tmp_path / "board.db")
    _atomic_json(
        job_dir / "spec.json",
        {
            "schema_version": 1,
            "job_id": "job-1",
            "project_key": key,
            "cwd": str(tmp_path),
            "argv": ["--version"],
            "log_max_bytes": 4096,
        },
    )
    _atomic_json(job_dir / "state.json", {"job_id": "job-1", "status": "starting"})
    (job_dir / "control.token").write_text("token", encoding="ascii")
    os.chmod(job_dir / "control.token", 0o600)
    lock_fd = os.open(tmp_path / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert _Manager(job_dir, lock_fd).run() == 0
    finally:
        os.close(lock_fd)
    state = json.loads((job_dir / "state.json").read_text())
    assert state["status"] == "finished" and state["exit_code"] == 0
    assert "syncmill" in (job_dir / "console.log").read_text().lower()


def _prepare_long_board_job(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    db_path = tmp_path / "board.db"
    board = Board(str(db_path), lease_timeout_seconds=30)
    item = board.add("cancel me")
    board.close()
    (repo / "syncmill.toml").write_text(
        f'repo_root = "{repo}"\n'
        'agent_priority = ["claude"]\n'
        '[board]\n'
        'enabled = true\n'
        f'db_path = "{db_path}"\n'
        '[memory]\n'
        f'db_path = "{tmp_path / "memory.db"}"\n'
    )

    store = JobStore(TuiConfig(state_dir=str(tmp_path / "state")), repo, board_db=db_path)
    store.ensure()
    job_id = "cancel-job"
    lock_key = store._target_key_for(
        BoardJobSpec(mode="selected", item_id=item.id, agent="claude")
    )
    job_dir = store.jobs_dir / job_id
    job_dir.mkdir(mode=0o700)
    token = "safe-token"
    _atomic_json(
        job_dir / "spec.json",
        {
            "schema_version": 2,
            "job_id": job_id,
            "op": "board-run",
            "project_key": store.key,
            "project_root": str(repo),
            "board_db": str(db_path),
            "lock_key": lock_key,
            "cwd": str(repo),
            "argv": ["board", "run", "--id", item.id, "--agent", "claude"],
            "log_max_bytes": 1024 * 1024,
        },
    )
    _atomic_json(
        job_dir / "state.json",
        {"job_id": job_id, "lock_key": lock_key, "status": "starting"},
    )
    (job_dir / "control.token").write_text(token)
    os.chmod(job_dir / "control.token", 0o600)
    lock_fd = store._try_target_lock(lock_key, job_id)
    return repo, db_path, item, store, job_dir, token, lock_key, lock_fd


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in real process-group/socket test",
)
def test_manager_safe_stop_cleans_board_claim(tmp_path, monkeypatch):
    repo, db_path, item, store, job_dir, token, lock_key, lock_fd = (
        _prepare_long_board_job(tmp_path, monkeypatch)
    )
    config_path = repo / "syncmill.toml"
    config_path.write_text(
        config_path.read_text().replace(str(db_path), str(tmp_path / "changed.db"), 1)
    )
    job_id = "cancel-job"
    manager = _Manager(job_dir, lock_fd)
    thread = threading.Thread(target=manager.run)
    thread.start()
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            state = json.loads((job_dir / "state.json").read_text())
            probe = Board(str(db_path))
            try:
                current = probe.get(item.id)
            finally:
                probe.close()
            if state.get("status") == "running" and current.state is WorkState.RUNNING:
                break
            time.sleep(0.05)
        else:
            pytest.fail("manager never claimed the board item")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(_socket_path(job_id)))
            client.sendall(
                json.dumps({"job_id": job_id, "token": token, "action": "stop"}).encode()
            )
            assert json.loads(client.recv(4096))["ok"] is True
        thread.join(timeout=15)
        assert not thread.is_alive()
        state = json.loads((job_dir / "state.json").read_text())
        assert state["status"] == "cancelled" and state["exit_code"] == 130
        checked_board = Board(str(db_path))
        try:
            checked = checked_board.get(item.id)
            assert checked.state is WorkState.BLOCKED and checked.human_required is True
            assert "cancelled by operator" in checked_board.transitions(item.id)[-1]["note"]
        finally:
            checked_board.close()
    finally:
        os.close(lock_fd)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in real manager-death recovery test",
)
def test_manager_death_stops_child_and_releases_lock(tmp_path, monkeypatch):
    repo, db_path, item, store, job_dir, _, lock_key, lock_fd = (
        _prepare_long_board_job(tmp_path, monkeypatch)
    )
    manager = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "syncmill.tui_jobs",
            "worker",
            "--job-dir",
            str(job_dir),
            "--lock-fd",
            str(lock_fd),
        ],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        pass_fds=(lock_fd,),
    )
    os.close(lock_fd)
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            state = json.loads((job_dir / "state.json").read_text())
            probe = Board(str(db_path))
            try:
                current = probe.get(item.id)
            finally:
                probe.close()
            if state.get("status") == "running" and current.state is WorkState.RUNNING:
                break
            time.sleep(0.05)
        else:
            pytest.fail("manager never claimed the board item")

        os.kill(manager.pid, signal.SIGKILL)
        manager.wait(timeout=5)
        deadline = time.time() + 15
        while time.time() < deadline:
            state = json.loads((job_dir / "state.json").read_text())
            if state.get("status") == "lost" and not store._lock_state(lock_key)[0]:
                break
            time.sleep(0.05)
        else:
            pytest.fail("guardian did not stop the orphan and release the lock")

        checked = Board(str(db_path))
        try:
            current = checked.get(item.id)
            assert current.state is WorkState.BLOCKED and current.human_required is True
        finally:
            checked.close()
        replacement_lock = store._try_target_lock(lock_key, "replacement")
        os.close(replacement_lock)
    finally:
        if manager.poll() is None:
            os.killpg(manager.pid, signal.SIGKILL)
            manager.wait(timeout=5)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in guardian escalation test",
)
def test_guardian_force_stops_sigint_ignoring_group(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    job_dir = _job(store, "orphan-job", "running", time.time())
    lock_fd = store._try_target_lock("target-key", "orphan-job")
    target = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)",
        ],
        start_new_session=True,
    )
    manager_read, manager_write = os.pipe()
    child_read, child_write = os.pipe()
    guardian = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "syncmill.tui_jobs",
            "guardian",
            "--job-dir",
            str(job_dir),
            "--lock-fd",
            str(lock_fd),
            "--manager-fd",
            str(manager_read),
            "--child-fd",
            str(child_read),
            "--pgid",
            str(target.pid),
            "--grace",
            "0.1",
        ],
        start_new_session=True,
        close_fds=True,
        pass_fds=(lock_fd, manager_read, child_read),
    )
    os.close(lock_fd)
    os.close(manager_read)
    os.close(child_read)
    os.close(manager_write)
    try:
        guardian.wait(timeout=5)
        target.wait(timeout=5)
        assert target.returncode == -signal.SIGKILL
        state = json.loads((job_dir / "state.json").read_text())
        assert state["status"] == "lost"
        assert "force-stopped" in state["error"]
        replacement_lock = store._try_target_lock("target-key", "replacement")
        os.close(replacement_lock)
    finally:
        os.close(child_write)
        if target.poll() is None:
            os.killpg(target.pid, signal.SIGKILL)
            target.wait(timeout=5)


def _spawn_guardian(job_dir, *, lock_fd, pgid, manager_read, child_read, grace,
                    legacy_lock_fd=None):
    argv = [
        sys.executable, "-m", "syncmill.tui_jobs", "guardian",
        "--job-dir", str(job_dir),
        "--lock-fd", str(lock_fd),
        "--manager-fd", str(manager_read),
        "--child-fd", str(child_read),
        "--pgid", str(pgid),
        "--grace", str(grace),
    ]
    pass_fds = [lock_fd, manager_read, child_read]
    if legacy_lock_fd is not None:
        argv += ["--legacy-lock-fd", str(legacy_lock_fd)]
        pass_fds.append(legacy_lock_fd)
    return subprocess.Popen(
        argv, start_new_session=True, close_fds=True, pass_fds=tuple(pass_fds)
    )


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in guardian child-EOF-then-manager-death test",
)
def test_guardian_cleans_group_when_child_exits_before_manager_death(tmp_path):
    # The Blocker: child EOF (the CLI finishing) must NOT make the guardian exit —
    # a SIGINT-ignoring descendant can survive and the manager can die right after
    # WITHOUT a clean-completion ack. The guardian must ignore child EOF, then on
    # manager death (no ack) tear the surviving group down, holding the lock
    # throughout. The OLD impl returned 0 on child EOF and orphaned the survivor.
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    job_dir = _job(store, "orphan-job", "running", time.time())
    lock_fd = store._try_target_lock("target-key", "orphan-job")
    survivor = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)",
        ],
        start_new_session=True,
    )
    manager_read, manager_write = os.pipe()
    child_read, child_write = os.pipe()
    guardian = _spawn_guardian(
        job_dir, lock_fd=lock_fd, pgid=survivor.pid,
        manager_read=manager_read, child_read=child_read, grace="0.3",
    )
    os.close(lock_fd)
    os.close(manager_read)
    os.close(child_read)
    try:
        # The child CLI "finishes": close its completion pipe (child EOF).
        os.close(child_write)
        # The guardian must NOT exit on child EOF — it keeps waiting on the
        # manager AND keeps holding the lock while the survivor is alive.
        time.sleep(0.4)
        assert guardian.poll() is None, "guardian exited on child EOF"
        assert survivor.poll() is None
        assert store._lock_state("target-key")[0], "guardian released the lock early"
        # The manager now dies WITHOUT a clean-completion ack.
        os.close(manager_write)
        guardian.wait(timeout=10)
        survivor.wait(timeout=10)
        assert survivor.returncode == -signal.SIGKILL
        state = json.loads((job_dir / "state.json").read_text())
        assert state["status"] == "lost" and "force-stopped" in state["error"]
        replacement = store._try_target_lock("target-key", "replacement")
        os.close(replacement)
    finally:
        if guardian.poll() is None:
            guardian.wait(timeout=5)
        if survivor.poll() is None:
            os.killpg(survivor.pid, signal.SIGKILL)
            survivor.wait(timeout=5)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in guardian clean-ack test",
)
def test_guardian_clean_ack_exits_without_touching_group(tmp_path):
    # A clean-completion ack from the manager: the guardian exits 0 WITHOUT
    # tearing the group down or overwriting the manager's terminal state.
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    job_dir = _job(store, "done-job", "finished", time.time())
    lock_fd = store._try_target_lock("target-key", "done-job")
    survivor = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    manager_read, manager_write = os.pipe()
    child_read, child_write = os.pipe()
    guardian = _spawn_guardian(
        job_dir, lock_fd=lock_fd, pgid=survivor.pid,
        manager_read=manager_read, child_read=child_read, grace="0.3",
    )
    os.close(lock_fd)
    os.close(manager_read)
    os.close(child_read)
    try:
        # The manager durably recorded the outcome and acks, then closes the pipe.
        os.write(manager_write, _MANAGER_DONE)
        os.close(manager_write)
        assert guardian.wait(timeout=10) == 0
        # The group was NOT touched (clean completion, not a manager death).
        assert survivor.poll() is None
        # The terminal state the manager wrote is untouched (guardian did not
        # overwrite it with "lost").
        assert json.loads((job_dir / "state.json").read_text())["status"] == "finished"
    finally:
        os.close(child_write)
        if survivor.poll() is None:
            os.killpg(survivor.pid, signal.SIGKILL)
            survivor.wait(timeout=5)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in guardian legacy-bridge retention test",
)
def test_guardian_holds_legacy_bridge_until_group_dies(tmp_path):
    # The Blocker regression: the guardian must RETAIN the legacy schema-1 bridge
    # lock through the whole manager-death cleanup — including while a
    # SIGINT-ignoring descendant survives the grace period — and only release it
    # once the group is truly gone.
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state")), tmp_path, board_db=tmp_path / "board.db"
    )
    job_dir = _job(store, "orphan-job", "running", time.time())
    lock_fd = store._try_target_lock("target-key", "orphan-job")
    legacy_fd = store._try_legacy_lock()  # the schema-1 bridge lock
    target = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(60)",
        ],
        start_new_session=True,
    )
    manager_read, manager_write = os.pipe()
    child_read, child_write = os.pipe()
    guardian = subprocess.Popen(
        [
            sys.executable, "-m", "syncmill.tui_jobs", "guardian",
            "--job-dir", str(job_dir),
            "--lock-fd", str(lock_fd),
            "--legacy-lock-fd", str(legacy_fd),
            "--manager-fd", str(manager_read),
            "--child-fd", str(child_read),
            "--pgid", str(target.pid),
            "--grace", "0.6",
        ],
        start_new_session=True,
        close_fds=True,
        pass_fds=(lock_fd, legacy_fd, manager_read, child_read),
    )
    # Release OUR copies — only the guardian must now hold both locks.
    os.close(lock_fd)
    os.close(legacy_fd)
    os.close(manager_read)
    os.close(child_read)
    os.close(manager_write)  # signal manager loss
    try:
        # While the SIGINT-ignoring descendant is alive (during the grace window),
        # the guardian still holds the legacy bridge → it is unavailable.
        observed_held = False
        deadline = time.time() + 3
        while time.time() < deadline and target.poll() is None:
            if store._legacy_lock_held():
                observed_held = True
                break
            time.sleep(0.02)
        assert observed_held, "guardian released the bridge before the group died"
        guardian.wait(timeout=5)
        target.wait(timeout=5)
        assert target.returncode == -signal.SIGKILL
        # Group gone → guardian exited → both locks released.
        released = store._try_legacy_lock()
        os.close(released)
        replacement = store._try_target_lock("target-key", "replacement")
        os.close(replacement)
    finally:
        os.close(child_write)
        if target.poll() is None:
            os.killpg(target.pid, signal.SIGKILL)
            target.wait(timeout=5)
