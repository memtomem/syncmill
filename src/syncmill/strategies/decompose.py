"""DecomposeStrategy (v0.3) — split a task into subtasks across agents, synthesize."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import replace

from syncmill.agents import AgentRunner, aexecute_each, aexecute_isolated
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
from syncmill.worktree import WorktreePool

logger = logging.getLogger(__name__)


def _decompose_plan_prompt(task_prompt: str, max_subtasks: int) -> str:
    """Phase-1 instruction: the planner breaks the task into subtasks.

    Asks for a STRICT JSON array of strings (no prose, no fences) so the plan parses
    deterministically; a non-conforming/empty plan degrades to a single whole-task
    subtask. Wrapped by ``_enrich_prompt`` (shared context prepended as "Context
    from previous work", this labeled "Task"), so it's phrased to avoid a nested
    literal "Task:".
    """
    return (
        "You are the planner for a larger software task that will be split into "
        "independent subtasks, each implemented by a separate coding agent in its "
        "own isolated copy of the repository (the agents cannot see each other's "
        "work). Break the work into at most "
        f"{max_subtasks} self-contained subtasks that can be implemented in "
        "parallel, each a single concrete, standalone instruction. Output ONLY a "
        "JSON array of strings — no prose, no markdown fences, no wrapping object — "
        'for example ["add the parser module", "add tests for the parser"]. Do NOT '
        "modify any files; output only the plan.\n\n"
        f"The overall task to break down: {task_prompt}"
    )


def _decompose_subtask_prompt(
    task_prompt: str, subtask: str, index: int, total: int
) -> str:
    """Per-subtask instruction (one of ``total``, run concurrently and isolated).

    Wrapped by ``_enrich_prompt`` (shared context prepended, this labeled "Task"),
    so it reads as the assignment; phrased to avoid a nested literal "Task:". The
    agent sees only its own piece — other subtasks run in parallel in separate
    worktrees it can't read — so it must implement its slice completely.
    """
    return (
        f"You are implementing subtask {index} of {total} of a larger software "
        "task, working in your own isolated copy of the repository. The other "
        "subtasks are being implemented in parallel by other agents whose work you "
        "cannot see, so implement ONLY your piece, completely and self-containedly. "
        "Explain what you changed and why in your final message so the work can be "
        "integrated later.\n\n"
        f"For reference, the overall task is: {task_prompt}\n\n"
        f"YOUR subtask to implement now: {subtask}"
    )


def _decompose_synthesize_prompt(task_prompt: str) -> str:
    """Final instruction: integrate the subtask results into one solution.

    Every runnable agent runs this concurrently in a FRESH worktree and reads the
    subtask summaries from the prepended ``shared`` context; the winner is chosen by
    ``_select_winner``. Integration is via shared KNOWLEDGE, not a file merge — the
    subtask worktrees are gone — so each synthesizer must produce a complete,
    standalone implementation rather than a diff against a peer.
    """
    return (
        "Independent coding agents have each implemented one subtask of a larger "
        "software task in separate workspaces; the context above holds their "
        "summaries of what each did. Integrate that work into a single, coherent, "
        "complete implementation of the overall task in THIS worktree — reconciling "
        "any overlaps or gaps between the pieces. Produce a standalone solution, not "
        "a diff against another agent's work.\n\n"
        f"The overall task to complete: {task_prompt}"
    )


def _parse_subtasks(raw: str, max_subtasks: int) -> list[str]:
    """Parse the planner's output into an ordered list of subtask strings.

    Tolerant of a surrounding markdown fence (but not of arbitrary prose — see
    ``_extract_json_list``). STRICT on the array's contents: it accepts only a
    non-empty list whose every element is a non-empty string, then truncates to
    ``max_subtasks`` (logging what it drops — no silent cap). A mixed or partial
    array — objects, numbers, blanks alongside strings (e.g.
    ``[{"task": "x"}, "y"]`` or ``["x", 123]``) — is treated as malformed and
    returns ``[]`` rather than *salvaging the string subset*, which would silently
    run a wrong/partial plan (the same failure class ``_extract_json_list`` guards
    against for prose/objects). Returns ``[]`` when nothing usable parses; the
    caller degrades that to a single whole-task subtask.
    """
    items = _extract_json_list(raw)
    if not items or not all(isinstance(s, str) and s.strip() for s in items):
        return []  # empty / mixed / partial array → degrade, never salvage a subset
    cleaned = [s.strip() for s in items]
    if len(cleaned) > max_subtasks:
        logger.info(
            "decompose: capping %d planned subtasks to max_subtasks=%d (dropping %d)",
            len(cleaned),
            max_subtasks,
            len(cleaned) - max_subtasks,
        )
        cleaned = cleaned[:max_subtasks]
    return cleaned


def _extract_json_list(raw: str) -> list:
    """Parse ``raw`` as a JSON array — the whole message, after stripping a
    surrounding markdown fence. Returns ``[]`` if it isn't a JSON array.

    Deliberately STRICT: it does **not** greedily slice a ``[...]`` span out of
    prose. A greedy slice would happily lift an *example* array out of a refusal
    (``"I can't; the format would be [\\"a\\", \\"b\\"]"``) or the inner array out of
    a wrapping object — yielding a silently-wrong breakdown instead of degrading.
    The planner is instructed to output ONLY the array, so anything that isn't a
    bare array (optionally fenced) degrades to the single whole-task subtask — the
    safe default — rather than risking a wrong plan.
    """
    text = raw.strip()
    if text.startswith("```"):  # strip a ```json … ``` fence if present
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


class DecomposeStrategy:
    """Split a compound task into subtasks, run them ACROSS agents concurrently and
    in isolation, then synthesize the results into one outcome (#14, v0.3).

    syncmill's first task-SPLITTING strategy: where route/pipeline/compete/council
    run agents on the SAME task, decompose runs agents on DIFFERENT pieces. Three
    phases, bridging async via ``run_async`` over the concurrent fan-outs, with all
    memory I/O done strictly BETWEEN fan-outs (never ``run_async`` inside a gathered
    coroutine — the shared-loop rule):

    1. **Decompose** (sync, single planner) — the highest-priority runnable agent
       plans the breakdown in a scratch worktree it's told not to edit: it returns a
       JSON array of subtask strings, parsed by ``_parse_subtasks``. A failed,
       empty, or unparseable plan degrades to a single whole-task subtask, so a
       flaky planner never aborts the run (best-effort, like memory) — decompose
       then behaves like "do the whole task, synthesized across agents". The planner
       is a single sequential step, so it uses the sync ``execute`` path (like
       route/pipeline); only the genuinely concurrent phases fan out.
    2. **Subtasks** (concurrent, isolated, ACROSS agents) — the subtasks are
       distributed round-robin over the runnable agents (faithful to the issue's
       "split a task into subtasks *across agents*") and run at once, each in its own
       worktree via ``aexecute_each`` (the per-item-prompt counterpart to
       ``aexecute_isolated``'s broadcast). Each COMPLETED subtask's summary is
       published to ``shared``; a failed subtask is tolerated (just not shared). If
       NONE complete there is nothing to integrate → aggregate failure (like council
       when no proposal completes).
    3. **Synthesize** (concurrent over all runnable) + converge — every runnable
       agent reads the subtask summaries from ``shared`` (relevance-ranked,
       best-effort — the handoff pipeline/council use) and produces the integrated
       solution in a FRESH worktree; the council/compete ``_select_winner`` seam
       picks the winner (first gate-passing COMPLETED in priority order). Integration
       is via shared KNOWLEDGE, not a file merge — the subtask worktrees are torn
       down, exactly as compete/council discard the losers' trees.

    The returned winner's ``duration_seconds`` is the decompose wall-clock (planner
    + slowest subtask + slowest synthesizer); its ``files_changed`` is its own
    worktree's diff. The Supervisor shares the winner to ``shared`` and tears every
    worktree down, as for compete/council — decompose neither cleans worktrees nor
    applies a diff.

    **Breaker (#8) is recorded once per agent per run, at each agent's FIRST
    execution** (a dedup set). Unlike council — whose propose phase runs every agent
    once from identical input, the natural single recording point — decompose has no
    such uniform-and-easy phase: the planner is one agent on a planning prompt, the
    subtasks are a non-uniform round-robin, and synthesis (the only phase where
    every runnable agent runs) is the HARDEST prompt, where recording would
    over-penalize a working agent struggling on integration — the very reason
    council records at propose, not synthesize. So decompose records each agent's RAW
    outcome the first time it runs anything this invocation (planner, a subtask, or
    synthesis) and skips it thereafter — keeping the one-record-per-agent-per-run
    semantics identical to the other strategies while still probing every agent that
    runs, and recording the planner at its short/early (so typically easy) planning
    step rather than at the hard synthesis phase. As everywhere, the RAW pre-gate
    status is recorded, so a synthesis gate rejection (a quality verdict on a working
    agent) never trips the breaker.

    Tracing (S6): the ``plan`` node is the fan-out hub — recorded even when the
    plan FAILED/degraded (the degraded whole-task subtask still parents on it).
    Each ``subtask:{i}`` parents on ``plan`` (its NAME is ``subtask:{i}:{agent}``
    — the round-robin repeats agent ids, so the diff-stable label needs the
    index) with ``worktree_slot=i``. Each ``synthesize:{agent}`` also parents on
    ``plan`` and links the COMPLETED subtask nodes — only completed subtasks were
    published to ``shared``, i.e. declared available, never claimed consumed (the
    finding-6 approximation). Convergence reuses Compete's select-span semantics
    (gate spans for the lazily-gated prefix; ``select`` parented on the winning
    synthesize node, else ``run``, linking the other raw-COMPLETED candidates).
    The no-completed-subtasks early return leaves only plan + subtask spans
    (selection never ran, so there is no select span).
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

        # Drop circuit-open agents BEFORE any work; if the breaker tripped them all,
        # say so rather than planning/fanning out for nobody.
        runnable = _breaker_runnable(agents, breaker)
        if not runnable:
            return _breaker_all_open_failure(task, agents)

        def timeout_for(aid: str) -> int:
            return config.agent_config(aid).timeout_seconds

        # Record each agent's RAW execution outcome exactly ONCE per run, at its
        # first execution (see the class docstring) — preserving the
        # one-record-per-agent-per-run semantics the cross-run breaker threshold
        # relies on, while still probing every agent that runs.
        recorded: set[str] = set()

        def record_once(agent_id: str, status: TaskStatus) -> None:
            if agent_id not in recorded:
                recorded.add(agent_id)
                _breaker_record(breaker, agent_id, status)

        elapsed = 0.0  # decompose wall-clock: planner + slowest subtask + slowest synth

        # ── Phase 1: DECOMPOSE (sync, single planner) ─────────────────────
        # The planner uses the SYNC execute path, which the streaming seam (an
        # aexecute-only ContextVar) doesn't reach — so this phase is status-only in
        # the monitor (the `status.log` line is the signal; no live pane).
        _monitor_phase(monitor, "plan")
        planner = runnable[0]
        plan_wt = pool.acquire(planner.agent_id)
        plan_prompt = _enrich_prompt(
            _decompose_plan_prompt(task.prompt, config.decompose.max_subtasks),
            run_async(memory.search_context(task.prompt)),
        )
        plan = planner.execute(
            prompt=plan_prompt,
            workdir=plan_wt.path,
            timeout=timeout_for(planner.agent_id),
        )
        plan_end = time.time()
        elapsed += plan.duration_seconds
        record_once(planner.agent_id, plan.status)
        _monitor_status(monitor, planner.agent_id, plan.status)
        # The plan node is the fan-out hub — recorded even when FAILED/degraded,
        # since the degraded whole-task subtask still parents on it.
        _trace_attempt(tracer, _attempt_span(
            planner.agent_id,
            node_id="plan",
            parent="run",
            attempt=0,
            phase="plan",
            raw_status=plan.status,
            error=plan.error,
            exit_code=plan.exit_code,
            end=plan_end,
            duration=plan.duration_seconds,
        ), plan)

        subtasks = (
            _parse_subtasks(plan.output, config.decompose.max_subtasks)
            if plan.status == TaskStatus.COMPLETED
            else []
        )
        if subtasks:
            # Publish the work plan itself to `shared` so unified search (and
            # tag_filter="decompose-plan") can surface how the task was split.
            # Sits between the sync planner and the fan-out (shared-loop rule).
            # Share the PARSED plan (what actually runs), not the raw planner
            # response — _parse_subtasks may have normalized or capped it, and
            # advertising dropped subtasks would mislead later enrichment.
            parsed_plan = replace(
                plan, output=json.dumps(subtasks, ensure_ascii=False)
            )
            run_async(memory.share_result(task, parsed_plan, kind="decompose-plan"))
        else:
            logger.info(
                "decompose: no usable subtask plan — running the whole task as one "
                "subtask"
            )
            # The degraded whole-task fallback is not a real plan — never shared.
            subtasks = [task.prompt]

        # ── Phase 2: SUBTASKS (concurrent, isolated, round-robin across agents) ─
        n = len(subtasks)
        assigned = [runnable[i % len(runnable)] for i in range(n)]
        sub_worktrees = [pool.acquire(agent.agent_id) for agent in assigned]
        # One shared-memory read, broadcast as context to every subtask (the
        # subtasks are independent and run before any of them shares back, so a
        # per-subtask search would only surface prior-run context anyway). Done
        # fully sync, before the fan-out.
        sub_context = run_async(memory.search_context(task.prompt))
        sub_prompts = [
            _enrich_prompt(
                _decompose_subtask_prompt(task.prompt, subtasks[i], i + 1, n),
                sub_context,
            )
            for i in range(n)
        ]
        _monitor_phase(monitor, "subtasks")
        # The round-robin runs one agent several times AT ONCE, so the monitor is
        # keyed per SUBTASK rather than per agent — keyed by agent id, subtasks 0
        # and 2 of the same agent would interleave byte-wise in one log and share
        # one pane. Every other strategy runs each agent once per fan-out, so its
        # labels stay the bare agent ids.
        sub_labels = [f"{a.agent_id}-sub{i + 1}" for i, a in enumerate(assigned)]
        with _monitor_streaming(monitor, sub_labels):
            sub_results = run_async(
                aexecute_each(
                    assigned,
                    sub_prompts,
                    [wt.path for wt in sub_worktrees],
                    timeout_for,
                    labels=sub_labels,
                )
            )
        subtask_end = time.time()
        elapsed += max((r.duration_seconds for r in sub_results), default=0.0)
        for i, (agent, result) in enumerate(zip(assigned, sub_results)):
            record_once(agent.agent_id, result.status)
            _monitor_status(monitor, sub_labels[i], result.status)
            _trace_attempt(tracer, _attempt_span(
                agent.agent_id,
                node_id=f"subtask:{i}",
                # the round-robin repeats agent ids, so the diff-stable label
                # needs the index (the one name= override across strategies)
                name=f"subtask:{i}:{agent.agent_id}",
                parent="plan",
                attempt=i,
                phase="subtask",
                raw_status=result.status,
                error=result.error,
                exit_code=result.exit_code,
                end=subtask_end,
                duration=result.duration_seconds,
                worktree_slot=i,
            ), result)

        completed = [r for r in sub_results if r.status == TaskStatus.COMPLETED]
        # Only completed subtasks are published to `shared` — the synthesize
        # spans link exactly these (declared availability).
        completed_nodes = tuple(
            f"subtask:{i}"
            for i, r in enumerate(sub_results)
            if r.status == TaskStatus.COMPLETED
        )
        if not completed:
            # Nothing decomposed completed → nothing to integrate. Concurrent, so the
            # wall-clock is the slowest, not the sum (like Compete/Council).
            return _aggregate_failure(
                task, sub_results, duration_seconds=round(elapsed, 2)
            )
        for result in completed:  # publish each completed subtask for synthesis
            run_async(memory.share_result(task, result, kind="decompose-subtask"))

        # ── Phase 3: SYNTHESIZE (concurrent over all runnable) + converge ──
        synth_worktrees = [pool.acquire(agent.agent_id) for agent in runnable]
        synth_prompt = _enrich_prompt(
            _decompose_synthesize_prompt(task.prompt),
            run_async(memory.search_context(task.prompt)),
        )
        _monitor_phase(monitor, "synthesize")
        with _monitor_streaming(monitor, [a.agent_id for a in runnable]):
            synth_results = run_async(
                aexecute_isolated(
                    runnable,
                    synth_prompt,
                    [wt.path for wt in synth_worktrees],
                    timeout_for,
                )
            )
        synth_end = time.time()
        elapsed += max((r.duration_seconds for r in synth_results), default=0.0)
        # RAW pre-select snapshot for the spans: `_select_winner` downgrades
        # gate-failing completers in place, and the winner's duration is mutated
        # to the whole-decompose wall-clock below — the span must carry the
        # synthesizer's own raw outcome/duration (3-tuple, like council).
        raw = [(r.status, r.error, r.duration_seconds) for r in synth_results]
        for agent, result in zip(runnable, synth_results):
            record_once(agent.agent_id, result.status)
            _monitor_status(monitor, agent.agent_id, result.status)

        # Converge via Compete's selection seam: first gate-passing COMPLETED synth
        # in priority order (a gate failer is downgraded in place so the aggregate
        # below names it). Nobody qualifying → aggregate failure.
        sel_start = time.time()
        trace_out: dict = {}
        winner = _select_winner(
            synth_worktrees,
            synth_results,
            gate,
            mode=config.selection.mode,
            agents=runnable,
            pool=pool,
            config=config,
            tracer=tracer,
            candidate_node_prefix="synthesize",
            trace_out=trace_out,
        )
        winner_result: TaskResult | None = None
        if winner is not None:
            winner_result, winner_worktree = winner
            winner_result.duration_seconds = round(elapsed, 2)  # wall-clock
            _collect_files_changed(winner_result, winner_worktree)  # its OWN diff
        sel_end = time.time()

        if tracer is not None:
            for slot, ((agent, synth), (raw_status, raw_error, raw_duration)) in (
                enumerate(zip(zip(runnable, synth_results), raw))
            ):
                is_winner = synth is winner_result
                node_id = f"synthesize:{agent.agent_id}"
                _trace_attempt(tracer, _attempt_span(
                    agent.agent_id,
                    node_id=node_id,
                    parent="plan",  # fan-in over the plan's decomposition
                    attempt=0,
                    phase="synthesize",
                    links=completed_nodes,  # the published subtask results
                    raw_status=raw_status,
                    error=raw_error,
                    exit_code=synth.exit_code,
                    end=synth_end,
                    duration=raw_duration,
                    winner=is_winner,
                    files_changed_count=(
                        len(synth.files_changed) if is_winner else None
                    ),
                    worktree_slot=slot,  # fresh worktrees for this fan-out
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
                    runnable, synth_results, raw
                )
                if raw_status == TaskStatus.COMPLETED and synth is not winner_result
            ]
            if trace_out.get("judge_span") is not None:
                # judge mode ran: emit its deferred span now that the candidate
                # spans it links are recorded (a streaming sink drops forward
                # links), and link the verdict the selection depended on.
                _trace_attempt(tracer, *trace_out["judge_span"])
                links.append(trace_out["judge_node"])
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
