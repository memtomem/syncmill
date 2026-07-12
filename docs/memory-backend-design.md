# Design memo: pluggable long-term-memory backend

**Status: P1 + P2 + P3 + MEM0 HARDENING + BIDIRECTIONAL MIGRATION IMPLEMENTED (2026-07-12) — memtomem remains the default and optional mem0 OSS is the Qdrant-backed reference adapter proving substitution.**

This memo evaluates whether syncmill's long-term memory should stay hard-wired to
[memtomem](https://github.com/memtomem/memtomem) or grow a pluggable backend seam,
surveys four candidate systems (mem0, Zep/Graphiti, Letta, LangMem), and proposes a
minimal `MemoryBackend` protocol plus a phased path to a second backend.

Research date: 2026-07-12. External facts are grounded in official docs and main-branch
source read directly (mem0 `mem0/memory/main.py`, graphiti-core `main@526dcad`,
LangGraph `store/base`, Letta docs + PyPI), verified adversarially. Version-sensitive
claims should be re-pinned at implementation time.

Reviewed by codex twice (read-only, 2026-07-12), all findings verified against source and
applied. Round 1 (9 findings): the C4a/C4b split (the gateway's `shared_stats` is a second,
permanent enumeration-family consumer), the C8 readiness probe, the `scoped()` backend
hand-off, the constructor-literal ordering. Round 2 (6 findings) hardened the P1 sketch's
byte-identity: `status()` adds no `backend` field; a bare `ensure_ready()` (C8a) — not full
`status()` — backs `delete_board_runs`' `None`-vs-`0`; a single-try constructor with the
one residual micro-divergence documented; the full `raise_on` key-migration table; and N1
reworded so enumeration is capability-gated, not required.

---

## 1. Context — why look at this now

`SyncmillMemory` (`src/syncmill/memory.py`) is syncmill's single coupling point to
memtomem: the Supervisor is the sole reader/writer, and every operation is best-effort
(a broken memtomem degrades, never aborts a run). Two facts make the backend seam worth
naming explicitly:

1. **An existing, un-named abstraction.** `SyncmillMemory` already *is* the adapter —
   it translates syncmill's namespace/tag conventions onto memtomem calls. Its underside
   is just not a declared contract.
2. **Real, load-bearing debt.** Four `SyncmillMemory` methods reach into memtomem's
   **private internals** — `_store._ensure_init()`, and
   `_store._components.storage.{list,get,set}_namespace_meta` / `delete_by_namespace`
   (`memory.py:122-128, 325-326, 357-358, 390-391`). A memtomem minor upgrade can break
   these. Promoting them to a public contract is worthwhile *independent of* ever adding a
   second backend.

This is not speculative abstraction (the repo's "don't pre-abstract" invariant): the debt
already exists in code today. The minimal deliverable is **normalizing the current
coupling**, not "backend swapping."

## 2. The capability contract syncmill actually needs

The whole surface `SyncmillMemory` uses against `MemtomemStore` is seven core operations
plus three optional session calls. This is the upper bound of any backend protocol.

### 2.1 Core operations (required)

| # | Operation | Current call shape | Call site | Return requirement |
|---|-----------|--------------------|-----------|--------------------|
| C1 | add | `add(content, tags, namespace)` | `share_result`, `share_board_result` | none — id not needed; cross-link is a tag |
| C2 | search | `search(query, namespace=, top_k=)` | `search_context`, `search_shared` | `list[dict]` rows keyed `id`, `content`, `score`, `source`, `namespace` |
| C3 | get by id | `get(chunk_id)` | `get_shared` | `dict\|None` — **must carry `namespace`** (the isolation guard for an unscoped get) |
| C4a | count one namespace | `storage.list_namespace_meta()` *(private!)* | `shared_stats` (→ MCP gateway `shared_stats` tool) | chunk count for a *given* ns |
| C4b | enumerate namespaces | removed in P2 | no production caller | not part of the protocol |
| C5 | delete a namespace as a unit | `storage.delete_by_namespace(ns)` *(private!)* | `delete_board_runs` | int; transactional (chunks+FTS+vectors+meta) |
| C6 | ensure namespace / meta | `storage.get/set_namespace_meta(...)` *(private!)* | `register_agent` | existence guarantee (no-op OK if implicit-create) |
| C7 | close | `close()` | `close` | — |
| C8 | readiness probe | `_store._ensure_init()` + `_config_overrides` *(private!)* | `status()` (CLI branches on `connected`), `delete_board_runs` up-front check | raises when unreachable; info dict on success |

C4 is deliberately split: **counting one namespace (C4a)** serves the read-only MCP
gateway's `shared_stats` and must survive every phase; **enumeration (C4b)** served only
the old board-run GC and P2 removed it (§4.1). C8 was a fifth private-internal
reach hiding in `status()` and `delete_board_runs`' up-front init (whose failure is what
distinguishes `None` = "unreachable, couldn't try" from `0` = "tried, nothing purged").

### 2.2 Episodic sessions (optional — degradable)

`start_session` / `log_event` / `end_session` back the per-task episodic log. syncmill runs
fully without them (every method self-degrades), so a backend that lacks sessions gets a
no-op adapter behind a `supports_sessions=False` capability flag.

### 2.3 Hidden contract (must be documented on the protocol)

- **N1 — namespaces are cheap logical buckets.** String keys, implicit-create on write,
  **countable by known key (C4a) and unit-deletable (C5)** — these are the required
  demands. **Full enumeration (C4b) is not required**: P2 moved scratch ownership to the
  board ledger, so a backend like mem0 that cannot enumerate is still first-class.
  syncmill mints a `board-run:{run_id}` scratch namespace per board
  run and GCs it. Systems where a "scope" is an expensive object (a vector-DB collection =
  an index) fail here.
- **N2 — add need not return an id.** The board↔knowledge cross-link is the composite tag
  `board-result:{item_id}`; the designed retrieval key is **exact tag match** (`tag_filter`,
  comma = OR). *Prospective:* syncmill currently only **writes** the tag — no production
  call passes `tag_filter` to search yet (it exists in a `share_board_result` docstring,
  for external MCP consumers reading via memtomem directly). The protocol keeps the
  parameter because it is the documented retrieval contract, but a backend without tag
  filtering breaks nothing that ships today (`supports_tag_filter` flag).
- **N3 — get is not namespace-scoped.** The caller guards isolation via the returned row's
  `namespace`.
- **N4 — every op is best-effort.** The backend may raise freely; `SyncmillMemory` catches
  and degrades. Backends must **not** add their own retry/fallback (no double recovery).
- **N5 — async.** syncmill bridges into its sync world via `run_async` (a shared module
  loop). A sync-only SDK is wrapped with `asyncio.to_thread` in its adapter.
- **N6 — verbatim storage (non-negotiable).** syncmill writes deterministic summaries
  (task results, board summaries) it must retrieve **byte-for-byte**. Any backend that
  LLM-rewrites content on the write path violates this and is rejected at construction.
- **N7 — duplicate-write semantics.** Today writes are at-least-once (no retry; the memtomem
  *library* path has no idempotency ledger — that lives in memtomem's server tool layer).
  The protocol documents "tolerate duplicate adds"; adding retries later requires a
  `board-result:{item_id}`-derived idempotency key.
- **N8 — run_id must satisfy namespace grammar.** `board-run:{run_id}` must be a legal
  namespace (memtomem: segments `[A-Za-z0-9._-]+`). A `validate_namespace(ns) -> bool` hook
  makes this a structural check. Board dispatch constructs and validates the scope before
  claiming: a definitive rejection hard-fails, while an unavailable or unexpectedly
  failing validator remains advisory and dispatch continues in degraded mode.
- **N9 — data minimization is upstream.** Not storing secrets/bodies is syncmill's own
  contract (board-block strip, 1000-char cap), not something delegated to a backend guard.

## 3. Candidate systems

All four assessed as **passive backends** — syncmill brings its own agents and wants only
storage/retrieval, *not* an agent runtime or LLM-driven memory extraction.

### 3.1 mem0 (`mem0ai`, OSS) — leading candidate

- **API:** `Memory.add(messages, *, user_id/agent_id/run_id, metadata=, infer=True, ...)`;
  `search(query, *, top_k=20, filters=None, threshold=, ...)` — since 1.x, top-level scope
  kwargs on search raise `ValueError`; scoping is **filters-dict only**. Plus `get`,
  `get_all`, `delete`, `delete_all(...)`, `reset`.
- **Scoping:** `user_id`/`agent_id`/`run_id` = implicit-create buckets, ≥1 mandatory on add.
  **Unit-deletable** via `delete_all`, but **OSS `Memory` has no scope-enumeration API** (C4
  gap; only the hosted `MemoryClient` enumerates).
- **Verbatim (N6):** `infer=False` stores content unmodified with **zero LLM calls**
  (source-verified: the not-infer branch of `_add_to_vector_store`). Caveats: an embedding
  call still fires per message; `system`-role messages are skipped; dedup is forfeited. The
  **default** `infer=True` runs LLM fact-extraction per add (default OpenAI `gpt-5-mini` +
  `text-embedding-3-small`, `OPENAI_API_KEY` required) — so the *default* is LLM-heavy; the
  passive path is `infer=False`.
- **Tag filter:** the filters dict does exact key/value match plus `eq/ne/in/contains/`
  wildcard/AND-OR-NOT — but must ride alongside ≥1 scope id, and advanced operators are
  vector-store-dependent (exact match is universal).
- **Self-hosting:** pip-installed in-process library; default embedded on-disk Qdrant +
  SQLite history; Ollama etc. for fully-local inference. Server/cloud optional.
- **Sync/async:** `Memory` + `AsyncMemory`, full parity, shared storage layer.
- **License/maintenance:** Apache-2.0, actively maintained (~60k stars, release 2026-07-01).

### 3.2 LangGraph `BaseStore` — strong runner-up (LangMem itself: poor)

Assess two layers separately:

- **LangMem the library = poor.** Its differentiating layer
  (`create_memory_manager` / `create_memory_store_manager`) **mandates an LLM on the write
  path** (conversation → structured memory) — exactly what syncmill does not want. Its store
  tools are thin agent-tool wrappers over `BaseStore`. MIT but still 0.0.x with near-dormant
  2026 maintenance.
- **Bare `BaseStore` = good** (the real candidate; usable with **no** LangMem and **no**
  running graph — `store = SqliteStore.from_conn_string(...); store.put(...)`):
  - API: `put(namespace: tuple, key, value: dict, index=)`, `search(namespace_prefix, *,
    query=, filter=, limit=)`, `get`, `delete(ns, key)`, `list_namespaces(prefix=, suffix=,
    max_depth=)` — **full async mirrors** (`aput`/`asearch`/…).
  - Namespaces are **hierarchical string tuples** (`("board-run", run_id)`), searched by
    prefix — a natural map for syncmill's colon strings (constraints: no `.` in segments,
    cannot start with `langgraph`).
  - **Local vector search:** `SqliteStore`/`AsyncSqliteStore` (`langgraph-checkpoint-sqlite`,
    **sqlite-vec** extension) — fully local embedded, like memtomem; `PostgresStore`+pgvector
    at scale. Indexed writes embed configured fields (`index=False` opts out per item).
    **No LLM required; verbatim.**
  - `filter=`: value exact-match + `$eq/$ne/$gt/...`. **No documented list-membership
    (`$in`)** → model tags as scalar fields (one key per tag).
  - **Two gaps:** (C4) `list_namespaces` enumerates but gives **no counts**; (C5) **no atomic
    namespace delete** — per-key delete loop or out-of-band SQL. Both solvable with a shim.
  - MIT, langgraph very active (v1.2.9, 2026-07), Python 3.12, sync + async.

### 3.3 Letta (MemGPT) — rejected

- **Client-server only.** No in-process mode (old MemGPT `LocalClient` deprecated).
  Self-host = run a `letta` server (Docker = Postgres+pgvector; pip = unsupported-migration
  SQLite). **The V1 server is officially repositioned as a "no-longer-supported legacy
  surface" as of 2026** — a risky foundation for a new local-first system.
- Namespace analog = **Archives** (named, CRUD, unit-deletable) — clean for agent-free
  *writes* (`archives.passages.create(text, tags, metadata)`).
- **Fatal mismatch:** semantic **search is exposed only through a Letta agent**
  (`agents.passages.search(agent_id, ...)`), so reading memory back needs a dummy Letta
  agent (each with its own LLM+embedding config) per namespace — collides with syncmill's
  bring-your-own-agents design. No get-by-id endpoint (C3 unmet).
- Apache-2.0, `letta-client` async SDK, Python 3.12 OK — but the above rules it out.

### 3.4 Zep / Graphiti (OSS engine) — rejected for this use

- **Write path mandates an LLM every time.** `add_episode` always runs LLM entity/edge
  extraction; there is no plain-chunk escape hatch. → cannot act as a passive chunk store,
  a hard cost/latency difference vs mem0's `infer=False`.
- Scoping is solid (implicit `group_id` on write, `group_ids` filter on read,
  `clear_data(group_ids)` for unit delete — but **no group-enumeration API**).
- **Verbatim retrieval is BM25-only** via the episode path; the default `search()` returns
  graph facts/edges, not chunks, with no vector similarity over episode text.
- **Tag filtering is declared but unwired**: `episode_metadata` and
  `SearchFilters.property_filters` exist as fields but have **zero consumers** in the core
  Cypher path at `main@526dcad` — exact-match tag filtering does not actually function.
- Apache-2.0, active (~28k stars), can run fully local via `graphiti-core[falkordblite]`
  (Python 3.12+). Valuable as a **knowledge-graph** store, not as a namespace-scoped
  verbatim-chunk backend.

## 4. Capability matrix

Legend: ✅ supported · 🟡 partial / workaround · ❌ not supported

| Axis | memtomem (baseline) | mem0 (`infer=False`) | LangGraph BaseStore | Letta | Graphiti |
|------|:---:|:---:|:---:|:---:|:---:|
| C1 add (verbatim+tags+ns) | ✅ | ✅ | ✅ | ✅ | ❌ (LLM forced) |
| C2 scoped search + top_k | ✅ | ✅ | ✅ | 🟡 (agent-gated) | 🟡 (facts/BM25) |
| C2 tag exact-filter | ✅ | ✅ | 🟡 (scalar only) | ✅ | ❌ (unwired) |
| C3 get by id | ✅ | ✅ | ✅ | ❌ | 🟡 (driver-level) |
| **C4a count one namespace** | ✅ | 🟡 (get_all len) | 🟡 (search count) | 🟡 (list len) | ❌ |
| **C4b enumerate namespaces** | ✅ | ❌ | 🟡 (no counts) | 🟡 (no counts) | ❌ |
| **C5 unit namespace delete** | ✅ | ✅ | 🟡 (adapter loop) | ✅ | 🟡 (util) |
| Sessions (S1–S3) | ✅ | ❌ | ❌ | 🟡 | ❌ |
| **N6 verbatim (no LLM)** | ✅ | ✅ | ✅ | ✅ | 🟡 (BM25 recall) |
| N5 async | ✅ | ✅ | ✅ | ✅ | ✅ (async-only) |
| Local embedded | ✅ | ✅ | ✅ | ❌ (server) | 🟡 (falkordblite) |
| Write-path cost | embed | embed | embed (if indexed) | embed | **LLM + embed** |
| License | — | Apache-2.0 | MIT | Apache-2.0 | Apache-2.0 |

### 4.1 The finding that decides the design

Sessions degrade cleanly everywhere. The real friction is **namespace lifecycle** — and it
splits cleanly by caller:

- **C4b enumeration** had exactly **one** caller: the old `board-run:{run_id}` scratch GC.
- **C4a counting one namespace** has a second, permanent consumer: the read-only MCP
  gateway's `shared_stats` tool (`memory.py:shared_stats` → `gateway.py`). It cannot be
  moved to the board ledger — but it is a far weaker demand (count *one known* namespace,
  not enumerate all).
- **C5 unit delete** likewise serves only the GC.

**Implemented design:** the scratch-namespace **ledger lives in syncmill's own board
SQLite** (`~/.syncmill/board.db`). Claiming atomically inserts one durable row per run;
schema migration backfills historical claim transitions and current leases. A single
SQLite snapshot selects the oldest 100 unpurged rows with no live running lease. Memory
deletes those known ids independently, and the board tombstones only per-id acknowledgements,
so partial failure is retried. Run exit reactivates its row best-effort to cover late writes;
SIGKILL between a late recreation and that finalizer can leave residue. This removes
**C4b** from the backend protocol; **C4a survives as a narrow
`count_namespace(ns)`** (mem0: `get_all(filters=...)` length or metadata — a shim;
`BaseStore`: paginated `search` count — a shim); C5 reduces to "delete these known
namespaces", which each adapter implements internally (per-key loop where no atomic op
exists — see the `namespace_delete_is_atomic` flag, §5). This promotes both mem0 and
`BaseStore` to first-class backends. The memtomem adapter keeps its native
enumerate/unit-delete throughout.

