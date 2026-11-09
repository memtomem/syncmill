"""Versioned repository fingerprints (#262).

A record keeps the fingerprint version it was created with. "Created before
the upgrade" is reproduced by creating the record while
``CURRENT_FINGERPRINT_VERSION`` is 1, so every write path stamps v1 exactly as
the old code did, then running the rest of the lifecycle at the current
version.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import syncmill.isolated_session as isolated
import syncmill.worktree as W
from syncmill.artifacts import ArtifactStore
from syncmill.git_env import isolated_git_env
from syncmill.isolated_session import IsolatedSessionStore
from syncmill.rooms import RoomCoordinator
from syncmill.workflow import WorkflowCoordinator
from syncmill.worktree import (
    WorkspaceError,
    fingerprint_matches,
    fingerprint_version,
    like_stored,
    repository_fingerprint,
)

from conftest import FakeRegistry
from test_isolated_session import _config as _session_config
from test_rooms import WritingAgent, _config as _room_config
from test_workflow import WorkflowAgent, _config as _workflow_config

V1_LIKE = "0" * 64


@contextlib.contextmanager
def _before_upgrade(monkeypatch):
    """Code paths inside run as the pre-#262 release did: they stamp v1."""
    with monkeypatch.context() as patch:
        patch.setattr(W, "CURRENT_FINGERPRINT_VERSION", 1)
        yield


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo, env=isolated_git_env(), check=True, capture_output=True
    ).stdout


# ── encoding ─────────────────────────────────────────────────────────────────


def test_versions_are_recognized_by_shape(git_repo):
    v2 = repository_fingerprint(git_repo)
    v1 = repository_fingerprint(git_repo, like=V1_LIKE)
    assert v2.startswith("v2:") and fingerprint_version(v2) == 2
    assert fingerprint_version(v1) == 1 and len(v1) == 64
    assert v2[3:] != v1
    for bad in (None, "", "v9:" + "0" * 64, "v2:" + "0" * 63, "V2:" + "0" * 64,
                "0" * 63, "g" * 64, 64, b"0" * 64):
        assert fingerprint_version(bad) is None
        with pytest.raises(WorkspaceError, match="unrecognized"):
            repository_fingerprint(git_repo, like=bad)


