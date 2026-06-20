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


def _install_supervisor(
    monkeypatch, *, result, agents, degraded=False, breaker_degraded=False
):
    holder: dict = {"cleanup": []}

    class Sup:
        def __init__(self, config):
            holder["config"] = config
            self.memory = types.SimpleNamespace(degraded=degraded, last_error="boom")
            # breaker is None unless the test wants to exercise the degraded notice
            self.breaker = (
                types.SimpleNamespace(degraded=True, last_error="state unreadable")
                if breaker_degraded
                else None
            )
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
