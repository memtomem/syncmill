"""PR #107 integration: durable state must survive upgrades and interrupted writes."""

from __future__ import annotations

import hashlib
import json
import threading

import pytest

import syncmill.workflow as workflow_module
from syncmill.models import TaskStatus, TokenUsage
from syncmill.workflow import WorkflowError, WorkflowLeaseLost, WorkflowStore
from test_workflow import FakeRegistry, WorkflowAgent, _config, _single_node_workflow
from syncmill.workflow import WorkflowCoordinator


@pytest.fixture
def workflow(git_repo, tmp_path):
    agent = WorkflowAgent("g")
    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([agent])
    )
    _, record = _single_node_workflow(coordinator, git_repo)
    return coordinator, record, agent


def raw_manifest(store, workflow_id, manifest, *, state="planned"):
    with store._connect() as connection:
        connection.execute(
            "UPDATE workflows SET manifest_json=?,state=? WHERE workflow_id=?",
            (json.dumps(manifest), state, workflow_id),
        )


def claim(store, workflow_id, *, resume=False, job_id=None):
    return store.claim_execution(
        workflow_id,
        resume=resume,
        owner=None,
        job_id=job_id,
        lease_timeout_seconds=1800,
        deadline_seconds=3600,
        recovered_results={},
    )


@pytest.mark.parametrize("count", [0, 1, 6])
def test_marked_v1_migration_preserves_exact_count_and_control_fields(workflow, count):
    coordinator, record, _ = workflow
    store = coordinator.store
    raw = record.manifest.copy()
    raw.update(
        schema_version=1,
        dispatches=count,
        dispatches_include_planner=True,
        executions=[{"execution_id": "previous", "status": "completed"}],
        control_plan={"artifact": "control-plan.json", "digest": "saved"},
        execution_limits={"max_parallel": 2},
    )
    raw.pop("calls_used")
    raw_manifest(store, record.workflow_id, raw)
    WorkflowStore(store.root.parent.parent, store.root.parent.name)
    migrated = store.load(record.workflow_id)
    assert migrated.manifest["schema_version"] == 3
    assert migrated.manifest["calls_used"] == count
    assert migrated.manifest["executions"] == raw["executions"]
    assert migrated.manifest["control_plan"] == raw["control_plan"]
    assert migrated.manifest["execution_limits"] == raw["execution_limits"]
    with store._connect() as connection:
        before = tuple(connection.execute("SELECT * FROM workflows").fetchone())
    WorkflowStore(store.root.parent.parent, store.root.parent.name)
    with store._connect() as connection:
        assert tuple(connection.execute("SELECT * FROM workflows").fetchone()) == before


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("count", [None, 1, 6])
def test_ambiguous_old_counts_never_reach_a_provider(workflow, version, count):
    coordinator, record, agent = workflow
    raw = record.manifest.copy()
    raw["schema_version"] = version
    raw.pop("calls_used")
    if count is not None:
        raw["calls_used" if version == 2 else "dispatches"] = count
    raw_manifest(coordinator.store, record.workflow_id, raw)
    with pytest.raises(WorkflowError, match="plan it again"):
        coordinator.run(record.workflow_id)
    assert agent.calls == []
    assert coordinator.store.load(record.workflow_id).manifest == raw
    assert coordinator.store.cancel(record.workflow_id).state == "cancelled"


@pytest.mark.parametrize("count", [None, True, -1, 1.2, "2"])
def test_v3_requires_an_explicit_integer_count(workflow, count):
    coordinator, record, agent = workflow
    raw = record.manifest.copy()
    if count is None:
        raw.pop("calls_used")
    else:
        raw["calls_used"] = count
    raw_manifest(coordinator.store, record.workflow_id, raw)
    with pytest.raises(WorkflowError, match="corrupt"):
        coordinator.run(record.workflow_id)
    assert agent.calls == []


def test_open_does_not_rewrite_a_marked_active_legacy_row(workflow):
    coordinator, record, _ = workflow
    store = coordinator.store
    raw = record.manifest.copy()
    raw.update(schema_version=1, dispatches=4, dispatches_include_planner=True)
    raw.pop("calls_used")
    raw_manifest(store, record.workflow_id, raw, state="running")
    with store._connect() as connection:
        before = tuple(connection.execute("SELECT * FROM workflows").fetchone())
    WorkflowStore(store.root.parent.parent, store.root.parent.name)
    with store._connect() as connection:
        assert tuple(connection.execute("SELECT * FROM workflows").fetchone()) == before
    assert store.recover_expired(record.workflow_id) == []


