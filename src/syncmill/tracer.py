"""OTLP-JSON trace exporter for tracegraph (S3′ of the ecosystem plan).

A **retrospective recorder**: strategies append completed :class:`SpanRecord`\\ s
right after each fan-out/attempt returns (the shared-loop boundary where memory
I/O already sits), never live span context managers — a live tracer would have
to penetrate the agent layer and break agent purity. The approximation is safe
by tracegraph's design: a span's ``start`` is derived as ``end −
duration_seconds`` and timestamps are never promoted to causality there (they
are only a prerank tiebreak); causality rides ``graph.node.parent_id`` (the one
logical cause) and span ``links`` (additional consumed causes, fan-in).

The emitted document is pinned byte-level by tracegraph's T1 generators
(``examples/syncmill_otlp_traces.py`` + the goldens under
``tests/fixtures/syncmill/``): protobuf-JSON attribute encoding (``intValue``
carries the int64 as a *string*; bool checked before int — bool is an int
subclass), span ``name`` as a stable structural label (agent ids / phases /
indices — never uuids/timestamps, or every tracegraph ``diff`` reads as
divergence), flat ``parentSpanId`` containment under the run root, and error
states as OTLP ``status`` with a bounded message (``timeout`` |
``exit_code=N`` | ``gate: <summary>`` — never raw agent output). tracegraph
consumes exactly three attributes (``openinference.span.kind``,
``graph.node.id``, ``graph.node.parent_id``); everything syncmill-specific is
additive ``syncmill.*`` from a hard allowlist the flush enforces (prompts,
output, patch bodies, and file paths are forbidden by contract).

``OTLPSpanAdapter`` raises on dangling parents/duplicate spanIds/partial
documents, so the exporter buffers everything and lands the file with one
atomic write (temp + ``os.replace``, the breaker/board recipe). Everything
degrades (the memory idiom): ``record`` never raises, ``flush`` returns
``None`` + a warning on failure, and :func:`build_tracer` returns ``None``
when disabled so the off path is byte-identical to before.

A flat top-level module (gateway/monitor/session/board/report precedent).
Imports only ``config``; ``strategies/base`` imports the record/Protocol from
here, keeping the graph acyclic.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from syncmill.config import TraceConfig

logger = logging.getLogger(__name__)

TRACE_FILE = "trace.otlp.json"
SCHEMA_VERSION = 1  # int — the ecosystem-wide contract version

# Bare-suffix mirror of tracegraph's SYNCMILL_ALLOWLIST (T1 generators). The
# flush prefixes ``syncmill.`` and drops anything outside this set, so a span
# builder can never leak a body-shaped attribute into the artifact.
_ALLOWLIST = frozenset(
    {
        "schema_version",
        "run_id",
        "strategy",
        "phase",
        "agent_id",
        "attempt",
        "round",
        "status",
        "exit_code",
        "gate.passed",
        "worktree_slot",
        "winner",
        "files_changed_count",
    }
)

# The three attribute keys tracegraph actually consumes (OTLPSpanAdapter).
_KIND_KEY = "openinference.span.kind"
_NODE_KEY = "graph.node.id"
_PARENT_KEY = "graph.node.parent_id"

_ROOT_NODE = "run"

# Defensive upper bound on an OTLP status message. Messages come only from our
# own bounded builders ("timeout" | "exit_code=N" | "gate: <summary>"), but a
# gate summary interpolates operator-configured command strings, so cap the
# length at flush to keep a pathological config out of the artifact.
_MAX_STATUS_MESSAGE = 200


@dataclass(frozen=True)
class SpanRecord:
    """One completed span, recorded retrospectively by a strategy.

    ``name`` is the stable diff/AHU label (agent ids/phases/indices only —
    never uuids); ``node_id`` must be unique within the trace; ``node_parent``
    is the ONE logical cause; ``links`` are additional consumed causes
    (node_ids, resolved to spanIds at flush). ``attrs`` carries bare-suffix
    ``syncmill.*`` keys (e.g. ``"phase"``, ``"gate.passed"``) — the tracer
    prefixes and allowlist-filters them.
    """

    name: str
    kind: str  # AGENT | CHAIN | TOOL (openinference.span.kind)
    node_id: str
    node_parent: str | None
    links: tuple[str, ...] = ()
    start: float = 0.0  # unix seconds
    end: float = 0.0
    status: str = "completed"  # completed | failed | timeout | cancelled
    error: str | None = None  # bounded summary — never raw output
    attrs: Mapping[str, str | int | bool] = field(default_factory=dict)


def run_span(*, strategy: str, status: str, start: float, end: float) -> SpanRecord:
    """The ``syncmill.run`` root span — recorded by the Supervisor in its
    ``finally``, once the final status is known."""
    return SpanRecord(
        name="syncmill.run",
        kind="CHAIN",
        node_id=_ROOT_NODE,
        node_parent=None,
        start=start,
        end=end,
        status=status,
        attrs={"strategy": strategy, "agent_id": "supervisor", "status": status},
    )


@runtime_checkable
class Tracer(Protocol):
    """The recorder seam strategies receive (``Strategy.execute(tracer=)``).

    ``record`` must never raise out (the strategy is mid-orchestration);
    ``flush`` writes the artifact and returns its path, or ``None`` + a
    warning on failure — callers own the degrade.
    """

    trace_id: str

    def record(self, span: SpanRecord) -> None: ...

    def flush(self) -> Path | None: ...


def _attr(key: str, value: str | int | bool) -> dict:
    """Protobuf-JSON attribute encoding (byte-contract with the T1 goldens)."""
    if isinstance(value, bool):  # bool first — bool is an int subclass
        wrapped: dict = {"boolValue": value}
    elif isinstance(value, int):
        wrapped = {"intValue": str(value)}  # protobuf-JSON encodes int64 as string
    else:
        wrapped = {"stringValue": str(value)}
    return {"key": key, "value": wrapped}


class FileTracer:
    """Buffers span records; ``flush()`` emits one OTLP protobuf-JSON document.

    ``id_factory`` is the deterministic test seam — it supplies the 32-hex
    trace id first, then one 16-hex span id per span (in recording order).
    """

    def __init__(
        self,
        run_id: str,
        out_dir: Path,
        *,
        id_factory: Callable[[int], str] | None = None,
    ) -> None:
        self._run_id = run_id
        self._out_dir = out_dir
        self._ids = id_factory or (lambda nbytes: secrets.token_hex(nbytes))
        self.trace_id: str = self._ids(16)
        self._spans: list[SpanRecord] = []
        self.degraded = False
        self.last_error: str | None = None

    def record(self, span: SpanRecord) -> None:
        """Append one completed span. Never raises out — recording is
        observability, and a broken tracer must not abort orchestration."""
        try:
            self._spans.append(span)
        except Exception as exc:  # pragma: no cover - defensive
            self._degrade(f"could not record span: {exc}")

    def flush(self) -> Path | None:
        """Resolve ids, build the document, and atomically write the artifact.

        All-or-nothing by contract: tracegraph's ``OTLPSpanAdapter`` raises on
        partial documents, dangling ``parentSpanId``, and duplicate spanIds —
        so ids are validated here and the write is temp + ``os.replace``.
        """
        try:
            return self._write(self._build_document())
        except (OSError, ValueError, RuntimeError) as exc:
            self._degrade(f"trace flush failed: {exc}")
            return None

    # ── internals ────────────────────────────────────────────────────────────

    def _build_document(self) -> dict:
        span_ids: dict[str, str] = {}
        for span in self._spans:
            if span.node_id in span_ids:
                raise ValueError(f"duplicate span node_id: {span.node_id!r}")
            span_ids[span.node_id] = self._ids(8)
        root_span_id = span_ids.get(_ROOT_NODE)
        out = []
        for span in self._spans:
            attrs = [_attr(_KIND_KEY, span.kind), _attr(_NODE_KEY, span.node_id)]
            if span.node_parent is not None:
                # An unresolved logical parent makes the whole document
                # rejectable (tracegraph's adapter raises on it) — fail the
                # flush all-or-nothing rather than write a lossy artifact and
                # report it "ok".
                if span.node_parent not in span_ids:
                    raise ValueError(
                        f"span {span.node_id!r} has unresolved "
                        f"graph.node.parent_id {span.node_parent!r}"
                    )
                attrs.append(_attr(_PARENT_KEY, span.node_parent))
            # Injected here, not by the builders, so no span can miss them.
            merged: dict[str, str | int | bool] = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self._run_id,
            }
            for key, value in span.attrs.items():
                if key in ("schema_version", "run_id"):
                    continue
                if key not in _ALLOWLIST:
                    logger.warning(
                        "trace attr %r is outside the syncmill.* allowlist; dropped",
                        key,
                    )
                    continue
                merged[key] = value
            attrs.extend(_attr(f"syncmill.{k}", v) for k, v in merged.items())
            doc: dict = {
                "traceId": self.trace_id,
                "spanId": span_ids[span.node_id],
                "name": span.name,
                "startTimeUnixNano": str(int(span.start * 1e9)),
                "endTimeUnixNano": str(int(span.end * 1e9)),
                "attributes": attrs,
            }
            # Flat containment: every non-root span parents on the run root
            # (causality rides graph.node.parent_id, never parentSpanId). A
            # missing root must not dangle — the adapter raises on that.
            if span.node_id != _ROOT_NODE and root_span_id is not None:
                doc["parentSpanId"] = root_span_id
            if span.status != "completed":
                message = span.error or span.status
                doc["status"] = {
                    "code": "STATUS_CODE_ERROR",
                    "message": message[:_MAX_STATUS_MESSAGE],
                }
            links = []
            for target in span.links:
                span_id = span_ids.get(target)
                if span_id is None:
                    # A dangling link would make the adapter reject the whole
                    # document — fail the flush all-or-nothing rather than emit
                    # a silently-lossy trace (the degrade path, same as an
                    # unresolved logical parent above).
                    raise ValueError(
                        f"span {span.node_id!r} links to unrecorded node "
                        f"{target!r}"
                    )
                links.append({"traceId": self.trace_id, "spanId": span_id})
            if links:
                doc["links"] = links
            out.append(doc)
        return {"resourceSpans": [{"scopeSpans": [{"spans": out}]}]}

    def _write(self, document: dict) -> Path:
        self._out_dir.mkdir(parents=True, exist_ok=True)
        path = self._out_dir / TRACE_FILE
        fd, tmp = tempfile.mkstemp(dir=str(self._out_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(document, f)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path

    def _degrade(self, msg: str) -> None:
        self.degraded = True
        self.last_error = msg
        logger.warning("tracer degraded: %s", msg)


def flush_trace(tracer: Tracer) -> dict:
    """Flush + digest into the envelope ``trace`` block. Never raises.

    ``{"status": "ok", protocol, trace_id, artifact, digest}`` on success —
    the exact conditional-required set of the envelope schema's ``traceBlock``
    — or ``{"status": "error"}`` when the flush or digest failed.
    """
    # Local import: report.py imports config/models/monitor only, so this is
    # acyclic — reusing sha256_digest keeps the digest form from drifting.
    from syncmill.report import sha256_digest

    try:
        path = tracer.flush()
        if path is None:
            return {"status": "error"}
        return {
            "status": "ok",
            "protocol": "otlp-json-file",
            "trace_id": tracer.trace_id,
            "artifact": TRACE_FILE,  # relative to the envelope's directory
            "digest": sha256_digest(path.read_bytes()),
        }
    except Exception as exc:
        logger.warning("trace block build failed: %s", exc)
        return {"status": "error"}


def build_tracer(cfg: TraceConfig, *, run_id: str, fallback_dir: str) -> Tracer | None:
    """The tracer, or ``None`` when tracing is disabled (the default) — the
    ``build_gate``/``build_breaker`` pattern: the disabled path records nothing,
    touches no filesystem, and behavior is byte-identical to before.

    ``fallback_dir`` is the report ``runs_dir`` — ``dir=""`` (the default)
    means "beside the run envelope", and only the caller sees ``ReportConfig``.
    The v1 reservations warn here (once per run), not in a config validator
    (which would fire on every construction, tracing enabled or not).
    """
    if not cfg.enabled:
        return None
    if cfg.exporter == "otlp-http":
        logger.warning(
            "trace exporter 'otlp-http' is reserved (tracegraph has no HTTP "
            "receiver yet); falling back to the file exporter"
        )
    elif cfg.exporter != "file":
        logger.warning("unknown trace exporter %r; using the file exporter", cfg.exporter)
    if cfg.include_output:
        logger.warning(
            "trace include_output is not supported in v1 (spans are body-free "
            "by contract); ignoring"
        )
    try:
        out_dir = Path(cfg.dir or fallback_dir).expanduser() / run_id
    except RuntimeError as exc:  # unresolvable ~ (no-HOME cron/systemd)
        logger.warning("trace dir could not be resolved: %s; tracing disabled", exc)
        return None
    return FileTracer(run_id, out_dir)
