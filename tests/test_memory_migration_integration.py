"""Opt-in bidirectional smoke for real memtomem and Mem0 migration."""

from __future__ import annotations

import os

import pytest

import syncmill.memory_migration as migration
from syncmill.config import MemoryConfig, SyncmillConfig
from syncmill.memory import run_async
from syncmill.memory_backend import Mem0Backend, MemtomemBackend

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    os.environ.get("SYNCMILL_TEST_MEMORY_MIGRATION_REAL") != "1",
    reason="set SYNCMILL_TEST_MEMORY_MIGRATION_REAL=1 to run the real migration smoke",
)
def test_real_bidirectional_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNCMILL_MIGRATIONS_DIR", str(tmp_path / "migrations"))
    mem0_dir = tmp_path / "mem0"
    first_db = tmp_path / "source.db"
    first = SyncmillConfig(
        memory={
            "db_path": str(first_db),
            "mem0": {"data_dir": str(mem0_dir)},
        }
    )

    source = MemtomemBackend(MemoryConfig(db_path=str(first_db)))
    try:
        run_async(
            source.add(
                "Migration smoke — 한글",
                tags=["migration", "unicode"],
                namespace="shared",
            )
        )
    finally:
        run_async(source.close())

    forward_id, _ = run_async(
        migration.plan_migration(first, "memtomem", "mem0")
    )
    assert run_async(migration.apply_migration(first, forward_id))["verified_count"] == 1
    assert run_async(migration.verify_migration(first, forward_id))["status"] == "verified"

    # Use the migrated Mem0 store as the reverse source and a fresh memtomem DB.
    reverse = SyncmillConfig(
        memory={
            "db_path": str(tmp_path / "reverse.db"),
            "mem0": {"data_dir": str(mem0_dir)},
        }
    )
    reverse_id, _ = run_async(
        migration.plan_migration(reverse, "mem0", "memtomem")
    )
    assert run_async(migration.apply_migration(reverse, reverse_id))["verified_count"] == 1

    target = migration._MemtomemMaintenance(reverse.memory.model_copy(update={"backend": "memtomem"}))
    try:
        rows = run_async(target.records())
        assert [(r.content, r.tags, r.namespace) for r in rows] == [
            ("Migration smoke — 한글", ["migration", "unicode"], "shared")
        ]
    finally:
        run_async(target.close())

    assert run_async(migration.rollback_migration(reverse, reverse_id))["deleted"] == 1
    assert run_async(migration.rollback_migration(first, forward_id))["deleted"] == 1

    reopened = Mem0Backend(
        MemoryConfig(backend="mem0", mem0={"data_dir": str(mem0_dir)})
    )
    run_async(reopened.close())
