"""MemoryBackend contract and the memtomem adapter boundary."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from typing import Any

import pytest

import syncmill.memory_backend as mb
from syncmill.config import MemoryConfig
from syncmill.memory import SyncmillMemory, run_async
from syncmill.memory_backend import (
    _DAY_FILE_DATE,
    BackendCapabilities,
    LangGraphBackend,
    Mem0Backend,
    MemoryBackend,
    MemtomemBackend,
    WriteBlockedError,
)

from conftest import FakeBackend, make_memory


class _FakeStorage:
    def __init__(self):
        self.meta: dict[str, dict] = {}
        self.counts: dict[str, int] = {}
        self.set_calls: list[tuple[str, str]] = []
        self.meta_upserts: list[dict] = []
        self.deleted: list[str] = []
        self.chunk_links: list[dict] = []
        self.link_raises = False
        self.sources_by_ns: dict[str, list] = {}
        self.stats: dict[str, Any] | None = None

    async def get_namespace_meta(self, ns: str):
        return self.meta.get(ns)

    async def set_namespace_meta(self, ns: str, description=None, color=None):
        self.meta[ns] = {"namespace": ns}
        self.set_calls.append((ns, description))
        self.meta_upserts.append({"ns": ns, "description": description, "color": color})

    async def add_chunk_link(self, source_id, target_id, link_type, namespace_target):
        if self.link_raises:
            raise RuntimeError("chunk_links table unavailable")
        self.chunk_links.append(
            {
                "source_id": str(source_id),
                "target_id": str(target_id),
                "link_type": link_type,
                "namespace_target": namespace_target,
            }
        )

    async def list_namespace_meta(self):
        names = set(self.meta) | set(self.counts)
        return [
            {"namespace": ns, "chunk_count": self.counts.get(ns, 0)} for ns in names
        ]

    async def delete_by_namespace(self, ns: str):
        self.deleted.append(ns)
        self.meta.pop(ns, None)
        return self.counts.pop(ns, 0)

    async def list_sources_by_namespace(self, ns: str):
        # Real memtomem knows which files a namespace's rows came from. A purge
        # unions this with the disk scan, because an indexer can be holding a
        # source that was already unlinked externally — disk alone would miss it.
        return list(self.sources_by_ns.get(ns, ()))

    async def get_stats(self):
        return self.stats


class _FakeIndexEngine:
    def __init__(self):
        self.index_calls: list[dict] = []
        # scriptable per-call new_chunk_ids; () simulates the hash-collision /
        # short-chunk-merge edge where memtomem yields no fresh id
        self.next_chunk_ids: tuple[str, ...] = ("aaaaaaaa-1111-4111-8111-111111111111",)
        # per-call ids, consumed first-in-first-out before next_chunk_ids
        # applies (add_and_share indexes two files with different results)
        self.chunk_id_queue: list[tuple[str, ...]] = []
        # shared ordering log the adapter fixture wires in (lock-span tests)
        self.events: list[str] | None = None
        # memtomem 0.6's source predicates; tests add paths to refuse them
        self.read_only: set = set()
        self.excluded: set = set()

    def is_read_only_source(self, file_path):
        return file_path in self.read_only

    def is_excluded(self, file_path):
        return file_path in self.excluded

    async def index_file(self, path, *, namespace, already_scanned, lock_held=False):
        self.index_calls.append(
            {
                "path": path,
                "namespace": namespace,
                "already_scanned": already_scanned,
                "lock_held": lock_held,
            }
        )
        if self.events is not None:
            self.events.append("index")
        ids = self.chunk_id_queue.pop(0) if self.chunk_id_queue else self.next_chunk_ids
        return SimpleNamespace(new_chunk_ids=ids, indexed_chunks=len(ids))


class _FakeSearchPipeline:
    def __init__(self):
        self.invalidate_calls = 0

    def invalidate_cache(self):
        self.invalidate_calls += 1


class _FakeIndexingConfig:
    """Mirrors memtomem's IndexingConfig where the adapter reads it.

    ``all_index_roots`` is upstream's user + project tiers in one list, and the
    adapter REQUIRES it — a double without it would prove only that the missing
    -method branch raises, never that the sweep covers both tiers.
    """

    def __init__(self):
        self.memory_dirs = ["/tmp/syncmill-mm-mem"]
        self.project_memory_dirs = []
        self.read_only_memory_dirs = []

    def all_index_roots(self):
        return [*self.memory_dirs, *self.project_memory_dirs, *self.read_only_memory_dirs]


class _FakeMemtomemStore:
    last: "_FakeMemtomemStore | None" = None

    def __init__(self, config_overrides=None):
        type(self).last = self
        self._config_overrides = config_overrides or {}
        self.storage = _FakeStorage()
        self.index_engine = _FakeIndexEngine()
        self.search_pipeline = _FakeSearchPipeline()
        self._components = SimpleNamespace(
            storage=self.storage,
            index_engine=self.index_engine,
            search_pipeline=self.search_pipeline,
            config=SimpleNamespace(
                indexing=_FakeIndexingConfig(),
                namespace=SimpleNamespace(default_namespace="default"),
            ),
        )
        self.ready_calls = 0
        self.added: list[dict] = []
        self.searches: list[dict] = []
        self.events: list[tuple[str, str]] = []
        self.ended: list[str | None] = []
        self.get_results: dict[str, dict] = {}
        self.closed = False

    async def _ensure_init(self):
        self.ready_calls += 1
        return self._components

    async def add(self, content, *, tags, namespace):
        self.added.append({"content": content, "tags": tags, "namespace": namespace})

    async def search(self, query, *, namespace, top_k, **kwargs):
        self.searches.append(
            {"query": query, "namespace": namespace, "top_k": top_k, **kwargs}
        )
        return [
            {
                "id": "id-1",
                "content": "hit",
                "score": 1.0,
                "namespace": namespace,
            }
        ]

    async def get(self, chunk_id):
        if chunk_id in self.get_results:
            return self.get_results[chunk_id]
        return {"id": chunk_id, "content": "hit", "namespace": "shared"}

    async def start_session(self, *, agent_id, namespace):
        return f"{agent_id}:{namespace}"

    async def log_event(self, kind, text):
        self.events.append((kind, text))

    async def end_session(self, *, summary=None):
        self.ended.append(summary)

    async def close(self):
        self.closed = True


class _FakeMatchValue:
    def __init__(self, *, value):
        self.value = value


class _FakeFieldCondition:
    def __init__(self, *, key, match):
        self.key = key
        self.match = match


class _FakeFilter:
    def __init__(self, *, must):
        self.must = must


class _FakeQdrantClient:
    def __init__(self, owner):
        self.owner = owner
        self.closed = False

    def count(self, *, collection_name, count_filter, exact):
        namespace = count_filter.must[0].match.value
        return SimpleNamespace(count=self.owner.counts.get(namespace, 0))

    def close(self):
        self.closed = True


class _FakeMem0Memory:
    last: "_FakeMem0Memory | None" = None

    def __init__(self, *, config):
        type(self).last = self
        self.config = config
        self.counts: dict[str, int] = {}
        self.added: list[dict] = []
        self.searches: list[dict] = []
        self.rows: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.delete_progress = True
        self.alter_add = False
        self.closed = False
        self.vector_store = SimpleNamespace(
            collection_name=config.data["vector_store"]["config"]["collection_name"]
        )
        self.vector_store.client = _FakeQdrantClient(self)
        self.llm = object()

    async def add(self, messages, **kwargs):
        self.added.append({"messages": messages, **kwargs})
        content = messages[0]["content"]
        namespace = kwargs["user_id"]
        self.counts[namespace] = self.counts.get(namespace, 0) + 1
        stored = f"altered:{content}" if self.alter_add else content
        return {"results": [{"id": "id-1", "memory": stored, "event": "ADD"}]}

    async def search(self, query, **kwargs):
        self.searches.append({"query": query, **kwargs})
        namespace = kwargs["filters"].get("user_id")
        return {
            "results": [
                {
                    "id": "id-1",
                    "memory": "hit",
                    "score": 0.8,
                    "user_id": namespace,
                    "metadata": {},
                }
            ]
        }

    async def get(self, chunk_id):
        return self.rows.get(chunk_id)

    async def delete_all(self, *, user_id=None, **kwargs):
        self.deleted.append(user_id)
        if self.delete_progress:
            self.counts[user_id] = max(0, self.counts.get(user_id, 0) - 100)
        return {"message": "Memories deleted successfully!"}

    def close(self):
        self.closed = True


class _FakeMem0Config:
    def __init__(self, **kwargs):
        self.data = kwargs


def _install_fake_mem0(monkeypatch):
    mem0 = ModuleType("mem0")
    mem0.AsyncMemory = _FakeMem0Memory
    configs = ModuleType("mem0.configs")
    base = ModuleType("mem0.configs.base")
    base.MemoryConfig = _FakeMem0Config
    qdrant = ModuleType("qdrant_client")
    models = ModuleType("qdrant_client.models")
    models.FieldCondition = _FakeFieldCondition
    models.Filter = _FakeFilter
    models.MatchValue = _FakeMatchValue
    monkeypatch.setitem(sys.modules, "mem0", mem0)
    monkeypatch.setitem(sys.modules, "mem0.configs", configs)
    monkeypatch.setitem(sys.modules, "mem0.configs.base", base)
    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant)
    monkeypatch.setitem(sys.modules, "qdrant_client.models", models)


class _FakeWriteGuard:
    """Stands in for memtomem.privacy.enforce_write_guard (patched at its real
    module attr — add_returning_ids imports it lazily at call time)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.block = False
        # any decision string, e.g. memtomem 0.6's "blocked_project_shared"
        self.decision: str | None = None

    def __call__(self, content, *, surface, audit_context=None, **kwargs):
        self.calls.append(
            {"content": content, "surface": surface, "audit_context": audit_context}
        )
        decision = self.decision or ("blocked" if self.block else "allowed")
        return SimpleNamespace(decision=decision, hits=())


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore", _FakeMemtomemStore
    )
    # add_returning_ids's lazy imports resolve these at call time, so patching
    # the real module attrs keeps the test hermetic (no privacy-audit side
    # effects, no markdown file written to the fake memory_dirs path).
    guard = _FakeWriteGuard()
    appended: list[dict] = []
    events: list[str] = []
    lock_paths: list = []
    # hooks run as a lock is acquired: what changes while a writer waits
    on_acquire: list = []
    mix_calls: list[dict] = []
    # scriptable refusal: tests set mix_result["value"] to a message string
    mix_result: dict = {"value": None}

    def fake_append_entry(file_path, content, title=None, tags=None):
        appended.append(
            {"path": file_path, "content": content, "title": title, "tags": tags}
        )
        events.append("append")

    class _FakeSidecarLock:
        def __init__(self, path, timeout=None):
            self.path = path

        async def __aenter__(self):
            events.append("lock:acquire")
            lock_paths.append(self.path)
            for hook in on_acquire:
                hook(self.path)

        async def __aexit__(self, *exc):
            events.append("lock:release")

    async def fake_mix_refusal(**kwargs):
        mix_calls.append(kwargs)
        events.append("mix")
        return mix_result["value"]

    monkeypatch.setattr("memtomem.privacy.enforce_write_guard", guard)
    monkeypatch.setattr("memtomem.tools.memory_writer.append_entry", fake_append_entry)
    monkeypatch.setattr(
        "memtomem.context._atomic.async_file_lock",
        lambda path, timeout=None: _FakeSidecarLock(path, timeout),
    )
    monkeypatch.setattr("memtomem.memory_scope.namespace_mix_refusal", fake_mix_refusal)
    backend = MemtomemBackend(MemoryConfig(db_path="/tmp/p1-memory.db"))
    store = _FakeMemtomemStore.last
    store.guard = guard
    store.appended = appended
    store.events = events
    store.lock_paths = lock_paths
    store.on_acquire = on_acquire
    store.index_engine.events = events
    store.mix_calls = mix_calls
    store.mix_result = mix_result
    return backend


