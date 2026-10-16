# Issue #166: independent-review corrections

This follow-up addresses the independent review of commit `b07866b` recorded in
`codex-20261016-220056-28300.review.json` (one Major, six Minor/Nit findings).
Earlier implementation receipts describe the working tree committed in `b07866b`;
their source hashes must be compared with that commit, not this follow-up.
Original reproduction outputs and previous test logs are preserved.

## Changes and decisions

- `doctor` handles `ArtifactError` from the real store constructor and reports
  the fatal diagnostic without an uncaught traceback.
- `runs list` handles unavailable stores as Click errors. The dead existence
  guard in decorated `list()` is removed. The guard in the **undecorated**
  `reconcile_interrupted_applies()` remains reachable and is retained.
- Each decorated store operation releases the directory handles it acquired.
  Public `load()` therefore does not accumulate run descriptors inside an outer
  apply lock. Internal `_load_pinned()` keeps mutation handles alive until the
  enclosing operation completes, preserving replacement detection and CAS.
- Ordinary duplicate and missing-file conditions have specific messages rather
  than being labelled unsafe access. Missing optional TUI results remain distinct
  from unreadable results.
- The unused path-based `_atomic_write` is removed. The private-file permission
  regression now exercises the production descriptor-relative writer.
- Root identity mismatches remain fail-closed. The error and architecture notes
  instruct users to restart/reopen after restoring the intended store; a live
  store never silently adopts a replacement directory.
- The TUI result helper requires a real store, and tests use the same path as
  production. Its test-only filesystem fallback is removed.

## Validation

The regression set includes a subprocess with `RLIMIT_NOFILE=256` reading 300
runs and reconciling them inside `apply_lock`, real-constructor doctor failures,
removed/replaced-root handling, duplicate/missing messages, and all original
path-swap, patch-swap, rollback and crash-recovery scenarios.

See [test output](tests.log), [lint output](lint.log) and the source-bound
[validation receipt](validation.json). The receipt records exact counts, commands,
platform and hashes. Linux, remote CI and live providers were not exercised.
