"""Shared memory layer — syncmill conventions over a memory backend.

Supervisor is the sole reader/writer. Agents never touch this directly.

Memory is *advisory*: every operation is best-effort. If memtomem is
unavailable or misconfigured (e.g. an unsupported embedding provider), the
store degrades gracefully — operations log a warning, set ``degraded``, and
return safe defaults — so the Supervisor can still run agents. Memory is an
enhancement, not a hard dependency for executing a task.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from syncmill.config import MemoryConfig
from syncmill.models import Task, TaskResult

logger = logging.getLogger(__name__)

# Prefix of the per-board-run scratch namespaces (`board-run:{run_id}`), set by
# `scoped` and swept by the GC methods below. A syncmill convention (the board
# is syncmill's, not memtomem's), so declared here rather than derived from
# memtomem.constants like the agent/shared names.
BOARD_RUN_PREFIX = "board-run:"


class SyncmillMemory:
    """syncmill-specific conventions over a pluggable memory backend.

    Namespace layout (derived from memtomem.constants):
        agent-runtime:{agent_id}  — private namespace per agent
        shared                    — cross-agent knowledge base
    """

    def __init__(self, config: MemoryConfig):
        # Memory is advisory even at construction: if the backend can't be imported
        # or built, degrade instead of crashing Supervisor(config). With a None
        # backend, each method's try/except below catches the AttributeError and
        # returns its safe default.
        self.degraded = False
        self.last_error: str | None = None
        self._backend = None
        # When set (by `scoped`), BOTH the per-agent namespace and the shared
        # namespace collapse to this one per-run scope (`board-run:{run_id}`) — so
        # a board run's strategy-internal handoffs AND its episodic session
        # prompt-log stay off the persistent global `shared` / `agent-runtime:*`
        # namespaces. None for an ordinary memory (the default).
        self._scope: str | None = None
        # The REAL global ``shared`` namespace, captured by ``scoped`` so a
        # board-scoped view can UNION-READ cross-run knowledge from it (D4) while its
        # WRITES stay confined to the per-run scope (the leak guard). None on an
        # ordinary memory — which already reads global ``shared`` as ``_shared_ns``.
        self._global_shared_ns: str | None = None
        # Namespace conventions, derived from memtomem's constants (its
        # constants.py asks importers to derive the values rather than
        # re-declare the literal). The fallbacks match memtomem 0.2.x and only
        # apply when memtomem can't be imported — in which case the backend is
        # None and these are never used anyway.
        self._agent_prefix = "agent-runtime:"
        self._shared_ns = "shared"
        try:
            from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
            from syncmill.memory_backend import build_backend

            self._agent_prefix = AGENT_NAMESPACE_PREFIX
            self._shared_ns = SHARED_NAMESPACE
            self._backend = build_backend(config)
            self._agent_prefix = self._backend.agent_namespace_prefix
            self._shared_ns = self._backend.shared_namespace
        except Exception as exc:
            self._degrade("init", exc)

    def _degrade(self, where: str, exc: Exception) -> None:
        """Record a non-fatal memtomem failure and keep going."""
        self.degraded = True
        self.last_error = f"{type(exc).__name__}: {exc}"
        logger.warning("memory unavailable during %s: %s", where, self.last_error)

    def _agent_ns(self, agent_id: str) -> str:
        """The per-agent namespace — ``agent-runtime:{agent_id}`` normally, or the
        per-run scope when this is a board-scoped view. The single place the
        agent-vs-scope choice is made, so every read/write/session honors it."""
        return self._scope or f"{self._agent_prefix}{agent_id}"

    def scoped(self, run_id: str) -> "SyncmillMemory":
        """A board-scoped VIEW of this memory: both the shared namespace AND every
        per-agent (session/search) namespace collapse to ``board-run:{run_id}``.

        Closes the board↔``shared`` leak: ``run_item`` runs a board item through the
        normal ``run`` pipeline with this view, so the strategy-internal shares
        (pipeline/council/decompose) AND ``start_task_session``'s prompt-log all
        write to the per-run scope — intra-run handoff still works — and NOTHING
        board-enriched lands on the persistent global ``shared`` or
        ``agent-runtime:*`` namespaces. The single sanitized summary that DOES reach
        global ``shared`` is written separately via :meth:`share_board_result` on
        the UNSCOPED memory. Shares the underlying backend; best-effort like the
        parent (a backend failure during a board run degrades this view, not the
        parent — board runs are advisory on memory like any other run)."""
        scope = f"{BOARD_RUN_PREFIX}{run_id}"
        if self._backend is not None:
            try:
                valid = self._backend.validate_namespace(scope)
            except Exception as exc:
                # Validation is specified as pure/non-raising. Preserve memory's
                # advisory posture if a broken backend violates that contract.
                self._degrade("validate_namespace", exc)
            else:
                if not valid:
                    raise ValueError(f"invalid board-run memory namespace: {scope!r}")

        view = SyncmillMemory.__new__(SyncmillMemory)
        view._backend = self._backend
        view.degraded = False
        view.last_error = None
        view._agent_prefix = self._agent_prefix
        view._shared_ns = scope
        view._scope = scope
        # Remember the real global ``shared`` so search_context can UNION-READ it for
        # cross-run context (D4). Writes still target the per-run ``_shared_ns`` above
        # — only reads widen, so the board↔shared write leak guard is untouched.
        view._global_shared_ns = self._shared_ns
        return view

    # ── Agent registration ───────────────────────────────────────────────

    async def register_agent(self, agent_id: str) -> None:
        """Ensure agent-runtime:{agent_id} and shared namespaces exist."""
        # Scratch scopes are implicit-create and short-lived. Explicit metadata
        # registration produces a metadata-only row that memtomem 0.2.4 cannot
        # delete (#1705), so only persistent/global memory is registered.
        if self._scope is not None:
            return
        try:
            ns = self._agent_ns(agent_id)
            await self._backend.ensure_namespace(ns, description=f"Agent: {agent_id}")
            await self._backend.ensure_namespace(
                self._shared_ns, description="Shared knowledge"
            )
        except Exception as exc:
            self._degrade("register_agent", exc)

    # ── Task session lifecycle ───────────────────────────────────────────

    async def start_task_session(self, agent_id: str, task: Task) -> str | None:
        """Start an episodic memory session. Returns the session id, or None
        if memory is unavailable."""
        try:
            ns = self._agent_ns(agent_id)
            session_id = await self._backend.start_session(agent_id, ns)
            await self._backend.log_event("task_start", f"Prompt: {task.prompt[:200]}")
            return session_id
        except Exception as exc:
            self._degrade("start_task_session", exc)
            return None

    async def end_task_session(self, session_id: str | None, result: TaskResult) -> None:
        """End the session and log the outcome."""
        try:
            summary = (
                f"{result.agent_id}: {result.status.value} "
                f"(exit={result.exit_code}, {result.duration_seconds}s)"
            )
            if result.error:
                await self._backend.log_event("task_error", result.error[:500])
            await self._backend.log_event(
                "task_end",
                f"exit_code={result.exit_code}, duration={result.duration_seconds}s",
            )
            await self._backend.end_session(summary)
        except Exception as exc:
            self._degrade("end_task_session", exc)

    # ── Result sharing ───────────────────────────────────────────────────

    async def share_result(self, task: Task, result: TaskResult) -> None:
        """Write a successful result summary to the shared namespace."""
        try:
            content = (
                f"## Task Result\n\n**Prompt:** {task.prompt}\n\n"
                f"**Agent:** {result.agent_id}\n\n**Output:**\n{result.output[:1000]}"
            )
            tags = ["task-result", result.agent_id]
            if result.files_changed:
                content += f"\n\n**Files changed:** {', '.join(result.files_changed)}"
            await self._backend.add(content, tags=tags, namespace=self._shared_ns)
        except Exception as exc:
            self._degrade("share_result", exc)

    async def share_board_result(
        self, item_id: str, task_text: str, stripped_output: str
    ) -> bool:
        """Write ONE sanitized board-item summary to the GLOBAL ``shared`` namespace.

        Called by ``run_item`` on the UNSCOPED memory after a board run, so this is
        the single board-derived write that reaches persistent global knowledge.
        The content is built from the item's task text + the agent output with the
        ``syncmill-board`` block stripped (``board.strip_board_block``) — NEVER the
        board-enriched prompt, decisions, or handoff — so no board-internal context
        leaks into ``shared``.

        **The cross-link is the SINGLE composite tag** ``board-result:{item_id}``:
        memtomem ``add()`` returns ``{"file", "indexed_chunks"}`` with NO chunk id,
        and ``search(tag_filter=...)`` takes a ``str`` (comma = OR), so the exact
        composite tag is the retrieval key — never a ``["board-result", item_id]``
        list (which ``tag_filter`` would treat as an OR of two unrelated tags).
        Best-effort: returns ``True`` on a successful write, ``False`` on any backend
        failure (degrading like the other writers — a board run never aborts because
        the global share failed)."""
        try:
            content = (
                f"## Board Item Result\n\n**Item:** {item_id}\n\n"
                f"**Task:** {task_text}\n\n**Output:**\n{stripped_output[:1000]}"
            )
            tags = [f"board-result:{item_id}"]
            await self._backend.add(content, tags=tags, namespace=self._shared_ns)
            return True
        except Exception as exc:
            self._degrade("share_board_result", exc)
            return False

    # ── Context search ───────────────────────────────────────────────────

    async def search_context(self, query: str, agent_id: str | None = None) -> str:
        """Search agent-scoped + shared memories, return as formatted text.

        Returns an empty string if memory is unavailable or nothing matches.
        """
        try:
            results: list[dict] = []

            # Search agent namespace if specified
            if agent_id:
                ns = self._agent_ns(agent_id)
                results.extend(await self._backend.search(query, namespace=ns, top_k=3))

            # Always search shared namespace
            shared_results = list(
                await self._backend.search(query, namespace=self._shared_ns, top_k=3)
            )

            # D4: a board-scoped view ALSO union-reads the REAL global `shared`, so a
            # board run is enriched by cross-run knowledge (prior `board-result`
            # summaries, task results) — not only its per-run `board-run:{run_id}`
            # scope. Reads widen; writes stay scoped (the leak guard is untouched). A
            # plain (unscoped) run skips this — its `_shared_ns` IS global `shared`.
            if self._scope is not None and self._global_shared_ns:
                shared_results += list(
                    await self._backend.search(
                        query, namespace=self._global_shared_ns, top_k=3
                    )
                )

            # Merge and deduplicate by chunk id
            seen: set[str] = {r["id"] for r in results}
            for r in shared_results:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    results.append(r)

            # Sort by score descending
            results.sort(key=lambda r: -r["score"])

            if not results:
                return ""

            # Format as text for prompt injection
            lines = [
                f"[{r.get('source', 'unknown')}] {r['content'][:300]}"
                for r in results[:5]
            ]
            return "\n---\n".join(lines)
        except Exception as exc:
            self._degrade("search_context", exc)
            return ""

    # ── Read-only shared gateway (#15) ───────────────────────────────────
    #
    # These three methods back the read-only MCP gateway (see ``gateway.py``).
    # They are shared-scoped *structured* reads — distinct from ``search_context``
    # (which merges agent+shared scopes and formats text for prompt injection).
    # They return ``None`` on a genuine per-call backend failure (so the gateway
    # can distinguish "unavailable" from a healthy-empty result without consulting
    # the sticky ``degraded`` flag), and the concrete value on success.

    async def search_shared(self, query: str, top_k: int = 10) -> list[dict] | None:
        """Search ONLY the shared namespace; return the raw result rows.

        Returns ``None`` on backend failure, else the (possibly empty) list of
        rows whose namespace is ``shared``.
        """
        try:
            results = await self._backend.search(
                query, namespace=self._shared_ns, top_k=top_k
            )
            # Defense in depth: the namespace= arg already scopes the search, but
            # filter so the shared-only guarantee is local to this method.
            return [r for r in results if r.get("namespace") == self._shared_ns]
        except Exception as exc:
            self._degrade("search_shared", exc)
            return None

    async def get_shared(self, chunk_id: str) -> dict | None:
        """Read one chunk by id, but ONLY if it lives in the shared namespace.

        Isolation guard for the gateway: ``MemtomemStore.get`` is not
        namespace-scoped, so a guessed/leaked id could otherwise pull an
        ``agent-runtime:*`` private chunk — return ``None`` for anything not in
        ``shared``. A malformed id is a client typo (not-found), NOT a backend
        failure, so it returns ``None`` *without* degrading; only a genuine store
        error degrades.
        """
        try:
            UUID(str(chunk_id))
        except (ValueError, TypeError):
            return None
        try:
            r = await self._backend.get(chunk_id)
            if r is None or r.get("namespace") != self._shared_ns:
                return None
            return r
        except Exception as exc:
            self._degrade("get_shared", exc)
            return None

    async def shared_stats(self) -> dict | None:
        """Chunk count of the shared namespace (only).

        Uses the backend's namespace counter and reports the ``shared`` entry
        alone — never other namespaces, so it
        can't disclose the existence of agent scopes. Returns ``None`` on backend
        failure, else ``{"namespace": "shared", "chunk_count": N}`` (N=0 if shared
        isn't created yet).
        """
        try:
            count = await self._backend.count_namespace(self._shared_ns)
            return {"namespace": self._shared_ns, "chunk_count": count}
        except Exception as exc:
            self._degrade("shared_stats", exc)
            return None

    # ── Board-run namespace GC ───────────────────────────────────────────
    #
    # Board SQLite owns discovery/liveness in P2. Memory only performs the known,
    # prefix-reconstructed deletions and reports exactly which calls completed.

    async def delete_board_runs(self, run_ids: Iterable[str]) -> set[str] | None:
        """Delete the ``board-run:{run_id}`` namespace for each given run_id.

        The prefix is reconstructed HERE from bare run_ids, so this method can
        only ever delete ``board-run:*`` namespaces — never ``shared`` or
        ``agent-runtime:*`` (a guard by construction, not by validation).
        Each ``delete_namespace`` is independent, so a per-namespace failure
        degrades and CONTINUES. Returns the run IDs whose delete call completed
        without raising (including an idempotent zero-chunk result), or ``None``
        only when the backend itself is unreachable and nothing could be tried.

        Known legacy residue (memtomem#1705): a zero-chunk delete does not remove
        an existing metadata-only row. P2 stops creating new scratch metadata
        rows explicitly; zero still acknowledges the ledger because there is no
        chunk content left to collect.
        """
        try:
            await self._backend.ensure_ready()
        except Exception as exc:
            self._degrade("delete_board_runs", exc)
            return None
        successful: set[str] = set()
        for run_id in run_ids:
            if not run_id:
                continue
            try:
                await self._backend.delete_namespace(f"{BOARD_RUN_PREFIX}{run_id}")
                successful.add(run_id)
            except Exception as exc:
                self._degrade("delete_board_runs", exc)
        return successful

    # ── Status ───────────────────────────────────────────────────────────

    async def status(self) -> dict:
        """Check memtomem connection status."""
        try:
            info = await self._backend.status()
            return {"connected": True, **info}
        except Exception as e:
            return {"connected": False, "error": str(e)}

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def close(self) -> None:
        try:
            await self._backend.close()
        except Exception as exc:
            self._degrade("close", exc)


_loop: asyncio.AbstractEventLoop | None = None


def run_async(coro: Any) -> Any:
    """Run an async coroutine from sync context, reusing the event loop.

    memtomem is async; the rest of syncmill is sync. A single reused loop
    avoids the "Event loop is closed" error that ``asyncio.run`` triggers on
    memtomem's lazily-initialized resources.
    """
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop.run_until_complete(coro)


def close_event_loop() -> None:
    """Close the shared event loop. Call once at process shutdown (the CLI
    does this in its finally blocks) so the loop's resources are released."""
    global _loop
    if _loop is not None and not _loop.is_closed():
        _loop.close()
    _loop = None
