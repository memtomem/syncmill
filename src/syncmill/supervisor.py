"""Orchestration — Strategy pattern + Supervisor.

Supervisor is the sole mediator. Agents don't know about each other or about memory.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from syncmill.agents import AgentRegistry, AgentRunner, aexecute_isolated
from syncmill.breaker import CircuitBreaker, build_breaker
from syncmill.config import SyncmillConfig
from syncmill.gates import QualityGate, build_gate
from syncmill.matcher import CapabilityMatcher, build_matcher
from syncmill.memory import SyncmillMemory, run_async
from syncmill.models import Task, TaskResult, TaskStatus
from syncmill.worktree import Worktree, WorktreePool

logger = logging.getLogger(__name__)


# ── Strategy Protocol ────────────────────────────────────────────────────


@runtime_checkable
class Strategy(Protocol):
    """Orchestration strategy — determines how agents are selected and run.

    A strategy is handed a ``WorktreePool`` rather than a single ``workdir``: it
    owns its worktree *topology* (one isolated worktree per agent for concurrent
    isolation, or a single shared one for a sequential pipeline), acquiring from
    the pool as it runs. The Supervisor owns the pool's *lifetime* and tears
    every acquired worktree down afterward. The strategy also sets
    ``files_changed`` on the result it returns (only it knows which worktree
    produced that result) via ``_collect_files_changed``.

    The optional ``gate`` (#10) is a post-finish quality check the strategy runs
    against an agent's worktree before accepting its result; ``None`` means no
    gate is configured and the strategy accepts a completed result as today.

    The optional ``breaker`` (#8) is a reactive circuit breaker the strategy
    consults to skip agents whose circuit is OPEN (too many recent failures) and
    to record each agent's raw execution outcome; ``None`` means no breaker is
    configured and every agent runs as today.
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
    ) -> TaskResult: ...


def _enrich_prompt(prompt: str, context: str) -> str:
    """Prepend shared-memory context to a task prompt, if any.

    Shared by every strategy so the enrichment format stays identical across
    them (RouteStrategy, PipelineStrategy, …).
    """
    if context:
        return f"Context from previous work:\n{context}\n\n---\n\nTask: {prompt}"
    return prompt


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


def _collect_files_changed(result: TaskResult, worktree: Worktree) -> None:
    """Record the files a completed agent changed in its own worktree.

    Per-worktree (not whole-repo), so each result's ``files_changed`` reflects
    only that agent's edits — the point of per-agent isolation. No-op for a
    failed result or a non-git (tempdir) workspace. Strategies call this on the
    result they return; the Supervisor no longer diffs centrally because with
    per-agent worktrees only the strategy knows which one produced the result.
    """
    if worktree.is_git and result.status == TaskStatus.COMPLETED:
        result.files_changed = list(_git_changed_files(worktree.path))


def _gate_accepts(
    result: TaskResult, worktree: Worktree, gate: QualityGate | None
) -> bool:
    """Run the quality gate against a COMPLETED result's worktree and judge it.

    Returns ``True`` when the result is accepted — no gate configured, or the gate
    passed. On a gate failure it returns ``False`` and **downgrades the result in
    place** to FAILED with a gate error, so the rejection is reported through the
    existing failure plumbing (RouteStrategy's aggregate, the Compete aggregate,
    the pipeline failure) rather than silently accepted. The ``GateReport`` is
    attached to ``result.gate`` either way (pass or fail) — an annotation like
    ``files_changed``, so callers/CLI can surface the verdict.

    Shared by every strategy so "passed the gate" means exactly one thing across
    them. Call only on a COMPLETED result — a failed agent is never gated (there's
    nothing trustworthy to check).
    """
    if gate is None:
        return True
    report = gate.evaluate(worktree)
    result.gate = report
    if report.passed:
        return True
    result.status = TaskStatus.FAILED
    result.error = f"quality gate failed: {report.summary()}"
    return False


def _matched_order(
    agents: list[AgentRunner], task: Task, matcher: CapabilityMatcher | None
) -> list[AgentRunner]:
    """Re-order agents by capability fit for this task; identity when no matcher.

    Capability matching (#9) layered OVER priority: a matching agent moves ahead
    of raw ``agent_priority``, and equal-fit agents keep priority order (the
    matcher's sort is stable). Returns a permutation — it never drops an agent, so
    Route's fallback chain / Pipeline's stages / Compete's competitors / Council's
    members stay intact and the ``if not agents`` guard still covers the empty case.

    Applied ONCE in the Supervisor (not threaded into ``Strategy.execute`` like
    the gate/breaker) because re-ordering has no topology-specific meaning: every
    strategy consumes the candidate list as a priority sequence, so re-ordering at
    the source makes all four capability-aware with no strategy change.
    """
    if matcher is None:
        return list(agents)
    return matcher.order(agents, task)


def _breaker_runnable(
    agents: list[AgentRunner], breaker: CircuitBreaker | None
) -> list[AgentRunner]:
    """Agents whose circuit allows a run (CLOSED or HALF-OPEN); all of them when
    no breaker is configured.

    Skipping an OPEN agent is reactive back-off from its *recorded* past failures
    — never a liveness probe (no ``health_check``). Shared by every strategy so
    "is this agent currently allowed to run" means one thing across them.
    """
    if breaker is None:
        return list(agents)
    runnable: list[AgentRunner] = []
    for agent in agents:
        if breaker.allows(agent.agent_id):
            runnable.append(agent)
        else:
            logger.info(
                "circuit breaker open for %s — skipping this run", agent.agent_id
            )
    return runnable


def _breaker_record(
    breaker: CircuitBreaker | None, agent_id: str, status: TaskStatus
) -> None:
    """Record an agent's RAW execution outcome (no-op without a breaker).

    Called immediately after the agent runs and **before** the quality gate, so
    the breaker tracks *execution reliability* (did it run to completion?) — a
    gate rejection is a quality verdict on a working agent and must not trip the
    breaker. Shared by every strategy so recording stays identical across them.
    """
    if breaker is not None:
        breaker.record(agent_id, status)


def _breaker_all_open_failure(
    task: Task, agents: list[AgentRunner]
) -> TaskResult:
    """Failure returned when every candidate agent's circuit is OPEN.

    Distinct from ``_no_stages_failure``/"no agents available" so the user knows
    it's the breaker cooling down after repeated failures, not a config gap.

    Failing fast here is deliberate, not the breaker "aborting" work: every agent
    is within its cooldown after recent recorded failures, so honoring the
    back-off means there is nothing to run this invocation — the same shape as the
    quality gate failing the run when every output is rejected. (The *advisory*
    property — never blocking work — applies to a DEGRADED breaker, whose
    ``allows`` returns True for everyone; an OPEN circuit is the back-off working
    as designed.) The error tells the user how to recover: wait for a cooldown,
    or disable the breaker.
    """
    names = ", ".join(a.agent_id for a in agents)
    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=-1,
        duration_seconds=0.0,
        error=(
            f"All agents are circuit-open (cooling down after repeated failures): "
            f"{names}. Retry once a cooldown elapses, or disable the breaker."
        ),
    )


