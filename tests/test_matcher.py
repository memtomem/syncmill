"""matcher.py — DeclarativeMatcher re-ordering, the build_matcher factory, and
the two acceptance criteria for capability matching (#9). Hermetic: a matcher is
a pure function of static config + Task.tags, so these need no subprocess, no
git, and no agent binaries."""

from __future__ import annotations

import logging

import pytest

from syncmill.config import CapabilityConfig
from syncmill.matcher import DeclarativeMatcher, build_matcher
from syncmill.models import Task

from conftest import FakeAgent

PRIORITY = ["claude", "codex", "kimi-code"]


def _agents(*ids: str) -> list[FakeAgent]:
    return [FakeAgent(i) for i in (ids or PRIORITY)]


def _ids(agents) -> list[str]:
    return [a.agent_id for a in agents]


# ── build_matcher (the disabled-default factory) ──────────────────────────


def test_build_matcher_none_when_no_capabilities():
    # empty map (the default) → None, so ordering stays at raw priority — mirrors
    # build_gate/build_breaker returning None when unconfigured.
    assert build_matcher(CapabilityConfig()) is None


def test_build_matcher_none_when_all_capabilities_normalize_away():
    # whitespace-/empty-only lists are not "configured": they normalize to nothing.
    cfg = CapabilityConfig(agents={"codex": ["", "  "], "claude": []})
    assert build_matcher(cfg) is None


def test_build_matcher_returns_matcher_when_any_capability_declared():
    cfg = CapabilityConfig(agents={"codex": ["python"]})
    assert isinstance(build_matcher(cfg), DeclarativeMatcher)


def test_build_matcher_warns_on_unknown_agent_key(caplog):
    cfg = CapabilityConfig(agents={"codez": ["python"]})  # typo for codex
    with caplog.at_level(logging.WARNING, logger="syncmill.matcher"):
        m = build_matcher(cfg, known_agent_ids=PRIORITY)
    assert m is not None  # advisory: still builds, never raises
    assert "codez" in caplog.text and "unknown agent" in caplog.text.lower()


def test_build_matcher_no_warning_when_keys_known(caplog):
    cfg = CapabilityConfig(agents={"codex": ["python"]})
    with caplog.at_level(logging.WARNING, logger="syncmill.matcher"):
        build_matcher(cfg, known_agent_ids=PRIORITY)
    assert caplog.text == ""


def test_build_matcher_no_warning_without_known_ids(caplog):
    # display path passes no known_agent_ids → no validation, no warning.
    cfg = CapabilityConfig(agents={"codez": ["python"]})
    with caplog.at_level(logging.WARNING, logger="syncmill.matcher"):
        build_matcher(cfg)
    assert caplog.text == ""


# ── order(): the two acceptance criteria ───────────────────────────────────


def test_order_promotes_matching_agent_ahead_of_priority():
    # ACCEPTANCE CRITERION 1: a hinted task routes to a matching agent ahead of
    # raw priority — codex (priority 2) declares python, so it leads claude.
    m = build_matcher(CapabilityConfig(agents={"codex": ["python"]}))
    out = m.order(_agents(), Task(prompt="x", tags=["python"]))
    assert _ids(out) == ["codex", "claude", "kimi-code"]


def test_order_falls_back_to_priority_when_nothing_matches():
    # ACCEPTANCE CRITERION 2: nothing matches → original priority order verbatim.
    m = build_matcher(CapabilityConfig(agents={"codex": ["python"]}))
    out = m.order(_agents(), Task(prompt="x", tags=["cobol"]))
    assert _ids(out) == PRIORITY


def test_order_identity_when_no_task_tags():
    # no hint (the default CLI invocation) → raw priority, even with a matcher.
    m = build_matcher(CapabilityConfig(agents={"codex": ["python"]}))
    out = m.order(_agents(), Task(prompt="x"))
    assert _ids(out) == PRIORITY


# ── order(): semantics ─────────────────────────────────────────────────────


def test_order_stable_tie_break_by_priority():
    # two agents both match → equal score → keep input (priority) relative order.
    m = build_matcher(
        CapabilityConfig(agents={"codex": ["python"], "kimi-code": ["python"]})
    )
    out = m.order(_agents(), Task(prompt="x", tags=["python"]))
    # codex before kimi-code (both score 1, priority order), claude (0) last.
    assert _ids(out) == ["codex", "kimi-code", "claude"]


def test_order_scores_by_intersection_size():
    # an agent covering more of the hint ranks ahead of one covering less.
    m = build_matcher(
        CapabilityConfig(
            agents={"codex": ["python", "refactor"], "claude": ["python"]}
        )
    )
    out = m.order(_agents(), Task(prompt="x", tags=["python", "refactor"]))
    assert _ids(out) == ["codex", "claude", "kimi-code"]  # 2, 1, 0


def test_order_case_and_whitespace_insensitive():
    m = build_matcher(CapabilityConfig(agents={"codex": ["Python"]}))
    out = m.order(_agents(), Task(prompt="x", tags=["  PYTHON  "]))
    assert _ids(out)[0] == "codex"


def test_order_hyphenated_agent_id_key():
    # the map keys on the real agent id, so kimi-code is a verbatim key (no
    # hyphen→underscore mapping, unlike AgentConfig fields).
    m = build_matcher(CapabilityConfig(agents={"kimi-code": ["rust"]}))
    out = m.order(_agents(), Task(prompt="x", tags=["rust"]))
    assert _ids(out)[0] == "kimi-code"


def test_order_duplicate_tags_do_not_inflate_score():
    m = build_matcher(
        CapabilityConfig(agents={"codex": ["python"], "claude": ["python", "rust"]})
    )
    # claude covers python+rust; codex covers python. With ['python','python']
    # the set dedups to {python}, so both score 1 → priority order (claude first).
    out = m.order(_agents(), Task(prompt="x", tags=["python", "python"]))
    assert _ids(out) == PRIORITY


def test_order_never_drops_an_agent():
    # re-orders, never filters: output is a permutation of the input.
    m = build_matcher(CapabilityConfig(agents={"codex": ["python"]}))
    agents = _agents()
    out = m.order(agents, Task(prompt="x", tags=["python", "cobol", "haskell"]))
    assert sorted(_ids(out)) == sorted(PRIORITY)
    assert len(out) == len(agents)


def test_order_is_deterministic_and_non_destructive():
    m = build_matcher(CapabilityConfig(agents={"kimi-code": ["rust"]}))
    agents = _agents()
    task = Task(prompt="x", tags=["rust"])
    first = _ids(m.order(agents, task))
    second = _ids(m.order(agents, task))
    assert first == second  # identical inputs → identical permutation
    assert _ids(agents) == PRIORITY  # input list object unmutated
    assert task.tags == ["rust"]  # the hint is read, never normalized in place


def test_order_undeclared_agent_scores_zero():
    # an agent with no entry in the map is simply a non-matcher (score 0).
    m = build_matcher(CapabilityConfig(agents={"codex": ["python"]}))
    out = m.order(_agents("claude", "codex"), Task(prompt="x", tags=["python"]))
    assert _ids(out) == ["codex", "claude"]
