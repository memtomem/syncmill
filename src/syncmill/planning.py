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


UNMAPPED_PRINCIPAL_PREFIX = "syncmill-unmapped-"


def unmapped_principal_id(agent_id: str) -> str:
    """The synthetic principal for an agent with no ``agent_map`` entry.

    The one place this spelling is produced. A consumer deciding whether a
    principal is synthetic must compare against THIS, computed from the agents
    that actually had no mapping — never against the prefix, which an explicit
    mapping is free to use.
    """
    suffix = hashlib.sha256(agent_id.encode()).hexdigest()[:16]
    return f"{UNMAPPED_PRINCIPAL_PREFIX}{suffix}"


def control_principal_id(config: SyncmillConfig, agent_id: str) -> str:
    """Return the stable Toolgraph principal for one SyncMill agent."""
    mapped = config.integration.toolgraph.agent_map.get(agent_id)
    if mapped is not None:
        return mapped
    return unmapped_principal_id(agent_id)


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
        phases = (
            ("propose",)
            + tuple(f"critique-{i + 1}" for i in range(config.council.rounds))
            + ("synthesize", "select")
        )
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

    mapped_by_agent = {
        agent_id: control_principal_id(config, agent_id) for agent_id in agents
    }
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
            append_agent_node(f"critique-{index + 1}", invocations=n, parallelism=n)
        append_agent_node("synthesize", invocations=n, parallelism=n)
    elif strategy == "decompose":
        # Every agent that could end up planning, not just the one that would if
        # nothing were filtered. Execution takes `runnable[0]`, which is the
        # first agent the circuit breaker did NOT skip, so declaring only
        # `agents[0]` describes a planner that may not be the one that runs —
        # the evidence would name Claude while Codex planned. The bound stays
        # one invocation; this widens WHO may spend it, not how many.
        #
        # No `node_principals`: the default is exactly this set, already
        # deduplicated. Rebuilding it by comprehension emits one entry per
        # AGENT, and two agents may map to one identity — a configuration the
        # agent_map allows — which the schema rejects for non-unique elements.
        append_agent_node("plan", invocations=1, parallelism=1)
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
            # Same divergence as the decompose planner: `_select_winner` falls
            # back to the first agent it was handed when the configured judge is
            # not among them, and the breaker decides what it is handed. The
            # configured judge leads the declaration so the common case reads
            # first, and every agent that could stand in follows it.
            configured_judge = config.selection.judge_agent
            ordered = (
                [configured_judge] + [a for a in agents if a != configured_judge]
                if configured_judge in agents
                else list(agents)
            )
            # Deduplicated, order preserved: two agents may map to one identity,
            # and the schema rejects a node whose principals repeat.
            append_agent_node(
                "judge",
                invocations=1,
                parallelism=1,
                node_principals=list(
                    dict.fromkeys(mapped_by_agent[agent] for agent in ordered)
                ),
            )

    elif config.gate.commands:
        append_validator()

    nodes.append({"id": "end", "kind": "end", "principals": [], "agent_call": False})
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


