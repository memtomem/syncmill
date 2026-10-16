"""Mandatory, durable run artifacts and explicit patch application."""

from __future__ import annotations

import errno
import hashlib
import json
import contextlib
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from functools import wraps
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

from syncmill.artifact_io import ArtifactIO, validate_id
from syncmill.platform_support import require_supported_platform
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.git_env import isolated_git_env
from syncmill.gates import CommandGate
from syncmill.worktree import (
    WorktreePool,
    WorkspaceError,
    _run_git,
    capture_repository_snapshot,
    repository_fingerprint,
)

#: Hang bound for every git subprocess on the apply/rollback path (via
#: worktree._run_git, the owned-group runner — a wedged git can no longer hang
#: an apply forever, and its filter/fsmonitor children die with it). Matches
#: _run_git's own default: hang protection, not a latency bound.
_APPLY_GIT_TIMEOUT_SECONDS = 600.0


def _git_timed_out(proc: subprocess.CompletedProcess) -> bool:
    """Exact _run_git-timeout detection: rc 124 AND its timeout note.

    ``git apply`` fails with 1/128, never 124 in practice, but the stderr note
    check makes this exact rather than convention-based — a timed-out apply
    routes into recovery handling, never into a site's ordinary-failure idiom.
    """
    return proc.returncode == 124 and str(proc.stderr).startswith(
        "git command timed out"
    )


def _run_patch_git(patch: bytes, cmd: list[str], **kwargs):
    """Use exactly the digest-verified bytes, including non-UTF8 filenames."""
    kwargs.pop("text", None)
    result = _run_git(cmd, input=patch, **kwargs)
    for field in ("stdout", "stderr"):
        value = getattr(result, field)
        if isinstance(value, bytes):
            setattr(result, field, value.decode("utf-8", errors="replace"))
    return result


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


def _open_apply_lock_descriptor(
    lock_path: Path, opener: Callable[[Path, int], int] | None = None,
) -> tuple[int, int]:
    """Open and register a lock fd atomically with respect to fork."""
    with _APPLY_FORK_GUARD:
        fd = (
            opener(lock_path, os.O_CREAT | os.O_RDWR) if opener else
            os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        )
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
def _cross_process_lock(
    lock_path: Path, *, blocking: bool = True,
    opener: Callable[[Path, int], int] | None = None,
) -> Iterator[None]:
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
            fd, owner_pid = (
                _open_apply_lock_descriptor(lock_path, opener) if opener
                else _open_apply_lock_descriptor(lock_path)
            )
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


def workflow_origin_id(manifest: dict[str, Any]) -> str | None:
    """The workflow a run artifact came from, or ``None`` if it came from none.

    The manifest is what an artifact SAYS about itself, so it — not the shape of
    the run id — decides whether a workflow has to approve applying it. A
    workflow-origin artifact carrying no usable id returns the empty string: it
    is still workflow-bound, and a caller must not read "no workflow to ask" out
    of a missing field (`is_room_bound` reads the same block the same way).
    """
    plan = manifest.get("plan") or {}
    if not isinstance(plan, dict) or plan.get("origin") != "workflow":
        return None
    workflow_id = plan.get("workflow_id")
    return workflow_id if isinstance(workflow_id, str) else ""


def strip_approval(plan: dict[str, Any] | None) -> dict[str, Any]:
    """A producer-supplied plan with any approval claim removed.

    Approval is written by exactly one method, after a workflow has committed
    its completion. Everything else that builds an artifact — including the
    workflow coordinator itself — passes its plan through here, so "the run was
    approved" can never be something a producer simply asserted at save time.
    """
    return {
        key: value for key, value in (plan or {}).items() if key != "approved"
    }


