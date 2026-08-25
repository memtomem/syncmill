from __future__ import annotations

import json
import multiprocessing
import os
import select
import shutil
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


def test_final_fingerprint_error_restores_pending_before_source_mutation(
    monkeypatch, tmp_path: Path
):
    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo)
    expected = repository_fingerprint(repo)
    real_fingerprint = artifacts_module.repository_fingerprint
    calls = 0

    def fail_final_probe(path: Path) -> str:
        nonlocal calls
        calls += 1
        # On the no-verify path the FIRST fingerprint is now the pre-mutation
        # probe (the redundant 'apply preparation' pass was removed).
        if calls == 1:
            raise WorkspaceError("untracked file disappeared during scan")
        return real_fingerprint(path)

    monkeypatch.setattr(artifacts_module, "repository_fingerprint", fail_final_probe)

    with pytest.raises(WorkspaceError, match="disappeared"):
        store.apply("run-1", repo, expected)

    assert store.load("run-1").manifest["state"] == "pending"
    assert not (repo / "winner.txt").exists()


def test_git_apply_spawn_error_restores_pending_before_source_mutation(
    monkeypatch, tmp_path: Path
):
    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo)
    expected = repository_fingerprint(repo)
    real_run = artifacts_module.subprocess.run

    def fail_patch_spawn(argv, **kwargs):
        if argv[:3] == ["git", "apply", "--binary"]:
            raise OSError("git executable disappeared")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(artifacts_module.subprocess, "run", fail_patch_spawn)

    with pytest.raises(ArtifactError, match="could not start git apply"):
        store.apply("run-1", repo, expected)

    assert store.load("run-1").manifest["state"] == "pending"
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


# ---------------------------------------------------------------------------
# Standalone artifact-apply crash reconciler (Chunk C)
# ---------------------------------------------------------------------------


def _force_applying(
    store: ArtifactStore,
    artifact,
    *,
    owner: str | None,
    apply_id: str = "apply-1",
    repo: Path | None = None,
    mutate: bool = False,
    receipt: bool = False,
    receipt_apply_id: str | None = "apply-1",
    corrupt_receipt: bool = False,
) -> dict:
    """Hand-build an ``applying`` strand exactly as a crash would leave one."""
    manifest = dict(store.load(artifact.run_id).manifest)
    manifest["state"] = "applying"
    manifest["apply_id"] = apply_id
    manifest["apply_started_at"] = datetime.now(UTC).isoformat()
    if owner is not None:
        manifest["apply_owner"] = owner
    store._write_manifest(artifact.path, manifest)
    if mutate:
        assert repo is not None
        _git(repo, "apply", "--binary", str(artifact.path / "winner.patch"))
    if receipt:
        receipt_dir = artifact.path / "applications"
        receipt_dir.mkdir(mode=0o700, exist_ok=True)
        payload = {
            "applied_at": datetime.now(UTC).isoformat(),
            "repo_root": manifest["repo_root"],
            "patch_sha256": manifest["patch_sha256"],
            "run_id": artifact.run_id,
        }
        if receipt_apply_id is not None:
            payload["apply_id"] = receipt_apply_id
        (receipt_dir / "20260101T000000000000Z.json").write_text(json.dumps(payload))
    if corrupt_receipt:
        receipt_dir = artifact.path / "applications"
        receipt_dir.mkdir(mode=0o700, exist_ok=True)
        (receipt_dir / "20260101T000000000001Z.json").write_text("{not json")
    return manifest


