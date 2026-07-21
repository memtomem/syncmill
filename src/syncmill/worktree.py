"""Workspace isolation from an immutable snapshot of the complete Git state."""

from __future__ import annotations

import logging
import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


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
    commands = (
        ["git", "rev-parse", "--verify", "HEAD"],
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        ["git", "diff", "--binary", "HEAD"],
    )
    for command in commands:
        proc = subprocess.run(command, cwd=str(repo_root), capture_output=True)
        if proc.returncode != 0:
            raise WorkspaceError(proc.stderr.decode(errors="replace").strip())
        digest.update(proc.stdout)
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=str(repo_root), capture_output=True,
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


def capture_repository_snapshot(
    repo_root: Path, run_id: str, workspace_root: Path
) -> RepositorySnapshot:
    """Capture HEAD + tracked dirty state + untracked files in a private clone.

    The source index, branch, and object database are never modified. Ignored
    files stay out of the snapshot by normal Git rules.
    """
    source = repo_root.resolve()
    if not _is_git_repo(source):
        raise WorkspaceError(f"not a Git repository: {source}")
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(source),
        capture_output=True, text=True,
    )
    source = Path(top.stdout.strip()).resolve()
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"], cwd=str(source),
        capture_output=True, text=True,
    )
    if head.returncode != 0:
        raise WorkspaceError("repository has no initial commit")
    source_head = head.stdout.strip()
    unmerged = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=str(source),
        capture_output=True, text=True,
    )
    if unmerged.stdout.strip():
        raise WorkspaceError("repository has unresolved merge conflicts")
    for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", marker], cwd=str(source),
            capture_output=True,
        )
        if probe.returncode == 0:
            raise WorkspaceError(f"repository operation in progress: {marker}")
    sparse = subprocess.run(
        ["git", "config", "--bool", "core.sparseCheckout"], cwd=str(source),
        capture_output=True, text=True,
    )
    if sparse.returncode == 0 and sparse.stdout.strip() == "true":
        raise WorkspaceError("sparse checkouts are not supported yet")
    submodules = subprocess.run(
        ["git", "submodule", "status", "--recursive"], cwd=str(source),
        capture_output=True, text=True,
    )
    if submodules.returncode != 0 or any(
        line[:1] in {"+", "-", "U"} for line in submodules.stdout.splitlines()
    ):
        raise WorkspaceError("submodules must be initialized and clean")
    if submodules.stdout.strip():
        dirty_submodules = subprocess.run(
            [
                "git", "submodule", "foreach", "--recursive", "--quiet",
                'git diff-index --quiet HEAD -- && test -z "$(git ls-files --others --exclude-standard)"',
            ],
            cwd=str(source), capture_output=True, text=True,
        )
        if dirty_submodules.returncode != 0:
            raise WorkspaceError("submodules must be initialized and clean")

    for operation_dir in ("rebase-merge", "rebase-apply"):
        probe = subprocess.run(
            ["git", "rev-parse", "--git-path", operation_dir], cwd=str(source),
            capture_output=True, text=True,
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
        clone = subprocess.run(
            ["git", "clone", "--shared", "--no-checkout", str(source), str(control)],
            capture_output=True, text=True,
        )
        if clone.returncode != 0:
            raise WorkspaceError(clone.stderr.strip() or "private clone failed")
        index = run_root / "snapshot.index"
        env = os.environ.copy()
        env.update({
            "GIT_DIR": str(control / ".git"),
            "GIT_WORK_TREE": str(source),
            "GIT_INDEX_FILE": str(index),
            "GIT_AUTHOR_NAME": "syncmill",
            "GIT_AUTHOR_EMAIL": "syncmill@local",
            "GIT_COMMITTER_NAME": "syncmill",
            "GIT_COMMITTER_EMAIL": "syncmill@local",
        })
        for command in (["git", "read-tree", source_head], ["git", "add", "-A"]):
            proc = subprocess.run(command, cwd=str(source), env=env, capture_output=True, text=True)
            if proc.returncode != 0:
                raise WorkspaceError(proc.stderr.strip() or "snapshot index failed")
        tree = subprocess.run(
            ["git", "write-tree"], cwd=str(source), env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "commit-tree", tree, "-p", source_head, "-m", f"syncmill snapshot {run_id}"],
            cwd=str(source), env=env, capture_output=True, text=True, check=True,
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
        subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree_path)],
            capture_output=True, text=True, cwd=str(repo_root),
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
        import shutil
        try:
            shutil.rmtree(worktree.path, ignore_errors=True)
            logger.debug("Cleaned up tempdir %s", worktree.path)
        except Exception:
            pass
        return not worktree.path.exists()

    if not worktree.is_git or not worktree.branch:
        return False

    try:
        env = os.environ.copy()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
            env.pop(name, None)
        rm = subprocess.run(
            ["git", "worktree", "remove", str(worktree.path), "--force"],
            capture_output=True, text=True, cwd=str(repo_root), env=env,
        )
        if rm.returncode != 0:
            logger.warning(
                "git worktree remove failed for %s: %s", worktree.path, rm.stderr.strip()
            )
        br = subprocess.run(
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
    get torn down — on success, failure, or mid-run exception.

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

    def retain(self) -> None:
        """Keep the private workspace for manual recovery after artifact failure."""
        self._retained = True

    def acquire(self, agent_id: str) -> Worktree:
        """Create and track an isolated worktree for ``agent_id``.

        Each call advances ``slot`` so acquiring the same agent twice in one run
        (Decompose's planner/subtask/synthesis phases) never collides.
        """
        if self._snapshot is None:
            worktree = create_worktree(
                self._repo_root, agent_id, self._task_id, slot=self._next_slot
            )
        else:
            tag = f"{self._task_id[:8]}-{self._next_slot}"
            branch = f"syncmill/{agent_id}/{tag}"
            path = self._snapshot.run_root / "worktrees" / f"{agent_id}-{tag}"
            path.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(path), self._snapshot.commit],
                cwd=str(self._snapshot.control_repo), capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise WorkspaceError(proc.stderr.strip() or "worktree creation failed")
            worktree = Worktree(
                path, branch, agent_id, True, base_commit=self._snapshot.commit
            )
            if self._seed_patch:
                proc = subprocess.run(
                    ["git", "apply", "--binary", "-"],
                    cwd=str(path), input=self._seed_patch, capture_output=True,
                )
                if proc.returncode != 0:
                    cleanup_worktree(self._snapshot.control_repo, worktree)
                    raise WorkspaceError(
                        proc.stderr.decode(errors="replace").strip()
                        or "could not restore parent patch"
                    )
        self._next_slot += 1
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
        removed = 0
        for worktree in self._worktrees:
            try:
                if cleanup_worktree(self._repo_root, worktree):
                    removed += 1
            except Exception as exc:  # pragma: no cover - cleanup_worktree guards itself
                logger.warning("Failed to clean up worktree %s: %s", worktree.path, exc)
        self._worktrees.clear()
        if self._snapshot is not None:
            shutil.rmtree(self._snapshot.run_root, ignore_errors=True)
        return removed

    @property
    def recovery_path(self) -> Path | None:
        return self._snapshot.run_root if self._snapshot is not None else None


def cleanup_orphaned_worktrees(repo_root: Path) -> int:
    """Remove orphaned syncmill worktrees left by crashed executions."""
    if not _is_git_repo(repo_root):
        return 0

    syncmill_dir = (repo_root.parent / ".syncmill-worktrees").resolve()
    cleaned = 0
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=str(repo_root),
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
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, cwd=str(path),
        )
        return proc.returncode == 0
    except Exception:
        return False


def _rev_parse(path: Path, revision: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", revision],
            capture_output=True, text=True, cwd=str(path),
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
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(path),
        )
        return proc.returncode == 0 and not proc.stdout.strip()
    except Exception:
        return False
