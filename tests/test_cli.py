"""cli.py — run / agents / status via CliRunner with fakes (no real subprocess,
no real memtomem)."""

from __future__ import annotations

import json
import os
import subprocess
import types
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from syncmill.artifacts import ArtifactStore
from syncmill.board import BoardError
from syncmill.cli import cli, _target_project_config
from syncmill.config import SyncmillConfig
from syncmill.matcher import build_matcher
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.project import project_id
from syncmill.report import load_envelope, render_report
from syncmill.rooms import RoomError

from conftest import FakeAgent, FakeRegistry


@pytest.fixture
def runner():
    return CliRunner()


def _result(
    status=TaskStatus.COMPLETED, agent_id="claude", error=None, files=None, patch=None
):
    r = TaskResult(
        task_id=Task(prompt="p").id,
        agent_id=agent_id,
        status=status,
        output="the output",
        exit_code=0 if status is TaskStatus.COMPLETED else 1,
        duration_seconds=0.5,
        error=error,
        patch=patch,
    )
    if files:
        r.files_changed = files
    return r


def _install_supervisor(
    monkeypatch, *, result, agents, degraded=False, breaker_degraded=False,
    report_path=None, preflight_warnings=None, routing_decision=None,
    routing_warning=None,
):
    holder: dict = {"cleanup": []}

    class Sup:
        def __init__(self, config):
            holder["config"] = config
            self.memory = types.SimpleNamespace(degraded=degraded, last_error="boom")
            # mirror the real Supervisor's advisory-preflight warning seam
            self.preflight_warnings = list(preflight_warnings or [])
            # breaker is None unless the test wants to exercise the degraded notice
            self.breaker = (
                types.SimpleNamespace(degraded=True, last_error="state unreadable")
                if breaker_degraded
                else None
            )
            self.registry = FakeRegistry(agents)
            # mirror the real Supervisor: the CLI banner re-orders through this
            # same matcher, so the fake must expose it (None by default).
            self.matcher = build_matcher(config.capabilities, config.agent_priority)
            # mirror the real Supervisor's report seam (None = no report line)
            self.last_report_path = report_path
            # mirror the model-routing banner/degrade seams
            self.last_routing_decision = routing_decision
            self.last_routing_warning = routing_warning
            self.closed = False
            holder["sup"] = self

        def run(self, task, agent_id=None, exclude=None, **kwargs):
            holder["run"] = {
                "task": task, "agent_id": agent_id, "exclude": exclude, **kwargs,
            }
            return result

        def close(self):
            self.closed = True

    monkeypatch.setattr("syncmill.cli.Supervisor", Sup)
    monkeypatch.setattr(
        "syncmill.cli.cleanup_orphaned_worktrees",
        lambda p: holder["cleanup"].append(p),
    )
    return holder


# ── group ────────────────────────────────────────────────────────────────


def test_cli_help_lists_commands(runner):
    res = runner.invoke(cli, ["--help"])
    assert res.exit_code == 0
    for cmd in (
        "run", "agents", "status", "serve", "workflow", "session", "room", "tui"
    ):
        assert cmd in res.output


def test_room_commands_are_feature_gated(runner, git_repo):
    res = runner.invoke(cli, ["room", "list", "--repo", str(git_repo)])
    assert res.exit_code != 0
    assert "rooms are disabled" in res.output


def test_room_help_exposes_compare_apply_flow(runner):
    res = runner.invoke(cli, ["room", "--help"])
    assert res.exit_code == 0
    for command in ("create", "round", "candidates", "diff", "select", "apply"):
        assert command in res.output


def test_room_create_documents_short_agent_alias(runner):
    res = runner.invoke(cli, ["room", "create", "--help"])
    assert res.exit_code == 0
    assert "-a, --agent" in res.output


def test_room_recover_routes_round_and_apply_recovery_through_coordinator(
    monkeypatch, runner, tmp_path
):
    calls: dict[str, object] = {}

    class Store:
        def get_room(self, room_id):
            calls["checked"] = room_id

    class Coordinator:
        store = Store()

        def recover(self, room_id=None):
            calls["recovered"] = room_id
            return [room_id]

        def close(self):
            calls["closed"] = True

    coordinator = Coordinator()
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), coordinator),
    )

    res = runner.invoke(cli, ["room", "recover", "room-1"])

    assert res.exit_code == 0, res.output
    assert calls == {
        "checked": "room-1",
        "recovered": "room-1",
        "closed": True,
    }
    assert "Recovered room room-1" in res.output


def _patch_room_coordinator(monkeypatch, tmp_path, coordinator):
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), coordinator),
    )


def test_room_attach_ensures_viewer_and_attaches(monkeypatch, runner, tmp_path):
    events: dict[str, object] = {}

    class Coordinator:
        store = object()
        project_id = "proj-1"

        def close(self):
            events["closed"] = True

    def fake_ensure(store, project_id, room_id, round_number):
        events["ensured"] = (project_id, room_id, round_number)
        return "sess"

    def fake_attach(name):
        events["attached"] = name
        return 0

    _patch_room_coordinator(monkeypatch, tmp_path, Coordinator())
    monkeypatch.setattr("syncmill.cli.sweep_orphan_viewers", lambda store, project_id: events.__setitem__("swept", project_id))
    monkeypatch.setattr("syncmill.cli.ensure_viewer", fake_ensure)
    monkeypatch.setattr("syncmill.cli.in_tmux", lambda: False)
    monkeypatch.setattr("syncmill.cli.attach_viewer", fake_attach)

    res = runner.invoke(cli, ["room", "attach", "room-1", "--round", "2"])

    assert res.exit_code == 0, res.output
    assert events["swept"] == "proj-1"
    assert events["ensured"] == ("proj-1", "room-1", 2)
    assert events["attached"] == "sess"
    assert events["closed"] is True


def test_room_attach_surfaces_viewer_error(monkeypatch, runner, tmp_path):
    from syncmill.room_viewer import RoomViewerError

    class Coordinator:
        store = object()
        project_id = "proj-1"

        def close(self):
            pass

    _patch_room_coordinator(monkeypatch, tmp_path, Coordinator())
    monkeypatch.setattr("syncmill.cli.sweep_orphan_viewers", lambda store, project_id: None)

    def boom(store, project_id, room_id, round_number):
        raise RoomViewerError("terminal unavailable")

    monkeypatch.setattr("syncmill.cli.ensure_viewer", boom)
    res = runner.invoke(cli, ["room", "attach", "room-1"])
    assert res.exit_code != 0
    assert "terminal unavailable" in res.output


def test_room_close_delegates_to_coordinator(monkeypatch, runner, tmp_path):
    # Viewer teardown lives in the coordinator's close_room (the lifecycle
    # boundary), so the CLI only needs to invoke it.
    events: dict[str, object] = {}

    class Coordinator:
        store = object()
        project_id = "proj-1"

        def close_room(self, room_id):
            events["closed_room"] = room_id

        def close(self):
            pass

    _patch_room_coordinator(monkeypatch, tmp_path, Coordinator())
    res = runner.invoke(cli, ["room", "close", "room-1"])
    assert res.exit_code == 0, res.output
    assert events["closed_room"] == "room-1"


def test_room_help_lists_attach(runner):
    res = runner.invoke(cli, ["room", "--help"])
    assert res.exit_code == 0
    assert "attach" in res.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--detach", "--lineage", "branch-from"],
        ["--detach", "--lineage", "fresh", "--base-candidate", "candidate-1"],
    ],
)
def test_detached_room_round_rejects_invalid_lineage_before_spawn(
    monkeypatch, runner, tmp_path, arguments
):
    spawned = False

    class Coordinator:
        def close(self):
            pass

    def launch(*args, **kwargs):
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    monkeypatch.setattr("syncmill.cli.launch_room_worker", launch)

    res = runner.invoke(cli, ["room", "round", "room-1", *arguments])

    assert res.exit_code != 0
    assert spawned is False
    assert "base candidate" in res.output


def test_detached_room_round_preflights_before_spawn(monkeypatch, runner, tmp_path):
    spawned = False

    class Coordinator:
        def preflight_round(self, room_id, prompt, **kwargs):
            raise RoomError("room cannot start a round from selected")

        def close(self):
            pass

    def launch(*args, **kwargs):
        nonlocal spawned
        spawned = True

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    monkeypatch.setattr("syncmill.cli.launch_room_worker", launch)

    res = runner.invoke(cli, ["room", "round", "room-1", "--detach"])

    assert res.exit_code != 0
    assert "cannot start" in res.output
    assert spawned is False


def test_room_worker_consumes_prompt_fd(monkeypatch, runner, tmp_path):
    # The launcher keeps the prompt off argv AND off the filesystem — it rides
    # an inherited fd of an already-unlinked temp file, so a worker that dies
    # before consuming it strands nothing. The worker reads the fd to EOF.
    captured: dict = {}

    class Coordinator:
        def run_round(self, room_id, prompt, **kwargs):
            captured["room_id"] = room_id
            captured["prompt"] = prompt

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"from the fd")
    os.close(write_fd)

    res = runner.invoke(
        cli,
        ["room-worker", "--prompt-fd", str(read_fd), "--", "room-1"],
    )

    assert res.exit_code == 0, res.output
    assert captured["room_id"] == "room-1"
    assert captured["prompt"] == "from the fd"
    assert captured["closed"] is True


def test_room_candidates_and_diff_render_corrupt_manifests_as_clean_errors(
    monkeypatch, runner, tmp_path
):
    # A manifest missing an expected key must become a ClickException, never a
    # traceback — the _ROOM_POLL_ERRORS posture on the CLI read surface.
    class Store:
        def get_room(self, room_id):
            return {"id": room_id}

        def list_candidates(self, room_id):
            return [{"id": "cand-1", "member_id": "claude", "state": "completed"}]

        def load_candidate(self, candidate_id):
            return {"gate": None}  # no patch_bytes / no patch

    class Coordinator:
        store = Store()

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )

    res = runner.invoke(cli, ["room", "candidates", "room-1"])
    assert res.exit_code != 0
    assert "Error:" in res.output
    assert "Traceback" not in res.output

    res = runner.invoke(cli, ["room", "diff", "cand-1"])
    assert res.exit_code != 0
    assert "Error:" in res.output
    assert "Traceback" not in res.output


