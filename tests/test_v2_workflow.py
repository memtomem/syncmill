from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
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


def _pending_artifact(tmp_path: Path, repo: Path, run_id: str = "run-1"):
    store = ArtifactStore(tmp_path / "artifacts", "project-1")
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


def test_artifact_order_prune_and_recovery_lifecycle(tmp_path: Path):
    repo = _repo(tmp_path / "repo")
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
