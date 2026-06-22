"""Hermetic tests for the tmux live monitor (#16).

NEVER launches real tmux: every tmux call funnels through ``monitor._tmux`` /
``monitor._tmux_attach`` / ``monitor.tmux_available``, which these tests patch.
The one place a subprocess is acceptable (a real teeing child) lives in
test_agents.py; here the FileSink writes to a tmp dir and nothing else escapes.
"""

from __future__ import annotations

import json
import types
import uuid
from pathlib import Path

import pytest

import syncmill.agents as A
from syncmill import monitor as M
from syncmill.config import MonitorConfig
from syncmill.models import GateCheck, GateReport, TaskResult, TaskStatus


def _ok(stdout: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _result(**kw) -> TaskResult:
    base = dict(
        task_id=uuid.uuid4(),
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="done",
        exit_code=0,
        duration_seconds=1.5,
    )
    base.update(kw)
    return TaskResult(**base)


# ── inner_dir: the outer↔inner handshake (env var + sentinel) ──────────────


def test_inner_dir_none_without_env(monkeypatch):
    # clean_syncmill_env already strips SYNCMILL_*; assert the default is None.
    monkeypatch.delenv(M.INNER_ENV, raising=False)
    assert M.inner_dir() is None


def test_inner_dir_none_when_sentinel_missing(monkeypatch, tmp_path):
    # env set but NO sentinel → a stray exported var must NOT mark a normal run inner
    monkeypatch.setenv(M.INNER_ENV, str(tmp_path))
    assert M.inner_dir() is None


def test_inner_dir_requires_both(monkeypatch, tmp_path):
    monkeypatch.setenv(M.INNER_ENV, str(tmp_path))
    (tmp_path / M.SENTINEL_NAME).write_text("")
    assert M.inner_dir() == str(tmp_path)


# ── FileSink: non-blocking append, per-agent logs ─────────────────────────


def test_filesink_appends_per_agent(tmp_path):
    sink = M.FileSink(tmp_path)
    sink.feed("claude", b"hello ")
    sink.feed("claude", b"world")
    sink.feed("codex", b"other")
    sink.close()
    assert (tmp_path / "agent-claude.log").read_bytes() == b"hello world"
    assert (tmp_path / "agent-codex.log").read_bytes() == b"other"


def test_filesink_kimi_code_filename(tmp_path):
    sink = M.FileSink(tmp_path)
    sink.feed("kimi-code", b"x")
    sink.close()
    assert (tmp_path / "agent-kimi-code.log").read_bytes() == b"x"


def test_filesink_bad_dir_never_raises(tmp_path):
    # An unwritable/missing run dir must drop the chunk, never raise (it runs in
    # the agent's drain loop). os.open fails → feed no-ops.
    sink = M.FileSink(tmp_path / "does-not-exist")
    sink.feed("claude", b"x")  # must not raise
    sink.close()


def test_filesink_satisfies_output_sink_protocol(tmp_path):
    assert isinstance(M.FileSink(tmp_path), A.OutputSink)


# ── result.json contract: write / read / render ───────────────────────────


def test_result_json_round_trip(tmp_path):
    r = _result(files_changed=["a.py", "b.py"], output="hi")
    M.write_result(tmp_path, r)
    data = M.read_result(tmp_path)
    assert data["status"] == str(TaskStatus.COMPLETED)
    assert data["agent_id"] == "claude"
    assert data["output"] == "hi"
    assert data["files_changed"] == ["a.py", "b.py"]
    assert data["error"] is None
    assert data["gate_summary"] is None and data["gate_passed"] is None


def test_result_json_flattens_gate(tmp_path):
    report = GateReport.from_checks([GateCheck("pytest", passed=True, exit_code=0)])
    r = _result(gate=report)
    M.write_result(tmp_path, r)
    data = M.read_result(tmp_path)
    # the gate is a flat string + bool, never the GateReport object
    assert isinstance(data["gate_summary"], str)
    assert data["gate_passed"] is True


def test_read_result_missing_is_none(tmp_path):
    assert M.read_result(tmp_path) is None


def test_render_result_completed(capsys):
    code = M.render_result(M._result_to_json(_result(files_changed=["x.py"])))
    out = capsys.readouterr().out
    assert code == 0
    assert "Completed in 1.5s" in out
    assert "Files changed: x.py" in out
    assert "done" in out


def test_render_result_failed(capsys):
    data = M._result_to_json(
        _result(status=TaskStatus.FAILED, output="", error="boom")
    )
    code = M.render_result(data)
    out = capsys.readouterr().out
    assert code == 1
    assert "Failed" in out
    assert "boom" in out


def test_render_task_result_matches_render_result(capsys):
    r = _result(output="z")
    assert M.render_task_result(r) == 0
    out = capsys.readouterr().out
    assert "Completed in 1.5s" in out and "z" in out


# ── build_monitor ─────────────────────────────────────────────────────────


def test_build_monitor_none_without_dir():
    assert M.build_monitor(MonitorConfig(), monitor_dir=None) is None


def test_build_monitor_returns_tmux_monitor(tmp_path):
    mon = M.build_monitor(MonitorConfig(), monitor_dir=str(tmp_path))
    assert isinstance(mon, M.TmuxFileMonitor)
    assert isinstance(mon, M.Monitor)


# ── TmuxFileMonitor (outside tmux: status/log/result, no tmux calls) ───────


def test_monitor_outside_tmux_logs_and_writes_result(monkeypatch, tmp_path):
    monkeypatch.delenv("TMUX", raising=False)
    calls = []
    monkeypatch.setattr(M, "_tmux", lambda *a: calls.append(a) or _ok())
    mon = M.TmuxFileMonitor(tmp_path, MonitorConfig())
    mon.phase("compete")
    with mon.streaming(["claude", "codex"]):
        # the sink is live: a feed lands in the agent log
        A._OUTPUT_SINK.get().feed("claude", b"chunk")
    mon.status("claude", TaskStatus.COMPLETED)
    mon.finish(_result())
    # no tmux interaction when not inside a session
    assert calls == []
    status = (tmp_path / "status.log").read_text()
    assert "phase: compete" in status
    assert "[completed] claude" in status
    assert (tmp_path / "agent-claude.log").read_bytes() == b"chunk"
    assert M.read_result(tmp_path)["agent_id"] == "claude"


def test_monitor_streaming_opens_the_real_sink(monkeypatch, tmp_path):
    monkeypatch.delenv("TMUX", raising=False)
    mon = M.TmuxFileMonitor(tmp_path, MonitorConfig())
    assert A._OUTPUT_SINK.get() is None
    with mon.streaming(["claude"]):
        assert A._OUTPUT_SINK.get() is mon._sink  # the seam is open during the block
    assert A._OUTPUT_SINK.get() is None  # and closed after


# ── TmuxFileMonitor (inside tmux: lazy panes, finish keeps control pane) ───


def test_monitor_inside_tmux_lazy_panes(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1,1,0")
    calls = []

    def fake_tmux(*args):
        calls.append(args)
        if args[:1] == ("split-window",):
            return _ok("%7")  # pane id
        return _ok()

    monkeypatch.setattr(M, "_tmux", fake_tmux)
    mon = M.TmuxFileMonitor(tmp_path, MonitorConfig())
    with mon.streaming(["claude", "codex"]):
        pass
    # one pane created per agent, lazily, the first time it runs
    splits = [c for c in calls if c[:1] == ("split-window",)]
    assert len(splits) == 2
    # re-entering with the same agents must NOT create more panes (dedup)
    with mon.streaming(["claude", "codex"]):
        pass
    splits2 = [c for c in calls if c[:1] == ("split-window",)]
    assert len(splits2) == 2


def test_monitor_finish_inside_tmux_keeps_control_pane(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1,1,0")
    calls = []
    monkeypatch.setattr(M, "_tmux", lambda *a: calls.append(a) or _ok("%1"))
    mon = M.TmuxFileMonitor(tmp_path, MonitorConfig())
    mon.finish(_result())
    flat = [" ".join(a) for a in calls]
    assert any("remain-on-exit on" in f for f in flat)
    assert M.read_result(tmp_path)["agent_id"] == "claude"


# ── launch_monitored_run (OUTER): all tmux patched, no real attach ─────────


class _FakeTmux:
    """Records every control call; captures the run dir from new-session's -e arg."""

    def __init__(self, *, new_session_rc: int = 0):
        self.calls: list[tuple] = []
        self.run_dir: str | None = None
        self._rc = new_session_rc

    def __call__(self, *args):
        self.calls.append(args)
        if args and args[0] == "new-session":
            for i, a in enumerate(args):
                if a == "-e" and args[i + 1].startswith(f"{M.INNER_ENV}="):
                    self.run_dir = args[i + 1].split("=", 1)[1]
            return types.SimpleNamespace(
                returncode=self._rc, stdout="", stderr="boom" if self._rc else ""
            )
        return _ok()

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _patch_outer(monkeypatch, fake_tmux, *, attach):
    monkeypatch.setattr(M, "_tmux", fake_tmux)
    monkeypatch.setattr(M, "_tmux_attach", attach)
    monkeypatch.setattr(M, "_RESULT_GRACE_SECONDS", 0.0)


def test_launch_completed_renders_and_cleans_up(monkeypatch, capsys):
    fake = _FakeTmux()

    def attach(session):  # the inner "finished" — drop a result.json
        Path(fake.run_dir, M._RESULT_FILE).write_text(
            json.dumps(M._result_to_json(_result(output="winner")))
        )

    _patch_outer(monkeypatch, fake, attach=attach)
    code = M.launch_monitored_run(["syncmill", "run", "do it", "-s", "compete"])
    out = capsys.readouterr().out
    assert code == 0
    assert "winner" in out
    # the session was created with -e env + the inner argv passed DIRECTLY
    new_session = next(c for c in fake.calls if c[0] == "new-session")
    assert "-e" in new_session and f"{M.INNER_ENV}={fake.run_dir}" in new_session
    # the inner argv is the tail of the new-session command, passed directly
    assert new_session[-5:] == ("syncmill", "run", "do it", "-s", "compete")
    assert "attach-session" not in fake.names()  # attach went through _tmux_attach
    assert "kill-session" in fake.names()  # completed → torn down
    assert not Path(fake.run_dir).exists()  # run dir cleaned up


def test_launch_detach_early_keeps_session(monkeypatch, capsys):
    fake = _FakeTmux()
    _patch_outer(monkeypatch, fake, attach=lambda session: None)  # no result written
    code = M.launch_monitored_run(["syncmill", "run", "x", "-s", "council"])
    out = capsys.readouterr().out
    assert code == 0
    assert "detached before the run finished" in out
    assert "kill-session" not in fake.names()  # left running
    assert Path(fake.run_dir).exists()  # run dir NOT cleaned (result may still land)


def test_launch_new_session_failure_raises_and_cleans(monkeypatch):
    fake = _FakeTmux(new_session_rc=1)
    _patch_outer(monkeypatch, fake, attach=lambda session: None)
    with pytest.raises(M.MonitorLaunchError):
        M.launch_monitored_run(["syncmill", "run", "x", "-s", "compete"])
    assert fake.run_dir is not None and not Path(fake.run_dir).exists()


def test_launch_passes_inner_argv_without_shell(monkeypatch):
    # The inner argv with shell metacharacters is passed as DISTINCT args — never a
    # shell string (no injection, works under fish/csh).
    fake = _FakeTmux()

    def attach(session):
        Path(fake.run_dir, M._RESULT_FILE).write_text(
            json.dumps(M._result_to_json(_result()))
        )

    _patch_outer(monkeypatch, fake, attach=attach)
    M.launch_monitored_run(["syncmill", "run", "$(echo pwned) `whoami`", "-s", "compete"])
    new_session = next(c for c in fake.calls if c[0] == "new-session")
    assert "$(echo pwned) `whoami`" in new_session  # verbatim, one argv element