def test_room_worker_consumes_resolved_config_snapshot(
    monkeypatch, runner, git_repo, tmp_path
):
    captured: dict = {}
    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "worker-config-test"},
        rooms={"enabled": True},
        runs={"root": str(tmp_path / "custom-runs")},
        workspace={"root": str(tmp_path / "custom-workspaces")},
        gate={"commands": ["pytest -q"]},
    )
    snapshot = tmp_path / "resolved.json"
    snapshot.write_text(config.model_dump_json(), encoding="utf-8")

    class Coordinator:
        def __init__(self, resolved, root):
            captured["config"] = resolved
            captured["root"] = root

        def run_round(self, room_id, prompt, **kwargs):
            captured["run"] = {"room_id": room_id, "prompt": prompt, **kwargs}

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("syncmill.cli.RoomCoordinator", Coordinator)
    res = runner.invoke(
        cli,
        [
            "room-worker",
            "room-1",
            "--repo",
            str(git_repo),
            "--resolved-config",
            str(snapshot),
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured["root"] == git_repo.resolve()
    assert captured["config"].runs.root == str(tmp_path / "custom-runs")
    assert captured["config"].workspace.root == str(tmp_path / "custom-workspaces")
    assert captured["config"].gate.commands == ["pytest -q"]
    assert captured["closed"] is True
    assert not snapshot.exists()


def test_worker_snapshot_carries_the_applied_team(
    monkeypatch, runner, git_repo, tmp_path
):
    # The anti-forgery scrub nulls `applied_team` at every construction, so a
    # detached/room worker launched from a team-selected session used to record
    # `team: null` in its envelope. _config_from_snapshot re-stamps it via
    # apply_team — bounded to teams the snapshot itself declares.
    from syncmill.config import apply_team

    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "team-carry"},
        rooms={"enabled": True},
        agent_priority=["claude", "codex"],
        teams={"py": {"agents": ["codex"]}},
    )
    apply_team(config, "py")
    snapshot = tmp_path / "resolved.json"
    snapshot.write_text(config.model_dump_json(), encoding="utf-8")
    captured: dict = {}

    class Coordinator:
        def __init__(self, resolved, root):
            captured["config"] = resolved

        def run_round(self, room_id, prompt, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr("syncmill.cli.RoomCoordinator", Coordinator)
    res = runner.invoke(
        cli,
        ["room-worker", "--repo", str(git_repo), "--resolved-config", str(snapshot), "--", "room-1"],
    )
    assert res.exit_code == 0, res.output
    assert captured["config"].applied_team == "py"
    assert captured["config"].agent_priority == ["codex"]  # overlay re-applied


def test_worker_snapshot_rejects_a_forged_applied_team(
    monkeypatch, runner, git_repo, tmp_path
):
    # An applied_team naming a team the snapshot does NOT declare is a forgery
    # (or drift) and must stay scrubbed to None, never re-applied.
    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "team-forge"},
        rooms={"enabled": True},
    )
    payload = json.loads(config.model_dump_json())
    payload["applied_team"] = "evil"
    snapshot = tmp_path / "resolved.json"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    captured: dict = {}

    class Coordinator:
        def __init__(self, resolved, root):
            captured["config"] = resolved

        def run_round(self, room_id, prompt, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr("syncmill.cli.RoomCoordinator", Coordinator)
    res = runner.invoke(
        cli,
        ["room-worker", "--repo", str(git_repo), "--resolved-config", str(snapshot), "--", "room-1"],
    )
    assert res.exit_code == 0, res.output
    assert captured["config"].applied_team is None


def test_apply_worker_consumes_resolved_config_snapshot(
    monkeypatch, runner, git_repo, tmp_path
):
    captured: dict = {}
    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "apply-worker-test"},
        runs={"root": str(tmp_path / "custom-runs")},
        gate={"commands": ["pytest -q"]},
    )
    snapshot = tmp_path / "resolved.json"
    snapshot.write_text(config.model_dump_json(), encoding="utf-8")

    class FakeStore:
        def __init__(self, runs_root, pid):
            captured["runs_root"] = runs_root
            captured["pid"] = pid

        def reconcile_interrupted_applies(self, *, run_id=None):
            captured["reconciled"] = run_id
            return []

        def load(self, run_id):
            from syncmill.artifacts import RunArtifact

            return RunArtifact(run_id, tmp_path / run_id, {})

        def apply(self, run_id, root, fingerprint, *, verify_commands, verify_timeout, verify_workspace_root=None):
            captured["apply"] = {
                "run_id": run_id, "root": root,
                "verify_commands": verify_commands,
            }
            return tmp_path / "receipt.json"

    monkeypatch.setattr("syncmill.cli.ArtifactStore", FakeStore)
    res = runner.invoke(
        cli,
        [
            "apply-worker", "run-42", "--repo", str(git_repo),
            "--verify", "--resolved-config", str(snapshot),
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured["apply"]["run_id"] == "run-42"
    assert captured["apply"]["root"] == git_repo.resolve()
    # --verify threaded the pinned config's gate commands.
    assert captured["apply"]["verify_commands"] == ["pytest -q"]
    assert str(tmp_path / "custom-runs") in str(captured["runs_root"])
    assert not snapshot.exists()  # consumed + unlinked


def test_room_apply_worker_consumes_snapshot_and_fences(
    monkeypatch, runner, git_repo, tmp_path
):
    captured: dict = {}
    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "room-apply-worker-test"},
        rooms={"enabled": True},
        runs={"root": str(tmp_path / "custom-runs")},
    )
    snapshot = tmp_path / "resolved.json"
    snapshot.write_text(config.model_dump_json(), encoding="utf-8")

    class Coordinator:
        def __init__(self, resolved, root):
            captured["root"] = root

        def apply(self, room_id, *, verify, expected_candidate_id, expected_revision):
            captured["apply"] = {
                "room_id": room_id, "verify": verify,
                "expected_candidate_id": expected_candidate_id,
                "expected_revision": expected_revision,
            }
            return "run-9", tmp_path / "receipt.json"

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("syncmill.cli.RoomCoordinator", Coordinator)
    res = runner.invoke(
        cli,
        [
            "room-apply-worker", "room-3", "--repo", str(git_repo),
            "--verify", "--expected-candidate", "cand-1", "--expected-revision", "5",
            "--resolved-config", str(snapshot),
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured["apply"] == {
        "room_id": "room-3", "verify": True,
        "expected_candidate_id": "cand-1", "expected_revision": 5,
    }
    assert captured["closed"] is True
    assert not snapshot.exists()


def _patch_fake_jobstore(monkeypatch, captured, *, launch_error=None):
    from syncmill.tui_jobs import JobError

    class FakeJobStore:
        def __init__(self, tui, project_root, *, board_db=None):
            captured["ctor"] = {"project_root": project_root, "board_db": board_db}

        def launch(self, spec, *, resolved_config=None):
            if launch_error is not None:
                raise JobError(launch_error)
            captured["spec"] = spec
            captured["resolved_config"] = resolved_config
            return {"job_id": "abcd1234efgh", "op": spec.op}

    monkeypatch.setattr("syncmill.cli.JobStore", FakeJobStore)


def _patch_apply_store(monkeypatch, captured, *, missing=False, manifest=None):
    import types

    from syncmill.artifacts import ArtifactError

    class FakeArtifactStore:
        def __init__(self, runs_root, pid):
            pass

        def validate_applicable(self, run_id):
            captured["loaded"] = run_id
            if missing:
                raise ArtifactError(f"unknown or corrupt run: {run_id}")
            return types.SimpleNamespace(manifest=manifest or {"state": "pending"})

    monkeypatch.setattr("syncmill.cli.ArtifactStore", FakeArtifactStore)


def test_apply_detach_launches_a_detached_worker(monkeypatch, runner, git_repo):
    captured: dict = {}
    _patch_fake_jobstore(monkeypatch, captured)
    _patch_apply_store(monkeypatch, captured)
    res = runner.invoke(
        cli, ["apply", "run-77", "--repo", str(git_repo), "--verify", "--detach"]
    )
    assert res.exit_code == 0, res.output
    assert "detached artifact-apply job abcd1234" in res.output
    # The run id was preflighted against the artifact store before spawning.
    assert captured["loaded"] == "run-77"
    spec = captured["spec"]
    assert spec.op == "artifact-apply" and spec.run_id == "run-77" and spec.verify is True
    # The confirmed config is pinned into the launch (frozen-config seam).
    assert captured["resolved_config"] is not None
    assert captured["ctor"]["project_root"] == git_repo.resolve()


def test_apply_detach_surfaces_a_launch_failure(monkeypatch, runner, git_repo):
    # PR #78 nit (CLI coverage): a JobError from the launch must reach the user
    # as a clean non-zero ClickException, not a traceback or a false success.
    captured: dict = {}
    _patch_fake_jobstore(monkeypatch, captured, launch_error="a worker is already active")
    _patch_apply_store(monkeypatch, captured)
    res = runner.invoke(cli, ["apply", "run-77", "--repo", str(git_repo), "--detach"])
    assert res.exit_code != 0
    assert "a worker is already active" in res.output
    assert "detached" not in res.output


def test_apply_detach_rejects_an_unknown_run_id(monkeypatch, runner, git_repo):
    # PR #78 review: a typo'd run id must fail HERE with a clean non-zero exit,
    # not exit 0 with "detached job launched" and die in the background log.
    captured: dict = {}
    _patch_fake_jobstore(monkeypatch, captured)
    _patch_apply_store(monkeypatch, captured, missing=True)
    res = runner.invoke(cli, ["apply", "run-nope", "--repo", str(git_repo), "--detach"])
    assert res.exit_code != 0
    assert "unknown or corrupt run: run-nope" in res.output
    assert "spec" not in captured  # no worker was launched


def test_apply_detach_rejects_terminal_states_and_allows_recovery(
    monkeypatch, runner, git_repo
):
    # codex R10: a terminal standalone state (applied/discarded/...) is
    # guaranteed to fail in the worker — refuse before reporting a successful
    # detach. `applying` stays spawnable: the worker's reconcile pre-pass is
    # the crash-recovery path.
    for state in ("applied", "discarded", "failed", "no_changes"):
        captured: dict = {}
        _patch_fake_jobstore(monkeypatch, captured)
        _patch_apply_store(monkeypatch, captured, manifest={"state": state})
        res = runner.invoke(
            cli, ["apply", "run-t", "--repo", str(git_repo), "--detach"]
        )
        assert res.exit_code != 0, state
        assert "nothing to apply" in res.output
        assert "spec" not in captured
    for state in ("pending", "applying"):
        captured = {}
        _patch_fake_jobstore(monkeypatch, captured)
        _patch_apply_store(monkeypatch, captured, manifest={"state": state})
        res = runner.invoke(
            cli, ["apply", "run-t", "--repo", str(git_repo), "--detach"]
        )
        assert res.exit_code == 0, res.output
        assert captured["spec"].run_id == "run-t"


def test_apply_detach_rejects_room_bound_runs(monkeypatch, runner, git_repo):
    # codex R9 blocker: a room-bound run must be refused up front with the
    # `room apply` redirect — spawning the worker would bypass the Room
    # selection/revision lifecycle. Both room markers are recognized.
    for manifest in (
        {"state": "pending", "plan": {"origin": "room"}},
        {"state": "pending", "strategy": "room"},
    ):
        captured: dict = {}
        _patch_fake_jobstore(monkeypatch, captured)
        _patch_apply_store(monkeypatch, captured, manifest=manifest)
        res = runner.invoke(
            cli, ["apply", "run-room", "--repo", str(git_repo), "--detach"]
        )
        assert res.exit_code != 0
        assert "room-bound" in res.output and "room apply" in res.output
        assert "spec" not in captured  # no worker was launched


def _patch_recover_store(monkeypatch, captured, reports, receipt=None, apply_error=None):
    from syncmill.artifacts import ArtifactError

    class FakeStore:
        def __init__(self, runs_root, pid):
            pass

        def reconcile_interrupted_applies(self, *, run_id=None):
            captured["run_id"] = run_id
            return list(reports)

        def load(self, run_id):
            from syncmill.artifacts import RunArtifact

            # The apply path reads the manifest to learn whether a workflow has
            # to approve applying this run; these came from no workflow.
            return RunArtifact(run_id, Path("/runs") / run_id, {})

        def apply(self, run_id, root, fingerprint, *, verify_commands, verify_timeout, verify_workspace_root=None):
            captured["applied"] = run_id
            if apply_error is not None:
                raise ArtifactError(apply_error)
            return receipt

    monkeypatch.setattr("syncmill.cli.ArtifactStore", FakeStore)


def _retained_workspace(root, name):
    from syncmill import worktree as W

    ws = root / name
    ws.mkdir(parents=True)
    # BOTH sentinels: the run sentinel is syncmill's ownership proof and the
    # recovery marker is retain()'s exemption — the destructive verb needs both.
    (ws / W._RUN_SENTINEL).write_text("999999\n")
    (ws / W._RECOVERY_MARKER).write_text("")
    return ws


def test_recover_list_shows_retained_workspaces(monkeypatch, runner, tmp_path):
    root = tmp_path / "workspaces"
    _retained_workspace(root, "run-old")
    monkeypatch.setenv("SYNCMILL_WORKSPACE__ROOT", str(root))
    res = runner.invoke(cli, ["recover", "--list"])
    assert res.exit_code == 0, res.output
    assert "run-old" in res.output and str(root / "run-old") in res.output


def test_recover_list_empty(monkeypatch, runner, tmp_path):
    monkeypatch.setenv("SYNCMILL_WORKSPACE__ROOT", str(tmp_path / "workspaces"))
    res = runner.invoke(cli, ["recover", "--list"])
    assert res.exit_code == 0
    assert "No retained recovery workspaces" in res.output


def test_recover_discard_removes_a_retained_workspace(monkeypatch, runner, tmp_path):
    root = tmp_path / "workspaces"
    ws = _retained_workspace(root, "run-old")
    monkeypatch.setenv("SYNCMILL_WORKSPACE__ROOT", str(root))
    res = runner.invoke(cli, ["recover", "--discard", "run-old"])
    assert res.exit_code == 0, res.output
    assert not ws.exists()


def test_recover_discard_refuses_an_unmarked_dir(monkeypatch, runner, tmp_path):
    root = tmp_path / "workspaces"
    (root / "run-live").mkdir(parents=True)  # no recovery marker → fail closed
    monkeypatch.setenv("SYNCMILL_WORKSPACE__ROOT", str(root))
    res = runner.invoke(cli, ["recover", "--discard", "run-live"])
    assert res.exit_code != 0
    assert "not a recovery-retained" in res.output
    assert (root / "run-live").exists()


def test_recover_list_discard_use_the_target_projects_workspace(
    monkeypatch, runner, git_repo, tmp_path
):
    # --repo selects WHICH project's config is read. A destructive verb reading
    # the CALLER's cwd config would list (and delete) the wrong project's
    # workspace.root entirely.
    caller_root = tmp_path / "caller-workspaces"
    target_root = tmp_path / "target-workspaces"
    _retained_workspace(caller_root, "caller-run")
    target_ws = _retained_workspace(target_root, "target-run")

    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "syncmill.toml").write_text(
        f'config_version = 2\n[workspace]\nroot = "{caller_root}"\n', encoding="utf-8"
    )
    (git_repo / "syncmill.toml").write_text(
        f'config_version = 2\n[workspace]\nroot = "{target_root}"\n', encoding="utf-8"
    )
    monkeypatch.chdir(caller)
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    listed = runner.invoke(cli, ["recover", "--list", "--repo", str(git_repo)])
    assert listed.exit_code == 0, listed.output
    assert "target-run" in listed.output and "caller-run" not in listed.output

    # The caller's identically-shaped name must not be reachable from here.
    missed = runner.invoke(
        cli, ["recover", "--discard", "caller-run", "--repo", str(git_repo)]
    )
    assert missed.exit_code != 0
    assert (caller_root / "caller-run").exists()

    dropped = runner.invoke(
        cli, ["recover", "--discard", "target-run", "--repo", str(git_repo)]
    )
    assert dropped.exit_code == 0, dropped.output
    assert not target_ws.exists()


def test_recover_list_and_discard_are_exclusive(runner, tmp_path):
    res = runner.invoke(cli, ["recover", "--list", "--discard", "x"])
    assert res.exit_code != 0
    res = runner.invoke(cli, ["recover", "run-1", "--list"])
    assert res.exit_code != 0


def test_recover_echoes_each_outcome(monkeypatch, runner, git_repo):
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    reports = [
        ApplyReconciliation("run-a", "completed", "done", receipt="/r/a.json"),
        ApplyReconciliation("run-b", "not_applied", "restored"),
        ApplyReconciliation("run-c", "manual_recovery", "undecidable; inspect"),
        ApplyReconciliation("run-d", "skipped_room_bound", "room-bound"),
    ]
    _patch_recover_store(monkeypatch, captured, reports)
    res = runner.invoke(cli, ["recover", "--repo", str(git_repo)])
    assert res.exit_code == 0, res.output
    assert captured["run_id"] is None
    assert "run-a: completed" in res.output and "/r/a.json" in res.output
    assert "run-b: not applied — restored to pending" in res.output
    assert "run-c: manual recovery required — undecidable; inspect" in res.output
    assert "run-d: skipped — room-bound; use 'syncmill room recover'" in res.output


def test_recover_all_skip_sweep_prints_the_skip_rows(monkeypatch, runner, git_repo):
    # PR #78 review: an all-skip sweep is NOT an all-clear — a room-bound or
    # lock-held strand still exists, and suppressing its row would hide the
    # `room recover` redirect.
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    reports = [
        ApplyReconciliation("run-x", "skipped_live_owner", "still running"),
        ApplyReconciliation("run-y", "skipped_room_bound", "room-bound"),
    ]
    _patch_recover_store(monkeypatch, captured, reports)
    res = runner.invoke(cli, ["recover", "--repo", str(git_repo)])
    assert res.exit_code == 0, res.output
    assert "No interrupted artifact apply to recover" not in res.output
    assert "run-x: skipped — still running" in res.output
    assert "run-y: skipped — room-bound; use 'syncmill room recover'" in res.output


def test_recover_empty_sweep_prints_the_all_clear(monkeypatch, runner, git_repo):
    captured: dict = {}
    _patch_recover_store(monkeypatch, captured, [])
    res = runner.invoke(cli, ["recover", "--repo", str(git_repo)])
    assert res.exit_code == 0, res.output
    assert "No interrupted artifact apply to recover" in res.output


def test_recover_passes_the_run_id_filter(monkeypatch, runner, git_repo):
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    reports = [ApplyReconciliation("run-5", "skipped_live_owner", "still running")]
    _patch_recover_store(monkeypatch, captured, reports)
    res = runner.invoke(cli, ["recover", "run-5", "--repo", str(git_repo)])
    assert res.exit_code == 0, res.output
    assert captured["run_id"] == "run-5"
    # An explicit target always prints its verdict, even a skip.
    assert "run-5: skipped — still running" in res.output


def test_recover_exits_nonzero_when_a_report_is_an_error(monkeypatch, runner, git_repo):
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    reports = [
        ApplyReconciliation("run-a", "not_applied", "restored"),
        ApplyReconciliation("run-b", "error", "repository is gone"),
    ]
    _patch_recover_store(monkeypatch, captured, reports)
    res = runner.invoke(cli, ["recover", "--repo", str(git_repo)])
    # Every report still prints, but a failed reconciliation exits non-zero.
    assert res.exit_code != 0
    assert "run-a: not applied — restored to pending" in res.output
    assert "run-b: error — repository is gone" in res.output


def test_recover_run_id_not_applying_is_a_clean_skip(monkeypatch, runner, git_repo):
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    reports = [
        ApplyReconciliation("run-6", "skipped_not_applying", "run is 'pending', not applying")
    ]
    _patch_recover_store(monkeypatch, captured, reports)
    res = runner.invoke(cli, ["recover", "run-6", "--repo", str(git_repo)])
    assert res.exit_code == 0, res.output
    assert "run-6: skipped — run is 'pending', not applying" in res.output


def test_apply_runs_the_reconcile_pre_pass(monkeypatch, runner, git_repo):
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    _patch_recover_store(
        monkeypatch,
        captured,
        [ApplyReconciliation("run-9", "error", "run is 'pending', not applying")],
        receipt=Path("/receipt.json"),
    )
    res = runner.invoke(cli, ["apply", "run-9", "--repo", str(git_repo)])
    assert res.exit_code == 0, res.output
    assert captured["run_id"] == "run-9"  # pre-pass ran, scoped to the target
    assert captured["applied"] == "run-9"  # a non-completed strand still applies
    assert "Applied run run-9" in res.output


def test_apply_short_circuits_on_a_completed_strand(monkeypatch, runner, git_repo):
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    _patch_recover_store(
        monkeypatch,
        captured,
        [ApplyReconciliation("run-9", "completed", "done", receipt="/r/done.json")],
        receipt=Path("/receipt.json"),
    )
    res = runner.invoke(cli, ["apply", "run-9", "--repo", str(git_repo)])
    assert res.exit_code == 0, res.output
    assert "applied" not in captured  # apply short-circuited
    assert "run-9: completed" in res.output
    assert "Applied run" not in res.output


def test_apply_pre_pass_not_applied_restores_then_applies(monkeypatch, runner, git_repo):
    # PR #78 nit (CLI coverage): the not_applied branch of the apply pre-pass —
    # the strand is reported as restored to pending and the fresh apply proceeds.
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    _patch_recover_store(
        monkeypatch,
        captured,
        [ApplyReconciliation("run-9", "not_applied", "restored")],
        receipt=Path("/receipt.json"),
    )
    res = runner.invoke(cli, ["apply", "run-9", "--repo", str(git_repo)])
    assert res.exit_code == 0, res.output
    assert "run-9: not applied — restored to pending" in res.output
    assert captured["applied"] == "run-9"
    assert "Applied run run-9" in res.output


def test_apply_pre_pass_manual_recovery_reports_then_fails_cleanly(
    monkeypatch, runner, git_repo
):
    # The undecidable strand is frozen: the row is printed, the apply is still
    # attempted, and the store's refusal surfaces as a clean non-zero error.
    from syncmill.artifacts import ApplyReconciliation

    captured: dict = {}
    _patch_recover_store(
        monkeypatch,
        captured,
        [ApplyReconciliation("run-9", "manual_recovery", "undecidable; inspect")],
        apply_error="run requires manual recovery",
    )
    res = runner.invoke(cli, ["apply", "run-9", "--repo", str(git_repo)])
    assert res.exit_code != 0
    assert "run-9: manual recovery required — undecidable; inspect" in res.output
    assert "run requires manual recovery" in res.output
    assert "Applied run" not in res.output


def _patch_fake_room_coordinator(monkeypatch, captured, room):
    """A fake RoomCoordinator exposing only what the detach path reads:
    ``store.get_room`` and ``close``."""

    class FakeCoordinator:
        def __init__(self, config, target, *args, **kwargs):
            self.store = types.SimpleNamespace(get_room=lambda rid: room)

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("syncmill.cli.RoomCoordinator", FakeCoordinator)


def test_room_apply_detach_fences_the_current_selection(monkeypatch, runner, git_repo):
    captured: dict = {}
    _patch_fake_jobstore(monkeypatch, captured)
    _patch_fake_room_coordinator(
        monkeypatch,
        captured,
        {"id": "room-8", "selected_candidate_id": "cand-1", "revision": 5},
    )
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
    res = runner.invoke(
        cli, ["room", "apply", "room-8", "--repo", str(git_repo), "--detach"]
    )
    assert res.exit_code == 0, res.output
    assert "detached room-apply job abcd1234" in res.output
    spec = captured["spec"]
    assert spec.op == "room-apply" and spec.room_id == "room-8"
    # The selection current at launch is pinned into the spec so a later
    # re-selection can never redirect the delayed worker.
    assert spec.expected_candidate_id == "cand-1"
    assert spec.expected_revision == 5
    assert captured["resolved_config"] is not None
    assert captured["closed"] is True


def test_room_apply_detach_surfaces_a_launch_failure(monkeypatch, runner, git_repo):
    captured: dict = {}
    _patch_fake_jobstore(monkeypatch, captured, launch_error="a worker is already active")
    _patch_fake_room_coordinator(
        monkeypatch,
        captured,
        {"id": "room-8", "selected_candidate_id": "cand-1", "revision": 5},
    )
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
    res = runner.invoke(
        cli, ["room", "apply", "room-8", "--repo", str(git_repo), "--detach"]
    )
    assert res.exit_code != 0
    assert "a worker is already active" in res.output
    assert "detached" not in res.output
    assert captured["closed"] is True  # the inline coordinator is still released


def test_room_apply_detach_requires_a_selection(monkeypatch, runner, git_repo):
    captured: dict = {}
    _patch_fake_jobstore(monkeypatch, captured)
    _patch_fake_room_coordinator(
        monkeypatch, captured, {"id": "room-8", "selected_candidate_id": None}
    )
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
    res = runner.invoke(
        cli, ["room", "apply", "room-8", "--repo", str(git_repo), "--detach"]
    )
    assert res.exit_code != 0
    assert "no selected candidate" in res.output
    assert "spec" not in captured  # no worker was launched
    assert captured["closed"] is True


def test_room_apply_detach_reselection_is_fenced(monkeypatch, runner, git_repo, tmp_path):
    """End-to-end: the detach path captures the selection at launch, and a
    worker running that captured fence rejects a candidate re-selected after."""
    from test_rooms import WritingAgent

    from syncmill.rooms import RoomCoordinator

    config = SyncmillConfig(
        repo_root=str(git_repo),
        project={"id": "fence-e2e"},
        runs={"root": str(tmp_path / "runs")},
        workspace={"root": str(tmp_path / "ws")},
        rooms={"enabled": True},
    )
    seed = RoomCoordinator(
        config, git_repo, agents=[WritingAgent("claude"), WritingAgent("codex")]
    )
    try:
        room = seed.create("compare", ["claude", "codex"])
        seed.run_round(room["id"])
        candidates = seed.store.list_candidates(room["id"])
        first = seed.select(room["id"], candidates[0]["id"])

        # The CLI re-derives the same store from env; capture the launch spec.
        captured: dict = {}
        _patch_fake_jobstore(monkeypatch, captured)
        monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
        monkeypatch.setenv("SYNCMILL_PROJECT__ID", "fence-e2e")
        monkeypatch.setenv("SYNCMILL_RUNS__ROOT", str(tmp_path / "runs"))
        monkeypatch.setenv("SYNCMILL_WORKSPACE__ROOT", str(tmp_path / "ws"))
        res = runner.invoke(
            cli, ["room", "apply", room["id"], "--repo", str(git_repo), "--detach"]
        )
        assert res.exit_code == 0, res.output
        spec = captured["spec"]
        assert spec.expected_candidate_id == candidates[0]["id"]
        assert spec.expected_revision == first["revision"]

        # Someone re-selects the OTHER candidate before the worker runs.
        seed.select(room["id"], candidates[1]["id"])

        snapshot = tmp_path / "resolved.json"
        snapshot.write_text(config.model_dump_json(), encoding="utf-8")
        worker = runner.invoke(
            cli,
            [
                "room-apply-worker", room["id"], "--repo", str(git_repo),
                "--expected-candidate", spec.expected_candidate_id,
                "--expected-revision", str(spec.expected_revision),
                "--resolved-config", str(snapshot),
            ],
        )
        assert worker.exit_code != 0
        assert "selected candidate changed" in worker.output
        # Source untouched — no candidate was applied.
        assert not (git_repo / "claude.txt").exists()
        assert not (git_repo / "codex.txt").exists()
    finally:
        seed.close()


def test_session_help_keeps_direct_and_isolated_flows(runner):
    res = runner.invoke(cli, ["session", "--help"])
    assert res.exit_code == 0
    for command in ("start", "open", "isolated-list", "checkpoint", "close"):
        assert command in res.output


def _configure_relative_collaboration_project(
    monkeypatch,
    git_repo: Path,
    tmp_path: Path,
    project_key: str | None,
    *,
    target: Path | None = None,
) -> tuple[Path, Path]:
    target_value = target.as_posix() if target is not None else "."
    body = (
        "config_version = 2\n"
        f"repo_root = \"{target_value}\"\n\n"
    )
    if project_key is not None:
        body += f"[project]\nid = \"{project_key}\"\n\n"
    body += (
        "[runs]\n"
        "root = \"relative-runs\"\n\n"
        "[workspace]\n"
        "root = \"relative-workspaces\"\n\n"
        "[rooms]\n"
        "enabled = true\n"
    )
    (git_repo / "syncmill.toml").write_text(body, encoding="utf-8")
    # Durable project-local state must not itself count as source drift.
    (git_repo / ".git" / "info" / "exclude").write_text(
        "relative-runs/\nrelative-workspaces/\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")
    caller = tmp_path / f"outside-{project_key or 'implicit-project'}"
    caller.mkdir()
    monkeypatch.chdir(caller)
    return (
        (git_repo / "relative-runs").resolve(),
        (git_repo / "relative-workspaces").resolve(),
    )


def test_relative_room_run_is_visible_to_artifact_cli_outside_repo(
    monkeypatch, runner, git_repo, tmp_path
):
    """A real Room round/apply and general artifact commands share one root."""
    project_key = "relative-state-room"
    expected_runs, expected_workspaces = _configure_relative_collaboration_project(
        monkeypatch, git_repo, tmp_path, project_key
    )

    class WritingAgent(FakeAgent):
        async def aexecute(self, prompt: str, workdir: Path, timeout: int):
            (workdir / "room-output.txt").write_text(
                "created by room\n", encoding="utf-8"
            )
            return self.execute(prompt, workdir, timeout)

    agent = WritingAgent("codex")
    monkeypatch.setattr(
        "syncmill.rooms.AgentRegistry",
        lambda _config: types.SimpleNamespace(available=lambda: [agent]),
    )

    created = runner.invoke(
        cli,
        ["room", "create", "write room output", "-a", "codex", "--repo", str(git_repo)],
    )
    assert created.exit_code == 0, created.output
    room_id = created.output.splitlines()[0]
    rounded = runner.invoke(
        cli, ["room", "round", room_id, "--repo", str(git_repo)]
    )
    assert rounded.exit_code == 0, rounded.output
    # Candidate rows follow the "Room <id>: <state>" line (the round output now
    # leads with the prompt echo/fallback warning).
    round_lines = rounded.output.splitlines()
    room_line = next(
        index for index, line in enumerate(round_lines)
        if line.startswith(f"Room {room_id}")
    )
    candidate_id = round_lines[room_line + 1].split()[0]
    selected = runner.invoke(
        cli,
        ["room", "select", room_id, candidate_id, "--repo", str(git_repo)],
    )
    assert selected.exit_code == 0, selected.output
    room_applied = runner.invoke(
        cli, ["room", "apply", room_id, "--repo", str(git_repo)]
    )
    assert room_applied.exit_code == 0, room_applied.output
    run_id = room_applied.output.splitlines()[0].rsplit(" ", 1)[-1]

    assert expected_workspaces.is_dir()
    artifacts = ArtifactStore(expected_runs, project_key)
    assert artifacts.load(run_id).manifest["plan"]["origin"] == "room"
    assert (git_repo / "room-output.txt").read_text(encoding="utf-8") == (
        "created by room\n"
    )

    listed = runner.invoke(cli, ["runs", "list", "--repo", str(git_repo)])
    assert listed.exit_code == 0, listed.output
    assert run_id in listed.output

    diffed = runner.invoke(cli, ["diff", run_id, "--repo", str(git_repo)])
    assert diffed.exit_code == 0, diffed.output
    assert "room-output.txt" in diffed.output
    assert "created by room" in diffed.output

    already_applied = runner.invoke(
        cli, ["apply", run_id, "--repo", str(git_repo)]
    )
    assert already_applied.exit_code != 0
    # The room-bound guard fires before the state check (codex R9): a
    # standalone apply of a room run is redirected, whatever its state.
    assert "room-bound" in already_applied.output
    assert "room apply" in already_applied.output


def test_relative_session_checkpoint_is_visible_and_applicable_outside_repo(
    monkeypatch, runner, git_repo, tmp_path
):
    """Owner A's checkpoint is found under B's identity and applied to B."""
    target = tmp_path / "target-repo"
    subprocess.run(
        ["git", "clone", "-q", str(git_repo), str(target)],
        check=True,
        capture_output=True,
        text=True,
    )
    expected_runs, expected_workspaces = _configure_relative_collaboration_project(
        monkeypatch, git_repo, tmp_path, None, target=target
    )
    monkeypatch.setattr("syncmill.cli._require_tmux", lambda: None)
    monkeypatch.setattr(
        "syncmill.isolated_session._create_session", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "syncmill.isolated_session.session_exists", lambda _name: False
    )

    opened = runner.invoke(
        cli,
        [
            "session",
            "open",
            "codex",
            "--no-attach",
            "--repo",
            str(git_repo),
        ],
    )
    assert opened.exit_code == 0, opened.output
    line = opened.output.strip()
    session_id = line.split()[3]
    worktree = Path(line.split(" in ", 1)[1])
    assert worktree.is_relative_to(expected_workspaces)
    (worktree / "session-output.txt").write_text(
        "created by session\n", encoding="utf-8"
    )

    checkpointed = runner.invoke(
        cli, ["session", "checkpoint", session_id, "--repo", str(git_repo)]
    )
    assert checkpointed.exit_code == 0, checkpointed.output
    run_id = checkpointed.output.splitlines()[0].split(": ", 1)[1]
    artifacts = ArtifactStore(expected_runs, project_id(target))
    assert artifacts.load(run_id).manifest["plan"]["origin"] == "isolated-session"
    assert artifacts.load(run_id).manifest["repo_root"] == str(target.resolve())

    listed = runner.invoke(cli, ["runs", "list", "--repo", str(git_repo)])
    assert listed.exit_code == 0, listed.output
    assert run_id in listed.output
    diffed = runner.invoke(cli, ["diff", run_id, "--repo", str(git_repo)])
    assert diffed.exit_code == 0, diffed.output
    assert "session-output.txt" in diffed.output
    assert "created by session" in diffed.output

    applied = runner.invoke(cli, ["apply", run_id, "--repo", str(git_repo)])
    assert applied.exit_code == 0, applied.output
    assert (target / "session-output.txt").read_text(encoding="utf-8") == (
        "created by session\n"
    )
    assert not (git_repo / "session-output.txt").exists()
    assert artifacts.load(run_id).manifest["state"] == "applied"

    closed = runner.invoke(
        cli, ["session", "close", session_id, "--repo", str(git_repo)]
    )
    assert closed.exit_code == 0, closed.output


# ── run ──────────────────────────────────────────────────────────────────


def test_run_success(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch, result=_result(files=["a.py"]), agents=[FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do the thing"])
    assert res.exit_code == 0
    assert "[claude] Completed" in res.output
    assert "Files changed: a.py" in res.output
    assert h["sup"].closed is True
    assert len(h["cleanup"]) == 1
    assert h["run"]["agent_id"] is None


def test_run_sweep_uses_the_configured_stale_backstop(monkeypatch, runner):
    """The sweep's age backstop is operator-tunable, so the CLI must pass the
    resolved value — a hardcoded week would silently ignore the setting."""
    monkeypatch.setenv("SYNCMILL_WORKSPACE__STALE_SECONDS", "7200")
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    seen: dict = {}
    monkeypatch.setattr(
        "syncmill.cli.sweep_orphaned_workspaces",
        lambda root, *, older_than: seen.update(root=root, older_than=older_than),
    )
    res = runner.invoke(cli, ["run", "do the thing"])
    assert res.exit_code == 0, res.output
    assert seen["older_than"] == 7200


def test_run_failed_exits_1(monkeypatch, runner):
    _install_supervisor(
        monkeypatch,
        result=_result(status=TaskStatus.FAILED, agent_id="none", error="all failed"),
        agents=[FakeAgent("claude")],
    )
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 1
    assert "Failed" in res.output
    assert "all failed" in res.output


def test_run_forced_agent(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "--agent", "claude"])
    assert "forced, no fallback" in res.output
    assert h["run"]["agent_id"] == "claude"


def test_run_shows_priority_chain(monkeypatch, runner):
    _install_supervisor(
        monkeypatch,
        result=_result(),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["run", "do"])
    assert "claude → codex" in res.output


def test_run_exclude_passes_set_and_shows_remaining(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch,
        result=_result(agent_id="codex"),
        agents=[FakeAgent("claude"), FakeAgent("codex"), FakeAgent("kimi-code")],
    )
    res = runner.invoke(cli, ["run", "do", "--exclude", "claude"])
    assert res.exit_code == 0
    assert h["run"]["exclude"] == {"claude"}
    assert "codex → kimi-code" in res.output
    assert "excluded: claude" in res.output


def test_run_capability_flag_flows_to_task_tags(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do", "-c", "python", "-c", "rust"])
    assert res.exit_code == 0
    assert h["run"]["task"].tags == ["python", "rust"]


def test_run_no_capability_leaves_tags_empty(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert h["run"]["task"].tags == []


def test_run_empty_capability_value_dropped(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "-c", "", "-c", "python"])
    assert res.exit_code == 0
    assert h["run"]["task"].tags == ["python"]  # the empty -c "" is dropped


def test_run_capability_banner_shows_matched_order_and_hint(monkeypatch, runner):
    # with declared capabilities + a hint, the banner reflects the ACTUAL run
    # order (codex promoted ahead of claude) and notes the hint — it never lies.
    monkeypatch.setenv("SYNCMILL_CAPABILITIES__AGENTS", '{"codex": ["python"]}')
    _install_supervisor(
        monkeypatch,
        result=_result(agent_id="codex"),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["run", "do", "-c", "python"])
    assert res.exit_code == 0
    assert "codex → claude" in res.output  # promoted, not raw priority
    assert "capability hint: python" in res.output


def test_run_capability_banner_unchanged_without_declared_capabilities(monkeypatch, runner):
    # a hint with NO declared capabilities → build_matcher None → raw priority
    # order in the banner (the disabled default), but the hint note still shows.
    _install_supervisor(
        monkeypatch,
        result=_result(),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["run", "do", "-c", "python"])
    assert res.exit_code == 0
    assert "claude → codex" in res.output  # unchanged
    assert "capability hint: python" in res.output


def test_run_exclude_unknown_agent_errors(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "--exclude", "clade"])
    assert res.exit_code != 0
    assert "unknown agent" in res.output.lower()
    # rejected before any side effects: no cleanup, no Supervisor constructed
    assert h["cleanup"] == [] and "sup" not in h


def test_run_exclude_conflicts_with_forced_agent(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "-a", "claude", "-x", "claude"])
    assert res.exit_code != 0
    assert "both forced" in res.output.lower()
    assert h["cleanup"] == [] and "sup" not in h


def test_run_exclude_all_available_shows_none(monkeypatch, runner):
    # mock error is generic, so "excluded: claude, codex, kimi-code" can ONLY come
    # from the CLI's own pre-run line — pinning cli.py's sorted() of the excluded set.
    h = _install_supervisor(
        monkeypatch,
        result=_result(status=TaskStatus.FAILED, agent_id="none", error="no agents"),
        agents=[FakeAgent("claude"), FakeAgent("codex"), FakeAgent("kimi-code")],
    )
    res = runner.invoke(cli, ["run", "do", "-x", "claude", "-x", "codex", "-x", "kimi-code"])
    assert h["run"]["exclude"] == {"claude", "codex", "kimi-code"}
    assert "(none) (priority fallback)" in res.output
    assert "excluded: claude, codex, kimi-code" in res.output  # sorted by the CLI
    assert res.exit_code == 1  # FAILED handoff from Supervisor.run


def test_run_forced_agent_with_unrelated_exclude(monkeypatch, runner):
    # -a kimi-code -x claude (X != Y) is accepted; both thread through (and the
    # forced agent is the hyphenated id), not a BadParameter
    h = _install_supervisor(
        monkeypatch,
        result=_result(agent_id="kimi-code"),
        agents=[FakeAgent("claude"), FakeAgent("codex"), FakeAgent("kimi-code")],
    )
    res = runner.invoke(cli, ["run", "do", "-a", "kimi-code", "-x", "claude"])
    assert res.exit_code == 0
    assert "forced, no fallback" in res.output
    assert h["run"]["agent_id"] == "kimi-code"
    assert h["run"]["exclude"] == {"claude"}


def test_run_exclude_disabled_priority_agent_is_noop(monkeypatch, runner):
    # kimi-code is in agent_priority but absent from the live registry; excluding it
    # is a no-op — and exercises the hyphenated id through the CLI's validation.
    h = _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude"), FakeAgent("codex")]
    )
    res = runner.invoke(cli, ["run", "do", "-x", "kimi-code"])
    assert res.exit_code == 0
    assert h["run"]["exclude"] == {"kimi-code"}
    assert "claude → codex" in res.output  # validated vs agent_priority, chain unaffected


