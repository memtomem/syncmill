# Persisted contracts (#267)

A **persisted contract** is a value syncmill stores that a later operation, possibly run by a later syncmill version, re-derives and compares, or reads back as a key. Breaking one makes existing records misread. Tests that create and check a record within one version cannot see the break, because a fresh record is always self-consistent.

PR #261 broke two such contracts with CI green: the source fingerprint encoding (restored, then versioned in #262) and verbatim lesson text.

## Review rule

A PR that changes a producer or a comparator listed below states whether stored values change. If they do, it **versions** the value instead of changing it in place. #262 is the model: the old encoding stays byte-identical, and new records carry a version.

A failing pin in `tests/test_persisted_contracts.py` means existing records would be misread. Do not update a pin to make it pass.

## Kinds

- **A.** Recomputed from live state and compared with the stored value.
- **B.** Recomputed from stored data and compared with the stored value.
- **C.** A derived key or id, re-derived for lookup or idempotency. A changed derivation silently forks identity: duplicates, or "not found".
- **D.** A stored document read by later versions. Its readers must keep accepting old shapes, or refuse them deliberately.

A **golden pin** is a fixed input with a hard-coded expected stored value, computed by the released derivation. It calls the same function production calls. Where the producer was inline, it is extracted so the pin and production cannot diverge.

## A, B, C — value contracts (all pinned)

Unless noted otherwise, pins are in `tests/test_persisted_contracts.py`.

