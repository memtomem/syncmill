"""Hermetic tests for the optional Phoenix OTLP/HTTP streaming sink."""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("phoenix.otel")
pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.trace import StatusCode

import syncmill.phoenix_exporter as P
from syncmill.phoenix_exporter import PhoenixSpanSink, _BoundedBatchSpanProcessor
from syncmill.tracer import SpanRecord, _span_attributes, run_span


class _Exporter:
    def __init__(self, outcome=SpanExportResult.SUCCESS, block: threading.Event | None = None):
        self.outcome = outcome
        self.block = block
        self.batches = []
        self.closed = False

    def export(self, spans):
        if self.block is not None:
            self.block.wait(1.0)
        self.batches.append(tuple(spans))
        return self.outcome

    def shutdown(self):
        self.closed = True


def _attempt(**overrides):
    values = {
        "name": "attempt:claude",
        "kind": "AGENT",
        "node_id": "attempt:claude:0",
        "node_parent": "run",
        "start": 11.0,
        "end": 12.0,
        "status": "completed",
        "attrs": {
            "phase": "attempt",
            "agent_id": "claude",
            "attempt": 0,
            "status": "completed",
        },
    }
    values.update(overrides)
    return SpanRecord(**values)


def _sink(monkeypatch, exporter, *, trace_id="0" * 31 + "a"):
    monkeypatch.setattr(P, "HTTPSpanExporter", lambda **kwargs: exporter)
    return PhoenixSpanSink(
        run_id="run-1",
        trace_id=trace_id,
        run_start=10.0,
        endpoint="http://127.0.0.1:6006/v1/traces",
        project_name="syncmill-test",
        batch_delay_seconds=10.0,
        timeout_seconds=1,
        attribute_factory=lambda span: _span_attributes(span, "run-1"),
    )


def test_sink_exports_completed_spans_with_shared_trace_parent_and_links(monkeypatch):
    exporter = _Exporter()
    trace_id = "1234567890abcdef1234567890abcdef"
    sink = _sink(monkeypatch, exporter, trace_id=trace_id)
    attempt = _attempt()
    sink.record(attempt)
    sink.record(
        SpanRecord(
            name="select",
            kind="CHAIN",
            node_id="select",
            node_parent=attempt.node_id,
            links=(attempt.node_id,),
            start=12.0,
            end=12.1,
            attrs={"phase": "select", "agent_id": "supervisor", "status": "completed"},
        )
    )
    sink.record(run_span(strategy="compete", status="completed", start=10.0, end=13.0))

    receipt = sink.flush()
    spans = [span for batch in exporter.batches for span in batch]
    by_name = {span.name: span for span in spans}
    root = by_name["syncmill.run"]
    child = by_name["attempt:claude"]
    select = by_name["select"]

    assert receipt == {
        "backend": "phoenix",
        "protocol": "otlp-http/protobuf",
        "status": "ok",
        "exported_span_count": 3,
        "dropped_span_count": 0,
    }
    assert sink.flush() == receipt
    assert root.context.trace_id == int(trace_id, 16)
    assert {span.context.trace_id for span in spans} == {int(trace_id, 16)}
    assert child.parent.span_id == root.context.span_id
    assert select.parent.span_id == root.context.span_id  # physical containment stays flat
    assert select.links[0].context.span_id == child.context.span_id
    assert child.attributes["graph.node.parent_id"] == "run"
    assert select.attributes["graph.node.parent_id"] == attempt.node_id
    assert child.resource.attributes["openinference.project.name"] == "syncmill-test"
    assert exporter.closed


def test_sink_maps_error_status_and_rejects_dangling_link(monkeypatch):
    exporter = _Exporter()
    sink = _sink(monkeypatch, exporter)
    sink.record(_attempt(status="failed", error="exit_code=2"))
    sink.record(_attempt(node_id="bad", links=("ghost",)))
    sink.record(run_span(strategy="route", status="failed", start=10.0, end=13.0))

    receipt = sink.flush()
    spans = [span for batch in exporter.batches for span in batch]
    attempt = next(span for span in spans if span.name == "attempt:claude")
    assert attempt.status.status_code is StatusCode.ERROR
    assert attempt.status.description == "exit_code=2"
    assert receipt["status"] == "error"
    assert receipt["exported_span_count"] == 2
    assert receipt["dropped_span_count"] == 1


def test_export_failure_is_counted_without_raising(monkeypatch):
    exporter = _Exporter(SpanExportResult.FAILURE)
    sink = _sink(monkeypatch, exporter)
    sink.record(_attempt())
    sink.record(run_span(strategy="route", status="completed", start=10.0, end=13.0))
    receipt = sink.flush()
    assert receipt["status"] == "error"
    assert receipt["exported_span_count"] == 0
    assert receipt["dropped_span_count"] == 2


def test_batch_shutdown_honors_syncmill_deadline_when_exporter_stalls():
    release = threading.Event()
    exporter = _Exporter(block=release)
    processor = _BoundedBatchSpanProcessor(exporter, batch_delay_seconds=0.01)
    processor.on_end(object())
    started = time.monotonic()
    clean = processor.shutdown(timeout_seconds=0.05)
    elapsed = time.monotonic() - started
    assert not clean
    assert elapsed < 0.2
    assert processor.receipt()["status"] == "error"
    release.set()
    processor._worker.join(1.0)
    assert not processor._worker.is_alive()
