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
import contextlib
import logging
import threading
from dataclasses import dataclass
from time import monotonic
from collections.abc import Iterable
from typing import Any, Literal
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

# Ceiling on one board-run scratch collection pass. The sweep is best-effort and
# runs after an outcome or an empty claim. Unfinished namespaces remain
# collectable for the next pass; this is a cooperative maintenance budget.
_BOARD_GC_BUDGET_SECONDS = 30.0


@dataclass(frozen=True)
class BoardRunPurgeResult:
    """Per-pass receipts; deferred work is not a failed purge attempt."""

    successful: frozenset[str]
    failed: frozenset[str]
    unattempted: frozenset[str]


@dataclass(frozen=True)
class SharedRead:
    """One ``get_shared`` outcome, decided from that call alone (#169).

    ``found`` carries the shared chunk; ``not_found`` covers a missing id, a
    malformed id and a chunk outside ``shared`` alike (the privacy contract —
    a private chunk's existence is never disclosed); ``unavailable`` is a
    backend failure or a refused board-scoped view, never a data answer.
    """

    status: Literal["found", "not_found", "unavailable"]
    chunk: dict | None = None


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
#: Ceiling for a caller-widened `search_context`. A strategy that publishes N
#: artifacts and must read them all back asks for N, but `max_subtasks` is
#: free-form config, so the request is clamped: an unbounded top_k would make a
#: pathological setting issue a huge search whose rows the prompt cap then
#: discards anyway. Mirrors the gateway's MAX_TOP_K untrusted-input guard.
_CONTEXT_TOP_K_MAX = 50
#: Default UTF-8 byte budget for a formatted search_context block. It leaves
#: 200 bytes of spare context capacity below _enrich_prompt's 4,000-byte backstop;
#: neither budget includes the wrapper or the original task prompt.
_CONTEXT_MAX_BYTES = 3_800
_CONTEXT_SEPARATOR = "\n---\n"
_CONTEXT_SEPARATOR_BYTES = len(_CONTEXT_SEPARATOR.encode("utf-8"))


