from __future__ import annotations

import json
import multiprocessing
import os
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

import syncmill.artifacts as artifacts_module
from syncmill.agents import KimiCodeAgent, provider_session_scope
from syncmill.artifacts import ArtifactError, ArtifactRecoveryRequired, ArtifactStore
from syncmill.cli import cli
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.worktree import (
    WorkspaceError,
    WorktreePool,
    capture_repository_snapshot,
    repository_fingerprint,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "tracked.txt").write_text("base\n")
    (path / ".gitignore").write_text("ignored.txt\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "base")
    return path


_NEW_FILE_PATCH = """diff --git a/winner.txt b/winner.txt
new file mode 100644
--- /dev/null
+++ b/winner.txt
@@ -0,0 +1 @@
+winner
"""


def _pending_artifact(
    tmp_path: Path,
    repo: Path,
    run_id: str = "run-1",
    *,
    artifact_root: Path | None = None,
    project_id: str = "project-1",
):
    store = ArtifactStore(artifact_root or tmp_path / "artifacts", project_id)
    task = Task(prompt="make winner")
    fingerprint = repository_fingerprint(repo)
    store.begin(
        run_id=run_id,
        task=task,
        repo_root=repo,
        snapshot_commit=_git(repo, "rev-parse", "HEAD"),
        source_fingerprint=fingerprint,
        strategy="route",
        agents=["codex"],
        plan={},
    )
    result = TaskResult(
        task_id=task.id,
        agent_id="codex",
        status=TaskStatus.COMPLETED,
        output="done",
        exit_code=0,
        duration_seconds=0.1,
        files_changed=["winner.txt"],
        patch=_NEW_FILE_PATCH,
    )
    artifact = store.save(
        run_id=run_id,
        task=task,
        result=result,
        repo_root=repo,
        snapshot_commit=_git(repo, "rev-parse", "HEAD"),
        source_fingerprint=fingerprint,
        strategy="route",
        plan={},
    )
    return store, artifact


def _concurrent_apply_worker(
    artifact_root: str,
    repo_path: str,
    *,
    project_id: str = "project-1",
    run_id: str = "run-1",
    pause_after_fingerprint: bool,
    paused,
    release,
    started,
    done,
    results,
) -> None:
    """Process target that makes the pre-lock stale-manifest race deterministic."""
    repo = Path(repo_path)
    store = ArtifactStore(Path(artifact_root), project_id)
    fingerprint = repository_fingerprint(repo)
    if pause_after_fingerprint:
        real_fingerprint = artifacts_module.repository_fingerprint
        calls = 0

        def stale_first_fingerprint(path: Path) -> str:
            nonlocal calls
            calls += 1
            value = real_fingerprint(path)
            if calls == 1:
                paused.set()
                if not release.wait(15):
                    raise RuntimeError("concurrent apply test release timed out")
            return value

        artifacts_module.repository_fingerprint = stale_first_fingerprint
    else:
        started.set()

    try:
        receipt = store.apply(run_id, repo, fingerprint)
        results.put(("ok", receipt.name))
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        done.set()


_REAL_POSIX_FORK = (
    os.name == "posix"
    and hasattr(os, "fork")
    and hasattr(os, "register_at_fork")
    and artifacts_module.fcntl is not None
)


def _read_fork_pipe(fd: int, timeout: float = 10.0) -> bytes:
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        raise TimeoutError("forked apply-lock child did not report in time")
    return os.read(fd, 4096)


def _wait_for_forked_child(pid: int, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return status
        time.sleep(0.01)
    raise TimeoutError("forked apply-lock child did not exit in time")


def _terminate_forked_child(pid: int) -> None:
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return
    if waited == pid:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _forked_apply_lock_contender(
    store: ArtifactStore,
    repo: Path,
    started_fd: int,
    result_fd: int,
) -> int:
    try:
        os.write(started_fd, b"started")
        with store.apply_lock(repo):
            os.write(result_fd, b"acquired")
    except BaseException as exc:
        message = f"error:{type(exc).__name__}:{exc}".encode(errors="replace")
        try:
            os.write(result_fd, message[:4096])
        except OSError:
            pass
        return 1
    return 0


def _report_fork_child_error(result_fd: int, exc: BaseException) -> None:
    message = f"error:{type(exc).__name__}:{exc}".encode(errors="replace")
    try:
        os.write(result_fd, message[:4096])
    except OSError:
        pass


def _fork_from_apply_lock_owner_worker(
    artifact_root: str,
    repo_path: str,
    results,
) -> None:
    """Exercise a child that continues through its inherited outer context."""
    store = ArtifactStore(Path(artifact_root), "fork-owner")
    repo = Path(repo_path)
    started_read, started_write = os.pipe()
    result_read, result_write = os.pipe()
    pid: int | None = None
    child = False
    try:
        try:
            with store.apply_lock(repo):
                pid = os.fork()
                if pid == 0:  # pragma: no cover - assertions run in parent
                    child = True
                    _close_fd(started_read)
                    started_read = -1
                    _close_fd(result_read)
                    result_read = -1
                    child_code = _forked_apply_lock_contender(
                        store, repo, started_write, result_write
                    )
                else:
                    _close_fd(started_write)
                    started_write = -1
                    _close_fd(result_write)
                    result_write = -1
                    started = _read_fork_pipe(started_read)
                    acquired_before_release = bool(
                        select.select([result_read], [], [], 0.5)[0]
                    )
        except BaseException as exc:
            if child:  # includes inherited outer-context cleanup failures
                _report_fork_child_error(result_write, exc)
                os._exit(1)
            raise

        if child:  # pragma: no cover - assertions run in parent
            _close_fd(started_write)
            _close_fd(result_write)
            os._exit(child_code)

        assert pid is not None and pid > 0
        payload = _read_fork_pipe(result_read)
        status = _wait_for_forked_child(pid)
        pid = None
        results.put(
            (
                "ok",
                started,
                acquired_before_release,
                payload,
                os.waitstatus_to_exitcode(status),
            )
        )
    except BaseException as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        if not child:
            for fd in (started_read, started_write, result_read, result_write):
                _close_fd(fd)
            if pid is not None and pid > 0:
                _terminate_forked_child(pid)


def _fork_from_non_owner_thread_worker(
    artifact_root: str,
    repo_path: str,
    results,
) -> None:
    """Exercise a child inheriting an RLock owned by a vanished thread."""
    store = ArtifactStore(Path(artifact_root), "fork-non-owner")
    repo = Path(repo_path)
    lock_held = threading.Event()
    release_lock = threading.Event()
    holder_errors: list[str] = []

    def hold_apply_lock() -> None:
        try:
            with store.apply_lock(repo):
                lock_held.set()
                if not release_lock.wait(10):
                    raise TimeoutError("parent holder was not released")
        except BaseException as exc:
            holder_errors.append(f"{type(exc).__name__}:{exc}")

    holder = threading.Thread(target=hold_apply_lock, daemon=True)
    holder.start()
    started_read, started_write = os.pipe()
    result_read, result_write = os.pipe()
    pid: int | None = None
    child = False
    try:
        if not lock_held.wait(5):
            raise TimeoutError("background thread did not acquire apply lock")
        pid = os.fork()
        if pid == 0:  # pragma: no cover - assertions run in parent
            child = True
            _close_fd(started_read)
            _close_fd(result_read)
            child_code = _forked_apply_lock_contender(
                store, repo, started_write, result_write
            )
            _close_fd(started_write)
            _close_fd(result_write)
            os._exit(child_code)

        _close_fd(started_write)
        started_write = -1
        _close_fd(result_write)
        result_write = -1
        started = _read_fork_pipe(started_read)
        acquired_before_release = bool(
            select.select([result_read], [], [], 0.5)[0]
        )
        release_lock.set()
        holder.join(5)
        if holder.is_alive():
            raise TimeoutError("background apply-lock holder did not exit")
        payload = _read_fork_pipe(result_read)
        status = _wait_for_forked_child(pid)
        pid = None
        results.put(
            (
                "ok",
                started,
                acquired_before_release,
                payload,
                os.waitstatus_to_exitcode(status),
                holder_errors,
            )
        )
    except BaseException as exc:
        if child:  # pragma: no cover - child reports through its exit status
            _report_fork_child_error(result_write, exc)
            os._exit(1)
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        if not child:
            release_lock.set()
            holder.join(5)
            for fd in (started_read, started_write, result_read, result_write):
                _close_fd(fd)
            if pid is not None and pid > 0:
                _terminate_forked_child(pid)


def _run_isolated_fork_scenario(target, tmp_path: Path, repo: Path):
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=target,
        args=(str(tmp_path / "fork-artifacts"), str(repo), results),
    )
    process.start()
    process.join(20)
    if process.is_alive():
        process.terminate()
        process.join(5)
        pytest.fail("isolated fork apply-lock scenario timed out")
    assert process.exitcode == 0
    return results.get(timeout=5)


def test_snapshot_captures_dirty_and_untracked_without_touching_source(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("dirty\n")
    (repo / "new.txt").write_text("untracked\n")
    (repo / "ignored.txt").write_text("secret\n")
    before_status = _git(repo, "status", "--porcelain")
    before_index = _git(repo, "write-tree")

    snapshot = capture_repository_snapshot(repo, "run-1", tmp_path / "workspaces")
    pool = WorktreePool(repo, "task-1", snapshot=snapshot)
    try:
        worktree = pool.acquire("codex")
        assert (worktree.path / "tracked.txt").read_text() == "dirty\n"
        assert (worktree.path / "new.txt").read_text() == "untracked\n"
        assert not (worktree.path / "ignored.txt").exists()
        assert _git(repo, "status", "--porcelain") == before_status
        assert _git(repo, "write-tree") == before_index
    finally:
        pool.cleanup_all()
    assert not snapshot.run_root.exists()


def test_snapshot_blocks_unborn_sparse_and_rebase_states(tmp_path: Path):
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q")
    with pytest.raises(WorkspaceError, match="no initial commit"):
        capture_repository_snapshot(unborn, "unborn", tmp_path / "ws-unborn")

    sparse = _repo(tmp_path / "sparse")
    _git(sparse, "config", "core.sparseCheckout", "true")
    with pytest.raises(WorkspaceError, match="sparse checkouts"):
        capture_repository_snapshot(sparse, "sparse", tmp_path / "ws-sparse")

    rebasing = _repo(tmp_path / "rebasing")
    git_dir = Path(_git(rebasing, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = rebasing / git_dir
    (git_dir / "rebase-merge").mkdir()
    with pytest.raises(WorkspaceError, match="rebase-merge"):
        capture_repository_snapshot(rebasing, "rebase", tmp_path / "ws-rebase")


def test_snapshot_blocks_unresolved_merge(tmp_path: Path):
    repo = _repo(tmp_path / "merge")
    base_branch = _git(repo, "branch", "--show-current")
    _git(repo, "switch", "-c", "feature")
    (repo / "tracked.txt").write_text("feature\n")
    _git(repo, "commit", "-am", "feature", "-q")
    _git(repo, "switch", base_branch)
    (repo / "tracked.txt").write_text("base branch\n")
    _git(repo, "commit", "-am", "base branch", "-q")
    merge = subprocess.run(
        ["git", "merge", "feature"], cwd=repo, capture_output=True, text=True
    )
    assert merge.returncode != 0
    with pytest.raises(WorkspaceError, match="unresolved merge conflicts"):
        capture_repository_snapshot(repo, "merge", tmp_path / "ws-merge")


def test_snapshot_blocks_dirty_submodule(tmp_path: Path):
    child = _repo(tmp_path / "child")
    parent = _repo(tmp_path / "parent")
    _git(
        parent,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        str(child),
        "sub",
    )
    _git(parent, "commit", "-am", "add submodule", "-q")
    (parent / "sub" / "tracked.txt").write_text("dirty submodule\n")
    with pytest.raises(WorkspaceError, match="submodules must be initialized and clean"):
        capture_repository_snapshot(parent, "submodule", tmp_path / "ws-submodule")


def test_apply_rejects_fingerprint_and_digest_mismatches(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    (repo / "tracked.txt").write_text("changed after run\n")
    with pytest.raises(ArtifactError, match="working tree changed"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    assert not (repo / "winner.txt").exists()

    (repo / "tracked.txt").write_text("base\n")
    (artifact.path / "winner.patch").write_text(_NEW_FILE_PATCH + "# corrupt\n")
    with pytest.raises(ArtifactError, match="patch digest mismatch"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    assert not (repo / "winner.txt").exists()


def test_apply_verify_passes_in_isolation_then_applies_source(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo)
    command = (
        f'{sys.executable} -c "from pathlib import Path; '
        "assert Path('winner.txt').read_text() == 'winner\\n'\""
    )
    receipt = store.apply(
        "run-1",
        repo,
        repository_fingerprint(repo),
        verify_commands=[command],
    )
    assert receipt.exists()
    assert (repo / "winner.txt").read_text() == "winner\n"
    assert store.load("run-1").manifest["state"] == "applied"


def test_apply_verify_failure_never_mutates_source(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo)
    before = _git(repo, "status", "--porcelain")
    command = (
        f'{sys.executable} -c "from pathlib import Path; '
        "Path('verify-side-effect.txt').write_text('x'); raise SystemExit(1)\""
    )
    with pytest.raises(ArtifactError, match="isolated verification failed"):
        store.apply(
            "run-1",
            repo,
            repository_fingerprint(repo),
            verify_commands=[command],
        )
    assert _git(repo, "status", "--porcelain") == before
    assert not (repo / "winner.txt").exists()
    assert not (repo / "verify-side-effect.txt").exists()
    assert store.load("run-1").manifest["state"] == "pending"


@pytest.mark.parametrize(
    "separate_stores",
    [False, True],
    ids=["same-run", "same-repo-different-stores"],
)
def test_concurrent_cross_process_applies_never_restore_stale_pending(
    tmp_path: Path, separate_stores: bool
):
    repo = _repo(tmp_path / "repo")
    if separate_stores:
        first_store, first_artifact = _pending_artifact(
            tmp_path,
            repo,
            "run-a",
            artifact_root=tmp_path / "artifacts-a",
            project_id="project-a",
        )
        second_store, second_artifact = _pending_artifact(
            tmp_path,
            repo,
            "run-b",
            artifact_root=tmp_path / "artifacts-b",
            project_id="project-b",
        )
        first_identity = {
            "artifact_root": str(tmp_path / "artifacts-a"),
            "project_id": "project-a",
            "run_id": "run-a",
        }
        second_identity = {
            "artifact_root": str(tmp_path / "artifacts-b"),
            "project_id": "project-b",
            "run_id": "run-b",
        }
    else:
        first_store, first_artifact = _pending_artifact(tmp_path, repo)
        second_store, second_artifact = first_store, first_artifact
        first_identity = second_identity = {
            "artifact_root": str(tmp_path / "artifacts"),
            "project_id": "project-1",
            "run_id": "run-1",
        }
    context = multiprocessing.get_context("spawn")
    paused = context.Event()
    release = context.Event()
    first_started = context.Event()
    second_started = context.Event()
    first_done = context.Event()
    second_done = context.Event()
    results = context.Queue()
    common = {
        "repo_path": str(repo),
        "paused": paused,
        "release": release,
        "results": results,
    }
    first = context.Process(
        target=_concurrent_apply_worker,
        kwargs={
            **common,
            **first_identity,
            "pause_after_fingerprint": True,
            "started": first_started,
            "done": first_done,
        },
    )
    second = context.Process(
        target=_concurrent_apply_worker,
        kwargs={
            **common,
            **second_identity,
            "pause_after_fingerprint": False,
            "started": second_started,
            "done": second_done,
        },
    )
    processes: list[multiprocessing.Process] = []
    second_finished_while_first_paused = False
    try:
        first.start()
        processes.append(first)
        assert paused.wait(10), "first apply did not reach the controlled pause"
        second.start()
        processes.append(second)
        assert second_started.wait(10), "second apply did not start"
        second_finished_while_first_paused = second_done.wait(1)
    finally:
        release.set()
        for process in processes:
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)

    assert not second_finished_while_first_paused
    assert all(process.exitcode == 0 for process in processes)
    outcomes = [results.get(timeout=5), results.get(timeout=5)]
    assert sorted(outcome[0] for outcome in outcomes) == ["error", "ok"]
    error = next(outcome for outcome in outcomes if outcome[0] == "error")
    assert error[1] == "ArtifactError"
    assert (repo / "winner.txt").read_text() == "winner\n"
    assert first_store.load(first_identity["run_id"]).manifest["state"] == "applied"
    assert len(list((first_artifact.path / "applications").glob("*.json"))) == 1
    if separate_stores:
        assert second_store.load("run-b").manifest["state"] == "pending"
        assert not list((second_artifact.path / "applications").glob("*.json"))
    else:
        assert "'applied', not pending" in error[2]


@pytest.mark.skipif(
    not _REAL_POSIX_FORK,
    reason="requires POSIX fork and flock",
)
def test_apply_lock_resets_owner_state_and_descriptors_after_fork(tmp_path: Path):
    repo = _repo(tmp_path / "repo-owner-fork")

    outcome = _run_isolated_fork_scenario(
        _fork_from_apply_lock_owner_worker, tmp_path, repo
    )

    assert outcome == ("ok", b"started", False, b"acquired", 0)


@pytest.mark.skipif(
    not _REAL_POSIX_FORK,
    reason="requires POSIX fork and flock",
)
def test_apply_lock_resets_non_owner_thread_state_after_fork(tmp_path: Path):
    repo = _repo(tmp_path / "repo-non-owner-fork")

    outcome = _run_isolated_fork_scenario(
        _fork_from_non_owner_thread_worker, tmp_path, repo
    )

    assert outcome == ("ok", b"started", False, b"acquired", 0, [])


def test_repository_fingerprint_changes_when_clean_head_advances(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    before = repository_fingerprint(repo)
    (repo / "second.txt").write_text("second\n", encoding="utf-8")
    _git(repo, "add", "second.txt")
    _git(repo, "commit", "-qm", "second")
    assert _git(repo, "status", "--porcelain") == ""
    assert repository_fingerprint(repo) != before


def test_apply_receipt_failure_rolls_source_and_manifest_back(
    monkeypatch, tmp_path: Path
):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    real_atomic_write = artifacts_module._atomic_write

    def fail_receipt(path: Path, data: bytes) -> None:
        if path.parent.name == "applications":
            raise OSError("injected receipt failure")
        real_atomic_write(path, data)

    monkeypatch.setattr(artifacts_module, "_atomic_write", fail_receipt)
    with pytest.raises(ArtifactError, match="source patch was rolled back"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    assert not (repo / "winner.txt").exists()
    assert store.load("run-1").manifest["state"] == "pending"
    assert not list((artifact.path / "applications").glob("*.json"))


def test_apply_manifest_failure_removes_receipt_and_rolls_source_back(
    monkeypatch, tmp_path: Path
):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    real_write_manifest = store._write_manifest

    def fail_applied_manifest(run_dir: Path, manifest: dict) -> None:
        if manifest.get("state") == "applied":
            raise OSError("injected manifest failure")
        real_write_manifest(run_dir, manifest)

    monkeypatch.setattr(store, "_write_manifest", fail_applied_manifest)
    with pytest.raises(ArtifactError, match="source patch was rolled back"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    assert not (repo / "winner.txt").exists()
    assert store.load("run-1").manifest["state"] == "pending"
    assert not list((artifact.path / "applications").glob("*.json"))


def test_apply_rollback_failure_is_explicit_recovery_state(
    monkeypatch, tmp_path: Path
):
    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo)
    real_atomic_write = artifacts_module._atomic_write
    real_run = artifacts_module.subprocess.run

    def fail_receipt(path: Path, data: bytes) -> None:
        if path.parent.name == "applications":
            raise OSError("injected receipt failure")
        real_atomic_write(path, data)

    def fail_reverse(argv, **kwargs):
        if "--reverse" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "reverse failed")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(artifacts_module, "_atomic_write", fail_receipt)
    monkeypatch.setattr(artifacts_module.subprocess, "run", fail_reverse)
    with pytest.raises(ArtifactRecoveryRequired, match="manual recovery") as exc:
        store.apply("run-1", repo, repository_fingerprint(repo))
    assert exc.value.source_changed is True
    assert (repo / "winner.txt").exists()
    assert store.load("run-1").manifest["state"] == "applying"


def test_recovery_required_cas_never_overwrites_applied(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo)
    store.apply("run-1", repo, repository_fingerprint(repo))

    changed = store.recovery_required("run-1", "stale room recovery")

    assert changed is False
    manifest = store.load("run-1").manifest
    assert manifest["state"] == "applied"
    assert "recovery_reason" not in manifest


def test_artifact_order_prune_and_recovery_lifecycle(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts", "project-1")
    now = datetime.now(UTC)
    states = [
        ("old", "no_changes", now - timedelta(days=2)),
        ("middle", "failed", now - timedelta(days=1)),
        ("new", "pending", now),
    ]
    for run_id, state, created_at in states:
        path = store.root / run_id
        path.mkdir()
        manifest = {
            "run_id": run_id,
            "state": state,
            "created_at": created_at.isoformat(),
        }
        (path / "manifest.json").write_text(json.dumps(manifest))
    assert [artifact.run_id for artifact in store.list()] == ["new", "middle", "old"]
    assert store.prune(keep=1, dry_run=True) == ["middle", "old"]
    assert store.prune(keep=1) == ["middle", "old"]
    assert (store.root / "new").exists()

    store.recovery_required("new", "save failed")
    recovered = store.load("new").manifest
    assert recovered["state"] == "recovery_required"
    assert recovered["recovery_reason"] == "save failed"


def test_run_continue_and_explicit_apply_end_to_end(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "resumed = len(sys.argv) > 2 and sys.argv[1:3] == ['exec', 'resume']\n"
        "pathlib.Path('winner.txt').write_text('second\\n' if resumed else 'first\\n')\n"
        "sid = sys.argv[3] if resumed else '11111111-1111-4111-8111-111111111111'\n"
        "print(json.dumps({'type':'thread.started','thread_id':sid}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))\n"
    )
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SYNCMILL_AGENT_PRIORITY": '["codex"]',
        "SYNCMILL_WORKSPACE__ROOT": str(tmp_path / "workspaces"),
        "SYNCMILL_RUNS__ROOT": str(tmp_path / "runs"),
        "SYNCMILL_MEMORY__DB_PATH": str(tmp_path / "memory" / "memory.db"),
        "SYNCMILL_MEMORY__MEMORY_DIR": str(tmp_path / "memory"),
    }
    runner = CliRunner()
    first = runner.invoke(
        cli, ["run", "--repo", str(repo), "--agent", "codex", "make it"], env=env
    )
    assert first.exit_code == 0, first.output
    assert not (repo / "winner.txt").exists()
    first_run = next(
        line.split(": ", 1)[1]
        for line in first.output.splitlines()
        if line.startswith("[syncmill] run:")
    )

    continued = runner.invoke(
        cli,
        ["continue", first_run, "improve it", "--repo", str(repo)],
        env=env,
    )
    assert continued.exit_code == 0, continued.output
    second_run = next(
        line.split(": ", 1)[1]
        for line in continued.output.splitlines()
        if line.startswith("[syncmill] run:")
    )
    project_runs = next((tmp_path / "runs").glob("*/runs"))
    first_manifest = json.loads(
        (project_runs / first_run / "manifest.json").read_text()
    )
    second_manifest = json.loads(
        (project_runs / second_run / "manifest.json").read_text()
    )
    assert first_manifest["state"] == "superseded"
    assert second_manifest["parent_run_id"] == first_run
    assert second_manifest["provider_session_id"] == first_manifest["provider_session_id"]
    assert not (repo / "winner.txt").exists()

    applied = runner.invoke(
        cli, ["apply", second_run, "--repo", str(repo)], env=env
    )
    assert applied.exit_code == 0, applied.output
    assert (repo / "winner.txt").read_text() == "second\n"
    final_manifest = json.loads(
        (project_runs / second_run / "manifest.json").read_text()
    )
    assert final_manifest["state"] == "applied"
    assert list((project_runs / second_run / "applications").glob("*.json"))


def test_kimi_resume_hint_is_used_on_next_turn(tmp_path: Path, monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                '{"role":"assistant","content":"done"}\n'
                '{"role":"meta","type":"session.resume_hint",'
                '"content":"To resume: kimi -r kimi-session-1"}\n'
            ),
            stderr="",
        )

    monkeypatch.setattr("syncmill.agents.subprocess.run", fake_run)
    agent = KimiCodeAgent()
    with provider_session_scope():
        first = agent.execute("first", tmp_path, 10)
        second = agent.execute("second", tmp_path, 10)
    assert first.provider_session_id == "kimi-session-1"
    assert second.provider_session_id == "kimi-session-1"
    assert "--session" not in commands[0]
    assert commands[1][-2:] == ["--session", "kimi-session-1"]
