"""`syncmill start` on a real project (#171): readiness, the confirmation
screen, `--dry-run`, verification choice and the single-agent run → diff →
apply follow-through.

Hermetic: agent readiness is a fake, and the `run`/`diff`/`apply` commands that
`start` invokes are replaced by recorders, so no agent, provider or apply runs.
"""

from __future__ import annotations

import json
import os
import subprocess

import click
import pytest
from click.testing import CliRunner

import syncmill.cli as cli_module
from syncmill.cli import AgentReadiness, cli
from syncmill.start import StartPlan, discover_gate_commands, render_start_plan
from syncmill.teamfile import set_gate_commands, toml_path


def _ready(*ids, missing=(), logged_out=()):
    rows = [
        AgentReadiness(a, known=True, enabled=True, binary=f"/bin/{a}",
                       authenticated=True, ready=True, problem=None)
        for a in ids
    ]
    rows += [
        AgentReadiness(a, known=True, enabled=True, binary=None, authenticated=False,
                       ready=False, problem=f"enabled agent binary not found: {a}")
        for a in missing
    ]
    rows += [
        AgentReadiness(a, known=True, enabled=True, binary=f"/bin/{a}",
                       authenticated=False, ready=False, problem=f"{a} is not authenticated")
        for a in logged_out
    ]
    return rows


@pytest.fixture
def project(monkeypatch, git_repo):
    monkeypatch.chdir(git_repo)
    monkeypatch.setenv("SYNCMILL_REPO_ROOT", str(git_repo))
    return git_repo


@pytest.fixture
def calls(monkeypatch):
    """Record what `start` invokes instead of running agents or applying."""
    seen: dict[str, list] = {"run": [], "diff": [], "apply": []}

    def fake_run(**kwargs):
        seen["run"].append({**kwargs, "gate_env": os.environ.get("SYNCMILL_GATE__COMMANDS")})
        click.get_current_context().meta[cli_module._START_RUN_META] = ("RUN-1", True)

    def fake_diff(**kwargs):
        seen["diff"].append(kwargs)

    def fake_apply(**kwargs):
        seen["apply"].append({**kwargs, "gate_env": os.environ.get("SYNCMILL_GATE__COMMANDS")})

    monkeypatch.setattr(cli_module.run, "callback", fake_run)
    monkeypatch.setattr(cli_module.diff_run, "callback", fake_diff)
    monkeypatch.setattr(cli_module.apply_run, "callback", fake_apply)
    return seen


def _start(monkeypatch, args, *, ready, interactive=False, input=None):
    monkeypatch.setattr(cli_module, "agent_readiness", lambda config: ready)
    monkeypatch.setattr(cli_module, "_start_is_interactive", lambda: interactive)
    return CliRunner().invoke(cli, ["start", *args], input=input)


def test_dry_run_shows_the_screen_and_runs_nothing(monkeypatch, project, calls):
    result = _start(
        monkeypatch, ["--task", "add type hints", "--dry-run"],
        ready=_ready("codex", "claude", missing=["kimi"]),
    )

    assert result.exit_code == 0, result.output
    assert "Mode           single agent" in result.output
    assert "Agents         codex (timeout 5 min)" in result.output
    assert "Agent calls    at most 1 call" in result.output
    assert "Verification   none — the result will be unverified" in result.output
    assert f"Repository     {project}" in result.output
    assert "Cost           unknown" in result.output
    assert "Nothing was run (--dry-run)." in result.output
    assert "kimi: not installed" in result.output  # why it is not used
    assert calls["run"] == []


def test_without_a_terminal_it_needs_task_and_yes(monkeypatch, project, calls):
    no_task = _start(monkeypatch, ["-a", "codex"], ready=_ready("codex"))
    assert no_task.exit_code == 1 and "start needs --task" in no_task.output

    no_yes = _start(monkeypatch, ["--task", "t"], ready=_ready("codex"))
    assert no_yes.exit_code == 1 and "start needs --yes" in no_yes.output
    assert calls["run"] == []

    ran = _start(monkeypatch, ["--task", "t", "--yes"], ready=_ready("codex"))
    assert ran.exit_code == 0, ran.output
    assert [(c["prompt"], c["agent"]) for c in calls["run"]] == [("t", "codex")]
    assert calls["diff"] == calls["apply"] == []  # nobody to ask; run printed the commands


def test_no_ready_agent_points_at_the_trial(monkeypatch, project, calls):
    result = _start(
        monkeypatch, ["--task", "t"], ready=_ready(missing=["codex"], logged_out=["claude"])
    )

    assert result.exit_code == 1
    assert "No agent is ready to run:" in result.output
    assert "codex: not installed" in result.output
    assert "claude: not logged in" in result.output
    assert "syncmill start --trial" in result.output


