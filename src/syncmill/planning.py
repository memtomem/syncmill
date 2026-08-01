"""Bounded, inspectable execution plans for orchestration strategies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from syncmill.config import SyncmillConfig


@dataclass(frozen=True)
class RunPlan:
    strategy: str
    agents: tuple[str, ...]
    phases: tuple[str, ...]
    maximum_agent_calls: int
    selection: str
    gate_commands: tuple[str, ...]
    # The canonical model-routing decision dict, or None when routing is off.
    # as_dict() OMITS the key when None so disabled artifacts stay identical.
    model_routing: dict[str, Any] | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        if data["model_routing"] is None:
            del data["model_routing"]
        return data


def build_run_plan(
    config: SyncmillConfig,
    agents: list[str],
    *,
    model_routing: dict[str, Any] | None = None,
) -> RunPlan:
    """Mirror the bounded phase topology implemented in ``strategies/*.py``.

    Update this projection whenever a concrete strategy adds or removes a phase;
    tests should compare its call bound with that strategy's configured topology.
    """
    n = len(agents)
    strategy = config.default_strategy
    if strategy == "route":
        phases, calls = ("route",), n
    elif strategy == "pipeline":
        phases, calls = ("pipeline",), n
    elif strategy == "compete":
        phases, calls = ("compete", "select"), n
    elif strategy == "council":
        phases = ("propose",) + tuple(
            f"critique-{i + 1}" for i in range(config.council.rounds)
        ) + ("synthesize", "select")
        calls = n * (config.council.rounds + 2)
    else:
        phases = ("plan", "subtasks", "synthesize", "select")
        calls = 1 + config.decompose.max_subtasks + n
    # Judge selection spends one MORE agent call than the phases above: with two
    # or more gate-verified candidates, `_select_winner` runs a judge agent in its
    # own worktree. The bound is a promise the artifact records, so it has to
    # cover every call the run can actually make — the sequential strategies never
    # reach that seam, so it only applies where a `select` phase exists.
    if config.selection.mode == "judge" and "select" in phases:
        calls += 1
    return RunPlan(
        strategy=strategy,
        agents=tuple(agents),
        phases=phases,
        maximum_agent_calls=calls,
        selection=config.selection.mode,
        gate_commands=tuple(config.gate.commands),
        model_routing=model_routing,
    )