def test_run_exclude_empty_string_ignored(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "-x", ""])
    assert res.exit_code == 0  # empty -x value is dropped, not treated as an unknown agent
    assert h["run"]["exclude"] == set()


def test_run_strategy_pipeline_sets_config_and_label(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch,
        result=_result(agent_id="codex"),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["run", "do", "--strategy", "pipeline"])
    assert res.exit_code == 0
    assert h["config"].default_strategy == "pipeline"
    assert "claude → codex (pipeline)" in res.output  # label reflects the strategy


def test_run_strategy_compete_sets_config_and_label(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch,
        result=_result(agent_id="codex"),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["run", "do", "--strategy", "compete"])
    assert res.exit_code == 0
    assert h["config"].default_strategy == "compete"
    assert "claude → codex (compete)" in res.output  # label reflects the strategy


def test_run_strategy_council_sets_config_and_label(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch,
        result=_result(agent_id="claude"),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["run", "do", "--strategy", "council"])
    assert res.exit_code == 0
    assert h["config"].default_strategy == "council"
    assert "claude → codex (council)" in res.output  # label reflects the strategy


def test_run_strategy_decompose_sets_config_and_label(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch,
        result=_result(agent_id="claude"),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["run", "do", "--strategy", "decompose"])
    assert res.exit_code == 0
    assert h["config"].default_strategy == "decompose"
    assert "claude → codex (decompose)" in res.output  # label reflects the strategy


def test_run_strategy_defaults_to_route_label(monkeypatch, runner):
    _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude"), FakeAgent("codex")]
    )
    res = runner.invoke(cli, ["run", "do"])
    assert "claude → codex (priority fallback)" in res.output


def test_run_strategy_invalid_rejected_before_side_effects(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "--strategy", "nope"])
    assert res.exit_code != 0
    assert "nope" in res.output  # click.Choice rejects the bad value
    # rejected during parsing: no cleanup, no Supervisor constructed
    assert h["cleanup"] == [] and "sup" not in h


def test_run_invalid_strategy_env_var_gets_clean_error(monkeypatch, runner):
    # SYNCMILL_DEFAULT_STRATEGY bypasses click.Choice — must still be a clean
    # one-liner, not a Supervisor() ValueError traceback.
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    monkeypatch.setenv("SYNCMILL_DEFAULT_STRATEGY", "bogus")
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code != 0
    assert "unknown strategy" in res.output.lower() and "bogus" in res.output
    # rejected before side effects: no cleanup, no Supervisor constructed
    assert h["cleanup"] == [] and "sup" not in h


def test_run_timeout_override(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    runner.invoke(cli, ["run", "do", "--timeout", "99"])
    cfg = h["config"]
    # the override loops over agent_priority, so every agent gets it (incl. the
    # hyphenated kimi-code → kimi_code field)
    assert cfg.claude.timeout_seconds == 99
    assert cfg.codex.timeout_seconds == 99
    assert cfg.kimi_code.timeout_seconds == 99


def test_run_warns_on_degraded_memory(monkeypatch, runner):
    _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")], degraded=True
    )
    res = runner.invoke(cli, ["run", "do"])
    assert "memory unavailable" in res.output


def test_run_warns_on_degraded_breaker(monkeypatch, runner):
    _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")],
        breaker_degraded=True,
    )
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert "circuit breaker state unavailable" in res.output


def test_run_no_breaker_notice_when_breaker_disabled(monkeypatch, runner):
    # the default (breaker None) prints no breaker line at all
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do"])
    assert "circuit breaker" not in res.output


def _point_toml(monkeypatch, tmp_path, body: str):
    """Write a syncmill.toml and point the config's toml source at it (absolute
    path), overriding the autouse isolate_toml_discovery default — so the CLI's
    own ``SyncmillConfig()`` loads it without depending on the test's cwd."""
    from syncmill.config import SyncmillConfig

    p = tmp_path / "syncmill.toml"
    p.write_text(body)
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", str(p))


def test_run_uses_toml_repo_root_when_no_flag(monkeypatch, runner, tmp_path):
    # with no -r flag, repo_root (and other fields) come from syncmill.toml, and
    # orphan cleanup targets that same resolved root — not the cwd.
    target = tmp_path / "myrepo"
    _point_toml(
        monkeypatch, tmp_path, f'repo_root = "{target}"\ndefault_strategy = "compete"\n'
    )
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert h["config"].repo_root == str(target)
    assert h["config"].default_strategy == "compete"
    assert h["cleanup"] == [target.resolve()]


def test_run_repo_flag_overrides_toml(monkeypatch, runner, tmp_path):
    # an explicit -r beats syncmill.toml's repo_root (CLI flag > file)
    _point_toml(monkeypatch, tmp_path, 'repo_root = "/from/toml"\n')
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "-r", str(tmp_path)])
    assert res.exit_code == 0
    assert h["config"].repo_root == str(tmp_path)


def test_run_empty_repo_flag_falls_back_to_toml(monkeypatch, runner, tmp_path):
    # `-r ""` is dropped like `-x ""`/`-c ""`, so repo_root still comes from the
    # file rather than being clobbered with cwd.
    target = tmp_path / "myrepo"
    _point_toml(monkeypatch, tmp_path, f'repo_root = "{target}"\n')
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "-r", ""])
    assert res.exit_code == 0
    assert h["config"].repo_root == str(target)


# ── agents ───────────────────────────────────────────────────────────────


def test_agents_lists_enabled_and_disabled(monkeypatch, runner):
    # registry only knows claude+codex; kimi-code (in default priority, absent from
    # the registry) shows disabled — and confirms the hyphenated id renders verbatim.
    _install_supervisor(
        monkeypatch,
        result=_result(),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["agents"])
    assert res.exit_code == 0
    assert "claude" in res.output and "[enabled]" in res.output
    assert "kimi-code" in res.output and "[disabled]" in res.output


# ── status ───────────────────────────────────────────────────────────────


def _install_memory(monkeypatch, info):
    closed = {"v": False}

    class Mem:
        def __init__(self, cfg):
            pass

        async def status(self):
            return info

        async def close(self):
            closed["v"] = True

    monkeypatch.setattr("syncmill.memory.SyncmillMemory", Mem)
    return closed


def test_status_connected(monkeypatch, runner):
    closed = _install_memory(monkeypatch, {"connected": True, "db_path": "/x/db"})
    res = runner.invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "memtomem: connected" in res.output
    assert "/x/db" in res.output
    assert closed["v"] is True


def test_status_disconnected(monkeypatch, runner):
    _install_memory(monkeypatch, {"connected": False, "error": "onnx not supported"})
    res = runner.invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "memtomem: disconnected" in res.output
    assert "onnx not supported" in res.output