def workflow_approved(manifest: dict[str, Any]) -> bool:
    """Whether the workflow that produced this run recorded its completion.

    A workflow publishes its final artifact BEFORE the lease-fenced write that
    commits the completion, so for a moment the artifact exists for a workflow
    that may still end up cancelled. The approval is written after that commit
    succeeds, which makes the artifact carry its own authorization instead of
    depending on a workflow row that ``workflow prune`` is entitled to delete.

    It is a RECORD, not a flag, and it is checked against the artifact it sits
    in: it names the workflow that granted it, the run it was granted for, and
    the digest of the patch that was approved. A bare boolean would authorize
    whatever it was pasted onto — an artifact copied from another project, a
    run whose patch was replaced afterwards, or a manifest that simply claimed
    it. Each of those now fails a comparison against the surrounding manifest.

    What this cannot do is turn approval into a secret. Anyone able to write
    files into the run directory can write a consistent record, just as they
    could write an ordinary artifact with any patch and apply that — a boundary
    this guard was never standing on. It rules out the accidents that actually
    happen: a producer asserting its own approval, a copied or re-published
    artifact carrying someone else's, and a patch changed after the fact.
    """
    plan = manifest.get("plan")
    if not isinstance(plan, dict):
        return False
    approval = plan.get("approved")
    if not isinstance(approval, dict):
        return False
    workflow_id = plan.get("workflow_id")
    return bool(
        workflow_id
        and approval.get("workflow_id") == workflow_id
        and approval.get("run_id") == manifest.get("run_id")
        and approval.get("patch_sha256") == manifest.get("patch_sha256")
    )


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


