"""agents.py — parsing, command building, execute(), and AgentRegistry."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest

from syncmill import agents as A
from syncmill.agents import (
    AgentRegistry,
    AgentRunner,
    ClaudeAgent,
    CodexAgent,
    GeminiAgent,
    _strip_ansi,
    _try_parse_json,
)
from syncmill.config import AgentConfig, SyncmillConfig
from syncmill.memory import run_async
from syncmill.models import TaskStatus


# ── pure helpers ─────────────────────────────────────────────────────────


def test_strip_ansi():
    assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"
    assert _strip_ansi("plain") == "plain"


def test_try_parse_json_whole():
    assert _try_parse_json('{"a": 1}') == {"a": 1}
    assert _try_parse_json("[1, 2]") == [1, 2]


def test_try_parse_json_mixed_lines():
    assert _try_parse_json('noise\n{"a":1}\nmore') == {"a": 1}


def test_try_parse_json_none():
    assert _try_parse_json("just words") is None
    assert _try_parse_json("") is None


# ── Claude parsing ───────────────────────────────────────────────────────


def test_claude_parse_output_extracts_result():
    a = ClaudeAgent()
    out = a._parse_output('{"type":"result","result":"  hi  ","is_error":false}')
    assert out == "hi"


def test_claude_parse_output_non_json_fallthrough():
    a = ClaudeAgent()
    assert a._parse_output("not json at all") == "not json at all"


def test_claude_parse_output_missing_result_key():
    a = ClaudeAgent()
    assert a._parse_output('{"type":"result"}') == ""


def test_claude_is_error_true_returns_message():
    a = ClaudeAgent()
    assert a._is_error_output('{"is_error":true,"result":"boom"}') == "boom"
    assert a._is_error_output('{"is_error":true}') == "Unknown Claude error"


def test_claude_is_error_false_or_nonjson():
    a = ClaudeAgent()
    assert a._is_error_output('{"is_error":false,"result":"ok"}') is None
    assert a._is_error_output("plain text") is None


# ── Gemini / Codex parsing ───────────────────────────────────────────────


def test_gemini_parse_prefers_model_response():
    a = GeminiAgent()
    assert a._parse_output('{"modelResponse":"A","response":"B"}') == "A"
    assert a._parse_output('{"response":"B"}') == "B"


def test_gemini_parse_dumps_when_no_known_key():
    a = GeminiAgent()
    out = a._parse_output('{"other":1}')
    assert json.loads(out) == {"other": 1}


def test_codex_parse_joins_agent_messages():
    a = CodexAgent()
    lines = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}',
            "garbage line",
            '{"type":"item.completed","item":{"type":"reasoning","text":"ignored"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"world"}}',
        ]
    )
    assert a._parse_output(lines) == "hello\nworld"


def test_codex_parse_fallback_when_no_messages():
    a = CodexAgent()
    assert a._parse_output("plain non-jsonl") == "plain non-jsonl"


# ── build_command + protocol ─────────────────────────────────────────────


def test_build_commands():
    assert ClaudeAgent(AgentConfig(max_turns=3))._build_command("P") == [
        "claude", "-p", "P", "--output-format", "json", "--max-turns", "3",
    ]
    assert GeminiAgent()._build_command("P") == [
        "gemini", "-p", "P", "--output-format", "json", "--yolo",
    ]
    assert CodexAgent()._build_command("P") == [
        "codex", "exec", "P", "--json", "--full-auto",
    ]


def test_agents_satisfy_runtime_protocol():
    for a in (ClaudeAgent(), GeminiAgent(), CodexAgent()):
        assert isinstance(a, AgentRunner)
        assert a.agent_id in {"claude", "gemini", "codex"}


# ── execute() via monkeypatched subprocess.run ───────────────────────────


class _Completed:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, *, result=None, raise_exc=None, capture=None):
    def fake_run(cmd, **kwargs):
        if capture is not None:
            capture["cmd"] = cmd
            capture["kwargs"] = kwargs
        if raise_exc is not None:
            raise raise_exc
        return result

    monkeypatch.setattr(A.subprocess, "run", fake_run)


def test_execute_success(monkeypatch, tmp_path):
    cap = {}
    _patch_run(
        monkeypatch,
        result=_Completed(0, '{"type":"result","result":"done","is_error":false}'),
        capture=cap,
    )
    r = ClaudeAgent().execute("prompt", tmp_path, timeout=30)
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "done"
    assert r.exit_code == 0
    assert r.error is None
    assert isinstance(r.task_id, UUID)
    # passes cwd + timeout + env
    assert cap["kwargs"]["cwd"] == str(tmp_path)
    assert cap["kwargs"]["timeout"] == 30
    assert "env" in cap["kwargs"]


def test_execute_nonzero_uses_stderr(monkeypatch, tmp_path):
    _patch_run(monkeypatch, result=_Completed(2, "some out", "\x1b[31mbad\x1b[0m"))
    r = GeminiAgent().execute("p", tmp_path, timeout=5)
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == 2
    assert r.error == "bad"


def test_execute_is_error_with_zero_exit(monkeypatch, tmp_path):
    _patch_run(monkeypatch, result=_Completed(0, '{"is_error":true,"result":"Not logged in"}'))
    r = ClaudeAgent().execute("p", tmp_path, timeout=5)
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == 0
    assert r.error == "Not logged in"


def test_execute_failed_with_no_message(monkeypatch, tmp_path):
    # documents current behavior: nonzero exit, no stderr -> FAILED, error None
    _patch_run(monkeypatch, result=_Completed(1, "x", "   "))
    r = GeminiAgent().execute("p", tmp_path, timeout=5)
    assert r.status is TaskStatus.FAILED
    assert r.error is None


def test_execute_timeout(monkeypatch, tmp_path):
    exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=5, output="partial")
    _patch_run(monkeypatch, raise_exc=exc)
    r = ClaudeAgent().execute("p", tmp_path, timeout=5)
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == -1
    assert r.error == "Timed out after 5s"
    assert r.output == "partial"


def test_execute_timeout_no_output(monkeypatch, tmp_path):
    exc = subprocess.TimeoutExpired(cmd=["claude"], timeout=5)
    _patch_run(monkeypatch, raise_exc=exc)
    r = ClaudeAgent().execute("p", tmp_path, timeout=5)
    assert r.output == ""


def test_execute_timeout_bytes_output(monkeypatch, tmp_path):
    # subprocess.run yields BYTES in TimeoutExpired.stdout even with text=True
    exc = subprocess.TimeoutExpired(
        cmd=["claude"], timeout=5, output=b"\x1b[31mpartial\x1b[0m"
    )
    _patch_run(monkeypatch, raise_exc=exc)
    r = ClaudeAgent().execute("p", tmp_path, timeout=5)
    assert r.status is TaskStatus.FAILED
    assert r.output == "partial"  # decoded + ANSI-stripped, not a TypeError


def test_execute_oserror_does_not_crash(monkeypatch, tmp_path):
    _patch_run(monkeypatch, raise_exc=PermissionError(13, "Permission denied"))
    r = ClaudeAgent().execute("p", tmp_path, timeout=5)
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == -1
    assert "Failed to launch 'claude'" in r.error


def test_execute_file_not_found(monkeypatch, tmp_path):
    _patch_run(monkeypatch, raise_exc=FileNotFoundError())
    r = ClaudeAgent().execute("p", tmp_path, timeout=5)
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == -1
    assert r.duration_seconds == 0.0
    assert r.error == "CLI tool 'claude' not found in PATH"


# ── aexecute() via monkeypatched create_subprocess_exec ───────────────────
#
# Mirrors the sync execute() suite: where those fake A.subprocess.run, these
# fake A.asyncio.create_subprocess_exec, so the async path is just as hermetic
# (no real binaries, no network, no event loop surprises).


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process. Bytes in (like real pipes)."""

    def __init__(self, returncode=0, stdout=b"", stderr=b"", *, hang=False, hang_wait=False):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang  # never return from communicate() → force a timeout
        # hang_wait: wait() never returns → models a grandchild holding the pipes
        # after SIGKILL, so the reap must be time-bounded or it hangs forever.
        self._hang_wait = hang_wait
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        if self._hang_wait:
            await asyncio.sleep(3600)
        return self.returncode


