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
    KimiCodeAgent,
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


# ── Kimi Code / Codex parsing ────────────────────────────────────────────


def test_kimi_parse_keeps_assistant_text_only():
    # stream-json JSONL: keep assistant text lines, in order; skip tool_calls
    # (no text content), tool results (role=tool), and the meta resume-hint
    # (role=meta — note it ALSO carries a `content` field, so role is the filter).
    a = KimiCodeAgent()
    lines = "\n".join(
        [
            '{"role":"assistant","tool_calls":[{"type":"function","id":"Write_0",'
            '"function":{"name":"Write","arguments":"{}"}}]}',
            '{"role":"tool","tool_call_id":"Write_0","content":"Wrote 10 bytes"}',
            '{"role":"assistant","content":"hello"}',
            "garbage line",
            '{"role":"assistant","content":"world"}',
            '{"role":"meta","type":"session.resume_hint","content":"To resume: kimi -r x"}',
        ]
    )
    assert a._parse_output(lines) == "hello\nworld"


def test_kimi_parse_fallback_when_no_assistant_text():
    a = KimiCodeAgent()
    assert a._parse_output("plain non-jsonl") == "plain non-jsonl"


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
    # kimi-code: product/agent id is kimi-code but the binary is `kimi`.
    assert KimiCodeAgent()._build_command("P") == [
        "kimi", "-p", "P", "--output-format", "stream-json",
    ]
    assert CodexAgent()._build_command("P") == [
        "codex", "exec", "P", "--json", "--full-auto",
    ]


def test_agents_satisfy_runtime_protocol():
    for a in (ClaudeAgent(), CodexAgent(), KimiCodeAgent()):
        assert isinstance(a, AgentRunner)
        assert a.agent_id in {"claude", "codex", "kimi-code"}


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
    r = CodexAgent().execute("p", tmp_path, timeout=5)
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
    r = CodexAgent().execute("p", tmp_path, timeout=5)
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


