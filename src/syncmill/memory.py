"""Shared memory layer — syncmill conventions over a memory backend.

Supervisor is the sole reader/writer. Agents never touch this directly.

Memory is *advisory*: every operation is best-effort. If the configured backend is
unavailable or misconfigured (e.g. an unsupported embedding provider), the
store degrades gracefully — operations log a warning, set ``degraded``, and
return safe defaults — so the Supervisor can still run agents. Memory is an
enhancement, not a hard dependency for executing a task.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from syncmill.config import MemoryConfig

# Module-level on purpose: memory_backend itself is stdlib+config only (every
# SDK import inside it is lazy), so this cannot re-introduce the construction
# failures that __init__'s lazy build_backend import guards against.
from syncmill.memory_backend import WriteBlockedError
from syncmill.models import Task, TaskResult

logger = logging.getLogger(__name__)

# Prefix of the per-board-run scratch namespaces (`board-run:{run_id}`), set by
# `scoped` and swept by the GC methods below. A syncmill convention (the board
# is syncmill's, not memtomem's), so declared here rather than derived from
# memtomem.constants like the agent/shared names.
BOARD_RUN_PREFIX = "board-run:"

# WHY EVERY PAYLOAD BELOW LABELS ITSELF IN **BOLD** RATHER THAN WITH A `## `
# HEADING (the call sites point here):
#
# memtomem's `format_entry_block` treats heading-led content as supplying its
# own heading and skips the one syncmill passes — so a `## Task Result` payload
# produced the SAME heading on every result. Two chunks sharing a heading are
# mergeable, and the damage was not limited to the same-second window that
# motivated the unique heading: consecutive task results were packed into ONE
# chunk, the chunk ids already returned to `share_result` were re-minted (the
# provenance share then found nothing), and the tags blockquote — emitted
# BEFORE the content's own heading in that branch — landed in the previous
# entry's section, so the surviving chunk carried NO tags and `tag_filter`
# could not retrieve it at all.
#
# A heading-free payload keeps the whole entry in one section under the unique
# `## Entry …` heading, which is where the tags are correctly attributed. Note
# a bold line is also NOT enough on its own if the body carries headings of its
# own: agent output is embedded verbatim, so an agent that writes `## Summary`
# still opens a section whose heading two runs can share. That residue predates
# this change and cannot be closed without either mutating the agent's text
# (the backend contract is verbatim storage) or a chunker change upstream.

# Advisory per-agent namespace colors for register_agent's mem_agent_register-style
# metadata. Unknown agent ids map to None, which leaves any existing color as-is.
_AGENT_COLORS = {
    "claude": "#D97757",
    "codex": "#10A37F",
    "kimi-code": "#1E80FF",
}

# How many merged-ranking rows one search_context call requests (and renders).
_CONTEXT_TOP_K = 5


class SyncmillMemory:
    """syncmill-specific conventions over a pluggable memory backend.

    Namespace layout (supplied by the selected backend):
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
        self._backend_name = config.backend
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
        # Namespace conventions come from the selected backend. The fallbacks
        # match syncmill's established public layout and are unused when backend
        # construction fails.
        self._agent_prefix = "agent-runtime:"
        self._shared_ns = "shared"
        try:
            from syncmill.memory_backend import build_backend

            self._backend = build_backend(config)
            self._agent_prefix = self._backend.agent_namespace_prefix
            self._shared_ns = self._backend.shared_namespace
        except Exception as exc:
            self._degrade("init", exc)

    def _degrade(self, where: str, exc: Exception) -> None:
        """Record a non-fatal memory-backend failure and keep going."""
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
        parent. Scope-validation failures degrade the parent before the view exists;
        later backend failures degrade the view that performs the operation. Board
        runs remain advisory on memory like any other run.

        THE ONE DELIBERATELY-RAISING METHOD: an invalid scope raises
        ``ValueError`` (the class contract everywhere else is "degrade and
        return a safe default") because the caller is about to take
        load-bearing board mutations against that scope — the Supervisor wraps
        it into a ``BoardError`` before any claim is taken."""
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
        view._backend_name = self._backend_name
        view._backend = self._backend
        view.degraded = False
        view.last_error = None
        view._agent_prefix = self._agent_prefix
        view._shared_ns = scope
        view._scope = scope
        # Remember the real global ``shared`` so search_context can UNION-READ it for
        # cross-run context (D4). Writes still target the per-run ``_shared_ns`` above
        # — only reads widen, so the board↔shared write leak guard is untouched.
        # On an already-scoped parent `_shared_ns` IS that parent's scratch, so
        # carry the parent's captured global instead — a doubly-scoped view must
        # never union-read another run's scratch as "shared".
        view._global_shared_ns = self._global_shared_ns or self._shared_ns
        return view

    # ── Agent registration ───────────────────────────────────────────────

    async def register_agent(
        self,
        agent_id: str,
        *,
        description: str | None = None,
        color: str | None = None,
    ) -> None:
        """Register agent-runtime:{agent_id} metadata and ensure ``shared`` exists.

        The agent namespace gets mem_agent_register-style metadata (an
        unconditional description/color upsert), so registered agents are
        identifiable from memtomem's own CLI/MCP surfaces."""
        # Scratch scopes are implicit-create and short-lived. Explicit metadata
        # registration produces a metadata-only row that memtomem 0.2.4 cannot
        # delete (#1705), so only persistent/global memory is registered.
        if self._scope is not None:
            return
        try:
            ns = self._agent_ns(agent_id)
            # `None` means "not provided" → default; an explicit "" is a valid
            # clear-the-field update in memtomem and must pass through verbatim.
            await self._backend.register_namespace(
                ns,
                description=(
                    description
                    if description is not None
                    else f"Agent: {agent_id}"
                ),
                color=color if color is not None else _AGENT_COLORS.get(agent_id),
            )
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

    async def share_result(
        self, task: Task, result: TaskResult, *, kind: str = "task-result"
    ) -> None:
        """Write a successful result summary to the shared namespace.

        ``kind`` is the artifact-type tag (``task-result``, ``decompose-plan``,
        ``council-proposal``, …) so unified search / ``tag_filter`` can pull one
        artifact class precisely; ``task:{task.id}`` cross-links every artifact
        of one run (the ``board-result:{item_id}`` composite-tag precedent).

        Unscoped, the write is an agent-share two-step: the result lands in the
        producing agent's private ``agent-runtime:{agent_id}`` namespace first,
        then is COPIED to ``shared`` with mem_agent_share provenance (a
        ``shared-from=<id>`` tag + a chunk_links row where the backend supports
        it). Either step failing falls back to today's direct ``shared`` write.
        A board-scoped view always writes directly: both namespaces collapse to
        ``board-run:{run_id}``, so a copy would be pure duplication — and the
        write-leak guard stays byte-identical."""
        try:
            # BOLD, never a `## ` heading — see the heading note at the top of this module.
            content = (
                f"**Task Result**\n\n**Prompt:** {task.prompt}\n\n"
                f"**Agent:** {result.agent_id}\n\n**Output:**\n{result.output[:1000]}"
            )
            tags = [kind, result.agent_id, f"task:{task.id}"]
            if result.files_changed:
                content += f"\n\n**Files changed:** {', '.join(result.files_changed)}"
            if self._scope is None:
                try:
                    chunk_ids = await self._backend.add_returning_ids(
                        content, tags=tags, namespace=self._agent_ns(result.agent_id)
                    )
                    if chunk_ids:
                        shared_ids = [
                            await self._backend.share_chunk(
                                cid, target_namespace=self._shared_ns
                            )
                            for cid in chunk_ids
                        ]
                        # Every chunk of the result must reach `shared`.
                        if all(sid is not None for sid in shared_ids):
                            return
                        # A PARTIAL share is not a failed share: some chunks are
                        # already in `shared`, so the direct fallback below would
                        # add a second full copy beside them — inflating
                        # shared_stats and feeding near-duplicate rows to every
                        # reader. Surface the incomplete provenance instead; a
                        # raising share_chunk degrades the same way just below.
                        if any(sid is not None for sid in shared_ids):
                            self._degrade(
                                "share_result",
                                RuntimeError(
                                    f"shared {sum(s is not None for s in shared_ids)}"
                                    f" of {len(shared_ids)} chunks"
                                ),
                            )
                            return
                    # No fresh ids at all (a content-hash dedup) is a benign
                    # edge, not a failure: nothing reached `shared`, so the
                    # direct write below is the whole share and adds no
                    # duplicate. Memory stays healthy.
                except WriteBlockedError as exc:
                    # The redaction guard blocked this content; the direct
                    # fallback runs the same guard on the same content and can
                    # only re-block — degrade once and stop.
                    self._degrade("share_result", exc)
                    return
                except Exception as exc:
                    # Fall through to the direct write — a broken provenance
                    # path must not lose the share entirely — but SURFACE the
                    # loss: the private-original/link contract was not met.
                    self._degrade("share_result", exc)
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
            # BOLD, never a `## ` heading — see the heading note at the top of this module.
            content = (
                f"**Board Item Result**\n\n**Item:** {item_id}\n\n"
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

        One unified merged-ranking search (mem_agent_search's include_shared
        semantics): the agent namespace and ``shared`` are one relevance-ranked
        pool — a single native union call on memtomem, an adapter-side merge on
        the other backends. Returns an empty string if memory is unavailable or
        nothing matches.
        """
        try:
            namespaces: list[str] = []
            if agent_id:
                namespaces.append(self._agent_ns(agent_id))
            if self._shared_ns not in namespaces:
                namespaces.append(self._shared_ns)

            # D4: a board-scoped view ALSO union-reads the REAL global `shared`, so a
            # board run is enriched by cross-run knowledge (prior `board-result`
            # summaries, task results) — not only its per-run `board-run:{run_id}`
            # scope. Reads widen; writes stay scoped (the leak guard is untouched). A
            # plain (unscoped) run skips this — its `_shared_ns` IS global `shared`.
            if (
                self._scope is not None
                and self._global_shared_ns
                and self._global_shared_ns not in namespaces
            ):
                namespaces.append(self._global_shared_ns)

            results = await self._backend.search_multi(
                query, namespaces=namespaces, top_k=_CONTEXT_TOP_K
            )

            if not results:
                return ""

            # Format as text for prompt injection
            lines = [
                f"[{r.get('source', 'unknown')}] {r['content'][:300]}"
                for r in results[:_CONTEXT_TOP_K]
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
    #
    # Shared-only BY CONSTRUCTION, not by name: on a board-scoped view
    # ``_shared_ns`` IS the ``board-run:*`` scratch, so these readers refuse
    # (return ``None`` = unavailable) rather than silently serving scratch as
    # shared — a scoped view handed to ``build_gateway`` would otherwise pass
    # the namespace checks while leaking board-internal content.

    async def search_shared(self, query: str, top_k: int = 10) -> list[dict] | None:
        """Search ONLY the shared namespace; return the raw result rows.

        Returns ``None`` on backend failure, else the (possibly empty) list of
        rows whose namespace is ``shared``.
        """
        if self._scope is not None:
            return None
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
        if self._scope is not None:
            return None
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
        if self._scope is not None:
            return None
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

        Deleting the rows is not enough on a markdown-backed store: the files are
        what the index is rebuilt FROM, so a backend that can also drop a
        namespace's source files is asked to FIRST, and the rows go only once
        that succeeded. Otherwise the next reindex resurrects the scratch
        content — and, its rows now gone, stamps it with whatever namespace that
        pass resolves, which is how board-internal text would reach global
        ``shared``. The rows are what keep a surviving file harmless in the
        meantime, so they must outlive it, not the other way round. Backends
        without source files simply don't offer the method.

        A zero-chunk delete is a genuine purge, not a partial one: memtomem
        removes the namespace's metadata row whether or not it held chunks
        (#1705, fixed in 0.3.9 — below syncmill's version floor, so every
        version the pin admits behaves this way). So a registered-but-never-
        written scratch namespace is acknowledged and stops being a candidate,
        rather than being re-attempted by every later sweep.
        """
        try:
            await self._backend.ensure_ready()
        except Exception as exc:
            self._degrade("delete_board_runs", exc)
            return None
        drop_sources = getattr(self._backend, "delete_namespace_sources", None)
        successful: set[str] = set()
        for run_id in run_ids:
            if not run_id:
                continue
            namespace = f"{BOARD_RUN_PREFIX}{run_id}"
            try:
                # SOURCES FIRST. Rows are what make a surviving file harmless:
                # while they exist, a reindex recognizes the text and leaves its
                # namespace alone. Delete them first and any failure below
                # strands the markdown with nothing claiming it, so the next
                # reindex reads it as new content and stamps it with whatever
                # namespace that pass resolves — the leak into `shared` this
                # whole path exists to prevent. The reverse residue is benign:
                # files gone, rows still scoped to the scratch namespace, and
                # the run stays unacknowledged so a later sweep retries them.
                if drop_sources is not None:
                    await drop_sources(namespace)
                await self._backend.delete_namespace(namespace)
            except Exception as exc:
                self._degrade("delete_board_runs", exc)
                continue
            # Acknowledged only once BOTH halves are done. A namespace whose
            # source file survived is not purged — the rows are gone, so the
            # next reindex would read that text as new content — so it must stay
            # collectable for a later sweep to retry rather than be tombstoned.
            successful.add(run_id)
        return successful

    # ── Status ───────────────────────────────────────────────────────────

    async def status(self) -> dict:
        """Check the configured memory backend's connection status."""
        if self._backend is None:
            return {
                "connected": False,
                "error": self.last_error or "memory backend unavailable",
            }
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
# Guards the create/replace/close of the module loop. run_until_complete itself
# is NOT made thread-safe by this (a second thread would still die on "loop
# already running") — every run_async caller in src/ is on the main thread; the
# lock only prevents two racing threads from each minting a loop and leaking
# one, and serializes close against a concurrent create.
_LOOP_GUARD = threading.Lock()


def run_async(coro: Any) -> Any:
    """Run an async coroutine from sync context, reusing the event loop.

    Memory backends are async; the rest of syncmill is sync. A single reused loop
    avoids the "Event loop is closed" error that ``asyncio.run`` triggers on
    backend resources.
    """
    global _loop
    with _LOOP_GUARD:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
        loop = _loop
    return loop.run_until_complete(coro)


def close_event_loop() -> None:
    """Close the shared event loop. Call once at process shutdown (the CLI
    does this in its finally blocks) so the loop's resources are released.

    Async generators are shut down before the close — closing without draining
    them leaves backend resources (e.g. aiosqlite handles) finalized against a
    dead loop, the exact class of error the shared loop exists to avoid."""
    global _loop
    with _LOOP_GUARD:
        if _loop is not None and not _loop.is_closed():
            try:
                _loop.run_until_complete(_loop.shutdown_asyncgens())
            except Exception:
                logger.warning("event-loop asyncgen shutdown failed", exc_info=True)
            _loop.close()
        _loop = None