## 5. Proposed `MemoryBackend` protocol

```python
class MemoryBackend(Protocol):
    @property
    def capabilities(self) -> BackendCapabilities: ...
    def validate_namespace(self, ns: str) -> bool: ...            # N8

    # core (C1–C3, C6, C7, C8)
    async def add(self, content: str, *, tags: list[str], namespace: str) -> None: ...
    async def search(self, query: str, *, namespace: str, top_k: int,
                     tag_filter: str | None = None) -> list[MemoryRow]: ...
    async def get(self, chunk_id: str) -> MemoryRow | None: ...
    async def ensure_namespace(self, ns: str, *, description: str = "") -> None: ...
    async def status(self) -> dict[str, Any]: ...                  # C8: raises if unreachable
    async def close(self) -> None: ...

    # namespace lifecycle (see §4.1)
    async def count_namespace(self, ns: str) -> int: ...           # C4a — permanent
    async def delete_namespace(self, ns: str) -> int: ...          # C5 — adapter owns any loop

    # sessions (optional — no-op when supports_sessions=False)
    async def start_session(self, agent_id: str, namespace: str) -> str | None: ...
    async def log_event(self, kind: str, text: str) -> None: ...
    async def end_session(self, summary: str | None) -> None: ...
```

`BackendCapabilities` flags: `supports_sessions`, `supports_tag_filter` (prospective — see
N2), `namespace_delete_is_atomic` (C5 is **always implemented** — the adapter owns
any internal enumerate-and-delete loop; the flag only documents whether a partial failure
can leave a half-deleted namespace, which callers may surface in logs), `verbatim_storage`
(**`False` → construction refused**; N6 is non-negotiable), `namespace_pattern` (regex; N8
— P2 calls the validator at the pre-claim scope-construction boundary).

