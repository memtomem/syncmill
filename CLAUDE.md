# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

syncmill orchestrates multiple CLI coding agents (Claude Code, Gemini CLI, Codex CLI) over a single task, giving them a shared long-term memory (memtomem) so they collaborate and compete. It ships two orchestration strategies — `route` (priority fallback) and `pipeline` (sequential chain) — and the architecture is built to grow more (compete, council).

## Commands

```bash
uv sync                              # install deps + create .venv (uses uv, Python 3.12)
uv run syncmill run "<prompt>"       # run a task through the orchestration pipeline
uv run syncmill run "<prompt>" -a claude   # force one agent, no fallback
uv run syncmill run "<prompt>" -s pipeline   # chain agents as sequential stages (default: route)
uv run syncmill run "<prompt>" --exclude claude   # run the rest; drive claude interactively yourself (repeatable)
uv run syncmill run "<prompt>" -r /path/to/repo -t 600   # target repo + timeout override
uv run syncmill agents               # list registered agents in priority order
uv run syncmill status               # check memtomem connection / db path
uv add <pkg>                         # add a dependency (edits pyproject.toml + uv.lock)
uv sync --group dev                  # install dev deps (pytest)
uv run pytest -q                     # run the hermetic test suite (tests/)
uv run pytest tests/test_agents.py -q -k execute   # one file / one pattern
```

The agents shell out to real `claude`/`gemini`/`codex` binaries, so `syncmill run` requires those CLIs installed and authenticated. The **test suite is fully hermetic** — `tests/conftest.py` provides fake agents/memory/store/registry so tests never touch the real `~/.memtomem` DB, the network, or the agent binaries. When adding behavior, mirror the existing pattern: monkeypatch `syncmill.agents.subprocess.run` (sync path) or `syncmill.agents.asyncio.create_subprocess_exec` (async path) for agent execution, inject a `FakeStore` via `make_memory()` for memory logic, and use the `git_repo` fixture for worktree/git tests.

## Architecture

The whole system is one rule made concrete: **the Supervisor is the only component that touches both agents and memory. Agents are pure functions — prompt in, `TaskResult` out — and know nothing about memtomem, each other, or the Supervisor.** This isolation is enforced at the type level: `AgentRunner.execute`/`aexecute`(prompt, workdir, timeout) receive only a `str` prompt. Never thread a `Task`, memory handle, or another agent's result into an agent.

Request flow (`CLI → Supervisor.run`, in `supervisor.py`):

1. `cleanup_orphaned_worktrees` sweeps leftovers from crashed runs.
2. `Supervisor` builds an `AgentRegistry` (priority-ordered, enabled agents) and a `SyncmillMemory`.
3. Per run: build a `WorktreePool` (the strategy acquires worktrees from it), start a memtomem task session.
4. The selected **`Strategy`** executes: it searches memory for context, **enriches the prompt** with it, acquires worktree(s), then runs agents — recording per-worktree `files_changed` on the result it returns.
5. On success: the Supervisor writes that result's summary to the `shared` memory namespace.
6. `finally`: end the memory session, then `pool.cleanup_all()` removes every worktree + branch the run acquired.

### The two extension points

- **Agents** (`agents.py`): subclass `CLIAgent`, implement `_build_command`, and override `_parse_output` / `_is_error_output` for that CLI's output format — you get both the sync `execute` and the async `aexecute` paths for free from the base class. Register the class in `_AGENT_CONSTRUCTORS`, add a matching `AgentConfig` field in `config.py`, and add the id to `agent_priority`. Each agent already runs its CLI in JSON mode and extracts the response text (Claude `result` key, Gemini `modelResponse`, Codex JSONL `agent_message` items).
- **Strategies** (`supervisor.py`): implement the `Strategy` Protocol and register it in `_STRATEGIES`, then select it via `default_strategy` (or the `--strategy/-s` CLI flag). `_make_strategy` raises on an unknown name — mirror that hard-fail rather than silently defaulting. Two strategies exist: `RouteStrategy` (try agents in priority order, fall back on failure, aggregate errors if all fail) and `PipelineStrategy` (run agents as sequential stages — each reads prior stages' results from `shared` and writes its own there, fail-fast on a broken stage; the prompt enricher `_enrich_prompt` is shared by both). Each strategy is handed a `WorktreePool` and owns its worktree topology — Route acquires one isolated worktree per agent, Pipeline a single shared one — and sets per-worktree `files_changed` (via `_collect_files_changed`) on the result it returns. CompeteStrategy/CouncilStrategy are the planned v0.2–v0.3 additions — the registry, the prompt-only agent contract, the async `aexecute` path, and per-agent worktrees exist to make them drop-in.

### Memory (`memory.py`)

`SyncmillMemory` wraps memtomem's `MemtomemStore` with syncmill conventions: namespace `agent-runtime:{agent_id}` is private per agent, `shared` is the cross-agent knowledge base (both derived from `memtomem.constants` — `AGENT_NAMESPACE_PREFIX`/`SHARED_NAMESPACE` — so they track memtomem's scheme; matches the 0.2.x MCP multi-agent tools). The Supervisor is the **sole reader/writer** — there is intentionally no MCP sharing between agents. memtomem is async, and so is the agents' `aexecute` path; syncmill's orchestration (Supervisor, route/pipeline strategies) is sync. `run_async()` bridges async into that sync world by **reusing a module-level event loop** — do not switch to `asyncio.run()`, which closes the loop and breaks memtomem's later embedding calls (a fixed bug); `close_event_loop()` releases it at CLI shutdown.

