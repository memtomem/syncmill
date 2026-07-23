"""Opt-in, fully local smoke for the real memtomem multi-agent surface.

REQUIRED before changing ``MemtomemBackend``'s replicated add flow
(``add_returning_ids``/``share_chunk``/``register_namespace``/``search_multi``)
or the memtomem version pin: the flow reaches memtomem privates
(``_ensure_init``/``_components``, ``index_engine.index_file``,
``storage.add_chunk_link``/``set_namespace_meta``) plus the non-server helpers
(``memory_scope.require_user_base``, ``privacy.enforce_write_guard``,
``tools.memory_writer.append_entry``) — drift there fails SILENTLY at runtime
(the two-step falls back to the direct write and provenance quietly vanishes),
so only this real smoke catches it. The migration/LangGraph real-smoke
precedent (``SYNCMILL_TEST_*_REAL=1``) applies verbatim.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest

from syncmill.config import MemoryConfig
from syncmill.memory import run_async
from syncmill.memory_backend import MemtomemBackend

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_MEMTOMEM_REAL") != "1",
    reason="set SYNCMILL_TEST_MEMTOMEM_REAL=1 to run the real memtomem smoke",
)
def test_real_memtomem_multiagent_surface(tmp_path):
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    backend = MemtomemBackend(
        MemoryConfig(db_path=str(tmp_path / "mm.db"), memory_dir=str(mem_dir))
    )
    agent_ns = "agent-runtime:claude"
    try:
        # register: unconditional metadata upsert, None leaves fields untouched
        run_async(
            backend.register_namespace(
                agent_ns, description="Agent: claude", color="#D97757"
            )
        )
        run_async(backend.register_namespace(agent_ns, color="#000000"))
        storage = run_async(backend._storage())
        meta = run_async(storage.get_namespace_meta(agent_ns))
        assert meta["description"] == "Agent: claude"  # None left it untouched
        assert meta["color"] == "#000000"

        # the replicated add flow yields real fresh chunk ids
        content = "syncmill real smoke: the parser fix landed in module X"
        chunk_ids = run_async(
            backend.add_returning_ids(
                content, tags=["task-result", "claude"], namespace=agent_ns
            )
        )
        assert chunk_ids, "replicated add flow returned no chunk ids"

        # provenance share: copy + collapsed shared-from tag + chunk_links row
        shared_id = run_async(
            backend.share_chunk(chunk_ids[0], target_namespace="shared")
        )
        assert shared_id and shared_id != chunk_ids[0]
        copy = run_async(backend.get(shared_id))
        assert copy["namespace"] == "shared"
        assert f"shared-from={chunk_ids[0]}" in copy["tags"]
        link = run_async(storage.get_chunk_link(UUID(shared_id), "shared"))
        assert link is not None and str(link.source_id) == chunk_ids[0]

        # unified merged-ranking search covers both namespaces in one call
        rows = run_async(
            backend.search_multi(
                "parser fix", namespaces=[agent_ns, "shared"], top_k=5
            )
        )
        assert {r["namespace"] for r in rows} == {agent_ns, "shared"}

        # identical-content-two-namespaces: the direct-add fallback writes the
        # SAME content to `shared` that already exists in the agent namespace —
        # pin that memtomem's dedup is not content-global (else the fallback
        # would silently no-op and the share would be lost).
        before = run_async(backend.count_namespace("shared"))
        run_async(backend.add(content, tags=["task-result"], namespace="shared"))
        assert run_async(backend.count_namespace("shared")) > before

        # MCP-write parity: a write is searchable immediately — the TTL search
        # cache must not serve the pre-write result (the intra-run handoff).
        # Semantic search returns unrelated rows too, so the assertion is on
        # the probe text appearing, not on emptiness.
        probe = "council critique of the widget renderer refactor"
        warmed = run_async(backend.search(probe, namespace="shared", top_k=3))
        assert not any(probe in r["content"] for r in warmed)  # warm the cache
        run_async(backend.add(probe, tags=["council-critique"], namespace="shared"))
        fresh = run_async(backend.search(probe, namespace="shared", top_k=3))
        assert any(probe in r["content"] for r in fresh)
    finally:
        run_async(backend.close())
