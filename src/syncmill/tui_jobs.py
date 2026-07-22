"""Durable, POSIX-only job manager used by ``syncmill tui``.

The Textual process never owns orchestration.  It writes a small job spec and
starts this module as a detached manager; the manager owns the CLI child, rolling
log, authenticated control socket, exit receipt, and per-target launch lock.

Generalized in #0.1.2 Phase 3 from a board-only, single-active-job manager into a
project-scoped, multi-job manager keyed PER LOGICAL TARGET (the settled Phase-3
decisions): the store is anchored on the canonical git root (``project_key``), each
job holds its OWN launch lock (so different targets run concurrently while a
duplicate launch for the same target fails fast), and source mutation is serialized
NOT here but on the existing ``artifacts.apply_lock`` — this manager lock is
exclusion, never crash recovery.  Chunk A generalizes the plumbing; board-run stays
the only op the UI launches (room/artifact-apply execution lands in Chunk B).
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
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, NoReturn
from uuid import uuid4

from syncmill.config import TuiConfig

try:  # browsing remains available on non-POSIX platforms; execution is gated.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

#: Bumped to 2 when the manager became project-scoped/multi-job.  Legacy schema-1
#: board job dirs are still READ (never written) via the compat overlay below.
_SCHEMA_VERSION = 2
_STOP_GRACE_SECONDS = 10.0
#: A force stop (SIGKILL) is only honored this long after a safe stop (SIGINT).
_FORCE_GRACE_SECONDS = 10.0
_TERMINAL = {"finished", "cancelled", "force_stopped", "launch_failed", "lost"}
#: The manager writes this one byte to the guardian pipe AFTER it has durably
#: recorded a terminal state — the "clean completion, you may stand down" ack.
#: A manager pipe that reaches EOF WITHOUT it means the manager died mid-run, so
#: the guardian must tear the child's process group down.
_MANAGER_DONE = b"\x01"


class JobError(RuntimeError):
    pass


@dataclass(frozen=True)
class BoardRunSpec:
    """A ``board run`` operation (loop/selected).  ``op`` is a fixed, ``init=False``
    discriminator so today's callers construct it exactly as before."""

    mode: str
    item_id: str | None = None
    goal: str | None = None
    strategy: str | None = None
    agent: str | None = None
    team: str | None = None
    op: str = field(default="board-run", init=False)

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


#: Backwards-compatible alias — the board-run spec was the only spec in schema-1.
BoardJobSpec = BoardRunSpec
#: The typed job spec union.  Widened in Chunk B when room/artifact ops land.
JobSpec = BoardRunSpec


def build_job_argv(spec: JobSpec) -> list[str]:
    if spec.op == "board-run":
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
    raise JobError(f"unsupported job op: {spec.op!r}")


def canonical_board_path(db_path: str, cwd: str | Path | None = None) -> Path:
    try:
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            path = Path(cwd or Path.cwd()) / path
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise JobError(f"cannot resolve board database path {db_path!r}: {exc}") from exc


def board_key(db_path: str | Path) -> str:
    """The schema-1 board key — still used for the legacy compat root AND as the
    per-target lock identity of a board run (D2: ``board-run\\0<canonical path>``)."""
    return hashlib.sha256(str(Path(db_path)).encode()).hexdigest()


def project_key(project_root: str | Path) -> str:
    """The store anchor — the canonical git root, NOT the user-controlled
    ``project.id`` (which can collide across checkouts)."""
    return hashlib.sha256(b"repo-v1\0" + str(project_root).encode()).hexdigest()


def _target_lock_key(spec: JobSpec, *, project_key: str, board_db: Path | None) -> str:
    """Per-logical-target launch-lock key (the settled D2 formulas)."""
    if spec.op == "board-run":
        if board_db is None:
            raise JobError("board run requires a board database path")
        return hashlib.sha256(b"board-run\0" + str(board_db).encode()).hexdigest()
    raise JobError(f"unsupported job op: {spec.op!r}")


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


def _socket_path(job_id: str) -> Path:
    """One control socket PER JOB (was per-project) — so many jobs coexist."""
    return _runtime_dir() / f"tui-{job_id[:24]}.sock"


