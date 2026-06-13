"""agents.py — parsing, command building, execute(), and AgentRegistry."""

from __future__ import annotations

import json
import subprocess
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
