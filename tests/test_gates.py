"""gates.py — CommandGate subprocess handling, GateReport aggregation, and the
build_gate factory. Uses real but harmless commands (true/false/python), never
the agent binaries — hermetic like the rest of the suite."""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

import pytest

from syncmill.agents import _WORKFLOW_COMMON_ENV, _isolated_process_env
from syncmill.config import GateConfig
from syncmill.gates import CommandGate, _gate_env, build_gate

from conftest import FakeWorktree


def _wt(path: Path) -> FakeWorktree:
    return FakeWorktree(path, is_git=True)


def _py(code: str) -> str:
    """A 'python -c <code>' command string, safely quoted for shlex.split."""
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


# ── build_gate factory ────────────────────────────────────────────────────


def test_build_gate_none_when_no_commands():
    assert build_gate(GateConfig()) is None
    assert build_gate(GateConfig(commands=[])) is None


def test_build_gate_returns_command_gate_when_configured():
    gate = build_gate(GateConfig(commands=["true"]))
    assert isinstance(gate, CommandGate)


# ── CommandGate execution ─────────────────────────────────────────────────


def test_command_gate_all_pass(tmp_path):
    gate = CommandGate(["true", _py("import sys; sys.exit(0)")], timeout_seconds=10)
    report = gate.evaluate(_wt(tmp_path))
    assert report.passed is True
    assert report.score == 1.0
    assert len(report.checks) == 2
    assert all(c.exit_code == 0 for c in report.checks)


def test_command_gate_one_failure_fails_the_gate(tmp_path):
    gate = CommandGate(["true", "false"], timeout_seconds=10)
    report = gate.evaluate(_wt(tmp_path))
    assert report.passed is False
    assert report.score == 0.5
    assert [c.passed for c in report.checks] == [True, False]
    assert report.checks[1].exit_code != 0


def test_command_gate_runs_in_the_worktree(tmp_path):
    # the command's cwd is the worktree — prove it by reading a file only present
    # in tmp_path (so a check can act on the agent's actual diff).
    (tmp_path / "marker.txt").write_text("present\n")
    gate = CommandGate(
        [_py("import os,sys; sys.exit(0 if os.path.exists('marker.txt') else 1)")],
        timeout_seconds=10,
    )
    assert gate.evaluate(_wt(tmp_path)).passed is True


def test_command_gate_captures_output_tail(tmp_path):
    gate = CommandGate([_py("print('gate-says-hello')")], timeout_seconds=10)
    report = gate.evaluate(_wt(tmp_path))
    assert "gate-says-hello" in report.checks[0].output


def test_command_gate_missing_binary_is_a_failed_check(tmp_path):
    gate = CommandGate(["this-binary-does-not-exist-xyz"], timeout_seconds=10)
    report = gate.evaluate(_wt(tmp_path))
    assert report.passed is False
    assert "not found" in report.checks[0].output


def test_command_gate_invalid_command_is_a_failed_check(tmp_path):
    gate = CommandGate(['foo "unbalanced'], timeout_seconds=10)  # bad shlex
    report = gate.evaluate(_wt(tmp_path))
    assert report.passed is False
    check = report.checks[0]
    assert check.passed is False and check.exit_code == -1
    assert "invalid command" in check.output


def test_command_gate_empty_command_is_a_failed_check(tmp_path):
    gate = CommandGate(["   "], timeout_seconds=10)
    report = gate.evaluate(_wt(tmp_path))
    assert report.passed is False
    check = report.checks[0]
    assert check.passed is False and check.exit_code == -1
    assert "empty command" in check.output


def test_command_gate_timeout_is_a_failed_check(tmp_path):
    # short timeout against a long sleep: the timer fires well before the sleep
    # ends, so it reliably times out without adding a full second of wall-clock.
    gate = CommandGate([_py("import time; time.sleep(5)")], timeout_seconds=0.3)
    report = gate.evaluate(_wt(tmp_path))
    assert report.passed is False
    check = report.checks[0]
    assert check.passed is False and check.exit_code == -1
    assert "timed out" in check.output


def test_command_gate_embedded_nul_is_a_failed_check(tmp_path):
    # an embedded NUL passes shlex.split but makes subprocess.run raise ValueError
    # (not OSError) — it must become a failed check, not escape and crash the gate.
    gate = CommandGate(["foo\x00bar"], timeout_seconds=10)
    report = gate.evaluate(_wt(tmp_path))  # must not raise
    assert report.passed is False
    check = report.checks[0]
    assert check.passed is False and check.exit_code == -1
    assert "invalid command argument" in check.output


def test_command_gate_invalid_utf8_output_does_not_crash(tmp_path):
    # a command emitting invalid UTF-8 must degrade to replacement chars, never
    # raise UnicodeDecodeError and abort the gate (errors="replace").
    gate = CommandGate(
        [_py("import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\x00bad'); sys.exit(0)")],
        timeout_seconds=10,
    )
    report = gate.evaluate(_wt(tmp_path))  # must not raise
    assert report.passed is True  # exit 0 → passes despite the garbage bytes