Wiring: `SyncmillMemory.__init__` calls `build_backend(config.memory)` with
`[memory] backend = "memtomem"` (default). The `MemtomemBackend` adapter absorbs today's
code **and its private-internal reach** — the debt-normalization that stands on its own.
Config extends the existing `MemoryConfig` (`backend` + `[memory.mem0]` / `[memory.langgraph]`
sub-blocks), following the same env/toml precedence as the rest of `config.py`.

## 6. Recommendation

### Phases (each independently approvable)

- **P1 — normalize the seam (value now, no second backend needed).** Define
  `MemoryBackend` + capabilities; isolate the memtomem private-internal reach into a
  `MemtomemBackend` adapter. Behavior byte-identical; removes the upgrade-fragility. Worth a
  standalone PR.
- **P2 — implemented: move board-run GC to the board ledger (§4.1).** Drops C4b
  (enumeration) from the protocol — C4a (`count_namespace`) stays for the gateway's
  `shared_stats`; validates the scratch scope before claim and uses per-id delete
  acknowledgements plus durable tombstones.
- **P3 — implemented: prove swappability with mem0 OSS.** The optional adapter uses
  `infer=False`, maps syncmill namespaces onto mem0 user IDs, and constrains storage to
  Qdrant so exact counts and retry-safe deletion preserve the P2 lifecycle contract.

