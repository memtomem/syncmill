# Interactive collaboration (human drives one agent, syncmill runs the rest)

> Design record for the interactive-collaboration mode, moved verbatim from `CLAUDE.md`. `CLAUDE.md`
> keeps a short rule list for this mode; this file holds the full reasoning, history and test map.

Programmatic `claude` runs may be billed separately from interactive use. To keep Claude **interactive** while syncmill orchestrates the others: exclude claude from the run (`run … --exclude claude`, repeatable; or persistently via `SYNCMILL_CLAUDE__ENABLED=false`) and drive Claude yourself in the terminal. The two sides collaborate **only through the `shared` namespace** — never by syncmill invoking claude, so the agent-isolation invariant is untouched (the interactive session is a separate process, not a syncmill agent).

Two rules make it work. (1) **Same store.** syncmill's `SyncmillMemory` and memtomem's MCP multi-agent tools share the `shared` literal (exact-match namespaces), but only meet when they open the same database: an uninitialized checkout uses memtomem's `~/.memtomem/memtomem.db`, while an initialized project left at `[memory]` defaults is moved by `configure_project_memory` to `<repo>/.syncmill/memory` — so for interactive collaboration set `[memory] db_path = "~/.memtomem/memtomem.db"` and `memory_dir = "~/.memtomem/memories"` together (the user guides document this). (2) The interactive session must target `shared` **explicitly**, because its default namespace is cwd-scoped (`claude:<cwd>`), which syncmill does not read.

- Interactive session **writes** (so syncmill sees it): `mem_add(content=..., namespace="shared")`, or `mem_agent_share(chunk_id=..., target="shared")` to promote an existing chunk.
- Interactive session **reads** syncmill's results: `mem_agent_search(query=..., include_shared=True)` or `mem_search(query=..., namespace="shared")`.

**Recall is phase-specific.** General `shared` notes remain eligible during
route/compete, rooms and each strategy's initial phase. Follow-up phases read
only the artifact kinds below; a human note must carry a matching kind tag to
be eligible there. Untagged notes, rows tagged only `task-result`, and
`board-result:*` summaries are excluded from these filtered phases.

| Phase | Eligible kind tags (comma-separated OR) |
|---|---|
| Pipeline stage 2 onward | `pipeline-stage` |
| Decompose subtask execution | `decompose-plan,decompose-subtask` |
| Decompose synthesis | `decompose-subtask` |
| Council first critique | `council-proposal` |
| Council later critiques and synthesis | `council-proposal,council-critique` |

These are relevance searches, not delivery guarantees: prior runs of the same
kind still compete, and row/byte limits apply. A run's own earlier-phase
results do not depend on them — the strategy passes those to the next phase
directly, in full (#125). Boards union-read both their run
namespace and global `shared`, applying the same filter to both. Backends that
cannot filter return unfiltered context with a warning and degraded status.

- syncmill side: `share_result` writes the winner to `shared` **with `mem_agent_share`-compatible provenance** (original in `agent-runtime:{agent_id}`, `shared` copy carrying `shared-from=<chunk-id>` + a `chunk_links` row) and the tags `[task-result, {agent_id}, task:{task-id}]`; `search_context` reads `shared` (plus the agent namespace when a caller passes `agent_id`, which no strategy does today) to enrich the programmatic agents' prompts. The interactive session can pull one artifact class exactly via `mem_search(tag_filter="decompose-plan")` (or `council-proposal`/`council-critique`/`pipeline-stage`/`decompose-subtask`/`task-result`).

Collaboration is via shared *knowledge*, not file merges: the interactive session edits your real working tree while syncmill agents edit isolated worktrees, so syncmill's diff never sees the interactive edits. `CompeteStrategy` now runs the syncmill *agents* head-to-head; entering the human's interactive session as a competitor judged alongside them is still future work (the interactive session is a separate process, not a syncmill agent).

`syncmill session` (#17, below) makes this mode **durable**: instead of running the bare CLI in your own terminal (where it dies on terminal close / SSH drop), it launches that same interactive binary inside a dedicated, reattachable tmux session. The collaboration contract is identical — the session still targets `shared` explicitly (`session start` prints the `mem_add`/`mem_agent_search` reminder) — it just survives detach.
