# syncmill

Multi-agent orchestration with shared memory — grind tasks through competing and
collaborating AI coding agents (Claude Code, Gemini CLI, Codex CLI).

syncmill hands a task to CLI coding agents, gives them a shared long-term memory
([memtomem](https://pypi.org/project/memtomem/)) for context continuity, and
isolates each run in its own git worktree. A central **Supervisor** is the only
mediator: agents are pure functions (prompt in → result out) and know nothing
about memtomem, each other, or the orchestrator.

> **Status:** v0.1 — one orchestration strategy (priority fallback). The
> architecture is built to grow more (pipeline, compete, council). See
> [CLAUDE.md](CLAUDE.md) for the design invariants.

## Install

syncmill uses [uv](https://docs.astral.sh/uv/) and Python 3.12+:

```bash
uv sync
```

### Prerequisites

- **Agent CLIs** — install and authenticate whichever you want to use; they are
  invoked as subprocesses:
  - `claude` ([Claude Code](https://claude.com/claude-code))
  - `gemini` (Gemini CLI)
  - `codex` (Codex CLI)
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

Every field is overridable via `SYNCMILL_*` environment variables (nested fields
use a `__` delimiter):

| Variable | Default | Meaning |
| --- | --- | --- |
| `SYNCMILL_AGENT_PRIORITY` | `["claude","gemini","codex"]` | Fallback order (JSON list) |
| `SYNCMILL_DEFAULT_STRATEGY` | `route` | Orchestration strategy |
| `SYNCMILL_REPO_ROOT` | `.` | Default target repo |
| `SYNCMILL_MEMORY__DB_PATH` | `~/.memtomem/memtomem.db` | memtomem SQLite path |
| `SYNCMILL_CLAUDE__ENABLED` | `true` | Enable/disable an agent |
| `SYNCMILL_CLAUDE__TIMEOUT_SECONDS` | `300` | Per-agent timeout |
| `SYNCMILL_CLAUDE__MAX_TURNS` | `10` | Per-agent max turns |

(`CLAUDE`, `GEMINI`, `CODEX` sub-configs all accept `ENABLED`, `TIMEOUT_SECONDS`,
`MAX_TURNS`.)

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
- **RouteStrategy** (v0.1) tries agents in priority order and returns the first
  success; if all fail it returns an aggregated error.
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
`claude`/`gemini`/`codex` binaries.
