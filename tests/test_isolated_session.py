from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import syncmill.isolated_session as isolated
from syncmill.artifacts import ArtifactError, ArtifactStore
from syncmill.config import SyncmillConfig
from syncmill.isolated_session import IsolatedSessionError, IsolatedSessionStore
from syncmill.worktree import repository_fingerprint


def _config(tmp_path: Path) -> SyncmillConfig:
    return SyncmillConfig(
        project={"id": "session-tests"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
    )


def test_isolated_session_create_checkpoint_and_close(monkeypatch, git_repo, tmp_path):
    created: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        isolated,
        "_create_session",
        lambda name, agent, cwd: created.append((name, agent, cwd)),
    )
    monkeypatch.setattr(isolated, "session_exists", lambda name: False)
    store = IsolatedSessionStore(_config(tmp_path), git_repo)

    value = store.create("claude")
    assert created == [(value["tmux_name"], "claude", value["worktree_path"])]
    assert value["project_id"] == "session-tests"
    worktree = Path(value["worktree_path"])
    (worktree / "feature.txt").write_text("done\n", encoding="utf-8")

    run_id = store.checkpoint(value["session_id"])
    patch = (
        Path(store.config.runs.root)
        / store.project_id
        / "runs"
        / run_id
        / "winner.patch"
    ).read_text(encoding="utf-8")
    assert "feature.txt" in patch
    assert store.load(value["session_id"])["checkpoints"] == [run_id]

    closed = store.close_session(value["session_id"])
    assert closed["state"] == "closed"
    assert not worktree.exists()


def test_attach_verifies_project_worktree_cwd(monkeypatch, git_repo, tmp_path):
    monkeypatch.setattr(isolated, "_create_session", lambda *args: None)
    store = IsolatedSessionStore(_config(tmp_path), git_repo)
    value = store.create("codex")
    monkeypatch.setattr(isolated, "session_exists", lambda name: True)
    monkeypatch.setattr(
        isolated,
        "_tmux",
        lambda *args: SimpleNamespace(
            returncode=0, stdout=value["worktree_path"] + "\n", stderr=""
        ),
    )
    monkeypatch.setattr(isolated, "_attach_and_report", lambda name, agent: 0)
    assert store.attach(value["session_id"]) == 0


def test_list_marks_source_drift(monkeypatch, git_repo, tmp_path):
    monkeypatch.setattr(isolated, "_create_session", lambda *args: None)
    monkeypatch.setattr(isolated, "session_exists", lambda name: True)
    store = IsolatedSessionStore(_config(tmp_path), git_repo)
    store.create("kimi-code")
    (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    values = store.list()
    assert values[0]["source_stale"] is True


def test_tampered_cleanup_path_is_rejected(monkeypatch, git_repo, tmp_path):
    monkeypatch.setattr(isolated, "_create_session", lambda *args: None)
    store = IsolatedSessionStore(_config(tmp_path), git_repo)
    value = store.create("claude")
    metadata = store._path(value["session_id"])
    document = json.loads(metadata.read_text(encoding="utf-8"))
    document["run_root"] = str(tmp_path)
    metadata.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(IsolatedSessionError, match="unsafe paths"):
        store.close_session(value["session_id"])


def test_checkpoint_capture_preserves_real_git_index(monkeypatch, git_repo, tmp_path):
    monkeypatch.setattr(isolated, "_create_session", lambda *args: None)
    monkeypatch.setattr(isolated, "session_exists", lambda name: False)
    store = IsolatedSessionStore(_config(tmp_path), git_repo)
    value = store.create("claude")
    worktree = Path(value["worktree_path"])
    (worktree / "staged.txt").write_text("staged\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged.txt"], cwd=worktree, check=True)
    (worktree / "unstaged.txt").write_text("unstaged\n", encoding="utf-8")
    before_index = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=worktree,
        capture_output=True,
        check=True,
    ).stdout

    run_id = store.checkpoint(value["session_id"])
    after_index = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=worktree,
        capture_output=True,
        check=True,
    ).stdout
    patch = ArtifactStore(Path(store.config.runs.root), store.project_id).patch(run_id)
    assert before_index == after_index
    assert b"staged.txt" in patch
    assert b"unstaged.txt" in patch


def test_checkpoint_capture_failure_never_reports_success_or_loses_changes(
    monkeypatch, git_repo, tmp_path
):
    monkeypatch.setattr(isolated, "_create_session", lambda *args: None)
    monkeypatch.setattr(isolated, "session_exists", lambda name: False)
    store = IsolatedSessionStore(_config(tmp_path), git_repo)
    value = store.create("codex")
    worktree = Path(value["worktree_path"])
    (worktree / "important.txt").write_text("keep me\n", encoding="utf-8")
    monkeypatch.setattr(
        isolated,
        "_strict_checkpoint_patch",
        lambda *args: (_ for _ in ()).throw(IsolatedSessionError("capture failed")),
    )

    with pytest.raises(IsolatedSessionError, match="capture failed"):
        store.checkpoint(value["session_id"])
    assert (worktree / "important.txt").read_text() == "keep me\n"
    assert store.load(value["session_id"])["checkpoints"] == []


def test_clean_head_change_blocks_session_checkpoint_apply(
    monkeypatch, git_repo, tmp_path
):
    monkeypatch.setattr(isolated, "_create_session", lambda *args: None)
    monkeypatch.setattr(isolated, "session_exists", lambda name: False)
    store = IsolatedSessionStore(_config(tmp_path), git_repo)
    value = store.create("claude")
    worktree = Path(value["worktree_path"])
    (worktree / "feature.txt").write_text("session change\n", encoding="utf-8")
    run_id = store.checkpoint(value["session_id"])

    (git_repo / "new-head.txt").write_text("new head\n", encoding="utf-8")
    subprocess.run(["git", "add", "new-head.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "advance"], cwd=git_repo, check=True)
    artifacts = ArtifactStore(Path(store.config.runs.root), store.project_id)
    with pytest.raises(ArtifactError, match="working tree changed"):
        artifacts.apply(run_id, git_repo, repository_fingerprint(git_repo))
    assert not (git_repo / "feature.txt").exists()


def test_checkpoint_and_close_are_serialized(monkeypatch, git_repo, tmp_path):
    monkeypatch.setattr(isolated, "_create_session", lambda *args: None)
    monkeypatch.setattr(isolated, "session_exists", lambda name: False)
    store = IsolatedSessionStore(_config(tmp_path), git_repo)
    value = store.create("claude")
    worktree = Path(value["worktree_path"])
    (worktree / "feature.txt").write_text("change\n", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()
    original_capture = isolated._strict_checkpoint_patch
    errors: list[BaseException] = []

    def slow_capture(workdir: Path, base: str):
        entered.set()
        assert release.wait(5)
        return original_capture(workdir, base)

    monkeypatch.setattr(isolated, "_strict_checkpoint_patch", slow_capture)

    def checkpoint() -> None:
        try:
            store.checkpoint(value["session_id"])
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def close() -> None:
        try:
            store.close_session(value["session_id"])
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    checkpoint_thread = threading.Thread(target=checkpoint)
    close_thread = threading.Thread(target=close)
    checkpoint_thread.start()
    assert entered.wait(5)
    close_thread.start()
    time.sleep(0.1)
    assert close_thread.is_alive()
    release.set()
    checkpoint_thread.join(5)
    close_thread.join(5)

    assert errors == []
    final = store.load(value["session_id"])
    assert final["state"] == "closed"
    assert len(final["checkpoints"]) == 1
    assert not worktree.exists()