def test_command_gate_strips_ambient_git_routing(tmp_path, monkeypatch):
    # A gate check reads the repository, so an ambient GIT_DIR would have it judge
    # a different tree than the worktree under test, and GIT_CONFIG_PARAMETERS
    # could execute a hook inside an agent-writable worktree. Both must be gone.
    monkeypatch.setenv("GIT_DIR", "/tmp/somewhere-else/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/tmp/somewhere-else")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/tmp/evil'")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    # A gate command that invokes syncmill must not adopt the parent run's
    # monitor identity and overwrite its envelope/result.json.
    monkeypatch.setenv("SYNCMILL_MONITOR_DIR", "/tmp/syncmill-monitor-abc")
    monkeypatch.setenv("SYNCMILL_RUN_ID", "parent-run-id")
    monkeypatch.setenv("SYNCMILL_GATE_KEEPS_THIS", "yes")
    dump = tmp_path / "env.json"
    gate = CommandGate(
        [
            _py(
                "import json,os,sys; "
                f"open({str(dump)!r},'w').write(json.dumps(dict(os.environ)))"
            )
        ],
        timeout_seconds=30,
    )
    assert gate.evaluate(_wt(tmp_path)).passed is True
    env = json.loads(dump.read_text())
    assert not {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "SYNCMILL_MONITOR_DIR",
        "SYNCMILL_RUN_ID",
    } & env.keys()
    # Isolation strips routing, not the rest of the environment.
    assert env["SYNCMILL_GATE_KEEPS_THIS"] == "yes"
    assert env["PWD"] == str(tmp_path.resolve())


def _env_seen_by(gate: CommandGate, tmp_path: Path) -> dict[str, str]:
    dump = tmp_path / "env.json"
    gate._commands = [
        _py(
            "import json,os; "
            f"open({str(dump)!r},'w').write(json.dumps(dict(os.environ)))"
        )
    ]
    assert gate.evaluate(_wt(tmp_path)).passed is True
    return json.loads(dump.read_text())


def _secrets(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("UNLISTED_SECRET", "s3cret")
    monkeypatch.setenv("UV_INDEX_URL", "https://index.example/simple")


def test_an_unset_gate_allowlist_keeps_the_isolated_environment(tmp_path, monkeypatch):
    _secrets(monkeypatch)
    assert _gate_env(tmp_path) == _isolated_process_env(tmp_path)
    env = _env_seen_by(CommandGate([], timeout_seconds=30), tmp_path)
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"
    assert env["UNLISTED_SECRET"] == "s3cret"


@pytest.mark.parametrize("allowlist", [[], ["UV_INDEX_URL"]])
def test_a_gate_allowlist_keeps_only_common_and_listed_names(
    tmp_path, monkeypatch, allowlist
):
    _secrets(monkeypatch)
    # The exact set is checked on what the gate passes: a child process can
    # gain variables of its own (macOS adds __CF_USER_TEXT_ENCODING).
    assert set(_gate_env(tmp_path, allowlist)) <= (
        _WORKFLOW_COMMON_ENV | set(allowlist) | {"PWD"}
    )
    gate = build_gate(GateConfig(commands=["true"], env_allowlist=allowlist))
    env = _env_seen_by(gate, tmp_path)
    assert env["PATH"] == os.environ["PATH"]
    assert env["PWD"] == str(tmp_path.resolve())
    assert not {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "UNLISTED_SECRET"} & env.keys()
    assert ("UV_INDEX_URL" in env) == bool(allowlist)


def test_a_gate_allowlist_cannot_bring_back_what_isolation_strips(
    tmp_path, monkeypatch
):
    stripped = {
        "GIT_DIR": "/tmp/elsewhere/.git",
        "GIT_CONFIG_PARAMETERS": "'core.hooksPath=/tmp/evil'",
        "SYNCMILL_MONITOR_DIR": "/tmp/syncmill-monitor-abc",
        "SYNCMILL_RUN_ID": "parent-run-id",
        "SYNCMILL_TUI_JOB_ID": "job-1",
        "OLDPWD": "/tmp/elsewhere",
        "CDPATH": "/tmp/elsewhere",
    }
    for name, value in stripped.items():
        monkeypatch.setenv(name, value)
    gate = CommandGate([], timeout_seconds=30, env_allowlist=list(stripped))
    env = _env_seen_by(gate, tmp_path)
    assert not set(stripped) & env.keys()


def test_command_gate_does_not_raise_on_bad_workdir():
    # a non-existent cwd must surface as a failed check, never crash the gate, and
    # name the real cause (the workdir) rather than mislabel it "command not found".
    gate = CommandGate(["true"], timeout_seconds=10)
    report = gate.evaluate(_wt(Path("/no/such/dir/anywhere")))
    assert report.passed is False
    assert "working directory not found" in report.checks[0].output


def test_command_gate_timeout_kills_the_commands_descendants(tmp_path):
    # A gate command is typically a test runner that spawns workers; a bare
    # subprocess.run timeout killed only the direct child, orphaning them
    # (codex round-7, reproduced). The gate routes through the owned-group
    # runner, so the whole tree dies with the timeout.
    import os
    import signal
    import time

    if os.name != "posix":
        import pytest

        pytest.skip("process groups are POSIX")
    pidfile = tmp_path / "gpid"
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(600)\n"
    )
    gate = CommandGate([_py(code)], timeout_seconds=5)
    report = gate.evaluate(_wt(tmp_path))
    assert report.passed is False
    assert "timed out" in report.checks[0].output
    gpid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            return  # the worker died with the group
        time.sleep(0.05)
    os.kill(gpid, signal.SIGKILL)  # cleanup before failing
    import pytest

    pytest.fail("gate command's descendant survived the timeout")
