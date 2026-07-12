# Gate E and live qualified-tool telemetry runbook

This runbook validates strict P3.1 separately from P4 review intake. A passing
run is a GO recommendation only: it does not enable strict mode by default,
approve Gate E, or change Toolgraph governance.

## Enable body-free tool telemetry

```toml
[integration.trace]
enabled = true
tool_calls_enabled = true

[integration.trace.tool_name_map.claude]
"mcp__gate_e__always_fail" = "gate_e::always_fail"
```

Codex supplies `server` and `tool` directly. Claude and Kimi MCP names are
decoded only when the `mcp__server__tool` form is unambiguous; aliases are
required for truncated or ambiguous names. Only sequence, qualified tool key,
provider source, completeness, and `succeeded|failed|unknown` are retained.
Arguments, results, exception text, prompts, stdout, and call IDs are never
written to the trace or run envelope.

Tool calls are logical children of their owning attempt and siblings of one
another. Event order does not imply retry causality. A failed call emits the
constant OTLP message `tool_failed`; an unknown Kimi outcome is not an error and
cannot produce a P4 failure candidate.

## Workspace-local hook compatibility

Kimi and Antigravity hooks may invoke the hidden, fail-open helper:

```bash
syncmill telemetry-hook \
  --provider antigravity \
  --event pre \
  --spool /private/tmp/gate-e-hook.ndjson
```

Pass the hook JSON on stdin. For Antigravity, configure matching `pre` and
`post`/`failure` hooks; SyncMill pairs `(conversationId, stepIdx)` and persists
only the canonical tool key. For Kimi use `success` and `failure`. Put any
lossy provider-name aliases in a JSON object and pass `--tool-map PATH`.
Collectors always exit successfully so telemetry cannot change an agent run;
an incomplete marker makes the operational canary fail instead.

Antigravity remains an experimental, non-gating compatibility surface, not a
fourth SyncMill agent. Hook tests do not qualify it for Gate E.

## Emergency rollback

For a persisted strict configuration, this one field disables both validation
and preflight subprocess execution:

```toml
[integration.toolgraph]
enabled = false
mode = "strict"
```

No candidate list, agent map, report, or command is required while disabled.
Changing `mode = "advisory"` is a softer rollback that keeps preflight active.

## Operational package

Run Toolgraph's `scripts/gate_e_operational.py` with explicit refs. It verifies
known allow, expected exception, explicit violation, unknown, drift defaults
and overrides, unavailable defaults and overrides, the one-switch rollback,
and the body-free Tracegraph → SyncMill board → Toolgraph review loop.

Summary schema version 2 defines Codex, Claude Code, and Kimi Code as the
required provider set. It records Antigravity (`agy`) under
`provider_scope.excluded` as `compatibility_only`; the default operational
harness never invokes or probes it. Every required provider must write exactly
one ordered success/failure pair to its current body-free spool.

Retain only the summary, raw body-free OTLP, normalized artifact, candidate
report, and annotation sidecar. Keep provider stdout, prompts, databases,
temporary hook state, worktrees, and model configuration private and delete
them after success.

The latest reconciled result is recorded in
[`gate-e-p4-operating-evidence.md`](gate-e-p4-operating-evidence.md).
