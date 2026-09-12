from __future__ import annotations

import asyncio
import json
import os
import threading
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


def test_rollback_refuses_wrong_physical_destination_and_preserves_ledger(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNCMILL_MIGRATIONS_DIR", str(tmp_path / "migrations"))
    source_db = str(tmp_path / "source.db")
    target_a = str(tmp_path / "target-a")
    target_b = str(tmp_path / "target-b")
    stores = {
        ("memtomem", source_db): {
            "one": mm._record("memtomem", "one", "shared", "body", [], None)
        },
        ("mem0", target_a): {},
        ("mem0", target_b): {},
    }
    opened = []

    def factory(config, backend):
        locator = (
            config.memory.db_path
            if backend == "memtomem"
            else config.memory.mem0.data_dir
        )
        adapter = FakeMaintenance(backend, stores[(backend, locator)])
        opened.append((backend, locator, adapter))
        return adapter

    monkeypatch.setattr(mm, "_maintenance", factory)
    config_a = SyncmillConfig(
        memory={"db_path": source_db, "mem0": {"data_dir": target_a}}
    )
    config_b = SyncmillConfig(
        memory={"db_path": source_db, "mem0": {"data_dir": target_b}}
    )
    migration_id, _ = run_async(mm.plan_migration(config_a, "memtomem", "mem0"))
    artifact = mm.migration_root() / migration_id
    planned_state = (artifact / "state.json").read_bytes()
    opened_before = len(opened)
    with pytest.raises(mm.MigrationError, match="destination storage configuration"):
        run_async(mm.apply_migration(config_b, migration_id))
    assert len(opened) == opened_before
    assert (artifact / "state.json").read_bytes() == planned_state
    assert stores[("mem0", target_a)] == stores[("mem0", target_b)] == {}

    run_async(mm.apply_migration(config_a, migration_id))
    state_before = (artifact / "state.json").read_bytes()
    receipt_before = (artifact / "receipt.json").read_bytes()
    opened_before = len(opened)

    with pytest.raises(mm.MigrationError, match="destination storage configuration"):
        run_async(mm.verify_migration(config_b, migration_id))
    assert len(opened) == opened_before
    assert (artifact / "state.json").read_bytes() == state_before
    assert (artifact / "receipt.json").read_bytes() == receipt_before

    target_id, row = next(iter(stores[("mem0", target_a)].items()))
    stores[("mem0", target_b)][target_id] = row
    with pytest.raises(mm.MigrationError, match="destination storage configuration"):
        run_async(mm.rollback_migration(config_b, migration_id))

    assert len(opened) == opened_before
    assert (artifact / "state.json").read_bytes() == state_before
    assert not (artifact / "rollback.json").exists()
    assert target_id in stores[("mem0", target_a)]
    assert target_id in stores[("mem0", target_b)]

    result = run_async(mm.rollback_migration(config_a, migration_id))
    assert result["deleted"] == 1
    assert stores[("mem0", target_a)] == {}
    assert target_id in stores[("mem0", target_b)]


def test_destination_binding_is_secret_free_and_ignores_irrelevant_mem0_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNCMILL_MIGRATIONS_DIR", str(tmp_path / "migrations"))
    base = {
        "data_dir": str(tmp_path / "mem0"),
        "telemetry": False,
        "config": {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "url": "https://qdrant.example/v1/",
                    "api_key": "first-secret",
                    "collection_name": "syncmill",
                },
            },
            "history_db_path": str(tmp_path / "history.db"),
        },
    }
    config_a = SyncmillConfig(memory={"mem0": base})
    changed = json.loads(json.dumps(base))
    changed["telemetry"] = True
    changed["config"]["vector_store"]["config"]["api_key"] = "rotated-secret"
    changed["config"]["embedder"] = {
        "provider": "fastembed",
        "config": {
            "model": "another-model",
            "embedding_dims": 384,
        },
    }
    config_b = SyncmillConfig(memory={"mem0": changed})

    digest = mm._target_storage_digest(config_a, "mem0")
    assert digest == mm._target_storage_digest(config_b, "mem0")

    different = json.loads(json.dumps(base))
    different["config"]["vector_store"]["config"]["collection_name"] = "other"
    assert digest != mm._target_storage_digest(
        SyncmillConfig(memory={"mem0": different}), "mem0"
    )

    stores = {
        "memtomem": {
            "one": mm._record("memtomem", "one", "shared", "body", [], None)
        },
        "mem0": {},
    }
    monkeypatch.setattr(
        mm, "_maintenance", lambda config, backend: FakeMaintenance(backend, stores[backend])
    )
    migration_id, _ = run_async(mm.plan_migration(config_a, "memtomem", "mem0"))
    manifest_text = (
        mm.migration_root() / migration_id / "manifest.json"
    ).read_text(encoding="utf-8")
    assert "first-secret" not in manifest_text
    assert "qdrant.example" not in manifest_text
    assert str(tmp_path) not in manifest_text


