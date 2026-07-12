"""Backend seam for syncmill's long-term memory.

``MemoryBackend`` names the contract ``SyncmillMemory`` imposes on its
storage provider. Backends raise freely: ``SyncmillMemory`` owns graceful
degradation, and adapters must not add retries or fallback behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from syncmill.config import MemoryConfig


class MemoryRow(TypedDict, total=False):
    """One search/get result row; ``namespace`` is required for get isolation."""

    id: str
    content: str
    score: float
    source: str
    namespace: str


class NamespaceMeta(TypedDict, total=False):
    namespace: str
    chunk_count: int


@dataclass(frozen=True)
class BackendCapabilities:
    """Capabilities used by later backend/namespace-lifecycle phases."""

    supports_sessions: bool = True
    supports_tag_filter: bool = True
    supports_namespace_enumeration: bool = True
    namespace_delete_is_atomic: bool = True
    verbatim_storage: bool = True
    namespace_pattern: re.Pattern[str] = field(
        default_factory=lambda: re.compile(
            r"^[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)*$"
        )
    )


class MemoryBackend(Protocol):
    """Async, raise-freely storage contract for ``SyncmillMemory``."""

    @property
    def capabilities(self) -> BackendCapabilities: ...

    @property
    def agent_namespace_prefix(self) -> str: ...

    @property
    def shared_namespace(self) -> str: ...

    def validate_namespace(self, ns: str) -> bool: ...

    async def add(
        self, content: str, *, tags: list[str], namespace: str
    ) -> None: ...

    async def search(
        self,
        query: str,
        *,
        namespace: str,
        top_k: int,
        tag_filter: str | None = None,
    ) -> list[MemoryRow]: ...

    async def get(self, chunk_id: str) -> MemoryRow | None: ...

    async def ensure_namespace(
        self, ns: str, *, description: str = ""
    ) -> None: ...

    async def ensure_ready(self) -> None: ...

    async def status(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...

    async def count_namespace(self, ns: str) -> int: ...

    async def list_namespaces(self) -> list[NamespaceMeta]: ...

    async def delete_namespace(self, ns: str) -> int: ...

    async def start_session(
        self, agent_id: str, namespace: str
    ) -> str | None: ...

    async def log_event(self, kind: str, text: str) -> None: ...

    async def end_session(self, summary: str | None) -> None: ...


class MemtomemBackend:
    """Default backend and the sole owner of memtomem-private coupling."""

    def __init__(self, config: MemoryConfig):
        # Keep imports lazy: import or construction failures are advisory and are
        # caught by SyncmillMemory's existing construction boundary.
        from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
        from memtomem.integrations.langgraph import MemtomemStore

        self._agent_prefix = AGENT_NAMESPACE_PREFIX
        self._shared_ns = SHARED_NAMESPACE
        self._store = MemtomemStore(
            config_overrides={"storage": {"sqlite_path": config.db_path}}
        )
        self._caps = BackendCapabilities()

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._caps

    @property
    def agent_namespace_prefix(self) -> str:
        return self._agent_prefix

    @property
    def shared_namespace(self) -> str:
        return self._shared_ns

    def validate_namespace(self, ns: str) -> bool:
        return bool(self._caps.namespace_pattern.fullmatch(ns))

    async def add(self, content: str, *, tags: list[str], namespace: str) -> None:
        await self._store.add(content=content, tags=tags, namespace=namespace)

    async def search(
        self,
        query: str,
        *,
        namespace: str,
        top_k: int,
        tag_filter: str | None = None,
    ) -> list[MemoryRow]:
        if tag_filter is not None:
            return await self._store.search(
                query,
                namespace=namespace,
                top_k=top_k,
                tag_filter=tag_filter,
            )
        return await self._store.search(query, namespace=namespace, top_k=top_k)

    async def get(self, chunk_id: str) -> MemoryRow | None:
        return await self._store.get(chunk_id)

    async def ensure_namespace(self, ns: str, *, description: str = "") -> None:
        storage = await self._storage()
        if await storage.get_namespace_meta(ns) is None:
            await storage.set_namespace_meta(ns, description=description)

    async def ensure_ready(self) -> None:
        await self._store._ensure_init()

    async def status(self) -> dict[str, Any]:
        await self.ensure_ready()
        db_path = self._store._config_overrides.get("storage", {}).get(
            "sqlite_path", "default"
        )
        return {"db_path": db_path}

    async def close(self) -> None:
        await self._store.close()

    async def _storage(self) -> Any:
        await self._store._ensure_init()
        return self._store._components.storage

    async def count_namespace(self, ns: str) -> int:
        metas = await (await self._storage()).list_namespace_meta()
        return next(
            (m.get("chunk_count", 0) for m in metas if m.get("namespace") == ns),
            0,
        )

    async def list_namespaces(self) -> list[NamespaceMeta]:
        return await (await self._storage()).list_namespace_meta()

    async def delete_namespace(self, ns: str) -> int:
        return await (await self._storage()).delete_by_namespace(ns)

    async def start_session(self, agent_id: str, namespace: str) -> str | None:
        return await self._store.start_session(
            agent_id=agent_id, namespace=namespace
        )

    async def log_event(self, kind: str, text: str) -> None:
        await self._store.log_event(kind, text)

    async def end_session(self, summary: str | None) -> None:
        await self._store.end_session(summary=summary)


def build_backend(config: MemoryConfig) -> MemoryBackend:
    """Build the configured backend, refusing non-verbatim storage."""

    backend = MemtomemBackend(config)
    if not backend.capabilities.verbatim_storage:
        raise ValueError(
            f"backend {config.backend!r} cannot store verbatim content"
        )
    return backend