def test_resume_rechecks_live_owner_after_stale_pretransaction_read(
    workflow, monkeypatch
):
    coordinator, record, _ = workflow
    store = coordinator.store
    store.mutate(record.workflow_id, lambda m, s: (m, "recovery_required"))
    load = store.load
    stale = load(record.workflow_id)
    winner = claim(store, record.workflow_id, resume=True)
    monkeypatch.setattr(store, "load", lambda ident: stale)
    with pytest.raises(WorkflowError, match="live run"):
        claim(store, record.workflow_id, resume=True)
    monkeypatch.setattr(store, "load", load)
    assert load(record.workflow_id).lease_id == winner.lease_id


def test_late_writer_cannot_overwrite_new_owners_bundle(workflow, monkeypatch):
    coordinator, record, agent = workflow
    store = coordinator.store
    first = claim(store, record.workflow_id)
    paused, proceed = threading.Event(), threading.Event()
    original = workflow_module._atomic_write
    errors = []

    def pause(path, data):
        if first.lease_id in str(path) and path.name == "winner.patch":
            paused.set()
            assert proceed.wait(10)
        return original(path, data)

    monkeypatch.setattr(workflow_module, "_atomic_write", pause)

    def late_writer():
        try:
            store.save_node_result(
                record.workflow_id,
                "final",
                agent._result("old"),
                lease_id=first.lease_id,
                attempt=1,
            )
            store.mutate(
                record.workflow_id,
                lambda m, s: (m, "completed"),
                lease_id=first.lease_id,
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=late_writer)
    thread.start()
    try:
        assert paused.wait(10)
        # Simulate takeover after the first writer passed its early lease check.
        second_lease = "newlease01234567"
        store.mutate(
            record.workflow_id, lambda m, s: (m, "running"), assign_lease=second_lease
        )
        output, patch = store.save_node_result(
            record.workflow_id,
            "final",
            agent._result("new"),
            lease_id=second_lease,
            attempt=1,
        )

        def link(manifest, state):
            manifest["nodes"]["final"].update(
                output_path=output, patch_path=patch, state="completed"
            )
            return manifest, state

        store.mutate(record.workflow_id, link, lease_id=second_lease)
    finally:
        proceed.set()
        thread.join(10)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], WorkflowLeaseLost)
    assert store.read_node_result(record.workflow_id, "final")["output"] == "new"


@pytest.mark.parametrize("outcome", [TaskStatus.COMPLETED, TaskStatus.FAILED])
def test_recovery_uses_only_reserved_attempt_and_counts_usage_once(workflow, outcome):
    coordinator, record, agent = workflow
    store = coordinator.store
    claimed = claim(store, record.workflow_id)
    prefix = f"nodes/final/{claimed.lease_id}-1"

    def reserve(manifest, state):
        manifest["calls_used"] += 1
        manifest["nodes"]["final"].update(
            state="running",
            attempts=1,
            output_path=f"{prefix}/result.json",
            patch_path=f"{prefix}/winner.patch",
            bundle_version=3,
        )
        return manifest, state

    store.mutate(record.workflow_id, reserve, lease_id=claimed.lease_id)
    result = agent._result("recorded")
    result.status = outcome
    result.usage = TokenUsage(total_input_tokens=9, output_tokens=4, total_cost_usd=0.1)
    store.save_node_result(
        record.workflow_id, "final", result, lease_id=claimed.lease_id, attempt=1
    )
    # A second complete directory is deliberately not reserved by the manifest.
    store.save_node_result(
        record.workflow_id,
        "final",
        agent._result("unreferenced"),
        lease_id=claimed.lease_id,
        attempt=2,
    )
    store.mutate(record.workflow_id, lambda m, s: (m, "recovery_required"))

    def interrupted(manifest, state):
        manifest["nodes"]["final"]["state"] = "interrupted"
        return manifest, state

    store.mutate(record.workflow_id, interrupted)
    finished = coordinator.run(record.workflow_id, resume=True)
    assert finished.state == (
        "completed" if outcome == TaskStatus.COMPLETED else "failed"
    )
    assert agent.calls == []
    assert finished.input_tokens == 9 and finished.output_tokens == 4
    assert finished.total_cost_usd == pytest.approx(0.1)
    assert finished.manifest["calls_used"] == 2
    assert finished.manifest["nodes"]["final"]["attempts"] == 1
    store.mutate(record.workflow_id, lambda m, s: (m, "recovery_required"))
    again = coordinator.run(record.workflow_id, resume=True)
    assert again.input_tokens == 9 and again.manifest["calls_used"] == 2


