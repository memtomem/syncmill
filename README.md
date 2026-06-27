# syncmill

Multi-agent orchestration with shared memory — grind tasks through competing and
collaborating AI coding agents (Claude Code, Codex CLI, Kimi Code).

syncmill hands a task to CLI coding agents, gives them a shared long-term memory
([memtomem](https://pypi.org/project/memtomem/)) for context continuity, and
isolates each run in its own git worktree. A central **Supervisor** is the only
mediator: agents are pure functions (prompt in → result out) and know nothing
about memtomem, each other, or the orchestrator.

> **Status:** All five orchestration strategies have shipped: **route**
> (priority fallback), **pipeline** (sequential chain, each agent building on the
> previous), **compete** (concurrent head-to-head — every agent runs at once in its
> own worktree and the winner is kept), **council** (multi-round propose →
> critique → synthesize deliberation through shared memory), and **decompose**
> (split the task into subtasks, run them across agents concurrently, then
> synthesize the results into one outcome). The architecture is built to grow
> more. See [CLAUDE.md](CLAUDE.md) for the design invariants.

> **New to syncmill?** Start with the **[User Guide](docs/user-guide.md)** — a
> friendly, task-oriented walkthrough (install → first run → strategies →
> configuration → troubleshooting). 한국어 안내서:
> **[처음 시작하는 분들을 위한 가이드](docs/user-guide.ko.md)**.

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

# Watch a concurrent run live in tmux — one pane per agent, auto-attached
uv run syncmill run "Design the caching layer" --strategy compete --monitor

# Drive one agent interactively yourself; syncmill runs the rest (repeatable)
uv run syncmill run "Refactor the auth module" --exclude claude

# Drive that agent in a DURABLE tmux session — survives detach, reattach by name
uv run syncmill session start claude      # attach; Ctrl-b d detaches, agent keeps running
uv run syncmill session list              # show active sessions
uv run syncmill session attach claude     # reconnect later
uv run syncmill session stop claude       # (or --all)

# Point at another repo, override the per-agent timeout (seconds)
uv run syncmill run "Fix the failing test" --repo /path/to/repo --timeout 600

# Inspect registered agents (priority order) and their settings
uv run syncmill agents

# Check the memtomem connection
uv run syncmill status

# Serve shared memory read-only to external MCP clients (stdio)
uv run syncmill serve
```

For a step-by-step first run with real agent CLIs (including billing/quota,
worktree isolation, and why `Files changed` is not automatically applied to your
checkout), see [Running real agents safely](docs/user-guide.md#running-real-agents-safely)
or the Korean guide's [실제 에이전트로 안전하게 실행하기](docs/user-guide.ko.md#실제-에이전트로-안전하게-실행하기).

## Read-only MCP gateway (`syncmill serve`)

syncmill deliberately keeps its **agents** off MCP — the Supervisor is the sole reader/writer
of memory. `syncmill serve` adds a **one-way (read-only) MCP gateway** so external/interactive
MCP clients (Claude Code/Desktop, Cursor, …) can *read* syncmill's accumulated `shared`
knowledge without breaking that isolation. It is a separate, non-agent consumer: agents still
never touch MCP, the Supervisor stays the sole writer, and the gateway exposes **only** the
`shared` namespace — never any agent's private scope.

Three read-only tools (no write/delete/index):

| Tool | Purpose |
|------|---------|
| `shared_search(query, top_k)` | Ranked excerpts from `shared` (each result carries its chunk id). |
| `shared_read(chunk_id)` | Full content of one `shared` chunk by id (refuses non-shared ids). |
| `shared_stats()` | Number of chunks in `shared`. |

```bash
# stdio (default) — register with an MCP client, e.g. Claude Code:
claude mcp add syncmill-shared -- uv run syncmill serve

# HTTP (streamable-http) for remote consumers — loopback by default:
uv run syncmill serve --http --port 8765        # http://127.0.0.1:8765/mcp
```

**Security:** HTTP serving exposes read-only shared memory with **no authentication**. The bind
host defaults to `127.0.0.1` (loopback); a non-loopback `--host` is warned about at startup —
only use it on a trusted network or behind an authenticated proxy.

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
| `SYNCMILL_AGENT_PRIORITY` | `["claude","codex","kimi-code"]` | Agent order (JSON list) — fallback for `route`, stage/winner priority for the others |
| `SYNCMILL_DEFAULT_STRATEGY` | `route` | Orchestration strategy (`route`, `pipeline`, `compete`, `council`, or `decompose`; or pass `--strategy`) |
| `SYNCMILL_COUNCIL__ROUNDS` | `1` | Council critique rounds (≥1) between propose and synthesize |
| `SYNCMILL_DECOMPOSE__MAX_SUBTASKS` | `8` | Max subtasks the decompose planner may produce (≥1) |
| `SYNCMILL_MONITOR__ENABLED` | `false` | Live tmux monitor for concurrent runs (or pass `--monitor`) |
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
  **concurrently** in isolated worktrees (each *completed* subtask publishes its
  result to `shared`; a failed subtask is tolerated and skipped, and synthesis
  proceeds as long as at least one completed), then every agent **synthesizes** the
  subtask results into one solution and the winner is picked the same way `compete`
  does. Integration is via shared knowledge,
  not a file merge. Capped at `SYNCMILL_DECOMPOSE__MAX_SUBTASKS` subtasks.
- **Memory is advisory** — best-effort and non-fatal. A broken or missing
  memtomem never aborts a task.
- **Worktrees** isolate each run under `../.syncmill-worktrees/`, with a tempdir
  fallback for non-git targets. Orphans from crashed runs are swept on the next
  run.

## Live monitoring (`--monitor`)

`--monitor` gives a **live tmux view** of a concurrent run — one pane per agent,
streaming each agent's output as it happens, auto-attached:

```bash
uv run syncmill run "Design the caching layer" -s compete --monitor
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
uv run syncmill session start [AGENT]   # launch (or reattach), then attach; default AGENT = first in priority
uv run syncmill session list            # active sessions + attached/detached state
uv run syncmill session attach TARGET   # reconnect (TARGET = agent id or full session name)
uv run syncmill session stop [TARGET] [--all]
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

## Development

```bash
uv sync --group dev     # install pytest
uv run pytest           # run the hermetic test suite (no network, no real agents/DB)
```

The suite replaces agents and memory with in-memory fakes (see
`tests/conftest.py`), so it never touches the real `~/.memtomem` DB or the
`claude`/`codex`/`kimi` binaries.
