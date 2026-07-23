# syncmill User Guide

> A friendly, task-oriented guide to running syncmill. For the design rationale and
> internal invariants, see [CLAUDE.md](../CLAUDE.md); for a quick reference, see the
> [README](../README.md). **한국어 안내서:** [user-guide.ko.md](user-guide.ko.md).

## Table of contents

- [What syncmill is](#what-syncmill-is)
- [Install](#install)
- [Your first run](#your-first-run)
- [Running real agents safely](#running-real-agents-safely)
- [Core concepts](#core-concepts)
- [Choosing a strategy](#choosing-a-strategy)
- [Command reference](#command-reference)
- [Configuration](#configuration)
- [Optional features](#optional-features)
- [The work-board](#the-work-board)
- [Common workflows](#common-workflows)
- [Troubleshooting](#troubleshooting)

---

## What syncmill is

**syncmill runs one coding task through several AI coding agents at once** — Claude
Code, Codex CLI, and Kimi Code — and orchestrates how they cooperate or compete. It
gives them a **shared long-term memory** ([memtomem](https://pypi.org/project/memtomem/))
so what one agent learns is available to the others, and it runs every agent in an
**isolated git worktree**, so syncmill's agents never touch your real working tree.

You give syncmill a prompt; it picks the agents, runs them according to a
**strategy** you choose, and hands you back one result.

The whole system follows one rule: a central **Supervisor** is the only thing that
touches both the agents and the memory. The agents themselves are simple — *prompt
in, result out* — and know nothing about each other, the memory, or the Supervisor.

---

## Install

syncmill uses [uv](https://docs.astral.sh/uv/) and Python 3.12+. The supported
beginner path is a source checkout:

```bash
git clone https://github.com/memtomem/syncmill.git
cd syncmill
uv sync
export SYNCMILL_SRC="$PWD"
cd /path/to/my-project
```

Run Syncmill from the target Git repository while `--project` points to the
source checkout:

```bash
uv run --project "$SYNCMILL_SRC" syncmill <command>
```

### Prerequisites

1. **At least one agent CLI**, installed and logged in. syncmill shells out to the
   real binaries, so they must be on your `PATH` and authenticated:

   | Agent id   | Binary   | Tool                                                                  |
   |------------|----------|-----------------------------------------------------------------------|
   | `claude`   | `claude` | [Claude Code](https://claude.com/claude-code)                         |
   | `codex`    | `codex`  | Codex CLI                                                             |
   | `kimi-code`| `kimi`   | [Kimi Code](https://moonshotai.github.io/kimi-code/)                  |

   You don't need all three. A missing or logged-out agent simply fails, and (with
   the default `route` strategy) the next one runs instead.

2. **A memtomem embedding backend** *(optional but recommended)*. Shared memory needs
   an embedding backend. syncmill installs `memtomem[onnx,korean]`, so the local
   **ONNX** provider works out of the box; local **ollama** or an **OpenAI** key work
   too. The active provider is set in `~/.memtomem/config.json`. **If memory is
   unavailable, syncmill keeps working** — it just runs the agents without shared
   context and prints a yellow notice.

   By default SyncMill uses memtomem's global database and memory directory. For
   a private or throwaway runtime, set both `memory.db_path` and
   `memory.memory_dir`; setting only the database path does not relocate the
   Markdown source files written by memtomem.

   P3 also provides an optional mem0 backend. Install it with
   `uv sync --project "$SYNCMILL_SRC" --extra mem0`, set
   `SYNCMILL_MEMORY__BACKEND=mem0`, and run `syncmill status`.
   It defaults to local Qdrant and multilingual FastEmbed under
   `~/.syncmill/mem0`; writes always use `infer=False`, so no LLM is called.
   First use downloads model assets, and anonymous mem0 telemetry is off by default.
   Embedded Qdrant holds an exclusive lock on its configured path, so only one
   process can use the default local store at a time. Concurrent runs, `serve`,
   and `status` should use a Qdrant server configured with
   `memory.mem0.config.vector_store.config.url`; Mem0 history still remains under
   `data_dir`. Mem0 reads its telemetry setting on the process's first `mem0`
   import, so set it before launch and restart the process to change it. If
   another library imports Mem0 first, that earlier setting wins for the process.

   A third option is LangGraph's persistent `AsyncSqliteStore`. Install it with
   `uv sync --project "$SYNCMILL_SRC" --extra langgraph`, set
   `SYNCMILL_MEMORY__BACKEND=langgraph`, and optionally set
   `SYNCMILL_MEMORY__LANGGRAPH__DB_PATH`. It stores verbatim text
   in `~/.syncmill/langgraph.db` by default and uses local multilingual FastEmbed;
   no LLM is called.

### Check your setup

```bash
uv run --project "$SYNCMILL_SRC" syncmill --version
uv run --project "$SYNCMILL_SRC" syncmill agents
```

---

## Your first run

Initialize the target project, check provider readiness, and inspect the bounded
plan before making a paid/remote provider call:

```bash
uv run --project "$SYNCMILL_SRC" syncmill init
uv run --project "$SYNCMILL_SRC" syncmill doctor
uv run --project "$SYNCMILL_SRC" syncmill run --dry-run "Add type hints to main.py"
```

`init` creates `syncmill.toml`, `.syncmill/project.json`, and
`.syncmill/.gitignore` without staging them. The default `doctor` succeeds when
at least one enabled provider is ready and reports the others as warnings. Use
`doctor --strict` when every enabled priority provider must be ready.

```bash
uv run --project "$SYNCMILL_SRC" syncmill run \
  "Add type hints to main.py and run the relevant checks" \
  --agent codex --timeout 120
```

The result remains an artifact until you inspect and explicitly apply it.

---

## Running real agents safely

The test suite is hermetic, but `syncmill run` is not a simulation. It launches the
real agent CLIs from your `PATH`, using your current shell environment and each
tool's logged-in account. Expect normal agent behavior: network access, provider
billing or quota usage, tool execution inside the agent workspace, and whatever
approval model that CLI uses in non-interactive mode.

syncmill still keeps the target repository isolated:

- syncmill snapshots HEAD plus tracked dirty and untracked files into a private
  control clone, then gives each agent a separate worktree. Ignored files stay
  excluded. Unsafe or non-Git states are blocked instead of falling back to an
  empty temporary directory.
- The final result prints the winning agent's text and `Files changed`, but syncmill
  does **not** merge or copy those changes back into your original working tree. To
  recover the winner's code, use the printed run id with `syncmill diff <run-id>`
  and `syncmill apply <run-id>`. The patch and manifest are always retained before
  worktree teardown; `--emit-patch` remains an optional compatibility copy.
- Worktrees are removed at the end of the run, while durable artifacts and apply
  receipts remain under `~/.syncmill/projects/<project-id>/runs/<run-id>/`.
- Legacy `session start` is different: it runs the raw interactive agent in your
  current working tree, so its edits are real local edits. Prefer `session open
  --workspace isolated` for a persistent project-scoped worktree whose patch is
  checkpointed and reviewed before application.

### A practical first real-agent run

Start with one forced agent and a short timeout. This proves authentication,
subprocess launch, worktree creation, result parsing, memory, and cleanup without
starting several paid/remote agents at once.

```bash
# 1. Confirm syncmill's view of your local setup.
uv run --project "$SYNCMILL_SRC" syncmill init
uv run --project "$SYNCMILL_SRC" syncmill doctor

# 2. Confirm the CLI you plan to use is available and authenticated.
command -v codex
codex --help

# 3. Run exactly one real agent against this repo.
uv run --project "$SYNCMILL_SRC" syncmill run \
  "Inspect this repo and summarize one low-risk improvement. Do not edit files." \
  --agent codex \
  --timeout 120
```

Successful output has this shape:

```text
[syncmill] Executing task 1a2b3c4d...
[syncmill] Agent: codex (forced, no fallback)

[codex] Completed in 42.1s

<agent output>
```

If the agent edits files in its isolated worktree, syncmill adds a line like:

```text
[syncmill] Files changed: README.md, src/example.py
```

That line is an inventory of the winning worktree's diff, not an applied patch in
your original checkout. Review and apply the retained artifact explicitly:

```bash
uv run --project "$SYNCMILL_SRC" syncmill runs list
uv run --project "$SYNCMILL_SRC" syncmill diff <run-id>
uv run --project "$SYNCMILL_SRC" syncmill apply <run-id>
uv run --project "$SYNCMILL_SRC" syncmill recover [<run-id>]   # reconcile a standalone apply interrupted by a crash
uv run --project "$SYNCMILL_SRC" syncmill continue <run-id> "Now add regression tests"
```

If `syncmill apply` (or a detached `apply --detach` worker) is killed mid-apply,
the run is left `applying`. `syncmill recover` reconciles it from the receipt,
patch digest, and source fingerprint: a completed apply is finished, an
untouched source is restored to `pending`, and anything undecidable is frozen to
`recovery_required` for you to inspect. A live apply (still holding the
repository apply lock) is never touched; room applies are recovered by
`room recover`. `apply` runs the same reconcile for its own run before retrying,
and the TUI Runs tab exposes it on `R`.

### Escalating from one agent to a real multi-agent run

Once one agent works, move up in this order:

```bash
# Fallback: cheaper than concurrent strategies because it stops at first success.
uv run --project "$SYNCMILL_SRC" syncmill run "Fix the smallest failing test and explain the change" -s route

# Head-to-head: all enabled agents run, so expect parallel billing/quota usage.
uv run --project "$SYNCMILL_SRC" syncmill run "Propose a robust CLI error-message cleanup" -s compete

# Live view for concurrent strategies, when tmux and an interactive terminal exist.
uv run --project "$SYNCMILL_SRC" syncmill run "Compare two approaches for config validation" -s compete --monitor
```

Use a quality gate before trusting a winner for implementation work:

```toml
[gate]
commands = ["pytest -q"]
timeout_seconds = 300
```

Then run:

```bash
uv run --project "$SYNCMILL_SRC" syncmill run "Fix the failing tests" --agent codex --timeout 600
```

The gate runs inside the agent's worktree. A passing gate means the isolated result
validated there; it still does not apply the diff to your original checkout.

### Real-agent troubleshooting checklist

| Symptom | Check |
|---------|-------|
| `CLI tool 'codex' not found in PATH` | Run `command -v codex` from the same shell where you run syncmill. |
| The agent fails immediately with auth text | Open that CLI directly and complete login/auth. |
| The run works in `uv run pytest` but not with a real agent | The tests use fakes; inspect the real CLI, account, network, and provider quota. |
| `Files changed` is printed but your repo is unchanged | Expected: review the printed run id with `syncmill diff <run-id>`, then use `syncmill apply <run-id>`. |
| A concurrent run is expensive or noisy | Use `--agent <id>` first, then `route`, then `compete`/`council`/`decompose`. |

---

## Core concepts

You only need five ideas to use syncmill well.

- **Agent** — a CLI coding tool (`claude` / `codex` / `kimi-code`) that syncmill runs
  as a subprocess. Each agent is identified by its id.

- **Priority order** — the default ordering of agents
  (`["claude", "codex", "kimi-code"]`). It's the fallback order for `route`, the
  stage order for `pipeline`, and the tie-break for who "wins" in `compete` /
  `council` / `decompose`. You can change it (see [Configuration](#configuration)).

- **Strategy** — *how* the agents are run. There are five (next section). The default
  is `route`.

- **Shared memory** — a knowledge base (`shared` namespace in memtomem) that the
  Supervisor reads before a run (to enrich the prompt) and writes after (to record
  the result). It's how agents "collaborate" without ever talking directly. Memory is
  **advisory**: if it breaks, the task still runs. With the default memtomem backend,
  a successful result first lands in its producer's `agent-runtime:<id>` namespace
  and is then copied to `shared` with `shared-from=<chunk-id>` provenance. Context
  lookup ranks the private and shared namespaces as one merged result pool.

- **Worktree** — a throwaway checkout under the run's private
  `~/.syncmill/workspaces/<run-id>/` snapshot. Your real working tree is not
  touched until an explicit `syncmill apply`; the durable patch lives in the
  separate project run store.

---

## Choosing a strategy

Pick the strategy with `-s` / `--strategy` (or set `default_strategy` in config).

| Strategy    | What it does                                                                                       | Reach for it when…                                                            |
|-------------|----------------------------------------------------------------------------------------------------|------------------------------------------------------------------------------|
| `route`     | Try agents in priority order; return the **first success**. *(default)*                             | You just want the task done by whoever can do it. Cheapest — stops early.     |
| `pipeline`  | Run agents as **sequential stages**; each builds on the previous via shared memory. Fail-fast.      | The task has natural phases ("implement, then test, then document").          |
| `compete`   | Run **every agent concurrently** in its own worktree; keep the highest-priority one that succeeds.  | You want several independent attempts at the *same* task at once, keeping the highest-priority success (most useful with a quality gate).         |
| `council`   | Agents **propose → critique (×rounds) → synthesize**, deliberating through shared memory.            | Hard design problems that benefit from agents reviewing each other's ideas.   |
| `decompose` | A planner **splits** the task into subtasks, agents run them concurrently, then **synthesize**.     | Big compound tasks that break cleanly into independent pieces.                |

### How each one works, in one line

- **`route`** — `claude` first; if it fails, `codex`; if that fails, `kimi-code`. If
  all fail, you get an aggregated error.
- **`pipeline`** — agents run one after another in a *single shared* worktree, each
  reading prior results from `shared` memory, so the chain builds on itself. If one
  stage breaks, the pipeline stops and tells you which.
- **`compete`** — all agents run at the same time, each in its *own* worktree, from the
  same enriched prompt. The winner is the first agent to complete **in priority
  order** (deterministic — not whoever happens to finish first by the clock).
- **`council`** — every agent drafts a proposal; those that complete become the
  council *members*, who refine over `council.rounds` critique rounds (reading peers'
  proposals/critiques from `shared`) and then produce a final synthesis — the winner
  is picked the `compete` way.
- **`decompose`** — the top-priority agent plans a JSON list of subtasks; the subtasks
  are spread round-robin across agents and run concurrently; then every agent reads the
  subtask results from `shared` and produces one integrated solution. Integration is by
  shared *knowledge*, not a file merge.

Pipeline stages, council proposals/critiques, and the normalized decompose plan/subtasks
carry `pipeline-stage`, `council-proposal`, `council-critique`, `decompose-plan`, and
`decompose-subtask` kind tags. Unified context search can rank them together, while an
advanced `tag_filter` query can retrieve one artifact class precisely. Decompose shares
the validated, capped plan that actually ran, never the planner's raw response.

> The concurrent strategies (`compete`, `council`, `decompose`) can be watched live
> with [`--monitor`](#live-monitoring---monitor).

---

## Command reference

syncmill's commands: `run`, `agents`, `status`, `serve`, `dashboard`, `tui`, and the
`session`, `board`, and `team` groups. Run `uv run --project "$SYNCMILL_SRC" syncmill --help` or
`uv run --project "$SYNCMILL_SRC" syncmill <command> --help` any time; `uv run --project "$SYNCMILL_SRC" syncmill --version` prints the
installed version. (`team` is covered in [Team profiles](#team-profiles); `board` and
`dashboard` are summarized below and explained in [The work-board](#the-work-board).)

### `run` — execute a task

```bash
uv run --project "$SYNCMILL_SRC" syncmill run "<prompt>" [options]
```

| Option            | Short | Meaning                                                                                                   |
|-------------------|-------|-----------------------------------------------------------------------------------------------------------|
| `--agent`         | `-a`  | Force a single agent (no fallback).                                                                        |
| `--exclude`       | `-x`  | Exclude an agent from the run. **Repeatable.** (e.g. drive it interactively yourself.)                     |
| `--strategy`      | `-s`  | `route` (default) · `pipeline` · `compete` · `council` · `decompose`.                                      |
| `--timeout`       | `-t`  | Override the per-agent timeout, in seconds.                                                                |
| `--repo`          | `-r`  | Target repository root (default: current directory or `repo_root` from config).                           |
| `--capability`    | `-c`  | Capability hint. **Repeatable.** Routes an agent declaring a matching capability ahead of priority order. |
| `--team`          |       | Run with a named team profile (`[teams.<name>]`). See [Team profiles](#team-profiles).                    |
| `--monitor`       |       | Live tmux view of a concurrent run (one pane per agent). See [Live monitoring](#live-monitoring---monitor).|

The prompt is a positional argument. To pass a prompt that starts with a dash, use
`--` first: `uv run --project "$SYNCMILL_SRC" syncmill run -- "--fix the flag parser"`.

### `agents` — list registered agents

```bash
uv run --project "$SYNCMILL_SRC" syncmill agents
```

Shows agents in priority order with their enabled/disabled state, timeout, and max
turns.

### `status` — check memory

```bash
uv run --project "$SYNCMILL_SRC" syncmill status
```

Shows whether the selected memory backend is connected and its safe storage details.

### `serve` — read-only MCP gateway

Exposes syncmill's accumulated `shared` knowledge — and, when enabled, the
[work-board](#the-work-board) — to **external** MCP clients (Claude Code/Desktop,
Cursor, …) **read-only**. It's a one-way consumer surface — agents never touch MCP and
the Supervisor stays the sole writer; there is no write tool for memory or the board.
Six tools: `shared_search`, `shared_read`, `shared_stats` over shared knowledge, plus
`board_list`, `board_get`, `board_stats` over the work-board. The board tools are
always registered; with the board disabled they simply answer
`the work-board is not enabled on this server`.

```bash
# stdio (default) — register with an MCP client, e.g. Claude Code:
claude mcp add syncmill-shared -- uv run --project "$SYNCMILL_SRC" syncmill serve

# HTTP for remote consumers — loopback by default, port 8000:
uv run --project "$SYNCMILL_SRC" syncmill serve --http                 # http://127.0.0.1:8000/mcp
uv run --project "$SYNCMILL_SRC" syncmill serve --http --port 8765     # custom port
```

| Option   | Default     | Meaning                                                             |
|----------|-------------|--------------------------------------------------------------------|
| `--http` | off (stdio) | Serve over HTTP (streamable-http) instead of stdio.                 |
| `--host` | `127.0.0.1` | Bind host for `--http`. A non-loopback host is warned about.        |
| `--port` | `8000`      | Bind port for `--http`.                                             |

> **Security:** HTTP serving has **no authentication**. Keep it on loopback, or put it
> behind a trusted network / authenticated proxy.

### `session` — durable interactive sessions

Run an agent's **interactive** REPL (the `claude` / `codex` / `kimi` you type into) in
a tmux session that **survives detach and reattaches by name** — so a long
collaboration outlives a closed terminal or dropped SSH. Every subcommand needs tmux;
`start` / `attach` also need an interactive terminal.

```bash
uv run --project "$SYNCMILL_SRC" syncmill session start [AGENT]   # launch (or reattach) + attach; default = first in priority
uv run --project "$SYNCMILL_SRC" syncmill session list            # active sessions + attached/detached state
uv run --project "$SYNCMILL_SRC" syncmill session attach TARGET   # reconnect (TARGET = agent id or full session name)
uv run --project "$SYNCMILL_SRC" syncmill session stop [TARGET]   # stop one session
uv run --project "$SYNCMILL_SRC" syncmill session stop --all      # stop all syncmill sessions
```

Detach with **`Ctrl-b d`** — the agent keeps running; `session attach` reconnects to
the *same* live session. See [Interactive collaboration](#interactive-collaboration)
for how this pairs with `run --exclude`.

For code-changing sessions, use the isolated form:

```bash
uv run --project "$SYNCMILL_SRC" syncmill session open codex --workspace isolated
uv run --project "$SYNCMILL_SRC" syncmill session isolated-list
uv run --project "$SYNCMILL_SRC" syncmill session attach-isolated <session-id>
uv run --project "$SYNCMILL_SRC" syncmill session checkpoint <session-id>
uv run --project "$SYNCMILL_SRC" syncmill session close <session-id>
```

The worktree persists across detach/attach. Before attachment, syncmill verifies
that tmux is still in the recorded worktree. A checkpoint exports the current diff
as a normal immutable run artifact for `runs list`, `diff`, and `apply`; closing the
session removes the isolated workspace. `--workspace direct` explicitly selects the
legacy current-working-tree behavior.

### `room` — multi-CLI Compare & Apply

Rooms run multiple unique providers concurrently in isolated, persistent worktrees
and stop at a review barrier. Enable them explicitly because one round may consume
quota from every member:

```bash
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room create "Refactor auth" -a claude -a codex
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room round <room-id> --detach
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room candidates <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room diff <candidate-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room select <room-id> <candidate-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room apply <room-id> --verify
# after a worker crash and lease expiry, or an interrupted apply
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room recover <room-id>
SYNCMILL_ROOMS__ENABLED=true uv run --project "$SYNCMILL_SRC" syncmill room prune --dry-run
```

All attempts—including failures, timeouts, partial patches, and bounded output—are
retained. Selection does not apply code. Apply fails closed on source drift or
candidate digest mismatch and then uses the standard run-artifact apply path. Later
rounds use `--lineage continue-own` by default; `fresh` resets member worktrees and
`branch-from --base-candidate <id>` seeds them from one eligible candidate. The
initial release supports up to three distinct providers, not duplicate instances of
one provider. `room recover` recovers an expired round lease only after its local
worker PID is gone. It also reconciles an `applying` room whose apply owner was
interrupted, preserving the durable run evidence and moving the room to an explicit
recovery state. It never recovers a live owner; retained worktrees and candidates
remain available for inspection.
`room prune` applies the configured count and age bounds only to terminal rooms;
start with `--dry-run` to inspect the exact ids.

### `board` — persistent work-board

Durable, structured records of team work. Requires `SYNCMILL_BOARD__ENABLED=true`
(see [The work-board](#the-work-board) for the concepts and a walkthrough).

```bash
uv run --project "$SYNCMILL_SRC" syncmill board add "<task>" [--goal <g>] [--depends-on <id>]... [--human-required]
uv run --project "$SYNCMILL_SRC" syncmill board import-review-candidates <report.json> [--goal <g>]
uv run --project "$SYNCMILL_SRC" syncmill board list [--state <s>] [--goal <g>]
uv run --project "$SYNCMILL_SRC" syncmill board show <id>
uv run --project "$SYNCMILL_SRC" syncmill board run [--id <id> | --loop] [--goal <g>] [-s <strategy>] [-a <agent>] [--team <name>]
uv run --project "$SYNCMILL_SRC" syncmill board update <id> [--state <s>] [--evidence <ref>] [--sign-off] [--note <text>] [--clear-human-required]
uv run --project "$SYNCMILL_SRC" syncmill board decide "<text>" [--scope <goal>] [--supersedes <id>]
uv run --project "$SYNCMILL_SRC" syncmill board decisions [--goal <g>]
uv run --project "$SYNCMILL_SRC" syncmill board stats
```

| Subcommand  | What it does                                                                                                                                                                                                        |
|-------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `add`       | Add a `planned` item. `--depends-on` (repeatable) keeps it undispatchable until every dependency is `done`; `--human-required` excludes it from auto-dispatch.                                                       |
| `import-review-candidates` | Import a Tracegraph v1 report as idempotent `review`, human-required items. It validates the complete batch first and never runs agents or changes policy. |
| `list`      | List items, optionally filtered by `--state` (`planned`/`running`/`blocked`/`review`/`done`) and/or `--goal`.                                                                                                        |
| `show`      | One item in full: state, evidence, handoff, and the append-only audit history.                                                                                                                                       |
| `run`       | Atomically claim an item (the next dispatchable one, or `--id`) and orchestrate it through the normal pipeline. `--loop` drains every dispatchable item (exits non-zero if any run failed); `--goal` scopes the claim to that goal and auto-applies the [team](#team-profiles) declaring it. `--id` is mutually exclusive with `--loop` and `--goal`. |
| `update`    | The **human path**: transition state, attach evidence, sign off, annotate. `--state done` **requires** `--evidence` or `--sign-off`; `running` can never be set by hand (it's owned by the claim).                    |
| `decide`    | Record a team decision agents must **not re-litigate** — injected in full into matching board runs (best-effort: a failed decisions read never blocks the item). `--scope` limits it to one goal (default: global); `--supersedes` retires an earlier decision. |
| `decisions` | List active (non-superseded) decisions, optionally for one goal (global + goal-scoped).                                                                                                                              |
| `stats`     | Team-health stats: duplicate work, stalls, false-done. See [Team-health stats](#team-health-stats-board-stats).                                                                                                      |

### `dashboard` — live board TUI

A live terminal UI over the work-board: a polling item table, a detail pane, a stats
overlay, read-only run reports, and the human-path edits bound to keys. Requires the
board enabled **and** an interactive terminal — there is no fallback (like `session`,
the TUI *is* the feature).

```bash
uv run --project "$SYNCMILL_SRC" syncmill dashboard                # poll every 2s
uv run --project "$SYNCMILL_SRC" syncmill dashboard --refresh 0.5  # faster polling (minimum 0.5s)
```

| Key | Action                                                                    |
|-----|---------------------------------------------------------------------------|
| `r` | Refresh now.                                                              |
| `f` | Cycle the state filter.                                                   |
| `e` | Update the selected item (the `board update` form — done gate enforced).  |
| `u` | Unblock the selected item.                                                |
| `h` | Clear its human-required flag.                                            |
| `s` | Stats overlay (`board stats`).                                            |
| `t` | TEAM pane (see [Team profiles](#team-profiles)).                          |
| `p` | REPORT pane: recorded runs newest-first; `r` refreshes while it is open.   |
| `q` | Quit.                                                                     |

Every edit goes through the same rules as `board update`. The dashboard reads reports
from `[integration.report].runs_dir` even when recording is currently disabled; the
highlighted detail is identical to `report show`. It never touches agents or memory.

### `tui` — full board command center

`syncmill tui` is a separate seven-tab UI for **Board, Runs, Reports, Teams, Status,
Sessions, and Rooms**. Unlike `dashboard`, it still opens when the board is disabled
or broken;
the Board tab shows how to enable/fix it, while the other tabs remain usable.
Browsing and editing remain available on non-POSIX systems. Detached `x`/`g`
execution, reconnect, and Runs `k`/`K` control require POSIX `flock`, Unix sockets,
and process-group signals; use `syncmill board run` directly on non-POSIX systems.
The Korean [screen-by-screen TUI guide](tui-guide.ko.md) documents all eleven
normal screen states, empty/limited/degraded states, action boundaries, and
actual 120×40 renders.

```bash
uv run --project "$SYNCMILL_SRC" syncmill tui                # poll every 2s
uv run --project "$SYNCMILL_SRC" syncmill tui --refresh 0.5  # minimum 0.5s
uv run --project "$SYNCMILL_SRC" syncmill tui --tab rooms    # open on a specific tab
```

`--tab auto` (the default) opens the first available surface in the order
**Rooms → Board → Runs → Status** — a tab counts as available when its feature
is enabled and its store actually opened. An explicit `--tab` is always
honored; an unavailable choice shows that tab's usual limited-mode banner.

Use `1`–`7` to switch tabs; switching focuses the tab's list and updates the
action bar at the bottom, which shows only the active tab's live keys (plus
the always-available `r`/`?`/`q`). The Board tab adds/imports/updates items,
manages team decisions, and starts either the selected planned item (`x`) or a
goal/all queue (`g`). Starting a job requires an explicit cost/quota
confirmation. Targeted runs may bypass dependency and human-required queue
gates; the confirmation calls this out.

The Runs tab has two sub-views, toggled with `v`: **Operations** (the detached
TUI-managed jobs, as before) and **Artifacts** (the durable run artifacts
`syncmill runs list` shows). In Artifacts, `d` toggles between the summary and
the preserved diff, `g` applies a pending run via a durable detached worker
(same confirmation and fencing as the CLI `apply --detach`), and `k` discards
a pending run. A successful Sessions checkpoint (`c`) now lands directly on
Runs → Artifacts with the new run selected for review.

Creating a Room with `a` records its prompt and members but makes no provider
call; member calls and cost begin only when `x` starts a round. In Rooms, the
selected candidate has three views: `d` **Diff** (the digest-
bound patch, paged — press `d` again to load more of a truncated patch, with
binary content marked), `o` **Output** (state, error, and gate first, then the
bounded provider output), and `m` **Details** (round, attempt, duration, exit
code, per-check gate results, files, digests, and truncation state). A
completed candidate opens on Diff; a failed one opens on Output. `R` recovers
dead room workers/interrupted applies for the whole project via `room recover`,
and `k` closes the
selected room (candidates and receipts are preserved).

Runs are owned by a detached manager, not Textual. Only one TUI-managed worker per
board is allowed, but ordinary external `board run` processes remain legal. Pressing
`q` closes only the UI. Reopen `syncmill tui` to reconnect to the worker and its
rolling local log. In Runs, `k` requests a cleanup-safe SIGINT; only after ten seconds
does `K` allow a separately confirmed force stop. Force stop can leave a board lease
or dirty isolated worktree that needs later inspection.

By default job state lives under `~/.syncmill/tui`, keeps 20 terminal jobs, and caps
each combined stdout/stderr log at 10 MiB. Override these with `[tui] state_dir`,
`keep_jobs`, and `log_max_bytes` (or `SYNCMILL_TUI__*`). The files are private to the
local user, but logs can still contain agent output—treat that directory as sensitive.

---

## Configuration

Config resolves from three layers, **highest priority first**:

1. **`SYNCMILL_*` environment variables** (nested fields use `__`).
2. **A project `syncmill.toml`** in the selected target Git root.
3. **Built-in defaults.**

So **env overrides the file, and the file overrides defaults** — every field stays
env-overridable. The CLI flags `--repo` / `--strategy` / `--timeout` override all
three for a single invocation.

> Direct commands use the current project. For `run --repo <target>`, Syncmill
> loads `<target>/syncmill.toml`; invalid TOML or invalid project-state paths are
> reported instead of silently falling back to the caller's configuration.

### Environment variables

| Variable                            | Default                       | Meaning                                                   |
|-------------------------------------|-------------------------------|-----------------------------------------------------------|
| `SYNCMILL_AGENT_PRIORITY`           | `["claude","codex","kimi-code"]` | Agent order (JSON list).                                |
| `SYNCMILL_DEFAULT_STRATEGY`         | `route`                       | `route`·`pipeline`·`compete`·`council`·`decompose`.       |
| `SYNCMILL_REPO_ROOT`                | `.`                           | Default target repo.                                      |
| `SYNCMILL_MEMORY__BACKEND`          | `memtomem`                    | `memtomem`, optional `mem0`, or optional `langgraph`.     |
| `SYNCMILL_MEMORY__DB_PATH`          | `~/.memtomem/memtomem.db`     | memtomem SQLite path.                                     |
| `SYNCMILL_MEMORY__MEMORY_DIR`       | unset (memtomem default)       | Explicit memtomem Markdown directory; set with `DB_PATH` for isolation. |
| `SYNCMILL_MEMORY__MEM0__DATA_DIR`   | `~/.syncmill/mem0`            | Local mem0 history/embedded-Qdrant directory.             |
| `SYNCMILL_MEMORY__MEM0__TELEMETRY`  | `false`                       | Value used on the process's first `mem0` import.          |
| `SYNCMILL_MEMORY__LANGGRAPH__DB_PATH` | `~/.syncmill/langgraph.db`  | Persistent LangGraph SQLite store.                        |
| `SYNCMILL_CLAUDE__ENABLED`          | `true`                        | Enable/disable an agent.                                  |
| `SYNCMILL_CLAUDE__TIMEOUT_SECONDS`  | `300`                         | Per-agent timeout (seconds).                              |
| `SYNCMILL_CLAUDE__MAX_TURNS`        | `10`                          | Per-agent max turns (honored by `claude` only).          |
| `SYNCMILL_COUNCIL__ROUNDS`          | `1`                           | Council critique rounds (≥1).                             |
| `SYNCMILL_DECOMPOSE__MAX_SUBTASKS`  | `8`                           | Max subtasks the decompose planner may produce (≥1).     |
| `SYNCMILL_MONITOR__ENABLED`         | `false`                       | Live tmux monitor for concurrent runs.                   |
| `SYNCMILL_MONITOR__SESSION_PREFIX`  | `syncmill`                    | tmux session-name prefix for monitored runs.             |
| `SYNCMILL_BOARD__ENABLED`           | `false`                       | The persistent [work-board](#the-work-board).            |
| `SYNCMILL_BOARD__DB_PATH`           | `~/.syncmill/board.db`        | Work-board SQLite path.                                  |
| `SYNCMILL_BOARD__LEASE_TIMEOUT_SECONDS` | `1800`                    | How long a claimed (`running`) item may sit before the next `board run` reclaims it to `planned` (≥1). |
| `SYNCMILL_BOARD__STALL_SECONDS`     | `3600`                        | Aging window for `board stats` stall signals (≥1; never affects dispatch). |
| `SYNCMILL_INTEGRATION__TRACE__ENABLED` | `false`                    | Write a body-free OTLP-JSON file for the run.             |
| `SYNCMILL_INTEGRATION__TRACE__EXPORTER` | `file`                     | Use `otlp-http` to also stream completed spans to Phoenix. |
| `SYNCMILL_INTEGRATION__TRACE__ENDPOINT` | unset                      | Phoenix endpoint; falls back to `PHOENIX_COLLECTOR_ENDPOINT`, then local Phoenix. |
| `SYNCMILL_INTEGRATION__TRACE__PROJECT_NAME` | unset                  | Phoenix project; falls back to `PHOENIX_PROJECT_NAME`, then `syncmill`. |

The per-agent sub-configs (`CLAUDE`, `CODEX`, `KIMI_CODE`) all accept `ENABLED`,
`TIMEOUT_SECONDS`, and `MAX_TURNS`. Note the hyphenated agent id `kimi-code` maps to
the **underscored** env prefix `SYNCMILL_KIMI_CODE__…`.

### Stream traces to Phoenix

Install the optional exporter and enable the existing trace integration:

```bash
uv sync --project "$SYNCMILL_SRC" --extra phoenix
PHOENIX_API_KEY="..." \
SYNCMILL_INTEGRATION__TRACE__ENABLED=true \
SYNCMILL_INTEGRATION__TRACE__EXPORTER=otlp-http \
uv run --project "$SYNCMILL_SRC" syncmill run "Fix the failing test"
```

SyncMill still writes `trace.otlp.json` atomically for Tracegraph and additionally
batches completed, body-free spans to Phoenix. Streaming has no durable spool:
network errors and queue drops are warnings recorded in the run envelope's
`trace.stream` receipt, while the agent run and local trace continue. Put the
endpoint and project in `syncmill.toml` if desired, but keep credentials in
`PHOENIX_API_KEY`; they are never trace attributes.

### Migrate between memtomem and Mem0

Stop active memory users before migrating. Planning snapshots `shared` and
`agent-runtime:*`, rejects non-empty destination namespaces, and always excludes
ephemeral `board-run:*` data:

```bash
uv run --project "$SYNCMILL_SRC" syncmill memory migration plan --from memtomem --to mem0
# Any installed pair is supported: memtomem, mem0, or langgraph.
uv run --project "$SYNCMILL_SRC" syncmill memory migration plan --from mem0 --to langgraph
uv run --project "$SYNCMILL_SRC" syncmill memory migration apply <migration-id>
uv run --project "$SYNCMILL_SRC" syncmill memory migration verify <migration-id>
uv run --project "$SYNCMILL_SRC" syncmill memory migration rollback <migration-id> --yes
```

Use repeated `--namespace` options on `plan` for an exact custom selection.
Artifacts under `~/.syncmill/migrations/` contain the immutable snapshot and a
resumable, body-free state/receipt trail. The snapshot contains memory text in
plaintext and is never auto-purged; protect the directory and manually remove an
artifact only when its receipt is no longer needed. Apply copies data without
deleting the source or changing configuration; rollback refuses any migrated
target row that has changed since verification. The plan binds the artifact to
the resolved destination path or Qdrant endpoint, collection, and history DB.
Apply, verify, and rollback fail before opening the store when the current
destination differs. Legacy schema-v1 artifacts are deliberately not rebound
from the current configuration: re-plan unapplied work and recover already
applied legacy artifacts manually using the original destination settings.
For an applied v1 artifact, retain the artifact and use the target IDs in
`state.json` as the manual-recovery ledger. Mem0's history DB is deliberately
part of the destination binding because rollback records deletion there after
removing the Qdrant row.

### `syncmill.toml`

Drop a `syncmill.toml` in the directory you run syncmill from for a checked-in project
baseline. Copy [`syncmill.toml.example`](../syncmill.toml.example) to get started:

```toml
agent_priority = ["claude", "codex", "kimi-code"]
default_strategy = "compete"
repo_root = "."

[claude]
timeout_seconds = 600
max_turns = 12

# Per-agent tables use the FIELD name, so kimi-code is the underscored [kimi_code].
[kimi_code]
timeout_seconds = 300

# Quality gates: each entry is one command; all must exit 0 (see Optional features).
[gate]
commands = ["pytest -q", "ruff check ."]

# Circuit breaker: off by default.
[breaker]
enabled = true
failure_threshold = 3

# Capability matching: keyed by the REAL agent id, so kimi-code keeps its hyphen.
[capabilities.agents]
codex = ["python", "refactor"]
kimi-code = ["rust"]

# Persistent work-board: off by default (see "The work-board").
[board]
enabled = true
db_path = "~/.syncmill/board.db"
lease_timeout_seconds = 1800
stall_seconds = 3600
```

A few things to remember:

- **Env wins.** `SYNCMILL_DEFAULT_STRATEGY=route` overrides `default_strategy` in the
  file. Overriding one nested field (e.g. `SYNCMILL_CLAUDE__TIMEOUT_SECONDS`) leaves
  the rest of that `[claude]` table intact (sources deep-merge).
- **Naming split.** Per-agent tables use the *field* name (`[kimi_code]`, underscored),
  but `[capabilities.agents]` is keyed by the *real* agent id (`kimi-code`, with the
  hyphen).
- **Typo protection.** An unknown *top-level* key in the file is rejected with a clear
  error.

---

## Optional features

These are all **off by default** — enable them only when you want them.

### Quality gates

A gate validates an agent's work **after it finishes**, by running commands against
that agent's worktree. The result is accepted only if **every** command exits 0.

```toml
[gate]
commands = ["pytest -q", "ruff check ."]
timeout_seconds = 300
```

Each entry is one command (no shell — chain checks by adding entries, not `&&`). The
gate plugs into every strategy: `route` falls back to the next agent on failure,
`compete` / `council` / `decompose` downrank failers when picking a winner, and
`pipeline` fails the chain.

### Circuit breaker

The breaker remembers **repeated** failures **across runs**. After an agent fails
`failure_threshold` times in a row, its circuit **opens** and it's skipped for
`cooldown_seconds`, then gets one trial run; any success closes it.

```toml
[breaker]
enabled = true
failure_threshold = 3
cooldown_seconds = 300
state_path = "~/.syncmill/breaker.json"
```

State persists to `state_path` (that's how "N in a row" accumulates across separate
`syncmill run` invocations). A gate rejection does **not** trip the breaker — only an
agent that fails to *run* does.

### Capability matching

Tell syncmill which agents are good at what, then hint a task to route a matching agent
first — *over* the priority baseline. It never drops agents, only re-orders them.

```toml
[capabilities.agents]
codex = ["python", "refactor"]
kimi-code = ["rust"]
```

```bash
# This run prefers an agent declaring "python", else falls back to priority order:
uv run --project "$SYNCMILL_SRC" syncmill run "Optimize the data loader" -c python
```

`--capability` is repeatable and is a no-op under `--agent` (one forced agent has no
ordering to influence).

### Difficulty-based model routing (experimental)

Pick a cheaper or stronger **provider-internal model** per run, by task
difficulty. This never changes *which* agents run, their order, capability
matching, strategy topology, gates, breaker, or winner selection — only the
model flag each built-in provider CLI receives (`claude --model`, `codex -m`,
`kimi -m`).

You own the mapping. syncmill ships no model names, never validates a selector
against a provider catalog, and passes the string verbatim as one argv token:

```toml
[model_routing]
mode = "observe"          # off (default) | observe; enforce is reserved

[model_routing.models.low]
claude = "<your-cheap-claude-selector>"
codex = "<your-cheap-codex-selector>"
kimi-code = "<your-cheap-kimi-selector>"   # the real agent id, verbatim

[model_routing.models.high]
claude = "<your-strong-claude-selector>"
```

Three ways to use it:

- **Explicit tier** — `syncmill run --difficulty low "fix the typo"` applies
  the `low` mapping immediately, even with `mode = "off"` (the flag is an
  explicit request). Agents without a mapping for that tier keep their provider
  CLI default (the compact banner reports `N cli-default`); if *no* eligible
  agent has a mapping, the run fails up front — never a silent no-op.
- **Observe** — `mode = "observe"` (or a one-off `--difficulty auto`)
  classifies the prompt with the deterministic `prompt-complexity-v1` heuristic
  and records the recommendation in the run plan (visible in `--dry-run` JSON
  and the durable manifest under `plan.model_routing`) **without passing any
  model flag**. Collect observe data before trusting automatic routing.
  Routing currently applies to **direct `syncmill run` (and its monitored
  form) plus `syncmill continue` only** — board runs, TUI jobs, and rooms
  deliberately don't route yet, even under `mode = "observe"`.
- **Continuation** — `syncmill continue` keeps **selector affinity**: it
  resumes with the selector string the parent run actually used (or no flag,
  for observe/legacy parents), even if your config changed in between. There is
  no `--difficulty` on `continue`; start a new run to change model policy.

The classifier is a conservative structural heuristic (high-risk/broad-scope
markers in English and Korean, target counts, prompt size) — not a quality
model. Unknown languages and pure code default to `medium`. One decision is
made per top-level run; council rounds, decompose subtasks, retries, and route
fallbacks all share it.

Failure semantics worth knowing:

- A syntactically valid but **unavailable selector** is an ordinary provider
  failure: route falls back to the next agent, and the circuit breaker (if
  enabled) records it like any failure — `syncmill breaker reset` recovers a
  tripped agent after you fix the mapping.
- A **corrupt** stored routing block on `continue` fails closed
  (`corrupt model routing metadata`) rather than guessing.
- Rollback is config-only (`mode = "off"` or delete the section), with two
  deliberate survivors: an explicit `--difficulty` flag still applies, and
  continuations of runs that stored a selector still resume with it.

The full contract — classifier rules, decision schema, and the calibration
gates `enforce` must pass — lives in `docs/difficulty-model-routing-plan.md`.

### Team profiles

A **team profile** is a named, savable bundle — an ordered agent subset, an optional
capability map, and an optional strategy — so different kinds of work can run with
different lineups without editing env vars each time.

```toml
[teams.python-team]
agents = ["codex", "claude"]     # ordered subset -> this team's priority order
strategy = "compete"             # optional; omit to inherit default_strategy
goals = ["backend", "api"]       # board goals this team owns

[teams.python-team.capabilities] # optional; omit to keep [capabilities.agents]
codex = ["python"]
```

```bash
uv run --project "$SYNCMILL_SRC" syncmill team list                       # all configured teams
uv run --project "$SYNCMILL_SRC" syncmill team show python-team           # one profile, with agent status
uv run --project "$SYNCMILL_SRC" syncmill run "Fix the API" --team python-team
```

Selecting a team replaces the run's agent lineup (subset **and** order) and, when the
profile sets them, its capabilities and strategy. An explicit `--strategy` flag still
beats the profile. The table name is the verbatim team name (hyphens fine, like
`[capabilities.agents]` keys); the env form is JSON:
`SYNCMILL_TEAMS='{"python-team": {"agents": ["codex", "claude"]}}'`.

With [the work-board](#the-work-board) enabled, a team can **own goals**:
`syncmill board run --goal backend` scopes the claim to that goal *and* automatically
applies the team that declares it (an explicit `--team` wins over the mapping). A goal
may belong to at most one team — two teams declaring the same goal is a hard config
error.

Profiles can also be **written from the CLI** — `team save` creates or replaces a
`[teams.<name>]` table in the project `syncmill.toml`, preserving every comment
elsewhere in the file (the flags define the *full* profile, so re-saving a team
replaces its table wholesale):

```bash
uv run --project "$SYNCMILL_SRC" syncmill team save python-team -a codex -a claude -s compete -g backend -c codex=python,cli
uv run --project "$SYNCMILL_SRC" syncmill team use python-team    # persist default_team — applied when no --team is passed
uv run --project "$SYNCMILL_SRC" syncmill team use --clear        # remove the persisted default
```

`team use` writes a top-level `default_team` key: `run` and `board run` apply that
team automatically when you pass no `--team` (precedence: explicit `--team` >
goal→team mapping > `default_team`). A save that would leave the file invalid — for
example a goal already owned by another team, even one currently masked by an env
override — is rejected before anything is written.

The dashboard has a matching **TEAM pane**: press `t` in `syncmill dashboard` to
list the profiles (with the default marked), `e` to edit or compose one in a form,
and `u` to toggle the highlighted team as the default — the same validation and
comment-preserving writes as `team save` / `team use`.

### Strict Toolgraph preflight (preview)

Toolgraph integration is advisory by default. To enforce the selected Toolgraph
profile before agents start, enable reporting and configure a qualified ambient
tool surface:

```toml
[integration.report]
enabled = true

[integration.toolgraph]
enabled = true
mode = "strict"
profile = "review"
candidate_tools = ["memtomem::mem_search", "syncmill::shared_read"]
unknown_policy = "block"
drift_policy = "warn"
unavailable_policy = "block"

[integration.toolgraph.agent_map]
codex = "codex"
"kimi-code" = "kimi"
```

`profile` chooses Toolgraph's rejection rules; `mode` chooses whether SyncMill
enforces them. Strict checks every participating mapped identity. Unknown
identity/tool and unavailable Toolgraph block by default, while drift warns;
each category can be changed to `block` or `warn`. Invalid or request-mismatched
evidence always blocks. This checks the configured ambient MCP surface, not
individual tool calls, and `graph_generation` is not a freshness timestamp.

Set `mode = "advisory"` or `enabled = false` to roll back immediately.

---

## The work-board

A single `syncmill run` preserves its patch and conversation but does not manage a
queue. The **work-board**
turns syncmill into a system with **durable, structured records of team work** — a
persistent queue of work items, each carrying a goal, task, state, dependencies,
verification evidence, and a handoff note, stored in its own SQLite database
(`~/.syncmill/board.db`). You plan items onto the board; `board run` claims the next
one and orchestrates it through the normal pipeline; humans review and sign off.

Two boundaries keep it safe:

- **Agents never touch the board.** The Supervisor renders the claimed item into the
  prompt and parses the agent's optional status block back out — the agents stay
  simple *prompt in, result out* functions.
- **State lives on the board; knowledge lives in shared memory.** After a board run, a
  sanitized summary of the result is written to `shared` (tagged
  `board-result:<item-id>`), so later runs — board or not — can find it. Like all
  memory writes this is best-effort: if shared memory is unavailable, the board still
  records the outcome and the CLI warns that the summary may not have reached `shared`.
  During the run itself, the agents' working notes go to a private per-run scratch
  area instead of `shared`. The board database records ownership of every scratch
  namespace when it claims an item. Before each `board run`, syncmill best-effort deletes
  the oldest 100 recorded namespaces that no longer hold a live claim, then tombstones
  only the deletions acknowledged by memory; failed deletions remain eligible for retry.
  A live claim is never collected. The exiting run reactivates its ledger row in a
  `finally` block so a late scratch write is retried by a later sweep (a process killed
  between that late write and the finalizer can still leave residue). This keeps cleanup
  independent of backend-wide namespace enumeration.

The board is **off by default**. Enable it with `SYNCMILL_BOARD__ENABLED=true` (or
`[board] enabled = true` in `syncmill.toml`); without that, every `board` command
fails with:

```text
Error: the work-board is disabled — enable it with SYNCMILL_BOARD__ENABLED=true (or [board] enabled=true in syncmill.toml).
```

### Item states

An item moves `planned → running → (blocked | review) → done`:

| State     | Meaning                                                                                                                                  |
|-----------|-------------------------------------------------------------------------------------------------------------------------------------------|
| `planned` | Waiting. *Dispatchable* once every `--depends-on` dependency is `done` and it isn't marked `--human-required`. Items dispatch oldest-first. |
| `running` | Claimed by a `board run`. The claim is **atomic** — two concurrent runners can never grab the same item. You can't set `running` by hand; if the claiming run dies, the **next `board run`** reclaims the item to `planned` once `lease_timeout_seconds` (default 30 min) has passed. |
| `blocked` | The run failed or crashed, or the agent reported it can't proceed. A crash also flags the item human-required.                              |
| `review`  | Completed work or an imported finding is awaiting **human verification**.                                                                  |
| `done`    | Finished **and verified**.                                                                                                                  |

**`review` vs `done` is the load-bearing distinction.** Marking an item `done`
requires verification — a passing quality-gate result recorded during the run, a
`--evidence` reference, or a human `--sign-off`. A bare `board update <id> --state
done` is rejected:

```text
Error: cannot mark item <id> done without verification — attach passing evidence (--evidence) or a human sign-off (--sign-off)
```

Even an agent that *claims* it's done is only recorded as `review` unless the run
itself produced passing verification (e.g. a [quality gate](#quality-gates)).

### Importing Tracegraph review candidates

Tracegraph can export a body-free failure-pattern report containing only a run id,
versioned pattern id, qualified tool key, and normalized-artifact digest. Import it
without installing Tracegraph in SyncMill's environment:

```bash
uv run --project "$SYNCMILL_SRC" syncmill board import-review-candidates candidates.json
uv run --project "$SYNCMILL_SRC" syncmill board list --state review --goal tracegraph-review
uv run --project "$SYNCMILL_SRC" syncmill report show <run-id>  # optional, when the original report is retained
```

Each exact candidate gets a deterministic item id, so importing the same report
again skips the existing item without reopening it. The Tracegraph digest is stored
as **non-passing** evidence: it proves why review is needed, not that review is done.
Imported items are `human-required` and cannot be claimed by `board run`. Resolve one
through the existing human path:

```bash
uv run --project "$SYNCMILL_SRC" syncmill board update <id> --state done --sign-off \
  --clear-human-required --note "dismissed: false positive"
```

The `run_id` is only a join key to a retained SyncMill report. Import does not require
that report, invoke Toolgraph, reinterpret strict P3.1 decisions, or modify unknown,
drift, availability, breaker, capability, manifest, priority, or rerun policy.

### A lifecycle walkthrough

```bash
export SYNCMILL_BOARD__ENABLED=true

# Plan the work (the second item waits for the first).
uv run --project "$SYNCMILL_SRC" syncmill board add "Design the API schema" --goal backend
uv run --project "$SYNCMILL_SRC" syncmill board add "Implement the endpoints" --goal backend --depends-on <id-of-first>

# Record a constraint agents must respect (see Decisions below).
uv run --project "$SYNCMILL_SRC" syncmill board decide "REST only, no GraphQL" --scope backend

# Work the board: claim the next dispatchable item and orchestrate it.
uv run --project "$SYNCMILL_SRC" syncmill board run                 # one item
uv run --project "$SYNCMILL_SRC" syncmill board run --loop          # drain everything dispatchable
uv run --project "$SYNCMILL_SRC" syncmill board run --goal backend  # only this goal (auto-applies its team)

# Inspect: state, evidence, and the append-only audit history.
uv run --project "$SYNCMILL_SRC" syncmill board list --state review
uv run --project "$SYNCMILL_SRC" syncmill board show <id>

# Human review: promote verified work to done.
uv run --project "$SYNCMILL_SRC" syncmill board update <id> --state done --sign-off
uv run --project "$SYNCMILL_SRC" syncmill board update <id> --state done --evidence "https://ci.example.com/run/123"
```

A completed run lands in `review`; a failed one in `blocked`. `board run` accepts the
same `-s`/`--strategy`, `-a`/`--agent`, and `--team` selection as `run`. With a
[quality gate](#quality-gates) configured, a gate that passes during the run counts as
verification — so an agent that reports `"state": "done"` (see below) can land there
directly.

### Decisions — settle it once

`board decide` records a decision the team must **not re-litigate**. Active decisions
are injected **deterministically, in full**, into every board run whose item matches
the scope (global by default, or one goal via `--scope`) — never via fuzzy memory
recall. (The injection is advisory prompt enrichment: if the decisions can't be read,
the run proceeds without them rather than blocking the item.)

```bash
uv run --project "$SYNCMILL_SRC" syncmill board decide "Use uv, never pip"                      # global
uv run --project "$SYNCMILL_SRC" syncmill board decide "REST only, no GraphQL" --scope backend  # one goal
uv run --project "$SYNCMILL_SRC" syncmill board decisions --goal backend                        # what applies here
uv run --project "$SYNCMILL_SRC" syncmill board decide "GraphQL for the gateway only" --supersedes <id>
```

Superseding **retires** the old decision (it stops being injected) — nothing is
deleted, so the history stays auditable.

### How agents report back

The item is rendered into the agent's prompt (goal, decisions, inputs, any handoff
note from prior work, then the task). The agent **may** end its output with one fenced
`syncmill-board` block — every field optional:

````text
```syncmill-board
{"state": "review", "handoff": "<note for whoever picks this up next>",
 "outputs": ["<artifact or file you produced>"], "human_required": false}
```
````

`"state": "blocked"` with a `"blocked_reason"` flags a dead end; `"human_required":
true` calls for a person. Requesting `"done"` only sticks when the run produced
passing verification — otherwise it's recorded as `review`. The block is advisory and
parsed defensively: a malformed block never fails a good run, and it's stripped before
the result summary is shared to memory.

### Team-health stats (`board stats`)

`board stats` computes team-health signals from the items and their audit history:

```text
items: 3
  by state:  done=1  planned=2
  reclaimed (stale-lease):  0
  redispatched (work redone):  0
  false-done (done, unverified):  0
  blocked > 3600s:  0
  human-required > 3600s:  0
  dropped handoffs > 3600s:  0
```

- **reclaimed (stale-lease)** — claims that died and were recovered to `planned`.
- **redispatched** — items claimed more than once, i.e. work that had to be redone.
- **false-done** — `done` items with no passing evidence (an integrity alarm; the done
  gate makes this impossible through normal paths).
- **blocked / human-required / dropped handoffs** — items sitting untouched past
  `stall_seconds` (default 1 h) in a state that needs attention.

### Watching and sharing the board

- **[`dashboard`](#dashboard--live-board-tui)** — a live TUI over the board, with the
  human-path edits on keys.
- **[`serve`](#serve--read-only-mcp-gateway)** — exposes `board_list` / `board_get` /
  `board_stats` read-only to external MCP clients alongside the shared-memory tools.

---

## Common workflows

### Live monitoring (`--monitor`)

Watch a **concurrent** run unfold live in tmux — one pane per agent, auto-attached:

```bash
uv run --project "$SYNCMILL_SRC" syncmill run "Design the caching layer" -s compete --monitor
```

It applies only to the concurrent strategies (`compete`, `council`, `decompose`). For a
sequential strategy, a non-interactive terminal, or a missing tmux, it prints a notice
and runs normally — the monitor is a convenience, never required. Detach with
`Ctrl-b d` to leave the run going in tmux; reattach with the printed `tmux attach -t …`.
Enable it persistently with `SYNCMILL_MONITOR__ENABLED=true`.

> Note: `claude` emits one final result blob (no live stream), so its pane fills at
> completion, while `codex` / `kimi-code` stream as they go.

### Interactive collaboration

You can drive **one agent yourself** (interactively) while syncmill orchestrates the
rest. This is useful because interactive use may be billed differently from programmatic
runs. The two sides meet **only through `shared` memory** — syncmill never invokes your
interactive agent.

```bash
# Terminal A: you drive claude interactively (durable tmux session)
uv run --project "$SYNCMILL_SRC" syncmill session start claude

# Terminal B: syncmill orchestrates the others
uv run --project "$SYNCMILL_SRC" syncmill run "Build the feature" --exclude claude
```

The one rule for the interactive side: **target the `shared` namespace explicitly**,
because the interactive agent's default namespace is cwd-scoped (which syncmill doesn't
read). `session start` prints the exact contract:

- **Write** so syncmill sees it: `mem_add(content=..., namespace="shared")`.
- **Read** syncmill's results: `mem_agent_search(query=..., include_shared=True)`.

Collaboration is via shared *knowledge*, not file merges: your interactive edits land in
your real working tree, while syncmill's agents work in isolated worktrees.

### Running a team off the board

Combine [the work-board](#the-work-board) and [team profiles](#team-profiles) to run a
standing team against a queue of goal-scoped work:

```bash
export SYNCMILL_BOARD__ENABLED=true

# One-time setup: a team that owns the "backend" goal, and a standing decision.
uv run --project "$SYNCMILL_SRC" syncmill team save python-team -a codex -a claude -s compete -g backend
uv run --project "$SYNCMILL_SRC" syncmill board decide "REST only, no GraphQL" --scope backend

# Plan the work.
uv run --project "$SYNCMILL_SRC" syncmill board add "Design the API schema" --goal backend
uv run --project "$SYNCMILL_SRC" syncmill board add "Implement the endpoints" --goal backend --depends-on <first-id>

# Terminal A: watch (and review) live.
uv run --project "$SYNCMILL_SRC" syncmill dashboard

# Terminal B: drain the backend queue — python-team is applied automatically.
uv run --project "$SYNCMILL_SRC" syncmill board run --loop --goal backend
```

Completed items land in `review`; promote them from the dashboard (`e`) or with
`board update <id> --state done --sign-off` once you've verified the work.

### Sharing memory with other tools (`serve`)

If you want an external MCP client (your editor, another Claude Code session) to
*read* syncmill's shared knowledge, run the gateway and register it — see
[`serve`](#serve--read-only-mcp-gateway) above.

---

## Troubleshooting

| Symptom                                                          | What it means / what to do                                                                                          |
|-----------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| `memory unavailable — continuing without shared context`        | The selected backend could not initialize. For memtomem, check `~/.memtomem/config.json`; for mem0, install `syncmill[mem0]`, allow the first model download, and verify Qdrant/embedder dimensions. If local Qdrant says the storage folder is already accessed, stop the other process or configure a Qdrant server URL. The task still ran — use `syncmill status` to inspect. |
| An agent is skipped / "All agents failed:"                       | The binary may be missing, logged out, or disabled. Check `syncmill agents`; ensure the CLI is on `PATH` and authenticated. |
| `circuit breaker state unavailable`                             | The breaker's state file was unreadable; it tracked nothing this run but the task still ran. Check `state_path` permissions. |
| `unknown strategy: …`                                           | A bad `--strategy` or `SYNCMILL_DEFAULT_STRATEGY`. Valid: `route`, `pipeline`, `compete`, `council`, `decompose`.   |
| `--monitor` ran without the live view                           | `--monitor` needs a concurrent strategy, a real terminal (TTY), and tmux installed. The notice says which is missing. |
| `session needs tmux …`                                          | `session` *is* the tmux feature — install tmux (and run `start`/`attach` from a real terminal).                     |
| `the work-board is disabled — enable it with SYNCMILL_BOARD__ENABLED=true …` | Every `board` command (and `dashboard`) needs the board enabled — export the env var or set `[board] enabled = true` in `syncmill.toml`. |
| `cannot mark item … done without verification — …`              | The done gate: `done` means *done and verified*. Pass `--evidence <ref>` or `--sign-off` with `--state done`.        |
| `dashboard needs an interactive terminal (stdout is not a TTY).` | Run `syncmill dashboard` from a real terminal — like `session`, the TUI has no non-interactive fallback.            |
| A prompt starting with `-` is misparsed                         | Put `--` before it: `syncmill run -- "--fix the parser"`.                                                           |
| A retained `~/.syncmill/workspaces/<run-id>` directory          | Artifact persistence failed; inspect the recovery path named in the error before removing it manually.              |

---

*Want the internals?* The architecture, design invariants, and the reasoning behind
every strategy live in [CLAUDE.md](../CLAUDE.md).
