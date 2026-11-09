"""``run`` and ``apply`` say when no check stands behind a result (#265)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from conftest import FakeAgent, FakeMemory, FakeRegistry, FakeWorktreePool

import syncmill.monitor as M
from syncmill.artifacts import ArtifactStore, recorded_gate_commands
from syncmill.cli import _run_apply, cli
from syncmill.config import SyncmillConfig
from syncmill.models import GateCheck, GateReport, Task, TaskResult, TaskStatus
from syncmill.strategies import CompeteStrategy, _make_strategy
from syncmill.supervisor import Supervisor
from syncmill.worktree import WorkspaceError

UNVERIFIED = (
    "[syncmill] Unverified: no quality gate ran on this result. To check results, "
    "add commands under [gate] in syncmill.toml."
)
FIRST_COMPLETED = (
    "[syncmill] With no gate, the winner is the first completed candidate in "
    "priority order, not the result of a comparison."
)
APPLY_UNVERIFIED = (
    "[syncmill] Unverified: this run recorded no quality gate commands, and "
    "--verify was not used."
)
APPLY_NOT_RECORDED = (
    "[syncmill] Verification not recorded: this run has no record of a quality "
    "gate, and --verify was not used."
)


def _result(**overrides) -> TaskResult:
    values = dict(
        task_id=uuid.uuid4(),
        agent_id="codex",
        status=TaskStatus.COMPLETED,
        output="done",
        exit_code=0,
        duration_seconds=1.5,
    )
    values.update(overrides)
    return TaskResult(**values)


def _passing_gate() -> GateReport:
    return GateReport.from_checks([GateCheck("pytest", passed=True, exit_code=0)])


def _lines(capsys) -> list[str]:
    return capsys.readouterr().out.splitlines()


# ── render_result: every caller's shared output ───────────────────────────


def test_a_completed_result_with_no_gate_says_it_is_unverified(capsys):
    assert M.render_task_result(_result()) == 0
    lines = _lines(capsys)
    assert lines[:2] == ["[codex] Completed in 1.5s", UNVERIFIED]
    assert FIRST_COMPLETED not in lines


def test_a_passing_gate_prints_no_notice(capsys):
    assert M.render_task_result(_result(gate=_passing_gate())) == 0
    lines = _lines(capsys)
    assert lines[1] == "[syncmill] Quality gate: passed (1/1 checks passed)"
    assert UNVERIFIED not in lines


def test_a_failed_result_prints_no_notice_and_keeps_its_exit_code(capsys):
    result = _result(status=TaskStatus.FAILED, exit_code=1, error="boom",
                     picked_first_completed=True)
    assert M.render_task_result(result) == 1
    lines = _lines(capsys)
    assert UNVERIFIED not in lines and FIRST_COMPLETED not in lines


def test_the_first_completed_line_follows_the_unverified_line(capsys):
    assert M.render_task_result(_result(picked_first_completed=True)) == 0
    assert _lines(capsys)[1:3] == [UNVERIFIED, FIRST_COMPLETED]


def test_result_json_carries_the_flag_to_the_monitored_outer(tmp_path, capsys):
    M.write_result(tmp_path, _result(picked_first_completed=True))
    data = M.read_result(tmp_path)
    assert data["picked_first_completed"] is True
    assert M.render_result(data) == 0
    assert FIRST_COMPLETED in _lines(capsys)


def test_monitored_and_live_renders_are_identical(tmp_path, capsys):
    result = _result(picked_first_completed=True, files_changed=["a.py"])
    M.render_task_result(result)
    live = capsys.readouterr().out
    M.write_result(tmp_path, result)
    M.render_result(M.read_result(tmp_path))
    assert capsys.readouterr().out == live
    assert UNVERIFIED in live and FIRST_COMPLETED in live


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_an_unvalidated_flag_must_be_exactly_true(value, capsys):
    data = json.loads(json.dumps(M._result_to_json(_result())))
    data["picked_first_completed"] = value
    assert M.render_result(data) == 0
    lines = _lines(capsys)
    assert FIRST_COMPLETED not in lines
    assert UNVERIFIED in lines


# ── CompeteStrategy: what the flag means ──────────────────────────────────


def _compete(agents, *, mode="verified", gate=None, pool=None):
    config = SyncmillConfig()
    config.selection.mode = mode
    config.selection.judge_agent = "claude"
    return CompeteStrategy().execute(
        task=Task(prompt="do it"),
        agents=agents,
        memory=FakeMemory(),
        pool=pool or FakeWorktreePool(),
        config=config,
        gate=gate,
    )


@pytest.fixture
def stub_capture(monkeypatch):
    from syncmill.worktree import CapturedChanges

    monkeypatch.setattr(
        "syncmill.strategies.base.capture_changes",
        lambda *a, **k: CapturedChanges(files_changed=[], patch=""),
    )


@pytest.mark.parametrize("mode", ["verified", "priority"])
def test_two_completed_with_no_gate_is_a_priority_pick(mode):
    winner = _compete([FakeAgent("claude"), FakeAgent("codex")], mode=mode)
    assert winner.agent_id == "claude"
    assert winner.picked_first_completed is True


def test_one_completed_passed_nobody_over():
    winner = _compete([
        FakeAgent("claude", status=TaskStatus.FAILED, error="boom", exit_code=1),
        FakeAgent("codex"),
    ])
    assert winner.agent_id == "codex"
    assert winner.picked_first_completed is False


def test_a_lone_candidate_passed_nobody_over():
    assert _compete([FakeAgent("codex")]).picked_first_completed is False


def test_a_gate_means_no_priority_pick(stub_capture):
    from conftest import FakeGate

    winner = _compete([FakeAgent("claude"), FakeAgent("codex")], gate=FakeGate(passes=True))
    assert winner.gate is not None
    assert winner.picked_first_completed is False


def test_a_valid_judge_verdict_is_not_a_priority_pick():
    winner = _compete(
        [FakeAgent("claude", output="analysis\nWINNER: codex"), FakeAgent("codex")],
        mode="judge",
    )
    assert winner.agent_id == "codex"
    assert winner.picked_first_completed is False


def test_a_judge_verdict_naming_the_first_candidate_is_not_a_priority_pick():
    winner = _compete(
        [FakeAgent("claude", output="analysis\nWINNER: claude"), FakeAgent("codex")],
        mode="judge",
    )
    assert winner.agent_id == "claude"
    assert winner.picked_first_completed is False


def test_a_judge_with_no_valid_winner_falls_back_to_a_priority_pick():
    winner = _compete(
        [FakeAgent("claude", output="no idea"), FakeAgent("codex")], mode="judge"
    )
    assert winner.agent_id == "claude"
    assert winner.picked_first_completed is True


def test_a_judge_that_could_not_run_falls_back_to_a_priority_pick():
    class _NoJudgeWorktree(FakeWorktreePool):
        def acquire(self, agent_id):
            if agent_id.endswith("-judge"):
                raise WorkspaceError("no space left on device")
            return super().acquire(agent_id)

    winner = _compete(
        [FakeAgent("claude"), FakeAgent("codex")], mode="judge", pool=_NoJudgeWorktree()
    )
    assert winner.agent_id == "claude"
    assert winner.picked_first_completed is True


# ── The real path: Supervisor → artifact → apply ──────────────────────────


class _EditingAgent(FakeAgent):
    """Leaves a real change, so the run has something to apply."""

    def _edit(self, workdir: Path) -> None:
        (workdir / f"{self.agent_id}.txt").write_text("edit\n", encoding="utf-8")

    def execute(self, prompt, workdir, timeout):
        self._edit(workdir)
        return super().execute(prompt, workdir, timeout)

    async def aexecute(self, prompt, workdir, timeout):
        self._edit(workdir)
        return await super().aexecute(prompt, workdir, timeout)


def _config(git_repo: Path, tmp_path: Path, gate: list[str], agents: list[str]) -> SyncmillConfig:
    return SyncmillConfig(
        repo_root=str(git_repo),
        default_strategy="compete",
        workspace={"root": str(tmp_path / "workspaces")},
        runs={"root": str(tmp_path / "runs")},
        agent_priority=agents,
        gate={"commands": gate},
    )


def _run(config: SyncmillConfig) -> TaskResult:
    sup = Supervisor.__new__(Supervisor)
    sup._config = config
    sup._registry = FakeRegistry([_EditingAgent(a) for a in config.agent_priority])
    sup._memory = FakeMemory()
    sup._breaker = None
    sup._matcher = None
    sup._board = None
    sup._strategy = _make_strategy(config.default_strategy)
    sup.last_report_path = None
    return sup.run(Task(prompt="go"))


def _store(tmp_path: Path) -> ArtifactStore:
    project = next((tmp_path / "runs").iterdir())
    return ArtifactStore(tmp_path / "runs", project.name)


def _manifest_path(store: ArtifactStore, tmp_path: Path, run_id: str) -> Path:
    project = next((tmp_path / "runs").iterdir())
    return project / "runs" / run_id / "manifest.json"


def _apply_output(store, git_repo, config, run_id, verify=False) -> list[str]:
    @click.command()
    def apply():
        _run_apply(store, git_repo, config, run_id, verify)

    result = CliRunner().invoke(apply)
    assert result.exit_code == 0, result.output
    assert any(line.startswith("Applied run ") for line in result.output.splitlines())
    return result.output.splitlines()


def test_the_flag_survives_a_real_run_to_the_returned_result(git_repo, tmp_path, capsys):
    config = _config(git_repo, tmp_path, [], ["claude", "codex"])
    result = _run(config)
    assert result.agent_id == "claude"
    assert result.picked_first_completed is True
    assert M.render_task_result(result) == 0
    assert _lines(capsys)[1:3] == [UNVERIFIED, FIRST_COMPLETED]


def test_apply_of_a_run_with_no_gate_says_it_is_unverified(git_repo, tmp_path):
    config = _config(git_repo, tmp_path, [], ["codex"])
    result = _run(config)
    lines = _apply_output(_store(tmp_path), git_repo, config, result.run_id)
    assert lines[-1] == APPLY_UNVERIFIED
    assert APPLY_NOT_RECORDED not in lines


def test_apply_of_a_gated_run_says_nothing(git_repo, tmp_path):
    config = _config(git_repo, tmp_path, ["python -c pass"], ["codex"])
    result = _run(config)
    assert result.gate is not None and result.gate.passed
    lines = _apply_output(_store(tmp_path), git_repo, config, result.run_id)
    assert APPLY_UNVERIFIED not in lines and APPLY_NOT_RECORDED not in lines


def test_apply_verify_says_nothing_even_for_a_run_with_no_gate(git_repo, tmp_path):
    config = _config(git_repo, tmp_path, [], ["codex"])
    result = _run(config)
    config.gate.commands = ["python -c pass"]
    lines = _apply_output(_store(tmp_path), git_repo, config, result.run_id, verify=True)
    assert APPLY_UNVERIFIED not in lines and APPLY_NOT_RECORDED not in lines


@pytest.mark.parametrize(
    "edit",
    [
        lambda m: m.pop("plan"),
        lambda m: m.update(plan="not a mapping"),
        lambda m: m["plan"].pop("gate_commands"),
        lambda m: m["plan"].update(gate_commands="pytest"),
        lambda m: m["plan"].update(gate_commands=[1]),
    ],
    ids=["no-plan", "plan-not-a-dict", "no-field", "field-not-a-list", "field-not-strings"],
)
def test_apply_of_a_run_with_no_usable_record_never_reads_as_verified(
    edit, git_repo, tmp_path
):
    config = _config(git_repo, tmp_path, ["python -c pass"], ["codex"])
    result = _run(config)
    store = _store(tmp_path)
    path = _manifest_path(store, tmp_path, result.run_id)
    manifest = json.loads(path.read_text())
    edit(manifest)
    path.write_text(json.dumps(manifest))
    lines = _apply_output(store, git_repo, config, result.run_id)
    assert lines[-1] == APPLY_NOT_RECORDED
    assert APPLY_UNVERIFIED not in lines


def test_the_detached_worker_prints_the_same_line(git_repo, tmp_path, monkeypatch):
    config = _config(git_repo, tmp_path, [], ["codex"])
    result = _run(config)
    snapshot = tmp_path / "resolved.json"
    snapshot.write_text(config.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        "syncmill.cli.project_id", lambda root, explicit: _store(tmp_path).root.parent.name
    )
    out = CliRunner().invoke(
        cli,
        ["apply-worker", result.run_id, "--repo", str(git_repo),
         "--resolved-config", str(snapshot)],
    )
    assert out.exit_code == 0, out.output
    assert out.output.splitlines()[-1] == APPLY_UNVERIFIED


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({"plan": {"gate_commands": ["pytest"]}}, ["pytest"]),
        ({"plan": {"gate_commands": []}}, []),
        ({"plan": {}}, None),
        ({}, None),
    ],
)
def test_recorded_gate_commands(manifest, expected):
    assert recorded_gate_commands(manifest) == expected
