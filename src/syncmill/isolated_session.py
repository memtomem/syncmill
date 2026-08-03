"""Project-scoped interactive sessions backed by persistent isolated worktrees."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import UUID, uuid4

from syncmill.artifacts import ArtifactStore
from syncmill.config import SyncmillConfig
from syncmill.git_env import (
    GIT_CONFIG_INJECTION_ENV,
    GIT_PATHSPEC_SEMANTICS_ENV,
    GIT_REPOSITORY_ROUTING_ENV,
    isolated_git_env,
)
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.project import project_id
from syncmill.rooms import _atomic_write, _now
from syncmill.session import (
    _attach_and_report,
    _create_session,
    _exact,
    _tmux,
    interactive_agents,
    session_exists,
)
from syncmill.worktree import (
    Worktree,
    WorktreePool,
    capture_repository_snapshot,
    cleanup_worktree,
    repository_fingerprint,
)

try:  # Isolated interactive sessions are POSIX/tmux today; keep import graceful.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX session commands already fail
    fcntl = None  # type: ignore[assignment]


_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.Lock] = {}
# Refcounts for _PROCESS_LOCKS so entries are evicted once the last holder
# releases — otherwise a long-lived TUI accumulates one Lock per session id
# ever touched. An entry is only popped at refcount zero, never while a
# waiter still holds a reference to the Lock.
_PROCESS_LOCK_REFS: dict[Path, int] = {}
# Membership set (the snapshot writer filters by name), so the numbered
# GIT_CONFIG_KEY_<n>/VALUE_<n> pairs can't be listed here — dropping
# GIT_CONFIG_COUNT is what makes them inert, since git reads the count to learn
# how many pairs to apply and ignores them entirely without it.
_ISOLATED_LAUNCH_ROUTING_ENV = (
    GIT_REPOSITORY_ROUTING_ENV
    | GIT_CONFIG_INJECTION_ENV
    | GIT_PATHSPEC_SEMANTICS_ENV
    | {
        "PWD",
        "OLDPWD",
        "CDPATH",
    }
)


class IsolatedSessionError(RuntimeError):
    """A project-scoped isolated session operation failed."""


def _strict_checkpoint_patch(workdir: Path, base_commit: str) -> tuple[list[str], str]:
    """Capture a complete binary patch without touching the session's real index."""
    fd, raw_index = tempfile.mkstemp(prefix="syncmill-session-index-")
    os.close(fd)
    index = Path(raw_index)
    index.unlink()
    env = isolated_git_env({"GIT_INDEX_FILE": str(index)})
    try:
        for argv in (
            ["git", "read-tree", base_commit],
            ["git", "add", "-A"],
        ):
            proc = subprocess.run(argv, cwd=str(workdir), env=env, capture_output=True)
            if proc.returncode != 0:
                raise IsolatedSessionError(
                    proc.stderr.decode(errors="replace").strip()
                    or f"checkpoint patch capture failed: {' '.join(argv)}"
                )
        names = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "-z", base_commit],
            cwd=str(workdir), env=env, capture_output=True,
        )
        patch = subprocess.run(
            ["git", "diff", "--cached", "--binary", base_commit],
            cwd=str(workdir), env=env, capture_output=True,
        )
        if names.returncode != 0 or patch.returncode != 0:
            error = names.stderr or patch.stderr
            raise IsolatedSessionError(
                error.decode(errors="replace").strip() or "checkpoint patch capture failed"
            )
        files = [
            os.fsdecode(value)
            for value in names.stdout.split(b"\0")
            if value
        ]
        patch_text = patch.stdout.decode("utf-8", errors="surrogateescape")
        if files and not patch_text:
            raise IsolatedSessionError(
                "checkpoint found changed files but could not produce an applyable patch"
            )
        return sorted(files), patch_text
    finally:
        index.unlink(missing_ok=True)


