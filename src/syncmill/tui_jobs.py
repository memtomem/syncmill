"""Durable, POSIX-only job manager used by ``syncmill tui``.

The Textual process never owns orchestration.  It writes a small job spec and
starts this module as a detached manager; the manager owns the CLI child, rolling
log, authenticated control socket, exit receipt, and per-board launch lock.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import selectors
import secrets
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from syncmill.config import TuiConfig

try:  # browsing remains available on non-POSIX platforms; execution is gated.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

_SCHEMA_VERSION = 1
_STOP_GRACE_SECONDS = 10.0
_TERMINAL = {"finished", "cancelled", "force_stopped", "launch_failed", "lost"}


class JobError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoardJobSpec:
    mode: str
    item_id: str | None = None
    goal: str | None = None
    strategy: str | None = None
    agent: str | None = None
    team: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"selected", "loop"}:
            raise ValueError("job mode must be 'selected' or 'loop'")
        if self.mode == "selected":
            if not self.item_id:
                raise ValueError("selected job requires item_id")
            if self.goal:
                raise ValueError("selected job cannot set goal")
        elif self.item_id:
            raise ValueError("loop job cannot set item_id")


def build_job_argv(spec: BoardJobSpec) -> list[str]:
    argv = ["board", "run"]
    if spec.mode == "selected":
        argv += ["--id", str(spec.item_id)]
    else:
        argv.append("--loop")
        if spec.goal:
            argv += ["--goal", spec.goal]
    if spec.strategy:
        argv += ["--strategy", spec.strategy]
    if spec.agent:
        argv += ["--agent", spec.agent]
    if spec.team:
        argv += ["--team", spec.team]
    return argv


def canonical_board_path(db_path: str, cwd: str | Path | None = None) -> Path:
    try:
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            path = Path(cwd or Path.cwd()) / path
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise JobError(f"cannot resolve board database path {db_path!r}: {exc}") from exc


def board_key(db_path: str | Path) -> str:
    return hashlib.sha256(str(Path(db_path)).encode()).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _runtime_dir() -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    root = (
        Path(xdg_runtime) / "syncmill"
        if xdg_runtime
        else Path(tempfile.gettempdir()) / f"syncmill-{uid}"
    )
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
    except OSError as exc:
        raise JobError(f"cannot prepare secure runtime directory: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise JobError(f"unsafe runtime path: {root}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise JobError(f"runtime directory is owned by another user: {root}")
    if info.st_mode & 0o077:
        os.chmod(root, 0o700)
    return root


def _socket_path(key: str) -> Path:
    return _runtime_dir() / f"tui-{key[:24]}.sock"


class JobStore:
    def __init__(self, config: TuiConfig, db_path: str | Path):
        try:
            resolved_db = Path(db_path).resolve()
        except (OSError, RuntimeError) as exc:
            raise JobError(f"cannot resolve board database path {db_path!r}: {exc}") from exc
        try:
            state_root = Path(config.state_dir).expanduser()
        except (OSError, RuntimeError) as exc:
            raise JobError(
                f"cannot resolve TUI state directory {config.state_dir!r}: {exc}"
            ) from exc
        self.config = config
        self.db_path = resolved_db
        self.key = board_key(self.db_path)
        self.root = state_root / self.key
        self.jobs_dir = self.root / "jobs"
        self.lock_path = self.root / "worker.lock"
        self.active_path = self.root / "active.json"

    def ensure(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.jobs_dir, 0o700)

    def list_jobs(self) -> list[dict[str, Any]]:
        if not self.jobs_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for directory in self.jobs_dir.iterdir():
            if not directory.is_dir():
                continue
            state = _read_json(directory / "state.json")
            spec = _read_json(directory / "spec.json")
            if state is None or spec is None:
                continue
            rows.append({**spec, **state, "job_dir": str(directory)})
        return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

    def active_job(self) -> dict[str, Any] | None:
        pointer = _read_json(self.active_path)
        if pointer and pointer.get("job_id"):
            job_dir = self.jobs_dir / str(pointer["job_id"])
            state = _read_json(job_dir / "state.json")
            spec = _read_json(job_dir / "spec.json")
            if state and spec and state.get("status") not in _TERMINAL:
                candidate = {**spec, **state, "job_dir": str(job_dir)}
                return self._reconcile(candidate)
        candidate = next(
            (row for row in self.list_jobs() if row.get("status") not in _TERMINAL),
            None,
        )
        return self._reconcile(candidate) if candidate else None

    def _lock_held(self) -> bool:
        if fcntl is None or not self.lock_path.exists():
            return False
        fd = os.open(self.lock_path, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def _reconcile(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if self._lock_held():
            return row
        if row.get("status") in _TERMINAL:
            return None
        state_path = Path(row["job_dir"]) / "state.json"
        state = _read_json(state_path) or {}
        state.update(
            status="lost",
            finished_at=time.time(),
            error="worker lock released without terminal receipt",
        )
        _atomic_json(state_path, state)
        return None

    def _try_lock(self) -> int:
        if fcntl is None:
            raise JobError("durable TUI execution requires a POSIX platform")
        self.ensure()
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise JobError("a TUI-managed worker is already active for this board")
        return fd

    def launch(self, spec: BoardJobSpec, *, cwd: str | Path | None = None) -> dict[str, Any]:
        lock_fd = self._try_lock()
        job_id = str(uuid4())
        job_dir = self.jobs_dir / job_id
        job_dir.mkdir(mode=0o700)
        token = secrets.token_hex(32)
        created = time.time()
        cwd_path = Path(cwd or Path.cwd()).resolve()
        spec_doc = {
            "schema_version": _SCHEMA_VERSION,
            "job_id": job_id,
            "project_key": self.key,
            "board_db": str(self.db_path),
            "cwd": str(cwd_path),
            "created_at": created,
            "request": asdict(spec),
            "argv": build_job_argv(spec),
            "log_max_bytes": self.config.log_max_bytes,
        }
        state = {
            "schema_version": _SCHEMA_VERSION,
            "job_id": job_id,
            "status": "starting",
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error": None,
            "pid": None,
            "pgid": None,
            "cancel_requested_at": None,
        }
        try:
            _atomic_json(job_dir / "spec.json", spec_doc)
            _atomic_json(job_dir / "state.json", state)
            token_path = job_dir / "control.token"
            token_path.write_text(token, encoding="ascii")
            os.chmod(token_path, 0o600)
            _atomic_json(self.active_path, {"job_id": job_id})
            bootstrap_path = job_dir / "manager.log"
            with bootstrap_path.open("ab", buffering=0) as bootstrap:
                os.chmod(bootstrap_path, 0o600)
                subprocess.Popen(
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
                    cwd=cwd_path,
                    stdin=subprocess.DEVNULL,
                    stdout=bootstrap,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(lock_fd,),
                )
        except Exception as exc:
            state.update(status="launch_failed", finished_at=time.time(), error=str(exc))
            _atomic_json(job_dir / "state.json", state)
            raise JobError(f"failed to launch TUI worker: {exc}") from exc
        finally:
            os.close(lock_fd)
        self.prune()
        return {**spec_doc, **state, "job_dir": str(job_dir)}

    def prune(self) -> None:
        terminal = [row for row in self.list_jobs() if row.get("status") in _TERMINAL]
        for row in terminal[self.config.keep_jobs :]:
            directory = Path(row["job_dir"])
            for path in sorted(directory.glob("*"), reverse=True):
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                directory.rmdir()
            except OSError:
                pass

    def control(self, action: str) -> dict[str, Any]:
        active = self.active_job()
        if active is None:
            raise JobError("no active TUI worker")
        if action == "force":
            requested = active.get("cancel_requested_at")
            if not requested or time.time() - float(requested) < 10:
                raise JobError("force stop is available 10 seconds after safe stop")
        job_dir = Path(active["job_dir"])
        token = (job_dir / "control.token").read_text(encoding="ascii")
        payload = json.dumps(
            {"job_id": active["job_id"], "token": token, "action": action}
        ).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(str(_socket_path(self.key)))
                client.sendall(payload)
                raw = client.recv(65536)
        except OSError as exc:
            raise JobError(f"worker control unavailable: {exc}") from exc
        reply = json.loads(raw.decode("utf-8", errors="replace"))
        if not reply.get("ok"):
            raise JobError(str(reply.get("error") or "worker rejected control request"))
        return reply


class _RollingLog:
    def __init__(self, job_dir: Path, max_bytes: int):
        self.current = job_dir / "console.log"
        self.previous = job_dir / "console.log.1"
        self.segment = max(512, max_bytes // 2)
        self._fh = self.current.open("ab", buffering=0)
        os.chmod(self.current, 0o600)

    def write(self, chunk: bytes) -> None:
        view = memoryview(chunk)
        while view:
            room = self.segment - self._fh.tell()
            if room <= 0:
                self._rotate()
                room = self.segment
            piece = view[:room]
            self._fh.write(piece)
            view = view[len(piece) :]

    def _rotate(self) -> None:
        self._fh.close()
        try:
            os.replace(self.current, self.previous)
        except FileNotFoundError:
            pass
        self._fh = self.current.open("ab", buffering=0)
        os.chmod(self.current, 0o600)

    def close(self) -> None:
        self._fh.close()


class _Manager:
    def __init__(self, job_dir: Path, lock_fd: int):
        self.job_dir = job_dir
        self.lock_fd = lock_fd
        self.spec = _read_json(job_dir / "spec.json") or {}
        self.state_path = job_dir / "state.json"
        self.state = _read_json(self.state_path) or {}
        self.token = (job_dir / "control.token").read_text(encoding="ascii")
        self.key = str(self.spec["project_key"])
        self.child: subprocess.Popen[bytes] | None = None
        self._state_lock = threading.Lock()
        self._stop_server = threading.Event()
        self._cancel_requested = False
        self._force_requested = False

    def _update(self, **values: Any) -> None:
        with self._state_lock:
            self.state.update(values)
            _atomic_json(self.state_path, self.state)

    def run(self) -> int:
        sock_path = _socket_path(self.key)
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        server.listen(4)
        server.settimeout(0.25)
        thread = threading.Thread(target=self._serve, args=(server,), daemon=True)
        thread.start()
        log = _RollingLog(self.job_dir, int(self.spec.get("log_max_bytes", 10 << 20)))
        manager_read, manager_write = os.pipe()
        try:
            child_env = os.environ.copy()
            if self.spec.get("board_db"):
                child_env["SYNCMILL_BOARD__ENABLED"] = "true"
                child_env["SYNCMILL_BOARD__DB_PATH"] = str(self.spec["board_db"])
            self.child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "syncmill.tui_jobs",
                    "child",
                    "--job-dir",
                    str(self.job_dir),
                    "--lock-fd",
                    str(self.lock_fd),
                    "--manager-fd",
                    str(manager_read),
                    "--",
                    *self.spec["argv"],
                ],
                cwd=self.spec["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                pass_fds=(self.lock_fd, manager_read),
                env=child_env,
            )
            os.close(manager_read)
            manager_read = -1
            self._update(
                status="running",
                started_at=time.time(),
                pid=self.child.pid,
                pgid=self.child.pid,
            )
            assert self.child.stdout is not None
            while chunk := self.child.stdout.read(64 * 1024):
                log.write(chunk)
            code = self.child.wait()
            if self._force_requested:
                status, normalized = "force_stopped", 137
            elif self._cancel_requested:
                status, normalized = "cancelled", 130
            else:
                status, normalized = "finished", code
            self._update(status=status, exit_code=normalized, finished_at=time.time())
            return normalized
        except BaseException as exc:
            self._update(
                status="launch_failed" if self.child is None else "lost",
                finished_at=time.time(),
                error=f"{type(exc).__name__}: {exc}",
            )
            return 1
        finally:
            if manager_read >= 0:
                os.close(manager_read)
            os.close(manager_write)
            log.close()
            self._stop_server.set()
            server.close()
            thread.join(timeout=1)
            try:
                sock_path.unlink()
            except FileNotFoundError:
                pass

    def _serve(self, server: socket.socket) -> None:
        while not self._stop_server.is_set():
            try:
                conn, _ = server.accept()
            except (TimeoutError, OSError):
                continue
            with conn:
                try:
                    request = json.loads(conn.recv(65536).decode("utf-8"))
                    reply = self._handle_control(request)
                except Exception as exc:
                    reply = {"ok": False, "error": str(exc)}
                conn.sendall(json.dumps(reply).encode())

    def _handle_control(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("job_id") != self.spec.get("job_id"):
            raise JobError("job id mismatch")
        if not secrets.compare_digest(str(request.get("token", "")), self.token):
            raise JobError("invalid control token")
        action = request.get("action")
        if self.child is None or self.child.poll() is not None:
            raise JobError("worker child is not running")
        if action == "stop":
            self._cancel_requested = True
            self._update(status="cancel_requested", cancel_requested_at=time.time())
            os.killpg(self.child.pid, signal.SIGINT)
        elif action == "force":
            self._force_requested = True
            self._update(status="force_stop_requested")
            os.killpg(self.child.pid, signal.SIGKILL)
        else:
            raise JobError("unknown control action")
        return {"ok": True, "status": self.state.get("status")}


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_pipe_eof(fd: int, timeout: float) -> bool:
    selector = selectors.DefaultSelector()
    try:
        selector.register(fd, selectors.EVENT_READ)
        if not selector.select(timeout):
            return False
        return os.read(fd, 1) == b""
    finally:
        selector.close()


def _guardian_main(args: argparse.Namespace) -> int:
    manager_fd = int(args.manager_fd)
    child_fd = int(args.child_fd)
    pgid = int(args.pgid)
    selector = selectors.DefaultSelector()
    manager_lost = False
    try:
        selector.register(child_fd, selectors.EVENT_READ, "child")
        selector.register(manager_fd, selectors.EVENT_READ, "manager")
        while True:
            for key, _ in selector.select():
                if os.read(key.fd, 1) != b"":
                    continue
                if key.data == "child":
                    return 0
                manager_lost = True
                break
            if manager_lost:
                break
    finally:
        selector.close()

    forced = False
    try:
        os.killpg(pgid, signal.SIGINT)
    except ProcessLookupError:
        pass
    if not _wait_for_pipe_eof(child_fd, float(args.grace)) and _process_group_exists(pgid):
        forced = True
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    state_path = Path(args.job_dir) / "state.json"
    state = _read_json(state_path) or {}
    state.update(
        status="lost",
        finished_at=time.time(),
        error=(
            "manager exited unexpectedly; child process group was force-stopped"
            if forced
            else "manager exited unexpectedly; child process group was stopped"
        ),
    )
    _atomic_json(state_path, state)
    return 0


def _child_main(args: argparse.Namespace) -> int:
    completion_read, completion_write = os.pipe()
    guardian = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "syncmill.tui_jobs",
            "guardian",
            "--job-dir",
            str(args.job_dir),
            "--lock-fd",
            str(args.lock_fd),
            "--manager-fd",
            str(args.manager_fd),
            "--child-fd",
            str(completion_read),
            "--pgid",
            str(os.getpgrp()),
            "--grace",
            str(args.grace),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        pass_fds=(int(args.lock_fd), int(args.manager_fd), completion_read),
    )
    os.close(completion_read)
    os.close(int(args.manager_fd))
    os.close(int(args.lock_fd))
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    try:
        from syncmill.cli import cli

        cli(args=argv, prog_name="syncmill")
        return 0
    finally:
        os.close(completion_write)
        try:
            guardian.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _worker_main(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    try:
        return _Manager(job_dir, int(args.lock_fd)).run()
    except BaseException as exc:
        state_path = job_dir / "state.json"
        state = _read_json(state_path) or {}
        state.update(
            status="launch_failed",
            finished_at=time.time(),
            error=f"{type(exc).__name__}: {exc}",
        )
        _atomic_json(state_path, state)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    worker = sub.add_parser("worker")
    worker.add_argument("--job-dir", required=True)
    worker.add_argument("--lock-fd", required=True, type=int)
    child = sub.add_parser("child")
    child.add_argument("--job-dir", required=True)
    child.add_argument("--lock-fd", required=True, type=int)
    child.add_argument("--manager-fd", required=True, type=int)
    child.add_argument("--grace", type=float, default=_STOP_GRACE_SECONDS)
    child.add_argument("argv", nargs=argparse.REMAINDER)
    guardian = sub.add_parser("guardian")
    guardian.add_argument("--job-dir", required=True)
    guardian.add_argument("--lock-fd", required=True, type=int)
    guardian.add_argument("--manager-fd", required=True, type=int)
    guardian.add_argument("--child-fd", required=True, type=int)
    guardian.add_argument("--pgid", required=True, type=int)
    guardian.add_argument("--grace", type=float, default=_STOP_GRACE_SECONDS)
    args = parser.parse_args()
    if args.command == "worker":
        return _worker_main(args)
    if args.command == "child":
        return _child_main(args)
    return _guardian_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
