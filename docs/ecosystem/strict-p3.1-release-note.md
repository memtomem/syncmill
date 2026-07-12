# Strict P3.1 opt-in release note

SyncMill now supports opt-in `integration.toolgraph.mode = "strict"`. It runs
Toolgraph's existing advisory preflight for every participating identity and
blocks orchestration before agents or worktrees start when the selected policy
requires it.

Defaults are `unknown_policy = "block"`, `drift_policy = "warn"`, and
`unavailable_policy = "block"`. Strict additionally requires report recording,
at least one identity mapping, and non-empty unique `server::tool` candidates.
Malformed or request-mismatched evidence always blocks.

The run envelope points to a body-free aggregate manifest and records
`enforcement_decision`, evaluation count, block count, warning count, and the
common graph generation. In a board drain, the first strict block stops the loop.

Gate E approved explicit per-project opt-in after two consecutive operating GO
runs for Codex, Claude Code, and Kimi Code. The shipped default remains disabled,
Antigravity remains non-gating compatibility, and persistent-project promotion
still requires an operator-selected target. Strict does not mediate individual
MCP calls or prove graph freshness. Roll back with `mode = "advisory"` or
`enabled = false`.
