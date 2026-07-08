"""Hermetic tests for the tmux live monitor (#16).

NEVER launches real tmux: every tmux call funnels through ``monitor._tmux`` /
``monitor._tmux_attach`` / ``monitor.tmux_available``, which these tests patch.
Real subprocesses are confined to two controlled, tmux-free children: the teeing
child (test_agents.py) and the ``_ENV_SHIM`` env-isolation round-trip below (a
plain ``python -c`` that dumps its env). Everything else writes to a tmp dir.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


def test_result_json_carries_patch(tmp_path):
    # the OUTER honors --emit-patch from result.json, so the patch must survive
    # the write/read round-trip — including surrogateescape'd non-UTF-8 bytes
    # (json escapes lone surrogates losslessly).
    raw = b"diff --git a/f b/f\n+caf\xe9\n"
    patch = raw.decode("utf-8", errors="surrogateescape")
    M.write_result(tmp_path, _result(patch=patch))
    data = M.read_result(tmp_path)
    assert data["patch"] == patch
    assert data["patch"].encode("utf-8", errors="surrogateescape") == raw


def test_result_json_patch_default_none(tmp_path):
    M.write_result(tmp_path, _result())
    assert M.read_result(tmp_path)["patch"] is None


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


# ── emit_patch_file (the --emit-patch recovery path) ────────────────────────


def test_emit_patch_writes_and_hints_apply(tmp_path, capsys):
    dest = tmp_path / "win.patch"
    ok = M.emit_patch_file(dest, M._result_to_json(_result(patch="diff --git x")))
    out = capsys.readouterr().out
    assert ok is True
    assert dest.read_text() == "diff --git x"
    assert f"Patch written: {dest}" in out and "git apply" in out


def test_emit_patch_nothing_to_emit(tmp_path, capsys):
    # "" (clean worktree) and None (non-git / collection failed) both
    # notice-and-skip — no file is created, and it's not a failure.
    dest = tmp_path / "win.patch"
    assert M.emit_patch_file(dest, M._result_to_json(_result(patch=""))) is True
    assert M.emit_patch_file(dest, M._result_to_json(_result(patch=None))) is True
    out = capsys.readouterr().out
    assert out.count("no diff to emit") == 2
    assert not dest.exists()


def test_emit_patch_failed_run_writes_nothing(tmp_path, capsys):
    # a FAILED result never exports — even if a stale patch string is attached
    dest = tmp_path / "win.patch"
    data = M._result_to_json(_result(status=TaskStatus.FAILED, patch="stale diff"))
    assert M.emit_patch_file(dest, data) is True
    assert "did not complete" in capsys.readouterr().out
    assert not dest.exists()


def test_emit_patch_write_failure_returns_false(tmp_path, capsys):
    dest = tmp_path / "no_such_dir" / "win.patch"  # missing parent → OSError
    ok = M.emit_patch_file(dest, M._result_to_json(_result(patch="d")))
    assert ok is False
    assert "failed to write" in capsys.readouterr().out


def test_emit_patch_round_trips_non_utf8_bytes(tmp_path):
    # the writer encodes with surrogateescape, so _git_diff_patch's byte-exact
    # decode lands on disk byte-exact — `git apply` sees the original bytes.
    raw = b"+caf\xe9\n"
    patch = raw.decode("utf-8", errors="surrogateescape")
    dest = tmp_path / "win.patch"
    assert M.emit_patch_file(dest, M._result_to_json(_result(patch=patch))) is True
    assert dest.read_bytes() == raw


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


def test_finish_result_write_failure_is_swallowed(monkeypatch, tmp_path):
    # A result.json write failure (here: a vanished run dir) must NOT raise out of
    # finish() — else it would escape the Supervisor's finally and strand cleanup.
    monkeypatch.delenv("TMUX", raising=False)
    missing = tmp_path / "gone"  # never created → write_result hits FileNotFoundError
    mon = M.TmuxFileMonitor(missing, MonitorConfig())
    mon.finish(_result())  # must not raise
    assert M.read_result(missing) is None  # nothing written, but cleanly handled


# ── launch_monitored_run (OUTER): all tmux patched, no real attach ─────────


def _find_run_dir(args) -> str | None:
    """The new-session arg that is a real dir holding the inner sentinel (the shim's
    ``run_dir``). Probes each arg defensively: ``is_dir()`` raises ``ENAMETOOLONG`` on
    the long ``-c`` shim string (> the OS filename limit), which is NOT a run dir."""
    for a in args:
        try:
            p = Path(str(a))
            if p.is_dir() and (p / M.SENTINEL_NAME).exists():
                return str(a)
        except OSError:
            continue
    return None


class _FakeTmux:
    """Records every control call; captures the run dir as the new-session arg that
    is a real directory holding the inner sentinel (the shim's run_dir argument)."""

    def __init__(self, *, new_session_rc: int = 0):
        self.calls: list[tuple] = []
        self.run_dir: str | None = None
        self._rc = new_session_rc

    def __call__(self, *args):
        self.calls.append(args)
        if args and args[0] == "new-session":
            self.run_dir = _find_run_dir(args) or self.run_dir
            return types.SimpleNamespace(
                returncode=self._rc, stdout="", stderr="boom" if self._rc else ""
            )
        return _ok()

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def _patch_outer(monkeypatch, fake_tmux, *, attach):
    # the real _tmux_attach returns an int rc; wrap a void-returning fake to 0
    def _attach(session):
        rc = attach(session)
        return rc if isinstance(rc, int) else 0

    monkeypatch.setattr(M, "_tmux", fake_tmux)
    monkeypatch.setattr(M, "_tmux_attach", _attach)
    monkeypatch.setattr(M, "_RESULT_GRACE_SECONDS", 0.0)
    # keep the launch path from sweeping the real /tmp (covered separately below)
    monkeypatch.setattr(M, "_sweep_stale_run_dirs", lambda: None)


def test_sweep_removes_only_stale_run_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(M.tempfile, "gettempdir", lambda: str(tmp_path))
    fresh = tmp_path / "syncmill-monitor-fresh"
    stale = tmp_path / "syncmill-monitor-stale"
    no_sentinel = tmp_path / "syncmill-monitor-nosentinel"
    other = tmp_path / "unrelated-dir"
    for d in (fresh, stale, no_sentinel, other):
        d.mkdir()
    # A real run dir carries the inner sentinel; write it BEFORE backdating mtime
    # (creating the file would otherwise bump the dir's mtime).
    (fresh / M.SENTINEL_NAME).write_text("")
    (stale / M.SENTINEL_NAME).write_text("")
    old = time.time() - (M._STALE_RUN_DIR_SECONDS + 60)
    os.utime(stale, (old, old))
    os.utime(no_sentinel, (old, old))  # old + prefix-match but NO sentinel → not ours
    os.utime(other, (old, old))  # old but not ours → must be left alone
    M._sweep_stale_run_dirs()
    assert fresh.exists()  # recent → kept (a live detached run may still need it)
    assert other.exists()  # not a syncmill-monitor-* dir → never touched
    assert no_sentinel.exists()  # prefix match without the sentinel → never touched
    assert not stale.exists()  # old + ours (sentinel present) → swept


def test_env_snapshot_forwards_outer_env_without_tmux_owned(monkeypatch, tmp_path):
    # The outer env (PATH so a venv `syncmill`/agents resolve, user SYNCMILL_*
    # config) is written to a 0600 FILE — never argv (no secret exposure) — and the
    # tmux-owned terminal vars (TERM/TMUX/TMUX_PANE) are NOT forwarded.
    monkeypatch.setenv("PATH", "/venv/bin:/usr/bin")
    monkeypatch.setenv("SYNCMILL_GATE__COMMANDS", '["pytest"]')
    monkeypatch.setenv("MY_SECRET", "shh")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TMUX", "/should/not/forward")
    monkeypatch.delenv(M.INNER_ENV, raising=False)
    M._write_env_snapshot(tmp_path)
    env_file = tmp_path / "env"
    snap = json.loads(env_file.read_text())
    assert snap["PATH"] == "/venv/bin:/usr/bin"  # forwarded → command resolves
    assert snap["SYNCMILL_GATE__COMMANDS"] == '["pytest"]'  # user config forwarded
    assert snap["MY_SECRET"] == "shh"  # secret carried — but in the file, not argv
    assert snap[M.INNER_ENV] == str(tmp_path)  # the self-detection handshake
    assert "TERM" not in snap and "TMUX" not in snap  # tmux owns these
    # 0600 so the secret-bearing snapshot is owner-only
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"


def test_env_shim_replaces_stale_server_env(tmp_path):
    # The exec shim must REPLACE the pane's inherited (possibly stale tmux-server)
    # env with the outer snapshot, preserving only the tmux-owned pane vars — an
    # additive update would leak a stale SYNCMILL_*/auth var the outer client lacks.
    # Driven through a real (tmux-free) python child: the shim execs a python that
    # dumps its resulting environment, exactly as production execs the inner argv.
    snap = {"SYNCMILL_KEEP": "from-outer", "PATH": "/snapshot/bin"}
    (tmp_path / "env").write_text(json.dumps(snap))
    polluted = dict(os.environ)  # the stale server env the pane would inherit
    # All three tmux-owned pane vars must survive the swap — the inner needs the
    # PANE's $TMUX/$TMUX_PANE to drive panes from inside the session.
    polluted["TERM"] = "xterm-pane"
    polluted["TMUX"] = "/tmp/tmux-server,1,0"
    polluted["TMUX_PANE"] = "%9"
    polluted["SYNCMILL_STALE"] = "leaked"  # stale config absent from the snapshot
    polluted["AUTH_TOKEN_STALE"] = "leaked"  # stale auth absent from the snapshot
    dump = "import os,json,sys; sys.stdout.write(json.dumps(dict(os.environ)))"
    proc = subprocess.run(
        [sys.executable, "-c", M._ENV_SHIM, str(tmp_path), sys.executable, "-c", dump],
        capture_output=True,
        text=True,
        env=polluted,
    )
    assert proc.returncode == 0, proc.stderr
    env = json.loads(proc.stdout)
    assert env["SYNCMILL_KEEP"] == "from-outer"  # the snapshot was applied
    assert env["PATH"] == "/snapshot/bin"  # snapshot wins — NOT the stale server PATH
    assert env["TERM"] == "xterm-pane"  # tmux-owned pane vars preserved across the swap
    assert env["TMUX"] == "/tmp/tmux-server,1,0"  # ← the inner drives panes via this
    assert env["TMUX_PANE"] == "%9"
    assert "SYNCMILL_STALE" not in env  # stale server config CLEARED, not leaked through
    assert "AUTH_TOKEN_STALE" not in env  # stale auth CLEARED (proves clear(), not update)


def test_env_shim_keep_set_matches_snapshot_skip():
    # The shim runs in a separate `python -c` interpreter, so it can't import
    # _SNAPSHOT_SKIP_ENV — its keep-tuple is a hand-copy. Guard the lockstep: every
    # var the snapshot OMITS must be re-kept by the shim, else it's lost from the
    # inner env entirely (neither snapshotted nor inherited).
    for var in M._SNAPSHOT_SKIP_ENV:
        assert var in M._ENV_SHIM, f"{var} skipped from snapshot but not kept by shim"


def test_tmux_attach_keeps_term_strips_nesting(monkeypatch):
    # The attach CLIENT needs the caller's TERM to render the real terminal — only
    # the tmux-nesting vars are stripped (so attach works from inside tmux too).
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TMUX", "/tmp/server,1,0")
    monkeypatch.setenv("TMUX_PANE", "%3")
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["env"] = kw.get("env")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(M.subprocess, "run", fake_run)
    rc = M._tmux_attach("sess")
    assert rc == 0
    assert captured["argv"] == ["tmux", "attach-session", "-t", "sess"]
    assert captured["env"]["TERM"] == "xterm-256color"  # KEPT — needed to render
    assert "TMUX" not in captured["env"]  # nesting var stripped
    assert "TMUX_PANE" not in captured["env"]


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
    new_session = next(c for c in fake.calls if c[0] == "new-session")
    # NO -e env in argv (secrets stay in the 0600 file); the pane runs the shim:
    # <python> -c <shim> <run_dir> <inner argv…>, with inner argv as the tail.
    assert "-e" not in new_session
    assert sys.executable in new_session and "-c" in new_session
    assert new_session[-5:] == ("syncmill", "run", "do it", "-s", "compete")
    assert "attach-session" not in fake.names()  # attach went through _tmux_attach
    assert "kill-session" in fake.names()  # completed → torn down
    assert not Path(fake.run_dir).exists()  # run dir cleaned up


def test_launch_completed_honors_emit_patch(monkeypatch, capsys, tmp_path):
    # the OUTER writes --emit-patch from result.json's patch field — the flag
    # never rides the inner argv (see the CLI's _inner_argv omission).
    fake = _FakeTmux()

    def attach(session):
        Path(fake.run_dir, M._RESULT_FILE).write_text(
            json.dumps(M._result_to_json(_result(patch="diff --git x")))
        )

    _patch_outer(monkeypatch, fake, attach=attach)
    dest = tmp_path / "win.patch"
    code = M.launch_monitored_run(
        ["syncmill", "run", "do", "-s", "compete"], emit_patch=str(dest)
    )
    assert code == 0
    assert dest.read_text() == "diff --git x"
    assert f"Patch written: {dest}" in capsys.readouterr().out


def test_launch_emit_patch_write_failure_exits_nonzero(monkeypatch, capsys, tmp_path):
    # an explicit export the OUTER can't honor must not exit 0
    fake = _FakeTmux()

    def attach(session):
        Path(fake.run_dir, M._RESULT_FILE).write_text(
            json.dumps(M._result_to_json(_result(patch="d")))
        )

    _patch_outer(monkeypatch, fake, attach=attach)
    dest = tmp_path / "no_such_dir" / "win.patch"
    code = M.launch_monitored_run(
        ["syncmill", "run", "do", "-s", "compete"], emit_patch=str(dest)
    )
    assert code == 1
    assert "failed to write" in capsys.readouterr().out


def test_launch_detach_early_keeps_session(monkeypatch, capsys):
    fake = _FakeTmux()
    _patch_outer(monkeypatch, fake, attach=lambda session: None)  # clean detach (rc 0)
    code = M.launch_monitored_run(["syncmill", "run", "x", "-s", "council"])
    out = capsys.readouterr().out
    assert code == 0
    assert "detached before the run finished" in out
    assert "kill-session" not in fake.names()  # left running
    assert Path(fake.run_dir).exists()  # run dir NOT cleaned (result may still land)


def test_launch_detach_early_emit_patch_hints_extraction(monkeypatch, capsys, tmp_path):
    # no result yet → nothing to export; point at result.json's patch field. The
    # hinted extractor must be byte-preserving — `jq -r` would replace the
    # JSON-escaped surrogateescape bytes of a non-UTF-8 patch with U+FFFD
    # (codex-gate Major), so the hint mirrors emit_patch_file's write instead.
    fake = _FakeTmux()
    _patch_outer(monkeypatch, fake, attach=lambda session: None)
    dest = tmp_path / "win.patch"
    code = M.launch_monitored_run(
        ["syncmill", "run", "x", "-s", "council"], emit_patch=str(dest)
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "--emit-patch: no result yet" in out
    assert "jq" not in out and "surrogateescape" in out
    assert not dest.exists()

    # Prove the PRINTED command round-trips non-UTF-8 bytes: drop a result.json
    # with a raw \xe9 patch, run the hinted extractor verbatim (a controlled,
    # tmux-free child like the shim tests), and compare bytes on disk.
    raw = b"+caf\xe9\n"
    M.write_result(
        fake.run_dir, _result(patch=raw.decode("utf-8", errors="surrogateescape"))
    )
    hint_line = next(l for l in out.splitlines() if "no result yet" in l)
    cmd = hint_line.split("extract with: ", 1)[1]
    subprocess.run(cmd, shell=True, check=True)
    assert dest.read_bytes() == raw


def test_launch_attach_failure_is_surfaced_not_silent(monkeypatch, capsys):
    # A failed attach (e.g. invoked from inside tmux) with no result.json must be
    # surfaced + return non-zero — NOT mistaken for a clean "detached, exit 0".
    fake = _FakeTmux()
    _patch_outer(monkeypatch, fake, attach=lambda session: 1)  # tmux attach failed
    code = M.launch_monitored_run(["syncmill", "run", "x", "-s", "compete"])
    out = capsys.readouterr().out
    assert code == 1
    assert "could not attach to the live monitor" in out
    assert "kill-session" not in fake.names()  # the run may still be going


def test_launch_new_session_failure_raises_and_cleans(monkeypatch):
    fake = _FakeTmux(new_session_rc=1)
    _patch_outer(monkeypatch, fake, attach=lambda session: None)
    with pytest.raises(M.MonitorLaunchError):
        M.launch_monitored_run(["syncmill", "run", "x", "-s", "compete"])
    assert fake.run_dir is not None and not Path(fake.run_dir).exists()


def test_launch_new_session_oserror_falls_back(monkeypatch):
    # An exec/arg-limit OSError from new-session must become MonitorLaunchError so
    # the CLI falls back to a normal run (and the run dir is cleaned).
    seen = {}

    def boom(*args):
        if args and args[0] == "new-session":
            seen["dir"] = _find_run_dir(args)
            raise OSError("Argument list too long")
        return _ok()

    monkeypatch.setattr(M, "_tmux", boom)
    monkeypatch.setattr(M, "_tmux_attach", lambda s: 0)
    monkeypatch.setattr(M, "_sweep_stale_run_dirs", lambda: None)
    with pytest.raises(M.MonitorLaunchError):
        M.launch_monitored_run(["syncmill", "run", "x", "-s", "compete"])
    assert not Path(seen["dir"]).exists()  # cleaned up on the fallback path


def test_launch_setup_failure_falls_back(monkeypatch):
    # A setup failure before launch (e.g. a non-UTF8 env var → ValueError from the
    # snapshot write, or disk-full → OSError) must become MonitorLaunchError so the
    # CLI falls back to a normal run, never crashing the task.
    fake = _FakeTmux()
    monkeypatch.setattr(M, "_tmux", fake)
    monkeypatch.setattr(M, "_tmux_attach", lambda s: 0)
    monkeypatch.setattr(M, "_sweep_stale_run_dirs", lambda: None)

    def boom(run_dir):
        raise ValueError("bad env encoding")

    monkeypatch.setattr(M, "_write_env_snapshot", boom)
    with pytest.raises(M.MonitorLaunchError):
        M.launch_monitored_run(["syncmill", "run", "x", "-s", "compete"])
    assert "new-session" not in fake.names()  # never reached the launch


def test_launch_mkdtemp_oserror_falls_back(monkeypatch):
    # mkdtemp itself can fail (missing/unwritable TMPDIR) BEFORE the run dir exists —
    # and it sits OUTSIDE the setup/launch try (whose except rmtrees run_dir, which
    # doesn't exist yet here). A narrow miss would let the OSError escape un-wrapped
    # and CRASH `syncmill run` instead of falling back to a normal unmonitored run.
    # MUTATION GUARD: removing the mkdtemp try/except lets the bare OSError escape
    # (not a MonitorLaunchError) → this test fails.
    fake = _FakeTmux()
    monkeypatch.setattr(M, "_tmux", fake)
    monkeypatch.setattr(M, "_tmux_attach", lambda s: 0)
    monkeypatch.setattr(M, "_sweep_stale_run_dirs", lambda: None)
    monkeypatch.setattr(
        M.tempfile, "mkdtemp",
        lambda *a, **k: (_ for _ in ()).throw(OSError("No usable temporary directory")),
    )
    with pytest.raises(M.MonitorLaunchError):
        M.launch_monitored_run(["syncmill", "run", "x", "-s", "compete"])
    assert "new-session" not in fake.names()  # never reached the launch


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
