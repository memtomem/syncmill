"""Capability matching (#9) — declarative, config-driven agent re-ordering.

Routing's baseline is a fixed priority order (``agent_priority``). Capability
matching layers *over* that baseline: given a task's capability hint (its
``Task.tags``) and each agent's **declared** capabilities (from config), it
re-orders the candidate agents so a matching agent is preferred ahead of raw
priority — falling back to priority order verbatim when nothing matches.

DECLARATIVE, never a probe — honoring the "no ``health_check`` on agents"
invariant. The matcher never runs, inspects, or pings a binary; a score is a
pure ``set``-intersection of static config (declared capabilities) with the
task's hint. It is strictly more passive than the circuit breaker (which at
least reads *recorded* outcomes) — the matcher reads only declared config.

RE-ORDERS, never DROPS. ``order`` returns a permutation of its input — every
agent is still present — so nothing is ever stranded: RouteStrategy's fallback
chain, PipelineStrategy's stages, and CompeteStrategy's competitors all stay
intact, just re-prioritized. (Filtering would risk emptying the pool and break
the "fall back to priority when nothing matches" criterion.)

Like quality gates and the circuit breaker, capability matching is plumbed
**outside** the agent layer — agents stay pure ``execute(prompt, workdir,
timeout)`` functions and never see a matcher, a Task, or a capability. Unlike
the gate/breaker (whose verdicts mean different things per strategy, so they are
threaded *into* ``Strategy.execute``), capability matching has **no
topology-specific semantics**: it produces exactly one artifact — a re-ordering
of the flat candidate list every strategy already consumes as a priority
sequence — so the Supervisor applies it **once**, before dispatching to the
strategy, and all three strategies become capability-aware for free.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from syncmill.agents import AgentRunner
from syncmill.config import CapabilityConfig
from syncmill.models import Task

logger = logging.getLogger(__name__)


@runtime_checkable
class CapabilityMatcher(Protocol):
    """Re-orders candidate agents by declared-capability fit for a task.

    Pure: ``(agents, task)`` in, re-ordered agents out — like agents it knows
    nothing about the Supervisor, memory, or strategies, and NEVER touches an
    agent's ``execute``.
    """

    def order(self, agents: list[AgentRunner], task: Task) -> list[AgentRunner]: ...


class DeclarativeMatcher:
    """Stable-re-orders agents by how many of ``task.tags`` each agent's declared
    capabilities cover.

    Re-orders, never drops, so non-matching agents remain as fallback. The score
    is the size of the intersection between an agent's declared capabilities and
    the task's (normalized) hint tokens. A descending **stable** sort means equal
    scores keep their input (priority) relative order — priority is the
    deterministic secondary key, implicitly, via sort stability. Deterministic:
    the key is a pure function of static config + ``task.tags`` (no wall-clock, no
    ``duration_seconds``, no set-iteration nondeterminism — intersection *size* is
    order-free).
    """

    def __init__(self, capabilities: dict[str, frozenset[str]]) -> None:
        # agent_id -> its declared capabilities (already normalized: stripped,
        # lower-cased, empties dropped). build_matcher does the normalization so
        # the hot path is a plain set-intersection.
        self._caps = capabilities

    def _score(self, agent_id: str, wanted: set[str]) -> int:
        return len(self._caps.get(agent_id, frozenset()) & wanted)

    def order(self, agents: list[AgentRunner], task: Task) -> list[AgentRunner]:
        # Normalize the hint the same way capabilities were normalized, so
        # matching is case-/whitespace-insensitive without mutating Task.tags
        # (the stored hint keeps the user's raw casing). A set dedups, so a
        # repeated tag can't inflate a score.
        wanted = {t.strip().lower() for t in task.tags if t.strip()}
        if not wanted:
            return list(agents)  # no hint → raw priority order (criterion 2)
        # Stable descending-score sort: matchers (score >= 1) move ahead of all
        # non-matchers (score 0), and equal-score agents keep priority order.
        return sorted(agents, key=lambda a: -self._score(a.agent_id, wanted))


def build_matcher(
    config: CapabilityConfig, known_agent_ids: list[str] | None = None
) -> CapabilityMatcher | None:
    """The matcher described by ``config``, or ``None`` when no agent declares any
    capability (the disabled default) — the Supervisor then leaves agent order at
    raw priority. Mirrors ``build_gate``/``build_breaker``: a single place that
    decides "configured or not", so the disabled path constructs nothing.

    ``known_agent_ids`` (when given, the configured ``agent_priority``) is used
    only to surface a config typo: a capability declared for an id not in the
    lineup is **advisory-warned** (``logger.warning``, never raised) — it stays an
    inert no-op, but it's surfaced rather than silently scoring nothing, matching
    syncmill's "name the typo" posture. The free-form capability *tokens* are NOT
    validated — an unmatched token is a valid no-op (the graceful fallback).
    """
    normalized = {
        aid: frozenset(c.strip().lower() for c in caps if c.strip())
        for aid, caps in config.agents.items()
    }
    # Drop agents whose declared list normalizes away entirely (e.g. [""], ["  "])
    # so they don't count as "configured" and can't keep the feature on by accident.
    normalized = {aid: caps for aid, caps in normalized.items() if caps}
    if not normalized:
        return None
    if known_agent_ids is not None:
        unknown = sorted(aid for aid in normalized if aid not in known_agent_ids)
        if unknown:
            logger.warning(
                "capability descriptors declared for unknown agent id(s): %s "
                "(not in agent_priority: %s) — these will never match",
                ", ".join(unknown),
                ", ".join(known_agent_ids),
            )
    return DeclarativeMatcher(normalized)
