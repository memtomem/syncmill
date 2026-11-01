# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

syncmill orchestrates multiple CLI coding agents (Claude Code, Codex CLI, Kimi Code) over a single task, giving them pluggable shared long-term memory (memtomem by default, optional mem0 or LangGraph SQLite) so they collaborate and compete. It ships five orchestration strategies — `route` (priority fallback), `pipeline` (sequential chain), `compete` (concurrent head-to-head), `council` (multi-round propose → critique → synthesize deliberation), and `decompose` (split the task into subtasks, run them across agents concurrently, then synthesize) — and the architecture is built to grow more.

## Commands

```bash
uv sync                              # install deps + create .venv (uses uv, Python 3.12)
uv run syncmill start --trial         # offline walkthrough (trial.py, #171): fake codex/kimi in a temp sandbox, room round → compare → diff → apply --verify; --keep keeps the folder; bare `start` menu (no TTY → explains instead of waiting)
uv run syncmill start --task "<task>" [-a AGENT] [--dry-run | --yes]   # project path: agent_readiness → confirm screen (start.py render_start_plan; gate discovery reads files only, saved via teamfile.set_gate_commands after a yes) → ctx.invoke(run) → diff/apply offer; run leaves its id in ctx.meta[_START_RUN_META]; --compare / 2×-a = one Room round via RoomCoordinator → start.room_candidates over comparison_to_dict → room diff/select/apply (rooms off → ask, teamfile.set_rooms_enabled, else single); repo root only
uv run syncmill init                 # initialize a project
# Claude Code plugin: marketplace at .claude-plugin/marketplace.json, plugin at plugins/syncmill-claude-plugin (one user-invoked skill, /syncmill:trial → pinned `uvx --from syncmill==<pyproject version> syncmill start --trial`); tests/test_claude_plugin.py fails until a release-prep bump also updates that pin; check with `claude plugin validate .` and `claude plugin validate ./plugins/syncmill-claude-plugin`
uv run syncmill doctor               # validate repo, agents, and stores
uv run syncmill run --dry-run "<prompt>"   # print bounded RunPlan; no agent starts
uv run syncmill run "<prompt>"       # run a task through the orchestration pipeline
uv run syncmill run "<prompt>" -a claude   # force one agent, no fallback
uv run syncmill runs list            # list durable winners
uv run syncmill diff <run-id>        # review a retained winner
uv run syncmill apply <run-id>       # explicitly apply a retained winner
uv run syncmill apply <run-id> --detach   # durable detached apply worker (survives this process; inspect via `syncmill tui`)
uv run syncmill recover [<run-id>]   # reconcile a standalone artifact apply interrupted by a crash (completed/not-applied/manual-recovery); room applies use `room recover`
uv run syncmill continue <run-id> "<prompt>"   # resume winner's native Claude/Codex/Kimi session
uv run syncmill room compare <room-id> [--round N | --all-rounds] [--json]   # read-only side-by-side of one round's candidates on recorded evidence (room_compare.py, #172)
uv run syncmill room export <room-id> [--round N | --all-rounds] [--format markdown|json] [--output FILE]   # body-free review summary of the same read model: json == `room compare --json`; markdown fences all stored free text (render_review_markdown); --output is O_EXCL 0600, no overwrite
uv run syncmill room export <room-id> --include patch:<cand> [--include output:|gate-output:<cand>|prompt:<round> ...] (--preview | --confirm TOKEN)   # opt-in raw content (room_raw.py, #189): preview prints sizes/digests/first lines + a token over every item descriptor; confirm re-collects and writes only on an equal token; reads bound to the comparison's room/round/digests; gate-output accepted only when room_compare._gate_verdict accepts the record; markdown = escaped display rendering, json adds `raw_content` (base64, ensure_ascii); never redacted
# room rounds also record each member's conversation (session cold/resume/fork, checkpoint candidate ids + omitted, memory delivery) in rounds.conversation_json, written once under the lease before the fan-out (rooms.round_conversation_status binds it to SQLite rows); compare shows per-candidate facts and round counts only
# room rounds record their effective starting tree + gate settings (v2 adds the gate's env_allowlist: null = inherited, [] distinct) in rounds.provenance_json (rooms.round_provenance_status, writer and readers share it, readers accept v1+v2; NULL = legacy); compare/export/TUI show them as starting tree / gate settings beside the report-derived gate profile, whose JSON fields are unchanged; a v1 round's environment reads not recorded and never groups as same settings; `room apply --verify` still runs the current `[gate]` and prints a `verify:` notice (`rooms.verify_settings_notice`) unless the round's v2 record equals it
uv run syncmill room select <room-id> <cand> [--expected-revision N --expected-digest SHA256 --rationale TEXT]   # verifies the artifact, records a bound selection (rooms.selection_json); apply refuses an inconsistent record
uv run syncmill room judge <room-id> accepted|rejected [--expected-digest SHA256 --rationale TEXT]   # #174: append-only human verdict on the APPLIED candidate (room_judgments, bound to candidate + patch digest + applied run; never inferred from select/apply, never touches memory, no state/revision change); rooms.room_outcome_status is the one read model (not_selected / selected_not_applied / applied_unreviewed / accepted / rejected / unknown — a damaged latest record is unknown, never an older verdict) shown by compare/export as room.outcome
uv run syncmill room promote <room-id> --lesson TEXT [--scope GOAL --supersedes ID]   # #174 PR 2: needs the board; only a judged room (accepted/rejected); a board decision bound to project_id with provenance_json (room/judgment/verdict/candidate/digest/run) and a unique source_key (retry returns the existing decision, never reactivates a superseded one); `run` shows then injects Board.project_lessons (project-wide scope only; board.select_lessons budget; --skip-lesson/--no-lessons, forwarded in _inner_argv) via Supervisor.run(lessons=) AFTER routing classified the raw prompt; board items get unbound + own-project decisions; room rounds do not get lessons yet
uv run syncmill conversations list
uv run syncmill conversations show <run-id>
uv run syncmill conversations close <run-id>
uv run syncmill run "<prompt>" --emit-patch winner.patch   # optional compatibility copy; artifact is always retained
uv run syncmill run "<prompt>" -s pipeline   # chain agents as sequential stages (default: route)
uv run syncmill run "<prompt>" -s compete   # run all agents concurrently; selection policy keeps the winner
uv run syncmill run "<prompt>" -s council   # agents propose → critique (×SYNCMILL_COUNCIL__ROUNDS) → synthesize, deliberating via shared memory
uv run syncmill run "<prompt>" -s decompose   # planner splits into ≤SYNCMILL_DECOMPOSE__MAX_SUBTASKS subtasks, run across agents concurrently, then synthesize
uv run syncmill run "<prompt>" --exclude claude   # run the rest; drive claude interactively yourself (repeatable)
uv run syncmill run "<prompt>" -c python -c refactor   # capability hints — prefer an agent declaring them (needs SYNCMILL_CAPABILITIES__AGENTS), else priority order
uv run syncmill run "<prompt>" -r /path/to/repo -t 600   # target repo + timeout override
uv run syncmill run "<prompt>" -s compete --monitor   # live tmux monitor of a concurrent run (one pane per agent, auto-attached)
uv run syncmill session start [agent]   # durable interactive agent in a dedicated tmux session (survives detach; reattach by name)
uv run syncmill session list
uv run syncmill session attach <agent-or-name>
uv run syncmill session stop <agent-or-name>   # or: syncmill session stop --all
SYNCMILL_BOARD__ENABLED=true uv run syncmill board add "<task>" --goal "<g>"   # persistent work-board: add a planned item (disabled by default)
uv run syncmill board list [--state STATE] [--goal GOAL]
uv run syncmill board show <id>       # view one item (state, evidence, audit history)
uv run syncmill board run [--id --loop --strategy --agent --team --goal]   # claim the next dispatchable item (or --id), orchestrate it, record the outcome; --goal scopes the claim to one goal (a team declaring it auto-applies)
uv run syncmill board update <id> [--state --evidence --sign-off --clear-human-required]   # human path; `done` REQUIRES evidence/sign-off (bare --state done rejected)
uv run syncmill board decide "<text>" [--scope <goal> --supersedes <id>]   # Phase 2: record a never-re-litigate decision (injected into matching board runs)
uv run syncmill board decisions [--goal GOAL]
uv run syncmill board stats           # team-health stats (dup-work, stalls, false-done)
# Phase 2 also extends `serve` with read-only board_list/board_get/board_stats MCP tools (6-tool surface)
SYNCMILL_BOARD__ENABLED=true uv run syncmill dashboard [--refresh 2.0]   # Phase 3: live Textual TUI over the board (view + human-path edits; t TEAM / p REPORT; needs a TTY)
uv run syncmill tui [--refresh 2.0] [--tab auto|board|runs|reports|teams|status|sessions|rooms]   # seven-tab command center; --tab auto (default) opens the first open surface (Rooms → Board → Runs → Status); Runs splits into Operations/Artifacts (v); limited mode without board; q detaches durable worker
uv run syncmill run "<prompt>" --team python-team   # Phase 4: run with a named team profile ([teams.<name>] in syncmill.toml / SYNCMILL_TEAMS JSON)
uv run syncmill team list
uv run syncmill team show <name>      # inspect configured team profiles (read-only)
uv run syncmill team save <name> -a codex -a claude [-s compete -g backend -c codex=python,cli]   # P3 TEAM pane: create/replace [teams.<name>] in syncmill.toml (tomlkit, comment-preserving; the flags define the FULL profile)
uv run syncmill team use <name>       # persist default_team in syncmill.toml
uv run syncmill team use --clear      # remove it; works even when config is unloadable
SYNCMILL_INTEGRATION__REPORT__ENABLED=true uv run syncmill run "<prompt>"   # also record the run-envelope join artifact to ~/.syncmill/runs/{run_id}/ (S3; default off)
uv run syncmill report list
uv run syncmill report show <run_id>  # read-only viewers; work while recording is off
SYNCMILL_INTEGRATION__TRACE__ENABLED=true uv run syncmill run "<prompt>"   # also export an OTLP-JSON trace to ~/.syncmill/runs/{run_id}/trace.otlp.json (S3′; default off; tracegraph ingests it as-is)
SYNCMILL_INTEGRATION__TOOLGRAPH__ENABLED=true uv run syncmill run "<prompt>"   # also run advisory `toolgraph preflight` once for the lead agent, recording the verdict in the envelope preflight block (S5; default off; needs the toolgraph CLI)
uv run syncmill agents               # list registered agents in priority order
uv run syncmill status               # check the selected memory backend
uv run syncmill serve                # read-only MCP gateway over `shared` (stdio; --http/--host/--port)
uv run syncmill memory migration plan --from memtomem --to mem0   # source-retaining snapshot; apply/verify/rollback by id
SYNCMILL_TEST_MEMORY_MIGRATION_REAL=1 uv run pytest -q tests/test_memory_migration_integration.py  # required before changing migration adapters
SYNCMILL_TEST_MEMTOMEM_REAL=1 uv run pytest -q tests/test_memory_backend_memtomem_integration.py # required before changing MemtomemBackend's replicated add flow or the memtomem pin
SYNCMILL_TEST_LANGGRAPH_REAL=1 uv run pytest -q tests/test_memory_backend_langgraph_integration.py # required before changing LangGraph SDK pins/API calls
uv add <pkg>                         # add a dependency (edits pyproject.toml + uv.lock)
uv sync --group dev                  # install dev deps (pytest)
uv run pytest -q                     # run the hermetic test suite (tests/)
uv run pytest tests/test_agents.py -q -k execute   # one file / one pattern
```

