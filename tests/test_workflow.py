"""Native workflow planning, validation, persistence, and final artifacts."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from conftest import FakeRegistry
from syncmill.cli import cli
from syncmill.config import SyncmillConfig
from syncmill.models import TaskResult, TaskStatus
import syncmill.workflow as workflow_module
from syncmill.workflow import (
    WorkflowCoordinator,
    WorkflowError,
    WorkflowLeaseLost,
    WorkflowSpec,
    WorkflowStore,
    parse_workflow_spec,
    render_workflow,
    validate_workflow_policy,
)
from syncmill.rooms import _prompt_arg_units, _prompt_max_bytes
from syncmill.workflow import _now as _now_iso
from syncmill.worktree import repository_fingerprint


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

    workflow_dir = coordinator.store.workflow_dir(planned.workflow_id)
    assert (workflow_dir / "nodes" / "inspect" / "winner.patch").read_bytes() == b""
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
    coordinator.store.save_node_result(record.workflow_id, "final", result)

    def interrupted(manifest, state):
        manifest["nodes"]["final"].update(state="interrupted", attempts=1)
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


def _terminal_workflow(store: WorkflowStore, git_repo: Path, *, age_days: int):
    """A cancelled workflow whose updated_at is old enough to be prunable."""
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
    store.cancel(record.workflow_id)
    stale = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE workflows SET updated_at=? WHERE workflow_id=?",
            (stale, record.workflow_id),
        )
    return record


def test_prune_deletes_the_directory_before_the_row(git_repo: Path, tmp_path: Path):
    # The row is the only handle on the directory, so deleting it first turns
    # any removal failure into an orphan nothing can find again.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=30)
    directory = store.workflow_dir(record.workflow_id)
    assert directory.is_dir()

    assert store.prune(keep=0, retention_days=1, dry_run=False) == [
        record.workflow_id
    ]
    assert not directory.exists()
    assert store.list() == []


def test_prune_keeps_the_row_when_the_directory_cannot_be_removed(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    # A failed removal must stay retryable, and must never be reported as done.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=30)

    def _boom(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(workflow_module.shutil, "rmtree", _boom)
    assert store.prune(keep=0, retention_days=1, dry_run=False) == []
    assert [r.workflow_id for r in store.list()] == [record.workflow_id]
    assert store.workflow_dir(record.workflow_id).is_dir()


def test_prune_skips_a_directory_without_syncmill_ownership(
    git_repo: Path, tmp_path: Path
):
    # "Under my root with my id shape" is an inference; rmtree is not the place
    # to infer. Only a directory carrying our own marker is ours to delete.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=30)
    (store.workflow_dir(record.workflow_id) / "spec.json").unlink()

    assert store.prune(keep=0, retention_days=1, dry_run=False) == []
    assert store.workflow_dir(record.workflow_id).is_dir()


def test_two_processes_cannot_both_claim_one_workflow(git_repo: Path, tmp_path: Path):
    # The pre-transaction state check is only a hint: both readers can pass it.
    # The claim itself has to be the compare-and-swap.
    store = WorkflowStore(tmp_path / "runs", "project")
    other = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE workflows SET state='planned' WHERE workflow_id=?",
            (record.workflow_id,),
        )

    def start(manifest, state):
        return manifest, "running"

    first = store.mutate(
        record.workflow_id, start, expect_states={"planned"}, assign_lease="lease-1"
    )
    assert first.state == "running"
    with pytest.raises(WorkflowError, match="expected one of"):
        other.mutate(
            record.workflow_id,
            start,
            expect_states={"planned"},
            assign_lease="lease-2",
        )


def test_a_cancelled_workflow_drops_the_previous_owners_writes(
    git_repo: Path, tmp_path: Path
):
    # Cancel revokes the lease, so the worker still running cannot finalize it
    # back to completed.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE workflows SET state='planned' WHERE workflow_id=?",
            (record.workflow_id,),
        )
    store.mutate(
        record.workflow_id,
        lambda manifest, state: (manifest, "running"),
        expect_states={"planned"},
        assign_lease="lease-1",
    )
    assert store.cancel(record.workflow_id).state == "cancelled"

    with pytest.raises(WorkflowLeaseLost):
        store.mutate(
            record.workflow_id,
            lambda manifest, state: (manifest, "completed"),
            lease_id="lease-1",
        )
    assert store.load(record.workflow_id).state == "cancelled"


def test_a_torn_node_bundle_is_not_trusted(git_repo: Path, tmp_path: Path):
    # result.json is the commit marker for the patch beside it. A crash between
    # the two writes must not leave recovery believing a completed node whose
    # changes are gone.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    result = TaskResult(
        task_id=uuid4(),
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="done",
        exit_code=0,
        duration_seconds=0.1,
        patch="diff --git a/x b/x\n",
    )
    store.save_node_result(record.workflow_id, "final", result)
    assert store.node_bundle_is_intact(record.workflow_id, "final")

    patch_path = store.workflow_dir(record.workflow_id) / "nodes" / "final" / "winner.patch"
    patch_path.write_text("")  # the crash: record kept, patch lost
    assert not store.node_bundle_is_intact(record.workflow_id, "final")


def test_strict_preflight_gets_a_durable_evidence_directory(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    # Strict enforcement blocks on EVIDENCE_PERSIST_UNAVAILABLE when it has
    # nowhere to persist evidence, so passing out_dir=None would make strict
    # mode reject every workflow instead of enforcing policy.
    config = _config(git_repo, tmp_path)
    config.integration.report.enabled = True
    config.integration.report.runs_dir = str(tmp_path / "runs-report")
    seen: dict[str, object] = {}

    def _capture(cfg, *, run_id, agent_ids, out_dir):
        seen["out_dir"] = out_dir
        return None  # disabled: the coordinator must still pass a real dir

    monkeypatch.setattr(workflow_module, "run_preflight", _capture)
    coordinator = WorkflowCoordinator(config, FakeRegistry([]))
    coordinator._enforce_preflight("run-1", ["claude"])
    assert seen["out_dir"] == Path(tmp_path / "runs-report") / "run-1"


def test_a_bundle_without_a_digest_is_not_trusted(git_repo: Path, tmp_path: Path):
    # The previous writer wrote result.json FIRST, so a legacy record can sit
    # beside a missing or previous-attempt patch. Nothing in the payload tells
    # those apart from a complete one, so recovery must re-run the node.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    result = TaskResult(
        task_id=uuid4(),
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="done",
        exit_code=0,
        duration_seconds=0.1,
        patch="diff --git a/x b/x\n",
    )
    store.save_node_result(record.workflow_id, "final", result)
    path = store.workflow_dir(record.workflow_id) / "nodes" / "final" / "result.json"
    payload = json.loads(path.read_text())
    del payload["patch_sha256"]  # a record written before the digest existed
    path.write_text(json.dumps(payload))

    assert not store.node_bundle_is_intact(record.workflow_id, "final")


def test_a_planner_prompt_with_terminal_control_sequences_is_rejected():
    # The planner is an LLM and its prompt is both a subprocess argument and
    # text printed to the reviewer's terminal by `workflow show`. An OSC 52
    # sequence could rewrite the clipboard of the person approving the plan.
    with pytest.raises(ValidationError, match="control characters"):
        WorkflowSpec.model_validate(
            {
                "goal": "g",
                "nodes": [
                    {
                        "id": "final",
                        "agent": "claude",
                        "prompt": "ok\x1b]52;c;cGF5bG9hZA==\x07",
                    }
                ],
                "final_node": "final",
            }
        )
    # tabs and newlines stay legal — real prompts are multi-line
    WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [
                {"id": "final", "agent": "claude", "prompt": "line\n\tindented"}
            ],
            "final_node": "final",
        }
    )


def test_show_escapes_control_characters_stored_before_the_validator(
    git_repo: Path, tmp_path: Path
):
    # A prompt written before the validator existed is still on disk, and this
    # is the surface that prints it to a terminal.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    hostile = record.model_copy(
        update={
            "spec": record.spec.model_copy(
                update={
                    "nodes": [
                        record.spec.nodes[0].model_construct(
                            id="final",
                            agent="claude",
                            prompt="before\x1b]52;c;x\x07after",
                            needs=[],
                            timeout_seconds=None,
                            max_attempts=1,
                        )
                    ]
                }
            )
        }
    )
    rendered = render_workflow(hostile, prompts=True)
    assert "\x1b" not in rendered
    assert "\\x1b" in rendered


def test_a_dead_owners_workflow_can_be_taken_over_by_resume(
    git_repo: Path, tmp_path: Path
):
    # Nothing releases a lease when the owner is SIGKILLed, and `running` is
    # not resumable — so without a takeover path the workflow is stuck forever.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE workflows SET state='running', lease_id='dead', "
            "lease_owner=?, lease_heartbeat=? WHERE workflow_id=?",
            # a pid that cannot be running, on this host
            (f"999999999@{socket.gethostname()}", _now_iso(), record.workflow_id),
        )
    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM workflows WHERE workflow_id=?", (record.workflow_id,)
        ).fetchone()
    assert store.lease_is_stale(row, timeout_seconds=86_400)

    taken = store.mutate(
        record.workflow_id,
        lambda manifest, state: (manifest, "running"),
        expect_states={"running"},
        assign_lease="fresh",
        require_stale_lease_after=86_400,
    )
    assert taken.state == "running"


def test_a_live_owners_workflow_is_never_taken_over(git_repo: Path, tmp_path: Path):
    # The heartbeat backstop must never evict a run that is merely slow, so a
    # live pid on this host wins regardless of how old the heartbeat is.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    ancient = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE workflows SET state='running', lease_id='live', "
            "lease_owner=?, lease_heartbeat=? WHERE workflow_id=?",
            (f"{os.getpid()}@{socket.gethostname()}", ancient, record.workflow_id),
        )
        connection.row_factory = sqlite3.Row
    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM workflows WHERE workflow_id=?", (record.workflow_id,)
        ).fetchone()
    assert not store.lease_is_stale(row, timeout_seconds=1)

    with pytest.raises(WorkflowError, match="still owned by a live run"):
        store.mutate(
            record.workflow_id,
            lambda manifest, state: (manifest, "running"),
            expect_states={"running"},
            assign_lease="thief",
            require_stale_lease_after=1,
        )


def test_the_store_opens_a_database_written_before_the_new_columns(
    git_repo: Path, tmp_path: Path
):
    # CREATE TABLE IF NOT EXISTS is a no-op on an existing database, so every
    # column added after the first release needs its ALTER to run on open.
    root = tmp_path / "runs"
    directory = root / "project" / "workflows"
    directory.mkdir(parents=True)
    with sqlite3.connect(directory / "workflows.db") as connection:
        connection.execute(
            """
            CREATE TABLE workflows (
                workflow_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                state TEXT NOT NULL,
                repo_root TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                planner_agent TEXT NOT NULL,
                spec_json TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                final_run_id TEXT
            )
            """
        )

    store = WorkflowStore(root, "project")  # must migrate, not explode
    record = _terminal_workflow(store, git_repo, age_days=0)
    assert store.load(record.workflow_id).team is None


def test_a_node_prompt_is_bounded_by_the_platform_argv_budget(
    git_repo: Path, tmp_path: Path
):
    # The prompt is ONE argv element, so bounding each dependency separately
    # still lets their sum overflow into an opaque E2BIG.
    config = _config(git_repo, tmp_path)
    config.workflow.max_output_bytes = 4 * 1024 * 1024
    coordinator = WorkflowCoordinator(config, FakeRegistry([]))
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [
                {"id": "a", "agent": "claude", "prompt": "one"},
                {"id": "final", "agent": "claude", "prompt": "f", "needs": ["a"]},
            ],
            "final_node": "final",
        }
    )
    record = coordinator.store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="claude",
        planner_output="{}",
    )
    huge = TaskResult(
        task_id=uuid4(),
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="x" * (3 * 1024 * 1024),
        exit_code=0,
        duration_seconds=0.1,
    )
    coordinator.store.save_node_result(record.workflow_id, "a", huge)

    prompt = coordinator._node_prompt(record.workflow_id, spec, spec.nodes[1])
    assert _prompt_arg_units(prompt, os.name) <= _prompt_max_bytes(os.name)


def _single_node_workflow(coordinator: WorkflowCoordinator, git_repo: Path):
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
            "final_node": "final",
        }
    )
    return spec, coordinator.store.create(
        spec=spec,
        repo_root=git_repo,
        source_fingerprint=repository_fingerprint(git_repo),
        planner_agent="claude",
        planner_output="{}",
    )


def _kill_owner(store: WorkflowStore, workflow_id: str, node_state: str) -> None:
    """Leave the workflow exactly as a SIGKILLed owner would: the row still
    `running` under a lease whose pid is gone, and its node still `running`."""

    def stuck(manifest, state):
        manifest["nodes"]["final"].update(state=node_state, attempts=1)
        return manifest, "running"

    store.mutate(workflow_id, stuck, assign_lease="dead")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE workflows SET lease_owner=?, lease_heartbeat=? "
            "WHERE workflow_id=?",
            (f"999999999@{socket.gethostname()}", _now_iso(), workflow_id),
        )


def test_takeover_reclaims_a_node_bundle_the_dead_owner_had_finished(
    git_repo: Path, tmp_path: Path
):
    # The bundle scan runs BEFORE the claim marks these nodes interrupted, so a
    # scan restricted to `interrupted` would discard exactly the completed work
    # this recovery exists to reclaim, and the re-run would then be refused for
    # having spent its only attempt.
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    _, record = _single_node_workflow(coordinator, git_repo)
    finished = agent._result("already complete")
    finished.patch = ""
    coordinator.store.save_node_result(record.workflow_id, "final", finished)
    _kill_owner(coordinator.store, record.workflow_id, "running")

    taken = coordinator.run(record.workflow_id, resume=True)
    assert taken.state == "completed"
    assert agent.calls == []  # reclaimed, not re-run


def test_a_node_interrupted_before_it_finished_is_retried_not_stranded(
    git_repo: Path, tmp_path: Path
):
    # `mark_running` spends an attempt BEFORE the agent runs, so a node killed
    # mid-flight has consumed its only one (max_attempts defaults to 1) while
    # producing nothing. Leaving it spent makes every resume fail with "no
    # runnable nodes remain" — the one case resume exists for.
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    _, record = _single_node_workflow(coordinator, git_repo)
    _kill_owner(coordinator.store, record.workflow_id, "running")

    taken = coordinator.run(record.workflow_id, resume=True)
    assert taken.state == "completed"
    assert len(agent.calls) == 1  # no bundle to reclaim, so it ran once


def test_a_live_owner_is_never_evicted_even_by_a_pid_we_may_not_signal(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    # PermissionError means the process EXISTS (owned by another user), and any
    # other OSError means undecidable. Deciding either from a timestamp would
    # evict a live run, so both must count as alive.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    ancient = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE workflows SET state='running', lease_id='live', "
            "lease_owner=?, lease_heartbeat=? WHERE workflow_id=?",
            (f"4242@{socket.gethostname()}", ancient, record.workflow_id),
        )
    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM workflows WHERE workflow_id=?", (record.workflow_id,)
        ).fetchone()

    for error in (PermissionError(), OSError()):
        def refuse(pid, sig, _error=error):
            raise _error

        monkeypatch.setattr(workflow_module.os, "kill", refuse)
        assert not store.lease_is_stale(row, timeout_seconds=1)


def test_a_node_is_not_dispatched_to_a_circuit_open_agent(
    git_repo: Path, tmp_path: Path
):
    # An OPEN circuit is a decision that the agent may not run. A node is
    # pinned to one agent with nothing to fall back to, so the workflow must
    # fail with the reason rather than dispatch what an ordinary run skips.
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    config.breaker.enabled = True
    config.breaker.failure_threshold = 1
    config.breaker.state_path = str(tmp_path / "breaker.json")
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    _, record = _single_node_workflow(coordinator, git_repo)
    coordinator.breaker.record("claude", TaskStatus.FAILED)
    assert not coordinator.breaker.allows("claude")

    with pytest.raises(WorkflowError, match="circuit-open"):
        coordinator.run(record.workflow_id)
    assert agent.calls == []


def test_a_workflow_node_outcome_is_recorded_against_the_breaker(
    git_repo: Path, tmp_path: Path
):
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    config.breaker.enabled = True
    config.breaker.state_path = str(tmp_path / "breaker.json")
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    _, record = _single_node_workflow(coordinator, git_repo)

    assert coordinator.run(record.workflow_id).state == "completed"
    assert json.loads(Path(config.breaker.state_path).read_text())["claude"] == {
        "failures": 0,
        "opened_at": None,
    }


def test_a_bound_never_returns_more_than_it_was_asked_for():
    # The truncation marker is longer than the smallest bounds the prompt
    # shrink loop walks down to, and a naive slice goes NEGATIVE there.
    value = "x" * 5000
    floor = len(workflow_module._bounded_text(value, 1))
    assert floor < 100  # the marker alone, not nearly the whole input back
    for bound in (1, 10, 30, 31, 100):
        assert len(workflow_module._bounded_text(value, bound)) <= max(bound, floor)
