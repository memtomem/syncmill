"""CouncilStrategy (v0.3) — multi-round propose → critique → synthesize."""

from __future__ import annotations

import time

from syncmill.agents import AgentRunner, aexecute_isolated
from syncmill.breaker import CircuitBreaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.monitor import Monitor
from syncmill.tracer import Tracer
from syncmill.strategies.base import (
    _aggregate_failure,
    _attempt_span,
    _breaker_all_open_failure,
    _breaker_record,
    _breaker_runnable,
    _collect_files_changed,
    _enrich_prompt,
    _gate_span,
    _monitor_phase,
    _monitor_status,
    _monitor_streaming,
    _no_stages_failure,
    _select_span,
    _select_winner,
    _trace_attempt,
    _trace_record,
)
from syncmill.worktree import Worktree, WorktreePool


def _council_propose(task_prompt: str) -> str:
    """Phase-1 instruction: each council member drafts its own solution.

    Wrapped by ``_enrich_prompt`` (which prepends the ``shared`` context as
    "Context from previous work" and labels this as the "Task"), so this reads as
    the assignment. Phrased to avoid a nested literal "Task:".
    """
    return (
        "You are one member of a council of independent AI coding agents, all "
        "solving the SAME problem in separate workspaces. Draft and implement your "
        "best solution in this worktree. Be concrete and explain your approach in "
        "your final message so the rest of the council can learn from it.\n\n"
        f"The problem to solve: {task_prompt}"
    )


def _council_critique(task_prompt: str) -> str:
    """Phase-2 instruction (repeated each critique round): refine after reading the
    council's shared proposals and prior critiques (carried in the prepended
    context). Deliberation flows ONLY through that shared context — the agent never
    sees another agent's worktree."""
    return (
        "You are one member of a council of independent AI coding agents solving "
        "the SAME problem. The context above holds the council's proposals and "
        "critiques so far. Critique those ideas, then refine YOUR solution in this "
        "worktree to incorporate the strongest points. Summarize what you changed "
        "and why in your final message.\n\n"
        f"The problem to solve: {task_prompt}"
    )


def _council_synthesize(task_prompt: str) -> str:
    """Phase-3 instruction: produce the final solution from the whole deliberation.

    Every member runs this; the council converges by selecting the winner among
    the synthesized candidates (``_select_winner``), so each member's output should
    be a complete, standalone solution — not a diff against a peer."""
    return (
        "You are one member of a council of independent AI coding agents that has "
        "deliberated on the SAME problem. The context above holds the council's "
        "proposals and critiques. Synthesize them into a single best FINAL solution "
        "in this worktree, combining the strongest ideas from the whole council "
        "into a complete, coherent implementation.\n\n"
        f"The problem to solve: {task_prompt}"
    )


