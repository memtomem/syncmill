"""Staged, source-retaining migration between syncmill memory backends."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from syncmill.artifacts import (
    ApplyLockBusy,
    ArtifactError,
    _lock_file_descriptor,
    _unlock_file_descriptor,
)
from syncmill.config import MemoryConfig, SyncmillConfig

BackendName = Literal["memtomem", "mem0", "langgraph"]
_PERSISTENT_PREFIX = "agent-runtime:"
_SCRATCH_PREFIX = "board-run:"
_SCHEMA_VERSION = 2
_STORAGE_BINDING_VERSION = 1


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


def _storage_path(value: str) -> str:
    if value == ":memory:":
        raise MigrationError("in-memory stores cannot be migration destinations")
    return str(Path(value).expanduser().resolve(strict=False))


def _mem0_locator_unchecked(config: MemoryConfig) -> dict[str, Any]:
    from syncmill.memory_backend import _resolve_mem0_config

    try:
        _, resolved = _resolve_mem0_config(config)
    except (TypeError, ValueError) as exc:
        raise MigrationError(f"invalid Mem0 destination configuration: {exc}") from exc
    vector_cfg = resolved["vector_store"]["config"]
    if vector_cfg.get("client") is not None:
        raise MigrationError(
            "programmatic Mem0 clients cannot be bound to a migration destination"
        )

    collection = vector_cfg.get("collection_name", "syncmill")
    url = vector_cfg.get("url")
    host = vector_cfg.get("host")
    port = vector_cfg.get("port")
    https = vector_cfg.get("https")
    if url and host:
        raise MigrationError("Mem0 destination must use either url or host, not both")
    if url:
        parsed = urlsplit(str(url))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise MigrationError(
                "Mem0 destination url must be an uncredentialed http(s) endpoint"
            )
        prefix = parsed.path.rstrip("/")
        vector = {
            "mode": "remote",
            "scheme": parsed.scheme,
            "host": parsed.hostname.lower(),
            "port": parsed.port or 6333,
            "prefix": prefix,
            "collection": collection,
        }
    elif host is not None:
        if port is None:
            raise MigrationError("Mem0 destination host requires an explicit port")
        secure = https is True or (
            https is None and vector_cfg.get("api_key") is not None
        )
        vector = {
            "mode": "remote",
            "scheme": "https" if secure else "http",
            "host": str(host).lower(),
            "port": int(port),
            "prefix": "",
            "collection": collection,
        }
    else:
        if vector_cfg.get("api_key") is not None or https is not None:
            raise MigrationError(
                "Mem0 destination api_key/https requires an explicit url or host"
            )
        path = vector_cfg.get("path")
        if not isinstance(path, str):
            raise MigrationError("Mem0 local destination requires a persistent path")
        vector = {
            "mode": "local",
            "path": _storage_path(path),
            "collection": collection,
        }
    return {
        "vector": vector,
        # Mem0 delete writes a DELETE event here after removing the Qdrant row.
        # It is therefore part of the rollback destination, not auxiliary cache.
        "history_db_path": _storage_path(resolved["history_db_path"]),
    }


def _mem0_locator(config: MemoryConfig) -> dict[str, Any]:
    try:
        return _mem0_locator_unchecked(config)
    except MigrationError:
        raise
    except (TypeError, ValueError) as exc:
        raise MigrationError("invalid Mem0 destination configuration") from exc


def _target_storage_digest(config: SyncmillConfig, backend: BackendName) -> str:
    memory = _config_for(config, backend)
    if backend == "memtomem":
        locator = {"db_path": _storage_path(memory.db_path)}
    elif backend == "langgraph":
        locator = {"db_path": _storage_path(memory.langgraph.db_path)}
    else:
        locator = _mem0_locator(memory)
    payload = {
        "binding_version": _STORAGE_BINDING_VERSION,
        "backend": backend,
        "locator": locator,
    }
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _validate_target_storage(
    config: SyncmillConfig, manifest: dict[str, Any], state: dict[str, Any]
) -> None:
    if manifest.get("schema_version") == 1:
        if state.get("targets"):
            raise MigrationError(
                "legacy migration artifact has no destination binding; "
                "automatic recovery is disabled; use state.json targets with "
                "the original destination settings for manual recovery"
            )
        raise MigrationError(
            "legacy migration artifact has no destination binding; create a new plan"
        )
    if manifest.get("storage_binding_version") != _STORAGE_BINDING_VERSION:
        raise MigrationError("unsupported migration storage binding version")
    expected = manifest.get("target_storage_digest")
    actual = _target_storage_digest(config, manifest["target"])
    if not isinstance(expected, str) or actual != expected:
        raise MigrationError(
            "migration destination storage configuration does not match the plan"
        )


def migration_root() -> Path:
    return Path(os.environ.get("SYNCMILL_MIGRATIONS_DIR", "~/.syncmill/migrations")).expanduser()


def _tighten_migration_root(*, create: bool) -> Path:
    """Return the migrations root, private (0700).

    Every artifact under it holds verbatim memory content, and ``mkdir``'s ``mode``
    applies to the LEAF only — so creating an artifact with ``parents=True`` left the
    root itself at the default umask. Called on the READ paths too (via ``_artifact``,
    best-effort): an existing installation that only ever applies or verifies would
    otherwise keep the old permissive root forever.
    """
    root = migration_root()
    if create:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        return root
    try:
        os.chmod(root, 0o700)
    except FileNotFoundError:
        # Nothing to tighten; the caller reports the missing migration itself.
        pass
    except OSError as exc:
        # A root we cannot tighten (owned by someone else, or a filesystem that
        # refuses chmod) stays readable while holding verbatim memory content —
        # refuse rather than proceed under a privacy guarantee we are not keeping.
        raise MigrationError(
            f"cannot make the migrations directory private ({root}): {exc}"
        ) from exc
    return root


@contextmanager
def _migration_lock(path: Path) -> Iterator[None]:
    """Serialize a whole load → operate → write sequence for one migration.

    Unique temp names stop a torn ``state.json``; they do NOT stop a stale one.
    ``apply``/``verify``/``rollback`` each read the full state document and rewrite
    it, so two overlapping commands make the slower one publish a ``targets`` map
    that predates the other's work — orphaning imported rows from any later retry or
    rollback, which is exactly the data the rollback path needs.

    NOT re-entrant, deliberately. ``apply`` needs a verify at the end, but it calls
    ``_verify_locked`` directly inside the lock it already holds rather than
    re-entering here — a re-entrancy shortcut keyed on the migration id would let a
    SECOND task or thread using that same id walk straight past the lock while the
    first awaits backend I/O, which is precisely the concurrency the lock exists to
    stop. Uses the artifact store's cross-platform helpers so the guarantee is not
    quietly absent on a platform without ``fcntl``.

    Acquisition FAILS FAST rather than waiting. Orchestration bridges async through
    one shared module-level event loop, so a blocking acquire taken on that loop's
    thread wedges it: the waiter cannot yield, and the holder needs the same loop to
    finish and release. Failing fast also suits the CLI, where the alternative is a
    second invocation hanging with no output. Every failure becomes a
    ``MigrationError`` — the only exception type the migration commands' callers
    catch, so neither ``ApplyLockBusy`` nor a raw ``OSError`` escapes.
    """
    try:
        fd = os.open(str(path / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise MigrationError(f"cannot open the migration lock: {exc}") from exc
    try:
        try:
            _lock_file_descriptor(fd, blocking=False)
        except ApplyLockBusy as exc:
            raise MigrationError(
                "another migration command is already running for this migration — "
                "wait for it to finish, then retry"
            ) from exc
        except (OSError, ArtifactError) as exc:
            raise MigrationError(f"cannot lock the migration: {exc}") from exc
        try:
            yield
        finally:
            try:
                _unlock_file_descriptor(fd)
            except OSError:  # closing the descriptor below releases it regardless
                pass
    finally:
        os.close(fd)


def _atomic_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    # A UNIQUE temp in the same dir (the report mkstemp recipe), not a fixed `.tmp`
    # sibling: two writers to one `state.json` — apply and verify can overlap — both
    # opened the same temp path and clobbered each other's content, with the loser's
    # `os.replace` publishing a half-written mix.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _artifact(migration_id: str) -> Path:
    if not migration_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in migration_id):
        raise MigrationError("invalid migration id")
    path = _tighten_migration_root(create=False) / migration_id
    if not path.is_dir():
        raise MigrationError(f"migration not found: {migration_id}")
    return path


def _materialized_source(migration_id: str, record: MigrationRecord) -> Path:
    """Write a migrated record's content beside its migration artifacts and
    return that absolute path, for use as the imported chunk's ``source_file``.

    A synthetic path is not free: memtomem's orphan detection collects every
    indexed ``source_file`` that no longer exists on disk and deletes its
    chunks — run by the scheduled compaction job, the health watchdog, and
    ``mem_cleanup_orphans``. A path that never existed is orphaned by
    definition, so the migrated chunks would be swept the first time any of
    those runs (and, in bulk, could instead trip the mass-delete brake and
    suppress the user's real orphan cleanup).

    The file lives under the retained migration artifact directory rather than
    the memory dirs: it is real enough for the existence check, outside every
    index root so no watcher re-chunks it under a namespace of its own, and it
    is genuinely the provenance record for what was imported.
    """
    sources = migration_root() / migration_id / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    # These files hold verbatim memory content; mkdir's mode is umask-masked, so
    # tighten explicitly (the snapshot files beside them are written 0600 too).
    os.chmod(sources, 0o700)
    # The lineage key is caller-derived, so hash it: it may contain separators
    # or exceed the filesystem's name limit.
    name = hashlib.sha256(record.lineage_key.encode("utf-8")).hexdigest()[:32]
    path = sources / f"{name}.md"
    if not path.exists():
        path.write_text(record.content, encoding="utf-8")
        os.chmod(path, 0o600)
    return path.resolve()


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
                source_file=_materialized_source(migration_id, record),
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
    target_storage_digest = _target_storage_digest(config, target)
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
    path = _tighten_migration_root(create=True) / migration_id
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    snapshot = {"schema_version": _SCHEMA_VERSION, "records": [asdict(r) for r in records]}
    manifest = {
        "schema_version": _SCHEMA_VERSION,
        "migration_id": migration_id,
        "source": source,
        "target": target,
        "storage_binding_version": _STORAGE_BINDING_VERSION,
        "target_storage_digest": target_storage_digest,
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
    manifest_version = manifest.get("schema_version")
    snapshot_version = snapshot.get("schema_version")
    if manifest_version not in {1, _SCHEMA_VERSION}:
        raise MigrationError("unsupported migration artifact schema version")
    if snapshot_version != manifest_version:
        raise MigrationError("migration artifact schema versions do not match")
    records = [MigrationRecord(**row) for row in snapshot["records"]]
    if _aggregate(records) != manifest["source_digest"]:
        raise MigrationError("migration snapshot digest mismatch")
    return manifest, records, state


async def apply_migration(config: SyncmillConfig, migration_id: str) -> dict[str, Any]:
    path = _artifact(migration_id)
    with _migration_lock(path):
        return await _apply_locked(config, migration_id, path)


async def _apply_locked(
    config: SyncmillConfig, migration_id: str, path: Path
) -> dict[str, Any]:
    manifest, records, state = _load(path)
    _validate_target_storage(config, manifest, state)
    # AsyncExitStack, not two bare names: `dst`'s construction can raise after
    # `src` exists (leaking an embedded store's handle and its lock, so the retry
    # this failure invites cannot open it), and a failing `src.close()` in a plain
    # `finally` skipped `dst.close()` entirely. Each adapter is registered the
    # moment it exists, so every close runs.
    async with AsyncExitStack() as stack:
        src = _maintenance(config, manifest["source"])
        stack.push_async_callback(src.close)
        dst = _maintenance(config, manifest["target"])
        stack.push_async_callback(dst.close)
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
    # Direct, NOT verify_migration: we already hold the lock (see _migration_lock).
    return await _verify_locked(config, migration_id, path)


async def verify_migration(config: SyncmillConfig, migration_id: str) -> dict[str, Any]:
    path = _artifact(migration_id)
    with _migration_lock(path):
        return await _verify_locked(config, migration_id, path)


async def _verify_locked(
    config: SyncmillConfig, migration_id: str, path: Path
) -> dict[str, Any]:
    manifest, records, state = _load(path)
    _validate_target_storage(config, manifest, state)
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
    with _migration_lock(path):
        return await _rollback_locked(config, migration_id, path)


async def _rollback_locked(
    config: SyncmillConfig, migration_id: str, path: Path
) -> dict[str, Any]:
    manifest, records, state = _load(path)
    _validate_target_storage(config, manifest, state)
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
