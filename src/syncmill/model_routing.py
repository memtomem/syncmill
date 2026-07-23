"""Difficulty-based provider model routing (PR 1: explicit tiers + observe).

Implements the contract in ``docs/difficulty-model-routing-plan.md``: a
deterministic prompt classifier (``prompt-complexity-v1``), the canonical
immutable :class:`ModelRoutingDecision`, and the pure helpers the Supervisor
and CLI consume. This module is a leaf — it imports nothing from ``agents``,
``supervisor``, or ``cli`` — so the decision can flow into the agents' ambient
ContextVar scope without an import cycle.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from syncmill.config import ModelRoutingConfig, valid_model_selector

CLASSIFIER_V1 = "prompt-complexity-v1"

TIERS = ("low", "medium", "high")

# Reason codes (§4.4 / §4.5).
REASON_HIGH_RISK = "high_risk"
REASON_BROAD_SCOPE = "broad_scope"
REASON_MULTI_TARGET = "multi_target"
REASON_LONG_STRUCTURED = "long_structured_prompt"
REASON_NARROW_SIMPLE = "narrow_simple_intent"
REASON_DEFAULT_MEDIUM = "default_medium"
REASON_EXPLICIT = "explicit_override"
REASON_AFFINITY = "selector_affinity"

_KNOWN_REASONS = {
    REASON_HIGH_RISK,
    REASON_BROAD_SCOPE,
    REASON_MULTI_TARGET,
    REASON_LONG_STRUCTURED,
    REASON_NARROW_SIMPLE,
    REASON_DEFAULT_MEDIUM,
    REASON_EXPLICIT,
    REASON_AFFINITY,
}

# The tier each classifier reason implies (§4.4) — a stored auto decision whose
# resolved_tier contradicts its reason code was not produced by the classifier.
_REASON_TIER = {
    REASON_HIGH_RISK: "high",
    REASON_BROAD_SCOPE: "high",
    REASON_MULTI_TARGET: "high",
    REASON_LONG_STRUCTURED: "high",
    REASON_NARROW_SIMPLE: "low",
    REASON_DEFAULT_MEDIUM: "medium",
}

CORRUPT_ROUTING_ERROR = "corrupt model routing metadata"


class ClassifierError(Exception):
    """The deterministic classifier failed; observe callers degrade + warn."""


class AllUnmappedError(Exception):
    """An applied tier has no selector for any eligible agent (§3.3)."""

    def __init__(self, tier: str) -> None:
        super().__init__(
            f"Model routing selected {tier}, but no eligible agent has a mapping"
        )
        self.tier = tier


# ---------------------------------------------------------------------------
# prompt-complexity-v1 (§4)
# ---------------------------------------------------------------------------

# English markers use letter/digit-boundary phrase matching with hyphen and
# underscore normalized to spaces; Korean markers use plain substring matching
# because agglutinated particles/suffixes attach directly to the marker
# (마이그레이션을, 배포해줘) and a boundary rule would never fire (§4.3).
_HIGH_RISK_EN = (
    "migration", "schema change", "authentication", "authorization",
    "security", "permission", "concurrency", "race condition", "data loss",
    "backward compatibility", "public api", "deploy", "deployment",
    "release", "production",
)
_HIGH_RISK_KO = (
    "마이그레이션", "스키마 변경", "인증", "인가", "보안", "권한", "동시성",
    "경쟁 상태", "데이터 손실", "하위 호환", "공개 api", "배포", "릴리스", "운영",
)
_BROAD_SCOPE_EN = (
    "entire repo", "whole repository", "across the repository", "all modules",
    "end to end", "architecture", "redesign",
)
_BROAD_SCOPE_KO = (
    "전체 저장소", "저장소 전체", "모든 모듈", "전반", "엔드투엔드",
    "아키텍처", "재설계",
)
_SIMPLE_EN = (
    "status", "show", "list", "summarize", "translate", "rename", "typo",
    "format", "lint",
)
_SIMPLE_KO = ("상태", "보여", "목록", "요약", "번역", "이름 변경", "오타", "포맷", "린트")

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_PATH_TOKEN_RE = re.compile(r"(?<!\w)(?:\.{0,2}/)?[\w.@+-]+(?:/[\w.@+-]+)+")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
# NOTE: no dedicated fence handling — the §4.2 fence semantics ("a fenced block
# is not itself one target; path tokens inside count normally") fall out of the
# path/span regexes as written (a multi-line fence can never be a single-line
# code span, and _PATH_TOKEN_RE scans inside it like anywhere else).


def _normalize(prompt: str) -> str:
    text = unicodedata.normalize("NFKC", prompt)
    text = text.casefold()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _phrase_boundary_match(text: str, marker: str) -> bool:
    """English phrase match at Unicode letter/digit boundaries.

    Hyphen/underscore in the text are treated like spaces so ``schema-change``
    matches the marker ``schema change``.
    """
    normalized = re.sub(r"[-_]", " ", text)
    pattern = r"(?<![^\W_])" + re.escape(marker) + r"(?![^\W_])"
    # \w includes letters/digits/underscore; underscores were already folded to
    # spaces above, so the lookarounds reduce to letter/digit boundaries.
    return re.search(pattern, normalized) is not None


def _any_marker(text: str, en: tuple[str, ...], ko: tuple[str, ...]) -> bool:
    return any(_phrase_boundary_match(text, m) for m in en) or any(
        m in text for m in ko
    )


def _count_targets(text: str) -> tuple[int, int]:
    """Return ``(path_targets, targets)`` per §4.2.

    ``path_targets`` excludes URL tokens (anything participating in a
    ``scheme://`` sequence); ``targets`` unions in single-line inline code
    spans. Fenced code blocks are not themselves targets, but path-like tokens
    inside them count normally.
    """
    # URL exclusion: drop any whitespace-delimited token containing "://"
    # BEFORE scanning, so no sub-span of a URL (host/path tail) can count.
    scannable = re.sub(r"\S*://\S*", " ", text)
    paths = {m.group(0) for m in _PATH_TOKEN_RE.finditer(scannable)}
    # Spans are extracted from the ORIGINAL text, so a URL inside an inline
    # code span (`https://…`) still counts as a target. Deliberate: it only
    # affects the low rule's `targets <= 1`, and the error direction is the
    # conservative one (medium instead of low) — URL exclusion exists to keep
    # doc links out of the EXPENSIVE multi_target rule, which reads only
    # `paths`.
    spans = {
        m.group(1).strip()
        for m in _CODE_SPAN_RE.finditer(text)
        if m.group(1).strip()
    }
    return len(paths), len(paths | spans)


def classify_prompt(prompt: str) -> tuple[str, str]:
    """Classify a raw task prompt into ``(tier, reason_code)`` (§4.4).

    Deterministic and self-contained: no filesystem, network, or model calls.
    Any internal failure is wrapped in :class:`ClassifierError` so callers can
    apply the §9 degrade policy without catching arbitrary exceptions.
    """
    try:
        text = _normalize(prompt)
        char_count = len(text)
        lines = text.split("\n")
        nonempty_lines = sum(1 for line in lines if line.strip())
        list_items = sum(1 for line in lines if _LIST_ITEM_RE.match(line))
        path_targets, targets = _count_targets(text)

        if _any_marker(text, _HIGH_RISK_EN, _HIGH_RISK_KO):
            return "high", REASON_HIGH_RISK
        if _any_marker(text, _BROAD_SCOPE_EN, _BROAD_SCOPE_KO):
            return "high", REASON_BROAD_SCOPE
        if path_targets >= 3:
            return "high", REASON_MULTI_TARGET
        if char_count >= 1200 and list_items >= 4:
            return "high", REASON_LONG_STRUCTURED
        if (
            char_count <= 240
            and nonempty_lines <= 3
            and targets <= 1
            and list_items <= 1
            and _any_marker(text, _SIMPLE_EN, _SIMPLE_KO)
        ):
            return "low", REASON_NARROW_SIMPLE
        return "medium", REASON_DEFAULT_MEDIUM
    except Exception as exc:  # pragma: no cover - defensive wrap
        raise ClassifierError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Canonical decision (§5.1)
# ---------------------------------------------------------------------------


def _frozen(mapping: Mapping[str, str | None]) -> Mapping[str, str | None]:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class ModelRoutingDecision:
    """One immutable routing decision per top-level run.

    ``recommended_selectors``/``effective_selectors`` are projected onto the
    eligible lineup only (never the full config tier table) and are stored as
    read-only mapping proxies — the frozen dataclass alone would not freeze a
    contained dict.
    """

    schema_version: int
    requested: str
    resolved_tier: str
    source: str
    classifier_version: str | None
    config_mode: str
    applied: bool
    reason_codes: tuple[str, ...]
    recommended_selectors: Mapping[str, str | None]
    effective_selectors: Mapping[str, str | None]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "recommended_selectors", _frozen(self.recommended_selectors)
        )
        object.__setattr__(
            self, "effective_selectors", _frozen(self.effective_selectors)
        )
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))

    def as_dict(self) -> dict[str, Any]:
        """The ONLY serializer — deterministic sorted mappings and reasons."""
        return {
            "schema_version": self.schema_version,
            "requested": self.requested,
            "resolved_tier": self.resolved_tier,
            "source": self.source,
            "classifier_version": self.classifier_version,
            "config_mode": self.config_mode,
            "applied": self.applied,
            "reason_codes": sorted(self.reason_codes),
            "recommended_selectors": dict(sorted(self.recommended_selectors.items())),
            "effective_selectors": dict(sorted(self.effective_selectors.items())),
        }


def _project(
    cfg: ModelRoutingConfig, tier: str, lineup: Sequence[str]
) -> dict[str, str | None]:
    table = cfg.models.get(tier, {})
    return {agent_id: table.get(agent_id) for agent_id in lineup}


def build_decision(
    cfg: ModelRoutingConfig,
    *,
    prompt: str,
    lineup: Sequence[str],
    requested: str | None,
) -> ModelRoutingDecision | None:
    """Compute the run's single decision (§5.2), or ``None`` when routing is off.

    Raises :class:`ClassifierError` (auto path) for the caller to degrade+warn,
    and :class:`AllUnmappedError` (explicit path) when the applied tier has no
    selector for any eligible agent.
    """
    if requested in (None, ""):
        if cfg.mode == "off":
            return None
        requested = "auto"

    if requested == "auto":
        tier, reason = classify_prompt(prompt)
        recommended = _project(cfg, tier, lineup)
        return ModelRoutingDecision(
            schema_version=1,
            requested="auto",
            resolved_tier=tier,
            source="auto",
            classifier_version=CLASSIFIER_V1,
            config_mode=cfg.mode,
            applied=False,
            reason_codes=(reason,),
            recommended_selectors=recommended,
            effective_selectors={agent_id: None for agent_id in lineup},
        )

    # Explicit tier: user-requested application, honored even under mode=off.
    # An empty lineup is by definition all-unmapped — never a silently
    # applied=true decision over empty maps.
    recommended = _project(cfg, requested, lineup)
    if not lineup or all(sel is None for sel in recommended.values()):
        raise AllUnmappedError(requested)
    return ModelRoutingDecision(
        schema_version=1,
        requested=requested,
        resolved_tier=requested,
        source="explicit",
        classifier_version=None,
        config_mode=cfg.mode,
        applied=True,
        reason_codes=(REASON_EXPLICIT,),
        recommended_selectors=recommended,
        effective_selectors=dict(recommended),
    )


def inherit_decision(
    stored: Any, *, agent_id: str
) -> ModelRoutingDecision | None:
    """Rebuild an inherited decision from a parent manifest block (§6.2).

    ``None``/absent → legacy parent → ``None`` (resume with no model flag).
    Anything present must be a strictly valid v1 block — EVERY canonical field
    well-typed (both selector maps included), the cross-field invariants the
    builder guarantees intact (an unapplied decision has no non-null effective
    selector; an applied one has at least one), and an ``effective_selectors``
    entry present for the winning ``agent_id``. Any violation raises
    ``ValueError(CORRUPT_ROUTING_ERROR)`` (fail closed, §9).
    """
    if stored is None:
        return None
    if not isinstance(stored, dict):
        raise ValueError(CORRUPT_ROUTING_ERROR)
    schema_version = stored.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise ValueError(CORRUPT_ROUTING_ERROR)
    requested = stored.get("requested")
    resolved = stored.get("resolved_tier")
    source = stored.get("source")
    mode = stored.get("config_mode")
    classifier_version = stored.get("classifier_version")
    applied = stored.get("applied")
    reasons = stored.get("reason_codes")
    recommended = stored.get("recommended_selectors")
    effective = stored.get("effective_selectors")

    def _valid_map(mapping: Any) -> bool:
        return isinstance(mapping, dict) and all(
            isinstance(key, str) and (value is None or valid_model_selector(value))
            for key, value in mapping.items()
        )

    if (
        requested not in ("auto", *TIERS)
        or resolved not in TIERS
        or source not in ("auto", "explicit", "inherited")
        or mode not in ("off", "observe", "enforce")
        or not (classifier_version is None or isinstance(classifier_version, str))
        or not isinstance(applied, bool)
        or not isinstance(reasons, list)
        or not all(isinstance(r, str) and r in _KNOWN_REASONS for r in reasons)
        or not _valid_map(recommended)
        or not _valid_map(effective)
        or agent_id not in effective
    ):
        raise ValueError(CORRUPT_ROUTING_ERROR)
    # Cross-field invariants the canonical builder guarantees — a block that
    # contradicts any of them was not produced by syncmill and fails closed
    # rather than being reinterpreted from whichever field one happens to read:
    #   * both maps cover the SAME lineup;
    #   * an applied decision's effective map IS its recommended map (and has
    #     at least one selector), an unapplied one is all-null;
    #   * the source shape is internally consistent (auto = classifier ran and
    #     nothing applied in this release; explicit = the requested tier, no
    #     classifier, exactly the explicit_override reason; inherited carries
    #     the selector_affinity marker).
    classifier_reasons = _KNOWN_REASONS - {REASON_EXPLICIT, REASON_AFFINITY}
    consistent = set(recommended) == set(effective) and applied == any(
        value is not None for value in effective.values()
    )
    if applied:
        consistent = consistent and effective == recommended
    if source == "auto":
        consistent = (
            consistent
            and requested == "auto"
            and classifier_version == CLASSIFIER_V1
            and not applied
            and len(reasons) == 1
            and reasons[0] in classifier_reasons
            and resolved == _REASON_TIER.get(reasons[0])
        )
    elif source == "explicit":
        consistent = (
            consistent
            and requested == resolved
            and classifier_version is None
            and applied
            and reasons == [REASON_EXPLICIT]
        )
    else:
        # Inherited (a continuation of a continuation): inherit_decision itself
        # produced this block, so its shape is fully determined — single-agent
        # maps keyed to the continued winner, recommended == effective, and
        # reasons == the ancestor's ONE origin reason + selector_affinity, with
        # the origin's own field constraints (explicit ancestry: no classifier,
        # requested == resolved; auto ancestry: classifier v1, requested auto,
        # and the reason must resolve to the tier it implies).
        origin_reasons = [r for r in reasons if r != REASON_AFFINITY]
        consistent = (
            consistent
            and REASON_AFFINITY in reasons
            and len(reasons) == len(set(reasons))
            and len(origin_reasons) == 1
            and set(effective) == {agent_id}
            and recommended == effective
        )
        if consistent:
            origin = origin_reasons[0]
            if origin == REASON_EXPLICIT:
                consistent = classifier_version is None and requested == resolved
            elif origin in classifier_reasons:
                consistent = (
                    classifier_version == CLASSIFIER_V1
                    and requested == "auto"
                    and resolved == _REASON_TIER.get(origin)
                )
            else:
                consistent = False
    if not consistent:
        raise ValueError(CORRUPT_ROUTING_ERROR)
    selector = effective[agent_id]
    reason_codes = tuple(reasons)
    if REASON_AFFINITY not in reason_codes:
        reason_codes = reason_codes + (REASON_AFFINITY,)
    return ModelRoutingDecision(
        schema_version=1,
        requested=requested,
        resolved_tier=resolved,
        source="inherited",
        classifier_version=classifier_version,
        config_mode=mode,
        applied=selector is not None,
        reason_codes=reason_codes,
        recommended_selectors={agent_id: selector},
        effective_selectors={agent_id: selector},
    )


# ---------------------------------------------------------------------------
# Output (§3.4)
# ---------------------------------------------------------------------------


def cli_default_count(decision: ModelRoutingDecision) -> int:
    """Agents whose resolved tier has no configured selector.

    Counted over ``recommended_selectors`` — in observe mode every effective
    value is intentionally ``None``, so effective would always read all-default.
    """
    return sum(1 for sel in decision.recommended_selectors.values() if sel is None)


def render_banner(decision: ModelRoutingDecision) -> str:
    """The compact one-line banner (§3.4).

    An auto decision that applied nothing is labeled ``observe`` regardless of
    ``config_mode`` — `--difficulty auto` under ``mode="off"`` IS a one-run
    observe (§3.2), and the honest ``config_mode="off"`` snapshot in the
    serialized block must not leak "off" into the label of a run that routed.
    """
    if decision.source == "auto":
        label = decision.config_mode if decision.applied else "observe"
    else:
        label = decision.source
    applied = "applied" if decision.applied else "not applied"
    parts = [f"model routing: {decision.resolved_tier} / {label} / {applied}"]
    defaults = cli_default_count(decision)
    if defaults:
        parts.append(f"{defaults} cli-default")
    return " / ".join(parts)