class _FakeStream:
    """asyncio.StreamReader stand-in for the teed path: yields ``data`` in <=n-byte
    slices, then EOF (``b""``). ``hang_after=True`` blocks forever after draining —
    models a stream that never closes, forcing a teed timeout."""

    def __init__(self, data: bytes = b"", *, hang_after: bool = False):
        self._data = data
        self._pos = 0
        self._hang_after = hang_after

    async def read(self, n: int) -> bytes:
        if self._pos >= len(self._data):
            if self._hang_after:
                await asyncio.sleep(3600)
            return b""
        chunk = self._data[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process. Bytes in (like real pipes).

    Carries ``.stdout``/``.stderr`` ``_FakeStream``s so the teed path
    (``_communicate_teed``, which reads the streams) is just as hermetic as the
    ``communicate()`` path — the communicate()-only call sites ignore them.
    """

    def __init__(
        self,
        returncode=0,
        stdout=b"",
        stderr=b"",
        *,
        hang=False,
        hang_wait=False,
        stream_hang=False,
    ):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang  # never return from communicate() → force a timeout
        # hang_wait: wait() never returns → models a grandchild holding the pipes
        # after SIGKILL, so the reap must be time-bounded or it hangs forever.
        self._hang_wait = hang_wait
        self.killed = False
        self.waited = False
        # stream_hang: stdout never EOFs after draining → forces a TEED timeout.
        self.stdout = _FakeStream(stdout, hang_after=stream_hang)
        self.stderr = _FakeStream(stderr)

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        if self._hang_wait:
            # Real asyncio: a reap cut short by wait_for leaves returncode unset
            # (None) — so _build_result classifies the run FAILED, not COMPLETED.
            self.returncode = None
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
    r = run_async(CodexAgent().aexecute("p", tmp_path, timeout=5))
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
    _patch_exec(
        monkeypatch,
        proc=_FakeProc(0, '{"role":"assistant","content":"héllo"}'.encode("utf-8")),
    )
    r = run_async(KimiCodeAgent().aexecute("p", tmp_path, timeout=5))
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


# ── aexecute() output teeing (#16, the live-monitor seam) ─────────────────


class _RecSink:
    """Records every (agent_id, chunk) the teed path feeds it."""

    def __init__(self):
        self.fed: list[tuple[str, bytes]] = []

    def feed(self, agent_id: str, chunk: bytes) -> None:
        self.fed.append((agent_id, chunk))

    def joined(self, agent_id: str) -> bytes:
        # Assert on the CONCATENATION, never chunk boundaries (read(4096) may
        # coalesce or split).
        return b"".join(c for a, c in self.fed if a == agent_id)


def test_aexecute_no_sink_uses_communicate(monkeypatch, tmp_path):
    # Byte-for-byte-unchanged proof: with no sink, the streams are never read —
    # so a proc whose .stdout would hang forever still succeeds via communicate().
    _patch_exec(monkeypatch, proc=_FakeProc(0, b'{"result":"ok"}', stream_hang=True))
    r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=5))
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "ok"


def test_aexecute_tees_chunks_and_keeps_result(monkeypatch, tmp_path):
    sink = _RecSink()
    out = b'{"type":"result","result":"done","is_error":false}'
    _patch_exec(monkeypatch, proc=_FakeProc(0, out))
    with A.stream_to(sink):
        r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=5))
    # the result is IDENTICAL to the communicate() path …
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "done"
    # … and the sink saw the full streamed output, keyed by agent id
    assert sink.joined("claude") == out


def test_aexecute_real_child_tees(monkeypatch, tmp_path):
    # The ONE real subprocess in the teeing suite: a tiny python child whose
    # stdout the teed path must stream. Asserts on the concatenation + that the
    # captured result still parses (Claude reads its JSON `result`).
    sink = _RecSink()
    payload = '{"type":"result","result":"hi","is_error":false}'
    monkeypatch.setattr(
        ClaudeAgent,
        "_build_command",
        lambda self, prompt: [
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({payload!r}); sys.stdout.flush()",
        ],
    )
    with A.stream_to(sink):
        r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=30))
    assert r.status is TaskStatus.COMPLETED
    assert r.output == "hi"
    assert sink.joined("claude").decode() == payload


def test_aexecute_teed_timeout_marks_sink(monkeypatch, tmp_path):
    # A teed timeout: the pane already saw the pre-timeout bytes (the documented
    # third divergence), so the sink gets them + a self-explaining marker, while
    # the RESULT stays empty + FAILED and the child is killed/reaped.
    sink = _RecSink()
    proc = _FakeProc(0, b"partial-output", stream_hang=True)
    created = _patch_exec(monkeypatch, proc=proc)
    with A.stream_to(sink):
        r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=0.05))
    assert r.status is TaskStatus.FAILED
    assert r.output == ""  # the result contract is unchanged on timeout
    assert r.error == "Timed out after 0.05s"
    joined = sink.joined("claude").decode()
    assert "partial-output" in joined  # the live view kept the early bytes
    assert "timed out after 0.05s" in joined  # + a marker so the pane self-explains
    assert created["proc"].killed is True


def test_aexecute_teed_reap_is_bounded_not_hanging(monkeypatch, tmp_path):
    # Streams EOF cleanly but proc.wait() hangs (a grandchild holding the pipes).
    # The teed reap is bounded like _terminate's — aexecute must still finish.
    monkeypatch.setattr(A, "_REAP_TIMEOUT_SECONDS", 0.05)
    sink = _RecSink()
    proc = _FakeProc(0, b'{"result":"ok"}', hang_wait=True)
    _patch_exec(monkeypatch, proc=proc)
    with A.stream_to(sink):
        r = run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=5))
    # returned (didn't hang) with the streamed output captured + reap attempted;
    # a cut-short reap leaves returncode None → FAILED (the real classification).
    assert r.status is TaskStatus.FAILED
    assert r.exit_code is None
    assert r.output == "ok"  # the teed bytes were still captured before the reap
    assert proc.waited is True
    assert sink.joined("claude") == b'{"result":"ok"}'


def test_aexecute_isolated_tees_each_agent_through_fanout(monkeypatch, tmp_path):
    # The load-bearing async claim: the stream_to ContextVar set in the SYNC frame
    # propagates through run_async → gather into EACH agent task of the fan-out, so
    # every concurrent agent tees to the sink keyed by its own id.
    sink = _RecSink()
    outs = {
        "claude": b'{"type":"result","result":"C","is_error":false}',
        "codex": b'{"type":"item.completed","item":{"type":"agent_message","text":"X"}}',
    }

    async def fake_exec(*cmd, **kw):
        return _FakeProc(0, outs[cmd[0]])

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)
    agents = [ClaudeAgent(), CodexAgent()]
    wts = [tmp_path / "a", tmp_path / "b"]
    for w in wts:
        w.mkdir()
    with A.stream_to(sink):
        results = run_async(
            A.aexecute_isolated(agents, "p", wts, lambda aid: 5)
        )
    assert [r.status for r in results] == [TaskStatus.COMPLETED, TaskStatus.COMPLETED]
    # BOTH agents teed to the sink under their own id → the contextvar propagated
    # into the gather tasks (not just the top-level coroutine).
    assert sink.joined("claude") == outs["claude"]
    assert sink.joined("codex") == outs["codex"]


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

    agents = [ClaudeAgent(), CodexAgent(), KimiCodeAgent()]
    results = run_async(A.aexecute_all(agents, "p", tmp_path, timeout=5))
    assert [r.status for r in results] == [TaskStatus.COMPLETED] * 3
    assert state["max_active"] == 3


def test_aexecute_all_preserves_agent_order(monkeypatch, tmp_path):
    # gather returns results in submission order even when a later agent
    # finishes first (claude is slowest here, kimi-code instant). NOTE: the fake
    # keys delays by cmd[0] — the BINARY — and kimi-code's binary is `kimi`.
    delays = {"claude": 0.04, "codex": 0.02, "kimi": 0.0}

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

    agents = [ClaudeAgent(), CodexAgent(), KimiCodeAgent()]
    results = run_async(A.aexecute_all(agents, "p", tmp_path, timeout=5))
    assert [r.agent_id for r in results] == ["claude", "codex", "kimi-code"]


# ── aexecute_isolated (per-agent-worktree concurrency primitive) ──────────


def test_aexecute_isolated_runs_agents_concurrently(monkeypatch, tmp_path):
    # The compete shape: each agent in its OWN worktree, all overlapping. Prove
    # max_active == N deterministically (same probe as the aexecute_all test).
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

    agents = [ClaudeAgent(), CodexAgent(), KimiCodeAgent()]
    worktrees = [tmp_path / a.agent_id for a in agents]
    results = run_async(A.aexecute_isolated(agents, "p", worktrees, lambda aid: 5))
    assert [r.status for r in results] == [TaskStatus.COMPLETED] * 3
    assert state["max_active"] == 3


def test_aexecute_isolated_per_agent_workdir_and_order(monkeypatch, tmp_path):
    # each agent executes in its OWN worktree (distinct cwd), and gather returns
    # results in agent order even when a later agent finishes first.
    delays = {"claude": 0.04, "codex": 0.02, "kimi": 0.0}  # keyed by binary
    seen_cwd: dict[str, str] = {}

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
        seen_cwd[cmd[0]] = kwargs["cwd"]
        return _Proc(delays[cmd[0]])

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    agents = [ClaudeAgent(), CodexAgent(), KimiCodeAgent()]
    worktrees = [tmp_path / a.agent_id for a in agents]
    results = run_async(A.aexecute_isolated(agents, "p", worktrees, lambda aid: 5))
    assert [r.agent_id for r in results] == ["claude", "codex", "kimi-code"]
    # each ran in its own worktree path — no shared workdir (seen_cwd is keyed by
    # binary; kimi-code's binary `kimi` maps to its own `kimi-code` worktree)
    assert seen_cwd == {
        "claude": str(tmp_path / "claude"),
        "codex": str(tmp_path / "codex"),
        "kimi": str(tmp_path / "kimi-code"),
    }


def test_aexecute_isolated_honors_per_agent_timeout_without_aborting_others(
    monkeypatch, tmp_path
):
    # timeout_for gives kimi-code a tiny deadline and its child hangs → kimi-code
    # times out (FAILED) while claude/codex finish. Proves the per-agent timeout
    # routes to the right agent AND that one timeout doesn't abort the batch.
    procs: dict[str, "object"] = {}

    class _Proc:
        returncode = 0

        def __init__(self, hang):
            self._hang = hang
            self.killed = False

        async def communicate(self):
            if self._hang:
                await asyncio.sleep(3600)
            return b'{"type":"result","result":"ok","is_error":false}', b""

        def kill(self):
            self.killed = True

        async def wait(self):
            return 0

    async def fake_exec(*cmd, **kwargs):
        p = _Proc(hang=(cmd[0] == "kimi"))  # kimi-code's binary is `kimi`
        procs[cmd[0]] = p
        return p

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    # timeouts is keyed by agent_id (timeout_for receives agent.agent_id)
    timeouts = {"claude": 5, "codex": 5, "kimi-code": 0.05}
    agents = [ClaudeAgent(), CodexAgent(), KimiCodeAgent()]
    worktrees = [tmp_path / a.agent_id for a in agents]
    results = run_async(
        A.aexecute_isolated(agents, "p", worktrees, lambda aid: timeouts[aid])
    )
    by_id = {r.agent_id: r for r in results}
    assert by_id["claude"].status is TaskStatus.COMPLETED
    assert by_id["codex"].status is TaskStatus.COMPLETED
    assert by_id["kimi-code"].status is TaskStatus.FAILED
    assert by_id["kimi-code"].error == "Timed out after 0.05s"
    assert procs["kimi"].killed is True  # the hung child (binary `kimi`) was reaped


def test_aexecute_isolated_rejects_worktree_count_mismatch(tmp_path):
    # contract guard: one worktree per agent, never a silent zip truncation
    agents = [ClaudeAgent(), CodexAgent()]
    with pytest.raises(ValueError, match="one worktree per agent"):
        run_async(A.aexecute_isolated(agents, "p", [tmp_path], lambda aid: 5))


def test_aexecute_isolated_propagates_unexpected_agent_exception(monkeypatch, tmp_path):
    # Plain gather (no return_exceptions): EXPECTED failures (FileNotFoundError /
    # OSError / timeout) already become FAILED results inside aexecute, but an
    # UNEXPECTED exception is a programming error that must SURFACE, not be masked
    # into a FAILED result — same philosophy as the sync path. ValueError is not
    # one aexecute catches, so it escapes create_subprocess_exec → gather → caller.
    _patch_exec(monkeypatch, raise_exc=ValueError("boom"))
    agents = [ClaudeAgent(), CodexAgent()]
    worktrees = [tmp_path / "a", tmp_path / "b"]
    with pytest.raises(ValueError, match="boom"):
        run_async(A.aexecute_isolated(agents, "p", worktrees, lambda aid: 5))


def test_aexecute_isolated_drains_siblings_when_one_raises(monkeypatch, tmp_path):
    # The raise surfaces (above), but the fan-out must first cancel + drain the
    # siblings: no agent task may be left pending on the REUSED loop (it could
    # resume mid-cleanup), and a sibling mid-flight must have its child reaped.
    import syncmill.memory as mem

    sibling = _FakeProc(hang=True)  # still inside communicate() when claude blows up

    async def fake_exec(*cmd, **kwargs):
        if cmd[0] == "claude":
            raise ValueError("boom")  # unexpected — aexecute does not catch this
        return sibling  # codex hangs until cancelled

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    agents = [ClaudeAgent(), CodexAgent()]
    worktrees = [tmp_path / "claude", tmp_path / "codex"]
    with pytest.raises(ValueError, match="boom"):
        run_async(A.aexecute_isolated(agents, "p", worktrees, lambda aid: 5))

    # no sibling task left alive on the shared loop after the raise propagated
    assert [t for t in asyncio.all_tasks(mem._loop) if not t.done()] == []
    # the hung sibling's child was SIGKILLed + reaped during the drain
    assert sibling.killed is True
    assert sibling.waited is True


# ── aexecute_each (per-item-prompt concurrency primitive) ─────────────────


def test_aexecute_each_runs_agents_concurrently(monkeypatch, tmp_path):
    # DecomposeStrategy's subtask shape: each agent a DISTINCT prompt in its OWN
    # worktree, all overlapping. Prove max_active == N (same probe as isolated).
    state = {"active": 0, "max_active": 0}

    class _Probe:
        returncode = 0

        async def communicate(self):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.02)
            state["active"] -= 1
            return b'{"type":"result","result":"ok","is_error":false}', b""

        def kill(self):  # pragma: no cover - not hit on success
            ...

        async def wait(self):  # pragma: no cover
            return 0

    async def fake_exec(*cmd, **kwargs):
        return _Probe()

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    agents = [ClaudeAgent(), CodexAgent(), KimiCodeAgent()]
    prompts = ["p0", "p1", "p2"]
    worktrees = [tmp_path / a.agent_id for a in agents]
    results = run_async(A.aexecute_each(agents, prompts, worktrees, lambda aid: 5))
    assert [r.status for r in results] == [TaskStatus.COMPLETED] * 3
    assert state["max_active"] == 3


def test_aexecute_each_distinct_prompt_per_agent_and_order(monkeypatch, tmp_path):
    # each agent runs with its OWN prompt in its OWN worktree; gather returns results
    # in submission order even when a later agent finishes first.
    delays = {"claude": 0.04, "codex": 0.02, "kimi": 0.0}  # keyed by binary
    seen: dict[str, tuple[str, str]] = {}  # binary -> (prompt, cwd)

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
        seen[cmd[0]] = (cmd[2], kwargs["cwd"])  # cmd[2] is the prompt for all three
        return _Proc(delays[cmd[0]])

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    agents = [ClaudeAgent(), CodexAgent(), KimiCodeAgent()]
    prompts = ["claude-sub", "codex-sub", "kimi-sub"]
    worktrees = [tmp_path / a.agent_id for a in agents]
    results = run_async(A.aexecute_each(agents, prompts, worktrees, lambda aid: 5))
    assert [r.agent_id for r in results] == ["claude", "codex", "kimi-code"]
    # each ran with its own prompt in its own worktree (kimi-code's binary is `kimi`)
    assert seen == {
        "claude": ("claude-sub", str(tmp_path / "claude")),
        "codex": ("codex-sub", str(tmp_path / "codex")),
        "kimi": ("kimi-sub", str(tmp_path / "kimi-code")),
    }


def test_aexecute_each_rejects_length_mismatch(tmp_path):
    # contract guard: agents / prompts / worktrees must be aligned, never a silent
    # zip truncation
    agents = [ClaudeAgent(), CodexAgent()]
    with pytest.raises(ValueError, match="aligned agents/prompts/worktrees"):
        run_async(
            A.aexecute_each(
                agents, ["only one"], [tmp_path / "a", tmp_path / "b"], lambda aid: 5
            )
        )


def test_aexecute_each_drains_siblings_when_one_raises(monkeypatch, tmp_path):
    # shares aexecute_isolated's cancel-drain contract (both route through
    # _gather_or_drain): an unexpected raise surfaces, but only after the siblings
    # are cancelled and their children reaped.
    import syncmill.memory as mem

    sibling = _FakeProc(hang=True)

    async def fake_exec(*cmd, **kwargs):
        if cmd[0] == "claude":
            raise ValueError("boom")  # unexpected — aexecute does not catch this
        return sibling  # codex hangs until cancelled

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    agents = [ClaudeAgent(), CodexAgent()]
    worktrees = [tmp_path / "claude", tmp_path / "codex"]
    with pytest.raises(ValueError, match="boom"):
        run_async(A.aexecute_each(agents, ["p0", "p1"], worktrees, lambda aid: 5))

    assert [t for t in asyncio.all_tasks(mem._loop) if not t.done()] == []
    assert sibling.killed is True and sibling.waited is True


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
    assert [a.agent_id for a in reg.available()] == ["claude", "codex", "kimi-code"]


def test_registry_custom_order():
    reg = AgentRegistry(SyncmillConfig(agent_priority=["codex", "claude"]))
    assert [a.agent_id for a in reg.available()] == ["codex", "claude"]


def test_registry_disabled_excluded():
    cfg = SyncmillConfig()
    cfg.codex.enabled = False
    reg = AgentRegistry(cfg)
    assert [a.agent_id for a in reg.available()] == ["claude", "kimi-code"]
    assert reg.get("codex") is None


def test_registry_hyphenated_id_maps_to_underscore_config():
    # kimi-code's per-agent config lives under the `kimi_code` field; disabling
    # it via that field must drop the hyphenated id from the registry.
    cfg = SyncmillConfig()
    assert cfg.agent_config("kimi-code") is cfg.kimi_code
    cfg.kimi_code.enabled = False
    reg = AgentRegistry(cfg)
    assert reg.get("kimi-code") is None
    assert [a.agent_id for a in reg.available()] == ["claude", "codex"]


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
