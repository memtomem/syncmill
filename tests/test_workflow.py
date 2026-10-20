"""Native workflow planning, validation, persistence, and final artifacts."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import socket
import threading
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from conftest import FakeRegistry
from syncmill.artifacts import ArtifactError, workflow_approved
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
from syncmill.tui_jobs import WorkflowRunSpec
from syncmill.rooms import _prompt_arg_units, _prompt_max_bytes
from syncmill.workflow import _now as _now_iso
from syncmill.worktree import repository_fingerprint

_REAL_SUBPROCESS_RUN = subprocess.run


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

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        if prompt.startswith("You are planning a SyncMill native workflow"):
            return self.execute(prompt, workdir, timeout)
        self.calls.append(prompt)
        if "This is the final node" in prompt:
            (workdir / "implemented.txt").write_text("integrated\n")
            return self._result("Implemented the integrated result.")
        return self._result("Repository inspection and recommendation.")


class PlannedWorkflowAgent(WorkflowAgent):
    def __init__(
        self, goal: str, agent_id: str, spec: dict, *, fail_once: bool = False
    ):
        super().__init__(goal)
        self.agent_id = agent_id
        self.spec = spec
        self.fail_once = fail_once
        self.runtime_calls = 0

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        self.calls.append(prompt)
        return self._result(json.dumps(self.spec))

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        if prompt.startswith("You are planning a SyncMill native workflow"):
            return self.execute(prompt, workdir, timeout)
        self.calls.append(prompt)
        self.runtime_calls += 1
        if self.fail_once and self.runtime_calls == 1:
            result = self._result("retryable failure")
            result.status = TaskStatus.FAILED
            result.exit_code = 1
            result.error = "failed"
            return result
        if "This is the final node" in prompt:
            (workdir / "implemented.txt").write_text("integrated\n")
        return self._result(f"{self.agent_id} completed")


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



def _save_bundle(store, workflow_id, node_id, result, *, lease="leasea1b2c3d4", attempt=1,
                 state=None, extra=None):
    """Write a node bundle AND record its paths, as a fenced run does.

    A bundle now lives under the lease and attempt that produced it, so nothing
    can find one that the manifest does not name.
    """
    # The writer is fenced, so the bundle can only be written by the run that
    # holds the claim — take it first, as a real run does.
    original_state = store.load(workflow_id).state
    store.mutate(workflow_id, lambda m, s: (m, "running"), assign_lease=lease)
    output_path, patch_path = store.save_node_result(
        workflow_id, node_id, result, lease_id=lease, attempt=attempt
    )

    def record(manifest, current):
        node = manifest["nodes"][node_id]
        node.update(output_path=output_path, patch_path=patch_path)
        if state is not None:
            node["state"] = state
        node.update(extra or {})
        return manifest, original_state

    store.mutate(workflow_id, record, lease_id=None)
    return output_path, patch_path


def _control_config(git_repo: Path, tmp_path: Path) -> SyncmillConfig:
    config = _config(git_repo, tmp_path)
    config.integration.toolgraph.enabled = True
    config.integration.toolgraph.mode = "strict"
    config.integration.toolgraph.control_plan_enabled = True
    config.integration.toolgraph.candidate_tools = [
        "workspace::read",
        "workspace::write",
    ]
    config.integration.toolgraph.agent_map = {"claude": "claude"}
    config.integration.trace.enabled = True
    # Strict enforcement needs somewhere durable to persist its evidence, or it
    # blocks on EVIDENCE_PERSIST_UNAVAILABLE whatever the policy says. The model
    # validator normally guarantees this pairing at construction; these fields
    # are set afterwards, so the fixture has to honour it by hand.
    config.integration.report.enabled = True
    config.integration.report.runs_dir = str(tmp_path / "runs")
    return config


def _fake_control_run(*, findings=None, calls=None):
    """Script the toolgraph binary for BOTH protocols this path can invoke.

    A workflow under a control plan still makes the legacy per-identity call at
    PLANNING, where no control plan can exist yet. Before strict enforcement
    landed that call was advisory and a miss cost a warning; now it blocks, so a
    fake that answers only the control protocol leaves planning unable to start.
    """
    def fake(argv, *args, **kwargs):
        if argv[:2] == ["toolgraph", "control-preflight"] and calls is not None:
            calls.append("control")
        if argv[:2] != ["toolgraph", "control-preflight"]:
            # The EXACT legacy shape, not "any toolgraph command with --out":
            # a fake that answers anything would hand an allow artifact to a
            # second, unintended check and leave these tests green.
            legacy = (
                len(argv) > 3
                and argv[0] == "toolgraph"
                and "--profile" in argv
                and "--run-id" in argv
                and "--out" in argv
            )
            if legacy:
                if calls is not None:
                    calls.append("legacy")
                # Built from the request rather than served from a golden: strict
                # checks that the artifact answers THIS call, so a fixed run_id
                # or agent is rejected as CONTRACT_INVALID.
                eligible = argv[3:argv.index("--profile")]
                doc = {
                    "schema_version": 1,
                    "kind": "toolgraph.preflight",
                    "run_id": argv[argv.index("--run-id") + 1],
                    "created_at": "2026-08-12T00:00:00+00:00",
                    "graph_generation": 9,
                    "agent": argv[2],
                    "agent_found": True,
                    "profile": argv[argv.index("--profile") + 1],
                    "mode": "advisory",
                    "eligible": list(eligible),
                    "rejected": [],
                    "features": [],
                    "decision": "advisory_allow",
                }
                Path(argv[argv.index("--out") + 1]).write_text(
                    json.dumps(doc), encoding="utf-8"
                )
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return _REAL_SUBPROCESS_RUN(argv, *args, **kwargs)
        plan_path = Path(argv[2])
        raw_plan = plan_path.read_bytes()
        plan = json.loads(raw_plan)
        evaluations = [
            {
                "principal": principal["id"],
                "agent_found": True,
                "eligible": list(principal["candidate_tools"]),
                "rejected": [],
            }
            for principal in plan["principals"]
        ]
        rows = list(findings or [])
        result = {
            "schema_version": 1,
            "kind": "toolgraph.control-preflight",
            "run_id": plan["run_id"],
            "created_at": "2026-08-12T00:00:00+00:00",
            "mode": "advisory",
            "profile": argv[argv.index("--profile") + 1],
            "plan_digest": f"sha256:{hashlib.sha256(raw_plan).hexdigest()}",
            "graph_state": {"instance_id": "graph-1", "generation": 9},
            "decision": "advisory_warn" if rows else "advisory_allow",
            "evaluations": evaluations,
            "findings": rows,
        }
        Path(argv[argv.index("--out") + 1]).write_text(
            json.dumps(result), encoding="utf-8"
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return fake


def _trace_spans(path: Path) -> list[dict]:
    document = json.loads(path.read_text())
    return document["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _trace_attrs(span: dict) -> dict[str, object]:
    attrs = {}
    for row in span["attributes"]:
        value = next(iter(row["value"].values()))
        attrs[row["key"]] = value
    return attrs


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
    output = (
        "before\n```json\n"
        + json.dumps(
            {
                "goal": "g",
                "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
                "final_node": "final",
            }
        )
        + "\n```"
    )
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
    assert (
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )

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
    assert not (workflow_dir / "control-plan.json").exists()
    inspect_patch = completed.manifest["nodes"]["inspect"]["patch_path"]
    assert (workflow_dir / inspect_patch).read_bytes() == b""
    assert [
        path.name for path in coordinator.artifacts.root.iterdir() if path.is_dir()
    ] == [completed.final_run_id]


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
    config = _config(git_repo, tmp_path)
    config.integration.trace.enabled = True
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
    result = agent._result("already complete")
    result.patch = ""
    _save_bundle(
        coordinator.store, record.workflow_id, "final", result,
        state="interrupted", extra={"attempts": 1},
    )

    def interrupted(manifest, state):
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, interrupted)
    completed = coordinator.run(record.workflow_id, resume=True)
    assert completed.state == "completed"
    assert completed.final_run_id
    assert agent.calls == []  # persisted completed result was reconciled, not rerun
    execution = completed.manifest["executions"][-1]
    assert execution["action"] == "resume"
    assert execution["execution_id"].startswith("wfx-")
    trace_path = (
        coordinator.store.workflow_dir(record.workflow_id)
        / "executions"
        / execution["execution_id"]
        / "trace.otlp.json"
    )
    spans = _trace_spans(trace_path)
    checkpoint = next(span for span in spans if span["name"] == "checkpoint-read:final")
    checkpoint_attrs = _trace_attrs(checkpoint)
    assert checkpoint_attrs["graph.node.id"] == "checkpoint:final"
    assert checkpoint_attrs["graph.node.parent_id"] == "run"
    assert all(not span.get("links") for span in spans)


def test_control_plan_preflight_trace_and_manifest_are_execution_scoped(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("syncmill.preflight._sync_run", _fake_control_run())
    goal = "Implement with durable control evidence"
    agent = WorkflowAgent(goal)
    coordinator = WorkflowCoordinator(
        _control_config(git_repo, tmp_path), FakeRegistry([agent])
    )

    planned = coordinator.plan(goal)
    workflow_dir = coordinator.store.workflow_dir(planned.workflow_id)
    frozen = (workflow_dir / "control-plan.json").read_bytes()
    assert b"prompt" not in frozen and goal.encode() not in frozen
    completed = coordinator.run(planned.workflow_id)

    execution = completed.manifest["executions"][-1]
    execution_id = execution["execution_id"]
    execution_dir = workflow_dir / "executions" / execution_id
    assert execution["status"] == "completed"
    assert execution["stop_reason"] == "completed"
    assert execution["workflow_id"] == planned.workflow_id
    assert execution["spec_sha256"] == planned.manifest["spec_sha256"]
    assert execution["limits"] == {
        "deadline_seconds": 3600,
        "max_parallel": 2,
        "max_output_bytes": 1024 * 1024,
    }
    assert execution["preflight"]["producer_decision"] == "advisory_allow"
    assert (
        planned.manifest["control_plan"]["spec_sha256"]
        == (planned.manifest["spec_sha256"])
    )
    assert (execution_dir / "control-plan.json").read_bytes() == frozen
    assert (execution_dir / "control-preflight.json").exists()
    assert execution["trace"]["status"] == "ok"
    spans = _trace_spans(execution_dir / "trace.otlp.json")
    root = next(span for span in spans if span["name"] == "syncmill.run")
    root_attrs = _trace_attrs(root)
    assert root_attrs["syncmill.run_id"] == execution_id
    assert root_attrs["toolgraph.graph_generation"] == "9"
    assert root_attrs["toolgraph.preflight.verdict"] == "advisory_allow"
    final_span = next(span for span in spans if span["name"] == "workflow:final")
    assert _trace_attrs(final_span)["graph.node.parent_id"] == (
        "wfnode:inspect:attempt:1"
    )
    artifact = coordinator.artifacts.load(completed.final_run_id)
    assert (
        artifact.manifest["plan"]["control_plan_digest"]
        == (planned.manifest["control_plan"]["digest"])
    )


def test_strict_control_finding_blocks_before_any_workflow_agent_call(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        "syncmill.preflight._sync_run",
        _fake_control_run(findings=[{"code": "CALL_BOUND_TOO_LOW"}]),
    )
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _control_config(git_repo, tmp_path), FakeRegistry([agent])
    )
    planned = coordinator.plan("g")

    blocked = coordinator.run(planned.workflow_id)

    assert blocked.state == "planned"
    assert len(agent.calls) == 1  # planner only
    assert all(
        node["state"] == "pending" for node in blocked.manifest["nodes"].values()
    )
    execution = blocked.manifest["executions"][-1]
    assert execution["status"] == "failed"
    assert execution["stop_reason"] == "preflight_blocked"
    assert execution["preflight"]["enforcement_decision"] == "block"


def test_a_blocked_invocation_reports_its_own_outcome_not_a_concurrent_one(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """Both pass the unlocked ``planned`` check; A runs to completion while B's
    preflight is still deciding, then B is blocked. B must neither stamp its
    rejection on A's row nor come back looking like A's success."""
    import click

    from syncmill.cli import _render_workflow_completion

    allow = _fake_control_run()
    block = _fake_control_run(findings=[{"code": "CALL_BOUND_TOO_LOW"}])
    monkeypatch.setattr("syncmill.preflight._sync_run", allow)
    config = _control_config(git_repo, tmp_path)
    coordinator_b = WorkflowCoordinator(config, FakeRegistry([WorkflowAgent("g")]))
    planned = coordinator_b.plan("g")
    workflow_id = planned.workflow_id
    coordinator_a = WorkflowCoordinator(config, FakeRegistry([WorkflowAgent("g")]))
    seen: dict = {}

    def fake(argv, *args, **kwargs):
        if argv[:2] == ["toolgraph", "control-preflight"] and not seen:
            seen["b_in_preflight"] = True  # A's own preflight must not recurse
            seen["a"] = coordinator_a.run(workflow_id)
            return block(argv, *args, **kwargs)
        return allow(argv, *args, **kwargs)

    monkeypatch.setattr("syncmill.preflight._sync_run", fake)
    b = coordinator_b.run(workflow_id)
    a = seen["a"]

    assert a.state == "completed" and a.invocation()["status"] == "completed"
    stored = coordinator_b.store.load(workflow_id)
    assert stored.state == "completed"
    assert stored.manifest["error"] is None
    assert stored.execution_id is None
    assert b.execution_id != a.execution_id
    own = b.invocation()
    assert own["status"] == "failed"
    assert own["stop_reason"] == "preflight_blocked"
    assert own["error"] == (
        "strict Toolgraph preflight blocked workflow: CONTROL_PLAN_FINDING"
    )
    with pytest.raises(click.ClickException, match="preflight blocked workflow"):
        _render_workflow_completion(b, None)
    # A's own record still renders as the success it was.
    _render_workflow_completion(a, None)


def test_a_blocked_workflow_shows_the_block_reason_in_status(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    import click

    from syncmill.cli import _render_workflow_completion

    monkeypatch.setattr(
        "syncmill.preflight._sync_run",
        _fake_control_run(findings=[{"code": "CALL_BOUND_TOO_LOW"}]),
    )
    coordinator = WorkflowCoordinator(
        _control_config(git_repo, tmp_path), FakeRegistry([WorkflowAgent("g")])
    )
    blocked = coordinator.run(coordinator.plan("g").workflow_id)

    assert blocked.state == "planned"
    with pytest.raises(click.ClickException, match="preflight blocked workflow"):
        _render_workflow_completion(blocked, None)
    reloaded = render_workflow(coordinator.store.load(blocked.workflow_id))
    assert (
        "execution_error: strict Toolgraph preflight blocked workflow: "
        "CONTROL_PLAN_FINDING"
    ) in reloaded.splitlines()


def test_mutate_returns_the_row_it_committed_not_a_later_one(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([WorkflowAgent("g")])
    )
    workflow_id = coordinator.plan("g").workflow_id
    store = coordinator.store
    real_load = store.load
    intruded: list[bool] = []

    def load_after_another_writer(wid):
        # Another invocation commits between this mutate's commit and any
        # read that follows it.
        if not intruded:
            intruded.append(True)

            def intrude(manifest, state):
                manifest["error"] = "another invocation's failure"
                return manifest, "failed"

            store.mutate(wid, intrude)
        return real_load(wid)

    monkeypatch.setattr(store, "load", load_after_another_writer)

    def own(manifest, state):
        manifest["error"] = "mine"
        return manifest, state

    returned = store.mutate(workflow_id, own)

    assert returned.state == "planned"
    assert returned.manifest["error"] == "mine"


def test_a_mutate_whose_result_cannot_be_parsed_still_commits(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([WorkflowAgent("g")])
    )
    workflow_id = coordinator.plan("g").workflow_id
    store = coordinator.store

    def unparseable(row):
        raise WorkflowError("corrupt durable state")

    def own(manifest, state):
        manifest["error"] = "mine"
        return manifest, state

    with monkeypatch.context() as patched:
        patched.setattr(store, "_from_row", unparseable)
        with pytest.raises(WorkflowError):
            store.mutate(workflow_id, own)

    assert store.load(workflow_id).manifest["error"] == "mine"


def _run_with_outcome(git_repo, tmp_path, monkeypatch, outcome_of):
    """Drive ``run`` with ``_run_nodes`` replaced: ``outcome_of`` performs this
    invocation's terminal write and returns its committed record, and may let
    another invocation write afterwards."""
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([WorkflowAgent("g")])
    )
    workflow_id = coordinator.plan("g").workflow_id
    monkeypatch.setattr(
        coordinator,
        "_run_nodes",
        lambda wid, **kwargs: outcome_of(coordinator, wid),
    )
    return coordinator, coordinator.run(workflow_id)


def test_a_failed_invocation_is_not_reported_by_a_later_resumes_success(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    import click

    from syncmill.cli import _render_workflow_completion

    def fail_then_resumed_elsewhere(coordinator, wid):
        failed = coordinator._finish_failure(wid, "node inspect failed")
        coordinator.store.mutate(
            wid, lambda manifest, state: ({**manifest, "error": None}, "completed")
        )
        return failed

    _, record = _run_with_outcome(
        git_repo, tmp_path, monkeypatch, fail_then_resumed_elsewhere
    )

    assert record.state == "completed"  # the row: someone else's outcome
    own = record.invocation()
    assert own["status"] == "failed"
    assert own["error"] == "node inspect failed"
    with pytest.raises(click.ClickException, match="node inspect failed"):
        _render_workflow_completion(record, None)


def test_finalization_returns_the_execution_update_it_committed(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    def fail_then_others_write_on_every_reload(coordinator, wid):
        failed = coordinator._finish_failure(wid, "node inspect failed")
        store = coordinator.store
        real_load = store.load

        def load_after_another_writer(workflow_id):
            store.mutate(
                workflow_id,
                lambda manifest, state: (
                    {**manifest, "error": "another invocation's"},
                    "recovery_required",
                ),
            )
            return real_load(workflow_id)

        monkeypatch.setattr(store, "load", load_after_another_writer)
        return failed

    _, record = _run_with_outcome(
        git_repo, tmp_path, monkeypatch, fail_then_others_write_on_every_reload
    )

    assert record.state == "failed"
    assert record.manifest["error"] == "node inspect failed"
    assert record.invocation()["error"] == "node inspect failed"


def test_a_cancelled_invocation_renders_as_cancelled(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    import click

    from syncmill.cli import _render_workflow_completion

    def cancelled(coordinator, wid):
        return coordinator.store.mutate(
            wid,
            lambda manifest, state: (
                {**manifest, "error": "cancelled by operator"},
                "cancelled",
            ),
        )

    _, record = _run_with_outcome(git_repo, tmp_path, monkeypatch, cancelled)

    assert record.invocation()["status"] == "cancelled"
    with pytest.raises(click.ClickException, match="was cancelled"):
        _render_workflow_completion(record, None)


def test_completion_rendering_without_or_with_an_unknown_execution(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    import click

    from syncmill.cli import _render_workflow_completion

    def failed(coordinator, wid):
        return coordinator._finish_failure(wid, "node inspect failed")

    coordinator, record = _run_with_outcome(git_repo, tmp_path, monkeypatch, failed)

    # A record not returned by run carries no execution and is judged by the
    # workflow state, as before.
    loaded = coordinator.store.load(record.workflow_id)
    assert loaded.execution_id is None and loaded.invocation() is None
    with pytest.raises(click.ClickException, match="node inspect failed"):
        _render_workflow_completion(loaded, None)
    # Naming an execution the row has no record of fails closed rather than
    # falling back to the workflow state.
    stray = loaded.model_copy(
        update={"state": "completed", "execution_id": "wfx-" + "0" * 24}
    )
    with pytest.raises(click.ClickException, match="has no record of execution"):
        _render_workflow_completion(stray, None)


def test_control_binding_and_digest_drift_fail_before_execution(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    # Planning makes the legacy per-identity call, which strict mode enforces,
    # so the binary has to be scripted even for a test about control-plan drift.
    monkeypatch.setattr("syncmill.preflight._sync_run", _fake_control_run())
    agent = WorkflowAgent("g")
    config = _control_config(git_repo, tmp_path)
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    planned = coordinator.plan("g")

    config.integration.toolgraph.candidate_tools.append("workspace::delete")
    with pytest.raises(WorkflowError, match="mapping or candidate tools changed"):
        coordinator.run(planned.workflow_id)
    assert coordinator.store.load(planned.workflow_id).manifest["executions"] == []

    config.integration.toolgraph.candidate_tools.pop()
    config.gate.commands = ["pytest -q"]
    with pytest.raises(WorkflowError, match="validation topology changed"):
        coordinator.run(planned.workflow_id)
    assert coordinator.store.load(planned.workflow_id).manifest["executions"] == []

    config.gate.commands = []
    control_path = (
        coordinator.store.workflow_dir(planned.workflow_id) / "control-plan.json"
    )
    control_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(WorkflowError, match="digest does not match"):
        coordinator.run(planned.workflow_id)
    assert len(agent.calls) == 1


def test_workflow_trace_encodes_fanin_with_one_parent_and_additional_link(
    git_repo: Path, tmp_path: Path
):
    goal = "fan out and join"
    spec = {
        "schema_version": 1,
        "goal": goal,
        "nodes": [
            {"id": "inspect", "agent": "claude", "prompt": "inspect"},
            {"id": "research", "agent": "codex", "prompt": "research"},
            {
                "id": "final",
                "agent": "claude",
                "prompt": "integrate",
                "needs": ["inspect", "research"],
            },
        ],
        "final_node": "final",
    }
    claude = PlannedWorkflowAgent(goal, "claude", spec)
    codex = PlannedWorkflowAgent(goal, "codex", spec)
    config = _config(git_repo, tmp_path)
    config.agent_priority = ["claude", "codex"]
    config.integration.trace.enabled = True
    coordinator = WorkflowCoordinator(config, FakeRegistry([claude, codex]))

    planned = coordinator.plan(goal)
    completed = coordinator.run(planned.workflow_id)

    execution_id = completed.manifest["executions"][-1]["execution_id"]
    trace_path = (
        coordinator.store.workflow_dir(planned.workflow_id)
        / "executions"
        / execution_id
        / "trace.otlp.json"
    )
    spans = _trace_spans(trace_path)
    by_name = {span["name"]: span for span in spans}
    final = by_name["workflow:final"]
    assert _trace_attrs(final)["graph.node.parent_id"] == ("wfnode:inspect:attempt:1")
    research = by_name["workflow:research"]
    assert final["links"] == [
        {"traceId": final["traceId"], "spanId": research["spanId"]}
    ]


_TIMED_SPEC = {
    "schema_version": 1,
    "goal": "timed",
    "nodes": [
        {"id": "fast", "agent": "claude", "prompt": "FAST node work."},
        {"id": "slow", "agent": "claude", "prompt": "SLOW node work."},
        {
            "id": "final",
            "agent": "claude",
            "prompt": "Integrate both.",
            "needs": ["fast", "slow"],
        },
    ],
    "final_node": "final",
}


class _TimedAgent(PlannedWorkflowAgent):
    """Reports a fixed duration per node and, unless told not to, records its
    start through the same seam ``CLIAgent.aexecute`` uses."""

    def __init__(self, spec, durations, *, record=True, fail_once=False):
        super().__init__(spec["goal"], "claude", spec, fail_once=fail_once)
        self.durations = durations
        self.record = record

    async def aexecute(self, prompt, workdir, timeout):
        if prompt.startswith("You are planning a SyncMill native workflow"):
            return self.execute(prompt, workdir, timeout)
        if self.record:
            import syncmill.agents as agents_module

            agents_module.record_invocation_start()
        result = await super().aexecute(prompt, workdir, timeout)
        result.duration_seconds = next(
            seconds for marker, seconds in self.durations.items() if marker in prompt
        )
        return result


def _node_spans(coordinator, record, name):
    execution_id = record.manifest["executions"][-1]["execution_id"]
    path = (
        coordinator.store.workflow_dir(record.workflow_id)
        / "executions"
        / execution_id
        / "trace.otlp.json"
    )
    return [
        (int(span["startTimeUnixNano"]) / 1e9, int(span["endTimeUnixNano"]) / 1e9)
        for span in _trace_spans(path)
        if span["name"] == name
    ]


def _timed_run(git_repo, tmp_path, agent):
    config = _config(git_repo, tmp_path)
    config.integration.trace.enabled = True
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    completed = coordinator.run(coordinator.plan(agent.goal).workflow_id)
    assert completed.state == "completed"
    return coordinator, completed


def test_node_spans_sit_where_each_execution_ran(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    # fast and slow share a batch; final runs in the next. Each span starts at
    # its own recorded instant and lasts its reported duration — not anchored
    # at collection time, which would put fast near the end of slow.
    clock = iter([1000.0, 1000.5, 1100.0])
    monkeypatch.setattr("syncmill.agents.time", lambda: next(clock))
    agent = _TimedAgent(
        _TIMED_SPEC, {"FAST node": 1.0, "SLOW node": 60.0, "Integrate": 2.0}
    )

    coordinator, completed = _timed_run(git_repo, tmp_path, agent)

    assert _node_spans(coordinator, completed, "workflow:fast") == [(1000.0, 1001.0)]
    assert _node_spans(coordinator, completed, "workflow:slow") == [(1000.5, 1060.5)]
    assert _node_spans(coordinator, completed, "workflow:final") == [(1100.0, 1102.0)]


def test_node_spans_fall_back_to_the_batch_start_without_a_recorded_start(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    # Runners that never reach the seam are placed at THEIR batch's fan-out
    # start. The workflow clock steps on every read, and the last reading
    # before each aexecute_each call is that batch's fan-out start.
    readings: list[float] = []

    def stepping_clock():
        readings.append(1000.0 * (len(readings) + 1))
        return readings[-1]

    monkeypatch.setattr(workflow_module, "time", stepping_clock)
    fanout_starts: list[float] = []
    real_aexecute_each = workflow_module.aexecute_each

    def observed_aexecute_each(*args, **kwargs):
        fanout_starts.append(readings[-1])
        return real_aexecute_each(*args, **kwargs)

    monkeypatch.setattr(workflow_module, "aexecute_each", observed_aexecute_each)
    agent = _TimedAgent(
        _TIMED_SPEC,
        {"FAST node": 1.0, "SLOW node": 60.0, "Integrate": 2.0},
        record=False,
    )

    coordinator, completed = _timed_run(git_repo, tmp_path, agent)

    first, second = fanout_starts
    assert first != second
    assert _node_spans(coordinator, completed, "workflow:fast") == [
        (first, first + 1.0)
    ]
    assert _node_spans(coordinator, completed, "workflow:slow") == [
        (first, first + 60.0)
    ]
    assert _node_spans(coordinator, completed, "workflow:final") == [
        (second, second + 2.0)
    ]


def test_a_retried_node_span_carries_its_own_recorded_start(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    spec = {
        "schema_version": 1,
        "goal": "retry once",
        "nodes": [
            {
                "id": "final",
                "agent": "claude",
                "prompt": "finish",
                "max_attempts": 2,
            }
        ],
        "final_node": "final",
    }
    clock = iter([500.0, 600.0])
    monkeypatch.setattr("syncmill.agents.time", lambda: next(clock))
    agent = _TimedAgent(spec, {"finish": 3.0}, fail_once=True)

    coordinator, completed = _timed_run(git_repo, tmp_path, agent)

    assert _node_spans(coordinator, completed, "workflow:final") == [
        (500.0, 503.0),
        (600.0, 603.0),
    ]


def test_workflow_trace_retry_is_caused_by_previous_failed_attempt(
    git_repo: Path, tmp_path: Path
):
    goal = "retry once"
    spec = {
        "schema_version": 1,
        "goal": goal,
        "nodes": [
            {
                "id": "final",
                "agent": "claude",
                "prompt": "finish",
                "max_attempts": 2,
            }
        ],
        "final_node": "final",
    }
    agent = PlannedWorkflowAgent(goal, "claude", spec, fail_once=True)
    config = _config(git_repo, tmp_path)
    config.integration.trace.enabled = True
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))

    planned = coordinator.plan(goal)
    completed = coordinator.run(planned.workflow_id)

    assert completed.state == "completed"
    execution_id = completed.manifest["executions"][-1]["execution_id"]
    trace_path = (
        coordinator.store.workflow_dir(planned.workflow_id)
        / "executions"
        / execution_id
        / "trace.otlp.json"
    )
    attempts = [
        span for span in _trace_spans(trace_path) if span["name"] == "workflow:final"
    ]
    assert len(attempts) == 2
    assert _trace_attrs(attempts[0])["graph.node.id"] == "wfnode:final:attempt:1"
    assert _trace_attrs(attempts[1])["graph.node.parent_id"] == (
        "wfnode:final:attempt:1"
    )


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
    assert "expected one of" in next(
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
            "UPDATE workflows SET lease_owner='999999@remote-host', "
            "lease_heartbeat='2000-01-01T00:00:00+00:00' WHERE workflow_id=?",
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
    _, patch_rel = _save_bundle(store, record.workflow_id, "final", result, state="completed")
    patch_path = store.workflow_dir(record.workflow_id) / patch_rel
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


def test_legacy_running_row_is_preserved_until_explicit_cancel(
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
    assert migrated.state == "running"
    assert migrated.manifest == legacy_manifest
    assert migrated.lease_owner is None
    assert store.recover_expired(migrated.workflow_id) == []
    assert store.cancel(migrated.workflow_id).state == "cancelled"


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
    outcome = coordinator.run(record.workflow_id, resume=True)
    assert outcome.state == "failed"
    assert "permitted agent calls" in outcome.manifest["error"]
    assert agent.calls == []
    assert coordinator.store.load(record.workflow_id).manifest["calls_used"] == 2


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
        workflow_preflight_warnings = []

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
        workflow_preflight_warnings = []

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
    result = CliRunner().invoke(cli, ["run", "millflow: goal", "--strategy", "compete"])
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
    output_rel, patch_rel = _save_bundle(store, record.workflow_id, "final", result)
    assert store.node_bundle_is_intact(record.workflow_id, "final", output_rel, patch_rel)

    (store.workflow_dir(record.workflow_id) / patch_rel).write_text("")  # patch lost
    assert not store.node_bundle_is_intact(record.workflow_id, "final", output_rel, patch_rel)


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
    output_rel, patch_rel = _save_bundle(store, record.workflow_id, "final", result)
    path = store.workflow_dir(record.workflow_id) / output_rel
    payload = json.loads(path.read_text())
    del payload["patch_sha256"]  # a record written before the digest existed
    path.write_text(json.dumps(payload))

    assert not store.node_bundle_is_intact(record.workflow_id, "final", output_rel, patch_rel)


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


def test_show_escapes_bidi_controls_and_the_stored_error(git_repo: Path, tmp_path: Path):
    # The prompt regex used to cover C0/C1 only, so a bidi override reached the
    # terminal; the recorded error (a provider's text) was printed raw.
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
                            prompt="first line\n\tfine‮evil\rforged",
                            needs=[],
                            timeout_seconds=None,
                            max_attempts=1,
                        )
                    ]
                }
            ),
            "manifest": {**record.manifest, "error": "provider\u009b2J\nsaid no"},
        }
    )
    rendered = render_workflow(hostile, prompts=True)
    for raw in ("‮", "\r", "\u009b"):
        assert raw not in rendered
    lines = rendered.splitlines()
    assert "      first line" in lines
    assert "      \tfine\\u202eevil\\x0dforged" in lines
    assert "error: provider\\x9b2J" in lines and "said no" in lines


def test_show_escapes_the_latest_execution_and_the_goal(git_repo: Path, tmp_path: Path):
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    hostile = "x\u009b2J‮\x1b]52;c;QQ\x07"
    manifest = {
        **record.manifest,
        "error": None,
        "executions": [
            {
                "execution_id": "e" + hostile, "action": "run", "status": "failed",
                "stop_reason": "stop" + hostile,
                "error": "provider said\n" + hostile,
                "preflight": {"status": "warn" + hostile, "graph_generation": 1},
                "trace": {"status": "ok", "trace_id": "t" + hostile},
            }
        ],
    }
    shown = record.model_copy(
        update={"manifest": manifest, "spec": record.spec.model_copy(update={"goal": "g\n" + hostile})}
    )
    rendered = render_workflow(shown)
    for raw in ("\u009b", "‮", "\x1b", "\x07"):
        assert raw not in rendered
    lines = rendered.splitlines()
    assert "goal: g\\x0ax\\x9b2J\\u202e\\x1b]52;c;QQ\\x07" in lines
    assert "execution_error: provider said" in lines


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
    _save_bundle(coordinator.store, record.workflow_id, "a", huge)
    nodes = coordinator.store.load(record.workflow_id).manifest["nodes"]

    prompt = coordinator._node_prompt(
        record.workflow_id,
        spec,
        spec.nodes[1],
        nodes,
        max_output_bytes=coordinator.config.workflow.max_output_bytes,
    )
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
    _save_bundle(coordinator.store, record.workflow_id, "final", finished)
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


def test_a_completed_workflow_stamps_approval_on_its_final_artifact(
    git_repo: Path, tmp_path: Path
):
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    planned = coordinator.plan("Implement one integrated change")
    completed = coordinator.run(planned.workflow_id)

    assert completed.state == "completed"
    manifest = coordinator.artifacts.load(completed.final_run_id).manifest
    assert workflow_approved(manifest)


def test_the_apply_seam_refuses_an_unapproved_workflow_artifact(
    git_repo: Path, tmp_path: Path
):
    # The authority is the LOCKED apply path, not the CLI helper above it: a
    # direct caller of the public ArtifactStore.apply must be refused too.
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    planned = coordinator.plan("Implement one integrated change")
    completed = coordinator.run(planned.workflow_id)
    run_id = completed.final_run_id
    assert run_id is not None

    artifact = coordinator.artifacts.load(run_id)
    manifest = artifact.manifest
    manifest["plan"] = {
        key: value for key, value in manifest["plan"].items() if key != "approved"
    }
    coordinator.artifacts._write_manifest(artifact.path, manifest)

    with pytest.raises(ArtifactError, match="not approved for apply"):
        coordinator.artifacts.apply(
            run_id, git_repo, repository_fingerprint(git_repo)
        )


def test_a_legacy_claim_with_no_recorded_owner_is_never_taken_over(
    git_repo: Path, tmp_path: Path
):
    # A row claimed before the owner/heartbeat columns existed says nothing
    # about liveness. A pre-upgrade process may still be running it right now,
    # so "no evidence" must not read as "abandoned" — that would evict a live
    # owner, and its unfenced node-bundle writes would then race the new one.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE workflows SET state='running', lease_id='legacy', "
            "lease_owner=NULL, lease_heartbeat=NULL WHERE workflow_id=?",
            (record.workflow_id,),
        )
        connection.row_factory = sqlite3.Row
    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM workflows WHERE workflow_id=?", (record.workflow_id,)
        ).fetchone()

    assert not store.lease_is_stale(row, timeout_seconds=1)


def test_a_durably_failed_node_is_reinstated_and_keeps_its_attempt(
    git_repo: Path, tmp_path: Path
):
    # The node RAN and recorded a failure; only the manifest write was lost.
    # Refunding its attempt would let a node that always fails be handed a
    # fresh attempt by every resume, so max_attempts would never bite.
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    _, record = _single_node_workflow(coordinator, git_repo)
    failed = agent._result("it broke")
    failed.status = TaskStatus.FAILED
    failed.error = "node failed"
    failed.patch = ""
    _save_bundle(coordinator.store, record.workflow_id, "final", failed)
    _kill_owner(coordinator.store, record.workflow_id, "running")

    taken = coordinator.run(record.workflow_id, resume=True)
    assert taken.state == "failed"
    node = taken.manifest["nodes"]["final"]
    assert node["state"] == "failed"
    assert node["attempts"] == 1  # spent, not refunded
    assert agent.calls == []  # the recorded outcome stood; no re-run


def test_a_half_open_agent_gets_one_trial_not_the_whole_batch(
    git_repo: Path, tmp_path: Path
):
    # allows() is side-effect-free, so without trimming, every node pinned to a
    # half-open agent dispatches at once — spending one cooldown's single trial
    # on the whole batch, which is the pile of calls the back-off exists to stop.
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    config.breaker.enabled = True
    config.breaker.failure_threshold = 1
    config.breaker.cooldown_seconds = 0  # straight to HALF_OPEN
    config.breaker.state_path = str(tmp_path / "breaker.json")
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    coordinator.breaker.record("claude", TaskStatus.FAILED)
    assert coordinator.breaker.is_half_open("claude")

    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [
                {"id": "a", "agent": "claude", "prompt": "a"},
                {"id": "b", "agent": "claude", "prompt": "b"},
                {"id": "final", "agent": "claude", "prompt": "f", "needs": ["a", "b"]},
            ],
            "final_node": "final",
        }
    )
    assert [node.id for node in coordinator._trim_half_open(list(spec.nodes))] == ["a"]

    coordinator.breaker.record("claude", TaskStatus.COMPLETED)  # closed again
    assert len(coordinator._trim_half_open(list(spec.nodes))) == 3


def test_a_node_bundle_write_is_refused_once_the_lease_is_gone(
    git_repo: Path, tmp_path: Path
):
    # The bundle is a FILE pair at a path derived from the node id, so it is not
    # fenced by the lease the way every SQLite write is. A run that has lost the
    # workflow must stop here rather than write its stale patch over the new
    # owner's, which the new owner's manifest already names.
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    _, record = _single_node_workflow(coordinator, git_repo)
    coordinator.store.mutate(
        record.workflow_id, lambda m, s: (m, s), assign_lease="currentaaaa"
    )

    with pytest.raises(WorkflowLeaseLost):
        coordinator.store.save_node_result(
            record.workflow_id,
            "final",
            agent._result("stale"),
            lease_id="staleaaaabbbb",
            attempt=1,
        )
    ours = coordinator.store.save_node_result(
        record.workflow_id,
        "final",
        agent._result("ours"),
        lease_id="currentaaaa",
        attempt=1,
    )
    # And even a stale owner that DOES write — paused past its lease check —
    # cannot land on the bundle this run recorded: the path carries the lease.
    stale_dir = coordinator.store.workflow_dir(record.workflow_id) / "nodes" / "final"
    theirs = [
        path for path in stale_dir.iterdir() if not path.name.startswith("currentaaaa")
    ]
    assert theirs == []  # the refused write left nothing behind
    assert "currentaaaa" in ours[0]


def test_a_running_workflow_keeps_beating_while_a_node_runs(
    git_repo: Path, tmp_path: Path
):
    # The beat has to survive the fan-out: a node may legitimately run far
    # longer than the lease timeout, and a claim nobody refreshes in that window
    # reads as abandoned to a contender that cannot ask this host's pid.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    store.mutate(record.workflow_id, lambda m, s: (m, "running"), assign_lease="held")

    def beat_at() -> str:
        with sqlite3.connect(store.db_path) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT lease_heartbeat FROM workflows WHERE workflow_id=?",
                (record.workflow_id,),
            ).fetchone()["lease_heartbeat"]

    before = beat_at()
    beating = workflow_module._Heartbeat(
        store, record.workflow_id, "held", interval_seconds=0.01
    )
    beating.start()
    try:
        deadline = monotonic() + 10
        while beat_at() == before and monotonic() < deadline:
            sleep(0.01)
    finally:
        beating.stop()
    assert beat_at() != before


def test_a_heartbeat_that_loses_the_lease_stops_instead_of_fighting(tmp_path: Path):
    # Driven by the beat itself, not by a sleep: the interval has a one-second
    # floor, so waiting a fraction of a second proves only that nothing had
    # happened yet — the test would pass with the lease-loss handling deleted.
    beat = threading.Event()

    class LostLease:
        def heartbeat(self, workflow_id: str, lease_id: str) -> None:
            beat.set()
            raise WorkflowLeaseLost("taken over")

    quiet = workflow_module._Heartbeat(
        LostLease(), "wf-0123456789abcdef", "held", interval_seconds=0.01
    )
    quiet.start()
    try:
        assert beat.wait(10), "the heartbeat never ticked"
        stopped = monotonic() + 10
        while quiet._thread.is_alive() and monotonic() < stopped:
            sleep(0.01)
        assert not quiet._thread.is_alive()  # it stood down for the new owner
    finally:
        quiet.stop()


def test_stopping_a_heartbeat_that_never_started_is_safe():
    # The run's `finally` calls stop() unconditionally, including on the path
    # where start() itself raised.
    workflow_module._Heartbeat(None, "wf-0123456789abcdef", "l", interval_seconds=1).stop()


def test_dispatches_are_counted_across_resumes_and_bound_the_budget(
    git_repo: Path, tmp_path: Path
):
    # `attempts` is refunded to a node interrupted without an outcome, which is
    # right for retry accounting but would make the approved call budget
    # resettable: every resume would hand back what the last one spent.
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    config.workflow.max_agent_calls = 2
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    _, record = _single_node_workflow(coordinator, git_repo)

    def spent(manifest_state, count):
        def bump(manifest, state):
            manifest["calls_used"] = count
            manifest["nodes"]["final"].update(state="interrupted", attempts=0)
            return manifest, manifest_state

        coordinator.store.mutate(record.workflow_id, bump)

    # ONE call spent: the planner. The budget of 2 covers it plus one node
    # call, which an interrupted node already used — so the resume, which
    # refunds the ATTEMPT, must not also refund the call.
    spent("recovery_required", 2)
    out = coordinator.run(record.workflow_id, resume=True)
    assert out.state == "failed"
    assert "2 of 2 permitted agent calls" in out.manifest["error"]
    assert agent.calls == []


def test_the_planner_call_counts_against_the_dispatch_budget(
    git_repo: Path, tmp_path: Path
):
    # `max_agent_calls` is validated as attempts + 1, the +1 being the planner,
    # so a counter that starts at zero permits one call beyond the plan.
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    config.workflow.max_agent_calls = 2
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    _, record = _single_node_workflow(coordinator, git_repo)
    assert coordinator.store.load(record.workflow_id).manifest["calls_used"] == 1

    def interrupted(manifest, state):
        manifest["nodes"]["final"].update(state="interrupted", attempts=0)
        manifest["calls_used"] = 2  # planner + the node call that was cut off
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, interrupted)
    out = coordinator.run(record.workflow_id, resume=True)
    assert out.state == "failed"
    assert agent.calls == []  # the third call is one more than the plan allows


def test_the_approver_refuses_an_artifact_claiming_another_workflow(
    git_repo: Path, tmp_path: Path
):
    # The stamp is the authorization, so the approver takes the caller's
    # statement of WHICH workflow it verified and refuses to write it onto an
    # artifact that claims a different one — or none at all.
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    planned = coordinator.plan("Implement one integrated change")
    completed = coordinator.run(planned.workflow_id)
    run_id = completed.final_run_id

    with pytest.raises(ArtifactError, match="not wf-abcdef0123456789"):
        coordinator.artifacts.approve_workflow_run(
            run_id, git_repo, workflow_id="wf-abcdef0123456789"
        )

    artifact = coordinator.artifacts.load(run_id)
    manifest = artifact.manifest
    manifest["plan"] = {"origin": "workflow"}  # workflow-bound, but names none
    coordinator.artifacts._write_manifest(artifact.path, manifest)
    with pytest.raises(ArtifactError, match="does not identify the workflow"):
        coordinator.artifacts.approve_workflow_run(
            run_id, git_repo, workflow_id=planned.workflow_id
        )


def test_a_bundle_must_belong_to_the_node_that_names_it(git_repo: Path, tmp_path: Path):
    # Containment is not identity. A manifest is durable state, and one that is
    # corrupt can name a path sitting perfectly well inside this workflow that
    # belongs to ANOTHER node — or pair one attempt's record with another
    # attempt's patch. Either publishes an output beside a diff that did not
    # produce it, which the digest check cannot catch: it would be comparing a
    # matched pair.
    store = WorkflowStore(tmp_path / "runs", "project")
    spec = WorkflowSpec.model_validate(
        {
            "goal": "g",
            "nodes": [
                {"id": "other", "agent": "claude", "prompt": "o"},
                {"id": "final", "agent": "claude", "prompt": "f", "needs": ["other"]},
            ],
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
    result = TaskResult(
        task_id=uuid4(),
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="done",
        exit_code=0,
        duration_seconds=0.1,
        patch="diff --git a/x b/x\n",
    )
    theirs_out, theirs_patch = _save_bundle(store, record.workflow_id, "other", result)
    mine_out, mine_patch = _save_bundle(
        store, record.workflow_id, "final", result, lease="leaseb5c6d7e8", attempt=2
    )

    # Another node's perfectly valid, digest-matching bundle.
    assert store.resolve_bundle(record.workflow_id, "final", theirs_out, theirs_patch) is None
    assert not store.node_bundle_is_intact(
        record.workflow_id, "final", theirs_out, theirs_patch
    )
    with pytest.raises(WorkflowError, match="does not name a bundle of its own"):
        store.read_node_output(record.workflow_id, "final", theirs_out, theirs_patch)

    # One attempt's record beside ANOTHER ATTEMPT of the same node's patch —
    # both legitimately this node's, which is why the node check alone cannot
    # catch it, and why the digest check downstream would not either: it would
    # be comparing a matched pair.
    later_out, later_patch = _save_bundle(
        store, record.workflow_id, "final", result, lease="leasec9d0e1f2", attempt=3
    )
    assert later_out != mine_out
    assert store.resolve_bundle(record.workflow_id, "final", mine_out, later_patch) is None
    assert store.resolve_bundle(record.workflow_id, "final", later_out, mine_patch) is None
    # Escapes, and files that are not a bundle's two names.
    assert store.resolve_bundle(record.workflow_id, "final", "../../etc/passwd", mine_patch) is None
    assert store.resolve_bundle(record.workflow_id, "final", mine_patch, mine_patch) is None
    # Its own pair still resolves.
    assert store.resolve_bundle(record.workflow_id, "final", mine_out, mine_patch)


def test_a_bundle_written_before_namespacing_is_still_readable(
    git_repo: Path, tmp_path: Path
):
    # Manifests written by earlier builds of this branch name
    # nodes/<id>/result.json, and those bundles are perfectly good.
    store = WorkflowStore(tmp_path / "runs", "project")
    record = _terminal_workflow(store, git_repo, age_days=0)
    legacy = store.workflow_dir(record.workflow_id) / "nodes" / "final"
    legacy.mkdir(parents=True)
    (legacy / "winner.patch").write_bytes(b"")
    (legacy / "result.json").write_text(
        json.dumps({"output": "old", "status": "completed", "patch_sha256": hashlib.sha256(b"").hexdigest()})
    )

    assert store.read_node_output(
        record.workflow_id, "final", "nodes/final/result.json", "nodes/final/winner.patch"
    ) == "old"
    assert store.node_bundle_is_intact(
        record.workflow_id, "final", "nodes/final/result.json", "nodes/final/winner.patch"
    )


@pytest.mark.parametrize(
    "shape",
    [
        pytest.param({}, id="no counter at all"),
        pytest.param({"dispatches": 1}, id="one, meaning either thing"),
        pytest.param({"dispatches": 3}, id="three node calls, or two plus planner"),
    ],
)
def test_a_workflow_whose_call_accounting_is_ambiguous_is_refused(
    git_repo: Path, tmp_path: Path, shape: dict
):
    # The counter's meaning changed twice, and only the last shape says so. The
    # middle two are indistinguishable — 1 is both "one node call, planner
    # uncounted" and "the planner, nothing dispatched" — and every attempt to
    # infer it broke a real case in one direction or the other. `max_agent_calls`
    # is a bound the user approved, so an unreadable count declines to run
    # rather than quietly guessing which side of that bound it sits on.
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    _, record = _single_node_workflow(coordinator, git_repo)

    def as_an_older_build_wrote_it(manifest, state):
        manifest["schema_version"] = 1
        manifest.pop("calls_used", None)
        manifest.pop(workflow_module._PLANNER_COUNTED, None)
        manifest.update(shape)
        manifest["nodes"]["final"].update(state="interrupted", attempts=1)
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, as_an_older_build_wrote_it)

    with pytest.raises(WorkflowError, match="plan it again"):
        coordinator.run(record.workflow_id, resume=True)
    assert agent.calls == []
    # Refused BEFORE the claim, so the workflow is left exactly as it was for
    # whoever re-plans — not stranded in `running` under a lease.
    after = coordinator.store.load(record.workflow_id)
    assert after.state == "recovery_required"


def test_a_refunded_attempt_cannot_buy_a_call_past_the_cap(
    git_repo: Path, tmp_path: Path
):
    # The shape that made the earlier attempts-derived floor unsafe: the counter
    # says three calls happened while the node's attempt was given back, so
    # nothing in the per-node state accounts for them. With the counter trusted
    # (it carries the marker) the budget still holds.
    agent = WorkflowAgent("g")
    config = _config(git_repo, tmp_path)
    config.workflow.max_agent_calls = 4
    coordinator = WorkflowCoordinator(config, FakeRegistry([agent]))
    _, record = _single_node_workflow(coordinator, git_repo)

    def spent_but_refunded(manifest, state):
        manifest["calls_used"] = 4  # the whole budget, already used
        manifest["nodes"]["final"].update(
            state="interrupted", attempts=0, output_path=None, patch_path=None
        )
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, spent_but_refunded)
    out = coordinator.run(record.workflow_id, resume=True)

    assert out.state == "failed"
    assert "4 of 4 permitted agent calls" in out.manifest["error"]
    assert agent.calls == []


def test_a_refused_workflow_can_still_be_cancelled(git_repo: Path, tmp_path: Path):
    # Refusal happens before the claim, so a refused workflow keeps whatever
    # state it had — which for a `running` one is nonterminal, and prune only
    # collects terminal workflows. The error names the way out, and it works.
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    _, record = _single_node_workflow(coordinator, git_repo)

    def as_an_older_build_left_it(manifest, state):
        manifest["schema_version"] = 1
        manifest.pop("calls_used", None)
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, as_an_older_build_left_it)
    with pytest.raises(WorkflowError, match="workflow cancel"):
        coordinator.run(record.workflow_id, resume=True)

    assert coordinator.store.cancel(record.workflow_id).state == "cancelled"


def test_each_phase_makes_exactly_one_preflight_call(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """One probe per execution, and it is the right protocol for that phase.

    Planning checks the planner's identity, because the control plan describes a
    DAG that does not exist until the planner has produced it. Execution has the
    frozen plan, so the control protocol replaces the per-identity fan-out rather
    than joining it. The failure this pins is a second legacy check during
    execution, which would re-decide a settled question and spend another probe.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        "syncmill.preflight._sync_run", _fake_control_run(calls=calls)
    )
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _control_config(git_repo, tmp_path), FakeRegistry([agent])
    )

    planned = coordinator.plan("g")
    assert calls == ["legacy"], calls

    calls.clear()
    coordinator.run(planned.workflow_id)
    assert calls == ["control"], calls


