from __future__ import annotations

import pytest

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


def _kimi_event(spool, name="mcp__gate_e__always_ok"):
    collect_hook_event(
        provider="kimi-code", event="success", spool=spool,
        payload={"tool_name": name},
    )


def test_torn_spool_line_never_reuses_a_sequence_number(tmp_path):
    # Sequence comes from the {spool}.seq sidecar, not a record COUNT: a torn
    # line is skipped by the reader, so a count under-reports and the next
    # record would reuse the torn record's sequence number (which the reader
    # sorts by). Simulate _append's own crash — sequence 3 reserved in the
    # sidecar, the spool line torn mid-write.
    spool = tmp_path / "hook.ndjson"
    _kimi_event(spool)
    _kimi_event(spool)
    (tmp_path / "hook.ndjson.seq").write_text("3", encoding="utf-8")
    with spool.open("a", encoding="utf-8") as handle:
        handle.write('{"source": "kimi-code", "sequence": 3, "tool_ke\n')  # torn
    _kimi_event(spool)
    telemetry = load_hook_telemetry(spool, source="kimi-code")
    assert [call.sequence for call in telemetry.calls] == [1, 2, 4]


def test_torn_spool_line_marks_telemetry_incomplete(tmp_path):
    spool = tmp_path / "hook.ndjson"
    _kimi_event(spool)
    with spool.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    telemetry = load_hook_telemetry(spool, source="kimi-code")
    assert telemetry.complete is False
    assert telemetry.malformed_count == 1
    assert len(telemetry.calls) == 1


def test_seq_sidecar_write_failure_never_truncates_the_old_value(
    tmp_path, monkeypatch
):
    # The sidecar write is atomic (unique temp + fsync + replace): a plain
    # write_text truncates first, so a crash mid-write could leave a valid
    # PREFIX of the old value ("12" → "1") that _next_sequence would trust —
    # re-minting already-used numbers. A failed reserve must leave the old
    # value byte-identical.
    import syncmill.telemetry_hook as th

    spool = tmp_path / "hook.ndjson"
    for _ in range(12):
        _kimi_event(spool)
    seq_path = tmp_path / "hook.ndjson.seq"
    assert seq_path.read_text(encoding="utf-8") == "12"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(th.os, "replace", boom)
    with pytest.raises(OSError):
        _kimi_event(spool)
    monkeypatch.undo()
    assert seq_path.read_text(encoding="utf-8") == "12"
    _kimi_event(spool)
    telemetry = load_hook_telemetry(spool, source="kimi-code")
    sequences = [call.sequence for call in telemetry.calls]
    assert len(sequences) == len(set(sequences))


def test_a_reserved_but_unwritten_sequence_marks_telemetry_incomplete(tmp_path):
    # A crash BETWEEN the sidecar reserve and the spool append loses an event
    # with no torn line and no .incomplete marker — the reservation (and,
    # after later events, the numbering gap) is the only evidence, and it must
    # flip `complete` so the consumer never trusts a spool that dropped one.
    spool = tmp_path / "hook.ndjson"
    _kimi_event(spool)
    _kimi_event(spool)
    (tmp_path / "hook.ndjson.seq").write_text("3", encoding="utf-8")  # reserved, unwritten
    telemetry = load_hook_telemetry(spool, source="kimi-code")
    assert telemetry.complete is False
    _kimi_event(spool)  # the next event lands on 4, leaving the internal gap
    telemetry = load_hook_telemetry(spool, source="kimi-code")
    assert [call.sequence for call in telemetry.calls] == [1, 2, 4]
    assert telemetry.complete is False


def test_legacy_spool_without_seq_sidecar_seeds_from_max_sequence(tmp_path):
    # A spool written before the sidecar existed: the next sequence must come
    # from the highest number on disk — max, not count: with a gap on disk
    # (sequences 1 and 3), a count seed would re-mint the LIVE number 3.
    spool = tmp_path / "hook.ndjson"
    spool.write_text(
        '{"source":"kimi-code","sequence":1,"tool_key":"a::b","outcome":"succeeded"}\n'
        '{"source":"kimi-code","sequence":3,"tool_key":"a::b","outcome":"succeeded"}\n',
        encoding="utf-8",
    )
    _kimi_event(spool)
    telemetry = load_hook_telemetry(spool, source="kimi-code")
    assert [call.sequence for call in telemetry.calls] == [1, 3, 4]


def test_a_lost_first_event_marks_telemetry_incomplete(tmp_path):
    # Sequences mint from 1, so completeness is exactly 1..max — a min-to-max
    # contiguity check would miss a lost PREFIX: event 1 reserved and lost,
    # event 2 recorded → the singleton [2] looked contiguous and the sidecar
    # (2) matched, silently flipping complete back to True.
    spool = tmp_path / "hook.ndjson"
    (tmp_path / "hook.ndjson.seq").write_text("1", encoding="utf-8")  # lost event 1
    _kimi_event(spool)  # lands on 2
    telemetry = load_hook_telemetry(spool, source="kimi-code")
    assert [call.sequence for call in telemetry.calls] == [2]
    assert telemetry.complete is False
