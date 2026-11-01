"""Owned subprocess for the opt-in real-store GC and share-span concurrency tests.

Markers are synchronization barriers, not success evidence. Final JSON is
emitted only after the actual backend operation completes. All state is below
an explicit temporary root; no provider is invoked.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from syncmill.config import MemoryConfig
from syncmill.memory_backend import MemtomemBackend, _namespace_lock_path

NS = "board-run:11111111-1111-4111-8111-111111111111"
AGENT_NS = "agent-runtime:claude"
SHARE_TAGS = ["council-proposal", "claude", "task:t1"]
# The production payload shape: bold-led, sections in the agent's output, so
# the original splits into several chunks and the share copies each one.
SHARE_CONTENT = (
    "**Task Result**\n\n**Prompt:** span test\n\n**Agent:** claude\n\n"
    "**Output:**\n## Plan\nstep one\n## Risks\nrisk two\n## Summary\nall good"
)


async def main(root: Path, mode: str, name: str) -> None:
    def mark(stage: str) -> None:
        (root / f"{name}.{stage}").touch()

    async def wait(stage: str) -> None:
        async with asyncio.timeout(20):
            while not (root / f"{name}.{stage}").exists():
                await asyncio.sleep(0.01)

    backend = MemtomemBackend(MemoryConfig(
        db_path=str(root / "mm.db"), memory_dir=str(root / "mem"),
    ))
    watcher = None
    try:
        await backend.ensure_ready()
        from memtomem.context import _atomic

        # Allow the parent to exercise each barrier even on a slow CI worker.
        _atomic._CRUD_SIDECAR_LOCK_BUDGET_S = 15
        original_lock = _atomic.async_file_lock
        namespace_lock = _namespace_lock_path(root / "mem", NS)

        @asynccontextmanager
        async def observed_lock(path, **kwargs):
            # Any syncmill namespace lock counts (a share takes two); the purge
            # modes only ever take NS's.
            is_ns = path == namespace_lock or path.name.startswith(".syncmill-ns-")
            kind = "namespace" if is_ns else "source"
            mark(f"{kind}-attempt")
            async with original_lock(path, **kwargs):
                mark(f"{kind}-held")
                if mode == "paused-purge" and kind == "namespace":
                    await wait("release")
                yield

        _atomic.async_file_lock = observed_lock
        if mode == "watcher":
            from memtomem.indexing.watcher import FileWatcher

            comp = await backend._store._ensure_init()
            get_state = comp.storage.get_chunk_index_state
            paused = False

            async def pause_after_read(path):
                nonlocal paused
                value = await get_state(path)
                if not paused and ".board-run." in str(path):
                    paused = True
                    # Real watcher -> index_file -> read -> diff. The source's
                    # real L2 lock is still held, before the DB transaction.
                    mark("read")
                    await wait("release")
                return value

            comp.storage.get_chunk_index_state = pause_after_read
            watcher = FileWatcher(comp.index_engine, comp.config.indexing, debounce_ms=10)
            await watcher.start()
            mark("ready")
            await wait("stop")
            result = {"watcher_observed": paused}
        elif mode in {"write", "paused-write"}:
            original_append = backend._locked_append

            async def append(*args, **kwargs):
                if mode == "paused-write":
                    mark("write-held")
                    await wait("release")
                return await original_append(*args, **kwargs)

            backend._locked_append = append
            result = {"ids": await backend.add_returning_ids(
                "late private stage result", tags=["pipeline-stage"], namespace=NS,
            )}
        elif mode == "paused-share":
            # Pause INSIDE add_and_share's span, after the original is appended
            # and indexed and before any copy is: the window the two-call path
            # leaves open between its halves.
            original_append = backend._append_and_index
            calls = 0

            async def append(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    mark("share-between")
                    await wait("release")
                return await original_append(*args, **kwargs)

            backend._append_and_index = append
            out = await backend.add_and_share(
                SHARE_CONTENT, tags=SHARE_TAGS, namespace=AGENT_NS,
                target_namespace="shared",
            )
            result = {"source_ids": list(out.source_ids),
                      "shared_ids": list(out.shared_ids)}
        elif mode == "paused-two-call":
            # The same pause on the two-call path, for contrast: between the
            # private original and its first share_chunk, holding nothing.
            source_ids = await backend.add_returning_ids(
                SHARE_CONTENT, tags=SHARE_TAGS, namespace=AGENT_NS,
            )
            mark("share-between")
            await wait("release")
            shared_ids = [
                await backend.share_chunk(cid, target_namespace="shared")
                for cid in source_ids
            ]
            result = {"source_ids": source_ids, "shared_ids": shared_ids}
        elif mode in {"write-shared", "write-private"}:
            namespace = "shared" if mode == "write-shared" else AGENT_NS
            result = {"ids": await backend.add_returning_ids(
                f"**Note**\n\ninterleaver {name}", tags=["note"], namespace=namespace,
            )}
        else:
            if mode == "crash-after-unlink":
                storage = await backend._storage()

                async def crash(ns):
                    mark("files-removed")
                    os._exit(23)

                storage.delete_by_namespace = crash
            result = {"removed": await backend.purge_namespace(NS)}
        print(json.dumps(result), flush=True)
        mark("done")
    finally:
        if watcher is not None:
            await watcher.stop()
        await backend.close()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
