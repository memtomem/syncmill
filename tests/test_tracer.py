"""FileTracer / build_tracer unit tests (S3′).

Hermetic and tracegraph-free: the OTLP document shape is asserted directly
against the T1 contract (protobuf-JSON attribute encoding, flat parentSpanId
containment, in-trace links, bounded error status) plus a pure-python
mini-validator mirroring what tracegraph's ``OTLPSpanAdapter`` would raise on
(duplicate spanIds, dangling parents, unresolvable logical parents).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import types
from pathlib import Path

from syncmill.config import TraceConfig
from syncmill.tracer import (
    TRACE_FILE,
    FileTracer,
    SpanRecord,
    TeeTracer,
    Tracer,
    build_tracer,
    flush_trace,
    run_span,
)


def _id_factory():
    """Deterministic ids: 1st call is the 32-hex trace id, then 16-hex span ids."""
    counter = {"n": 0}

    def factory(nbytes: int) -> str:
        counter["n"] += 1
        return f"{counter['n']:0{nbytes * 2}x}"

    return factory


def _tracer(tmp_path, run_id="run-1") -> FileTracer:
    return FileTracer(run_id, tmp_path / run_id, id_factory=_id_factory())


def _attempt(node_id="attempt:claude:0", parent="run", **kw) -> SpanRecord:
    defaults = dict(
        name="attempt:claude",
        kind="AGENT",
        node_id=node_id,
        node_parent=parent,
        start=1.0,
        end=2.0,
        status="completed",
        attrs={"phase": "attempt", "agent_id": "claude", "attempt": 0,
               "status": "completed", "exit_code": 0, "winner": True},
    )
    defaults.update(kw)
    return SpanRecord(**defaults)


def _spans_of(path: Path) -> list[dict]:
    doc = json.loads(path.read_text())
    return doc["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _attrs(span: dict) -> dict:
    return {a["key"]: a["value"] for a in span["attributes"]}


# ── build_tracer ─────────────────────────────────────────────────────────────


def test_build_tracer_disabled_returns_none():
    assert build_tracer(TraceConfig(), run_id="r", fallback_dir="/nowhere") is None


def test_build_tracer_enabled_uses_fallback_dir(tmp_path):
    cfg = TraceConfig(enabled=True)
    tracer = build_tracer(cfg, run_id="rid", fallback_dir=str(tmp_path / "runs"))
    assert isinstance(tracer, FileTracer)
    assert tracer._out_dir == tmp_path / "runs" / "rid"


def test_build_tracer_explicit_dir_wins(tmp_path):
    cfg = TraceConfig(enabled=True, dir=str(tmp_path / "traces"))
    tracer = build_tracer(cfg, run_id="rid", fallback_dir=str(tmp_path / "runs"))
    assert tracer._out_dir == tmp_path / "traces" / "rid"


def test_build_tracer_otlp_http_missing_extra_keeps_file_and_reports_stream_error(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.setitem(sys.modules, "syncmill.phoenix_exporter", None)
    cfg = TraceConfig(enabled=True, exporter="otlp-http")
    with caplog.at_level(logging.WARNING):
        tracer = build_tracer(cfg, run_id="r", fallback_dir=str(tmp_path))
    assert isinstance(tracer, TeeTracer)
    tracer.record(run_span(strategy="route", status="completed", start=0.0, end=1.0))
    block = flush_trace(tracer)
    assert block["status"] == "ok"  # durable file remains canonical
    assert block["stream"] == {
        "backend": "phoenix",
        "protocol": "otlp-http/protobuf",
        "status": "error",
        "exported_span_count": 0,
        "dropped_span_count": 1,
    }
    assert any("syncmill[phoenix]" in r.message for r in caplog.records)


def test_build_tracer_otlp_http_tees_same_records_to_stream(tmp_path, monkeypatch):
    seen = {}

    class _FakeStream:
        def __init__(self, **kwargs):
            seen["kwargs"] = kwargs
            seen["records"] = []

        def record(self, span):
            seen["records"].append(span)

        def flush(self):
            return {
                "backend": "phoenix",
                "protocol": "otlp-http/protobuf",
                "status": "ok",
                "exported_span_count": len(seen["records"]),
                "dropped_span_count": 0,
            }

    module = types.ModuleType("syncmill.phoenix_exporter")
    module.PhoenixSpanSink = _FakeStream
    monkeypatch.setitem(sys.modules, "syncmill.phoenix_exporter", module)
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", "https://phoenix.test/space")
    monkeypatch.setenv("PHOENIX_PROJECT_NAME", "project-a")

    tracer = build_tracer(
        TraceConfig(enabled=True, exporter="otlp-http"),
        run_id="rid",
        fallback_dir=str(tmp_path),
        run_start=12.5,
    )
    assert isinstance(tracer, TeeTracer)
    record = run_span(strategy="route", status="completed", start=12.5, end=13.0)
    tracer.record(record)
    block = flush_trace(tracer)

    assert seen["records"] == [record]
    assert seen["kwargs"]["trace_id"] == tracer.trace_id
    assert seen["kwargs"]["run_start"] == 12.5
    assert seen["kwargs"]["endpoint"] == "https://phoenix.test/space/v1/traces"
    assert seen["kwargs"]["project_name"] == "project-a"
    assert block["stream"]["status"] == "ok"


def test_build_tracer_include_output_warn_and_ignored(tmp_path, caplog):
    cfg = TraceConfig(enabled=True, include_output=True)
    with caplog.at_level(logging.WARNING):
        tracer = build_tracer(cfg, run_id="r", fallback_dir=str(tmp_path))
    assert isinstance(tracer, FileTracer)
    assert any("include_output" in r.message for r in caplog.records)


def test_file_tracer_satisfies_protocol(tmp_path):
    assert isinstance(_tracer(tmp_path), Tracer)


# ── document shape (the T1 byte contract) ────────────────────────────────────


def test_flush_document_shape(tmp_path):
    tracer = _tracer(tmp_path)
    assert tracer.trace_id == f"{1:032x}"  # id_factory's 1st call: the trace id
    tracer.record(run_span(strategy="compete", status="completed", start=0.5, end=3.0))
    tracer.record(_attempt(attrs={"phase": "attempt", "agent_id": "claude",
                                  "attempt": 0, "status": "completed",
                                  "exit_code": 0, "winner": True,
                                  "files_changed_count": 2}))
    tracer.record(
        SpanRecord(
            name="select",
            kind="CHAIN",
            node_id="select",
            node_parent="attempt:claude:0",
            links=("attempt:claude:0",),
            start=2.0,
            end=2.5,
            attrs={"phase": "select", "agent_id": "supervisor", "status": "completed"},
        )
    )
    path = tracer.flush()
    assert path == tmp_path / "run-1" / TRACE_FILE
    spans = _spans_of(path)
    assert [s["name"] for s in spans] == ["syncmill.run", "attempt:claude", "select"]

    root, attempt, select = spans
    # ids: 32-hex trace id shared, 16-hex unique span ids
    assert {s["traceId"] for s in spans} == {tracer.trace_id}
    assert len(tracer.trace_id) == 32
    assert len({s["spanId"] for s in spans}) == 3
    assert all(len(s["spanId"]) == 16 for s in spans)
    # flat containment: root has NO parentSpanId, every other span parents on it
    assert "parentSpanId" not in root
    assert attempt["parentSpanId"] == root["spanId"]
    assert select["parentSpanId"] == root["spanId"]
    # timestamps are unix-nano STRINGS (protobuf-JSON int64)
    assert root["startTimeUnixNano"] == str(int(0.5 * 1e9))
    assert root["endTimeUnixNano"] == str(int(3.0 * 1e9))
    # attribute encoding: bool→boolValue, int→intValue-as-string, str→stringValue
    attempt_attrs = _attrs(attempt)
    assert attempt_attrs["openinference.span.kind"] == {"stringValue": "AGENT"}
    assert attempt_attrs["graph.node.id"] == {"stringValue": "attempt:claude:0"}
    assert attempt_attrs["graph.node.parent_id"] == {"stringValue": "run"}
    assert attempt_attrs["syncmill.schema_version"] == {"intValue": "1"}
    assert attempt_attrs["syncmill.run_id"] == {"stringValue": "run-1"}
    assert attempt_attrs["syncmill.exit_code"] == {"intValue": "0"}
    assert attempt_attrs["syncmill.winner"] == {"boolValue": True}
    assert attempt_attrs["syncmill.files_changed_count"] == {"intValue": "2"}
    # the root carries no graph.node.parent_id (node_parent=None)
    assert "graph.node.parent_id" not in _attrs(root)
    # links resolve to in-trace {traceId, spanId}
    assert select["links"] == [
        {"traceId": tracer.trace_id, "spanId": attempt["spanId"]}
    ]
    # completed spans omit the status field entirely (the golden shape)
    assert "status" not in root and "status" not in attempt and "status" not in select


def test_flush_error_status_bounded_message(tmp_path):
    tracer = _tracer(tmp_path)
    tracer.record(run_span(strategy="route", status="failed", start=0.0, end=1.0))
    tracer.record(
        _attempt(status="timeout", error="timeout",
                 attrs={"phase": "attempt", "agent_id": "claude", "attempt": 0,
                        "status": "timeout"})
    )
    spans = _spans_of(tracer.flush())
    assert spans[0]["status"] == {"code": "STATUS_CODE_ERROR", "message": "failed"}
    assert spans[1]["status"] == {"code": "STATUS_CODE_ERROR", "message": "timeout"}


def test_flush_unknown_tool_outcome_is_not_otlp_error(tmp_path):
    tracer = _tracer(tmp_path)
    tracer.record(
        SpanRecord(
            name="gate_e::maybe",
            kind="TOOL",
            node_id="tool:1",
            node_parent=None,
            status="unknown",
            attrs={"phase": "tool", "agent_id": "kimi-code", "status": "unknown"},
        )
    )
    span = _spans_of(tracer.flush())[0]
    assert "status" not in span


def test_flush_drops_out_of_allowlist_attr(tmp_path, caplog):
    tracer = _tracer(tmp_path)
    tracer.record(_attempt(parent=None, attrs={"phase": "attempt", "agent_id": "claude",
                                  "attempt": 0, "status": "completed",
                                  "prompt": "SECRET BODY"}))
    with caplog.at_level(logging.WARNING):
        path = tracer.flush()
    text = path.read_text()
    assert "SECRET BODY" not in text and "syncmill.prompt" not in text
    assert any("allowlist" in r.message for r in caplog.records)


def test_flush_dangling_link_degrades(tmp_path):
    # A dangling link would make tracegraph's adapter reject the document, so
    # the flush fails all-or-nothing (no artifact) rather than emit a lossy one.
    tracer = _tracer(tmp_path)
    tracer.record(run_span(strategy="compete", status="completed", start=0.0, end=1.0))
    tracer.record(_attempt(links=("attempt:ghost",)))
    assert tracer.flush() is None
    assert tracer.degraded and "unrecorded" in (tracer.last_error or "")
    assert not (tmp_path / "run-1" / TRACE_FILE).exists()  # no partial file


def test_flush_unresolved_parent_degrades(tmp_path):
    # An unresolved graph.node.parent_id is the same class of rejectable defect.
    tracer = _tracer(tmp_path)
    tracer.record(_attempt(parent="attempt:ghost"))
    assert tracer.flush() is None
    assert tracer.degraded and "unresolved" in (tracer.last_error or "")
    assert not (tmp_path / "run-1" / TRACE_FILE).exists()


def test_flush_bounds_status_message_length(tmp_path):
    tracer = _tracer(tmp_path)
    tracer.record(
        _attempt(parent=None, status="failed", error="gate: " + "x" * 500,
                 attrs={"phase": "attempt", "agent_id": "claude", "attempt": 0,
                        "status": "failed"})
    )
    spans = _spans_of(tracer.flush())
    assert len(spans[0]["status"]["message"]) == 200  # capped, no unbounded body


def test_flush_duplicate_node_id_degrades(tmp_path, caplog):
    tracer = _tracer(tmp_path)
    tracer.record(_attempt())
    tracer.record(_attempt())  # same node_id — the adapter would raise on the doc
    with caplog.at_level(logging.WARNING):
        assert tracer.flush() is None
    assert tracer.degraded and "duplicate" in (tracer.last_error or "")
    assert not (tmp_path / "run-1" / TRACE_FILE).exists()  # no partial file


def test_flush_unwritable_dir_degrades(tmp_path):
    target = tmp_path / "run-1"
    target.mkdir()
    tracer = FileTracer("run-1", target, id_factory=_id_factory())
    tracer.record(_attempt(parent=None))
    os.chmod(target, stat.S_IRUSR | stat.S_IXUSR)  # read-only dir
    try:
        assert tracer.flush() is None
        assert tracer.degraded
    finally:
        os.chmod(target, stat.S_IRWXU)
    assert not (target / TRACE_FILE).exists()


def test_record_never_raises_after_flush_state(tmp_path):
    tracer = _tracer(tmp_path)
    tracer.record(_attempt(parent=None))  # plain append — must never raise
    assert len(tracer._spans) == 1


# ── flush_trace (the envelope block) ─────────────────────────────────────────


def test_flush_trace_ok_block(tmp_path):
    tracer = _tracer(tmp_path)
    tracer.record(run_span(strategy="route", status="completed", start=0.0, end=1.0))
    block = flush_trace(tracer)
    path = tmp_path / "run-1" / TRACE_FILE
    assert block["status"] == "ok"
    assert block["protocol"] == "otlp-json-file"
    assert block["trace_id"] == tracer.trace_id and len(block["trace_id"]) == 32
    assert block["artifact"] == TRACE_FILE  # relative, envelope-dir based
    import hashlib

    assert block["digest"] == f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def test_flush_trace_error_block_on_failed_flush(tmp_path):
    tracer = _tracer(tmp_path)
    tracer.record(_attempt())
    tracer.record(_attempt())  # duplicate node_id → flush returns None
    assert flush_trace(tracer) == {"status": "error"}


def test_flush_trace_error_block_on_raising_tracer():
    class _Boom:
        trace_id = "0" * 32

        def flush(self):
            raise OSError("disk gone")

    assert flush_trace(_Boom()) == {"status": "error"}