### Shipped second backend — mem0 (`infer=False`)

- **mem0** is an optional extra, while memtomem remains installed/default. Every add uses
  `infer=False`; namespaces map to mem0 `user_id`, tags to metadata, and result rows are
  normalized at the adapter boundary. Local Qdrant + multilingual FastEmbed is the no-key
  default, with telemetry disabled. Embedded Qdrant exclusively locks its path; operators
  needing concurrent processes can configure a Qdrant server while keeping Mem0 history
  under `data_dir`. P3 restricts the adapter to Qdrant because the public bulk delete can
  batch/partially fail: exact Qdrant counts and progress checks are what let the board
  acknowledge deletion safely.
- **LangGraph BaseStore** remains an evaluated option, not a planned or shipped adapter.
  Its documented local store is in-memory; persistent choices introduce another service,
  so another adapter requires separate scope rather than extending this roadmap.

### Rejected, with rationale

- **Letta** — client-server forced (no in-process), search reachable only through a Letta
  agent (needs a dummy agent per namespace, violating bring-your-own-agents), V1 server
  declared legacy.
- **Graphiti** — mandatory per-write LLM extraction (no passive chunk store), BM25-only
  verbatim recall, tag filtering unwired. Keep only as an optional knowledge-graph backend.

### Non-goals / guardrails