@pytest.fixture
def mem0_adapter(monkeypatch, tmp_path):
    _install_fake_mem0(monkeypatch)
    return Mem0Backend(
        MemoryConfig(
            backend="mem0",
            mem0={"data_dir": str(tmp_path / "mem0")},
        )
    )


async def _protocol_member_smoke(backend: MemoryBackend) -> None:
    """Runtime call-sequence smoke over every member of the annotated Protocol."""

    assert backend.capabilities.verbatim_storage is True
    assert backend.agent_namespace_prefix
    assert backend.shared_namespace
    assert backend.validate_namespace("board-run:R1") is True
    await backend.ensure_ready()
    await backend.ensure_namespace("shared", description="Shared")
    await backend.add("body", tags=["tag"], namespace="shared")
    await backend.search("q", namespace="shared", top_k=1, tag_filter="tag")
    await backend.search_multi(
        "q", namespaces=["agent-runtime:codex", "shared"], top_k=2
    )
    chunk_ids = await backend.add_returning_ids(
        "body2", tags=["tag"], namespace="agent-runtime:codex"
    )
    for chunk_id in chunk_ids:
        await backend.share_chunk(chunk_id, target_namespace="shared")
    await backend.register_namespace(
        "agent-runtime:codex", description="Agent: codex", color="#123456"
    )
    await backend.get("id-1")
    await backend.count_namespace("shared")
    session_id = await backend.start_session("codex", "shared")
    if backend.capabilities.supports_sessions:
        assert session_id
    else:
        assert session_id is None
    await backend.log_event("kind", "text")
    await backend.end_session("done")
    assert await backend.status()
    await backend.delete_namespace("shared")
    await backend.close()


def test_protocol_member_smoke_fake_and_adapter(adapter):
    run_async(_protocol_member_smoke(FakeBackend()))
    run_async(_protocol_member_smoke(adapter))


def test_protocol_member_smoke_mem0(mem0_adapter):
    run_async(_protocol_member_smoke(mem0_adapter))


def test_capability_defaults_and_namespace_validation(adapter):
    caps = adapter.capabilities
    assert caps.supports_sessions is True
    assert caps.supports_tag_filter is True
    assert caps.namespace_delete_is_atomic is True
    assert caps.verbatim_storage is True
    expected_pattern = BackendCapabilities().namespace_pattern
    assert caps.namespace_pattern.pattern == expected_pattern.pattern
    assert caps.namespace_pattern.flags == expected_pattern.flags
    for valid in ("shared", "agent-runtime:codex", "board-run:R1", "a.b_c-d"):
        assert adapter.validate_namespace(valid)
    for invalid in ("", ":bad", "bad:", "bad::segment", "has space", "slash/x"):
        assert not adapter.validate_namespace(invalid)


def test_adapter_core_passthrough_omits_none_tag_filter(adapter):
    store = _FakeMemtomemStore.last
    run_async(adapter.add("body", tags=["one"], namespace="shared"))
    run_async(adapter.search("a", namespace="shared", top_k=2))
    run_async(adapter.search("b", namespace="shared", top_k=3, tag_filter="one,two"))

    # add replicates the write flow itself (per-namespace file) rather than
    # delegating to MemtomemStore.add's shared daily file.
    assert store.added == []
    assert store.appended[0] == {
        **store.appended[0],
        "content": "body",
        "tags": ["one"],
    }
    assert store.searches == [
        {"query": "a", "namespace": "shared", "top_k": 2},
        {
            "query": "b",
            "namespace": "shared",
            "top_k": 3,
            "tag_filter": "one,two",
        },
    ]


def test_adapter_search_multi_joins_namespaces_into_one_native_call(adapter):
    store = _FakeMemtomemStore.last
    run_async(
        adapter.search_multi(
            "q", namespaces=["agent-runtime:claude", "shared"], top_k=5
        )
    )
    run_async(
        adapter.search_multi("q2", namespaces=["shared"], top_k=3, tag_filter="one")
    )
    # ONE store.search per call, namespace = the comma union filter memtomem's
    # NamespaceFilter.parse understands (the mem_agent_search shape).
    assert store.searches == [
        {"query": "q", "namespace": "agent-runtime:claude,shared", "top_k": 5},
        {"query": "q2", "namespace": "shared", "top_k": 3, "tag_filter": "one"},
    ]


def test_adapter_search_multi_rejects_a_comma_bearing_namespace(adapter):
    # The comma is the union-filter separator; a namespace containing one
    # (e.g. built from an unvalidated agent id) would silently widen the
    # search to namespaces the caller never asked for.
    store = _FakeMemtomemStore.last
    before = list(store.searches)
    with pytest.raises(ValueError, match="comma"):
        run_async(adapter.search_multi("q", namespaces=["shared", "a,b"], top_k=5))
    assert store.searches == before  # nothing reached the store


def test_adapter_add_returning_ids_replicates_add_flow_and_keeps_ids(adapter):
    store = _FakeMemtomemStore.last
    chunk_ids = run_async(
        adapter.add_returning_ids("body", tags=["t1"], namespace="agent-runtime:claude")
    )
    assert chunk_ids == ["aaaaaaaa-1111-4111-8111-111111111111"]
    # guard ran with the syncmill surface, append_entry got the content+tags,
    # index_file was namespace-scoped and already_scanned (the guarded path).
    assert store.guard.calls[0]["surface"] == "syncmill_add"
    assert store.guard.calls[0]["audit_context"] == {
        "namespace": "agent-runtime:claude"
    }
    assert store.appended[0]["content"] == "body"
    assert store.appended[0]["tags"] == ["t1"]
    call = store.index_engine.index_calls[0]
    assert call["namespace"] == "agent-runtime:claude"
    assert call["already_scanned"] is True
    assert call["path"] == store.appended[0]["path"]
    # namespace-suffixed daily file: same-content chunks in ONE file collapse
    # to the last write on reindex (default memtomem config), which would
    # delete the two-step share's private original — so each namespace gets
    # its own file.
    assert str(call["path"]).endswith(".agent-runtime.claude.md")
    # MCP-write parity: the TTL search cache was invalidated after the write
    assert store.search_pipeline.invalidate_calls == 1


def test_adapter_add_writes_a_unique_heading_per_entry(adapter):
    """append_entry's default heading is second-resolution, and two entries
    under ONE heading re-chunk into a single chunk — swallowing the earlier
    entry's text and re-minting the id already returned to the caller, which
    then makes the provenance share look up a chunk that no longer exists.
    Every write therefore supplies its own heading."""
    store = _FakeMemtomemStore.last
    for i in range(3):
        run_async(adapter.add(f"body {i}", tags=[], namespace="shared"))
    titles = [entry["title"] for entry in store.appended]
    assert all(titles), "no explicit heading — append_entry would key on the second"
    assert len(set(titles)) == 3
    # still a plain `## <title>` heading: content-shaped headings are all the
    # chunker requires, and the `> created:` line keeps its own resolution.
    assert all(t.startswith("Entry ") and "\n" not in t for t in titles)


def test_adapter_add_invalidates_search_cache(adapter):
    # the plain add path (fallbacks, board writes) gets the same MCP-write
    # parity — a just-shared result must be searchable within the cache TTL.
    store = _FakeMemtomemStore.last
    run_async(adapter.add("body", tags=[], namespace="shared"))
    assert store.search_pipeline.invalidate_calls == 1


def test_adapter_write_holds_the_sidecar_lock_across_append_and_index(adapter):
    """memtomem 0.4's write-locking contract: the mix-guard read, the append,
    and the re-index share ONE cross-process sidecar-lock span, and index_file
    is told the lock is already held (the nested acquire would self-deadlock,
    memtomem #1587). Without the span the watcher can index the appended file
    FIRST — without the explicit namespace — stranding the agent-private chunk
    in `default` (reproduced against the real 0.4 store)."""
    store = _FakeMemtomemStore.last
    ids = run_async(
        adapter.add_returning_ids("body", tags=["t"], namespace="agent-runtime:codex")
    )
    assert ids
    # Two nested spans now: syncmill's namespace lock (taken first, before the
    # target is derived, so a purge cannot interleave — see
    # `_namespace_lock_path`) and INSIDE it memtomem's per-file sidecar, which
    # must still wrap mix + append + index exactly.
    assert store.events == [
        "lock:acquire",
        "lock:acquire",
        "mix",
        "append",
        "index",
        "lock:release",
        "lock:release",
    ]
    assert store.index_engine.index_calls[0]["lock_held"] is True
    # the mix guard judged the very file/namespace the write targets
    mix = store.mix_calls[0]
    assert mix["target"] == store.index_engine.index_calls[0]["path"]
    assert mix["effective_ns"] == "agent-runtime:codex"


def test_adapter_write_mix_refusal_aborts_before_the_append(adapter):
    # A refusal must abort the write entirely (refusing beats letting a
    # re-chunk restamp the file's stored rows) — and still release the lock.
    store = _FakeMemtomemStore.last
    store.mix_result["value"] = "stored rows disagree"
    with pytest.raises(RuntimeError, match="namespace mix refused"):
        run_async(adapter.add_returning_ids("body", tags=[], namespace="shared"))
    assert store.appended == []
    assert store.index_engine.index_calls == []
    # Both spans unwind on the refusal — the namespace lock as well as the
    # per-file sidecar, or a refused write would strand a purge forever.
    assert store.events == [
        "lock:acquire",
        "lock:acquire",
        "mix",
        "lock:release",
        "lock:release",
    ]


def test_adapter_add_uses_the_namespace_suffixed_file(adapter):
    # add must land in the SAME per-namespace file add_returning_ids uses. The
    # shared `{date}.md` breaks isolation: row-only deletes leave the text on
    # disk, so a later write to that file re-indexes a GC'd board-run chunk and
    # stamps it with the writing namespace.
    store = _FakeMemtomemStore.last
    run_async(adapter.add("body", tags=["t"], namespace="board-run:r1"))
    call = store.index_engine.index_calls[0]
    assert str(call["path"]).endswith(".board-run.r1.md")
    assert call["namespace"] == "board-run:r1"
    assert store.appended[0]["content"] == "body"


def test_adapter_add_two_namespaces_write_separate_files(adapter):
    # Same content, two namespaces: one shared file would content-hash dedup the
    # second write into a silent no-op.
    store = _FakeMemtomemStore.last
    run_async(adapter.add("same body", tags=[], namespace="board-run:r1"))
    run_async(adapter.add("same body", tags=[], namespace="shared"))
    paths = [str(c["path"]) for c in store.index_engine.index_calls]
    assert len(set(paths)) == 2
    assert paths[0].endswith(".board-run.r1.md") and paths[1].endswith(".shared.md")


def _point_index_roots(monkeypatch, user, *project):
    """Aim both the user tier and the configured index roots at real dirs.

    The sweep verifies every root, so a double still pointing at a path that
    does not exist would raise before reaching what the test is about.
    """
    monkeypatch.setattr(
        "memtomem.memory_scope.require_user_base", lambda dirs, project_dirs, **kw: user
    )
    indexing = _FakeMemtomemStore.last._components.config.indexing
    indexing.memory_dirs = [user]
    indexing.project_memory_dirs = list(project)


def test_delete_namespace_sources_removes_only_that_namespaces_files(
    adapter, tmp_path, monkeypatch
):
    # Row deletes leave the markdown, which is what the index is rebuilt FROM —
    # so a deleted namespace returns on the next reindex unless its source files
    # go too. The per-namespace suffix is what makes this safe to glob.
    from memtomem.memory_scope import day_file_name

    _point_index_roots(monkeypatch, tmp_path)
    mine = tmp_path / "2026-08-01.board-run.r1.md"
    # memtomem 0.4 writes its own per-namespace day files under a different
    # name; ask the real helper for it so an upstream naming change breaks this
    # test rather than silently stranding files the sweep can no longer match.
    mine_native = tmp_path / day_file_name(
        "board-run:r1", "default", date_str="2026-08-01"
    )
    other_native = tmp_path / day_file_name(
        "board-run:r2", "default", date_str="2026-08-01"
    )
    other = tmp_path / "2026-08-01.shared.md"
    default_day = tmp_path / "2026-08-01.md"
    for path in (mine, mine_native, other, other_native, default_day):
        path.write_text("x")

    assert run_async(adapter.delete_namespace_sources("board-run:r1")) == 2
    assert not mine.exists()
    assert not mine_native.exists()
    assert other.exists()
    assert other_native.exists()
    assert default_day.exists()