def test_interrupted_uncommitted_attempt_is_detached_and_retried(workflow):
    coordinator, record, agent = workflow
    store = coordinator.store
    claimed = claim(store, record.workflow_id)
    prefix = f"nodes/final/{claimed.lease_id}-1"

    def reserve(manifest, state):
        manifest["calls_used"] += 1
        manifest["nodes"]["final"].update(
            state="interrupted",
            attempts=1,
            output_path=f"{prefix}/result.json",
            patch_path=f"{prefix}/winner.patch",
            bundle_version=3,
        )
        return manifest, "recovery_required"

    store.mutate(record.workflow_id, reserve)
    directory = store.workflow_dir(record.workflow_id) / prefix
    directory.mkdir(parents=True)
    (directory / "winner.patch").write_bytes(b"")
    (directory / "result.json").write_text(
        json.dumps(
            {"status": "completed", "patch_sha256": hashlib.sha256(b"").hexdigest()}
        )
    )
    finished = coordinator.run(record.workflow_id, resume=True)
    assert finished.state == "completed"
    assert len(agent.calls) == 1
    assert finished.manifest["calls_used"] == 3
    assert finished.manifest["nodes"]["final"]["output_path"] != f"{prefix}/result.json"


def test_expired_deadline_does_not_reset_on_resume(workflow):
    coordinator, record, agent = workflow

    def expire(manifest, state):
        manifest["deadline_at"] = "2000-01-01T00:00:00+00:00"
        return manifest, "recovery_required"

    coordinator.store.mutate(record.workflow_id, expire)
    finished = coordinator.run(record.workflow_id, resume=True)
    assert finished.state == "failed"
    assert "deadline exceeded" in finished.manifest["error"]
    assert finished.manifest["deadline_at"].startswith("2000-")
    assert agent.calls == []


def test_call_reservations_cannot_be_refunded(workflow):
    coordinator, record, _ = workflow

    def refund(manifest, state):
        manifest["calls_used"] = 0
        return manifest, state

    with pytest.raises(WorkflowError, match="cannot be refunded"):
        coordinator.store.mutate(record.workflow_id, refund)
    assert coordinator.store.load(record.workflow_id).manifest["calls_used"] == 1


def cli_supervisor(monkeypatch, coordinator):
    from types import SimpleNamespace

    supervisor = SimpleNamespace(
        workflow_store=coordinator.store,
        registry=coordinator.registry,
        workflow_preflight_warnings=[],
        close=lambda: None,
    )
    monkeypatch.setattr(
        "syncmill.cli._workflow_supervisor",
        lambda repo, team: (supervisor, coordinator.config),
    )
    return supervisor


def test_follow_tail_does_not_replay_rotated_log(workflow, monkeypatch):
    from click.testing import CliRunner
    from syncmill.cli import cli

    coordinator, record, _ = workflow
    cli_supervisor(monkeypatch, coordinator)
    monkeypatch.setattr("syncmill.cli._observe_workflow_job", lambda *args: None)
    coordinator.store.mutate(record.workflow_id, lambda m, s: (m, "running"))
    current, previous = coordinator.store.node_log_paths(record.workflow_id, "final")
    current.parent.mkdir(parents=True)
    current.write_text("old\nkeep\n")

    def rotate(_):
        current.replace(previous)
        current.write_text("new\n")
        coordinator.store.mutate(record.workflow_id, lambda m, s: (m, "completed"))

    monkeypatch.setattr("syncmill.cli.time.sleep", rotate)
    result = CliRunner().invoke(
        cli, ["workflow", "logs", record.workflow_id, "--follow", "--tail", "1"]
    )
    assert result.exit_code == 0, result.output
    assert result.output == "keep\nnew\n"