def test_legacy_artifact_fails_closed_before_opening_destination(migration_env):
    config, _, opened = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    artifact = mm.migration_root() / migration_id
    manifest = json.loads((artifact / "manifest.json").read_text())
    snapshot = json.loads((artifact / "snapshot.json").read_text())
    manifest["schema_version"] = 1
    manifest.pop("storage_binding_version")
    manifest.pop("target_storage_digest")
    snapshot["schema_version"] = 1
    mm._atomic_json(artifact / "manifest.json", manifest)
    mm._atomic_json(artifact / "snapshot.json", snapshot)
    state_before = (artifact / "state.json").read_bytes()
    opened_before = len(opened)

    for operation in (mm.apply_migration, mm.verify_migration, mm.rollback_migration):
        with pytest.raises(mm.MigrationError, match="legacy migration artifact"):
            run_async(operation(config, migration_id))

    assert len(opened) == opened_before
    assert (artifact / "state.json").read_bytes() == state_before
    assert not (artifact / "receipt.json").exists()
    assert not (artifact / "rollback.json").exists()

    state = json.loads((artifact / "state.json").read_text())
    state["targets"] = {"legacy-lineage": "legacy-target-id"}
    mm._atomic_json(artifact / "state.json", state)
    with pytest.raises(mm.MigrationError, match=r"state\.json targets"):
        run_async(mm.rollback_migration(config, migration_id))
    assert len(opened) == opened_before


def test_legacy_artifact_is_rejected_before_the_digest_check(migration_env):
    """The legacy verdict is the boundary's, not a consumer's — so it wins over
    every later check in ``_load``, and a corrupt v1 artifact still says why it
    is unusable instead of reporting an incidental digest mismatch."""
    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    artifact = mm.migration_root() / migration_id
    manifest = json.loads((artifact / "manifest.json").read_text())
    snapshot = json.loads((artifact / "snapshot.json").read_text())
    manifest["schema_version"] = 1
    manifest.pop("storage_binding_version")
    manifest.pop("target_storage_digest")
    manifest["source_digest"] = "0" * 64
    snapshot["schema_version"] = 1
    mm._atomic_json(artifact / "manifest.json", manifest)
    mm._atomic_json(artifact / "snapshot.json", snapshot)

    with pytest.raises(mm.MigrationError, match="legacy migration artifact"):
        run_async(mm.apply_migration(config, migration_id))


def test_destination_binding_normalizes_paths_and_rejects_unsafe_mem0(tmp_path):
    path = tmp_path / "store.db"
    alias = tmp_path / "nested" / ".." / "store.db"
    first = SyncmillConfig(memory={"langgraph": {"db_path": str(path)}})
    second = SyncmillConfig(memory={"langgraph": {"db_path": str(alias)}})
    assert mm._target_storage_digest(first, "langgraph") == mm._target_storage_digest(
        second, "langgraph"
    )

    with pytest.raises(mm.MigrationError, match="in-memory stores"):
        mm._target_storage_digest(
            SyncmillConfig(memory={"langgraph": {"db_path": ":memory:"}}),
            "langgraph",
        )
    with pytest.raises(mm.MigrationError, match="programmatic Mem0 clients"):
        mm._target_storage_digest(
            SyncmillConfig(
                memory={
                    "mem0": {
                        "config": {
                            "vector_store": {
                                "config": {"client": SimpleNamespace()}
                            }
                        }
                    }
                }
            ),
            "mem0",
        )