def _patch_exec(monkeypatch, *, proc=None, raise_exc=None, capture=None):
    """Fake create_subprocess_exec the way _patch_run fakes subprocess.run."""
    created = {}

    async def fake_exec(*cmd, **kwargs):
        if capture is not None:
            capture["cmd"] = cmd
            capture["kwargs"] = kwargs
        if raise_exc is not None:
            raise raise_exc
        created["proc"] = proc
        return proc

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
    return created


def test_aexecute_success(monkeypatch, tmp_path):
    cap = {}
    proc = _FakeProc(0, b'{"type":"result","result":"done","is_error":false}')
    _patch_exec(monkeypatch, proc=proc, capture=cap)
    r = run_async(ClaudeAgent().aexecute("prompt", tmp_path, timeout=30))
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "done"
    assert r.exit_code == 0
    assert r.error is None
    assert isinstance(r.task_id, UUID)
    # command is splatted as *args; cwd + env still flow through
    assert cap["cmd"] == tuple(ClaudeAgent()._build_command("prompt"))
    assert cap["kwargs"]["cwd"] == str(tmp_path)
    assert "env" in cap["kwargs"]


def test_aexecute_nonzero_uses_stderr(monkeypatch, tmp_path):
    _patch_exec(monkeypatch, proc=_FakeProc(2, b"some out", b"\x1b[31mbad\x1b[0m"))
    r = run_async(GeminiAgent().aexecute("p", tmp_path, timeout=5))
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == 2
    assert r.error == "bad"


