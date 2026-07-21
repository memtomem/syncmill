"""Opt-in real Phoenix OTLP/HTTP exporter smoke against a local receiver."""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

if os.environ.get("SYNCMILL_TEST_PHOENIX_REAL") != "1":
    pytest.skip(
        "set SYNCMILL_TEST_PHOENIX_REAL=1 to run the Phoenix exporter smoke",
        allow_module_level=True,
    )

pytest.importorskip("phoenix.otel")
trace_service = pytest.importorskip(
    "opentelemetry.proto.collector.trace.v1.trace_service_pb2"
)

from syncmill.config import TraceConfig  # noqa: E402 - optional dependency guard
from syncmill.tracer import (  # noqa: E402 - optional dependency guard
    SpanRecord,
    build_tracer,
    flush_trace,
    run_span,
)

pytestmark = pytest.mark.integration


class _Receiver(ThreadingHTTPServer):
    bodies: list[bytes]
    headers_seen: list[dict[str, str]]
    paths: list[str]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0"))
        self.server.bodies.append(self.rfile.read(length))  # type: ignore[attr-defined]
        self.server.headers_seen.append(dict(self.headers))  # type: ignore[attr-defined]
        self.server.paths.append(self.path)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()
        self.wfile.write(b"")

    def log_message(self, format: str, *args: object) -> None:
        return None


def test_real_phoenix_http_exporter_sends_body_free_otlp(tmp_path, monkeypatch):
    receiver = _Receiver(("127.0.0.1", 0), _Handler)
    receiver.bodies = []
    receiver.headers_seen = []
    receiver.paths = []
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("PHOENIX_API_KEY", "integration-secret")

    try:
        tracer = build_tracer(
            TraceConfig(
                enabled=True,
                exporter="otlp-http",
                endpoint=f"http://127.0.0.1:{receiver.server_port}",
                project_name="syncmill-smoke",
                batch_delay_seconds=0.01,
                timeout_seconds=2,
            ),
            run_id="run-smoke",
            fallback_dir=str(tmp_path),
            run_start=10.0,
        )
        assert tracer is not None
        tracer.record(
            SpanRecord(
                name="attempt:codex",
                kind="AGENT",
                node_id="attempt:codex:0",
                node_parent="run",
                start=11.0,
                end=12.0,
                attrs={"agent_id": "codex", "status": "completed"},
            )
        )
        tracer.record(run_span(strategy="route", status="completed", start=10.0, end=13.0))
        receipt = flush_trace(tracer)
    finally:
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=2)

    assert receipt["status"] == "ok"
    assert receipt["stream"] == {
        "backend": "phoenix",
        "protocol": "otlp-http/protobuf",
        "status": "ok",
        "exported_span_count": 2,
        "dropped_span_count": 0,
    }
    assert receiver.paths == ["/v1/traces"]
    headers = {key.lower(): value for key, value in receiver.headers_seen[0].items()}
    assert headers["authorization"] == "Bearer integration-secret"

    request = trace_service.ExportTraceServiceRequest()
    request.ParseFromString(receiver.bodies[0])
    resource_spans = request.resource_spans[0]
    resource_attrs = {attr.key: attr.value.string_value for attr in resource_spans.resource.attributes}
    assert resource_attrs["openinference.project.name"] == "syncmill-smoke"
    spans = list(resource_spans.scope_spans[0].spans)
    assert {span.name for span in spans} == {"attempt:codex", "syncmill.run"}
    assert len({span.trace_id for span in spans}) == 1
    assert b"integration-secret" not in receiver.bodies[0]
