from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

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
    _atomic_json,
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


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in real process-group/socket test",
)
def test_manager_safe_stop_cleans_board_claim(tmp_path, monkeypatch):
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
            "cwd": str(repo),
            "argv": ["board", "run", "--id", item.id, "--agent", "claude"],
            "log_max_bytes": 1024 * 1024,
        },
    )
    _atomic_json(job_dir / "state.json", {"job_id": job_id, "status": "starting"})
    (job_dir / "control.token").write_text(token)
    os.chmod(job_dir / "control.token", 0o600)
    _atomic_json(store.active_path, {"job_id": job_id})
    lock_fd = store._try_lock()
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