def test_agent_choices_are_checked_before_anything_runs(monkeypatch, project, calls):
    lonely = _start(monkeypatch, ["--task", "t", "--compare"], ready=_ready("codex"))
    assert lonely.exit_code == 1 and "nothing to compare" in lonely.output

    unready = _start(monkeypatch, ["--task", "t", "-a", "kimi-code"], ready=_ready("codex"))
    assert unready.exit_code == 2 and "kimi-code is not ready" in unready.output

    same = _start(monkeypatch, ["--task", "t", "-a", "codex", "-a", "codex"],
                  ready=_ready("codex", "claude"))
    assert same.exit_code == 2 and "two different agents" in same.output
    assert calls["run"] == []


def test_one_ready_agent_is_offered_as_a_single_run(monkeypatch, project, calls):
    result = _start(
        monkeypatch, [], ready=_ready("codex"), interactive=True,
        input="2\nfix it\n2\nn\n",  # project; task; no verification; don't run
    )

    assert result.exit_code == 0, result.output
    assert "Only codex is ready, so this is a single-agent run." in result.output
    assert "Nothing was run." in result.output
    assert calls["run"] == []


def test_interactive_run_saves_the_gate_then_shows_and_applies(monkeypatch, project, calls):
    (project / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-q"\n')

    result = _start(
        monkeypatch, ["--task", "fix the bug"], ready=_ready("codex", "claude"),
        interactive=True,
        # one agent; first suggested check; save it; run; show diff; apply
        input="1\n1\ny\ny\ny\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "1) python -m pytest -q   <- pyproject.toml" in result.output
    assert "Verification   python -m pytest -q  (from your [gate] settings)" in result.output
    assert "[gate]" in toml_path().read_text()
    assert calls["run"][0]["agent"] == "codex"
    assert calls["run"][0]["gate_env"] is None  # saved, so the file carries it
    assert calls["diff"] == [{"run_id": "RUN-1", "repo": cli_module.Path(".")}]
    assert calls["apply"][0]["verify"] is True


def test_a_gate_for_this_run_only_reaches_run_and_apply_but_not_the_file(
    monkeypatch, project, calls
):
    result = _start(
        monkeypatch, ["--task", "t", "-a", "codex"], ready=_ready("codex"),
        interactive=True,
        input="1\npython -m unittest\nn\ny\nn\ny\n",  # type a command; don't save
    )

    assert result.exit_code == 0, result.output
    assert "(this run only, not saved)" in result.output
    assert json.loads(calls["run"][0]["gate_env"]) == ["python -m unittest"]
    assert json.loads(calls["apply"][0]["gate_env"]) == ["python -m unittest"]
    assert "SYNCMILL_GATE__COMMANDS" not in os.environ
    assert not toml_path().exists()


def test_a_configured_gate_is_used_without_asking(monkeypatch, project, calls):
    monkeypatch.setenv("SYNCMILL_GATE__COMMANDS", json.dumps(["make check"]))

    result = _start(monkeypatch, ["--task", "t", "--dry-run"], ready=_ready("codex"))

    assert "Verification   make check  (from your [gate] settings)" in result.output
    assert "How should each result be verified?" not in result.output


def test_uncommitted_changes_are_named_on_the_screen(monkeypatch, project, calls):
    (project / "README.md").write_text("edited\n")
    (project / "new.txt").write_text("new\n")

    result = _start(monkeypatch, ["--task", "t", "--dry-run"], ready=_ready("codex"))

    assert "includes 2 uncommitted change(s)" in result.output


def test_declining_the_apply_names_the_command(monkeypatch, project, calls):
    result = _start(
        monkeypatch, ["--task", "t"], ready=_ready("codex"), interactive=True,
        input="2\ny\nn\nn\n",  # no verification; run; no diff; no apply
    )

    assert result.exit_code == 0, result.output
    assert "Apply it to your repository? (unverified)" in result.output
    assert "Not applied. Later: syncmill apply RUN-1" in result.output
    assert calls["apply"] == []


# ── pure helpers ─────────────────────────────────────────────────────────


def test_discovery_reads_known_files_and_never_runs_them(tmp_path):
    canary = tmp_path / "ran"
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["pytest"]\n')
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": f"touch {canary}"}}))
    (tmp_path / "Makefile").write_text(f"build:\n\ttrue\ntest:\n\ttouch {canary}\n")
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    (tmp_path / "go.mod").write_text("module x\n")

    found = discover_gate_commands(tmp_path)

    assert [(s.command, s.source) for s in found] == [
        ("python -m pytest -q", "pyproject.toml"),
        ("npm test", "package.json"),
        ("make test", "Makefile"),
        ("cargo test", "Cargo.toml"),
        ("go test ./...", "go.mod"),
    ]
    assert not canary.exists()


def test_discovery_skips_placeholders_and_symlinks(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps(
        {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}
    ))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "go.mod").write_text("module x\n")
    (tmp_path / "go.mod").symlink_to(elsewhere / "go.mod")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")  # no pytest

    assert discover_gate_commands(tmp_path) == []