- P1, P2, P3, Mem0 hardening, and staged memtomem↔Mem0 migration were separately
  implemented. Migration is a source-retaining copy with explicit verification;
  it never enables dual writes or changes the selected backend automatically.
- memtomem is **not** replaced — it stays the default; the protocol normalizes the coupling
  and opens the option.
- Backends are exclusive stores: there is no dual-write, fallback read, or automatic data
  migration when `memory.backend` changes.
- "Don't pre-abstract" holds: P1 resolves real existing debt (private-internal reach), not a
  speculative abstraction; P3 proves the seam with one concrete alternative rather than a
  backend registry or a multi-provider matrix.

---

## 7. Appendix — P1 implementation contract

This section pins the implemented P1 shapes: a new flat
top-level module `memory_backend.py` (gateway/monitor/session/board precedent), a
byte-identical `MemtomemBackend`, and a `SyncmillMemory` whose only change is *where*
calls go — every degrade path, namespace string, and result shape stays exactly as today.

### 7.1 `src/syncmill/memory_backend.py` (new)

```python
"""Backend seam for syncmill's long-term memory.

`MemoryBackend` names the contract `SyncmillMemory` already imposes on
memtomem (design memo §2). Backends raise freely — `SyncmillMemory` owns
degradation (N4); a backend must never retry or self-recover.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, TypedDict

from syncmill.config import MemoryConfig


class MemoryRow(TypedDict, total=False):
    """One search/get result row. A plain dict at runtime, so existing
    `r["id"]` / `r.get("namespace")` call sites in memory.py are untouched."""

    id: str          # chunk id (uuid string)
    content: str
    score: float     # search only; sort key, higher = better
    source: str      # display hint ("unknown" fallback applied by the caller)
    namespace: str   # REQUIRED on get() rows — the isolation guard reads it (N3)


class NamespaceMeta(TypedDict, total=False):
    namespace: str
    chunk_count: int


@dataclass(frozen=True)
class BackendCapabilities:
    """What this backend can do. P1 ships memtomem's profile only; the flags
    exist so P2/P3 call sites can branch without isinstance checks."""

    supports_sessions: bool = True
    supports_tag_filter: bool = True              # prospective (N2) — nothing reads it yet
    supports_namespace_enumeration: bool = True   # C4b — False: board-ledger GC (P2)
    namespace_delete_is_atomic: bool = True       # C5 always works; flag = atomicity only
    verbatim_storage: bool = True                 # N6 — False is REFUSED at build
    namespace_pattern: re.Pattern[str] = field(   # N8 — memtomem grammar as default
        default_factory=lambda: re.compile(r"^[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+)*$")
    )


class MemoryBackend(Protocol):
    """Async, raise-freely storage contract (memo §2). All methods may raise;
    the caller degrades. Optional groups are gated by `capabilities`."""

    @property
    def capabilities(self) -> BackendCapabilities: ...

    # Namespace conventions — syncmill derives its literals from the backend so
    # the memtomem adapter can keep tracking memtomem.constants (today's rule).
    @property
    def agent_namespace_prefix(self) -> str: ...   # "agent-runtime:"
    @property
    def shared_namespace(self) -> str: ...         # "shared"

    def validate_namespace(self, ns: str) -> bool: ...   # N8; pure, never raises

    # ── core (C1–C3, C6, C7, C8) ─────────────────────────────────────────
    async def add(self, content: str, *, tags: list[str], namespace: str) -> None: ...
    async def search(
        self, query: str, *, namespace: str, top_k: int,
        tag_filter: str | None = None,               # comma = OR, exact match (N2)
    ) -> list[MemoryRow]: ...
    async def get(self, chunk_id: str) -> MemoryRow | None: ...
    async def ensure_namespace(self, ns: str, *, description: str = "") -> None: ...
    async def ensure_ready(self) -> None: ...
    # C8a: the bare readiness probe. Raises when the store is unreachable, does
    # NOTHING else (no metadata read). It is the up-front check for
    # `delete_board_runs`' None-vs-0 boundary — faithfully matching today's
    # lone `_ensure_init()` preflight, which reads no config (§7.2).
    async def status(self) -> dict[str, Any]: ...
    # C8b: `ensure_ready()` + connection info. Returns EXACTLY {"db_path": ...}
    # (no backend-name field — SyncmillMemory adds only "connected"), so the
    # wrapped dict stays byte-identical to today's {"connected", "db_path"}.
    async def close(self) -> None: ...

    # ── namespace lifecycle (§4.1) ───────────────────────────────────────
    async def count_namespace(self, ns: str) -> int: ...           # C4a — permanent
    async def list_namespaces(self) -> list[NamespaceMeta]: ...    # C4b — capability-gated
    async def delete_namespace(self, ns: str) -> int: ...
    # C5: always implemented; an adapter without a native atomic op owns its
    # internal enumerate-and-delete loop (namespace_delete_is_atomic=False).

    # ── sessions (S1–S3 — no-op adapter when supports_sessions=False) ───
    async def start_session(self, agent_id: str, namespace: str) -> str | None: ...
    async def log_event(self, kind: str, text: str) -> None: ...
    async def end_session(self, summary: str | None) -> None: ...


class MemtomemBackend:
    """The default backend. Absorbs ALL memtomem coupling — including the
    private-internal reach (`_ensure_init` / `_components.storage.*`) that
    today sits in SyncmillMemory (memo §1) — into this one class, so a
    memtomem upgrade breaks at most this adapter."""

    def __init__(self, config: MemoryConfig):
        # Import here, not module-top: a missing/broken memtomem must surface
        # as a constructor exception SyncmillMemory catches and degrades on —
        # exactly today's init contract.
        from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
        from memtomem.integrations.langgraph import MemtomemStore

        self._agent_prefix = AGENT_NAMESPACE_PREFIX
        self._shared_ns = SHARED_NAMESPACE
        self._store = MemtomemStore(
            config_overrides={"storage": {"sqlite_path": config.db_path}}
        )
        self._caps = BackendCapabilities()  # memtomem: everything supported

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

    # core — thin pass-throughs (signatures pinned by the Protocol)
    async def add(self, content, *, tags, namespace):
        await self._store.add(content=content, tags=tags, namespace=namespace)

    async def search(self, query, *, namespace, top_k, tag_filter=None):
        if tag_filter is not None:
            return await self._store.search(
                query, namespace=namespace, top_k=top_k, tag_filter=tag_filter
            )
        return await self._store.search(query, namespace=namespace, top_k=top_k)

    async def get(self, chunk_id):
        return await self._store.get(chunk_id)

    async def ensure_namespace(self, ns, *, description=""):
        storage = await self._storage()
        if await storage.get_namespace_meta(ns) is None:
            await storage.set_namespace_meta(ns, description=description)

    async def ensure_ready(self):
        # C8a: bare connectivity check — _ensure_init raises when unreachable,
        # reads NO config. This is what delete_board_runs' preflight uses, so
        # its None-vs-0 boundary depends on nothing but real reachability
        # (matching today's lone _ensure_init preflight, memory.py:389-407).
        await self._store._ensure_init()

    async def status(self):
        # C8b: readiness + info. db_path only — SyncmillMemory adds "connected".
        await self.ensure_ready()
        db = self._store._config_overrides.get("storage", {}).get(
            "sqlite_path", "default"
        )
        return {"db_path": db}

    async def close(self):
        await self._store.close()

    # namespace lifecycle — THE private-internal reach, now in one place
    async def _storage(self):
        await self._store._ensure_init()
        return self._store._components.storage

    async def count_namespace(self, ns):
        metas = await (await self._storage()).list_namespace_meta()
        return next(
            (m.get("chunk_count", 0) for m in metas if m.get("namespace") == ns), 0
        )

    async def list_namespaces(self):
        return await (await self._storage()).list_namespace_meta()

    async def delete_namespace(self, ns):
        return await (await self._storage()).delete_by_namespace(ns)

    # sessions — pass-throughs
    async def start_session(self, agent_id, namespace):
        return await self._store.start_session(agent_id=agent_id, namespace=namespace)

    async def log_event(self, kind, text):
        await self._store.log_event(kind, text)

    async def end_session(self, summary):
        await self._store.end_session(summary=summary)


def build_backend(config: MemoryConfig) -> MemoryBackend:
    """Construct the configured backend. P1 knows only "memtomem"; a typo is
    already impossible at this point because `MemoryConfig.backend` is a
    pydantic `Literal` (rejected at `_load_config`, the config-typo chokepoint).
    A backend whose capabilities report `verbatim_storage=False` is refused
    here (N6 is non-negotiable), mirroring `_make_strategy`'s hard-fail style.
    Construction errors propagate — SyncmillMemory catches and degrades."""
    backend = MemtomemBackend(config)          # P3: dispatch on config.backend
    if not backend.capabilities.verbatim_storage:
        raise ValueError(f"backend {config.backend!r} cannot store verbatim content")
    return backend
```