def test_logs_escape_terminal_controls_in_node_output(workflow, monkeypatch):
    # A node log is agent output: its lines and tabs stay, its controls are
    # shown, never sent to the terminal.
    from click.testing import CliRunner
    from syncmill.cli import cli

    coordinator, record, _ = workflow
    cli_supervisor(monkeypatch, coordinator)
    monkeypatch.setattr("syncmill.cli._observe_workflow_job", lambda *args: None)
    current, _ = coordinator.store.node_log_paths(record.workflow_id, "final")
    current.parent.mkdir(parents=True)
    current.write_bytes("ok\n\tcol\x1b[2J\u009b31m\u202e\rx\n".encode() + b"\xff\n")
    result = CliRunner().invoke(cli, ["workflow", "logs", record.workflow_id])
    assert result.exit_code == 0, result.output
    assert result.output == "ok\n\tcol\\x1b[2J\\x9b31m\\u202e\\x0dx\n�\n"


_HOSTILE_ERROR = "boom\x1b[2J\u009b31m\u202e\rforged\nsecond line"
_SHOWN_ERROR = "boom\\x1b[2J\\x9b31m\\u202e\\x0dforged\\x0asecond line"


def test_a_planner_failure_prints_the_provider_error_escaped_on_one_line(
    git_repo, tmp_path, monkeypatch
):
    # The planner's error is provider text, carried to the CLI's Error: line.
    from types import SimpleNamespace
    from uuid import uuid4

    from click.testing import CliRunner
    from syncmill.cli import cli
    from syncmill.models import TaskResult

    class FailingPlanner(WorkflowAgent):
        async def aexecute(self, prompt, workdir, timeout):
            return TaskResult(
                task_id=uuid4(), agent_id=self.agent_id, status=TaskStatus.FAILED,
                output="", error=_HOSTILE_ERROR, exit_code=1, duration_seconds=0.0,
            )

    coordinator = WorkflowCoordinator(
        _config(git_repo, tmp_path), FakeRegistry([FailingPlanner("g")])
    )
    supervisor = SimpleNamespace(
        plan_workflow=lambda goal, planner_agent=None: coordinator.plan(
            goal, planner_agent=planner_agent
        ),
        workflow_preflight_warnings=[],
        close=lambda: None,
    )
    monkeypatch.setattr(
        "syncmill.cli._workflow_supervisor",
        lambda repo, team: (supervisor, coordinator.config),
    )
    result = CliRunner().invoke(cli, ["workflow", "plan", "g"], color=True)
    assert result.exit_code == 1
    assert result.output.splitlines()[-1] == f"Error: {_SHOWN_ERROR}"


def test_following_a_job_replays_worker_output_and_its_error_escaped(
    tmp_path, monkeypatch
):
    # The foreground follower replays the worker's console.log, then prints
    # the job's error; both are worker text. Run under the root group, whose
    # Error: handling is part of the path.
    from types import SimpleNamespace

    import click
    from click.testing import CliRunner
    from syncmill import cli as cli_module

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "console.log").write_bytes(_HOSTILE_ERROR.encode() + b"\n")
    store = SimpleNamespace(
        active_jobs=lambda: None,
        list_jobs=lambda: [{"job_id": "j1", "status": "finished", "exit_code": 1,
                            "error": _HOSTILE_ERROR}],
    )

    @click.command("follow-probe")
    def follow_probe():
        cli_module._follow_workflow_job(
            store, None, {"job_id": "j1", "job_dir": str(job_dir)}, "wf-1"
        )

    monkeypatch.setitem(cli_module.cli.commands, "follow-probe", follow_probe)
    result = CliRunner().invoke(cli_module.cli, ["follow-probe"], color=True)
    assert result.exit_code == 1
    replay = _SHOWN_ERROR.replace("\\x0a", "\n")
    assert result.output == f"{replay}\nError: {_SHOWN_ERROR}\n"