SyncMill execution supports macOS and Linux. Windows commands are refused before
state/process side effects; root `--help` and `--version` remain available. Detached
workers use POSIX process-group and file-locking primitives.

The agents shell out to real `claude`/`codex`/`kimi` binaries (note the `kimi-code` agent's binary is `kimi`), so `syncmill run` requires those CLIs installed and authenticated. The **test suite is fully hermetic** — `tests/conftest.py` provides fake agents/memory backends/registries so tests never touch the real `~/.memtomem` DB, the network, or the agent binaries. When adding behavior, mirror the existing pattern: monkeypatch `syncmill.agents._sync_run` (sync path — the Popen-backed runner `execute` calls; plain `subprocess.run` is no longer on that path) or `syncmill.agents.asyncio.create_subprocess_exec` (async path) for agent execution, inject a `FakeBackend` via `make_memory()` for memory logic, use the low-level fake memtomem store in `test_memory_backend.py` for adapter-private-boundary tests, inject a `FakeGate` for quality-gate behavior (and exercise the real `CommandGate` with harmless `true`/`false`/`python` commands), and use the `git_repo` fixture for worktree/git tests.

## Architecture

The whole system is one rule made concrete: **the Supervisor is the only component that touches both agents and memory. Agents are pure functions — prompt in, `TaskResult` out — and know nothing about the selected backend, each other, or the Supervisor.** This isolation is enforced at the type level: `AgentRunner.execute`/`aexecute`(prompt, workdir, timeout) receive only a `str` prompt. Never thread a `Task`, memory handle, or another agent's result into an agent.

Request flow (`CLI → Supervisor.run`, in `supervisor.py`):

1. Resolve a strict Git root. `capture_repository_snapshot` blocks unsafe states and
   captures HEAD + dirty tracked + untracked (ignored excluded) in a private control
   clone without changing the source branch, object database or staged index
   contents. `repository_fingerprint` (snapshot capture, room rounds, every apply,
   `room compare`) does refresh the source index's stat cache, as `git status` does
   (#183).
2. `Supervisor` builds an `AgentRegistry` and project-scoped memory for initialized projects.
3. Per run: build a snapshot-backed `WorktreePool`; every strategy worktree starts
   at the same synthetic snapshot commit. A pending continuation seeds the parent patch.
4. Re-order candidates by capability, execute the strategy, and keep provider-native
   sessions affine to `(agent, worktree)` across phases.
5. Before cleanup, `ArtifactStore` atomically writes the manifest, result, and
   `winner.patch`. Persistence failure changes the run to failed and retains the recovery workspace.
6. On success share the result to memory. In `finally`, finalize observability and
   remove private worktrees. Source changes happen only through explicit `syncmill apply`.

### Artifact filesystem boundary (#166)

`ArtifactStore` reaches run artifacts through descriptor-relative `ArtifactIO` operations beneath the canonical configured state root. Full design: [docs/architecture/artifact-boundary.md](docs/architecture/artifact-boundary.md) — read it before changing artifact reads/writes, receipts, locks, listing, apply/rollback or pruning.

Rules that must hold:

- Project/run ids are single path components; directories below the root are not symlinks; artifact files are regular, singly linked files. Unsafe access raises `ArtifactError`. An explicit manifest `run_id`/`project_id` must match its location (missing legacy fields stay readable, no read-time writes).
- Read result sidecars with `read_result`, never by reopening `RunArtifact.path`. Apply/check/rollback pass the same digest-verified patch bytes to Git over stdin.
- `list_detailed` reports each omitted directory or symlink entry as a `SkippedRun` (regular files such as `.apply.lock` are ignored, not skipped) (raw name + fixed reason category; render with `printable_name`) so listings never show a corrupt-only store as healthy-empty; skipped entries are never repaired or pruned. A runs root replaced mid-enumeration (`ESTALE`) raises.
- The state root and its ancestors are trusted: this blocks traversal and symlink redirection, not a same-credential actor.

### The extension points

Pluggable seams: memory backends, agents, strategies, quality gates and the circuit breaker, plus capability matching and model routing layered on top. Full design: [docs/architecture/extension-points.md](docs/architecture/extension-points.md) — read the relevant part before adding or changing an agent, strategy, backend, gate, breaker, matcher or routing behavior.

**Memory backends** (`memory_backend.py`)
- `MemoryBackend` is the async, raise-freely storage contract; `SyncmillMemory` owns every best-effort/degradation path. Construct via `build_backend(config.memory)`, which refuses non-verbatim storage.
- `MemtomemBackend` is the default and the only runtime module allowed to reach memtomem privates (`memory_migration.py` is the documented maintenance exception). Optional `Mem0Backend` (`syncmill[mem0]`, Qdrant only, for exact count/delete) and `LangGraphBackend` (`syncmill[langgraph]`, `AsyncSqliteStore`): local FastEmbed defaults, no sessions, verbatim storage.
- The contract carries `search_multi`, `add_returning_ids`, `share_chunk`, `register_namespace`; `BackendCapabilities.native_multi_namespace_search`/`share_provenance_links` mark native vs adapter fallback. Tests fake each SDK boundary; real smokes are opt-in.

**Agents** (`agents.py`) — adding one:
1. Subclass `CLIAgent`, implement `_build_command(prompt)`, override `_parse_output` and `_is_error_output` for that CLI's output. The base class gives both `execute` and `aexecute`.
2. Register the class in `_AGENT_CONSTRUCTORS`.
3. Add an `AgentConfig` field in `config.py`; a hyphenated id uses the underscore field name (`kimi-code` → `kimi_code`, mapped by `SyncmillConfig.agent_config`).
4. Add the id to `agent_priority`.
5. Verify the real CLI's output with an actual run first. Current formats: Claude takes the `result` key of its JSON and can exit 0 with `{"is_error": true}` (`ClaudeAgent._is_error_output`); Codex joins `agent_message` items from JSONL; Kimi Code (binary `kimi`) keeps `role:"assistant"` text lines from `stream-json` JSONL. Codex and Kimi signal failure by non-zero exit; their `_is_error_output` only catches a zero-exit payload that decodes to no JSONL events. A failed result may carry `TaskResult.failure_kind` (#114): `_failure` callers tag timeout/launch, and `_classify_failure(stdout, stderr)` (default `None`; `_build_result` calls it only on failure, guarded) names provider refusals — only Claude overrides it, from `api_error_status`, an `API Error: NNN` result prefix or an error event's `error.type`, scanning like `_is_error_output`. Never classify from prose or an unverified CLI shape; `_aggregate_failure` keeps a kind only when every attempt shares it, and the artifact manifest/result.json and `monitor._result_to_json` record it.

**Strategies** (`strategies/`)
- Implement the `Strategy` Protocol and register it in `_STRATEGIES` (`strategies/__init__.py`); `_make_strategy` hard-fails on an unknown name — never default silently.
- `strategies/base.py` holds the Protocol and every cross-strategy helper (`_enrich_prompt`, `_gate_accepts`, `_breaker_*`, failure builders, `_select_winner`, `_collect_files_changed` → `worktree.capture_changes`); diff collection stays below the strategy modules to keep imports acyclic. One module per strategy.
- Each strategy gets a `WorktreePool` and owns its topology (Route/Compete/Council: one worktree per agent, Council reusing it across phases; Pipeline: one shared; Decompose: planner + each subtask + each synthesizer) and sets `files_changed`/`patch` via `_collect_files_changed` (mutates the result for a COMPLETED git worktree; the underlying `capture_changes` returns `CapturedChanges` or raises `PatchCaptureError`).
- Route = priority fallback; Pipeline = sequential stages, each given every earlier stage's result, fail-fast; Compete = concurrent, converge via `_select_winner`; Council = propose → critique ×`council.rounds` → synthesize, each critique round and synthesis given every proposal plus the latest round's critiques; Decompose = `runnable[0]` plans via sync `execute` (a bad plan degrades to one whole-task subtask), subtasks round-robin concurrently via `aexecute_each`, every runnable agent synthesizes in a fresh worktree from every subtask's result; capped by `decompose.max_subtasks`.
- **Intra-run handoff is total (#125).** A later phase's prompt carries the earlier results the strategy already holds — `_handoff_item` per result, rendered by `_merge_handoff` and placed by `_enrich_prompt(prompt, context, handoff)` between the recalled context and the task. Every item keeps its label (a result that did not complete is a visible gap); only bodies are shortened, water-filled within `_HANDOFF_MAX_BYTES`, and the budget shrinks so the composed prompt stays under `_PROMPT_MAX_BYTES` (argv). Never searched back from memory, so it survives a dead backend; the `shared` writes and tag-filtered recall stay for cross-run value and human notes. Results are never merged as trees.
- `_select_winner` under `config.selection.mode`: `verified` (default; first gate-passing COMPLETED in priority order), `priority` (checks only the first completed), `judge` (ranks gate-passing candidates, falls back to verified on failure). Never by wall-clock.
- No strategy applies the winner's diff; capability re-ordering happens once in the Supervisor, never per strategy. Tests stub `syncmill.strategies.base.capture_changes`; capture/index tests use real Git repos.

**Quality gates** (`gates.py`)
- `QualityGate.evaluate(worktree) -> GateReport` runs after the agent finishes, on its worktree — never a pre-run probe. `CommandGate` runs each command via `shlex.split` without a shell (add entries, not `&&`), passes iff all exit 0, in the agents' isolated environment (no ambient `GIT_DIR`/`GIT_CONFIG_PARAMETERS`/hooks). By default a gate is no wider than the agent it judges: a set `gate.env_allowlist` narrows it (and `apply --verify`) to the workflow common variables + those exact names, applied after isolation; a workflow gate is always narrowed (unset = `[]`, plus the final agent's `workflow_env_allowlist`), and so is `apply --verify` of its artifact (final agent from the manifest's `plan.final_agent`, decided under the apply lock). `build_gate` → `None` when no commands are configured (the default). `--verify` with no commands is refused (`artifacts.VERIFY_NEEDS_GATE`) before any apply-state change: in the CLI (and before a detached worker launches), in `RoomCoordinator.apply` before `begin_apply`, and in `ArtifactStore._apply_locked` as the backstop (`verify_commands=[]` refuses; `None` = not requested).
- Strategy layer only; agents never see it. `_gate_accepts`: fail → FAILED with `result.gate`. Route falls back; Pipeline gates only the final cumulative tree; Compete downranks in `_select_winner`; Council and Decompose gate only synthesis. A gate-rejected result is never shared.

**Circuit breaker** (`breaker.py`)
- Consecutive failures per agent across runs: OPEN after `failure_threshold` for `cooldown_seconds`, then half-open (one trial), any success closes; states derive from `(failures, opened_at)`. Persisted atomically to `state_path`. `build_breaker` → `None` when disabled (the default; no filesystem).
- Reactive from recorded outcomes, never a probe. Built once by the Supervisor, injected via `Strategy.execute(..., breaker=)`. `_breaker_runnable` drops OPEN agents (all OPEN → `_breaker_all_open_failure`); `_breaker_record` records the RAW pre-gate status, so a gate rejection never trips it.
- One record per agent per run: Council records only at propose; Decompose at each agent's first execution (a `recorded` set).
- A degraded (unreadable) state file is permissive with a yellow notice; an OPEN circuit within cooldown is skipped on purpose.

**Capability matching** (`matcher.py`)
- Agents declare tokens in `SYNCMILL_CAPABILITIES__AGENTS` (keyed by real agent id); the hint is `Task.tags` from `-c`. `DeclarativeMatcher.order` scores by intersection with the normalized hint and stable-sorts descending — a permutation that never drops; no hint or no match = priority order. Pure config, never a probe.
- `build_matcher` → `None` when no agent declares a capability; an unknown agent id only warns; tokens are never validated.
- Applied once in `Supervisor.run` (`_matched_order`, non-forced path only), before strategy dispatch and before the breaker filter — not threaded into `Strategy.execute`. The CLI rebuilds a display-only matcher for the banner.

**Model routing** (`model_routing.py`) — read `docs/difficulty-model-routing-plan.md` first
- Selects only the provider-internal model, never lineup, order or strategy. A fresh run with `mode="off"`, no `--difficulty` and no inherited routing classifies nothing and opens no scope or plan key (a continuation can still inherit a parent's selector). `[model_routing]` is `extra="forbid"`; `mode="enforce"` is rejected (reserved); tier → provider → selector keyed by real agent ids; selectors are shape-validated only.
- One immutable `ModelRoutingDecision` per top-level run, computed in `Supervisor.run` after `_matched_order`, projected onto the eligible lineup. `run_item` passes `enable_routing=False`; a continuation never classifies — it inherits via `inherit_decision` (corrupt metadata fails closed) and the stored selector wins. A classifier error degrades in observe; `AllUnmappedError` fails closed.
- Execution: `model_routing_scope(selectors)` sets `agents._MODEL_SELECTORS` (selectors only, so `agents` never imports `model_routing`); one scope around strategy execution, none without a decision. Claude appends `--model`, Kimi's `-m` leads the argv, Codex's `-m` precedes the positionals (fresh and resume) — pinned by exact-argv tests.
- `decision.as_dict()` → `RunPlan.model_routing` (omitted when None) → manifest `plan`; `TaskResult`/result.json/envelope/trace carry nothing. `--difficulty` forwards in `_inner_argv`. PR 2 (enforce/Board/Rooms/TUI) is calibration-gated (§8).

### Memory (`memory.py`)

`SyncmillMemory` applies syncmill conventions over the selected `MemoryBackend`: `agent-runtime:{agent_id}` is private per agent, `shared` is cross-agent knowledge, and the Supervisor is the sole reader/writer for orchestration (no agent-to-agent MCP). Full design, memtomem-parity history and residues: [docs/architecture/memory.md](docs/architecture/memory.md) — read it before changing `memory.py`, `memory_backend.py` (especially `MemtomemBackend`'s add/share/purge paths), board-run GC, or the memtomem/mcp pins.

Rules that must hold:

- **One reused loop.** `run_async()` reuses a module-level event loop; never switch to `asyncio.run()`. `close_event_loop()` releases it at CLI shutdown.
- **Best-effort.** `SyncmillMemory` methods catch backend exceptions, log, set `degraded`/`last_error` and return a safe default; a broken backend must never abort a task. The CLI shows `memory.degraded` as a yellow notice.
- **Coupling.** memtomem privates are reached only from `memory_backend.py`'s `MemtomemBackend` (`memory_migration.py` is the sanctioned maintenance exception); never `import memtomem.server.*` or use `start_agent_session`.
- **Search.** `search_context` is one `search_multi` over `shared` (+ `agent-runtime:{id}` only when a caller passes `agent_id`; + global `shared` for a board-scoped view). A `tag_filter` the backend cannot apply returns unfiltered results and degrades.
- **Shares.** Unscoped `share_result` writes the original to `agent-runtime:{id}`, then `share_chunk` copies every chunk to `shared` with `shared-from=` + a best-effort link. A backend with `BackendCapabilities.single_span_share` (memtomem) does both halves in one `add_and_share` call returning an aligned `SharedWrite(source_ids, shared_ids)`; any other backend keeps the two calls, and the rules below read either the same way. A raising step falls back to a direct `shared` add and degrades (even after some chunk copies succeeded); when every `share_chunk` call returns but only some return ids, it degrades without the fallback (the copies already in `shared` would be duplicated); `WriteBlockedError` degrades and skips the fallback. Board-scoped views write directly to their scope. Every share is tagged `[kind, agent_id, task:{id}]` (kinds: `task-result`, `room-result`, `pipeline-stage`, `council-proposal`, `council-critique`, `decompose-subtask`, `decompose-plan` = the parsed plan only).
- **Write-path invariants — do not change without the real smoke.** Each write invalidates the search result cache; writes go to namespace-suffixed day files `{date}.{ns}.md`; each entry gets an explicit unique heading (`_entry_title`); payloads open with a **bold** label, never `## ` (heading-led content skips the unique title).
- **Locking — do not change without the real smoke.** A write takes the namespace lock (`_namespace_lock_path`) before selecting its day file, then holds the resolved file's L2 sidecar (`memory_lock_path`) across mix-check → append → index; `purge_namespace` holds the namespace lock plus every owned source's sidecar, removes sources before rows, and raises when a purge cannot be proven complete. `add_and_share` (#117) holds ONE span over both appends: both namespace locks, then both day files' sidecars, from before the clock is read until the last copy is indexed; its copies share one `index_file` and are matched to their source chunks by `shared-from=`, never by index order. **Ordering rule** (on `_namespace_lock_path`; tested): every namespace lock before any sidecar, never a namespace lock while holding a sidecar, and within each level ascending `str(lock_path)`, deduplicated (`_namespace_lock_order`; sidecars by `memory_lock_path`). An unlocked append lets the watcher index the file first and strand the chunk in the wrong namespace.
- **Protected targets.** An unattended append refuses a symlinked day file, a non-`user` tier, a read-only source and an excluded source. `purge_namespace` refuses, before deleting anything, a symlinked source, one in a `read_only_memory_dirs` root, and one in a `project_shared` tier (a `project_local` source may be purged).
- **Pins.** `memtomem>=0.6.8,<0.7` and `mcp>=2.0.0,<3` move together; bump only with `SYNCMILL_TEST_MEMTOMEM_REAL=1 uv run pytest -q tests/test_memory_backend_memtomem_integration.py`.

### Interactive collaboration (human drives one agent, syncmill runs the rest)

Exclude an agent (`run --exclude claude`, or `SYNCMILL_CLAUDE__ENABLED=false`) and drive it yourself; the two sides meet only through `shared`, never by syncmill invoking the interactive process. Full contract and examples: [docs/architecture/interactive-collaboration.md](docs/architecture/interactive-collaboration.md) — read it before changing namespaces, share tags, `search_context` filtering, project memory paths or `syncmill session`.

Rules that must hold:

- **Same store.** An initialized project at `[memory]` defaults is moved by `configure_project_memory` to `<repo>/.syncmill/memory`; to collaborate, set `db_path = "~/.memtomem/memtomem.db"` and `memory_dir = "~/.memtomem/memories"` together. The interactive session must target `shared` explicitly (its default namespace is `claude:<cwd>`): write with `mem_add(..., namespace="shared")` or `mem_agent_share(..., target="shared")`, read with `mem_agent_search(..., include_shared=True)` or `mem_search(..., namespace="shared")`.
- **Phase-specific recall** (user-facing contract). General `shared` notes are eligible in route/compete, rooms and each strategy's first phase. Later phases read only these kinds (comma-separated OR); untagged notes, `task-result`-only rows and `board-result:*` are excluded there. Boards apply the same filter to their run scope and global `shared`; a backend that cannot filter returns unfiltered context and degrades.

  | Phase | Eligible kind tags |
  |---|---|
  | Pipeline stage 2+ | `pipeline-stage` |
  | Decompose subtasks | `decompose-plan,decompose-subtask` |
  | Decompose synthesis | `decompose-subtask` |
  | Council first critique | `council-proposal` |
  | Council later critiques + synthesis | `council-proposal,council-critique` |

- These are relevance searches, not delivery guarantees (prior runs compete; row/byte limits apply). The current run's own earlier results do not depend on them: they arrive through the total handoff above.
- Collaboration is knowledge, not file merges: interactive edits land in the real tree, agents in isolated worktrees. The human is not a compete candidate (future work).

### Read-only MCP gateway (`gateway.py`)

`syncmill serve` exposes `shared` (and the board, when enabled) to external MCP clients as a separate, read-only consumer — not an agent, not a strategy, never a writer. Full design: [docs/architecture/gateway.md](docs/architecture/gateway.md) — read it before changing `gateway.py`, the `SyncmillMemory` shared-read methods, the board reads behind the `board_*` tools (`Board.list_recent`, `Board.get`, `Board.stats`), the `serve` command or the mcp pin.

Rules that must hold:

- **Exactly six read-only tools**: `shared_search`, `shared_read`, `shared_stats`, `board_list`, `board_get`, `board_stats` (`test_exactly_six_readonly_tools`). No write tool for memory or the board; `build_gateway` returns an unstarted `MCPServer`.
- **Coupling stays in `memory.py`.** `search_shared`/`shared_stats` return `None` on a per-call backend failure; `get_shared` returns a `SharedRead` (`found`/`not_found`/`unavailable`). Never infer unavailability from the sticky `degraded` flag. A board-scoped memory view reports unavailable.
- **Isolation.** `search_shared` passes `namespace="shared"` and filters rows; `get_shared` returns `not_found` for a chunk outside `shared` (the store's `get` is not namespace-scoped) and for a malformed id, without degrading; `shared_stats` reports the `shared` count alone.
- **Board tools** are always registered: `_BOARD_DISABLED` when no board, `_BOARD_UNAVAILABLE: <error>` when it failed to open or a read raises `BoardError`. `board_list` clamps `limit` to `[1, 200]`, returns newest first via `Board.list_recent`, and answers an unknown `state` as a client argument error. `shared_search` clamps `top_k` to `[1, 50]`.
- **Transport.** Under stdio, stdout is the JSON-RPC channel — every human-facing line goes to stderr. `serve` never uses `run_async`; memory is closed with `anyio.run(memory.close)`. `--http` defaults to loopback, warns on a non-loopback host, and has no auth.

### Live tmux monitor (`monitor.py`)

`--monitor` shows a concurrent run live in tmux, one pane per agent — an observability layer, not a strategy or agent; the `AgentRunner` signature is unchanged. Full design: [docs/architecture/monitor.md](docs/architecture/monitor.md) — read it before changing the output-sink seam in `agents.py`, the `_monitor_*` strategy helpers, `_inner_argv`, the env shim, or the OUTER/INNER handoff.

Rules that must hold:

- **OUTER/INNER split.** The OUTER `syncmill run` builds a detached session whose control pane re-runs `syncmill run` from `_inner_argv` — deterministic reconstruction, **never `sys.argv`**; `--monitor` and `--emit-patch` are omitted, `--team` is forwarded. The OUTER `tmux attach`es, then renders the INNER's `result.json` with the shared `render_result`; a failed attach (non-zero rc, no result) exits non-zero.
- **Environment.** The INNER gets the OUTER's environment from a `0600` `<run_dir>/env` snapshot through `_ENV_SHIM`, which **replaces** the pane env (`os.environ.clear()`, keeping only `TERM`/`TMUX`/`TMUX_PANE`) and unlinks the file before parsing it. Never pass secrets in tmux argv (no `-e KEY=VALUE`). Attach strips `TMUX`/`TMUX_PANE` but **keeps `TERM`**.
- **Inner detection** is `inner_dir()` only: the env var **and** the `.syncmill-inner` sentinel. `agents._isolated_process_env` strips `SYNCMILL_MONITOR_DIR`/`SYNCMILL_RUN_ID` (`_RUN_IPC_ENV`) so an agent running syncmill cannot pose as the inner.
- **Never required.** The outer launch needs monitoring on ∧ not inner ∧ a strategy in `CONCURRENT_STRATEGIES` ∧ a TTY ∧ tmux. Monitoring off or already-inner returns silently; a requested outer launch that fails the strategy/TTY/tmux checks, or raises `MonitorLaunchError`, prints a notice. Every case falls through to a normal run.
- **Strategy plumbing.** `Strategy.execute(..., monitor=)`; concurrent strategies call `_monitor_phase`/`_monitor_streaming`/`_monitor_status` (no-ops on `None`); route/pipeline accept and ignore it. The Supervisor calls `monitor.finish(result)` in `finally`.
- **Sink seam.** `_OUTPUT_SINK`/`stream_to`/`OutputSink` live in `agents.py` so `agents` never imports `monitor`. `FileSink` writes non-blocking `O_APPEND` and never raises. On a teed timeout the log shows the pre-timeout bytes plus a marker while the result's `output` stays empty.
- **Tests never launch tmux**: patch `monitor._tmux`/`_tmux_attach`/`tmux_available`/`_stdout_isatty`/`inner_dir`/`launch_monitored_run`; `FakeMonitor` and the autouse `reset_output_sink` live in `conftest.py`.

### Durable interactive sessions (`session.py`)

`syncmill session start|list|attach|stop` runs an agent's **bare interactive CLI** in a dedicated tmux session that survives detach and reattaches by name. It is not an agent or strategy and touches neither agents nor memory. Full design: [docs/architecture/session.md](docs/architecture/session.md) — read it before changing session naming, lifecycle/listing (`start`/`list`/`attach`/`stop`), the env shim, or the shared monitor primitives.

Rules that must hold:

- **Never through `AgentRunner`.** The launch command is the standalone `_INTERACTIVE` map (duplicating `kimi-code → kimi` on purpose); the CLI validates against its keys, not `registry.available()`, so an orchestration-disabled agent can still run.
- **Reuses monitor primitives** (`_tmux`, `_tmux_attach`, `_SNAPSHOT_SKIP_ENV`, `INNER_ENV`, `RUN_ID_ENV`) without changing `monitor.py`; tests patch the lookup site `session._tmux`. Own `SessionLaunchError` — no fallback: missing tmux is a hard error, and `start`/`attach` require a TTY.
- **Deterministic names** `syncmill-session-<agent>` are the reattach key; every `-t` target uses `_exact` (`=name`). `list`/`stop --all` match the exact known names, never the prefix, so they cannot touch a monitor run. `#{session_attached}` is a client count: attached = `int > 0`.
- **Environment.** A `0600` snapshot (excluding `_SNAPSHOT_SKIP_ENV`, `INNER_ENV`, `RUN_ID_ENV`) is restored by `_SESSION_ENV_SHIM`, which unlinks the file before parsing; secrets never ride argv. The workdir is the resolved `repo_root`, checked with `is_dir()` (tmux `-c` silently falls back to `$HOME`). No `remain-on-exit`.
- Stale run dirs are swept only with the `.syncmill-session` sentinel and past the age bound. Tests (`tests/test_session.py`) never launch real tmux.

### Persistent work-board (`board.py`)

Durable records of team work (goal, owner, state, blockers, verified-done, deps, handoff baton) in a flat `board.py` with its **own SQLite store** (`~/.syncmill/board.db`). State lives only on the board, knowledge only in `shared`; they cross-link by the single tag `board-result:{item_id}`. Full design, history and test map: [docs/architecture/board.md](docs/architecture/board.md) — read it before changing board behavior or any of its integrations (Supervisor `run_item`, scoped memory, decision prompts, the gateway `board_*` tools, the dashboard/TUI).

Rules that must hold:

- **Supervisor-level, not a strategy concern.** `Supervisor.run_item` wraps `run`; the `Strategy` protocol is unchanged. `run_item` calls `run` with `memory=` (the scoped view, used at all five memory call sites), `run_id=` (lease, `board-run:{run_id}` scope and report identity are one id) and `enable_routing=False` (model routing must never classify the enriched board prompt). `--strategy` is not a `run_item` param — the CLI sets `config.default_strategy` before building the Supervisor.
- **Agents never touch the board.** The item reaches them via `render_item_prompt`; `parse_board_update` reads an optional ` ```syncmill-board ` block, **never raises** (catches `RecursionError` too), sanitizes each field independently, drops a malformed `outputs` list whole, and agrees with `strip_board_block` on block bounds (a genuine close fence is required).
- **Load-bearing, built lazily.** Disabled by default (`build_board` → `None`). Built on first `run_item`, never in `Supervisor.__init__`, so a corrupt board cannot break a plain `run`. `Board()` raises only `BoardError` (`sqlite3.Error`, `OSError`, and `RuntimeError` from `expanduser`, which runs inside the try). Dispatch and mutation (`board` commands, `dashboard`) convert it to `ClickException` — no fallback. Read-mostly consumer surfaces degrade instead: `serve` keeps the shared-memory tools up and its `board_*` tools report the board as unavailable (not "not enabled"); `tui` opens in limited mode.
- **State machine `planned → running → (blocked | review) → done`.** `done` requires ≥1 passing `Evidence` or a human sign-off, counting evidence already on the item plus any supplied now; `board update ID --state done` with neither is rejected. An agent's advisory `done` counts only with passing verification from **this** run, else it is clamped to `review` (never raises). Humans cannot move an item into `running`.
- **Atomic claim + lease fencing.** Every claim path (`claim(id)` and `next_runnable`) is a `BEGIN IMMEDIATE` CAS `planned→running` (autocommit `isolation_level=None`, WAL, `busy_timeout`). Automatic selection (`next_runnable`) additionally requires: not `human_required`, every `depends_on` item `done` (a missing dependency is unsatisfied), FIFO `created_at, rowid`, optional exact `goal`. An explicit `claim(id)` checks only id + `planned` — it is the operator override. The lease is a fencing token: `apply_update(run_id=)` and `_block_on_crash` CAS on `state='running' AND lease_run_id=?` and drop a 0-row match. A same-state annotate keeps the lease; a no-`--state` annotate resolves its target inside the transaction. Every transactional method rolls back on **any** exception. `recover_stale_running()` runs at the top of every `run_item`.
- **Memory boundary.** Items run with `SyncmillMemory.scoped(run_id)`, collapsing shared and per-agent namespaces to `board-run:{run_id}`; scoped reads also union `shared` (D4), writes never leave the scope. Afterwards one best-effort sanitized summary goes to global `shared` via `share_board_result` (output with the board block stripped, never the enriched prompt) — **only if the fenced `apply_update` applied**; a run that lost its lease publishes nothing. Decisions (`active_decisions(goal=item.goal)`: active global decisions plus that exact goal's) and the handoff baton are injected deterministically, never via top-k recall. Scratch namespaces are GC'd via `collectible_board_run_ids`, which reads ownership and liveness in one SQLite snapshot (a claim commits its lease and ledger row atomically) and excludes active leases and reclaimed runs still within one lease timeout of grace; the sweep runs under a cross-process lock, tombstones only acknowledged deletions, and never touches `shared` or `agent-runtime:*`.
- **Crash vs failure.** A crash (`BaseException`) moves the item to `blocked` + `human_required` and re-raises; a FAILED result is not a crash (`apply_update` records `blocked`).
- **Audit compaction.** `board compact` and the opt-in `board.transitions_retain_seconds` (default `0` = off) share `compact_transitions`: fold stats aggregates, then delete, in one transaction, so `stats()` is unchanged. The automatic call runs only at `run_item`'s housekeeping points (after the outcome, or on an empty claim; never on the claim or crash path) and degrades to a warning.
- **Phase 2.** Decisions live in their own table; supersede is a same-scope, same-project CAS (a replacement inherits the superseded decision's `project_id`; NULL = unbound, the pre-#174 reach), and injection stays in `board.py::render_item_prompt` (a `strategies/base.py` helper would create a `board → strategies` cycle); a failed decisions read degrades. `Board.stats` derives everything from rows + the audit table. The gateway's `board_*` tools are read-only and always registered (exact-set guard = 6 tools); a bad `state` is a client argument error, not "unavailable"; uuid validation lives in `Board.get`.
- `Evidence`/`WorkItem`/`WorkState`/`Decision`/`BoardStats` live in `board.py`, not `models.py`.
- **Tests:** `tests/test_board.py` drives the real `Board` on `tmp_path` with an injected `now`; `conftest.py` provides `FakeBoard` and `FakeMemory.scoped`/`share_board_result` (scoped shares never reach the parent's `.shared`).

### TUI dashboard (`dashboard.py`)

`syncmill dashboard` is the compatibility Textual app over the work-board: polling item table, detail, stats, human-path edits, the TEAM pane (`t`) and the read-only REPORT pane (`p`). Full design: [docs/architecture/dashboard.md](docs/architecture/dashboard.md) — read it before changing its edit funnels, TEAM writes, the REPORT pane or the poll loop.

Rules that must hold:

- A separate observer/editor process, never on the orchestration thread. It touches the board, `syncmill.toml` (via `teamfile.py`) and report files read-only — never agents, memory or the MCP gateway.
- Every board write is an in-process `board.py` call with `actor="human"`, the same recipe as `board update`. Detail text reuses `render_item`/`render_board_stats`; TEAM detail reuses `teamfile.render_team`; REPORT detail reuses `render_report`, and viewing reports is not gated on report recording being enabled (the `report list`/`report show` posture).
- TEAM writes go through `_apply_team_save`/`_apply_default_team` (the `team save`/`team use` recipe); a rejection sets `last_team_error` and never kills the app; reload uses `load_config_at(self._toml_path)`, never the ambient cwd.
- Board reads stay on Textual's thread (`check_same_thread=True`). Poll reads degrade (DEGRADED notice, last snapshot kept); the launch hard-fails (TTY gate before the db opens, `BoardError` → `ClickException`); the CLI owns and closes the board.
- Tests: `take_snapshot` is the pure fetch seam; the interval callback is `action_refresh`, so tests use `refresh_seconds=3600` and drive ticks explicitly; assert on app-owned seams (`status_text`, `detail_text`, `last_action_error`, `team_detail_text`, `last_team_error`), not widget internals.

### Full TUI command center (`tui.py` + `tui_jobs.py`)

`syncmill tui`: Board/Runs/Reports/Teams/Status/Sessions/Rooms tabs, separate from `dashboard`. Full design: [docs/architecture/tui.md](docs/architecture/tui.md) — read it before changing rendering/escaping, poll paths, workers, the `ArtifactStore.list_detailed` listing cache, the job manager, the board lease heartbeat (`Board.renew_claim`, the Supervisor heartbeat), or Rooms evidence views.

Rules that must hold:

- **Limited mode, never a new DB.** A disabled or broken board is a limited-mode page. `--tab auto` opens the first enabled-and-opened surface (Rooms → Board → Runs), else Status; an explicit `--tab` is honored; store bindings are independent.
- **No markup.** Render text through `tui_render.PlainStatic`/`PlainLabel`/`PlainOption`/`plain`/`plain_select` and `OptionList(markup=False)`; both apps' `notify` default to `markup=False` and escape message and title. `test_tui_render.markup_sinks` is the AST guard.
- **Terminal safety.** `run_tui`/`run_dashboard` call `install_render_guard()`, which substitutes dangerous characters with **same-width** ones in `Strip.render` (never deletes) and drops unsafe link URLs; each app sets `self.error_console = tui_render.error_console()`. Bodies are escaped with `printable_text`, one-line fields with `printable_name`, before splitting on LF. Editable `Input`/`TextArea` values are not escaped, except the round-prompt prefill (`escape_controls`).
- **Polling.** Every poll path is degrade-guarded (`_ROOM_POLL_ERRORS`/`_SESSION_POLL_ERRORS`/`_JOB_POLL_ERRORS`) — an unguarded read kills the app. `_sync_options`/`_update_static` skip unchanged content; `_focus_primary` runs only on tab switches. `JobStore.control` converts failures to `JobError`.
- **Threads.** Board stays on Textual's thread (SQLite is thread-affine); a `room-evidence` worker opens its own `RoomStore`. The Artifacts listing is a single-flight `artifact-listing` thread worker: the in-flight flag is released only when its result reaches the event loop, mid-scan requests coalesce into one rerun, stale generations are dropped, and it is not `exclusive`. Only the listing is cached (`ArtifactStore.list_detailed`'s per-instance manifest cache); the selected run's detail is reread on every refresh, even when the listing is unchanged.
- **Rooms evidence** (`m`/`c`) is a `room_compare.RoomComparison` snapshot built only on request (`c`, first `m`, `r` there) — never on polling or tab activation — and dropped when the **selected** room changes or the store root changes/becomes unavailable; when the selected room's contents change, the snapshot stays with its `evidence as of` time and a changed-since hint.
- **Orchestration never runs inside Textual.** A `BoardJobSpec` becomes `board run --id` / `--loop [--goal]` argv launched by `JobStore` as a detached POSIX manager (a fail-fast per-target `flock` — a board run is keyed by its board db — 0600 state, token-authenticated socket). Artifact apply goes through a detached `ArtifactApplySpec` worker. `q` only exits the UI; stop is SIGINT first, force (SIGKILL) only ten seconds later, never fabricating a board transition. A detached run survives the UI, but a long one keeps its claim because the Supervisor's heartbeat thread renews the lease (`Board.renew_claim`) on its own `Board.new_connection()` — without that stale recovery would reclaim and re-dispatch a healthy item. Default tests use fake children only.

### Named multi-team profiles (`[teams.<name>]`, `--team`, `board run --goal`)

Named profiles (ordered agent subset, optional capability map, optional strategy, owned board goals) are **config, not a store**: `SyncmillConfig.teams`, written as `[teams.<name>]` (the verbatim team name) or the JSON env `SYNCMILL_TEAMS` (deep-merged with toml teams). Full design and test map: [docs/architecture/teams.md](docs/architecture/teams.md) — read it before changing team selection, `apply_team`, the `run`/`board run` team paths or monitored-run forwarding.

Rules that must hold:

- **Overlay before the Supervisor.** `config.apply_team` sets `agent_priority ← team.agents`, `capabilities.agents ← team.capabilities` (only when non-empty) and `default_strategy ← team.strategy` (only when set), and stamps `config.applied_team` (the envelope's `team`). It must run before `Supervisor(config)`, which derives registry, matcher and strategy in `__init__`; no strategy, registry or matcher change is involved.
- **Precedence:** explicit `--team` > goal→team mapping (`board run --goal`, or a read-only peek of the item's goal on `board run --id`, which takes no claim) > `default_team` > none. An explicit `--strategy` beats the profile's strategy. `board run --loop` without `--goal` never applies goal mappings — one Supervisor, one lineup per invocation. `--id` and `--goal` are mutually exclusive; `--goal` also scopes the atomic claim (`next_runnable(goal=)`).
- **Validation split.** Structural breakage (empty or duplicate `agents`/`goals` entries, empty goal `""`, empty team name, one goal declared by two teams) fails at construction (`TeamConfig` validators + `check_team_goals`). An unknown team name or agent id fails in `cli._apply_team`; a profile strategy typo in `_validate_strategy`. On `board run` all of this happens before a claim is taken. A team whose agents are all disabled is not a config error.
- **CLI config loads go through a converting loader** — `_load_config()`, or `_target_project_config` for `--repo` project commands — so a broken config is a one-line `ClickException`, never a traceback. `team use --clear` deliberately skips loading (it is the fix for a broken default).
- **Monitored runs forward `--team` in `_inner_argv`** (config selection, like `--strategy`; unlike `--monitor`/`--emit-patch`). A resolved `default_team` rides the same forwarding.
- `team list` / `team show` are read-only and render even a broken profile (`team show` uses the shared `teamfile.render_team`).

### Team-profile writing (`teamfile.py` — `team save`/`use`, `default_team`)

`teamfile.py` is where `syncmill.toml` gets written: `team save`/`team use`, the dashboard and TUI TEAM panes, and `init` (via `write_document`) all land their writes through it, while reads stay on pydantic-settings' toml source. Full design, lock-path history and test map: [docs/architecture/teamfile.md](docs/architecture/teamfile.md) — read it before changing any config write, the lock, `default_team` handling or the TEAM panes.

Rules that must hold:

- **Never hand-serialize toml.** Writes parse with tomlkit, mutate the smallest span, dump, and land via temp + `os.replace` (`_atomic_write`), so comments survive and a failed write cannot truncate the file. The module lives outside `config.py` (imports stay acyclic: `teamfile → config, agents, artifacts`).
- **Every write runs under `config_write_lock(path)`** — blocking, cross-process, re-entrant per thread (`artifacts._cross_process_lock`). `save_team`/`set_default_team`/`write_document` take it internally; callers that read-then-write (`team save` validate→save, `team use`, the dashboard/TUI TEAM panes, `project.py`'s init merge) hold it across the whole span. Only acquisition failure becomes `TeamFileError`; body failures keep their own type. The lock file is never unlinked.
- **Lock path is settled — do not re-litigate:** `~/.syncmill/locks/<sha256 of the config's realpath>.lock`, derived (never probed via `git`) and outside the working tree. Per-user by design. An unresolvable/unwritable state root is a `TeamFileError`.
- `set_default_team(path, None)` checks for a missing file before locking, so `team use --clear` touches nothing when there is no file.
- **`save_team` replaces `[teams.<name>]` wholesale** (the flags/modal define the full profile), emitting `strategy`, `goals` and `capabilities` only when set/non-empty. A new top-level scalar must land before the first table (regression-tested).
- **Write target = `toml_path()`**, derived from `SyncmillConfig.model_config["toml_file"]`, so write and discovery are the same file (the autouse `isolate_toml_discovery` fixture isolates every save test).
- **Two-view validation** (`validate_team_save`): the shared `config.check_team_goals` over the resolved env+toml view with the candidate substituted, and over the file standing alone when `path` is given (an env override can mask a file conflict), plus the unknown-agent-id check. `TeamFileError` is the single seam error (CLI → `BadParameter`/`ClickException`, dashboard → `last_team_error`).
- **`default_team` is not validated against `teams` at construction** — a dangling value must not brick `team use --clear`. `cli._apply_default_team` applies it when no `--team` is given and raises a `ClickException` naming the fix for a dangling name; `""` counts as unset. `load_config_at(path)` is the TEAM panes' reload seam, pinned to the injected path.

### Run reports (`report.py` — run identity + run envelope)

Run identity and the body-free run-envelope join artifact (`contracts/run-envelope.schema.json`) written to `{runs_dir}/{run_id}/envelope.json`. Full design and test map: [docs/architecture/report.md](docs/architecture/report.md) — read it before changing run-id resolution, the envelope, retention, the Supervisor's finalization order, or how the CLI/monitor OUTER prints the report path.

Rules that must hold:

- **`resolve_run_id(explicit)`: explicit > monitor-inner env > fresh uuid4.** Board `run_item` passes its lease run id (lease, `board-run:{run_id}` scope and envelope are one id). A monitored OUTER mints the id and passes it as `SYNCMILL_RUN_ID` via the 0600 env snapshot (never argv); it is honored only when `inner_dir()` confirms this is the inner. `""` counts as unset.
- **Disabled is the default and touches no filesystem** (`build_reporter` → `None`). Enabled, `start()` sweeps retention and writes a schema-valid skeleton (`result.status:"failed"`, every sub-block present) so a killed run still leaves a parseable envelope.
- **Body-free by contract:** digests (`sha256:<64hex>`) and counts only — never prompts, output, patch content or path lists. The one body-carrying file is the run dir's `result.json`, built with `monitor._result_to_json`; the patch digest is over the same surrogateescape bytes `--emit-patch` writes.
- **Finalization order in `run`'s `finally`:** trace flush → `reporter.finish` → `monitor.finish` → memory-session close → `pool.cleanup_all()`. Each observability step is guarded so it cannot skip the later ones.
- **Everything degrades.** `RunReporter` methods catch `(OSError, ValueError, RuntimeError)` and warn; the Supervisor guards `start`/`finish` with `except Exception`. `patch_envelope_board` (board path, after `apply_update`) runs whenever reporting is enabled, deliberately not gated on the lease fence.
- **Retention is Supervisor-level and ownership-bound.** Oldest run dirs beyond `keep_last` (the current run counts and is never removed) are deleted only if syncmill owns them: the `RUN_DIR_SENTINEL` file (written by both `RunReporter` and `FileTracer`), or a legacy envelope that parses, has syncmill's `kind` and names its own directory. Two callers share `_sweep_retention`: `RunReporter.start()`, and `sweep_runs_dir` when reporting is off but tracing is on with no custom `trace.dir` — called as the first statement inside `run`'s try, degrading on `Exception` but letting `BaseException` reach finalization.
- `team` in the envelope comes from `config.applied_team`; `report list`/`report show` are read-only and work while recording is disabled. A monitored OUTER cannot see the inner's config, so it prints the report path only when the envelope file actually exists (`launch_monitored_run(report_runs_dir=)`).

### Trace export (`tracer.py`)

OTLP-JSON trace export (`trace.otlp.json` beside the envelope) that tracegraph ingests unchanged (`tracegraph ingest-otlp`). The document shape is pinned by tracegraph's T1 generators and goldens. Full span semantics per strategy and test map: [docs/architecture/tracer.md](docs/architecture/tracer.md) — read it before changing any span builder, span name, attribute, or the flush/finish wiring.

Rules that must hold:

- **Disabled is the default** (`build_tracer` → `None`, no filesystem touch). `dir=""` means the report `runs_dir` (`fallback_dir`). `otlp-http` keeps the durable file and additionally streams body-free spans to Phoenix; stream failures degrade to warnings/receipts. `include_output` is warned about and ignored.
- **Retrospective recorder.** Strategies append completed `SpanRecord`s after each attempt/fan-out returns (never live spans inside the agent layer); builders live in `strategies/base.py` (`_attempt_span`, `_gate_span`, `_select_span`) so semantics cannot drift between strategies. A concurrent phase passes `started_at=` to `aexecute_isolated`/`aexecute_each` and hands each attempt its recorded `start` (span = `start` → `start + duration`); without one (sequential steps, runners that bypass `CLIAgent.aexecute`) the span falls back to `end − duration`.
- **Span `name` is a stable structural label** (agent ids, phases, indices — never uuids or timestamps); tracegraph diffs on it. Causality rides `graph.node.parent_id` and span links, never `parentSpanId` (flat under the run root) or timestamps.
- **Attributes:** builders put bare-suffix keys in `attrs`; `_span_attributes` prefixes `syncmill.`, drops anything outside `_ALLOWLIST`, and injects `run_id`/`schema_version` (toolgraph evidence adds its own `toolgraph.*` keys). Error text is bounded (`timeout` | `exit_code=N` | `gate: <summary>`), never raw output.
- **Attempt spans carry the RAW pre-gate status** (snapshot before `_gate_accepts`/`_select_winner` mutate results; council/decompose also snapshot duration). Gate spans exist only for candidates actually gated; the `select` span parents on the winning attempt and links the other raw-COMPLETED candidates. An early aggregate failure leaves no select span.
- **`FileTracer.flush()` is all-or-nothing** (duplicate node ids or dangling parents fail the flush rather than write a rejectable file; temp + `os.replace`).
- **Wiring:** the Supervisor records the `syncmill.run` root span and flushes in `run`'s `finally` **before `reporter.finish`**, which digests the file into the envelope `trace` block; a flush failure yields `trace:{status:"error"}` and `"trace"` in `degraded`. Both steps are guarded.
- **A custom `trace.dir` is never swept** (user-owned); the default dir is covered by report retention (see Run reports).
- **Tests never import tracegraph:** `test_supervisor._mini_validate_otlp` mirrors the adapter's raise conditions; strategy tests drive `FakeTracer`.

### Toolgraph preflight (`preflight.py`)

Optional `toolgraph preflight` before strategy dispatch, checking the run's ambient MCP surface against toolgraph's policy graph. Full design, decision/status matrix and test map: [docs/architecture/preflight.md](docs/architecture/preflight.md); strict mode: `docs/ecosystem/adr-0001-strict-preflight-enforcement.md` — read them before changing the request/argv shape, artifact handling, status mapping, or the strict path.

Rules that must hold:

- **Disabled is the default** (`run_preflight` → `None`, no subprocess). Enabled, `run_preflight` never raises (a last-resort `except Exception` net).
- **Advisory mode (default) never blocks:** every failure (binary absent, timeout, non-zero exit, bad JSON, unknown major `schema_version`, nothing mapped) degrades to a warning and the run continues. Without `control_plan_enabled` it probes **once, for the lead mapped agent**; all `agent_ids` are mapped through `agent_map` (verbatim real-id keys such as `kimi-code`), each unmapped id warns, and nothing mapped means an `unresolved_identity` block with no subprocess.
- **Control-plan preflight** (`control_plan_enabled`, `docs/ecosystem/adr-0003-control-plan-preflight.md`) is checked first and replaces that path: one whole-plan batch call, persisting `control-plan.json` and `control-preflight.json`.
- **argv only, no stdin** (`_request_argv`); the subprocess runs through `syncmill.preflight._sync_run` (the owned-process-group runner; the test seam).
- **Artifact handling (legacy path):** the subprocess always writes `--out` to a tempdir; only an artifact that validates is atomically copied into the run dir as `preflight.json` and digested. With reporting disabled (`out_dir=None`) the probe still runs and the artifact is discarded.
- **Wiring:** called once after `_matched_order`, the empty-agents guard and `reporter.start`, outside `run`'s try/finally. Warnings surface through `Supervisor.preflight_warnings` (yellow CLI block); the block reaches the envelope via `reporter.finish(preflight=)`.
- **Strict mode (P3.1, opt-in `mode="strict"`):** evaluates every distinct mapped participating identity, enforces the selected profile, writes a body-free aggregate, and — when enforcement blocks (`preflight.should_block`) — returns a normal FAILED `TaskResult` before memory registration, worktree creation or any agent subprocess; an allowed verdict continues the run. Defaults: unknown=block, drift=warn, unavailable=block; malformed or mismatched evidence always blocks. When toolgraph is enabled, config rejects strict without report recording, without a non-empty unique `candidate_tools` list, or without an `agent_map` entry (a disabled strict config is accepted — the rollback path).
- **Tests never need toolgraph:** `test_preflight.py` monkeypatches `syncmill.preflight._sync_run` with vendored goldens (`tests/fixtures/toolgraph/preflight-*.json`).

### Config (`config.py`)

`SyncmillConfig` is pydantic-settings; `settings_customise_sources` orders the sources init kwargs > `SYNCMILL_*` env (`__` nesting) > `syncmill.toml` > field defaults. Full design: [docs/architecture/config.md](docs/architecture/config.md) — read it before changing sources, discovery, validation or how the CLI overrides config.

Rules that must hold:

- Every field stays env-overridable; nested tables deep-merge across sources (one env var overrides one field, the rest of the table still comes from the file).
- A bare `SyncmillConfig()` discovers `syncmill.toml` cwd-relative (not `repo_root`); a missing file is empty. Project-config commands given `--repo` load the **target** project's config instead (`_load_run_config` / `project.load_project_config`: env > target toml > defaults, even when the target has no toml) — never the caller's cwd toml, which would lend it another project's gates, id and state roots. For `run`, the git root of `--repo` is passed as `repo_root_override` and beats `SYNCMILL_REPO_ROOT`; it defaults to `None` so an unset flag leaves `repo_root` to file/env. Exception: `session start` loads the caller's config and uses `--repo` only as the working directory (which need not be a Git repo).
- `run`'s `--strategy`/`--timeout` mutate the built config after construction, each only when passed.
- Naming: per-agent tables use the underscore field (`[kimi_code]`); `[capabilities.agents]` keys use the real agent id (`kimi-code`).
- Unknown top-level keys are rejected; most nested tables ignore unknown keys, but some (e.g. `[model_routing]`) forbid them.
- `default_team` is deliberately not cross-validated at construction. The toml is written only via `teamfile.py`'s tomlkit helpers (`project.py`'s `init` uses `teamfile.write_document`), never hand-serialized.
- Never commit a real `syncmill.toml` at the repo root (the hermetic suite builds `SyncmillConfig()` from there); ship `syncmill.toml.example`.

### Worktrees (`worktree.py`)

Supervisor runs capture a private snapshot under `<workspace.root>/<run-id>/`; worktrees live at `<run-id>/worktrees/{agent}-{taskid}-{slot}` on branch `syncmill/{agent}/{taskid}-{slot}`. Full design: [docs/architecture/worktrees.md](docs/architecture/worktrees.md) — read it before changing worktree creation, cleanup, the orphan sweeps or patch capture.

Rules that must hold:

- One `WorktreePool` per run; the Supervisor calls `cleanup_all()` in `finally` (removals independent of each other), the strategy chooses the topology. The per-acquire `slot` keeps repeated acquisitions for one agent (Decompose) distinct.
- Two crash sweeps: `cleanup_orphaned_worktrees` (legacy in-repo `.syncmill-worktrees`) and `sweep_orphaned_workspaces` (snapshot workspaces). The latter touches only dirs carrying `.syncmill-run`, written before the clone by `capture_repository_snapshot(sweepable=True)` (room/session workspaces are not marked); `retain()` adds `.syncmill-recovery`; liveness is the owning pid (`os.kill(pid, 0)`), never file timestamps. Age (`workspace.stale_seconds`, default a week, floor one hour) is only the pid-reuse backstop; a pid-less sentinel falls back to age.
- The winner's code survives as `TaskResult.patch`, captured by `capture_changes` through a private `GIT_INDEX_FILE` (`diff --cached --binary`, untracked files included, agent index untouched). `run --emit-patch` exports it. `syncmill run` never applies a diff; `syncmill apply` does.

## Design invariants (the maintainer cares about these)

- **Don't pre-abstract.** The package stays flat until the code demands a split. The **first** justified split was `strategies/` (#12), carved out only after all four strategies (Route/Pipeline/Compete/Council) had landed and `supervisor.py` had grown past ~960 lines — the real code pressure the issue gated on, not a speculative one. Everything else stays as top-level modules under `src/syncmill/`; don't carve out further subpackages until they demand it the same way.
- **memtomem is the default PyPI memory dependency; mem0 and LangGraph SQLite are optional extras.** Use the runtime adapters, never reimplement their storage. `memory_migration.py` is the one sanctioned maintenance exception: it owns private enumerate/import/delete primitives unavailable on the runtime contract, with fake-SDK boundary tests and required real integration smokes. **autocli** is a pattern reference only, not a dependency.
- **No `health_check` on agents.** Run, and fall back on failure. Don't add liveness probes to the agent contract. The circuit breaker (#8) is **not** an exception: it skips an OPEN agent based on its *recorded past failures*, never by calling the agent to test it — reactive memory of failure, not a probe.
- **Sync-first orchestration.** `CLIAgent` exposes *both* a sync `execute` (`subprocess.run`) and an async `aexecute` twin (`asyncio.create_subprocess_exec`, plus the `aexecute_all` / `aexecute_isolated` / `aexecute_each` fan-out helpers — the last two sharing one cancel-drain tail, `_gather_or_drain`, so they can't drift) — but the Supervisor and the sequential strategies (route, pipeline) still call only `execute` and stay fully sync. `run_async` bridges memtomem **and** the concurrent path: `CompeteStrategy`, `CouncilStrategy`, and `DecomposeStrategy` are the live consumers — they call `run_async(aexecute_isolated(...))` / `run_async(aexecute_each(...))` to fan agents out over `aexecute` (Council does so once per phase: propose, each critique round, then synthesize; Decompose runs its planner step **sync** via `execute` — a single sequential step needs no fan-out — then fans out the concurrent subtask phase via `aexecute_each` and the synthesis phase via `aexecute_isolated`). The shared-loop rule still holds: do all memory I/O strictly **before/after** each fan-out, never `run_async` from inside the gathered coroutines (that re-enters the running loop) — Council's and Decompose's per-phase `search_context`/`share_result` calls all sit between their fan-outs, never inside them. Those boundary shares are deliberately SERIAL: batching them into one `asyncio.gather` (attempted for #117, reverted) makes the batch's cancellation semantics unfixable at this layer — a cancelled share alongside a cancellation-suppressing sibling either strands orphan tasks on the reused module loop (which resume and write late on the next `run_async` entry) or wedges an unbounded drain, and no same-loop construction closes both. `MemoryBackend` carries no cancellation-cooperation requirement, and the mem0 adapter writes through `asyncio.to_thread`, so draining asyncio tasks cannot prevent a late write anyway. Share batching lives at the BACKEND layer instead: memtomem's `add_and_share` takes one lock span over the private and shared appends and indexes all of a share's copies at once (#117, `scripts/bench_share_span.py` for the before/after) — not at the strategy layer. `aexecute` + per-agent worktrees are the foundation any further concurrent strategy will await; `aexecute_each` (distinct prompt per agent) is the new shape Decompose added beside `aexecute_isolated` (one prompt broadcast to all); its optional `labels=` gives each fan-out ITEM its own live-monitor stream key via the ambient `_SINK_LABEL` ContextVar (set per item immediately before `ensure_future`, so each Task copies its own) — needed only by Decompose, whose round-robin runs one agent several times concurrently and would otherwise interleave two subtasks byte-wise into one log and one pane. Unlabeled callers are byte-identical (the key stays the agent id), and the agent contract is untouched. Don't async-ify the Supervisor or the sequential strategies until a concurrent strategy actually needs it. The two paths share success/failure classification via `_build_result`/`_failure`, so they can't drift; **both decode with `errors="replace"`** — an agent CLI emitting non-UTF-8 must never raise `UnicodeDecodeError` (a `ValueError` no handler catches, which would escape `execute` and fail the whole run); the deliberate divergences are (a) a timed-out `aexecute` reports empty output (asyncio discards a killed child's buffer) where `execute` keeps `TimeoutExpired.stdout`, and (b) `aexecute` always reads through `_communicate_teed` (which enforces the output byte budget); when a live-monitor sink is active (the `_OUTPUT_SINK` contextvar seam) it also tees each chunk there — same `TaskResult`, but the sink/log sees pre-timeout bytes on a teed timeout (the log is a live view, the result is the contract). See the live-monitor section.
- When adding an agent, **verify the real CLI's JSON output format with an actual run first** — CLIs return errors in non-obvious ways (e.g. Claude can exit 0 while returning `{"is_error": true}`, which `ClaudeAgent._is_error_output` specifically catches).
