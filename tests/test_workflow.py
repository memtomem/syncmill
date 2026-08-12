"""Native workflow planning, validation, persistence, and final artifacts."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from conftest import FakeRegistry
from syncmill.cli import cli
from syncmill.config import SyncmillConfig
from syncmill.models import Task, TaskResult, TaskStatus, TokenUsage
from syncmill.workflow import (
    WORKFLOW_DOCUMENT_MAX_BYTES,
    WorkflowCoordinator,
    WorkflowError,
    WorkflowSpec,
    WorkflowStore,
    parse_workflow_document,
    parse_workflow_spec,
    validate_workflow_policy,
)
from syncmill.tui_jobs import WorkflowRunSpec
from syncmill.worktree import repository_fingerprint


def _claim_workflow_process(
    runs_root: str,
    workflow_id: str,
    ready,
    release,
    results,
) -> None:
    store = WorkflowStore(Path(runs_root), "project")
    ready.put("ready")
    release.wait(10)
    try:
        record = store.claim_execution(
            workflow_id,
            resume=False,
            owner=f"process-{os.getpid()}",
            job_id=None,
            lease_timeout_seconds=60,
            deadline_seconds=3600,
            recovered_results={},
        )
        results.put(("ok", record.lease_owner))
    except WorkflowError as exc:
        results.put(("error", str(exc)))


class WorkflowAgent:
    agent_id = "claude"

    def __init__(self, goal: str):
        self.goal = goal
        self.calls: list[str] = []

    def _result(self, output: str) -> TaskResult:
        return TaskResult(
            task_id=uuid4(),
            agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            output=output,
            exit_code=0,
            duration_seconds=0.01,
        )

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        self.calls.append(prompt)
        spec = {
            "schema_version": 1,
            "goal": self.goal,
            "nodes": [
                {
                    "id": "inspect",
                    "agent": "claude",
                    "prompt": "Inspect the implementation and propose the change.",
                },
                {
                    "id": "final",
                    "agent": "claude",
                    "prompt": "Implement the final integrated change.",
                    "needs": ["inspect"],
                },
            ],
            "final_node": "final",
        }
        return self._result(json.dumps(spec))

    async def aexecute(
        self, prompt: str, workdir: Path, timeout: int
    ) -> TaskResult:
        if prompt.startswith("You are planning a SyncMill native workflow"):
            return self.execute(prompt, workdir, timeout)
        self.calls.append(prompt)
        if "This is the final node" in prompt:
            (workdir / "implemented.txt").write_text("integrated\n")
            return self._result("Implemented the integrated result.")
        return self._result("Repository inspection and recommendation.")


def _config(git_repo: Path, tmp_path: Path) -> SyncmillConfig:
    return SyncmillConfig(
        repo_root=str(git_repo),
        agent_priority=["claude"],
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "workspaces")},
        workflow={
            "planner_agent": "claude",
            "max_parallel": 4,
            "max_agent_calls": 32,
            "max_depth": 6,
            "deadline_seconds": 3600,
        },
    )


def test_spec_rejects_cycles_disconnected_nodes_and_executable_fields():
    with pytest.raises(ValidationError, match="cycle"):
        WorkflowSpec.model_validate(
            {
                "goal": "g",
                "nodes": [
                    {"id": "a", "agent": "claude", "prompt": "a", "needs": ["b"]},
                    {"id": "b", "agent": "claude", "prompt": "b", "needs": ["a"]},
                ],
                "final_node": "b",
            }
        )
    with pytest.raises(ValidationError, match="does not contribute"):
        WorkflowSpec.model_validate(
            {
                "goal": "g",
                "nodes": [
                    {"id": "unused", "agent": "claude", "prompt": "u"},
                    {"id": "final", "agent": "claude", "prompt": "f"},
                ],
                "final_node": "final",
            }
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        WorkflowSpec.model_validate(
            {
                "goal": "g",
                "nodes": [
                    {
                        "id": "final",
                        "agent": "claude",
                        "prompt": "f",
                        "script": "rm -rf anything",
                    }
                ],
                "final_node": "final",
            }
        )


def test_parse_workflow_spec_accepts_fenced_provider_json():
    output = "before\n```json\n" + json.dumps(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    ) + "\n```"
    assert parse_workflow_spec(output).final_node == "final"


def test_policy_enforces_available_agents_call_budget_and_depth(
    git_repo: Path, tmp_path: Path
):
    agent = WorkflowAgent("g")
    registry = FakeRegistry([agent])
    unavailable = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "codex", "prompt": "f"}],
            "final_node": "final",
        }
    )
    with pytest.raises(WorkflowError, match="unavailable agent"):
        validate_workflow_policy(unavailable, _config(git_repo, tmp_path), registry)

    chain = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [
                {"id": "one", "agent": "claude", "prompt": "1"},
                {"id": "two", "agent": "claude", "prompt": "2", "needs": ["one"]},
                {"id": "final", "agent": "claude", "prompt": "3", "needs": ["two"]},
            ],
            "final_node": "final",
        }
    )
    depth_limited = _config(git_repo, tmp_path)
    depth_limited.workflow.max_depth = 2
    with pytest.raises(WorkflowError, match="depth 3 exceeds"):
        validate_workflow_policy(chain, depth_limited, registry)

    call_limited = _config(git_repo, tmp_path)
    call_limited.workflow.max_agent_calls = 3
    with pytest.raises(WorkflowError, match="needs up to 4 agent calls"):
        validate_workflow_policy(chain, call_limited, registry)


def test_plan_is_non_mutating_and_run_emits_only_final_standard_artifact(
    git_repo: Path, tmp_path: Path
):
    goal = "Implement one integrated change"
    agent = WorkflowAgent(goal)
    config = _config(git_repo, tmp_path)
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))

    planned = coordinator.plan(goal)
    assert planned.state == "planned"
    assert planned.final_run_id is None
    assert not (git_repo / "implemented.txt").exists()
    assert subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout == ""

    completed = coordinator.run(planned.workflow_id)
    assert completed.state == "completed"
    assert completed.final_run_id
    assert not (git_repo / "implemented.txt").exists()

    artifact = coordinator.artifacts.load(completed.final_run_id)
    assert artifact.manifest["plan"]["origin"] == "workflow"
    assert artifact.manifest["plan"]["workflow_id"] == planned.workflow_id
    assert artifact.manifest["state"] == "pending"
    assert b"implemented.txt" in coordinator.artifacts.patch(completed.final_run_id)

    inspect_result = coordinator.store.read_node_result(
        planned.workflow_id, "inspect"
    )
    workflow_dir = coordinator.store.workflow_dir(planned.workflow_id)
    assert (workflow_dir / inspect_result["_patch_path"]).read_bytes() == b""
    assert [path.name for path in coordinator.artifacts.root.iterdir()] == [
        completed.final_run_id
    ]


def test_run_refuses_source_drift(git_repo: Path, tmp_path: Path):
    goal = "Plan safely"
    agent = WorkflowAgent(goal)
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    planned = coordinator.plan(goal)
    (git_repo / "README.md").write_text("changed after planning\n")

    with pytest.raises(WorkflowError, match="changed since planning"):
        coordinator.run(planned.workflow_id)
    assert len(agent.calls) == 1  # planner only; no workflow node started


def test_resume_reconciles_a_completed_result_saved_before_crash(
    git_repo: Path, tmp_path: Path
):
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = coordinator.store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint=repository_fingerprint(git_repo),
        planner_agent="claude",
        planner_output="{}",
    )
    result = agent._result("already complete")
    result.patch = ""
    bundle = coordinator.store.save_node_result(record.workflow_id, "final", result)

    def interrupted(manifest, state):
        manifest["nodes"]["final"].update(
            state="interrupted",
            attempts=1,
            active_attempt_id=bundle["_attempt_id"],
            bundle_sha256=None,
        )
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, interrupted)
    completed = coordinator.run(record.workflow_id, resume=True)
    assert completed.state == "completed"
    assert completed.final_run_id
    assert agent.calls == []  # persisted completed result was reconciled, not rerun


def test_cancel_and_prune_preserve_active_workflows(git_repo: Path, tmp_path: Path):
    store = WorkflowStore(tmp_path / "runs", "project")
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    one = store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="claude",
        planner_output="{}",
    )
    two = store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="b" * 64,
        planner_agent="claude",
        planner_output="{}",
    )
    assert store.cancel(one.workflow_id).state == "cancelled"
    removable = store.prune(keep=0, retention_days=1, dry_run=True)
    assert one.workflow_id not in removable  # too recent
    assert two.workflow_id not in removable  # active/planned is never pruned


def test_cross_process_execution_claim_allows_exactly_one_owner(
    git_repo: Path, tmp_path: Path
):
    runs_root = tmp_path / "runs"
    store = WorkflowStore(runs_root, "project")
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="claude",
        planner_output="{}",
    )
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_workflow_process,
            args=(str(runs_root), record.workflow_id, ready, release, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    assert [ready.get(timeout=10) for _ in processes] == ["ready", "ready"]
    release.set()
    outcomes = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    assert [outcome[0] for outcome in outcomes].count("ok") == 1
    assert [outcome[0] for outcome in outcomes].count("error") == 1
    assert "busy under another execution lease" in next(
        outcome[1] for outcome in outcomes if outcome[0] == "error"
    )


def test_expired_lease_recovers_to_explicit_resume_state(
    git_repo: Path, tmp_path: Path
):
    store = WorkflowStore(tmp_path / "runs", "project")
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="claude",
        planner_output="{}",
    )
    claimed = store.claim_execution(
        record.workflow_id,
        resume=False,
        owner="dead-owner",
        job_id="job-1",
        lease_timeout_seconds=60,
        deadline_seconds=3600,
        recovered_results={},
    )
    original_deadline = claimed.deadline_at
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET lease_expires=0 WHERE workflow_id=?",
            (record.workflow_id,),
        )
    assert store.recover_expired(record.workflow_id) == [record.workflow_id]
    recovered = store.load(record.workflow_id)
    assert recovered.state == "recovery_required"
    assert recovered.lease_owner is None
    resumed = store.claim_execution(
        record.workflow_id,
        resume=True,
        owner="new-owner",
        job_id="job-2",
        lease_timeout_seconds=60,
        deadline_seconds=9999,
        recovered_results={},
    )
    assert resumed.deadline_at == original_deadline
    assert resumed.lease_generation == 2


def test_attempt_bundle_tampering_is_visible_and_never_promoted(
    git_repo: Path, tmp_path: Path
):
    store = WorkflowStore(tmp_path / "runs", "project")
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="claude",
        planner_output="{}",
    )
    result = WorkflowAgent("g")._result("done")
    result.patch = ""
    bundle = store.save_node_result(record.workflow_id, "final", result)

    def link(manifest, state):
        manifest["nodes"]["final"].update(
            state="completed",
            active_attempt_id=bundle["_attempt_id"],
            bundle_sha256=bundle["_bundle_sha256"],
            output_path=bundle["_output_path"],
            patch_path=bundle["_patch_path"],
        )
        return manifest, state

    store.mutate(record.workflow_id, link)
    patch_path = store.workflow_dir(record.workflow_id) / bundle["_patch_path"]
    patch_path.write_bytes(b"tampered")
    with pytest.raises(WorkflowError, match="digest mismatch"):
        store.read_node_result(record.workflow_id, "final")


def test_cancelling_job_receipt_is_fenced_to_linked_job(
    git_repo: Path, tmp_path: Path
):
    store = WorkflowStore(tmp_path / "runs", "project")
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="claude",
        planner_output="{}",
    )
    store.claim_execution(
        record.workflow_id,
        resume=False,
        owner="owner",
        job_id="new-job",
        lease_timeout_seconds=60,
        deadline_seconds=3600,
        recovered_results={},
    )
    store.cancel(record.workflow_id)
    stale = store.reconcile_job_terminal(record.workflow_id, "old-job", "force_stopped")
    assert stale.state == "cancelling"
    final = store.reconcile_job_terminal(record.workflow_id, "new-job", "force_stopped")
    assert final.state == "cancelled"
    assert final.lease_owner is None


def test_legacy_running_row_migrates_to_recovery_required(
    git_repo: Path, tmp_path: Path
):
    root = tmp_path / "runs" / "project" / "workflows"
    root.mkdir(parents=True)
    db = root / "workflows.db"
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    legacy_manifest = {
        "schema_version": 1,
        "spec_sha256": spec.digest(),
        "nodes": {
            "final": {
                "state": "running",
                "attempts": 1,
                "agent": "claude",
                "error": None,
            }
        },
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": None,
        "deadline_at": "2026-01-01T01:00:00+00:00",
        "error": None,
    }
    with sqlite3.connect(db) as connection:
        connection.execute(
            """CREATE TABLE workflows (
               workflow_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,state TEXT NOT NULL,repo_root TEXT NOT NULL,
               source_fingerprint TEXT NOT NULL,planner_agent TEXT NOT NULL,
               spec_json TEXT NOT NULL,manifest_json TEXT NOT NULL,final_run_id TEXT)"""
        )
        connection.execute(
            "INSERT INTO workflows VALUES (?,?,?,?,?,?,?,?,?,NULL)",
            (
                "wf-0123456789abcdef",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "running",
                str(git_repo),
                "a" * 64,
                "claude",
                spec.model_dump_json(),
                json.dumps(legacy_manifest),
            ),
        )
    store = WorkflowStore(tmp_path / "runs", "project")
    migrated = store.load("wf-0123456789abcdef")
    assert migrated.state == "recovery_required"
    assert migrated.manifest["schema_version"] == 2
    assert migrated.manifest["nodes"]["final"]["state"] == "interrupted"
    assert migrated.lease_owner is None


def test_resume_preserves_and_enforces_cumulative_call_budget(
    git_repo: Path, tmp_path: Path
):
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    config.workflow.max_agent_calls = 2  # planner reservation + one node
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = coordinator.store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint=repository_fingerprint(git_repo),
        planner_agent="claude",
        planner_output="{}",
    )

    def exhausted(manifest, state):
        manifest["calls_used"] = 2
        manifest["nodes"]["final"].update(state="interrupted", attempts=0)
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, exhausted)
    with pytest.raises(WorkflowError, match="agent-call budget exhausted"):
        coordinator.run(record.workflow_id, resume=True)
    assert agent.calls == []
    assert coordinator.store.load(record.workflow_id).state == "recovery_required"


def test_node_prompt_budget_fails_before_provider_spawn(
    git_repo: Path, tmp_path: Path
):
    goal = "x" * 5000
    agent = WorkflowAgent(goal)
    config = _config(git_repo, tmp_path)
    config.workflow.max_prompt_bytes = 4096
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    spec = WorkflowSpec.model_validate(
        {
            "goal": goal,
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = coordinator.store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint=repository_fingerprint(git_repo),
        planner_agent="claude",
        planner_output="{}",
    )
    with pytest.raises(WorkflowError, match="prompt exceeds"):
        coordinator.run(record.workflow_id)
    assert agent.calls == []
    assert coordinator.store.load(record.workflow_id).state == "recovery_required"


def test_millflow_prompt_prefix_plans_without_running(monkeypatch):
    called = {}

    def fake_plan(goal, *, repo, team, planner):
        called.update(goal=goal, repo=repo, team=team, planner=planner)

    monkeypatch.setattr("syncmill.cli._plan_workflow", fake_plan)
    result = CliRunner().invoke(
        cli, ["run", "millflow: inspect and implement", "--repo", "/tmp/project"]
    )
    assert result.exit_code == 0
    assert called == {
        "goal": "inspect and implement",
        "repo": "/tmp/project",
        "team": None,
        "planner": None,
    }


def test_workflow_run_detach_preflights_and_launches_typed_job(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    registry = FakeRegistry([agent])
    coordinator = WorkflowCoordinator(config, registry)
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = coordinator.store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint=repository_fingerprint(git_repo),
        planner_agent="claude",
        planner_output="{}",
    )

    class FakeSupervisor:
        def __init__(self):
            self.workflow_store = coordinator.store
            self.registry = registry

        def close(self):
            pass

    captured = {}

    class FakeJobs:
        def launch(self, job_spec, *, resolved_config):
            captured.update(spec=job_spec, config=resolved_config)
            return {"job_id": "12345678-1234", "job_dir": str(tmp_path / "job")}

    monkeypatch.setattr(
        "syncmill.cli._workflow_supervisor",
        lambda repo, team: (FakeSupervisor(), config),
    )
    monkeypatch.setattr("syncmill.cli._workflow_job_store", lambda *args: FakeJobs())
    result = CliRunner().invoke(
        cli, ["workflow", "run", record.workflow_id, "--detach"]
    )
    assert result.exit_code == 0, result.output
    assert isinstance(captured["spec"], WorkflowRunSpec)
    assert captured["spec"].workflow_id == record.workflow_id
    assert captured["spec"].resume is False
    assert "detached workflow-run job 12345678" in result.output


def test_workflow_detach_refuses_source_drift_before_launch(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    registry = FakeRegistry([agent])
    coordinator = WorkflowCoordinator(config, registry)
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = coordinator.store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint=repository_fingerprint(git_repo),
        planner_agent="claude",
        planner_output="{}",
    )

    class FakeSupervisor:
        def __init__(self):
            self.workflow_store = coordinator.store
            self.registry = registry

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._workflow_supervisor",
        lambda repo, team: (FakeSupervisor(), config),
    )
    monkeypatch.setattr(
        "syncmill.cli._workflow_job_store",
        lambda *args: pytest.fail("drifted workflow must not launch"),
    )
    (git_repo / "drift.txt").write_text("changed\n")
    result = CliRunner().invoke(
        cli, ["workflow", "run", record.workflow_id, "--detach"]
    )
    assert result.exit_code != 0
    assert "repository changed since planning" in result.output


def test_millflow_prefix_rejects_normal_run_flags(monkeypatch):
    monkeypatch.setattr(
        "syncmill.cli._plan_workflow",
        lambda *args, **kwargs: pytest.fail("planner must not start"),
    )
    result = CliRunner().invoke(
        cli, ["run", "millflow: goal", "--strategy", "compete"]
    )
    assert result.exit_code != 0
    assert "millflow planning does not accept: --strategy" in result.output


def test_schema_v1_digest_is_frozen_and_document_dispatch_is_strict():
    raw = {
        "schema_version": 1,
        "goal": "g",
        "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
        "final_node": "final",
    }
    spec = parse_workflow_document(raw)
    assert spec.digest() == (
        "1fa8877ddcb47c438fc71ff1657e49b3c4fd3f58d4b69b3b936184837d151152"
    )
    with pytest.raises(WorkflowError, match="unsupported workflow schema_version"):
        parse_workflow_document({**raw, "schema_version": 2})
    with pytest.raises(WorkflowError, match="NUL"):
        parse_workflow_document({**raw, "goal": "bad\0goal"})


def test_file_plan_create_export_validate_and_revise_round_trip(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    registry = FakeRegistry([agent])
    coordinator = WorkflowCoordinator(config, registry)
    document = tmp_path / "workflow.json"
    document.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "goal": "g",
                "nodes": [
                    {"id": "final", "agent": "claude", "prompt": "f"}
                ],
                "final_node": "final",
            }
        ),
        encoding="utf-8",
    )

    class FakeSupervisor:
        workflow_store = coordinator.store

        def create_workflow(self, spec, *, parent_workflow_id=None):
            return coordinator.create_from_spec(
                spec, parent_workflow_id=parent_workflow_id
            )

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._workflow_validation_context",
        lambda repo, team: (config, registry),
    )
    monkeypatch.setattr(
        "syncmill.cli._workflow_supervisor",
        lambda repo, team: (FakeSupervisor(), config),
    )
    runner = CliRunner()
    validated = runner.invoke(cli, ["workflow", "validate", str(document)])
    assert validated.exit_code == 0, validated.output
    assert "valid workflow schema=1" in validated.output
    validated_stdin = runner.invoke(
        cli,
        ["workflow", "validate", "-"],
        input=document.read_text(encoding="utf-8"),
    )
    assert validated_stdin.exit_code == 0, validated_stdin.output
    assert not agent.calls

    created = runner.invoke(
        cli, ["workflow", "create", "--file", str(document)]
    )
    assert created.exit_code == 0, created.output
    workflow_id = created.output.split("workflow: ", 1)[1].splitlines()[0]
    first = coordinator.store.load(workflow_id)
    assert first.plan_origin == "file"
    assert first.plan_version == 1
    assert first.lineage_id == workflow_id
    assert first.call_count == 0
    assert not (coordinator.store.workflow_dir(workflow_id) / "planner-output.txt").exists()

    exported = tmp_path / "exported.json"
    result = runner.invoke(
        cli,
        ["workflow", "export", workflow_id, "--output", str(exported)],
    )
    assert result.exit_code == 0, result.output
    exported_spec = parse_workflow_document(json.loads(exported.read_text()))
    assert exported_spec.digest() == first.spec.digest()
    stdout_export = runner.invoke(cli, ["workflow", "export", workflow_id])
    assert stdout_export.exit_code == 0, stdout_export.output
    assert parse_workflow_document(json.loads(stdout_export.output)).digest() == (
        first.spec.digest()
    )
    overwrite = runner.invoke(
        cli,
        ["workflow", "export", workflow_id, "--output", str(exported)],
    )
    assert overwrite.exit_code != 0 and "--force" in overwrite.output

    revised_doc = tmp_path / "revised.json"
    revised = first.spec.model_copy(update={"goal": "revised goal"})
    revised_doc.write_text(revised.model_dump_json(), encoding="utf-8")
    result = runner.invoke(
        cli,
        ["workflow", "revise", workflow_id, "--file", str(revised_doc)],
    )
    assert result.exit_code == 0, result.output
    revised_id = result.output.split("workflow: ", 1)[1].splitlines()[0]
    second = coordinator.store.load(revised_id)
    assert second.plan_origin == "revision"
    assert second.parent_workflow_id == workflow_id
    assert second.lineage_id == workflow_id
    assert second.plan_version == 2
    assert second.call_count == 0
    assert coordinator.store.load(workflow_id) == first
    assert [row.workflow_id for row in coordinator.store.lineage(revised_id)] == [
        workflow_id,
        revised_id,
    ]
    lineage = runner.invoke(cli, ["workflow", "lineage", revised_id])
    assert lineage.exit_code == 0, lineage.output
    assert f"v1  {workflow_id}" in lineage.output
    assert f"v2  {revised_id}" in lineage.output
    assert not agent.calls


def test_validate_rejects_oversize_and_invalid_utf8_before_context(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        "syncmill.cli._workflow_validation_context",
        lambda *args: pytest.fail("invalid input must fail before config or store"),
    )
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b"x" * WORKFLOW_DOCUMENT_MAX_BYTES)
    result = CliRunner().invoke(
        cli, ["workflow", "validate", str(oversized)]
    )
    assert result.exit_code != 0 and "8 MiB" in result.output
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"{\xff}")
    result = CliRunner().invoke(cli, ["workflow", "validate", str(invalid)])
    assert result.exit_code != 0 and "valid UTF-8" in result.output


def test_validate_is_state_free_and_never_builds_a_supervisor(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    document = tmp_path / "workflow.json"
    document.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "goal": "g",
                "nodes": [
                    {"id": "final", "agent": "claude", "prompt": "f"}
                ],
                "final_node": "final",
            }
        ),
        encoding="utf-8",
    )

    def forbidden(*args, **kwargs):
        pytest.fail("validate must not construct durable workflow state")

    monkeypatch.setattr("syncmill.cli._workflow_supervisor", forbidden)
    monkeypatch.setattr(WorkflowStore, "__init__", forbidden)
    result = CliRunner().invoke(
        cli,
        ["workflow", "validate", str(document), "--repo", str(git_repo)],
    )
    assert result.exit_code == 0, result.output
    assert "valid workflow schema=1" in result.output


def test_concurrent_revisions_get_unique_versions(
    git_repo: Path, tmp_path: Path
):
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    parent = coordinator.create_from_spec(spec)

    def revise(index: int):
        updated = spec.model_copy(update={"goal": f"revision {index}"})
        return coordinator.create_from_spec(
            updated, parent_workflow_id=parent.workflow_id
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        revisions = list(executor.map(revise, [1, 2]))
    assert sorted(row.plan_version for row in revisions) == [2, 3]
    assert len({row.workflow_id for row in revisions}) == 2
    assert coordinator.store.load(parent.workflow_id).plan_version == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX flock execution fence")
def test_live_execution_lock_blocks_expired_lease_recovery(
    git_repo: Path, tmp_path: Path
):
    store = WorkflowStore(tmp_path / "runs", "project")
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="file",
        planner_output=None,
        planning_calls=0,
        plan_origin="file",
    )
    store.claim_execution(
        record.workflow_id,
        resume=False,
        owner="live-owner",
        job_id=None,
        lease_timeout_seconds=60,
        deadline_seconds=3600,
        recovered_results={},
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET lease_expires=0 WHERE workflow_id=?",
            (record.workflow_id,),
        )
    with store.execution_lock(record.workflow_id, blocking=False) as acquired:
        assert acquired is True
        assert store.recover_expired(record.workflow_id) == []
        assert store.load(record.workflow_id).state == "running"
    assert store.recover_expired(record.workflow_id) == [record.workflow_id]


def test_stale_attempt_never_publishes_commit_marker_after_lease_loss(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    store = WorkflowStore(tmp_path / "runs", "project")
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="file",
        planner_output=None,
        planning_calls=0,
        plan_origin="file",
    )
    claimed = store.claim_execution(
        record.workflow_id,
        resume=False,
        owner="owner-one",
        job_id=None,
        lease_timeout_seconds=60,
        deadline_seconds=3600,
        recovered_results={},
    )
    attempt_id = "a0001-0123456789ab"

    def start_attempt(manifest, state):
        manifest["nodes"]["final"].update(
            state="running",
            attempts=1,
            active_attempt_id=attempt_id,
            started_at="2026-01-01T00:00:00+00:00",
        )
        return manifest, state

    store.mutate(
        record.workflow_id,
        start_attempt,
        lease_owner="owner-one",
        lease_generation=claimed.lease_generation,
    )
    original = store.assert_execution_lease
    checks = 0

    def lose_on_commit(workflow_id, owner, generation):
        nonlocal checks
        checks += 1
        if checks == 2:
            with store._connect() as connection:
                connection.execute(
                    "UPDATE workflows SET lease_owner='owner-two',"
                    "lease_generation=lease_generation+1 WHERE workflow_id=?",
                    (workflow_id,),
                )
        original(workflow_id, owner, generation)

    monkeypatch.setattr(store, "assert_execution_lease", lose_on_commit)
    result = WorkflowAgent("g")._result("done")
    result.patch = ""
    with pytest.raises(WorkflowError, match="lease was lost"):
        store.save_node_result(
            record.workflow_id,
            "final",
            result,
            attempt_id=attempt_id,
            lease_owner="owner-one",
            lease_generation=claimed.lease_generation,
        )
    attempt = (
        store.workflow_dir(record.workflow_id)
        / f"nodes/final/attempts/{attempt_id}"
    )
    assert (attempt / "winner.patch").exists()
    assert (attempt / "result.json").exists()
    assert not (attempt / "commit.json").exists()

    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET lease_expires=0 WHERE workflow_id=?",
            (record.workflow_id,),
        )
    assert store.recover_expired(record.workflow_id) == [record.workflow_id]
    interrupted = store.load(record.workflow_id).manifest["nodes"]["final"]
    assert interrupted["active_attempt_id"] is None
    assert interrupted["bundle_sha256"] is None

    # Even if the expired writer publishes late, recovery has already detached
    # that attempt id from durable state, so resume retries instead of promoting it.
    (attempt / "commit.json").write_text("{}", encoding="utf-8")
    resumed = store.claim_execution(
        record.workflow_id,
        resume=True,
        owner="owner-three",
        job_id=None,
        lease_timeout_seconds=60,
        deadline_seconds=3600,
        recovered_results={},
    )
    assert resumed.manifest["nodes"]["final"]["state"] == "pending"
    assert resumed.manifest["nodes"]["final"]["active_attempt_id"] is None


def test_resume_accounts_committed_usage_exactly_once(
    git_repo: Path, tmp_path: Path
):
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = coordinator.create_from_spec(spec)
    result = agent._result("already complete")
    result.patch = ""
    result.usage = TokenUsage(
        total_input_tokens=11, output_tokens=7, total_cost_usd=0.25
    )
    bundle = coordinator.store.save_node_result(
        record.workflow_id, "final", result
    )

    def interrupted(manifest, state):
        manifest["nodes"]["final"].update(
            state="interrupted",
            attempts=1,
            active_attempt_id=bundle["_attempt_id"],
            bundle_sha256=None,
        )
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, interrupted)
    completed = coordinator.run(record.workflow_id, resume=True)
    assert completed.input_tokens == 11
    assert completed.output_tokens == 7
    assert completed.total_cost_usd == 0.25
    assert agent.calls == []


def test_workflow_gate_environment_is_fail_closed(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    class GateAgent(WorkflowAgent):
        async def aexecute(self, prompt, workdir, timeout):
            if prompt.startswith("You are planning a SyncMill native workflow"):
                return self.execute(prompt, workdir, timeout)
            self.calls.append(prompt)
            (workdir / "gate_check.py").write_text(
                "import os,sys\n"
                "blocked=['GITHUB_TOKEN','AWS_SECRET_ACCESS_KEY','OPENAI_API_KEY']\n"
                "sys.exit(1 if any(os.getenv(k) for k in blocked) "
                "or os.getenv('WORKFLOW_GATE_OK') != 'yes' else 0)\n"
            )
            return self._result("implemented")

    monkeypatch.setenv("GITHUB_TOKEN", "canary")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "canary")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-canary")
    monkeypatch.setenv("WORKFLOW_GATE_OK", "yes")
    agent = GateAgent("g")
    config = _config(git_repo, tmp_path)
    config.gate.commands = [f"{sys.executable} gate_check.py"]
    config.workflow.gate_env_allowlist = ["WORKFLOW_GATE_OK"]
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    record = coordinator.create_from_spec(
        WorkflowSpec.model_validate(
            {
                "goal": "g",
                "nodes": [
                    {"id": "final", "agent": "claude", "prompt": "f"}
                ],
                "final_node": "final",
            }
        )
    )
    completed = coordinator.run(record.workflow_id)
    assert completed.state == "completed"


def test_final_artifact_collision_requires_complete_provenance(
    git_repo: Path, tmp_path: Path
):
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    record = coordinator.create_from_spec(spec)
    result = agent._result("already complete")
    result.patch = ""
    bundle = coordinator.store.save_node_result(
        record.workflow_id, "final", result
    )

    def interrupted(manifest, state):
        manifest["nodes"]["final"].update(
            state="interrupted",
            attempts=1,
            active_attempt_id=bundle["_attempt_id"],
            bundle_sha256=None,
        )
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, interrupted)
    collision_result = TaskResult(
        task_id=uuid4(),
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="collision",
        exit_code=0,
        duration_seconds=0,
        patch="",
    )
    coordinator.artifacts.save(
        run_id=f"{record.workflow_id}-final",
        task=Task(prompt="g"),
        result=collision_result,
        repo_root=git_repo,
        snapshot_commit="wrong-snapshot",
        source_fingerprint=record.source_fingerprint,
        strategy="route",
        plan={"origin": "workflow", "workflow_id": record.workflow_id},
    )
    with pytest.raises(WorkflowError, match="final run id collision"):
        coordinator.run(record.workflow_id, resume=True)
    assert coordinator.store.load(record.workflow_id).state == "recovery_required"
