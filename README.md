# syncmill

Multi-agent orchestration with shared memory — grind tasks through competing and
collaborating AI coding agents (Claude Code, Codex CLI, Kimi Code).

syncmill hands a task to CLI coding agents, gives them a pluggable shared
long-term memory ([memtomem](https://pypi.org/project/memtomem/) by default,
optional mem0 or LangGraph SQLite) for context continuity, and
captures the complete Git state (including dirty tracked and untracked files) in
a private control clone, then isolates each agent in its own worktree. A central **Supervisor** is the only
mediator: agents are pure functions (prompt in → result out) and know nothing
about the selected backend, each other, or the orchestrator.

> **Status:** All five orchestration strategies have shipped: **route**
> (priority fallback), **pipeline** (sequential chain, each agent building on the
> previous), **compete** (concurrent head-to-head — every agent runs at once in its
> own worktree and the selection policy keeps a winner), **council** (multi-round propose →
> critique → synthesize deliberation through shared memory), and **decompose**
> (split the task into subtasks, run them across agents concurrently, then
> synthesize the results into one outcome). The architecture is built to grow
> more. See [CLAUDE.md](CLAUDE.md) for the design invariants.

> **New to syncmill?** Start with the **[User Guide](docs/user-guide.md)** — a
> friendly, task-oriented walkthrough (install → first run → strategies →
> configuration → troubleshooting). 한국어 안내서:
> **[처음 시작하는 분들을 위한 가이드](docs/user-guide.ko.md)**. TUI의 7개 탭과
> 11개 화면은 **[한국어 TUI 화면 안내서](docs/tui-guide.ko.md)**에서 볼 수 있습니다.
> 실제 프로젝트 대신 버려도 되는 저장소에서 검증하려면
> **[`/tmp` 안전 테스트 가이드](docs/tmp-test-guide.ko.md)**를 사용하세요.

## Install

syncmill uses [uv](https://docs.astral.sh/uv/) and Python 3.12+. The supported
beginner path is a source checkout:

```bash
git clone https://github.com/memtomem/syncmill.git
cd syncmill
uv sync
export SYNCMILL_SRC="$PWD"
```

Run commands from the **target Git repository**, while `--project` points back
to this checkout:

```bash
cd /path/to/my-project
uv run --project "$SYNCMILL_SRC" syncmill init
uv run --project "$SYNCMILL_SRC" syncmill doctor
uv run --project "$SYNCMILL_SRC" syncmill run --dry-run "Add type hints to main.py"
```

The default memory backend is memtomem. Optional alternatives are installed
separately:

```bash
uv sync --project "$SYNCMILL_SRC" --extra mem0
uv sync --project "$SYNCMILL_SRC" --extra langgraph
uv sync --project "$SYNCMILL_SRC" --extra phoenix
```

### Prerequisites

- **Agent CLIs** — install and authenticate whichever you want to use; they are
  invoked as subprocesses:
  - `claude` ([Claude Code](https://claude.com/claude-code))
  - `codex` (Codex CLI)
  - `kimi` ([Kimi Code](https://moonshotai.github.io/kimi-code/) — agent id `kimi-code`)
  A missing or unauthenticated agent simply fails and the next one in priority
  order runs (see [fallback](#how-it-works)).
- **memtomem embedding backend** (optional but recommended) — memtomem needs a
  working embedding backend for shared memory. syncmill installs
  `memtomem[onnx,korean]`, so the local **ONNX** provider (fastembed, e.g.
  `bge-m3`) and the Korean tokenizer work out of the box; a local **ollama**
  (`ollama pull nomic-embed-text`) or an **OpenAI** key work too. The active
  provider is governed by `~/.memtomem/config.json`.
  If memory is unavailable or misconfigured, syncmill **degrades gracefully** —
  it logs a warning and runs the agents anyway, just without shared context.
  Projects created by `syncmill init` use `.syncmill/memory/`; legacy uninitialized
  use retains the global `~/.memtomem/memtomem.db` default. Explicit deployments
  can set both `memory.db_path` and `memory.memory_dir`.
- **mem0** (optional alternative) — select `backend = "mem0"` after installing
  the `mem0` extra. It stores verbatim text with `infer=False`, defaults to local
  Qdrant plus multilingual FastEmbed under `~/.syncmill/mem0`, and makes no LLM
  calls. First use downloads the embedding model. Anonymous mem0 telemetry is
  disabled by default. Embedded Qdrant holds an exclusive lock on its configured
  path, so only one process can use the default local store at a time. For
  concurrent runs, `serve`, or `status`, configure a Qdrant server through
  `memory.mem0.config.vector_store.config.url` as shown in
  [`syncmill.toml.example`](syncmill.toml.example); Mem0 history remains local
  under `data_dir`. Mem0 determines telemetry on the process's first `mem0`
  import, so set it before launch and restart the process to change it.
- **LangGraph BaseStore** (optional alternative) — install the `langgraph` extra
  and select `backend = "langgraph"`. It uses persistent
  `AsyncSqliteStore` plus local multilingual FastEmbed at
  `~/.syncmill/langgraph.db`, stores text verbatim, and makes no network or LLM
  calls after the embedding model is available. Unlike embedded Qdrant, SQLite
  does not impose Mem0's single-client directory lock.

## Usage

```bash
# Run these examples from the target repository. --project selects the
# Syncmill source checkout without changing the target repository.

# One-time project setup and readiness check
uv run --project "$SYNCMILL_SRC" syncmill init
uv run --project "$SYNCMILL_SRC" syncmill doctor              # one ready provider is enough
uv run --project "$SYNCMILL_SRC" syncmill doctor --strict     # optional: require every enabled provider

# Inspect the exact bounded plan without starting an agent
uv run --project "$SYNCMILL_SRC" syncmill run --dry-run "Add type hints to main.py"

# Run a task through the orchestration pipeline (priority fallback)
uv run --project "$SYNCMILL_SRC" syncmill run "Add type hints to main.py"

# Force a single agent (no fallback)
uv run --project "$SYNCMILL_SRC" syncmill run "Refactor the auth module" --agent claude

# Every winner is retained. Review and explicitly apply it by run id.
uv run --project "$SYNCMILL_SRC" syncmill runs list
uv run --project "$SYNCMILL_SRC" syncmill diff <run-id>
uv run --project "$SYNCMILL_SRC" syncmill apply <run-id>
uv run --project "$SYNCMILL_SRC" syncmill apply <run-id> --detach   # durable detached apply worker (survives this process)
uv run --project "$SYNCMILL_SRC" syncmill recover [<run-id>]        # reconcile a standalone apply interrupted by a crash

# Continue the winning Claude/Codex/Kimi provider session in a later invocation.
# A pending parent patch is restored in isolation and the child patch is cumulative.
uv run --project "$SYNCMILL_SRC" syncmill continue <run-id> "Now add regression tests"

# Optional compatibility export (the durable artifact is still written)
uv run --project "$SYNCMILL_SRC" syncmill run "Add type hints to main.py" --emit-patch winner.patch

# Chain agents as a pipeline — each stage builds on the previous via shared memory
uv run --project "$SYNCMILL_SRC" syncmill run "Implement the parser, then add tests" --strategy pipeline

# Convene a council — agents propose, critique over rounds, then synthesize a winner
uv run --project "$SYNCMILL_SRC" syncmill run "Design the caching layer" --strategy council

# Decompose — split into subtasks, run them across agents concurrently, then synthesize
uv run --project "$SYNCMILL_SRC" syncmill run "Add a config loader with validation and tests" --strategy decompose

# Watch a concurrent run live in tmux — one pane per agent, auto-attached
uv run --project "$SYNCMILL_SRC" syncmill run "Design the caching layer" --strategy compete --monitor

# Drive one agent interactively yourself; syncmill runs the rest (repeatable)
uv run --project "$SYNCMILL_SRC" syncmill run "Refactor the auth module" --exclude claude

# Drive that agent in a DURABLE tmux session — survives detach, reattach by name
uv run --project "$SYNCMILL_SRC" syncmill session start claude      # attach; Ctrl-b d detaches, agent keeps running
uv run --project "$SYNCMILL_SRC" syncmill session list              # show active sessions
uv run --project "$SYNCMILL_SRC" syncmill session attach claude     # reconnect later
uv run --project "$SYNCMILL_SRC" syncmill session stop claude       # (or --all)

# Safer project-scoped interactive session in a persistent isolated worktree.
# Checkpoint exports a normal reviewable run artifact; close removes the worktree.
uv run --project "$SYNCMILL_SRC" syncmill session open codex --workspace isolated
uv run --project "$SYNCMILL_SRC" syncmill session isolated-list
uv run --project "$SYNCMILL_SRC" syncmill session checkpoint <session-id>
uv run --project "$SYNCMILL_SRC" syncmill session close <session-id>

# Compare & Apply room: run several unique providers in isolated worktrees,
# retain every attempt, select one candidate, then apply it explicitly.
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room create "Refactor auth" -a claude -a codex
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room round <room-id> --detach
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room candidates <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room diff <candidate-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room select <room-id> <candidate-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room apply <room-id> --verify
# After a worker crash and lease expiry, or an interrupted apply:
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room recover <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room prune --dry-run

# Point at another repo, override the per-agent timeout (seconds)
uv run --project "$SYNCMILL_SRC" syncmill run "Fix the failing test" --repo /path/to/repo --timeout 600

# Inspect registered agents (priority order) and their settings
uv run --project "$SYNCMILL_SRC" syncmill agents

# Check the selected memory backend
uv run --project "$SYNCMILL_SRC" syncmill status

# Serve shared memory (and the work-board) read-only to external MCP clients (stdio)
uv run --project "$SYNCMILL_SRC" syncmill serve

# Run with a named team profile ([teams.<name>] in syncmill.toml)
uv run --project "$SYNCMILL_SRC" syncmill run "Fix the API" --team python-team

# Persistent work-board (disabled by default): plan items, drain them, review, done
SYNCMILL_BOARD__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill board add "Design the API schema" --goal backend
SYNCMILL_BOARD__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill board import-review-candidates candidates.json
SYNCMILL_BOARD__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill board run --loop --goal backend
SYNCMILL_BOARD__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill board update <id> --state done --sign-off

# Live TUI over the work-board (needs a TTY)
SYNCMILL_BOARD__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill dashboard

# Full seven-tab Board/Runs/Reports/Teams/Status/Sessions/Rooms command center.
# It still opens in limited mode when the board is disabled.
# Browsing/editing is cross-platform; detached x/g execution requires POSIX.
uv run --project "$SYNCMILL_SRC" syncmill tui
```

Detached `apply --detach` and `room round --detach` workers require POSIX process
and file-locking primitives. Use their foreground forms on non-POSIX systems.

For a step-by-step first run with real agent CLIs (including billing/quota,
worktree isolation, and why `Files changed` is not automatically applied to your
checkout), see [Running real agents safely](docs/user-guide.md#running-real-agents-safely)
or the Korean guide's [실제 에이전트로 안전하게 실행하기](docs/user-guide.ko.md#실제-에이전트로-안전하게-실행하기).

## Read-only MCP gateway (`syncmill serve`)

syncmill deliberately keeps its **agents** off MCP — the Supervisor is the sole reader/writer
of memory. `syncmill serve` adds a **one-way (read-only) MCP gateway** so external/interactive
MCP clients (Claude Code/Desktop, Cursor, …) can *read* syncmill's accumulated `shared`
knowledge — and, when the work-board is enabled, its board state — without breaking that
isolation. It is a separate, non-agent consumer: agents still never touch MCP, the Supervisor
stays the sole writer, and the gateway exposes **only** the `shared` namespace — never any
agent's private scope.

Six read-only tools (no write/delete/index — for memory or the board):

| Tool | Purpose |
|------|---------|
| `shared_search(query, top_k)` | Ranked excerpts from `shared` (each result carries its chunk id). |
| `shared_read(chunk_id)` | Full content of one `shared` chunk by id (refuses non-shared ids). |
| `shared_stats()` | Number of chunks in `shared`. |
| `board_list(state, goal, limit)` | Work items, most recent first (filterable). |
| `board_get(item_id)` | One work item in full — state, evidence, audit history. |
| `board_stats()` | Team-health stats (duplicate work, stalls, false-done). |

The `board_*` tools are always registered; with the board disabled they answer
`the work-board is not enabled on this server` while the shared tools keep working.

```bash
# stdio (default) — register with an MCP client, e.g. Claude Code:
claude mcp add syncmill-shared -- uv run --project "$SYNCMILL_SRC" syncmill serve

# HTTP (streamable-http) for remote consumers — loopback by default:
uv run --project "$SYNCMILL_SRC" syncmill serve --http --port 8765        # http://127.0.0.1:8765/mcp
```

**Security:** HTTP serving exposes read-only shared memory with **no authentication**. The bind
host defaults to `127.0.0.1` (loopback); a non-loopback `--host` is warned about at startup —
only use it on a trusted network or behind an authenticated proxy.

## Configuration

Config is resolved from three layers, **highest priority first**:

1. `SYNCMILL_*` environment variables.
2. A project `syncmill.toml` in the selected target Git root.
3. Built-in defaults.

So **env always overrides the file, and the file overrides defaults** — every
field stays env-overridable. The CLI flags `--repo` / `--strategy` / `--timeout`
sit above all three (an explicit per-invocation override).
Commands that target an explicit `--repo <target>` load that project's
`<target>/syncmill.toml`; commands without an explicit target resolve config from
the invocation working directory. Invalid target configuration or project-state
paths are reported instead of falling back to caller config. The one exception is
the legacy `session start --repo`, which takes the path only as the interactive
agent's working directory: it resolves its config (including `agent_priority`)
from the invocation directory. Use `session open --workspace isolated` for a
project-scoped session that reads the target's configuration.

### Environment variables

Every field is overridable via `SYNCMILL_*` environment variables (nested fields
use a `__` delimiter). These are the most common variables;
[`syncmill.toml.example`](syncmill.toml.example) is a broader copyable reference
covering the common tables, and command `--help` documents per-invocation flags.
Neither is exhaustive — the field definitions in `src/syncmill/config.py` are.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SYNCMILL_AGENT_PRIORITY` | `["claude","codex","kimi-code"]` | Agent order (JSON list) — fallback/stage/planner order and the default concurrent tie-break |
| `SYNCMILL_DEFAULT_STRATEGY` | `route` | Orchestration strategy (`route`, `pipeline`, `compete`, `council`, or `decompose`; or pass `--strategy`) |
| `SYNCMILL_COUNCIL__ROUNDS` | `1` | Council critique rounds (≥1) between propose and synthesize |
| `SYNCMILL_DECOMPOSE__MAX_SUBTASKS` | `8` | Max subtasks the decompose planner may produce (≥1) |
| `SYNCMILL_MONITOR__ENABLED` | `false` | Live tmux monitor for concurrent runs (or pass `--monitor`) |
| `SYNCMILL_REPO_ROOT` | `.` | Default target repo |
| `SYNCMILL_WORKSPACE__ROOT` | `~/.syncmill/workspaces` | Private snapshot/worktree root |
| `SYNCMILL_RUNS__ROOT` | `~/.syncmill/projects` | Durable project run store root |
| `SYNCMILL_CONVERSATIONS__ENABLED` | `true` | Capture and resume provider-native sessions |
| `SYNCMILL_SELECTION__MODE` | `verified` | Concurrent winner policy: `verified`, `priority`, or `judge` |
| `SYNCMILL_MEMORY__BACKEND` | `memtomem` | Long-term memory backend: `memtomem`, `mem0`, or `langgraph` |
| `SYNCMILL_MEMORY__DB_PATH` | `~/.memtomem/memtomem.db` | memtomem SQLite path |
| `SYNCMILL_MEMORY__MEMORY_DIR` | unset (memtomem default) | Explicit memtomem Markdown directory; set with `DB_PATH` for isolation |
| `SYNCMILL_MEMORY__MEM0__DATA_DIR` | `~/.syncmill/mem0` | Local mem0 history/embedded-Qdrant directory |
| `SYNCMILL_MEMORY__MEM0__TELEMETRY` | `false` | Mem0 telemetry value used on the process's first `mem0` import |
| `SYNCMILL_MEMORY__LANGGRAPH__DB_PATH` | `~/.syncmill/langgraph.db` | Persistent LangGraph SQLite store |
| `SYNCMILL_INTEGRATION__TRACE__ENABLED` | `false` | Write the body-free OTLP-JSON trace artifact |
| `SYNCMILL_INTEGRATION__TRACE__EXPORTER` | `file` | `file`, or `otlp-http` to also stream to Phoenix (requires the `phoenix` extra) |
| `SYNCMILL_INTEGRATION__TRACE__ENDPOINT` | unset | Phoenix OTLP/HTTP endpoint; config overrides `PHOENIX_COLLECTOR_ENDPOINT`, then defaults to `http://127.0.0.1:6006/v1/traces` |
| `SYNCMILL_INTEGRATION__TRACE__PROJECT_NAME` | unset | Phoenix project; config overrides `PHOENIX_PROJECT_NAME`, then defaults to `syncmill` |
| `SYNCMILL_MODEL_ROUTING__MODE` | `off` | Difficulty-based model routing: `off` or `observe` (experimental) |
| `SYNCMILL_MODEL_ROUTING__MODELS` | `{}` | Tier→provider→selector map (JSON), e.g. `{"low":{"codex":"<selector>"}}` |
| `SYNCMILL_CLAUDE__ENABLED` | `true` | Enable/disable an agent |
| `SYNCMILL_CLAUDE__TIMEOUT_SECONDS` | `300` | Per-agent timeout |
| `SYNCMILL_CLAUDE__MAX_TURNS` | `10` | Per-agent max turns |

(`CLAUDE`, `CODEX`, `KIMI_CODE` sub-configs all accept `ENABLED`,
`TIMEOUT_SECONDS`, `MAX_TURNS`. The hyphenated agent id `kimi-code` maps to the
`KIMI_CODE` env prefix, e.g. `SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS`; `MAX_TURNS`
is only honored by `claude`.)

### `syncmill.toml`

Drop a `syncmill.toml` in the target Git root to set a checked-in project
baseline. Discovery is not recursive: without an explicit `--repo`, the file is
read from the current working directory, so run syncmill from the Git root (or
pass `--repo <root>`) for it to take effect.
Copy [`syncmill.toml.example`](syncmill.toml.example) to get
started. Every field has the same name as its config field, and `SYNCMILL_*` env
vars still override it:

```toml
agent_priority = ["claude", "codex", "kimi-code"]
default_strategy = "compete"
repo_root = "."

[memory]
backend = "memtomem"
db_path = "~/.memtomem/memtomem.db"
# memory_dir = "~/.syncmill/memtomem-memories"  # set with a private db_path

# Optional alternative (requires `uv sync --project "$SYNCMILL_SRC" --extra mem0`):
# backend = "mem0"
# [memory.mem0]
# data_dir = "~/.syncmill/mem0"
# telemetry = false

# Optional local BaseStore (requires `uv sync --project "$SYNCMILL_SRC" --extra langgraph`):
# backend = "langgraph"
# [memory.langgraph]
# db_path = "~/.syncmill/langgraph.db"

[claude]
timeout_seconds = 600
max_turns = 12

# Per-agent tables use the FIELD name — so kimi-code is the underscored
# [kimi_code], matching the SYNCMILL_KIMI_CODE__ env prefix.
[kimi_code]
timeout_seconds = 300

# Quality gates (#10): each entry is one command, all must exit 0.
[gate]
commands = ["pytest -q", "ruff check ."]

# Circuit breaker (#8): off by default.
[breaker]
enabled = true
failure_threshold = 3

# Capability matching (#9): keyed by the REAL agent id, so kimi-code keeps
# its hyphen here (unlike the [kimi_code] table above).
[capabilities.agents]
codex = ["python", "refactor"]
kimi-code = ["rust"]
```

Notes:
- **Env wins:** `SYNCMILL_DEFAULT_STRATEGY=route` overrides `default_strategy`
  above. Overriding one nested field (e.g. `SYNCMILL_CLAUDE__TIMEOUT_SECONDS`)
  leaves the rest of that table from the file intact (sources deep-merge).
- **Discovery:** `run --repo <target>` and `doctor --repo <target>` load
  `<target>/syncmill.toml` — running from repository A against repository B
  uses B's file, never A's (a target without a file resolves env + defaults at
  the target). Every other command reads `syncmill.toml` from the directory
  you invoke syncmill in. Missing file → just defaults + env.
- **Typo protection:** an unknown *top-level* key in the file is rejected with a
  clear error rather than silently ignored.

### Migrating memory backends

Migration is an explicit, source-retaining maintenance workflow. Stop active
`run`/`serve` processes, plan against empty destination namespaces, then apply
and verify the immutable snapshot:

```bash
uv run --project "$SYNCMILL_SRC" syncmill memory migration plan --from memtomem --to mem0
# Any installed pair is supported, including LangGraph:
uv run --project "$SYNCMILL_SRC" syncmill memory migration plan --from mem0 --to langgraph
uv run --project "$SYNCMILL_SRC" syncmill memory migration apply <migration-id>
uv run --project "$SYNCMILL_SRC" syncmill memory migration verify <migration-id>
# Deletes only unchanged rows created by that migration:
uv run --project "$SYNCMILL_SRC" syncmill memory migration rollback <migration-id> --yes
```

The default scope is `shared` plus `agent-runtime:*`; `board-run:*` is always
excluded. Artifacts live under `~/.syncmill/migrations/`; `snapshot.json` contains
the selected memories verbatim in plaintext and is never auto-purged. Protect the
directory and remove an artifact manually only after its receipt is no longer
needed. Migration never deletes the source, changes `memory.backend`, or
dual-writes, so cutover remains an operator decision after verification. The
plan also binds the artifact to the resolved destination path or Qdrant
endpoint/collection. Run apply, verify, and rollback with that same destination
configuration; a mismatch fails before the store or migration ledger is changed.
Legacy schema-v1 artifacts have no such binding and must be replanned before use.
If a v1 artifact was already applied, retain it and use its `state.json` target
IDs with the original destination settings for manual recovery; the v2 CLI will
not guess a store and delete automatically. Mem0 binding includes its history DB
because rollback writes a DELETE event there after removing the Qdrant row.

## How it works

```
User ─▶ Supervisor ─┬─▶ (read) memory backend  → inject context into the prompt
                    ├─▶ snapshot HEAD + dirty + untracked state in a private clone
                    ├─▶ create per-agent worktrees from that immutable snapshot
                    ├─▶ Strategy ─▶ Agent subprocess (prompt in → stdout out)
                    ├─▶ atomically retain manifest + winner.patch
                    ├─▶ (write) memory backend ← share the result
                    └─▶ remove worktree
```

- **Supervisor** is the sole mediator and the only reader/writer of the selected
  memory backend. Backends are exclusive; switching does not migrate or dual-write
  data automatically—the explicit maintenance workflow above performs a copy.
- **Default memtomem sharing** retains the producer's result in
  `agent-runtime:<id>`, copies it to `shared` with `shared-from=<chunk-id>`
  provenance, and searches the private/shared namespaces as one merged-ranking
  pool. Pipeline stages, council proposals/critiques, decompose plans/subtasks,
  and final results carry `pipeline-stage`, `council-proposal`,
  `council-critique`, `decompose-plan`, `decompose-subtask`, and `task-result`
  tags for exact filtered retrieval.
  mem0 and LangGraph preserve the visible multi-namespace search/share behavior
  through adapter fallbacks, without memtomem's structural provenance link.
- **Agents** are pure functions: `execute(prompt, workdir, timeout) → TaskResult`.
  Each wraps a real CLI in JSON mode and parses its output.
- **RouteStrategy** (`route`, default) tries agents in priority order and
  returns the first success; if all fail it returns an aggregated error.
- **PipelineStrategy** (`pipeline`) runs the agents as sequential stages: each
  stage enriches its prompt with the most relevant prior results from `shared`
  memory (best-effort) and edits the same worktree, so the chain builds on
  itself. It is fail-fast — a stage failure stops the pipeline and reports which
  stage broke.
- **CompeteStrategy** (`compete`) runs every agent **concurrently**, each in its
  own isolated worktree, from the same enriched prompt, then converges under the
  configured selection policy. The default `verified` policy keeps the first
  gate-passing completed candidate in priority order (deterministic, never finish
  time); `judge` may choose a lower-priority verified candidate.
  All agents actually run (unlike `route`'s short-circuit); if none succeed it
  returns an aggregated error. The winner's diff is surfaced (`files_changed`)
  and shared. Worktrees are normally cleaned up; interrupted or recovery-marked
  runs can retain a workspace for later inspection.
- **CouncilStrategy** (`council`) runs a multi-round deliberation through shared
  memory: agents **propose** a solution concurrently (one worktree each), then
  **critique** over `SYNCMILL_COUNCIL__ROUNDS` rounds — each reading the council's
  proposals and prior critiques from `shared` and refining its own worktree — then
  every member **synthesizes** a final solution and the winner is picked under the
  same selection policy as `compete`. Deliberation
  flows only through `shared` (agents never see each other's worktrees), so the
  synthesized winner reflects more than one agent's input when shared memory
  surfaces peers (best-effort and relevance-ranked, like the pipeline handoff; a
  degraded memtomem never aborts the council — each member just falls back to its
  own accumulated worktree).
- **DecomposeStrategy** (`decompose`) splits a compound task into subtasks and runs
  them **across** agents: the highest-priority agent **plans** the breakdown (a JSON
  subtask list; an unparseable/empty plan degrades to running the whole task as one
  subtask), the subtasks are distributed round-robin over the agents and run
  **concurrently** in isolated worktrees (each *completed* subtask publishes its
  result to `shared`; a failed subtask is tolerated and skipped, and synthesis
  proceeds as long as at least one completed), then every agent **synthesizes** the
  subtask results into one solution and the winner is picked under the same
  selection policy as `compete`. Integration is via shared knowledge,
  not a file merge. Capped at `SYNCMILL_DECOMPOSE__MAX_SUBTASKS` subtasks.
- **Memory is advisory** — best-effort and non-fatal. A broken or missing
  memtomem never aborts a task.
- **Workspaces** live under `~/.syncmill/workspaces/<run-id>/`. syncmill refuses
  non-Git repositories, unborn repositories, unresolved operations, dirty
  submodules, and sparse checkouts instead of silently running in an empty temp
  directory. Source branches, the source index, and the source object database
  are not changed while the snapshot is made. A killed run's marked workspace is
  reclaimed by the next normal run once its owner process is gone.
  Recovery-marked workspaces are preserved; PID reuse can delay dead-owner
  cleanup until the one-week safety backstop.
- **Run artifacts** live under
  `~/.syncmill/projects/<project-id>/runs/<run-id>/`. A successful edit remains
  `pending` until `syncmill apply`; application verifies the project, patch
  digest, unchanged source fingerprint, and `git apply --check`, then writes a
  receipt. Pending artifacts are never auto-pruned.
- **Integration report/trace retention** only counts and removes run directories
  SyncMill can prove it owns. Unrelated directories in a custom `runs_dir` remain
  untouched and do not count toward `keep_last`, so the physical directory total
  may be higher than that setting.
- **Interrupted-apply recovery.** `syncmill apply` records `applying` (with an
  owner and apply id) before touching the source and binds the receipt to that
  apply id, so a crash mid-apply is decidable. `syncmill recover [<run-id>]`
  reconciles any stranded standalone apply — finishing a completed one,
  restoring an untouched source to `pending`, or freezing an undecidable one to
  `recovery_required` for inspection — classified from the receipt, patch
  digest, and source fingerprint. A live apply (still holding the repository
  apply lock) is never touched, and room applies are left to `room recover`.
  The `apply` path runs the same reconcile for its own run before retrying, and
  the TUI Runs tab exposes it on `R`.

## Provider-native conversations (`syncmill continue`)

Programmatic runs persist the winner's real provider session id. Claude uses
`--session-id`/`--resume` (and `--fork-session` for `continue --fork`), Codex uses
`codex exec resume`, and Kimi uses `--session`. Council members retain separate
sessions by agent and worktree across propose/critique/synthesis phases; isolated
candidates never share a session.

```bash
uv run --project "$SYNCMILL_SRC" syncmill conversations list
uv run --project "$SYNCMILL_SRC" syncmill conversations show <run-id>
uv run --project "$SYNCMILL_SRC" syncmill continue <run-id> "Address the remaining review finding"
uv run --project "$SYNCMILL_SRC" syncmill conversations close <run-id>
```

This is distinct from `syncmill session`, which is a raw interactive CLI kept
alive by tmux and edits the user's current working tree directly.

## Difficulty-based model routing (experimental)

Route a run to a cheaper or stronger **provider-internal model** by task
difficulty — without changing which agents run, their order, or any strategy
semantics. You declare the tier→model mapping (syncmill ships no model names);
the selector is passed as the provider CLI's model flag (`claude --model`,
`codex -m`, `kimi -m`).

```toml
[model_routing]
mode = "observe"                     # off (default) | observe

[model_routing.models.low]
claude = "<your-cheap-claude-selector>"
codex = "<your-cheap-codex-selector>"

[model_routing.models.high]
claude = "<your-strong-claude-selector>"
```

- `syncmill run --difficulty low|medium|high "<prompt>"` **applies** the
  configured selector for that tier immediately (even with `mode = "off"`).
  Unmapped agents fall back to their provider CLI default; if *no* eligible
  agent has a mapping the run fails up front rather than silently no-oping.
- `mode = "observe"` (or `--difficulty auto`) classifies each run with a
  deterministic prompt heuristic (`prompt-complexity-v1`) and **records** the
  recommendation in the run plan/manifest without passing any model flag — use
  it to calibrate before trusting automatic routing. Routing covers direct
  `syncmill run` (incl. `--monitor`) and `syncmill continue` only; board runs,
  TUI jobs, and rooms deliberately don't route yet.
- `syncmill continue` keeps **selector affinity**: it resumes with the parent
  run's stored selector (or no flag, for observe/legacy parents) even if your
  mapping has changed since. This is affinity to the selector *string* — a
  provider may re-resolve an alias to a newer model.
- Rollback is config-only (`mode = "off"` / remove the section), but note the
  two paths that deliberately survive it: an explicit `--difficulty` flag, and
  continuations of runs that stored a selector.

Automatic application (`enforce`) is reserved until the observe data proves the
classifier out — see the [difficulty routing contract](docs/difficulty-model-routing-plan.md)
and promotion gates.

## Live monitoring (`--monitor`)

`--monitor` gives a **live tmux view** of a concurrent run — one pane per agent,
streaming each agent's output as it happens, auto-attached:

```bash
uv run --project "$SYNCMILL_SRC" syncmill run "Design the caching layer" -s compete --monitor
```

It only applies to the concurrent strategies (`compete`, `council`, `decompose`);
for a sequential strategy, a non-TTY stdout, or a missing `tmux` it prints a notice
and runs normally (the monitor is a convenience, never a requirement; nothing is
installed beyond `tmux`).

How it works without breaking agent isolation: the agent contract is unchanged
(agents stay pure `(prompt, workdir, timeout)` functions). Output reaches tmux
through an ambient streaming seam the concurrent strategies open around each
fan-out, teeing each agent's output to a per-agent log. Because the orchestration
is deliberately sync (and `tmux attach` needs the foreground), the outer
`syncmill run` builds the tmux session and re-runs the *real* orchestration in a
control pane (the inner process), then attaches; the inner adds a live `tail` pane
per agent it actually runs and writes the result, which the outer prints on detach.
Enable persistently with `SYNCMILL_MONITOR__ENABLED=true`. Detaching (`Ctrl-b d`)
before the run finishes leaves it running in tmux — re-attach with the printed
`tmux attach -t …`.

## Durable interactive sessions (`syncmill session`)

The complement to `--monitor`: where the monitor gives an *ephemeral* view of a
*programmatic* run, `session` runs an agent's **interactive** CLI (the `claude` /
`codex` / `kimi` REPL you type into) in a **dedicated tmux session that survives
detach and reattaches by name** — so a long interactive collaboration outlives a
closed terminal or dropped SSH.

```bash
uv run --project "$SYNCMILL_SRC" syncmill session start [AGENT]   # launch (or reattach), then attach; default AGENT = first in priority
uv run --project "$SYNCMILL_SRC" syncmill session list            # active sessions + attached/detached state
uv run --project "$SYNCMILL_SRC" syncmill session attach TARGET   # reconnect (TARGET = agent id or full session name)
uv run --project "$SYNCMILL_SRC" syncmill session stop [TARGET] [--all]
```

This is the durable form of the interactive-collaboration mode: drive one agent
yourself (interactive use may be billed differently from programmatic runs) while
`syncmill run --exclude <agent>` orchestrates the rest, the two meeting in the
`shared` memory namespace (`session start` prints the exact `mem_add` /
`mem_agent_search` contract). Detach with `Ctrl-b d` — the agent keeps running in
tmux; `session attach <agent>` reconnects to the *same* live session.

Notes: the interactive session is **not** a syncmill agent — it runs the bare
binary and never enters the orchestration pipeline, so its edits land in your real
working tree (collaboration is via shared *knowledge*, not file merges). Every
subcommand needs `tmux` (it *is* the tmux feature — no fallback, unlike
`--monitor`); `start`/`attach` also need an interactive terminal. The session runs
with your current shell's environment (PATH/auth/`SYNCMILL_*`), restored via a
`0600` snapshot the launcher writes — never via the command line — so a stale tmux
server can't strip your auth. You can drive an agent that is *disabled* for
orchestration (`SYNCMILL_CLAUDE__ENABLED=false`); the session lineup is independent
of the enabled registry.

For code-changing work, the newer `session open --workspace isolated` path is the
safer default. It creates a project-scoped persistent worktree, validates the pane's
working directory before attaching, and never edits the source checkout directly.
`session checkpoint` converts its current patch into the same immutable run artifact
used by `runs list`, `diff`, and `apply`; `session close` removes the isolated
workspace. The original `start/list/attach/stop` commands remain available as the
explicit direct-worktree mode.

## Compare & Apply rooms (`syncmill room`)

Rooms are an opt-in multi-CLI review loop. A round runs up to `max_parallel`
different enabled providers in persistent isolated worktrees, waits at a barrier,
and stores every success, failure, timeout, partial patch, and bounded output. A
candidate is eligible only after the agent succeeds and its configured quality gate
passes. Selection and application are separate operations; apply also rejects source
drift and digest tampering before reusing the normal run-artifact apply path.

```bash
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room create "Refactor auth" -a claude -a codex
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room round <room-id> --detach
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room candidates <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room diff <candidate-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room select <room-id> <candidate-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room apply <room-id> --verify
```

Later rounds default to `--lineage continue-own`, preserving each provider's own
worktree and native session. Use `--lineage fresh` for clean baselines or
`--lineage branch-from --base-candidate <id>` to seed all members from one eligible
candidate. Phase 1 deliberately requires unique providers and supports at most three
members; it does not yet multiplex several independent instances of the same CLI.
If a detached worker dies, `room recover <room-id>` moves its expired lease to
`recovery_required` after confirming that the worker is gone. The same command
reconciles an `applying` room after its apply owner is interrupted, preserving the
durable run evidence instead of requiring SQLite repair. Live owners are never
recovered, and retained worktrees and candidates are not deleted.
`room prune` applies `[rooms].keep_rooms` and `retention_days` only to terminal
rooms; active and recovery-required state is never pruned.

## Work-board & team profiles

A single `run` preserves its result but does not provide queue state; the
**work-board** (`SYNCMILL_BOARD__ENABLED=true`,
off by default) adds durable, structured records of team work: items with a goal,
state (`planned → running → (blocked | review) → done`), dependencies, verification
evidence, and a handoff note, in their own SQLite db (`~/.syncmill/board.db`).
`board run` atomically claims the next dispatchable item and orchestrates it through
the normal pipeline — agents never touch the board, and marking an item `done`
requires verification (a passing quality gate, an evidence ref, or a human
sign-off). `board decide` records decisions agents must not re-litigate — injected
in full into matching board runs (best-effort: if the decisions can't be read, the
item runs without them); `board stats` surfaces duplicate work, stalls,
and false-dones. `board import-review-candidates` turns Tracegraph's body-free
failure-pattern report into idempotent, human-required `review` items; it never
runs agents or changes Toolgraph policy. `syncmill dashboard` is the compact live
board observer/editor. `syncmill tui` is the full seven-tab command center: it can
add/import/update board work, launch one selected item or a queue in a durable
background worker, reconnect to bounded local logs after the UI closes, and inspect
reports, teams, agents, and memory status. `q` closes only the UI; use the Runs tab's
explicit safe-stop action to interrupt a worker. Browsing and editing remain
cross-platform, but detached `x`/`g` execution, reconnect, and Runs `k`/`K` control
require POSIX `flock`, Unix sockets, and process-group signals; on non-POSIX systems,
use `syncmill board run` directly. The dashboard's `p` key opens
recorded run-envelope reports through the same renderer as `report show`. **Team profiles**
(`[teams.<name>]` in `syncmill.toml`) bundle an agent lineup + strategy +
capabilities, selected per run (`--team`), by board goal (`board run --goal`), or
persistently (`team use`). See the user guide:
[The work-board](docs/user-guide.md#the-work-board) ·
[Team profiles](docs/user-guide.md#team-profiles) ·
[워크보드로 팀처럼 일하기](docs/user-guide.ko.md#워크보드로-팀처럼-일하기).

## Development

```bash
uv sync --project "$SYNCMILL_SRC" --group dev     # install pytest
uv run pytest           # run the hermetic test suite (no network, no real agents/DB)
```

The suite replaces agents and memory with in-memory fakes (see
`tests/conftest.py`), so it never touches the real `~/.memtomem` DB or the
`claude`/`codex`/`kimi` binaries.