# ── RouteStrategy (v0.1 MVP) ────────────────────────────────────────────


class RouteStrategy:
    """Priority-based fallback. Try agents in order, fall back on failure."""

    def execute(
        self,
        task: Task,
        agents: list[AgentRunner],
        memory: SyncmillMemory,
        pool: WorktreePool,
        config: SyncmillConfig,
        gate: QualityGate | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> TaskResult:
        # Drop agents whose circuit is OPEN before doing any work; if the breaker
        # has tripped them all, say so clearly rather than searching memory or
        # acquiring worktrees for nobody.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        # Search memory for relevant context
        enriched_prompt = _enrich_prompt(
            task.prompt, run_async(memory.search_context(task.prompt))
        )

        errors: list[TaskResult] = []
        for agent in runnable:
            # A fresh, isolated worktree per attempt: a failed agent's partial
            # edits never leak into the next agent's run, so the winner's
            # files_changed reflects only its own work.
            worktree = pool.acquire(agent.agent_id)
            timeout = config.agent_config(agent.agent_id).timeout_seconds
            result = agent.execute(
                prompt=enriched_prompt,
                workdir=worktree.path,
                timeout=timeout,
            )
            # Record the RAW execution outcome before gating: a completed run
            # closes the agent's circuit even if its diff is later gate-rejected
            # (a quality verdict, not an execution failure).
            _breaker_record(breaker, agent.agent_id, result.status)
            # Accept only a completed result that also clears the quality gate;
            # a gate failure downgrades the result and falls through to the next
            # agent (recorded in errors), so an unverified diff is never accepted.
            if result.status == TaskStatus.COMPLETED and _gate_accepts(
                result, worktree, gate
            ):
                _collect_files_changed(result, worktree)
                return result
            errors.append(result)

        # All agents failed (or failed the gate) — return aggregate error
        return _aggregate_failure(task, errors)


def _aggregate_failure(
    task: Task, errors: list[TaskResult], *, duration_seconds: float | None = None
) -> TaskResult:
    """Combine all failure reasons into a single TaskResult.

    ``duration_seconds`` defaults to the *sum* of the attempts — correct for the
    sequential RouteStrategy, whose agents run one after another. A concurrent
    strategy (CompeteStrategy) overrides it with the *max*, since its agents ran
    at once and the wall-clock is the slowest one, not their sum.
    """
    lines = []
    for e in errors:
        reason = e.error or f"exit_code={e.exit_code}"
        lines.append(f"  [{e.agent_id}] {reason}")
    combined_error = "All agents failed:\n" + "\n".join(lines)

    if duration_seconds is None:
        duration_seconds = sum(e.duration_seconds for e in errors)

    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=-1,
        duration_seconds=duration_seconds,
        error=combined_error,
    )