class IsolatedSessionStore:
    def __init__(self, config: SyncmillConfig, repo_root: Path):
        self.config = config
        self.repo_root = repo_root.resolve()
        self.project_id = project_id(self.repo_root, config.project.id)
        self.root = (
            Path(config.runs.root).expanduser() / self.project_id / "sessions"
        ).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    @staticmethod
    def _canonical_session_id(session_id: str) -> str:
        try:
            canonical = str(UUID(session_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise IsolatedSessionError(f"invalid session id: {session_id}") from exc
        if canonical != session_id:
            raise IsolatedSessionError(f"invalid session id: {session_id}")
        return canonical

    def _path(self, session_id: str) -> Path:
        canonical = self._canonical_session_id(session_id)
        return self.root / f"{canonical}.json"

    @contextmanager
    def _locked(self, session_id: str) -> Iterator[None]:
        canonical = self._canonical_session_id(session_id)
        lock_path = self.root / f"{canonical}.lock"
        with _LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(lock_path, threading.Lock())
            _PROCESS_LOCK_REFS[lock_path] = _PROCESS_LOCK_REFS.get(lock_path, 0) + 1
        try:
            with process_lock:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
                try:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_EX)
                    yield
                finally:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
        finally:
            # The inner `with` released process_lock before this runs, so a
            # zero refcount means no holder and no waiter: safe to evict.
            with _LOCKS_GUARD:
                remaining = _PROCESS_LOCK_REFS[lock_path] - 1
                if remaining:
                    _PROCESS_LOCK_REFS[lock_path] = remaining
                else:
                    del _PROCESS_LOCK_REFS[lock_path]
                    del _PROCESS_LOCKS[lock_path]

    def _write(self, value: dict, *, expected_revision: int | None = None) -> None:
        if expected_revision is not None:
            try:
                current = json.loads(
                    self._path(value["session_id"]).read_text(encoding="utf-8")
                )
            except (OSError, ValueError, TypeError) as exc:
                raise IsolatedSessionError("session changed or disappeared") from exc
            if int(current.get("revision", 0)) != expected_revision:
                raise IsolatedSessionError("session changed concurrently; retry")
        value["updated_at"] = _now()
        _atomic_write(
            self._path(value["session_id"]),
            (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(),
        )

    def load(self, session_id: str) -> dict:
        try:
            value = json.loads(self._path(session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise IsolatedSessionError(f"unknown or corrupt session: {session_id}") from exc
        if Path(value.get("repo_root", "")).resolve() != self.repo_root:
            raise IsolatedSessionError("session belongs to a different repository")
        self._validate_paths(value)
        return value

    def _validate_paths(self, value: dict) -> None:
        """Bind cleanup and attachment to paths derived from this session id."""
        try:
            sid = str(UUID(value["session_id"]))
            if sid != value["session_id"]:
                raise ValueError("non-canonical session id")
            expected_run = (
                Path(self.config.workspace.root).expanduser().resolve()
                / f"session-{sid}"
            )
            run_root = Path(value["run_root"]).resolve()
            control = Path(value["control_repo"]).resolve()
            worktree = Path(value["worktree_path"]).resolve()
            worktree.relative_to(run_root / "worktrees")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise IsolatedSessionError("session metadata contains unsafe paths") from exc
        expected_tmux = (
            f"syncmill-p-{self.project_id[:8]}-{value.get('agent_id')}-{sid[:8]}"
        )
        if (
            run_root != expected_run
            or control != run_root / "control"
            or value.get("tmux_name") != expected_tmux
        ):
            raise IsolatedSessionError("session metadata contains unsafe paths")

    def list(self) -> list[dict]:
        values: list[dict] = []
        current_fingerprint: str | None = None
        fingerprint_loaded = False
        fingerprint_failed = False
        for path in self.root.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if Path(value.get("repo_root", "")).resolve() != self.repo_root:
                    continue
                self._validate_paths(value)
                if path.resolve() != self._path(value["session_id"]).resolve():
                    continue
            except (OSError, ValueError, TypeError, KeyError, IsolatedSessionError):
                continue
            name = value.get("tmux_name", "")
            try:
                value["live"] = bool(name and session_exists(name))
            except FileNotFoundError:
                # Persisted session metadata remains useful after tmux is
                # uninstalled or disappears from PATH.  Listing is read-only,
                # so report that process as stopped instead of making the CLI
                # and TUI unavailable.  Other launch failures still surface.
                value["live"] = False
            value["source_stale"] = False
            if value.get("state") == "open":
                if not fingerprint_loaded:
                    try:
                        current_fingerprint = repository_fingerprint(self.repo_root)
                    except Exception:
                        fingerprint_failed = True
                    fingerprint_loaded = True
                value["source_stale"] = fingerprint_failed or (
                    current_fingerprint != value.get("source_fingerprint")
                )
            values.append(value)
        return sorted(values, key=lambda value: value.get("created_at", ""), reverse=True)

    def create(self, agent_id: str) -> dict:
        if agent_id not in interactive_agents():
            raise IsolatedSessionError(f"unknown interactive agent: {agent_id}")
        session_id = str(uuid4())
        snapshot = capture_repository_snapshot(
            self.repo_root,
            f"session-{session_id}",
            Path(self.config.workspace.root),
        )
        pool = WorktreePool(
            self.repo_root, f"session-{session_id}", snapshot=snapshot
        )
        tmux_name: str | None = None
        launched = False
        try:
            worktree = pool.acquire(agent_id)
            tmux_name = (
                f"syncmill-p-{self.project_id[:8]}-{agent_id}-{session_id[:8]}"
            )
            value = {
                "schema_version": 1,
                "session_id": session_id,
                "project_id": self.project_id,
                "repo_root": str(self.repo_root),
                "agent_id": agent_id,
                "state": "creating",
                "tmux_name": tmux_name,
                "run_root": str(snapshot.run_root),
                "control_repo": str(snapshot.control_repo),
                "worktree_path": str(worktree.path),
                "worktree_branch": worktree.branch,
                "base_commit": worktree.base_commit,
                "source_fingerprint": snapshot.source_fingerprint,
                "source_head": snapshot.source_head,
                "checkpoints": [],
                "revision": 0,
                "created_at": _now(),
            }
            self._validate_paths(value)
            with self._locked(session_id):
                # A crash before launch remains discoverable and cleanable, but
                # close/checkpoint cannot cross this lifecycle lock while tmux
                # is starting. Only the post-launch CAS publishes ``open``.
                self._write(value)
                _create_session(
                    tmux_name,
                    agent_id,
                    str(worktree.path),
                    _ISOLATED_LAUNCH_ROUTING_ENV,
                )
                launched = True
                value["state"] = "open"
                value["revision"] = 1
                self._write(value, expected_revision=0)
            return value
        except BaseException:
            if launched and tmux_name is not None:
                try:
                    _tmux("kill-session", "-t", _exact(tmux_name))
                except BaseException:
                    pass
            try:
                self._path(session_id).unlink()
            except FileNotFoundError:
                pass
            shutil.rmtree(snapshot.run_root, ignore_errors=True)
            raise

    def attach(self, session_id: str) -> int:
        value = self.load(session_id)
        if value["state"] != "open":
            raise IsolatedSessionError(f"session is {value['state']}, not open")
        name = value["tmux_name"]
        if not session_exists(name):
            raise IsolatedSessionError("session process is no longer running")
        # ``display-message -t =session`` is parsed as a target-pane and can
        # return an empty path even though the exact session exists. ``list-panes``
        # accepts an exact target-session and gives an unambiguous cwd record.
        cwd = _tmux(
            "list-panes", "-t", _exact(name), "-F", "#{pane_current_path}"
        )
        if cwd.returncode != 0:
            raise IsolatedSessionError(cwd.stderr.strip() or "could not inspect session cwd")
        panes = [line for line in cwd.stdout.splitlines() if line]
        if len(panes) != 1 or Path(panes[0]).resolve() != Path(
            value["worktree_path"]
        ).resolve():
            raise IsolatedSessionError("session cwd does not match its isolated workspace")
        return _attach_and_report(name, value["agent_id"])

    def checkpoint(self, session_id: str) -> str:
        with self._locked(session_id):
            value = self.load(session_id)
            if value["state"] != "open":
                raise IsolatedSessionError(f"session is {value['state']}, not open")
            workdir = Path(value["worktree_path"])
            if not workdir.is_dir():
                raise IsolatedSessionError("isolated worktree is missing")
            base = value["base_commit"]
            files, patch = _strict_checkpoint_patch(workdir, base)
            task = Task(prompt=f"Interactive session checkpoint {session_id}")
            result = TaskResult(
                task_id=task.id,
                agent_id=value["agent_id"],
                status=TaskStatus.COMPLETED,
                output=f"Checkpoint from isolated session {session_id}",
                exit_code=0,
                duration_seconds=0.0,
                files_changed=files,
                patch=patch,
            )
            run_id = str(uuid4())
            artifacts = ArtifactStore(Path(self.config.runs.root), self.project_id)
            plan = {"origin": "isolated-session", "session_id": session_id}
            artifacts.begin(
                run_id=run_id,
                task=task,
                repo_root=self.repo_root,
                snapshot_commit=base,
                source_fingerprint=value["source_fingerprint"],
                strategy="interactive",
                agents=[value["agent_id"]],
                plan=plan,
            )
            artifacts.save(
                run_id=run_id,
                task=task,
                result=result,
                repo_root=self.repo_root,
                snapshot_commit=base,
                source_fingerprint=value["source_fingerprint"],
                strategy="interactive",
                plan=plan,
            )
            revision = int(value.get("revision", 0))
            value["checkpoints"].append(run_id)
            value["revision"] = revision + 1
            self._write(value, expected_revision=revision)
            return run_id

    def close_session(self, session_id: str) -> dict:
        with self._locked(session_id):
            value = self.load(session_id)
            if value["state"] == "closed":
                return value
            revision = int(value.get("revision", 0))
            entry_state = value["state"]
            if entry_state == "open":
                value["state"] = "closing"
                value["revision"] = revision + 1
                self._write(value, expected_revision=revision)
                revision += 1
            name = value["tmux_name"]
            if session_exists(name):
                killed = _tmux("kill-session", "-t", _exact(name))
                if killed.returncode != 0:
                    # Restore the state we found, not "open": a session in
                    # recovery_required (or creating, after a crash mid-launch)
                    # would otherwise be published as healthy, letting attach and
                    # list treat it as usable and destroying the only signal that
                    # its worktree was never cleaned up.
                    value["state"] = entry_state
                    value["revision"] = revision + 1
                    self._write(value, expected_revision=revision)
                    raise IsolatedSessionError(
                        killed.stderr.strip() or "could not stop tmux session"
                    )
            worktree = Worktree(
                path=Path(value["worktree_path"]),
                branch=value.get("worktree_branch"),
                agent_id=value["agent_id"],
                is_git=True,
                base_commit=value.get("base_commit"),
            )
            cleaned = cleanup_worktree(Path(value["control_repo"]), worktree)
            if not cleaned and Path(value["worktree_path"]).exists():
                value["state"] = "recovery_required"
                value["revision"] = revision + 1
                self._write(value, expected_revision=revision)
                raise IsolatedSessionError("could not remove isolated worktree")
            run_root = Path(value["run_root"])
            shutil.rmtree(run_root, ignore_errors=True)
            try:
                run_root.lstat()
            except FileNotFoundError:
                run_root_remains = False
            except OSError:
                run_root_remains = True
            else:
                run_root_remains = True
            if run_root_remains:
                value["state"] = "recovery_required"
                value["revision"] = revision + 1
                self._write(value, expected_revision=revision)
                raise IsolatedSessionError("could not remove isolated session workspace")
            value["state"] = "closed"
            value["revision"] = revision + 1
            self._write(value, expected_revision=revision)
            return value
