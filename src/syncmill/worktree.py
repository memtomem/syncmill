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


def create_worktree(repo_root: Path, agent_id: str, task_id: str) -> Worktree:
    """Create an isolated workspace for an agent.

    Uses git worktree if in a git repo, falls back to tempdir otherwise.
    """
    if not _is_git_repo(repo_root):
        return _create_tempdir(agent_id)

    branch = f"syncmill/{agent_id}/{task_id[:8]}"
    worktree_dir = repo_root.parent / ".syncmill-worktrees"
    worktree_path = worktree_dir / f"{agent_id}-{task_id[:8]}"

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