def test_delete_namespace_sources_spares_files_that_merely_end_in_the_suffix(
    adapter, tmp_path, monkeypatch
):
    # The digest proves WHICH namespace a day file belongs to; it proves neither
    # that the prefix is a date nor that syncmill wrote the file. A bare
    # `*{suffix}` glob accepts any prefix, so a user's own notes or a backup
    # copy sitting in the memory dir would be unlinked at a destructive
    # boundary. Only a whole {date} + suffix is a day file.
    from memtomem.memory_scope import day_file_name

    _point_index_roots(monkeypatch, tmp_path)
    native = day_file_name("board-run:r1", "default", date_str="2026-08-01")
    bystanders = [
        tmp_path / "notes.board-run.r1.md",
        tmp_path / f"backup{native[len('2026-08-01') :]}",
        tmp_path / "2026-8-1.board-run.r1.md",  # not a zero-padded date
        tmp_path / "x2026-08-01.board-run.r1.md",
    ]
    doomed = tmp_path / "2026-08-01.board-run.r1.md"
    for path in [*bystanders, doomed]:
        path.write_text("x")

    assert run_async(adapter.delete_namespace_sources("board-run:r1")) == 1
    assert not doomed.exists()
    assert [p for p in bystanders if p.exists()] == bystanders


def test_delete_namespace_sources_never_lets_a_namespace_widen_the_sweep(
    adapter, tmp_path, monkeypatch
):
    # ns reaches this destructive boundary from the caller, and a glob
    # metacharacter in it would silently widen the sweep — `board-run:*` as a
    # pattern takes every board run's sources. Nothing is interpolated into a
    # pattern, so the wildcard can only ever match itself.
    _point_index_roots(monkeypatch, tmp_path)
    others = [
        tmp_path / "2026-08-01.board-run.r1.md",
        tmp_path / "2026-08-01.board-run.r2.md",
    ]
    for path in others:
        path.write_text("x")

    assert run_async(adapter.delete_namespace_sources("board-run:*")) == 0
    assert all(path.exists() for path in others)


def test_delete_namespace_sources_sweeps_registered_project_roots(
    adapter, tmp_path, monkeypatch
):
    # A file under a registered project root is re-indexed exactly like a
    # user-tier one, so leaving it behind resurrects the namespace the caller
    # was just told had been purged — the original bug, one tier over.
    user = tmp_path / "user"
    project = tmp_path / "project"
    for directory in (user, project):
        directory.mkdir()
    _point_index_roots(monkeypatch, user, project)
    mine = [
        user / "2026-08-01.board-run.r1.md",
        project / "2026-08-01.board-run.r1.md",
    ]
    other = project / "2026-08-01.shared.md"
    for path in [*mine, other]:
        path.write_text("x")

    assert run_async(adapter.delete_namespace_sources("board-run:r1")) == 2
    assert not any(path.exists() for path in mine)
    assert other.exists()


def test_delete_namespace_sources_raises_on_an_unverifiable_root(
    adapter, tmp_path, monkeypatch
):
    # A configured root that is merely unreachable right now — an unmounted
    # project tier — must not be skipped. Skipping reports a clean zero-file
    # purge over sources still on disk, the caller tombstones the namespace,
    # and the root coming back resurrects it. Failing keeps it collectable.
    user = tmp_path / "user"
    user.mkdir()
    _point_index_roots(monkeypatch, user, tmp_path / "unmounted")
    survivor = user / "2026-08-01.board-run.r1.md"
    survivor.write_text("x")

    with pytest.raises(RuntimeError, match="readable directory"):
        run_async(adapter.delete_namespace_sources("board-run:r1"))
    assert survivor.exists()


def test_delete_namespace_sources_raises_when_index_roots_are_unknowable(
    adapter, tmp_path, monkeypatch
):
    # Version skew below the method the sweep needs is the same problem: it
    # cannot prove it saw every root, so it must not report a purge.
    _point_index_roots(monkeypatch, tmp_path)
    indexing = _FakeMemtomemStore.last._components.config.indexing
    monkeypatch.delattr(type(indexing), "all_index_roots")
    survivor = tmp_path / "2026-08-01.board-run.r1.md"
    survivor.write_text("x")

    with pytest.raises(RuntimeError, match="all_index_roots"):
        run_async(adapter.delete_namespace_sources("board-run:r1"))
    assert survivor.exists()