### 7.2 `SyncmillMemory` delta (memory.py — call-site moves only)

```python
class SyncmillMemory:
    def __init__(self, config: MemoryConfig):
        self.degraded = False
        self.last_error: str | None = None
        self._backend: MemoryBackend | None = None
        self._scope: str | None = None
        self._global_shared_ns: str | None = None
        self._agent_prefix = "agent-runtime:"   # fallback: memtomem.constants unimportable
        self._shared_ns = "shared"
        # SINGLE try, matching today's structure (memory.py:66-76): constants
        # are imported+assigned, THEN build_backend runs (it imports the store
        # module and constructs). So a store *construction* failure still
        # leaves the real constants installed (today's memory.py:70-74 order;
        # round-1 finding 5). One documented micro-divergence remains — see below.
        try:
            from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
            from syncmill.memory_backend import build_backend

            self._agent_prefix = AGENT_NAMESPACE_PREFIX
            self._shared_ns = SHARED_NAMESPACE
            self._backend = build_backend(config)
            self._agent_prefix = self._backend.agent_namespace_prefix
            self._shared_ns = self._backend.shared_namespace
        except Exception as exc:
            self._degrade("init", exc)          # unchanged degrade contract

    def scoped(self, run_id: str) -> "SyncmillMemory":
        view = SyncmillMemory.__new__(SyncmillMemory)
        view._backend = self._backend           # ← ONLY change: was `view._store = self._store`
        view.degraded = False                   # ↓ every other line verbatim from today
        view.last_error = None
        view._agent_prefix = self._agent_prefix
        view._shared_ns = f"{BOARD_RUN_PREFIX}{run_id}"
        view._scope = f"{BOARD_RUN_PREFIX}{run_id}"
        view._global_shared_ns = self._shared_ns
        return view
```

