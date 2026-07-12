# Gate E and P4 operating evidence — 2026-07-12

This is the canonical body-free result for live qualified-tool and strict
operating runs. Two consecutive runs against the version-2 harness were GO for
the required SyncMill AgentRunner scope: Codex, Claude Code, and Kimi Code.
Strict P3.1 remains an opt-in preview.

Earlier evidence described two Antigravity reruns as missing both MCP calls.
That diagnosis was wrong: the calls were routed by a same-name global
`gate_e` definition into an older spool. The stale canary-only definitions were
backed up and removed. Antigravity is a compatibility surface rather than a
SyncMill AgentRunner and is now explicitly non-gating instead of being reported
as passed, skipped, unavailable, or failed.

## Tested code

| Repository | Tested commit |
| --- | --- |
| SyncMill | `017aa2fd96bbfe18510317ac967594a9fa93a1ae` |
| Tracegraph | `eeac92267e174fbb3f67f40e4d7e94d0823bd640` |
| Toolgraph harness | `64a2dd29e08dc5ecef5a5f8ddd00713085238e9e` |

Each run used a fresh Neo4j 5.26 container, disposable Git target, and private
provider workspaces. Both successful workspaces and containers were deleted;
provider stdout and stderr were not copied into either retained body-free package.

## Live provider canaries

Each required CLI called `gate_e::always_ok` and `gate_e::always_fail` exactly
once and in that order. The body-free MCP server spool was the common
terminal-outcome oracle. Both consecutive runs produced the same result:

| Provider | Version | Attempts | Result |
| --- | --- | ---: | --- |
| Codex | `0.144.1` | 1 | success and failure observed |
| Claude Code | `2.1.207` | 1 | success and failure observed |
| Kimi Code | `0.23.5` | 1 | success and failure observed |

The summary schema is version 2 and records `agy` under
`provider_scope.excluded` with classification `compatibility_only` and reason
`not_syncmill_agent_runner`. The harness did not probe or execute `agy`. The two
user-level Antigravity MCP configuration files and the old misrouted spool were
byte-identical before, between, and after the consecutive runs.

Kimi native stream-json still omits a typed tool failure; the server oracle
supplies its operational terminal outcome without parsing body text.

## Gate E strict matrix

All 9 labeled scenarios matched the expected dispatch decision:

| Scenario | Expected/observed |
| --- | --- |
| known allow | dispatch |
| expected policy exception | dispatch |
| explicit violation | block before agent |
| unknown tool | block before agent |
| drift default | warn and dispatch |
| drift block override | block before agent |
| Toolgraph unavailable default | block before agent |
| unavailable warn override | warn and dispatch |
| persisted strict plus `enabled=false` | no preflight; dispatch |

This fixed matrix had zero unexpected classifications. It is not presented as
a long-term statistical false-positive/false-negative rate.

## P4 evidence chain

- Run: `gate-e-live-001`; tool: `gate_e::always_fail`; pattern:
  `tool-failure@v1`.
- Raw OTLP digest:
  `sha256:744b6ede10e9fbe5dc7d9c834f63f5eacba5c05684225546a8cb8e3c412c383b`.
- Normalized artifact digest:
  `sha256:44b3711bdbcc67b40c8b4d0c322f346ff37d72d2ab310d6a7a1cac4f48b8962b`.
- Candidate report digest:
  `sha256:662ff2a2eef0e504bfff2740f22ddbbf49a82820a8f5c089c93fd7dad25f3f19`.
- Candidate UUID: `aa659ccc-2069-563d-9c54-4531ada90725` in both
  SyncMill and Toolgraph.
- Board import: first `imported=1`; repeat `skipped=1`; item remained
  human-required and non-runnable.
- Synthetic Toolgraph disposition: `dismissed`; annotation digest:
  `sha256:c251d055c89225cedfc310a1bec3e20d5f8b77b755ff91187fb33a50a273798e`
  in the second, canonical run. The first consecutive run recorded
  `sha256:4f5da640f05bc8cc0d910ea0c7b609731f69e17504faaab325eebc4f49a836c4`.
- Graph generation `4`, preflight, blast radius, source report bytes, and the
  independent SyncMill board state were unchanged by annotation.
- Privacy scan: pass; no prompt, args, result, stdout/stderr, credential, or
  secret sentinel was retained.

Sibling tool calls remain non-causal. This evidence closes the live
single-failure review path but does not claim `tool-retry-failure` support until
a provider supplies an explicit retry relationship.

## Verdict

- Gate E operating readiness: **GO recommendation for the required
  Codex/Claude/Kimi scope** after two consecutive runs.
- Live P4 review path: **GO**, with no automatic governance change.
- Automatic strict enablement or Gate E approval: **not performed**.
