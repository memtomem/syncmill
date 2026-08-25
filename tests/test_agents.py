"""agents.py — parsing, command building, execute(), and AgentRegistry."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from syncmill import agents as A
from syncmill.agents import (
    AgentRegistry,
    AgentRunner,
    ClaudeAgent,
    CodexAgent,
    KimiCodeAgent,
    capture_tool_calls,
    model_routing_scope,
    provider_session_scope,
    _strip_ansi,
)
from syncmill.config import AgentConfig, SyncmillConfig
from syncmill.git_env import GIT_REPOSITORY_ROUTING_ENV
from syncmill.memory import run_async
from syncmill.models import TaskStatus, ToolOutcome


# ── pure helpers ─────────────────────────────────────────────────────────


def test_strip_ansi():
    assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"
    assert _strip_ansi("plain") == "plain"


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


def _claude_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / "claude" / name).read_text()


def test_claude_parses_array_transcript():
    # Claude 2.1.x emits a top-level JSON array of the same events stream-json
    # emits one-per-line. Before the shapes were unified this returned the raw
    # array text and lost the session id entirely.
    a = ClaudeAgent()
    payload = _claude_fixture("array-success.json")
    assert a._parse_output(payload) == "Added the helper to src/example.py."
    assert a._is_error_output(payload) is None
    assert a._provider_session_from_output(payload) == "11111111-2222-3333-4444-555555555555"


def test_claude_array_rate_limit_is_not_mistaken_for_success():
    # The regression that motivated the normalizer: exit 0 + an array-shaped
    # in-band 429 must fail the run, with the provider message preserved.
    a = ClaudeAgent()
    payload = _claude_fixture("array-rate-limit.json")
    error = a._is_error_output(payload)
    assert error is not None and "429" in error and "rate_limit_error" in error
    assert a._provider_session_from_output(payload) == "99999999-8888-7777-6666-555555555555"


def test_claude_array_error_event_surfaces_message():
    a = ClaudeAgent()
    error = a._is_error_output(_claude_fixture("array-error-event.json"))
    assert error == "Number of requests has exceeded your per-minute rate limit"


def test_claude_jsonl_transcript_still_parses_without_capture():
    # Same events, JSONL-shaped, parsed identically regardless of capture mode.
    a = ClaudeAgent()
    events = json.loads(_claude_fixture("array-success.json"))
    payload = "\n".join(json.dumps(event) for event in events)
    assert a._parse_output(payload) == "Added the helper to src/example.py."
    assert a._provider_session_from_output(payload) == "11111111-2222-3333-4444-555555555555"


def test_claude_execute_fails_on_array_rate_limit_with_zero_exit(monkeypatch, tmp_path):
    _patch_run(monkeypatch, result=_Completed(0, _claude_fixture("array-rate-limit.json")))
    result = ClaudeAgent().execute("p", tmp_path, 60)
    assert result.status is TaskStatus.FAILED
    assert result.error is not None and "429" in result.error
    assert result.exit_code == 0
    assert result.duration_seconds >= 0


_RECOVERED_ERROR_TRANSCRIPT = [
    {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "done",
        "session_id": "11111111-2222-3333-4444-555555555555",
    },
]


def test_claude_recovered_error_before_terminal_success_is_not_an_error():
    # A transient API error the CLI retried past must not fail a transcript
    # whose terminal result succeeded: the reversed scan stops at the first
    # definitive success (type:"result", is_error strictly False).
    a = ClaudeAgent()
    payload = json.dumps(_RECOVERED_ERROR_TRANSCRIPT)
    assert a._is_error_output(payload) is None
    assert a._parse_output(payload) == "done"


def test_claude_recovered_error_jsonl_variant_is_not_an_error():
    a = ClaudeAgent()
    payload = "\n".join(json.dumps(event) for event in _RECOVERED_ERROR_TRANSCRIPT)
    assert a._is_error_output(payload) is None
    assert a._parse_output(payload) == "done"


def test_claude_execute_completes_on_recovered_error_transcript(monkeypatch, tmp_path):
    _patch_run(
        monkeypatch, result=_Completed(0, json.dumps(_RECOVERED_ERROR_TRANSCRIPT))
    )
    result = ClaudeAgent().execute("p", tmp_path, 60)
    assert result.status is TaskStatus.COMPLETED
    assert result.output == "done"
    assert result.error is None


def test_claude_terminal_success_owns_the_output(monkeypatch, tmp_path):
    # codex R11: _parse_output must honor the SAME terminal-success boundary
    # as _is_error_output — an earlier recovered error's prose must never be
    # published as the successful run's output.
    a = ClaudeAgent()
    payload = json.dumps(
        [
            {"type": "result", "is_error": True, "result": "transient failure"},
            {"type": "result", "subtype": "success", "is_error": False, "result": ""},
        ]
    )
    assert a._is_error_output(payload) is None
    assert a._parse_output(payload) == ""  # NOT "transient failure"
    _patch_run(monkeypatch, result=_Completed(0, payload))
    result = a.execute("p", tmp_path, 60)
    assert result.status is TaskStatus.COMPLETED
    assert result.output == ""
    # A terminal success with a MISSING result key likewise yields "".
    missing = json.dumps(
        [
            {"type": "result", "is_error": True, "result": "transient failure"},
            {"type": "result", "subtype": "success", "is_error": False},
        ]
    )
    assert a._parse_output(missing) == ""


def test_claude_error_after_success_result_still_fails():
    # The success terminator only recovers errors *before* the terminal result;
    # an error event newer than the last success still fails the run.
    a = ClaudeAgent()
    payload = json.dumps(list(reversed(_RECOVERED_ERROR_TRANSCRIPT)))
    assert a._is_error_output(payload) == "Overloaded"


def test_claude_non_boolean_terminal_is_error_does_not_recover():
    # Strict `is False`: a missing/None/stringy is_error on the final result
    # decides nothing, so an earlier genuine error still surfaces.
    a = ClaudeAgent()
    payload = json.dumps(
        [
            {"type": "error", "error": {"message": "Overloaded"}},
            {"type": "result", "result": "done", "is_error": "false"},
        ]
    )
    assert a._is_error_output(payload) == "Overloaded"


def test_claude_pathological_nesting_degrades_instead_of_raising():
    # json.loads("[" * 100000) raises RecursionError (a RuntimeError, not a
    # ValueError) — it must never abort the run (the parse_board_update
    # lesson). And because the payload CLAIMED to be JSON yet yielded no
    # usable event, it classifies as a protocol failure, not as output.
    a = ClaudeAgent()
    hostile = "[" * 100000
    assert a._is_error_output(hostile) == (
        "malformed provider transcript: no usable events"
    )
    assert a._provider_session_from_output(hostile) is None


def test_claude_execute_fails_cleanly_on_pathological_nesting(monkeypatch, tmp_path):
    # No raise — but no false COMPLETED either: route/compete must not crown
    # a protocol failure the winner (codex R10).
    _patch_run(monkeypatch, result=_Completed(0, "[" * 100000))
    result = ClaudeAgent().execute("p", tmp_path, 60)
    assert result.status is TaskStatus.FAILED
    assert result.error is not None and "malformed provider transcript" in result.error
    assert result.exit_code == 0


def test_kimi_and_codex_parsers_survive_pathological_nesting(monkeypatch, tmp_path):
    # codex R7: the Kimi/Codex JSONL parsers call json.loads directly, so a
    # pathologically-nested line raised RecursionError past execute() instead
    # of yielding a TaskResult. codex R10: the surviving TaskResult is FAILED —
    # a zero-exit JSON-shaped payload with no usable event is a protocol
    # failure, not output.
    hostile = "[" * 100000
    assert KimiCodeAgent()._parse_output(hostile) == hostile
    assert CodexAgent()._parse_output(hostile) == hostile
    for agent in (KimiCodeAgent(), CodexAgent()):
        _patch_run(monkeypatch, result=_Completed(0, hostile))
        result = agent.execute("p", tmp_path, 60)
        assert result.status is TaskStatus.FAILED
        assert (
            result.error is not None
            and "malformed provider transcript" in result.error
        )
        assert result.exit_code == 0


def test_plain_text_zero_exit_output_stays_completed(monkeypatch, tmp_path):
    # The malformed-transcript rule fires ONLY on JSON-shaped payloads:
    # genuine plain text never claimed to be JSON and keeps the compatible
    # fallthrough for all three agents.
    for agent in (ClaudeAgent(), KimiCodeAgent(), CodexAgent()):
        assert agent._is_error_output("plain text explanation") is None
        _patch_run(monkeypatch, result=_Completed(0, "plain text explanation"))
        result = agent.execute("p", tmp_path, 60)
        assert result.status is TaskStatus.COMPLETED
        assert result.output == "plain text explanation"


def test_codex_non_dict_json_lines_are_skipped():
    # A valid-JSON list/scalar line used to hit event.get → AttributeError.
    payload = "\n".join(
        [
            '["not-a-dict"]',
            "42",
            '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}',
        ]
    )
    assert CodexAgent()._parse_output(payload) == "hi"


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
        "codex", "exec", "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"', "P", "--json",
    ]


def test_claude_capture_switches_to_stream_json():
    with capture_tool_calls(True):
        assert ClaudeAgent(AgentConfig(max_turns=3))._build_command("P") == [
            "claude", "-p", "P", "--output-format", "stream-json", "--verbose",
            "--max-turns", "3",
        ]


# ── model routing scope → provider model flags (§6 argv table) ──────────


_ROUTED = {"claude": "c-model", "codex": "o-model", "kimi-code": "k-model"}


def test_model_scope_fresh_argv_exact():
    with model_routing_scope(_ROUTED):
        assert ClaudeAgent(AgentConfig(max_turns=3))._build_command("P") == [
            "claude", "-p", "P", "--output-format", "json", "--max-turns", "3",
            "--model", "c-model",
        ]
        # Kimi's model flag LEADS: `kimi -m SELECTOR -p PROMPT ...` (§6 table).
        assert KimiCodeAgent()._build_command("P") == [
            "kimi", "-m", "k-model", "-p", "P", "--output-format", "stream-json",
        ]
        # Codex's flag precedes the positionals: `codex exec -m SEL PROMPT ...`.
        assert CodexAgent()._build_command("P") == [
            "codex", "exec", "-m", "o-model", "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"', "P", "--json",
        ]


def _resume_session(sid: str):
    return A._ACTIVE_PROVIDER_SESSION.set((sid, True, False))


def test_model_scope_resume_argv_exact():
    token = _resume_session("sess-1")
    try:
        with model_routing_scope(_ROUTED):
            assert ClaudeAgent(AgentConfig(max_turns=3))._build_command("P") == [
                "claude", "-p", "P", "--output-format", "json", "--max-turns",
                "3", "--resume", "sess-1", "--model", "c-model",
            ]
            assert KimiCodeAgent()._build_command("P") == [
                "kimi", "-m", "k-model", "-p", "P", "--output-format",
                "stream-json", "--session", "sess-1",
            ]
            # `codex exec resume -m SEL -c ... SESSION PROMPT --json` (§6 table).
            # Sandbox/approval ride -c on BOTH shapes: codex-cli 0.147.0
            # rejects --full-auto outright and dropped the on-failure policy.
            assert CodexAgent()._build_command("P") == [
                "codex", "exec", "resume", "-m", "o-model",
                "-c", 'sandbox_mode="workspace-write"',
                "-c", 'approval_policy="never"',
                "sess-1", "P", "--json",
            ]
    finally:
        A._ACTIVE_PROVIDER_SESSION.reset(token)


def test_no_scope_is_legacy_argv_byte_identical():
    assert ClaudeAgent(AgentConfig(max_turns=3))._build_command("P") == [
        "claude", "-p", "P", "--output-format", "json", "--max-turns", "3",
    ]
    assert KimiCodeAgent()._build_command("P") == [
        "kimi", "-p", "P", "--output-format", "stream-json",
    ]
    assert CodexAgent()._build_command("P") == [
        "codex", "exec", "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"', "P", "--json",
    ]


def test_null_entry_and_missing_entry_omit_the_flag():
    # A None selector (observe / unmapped tier entry) and an absent agent key
    # both mean "omit the model flag" — legacy argv byte-for-byte.
    with model_routing_scope({"claude": None}):
        assert ClaudeAgent(AgentConfig(max_turns=3))._build_command("P") == [
            "claude", "-p", "P", "--output-format", "json", "--max-turns", "3",
        ]
        assert CodexAgent()._build_command("P") == [
            "codex", "exec", "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"', "P", "--json",
        ]


def test_selector_is_one_argv_token_never_shell_interpreted():
    # A selector rides as ONE argv element even with shell metacharacters.
    with model_routing_scope({"codex": "model;rm -rf"}):
        cmd = CodexAgent()._build_command("P")
    assert "model;rm -rf" in cmd
    assert cmd.count("model;rm -rf") == 1


# ── model_routing_scope isolation (§10.4) ───────────────────────────────


def test_model_scope_nested_restores_parent():
    outer = {"claude": "outer"}
    inner = {"claude": "inner"}
    with model_routing_scope(outer):
        with model_routing_scope(inner):
            assert A._MODEL_SELECTORS.get() is inner
        assert A._MODEL_SELECTORS.get() is outer
    assert A._MODEL_SELECTORS.get() is None


def test_model_scope_exception_restores_none():
    with pytest.raises(RuntimeError):
        with model_routing_scope({"claude": "x"}):
            raise RuntimeError("boom")
    assert A._MODEL_SELECTORS.get() is None


def test_model_scope_sequential_runs_do_not_leak():
    with model_routing_scope({"claude": "first"}):
        pass
    assert ClaudeAgent(AgentConfig(max_turns=3))._build_command("P") == [
        "claude", "-p", "P", "--output-format", "json", "--max-turns", "3",
    ]


def test_model_scope_nests_with_session_and_capture_scopes():
    with model_routing_scope(_ROUTED):
        with provider_session_scope():
            with capture_tool_calls(True):
                cmd = ClaudeAgent(AgentConfig(max_turns=3))._build_command("P")
    # capture flags AND the model flag are both present; claude mints a fresh
    # session id inside execute (not _build_command), so no session flag here.
    assert cmd[:5] == ["claude", "-p", "P", "--output-format", "stream-json"]
    assert cmd[-2:] == ["--model", "c-model"]
    assert A._MODEL_SELECTORS.get() is None


def test_model_scope_concurrent_async_contexts_are_isolated():
    """Two top-level async contexts under different selector maps stay isolated
    — task creation copies the ambient context (the compute-once guarantee)."""

    async def build_under(selectors, marker):
        with model_routing_scope(selectors):
            await asyncio.sleep(0.01)  # interleave with the sibling task
            return marker, CodexAgent()._build_command("P")

    async def main():
        return await asyncio.gather(
            build_under({"codex": "model-a"}, "a"),
            build_under({"codex": "model-b"}, "b"),
        )

    results = dict(run_async(main()))
    assert results["a"] == ["codex", "exec", "-m", "model-a", "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"', "P", "--json"]
    assert results["b"] == ["codex", "exec", "-m", "model-b", "-c", 'sandbox_mode="workspace-write"', "-c", 'approval_policy="never"', "P", "--json"]


def test_codex_typed_mcp_telemetry_is_body_free():
    lines = "\n".join([
        json.dumps({"type": "item.completed", "item": {
            "id": "call-1", "type": "mcp_tool_call", "server": "gate_e",
            "tool": "always_fail", "status": "failed", "arguments": {"secret": "SENTINEL"},
            "error": "SENTINEL",
        }}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
    ])
    with capture_tool_calls(True):
        result = CodexAgent()._build_result(UUID(int=1), 0, lines, "", 0.1)
    telemetry = result.tool_telemetry
    assert telemetry is not None and telemetry.complete
    assert [(c.tool_key, c.outcome) for c in telemetry.calls] == [
        ("gate_e::always_fail", ToolOutcome.FAILED)
    ]
    assert "SENTINEL" not in repr(telemetry)


def test_claude_stream_pairs_typed_mcp_outcome():
    lines = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "u1", "name": "mcp__gate_e__always_ok", "input": {"secret": "S"}}
        ]}}),
        json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1", "is_error": False, "content": "S"}
        ]}}),
        json.dumps({"type": "result", "result": "done", "is_error": False}),
    ])
    with capture_tool_calls(True):
        result = ClaudeAgent()._build_result(UUID(int=2), 0, lines, "", 0.1)
    assert result.output == "done"
    assert result.tool_telemetry is not None
    assert result.tool_telemetry.calls[0].tool_key == "gate_e::always_ok"
    assert result.tool_telemetry.calls[0].outcome is ToolOutcome.SUCCEEDED


def test_claude_array_transcript_pairs_tool_outcome():
    # Same events as the JSONL case above, array-shaped: telemetry must survive
    # the wire shape, since the array form previously yielded no events at all.
    payload = json.dumps([
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "u1", "name": "mcp__gate_e__always_ok", "input": {"secret": "S"}}
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1", "is_error": False, "content": "S"}
        ]}},
        {"type": "result", "result": "done", "is_error": False},
    ])
    with capture_tool_calls(True):
        result = ClaudeAgent()._build_result(UUID(int=4), 0, payload, "", 0.1)
    assert result.output == "done"
    assert result.tool_telemetry is not None and result.tool_telemetry.complete
    assert result.tool_telemetry.calls[0].tool_key == "gate_e::always_ok"
    assert result.tool_telemetry.calls[0].outcome is ToolOutcome.SUCCEEDED
    assert "SENTINEL" not in repr(result.tool_telemetry)


def test_claude_array_with_dropped_member_is_incomplete():
    # A non-dict array member is a lost event; telemetry must not claim
    # completeness after silently discarding it.
    payload = json.dumps([
        "unexpected-scalar",
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "u1", "name": "mcp__gate_e__always_ok", "input": {}}
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1", "is_error": False, "content": "ok"}
        ]}},
        {"type": "result", "result": "done", "is_error": False},
    ])
    with capture_tool_calls(True):
        result = ClaudeAgent()._build_result(UUID(int=5), 0, payload, "", 0.1)
    assert result.output == "done"
    assert result.tool_telemetry is not None
    assert result.tool_telemetry.malformed_count == 1
    assert result.tool_telemetry.complete is False


def test_claude_error_transcript_does_not_leak_raw_events_as_output():
    # A decoded transcript with no result text yields empty output, never the
    # raw array (which would put system/session/tool bodies into TaskResult).
    a = ClaudeAgent()
    payload = _claude_fixture("array-error-event.json")
    assert a._parse_output(payload) == ""
    assert a._parse_output("not json at all") == "not json at all"


@pytest.mark.parametrize(
    "payload",
    [
        "[]",                        # decoded, no events at all
        '["SENTINEL"]',              # decoded, only non-dict members
        '"SENTINEL"',                # decoded, bare scalar
        '{"type":"result"}',         # decoded, result event with no text
        '[{"type":"system","session_id":"s","cwd":"/SENTINEL"}]',  # truncated stream
    ],
)
def test_claude_structured_transcript_never_falls_back_to_raw(payload):
    # Any payload that decoded as JSON yields empty output rather than the raw
    # transcript — otherwise event bodies land in TaskResult.output.
    assert ClaudeAgent()._parse_output(payload) == ""


@pytest.mark.parametrize(
    "payload",
    ["not json at all", "Error: command not found", "  ", "{unterminated"],
)
def test_claude_unstructured_output_is_preserved_verbatim(payload):
    # Genuinely unstructured output is the one case where the raw text IS the
    # result — a crash message must not be swallowed into an empty output.
    assert ClaudeAgent()._parse_output(payload) == payload.strip()


def test_claude_non_boolean_is_error_is_not_an_error():
    # Strict boolean check, matching the other providers: the string "false"
    # must not be read as a failure.
    a = ClaudeAgent()
    assert a._is_error_output('{"type":"result","is_error":"false","result":"ok"}') is None
    assert a._is_error_output('{"type":"result","is_error":true,"result":"boom"}') == "boom"


def test_kimi_untyped_tool_result_is_unknown_and_incomplete():
    lines = "\n".join([
        json.dumps({"role": "assistant", "tool_calls": [{
            "id": "k1", "function": {"name": "mcp__gate_e__always_fail", "arguments": "{}"}
        }]}),
        json.dumps({"role": "tool", "tool_call_id": "k1", "content": "failed body"}),
    ])
    with capture_tool_calls(True):
        result = KimiCodeAgent()._build_result(UUID(int=3), 0, lines, "", 0.1)
    assert result.tool_telemetry is not None
    assert result.tool_telemetry.complete is False
    assert result.tool_telemetry.calls[0].outcome is ToolOutcome.UNKNOWN


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

    monkeypatch.setattr(A, "_sync_run", fake_run)


def test_execute_success(monkeypatch, tmp_path):
    for name in GIT_REPOSITORY_ROUTING_ENV:
        monkeypatch.setenv(name, "/tmp/source-routing")
    monkeypatch.setenv("PWD", "/tmp/source")
    monkeypatch.setenv("OLDPWD", "/tmp/previous")
    monkeypatch.setenv("CDPATH", "/tmp")
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
    env = cap["kwargs"]["env"]
    assert env["PWD"] == str(tmp_path.resolve())
    assert not (GIT_REPOSITORY_ROUTING_ENV | {"OLDPWD", "CDPATH"}) & env.keys()


def test_execute_strips_run_identity_ipc(monkeypatch, tmp_path):
    # An agent that inherits these and then invokes syncmill itself satisfies the
    # inner monitor's self-detection and adopts the parent's run id, overwriting
    # the parent's envelope and result.json with its own nested run.
    monkeypatch.setenv("SYNCMILL_MONITOR_DIR", "/tmp/syncmill-monitor-abc")
    monkeypatch.setenv("SYNCMILL_RUN_ID", "parent-run-id")
    monkeypatch.setenv("SYNCMILL_CLAUDE__TIMEOUT_SECONDS", "60")  # config: kept
    cap = {}
    _patch_run(
        monkeypatch,
        result=_Completed(0, '{"type":"result","result":"ok","is_error":false}'),
        capture=cap,
    )
    ClaudeAgent().execute("prompt", tmp_path, timeout=30)
    env = cap["kwargs"]["env"]
    assert "SYNCMILL_MONITOR_DIR" not in env and "SYNCMILL_RUN_ID" not in env
    assert env["SYNCMILL_CLAUDE__TIMEOUT_SECONDS"] == "60"


def test_run_ipc_env_matches_the_monitor_constants():
    # agents.py can't import monitor (monitor imports agents), so the pair is a
    # hand-copy — pin the lockstep.
    from syncmill import monitor as M

    assert A._RUN_IPC_ENV == {M.INNER_ENV, M.RUN_ID_ENV}


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
    for name in GIT_REPOSITORY_ROUTING_ENV:
        monkeypatch.setenv(name, "/tmp/source-routing")
    monkeypatch.setenv("PWD", "/tmp/source")
    monkeypatch.setenv("OLDPWD", "/tmp/previous")
    monkeypatch.setenv("CDPATH", "/tmp")
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
    env = cap["kwargs"]["env"]
    assert env["PWD"] == str(tmp_path.resolve())
    assert not (GIT_REPOSITORY_ROUTING_ENV | {"OLDPWD", "CDPATH"}) & env.keys()


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
    # a cut-short reap leaves returncode None → coerced to -1 → FAILED.
    assert r.status is TaskStatus.FAILED
    assert r.exit_code == -1
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


def test_aexecute_all_drains_siblings_when_one_raises(monkeypatch, tmp_path):
    # aexecute_all shares _gather_or_drain with its siblings: a raising
    # aexecute must first cancel + drain the others — a bare gather left them
    # running on the REUSED loop with un-reaped children (the exact drift the
    # shared tail exists to prevent; no src/ caller today, but the next
    # concurrent strategy would inherit the footgun).
    import syncmill.memory as mem

    sibling = _FakeProc(hang=True)

    async def fake_exec(*cmd, **kwargs):
        if cmd[0] == "claude":
            raise ValueError("boom")
        return sibling

    monkeypatch.setattr(A.asyncio, "create_subprocess_exec", fake_exec)

    agents = [ClaudeAgent(), CodexAgent()]
    with pytest.raises(ValueError, match="boom"):
        run_async(A.aexecute_all(agents, "p", tmp_path, timeout=5))

    assert [t for t in asyncio.all_tasks(mem._loop) if not t.done()] == []
    assert sibling.killed is True
    assert sibling.waited is True


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
        # The prompt's argv POSITION differs per provider (codex prefixes its
        # sandbox `-c` overrides), so match the sub-prompt itself.
        prompt = next(arg for arg in cmd if arg.endswith("-sub"))
        seen[cmd[0]] = (prompt, kwargs["cwd"])
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


def test_aexecute_each_labels_each_stream_separately(tmp_path):
    # The SAME agent object run twice concurrently must tee under its own label —
    # without it decompose's round-robin merges two subtasks into one log/pane.
    class _Sink:
        def __init__(self):
            self.chunks: dict[str, bytes] = {}

        def feed(self, key, chunk):
            self.chunks[key] = self.chunks.get(key, b"") + chunk

    sink = _Sink()
    agent = _PyAgent("print('hello')")
    with A.stream_to(sink):
        results = run_async(
            A.aexecute_each(
                [agent, agent],
                ["p0", "p1"],
                [tmp_path, tmp_path],
                lambda aid: 30,
                labels=["pyagent-sub1", "pyagent-sub2"],
            )
        )
    assert all(r.status is TaskStatus.COMPLETED for r in results)
    assert sorted(sink.chunks) == ["pyagent-sub1", "pyagent-sub2"]
    assert all(b"hello" in v for v in sink.chunks.values())


def test_aexecute_each_without_labels_keys_by_agent_id(tmp_path):
    # Every other fan-out runs each agent once, so the unlabeled path must stay
    # byte-identical: the sink key is still the agent id.
    class _Sink:
        def __init__(self):
            self.keys: list[str] = []

        def feed(self, key, chunk):
            self.keys.append(key)

    sink = _Sink()
    with A.stream_to(sink):
        run_async(
            A.aexecute_each(
                [_PyAgent("print('x')")], ["p"], [tmp_path], lambda aid: 30
            )
        )
    assert set(sink.keys) == {"pyagent"}


def test_aexecute_each_rejects_label_length_mismatch(tmp_path):
    with pytest.raises(ValueError, match="one label per agent"):
        run_async(
            A.aexecute_each(
                [ClaudeAgent(), CodexAgent()],
                ["a", "b"],
                [tmp_path, tmp_path],
                lambda aid: 5,
                labels=["only-one"],
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


_INVALID_UTF8_SCRIPT = (
    "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe bytes\\n'); sys.exit(0)"
)


def test_execute_real_subprocess_invalid_utf8_does_not_raise(tmp_path):
    # An agent CLI can emit non-UTF-8 (a latin-1 path in an error message, a
    # binary blob). Under strict decoding that raises UnicodeDecodeError — a
    # ValueError none of execute's handlers catch — and kills the whole run.
    agent = _PyAgent(_INVALID_UTF8_SCRIPT)
    r = agent.execute("p", tmp_path, timeout=30)  # must not raise
    assert r.status is TaskStatus.COMPLETED
    assert "ok" in r.output and "bytes" in r.output


def test_execute_and_aexecute_agree_on_invalid_utf8(tmp_path):
    # The documented contract is that the two paths classify identically; strict
    # decoding on one side silently broke it.
    agent = _PyAgent(_INVALID_UTF8_SCRIPT)
    sync = agent.execute("p", tmp_path, timeout=30)
    async_ = run_async(agent.aexecute("p", tmp_path, timeout=30))
    assert sync.status is async_.status
    assert sync.output == async_.output


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


# ── provider usage telemetry (TokenUsage) ──────────────────────────────────


def test_claude_usage_parsed_and_normalized():
    stdout = json.dumps({
        "type": "result", "is_error": False, "result": "done",
        "session_id": "s1", "total_cost_usd": 0.42,
        "usage": {
            "input_tokens": 900,
            "cache_read_input_tokens": 34_200,
            "cache_creation_input_tokens": 5_000,
            "output_tokens": 2_300,
        },
    })
    usage = ClaudeAgent()._usage_from_output(stdout)
    # Claude's input_tokens EXCLUDES cache tokens — total_input sums them in.
    assert usage.total_input_tokens == 900 + 34_200 + 5_000
    assert usage.cache_read_tokens == 34_200
    assert usage.cache_creation_tokens == 5_000
    assert usage.output_tokens == 2_300
    assert usage.total_cost_usd == 0.42
    assert round(usage.cache_hit_ratio, 3) == round(34_200 / 40_100, 3)
    assert usage.format_compact() == "cache 85% (34.2k/40.1k)"


def test_codex_usage_summed_across_turns():
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "t1"}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 10_000, "cached_input_tokens": 8_000,
            "output_tokens": 500,
        }}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 12_000, "cached_input_tokens": 11_000,
            "output_tokens": 700,
        }}),
    ]
    usage = CodexAgent()._usage_from_output("\n".join(lines))
    # codex input_tokens INCLUDES cached — summed verbatim, never re-added.
    assert usage.total_input_tokens == 22_000
    assert usage.cache_read_tokens == 19_000
    assert usage.output_tokens == 1_200
    assert usage.total_cost_usd is None


def test_usage_absent_or_malformed_is_none_and_never_raises():
    assert KimiCodeAgent()._usage_from_output(
        '{"role":"assistant","content":"hi"}'
    ) is None
    assert CodexAgent()._usage_from_output("not json at all") is None
    # Malformed usage values (strings, bools, negatives) degrade to None.
    bad = json.dumps({
        "type": "result", "is_error": False, "result": "x",
        "usage": {"input_tokens": "many", "output_tokens": True},
    })
    assert ClaudeAgent()._usage_from_output(bad) is None
    # A result event with NO usage dict is also None.
    assert ClaudeAgent()._usage_from_output(
        json.dumps({"type": "result", "is_error": False, "result": "x"})
    ) is None


def test_build_result_attaches_usage_best_effort():
    stdout = json.dumps({
        "type": "result", "is_error": False, "result": "done",
        "usage": {"input_tokens": 100, "output_tokens": 10},
    })
    result = ClaudeAgent()._build_result(uuid4(), 0, stdout, "", 1.0)
    assert result.usage is not None
    assert result.usage.total_input_tokens == 100

    # A parser that raises must never fail the run — usage just stays None.
    agent = ClaudeAgent()
    agent._usage_from_output = lambda stdout: (_ for _ in ()).throw(RuntimeError("boom"))
    result = agent._build_result(uuid4(), 0, stdout, "", 1.0)
    assert result.status == TaskStatus.COMPLETED
    assert result.usage is None


# ── label-keyed session seeding ────────────────────────────────────────────


def test_session_seed_prefers_sink_label_over_agent_id(tmp_path):
    agent = ClaudeAgent()
    with provider_session_scope(
        True, {"member-a": "sid-member", "claude": "sid-agent"}
    ):
        token = A._SINK_LABEL.set("member-a")
        try:
            session_id, resume, _ = agent._session_for(tmp_path)
        finally:
            A._SINK_LABEL.reset(token)
        assert (session_id, resume) == ("sid-member", True)


def test_session_seed_falls_back_to_agent_id_without_label(tmp_path):
    agent = ClaudeAgent()
    with provider_session_scope(True, {"claude": "sid-agent"}):
        session_id, resume, _ = agent._session_for(tmp_path)
        assert (session_id, resume) == ("sid-agent", True)


def test_labeled_execution_never_inherits_the_agent_id_seed(tmp_path):
    # A label IS the seed namespace: a labeled member with no seed entry must
    # NOT fall back to the agent-id key, or one member of a duplicate-provider
    # room would resume another member's conversation. Claude still mints a
    # fresh id (resume=False); it just never RESUMES the wrong session.
    agent = ClaudeAgent()
    with provider_session_scope(True, {"claude": "sid-agent"}):
        token = A._SINK_LABEL.set("member-b")
        try:
            session_id, resume, _ = agent._session_for(tmp_path / "other")
        finally:
            A._SINK_LABEL.reset(token)
        assert resume is False
        assert session_id != "sid-agent"


# ── Process-group isolation + stdin detachment (P0-1) ──────────────────────
#
# Every agent subprocess must run detached from the user's terminal (no
# inherited stdin — N concurrent competitors would race for one tty) and, on
# POSIX, lead its own process group so a timeout kill reaches grandchildren
# (MCP servers holding the pipes) via killpg, not just the direct child.


def test_execute_detaches_stdin_and_starts_new_session(monkeypatch, tmp_path):
    cap = {}
    _patch_run(
        monkeypatch,
        result=_Completed(0, '{"type":"result","result":"ok","is_error":false}'),
        capture=cap,
    )
    ClaudeAgent().execute("p", tmp_path, timeout=5)
    assert cap["kwargs"]["stdin"] is subprocess.DEVNULL
    assert cap["kwargs"]["start_new_session"] is A._START_NEW_SESSION


def test_aexecute_detaches_stdin_and_starts_new_session(monkeypatch, tmp_path):
    cap = {}
    proc = _FakeProc(0, b'{"type":"result","result":"ok","is_error":false}')
    _patch_exec(monkeypatch, proc=proc, capture=cap)
    run_async(ClaudeAgent().aexecute("p", tmp_path, timeout=5))
    assert cap["kwargs"]["stdin"] is A.asyncio.subprocess.DEVNULL
    assert cap["kwargs"]["start_new_session"] is A._START_NEW_SESSION


def test_kill_process_group_skips_without_pid_or_after_reap(monkeypatch):
    calls = []
    monkeypatch.setattr(A.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(A, "_START_NEW_SESSION", True)
    # No pid attribute (the hermetic fakes): never signals anything.
    assert A._kill_process_group(_FakeProc(returncode=None)) is False
    # Already reaped (returncode set): the pid may be RECYCLED — never signal.
    reaped = _FakeProc(returncode=0)
    reaped.pid = 12345
    assert A._kill_process_group(reaped) is False
    assert calls == []


def test_kill_process_group_signals_a_live_group(monkeypatch):
    calls = []
    monkeypatch.setattr(A.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(A, "_START_NEW_SESSION", True)
    live = _FakeProc(returncode=None)
    live.pid = 4242
    assert A._kill_process_group(live) is True
    assert calls == [(4242, A.signal.SIGKILL)]


def test_kill_process_group_survives_esrch(monkeypatch):
    def boom(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(A.os, "killpg", boom)
    monkeypatch.setattr(A, "_START_NEW_SESSION", True)
    live = _FakeProc(returncode=None)
    live.pid = 4242
    assert A._kill_process_group(live) is False  # never raises


def test_terminate_kills_the_group_before_the_child(monkeypatch):
    order = []
    monkeypatch.setattr(A.os, "killpg", lambda pid, sig: order.append("group"))
    monkeypatch.setattr(A, "_START_NEW_SESSION", True)

    proc = _FakeProc(returncode=None)
    proc.pid = 4242
    original_kill = proc.kill
    proc.kill = lambda: (order.append("child"), original_kill())[1]
    run_async(A._terminate(proc))
    assert order == ["group", "child"]
    assert proc.waited


# ── single-parse transcript memoization (P1-3) ─────────────────────────────


def test_claude_transcript_is_parsed_once_and_released(monkeypatch, tmp_path):
    # _build_result + _attach_provider_session re-derive the transcript through
    # five accessors; the memo must collapse that to ONE real parse (a verbose
    # agent used to block the shared event loop for five full passes) — and the
    # cache must be RELEASED once the result is built, or a long-lived process
    # pins its last potentially huge transcript forever.
    from functools import lru_cache

    parses: list[str] = []
    real_parse = A._claude_events.__wrapped__

    def counting(stdout):
        parses.append(stdout)
        return real_parse(stdout)

    monkeypatch.setattr(A, "_claude_events", lru_cache(maxsize=1)(counting))
    payload = '{"type":"result","result":"done","is_error":false,"session_id":"s1"}'
    _patch_run(monkeypatch, result=_Completed(0, payload))
    r = ClaudeAgent().execute("p", tmp_path, timeout=5)
    assert r.status is TaskStatus.COMPLETED
    assert len(parses) == 1  # one real parse served every accessor
    assert A._claude_events.cache_info().currsize == 0  # released after the result


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_sync_run_timeout_kills_the_whole_group(tmp_path):
    # The sync path used subprocess.run, which kills only the DIRECT child on
    # timeout and never exposes the pid — a grandchild (MCP server) survived.
    # _sync_run retains the Popen and killpgs the group.
    import os
    import signal
    import time as time_mod

    pidfile = tmp_path / "gpid"
    code = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(600)\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        A._sync_run(
            [sys.executable, "-c", code],
            timeout=5, text=True, start_new_session=True,
        )
    gpid = int(pidfile.read_text())
    deadline = time_mod.monotonic() + 5
    while time_mod.monotonic() < deadline:
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            return  # the grandchild died with the group
        time_mod.sleep(0.05)
    os.kill(gpid, signal.SIGKILL)  # cleanup before failing
    pytest.fail("grandchild survived the group kill")


def test_sync_run_interrupt_kills_the_detached_child(monkeypatch):
    # start_new_session detaches the child from the terminal's foreground
    # group, so the SIGINT that unwinds _sync_run never reaches it — without an
    # explicit kill on the interrupt path the agent keeps running (and editing)
    # after the user believes the run was cancelled.
    created = {}
    real_popen = subprocess.Popen

    class SpyPopen(real_popen):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            created["proc"] = self

        def communicate(self, *a, **k):
            raise KeyboardInterrupt

    monkeypatch.setattr(A.subprocess, "Popen", SpyPopen)
    with pytest.raises(KeyboardInterrupt):
        A._sync_run(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            timeout=30, text=True,
            start_new_session=A._START_NEW_SESSION,
        )
    proc = created["proc"]
    assert proc.returncode is not None  # killed and reaped before propagating


def test_sync_run_double_timeout_keeps_first_partial_buffers(monkeypatch):
    # If the post-kill reap ALSO times out (a grandchild still holds the
    # pipes), the re-raised TimeoutExpired must keep the FIRST timeout's
    # partial buffers rather than dropping them to None.
    class FakeProc:
        returncode = None
        calls = 0

        def communicate(self, timeout=None):
            FakeProc.calls += 1
            if FakeProc.calls == 1:
                raise subprocess.TimeoutExpired(
                    "cmd", 1, output="partial-out", stderr="partial-err"
                )
            raise subprocess.TimeoutExpired("cmd", 1)

        def kill(self):
            pass

        def wait(self, timeout=None):
            return None

        def poll(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(A.subprocess, "Popen", lambda *a, **k: FakeProc())
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        A._sync_run(["cmd"], timeout=1)
    assert ei.value.output == "partial-out"
    assert ei.value.stderr == "partial-err"


class _UnreapableProc:
    """A child the bounded reap cannot reap: any UNBOUNDED wait() is the hang
    Popen.__exit__ would have caused — fail loudly instead of hanging."""

    def __init__(self, comm_exc):
        self.returncode = None
        self.stdout = self.stderr = self.stdin = None
        self._comm_exc = comm_exc

    def communicate(self, input=None, timeout=None):
        raise self._comm_exc

    def kill(self):
        pass

    def poll(self):
        return None

    def wait(self, timeout=None):
        assert timeout is not None, "unbounded wait() after the bounded reap"
        raise subprocess.TimeoutExpired("cmd", timeout)


def test_sync_run_never_waits_unbounded_on_an_unreapable_child(monkeypatch):
    # Both give-up paths must exit without ANY unbounded wait — Popen.__exit__
    # ends with one, which is why the runner no longer uses the context manager.
    monkeypatch.setattr(
        A.subprocess, "Popen",
        lambda *a, **k: _UnreapableProc(subprocess.TimeoutExpired("cmd", 1)),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        A._sync_run(["cmd"], timeout=1)

    monkeypatch.setattr(
        A.subprocess, "Popen", lambda *a, **k: _UnreapableProc(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        A._sync_run(["cmd"], timeout=1)


def test_kill_process_group_owned_kills_even_after_reap(monkeypatch):
    # A fast-exiting leader whose grandchild still holds the pipes leaves that
    # grandchild as the group's surviving member — an OWNED group (we launched
    # it with start_new_session) must still be killed after the leader was
    # reaped; POSIX reserves the pgid from reuse while the group has members.
    calls = []
    monkeypatch.setattr(A.os, "killpg", lambda pid, sig: calls.append((pid, sig)))
    monkeypatch.setattr(A, "_START_NEW_SESSION", True)
    reaped = _FakeProc(returncode=0)
    reaped.pid = 777
    assert A._kill_process_group(reaped, owned=True) is True
    assert calls == [(777, A.signal.SIGKILL)]
    # An UN-owned reaped process keeps the strict recycled-pid guard.
    assert A._kill_process_group(reaped) is False
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="process groups are POSIX")
def test_terminate_kills_grandchildren_after_the_leader_was_reaped(tmp_path):
    # codex round-6 (reproduced): the leader exits immediately and is reaped
    # (returncode set), the grandchild lives on in the group — _terminate must
    # still kill the group, not skip on the reaped-leader guard. Built on a
    # REAL process group but a reaped-leader shim rather than awaiting asyncio's
    # proc.wait(), which is exactly the call a pipe-holding grandchild wedges
    # (the _REAP_TIMEOUT_SECONDS rationale) — the semantics under test are the
    # guard's, not the transport's.
    import os
    import signal
    import time as time_mod

    pidfile = tmp_path / "gpid"
    code = (
        "import subprocess, sys\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", code], start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert leader.wait(timeout=10) == 0  # leader reaped; grandchild survives

    class _ReapedLeader:
        pid = leader.pid
        returncode = leader.returncode

        def kill(self):
            raise ProcessLookupError  # what asyncio raises for a reaped child

        async def wait(self):  # pragma: no cover - unreachable after kill()
            return self.returncode

    run_async(A._terminate(_ReapedLeader()))
    gpid = int(pidfile.read_text())
    deadline = time_mod.monotonic() + 5
    while time_mod.monotonic() < deadline:
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            return  # the grandchild died with the group
        time_mod.sleep(0.05)
    os.kill(gpid, signal.SIGKILL)  # cleanup before failing
    pytest.fail("grandchild survived _terminate after the leader was reaped")
