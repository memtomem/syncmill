from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time
from pathlib import Path

import pytest


pytestmark = [
    pytest.mark.skipif(os.name != "posix", reason="PTY smoke is POSIX-only"),
    pytest.mark.skipif(
        os.environ.get("SYNCMILL_TEST_TUI_PTY") != "1",
        reason="set SYNCMILL_TEST_TUI_PTY=1 for the real terminal smoke",
    ),
]


def _read_until(fd: int, marker: bytes, deadline: float) -> bytes:
    chunks: list[bytes] = []
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        if marker in b"".join(chunks):
            break
    return b"".join(chunks)


@pytest.mark.parametrize(("columns", "rows"), [(120, 40), (80, 24)])
def test_real_tui_mounts_in_a_pty_and_q_exits_cleanly(
    tmp_path: Path, git_repo: Path, columns: int, rows: int
):
    env = os.environ.copy()
    env.update(
        {
            "TERM": "xterm-256color",
            "NO_COLOR": "1",
            "SYNCMILL_BOARD__ENABLED": "false",
            "SYNCMILL_ROOMS__ENABLED": "false",
            "SYNCMILL_RUNS__ROOT": str(tmp_path / "runs"),
            "SYNCMILL_WORKSPACE__ROOT": str(tmp_path / "workspaces"),
            "SYNCMILL_TUI__STATE_DIR": str(tmp_path / "tui"),
        }
    )
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        os.chdir(git_repo)
        os.execve(
            sys.executable,
            [
                sys.executable,
                "-c",
                "from syncmill.cli import cli; cli()",
                "tui",
                "--tab",
                "status",
                "--refresh",
                "3600",
            ],
            env,
        )

    # waitpid(WNOHANG) returns (0, 0) while the child runs, so `status` alone
    # cannot distinguish "reaped with exit 0" from "still running": track the
    # reap separately so a timeout always kills AND waits for the child.
    reaped = False
    status = 0
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        output = _read_until(fd, b"q quit", time.monotonic() + 15)
        assert b"syncmill tui" in output, output[-2000:]
        assert b"Status" in output, output[-2000:]
        assert b"q quit" in output, output[-2000:]
        os.write(fd, b"q")
        exit_deadline = time.monotonic() + 10
        while time.monotonic() < exit_deadline:
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished == pid:
                reaped = True
                break
            # Drain the PTY so Textual's final restore/paint cannot block on a
            # full terminal buffer while the parent waits.
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    os.read(fd, 65536)
                except OSError:
                    pass
            time.sleep(0.05)
        else:
            pytest.fail("TUI did not exit after q")
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(fd)