# ── PipelineStrategy (v0.2) ─────────────────────────────────────────────


class PipelineStrategy:
    """Sequential chain. Agents run in priority order as pipeline stages, each
    building on the previous stage's work.

    Two handoff channels, both honoring agent isolation — agents stay pure
    ``execute(prompt, workdir, timeout)`` functions and never receive another
    stage's ``TaskResult``:

    * **Knowledge** — each completed stage writes its result to ``shared``
      (``share_result``); the next stage reads ``shared`` back
      (``search_context``) and prepends the most relevant matches to its prompt.
      This is best-effort and relevance-ranked, not a guaranteed point-to-point
      handoff — it surfaces the strongest shared context, which for a short
      pipeline is the recent stages — consistent with memory being advisory.
    * **Files** — every stage shares the one worktree acquired from the pool, so
      a stage edits the working tree the next stage inherits. Per-agent worktrees
      deliberately do *not* apply here: isolating stages would defeat the file
      handoff, so the pipeline acquires a single shared workspace (where
      RouteStrategy/Compete acquire one per agent).

    Fail-fast: a stage failure breaks the chain — later stages would have
    nothing valid to build on — so the pipeline stops and reports which stage
    broke.
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
    ) -> TaskResult:
        if not agents:  # Supervisor guards this; stay safe if run standalone
            return _no_stages_failure(task)

        # A circuit-open stage is skipped; the pipeline chains the remaining
        # stages (consistent with Route skipping OPEN agents). If the breaker has
        # tripped every stage, report that rather than acquiring a worktree.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        # One worktree shared by every stage: a pipeline's whole point is that
        # each stage builds on the working tree the previous stage left behind,
        # so — unlike RouteStrategy/Compete — it acquires a single workspace.
        worktree = pool.acquire(runnable[0].agent_id)

        results: list[TaskResult] = []
        last = len(runnable) - 1
        for stage, agent in enumerate(runnable):
            # Each stage reads shared memory first, so it sees the prior stages'
            # outputs (plus any context from earlier syncmill runs).
            prompt = _enrich_prompt(
                task.prompt, run_async(memory.search_context(task.prompt))
            )
            timeout = config.agent_config(agent.agent_id).timeout_seconds
            result = agent.execute(prompt=prompt, workdir=worktree.path, timeout=timeout)
            results.append(result)
            # Record each stage's RAW execution outcome (before the final gate).
            _breaker_record(breaker, agent.agent_id, result.status)

            if result.status != TaskStatus.COMPLETED:
                return _pipeline_failure(task, stage + 1, len(runnable), results)

            # Hand this stage's output to the next one via shared memory
            # (best-effort). The final stage is shared by the Supervisor, so
            # don't double-write it here.
            if stage != last:
                run_async(memory.share_result(task, result))

        # The chain's product is the last stage's result; report the whole
        # pipeline's wall-clock as its duration. The gate runs once, against the
        # FINAL cumulative worktree (intermediate stages may legitimately leave
        # the tree broken mid-chain) — a gate failure fails the pipeline.
        final = results[-1]
        final.duration_seconds = round(sum(r.duration_seconds for r in results), 2)
        if not _gate_accepts(final, worktree, gate):
            return final  # downgraded to FAILED with the gate error
        _collect_files_changed(final, worktree)
        return final


def _pipeline_failure(
    task: Task, stage: int, total_stages: int, results: list[TaskResult]
) -> TaskResult:
    """Build the failure result for a broken pipeline stage."""
    failed = results[-1]
    reason = failed.error or f"exit_code={failed.exit_code}"
    return TaskResult(
        task_id=task.id,
        agent_id=failed.agent_id,
        status=TaskStatus.FAILED,
        output=failed.output,
        exit_code=failed.exit_code,
        duration_seconds=round(sum(r.duration_seconds for r in results), 2),
        error=(
            f"Pipeline failed at stage {stage}/{total_stages} "
            f"({failed.agent_id}): {reason}"
        ),
    )


def _no_stages_failure(task: Task) -> TaskResult:
    """Defensive failure when a strategy is handed no agents to run."""
    return TaskResult(
        task_id=task.id,
        agent_id="none",
        status=TaskStatus.FAILED,
        output="",
        exit_code=-1,
        duration_seconds=0.0,
        error="No agents available",
    )


# ── CompeteStrategy (v0.2) ──────────────────────────────────────────────


class CompeteStrategy:
    """Concurrent head-to-head. Every enabled agent runs the SAME task at once,
    each in its own isolated worktree; the highest-priority agent that COMPLETED
    wins.

    syncmill's first *concurrent* strategy. It bridges async via ``run_async``
    (reusing the module event loop — never ``asyncio.run``) to fan agents out over
    ``aexecute``, one private worktree per agent so parallel edits never clobber
    (where Pipeline shares a single tree). Every agent's prompt is enriched
    identically from ``shared`` (``_enrich_prompt``): they compete from the same
    starting context, with no stage-to-stage handoff.

    Unlike RouteStrategy it does **not** stop at the first success — all agents
    run and each produces a real diff in its own worktree — but the winner is the
    same priority preference made *deterministic* by ignoring finish time: among
    the COMPLETED results, the first in priority (input ``agents``) order. The
    criterion never reads ``duration_seconds`` (nondeterministic wall-clock), so
    it's reproducible across runs and across git vs tempdir workspaces. Selection
    is funnelled through ``_select_winner`` — the seam where quality gates (#10)
    will later rank the COMPLETED candidates by a verified score. If no agent
    completes, it reports an aggregate failure (``_aggregate_failure``), the same
    shape Route uses.

    The Supervisor owns worktree lifetime — it shares the returned winner to
    ``shared`` and tears down EVERY worktree (winner + losers) in its ``finally``
    — so compete neither cleans up worktrees nor applies the winner's diff back to
    the repo; it only *surfaces* it (``files_changed`` + the Supervisor's share).
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
    ) -> TaskResult:
        if not agents:  # Supervisor guards this; stay safe if run standalone
            return _no_stages_failure(task)

        # Drop circuit-open agents BEFORE acquiring worktrees / fanning out, so
        # `runnable`, `worktrees`, and `results` stay the same length and
        # positionally aligned (the alignment _select_winner and recording rely
        # on). If the breaker tripped them all, say so.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        # Memory read happens FIRST, fully sync — never call run_async from inside
        # the gather below (that would re-enter the running loop). Every agent
        # competes from the same enriched starting context.
        enriched_prompt = _enrich_prompt(
            task.prompt, run_async(memory.search_context(task.prompt))
        )

        # One isolated worktree per agent, acquired UP FRONT and SERIALLY:
        # acquire() is sync git (contends on the index lock) and must not run
        # inside the concurrent fan-out. The comprehension over `runnable` keeps
        # the lists the same length, so results/worktrees/agents stay aligned.
        worktrees = [pool.acquire(agent.agent_id) for agent in runnable]

        # One concurrent batch on the shared loop; gather preserves submission
        # order, so results line up with `runnable` and `worktrees`.
        results = run_async(
            aexecute_isolated(
                runnable,
                enriched_prompt,
                [wt.path for wt in worktrees],
                lambda aid: config.agent_config(aid).timeout_seconds,
            )
        )

        # Record EVERY competitor's raw outcome (winner + losers), before gating,
        # so a repeatedly-crashing agent's failures accumulate across runs and a
        # working one's circuit closes — even when a higher-priority peer wins.
        for agent, result in zip(runnable, results):
            _breaker_record(breaker, agent.agent_id, result.status)

        winner = _select_winner(worktrees, results, gate)
        if winner is None:
            # Nobody completed, or completers all failed the gate (which downgrades
            # them to FAILED in `results`, so the aggregate names the gate failure).
            # Same all-failed shape as Route, but agents ran concurrently, so the
            # wall-clock is the slowest, not the sum.
            return _aggregate_failure(
                task,
                results,
                duration_seconds=max(
                    (r.duration_seconds for r in results), default=0.0
                ),
            )

        result, worktree = winner
        _collect_files_changed(result, worktree)  # the winner's OWN worktree diff
        return result


def _select_winner(
    worktrees: list[Worktree],
    results: list[TaskResult],
    gate: QualityGate | None = None,
) -> tuple[TaskResult, Worktree] | None:
    """First gate-PASSING COMPLETED result in priority order, paired with its
    worktree; ``None`` if no candidate qualifies.

    Deterministic: ``results`` (and the aligned ``worktrees``) arrive in the
    caller-fixed priority order — gather preserves submission order, and the
    strategy acquired worktrees in that same order — so selection iterates
    priority, never gather's completion order and never ``duration_seconds``.
    Identical inputs always pick the same winner. Failed/timed-out candidates are
    never eligible; losing is observed by running, not probing (no
    ``health_check``).

    This is the seam quality gates (#10) extend — the promised hook is now live.
    With a ``gate``, each COMPLETED candidate is verified against its own worktree
    (via ``_gate_accepts``): a candidate that fails is downgraded to FAILED with a
    gate error *in place* (so the caller's aggregate failure names it) and is not
    eligible; the winner is the first candidate that both completed and passed, in
    priority order. So the ranking is "verified score (binary pass) with priority
    index as the final deterministic tie-break" — all passers score 1.0, so
    priority breaks the tie. Without a gate (``None``) this is the original
    "first COMPLETED in priority order". Evaluation is lazy: it stops at the first
    passer, so candidates after the winner aren't gated (losers, discarded anyway).
    """
    for worktree, result in zip(worktrees, results):
        if result.status == TaskStatus.COMPLETED and _gate_accepts(
            result, worktree, gate
        ):
            return result, worktree
    return None


# ── CouncilStrategy (v0.3) ──────────────────────────────────────────────


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
        results = run_async(
            aexecute_isolated(
                runnable, propose_prompt, [wt.path for wt in worktrees], timeout_for
            )
        )
        elapsed += max((r.duration_seconds for r in results), default=0.0)

        # Record EVERY proposer's raw outcome — the council's single breaker
        # recording point (see the class docstring), before any sharing/gating.
        for agent, result in zip(runnable, results):
            _breaker_record(breaker, agent.agent_id, result.status)

        # The completers become the members; publish each proposal to `shared` so
        # the critique rounds can read it. members carry only (agent, worktree) —
        # no result state, because synthesis re-runs every member and _select_winner
        # is the single convergence point.
        members: list[tuple[AgentRunner, Worktree]] = []
        for agent, worktree, result in zip(runnable, worktrees, results):
            if result.status == TaskStatus.COMPLETED:
                members.append((agent, worktree))
                run_async(memory.share_result(task, result))

        if not members:
            return _aggregate_failure(
                task, results, duration_seconds=round(elapsed, 2)
            )

        member_agents = [agent for agent, _ in members]
        member_worktrees = [wt for _, wt in members]

        # ── Phase 2: CRITIQUE × rounds (concurrent over members) ───────────
        for _ in range(config.council.rounds):
            critique_prompt = _enrich_prompt(
                _council_critique(task.prompt),
                run_async(memory.search_context(task.prompt)),
            )
            critiques = run_async(
                aexecute_isolated(
                    member_agents,
                    critique_prompt,
                    [wt.path for wt in member_worktrees],
                    timeout_for,
                )
            )
            elapsed += max((c.duration_seconds for c in critiques), default=0.0)
            # No breaker recording here (already recorded at propose). Share each
            # completed critique back to `shared`; a failed critique is tolerated
            # (not shared, member kept) — synthesis re-runs everyone regardless.
            for critique in critiques:
                if critique.status == TaskStatus.COMPLETED:
                    run_async(memory.share_result(task, critique))

        # ── Phase 3: SYNTHESIZE (concurrent over members) + converge ───────
        synth_prompt = _enrich_prompt(
            _council_synthesize(task.prompt),
            run_async(memory.search_context(task.prompt)),
        )
        synth_results = run_async(
            aexecute_isolated(
                member_agents,
                synth_prompt,
                [wt.path for wt in member_worktrees],
                timeout_for,
            )
        )
        elapsed += max((s.duration_seconds for s in synth_results), default=0.0)
        # No breaker recording here either (already recorded at propose).

        # Converge via Compete's selection seam: first gate-passing COMPLETED
        # synthesis in priority order (a gate failer is downgraded in place so the
        # aggregate below names it). Nobody qualifying → aggregate failure.
        winner = _select_winner(member_worktrees, synth_results, gate)
        if winner is None:
            return _aggregate_failure(
                task, synth_results, duration_seconds=round(elapsed, 2)
            )

        result, worktree = winner
        result.duration_seconds = round(elapsed, 2)  # whole-council wall-clock
        _collect_files_changed(result, worktree)  # the winner's OWN worktree diff
        return result


# ── Supervisor ───────────────────────────────────────────────────────────

_STRATEGIES: dict[str, type[Strategy]] = {
    "route": RouteStrategy,
    "pipeline": PipelineStrategy,
    "compete": CompeteStrategy,
    "council": CouncilStrategy,
}


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


class Supervisor:
    """Central orchestrator. The only component that touches both agents and memory."""

    def __init__(self, config: SyncmillConfig):
        self._config = config
        self._registry = AgentRegistry(config)
        self._memory = SyncmillMemory(config.memory)
        # Built once (like memory) because it carries cross-run state loaded from
        # disk and a `degraded` flag the CLI surfaces; None when disabled, so the
        # default path never touches the filesystem. (The gate, being stateless,
        # is built per-run inside `run`.)
        self._breaker = build_breaker(config.breaker)
        # Capability matcher (#9) — None when no agent declares a capability (the
        # default), so ordering stays at raw priority. Stateless/config-derived,
        # so it's built once here; the known agent ids surface a config typo.
        self._matcher = build_matcher(config.capabilities, config.agent_priority)

        self._strategy: Strategy = _make_strategy(config.default_strategy)

    def run(
        self,
        task: Task,
        agent_id: str | None = None,
        exclude: set[str] | None = None,
    ) -> TaskResult:
        """Execute a task through the orchestration pipeline.

        Args:
            task: The task to execute.
            agent_id: If specified, force this agent only (no fallback).
            exclude: Agent ids to drop from the priority fallback — e.g. run one
                agent interactively yourself while syncmill orchestrates the
                rest. Forcing an excluded agent via ``agent_id`` is an error.
        """
        exclude = exclude or set()
        # Determine which agents to use
        if agent_id:
            if agent_id in exclude:
                return TaskResult(
                    task_id=task.id,
                    agent_id=agent_id,
                    status=TaskStatus.FAILED,
                    output="",
                    exit_code=-1,
                    duration_seconds=0.0,
                    error=f"Agent '{agent_id}' is both forced (agent_id) and excluded",
                )
            agent = self._registry.get(agent_id)
            if agent is None:
                return TaskResult(
                    task_id=task.id,
                    agent_id=agent_id,
                    status=TaskStatus.FAILED,
                    output="",
                    exit_code=-1,
                    duration_seconds=0.0,
                    error=f"Agent '{agent_id}' not found or not enabled",
                )
            agents = [agent]
        else:
            agents = [
                a for a in self._registry.available() if a.agent_id not in exclude
            ]
            # Capability matching (#9): re-order the candidate list by fit for
            # this task BEFORE the strategy runs, so every strategy sees a
            # capability-preferred priority sequence. No-op (identity) when
            # no matcher is configured or the task carries no hint. Not applied on
            # the forced-`agent_id` path above — forcing one agent has no ordering
            # to influence. Re-orders before the breaker's per-strategy OPEN-agent
            # filter (preference first, then availability), and never empties the
            # pool (it's a permutation), so the guard below still holds.
            agents = _matched_order(agents, task, self._matcher)

        if not agents:
            error = "No agents available"
            if exclude:
                error += f" (excluded: {', '.join(sorted(exclude))})"
            return TaskResult(
                task_id=task.id,
                agent_id="none",
                status=TaskStatus.FAILED,
                output="",
                exit_code=-1,
                duration_seconds=0.0,
                error=error,
            )

        # Register agents in memory (best-effort)
        for agent in agents:
            run_async(self._memory.register_agent(agent.agent_id))

        # The task's worktree pool — the strategy acquires from it (one worktree
        # per agent, or a single shared one for a pipeline); the Supervisor owns
        # its lifetime and tears every worktree down in the finally below.
        repo_root = Path(self._config.repo_root).resolve()
        pool = WorktreePool(repo_root, str(task.id))

        # The quality gate (#10), built per-run from config like the pool — None
        # when no commands are configured, so gating is off and behavior is
        # unchanged. The strategy decides what a verdict means for its topology.
        gate = build_gate(self._config.gate)

        # Start memory session (best-effort; None if memory is unavailable)
        session_id = run_async(
            self._memory.start_task_session(agents[0].agent_id, task)
        )

        result: TaskResult | None = None
        try:
            # Each acquired worktree starts clean at HEAD; the strategy diffs the
            # one that produced its result (via _collect_files_changed) so
            # files_changed reflects only that agent's work.
            result = self._strategy.execute(
                task=task,
                agents=agents,
                memory=self._memory,
                pool=pool,
                config=self._config,
                gate=gate,
                breaker=self._breaker,
            )

            # Share successful results to memory
            if result.status == TaskStatus.COMPLETED:
                run_async(self._memory.share_result(task, result))

            return result
        finally:
            if result is not None:
                run_async(self._memory.end_task_session(session_id, result))
            pool.cleanup_all()

    def close(self) -> None:
        run_async(self._memory.close())

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def memory(self) -> SyncmillMemory:
        return self._memory

    @property
    def breaker(self) -> CircuitBreaker | None:
        return self._breaker

    @property
    def matcher(self) -> CapabilityMatcher | None:
        return self._matcher


def _git_changed_files(workdir: Path) -> set[str]:
    """Get files changed in worktree (staged + unstaged + untracked)."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=str(workdir),
        )
        changed = set(proc.stdout.strip().splitlines()) if proc.returncode == 0 else set()
        # Also check untracked
        proc2 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, cwd=str(workdir),
        )
        if proc2.returncode == 0:
            changed.update(proc2.stdout.strip().splitlines())
        return changed
    except Exception:
        return set()
