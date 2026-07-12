"""MemoryBackend contract and the memtomem adapter boundary."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

import pytest

import syncmill.memory_backend as mb
from syncmill.config import MemoryConfig
from syncmill.memory import SyncmillMemory, run_async
from syncmill.memory_backend import (
    BackendCapabilities,
    Mem0Backend,
    MemoryBackend,
    MemtomemBackend,
)

from conftest import FakeBackend, make_memory


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


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(
        "memtomem.integrations.langgraph.MemtomemStore", _FakeMemtomemStore
    )
    return MemtomemBackend(MemoryConfig(db_path="/tmp/p1-memory.db"))


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
        }
    ]
    run_async(mem0_adapter.close())
    run_async(mem0_adapter.close())
    assert store.closed is True
    assert store.vector_store.client.closed is True


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
