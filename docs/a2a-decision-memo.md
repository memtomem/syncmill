# Decision memo: Google A2A applicability to syncmill

**Status: DEFER the build. No code changes now. Board states verified A2A-mappable as-is.**

This memo closes Phase 5 of the agent-team platform epic (#38): a research spike on whether
and where the [Agent2Agent (A2A) protocol](https://a2a-protocol.org/) fits syncmill. It
answers the codex pre-code NEED-RESEARCH item — confirming A2A's exact task lifecycle state
names from primary sources and mapping them to the work-board's states — and records the
decision plus the concrete triggers that would reopen it.

Research date: 2026-07-09. Facts below are grounded in the official spec, the Linux
Foundation announcements, and a hands-on probe of the official Python SDK (`a2a-sdk` 1.1.0,
installed in a scratch venv; types inspected from code, never added to this repo).

---

## 1. Grounded protocol facts

- **Spec version: 1.0.0** (stable). Released version history is 0.1.0 → 0.2.6 → 0.3.0 →
  1.0.0 ([spec][spec]). An earlier internal note said "v1.2" — that was wrong; 1.0.0 is the
  latest released spec. The official **Python SDK is versioned independently** (`a2a-sdk`
  1.1.0 on PyPI at research time; protobuf-generated types).
- **Governance:** created by Google (April 2025), donated to the **Linux Foundation A2A
  Project** ([LF launch press][lf-launch]), now under the LF **Agentic AI Foundation**
  alongside MCP. As of April 2026 the LF reports **150+ organizations using A2A in
  production** and SDKs in five languages ([LF press][lf-150]).
- **Task lifecycle states** (the NEED-RESEARCH item) — spec §6.3, verified verbatim against
  the SDK's protobuf enum:

  | v1.0 canonical (`TaskState`) | ≤0.3 JSON string | Meaning |
  |---|---|---|
  | `TASK_STATE_UNSPECIFIED` | `"unknown"` | default/unknown |
  | `TASK_STATE_SUBMITTED` | `"submitted"` | accepted, not started |
  | `TASK_STATE_WORKING` | `"working"` | actively processing |
  | `TASK_STATE_COMPLETED` | `"completed"` | terminal: success |
  | `TASK_STATE_FAILED` | `"failed"` | terminal: error |
  | `TASK_STATE_CANCELED` | `"canceled"` | terminal: canceled by client |
  | `TASK_STATE_INPUT_REQUIRED` | `"input-required"` | interrupted: needs more input |
  | `TASK_STATE_REJECTED` | `"rejected"` | terminal: refused |
  | `TASK_STATE_AUTH_REQUIRED` | `"auth-required"` | interrupted: needs credentials |

  Note the naming change: spec versions ≤0.3.0 serialized states as kebab-case strings
  ([v0.3.0 spec §6.3][spec-030]); v1.0 uses proto-canonical `TASK_STATE_*` names, and the
  SDK's proto-JSON serialization emits the full name (verified:
  `{"status": {"state": "TASK_STATE_INPUT_REQUIRED"}}`). Any future bridge should target the
  v1.0 names.
- **Transports:** three normative bindings — JSON-RPC 2.0, gRPC, HTTP+JSON/REST — plus a
  custom-binding mechanism ([spec][spec] §9–12).
- **Task shape:** `Task{id, context_id, status{state, message, timestamp}, artifacts,
  history, metadata}` — a stateful, pollable record with attached **artifacts** (outputs) and
  message history (SDK probe).
- **Agent Card:** a JSON metadata document (name, description, provider, capabilities,
  skills, security schemes, `signatures`, …) published by an A2A server for discovery;
  v1.0 supports **digital signing** ([spec][spec] §8.4; field list verified in the SDK).
- **Streaming & async:** streaming via `SendStreamingMessage`/`SubscribeToTask` and push
  notifications via client-registered webhooks — both **optional capabilities** advertised on
  the Agent Card (`capabilities.streaming` / `capabilities.pushNotifications`), so a
  non-streaming server is spec-legal ([spec][spec]).
- **Security:** API key, HTTP Basic/Bearer, OAuth 2.0, OIDC, mTLS; servers **MUST** reject
  requests with invalid or missing credentials ([spec][spec]). There is no unauthenticated
  posture for a write-capable server.
- **Relationship to MCP: complementary, not competing.** "A2A is about agents *partnering*
  on tasks, while MCP is more about agents *using* capabilities" ([a2a-and-mcp][a2a-mcp]).
  Both protocols now sit under the same LF foundation.

## 2. Board ↔ A2A state mapping (the compatibility check)

syncmill's work-board lifecycle is `planned → running → (blocked | review) → done`
(`WorkState`, `board.py`), plus the `human_required` flag. The mapping a
syncmill-as-A2A-server bridge would use:

| Board | → A2A `TaskState` | Notes |
|---|---|---|
| `planned` | `TASK_STATE_SUBMITTED` | accepted onto the board, not yet claimed |
| `running` | `TASK_STATE_WORKING` | claimed by a run (lease held) |
| `blocked` (`human_required` or not) | `TASK_STATE_INPUT_REQUIRED` | the "interrupted, needs outside help" state; board `blocked` is non-terminal (unblockable), so terminal `FAILED` would be wrong |
| `review` | `TASK_STATE_WORKING` | **deliberate:** `review` = done but **not verified** — from an external client's view the task is not complete; only verified work maps to `COMPLETED` |
| `done` | `TASK_STATE_COMPLETED` | done AND verified — the board's done-gate makes this mapping honest |

A2A states with no board row (all synthesizable at the bridge, none requiring schema
changes): `REJECTED` (bridge refuses before `board add` — validation, board disabled),
`AUTH_REQUIRED` (transport-level, pre-board), `CANCELED` (the board has no cancel state; a
bridge could mark the item `blocked` with a cancel note and report `CANCELED` from its own
task record), `FAILED` (a run failure surfaces as `blocked` — non-terminal on the board by
design, since the item can be re-dispatched; a bridge that wants terminal `FAILED` decides
that at its own layer), `UNSPECIFIED` (n/a).

**Verdict: the "keep board states A2A-compatible" action is a verified no-op.** Every board
state has a faithful A2A image; the two board concepts A2A cannot express (`review`'s
verified-vs-unverified distinction, `blocked`'s non-terminality) both degrade safely to
conservative A2A states (`WORKING`, `INPUT_REQUIRED`). One constraint for the future: if the
board ever grows a cancel/abandon state, map it to `TASK_STATE_CANCELED` rather than
overloading `blocked`.

## 3. Seam (a): syncmill-as-A2A-server — feasible, deferred

Expose the whole team as **one** remote A2A agent: an Agent Card describing syncmill; A2A
`message/send` → `board add` (+ dispatch via the `run_item` path); A2A `tasks/get` → board
read with the §2 mapping; A2A task `artifacts` ← the winner's `TaskResult.patch`/summary.
The board is a natural fit — A2A tasks are stateful, pollable records with ids, which is
exactly what board items already are, down to `human_required` ↔ `INPUT_REQUIRED`.

What it costs, and why it waits:

- **It is a write surface.** The existing MCP gateway (`gateway.py`) is deliberately
  read-only with no auth. An A2A server accepts work, so the spec's mandatory-auth posture
  applies — authn/z design (API key/OAuth/mTLS), rate limiting, and abuse handling that
  syncmill has intentionally punted on so far.
- **Streaming is optional, so the first bridge could be poll-only** (`capabilities.streaming
  = false`) — syncmill has no incremental result stream today (monitor logs are local files).
- **Architecturally it fits the established invariant class:** a non-agent boundary process
  (gateway/dashboard/session precedent) — a new flat module that touches the board and reads
  memory, never touches agents; the Supervisor stays the only component touching both agents
  and memory. Internal orchestration stays exactly as it is: A2A at the edge, never
  agent-to-agent inside (that would break agent purity).
- **No consumer exists yet.** Nothing currently needs to submit work to syncmill from
  outside the CLI/dashboard.

## 4. Seam (b): A2A-backed `AgentRunner` — structurally mismatched, further out

A remote A2A agent behind the pure `(prompt, workdir, timeout) → TaskResult` contract would
make the lineup heterogeneous (remote agents competing alongside local CLIs). The blocker is
structural: every current agent **edits a local worktree**, and `files_changed`/`patch` are
collected from that workdir after the run (`_collect_files_changed`). A remote agent has no
access to the worktree; the prompt alone cannot carry a repository.

A bridge would have to invert the flow: send the task (and whatever context fits in message
parts), receive **artifacts** (A2A's native output channel), and apply a returned diff to the
local worktree itself — an artifacts-as-patch adapter. Feasible, but it changes the trust
model (applying remote-authored diffs locally) and gives the remote agent far less context
than local agents get, so it would compete at a systematic disadvantage. The gate/breaker
plumbing would work unchanged (both act on the raw result status and the worktree). Verdict:
only worth building when a concrete remote/heterogeneous lineup is actually wanted — after
seam (a), if ever.

## 5. Decision

**DEFER.** No A2A code in v0.4.x. Action now = this memo only; §2 verified that keeping the
board A2A-mappable requires no change. Internal agent-to-agent coordination remains
permanently out of scope — it would violate the isolation invariant, and A2A itself is an
edge protocol, not an internal bus.

Revisit triggers (any one reopens the question, seam (a) first):

1. An external system/agent actually needs to **submit work to syncmill** remotely (not just
   read — the MCP gateway already covers reading) → build seam (a).
2. A concrete **remote agent worth adding to the lineup** appears (e.g. a hosted specialist
   agent exposed over A2A) → assess seam (b) with the artifacts-as-patch adapter.
3. The orchestrated CLIs (`claude`/`codex`/`kimi`) gain **native A2A endpoints**, changing
   the integration cost.
4. The LF **MCP/A2A convergence** work produces something that affects the existing gateway
   (both protocols now share a foundation).

## Sources

- [A2A Protocol Specification (latest = 1.0.0)][spec] — states, transports, Agent Card,
  security, streaming/push capabilities, version history.
- [A2A Specification v0.3.0][spec-030] — the pre-1.0 kebab-case state strings.
- [A2A and MCP][a2a-mcp] — the complementarity framing.
- [Linux Foundation launches the Agent2Agent Protocol Project][lf-launch] — governance.
- [LF: A2A surpasses 150 organizations][lf-150] — production adoption.
- `a2a-sdk` 1.1.0 (PyPI) — hands-on probe of `a2a.types` (protobuf `TaskState`,
  `Task`/`TaskStatus`/`AgentCard` fields, proto-JSON serialization), 2026-07-09.

[spec]: https://a2a-protocol.org/latest/specification/
[spec-030]: https://a2a-protocol.org/v0.3.0/specification/
[a2a-mcp]: https://a2a-protocol.org/latest/topics/a2a-and-mcp/
[lf-launch]: https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents
[lf-150]: https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year
