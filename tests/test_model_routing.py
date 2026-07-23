"""Tests for difficulty-based model routing (model_routing.py, PR 1).

Covers the deterministic classifier's §10.1 matrix (boundaries, EN/KO marker
semantics, normalization, determinism), the canonical decision serialization,
inheritance validation, and the compact banner.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from syncmill.config import ModelRoutingConfig
from syncmill.model_routing import (
    AllUnmappedError,
    ModelRoutingDecision,
    build_decision,
    classify_prompt,
    cli_default_count,
    inherit_decision,
    render_banner,
)

# ── classifier: marker families ──────────────────────────────────────────


def test_english_high_risk_marker():
    assert classify_prompt("run the database migration now") == ("high", "high_risk")


def test_english_marker_is_boundary_matched_not_substring():
    # "migrations" does not boundary-match the marker "migration".
    tier, reason = classify_prompt("read the docs about premigrationszz stuff")
    assert (tier, reason) == ("medium", "default_medium")


def test_english_hyphen_normalized_like_space():
    assert classify_prompt("apply the schema-change carefully") == (
        "high",
        "high_risk",
    )


def test_korean_marker_substring_matches_agglutinated_forms():
    # Particle/suffix-attached forms must match (§4.3 Korean substring rule).
    assert classify_prompt("마이그레이션을 진행해줘") == ("high", "high_risk")
    assert classify_prompt("이거 배포해줘") == ("high", "high_risk")
    assert classify_prompt("보안이 걱정된다") == ("high", "high_risk")


def test_korean_false_positive_is_accepted_conservatively():
    # 인증서 contains 인증 — conservative over-routing to high is the contract.
    assert classify_prompt("인증서 파일 경로 알려줘") == ("high", "high_risk")


def test_broad_scope_marker():
    assert classify_prompt("redesign the module layout") == ("high", "broad_scope")
    assert classify_prompt("저장소 전체를 정리") == ("high", "broad_scope")


def test_simple_intent_low():
    assert classify_prompt("show the status") == ("low", "narrow_simple_intent")
    assert classify_prompt("오타 고쳐줘") == ("low", "narrow_simple_intent")


def test_wide_verbs_are_not_simple_markers():
    # fix/check/review/implement/explain are deliberately not simple markers.
    assert classify_prompt("fix the bug") == ("medium", "default_medium")


def test_high_risk_beats_simple_marker():
    # Rule order: high_risk (rule 1) wins over the low rule (rule 5).
    assert classify_prompt("show the deploy") == ("high", "high_risk")


def test_repeated_marker_does_not_escalate_further():
    assert classify_prompt("deploy deploy deploy") == ("high", "high_risk")


def test_unknown_language_defaults_medium():
    assert classify_prompt("Corrige el problema con cuidado") == (
        "medium",
        "default_medium",
    )


# ── classifier: normalization ────────────────────────────────────────────


def test_casefold_matches_uppercase_marker():
    assert classify_prompt("URGENT: PRODUCTION issue") == ("high", "high_risk")


def test_nfkc_normalization_applies():
    # Fullwidth latin letters NFKC-fold to ASCII before matching.
    assert classify_prompt("ｄｅｐｌｏｙ now") == ("high", "high_risk")


def test_crlf_and_cr_fold_to_lf():
    a = classify_prompt("do this\r\n- a\r\n- b")
    b = classify_prompt("do this\n- a\n- b")
    c = classify_prompt("do this\r- a\r- b")
    assert a == b == c


# ── classifier: structural boundaries (§10.1) ────────────────────────────


def _low_prompt(char_count: int) -> str:
    # A one-line prompt with a simple marker, padded to exactly char_count.
    base = "summarize "
    return base + "x" * (char_count - len(base))


def test_char_count_240_low_241_medium():
    assert classify_prompt(_low_prompt(240)) == ("low", "narrow_simple_intent")
    assert classify_prompt(_low_prompt(241)) == ("medium", "default_medium")


def test_nonempty_lines_3_low_4_medium():
    three = "summarize\nalpha\nbeta"
    four = "summarize\nalpha\nbeta\ngamma"
    assert classify_prompt(three) == ("low", "narrow_simple_intent")
    assert classify_prompt(four) == ("medium", "default_medium")


def test_list_items_1_low_2_medium():
    one = "summarize\n- alpha"
    two = "summarize\n- alpha\n- beta"
    assert classify_prompt(one) == ("low", "narrow_simple_intent")
    assert classify_prompt(two) == ("medium", "default_medium")


def test_targets_1_low_2_medium():
    one = "summarize src/syncmill/cli.py"
    two = "summarize src/syncmill/cli.py and src/syncmill/agents.py"
    assert classify_prompt(one) == ("low", "narrow_simple_intent")
    assert classify_prompt(two) == ("medium", "default_medium")


def test_path_targets_2_medium_3_high():
    two = "fix src/a/b.py and src/c/d.py"
    three = "fix src/a/b.py and src/c/d.py and tests/e/f.py"
    assert classify_prompt(two) == ("medium", "default_medium")
    assert classify_prompt(three) == ("high", "multi_target")


def test_repeated_target_counts_once():
    text = "fix src/a/b.py src/a/b.py src/a/b.py"
    assert classify_prompt(text) == ("medium", "default_medium")


def test_long_structured_prompt_boundaries():
    def structured(total: int) -> str:
        base = "do all of this\n- a\n- b\n- c\n- d\n"
        return base + "y" * (total - len(base))

    at_1200 = structured(1200)
    at_1199 = structured(1199)
    assert (len(at_1200), len(at_1199)) == (1200, 1199)
    assert classify_prompt(at_1200) == ("high", "long_structured_prompt")
    assert classify_prompt(at_1199) == ("medium", "default_medium")
    # 1200+ chars but only 3 list items → medium.
    three_items = "do all of this\n- a\n- b\n- c\n" + "y" * 1200
    assert classify_prompt(three_items) == ("medium", "default_medium")


# ── classifier: targets details ──────────────────────────────────────────


def test_urls_do_not_count_as_path_targets():
    text = (
        "see https://docs.anthropic.com/en/docs and https://x.io/a/b "
        "plus https://y.io/c/d"
    )
    assert classify_prompt(text) == ("medium", "default_medium")


def test_inline_code_spans_never_trigger_multi_target():
    # Three spans, zero path targets: spans count toward `targets` (blocking
    # the low rule) but never toward the multi_target high rule.
    assert classify_prompt("rename `foo` to `bar` in `baz`") == (
        "medium",
        "default_medium",
    )


def test_inline_code_span_counts_toward_low_targets():
    assert classify_prompt("summarize `cli.py`") == ("low", "narrow_simple_intent")


def test_fenced_block_is_not_one_target_but_paths_inside_count():
    text = "fix this\n```\nsrc/a/b.py\nsrc/c/d.py\nsrc/e/f.py\n```"
    assert classify_prompt(text) == ("high", "multi_target")


def test_negation_still_matches_conservatively():
    assert classify_prompt("do not touch the deploy config") == (
        "high",
        "high_risk",
    )


def test_empty_prompt_is_medium():
    assert classify_prompt("") == ("medium", "default_medium")


# ── classifier: determinism ──────────────────────────────────────────────

_DETERMINISM_FIXTURES = [
    "list files",
    "마이그레이션을 진행해줘",
    "fix src/a/b.py src/c/d.py tests/e/f.py",
    "rename `foo` to `bar` in `baz.py`",
    "Corrige el problema",
    "redesign everything end to end",
]


def test_repeat_determinism():
    for prompt in _DETERMINISM_FIXTURES:
        first = classify_prompt(prompt)
        assert all(classify_prompt(prompt) == first for _ in range(5))


def test_multi_process_determinism():
    """The same fixtures classify identically in a fresh interpreter (§10.1)."""
    code = (
        "import json, sys\n"
        "from syncmill.model_routing import classify_prompt\n"
        "prompts = json.loads(sys.stdin.read())\n"
        "print(json.dumps([list(classify_prompt(p)) for p in prompts]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(_DETERMINISM_FIXTURES),
        capture_output=True,
        text=True,
        check=True,
    )
    child = [tuple(item) for item in json.loads(proc.stdout)]
    assert child == [classify_prompt(p) for p in _DETERMINISM_FIXTURES]


# ── decisions: build_decision ────────────────────────────────────────────


def _cfg(mode="observe", models=None) -> ModelRoutingConfig:
    return ModelRoutingConfig(mode=mode, models=models or {})


def test_off_mode_no_request_returns_none():
    assert (
        build_decision(_cfg(mode="off"), prompt="p", lineup=["claude"], requested=None)
        is None
    )


def test_observe_decision_shape():
    cfg = _cfg(models={"low": {"claude": "cheap-model"}})
    d = build_decision(cfg, prompt="show status", lineup=["claude", "codex"], requested=None)
    assert d.resolved_tier == "low"
    assert d.source == "auto"
    assert d.applied is False
    assert d.classifier_version == "prompt-complexity-v1"
    assert dict(d.recommended_selectors) == {"claude": "cheap-model", "codex": None}
    assert dict(d.effective_selectors) == {"claude": None, "codex": None}


def test_explicit_tier_applies_even_under_off_mode():
    cfg = _cfg(mode="off", models={"high": {"codex": "big-model"}})
    d = build_decision(cfg, prompt="p", lineup=["codex"], requested="high")
    assert d.applied is True
    assert d.source == "explicit"
    assert d.classifier_version is None
    assert d.reason_codes == ("explicit_override",)
    assert dict(d.effective_selectors) == {"codex": "big-model"}


def test_explicit_auto_forces_observe_even_under_off_mode():
    d = build_decision(_cfg(mode="off"), prompt="show status", lineup=["claude"], requested="auto")
    assert d is not None
    assert d.source == "auto"
    assert d.applied is False


def test_explicit_all_unmapped_raises():
    with pytest.raises(AllUnmappedError, match="no eligible agent has a mapping"):
        build_decision(_cfg(), prompt="p", lineup=["claude", "codex"], requested="low")


def test_explicit_partial_mapping_allows_run():
    cfg = _cfg(models={"low": {"claude": "cheap"}})
    d = build_decision(cfg, prompt="p", lineup=["claude", "codex"], requested="low")
    assert dict(d.effective_selectors) == {"claude": "cheap", "codex": None}
    assert cli_default_count(d) == 1


def test_selectors_projected_onto_lineup_only():
    cfg = _cfg(models={"low": {"claude": "a", "codex": "b", "kimi-code": "c"}})
    d = build_decision(cfg, prompt="p", lineup=["codex"], requested="low")
    assert dict(d.recommended_selectors) == {"codex": "b"}
    assert dict(d.effective_selectors) == {"codex": "b"}


def test_decision_mappings_are_immutable():
    d = build_decision(_cfg(), prompt="show status", lineup=["claude"], requested=None)
    with pytest.raises(TypeError):
        d.effective_selectors["claude"] = "sneaky"  # type: ignore[index]


def test_as_dict_is_sorted_and_stable():
    cfg = _cfg(models={"medium": {"kimi-code": "k", "claude": "c"}})
    d = build_decision(
        cfg, prompt="p", lineup=["kimi-code", "claude"], requested="medium"
    )
    data = d.as_dict()
    assert list(data["recommended_selectors"]) == ["claude", "kimi-code"]
    assert list(data["effective_selectors"]) == ["claude", "kimi-code"]
    assert data["schema_version"] == 1
    # Serialization is the canonical single form — stable across calls.
    assert d.as_dict() == data


# ── decisions: inherit_decision ──────────────────────────────────────────


def _parent_block(**overrides):
    block = {
        "schema_version": 1,
        "requested": "low",
        "resolved_tier": "low",
        "source": "explicit",
        "classifier_version": None,
        "config_mode": "off",
        "applied": True,
        "reason_codes": ["explicit_override"],
        "recommended_selectors": {"claude": "cheap"},
        "effective_selectors": {"claude": "cheap"},
    }
    block.update(overrides)
    return block


def test_inherit_legacy_none_returns_none():
    assert inherit_decision(None, agent_id="claude") is None


def test_inherit_explicit_parent_reuses_selector():
    d = inherit_decision(_parent_block(), agent_id="claude")
    assert d.source == "inherited"
    assert d.applied is True
    assert dict(d.effective_selectors) == {"claude": "cheap"}
    assert "selector_affinity" in d.reason_codes
    assert "explicit_override" in d.reason_codes


def test_inherit_observe_parent_resumes_without_flag():
    block = _parent_block(
        requested="auto",
        resolved_tier="medium",
        source="auto",
        applied=False,
        classifier_version="prompt-complexity-v1",
        config_mode="observe",
        reason_codes=["default_medium"],
        recommended_selectors={"claude": "cheap"},
        effective_selectors={"claude": None},
    )
    d = inherit_decision(block, agent_id="claude")
    assert d.applied is False
    assert dict(d.effective_selectors) == {"claude": None}
    assert d.classifier_version == "prompt-complexity-v1"


@pytest.mark.parametrize(
    "corrupt",
    [
        "not-a-dict",
        {},
        _parent_block(schema_version=2),
        _parent_block(resolved_tier="ultra"),
        _parent_block(source="wat"),
        _parent_block(reason_codes=["unknown_reason"]),
        _parent_block(reason_codes="explicit_override"),
        _parent_block(effective_selectors="nope"),
        _parent_block(effective_selectors={"codex": "other"}),  # winner missing
        _parent_block(effective_selectors={"claude": "has space"}),
        _parent_block(effective_selectors={"claude": 42}),
    ],
)
def test_inherit_corrupt_block_fails_closed(corrupt):
    with pytest.raises(ValueError, match="corrupt model routing metadata"):
        inherit_decision(corrupt, agent_id="claude")


def test_inherit_is_idempotent_on_affinity_reason():
    # A continue-of-a-continue: the parent's block is itself inherited.
    block = _parent_block(
        source="inherited",
        reason_codes=["explicit_override", "selector_affinity"],
    )
    d = inherit_decision(block, agent_id="claude")
    assert d.reason_codes.count("selector_affinity") == 1


# ── banner ───────────────────────────────────────────────────────────────


def test_banner_observe_with_defaults():
    d = build_decision(
        _cfg(models={"low": {"claude": "cheap"}}),
        prompt="show status",
        lineup=["claude", "codex"],
        requested=None,
    )
    assert render_banner(d) == "model routing: low / observe / not applied / 1 cli-default"


def test_banner_explicit_applied():
    d = build_decision(
        _cfg(mode="off", models={"low": {"claude": "cheap"}}),
        prompt="p",
        lineup=["claude"],
        requested="low",
    )
    assert render_banner(d) == "model routing: low / explicit / applied"


def test_banner_inherited():
    d = inherit_decision(_parent_block(), agent_id="claude")
    assert render_banner(d) == "model routing: low / inherited / applied"


def test_decision_dataclass_is_frozen():
    d = ModelRoutingDecision(
        schema_version=1,
        requested="low",
        resolved_tier="low",
        source="explicit",
        classifier_version=None,
        config_mode="off",
        applied=True,
        reason_codes=("explicit_override",),
        recommended_selectors={"claude": "x"},
        effective_selectors={"claude": "x"},
    )
    with pytest.raises(AttributeError):
        d.resolved_tier = "high"  # type: ignore[misc]


def test_banner_auto_under_off_mode_renders_observe():
    # `--difficulty auto` with config mode=off IS a one-run observe (§3.2):
    # the banner must say observe, not leak the config_mode snapshot "off".
    d = build_decision(
        _cfg(mode="off"), prompt="show status", lineup=["claude"], requested="auto"
    )
    assert d.config_mode == "off"  # the serialized snapshot stays honest
    assert render_banner(d) == (
        "model routing: low / observe / not applied / 1 cli-default"
    )


def test_inherit_rejects_control_character_selector():
    # The canonical selector rule (incl. ASCII controls) applies to inherited
    # blocks too — a corrupt manifest can't inject a BEL/NUL into argv.
    block = _parent_block(effective_selectors={"claude": "bad\x07model"})
    with pytest.raises(ValueError, match="corrupt model routing metadata"):
        inherit_decision(block, agent_id="claude")


@pytest.mark.parametrize(
    "corrupt",
    [
        # Every canonical field is validated — not just the ones inherit uses.
        _parent_block(applied="yes"),
        {k: v for k, v in _parent_block().items() if k != "applied"},
        _parent_block(recommended_selectors="nope"),
        _parent_block(recommended_selectors={"claude": "has space"}),
        # Cross-field contradictions the builder can never produce:
        # unapplied (observe-shaped) with a non-null effective selector …
        _parent_block(applied=False),
        # … and applied with nothing effective anywhere.
        _parent_block(effective_selectors={"claude": None}),
    ],
)
def test_inherit_rejects_contradictory_or_partial_blocks(corrupt):
    with pytest.raises(ValueError, match="corrupt model routing metadata"):
        inherit_decision(corrupt, agent_id="claude")


def test_inherit_partial_mapping_parent_with_unmapped_winner():
    # Legitimate: parent applied a tier with a partial mapping and the WINNER
    # happened to be unmapped — inherited run resumes with no flag.
    block = _parent_block(
        recommended_selectors={"claude": None, "codex": "big"},
        effective_selectors={"claude": None, "codex": "big"},
    )
    d = inherit_decision(block, agent_id="claude")
    assert d.applied is False
    assert dict(d.effective_selectors) == {"claude": None}


def test_build_decision_empty_lineup_explicit_raises():
    with pytest.raises(AllUnmappedError):
        build_decision(
            _cfg(models={"low": {"claude": "x"}}),
            prompt="p",
            lineup=[],
            requested="low",
        )


@pytest.mark.parametrize(
    "corrupt",
    [
        # Blocks the canonical builder can never produce (R4 sweep):
        _parent_block(schema_version=True),  # bool is not the int 1
        _parent_block(recommended_selectors={"claude": "cheap", "codex": "big"}),
        _parent_block(  # applied but effective differs from recommended
            recommended_selectors={"claude": "cheap"},
            effective_selectors={"claude": "other"},
        ),
        _parent_block(source="auto"),  # auto with explicit-shaped fields
        _parent_block(source="auto", requested="auto"),  # auto but applied/no classifier
        _parent_block(  # explicit with a classifier stamp
            classifier_version="prompt-complexity-v1"
        ),
        _parent_block(reason_codes=[]),  # explicit requires explicit_override
        _parent_block(requested="high"),  # explicit requested != resolved
    ],
)
def test_inherit_rejects_builder_impossible_blocks(corrupt):
    with pytest.raises(ValueError, match="corrupt model routing metadata"):
        inherit_decision(corrupt, agent_id="claude")


@pytest.mark.parametrize(
    "corrupt",
    [
        # Inherited-lineage shapes inherit_decision can never emit (R5 sweep):
        _parent_block(source="inherited", reason_codes=["selector_affinity"]),
        _parent_block(  # auto ancestry but explicit-shaped fields
            source="inherited",
            reason_codes=["default_medium", "selector_affinity"],
        ),
        _parent_block(  # inherited maps must be keyed to the winner only
            source="inherited",
            reason_codes=["explicit_override", "selector_affinity"],
            recommended_selectors={"claude": "cheap", "codex": "cheap"},
            effective_selectors={"claude": "cheap", "codex": "cheap"},
        ),
        _parent_block(  # duplicate reasons
            source="inherited",
            reason_codes=[
                "explicit_override", "explicit_override", "selector_affinity",
            ],
        ),
        # Auto decision whose tier contradicts its classifier reason.
        _parent_block(
            requested="auto",
            source="auto",
            applied=False,
            classifier_version="prompt-complexity-v1",
            reason_codes=["high_risk"],  # implies high, but resolved is low
            effective_selectors={"claude": None},
            recommended_selectors={"claude": None},
        ),
    ],
)
def test_inherit_rejects_impossible_lineage(corrupt):
    with pytest.raises(ValueError, match="corrupt model routing metadata"):
        inherit_decision(corrupt, agent_id="claude")


def test_inherit_auto_ancestry_chain_is_accepted():
    # A continue-of-a-continue whose ancestor was an observe run.
    block = _parent_block(
        requested="auto",
        resolved_tier="high",
        source="inherited",
        applied=False,
        classifier_version="prompt-complexity-v1",
        config_mode="observe",
        reason_codes=["high_risk", "selector_affinity"],
        recommended_selectors={"claude": None},
        effective_selectors={"claude": None},
    )
    d = inherit_decision(block, agent_id="claude")
    assert d.applied is False and d.source == "inherited"