def _format_context_rows(
    results: list[dict[str, Any]], max_bytes: int = _CONTEXT_MAX_BYTES
) -> str:
    """Format memory search rows into a single text block bounded by max_bytes.

    Replaces uncoordinated static cuts with a coordinated, row-aware budget:
    - Normalizes surrogates with errors='replace' to avoid UnicodeEncodeError.
    - Bounds source prefix to 64 chars and computes exact UTF-8 byte length.
    - Computes character-aware minimum viable bytes M_i = min(C_full, C_trunc)
      where C_trunc = len(content[0].encode("utf-8")) + 3 (first char + "...").
    - Enforces overflow tail-drop loop: drops lowest-ranked tail rows until
      minimum budget fits within max_bytes.
    - Allocates baseline M_i to each surviving row, then distributes remaining
      surplus adaptively to longer rows.
    - Truncates exceeding rows strictly at valid UTF-8 character boundaries and
      appends '...'.
    - Guarantees total rendered UTF-8 bytes <= max_bytes.

    Coverage takes priority over excerpt depth: a wider window yields shorter
    excerpts. The minimum is a representation bound, not a semantic guarantee.
    Do not impose a larger fixed per-row floor that silently drops subtask rows.
    """
    if not results or max_bytes <= 0:
        return ""

    # Normalize source and content against lone surrogates
    normalized_rows: list[tuple[str, str, int, int]] = []
    for r in results:
        raw_source = str(r.get("source") or "unknown")
        norm_source = raw_source.encode("utf-8", errors="replace").decode("utf-8")[:64]
        prefix = f"[{norm_source}] "
        prefix_bytes = len(prefix.encode("utf-8"))

        raw_content = str(r.get("content") or "")
        norm_content = raw_content.encode("utf-8", errors="replace").decode("utf-8")
        full_bytes = len(norm_content.encode("utf-8"))

        if not norm_content:
            min_content_bytes = 0
        else:
            first_char_bytes = len(norm_content[0].encode("utf-8"))
            trunc_min = first_char_bytes + 3  # 1st char + "..."
            min_content_bytes = min(full_bytes, trunc_min)

        normalized_rows.append((prefix, norm_content, prefix_bytes, min_content_bytes))

    # Tail-drop loop: drop lowest-ranked tail rows until minimum budget fits
    n = len(normalized_rows)
    while n > 1:
        total_min = (
            sum(
                prefix_bytes + min_c
                for _, _, prefix_bytes, min_c in normalized_rows[:n]
            )
            + (n - 1) * _CONTEXT_SEPARATOR_BYTES
        )
        if total_min <= max_bytes:
            break
        n -= 1

    # Check if even single row cannot fit
    if n == 1:
        prefix, norm_content, prefix_bytes, min_content_bytes = normalized_rows[0]
        if prefix_bytes + min_content_bytes > max_bytes:
            return ""

    surviving = normalized_rows[:n]

    # Baseline allocation: give each surviving row its min_content_bytes
    allocated_content = [min_c for _, _, _, min_c in surviving]
    total_allocated = (
        sum(
            prefix_bytes + alloc
            for (_, _, prefix_bytes, _), alloc in zip(surviving, allocated_content)
        )
        + (n - 1) * _CONTEXT_SEPARATOR_BYTES
    )
    surplus = max_bytes - total_allocated

    # Distribute surplus adaptively to rows that have more content
    full_content_bytes = [
        len(content.encode("utf-8")) for _, content, _, _ in surviving
    ]
    unsatisfied = [i for i in range(n) if allocated_content[i] < full_content_bytes[i]]

    while surplus > 0 and unsatisfied:
        share = max(1, surplus // len(unsatisfied))
        progress = False
        new_unsatisfied = []
        for i in unsatisfied:
            needed = full_content_bytes[i] - allocated_content[i]
            grant = min(needed, share, surplus)
            if grant > 0:
                allocated_content[i] += grant
                surplus -= grant
                progress = True
            if allocated_content[i] < full_content_bytes[i]:
                new_unsatisfied.append(i)
            if surplus <= 0:
                break
        if not progress:
            break
        unsatisfied = new_unsatisfied

    # Render each row
    lines: list[str] = []
    for (prefix, content, _, _), alloc_bytes in zip(surviving, allocated_content):
        c_bytes = len(content.encode("utf-8"))
        if c_bytes <= alloc_bytes:
            lines.append(f"{prefix}{content}")
        else:
            # Need to truncate to fit within alloc_bytes (alloc_bytes >= min_content_bytes >= first_char + 3)
            max_c_bytes = alloc_bytes - 3  # reserve 3 bytes for "..."
            encoded = content.encode("utf-8")
            truncated_bytes = encoded[:max_c_bytes].decode("utf-8", errors="ignore")
            lines.append(f"{prefix}{truncated_bytes}...")

    rendered = _CONTEXT_SEPARATOR.join(lines)
    return rendered


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

    def _mark_healthy(self) -> None:
        """Latest-outcome semantics, the breaker's rule (`breaker.py`): a fully
        successful backend operation clears a PRIOR transient degrade, so a
        long-lived process (`board run --loop`, the TUI) doesn't report a
        recovered backend as degraded for the rest of its lifetime. Never called
        on a partially successful op (a partial share's lost provenance must
        stay surfaced until a later op fully succeeds), and a construction
        failure can never clear — a None backend has not succeeded at anything.
        """
        if self._backend is None:
            return
        self.degraded = False
        self.last_error = None

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
                    description if description is not None else f"Agent: {agent_id}"
                ),
                color=color if color is not None else _AGENT_COLORS.get(agent_id),
            )
            await self._backend.ensure_namespace(
                self._shared_ns, description="Shared knowledge"
            )
            self._mark_healthy()
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
            self._mark_healthy()
            return session_id
        except Exception as exc:
            self._degrade("start_task_session", exc)
            return None

    async def end_task_session(
        self, session_id: str | None, result: TaskResult
    ) -> None:
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
            self._mark_healthy()
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
        it) — under one lock span when the backend offers it
        (``_provenance_share``). Either step failing falls back to today's
        direct ``shared`` write.
        A board-scoped view always writes directly: both namespaces collapse to
        ``board-run:{run_id}``, so a copy would be pure duplication — and the
        write-leak guard stays byte-identical."""
        try:
            provenance_lost = False
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
                    chunk_ids, shared_ids = await self._provenance_share(
                        content, tags=tags, agent_id=result.agent_id
                    )
                    if chunk_ids:
                        # Every chunk of the result must reach `shared`.
                        if all(sid is not None for sid in shared_ids):
                            self._mark_healthy()
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
                    provenance_lost = True
            await self._backend.add(content, tags=tags, namespace=self._shared_ns)
            # The direct write clears a PRIOR degrade only when THIS call met its
            # whole contract — a provenance-lost fallback stays surfaced.
            if not provenance_lost:
                self._mark_healthy()
        except Exception as exc:
            self._degrade("share_result", exc)

    async def _provenance_share(
        self, content: str, *, tags: list[str], agent_id: str
    ) -> tuple[list[str], list[str | None]]:
        """Write the private original and copy every chunk of it to ``shared``.

        Returns ``(chunk_ids, shared_ids)``, aligned: ``shared_ids[i]`` is the
        copy of ``chunk_ids[i]`` or ``None``. A backend with
        ``single_span_share`` does both halves as ONE locked operation
        (``add_and_share``, #117); any other backend gets the two-call path —
        ``add_returning_ids``, then one ``share_chunk`` per chunk, serially.
        Both shapes raise the same way, so ``share_result``'s healthy /
        partial / fallback / ``WriteBlockedError`` rules read one result.
        """
        agent_ns = self._agent_ns(agent_id)
        combined = getattr(self._backend, "add_and_share", None)
        # getattr: a third-party capabilities object predating the field must
        # read as "off", not fail every share with an AttributeError.
        single_span = getattr(self._backend.capabilities, "single_span_share", False)
        if single_span and combined is not None:
            outcome = await combined(
                content,
                tags=tags,
                namespace=agent_ns,
                target_namespace=self._shared_ns,
            )
            return list(outcome.source_ids), list(outcome.shared_ids)
        chunk_ids = await self._backend.add_returning_ids(
            content, tags=tags, namespace=agent_ns
        )
        shared_ids = [
            await self._backend.share_chunk(cid, target_namespace=self._shared_ns)
            for cid in chunk_ids
        ]
        return chunk_ids, shared_ids

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
        Refuses on a SCOPED view, like every other ``shared``-facing method:
        shared-only BY CONSTRUCTION, not by name — on a board-scoped view
        ``_shared_ns`` IS the ``board-run:*`` scratch, so the one cross-link
        meant to outlive the run would land in the namespace the next GC pass
        deletes, and report success doing it.

        Best-effort: returns ``True`` on a successful write, ``False`` on any backend
        failure (degrading like the other writers — a board run never aborts because
        the global share failed)."""
        if self._scope is not None:
            return False
        try:
            # BOLD, never a `## ` heading — see the heading note at the top of this module.
            content = (
                f"**Board Item Result**\n\n**Item:** {item_id}\n\n"
                f"**Task:** {task_text}\n\n**Output:**\n{stripped_output[:1000]}"
            )
            tags = [f"board-result:{item_id}"]
            add_fn = getattr(self._backend, "add_returning_ids", None)
            if add_fn is not None:
                await add_fn(content, tags=tags, namespace=self._shared_ns)
            else:
                await self._backend.add(content, tags=tags, namespace=self._shared_ns)
            self._mark_healthy()
            return True
        except Exception as exc:
            self._degrade("share_board_result", exc)
            return False

    # ── Context search ───────────────────────────────────────────────────

    async def search_context(
        self,
        query: str,
        agent_id: str | None = None,
        *,
        top_k: int | None = None,
        tag_filter: str | None = None,
        max_bytes: int = _CONTEXT_MAX_BYTES,
    ) -> str:
        """Search agent-scoped + shared memories, return as formatted text.

        One unified merged-ranking search (mem_agent_search's include_shared
        semantics): the agent namespace and ``shared`` are one relevance-ranked
        pool — a single native union call on memtomem, an adapter-side merge on
        the other backends. Returns an empty string if memory is unavailable or
        nothing matches.

        ``tag_filter`` targets specific artifact kinds (e.g. ``pipeline-stage``,
        ``decompose-subtask``, or a comma-separated OR like
        ``council-proposal,council-critique``). Backends lacking tag filter support
        return an untagged search with a warning and degraded status.

        ``top_k`` widens the window (clamped to ``_CONTEXT_TOP_K_MAX``) for a
        caller that published N artifacts THIS run and needs to read them back:
        the default 5 is smaller than ``decompose.max_subtasks`` (8), so a
        default decompose could never surface three of its own subtask results.

        A wider window is not a guarantee of inclusion. Tags select artifact
        kinds, not this run: older rows of the same kind still compete, one
        result can occupy several chunks, and the byte budget may shorten or
        drop rows. Strategies therefore no longer rely on it for intra-run
        handoff: they pass the previous phase's results directly (#125).

        ``max_bytes`` enforces a coordinated, row-aware UTF-8 byte budget across
        all returned rows (default 3,800 bytes, fitting safely within the
        platform/argv prompt cap without blind mid-row byte cuts).
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

            window = (
                _CONTEXT_TOP_K
                if top_k is None
                else max(1, min(top_k, _CONTEXT_TOP_K_MAX))
            )

            effective_tag_filter = tag_filter
            if tag_filter is not None and not getattr(
                getattr(self._backend, "capabilities", None),
                "supports_tag_filter",
                True,
            ):
                effective_tag_filter = None

            results = await self._backend.search_multi(
                query,
                namespaces=namespaces,
                top_k=window,
                tag_filter=effective_tag_filter,
            )
            self._mark_healthy()

            if tag_filter is not None and effective_tag_filter is None:
                self._degrade(
                    "search_context",
                    ValueError(
                        "backend does not support tag_filter; returned unfiltered context"
                    ),
                )

            if not results:
                return ""

            return _format_context_rows(results[:window], max_bytes=max_bytes)
        except Exception as exc:
            self._degrade("search_context", exc)
            return ""

    # ── Read-only shared gateway (#15) ───────────────────────────────────
    #
    # These three methods back the read-only MCP gateway (see ``gateway.py``).
    # They are shared-scoped *structured* reads — distinct from ``search_context``
    # (which merges agent+shared scopes and formats text for prompt injection).
    # Each call reports its OWN outcome so the gateway can distinguish
    # "unavailable" from a healthy-empty result without consulting the sticky
    # ``degraded`` flag: ``search_shared``/``shared_stats`` return ``None`` on a
    # genuine per-call backend failure and the concrete value on success;
    # ``get_shared`` returns a ``SharedRead`` whose status separates ``found``,
    # ``not_found`` and ``unavailable`` (#169 — a plain ``None`` conflated a
    # storage outage with a missing chunk, so the MCP client was told "not
    # found" while the backend was down).
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
            self._mark_healthy()
            # Defense in depth: the namespace= arg already scopes the search, but
            # filter so the shared-only guarantee is local to this method.
            return [r for r in results if r.get("namespace") == self._shared_ns]
        except Exception as exc:
            self._degrade("search_shared", exc)
            return None

    async def get_shared(self, chunk_id: str) -> SharedRead:
        """Read one chunk by id, but ONLY if it lives in the shared namespace.

        Isolation guard for the gateway: ``MemtomemStore.get`` is not
        namespace-scoped, so a guessed/leaked id could otherwise pull an
        ``agent-runtime:*`` private chunk — anything not in ``shared`` is
        ``not_found`` (never the chunk), indistinguishable from a missing id.
        A malformed id is a client typo (``not_found``), NOT a backend failure,
        so it never degrades; only a genuine store error degrades, and THAT is
        reported as ``unavailable`` — decided from this call alone, never from
        the sticky ``degraded`` flag. A board-scoped view is refused as
        ``unavailable`` (its shared namespace is the board-run scratch).
        """
        if self._scope is not None:
            return SharedRead("unavailable")
        try:
            UUID(str(chunk_id))
        except (ValueError, TypeError):
            return SharedRead("not_found")
        try:
            r = await self._backend.get(chunk_id)
            self._mark_healthy()
        except Exception as exc:
            self._degrade("get_shared", exc)
            return SharedRead("unavailable")
        if r is None or r.get("namespace") != self._shared_ns:
            return SharedRead("not_found")
        return SharedRead("found", r)

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
            self._mark_healthy()
            return {"namespace": self._shared_ns, "chunk_count": count}
        except Exception as exc:
            self._degrade("shared_stats", exc)
            return None

    # ── Board-run namespace GC ───────────────────────────────────────────
    #
    # Board SQLite owns discovery/liveness in P2. Memory only performs the known,
    # prefix-reconstructed deletions and reports exactly which calls completed.

    async def delete_board_runs(self, run_ids: Iterable[str]) -> set[str] | None:
        """Compatibility surface returning only successful deletion IDs.

        Schedulers must use purge_board_runs to distinguish failures from work
        deferred by the deadline. None means backend readiness was unavailable.
        """
        result = await self.purge_board_runs(run_ids)
        return None if result is None else set(result.successful)

    async def purge_board_runs(
        self, run_ids: Iterable[str]
    ) -> BoardRunPurgeResult | None:
        """Delete the ``board-run:{run_id}`` namespace for each given run_id.

        The prefix is reconstructed HERE from bare run_ids, so this method can
        only ever delete ``board-run:*`` namespaces — never ``shared`` or
        ``agent-runtime:*`` (a guard by construction, not by validation).
        Each ``delete_namespace`` is independent, so a per-namespace failure
        degrades and CONTINUES. Returns immutable successful, failed and unattempted run-ID sets
        (including idempotent zero-chunk success), or ``None``
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

        A backend offering ``purge_namespace`` does both halves with syncmill's
        writers LOCKED OUT, which is what closes the window between them — see
        that method for exactly which writers are excluded (every syncmill
        write, which takes the same namespace lock) and which (a memtomem-native
        write, which does not) are not. It raises rather than reporting a count when the
        purge cannot be proven complete, so an unprovable purge degrades below
        and the namespace stays collectable.

        Backends without it fall back to the two-call sequence plus a second
        source sweep. That sweep restores the no-file-outlives-its-rows
        invariant and, because finding something means a writer was attached,
        disqualifies the acknowledgement — a namespace with a live producer must
        stay collectable rather than be tombstoned. It NARROWS the window rather
        than closing it: a write landing after the sweep is still possible.

        A zero-chunk delete is a genuine purge, not a partial one: memtomem
        removes the namespace's metadata row whether or not it held chunks
        (#1705, fixed in 0.3.9 — below syncmill's version floor, so every
        version the pin admits behaves this way). So a registered-but-never-
        written scratch namespace is acknowledged and stops being a candidate,
        rather than being re-attempted by every later sweep.
        """
        # This operation owns its deadline, including backend readiness. An
        # outer timeout would discard successful receipts before the supervisor
        # can tombstone them and schedule failed candidates for backoff.
        gc_deadline = monotonic() + _BOARD_GC_BUDGET_SECONDS
        try:
            async with asyncio.timeout(max(0.0, gc_deadline - monotonic())):
                await self._backend.ensure_ready()
        except Exception as exc:
            self._degrade("delete_board_runs", exc)
            return None
        drop_sources = getattr(self._backend, "delete_namespace_sources", None)
        purge = getattr(self._backend, "purge_namespace", None)
        successful: set[str] = set()
        attempted: set[str] = set()
        ids = list(dict.fromkeys(rid for rid in run_ids if rid))
        for run_id in ids:
            if monotonic() >= gc_deadline:
                logger.info(
                    "board-run scratch collection stopped after %.0fs; the rest "
                    "stay collectable for the next sweep",
                    _BOARD_GC_BUDGET_SECONDS,
                )
                break
            attempted.add(run_id)
            namespace = f"{BOARD_RUN_PREFIX}{run_id}"
            try:
                async with asyncio.timeout(max(0.0, gc_deadline - monotonic())):
                    if purge is not None:
                        # Both halves under the writer lock; raises when it cannot
                        # prove the namespace is gone, which the degrade below turns
                        # into "stays collectable".
                        await purge(namespace)
                        successful.add(run_id)
                        continue
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
                    # Nothing locks a writer out between those two calls, and a run
                    # collected while still alive is a documented (narrowed, not
                    # closed) race. A write landing in that window creates a file
                    # AND its row, and the row delete above then takes the row —
                    # leaving exactly the stranded markdown this ordering exists to
                    # prevent. So sweep once more: this both restores the invariant
                    # (no file outlives its rows) and DETECTS that a writer was
                    # active, which is what disqualifies the acknowledgement below.
                    raced = bool(
                        drop_sources is not None and await drop_sources(namespace)
                    )
            except Exception as exc:
                self._degrade("delete_board_runs", exc)
                continue
            if raced:
                # A concurrent write means the namespace may be written again a
                # moment from now. Tombstoning would retire it from collection
                # with a live producer still attached, so leave it collectable.
                self._degrade(
                    "delete_board_runs",
                    RuntimeError(f"namespace {namespace} was written during purge"),
                )
                continue
            # Acknowledged only once BOTH halves are done, with no sign of a
            # racing writer. A namespace whose sources could not be dropped
            # raised above and is likewise never acknowledged — it must stay
            # collectable for a later sweep to retry rather than be tombstoned.
            successful.add(run_id)
        # Latest-outcome: only a sweep where EVERY attempted delete completed
        # clears a prior degrade — a partial sweep's failures stay surfaced.
        # `ids` guards the vacuous case: all([]) is True, so an empty sweep
        # would clear a prior degrade having performed no backend operation at
        # all — only ensure_ready() ran.
        if ids and all(rid in successful for rid in ids):
            self._mark_healthy()
        return BoardRunPurgeResult(
            successful=frozenset(successful),
            failed=frozenset(attempted - successful),
            unattempted=frozenset(set(ids) - attempted),
        )

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

    An interrupt is contained HERE, not left to the callers: a
    ``KeyboardInterrupt`` escapes ``run_until_complete`` with the in-flight
    future still pending on the REUSED loop, and any later ``run_async`` (a
    ``Supervisor.close()``, a room finalizer) would then resume the stranded
    fan-out — with its now-detached agent subprocesses — mid-teardown. So on an
    exception the future did not itself produce, the future is cancelled and
    bounded-drained (each ``aexecute`` cancellation tail kills + reaps its
    child) before the interrupt propagates. ``close_event_loop`` remains the
    backstop for anything scheduled outside this entry point.
    """
    global _loop
    with _LOOP_GUARD:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
        loop = _loop
    task = asyncio.ensure_future(coro, loop=loop)
    try:
        return loop.run_until_complete(task)
    except BaseException:
        # task.done() means the exception came FROM the coroutine — nothing is
        # stranded. Pending means the interrupt cut run_until_complete short.
        if not task.done():
            task.cancel()

            async def _drain() -> None:
                await asyncio.wait([task], timeout=_PENDING_DRAIN_TIMEOUT_SECONDS)

            with contextlib.suppress(BaseException):
                # A second Ctrl-C during the drain is deliberately absorbed —
                # the FIRST interrupt is re-raised right below either way.
                loop.run_until_complete(_drain())
        raise


#: Deadline on best-effort memory calls made from the sync orchestration path
#: (the Supervisor and the strategies). ``SyncmillMemory`` degrades on
#: *exceptions*, but a HUNG backend — an unreachable Qdrant, a stuck network
#: read — never raises: without a bound the whole run hangs between fan-outs
#: with worktrees held. The bound covers CANCELLATION-COOPERATIVE async waits
#: (see ``run_memory_bounded``) — it is not a wall-clock guarantee against
#: loop-blocking sync work or a coroutine that suppresses cancellation. Shared
#: with the rooms path (``_ROOM_MEMORY_TIMEOUT_SECONDS``) so the two surfaces
#: keep one policy.
MEMORY_CALL_TIMEOUT_SECONDS = 30.0


def run_memory_bounded(
    coro: Any,
    *,
    default: Any = None,
    memory: "SyncmillMemory | None" = None,
    label: str = "memory call",
    timeout: float = MEMORY_CALL_TIMEOUT_SECONDS,
) -> Any:
    """``run_async`` with the advisory-memory hang bound.

    On timeout the underlying backend operation is cancelled, ``default`` is
    returned, and — when ``memory`` is passed — the memory surface is marked
    degraded so the CLI's yellow notice reports it, exactly like any other
    best-effort memory failure.

    The deadline is COOPERATIVE (``asyncio.wait_for``), not a wall-clock
    guarantee: a backend coroutine that blocks the loop synchronously (an
    un-offloaded sqlite/embed call) delays the timer itself, and one that
    suppresses cancellation and completes has its late value returned as a
    normal success (``wait_for``'s documented behavior — for an advisory read,
    a late real value beats the fallback). What the bound reliably converts is
    the *await-shaped* hang — an unreachable Qdrant, a stuck network read —
    the common failure class on this path — while one that never reaches an
    await point (or suppresses cancellation without completing) stays hung; a
    hard wall-clock bound would need process/thread isolation the advisory
    surface doesn't justify.
    """

    async def _bounded() -> Any:
        return await asyncio.wait_for(coro, timeout)

    try:
        return run_async(_bounded())
    except (asyncio.TimeoutError, TimeoutError) as exc:
        logger.warning("%s timed out after %ss", label, timeout)
        if memory is not None:
            memory._degrade(label, exc)
        return default


#: Bound on draining cancelled tasks at loop shutdown. Each cancelled
#: ``aexecute`` SIGKILLs its child before its first await and reaps within
#: ``agents._REAP_TIMEOUT_SECONDS`` (5s), concurrently — 10s covers that with
#: headroom while guaranteeing shutdown itself can never hang.
_PENDING_DRAIN_TIMEOUT_SECONDS = 10.0


def _cancel_pending_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel and drain tasks left pending on the shared loop, time-bounded.

    A SIGINT escapes ``run_until_complete`` *without* cancelling the in-flight
    future, so a Ctrl-C during a concurrent fan-out strands the gather and every
    ``aexecute`` task — with live agent subprocesses — pending on the reused
    module loop. Left there, a later ``run_async`` (e.g. ``supervisor.close()``)
    re-enters the loop and RESUMES them mid-teardown, and ``loop.close()`` would
    destroy them un-awaited, leaking the children past process exit. Cancelling
    here triggers each ``aexecute``'s cancellation tail (kill + bounded reap),
    so the subprocesses die with the loop.
    """
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if not pending:
        return
    logger.warning(
        "cancelling %d task(s) left pending on the shared event loop", len(pending)
    )
    for task in pending:
        task.cancel()

    async def _drain() -> None:
        await asyncio.wait(pending, timeout=_PENDING_DRAIN_TIMEOUT_SECONDS)

    loop.run_until_complete(_drain())


def close_event_loop() -> None:
    """Close the shared event loop. Call once at process shutdown (the CLI
    does this in its finally blocks) so the loop's resources are released.

    Pending tasks are cancelled and drained first (see ``_cancel_pending_tasks``
    — the Ctrl-C-mid-fan-out case), then async generators are shut down —
    closing without draining them leaves backend resources (e.g. aiosqlite
    handles) finalized against a dead loop, the exact class of error the shared
    loop exists to avoid."""
    global _loop
    with _LOOP_GUARD:
        if _loop is not None and not _loop.is_closed():
            try:
                _cancel_pending_tasks(_loop)
            except Exception:
                logger.warning("pending-task drain failed", exc_info=True)
            try:
                _loop.run_until_complete(_loop.shutdown_asyncgens())
            except Exception:
                logger.warning("event-loop asyncgen shutdown failed", exc_info=True)
            _loop.close()
        _loop = None
