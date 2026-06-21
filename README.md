# syncmill

Multi-agent orchestration with shared memory — grind tasks through competing and
collaborating AI coding agents (Claude Code, Codex CLI, Kimi Code).

syncmill hands a task to CLI coding agents, gives them a shared long-term memory
([memtomem](https://pypi.org/project/memtomem/)) for context continuity, and
isolates each run in its own git worktree. A central **Supervisor** is the only
mediator: agents are pure functions (prompt in → result out) and know nothing
about memtomem, each other, or the orchestrator.

> **Status:** v0.3 (in progress) — five orchestration strategies: **route**
> (priority fallback), **pipeline** (sequential chain, each agent building on the
> previous), **compete** (concurrent head-to-head — every agent runs at once in its
> own worktree and the winner is kept), **council** (multi-round propose →
> critique → synthesize deliberation through shared memory), and **decompose**
> (split the task into subtasks, run them across agents concurrently, then
> synthesize the results into one outcome). The architecture is built to grow
> more. See [CLAUDE.md](CLAUDE.md) for the design invariants.

## Install

syncmill uses [uv](https://docs.astral.sh/uv/) and Python 3.12+:

```bash
uv sync
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
  provider is governed by `~/.memtomem/config.json` (memtomem applies it last).
  If memory is unavailable or misconfigured, syncmill **degrades gracefully** —
  it logs a warning and runs the agents anyway, just without shared context. By
  default syncmill shares the global `~/.memtomem/memtomem.db`.

## Usage

```bash
# Run a task through the orchestration pipeline (priority fallback)
uv run syncmill run "Add type hints to main.py"

# Force a single agent (no fallback)
uv run syncmill run "Refactor the auth module" --agent claude

# Chain agents as a pipeline — each stage builds on the previous via shared memory
uv run syncmill run "Implement the parser, then add tests" --strategy pipeline

# Convene a council — agents propose, critique over rounds, then synthesize a winner
uv run syncmill run "Design the caching layer" --strategy council

# Decompose — split into subtasks, run them across agents concurrently, then synthesize
uv run syncmill run "Add a config loader with validation and tests" --strategy decompose

# Drive one agent interactively yourself; syncmill runs the rest (repeatable)
uv run syncmill run "Refactor the auth module" --exclude claude

# Point at another repo, override the per-agent timeout (seconds)
uv run syncmill run "Fix the failing test" --repo /path/to/repo --timeout 600

# Inspect registered agents (priority order) and their settings
uv run syncmill agents

# Check the memtomem connection
uv run syncmill status
```

## Configuration

Config is resolved from three layers, **highest priority first**:

1. `SYNCMILL_*` environment variables.
2. A project `syncmill.toml` in the current working directory.
3. Built-in defaults.

So **env always overrides the file, and the file overrides defaults** — every
field stays env-overridable. The CLI flags `--repo` / `--strategy` / `--timeout`
sit above all three (an explicit per-invocation override).

### Environment variables

Every field is overridable via `SYNCMILL_*` environment variables (nested fields
use a `__` delimiter):

| Variable | Default | Meaning |
| --- | --- | --- |
| `SYNCMILL_AGENT_PRIORITY` | `["claude","codex","kimi-code"]` | Fallback order (JSON list) |
| `SYNCMILL_DEFAULT_STRATEGY` | `route` | Orchestration strategy (`route`, `pipeline`, `compete`, `council`, or `decompose`; or pass `--strategy`) |
| `SYNCMILL_COUNCIL__ROUNDS` | `1` | Council critique rounds (≥1) between propose and synthesize |
| `SYNCMILL_DECOMPOSE__MAX_SUBTASKS` | `8` | Max subtasks the decompose planner may produce (≥1) |
| `SYNCMILL_REPO_ROOT` | `.` | Default target repo |
| `SYNCMILL_MEMORY__DB_PATH` | `~/.memtomem/memtomem.db` | memtomem SQLite path |
| `SYNCMILL_CLAUDE__ENABLED` | `true` | Enable/disable an agent |
| `SYNCMILL_CLAUDE__TIMEOUT_SECONDS` | `300` | Per-agent timeout |
| `SYNCMILL_CLAUDE__MAX_TURNS` | `10` | Per-agent max turns |

(`CLAUDE`, `CODEX`, `KIMI_CODE` sub-configs all accept `ENABLED`,
`TIMEOUT_SECONDS`, `MAX_TURNS`. The hyphenated agent id `kimi-code` maps to the
`KIMI_CODE` env prefix, e.g. `SYNCMILL_KIMI_CODE__TIMEOUT_SECONDS`; `MAX_TURNS`
is only honored by `claude`.)

### `syncmill.toml`

Drop a `syncmill.toml` in the directory you run syncmill from to set a checked-in
project baseline. Copy [`syncmill.toml.example`](syncmill.toml.example) to get
started. Every field has the same name as its config field, and `SYNCMILL_*` env
vars still override it:

```toml
agent_priority = ["claude", "codex", "kimi-code"]
default_strategy = "compete"
repo_root = "."

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
- **Discovery is cwd-relative** — `syncmill.toml` is read from the directory you
  invoke syncmill in, not from `--repo`/`repo_root` (which the file itself can
  set). Missing file → just defaults + env.
- **Typo protection:** an unknown *top-level* key in the file is rejected with a
  clear error rather than silently ignored.

## How it works

```
User ─▶ Supervisor ─┬─▶ (read) memtomem  → inject context into the prompt
                    ├─▶ create git worktree (isolated workspace)
                    ├─▶ Strategy ─▶ Agent subprocess (prompt in → stdout out)
                    ├─▶ (write) memtomem ← share the result
                    └─▶ remove worktree
```

- **Supervisor** is the sole mediator and the only reader/writer of memtomem.
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
  own isolated worktree, from the same enriched prompt — then keeps a winner: the
  first agent to complete in priority order (deterministic, never finish time).
  All agents actually run (unlike `route`'s short-circuit); if none succeed it
  returns an aggregated error. The winner's diff is surfaced (`files_changed`)
  and shared, and every worktree is cleaned up.
- **CouncilStrategy** (`council`) runs a multi-round deliberation through shared
  memory: agents **propose** a solution concurrently (one worktree each), then
  **critique** over `SYNCMILL_COUNCIL__ROUNDS` rounds — each reading the council's
  proposals and prior critiques from `shared` and refining its own worktree — then
  every member **synthesizes** a final solution and the winner is picked the same
  way `compete` does (first gate-passing completer in priority order). Deliberation
  flows only through `shared` (agents never see each other's worktrees), so the
  synthesized winner reflects more than one agent's input when shared memory
  surfaces peers (best-effort and relevance-ranked, like the pipeline handoff; a
  degraded memtomem never aborts the council — each member just falls back to its
  own accumulated worktree).
- **DecomposeStrategy** (`decompose`) splits a compound task into subtasks and runs
  them **across** agents: the highest-priority agent **plans** the breakdown (a JSON
  subtask list; an unparseable/empty plan degrades to running the whole task as one
  subtask), the subtasks are distributed round-robin over the agents and run
  **concurrently** in isolated worktrees (each publishing its result to `shared`),
  then every agent **synthesizes** the subtask results into one solution and the
  winner is picked the same way `compete` does. Integration is via shared knowledge,
  not a file merge. Capped at `SYNCMILL_DECOMPOSE__MAX_SUBTASKS` subtasks.
- **Memory is advisory** — best-effort and non-fatal. A broken or missing
  memtomem never aborts a task.
- **Worktrees** isolate each run under `../.syncmill-worktrees/`, with a tempdir
  fallback for non-git targets. Orphans from crashed runs are swept on the next
  run.

## Development

```bash
uv sync --group dev     # install pytest
uv run pytest           # run the hermetic test suite (no network, no real agents/DB)
```

The suite replaces agents and memory with in-memory fakes (see
`tests/conftest.py`), so it never touches the real `~/.memtomem` DB or the
`claude`/`codex`/`kimi` binaries.
