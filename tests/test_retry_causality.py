"""Explicit, body-free tool retry causality at the shared strategy trace seam."""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

from syncmill.models import (
    TaskResult,
    TaskStatus,
    ToolCallRecord,
    ToolOutcome,
    ToolTelemetry,
)
from syncmill.strategies.base import _trace_attempt
from syncmill.tracer import SpanRecord


class _CaptureTracer:
    trace_id = "0" * 32

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []

    def record(self, span: SpanRecord) -> None:
        self.spans.append(span)

    def flush(self):
        return None


def _attempt() -> SpanRecord:
    return SpanRecord(
        name="attempt:canary",
        kind="AGENT",
        node_id="attempt:canary:0",
        node_parent="run",
        start=1.0,
        end=2.0,
    )


def _result(calls: tuple[ToolCallRecord, ...], *, complete: bool = True) -> TaskResult:
    return TaskResult(
        task_id=uuid4(),
        agent_id="canary",
        status=TaskStatus.FAILED,
        output="body must never enter spans",
        exit_code=1,
        duration_seconds=1.0,
        tool_telemetry=ToolTelemetry(
            source="controlled-canary",
            complete=complete,
            calls=calls,
        ),
    )


def _call(
    sequence: int,
    tool: str = "syncmill::probe",
    *,
    retry_of: int | None = None,
    outcome: ToolOutcome = ToolOutcome.SUCCEEDED,
) -> ToolCallRecord:
    return ToolCallRecord(sequence, tool, outcome, retry_of_sequence=retry_of)


def test_valid_retry_records_marker_between_original_and_retried_tool() -> None:
    tracer = _CaptureTracer()
    _trace_attempt(
        tracer,
        _attempt(),
        _result((_call(1), _call(2, retry_of=1, outcome=ToolOutcome.FAILED))),
    )

    assert [(s.node_id, s.node_parent, s.kind) for s in tracer.spans] == [
        ("attempt:canary:0", "run", "AGENT"),
        ("attempt:canary:0:tool:1", "attempt:canary:0", "TOOL"),
        ("attempt:canary:0:retry:2", "attempt:canary:0:tool:1", "CHAIN"),
        ("attempt:canary:0:tool:2", "attempt:canary:0:retry:2", "TOOL"),
    ]
    marker = tracer.spans[2]
    assert marker.name == "retry:syncmill::probe"
    assert marker.attrs == {
        "phase": "retry",
        "agent_id": "canary",
        "status": "completed",
        "tool.key": "syncmill::probe",
        "tool.sequence": 2,
    }


def test_chained_retries_link_each_marker_to_the_immediate_producer() -> None:
    tracer = _CaptureTracer()
    _trace_attempt(
        tracer,
        _attempt(),
        _result(
            (
                _call(1),
                _call(2, retry_of=1),
                _call(3, retry_of=2, outcome=ToolOutcome.FAILED),
            )
        ),
    )

    by_id = {span.node_id: span for span in tracer.spans}
    assert by_id["attempt:canary:0:retry:2"].node_parent == "attempt:canary:0:tool:1"
    assert by_id["attempt:canary:0:retry:3"].node_parent == "attempt:canary:0:tool:2"
    assert by_id["attempt:canary:0:tool:3"].node_parent == "attempt:canary:0:retry:3"


def test_out_of_order_calls_are_recorded_parent_before_child() -> None:
    tracer = _CaptureTracer()
    _trace_attempt(
        tracer,
        _attempt(),
        _result((_call(2, retry_of=1, outcome=ToolOutcome.FAILED), _call(1))),
    )

    assert [span.node_id for span in tracer.spans[1:]] == [
        "attempt:canary:0:tool:1",
        "attempt:canary:0:retry:2",
        "attempt:canary:0:tool:2",
    ]


def test_duplicate_sequence_keeps_first_call_without_poisoning_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tracer = _CaptureTracer()
    with caplog.at_level(logging.WARNING):
        _trace_attempt(
            tracer,
            _attempt(),
            _result((_call(1), _call(1, "syncmill::duplicate"))),
        )

    tools = [span for span in tracer.spans if span.kind == "TOOL"]
    assert len(tools) == 1
    assert tools[0].name == "syncmill::probe"
    assert "ignored duplicate tool call sequence=1" in caplog.text


@pytest.mark.parametrize(
    ("calls", "complete"),
    [
        ((_call(2, retry_of=1),), True),  # missing target
        ((_call(1, retry_of=2), _call(2)), True),  # forward target
        ((_call(1, "syncmill::other"), _call(2, retry_of=1)), True),  # cross-tool
        ((_call(1), _call(2, retry_of=1)), False),  # incomplete capture
    ],
)
def test_invalid_retry_relation_warns_and_keeps_normal_tool_parent(
    calls: tuple[ToolCallRecord, ...], complete: bool, caplog: pytest.LogCaptureFixture
) -> None:
    tracer = _CaptureTracer()
    with caplog.at_level(logging.WARNING):
        _trace_attempt(tracer, _attempt(), _result(calls, complete=complete))

    assert not [span for span in tracer.spans if span.kind == "CHAIN" and ":retry:" in span.node_id]
    assert all(
        span.node_parent == "attempt:canary:0"
        for span in tracer.spans
        if span.kind == "TOOL"
    )
    assert "ignored invalid explicit tool retry relation" in caplog.text
    retry_calls = [call for call in calls if call.retry_of_sequence is not None]
    assert f"sequence={retry_calls[0].sequence}" in caplog.text
    assert f"retry_of={retry_calls[0].retry_of_sequence}" in caplog.text


def test_default_builtin_record_never_fabricates_retry_or_body_fields() -> None:
    tracer = _CaptureTracer()
    _trace_attempt(tracer, _attempt(), _result((_call(1), _call(2))))

    assert [span.kind for span in tracer.spans] == ["AGENT", "TOOL", "TOOL"]
    serialized = repr(tracer.spans)
    assert "body must never enter spans" not in serialized
    assert "retry_of_sequence" not in serialized