def _artifact_access(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._access():
            # A public call owns only handles it opened. Nested mutation steps
            # retain handles pinned by their caller until that caller finishes.
            pinned = set(self._io.fds)
            try:
                return method(self, *args, **kwargs)
            finally:
                for path in reversed(list(self._io.fds)):
                    if path not in pinned:
                        self._io.release(path)
    return wrapped


class ArtifactStore:
    def __init__(self, root: Path, project_id: str):
        require_supported_platform()
        try:
            validate_id(project_id)
            self._base = root.expanduser().resolve()
            self._base.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root = self._base / project_id / "runs"
            self._local = threading.local()
            io = ArtifactIO(self._base)
            try:
                fd = io.directory(self.root, create=True)
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o700)
                info = os.fstat(fd)
                self._identity = (info.st_dev, info.st_ino)
            finally:
                io.close()
        except (OSError, ValueError) as exc:
            raise ArtifactError(f"invalid artifact store: {exc}") from exc

    @contextmanager
    def _access(self) -> Iterator[None]:
        if getattr(self._local, "io", None) is not None:
            yield
            return
        io = None
        try:
            io = ArtifactIO(self._base)
            info = os.fstat(io.directory(self.root))
            if (info.st_dev, info.st_ino) != self._identity:
                raise ArtifactError("artifact store directory changed; restart or reopen the store")
            self._local.io = io
            yield
        except FileExistsError as exc:
            raise ArtifactError(f"artifact already exists: {exc.filename}") from exc
        except FileNotFoundError as exc:
            raise ArtifactError(f"artifact not found: {exc.filename}") from exc
        except (OSError, ValueError) as exc:
            raise ArtifactError(f"unsafe or unreadable artifact: {exc}") from exc
        finally:
            self._local.io = None
            if io is not None:
                io.close()

    @property
    def _io(self) -> ArtifactIO:
        return self._local.io

    def _run_path(self, run_id: str) -> Path:
        try:
            validate_id(run_id)
        except ValueError as exc:
            raise ArtifactError(f"invalid run ID: {run_id!r}") from exc
        return self.root / run_id

    def _check_binding(self, run_id: str, manifest: dict) -> None:
        for key, expected in (("run_id", run_id), ("project_id", self.root.parent.name)):
            if key in manifest and manifest[key] != expected:
                raise ArtifactError(f"artifact {key} mismatch for run {run_id}")

    @_artifact_access
    def read_result(
        self, run_id: str, *, limit: int = -1, missing_ok: bool = False,
    ) -> bytes | None:
        """Read a sidecar safely; optionally distinguish absence for the TUI."""
        artifact = self._load_pinned(run_id)
        try:
            return self._io.read(artifact.path / "result.json", limit)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise

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
            common_dir = _run_git(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=str(root),
                env=isolated_git_env(),
                capture_output=True,
                text=True,
                check=True,
                timeout=_APPLY_GIT_TIMEOUT_SECONDS,
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
        with self._access(), ExitStack() as stack:
            for lock_path in lock_paths:
                stack.enter_context(_cross_process_lock(
                    lock_path, blocking=blocking,
                    opener=self._io.open_file if lock_path == self.root / ".apply.lock" else None,
                ))
            yield

    @_artifact_access
    def _compare_and_swap_manifest(
        self,
        run_dir: Path,
        expected: dict[str, Any],
        replacement: dict[str, Any],
    ) -> None:
        """Replace a manifest only when its complete on-disk revision matches."""
        try:
            current = json.loads(
                self._io.read(run_dir / "manifest.json")
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

    @_artifact_access
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
        if result.status == TaskStatus.COMPLETED and result.patch is None:
            raise ArtifactError("completed result is missing its mandatory patch")
        run_dir = self._run_path(run_id)
        self._io.mkdir(run_dir)
        existing: dict[str, Any] = {}
        manifest_path = run_dir / "manifest.json"
        if self._io.exists(manifest_path):
            try:
                existing = json.loads(self._io.read(manifest_path))
            except (OSError, ValueError) as exc:
                raise ArtifactError(f"corrupt running artifact: {run_id}") from exc
            if not isinstance(existing, dict):
                raise ArtifactError(f"corrupt running artifact: {run_id}")
            self._check_binding(run_id, existing)
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
            "plan": strip_approval(plan),
        }
        self._io.write(run_dir / "winner.patch", patch)
        self._io.write(
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

    @_artifact_access
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
        run_dir = self._run_path(run_id)
        self._io.mkdir(run_dir, exist_ok=False)
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
            "plan": strip_approval(plan),
        }
        self._write_manifest(run_dir, manifest)
        return RunArtifact(run_id, run_dir, manifest)

    @_artifact_access
    def load(self, run_id: str) -> RunArtifact:
        """Read one manifest without retaining per-run descriptors on return."""
        return self._load_pinned(run_id)

    def _load_pinned(self, run_id: str) -> RunArtifact:
        """Internal read: keep the directory pinned through the caller's mutation."""
        path = self._run_path(run_id)
        try:
            manifest = json.loads(self._io.read(path / "manifest.json"))
        except (OSError, ValueError, RecursionError) as exc:
            # RecursionError: a pathologically-nested manifest is corrupt, not
            # a crash (the _claude_events / parse_board_update lesson).
            raise ArtifactError(f"unknown or corrupt run: {run_id}") from exc
        if not isinstance(manifest, dict):
            # Valid JSON but not an object (a list/scalar) would crash every
            # `manifest.get` downstream — it is a corrupt run, same as bad JSON.
            raise ArtifactError(f"unknown or corrupt run: {run_id}")
        self._check_binding(run_id, manifest)
        return RunArtifact(run_id, path, manifest)

    @_artifact_access
    def list(self) -> list[RunArtifact]:
        artifacts: list[RunArtifact] = []
        for name in self._io.names(self.root):
            path = self.root / name
            try:
                if self._io.is_directory(path):
                    artifacts.append(self.load(path.name))
            except (ArtifactError, OSError):
                continue
        return sorted(
            artifacts,
            key=lambda artifact: str(artifact.manifest.get("created_at", "")),
            reverse=True,
        )

    @_artifact_access
    def prune(
        self, keep: int, *, dry_run: bool = False, protect: str | None = None,
        blocking: bool = True,
    ) -> list[str]:
        """Remove old terminal runs; pending/recovery state is never pruned.

        ``protect`` (the Supervisor's just-saved run) is ALWAYS retained and
        counts toward ``keep`` — the ``report._sweep_retention`` rule. Retention
        ranks by ``created_at``, which is stamped when a run *begins*, so a
        long-running run that finishes after ``keep`` newer runs started would
        otherwise fall outside the protected window and delete its own artifact
        moments after saving it, leaving ``result.artifact_path`` dangling.

        ``blocking=False`` raises :class:`ApplyLockBusy` instead of waiting, for
        callers on a latency-sensitive path: ``_apply_locked`` holds this same
        lock across a full verification (a clone plus every gate command), so a
        concurrent ``apply --verify`` would otherwise stall the caller for
        minutes. Retention is housekeeping — skipping a busy sweep costs
        nothing, since the next run repeats it.
        """
        with self.apply_lock(blocking=blocking):
            removable_states = {
                "applied", "no_changes", "failed", "discarded", "superseded"
            }
            artifacts = self.list()
            if protect is None:
                protected = set(artifact.run_id for artifact in artifacts[:keep])
            else:
                others = [a for a in artifacts if a.run_id != protect]
                protected = {a.run_id for a in others[: max(keep - 1, 0)]}
                protected.add(protect)
            removed: list[str] = []
            for artifact in artifacts:
                if artifact.run_id in protected:
                    continue
                if artifact.manifest.get("state") not in removable_states:
                    continue
                removed.append(artifact.run_id)
                if not dry_run:
                    self._io.rmtree(artifact.path)
            return removed

    @_artifact_access
    def patch(self, run_id: str) -> bytes:
        artifact = self._load_pinned(run_id)
        data = self._io.read(artifact.path / "winner.patch")
        if _digest(data) != artifact.manifest.get("patch_sha256"):
            raise ArtifactError(f"patch digest mismatch for run {run_id}")
        return data

    @_artifact_access
    def validate_applicable(self, run_id: str) -> RunArtifact:
        """Structural preflight for announcing a detached apply.

        Verifies the manifest carries the fields apply needs and that the
        patch bytes match their recorded digest — so "detached job launched"
        can never precede a guaranteed worker crash on a hollow/hand-edited
        manifest. The worker still fully revalidates under the apply lock.
        """
        artifact = self._load_pinned(run_id)
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

    @_artifact_access
    def approve_workflow_run(
        self, run_id: str, repo_root: Path, *, workflow_id: str
    ) -> None:
        """Record that ``workflow_id`` committed the completion of THIS run.

        Written AFTER the workflow's lease-fenced completion write, so the
        artifact carries its own authorization: `workflow prune` is entitled to
        delete a terminal workflow row, and once it has, the row can no longer
        answer whether the run it produced was ever approved. Idempotent, so the
        healing path can stamp a run whose producer died between the two writes.

        ``workflow_id`` is the caller's statement of WHICH workflow it verified,
        and it must match what this artifact claims. Approval is otherwise only
        bound to "some completed workflow exists", which any artifact can name:
        the manifest is data, and a copied or hand-written one naming a
        genuinely completed workflow would launder itself into an applyable run.
        An artifact that names no workflow, or names it emptily, is refused for
        the same reason.

        Under the apply lock and through a compare-and-swap, like every other
        manifest transition: a plain read-modify-write here would let a stamp
        that loaded a `pending` manifest overwrite a concurrent `discard` and
        resurrect the run as pending AND approved.
        """
        with self.apply_lock(repo_root):
            artifact = self._load_pinned(run_id)
            current = artifact.manifest
            claimed = workflow_origin_id(current)
            if not claimed:
                raise ArtifactError(
                    f"run {run_id} does not identify the workflow that produced it"
                )
            if claimed != workflow_id:
                raise ArtifactError(
                    f"run {run_id} came from workflow {claimed}, not {workflow_id}"
                )
            if workflow_approved(current):
                return
            replacement = json.loads(json.dumps(current))
            replacement["plan"] = {
                **(replacement.get("plan") or {}),
                "approved": {
                    "workflow_id": workflow_id,
                    "run_id": run_id,
                    # The patch AS RECORDED for this run: `patch()` already
                    # refuses bytes that do not match it, so an approval naming
                    # it cannot survive the patch being swapped underneath.
                    "patch_sha256": current.get("patch_sha256"),
                    "approved_at": _now(),
                },
            }
            replacement["updated_at"] = _now()
            self._compare_and_swap_manifest(artifact.path, current, replacement)

    @_artifact_access
    def apply(
        self,
        run_id: str,
        repo_root: Path,
        source_fingerprint: str,
        *,
        verify_commands: list[str] | None = None,
        verify_timeout: int = 300,
        allow_room_bound: bool = False,
        verify_workspace_root: Path | None = None,
    ) -> Path:
        with self.apply_lock(repo_root):
            return self._apply_locked(
                run_id,
                repo_root,
                source_fingerprint,
                verify_commands=verify_commands,
                verify_timeout=verify_timeout,
                allow_room_bound=allow_room_bound,
                verify_workspace_root=verify_workspace_root,
            )

    @_artifact_access
    def _apply_locked(
        self,
        run_id: str,
        repo_root: Path,
        source_fingerprint: str,
        *,
        verify_commands: list[str] | None = None,
        verify_timeout: int = 300,
        allow_room_bound: bool = False,
        verify_workspace_root: Path | None = None,
    ) -> Path:
        artifact = self._load_pinned(run_id)
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
        if workflow_origin_id(manifest) is not None and not workflow_approved(manifest):
            # Enforced HERE, beside the Room guard, rather than in the CLI: the
            # locked apply path is the only place every caller passes through,
            # so a guard above it is advice that a direct caller silently skips.
            # A workflow publishes this artifact before committing its
            # completion, so an unapproved one is a run whose workflow may have
            # been cancelled in that window — or one whose approval was never
            # written. `syncmill apply` heals the second case; nothing may
            # apply the first.
            raise ArtifactError(
                f"run {run_id} came from a workflow that has not recorded its "
                "completion; its result is not approved for apply"
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
        patch_data = self.patch(run_id)
        check_apply = _run_patch_git(
            patch_data,
            ["git", "apply", "--check", "--binary", "-"],
            cwd=str(repo_root), env=git_env, capture_output=True, text=True,
            timeout=_APPLY_GIT_TIMEOUT_SECONDS,
        )
        if check_apply.returncode != 0:
            # A timed-out --check is read-only — the timeout note in stderr
            # names it; degrading into the ordinary check-failure idiom is safe.
            raise ArtifactError(check_apply.stderr.strip() or "git apply check failed")

        if verify_commands:
            self._verify_in_isolation(
                run_id=run_id,
                repo_root=repo_root,
                patch=patch_data,
                commands=verify_commands,
                timeout=verify_timeout,
                workspace_root=verify_workspace_root,
            )
            if repository_fingerprint(repo_root) != source_fingerprint:
                raise ArtifactError(
                    "working tree changed during isolated verification; retry"
                )
            check_apply = _run_patch_git(
                patch_data,
                ["git", "apply", "--check", "--binary", "-"],
                cwd=str(repo_root), env=git_env, capture_output=True, text=True,
                timeout=_APPLY_GIT_TIMEOUT_SECONDS,
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
            self._io.directory(artifact.path)
        except BaseException:
            restore_pending_before_source_mutation()
            raise
        if final_fingerprint != source_fingerprint:
            restore_pending_before_source_mutation()
            raise ArtifactError("working tree changed before patch application; retry")

        try:
            apply_patch = _run_patch_git(
                patch_data,
                ["git", "apply", "--binary", "-"],
                cwd=str(repo_root), env=git_env, capture_output=True, text=True,
                timeout=_APPLY_GIT_TIMEOUT_SECONDS,
            )
        except OSError as spawn_exc:
            restore_pending_before_source_mutation()
            raise ArtifactError(f"could not start git apply: {spawn_exc}") from spawn_exc
        if _git_timed_out(apply_patch):
            # A timed-out (group-killed) apply may have half-mutated the tree —
            # it must NEVER take the clean "patch did not apply → restore
            # pending" branch below over a mutated source. Attempt a bounded
            # rollback, then let the fingerprint decide: verified-unchanged
            # restores pending like a clean failure; anything else is the
            # recovery-required path (the manifest stays `applying` for the
            # reconciler / `syncmill recover`).
            try:
                undo = _run_patch_git(
                    patch_data,
                    ["git", "apply", "--reverse", "--check", "--binary",
                     "-"],
                    cwd=str(repo_root), env=git_env, capture_output=True,
                    text=True, timeout=_APPLY_GIT_TIMEOUT_SECONDS,
                )
                if undo.returncode == 0:
                    _run_patch_git(
                        patch_data,
                        ["git", "apply", "--reverse", "--binary", "-"],
                        cwd=str(repo_root), env=git_env, capture_output=True,
                        text=True, timeout=_APPLY_GIT_TIMEOUT_SECONDS,
                    )
            except OSError:
                pass  # the fingerprint below is the verdict, not the rollback
            try:
                source_unchanged = (
                    repository_fingerprint(repo_root) == source_fingerprint
                )
            except Exception:
                source_unchanged = False
            if not source_unchanged:
                raise ArtifactRecoveryRequired(
                    "git apply timed out and the source could not be verified "
                    "restored; manual recovery is required",
                    source_changed=True,
                )
            restore_pending_before_source_mutation()
            raise ArtifactError("git apply timed out; source verified unchanged")
        if apply_patch.returncode != 0:
            restore_pending_before_source_mutation()
            raise ArtifactError(apply_patch.stderr.strip() or "git apply failed")

        receipt_dir = artifact.path / "applications"
        receipt: Path | None = None
        try:
            self._io.mkdir(receipt_dir)
            receipt = receipt_dir / (
                f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
            )
            self._io.write(
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
                    self._io.unlink(receipt)
                except OSError as unlink_exc:
                    raise ArtifactRecoveryRequired(
                        "apply bookkeeping failed and the receipt could not be "
                        "invalidated before rollback; manual recovery is required",
                        source_changed=True,
                    ) from unlink_exc
            rollback = _run_patch_git(
                patch_data,
                ["git", "apply", "--reverse", "--check", "--binary", "-"],
                cwd=str(repo_root), env=git_env, capture_output=True, text=True,
                timeout=_APPLY_GIT_TIMEOUT_SECONDS,
            )
            if rollback.returncode == 0:
                rollback = _run_patch_git(
                    patch_data,
                    ["git", "apply", "--reverse", "--binary", "-"],
                    cwd=str(repo_root), env=git_env, capture_output=True, text=True,
                    timeout=_APPLY_GIT_TIMEOUT_SECONDS,
                )
            rolled_back = (
                rollback.returncode == 0
                and repository_fingerprint(repo_root) == source_fingerprint
            )
            if not rolled_back:
                # A timed-out rollback already lands here via the fingerprint
                # check; name the timeout so the freeze reason is honest.
                detail = (
                    " (rollback timed out)" if _git_timed_out(rollback) else ""
                )
                raise ArtifactRecoveryRequired(
                    "source patch was applied but durable bookkeeping failed"
                    f"{detail}; manual recovery is required",
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
        workspace_root: Path | None = None,
    ) -> None:
        """Verify the candidate in a disposable clone; never run gates in source.

        With ``workspace_root`` (the configured ``workspace.root``, threaded by
        the CLI/room callers) the clone lands under the directory
        ``sweep_orphaned_workspaces`` actually inspects, so a SIGKILL
        mid-verification — which skips every finally — leaves a sweepable,
        eventually-reclaimed workspace instead of an unreclaimable clone in a
        one-off tempdir. Without it (a direct library caller) the tempdir
        fallback keeps the old behavior, SIGKILL residue included.
        """
        with contextlib.ExitStack() as stack:
            if workspace_root is None:
                base = Path(
                    stack.enter_context(
                        tempfile.TemporaryDirectory(prefix="syncmill-apply-verify-")
                    )
                )
            else:
                base = workspace_root
            # A unique run-dir name: a retried verify of the same run must not
            # collide with a SIGKILL'd predecessor's leftover (run_root-exists
            # is a WorkspaceError).
            snapshot = capture_repository_snapshot(
                repo_root,
                f"verify-{run_id}-{uuid4().hex[:8]}",
                base,
                sweepable=True,
            )
            pool = WorktreePool(repo_root, f"verify-{run_id}", snapshot=snapshot)
            try:
                worktree = pool.acquire("apply-verify")
                restored = _run_git(
                    ["git", "apply", "--binary", "-"],
                    cwd=str(worktree.path), env=isolated_git_env(),
                    input=patch, capture_output=True,
                    timeout=_APPLY_GIT_TIMEOUT_SECONDS,
                )
                if restored.returncode != 0:
                    raise ArtifactError(
                        restored.stderr.decode(errors="replace").strip()
                        or "could not restore patch for verification"
                    )
                for command in commands:
                    report = CommandGate([command], timeout).evaluate(worktree)
                    if not report.passed:
                        raise ArtifactError(
                            f"isolated verification failed ({command}): {report.checks[0].output}"
                        )
            finally:
                pool.cleanup_all()

    @_artifact_access
    def discard(self, run_id: str) -> None:
        with self.apply_lock():
            artifact = self._load_pinned(run_id)
            current = artifact.manifest
            if current.get("state") != "pending":
                raise ArtifactError("only a pending run can be discarded")
            replacement = {
                **current,
                "state": "discarded",
                "updated_at": _now(),
            }
            self._compare_and_swap_manifest(artifact.path, current, replacement)

    @_artifact_access
    def supersede(self, run_id: str, child_run_id: str) -> None:
        with self.apply_lock():
            artifact = self._load_pinned(run_id)
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

    @_artifact_access
    def close_conversation(self, run_id: str) -> None:
        with self.apply_lock():
            artifact = self._load_pinned(run_id)
            current = artifact.manifest
            replacement = {
                **current,
                "conversation_closed": True,
                "updated_at": _now(),
            }
            self._compare_and_swap_manifest(artifact.path, current, replacement)

    @_artifact_access
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
            artifact = self._load_pinned(run_id)
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

    @_artifact_access
    def _bound_receipt(self, run_dir: Path, manifest: dict[str, Any]) -> Path | None:
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
        try:
            names = self._io.names(receipt_dir)
        except OSError:
            return None
        for name in sorted(names):
            if not name.endswith(".json"):
                continue
            receipt = receipt_dir / name
            try:
                raw = self._io.read(receipt, _MAX_RECEIPT_BYTES + 1)
                if len(raw) > _MAX_RECEIPT_BYTES:
                    # Our receipts are a few hundred bytes; an oversized file is
                    # a bad artifact, not evidence — skip it without parsing.
                    continue
                # ``json.loads`` raises RecursionError (a RuntimeError, not a
                # ValueError) on pathologically nested input; catch it so one
                # malformed file can never abort the whole recovery sweep.
                payload = json.loads(raw)
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
    def _reverse_check(repo_root: Path, patch_data: bytes) -> str:
        """Advisory probe: is the run's patch reverse-applicable right now?

        Never drives a verdict — a reverse-applicable patch only proves the
        content is present now, not that this attempt performed the mutation.
        The result is embedded in the freeze reason for an operator to read.
        """
        try:
            probe = _run_patch_git(
                patch_data,
                ["git", "apply", "--reverse", "--check", "--binary", "-"],
                cwd=str(repo_root),
                env=isolated_git_env(),
                capture_output=True,
                text=True,
                timeout=_APPLY_GIT_TIMEOUT_SECONDS,
            )
        except OSError as exc:
            return f"reverse-check unavailable ({exc})"
        if _git_timed_out(probe):
            # "not reverse-applicable" would be a false verdict — the probe
            # never finished, and this string is operator-facing.
            return "reverse-check unavailable (timed out)"
        return (
            "patch is reverse-applicable"
            if probe.returncode == 0
            else "patch is not reverse-applicable"
        )

    @_artifact_access
    def _run_directories(self) -> list[Path]:
        return [self.root / name for name in sorted(self._io.names(self.root))
                if self._io.is_directory(self.root / name)]

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
            for path in self._run_directories():
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

    @_artifact_access
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
                    current = self._load_pinned(run_id)
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

    @_artifact_access
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
        try:
            reverse = self._reverse_check(repo_root, self.patch(run_id))
        except (ArtifactError, OSError) as exc:
            reverse = f"winner.patch unavailable: {exc}"
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

    @_artifact_access
    def _write_manifest(self, run_dir: Path, manifest: dict[str, Any]) -> None:
        if run_dir != self._run_path(run_dir.name):
            raise ArtifactError("manifest directory is outside this store")
        self._check_binding(run_dir.name, manifest)
        self._io.write(
            run_dir / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        )
