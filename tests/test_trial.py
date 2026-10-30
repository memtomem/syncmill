"""`syncmill start` and its offline trial (#171).

The trial drives the real ``syncmill`` CLI against two fake agents inside a
throwaway folder, so the end-to-end tests below run real subprocesses and real
Git — but never a real provider, the network, or the user's home: the children
get an allowlisted environment with HOME and every state path inside the
sandbox.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import syncmill.cli as cli_module
import syncmill.trial as trial
from syncmill.cli import cli


def _run(base, *, confirm=True, keep=False):
    said: list[str] = []
    asked: list[str] = []

    def ask(question: str) -> bool:
        asked.append(question)
        return confirm

    outcome = trial.run_trial(say=said.append, confirm=ask, keep=keep, base=base)
    return outcome, "\n".join(said), asked


@pytest.fixture
def hostile_env(monkeypatch, tmp_path):
    """Ambient settings that would break the trial if they leaked into it.
    Returns the folder the trial is created in (conftest's autouse fixtures
    put their own `home` in tmp_path, so that folder cannot be the base)."""
    monkeypatch.setenv("SYNCMILL_GATE__COMMANDS", json.dumps(["false"]))
    monkeypatch.setenv("SYNCMILL_ROOMS__ENABLED", "false")
    monkeypatch.setenv("SYNCMILL_DEFAULT_STRATEGY", "no-such-strategy")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repo"))
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    base = tmp_path / "base"
    base.mkdir()
    return base


def test_trial_applies_the_verified_candidate_and_keeps_on_request(tmp_path, hostile_env):
    outcome, said, asked = _run(hostile_env, keep=True)

    assert outcome.applied and outcome.kept is not None
    assert asked == ["Apply A to the demo repository?"]
    assert "A  codex      verification passed" in said
    assert "B  kimi-code  verification FAILED" in said
    assert "Only A can be applied: B changed the code but failed the test." in said
    assert "-    return a + b + 1\n+    return a + b" in said
    assert "`python -m unittest` now passes there." in said
    repo = outcome.kept / "demo-repo"
    assert (repo / "calc.py").read_text() == "def add(a, b):\n    return a + b\n"
    assert not (repo / "__pycache__").exists()
    # Nothing reached the "real" home or anywhere outside the sandbox.
    assert not (tmp_path / "real-home").exists()
    assert [p.name for p in hostile_env.iterdir()] == [outcome.kept.name]


def test_trial_declined_applies_nothing_and_removes_its_folder(hostile_env):
    outcome, said, _ = _run(hostile_env, confirm=False)

    assert outcome == trial.TrialOutcome(applied=False, kept=None)
    assert "Nothing was applied. The demo repository still has the bug." in said
    assert "The trial folder was removed (use --keep to keep it)." in said
    assert list(hostile_env.iterdir()) == []


def test_a_failed_trial_keeps_its_folder_and_says_where(tmp_path, monkeypatch):
    # Both fakes take the shortcut, so no candidate passes verification.
    broken = {aid: trial.SCENARIO["kimi-code"] for aid in trial.FAKE_AGENTS}
    monkeypatch.setattr(trial, "SCENARIO", broken)

    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(trial.TrialError) as raised:
        _run(base)

    (kept,) = base.iterdir()
    assert "no candidate passed verification" in str(raised.value)
    assert f"kept for inspection: {kept}" in str(raised.value)


def test_cleanup_refuses_a_folder_it_did_not_create(tmp_path):
    box = trial.TrialSandbox.create(tmp_path)
    stranger = trial.TrialSandbox(root=box.root, base=tmp_path)
    with pytest.raises(trial.TrialError, match="did not create"):
        stranger.cleanup()
    (box.root / trial.MARKER).write_text("someone else\n")
    with pytest.raises(trial.TrialError, match="without our"):
        box.cleanup()
    assert box.root.exists()


def test_sandbox_env_is_an_allowlist(tmp_path, hostile_env):
    box = trial.TrialSandbox.create(tmp_path)
    try:
        env = box.env()
        assert env["HOME"] == str(box.home)
        assert env["PATH"].split(":")[0] == str(box.fakebin)
        assert "GIT_DIR" not in env
        assert json.loads(env["SYNCMILL_GATE__COMMANDS"]) == [trial.GATE_COMMAND]
        assert env["SYNCMILL_ROOMS__ENABLED"] == "true"
        assert "SYNCMILL_DEFAULT_STRATEGY" not in env
        assert all(
            value.startswith(str(box.root))
            for key, value in env.items()
            if key.endswith(("_PATH", "__ROOT", "_DIR", "_ROOT")) and key != "GIT_CONFIG_NOSYSTEM"
        )
    finally:
        box.cleanup()


# ── the `start` command ──────────────────────────────────────────────────


@pytest.fixture
def recorded(monkeypatch):
    calls: list[dict] = []

    def fake_run_trial(*, say, confirm, keep):
        calls.append({"keep": keep, "confirmed": confirm("Apply A to the demo repository?")})
        say("trial ran")
        return trial.TrialOutcome(applied=True, kept=None)

    monkeypatch.setattr(trial, "run_trial", fake_run_trial)
    return calls


def test_start_without_a_terminal_never_waits(monkeypatch, recorded):
    monkeypatch.setattr(cli_module, "_start_is_interactive", lambda: False)

    result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code != 0
    assert "syncmill start --trial" in result.output
    assert "syncmill run --dry-run" in result.output  # the equivalent commands
    assert recorded == []


def test_start_trial_without_a_terminal_applies_in_the_demo_repo(monkeypatch, recorded):
    monkeypatch.setattr(cli_module, "_start_is_interactive", lambda: False)

    result = CliRunner().invoke(cli, ["start", "--trial", "--keep"])

    assert result.exit_code == 0, result.output
    assert recorded == [{"keep": True, "confirmed": True}]
    assert "no terminal to ask" in result.output


def test_start_menu_offers_the_trial_or_the_project_steps(monkeypatch, recorded):
    monkeypatch.setattr(cli_module, "_start_is_interactive", lambda: True)

    project = CliRunner().invoke(cli, ["start"], input="2\n")
    assert project.exit_code == 0, project.output
    assert "1) Try it (no login, about 5 minutes)" in project.output
    assert "syncmill init" in project.output and recorded == []

    tried = CliRunner().invoke(cli, ["start"], input="1\nn\n")
    assert tried.exit_code == 0, tried.output
    assert recorded == [{"keep": False, "confirmed": False}]  # default is No


def test_start_turns_a_trial_error_into_a_clean_message(monkeypatch):
    monkeypatch.setattr(cli_module, "_start_is_interactive", lambda: False)

    def boom(**_):
        raise trial.TrialError("git was not found on PATH; the trial needs it")

    monkeypatch.setattr(trial, "run_trial", boom)
    result = CliRunner().invoke(cli, ["start", "--trial"])

    assert result.exit_code == 1
    assert "Error: git was not found on PATH" in result.output
    assert "Traceback" not in result.output
