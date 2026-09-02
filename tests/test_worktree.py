"""worktree.py — workspace creation, cleanup, orphan sweeping, path safety."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

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
    sweep_orphaned_workspaces,
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

    monkeypatch.setattr(W, "_run_git", boom)
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

    monkeypatch.setattr(W, "_run_git", boom)
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
    monkeypatch.setattr(W, "_run_git", lambda cmd, **kw: _Completed(0, porcelain))
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
        W, "_run_git", lambda cmd, **kw: _Completed(0, porcelain)
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
    assert wt.branch == "syncmill/claude/abcdef12-0"  # per-acquire slot suffix
    assert pool.worktrees == [wt]
    pool.cleanup_all()


def test_pool_acquire_same_agent_repeat_is_distinct_real_git(git_repo):
    # Decompose acquires the SAME agent several times per run (planner + subtask +
    # synthesizer). Without a per-acquire slot the 2nd `git worktree add` collides
    # on the existing branch/path and silently degrades to a non-git tempdir; the
    # slot keeps each acquisition a distinct, REAL git worktree.
    pool = WorktreePool(git_repo, "abcdef1234")
    a = pool.acquire("claude")
    b = pool.acquire("claude")
    c = pool.acquire("claude")
    assert a.is_git and b.is_git and c.is_git  # none fell back to a tempdir
    assert len({w.path for w in (a, b, c)}) == 3  # distinct paths
    assert len({w.branch for w in (a, b, c)}) == 3  # distinct branches
    assert all(w.path.exists() for w in (a, b, c))
    assert pool.cleanup_all() == 3  # all three torn down


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
    from syncmill.strategies import _git_changed_files

    class _Writer(CLIAgent):
        def __init__(self, agent_id: str, fname: str):
            super().__init__(agent_id, AgentConfig())
            self._fname = fname

        def _build_command(self, prompt):
            return [sys.executable, "-c", f"open({self._fname!r}, 'w').write('x'); print('ok')"]

    agent_files = {"claude.txt", "codex.txt", "kimi-code.txt"}
    agents = [
        _Writer("claude", "claude.txt"),
        _Writer("codex", "codex.txt"),
        _Writer("kimi-code", "kimi-code.txt"),
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


# ── sweep_orphaned_workspaces (snapshot workspaces) ──────────────────────
#
# The snapshot workspace lives under workspace.root, outside the repo, so
# cleanup_orphaned_worktrees cannot see it: a SIGKILL'd run would otherwise leak
# its whole control clone forever. Liveness is the OWNING PID, asked directly —
# not inferred from timestamps, because an agent thinking for hours writes
# nothing and a heuristic that guesses wrong deletes a live run's workspace.

#: A pid that cannot be running: 0 is never a real process for os.kill(pid, 0).
_DEAD_PID = 2**22 - 1


def _workspace(root: Path, name: str, *, pid: int | None, age: float = 0.0) -> Path:
    run_root = root / name
    (run_root / "control").mkdir(parents=True)
    if pid is not None:
        marker = run_root / W._RUN_SENTINEL
        marker.write_text(f"{pid}\n")
        stale = time.time() - age
        os.utime(marker, (stale, stale))
    return run_root


def test_sweep_removes_workspaces_whose_owner_is_gone(tmp_path):
    root = tmp_path / "workspaces"
    orphan = _workspace(root, "run-dead", pid=_DEAD_PID)
    # This process owns it, so it survives regardless of how quiet it has been.
    live = _workspace(root, "run-live", pid=os.getpid(), age=3 * 24 * 3600)
    # A room / interactive-session workspace: same root, externally tracked
    # lifecycle, no sentinel — it may legitimately outlive any run.
    foreign = _workspace(root, "session-abc", pid=None)

    assert sweep_orphaned_workspaces(root) == 1
    assert not orphan.exists()
    assert live.exists()
    assert foreign.exists()


def test_sweep_keeps_an_idle_live_run(tmp_path):
    # The case timestamps got wrong twice: an agent can think for hours — or
    # days — writing nothing at all, and its workspace must survive that. Asking
    # the owner whether it is alive is immune to how quiet it has been; only the
    # pid-reuse backstop (a separate test) is time-based at all.
    root = tmp_path / "workspaces"
    thinking = _workspace(root, "run-thinking", pid=os.getpid(), age=3 * 24 * 3600)
    stale = time.time() - 30 * 24 * 3600
    os.utime(thinking, (stale, stale))  # nothing written for a month
    os.utime(thinking / "control", (stale, stale))

    assert sweep_orphaned_workspaces(root) == 0
    assert thinking.exists()


def test_sweep_reclaims_a_reused_pid_after_the_backstop(tmp_path):
    # A dead owner whose pid was recycled by an unrelated process would be
    # protected forever, so a live pid stops counting once nothing has touched
    # the workspace for the backstop window.
    root = tmp_path / "workspaces"
    leaked = _workspace(root, "run-reused", pid=os.getpid(), age=14 * 24 * 3600)

    assert sweep_orphaned_workspaces(root, older_than=7 * 24 * 3600) == 1
    assert not leaked.exists()


def test_sweep_falls_back_to_age_for_a_sentinel_without_a_pid(tmp_path):
    # Written by an earlier version: age is all there is to go on.
    root = tmp_path / "workspaces"
    old = root / "run-legacy"
    (old / "control").mkdir(parents=True)
    marker = old / W._RUN_SENTINEL
    marker.write_text("")
    stale = time.time() - 14 * 24 * 3600
    os.utime(marker, (stale, stale))
    recent = _workspace(root, "run-recent", pid=None)
    (recent / W._RUN_SENTINEL).write_text("")

    assert sweep_orphaned_workspaces(root) == 1
    assert not old.exists()
    assert recent.exists()


def test_sweep_never_reclaims_a_retained_recovery_workspace(tmp_path, git_repo):
    snapshot = W.capture_repository_snapshot(
        git_repo, "run-retained", tmp_path / "workspaces", sweepable=True
    )
    pool = WorktreePool(git_repo, "abcdef12", snapshot=snapshot)
    assert (snapshot.run_root / W._RUN_SENTINEL).is_file()

    pool.retain()  # the user's to inspect — nothing may ever reclaim it
    assert (snapshot.run_root / W._RECOVERY_MARKER).is_file()
    # Even with a dead owner and the backstop fully elapsed.
    (snapshot.run_root / W._RUN_SENTINEL).write_text(f"{_DEAD_PID}\n")
    assert sweep_orphaned_workspaces(tmp_path / "workspaces", older_than=-1) == 0
    assert snapshot.run_root.exists()


def test_failed_acquire_never_reuses_its_branch_and_path(tmp_path, git_repo, monkeypatch):
    # The slot is claimed BEFORE the git worktree add: a failed add used to
    # leave _next_slot unadvanced, so a retried acquire recomputed the SAME
    # branch/path — guaranteed to fail again against the first attempt's
    # partial leftovers. A retry must land on a fresh slot.
    snapshot = W.capture_repository_snapshot(
        git_repo, "run-slot", tmp_path / "workspaces", sweepable=True
    )
    pool = WorktreePool(git_repo, "abcdef12", snapshot=snapshot)
    real_run = W._run_git
    fail_once = {"armed": True}
    attempted: list[str] = []

    def flaky(argv, **kwargs):
        if argv[:3] == ["git", "worktree", "add"]:
            attempted.append(argv[4])  # the -b branch name
            if fail_once["armed"]:
                fail_once["armed"] = False
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="fail")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(W, "_run_git", flaky)
    with pytest.raises(W.WorkspaceError):
        pool.acquire("claude")
    worktree = pool.acquire("claude")  # the retry
    assert worktree.path.is_dir()
    assert len(attempted) == 2 and attempted[0] != attempted[1]


def test_snapshot_claims_the_workspace_before_the_clone(tmp_path, git_repo, monkeypatch):
    # An interrupt during the clone (the slow part) must still leave a directory
    # the sweep can recognise — an unmarked leftover is one it can never reclaim.
    seen: dict = {}
    real_run = W._run_git

    def _capture(argv, **kwargs):
        if argv[:2] == ["git", "clone"]:
            run_root = tmp_path / "workspaces" / "run-x"
            seen["marked_before_clone"] = (run_root / W._RUN_SENTINEL).is_file()
        return real_run(argv, **kwargs)

    monkeypatch.setattr(W, "_run_git", _capture)
    W.capture_repository_snapshot(
        git_repo, "run-x", tmp_path / "workspaces", sweepable=True
    )
    assert seen["marked_before_clone"] is True


def test_snapshot_does_not_claim_room_or_session_workspaces(tmp_path, git_repo):
    # Their lifecycles are tracked externally and may outlive any run.
    snapshot = W.capture_repository_snapshot(
        git_repo, "session-abc", tmp_path / "workspaces"
    )
    assert not (snapshot.run_root / W._RUN_SENTINEL).exists()
    assert sweep_orphaned_workspaces(tmp_path / "workspaces", older_than=-1) == 0


def test_sweep_is_best_effort_on_a_missing_root(tmp_path):
    assert sweep_orphaned_workspaces(tmp_path / "nope") == 0




# ── _run_git hang bound (P0-8) ─────────────────────────────────────────────


def test_run_git_timeout_degrades_to_failed_completedprocess():
    import sys

    import syncmill.worktree as W

    proc = W._run_git(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        capture_output=True, text=True, timeout=0.2,
    )
    assert proc.returncode == 124  # every rc!=0 call site handles this
    assert "timed out" in proc.stderr


def test_run_git_timeout_raises_for_check_callers():
    import subprocess as sp
    import sys

    import pytest

    import syncmill.worktree as W

    with pytest.raises(sp.CalledProcessError):
        W._run_git(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            capture_output=True, text=True, check=True, timeout=0.2,
        )


# ── snapshot index seeding (P1-1) ──────────────────────────────────────────


def _snapshot_tree(repo, snapshot):
    import subprocess as sp

    out = sp.run(
        ["git", "cat-file", "-p", snapshot.commit],
        cwd=str(snapshot.control_repo), capture_output=True, text=True,
    )
    assert out.returncode == 0
    return out.stdout.splitlines()[0]  # "tree <sha>"


def test_seeded_and_unseeded_snapshots_capture_the_same_tree(
    tmp_path, git_repo, monkeypatch
):
    # The seed is a pure fast path: `git add -A` syncs the index to the exact
    # worktree state from EITHER starting point, so the snapshot tree must be
    # byte-identical with and without it.
    (git_repo / "README.md").write_text("# dirty edit\n")
    (git_repo / "untracked.txt").write_text("new file\n")

    seeded = W.capture_repository_snapshot(
        git_repo, "run-seeded", tmp_path / "ws-a", sweepable=True
    )
    monkeypatch.setattr(W, "_seed_snapshot_index", lambda *a, **k: False)
    unseeded = W.capture_repository_snapshot(
        git_repo, "run-unseeded", tmp_path / "ws-b", sweepable=True
    )
    assert _snapshot_tree(git_repo, seeded) == _snapshot_tree(git_repo, unseeded)


def test_snapshot_uses_the_seeded_fast_path_by_default(tmp_path, git_repo):
    calls = []
    real = W._seed_snapshot_index

    def spy(*a, **k):
        r = real(*a, **k)
        calls.append(r)
        return r

    W._seed_snapshot_index, orig = spy, W._seed_snapshot_index
    try:
        W.capture_repository_snapshot(
            git_repo, "run-fast", tmp_path / "ws", sweepable=True
        )
    finally:
        W._seed_snapshot_index = orig
    assert calls == [True]  # the live index seeded successfully


def test_corrupt_seed_falls_back_to_read_tree(tmp_path, git_repo, monkeypatch):
    # An incompatible seed (split index, torn copy) fails the git command; the
    # capture must rebuild unseeded rather than fail the run.
    def bad_seed(source, index, env):
        index.write_bytes(b"not a git index")
        return True

    monkeypatch.setattr(W, "_seed_snapshot_index", bad_seed)
    (git_repo / "README.md").write_text("# changed\n")
    snapshot = W.capture_repository_snapshot(
        git_repo, "run-corrupt-seed", tmp_path / "ws", sweepable=True
    )
    assert snapshot.commit  # capture survived on the fallback path


def test_snapshot_cleanup_all_skips_per_worktree_git(tmp_path, git_repo, monkeypatch):
    # Everything a snapshot pool creates lives inside run_root; cleanup must be
    # ONE rmtree, not 2 git subprocesses per worktree that delete files the
    # rmtree removes anyway.
    snapshot = W.capture_repository_snapshot(
        git_repo, "run-clean-fast", tmp_path / "ws", sweepable=True
    )
    pool = WorktreePool(git_repo, "abcdef12", snapshot=snapshot)
    pool.acquire("claude")
    called = []
    monkeypatch.setattr(W, "cleanup_worktree", lambda *a: called.append(a) or True)
    assert pool.cleanup_all() == 1
    assert called == []  # no per-worktree git path taken
    assert not snapshot.run_root.exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_suppressive_index_flags_refuse_the_seed(tmp_path, git_repo, flag):
    # A copied live index carries assume-unchanged/skip-worktree flags, and a
    # seeded `git add -A` then TRUSTS the index over the worktree — silently
    # snapshotting stale content for a genuinely edited file (codex round-1
    # blocker, reproduced). Such repos must take the read-tree path.
    import subprocess as sp

    sp.run(["git", "update-index", flag, "README.md"], cwd=str(git_repo), check=True)
    (git_repo / "README.md").write_text("# edited under a suppressive flag\n")
    snapshot = W.capture_repository_snapshot(
        git_repo, f"run-flag{flag[2:3]}", tmp_path / "ws", sweepable=True
    )
    shown = sp.run(
        ["git", "show", f"{snapshot.commit}:README.md"],
        cwd=str(snapshot.control_repo), capture_output=True, text=True,
    )
    assert shown.stdout == "# edited under a suppressive flag\n"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")
def test_run_git_timeout_kills_gits_descendants(tmp_path):
    # subprocess.run(timeout=...) terminates only the direct child, so a
    # clean/smudge/LFS filter or fsmonitor daemon git spawned survived the
    # timeout as an orphan (codex round-4 major). _run_git launches the command
    # in its own group and kills the whole tree.
    import signal
    import sys

    pidfile = tmp_path / "gpid"
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(600)\n"
    )
    proc = W._run_git([sys.executable, "-c", code], timeout=5, text=True)
    assert proc.returncode == 124
    gpid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            return  # the descendant died with the group
        time.sleep(0.05)
    os.kill(gpid, signal.SIGKILL)  # cleanup before failing
    pytest.fail("git descendant survived the group kill")


def test_run_git_never_waits_unbounded_on_an_unreapable_child(monkeypatch):
    # Popen.__exit__ ends with an unbounded wait(); the runner must give up
    # bounded on both the timeout and the interrupt path (the _sync_run rule).
    from test_agents import _UnreapableProc

    monkeypatch.setattr(
        W.subprocess, "Popen",
        lambda *a, **k: _UnreapableProc(subprocess.TimeoutExpired("cmd", 1)),
    )
    proc = W._run_git(["git", "status"], timeout=1, capture_output=True, text=True)
    assert proc.returncode == 124  # degraded, not hung

    monkeypatch.setattr(
        W.subprocess, "Popen", lambda *a, **k: _UnreapableProc(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        W._run_git(["git", "status"], timeout=1, capture_output=True, text=True)
