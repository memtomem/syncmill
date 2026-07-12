# ADR-0002 — Gate E approves opt-in strict rollout

Status: Accepted · Date: 2026-07-12

## Context

ADR-0001 defined SyncMill-owned strict enforcement but kept it opt-in pending
operating evidence. The version-2 Gate E harness subsequently completed two
consecutive body-free GO runs for the required SyncMill AgentRunner scope:
Codex, Claude Code, and Kimi Code. Both runs matched all nine strict scenarios,
kept P4 human-only and non-mutating, and verified the one-field rollback.

Antigravity is not a SyncMill AgentRunner. It remains an experimental hook
compatibility surface and is explicitly excluded from the Gate E verdict.

## Decision

- Gate E is approved for **explicit per-project opt-in** use of
  `integration.toolgraph.mode = "strict"` for the Codex/Claude/Kimi scope.
- The shipped default remains `integration.toolgraph.enabled = false`. This
  approval does not persist strict configuration in any project or enable a
  fleet-wide default.
- An opted-in project must enable report recording, declare a non-empty unique
  qualified `server::tool` candidate surface, and map every participating
  SyncMill agent to a Toolgraph identity.
- Default policies remain unknown=block, drift=warn, unavailable=block.
  Malformed, mismatched, incomplete, or cross-generation evidence always blocks.
- Antigravity hook compatibility is non-gating and cannot be represented as a
  passed, skipped, unavailable, or failed required provider.
- Toolgraph governance and P4 dispositions remain human-controlled. Gate E
  approval does not authorize automatic manifest or policy changes.

## Rollout and rollback

Rollout starts with one disposable-project R0 canary using the exact approved
provider and candidate scope. Promotion to a persistent project requires a
separate operator-selected target and review of its warnings and false blocks.
Changing the default from disabled requires another decision.

On any unexpected block or dependency failure, set
`integration.toolgraph.enabled = false`. This disables strict validation and
the preflight subprocess without requiring the remaining strict fields to be
removed. `mode = "advisory"` is the softer fallback when evidence should still
be collected.

## R0 result

The post-approval disposable-project canary completed with Gate E GO, P4 GO,
and privacy pass against SyncMill `cf0e10bc47fc201758c215802e77c14822f43d36`,
Toolgraph `64a2dd29e08dc5ecef5a5f8ddd00713085238e9e`, and Tracegraph
`eeac92267e174fbb3f67f40e4d7e94d0823bd640`. All three required providers
produced the exact success/failure pair on attempt 1. All nine dispatch
decisions matched, including the persisted-strict `enabled = false` scenario,
which skipped preflight and dispatched normally. No persistent project setting,
governance state, or default changed.
