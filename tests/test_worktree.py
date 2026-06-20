"""worktree.py — workspace creation, cleanup, orphan sweeping, path safety."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from syncmill import worktree as W
from syncmill.worktree import (
    Worktree,
    WorktreePool,
    _is_empty_worktree,
    _is_git_repo,
    _within,
    cleanup_orphaned_worktrees,
    cleanup_worktree,
    create_worktree,
)


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── create_worktree ──────────────────────────────────────────────────────


def test_create_worktree_git(git_repo):
    wt = create_worktree(git_repo, "claude", "abcdef1234")
    assert wt.is_git is True and wt.is_temp is False
    assert wt.branch == "syncmill/claude/abcdef12"
    assert wt.path.exists()
    assert wt.path.parent == (git_repo.parent / ".syncmill-worktrees")


def test_create_worktree_non_git_falls_back_to_tempdir(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    wt = create_worktree(plain, "codex", "deadbeef00")
    assert wt.is_git is False and wt.is_temp is True
    assert wt.branch is None
    assert wt.path.exists()
    shutil.rmtree(wt.path, ignore_errors=True)


def test_create_worktree_falls_back_when_git_add_fails(monkeypatch, git_repo):
    monkeypatch.setattr(W, "_is_git_repo", lambda p: True)

    def boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(W.subprocess, "run", boom)
    wt = create_worktree(git_repo, "claude", "abcdef1234")
    assert wt.is_temp is True and wt.is_git is False
    shutil.rmtree(wt.path, ignore_errors=True)


# ── cleanup_worktree ─────────────────────────────────────────────────────


def test_cleanup_git_worktree_removes_dir_and_branch(git_repo):
    wt = create_worktree(git_repo, "claude", "abcdef1234")
    assert wt.path.exists()
    cleanup_worktree(git_repo, wt)
    assert not wt.path.exists()
    branches = subprocess.run(
        ["git", "branch"], cwd=str(git_repo), capture_output=True, text=True
    ).stdout
    assert "syncmill/claude/abcdef12" not in branches


def test_cleanup_temp_worktree(tmp_path):
    d = tmp_path / "t"
    d.mkdir()
    wt = Worktree(path=d, branch=None, agent_id="x", is_git=False, is_temp=True)
    cleanup_worktree(tmp_path, wt)
    assert not d.exists()


def test_cleanup_git_worktree_branch_none_is_noop(git_repo):
    wt = Worktree(path=git_repo / "nope", branch=None, agent_id="x", is_git=True)
    cleanup_worktree(git_repo, wt)  # must not raise


# ── _is_empty_worktree / _is_git_repo / _within ──────────────────────────


def test_is_empty_worktree(git_repo):
    wt = create_worktree(git_repo, "claude", "abcdef1234")
    assert _is_empty_worktree(wt.path) is True
    (wt.path / "dirty.txt").write_text("x\n")
    assert _is_empty_worktree(wt.path) is False
    cleanup_worktree(git_repo, wt)


def test_is_git_repo(git_repo, tmp_path):
    assert _is_git_repo(git_repo) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _is_git_repo(plain) is False


def test_within():
    parent = Path("/a/b").resolve()
    assert _within(Path("/a/b/c"), parent) is True
    assert _within(Path("/a/x"), parent) is False


# ── cleanup_orphaned_worktrees ───────────────────────────────────────────


def test_orphan_cleanup_non_git_returns_zero(tmp_path):
    assert cleanup_orphaned_worktrees(tmp_path) == 0


def test_orphan_cleanup_clean_repo_returns_zero(git_repo):
    assert cleanup_orphaned_worktrees(git_repo) == 0


def test_orphan_cleanup_removes_orphan(git_repo):
    wt = create_worktree(git_repo, "claude", "abcdef1234")
    # Simulate a crash: the worktree dir vanishes but git still tracks it.
    shutil.rmtree(wt.path, ignore_errors=True)
    cleaned = cleanup_orphaned_worktrees(git_repo)
    assert cleaned == 1
    branches = subprocess.run(
        ["git", "branch"], cwd=str(git_repo), capture_output=True, text=True
    ).stdout
    assert "syncmill/claude/abcdef12" not in branches


def test_is_empty_worktree_false_on_error(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise OSError("git exploded")

    monkeypatch.setattr(W.subprocess, "run", boom)
    # can't prove empty -> conservatively False (never delete on uncertainty)
    assert _is_empty_worktree(tmp_path) is False


def test_cleanup_worktree_returns_bool(git_repo):
    wt = create_worktree(git_repo, "claude", "abcdef1234")
    assert cleanup_worktree(git_repo, wt) is True
    noop = Worktree(path=git_repo / "x", branch=None, agent_id="x", is_git=True)
    assert cleanup_worktree(git_repo, noop) is False


def test_orphan_cleanup_counts_only_real_removals(monkeypatch, git_repo):
    wt_dir = git_repo.parent / ".syncmill-worktrees"
    porcelain = (
        f"worktree {wt_dir}/claude-deadbeef\n"
        "branch refs/heads/syncmill/claude/deadbeef\n\n"
    )
    monkeypatch.setattr(W, "_is_git_repo", lambda p: True)
    monkeypatch.setattr(W.subprocess, "run", lambda cmd, **kw: _Completed(0, porcelain))
    # failed cleanup must NOT be counted
    monkeypatch.setattr(W, "cleanup_worktree", lambda root, w: False)
    assert cleanup_orphaned_worktrees(git_repo) == 0
    # successful cleanup IS counted
    monkeypatch.setattr(W, "cleanup_worktree", lambda root, w: True)
    assert cleanup_orphaned_worktrees(git_repo) == 1


def test_orphan_cleanup_skips_worktree_outside_our_dir(monkeypatch, git_repo):
    monkeypatch.setattr(W, "_is_git_repo", lambda p: True)
    porcelain = "worktree /etc/evil\nbranch refs/heads/syncmill/x/deadbeef\n\n"
    monkeypatch.setattr(
        W.subprocess, "run", lambda cmd, **kw: _Completed(0, porcelain)
    )
    calls = []
    monkeypatch.setattr(W, "cleanup_worktree", lambda root, w: calls.append(w))
    assert cleanup_orphaned_worktrees(git_repo) == 0
    assert calls == []  # never touched the out-of-tree worktree


# ── WorktreePool ──────────────────────────────────────────────────────────


def test_pool_acquire_tracks_and_creates(git_repo):
    pool = WorktreePool(git_repo, "abcdef1234")
    wt = pool.acquire("claude")
    assert wt.is_git and wt.path.exists()
    assert wt.branch == "syncmill/claude/abcdef12"
    assert pool.worktrees == [wt]
    pool.cleanup_all()


def test_pool_acquire_per_agent_distinct(git_repo):
    pool = WorktreePool(git_repo, "abcdef1234")
    a = pool.acquire("claude")
    b = pool.acquire("codex")
    assert a.path != b.path and a.branch != b.branch
    assert a.path.exists() and b.path.exists()
    assert [w.agent_id for w in pool.worktrees] == ["claude", "codex"]
    pool.cleanup_all()


def test_pool_cleanup_all_removes_everything(git_repo):
    pool = WorktreePool(git_repo, "abcdef1234")
    a = pool.acquire("claude")
    b = pool.acquire("codex")
    removed = pool.cleanup_all()
    assert removed == 2
    assert not a.path.exists() and not b.path.exists()
    assert pool.worktrees == []  # tracking cleared
    branches = subprocess.run(
        ["git", "branch"], cwd=str(git_repo), capture_output=True, text=True
    ).stdout
    assert "syncmill/claude/abcdef12" not in branches
    assert "syncmill/codex/abcdef12" not in branches


def test_pool_cleanup_all_independent_on_failure(monkeypatch, git_repo):
    # one failed removal is excluded from the count but never strands the rest
    pool = WorktreePool(git_repo, "abcdef1234")
    a = pool.acquire("claude")
    b = pool.acquire("codex")
    real = W.cleanup_worktree
    monkeypatch.setattr(
        W, "cleanup_worktree", lambda root, w: False if w is a else real(root, w)
    )
    removed = pool.cleanup_all()
    assert removed == 1  # only codex counted
    assert not b.path.exists()  # codex cleaned despite claude's "failure"
    real(git_repo, a)  # clean up the deliberately-skipped one


def test_pool_over_non_git_uses_tempdirs(tmp_path):
    # non-git repo → pool hands out tempdir-fallback worktrees, and cleanup_all
    # tears them down (shutil.rmtree) just the same.
    plain = tmp_path / "plain"
    plain.mkdir()
    pool = WorktreePool(plain, "abcdef1234")
    a = pool.acquire("claude")
    b = pool.acquire("codex")
    assert a.is_temp and b.is_temp
    assert a.path != b.path
    assert a.path.exists() and b.path.exists()
    removed = pool.cleanup_all()
    assert removed == 2
    assert not a.path.exists() and not b.path.exists()
    assert pool.worktrees == []


def test_pool_concurrent_per_agent_isolation(git_repo):
    # The #5 acceptance criterion, end-to-end: N agents launched together via
    # asyncio.gather, each in its own isolated pool worktree, each writing a
    # different file — zero cross-contamination, correct per-worktree diff, full
    # cleanup. (Temporal overlap of the async subprocesses is proven separately
    # in test_agents.py; here the property under test is per-worktree isolation.)
    # Uses real git worktrees + real subprocesses (the test's own Python), so it
    # stays hermetic.
    import asyncio
    import sys

    from syncmill.agents import CLIAgent
    from syncmill.config import AgentConfig
    from syncmill.memory import run_async
    from syncmill.models import TaskStatus
    from syncmill.supervisor import _git_changed_files

    class _Writer(CLIAgent):
        def __init__(self, agent_id: str, fname: str):
            super().__init__(agent_id, AgentConfig())
            self._fname = fname

        def _build_command(self, prompt):
            return [sys.executable, "-c", f"open({self._fname!r}, 'w').write('x'); print('ok')"]

    agent_files = {"claude.txt", "codex.txt", "gemini.txt"}
    agents = [
        _Writer("claude", "claude.txt"),
        _Writer("codex", "codex.txt"),
        _Writer("gemini", "gemini.txt"),
    ]

    pool = WorktreePool(git_repo, "deadbeefcafe")
    worktrees: list = []
    try:
        # acquire inside the try so a partial acquisition failure is still
        # cleaned up by the finally below
        worktrees = [pool.acquire(a.agent_id) for a in agents]
        assert len({wt.path for wt in worktrees}) == 3  # distinct, isolated
        assert all(wt.is_git and wt.path.exists() for wt in worktrees)

        async def _gather():
            return await asyncio.gather(
                *(a.aexecute("p", wt.path, 30) for a, wt in zip(agents, worktrees))
            )

        results = run_async(_gather())
        assert all(r.status is TaskStatus.COMPLETED for r in results)

        for a, wt in zip(agents, worktrees):
            present = {p.name for p in wt.path.iterdir() if p.is_file()}
            assert present & agent_files == {a._fname}  # only its OWN file
            assert _git_changed_files(wt.path) == {a._fname}  # per-worktree diff
    finally:
        removed = pool.cleanup_all()
    assert removed == 3
    assert all(not wt.path.exists() for wt in worktrees)
    assert pool.worktrees == []
