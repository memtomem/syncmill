"""Bounded, inspectable execution plans for orchestration strategies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from typing import Any

from syncmill import __version__
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
    elif strategy == "decompose":
        phases = ("plan", "subtasks", "synthesize", "select")
        calls = 1 + config.decompose.max_subtasks + n
    else:
        # Mirror _make_strategy's hard fail: a bare else silently attributed an
        # unknown strategy to decompose's topology — a wrong promise recorded
        # into the plan artifact. Unreachable via the CLI (validated there);
        # programmatic callers get the same error the strategy registry raises.
        raise ValueError(f"unknown strategy: {strategy!r}")
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
        # selection.mode only means something where a `select` phase exists
        # (compete/council/decompose reach _select_winner); printing `judge`
        # for a route/pipeline dry-run promised a selection that never runs.
        selection=config.selection.mode if "select" in phases else "n/a",
        gate_commands=tuple(config.gate.commands),
        model_routing=model_routing,
    )


def build_control_plan(
    config: SyncmillConfig,
    agents: list[str],
    *,
    run_id: str,
) -> dict:
    """Project the implemented strategy into Toolgraph's body-free v1 DAG.

    This is an upper-bound declaration, not an execution transcript. It contains
    no prompt, output, command, model selector, repository path, or checkpoint.
    Missing identity mappings receive a run-local sentinel principal so the
    consumer can classify the mapping failure without omitting a participant.
    """
    if not run_id:
        raise ValueError("control plan run_id must not be empty")
    toolgraph = config.integration.toolgraph
    if not toolgraph.candidate_tools:
        raise ValueError("control plan requires toolgraph candidate_tools")
    if not agents:
        raise ValueError("control plan requires at least one agent")

    mapped_by_agent: dict[str, str] = {}
    for agent_id in agents:
        mapped = toolgraph.agent_map.get(agent_id)
        if mapped is None:
            suffix = hashlib.sha256(agent_id.encode()).hexdigest()[:16]
            mapped = f"syncmill-unmapped-{suffix}"
        mapped_by_agent[agent_id] = mapped
    principals = list(dict.fromkeys(mapped_by_agent.values()))
    timeout = max(config.agent_config(agent).timeout_seconds for agent in agents)
    n = len(agents)

    nodes: list[dict] = [
        {"id": "start", "kind": "start", "principals": [], "agent_call": False}
    ]
    edges: list[dict] = []
    previous = "start"

    def append_validator() -> None:
        nonlocal previous
        nodes.append(
            {
                "id": "verify",
                "kind": "validator",
                "principals": [],
                "agent_call": False,
                "validator_scope": "workspace_result",
            }
        )
        edges.append(
            {
                "from": previous,
                "to": "verify",
                "kind": "always",
                "decided_by": "code",
            }
        )
        previous = "verify"

    def append_agent_node(
        node_id: str,
        *,
        invocations: int,
        parallelism: int,
        node_principals: list[str] | None = None,
        model_fanout: bool = False,
    ) -> None:
        nonlocal previous
        selected = principals if node_principals is None else node_principals
        nodes.append(
            {
                "id": node_id,
                "kind": "agent",
                "principals": selected,
                "agent_call": True,
                "max_invocations": invocations,
                "max_parallelism": min(parallelism, invocations),
                "timeout_seconds": timeout,
            }
        )
        edge = {
            "from": previous,
            "to": node_id,
            "kind": "fanout" if parallelism > 1 else "always",
            "decided_by": "model" if model_fanout else "code",
        }
        if parallelism > 1:
            edge["max_fan_out"] = invocations
        edges.append(edge)
        previous = node_id

    strategy = config.default_strategy
    if strategy in {"route", "pipeline"}:
        append_agent_node(strategy, invocations=n, parallelism=1)
    elif strategy == "compete":
        append_agent_node("compete", invocations=n, parallelism=n)
    elif strategy == "council":
        append_agent_node("propose", invocations=n, parallelism=n)
        for index in range(config.council.rounds):
            append_agent_node(
                f"critique-{index + 1}", invocations=n, parallelism=n
            )
        append_agent_node("synthesize", invocations=n, parallelism=n)
    elif strategy == "decompose":
        append_agent_node(
            "plan",
            invocations=1,
            parallelism=1,
            node_principals=[mapped_by_agent[agents[0]]],
        )
        append_agent_node(
            "subtasks",
            invocations=config.decompose.max_subtasks,
            parallelism=config.decompose.max_subtasks,
            model_fanout=True,
        )
        append_agent_node("synthesize", invocations=n, parallelism=n)
    else:
        raise ValueError(f"unknown strategy: {strategy!r}")

    if strategy in {"compete", "council", "decompose"}:
        if config.gate.commands:
            append_validator()
        nodes.append(
            {
                "id": "select",
                "kind": "router",
                "principals": [],
                "agent_call": False,
            }
        )
        edges.append(
            {
                "from": previous,
                "to": "select",
                "kind": "join",
                "decided_by": "code",
            }
        )
        previous = "select"
        if config.selection.mode == "judge":
            configured_judge = config.selection.judge_agent
            judge_id = configured_judge if configured_judge in agents else agents[0]
            judge_principal = mapped_by_agent[judge_id]
            append_agent_node(
                "judge",
                invocations=1,
                parallelism=1,
                node_principals=[judge_principal],
            )

    elif config.gate.commands:
        append_validator()

    nodes.append(
        {"id": "end", "kind": "end", "principals": [], "agent_call": False}
    )
    edges.append(
        {"from": previous, "to": "end", "kind": "success", "decided_by": "code"}
    )
    run_plan = build_run_plan(config, agents)
    maximum_parallelism = max(
        (node.get("max_parallelism", 0) for node in nodes), default=1
    )
    return {
        "schema_version": 1,
        "kind": "toolgraph.control-plan",
        "run_id": run_id,
        "producer": {"name": "syncmill", "version": __version__},
        "principals": [
            {"id": principal, "candidate_tools": list(toolgraph.candidate_tools)}
            for principal in principals
        ],
        "nodes": nodes,
        "edges": edges,
        "limits": {
            "maximum_agent_calls": run_plan.maximum_agent_calls,
            "maximum_parallelism": maximum_parallelism,
            "maximum_cycles": 0,
        },
    }
