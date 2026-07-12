# Gate E and P4 operating evidence — 2026-07-12

This is the canonical body-free result for live qualified-tool and strict
operating runs. The first run was GO, but two independent reruns against the
review-fixed harness were NO-GO because Antigravity did not invoke either MCP
tool. Strict P3.1 remains an opt-in preview.

## Tested code

| Repository | Tested commit |
| --- | --- |
| SyncMill | `30b9d6119d2de1a5b6f14d59c85fe27bde96e15d` |
| Tracegraph | `e6dc45839e3738804015ab2ef40ee38e6f5c21fd` |
| Toolgraph harness | `cbb934900ad49234b68893d91ec30b25a5aa6ee5` |

Each run used a fresh Neo4j 5.26 container, disposable Git target, and private
provider workspaces. NO-GO workspaces were preserved as private failure evidence;
provider stdout and stderr were not copied into the retained body-free package.

## Live provider canaries

Each CLI called `gate_e::always_ok` and `gate_e::always_fail` exactly once. The
body-free MCP server spool was the common terminal-outcome oracle.

| Provider | Version | Attempts | Result |
| --- | --- | ---: | --- |
| Codex | `0.144.1` | 1 | success and failure observed |
| Claude Code | `2.1.207` | 1 | success and failure observed |
| Kimi Code | `0.23.5` | 1 | success and failure observed |
| Antigravity CLI | `1.1.1` | 2 per run | no MCP calls observed in two reruns |

Antigravity remains a workspace-hook compatibility target rather than a
SyncMill AgentRunner. Its process exited successfully with `done`, but the
body-free server oracle remained empty, so the harness correctly returned 1 and
preserved the private workspace. Kimi native stream-json still omits a typed tool failure;
the hook/server oracle supplies its operational terminal outcome without
parsing body text.

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
  `sha256:2146fa9c94fb33614cb0263c07232893fd45221f4d6b911a9f42b1a5167c5941`.
- Graph generation `4`, preflight, blast radius, source report bytes, and the
  independent SyncMill board state were unchanged by annotation.
- Privacy scan: pass; no prompt, args, result, stdout/stderr, credential, or
  secret sentinel was retained.

Sibling tool calls remain non-causal. This evidence closes the live
single-failure review path but does not claim `tool-retry-failure` support until
a provider supplies an explicit retry relationship.

## Verdict

- Gate E operating readiness: **NO-GO** until the Antigravity canary reliably
  invokes both required MCP tools.
- Live P4 review path: **GO**, with no automatic governance change.
- Automatic strict enablement or Gate E approval: **not performed**.