def test_status_mem0_connected(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_MEMORY__BACKEND", "mem0")
    _install_memory(
        monkeypatch,
        {
            "connected": True,
            "data_dir": "/x/mem0",
            "vector_store": "qdrant",
            "collection": "syncmill",
            "embedder": "fastembed/model",
        },
    )
    res = runner.invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "mem0: connected" in res.output
    assert "data_dir: /x/mem0" in res.output
    assert "vector_store: qdrant" in res.output
    assert "collection: syncmill" in res.output
    assert "embedder: fastembed/model" in res.output


def test_status_mem0_disconnected(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_MEMORY__BACKEND", "mem0")
    _install_memory(
        monkeypatch,
        {
            "connected": False,
            "error": "install syncmill[mem0] or run `uv sync --extra mem0`",
        },
    )
    res = runner.invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "mem0: disconnected" in res.output
    assert "syncmill[mem0]" in res.output


def test_status_langgraph_connected(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_MEMORY__BACKEND", "langgraph")
    _install_memory(
        monkeypatch,
        {
            "connected": True,
            "db_path": "/x/langgraph.db",
            "store": "langgraph/AsyncSqliteStore",
            "embedder": "fastembed/model",
            "future_secret": "must-not-print",
        },
    )
    res = runner.invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "langgraph: connected" in res.output
    assert "db_path: /x/langgraph.db" in res.output
    assert "store: langgraph/AsyncSqliteStore" in res.output
    assert "embedder: fastembed/model" in res.output
    assert "must-not-print" not in res.output


def test_memory_migration_plan_cli(monkeypatch, runner, tmp_path):
    import syncmill.memory_migration as migration

    monkeypatch.setenv("SYNCMILL_MIGRATIONS_DIR", str(tmp_path))

    async def fake_plan(config, source, target, namespaces):
        assert source == "memtomem" and target == "mem0"
        assert namespaces == ("shared",)
        return "20260712-deadbeef", {"record_count": 3}

    monkeypatch.setattr(migration, "plan_migration", fake_plan)
    result = runner.invoke(
        cli,
        [
            "memory",
            "migration",
            "plan",
            "--from",
            "memtomem",
            "--to",
            "mem0",
            "--namespace",
            "shared",
        ],
    )
    assert result.exit_code == 0
    assert "migration planned: 20260712-deadbeef" in result.output
    assert "records: 3" in result.output


def test_memory_migration_rollback_requires_yes(runner):
    result = runner.invoke(
        cli, ["memory", "migration", "rollback", "20260712-deadbeef"]
    )
    assert result.exit_code == 2
    assert "requires --yes" in result.output


# ── serve (read-only MCP gateway) ──────────────────────────────────────────


def _install_serve(monkeypatch, *, degraded=False):
    """Stub the gateway + memory so `serve` never starts a real server or memtomem.

    Records the transport AND the run-time kwargs mcp.run received (host/port are
    run-time transport kwargs on the mcp 2.0 SDK, no longer construction args).
    """
    holder: dict = {}

    class FakeMem:
        def __init__(self, cfg):
            holder["mem_cfg"] = cfg
            self.degraded = degraded
            self.last_error = "boom" if degraded else None

        async def close(self):
            holder["closed"] = True

    class FakeServer:
        def run(self, transport="stdio", **kwargs):
            holder["transport"] = transport
            holder["run_kwargs"] = kwargs

    def fake_build(memory, *, board=None, board_error=None):
        holder["build"] = {"board": board, "board_error": board_error}
        return FakeServer()

    monkeypatch.setattr("syncmill.memory.SyncmillMemory", FakeMem)
    monkeypatch.setattr("syncmill.gateway.build_gateway", fake_build)
    return holder


def test_serve_stdio_default(monkeypatch, runner):
    h = _install_serve(monkeypatch)
    res = runner.invoke(cli, ["serve"])
    assert res.exit_code == 0
    assert h["transport"] == "stdio"
    assert h["run_kwargs"] == {}  # stdio takes no host/port
    assert h["closed"] is True  # memory closed in finally
    # stdio uses stdout as the JSON-RPC channel — notices go to stderr, stdout empty
    assert res.stdout == ""
    assert "stdio" in res.stderr


def test_serve_http_passes_host_port_to_run(monkeypatch, runner):
    h = _install_serve(monkeypatch)
    res = runner.invoke(
        cli, ["serve", "--http", "--host", "0.0.0.0", "--port", "8765"]
    )
    assert res.exit_code == 0
    assert h["transport"] == "streamable-http"
    # host/port reach the TRANSPORT (mcp 2.0: the SDK derives the DNS-rebinding
    # policy from the run-time host), not the constructor.
    assert h["run_kwargs"] == {"host": "0.0.0.0", "port": 8765}
    assert h["build"]["board"] is None  # board disabled by default → board tools report disabled
    assert "WARNING" in res.stderr  # non-loopback bind warns
    assert res.stdout == ""


def test_serve_http_loopback_security_note(monkeypatch, runner):
    h = _install_serve(monkeypatch)
    res = runner.invoke(cli, ["serve", "--http"])
    assert res.exit_code == 0
    assert h["transport"] == "streamable-http"
    assert h["run_kwargs"]["host"] == "127.0.0.1"  # loopback default reaches run()
    assert "no auth" in res.stderr.lower()


def test_serve_surfaces_degraded_memory_but_still_starts(monkeypatch, runner):
    _install_serve(monkeypatch, degraded=True)
    res = runner.invoke(cli, ["serve"])
    assert res.exit_code == 0
    assert "unavailable" in res.stderr.lower()


def test_serve_corrupt_board_passes_error_not_none_silently(monkeypatch, runner):
    # codex-found major: an enabled-but-corrupt board must NOT collapse to a silent
    # board=None ("not enabled"). serve threads board_error into build_gateway (so the
    # tools report "unavailable") AND warns — the shared gateway still starts.
    h = _install_serve(monkeypatch)
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setattr(
        "syncmill.cli.build_board",
        lambda cfg: (_ for _ in ()).throw(BoardError("corrupt board.db")),
    )
    res = runner.invoke(cli, ["serve"])
    assert res.exit_code == 0
    assert h["build"]["board"] is None
    assert "corrupt board.db" in h["build"]["board_error"]  # error threaded, not lost
    assert "unavailable" in res.stderr.lower()  # warned to stderr (stdio-safe)
    assert h["transport"] == "stdio"  # still starts despite degraded memory


# ── tmux monitor (#16): the outer-launch decision ─────────────────────────


def _patch_monitor_seams(
    monkeypatch, *, isatty=True, tmux=True, inner=None, launch_code=0, launch_raises=None
):
    """Patch the monitor seams the CLI consults, so the outer-launch truth table is
    driven hermetically (no real tmux, no real TTY, no real signal handlers)."""
    from syncmill.monitor import MonitorLaunchError

    holder = {"launch": None, "code": launch_code, "signals": False}

    monkeypatch.setattr("syncmill.cli._stdout_isatty", lambda: isatty)
    monkeypatch.setattr("syncmill.cli.tmux_available", lambda: tmux)
    monkeypatch.setattr("syncmill.cli.inner_dir", lambda: inner)
    monkeypatch.setattr(
        "syncmill.cli._install_inner_signal_handlers",
        lambda: holder.__setitem__("signals", True),
    )

    def fake_launch(
        inner_argv, *, session_prefix="syncmill", emit_patch=None, report_runs_dir=None
    ):
        holder["launch"] = {
            "argv": inner_argv,
            "prefix": session_prefix,
            "emit_patch": emit_patch,
            "report_runs_dir": report_runs_dir,
        }
        if launch_raises is not None:
            raise MonitorLaunchError(launch_raises)
        return holder["code"]

    monkeypatch.setattr("syncmill.cli.launch_monitored_run", fake_launch)
    return holder


def test_monitor_launches_for_concurrent_when_viable(monkeypatch, runner):
    sup = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch, launch_code=0)
    res = runner.invoke(cli, ["run", "do it", "-s", "compete", "--monitor"])
    assert res.exit_code == 0
    # launched inside tmux with the reconstructed argv; the Supervisor never ran here.
    # Options first, then `--`, then the prompt LAST (dash-prompt safe).
    assert mon["launch"]["argv"] == [
        "syncmill", "run", "--strategy", "compete", "--", "do it",
    ]
    assert "run" not in sup  # supervisor.run was NOT called on the outer/launch path


def test_monitor_launch_exit_code_propagates(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    _patch_monitor_seams(monkeypatch, launch_code=1)
    res = runner.invoke(cli, ["run", "x", "-s", "council", "--monitor"])
    assert res.exit_code == 1


def test_monitor_inner_argv_reconstruction(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(
        cli,
        ["run", "task", "-s", "decompose", "--monitor", "-x", "kimi-code",
         "-t", "60", "-c", "python"],
    )
    assert res.exit_code == 0
    argv = mon["launch"]["argv"]
    assert argv[:2] == ["syncmill", "run"]
    assert argv[-2:] == ["--", "task"]  # prompt LAST, behind the end-of-options marker
    assert "--exclude" in argv and "kimi-code" in argv
    assert "--timeout" in argv and "60" in argv
    assert "--capability" in argv and "python" in argv
    assert "--strategy" in argv and "decompose" in argv
    assert "--monitor" not in argv  # never re-passed → no relaunch recursion


def test_monitor_dash_prefixed_prompt_round_trips(monkeypatch, runner):
    # A prompt that looks like an option must be passed positionally to the OUTER
    # (`run -- "--weird"`) and reconstructed behind `--` so the inner's click parser
    # treats it as the prompt, not an unknown option.
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(cli, ["run", "-s", "compete", "--monitor", "--", "--fix-auth"])
    assert res.exit_code == 0
    argv = mon["launch"]["argv"]
    assert argv[-2:] == ["--", "--fix-auth"]  # round-trips intact, not mis-parsed
    # for THIS input (no option whose value is "--") exactly one `--` is emitted: the
    # separator, not a doubled-up token. (A literal `-c --` value would legitimately
    # add a second; the inner still parses correctly, so this is input-scoped.)
    assert argv.count("--") == 1


def test_monitor_skips_sequential_strategy(monkeypatch, runner):
    sup = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(cli, ["run", "do", "-s", "route", "--monitor"])
    assert res.exit_code == 0
    assert mon["launch"] is None  # not launched
    assert "applies only to concurrent strategies" in res.output
    assert "run" in sup  # fell through to a normal run


# ── run --emit-patch (winner-diff recovery) ─────────────────────────────────


def test_run_emit_patch_writes_file(monkeypatch, runner, tmp_path):
    dest = tmp_path / "win.patch"
    _install_supervisor(
        monkeypatch, result=_result(patch="diff --git x"), agents=[FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do", "--emit-patch", str(dest)])
    assert res.exit_code == 0
    assert dest.read_text() == "diff --git x"
    assert f"Patch written: {dest}" in res.output and "git apply" in res.output


def test_run_emit_patch_no_diff_notice(monkeypatch, runner, tmp_path):
    dest = tmp_path / "win.patch"
    _install_supervisor(
        monkeypatch, result=_result(patch=None), agents=[FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do", "--emit-patch", str(dest)])
    assert res.exit_code == 0  # a clean/non-git winner is not a failure
    assert "no diff to emit" in res.output
    assert not dest.exists()


def test_run_emit_patch_failed_run_writes_nothing(monkeypatch, runner, tmp_path):
    dest = tmp_path / "win.patch"
    _install_supervisor(
        monkeypatch,
        result=_result(status=TaskStatus.FAILED, error="boom", patch="stale"),
        agents=[FakeAgent("claude")],
    )
    res = runner.invoke(cli, ["run", "do", "--emit-patch", str(dest)])
    assert res.exit_code == 1  # the run's failure code, unchanged
    assert "did not complete" in res.output
    assert not dest.exists()


def test_run_emit_patch_write_failure_exits_nonzero(monkeypatch, runner, tmp_path):
    # an explicit export request that can't be honored must not exit 0
    dest = tmp_path / "no_such_dir" / "win.patch"
    _install_supervisor(
        monkeypatch, result=_result(patch="d"), agents=[FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do", "--emit-patch", str(dest)])
    assert res.exit_code == 1
    assert "failed to write" in res.output


def test_monitor_emit_patch_outer_owned_not_in_inner_argv(monkeypatch, runner, tmp_path):
    # --emit-patch is the OUTER's job (written from result.json): it must reach
    # launch_monitored_run as a kwarg and NEVER ride the reconstructed inner argv
    # (like --monitor itself).
    dest = tmp_path / "win.patch"
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch, launch_code=0)
    res = runner.invoke(
        cli, ["run", "do", "-s", "compete", "--monitor", "--emit-patch", str(dest)]
    )
    assert res.exit_code == 0
    assert "--emit-patch" not in mon["launch"]["argv"]
    assert mon["launch"]["emit_patch"] == str(dest)


def test_monitor_skips_without_tty(monkeypatch, runner):
    sup = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch, isatty=False)
    res = runner.invoke(cli, ["run", "do", "-s", "compete", "--monitor"])
    assert res.exit_code == 0
    assert mon["launch"] is None
    assert "interactive terminal" in res.output
    assert "run" in sup


def test_monitor_skips_without_tmux(monkeypatch, runner):
    sup = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch, tmux=False)
    res = runner.invoke(cli, ["run", "do", "-s", "compete", "--monitor"])
    assert res.exit_code == 0
    assert mon["launch"] is None
    assert "tmux installed" in res.output
    assert "run" in sup


def test_monitor_inner_process_does_not_relaunch(monkeypatch, runner):
    sup = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch, inner="/tmp/run-dir")
    res = runner.invoke(cli, ["run", "do", "-s", "compete", "--monitor"])
    assert res.exit_code == 0
    assert mon["launch"] is None  # we ARE the inner → never re-launch
    assert mon["signals"] is True  # inner installs the SIGHUP/SIGTERM handler
    assert "run" in sup  # the inner runs the real orchestration


def test_monitor_launch_failure_falls_back(monkeypatch, runner):
    sup = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    _patch_monitor_seams(monkeypatch, launch_raises="server unreachable")
    res = runner.invoke(cli, ["run", "do", "-s", "compete", "--monitor"])
    assert res.exit_code == 0
    assert "tmux session failed to start" in res.output
    assert "run" in sup  # fell back to a normal run


def test_monitor_off_by_default_no_seam_calls(monkeypatch, runner):
    sup = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(cli, ["run", "do", "-s", "compete"])  # no --monitor
    assert res.exit_code == 0
    assert mon["launch"] is None  # enabled gate: nothing happens without --monitor
    assert "run" in sup


# ── session group (#17): durable interactive tmux sessions ────────────────


def _patch_session_seams(
    monkeypatch,
    *,
    isatty=True,
    tmux=True,
    start_code=0,
    start_raises=None,
    attach_code=0,
    attach_raises=None,
    sessions=None,
    stop_count=0,
):
    """Patch the session CLI seams (gates + the four session.py functions, at the
    `syncmill.cli` lookup site) so the command surface is driven hermetically — no
    real tmux, TTY, or attach."""
    from syncmill.session import SessionLaunchError

    holder = {"start": None, "attach": None, "stop": None, "listed": False}

    monkeypatch.setattr("syncmill.cli._stdout_isatty", lambda: isatty)
    monkeypatch.setattr("syncmill.cli.tmux_available", lambda: tmux)

    def fake_start(agent_id, workdir):
        holder["start"] = {"agent": agent_id, "workdir": workdir}
        if start_raises is not None:
            raise SessionLaunchError(start_raises)
        return start_code

    def fake_attach(target):
        holder["attach"] = {"target": target}
        if attach_raises is not None:
            raise SessionLaunchError(attach_raises)
        return attach_code

    def fake_list():
        holder["listed"] = True
        return sessions or []

    def fake_stop(target, all_):
        holder["stop"] = {"target": target, "all": all_}
        return stop_count

    monkeypatch.setattr("syncmill.cli.start_session", fake_start)
    monkeypatch.setattr("syncmill.cli.attach_session", fake_attach)
    monkeypatch.setattr("syncmill.cli.list_sessions", fake_list)
    monkeypatch.setattr("syncmill.cli.stop_sessions", fake_stop)
    return holder


def test_session_help_lists_subcommands(runner):
    res = runner.invoke(cli, ["session", "--help"])
    assert res.exit_code == 0
    for sub in ("start", "list", "attach", "stop"):
        assert sub in res.output


def test_session_start_default_agent(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch)
    res = runner.invoke(cli, ["session", "start"])
    assert res.exit_code == 0
    assert h["start"]["agent"] == "claude"  # first priority entry with a mapping
    assert Path(h["start"]["workdir"]).is_dir()  # resolved repo_root (cwd)


def test_session_start_explicit_hyphenated_agent(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch)
    res = runner.invoke(cli, ["session", "start", "kimi-code"])
    assert res.exit_code == 0
    assert h["start"]["agent"] == "kimi-code"


def test_session_start_repo_flag_resolved(monkeypatch, runner, tmp_path):
    h = _patch_session_seams(monkeypatch)
    res = runner.invoke(cli, ["session", "start", "-r", str(tmp_path)])
    assert res.exit_code == 0
    assert h["start"]["workdir"] == str(tmp_path.resolve())


def test_session_start_repo_missing_dir_rejected(monkeypatch, runner, tmp_path):
    # The is_dir() guard rejects a nonexistent -r path up front: tmux -c would
    # otherwise silently fall back to $HOME, launching the session in the wrong
    # tree. The guard fires before any launch (h["start"] stays None).
    h = _patch_session_seams(monkeypatch)
    missing = tmp_path / "does-not-exist" / "repo"
    res = runner.invoke(cli, ["session", "start", "-r", str(missing)])
    assert res.exit_code != 0
    assert "not a directory" in res.output.lower()
    assert h["start"] is None  # validated before start_session is ever called


def test_session_start_unknown_agent(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch)
    res = runner.invoke(cli, ["session", "start", "clade"])
    assert res.exit_code != 0
    assert "unknown agent" in res.output.lower()
    assert h["start"] is None  # validated before any launch


def test_session_start_no_tmux_hard_error(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, tmux=False)
    res = runner.invoke(cli, ["session", "start"])
    assert res.exit_code != 0
    assert "tmux" in res.output.lower()
    assert h["start"] is None  # no fallback — start never runs


def test_session_start_no_tty(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, isatty=False)
    res = runner.invoke(cli, ["session", "start"])
    assert res.exit_code != 0
    assert "terminal" in res.output.lower()
    assert h["start"] is None


def test_session_start_launch_failure_no_fallback(monkeypatch, runner):
    _patch_session_seams(monkeypatch, start_raises="tmux server unreachable")
    res = runner.invoke(cli, ["session", "start"])
    assert res.exit_code != 0
    assert "failed to start" in res.output
    assert "running without" not in res.output  # NOT the --monitor-style fallback


def test_session_start_exit_code_propagates(monkeypatch, runner):
    _patch_session_seams(monkeypatch, start_code=3)
    res = runner.invoke(cli, ["session", "start"])
    assert res.exit_code == 3


def test_session_start_empty_priority(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_AGENT_PRIORITY", "[]")
    h = _patch_session_seams(monkeypatch)
    res = runner.invoke(cli, ["session", "start"])
    assert res.exit_code != 0
    assert "no interactive agent" in res.output.lower()
    assert h["start"] is None


def test_session_list_works_without_tty(monkeypatch, runner):
    from syncmill.session import SessionInfo

    h = _patch_session_seams(
        monkeypatch,
        isatty=False,  # list needs no TTY
        sessions=[
            SessionInfo("syncmill-session-claude", True),
            SessionInfo("syncmill-session-codex", False),
        ],
    )
    res = runner.invoke(cli, ["session", "list"])
    assert res.exit_code == 0
    assert "syncmill-session-claude" in res.output and "attached" in res.output
    assert "syncmill-session-codex" in res.output and "detached" in res.output
    assert h["listed"] is True


def test_session_list_empty(monkeypatch, runner):
    _patch_session_seams(monkeypatch, sessions=[])
    res = runner.invoke(cli, ["session", "list"])
    assert res.exit_code == 0
    assert "no active sessions" in res.output


def test_session_list_no_tmux(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, tmux=False)
    res = runner.invoke(cli, ["session", "list"])
    assert res.exit_code != 0
    assert "tmux" in res.output.lower()
    assert h["listed"] is False


def test_session_attach_invokes(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, attach_code=0)
    res = runner.invoke(cli, ["session", "attach", "codex"])
    assert res.exit_code == 0
    assert h["attach"]["target"] == "codex"


def test_session_attach_exit_code_propagates(monkeypatch, runner):
    _patch_session_seams(monkeypatch, attach_code=3)
    res = runner.invoke(cli, ["session", "attach", "codex"])
    assert res.exit_code == 3  # sys.exit(attach_session(...)) propagates the rc


def test_session_attach_no_tty(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, isatty=False)
    res = runner.invoke(cli, ["session", "attach", "codex"])
    assert res.exit_code != 0
    assert h["attach"] is None


def test_session_attach_nonexistent_surfaced(monkeypatch, runner):
    _patch_session_seams(monkeypatch, attach_raises="no such session: ghost")
    res = runner.invoke(cli, ["session", "attach", "ghost"])
    assert res.exit_code != 0
    assert "no such session" in res.output


def test_session_stop_one(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, stop_count=1)
    res = runner.invoke(cli, ["session", "stop", "claude"])
    assert res.exit_code == 0
    assert h["stop"] == {"target": "claude", "all": False}
    assert "stopped 1" in res.output


def test_session_stop_all(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, stop_count=2)
    res = runner.invoke(cli, ["session", "stop", "--all"])
    assert res.exit_code == 0
    assert h["stop"] == {"target": None, "all": True}
    assert "stopped 2" in res.output


def test_session_stop_requires_target_or_all(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch)
    res = runner.invoke(cli, ["session", "stop"])
    assert res.exit_code == 2  # UsageError
    assert "TARGET or --all" in res.output
    assert h["stop"] is None


def test_session_stop_target_and_all_exclusive(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch)
    res = runner.invoke(cli, ["session", "stop", "claude", "--all"])
    assert res.exit_code == 2
    assert "mutually exclusive" in res.output
    assert h["stop"] is None


def test_session_stop_no_tmux(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, tmux=False)
    res = runner.invoke(cli, ["session", "stop", "claude"])
    assert res.exit_code != 0
    assert "tmux" in res.output.lower()
    assert h["stop"] is None


def test_session_stop_nonexistent_zero(monkeypatch, runner):
    h = _patch_session_seams(monkeypatch, stop_count=0)
    res = runner.invoke(cli, ["session", "stop", "claude"])
    assert res.exit_code == 0
    assert "stopped 0" in res.output
    assert h["stop"] == {"target": "claude", "all": False}


# ── board (Phase 1: persistent work-board) ─────────────────────────────────


@pytest.fixture
def board_env(monkeypatch, tmp_path):
    """Enable the board against a throwaway SQLite db, so board CLI tests drive the
    REAL Board without touching the user's ~/.syncmill (hermetic, like
    isolated_home). Returns the db path."""
    db = tmp_path / "board.db"
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(db))
    return db


def _add(runner, task, *args):
    res = runner.invoke(cli, ["board", "add", task, *args])
    assert res.exit_code == 0, res.output
    # the printed line is "[syncmill] added item <id>"
    return res.output.split("added item")[1].split()[0].strip()


def _install_board_supervisor(
    monkeypatch, *, results, agents=None, mem_degraded=False, breaker_degraded=False,
    preflight_blocked=False, board_warnings=None,
):
    """Patch cli.Supervisor with a fake whose run_item yields ``results`` in order
    (a list; None entries model 'nothing to run' / loop drain). The fake exposes
    registry/memory/breaker so board_run's --agent validation + degraded notices
    can be exercised."""
    holder: dict = {"run_item_calls": [], "cleanup": []}
    seq = list(results)
    # One lease warning per run_item call (the real seam is reset per call), so the
    # --loop path can be checked for a per-ITEM notice rather than a post-drain one.
    warnings = list(board_warnings or [])
    lineup = agents if agents is not None else [FakeAgent("claude"), FakeAgent("codex")]

    class Sup:
        def __init__(self, config):
            holder["config"] = config
            self.closed = False
            self.registry = FakeRegistry(lineup)
            self.memory = types.SimpleNamespace(degraded=mem_degraded, last_error="boom")
            self.breaker = (
                types.SimpleNamespace(degraded=True, last_error="state unreadable")
                if breaker_degraded
                else None
            )
            # mirror the real Supervisor's report seam (None = no report line)
            self.last_report_path = None
            self.last_preflight_blocked = preflight_blocked
            self.last_board_warning = None
            holder["sup"] = self

        def run_item(self, item_id=None, agent_id=None, exclude=None, goal=None):
            holder["run_item_calls"].append(
                {"item_id": item_id, "agent_id": agent_id, "goal": goal}
            )
            self.last_board_warning = warnings.pop(0) if warnings else None
            return seq.pop(0) if seq else None

        def close(self):
            self.closed = True

    monkeypatch.setattr("syncmill.cli.Supervisor", Sup)
    monkeypatch.setattr(
        "syncmill.cli.cleanup_orphaned_worktrees", lambda p: holder["cleanup"].append(p)
    )
    return holder


def test_board_disabled_errors(runner):
    # no SYNCMILL_BOARD__ENABLED → every subcommand fails with a clear message
    for args in (["board", "add", "x"], ["board", "list"], ["board", "show", "id"],
                 ["board", "run"], ["board", "update", "id", "--state", "review"],
                 ["board", "decide", "x"], ["board", "decisions"], ["board", "stats"]):
        res = runner.invoke(cli, args)
        assert res.exit_code != 0
        assert "disabled" in res.output


def test_board_add_and_list(board_env, runner):
    iid = _add(runner, "build the parser", "--goal", "parsing")
    res = runner.invoke(cli, ["board", "list"])
    assert res.exit_code == 0
    assert iid in res.output
    assert "build the parser" in res.output
    assert "planned" in res.output


def test_board_import_review_candidates_is_repeat_safe(board_env, runner):
    source = Path(__file__).parent / "fixtures" / "tracegraph" / "v1.json"

    first = runner.invoke(
        cli,
        ["board", "import-review-candidates", str(source), "--goal", "governance"],
    )
    second = runner.invoke(
        cli,
        ["board", "import-review-candidates", str(source), "--goal", "ignored"],
    )
    listing = runner.invoke(cli, ["board", "list", "--state", "review"])

    assert first.exit_code == second.exit_code == listing.exit_code == 0
    assert "imported 1" in first.output and "skipped 0" in first.output
    assert "imported 0" in second.output and "skipped 1" in second.output
    # goal is assigned on first import only — the re-import under a new --goal
    # says so instead of reading as if the new goal took effect
    assert "kept their original goal" not in first.output
    assert "(1 kept their original goal)" in second.output
    assert "[governance] Review Tracegraph tool-retry-failure@v2" in listing.output
    assert "human-required" in listing.output


def test_board_import_invalid_report_does_not_create_database(board_env, runner, tmp_path):
    source = tmp_path / "bad.json"
    source.write_text('{"schema_version": 2}', encoding="utf-8")

    result = runner.invoke(cli, ["board", "import-review-candidates", str(source)])

    assert result.exit_code != 0
    assert "schema_version" in result.output
    assert not board_env.exists()


def test_board_import_requires_enabled_board(runner):
    source = Path(__file__).parent / "fixtures" / "tracegraph" / "v1.json"
    result = runner.invoke(cli, ["board", "import-review-candidates", str(source)])
    assert result.exit_code != 0
    assert "disabled" in result.output


def test_board_add_human_required_and_deps(board_env, runner):
    dep = _add(runner, "dependency")
    iid = _add(runner, "main", "--depends-on", dep, "--human-required")
    res = runner.invoke(cli, ["board", "show", iid])
    assert res.exit_code == 0
    assert "human:    REQUIRED" in res.output
    assert dep in res.output


def test_board_list_filter_by_state(board_env, runner):
    _add(runner, "one")
    res = runner.invoke(cli, ["board", "list", "--state", "done"])
    assert res.exit_code == 0
    assert "empty" in res.output  # nothing is done yet


def test_board_show_missing(board_env, runner):
    res = runner.invoke(cli, ["board", "show", "no-such-id"])
    assert res.exit_code != 0
    assert "no such item" in res.output


def test_board_update_bare_done_rejected(board_env, runner):
    iid = _add(runner, "x")
    runner.invoke(cli, ["board", "update", iid, "--state", "review"])
    res = runner.invoke(cli, ["board", "update", iid, "--state", "done"])
    assert res.exit_code != 0
    assert "without verification" in res.output


def test_board_update_done_with_signoff(board_env, runner):
    iid = _add(runner, "x")
    res = runner.invoke(cli, ["board", "update", iid, "--state", "done", "--sign-off"])
    assert res.exit_code == 0
    assert "state:    done" in res.output


def test_board_update_done_with_evidence(board_env, runner):
    iid = _add(runner, "x")
    res = runner.invoke(
        cli, ["board", "update", iid, "--state", "done", "--evidence", "ci#42"]
    )
    assert res.exit_code == 0
    assert "state:    done" in res.output
    assert "ci#42" in res.output


def test_board_update_clear_human_required(board_env, runner):
    iid = _add(runner, "x", "--human-required")
    res = runner.invoke(cli, ["board", "update", iid, "--clear-human-required"])
    assert res.exit_code == 0
    assert "human:    REQUIRED" not in res.output


def test_board_update_nothing_given(board_env, runner):
    iid = _add(runner, "x")
    res = runner.invoke(cli, ["board", "update", iid])
    assert res.exit_code != 0
    assert "nothing to update" in res.output


def test_board_run_single(board_env, monkeypatch, runner):
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="claude")])
    res = runner.invoke(cli, ["board", "run"])
    assert res.exit_code == 0
    assert "[claude] Completed" in res.output
    assert h["run_item_calls"] == [{"item_id": None, "agent_id": None, "goal": None}]
    assert h["sup"].closed is True
    assert len(h["cleanup"]) == 1


def test_board_run_by_id_and_agent(board_env, monkeypatch, runner):
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="codex")])
    res = runner.invoke(cli, ["board", "run", "--id", "item-7", "--agent", "codex"])
    assert res.exit_code == 0
    assert h["run_item_calls"] == [
        {"item_id": "item-7", "agent_id": "codex", "goal": None}
    ]


def test_board_run_strategy_mutates_config_pre_construction(board_env, monkeypatch, runner):
    h = _install_board_supervisor(monkeypatch, results=[_result()])
    res = runner.invoke(cli, ["board", "run", "--strategy", "compete"])
    assert res.exit_code == 0
    # the Supervisor was built with default_strategy already set to compete
    assert h["config"].default_strategy == "compete"


def test_board_run_nothing_to_run(board_env, monkeypatch, runner):
    _install_board_supervisor(monkeypatch, results=[None])
    res = runner.invoke(cli, ["board", "run"])
    assert res.exit_code == 0
    assert "nothing to run" in res.output


def test_board_run_unclaimable_id(board_env, monkeypatch, runner):
    _install_board_supervisor(monkeypatch, results=[None])
    res = runner.invoke(cli, ["board", "run", "--id", "ghost"])
    assert res.exit_code == 0
    assert "not claimable" in res.output


def test_board_run_loop_drains(board_env, monkeypatch, runner):
    h = _install_board_supervisor(
        monkeypatch, results=[_result(agent_id="claude"), _result(agent_id="codex")]
    )
    res = runner.invoke(cli, ["board", "run", "--loop"])
    assert res.exit_code == 0
    assert len(h["run_item_calls"]) == 3  # two items + the None that stops the loop
    assert "drained 2 item(s)" in res.output


def test_board_run_loop_and_id_mutually_exclusive(board_env, monkeypatch, runner):
    _install_board_supervisor(monkeypatch, results=[])
    res = runner.invoke(cli, ["board", "run", "--loop", "--id", "x"])
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def test_board_run_failed_exits_nonzero(board_env, monkeypatch, runner):
    _install_board_supervisor(
        monkeypatch, results=[_result(status=TaskStatus.FAILED, agent_id="none", error="boom")]
    )
    res = runner.invoke(cli, ["board", "run"])
    assert res.exit_code == 1
    assert "Failed" in res.output


def test_board_run_unknown_agent_rejected_without_claiming(board_env, monkeypatch, runner):
    # FIX: an unknown/disabled --agent must be rejected UP FRONT — never claim+block
    # a real backlog item on a typo.
    h = _install_board_supervisor(monkeypatch, results=[_result()])
    res = runner.invoke(cli, ["board", "run", "--agent", "ghost"])
    assert res.exit_code != 0
    assert "unknown or disabled agent" in res.output
    assert h["run_item_calls"] == []  # nothing claimed/run


def test_board_run_loop_exits_nonzero_when_an_item_failed(board_env, monkeypatch, runner):
    # FIX: --loop must propagate failure as a nonzero exit (CI/cron signal), not
    # silently exit 0 after draining a backlog where items failed.
    _install_board_supervisor(
        monkeypatch,
        results=[_result(agent_id="claude"), _result(status=TaskStatus.FAILED, agent_id="codex", error="x")],
    )
    res = runner.invoke(cli, ["board", "run", "--loop"])
    assert res.exit_code == 1
    assert "drained 2 item(s)" in res.output


def test_board_run_loop_stops_after_strict_preflight_block(board_env, monkeypatch, runner):
    h = _install_board_supervisor(
        monkeypatch,
        results=[
            _result(status=TaskStatus.FAILED, agent_id="preflight", error="blocked"),
            _result(agent_id="codex"),
        ],
        preflight_blocked=True,
    )
    res = runner.invoke(cli, ["board", "run", "--loop"])
    assert res.exit_code == 1
    assert len(h["run_item_calls"]) == 1
    assert "loop stopped after strict preflight block" in res.output


def test_board_run_surfaces_degraded_memory(board_env, monkeypatch, runner):
    # FIX: a degraded memtomem (the board-result cross-link share may not have landed)
    # is surfaced, like plain `run`.
    _install_board_supervisor(monkeypatch, results=[_result()], mem_degraded=True)
    res = runner.invoke(cli, ["board", "run"])
    assert res.exit_code == 0
    assert "memory degraded" in res.output


def test_board_run_surfaces_degraded_breaker(board_env, monkeypatch, runner):
    # COVERAGE: a degraded circuit breaker (unreadable state file) is surfaced on a
    # board run, like plain `run` — not silently dropped.
    _install_board_supervisor(monkeypatch, results=[_result()], breaker_degraded=True)
    res = runner.invoke(cli, ["board", "run"])
    assert res.exit_code == 0
    assert "circuit breaker state unavailable" in res.output


