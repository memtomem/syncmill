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


_RAW = (b"\x1b]52;c;", b"\xc2\x9b", "\u202e".encode(), b"\x07RAW")


def _run_in_pty(argv, env, cwd, marker: bytes, *, keys: bytes = b"q") -> bytes:
    """Run ``argv`` on a pty, wait for ``marker``, send ``keys``, and return
    everything written until the child exits."""
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        os.chdir(cwd)
        os.execve(argv[0], argv, env)
    reaped = False
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        output = _read_until(fd, marker, time.monotonic() + 15)
        if keys:
            os.write(fd, keys)
        deadline = time.monotonic() + 10
        chunks = [output]
        while time.monotonic() < deadline:
            finished, _ = os.waitpid(pid, os.WNOHANG)
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b""
                chunks.append(chunk)
            if finished == pid:
                reaped = True
                break
        else:
            pytest.fail("child did not exit")
        return b"".join(chunks)
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(fd)


def test_stored_text_with_terminal_controls_never_reaches_the_terminal(
    tmp_path: Path, git_repo: Path
):
    # #210: Textual passes ESC, C1 and bidi controls through; the guard that
    # run_tui installs must keep them off the real terminal.
    from syncmill.board import Board

    db = tmp_path / "board.db"
    board = Board(db)
    board.add("MARKER210 \x1b]52;c;QQ==\x07RAW csi\u009b2J bidi\u202eRTL", goal="g")
    board.close()
    env = os.environ.copy()
    env.update({
        "TERM": "xterm-256color",
        "SYNCMILL_BOARD__ENABLED": "true",
        "SYNCMILL_BOARD__DB_PATH": str(db),
        "SYNCMILL_ROOMS__ENABLED": "false",
        "SYNCMILL_RUNS__ROOT": str(tmp_path / "runs"),
        "SYNCMILL_WORKSPACE__ROOT": str(tmp_path / "workspaces"),
        "SYNCMILL_TUI__STATE_DIR": str(tmp_path / "tui"),
    })
    argv = [sys.executable, "-c", "from syncmill.cli import cli; cli()",
            "tui", "--tab", "board", "--refresh", "3600"]
    output = _run_in_pty(argv, env, git_repo, b"MARKER210")
    assert b"MARKER210" in output, output[-2000:]
    for raw in _RAW:
        assert raw not in output, (raw, output[-2000:])


def test_a_fatal_error_prints_its_message_escaped(tmp_path: Path):
    # Textual prints a crash through App.error_console, outside the render
    # guard; the apps replace it with tui_render.error_console().
    script = tmp_path / "crash.py"
    script.write_text(
        "from textual.app import App\n"
        "from syncmill.tui_render import error_console, install_render_guard\n"
        "class Crash(App):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self.error_console = error_console()\n"
        "    def on_mount(self):\n"
        "        raise RuntimeError('boom \\x1b]52;c;QQ==\\x07RAW \\u009b2J \\u202eRTL')\n"
        "install_render_guard()\n"
        "Crash().run()\n"
    )
    env = {**os.environ, "TERM": "xterm-256color"}
    output = _run_in_pty([sys.executable, str(script)], env, tmp_path, b"", keys=b"")
    assert b"RuntimeError" in output, output[-2000:]
    assert b"\\x1b]52;c;QQ==" in output, output[-2000:]
    for raw in _RAW:
        assert raw not in output, (raw, output[-2000:])


def test_every_widget_kind_keeps_terminal_controls_off_the_terminal(tmp_path: Path):
    # Log, OptionList, Input and DataTable build rich Text rather than Content,
    # toasts render through their own widget, and a link style is serialized as
    # OSC 8: each must reach the real terminal without a hostile byte.
    script = tmp_path / "widgets.py"
    script.write_text(
        "from rich.style import Style\n"
        "from rich.text import Text\n"
        "from textual.app import App\n"
        "from textual.widgets import DataTable, Input, Log, OptionList\n"
        "from syncmill.tui_render import install_render_guard, plain\n"
        "H = ' \\x1b]52;c;QQ==\\x07RAW \\u009b2J \\u202eRTL'\n"
        "class W(App):\n"
        "    def compose(self):\n"
        "        yield Log(id='log')\n"
        "        yield OptionList('OPT210' + H, markup=False)\n"
        "        yield Input(value='INP210' + H)\n"
        "        yield DataTable(id='t')\n"
        "    def on_mount(self):\n"
        "        self.query_one('#log', Log).write_line('LOG210' + H)\n"
        "        table = self.query_one('#t', DataTable)\n"
        "        table.add_columns(plain('c'))\n"
        "        link = Text('LNK210', style=Style(link='x\\u009d52;c;QQ==\\u009c'))\n"
        "        table.add_row(link)\n"
        "        self.notify('TST210' + H, markup=False, timeout=30)\n"
        "install_render_guard()\n"
        "W().run()\n"
    )
    env = {**os.environ, "TERM": "xterm-256color"}
    output = _run_in_pty([sys.executable, str(script)], env, tmp_path, b"TST210",
                         keys=b"\x11")  # ctrl+q
    for marker in (b"LOG210", b"OPT210", b"INP210", b"LNK210", b"TST210"):
        assert marker in output, (marker, output[-3000:])
    for raw in (*_RAW, b"\xc2\x9d"):
        assert raw not in output, (raw, output[-3000:])
