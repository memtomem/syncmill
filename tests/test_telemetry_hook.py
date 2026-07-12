from __future__ import annotations

from syncmill.models import ToolOutcome
from syncmill.telemetry_hook import collect_hook_event, load_hook_telemetry


def test_antigravity_hook_pairs_without_persisting_bodies(tmp_path):
    spool = tmp_path / "hook.ndjson"
    collect_hook_event(
        provider="antigravity",
        event="pre",
        spool=spool,
        payload={
            "conversationId": "c1", "stepIdx": 7,
            "toolCall": {"name": "mcp__gate_e__always_fail", "args": {"secret": "SENTINEL"}},
        },
    )
    collect_hook_event(
        provider="antigravity",
        event="post",
        spool=spool,
        payload={"conversationId": "c1", "stepIdx": 7, "error": "SENTINEL"},
    )
    telemetry = load_hook_telemetry(spool, source="antigravity")
    assert telemetry.complete
    assert telemetry.calls[0].tool_key == "gate_e::always_fail"
    assert telemetry.calls[0].outcome is ToolOutcome.FAILED
    assert "SENTINEL" not in spool.read_text()


def test_kimi_hook_terminal_events_are_typed(tmp_path):
    spool = tmp_path / "hook.ndjson"
    collect_hook_event(
        provider="kimi-code", event="success", spool=spool,
        payload={"tool_name": "mcp__gate_e__always_ok", "tool_input": "SECRET"},
    )
    collect_hook_event(
        provider="kimi-code", event="failure", spool=spool,
        payload={"tool_name": "mcp__gate_e__always_fail", "error": "SECRET"},
    )
    telemetry = load_hook_telemetry(spool, source="kimi-code")
    assert [call.outcome for call in telemetry.calls] == [
        ToolOutcome.SUCCEEDED, ToolOutcome.FAILED
    ]
    assert "SECRET" not in spool.read_text()