def test_delete_namespace_sources_raises_when_a_root_cannot_be_scanned(
    adapter, tmp_path, monkeypatch
):
    # pathlib's glob SWALLOWS a scan-time OSError and yields nothing, so an
    # unreadable root would look like an empty one — a clean purge reported over
    # files never looked at, which the caller then tombstones. The os.access
    # preflight cannot settle this alone: it can disagree with the real
    # operation under ACLs, and permissions can change between check and scan.
    _point_index_roots(monkeypatch, tmp_path)
    survivor = tmp_path / "2026-08-01.board-run.r1.md"
    survivor.write_text("x")

    def _boom(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(mb.os, "scandir", _boom)
    with pytest.raises(PermissionError):
        run_async(adapter.delete_namespace_sources("board-run:r1"))
    assert survivor.exists()


def test_delete_namespace_sources_requires_a_date_a_writer_could_emit(
    adapter, tmp_path, monkeypatch
):
    # Every name the sweep accepts is one syncmill or memtomem wrote, and both
    # date it with strftime("%Y-%m-%d"). A shape-only check would also accept
    # dates no writer can produce — non-ASCII digits (`\d` matches them) and
    # impossible calendar days — i.e. somebody else's file that happens to end
    # the same way.
    _point_index_roots(monkeypatch, tmp_path)
    bystanders = [
        tmp_path / "２０２６-０８-０１.board-run.r1.md",
        tmp_path / "2026-99-99.board-run.r1.md",
        tmp_path / "2026-02-30.board-run.r1.md",
    ]
    doomed = tmp_path / "2026-02-28.board-run.r1.md"
    for path in [*bystanders, doomed]:
        path.write_text("x")

    assert run_async(adapter.delete_namespace_sources("board-run:r1")) == 1
    assert not doomed.exists()
    assert [p for p in bystanders if p.exists()] == bystanders


def test_delete_namespace_sources_default_namespace_never_wild_matches(
    adapter, tmp_path, monkeypatch
):
    # day_file_name gives the DEFAULT namespace the bare "{date}.md" shape, so
    # building a native pattern from it yields "*.md" — a glob that would take
    # every source file in the store. Nothing passes the default today, which is
    # exactly why the guard has to be structural rather than trusted to callers.
    _point_index_roots(monkeypatch, tmp_path)
    default_day = tmp_path / "2026-08-01.md"
    shared = tmp_path / "2026-08-01.shared.md"
    for path in (default_day, shared):
        path.write_text("x")

    assert run_async(adapter.delete_namespace_sources("default")) == 0
    assert default_day.exists()
    assert shared.exists()


def test_delete_namespace_sources_raises_rather_than_reporting_zero(
    adapter, monkeypatch
):
    # The caller tombstones a purged namespace. A namespace whose source file is
    # still on disk has NOT been purged (its rows are gone, so a reindex reads
    # that text as new content), so the failure has to be visible — a quiet zero
    # would retire it from collection forever.
    def _boom(dirs, project_dirs, **kw):
        raise RuntimeError("no memory dir configured")

    monkeypatch.setattr("memtomem.memory_scope.require_user_base", _boom)
    with pytest.raises(RuntimeError):
        run_async(adapter.delete_namespace_sources("board-run:r1"))


def test_purge_namespace_drops_sources_then_rows_inside_one_lock_span(
    adapter, tmp_path, monkeypatch
):
    # The whole point of the operation: both halves happen with the writers
    # locked out, so nothing can land a file-plus-row pair between them.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    store.events.clear()
    (tmp_path / "2026-08-01.board-run.r1.md").write_text("x")
    # The shared ordering log is what makes the span observable, so the row
    # delete and the sweeps have to appear in it too — asserting only on the
    # lock events would pass with the delete moved outside the span entirely.
    real_delete = store.storage.delete_by_namespace

    async def logging_delete(ns):
        store.events.append("delete_rows")
        return await real_delete(ns)

    monkeypatch.setattr(store.storage, "delete_by_namespace", logging_delete)
    real_scan = mb._namespace_source_paths

    def logging_scan(config, ns):
        store.events.append("scan")
        return real_scan(config, ns)

    monkeypatch.setattr(mb, "_namespace_source_paths", logging_scan)
    real_unlink = os.unlink

    def logging_unlink(path):
        store.events.append("unlink")
        return real_unlink(path)

    monkeypatch.setattr(mb.os, "unlink", logging_unlink)

    assert run_async(adapter.purge_namespace("board-run:r1")) == 1
    assert store.storage.deleted == ["board-run:r1"]

    events = store.events
    # Two levels: the namespace lock (writers) and the existing source's own
    # sidecar (an in-flight indexer, which takes that rather than the namespace
    # lock and could otherwise commit rows after the purge reported success).
    assert events.count("lock:acquire") == events.count("lock:release") == 2
    # The unlink, the row delete and the verify scan ALL sit between the last
    # acquire and the first release — that is what "atomic against a syncmill
    # writer" means. Asserting only on the lock events would pass with the
    # delete moved outside the span entirely.
    last_acquire = max(i for i, e in enumerate(events) if e == "lock:acquire")
    first_release = min(i for i, e in enumerate(events) if e == "lock:release")
    inside = [e for e in events[last_acquire:first_release] if e != "lock:acquire"]
    # The leading scan is the re-snapshot AFTER the last lock was taken: the
    # candidate set can grow while locks are being acquired, so the loop only
    # proceeds once a pass adds nothing new.
    assert inside == ["scan", "unlink", "delete_rows", "scan"]
    # Sources go before rows: rows are what keep a surviving file harmless.
    assert events.index("unlink") < events.index("delete_rows")


def test_purge_locks_the_sidecar_of_every_existing_source(
    adapter, tmp_path, monkeypatch
):
    # memtomem's watcher and manual indexers hold a FILE's sidecar, not the
    # namespace lock, so a purge that skipped them could unlink and delete
    # under an indexer that then commits its buffered rows.
    _point_index_roots(monkeypatch, tmp_path)
    taken: list[Path] = []

    class _RecordingLock:
        def __init__(self, path, timeout=None):
            taken.append(Path(path))

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(
        "memtomem.context._atomic.async_file_lock",
        lambda path, timeout=None: _RecordingLock(path, timeout),
    )
    sources = [
        tmp_path / "2026-08-01.board-run.r1.md",
        tmp_path / "2026-08-02.board-run.r1.md",
    ]
    for path in sources:
        path.write_text("x")
    (tmp_path / "2026-08-01.shared.md").write_text("other")

    run_async(adapter.purge_namespace("board-run:r1"))

    names = [p.name for p in taken]
    for path in sources:
        assert f".{path.name}.lock" in names
    # The namespace lock leads; the per-file ones nest inside it.
    assert names[0] == mb._namespace_lock_path(tmp_path, "board-run:r1").name
    # Another namespace's file is untouched — this locks what it will delete.
    assert ".2026-08-01.shared.md.lock" not in names
    # Sorted, per upstream's L2 multi-acquire rule.
    file_locks = [str(p) for p in taken[1:]]
    assert file_locks == sorted(file_locks)


def test_purge_and_write_take_the_same_date_free_namespace_lock(
    adapter, tmp_path, monkeypatch
):
    # The exclusion has to be on the NAMESPACE, not on a file name. The writer
    # derives its target only AFTER taking this lock, so a writer suspended
    # between deriving and appending — which is what makes any date-based lock
    # set unsound, since a stale target can be arbitrarily old — still cannot
    # interleave with a purge.
    _point_index_roots(monkeypatch, tmp_path)
    taken: list[Path] = []

    class _RecordingLock:
        def __init__(self, path, timeout=None):
            taken.append(Path(path))

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(
        "memtomem.context._atomic.async_file_lock",
        lambda path, timeout=None: _RecordingLock(path, timeout),
    )
    run_async(adapter.purge_namespace("board-run:r1"))
    purge_locks = list(taken)

    taken.clear()
    run_async(adapter.add_returning_ids("body", tags=[], namespace="board-run:r1"))
    write_locks = list(taken)

    expected = mb._namespace_lock_path(tmp_path, "board-run:r1")
    assert expected in purge_locks, "purge did not take the namespace lock"
    assert expected in write_locks, "write did not take the namespace lock"
    # And the writer takes it FIRST — before the per-file sidecar it can only
    # name after reading the clock.
    assert write_locks[0] == expected
    # Nothing in the lock's identity is a date, so no rollover argument is
    # needed for the guarantee to hold.
    assert not _DAY_FILE_DATE.search(expected.name)


def test_namespace_lock_is_not_mistaken_for_a_source_file(tmp_path):
    # It lives beside the day files, so the destructive sweep must never treat
    # it as one.
    lock = mb._namespace_lock_path(tmp_path, "board-run:r1")
    for suffix in (".board-run.r1.md", ".md"):
        assert not mb._is_day_file(lock.name, suffix)


def test_purge_namespace_raises_when_a_writer_it_cannot_exclude_lands_a_file(
    adapter, tmp_path, monkeypatch
):
    # A memtomem-NATIVE write to the same namespace is the one interleaving the
    # locks do not cover — not because its file name is unknowable
    # (`day_file_name` is deterministic, and the digest is over the NAMESPACE,
    # not the date) but because it does not take syncmill's namespace lock and
    # upstream has none to share. The verify scan is best-effort detection of
    # it, and an unprovable purge must raise rather than report a count — the
    # caller tombstones on success, and a namespace with a live producer must
    # stay collectable.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    real_delete = store.storage.delete_by_namespace

    async def landing_write(ns):
        # The native writer lands its file while the rows are being deleted —
        # after every lock this purge could have taken.
        (tmp_path / "2026-08-02.board-run.r1.md").write_text("late")
        return await real_delete(ns)

    monkeypatch.setattr(store.storage, "delete_by_namespace", landing_write)
    with pytest.raises(RuntimeError, match="not provably complete"):
        run_async(adapter.purge_namespace("board-run:r1"))
    # The rows still went: leaving them would strand markdown with no rows, the
    # exact leak this path prevents. The namespace simply stays collectable.
    assert store.storage.deleted == ["board-run:r1"]
    # And the file it could not lock was NOT unlinked — mutating a path this
    # purge never held is precisely what the locking exists to stop.
    assert (tmp_path / "2026-08-02.board-run.r1.md").exists()


def test_purge_namespace_unions_db_known_sources_with_the_disk_scan(
    adapter, tmp_path, monkeypatch
):
    # An indexer can be holding a source that was ALREADY unlinked externally:
    # disk sees nothing, so a disk-only snapshot locks nothing, deletes the
    # rows and returns — and the indexer then commits its buffered chunks into
    # a namespace the caller was told is gone. The DB still attributes that
    # source to the namespace, so the candidate set is the union.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    gone = tmp_path / "2026-08-01.board-run.r1.md"  # deliberately never created
    store.storage.sources_by_ns["board-run:r1"] = [gone]
    taken: list[Path] = []

    class _RecordingLock:
        def __init__(self, path, timeout=None):
            taken.append(Path(path))

        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr(
        "memtomem.context._atomic.async_file_lock",
        lambda path, timeout=None: _RecordingLock(path, timeout),
    )
    # No file on disk, so nothing to unlink — but the lock must still be taken.
    assert run_async(adapter.purge_namespace("board-run:r1")) == 0
    assert f".{gone.name}.lock" in [p.name for p in taken]


def test_purge_namespace_refuses_when_a_db_path_is_not_its_day_file(
    adapter, tmp_path, monkeypatch
):
    # The DB half of the candidate set is DATA, not a promise. The pre-upgrade
    # residue is a namespace-SHARED `{date}.md` holding several namespaces'
    # entries, which this store deliberately never deletes; a row attributing
    # board-run chunks to it must not turn the purge into a delete of shared
    # knowledge. But silently SKIPPING it is not safe either — it may be exactly
    # the source an in-flight indexer holds, and ignoring it is how rows come
    # back after an acknowledged purge. So it is unprovable: nothing is removed.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    shared_day = tmp_path / "2026-08-01.md"  # the pre-upgrade shared shape
    shared_day.write_text("other namespaces live here too")
    mine = tmp_path / "2026-08-02.board-run.r1.md"
    mine.write_text("x")
    store.storage.sources_by_ns["board-run:r1"] = [shared_day, mine]

    with pytest.raises(mb.UnprovablePurgeError, match="not a day file"):
        run_async(adapter.purge_namespace("board-run:r1"))
    assert shared_day.exists(), "purge deleted the shared pre-upgrade day file"
    assert mine.exists(), "purge removed sources despite being unprovable"
    assert store.storage.deleted == []


def test_purge_namespace_refuses_a_db_path_outside_every_index_root(
    adapter, tmp_path, monkeypatch
):
    # Right name, wrong place: still not this purge's to touch, and still
    # unprovable rather than quietly ignored.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stray = outside / "2026-08-01.board-run.r1.md"
    stray.write_text("not in an index root")
    store.storage.sources_by_ns["board-run:r1"] = [stray]

    with pytest.raises(mb.UnprovablePurgeError):
        run_async(adapter.purge_namespace("board-run:r1"))
    assert stray.exists()


def test_purge_namespace_refuses_a_symlinked_source(adapter, tmp_path, monkeypatch):
    # memtomem RESOLVES a path before deriving its sidecar, so a link and its
    # target have different lock identities: the purge would take `.link.lock`
    # while an indexer holds `.target.lock`, exclude nothing, and still report
    # success. Refusing beats mirroring upstream's resolution here.
    _point_index_roots(monkeypatch, tmp_path)
    target = tmp_path / "real-content.md"
    target.write_text("x")
    link = tmp_path / "2026-08-01.board-run.r1.md"
    link.symlink_to(target)

    with pytest.raises(mb.UnprovablePurgeError, match="symlink"):
        run_async(adapter.purge_namespace("board-run:r1"))
    assert link.exists() and target.exists()
    assert _FakeMemtomemStore.last.storage.deleted == []


def _today_target(base, ns):
    return base / f"{mb._utc_date_str()}.{ns.replace(':', '.')}.md"


def test_adapter_write_keys_the_file_lock_on_the_resolved_path(
    adapter, tmp_path, monkeypatch
):
    # memtomem's indexer keys a memory file's L2 sidecar on the RESOLVED path
    # (`memory_lock_path`); a writer keying it any other way takes a different
    # lock for the same file and excludes nothing (upstream #2130). A base
    # reached through a symlinked directory is where the two keys differ.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    _point_index_roots(monkeypatch, link)
    store = _FakeMemtomemStore.last

    run_async(adapter.add_returning_ids("body", tags=["t"], namespace="shared"))

    atomic = mb._get_memtomem_atomic()
    target = _today_target(link, "shared")
    assert store.lock_paths[1] == atomic.memory_lock_path(target)
    assert atomic.memory_lock_path(target) != atomic._lock_path_for(target)


def test_adapter_write_refuses_a_symlinked_day_file(adapter, tmp_path, monkeypatch):
    # Following it could land an unattended append in a tier the base check
    # never saw (a git-tracked project tier, say); syncmill never makes one.
    _point_index_roots(monkeypatch, tmp_path)
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("")
    _today_target(tmp_path, "shared").symlink_to(elsewhere)
    store = _FakeMemtomemStore.last

    with pytest.raises(RuntimeError, match="symlinked"):
        run_async(adapter.add_returning_ids("body", tags=["t"], namespace="shared"))
    assert store.appended == []
    assert elsewhere.read_text() == ""


@pytest.mark.parametrize("predicate", ["read_only", "excluded"])
def test_adapter_write_refuses_a_target_memtomem_would_not_take(
    adapter, tmp_path, monkeypatch, predicate
):
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    getattr(store.index_engine, predicate).add(_today_target(tmp_path, "shared"))

    with pytest.raises(RuntimeError, match=predicate.replace("_", "-")):
        run_async(adapter.add_returning_ids("body", tags=["t"], namespace="shared"))
    assert store.appended == []
    assert store.index_engine.index_calls == []


@pytest.mark.parametrize("change", ["read_only", "excluded", "symlink"])
def test_adapter_write_rechecks_the_target_once_the_file_lock_is_held(
    adapter, tmp_path, monkeypatch, change
):
    # The pre-lock checks only keep a sidecar out of the wrong place; the
    # target can change while the writer waits, so the in-lock check is the
    # authoritative one. Change it DURING the file-lock acquisition.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    target = _today_target(tmp_path, "shared")
    atomic = mb._get_memtomem_atomic()

    def flip(path):
        if path != atomic.memory_lock_path(target):
            return  # the namespace lock
        if change == "symlink":
            elsewhere = tmp_path / "elsewhere.md"
            elsewhere.write_text("")
            target.symlink_to(elsewhere)
        else:
            getattr(store.index_engine, change).add(target)

    store.on_acquire.append(flip)
    with pytest.raises(RuntimeError, match="refusing"):
        run_async(adapter.add_returning_ids("body", tags=["t"], namespace="shared"))
    assert store.appended == []
    assert store.index_engine.index_calls == []


def test_adapter_write_refuses_every_blocking_guard_decision(adapter):
    store = _FakeMemtomemStore.last
    store.guard.decision = "blocked_project_shared"
    with pytest.raises(WriteBlockedError, match="blocked_project_shared"):
        run_async(adapter.add_returning_ids("body", tags=["t"], namespace="shared"))
    assert store.appended == []


def test_user_base_refuses_a_registered_project_tier(tmp_path):
    # syncmill writes unattended and has no consent gate for a git-tracked
    # tier, so a base that IS one is refused rather than written into.
    project = tmp_path / "repo" / ".memtomem" / "memories"
    project.mkdir(parents=True)
    config = SimpleNamespace(
        indexing=SimpleNamespace(memory_dirs=[project], project_memory_dirs=[project])
    )
    with pytest.raises(Exception, match="project"):
        mb._user_base(config)


def test_purge_namespace_refuses_a_source_in_a_read_only_root(
    adapter, tmp_path, monkeypatch
):
    # memtomem 0.6 counts read-only roots among the index roots; a matching
    # day file there is indexed but must never be rewritten — or deleted.
    user = tmp_path / "user"
    read_only = tmp_path / "read-only"
    user.mkdir()
    read_only.mkdir()
    _point_index_roots(monkeypatch, user)
    _FakeMemtomemStore.last._components.config.indexing.read_only_memory_dirs = [read_only]
    protected = read_only / "2026-08-01.board-run.r1.md"
    protected.write_text("x")
    ours = user / "2026-08-01.board-run.r1.md"
    ours.write_text("y")

    with pytest.raises(mb.UnprovablePurgeError, match="read-only"):
        run_async(adapter.purge_namespace("board-run:r1"))
    # refused BEFORE anything was deleted: a partial purge would leave the
    # namespace half-gone with its rows intact
    assert protected.exists() and ours.exists()
    assert _FakeMemtomemStore.last.storage.deleted == []


def test_purge_namespace_refuses_a_project_shared_source(adapter, tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    shared_tier = tmp_path / "repo" / ".memtomem" / "memories"
    shared_tier.mkdir(parents=True)
    _point_index_roots(monkeypatch, user, shared_tier)
    tracked = shared_tier / "2026-08-01.board-run.r1.md"
    tracked.write_text("x")

    with pytest.raises(mb.UnprovablePurgeError, match="project_shared"):
        run_async(adapter.purge_namespace("board-run:r1"))
    assert tracked.exists()
    assert _FakeMemtomemStore.last.storage.deleted == []


def test_purge_namespace_still_removes_a_project_local_source(
    adapter, tmp_path, monkeypatch
):
    # Only the git-tracked tier needs consent; project_local is per-machine
    # and stays collectable, exactly as before.
    user = tmp_path / "user"
    user.mkdir()
    local_tier = tmp_path / "repo" / ".memtomem" / "memories.local"
    local_tier.mkdir(parents=True)
    _point_index_roots(monkeypatch, user, local_tier)
    scratch = local_tier / "2026-08-01.board-run.r1.md"
    scratch.write_text("x")

    assert run_async(adapter.purge_namespace("board-run:r1")) == 1
    assert not scratch.exists()


def test_purge_namespace_propagates_an_unverifiable_root(
    adapter, tmp_path, monkeypatch
):
    # Turning root verification into "skip this path" would be fail-OPEN at the
    # operation level: a transient failure could drop exactly the externally
    # unlinked source an in-flight indexer is holding, and the purge would then
    # delete its rows and report success.
    #
    # The disk scan is stubbed so that ITS own root check cannot be what raises
    # — otherwise this passes on pre-existing behavior and says nothing about
    # the purge's own verification.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    known = tmp_path / "2026-08-01.board-run.r1.md"
    known.write_text("x")
    store.storage.sources_by_ns["board-run:r1"] = [known]
    monkeypatch.setattr(mb, "_namespace_source_paths", lambda config, ns: [])

    def _boom(config, base):
        raise RuntimeError("project tier went away")

    monkeypatch.setattr(mb, "_index_roots", _boom)
    with pytest.raises(RuntimeError, match="project tier went away"):
        run_async(adapter.purge_namespace("board-run:r1"))
    assert store.storage.deleted == []
    assert known.exists()


def test_purge_namespace_refuses_to_hold_an_unbounded_number_of_locks(
    adapter, tmp_path, monkeypatch
):
    # Every source lock is an open descriptor held for the whole purge, so an
    # unbounded namespace could exhaust the process limit mid-purge with the
    # board GC waiting on it. Refusing loudly leaves it collectable.
    _point_index_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(mb, "_MAX_PURGE_SOURCE_LOCKS", 2)
    for day in range(1, 5):
        (tmp_path / f"2026-08-0{day}.board-run.r1.md").write_text("x")
    with pytest.raises(RuntimeError, match="source files"):
        run_async(adapter.purge_namespace("board-run:r1"))
    # Nothing was unlinked — it refused before touching anything.
    assert len(list(tmp_path.glob("*.board-run.r1.md"))) == 4


def test_purge_namespace_fails_closed_when_the_local_cache_cannot_be_cleared(
    adapter, tmp_path, monkeypatch
):
    # The caller TOMBSTONES on success, so a namespace whose cached rows this
    # process may still serve must not be reported as purged. (Another
    # process's cache is beyond reach — the guarantee is scoped to this one.)
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    (tmp_path / "2026-08-01.board-run.r1.md").write_text("x")

    def _boom():
        raise RuntimeError("pipeline gone")

    monkeypatch.setattr(store.search_pipeline, "invalidate_cache", _boom)
    with pytest.raises(RuntimeError, match="pipeline gone"):
        run_async(adapter.purge_namespace("board-run:r1"))


def test_adapter_add_returning_ids_multi_chunk_returns_all(adapter):
    store = _FakeMemtomemStore.last
    store.index_engine.next_chunk_ids = (
        "aaaaaaaa-1111-4111-8111-111111111111",
        "cccccccc-3333-4333-8333-333333333333",
    )
    assert run_async(
        adapter.add_returning_ids("long body", tags=[], namespace="shared")
    ) == [
        "aaaaaaaa-1111-4111-8111-111111111111",
        "cccccccc-3333-4333-8333-333333333333",
    ]


def test_adapter_add_returning_ids_empty_on_no_new_chunks(adapter):
    store = _FakeMemtomemStore.last
    store.index_engine.next_chunk_ids = ()
    assert (
        run_async(adapter.add_returning_ids("body", tags=[], namespace="shared")) == []
    )


def test_adapter_add_returning_ids_raises_when_guard_blocks(adapter):
    store = _FakeMemtomemStore.last
    store.guard.block = True
    with pytest.raises(WriteBlockedError, match="redaction guard"):
        run_async(adapter.add_returning_ids("secret", tags=[], namespace="shared"))
    # blocked before any write reached the store
    assert store.appended == [] and store.index_engine.index_calls == []


_SRC_ID = "bbbbbbbb-2222-4222-8222-222222222222"


def test_adapter_share_chunk_copies_with_collapsed_tag_and_link_row(adapter):
    store = _FakeMemtomemStore.last
    # source carries a stale shared-from marker from an earlier share — the
    # copy must collapse the chain to the immediate parent only.
    store.get_results[_SRC_ID] = {
        "id": _SRC_ID,
        "content": "the finding",
        "tags": ["task-result", "shared-from=old-parent"],
        "namespace": "agent-runtime:claude",
    }
    new_id = run_async(adapter.share_chunk(_SRC_ID, target_namespace="shared"))
    assert new_id == "aaaaaaaa-1111-4111-8111-111111111111"
    assert store.appended[0]["tags"] == ["task-result", f"shared-from={_SRC_ID}"]
    assert store.index_engine.index_calls[0]["namespace"] == "shared"
    assert store.storage.chunk_links == [
        {
            "source_id": _SRC_ID,
            "target_id": new_id,
            "link_type": "shared",
            "namespace_target": "shared",
        }
    ]


def test_adapter_share_chunk_missing_source_returns_none(adapter):
    store = _FakeMemtomemStore.last
    store.get_results[_SRC_ID] = None
    assert run_async(adapter.share_chunk(_SRC_ID, target_namespace="shared")) is None
    assert store.appended == []


def test_adapter_share_chunk_tolerates_link_failure(adapter):
    # the copy is durable — a chunk_links write failure must not fail the share
    store = _FakeMemtomemStore.last
    store.get_results[_SRC_ID] = {
        "id": _SRC_ID,
        "content": "c",
        "tags": [],
        "namespace": "agent-runtime:claude",
    }
    store.storage.link_raises = True
    new_id = run_async(adapter.share_chunk(_SRC_ID, target_namespace="shared"))
    assert new_id == "aaaaaaaa-1111-4111-8111-111111111111"
    assert store.storage.chunk_links == []


# ── add_and_share: one lock span per share (#117) ──────────────────────────

_SRC_A = "aaaaaaaa-0000-4000-8000-00000000000a"
_SRC_B = "bbbbbbbb-0000-4000-8000-00000000000b"
_COPY_A = "cccccccc-0000-4000-8000-00000000000c"
_COPY_B = "dddddddd-0000-4000-8000-00000000000d"
_AGENT_NS = "agent-runtime:claude"


def _script_share(store, *, source_ids, copy_ids, origins=None):
    """Script one add_and_share: the original indexes to ``source_ids``, the
    copies to ``copy_ids``, and each copy row names its source via the
    shared-from tag (``origins`` maps copy id -> source id; default pairwise)."""
    store.index_engine.chunk_id_queue = [tuple(source_ids), tuple(copy_ids)]
    for cid in source_ids:
        store.get_results[cid] = {
            "id": cid,
            "content": f"chunk {cid[:4]}",
            "tags": ["council-proposal", "claude", "task:t1"],
            "namespace": _AGENT_NS,
        }
    origins = origins if origins is not None else dict(zip(copy_ids, source_ids))
    for copy_id in copy_ids:
        tags = ["council-proposal", "claude", "task:t1"]
        if origins.get(copy_id):
            tags.append(f"shared-from={origins[copy_id]}")
        store.get_results[copy_id] = {
            "id": copy_id,
            "content": "copy",
            "tags": tags,
            "namespace": "shared",
        }


def _share(adapter, content="**Task Result**\n\nbody"):
    return run_async(
        adapter.add_and_share(
            content,
            tags=["council-proposal", "claude", "task:t1"],
            namespace=_AGENT_NS,
            target_namespace="shared",
        )
    )


def test_memtomem_declares_single_span_share_and_the_others_do_not(
    adapter, mem0_adapter, monkeypatch, tmp_path
):
    assert adapter.capabilities.single_span_share is True
    # mem0/LangGraph have no file locks to merge, so they keep the two-call
    # adapter fallback (and the safe default is off for any other adapter).
    assert mem0_adapter.capabilities.single_span_share is False
    _install_fake_langgraph(monkeypatch)
    langgraph = LangGraphBackend(
        MemoryConfig(backend="langgraph", langgraph={"db_path": str(tmp_path / "lg.db")})
    )
    assert langgraph.capabilities.single_span_share is False
    assert BackendCapabilities().single_span_share is False


def test_add_and_share_holds_one_span_across_both_appends(adapter):
    """Both namespace locks, then both sidecars, are taken BEFORE the original
    is written and released only AFTER the last copy is indexed — so no other
    writer of either namespace can land between the original and its copies.
    Each file keeps its own mix → append → index inside the span, and the
    copies share ONE index of the target file instead of one per chunk."""
    store = _FakeMemtomemStore.last
    _script_share(store, source_ids=[_SRC_A, _SRC_B], copy_ids=[_COPY_A, _COPY_B])

    out = _share(adapter)

    assert out == mb.SharedWrite((_SRC_A, _SRC_B), (_COPY_A, _COPY_B))
    assert store.events == [
        "lock:acquire",
        "lock:acquire",
        "lock:acquire",
        "lock:acquire",
        "mix",
        "append",
        "index",
        "mix",
        "append",
        "append",
        "index",
        "lock:release",
        "lock:release",
        "lock:release",
        "lock:release",
    ]
    original, *copies = store.appended
    assert original["path"].name.endswith(".agent-runtime.claude.md")
    assert {c["path"].name for c in copies} == {original["path"].name.replace(
        "agent-runtime.claude", "shared")}
    # each copy carries the collapsed shared-from tag of ITS source chunk
    assert [c["tags"][-1] for c in copies] == [
        f"shared-from={_SRC_A}",
        f"shared-from={_SRC_B}",
    ]
    # the unique-heading invariant holds for every entry in the span
    titles = [e["title"] for e in store.appended]
    assert all(titles) and len(set(titles)) == len(titles)
    # both index calls are namespace-explicit and run under the held sidecar
    assert [(c["namespace"], c["lock_held"]) for c in store.index_engine.index_calls] == [
        (_AGENT_NS, True),
        ("shared", True),
    ]
    # the mix guard judged each file for its own namespace
    assert [(m["target"].name, m["effective_ns"]) for m in store.mix_calls] == [
        (original["path"].name, _AGENT_NS),
        (copies[0]["path"].name, "shared"),
    ]
    assert store.storage.chunk_links == [
        {"source_id": _SRC_A, "target_id": _COPY_A, "link_type": "shared",
         "namespace_target": "shared"},
        {"source_id": _SRC_B, "target_id": _COPY_B, "link_type": "shared",
         "namespace_target": "shared"},
    ]
    assert store.search_pipeline.invalidate_calls == 1


def test_add_and_share_acquires_namespace_locks_then_sidecars_each_ascending(adapter):
    # The global order (`_namespace_lock_path`): every namespace lock before
    # any sidecar, each level ascending by str(lock path). It is what keeps two
    # shares, a share and a write, and a share and a purge cycle-free.
    store = _FakeMemtomemStore.last
    _script_share(store, source_ids=[_SRC_A], copy_ids=[_COPY_A])
    _share(adapter)
    taken = [str(p) for p in store.lock_paths]
    ns_locks, sidecars = taken[:2], taken[2:]
    assert all(".syncmill-ns-" in Path(p).name for p in ns_locks)
    assert not any(".syncmill-ns-" in Path(p).name for p in sidecars)
    assert ns_locks == sorted(ns_locks)
    assert sidecars == sorted(sidecars)


def test_add_and_share_order_does_not_depend_on_which_namespace_is_the_source(
    adapter,
):
    # Two shares naming the same pair in opposite roles must still agree on
    # the order, or each could hold the lock the other waits for.
    store = _FakeMemtomemStore.last
    _script_share(store, source_ids=[_SRC_A], copy_ids=[_COPY_A])
    run_async(adapter.add_and_share(
        "**a**", tags=[], namespace=_AGENT_NS, target_namespace="shared"))
    forward = list(store.lock_paths)
    store.lock_paths.clear()
    _script_share(store, source_ids=[_SRC_B], copy_ids=[_COPY_B])
    run_async(adapter.add_and_share(
        "**b**", tags=[], namespace="shared", target_namespace=_AGENT_NS))
    assert store.lock_paths == forward


def test_namespace_lock_order_is_sorted_and_deduplicated(tmp_path):
    order = mb._namespace_lock_order(tmp_path, ["shared", _AGENT_NS, "shared"])
    assert len(order) == 2
    assert [str(p) for p in order] == sorted(str(p) for p in order)
    assert order == mb._namespace_lock_order(tmp_path, [_AGENT_NS, "shared"])


def test_add_and_share_and_purge_agree_on_the_level_order(adapter, tmp_path, monkeypatch):
    # The purge takes its namespace lock first and only then sidecars — the
    # same level order the share uses, so neither can hold a sidecar the other
    # needs while waiting on a namespace lock the first holds.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    (tmp_path / "2026-08-01.shared.md").write_text("x")
    run_async(adapter.purge_namespace("shared"))
    purge = [p.name for p in store.lock_paths]
    assert purge[0].startswith(".syncmill-ns-")
    assert not any(n.startswith(".syncmill-ns-") for n in purge[1:])


def test_add_and_share_attributes_copies_by_their_shared_from_tag(adapter, caplog):
    # The copy index can return the copies in any order, a copy that re-chunks
    # into two, or a stale chunk left in the same file by a writer that died
    # before indexing. Attribution is by the shared-from tag each copy carries,
    # so a stranger is never linked and never counted as a copy.
    store = _FakeMemtomemStore.last
    stranger = "eeeeeeee-0000-4000-8000-00000000000e"
    second_half = "ffffffff-0000-4000-8000-00000000000f"
    _script_share(
        store,
        source_ids=[_SRC_A, _SRC_B],
        copy_ids=[stranger, _COPY_B, _COPY_A, second_half],
        origins={_COPY_B: _SRC_B, _COPY_A: _SRC_A, second_half: _SRC_A},
    )
    with caplog.at_level(logging.WARNING, logger="syncmill.memory_backend"):
        out = _share(adapter)
    assert out == mb.SharedWrite((_SRC_A, _SRC_B), (_COPY_A, _COPY_B))
    assert {(link["source_id"], link["target_id"]) for link in store.storage.chunk_links} == {
        (_SRC_A, _COPY_A), (_SRC_A, second_half), (_SRC_B, _COPY_B),
    }
    assert stranger in caplog.text


def test_add_and_share_reports_an_uncopied_chunk_as_none(adapter):
    # What share_chunk returns None for — here the copy minted no fresh id —
    # stays None in its slot, so SyncmillMemory can tell a PARTIAL share.
    store = _FakeMemtomemStore.last
    _script_share(store, source_ids=[_SRC_A, _SRC_B], copy_ids=[_COPY_A])
    assert _share(adapter) == mb.SharedWrite((_SRC_A, _SRC_B), (_COPY_A, None))


def test_add_and_share_skips_a_source_whose_lookup_is_empty(adapter):
    store = _FakeMemtomemStore.last
    _script_share(
        store, source_ids=[_SRC_A, _SRC_B], copy_ids=[_COPY_B], origins={_COPY_B: _SRC_B}
    )
    store.get_results[_SRC_A] = None
    assert _share(adapter) == mb.SharedWrite((_SRC_A, _SRC_B), (None, _COPY_B))
    # only the chunk that could be read was copied
    assert len(store.appended) == 2


def test_add_and_share_with_no_fresh_source_id_copies_nothing(adapter):
    # The content-hash dedup edge: nothing minted, so nothing to copy, and the
    # target file is never re-indexed.
    store = _FakeMemtomemStore.last
    store.index_engine.chunk_id_queue = [()]
    assert _share(adapter) == mb.SharedWrite()
    assert len(store.appended) == 1
    assert len(store.index_engine.index_calls) == 1
    assert store.events.count("lock:release") == 4
    assert store.search_pipeline.invalidate_calls == 1


def test_add_and_share_guard_block_on_the_original_writes_and_locks_nothing(adapter):
    store = _FakeMemtomemStore.last
    store.guard.block = True
    with pytest.raises(WriteBlockedError):
        _share(adapter)
    assert store.appended == [] and store.lock_paths == []
    assert store.search_pipeline.invalidate_calls == 0


def test_add_and_share_guard_block_on_a_copy_appends_no_copy(adapter, monkeypatch):
    # Every copy is guarded before ANY is appended; the original is already
    # durable, so this surfaces as WriteBlockedError (SyncmillMemory degrades
    # and skips the fallback, which would re-block) with every lock released.
    store = _FakeMemtomemStore.last
    _script_share(store, source_ids=[_SRC_A, _SRC_B], copy_ids=[_COPY_A, _COPY_B])
    store.get_results[_SRC_B]["content"] = "a secret chunk"
    real_guard = store.guard

    def guard(content, **kwargs):
        decision = real_guard(content, **kwargs)
        if "secret" in content:
            return SimpleNamespace(decision="blocked", hits=())
        return decision

    monkeypatch.setattr("memtomem.privacy.enforce_write_guard", guard)
    with pytest.raises(WriteBlockedError):
        _share(adapter)
    assert len(store.appended) == 1  # the original only
    assert store.events.count("lock:acquire") == store.events.count("lock:release") == 4
    assert store.search_pipeline.invalidate_calls == 1


def test_add_and_share_mix_refusal_on_the_target_aborts_before_any_copy(
    adapter, monkeypatch
):
    store = _FakeMemtomemStore.last
    _script_share(store, source_ids=[_SRC_A], copy_ids=[_COPY_A])
    calls = {"n": 0}
    real_mix = store.mix_result

    async def refuse_second(**kwargs):
        calls["n"] += 1
        store.events.append("mix")
        return "stored rows disagree" if calls["n"] == 2 else real_mix["value"]

    monkeypatch.setattr("memtomem.memory_scope.namespace_mix_refusal", refuse_second)
    with pytest.raises(RuntimeError, match="namespace mix refused"):
        _share(adapter)
    assert len(store.appended) == 1
    assert store.storage.chunk_links == []
    assert store.events.count("lock:release") == 4


def test_add_and_share_refuses_a_protected_copy_target_before_writing(
    adapter, tmp_path, monkeypatch
):
    # The protected-target refusals cover BOTH files before either is touched:
    # a refused `shared` day file must not leave a private original behind it.
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text("")
    _today_target(tmp_path, "shared").symlink_to(elsewhere)
    with pytest.raises(RuntimeError, match="symlinked"):
        _share(adapter)
    assert store.appended == []
    assert elsewhere.read_text() == ""
    # refused before the sidecars: only the two namespace locks were taken
    assert store.events == ["lock:acquire"] * 2 + ["lock:release"] * 2


@pytest.mark.parametrize("predicate", ["read_only", "excluded"])
def test_add_and_share_rechecks_both_targets_once_the_sidecars_are_held(
    adapter, tmp_path, monkeypatch, predicate
):
    _point_index_roots(monkeypatch, tmp_path)
    store = _FakeMemtomemStore.last
    target = _today_target(tmp_path, "shared")
    atomic = mb._get_memtomem_atomic()

    def flip(path):
        if path == atomic.memory_lock_path(target):
            getattr(store.index_engine, predicate).add(target)

    store.on_acquire.append(flip)
    with pytest.raises(RuntimeError, match="refusing"):
        _share(adapter)
    assert store.appended == []
    assert store.index_engine.index_calls == []


def test_add_and_share_refuses_one_namespace_for_both_halves(adapter):
    store = _FakeMemtomemStore.last
    with pytest.raises(ValueError, match="two namespaces"):
        run_async(adapter.add_and_share(
            "**x**", tags=[], namespace="shared", target_namespace="shared"))
    assert store.appended == [] and store.lock_paths == []


def test_shared_write_must_align_its_two_sequences():
    with pytest.raises(ValueError, match="align"):
        mb.SharedWrite(("a", "b"), ("c",))


def test_adapter_register_namespace_upserts_unconditionally(adapter):
    store = _FakeMemtomemStore.last
    run_async(
        adapter.register_namespace(
            "agent-runtime:claude", description="Agent: claude", color="#D97757"
        )
    )
    run_async(adapter.register_namespace("agent-runtime:claude", color="#000000"))
    # both calls reached set_namespace_meta — no create-if-missing gate, and a
    # None field is passed through (memtomem "leave as is" semantics).
    assert store.storage.meta_upserts == [
        {
            "ns": "agent-runtime:claude",
            "description": "Agent: claude",
            "color": "#D97757",
        },
        {"ns": "agent-runtime:claude", "description": None, "color": "#000000"},
    ]


def test_adapter_namespace_lifecycle_and_exact_status(adapter):
    store = _FakeMemtomemStore.last
    run_async(adapter.ensure_namespace("shared", description="Shared knowledge"))
    run_async(adapter.ensure_namespace("shared", description="ignored"))
    store.storage.counts["shared"] = 4

    assert store.storage.set_calls == [("shared", "Shared knowledge")]
    assert run_async(adapter.count_namespace("shared")) == 4
    assert run_async(adapter.count_namespace("missing")) == 0
    assert run_async(adapter.status()) == {"db_path": "/tmp/p1-memory.db"}
    # status() reads the adapter's OWN overrides dict, never the store's
    # private `_config_overrides` — an upstream rename must not turn into a
    # "connection failed" report.
    del store._config_overrides
    assert run_async(adapter.status()) == {"db_path": "/tmp/p1-memory.db"}
    assert run_async(adapter.delete_namespace("shared")) == 4
    assert store.storage.deleted == ["shared"]


def test_adapter_passes_explicit_memory_dir_to_memtomem(monkeypatch):
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore", _FakeMemtomemStore
    )
    MemtomemBackend(
        MemoryConfig(
            db_path="/tmp/isolated.db",
            memory_dir="/tmp/isolated-memories",
        )
    )

    assert _FakeMemtomemStore.last._config_overrides == {
        "storage": {"sqlite_path": "/tmp/isolated.db"},
        "indexing": {"memory_dirs": ["/tmp/isolated-memories"]},
    }


def test_adapter_preserves_ambient_memory_dir_by_default(monkeypatch):
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore", _FakeMemtomemStore
    )
    MemtomemBackend(MemoryConfig(db_path="/tmp/custom.db"))

    assert _FakeMemtomemStore.last._config_overrides == {
        "storage": {"sqlite_path": "/tmp/custom.db"}
    }