def test_watch_exits_if_latest_job_failed_before_claim(workflow, monkeypatch):
    from types import SimpleNamespace
    from click.testing import CliRunner
    from syncmill.cli import cli

    coordinator, record, _ = workflow
    cli_supervisor(monkeypatch, coordinator)
    jobs = SimpleNamespace(
        active_jobs=lambda: [],
        list_jobs=lambda: [
            {
                "op": "workflow-run",
                "request": {"workflow_id": record.workflow_id},
                "status": "launch_failed",
                "job_id": "early",
            }
        ],
    )
    monkeypatch.setattr("syncmill.cli._workflow_job_store", lambda *args: jobs)
    monkeypatch.setattr(
        "syncmill.cli.time.sleep", lambda _: pytest.fail("watch did not terminate")
    )
    result = CliRunner().invoke(
        cli, ["workflow", "status", record.workflow_id, "--watch"]
    )
    assert result.exit_code == 0, result.output
    assert "ended in launch_failed before claiming" in result.output
    assert coordinator.store.load(record.workflow_id).state == "planned"


def test_detached_launch_carries_selected_team_config(workflow, monkeypatch):
    from types import SimpleNamespace
    from click.testing import CliRunner
    from syncmill.cli import cli

    coordinator, record, _ = workflow
    supervisor = cli_supervisor(monkeypatch, coordinator)
    seen = {}

    def select(repo, team):
        seen["team"] = team
        return supervisor, coordinator.config

    def launch(spec, *, resolved_config):
        seen.update(spec=spec, config=resolved_config)
        return {"job_id": "detached-job"}

    monkeypatch.setattr("syncmill.cli._workflow_supervisor", select)
    monkeypatch.setattr(
        "syncmill.cli._workflow_job_store", lambda *args: SimpleNamespace(launch=launch)
    )
    result = CliRunner().invoke(
        cli, ["workflow", "run", record.workflow_id, "--detach", "--team", "blue"]
    )
    assert result.exit_code == 0, result.output
    assert seen["team"] == "blue"
    assert seen["config"] is coordinator.config
    assert seen["spec"].workflow_id == record.workflow_id


def test_gate_deadline_prevents_later_commands(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import syncmill.gates as gates
    from syncmill.gates import CommandGate

    clock = [0.0]
    launched = []
    monkeypatch.setattr(gates, "monotonic", lambda: clock[0])

    def run(argv, **kwargs):
        launched.append((argv, kwargs["timeout"]))
        clock[0] += 2
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gates, "_sync_run", run)
    report = CommandGate(["first", "second"], 30, total_timeout_seconds=1).evaluate(
        SimpleNamespace(path=tmp_path, agent_id="claude")
    )
    assert launched == [(["first"], 1.0)]
    assert not report.passed
    assert "deadline exceeded before gate" in report.checks[1].output


@pytest.mark.integration
@pytest.mark.skipif(
    __import__("os").environ.get("SYNCMILL_TEST_TUI_PROCESS") != "1",
    reason="opt-in detached workflow process test",
)
def test_detached_workflow_executes_with_frozen_environment(
    workflow, tmp_path, monkeypatch
):
    import os
    import sys
    import time
    from syncmill.tui_jobs import JobStore, WorkflowRunSpec

    coordinator, record, _ = workflow
    config = coordinator.config
    config.tui.state_dir = str(tmp_path / "jobs")
    config.memory.db_path = str(tmp_path / "memory.db")
    config.claude.workflow_env_allowlist = ["WORKFLOW_TEST_ALLOWED"]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "claude"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os\nfrom pathlib import Path\n"
        "assert os.environ['WORKFLOW_TEST_ALLOWED'] == 'selected'\n"
        "assert 'WORKFLOW_TEST_DENIED' not in os.environ\n"
        "assert 'SYNCMILL_TUI_JOB_ID' not in os.environ\n"
        "Path('done.txt').write_text('done\\n')\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'done', 'usage': {'input_tokens': 7, 'output_tokens': 3}, 'total_cost_usd': 0.01}))\n"
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("WORKFLOW_TEST_ALLOWED", "selected")
    monkeypatch.setenv("WORKFLOW_TEST_DENIED", "excluded")
    jobs = JobStore(config.tui, coordinator.repo_root)
    launched = jobs.launch(WorkflowRunSpec(record.workflow_id), resolved_config=config)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            jobs.active_jobs()
            row = next(
                item
                for item in jobs.list_jobs()
                if item["job_id"] == launched["job_id"]
            )
            if row["status"] in {
                "finished",
                "lost",
                "cancelled",
                "launch_failed",
                "force_stopped",
            }:
                break
            time.sleep(0.1)
        else:
            pytest.fail("detached workflow did not finish")
        from pathlib import Path

        console = (Path(row["job_dir"]) / "console.log").read_text()
        assert row["status"] == "finished" and row["exit_code"] == 0, console
        finished = coordinator.store.load(record.workflow_id)
        assert finished.state == "completed", console
        assert finished.call_count == 2
        assert finished.input_tokens == 7 and finished.output_tokens == 3
        assert b"done.txt" in coordinator.artifacts.patch(finished.final_run_id)
        assert not (coordinator.repo_root / "done.txt").exists()
        assert jobs.active_jobs() == []
    finally:
        for active in jobs.active_jobs():
            if active["job_id"] == launched["job_id"]:
                jobs.control(active["job_id"], "stop")