def test_set_gate_commands_keeps_comments_and_other_settings(tmp_path):
    path = tmp_path / "syncmill.toml"
    path.write_text("# my config\n[gate]\n# keep me\ntimeout_seconds = 99\n")

    set_gate_commands(path, ["python -m pytest -q"])

    text = path.read_text()
    assert "# my config" in text and "# keep me" in text and "timeout_seconds = 99" in text
    assert 'commands = ["python -m pytest -q"]' in text
    fresh = tmp_path / "new.toml"
    set_gate_commands(fresh, ["make test"])
    assert fresh.read_text().strip() == '[gate]\ncommands = ["make test"]'


def test_render_start_plan_for_a_comparison():
    text = render_start_plan(StartPlan(
        mode="compare", agents=("codex", "claude"), timeouts=(1800, 90),
        max_calls=2, gate_commands=(), gate_scope="none", repository="/r",
        dirty=0, memory="/m",
    ))
    assert "Mode           compare 2 candidates (Room), then pick one to apply" in text
    assert "Agents         codex (timeout 30 min), claude (timeout 90 s)" in text
    assert "Agent calls    at most 2 calls" in text


def test_git_status_failure_is_a_clean_error(monkeypatch, tmp_path, calls):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)
    monkeypatch.setenv("SYNCMILL_REPO_ROOT", str(not_a_repo))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))

    result = _start(monkeypatch, ["--task", "t", "--dry-run"], ready=_ready("codex"))

    assert result.exit_code == 1
    assert "not a Git repository" in result.output
    assert subprocess.run(["git", "status"], cwd=not_a_repo, capture_output=True).returncode != 0


# ── two-candidate comparison (Room) ──────────────────────────────────────


def _doc(*rows):
    """A minimal `room compare --json` document for the latest round."""
    return {"room": {"room_id": "ROOM-1", "revision": 3}, "rounds": [{"candidates": [
        {"candidate_id": cid, "member_id": agent, "eligible": eligible,
         "not_eligible_reason": None if eligible else "quality gate failed",
         "gate": {"verdict": verdict}, "files_changed": ["calc.py"],
         "patch": {"sha256": f"sha-{cid}"}}
        for cid, agent, verdict, eligible in rows
    ]}]}


@pytest.fixture
def room(monkeypatch, calls):
    """A fake coordinator plus recorders for the room commands `start` invokes."""
    state = {"created": [], "rounds": [], "doc": _doc(
        ("CAND-A", "codex", "passed", True), ("CAND-B", "claude", "passed", True),
    ), "create_error": None, "selected": []}

    class Coordinator:
        store = object()
        repo_root = "."

        def create(self, prompt, agent_ids):
            if state["create_error"]:
                raise cli_module.RoomError(state["create_error"])
            state["created"].append((prompt, list(agent_ids)))
            return {"id": "ROOM-1"}

        def run_round(self, room_id, prompt):
            state["rounds"].append((room_id, prompt))

        def select(self, room_id, candidate_id, **kwargs):
            state["selected"].append((room_id, candidate_id, kwargs))

        def close(self):
            pass

    monkeypatch.setattr(cli_module, "_room_coordinator", lambda repo: (repo, None, Coordinator()))
    monkeypatch.setattr(cli_module, "build_room_comparison", lambda *a, **k: "comparison")
    monkeypatch.setattr(cli_module, "comparison_to_dict", lambda comparison: state["doc"])
    for name in ("diff", "select", "apply"):
        calls[f"room_{name}"] = []
    monkeypatch.setattr(cli_module.room_diff, "callback", lambda **k: calls["room_diff"].append(k))
    monkeypatch.setattr(cli_module.room_select, "callback", lambda **k: calls["room_select"].append(k))
    monkeypatch.setattr(cli_module.room_apply, "callback", lambda **k: calls["room_apply"].append(k))
    return state


def test_compare_without_a_terminal_shows_candidates_and_the_commands(
    monkeypatch, project, calls, room
):
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_GATE__COMMANDS", json.dumps(["make check"]))

    result = _start(monkeypatch, ["--task", "fix it", "--compare", "--yes"],
                    ready=_ready("codex", "claude"))

    assert result.exit_code == 0, result.output
    assert "Agent calls    at most 2 calls" in result.output
    assert room["created"] == [("fix it", ["codex", "claude"])]
    assert room["rounds"] == [("ROOM-1", None)]
    assert "  A  codex      verification passed" in result.output
    assert "Review: syncmill room diff CAND-A" in result.output
    assert "Select: syncmill room select ROOM-1 CAND-A" in result.output
    assert "Apply:  syncmill room apply ROOM-1 --verify" in result.output
    assert room["selected"] == calls["room_apply"] == []