def test_a_control_preflight_warning_reaches_the_operator(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """The control path is the only one that can report an advisory degradation.

    Under a control plan the legacy call returns early, so if the control
    outcome's warnings are not copied out, an operator whose policy ALLOWS an
    unavailable Toolgraph sees the run proceed with no explanation at all. A
    stale warning left by a previous operation on the same coordinator is the
    other half: it must be replaced, not appended to.
    """
    def unavailable(argv, *a, **k):
        if argv[:2] == ["toolgraph", "control-preflight"]:
            raise FileNotFoundError("toolgraph")
        return _fake_control_run()(argv, *a, **k)

    config = _control_config(git_repo, tmp_path)
    config.integration.toolgraph.unavailable_policy = "warn"
    coordinator = WorkflowCoordinator(config, FakeRegistry([WorkflowAgent("g")]))
    monkeypatch.setattr("syncmill.preflight._sync_run", _fake_control_run())
    planned = coordinator.plan("g")

    coordinator.preflight_warnings = ["a warning from a previous operation"]
    monkeypatch.setattr("syncmill.preflight._sync_run", unavailable)
    coordinator.run(planned.workflow_id)

    stale = "a warning from a previous operation"
    assert stale not in coordinator.preflight_warnings, coordinator.preflight_warnings
    assert coordinator.preflight_warnings, "an unavailable probe must say so"


def test_a_probe_the_policy_allowed_is_not_traced_as_a_block(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """"Blocked" is a claim about the run, not about the probe.

    With an unavailability policy that allows the run, the nodes execute. A span
    asserting the preflight blocked then sits next to the work it says was
    stopped. The error status stays — a failed probe is worth seeing — but only
    an actual enforcement decision gets to call itself a block.
    """
    def unavailable(argv, *a, **k):
        if argv[:2] == ["toolgraph", "control-preflight"]:
            raise FileNotFoundError("toolgraph")
        return _fake_control_run()(argv, *a, **k)

    config = _control_config(git_repo, tmp_path)
    config.integration.toolgraph.unavailable_policy = "warn"
    coordinator = WorkflowCoordinator(config, FakeRegistry([WorkflowAgent("g")]))
    monkeypatch.setattr("syncmill.preflight._sync_run", _fake_control_run())
    planned = coordinator.plan("g")
    monkeypatch.setattr("syncmill.preflight._sync_run", unavailable)
    record = coordinator.run(planned.workflow_id)

    execution = record.manifest["executions"][-1]
    spans = _trace_spans(
        coordinator.store.workflow_dir(record.workflow_id)
        / "executions"
        / execution["execution_id"]
        / "trace.otlp.json"
    )
    preflight_spans = [
        s for s in spans if _trace_attrs(s).get("syncmill.phase") == "preflight"
    ]
    assert preflight_spans, [s["name"] for s in spans]
    # Assert the whole shape, not just the absence of the old word: a span that
    # lost its error status entirely would satisfy a "not blocked" check while
    # hiding a probe that failed.
    for span in preflight_spans:
        status = span.get("status", {})
        assert status.get("code") == "STATUS_CODE_ERROR", status
        assert status.get("message") == "control_preflight_failed", status
        assert _trace_attrs(span).get("syncmill.status") == "failed", span


def test_a_failed_patch_capture_keeps_the_node_workspace(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """`capture_changes` raises; it does not return "files but no patch".

    That contract changed while this branch was open, and the `finally` calls
    `pool.cleanup_all()` unconditionally — so an escaping `PatchCaptureError`
    deleted the only worktree still holding the node's work. `Supervisor.run`
    retains on exactly this exception, and a workflow node has no less to lose.
    """
    from syncmill.worktree import PatchCaptureError

    def boom(path, base=None):
        raise PatchCaptureError("patch capture failed: simulated git failure")

    monkeypatch.setattr("syncmill.strategies.base.capture_changes", boom)

    goal = "Implement one integrated change"
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([WorkflowAgent(goal)])
    )
    planned = coordinator.plan(goal)

    with pytest.raises(WorkflowError, match="retained for recovery"):
        coordinator.run(planned.workflow_id)

    workspaces = tmp_path / "workspaces"
    retained = list(workspaces.rglob(".syncmill-recovery"))
    assert retained, sorted(p.name for p in workspaces.rglob("*"))[:20]


class _CRLFWorkflowAgent(WorkflowAgent):
    """A final node that writes CRLF line endings and a non-UTF-8 byte."""

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        if "This is the final node" in prompt:
            self.calls.append(prompt)
            (workdir / "crlf.txt").write_bytes(b"one\r\ntwo\r\ncaf\xe9\r\n")
            return self._result("Implemented the integrated result.")
        return await super().aexecute(prompt, workdir, timeout)


def test_the_final_patch_is_published_byte_for_byte(git_repo: Path, tmp_path: Path):
    """#143: finalization read the captured patch as text with newline translation.

    A final node that wrote CRLF lines published LF ones instead, so the
    artifact no longer matched what the node produced and could fail to apply.
    """
    agent = _CRLFWorkflowAgent("Write a CRLF file")
    coordinator = WorkflowCoordinator(_config(git_repo, tmp_path), FakeRegistry([agent]))
    completed = coordinator.run(coordinator.plan(agent.goal).workflow_id)
    assert completed.state == "completed"

    workflow_dir = coordinator.store.workflow_dir(completed.workflow_id)
    captured = (workflow_dir / completed.manifest["nodes"]["final"]["patch_path"]).read_bytes()
    assert b"+one\r\n" in captured and b"caf\xe9" in captured, captured
    assert coordinator.artifacts.patch(completed.final_run_id) == captured


def _completed_final_node_awaiting_resume(coordinator, git_repo: Path):
    """A workflow whose final node was saved completed before the run stopped.

    Resume reconciles only interrupted nodes, so a completed one reaches
    finalization with no integrity check except the one finalization makes.
    """
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
    result = WorkflowAgent("g")._result("done")
    result.patch = "diff --git a/kept b/kept\n"
    _, patch_rel = _save_bundle(
        coordinator.store, record.workflow_id, "final", result,
        state="completed", extra={"attempts": 1},
    )
    coordinator.store.mutate(record.workflow_id, lambda m, s: (m, "recovery_required"))
    return record, coordinator.store.workflow_dir(record.workflow_id) / patch_rel


def test_resume_refuses_to_publish_a_completed_node_whose_patch_changed(
    git_repo: Path, tmp_path: Path
):
    """#144: a completed final node's patch replaced at rest is never published."""
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(_config(git_repo, tmp_path), FakeRegistry([agent]))
    record, patch_path = _completed_final_node_awaiting_resume(coordinator, git_repo)
    patch_path.write_bytes(b"diff --git a/replaced b/replaced\n")

    with pytest.raises(
        WorkflowError,
        match="final node artifact is missing or corrupt: .*digest mismatch for node 'final'",
    ):
        coordinator.run(record.workflow_id, resume=True)
    assert coordinator.store.load(record.workflow_id).state == "recovery_required"
    root = coordinator.artifacts.root
    assert not root.exists() or not any(path.is_dir() for path in root.iterdir())
    assert agent.calls == []


def test_finalization_publishes_the_bytes_it_verified(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    """#144: a patch replaced after verification must not ride a fresh digest.

    Finalization checked the bundle and then read the patch again to publish
    it, so a replacement landing in between was published under digests
    computed from the replacement.
    """
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(_config(git_repo, tmp_path), FakeRegistry([agent]))
    record, patch_path = _completed_final_node_awaiting_resume(coordinator, git_repo)
    verified = patch_path.read_bytes()
    record_bytes = (patch_path.parent / "result.json").read_bytes()
    read_node_bundle = coordinator.store.read_node_bundle

    def replace_after_verifying(workflow_id, node_id, output_path, patch_rel):
        bundle = read_node_bundle(workflow_id, node_id, output_path, patch_rel)
        if node_id == "final":
            patch_path.write_bytes(b"diff --git a/replaced b/replaced\n")
        return bundle

    monkeypatch.setattr(coordinator.store, "read_node_bundle", replace_after_verifying)
    completed = coordinator.run(record.workflow_id, resume=True)

    assert completed.state == "completed"
    assert coordinator.artifacts.patch(completed.final_run_id) == verified
    plan = coordinator.artifacts.load(completed.final_run_id).manifest["plan"]
    assert plan["patch_sha256"] == hashlib.sha256(verified).hexdigest()
    assert plan["bundle_sha256"] == hashlib.sha256(
        record_bytes + b"\0" + verified
    ).hexdigest()


# ── prune vs. concurrent invocations ────────────────────────────────────────


def _failed_workflow(git_repo, tmp_path, *, coordinator=None):
    coordinator = coordinator or WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([WorkflowAgent("g")])
    )
    workflow_id = coordinator.plan("g").workflow_id
    coordinator.store.mutate(
        workflow_id, lambda manifest, state: ({**manifest, "error": "boom"}, "failed")
    )
    return coordinator, workflow_id


def _prune_all(store):
    return store.prune(keep=0, retention_days=0, dry_run=False)


def _claim(store, workflow_id):
    return store.mutate(
        workflow_id,
        lambda manifest, state: (manifest, "running"),
        expect_states={"failed"},
        assign_lease="lease-claimed",
    )


def _lock_path(store, workflow_id):
    return store.root / ".locks" / f"{workflow_id}.lock"


def test_prune_removes_an_untouched_terminal_workflow(git_repo: Path, tmp_path: Path):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store

    assert _prune_all(store) == [workflow_id]

    assert not store.workflow_dir(workflow_id).exists()
    with pytest.raises(WorkflowError, match="unknown workflow"):
        store.load(workflow_id)
    # Never unlinked: a fresh inode per acquire would split the lock.
    assert _lock_path(store, workflow_id).exists()


def test_prune_skips_a_workflow_claimed_after_selection(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    real_list = store.list

    def list_then_claim():
        selected = real_list()
        _claim(store, workflow_id)
        return selected

    monkeypatch.setattr(store, "list", list_then_claim)

    assert _prune_all(store) == []

    record = store.load(workflow_id)
    assert record.state == "running" and record.lease_id == "lease-claimed"
    assert (store.workflow_dir(workflow_id) / "spec.json").is_file()


def test_prune_skips_a_workflow_that_failed_again_after_selection(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    real_list = store.list

    def list_then_claim_and_fail():
        selected = real_list()
        _claim(store, workflow_id)
        coordinator._finish_failure(workflow_id, "failed again")
        return selected

    monkeypatch.setattr(store, "list", list_then_claim_and_fail)

    assert _prune_all(store) == []

    assert store.load(workflow_id).state == "failed"
    assert (store.workflow_dir(workflow_id) / "spec.json").is_file()


def test_prune_skips_a_workflow_with_an_invocation_in_flight(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    seen: dict = {}

    def nodes_that_prune(wid, **kwargs):
        # The invocation has begun its execution and written under the
        # workflow directory; the row is still the selected terminal one.
        seen["inner"] = _prune_all(store)
        return coordinator._finish_failure(wid, "node failed")

    monkeypatch.setattr(coordinator, "_run_nodes", nodes_that_prune)

    coordinator.run(workflow_id, resume=True)

    assert seen["inner"] == []
    assert (store.workflow_dir(workflow_id) / "spec.json").is_file()
    assert _prune_all(store) == [workflow_id]


def test_a_failed_invocation_releases_its_lock(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)

    def crash(wid, **kwargs):
        raise RuntimeError("crash")

    monkeypatch.setattr(coordinator, "_run_nodes", crash)
    with pytest.raises(RuntimeError, match="crash"):
        coordinator.run(workflow_id, resume=True)

    assert _prune_all(coordinator.store) == [workflow_id]


def test_prune_holds_the_workflow_while_removing_its_directory(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    import fcntl

    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    other_id = coordinator.plan("g").workflow_id  # planned: not prunable
    store = coordinator.store
    real_rmtree = workflow_module.shutil.rmtree
    real_flock = workflow_module._flock
    arrived = threading.Event()
    outcome: dict = {}

    def observed_flock(fd, flags):
        if flags & fcntl.LOCK_SH and not flags & fcntl.LOCK_NB:
            arrived.set()
        real_flock(fd, flags)

    def resume():
        try:
            coordinator.run(workflow_id, resume=True)
        except BaseException as exc:  # noqa: BLE001 - the outcome is the assertion
            outcome["error"] = exc

    def paused_rmtree(path, *args, **kwargs):
        if Path(path) != store.workflow_dir(workflow_id):
            return real_rmtree(path, *args, **kwargs)
        # The lock is held exclusively for the whole removal ...
        fd = os.open(_lock_path(store, workflow_id), os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        # ... a resume that arrives now waits on it ...
        runner = threading.Thread(target=resume)
        runner.start()
        assert arrived.wait(10)
        runner.join(0.2)
        assert runner.is_alive()
        outcome["runner"] = runner
        # ... and no SQLite lock is held, so other workflows' writers go on.
        store.mutate(other_id, lambda manifest, state: (manifest, state))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workflow_module, "_flock", observed_flock)
    monkeypatch.setattr(workflow_module.shutil, "rmtree", paused_rmtree)

    removed = store.prune(keep=0, retention_days=0, dry_run=False)
    outcome["runner"].join(10)

    assert removed == [workflow_id]
    assert not outcome["runner"].is_alive()
    assert isinstance(outcome["error"], WorkflowError)
    assert "unknown workflow" in str(outcome["error"])
    assert not store.workflow_dir(workflow_id).exists()


def test_a_partly_removed_workflow_stays_unrunnable_until_prune_finishes(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    directory = store.workflow_dir(workflow_id)

    def partial_rmtree(path, *args, **kwargs):
        (Path(path) / "spec.json").unlink()
        raise PermissionError("cannot remove the rest")

    with monkeypatch.context() as patched:
        patched.setattr(workflow_module.shutil, "rmtree", partial_rmtree)
        assert _prune_all(store) == []

    assert store.load(workflow_id).state == "pruning"
    with pytest.raises(WorkflowError, match="from state 'pruning'"):
        coordinator.run(workflow_id, resume=True)
    assert not (directory / "executions").exists()
    with pytest.raises(WorkflowError, match="being pruned"):
        store.cancel(workflow_id)
    # Finished on the next prune, however young and however many are kept.
    assert store.prune(keep=100, retention_days=365, dry_run=True) == [workflow_id]
    assert store.prune(keep=100, retention_days=365, dry_run=False) == [workflow_id]
    assert not directory.exists()
    with pytest.raises(WorkflowError, match="unknown workflow"):
        store.load(workflow_id)


def _interrupted_tombstone(store, workflow_id):
    assert store._tombstone(store.load(workflow_id))
    shutil.rmtree(store.workflow_dir(workflow_id))


def _recreate(store, record):
    return store.create(
        spec=record.spec,
        repo_root=Path(record.repo_root),
        source_fingerprint=record.source_fingerprint,
        planner_agent=record.planner_agent,
        planner_output="replanned",
        workflow_id=record.workflow_id,
    )


def test_a_stale_prune_does_not_remove_a_workflow_recreated_under_its_id(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    original = store.load(workflow_id)
    _interrupted_tombstone(store, workflow_id)
    stale = store.list()

    assert _prune_all(store) == [workflow_id]
    _recreate(store, original)
    monkeypatch.setattr(store, "list", lambda: stale)

    assert store.prune(keep=100, retention_days=365, dry_run=False) == []

    assert store.load(workflow_id).state == "planned"
    assert (store.workflow_dir(workflow_id) / "spec.json").is_file()


def test_creating_a_workflow_excludes_a_retrying_prune(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    original = store.load(workflow_id)
    _interrupted_tombstone(store, workflow_id)
    real_write = workflow_module._atomic_write
    seen: dict = {}

    def write_then_prune(path, data):
        real_write(path, data)
        if Path(path).name == "planner-output.txt":
            # The new files exist and the INSERT has not run yet.
            seen["inner"] = store.prune(keep=100, retention_days=365, dry_run=False)

    monkeypatch.setattr(workflow_module, "_atomic_write", write_then_prune)
    with pytest.raises(sqlite3.IntegrityError):
        _recreate(store, original)

    assert seen["inner"] == []
    assert store.load(workflow_id).state == "pruning"
    assert not store.workflow_dir(workflow_id).exists()
    monkeypatch.setattr(workflow_module, "_atomic_write", real_write)
    assert store.prune(keep=100, retention_days=365, dry_run=False) == [workflow_id]


def test_prune_skips_a_workflow_another_process_is_running(
    git_repo: Path, tmp_path: Path
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    ready = tmp_path / "ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_SH)\n"
            "open(sys.argv[2], 'w').close()\n"
            "sys.stdin.read()\n",
            str(_lock_path(store, workflow_id)),
            str(ready),
        ],
        stdin=subprocess.PIPE,
    )
    try:
        _lock_path(store, workflow_id).parent.mkdir(exist_ok=True)
        deadline = monotonic() + 10
        while not ready.exists():
            assert monotonic() < deadline, "lock holder never became ready"
            sleep(0.01)
        assert _prune_all(store) == []
        assert (store.workflow_dir(workflow_id) / "spec.json").is_file()
    finally:
        holder.stdin.close()
        holder.wait(10)

    assert _prune_all(store) == [workflow_id]


def _busy(message="database is locked"):
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return exc


def test_a_busy_tombstone_skips_only_that_workflow(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, first = _failed_workflow(git_repo, tmp_path)
    _, second = _failed_workflow(git_repo, tmp_path, coordinator=coordinator)
    store = coordinator.store
    real_tombstone = store._tombstone

    def busy_for_first(record):
        if record.workflow_id == first:
            raise _busy()
        return real_tombstone(record)

    monkeypatch.setattr(store, "_tombstone", busy_for_first)

    assert _prune_all(store) == [second]
    assert store.load(first).state == "failed"


def test_a_non_busy_database_error_is_not_swallowed(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, _ = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store

    def broken(record):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_tombstone", broken)

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _prune_all(store)


def test_a_busy_final_delete_keeps_the_tombstone_for_the_next_prune(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    real_connect = sqlite3.connect
    real_rmtree = workflow_module.shutil.rmtree
    blocker = real_connect(store.db_path, timeout=0, isolation_level=None)

    def rmtree_then_lock_database(path, *args, **kwargs):
        real_rmtree(path, *args, **kwargs)
        blocker.execute("BEGIN IMMEDIATE")

    try:
        with monkeypatch.context() as patched:
            patched.setattr(
                sqlite3,
                "connect",
                lambda *args, **kwargs: real_connect(*args, **{**kwargs, "timeout": 0.05}),
            )
            patched.setattr(
                workflow_module.shutil, "rmtree", rmtree_then_lock_database
            )
            assert _prune_all(store) == []
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert store.load(workflow_id).state == "pruning"
    assert _prune_all(store) == [workflow_id]


def test_a_stale_prune_does_not_remove_a_new_incarnation_at_the_same_revision(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    original = store.load(workflow_id)
    stale = store.list()

    assert _prune_all(store) == [workflow_id]
    _recreate(store, original)
    store.mutate(
        workflow_id, lambda manifest, state: ({**manifest, "error": "boom"}, "failed")
    )
    recreated = store.load(workflow_id)
    assert (recreated.state, recreated.revision) == (original.state, original.revision)
    monkeypatch.setattr(store, "list", lambda: stale)

    assert _prune_all(store) == []

    assert store.load(workflow_id).created_at == recreated.created_at
    assert (store.workflow_dir(workflow_id) / "spec.json").is_file()


def test_a_thread_cannot_wait_on_a_workflow_lock_it_already_holds(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store
    original = store.load(workflow_id)
    outcome: dict = {}

    def nodes_that_recreate(wid, **kwargs):
        try:
            _recreate(store, original)
        except BaseException as exc:  # noqa: BLE001 - the outcome is the assertion
            outcome["error"] = exc
        return coordinator._finish_failure(wid, "node failed")

    monkeypatch.setattr(coordinator, "_run_nodes", nodes_that_recreate)
    runner = threading.Thread(
        target=coordinator.run, args=(workflow_id,), kwargs={"resume": True}, daemon=True
    )
    runner.start()
    runner.join(10)

    assert not runner.is_alive(), "create waited on its own thread's run lock"
    assert isinstance(outcome["error"], WorkflowError)
    assert "already locked by this thread" in str(outcome["error"])
    # Shared under shared stays allowed: a run nested in a run, as in the
    # issue-140 race, still proceeds.
    with store.workflow_lock(workflow_id, exclusive=False):
        with store.workflow_lock(workflow_id, exclusive=False):
            pass


def test_a_busy_final_delete_skips_only_that_workflow(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, first = _failed_workflow(git_repo, tmp_path)
    _, second = _failed_workflow(git_repo, tmp_path, coordinator=coordinator)
    store = coordinator.store
    real_delete = store._delete_tombstoned

    def busy_for_first(workflow_id):
        if workflow_id == first:
            raise _busy()
        return real_delete(workflow_id)

    monkeypatch.setattr(store, "_delete_tombstoned", busy_for_first)

    assert _prune_all(store) == [second]
    assert store.load(first).state == "pruning"


def test_a_non_busy_final_delete_error_is_not_swallowed(
    git_repo: Path, tmp_path: Path, monkeypatch
):
    coordinator, workflow_id = _failed_workflow(git_repo, tmp_path)
    store = coordinator.store

    def broken(wid):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_delete_tombstoned", broken)

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _prune_all(store)
    assert store.load(workflow_id).state == "pruning"