class CouncilStrategy:
    """Multi-round deliberation. Agents PROPOSE, CRITIQUE over rounds, then
    SYNTHESIZE a converged final solution — exchanging context ONLY through the
    ``shared`` namespace (agent isolation preserved).

    Three kinds of concurrent phase, each fanned out over ``aexecute_isolated``
    (one private worktree per agent, reused across all phases so a member's
    solution accumulates), with all memory I/O done strictly between fan-outs
    (never ``run_async`` inside a gathered coroutine — the shared-loop rule):

    1. **Propose** — every runnable agent drafts a solution from the same enriched
       prompt; each completed proposal is published to ``shared``. The agents that
       complete become the council *members*. If none complete, it aggregates the
       failures (concurrent → max wall-clock, like Compete).
    2. **Critique** (``config.council.rounds`` times) — each member re-reads the
       council's proposals + prior critiques from ``shared`` (relevance-ranked,
       best-effort — the same handoff Pipeline uses) and refines its own worktree;
       each completed critique is published back to ``shared``. A failed critique
       is tolerated (not shared, member not dropped) — synthesis re-runs everyone
       and the winner selection sorts it out, so no per-member state is tracked.
    3. **Synthesize** — every member produces a final solution from the whole
       deliberation; the council converges by reusing Compete's ``_select_winner``
       (first gate-passing COMPLETED in priority order), so the result reflects
       more than one agent's input *when ``shared`` surfaces peers* — the
       relevance-ranked, best-effort handoff Pipeline uses, so each synthesizer's
       prompt carries the council's shared proposals/critiques (under a degraded
       memtomem it injects nothing and each synthesizer falls back to its own
       accumulated worktree). No member completing/passing the gate is an
       aggregate failure.

    The returned winner's ``duration_seconds`` is the council's wall-clock — the
    sum of each concurrent phase's slowest agent (propose + every critique round +
    synthesize) — not just the synthesizer's own time. The Supervisor shares the
    winner to ``shared`` and tears down every worktree, exactly as for Compete.

    **Breaker (#8) is recorded exactly ONCE per agent, at propose** — even though a
    member runs up to ``2 + rounds`` times. Propose is the unique phase where every
    runnable agent executes once, from identical input, so it is the fair
    "can this agent run?" probe the breaker wants. The breaker threshold is
    denominated in *runs*; recording every council execution would make council
    trip it ~``(2 + rounds)``× faster than the other strategies (a strategy-
    dependent regression) and conflate execution-reliability with a member
    struggling on the harder critique/synthesis prompts — the same reason a quality
    gate rejection never trips the breaker. Critique/synthesize re-runs of an
    already-recorded member are therefore not recorded, keeping council's
    contribution to the cross-run "N failures in a row" count one-per-agent-per-run,
    identical to Route/Pipeline/Compete.

    Tracing (S6): ``propose:{agent}`` fans out from ``run`` (with
    ``worktree_slot`` — the one phase that acquires fresh worktrees);
    ``critique:{agent}:{r}`` and ``synthesize:{agent}`` each parent on that
    member's OWN previous-phase node (worktree continuity — its execution really
    ran in, and possibly mutated, that worktree, so a FAILED critique still
    becomes the next phase's parent), while span ``links`` carry the nodes whose
    results were PUBLISHED to ``shared`` at the previous phase boundary —
    declared availability, never claimed consumption (the handoff is a
    relevance-ranked best-effort search, so actual consumption is unknowable).
    Synthesis reuses Compete's select-span semantics: gate spans for the lazily
    gated prefix, then ``select`` parented on the winning synthesize node (or
    ``run`` when nobody won) linking the other raw-COMPLETED candidates. The
    no-members early return leaves only propose spans (selection never ran, so
    there is no select span).
    """

    def execute(
        self,
        task: Task,
        agents: list[AgentRunner],
        memory: SyncmillMemory,
        pool: WorktreePool,
        config: SyncmillConfig,
        gate: QualityGate | None = None,
        breaker: CircuitBreaker | None = None,
        monitor: Monitor | None = None,
        tracer: Tracer | None = None,
    ) -> TaskResult:
        if not agents:  # Supervisor guards this; stay safe if run standalone
            return _no_stages_failure(task)

        # Drop circuit-open agents BEFORE acquiring worktrees / fanning out, so the
        # lists stay positionally aligned. If the breaker tripped them all, say so.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        # One isolated worktree per agent, acquired UP FRONT and SERIALLY (acquire
        # is sync git on the index lock; never inside the concurrent fan-out). Each
        # agent reuses its own worktree across every phase, so its work accumulates.
        worktrees = [pool.acquire(agent.agent_id) for agent in runnable]

        def timeout_for(aid: str) -> int:
            return config.agent_config(aid).timeout_seconds

        elapsed = 0.0  # council wall-clock: sum of each concurrent phase's slowest

        # ── Phase 1: PROPOSE (concurrent) ─────────────────────────────────
        propose_prompt = _enrich_prompt(
            _council_propose(task.prompt),
            run_async(memory.search_context(task.prompt)),
        )
        _monitor_phase(monitor, "propose")
        with _monitor_streaming(monitor, [a.agent_id for a in runnable]):
            results = run_async(
                aexecute_isolated(
                    runnable, propose_prompt, [wt.path for wt in worktrees], timeout_for
                )
            )
        propose_end = time.time()
        elapsed += max((r.duration_seconds for r in results), default=0.0)

        # Record EVERY proposer's raw outcome — the council's single breaker
        # recording point (see the class docstring), before any sharing/gating.
        # Every proposer (completer or failure) also leaves a propose span, so
        # later links to published proposals always resolve.
        for slot, (agent, result) in enumerate(zip(runnable, results)):
            _breaker_record(breaker, agent.agent_id, result.status)
            _monitor_status(monitor, agent.agent_id, result.status)
            _trace_attempt(tracer, _attempt_span(
                agent.agent_id,
                node_id=f"propose:{agent.agent_id}",
                parent="run",
                attempt=0,
                phase="propose",
                raw_status=result.status,
                error=result.error,
                exit_code=result.exit_code,
                end=propose_end,
                duration=result.duration_seconds,
                worktree_slot=slot,
            ), result)

        # The completers become the members; publish each proposal to `shared` so
        # the critique rounds can read it. members carry only (agent, worktree) —
        # no result state, because synthesis re-runs every member and _select_winner
        # is the single convergence point.
        members: list[tuple[AgentRunner, Worktree]] = []
        for agent, worktree, result in zip(runnable, worktrees, results):
            if result.status == TaskStatus.COMPLETED:
                members.append((agent, worktree))
                run_async(memory.share_result(task, result, kind="council-proposal"))

        if not members:
            return _aggregate_failure(
                task, results, duration_seconds=round(elapsed, 2)
            )

        member_agents = [agent for agent, _ in members]
        member_worktrees = [wt for _, wt in members]

        # Trace bookkeeping: each member's own previous-phase node (its next
        # span's parent — worktree continuity), and the nodes whose results were
        # PUBLISHED to `shared` at the last phase boundary (the next spans'
        # links — declared availability, the finding-6 approximation).
        prev_node = {a.agent_id: f"propose:{a.agent_id}" for a in member_agents}
        published = [
            f"propose:{agent.agent_id}"
            for agent, result in zip(runnable, results)
            if result.status == TaskStatus.COMPLETED
        ]

        # ── Phase 2: CRITIQUE × rounds (concurrent over members) ───────────
        member_ids = [a.agent_id for a in member_agents]
        for round_no in range(config.council.rounds):
            critique_prompt = _enrich_prompt(
                _council_critique(task.prompt),
                run_async(memory.search_context(task.prompt)),
            )
            _monitor_phase(monitor, f"critique {round_no + 1}/{config.council.rounds}")
            with _monitor_streaming(monitor, member_ids):
                critiques = run_async(
                    aexecute_isolated(
                        member_agents,
                        critique_prompt,
                        [wt.path for wt in member_worktrees],
                        timeout_for,
                    )
                )
            critique_end = time.time()
            elapsed += max((c.duration_seconds for c in critiques), default=0.0)
            # No breaker recording here (already recorded at propose). Share each
            # completed critique back to `shared`; a failed critique is tolerated
            # (not shared, member kept) — synthesis re-runs everyone regardless.
            round_published: list[str] = []
            for agent, critique in zip(member_agents, critiques):
                _monitor_status(monitor, agent.agent_id, critique.status)
                node_id = f"critique:{agent.agent_id}:{round_no}"
                _trace_attempt(tracer, _attempt_span(
                    agent.agent_id,
                    node_id=node_id,
                    parent=prev_node[agent.agent_id],
                    attempt=0,
                    phase="critique",
                    round_no=round_no,
                    links=tuple(published),
                    raw_status=critique.status,
                    error=critique.error,
                    exit_code=critique.exit_code,
                    end=critique_end,
                    duration=critique.duration_seconds,
                ), critique)
                # Unconditional: a FAILED critique still ran in (and possibly
                # mutated) the member's worktree, so it stays the next parent —
                # it just never becomes a peer's link target (published only).
                prev_node[agent.agent_id] = node_id
                if critique.status == TaskStatus.COMPLETED:
                    round_published.append(node_id)
                    run_async(
                        memory.share_result(task, critique, kind="council-critique")
                    )
            published = round_published

        # ── Phase 3: SYNTHESIZE (concurrent over members) + converge ───────
        synth_prompt = _enrich_prompt(
            _council_synthesize(task.prompt),
            run_async(memory.search_context(task.prompt)),
        )
        _monitor_phase(monitor, "synthesize")
        with _monitor_streaming(monitor, member_ids):
            synth_results = run_async(
                aexecute_isolated(
                    member_agents,
                    synth_prompt,
                    [wt.path for wt in member_worktrees],
                    timeout_for,
                )
            )
        synth_end = time.time()
        elapsed += max((s.duration_seconds for s in synth_results), default=0.0)
        # RAW pre-select snapshot for the spans: `_select_winner` downgrades
        # gate-failing completers in place, and the winner's duration is mutated
        # to the whole-council wall-clock below — the span must carry the
        # synthesizer's own raw outcome/duration (3-tuple, unlike compete's 2).
        raw = [(s.status, s.error, s.duration_seconds) for s in synth_results]
        # No breaker recording here either (already recorded at propose).
        for agent, synth in zip(member_agents, synth_results):
            _monitor_status(monitor, agent.agent_id, synth.status)

        # Converge via Compete's selection seam: first gate-passing COMPLETED
        # synthesis in priority order (a gate failer is downgraded in place so the
        # aggregate below names it). Nobody qualifying → aggregate failure.
        sel_start = time.time()
        winner = _select_winner(
            member_worktrees,
            synth_results,
            gate,
            mode=config.selection.mode,
            agents=member_agents,
            pool=pool,
            config=config,
            tracer=tracer,
        )
        winner_result: TaskResult | None = None
        if winner is not None:
            winner_result, winner_worktree = winner
            winner_result.duration_seconds = round(elapsed, 2)  # council wall-clock
            _collect_files_changed(winner_result, winner_worktree)  # its OWN diff
        sel_end = time.time()

        if tracer is not None:
            for (agent, synth), (raw_status, raw_error, raw_duration) in zip(
                zip(member_agents, synth_results), raw
            ):
                is_winner = synth is winner_result
                node_id = f"synthesize:{agent.agent_id}"
                _trace_attempt(tracer, _attempt_span(
                    agent.agent_id,
                    node_id=node_id,
                    parent=prev_node[agent.agent_id],  # its last critique node
                    attempt=0,
                    phase="synthesize",
                    links=tuple(published),  # the last round's published critiques
                    raw_status=raw_status,
                    error=raw_error,
                    exit_code=synth.exit_code,
                    end=synth_end,
                    duration=raw_duration,
                    winner=is_winner,
                    files_changed_count=(
                        len(synth.files_changed) if is_winner else None
                    ),
                ), synth)
                if synth.gate is not None:  # the lazily-gated prefix only
                    _trace_record(tracer, _gate_span(
                        agent.agent_id,
                        node_id=f"gate:{agent.agent_id}",
                        parent=node_id,
                        report=synth.gate,
                        end=sel_end,
                    ))
            links = [
                f"synthesize:{agent.agent_id}"
                for agent, synth, (raw_status, _, _) in zip(
                    member_agents, synth_results, raw
                )
                if raw_status == TaskStatus.COMPLETED and synth is not winner_result
            ]
            _trace_record(tracer, _select_span(
                winner_node=(
                    f"synthesize:{winner_result.agent_id}"
                    if winner_result is not None else None
                ),
                links=links,
                start=sel_start,
                end=sel_end,
            ))

        if winner_result is None:
            return _aggregate_failure(
                task, synth_results, duration_seconds=round(elapsed, 2)
            )
        return winner_result
