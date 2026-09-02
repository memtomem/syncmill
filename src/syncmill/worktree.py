"""Workspace isolation from an immutable snapshot of the complete Git state."""

from __future__ import annotations

import contextlib
import logging
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from syncmill.git_env import isolated_git_env

# Private cross-import (the session.py precedent) — one implementation of the
# reaped/pid-recycling-safe group kill; agents.py imports nothing from here, so
# the graph stays acyclic.
from syncmill.agents import _close_process_streams, _kill_process_group

logger = logging.getLogger(__name__)

#: Written into a supervisor run's private workspace as soon as the directory
#: exists, holding the owning process's pid. Proof of ownership AND of liveness
#: for ``sweep_orphaned_workspaces``.
_RUN_SENTINEL = ".syncmill-run"

#: Written by ``retain()`` to exempt a recovery workspace from the sweep
#: permanently. A separate file rather than removing the run sentinel: adding a
#: file is the operation more likely to succeed, and the exemption has to be
#: recorded on disk for a LATER process to honor.
_RECOVERY_MARKER = ".syncmill-recovery"

#: Backstop for the one case pid liveness can't decide: the owner is gone but
#: its pid has since been reused by an unrelated process, which would otherwise
#: protect the leak forever. Deliberately long — the pid check already collects
#: the ordinary crash promptly, so this only bounds a rare case, and being wrong
#: here deletes a live run's workspace. Operators who need a different window
#: set ``workspace.stale_seconds`` (floor: an hour) — this stays the default
#: every direct caller gets.
_STALE_WORKSPACE_SECONDS = 7 * 24 * 3600

#: Hang bound for every git subprocess in this module — a wedged git (a stale
#: index.lock left by a crashed peer, a prompting clean/smudge/LFS filter, a
#: broken fsmonitor) must never hang a run, or the Supervisor's finally-time
#: cleanup, forever. Deliberately generous: this is hang PROTECTION, not a
#: latency bound — a snapshot of a large repository legitimately takes minutes.
_GIT_TIMEOUT_SECONDS = 600.0


def _run_git(cmd, **kwargs):
    """Bounded git runner that kills the WHOLE process tree on timeout.

    ``subprocess.run(timeout=...)`` terminates only the direct child, so a
    clean/smudge/LFS filter or fsmonitor daemon git spawned survived the
    timeout as an orphan (codex round-4 major, reproduced). Git therefore
    launches in its own process group (POSIX) and a timeout — or an interrupt
    unwinding this frame, which the detached group would otherwise never
    receive — SIGKILLs the group before the direct child.

    A timeout still degrades into each call site's EXISTING failure idiom
    instead of introducing a new exception type at 23 sites: non-``check``
    callers get a failed ``CompletedProcess`` (rc 124, the coreutils timeout
    convention) whose stderr names the timeout, and ``check=True`` callers get
    the ``CalledProcessError`` they already handle.
    """
    timeout = kwargs.pop("timeout", _GIT_TIMEOUT_SECONDS)
    check = kwargs.pop("check", False)
    input_data = kwargs.pop("input", None)  # run()-style; feeds communicate()
    if input_data is not None:
        kwargs["stdin"] = subprocess.PIPE
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    kwargs.setdefault("start_new_session", os.name == "posix")
    text = bool(kwargs.get("text"))
    # NOT a `with Popen(...)` block — Popen.__exit__ ends with an UNBOUNDED
    # wait(), so an unreapable child would hang this frame on the way out,
    # defeating the timeout contract (the _sync_run rule; codex round-5).
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        try:
            stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc, owned=True)  # we launched this group
            proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5.0)
            note = f"git command timed out after {timeout}s: {' '.join(cmd)}"
            empty = "" if text else b""
            note_b = note if text else note.encode()
            if check:
                raise subprocess.CalledProcessError(
                    124, cmd, output=empty, stderr=note_b
                ) from None
            return subprocess.CompletedProcess(cmd, 124, stdout=empty, stderr=note_b)
        except BaseException:
            # A Ctrl-C that unwinds this frame never reaches the detached
            # group — kill the tree before propagating (the _sync_run rule).
            with contextlib.suppress(Exception):
                _kill_process_group(proc, owned=True)
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5.0)
            raise
    finally:
        _close_process_streams(proc)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, cmd, output=stdout, stderr=stderr
        )
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


