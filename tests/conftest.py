"""Shared hermetic test fixtures.

These tests never touch the user's real ~/.memtomem DB, the network, or the
real claude/codex/kimi binaries. Memory and agents are replaced with
in-memory duck-typed fakes that honor the same contracts as the real classes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from pathlib import Path

import pytest

import syncmill.agents as _agents
from syncmill.board import WorkItem
from syncmill.memory import BoardRunPurgeResult, SyncmillMemory
from syncmill.memory_backend import BackendCapabilities, WriteBlockedError
from syncmill.models import (
    GateCheck,
    GateReport,
    Task,
    TaskResult,
    TaskStatus,
    ToolTelemetry,
)

# ── Fake agents / memory / registry ──────────────────────────────────────


class FakeAgent:
    """In-memory AgentRunner: records calls; returns a scripted TaskResult."""

    def __init__(
        self,
        agent_id: str,
        *,
        status: TaskStatus = TaskStatus.COMPLETED,
        output: str = "ok",
        error: str | None = None,
        exit_code: int = 0,
        duration: float = 0.01,
        raises: BaseException | None = None,
        adelay: float = 0.0,
        tool_telemetry: ToolTelemetry | None = None,
    ):
        self._agent_id = agent_id
        self._status = status
        self._output = output
        self._error = error
        self._exit_code = exit_code
        self._duration = duration
        self._raises = raises
        # adelay: seconds aexecute() awaits before returning — lets concurrency
        # tests prove overlap (a slow agent shouldn't serialize the others).
        self._adelay = adelay
        self._tool_telemetry = tool_telemetry
        self.calls: list[dict] = []

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        self.calls.append({"prompt": prompt, "workdir": workdir, "timeout": timeout})
        if self._raises is not None:
            raise self._raises
        import uuid

        return TaskResult(
            task_id=uuid.uuid4(),
            agent_id=self._agent_id,
            status=self._status,
            output=self._output,
            exit_code=self._exit_code,
            duration_seconds=self._duration,
            error=self._error,
            tool_telemetry=self._tool_telemetry,
        )

    async def aexecute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        """Async twin of execute(): same scripted result, optional await delay.

        Delegates to execute() so call recording and the scripted TaskResult /
        ``raises`` behavior stay identical across both paths.
        """
        if self._adelay:
            await asyncio.sleep(self._adelay)
        return self.execute(prompt, workdir, timeout)


class FakeMemory:
    """Duck-typed stand-in for SyncmillMemory (async methods, via run_async).

    ``fail=True`` makes every operation raise — used to assert that a dead
    memtomem does NOT abort the task.
    """

    def __init__(self, *, context: str = "", fail: bool = False, echo_shared: bool = False,
                 fail_on: set[str] | None = None):
        self._context = context
        self._fail = fail
        # fail_on scopes the failure to specific methods (by their _maybe_fail
        # label) — used to assert e.g. a raising end_task_session still lets
        # cleanup run, without failing the rest of the run.
        self._fail_on = fail_on or set()
        # echo_shared models a live shared namespace: search_context replays
        # whatever was share_result()'d so far. Lets pipeline tests prove the
        # stage→stage handoff without the real memtomem store.
        self._echo_shared = echo_shared
        self.degraded = False
        self.last_error: str | None = None
        self.shared: list[tuple] = []
        # Parallel to `shared`: the artifact-kind tag of each share, so strategy
        # tests can assert e.g. the decompose plan rode kind="decompose-plan".
        self.share_kinds: list[str] = []
        self.events: list[str] = []
        # Board (Phase 1) plumbing: `scoped(run_id)` returns a CHILD FakeMemory whose
        # share_result writes to the CHILD's own `.shared` (the per-run scope), NEVER
        # the parent's — so a run_item test can assert nothing board-enriched reaches
        # global `shared` here, only the sanitized `share_board_result` summary in
        # `board_results`.
        self._scope: str | None = None
        self.board_results: list[tuple] = []
        self.context_windows: list[int | None] = []
        self.tag_filters: list[str | None] = []
        self.scoped_children: list["FakeMemory"] = []
        # Board-run namespace GC: Board owns candidates in P2; memory records
        # deletion batches and can fail selected per-run operations.
        self.purged: list[list[str]] = []
        self.delete_fail_ids: set[str] = set()
        self.delete_unavailable = False

    def _maybe_fail(self, where: str) -> None:
        if self._fail or where in self._fail_on:
            raise RuntimeError(f"memtomem unavailable ({where})")

    async def register_agent(self, agent_id: str) -> None:
        self.events.append(f"register:{agent_id}")
        self._maybe_fail("register_agent")

    async def start_task_session(self, agent_id: str, task: Task) -> str | None:
        self._maybe_fail("start_task_session")
        self.events.append(f"start:{agent_id}")
        return "session-123"

    async def end_task_session(self, session_id: str | None, result: TaskResult) -> None:
        self.events.append(f"end:{session_id}")
        self._maybe_fail("end_task_session")

    async def share_result(
        self, task: Task, result: TaskResult, *, kind: str = "task-result"
    ) -> None:
        self._maybe_fail("share_result")
        self.shared.append((task, result))
        self.share_kinds.append(kind)

    async def search_context(
        self,
        query: str,
        agent_id: str | None = None,
        *,
        top_k: int | None = None,
        tag_filter: str | None = None,
        max_bytes: int | None = None,
    ) -> str:
        self._maybe_fail("search_context")
        # Record the requested window: a strategy that publishes N artifacts and
        # reads them back must ask for at least N (the real default is smaller
        # than decompose.max_subtasks).
        self.context_windows.append(top_k)
        self.tag_filters.append(tag_filter)
        if self._echo_shared and self.shared:
            return "\n---\n".join(f"[{r.agent_id}] {r.output}" for _, r in self.shared)
        return self._context

    def scoped(self, run_id: str) -> "FakeMemory":
        """A board-scoped child (mirrors SyncmillMemory.scoped): its writes land in
        the child's OWN `.shared`, isolated from the parent's global `.shared`."""
        child = FakeMemory(
            context=self._context, fail=self._fail, echo_shared=self._echo_shared
        )
        child._scope = f"board-run:{run_id}"
        self.scoped_children.append(child)
        return child

    async def share_board_result(
        self, item_id: str, task_text: str, stripped_output: str
    ) -> bool:
        self._maybe_fail("share_board_result")
        self.board_results.append((item_id, task_text, stripped_output))
        return True

    async def purge_board_runs(self, run_ids):
        batch = list(run_ids)
        successful = await self.delete_board_runs(batch)
        if successful is None:
            return None
        return BoardRunPurgeResult(
            frozenset(successful), frozenset(set(batch) - set(successful)), frozenset()
        )

    async def delete_board_runs(self, run_ids) -> set[str] | None:
        self._maybe_fail("delete_board_runs")
        batch = list(run_ids)
        self.purged.append(batch)
        if self.delete_unavailable:
            return None
        return {rid for rid in batch if rid not in self.delete_fail_ids}

    async def close(self) -> None:
        self.events.append("close")


class FakeRegistry:
    """Duck-typed AgentRegistry."""

    def __init__(self, agents: list):
        self._agents = list(agents)

    def available(self) -> list:
        return list(self._agents)

    def get(self, agent_id: str):
        for a in self._agents:
            if a.agent_id == agent_id:
                return a
        return None


class FakeBackend:
    """In-memory ``MemoryBackend``. Records calls and scripts failures/results.

    ``search_results`` maps namespace -> list[dict]. ``get_results`` maps
    chunk_id -> chunk dict (for ``get`` / the gateway's ``get_shared``).
    ``ns_counts`` maps namespace -> chunk count (for ``shared_stats``).
    ``raise_on`` contains public backend method names. ``ensure_ready`` is a
    broad namespace-lifecycle outage switch, matching the old fake's private
    ``_ensure_init`` failure semantics.
    """

    def __init__(
        self,
        *,
        search_results=None,
        existing_ns=None,
        raise_on=None,
        get_results=None,
        ns_counts=None,
        delete_raise_ns=None,
        add_ids_empty=False,
        add_chunk_count=1,
        block_writes=False,
    ):
        self._search_results = search_results or {}
        self._raise_on = set(raise_on or ())
        self._get_results = get_results or {}
        self.meta: set[str] = set(existing_ns or ())
        self.counts: dict[str, int] = dict(ns_counts or {})
        self.set_calls: list[str] = []
        self.deleted: list[str] = []
        self._delete_raise_ns = set(delete_raise_ns or ())
        self.added: list[dict] = []
        self.events: list[tuple] = []
        self.sessions: list[dict] = []
        self.ended: list[str] = []
        self.searched: list[dict] = []
        self.registered: list[dict] = []
        self.share_calls: list[dict] = []
        self._add_ids_empty = add_ids_empty
        # how many chunk ids one add_returning_ids call mints (models a long
        # content splitting into several chunks)
        self._add_chunk_count = add_chunk_count
        # models the redaction guard blocking the content (WriteBlockedError)
        self._block_writes = block_writes
        self._chunk_n = 0
        self._chunks: dict[str, dict] = {}
        self.closed = False
        self._caps = BackendCapabilities()

    @property
    def capabilities(self):
        return self._caps

    @property
    def agent_namespace_prefix(self):
        return "agent-runtime:"

    @property
    def shared_namespace(self):
        return "shared"

    def validate_namespace(self, ns: str):
        return bool(self._caps.namespace_pattern.fullmatch(ns))

    def _guard(self, name: str):
        if name in self._raise_on:
            raise RuntimeError(f"boom in {name}")

    async def ensure_ready(self):
        self._guard("ensure_ready")

    async def ensure_namespace(self, ns: str, *, description: str = ""):
        await self.ensure_ready()
        self._guard("ensure_namespace")
        if ns not in self.meta:
            self.meta.add(ns)
            self.set_calls.append(ns)

    async def count_namespace(self, ns: str):
        await self.ensure_ready()
        self._guard("count_namespace")
        return self.counts.get(ns, 0)

    async def delete_namespace(self, ns: str):
        await self.ensure_ready()
        self._guard("delete_namespace")
        if ns in self._delete_raise_ns:
            raise RuntimeError(f"boom deleting {ns}")
        self.deleted.append(ns)
        count = self.counts.pop(ns, 0)
        # The metadata row goes unconditionally, chunks or not (memtomem#1705,
        # fixed upstream in 0.3.9 — below syncmill's floor, so every admitted
        # version behaves this way). The return value stays the CHUNK count, so
        # a metadata-only namespace still reports 0.
        self.meta.discard(ns)
        return count

    async def status(self):
        await self.ensure_ready()
        self._guard("status")
        return {"db_path": "/tmp/fake.db"}

    async def start_session(self, agent_id: str, namespace: str):
        self._guard("start_session")
        self.sessions.append({"agent_id": agent_id, "namespace": namespace})
        return "sid-1"

    async def log_event(self, event_type: str, content: str):
        self._guard("log_event")
        self.events.append((event_type, content))

    async def end_session(self, summary: str | None = None):
        self._guard("end_session")
        self.ended.append(summary)
        return {"summary": summary}

    async def add(self, content: str, *, tags: list[str], namespace: str):
        self._guard("add")
        self.added.append({"content": content, "tags": tags, "namespace": namespace})

    async def search(
        self, query: str, *, namespace: str, top_k: int, tag_filter: str | None = None
    ):
        self._guard("search")
        # Record the call so tests can assert the namespace scope and the clamped
        # top_k that actually reached the store.
        call = {"query": query, "namespace": namespace, "top_k": top_k}
        if tag_filter is not None:
            call["tag_filter"] = tag_filter
        self.searched.append(call)
        return [
            row for row in self._search_results.get(namespace, [])
            if self._matches_tags(row, tag_filter)
        ]

    @staticmethod
    def _matches_tags(row, tag_filter):
        wanted = {tag.strip() for tag in (tag_filter or "").split(",") if tag.strip()}
        return not wanted or bool(wanted.intersection(row.get("tags") or []))

    async def search_multi(
        self,
        query: str,
        *,
        namespaces,
        top_k: int,
        tag_filter: str | None = None,
    ):
        # "search" in raise_on means the whole search surface is dead — the
        # dead-memory tests script it once and expect both entry points to fail.
        self._guard("search")
        self._guard("search_multi")
        call = {"query": query, "namespaces": list(namespaces), "top_k": top_k}
        if tag_filter is not None:
            call["tag_filter"] = tag_filter
        self.searched.append(call)
        merged: dict[str, dict] = {}
        for ns in namespaces:
            for row in self._search_results.get(ns, []):
                if not self._matches_tags(row, tag_filter):
                    continue
                prior = merged.get(row["id"])
                if prior is None or row.get("score", 0.0) > prior.get("score", 0.0):
                    merged[row["id"]] = row
        return sorted(merged.values(), key=lambda r: -r.get("score", 0.0))[:top_k]

    async def add_returning_ids(self, content: str, *, tags: list[str], namespace: str):
        # "add" kills the whole write surface (see search_multi's guard note).
        self._guard("add")
        self._guard("add_returning_ids")
        if self._block_writes:
            raise WriteBlockedError("fake redaction guard blocked the write")
        record = {"content": content, "tags": tags, "namespace": namespace}
        self.added.append(record)
        if self._add_ids_empty:
            return []
        ids = []
        for _ in range(self._add_chunk_count):
            self._chunk_n += 1
            chunk_id = f"fake-chunk-{self._chunk_n}"
            self._chunks[chunk_id] = record
            ids.append(chunk_id)
        return ids

    async def share_chunk(self, chunk_id: str, *, target_namespace: str):
        self._guard("share_chunk")
        self.share_calls.append(
            {"chunk_id": chunk_id, "target_namespace": target_namespace}
        )
        source = self._chunks.get(chunk_id)
        if source is None:
            return None
        # Simulate the copy with mem_agent_share's collapsed shared-from tag.
        # A share copies ONE already-sized chunk, so it mints one id.
        prev = self._add_chunk_count
        self._add_chunk_count = 1
        try:
            ids = await self.add_returning_ids(
                source["content"],
                tags=[t for t in source["tags"] if not t.startswith("shared-from=")]
                + [f"shared-from={chunk_id}"],
                namespace=target_namespace,
            )
        finally:
            self._add_chunk_count = prev
        return ids[0] if ids else None

    async def register_namespace(
        self, ns: str, *, description: str | None = None, color: str | None = None
    ):
        self._guard("register_namespace")
        self.registered.append({"ns": ns, "description": description, "color": color})
        self.meta.add(ns)

    async def get(self, chunk_id: str):
        self._guard("get")
        return self._get_results.get(chunk_id)

    async def close(self):
        self._guard("close")
        self.closed = True


class FakeWorktree:
    def __init__(self, path: Path, *, is_git: bool = True, branch="syncmill/x/y"):
        self.path = path
        self.is_git = is_git
        self.branch = branch
        self.agent_id = "claude"
        self.is_temp = not is_git


class FakeWorktreePool:
    """Duck-typed WorktreePool: hands out distinct FakeWorktrees per acquire and
    records the acquisition order so tests can assert a strategy's topology
    (one-per-agent vs. single-shared) without touching real git.

    ``is_git`` flows into the handed-out worktrees so ``_collect_files_changed``
    either diffs (paired with a monkeypatched ``capture_changes``) or no-ops.
    """

    def __init__(self, *, is_git: bool = False):
        self._is_git = is_git
        self.acquired: list[str] = []  # agent_ids, in acquire order
        self.handed: list[FakeWorktree] = []
        self.cleaned = False
        self._n = 0

    def acquire(self, agent_id: str) -> FakeWorktree:
        self._n += 1
        wt = FakeWorktree(
            Path(f"/tmp/syncmill-wt/{agent_id}-{self._n}"),
            is_git=self._is_git,
            branch=f"syncmill/{agent_id}/{self._n}",
        )
        wt.agent_id = agent_id
        self.acquired.append(agent_id)
        self.handed.append(wt)
        return wt

    @property
    def worktrees(self) -> list[FakeWorktree]:
        return list(self.handed)

    def cleanup_all(self) -> int:
        # Simplification: always reports full success. The real pool can return
        # a partial count when a removal fails — that path is covered directly by
        # test_pool_cleanup_all_independent_on_failure against the real pool.
        self.cleaned = True
        return len(self.handed)


class FakeBreaker:
    """Duck-typed CircuitBreaker for strategy tests: scripted ``allows()`` and
    recorded ``record()`` calls, NO file I/O.

    ``open_agents`` is the set whose circuit is OPEN (``allows()`` -> False, so the
    strategy skips them). ``record`` appends ``(agent_id, status)`` in call order
    so a test can assert exactly which agents the strategy recorded and with what
    *raw* outcome — and that skipped (open) agents are NOT recorded. ``degraded``
    mirrors the real breaker so CLI/Supervisor surfacing can be exercised.
    """

    def __init__(self, *, open_agents: set[str] | None = None, degraded: bool = False):
        self._open = set(open_agents or ())
        self.recorded: list[tuple[str, TaskStatus]] = []
        self.degraded = degraded
        self.last_error: str | None = "boom" if degraded else None

    def allows(self, agent_id: str) -> bool:
        return agent_id not in self._open

    def record(self, agent_id: str, status: TaskStatus) -> None:
        self.recorded.append((agent_id, status))


class FakeMatcher:
    """Duck-typed CapabilityMatcher for Supervisor tests: a scripted re-ordering,
    no config.

    ``order`` returns the input agents sorted so that any whose id is in
    ``promote`` comes first (stable: ``promote`` order, then the rest in input
    order), letting a test assert the Supervisor re-orders BEFORE dispatching to
    the strategy without standing up a real DeclarativeMatcher. Records each
    ``order`` call's input ids so a test can assert the matcher saw the
    post-exclude candidate list. Never drops an agent (a permutation), like the
    real matcher.
    """

    def __init__(self, *, promote: list[str] | None = None):
        self._promote = list(promote or [])
        self.calls: list[list[str]] = []

    def order(self, agents, task):
        self.calls.append([a.agent_id for a in agents])
        rank = {aid: i for i, aid in enumerate(self._promote)}
        # stable: promoted ids first (in `promote` order), everyone else after in
        # input order — len(rank)+1 buckets the non-promoted together, ties broken
        # by Python's stable sort = input order.
        return sorted(agents, key=lambda a: rank.get(a.agent_id, len(rank)))


class FakeGate:
    """Duck-typed QualityGate: scripted verdict, no subprocess.

    The 'fake gate' the #10 acceptance criteria call for. ``passes`` is the
    default verdict; ``by_agent`` overrides it per agent id (keyed off
    ``worktree.agent_id``) so a test can make one competitor pass and another
    fail. Records the agent ids it evaluated, in order, so tests can assert which
    worktrees were gated (and that gating is lazy).
    """

    def __init__(self, *, passes: bool = True, by_agent: dict[str, bool] | None = None):
        self._passes = passes
        self._by_agent = by_agent or {}
        self.evaluated: list[str] = []

    def evaluate(self, worktree) -> GateReport:
        self.evaluated.append(worktree.agent_id)
        ok = self._by_agent.get(worktree.agent_id, self._passes)
        return GateReport(
            passed=ok,
            score=1.0 if ok else 0.0,
            checks=[GateCheck("fake-check", passed=ok, exit_code=0 if ok else 1)],
        )


class FakeBoard:
    """Duck-typed Board for ``Supervisor.run_item`` tests: scripts the claimed item
    and records every lifecycle call (recover / claim / next_runnable / apply_update
    / transition / close) so a test can assert the run_item flow without a real
    SQLite store. ``apply_raises`` injects a mid-run crash to exercise the
    crash → blocked path."""

    def __init__(
        self,
        *,
        item: WorkItem | None = None,
        next_item: WorkItem | None = None,
        apply_raises: BaseException | None = None,
        transition_raises: BaseException | None = None,
        apply_applied: bool = True,
        scratch_candidates: list[str] | None = None,
        scratch_raises: Exception | None = None,
        mark_raises: Exception | None = None,
        reactivate_raises: Exception | None = None,
    ):
        # `item` is returned by claim(id) (one-shot); `next_item` by next_runnable()
        # (one-shot, then None so a --loop drains).
        self._claim_item = item
        self._next_item = next_item
        self._apply_raises = apply_raises
        # apply_update returns (item, applied); `apply_applied=False` models a
        # lost-lease drop so a test can assert run_item suppresses the global share.
        self._apply_applied = apply_applied
        # transition_raises lets a crash test make the crash-block transition itself
        # raise, to assert _block_on_crash never masks the ORIGINAL exception.
        self._transition_raises = transition_raises
        self._scratch_candidates = list(scratch_candidates or [])
        self._scratch_raises = scratch_raises
        self._mark_raises = mark_raises
        self._reactivate_raises = reactivate_raises
        self.scratch_reads = 0
        self.last_scratch_limit: int | None = None
        self.marked_purged: list[set[str]] = []
        self.reactivated: list[str] = []
        self.recovered = 0
        self.claimed: list[tuple[str, str]] = []      # (item_id, run_id)
        self.dispatched: list[tuple] = []             # (run_id, goal) per next_runnable
        self.applied: list[tuple] = []                # (item_id, parsed, result, actor, run_id)
        self.transitioned: list[tuple] = []           # (item_id, to_state, note, run_id)
        self.blocked_reasons: list[str | None] = []   # one per transition() call
        self.closed = False

    def recover_stale_running(self, lease_timeout=None) -> int:
        self.recovered += 1
        return 0

    def collectible_board_run_ids(self, *, limit=100) -> list[str]:
        self.scratch_reads += 1
        self.last_scratch_limit = limit
        if self._scratch_raises is not None:
            raise self._scratch_raises
        return list(self._scratch_candidates[:limit])

    def mark_board_run_namespaces_purged(self, run_ids) -> int:
        if self._mark_raises is not None:
            raise self._mark_raises
        ids = set(run_ids)
        self.marked_purged.append(ids)
        return len(ids)

    def reactivate_board_run_namespace(self, run_id: str) -> bool:
        self.reactivated.append(run_id)
        if self._reactivate_raises is not None:
            raise self._reactivate_raises
        return False

    def active_decisions(self, goal=None):
        # Phase 2 (D1): run_item fetches decisions to inject. Default to none; a test
        # that exercises injection sets `.decisions` to a list of Decision-likes.
        return list(getattr(self, "decisions", []))

    def claim(self, item_id: str, run_id: str):
        self.claimed.append((item_id, run_id))
        it, self._claim_item = self._claim_item, None  # one-shot: re-claim → None
        return it

    def next_runnable(self, run_id: str, goal=None):
        self.dispatched.append((run_id, goal))
        it, self._next_item = self._next_item, None  # one-shot drain
        return it

    def apply_update(self, item_id, parsed, result, actor, run_id=None):
        self.applied.append((item_id, parsed, result, actor, run_id))
        if self._apply_raises is not None:
            raise self._apply_raises
        return None, self._apply_applied

    def transition(self, item_id, to_state, *, actor="", note="", evidence=None,
                   human_required=None, blocked_reason=None, run_id=None):
        self.transitioned.append((item_id, to_state, note, run_id))
        self.blocked_reasons.append(blocked_reason)
        if self._transition_raises is not None:
            raise self._transition_raises
        return None

    def get(self, item_id: str):
        return None

    def close(self) -> None:
        self.closed = True


class _RecordingSink:
    """A minimal OutputSink that records every (agent_id, chunk) feed — lets a
    test assert what teed without touching tmux or a file."""

    def __init__(self):
        self.fed: list[tuple[str, bytes]] = []

    def feed(self, agent_id: str, chunk: bytes) -> None:
        self.fed.append((agent_id, chunk))


class FakeMonitor:
    """Duck-typed Monitor for strategy tests: records an ordered ``events`` log and
    opens the REAL ``_OUTPUT_SINK`` inside ``streaming()`` so a test can assert the
    strategy actually wrapped its fan-out in the seam.

    Under ``FakeAgent`` no subprocess runs, so nothing tees (``feed`` never fires) —
    tests assert the strategy *entered* streaming (with the right agent ids), *exited*
    it, announced each *phase*, and reported one *status* per runnable agent (raw
    status, in order). ``stream_depth`` must be 0 at the end (balanced enter/exit);
    ``finished`` records the ``finish()`` call(s).
    """

    def __init__(self):
        self.events: list = []
        self.stream_depth = 0
        self.max_stream_depth = 0
        self.finished: list[TaskResult] = []
        self._sink = _RecordingSink()

    def phase(self, name: str) -> None:
        self.events.append(("phase", name))

    @contextlib.contextmanager
    def streaming(self, agent_ids):
        self.events.append(("stream-enter", list(agent_ids)))
        self.stream_depth += 1
        self.max_stream_depth = max(self.max_stream_depth, self.stream_depth)
        token = _agents._OUTPUT_SINK.set(self._sink)
        try:
            yield
        finally:
            _agents._OUTPUT_SINK.reset(token)
            self.stream_depth -= 1
            self.events.append(("stream-exit", list(agent_ids)))

    def status(self, agent_id: str, status: TaskStatus) -> None:
        self.events.append(("status", agent_id, status))

    def finish(self, result: TaskResult) -> None:
        self.finished.append(result)

    # convenience views for assertions
    @property
    def statuses(self) -> list[tuple[str, TaskStatus]]:
        return [(e[1], e[2]) for e in self.events if e[0] == "status"]

    @property
    def phases(self) -> list[str]:
        return [e[1] for e in self.events if e[0] == "phase"]


class FakeTracer:
    """Duck-typed Tracer for strategy/Supervisor tests (the FakeMonitor template):
    records SpanRecords in order; ``flush`` returns a preset path (or ``None``
    when ``flush_raises`` — after raising once — to drive the degrade path)."""

    def __init__(self, flush_path=None, flush_raises: bool = False):
        self.trace_id = "0" * 32
        self.spans: list = []
        self.flushed = 0
        self._flush_path = flush_path
        self._flush_raises = flush_raises

    def record(self, span) -> None:
        self.spans.append(span)

    def flush(self):
        self.flushed += 1
        if self._flush_raises:
            raise OSError("flush blew up")
        return self._flush_path

    # convenience views for assertions
    def by_node(self, node_id: str):
        return next(s for s in self.spans if s.node_id == node_id)

    @property
    def node_ids(self) -> list[str]:
        return [s.node_id for s in self.spans]


# ── Factories / fixtures ─────────────────────────────────────────────────


def make_memory(backend: FakeBackend) -> SyncmillMemory:
    """Build ``SyncmillMemory`` around a fake backend without real storage."""
    m = SyncmillMemory.__new__(SyncmillMemory)
    m._backend_name = "fake"
    m._backend = backend
    m.degraded = False
    m.last_error = None
    m._scope = None  # ordinary (non-board-scoped) memory — see SyncmillMemory.scoped
    m._global_shared_ns = None  # set only on a scoped view (D4 union-read source)
    m._agent_prefix = backend.agent_namespace_prefix
    m._shared_ns = backend.shared_namespace
    return m


@pytest.fixture
def make_agent():
    return FakeAgent


@pytest.fixture
def fake_memory():
    return FakeMemory()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repo (parent is tmp_path, so .syncmill-worktrees lands
    in tmp_path) with one committed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# test\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


# ── Autouse isolation ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_syncmill_env(monkeypatch):
    """Strip the developer's SYNCMILL_* vars so they can't leak into config tests."""
    for k in list(os.environ):
        if k.startswith("SYNCMILL_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def isolate_toml_discovery(monkeypatch, tmp_path):
    """Neutralize cwd-relative ``syncmill.toml`` discovery for the whole suite.

    ``SyncmillConfig`` reads ``model_config['toml_file'] = "syncmill.toml"``
    relative to the cwd (#11). pytest runs from the repo root, so a stray
    ``./syncmill.toml`` (e.g. a dev copying ``syncmill.toml.example`` to try the
    feature) would leak into every bare ``SyncmillConfig()`` and silently flip
    defaults — the same ambient-state hazard ``clean_syncmill_env`` /
    ``isolated_home`` already guard for env and ``~``. Point the toml source at a
    guaranteed-absent path so discovery is a no-op by default. Tests that exercise
    toml loading re-point it explicitly (``use_toml`` helper / a deliberate
    cwd-discovery test), and ``monkeypatch`` reverts it afterward."""
    from syncmill.config import SyncmillConfig

    monkeypatch.setitem(
        SyncmillConfig.model_config, "toml_file", str(tmp_path / "no-such-syncmill.toml")
    )


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    """Point HOME at a throwaway dir so a real CircuitBreaker (default state_path
    ``~/.syncmill/breaker.json``) or any other ``~/`` path can never read or write
    the developer's real home during tests.

    Belt-and-suspenders: breaker unit tests pass an explicit ``state_path`` and
    supervisor tests use ``FakeBreaker``, so nothing should reach ``~`` anyway —
    but a misconfigured test that enables a real breaker writes here, not to the
    real ``~/.syncmill``. Mirrors how ``git_repo`` keeps worktrees inside
    ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def hermetic_room_memory(monkeypatch):
    """Rooms lazily build a ``SyncmillMemory`` for round enrichment and the
    apply write-back; default that lazy construction to a no-op fake so no
    room test can ever reach a real memory backend (the ``isolated_home``
    belt-and-suspenders). Tests observing enrichment inject ``memory=``."""

    class _NoopRoomMemory:
        degraded = False
        last_error = None

        def __init__(self, _cfg):
            pass

        async def search_context(self, query, agent_id=None, *, top_k=None, **kwargs):
            return ""

        async def share_result(self, task, result, *, kind="task-result"):
            return None

    monkeypatch.setattr("syncmill.rooms.SyncmillMemory", _NoopRoomMemory)


@pytest.fixture(autouse=True)
def reset_output_sink():
    """Force the agents' live-output ``ContextVar`` (#16) back to ``None`` around
    every test, so a monitor/sink set by one test (or one that raised before its
    own reset) can never leak into another async test's ``aexecute``. The main
    thread's context persists for the whole pytest process, and
    ``reset_run_async_loop`` only resets the event loop — not this var."""
    _agents._OUTPUT_SINK.set(None)
    yield
    _agents._OUTPUT_SINK.set(None)


@pytest.fixture(autouse=True)
def reset_model_selectors():
    """Force the model-routing selectors ``ContextVar`` back to ``None`` around
    every test — the same leak hazard (and fix) as ``reset_output_sink``."""
    _agents._MODEL_SELECTORS.set(None)
    yield
    _agents._MODEL_SELECTORS.set(None)


@pytest.fixture(autouse=True)
def reset_run_async_loop():
    """Keep the module-global event loop from leaking across tests."""
    import syncmill.memory as mem

    original = mem._loop
    yield
    if mem._loop is not None and mem._loop is not original and not mem._loop.is_closed():
        mem._loop.close()
    mem._loop = original