def build_workflow_control_plan(
    config: SyncmillConfig,
    workflow_nodes: list[dict[str, Any]],
    *,
    run_id: str,
    final_node: str,
) -> dict:
    """Project a validated, body-free MillFlow DAG into control-plan v1."""
    if not run_id:
        raise ValueError("control plan run_id must not be empty")
    if not workflow_nodes:
        raise ValueError("workflow control plan requires at least one node")
    toolgraph = config.integration.toolgraph
    if not toolgraph.candidate_tools:
        raise ValueError("control plan requires toolgraph candidate_tools")

    ids = [str(row["id"]) for row in workflow_nodes]
    if len(ids) != len(set(ids)):
        raise ValueError("workflow control-plan node ids must be unique")
    if final_node not in set(ids):
        raise ValueError(f"unknown workflow final node: {final_node!r}")

    agent_by_node = {str(row["id"]): str(row["agent"]) for row in workflow_nodes}
    mapped_by_agent = {
        agent: control_principal_id(config, agent)
        for agent in dict.fromkeys(agent_by_node.values())
    }
    principals = list(dict.fromkeys(mapped_by_agent.values()))
    plan_nodes: list[dict[str, Any]] = [
        {
            "id": "control:start",
            "kind": "start",
            "principals": [],
            "agent_call": False,
        }
    ]
    edges: list[dict[str, Any]] = []

    roots = [
        str(row["id"]) for row in workflow_nodes if not list(row.get("needs") or [])
    ]
    if len(roots) > 1:
        plan_nodes.append(
            {
                "id": "control:fanout:roots",
                "kind": "fanout",
                "principals": [],
                "agent_call": False,
            }
        )
        edges.append(
            {
                "from": "control:start",
                "to": "control:fanout:roots",
                "kind": "always",
                "decided_by": "code",
            }
        )

    for row in workflow_nodes:
        node_id = str(row["id"])
        agent_id = str(row["agent"])
        needs = [str(value) for value in row.get("needs") or []]
        plan_node_id = f"workflow:{node_id}"
        plan_nodes.append(
            {
                "id": plan_node_id,
                "kind": "agent",
                "principals": [mapped_by_agent[agent_id]],
                "agent_call": True,
                "max_invocations": int(row["max_attempts"]),
                "max_parallelism": 1,
                "timeout_seconds": int(row["timeout_seconds"]),
            }
        )
        if not needs:
            source = "control:fanout:roots" if len(roots) > 1 else "control:start"
            edge: dict[str, Any] = {
                "from": source,
                "to": plan_node_id,
                "kind": "fanout" if len(roots) > 1 else "always",
                "decided_by": "code",
            }
            if len(roots) > 1:
                edge["max_fan_out"] = len(roots)
            edges.append(edge)
        elif len(needs) == 1:
            edges.append(
                {
                    "from": f"workflow:{needs[0]}",
                    "to": plan_node_id,
                    "kind": "success",
                    "decided_by": "code",
                }
            )
        else:
            join_id = f"control:join:{node_id}"
            plan_nodes.append(
                {
                    "id": join_id,
                    "kind": "join",
                    "principals": [],
                    "agent_call": False,
                }
            )
            for dependency in needs:
                edges.append(
                    {
                        "from": f"workflow:{dependency}",
                        "to": join_id,
                        "kind": "join",
                        "decided_by": "code",
                    }
                )
            edges.append(
                {
                    "from": join_id,
                    "to": plan_node_id,
                    "kind": "success",
                    "decided_by": "code",
                }
            )

    plan_nodes.append(
        {
            "id": "control:end",
            "kind": "end",
            "principals": [],
            "agent_call": False,
        }
    )
    for node_id in ids:
        edges.append(
            {
                "from": f"workflow:{node_id}",
                "to": "control:end",
                "kind": "failure",
                "decided_by": "code",
            }
        )

    final_source = f"workflow:{final_node}"
    if config.gate.commands:
        validator_id = "control:validator:final"
        plan_nodes.insert(
            -1,
            {
                "id": validator_id,
                "kind": "validator",
                "principals": [],
                "agent_call": False,
                "validator_scope": "workspace_result",
            },
        )
        edges.extend(
            [
                {
                    "from": final_source,
                    "to": validator_id,
                    "kind": "success",
                    "decided_by": "code",
                },
                {
                    "from": validator_id,
                    "to": "control:end",
                    "kind": "success",
                    "decided_by": "code",
                },
                {
                    "from": validator_id,
                    "to": "control:end",
                    "kind": "failure",
                    "decided_by": "code",
                },
            ]
        )
    else:
        edges.append(
            {
                "from": final_source,
                "to": "control:end",
                "kind": "success",
                "decided_by": "code",
            }
        )

    maximum_calls = sum(int(row["max_attempts"]) for row in workflow_nodes)
    maximum_parallelism = min(config.workflow.max_parallel, len(workflow_nodes))
    return {
        "schema_version": 1,
        "kind": "toolgraph.control-plan",
        "run_id": run_id,
        "producer": {"name": "syncmill", "version": __version__},
        "principals": [
            {
                "id": principal,
                "candidate_tools": list(toolgraph.candidate_tools),
            }
            for principal in principals
        ],
        "nodes": plan_nodes,
        "edges": edges,
        "limits": {
            "maximum_agent_calls": maximum_calls,
            "maximum_parallelism": maximum_parallelism,
            "maximum_cycles": 0,
        },
    }
