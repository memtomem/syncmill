"""A one-byte pipe that tells a launcher whether its detached worker got going.

The launcher keeps the read end and hands the write end to the worker, which
writes one byte at the point from which its failures become visible somewhere
else (a job's ``state.json``, a room's own records) and then closes it. EOF
with no byte means every holder of the write end is gone before that point:
the kernel's answer, with no pid to reuse and no polling interval. No byte
within the bound means only that: the worker was not confirmed in time.
"""

from __future__ import annotations

import enum
import os
import select
import threading
import time

READY_BYTE = b"R"

#: How long a command-line launcher waits. Measured warm bootstrap is 0.1 s
#: (manager) and 0.2 s (room worker); the rest is cold-start and big-repo room.
CLI_READY_TIMEOUT_SECONDS = 10.0
#: The TUI waits off its UI thread, so it can afford a longer bound.
TUI_READY_TIMEOUT_SECONDS = 30.0

_POLL_SLICE_SECONDS = 0.1


class Outcome(enum.Enum):
    READY = "ready"
    EXITED = "exited"
    PENDING = "pending"


class ReadyWaiter:
    """Owns the read end. ``wait`` always closes it; ``close`` is idempotent.

    One lock covers the fd from ``select`` through ``read`` and in ``close``,
    so a close from another thread (TUI shutdown) waits out at most one poll
    slice instead of freeing the number while ``wait`` still uses it: a
    reused number could hand ``wait`` another pipe's byte."""

    def __init__(self, read_fd: int):
        self._fd: int | None = read_fd
        self._lock = threading.Lock()

    def wait(
        self, timeout: float, cancel: threading.Event | None = None
    ) -> Outcome:
        try:
            deadline = time.monotonic() + max(timeout, 0.0)
            while True:
                if cancel is not None and cancel.is_set():
                    return Outcome.PENDING
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return Outcome.PENDING
                with self._lock:
                    if self._fd is None:
                        return Outcome.PENDING
                    try:
                        readable, _, _ = select.select(
                            [self._fd], [], [], min(remaining, _POLL_SLICE_SECONDS)
                        )
                        data = os.read(self._fd, 1) if readable else None
                    except (OSError, ValueError):
                        return Outcome.PENDING
                if data is None:
                    continue
                return Outcome.READY if data == READY_BYTE else Outcome.EXITED
        finally:
            self.close()

    def close(self) -> None:
        with self._lock:
            fd, self._fd = self._fd, None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def __enter__(self) -> ReadyWaiter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def ready_pipe() -> tuple[ReadyWaiter, int]:
    """Return ``(waiter, write_fd)``. The write end is not inheritable: the
    launcher passes it explicitly (``pass_fds``) and closes its own copy."""
    read_fd, write_fd = os.pipe()
    os.set_inheritable(read_fd, False)
    os.set_inheritable(write_fd, False)
    return ReadyWaiter(read_fd), write_fd


def adopt_ready_fd(fd: int | None) -> int | None:
    """Worker side: keep the inherited write end from reaching anything it
    starts. Returns the fd, or None when there is none to adopt."""
    if fd is None or fd < 3:
        return None
    try:
        os.set_inheritable(fd, False)
    except OSError:
        return None
    return fd


def signal_ready(fd: int | None) -> None:
    """Write the byte and close. Never raises: a launcher that stopped waiting
    has closed the read end (EPIPE), and signalling must never turn a healthy
    worker into a crash."""
    if fd is None:
        return
    try:
        os.write(fd, READY_BYTE)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def pending_text(timeout: float) -> str:
    return f"not confirmed within {timeout:g}s"


def log_tail(path: str | os.PathLike[str], *, max_bytes: int = 2048, max_lines: int = 5) -> list[str]:
    """The last non-blank lines of a bootstrap log, raw (callers escape them
    for their surface). ``[]`` when the file is missing, unreadable or empty."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
    except OSError:
        return []
    lines = [line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]
    if size > max_bytes and lines:
        lines = lines[1:]  # the first line is probably cut
    return lines[-max_lines:]