def test_board_run_surfaces_a_lost_lease(board_env, monkeypatch, runner):
    # A run whose lease was reclaimed mid-flight still returns COMPLETED and exits 0,
    # so without this notice the operator never learns the work was discarded.
    _install_board_supervisor(
        monkeypatch,
        results=[_result()],
        board_warnings=["lost the board lease on item item-1"],
    )
    res = runner.invoke(cli, ["board", "run"])
    assert res.exit_code == 0
    assert "lost the board lease on item item-1" in res.output
    assert "NOT recorded on the board" in res.output


def test_board_run_loop_surfaces_a_lost_lease_per_item(board_env, monkeypatch, runner):
    # The seam is reset per run_item, so the notice must be printed INSIDE the loop —
    # after the drain only the last item's warning would survive.
    _install_board_supervisor(
        monkeypatch,
        results=[_result(agent_id="claude"), _result(agent_id="codex")],
        board_warnings=["lost the board lease on item item-1", None],
    )
    res = runner.invoke(cli, ["board", "run", "--loop"])
    assert res.exit_code == 0
    assert res.output.count("NOT recorded on the board") == 1
    assert "drained 2 item(s)" in res.output


def test_board_update_state_running_rejected(board_env, runner):
    # FIX (W3): the human update path does not offer `running` (claim-lease-owned) —
    # click rejects it as an invalid choice before any board write.
    iid = _add(runner, "x")
    res = runner.invoke(cli, ["board", "update", iid, "--state", "running"])
    assert res.exit_code != 0
    assert "running" in res.output  # invalid choice surfaced


# ── board decide / decisions / stats (Phase 2) ───────────────────────────────


def _decide(runner, text, *args):
    res = runner.invoke(cli, ["board", "decide", text, *args])
    assert res.exit_code == 0, res.output
    return res.output.split("recorded decision")[1].split()[0].strip()


def test_board_decide_and_decisions_list(board_env, runner):
    _decide(runner, "always run pytest")
    _decide(runner, "target python 3.12", "--scope", "epic")
    res = runner.invoke(cli, ["board", "decisions"])
    assert res.exit_code == 0
    assert "always run pytest" in res.output
    assert "target python 3.12" in res.output


def test_board_decisions_filter_by_goal(board_env, runner):
    _decide(runner, "global rule")                       # scope="" (global)
    _decide(runner, "epic rule", "--scope", "epic")
    _decide(runner, "other rule", "--scope", "other")
    res = runner.invoke(cli, ["board", "decisions", "--goal", "epic"])
    assert res.exit_code == 0
    assert "global rule" in res.output and "epic rule" in res.output
    assert "other rule" not in res.output  # other-scoped excluded


def test_board_decide_supersede_retires_old(board_env, runner):
    old = _decide(runner, "use postgres", "--scope", "epic")
    _decide(runner, "use sqlite", "--scope", "epic", "--supersedes", old)
    res = runner.invoke(cli, ["board", "decisions", "--goal", "epic"])
    assert "use sqlite" in res.output
    assert "use postgres" not in res.output  # the superseded one drops out


def test_board_decide_supersede_missing_errors(board_env, runner):
    res = runner.invoke(
        cli, ["board", "decide", "x", "--supersedes",
              "00000000-0000-0000-0000-000000000000"],
    )
    assert res.exit_code != 0
    assert "no such decision" in res.output


def test_board_decide_empty_text_rejected(board_env, runner):
    res = runner.invoke(cli, ["board", "decide", "   "])
    assert res.exit_code != 0
    assert "non-empty" in res.output


def test_board_decisions_empty(board_env, runner):
    res = runner.invoke(cli, ["board", "decisions"])
    assert res.exit_code == 0
    assert "no active decisions" in res.output


def test_board_stats_report(board_env, runner):
    _add(runner, "task one")
    _add(runner, "task two")
    res = runner.invoke(cli, ["board", "stats"])
    assert res.exit_code == 0
    assert "items: 2" in res.output
    assert "planned=2" in res.output


def test_board_compact_trims_and_reports(board_env, runner):
    _add(runner, "task one")  # writes at least one audit row
    res = runner.invoke(cli, ["board", "compact", "--older-than", "0"])
    assert res.exit_code == 0, res.output
    assert "Compacted" in res.output
    # stats still works over the compacted board
    res = runner.invoke(cli, ["board", "stats"])
    assert res.exit_code == 0


def test_board_compact_rejects_a_negative_window(board_env, runner):
    _add(runner, "task one")
    # -1e-999 underflows to signed negative zero, which FloatRange(min=0)
    # accepts and which would then trim as 0 — the whole audit.
    for bad in ("-1", "-1e-999"):
        res = runner.invoke(cli, ["board", "compact", "--older-than", bad])
        assert res.exit_code != 0, res.output
    ok = runner.invoke(cli, ["board", "list"])
    assert "task one" in ok.output


def test_board_compact_disabled_board_errors(runner, monkeypatch, tmp_path):
    monkeypatch.delenv("SYNCMILL_BOARD__ENABLED", raising=False)
    res = runner.invoke(cli, ["board", "compact"])
    assert res.exit_code != 0


# ── dashboard (Phase 3: live TUI over the work-board) ──────────────────────


def _patch_dashboard_seams(monkeypatch, *, isatty=True):
    """Patch the dashboard CLI seams (the TTY gate + `run_dashboard`, at the
    `syncmill.cli` lookup site) so the command surface is driven hermetically —
    no real TTY and no Textual app ever runs."""
    holder = {"run": None}

    monkeypatch.setattr("syncmill.cli._stdout_isatty", lambda: isatty)

    def fake_run(board, *, refresh_seconds, config=None, toml_path=None):
        holder["run"] = {
            "board": board,
            "refresh_seconds": refresh_seconds,
            "config": config,
            "toml_path": toml_path,
        }

    monkeypatch.setattr("syncmill.cli.run_dashboard", fake_run)
    return holder


def test_dashboard_disabled_errors(runner, monkeypatch):
    h = _patch_dashboard_seams(monkeypatch)
    res = runner.invoke(cli, ["dashboard"])
    assert res.exit_code != 0
    assert "disabled" in res.output
    assert h["run"] is None


def test_dashboard_requires_tty(board_env, runner, monkeypatch):
    h = _patch_dashboard_seams(monkeypatch, isatty=False)
    res = runner.invoke(cli, ["dashboard"])
    assert res.exit_code != 0
    assert "interactive terminal" in res.output
    assert h["run"] is None
    # the TTY gate fires BEFORE the db is opened — no board.db side effect
    assert not board_env.exists()


def test_dashboard_runs_and_closes_board(board_env, runner, monkeypatch):
    from syncmill.board import BoardError
    from syncmill.dashboard import DEFAULT_REFRESH_SECONDS

    h = _patch_dashboard_seams(monkeypatch)
    res = runner.invoke(cli, ["dashboard"])
    assert res.exit_code == 0, res.output
    assert h["run"]["refresh_seconds"] == DEFAULT_REFRESH_SECONDS
    bd = h["run"]["board"]
    assert bd is not None
    # the CLI's finally closed the board after run_dashboard returned
    with pytest.raises(BoardError):
        bd.list()


def test_dashboard_passes_refresh_flag_raw(board_env, runner, monkeypatch):
    # clamping lives in run_dashboard (unit-covered in test_dashboard.py) — the
    # CLI hands the flag through untouched
    h = _patch_dashboard_seams(monkeypatch)
    res = runner.invoke(cli, ["dashboard", "--refresh", "0.1"])
    assert res.exit_code == 0, res.output
    assert h["run"]["refresh_seconds"] == 0.1


def test_dashboard_threads_config_and_toml_path(board_env, runner, monkeypatch):
    # the TEAM pane inputs: the loaded config + the ONCE-resolved write target
    # (teamfile.toml_path() — the same file discovery reads)
    from syncmill.config import SyncmillConfig
    from syncmill.teamfile import toml_path

    monkeypatch.setenv("SYNCMILL_TEAMS", '{"demo": {"agents": ["codex"]}}')
    h = _patch_dashboard_seams(monkeypatch)
    res = runner.invoke(cli, ["dashboard"])
    assert res.exit_code == 0, res.output
    assert isinstance(h["run"]["config"], SyncmillConfig)
    assert h["run"]["config"].teams["demo"].agents == ["codex"]
    assert h["run"]["toml_path"] == toml_path()


def test_dashboard_corrupt_db_hard_fails(board_env, runner, monkeypatch):
    board_env.write_bytes(b"this is not a sqlite database at all")
    h = _patch_dashboard_seams(monkeypatch)
    res = runner.invoke(cli, ["dashboard"])
    assert res.exit_code != 0
    assert "failed to open board" in res.output
    assert h["run"] is None


def test_tui_opens_limited_mode_when_board_disabled(runner, monkeypatch, tmp_path):
    monkeypatch.setattr("syncmill.cli._stdout_isatty", lambda: True)
    captured = {}

    def fake_run(config, **kwargs):
        captured.update(config=config, **kwargs)

    monkeypatch.setattr("syncmill.tui.run_tui", fake_run)
    result = runner.invoke(cli, ["tui", "--refresh", "0.25"])
    assert result.exit_code == 0, result.output
    assert captured["board"] is None
    assert captured["board_error"] is None
    assert captured["refresh_seconds"] == 0.25


def test_tui_requires_tty_before_board_open(runner, monkeypatch):
    monkeypatch.setattr("syncmill.cli._stdout_isatty", lambda: False)
    opened = []
    monkeypatch.setattr("syncmill.cli.build_board", lambda config: opened.append(config))
    result = runner.invoke(cli, ["tui"])
    assert result.exit_code != 0
    assert "interactive terminal" in result.output
    assert opened == []


def test_tui_help_states_posix_execution_boundary(runner):
    result = runner.invoke(cli, ["tui", "--help"])
    assert result.exit_code == 0, result.output
    assert "Sessions/Rooms" in result.output
    assert "macOS/Linux" in result.output
    assert "use POSIX" in result.output


def test_init_lists_created_project_files_and_does_not_stage_them(runner, git_repo):
    result = runner.invoke(cli, ["init", str(git_repo)])

    assert result.exit_code == 0, result.output
    for relative in (
        "syncmill.toml",
        ".syncmill/project.json",
        ".syncmill/.gitignore",
    ):
        assert relative in result.output
        assert (git_repo / relative).is_file()
    assert "not staged automatically" in result.output
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout == ""


def test_beginner_onboarding_init_doctor_dry_run_is_hermetic(
    runner, git_repo, tmp_path, monkeypatch
):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    codex = fake_bin / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2" = "login status" ]; then exit 0; fi\n'
        "exit 2\n",
        encoding="utf-8",
    )
    codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    initialized = runner.invoke(cli, ["init", str(git_repo)])
    assert initialized.exit_code == 0, initialized.output

    checked = runner.invoke(cli, ["doctor", "--repo", str(git_repo)])
    assert checked.exit_code == 0, checked.output
    assert "Agent codex authentication: ready" in checked.output
    assert "Provider warnings:" in checked.output
    assert "Ready." in checked.output

    planned = runner.invoke(
        cli,
        [
            "run",
            "--dry-run",
            "--repo",
            str(git_repo),
            "Add one beginner example",
        ],
    )
    assert planned.exit_code == 0, planned.output
    assert '"strategy": "route"' in planned.output
    assert "codex" in planned.output
    assert "No agent was started and no source file was changed." in planned.output


def _patch_doctor_runtime(monkeypatch, tmp_path, *, found, auth_ready, config=None):
    if config is None:
        config = SyncmillConfig(agent_priority=["claude", "codex", "kimi-code"])
    # Mirror the real keyword-only signature: doctor loads through the shared
    # _target_project_config like every other project command.
    monkeypatch.setattr(
        "syncmill.cli.load_project_config",
        lambda repo, *, repo_root_override=None: (tmp_path, config),
    )
    monkeypatch.setattr(
        "syncmill.cli.shutil.which", lambda binary: found.get(binary)
    )

    def fake_run(argv, **kwargs):
        binary = argv[0]
        return subprocess.CompletedProcess(
            argv, 0 if auth_ready.get(binary, False) else 1, "", ""
        )

    monkeypatch.setattr("syncmill.cli.subprocess.run", fake_run)
    monkeypatch.setattr("syncmill.cli.subprocess_status_dirty", lambda root: False)
    snapshot_root = tmp_path / "snapshot"
    snapshot_root.mkdir()
    monkeypatch.setattr(
        "syncmill.cli.capture_repository_snapshot",
        lambda *args, **kwargs: types.SimpleNamespace(run_root=snapshot_root),
    )

    class Store:
        def __init__(self, root, project):
            self.root = Path(root) / project

    monkeypatch.setattr("syncmill.cli.ArtifactStore", Store)


def test_doctor_succeeds_when_one_enabled_provider_is_ready(
    monkeypatch, runner, tmp_path
):
    _patch_doctor_runtime(
        monkeypatch,
        tmp_path,
        found={"claude": None, "codex": "/bin/codex", "kimi": None},
        auth_ready={"codex": True},
    )

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Provider warnings:" in result.output
    assert "enabled agent binary not found: claude" in result.output
    assert "Ready." in result.output


def test_doctor_strict_fails_for_any_unready_enabled_provider(
    monkeypatch, runner, tmp_path
):
    _patch_doctor_runtime(
        monkeypatch,
        tmp_path,
        found={"claude": None, "codex": "/bin/codex", "kimi": None},
        auth_ready={"codex": True},
    )

    result = runner.invoke(cli, ["doctor", "--strict"])

    assert result.exit_code != 0
    assert "enabled agent binary not found: claude" in result.output
    assert "enabled agent binary not found: kimi" in result.output


def test_doctor_fails_when_no_enabled_provider_is_ready(
    monkeypatch, runner, tmp_path
):
    _patch_doctor_runtime(
        monkeypatch,
        tmp_path,
        found={"claude": None, "codex": None, "kimi": None},
        auth_ready={},
    )

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code != 0
    assert "no enabled agent is ready" in result.output


def test_doctor_unknown_agent_id_is_fatal_despite_a_ready_provider(
    monkeypatch, runner, tmp_path
):
    # AgentRegistry rejects unknown ids before running ANY agent, so a typo in
    # agent_priority must never ride the relaxed provider-warning policy into
    # a Ready. verdict.
    _patch_doctor_runtime(
        monkeypatch,
        tmp_path,
        found={"codex": "/bin/codex", "claud": None},
        auth_ready={"codex": True},
        config=SyncmillConfig(agent_priority=["codex", "claud"]),
    )

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code != 0
    assert "unknown agent id in agent_priority: claud" in result.output
    assert "kimi-code" in result.output  # the valid-id list names real ids
    assert "Ready." not in result.output


def test_doctor_fails_when_all_agents_disabled(monkeypatch, runner, tmp_path):
    # An all-disabled lineup used to print Ready.; nothing can run, so it is
    # now the same fatal "no enabled agent is ready" as an all-missing one.
    _patch_doctor_runtime(
        monkeypatch,
        tmp_path,
        found={"claude": "/bin/claude", "codex": "/bin/codex", "kimi": "/bin/kimi"},
        auth_ready={"claude": True, "codex": True},
        config=SyncmillConfig(
            agent_priority=["claude", "codex", "kimi-code"],
            claude={"enabled": False},
            codex={"enabled": False},
            kimi_code={"enabled": False},
        ),
    )

    result = runner.invoke(cli, ["doctor"])

    assert result.exit_code != 0
    assert "no enabled agent is ready" in result.output


def test_run_repo_without_toml_never_reads_the_callers_config(
    monkeypatch, runner, git_repo, tmp_path
):
    # A valid Git target WITHOUT syncmill.toml must resolve env > defaults AT
    # THE TARGET — not fall through to the caller's cwd toml, which would lend
    # the caller's project id/state roots/strategy to the target.
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "syncmill.toml").write_text(
        'config_version = 2\ndefault_strategy = "pipeline"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(caller)
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    planned = runner.invoke(
        cli, ["run", "--dry-run", "--repo", str(git_repo), "do"]
    )

    assert planned.exit_code == 0, planned.output
    assert '"strategy": "route"' in planned.output
    assert "pipeline" not in planned.output


def test_run_explicit_repo_beats_stale_repo_root_env(
    monkeypatch, runner, git_repo, tmp_path
):
    # The --repo flag is documented to override the environment: a stale
    # SYNCMILL_REPO_ROOT must not fail the target-config load (it used to
    # surface as a raw FileNotFoundError from git_root's subprocess cwd).
    monkeypatch.setenv("SYNCMILL_REPO_ROOT", str(tmp_path / "missing"))

    planned = runner.invoke(
        cli, ["run", "--dry-run", "--repo", str(git_repo), "do"]
    )

    assert planned.exit_code == 0, planned.output
    assert '"strategy": "route"' in planned.output


def test_run_surfaces_invalid_target_project_config(
    monkeypatch, runner, git_repo
):
    (git_repo / "syncmill.toml").write_text(
        "config_version = 2\nunknown_beginner_typo = true\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    result = runner.invoke(cli, ["run", "do", "--repo", str(git_repo)])

    assert result.exit_code != 0
    assert "invalid target project configuration" in result.output
    assert "unknown_beginner_typo" in result.output


def test_run_surfaces_malformed_target_project_toml(
    monkeypatch, runner, git_repo
):
    # tomllib.TOMLDecodeError is a ValueError from the settings source, not a
    # ValidationError — it must become the documented CLI error, not a
    # traceback.
    (git_repo / "syncmill.toml").write_text(
        "config_version = [broken\n", encoding="utf-8"
    )
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    result = runner.invoke(cli, ["run", "do", "--repo", str(git_repo)])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "invalid target project configuration" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["runs", "list"],
        ["diff", "0f0f0f0f"],
        ["apply", "0f0f0f0f"],
        ["discard", "0f0f0f0f"],
        ["recover"],
        ["conversations", "list"],
        ["runs", "prune", "--dry-run"],
        ["room", "list"],
        ["session", "isolated-list"],
        ["doctor"],
    ],
)
def test_target_commands_surface_malformed_target_toml(
    monkeypatch, runner, git_repo, command
):
    # Every --repo command that READS a project config loads the same target
    # syncmill.toml `run` does (the legacy `session start` is the documented
    # exception — its --repo is only a working directory), so the documented
    # "invalid target configuration is reported, never a silent fallback" must
    # hold across that whole surface. Each site used to carry its
    # own except tuple; the artifact, room, and session ones caught only
    # (ProjectError, ValidationError), so a TOMLDecodeError (a ValueError)
    # escaped as a traceback. They now share _target_project_config, and this
    # parametrization is what keeps a new --repo command from re-drifting.
    (git_repo / "syncmill.toml").write_text(
        "config_version = [broken\n", encoding="utf-8"
    )
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    result = runner.invoke(cli, [*command, "--repo", str(git_repo)])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "invalid target project configuration" in result.output


def test_a_non_repository_target_is_not_reported_as_a_config_error(
    runner, tmp_path
):
    # Failing to FIND a repository is discovery, not configuration: framing it as
    # "invalid target project configuration" sends the reader hunting for a bad
    # syncmill.toml that need not exist. ProjectDiscoveryError carries that
    # distinction; a config value naming a non-repository stays a config error.
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    result = runner.invoke(cli, ["runs", "list", "--repo", str(plain)])

    assert result.exit_code != 0
    assert "not a Git repository" in result.output
    assert "invalid target project configuration" not in result.output


def test_doctor_checks_the_configured_target_not_the_config_owner(
    monkeypatch, runner, git_repo, tmp_path
):
    # doctor snapshotted and fingerprinted the CONFIG-owning repository and
    # derived the artifact namespace from it, so with project A targeting B it
    # could report A ready while every run from A would have touched B. The
    # non-repository target is the cheapest proof of which tree it inspects.
    (git_repo / "syncmill.toml").write_text(
        f'config_version = 2\nrepo_root = "{(tmp_path / "plain").as_posix()}"\n',
        encoding="utf-8",
    )
    (tmp_path / "plain").mkdir()
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    result = runner.invoke(cli, ["doctor", "--repo", str(git_repo)])

    assert result.exit_code != 0
    assert "invalid target project configuration" in result.output


