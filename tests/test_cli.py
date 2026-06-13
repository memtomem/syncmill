"""cli.py — run / agents / status via CliRunner with fakes (no real subprocess,
no real memtomem)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from syncmill.cli import cli
from syncmill.models import Task, TaskResult, TaskStatus

from conftest import FakeAgent, FakeRegistry


@pytest.fixture
def runner():
    return CliRunner()


def _result(status=TaskStatus.COMPLETED, agent_id="claude", error=None, files=None):
    r = TaskResult(
        task_id=Task(prompt="p").id,
        agent_id=agent_id,
        status=status,
        output="the output",
        exit_code=0 if status is TaskStatus.COMPLETED else 1,
        duration_seconds=0.5,
        error=error,
    )
    if files:
        r.files_changed = files
    return r


def _install_supervisor(monkeypatch, *, result, agents, degraded=False):
    holder: dict = {"cleanup": []}

    class Sup:
        def __init__(self, config):
            holder["config"] = config
            self.memory = types.SimpleNamespace(degraded=degraded, last_error="boom")
            self.registry = FakeRegistry(agents)
            self.closed = False
            holder["sup"] = self

        def run(self, task, agent_id=None, exclude=None):
            holder["run"] = {"task": task, "agent_id": agent_id, "exclude": exclude}
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
    for cmd in ("run", "agents", "status"):
        assert cmd in res.output


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
        result=_result(agent_id="gemini"),
        agents=[FakeAgent("claude"), FakeAgent("gemini"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["run", "do", "--exclude", "claude"])
    assert res.exit_code == 0
    assert h["run"]["exclude"] == {"claude"}
    assert "gemini → codex" in res.output
    assert "excluded: claude" in res.output


def test_run_exclude_unknown_agent_errors(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "--exclude", "clade"])
    assert res.exit_code != 0
    assert "unknown agent" in res.output.lower()


def test_run_exclude_conflicts_with_forced_agent(monkeypatch, runner):
    _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    res = runner.invoke(cli, ["run", "do", "-a", "claude", "-x", "claude"])
    assert res.exit_code != 0
    assert "both forced" in res.output.lower()


def test_run_timeout_override(monkeypatch, runner):
    h = _install_supervisor(monkeypatch, result=_result(), agents=[FakeAgent("claude")])
    runner.invoke(cli, ["run", "do", "--timeout", "99"])
    cfg = h["config"]
    assert cfg.claude.timeout_seconds == 99
    assert cfg.gemini.timeout_seconds == 99
    assert cfg.codex.timeout_seconds == 99


def test_run_warns_on_degraded_memory(monkeypatch, runner):
    _install_supervisor(
        monkeypatch, result=_result(), agents=[FakeAgent("claude")], degraded=True
    )
    res = runner.invoke(cli, ["run", "do"])
    assert "memory unavailable" in res.output


# ── agents ───────────────────────────────────────────────────────────────


def test_agents_lists_enabled_and_disabled(monkeypatch, runner):
    # registry only knows claude+codex; gemini (in default priority) shows disabled
    _install_supervisor(
        monkeypatch,
        result=_result(),
        agents=[FakeAgent("claude"), FakeAgent("codex")],
    )
    res = runner.invoke(cli, ["agents"])
    assert res.exit_code == 0
    assert "claude" in res.output and "[enabled]" in res.output
    assert "gemini" in res.output and "[disabled]" in res.output


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
