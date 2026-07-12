"""Staged, source-retaining migration between syncmill memory backends."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from syncmill.config import MemoryConfig, SyncmillConfig

BackendName = Literal["memtomem", "mem0", "langgraph"]
_PERSISTENT_PREFIX = "agent-runtime:"
_SCRATCH_PREFIX = "board-run:"
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MigrationRecord:
    source_id: str
    namespace: str
    content: str
    tags: list[str]
    created_at: str | None
    lineage_key: str
    digest: str


class MigrationError(RuntimeError):
    """A load-bearing migration failure."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(namespace: str, content: str, tags: list[str]) -> str:
    return hashlib.sha256(
        _canonical({"namespace": namespace, "content": content, "tags": tags})
    ).hexdigest()


def _record(source: BackendName, source_id: str, namespace: str, content: str,
            tags: list[str], created_at: str | None) -> MigrationRecord:
    digest = _digest(namespace, content, tags)
    lineage = hashlib.sha256(
        _canonical({"source": source, "source_id": source_id, "digest": digest})
    ).hexdigest()
    return MigrationRecord(source_id, namespace, content, tags, created_at, lineage, digest)


def _aggregate(records: list[MigrationRecord]) -> str:
    return hashlib.sha256(
        _canonical([(r.lineage_key, r.digest) for r in records])
    ).hexdigest()


def migration_root() -> Path:
    return Path(os.environ.get("SYNCMILL_MIGRATIONS_DIR", "~/.syncmill/migrations")).expanduser()


def _atomic_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _artifact(migration_id: str) -> Path:
    if not migration_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in migration_id):
        raise MigrationError("invalid migration id")
    path = migration_root() / migration_id
    if not path.is_dir():
        raise MigrationError(f"migration not found: {migration_id}")
    return path


def _config_for(config: SyncmillConfig, backend: BackendName) -> MemoryConfig:
    return config.memory.model_copy(update={"backend": backend})


class _MemtomemMaintenance:
    """Migration-only adapter; sanctioned owner of memtomem maintenance internals.

    Runtime access remains isolated in ``MemtomemBackend``. Migration needs
    enumerate/import/delete primitives that the public backend contract does not
    expose, so this adapter is the single explicit maintenance exception.
    """
    name: BackendName = "memtomem"

    def __init__(self, config: MemoryConfig):
        from syncmill.memory_backend import MemtomemBackend
        self.backend = MemtomemBackend(config)

    async def records(self) -> list[MigrationRecord]:
        storage = await self.backend._storage()
        rows: list[MigrationRecord] = []
        for source in sorted(await storage.get_all_source_files()):
            for chunk in await storage.list_chunks_by_source(source, limit=100_000):
                rows.append(_record(
                    self.name, str(chunk.id), chunk.metadata.namespace, chunk.content,
                    list(chunk.metadata.tags), chunk.created_at.isoformat(),
                ))
        return rows

    async def put(self, record: MigrationRecord, migration_id: str) -> str:
        from memtomem.models import Chunk, ChunkMetadata, ChunkType

        comp = await self.backend._store._ensure_init()
        chunk_id = uuid5(NAMESPACE_URL, f"syncmill:{record.lineage_key}")
        existing = await comp.storage.get_chunk(chunk_id)
        if existing is not None:
            return str(chunk_id)
        created = (
            datetime.fromisoformat(record.created_at)
            if record.created_at else datetime.now(timezone.utc)
        )
        chunk = Chunk(
            id=chunk_id,
            content=record.content,
            created_at=created,
            metadata=ChunkMetadata(
                source_file=Path(f".syncmill-migration/{migration_id}/{record.lineage_key}.md"),
                chunk_type=ChunkType.RAW_TEXT,
                tags=tuple(record.tags),
                namespace=record.namespace,
            ),
        )
        chunk.embedding = (await comp.embedder.embed_texts([record.content]))[0]
        await comp.storage.upsert_chunks([chunk])
        return str(chunk_id)

    async def get(self, target_id: str) -> MigrationRecord | None:
        try:
            chunk = await (await self.backend._storage()).get_chunk(UUID(target_id))
        except (ValueError, TypeError):
            return None
        if chunk is None:
            return None
        return _record(
            self.name, str(chunk.id), chunk.metadata.namespace, chunk.content,
            list(chunk.metadata.tags), chunk.created_at.isoformat(),
        )

    async def delete(self, target_id: str) -> bool:
        try:
            return bool(await (await self.backend._storage()).delete_chunks([UUID(target_id)]))
        except (ValueError, TypeError):
            return False

    async def close(self) -> None:
        await self.backend.close()


