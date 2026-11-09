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
| Patch and output digests | B | `artifacts._digest`, `rooms._digest` (sha256 hex of the stored bytes) | artifact manifests, room candidates, workflow bundles, selections, judgments → every load | `test_patch_and_output_digests_are_plain_sha256_hex` |
| Workflow bundle digest | B | `workflow.bundle_digest` (sha256 of `result.json` NUL patch) | `commit.json`, a final artifact's plan → resume | `test_workflow_bundle_digest_is_pinned` |
| Prefixed digest spelling `sha256:<hex>` | B | `workflow._sha256` | control-plan reference → resume | `test_workflow_prefixed_sha256_spelling_is_pinned` |
| Memory migration record digest, lineage key, `source_digest`, target id | B / C | `memory_migration._record`, `_aggregate`, `_target_id` | migration snapshot and state, destination copies → apply, verify, rollback, retried write | `test_memory_migration_digests_and_target_id_are_pinned` |
| Memory migration destination binding | B | `memory_migration._target_storage_digest` | migration state → apply, verify, rollback | `test_memory_migration_destination_binding_is_pinned` |
| Path-hash project id | C | `project.project_id`, when no trusted marker exists | state directory names → every lookup | `test_path_hash_project_id_is_pinned` |
| Lesson `source_key` | C | `rooms.lesson_source_key` | `decisions.source_key` → promote-retry idempotency | `test_lesson_source_key_is_pinned` |
| Review item projection (id, task, inputs, digest) | C | `review_candidates.item_spec` | the board item → re-import provenance check | `test_review_item_projection_is_pinned` |
| Unmapped principal id | C | `planning.unmapped_principal_id` | control-plan principals → workflow resume | `test_unmapped_principal_id_is_pinned` |
| Control-plan node id, final run id | C | `planning.plan_node_id`, `workflow.workflow_final_run_id` | frozen control plan, final artifact → resume, interrupted finalization | `test_workflow_ids_are_pinned` |
| Memory namespaces, day-file names and matcher, LangGraph namespace and tag key | C | `BOARD_RUN_PREFIX`, the Mem0/LangGraph `agent_namespace_prefix`/`shared_namespace`, `_day_file`, `_namespace_day_file_suffixes`, `_LANGGRAPH_NS`, `_tag_key` | memory rows, day files, the LangGraph index → reads, GC, purge, tag filtering | `test_memory_namespace_names_are_pinned` |
| TUI job directory keys | C | `tui_jobs.project_key`, `board_key` (legacy root) | job store directory → `list_jobs` | `test_tui_job_directory_keys_are_pinned` |
| Isolated session run root and tmux name, room viewer name and ownership digest | C | `isolated_session.session_run_name`, `session_tmux_name`; `room_viewer.viewer_session_name`, `_project_digest` | session documents, tmux → path re-check, reattach, sweep | `test_session_and_viewer_names_are_pinned` |

memtomem's own agent namespace prefix comes from memtomem's configuration, not syncmill. The memtomem real smoke covers it (`SYNCMILL_TEST_MEMTOMEM_REAL=1`).

## D — stored documents read by later versions (not yet frozen)

A golden over the current writer does not protect these. A reader can stop accepting an old document while every current-writer pin still passes. Each family needs a frozen historical document passed through its actual reader, asserting the accepted interpretation or a deliberate refusal. Where an existing v1 test is sufficient, it is reused.

| Family | Documents | Readers |
|---|---|---|
| Artifacts | run `manifest.json`, `result.json`, apply receipts (`applications/*.json`) | `ArtifactStore.load` / `read_result`, `_bound_receipt` (recover) |
| Rooms | candidate `manifest.json`, `rooms.sessions_json`, `selection_json`, judgment rows; `provenance_json` and `conversation_json` (readers accept v1 and v2) | `RoomCandidateStore.load`, `_parse_sessions_json`, `selection_record_status`, `room_outcome_status`, `round_provenance_status`, `round_conversation_status` |
| Workflows | stored `spec_json`, manifest `schema_version` 1 and 3 with accounting, node `result.json` and `commit.json` | `WorkflowStore.load`, bundle verification |
| Sessions and project | isolated-session documents, project marker `.syncmill/project.json` | `IsolatedSessionStore.load` / `list`, `_marker_project_id` |
| Board | item rows and their evidence and inputs, decisions | `_row_to_item`, `_load_evidence`, `_row_to_decision` |
| Memory backends | Mem0 and LangGraph stored payloads | the backends' read paths |
| TUI jobs | job documents, `schema_version` 1 and 2 | `JobStore.list_jobs` |
| Run envelope | `envelope.json` | `contracts/run-envelope.schema.json`, `report list` / `report show` |

## Not contracts

Some keys only coordinate processes that run at the same time, and no stored record depends on them: the `teamfile` config lock path, the memory namespace lock path, and the TUI job per-target lock keys (`_target_lock_key`). They are listed here so that they are not mistaken for omissions. They are not pinned.