@dataclass
class Worktree:
    path: Path
    branch: str | None
    agent_id: str
    is_git: bool
    is_temp: bool = False
    base_commit: str | None = None


class WorkspaceError(RuntimeError):
    """A repository cannot be snapshotted safely."""


@dataclass
class RepositorySnapshot:
    source_root: Path
    run_root: Path
    control_repo: Path
    commit: str
    source_head: str
    source_fingerprint: str


def repository_fingerprint(repo_root: Path) -> str:
    """Digest HEAD plus tracked/untracked changes, excluding ignored files.

    The commit identity is part of the source contract even for a clean checkout:
    two different clean HEADs must never compare equal merely because both have an
    empty working-tree diff.
    """
    digest = hashlib.sha256()
    env = isolated_git_env()
    commands = (
        ["git", "rev-parse", "--verify", "HEAD"],
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        ["git", "diff", "--binary", "HEAD"],
    )
    for command in commands:
        proc = _run_git(
            command, cwd=str(repo_root), env=env, capture_output=True
        )
        if proc.returncode != 0:
            raise WorkspaceError(proc.stderr.decode(errors="replace").strip())
        digest.update(proc.stdout)
    untracked = _run_git(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=str(repo_root), env=env, capture_output=True,
    )
    if untracked.returncode != 0:
        raise WorkspaceError("could not enumerate untracked files")
    for raw in sorted(p for p in untracked.stdout.split(b"\0") if p):
        rel = os.fsdecode(raw)
        path = repo_root / rel
        digest.update(raw + b"\0")
        try:
            if path.is_symlink():
                digest.update(b"L" + os.readlink(path).encode(errors="surrogateescape"))
            else:
                digest.update(b"F" + path.read_bytes())
        except OSError as exc:
            raise WorkspaceError(f"could not read untracked file {rel}: {exc}") from exc
    return digest.hexdigest()


