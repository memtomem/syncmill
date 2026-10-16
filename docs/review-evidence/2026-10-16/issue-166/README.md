# Issue #166: local implementation evidence

Status: implemented and locally validated; uncommitted, not released. The GitHub
issue remains open. This record supplements the original review reproductions;
it does not replace their baseline measurements or claim remote CI success.

## Behavior

Artifact IDs are single path components, including compatible names such as
`run-1`. Invalid IDs, symlink directories/files, special files, hard-linked files,
and explicit manifest identity mismatches fail with `ArtifactError`. Legacy
manifests without identity fields remain supported without migration. Listings
omit invalid entries, retaining the existing behavior tracked separately by #170.

Descriptor-relative I/O covers creation, sidecars, manifest transitions, store
locks, receipts, recovery and pruning. Each operation pins opened directories
and checks their identities before reuse. Listing and pruning close per-run
handles to keep descriptor use bounded. Apply/check/rollback consume the same
verified bytes over stdin, including non-UTF8 data. CLI and TUI result readers use
the store boundary; missing TUI results remain distinct from unreadable results.

The canonical configured root and its ancestors are trusted. This is protection
against path traversal and symlink redirection, not a sandbox against arbitrary
filesystem mutation or relocation by another process with the same OS identity.

## Validation

- [Tests](tests.log): **1,381 passed, 9 skipped**, exit 0; 13 relevant modules.
  Includes 38 dedicated boundary tests, real temporary Git applications,
  rollback/receipt failure and killed-process recovery tests, fork/reentrant lock
  tests, room/workflow integration, CLI/TUI and shared lock consumers.
- [Lint](lint.log): repository-wide Ruff passed, exit 0.
- [Receipt](validation.json): source/test hashes, exact commands and platform.
- Git diff whitespace checks passed.
- Linux, remote CI, paid/live providers and opt-in terminal/backend smoke were
  not run. This local run used macOS and Python 3.12.

The initial review's `probe.py`, `results.json`, `issue-reproductions.json` and
original test logs remain byte-identical. They describe the pre-fix baseline.
