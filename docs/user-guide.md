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

syncmill uses [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync          # installs dependencies and creates the .venv
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

### Check your setup

```bash
uv run syncmill agents     # which agents are enabled, and their timeouts
uv run syncmill status     # is memtomem connected? where is the DB?
```

---

## Your first run

```bash
uv run syncmill run "Add type hints to main.py"
```

That's it. By default this uses the **`route`** strategy: it tries your agents in
priority order (`claude` → `codex` → `kimi-code`) and returns the first one that
succeeds. If the first agent fails or isn't installed, the next one runs.

A few variations to try:

```bash
# Force exactly one agent (no fallback)
uv run syncmill run "Refactor the auth module" -a claude

# Point at a different repository, and give each agent more time
uv run syncmill run "Fix the failing test" -r /path/to/repo -t 600

# Run all agents head-to-head and keep the winner
uv run syncmill run "Design the caching layer" -s compete
```

When the run finishes, syncmill prints the result and which files changed.

---

## Running real agents safely

The test suite is hermetic, but `syncmill run` is not a simulation. It launches the
real agent CLIs from your `PATH`, using your current shell environment and each
tool's logged-in account. Expect normal agent behavior: network access, provider
billing or quota usage, tool execution inside the agent workspace, and whatever
approval model that CLI uses in non-interactive mode.

syncmill still keeps the target repository isolated:

- Each programmatic agent runs in a separate git worktree under
  `../.syncmill-worktrees/` (or a temporary directory when the target is not a git
  repo).
- The final result prints the winning agent's text and `Files changed`, but syncmill
  does **not** merge or copy those changes back into your original working tree. To
  recover the winner's code, pass `--emit-patch <file>`: syncmill writes the winning
  worktree's full diff (new files included) to that file before teardown, and you
  review and apply it yourself with `git apply <file>`.
- Worktrees and their `syncmill/<agent>/<task>` branches are removed at the end of the
  run. If the process crashes, the next `syncmill run` sweeps empty orphaned
  worktrees.
- A `session` is different: it runs the raw interactive agent in your current working
  tree, so edits from `syncmill session start ...` are real local edits.

### A practical first real-agent run

Start with one forced agent and a short timeout. This proves authentication,
subprocess launch, worktree creation, result parsing, memory, and cleanup without
starting several paid/remote agents at once.

```bash
# 1. Confirm syncmill's view of your local setup.
uv run syncmill agents
uv run syncmill status

# 2. Confirm the CLI you plan to use is available and authenticated.
command -v codex
codex --help

# 3. Run exactly one real agent against this repo.
uv run syncmill run \
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
your original checkout. To export that diff as a real patch file before the worktree
is torn down, add `--emit-patch`:

```bash
uv run syncmill run "Add type hints to src/example.py" --emit-patch winner.patch
git apply --stat winner.patch   # review first
git apply winner.patch          # apply when you're satisfied
```

### Escalating from one agent to a real multi-agent run

Once one agent works, move up in this order:

```bash
# Fallback: cheaper than concurrent strategies because it stops at first success.
uv run syncmill run "Fix the smallest failing test and explain the change" -s route

# Head-to-head: all enabled agents run, so expect parallel billing/quota usage.
uv run syncmill run "Propose a robust CLI error-message cleanup" -s compete

# Live view for concurrent strategies, when tmux and an interactive terminal exist.
uv run syncmill run "Compare two approaches for config validation" -s compete --monitor
```

Use a quality gate before trusting a winner for implementation work:

```toml
[gate]
commands = ["pytest -q"]
timeout_seconds = 300
```

Then run:

```bash
uv run syncmill run "Fix the failing tests" --agent codex --timeout 600
```

The gate runs inside the agent's worktree. A passing gate means the isolated result
validated there; it still does not apply the diff to your original checkout.

### Real-agent troubleshooting checklist

| Symptom | Check |
|---------|-------|
| `CLI tool 'codex' not found in PATH` | Run `command -v codex` from the same shell where you run syncmill. |
| The agent fails immediately with auth text | Open that CLI directly and complete login/auth. |
| The run works in `uv run pytest` but not with a real agent | The tests use fakes; inspect the real CLI, account, network, and provider quota. |
| `Files changed` is printed but your repo is unchanged | Expected: changes lived in the isolated worktree and were cleaned up. Rerun with `--emit-patch <file>` to export the winner's diff, then `git apply <file>` yourself. |
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
  **advisory**: if it breaks, the task still runs.

- **Worktree** — a throwaway, isolated git checkout under `../.syncmill-worktrees/`
  where an agent does its work. Your real working tree is never touched by syncmill's
  agents. syncmill cleans these up after each run (and sweeps any leftovers from
  crashes on the next run).

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

> The concurrent strategies (`compete`, `council`, `decompose`) can be watched live
> with [`--monitor`](#live-monitoring---monitor).

---

## Command reference

syncmill's commands: `run`, `agents`, `status`, `serve`, `dashboard`, and the
`session`, `board`, and `team` groups. Run `uv run syncmill --help` or
`uv run syncmill <command> --help` any time; `uv run syncmill --version` prints the
installed version. (`team` is covered in [Team profiles](#team-profiles); `board` and
`dashboard` are summarized below and explained in [The work-board](#the-work-board).)

### `run` — execute a task

```bash
uv run syncmill run "<prompt>" [options]
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
`--` first: `uv run syncmill run -- "--fix the flag parser"`.

### `agents` — list registered agents

```bash
uv run syncmill agents
```

Shows agents in priority order with their enabled/disabled state, timeout, and max
turns.

### `status` — check memory

```bash
uv run syncmill status
```

Shows whether memtomem is connected and the database path.

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
claude mcp add syncmill-shared -- uv run syncmill serve

# HTTP for remote consumers — loopback by default, port 8000:
uv run syncmill serve --http                 # http://127.0.0.1:8000/mcp
uv run syncmill serve --http --port 8765     # custom port
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
uv run syncmill session start [AGENT]   # launch (or reattach) + attach; default = first in priority
uv run syncmill session list            # active sessions + attached/detached state
uv run syncmill session attach TARGET   # reconnect (TARGET = agent id or full session name)
uv run syncmill session stop [TARGET]   # stop one session
uv run syncmill session stop --all      # stop all syncmill sessions
```

Detach with **`Ctrl-b d`** — the agent keeps running; `session attach` reconnects to
the *same* live session. See [Interactive collaboration](#interactive-collaboration)
for how this pairs with `run --exclude`.

### `board` — persistent work-board

Durable, structured records of team work. Requires `SYNCMILL_BOARD__ENABLED=true`
(see [The work-board](#the-work-board) for the concepts and a walkthrough).

```bash
uv run syncmill board add "<task>" [--goal <g>] [--depends-on <id>]... [--human-required]
uv run syncmill board list [--state <s>] [--goal <g>]
uv run syncmill board show <id>
uv run syncmill board run [--id <id> | --loop] [--goal <g>] [-s <strategy>] [-a <agent>] [--team <name>]
uv run syncmill board update <id> [--state <s>] [--evidence <ref>] [--sign-off] [--note <text>] [--clear-human-required]
uv run syncmill board decide "<text>" [--scope <goal>] [--supersedes <id>]
uv run syncmill board decisions [--goal <g>]
uv run syncmill board stats
```

| Subcommand  | What it does                                                                                                                                                                                                        |
|-------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `add`       | Add a `planned` item. `--depends-on` (repeatable) keeps it undispatchable until every dependency is `done`; `--human-required` excludes it from auto-dispatch.                                                       |
| `list`      | List items, optionally filtered by `--state` (`planned`/`running`/`blocked`/`review`/`done`) and/or `--goal`.                                                                                                        |
| `show`      | One item in full: state, evidence, handoff, and the append-only audit history.                                                                                                                                       |
| `run`       | Atomically claim an item (the next dispatchable one, or `--id`) and orchestrate it through the normal pipeline. `--loop` drains every dispatchable item (exits non-zero if any run failed); `--goal` scopes the claim to that goal and auto-applies the [team](#team-profiles) declaring it. `--id` is mutually exclusive with `--loop` and `--goal`. |
| `update`    | The **human path**: transition state, attach evidence, sign off, annotate. `--state done` **requires** `--evidence` or `--sign-off`; `running` can never be set by hand (it's owned by the claim).                    |
| `decide`    | Record a team decision agents must **not re-litigate** — injected in full into every matching board run. `--scope` limits it to one goal (default: global); `--supersedes` retires an earlier decision.              |
| `decisions` | List active (non-superseded) decisions, optionally for one goal (global + goal-scoped).                                                                                                                              |
| `stats`     | Team-health stats: duplicate work, stalls, false-done. See [Team-health stats](#team-health-stats-board-stats).                                                                                                      |

### `dashboard` — live board TUI

A live terminal UI over the work-board: a polling item table, a detail pane, a stats
overlay, and the human-path edits bound to keys. Requires the board enabled **and** an
interactive terminal — there is no fallback (like `session`, the TUI *is* the feature).

```bash
uv run syncmill dashboard                # poll every 2s
uv run syncmill dashboard --refresh 0.5  # faster polling (minimum 0.5s)
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
| `q` | Quit.                                                                     |

Every edit goes through the same rules as `board update` — the dashboard is an
observer/editor over the board only; it never touches agents or memory.

---

## Configuration

Config resolves from three layers, **highest priority first**:

1. **`SYNCMILL_*` environment variables** (nested fields use `__`).
2. **A project `syncmill.toml`** in the current working directory.
3. **Built-in defaults.**

So **env overrides the file, and the file overrides defaults** — every field stays
env-overridable. The CLI flags `--repo` / `--strategy` / `--timeout` override all
three for a single invocation.

> **Discovery is cwd-relative:** `syncmill.toml` is read from the directory you run
> syncmill *in*, not from `--repo`. A missing file just means "defaults + env."

### Environment variables

| Variable                            | Default                       | Meaning                                                   |
|-------------------------------------|-------------------------------|-----------------------------------------------------------|
| `SYNCMILL_AGENT_PRIORITY`           | `["claude","codex","kimi-code"]` | Agent order (JSON list).                                |
| `SYNCMILL_DEFAULT_STRATEGY`         | `route`                       | `route`·`pipeline`·`compete`·`council`·`decompose`.       |
| `SYNCMILL_REPO_ROOT`                | `.`                           | Default target repo.                                      |
| `SYNCMILL_MEMORY__DB_PATH`          | `~/.memtomem/memtomem.db`     | memtomem SQLite path.                                     |
| `SYNCMILL_CLAUDE__ENABLED`          | `true`                        | Enable/disable an agent.                                  |
| `SYNCMILL_CLAUDE__TIMEOUT_SECONDS`  | `300`                         | Per-agent timeout (seconds).                              |
| `SYNCMILL_CLAUDE__MAX_TURNS`        | `10`                          | Per-agent max turns (honored by `claude` only).          |
| `SYNCMILL_COUNCIL__ROUNDS`          | `1`                           | Council critique rounds (≥1).                             |
| `SYNCMILL_DECOMPOSE__MAX_SUBTASKS`  | `8`                           | Max subtasks the decompose planner may produce (≥1).     |
| `SYNCMILL_MONITOR__ENABLED`         | `false`                       | Live tmux monitor for concurrent runs.                   |
| `SYNCMILL_MONITOR__SESSION_PREFIX`  | `syncmill`                    | tmux session-name prefix for monitored runs.             |
| `SYNCMILL_BOARD__ENABLED`           | `false`                       | The persistent [work-board](#the-work-board).            |
| `SYNCMILL_BOARD__DB_PATH`           | `~/.syncmill/board.db`        | Work-board SQLite path.                                  |
| `SYNCMILL_BOARD__LEASE_TIMEOUT_SECONDS` | `1800`                    | How long a claimed (`running`) item may sit before it's reclaimed to `planned` (≥1). |
| `SYNCMILL_BOARD__STALL_SECONDS`     | `3600`                        | Aging window for `board stats` stall signals (≥1; never affects dispatch). |

The per-agent sub-configs (`CLAUDE`, `CODEX`, `KIMI_CODE`) all accept `ENABLED`,
`TIMEOUT_SECONDS`, and `MAX_TURNS`. Note the hyphenated agent id `kimi-code` maps to
the **underscored** env prefix `SYNCMILL_KIMI_CODE__…`.

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
uv run syncmill run "Optimize the data loader" -c python
```

`--capability` is repeatable and is a no-op under `--agent` (one forced agent has no
ordering to influence).

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
uv run syncmill team list                       # all configured teams
uv run syncmill team show python-team           # one profile, with agent status
uv run syncmill run "Fix the API" --team python-team
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
uv run syncmill team save python-team -a codex -a claude -s compete -g backend -c codex=python,cli
uv run syncmill team use python-team    # persist default_team — applied when no --team is passed
uv run syncmill team use --clear        # remove the persisted default
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

---

## The work-board

A single `syncmill run` is stateless: it runs, prints, and forgets. The **work-board**
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
  `board-result:<item-id>`), so later runs — board or not — can find it.

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
| `running` | Claimed by a `board run`. The claim is **atomic** — two concurrent runners can never grab the same item. You can't set `running` by hand; if the claiming run dies, the item is reclaimed to `planned` after `lease_timeout_seconds` (default 30 min). |
| `blocked` | The run failed or crashed, or the agent reported it can't proceed. A crash also flags the item human-required.                              |
| `review`  | The agent finished, but the work is **not yet verified**.                                                                                   |
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

### A lifecycle walkthrough

```bash
export SYNCMILL_BOARD__ENABLED=true

# Plan the work (the second item waits for the first).
uv run syncmill board add "Design the API schema" --goal backend
uv run syncmill board add "Implement the endpoints" --goal backend --depends-on <id-of-first>

# Record a constraint agents must respect (see Decisions below).
uv run syncmill board decide "REST only, no GraphQL" --scope backend

# Work the board: claim the next dispatchable item and orchestrate it.
uv run syncmill board run                 # one item
uv run syncmill board run --loop          # drain everything dispatchable
uv run syncmill board run --goal backend  # only this goal (auto-applies its team)

# Inspect: state, evidence, and the append-only audit history.
uv run syncmill board list --state review
uv run syncmill board show <id>

# Human review: promote verified work to done.
uv run syncmill board update <id> --state done --sign-off
uv run syncmill board update <id> --state done --evidence "https://ci.example.com/run/123"
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
recall, so an agent can't miss one:

```bash
uv run syncmill board decide "Use uv, never pip"                      # global
uv run syncmill board decide "REST only, no GraphQL" --scope backend  # one goal
uv run syncmill board decisions --goal backend                        # what applies here
uv run syncmill board decide "GraphQL for the gateway only" --supersedes <id>
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
uv run syncmill run "Design the caching layer" -s compete --monitor
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
uv run syncmill session start claude

# Terminal B: syncmill orchestrates the others
uv run syncmill run "Build the feature" --exclude claude
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
uv run syncmill team save python-team -a codex -a claude -s compete -g backend
uv run syncmill board decide "REST only, no GraphQL" --scope backend

# Plan the work.
uv run syncmill board add "Design the API schema" --goal backend
uv run syncmill board add "Implement the endpoints" --goal backend --depends-on <first-id>

# Terminal A: watch (and review) live.
uv run syncmill dashboard

# Terminal B: drain the backend queue — python-team is applied automatically.
uv run syncmill board run --loop --goal backend
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
| `memory unavailable — continuing without shared context`        | memtomem couldn't initialize (often the embedding backend in `~/.memtomem/config.json`). The task still ran — fix the backend to restore shared context. Run `syncmill status` to inspect. |
| An agent is skipped / "All agents failed:"                       | The binary may be missing, logged out, or disabled. Check `syncmill agents`; ensure the CLI is on `PATH` and authenticated. |
| `circuit breaker state unavailable`                             | The breaker's state file was unreadable; it tracked nothing this run but the task still ran. Check `state_path` permissions. |
| `unknown strategy: …`                                           | A bad `--strategy` or `SYNCMILL_DEFAULT_STRATEGY`. Valid: `route`, `pipeline`, `compete`, `council`, `decompose`.   |
| `--monitor` ran without the live view                           | `--monitor` needs a concurrent strategy, a real terminal (TTY), and tmux installed. The notice says which is missing. |
| `session needs tmux …`                                          | `session` *is* the tmux feature — install tmux (and run `start`/`attach` from a real terminal).                     |
| `the work-board is disabled — enable it with SYNCMILL_BOARD__ENABLED=true …` | Every `board` command (and `dashboard`) needs the board enabled — export the env var or set `[board] enabled = true` in `syncmill.toml`. |
| `cannot mark item … done without verification — …`              | The done gate: `done` means *done and verified*. Pass `--evidence <ref>` or `--sign-off` with `--state done`.        |
| `dashboard needs an interactive terminal (stdout is not a TTY).` | Run `syncmill dashboard` from a real terminal — like `session`, the TUI has no non-interactive fallback.            |
| A prompt starting with `-` is misparsed                         | Put `--` before it: `syncmill run -- "--fix the parser"`.                                                           |
| Leftover `../.syncmill-worktrees/` directories                  | Orphans from a crashed run; the next `syncmill run` sweeps them automatically.                                       |

---

*Want the internals?* The architecture, design invariants, and the reasoning behind
every strategy live in [CLAUDE.md](../CLAUDE.md).