def test_compare_at_a_terminal_picks_shows_and_applies(monkeypatch, project, calls, room):
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")

    result = _start(
        monkeypatch, ["--task", "fix it"], ready=_ready("codex", "claude"), interactive=True,
        # compare; no verification; run; candidate B; show; apply
        input="2\n2\ny\nB\ny\ny\n",
    )

    assert result.exit_code == 0, result.output
    assert "Apply B to your repository? (unverified)" in result.output
    assert calls["room_diff"] == [{"candidate_id": "CAND-B", "repo": cli_module.Path(".")}]
    # bound to what was reviewed: the compared revision and B's patch digest
    assert room["selected"] == [("ROOM-1", "CAND-B", {
        "expected_revision": 3, "expected_patch_sha256": "sha-CAND-B", "surface": "cli",
    })]
    assert "Selected B (claude)." in result.output
    assert "Apply separately" not in result.output
    assert calls["room_apply"][0]["verify"] is False
    assert calls["run"] == []


def test_compare_with_three_ready_agents_lets_you_pick_the_pair(
    monkeypatch, project, calls, room
):
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")

    result = _start(
        monkeypatch, ["--task", "t"], ready=_ready("codex", "claude", "kimi-code"),
        interactive=True, input="2\nclaude,kimi-code\n2\nn\n",  # ...then don't run
    )

    assert result.exit_code == 0, result.output
    assert "Agents         claude (timeout 5 min), kimi-code (timeout 5 min)" in result.output
    assert room["created"] == []


def test_rooms_off_are_turned_on_only_after_a_yes(monkeypatch, project, calls, room):
    result = _start(
        monkeypatch, ["--task", "t", "--compare"], ready=_ready("codex", "claude"),
        interactive=True, input="y\n2\ny\nq\n",  # turn on; no check; run; decide later
    )

    assert result.exit_code == 0, result.output
    assert "Comparing needs Rooms, which are off." in result.output
    text = toml_path().read_text()
    assert "[rooms]" in text and "enabled = true" in text
    assert "Details: syncmill room compare ROOM-1" in result.output


def test_declining_rooms_falls_back_to_one_agent(monkeypatch, project, calls, room):
    result = _start(
        monkeypatch, ["--task", "t", "--compare"], ready=_ready("codex", "claude"),
        interactive=True, input="n\n2\ny\nn\nn\n",  # no rooms; no check; run; no diff; no apply
    )

    assert result.exit_code == 0, result.output
    assert "Running one agent (codex) instead." in result.output
    assert [c["agent"] for c in calls["run"]] == ["codex"]
    assert room["created"] == [] and not toml_path().exists()


def test_rooms_off_without_a_terminal_is_a_clear_error(monkeypatch, project, calls, room):
    result = _start(monkeypatch, ["--task", "t", "--compare", "--yes"],
                    ready=_ready("codex", "claude"))

    assert result.exit_code == 1
    assert "Comparing needs Rooms, which are off" in result.output
    assert room["created"] == []


def test_dry_run_notes_that_rooms_would_be_turned_on(monkeypatch, project, calls, room):
    result = _start(monkeypatch, ["--task", "t", "--compare", "--dry-run"],
                    ready=_ready("codex", "claude"))

    assert result.exit_code == 0, result.output
    assert "Rooms are off: running this would first ask to turn them on" in result.output
    assert room["created"] == []


def test_no_eligible_candidate_says_so(monkeypatch, project, calls, room):
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
    room["doc"] = _doc(("CAND-A", "codex", "failed", False), ("CAND-B", "claude", "failed", False))

    result = _start(monkeypatch, ["--task", "t", "--compare"], ready=_ready("codex", "claude"),
                    interactive=True, input="2\ny\n")

    assert result.exit_code == 0, result.output
    assert "No candidate can be applied." in result.output
    assert "syncmill room close ROOM-1" in result.output
    assert room["selected"] == []


def test_a_room_error_is_a_clean_message(monkeypatch, project, calls, room):
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "true")
    room["create_error"] = "project already has an active room"

    result = _start(monkeypatch, ["--task", "t", "--compare", "--yes"],
                    ready=_ready("codex", "claude"))

    assert result.exit_code == 1
    assert "Error: project already has an active room" in result.output


def test_start_runs_only_from_the_repository_root(monkeypatch, project, calls):
    sub = project / "src"
    sub.mkdir()
    monkeypatch.chdir(sub)

    result = _start(monkeypatch, ["--task", "t", "--dry-run"], ready=_ready("codex"))

    assert result.exit_code == 1
    assert f"Run syncmill start from the repository root: {project}" in result.output