def test_v2_encoding_is_pinned(git_repo):
    (git_repo / "untracked.txt").write_bytes(b"hello\n")
    (git_repo / "run.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(git_repo / "run.sh", 0o755)
    os.chmod(git_repo / "untracked.txt", 0o644)
    (git_repo / "link").symlink_to("untracked.txt")

    def framed(data: bytes) -> bytes:
        return str(len(data)).encode() + b"\0" + data

    expected = hashlib.sha256(b"syncmill-repository-fingerprint-v2\0")
    expected.update(framed(_git(git_repo, "rev-parse", "--verify", "HEAD")))
    expected.update(framed(_git(git_repo, "status", "--porcelain=v2", "--untracked-files=all")))
    expected.update(framed(_git(git_repo, "diff", "--binary", "HEAD")))
    expected.update(framed(b"link") + b"L" + framed(b"untracked.txt"))
    expected.update(framed(b"run.sh") + b"X" + framed(b"#!/bin/sh\n"))
    expected.update(framed(b"untracked.txt") + b"F" + framed(b"hello\n"))
    assert repository_fingerprint(git_repo) == "v2:" + expected.hexdigest()


def test_v2_closes_the_framing_collision_v1_keeps(git_repo):
    a, b = git_repo / "a", git_repo / "b"

    def both(a_bytes: bytes, b_bytes: bytes) -> tuple[str, str]:
        a.write_bytes(a_bytes)
        b.write_bytes(b_bytes)
        return (
            repository_fingerprint(git_repo, like=V1_LIKE),
            repository_fingerprint(git_repo),
        )

    first_v1, first_v2 = both(b"", b"b\0FX")
    second_v1, second_v2 = both(b"b\0F", b"X")
    assert first_v1 == second_v1  # the v1 gap, kept for stored records
    assert first_v2 != second_v2


def test_v2_sees_an_untracked_exec_bit_v1_does_not(git_repo):
    script = git_repo / "run.sh"
    script.write_bytes(b"#!/bin/sh\n")
    os.chmod(script, 0o644)
    before = (repository_fingerprint(git_repo, like=V1_LIKE), repository_fingerprint(git_repo))
    os.chmod(script, 0o755)
    after = (repository_fingerprint(git_repo, like=V1_LIKE), repository_fingerprint(git_repo))
    assert before[0] == after[0]
    assert before[1] != after[1]


def test_fingerprint_matches_recomputes_in_the_stored_version(git_repo, tmp_path):
    v1 = repository_fingerprint(git_repo, like=V1_LIKE)
    v2 = repository_fingerprint(git_repo)
    assert fingerprint_matches(git_repo, v1)
    assert fingerprint_matches(git_repo, v2)
    (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert not fingerprint_matches(git_repo, v1)
    assert not fingerprint_matches(git_repo, v2)
    # An unrecognized stored value is a plain mismatch, as `!=` was ...
    for bad in (None, "v9:" + "0" * 64, "garbage", 7):
        assert fingerprint_matches(git_repo, bad) is False
    # ... but the source is still read, so a git failure still raises.
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(WorkspaceError):
        fingerprint_matches(not_a_repo, None)


def test_like_stored_falls_back_to_the_current_version(git_repo):
    v1 = repository_fingerprint(git_repo, like=V1_LIKE)
    assert repository_fingerprint(git_repo, like=like_stored(v1)) == v1
    for bad in (None, "garbage"):
        assert fingerprint_version(repository_fingerprint(git_repo, like=like_stored(bad))) == 2


# ── records made before the upgrade keep working after it ─────────────────────


def _session_store(monkeypatch, git_repo, tmp_path) -> IsolatedSessionStore:
    monkeypatch.setattr(isolated, "_create_session", lambda *args: None)
    monkeypatch.setattr(isolated, "session_exists", lambda name: False)
    return IsolatedSessionStore(_session_config(tmp_path), git_repo)


def test_a_v1_session_lists_fresh_checkpoints_v1_and_its_checkpoint_applies(
    monkeypatch, git_repo, tmp_path
):
    store = _session_store(monkeypatch, git_repo, tmp_path)
    with _before_upgrade(monkeypatch):
        old = store.create("claude")
    new = store.create("codex")
    assert fingerprint_version(old["source_fingerprint"]) == 1
    assert fingerprint_version(new["source_fingerprint"]) == 2

    listed = {value["session_id"]: value for value in store.list()}
    assert listed[old["session_id"]]["source_stale"] is False
    assert listed[new["session_id"]]["source_stale"] is False

    (Path(old["worktree_path"]) / "feature.txt").write_text("done\n", encoding="utf-8")
    run_id = store.checkpoint(old["session_id"])
    artifacts = ArtifactStore(Path(store.config.runs.root), store.project_id)
    assert artifacts.load(run_id).manifest["source_fingerprint"] == old["source_fingerprint"]
    assert artifacts.apply(run_id, git_repo, None).exists()
    assert (git_repo / "feature.txt").read_text(encoding="utf-8") == "done\n"


def test_session_listing_scans_once_per_version_and_isolates_a_bad_record(
    monkeypatch, git_repo, tmp_path
):
    store = _session_store(monkeypatch, git_repo, tmp_path)
    with _before_upgrade(monkeypatch):
        old = store.create("claude")
    new = store.create("codex")
    bad = store.create("kimi-code")
    path = store._path(bad["session_id"])
    path.write_text(
        path.read_text(encoding="utf-8").replace(bad["source_fingerprint"], "v9:" + "0" * 64),
        encoding="utf-8",
    )
    monkeypatch.setattr(isolated, "session_exists", lambda name: True)
    real = isolated.repository_fingerprint
    calls: list[object] = []

    def counted(repo_root, **kwargs):
        calls.append(kwargs.get("like"))
        return real(repo_root, **kwargs)

    monkeypatch.setattr(isolated, "repository_fingerprint", counted)
    listed = {value["session_id"]: value for value in store.list()}
    assert listed[old["session_id"]]["source_stale"] is False
    assert listed[new["session_id"]]["source_stale"] is False
    assert listed[bad["session_id"]]["source_stale"] is True
    assert len(calls) == 2  # one v1 scan, one current scan shared by v2 and the bad value


def test_a_v1_room_runs_later_rounds_and_applies_an_old_candidate(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _room_config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("change it")
        with _before_upgrade(monkeypatch):
            coordinator.run_round(room["id"])
        recorded = coordinator.store.get_room(room["id"])["source_fingerprint"]
        assert fingerprint_version(recorded) == 1
        old_candidate = coordinator.store.list_candidates(room["id"])[0]["id"]

        # continue-own (retained workspaces) and a fresh snapshot round.
        assert coordinator.run_round(room["id"])["state"] == "awaiting_decision"
        assert coordinator.run_round(room["id"], lineage="fresh")["state"] == "awaiting_decision"
        assert coordinator.store.get_room(room["id"])["source_fingerprint"] == recorded
        candidates = coordinator.store.list_candidates(room["id"])
        assert {c["source_fingerprint"] for c in candidates} == {recorded}

        coordinator.select(room["id"], old_candidate)
        _, receipt = coordinator.apply(room["id"])
        assert receipt.exists()
    finally:
        coordinator.close()


def test_a_v1_room_still_goes_stale_when_the_source_changes(monkeypatch, git_repo, tmp_path):
    coordinator = RoomCoordinator(
        _room_config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("change it")
        with _before_upgrade(monkeypatch):
            coordinator.run_round(room["id"])
        (git_repo / "README.md").write_text("drift\n", encoding="utf-8")
        with pytest.raises(Exception):
            coordinator.run_round(room["id"], lineage="fresh")
        assert coordinator.store.get_room(room["id"])["state"] == "stale"
    finally:
        coordinator.close()


def test_a_v1_workflow_runs_and_its_final_artifact_applies(monkeypatch, git_repo, tmp_path):
    goal = "Implement one integrated change"
    coordinator = WorkflowCoordinator(
        _workflow_config(git_repo, tmp_path), FakeRegistry([WorkflowAgent(goal)])
    )
    with _before_upgrade(monkeypatch):
        planned = coordinator.plan(goal)
    assert fingerprint_version(planned.source_fingerprint) == 1

    completed = coordinator.run(planned.workflow_id)
    assert completed.state == "completed"
    artifact = coordinator.artifacts.load(completed.final_run_id)
    assert artifact.manifest["source_fingerprint"] == planned.source_fingerprint


def test_room_compare_reports_a_v1_room_as_current(monkeypatch, git_repo, tmp_path):
    import json

    from click.testing import CliRunner

    from syncmill.cli import cli
    from test_cli import _patch_room_coordinator

    coordinator = RoomCoordinator(
        _room_config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("change it")
        with _before_upgrade(monkeypatch):
            coordinator.run_round(room["id"])
        _patch_room_coordinator(monkeypatch, tmp_path, coordinator)
        result = CliRunner().invoke(cli, ["room", "compare", room["id"], "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["source"]["status"] == "current"
    finally:
        coordinator.close()


def test_workflow_run_accepts_a_v1_plan_before_launch(monkeypatch, git_repo, tmp_path):
    from click.testing import CliRunner

    from syncmill.cli import cli
    from syncmill.workflow import WorkflowSpec

    config = _workflow_config(git_repo, tmp_path)
    registry = FakeRegistry([WorkflowAgent("g")])
    coordinator = WorkflowCoordinator(config, registry)
    spec = WorkflowSpec.model_validate(
        {"goal": "g", "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
         "final_node": "final"}
    )
    record = coordinator.store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint=repository_fingerprint(git_repo, like=V1_LIKE),
        planner_agent="claude",
        planner_output="{}",
    )

    class FakeSupervisor:
        workflow_preflight_warnings: list = []
        workflow_store = coordinator.store

        def __init__(self):
            self.registry = registry

        def close(self):
            pass

    class FakeJobs:
        def launch(self, job_spec, *, resolved_config):
            return {"job_id": "12345678-1234", "job_dir": str(tmp_path / "job")}

    monkeypatch.setattr(
        "syncmill.cli._workflow_supervisor", lambda repo, team: (FakeSupervisor(), config)
    )
    monkeypatch.setattr("syncmill.cli._peek_workflow_team", lambda repo, wid: None)
    monkeypatch.setattr("syncmill.cli._workflow_job_store", lambda *args: FakeJobs())
    result = CliRunner().invoke(cli, ["workflow", "run", record.workflow_id, "--detach"])
    assert result.exit_code == 0, result.output
    assert "detached workflow-run job 12345678" in result.output


def test_recover_restores_an_interrupted_v1_apply_to_pending(
    monkeypatch, git_repo, tmp_path
):
    from test_v2_workflow import _force_applying

    store = _session_store(monkeypatch, git_repo, tmp_path)
    with _before_upgrade(monkeypatch):
        session = store.create("claude")
    (Path(session["worktree_path"]) / "feature.txt").write_text("done\n", encoding="utf-8")
    run_id = store.checkpoint(session["session_id"])
    artifacts = ArtifactStore(Path(store.config.runs.root), store.project_id)
    _force_applying(artifacts, artifacts.load(run_id), owner=None)

    [verdict] = artifacts.reconcile_interrupted_applies(run_id=run_id)
    assert verdict.outcome == "not_applied"
    assert artifacts.load(run_id).manifest["state"] == "pending"


def test_continue_of_a_v1_pending_run_seeds_its_patch(monkeypatch, git_repo, tmp_path):
    import json
    import stat

    from click.testing import CliRunner

    from syncmill.cli import cli

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
    with _before_upgrade(monkeypatch):
        first = runner.invoke(
            cli, ["run", "--repo", str(git_repo), "--agent", "codex", "make it"], env=env
        )
    assert first.exit_code == 0, first.output
    first_run = next(
        line.split(": ", 1)[1]
        for line in first.output.splitlines()
        if line.startswith("[syncmill] run:")
    )
    project_runs = next((tmp_path / "runs").glob("*/runs"))
    first_manifest = json.loads((project_runs / first_run / "manifest.json").read_text())
    assert fingerprint_version(first_manifest["source_fingerprint"]) == 1

    continued = runner.invoke(
        cli, ["continue", first_run, "improve it", "--repo", str(git_repo)], env=env
    )
    assert continued.exit_code == 0, continued.output
    second_run = next(
        line.split(": ", 1)[1]
        for line in continued.output.splitlines()
        if line.startswith("[syncmill] run:")
    )
    second_manifest = json.loads((project_runs / second_run / "manifest.json").read_text())
    assert second_manifest["parent_run_id"] == first_run
    # A continuation is a new root: it is stamped with the current version.
    assert fingerprint_version(second_manifest["source_fingerprint"]) == 2


def test_room_recover_reports_an_unchanged_source_for_a_v1_room(
    monkeypatch, git_repo, tmp_path
):
    coordinator = RoomCoordinator(
        _room_config(tmp_path), git_repo, agents=[WritingAgent("claude")]
    )
    try:
        room = coordinator.create("interrupted")
        with _before_upgrade(monkeypatch):
            coordinator.run_round(room["id"])
        candidate = coordinator.store.list_candidates(room["id"])[0]
        coordinator.select(room["id"], candidate["id"])
        coordinator.store.begin_apply(
            room["id"], run_id="interrupted-before-artifact", lease_owner="99999999-dead"
        )
        assert coordinator.recover(room["id"]) == [room["id"]]
        event = coordinator.store.events(room["id"])[-1]
        assert event["kind"] == "apply.interrupted_recovered"
        assert event["payload"]["source_matches"] is True
    finally:
        coordinator.close()


def test_a_v1_checkpoint_applies_with_verification(monkeypatch, git_repo, tmp_path):
    import shlex
    import sys

    store = _session_store(monkeypatch, git_repo, tmp_path)
    with _before_upgrade(monkeypatch):
        session = store.create("claude")
    (Path(session["worktree_path"]) / "feature.txt").write_text("done\n", encoding="utf-8")
    run_id = store.checkpoint(session["session_id"])
    artifacts = ArtifactStore(Path(store.config.runs.root), store.project_id)
    receipt = artifacts.apply(
        run_id,
        git_repo,
        None,
        verify_commands=[f"{shlex.quote(sys.executable)} -c pass"],
        verify_workspace_root=tmp_path / "verify",
    )
    assert receipt.exists()