def test_doctor_reports_the_target_when_it_differs_from_the_config_owner(
    monkeypatch, runner, git_repo, tmp_path
):
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(
        ["git", "clone", "-q", str(git_repo), str(elsewhere)],
        check=True,
        capture_output=True,
    )
    (git_repo / "syncmill.toml").write_text(
        f'config_version = 2\nrepo_root = "{elsewhere.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    result = runner.invoke(cli, ["doctor", "--repo", str(git_repo)])

    # Which tree is inspected must be visible before anyone acts on the verdict.
    assert f"Target repository: {elsewhere.resolve()}" in result.output


def test_doctor_probes_the_targets_memory_and_leaves_the_config_owner_alone(
    monkeypatch, runner, git_repo, tmp_path
):
    # doctor probed `<config owner>/.syncmill/memory`, while a run binds project
    # memory under the CONFIGURED target — so it reported on the wrong tree's
    # writability and created project state in a checkout no run ever touches.
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(
        ["git", "clone", "-q", str(git_repo), str(elsewhere)],
        check=True,
        capture_output=True,
    )
    project_uuid = "11111111-2222-3333-4444-555555555555"
    (git_repo / "syncmill.toml").write_text(
        f'config_version = 2\nrepo_root = "{elsewhere.as_posix()}"\n\n'
        f'[project]\nid = "{project_uuid}"\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    result = runner.invoke(cli, ["doctor", "--repo", str(git_repo)])

    assert str(elsewhere.resolve() / ".syncmill" / "memory") in result.output
    assert (elsewhere / ".syncmill" / "memory").is_dir()
    assert not (git_repo / ".syncmill" / "memory").exists()


def test_repo_selects_the_project_whose_repo_root_may_point_elsewhere(
    monkeypatch, runner, git_repo, tmp_path
):
    """`--repo` names the PROJECT for project commands, not the working repo.

    `run --repo X` means "work on X" and pins `repo_root` to it. For the project
    commands it means "load X's configuration", whose `repo_root` legitimately
    names a DIFFERENT checkout — that is the relative-collaboration layout the
    session/room checkpoint tests exercise end to end. Pinning the flag onto
    `repo_root` here (an appealing way to make one stale `SYNCMILL_REPO_ROOT`
    stop defeating the flag) silently retargets those commands at themselves,
    which is why the two families deliberately differ.
    """
    elsewhere = tmp_path / "elsewhere"
    subprocess.run(
        ["git", "clone", "-q", str(git_repo), str(elsewhere)],
        check=True,
        capture_output=True,
    )
    (git_repo / "syncmill.toml").write_text(
        f'config_version = 2\nrepo_root = "{elsewhere.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    _, config = _target_project_config(git_repo)

    assert Path(config.repo_root).resolve() == elsewhere.resolve()


@pytest.mark.parametrize("kind", ["missing", "not-a-directory"])
def test_an_unusable_target_path_is_a_discovery_error(runner, tmp_path, kind):
    # A nonexistent or non-directory --repo makes `git rev-parse` raise OSError
    # before it can report "not a Git repository", so git_root normalizes it:
    # otherwise the CLI called a bad PATH an invalid target CONFIGURATION.
    if kind == "missing":
        target = tmp_path / "gone"
    else:
        target = tmp_path / "file.txt"
        target.write_text("x", encoding="utf-8")

    result = runner.invoke(cli, ["runs", "list", "--repo", str(target)])

    assert result.exit_code != 0
    assert "as a Git repository" in result.output
    assert "invalid target project configuration" not in result.output


@pytest.mark.parametrize("command", [["agents"], ["team", "list"], ["report", "list"]])
def test_commands_surface_malformed_cwd_toml(monkeypatch, runner, tmp_path, command):
    # Without --repo every command builds its config through _load_config, so a
    # half-edited syncmill.toml in cwd must produce the one-line CLI error there
    # too — TOMLDecodeError is a ValueError, which a ValidationError-only catch
    # let escape as a traceback.
    (tmp_path / "syncmill.toml").write_text("config_version = [broken\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    result = runner.invoke(cli, command)

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "invalid configuration" in result.output


def test_run_surfaces_invalid_target_project_state_root(
    monkeypatch, runner, git_repo
):
    (git_repo / "syncmill.toml").write_text(
        'config_version = 2\nrepo_root = "."\n'
        '[runs]\nroot = "tracked-runs"\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", "syncmill.toml")

    result = runner.invoke(cli, ["run", "do", "--repo", str(git_repo)])

    assert result.exit_code != 0
    assert "invalid target project configuration" in result.output
    assert "runs.root" in result.output


# ── team profiles (Phase 4) ──────────────────────────────────────────────────


_TEAMS_JSON = (
    '{"python-team": {"agents": ["codex", "claude"], "strategy": "compete",'
    ' "capabilities": {"codex": ["python"]}, "goals": ["auth"]}}'
)


def test_run_team_overlays_config(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", _TEAMS_JSON)
    h = _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("codex"), FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do", "--team", "python-team"])
    assert res.exit_code == 0, res.output
    cfg = h["config"]
    assert cfg.agent_priority == ["codex", "claude"]  # subset + order
    assert cfg.capabilities.agents == {"codex": ["python"]}
    assert cfg.default_strategy == "compete"
    assert "team: python-team" in res.output  # banner suffix


def test_run_team_unknown_lists_available(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", _TEAMS_JSON)
    res = runner.invoke(cli, ["run", "do", "--team", "nope"])
    assert res.exit_code != 0
    assert "unknown team" in res.output
    assert "python-team" in res.output


def test_run_team_none_configured_message(runner):
    res = runner.invoke(cli, ["run", "do", "--team", "nope"])
    assert res.exit_code != 0
    assert "(none configured)" in res.output


def test_run_team_unknown_agent_id_clean_error(monkeypatch, runner):
    # a profile naming an agent syncmill doesn't know fails BEFORE the registry's
    # raw ValueError could escape from Supervisor(config)
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"t": {"agents": ["gemini"]}}')
    res = runner.invoke(cli, ["run", "do", "--team", "t"])
    assert res.exit_code != 0
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "unknown agent(s): gemini" in res.output
    assert "claude" in res.output  # the valid set is listed


def test_run_strategy_flag_beats_team_strategy(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", _TEAMS_JSON)
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("codex")])
    res = runner.invoke(cli, ["run", "do", "--team", "python-team", "-s", "pipeline"])
    assert res.exit_code == 0
    assert h["config"].default_strategy == "pipeline"


def test_run_team_invalid_strategy_caught(monkeypatch, runner):
    # a profile strategy typo rides the existing _validate_strategy seam
    monkeypatch.setenv(
        "SYNCMILL_TEAMS", '{"t": {"agents": ["claude"], "strategy": "warp"}}'
    )
    res = runner.invoke(cli, ["run", "do", "--team", "t"])
    assert res.exit_code != 0
    assert "unknown strategy" in res.output
    assert "team profile" in res.output


def test_run_team_exclude_validated_against_team_lineup(monkeypatch, runner):
    # -x is checked against the POST-team agent_priority
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"t": {"agents": ["codex"]}}')
    res = runner.invoke(cli, ["run", "do", "--team", "t", "-x", "claude"])
    assert res.exit_code != 0
    assert "unknown agent(s): claude" in res.output
    assert "Valid: codex" in res.output


def test_run_team_timeout_touches_only_team_agents(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"t": {"agents": ["codex"]}}')
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("codex")])
    res = runner.invoke(cli, ["run", "do", "--team", "t", "-t", "99"])
    assert res.exit_code == 0
    cfg = h["config"]
    assert cfg.codex.timeout_seconds == 99
    assert cfg.claude.timeout_seconds == 300  # not in the team → untouched


def test_run_team_empty_flag_ignored(monkeypatch, runner):
    # `--team ""` falls back to no team, like -r ""/-x ""
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "--team", ""])
    assert res.exit_code == 0
    assert h["config"].agent_priority == ["claude", "codex", "kimi-code"]


def test_monitor_inner_argv_forwards_team(monkeypatch, runner):
    # --team is config selection (like --strategy): the inner re-invocation MUST
    # carry it or the monitored run would rebuild a team-less lineup
    monkeypatch.setenv(
        "SYNCMILL_TEAMS", '{"t": {"agents": ["claude"], "strategy": "compete"}}'
    )
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(cli, ["run", "task", "--team", "t", "--monitor"])
    assert res.exit_code == 0
    argv = mon["launch"]["argv"]
    assert "--team" in argv and "t" in argv
    assert argv[-2:] == ["--", "task"]


def test_board_run_team_overlays_config(board_env, monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", _TEAMS_JSON)
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="codex")])
    res = runner.invoke(cli, ["board", "run", "--team", "python-team"])
    assert res.exit_code == 0, res.output
    assert "team: python-team" in res.output
    assert h["config"].agent_priority == ["codex", "claude"]
    assert h["config"].default_strategy == "compete"


def test_board_run_goal_threads_and_resolves_team(board_env, monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", _TEAMS_JSON)
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="codex")])
    res = runner.invoke(cli, ["board", "run", "--goal", "auth"])
    assert res.exit_code == 0, res.output
    assert "team: python-team (via goal 'auth')" in res.output
    assert h["config"].agent_priority == ["codex", "claude"]
    assert h["run_item_calls"] == [
        {"item_id": None, "agent_id": None, "goal": "auth"}
    ]


def test_board_run_goal_unmapped_scopes_without_team(board_env, monkeypatch, runner):
    # a goal no team declares still scopes the claim — valid, not an error
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="claude")])
    res = runner.invoke(cli, ["board", "run", "--goal", "unmapped"])
    assert res.exit_code == 0, res.output
    assert "team:" not in res.output
    assert h["run_item_calls"][0]["goal"] == "unmapped"
    assert h["config"].agent_priority == ["claude", "codex", "kimi-code"]


def test_board_run_explicit_team_beats_goal_mapping(board_env, monkeypatch, runner):
    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        '{"python-team": {"agents": ["codex"], "goals": ["auth"]},'
        ' "other-team": {"agents": ["claude"]}}',
    )
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="claude")])
    res = runner.invoke(
        cli, ["board", "run", "--goal", "auth", "--team", "other-team"]
    )
    assert res.exit_code == 0, res.output
    assert "team: other-team" in res.output
    assert "via goal" not in res.output
    assert h["config"].agent_priority == ["claude"]
    assert h["run_item_calls"][0]["goal"] == "auth"  # goal still scopes the claim


def test_board_run_goal_and_id_mutually_exclusive(board_env, runner):
    res = runner.invoke(cli, ["board", "run", "--goal", "auth", "--id", "item-1"])
    assert res.exit_code != 0
    assert "mutually exclusive" in res.output


def test_board_run_empty_goal_ignored(board_env, monkeypatch, runner):
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="claude")])
    res = runner.invoke(cli, ["board", "run", "--goal", ""])
    assert res.exit_code == 0
    assert h["run_item_calls"][0]["goal"] is None


def test_board_run_loop_goal_suffix_in_drain_message(board_env, monkeypatch, runner):
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="claude")])
    res = runner.invoke(cli, ["board", "run", "--loop", "--goal", "auth"])
    assert res.exit_code == 0, res.output
    assert "drained 1 item(s) for goal 'auth'" in res.output
    assert all(c["goal"] == "auth" for c in h["run_item_calls"])


def test_board_run_id_peeks_item_goal_for_team(board_env, monkeypatch, runner):
    # --id alone honors the goal→team mapping via a read-only peek of the REAL
    # tmp board db (no claim taken by the peek — the fake Supervisor claims)
    iid = _add(runner, "auth work", "--goal", "auth")
    monkeypatch.setenv("SYNCMILL_TEAMS", _TEAMS_JSON)
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="codex")])
    res = runner.invoke(cli, ["board", "run", "--id", iid])
    assert res.exit_code == 0, res.output
    assert "team: python-team (via goal 'auth')" in res.output
    assert h["config"].agent_priority == ["codex", "claude"]
    assert h["run_item_calls"][0]["item_id"] == iid


def test_board_run_id_with_explicit_team_skips_peek(board_env, monkeypatch, runner):
    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        '{"python-team": {"agents": ["codex"], "goals": ["auth"]},'
        ' "other-team": {"agents": ["claude"]}}',
    )
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="claude")])
    res = runner.invoke(
        cli, ["board", "run", "--id", "item-1", "--team", "other-team"]
    )
    assert res.exit_code == 0, res.output
    assert "team: other-team" in res.output and "via goal" not in res.output
    assert h["config"].agent_priority == ["claude"]


def test_load_config_converts_validation_error(monkeypatch, runner):
    # codex design-review Major: a structurally broken profile fires at
    # SyncmillConfig() construction — every command must surface it as a clean
    # one-line error, never a raw ValidationError traceback
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"t": {"agents": []}}')
    for args in (["run", "do"], ["board", "run"], ["team", "list"], ["agents"]):
        res = runner.invoke(cli, args)
        assert res.exit_code != 0
        assert res.exception is None or isinstance(res.exception, SystemExit)
        assert "invalid configuration" in res.output


def test_invalid_memory_backend_is_clean_config_error(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_MEMORY__BACKEND", "unknown")
    res = runner.invoke(cli, ["agents"])
    assert res.exit_code != 0
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "invalid configuration" in res.output
    assert "memory.backend" in res.output


def test_team_list_renders_and_empty_notice(monkeypatch, runner):
    res = runner.invoke(cli, ["team", "list"])
    assert res.exit_code == 0
    assert "no teams configured" in res.output

    monkeypatch.setenv("SYNCMILL_TEAMS", _TEAMS_JSON)
    res = runner.invoke(cli, ["team", "list"])
    assert res.exit_code == 0
    assert "python-team" in res.output
    assert "codex → claude" in res.output
    assert "compete" in res.output
    assert "auth" in res.output


def test_team_show_renders_status_and_fields(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_CLAUDE__ENABLED", "false")
    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        '{"t": {"agents": ["codex", "claude", "gemini"],'
        ' "capabilities": {"codex": ["python"]}, "goals": ["auth"]}}',
    )
    res = runner.invoke(cli, ["team", "show", "t"])
    assert res.exit_code == 0, res.output
    assert "[enabled]" in res.output  # codex
    assert "[disabled]" in res.output  # claude (env-disabled)
    assert "[unknown agent]" in res.output  # gemini — visible, not a hard error
    assert "capabilities: python" in res.output
    assert "inherits default_strategy: route" in res.output
    assert "auth" in res.output


def test_team_show_unknown_name_errors(monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"t": {"agents": ["claude"]}}')
    res = runner.invoke(cli, ["team", "show", "nope"])
    assert res.exit_code != 0
    assert "unknown team" in res.output and "t" in res.output


# ── team save / team use / default_team (P3 TEAM pane) ──────────────────────


def _point_toml_target(monkeypatch, tmp_path, body: str | None = None) -> Path:
    """Point the config's toml source (= teamfile's write target) at a tmp file,
    optionally seeding it — the write-path counterpart of ``_point_toml``."""
    from syncmill.config import SyncmillConfig

    p = tmp_path / "syncmill.toml"
    if body is not None:
        p.write_text(body)
    monkeypatch.setitem(SyncmillConfig.model_config, "toml_file", str(p))
    return p


def test_team_save_creates_and_show_sees_it(monkeypatch, runner, tmp_path):
    p = _point_toml_target(monkeypatch, tmp_path)
    res = runner.invoke(
        cli,
        ["team", "save", "demo", "-a", "codex", "-a", "claude", "-s", "compete",
         "-g", "backend", "-c", "codex=python,cli"],
    )
    assert res.exit_code == 0, res.output
    assert f"saved team 'demo' to {p}" in res.output
    assert "[teams.demo]" in p.read_text()
    show = runner.invoke(cli, ["team", "show", "demo"])
    assert show.exit_code == 0, show.output
    assert "1. codex [enabled]  capabilities: python, cli" in show.output
    assert "strategy: compete" in show.output
    assert "goals:    backend" in show.output


def test_team_show_byte_identical_to_render_team(monkeypatch, runner, tmp_path):
    # the CLI and the dashboard TEAM pane share one renderer — prove the CLI
    # output IS render_team's text (the render_item/render_board_stats rule)
    from syncmill.config import SyncmillConfig
    from syncmill.teamfile import render_team

    _point_toml_target(
        monkeypatch, tmp_path, '[teams.demo]\nagents = ["codex"]\ngoals = ["g"]\n'
    )
    res = runner.invoke(cli, ["team", "show", "demo"])
    config = SyncmillConfig()
    assert res.output == render_team("demo", config.teams["demo"], config) + "\n"


def test_team_save_rejects_dup_goal_vs_env_team(monkeypatch, runner, tmp_path):
    # the resolved view includes SYNCMILL_TEAMS — a save conflicting with an
    # env-only team must fail (else the next _load_config dies)
    _point_toml_target(monkeypatch, tmp_path)
    monkeypatch.setenv("SYNCMILL_TEAMS", _TEAMS_JSON)  # python-team owns "auth"
    res = runner.invoke(cli, ["team", "save", "b", "-a", "claude", "-g", "auth"])
    assert res.exit_code != 0
    assert "declared by two teams" in res.output


def test_team_save_rejects_dup_goal_vs_file_team(monkeypatch, runner, tmp_path):
    _point_toml_target(
        monkeypatch, tmp_path, '[teams.a]\nagents = ["claude"]\ngoals = ["auth"]\n'
    )
    res = runner.invoke(cli, ["team", "save", "b", "-a", "codex", "-g", "auth"])
    assert res.exit_code != 0
    assert "declared by two teams" in res.output


def test_team_save_rejects_file_only_conflict_masked_by_env(
    monkeypatch, runner, tmp_path
):
    # codex design-review Major: env overrides team a's goals, so the RESOLVED
    # view has no conflict — but the file standing alone would once env is gone
    p = _point_toml_target(
        monkeypatch, tmp_path, '[teams.a]\nagents = ["claude"]\ngoals = ["auth"]\n'
    )
    monkeypatch.setenv(
        "SYNCMILL_TEAMS", '{"a": {"agents": ["claude"], "goals": ["other"]}}'
    )
    res = runner.invoke(cli, ["team", "save", "b", "-a", "codex", "-g", "auth"])
    assert res.exit_code != 0
    assert "standing alone" in res.output
    assert "[teams.b]" not in p.read_text()  # nothing written


def test_team_save_rejects_env_masked_invalid_file_team(monkeypatch, runner, tmp_path):
    # codex post-code Major repro: file has an invalid team masked by env — the
    # save must NOT exit 0 leaving the file unloadable once the env is gone
    p = _point_toml_target(monkeypatch, tmp_path, "[teams.a]\nagents = []\n")
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"a": {"agents": ["claude"]}}')
    res = runner.invoke(cli, ["team", "save", "b", "-a", "codex"])
    assert res.exit_code != 0
    assert "standing alone" in res.output
    assert "[teams.b]" not in p.read_text()  # nothing written


def test_team_save_resave_same_team_ok(monkeypatch, runner, tmp_path):
    # re-saving the goal's own team substitutes, not conflicts — and replaces
    # the table wholesale (stale strategy gone)
    p = _point_toml_target(
        monkeypatch,
        tmp_path,
        '[teams.a]\nagents = ["claude"]\nstrategy = "compete"\ngoals = ["auth"]\n',
    )
    res = runner.invoke(cli, ["team", "save", "a", "-a", "codex", "-g", "auth"])
    assert res.exit_code == 0, res.output
    text = p.read_text()
    assert "strategy" not in text  # wholesale replace cleared it


def test_team_save_unknown_agent_clean_error(monkeypatch, runner, tmp_path):
    _point_toml_target(monkeypatch, tmp_path)
    res = runner.invoke(cli, ["team", "save", "t", "-a", "gemini"])
    assert res.exit_code != 0
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "unknown agent(s): gemini" in res.output


def test_team_save_duplicate_agent_flag_rejected(monkeypatch, runner, tmp_path):
    _point_toml_target(monkeypatch, tmp_path)
    res = runner.invoke(cli, ["team", "save", "t", "-a", "codex", "-a", "codex"])
    assert res.exit_code != 0
    assert "invalid team profile" in res.output


def test_team_save_bad_capability_spec_rejected(monkeypatch, runner, tmp_path):
    _point_toml_target(monkeypatch, tmp_path)
    res = runner.invoke(cli, ["team", "save", "t", "-a", "codex", "-c", "codexpython"])
    assert res.exit_code != 0
    assert "agent=tok1,tok2" in res.output


def test_team_save_strategy_choice_gated(monkeypatch, runner, tmp_path):
    _point_toml_target(monkeypatch, tmp_path)
    res = runner.invoke(cli, ["team", "save", "t", "-a", "codex", "-s", "warp"])
    assert res.exit_code != 0  # click.Choice rejects before any file I/O


def test_team_save_env_shadow_notice(monkeypatch, runner, tmp_path):
    _point_toml_target(monkeypatch, tmp_path)
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"other": {"agents": ["claude"]}}')
    res = runner.invoke(cli, ["team", "save", "t", "-a", "codex"])
    assert res.exit_code == 0, res.output
    assert "SYNCMILL_TEAMS" in res.output and "deep-merges" in res.output


def test_team_use_sets_and_clears(monkeypatch, runner, tmp_path):
    p = _point_toml_target(
        monkeypatch, tmp_path, '[teams.demo]\nagents = ["codex"]\n'
    )
    res = runner.invoke(cli, ["team", "use", "demo"])
    assert res.exit_code == 0, res.output
    assert "default_team = 'demo'" in res.output
    assert 'default_team = "demo"' in p.read_text()

    res = runner.invoke(cli, ["team", "use", "--clear"])
    assert res.exit_code == 0, res.output
    assert "default_team" not in p.read_text()
    assert "[teams.demo]" in p.read_text()  # rest untouched


def test_team_use_unknown_name_lists_available(monkeypatch, runner, tmp_path):
    _point_toml_target(monkeypatch, tmp_path, '[teams.demo]\nagents = ["codex"]\n')
    res = runner.invoke(cli, ["team", "use", "nope"])
    assert res.exit_code != 0
    assert "unknown team" in res.output and "demo" in res.output


def test_team_use_name_and_clear_mutually_exclusive(runner):
    res = runner.invoke(cli, ["team", "use", "x", "--clear"])
    assert res.exit_code != 0
    assert "exactly one of NAME or --clear" in res.output
    res = runner.invoke(cli, ["team", "use"])
    assert res.exit_code != 0
    assert "exactly one of NAME or --clear" in res.output


def test_team_use_env_only_team_warns_dangling(monkeypatch, runner, tmp_path):
    # codex design-review Major: the name resolves via SYNCMILL_TEAMS only — the
    # persisted default dangles once that env is absent, so warn (not reject)
    p = _point_toml_target(monkeypatch, tmp_path)
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"envy": {"agents": ["claude"]}}')
    res = runner.invoke(cli, ["team", "use", "envy"])
    assert res.exit_code == 0, res.output
    assert 'default_team = "envy"' in p.read_text()
    assert "not defined in" in res.output and "team save envy" in res.output


def test_team_use_env_default_shadow_notice(monkeypatch, runner, tmp_path):
    _point_toml_target(monkeypatch, tmp_path, '[teams.demo]\nagents = ["codex"]\n')
    monkeypatch.setenv("SYNCMILL_DEFAULT_TEAM", "demo")
    res = runner.invoke(cli, ["team", "use", "demo"])
    assert res.exit_code == 0, res.output
    assert "SYNCMILL_DEFAULT_TEAM" in res.output and "overrides the file" in res.output


def test_team_use_clear_works_with_unloadable_config(monkeypatch, runner, tmp_path):
    # --clear is the FIX path — it must work even when _load_config would die
    # (extra="forbid" rejects the typo'd top-level key)
    p = _point_toml_target(
        monkeypatch, tmp_path, 'default_team = "x"\nnot_a_real_key = 1\n'
    )
    res = runner.invoke(cli, ["team", "use", "--clear"])
    assert res.exit_code == 0, res.output
    assert "default_team" not in p.read_text()
    assert "not_a_real_key" in p.read_text()  # everything else preserved


def test_run_default_team_auto_applies(monkeypatch, runner, tmp_path):
    _point_toml_target(
        monkeypatch,
        tmp_path,
        'default_team = "demo"\n\n[teams.demo]\nagents = ["codex"]\nstrategy = "compete"\n',
    )
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("codex")])
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0, res.output
    assert h["config"].agent_priority == ["codex"]
    assert h["config"].default_strategy == "compete"
    assert "team: demo (default)" in res.output


def test_run_explicit_team_beats_default(monkeypatch, runner, tmp_path):
    _point_toml_target(
        monkeypatch,
        tmp_path,
        'default_team = "a"\n\n[teams.a]\nagents = ["claude"]\n\n'
        '[teams.b]\nagents = ["codex"]\n',
    )
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("codex")])
    res = runner.invoke(cli, ["run", "do", "--team", "b"])
    assert res.exit_code == 0, res.output
    assert h["config"].agent_priority == ["codex"]
    assert "team: b" in res.output
    assert "(default)" not in res.output


def test_run_strategy_flag_beats_default_team_strategy(monkeypatch, runner, tmp_path):
    _point_toml_target(
        monkeypatch,
        tmp_path,
        'default_team = "demo"\n\n[teams.demo]\nagents = ["codex"]\nstrategy = "compete"\n',
    )
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("codex")])
    res = runner.invoke(cli, ["run", "do", "-s", "pipeline"])
    assert res.exit_code == 0, res.output
    assert h["config"].default_strategy == "pipeline"


def test_run_dangling_default_team_clean_error(monkeypatch, runner, tmp_path):
    _point_toml_target(monkeypatch, tmp_path, 'default_team = "ghost"\n')
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code != 0
    assert res.exception is None or isinstance(res.exception, SystemExit)
    assert "default_team 'ghost'" in res.output
    assert "team use --clear" in res.output


def test_run_empty_default_team_treated_unset(monkeypatch, runner, tmp_path):
    # default_team = "" is unset (the `-r ""` house rule), not a dangling error
    _point_toml_target(monkeypatch, tmp_path, 'default_team = ""\n')
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0, res.output
    assert h["config"].agent_priority == ["claude", "codex", "kimi-code"]