def _kill_window_worker(
    *,
    artifact_root: str,
    repo_path: str,
    run_id: str,
    project_id: str,
    window: str,
    paused,
    release,
    results,
) -> None:
    """Run ``store.apply`` and block at one crash window so the parent can kill it."""
    import syncmill.artifacts as art

    repo = Path(repo_path)
    store = art.ArtifactStore(Path(artifact_root), project_id)
    fingerprint = art.repository_fingerprint(repo)

    def pause() -> None:
        paused.set()
        release.wait(30)

    if window == "before_mutation":
        real_run = art.subprocess.run

        def wrapped_run(argv, *a, **k):
            if (
                argv[:2] == ["git", "apply"]
                and "--binary" in argv
                and "--check" not in argv
            ):
                pause()
            return real_run(argv, *a, **k)

        art.subprocess.run = wrapped_run
    elif window == "after_mutation_before_receipt":
        real_write = art._atomic_write

        def wrapped_write(path, data):
            if path.parent.name == "applications":
                pause()
            return real_write(path, data)

        art._atomic_write = wrapped_write
    elif window == "after_receipt_before_applied":
        real_cas = art.ArtifactStore._compare_and_swap_manifest

        def wrapped_cas(self, run_dir, expected, replacement):
            if replacement.get("state") == "applied":
                pause()
            return real_cas(self, run_dir, expected, replacement)

        art.ArtifactStore._compare_and_swap_manifest = wrapped_cas
    elif window == "rollback_after_reverse":
        # Force the applied-flip to fail so the in-process rollback runs, then
        # block just after the source is reversed (receipt already invalidated
        # by the fixed ordering) so the parent kills before the pending restore.
        real_cas = art.ArtifactStore._compare_and_swap_manifest

        def failing_cas(self, run_dir, expected, replacement):
            if replacement.get("state") == "applied":
                raise RuntimeError("simulated bookkeeping failure")
            return real_cas(self, run_dir, expected, replacement)

        art.ArtifactStore._compare_and_swap_manifest = failing_cas
        real_run = art.subprocess.run

        def reverse_pausing_run(argv, *a, **k):
            is_reverse_binary = (
                argv[:2] == ["git", "apply"]
                and "--reverse" in argv
                and "--binary" in argv
                and "--check" not in argv
            )
            result = real_run(argv, *a, **k)
            if is_reverse_binary:
                pause()
            return result

        art.subprocess.run = reverse_pausing_run

    try:
        store.apply(run_id, repo, fingerprint)
        results.put(("returned",))
    except BaseException as exc:  # pragma: no cover - killed before returning
        results.put(("error", type(exc).__name__))


@pytest.mark.skipif(os.name != "posix", reason="kill injection needs POSIX signals")
@pytest.mark.parametrize(
    ("window", "outcome", "state"),
    [
        ("before_mutation", "not_applied", "pending"),
        ("after_mutation_before_receipt", "manual_recovery", "recovery_required"),
        ("after_receipt_before_applied", "completed", "applied"),
    ],
)
def test_reconcile_classifies_killed_apply_windows(
    tmp_path: Path, window: str, outcome: str, state: str
):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    context = multiprocessing.get_context("spawn")
    paused = context.Event()
    release = context.Event()
    results = context.Queue()
    proc = context.Process(
        target=_kill_window_worker,
        kwargs={
            "artifact_root": str(tmp_path / "artifacts"),
            "repo_path": str(repo),
            "run_id": "run-1",
            "project_id": "project-1",
            "window": window,
            "paused": paused,
            "release": release,
            "results": results,
        },
    )
    proc.start()
    try:
        assert paused.wait(20), "apply did not reach the target window"
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        # Reap before reconciling so the dead owner's PID is gone, not a zombie
        # that would read as still alive.
        proc.join(20)

    assert store.load("run-1").manifest["state"] == "applying"

    fresh = ArtifactStore(tmp_path / "artifacts", "project-1")
    reports = fresh.reconcile_interrupted_applies()
    assert [report.outcome for report in reports] == [outcome]
    assert store.load("run-1").manifest["state"] == state

    if window == "before_mutation":
        assert not (repo / "winner.txt").exists()
        assert (
            repository_fingerprint(repo)
            == store.load("run-1").manifest["source_fingerprint"]
        )
        # A restored run is applyable again.
        for key in ("apply_id", "apply_owner", "apply_started_at"):
            assert key not in store.load("run-1").manifest
    if window == "after_receipt_before_applied":
        assert (repo / "winner.txt").read_text() == "winner\n"
        assert reports[0].receipt is not None


@pytest.mark.skipif(os.name != "posix", reason="kill injection needs POSIX signals")
def test_reconcile_never_completes_a_rolled_back_apply_after_source_drift(
    tmp_path: Path,
):
    # A crash during the bookkeeping-failure rollback (source reversed) followed
    # by unrelated source drift must never be read as completed: the receipt is
    # invalidated before the reverse, so no bound receipt survives to claim it.
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    context = multiprocessing.get_context("spawn")
    paused = context.Event()
    release = context.Event()
    results = context.Queue()
    proc = context.Process(
        target=_kill_window_worker,
        kwargs={
            "artifact_root": str(tmp_path / "artifacts"),
            "repo_path": str(repo),
            "run_id": "run-1",
            "project_id": "project-1",
            "window": "rollback_after_reverse",
            "paused": paused,
            "release": release,
            "results": results,
        },
    )
    proc.start()
    try:
        assert paused.wait(20), "apply did not reach the rollback window"
        os.kill(proc.pid, signal.SIGKILL)
    finally:
        proc.join(20)

    # The source was reversed and no bound receipt was left behind.
    assert not (repo / "winner.txt").exists()
    assert not list((artifact.path / "applications").glob("*.json"))

    # Unrelated drift after the crash must not flip the verdict to completed.
    (repo / "unrelated.txt").write_text("later change\n")
    fresh = ArtifactStore(tmp_path / "artifacts", "project-1")
    reports = fresh.reconcile_interrupted_applies()
    assert [report.outcome for report in reports] == ["manual_recovery"]
    assert store.load("run-1").manifest["state"] == "recovery_required"
    assert not (repo / "winner.txt").exists()