**Documented micro-divergence (round-2 finding 3).** In today's single import
block, `memtomem.integrations.langgraph` is imported *before* the constants are
assigned, so a partial install where `memtomem.constants` imports but the
integration module does **not** leaves the hard-coded fallbacks. In the sketch,
that same failure surfaces from `build_backend` *after* the constants were
assigned, so the real constants stay. This is **functionally inert**: it only
changes which namespace *literal* a fully-degraded (`_backend is None`) memory
reports, and per `memory.py:61-63`'s own comment those values "are never used
anyway" when the store is absent. Called out here (not hidden) so the P1 test
that monkeypatches non-default constants asserts the sketch's behavior on this
one path, not today's.

Every former private-internal reach becomes one public backend call — the five sites:

| memory.py method | today (private reach) | P1 |
|---|---|---|
| `register_agent` | `_ensure_init` + `get/set_namespace_meta` | `await self._backend.ensure_namespace(ns, description=...)` |
| `shared_stats` | `_ensure_init` + `list_namespace_meta` | `await self._backend.count_namespace(self._shared_ns)` (C4a) |
| `list_board_run_ids` | `_ensure_init` + `list_namespace_meta` | `await self._backend.list_namespaces()` (C4b) |
| `delete_board_runs` | up-front `_ensure_init` (fail → `None`), then `delete_by_namespace` loop | up-front `await self._backend.ensure_ready()` (C8a — fail → `None`), then `delete_namespace` loop — `ensure_ready` reads no config, so the `None` ("unreachable, couldn't try") vs `0` ("tried, nothing purged") boundary matches today's `_ensure_init`-only preflight exactly |
| `status` | `_ensure_init` + `_config_overrides` | `info = await self._backend.status()` (C8b, returns `{"db_path": ...}`) → `{"connected": True, **info}`; on raise → `{"connected": False, "error": str(e)}` — **byte-identical dict shapes** (`status()` adds no backend-name field; the CLI branches on `connected`, cli.py:899) |

All other methods swap `self._store.X(...)` for `self._backend.X(...)` with identical
arguments and result handling (`search_context`'s merge/sort, `get_shared`'s namespace
guard, and `scoped()`'s D4 union-read semantics are unchanged — only the attribute it
copies is renamed, per the sketch above). **No behavior change is permitted in P1** — the
acceptance bar is the existing hermetic suite passing with **fixture edits plus mechanical
test-assertion edits** (§7.4): memory tests now assert the equivalent `FakeBackend`
recording fields and `_backend is None`. Gateway/supervisor tests use that public fake
surface with the migrated `raise_on` keys.

