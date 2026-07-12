"""MemoryBackend contract and the memtomem adapter boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import syncmill.memory_backend as mb
from syncmill.config import MemoryConfig
from syncmill.memory import run_async
from syncmill.memory_backend import BackendCapabilities, MemoryBackend, MemtomemBackend

from conftest import FakeBackend


class _FakeStorage:
    def __init__(self):
        self.meta: dict[str, dict] = {}
        self.counts: dict[str, int] = {}
        self.set_calls: list[tuple[str, str]] = []
        self.deleted: list[str] = []

    async def get_namespace_meta(self, ns: str):
        return self.meta.get(ns)

    async def set_namespace_meta(self, ns: str, description: str = ""):
        self.meta[ns] = {"namespace": ns}
        self.set_calls.append((ns, description))

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


class _FakeMemtomemStore:
    last: "_FakeMemtomemStore | None" = None

    def __init__(self, config_overrides=None):
        type(self).last = self
        self._config_overrides = config_overrides or {}
        self.storage = _FakeStorage()
        self._components = SimpleNamespace(storage=self.storage)
        self.ready_calls = 0
        self.added: list[dict] = []
        self.searches: list[dict] = []
        self.events: list[tuple[str, str]] = []
        self.ended: list[str | None] = []
        self.closed = False

    async def _ensure_init(self):
        self.ready_calls += 1

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
        return {"id": chunk_id, "content": "hit", "namespace": "shared"}

    async def start_session(self, *, agent_id, namespace):
        return f"{agent_id}:{namespace}"

    async def log_event(self, kind, text):
        self.events.append((kind, text))

    async def end_session(self, *, summary=None):
        self.ended.append(summary)

    async def close(self):
        self.closed = True


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore", _FakeMemtomemStore
    )
    return MemtomemBackend(MemoryConfig(db_path="/tmp/p1-memory.db"))


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
    await backend.get("id-1")
    await backend.count_namespace("shared")
    assert await backend.start_session("codex", "shared")
    await backend.log_event("kind", "text")
    await backend.end_session("done")
    assert "db_path" in await backend.status()
    await backend.delete_namespace("shared")
    await backend.close()


def test_protocol_member_smoke_fake_and_adapter(adapter):
    run_async(_protocol_member_smoke(FakeBackend()))
    run_async(_protocol_member_smoke(adapter))


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

    assert store.added == [
        {"content": "body", "tags": ["one"], "namespace": "shared"}
    ]
    assert store.searches == [
        {"query": "a", "namespace": "shared", "top_k": 2},
        {
            "query": "b",
            "namespace": "shared",
            "top_k": 3,
            "tag_filter": "one,two",
        },
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
    assert run_async(adapter.delete_namespace("shared")) == 4
    assert store.storage.deleted == ["shared"]


def test_build_backend_constructs_memtomem(monkeypatch):
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore", _FakeMemtomemStore
    )
    backend = mb.build_backend(MemoryConfig())
    assert isinstance(backend, MemtomemBackend)


def test_build_backend_refuses_non_verbatim(monkeypatch):
    class NonVerbatim:
        capabilities = BackendCapabilities(verbatim_storage=False)

        def __init__(self, config):
            self.config = config

    monkeypatch.setattr(mb, "MemtomemBackend", NonVerbatim)
    with pytest.raises(ValueError, match="cannot store verbatim content"):
        mb.build_backend(MemoryConfig())
