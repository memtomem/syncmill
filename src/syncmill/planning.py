"""Bounded, inspectable execution plans for orchestration strategies."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from syncmill.config import SyncmillConfig


@dataclass(frozen=True)
class RunPlan:
    strategy: str
    agents: tuple[str, ...]
    phases: tuple[str, ...]
    maximum_agent_calls: int
    selection: str
    gate_commands: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


def build_run_plan(config: SyncmillConfig, agents: list[str]) -> RunPlan:
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
    return RunPlan(
        strategy=strategy,
        agents=tuple(agents),
        phases=phases,
        maximum_agent_calls=calls,
        selection=config.selection.mode,
        gate_commands=tuple(config.gate.commands),
    )