def test_build_backend_constructs_memtomem(monkeypatch):
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore", _FakeMemtomemStore
    )
    backend = mb.build_backend(MemoryConfig())
    assert isinstance(backend, MemtomemBackend)


def test_build_backend_constructs_mem0_without_memtomem_constants(
    monkeypatch, tmp_path
):
    _install_fake_mem0(monkeypatch)
    monkeypatch.setitem(sys.modules, "memtomem.constants", None)
    backend = mb.build_backend(
        MemoryConfig(backend="mem0", mem0={"data_dir": str(tmp_path / "mem0")})
    )
    assert isinstance(backend, Mem0Backend)


def test_mem0_verbatim_mapping_search_and_close(mem0_adapter):
    store = _FakeMem0Memory.last
    content = "한글\nverbatim"
    run_async(mem0_adapter.add(content, tags=["one", "two"], namespace="shared"))
    rows = run_async(
        mem0_adapter.search("query", namespace="shared", top_k=3, tag_filter="one,two")
    )
    assert store.added == [
        {
            "messages": [{"role": "user", "content": content}],
            "user_id": "shared",
            "metadata": {"syncmill_tags": ["one", "two"]},
            "infer": False,
        }
    ]
    assert os.environ["MEM0_TELEMETRY"] == "false"
    with pytest.raises(RuntimeError, match="disables LLM inference"):
        store.llm.generate_response([])
    assert store.searches[0]["threshold"] == 0.0
    assert store.searches[0]["rerank"] is False
    assert store.searches[0]["filters"] == {
        "user_id": "shared",
        "OR": [{"syncmill_tags": "one"}, {"syncmill_tags": "two"}],
    }
    assert rows == [
        {
            "id": "id-1",
            "content": "hit",
            "score": 0.8,
            "source": "mem0",
            "namespace": "shared",
            "tags": [],
        }
    ]
    run_async(mem0_adapter.close())
    run_async(mem0_adapter.close())
    assert store.closed is True
    assert store.vector_store.client.closed is True


