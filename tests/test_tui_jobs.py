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
    BoardJobSpec,
    JobError,
    JobStore,
    board_key,
    build_job_argv,
    canonical_board_path,
    _Manager,
    _RollingLog,
    _atomic_json,
    _guardian_main,
    _socket_path,
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
    store = JobStore(cfg, tmp_path / "board.db")
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
    assert "environment" not in spec and "task" not in spec
    assert (job_dir.stat().st_mode & 0o777) == 0o700
    assert ((job_dir / "spec.json").stat().st_mode & 0o777) == 0o600


def test_second_store_launch_fails_while_lock_is_held(tmp_path):
    cfg = TuiConfig(state_dir=str(tmp_path / "tui"))
    store = JobStore(cfg, tmp_path / "board.db")
    fd = store._try_lock()
    try:
        with pytest.raises(JobError, match="already active"):
            JobStore(cfg, tmp_path / "board.db")._try_lock()
    finally:
        os.close(fd)


def _job(store: JobStore, job_id: str, status: str, created_at: float) -> Path:
    store.ensure()
    directory = store.jobs_dir / job_id
    directory.mkdir(mode=0o700)
    _atomic_json(
        directory / "spec.json",
        {"job_id": job_id, "project_key": store.key, "created_at": created_at},
    )
    _atomic_json(directory / "state.json", {"job_id": job_id, "status": status})
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
    store = JobStore(TuiConfig(state_dir=str(tmp_path / "state")), tmp_path / "board.db")
    directory = _job(store, "lost-job", "running", 1)
    store.lock_path.touch(mode=0o600)
    _atomic_json(store.active_path, {"job_id": "lost-job"})
    assert store.active_job() is None
    assert json.loads((directory / "state.json").read_text())["status"] == "lost"


def test_prune_retains_newest_terminal_jobs(tmp_path):
    store = JobStore(
        TuiConfig(state_dir=str(tmp_path / "state"), keep_jobs=2), tmp_path / "board.db"
    )
    for number in range(4):
        _job(store, f"job-{number}", "finished", float(number))
    store.prune()
    assert {path.name for path in store.jobs_dir.iterdir()} == {"job-2", "job-3"}


def test_manager_rejects_wrong_job_id_and_token(tmp_path):
    directory = tmp_path / "job"
    directory.mkdir()
    _atomic_json(
        directory / "spec.json",
        {"job_id": "job-1", "project_key": "key", "cwd": str(tmp_path)},
    )
    _atomic_json(directory / "state.json", {"job_id": "job-1", "status": "running"})
    (directory / "control.token").write_text("secret", encoding="ascii")
    manager = _Manager(directory, -1)
    with pytest.raises(JobError, match="job id mismatch"):
        manager._handle_control({"job_id": "other", "token": "secret", "action": "stop"})
    with pytest.raises(JobError, match="invalid control token"):
        manager._handle_control({"job_id": "job-1", "token": "wrong", "action": "stop"})


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
    assert "force-stopped" in state["error"]


def test_force_stop_rejected_during_grace_period(tmp_path):
    store = JobStore(TuiConfig(state_dir=str(tmp_path / "state")), tmp_path / "board.db")
    directory = _job(store, "active-job", "cancel_requested", time.time())
    state = json.loads((directory / "state.json").read_text())
    state["cancel_requested_at"] = time.time()
    _atomic_json(directory / "state.json", state)
    _atomic_json(store.active_path, {"job_id": "active-job"})
    lock_fd = store._try_lock()
    try:
        with pytest.raises(JobError, match="10 seconds"):
            store.control("force")
    finally:
        os.close(lock_fd)


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

    store = JobStore(TuiConfig(state_dir=str(tmp_path / "state")), db_path)
    store.ensure()
    job_id = "cancel-job"
    job_dir = store.jobs_dir / job_id
    job_dir.mkdir(mode=0o700)
    token = "safe-token"
    _atomic_json(
        job_dir / "spec.json",
        {
            "schema_version": 1,
            "job_id": job_id,
            "project_key": store.key,
            "board_db": str(db_path),
            "cwd": str(repo),
            "argv": ["board", "run", "--id", item.id, "--agent", "claude"],
            "log_max_bytes": 1024 * 1024,
        },
    )
    _atomic_json(job_dir / "state.json", {"job_id": job_id, "status": "starting"})
    (job_dir / "control.token").write_text(token)
    os.chmod(job_dir / "control.token", 0o600)
    _atomic_json(store.active_path, {"job_id": job_id})
    return repo, db_path, item, store, job_dir, token, store._try_lock()


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in real process-group/socket test",
)
def test_manager_safe_stop_cleans_board_claim(tmp_path, monkeypatch):
    repo, db_path, item, store, job_dir, token, lock_fd = _prepare_long_board_job(
        tmp_path, monkeypatch
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
            client.connect(str(_socket_path(store.key)))
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
    repo, db_path, item, store, job_dir, _, lock_fd = _prepare_long_board_job(
        tmp_path, monkeypatch
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
            if state.get("status") == "lost" and not store._lock_held():
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
        replacement_lock = store._try_lock()
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
    store = JobStore(TuiConfig(state_dir=str(tmp_path / "state")), tmp_path / "board.db")
    job_dir = _job(store, "orphan-job", "running", time.time())
    lock_fd = store._try_lock()
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
        replacement_lock = store._try_lock()
        os.close(replacement_lock)
    finally:
        os.close(child_write)
        if target.poll() is None:
            os.killpg(target.pid, signal.SIGKILL)
            target.wait(timeout=5)