| Value | Kind | Producer | Stored in → compared or used at | Pin |
|---|---|---|---|---|
| Source fingerprint, v1 and v2 | A | `worktree.repository_fingerprint` | artifact manifests, rooms, room candidates, isolated sessions, workflows → apply, recover, continue, room rounds, workflow execution | `test_fingerprint_versions.py`; versioned (`worktrees.md`) |
| Workflow spec digest | B | `WorkflowSpec.digest` (`model_dump_json(exclude_none=True)`) | `spec_sha256` in the workflow manifest, `commit.json` and the control-plan reference → resume and bundle verification | `test_workflow_lifecycle.py` golden |
| Patch and output digests | B | `artifacts._digest`, `rooms._digest`, `rooms._stream_digest`, `workflow.patch_digest` (sha256 hex of the stored bytes) | artifact manifests, room candidates, workflow bundles, selections, judgments → every load | `test_patch_and_output_digests_are_plain_sha256_hex` |
| Workflow bundle digest | B | `workflow.bundle_digest` (sha256 of `result.json` NUL patch) | `commit.json`, a final artifact's plan → resume | `test_workflow_bundle_digest_is_pinned` |
| Prefixed digest spelling `sha256:<hex>` | B | `workflow._sha256` | control-plan reference → resume | `test_workflow_prefixed_sha256_spelling_is_pinned` |
| Memory migration record digest, lineage key, `source_digest`, target id | B / C | `memory_migration._record`, `_aggregate`, `_target_id` | migration snapshot and state, destination copies → apply, verify, rollback, retried write | `test_memory_migration_digests_and_target_id_are_pinned` |
| Memory migration destination binding | B | `memory_migration._target_storage_digest` (memtomem, LangGraph, and Mem0's url, host and local-path locators) | migration state → apply, verify, rollback | `test_memory_migration_destination_binding_is_pinned` |
| Path-hash project id | C | `project.project_id`, when no trusted marker exists | state directory names → every lookup | `test_path_hash_project_id_is_pinned` |
| Lesson `source_key` | C | `rooms.lesson_source_key` | `decisions.source_key` → promote-retry idempotency | `test_lesson_source_key_is_pinned` |
| Review item projection (id, task, inputs, digest) | C | `review_candidates.item_spec` | the board item → re-import provenance check | `test_review_item_projection_is_pinned` |
| Unmapped principal id | C | `planning.unmapped_principal_id` | control-plan principals → workflow resume | `test_unmapped_principal_id_is_pinned` |
| Control-plan node id, final run id | C | `planning.plan_node_id`, `workflow.workflow_final_run_id` | frozen control plan, final artifact → resume, interrupted finalization | `test_workflow_ids_are_pinned` |
| Memory namespaces, day-file names and matcher, LangGraph namespace and tag key | C | `BOARD_RUN_PREFIX`, the Mem0/LangGraph `agent_namespace_prefix`/`shared_namespace`, `_day_file`, `_namespace_day_file_suffixes` / `_is_day_file` (syncmill's and memtomem's native shapes, matched against literal names), `_LANGGRAPH_NS`, `_tag_key` | memory rows, day files, the LangGraph index → reads, GC, purge, tag filtering | `test_memory_namespace_names_are_pinned` |
| TUI job directory keys | C | `tui_jobs.project_key`, `board_key` (legacy root) | job store directory → `list_jobs` | `test_tui_job_directory_keys_are_pinned` |
| Isolated session run root and tmux name, room viewer name and ownership digest | C | `isolated_session.session_run_name`, `session_tmux_name`; `room_viewer.viewer_session_name`, `_project_digest` | session documents, tmux → path re-check, reattach, sweep | `test_session_and_viewer_names_are_pinned` |
| Room member log path | C | `RoomStore.round_log_relpath` via `rooms._safe_member_filename` | `round_members.log_path` → the room viewer re-derives and compares it | `test_room_member_log_path_is_pinned` |
| Lesson and decision text, room goal (verbatim) | C | `rooms.round_lessons` (no normalization), `rooms.normalize_goal` (never stripped) | `decisions.text`, `rooms.goal` → round prompts; a goal matches a lesson's `scope` exactly | `test_rooms.py::test_round_lessons_pass_decision_text_through_verbatim`, `test_room_goal.py::test_a_room_keeps_its_goal_verbatim` |

memtomem's own agent namespace prefix comes from memtomem's configuration, not syncmill. The memtomem real smoke covers it (`SYNCMILL_TEST_MEMTOMEM_REAL=1`).

## D — stored documents read by later versions (#271)

A golden over the current writer does not protect these. A reader can stop accepting an old document while every current-writer pin still passes. Each family needs a frozen historical document passed through its actual reader, asserting the accepted interpretation or a deliberate refusal. Where an existing v1 test is sufficient, it is reused.

| Family | Documents | Readers | Frozen by |
|---|---|---|---|
| Artifacts | run `manifest.json`, `result.json`, `winner.patch`, apply receipts (`applications/*.json`), in every state a standalone `run` leaves them (room-, session- and workflow-origin artifacts are frozen with those families) | `runs list` / `show` / `prune`, `conversations list` / `show`, `diff`, `apply`, `recover` (`_bound_receipt`), `continue`, the TUI artifact view | `test_frozen_artifacts.py` (v0.1.2, v0.3.0) |
| Rooms | the room database (`rooms.sqlite3`, with the WAL a killed process leaves): rooms, rounds, candidates, members, workspaces, events, `sessions_json`, `selection_json` v1, `provenance_json` v1/v2, `conversation_json` v1; candidate `manifest.json`, patch and output; room-bound run artifacts. No release wrote `provenance_json` v3, `conversation_json` v2 or judgment rows | `room list` / `rounds` / `show` / `candidates` / `diff` / `compare` / `export`, `select`, `apply`, `judge`, `recover`, `close`, `prune` `RoomStore.member_sessions` (and through these `RoomCandidateStore.load`, `_parse_sessions_json`, `selection_record_status`, `room_outcome_status`, `round_provenance_status`, `round_conversation_status`); the standalone `apply` / `recover` refusal and skip for room-bound artifacts. Not yet: a HEAD round continuing a released room, the room viewer's log paths, `apply --verify` settings notices, raw export, the TUI room views | `test_frozen_rooms.py` (v0.1.2, v0.2.0, v0.3.0), partly |
| Workflows | stored `spec_json`, manifest `schema_version` 1 and 3 with accounting, node `result.json` and `commit.json`; workflow-origin run artifacts (v0.2.0 on) | `WorkflowStore.load`, bundle verification; for workflow-origin artifacts, the approval check at `apply` | not yet |
| Sessions and project | isolated-session documents and their checkpoint run artifacts, project marker `.syncmill/project.json` | `IsolatedSessionStore.load` / `list`, `_marker_project_id`; the artifact readers for checkpoint runs | not yet |
| Board | item rows and their evidence and inputs, decisions | `_row_to_item`, `_load_evidence`, `_row_to_decision` | not yet |
| Memory backends | Mem0 and LangGraph stored payloads | the backends' read paths | not yet |
| TUI jobs | job documents, `schema_version` 1 and 2 | `JobStore.list_jobs` | not yet |
| Run envelope | `envelope.json` | `contracts/run-envelope.schema.json`, `report list` / `report show` | not yet |

### How a family is frozen

The documents live in `tests/fixtures/persisted/<family>/<release>/`. That release's own code writes them, through its production path where one exists. A `freeze.py` beside them does this, and it runs under the release's interpreter and source: a `git worktree` at the tag plus `uv sync --frozen`. The files are copied byte for byte and never regenerated. A later release adds a directory beside the existing ones.

Some values are bound to the machine that generated them, such as the repository root and the source fingerprint. A `bindings.json` names them, and the test swaps them for its own repository's values. A fingerprint is recomputed in the stored value's own version. Nothing else is edited. For plain JSON files the swap is an exact byte replacement, and the test asserts how many occurrences it replaced. A family stored in SQLite or in a backend's native store needs its own rebinding step, because there a value's length, an index or a digest can depend on it. For the room database that step rewrites only named columns (paths under the generating directory, fingerprints, owner pids set to one that cannot be alive) and asserts that every other cell is unchanged.

The test passes each document through the reader that production uses, the CLI command or the TUI function, and asserts the result field by field. A fixture that a current reader refuses is reported, not fixed (#271 is out of scope for reader changes).

## Not contracts

Some keys only coordinate processes that run at the same time, and no stored record depends on them: the `teamfile` config lock path, the memory namespace lock path, and the TUI job per-target lock keys (`_target_lock_key`). They are listed here so that they are not mistaken for omissions. They are not pinned.
