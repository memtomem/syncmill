"""Backend seam for syncmill's long-term memory.

``MemoryBackend`` names the contract ``SyncmillMemory`` imposes on its
storage provider. Backends raise freely: ``SyncmillMemory`` owns graceful
degradation, and adapters must not add retries or fallback behavior.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypedDict
from uuid import uuid4

from syncmill.config import MemoryConfig


class MemoryRow(TypedDict, total=False):
    """One search/get result row; ``namespace`` is required for get isolation."""

    id: str
    content: str
    score: float
    source: str
    namespace: str
    tags: list[str]


@dataclass(frozen=True)
class BackendCapabilities:
    """Capabilities used by later backend/namespace-lifecycle phases."""

    supports_sessions: bool = True
    supports_tag_filter: bool = True
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


_LOCAL_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_LOCAL_DIMS = 384


class _DisabledLLM:
    """Fail closed if a future mem0 path accidentally tries inference."""

    def generate_response(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("syncmill's mem0 backend disables LLM inference")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


class Mem0Backend:
    """Optional mem0 OSS reference backend, constrained to Qdrant.

    The adapter maps each syncmill namespace to one mem0 ``user_id`` and forces
    ``infer=False`` on writes. Qdrant is deliberately required because P2 needs
    exact namespace counts and proof that bulk deletion reached zero.
    """

    def __init__(self, config: MemoryConfig):
        mem0_cfg = config.mem0
        data_dir = Path(mem0_cfg.data_dir).expanduser()

        defaults: dict[str, Any] = {
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "collection_name": "syncmill",
                    "path": str(data_dir / "qdrant"),
                    "embedding_model_dims": _LOCAL_DIMS,
                },
            },
            "embedder": {
                "provider": "fastembed",
                "config": {
                    "model": _LOCAL_MODEL,
                    "embedding_dims": _LOCAL_DIMS,
                },
            },
            # AsyncMemory constructs an LLM even though infer=False never uses it.
            # Give that constructor no real credential or reachable endpoint, then
            # replace the object with _DisabledLLM below as defense in depth.
            "llm": {
                "provider": "openai",
                "config": {
                    "model": "gpt-5-mini",
                    "api_key": "syncmill-inference-disabled",
                    "openai_base_url": "http://127.0.0.1:9/v1",
                },
            },
            "history_db_path": str(data_dir / "history.db"),
            "version": "v1.1",
        }
        raw = copy.deepcopy(mem0_cfg.config)
        resolved = _deep_merge(defaults, raw)
        vector = resolved.get("vector_store", {})
        if vector.get("provider") != "qdrant":
            raise ValueError("mem0 backend requires vector_store.provider='qdrant'")

        embedder = resolved.get("embedder", {})
        embed_cfg = embedder.get("config", {})
        user_embedder = raw.get("embedder", {})
        user_embed_cfg = user_embedder.get("config", {})
        customized_embedder = (
            embedder.get("provider") != "fastembed"
            or embed_cfg.get("model") != _LOCAL_MODEL
        )
        if customized_embedder and "embedding_dims" not in user_embed_cfg:
            raise ValueError(
                "custom mem0 embedders must set config.embedding_dims explicitly"
            )
        if (
            embedder.get("provider") != "fastembed"
            and "model" not in user_embed_cfg
        ):
            raise ValueError("custom mem0 embedders must set config.model explicitly")
        dims = embed_cfg.get("embedding_dims")
        if not isinstance(dims, int) or isinstance(dims, bool) or dims < 1:
            raise ValueError("mem0 embedder.config.embedding_dims must be positive")
        vector_cfg = vector.setdefault("config", {})
        vector_dims = vector_cfg.get("embedding_model_dims")
        user_vector_cfg = raw.get("vector_store", {}).get("config", {})
        if (
            "embedding_model_dims" in user_vector_cfg
            and vector_dims is not None
            and vector_dims != dims
        ):
            raise ValueError(
                "mem0 vector_store embedding_model_dims must match embedder dimensions"
            )
        vector_cfg["embedding_model_dims"] = dims

        # A remote/custom Qdrant client and a local path are mutually exclusive.
        if any(k in user_vector_cfg for k in ("host", "url", "client")):
            vector_cfg.pop("path", None)
        elif "path" in vector_cfg:
            vector_cfg["path"] = str(Path(vector_cfg["path"]).expanduser())
        resolved["history_db_path"] = str(
            Path(resolved["history_db_path"]).expanduser()
        )
        data_dir.mkdir(parents=True, exist_ok=True)

        # mem0 snapshots this setting on the process's first mem0 import. Syncmill
        # defaults that first import to no anonymous telemetry; changing the
        # setting after another component imports mem0 requires a process restart.
        os.environ["MEM0_TELEMETRY"] = "true" if mem0_cfg.telemetry else "false"
        try:
            from mem0 import AsyncMemory
            from mem0.configs.base import MemoryConfig as Mem0MemoryConfig
            from qdrant_client.models import FieldCondition, Filter, MatchValue
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "mem0 backend is not installed; install syncmill[mem0] "
                "or run `uv sync --extra mem0`"
            ) from exc

        self._store = AsyncMemory(config=Mem0MemoryConfig(**resolved))
        self._store.llm = _DisabledLLM()
        self._filter_type = Filter
        self._field_condition_type = FieldCondition
        self._match_value_type = MatchValue
        self._data_dir = str(data_dir)
        self._resolved = resolved
        self._caps = BackendCapabilities(
            supports_sessions=False,
            supports_tag_filter=True,
            namespace_delete_is_atomic=False,
        )
        self._closed = False

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._caps

    @property
    def agent_namespace_prefix(self) -> str:
        return "agent-runtime:"

    @property
    def shared_namespace(self) -> str:
        return "shared"

    def validate_namespace(self, ns: str) -> bool:
        return bool(self._caps.namespace_pattern.fullmatch(ns))

    async def add(self, content: str, *, tags: list[str], namespace: str) -> None:
        response = await self._store.add(
            [{"role": "user", "content": content}],
            user_id=namespace,
            metadata={"syncmill_tags": list(tags)},
            infer=False,
        )
        rows = response.get("results", []) if isinstance(response, dict) else []
        if len(rows) != 1 or rows[0].get("memory") != content:
            raise RuntimeError("mem0 did not confirm one verbatim memory write")

    def _filters(self, namespace: str, tag_filter: str | None) -> dict[str, Any]:
        filters: dict[str, Any] = {"user_id": namespace}
        if tag_filter is not None:
            tags = [tag.strip() for tag in tag_filter.split(",") if tag.strip()]
            if tags:
                filters["OR"] = [{"syncmill_tags": tag} for tag in tags]
        return filters

    @staticmethod
    def _row(row: dict[str, Any]) -> MemoryRow | None:
        namespace = row.get("user_id")
        if not namespace:
            return None
        metadata = row.get("metadata") or {}
        return MemoryRow(
            id=str(row.get("id", "")),
            content=str(row.get("memory", "")),
            score=float(row.get("score") or 0.0),
            source=str(metadata.get("source") or "mem0"),
            namespace=str(namespace),
        )

    async def search(
        self,
        query: str,
        *,
        namespace: str,
        top_k: int,
        tag_filter: str | None = None,
    ) -> list[MemoryRow]:
        response = await self._store.search(
            query,
            top_k=top_k,
            filters=self._filters(namespace, tag_filter),
            threshold=0.0,
            rerank=False,
        )
        raw_rows = response.get("results", []) if isinstance(response, dict) else []
        rows = [mapped for raw in raw_rows if (mapped := self._row(raw))]
        return [row for row in rows if row.get("namespace") == namespace]

    async def get(self, chunk_id: str) -> MemoryRow | None:
        raw = await self._store.get(chunk_id)
        return self._row(raw) if isinstance(raw, dict) else None

    async def ensure_namespace(self, ns: str, *, description: str = "") -> None:
        return None

    async def ensure_ready(self) -> None:
        await self.count_namespace("syncmill-readiness-probe")

    async def status(self) -> dict[str, Any]:
        await self.ensure_ready()
        vector = self._resolved["vector_store"]
        embedder = self._resolved["embedder"]
        return {
            "data_dir": self._data_dir,
            "vector_store": "qdrant",
            "collection": vector["config"].get("collection_name", "mem0"),
            "embedder": f"{embedder.get('provider')}/{embedder['config'].get('model')}",
        }

    def _qdrant_store(self) -> Any:
        store = self._store.vector_store
        if not hasattr(store, "client") or not hasattr(store, "collection_name"):
            raise RuntimeError("mem0 Qdrant internals are unavailable")
        return store

    async def count_namespace(self, ns: str) -> int:
        store = self._qdrant_store()
        count_filter = self._filter_type(
            must=[
                self._field_condition_type(
                    key="user_id", match=self._match_value_type(value=ns)
                )
            ]
        )
        response = await asyncio.to_thread(
            store.client.count,
            collection_name=store.collection_name,
            count_filter=count_filter,
            exact=True,
        )
        return int(response.count)

    async def delete_namespace(self, ns: str) -> int:
        original = await self.count_namespace(ns)
        remaining = original
        while remaining:
            await self._store.delete_all(user_id=ns)
            after = await self.count_namespace(ns)
            if after >= remaining:
                raise RuntimeError(
                    f"mem0 namespace deletion made no progress for {ns!r}"
                )
            remaining = after
        return original

    async def start_session(self, agent_id: str, namespace: str) -> str | None:
        return None

    async def log_event(self, kind: str, text: str) -> None:
        return None

    async def end_session(self, summary: str | None) -> None:
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Teardown must not depend on the private Qdrant shape used by count/delete.
        # mem0 2.0.x closes its history DB, while the client close releases the
        # embedded Qdrant path lock when that client is still discoverable.
        vector_store = getattr(self._store, "vector_store", None)
        client = getattr(vector_store, "client", None)
        try:
            self._store.close()
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                await asyncio.to_thread(close)


_LANGGRAPH_NS = ("syncmill", "memory", "items")


def _tag_key(tag: str) -> str:
    return "tag_" + hashlib.sha256(tag.encode("utf-8")).hexdigest()


class LangGraphBackend:
    """Optional persistent LangGraph AsyncSqliteStore reference backend."""

    def __init__(self, config: MemoryConfig):
        self._db_path = str(Path(config.langgraph.db_path).expanduser())
        self._store: Any = None
        self._store_cm: Any = None
        self._init_lock = asyncio.Lock()
        self._closed = False
        self._caps = BackendCapabilities(
            supports_sessions=False,
            supports_tag_filter=True,
            namespace_delete_is_atomic=False,
        )

    @property
    def capabilities(self) -> BackendCapabilities:
        return self._caps

    @property
    def agent_namespace_prefix(self) -> str:
        return "agent-runtime:"

    @property
    def shared_namespace(self) -> str:
        return "shared"

    def validate_namespace(self, ns: str) -> bool:
        return bool(self._caps.namespace_pattern.fullmatch(ns))

    async def _ensure_store(self) -> Any:
        if self._closed:
            raise RuntimeError("LangGraph backend is closed")
        if self._store is not None:
            return self._store
        async with self._init_lock:
            if self._store is not None:
                return self._store
            try:
                from fastembed import TextEmbedding
                from langchain_core.embeddings import Embeddings
                from langgraph.store.sqlite.aio import AsyncSqliteStore
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "LangGraph backend is not installed; install syncmill[langgraph] "
                    "or run `uv sync --extra langgraph`"
                ) from exc

            class LocalEmbeddings(Embeddings):
                def __init__(self) -> None:
                    self.model = TextEmbedding(model_name=_LOCAL_MODEL)

                def embed_documents(self, texts: list[str]) -> list[list[float]]:
                    return [list(vector) for vector in self.model.embed(texts)]

                def embed_query(self, text: str) -> list[float]:
                    return self.embed_documents([text])[0]

                async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
                    return await asyncio.to_thread(self.embed_documents, texts)

                async def aembed_query(self, text: str) -> list[float]:
                    return (await self.aembed_documents([text]))[0]

            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._store_cm = AsyncSqliteStore.from_conn_string(
                self._db_path,
                index={
                    "dims": _LOCAL_DIMS,
                    "embed": LocalEmbeddings(),
                    "text_fields": ["content"],
                },
            )
            self._store = await self._store_cm.__aenter__()
            try:
                await self._store.setup()
            except Exception:
                await self._store_cm.__aexit__(None, None, None)
                self._store = self._store_cm = None
                raise
            return self._store

    @staticmethod
    def _value(content: str, tags: list[str], namespace: str) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": 1,
            "namespace": namespace,
            "content": content,
            "tags": list(tags),
            "source": "langgraph",
        }
        value.update({_tag_key(tag): True for tag in tags})
        return value

    @staticmethod
    def _row(item: Any) -> MemoryRow | None:
        value = getattr(item, "value", None)
        if not isinstance(value, dict):
            return None
        namespace = value.get("namespace")
        content = value.get("content")
        tags = value.get("tags", [])
        if not isinstance(namespace, str) or not isinstance(content, str):
            return None
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return None
        return MemoryRow(
            id=str(item.key),
            content=content,
            score=float(getattr(item, "score", None) or 0.0),
            source=str(value.get("source") or "langgraph"),
            namespace=namespace,
            tags=list(tags),
        )

    async def add(self, content: str, *, tags: list[str], namespace: str) -> None:
        store = await self._ensure_store()
        await store.aput(
            _LANGGRAPH_NS,
            str(uuid4()),
            self._value(content, tags, namespace),
            index=["content"],
        )

    async def search(
        self, query: str, *, namespace: str, top_k: int,
        tag_filter: str | None = None,
    ) -> list[MemoryRow]:
        store = await self._ensure_store()
        tags = (
            [tag.strip() for tag in tag_filter.split(",") if tag.strip()]
            if tag_filter else []
        )
        filters = (
            [{"namespace": namespace, _tag_key(tag): True} for tag in tags]
            or [{"namespace": namespace}]
        )
        merged: dict[str, MemoryRow] = {}
        for exact_filter in filters:
            items = await store.asearch(
                _LANGGRAPH_NS,
                query=query,
                filter=exact_filter,
                limit=top_k,
            )
            for item in items:
                row = self._row(item)
                if row is None or row.get("namespace") != namespace:
                    continue
                prior = merged.get(row["id"])
                if prior is None or row.get("score", 0.0) > prior.get("score", 0.0):
                    merged[row["id"]] = row
        return sorted(merged.values(), key=lambda row: -row.get("score", 0.0))[:top_k]

    async def get(self, chunk_id: str) -> MemoryRow | None:
        return self._row(await (await self._ensure_store()).aget(_LANGGRAPH_NS, chunk_id))

    async def ensure_namespace(self, ns: str, *, description: str = "") -> None:
        return None

    async def ensure_ready(self) -> None:
        await self._ensure_store()

    async def status(self) -> dict[str, Any]:
        await self.ensure_ready()
        return {
            "db_path": self._db_path,
            "store": "langgraph/AsyncSqliteStore",
            "embedder": f"fastembed/{_LOCAL_MODEL}",
        }

    async def _page(self, ns: str, *, limit: int = 100, offset: int = 0) -> list[Any]:
        return await (await self._ensure_store()).asearch(
            _LANGGRAPH_NS,
            query=None,
            filter={"namespace": ns},
            limit=limit,
            offset=offset,
        )

    async def count_namespace(self, ns: str) -> int:
        total = 0
        while True:
            page = await self._page(ns, offset=total)
            total += len(page)
            if len(page) < 100:
                return total

    async def delete_namespace(self, ns: str) -> int:
        original = await self.count_namespace(ns)
        store = await self._ensure_store()
        while True:
            page = await self._page(ns)
            if not page:
                break
            for item in page:
                await store.adelete(_LANGGRAPH_NS, item.key)
        if await self.count_namespace(ns):
            raise RuntimeError(f"LangGraph namespace deletion did not reach zero for {ns!r}")
        return original

    async def start_session(self, agent_id: str, namespace: str) -> str | None:
        return None

    async def log_event(self, kind: str, text: str) -> None:
        return None

    async def end_session(self, summary: str | None) -> None:
        return None

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._store_cm is not None:
            await self._store_cm.__aexit__(None, None, None)
        self._store = self._store_cm = None


def build_backend(config: MemoryConfig) -> MemoryBackend:
    """Build the configured backend, refusing non-verbatim storage."""

    if config.backend == "memtomem":
        backend: MemoryBackend = MemtomemBackend(config)
    elif config.backend == "mem0":
        backend = Mem0Backend(config)
    elif config.backend == "langgraph":
        backend = LangGraphBackend(config)
    else:  # guarded by MemoryConfig's Literal, defensive for programmatic callers
        raise ValueError(f"unknown memory backend: {config.backend!r}")
    if not backend.capabilities.verbatim_storage:
        raise ValueError(
            f"backend {config.backend!r} cannot store verbatim content"
        )
    return backend