def test_reconcile_skips_while_a_live_apply_holds_the_lock(tmp_path: Path):
    # Ownership is decided by the repository apply lock, not the recorded PID
    # (which a crash could see reused) — a held lock is a live apply.
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    _force_applying(store, artifact, owner=f"{os.getpid()}-live")

    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store.apply_lock(repo):
            acquired.set()
            release.wait(15)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert acquired.wait(10), "holder did not take the apply lock"
        started = time.monotonic()
        reports = store.reconcile_interrupted_applies()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        thread.join(15)

    assert [report.outcome for report in reports] == ["skipped_live_owner"]
    # The holder is UNKNOWABLE here (the busy lock may belong to an unrelated
    # apply, and the recorded owner's process may be gone), so the detail offers
    # the recorded owner as a hint and never names it as the holder.
    detail = reports[0].detail
    assert "the repository apply lock is held" in detail
    assert f"recorded apply owner: {os.getpid()}-live" in detail
    assert store.load("run-1").manifest["state"] == "applying"
    # Recovery must never block on a live holder: it returns promptly while the
    # holder is still sleeping on the lock.
    assert elapsed < 2.0


def test_nonblocking_apply_lock_raises_when_held_by_another_thread(tmp_path: Path):
    from syncmill.artifacts import ApplyLockBusy

    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo)
    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store.apply_lock(repo):
            acquired.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert acquired.wait(10)
        with pytest.raises(ApplyLockBusy):
            with store.apply_lock(repo, blocking=False):
                pass
    finally:
        release.set()
        thread.join(10)

    # Once free, the non-blocking acquire succeeds.
    with store.apply_lock(repo, blocking=False):
        pass


def test_recovery_required_nonblocking_raises_busy_then_succeeds(tmp_path: Path):
    # The Supervisor's crash path calls recovery_required(blocking=False): a live
    # lock holder must surface as ApplyLockBusy promptly, never a wait, and the
    # same call must succeed once the lock frees.
    from syncmill.artifacts import ApplyLockBusy

    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo)
    acquired = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store.apply_lock():
            acquired.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert acquired.wait(10)
        with pytest.raises(ApplyLockBusy):
            store.recovery_required("run-1", "interrupted", blocking=False)
        # The manifest was NOT mutated while busy.
        assert store.load("run-1").manifest["state"] != "recovery_required"
    finally:
        release.set()
        thread.join(10)

    assert store.recovery_required("run-1", "interrupted", blocking=False) is True
    assert store.load("run-1").manifest["state"] == "recovery_required"


def test_reconcile_reconciles_a_reused_pid_owner_when_the_lock_is_free(tmp_path: Path):
    # A dead worker whose PID was reused by an unrelated live process must not
    # strand the run forever: with the apply lock free, recovery proceeds.
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    _force_applying(store, artifact, owner=f"{os.getpid()}-reused")

    reports = store.reconcile_interrupted_applies()

    assert [report.outcome for report in reports] == ["not_applied"]
    assert store.load("run-1").manifest["state"] == "pending"