def test_aexecute_is_error_with_zero_exit(monkeypatch, tmp_path):
    _patch_exec(monkeypatch, proc=_FakeProc(0, b'{"is_error":true,"result":"Not logged in"}'))
    r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=5))
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == 0
    assert r.error == "Not logged in"


def test_aexecute_decodes_utf8_pipes(monkeypatch, tmp_path):
    # raw pipe bytes are decoded before parsing/ANSI-stripping
    _patch_exec(monkeypatch, proc=_FakeProc(0, '{"modelResponse":"héllo"}'.encode("utf-8")))
    r = run_async(GeminiAgent().aexecute("p", tmp_path, timeout=5))
    assert r.output == "héllo"


def test_aexecute_timeout_kills_and_reaps_child(monkeypatch, tmp_path):
    created = _patch_exec(monkeypatch, proc=_FakeProc(hang=True))
    r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=0.05))
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == -1
    assert r.error == "Timed out after 0.05s"
    assert r.output == ""  # asyncio drops buffered output of a killed child
    # the child was SIGKILLed and reaped — no zombie left behind
    assert created["proc"].killed is True
    assert created["proc"].waited is True


def test_aexecute_timeout_reap_is_bounded_not_hanging(monkeypatch, tmp_path):
    # The real defect this guards: if the SIGKILLed child left a grandchild
    # holding its pipes, proc.wait() never returns. aexecute must still finish —
    # the reap is time-bounded, not unbounded.
    monkeypatch.setattr(A, "_REAP_TIMEOUT_SECONDS", 0.05)
    created = _patch_exec(monkeypatch, proc=_FakeProc(hang=True, hang_wait=True))
    r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=0.05))
    assert r.status is TaskStatus.FAILED
    assert r.error == "Timed out after 0.05s"
    # the child was still SIGKILLed even though the reap itself timed out
    assert created["proc"].killed is True
    assert created["proc"].waited is True


def test_terminate_swallows_already_dead_child():
    # kill() racing the child's own exit raises ProcessLookupError — _terminate
    # must treat that as a benign "already reaped" and return without waiting.
    class _Dead:
        def __init__(self):
            self.waited = False

        def kill(self):
            raise ProcessLookupError()

        async def wait(self):
            self.waited = True
            return 0

    proc = _Dead()
    run_async(A._terminate(proc))  # must not raise
    assert proc.waited is False  # returned early — nothing left to reap


def test_aexecute_file_not_found(monkeypatch, tmp_path):
    _patch_exec(monkeypatch, raise_exc=FileNotFoundError())
    r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=5))
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == -1
    assert r.duration_seconds == 0.0
    assert r.error == "CLI tool 'claude' not found in PATH"


def test_aexecute_oserror_does_not_crash(monkeypatch, tmp_path):
    _patch_exec(monkeypatch, raise_exc=PermissionError(13, "Permission denied"))
    r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=5))
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == -1
    assert "Failed to launch 'claude'" in r.error


@pytest.mark.parametrize(
    "rc,out,err",
    [
        (0, '{"type":"result","result":"ok","is_error":false}', ""),
        (0, '{"is_error":true,"result":"boom"}', ""),  # exit 0 + in-band error
        (2, "x", "\x1b[31mbad\x1b[0m"),
        (1, "x", "   "),  # nonzero, no usable stderr
    ],
)
def test_sync_async_classification_parity(monkeypatch, tmp_path, rc, out, err):
    # execute() and aexecute() must classify the same process output identically
    # — the whole point of the shared _build_result helper.
    _patch_run(monkeypatch, result=_Completed(rc, out, err))
    sync_r = ClaudeAgent().execute("p", tmp_path, timeout=5)
    _patch_exec(monkeypatch, proc=_FakeProc(rc, out.encode(), err.encode()))
    async_r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=5))
    assert (sync_r.status, sync_r.output, sync_r.error, sync_r.exit_code) == (
        async_r.status,
        async_r.output,
        async_r.error,
        async_r.exit_code,
    )