def test_foreground_rotation(tmp_path, monkeypatch, capsys):
    import importlib
    from types import SimpleNamespace
    from syncmill.tui_jobs import _RollingLog

    cli = importlib.import_module('syncmill.cli')
    writer = _RollingLog(tmp_path, 1024)
    writer.write(b'A' * 512)
    calls = 0
    def active_jobs():
        nonlocal calls
        calls += 1
        if calls == 1:
            writer.write(b'FINAL RESULT\n')
        return []
    store = SimpleNamespace(active_jobs=active_jobs, list_jobs=lambda: [{'job_id':'job', 'status':'finished' if calls >= 2 else 'running', 'exit_code':0}])
    monkeypatch.setattr(cli.time, 'sleep', lambda _: None)
    cli._follow_workflow_job(store, None, {'job_id':'job','job_dir':str(tmp_path)}, 'workflow')
    writer.close()
    assert capsys.readouterr().out == 'A' * 512 + 'FINAL RESULT\n'


@pytest.mark.parametrize("damage", [None, "source", "patch", "partial", "identity"])
def test_resume_legacy_final_artifact_verifies_before_upgrade(workflow, damage):
    coordinator, record, agent = workflow
    completed = coordinator.run(record.workflow_id)
    calls = len(agent.calls)
    artifact = coordinator.artifacts.load(completed.final_run_id)
    for key in ("source_fingerprint", "bundle_sha256", "patch_sha256"):
        artifact.manifest["plan"].pop(key)
    if damage == "source":
        artifact.manifest["source_fingerprint"] = "wrong-source"
    elif damage == "patch":
        (artifact.path / "winner.patch").write_bytes(b"wrong patch")
    elif damage == "partial":
        artifact.manifest["plan"]["patch_sha256"] = "partial-provenance"
    elif damage == "identity":
        artifact.manifest["plan"]["node_id"] = "another-node"
    coordinator.artifacts._write_manifest(artifact.path, artifact.manifest)
    before = (artifact.path / "manifest.json").read_bytes()
    manifest = completed.manifest.copy()
    manifest["schema_version"] = 1
    manifest["dispatches"] = manifest.pop("calls_used")
    manifest["dispatches_include_planner"] = True
    manifest["final_run_id"] = None
    raw_manifest(coordinator.store, record.workflow_id, manifest, state="recovery_required")
    if damage:
        with pytest.raises(WorkflowError):
            coordinator.run(record.workflow_id, resume=True)
        assert (artifact.path / "manifest.json").read_bytes() == before
    else:
        resumed = coordinator.run(record.workflow_id, resume=True)
        assert resumed.state == "completed"
        assert resumed.final_run_id == completed.final_run_id
        upgraded = coordinator.artifacts.load(resumed.final_run_id).manifest["plan"]
        assert upgraded["source_fingerprint"] == record.source_fingerprint
        assert upgraded["patch_sha256"] == hashlib.sha256(
            coordinator.artifacts.patch(resumed.final_run_id)
        ).hexdigest()
        assert len(upgraded["bundle_sha256"]) == 64
    assert len(agent.calls) == calls


