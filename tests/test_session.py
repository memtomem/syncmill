"""Hermetic tests for durable interactive tmux sessions (#17).

NEVER launches real tmux: every tmux call funnels through ``session._tmux`` /
``session._tmux_attach``, which these tests patch (at the SESSION lookup site —
``session._tmux``, not ``monitor._tmux`` — because ``session.py`` imports those
names, rebinding them into its own globals; the documented ``strategies.base``
``_git_changed_files`` lesson). The only real subprocess is the env-shim
round-trip below: a plain ``python -c`` that dumps its env (no tmux).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from syncmill import monitor as M
from syncmill import session as S


def _ns(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _find_run_dir(args) -> str | None:
    """The new-session arg that is a real dir holding the session sentinel (the
    shim's ``run_dir``). Probes defensively: ``is_dir()`` raises ``ENAMETOOLONG`` on
    the long ``-c`` shim string, which is not a run dir."""
    for a in args:
        try:
            p = Path(str(a))
            if p.is_dir() and (p / S._SESSION_SENTINEL).exists():
                return str(a)
        except OSError:
            continue
    return None


class _FakeTmux:
    """Records every tmux control call and models session existence: ``new-session``
    adds the ``-s`` name to ``existing`` (rc 0) and captures its run dir;
    ``has-session`` reflects ``existing``; ``list-sessions`` renders ``existing`` with
    per-name attached counts; ``kill-session`` removes from ``existing``."""

    def __init__(self, *, existing=None, new_session_rc: int = 0, attached=None):
        self.calls: list[tuple] = []
        self.existing: set[str] = set(existing or [])
        self.new_session_rc = new_session_rc
        self.attached: dict[str, int] = attached or {}
        self.run_dir: str | None = None

    def __call__(self, *args):
        self.calls.append(args)
        cmd = args[0]
        if cmd == "has-session":
            name = args[args.index("-t") + 1]
            return _ns(0 if name in self.existing else 1)
        if cmd == "new-session":
            self.run_dir = _find_run_dir(args)
            if self.new_session_rc == 0:
                self.existing.add(args[args.index("-s") + 1])
                return _ns(0)
            return _ns(self.new_session_rc, stderr="boom")
        if cmd == "list-sessions":
            lines = "\n".join(
                f"{n} {self.attached.get(n, 0)}" for n in sorted(self.existing)
            )
            return _ns(0, stdout=lines)
        if cmd == "kill-session":
            self.existing.discard(args[args.index("-t") + 1])
            return _ns(0)
        return _ns(0)  # set-option, select-pane, …

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


# ── identity: names, lineup, default agent ─────────────────────────────────


def test_session_name_deterministic_and_safe():
    assert S.session_name("claude") == "syncmill-session-claude"
    assert S.session_name("kimi-code") == "syncmill-session-kimi-code"  # hyphen ok
    assert S.session_name("kimi-code") == S.session_name("kimi-code")  # stable = reattach key


def test_interactive_agents_and_known_names():
    assert set(S.interactive_agents()) == {"claude", "codex", "kimi-code"}
    assert S._known_session_names() == {
        "syncmill-session-claude": "claude",
        "syncmill-session-codex": "codex",
        "syncmill-session-kimi-code": "kimi-code",
    }


def test_default_interactive_agent():
    assert S.default_interactive_agent(["claude", "codex"]) == "claude"
    # skips a priority entry without an interactive mapping
    assert S.default_interactive_agent(["ghost", "codex"]) == "codex"
    assert S.default_interactive_agent([]) is None
    assert S.default_interactive_agent(["ghost"]) is None


def test_session_exists(monkeypatch):
    monkeypatch.setattr(S, "_tmux", lambda *a: _ns(0 if a[-1] == "live" else 1))
    assert S.session_exists("live") is True
    assert S.session_exists("dead") is False


# ── start: create / reattach / failures / post-attach states ───────────────


def test_start_creates_new_session_argv(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    fake = _FakeTmux()
    monkeypatch.setattr(S, "_tmux", fake)
    monkeypatch.setattr(S, "_tmux_attach", lambda name: 0)
    monkeypatch.setattr(S, "_sweep_stale_session_dirs", lambda: None)

    code = S.start_session("claude", "/work/dir")
    assert code == 0
    new = next(c for c in fake.calls if c[0] == "new-session")
    assert "-d" in new  # detached
    assert new[new.index("-s") + 1] == "syncmill-session-claude"
    assert new[new.index("-c") + 1] == "/work/dir"  # tmux start-directory = real tree
    # the pane runs the env-shim, then the BARE interactive binary (no -p / no JSON)
    assert sys.executable in new and S._SESSION_ENV_SHIM in new
    assert new[-1] == "claude"
    assert "-p" not in new and "-e" not in new  # no programmatic flags, no secrets in argv
    assert fake.run_dir and (Path(fake.run_dir) / S._SESSION_SENTINEL).is_file()


def test_start_reattach_skips_new_session(monkeypatch):
    fake = _FakeTmux(existing={"syncmill-session-codex"})
    monkeypatch.setattr(S, "_tmux", fake)
    attached: list[str] = []
    monkeypatch.setattr(S, "_tmux_attach", lambda name: attached.append(name) or 0)
    code = S.start_session("codex", "/w")
    assert code == 0
    assert "new-session" not in fake.names()  # reattach: never recreated
    assert attached == ["syncmill-session-codex"]  # attached to the deterministic name


def test_start_create_failure_raises_and_cleans(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    fake = _FakeTmux(new_session_rc=1)
    monkeypatch.setattr(S, "_tmux", fake)
    monkeypatch.setattr(S, "_tmux_attach", lambda name: 0)
    monkeypatch.setattr(S, "_sweep_stale_session_dirs", lambda: None)
    with pytest.raises(S.SessionLaunchError):
        S.start_session("claude", "/w")
    assert fake.run_dir and not Path(fake.run_dir).exists()  # cleaned on failure


def test_start_create_oserror_raises_and_cleans(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    seen: dict[str, str | None] = {}

    def boom(*args):
        if args[0] == "has-session":
            return _ns(1)  # not exists → create path
        if args[0] == "new-session":
            seen["dir"] = _find_run_dir(args)
            raise OSError("Argument list too long")
        return _ns(0)

    monkeypatch.setattr(S, "_tmux", boom)
    monkeypatch.setattr(S, "_sweep_stale_session_dirs", lambda: None)
    with pytest.raises(S.SessionLaunchError):
        S.start_session("claude", "/w")
    assert seen["dir"] and not Path(seen["dir"]).exists()


def test_start_setup_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    fake = _FakeTmux()
    monkeypatch.setattr(S, "_tmux", fake)
    monkeypatch.setattr(S, "_sweep_stale_session_dirs", lambda: None)

    def boom(run_dir):
        raise ValueError("bad env encoding")

    monkeypatch.setattr(S, "_write_session_env_snapshot", boom)
    with pytest.raises(S.SessionLaunchError):
        S.start_session("claude", "/w")
    assert "new-session" not in fake.names()  # never reached the launch


def test_start_post_attach_still_running(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    fake = _FakeTmux()
    monkeypatch.setattr(S, "_tmux", fake)
    monkeypatch.setattr(S, "_tmux_attach", lambda name: 0)  # detach; session stays
    monkeypatch.setattr(S, "_sweep_stale_session_dirs", lambda: None)
    assert S.start_session("claude", "/w") == 0
    assert "still running" in capsys.readouterr().out
    assert "kill-session" not in fake.names()


def test_start_post_attach_ended(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    fake = _FakeTmux()
    monkeypatch.setattr(S, "_tmux", fake)

    def attach(name):  # the user quit the agent → its session is gone
        fake.existing.discard(name)
        return 0

    monkeypatch.setattr(S, "_tmux_attach", attach)
    monkeypatch.setattr(S, "_sweep_stale_session_dirs", lambda: None)
    assert S.start_session("claude", "/w") == 0
    assert "session ended" in capsys.readouterr().out


def test_start_attach_failure_surfaced(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    fake = _FakeTmux()
    monkeypatch.setattr(S, "_tmux", fake)
    monkeypatch.setattr(S, "_tmux_attach", lambda name: 1)  # attach failed; session lives
    monkeypatch.setattr(S, "_sweep_stale_session_dirs", lambda: None)
    assert S.start_session("claude", "/w") == 1
    assert "could not attach" in capsys.readouterr().out


def test_create_passes_workdir_without_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    fake = _FakeTmux()
    monkeypatch.setattr(S, "_tmux", fake)
    monkeypatch.setattr(S, "_tmux_attach", lambda name: 0)
    monkeypatch.setattr(S, "_sweep_stale_session_dirs", lambda: None)
    S.start_session("claude", "/path with spaces/repo")
    new = next(c for c in fake.calls if c[0] == "new-session")
    assert new[new.index("-c") + 1] == "/path with spaces/repo"  # one verbatim arg


# ── env snapshot + self-unlinking shim ─────────────────────────────────────


def test_env_snapshot_0600_no_inner_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/venv/bin:/usr/bin")
    monkeypatch.setenv("SYNCMILL_GATE__COMMANDS", '["pytest"]')
    monkeypatch.setenv("MY_SECRET", "shh")
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TMUX", "/should/not/forward")
    monkeypatch.setenv(M.INNER_ENV, "/some/monitor/run-dir")  # already exported
    S._write_session_env_snapshot(tmp_path)
    env_file = tmp_path / "env"
    snap = json.loads(env_file.read_text())
    assert snap["PATH"] == "/venv/bin:/usr/bin"  # outer env forwarded → binary resolves
    assert snap["SYNCMILL_GATE__COMMANDS"] == '["pytest"]'
    assert snap["MY_SECRET"] == "shh"  # secret in the file, never argv
    assert "TERM" not in snap and "TMUX" not in snap  # tmux owns the pane's
    assert M.INNER_ENV not in snap  # SYNCMILL_MONITOR_DIR must NOT leak into the agent
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"


def test_session_env_shim_applies_and_unlinks(tmp_path):
    # Driven through a real (tmux-free) python child: the shim execs a python that
    # dumps its resulting env, exactly as production execs the bare agent binary.
    snap = {"SYNCMILL_KEEP": "from-outer", "PATH": "/snapshot/bin"}
    (tmp_path / "env").write_text(json.dumps(snap))
    polluted = dict(os.environ)
    polluted["TERM"] = "xterm-pane"  # tmux-owned pane var
    polluted["SYNCMILL_STALE"] = "leaked"  # stale server config absent from the snapshot
    dump = "import os,json,sys; sys.stdout.write(json.dumps(dict(os.environ)))"
    proc = subprocess.run(
        [sys.executable, "-c", S._SESSION_ENV_SHIM, str(tmp_path), sys.executable, "-c", dump],
        capture_output=True,
        text=True,
        env=polluted,
    )
    assert proc.returncode == 0, proc.stderr
    env = json.loads(proc.stdout)
    assert env["SYNCMILL_KEEP"] == "from-outer"  # snapshot applied
    assert env["PATH"] == "/snapshot/bin"  # snapshot wins over stale server PATH
    assert env["TERM"] == "xterm-pane"  # tmux-owned pane var preserved across the swap
    assert "SYNCMILL_STALE" not in env  # stale server env CLEARED (proves clear(), not update)
    assert not (tmp_path / "env").exists()  # the shim self-unlinked the secret file


def test_session_env_shim_unlinks_even_on_corrupt_env(tmp_path):
    # If json parsing fails, the env file must STILL be unlinked (unlink-before-parse),
    # so a corrupt snapshot never leaves a secret lingering.
    (tmp_path / "env").write_text("{ not valid json")
    proc = subprocess.run(
        [sys.executable, "-c", S._SESSION_ENV_SHIM, str(tmp_path), sys.executable, "-c", "pass"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0  # parse fails → shim aborts, the binary never runs
    assert not (tmp_path / "env").exists()  # but the secret file was still removed


def test_session_shim_keep_set_matches_snapshot_skip():
    # The shim is a separate `python -c` interpreter, so its keep-tuple is a hand
    # copy of _SNAPSHOT_SKIP_ENV — guard the lockstep, and prove the constant is the
    # very same object reused from monitor (no drift).
    for var in S._SNAPSHOT_SKIP_ENV:
        assert var in S._SESSION_ENV_SHIM, f"{var} skipped from snapshot but not kept by shim"
    assert S._SNAPSHOT_SKIP_ENV is M._SNAPSHOT_SKIP_ENV


# ── sweep, list, stop, attach ──────────────────────────────────────────────


def test_sweep_removes_only_stale_session_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(S.tempfile, "gettempdir", lambda: str(tmp_path))
    fresh = tmp_path / "syncmill-session-fresh"
    stale = tmp_path / "syncmill-session-stale"
    no_sentinel = tmp_path / "syncmill-session-nosentinel"
    other = tmp_path / "unrelated-dir"
    for d in (fresh, stale, no_sentinel, other):
        d.mkdir()
    (fresh / S._SESSION_SENTINEL).write_text("")
    (stale / S._SESSION_SENTINEL).write_text("")  # write BEFORE backdating mtime
    old = time.time() - (S._STALE_SESSION_DIR_SECONDS + 60)
    os.utime(stale, (old, old))
    os.utime(no_sentinel, (old, old))  # old + prefix-match but NO sentinel → not ours
    os.utime(other, (old, old))
    S._sweep_stale_session_dirs()
    assert fresh.exists()  # recent → kept (a live session may still reference it)
    assert other.exists()  # not a syncmill-session-* dir → never touched
    assert no_sentinel.exists()  # prefix match without the sentinel → never touched
    assert not stale.exists()  # old + ours (sentinel) → swept


def test_list_sessions_parses_filters_and_counts(monkeypatch):
    out = (
        "syncmill-session-claude 2\n"  # 2 clients attached → still True
        "syncmill-session-codex 0\n"  # detached
        "syncmill-session-kimi-code 1\n"
        "other-session 1\n"  # not a known interactive name → filtered out
    )
    monkeypatch.setattr(S, "_tmux", lambda *a: _ns(0, stdout=out))
    assert S.list_sessions() == [
        S.SessionInfo("syncmill-session-claude", True),
        S.SessionInfo("syncmill-session-codex", False),
        S.SessionInfo("syncmill-session-kimi-code", True),
    ]


def test_list_sessions_empty_on_no_server(monkeypatch):
    monkeypatch.setattr(S, "_tmux", lambda *a: _ns(1, stderr="no server running"))
    assert S.list_sessions() == []


def test_stop_one(monkeypatch):
    fake = _FakeTmux(existing={"syncmill-session-claude"})
    monkeypatch.setattr(S, "_tmux", fake)
    assert S.stop_sessions("claude", False) == 1
    assert ("kill-session", "-t", "syncmill-session-claude") in fake.calls
    assert "syncmill-session-claude" not in fake.existing


def test_stop_all_leaves_foreign(monkeypatch):
    # stop --all uses the EXACT known names only — a monitor-style session that
    # merely shares the prefix is never killed.
    fake = _FakeTmux(
        existing={
            "syncmill-session-claude",
            "syncmill-session-codex",
            "syncmill-session-deadbeef",  # monitor-style / unknown
        }
    )
    monkeypatch.setattr(S, "_tmux", fake)
    assert S.stop_sessions(None, True) == 2
    assert fake.existing == {"syncmill-session-deadbeef"}  # foreign untouched


def test_stop_nonexistent_is_zero(monkeypatch):
    fake = _FakeTmux(existing=set())
    monkeypatch.setattr(S, "_tmux", fake)
    assert S.stop_sessions("claude", False) == 0
    assert "kill-session" not in fake.names()  # nothing to kill (idempotent)


def test_attach_resolves_agent_id_and_full_name(monkeypatch):
    fake = _FakeTmux(existing={"syncmill-session-claude", "syncmill-session-codex"})
    monkeypatch.setattr(S, "_tmux", fake)
    attached: list[str] = []
    monkeypatch.setattr(S, "_tmux_attach", lambda name: attached.append(name) or 0)
    S.attach_session("claude")  # agent id → deterministic name
    S.attach_session("syncmill-session-codex")  # full name verbatim
    assert attached == ["syncmill-session-claude", "syncmill-session-codex"]


def test_attach_nonexistent_raises(monkeypatch):
    monkeypatch.setattr(S, "_tmux", lambda *a: _ns(1))  # has-session always rc 1
    with pytest.raises(S.SessionLaunchError):
        S.attach_session("claude")
