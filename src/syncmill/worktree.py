"""Workspace isolation via git worktrees with tempdir fallback."""

from __future__ import annotations

import logging
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
        rm = subprocess.run(
            ["git", "worktree", "remove", str(worktree.path), "--force"],
            capture_output=True, text=True, cwd=str(repo_root),
        )
        if rm.returncode != 0:
            logger.warning(
                "git worktree remove failed for %s: %s", worktree.path, rm.stderr.strip()
            )
        br = subprocess.run(
            ["git", "branch", "-D", worktree.branch],
            capture_output=True, text=True, cwd=str(repo_root),
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

    def __init__(self, repo_root: Path, task_id: str):
        self._repo_root = repo_root
        self._task_id = task_id
        self._worktrees: list[Worktree] = []
        self._next_slot = 0  # per-acquire discriminator → distinct repeat worktrees

    def acquire(self, agent_id: str) -> Worktree:
        """Create and track an isolated worktree for ``agent_id``.

        Each call advances ``slot`` so acquiring the same agent twice in one run
        (Decompose's planner/subtask/synthesis phases) never collides.
        """
        worktree = create_worktree(
            self._repo_root, agent_id, self._task_id, slot=self._next_slot
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
        removed = 0
        for worktree in self._worktrees:
            try:
                if cleanup_worktree(self._repo_root, worktree):
                    removed += 1
            except Exception as exc:  # pragma: no cover - cleanup_worktree guards itself
                logger.warning("Failed to clean up worktree %s: %s", worktree.path, exc)
        self._worktrees.clear()
        return removed


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
