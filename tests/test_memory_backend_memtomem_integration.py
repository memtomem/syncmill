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

        # identical-content-two-namespaces: the direct-add fallback writes to
        # `shared` content that already exists in the AGENT namespace — pin that
        # dedup is per namespace, not content-global (else the fallback would
        # silently no-op and the share would be lost). Fresh content shared to
        # nowhere yet, which is the state the fallback actually runs in: the
        # two-step reached the agent namespace and then failed to copy.
        # Asserted on retrievable CONTENT, not on the chunk count, since a long
        # result legitimately splits into several chunks.
        stranded = "syncmill real smoke: the fallback path content"
        run_async(
            backend.add_returning_ids(
                stranded, tags=["task-result", "claude"], namespace=agent_ns
            )
        )
        run_async(backend.add(stranded, tags=["task-result"], namespace="shared"))
        landed = run_async(backend.search(stranded, namespace="shared", top_k=5))
        assert any(stranded in r["content"] for r in landed)

        # Same-second writes to ONE namespace each keep their own live chunk.
        # append_entry's default heading is second-resolution, and two entries
        # sharing a heading in one file re-chunk into a SINGLE chunk: the first
        # entry's text was swallowed and its id — already returned here — was
        # re-minted, so the provenance share that follows found nothing and
        # silently fell back to a direct write. Back-to-back writes are what a
        # pipeline/council/decompose handoff does, so this must hold with no
        # wait between them. Only the real store re-chunks; the hermetic fake
        # can pin the unique heading but never this consequence.
        #
        # The payloads use the PRODUCTION shape `share_result` builds — a bold
        # self-label, deliberately not a `## ` heading. That distinction is the
        # whole fix: the writer skips the unique heading whenever content
        # supplies its own, and emits the tags blockquote before it, so a
        # heading-led payload merges AND loses its tags. Exercising plain text
        # here would pass while every real orchestration write still broke.
        rapid = [
            run_async(
                backend.add_returning_ids(
                    f"**Task Result**\n\n**Prompt:** rapid write {i}\n\n"
                    f"**Agent:** claude\n\n**Output:**\nwidget {i} refactored",
                    tags=["task-result", f"task:rapid-{i}"],
                    namespace=agent_ns,
                )
            )
            for i in range(3)
        ]
        assert all(group for group in rapid), "a rapid write minted no chunk id"
        for i, group in enumerate(rapid):
            for cid in group:
                row = run_async(backend.get(cid))
                assert row is not None, f"rapid write {i} chunk id was re-minted"
                assert f"rapid write {i}" in row["content"]
                # tags survive only when the entry owns its whole section
                assert f"task:rapid-{i}" in row["tags"]
        # and each is retrievable by its own tag, which a merged chunk is not
        for i in range(3):
            hits = run_async(
                backend.search(
                    f"rapid write {i}",
                    namespace=agent_ns,
                    top_k=5,
                    tag_filter=f"task:rapid-{i}",
                )
            )
            assert hits, f"rapid write {i} is not retrievable by its tag"

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

        # A background/watcher rescan passes NO explicit namespace. Incremental
        # reindexing must preserve an unchanged chunk's stamped namespace — the
        # property the namespace-suffixed files rely on. (A FORCED no-namespace
        # reindex re-stamps to the default namespace, but that is pre-existing
        # memtomem behavior for EVERY explicit-namespace write, including
        # MemtomemStore.add's shared `{date}.md` — not a syncmill divergence.)
        comp = run_async(backend._store._ensure_init())
        suffixed = sorted(mem_dir.glob("*.agent-runtime.claude.md"))
        assert suffixed, "namespace-suffixed daily file was not written"
        run_async(
            comp.index_engine.index_file(suffixed[0], already_scanned=True)
        )
        still = run_async(storage.get_chunk(UUID(chunk_ids[0])))
        assert still is not None
        assert still.metadata.namespace == agent_ns
    finally:
        run_async(backend.close())


@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_MEMTOMEM_REAL") != "1",
    reason="set SYNCMILL_TEST_MEMTOMEM_REAL=1 to run the real memtomem smoke",
)
def test_real_deleted_board_run_does_not_resurface_in_shared(tmp_path):
    """A collected board-run scratch namespace must stay gone across a reindex.

    delete_by_namespace removes DB rows only, while the markdown is what the
    index is rebuilt FROM. If a board run's scratch content shared a daily file
    with `shared`, a later `shared` write re-indexes that file, finds the
    deleted text missing from the index, treats it as new, and stamps it
    `shared` — putting board-internal content into global knowledge. This pins
    both halves of the fix: per-namespace files, and dropping the source files
    along with the rows.
    """
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    backend = MemtomemBackend(
        MemoryConfig(db_path=str(tmp_path / "mm.db"), memory_dir=str(mem_dir))
    )
    scratch_ns = "board-run:run-1"
    secret = "syncmill board scratch: internal handoff nobody outside may read"
    try:
        run_async(backend.add(secret, tags=["pipeline-stage"], namespace=scratch_ns))
        run_async(
            backend.add("a public result", tags=["task-result"], namespace="shared")
        )
        # Each namespace owns its own daily file — a shared one is what lets a
        # `shared` write re-index the scratch text at all.
        assert sorted(p.name for p in mem_dir.glob("*.md")) == sorted(
            p.name
            for p in [
                *mem_dir.glob("*.board-run.run-1.md"),
                *mem_dir.glob("*.shared.md"),
            ]
        )

        run_async(backend.delete_namespace(scratch_ns))
        run_async(backend.delete_namespace_sources(scratch_ns))
        assert not list(mem_dir.glob("*.board-run.run-1.md"))

        # The GC'd content must not come back under ANY namespace, including via
        # a later write to shared and a full rescan of the memory dir.
        run_async(
            backend.add("another public result", tags=["task-result"], namespace="shared")
        )
        comp = run_async(backend._store._ensure_init())
        for path in sorted(mem_dir.glob("*.md")):
            run_async(comp.index_engine.index_file(path, already_scanned=True))

        rows = run_async(backend.search(secret, namespace="shared", top_k=10))
        assert not any(secret in r["content"] for r in rows)
        assert run_async(backend.count_namespace(scratch_ns)) == 0
    finally:
        run_async(backend.close())