def test_mem0_close_survives_qdrant_collection_shape_drift(mem0_adapter):
    store = _FakeMem0Memory.last
    client = store.vector_store.client
    del store.vector_store.collection_name

    run_async(mem0_adapter.close())

    assert store.closed is True
    assert client.closed is True


@pytest.mark.parametrize("missing", ["client", "vector_store"])
def test_mem0_close_survives_missing_qdrant_internals(mem0_adapter, missing):
    store = _FakeMem0Memory.last
    if missing == "client":
        del store.vector_store.client
    else:
        del store.vector_store

    run_async(mem0_adapter.close())

    assert store.closed is True


def test_mem0_count_and_multibatch_delete(mem0_adapter):
    store = _FakeMem0Memory.last
    store.counts["board-run:r1"] = 250
    assert run_async(mem0_adapter.count_namespace("board-run:r1")) == 250
    assert run_async(mem0_adapter.delete_namespace("board-run:r1")) == 250
    assert store.deleted == ["board-run:r1"] * 3
    assert run_async(mem0_adapter.count_namespace("board-run:r1")) == 0


def test_mem0_rejects_nonverbatim_ack_and_delete_no_progress(mem0_adapter):
    store = _FakeMem0Memory.last
    store.alter_add = True
    with pytest.raises(RuntimeError, match="one verbatim memory write"):
        run_async(mem0_adapter.add("body", tags=[], namespace="shared"))

    store.alter_add = False
    store.counts["board-run:stuck"] = 2
    store.delete_progress = False
    with pytest.raises(RuntimeError, match="made no progress"):
        run_async(mem0_adapter.delete_namespace("board-run:stuck"))
    assert store.counts["board-run:stuck"] == 2


