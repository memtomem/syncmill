"""toolgraph advisory preflight (S5 of the ecosystem plan).

Before dispatching a strategy, the Supervisor optionally runs
``toolgraph preflight`` once — for the lead matched agent's mapped toolgraph
identity — to check the run's ambient MCP surface (memtomem, syncmill's own
read-only ``serve`` gateway) against toolgraph's policy graph. It is
**advisory**: every failure class (binary absent, timeout, non-zero exit, bad
JSON, unknown major version) degrades to a yellow warning and the run
CONTINUES — preflight never blocks or aborts orchestration (the memory idiom).
The outcome lands in the run envelope's ``preflight`` block; when reporting is
enabled the artifact is copied to ``{run_dir}/preflight.json`` and digested.

syncmill agents are CLI subprocesses that do not themselves call MCP tools, so
this checks the *ecosystem's* knowledge surface for the mapped identity — it
does not claim per-attempt tool mediation (the design's honest tier: identity
validation + generation stamp always; ambient-surface policy check when
``candidate_tools`` is configured; per-attempt mediation deliberately reserved).

Disabled by default (``build``-idiom ``None``): no subprocess, byte-identical
behavior. A flat top-level module (gateway/monitor/session/board/report/tracer
precedent); imports only ``config`` and ``report.sha256_digest``.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from syncmill.config import ToolgraphConfig

logger = logging.getLogger(__name__)

PREFLIGHT_FILE = "preflight.json"
SCHEMA_VERSION = 1  # the ecosystem-wide contract version (strict `!=` major check)


@dataclass(frozen=True)
class PreflightOutcome:
    """The result of one preflight run: the envelope block (``status`` always
    present), the yellow CLI warning lines, and the copied artifact path (``None``
    when reporting is disabled — nothing pretends an artifact exists)."""

    block: dict
    warnings: list[str] = field(default_factory=list)
    artifact_path: Path | None = None


def _build_request(cfg: ToolgraphConfig, *, run_id: str, agent: str) -> dict:
    """The request contract (``contracts/preflight-request.schema.json``) — the
    serialized form of the argv sent to ``toolgraph preflight``."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "agent": agent,
        "candidate_tools": list(cfg.candidate_tools),
        "policy_profile": cfg.profile,
    }


def _request_argv(cfg: ToolgraphConfig, request: dict, out_path: Path) -> list[str]:
    """argv only — no stdin. ``command`` is the invocation seam tests override."""
    return [
        *cfg.command,
        request["agent"],
        *request["candidate_tools"],
        "--profile",
        request["policy_profile"],
        "--run-id",
        request["run_id"],
        "--out",
        str(out_path),
    ]


def _error_block(profile: str, status: str) -> dict:
    """A minimal envelope block for a failed/unresolved preflight (schema only
    requires ``status`` off the ``ok`` path)."""
    return {"status": status, "mode": "advisory", "profile": profile}


def run_preflight(
    cfg: ToolgraphConfig,
    *,
    run_id: str,
    agent_ids: list[str],
    out_dir: Path | None,
) -> PreflightOutcome | None:
    """Run advisory preflight once for the lead mapped agent. ``None`` when
    disabled; otherwise NEVER raises — every failure degrades to a warning + a
    ``status`` block and the caller continues the run.

    ``out_dir`` is the run directory (report enabled) or ``None`` (report
    disabled) — in the latter case the subprocess still runs (its identity
    warning is the whole tier-1 value) but the artifact is parsed from a
    tempdir and discarded, so ``artifact_path`` is ``None``.
    """
    if not cfg.enabled:
        return None
    try:
        return _run_preflight_inner(cfg, run_id=run_id, agent_ids=agent_ids, out_dir=out_dir)
    except Exception as exc:  # last-resort net — preflight must never abort a run
        logger.warning("preflight failed unexpectedly; continuing run: %s", exc)
        return PreflightOutcome(
            block=_error_block(cfg.profile, "error"),
            warnings=[f"preflight failed unexpectedly: {exc}"],
        )


