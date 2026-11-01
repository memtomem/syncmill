from __future__ import annotations

import os
import pty
import subprocess
import sys
import time
from pathlib import Path

import pytest

import syncmill.isolated_session as isolated
import syncmill.session as session_module
from syncmill.config import SyncmillConfig
from syncmill.isolated_session import IsolatedSessionStore


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("SYNCMILL_TEST_TMUX") != "1",
        reason="opt-in real tmux create/attach/reconnect test",
    ),
]


def _pane_path(name: str) -> Path | None:
    cwd = isolated._tmux(
        "list-panes", "-t", isolated._exact(name), "-F", "#{pane_current_path}"
    )
    assert cwd.returncode == 0, cwd.stderr
    text = cwd.stdout.strip()
    return Path(text).resolve() if text else None


def _wait_for_pane_path(name: str, expected: Path, seconds: float = 10.0) -> Path | None:
    """The pane's reported cwd, polled until it equals ``expected`` or time runs
    out: tmux spawns the pane process asynchronously after new-session returns."""
    deadline = time.time() + seconds
    reported = _pane_path(name)
    while reported != expected and time.time() < deadline:
        time.sleep(0.05)
        reported = _pane_path(name)
    return reported


def _pane_diagnostics(name: str, reported: Path | None, expected: Path) -> str:
    """Everything needed to tell a tmux reporting change from a pane that really
    runs outside its worktree (which would break session isolation)."""
    fields = isolated._tmux(
        "list-panes", "-t", isolated._exact(name), "-F",
        "pid=#{pane_pid} cmd=#{pane_current_command} start=#{pane_start_path} "
        "current=#{pane_current_path} dead=#{pane_dead}",
    ).stdout.strip()
    version = subprocess.run(["tmux", "-V"], capture_output=True, text=True).stdout.strip()
    proc_cwd = "unavailable"
    pid = fields.split(" ", 1)[0].removeprefix("pid=")
    if pid.isdigit():
        try:
            proc_cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError as exc:
            proc_cwd = f"unreadable ({exc})"
    return (
        f"pane cwd {reported} != worktree {expected}; {version}; {fields}; "
        f"/proc/<pane_pid>/cwd={proc_cwd}; test cwd={os.getcwd()}"
    )


def test_real_tmux_create_attach_reconnect_and_close(
    monkeypatch, git_repo: Path, tmp_path: Path
):
    config = SyncmillConfig(
        project={"id": "real-tmux-session"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
    )
    monkeypatch.setitem(
        session_module._INTERACTIVE,
        "claude",
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    store = IsolatedSessionStore(config, git_repo)
    value = store.create("claude")
    name = value["tmux_name"]
    try:
        deadline = time.time() + 10
        while time.time() < deadline and not isolated.session_exists(name):
            time.sleep(0.05)
        assert isolated.session_exists(name)
        expected = Path(value["worktree_path"]).resolve()
        reported = _wait_for_pane_path(name, expected)
        assert reported == expected, _pane_diagnostics(name, reported, expected)

        env = {key: val for key, val in os.environ.items() if key not in {"TMUX", "TMUX_PANE"}}
        # GitHub Actions does not guarantee TERM for non-interactive steps.
        # The PTY is real, so give the tmux client a deterministic terminal
        # description instead of letting it exit before attaching.
        env["TERM"] = "xterm-256color"
        child_pid, master = pty.fork()
        if child_pid == 0:  # pragma: no cover - executed in the PTY child
            os.execvpe("tmux", ["tmux", "attach-session", "-t", f"={name}"], env)
        try:
            deadline = time.time() + 10
            while time.time() < deadline:
                status = subprocess.run(
                    ["tmux", "list-sessions", "-F",
                     "#{session_name} #{session_attached}"],
                    capture_output=True,
                    text=True,
                )
                if status.returncode == 0 and f"{name} 1" in status.stdout.splitlines():
                    break
                time.sleep(0.05)
            else:
                pytest.fail("real tmux client never attached")
            detached = subprocess.run(
                ["tmux", "detach-client", "-s", f"={name}"],
                capture_output=True,
                text=True,
            )
            assert detached.returncode == 0, detached.stderr
            deadline = time.time() + 10
            child_status: int | None = None
            while time.time() < deadline:
                waited, status_value = os.waitpid(child_pid, os.WNOHANG)
                if waited == child_pid:
                    child_status = status_value
                    break
                time.sleep(0.05)
            assert child_status is not None, "tmux attach did not exit after detach"
            assert os.waitstatus_to_exitcode(child_status) == 0
        finally:
            os.close(master)
            try:
                waited, _ = os.waitpid(child_pid, os.WNOHANG)
            except ChildProcessError:
                waited = child_pid
            if waited == 0:
                os.kill(child_pid, 15)
                os.waitpid(child_pid, 0)

        reconnected = IsolatedSessionStore(config, git_repo)
        assert reconnected.list()[0]["live"] is True
        monkeypatch.setattr(isolated, "_attach_and_report", lambda *_: 0)
        assert reconnected.attach(value["session_id"]) == 0
        assert reconnected.close_session(value["session_id"])["state"] == "closed"
        assert not isolated.session_exists(name)
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={name}"],
            capture_output=True,
        )


def test_session_survives_creator_exit_and_generic_sweep(git_repo, tmp_path):
    import json
    from syncmill.worktree import sweep_orphaned_workspaces

    config = SyncmillConfig(
        project={"id": "creator-exit"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
    )
    receipt = tmp_path / 'created.json'
    script = '''
import json, sys
from pathlib import Path
from syncmill.config import SyncmillConfig
from syncmill.isolated_session import IsolatedSessionStore
import syncmill.session as session
session._INTERACTIVE['claude'] = [sys.executable, '-c', 'import time; time.sleep(60)']
config = SyncmillConfig.model_validate_json(sys.argv[1])
value = IsolatedSessionStore(config, Path(sys.argv[2])).create('claude')
Path(sys.argv[3]).write_text(json.dumps(value))
'''
    # The real tmux session is created in another process, whose exit used to
    # turn the session's workspace into an orphan eligible for deletion.
    subprocess.run([sys.executable, '-c', script, config.model_dump_json(),
                    str(git_repo), str(receipt)], check=True, timeout=20)
    value = json.loads(receipt.read_text())
    store = IsolatedSessionStore(config, git_repo)
    try:
        assert isolated.session_exists(value['tmux_name'])
        assert sweep_orphaned_workspaces(Path(config.workspace.root), older_than=0) == 0
        assert Path(value['worktree_path']).exists()
        assert store.load(value['session_id'])['state'] == 'open'
        (Path(value['worktree_path']) / 'survived.txt').write_text('still usable\n')
        assert store.checkpoint(value['session_id'])
    finally:
        store.close_session(value['session_id'])
