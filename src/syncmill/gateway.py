"""Read-only MCP gateway over syncmill's ``shared`` memory + work-board (#15, Phase 2).

A *one-way* (read-only) MCP surface that lets external/interactive MCP clients
(Claude Code/Desktop, Cursor, …) READ syncmill's accumulated ``shared`` knowledge
AND its work-board WITHOUT making syncmill's agents MCP-aware. The core invariant is
untouched: agents never touch MCP and the Supervisor remains the sole *writer*; this
gateway is a separate, non-agent, read-only *consumer*.

Six read-only tools, no write/delete/index tool: ``shared_search`` / ``shared_read``
/ ``shared_stats`` are locked to the ``shared`` namespace (never an agent's private
``agent-runtime:*`` scope — see the isolation guard in ``SyncmillMemory.get_shared``);
``board_list`` / ``board_get`` / ``board_stats`` (Phase 2) read the work-board when it
is enabled, and report disabled-vs-unavailable when it is not.

All memtomem access flows through ``SyncmillMemory`` and all board access through the
``Board`` store (both advisory/best-effort here), so a broken or unavailable backend
degrades gracefully — the server still starts and the tools report "currently
unavailable" rather than crashing.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from syncmill.board import (
    Board,
    BoardError,
    WorkState,
    render_board,
    render_board_stats,
    render_item,
)
from syncmill.memory import SyncmillMemory

SERVER_NAME = "syncmill-shared"
INSTRUCTIONS = (
    "Read-only gateway over syncmill's shared agent memory and work-board. Use "
    "shared_search / shared_read / shared_stats for shared knowledge, and "
    "board_list / board_get / board_stats to READ the work-board (items, an item's "
    "detail + audit history, and team-health stats). The surface is read-only — it "
    "cannot write memory or the board, and cannot read any agent's private scope."
)

DEFAULT_TOP_K = 10
MAX_TOP_K = 50
DEFAULT_BOARD_LIMIT = 50
MAX_BOARD_LIMIT = 200
#: The valid `state` filter values for board_list — derived from the work-item state
#: machine so a client-supplied bad state is a clear ARGUMENT error, not a board outage.
_VALID_BOARD_STATES = tuple(s.value for s in WorkState)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_UNAVAILABLE = "shared memory is currently unavailable"
#: The board_* tools always exist (a stable 6-tool surface), but report this when
#: the server was started without the board enabled — distinct from a backend error.
_BOARD_DISABLED = "the work-board is not enabled on this server"
_BOARD_UNAVAILABLE = "the work-board is currently unavailable"


def build_gateway(
    memory: SyncmillMemory,
    *,
    board: Board | None = None,
    board_error: str | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> FastMCP:
    """Construct the read-only MCP server (without running it).

    ``host``/``port`` are passed at *construction* so FastMCP computes its
    DNS-rebinding ``transport_security`` policy from the actual bind host (it does
    this in ``__init__``, not lazily) — mutating ``mcp.settings`` afterward would
    leave a loopback-only policy on a non-loopback bind. They are inert for stdio.

    ``memory`` is injected (not built here) so tests can pass a fake-store-backed
    ``SyncmillMemory`` with no monkeypatching, and so the CLI owns its lifetime.
    The tools read memory's *per-call* return value (``None`` == backend failure)
    to choose their message — they never consult the sticky ``memory.degraded``
    flag, so a healthy-empty result is never mislabeled "unavailable".

    ``board`` is the optional Phase-2 work-board (read-only). The board_* tools are
    registered UNCONDITIONALLY — the surface is a stable six tools so the exact-set
    regression guard never depends on whether the board is enabled. When ``board is
    None`` they report ``_BOARD_DISABLED`` (genuinely off) UNLESS ``board_error`` is
    set — i.e. the board WAS enabled but failed to open — in which case they report
    ``_BOARD_UNAVAILABLE: <error>``, preserving the disabled-vs-broken distinction for
    clients (mirrors the shared tools' healthy-empty-vs-unavailable split) while
    keeping the shared-memory gateway alive. Like the shared tools they preserve
    read-only + no-auth (no board-write tool exists). The board is sqlite-backed and
    SYNCHRONOUS; its reads are quick, so calling them directly inside the async MCP
    handlers is fine (no thread offload needed)."""
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, host=host, port=port)

    def _board_or_message() -> Board | str:
        """The board_* tools' shared guard: the usable ``Board`` (a narrowed
        reference type checkers can see, unlike a bare None-check on the closed-over
        ``Board | None``), else the right message — ``_BOARD_UNAVAILABLE`` for an
        enabled-but-broken board (an error was reported), ``_BOARD_DISABLED`` for a
        genuinely-off one."""
        if board is not None:
            return board
        return f"{_BOARD_UNAVAILABLE}: {board_error}" if board_error else _BOARD_DISABLED

    @mcp.tool()
    async def shared_search(query: str, top_k: int = DEFAULT_TOP_K) -> str:
        """Search syncmill's shared memory and return ranked excerpts.

        Args:
            query: Natural-language search query.
            top_k: Maximum number of results to return (clamped to 1..50).
        """
        # Clamp untrusted input — an unauthenticated client must not be able to
        # request an unbounded / oversized search.
        top_k = max(1, min(int(top_k), MAX_TOP_K))
        hits = await memory.search_shared(query, top_k=top_k)
        if hits is None:
            return _UNAVAILABLE
        if not hits:
            return "No matches in shared memory."
        lines = [
            f"{i}. [score {r.get('score', 0):.2f}] {r.get('source', 'unknown')} "
            f"(id: {r.get('id', '?')})\n   {r.get('content', '')[:300]}"
            for i, r in enumerate(hits, 1)
        ]
        return "\n".join(lines)

    @mcp.tool()
    async def shared_read(chunk_id: str) -> str:
        """Read the full content of one shared-memory chunk by its id.

        Args:
            chunk_id: The chunk id (as shown in shared_search results).
        """
        chunk = await memory.get_shared(chunk_id)
        if chunk is None:
            # Covers missing, malformed, and non-shared ids alike — all "not
            # found" from a read-only shared consumer's point of view.
            return f"Chunk {chunk_id} not found in shared memory."
        header = [
            f"## Chunk {chunk.get('id', chunk_id)}",
            f"- Source: {chunk.get('source', 'unknown')}",
        ]
        tags = ", ".join(chunk.get("tags") or [])
        if tags:
            header.append(f"- Tags: {tags}")
        return "\n".join(header) + f"\n\n{chunk.get('content', '')}"

    @mcp.tool()
    async def shared_stats() -> str:
        """Report how many chunks are in syncmill's shared memory."""
        stats = await memory.shared_stats()
        if stats is None:
            return _UNAVAILABLE
        return f"shared: {stats['chunk_count']} chunks"

    @mcp.tool()
    async def board_list(
        state: str | None = None,
        goal: str | None = None,
        limit: int = DEFAULT_BOARD_LIMIT,
    ) -> str:
        """List work-board items (read-only), MOST-RECENT-first by creation order.

        Args:
            state: Optional state filter (planned/running/blocked/review/done).
            goal: Optional goal filter.
            limit: Maximum items to return (clamped to 1..200).
        """
        bd = _board_or_message()
        if isinstance(bd, str):
            return bd
        # A bad `state` filter is a CLIENT argument error, not a backend outage —
        # validate it up front and return a corrective message (with the valid set),
        # so the LLM fixes the argument rather than concluding the board is down.
        if state is not None and state not in _VALID_BOARD_STATES:
            return (
                f"Unknown state {state!r}. Valid states: "
                f"{', '.join(_VALID_BOARD_STATES)}."
            )
        limit = max(1, min(int(limit), MAX_BOARD_LIMIT))
        try:
            items = bd.list(state=state, goal=goal)
        except BoardError as exc:
            # A genuine backend/db error (the state was already validated above).
            return f"{_BOARD_UNAVAILABLE}: {exc}"
        if not items:
            return "No board items match."
        # Board.list is oldest-first FIFO (the CLI shows it that way). For an LLM
        # reading "current board state" through this surface, the NEWEST items matter
        # most — so reverse to most-recent-first and keep the newest `limit` (NOT the
        # oldest: `items[:limit]` would silently drop all recent work on a big board).
        return render_board(list(reversed(items))[:limit])

    @mcp.tool()
    async def board_get(item_id: str) -> str:
        """Read one work-board item in full — state, evidence, handoff, audit history.

        Args:
            item_id: The item id (as shown in board_list).
        """
        bd = _board_or_message()
        if isinstance(bd, str):
            return bd
        try:
            # `get` validates the id (a malformed/guessed id is not-found, not a probe)
            # and returns None for a missing item; only a real db error raises.
            item = bd.get(item_id)
            if item is None:
                return f"Board item {item_id} not found."
            return render_item(item, bd.transitions(item_id))
        except BoardError as exc:
            return f"{_BOARD_UNAVAILABLE}: {exc}"

    @mcp.tool()
    async def board_stats() -> str:
        """Report work-board team-health stats — state breakdown, redispatched work,
        stale-lease reclaims, false-done, and stalled (aged blocked / human-required /
        dropped-handoff) items."""
        bd = _board_or_message()
        if isinstance(bd, str):
            return bd
        try:
            return render_board_stats(bd.stats())
        except BoardError as exc:
            return f"{_BOARD_UNAVAILABLE}: {exc}"

    return mcp
