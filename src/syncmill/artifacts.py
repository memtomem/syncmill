"""Mandatory, durable run artifacts and explicit patch application."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Collection, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]

from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.git_env import isolated_git_env
from syncmill.worktree import (
    WorktreePool,
    WorkspaceError,
    capture_repository_snapshot,
    repository_fingerprint,
)


class ArtifactError(RuntimeError):
    pass


class ArtifactRecoveryRequired(ArtifactError):
    """Apply could not restore a single trustworthy source/artifact state."""

    def __init__(self, message: str, *, source_changed: bool):
        super().__init__(message)
        self.source_changed = source_changed


class ApplyLockBusy(ArtifactError):
    """A non-blocking apply-lock acquisition found the lock already held."""


if "_APPLY_LOCKS_GUARD" not in globals():
    _APPLY_LOCKS_GUARD = threading.Lock()
if "_APPLY_PROCESS_LOCKS" not in globals():
    _APPLY_PROCESS_LOCKS: dict[Path, threading.RLock] = {}
if "_APPLY_THREAD_STATE" not in globals():
    _APPLY_THREAD_STATE = threading.local()
if "_APPLY_FORK_GUARD" not in globals():
    _APPLY_FORK_GUARD = threading.RLock()
if "_APPLY_LOCK_FDS" not in globals():
    _APPLY_LOCK_FDS: set[int] = set()
if "_APPLY_AT_FORK_REGISTERED" not in globals():
    _APPLY_AT_FORK_REGISTERED = False


def _before_apply_fork() -> None:
    """Keep lock descriptors and their registry consistent across fork."""
    _APPLY_FORK_GUARD.acquire()


def _after_apply_fork_parent() -> None:
    _APPLY_FORK_GUARD.release()


def _after_apply_fork_child() -> None:
    """Drop inherited advisory locks and unusable thread state in the child."""
    global _APPLY_FORK_GUARD
    global _APPLY_LOCK_FDS
    global _APPLY_LOCKS_GUARD
    global _APPLY_PROCESS_LOCKS
    global _APPLY_THREAD_STATE

    inherited_fds = _APPLY_LOCK_FDS
    _APPLY_LOCK_FDS = set()
    for fd in inherited_fds:
        try:
            # Do not explicitly unlock: the parent still owns the shared open
            # file description and must retain its advisory lock.
            os.close(fd)
        except OSError:
            pass

    # Locks owned by threads that no longer exist cannot safely be acquired or
    # released.  Replace every piece of process-local bookkeeping instead.
    _APPLY_FORK_GUARD = threading.RLock()
    _APPLY_LOCKS_GUARD = threading.Lock()
    _APPLY_PROCESS_LOCKS = {}
    _APPLY_THREAD_STATE = threading.local()


register_at_fork = getattr(os, "register_at_fork", None)
if register_at_fork is not None and not _APPLY_AT_FORK_REGISTERED:
    register_at_fork(
        before=_before_apply_fork,
        after_in_parent=_after_apply_fork_parent,
        after_in_child=_after_apply_fork_child,
    )
    _APPLY_AT_FORK_REGISTERED = True


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Apply receipts are a few hundred bytes; anything far larger is a bad file, not
# evidence — the reconciler skips it without parsing (a cheap DoS/oversize guard).
_MAX_RECEIPT_BYTES = 64 * 1024


def _worker_alive(lease_owner: str | None) -> bool:
    """Best-effort local PID check for an expired detached apply lease.

    The owner string is ``"{pid}-{token}"`` (the same format rooms and the
    apply manifest both write), so one parser serves both callers.  A missing
    or unparsable owner reports dead, matching the recovery precedent that a
    strand we cannot prove live is safe to reconcile.
    """
    try:
        pid = int((lease_owner or "").split("-", 1)[0])
        if pid <= 0 or pid > 0xFFFFFFFF:
            return False
    except (TypeError, ValueError):
        return False

    # On Windows, ``os.kill(pid, 0)`` is destructive: signals other than the
    # console control events are implemented with TerminateProcess.  Use a
    # read-only process handle there, and never touch the POSIX zombie APIs.
    if os.name == "nt":
        return _windows_worker_alive(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if os.name != "posix":
        return True
    # A zombie still responds to ``kill(pid, 0)``.  Detached workers may be
    # direct children of the caller, so a non-blocking wait both detects and
    # reaps that state before recovery decides the worker is alive.
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        # The background Popen waiter may have reaped the child between kill(0)
        # and waitpid().  Probe once more instead of reporting that dead child as
        # alive until the next explicit recovery command.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
    else:
        if waited == pid:
            return False
    return True


def _windows_worker_alive(pid: int) -> bool:
    """Probe one Windows PID without signaling or mutating the process."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    error_invalid_parameter = 87
    wait_object_0 = 0
    wait_timeout = 0x00000102
    try:
        kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
        get_last_error = getattr(ctypes, "get_last_error")
    except (AttributeError, OSError):
        # An unavailable system probe must fail closed: a possibly live owner
        # is safer than recovering and racing its apply.
        return True

    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(
        process_query_limited_information | synchronize, False, pid
    )
    if not handle:
        # ERROR_INVALID_PARAMETER is the documented missing-PID result. Access
        # denial and every other uncertain probe failure must fail closed so a
        # possibly live owner is never raced by recovery.
        return get_last_error() != error_invalid_parameter
    try:
        wait_result = wait_for_single_object(handle, 0)
        if wait_result == wait_object_0:
            return False
        if wait_result == wait_timeout:
            return True
        return True
    finally:
        close_handle(handle)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _lock_file_descriptor(fd: int, *, blocking: bool = True) -> None:
    """Acquire an inherited-safe advisory lock.

    Blocking waits until the lock is available.  Non-blocking raises
    ``ApplyLockBusy`` when another holder owns it (the only "contended" signal),
    and propagates every other error unchanged so a genuine locking failure is
    never misread as contention.
    """
    if fcntl is not None:
        if blocking:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError as exc:
                raise ApplyLockBusy("apply lock is held") from exc
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise ApplyLockBusy("apply lock is held") from exc
                raise
    if msvcrt is None:  # pragma: no cover - every supported platform has one
        raise ArtifactError("cross-process apply locking is unavailable")

    if os.fstat(fd).st_size == 0:  # Windows byte-range locks need a real byte.
        os.write(fd, b"\0")
        os.fsync(fd)
    while True:  # LK_LOCK gives up after ten seconds; an apply may verify longer.
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:  # pragma: no cover - exercised on Windows
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if not blocking:
                raise ApplyLockBusy("apply lock is held") from exc
            time.sleep(0.05)