def test_mem0_get_fails_closed_without_namespace(mem0_adapter):
    store = _FakeMem0Memory.last
    store.rows["missing-scope"] = {"id": "missing-scope", "memory": "private"}
    store.rows["ok"] = {
        "id": "ok",
        "memory": "shared",
        "user_id": "shared",
        "metadata": {},
    }
    assert run_async(mem0_adapter.get("missing-scope")) is None
    assert run_async(mem0_adapter.get("ok"))["namespace"] == "shared"


def test_mem0_fallback_capabilities_and_search_multi_merge(mem0_adapter):
    caps = mem0_adapter.capabilities
    assert caps.native_multi_namespace_search is False
    assert caps.share_provenance_links is False
    store = _FakeMem0Memory.last
    rows = run_async(
        mem0_adapter.search_multi(
            "q", namespaces=["agent-runtime:claude", "shared"], top_k=5
        )
    )
    # adapter-side loop: one store.search per namespace, then dedupe-by-id
    # (the fake returns the same id-1 from both legs → one merged row)
    assert [s["filters"]["user_id"] for s in store.searches] == [
        "agent-runtime:claude",
        "shared",
    ]
    assert [r["id"] for r in rows] == ["id-1"]


def test_mem0_add_returning_ids_and_tag_only_share_chunk(mem0_adapter):
    store = _FakeMem0Memory.last
    assert run_async(
        mem0_adapter.add_returning_ids(
            "c", tags=["t"], namespace="agent-runtime:claude"
        )
    ) == ["id-1"]
    store.rows["id-1"] = {
        "id": "id-1",
        "memory": "the finding",
        "user_id": "agent-runtime:claude",
        "metadata": {"syncmill_tags": ["task-result", "shared-from=old-parent"]},
    }
    assert (
        run_async(mem0_adapter.share_chunk("id-1", target_namespace="shared")) == "id-1"
    )
    copy = store.added[-1]
    assert copy["user_id"] == "shared"
    assert copy["messages"][0]["content"] == "the finding"
    # collapsed shared-from chain, no link row (share_provenance_links=False)
    assert copy["metadata"]["syncmill_tags"] == ["task-result", "shared-from=id-1"]
    assert (
        run_async(mem0_adapter.share_chunk("absent", target_namespace="shared")) is None
    )


def test_syncmill_memory_operates_over_mem0_and_acknowledges_scratch_gc(
    mem0_adapter,
):
    store = _FakeMem0Memory.last
    memory = make_memory(mem0_adapter)
    assert run_async(memory.share_board_result("item-1", "task", "output")) is True
    assert store.added[-1]["user_id"] == "shared"
    assert store.added[-1]["infer"] is False

    store.counts["board-run:old"] = 1
    assert run_async(memory.delete_board_runs(["old"])) == {"old"}
    assert store.counts["board-run:old"] == 0


def test_mem0_rejects_non_qdrant_and_dimension_mismatch(monkeypatch, tmp_path):
    _install_fake_mem0(monkeypatch)
    with pytest.raises(ValueError, match="requires vector_store.provider='qdrant'"):
        Mem0Backend(
            MemoryConfig(
                backend="mem0",
                mem0={
                    "data_dir": str(tmp_path / "a"),
                    "config": {"vector_store": {"provider": "chroma"}},
                },
            )
        )
    with pytest.raises(ValueError, match="must match embedder dimensions"):
        Mem0Backend(
            MemoryConfig(
                backend="mem0",
                mem0={
                    "data_dir": str(tmp_path / "b"),
                    "config": {
                        "embedder": {"config": {"embedding_dims": 768}},
                        "vector_store": {"config": {"embedding_model_dims": 384}},
                    },
                },
            )
        )


def test_mem0_custom_embedder_requires_model_and_dimensions(monkeypatch, tmp_path):
    _install_fake_mem0(monkeypatch)
    with pytest.raises(ValueError, match="embedding_dims explicitly"):
        Mem0Backend(
            MemoryConfig(
                backend="mem0",
                mem0={
                    "data_dir": str(tmp_path / "a"),
                    "config": {
                        "embedder": {
                            "provider": "fastembed",
                            "config": {"model": "another-model"},
                        }
                    },
                },
            )
        )
    with pytest.raises(ValueError, match="config.model explicitly"):
        Mem0Backend(
            MemoryConfig(
                backend="mem0",
                mem0={
                    "data_dir": str(tmp_path / "b"),
                    "config": {
                        "embedder": {
                            "provider": "openai",
                            "config": {"embedding_dims": 1536},
                        }
                    },
                },
            )
        )