# ── aexecute_all (concurrency primitive) ──────────────────────────────────


def test_aexecute_all_runs_agents_concurrently(monkeypatch, tmp_path):
    # Prove agents overlap rather than serialize: all three are inside
    # communicate() at the same moment (max_active == 3), deterministically.
    state = {"active": 0, "max_active": 0}

    class _Probe:
        returncode = 0

        async def communicate(self):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.02)  # yield so the others enter too
            state["active"] -= 1
            return b'{"type":"result","result":"ok","is_error":false}', b""

        def kill(self):  # pragma: no cover - not hit on success
            ...

        async def wait(self):  # pragma: no cover
            return 0

    async def fake_exec(*cmd, **kwargs):
        return _Probe()

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    agents = [ClaudeAgent(), GeminiAgent(), CodexAgent()]
    results = run_async(A.aexecute_all(agents, "p", tmp_path, timeout=5))
    assert [r.status for r in results] == [TaskStatus.COMPLETED] * 3
    assert state["max_active"] == 3


def test_aexecute_all_preserves_agent_order(monkeypatch, tmp_path):
    # gather returns results in submission order even when a later agent
    # finishes first (claude is slowest here, gemini instant).
    delays = {"claude": 0.04, "gemini": 0.0, "codex": 0.02}

    class _Proc:
        returncode = 0

        def __init__(self, delay):
            self._delay = delay

        async def communicate(self):
            await asyncio.sleep(self._delay)
            return b'{"type":"result","result":"ok","is_error":false}', b""

        def kill(self):  # pragma: no cover
            ...

        async def wait(self):  # pragma: no cover
            return 0

    async def fake_exec(*cmd, **kwargs):
        return _Proc(delays[cmd[0]])

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    agents = [ClaudeAgent(), GeminiAgent(), CodexAgent()]
    results = run_async(A.aexecute_all(agents, "p", tmp_path, timeout=5))
    assert [r.agent_id for r in results] == ["claude", "gemini", "codex"]


# ── aexecute end-to-end with a real subprocess ────────────────────────────
#
# These spawn the test runner's own Python (always present, no network, never an
# agent binary), so they stay hermetic while exercising the actual asyncio
# subprocess + reused-loop plumbing the mocks above can't reach.


class _PyAgent(A.CLIAgent):
    def __init__(self, script: str):
        super().__init__("pyagent", AgentConfig())
        self._script = script

    def _build_command(self, prompt: str) -> list[str]:
        return [sys.executable, "-c", self._script]


def test_aexecute_real_subprocess_success(tmp_path):
    agent = _PyAgent("print('done from child')")
    r = run_async(agent.aexecute("p", tmp_path, timeout=30))
    assert r.status is TaskStatus.COMPLETED
    assert "done from child" in r.output
    assert r.exit_code == 0


def test_aexecute_real_subprocess_timeout(tmp_path):
    # a child that would sleep 30s is killed at the 0.3s deadline
    agent = _PyAgent("import time; time.sleep(30)")
    r = run_async(agent.aexecute("p", tmp_path, timeout=0.3))
    assert r.status is TaskStatus.FAILED
    assert r.error == "Timed out after 0.3s"


# ── AgentRegistry ────────────────────────────────────────────────────────


def test_registry_priority_order():
    reg = AgentRegistry(SyncmillConfig())
    assert [a.agent_id for a in reg.available()] == ["claude", "gemini", "codex"]


def test_registry_custom_order():
    reg = AgentRegistry(SyncmillConfig(agent_priority=["codex", "claude"]))
    assert [a.agent_id for a in reg.available()] == ["codex", "claude"]


def test_registry_disabled_excluded():
    cfg = SyncmillConfig()
    cfg.gemini.enabled = False
    reg = AgentRegistry(cfg)
    assert [a.agent_id for a in reg.available()] == ["claude", "codex"]
    assert reg.get("gemini") is None


def test_registry_get():
    reg = AgentRegistry(SyncmillConfig())
    assert reg.get("claude").agent_id == "claude"
    assert reg.get("nope") is None


def test_registry_rejects_unknown_id():
    with pytest.raises(ValueError, match="Unknown agent id"):
        AgentRegistry(SyncmillConfig(agent_priority=["cladue", "claude"]))


def test_registry_available_returns_fresh_list():
    reg = AgentRegistry(SyncmillConfig())
    got = reg.available()
    got.clear()
    assert len(reg.available()) == 3
