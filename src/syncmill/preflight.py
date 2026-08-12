"""Toolgraph advisory preflight (S5) and strict enforcement (P3.1).

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

Strict mode keeps Toolgraph as an advisory producer but validates every selected
identity, applies SyncMill's category policies, and writes a body-free aggregate
before the Supervisor decides whether to dispatch. Disabled by default
(``build``-idiom ``None``): no subprocess, byte-identical behavior.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from syncmill.config import ToolgraphConfig

logger = logging.getLogger(__name__)

PREFLIGHT_FILE = "preflight.json"
CONTROL_PLAN_FILE = "control-plan.json"
CONTROL_PREFLIGHT_FILE = "control-preflight.json"
SCHEMA_VERSION = 1  # the ecosystem-wide contract version (strict `!=` major check)


@dataclass(frozen=True)
class PreflightOutcome:
    """The result of one preflight run: the envelope block (``status`` always
    present), the yellow CLI warning lines, and the copied artifact path (``None``
    when reporting is disabled — nothing pretends an artifact exists)."""

    block: dict
    warnings: list[str] = field(default_factory=list)
    artifact_path: Path | None = None
    should_block: bool = False
    block_reasons: tuple[str, ...] = ()


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


def _error_block(profile: str, status: str, *, mode: str = "advisory") -> dict:
    """A minimal envelope block for a failed/unresolved preflight (schema only
    requires ``status`` off the ``ok`` path)."""
    return {"status": status, "mode": mode, "profile": profile}


def run_preflight(
    cfg: ToolgraphConfig,
    *,
    run_id: str,
    agent_ids: list[str],
    out_dir: Path | None,
    control_plan: dict | bytes | None = None,
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
        if cfg.control_plan_enabled:
            if control_plan is None:
                raise ValueError(
                    "control-plan preflight enabled without a control plan"
                )
            return _run_control_preflight(
                cfg,
                run_id=run_id,
                agent_ids=agent_ids,
                out_dir=out_dir,
                control_plan=control_plan,
            )
        if cfg.mode == "strict":
            return _run_strict_preflight(
                cfg, run_id=run_id, agent_ids=agent_ids, out_dir=out_dir
            )
        return _run_preflight_inner(
            cfg, run_id=run_id, agent_ids=agent_ids, out_dir=out_dir
        )
    except Exception as exc:  # last-resort net — preflight must never abort a run
        logger.warning("preflight failed unexpectedly; continuing run: %s", exc)
        strict = cfg.mode == "strict"
        block = _error_block(cfg.profile, "error", mode=cfg.mode)
        if strict:
            block.update(
                enforcement_decision="block",
                evaluation_count=0,
                block_count=1,
                warning_count=0,
            )
        return PreflightOutcome(
            block=block,
            warnings=[f"preflight failed unexpectedly: {exc}"],
            should_block=strict,
            block_reasons=("INTERNAL_ERROR",) if strict else (),
        )


_CONTROL_FINDING_CODES = frozenset(
    {
        "START_NODE_COUNT",
        "END_NODE_COUNT",
        "UNKNOWN_NODE_REFERENCE",
        "UNREACHABLE_NODE",
        "NO_TERMINAL_PATH",
        "CONTROL_CYCLE_UNSUPPORTED",
        "MODEL_FANOUT_UNBOUNDED",
        "CALL_BOUND_TOO_LOW",
        "PARALLEL_BOUND_TOO_LOW",
    }
)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def canonical_control_plan(value: object) -> bytes:
    """Return the canonical bytes shared by preflight and durable workflows."""
    return _canonical_json(value)


def _control_request_argv(
    cfg: ToolgraphConfig, plan_path: Path, out_path: Path
) -> list[str]:
    return [
        *cfg.control_plan_command,
        str(plan_path),
        "--profile",
        cfg.profile,
        "--out",
        str(out_path),
    ]


def _invoke_control_toolgraph(
    cfg: ToolgraphConfig, plan_path: Path, out_path: Path
) -> tuple[str | None, str]:
    argv = _control_request_argv(cfg, plan_path, out_path)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=cfg.timeout_seconds
        )
    except FileNotFoundError:
        return (
            "error",
            f"toolgraph binary {cfg.control_plan_command[0]!r} not found in PATH",
        )
    except subprocess.TimeoutExpired:
        return "timeout", f"control preflight timed out after {cfg.timeout_seconds}s"
    except OSError as exc:
        return (
            "error",
            f"control preflight could not launch {cfg.control_plan_command[0]!r}: {exc}",
        )
    if proc.returncode != 0:
        snippet = (proc.stderr or "").strip()[:200]
        return "error", f"control preflight exited {proc.returncode}: {snippet}"
    return None, ""


def _control_malformed_reason(
    doc: object, *, plan: dict, plan_digest: str, profile: str, run_id: str
) -> str | None:
    if not isinstance(doc, dict):
        return "artifact is not an object"
    expected = {
        "schema_version": SCHEMA_VERSION,
        "kind": "toolgraph.control-preflight",
        "run_id": run_id,
        "profile": profile,
        "mode": "advisory",
        "plan_digest": plan_digest,
    }
    for key, value in expected.items():
        if doc.get(key) != value:
            return f"{key} does not match the control plan request"
    if not isinstance(doc.get("created_at"), str) or not doc["created_at"]:
        return "created_at is empty"
    graph_state = doc.get("graph_state")
    if not isinstance(graph_state, dict):
        return "graph_state is not an object"
    if set(graph_state) != {"instance_id", "generation"}:
        return "graph_state has an unexpected shape"
    if (
        not isinstance(graph_state.get("instance_id"), str)
        or not graph_state["instance_id"]
    ):
        return "graph_state.instance_id is empty"
    generation = graph_state.get("generation")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
    ):
        return "graph_state.generation is not a non-negative integer"

    expected_principals = {
        row["id"]: row["candidate_tools"] for row in plan["principals"]
    }
    evaluations = doc.get("evaluations")
    if not isinstance(evaluations, list) or len(evaluations) != len(
        expected_principals
    ):
        return "evaluation coverage does not match plan principals"
    seen: set[str] = set()
    producer_warn = False
    for row in evaluations:
        if not isinstance(row, dict) or not isinstance(row.get("principal"), str):
            return "evaluation row has no principal"
        principal = row["principal"]
        if principal in seen or principal not in expected_principals:
            return "evaluation principals are duplicated or unknown"
        seen.add(principal)
        if not isinstance(row.get("agent_found"), bool):
            return "evaluation agent_found is not boolean"
        eligible = row.get("eligible")
        rejected = row.get("rejected")
        if not isinstance(eligible, list) or not all(
            isinstance(item, str) for item in eligible
        ):
            return "evaluation eligible is not a string list"
        if not isinstance(rejected, list):
            return "evaluation rejected is not a list"
        for rejected_row in rejected:
            if (
                not isinstance(rejected_row, dict)
                or rejected_row.get("reason") not in _KNOWN_REASONS
                or not isinstance(rejected_row.get("candidate"), str)
            ):
                return "evaluation rejected row is malformed"
            if rejected_row["reason"] == "AMBIGUOUS_TOOL" and not isinstance(
                rejected_row.get("candidates"), list
            ):
                return "ambiguous rejected row has no candidates"
            if rejected_row["reason"] in {
                "DENY_VIOLATION",
                "DENY_GOVERNED",
            } and not isinstance(rejected_row.get("paths"), list):
                return "deny rejected row has no evidence paths"
        if row["agent_found"]:
            accounted = [
                *eligible,
                *(rejected_row["candidate"] for rejected_row in rejected),
            ]
            if Counter(accounted) != Counter(expected_principals[principal]):
                return "evaluation candidate coverage does not match the plan"
        elif eligible or rejected:
            return "unresolved principal shape is inconsistent"
        producer_warn = producer_warn or not row["agent_found"] or bool(rejected)
    if seen != set(expected_principals):
        return "evaluation coverage does not match plan principals"

    findings = doc.get("findings")
    if not isinstance(findings, list):
        return "findings is not a list"
    if any(
        not isinstance(row, dict) or row.get("code") not in _CONTROL_FINDING_CODES
        for row in findings
    ):
        return "finding has an unknown code"
    producer_warn = producer_warn or bool(findings)
    expected_decision = "advisory_warn" if producer_warn else "advisory_allow"
    if doc.get("decision") != expected_decision:
        return "decision is inconsistent with evaluations and findings"
    return None


def _control_unavailable_outcome(
    cfg: ToolgraphConfig, *, status: str, message: str
) -> PreflightOutcome:
    strict = cfg.mode == "strict"
    action = _policy_action(cfg, "TOOLGRAPH_UNAVAILABLE")
    should_block = strict and action == "block"
    block = _error_block(cfg.profile, status, mode=cfg.mode)
    if strict:
        block.update(
            enforcement_decision="block" if should_block else "allow",
            evaluation_count=0,
            block_count=1 if should_block else 0,
            warning_count=0 if should_block else 1,
        )
    return PreflightOutcome(
        block=block,
        warnings=[message],
        should_block=should_block,
        block_reasons=("TOOLGRAPH_UNAVAILABLE",) if should_block else (),
    )


def _run_control_preflight(
    cfg: ToolgraphConfig,
    *,
    run_id: str,
    agent_ids: list[str],
    out_dir: Path | None,
    control_plan: dict | bytes,
) -> PreflightOutcome:
    """Consume one Toolgraph batch verdict and optionally enforce it strictly."""
    from syncmill.report import sha256_digest

    if isinstance(control_plan, bytes):
        raw_plan = control_plan
        try:
            decoded_plan = json.loads(raw_plan)
        except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
            raise ValueError("stored control plan is not valid JSON") from exc
        if not isinstance(decoded_plan, dict):
            raise ValueError("stored control plan is not a JSON object")
    else:
        decoded_plan = control_plan
        raw_plan = _canonical_json(decoded_plan)
    plan_digest = sha256_digest(raw_plan)
    with tempfile.TemporaryDirectory() as tmp:
        plan_path = Path(tmp) / CONTROL_PLAN_FILE
        result_path = Path(tmp) / CONTROL_PREFLIGHT_FILE
        plan_path.write_bytes(raw_plan)
        status, message = _invoke_control_toolgraph(cfg, plan_path, result_path)
        if status is not None:
            return _control_unavailable_outcome(cfg, status=status, message=message)
        try:
            raw_result = result_path.read_bytes()
            doc = json.loads(raw_result)
        except (OSError, ValueError) as exc:
            malformed = f"control preflight produced unreadable output: {exc}"
            if cfg.mode == "strict":
                return PreflightOutcome(
                    block={
                        **_error_block(cfg.profile, "error", mode="strict"),
                        "enforcement_decision": "block",
                        "evaluation_count": 0,
                        "block_count": 1,
                        "warning_count": 0,
                    },
                    warnings=[malformed],
                    should_block=True,
                    block_reasons=("CONTRACT_INVALID",),
                )
            return PreflightOutcome(_error_block(cfg.profile, "error"), [malformed])

        malformed = _control_malformed_reason(
            doc,
            plan=decoded_plan,
            plan_digest=plan_digest,
            profile=cfg.profile,
            run_id=run_id,
        )
        if malformed is not None:
            message = f"control preflight artifact is invalid ({malformed})"
            if cfg.mode == "strict":
                return PreflightOutcome(
                    block={
                        **_error_block(cfg.profile, "error", mode="strict"),
                        "enforcement_decision": "block",
                        "evaluation_count": 0,
                        "block_count": 1,
                        "warning_count": 0,
                    },
                    warnings=[message],
                    should_block=True,
                    block_reasons=("CONTRACT_INVALID",),
                )
            return PreflightOutcome(_error_block(cfg.profile, "error"), [message])

        warnings: list[str] = []
        blocking: list[str] = []
        warning_reasons: list[str] = []
        unmapped_agents = [agent for agent in agent_ids if agent not in cfg.agent_map]
        if unmapped_agents:
            reason = "AGENT_MAPPING_MISSING"
            target = (
                blocking
                if cfg.mode == "strict" and _policy_action(cfg, reason) == "block"
                else warning_reasons
            )
            target.append(reason)
            warnings.append(
                "control preflight has unmapped syncmill agents: "
                + ", ".join(unmapped_agents)
            )

        sentinel_principals = {
            principal["id"]
            for principal in decoded_plan["principals"]
            if principal["id"].startswith("syncmill-unmapped-")
        }
        for evaluation in doc["evaluations"]:
            principal = evaluation["principal"]
            if not evaluation["agent_found"] and principal not in sentinel_principals:
                reason = "AGENT_NOT_FOUND"
                target = (
                    blocking
                    if cfg.mode == "strict" and _policy_action(cfg, reason) == "block"
                    else warning_reasons
                )
                target.append(reason)
            for rejected in evaluation["rejected"]:
                reason = rejected["reason"]
                target = (
                    blocking
                    if cfg.mode == "strict" and _policy_action(cfg, reason) == "block"
                    else warning_reasons
                )
                target.append(reason)
        if doc["findings"]:
            finding_codes = sorted({row["code"] for row in doc["findings"]})
            warnings.append("control-plan findings: " + ", ".join(finding_codes))
            if cfg.mode == "strict":
                blocking.append("CONTROL_PLAN_FINDING")
            else:
                warning_reasons.append("CONTROL_PLAN_FINDING")
        if blocking or warning_reasons:
            warnings.append(
                f"control preflight: block={sorted(set(blocking)) or 'none'}, "
                f"warn={sorted(set(warning_reasons)) or 'none'}"
            )

        if out_dir is None and cfg.mode == "strict":
            blocking.append("EVIDENCE_PERSIST_UNAVAILABLE")
            warnings.append(
                "strict control preflight requires a report artifact directory"
            )

        artifact_path: Path | None = None
        if out_dir is not None:
            written: list[Path] = []
            try:
                written.append(_atomic_write(raw_plan, out_dir / CONTROL_PLAN_FILE))
                artifact_path = _atomic_write(
                    raw_result, out_dir / CONTROL_PREFLIGHT_FILE
                )
                written.append(artifact_path)
            except (OSError, ValueError) as exc:
                warnings.append(
                    f"control preflight evidence could not be persisted: {exc}"
                )
                if cfg.mode == "strict":
                    blocking.append("EVIDENCE_PERSIST_FAILED")
                for stale in written:
                    try:
                        stale.unlink(missing_ok=True)
                    except OSError:
                        pass
                artifact_path = None

        should_block = cfg.mode == "strict" and bool(blocking)
        persistence_error = out_dir is not None and artifact_path is None
        status = (
            "error"
            if (
                {
                    "EVIDENCE_PERSIST_UNAVAILABLE",
                    "EVIDENCE_PERSIST_FAILED",
                }
                & set(blocking)
                or persistence_error
            )
            else "ok"
        )
        warning_count = len(set(warning_reasons)) + len(doc["findings"])
        block: dict = {
            "status": status,
            "mode": cfg.mode,
            "profile": cfg.profile,
            "graph_generation": doc["graph_state"]["generation"],
            "graph_state": doc["graph_state"],
            "warning_count": warning_count,
            "evaluation_count": len(doc["evaluations"]),
            "producer_decision": doc["decision"],
        }
        if artifact_path is not None:
            block["artifact"] = CONTROL_PREFLIGHT_FILE
            block["digest"] = sha256_digest(raw_result)
            block["control_plan"] = {
                "artifact": CONTROL_PLAN_FILE,
                "digest": plan_digest,
            }
        if cfg.mode == "strict":
            block.update(
                enforcement_decision="block" if should_block else "allow",
                block_count=len(set(blocking)),
            )
        return PreflightOutcome(
            block=block,
            warnings=warnings,
            artifact_path=artifact_path,
            should_block=should_block,
            block_reasons=tuple(sorted(set(blocking))),
        )


_KNOWN_REASONS = frozenset(
    {
        "TOOL_NOT_FOUND",
        "AMBIGUOUS_TOOL",
        "NOT_GRANTED",
        "DENY_VIOLATION",
        "DENY_GOVERNED",
        "DRIFTED",
        "UNMAPPED",
    }
)
_UNKNOWN_REASONS = frozenset({"TOOL_NOT_FOUND", "AMBIGUOUS_TOOL"})


def _policy_action(cfg: ToolgraphConfig, reason: str) -> str:
    if reason in _UNKNOWN_REASONS or reason in {
        "AGENT_MAPPING_MISSING",
        "AGENT_NOT_FOUND",
    }:
        return cfg.unknown_policy
    if reason == "DRIFTED":
        return cfg.drift_policy
    if reason == "TOOLGRAPH_UNAVAILABLE":
        return cfg.unavailable_policy
    return "block"


def _run_strict_preflight(
    cfg: ToolgraphConfig,
    *,
    run_id: str,
    agent_ids: list[str],
    out_dir: Path | None,
) -> PreflightOutcome:
    """Evaluate every participating identity and enforce the selected profile.

    Toolgraph remains an advisory producer.  This consumer-owned aggregate is
    the P3.1 enforcement boundary; invalid evidence always fails closed.
    """
    from syncmill.report import sha256_digest

    warnings: list[str] = []
    evaluations: list[dict] = []
    blocking: list[str] = []
    generations: list[int] = []
    persisted: list[tuple[bytes, str]] = []

    # Preserve lineup order while evaluating a shared Toolgraph identity once.
    grouped: dict[str, list[str]] = {}
    for agent_id in agent_ids:
        mapped = cfg.agent_map.get(agent_id)
        if mapped is None:
            reason = "AGENT_MAPPING_MISSING"
            action = _policy_action(cfg, reason)
            evaluations.append(
                {
                    "syncmill_agents": [agent_id],
                    "toolgraph_agent": None,
                    "status": "unresolved_identity",
                    "blocking_reasons": [reason] if action == "block" else [],
                    "warning_reasons": [reason] if action == "warn" else [],
                }
            )
            warnings.append(f"agent {agent_id!r} has no toolgraph identity mapping")
            if action == "block":
                blocking.append(reason)
            continue
        grouped.setdefault(mapped, []).append(agent_id)

    with tempfile.TemporaryDirectory() as tmp:
        for index, (mapped, syncmill_agents) in enumerate(grouped.items()):
            request = _build_request(cfg, run_id=run_id, agent=mapped)
            out_path = Path(tmp) / f"preflight-{index:03d}.json"
            operational = _invoke_toolgraph(cfg, request, out_path)
            if operational[0] is not None:
                status, message = operational[0], operational[1]
                reason = "TOOLGRAPH_UNAVAILABLE"
                action = _policy_action(cfg, reason)
                evaluations.append(
                    {
                        "syncmill_agents": syncmill_agents,
                        "toolgraph_agent": mapped,
                        "status": status,
                        "blocking_reasons": [reason] if action == "block" else [],
                        "warning_reasons": [reason] if action == "warn" else [],
                    }
                )
                warnings.append(message)
                if action == "block":
                    blocking.append(reason)
                continue

            try:
                raw = out_path.read_bytes()
                doc = json.loads(raw)
            except (OSError, ValueError) as exc:
                reason = "CONTRACT_INVALID"
                blocking.append(reason)
                warnings.append(f"preflight produced unreadable output: {exc}")
                evaluations.append(
                    {
                        "syncmill_agents": syncmill_agents,
                        "toolgraph_agent": mapped,
                        "status": "error",
                        "blocking_reasons": [reason],
                        "warning_reasons": [],
                    }
                )
                continue

            malformed = _strict_malformed_reason(doc, request)
            if malformed is not None:
                reason = "CONTRACT_INVALID"
                blocking.append(reason)
                warnings.append(f"preflight artifact is invalid ({malformed})")
                evaluations.append(
                    {
                        "syncmill_agents": syncmill_agents,
                        "toolgraph_agent": mapped,
                        "status": "error",
                        "blocking_reasons": [reason],
                        "warning_reasons": [],
                    }
                )
                continue

            generations.append(doc["graph_generation"])
            row_blocks: list[str] = []
            row_warnings: list[str] = []
            if doc["decision"] == "unresolved_identity":
                reason = "AGENT_NOT_FOUND"
                (
                    row_blocks
                    if _policy_action(cfg, reason) == "block"
                    else row_warnings
                ).append(reason)
            else:
                for rejected in doc["rejected"]:
                    reason = rejected["reason"]
                    (
                        row_blocks
                        if _policy_action(cfg, reason) == "block"
                        else row_warnings
                    ).append(reason)
            blocking.extend(row_blocks)
            if row_blocks or row_warnings:
                warnings.append(
                    f"preflight for {mapped!r}: block={row_blocks or 'none'}, "
                    f"warn={row_warnings or 'none'}"
                )
            rel = f"preflight/eval-{index:03d}.json"
            persisted.append((raw, rel))
            evaluations.append(
                {
                    "syncmill_agents": syncmill_agents,
                    "toolgraph_agent": mapped,
                    "status": (
                        "unresolved_identity"
                        if doc["decision"] == "unresolved_identity"
                        else "ok"
                    ),
                    "producer_decision": doc["decision"],
                    "graph_generation": doc["graph_generation"],
                    "artifact": rel,
                    "digest": sha256_digest(raw),
                    "blocking_reasons": row_blocks,
                    "warning_reasons": row_warnings,
                }
            )

    unique_generations = set(generations)
    if len(unique_generations) > 1:
        blocking.append("GRAPH_GENERATION_MISMATCH")
        warnings.append(
            "strict preflight identities were evaluated at different graph generations"
        )
    if out_dir is None:
        blocking.append("EVIDENCE_PERSIST_UNAVAILABLE")
        warnings.append(
            "strict preflight requires a writable report artifact directory"
        )

    # Persist the per-eval artifacts BEFORE the decision/manifest are computed,
    # so a persist failure is itself a blocking reason recorded IN the manifest.
    # The old order froze the manifest bytes first and flipped only the RETURNED
    # block on failure — the on-disk decision could read `allow` for a run that
    # was correctly blocked.
    persisted_paths: list[Path] = []
    if out_dir is not None:
        persist_error: str | None = None
        written: set[str] = set()
        for raw, rel in persisted:
            try:
                persisted_paths.append(_atomic_write(raw, out_dir / rel))
                written.add(rel)
            except (OSError, ValueError) as exc:
                persist_error = str(exc)
                break
        if persist_error is not None:
            warnings.append(
                f"strict preflight evidence could not be persisted: {persist_error}"
            )
            blocking.append("EVIDENCE_PERSIST_FAILED")
            # The manifest must reference only files that actually exist: a
            # mid-loop failure leaves earlier artifacts on disk but not the
            # failed/remaining ones — strip their references (the same
            # no-artifact shape the error/timeout rows use) so a consumer can
            # verify every reference the aggregate makes.
            for row in evaluations:
                if row.get("artifact") is not None and row["artifact"] not in written:
                    row.pop("artifact", None)
                    row.pop("digest", None)

    should_block = bool(blocking)
    statuses = {row["status"] for row in evaluations}
    status = (
        "error"
        if "error" in statuses
        else "timeout"
        if "timeout" in statuses
        else "unresolved_identity"
        if "unresolved_identity" in statuses
        else "ok"
    )
    if {
        "GRAPH_GENERATION_MISMATCH",
        "EVIDENCE_PERSIST_UNAVAILABLE",
        "EVIDENCE_PERSIST_FAILED",
    } & set(blocking):
        status = "error"
    manifest = {
        "schema_version": 1,
        "kind": "syncmill.toolgraph-enforcement",
        "run_id": run_id,
        "mode": "strict",
        "profile": cfg.profile,
        "policies": {
            "unknown": cfg.unknown_policy,
            "drift": cfg.drift_policy,
            "unavailable": cfg.unavailable_policy,
        },
        "decision": "block" if should_block else "allow",
        "evaluations": evaluations,
        "block_reasons": sorted(set(blocking)),
    }
    raw_manifest = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    block: dict = {
        "status": status,
        "mode": "strict",
        "profile": cfg.profile,
        "enforcement_decision": manifest["decision"],
        "evaluation_count": len(evaluations),
        # Count distinct blocking conditions; the manifest exposes the same
        # deduplicated vocabulary in block_reasons.
        "block_count": len(set(blocking)),
        "warning_count": sum(len(row["warning_reasons"]) for row in evaluations),
    }
    if len(unique_generations) == 1:
        block["graph_generation"] = next(iter(unique_generations))

    # The manifest is written LAST, after every blocking reason (including a
    # failed eval persist above) is final — the on-disk decision always matches
    # the returned outcome.
    artifact_path: Path | None = None
    if out_dir is not None:
        try:
            artifact_path = _atomic_write(raw_manifest, out_dir / PREFLIGHT_FILE)
            block["artifact"] = PREFLIGHT_FILE
            block["digest"] = sha256_digest(raw_manifest)
        except (OSError, ValueError) as exc:
            warnings.append(f"strict preflight manifest could not be persisted: {exc}")
            blocking.append("EVIDENCE_PERSIST_FAILED")
            should_block = True
            block.update(status="error", enforcement_decision="block")
            block["block_count"] = len(set(blocking))
            # Leave no partial evidence pile behind: raw producer docs whose
            # decision reads advisory_allow, with no aggregate manifest, would
            # misrepresent a blocked run. Best-effort — the block is already
            # returned either way.
            artifact_path = None
            for stale in (*persisted_paths, out_dir / PREFLIGHT_FILE):
                try:
                    stale.unlink(missing_ok=True)
                except OSError:
                    pass

    return PreflightOutcome(
        block=block,
        warnings=warnings,
        artifact_path=artifact_path,
        should_block=should_block,
        block_reasons=tuple(sorted(set(blocking))),
    )


def _invoke_toolgraph(
    cfg: ToolgraphConfig, request: dict, out_path: Path
) -> tuple[str | None, str]:
    argv = _request_argv(cfg, request, out_path)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=cfg.timeout_seconds
        )
    except FileNotFoundError:
        return "error", f"toolgraph binary {cfg.command[0]!r} not found in PATH"
    except subprocess.TimeoutExpired:
        return "timeout", f"preflight timed out after {cfg.timeout_seconds}s"
    except OSError as exc:
        return "error", f"preflight could not launch {cfg.command[0]!r}: {exc}"
    if proc.returncode != 0:
        snippet = (proc.stderr or "").strip()[:200]
        return "error", f"preflight exited {proc.returncode}: {snippet}"
    return None, ""


def _strict_malformed_reason(doc: object, request: dict) -> str | None:
    if not isinstance(doc, dict):
        return "artifact is not an object"
    expected = {
        "schema_version": SCHEMA_VERSION,
        "kind": "toolgraph.preflight",
        "run_id": request["run_id"],
        "agent": request["agent"],
        "profile": request["policy_profile"],
        "mode": "advisory",
    }
    for key, value in expected.items():
        if doc.get(key) != value:
            return f"{key} does not match the request"
    reason = _malformed_reason(doc)
    if reason is not None:
        return reason
    if not isinstance(doc.get("agent_found"), bool):
        return "agent_found is not boolean"
    eligible = doc.get("eligible")
    rejected = doc.get("rejected")
    if not isinstance(eligible, list) or not all(
        isinstance(item, str) for item in eligible
    ):
        return "eligible is not a string list"
    if not isinstance(rejected, list):
        return "rejected is not a list"
    for row in rejected:
        if not isinstance(row, dict) or row.get("reason") not in _KNOWN_REASONS:
            return "rejected row has an unknown reason"
        if not isinstance(row.get("candidate"), str):
            return "rejected row has no candidate"
        if row["reason"] == "AMBIGUOUS_TOOL" and not isinstance(
            row.get("candidates"), list
        ):
            return "ambiguous row has no candidates"
        if row["reason"] in {"DENY_VIOLATION", "DENY_GOVERNED"} and not isinstance(
            row.get("paths"), list
        ):
            return "deny row has no evidence paths"
    if not doc["agent_found"]:
        if doc["decision"] != "unresolved_identity" or eligible or rejected:
            return "unresolved identity shape is inconsistent"
        return None
    expected_decision = "advisory_warn" if rejected else "advisory_allow"
    if doc["decision"] != expected_decision:
        return "decision is inconsistent with rejected rows"
    accounted = [*eligible, *(row["candidate"] for row in rejected)]
    if Counter(accounted) != Counter(request["candidate_tools"]):
        return "candidate coverage does not match the request"
    return None


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


_KNOWN_DECISIONS = frozenset({"advisory_allow", "advisory_warn", "unresolved_identity"})


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


def _atomic_write(raw: bytes, dest: Path) -> Path:
    """Atomically write a strict aggregate or one validated producer artifact."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest
