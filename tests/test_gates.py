"""gates.py — CommandGate subprocess handling, GateReport aggregation, and the
build_gate factory. Uses real but harmless commands (true/false/python), never
the agent binaries — hermetic like the rest of the suite."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from syncmill.config import GateConfig
from syncmill.gates import CommandGate, build_gate

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


def test_command_gate_invalid_utf8_output_does_not_crash(tmp_path):
    # a command emitting invalid UTF-8 must degrade to replacement chars, never
    # raise UnicodeDecodeError and abort the gate (errors="replace").
    gate = CommandGate(
        [_py("import sys; sys.stdout.buffer.write(b'\\xff\\xfe\\x00bad'); sys.exit(0)")],
        timeout_seconds=10,
    )
    report = gate.evaluate(_wt(tmp_path))  # must not raise
    assert report.passed is True  # exit 0 → passes despite the garbage bytes


def test_command_gate_does_not_raise_on_bad_workdir():
    # a non-existent cwd must surface as a failed check, never crash the gate, and
    # name the real cause (the workdir) rather than mislabel it "command not found".
    gate = CommandGate(["true"], timeout_seconds=10)
    report = gate.evaluate(_wt(Path("/no/such/dir/anywhere")))
    assert report.passed is False
    assert "working directory not found" in report.checks[0].output