def test_reconcile_restores_a_dead_no_owner_strand(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    # A pre-Chunk-C strand carries no apply_owner; it cannot be proven live and
    # is reconciled as dead, exactly like the room reconciler treats it.
    _force_applying(store, artifact, owner=None)

    reports = store.reconcile_interrupted_applies()

    assert [report.outcome for report in reports] == ["not_applied"]
    assert store.load("run-1").manifest["state"] == "pending"


def test_reconcile_freezes_a_legacy_receipt_without_apply_id(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    _force_applying(
        store,
        artifact,
        owner=None,
        repo=repo,
        mutate=True,
        receipt=True,
        receipt_apply_id=None,
    )

    reports = store.reconcile_interrupted_applies()

    # An unbound receipt is never proof of completion (fail closed); a mutated
    # source with no bound receipt is undecidable.
    assert [report.outcome for report in reports] == ["manual_recovery"]
    assert store.load("run-1").manifest["state"] == "recovery_required"


def test_reconcile_freezes_a_bound_receipt_with_matching_fingerprint(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    # Bound receipt but the source still matches the pre-apply fingerprint:
    # contradictory evidence (e.g. an external revert), so freeze.
    _force_applying(store, artifact, owner=None, receipt=True)

    reports = store.reconcile_interrupted_applies()

    assert [report.outcome for report in reports] == ["manual_recovery"]
    assert store.load("run-1").manifest["state"] == "recovery_required"


def test_reconcile_ignores_a_corrupt_receipt(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    # An unparsable receipt file must not defeat a clean not-applied verdict.
    _force_applying(store, artifact, owner=None, corrupt_receipt=True)

    reports = store.reconcile_interrupted_applies()

    assert [report.outcome for report in reports] == ["not_applied"]
    assert store.load("run-1").manifest["state"] == "pending"


def test_reconcile_tolerates_a_non_object_receipt_file(tmp_path: Path):
    # Valid JSON that is not an object (e.g. "[]") must be skipped, never raise
    # AttributeError and abort the whole sweep.
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    _force_applying(store, artifact, owner=None)
    receipt_dir = artifact.path / "applications"
    receipt_dir.mkdir(mode=0o700, exist_ok=True)
    (receipt_dir / "20260101T000000000002Z.json").write_text("[]")

    reports = store.reconcile_interrupted_applies()

    assert [report.outcome for report in reports] == ["not_applied"]
    assert store.load("run-1").manifest["state"] == "pending"


def test_reconcile_tolerates_a_deeply_nested_receipt_json(tmp_path: Path):
    # json.loads raises RecursionError (a RuntimeError, not ValueError) on
    # pathologically nested input; it must be caught, not abort the sweep.
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    _force_applying(store, artifact, owner=None)
    receipt_dir = artifact.path / "applications"
    receipt_dir.mkdir(mode=0o700, exist_ok=True)
    nested = "[" * 20000 + "]" * 20000  # ~40 KB, under the size cap, over-deep
    (receipt_dir / "20260101T000000000004Z.json").write_text(nested)

    reports = store.reconcile_interrupted_applies()

    assert [report.outcome for report in reports] == ["not_applied"]
    assert store.load("run-1").manifest["state"] == "pending"


def test_reconcile_skips_an_oversized_receipt_without_parsing(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    _force_applying(store, artifact, owner=None)
    receipt_dir = artifact.path / "applications"
    receipt_dir.mkdir(mode=0o700, exist_ok=True)
    (receipt_dir / "20260101T000000000005Z.json").write_text(
        '{"x":"' + "a" * (128 * 1024) + '"}'
    )

    reports = store.reconcile_interrupted_applies()

    assert [report.outcome for report in reports] == ["not_applied"]
    assert store.load("run-1").manifest["state"] == "pending"


def test_reconcile_rejects_a_receipt_bound_to_another_run(tmp_path: Path):
    # A receipt whose run_id differs is another run's evidence and must not
    # certify this one as completed.
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    manifest = _force_applying(store, artifact, owner=None, repo=repo, mutate=True)
    receipt_dir = artifact.path / "applications"
    receipt_dir.mkdir(mode=0o700, exist_ok=True)
    (receipt_dir / "20260101T000000000003Z.json").write_text(
        json.dumps(
            {
                "apply_id": manifest["apply_id"],
                "patch_sha256": manifest["patch_sha256"],
                "run_id": "some-other-run",
            }
        )
    )

    reports = store.reconcile_interrupted_applies()

    # No bound receipt for this run + mutated source -> undecidable, not done.
    assert [report.outcome for report in reports] == ["manual_recovery"]
    assert store.load("run-1").manifest["state"] == "recovery_required"


@pytest.mark.parametrize("marker", ["strategy", "plan"])
def test_reconcile_skips_room_bound_runs_untouched(tmp_path: Path, marker: str):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    _force_applying(store, artifact, owner=None)
    manifest = dict(store.load("run-1").manifest)
    if marker == "strategy":
        manifest["strategy"] = "room"
    else:
        manifest["plan"] = {"origin": "room", "room_id": "room-1"}
    store._write_manifest(artifact.path, manifest)

    reports = store.reconcile_interrupted_applies()

    # Room-bound runs are the room store's authority; the standalone reconciler
    # must never act on them, or it would fight the room DB.
    assert [report.outcome for report in reports] == ["skipped_room_bound"]
    assert store.load("run-1").manifest["state"] == "applying"


def test_reconcile_reports_error_and_keeps_applying_when_repo_is_gone(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    _force_applying(store, artifact, owner=None)
    shutil.rmtree(repo)

    reports = store.reconcile_interrupted_applies()

    # An environment failure must not freeze the run (there is no un-freeze),
    # so it stays prune-protected in 'applying' for a later retry.
    assert [report.outcome for report in reports] == ["error"]
    assert store.load("run-1").manifest["state"] == "applying"


def test_reconcile_corrupt_manifest_yields_error_row_without_aborting_sweep(
    tmp_path: Path,
):
    # PR #78 review: a corrupt/hand-edited manifest (missing repo_root) raised
    # KeyError past the reconciler's except clause and aborted the WHOLE sweep.
    # It must yield that run's "error" row while the healthy strand in the same
    # sweep is still reconciled.
    repo = _repo(tmp_path / "repo")
    store, corrupt = _pending_artifact(tmp_path, repo, "run-1")
    _, healthy = _pending_artifact(tmp_path, repo, "run-2")
    _force_applying(store, corrupt, owner=None)
    _force_applying(store, healthy, owner=None)
    manifest = dict(store.load("run-1").manifest)
    del manifest["repo_root"]
    store._write_manifest(corrupt.path, manifest)

    outcomes = {
        report.run_id: report.outcome
        for report in store.reconcile_interrupted_applies()
    }

    assert outcomes == {"run-1": "error", "run-2": "not_applied"}
    # A non-string repo_root (TypeError from Path()) degrades the same way.
    manifest["repo_root"] = None
    store._write_manifest(corrupt.path, manifest)
    reports = {
        report.run_id: report for report in store.reconcile_interrupted_applies()
    }
    assert reports["run-1"].outcome == "error"
    assert "TypeError" in reports["run-1"].detail


def test_reconcile_sweep_surfaces_unreadable_manifests_as_error_rows(
    tmp_path: Path,
):
    # codex review of the PR #78 fixes: list() silently skips a corrupt
    # manifest, so a sweep over one used to print a false all-clear. The sweep
    # now enumerates run dirs directly and emits an "error" row for every
    # unreadable/corrupt run — including a non-object (list/scalar) manifest,
    # which load() now rejects instead of crashing later `.get` calls.
    repo = _repo(tmp_path / "repo")
    store, healthy = _pending_artifact(tmp_path, repo, "run-1")
    _force_applying(store, healthy, owner=None)
    bad_json = store.root / "run-bad-json"
    bad_json.mkdir()
    (bad_json / "manifest.json").write_text("{not json", encoding="utf-8")
    non_object = store.root / "run-non-object"
    non_object.mkdir()
    (non_object / "manifest.json").write_text('["applying"]', encoding="utf-8")
    # A dict manifest with NO state passes load() — it must be an error row,
    # not a silent skip (it could be a mangled applying strand).
    no_state = store.root / "run-no-state"
    no_state.mkdir()
    (no_state / "manifest.json").write_text("{}", encoding="utf-8")

    reports = store.reconcile_interrupted_applies()

    # Rows keep the sorted enumeration order — a corrupt run's error row sits
    # beside the healthy runs around it, not appended at either end.
    assert [(report.run_id, report.outcome) for report in reports] == [
        ("run-1", "not_applied"),
        ("run-bad-json", "error"),
        ("run-no-state", "error"),
        ("run-non-object", "error"),
    ]
    with pytest.raises(ArtifactError, match="unknown or corrupt run"):
        store.load("run-non-object")


def test_validate_applicable_rejects_hollow_manifests(tmp_path: Path):
    # codex R11: a dict manifest passing load() but missing the fields apply
    # needs (or whose patch fails its digest) must fail the detach preflight —
    # never launch a worker guaranteed to crash. And the worker itself now
    # fails a missing repo_root as a corrupt run, not a raw KeyError.
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo, "run-1")
    assert store.validate_applicable("run-1").run_id == "run-1"  # happy path
    manifest = dict(store.load("run-1").manifest)
    original_root = manifest.pop("repo_root")
    store._write_manifest(artifact.path, manifest)
    with pytest.raises(ArtifactError, match="missing repo_root"):
        store.validate_applicable("run-1")
    with pytest.raises(ArtifactError, match="missing repo_root"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    # A malformed path (embedded NUL) fails the preflight AND the worker path
    # as a corrupt run — never a raw ValueError after "detached job launched".
    manifest["repo_root"] = "/tmp/evil\0path"
    store._write_manifest(artifact.path, manifest)
    with pytest.raises(ArtifactError, match="invalid repo_root"):
        store.validate_applicable("run-1")
    with pytest.raises(ArtifactError, match="invalid repo_root"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    manifest["repo_root"] = original_root
    fingerprint = manifest.pop("source_fingerprint")
    store._write_manifest(artifact.path, manifest)
    with pytest.raises(ArtifactError, match="missing source_fingerprint"):
        store.validate_applicable("run-1")
    manifest["source_fingerprint"] = fingerprint
    store._write_manifest(artifact.path, manifest)
    (artifact.path / "winner.patch").unlink()
    with pytest.raises(ArtifactError, match="missing patch"):
        store.validate_applicable("run-1")


def test_apply_rejects_room_bound_runs_without_the_coordinator_seam(tmp_path: Path):
    # codex R9 blocker: a standalone apply of a room-bound run bypasses the
    # Room selection/revision lifecycle. Both room markers must be rejected;
    # only the RoomCoordinator-only seam (allow_room_bound=True, passed after
    # its selection CAS) may proceed.
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo, "run-1")
    manifest = dict(store.load("run-1").manifest)
    manifest["plan"] = {"origin": "room"}
    store._write_manifest(artifact.path, manifest)
    with pytest.raises(ArtifactError, match="room-bound"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    manifest["plan"] = {}
    manifest["strategy"] = "room"
    store._write_manifest(artifact.path, manifest)
    with pytest.raises(ArtifactError, match="room-bound"):
        store.apply("run-1", repo, repository_fingerprint(repo))
    assert store.load("run-1").manifest["state"] == "pending"  # untouched
    receipt = store.apply(
        "run-1", repo, repository_fingerprint(repo), allow_room_bound=True
    )
    assert receipt.exists()
    assert store.load("run-1").manifest["state"] == "applied"


def test_reconcile_apply_lock_runtime_error_is_contained(tmp_path: Path):
    # codex R9: a RuntimeError raised past the manifest repo_root resolution
    # (e.g. apply_lock's git-common-dir resolve hitting a symlink loop) must
    # become that run's error row, not abort the sweep.
    repo = _repo(tmp_path / "repo")
    store, first = _pending_artifact(tmp_path, repo, "run-1")
    _, second = _pending_artifact(tmp_path, repo, "run-2")
    _force_applying(store, first, owner=None)
    _force_applying(store, second, owner=None)
    real_lock = store.apply_lock
    calls = {"n": 0}

    def flaky_lock(repo_root, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Symlink loop from the git common dir")
        return real_lock(repo_root, **kwargs)

    store.apply_lock = flaky_lock  # instance override; run-1 hits the loop

    reports = store.reconcile_interrupted_applies()

    assert [(report.run_id, report.outcome) for report in reports] == [
        ("run-1", "error"),
        ("run-2", "not_applied"),
    ]
    assert "RuntimeError: Symlink loop" in reports[0].detail


def test_reconcile_symlink_loop_repo_root_is_an_error_row(tmp_path: Path):
    # Python 3.13 changed non-strict Path.resolve() on symlink loops from a
    # RuntimeError to a partially unresolved path.  Recovery must expose one
    # stable ArtifactError row on every supported Python while the healthy
    # strand in the same sweep still reconciles.
    repo = _repo(tmp_path / "repo")
    store, corrupt = _pending_artifact(tmp_path, repo, "run-1")
    _, healthy = _pending_artifact(tmp_path, repo, "run-2")
    _force_applying(store, corrupt, owner=None)
    _force_applying(store, healthy, owner=None)
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    manifest = dict(store.load("run-1").manifest)
    manifest["repo_root"] = str(loop_a)
    store._write_manifest(corrupt.path, manifest)

    reports = {
        report.run_id: report for report in store.reconcile_interrupted_applies()
    }

    assert reports["run-1"].outcome == "error"
    assert reports["run-1"].detail.startswith(
        "ArtifactError: could not resolve repository root:"
    )
    assert str(loop_a) in reports["run-1"].detail
    assert reports["run-2"].outcome == "not_applied"


def test_reconcile_targeted_missing_state_is_an_error_not_a_skip(tmp_path: Path):
    # Targeted recovery on a {} manifest must not report the benign
    # "skipped_not_applying" — an unrecognized state is corruption.
    repo = _repo(tmp_path / "repo")
    store, _ = _pending_artifact(tmp_path, repo, "run-1")
    empty = store.root / "run-empty"
    empty.mkdir()
    (empty / "manifest.json").write_text("{}", encoding="utf-8")

    reports = store.reconcile_interrupted_applies(run_id="run-empty")

    assert [(report.run_id, report.outcome) for report in reports] == [
        ("run-empty", "error")
    ]
    assert "unrecognized state" in reports[0].detail
    # A recognized non-applying state is still the benign targeted skip.
    scoped = store.reconcile_interrupted_applies(run_id="run-1")
    assert [report.outcome for report in scoped] == ["skipped_not_applying"]


def test_reconcile_targets_one_run_and_ignores_non_applying(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
    store, first = _pending_artifact(tmp_path, repo, "run-1")
    _, second = _pending_artifact(tmp_path, repo, "run-2")
    _force_applying(store, first, owner=None)
    _force_applying(store, second, owner=None)

    scoped = store.reconcile_interrupted_applies(run_id="run-1")
    assert [report.run_id for report in scoped] == ["run-1"]
    assert store.load("run-1").manifest["state"] == "pending"
    assert store.load("run-2").manifest["state"] == "applying"

    # A full sweep skips the untouched pending run and reconciles the rest.
    third_repo = _repo(tmp_path / "repo3")
    _pending_artifact(tmp_path, third_repo, "run-3")
    remaining = {
        report.run_id: report.outcome
        for report in store.reconcile_interrupted_applies()
    }
    assert remaining == {"run-2": "not_applied"}


def test_apply_records_owner_on_the_applying_manifest_and_binds_the_receipt(
    tmp_path: Path,
):
    repo = _repo(tmp_path / "repo")
    store, artifact = _pending_artifact(tmp_path, repo)
    captured: dict = {}
    real_cas = ArtifactStore._compare_and_swap_manifest

    def spy(self, run_dir, expected, replacement):
        if replacement.get("state") == "applying":
            captured["applying"] = dict(replacement)
        return real_cas(self, run_dir, expected, replacement)

    ArtifactStore._compare_and_swap_manifest = spy
    try:
        receipt = store.apply("run-1", repo, repository_fingerprint(repo))
    finally:
        ArtifactStore._compare_and_swap_manifest = real_cas

    owner = captured["applying"]["apply_owner"]
    assert owner.split("-", 1)[0] == str(os.getpid())
    payload = json.loads(receipt.read_text())
    assert payload["apply_id"] == captured["applying"]["apply_id"]
    assert payload["run_id"] == "run-1"


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
        "args = sys.argv[3:]\n"
        "while args and args[0] in ('-m', '-c'): args = args[2:]\n"
        "sid = args[0] if resumed else '11111111-1111-4111-8111-111111111111'\n"
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


def test_model_routing_selector_affinity_end_to_end(tmp_path: Path):
    """Explicit-tier run passes `-m` to the provider; `continue` reuses the
    STORED selector even after the config mapping changes (selector affinity),
    and the parent manifest carries the canonical plan.model_routing block."""
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.jsonl"
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"log = pathlib.Path({str(argv_log)!r})\n"
        "log.open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "resumed = len(sys.argv) > 2 and sys.argv[1:3] == ['exec', 'resume']\n"
        "pathlib.Path('winner.txt').write_text('second\\n' if resumed else 'first\\n')\n"
        "sid = '11111111-1111-4111-8111-111111111111'\n"
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
        "SYNCMILL_MODEL_ROUTING__MODELS": '{"low": {"codex": "tiny-model"}}',
    }
    runner = CliRunner()
    first = runner.invoke(
        cli,
        ["run", "--repo", str(repo), "--agent", "codex", "--difficulty", "low",
         "make it"],
        env=env,
    )
    assert first.exit_code == 0, first.output
    assert "[syncmill] model routing: low / explicit / applied" in first.output
    first_run = next(
        line.split(": ", 1)[1]
        for line in first.output.splitlines()
        if line.startswith("[syncmill] run:")
    )
    fresh_argv = json.loads(argv_log.read_text().splitlines()[0])
    # `-m` leads; the sandbox `-c` overrides sit between it and the prompt.
    assert fresh_argv[:3] == ["exec", "-m", "tiny-model"]
    assert "make it" in fresh_argv

    project_runs = next((tmp_path / "runs").glob("*/runs"))
    manifest = json.loads((project_runs / first_run / "manifest.json").read_text())
    block = manifest["plan"]["model_routing"]
    assert block["applied"] is True
    assert block["effective_selectors"] == {"codex": "tiny-model"}

    # The mapping CHANGES; continue must reuse the stored selector, not this.
    env["SYNCMILL_MODEL_ROUTING__MODELS"] = '{"low": {"codex": "other-model"}}'
    continued = runner.invoke(
        cli, ["continue", first_run, "improve it", "--repo", str(repo)], env=env
    )
    assert continued.exit_code == 0, continued.output
    assert "[syncmill] model routing: low / inherited / applied" in continued.output
    resume_argv = json.loads(argv_log.read_text().splitlines()[1])
    assert resume_argv[:4] == ["exec", "resume", "-m", "tiny-model"]


def test_model_routing_observe_run_resumes_without_flag(tmp_path: Path):
    """Observe parent: no model flag on the fresh run OR the continuation."""
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.jsonl"
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"log = pathlib.Path({str(argv_log)!r})\n"
        "log.open('a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "pathlib.Path('winner.txt').write_text('x\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'11111111-1111-4111-8111-111111111111'}))\n"
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
        "SYNCMILL_MODEL_ROUTING__MODE": "observe",
        "SYNCMILL_MODEL_ROUTING__MODELS": '{"medium": {"codex": "mid-model"}}',
    }
    runner = CliRunner()
    first = runner.invoke(
        cli, ["run", "--repo", str(repo), "--agent", "codex", "make it"], env=env
    )
    assert first.exit_code == 0, first.output
    assert "/ observe / not applied" in first.output
    first_run = next(
        line.split(": ", 1)[1]
        for line in first.output.splitlines()
        if line.startswith("[syncmill] run:")
    )
    continued = runner.invoke(
        cli, ["continue", first_run, "more", "--repo", str(repo)], env=env
    )
    assert continued.exit_code == 0, continued.output
    for line in argv_log.read_text().splitlines():
        assert "-m" not in json.loads(line)  # observe never passes a flag


def test_continue_corrupt_routing_metadata_fails_closed(tmp_path: Path):
    """A present-but-invalid plan container or null routing key on the parent
    manifest fails `continue` with `corrupt model routing metadata` — never a
    silent legacy downgrade to no-flag."""
    repo = _repo(tmp_path / "repo")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib\n"
        "pathlib.Path('winner.txt').write_text('x\\n')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'11111111-1111-4111-8111-111111111111'}))\n"
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
    first_run = next(
        line.split(": ", 1)[1]
        for line in first.output.splitlines()
        if line.startswith("[syncmill] run:")
    )
    manifest_path = next((tmp_path / "runs").glob("*/runs")) / first_run / "manifest.json"

    # Case 1: plan container is present but not a dict.
    manifest = json.loads(manifest_path.read_text())
    manifest["plan"] = "not-a-dict"
    manifest_path.write_text(json.dumps(manifest))
    res = runner.invoke(
        cli, ["continue", first_run, "more", "--repo", str(repo)], env=env
    )
    assert res.exit_code != 0
    assert "corrupt model routing metadata" in res.output

    # Case 2: routing key present but null.
    manifest = json.loads(manifest_path.read_text())
    manifest["plan"] = {"strategy": "route", "model_routing": None}
    manifest_path.write_text(json.dumps(manifest))
    res = runner.invoke(
        cli, ["continue", first_run, "more", "--repo", str(repo)], env=env
    )
    assert res.exit_code != 0
    assert "corrupt model routing metadata" in res.output

    # Case 3: routing key present but structurally invalid — the Supervisor's
    # strict inherit validation fails the run (a FAILED result, exit != 0).
    manifest = json.loads(manifest_path.read_text())
    manifest["plan"] = {"strategy": "route", "model_routing": {"schema_version": 99}}
    manifest_path.write_text(json.dumps(manifest))
    res = runner.invoke(
        cli, ["continue", first_run, "more", "--repo", str(repo)], env=env
    )
    assert res.exit_code != 0
    assert "corrupt model routing metadata" in res.output
