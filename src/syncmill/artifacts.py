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
from syncmill.worktree import (
    WorktreePool,
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


def _lock_file_descriptor(fd: int) -> None:
    """Acquire an inherited-safe advisory lock, waiting until it is available."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
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
def _cross_process_lock(lock_path: Path) -> Iterator[None]:
    """Hold one re-entrant in-process and advisory cross-process file lock."""
    with _APPLY_LOCKS_GUARD:
        process_lock = _APPLY_PROCESS_LOCKS.setdefault(
            lock_path, threading.RLock()
        )

    with process_lock:
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
            try:
                os.fchmod(fd, 0o600)
            except OSError:  # pragma: no cover - permission model is platform-specific
                pass
            try:
                _lock_file_descriptor(fd)
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


@dataclass(frozen=True)
class RunArtifact:
    run_id: str
    path: Path
    manifest: dict[str, Any]


class ArtifactStore:
    def __init__(self, root: Path, project_id: str):
        self.root = root.expanduser().resolve() / project_id / "runs"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    @staticmethod
    def _repository_apply_lock_path(repo_root: Path) -> Path:
        root = repo_root.expanduser().resolve()
        try:
            common_dir = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=str(root),
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
        common_path = common_path.resolve()
        if not common_path.is_dir():
            raise ArtifactError(
                f"repository common directory does not exist: {common_path}"
            )
        return common_path / ".syncmill-apply.lock"

    @contextmanager
    def apply_lock(self, repo_root: Path | None = None) -> Iterator[None]:
        """Serialize project source mutation and apply-state reconciliation.

        The artifact-store lock protects manifest state.  When ``repo_root`` is
        supplied, a second lock in Git's canonical common directory serializes
        source mutation even when another ArtifactStore root/project id points at
        the same repository.  Locks are always acquired store-first, repo-second,
        and are re-entrant within one thread.
        """
        lock_paths = [self.root / ".apply.lock"]
        if repo_root is not None:
            repository_lock = self._repository_apply_lock_path(repo_root)
            if repository_lock not in lock_paths:
                lock_paths.append(repository_lock)
        with ExitStack() as stack:
            for lock_path in lock_paths:
                stack.enter_context(_cross_process_lock(lock_path))
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
        except (OSError, ValueError) as exc:
            raise ArtifactError(f"unknown or corrupt run: {run_id}") from exc
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

    def apply(
        self,
        run_id: str,
        repo_root: Path,
        source_fingerprint: str,
        *,
        verify_commands: list[str] | None = None,
        verify_timeout: int = 300,
    ) -> Path:
        with self.apply_lock(repo_root):
            return self._apply_locked(
                run_id,
                repo_root,
                source_fingerprint,
                verify_commands=verify_commands,
                verify_timeout=verify_timeout,
            )

    def _apply_locked(
        self,
        run_id: str,
        repo_root: Path,
        source_fingerprint: str,
        *,
        verify_commands: list[str] | None = None,
        verify_timeout: int = 300,
    ) -> Path:
        artifact = self.load(run_id)
        manifest = artifact.manifest
        if manifest.get("state") != "pending":
            raise ArtifactError(
                f"run {run_id} is {manifest.get('state')!r}, not pending"
            )
        if Path(manifest["repo_root"]).resolve() != repo_root.resolve():
            raise ArtifactError("run belongs to a different repository")
        if manifest.get("source_fingerprint") != source_fingerprint:
            raise ArtifactError(
                "working tree changed since the run; review the diff and rerun"
            )
        patch_path = artifact.path / "winner.patch"
        patch_data = self.patch(run_id)
        check_apply = subprocess.run(
            ["git", "apply", "--check", "--binary", str(patch_path)],
            cwd=str(repo_root), capture_output=True, text=True,
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
                cwd=str(repo_root), capture_output=True, text=True,
            )
            if check_apply.returncode != 0:
                raise ArtifactError(
                    check_apply.stderr.strip() or "git apply recheck failed"
                )

        if repository_fingerprint(repo_root) != source_fingerprint:
            raise ArtifactError("working tree changed during apply preparation; retry")

        original_manifest = dict(manifest)
        applying_manifest = {
            **manifest,
            "state": "applying",
            "apply_id": str(uuid4()),
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
                cwd=str(repo_root), capture_output=True, text=True,
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
            rollback = subprocess.run(
                ["git", "apply", "--reverse", "--check", "--binary", str(patch_path)],
                cwd=str(repo_root), capture_output=True, text=True,
            )
            if rollback.returncode == 0:
                rollback = subprocess.run(
                    ["git", "apply", "--reverse", "--binary", str(patch_path)],
                    cwd=str(repo_root), capture_output=True, text=True,
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
                if receipt is not None:
                    receipt.unlink(missing_ok=True)
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
            snapshot = capture_repository_snapshot(
                repo_root,
                f"verify-{run_id}",
                Path(temp),
            )
            pool = WorktreePool(repo_root, f"verify-{run_id}", snapshot=snapshot)
            try:
                worktree = pool.acquire("apply-verify")
                restored = subprocess.run(
                    ["git", "apply", "--binary", "-"],
                    cwd=str(worktree.path), input=patch, capture_output=True,
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
    ) -> bool:
        """Freeze an unfinished run without overwriting a terminal apply state."""
        with self.apply_lock():
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
    def _write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
        _atomic_write(
            run_dir / "manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(),
        )
