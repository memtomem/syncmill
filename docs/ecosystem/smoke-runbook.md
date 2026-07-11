# Toolgraph–SyncMill–Tracegraph ecosystem smoke

This opt-in smoke closes Gate D without paid model calls or production API
changes. It crawls the real `syncmill serve` stdio gateway, runs deterministic
Codex/Kimi executable stubs in disposable Git worktrees, and ingests the
resulting route and compete OTLP exports with Tracegraph.

## Prerequisites

- Docker with access to pull/run `neo4j:5.26`
- `git`, `uv`, and Python 3.12
- local checkouts of Toolgraph, SyncMill, and Tracegraph

Docker must be running. The harness exits before cloning or mutation when it is
not available.

## Run

From the Toolgraph checkout:

```bash
uv run python scripts/ecosystem_smoke.py \
  --syncmill-root ../syncmill \
  --tracegraph-root ../tracegraph \
  --artifacts-dir /private/tmp/toolgraph-smoke-artifacts \
  --export-compete-trace
```

The harness fetches and clones detached revisions of Toolgraph `origin/main`,
SyncMill `origin/main`, and Tracegraph `origin/tracegraph-mvp` under
`/private/tmp`. It never resets or reuses the source worktrees. Neo4j runs in a
fresh container on a random loopback port and is always removed.

On success the private workspace is deleted. The artifacts directory retains
only body-free evidence: a pass summary, the two preflight artifacts, and the
compete OTLP fixture only when explicitly requested. Prompts, raw agent output,
patch bodies, stubs, databases, target repositories, and worktrees are deleted.
On failure the private workspace path is printed and preserved for diagnosis.

## Expected result

- Route: forced `kimi-code`, mapped identity `kimi`, `advisory_allow`.
- Compete: Codex has priority but sleeps; Kimi finishes first; Codex still wins.
- Codex `board_stats`: `DENY_VIOLATION` with a policy evidence path, yielding
  `advisory_warn`; the orchestration continues because preflight is advisory.
- Every run directory, envelope, preflight artifact, and OTLP span carries the
  same canonical `run_id`; envelope digests and both JSON schemas validate.
- Tracegraph ingests both traces. Compete contains run, two attempts, and a
  lossy select, with no causal edge between sibling attempts.
- The target HEAD/worktree is unchanged and no `syncmill/*` branch survives.

## Reconciled 2026-07-11 pass

| Repository/ref | SHA |
| --- | --- |
| Toolgraph `origin/main` | `db7e3293713980a133f97f886ca614a0f901d9df` |
| SyncMill `origin/main` | `d2344ca590ea093b8aaf1317934a439db1d5820e` |
| Tracegraph `origin/tracegraph-mvp` | `8a6a7bc9b1fdb400b5e8c94b5924b33a3af2a5c9` |

Body-free summary: route `kimi/advisory_allow`; compete
`codex/advisory_warn`, Codex winner, `syncmill::board_stats:DENY_VIOLATION`.

## Deferred work

Dashboard report rendering, strict P3.1, Tracegraph T3 / Toolgraph G3, and P4
remain open. The smoke is advisory evidence, not per-attempt MCP enforcement.