@pytest.mark.parametrize("shape", ["current", "legacy"])
@pytest.mark.parametrize("state", ["pending", "running"])
def test_resume_accepts_a_final_artifact_published_before_final_agent(workflow, shape, state):
    coordinator, record, agent = workflow
    completed = coordinator.run(record.workflow_id)
    calls = len(agent.calls)
    artifact = coordinator.artifacts.load(completed.final_run_id)
    expected_agent = artifact.manifest["plan"].pop("final_agent")
    if shape == "legacy":
        for key in ("source_fingerprint", "bundle_sha256", "patch_sha256"):
            artifact.manifest["plan"].pop(key)
    artifact.manifest["state"] = state
    coordinator.artifacts._write_manifest(artifact.path, artifact.manifest)
    before = (artifact.path / "manifest.json").read_bytes()
    manifest = completed.manifest.copy()
    manifest["final_run_id"] = None
    raw_manifest(coordinator.store, record.workflow_id, manifest, state="recovery_required")

    resumed = coordinator.run(record.workflow_id, resume=True)

    assert resumed.state == "completed"
    assert len(agent.calls) == calls
    plan = coordinator.artifacts.load(resumed.final_run_id).manifest["plan"]
    if state == "pending" and shape == "current":
        # Never backfilled: no manifest write beyond the approval stamp.
        assert "final_agent" not in plan
        after = coordinator.artifacts.load(resumed.final_run_id).manifest
        after["plan"].pop("approved", None)
        expected = json.loads(before)
        expected["plan"].pop("approved", None)
        assert after["plan"] == expected["plan"]
    else:
        # A running artifact is re-saved, and the legacy upgrade already
        # merges the whole plan; both carry the recorded agent.
        assert plan["final_agent"] == expected_agent


def test_resume_refuses_a_final_artifact_naming_another_final_agent(workflow):
    coordinator, record, _ = workflow
    completed = coordinator.run(record.workflow_id)
    artifact = coordinator.artifacts.load(completed.final_run_id)
    artifact.manifest["plan"]["final_agent"] = "codex"
    coordinator.artifacts._write_manifest(artifact.path, artifact.manifest)
    before = (artifact.path / "manifest.json").read_bytes()
    manifest = completed.manifest.copy()
    manifest["final_run_id"] = None
    raw_manifest(coordinator.store, record.workflow_id, manifest, state="recovery_required")
    with pytest.raises(WorkflowError, match="final run id collision"):
        coordinator.run(record.workflow_id, resume=True)
    assert (artifact.path / "manifest.json").read_bytes() == before


@pytest.mark.parametrize("replacement", [False, True])
def test_foreground_interrupt_only_cancels_attached_execution(
    workflow, monkeypatch, tmp_path, replacement
):
    import importlib
    from types import SimpleNamespace

    cli = importlib.import_module("syncmill.cli")
    coordinator, record, _ = workflow
    store = coordinator.store
    claim(store, record.workflow_id, job_id="old-job")
    controls = []
    before = None
    jobs = SimpleNamespace(
        active_jobs=lambda: [],
        list_jobs=lambda: [{"job_id": "old-job", "status": "running"}],
        control=lambda job, action: controls.append((job, action)),
    )

    def interrupt(_):
        nonlocal before
        if replacement:
            store.reconcile_job_terminal(record.workflow_id, "old-job", "lost")
            claim(store, record.workflow_id, resume=True, job_id="new-job")
        before = store.load(record.workflow_id)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", interrupt)
    with pytest.raises(cli.click.ClickException, match="cancellation requested for job old-job"):
        cli._follow_workflow_job(
            jobs, store, {"job_id": "old-job", "job_dir": str(tmp_path)}, record.workflow_id
        )
    current = store.load(record.workflow_id)
    assert controls == [("old-job", "stop")]
    if replacement:
        assert current == before
        assert current.job_id == "new-job"
        assert current.state == "running"
    else:
        assert current.job_id == "old-job"
        assert current.state == "cancelling"


def test_job_scoped_cancel_does_not_cancel_an_unclaimed_plan(workflow):
    coordinator, record, _ = workflow
    before = coordinator.store.load(record.workflow_id)
    with pytest.raises(WorkflowLeaseLost, match="no longer owned"):
        coordinator.store.cancel(record.workflow_id, expected_job_id="unclaimed-job")
    assert coordinator.store.load(record.workflow_id) == before
    # Explicit operator cancellation still applies to the workflow itself.
    assert coordinator.store.cancel(record.workflow_id).state == "cancelled"
