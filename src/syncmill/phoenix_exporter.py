"""Optional Phoenix OTLP/HTTP streaming sink.

This module is imported lazily only for ``integration.trace.exporter=otlp-http``.
It deliberately owns a private OpenTelemetry provider: SyncMill exports only its
body-free :class:`~syncmill.tracer.SpanRecord` values and never auto-instruments
ambient libraries.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import (
    SpanExportResult,
    SpanExporter,
    SpanProcessor,
)
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.trace import Link, Status, StatusCode, set_span_in_context
from phoenix.otel import HTTPSpanExporter
from syncmill import __version__
from syncmill.tracer import _MAX_STATUS_MESSAGE

logger = logging.getLogger(__name__)

_MAX_QUEUE_SIZE = 2048
_MAX_EXPORT_BATCH_SIZE = 512
_STOP = object()


@dataclass
class _Barrier:
    event: threading.Event
    reached: bool = False


class _FixedTraceIdGenerator(IdGenerator):
    """Use the FileTracer trace id while retaining random span ids."""

    def __init__(self, trace_id: str) -> None:
        self._trace_id = int(trace_id, 16)
        self._random = RandomIdGenerator()

    def generate_span_id(self) -> int:
        return self._random.generate_span_id()

    def generate_trace_id(self) -> int:
        return self._trace_id


class _BoundedBatchSpanProcessor(SpanProcessor):
    """Small background batcher with a real caller-side flush deadline.

    OpenTelemetry's exporter still owns HTTP retry behavior. This processor owns
    only queueing and the maximum time SyncMill waits in its finalizer.
    """

    def __init__(
        self,
        exporter: SpanExporter,
        *,
        batch_delay_seconds: float,
    ) -> None:
        self._exporter = exporter
        self._delay = batch_delay_seconds
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._accepting = True
        self._stop_requested = threading.Event()
        self._submitted = 0
        self._exported = 0
        self._dropped = 0
        self._failed = False
        self._worker = threading.Thread(
            target=self._run,
            name="syncmill-phoenix-exporter",
            daemon=True,
        )
        self._worker.start()

    def on_start(self, span: Any, parent_context: Any | None = None) -> None:
        return None

    def on_end(self, span: ReadableSpan) -> None:
        with self._lock:
            self._submitted += 1
            accepting = self._accepting
        if not accepting:
            self._mark_dropped(1)
            return
        try:
            self._queue.put_nowait(span)
        except queue.Full:
            self._mark_dropped(1)
            logger.warning("Phoenix trace queue is full; dropping a completed span")

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        timeout = max(timeout_millis / 1000.0, 0.0)
        deadline = time.monotonic() + timeout
        barrier = _Barrier(threading.Event())
        try:
            self._queue.put(barrier, timeout=max(deadline - time.monotonic(), 0.0))
        except queue.Full:
            self._mark_failed()
            return False
        if not barrier.event.wait(max(deadline - time.monotonic(), 0.0)):
            self._mark_failed()
            return False
        return barrier.reached

    def shutdown(self, timeout_seconds: float = 5.0) -> bool:  # type: ignore[override]
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        with self._lock:
            self._accepting = False
        flushed = self.force_flush(
            timeout_millis=max(int((deadline - time.monotonic()) * 1000), 0)
        )
        self._stop_requested.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            pass
        self._worker.join(max(deadline - time.monotonic(), 0.0))
        clean = flushed and not self._worker.is_alive()
        if not clean:
            self._mark_failed()
        return clean

    def receipt(self, *, extra_dropped: int = 0, extra_failed: bool = False) -> dict:
        with self._lock:
            submitted = self._submitted + extra_dropped
            exported = self._exported
            dropped = self._dropped + extra_dropped
            failed = self._failed or extra_failed
        if failed:
            # Anything not acknowledged is honestly reported as dropped/unconfirmed.
            dropped = max(dropped, submitted - exported)
        return {
            "backend": "phoenix",
            "protocol": "otlp-http/protobuf",
            "status": "error" if failed or dropped else "ok",
            "exported_span_count": exported,
            "dropped_span_count": dropped,
        }

    def _run(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if item is _STOP:
                    return
                if isinstance(item, _Barrier):
                    item.reached = True
                    item.event.set()
                    continue

                batch: list[ReadableSpan] = [item]
                control: object | _Barrier | None = None
                deadline = time.monotonic() + self._delay
                while len(batch) < _MAX_EXPORT_BATCH_SIZE:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        next_item = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        break
                    if next_item is _STOP or isinstance(next_item, _Barrier):
                        control = next_item
                        break
                    batch.append(next_item)

                self._export(batch)
                if isinstance(control, _Barrier):
                    control.reached = True
                    control.event.set()
                elif control is _STOP:
                    return
                if self._stop_requested.is_set():
                    self._discard_remaining()
                    return
        finally:
            try:
                self._exporter.shutdown()
            except Exception:
                logger.warning("Phoenix exporter shutdown failed", exc_info=True)

    def _export(self, batch: Sequence[ReadableSpan]) -> None:
        try:
            outcome = self._exporter.export(batch)
        except Exception:
            logger.warning("Phoenix span export failed", exc_info=True)
            outcome = SpanExportResult.FAILURE
        with self._lock:
            if outcome is SpanExportResult.SUCCESS:
                self._exported += len(batch)
            else:
                self._dropped += len(batch)
                self._failed = True

    def _discard_remaining(self) -> None:
        discarded = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, _Barrier):
                item.event.set()
            elif item is not _STOP:
                discarded += 1
        if discarded:
            self._mark_dropped(discarded)

    def _mark_dropped(self, count: int) -> None:
        with self._lock:
            self._dropped += count
            self._failed = True

    def _mark_failed(self) -> None:
        with self._lock:
            self._failed = True


class PhoenixSpanSink:
    """Convert completed SyncMill records into private OTel spans."""

    def __init__(
        self,
        *,
        run_id: str,
        trace_id: str,
        run_start: float,
        endpoint: str,
        project_name: str,
        batch_delay_seconds: float,
        timeout_seconds: int,
        attribute_factory: Callable[[Any], Mapping[str, str | int | bool]],
    ) -> None:
        resource = Resource.create(
            {
                "service.name": "syncmill",
                "openinference.project.name": project_name,
            }
        )
        self._provider = TracerProvider(
            resource=resource,
            shutdown_on_exit=False,
            id_generator=_FixedTraceIdGenerator(trace_id),
        )
        exporter = HTTPSpanExporter(endpoint=endpoint, timeout=timeout_seconds)
        self._processor = _BoundedBatchSpanProcessor(
            exporter,
            batch_delay_seconds=batch_delay_seconds,
        )
        self._provider.add_span_processor(self._processor)
        self._tracer = self._provider.get_tracer("syncmill", __version__)
        self._root = self._tracer.start_span(
            "syncmill.run",
            start_time=int(run_start * 1e9),
        )
        self._contexts: dict[str, Any] = {"run": self._root.get_span_context()}
        self._recorded: set[str] = set()
        self._attribute_factory = attribute_factory
        self._timeout_seconds = timeout_seconds
        self._validation_dropped = 0
        self._validation_failed = False
        self._closed = False
        self._receipt: dict | None = None

    def record(self, record: Any) -> None:
        try:
            self._record(record)
        except Exception:
            self._validation_dropped += 1
            self._validation_failed = True
            logger.warning("Phoenix span conversion failed", exc_info=True)

    def flush(self) -> dict:
        if self._receipt is not None:
            return dict(self._receipt)
        if not self._closed:
            if "run" not in self._recorded:
                self._validation_failed = True
                # The private root exists and is exported, but its final
                # producer-authored run record never arrived. Mark the receipt
                # erroneous without also claiming that an emitted span dropped.
                self._root.set_status(Status(StatusCode.ERROR, "missing run span"))
                self._root.end()
            self._closed = True
        clean = self._processor.shutdown(self._timeout_seconds)
        self._receipt = self._processor.receipt(
            extra_dropped=self._validation_dropped,
            extra_failed=self._validation_failed or not clean,
        )
        return dict(self._receipt)

    def _record(self, record: Any) -> None:
        if self._closed:
            raise RuntimeError("Phoenix stream is already closed")
        if record.node_id in self._recorded:
            raise ValueError(f"duplicate span node_id: {record.node_id!r}")
        if record.node_parent is not None and record.node_parent not in self._contexts:
            raise ValueError(
                f"span {record.node_id!r} has unresolved parent {record.node_parent!r}"
            )
        missing_links = [target for target in record.links if target not in self._contexts]
        if missing_links:
            raise ValueError(
                f"span {record.node_id!r} links to unrecorded node {missing_links[0]!r}"
            )

        attributes = dict(self._attribute_factory(record))
        if record.node_id == "run":
            span = self._root
        else:
            links = [Link(self._contexts[target]) for target in record.links]
            span = self._tracer.start_span(
                record.name,
                context=set_span_in_context(self._root),
                links=links,
                start_time=int(record.start * 1e9),
            )
        for key, value in attributes.items():
            span.set_attribute(key, value)
        if record.status in {"failed", "timeout", "cancelled"}:
            message = (record.error or record.status)[:_MAX_STATUS_MESSAGE]
            span.set_status(Status(StatusCode.ERROR, message))
        self._contexts[record.node_id] = span.get_span_context()
        self._recorded.add(record.node_id)
        span.end(end_time=int(record.end * 1e9))