def test_monitor_inner_argv_forwards_default_team(monkeypatch, runner, tmp_path):
    # the default resolves into the SAME local `team` before the monitor launch,
    # so the inner argv carries an explicit --team — no _inner_argv change
    _point_toml_target(
        monkeypatch,
        tmp_path,
        'default_team = "demo"\n\n[teams.demo]\nagents = ["claude"]\nstrategy = "compete"\n',
    )
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(cli, ["run", "task", "--monitor"])
    assert res.exit_code == 0, res.output
    argv = mon["launch"]["argv"]
    assert "--team" in argv and "demo" in argv


def test_board_run_default_team_applies(board_env, monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"demo": {"agents": ["codex"]}}')
    monkeypatch.setenv("SYNCMILL_DEFAULT_TEAM", "demo")
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="codex")])
    res = runner.invoke(cli, ["board", "run"])
    assert res.exit_code == 0, res.output
    assert "team: demo (default)" in res.output
    assert h["config"].agent_priority == ["codex"]


def test_board_run_goal_mapping_beats_default_team(board_env, monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"python-team": {"agents": ["codex", "claude"], "goals": ["auth"]}, "other": {"agents": ["claude"]}}')
    monkeypatch.setenv("SYNCMILL_DEFAULT_TEAM", "other")
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="codex")])
    res = runner.invoke(cli, ["board", "run", "--goal", "auth"])
    assert res.exit_code == 0, res.output
    assert "team: python-team (via goal 'auth')" in res.output
    assert h["config"].agent_priority == ["codex", "claude"]


def test_board_run_id_peek_beats_default_team(board_env, monkeypatch, runner):
    iid = _add(runner, "auth work", "--goal", "auth")
    monkeypatch.setenv("SYNCMILL_TEAMS", '{"python-team": {"agents": ["codex", "claude"], "goals": ["auth"]}, "other": {"agents": ["claude"]}}')
    monkeypatch.setenv("SYNCMILL_DEFAULT_TEAM", "other")
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="codex")])
    res = runner.invoke(cli, ["board", "run", "--id", iid])
    assert res.exit_code == 0, res.output
    assert "team: python-team (via goal 'auth')" in res.output
    assert h["config"].agent_priority == ["codex", "claude"]


def test_board_run_explicit_team_beats_default(board_env, monkeypatch, runner):
    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        '{"a": {"agents": ["claude"]}, "b": {"agents": ["codex"]}}',
    )
    monkeypatch.setenv("SYNCMILL_DEFAULT_TEAM", "a")
    h = _install_board_supervisor(monkeypatch, results=[_result(agent_id="codex")])
    res = runner.invoke(cli, ["board", "run", "--team", "b"])
    assert res.exit_code == 0, res.output
    assert "team: b" in res.output and "(default)" not in res.output
    assert h["config"].agent_priority == ["codex"]


def test_board_run_dangling_default_team_clean_error(board_env, monkeypatch, runner):
    monkeypatch.setenv("SYNCMILL_DEFAULT_TEAM", "ghost")
    _install_board_supervisor(monkeypatch, results=[_result()])
    res = runner.invoke(cli, ["board", "run"])
    assert res.exit_code != 0
    assert "default_team 'ghost'" in res.output


# ── report group + report-path line (S3 envelope writer) ─────────────────────


def _seed_envelope(runs_dir, run_id, created_at, *, status="completed", agent="claude"):
    import json

    d = runs_dir / run_id
    d.mkdir(parents=True)
    (d / "envelope.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "syncmill.run-envelope",
                "run_id": run_id,
                "created_at": created_at,
                "finished_at": created_at,
                "strategy": "route",
                "team": None,
                "task_id": "t",
                "agents": [agent],
                "board": None,
                "preflight": {"status": "disabled"},
                "trace": {"status": "disabled"},
                "result": {"status": status, "agent_id": agent},
                "degraded": [],
            }
        )
    )


def test_report_list_empty_notice(monkeypatch, runner, tmp_path):
    monkeypatch.setenv(
        "SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(tmp_path / "runs")
    )
    res = runner.invoke(cli, ["report", "list"])
    assert res.exit_code == 0
    assert "no run reports found" in res.output


def test_report_list_newest_first(monkeypatch, runner, tmp_path):
    runs = tmp_path / "runs"
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(runs))
    _seed_envelope(runs, "run-old", "2026-07-10T00:00:00+00:00")
    _seed_envelope(runs, "run-new", "2026-07-11T00:00:00+00:00", agent="codex")
    res = runner.invoke(cli, ["report", "list"])
    assert res.exit_code == 0
    assert res.output.index("run-new") < res.output.index("run-old")
    assert "codex" in res.output


def test_report_show_renders_envelope(monkeypatch, runner, tmp_path):
    runs = tmp_path / "runs"
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(runs))
    _seed_envelope(runs, "run-1", "2026-07-11T00:00:00+00:00")
    res = runner.invoke(cli, ["report", "show", "run-1"])
    assert res.exit_code == 0
    doc = load_envelope(runs, "run-1")
    assert doc is not None
    assert res.output == render_report(doc) + "\n"


def test_report_show_unknown_id_errors(monkeypatch, runner, tmp_path):
    monkeypatch.setenv(
        "SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(tmp_path / "runs")
    )
    res = runner.invoke(cli, ["report", "show", "ghost"])
    assert res.exit_code != 0
    assert "no report found" in res.output


def test_report_group_readonly_works_while_disabled(monkeypatch, runner, tmp_path):
    # read-only viewers over whatever exists — no enabled gate (team list posture)
    runs = tmp_path / "runs"
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(runs))
    monkeypatch.delenv("SYNCMILL_INTEGRATION__REPORT__ENABLED", raising=False)
    _seed_envelope(runs, "run-1", "2026-07-11T00:00:00+00:00")
    assert runner.invoke(cli, ["report", "list"]).exit_code == 0
    assert runner.invoke(cli, ["report", "show", "run-1"]).exit_code == 0


def test_run_prints_report_line_when_reporter_wrote(monkeypatch, runner, tmp_path):
    path = tmp_path / "runs" / "rid" / "envelope.json"
    _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")], report_path=path
    )
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert f"[syncmill] report: {path}" in res.output


def test_run_no_report_line_by_default(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert "[syncmill] report:" not in res.output


def test_run_surfaces_preflight_warnings(monkeypatch, runner):
    _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")],
        preflight_warnings=["3 candidate tool(s) rejected by policy"],
    )
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert "[syncmill] preflight: 3 candidate tool(s) rejected by policy" in res.output