@pytest.mark.parametrize(
    ("vector_config", "message"),
    [
        (
            {
                "url": "https://qdrant.example",
                "host": "qdrant.example",
                "port": 6333,
                "api_key": "secret",
            },
            "either url or host",
        ),
        ({"host": "qdrant.example"}, "requires an explicit port"),
        (
            {"url": "https://user:pass@qdrant.example", "api_key": "secret"},
            "uncredentialed http",
        ),
        (
            {"url": "https://qdrant.example?tenant=a", "api_key": "secret"},
            "uncredentialed http",
        ),
        ({"api_key": "secret"}, "requires an explicit url or host"),
        ({"https": True}, "requires an explicit url or host"),
        (
            {"host": "qdrant.example", "port": "not-a-port"},
            "invalid Mem0 destination configuration",
        ),
    ],
)
def test_mem0_destination_binding_rejects_ambiguous_config(
    vector_config, message
):
    config = SyncmillConfig(
        memory={
            "mem0": {
                "config": {
                    "vector_store": {
                        "provider": "qdrant",
                        "config": vector_config,
                    }
                }
            }
        }
    )
    with pytest.raises(mm.MigrationError, match=message):
        mm._target_storage_digest(config, "mem0")


def test_mem0_destination_binding_includes_history_database(tmp_path):
    vector = {
        "provider": "qdrant",
        "config": {
            "url": "https://qdrant.example",
            "api_key": "secret",
            "collection_name": "syncmill",
        },
    }
    first = SyncmillConfig(
        memory={
            "mem0": {
                "config": {
                    "vector_store": vector,
                    "history_db_path": str(tmp_path / "history-a.db"),
                }
            }
        }
    )
    second = SyncmillConfig(
        memory={
            "mem0": {
                "config": {
                    "vector_store": vector,
                    "history_db_path": str(tmp_path / "history-b.db"),
                }
            }
        }
    )
    assert mm._target_storage_digest(first, "mem0") != mm._target_storage_digest(
        second, "mem0"
    )


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


def test_memtomem_maintenance_uses_deterministic_idempotent_import(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SYNCMILL_MIGRATIONS_DIR", str(tmp_path / "migrations"))
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
    # The source file must really exist: memtomem's orphan detection deletes the
    # chunks of any indexed source_file that is missing from disk (the scheduled
    # compaction job, the health watchdog, and mem_cleanup_orphans all do), so a
    # synthetic path would have every migrated chunk swept on the next run.
    source = saved.metadata.source_file
    assert source.is_absolute() and source.exists()
    assert source.read_text(encoding="utf-8") == "verbatim 한글"
    assert (tmp_path / "migrations") in source.parents


def test_langgraph_maintenance_enumerates_and_idempotently_imports(monkeypatch):
    import syncmill.memory_backend as mb

    class Store:
        def __init__(self):
            self.rows = {}
            self.puts = 0

        async def asearch(self, namespace, **kwargs):
            offset = kwargs.get("offset", 0)
            limit = kwargs.get("limit", 100)
            return list(self.rows.values())[offset:offset + limit]

        async def aget(self, namespace, key):
            return self.rows.get(key)

        async def aput(self, namespace, key, value, **kwargs):
            self.puts += 1
            self.rows[key] = SimpleNamespace(
                key=key, value=value, score=None, created_at=None
            )

        async def adelete(self, namespace, key):
            self.rows.pop(key, None)

    store = Store()

    class Backend:
        async def _ensure_store(self):
            return store

        _value = staticmethod(mb.LangGraphBackend._value)
        _row = staticmethod(mb.LangGraphBackend._row)

        async def get(self, target_id):
            return self._row(await store.aget(mb._LANGGRAPH_NS, target_id))

        async def close(self):
            return None

    adapter = mm._LangGraphMaintenance.__new__(mm._LangGraphMaintenance)
    adapter.backend = Backend()
    record = mm._record("mem0", "source", "shared", "body", ["tag"], None)

    first = run_async(adapter.put(record, "migration"))
    second = run_async(adapter.put(record, "migration"))
    assert first == second
    assert store.puts == 1
    assert [row.content for row in run_async(adapter.records())] == ["body"]
    assert run_async(adapter.get(first)).digest == record.digest
    assert run_async(adapter.delete(first)) is True
    assert run_async(adapter.delete(first)) is False


# ── artifact permissions + concurrent state writes ──────────────────────────


def test_migration_root_is_private(migration_env):
    """`mkdir(parents=True, mode=0o700)` applies the mode to the LEAF only, so the
    migrations root itself was left at the default umask — world-readable, while
    every directory under it holds verbatim memory content."""
    import stat

    config, _, _ = migration_env
    previous = os.umask(0o022)
    try:
        migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    finally:
        os.umask(previous)
    root = mm.migration_root()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / migration_id).stat().st_mode) == 0o700


