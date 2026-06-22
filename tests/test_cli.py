"""cli.py — run / agents / status via CliRunner with fakes (no real subprocess,
no real memtomem)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from syncmill.cli import cli
from syncmill.matcher import build_matcher
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
            # mirror the real Supervisor: the CLI banner re-orders through this
            # same matcher, so the fake must expose it (None by default).
            self.matcher = build_matcher(config.capabilities, config.agent_priority)
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
    for cmd in ("run", "agents", "status", "serve", "session"):
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


# ── serve (read-only MCP gateway) ──────────────────────────────────────────


def _install_serve(monkeypatch, *, degraded=False):
    """Stub the gateway + memory so `serve` never starts a real server or memtomem.

    Records the host/port build_gateway was called with (proving construction-time
    host, per the DNS-rebinding fix) and the transport mcp.run received.
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
        def __init__(self, host, port):
            self.settings = types.SimpleNamespace(host=host, port=port)

        def run(self, transport="stdio"):
            holder["transport"] = transport

    def fake_build(memory, *, host="127.0.0.1", port=8000):
        holder["build"] = {"host": host, "port": port}
        return FakeServer(host, port)

    monkeypatch.setattr("syncmill.memory.SyncmillMemory", FakeMem)
    monkeypatch.setattr("syncmill.gateway.build_gateway", fake_build)
    return holder


def test_serve_stdio_default(monkeypatch, runner):
    h = _install_serve(monkeypatch)
    res = runner.invoke(cli, ["serve"])
    assert res.exit_code == 0
    assert h["transport"] == "stdio"
    assert h["build"]["host"] == "127.0.0.1"
    assert h["closed"] is True  # memory closed in finally
    # stdio uses stdout as the JSON-RPC channel — notices go to stderr, stdout empty
    assert res.stdout == ""
    assert "stdio" in res.stderr


def test_serve_http_passes_host_port_at_construction(monkeypatch, runner):
    h = _install_serve(monkeypatch)
    res = runner.invoke(
        cli, ["serve", "--http", "--host", "0.0.0.0", "--port", "8765"]
    )
    assert res.exit_code == 0
    assert h["transport"] == "streamable-http"
    # host/port reached build_gateway (construction), not a post-hoc settings mutation
    assert h["build"] == {"host": "0.0.0.0", "port": 8765}
    assert "WARNING" in res.stderr  # non-loopback bind warns
    assert res.stdout == ""


def test_serve_http_loopback_security_note(monkeypatch, runner):
    h = _install_serve(monkeypatch)
    res = runner.invoke(cli, ["serve", "--http"])
    assert res.exit_code == 0
    assert h["transport"] == "streamable-http"
    assert "no auth" in res.stderr.lower()


def test_serve_surfaces_degraded_memory_but_still_starts(monkeypatch, runner):
    h = _install_serve(monkeypatch, degraded=True)
    res = runner.invoke(cli, ["serve"])
    assert res.exit_code == 0
    assert "unavailable" in res.stderr.lower()
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

    def fake_launch(inner_argv, *, session_prefix="syncmill"):
        holder["launch"] = {"argv": inner_argv, "prefix": session_prefix}
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
