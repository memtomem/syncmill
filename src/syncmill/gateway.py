"""Read-only MCP gateway over syncmill's ``shared`` memory (#15).

A *one-way* (read-only) MCP surface that lets external/interactive MCP clients
(Claude Code/Desktop, Cursor, …) READ syncmill's accumulated ``shared`` knowledge
WITHOUT making syncmill's agents MCP-aware. The core invariant is untouched:
agents never touch MCP and the Supervisor remains the sole *writer*; this gateway
is a separate, non-agent, read-only *consumer*.

Scope is locked to the ``shared`` namespace: the three tools only ever read shared
chunks, never an agent's private ``agent-runtime:*`` scope (see the isolation guard
in ``SyncmillMemory.get_shared``). There are no write/delete/index tools.

All memtomem access flows through ``SyncmillMemory`` (advisory/best-effort), so a
broken or unavailable memtomem degrades gracefully — the server still starts and
the tools report "currently unavailable" rather than crashing.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from syncmill.memory import SyncmillMemory

SERVER_NAME = "syncmill-shared"
INSTRUCTIONS = (
    "Read-only gateway over syncmill's shared agent memory. Use shared_search to "
    "find knowledge by query, shared_read to fetch a full chunk by id, and "
    "shared_stats for the shared chunk count. This surface is read-only and scoped "
    "to the shared namespace; it cannot write memory or read any agent's private scope."
)

DEFAULT_TOP_K = 10
MAX_TOP_K = 50
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_UNAVAILABLE = "shared memory is currently unavailable"


def build_gateway(
    memory: SyncmillMemory,
    *,
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
    """
    mcp = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS, host=host, port=port)

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

    return mcp