def test_atomic_json_concurrent_writers_do_not_corrupt(tmp_path):
    """Two writers overlapping on one `state.json` must not publish a mix.

    Behavioural, not name-shaped: both are held inside the write with a barrier, so
    with the old fixed `.tmp` sibling they share one temp path and the second
    write's bytes land under the first's `os.replace`. Whoever wins, the file must
    parse and must equal ONE of the two payloads in full."""
    workdir = tmp_path / "artifact"
    workdir.mkdir()
    target = workdir / "state.json"
    target.write_text("{}\n", encoding="utf-8")
    both_open = threading.Barrier(2, timeout=10)
    real_replace = os.replace
    errors: list[BaseException] = []

    def slow_replace(src, dst):
        both_open.wait()  # both temps written before either is published
        return real_replace(src, dst)

    def writer(payload):
        try:
            mm._atomic_json(target, payload)
        except BaseException as exc:  # surfaced below, never swallowed
            errors.append(exc)

    mm.os.replace = slow_replace
    try:
        threads = [
            threading.Thread(target=writer, args=({"status": "a", "targets": {"x": 1}},)),
            threading.Thread(target=writer, args=({"status": "b", "targets": {"y": 2}},)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
    finally:
        mm.os.replace = real_replace
    assert errors == []
    published = json.loads(target.read_text())  # parses at all == not torn
    assert published in (
        {"status": "a", "targets": {"x": 1}},
        {"status": "b", "targets": {"y": 2}},
    )
    assert [p.name for p in workdir.iterdir()] == ["state.json"]


def test_atomic_json_cleans_up_on_failure(tmp_path, monkeypatch):
    workdir = tmp_path / "artifact"
    workdir.mkdir()
    target = workdir / "state.json"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(mm.os, "replace", boom)
    with pytest.raises(OSError):
        mm._atomic_json(target, {"status": "a"})
    assert not target.exists()
    assert list(workdir.iterdir()) == []  # no temp left behind, hidden or not


def test_migration_root_is_tightened_on_read_paths_too(migration_env):
    """An installation that only applies/verifies would otherwise keep a
    permissive root forever — the tightening rode the plan path alone."""
    import stat

    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    root = mm.migration_root()
    os.chmod(root, 0o755)  # simulate a root created by an older version
    mm._artifact(migration_id)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_overlapping_migration_commands_fail_fast(migration_env):
    """`apply`/`verify`/`rollback` each load the WHOLE state document and rewrite
    it, so an overlap makes the slower one publish a `targets` map that predates
    the other's work — orphaning imported rows from any later rollback. Unique temp
    names never addressed that; the lock does.

    Both holders use the SAME migration — an earlier version of this test used two
    different id keys, which let an id-based re-entrancy shortcut pass it while a
    second task with the real id still walked straight through the lock."""
    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    path = mm.migration_root() / migration_id

    entered = threading.Event()
    release = threading.Event()
    outcome: list[BaseException | None] = []

    def holder():
        with mm._migration_lock(path):
            entered.set()
            release.wait(timeout=10)

    def contender():
        try:
            with mm._migration_lock(path):
                outcome.append(None)
        except BaseException as exc:
            outcome.append(exc)

    t1 = threading.Thread(target=holder)
    t1.start()
    assert entered.wait(timeout=10)
    t2 = threading.Thread(target=contender)
    t2.start()
    t2.join(timeout=10)
    release.set()
    t1.join(timeout=10)
    # Fail fast, not block: a CLI that hangs with no output is the worse failure,
    # and a blocking acquire on the shared event loop's thread would wedge it.
    assert isinstance(outcome[0], mm.MigrationError)
    assert "already running" in str(outcome[0])


def test_concurrent_migration_coroutines_do_not_wedge_the_loop(migration_env):
    """Two tasks on the ONE shared module-level event loop.

    A blocking acquire here is a deadlock, not a wait: the second task blocks the
    loop the first needs to finish and release. Failing fast keeps the loop live —
    the holder completes and the contender reports contention."""
    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    path = mm.migration_root() / migration_id

    async def hold():
        with mm._migration_lock(path):
            await asyncio.sleep(0.05)  # only completes if the loop still runs
        return "held"

    async def contend():
        await asyncio.sleep(0.01)  # after hold() has the lock
        with mm._migration_lock(path):
            return "acquired"

    async def both():
        return await asyncio.gather(hold(), contend(), return_exceptions=True)

    held, contended = run_async(both())
    assert held == "held"
    assert isinstance(contended, mm.MigrationError)


def test_apply_verifies_without_re_entering_its_own_lock(migration_env):
    """`apply` needs a verify at the end, and the lock is deliberately NOT
    re-entrant, so it must call the locked body directly. If it re-acquired, this
    would deadlock against itself (a flock belongs to the open file description, so
    a second acquire from the same process conflicts)."""
    config, stores, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    receipt = run_async(mm.apply_migration(config, migration_id))
    assert receipt["status"] == "verified"
    assert len(stores["mem0"]) == 2


def test_lock_survives_a_restrictive_umask(migration_env):
    """The lock file's 0600 is umask-masked at creation.

    Left at 0400 under `umask 0200`, it can never be reopened O_RDWR — so the
    FIRST command would lock the migration out permanently."""
    import stat

    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    path = mm.migration_root() / migration_id

    previous = os.umask(0o200)
    try:
        with mm._migration_lock(path):
            pass
    finally:
        os.umask(previous)

    lock = path / ".lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    with mm._migration_lock(path):  # a second command still gets in
        pass


def test_lock_repairs_an_under_permissioned_lock_file(migration_env):
    """An existing lock file left under-permissioned by an older version is
    repaired rather than locking the migration out forever."""
    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    path = mm.migration_root() / migration_id
    lock = path / ".lock"
    lock.write_bytes(b"")
    os.chmod(lock, 0o400)

    with mm._migration_lock(path):
        pass


def test_lock_does_not_repair_an_unrelated_open_failure(migration_env):
    """Repairing on ANY OSError chmod'ed unrelated failures.

    A `.lock` DIRECTORY fails the open with EISDIR, which is not a permission
    problem — the old code chmod'ed it to 0600 anyway, mutating something it did
    not understand and burying the real cause."""
    import stat as _stat

    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    path = mm.migration_root() / migration_id
    lock = path / ".lock"
    lock.mkdir(mode=0o755)

    with pytest.raises(mm.MigrationError, match="cannot open the migration lock"):
        with mm._migration_lock(path):
            pass
    info = lock.stat()
    assert _stat.S_ISDIR(info.st_mode)
    assert _stat.S_IMODE(info.st_mode) == 0o755  # untouched, not chmod'ed to 0600


def test_lock_refuses_repairing_a_non_regular_path(migration_env):
    """The repair path itself must not follow a symlink into someone else's file.

    An unreadable target makes the open fail with EACCES — the one failure the
    repair handles — so without the lstat guard the chmod would land on whatever
    the link points at."""
    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    path = mm.migration_root() / migration_id
    victim = path / "victim"
    victim.write_text("someone else's file", encoding="utf-8")
    os.chmod(victim, 0o000)
    (path / ".lock").symlink_to(victim)

    with pytest.raises(mm.MigrationError, match="not a regular file"):
        with mm._migration_lock(path):
            pass
    import stat as _stat

    assert _stat.S_IMODE(victim.stat().st_mode) == 0o000  # never chmod'ed


def test_lock_refuses_a_readable_symlink_lock_path(migration_env):
    """A READABLE symlink opens fine, so a check that only ran after a permission
    failure left the fast path unguarded — and `_chmod_lock` would then set the
    link TARGET (someone else's file) to 0600."""
    import stat as _stat

    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    path = mm.migration_root() / migration_id
    victim = path / "victim"
    victim.write_text("someone else's file", encoding="utf-8")
    os.chmod(victim, 0o644)
    (path / ".lock").symlink_to(victim)

    with pytest.raises(mm.MigrationError, match="not a regular file"):
        with mm._migration_lock(path):
            pass
    assert _stat.S_IMODE(victim.stat().st_mode) == 0o644  # never chmod'ed


@pytest.mark.parametrize("operation", ["apply", "verify", "rollback"])
def test_public_operations_take_the_lock(migration_env, operation):
    """WIRING: the contention tests drive `_migration_lock` directly, so deleting
    the `with _migration_lock(...)` from the public commands left them green."""
    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    path = mm.migration_root() / migration_id
    entered = threading.Event()
    release = threading.Event()

    def holder():
        with mm._migration_lock(path):
            entered.set()
            release.wait(timeout=10)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert entered.wait(timeout=10)
        command = getattr(mm, f"{operation}_migration")
        with pytest.raises(mm.MigrationError, match="already running"):
            run_async(command(config, migration_id))
    finally:
        release.set()
        t.join(timeout=10)


def _failing_factory(monkeypatch, closed, *, raise_on=None, close_raises=()):
    """Wrap `_maintenance` so construction and/or close can be made to fail."""
    real = mm._maintenance

    def factory(config, backend):
        if backend == raise_on:
            raise RuntimeError(f"cannot open {backend}")
        adapter = real(config, backend)
        real_close = adapter.close

        async def close():
            closed.append(backend)
            await real_close()
            if backend in close_raises:
                raise RuntimeError(f"close failed for {backend}")

        adapter.close = close
        return adapter

    monkeypatch.setattr(mm, "_maintenance", factory)


def test_plan_closes_the_source_when_the_destination_cannot_be_built(
    migration_env, monkeypatch
):
    """`dst`'s construction raises AFTER `src` exists — without registration-as-
    created the source keeps its handle (and an embedded store's lock) against the
    retry this failure invites."""
    config, _, _ = migration_env
    closed: list[str] = []
    _failing_factory(monkeypatch, closed, raise_on="mem0")
    with pytest.raises(RuntimeError, match="cannot open mem0"):
        run_async(mm.plan_migration(config, "memtomem", "mem0"))
    assert closed == ["memtomem"]


def test_plan_closes_both_adapters_when_one_close_fails(migration_env, monkeypatch):
    """A failing `src.close()` in a plain `finally` skipped `dst.close()` entirely."""
    config, _, _ = migration_env
    closed: list[str] = []
    _failing_factory(monkeypatch, closed, close_raises={"mem0"})
    with pytest.raises(RuntimeError, match="close failed"):
        run_async(mm.plan_migration(config, "memtomem", "mem0"))
    assert sorted(closed) == ["mem0", "memtomem"]


def test_apply_closes_the_source_when_the_destination_cannot_be_built(
    migration_env, monkeypatch
):
    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    closed: list[str] = []
    _failing_factory(monkeypatch, closed, raise_on="mem0")
    with pytest.raises(RuntimeError, match="cannot open mem0"):
        run_async(mm.apply_migration(config, migration_id))
    assert closed == ["memtomem"]


def test_apply_closes_both_adapters_when_one_close_fails(migration_env, monkeypatch):
    config, _, _ = migration_env
    migration_id, _ = run_async(mm.plan_migration(config, "memtomem", "mem0"))
    closed: list[str] = []
    _failing_factory(monkeypatch, closed, close_raises={"mem0"})
    with pytest.raises(RuntimeError, match="close failed"):
        run_async(mm.apply_migration(config, migration_id))
    assert sorted(closed) == ["mem0", "memtomem"]
