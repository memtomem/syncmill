from __future__ import annotations

import json

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