class JobStore:
    def __init__(
        self,
        config: TuiConfig,
        project_root: str | Path,
        *,
        board_db: str | Path | None = None,
    ):
        try:
            resolved_root = Path(project_root).resolve()
        except (OSError, RuntimeError) as exc:
            raise JobError(
                f"cannot resolve project root {project_root!r}: {exc}"
            ) from exc
        try:
            # Resolve to an ABSOLUTE path: job dirs are derived from this and
            # passed to a worker launched with cwd=project_root, so a relative
            # state dir would be created and looked up against different cwds.
            state_root = Path(config.state_dir).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise JobError(
                f"cannot resolve TUI state directory {config.state_dir!r}: {exc}"
            ) from exc
        resolved_board: Path | None = None
        if board_db is not None:
            try:
                resolved_board = Path(board_db).expanduser().resolve()
            except (OSError, RuntimeError) as exc:
                raise JobError(
                    f"cannot resolve board database path {board_db!r}: {exc}"
                ) from exc
        self.config = config
        self.project_root = resolved_root
        self.board_db = resolved_board
        # The store's stable identity; also its state root directory name.
        self.key = project_key(resolved_root)
        self.root = state_root / self.key
        self.jobs_dir = self.root / "jobs"
        self.locks_dir = self.root / "locks"
        # Schema-1 board jobs lived under a board-keyed root with a single
        # worker.lock; read (never write) them via the compat overlay.
        self.legacy_root = (
            state_root / board_key(resolved_board) if resolved_board else None
        )

    # -- directory / listing ------------------------------------------------

    def ensure(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.jobs_dir, 0o700)

    def _read_jobs_dir(self, jobs_dir: Path, *, origin: str) -> list[dict[str, Any]]:
        if not jobs_dir.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for directory in jobs_dir.iterdir():
            if not directory.is_dir():
                continue
            state = _read_json(directory / "state.json")
            spec = _read_json(directory / "spec.json")
            if state is None or spec is None:
                continue
            rows.append({**spec, **state, "job_dir": str(directory), "origin": origin})
        return rows

    def _bridge_held_by_us(self) -> bool:
        """True when a live schema-2 board-run job for THIS store's board db holds
        the legacy bridge lock — so the held legacy ``worker.lock`` is ours, NOT a
        schema-1 worker's, and no legacy row may be read as active on its strength.

        The project jobs_dir is shared across stores of the same project but
        different board dbs, so match the canonical board_db: a live board-run job
        for a DIFFERENT board must not mask a genuine schema-1 job for this one."""
        if self.board_db is None:
            return False
        board_db = str(self.board_db)
        for row in self._read_jobs_dir(self.jobs_dir, origin="project"):
            if (
                row.get("op") == "board-run"
                and row.get("board_db") == board_db
                and row.get("status") not in _TERMINAL
            ):
                held, owner = self._lock_state(row.get("lock_key"))
                if held and owner == row.get("job_id"):
                    return True
        return False

    def _overlay_legacy(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Ephemeral READ overlay for schema-1 board jobs (never written back).

        Schema-1 had a SINGLE worker.lock + active.json, so at most ONE legacy row
        can be genuinely running: the active.json pointer, and only while a
        schema-1 worker (not our own schema-2 bridge) holds the lock.  Every other
        non-terminal legacy row is stale → shown as ``lost``."""
        pointer = None
        ptr_doc = _read_json(self.legacy_root / "active.json") if self.legacy_root else None
        if ptr_doc and ptr_doc.get("job_id"):
            pointer = str(ptr_doc["job_id"])
        live_legacy = self._legacy_lock_held() and not self._bridge_held_by_us()
        out: list[dict[str, Any]] = []
        for row in rows:
            if row.get("status") not in _TERMINAL:
                if not (live_legacy and row.get("job_id") == pointer):
                    row = {**row, "status": "lost"}
            out.append(row)
        return out

    def list_jobs(self) -> list[dict[str, Any]]:
        rows = self._read_jobs_dir(self.jobs_dir, origin="project")
        if self.legacy_root is not None:
            rows += self._overlay_legacy(
                self._read_jobs_dir(self.legacy_root / "jobs", origin="legacy")
            )
        return sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True)

    def active_jobs(self) -> list[dict[str, Any]]:
        """Non-terminal jobs that genuinely own their target lock (held AND owner
        matches).  A schema-2 row whose lock is released, or whose target was
        taken over by a newer job, is reconciled to ``lost`` in place."""
        out: list[dict[str, Any]] = []
        for row in self.list_jobs():
            if row.get("origin") == "legacy":
                if row.get("status") not in _TERMINAL:
                    out.append(row)
                continue
            resolved = self._reconcile(row)
            if resolved is not None and resolved.get("status") not in _TERMINAL:
                out.append(resolved)
        return out

    def active_job(self) -> dict[str, Any] | None:
        """Compatibility helper for "is anything running?" callers ONLY — never
        used to address a control request now that concurrency is enabled."""
        jobs = self.active_jobs()
        return jobs[0] if jobs else None

    # -- locking ------------------------------------------------------------

    def _lock_state(self, lock_key: str | None) -> tuple[bool, str | None]:
        """``(held, owner_job_id)`` for a per-target lock file.  The owner is read
        from the lock file's contents (written under the lock), so a released-then-
        reacquired target is attributable to its current holder."""
        if fcntl is None or not lock_key:
            return (False, None)
        path = self.locks_dir / f"{lock_key}.lock"
        if not path.exists():
            return (False, None)
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return (False, None)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                owner = os.pread(fd, 256, 0).decode("utf-8", "replace").strip()
                return (True, owner or None)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return (False, None)
        finally:
            os.close(fd)

    def _legacy_lock_held(self) -> bool:
        if fcntl is None or self.legacy_root is None:
            return False
        legacy_lock = self.legacy_root / "worker.lock"
        if not legacy_lock.exists():
            return False
        try:
            fd = os.open(legacy_lock, os.O_RDWR)
        except OSError:
            return False
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
        if row.get("status") in _TERMINAL:
            return row
        held, owner = self._lock_state(row.get("lock_key"))
        if held and owner == row.get("job_id"):
            return row
        # Lock released, or a replacement job owns the target → this row is lost.
        state_path = Path(row["job_dir"]) / "state.json"
        state = _read_json(state_path) or {}
        if state.get("status") in _TERMINAL:
            return {**row, **state}
        state.update(
            status="lost",
            finished_at=time.time(),
            error="worker lock released without terminal receipt",
        )
        _atomic_json(state_path, state)
        return {**row, **state}

    def _reconcile_superseded(self, lock_key: str, new_job_id: str) -> None:
        """After WE acquire ``lock_key``, any older non-terminal job for the same
        target is stale (we own it now) — mark them lost."""
        for row in self._read_jobs_dir(self.jobs_dir, origin="project"):
            if (
                row.get("lock_key") == lock_key
                and row.get("job_id") != new_job_id
                and row.get("status") not in _TERMINAL
            ):
                state_path = Path(row["job_dir"]) / "state.json"
                state = _read_json(state_path) or {}
                if state.get("status") in _TERMINAL:
                    continue
                state.update(
                    status="lost",
                    finished_at=time.time(),
                    error="superseded by a new worker for the same target",
                )
                _atomic_json(state_path, state)

    def _open_exclusive(self, path: Path) -> int:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            # Close on ANY flock failure (contention or otherwise); callers
            # translate BlockingIOError into a JobError.
            os.close(fd)
            raise
        return fd

    def _target_key_for(self, spec: JobSpec) -> str:
        return _target_lock_key(spec, project_key=self.key, board_db=self.board_db)

    def _try_target_lock(self, lock_key: str, job_id: str) -> int:
        if fcntl is None:
            raise JobError("durable TUI execution requires a POSIX platform")
        self.locks_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Lock files are PERMANENT (never unlinked): unlinking a held inode would
        # let a second launcher create a new inode and win a second lock.
        path = self.locks_dir / f"{lock_key}.lock"
        try:
            fd = self._open_exclusive(path)
        except BlockingIOError:
            raise JobError("a TUI-managed worker is already active for this target")
        try:
            # Record ownership under the lock so active_jobs can attribute the
            # target.  On any failure close the fd — never leak a held lock.
            os.ftruncate(fd, 0)
            os.pwrite(fd, job_id.encode(), 0)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _try_legacy_lock(self) -> int | None:
        """Bridge schema-1 exclusion: a schema-2 board launch also holds the legacy
        board ``worker.lock`` so a concurrent OLD-binary board run is excluded."""
        if self.legacy_root is None:
            return None
        self.legacy_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        legacy_lock = self.legacy_root / "worker.lock"
        try:
            return self._open_exclusive(legacy_lock)
        except BlockingIOError:
            raise JobError("a TUI-managed worker is already active for this board")

    # -- launch / control ---------------------------------------------------

    def launch(
        self, spec: JobSpec, *, cwd: str | Path | None = None
    ) -> dict[str, Any]:
        # The store OWNS the repository boundary: the worker always runs in the
        # store's project root, never an arbitrary caller cwd.
        cwd_path = self.project_root
        if cwd is not None:
            try:
                given = Path(cwd).resolve()
            except (OSError, RuntimeError) as exc:
                raise JobError(f"cannot resolve cwd {cwd!r}: {exc}") from exc
            if given != cwd_path:
                raise JobError(
                    f"cwd {given} does not match the store project root {cwd_path}"
                )
        job_id = str(uuid4())
        lock_key = self._target_key_for(spec)
        job_dir = self.jobs_dir / job_id
        created = time.time()
        spec_doc = {
            "schema_version": _SCHEMA_VERSION,
            "job_id": job_id,
            "op": spec.op,
            "project_key": self.key,
            "project_root": str(self.project_root),
            "board_db": str(self.board_db) if self.board_db else None,
            "lock_key": lock_key,
            "cwd": str(cwd_path),
            "created_at": created,
            "request": asdict(spec),
            "argv": build_job_argv(spec),
            "log_max_bytes": self.config.log_max_bytes,
        }
        state = {
            "schema_version": _SCHEMA_VERSION,
            "job_id": job_id,
            "lock_key": lock_key,
            "status": "starting",
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error": None,
            "pid": None,
            "pgid": None,
            "cancel_requested_at": None,
        }
        # Acquire both locks, then do EVERYTHING under one try/finally so no path
        # (incl. _reconcile_superseded's I/O) can leak a held lock fd.
        target_fd = self._try_target_lock(lock_key, job_id)
        legacy_fd: int | None = None
        launched = False
        try:
            if spec.op == "board-run":
                legacy_fd = self._try_legacy_lock()
            # We now own the target — retire any older job still claiming it.
            self._reconcile_superseded(lock_key, job_id)
            pass_fds = [target_fd] if legacy_fd is None else [target_fd, legacy_fd]
            job_dir.mkdir(mode=0o700, parents=True)
            _atomic_json(job_dir / "spec.json", spec_doc)
            _atomic_json(job_dir / "state.json", state)
            token = secrets.token_hex(32)
            token_path = job_dir / "control.token"
            token_path.write_text(token, encoding="ascii")
            os.chmod(token_path, 0o600)
            worker_argv = [
                sys.executable, "-m", "syncmill.tui_jobs", "worker",
                "--job-dir", str(job_dir), "--lock-fd", str(target_fd),
            ]
            if legacy_fd is not None:
                worker_argv += ["--legacy-lock-fd", str(legacy_fd)]
            bootstrap_path = job_dir / "manager.log"
            with bootstrap_path.open("ab", buffering=0) as bootstrap:
                os.chmod(bootstrap_path, 0o600)
                subprocess.Popen(
                    worker_argv,
                    cwd=cwd_path,
                    stdin=subprocess.DEVNULL,
                    stdout=bootstrap,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=tuple(pass_fds),
                )
            launched = True
        except Exception as exc:
            if job_dir.is_dir():
                state.update(
                    status="launch_failed", finished_at=time.time(), error=str(exc)
                )
                _atomic_json(job_dir / "state.json", state)
            raise JobError(f"failed to launch TUI worker: {exc}") from exc
        finally:
            os.close(target_fd)
            if legacy_fd is not None:
                os.close(legacy_fd)
        if launched:
            self.prune()
        return {**spec_doc, **state, "job_dir": str(job_dir)}

    def prune(self) -> None:
        # Prune ONLY schema-2 project jobs; legacy dirs are read-only overlays,
        # and an unknown/future-version row under the project store is left alone.
        terminal = [
            row
            for row in self._read_jobs_dir(self.jobs_dir, origin="project")
            if row.get("status") in _TERMINAL and row.get("schema_version") == 2
        ]
        terminal.sort(key=lambda row: row.get("created_at", ""), reverse=True)
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

    def control(self, job_id: str, action: str) -> dict[str, Any]:
        job_dir = self.jobs_dir / str(job_id)
        state = _read_json(job_dir / "state.json")
        spec = _read_json(job_dir / "spec.json")
        if state is None or spec is None:
            raise JobError(f"no such TUI job: {job_id}")
        if state.get("status") in _TERMINAL:
            raise JobError("job is no longer running")
        if action == "force":
            requested = state.get("cancel_requested_at")
            if not requested or time.time() - float(requested) < _FORCE_GRACE_SECONDS:
                raise JobError("force stop is available 10 seconds after safe stop")
        token = (job_dir / "control.token").read_text(encoding="ascii")
        payload = json.dumps(
            {"job_id": str(job_id), "token": token, "action": action}
        ).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(str(_socket_path(str(job_id))))
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
    def __init__(self, job_dir: Path, lock_fd: int, legacy_lock_fd: int = -1):
        self.job_dir = job_dir
        self.lock_fd = lock_fd
        # Threaded through child -> guardian (like lock_fd) so the schema-1
        # exclusion bridge survives manager death: if the manager is killed, the
        # guardian still holds it while it tears the child's group down, so an old
        # schema-1 launcher cannot grab the legacy lock mid-cleanup.
        self.legacy_lock_fd = legacy_lock_fd
        self.spec = _read_json(job_dir / "spec.json") or {}
        self.state_path = job_dir / "state.json"
        self.state = _read_json(self.state_path) or {}
        self.token = (job_dir / "control.token").read_text(encoding="ascii")
        self.job_id = str(self.spec.get("job_id", ""))
        self.child: subprocess.Popen[bytes] | None = None
        self._state_lock = threading.Lock()
        self._stop_server = threading.Event()
        self._cancel_requested = False
        self._force_requested = False
        # A ONE-WAY latch under _state_lock: once the run records a terminal
        # state, no control request may overwrite it, and only the success path
        # (which sets this) may ack the guardian.
        self._finalized = False

    def _update(self, **values: Any) -> None:
        with self._state_lock:
            self.state.update(values)
            _atomic_json(self.state_path, self.state)

    def _mark_running(self, pid: int) -> None:
        """Publish the running transition WITHOUT clobbering a control state that
        already arrived (a stop accepted between Popen and this write): keep the
        control status if it is no longer 'starting'."""
        with self._state_lock:
            self.state.update(started_at=time.time(), pid=pid, pgid=pid)
            if self.state.get("status") == "starting":
                self.state["status"] = "running"
            _atomic_json(self.state_path, self.state)

    def _apply_control(self, action: str) -> str:
        """Deliver a control signal and publish its intent ATOMICALLY under the
        state lock: refuse once finalized, deliver the signal, and commit the flag
        + state ONLY after the kernel accepts it — so a failed signal never leaves
        a misleading cancelled/force_stopped receipt, and a concurrent finalize
        either wins (this refuses) or loses (it sees the flag). The force-grace
        read is part of the same transaction."""
        with self._state_lock:
            if self._finalized:
                raise JobError("job is finalizing")
            if self.child is None or self.child.poll() is not None:
                raise JobError("worker child is not running")
            if action == "stop":
                sig, status = signal.SIGINT, "cancel_requested"
            elif action == "force":
                requested = self.state.get("cancel_requested_at")
                if not requested or time.time() - float(requested) < _FORCE_GRACE_SECONDS:
                    raise JobError("force stop is available 10 seconds after safe stop")
                sig, status = signal.SIGKILL, "force_stop_requested"
            else:
                raise JobError("unknown control action")
            try:
                os.killpg(self.child.pid, sig)
            except OSError as exc:
                raise JobError(f"could not signal worker: {exc}") from exc
            # Kernel accepted the signal — NOW commit the intent + durable state.
            if action == "stop":
                self._cancel_requested = True
                self.state.update(status=status, cancel_requested_at=time.time())
            else:
                self._force_requested = True
                self.state.update(status=status)
            _atomic_json(self.state_path, self.state)
            return status

    def _finalize_completion(self, code: int) -> int:
        """One-way terminal latch for a NORMAL child exit. Reads the cancel/force
        flags and writes the terminal state atomically under the lock, so a
        concurrent control request cannot interleave and overwrite it. Returns the
        normalized exit code."""
        with self._state_lock:
            if self._force_requested:
                status, normalized = "force_stopped", 137
            elif self._cancel_requested:
                status, normalized = "cancelled", 130
            else:
                status, normalized = "finished", code
            self._finalized = True
            self.state.update(status=status, exit_code=normalized, finished_at=time.time())
            _atomic_json(self.state_path, self.state)
            return normalized

    def _finalize_crash(self, **values: Any) -> None:
        """One-way terminal latch for the crash path — a NO-OP if the run already
        finalized cleanly, so an async exception after a clean finalize cannot
        overwrite the terminal state with ``lost``."""
        with self._state_lock:
            if self._finalized:
                return
            self._finalized = True
            self.state.update(values)
            _atomic_json(self.state_path, self.state)

    def run(self) -> int:
        sock_path = _socket_path(self.job_id)
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
            if self.spec.get("op") == "board-run" and self.spec.get("board_db"):
                child_env["SYNCMILL_BOARD__ENABLED"] = "true"
                child_env["SYNCMILL_BOARD__DB_PATH"] = str(self.spec["board_db"])
            child_argv = [
                sys.executable, "-m", "syncmill.tui_jobs", "child",
                "--job-dir", str(self.job_dir),
                "--lock-fd", str(self.lock_fd),
                "--manager-fd", str(manager_read),
            ]
            pass_fds = [self.lock_fd, manager_read]
            if self.legacy_lock_fd >= 0:
                child_argv += ["--legacy-lock-fd", str(self.legacy_lock_fd)]
                pass_fds.append(self.legacy_lock_fd)
            child_argv += ["--", *self.spec["argv"]]
            self.child = subprocess.Popen(
                child_argv,
                cwd=self.spec["cwd"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                pass_fds=tuple(pass_fds),
                env=child_env,
            )
            os.close(manager_read)
            manager_read = -1
            self._mark_running(self.child.pid)
            assert self.child.stdout is not None
            while chunk := self.child.stdout.read(64 * 1024):
                log.write(chunk)
            code = self.child.wait()
            normalized = self._finalize_completion(code)
            # Terminal state is durably recorded and control can no longer
            # overwrite it → ack the guardian so it stands down WITHOUT tearing the
            # group down. Emitted ONLY here (the success path); the except/finally
            # never ack, so a crash always leaves the guardian to clean up. An
            # async exception after this point re-enters the handler but the
            # one-way latch drops its write, keeping ack ⟹ durable terminal.
            try:
                os.write(manager_write, _MANAGER_DONE)
            except OSError:
                pass
            return normalized
        except BaseException as exc:
            self._finalize_crash(
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
        # Signal delivery + intent publication + the finalize/grace gates are all
        # serialized under the state lock in _apply_control.
        status = self._apply_control(str(request.get("action")))
        return {"ok": True, "status": status}


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_group_exit(pgid: int, timeout: float) -> bool:
    """Poll until the process group has no members left, bounded by ``timeout``.
    Returns True if the group is gone. The process-group survival check is
    authoritative — a completion-pipe EOF proves only the CHILD exited, not a
    SIGINT-ignoring descendant sharing the group."""
    deadline = time.monotonic() + timeout
    while _process_group_exists(pgid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _guardian_main(args: argparse.Namespace) -> int:
    manager_fd = int(args.manager_fd)
    child_fd = int(args.child_fd)
    pgid = int(args.pgid)
    selector = selectors.DefaultSelector()
    # The ONLY clean-exit signal is the manager's explicit done-ack (sent after it
    # durably records the terminal state). Child EOF is informational: the child's
    # CLI finishing does NOT prove the manager recorded the outcome, nor that the
    # group has no surviving descendants, so it never triggers exit on its own.
    acked = False
    manager_open = True
    try:
        selector.register(manager_fd, selectors.EVENT_READ, "manager")
        selector.register(child_fd, selectors.EVENT_READ, "child")
        while manager_open:
            for key, _ in selector.select():
                data = os.read(key.fd, 1)
                if key.data == "manager":
                    if data == b"":
                        manager_open = False
                    elif data == _MANAGER_DONE:
                        acked = True
                elif data == b"":
                    # Child pipe EOF is informational only — drain and stop
                    # watching it; the manager's ack/EOF drives the decision.
                    selector.unregister(child_fd)
    finally:
        selector.close()

    if acked:
        return 0

    # Manager died WITHOUT a clean-completion ack — stop the child's whole process
    # group. The target and legacy-bridge lock fds are inherited (never closed
    # here) and stay held until this function returns; we FAIL CLOSED, never
    # returning while any group member survives.
    forced = False
    try:
        os.killpg(pgid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        pass
    # Grace for the group to exit on its own after SIGINT.
    _wait_for_group_exit(pgid, float(args.grace))
    # Then SIGKILL any survivor (a descendant that ignored SIGINT) and keep
    # polling+re-killing until the group is truly gone — the process-group
    # survival check, never a pipe EOF, is authoritative.
    while _process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            break  # gone between the check and the signal
        except PermissionError:
            # Can't signal: a foreign group (never our own session in practice)
            # or, on some platforms, an already-dead zombie awaiting reap. If we
            # already landed a SIGKILL the group WAS force-stopped; a lingering
            # zombie is reaped once its parent is gone. Either way, stop looping.
            break
        forced = True  # a SIGKILL actually landed
        _wait_for_group_exit(pgid, float(args.grace))
    if forced:
        message = "manager exited unexpectedly; child process group was force-stopped"
    elif not _process_group_exists(pgid):
        message = "manager exited unexpectedly; child process group was stopped"
    else:
        # Never landed a kill and the group persists — we could not signal it.
        message = (
            "manager exited unexpectedly; the child process group could not be "
            "signaled and may still be running"
        )
    state_path = Path(args.job_dir) / "state.json"
    state = _read_json(state_path) or {}
    state.update(status="lost", finished_at=time.time(), error=message)
    _atomic_json(state_path, state)
    return 0


def _child_main(args: argparse.Namespace) -> NoReturn:
    completion_read, completion_write = os.pipe()
    legacy_fd = int(getattr(args, "legacy_lock_fd", -1))
    guardian_argv = [
        sys.executable, "-m", "syncmill.tui_jobs", "guardian",
        "--job-dir", str(args.job_dir),
        "--lock-fd", str(args.lock_fd),
        "--manager-fd", str(args.manager_fd),
        "--child-fd", str(completion_read),
        "--pgid", str(os.getpgrp()),
        "--grace", str(args.grace),
    ]
    guardian_pass_fds = [int(args.lock_fd), int(args.manager_fd), completion_read]
    if legacy_fd >= 0:
        guardian_argv += ["--legacy-lock-fd", str(legacy_fd)]
        guardian_pass_fds.append(legacy_fd)
    guardian = subprocess.Popen(
        guardian_argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        pass_fds=tuple(guardian_pass_fds),
    )
    os.close(completion_read)
    os.close(int(args.manager_fd))
    os.close(int(args.lock_fd))
    if legacy_fd >= 0:
        # Release the child's copy; the guardian now holds the bridge.
        os.close(legacy_fd)
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    try:
        from syncmill.cli import cli

        cli(args=argv, prog_name="syncmill")
    finally:
        os.close(completion_write)
        try:
            guardian.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _worker_main(args: argparse.Namespace) -> int:
    job_dir = Path(args.job_dir)
    state_path = job_dir / "state.json"
    try:
        return _Manager(
            job_dir, int(args.lock_fd), int(args.legacy_lock_fd)
        ).run()
    except BaseException as exc:
        # PRESERVE a terminal receipt already recorded by the manager (the
        # guardian may have accepted the manager's ack and stood down). A crash in
        # run()'s finally AFTER a clean finalize/ack reaches here — it must NOT
        # overwrite finished/cancelled/force_stopped/lost. Only a run that never
        # reached a terminal state becomes launch_failed.
        state = _read_json(state_path) or {}
        if state.get("status") not in _TERMINAL:
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
    worker.add_argument("--legacy-lock-fd", type=int, default=-1)
    child = sub.add_parser("child")
    child.add_argument("--job-dir", required=True)
    child.add_argument("--lock-fd", required=True, type=int)
    child.add_argument("--legacy-lock-fd", type=int, default=-1)
    child.add_argument("--manager-fd", required=True, type=int)
    child.add_argument("--grace", type=float, default=_STOP_GRACE_SECONDS)
    child.add_argument("argv", nargs=argparse.REMAINDER)
    guardian = sub.add_parser("guardian")
    guardian.add_argument("--job-dir", required=True)
    guardian.add_argument("--lock-fd", required=True, type=int)
    guardian.add_argument("--legacy-lock-fd", type=int, default=-1)
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
