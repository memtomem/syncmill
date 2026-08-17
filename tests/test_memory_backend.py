"""MemoryBackend contract and the memtomem adapter boundary."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

import pytest

import syncmill.memory_backend as mb
from syncmill.config import MemoryConfig
from syncmill.memory import SyncmillMemory, run_async
from syncmill.memory_backend import (
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

    async def get_namespace_meta(self, ns: str):
        return self.meta.get(ns)

    async def set_namespace_meta(self, ns: str, description=None, color=None):
        self.meta[ns] = {"namespace": ns}
        self.set_calls.append((ns, description))
        self.meta_upserts.append(
            {"ns": ns, "description": description, "color": color}
        )

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
            {"namespace": ns, "chunk_count": self.counts.get(ns, 0)}
            for ns in names
        ]

    async def delete_by_namespace(self, ns: str):
        self.deleted.append(ns)
        self.meta.pop(ns, None)
        return self.counts.pop(ns, 0)


class _FakeIndexEngine:
    def __init__(self):
        self.index_calls: list[dict] = []
        # scriptable per-call new_chunk_ids; () simulates the hash-collision /
        # short-chunk-merge edge where memtomem yields no fresh id
        self.next_chunk_ids: tuple[str, ...] = ("aaaaaaaa-1111-4111-8111-111111111111",)
        # shared ordering log the adapter fixture wires in (lock-span tests)
        self.events: list[str] | None = None

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
        return SimpleNamespace(
            new_chunk_ids=self.next_chunk_ids,
            indexed_chunks=len(self.next_chunk_ids),
        )


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

    def all_index_roots(self):
        return [*self.memory_dirs, *self.project_memory_dirs]


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

    def __call__(self, content, *, surface, audit_context=None, **kwargs):
        self.calls.append(
            {"content": content, "surface": surface, "audit_context": audit_context}
        )
        return SimpleNamespace(
            decision="blocked" if self.block else "allowed", hits=()
        )


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

        async def __aexit__(self, *exc):
            events.append("lock:release")

    async def fake_mix_refusal(**kwargs):
        mix_calls.append(kwargs)
        events.append("mix")
        return mix_result["value"]

    monkeypatch.setattr("memtomem.privacy.enforce_write_guard", guard)
    monkeypatch.setattr(
        "memtomem.tools.memory_writer.append_entry", fake_append_entry
    )
    monkeypatch.setattr(
        "memtomem.context._atomic.async_file_lock",
        lambda path, timeout=None: _FakeSidecarLock(path, timeout),
    )
    monkeypatch.setattr(
        "memtomem.memory_scope.namespace_mix_refusal", fake_mix_refusal
    )
    backend = MemtomemBackend(MemoryConfig(db_path="/tmp/p1-memory.db"))
    store = _FakeMemtomemStore.last
    store.guard = guard
    store.appended = appended
    store.events = events
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
    run_async(
        adapter.search("b", namespace="shared", top_k=3, tag_filter="one,two")
    )

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
        adapter.search_multi(
            "q2", namespaces=["shared"], top_k=3, tag_filter="one"
        )
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
        run_async(
            adapter.search_multi("q", namespaces=["shared", "a,b"], top_k=5)
        )
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
        adapter.add_returning_ids(
            "body", tags=["t"], namespace="agent-runtime:codex"
        )
    )
    assert ids
    assert store.events == [
        "lock:acquire",
        "mix",
        "append",
        "index",
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
    assert store.events == ["lock:acquire", "mix", "lock:release"]


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
        "memtomem.memory_scope.require_user_base", lambda dirs: user
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
        tmp_path / f"backup{native[len('2026-08-01'):]}",
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
    def _boom(dirs):
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
    real_sweep = mb._sweep_namespace_sources

    def logging_sweep(config, ns):
        store.events.append("sweep")
        return real_sweep(config, ns)

    monkeypatch.setattr(mb, "_sweep_namespace_sources", logging_sweep)

    assert run_async(adapter.purge_namespace("board-run:r1")) == 1
    assert store.storage.deleted == ["board-run:r1"]

    events = store.events
    assert events.count("lock:acquire") == events.count("lock:release") == 2
    # Sources, rows and the verify sweep ALL sit between the last acquire and
    # the first release — that is what "atomic against a syncmill writer" means.
    last_acquire = max(i for i, e in enumerate(events) if e == "lock:acquire")
    first_release = min(i for i, e in enumerate(events) if e == "lock:release")
    inside = [e for e in events[last_acquire:first_release] if e != "lock:acquire"]
    assert inside == ["sweep", "delete_rows", "sweep"]


def test_purge_namespace_locks_today_and_tomorrows_writer_targets(
    adapter, tmp_path, monkeypatch
):
    # A writer reading the clock just before a UTC midnight appends just after,
    # to TOMORROW's file. Locking only today's would leave that write free to
    # land mid-purge, which is the race this method exists to close.
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

    today = datetime.now(timezone.utc)
    tomorrow = today + timedelta(days=1)
    names = {path.name for path in taken}
    for stamp in (today, tomorrow):
        target = f"{stamp.strftime('%Y-%m-%d')}.board-run.r1.md"
        assert f".{target}.lock" in names
    # Upstream's L2 rule for multi-acquire is sorted order; anything else can
    # deadlock against another sorted acquirer holding one of the same locks.
    assert [str(p) for p in taken] == sorted(str(p) for p in taken)


def test_purge_namespace_raises_when_a_writer_it_cannot_exclude_lands_a_file(
    adapter, tmp_path, monkeypatch
):
    # A memtomem-NATIVE write to the same namespace has an unpredictable file
    # name, so its sidecar cannot be pre-acquired. The verify sweep is what
    # catches it, and an unprovable purge must raise rather than report a count
    # — the caller tombstones on success, and a namespace with a live producer
    # must stay collectable.
    _point_index_roots(monkeypatch, tmp_path)
    real_sweep = mb._sweep_namespace_sources
    calls = {"n": 0}

    def sweeping(config, ns):
        calls["n"] += 1
        removed = real_sweep(config, ns)
        if calls["n"] == 1:  # a writer lands one between the delete and the check
            (tmp_path / "2026-08-02.board-run.r1.md").write_text("late")
        return removed

    monkeypatch.setattr(mb, "_sweep_namespace_sources", sweeping)
    with pytest.raises(RuntimeError, match="not provably complete"):
        run_async(adapter.purge_namespace("board-run:r1"))
    # The rows still went: leaving them would strand markdown with no rows, the
    # exact leak this path prevents. The namespace simply stays collectable.
    assert _FakeMemtomemStore.last.storage.deleted == ["board-run:r1"]


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
        run_async(adapter.add_returning_ids("body", tags=[], namespace="shared"))
        == []
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
        MemoryConfig(
            backend="mem0", mem0={"data_dir": str(tmp_path / "mem0")}
        )
    )
    assert isinstance(backend, Mem0Backend)


def test_mem0_verbatim_mapping_search_and_close(mem0_adapter):
    store = _FakeMem0Memory.last
    content = "한글\nverbatim"
    run_async(mem0_adapter.add(content, tags=["one", "two"], namespace="shared"))
    rows = run_async(
        mem0_adapter.search(
            "query", namespace="shared", top_k=3, tag_filter="one,two"
        )
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
    assert (
        run_async(
            mem0_adapter.add_returning_ids(
                "c", tags=["t"], namespace="agent-runtime:claude"
            )
        )
        == ["id-1"]
    )
    store.rows["id-1"] = {
        "id": "id-1",
        "memory": "the finding",
        "user_id": "agent-runtime:claude",
        "metadata": {"syncmill_tags": ["task-result", "shared-from=old-parent"]},
    }
    assert run_async(mem0_adapter.share_chunk("id-1", target_namespace="shared")) == "id-1"
    copy = store.added[-1]
    assert copy["user_id"] == "shared"
    assert copy["messages"][0]["content"] == "the finding"
    # collapsed shared-from chain, no link row (share_provenance_links=False)
    assert copy["metadata"]["syncmill_tags"] == ["task-result", "shared-from=id-1"]
    assert (
        run_async(mem0_adapter.share_chunk("absent", target_namespace="shared"))
        is None
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
                        "vector_store": {
                            "config": {"embedding_model_dims": 384}
                        },
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
            MemoryConfig(
                backend="mem0", mem0={"data_dir": str(tmp_path / "mem0")}
            )
        )
    memory = SyncmillMemory(
        MemoryConfig(
            backend="mem0", mem0={"data_dir": str(tmp_path / "status-mem0")}
        )
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
        return None if value is None else SimpleNamespace(key=key, value=value, score=None)

    async def asearch(self, namespace, *, query=None, filter=None, limit=10, offset=0):
        matches = []
        for (row_ns, key), value in self.rows.items():
            if row_ns != namespace:
                continue
            if filter and any(value.get(k) != v for k, v in filter.items()):
                continue
            matches.append(SimpleNamespace(key=key, value=value, score=0.8 if query else None))
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
        return [SimpleNamespace(key=f"k{i}", value={}, score=None) for i in range(limit)]

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
        run_async(backend.share_chunk("missing-key", target_namespace="shared"))
        is None
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