**Memory is advisory/best-effort.** Every `SyncmillMemory` method catches its own exceptions, logs a warning, sets `degraded`/`last_error`, and returns a safe default. A broken or misconfigured memtomem (e.g. an unsupported embedding provider) must never abort a task — the agent still runs, just without shared context. Preserve this when editing memory or the Supervisor; the CLI surfaces `supervisor.memory.degraded` as a yellow notice. Note memtomem applies `~/.memtomem/config.json` *last* (inside `create_components`), overriding both env vars and programmatic `config_overrides` — so the embedding provider is governed by that global file, and syncmill shares `~/.memtomem/memtomem.db` by default.

### Interactive collaboration (human drives one agent, syncmill runs the rest)

Programmatic `claude` runs may be billed separately from interactive use. To keep Claude **interactive** while syncmill orchestrates the others: exclude claude from the run (`run … --exclude claude`, repeatable; or persistently via `SYNCMILL_CLAUDE__ENABLED=false`) and drive Claude yourself in the terminal. The two sides collaborate **only through the `shared` namespace** — never by syncmill invoking claude, so the agent-isolation invariant is untouched (the interactive session is a separate process, not a syncmill agent).

This needs zero extra wiring: syncmill's `SyncmillMemory` and memtomem's MCP multi-agent tools use the **same `shared` literal on the same `~/.memtomem/memtomem.db`** (exact-match namespaces). The one rule — the interactive session must target `shared` **explicitly**, because its default namespace is cwd-scoped (`claude:<cwd>`), which syncmill does not read.

- Interactive session **writes** (so syncmill sees it): `mem_add(content=..., namespace="shared")`, or `mem_agent_share(chunk_id=..., target="shared")` to promote an existing chunk.
- Interactive session **reads** syncmill's results: `mem_agent_search(query=..., include_shared=True)` or `mem_search(query=..., namespace="shared")`.
- syncmill side is unchanged: `share_result` writes the winner to `shared`; `search_context` reads `shared` to enrich the programmatic agents' prompts.

Collaboration is via shared *knowledge*, not file merges: the interactive session edits your real working tree while syncmill agents edit isolated worktrees, so syncmill's diff never sees the interactive edits. Head-to-head "compete" with the human awaits the planned CompeteStrategy.

### Config (`config.py`)

`SyncmillConfig` is pydantic-settings. Every field is overridable via `SYNCMILL_*` env vars (nested delimiter `__`, e.g. `SYNCMILL_CLAUDE__TIMEOUT_SECONDS`).

### Worktrees (`worktree.py`)

Each worktree is `../.syncmill-worktrees/{agent}-{taskid}` on branch `syncmill/{agent}/{taskid}`, with a tempdir fallback for non-git repos — living *beside* `repo_root`, not inside it. A run's worktrees are managed by a **`WorktreePool`**: the Supervisor owns one per run and calls `cleanup_all()` in `finally` (each removal independent, so one stuck worktree can't strand the rest), while the *strategy* decides topology — `RouteStrategy` (and the future concurrent strategies) `acquire` one isolated worktree per agent so parallel agents never clobber each other and each result's `files_changed` is its own; `PipelineStrategy` acquires a single shared one so stages hand files down the chain. `cleanup_orphaned_worktrees` still sweeps any leftovers from crashes.

## Design invariants (the maintainer cares about these)

- **Don't pre-abstract.** The package is intentionally flat (8 files under `src/syncmill/`). Don't split into subpackages until the code demands it.
- **memtomem is a PyPI dependency** — use it, never reimplement memory. **autocli** is a *pattern* reference only (e.g. the ANSI stripper in `agents.py`), **not a dependency** — don't add it.
- **No `health_check` on agents.** Run, and fall back on failure. Don't add liveness probes to the agent contract.
- **Sync-first orchestration.** `CLIAgent` exposes *both* a sync `execute` (`subprocess.run`) and an async `aexecute` twin (`asyncio.create_subprocess_exec`, plus the `aexecute_all` fan-out helper) — but the Supervisor and the route/pipeline strategies still call only `execute` and stay fully sync (`run_async` bridges memtomem and, when a concurrent strategy needs it, `aexecute`). `aexecute` is the foundation the *concurrent* strategies (compete/council) will await; they also need per-agent worktrees before fanning into one tree. Don't async-ify the Supervisor or the sequential strategies until a concurrent strategy actually needs it. The two paths share success/failure classification via `_build_result`/`_failure`, so they can't drift; the one deliberate divergence is that a timed-out `aexecute` reports empty output (asyncio discards a killed child's buffer) where `execute` keeps `TimeoutExpired.stdout`.
- When adding an agent, **verify the real CLI's JSON output format with an actual run first** — CLIs return errors in non-obvious ways (e.g. Claude can exit 0 while returning `{"is_error": true}`, which `ClaudeAgent._is_error_output` specifically catches).