class _Mem0Maintenance:
    name: BackendName = "mem0"

    def __init__(self, config: MemoryConfig):
        from syncmill.memory_backend import Mem0Backend
        self.backend = Mem0Backend(config)
        self._lineage_ids: dict[str, str] | None = None

    async def _scan(self) -> tuple[list[MigrationRecord], dict[str, str]]:
        store = self.backend._qdrant_store()
        offset = None
        rows: list[MigrationRecord] = []
        lineage_ids: dict[str, str] = {}
        while True:
            points, offset = await asyncio.to_thread(
                store.client.scroll,
                collection_name=store.collection_name,
                offset=offset,
                limit=100,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                if "syncmill_tags" not in payload:
                    continue
                namespace = payload.get("user_id")
                content = payload.get("data")
                if not isinstance(namespace, str) or not isinstance(content, str):
                    continue
                tags = payload.get("syncmill_tags")
                if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                    continue
                lineage = payload.get("syncmill_migration_lineage")
                if isinstance(lineage, str):
                    lineage_ids[lineage] = str(point.id)
                rows.append(_record(
                    self.name, str(point.id), namespace, content, list(tags),
                    payload.get("created_at") if isinstance(payload.get("created_at"), str) else None,
                ))
            if offset is None:
                break
        return rows, lineage_ids

    async def records(self) -> list[MigrationRecord]:
        rows, self._lineage_ids = await self._scan()
        return rows

    async def put(self, record: MigrationRecord, migration_id: str) -> str:
        # One destination scan reconciles any add/state crash window for the
        # whole apply. Cache it so N migrated records remain O(N), not O(N²).
        if self._lineage_ids is None:
            _, self._lineage_ids = await self._scan()
        existing = self._lineage_ids.get(record.lineage_key)
        if existing is not None:
            return existing
        response = await self.backend._store.add(
            [{"role": "user", "content": record.content}],
            user_id=record.namespace,
            metadata={
                "syncmill_tags": list(record.tags),
                "syncmill_migration_id": migration_id,
                "syncmill_migration_lineage": record.lineage_key,
                "syncmill_migration_digest": record.digest,
                **({"created_at": record.created_at} if record.created_at else {}),
            },
            infer=False,
        )
        results = response.get("results", []) if isinstance(response, dict) else []
        if len(results) != 1 or results[0].get("memory") != record.content:
            raise MigrationError("Mem0 did not confirm the migrated verbatim write")
        target_id = str(results[0]["id"])
        self._lineage_ids[record.lineage_key] = target_id
        return target_id

    async def get(self, target_id: str) -> MigrationRecord | None:
        raw = await self.backend._store.get(target_id)
        if not isinstance(raw, dict):
            return None
        namespace = raw.get("user_id")
        content = raw.get("memory")
        metadata = raw.get("metadata") or {}
        tags = metadata.get("syncmill_tags")
        if not isinstance(namespace, str) or not isinstance(content, str) or not isinstance(tags, list):
            return None
        return _record(self.name, target_id, namespace, content, list(tags), raw.get("created_at"))

    async def delete(self, target_id: str) -> bool:
        try:
            await self.backend._store.delete(target_id)
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.backend.close()


class _LangGraphMaintenance:
    """Migration maintenance adapter over LangGraph's persistent BaseStore."""

    name: BackendName = "langgraph"

    def __init__(self, config: MemoryConfig):
        from syncmill.memory_backend import LangGraphBackend

        self.backend = LangGraphBackend(config)

    async def records(self) -> list[MigrationRecord]:
        from syncmill.memory_backend import _LANGGRAPH_NS

        store = await self.backend._ensure_store()
        rows: list[MigrationRecord] = []
        offset = 0
        while True:
            items = await store.asearch(
                _LANGGRAPH_NS, query=None, limit=100, offset=offset
            )
            for item in items:
                row = self.backend._row(item)
                if row is None:
                    continue
                created = getattr(item, "created_at", None)
                rows.append(_record(
                    self.name,
                    row["id"],
                    row["namespace"],
                    row["content"],
                    list(row.get("tags", [])),
                    created.isoformat() if hasattr(created, "isoformat") else None,
                ))
            offset += len(items)
            if len(items) < 100:
                return rows

    async def put(self, record: MigrationRecord, migration_id: str) -> str:
        from syncmill.memory_backend import _LANGGRAPH_NS

        target_id = str(uuid5(NAMESPACE_URL, f"syncmill:{record.lineage_key}"))
        store = await self.backend._ensure_store()
        existing = await store.aget(_LANGGRAPH_NS, target_id)
        if existing is not None:
            return target_id
        value = self.backend._value(
            record.content, list(record.tags), record.namespace
        )
        value.update({
            "syncmill_migration_id": migration_id,
            "syncmill_migration_lineage": record.lineage_key,
            "syncmill_migration_digest": record.digest,
        })
        await store.aput(_LANGGRAPH_NS, target_id, value, index=["content"])
        return target_id

    async def get(self, target_id: str) -> MigrationRecord | None:
        row = await self.backend.get(target_id)
        if row is None:
            return None
        return _record(
            self.name,
            row["id"],
            row["namespace"],
            row["content"],
            list(row.get("tags", [])),
            None,
        )

    async def delete(self, target_id: str) -> bool:
        from syncmill.memory_backend import _LANGGRAPH_NS

        store = await self.backend._ensure_store()
        if await store.aget(_LANGGRAPH_NS, target_id) is None:
            return False
        await store.adelete(_LANGGRAPH_NS, target_id)
        return True

    async def close(self) -> None:
        await self.backend.close()


def _maintenance(config: SyncmillConfig, backend: BackendName):
    cfg = _config_for(config, backend)
    if backend == "memtomem":
        return _MemtomemMaintenance(cfg)
    if backend == "mem0":
        return _Mem0Maintenance(cfg)
    return _LangGraphMaintenance(cfg)


def _selected(record: MigrationRecord, namespaces: tuple[str, ...]) -> bool:
    if record.namespace.startswith(_SCRATCH_PREFIX):
        return False
    if namespaces:
        return record.namespace in namespaces
    return record.namespace == "shared" or record.namespace.startswith(_PERSISTENT_PREFIX)


async def plan_migration(
    config: SyncmillConfig,
    source: BackendName,
    target: BackendName,
    namespaces: tuple[str, ...] = (),
) -> tuple[str, dict[str, Any]]:
    if source == target:
        raise MigrationError("source and target backends must differ")
    if any(ns.startswith(_SCRATCH_PREFIX) for ns in namespaces):
        raise MigrationError("board-run:* namespaces are never migratable")
    src = _maintenance(config, source)
    dst = _maintenance(config, target)
    try:
        records = sorted(
            (r for r in await src.records() if _selected(r, namespaces)),
            key=lambda r: (r.namespace, r.source_id),
        )
        target_rows = [r for r in await dst.records() if _selected(r, namespaces)]
        if target_rows:
            raise MigrationError("selected destination namespaces must be empty")
    finally:
        await src.close()
        await dst.close()

    migration_id = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid4().hex[:8]
    path = migration_root() / migration_id
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    snapshot = {"schema_version": _SCHEMA_VERSION, "records": [asdict(r) for r in records]}
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "migration_id": migration_id,
        "source": source,
        "target": target,
        "namespaces": list(namespaces),
        "record_count": len(records),
        "source_digest": _aggregate(records),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(path / "snapshot.json", snapshot)
    _atomic_json(path / "manifest.json", manifest)
    _atomic_json(path / "state.json", {"status": "planned", "targets": {}, "errors": []})
    return migration_id, manifest


def _load(path: Path) -> tuple[dict[str, Any], list[MigrationRecord], dict[str, Any]]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    snapshot = json.loads((path / "snapshot.json").read_text(encoding="utf-8"))
    state = json.loads((path / "state.json").read_text(encoding="utf-8"))
    records = [MigrationRecord(**row) for row in snapshot["records"]]
    if _aggregate(records) != manifest["source_digest"]:
        raise MigrationError("migration snapshot digest mismatch")
    return manifest, records, state


async def apply_migration(config: SyncmillConfig, migration_id: str) -> dict[str, Any]:
    path = _artifact(migration_id)
    manifest, records, state = _load(path)
    src = _maintenance(config, manifest["source"])
    dst = _maintenance(config, manifest["target"])
    try:
        live = sorted(
            (r for r in await src.records() if _selected(r, tuple(manifest["namespaces"]))),
            key=lambda r: (r.namespace, r.source_id),
        )
        if _aggregate(live) != manifest["source_digest"]:
            raise MigrationError("source changed since migration plan")
        if not state["targets"]:
            existing = [r for r in await dst.records() if _selected(r, tuple(manifest["namespaces"]))]
            if existing:
                raise MigrationError("selected destination namespaces are no longer empty")
        state["status"] = "applying"
        _atomic_json(path / "state.json", state)
        for record in records:
            if record.lineage_key in state["targets"]:
                continue
            try:
                state["targets"][record.lineage_key] = await dst.put(record, migration_id)
                _atomic_json(path / "state.json", state)
            except Exception as exc:
                state["errors"].append({"lineage": record.lineage_key, "error": f"{type(exc).__name__}: {exc}"[:500]})
                state["status"] = "partial"
                _atomic_json(path / "state.json", state)
                raise MigrationError("migration apply was partial") from exc
    finally:
        await src.close()
        await dst.close()
    result = await verify_migration(config, migration_id)
    return result


async def verify_migration(config: SyncmillConfig, migration_id: str) -> dict[str, Any]:
    path = _artifact(migration_id)
    manifest, records, state = _load(path)
    dst = _maintenance(config, manifest["target"])
    failures: list[str] = []
    by_lineage = {r.lineage_key: r for r in records}
    try:
        for lineage, target_id in state["targets"].items():
            expected = by_lineage.get(lineage)
            actual = await dst.get(target_id)
            if expected is None or actual is None or actual.digest != expected.digest:
                failures.append(lineage)
    finally:
        await dst.close()
    missing_target_count = len(records) - len(state["targets"])
    state["status"] = (
        "verified" if not failures and missing_target_count == 0
        else "verification_failed"
    )
    _atomic_json(path / "state.json", state)
    receipt = {
        "schema_version": _SCHEMA_VERSION,
        "migration_id": migration_id,
        "status": state["status"],
        "source": manifest["source"],
        "target": manifest["target"],
        "record_count": len(records),
        "verified_count": len(records) - len(failures) - missing_target_count,
        "missing_target_count": missing_target_count,
        "source_digest": manifest["source_digest"],
        "failure_lineages": failures[:20],
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_json(path / "receipt.json", receipt)
    if failures or missing_target_count:
        raise MigrationError("migration verification failed")
    return receipt


async def rollback_migration(config: SyncmillConfig, migration_id: str) -> dict[str, Any]:
    path = _artifact(migration_id)
    manifest, records, state = _load(path)
    dst = _maintenance(config, manifest["target"])
    by_lineage = {r.lineage_key: r for r in records}
    deleted = 0
    refused: list[str] = []
    try:
        for lineage, target_id in list(state["targets"].items()):
            actual = await dst.get(target_id)
            expected = by_lineage.get(lineage)
            if actual is None:
                state["targets"].pop(lineage, None)
                continue
            if expected is None or actual.digest != expected.digest:
                refused.append(lineage)
                continue
            if await dst.delete(target_id):
                deleted += 1
                state["targets"].pop(lineage, None)
                _atomic_json(path / "state.json", state)
    finally:
        await dst.close()
    state["status"] = "rolled_back" if not refused and not state["targets"] else "rollback_partial"
    _atomic_json(path / "state.json", state)
    result = {"migration_id": migration_id, "status": state["status"], "deleted": deleted, "refused": refused[:20]}
    _atomic_json(path / "rollback.json", result)
    if state["status"] != "rolled_back":
        raise MigrationError("migration rollback refused changed target records")
    return result