def test_run_no_preflight_line_by_default(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do"])
    assert "[syncmill] preflight:" not in res.output


def test_monitor_outer_passes_report_runs_dir_when_enabled(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", "/tmp/runs")
    res = runner.invoke(cli, ["run", "do", "-s", "compete", "--monitor"])
    assert res.exit_code == 0
    assert mon["launch"]["report_runs_dir"] == "/tmp/runs"


def test_monitor_outer_omits_report_runs_dir_when_disabled(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(cli, ["run", "do", "-s", "compete", "--monitor"])
    assert res.exit_code == 0
    assert mon["launch"]["report_runs_dir"] is None


# ── run --difficulty (model routing, PR 1) ──────────────────────────────────


def test_run_difficulty_flows_to_supervisor(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do", "--difficulty", "low"])
    assert res.exit_code == 0
    assert h["run"]["difficulty"] == "low"


def test_run_no_difficulty_passes_none(monkeypatch, runner):
    h = _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert h["run"]["difficulty"] is None


def test_run_difficulty_rejects_unknown_tier(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "--difficulty", "ultra"])
    assert res.exit_code != 0


def _decision(tier="low", source="explicit", applied=True, mode="off", selectors=None):
    from syncmill.model_routing import ModelRoutingDecision

    sel = selectors if selectors is not None else {"claude": "cheap"}
    return ModelRoutingDecision(
        schema_version=1,
        requested=tier if source == "explicit" else "auto",
        resolved_tier=tier,
        source=source,
        classifier_version=None if source == "explicit" else "prompt-complexity-v1",
        config_mode=mode,
        applied=applied,
        reason_codes=("explicit_override",) if source == "explicit" else ("default_medium",),
        recommended_selectors=sel,
        effective_selectors=sel if applied else {k: None for k in sel},
    )


def test_run_banner_rendered_when_routing_used(monkeypatch, runner):
    _install_supervisor(
        monkeypatch,
        result=_result(),
        agents=[FakeAgent("claude")],
        routing_decision=_decision(),
    )
    res = runner.invoke(cli, ["run", "do", "--difficulty", "low"])
    assert res.exit_code == 0
    assert "[syncmill] model routing: low / explicit / applied" in res.output


def test_run_no_banner_when_routing_off(monkeypatch, runner):
    _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")]
    )
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert "model routing" not in res.output


def test_run_routing_warning_surfaced_yellow(monkeypatch, runner):
    _install_supervisor(
        monkeypatch,
        result=_result(),
        agents=[FakeAgent("claude")],
        routing_warning="model routing classifier failed (boom); continuing",
    )
    res = runner.invoke(cli, ["run", "do"])
    assert res.exit_code == 0
    assert "classifier failed" in res.output


def test_monitor_inner_argv_forwards_difficulty(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(
        cli, ["run", "task", "-s", "compete", "--monitor", "--difficulty", "high"]
    )
    assert res.exit_code == 0
    argv = mon["launch"]["argv"]
    assert "--difficulty" in argv
    assert argv[argv.index("--difficulty") + 1] == "high"


def test_monitor_inner_argv_omits_unset_difficulty(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    mon = _patch_monitor_seams(monkeypatch)
    res = runner.invoke(cli, ["run", "task", "-s", "compete", "--monitor"])
    assert res.exit_code == 0
    assert "--difficulty" not in mon["launch"]["argv"]


# ── run --dry-run × model routing ───────────────────────────────────────────


def _patch_dry_run_seams(monkeypatch, tmp_path):
    snapshot = types.SimpleNamespace(
        source_root=tmp_path / "repo", run_root=tmp_path / "snap"
    )
    monkeypatch.setattr(
        "syncmill.cli.capture_repository_snapshot", lambda *a, **k: snapshot
    )
    monkeypatch.setattr("syncmill.cli.subprocess_status_dirty", lambda p: False)


def test_dry_run_off_mode_has_no_routing_block(monkeypatch, runner, tmp_path):
    _patch_dry_run_seams(monkeypatch, tmp_path)
    res = runner.invoke(cli, ["run", "do", "--dry-run"])
    assert res.exit_code == 0, res.output
    start = res.output.index("{")
    plan = json.loads(res.output[start : res.output.index("\nrepository:")])
    assert "model_routing" not in plan
    assert "No agent was started" in res.output


def test_dry_run_shows_canonical_decision(monkeypatch, runner, tmp_path):
    _patch_dry_run_seams(monkeypatch, tmp_path)
    monkeypatch.setenv("SYNCMILL_MODEL_ROUTING__MODE", "observe")
    monkeypatch.setenv(
        "SYNCMILL_MODEL_ROUTING__MODELS", '{"low": {"claude": "cheap"}}'
    )
    res = runner.invoke(cli, ["run", "show status", "--dry-run"])
    assert res.exit_code == 0, res.output
    plan = json.loads(res.output[: res.output.index("\nrepository:")])
    block = plan["model_routing"]
    assert block["resolved_tier"] == "low"
    assert block["source"] == "auto"
    assert block["applied"] is False
    assert block["recommended_selectors"]["claude"] == "cheap"
    assert "No agent was started" in res.output


def test_dry_run_explicit_all_unmapped_fails_like_the_real_run(
    monkeypatch, runner, tmp_path
):
    # Dry-run must reach the SAME verdict the real run would: an explicit tier
    # with no mapping anywhere is a hard error, not a silently blockless plan.
    _patch_dry_run_seams(monkeypatch, tmp_path)
    res = runner.invoke(cli, ["run", "do", "--dry-run", "--difficulty", "low"])
    assert res.exit_code != 0
    assert "no eligible agent has a mapping" in res.output


def test_dry_run_classifier_crash_degrades(monkeypatch, runner, tmp_path):
    from syncmill.model_routing import ClassifierError

    _patch_dry_run_seams(monkeypatch, tmp_path)
    monkeypatch.setenv("SYNCMILL_MODEL_ROUTING__MODE", "observe")

    def boom(*a, **k):
        raise ClassifierError("boom")

    monkeypatch.setattr("syncmill.cli.build_decision", boom)
    res = runner.invoke(cli, ["run", "do", "--dry-run"])
    assert res.exit_code == 0, res.output
    start = res.output.index("{")
    plan = json.loads(res.output[start : res.output.index("\nrepository:")])
    assert "model_routing" not in plan


def test_dry_run_rejects_disabled_forced_agent(monkeypatch, runner, tmp_path):
    # Parity with the real Supervisor: a disabled/unknown forced agent is a
    # rejected run — dry-run must not preview a plan (or decision) for it.
    _patch_dry_run_seams(monkeypatch, tmp_path)
    monkeypatch.setenv("SYNCMILL_CLAUDE__ENABLED", "false")
    res = runner.invoke(cli, ["run", "do", "--dry-run", "--agent", "claude"])
    assert res.exit_code != 0
    assert "not found or not enabled" in res.output
    res = runner.invoke(cli, ["run", "do", "--dry-run", "--agent", "bogus"])
    assert res.exit_code != 0
    assert "not found or not enabled" in res.output


def test_dry_run_rejects_empty_lineup(monkeypatch, runner, tmp_path):
    _patch_dry_run_seams(monkeypatch, tmp_path)
    monkeypatch.setenv("SYNCMILL_CLAUDE__ENABLED", "false")
    monkeypatch.setenv("SYNCMILL_CODEX__ENABLED", "false")
    monkeypatch.setenv("SYNCMILL_KIMI_CODE__ENABLED", "false")
    res = runner.invoke(cli, ["run", "do", "--dry-run", "--difficulty", "low"])
    assert res.exit_code != 0
    assert "No agents available" in res.output


def test_dry_run_capability_hint_reorders_lineup(monkeypatch, runner, tmp_path):
    # Parity with the real run's _matched_order: a -c hint must reorder the
    # previewed plan (and the routing decision's projection) the same way.
    _patch_dry_run_seams(monkeypatch, tmp_path)
    monkeypatch.setenv(
        "SYNCMILL_CAPABILITIES__AGENTS", '{"kimi-code": ["rust"]}'
    )
    res = runner.invoke(cli, ["run", "port it", "--dry-run", "-c", "rust"])
    assert res.exit_code == 0, res.output
    start = res.output.index("{")
    plan = json.loads(res.output[start : res.output.index("\nrepository:")])
    assert plan["agents"][0] == "kimi-code"  # matched agent leads
    assert sorted(plan["agents"]) == ["claude", "codex", "kimi-code"]  # permutation


# ── room round session flags (--fresh-session / --fork) ─────────────────────


def test_detached_room_round_forwards_session_flags(monkeypatch, runner, tmp_path):
    captured: dict = {}

    class Coordinator:
        class store:  # noqa: N801 - minimal duck-typed namespace
            @staticmethod
            def get_room(room_id):
                return {"members": [{"member_id": "claude", "provider_id": "claude"}]}

            @staticmethod
            def record_event(room_id, kind, payload):
                pass

        def preflight_round(self, room_id, prompt, **kwargs):
            return {}

        def close(self):
            pass

    def launch(root, room_id, prompt, **kwargs):
        captured.update(kwargs)
        return 4242

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    monkeypatch.setattr("syncmill.cli.launch_room_worker", launch)
    monkeypatch.setattr("syncmill.cli.git_root", lambda path: tmp_path)

    res = runner.invoke(
        cli,
        ["room", "round", "room-1", "--detach", "--fresh-session", "--fork"],
    )
    assert res.exit_code == 0, res.output
    assert captured["fresh_session"] is True
    assert captured["fork"] is True


def test_room_round_fork_rejects_non_claude_members(monkeypatch, runner, tmp_path):
    class Coordinator:
        class store:  # noqa: N801
            @staticmethod
            def get_room(room_id):
                return {"members": [
                    {"member_id": "claude", "provider_id": "claude"},
                    {"member_id": "codex", "provider_id": "codex"},
                ]}

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    res = runner.invoke(cli, ["room", "round", "room-1", "--fork"])
    assert res.exit_code != 0
    assert "--fork is supported only for claude members" in res.output
    assert "codex" in res.output


def test_room_worker_threads_session_flags(monkeypatch, runner, tmp_path):
    captured: dict = {}

    class Coordinator:
        def run_round(self, room_id, prompt, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    res = runner.invoke(
        cli,
        ["room-worker", "--fresh-session", "--fork", "--", "room-1"],
    )
    assert res.exit_code == 0, res.output
    assert captured["fresh_session"] is True
    assert captured["fork"] is True


def test_detached_room_round_preflights_session_flags(monkeypatch, runner, tmp_path):
    # The detached parent must report a doomed fork synchronously, so the
    # session flags ride into preflight_round (which validates them) rather
    # than surfacing only inside the worker.
    captured: dict = {}

    class Coordinator:
        class store:  # noqa: N801
            @staticmethod
            def get_room(room_id):
                return {"members": [{"member_id": "claude", "provider_id": "claude"}]}

            @staticmethod
            def record_event(room_id, kind, payload):
                pass

        def preflight_round(self, room_id, prompt, **kwargs):
            captured.update(kwargs)
            return {}

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    monkeypatch.setattr("syncmill.cli.launch_room_worker", lambda *a, **k: 4242)
    monkeypatch.setattr("syncmill.cli.git_root", lambda path: tmp_path)

    res = runner.invoke(
        cli, ["room", "round", "room-1", "--detach", "--fork"]
    )
    assert res.exit_code == 0, res.output
    assert captured["fork"] is True
    assert captured["fresh_session"] is False


def test_room_round_usage_render_degrades_per_candidate(monkeypatch, runner, tmp_path):
    # Post-commit usage display must neither load full artifacts nor fail the
    # command a corrupt HISTORICAL candidate cannot invalidate — the round
    # already advanced; usage is best-effort per candidate.
    class Coordinator:
        class store:  # noqa: N801
            @staticmethod
            def list_candidates(room_id):
                return [
                    {"id": "cand-1", "member_id": "claude", "state": "completed",
                     "eligible": 1},
                    {"id": "cand-2", "member_id": "codex", "state": "completed",
                     "eligible": 1},
                ]

            @staticmethod
            def load_candidate(candidate_id):
                raise AssertionError("usage display must not load full artifacts")

            @staticmethod
            def load_candidate_manifest(candidate_id):
                if candidate_id == "cand-1":
                    raise RoomError("corrupt candidate artifact")
                return {"usage": {"total_input_tokens": 40_100,
                                  "cache_read_tokens": 34_200}}

        def run_round(self, room_id, prompt, **kwargs):
            return {"state": "awaiting_decision"}

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    res = runner.invoke(cli, ["room", "round", "room-1"])
    assert res.exit_code == 0, res.output
    lines = {line.split()[0]: line for line in res.output.splitlines()
             if line.startswith("cand-")}
    assert "cache" not in lines["cand-1"]  # corrupt → usage omitted, row kept
    assert "cache 85%" in lines["cand-2"]


def test_token_usage_from_mapping_drops_malformed_fields_individually():
    from syncmill.models import TokenUsage

    assert TokenUsage.from_mapping(None) is None
    assert TokenUsage.from_mapping("nope") is None
    usage = TokenUsage.from_mapping(
        {
            "total_input_tokens": -5,          # negative → dropped
            "output_tokens": True,             # bool → dropped
            "cache_read_tokens": 10,           # valid on its own...
            "cache_creation_tokens": 3,
            "total_cost_usd": float("nan"),    # non-finite → dropped
        }
    )
    assert usage.total_input_tokens is None
    assert usage.output_tokens is None
    assert usage.cache_creation_tokens == 3
    assert usage.total_cost_usd is None
    # cache > total input is inconsistent telemetry — the ratio would render
    # as >100%; the cache field is dropped, the total kept.
    usage = TokenUsage.from_mapping(
        {"total_input_tokens": 100, "cache_read_tokens": 1000}
    )
    assert usage.total_input_tokens == 100
    assert usage.cache_read_tokens is None
    assert usage.cache_hit_ratio is None
    negative_cost = TokenUsage.from_mapping({"total_cost_usd": -0.4})
    assert negative_cost.total_cost_usd is None


def test_usage_compact_omits_overflowing_ints():
    # TokenUsage does no runtime validation: an absurd int from a corrupt
    # manifest overflows float division inside format_compact — omit, not die.
    from syncmill.cli import _usage_compact_from_manifest

    assert _usage_compact_from_manifest(
        {"usage": {"total_input_tokens": 10**400, "cache_read_tokens": 10**399}}
    ) is None


def test_room_candidates_shows_cache_column_when_usage_present(
    monkeypatch, runner, tmp_path
):
    manifest = {
        "gate": None,
        "patch_bytes": 12,
        "usage": {
            "total_input_tokens": 40_100,
            "output_tokens": 2_300,
            "cache_read_tokens": 34_200,
            "cache_creation_tokens": None,
            "total_cost_usd": None,
        },
    }

    class Coordinator:
        class store:  # noqa: N801
            @staticmethod
            def get_room(room_id):
                return {"id": room_id}

            @staticmethod
            def list_candidates(room_id):
                return [
                    {"id": "cand-1", "member_id": "claude", "state": "completed",
                     "eligible": 1},
                    {"id": "cand-2", "member_id": "codex", "state": "completed",
                     "eligible": 1},
                ]

            @staticmethod
            def load_candidate(candidate_id):
                if candidate_id == "cand-1":
                    return dict(manifest)
                return {"gate": None, "patch_bytes": 3, "usage": None}

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    res = runner.invoke(cli, ["room", "candidates", "room-1"])
    assert res.exit_code == 0, res.output
    lines = {line.split()[0]: line for line in res.output.splitlines() if line}
    assert "cache 85% (34.2k/40.1k)" in lines["cand-1"]
    assert "cache" not in lines["cand-2"]  # absent usage → column omitted


# ── room prompt UX: --prompt-file / echo / rounds ──────────────────────────


def _prompt_ux_coordinator(monkeypatch, tmp_path, *, rounds=None, room_prompt="create prompt"):
    captured: dict = {}

    class Coordinator:
        class store:  # noqa: N801
            @staticmethod
            def get_room(room_id):
                return {"id": room_id, "prompt": room_prompt,
                        "members": [{"member_id": "claude", "provider_id": "claude"}]}

            @staticmethod
            def list_rounds(room_id):
                return rounds or []

            @staticmethod
            def list_candidates(room_id):
                return []

            @staticmethod
            def record_event(room_id, kind, payload):
                pass

        def create(self, prompt, agents=None):
            captured["create_prompt"] = prompt
            return {"id": "room-1", "members": [{"member_id": "claude"}]}

        def preflight_round(self, room_id, prompt, **kwargs):
            return {}

        def run_round(self, room_id, prompt, **kwargs):
            captured["round_prompt"] = prompt
            captured["round_kwargs"] = kwargs
            return {"id": room_id, "state": "awaiting_decision"}

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), Coordinator()),
    )
    return captured


def test_room_create_prompt_file_and_stdin(monkeypatch, runner, tmp_path):
    captured = _prompt_ux_coordinator(monkeypatch, tmp_path)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("line one\nline two\n", encoding="utf-8")

    res = runner.invoke(cli, ["room", "create", "--prompt-file", str(prompt_path)])
    assert res.exit_code == 0, res.output
    assert captured["create_prompt"] == "line one\nline two\n"

    res = runner.invoke(
        cli, ["room", "create", "--prompt-file", "-"], input="from stdin\nsecond\n"
    )
    assert res.exit_code == 0, res.output
    assert captured["create_prompt"] == "from stdin\nsecond\n"


def test_prompt_file_validation_errors(monkeypatch, runner, tmp_path):
    _prompt_ux_coordinator(monkeypatch, tmp_path)
    prompt_path = tmp_path / "p.md"
    prompt_path.write_text("x", encoding="utf-8")

    res = runner.invoke(
        cli, ["room", "create", "positional", "--prompt-file", str(prompt_path)]
    )
    assert res.exit_code != 0 and "mutually exclusive" in res.output

    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    res = runner.invoke(cli, ["room", "create", "--prompt-file", str(empty)])
    assert res.exit_code != 0 and "empty" in res.output

    big = tmp_path / "big.md"
    big.write_bytes(b"y" * (96 * 1024 + 1))
    res = runner.invoke(cli, ["room", "round", "room-1", "--prompt-file", str(big)])
    assert res.exit_code != 0 and "byte limit" in res.output

    res = runner.invoke(cli, ["room", "create"])
    assert res.exit_code != 0 and "prompt is required" in res.output

    # A NUL byte survives a strict UTF-8 decode but can never ride argv —
    # subprocess would raise "embedded null byte" mid-round (freezing a room
    # as recovery_required before any agent runs). Reject at resolution.
    nul = tmp_path / "nul.md"
    nul.write_bytes(b"do the task\x00trailing")
    res = runner.invoke(cli, ["room", "create", "--prompt-file", str(nul)])
    assert res.exit_code != 0 and "NUL byte (offset 11)" in res.output


def test_resolve_prompt_file_uses_platform_argv_accounting(monkeypatch, tmp_path):
    # `run --prompt-file` never passes through RoomCoordinator's validation,
    # so the resolver itself must use the platform accounting: on Windows a
    # quote-heavy file that fits the byte cap doubles under list2cmdline
    # quoting and fails at provider launch.
    import click

    import syncmill.rooms as rooms_mod
    from syncmill.cli import _resolve_prompt

    monkeypatch.setattr(
        rooms_mod, "_ROOM_PROMPT_MAX_BYTES", rooms_mod._prompt_max_bytes("nt")
    )
    monkeypatch.setattr(rooms_mod.os, "name", "nt")
    path = tmp_path / "quoteful.md"
    path.write_text('"' * 20_000, encoding="utf-8")
    with pytest.raises(click.ClickException, match="byte limit"):
        _resolve_prompt(None, path, required=True)
    plain = tmp_path / "plain.md"
    plain.write_text("x" * 20_000, encoding="utf-8")
    assert _resolve_prompt(None, plain, required=True) == "x" * 20_000


def test_first_line_escapes_terminal_control_sequences():
    # A stored prompt is untrusted display data: echoing an OSC 52 / CSI
    # sequence verbatim through `room round`/`room rounds` could rewrite the
    # operator's clipboard, title, or displayed lines.
    from syncmill.cli import _first_line

    line = _first_line("evil \x1b]52;c;cGF5bG9hZA==\x07 title \x9b31m")
    assert "\x1b" not in line and "\x07" not in line and "\x9b" not in line
    assert "\\x1b" in line and "\\x07" in line and "\\x9b" in line
    # Plain text (incl. tabs) passes through untouched.
    assert _first_line("do the\tthing\nsecond") == "do the\tthing"


def test_room_round_echoes_prompt_and_warns_on_fallback(monkeypatch, runner, tmp_path):
    captured = _prompt_ux_coordinator(monkeypatch, tmp_path)

    res = runner.invoke(cli, ["room", "round", "room-1", "do the next step\nmore"])
    assert res.exit_code == 0, res.output
    assert "Round prompt: do the next step" in res.output
    assert "reusing the room creation prompt" not in res.output

    res = runner.invoke(cli, ["room", "round", "room-1"])
    assert res.exit_code == 0, res.output
    assert "reusing the room creation prompt" in res.output
    assert "Round prompt: create prompt" in res.output
    assert captured["round_prompt"] is None  # fallback resolves inside run_round


def test_room_round_prompt_file_reaches_run_round(monkeypatch, runner, tmp_path):
    captured = _prompt_ux_coordinator(monkeypatch, tmp_path)
    prompt_path = tmp_path / "round.md"
    prompt_path.write_text("multi\nline\nround\n", encoding="utf-8")
    res = runner.invoke(
        cli, ["room", "round", "room-1", "--prompt-file", str(prompt_path)]
    )
    assert res.exit_code == 0, res.output
    assert captured["round_prompt"] == "multi\nline\nround\n"


def test_room_rounds_lists_prompts(monkeypatch, runner, tmp_path):
    rounds = [
        {"number": 1, "state": "completed", "created_at": "2026-08-11T01:00:00Z",
         "prompt": "first round prompt\nwith detail"},
        {"number": 2, "state": "running", "created_at": "2026-08-11T02:00:00Z",
         "prompt": "second " + "x" * 200},
    ]
    _prompt_ux_coordinator(monkeypatch, tmp_path, rounds=rounds)
    res = runner.invoke(cli, ["room", "rounds", "room-1"])
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert "first round prompt" in lines[0] and "with detail" not in lines[0]
    assert lines[1].rstrip().endswith("…")  # long prompt trimmed with ellipsis


def test_run_prompt_file_resolves_before_dispatch(monkeypatch, runner, tmp_path):
    captured: dict = {}

    def fake_execute_run(config, prompt, **kwargs):
        captured["prompt"] = prompt
        raise SystemExit(0)

    # Intercept as early as safely possible: the resolved prompt reaches the
    # normal pipeline as the positional prompt string.
    monkeypatch.setattr("syncmill.cli._load_run_config", fake_execute_run)
    prompt_path = tmp_path / "task.md"
    prompt_path.write_text("do things\nover lines\n", encoding="utf-8")

    res = runner.invoke(cli, ["run", "positional", "--prompt-file", str(prompt_path)])
    assert res.exit_code != 0 and "mutually exclusive" in res.output

    res = runner.invoke(cli, ["run"])
    assert res.exit_code != 0 and "prompt is required" in res.output


def test_blank_positional_round_prompt_takes_the_warned_fallback(
    monkeypatch, runner, tmp_path
):
    # codex review MAJOR: `room round <id> ""` used to bypass the warning and
    # then hit rooms' silent `prompt or room["prompt"]` fallback.
    captured = _prompt_ux_coordinator(monkeypatch, tmp_path)
    res = runner.invoke(cli, ["room", "round", "room-1", "   "])
    assert res.exit_code == 0, res.output
    assert "reusing the room creation prompt" in res.output
    assert captured["round_prompt"] is None


def test_blank_create_prompt_is_rejected(monkeypatch, runner, tmp_path):
    _prompt_ux_coordinator(monkeypatch, tmp_path)
    res = runner.invoke(cli, ["room", "create", "  \n "])
    assert res.exit_code != 0 and "empty" in res.output


def test_prompt_file_rejects_invalid_utf8_cleanly(monkeypatch, runner, tmp_path):
    # codex review MAJOR: surrogateescape produced lone surrogates that SQLite
    # refuses to persist, surfacing as an uncaught UnicodeEncodeError mid-round.
    _prompt_ux_coordinator(monkeypatch, tmp_path)
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"valid start \xff\xfe then junk")
    res = runner.invoke(cli, ["room", "create", "--prompt-file", str(bad)])
    assert res.exit_code != 0
    assert "not valid UTF-8" in res.output


def test_prompt_file_size_check_does_not_buffer_the_whole_file(
    monkeypatch, runner, tmp_path
):
    # codex review MAJOR: the cap must be enforced from a BOUNDED read, so an
    # enormous file can never be slurped into memory first.
    _prompt_ux_coordinator(monkeypatch, tmp_path)
    big = tmp_path / "big.md"
    big.write_bytes(b"z" * (96 * 1024 + 4096))
    reads: list[int | None] = []
    real_open = Path.open

    def spy_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        real_read = handle.read

        def read(size=-1):
            reads.append(size)
            return real_read(size)

        handle.read = read
        return handle

    monkeypatch.setattr(Path, "open", spy_open)
    res = runner.invoke(cli, ["room", "create", "--prompt-file", str(big)])
    assert res.exit_code != 0 and "byte limit" in res.output
    # bounded: cap+1, never an unbounded read(-1)
    assert reads and all(size == 96 * 1024 + 1 for size in reads)


def _workflow_runs_root(tmp_path, git_repo):
    from syncmill.config import SyncmillConfig

    config = SyncmillConfig(
        repo_root=str(git_repo), runs={"root": str(tmp_path / "runs")}
    )
    return config, project_id(git_repo, config.project.id)


def _plant_workflow(tmp_path, git_repo, state, *, finalized=None):
    from syncmill.workflow import WorkflowSpec, WorkflowStore

    config, project = _workflow_runs_root(tmp_path, git_repo)
    store = WorkflowStore(Path(config.runs.root), project)
    record = store.create(
        spec=WorkflowSpec.model_validate(
            {
                "goal": "g",
                "nodes": [{"id": "final", "agent": "claude", "prompt": "f"}],
                "final_node": "final",
            }
        ),
        repo_root=git_repo,
        source_fingerprint="a" * 64,
        planner_agent="claude",
        planner_output="{}",
    )
    # `finalized` is the run this workflow recorded as its final one. "self"
    # means the run its own id implies, which is what a real completion writes.
    final_run_id = (
        f"{record.workflow_id}-final" if finalized == "self" else finalized
    )
    store.mutate(
        record.workflow_id,
        lambda manifest, _s: (manifest, state),
        final_run_id=final_run_id,
    )
    return config, record.workflow_id


def _workflow_manifest(workflow_id, *, approved=False, run_id=None):
    run_id = run_id or f"{workflow_id}-final"
    plan = {"origin": "workflow", "workflow_id": workflow_id}
    if approved:
        # The real shape: an approval names what it approved, so it cannot be
        # pasted onto another run or survive its patch being replaced.
        plan["approved"] = {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "patch_sha256": "d" * 64,
        }
    return {"run_id": run_id, "patch_sha256": "d" * 64, "plan": plan}


def _artifact_store(config, git_repo):
    from syncmill.artifacts import ArtifactStore

    return ArtifactStore(
        Path(config.runs.root), project_id(git_repo, config.project.id)
    )


def test_apply_heals_the_approval_of_a_workflow_that_did_commit(tmp_path, git_repo):
    # The producer can die between the fenced completion write and the stamp on
    # its artifact. The workflow row settles it, and the artifact learns.
    from syncmill.cli import _heal_workflow_approval

    config, workflow_id = _plant_workflow(
        tmp_path, git_repo, "completed", finalized="self"
    )
    store = _artifact_store(config, git_repo)
    run_id = f"{workflow_id}-final"
    task = Task(prompt="w")
    store.begin(
        run_id=run_id,
        task=task,
        repo_root=git_repo,
        snapshot_commit="head",
        source_fingerprint="fp",
        strategy="route",
        agents=["claude"],
        plan={"origin": "workflow", "workflow_id": workflow_id},
    )
    store.save(
        run_id=run_id,
        task=task,
        result=_result(patch="diff --git a/x b/x\n"),
        repo_root=git_repo,
        snapshot_commit="head",
        source_fingerprint="fp",
        strategy="route",
        plan={"origin": "workflow", "workflow_id": workflow_id},
    )

    _heal_workflow_approval(
        config, git_repo, run_id, _workflow_manifest(workflow_id), store
    )
    from syncmill.artifacts import workflow_approved

    assert workflow_approved(store.load(run_id).manifest)


def test_apply_refuses_a_final_artifact_whose_workflow_did_not_complete(
    tmp_path, git_repo
):
    # The artifact is published BEFORE the fenced commit, so a cancel in that
    # window leaves an applyable artifact for a workflow that never completed.
    from syncmill.cli import _heal_workflow_approval

    config, workflow_id = _plant_workflow(tmp_path, git_repo, "cancelled")
    store = _artifact_store(config, git_repo)
    with pytest.raises(click.ClickException, match="not approved for apply"):
        _heal_workflow_approval(
            config,
            git_repo,
            f"{workflow_id}-final",
            _workflow_manifest(workflow_id),
            store,
        )


def test_apply_refuses_an_artifact_the_named_workflow_did_not_finalize(
    tmp_path, git_repo
):
    # The artifact names the workflow, so "some completed workflow exists" is
    # not an authorization: a copied or hand-written manifest could point at
    # anyone's genuinely completed workflow. Only the workflow's own
    # final_run_id says which run it approved.
    from syncmill.cli import _heal_workflow_approval

    config, workflow_id = _plant_workflow(
        tmp_path, git_repo, "completed", finalized="wf-abcdef0123456789-final"
    )
    with pytest.raises(click.ClickException, match="not approved for apply"):
        _heal_workflow_approval(
            config,
            git_repo,
            f"{workflow_id}-final",
            _workflow_manifest(workflow_id),
            _artifact_store(config, git_repo),
        )


def test_apply_refuses_a_workflow_artifact_whose_row_was_pruned(tmp_path, git_repo):
    # `workflow prune` deletes terminal rows of EVERY outcome, cancelled
    # included, so absence is not evidence of approval. A workflow that
    # completed normally stamped its artifact and never reaches this path.
    from syncmill.cli import _heal_workflow_approval

    config, _ = _workflow_runs_root(tmp_path, git_repo)
    store = _artifact_store(config, git_repo)
    with pytest.raises(click.ClickException, match="no longer on record"):
        _heal_workflow_approval(
            config,
            git_repo,
            "wf-0123456789abcdef-final",
            _workflow_manifest("wf-0123456789abcdef"),
            store,
        )


def test_apply_guard_ignores_runs_that_did_not_come_from_a_workflow(
    tmp_path, git_repo
):
    from syncmill.cli import _heal_workflow_approval
    from syncmill.workflow import WorkflowStore

    config, project = _workflow_runs_root(tmp_path, git_repo)
    store = _artifact_store(config, git_repo)
    for manifest in ({}, {"plan": None}, {"plan": {"origin": "room"}}):
        # Even an id shaped exactly like a workflow final is someone else's run
        # when its manifest does not claim a workflow.
        _heal_workflow_approval(
            config, git_repo, "wf-0123456789abcdef-final", manifest, store
        )
    # An already-approved workflow run needs no store read either.
    _heal_workflow_approval(
        config,
        git_repo,
        "wf-0123456789abcdef-final",
        _workflow_manifest("wf-0123456789abcdef", approved=True),
        store,
    )
    # None of this created a workflow database in a project without workflows.
    assert not WorkflowStore.database_path(Path(config.runs.root), project).exists()


def test_apply_guard_fails_closed_when_the_workflow_store_cannot_be_read(
    tmp_path, git_repo
):
    # "The database would not open" is not evidence that the workflow completed,
    # and this guard stands in front of patching the user's tree. The damage
    # here is a TRUNCATED database — what a crash or a full disk leaves behind —
    # which sqlite accepts as a valid EMPTY one: constructing a WorkflowStore
    # over it re-runs CREATE TABLE and then answers "no such workflow", so the
    # guard would manufacture its own permission out of the damage. Reading it
    # read-only cannot: there is no table to select from.
    from syncmill.cli import _heal_workflow_approval
    from syncmill.workflow import WorkflowStore

    config, project = _workflow_runs_root(tmp_path, git_repo)
    database = WorkflowStore.database_path(Path(config.runs.root), project)
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(b"")

    with pytest.raises(click.ClickException, match="could not be read"):
        _heal_workflow_approval(
            config,
            git_repo,
            "wf-0123456789abcdef-final",
            _workflow_manifest("wf-0123456789abcdef"),
            _artifact_store(config, git_repo),
        )


def test_apply_guard_refuses_a_workflow_artifact_naming_no_workflow(
    tmp_path, git_repo
):
    from syncmill.cli import _heal_workflow_approval

    config, _ = _workflow_runs_root(tmp_path, git_repo)
    with pytest.raises(click.ClickException, match="could not be read"):
        _heal_workflow_approval(
            config,
            git_repo,
            "run-x",
            {"plan": {"origin": "workflow"}},
            _artifact_store(config, git_repo),
        )


def test_workflow_preflight_warnings_are_printed_even_when_the_run_fails(
    monkeypatch, runner
):
    from syncmill.workflow import WorkflowError

    class FakeSupervisor:
        workflow_preflight_warnings = ["toolgraph unavailable"]

        def run_workflow(self, workflow_id, *, resume, owner=None, job_id=None):
            raise WorkflowError("nothing ran")

        def close(self):
            pass

    monkeypatch.setattr(
        "syncmill.cli._workflow_supervisor",
        lambda repo, team: (FakeSupervisor(), None),
    )
    monkeypatch.setattr("syncmill.cli._peek_workflow_team", lambda repo, wid: None)
    res = runner.invoke(cli, ["workflow-worker", "wf-0123456789abcdef"])
    assert res.exit_code != 0
    assert "[syncmill] preflight: toolgraph unavailable" in res.output


def test_a_bare_approval_claim_is_not_an_approval(tmp_path, git_repo):
    # `plan` is producer-supplied data, so a boolean in it authorizes whatever
    # it was pasted onto: an artifact copied from another project, a run whose
    # patch was swapped afterwards, or a manifest that simply claimed it.
    from syncmill.artifacts import workflow_approved
    from syncmill.cli import _heal_workflow_approval

    manifest = _workflow_manifest("wf-0123456789abcdef")
    manifest["plan"]["approved"] = True
    assert not workflow_approved(manifest)

    # It also does not short-circuit the healing check, which is where a bare
    # flag would have bought its way past the workflow row.
    config, _ = _workflow_runs_root(tmp_path, git_repo)
    with pytest.raises(click.ClickException, match="no longer on record"):
        _heal_workflow_approval(
            config,
            git_repo,
            "wf-0123456789abcdef-final",
            manifest,
            _artifact_store(config, git_repo),
        )

    # An approval granted for a DIFFERENT run, or against a different patch,
    # is refused just as firmly.
    borrowed = _workflow_manifest("wf-0123456789abcdef", approved=True)
    borrowed["run_id"] = "some-other-run"
    assert not workflow_approved(borrowed)
    swapped = _workflow_manifest("wf-0123456789abcdef", approved=True)
    swapped["patch_sha256"] = "e" * 64
    assert not workflow_approved(swapped)


def test_a_producer_cannot_save_its_own_approval(tmp_path, git_repo):
    from syncmill.artifacts import workflow_approved

    config, _ = _workflow_runs_root(tmp_path, git_repo)
    store = _artifact_store(config, git_repo)
    task = Task(prompt="w")
    plan = {
        "origin": "workflow",
        "workflow_id": "wf-0123456789abcdef",
        "approved": {"workflow_id": "wf-0123456789abcdef", "run_id": "run-1"},
    }
    artifact = store.begin(
        run_id="run-1",
        task=task,
        repo_root=git_repo,
        snapshot_commit="head",
        source_fingerprint="fp",
        strategy="route",
        agents=["claude"],
        plan=plan,
    )
    assert "approved" not in artifact.manifest["plan"]

    saved = store.save(
        run_id="run-1",
        task=task,
        result=_result(patch="diff --git a/x b/x\n"),
        repo_root=git_repo,
        snapshot_commit="head",
        source_fingerprint="fp",
        strategy="route",
        plan=plan,
    )
    assert "approved" not in saved.manifest["plan"]
    assert not workflow_approved(saved.manifest)


# ── review 2026-09-10 regressions ────────────────────────────────────────


def test_status_dirty_ignores_an_ambient_git_dir(git_repo, tmp_path, monkeypatch):
    # The one git call in the package that ran without isolated_git_env: inside
    # a hook or `git rebase --exec`, an ambient GIT_DIR pointed it at ANOTHER
    # repository's index, so `doctor` and `run --dry-run` reported a
    # clean/dirty verdict for a tree other than the one they named.
    from syncmill.cli import subprocess_status_dirty

    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
    (git_repo / "README.md").write_text("# dirty now\n")

    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    assert subprocess_status_dirty(git_repo) is True


def test_status_dirty_reports_a_git_failure_instead_of_clean(tmp_path):
    # Swallowing the exit code reported an unreadable or rejected repository
    # ("dubious ownership") as having nothing to preserve.
    from syncmill.cli import subprocess_status_dirty
    from syncmill.worktree import WorkspaceError

    with pytest.raises(WorkspaceError):
        subprocess_status_dirty(tmp_path)  # not a git repository


def test_run_repo_flag_expands_a_tilde(monkeypatch, runner, tmp_path):
    # Consumers `.resolve()` repo_root without `.expanduser()`, so a quoted
    # `~/work/app` (from a Makefile, a config, a subprocess list) resolved to
    # `$PWD/~/work/app` and failed as "not a Git repository".
    monkeypatch.setenv("HOME", str(tmp_path))
    _point_toml(monkeypatch, tmp_path, "")
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "-r", "~/project"])
    assert res.exit_code == 0
    assert h["config"].repo_root == str(tmp_path / "project")


def test_toolgraph_agent_map_rejects_an_option_shaped_identity():
    from pydantic import ValidationError

    # agent_map values occupy the argv token immediately BEFORE candidate_tools,
    # which has this exact validator: an identity like "--out" redirects
    # toolgraph's own output flag (argv position confusion, no shell involved).
    with pytest.raises(ValidationError):
        SyncmillConfig(integration={"toolgraph": {"agent_map": {"codex": "--out"}}})
    with pytest.raises(ValidationError):
        SyncmillConfig(integration={"toolgraph": {"agent_map": {"codex": "a b"}}})
    # a hyphenated real agent id stays a verbatim KEY, and a normal identity passes
    config = SyncmillConfig(
        integration={"toolgraph": {"agent_map": {"kimi-code": "kimi"}}}
    )
    assert config.integration.toolgraph.agent_map == {"kimi-code": "kimi"}


def test_doctor_reports_artifact_store_construction_failure(monkeypatch, runner, tmp_path):
    from syncmill.artifacts import ArtifactStore

    occupied = tmp_path / "occupied-runs-root"
    occupied.write_text("not a directory")
    config = SyncmillConfig(agent_priority=["codex"])
    config.runs.root = str(occupied)
    config.memory.memory_dir = str(tmp_path / "memory")
    _patch_doctor_runtime(
        monkeypatch, tmp_path, found={"codex": "/bin/codex"},
        auth_ready={"codex": True}, config=config,
    )
    # Keep only provider/snapshot checks fake; exercise the actual constructor.
    monkeypatch.setattr("syncmill.cli.ArtifactStore", ArtifactStore)
    result = runner.invoke(cli, ["doctor"])
    assert result.exit_code == 1
    assert "run artifact store is not writable:" in result.output
    assert "Traceback" not in result.output
    assert isinstance(result.exception, SystemExit)
    assert occupied.read_text() == "not a directory"