def test_mem0_missing_optional_extra_has_install_hint(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mem0", None)
    with pytest.raises(RuntimeError, match=r"install syncmill\[mem0\]"):
        Mem0Backend(
            MemoryConfig(backend="mem0", mem0={"data_dir": str(tmp_path / "mem0")})
        )
    memory = SyncmillMemory(
        MemoryConfig(backend="mem0", mem0={"data_dir": str(tmp_path / "status-mem0")})
    )
    info = run_async(memory.status())
    assert info["connected"] is False
    assert "syncmill[mem0]" in info["error"]


def test_build_backend_refuses_non_verbatim(monkeypatch):
    class NonVerbatim:
        capabilities = BackendCapabilities(verbatim_storage=False)

        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(mb, "MemtomemBackend", NonVerbatim)
    with pytest.raises(ValueError, match="cannot store verbatim content"):
        mb.build_backend(MemoryConfig())


class _FakeLangGraphStore:
    last = None

    def __init__(self):
        type(self).last = self
        self.rows = {}
        self.setup_called = False
        self.closed = False

    async def setup(self):
        self.setup_called = True

    async def aput(self, namespace, key, value, **kwargs):
        self.rows[(namespace, key)] = dict(value)

    async def aget(self, namespace, key):
        value = self.rows.get((namespace, key))
        return (
            None if value is None else SimpleNamespace(key=key, value=value, score=None)
        )

    async def asearch(self, namespace, *, query=None, filter=None, limit=10, offset=0):
        matches = []
        for (row_ns, key), value in self.rows.items():
            if row_ns != namespace:
                continue
            if filter and any(value.get(k) != v for k, v in filter.items()):
                continue
            matches.append(
                SimpleNamespace(key=key, value=value, score=0.8 if query else None)
            )
        return matches[offset : offset + limit]

    async def adelete(self, namespace, key):
        self.rows.pop((namespace, key), None)


class _FakeLangGraphCM:
    def __init__(self):
        self.store = _FakeLangGraphStore()

    async def __aenter__(self):
        return self.store

    async def __aexit__(self, *args):
        self.store.closed = True


def _install_fake_langgraph(monkeypatch):
    fastembed = ModuleType("fastembed")

    class TextEmbedding:
        def __init__(self, **kwargs):
            pass

        def embed(self, texts):
            return [[0.0] * 384 for _ in texts]

    fastembed.TextEmbedding = TextEmbedding
    embeddings = ModuleType("langchain_core.embeddings")

    class Embeddings:
        pass

    embeddings.Embeddings = Embeddings
    aio = ModuleType("langgraph.store.sqlite.aio")

    class AsyncSqliteStore:
        @classmethod
        def from_conn_string(cls, path, **kwargs):
            return _FakeLangGraphCM()

    aio.AsyncSqliteStore = AsyncSqliteStore
    for name, module in {
        "fastembed": fastembed,
        "langchain_core": ModuleType("langchain_core"),
        "langchain_core.embeddings": embeddings,
        "langgraph": ModuleType("langgraph"),
        "langgraph.store": ModuleType("langgraph.store"),
        "langgraph.store.sqlite": ModuleType("langgraph.store.sqlite"),
        "langgraph.store.sqlite.aio": aio,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)


def test_langgraph_verbatim_tags_lifecycle_and_delete(monkeypatch, tmp_path):
    _install_fake_langgraph(monkeypatch)
    backend = LangGraphBackend(
        MemoryConfig(
            backend="langgraph",
            langgraph={"db_path": str(tmp_path / "langgraph.db")},
        )
    )
    run_async(backend.add("한글 body", tags=["one", "two"], namespace="shared"))
    store = _FakeLangGraphStore.last
    assert store.setup_called is True
    rows = run_async(
        backend.search("body", namespace="shared", top_k=3, tag_filter="two,missing")
    )
    assert rows[0]["content"] == "한글 body"
    assert rows[0]["tags"] == ["one", "two"]
    assert run_async(backend.get(rows[0]["id"]))["namespace"] == "shared"
    assert run_async(backend.count_namespace("shared")) == 1
    assert run_async(backend.delete_namespace("shared")) == 1
    assert run_async(backend.count_namespace("shared")) == 0
    run_async(backend.close())
    run_async(backend.close())
    assert store.closed is True


def test_langgraph_delete_without_progress_raises(monkeypatch, tmp_path):
    # The mem0 adapter's no-progress guard, ported: an adelete that no-ops
    # (mismatched key tuple, permission issue) returned the same page forever —
    # an unbounded, un-cancellable hang instead of an error.
    _install_fake_langgraph(monkeypatch)
    backend = LangGraphBackend(
        MemoryConfig(
            backend="langgraph",
            langgraph={"db_path": str(tmp_path / "langgraph.db")},
        )
    )
    run_async(backend.add("body", tags=[], namespace="shared"))
    store = _FakeLangGraphStore.last

    async def noop_delete(namespace, key):
        return None

    store.adelete = noop_delete
    with pytest.raises(RuntimeError, match="made no progress"):
        run_async(backend.delete_namespace("shared"))
    run_async(backend.close())


def test_langgraph_count_caps_a_store_that_ignores_offset(monkeypatch, tmp_path):
    # A store whose asearch ignores `offset` returns a full page forever;
    # count_namespace must hit the iteration backstop instead of spinning.
    _install_fake_langgraph(monkeypatch)
    monkeypatch.setattr(mb, "_LANGGRAPH_COUNT_CAP", 500)
    backend = LangGraphBackend(
        MemoryConfig(
            backend="langgraph",
            langgraph={"db_path": str(tmp_path / "langgraph.db")},
        )
    )
    run_async(backend.ensure_ready())
    store = _FakeLangGraphStore.last

    async def full_page(namespace, *, query=None, filter=None, limit=10, offset=0):
        return [
            SimpleNamespace(key=f"k{i}", value={}, score=None) for i in range(limit)
        ]

    store.asearch = full_page
    with pytest.raises(RuntimeError, match="count made no progress"):
        run_async(backend.count_namespace("shared"))
    run_async(backend.close())


def test_langgraph_fallback_search_multi_and_share_chunk(monkeypatch, tmp_path):
    _install_fake_langgraph(monkeypatch)
    backend = LangGraphBackend(
        MemoryConfig(
            backend="langgraph",
            langgraph={"db_path": str(tmp_path / "langgraph.db")},
        )
    )
    caps = backend.capabilities
    assert caps.native_multi_namespace_search is False
    assert caps.share_provenance_links is False
    ids = run_async(
        backend.add_returning_ids(
            "the finding",
            tags=["task-result", "shared-from=old-parent"],
            namespace="agent-runtime:claude",
        )
    )
    assert len(ids) == 1 and ids[0]  # langgraph mints the storage key itself
    chunk_id = ids[0]
    rows = run_async(
        backend.search_multi(
            "finding", namespaces=["agent-runtime:claude", "shared"], top_k=5
        )
    )
    assert [r["id"] for r in rows] == [chunk_id]
    new_id = run_async(backend.share_chunk(chunk_id, target_namespace="shared"))
    assert new_id and new_id != chunk_id
    copy = run_async(backend.get(new_id))
    assert copy["namespace"] == "shared"
    assert copy["content"] == "the finding"
    # collapsed shared-from chain (tag-only provenance — no link table here)
    assert copy["tags"] == ["task-result", f"shared-from={chunk_id}"]
    assert (
        run_async(backend.share_chunk("missing-key", target_namespace="shared")) is None
    )
    run_async(backend.close())


def test_langgraph_missing_optional_extra_has_hint(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    backend = LangGraphBackend(
        MemoryConfig(
            backend="langgraph",
            langgraph={"db_path": str(tmp_path / "missing.db")},
        )
    )
    with pytest.raises(RuntimeError, match=r"syncmill\[langgraph\]"):
        run_async(backend.ensure_ready())


def test_adapter_warns_only_for_the_heading_the_writer_actually_drops(adapter, caplog):
    # The tripwire must match the branch the PINNED writer takes, which is
    # `stripped.startswith("## ")` — nothing wider. Widening it to any ATX
    # heading made it warn about a failure that does not happen (see the real
    # formatter test below), which is worse than not warning: it sends the next
    # reader looking for a bug that is not there.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        run_async(
            adapter.add_returning_ids("## Two\n\nbody", tags=[], namespace="shared")
        )
    assert any("heading" in r.message for r in caplog.records)

    for quiet in ("# Title\n\nbody", "### Sub\n\nbody", "**Bold label**\n\nbody"):
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            run_async(adapter.add_returning_ids(quiet, tags=[], namespace="shared"))
        assert not [r for r in caplog.records if "heading" in r.message], quiet


@pytest.mark.integration
def test_the_real_formatter_only_bypasses_a_h2_heading():
    # The evidence the warning above is calibrated to. Hermetic tests fake the
    # SDK boundary, so only calling the REAL memtomem formatter can show which
    # payloads actually lose their unique heading — the claim the warning makes.
    pytest.importorskip("memtomem.tools.memory_writer")
    from memtomem.tools.memory_writer import format_entry_block

    def keeps_title(content: str) -> bool:
        return "UNIQUE-TITLE" in format_entry_block(
            content=content, title="UNIQUE-TITLE", tags=["t"]
        )

    assert not keeps_title("## H2\n\nbody"), "H2 is the branch we warn about"
    for kept in ("# H1\n\nbody", "### H3\n\nbody", "**Bold**\n\nbody"):
        assert keeps_title(kept), f"{kept!r} keeps its title — do not warn for it"


@pytest.mark.parametrize("count", [255, 256, 257])
def test_purge_source_lock_ceiling(adapter, tmp_path, monkeypatch, count):
    from datetime import date, timedelta

    # This fixture records locks without opening descriptors. The real-store
    # suite separately tests actual capacity and low-headroom refusal.
    monkeypatch.setattr("resource.getrlimit", lambda _: (4096, 4096))
    _point_index_roots(monkeypatch, tmp_path)
    paths = [
        tmp_path / f"{date(2025, 1, 1) + timedelta(days=i)}.board-run.r1.md"
        for i in range(count)
    ]
    for path in paths:
        path.write_text("scratch")
    shared = tmp_path / "2025-01-01.shared.md"
    shared.write_text("keep")
    store = _FakeMemtomemStore.last
    store.events.clear()
    if count > 256:
        with pytest.raises(mb.UnprovablePurgeError):
            run_async(adapter.purge_namespace("board-run:r1"))
        assert all(path.exists() for path in paths)
        assert not store.storage.deleted
    else:
        assert run_async(adapter.purge_namespace("board-run:r1")) == count
        assert not any(path.exists() for path in paths)
    assert store.events.count("lock:acquire") == store.events.count("lock:release")
    assert shared.read_text() == "keep"


def test_purge_deadline_covers_partial_unlinks(adapter, tmp_path, monkeypatch):
    _point_index_roots(monkeypatch, tmp_path)
    paths = [tmp_path / f"2026-08-0{day}.board-run.r1.md" for day in (1, 2)]
    for path in paths:
        path.write_text("scratch")
    clock = [0.0]
    monkeypatch.setattr(mb, "monotonic", lambda: clock[0])
    original = mb.os.unlink

    def unlink(path):
        original(path)
        clock[0] = 1000.0

    with monkeypatch.context() as scoped:
        scoped.setattr(mb.os, "unlink", unlink)
        with pytest.raises(TimeoutError):
            run_async(adapter.purge_namespace("board-run:r1"))
    assert not paths[0].exists() and paths[1].exists()
    assert not _FakeMemtomemStore.last.storage.deleted
    # Partial deletion is retryable, rather than acknowledged or rolled back.
    assert run_async(adapter.purge_namespace("board-run:r1")) == 1


def test_purge_partial_delete_releases_all_locks(adapter, tmp_path, monkeypatch):
    _point_index_roots(monkeypatch, tmp_path)
    paths = [tmp_path / f"2026-08-0{day}.board-run.r1.md" for day in (1, 2)]
    for path in paths:
        path.write_text("scratch")
    store = _FakeMemtomemStore.last
    original = mb.os.unlink

    def unlink(path):
        if path == paths[1]:
            raise PermissionError("injected unlink failure")
        original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(mb.os, "unlink", unlink)
        with pytest.raises(PermissionError):
            run_async(adapter.purge_namespace("board-run:r1"))
    assert not paths[0].exists() and paths[1].exists()
    assert not store.storage.deleted
    assert store.events.count("lock:acquire") == store.events.count("lock:release")
    assert run_async(adapter.purge_namespace("board-run:r1")) == 1


def test_get_memtomem_atomic_resolves():
    atomic = mb._get_memtomem_atomic()
    assert hasattr(atomic, "_CRUD_SIDECAR_LOCK_BUDGET_S")
    assert hasattr(atomic, "async_file_lock")
    # the L2 key the indexer uses (resolved path) — the write and purge locks
    # must be derived the same way or they exclude nothing (upstream #2130)
    assert hasattr(atomic, "memory_lock_path")


def test_get_memtomem_atomic_falls_back_on_importerror(monkeypatch):
    import sys
    from types import SimpleNamespace

    fake_atomic = SimpleNamespace(
        _CRUD_SIDECAR_LOCK_BUDGET_S=5.0,
        async_file_lock=lambda *a, **k: None,
        memory_lock_path=lambda p: p,
    )
    # Simulate memtomem._atomic not existing (ImportError)
    monkeypatch.setitem(sys.modules, "memtomem._atomic", None)
    monkeypatch.setattr("memtomem.context._atomic", fake_atomic, raising=False)

    resolved = mb._get_memtomem_atomic()
    assert resolved == fake_atomic


def test_memtomem_status_enriches_diagnostics(adapter):
    store = _FakeMemtomemStore.last
    store.storage.stats = {"total_chunks": 42, "total_sources": 3, "dense_vectors": 42}
    status = run_async(adapter.status())
    assert "db_path" in status
    assert status["db_path"] == "/tmp/p1-memory.db"
    assert status.get("total_chunks") == 42
    assert status.get("total_sources") == 3
    assert status.get("dense_vectors") == 42


def test_propagate_entry_tags_to_headings_fences_and_tabs():
    content = (
        "## Section\n"
        "```python\n"
        "# keep comment\n"
        "print(1)\n"
        "```\n"
        "    # indented comment\n"
        "\t# tab comment\n"
        "  \t# mixed comment\n"
        "~~~bash\n"
        "# bash comment\n"
        "~~~\n"
        "   ### Indented heading prose\n"
        "#\u00a0Heading with NBSP\n"
        "# Conclusion"
    )
    tags = ["test-tag", "item-1"]
    propagated = mb._propagate_entry_tags_to_headings(content, tags)
    # Headings matching ^(#{1,6})\\s+ get > tags: line right below them
    assert '## Section\n> tags: ["test-tag", "item-1"]\n' in propagated
    assert '#\u00a0Heading with NBSP\n> tags: ["test-tag", "item-1"]\n' in propagated
    assert '# Conclusion\n> tags: ["test-tag", "item-1"]\n' in propagated
    # Indented heading is prose in memtomem chunker; must NOT get > tags:
    assert "   ### Indented heading prose\n" in propagated
    assert (
        "> tags:"
        not in propagated.split("   ### Indented heading prose\n")[1].split(
            "#\u00a0Heading with NBSP"
        )[0]
    )
    # Interior code comments must NOT have > tags:
    assert "# keep comment\n" in propagated
    assert "> tags:" not in propagated.split("```python\n")[1].split("```\n")[0]
    assert "    # indented comment\n" in propagated
    assert "\t# tab comment\n" in propagated
    assert "  \t# mixed comment\n" in propagated
    assert "> tags:" not in propagated.split("~~~bash\n")[1].split("~~~\n")[0]


def test_propagate_entry_tags_to_headings_fence_edge_cases():
    # 1. Trailing text on closing fence does NOT close fence
    raw_invalid_closer = (
        "```python\n"
        "# inside 1\n"
        "```closing with note\n"
        "# inside 2\n"
        "```\n"
        "# outside heading"
    )
    propagated = mb._propagate_entry_tags_to_headings(raw_invalid_closer, ["tag"])
    # `# inside 2` was inside the unclosed fence, so no tag was injected
    assert "# inside 2\n" in propagated
    assert '# outside heading\n> tags: ["tag"]\n' in propagated

    # 2. Indented code block starting with 4 spaces is not a fence opener
    raw_indented_fence = (
        "    ```python\n    # code inside 4-space block\n    ```\n# Heading outside"
    )
    propagated2 = mb._propagate_entry_tags_to_headings(raw_indented_fence, ["tag"])
    assert '# Heading outside\n> tags: ["tag"]\n' in propagated2
