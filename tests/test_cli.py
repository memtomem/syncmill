"""cli.py — run / agents / status via CliRunner with fakes (no real subprocess,
no real memtomem)."""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from syncmill.board import BoardError
from syncmill.cli import cli
from syncmill.matcher import build_matcher
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.report import load_envelope, render_report

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
    report_path=None, preflight_warnings=None,
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

    def fake_build(memory, *, board=None, board_error=None, host="127.0.0.1", port=8000):
        holder["build"] = {
            "host": host, "port": port, "board": board, "board_error": board_error,
        }
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
    assert h["build"]["host"] == "0.0.0.0"
    assert h["build"]["port"] == 8765
    assert h["build"]["board"] is None  # board disabled by default → board tools report disabled
    assert "WARNING" in res.stderr  # non-loopback bind warns
    assert res.stdout == ""


def test_serve_http_loopback_security_note(monkeypatch, runner):
    h = _install_serve(monkeypatch)
    res = runner.invoke(cli, ["serve", "--http"])
    assert res.exit_code == 0
    assert h["transport"] == "streamable-http"
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
    preflight_blocked=False,
):
    """Patch cli.Supervisor with a fake whose run_item yields ``results`` in order
    (a list; None entries model 'nothing to run' / loop drain). The fake exposes
    registry/memory/breaker so board_run's --agent validation + degraded notices
    can be exercised."""
    holder: dict = {"run_item_calls": [], "cleanup": []}
    seq = list(results)
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
            holder["sup"] = self

        def run_item(self, item_id=None, agent_id=None, exclude=None, goal=None):
            holder["run_item_calls"].append(
                {"item_id": item_id, "agent_id": agent_id, "goal": goal}
            )
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
    assert "[governance] Review Tracegraph tool-retry-failure@v1" in listing.output
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