def _seed_snapshot_index(source: Path, index: Path, env: dict) -> bool:
    """Seed the snapshot index from the source repo's LIVE index, best-effort.

    A fresh index populated by ``git read-tree`` carries no stat cache, so the
    following ``git add -A`` re-hashes the content of EVERY tracked file — tens
    of seconds per run on a large repository. The live index's stat cache lets
    ``add -A`` skip files whose stat still matches (git's racily-clean handling
    re-hashes anything ambiguous), so only genuinely dirty paths are hashed.
    The resulting tree is identical either way: ``add -A`` syncs the index to
    the exact worktree state regardless of its starting point. Returns False on
    any failure — the caller falls back to the read-tree path (which also covers
    an incompatible seed: split index, torn copy — those fail the subsequent git
    command and are rebuilt unseeded).
    """
    # REFUSE the seed when the live index carries suppressive per-entry flags.
    # `assume-unchanged` (lowercase tag) and `skip-worktree` ('S'/'s') make git
    # trust the index over the worktree, so a seeded `git add -A` would silently
    # snapshot STALE content for a genuinely edited file — an incomplete
    # snapshot, the one failure mode worse than a slow one. The unseeded
    # read-tree path starts from a flag-free index and re-hashes everything,
    # which is exactly the pre-seed behavior for these repos. `ls-files -v`
    # tags every regular entry 'H'; anything else is grounds to fall back
    # (conservative: an unrecognized tag costs only the slow path).
    flags = _run_git(
        ["git", "ls-files", "-v"], cwd=str(source),
        env=env, capture_output=True, text=True,
    )
    if flags.returncode != 0 or any(
        line[:1] != "H" for line in flags.stdout.splitlines() if line
    ):
        return False
    probe = _run_git(
        ["git", "rev-parse", "--git-path", "index"], cwd=str(source),
        env=env, capture_output=True, text=True,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return False
    src = Path(probe.stdout.strip())
    if not src.is_absolute():
        src = source / src
    try:
        shutil.copyfile(src, index)
    except OSError:
        return False
    return True


def _populate_snapshot_index(
    source: Path, source_head: str, snapshot_env: dict, *, seeded: bool
) -> None:
    """Bring the snapshot index to the exact worktree state (raises on failure)."""
    commands = (
        [["git", "add", "-A"]]
        if seeded
        else [["git", "read-tree", source_head], ["git", "add", "-A"]]
    )
    for command in commands:
        proc = _run_git(
            command, cwd=str(source), env=snapshot_env,
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise WorkspaceError(proc.stderr.strip() or "snapshot index failed")


def capture_repository_snapshot(
    repo_root: Path, run_id: str, workspace_root: Path, *, sweepable: bool = False
) -> RepositorySnapshot:
    """Capture HEAD + tracked dirty state + untracked files in a private clone.

    The source index, branch, and object database are never modified. Ignored
    files stay out of the snapshot by normal Git rules.

    ``sweepable`` marks the workspace as a disposable supervisor run, claiming it
    for ``sweep_orphaned_workspaces`` the moment the directory exists — before
    the clone, which is the slow part an interrupt is most likely to land in, and
    an unmarked leftover is one the sweep can never reclaim. Rooms and
    interactive sessions leave it False: their workspaces are tracked externally
    and may outlive any run, so nothing may reclaim them on liveness or age.
    """
    source = repo_root.resolve()
    env = isolated_git_env()
    if not _is_git_repo(source):
        raise WorkspaceError(f"not a Git repository: {source}")
    top = _run_git(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(source),
        env=env, capture_output=True, text=True,
    )
    if top.returncode != 0 or not top.stdout.strip():
        # Without this check an empty stdout resolves Path("") to the PROCESS
        # CWD, which would then anchor every subsequent git command.
        raise WorkspaceError(
            top.stderr.strip() or f"cannot resolve git toplevel for {source}"
        )
    source = Path(top.stdout.strip()).resolve()
    head = _run_git(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=str(source),
        env=env, capture_output=True, text=True,
    )
    if head.returncode != 0:
        raise WorkspaceError("repository has no initial commit")
    source_head = head.stdout.strip()
    unmerged = _run_git(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(source),
        env=env, capture_output=True, text=True,
    )
    if unmerged.stdout.strip():
        raise WorkspaceError("repository has unresolved merge conflicts")
    for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        probe = _run_git(
            ["git", "rev-parse", "--verify", "-q", marker], cwd=str(source),
            env=env, capture_output=True,
        )
        if probe.returncode == 0:
            raise WorkspaceError(f"repository operation in progress: {marker}")
    sparse = _run_git(
        ["git", "config", "--bool", "core.sparseCheckout"], cwd=str(source),
        env=env, capture_output=True, text=True,
    )
    if sparse.returncode == 0 and sparse.stdout.strip() == "true":
        raise WorkspaceError("sparse checkouts are not supported yet")
    submodules = _run_git(
        ["git", "submodule", "status", "--recursive"], cwd=str(source),
        env=env, capture_output=True, text=True,
    )
    if submodules.returncode != 0 or any(
        line[:1] in {"+", "-", "U"} for line in submodules.stdout.splitlines()
    ):
        raise WorkspaceError("submodules must be initialized and clean")
    if submodules.stdout.strip():
        dirty_submodules = _run_git(
            [
                "git", "submodule", "foreach", "--recursive", "--quiet",
                'git diff-index --quiet HEAD -- && test -z "$(git ls-files --others --exclude-standard)"',
            ],
            cwd=str(source), env=env, capture_output=True, text=True,
        )
        if dirty_submodules.returncode != 0:
            raise WorkspaceError("submodules must be initialized and clean")

    for operation_dir in ("rebase-merge", "rebase-apply"):
        probe = _run_git(
            ["git", "rev-parse", "--git-path", operation_dir], cwd=str(source),
            env=env, capture_output=True, text=True,
        )
        operation_path = Path(probe.stdout.strip())
        if not operation_path.is_absolute():
            operation_path = source / operation_path
        if probe.returncode == 0 and operation_path.exists():
            raise WorkspaceError(f"repository operation in progress: {operation_dir}")

    fingerprint_before = repository_fingerprint(source)

    run_root = workspace_root.expanduser().resolve() / run_id
    control = run_root / "control"
    if run_root.exists():
        raise WorkspaceError(f"workspace already exists for run {run_id}")
    try:
        run_root.mkdir(parents=True, mode=0o700)
        if sweepable:
            write_run_sentinel(run_root)
        clone = _run_git(
            ["git", "clone", "--shared", "--no-checkout", str(source), str(control)],
            env=env, capture_output=True, text=True,
        )
        if clone.returncode != 0:
            raise WorkspaceError(clone.stderr.strip() or "private clone failed")
        index = run_root / "snapshot.index"
        snapshot_env = isolated_git_env({
            "GIT_DIR": str(control / ".git"),
            "GIT_WORK_TREE": str(source),
            "GIT_INDEX_FILE": str(index),
            "GIT_AUTHOR_NAME": "syncmill",
            "GIT_AUTHOR_EMAIL": "syncmill@local",
            "GIT_COMMITTER_NAME": "syncmill",
            "GIT_COMMITTER_EMAIL": "syncmill@local",
        })
        seeded = _seed_snapshot_index(source, index, env)
        try:
            _populate_snapshot_index(source, source_head, snapshot_env, seeded=seeded)
        except WorkspaceError:
            if not seeded:
                raise
            # The seed was incompatible (split index, torn copy) — rebuild the
            # index from scratch on the always-correct read-tree path.
            index.unlink(missing_ok=True)
            _populate_snapshot_index(source, source_head, snapshot_env, seeded=False)
        tree = _run_git(
            ["git", "write-tree"], cwd=str(source), env=snapshot_env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        commit = _run_git(
            ["git", "commit-tree", tree, "-p", source_head, "-m", f"syncmill snapshot {run_id}"],
            cwd=str(source), env=snapshot_env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        fingerprint = repository_fingerprint(source)
        if fingerprint != fingerprint_before:
            raise WorkspaceError(
                "working tree changed while the snapshot was being captured; retry"
            )
        index.unlink(missing_ok=True)
        return RepositorySnapshot(
            source, run_root, control, commit, source_head, fingerprint
        )
    except Exception as exc:
        shutil.rmtree(run_root, ignore_errors=True)
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError(f"could not create repository snapshot: {exc}") from exc


def create_worktree(
    repo_root: Path, agent_id: str, task_id: str, slot: int | None = None
) -> Worktree:
    """Create an isolated workspace for an agent.

    Uses git worktree if in a git repo, falls back to tempdir otherwise.

    ``slot`` disambiguates repeat worktrees for the **same** ``(agent_id,
    task_id)`` within one run. The branch and path are derived from
    ``agent_id`` + ``task_id`` alone, so a second ``git worktree add`` for the same
    agent would otherwise collide on an existing branch/path, fail, and silently
    fall back to a (non-git, empty) tempdir. Most strategies acquire one worktree
    per agent and pass ``None`` (the original bare naming); ``DecomposeStrategy``
    acquires several per agent (planner + per-subtask + per-synthesizer), so
    ``WorktreePool`` threads a per-acquire ``slot`` to keep every worktree distinct.
    """
    if not _is_git_repo(repo_root):
        return _create_tempdir(agent_id)

    tag = task_id[:8] if slot is None else f"{task_id[:8]}-{slot}"
    branch = f"syncmill/{agent_id}/{tag}"
    worktree_dir = repo_root.parent / ".syncmill-worktrees"
    worktree_path = worktree_dir / f"{agent_id}-{tag}"

    try:
        worktree_dir.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["git", "worktree", "add", "-b", branch, str(worktree_path)],
            capture_output=True, text=True, cwd=str(repo_root),
            env=isolated_git_env(),
            check=True,
        )
        logger.debug("Created worktree at %s (branch: %s)", worktree_path, branch)
        return Worktree(
            path=worktree_path,
            branch=branch,
            agent_id=agent_id,
            is_git=True,
            base_commit=_rev_parse(repo_root, "HEAD"),
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.warning("Failed to create git worktree: %s. Falling back to tempdir.", exc)
        return _create_tempdir(agent_id)


def cleanup_worktree(repo_root: Path, worktree: Worktree) -> bool:
    """Remove a worktree and its branch. Returns True if the path is gone after."""
    if worktree.is_temp:
        try:
            shutil.rmtree(worktree.path, ignore_errors=True)
            logger.debug("Cleaned up tempdir %s", worktree.path)
        except Exception:
            pass
        return not worktree.path.exists()

    if not worktree.is_git or not worktree.branch:
        return False

    try:
        env = isolated_git_env()
        rm = _run_git(
            ["git", "worktree", "remove", str(worktree.path), "--force"],
            capture_output=True, text=True, cwd=str(repo_root), env=env,
        )
        if rm.returncode != 0:
            logger.warning(
                "git worktree remove failed for %s: %s", worktree.path, rm.stderr.strip()
            )
        br = _run_git(
            ["git", "branch", "-D", worktree.branch],
            capture_output=True, text=True, cwd=str(repo_root), env=env,
        )
        if br.returncode != 0:
            logger.warning(
                "git branch -D failed for %s: %s", worktree.branch, br.stderr.strip()
            )
        logger.debug("Cleaned up worktree %s (branch: %s)", worktree.path, worktree.branch)
        return not worktree.path.exists()
    except Exception as exc:
        logger.warning("Failed to cleanup worktree: %s", exc)
        return False


class WorktreePool:
    """Creates per-run worktrees on demand and tracks every one for cleanup.

    A run's worktree topology is the *strategy's* decision, but their lifetime is
    the *Supervisor's*: it owns one pool per run and calls ``cleanup_all`` in a
    ``finally``, so however many worktrees a strategy acquires (one per agent for
    concurrent isolation, or a single shared one for a sequential pipeline) all
    normally get torn down. Explicit recovery retention exempts the private run
    workspace so it can be inspected after an undecidable failure.

    Worktrees are still created by ``create_worktree``, so each lands on its own
    ``syncmill/{agent}/{taskid}-{slot}`` branch beside ``repo_root`` (tempdir
    fallback for non-git repos). Every ``acquire`` gets a distinct ``slot`` (a
    per-pool monotonic counter), so even repeat acquisitions for the **same** agent
    — which ``DecomposeStrategy`` makes (planner + per-subtask + per-synthesizer) —
    get distinct branches/paths and never collide on ``git worktree add``.
    """

    supports_snapshots = True

    def __init__(
        self,
        repo_root: Path,
        task_id: str,
        snapshot: RepositorySnapshot | None = None,
        seed_patch: bytes | None = None,
    ):
        self._snapshot = snapshot
        self._seed_patch = seed_patch
        self._repo_root = snapshot.control_repo if snapshot is not None else repo_root
        self._task_id = task_id
        self._worktrees: list[Worktree] = []
        self._next_slot = 0  # per-acquire discriminator → distinct repeat worktrees
        self._retained = False
        if snapshot is not None:
            # Re-claim: capture_repository_snapshot already wrote the sentinel, but
            # this process may not be the one that did (a resumed/handed-off
            # workspace), and the sweep reads the pid to decide liveness.
            with contextlib.suppress(OSError):
                write_run_sentinel(snapshot.run_root)

    def retain(self) -> None:
        """Keep the private workspace for manual recovery after artifact failure."""
        self._retained = True
        if self._snapshot is not None:
            # ADD an exemption marker rather than removing the run sentinel:
            # creating a file is the operation more likely to succeed, and if it
            # does fail the workspace is still marked as a run rather than being
            # left in a state where neither file says anything.
            with contextlib.suppress(OSError):
                (self._snapshot.run_root / _RECOVERY_MARKER).write_text("")

    def acquire(self, agent_id: str) -> Worktree:
        """Create and track an isolated worktree for ``agent_id``.

        Each call advances ``slot`` so acquiring the same agent twice in one run
        (Decompose's planner/subtask/synthesis phases) never collides.
        """
        # Claim the slot up front: a failed `git worktree add` used to leave
        # `_next_slot` unadvanced, so a retried acquire recomputed the SAME
        # branch/path — now guaranteed to fail again against the partial
        # leftovers of the first attempt. A skipped slot is just a gap.
        slot = self._next_slot
        self._next_slot += 1
        if self._snapshot is None:
            worktree = create_worktree(
                self._repo_root, agent_id, self._task_id, slot=slot
            )
        else:
            # Refresh the liveness marker: the sweep's age backstop exists for a
            # reused pid, and a run doing real work must not age into it.
            with contextlib.suppress(OSError):
                write_run_sentinel(self._snapshot.run_root)
            tag = f"{self._task_id[:8]}-{slot}"
            branch = f"syncmill/{agent_id}/{tag}"
            path = self._snapshot.run_root / "worktrees" / f"{agent_id}-{tag}"
            path.parent.mkdir(parents=True, exist_ok=True)
            proc = _run_git(
                ["git", "worktree", "add", "-b", branch, str(path), self._snapshot.commit],
                cwd=str(self._snapshot.control_repo), env=isolated_git_env(),
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise WorkspaceError(proc.stderr.strip() or "worktree creation failed")
            worktree = Worktree(
                path, branch, agent_id, True, base_commit=self._snapshot.commit
            )
            if self._seed_patch:
                proc = _run_git(
                    ["git", "apply", "--binary", "-"],
                    cwd=str(path), env=isolated_git_env(),
                    input=self._seed_patch, capture_output=True,
                )
                if proc.returncode != 0:
                    cleanup_worktree(self._snapshot.control_repo, worktree)
                    raise WorkspaceError(
                        proc.stderr.decode(errors="replace").strip()
                        or "could not restore parent patch"
                    )
        self._worktrees.append(worktree)
        return worktree

    @property
    def worktrees(self) -> list[Worktree]:
        """Every worktree acquired so far (a fresh copy)."""
        return list(self._worktrees)

    def cleanup_all(self) -> int:
        """Remove all acquired worktrees + branches. Returns the count removed.

        Each removal is independent — one failure never skips the rest — so a
        single stuck worktree can't strand the others.
        """
        if self._retained:
            logger.warning("Retaining recovery workspace at %s", self.recovery_path)
            return 0
        if self._snapshot is not None:
            # Snapshot pools place every worktree, its branch, and the worktree
            # registration metadata INSIDE run_root (the control clone) — the
            # rmtree below removes all of it, so the per-worktree
            # `git worktree remove` + `git branch -D` pair spawned two
            # subprocesses per worktree only to delete files about to be
            # deleted anyway (Decompose acquires the most: planner + per-subtask
            # + per-synthesizer).
            removed = len(self._worktrees)
            self._worktrees.clear()
            shutil.rmtree(self._snapshot.run_root, ignore_errors=True)
            return removed
        removed = 0
        for worktree in self._worktrees:
            try:
                if cleanup_worktree(self._repo_root, worktree):
                    removed += 1
            except Exception as exc:  # pragma: no cover - cleanup_worktree guards itself
                logger.warning("Failed to clean up worktree %s: %s", worktree.path, exc)
        self._worktrees.clear()
        return removed

    @property
    def recovery_path(self) -> Path | None:
        return self._snapshot.run_root if self._snapshot is not None else None


def write_run_sentinel(run_root: Path) -> None:
    """Claim ``run_root`` for this process: ownership plus a liveness handle.

    Written as soon as the directory exists — BEFORE the expensive clone — so an
    interrupt partway through setup still leaves a directory the sweep can
    recognise. An unmarked directory is never swept, so a marker written late is
    a permanent leak.
    """
    (run_root / _RUN_SENTINEL).write_text(f"{os.getpid()}\n", encoding="utf-8")


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return True  # undecidable — assume alive, never delete on a guess
    return True


def _sentinel_owner_pid(sentinel: Path) -> int | None:
    """The pid recorded in a run sentinel, or ``None`` if it has none.

    Sentinels written before this carried no pid; those fall back to age alone.
    """
    try:
        return int(sentinel.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def sweep_orphaned_workspaces(
    workspace_root: Path, *, older_than: float = _STALE_WORKSPACE_SECONDS
) -> int:
    """Remove abandoned private run workspaces under ``workspace_root``.

    ``WorktreePool.cleanup_all`` removes a run's workspace on every ordinary exit,
    but a SIGKILL or power loss skips it, and nothing else reclaims the control
    clone plus its worktrees — so they accumulate under ``workspace.root``
    forever. This is the counterpart of ``cleanup_orphaned_worktrees``, which only
    ever sees the legacy in-repo worktree layout.

    Three guards keep it from touching anything it shouldn't. ``workspace.root``
    is shared with room and interactive-session workspaces, whose lifecycles are
    tracked externally and can legitimately outlive any run, so a directory is
    only a candidate if it carries ``_RUN_SENTINEL``. A workspace ``retain()``
    marked for recovery is exempt permanently. And the run is only abandoned if
    **the process that created it is gone** — asked directly, not inferred from
    file timestamps, so no amount of idleness, tree size, or scan budget can make
    a live run look dead. That is the whole reason liveness is a pid and not an
    mtime: an agent thinking for hours writes nothing, and a heuristic that
    guesses wrong deletes the workspace out from under it.

    Age is only the backstop for the case a pid cannot decide — the owner is gone
    but its pid has been reused, which would otherwise protect the leak forever —
    so a live pid stops protecting a workspace once it is that stale. ``acquire``
    refreshes the sentinel, so a run that is doing anything at all stays fresh, and
    the backstop only reaches a workspace nothing has touched for ``older_than``
    (the CLI passes ``workspace.stale_seconds``; a week by default). A sentinel
    with no pid (written by an older version) falls back to age alone.

    Fully best-effort: every error is ignored, like the sweeps in ``monitor.py``
    and ``session.py``. Returns the number of workspaces removed.
    """
    try:
        entries = list(workspace_root.expanduser().iterdir())
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for entry in entries:
        try:
            sentinel = entry / _RUN_SENTINEL
            if not (entry.is_dir() and sentinel.is_file()):
                continue
            if (entry / _RECOVERY_MARKER).exists():
                continue
            pid = _sentinel_owner_pid(sentinel)
            stale = now - sentinel.stat().st_mtime > older_than
            if pid is not None and _process_is_alive(pid) and not stale:
                continue
            if pid is None and not stale:
                continue  # no pid recorded: age is all there is to go on
        except OSError:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed += 1
            logger.info("Swept abandoned run workspace %s", entry)
    return removed


@dataclass
class RetainedWorkspace:
    """One recovery-retained workspace under ``workspace.root`` (#118)."""

    name: str  # the workspace directory name (the run's dir name)
    path: Path
    age_seconds: float


def list_retained_workspaces(workspace_root: Path) -> list[RetainedWorkspace]:
    """Enumerate recovery-retained workspaces (oldest first).

    These are the dirs ``retain()`` marked with ``_RECOVERY_MARKER`` —
    permanently exempt from ``sweep_orphaned_workspaces``, so without this
    surface they accumulate forever. Read-only and best-effort: enumeration
    errors yield an empty/partial list, never an exception.

    BOTH sentinels are required: ``_RUN_SENTINEL`` is this module's ownership
    proof, and the recovery marker alone is just a filename a foreign
    directory could happen to contain — which this listing feeds to a
    destructive verb. Room and interactive-session workspaces are created
    ``sweepable=False`` (no run sentinel) and so are deliberately out of
    scope here; a room's retained workspace is tracked in the rooms store,
    where ``syncmill room recover`` reconciles its ownership and
    ``syncmill room close`` is what actually reclaims the directory.
    """
    retained: list[RetainedWorkspace] = []
    now = time.time()
    try:
        entries = list(workspace_root.expanduser().iterdir())
    except OSError:
        return retained
    for entry in entries:
        try:
            marker = entry / _RECOVERY_MARKER
            if not (
                entry.is_dir()
                and marker.is_file()
                and (entry / _RUN_SENTINEL).is_file()
            ):
                continue
            age = max(0.0, now - marker.stat().st_mtime)
        except OSError:
            continue
        retained.append(RetainedWorkspace(entry.name, entry, age))
    retained.sort(key=lambda ws: -ws.age_seconds)
    return retained


def discard_retained_workspace(workspace_root: Path, name: str) -> Path:
    """Explicitly delete ONE recovery-retained workspace; fail closed.

    Deletion stays explicit/human (#118): nothing ever auto-reaps a retained
    workspace, this is the operator's reclaim verb. Destructive-boundary
    exactness: the caller's ``name`` is matched as an exact child directory —
    never a glob, never a path that can escape ``workspace_root`` — and BOTH
    the ``_RECOVERY_MARKER`` written by ``retain()`` and this module's
    ``_RUN_SENTINEL`` ownership proof must be present, so an ordinary (possibly
    live) run workspace, a room/session workspace, or a foreign directory that
    merely contains a file of that name is refused with ``WorkspaceError``
    rather than removed.
    """
    root = workspace_root.expanduser()
    target = root / name
    try:
        if target.resolve().parent != root.resolve():
            raise WorkspaceError(f"not a workspace name: {name!r}")
    except OSError as exc:
        raise WorkspaceError(f"could not resolve workspace {name!r}: {exc}") from exc
    if not target.is_dir():
        raise WorkspaceError(f"no retained workspace named {name!r}")
    if not (
        (target / _RECOVERY_MARKER).is_file() and (target / _RUN_SENTINEL).is_file()
    ):
        raise WorkspaceError(
            f"{name!r} is not a recovery-retained run workspace; refusing to delete"
        )
    shutil.rmtree(target)
    return target


def cleanup_orphaned_worktrees(repo_root: Path) -> int:
    """Remove orphaned syncmill worktrees left by crashed executions."""
    if not _is_git_repo(repo_root):
        return 0

    syncmill_dir = (repo_root.parent / ".syncmill-worktrees").resolve()
    cleaned = 0
    try:
        proc = _run_git(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=str(repo_root),
            env=isolated_git_env(),
        )
        if proc.returncode != 0:
            return 0

        entry: dict[str, str | None] = {"path": None, "branch": None}

        def _flush() -> int:
            path, branch = entry["path"], entry["branch"]
            if not (branch and branch.startswith("syncmill/") and path):
                return 0
            wt_path = Path(path)
            # Only ever touch worktrees inside our own .syncmill-worktrees dir —
            # never force-remove a worktree/branch git reports from elsewhere.
            if not _within(wt_path, syncmill_dir):
                logger.warning("Skipping worktree outside %s: %s", syncmill_dir, wt_path)
                return 0
            if not wt_path.exists() or _is_empty_worktree(wt_path):
                removed = cleanup_worktree(
                    repo_root,
                    Worktree(path=wt_path, branch=branch, agent_id="orphan", is_git=True),
                )
                return 1 if removed else 0
            return 0

        for line in proc.stdout.splitlines():
            if line.startswith("worktree "):
                entry = {"path": line[len("worktree "):], "branch": None}
            elif line.startswith("branch refs/heads/"):
                entry["branch"] = line[len("branch refs/heads/"):]
            elif line == "":
                cleaned += _flush()
                entry = {"path": None, "branch": None}
        # Flush the final entry if porcelain output had no trailing blank line.
        cleaned += _flush()
    except Exception as exc:
        logger.warning("Error during orphan cleanup: %s", exc)

    if cleaned:
        logger.info("Cleaned up %d orphaned worktree(s)", cleaned)
    return cleaned


def _within(path: Path, parent: Path) -> bool:
    """True if ``path`` is ``parent`` or nested under it (containment guard)."""
    try:
        path.resolve().relative_to(parent)
        return True
    except (ValueError, OSError):
        return False


def _is_git_repo(path: Path) -> bool:
    try:
        proc = _run_git(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=str(path),
            env=isolated_git_env(),
        )
        return proc.returncode == 0
    except Exception:
        return False


def _rev_parse(path: Path, revision: str) -> str | None:
    try:
        proc = _run_git(
            ["git", "rev-parse", "--verify", revision],
            capture_output=True, text=True, cwd=str(path),
            env=isolated_git_env(),
        )
        return proc.stdout.strip() if proc.returncode == 0 else None
    except OSError:
        return None


def _create_tempdir(agent_id: str) -> Worktree:
    tmpdir = Path(tempfile.mkdtemp(prefix=f"syncmill-{agent_id}-"))
    logger.debug("Created tempdir workspace at %s", tmpdir)
    return Worktree(
        path=tmpdir,
        branch=None,
        agent_id=agent_id,
        is_git=False,
        is_temp=True,
    )


def _is_empty_worktree(path: Path) -> bool:
    """Check if a worktree directory has no meaningful changes.

    Returns False if emptiness can't be proven (status error/exception), so a
    transient failure never causes a worktree with real work to be deleted.
    """
    try:
        proc = _run_git(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(path),
            env=isolated_git_env(),
        )
        return proc.returncode == 0 and not proc.stdout.strip()
    except Exception:
        return False