def _run_preflight_inner(
    cfg: ToolgraphConfig,
    *,
    run_id: str,
    agent_ids: list[str],
    out_dir: Path | None,
) -> PreflightOutcome:
    warnings: list[str] = []

    # Map ids locally (pure dict lookups). Warn on every unmapped id; the lead
    # (first matched) mapped agent is the identity we probe.
    lead: str | None = None
    for agent_id in agent_ids:
        mapped = cfg.agent_map.get(agent_id)
        if mapped is None:
            warnings.append(
                f"agent {agent_id!r} has no [integration.toolgraph.agent_map] "
                "entry — skipped for preflight"
            )
        elif lead is None:
            lead = mapped

    if lead is None:
        warnings.append(
            "no agent maps to a toolgraph identity "
            "(configure [integration.toolgraph.agent_map]); preflight skipped"
        )
        return PreflightOutcome(
            block=_error_block(cfg.profile, "unresolved_identity"),
            warnings=warnings,
        )

    request = _build_request(cfg, run_id=run_id, agent=lead)

    # The subprocess ALWAYS writes to a tempdir; only validated bytes are then
    # atomically copied into the run dir (_build_outcome). So a rejected artifact
    # (bad JSON, unknown major, non-zero exit) never lingers in the run dir, and
    # the copied bytes are exactly what the envelope digest covers.
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / PREFLIGHT_FILE
        argv = _request_argv(cfg, request, out_path)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=cfg.timeout_seconds,
            )
        except FileNotFoundError:
            warnings.append(f"toolgraph binary {cfg.command[0]!r} not found in PATH")
            return PreflightOutcome(_error_block(cfg.profile, "error"), warnings)
        except subprocess.TimeoutExpired:
            warnings.append(f"preflight timed out after {cfg.timeout_seconds}s")
            return PreflightOutcome(_error_block(cfg.profile, "timeout"), warnings)
        except OSError as exc:
            warnings.append(f"preflight could not launch {cfg.command[0]!r}: {exc}")
            return PreflightOutcome(_error_block(cfg.profile, "error"), warnings)

        if proc.returncode != 0:
            snippet = (proc.stderr or "").strip()[:200]
            warnings.append(f"preflight exited {proc.returncode}: {snippet}")
            return PreflightOutcome(_error_block(cfg.profile, "error"), warnings)

        try:
            raw = out_path.read_bytes()
            doc = json.loads(raw)
        except (OSError, ValueError) as exc:
            warnings.append(f"preflight produced unreadable output: {exc}")
            return PreflightOutcome(_error_block(cfg.profile, "error"), warnings)

        if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
            warnings.append(
                f"preflight artifact schema_version {doc.get('schema_version') if isinstance(doc, dict) else '?'} "
                f"!= {SCHEMA_VERSION}; rejected"
            )
            return PreflightOutcome(_error_block(cfg.profile, "error"), warnings)

        # A right-major but malformed v1 artifact (missing/typed-wrong
        # graph_generation, non-list rejected, unknown decision) must NOT reach
        # the envelope — an "ok" block with graph_generation=None would violate
        # run-envelope.schema.json. Reject it as an advisory error before copying.
        reason = _malformed_reason(doc)
        if reason is not None:
            warnings.append(f"preflight artifact is malformed ({reason}); rejected")
            return PreflightOutcome(_error_block(cfg.profile, "error"), warnings)

        return _build_outcome(cfg, doc, raw, out_dir, warnings)


_KNOWN_DECISIONS = frozenset(
    {"advisory_allow", "advisory_warn", "unresolved_identity"}
)


def _malformed_reason(doc: dict) -> str | None:
    """Validate the v1 fields the envelope block is built from; a reason string
    if the artifact can't produce a schema-valid block, else ``None``.

    Not full jsonschema (the toolgraph schema isn't a shipped runtime asset) —
    just the fields consumed: ``graph_generation`` (int ≥0, required by the
    envelope's ``ok`` block), ``rejected`` (a list — ``warning_count`` counts
    it), and ``decision`` (a known value — an unknown one must not silently
    map to ``ok``)."""
    gen = doc.get("graph_generation")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 0:
        return "graph_generation is not a non-negative integer"
    if not isinstance(doc.get("rejected", []), list):
        return "rejected is not a list"
    if doc.get("decision") not in _KNOWN_DECISIONS:
        return f"unknown decision {doc.get('decision')!r}"
    return None


def _build_outcome(
    cfg: ToolgraphConfig,
    doc: dict,
    raw: bytes,
    out_dir: Path | None,
    warnings: list[str],
) -> PreflightOutcome:
    """Map a valid preflight artifact to the envelope block + copy the artifact."""
    from syncmill.report import sha256_digest

    decision = doc.get("decision")
    rejected = doc.get("rejected") or []
    warning_count = len(rejected) if isinstance(rejected, list) else 0

    if decision == "unresolved_identity":
        status = "unresolved_identity"
        warnings.append(
            f"preflight could not resolve agent identity {doc.get('agent')!r} "
            "in the toolgraph"
        )
    else:  # advisory_allow / advisory_warn (validated by _malformed_reason) → ok
        status = "ok"
        if warning_count:
            warnings.append(
                f"preflight: {warning_count} candidate tool(s) rejected by policy"
            )

    block: dict = {
        "status": status,
        "mode": "advisory",
        # The REQUESTED profile — guaranteed a valid enum value (the config
        # validator), and it's exactly what this artifact evaluated.
        "profile": cfg.profile,
        "graph_generation": doc["graph_generation"],  # validated int by now
        "warning_count": warning_count,
    }

    # Copy the exact bytes into the run dir (report enabled) so the envelope's
    # digest verifies the artifact the consumer reads.
    if out_dir is not None:
        try:
            dest = _atomic_copy(raw, out_dir)
            block["artifact"] = PREFLIGHT_FILE
            block["digest"] = sha256_digest(raw)
            return PreflightOutcome(block, warnings, artifact_path=dest)
        except (OSError, ValueError) as exc:
            warnings.append(f"preflight artifact could not be persisted: {exc}")
            return PreflightOutcome(_error_block(cfg.profile, "error"), warnings)
    return PreflightOutcome(block, warnings)


def _atomic_copy(raw: bytes, out_dir: Path) -> Path:
    """Write ``raw`` to ``{out_dir}/preflight.json`` atomically (the report
    ``write_envelope`` mkstemp+``os.replace`` recipe)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / PREFLIGHT_FILE
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest
