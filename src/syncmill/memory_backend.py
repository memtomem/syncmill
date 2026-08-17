"""Backend seam for syncmill's long-term memory.

``MemoryBackend`` names the contract ``SyncmillMemory`` imposes on its
storage provider. Backends raise freely: ``SyncmillMemory`` owns graceful
degradation, and adapters must not add retries or fallback behavior.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypedDict
from uuid import UUID, uuid4

from syncmill.config import MemoryConfig

logger = logging.getLogger(__name__)


class WriteBlockedError(RuntimeError):
    """The backend's redaction guard blocked this content.

    Distinct from ordinary backend failures so ``SyncmillMemory`` can skip the
    direct-add fallback — the same guard runs on the same content there and can
    only re-block (duplicate audit noise, never a different outcome)."""


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
    # Whether search_multi is one native merged-ranking call (memtomem's
    # comma-namespace union filter) vs an adapter-side per-namespace loop.
    native_multi_namespace_search: bool = True
    # Whether share_chunk records structural provenance (a chunk_links row)
    # beside the shared-from tag, or the tag alone.
    share_provenance_links: bool = True
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

    async def search_multi(
        self,
        query: str,
        *,
        namespaces: Sequence[str],
        top_k: int,
        tag_filter: str | None = None,
    ) -> list[MemoryRow]: ...

    async def add_returning_ids(
        self, content: str, *, tags: list[str], namespace: str
    ) -> list[str]:
        """Like ``add`` but returns every freshly indexed chunk id (a long
        content can split into several chunks). Empty when the store minted no
        fresh id (e.g. a content-hash dedup). May raise ``WriteBlockedError``."""
        ...

    async def share_chunk(
        self, chunk_id: str, *, target_namespace: str
    ) -> str | None: ...

    async def register_namespace(
        self, ns: str, *, description: str | None = None, color: str | None = None
    ) -> None:
        """Unconditional description/color metadata upsert (mem_agent_register
        semantics; ``None`` leaves a field untouched). Metadata is a
        memtomem-native concept — mem0/LangGraph adapters no-op, exactly as
        their ``ensure_namespace`` already does."""
        ...

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


# The mem_agent_share provenance tag. On a re-share the chain is collapsed to
# the immediate parent only (prior markers dropped) — mirroring memtomem's
# _build_shared_tags so syncmill copies read identically to MCP-tool shares.
_SHARED_FROM_PREFIX = "shared-from="


def _utc_date_str() -> str:
    """Daily memory-file name, matching MemtomemStore.add's target choice."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _namespace_lock_path(base: Path, ns: str) -> Path:
    """syncmill's own per-namespace lock, held ABOVE memtomem's per-file one.

    A purge has to exclude writers to a NAMESPACE, and memtomem's locks are
    per-FILE (its L2 sidecar), so the natural move — lock the file a writer is
    about to touch — cannot work: the writer derives its target BEFORE taking
    any lock, so a process suspended between those two steps (a stopped
    process, a descheduled thread, a slow guard) resumes holding a target for
    an arbitrarily old date. Enumerating candidate dates cannot fix that, since
    there is no bound on how long a writer may hold a stale target. This is the
    same shape as the bundle-path defect in the workflow store: a paused
    process defeats any check-then-act, so the exclusion has to be on the thing
    both sides agree about — here the namespace — and it has to be taken before
    the racing side computes anything.

    So this lock is keyed by (memory base, namespace) and nothing else, and the
    write path takes it before it reads the clock. Being date-free is the whole
    point: no argument about rollovers is required, and none is made.

    **Lock ordering.** This sits ABOVE memtomem's documented memory-file domain
    (``context/_atomic.py``, L0-L4): a holder may go on to take an L2 sidecar,
    never the reverse. Only syncmill takes it, so that order is enforceable by
    inspection of this module alone.

    It lives beside the day files rather than in syncmill's state root because
    the writer must already be able to write there, so this adds no new failure
    mode to an advisory write path — an unwritable state root would otherwise
    turn every memory write into an error. The leading dot matches memtomem's
    own sidecar convention, and the name can never satisfy ``_is_day_file``, so
    the sweep does not treat it as a source file.
    """
    digest = hashlib.sha256(
        f"{os.path.realpath(base)}\0{ns}".encode()
    ).hexdigest()[:32]
    return base / f".syncmill-ns-{digest}.lock"


# ASCII digits only: `\d` also matches other Unicode decimal digits, so a
# full-width `２０２６-０８-０１` would pass a date the writer cannot produce.
_DAY_FILE_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _is_day_file(name: str, suffix: str) -> bool:
    """Whether ``name`` is a day file for ``suffix`` — a DATE plus that suffix.

    Suffix-matching alone is not enough at a destructive boundary: it accepts
    any prefix, so an unrelated ``backup--{slug}-{digest}.md`` or
    ``notes.{ns}.md`` would be unlinked. The namespace digest proves which
    namespace a day file belongs to, never that the file IS a day file.

    The date must be one ``strftime("%Y-%m-%d")`` could have produced, shape
    AND calendar: every name this accepts is one syncmill or memtomem wrote,
    so anything a writer cannot emit — ``2026-99-99``, non-ASCII digits — is
    somebody else's file that happens to end the same way.
    """
    if not name.endswith(suffix):
        return False
    date_part = name[: -len(suffix)]
    if not _DAY_FILE_DATE.fullmatch(date_part):
        return False
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _index_roots(config: Any, base: Path) -> list[Path]:
    """Every directory the indexer treats as a root, user tier first.

    Sweeping only the user tier leaves a native day file under a registered
    project root on disk, where the next pass re-indexes it exactly like a
    user-tier one — so the namespace the caller was told had been purged comes
    back. ``all_index_roots`` is upstream's own single source of truth for
    consumers that must act on every indexable file.

    Every root is verified to be a readable directory and anything else RAISES,
    including a missing ``all_index_roots`` (the pin floor 0.4.0 has it, so its
    absence is version skew, not an old-config shape to guess around). This
    boundary fails CLOSED for the same reason the caller does: skipping a root
    that is merely unreachable right now — an unmounted project tier — would
    report a clean zero-file purge, let the caller tombstone the namespace, and
    resurrect it when the root came back. An unverifiable root means the
    namespace stays collectable and a later sweep retries it.

    ``base`` (the caller's already-resolved ``require_user_base``) leads so the
    two never fork on how the user tier is resolved, but it is checked like any
    other root.
    """
    all_roots = getattr(config.indexing, "all_index_roots", None)
    if not callable(all_roots):
        raise RuntimeError(
            "memtomem config exposes no all_index_roots(); cannot prove every "
            "index root was swept"
        )
    roots: dict[Path, None] = dict.fromkeys(
        [base, *(Path(e).expanduser().resolve() for e in all_roots())]
    )
    for root in roots:
        if not root.is_dir() or not os.access(root, os.R_OK | os.X_OK):
            raise RuntimeError(
                f"index root is not a readable directory: {root}; cannot prove "
                "its sources were swept"
            )
    return list(roots)


def _namespace_day_file_suffixes(config: Any, ns: str) -> list[str]:
    """The day-file suffixes that identify ``ns``, both write shapes.

    ``{date}.{ns}.md`` is what this backend's replicated add flow writes;
    ``{date}--{slug}-{digest}.md`` is what memtomem 0.4 natively writes for the
    same namespace. The native suffix comes from upstream's own
    ``day_file_name``, so an upstream rename breaks a test rather than silently
    stranding files.
    """
    from memtomem.memory_scope import day_file_name

    suffixes = [f".{ns.replace(':', '.')}.md"]
    # day_file_name gives the DEFAULT namespace the bare "{date}.md" shape,
    # leaving just ".md" for an empty date_str — a suffix every file in the
    # store ends with. Only the digest-suffixed non-default shape is swept.
    native_suffix = day_file_name(ns, config.namespace.default_namespace, date_str="")
    if native_suffix != ".md":
        suffixes.append(native_suffix)
    return suffixes


def _namespace_source_paths(config: Any, ns: str) -> list[Path]:
    """Every day file belonging to ``ns``, across every index root.

    The single matcher. ``_sweep_namespace_sources`` unlinks what this returns
    and ``purge_namespace`` locks it first, so the two can never disagree about
    which files a namespace owns — the matching is the destructive boundary.
    """
    from memtomem.memory_scope import require_user_base

    # Raises on an empty memory_dirs, which the contract wants surfaced
    # rather than reported as a zero-file purge.
    base = require_user_base(config.indexing.memory_dirs)
    suffixes = _namespace_day_file_suffixes(config, ns)
    found: list[Path] = []
    for root in _index_roots(config, base):
        # scandir, not glob: pathlib SWALLOWS a scan-time OSError and yields
        # nothing, so an unreadable root would look like an empty one and
        # report a clean purge over files it never managed to look at. The
        # os.access preflight cannot settle this on its own — it can
        # disagree with the real operation under ACLs, and the permissions
        # can change between the check and the scan.
        with os.scandir(root) as entries:
            for entry in entries:
                if any(_is_day_file(entry.name, suffix) for suffix in suffixes):
                    found.append(Path(entry.path))
    return found


def _sweep_namespace_sources(config: Any, ns: str) -> int:
    """Unlink every day file belonging to ``ns``, across every index root."""
    removed = 0
    for path in _namespace_source_paths(config, ns):
        os.unlink(path)
        removed += 1
    return removed


def _entry_title() -> str:
    """A heading that is unique per write.

    Historical rationale: memtomem 0.3's ``append_entry`` defaulted to
    ``## Entry {now}`` at SECOND resolution, and two entries under the same
    heading in one file are re-chunked into a SINGLE chunk — so a second write
    inside the same second silently swallowed the first entry's text and
    RE-MINTED its chunk id. That id had already been returned to the caller,
    so the provenance share that follows looked the chunk up, found nothing,
    and fell back to a direct write with the link quietly gone. Two rapid
    writes to one namespace is not a corner case here: it is what a
    pipeline/council/decompose handoff does.

    memtomem 0.4's own default is now unique too (ms timestamp + full uuid);
    syncmill keeps supplying an explicit title of the same strength because
    the deterministic shape is pinned by hermetic tests and must not drift
    with the upstream default. It stays a plain ``## …`` heading, which is all
    the chunker requires, and only the heading changes — the ``> created:``
    line memtomem's own parsers read keeps its second resolution.
    """
    stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return f"Entry {stamp} {uuid4().hex}"


def _shared_tags(tags: Sequence[str], source_id: str) -> list[str]:
    kept = [t for t in tags if not t.startswith(_SHARED_FROM_PREFIX)]
    kept.append(f"{_SHARED_FROM_PREFIX}{source_id}")
    return kept


def _merge_rows(
    row_lists: Iterable[list[MemoryRow]], top_k: int
) -> list[MemoryRow]:
    """Adapter-side multi-namespace merge: dedupe by id keeping the highest
    score, sort by score descending, truncate. The fallback for backends
    without a native union-namespace search."""
    merged: dict[str, MemoryRow] = {}
    for rows in row_lists:
        for row in rows:
            prior = merged.get(row["id"])
            if prior is None or row.get("score", 0.0) > prior.get("score", 0.0):
                merged[row["id"]] = row
    return sorted(merged.values(), key=lambda row: -row.get("score", 0.0))[:top_k]


class MemtomemBackend:
    """Default backend and the sole owner of memtomem-private coupling."""

    def __init__(self, config: MemoryConfig):
        # Keep imports lazy: import or construction failures are advisory and are
        # caught by SyncmillMemory's existing construction boundary.
        from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
        from memtomem.integrations.langgraph import MemtomemStore

        self._agent_prefix = AGENT_NAMESPACE_PREFIX
        self._shared_ns = SHARED_NAMESPACE
        overrides: dict[str, dict[str, Any]] = {
            "storage": {"sqlite_path": config.db_path}
        }
        if config.memory_dir is not None:
            overrides["indexing"] = {"memory_dirs": [config.memory_dir]}
        # Kept for status(): reading it back off the store would touch the
        # private `_config_overrides`, and an upstream rename would then report
        # version skew as a connection failure.
        self._overrides = overrides
        self._store = MemtomemStore(config_overrides=overrides)
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
        # The SAME flow as add_returning_ids (ids discarded), so both writes land
        # in the namespace-suffixed daily file. Routing this through
        # MemtomemStore.add instead would append to the namespace-SHARED
        # `{date}.md`, which breaks namespace isolation in two ways. The markdown
        # is the index's source of truth while delete_by_namespace only removes
        # DB rows, so a GC'd `board-run:*` chunk survives in a file that a later
        # `shared` write re-indexes — and, its rows now gone, its text is treated
        # as new and stamped `shared`, leaking board-internal content into global
        # knowledge. And because one file's chunks are content-hash keyed, an
        # identical write to a second namespace is classified unchanged and
        # silently dropped.
        await self.add_returning_ids(content, tags=tags, namespace=namespace)

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

    async def search_multi(
        self,
        query: str,
        *,
        namespaces: Sequence[str],
        top_k: int,
        tag_filter: str | None = None,
    ) -> list[MemoryRow]:
        # One native merged-ranking call: NamespaceFilter.parse treats a comma
        # list as a union filter — the same shape mem_agent_search builds. The
        # namespace charset pattern forbids commas, but callers (search_context
        # builds names from free-form agent ids) don't all validate — so enforce
        # it here, or a comma-bearing name would silently widen the union
        # filter to namespaces the caller never asked for. Raising is fine on
        # this contract; SyncmillMemory degrades per call.
        if any("," in ns for ns in namespaces):
            raise ValueError(f"namespace contains a comma: {list(namespaces)!r}")
        namespace = ",".join(namespaces)
        if tag_filter is not None:
            return await self._store.search(
                query,
                namespace=namespace,
                top_k=top_k,
                tag_filter=tag_filter,
            )
        return await self._store.search(query, namespace=namespace, top_k=top_k)

    async def add_returning_ids(
        self, content: str, *, tags: list[str], namespace: str
    ) -> list[str]:
        # MemtomemStore.add discards IndexingStats.new_chunk_ids, so replicate
        # its short write flow (guard → lock → mix-refusal → append_entry →
        # index_file(lock_held=True)) to keep the ids. Lazy imports:
        # construction failures stay advisory.
        from memtomem.context._atomic import (
            _CRUD_SIDECAR_LOCK_BUDGET_S,
            async_file_lock,
        )
        from memtomem.memory_scope import require_user_base
        from memtomem.privacy import enforce_write_guard

        comp = await self._store._ensure_init()
        guard = enforce_write_guard(
            content,
            surface="syncmill_add",
            audit_context={"namespace": namespace},
        )
        if guard.decision == "blocked":
            raise WriteBlockedError("memtomem redaction guard blocked the write")
        base = require_user_base(comp.config.indexing.memory_dirs)
        # BEFORE the clock is read. `purge_namespace` holds this same lock for
        # the whole of its sweep → row delete → verify, so a write either
        # completes entirely before a purge begins or waits for it — never
        # lands a file whose rows the purge then takes. Deriving the target
        # first and locking only that file cannot give this: a writer suspended
        # between the two resumes with a stale target the purge never knew to
        # lock (see `_namespace_lock_path`).
        async with async_file_lock(
            _namespace_lock_path(base, namespace),
            timeout=_CRUD_SIDECAR_LOCK_BUDGET_S,
        ):
            return await self._locked_append(
                comp, base, content, tags=tags, namespace=namespace
            )

    async def _locked_append(
        self,
        comp: Any,
        base: Path,
        content: str,
        *,
        tags: list[str],
        namespace: str,
    ) -> list[str]:
        """The append itself, with this namespace's writers already excluded."""
        from memtomem.context._atomic import (
            _CRUD_SIDECAR_LOCK_BUDGET_S,
            _lock_path_for,
            async_file_lock,
        )
        from memtomem.memory_scope import namespace_mix_refusal
        from memtomem.tools.memory_writer import append_entry

        # A NAMESPACE-SUFFIXED daily file, deliberately diverging from
        # MemtomemStore.add's shared `{date}.md`: under memtomem's default
        # config, re-indexing one file that holds the same content-hash twice
        # collapses the chunks to the LAST write — so the two-step share
        # (identical content in agent-ns then `shared`, the mem_agent_share
        # copy) would DELETE the private original and orphan the provenance
        # link (a real FOREIGN KEY failure the opt-in real smoke caught).
        # Separate files per namespace keep both chunks alive. The ':'→'.'
        # mapping could collide only for a namespace literally containing the
        # dotted form (e.g. "agent-runtime.claude") — unreachable with
        # syncmill's fixed namespace vocabulary.
        target = base / f"{_utc_date_str()}.{namespace.replace(':', '.')}.md"
        # An EXPLICIT per-write heading, never append_entry's second-resolution
        # default: two entries sharing a heading in one file re-chunk into one,
        # swallowing the earlier text and re-minting the id already handed to
        # the caller (see `_entry_title`).
        #
        # `format_entry_block` IGNORES the title when the content supplies its
        # own `## ` heading, and in that branch it emits the tags blockquote
        # BEFORE that heading — so such a payload gets a shared heading (the
        # merge this guards against) AND loses its tags to the previous entry's
        # section. syncmill's writers deliberately label themselves in bold
        # instead (see memory.py's heading note); warn rather than mutate the
        # content, since the backend contract is verbatim storage.
        if content.strip().startswith("## "):
            logger.warning(
                "memory payload starts with a '## ' heading, so its unique "
                "per-entry heading and tags will be dropped by the writer; "
                "label the payload in bold instead"
            )
        # memtomem 0.4's write-locking contract (its issue #2005): the append
        # and its re-index must share one cross-process sidecar-lock span, or
        # the watcher can index the freshly appended file FIRST — without the
        # explicit namespace — and the agent-private chunk lands permanently in
        # `default`, exposed to non-private searches with the provenance share
        # broken (reproduced against the real 0.4 store). The mix-refusal read
        # sits inside the same span for the same reason: a guard that inspects
        # a file another writer may change before the append is decoration.
        # `lock_held=True` skips index_file's nested sidecar acquire, which
        # would otherwise self-deadlock (memtomem #1587). A lock timeout
        # (another process holds it past the budget) raises TimeoutError, which
        # SyncmillMemory degrades per call like any backend failure.
        async with async_file_lock(
            _lock_path_for(target), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S
        ):
            mix_err = await namespace_mix_refusal(
                index_engine=comp.index_engine,
                storage=comp.storage,
                default_namespace=comp.config.namespace.default_namespace,
                target=target,
                effective_ns=namespace,
                override_hint="a different target file",
            )
            if mix_err is not None:
                # Unreachable with syncmill's per-namespace files short of
                # legacy residue; refusing beats restamping stored rows.
                raise RuntimeError(
                    f"memtomem namespace mix refused for {target}: {mix_err}"
                )
            append_entry(target, content, title=_entry_title(), tags=list(tags))
            stats = await comp.index_engine.index_file(
                target, namespace=namespace, already_scanned=True, lock_held=True
            )
        # MCP-write parity — see `add`.
        comp.search_pipeline.invalidate_cache()
        return [str(cid) for cid in getattr(stats, "new_chunk_ids", ()) or ()]

    async def share_chunk(
        self, chunk_id: str, *, target_namespace: str
    ) -> str | None:
        # mem_agent_share semantics: COPY the chunk into the target namespace
        # with a collapsed shared-from tag, then record a chunk_links row per
        # copied chunk. NOTE: like MemtomemStore.get, the source lookup is NOT
        # namespace-scoped — today's only caller feeds ids it just minted; a
        # future promote-style surface must add a source-namespace check (the
        # gateway `get_shared` lesson) before exposing this to untrusted ids.
        source = await self._store.get(chunk_id)
        if source is None:
            return None
        new_ids = await self.add_returning_ids(
            source["content"],
            tags=_shared_tags(source.get("tags", []), chunk_id),
            namespace=target_namespace,
        )
        for new_id in new_ids:
            # Link recording is best-effort (the mem_agent_share posture): the
            # copy is durable, so a failed link must not report a failed share.
            try:
                storage = await self._storage()
                await storage.add_chunk_link(
                    UUID(chunk_id),
                    UUID(new_id),
                    "shared",
                    target_namespace,
                )
            except Exception:
                logger.warning(
                    "share provenance link failed; copy is durable", exc_info=True
                )
        return new_ids[0] if new_ids else None

    async def register_namespace(
        self, ns: str, *, description: str | None = None, color: str | None = None
    ) -> None:
        # mem_agent_register semantics: unconditional upsert (None leaves the
        # existing value untouched) — unlike ensure_namespace's create-if-missing.
        await (await self._storage()).set_namespace_meta(
            ns, description=description, color=color
        )

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
        # Our own overrides dict, not the store's private `_config_overrides` —
        # an upstream rename of that attribute would surface as AttributeError,
        # which SyncmillMemory.status reports as a CONNECTION failure (version
        # skew misdiagnosed as an outage).
        db_path = self._overrides.get("storage", {}).get("sqlite_path", "default")
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
        removed = await (await self._storage()).delete_by_namespace(ns)
        await self._invalidate_search_cache()
        return removed

    async def _invalidate_search_cache(self) -> None:
        """Drop the search pipeline's TTL result cache after a delete.

        The same MCP-parity rule the write path follows, for the same reason in
        the other direction: the pipeline caches result sets, so a reader that
        searched before a delete keeps being served the deleted rows until the
        TTL expires. For a ``board-run:*`` purge that means handing a later
        reader exactly the board-internal text the purge existed to remove —
        the leak this whole path guards, arriving from memory instead of from a
        reindex. Deliberately best-effort: the rows and files are already gone,
        so a cache that will expire on its own must not turn a completed purge
        into a reported failure.
        """
        try:
            comp = await self._store._ensure_init()
            comp.search_pipeline.invalidate_cache()
        except Exception:  # noqa: BLE001 - the purge itself already succeeded
            logger.warning("search cache invalidation failed", exc_info=True)

    async def delete_namespace_sources(self, ns: str) -> int:
        """Remove the markdown source files this backend wrote for ``ns``.

        ``delete_by_namespace`` only removes database rows, but the markdown is
        what the index is rebuilt FROM: a watcher, ``mm index``, or any later
        write to the same file re-indexes the deleted text and — its rows now
        gone — treats it as new content. Without this a deleted namespace comes
        back on the next reindex.

        Two file shapes hold a namespace's content, and both are matched: the
        ``{date}.{ns}.md`` files this backend's replicated add flow writes, and
        the ``{date}--{slug}-{digest}.md`` files memtomem 0.4 natively writes
        for the same namespace (CLI ``mm add``, the ``mem_add`` MCP tool — the
        surfaces that call ``day_file_name``; the watcher only re-indexes what
        they wrote, which is what makes a surviving file resurrect).

        A name is matched only when it is a whole ``{date}`` + one of those two
        suffixes: a bare ``*{suffix}`` glob would also take ``backup--{slug}-
        {digest}.md`` or ``notes.{ns}.md``, and the digest proves the namespace,
        never that syncmill wrote the file or that the prefix is a date. For the
        same reason ``ns`` is never interpolated INTO a glob pattern — it is
        caller-supplied, and a ``*`` or ``[`` in it would silently widen the
        sweep — so the glob is the literal ``*.md`` and every candidate is
        compared as an exact string.

        RAISES on any failure rather than reporting a misleading zero: the
        caller tombstones a purged namespace, and a namespace whose source is
        still on disk has not been purged — it must stay collectable so a
        later sweep retries it. Every root the indexer reads is swept, not just
        the user tier: a file under a registered project root is re-indexed
        exactly like a user-tier one, so leaving it behind resurrects the
        namespace the caller was told had been purged. A root that cannot be
        verified (missing, not a directory, unreadable — an unmounted project
        tier) RAISES rather than being skipped, for the same reason: skipping
        it would report a clean purge over files still on disk.

        **Known limitations.** (1) A store written by an earlier version put
        every namespace in one shared ``{date}.md``. Those files are not
        matched here and cannot be — the entries carry no namespace, so there
        is nothing to select on, and deleting the file would take shared
        knowledge with it. Scratch written before the upgrade can therefore
        still be re-indexed. (2) The legacy suffix maps ``:`` to ``.``, which is
        not injective: ``a:b`` and ``a.b`` produce the same file name. That
        comes from the WRITE side (see ``add``) and predates this method, and
        upstream's mix-guard means such a file only ever holds one of the two —
        so the exposure is deleting the other namespace's source, not mixing
        them. Unreachable for the only caller, which builds ``board-run:{uuid}``
        from a uuid4; closing it would mean changing the write-side name and
        orphaning every file already on disk.
        """
        config = (await self._store._ensure_init()).config
        return _sweep_namespace_sources(config, ns)

    async def purge_namespace(self, ns: str) -> int:
        """Remove a namespace's sources AND rows with writers locked out.

        The two-call sequence this replaces — sweep sources, then delete rows —
        had no exclusion between its halves. A write landing in that window
        creates a day file AND its row, the row delete then takes the row, and
        the markdown is left with nothing claiming it: the next reindex reads
        board-internal text as new content and stamps it with whatever
        namespace that pass resolves, which is how scratch reaches global
        ``shared``. Ordering and a second verify sweep NARROW that window; only
        holding the writers out closes it.

        **What is excluded, exactly.** Two levels, because two different things
        touch these files.

        WRITERS take the namespace lock: the add flow is the only producer of
        ``board-run:*`` content, it has a single write path (``add`` routes
        through ``add_returning_ids``), and that path takes this namespace's
        lock BEFORE it derives any file name. So a write either finishes before
        this purge starts or waits for it.

        READERS — memtomem's watcher and manual indexers — take each file's L2
        sidecar instead, and an indexer that has read a file but not yet
        committed its rows would otherwise resurrect the namespace right after
        this method reported success. So the purge also takes the L2 sidecar of
        every source that EXISTS, enumerated from disk under the namespace lock.
        That is not the date enumeration that made the first version of this
        unsound: those were paths a writer might LATER name, unbounded because a
        suspended writer's target can be arbitrarily old; these are paths found
        on disk now.

        An earlier version instead locked the writer's per-file L2 sidecar for
        today's and tomorrow's date. That was unsound, and the reason is worth
        keeping: the writer derives its target BEFORE taking any lock, so a
        process suspended in between resumes holding a target for an arbitrarily
        old date, which no finite set of dates covers. See
        ``_namespace_lock_path``.

        **What is NOT excluded.** A memtomem-native writer (``mm add``, the
        ``mem_add`` MCP tool) does not take syncmill's namespace lock, so it can
        still interleave. Note this is NOT because its file name is unknowable —
        ``day_file_name`` derives it deterministically, and the digest is over
        the namespace, not the date — but because upstream has no namespace-level
        lock to share (its documented lock domain is per-file, L0-L4 in
        ``context/_atomic.py``) and locking its file paths would need the same
        unsound date enumeration. Nothing writes a ``board-run:{uuid4}`` scratch
        namespace that way, since syncmill mints those ids and no other surface
        knows them, but the guarantee is scoped to syncmill's own writers rather
        than absolute.

        The verify sweep therefore stays, and now means something narrow and
        sharp: it can only fire for a native writer, since syncmill's are locked
        out and in-flight indexers are held. It RAISES rather than reporting a
        count, because a purge that cannot be proven complete must leave the
        namespace collectable — the caller tombstones on success, and a
        namespace with a live producer must not be retired from collection.
        Note what it is NOT: a proof of completeness. It samples once, so a
        native write landing after it is still missed; it is best-effort
        detection of the one interleaving the locks do not cover.

        A lock the budget cannot obtain raises TimeoutError, which the caller
        degrades like any other backend failure — again leaving the namespace
        collectable, with no retry here (a scratch GC has a later chance, and
        blocking board dispatch on it would be worse).

        Returns the number of source files removed.
        """
        from contextlib import AsyncExitStack

        from memtomem.context._atomic import (
            _CRUD_SIDECAR_LOCK_BUDGET_S,
            _lock_path_for,
            async_file_lock,
        )
        from memtomem.memory_scope import require_user_base

        config = (await self._store._ensure_init()).config
        base = require_user_base(config.indexing.memory_dirs)
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                async_file_lock(
                    _namespace_lock_path(base, ns),
                    timeout=_CRUD_SIDECAR_LOCK_BUDGET_S,
                )
            )
            # The namespace lock stops a syncmill WRITE, but memtomem's watcher
            # and manual indexers are READERS of these files: they take the
            # file's L2 sidecar, not this lock. One can hold L2, read a
            # board-run file, and pause before its DB transaction — and if the
            # purge ran through, it would unlink the file, delete the rows,
            # verify clean, and report success, after which the indexer resumes
            # and upserts its buffered chunks into a namespace the caller has
            # already been told is gone.
            #
            # So take the L2 sidecar of every source that EXISTS before
            # touching any of them. This is not the date enumeration that made
            # the first version unsound: those were paths a writer MIGHT later
            # name, unbounded because a suspended writer's target can be
            # arbitrarily old, whereas these are paths found on disk right now.
            # Sorted, per upstream's L2 multi-acquire rule; taken under the
            # namespace lock, never the reverse.
            #
            # A file appearing after this scan is by definition one no syncmill
            # writer created (they are locked out) — the unexcluded native
            # writer — and the verify sweep below is what notices it.
            existing = _namespace_source_paths(config, ns)
            for lock_path in sorted(
                {_lock_path_for(path) for path in existing}, key=str
            ):
                await stack.enter_async_context(
                    async_file_lock(lock_path, timeout=_CRUD_SIDECAR_LOCK_BUDGET_S)
                )
            # SOURCES FIRST, still: the rows are what keep a surviving file
            # harmless, since a reindex that recognizes the text leaves its
            # namespace alone. Under the lock no syncmill writer can interleave,
            # so the pair is atomic against them; the order continues to decide
            # what a FAILURE between the two leaves behind.
            removed = _sweep_namespace_sources(config, ns)
            await (await self._storage()).delete_by_namespace(ns)
            # Inside the span: a reader that searched before this purge would
            # otherwise keep being served the purged rows from the TTL cache.
            await self._invalidate_search_cache()
            raced = _sweep_namespace_sources(config, ns)
        if raced:
            raise RuntimeError(
                f"namespace {ns} gained {raced} source file(s) during its purge; "
                "a writer this lock cannot exclude is active, so the purge is "
                "not provably complete"
            )
        return removed

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


def _resolve_mem0_config(config: MemoryConfig) -> tuple[Path, dict[str, Any]]:
    """Resolve Mem0 defaults without importing Mem0 or touching the filesystem.

    Migration storage binding consumes the same resolved vector/history settings
    as the runtime adapter.  Keeping this step pure lets migration commands reject
    a mismatched destination before they construct an SDK client or create a DB.
    """
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
    if embedder.get("provider") != "fastembed" and "model" not in user_embed_cfg:
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
    return data_dir, resolved


class Mem0Backend:
    """Optional mem0 OSS reference backend, constrained to Qdrant.

    The adapter maps each syncmill namespace to one mem0 ``user_id`` and forces
    ``infer=False`` on writes. Qdrant is deliberately required because P2 needs
    exact namespace counts and proof that bulk deletion reached zero.
    """

    def __init__(self, config: MemoryConfig):
        mem0_cfg = config.mem0
        data_dir, resolved = _resolve_mem0_config(config)
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
            native_multi_namespace_search=False,
            share_provenance_links=False,
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
        await self.add_returning_ids(content, tags=tags, namespace=namespace)

    async def add_returning_ids(
        self, content: str, *, tags: list[str], namespace: str
    ) -> list[str]:
        response = await self._store.add(
            [{"role": "user", "content": content}],
            user_id=namespace,
            metadata={"syncmill_tags": list(tags)},
            infer=False,
        )
        rows = response.get("results", []) if isinstance(response, dict) else []
        if len(rows) != 1 or rows[0].get("memory") != content:
            raise RuntimeError("mem0 did not confirm one verbatim memory write")
        row_id = rows[0].get("id")
        return [str(row_id)] if row_id else []

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
        raw_tags = metadata.get("syncmill_tags")
        tags = (
            [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
        )
        return MemoryRow(
            id=str(row.get("id", "")),
            content=str(row.get("memory", "")),
            score=float(row.get("score") or 0.0),
            source=str(metadata.get("source") or "mem0"),
            namespace=str(namespace),
            tags=tags,
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

    async def search_multi(
        self,
        query: str,
        *,
        namespaces: Sequence[str],
        top_k: int,
        tag_filter: str | None = None,
    ) -> list[MemoryRow]:
        return _merge_rows(
            [
                await self.search(
                    query, namespace=ns, top_k=top_k, tag_filter=tag_filter
                )
                for ns in namespaces
            ],
            top_k,
        )

    async def share_chunk(
        self, chunk_id: str, *, target_namespace: str
    ) -> str | None:
        source = await self.get(chunk_id)
        if source is None:
            return None
        new_ids = await self.add_returning_ids(
            source["content"],
            tags=_shared_tags(source.get("tags", []), chunk_id),
            namespace=target_namespace,
        )
        return new_ids[0] if new_ids else None

    async def register_namespace(
        self, ns: str, *, description: str | None = None, color: str | None = None
    ) -> None:
        return None

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
# One page of asearch results; count_namespace's stop condition compares
# against this same constant so the two can't drift.
_LANGGRAPH_PAGE_SIZE = 100
# Iteration backstop for count_namespace: far above any real namespace (a
# syncmill namespace holds task results, not a corpus), hit only by a store
# whose asearch ignores `offset` and would otherwise loop forever.
_LANGGRAPH_COUNT_CAP = 1_000_000


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
            native_multi_namespace_search=False,
            share_provenance_links=False,
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
        await self.add_returning_ids(content, tags=tags, namespace=namespace)

    async def add_returning_ids(
        self, content: str, *, tags: list[str], namespace: str
    ) -> list[str]:
        store = await self._ensure_store()
        key = str(uuid4())
        await store.aput(
            _LANGGRAPH_NS,
            key,
            self._value(content, tags, namespace),
            index=["content"],
        )
        return [key]

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

    async def search_multi(
        self,
        query: str,
        *,
        namespaces: Sequence[str],
        top_k: int,
        tag_filter: str | None = None,
    ) -> list[MemoryRow]:
        return _merge_rows(
            [
                await self.search(
                    query, namespace=ns, top_k=top_k, tag_filter=tag_filter
                )
                for ns in namespaces
            ],
            top_k,
        )

    async def share_chunk(
        self, chunk_id: str, *, target_namespace: str
    ) -> str | None:
        source = await self.get(chunk_id)
        if source is None:
            return None
        new_ids = await self.add_returning_ids(
            source["content"],
            tags=_shared_tags(source.get("tags", []), chunk_id),
            namespace=target_namespace,
        )
        return new_ids[0] if new_ids else None

    async def register_namespace(
        self, ns: str, *, description: str | None = None, color: str | None = None
    ) -> None:
        return None

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

    async def _page(
        self, ns: str, *, limit: int = _LANGGRAPH_PAGE_SIZE, offset: int = 0
    ) -> list[Any]:
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
            if not page:
                return total
            total += len(page)
            if len(page) < _LANGGRAPH_PAGE_SIZE:
                return total
            # A store that ignores `offset` returns the same full page forever;
            # without a cap that spins unbounded (the mem0 adapter's no-progress
            # guard, adapted to offset paging).
            if total > _LANGGRAPH_COUNT_CAP:
                raise RuntimeError(
                    f"LangGraph namespace count made no progress for {ns!r}"
                )

    async def delete_namespace(self, ns: str) -> int:
        original = await self.count_namespace(ns)
        store = await self._ensure_store()
        remaining = original
        while True:
            page = await self._page(ns)
            if not page:
                break
            for item in page:
                await store.adelete(_LANGGRAPH_NS, item.key)
            # No-progress guard (the mem0 adapter's recipe): if a pass deleted
            # nothing — adelete no-ops on a mismatched key, a permission issue —
            # the same page would come back forever, an unbounded hang worse
            # than the failure it hides.
            after = await self.count_namespace(ns)
            if after >= remaining:
                raise RuntimeError(
                    f"LangGraph namespace deletion made no progress for {ns!r}"
                )
            remaining = after
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
    # Defensive, like the unknown-backend branch above: no shipped backend sets
    # verbatim_storage=False (all three leave the default True), so this is
    # reachable only from tests/monkeypatching — it exists to make the contract
    # explicit for a future adapter, not to guard a live path.
    if not backend.capabilities.verbatim_storage:
        raise ValueError(
            f"backend {config.backend!r} cannot store verbatim content"
        )
    return backend
