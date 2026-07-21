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
        cwd = isolated._tmux(
            "list-panes", "-t", isolated._exact(name), "-F",
            "#{pane_current_path}",
        )
        assert cwd.returncode == 0
        assert Path(cwd.stdout.strip()).resolve() == Path(value["worktree_path"]).resolve()

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