def _unlock_file_descriptor(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no branch - paired with acquisition
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _open_apply_lock_descriptor(lock_path: Path) -> tuple[int, int]:
    """Open and register a lock fd atomically with respect to fork."""
    with _APPLY_FORK_GUARD:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            owner_pid = os.getpid()
            _APPLY_LOCK_FDS.add(fd)
        except BaseException:
            os.close(fd)
            raise
        return fd, owner_pid


def _close_apply_lock_descriptor(fd: int) -> None:
    """Close and unregister a lock fd atomically with respect to fork."""
    with _APPLY_FORK_GUARD:
        try:
            os.close(fd)
        finally:
            _APPLY_LOCK_FDS.discard(fd)


@contextmanager
def _cross_process_lock(lock_path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Hold one re-entrant in-process and advisory cross-process file lock.

    With ``blocking=False`` a cross-process holder raises ``ApplyLockBusy``
    rather than waiting; a re-entrant same-thread hold always succeeds.
    """
    with _APPLY_LOCKS_GUARD:
        process_lock = _APPLY_PROCESS_LOCKS.setdefault(
            lock_path, threading.RLock()
        )

    # A non-blocking acquire must not wait on the in-process RLock either: if
    # another thread of this process holds it, that is contention.
    if not process_lock.acquire(blocking=blocking):
        raise ApplyLockBusy(f"apply lock {lock_path} is held")
    try:
        held: dict[Path, int] | None = getattr(_APPLY_THREAD_STATE, "held", None)
        if held is None:
            held = {}
            _APPLY_THREAD_STATE.held = held
        if lock_path in held:
            held[lock_path] += 1
            try:
                yield
            finally:
                held[lock_path] -= 1
            return

        try:
            fd, owner_pid = _open_apply_lock_descriptor(lock_path)
        except OSError as exc:
            raise ArtifactError(f"could not open apply lock {lock_path}: {exc}") from exc
        try:
            # `os.fchmod` is Unix-only: on Windows it does not exist at all, and
            # an AttributeError is outside the OSError boundary this module
            # converts, so it would escape `__enter__` and fail every locked
            # operation before the lock is even taken (`_restore_mode` in
            # teamfile.py hit the same edge). No path fallback is needed here
            # the way it is there: `_open_apply_lock_descriptor` already creates
            # the file 0600, the lock file holds nothing, and Windows honours
            # only the read-only bit regardless.
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                try:
                    fchmod(fd, 0o600)
                except OSError:  # pragma: no cover - permission model is platform-specific
                    pass
            try:
                _lock_file_descriptor(fd, blocking=blocking)
            except ApplyLockBusy:
                raise
            except OSError as exc:
                raise ArtifactError(
                    f"could not acquire apply lock {lock_path}: {exc}"
                ) from exc
            held[lock_path] = 1
            try:
                yield
            finally:
                held.pop(lock_path, None)
                if os.getpid() == owner_pid:
                    _unlock_file_descriptor(fd)
        finally:
            if os.getpid() == owner_pid:
                _close_apply_lock_descriptor(fd)
    finally:
        process_lock.release()


@dataclass(frozen=True)
class RunArtifact:
    run_id: str
    path: Path
    manifest: dict[str, Any]


def is_room_bound(manifest: dict[str, Any]) -> bool:
    """Room-bound runs are reconciled/applied by the room store, never
    standalone. Public so viewers (the TUI Artifacts pane) can mark them."""
    if manifest.get("strategy") == "room":
        return True
    plan = manifest.get("plan") or {}
    return isinstance(plan, dict) and plan.get("origin") == "room"


@dataclass(frozen=True)
class ApplyReconciliation:
    """One reconciler verdict for a single interrupted ``applying`` run."""

    run_id: str
    # completed | not_applied | manual_recovery | skipped_live_owner
    # | skipped_room_bound | skipped_not_applying | error
    outcome: str
    detail: str
    receipt: str | None = None


# The full run-manifest state vocabulary. The reconciler uses it to tell a
# benign non-applying run from a corrupt object manifest whose state is
# missing or unrecognized — the latter COULD be a mangled applying strand, so
# it must surface as an error row, never a silent skip.
_RUN_STATES = frozenset(
    {
        "running",
        "pending",
        "applying",
        "applied",
        "no_changes",
        "failed",
        "discarded",
        "superseded",
        "recovery_required",
    }
)


class ArtifactStore:
    def __init__(self, root: Path, project_id: str):
        self.root = root.expanduser().resolve() / project_id / "runs"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @staticmethod
    def _resolve_repository_root(repo_root: Path) -> Path:
        """Resolve an existing repository with one stable error contract.

        Python 3.13 changed ``Path.resolve(strict=False)`` so a symlink loop can
        be returned partially unresolved instead of raising ``RuntimeError``.
        Repositories used for apply/recovery must already exist, so strict
        resolution is both correct and version-independent.  Normalize the
        platform/Python-specific exception before it reaches a reconciliation
        report.
        """
        candidate = repo_root.expanduser()
        try:
            return candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactError(
                f"could not resolve repository root: {candidate}"
            ) from exc

    @staticmethod
    def _repository_apply_lock_path(repo_root: Path) -> Path:
        root = ArtifactStore._resolve_repository_root(repo_root)
        try:
            common_dir = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=str(root),
                env=isolated_git_env(),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ArtifactError(
                f"could not resolve repository apply lock for {root}"
            ) from exc
        if not common_dir:
            raise ArtifactError(f"could not resolve repository apply lock for {root}")
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = root / common_path
        try:
            common_path = common_path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ArtifactError(
                f"could not resolve repository apply lock for {root}"
            ) from exc
        if not common_path.is_dir():
            raise ArtifactError(
                f"repository common directory does not exist: {common_path}"
            )
        return common_path / ".syncmill-apply.lock"

    @contextmanager
    def apply_lock(
        self, repo_root: Path | None = None, *, blocking: bool = True
    ) -> Iterator[None]:
        """Serialize project source mutation and apply-state reconciliation.

        The artifact-store lock protects manifest state.  When ``repo_root`` is
        supplied, a second lock in Git's canonical common directory serializes
        source mutation even when another ArtifactStore root/project id points at
        the same repository.  Locks are always acquired store-first, repo-second,
        and are re-entrant within one thread.  With ``blocking=False`` a
        cross-process holder raises ``ApplyLockBusy`` instead of waiting (and the
        ExitStack unwinds any lock already taken), so the caller can hold the
        lock through its whole critical section without ever blocking on a live
        holder.
        """
        lock_paths = [self.root / ".apply.lock"]
        if repo_root is not None:
            repository_lock = self._repository_apply_lock_path(repo_root)
            if repository_lock not in lock_paths:
                lock_paths.append(repository_lock)
        with ExitStack() as stack:
            for lock_path in lock_paths:
                stack.enter_context(_cross_process_lock(lock_path, blocking=blocking))
            yield

    def _compare_and_swap_manifest(
        self,
        run_dir: Path,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> None:
        """Replace a manifest only when its complete on-disk revision matches."""
        try:
            current = json.loads(
                (run_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise ArtifactError(
                f"run {run_dir.name} changed or disappeared concurrently"
            ) from exc
        if current != expected:
            raise ArtifactError(
                f"run {run_dir.name} manifest changed concurrently; retry"
            )
        self._write_manifest(run_dir, replacement)

    def save(
        self,
        *,
        run_id: str,
        task: Task,
        result: TaskResult,
        repo_root: Path,
        snapshot_commit: str,
        source_fingerprint: str,
        strategy: str,
        parent_run_id: str | None = None,
        plan: dict[str, Any] | None = None,
    ) -> RunArtifact:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing: dict[str, Any] = {}
        manifest_path = run_dir / "manifest.json"
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ArtifactError(f"corrupt running artifact: {run_id}") from exc
            if existing.get("state") != "running":
                raise ArtifactError(f"run artifact already exists: {run_id}")
        patch = (result.patch or "").encode("utf-8", errors="surrogateescape")
        state = "pending" if result.status == TaskStatus.COMPLETED and patch else (
            "no_changes" if result.status == TaskStatus.COMPLETED else "failed"
        )
        manifest = {
            **existing,
            "schema_version": 1,
            "run_id": run_id,
            "project_id": self.root.parent.name,
            "parent_run_id": parent_run_id,
            "created_at": existing.get("created_at", _now()),
            "updated_at": _now(),
            "state": state,
            "repo_root": str(repo_root.resolve()),
            "source_fingerprint": source_fingerprint,
            "snapshot_commit": snapshot_commit,
            "task_id": str(task.id),
            "prompt": task.prompt,
            "strategy": strategy,
            "agent_id": result.agent_id,
            "files_changed": sorted(result.files_changed),
            "patch_sha256": _digest(patch),
            "patch_bytes": len(patch),
            "provider_session_id": result.provider_session_id,
            "conversation_id": result.conversation_id,
            "plan": plan,
        }
        _atomic_write(run_dir / "winner.patch", patch)
        _atomic_write(
            run_dir / "result.json",
            (json.dumps({
                "status": result.status.value,
                "output": result.output,
                "error": result.error,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
            }, ensure_ascii=False, indent=2) + "\n").encode(),
        )
        self._write_manifest(run_dir, manifest)
        return RunArtifact(run_id, run_dir, manifest)

    def begin(
        self,
        *,
        run_id: str,
        task: Task,
        repo_root: Path,
        snapshot_commit: str,
        source_fingerprint: str,
        strategy: str,
        agents: list[str],
        plan: dict[str, Any],
        parent_run_id: str | None = None,
    ) -> RunArtifact:
        """Create the crash-visible running manifest before any agent starts."""
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "project_id": self.root.parent.name,
            "parent_run_id": parent_run_id,
            "created_at": _now(),
            "updated_at": _now(),
            "state": "running",
            "repo_root": str(repo_root.resolve()),
            "source_fingerprint": source_fingerprint,
            "snapshot_commit": snapshot_commit,
            "task_id": str(task.id),
            "prompt": task.prompt,
            "strategy": strategy,
            "agents": agents,
            "plan": plan,
        }
        self._write_manifest(run_dir, manifest)
        return RunArtifact(run_id, run_dir, manifest)

    def load(self, run_id: str) -> RunArtifact:
        path = self.root / run_id
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as exc:
            # RecursionError: a pathologically-nested manifest is corrupt, not
            # a crash (the _claude_events / parse_board_update lesson).
            raise ArtifactError(f"unknown or corrupt run: {run_id}") from exc
        if not isinstance(manifest, dict):
            # Valid JSON but not an object (a list/scalar) would crash every
            # `manifest.get` downstream — it is a corrupt run, same as bad JSON.
            raise ArtifactError(f"unknown or corrupt run: {run_id}")
        return RunArtifact(run_id, path, manifest)

    def list(self) -> list[RunArtifact]:
        if not self.root.exists():
            return []
        artifacts: list[RunArtifact] = []
        for path in self.root.iterdir():
            if path.is_dir():
                try:
                    artifacts.append(self.load(path.name))
                except ArtifactError:
                    continue
        return sorted(
            artifacts,
            key=lambda artifact: str(artifact.manifest.get("created_at", "")),
            reverse=True,
        )

    def prune(self, keep: int, *, dry_run: bool = False) -> list[str]:
        """Remove old terminal runs; pending/recovery state is never pruned."""
        with self.apply_lock():
            removable_states = {
                "applied", "no_changes", "failed", "discarded", "superseded"
            }
            artifacts = self.list()
            protected = set(artifact.run_id for artifact in artifacts[:keep])
            removed: list[str] = []
            for artifact in artifacts:
                if artifact.run_id in protected:
                    continue
                if artifact.manifest.get("state") not in removable_states:
                    continue
                removed.append(artifact.run_id)
                if not dry_run:
                    shutil.rmtree(artifact.path)
            return removed

    def patch(self, run_id: str) -> bytes:
        artifact = self.load(run_id)
        data = (artifact.path / "winner.patch").read_bytes()
        if _digest(data) != artifact.manifest.get("patch_sha256"):
            raise ArtifactError(f"patch digest mismatch for run {run_id}")
        return data

    def validate_applicable(self, run_id: str) -> RunArtifact:
        """Structural preflight for announcing a detached apply.

        Verifies the manifest carries the fields apply needs and that the
        patch bytes match their recorded digest — so "detached job launched"
        can never precede a guaranteed worker crash on a hollow/hand-edited
        manifest. The worker still fully revalidates under the apply lock.
        """
        artifact = self.load(run_id)
        manifest = artifact.manifest
        repo_root = manifest.get("repo_root")
        if not isinstance(repo_root, str) or not repo_root:
            raise ArtifactError(
                f"unknown or corrupt run: {run_id} (missing repo_root)"
            )
        try:
            # A malformed path (embedded NUL → ValueError, symlink loop →
            # RuntimeError, I/O failure → OSError) must fail the preflight,
            # not crash the worker after "detached job launched".
            Path(repo_root).resolve()
        except (ValueError, OSError, RuntimeError) as exc:
            raise ArtifactError(
                f"unknown or corrupt run: {run_id} (invalid repo_root: {exc})"
            ) from exc
        if not isinstance(manifest.get("source_fingerprint"), str):
            raise ArtifactError(
                f"unknown or corrupt run: {run_id} (missing source_fingerprint)"
            )
        try:
            self.patch(run_id)  # digest-verified read
        except OSError as exc:
            raise ArtifactError(
                f"unknown or corrupt run: {run_id} (missing patch)"
            ) from exc
        return artifact

    def apply(
        self,
        run_id: str,
        repo_root: Path,
        source_fingerprint: str,
        *,
        verify_commands: list[str] | None = None,
        verify_timeout: int = 300,
        allow_room_bound: bool = False,
    ) -> Path:
        with self.apply_lock(repo_root):
            return self._apply_locked(
                run_id,
                repo_root,
                source_fingerprint,
                verify_commands=verify_commands,
                verify_timeout=verify_timeout,
                allow_room_bound=allow_room_bound,
            )

    def _apply_locked(
        self,
        run_id: str,
        repo_root: Path,
        source_fingerprint: str,
        *,
        verify_commands: list[str] | None = None,
        verify_timeout: int = 300,
        allow_room_bound: bool = False,
    ) -> Path:
        artifact = self.load(run_id)
        git_env = isolated_git_env()
        manifest = artifact.manifest
        if is_room_bound(manifest) and not allow_room_bound:
            # Room-bound runs are applied ONLY through RoomCoordinator.apply
            # (which passes allow_room_bound=True after its selection/revision
            # CAS): a standalone apply would bypass the Room lifecycle, mutate
            # the source, and leave Room state inconsistent.
            raise ArtifactError(
                f"run {run_id} is room-bound; use 'syncmill room apply'"
            )
        if manifest.get("state") != "pending":
            raise ArtifactError(
                f"run {run_id} is {manifest.get('state')!r}, not pending"
            )
        recorded_root = manifest.get("repo_root")
        if not isinstance(recorded_root, str) or not recorded_root:
            # A hollow/hand-edited manifest must fail as a corrupt run, never
            # as a raw KeyError escaping the worker's error boundary.
            raise ArtifactError(
                f"unknown or corrupt run: {run_id} (missing repo_root)"
            )
        try:
            resolved_recorded = Path(recorded_root).resolve()
        except (ValueError, OSError, RuntimeError) as exc:
            # Embedded NUL / symlink loop / I/O failure: corrupt run, not a
            # raw exception escaping the worker's error boundary.
            raise ArtifactError(
                f"unknown or corrupt run: {run_id} (invalid repo_root: {exc})"
            ) from exc
        if resolved_recorded != repo_root.resolve():
            raise ArtifactError("run belongs to a different repository")
        if manifest.get("source_fingerprint") != source_fingerprint:
            raise ArtifactError(
                "working tree changed since the run; review the diff and rerun"
            )
        patch_path = artifact.path / "winner.patch"
        patch_data = self.patch(run_id)
        check_apply = subprocess.run(
            ["git", "apply", "--check", "--binary", str(patch_path)],
            cwd=str(repo_root), env=git_env, capture_output=True, text=True,
        )
        if check_apply.returncode != 0:
            raise ArtifactError(check_apply.stderr.strip() or "git apply check failed")

        if verify_commands:
            self._verify_in_isolation(
                run_id=run_id,
                repo_root=repo_root,
                patch=patch_data,
                commands=verify_commands,
                timeout=verify_timeout,
            )
            if repository_fingerprint(repo_root) != source_fingerprint:
                raise ArtifactError(
                    "working tree changed during isolated verification; retry"
                )
            check_apply = subprocess.run(
                ["git", "apply", "--check", "--binary", str(patch_path)],
                cwd=str(repo_root), env=git_env, capture_output=True, text=True,
            )
            if check_apply.returncode != 0:
                raise ArtifactError(
                    check_apply.stderr.strip() or "git apply recheck failed"
                )

        # No fingerprint here: the pre-mutation check below (`final_fingerprint`)
        # runs moments later and is the one that gates source mutation. A full
        # fingerprint reads every untracked file — one redundant pass per apply
        # was the cost of failing a few statements earlier; a changed tree now
        # fails at the pre-mutation check with the manifest restored to pending.
        original_manifest = dict(manifest)
        applying_manifest = {
            **manifest,
            "state": "applying",
            "apply_id": str(uuid4()),
            "apply_owner": f"{os.getpid()}-{uuid4().hex[:8]}",
            "apply_started_at": _now(),
            "updated_at": _now(),
        }
        # Crash visibility comes before source mutation. A process death after the
        # next write is distinguishable from a never-started pending apply.
        self._compare_and_swap_manifest(
            artifact.path, original_manifest, applying_manifest
        )

        def restore_pending_before_source_mutation() -> None:
            try:
                self._compare_and_swap_manifest(
                    artifact.path, applying_manifest, original_manifest
                )
            except BaseException as restore_exc:
                raise ArtifactRecoveryRequired(
                    "source was not modified, but the run manifest could not be restored",
                    source_changed=False,
                ) from restore_exc

        try:
            final_fingerprint = repository_fingerprint(repo_root)
        except BaseException:
            restore_pending_before_source_mutation()
            raise
        if final_fingerprint != source_fingerprint:
            restore_pending_before_source_mutation()
            raise ArtifactError("working tree changed before patch application; retry")

        try:
            apply_patch = subprocess.run(
                ["git", "apply", "--binary", str(patch_path)],
                cwd=str(repo_root), env=git_env, capture_output=True, text=True,
            )
        except OSError as spawn_exc:
            restore_pending_before_source_mutation()
            raise ArtifactError(f"could not start git apply: {spawn_exc}") from spawn_exc
        if apply_patch.returncode != 0:
            restore_pending_before_source_mutation()
            raise ArtifactError(apply_patch.stderr.strip() or "git apply failed")

        receipt_dir = artifact.path / "applications"
        receipt: Path | None = None
        try:
            receipt_dir.mkdir(mode=0o700, exist_ok=True)
            receipt = receipt_dir / (
                f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
            )
            _atomic_write(
                receipt,
                (
                    json.dumps(
                        {
                            "applied_at": _now(),
                            "repo_root": str(repo_root.resolve()),
                            "patch_sha256": manifest["patch_sha256"],
                            "apply_id": applying_manifest["apply_id"],
                            "run_id": run_id,
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode(),
            )
            applied_manifest = {
                **applying_manifest,
                "state": "applied",
                "updated_at": _now(),
            }
            self._compare_and_swap_manifest(
                artifact.path, applying_manifest, applied_manifest
            )
            return receipt
        except BaseException as bookkeeping_exc:
            # Invalidate the bound receipt BEFORE reversing the source. The
            # receipt is proof the patch is applied; if a crash interrupted the
            # rollback with the receipt still present over a reverted tree, the
            # reconciler would later read "bound receipt + drifted fingerprint"
            # as completed and mark the run applied with the change absent. Abort
            # the rollback if the receipt cannot be removed, rather than reverse
            # the source underneath a surviving receipt.
            if receipt is not None:
                try:
                    receipt.unlink(missing_ok=True)
                except OSError as unlink_exc:
                    raise ArtifactRecoveryRequired(
                        "apply bookkeeping failed and the receipt could not be "
                        "invalidated before rollback; manual recovery is required",
                        source_changed=True,
                    ) from unlink_exc
            rollback = subprocess.run(
                ["git", "apply", "--reverse", "--check", "--binary", str(patch_path)],
                cwd=str(repo_root), env=git_env, capture_output=True, text=True,
            )
            if rollback.returncode == 0:
                rollback = subprocess.run(
                    ["git", "apply", "--reverse", "--binary", str(patch_path)],
                    cwd=str(repo_root), env=git_env, capture_output=True, text=True,
                )
            rolled_back = (
                rollback.returncode == 0
                and repository_fingerprint(repo_root) == source_fingerprint
            )
            if not rolled_back:
                raise ArtifactRecoveryRequired(
                    "source patch was applied but durable bookkeeping failed; "
                    "manual recovery is required",
                    source_changed=True,
                ) from bookkeeping_exc
            try:
                self._compare_and_swap_manifest(
                    artifact.path, applying_manifest, original_manifest
                )
            except BaseException as restore_exc:
                raise ArtifactRecoveryRequired(
                    "source was rolled back but apply bookkeeping could not be restored",
                    source_changed=False,
                ) from restore_exc
            raise ArtifactError(
                "apply bookkeeping failed; source patch was rolled back"
            ) from bookkeeping_exc

    @staticmethod
    def _verify_in_isolation(
        *,
        run_id: str,
        repo_root: Path,
        patch: bytes,
        commands: list[str],
        timeout: int,
    ) -> None:
        """Verify the candidate in a disposable clone; never run gates in source."""
        with tempfile.TemporaryDirectory(prefix="syncmill-apply-verify-") as temp:
            # sweepable=True: a SIGKILL during verification skips both this
            # `with` block's cleanup and the pool's finally — without the run
            # sentinel the stranded clone in the tempdir could never be
            # reclaimed by sweep_orphaned_workspaces.
            snapshot = capture_repository_snapshot(
                repo_root,
                f"verify-{run_id}",
                Path(temp),
                sweepable=True,
            )
            pool = WorktreePool(repo_root, f"verify-{run_id}", snapshot=snapshot)
            try:
                worktree = pool.acquire("apply-verify")
                restored = subprocess.run(
                    ["git", "apply", "--binary", "-"],
                    cwd=str(worktree.path), env=isolated_git_env(),
                    input=patch, capture_output=True,
                )
                if restored.returncode != 0:
                    raise ArtifactError(
                        restored.stderr.decode(errors="replace").strip()
                        or "could not restore patch for verification"
                    )
                for command in commands:
                    try:
                        check = subprocess.run(
                            shlex.split(command),
                            cwd=str(worktree.path),
                            env=isolated_git_env(),
                            capture_output=True,
                            text=True,
                            timeout=timeout,
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise ArtifactError(
                            f"isolated verification failed ({command}): {exc}"
                        ) from exc
                    if check.returncode != 0:
                        failure = check.stderr.strip() or check.stdout.strip()
                        raise ArtifactError(
                            f"isolated verification failed ({command}): {failure}"
                        )
            finally:
                pool.cleanup_all()

    def discard(self, run_id: str) -> None:
        with self.apply_lock():
            artifact = self.load(run_id)
            current = artifact.manifest
            if current.get("state") != "pending":
                raise ArtifactError("only a pending run can be discarded")
            replacement = {
                **current,
                "state": "discarded",
                "updated_at": _now(),
            }
            self._compare_and_swap_manifest(artifact.path, current, replacement)

    def supersede(self, run_id: str, child_run_id: str) -> None:
        with self.apply_lock():
            artifact = self.load(run_id)
            current = artifact.manifest
            if current.get("state") != "pending":
                return
            replacement = {
                **current,
                "state": "superseded",
                "superseded_by": child_run_id,
                "updated_at": _now(),
            }
            self._compare_and_swap_manifest(artifact.path, current, replacement)

    def close_conversation(self, run_id: str) -> None:
        with self.apply_lock():
            artifact = self.load(run_id)
            current = artifact.manifest
            replacement = {
                **current,
                "conversation_closed": True,
                "updated_at": _now(),
            }
            self._compare_and_swap_manifest(artifact.path, current, replacement)

    def recovery_required(
        self,
        run_id: str,
        reason: str,
        *,
        expected_states: Collection[str] = ("running", "pending", "applying"),
        blocking: bool = True,
    ) -> bool:
        """Freeze an unfinished run without overwriting a terminal apply state.

        ``blocking=False`` raises :class:`ApplyLockBusy` instead of waiting on a
        live lock holder — the Supervisor's crash path uses it so a Ctrl-C
        handler never hangs behind another process's long apply/verify.
        """
        with self.apply_lock(blocking=blocking):
            artifact = self.load(run_id)
            current = artifact.manifest
            if current.get("state") not in expected_states:
                return False
            replacement = {
                **current,
                "state": "recovery_required",
                "recovery_reason": reason[:1000],
                "updated_at": _now(),
            }
            self._compare_and_swap_manifest(artifact.path, current, replacement)
            return True

    @staticmethod
    def _is_room_bound(manifest: dict[str, Any]) -> bool:
        """Room-bound runs are reconciled by the room store, never here."""
        return is_room_bound(manifest)

    @staticmethod
    def _bound_receipt(run_dir: Path, manifest: dict[str, Any]) -> Path | None:
        """Return a receipt that provably belongs to *this* apply attempt.

        A receipt is evidence only when it is a JSON object carrying this run's
        ``apply_id``, ``run_id`` and ``patch_sha256``.  Legacy receipts without
        ``apply_id`` are non-evidence (fail closed): an in-process rollback
        leaves a stale receipt behind on the crash path, so an unbound receipt
        can never prove the source was mutated.  A malformed or non-object
        receipt file is skipped, never raised (one bad file must not abort the
        whole recovery sweep), and a receipt whose ``run_id`` differs is another
        run's evidence, never accepted for this one.
        """
        apply_id = manifest.get("apply_id")
        patch_sha256 = manifest.get("patch_sha256")
        run_id = manifest.get("run_id")
        if not apply_id:
            return None
        receipt_dir = run_dir / "applications"
        if not receipt_dir.is_dir():
            return None
        for receipt in sorted(receipt_dir.glob("*.json")):
            try:
                if receipt.stat().st_size > _MAX_RECEIPT_BYTES:
                    # Our receipts are a few hundred bytes; an oversized file is
                    # a bad artifact, not evidence — skip it without parsing.
                    continue
                # ``json.loads`` raises RecursionError (a RuntimeError, not a
                # ValueError) on pathologically nested input; catch it so one
                # malformed file can never abort the whole recovery sweep.
                payload = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                continue
            if not isinstance(payload, dict):
                continue
            if (
                payload.get("apply_id") == apply_id
                and payload.get("run_id") == run_id
                and payload.get("patch_sha256") == patch_sha256
            ):
                return receipt
        return None

    @staticmethod
    def _reverse_check(repo_root: Path, patch_path: Path) -> str:
        """Advisory probe: is the run's patch reverse-applicable right now?

        Never drives a verdict — a reverse-applicable patch only proves the
        content is present now, not that this attempt performed the mutation.
        The result is embedded in the freeze reason for an operator to read.
        """
        try:
            probe = subprocess.run(
                ["git", "apply", "--reverse", "--check", "--binary", str(patch_path)],
                cwd=str(repo_root),
                env=isolated_git_env(),
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            return f"reverse-check unavailable ({exc})"
        return (
            "patch is reverse-applicable"
            if probe.returncode == 0
            else "patch is not reverse-applicable"
        )

    def reconcile_interrupted_applies(
        self, *, run_id: str | None = None
    ) -> list[ApplyReconciliation]:
        """Classify and resolve standalone ``applying`` runs left by a crash.

        Scans (or targets one run), skips live owners and room-bound runs, and
        for each dead-owner ``applying`` strand decides — under the project
        apply lock — one of: completed (finish the interrupted flip), not
        applied (restore to pending), or manual recovery (freeze).  Never raises
        per run: an environment or store error becomes an ``error`` row that
        leaves the run untouched (``applying`` stays prune-protected, so the run
        can be reconciled again once the environment returns).
        """
        # One ordered sequence: a corrupt run's "error" row keeps its place in
        # the sorted enumeration beside the healthy runs around it.
        scanned: list[RunArtifact | ApplyReconciliation] = []
        if run_id is not None:
            try:
                scanned = [self.load(run_id)]
            except ArtifactError as exc:
                return [ApplyReconciliation(run_id, "error", str(exc))]
        elif self.root.exists():
            # Enumerate run dirs directly rather than via list(), which
            # silently skips corrupt manifests — a sweep must surface an
            # unreadable run as an "error" row, never a false all-clear.
            for path in sorted(self.root.iterdir()):
                if not path.is_dir():
                    continue
                try:
                    scanned.append(self.load(path.name))
                except ArtifactError as exc:
                    scanned.append(ApplyReconciliation(path.name, "error", str(exc)))
        reports: list[ApplyReconciliation] = []
        for item in scanned:
            if isinstance(item, ApplyReconciliation):
                reports.append(item)
                continue
            artifact = item
            manifest = artifact.manifest
            state = manifest.get("state")
            if state != "applying":
                if not isinstance(state, str) or state not in _RUN_STATES:
                    # A dict manifest with a missing/unrecognized state is
                    # corrupt, not benign — skipping it silently would
                    # recreate the false all-clear this sweep exists to
                    # prevent (it COULD be a mangled applying strand).
                    reports.append(
                        ApplyReconciliation(
                            artifact.run_id,
                            "error",
                            f"corrupt manifest: unrecognized state {state!r}",
                        )
                    )
                elif run_id is not None:
                    # An explicit target that is not mid-apply is nothing to do,
                    # not a failure — a benign skip, so `recover` still exits 0.
                    reports.append(
                        ApplyReconciliation(
                            artifact.run_id,
                            "skipped_not_applying",
                            f"run is {state!r}, not applying",
                        )
                    )
                continue
            reports.append(self._reconcile_one(artifact))
        return reports

    def _reconcile_one(self, artifact: RunArtifact) -> ApplyReconciliation:
        run_id = artifact.run_id
        manifest = artifact.manifest
        if self._is_room_bound(manifest):
            return ApplyReconciliation(
                run_id,
                "skipped_room_bound",
                "room-bound run; use 'syncmill room recover'",
            )
        try:
            repo_root = self._resolve_repository_root(Path(manifest["repo_root"]))
            # Ownership is decided by who holds the repository apply lock, not by
            # the recorded PID (which a crash could see reused): a held lock is a
            # live apply, a free lock is a crashed one.  A single NON-BLOCKING
            # acquire held through classification is both the liveness check and
            # the mutation guard — there is no probe/acquire gap in which a new
            # apply could start and make recovery block on it.
            try:
                with self.apply_lock(repo_root, blocking=False):
                    current = self.load(run_id)
                    cur = current.manifest
                    if cur.get("state") != "applying":
                        return ApplyReconciliation(
                            run_id,
                            "skipped_live_owner",
                            f"resolved concurrently to {cur.get('state')!r}",
                        )
                    return self._classify_and_act(current, repo_root)
            except ApplyLockBusy:
                # The lock is held, but by WHOM is unknowable here: it may be an
                # unrelated concurrent apply, and the recorded owner's process may
                # already be gone. Report the recorded owner as a hint, never as
                # the identified holder.
                owner = manifest.get("apply_owner")
                detail = (
                    f"the repository apply lock is held (recorded apply owner: {owner})"
                    if owner
                    else "the repository apply lock is held by a live apply"
                )
                return ApplyReconciliation(run_id, "skipped_live_owner", detail)
        except (
            ArtifactError,
            WorkspaceError,
            OSError,
            # A corrupt/hand-edited manifest (missing or non-string repo_root,
            # a NUL-bearing path) must yield this run's "error" row, never
            # abort the whole sweep — the docstring's "never raises per run".
            KeyError,
            TypeError,
            ValueError,
            # RuntimeError: Path.resolve() on a symlink loop — reachable from
            # the manifest repo_root above AND from apply_lock's git-common-dir
            # resolution — plus RecursionError (its subclass) from a
            # pathological concurrent manifest rewrite.
            RuntimeError,
        ) as exc:
            # KeyError's str() is just the missing key — keep the type name so
            # the row explains itself.
            return ApplyReconciliation(run_id, "error", f"{type(exc).__name__}: {exc}")

    def _classify_and_act(
        self, artifact: RunArtifact, repo_root: Path
    ) -> ApplyReconciliation:
        run_id = artifact.run_id
        manifest = artifact.manifest
        receipt = self._bound_receipt(artifact.path, manifest)
        current_fingerprint = repository_fingerprint(repo_root)
        source_matches = current_fingerprint == manifest.get("source_fingerprint")

        if receipt is not None and not source_matches:
            # completed: the patch was applied and receipted; only the final
            # applying -> applied flip was interrupted.  Finish it.
            applied_manifest = {
                **manifest,
                "state": "applied",
                "updated_at": _now(),
            }
            self._compare_and_swap_manifest(artifact.path, manifest, applied_manifest)
            return ApplyReconciliation(
                run_id,
                "completed",
                "interrupted apply finished",
                receipt=str(receipt),
            )

        if receipt is None and source_matches:
            # not applied: the source was never mutated, so the applying record
            # is safe to unwind back to a fresh pending run.
            pending_manifest = {
                key: value
                for key, value in manifest.items()
                if key not in ("apply_id", "apply_owner", "apply_started_at")
            }
            pending_manifest["state"] = "pending"
            pending_manifest["updated_at"] = _now()
            self._compare_and_swap_manifest(
                artifact.path, manifest, pending_manifest
            )
            return ApplyReconciliation(
                run_id, "not_applied", "restored to pending; source was not modified"
            )

        # Everything else is undecidable evidence: a bound receipt with a
        # matching fingerprint (patch applied then externally reverted), or no
        # receipt with a mismatched fingerprint (mutated with no proof of a
        # complete apply).  Freeze for an operator; never auto-roll-back, which
        # could destroy identical work a human applied by hand.
        patch_path = artifact.path / "winner.patch"
        reverse = (
            self._reverse_check(repo_root, patch_path)
            if patch_path.exists()
            else "winner.patch missing"
        )
        reason = (
            "interrupted apply is undecidable: "
            f"receipt={'present' if receipt else 'absent'}, "
            f"source_matches={source_matches}, {reverse}. "
            "Inspect the repository and resolve manually."
        )
        self.recovery_required(run_id, reason, expected_states=("applying",))
        return ApplyReconciliation(
            run_id,
            "manual_recovery",
            reason,
            receipt=str(receipt) if receipt else None,
        )

    @staticmethod
    def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
        _atomic_write(
            run_dir / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        )