### 7.3 Config delta (config.py)

```python
from typing import Literal   # new import in config.py (none exists today)


class MemoryConfig(BaseSettings):
    """Long-term memory settings (memtomem by default)."""

    model_config = SettingsConfigDict(extra="ignore")

    backend: Literal["memtomem"] = "memtomem"   # P3 widens: "mem0", ...
    db_path: str = "~/.memtomem/memtomem.db"
```

The `Literal` makes an unknown backend a config-load error surfaced by `_load_config`
(one-line `ClickException`), not a runtime branch — the `_validate_strategy` posture.
Backend-specific tables arrive only in P3 (`[memory.mem0]` etc.), keeping P1's config
surface a single additive field. Env form: `SYNCMILL_MEMORY__BACKEND=memtomem`.

### 7.4 Test delta (hermetic suite)

- `tests/conftest.py`: `FakeStore` became `FakeBackend` — same scripting surface
  (`search_results`, `raise_on`, `ns_counts`, …) but implementing the Protocol's names.
  `make_memory(backend)` assigns `m._backend = backend` and keeps deriving
  `_agent_prefix`/`_shared_ns` from `memtomem.constants` so fixtures can't drift.
- **`raise_on` key migration is not two renames** — today's fake keys on the memtomem
  private surface (`_ensure_init` fans failure into register/stats/enumerate/delete/status;
  `get/set_namespace_meta` also drive registration). The full old→new table (each row is a
  `raise_on` entry consumers already use):

  | today's key | new key(s) | consumers to touch |
  |---|---|---|
  | `_ensure_init` | `ensure_ready` **and/or** the specific op (`ensure_namespace`/`status`/`list_namespaces`/`count_namespace`/`delete_namespace`) | `test_supervisor.py` "dead memory" fixtures (135, 380, 630, 1060, 1591, 2095) — these want a *fully* failing backend, so they set the broad `ensure_ready` key (or all five) |
  | `list_namespace_meta` | `list_namespaces` (C4b) **+** `count_namespace` (C4a) | `test_memory.py` shared_stats/list-board-run tests |
  | `delete_by_namespace` | `delete_namespace` | `test_memory.py` delete_board_runs tests |
  | `get/set_namespace_meta` | `ensure_namespace` | register_agent tests |

  Because a single old `_ensure_init` failure modeled "the whole store is down," `FakeBackend`
  should keep a broad `ensure_ready`/all-methods switch so those fixtures stay one-liners;
  the degrade contract (not the key names) is the invariant under test.
- One new test file `tests/test_memory_backend.py`: capability defaults,
  `validate_namespace` accept/reject table (N8), `build_backend` verbatim refusal, and a
  Protocol-conformance check (`isinstance` via `runtime_checkable` or a structural
  smoke call-through against `FakeBackend`).
- New byte-identity pins (each anchors a codex review finding):
  - `scoped()` view shares the parent's backend (`view._backend is parent._backend`) with
    independent degradation state and working D4 union-read.
  - Constructor-failure literals: memtomem imports but store construction raises →
    `_agent_prefix`/`_shared_ns` still reflect (monkeypatched, non-default)
    `memtomem.constants`; memtomem unimportable → the hard-coded fallbacks.
  - `status()` dict shapes: healthy → `{"connected": True, "db_path": ...}` **exactly** (no
    `backend` field); backend raise → `{"connected": False, "error": ...}` (CLI's `connected`
    branch).
  - `delete_board_runs` `None`-vs-`0`: unreachable backend (up-front `ensure_ready()` raises)
    → `None`; reachable but every `delete_namespace` fails → degraded, count of successes.
  - `count_namespace` on a missing namespace → `0` (today's `shared_stats` default).
- Structural guarantee to pin in review: `grep -r "_components\|_ensure_init\|_config_overrides" src/syncmill/`
  returns hits **only** in `memory_backend.py` after P1.

### Sources (fetched 2026-07-12, primary only)

- mem0: `github.com/mem0ai/mem0/blob/main/mem0/memory/main.py`, `docs.mem0.ai` (add /
  operations / configuration), `.../features/async-memory.mdx`.
- LangGraph store: `github.com/langchain-ai/langgraph` (`libs/checkpoint/langgraph/store/
  base`, `checkpoint-sqlite`, `checkpoint-postgres`), `docs.langchain.com/oss/python/
  langgraph/memory`; langmem: `langchain-ai.github.io/langmem`.
- Letta: `docs.letta.com` (archives / passages / selfhosting), `pypi.org/project/letta-client`.
- Graphiti: `github.com/getzep/graphiti` `main@526dcad` (`graphiti_core/{graphiti,nodes}.py`,
  `search/*`, `utils/maintenance/graph_data_operations.py`), `help.getzep.com/graphiti`.
