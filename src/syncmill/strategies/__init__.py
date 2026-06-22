"""Orchestration strategies — the ``Strategy`` Protocol, its implementations,
and the name→strategy registry.

Extracted from ``supervisor.py`` (#12) once the four strategies made it
unwieldy. The registry (``_STRATEGIES`` + ``_make_strategy`` +
``available_strategies``) lives here at the package root because it needs every
concrete strategy class; ``base`` holds the Protocol and the cross-strategy
helpers, and each strategy gets its own module. The Supervisor imports
``_make_strategy``/``Strategy`` from here — selection-by-``default_strategy`` is
unchanged.
"""

from __future__ import annotations

from syncmill.strategies.base import (
    Strategy,
    _aggregate_failure,
    _git_changed_files,
    _select_winner,
)
from syncmill.strategies.compete import CompeteStrategy
from syncmill.strategies.council import CouncilStrategy
from syncmill.strategies.decompose import DecomposeStrategy
from syncmill.strategies.pipeline import PipelineStrategy
from syncmill.strategies.route import RouteStrategy

_STRATEGIES: dict[str, type[Strategy]] = {
    "route": RouteStrategy,
    "pipeline": PipelineStrategy,
    "compete": CompeteStrategy,
    "council": CouncilStrategy,
    "decompose": DecomposeStrategy,
}

#: The SEQUENTIAL strategies — they run agents one at a time via the sync
#: ``execute`` path, so the async-only live-monitor seam (#16) can't stream them.
SEQUENTIAL_STRATEGIES: frozenset[str] = frozenset({"route", "pipeline"})

#: The strategies that fan agents out CONCURRENTLY (over ``aexecute``) — the only
#: ones the live tmux monitor (#16) can stream. DERIVED from ``_STRATEGIES`` (all
#: minus the sequential ones) so a newly-registered concurrent strategy is
#: monitorable automatically and the set can't silently drift out of sync with the
#: registry. ``--monitor`` no-ops (with a CLI notice) for a sequential strategy.
CONCURRENT_STRATEGIES: frozenset[str] = frozenset(_STRATEGIES) - SEQUENTIAL_STRATEGIES


def available_strategies() -> list[str]:
    """Registered strategy names, sorted — used by the CLI to validate the
    ``--strategy`` option."""
    return sorted(_STRATEGIES)


def _make_strategy(name: str) -> Strategy:
    """Instantiate a strategy by name, raising on an unknown name.

    Mirrors ``AgentRegistry``'s hard failure on unknown agent ids: a typo'd
    strategy should be a clear error, not a silent fall back to RouteStrategy.
    """
    if name not in _STRATEGIES:
        valid = ", ".join(sorted(_STRATEGIES))
        raise ValueError(f"Unknown strategy: {name!r}. Valid strategies: {valid}.")
    return _STRATEGIES[name]()


__all__ = [
    "Strategy",
    "RouteStrategy",
    "PipelineStrategy",
    "CompeteStrategy",
    "CouncilStrategy",
    "DecomposeStrategy",
    "available_strategies",
    "CONCURRENT_STRATEGIES",
    "SEQUENTIAL_STRATEGIES",
    "_make_strategy",
    "_STRATEGIES",
    # Re-exported for direct import by tests / callers that reach into the
    # selection + diff-collection helpers (their canonical home is `base`).
    "_select_winner",
    "_aggregate_failure",
    "_git_changed_files",
]
