from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

import syncmill.memory_migration as mm
from syncmill.config import SyncmillConfig
from syncmill.memory import run_async


class FakeMaintenance:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows
        self.closed = False

    async def records(self):
        return list(self.rows.values())

    async def put(self, record, migration_id):
        target_id = f"target-{record.lineage_key[:12]}"
        self.rows[target_id] = mm._record(
            self.name,
            target_id,
            record.namespace,
            record.content,
            list(record.tags),
            record.created_at,
        )
        return target_id

    async def get(self, target_id):
        return self.rows.get(target_id)

    async def delete(self, target_id):
        return self.rows.pop(target_id, None) is not None

    async def close(self):
        self.closed = True


@pytest.fixture
def migration_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCMILL_MIGRATIONS_DIR", str(tmp_path / "migrations"))
    stores = {
        "memtomem": {
            "one": mm._record(
                "memtomem", "one", "shared", "한글 body", ["b", "a"], None
            ),
            "two": mm._record(
                "memtomem", "two", "agent-runtime:codex", "private", [], None
            ),
            "scratch": mm._record(
                "memtomem", "scratch", "board-run:gone", "scratch", [], None
            ),
        },
        "mem0": {},
    }
    opened = []

    def factory(config, backend):
        adapter = FakeMaintenance(backend, stores[backend])
        opened.append(adapter)
        return adapter

    monkeypatch.setattr(mm, "_maintenance", factory)
    return SyncmillConfig(), stores, opened


def test_plan_apply_verify_and_guarded_rollback(migration_env):
    config, stores, opened = migration_env
    migration_id, manifest = run_async(
        mm.plan_migration(config, "memtomem", "mem0")
    )
    assert manifest["record_count"] == 2
    artifact = mm.migration_root() / migration_id
    snapshot = json.loads((artifact / "snapshot.json").read_text())
    assert [r["namespace"] for r in snapshot["records"]] == [
        "agent-runtime:codex",
        "shared",
    ]
    assert all(a.closed for a in opened)

    receipt = run_async(mm.apply_migration(config, migration_id))
    assert receipt["status"] == "verified"
    assert receipt["verified_count"] == 2
    assert len(stores["mem0"]) == 2

    rerun = run_async(mm.apply_migration(config, migration_id))
    assert rerun["verified_count"] == 2
    assert len(stores["mem0"]) == 2

    result = run_async(mm.rollback_migration(config, migration_id))
    assert result == {
        "migration_id": migration_id,
        "status": "rolled_back",
        "deleted": 2,
        "refused": [],
    }
    assert stores["mem0"] == {}


def test_plan_refuses_nonempty_target_and_scratch_namespace(migration_env):
    config, stores, _ = migration_env
    stores["mem0"]["existing"] = mm._record(
        "mem0", "existing", "shared", "existing", [], None
    )
    with pytest.raises(mm.MigrationError, match="destination namespaces must be empty"):
        run_async(mm.plan_migration(config, "memtomem", "mem0"))
    with pytest.raises(mm.MigrationError, match=r"board-run:\*"):
        run_async(
            mm.plan_migration(
                config, "memtomem", "mem0", ("board-run:forbidden",)
            )
        )


def test_apply_refuses_source_drift(migration_env):
    config, stores, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    stores["memtomem"]["late"] = mm._record(
        "memtomem", "late", "shared", "late write", [], None
    )
    with pytest.raises(mm.MigrationError, match="source changed"):
        run_async(mm.apply_migration(config, migration_id))
    assert stores["mem0"] == {}


def test_rollback_refuses_changed_target(migration_env):
    config, stores, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    run_async(mm.apply_migration(config, migration_id))
    target_id = next(iter(stores["mem0"]))
    row = stores["mem0"][target_id]
    stores["mem0"][target_id] = mm._record(
        "mem0", target_id, row.namespace, "changed", row.tags, None
    )
    with pytest.raises(mm.MigrationError, match="refused changed"):
        run_async(mm.rollback_migration(config, migration_id))
    assert target_id in stores["mem0"]


def test_mem0_maintenance_scans_lineages_once_for_all_puts():
    existing = mm._record("memtomem", "s1", "shared", "one", ["x"], None)
    fresh = mm._record("memtomem", "s2", "shared", "two", [], None)
    point = SimpleNamespace(
        id="already-there",
        payload={
            "user_id": "shared",
            "data": "one",
            "syncmill_tags": ["x"],
            "syncmill_migration_lineage": existing.lineage_key,
        },
    )

    class Client:
        calls = 0

        def scroll(self, **kwargs):
            self.calls += 1
            return ([point], None)

    class Store:
        def __init__(self):
            self.client = Client()
            self.collection_name = "syncmill"
            self.adds = []

        async def add(self, messages, **kwargs):
            self.adds.append((messages, kwargs))
            return {"results": [{"id": "new-id", "memory": "two"}]}

    store = Store()
    adapter = mm._Mem0Maintenance.__new__(mm._Mem0Maintenance)
    adapter.backend = SimpleNamespace(
        _qdrant_store=lambda: store,
        _store=store,
    )
    adapter._lineage_ids = None

    assert run_async(adapter.put(existing, "migration")) == "already-there"
    assert run_async(adapter.put(fresh, "migration")) == "new-id"
    assert run_async(adapter.put(fresh, "migration")) == "new-id"
    assert store.client.calls == 1
    assert len(store.adds) == 1
    assert store.adds[0][1]["infer"] is False


def test_memtomem_maintenance_uses_deterministic_idempotent_import(monkeypatch):
    models = ModuleType("memtomem.models")

    class ChunkMetadata:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Chunk:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.embedding = None

    models.Chunk = Chunk
    models.ChunkMetadata = ChunkMetadata
    models.ChunkType = SimpleNamespace(RAW_TEXT="raw")
    monkeypatch.setitem(sys.modules, "memtomem.models", models)

    class Storage:
        def __init__(self):
            self.rows = {}
            self.upserts = 0

        async def get_chunk(self, chunk_id):
            return self.rows.get(chunk_id)

        async def upsert_chunks(self, chunks):
            self.upserts += 1
            for chunk in chunks:
                self.rows[chunk.id] = chunk

        async def delete_chunks(self, chunk_ids):
            return sum(self.rows.pop(chunk_id, None) is not None for chunk_id in chunk_ids)

    class Embedder:
        calls = 0

        async def embed_texts(self, texts):
            self.calls += 1
            return [[0.1, 0.2] for _ in texts]

    storage = Storage()
    embedder = Embedder()
    components = SimpleNamespace(storage=storage, embedder=embedder)
    backend = SimpleNamespace(
        _store=SimpleNamespace(_ensure_init=lambda: None),
    )

    async def ensure_init():
        return components

    backend._store._ensure_init = ensure_init
    adapter = mm._MemtomemMaintenance.__new__(mm._MemtomemMaintenance)
    adapter.backend = backend
    record = mm._record(
        "mem0", "source", "shared", "verbatim 한글", ["tag"],
        datetime.now(timezone.utc).isoformat(),
    )

    first = run_async(adapter.put(record, "migration"))
    second = run_async(adapter.put(record, "migration"))
    assert first == second
    assert storage.upserts == 1
    assert embedder.calls == 1
    saved = next(iter(storage.rows.values()))
    assert saved.content == "verbatim 한글"
    assert saved.metadata.namespace == "shared"
    assert saved.metadata.tags == ("tag",)
