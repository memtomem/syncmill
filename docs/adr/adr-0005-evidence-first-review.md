# ADR-0005 — Connect verification evidence and human judgments

Status: **Accepted** · Date: 2026-10-16 · Accepted: 2026-10-31

Accepted by the maintainer on 2026-10-31 to unblock
[#174](https://github.com/memtomem/syncmill/issues/174), ahead of
[#173](https://github.com/memtomem/syncmill/issues/173). The first judgment
records cover Room candidates only (the comparison unit below). Promoted lessons
will reuse the board's decisions table, its scope and its supersedes link, rather
than a second rules store. The validation plan under *Consequences* still stands:
acceptance settles the semantic boundary, not the product hypothesis.

This product-wide record continues the repository-wide numbering used by the
ecosystem ADRs. See the [index](README.md) and [Korean companion](adr-0005-evidence-first-review.ko.md).

## Context

SyncMill already has isolated execution, retained Room candidates, quality
gates, explicit apply, shared memory, and board decisions. Users still assemble
candidate evidence, transfer repair requests into prompts, and remember what
they chose. Parallel execution is also available in products such as
[Conductor](https://www.conductor.build/docs). Evaluate differentiation through
**human time spent selecting and applying a reviewable change**, not feature count.

This is a proposal about product direction and evidence semantics. It changes
no current API or memory behavior and does not claim implementation completion,
validated demand, or an exclusive competitive advantage.

## Proposed decision

- Start with individual developers and small teams. Connect task entry,
  candidate comparison, repair, review, and explicit apply. The initial
  comparison unit is a **Room candidate** with a common source baseline and
  verification configuration. Do not change ordinary `compete` lazy-gate/winner
  retention contracts or imply all its executions have comparable evidence.
- Keep execution completion, gate pass, selection, successful apply, and human
  acceptance distinct. Preserve unknown for absent/unrun gates, missing usage,
  and unresolved recovery. Passing a gate does not prove complete specification
  correctness or security.
- Bind feedback to candidate ID and patch digest. Save repairs as new candidates
  without overwriting prior candidates, evidence, or selection history. Reverify
  new candidates rather than inheriting old gate results. Mark line anchors stale
  when their patch changes.
- Persist human rationale and acceptance in durable judgment records. Existing
  stores remain authoritative for execution state; memory search and model
  summaries cannot change that state. An apply receipt does not imply user
  satisfaction or production success.
- Promote only explicitly selected, user-edited lessons into project-scoped
  reusable knowledge. Reuse board-decision scope and supersedes semantics rather
  than introducing a conflicting rules system. Preserve existing task-result and
  room-result sharing, but never silently promote it to approved knowledge or
  backfill historical acceptance.
- Export local review bundles explicitly. Default to summaries, hashes, and
  verification states. Preview optional raw prompts, full logs, and patches
  before including them. Export does not publish PRs or upload to external services.

## Alternatives

| Alternative | Assessment |
| --- | --- |
| Add web Kanban, more providers, or cloud execution first | Expands support and operations without directly reducing the current review burden. Defer until concrete demand. |
| Always apply the highest LLM-scored candidate | Can hide evaluation errors and extra cost; conflicts with explicit apply and unknown states. |
| Add only a comparison screen | Useful first increment, but does not connect repair and reuse. Deliver it first within the broader workflow. |
| Connect evidence, judgments, and follow-up repairs | Reuses existing assets and offers a measurable user-time hypothesis. Recommended. |

## Consequences and validation

This document alone adds no product state or public telemetry schema. Linked
issues must specify compatibility and versions for read models, judgment
records, and feedback APIs. Keep prompts and logs out of body-free telemetry;
external usage collection remains off by default.

First build the comparison view and free trial using existing evidence, then
connect targeted repair and judgments. In an exploratory study with six users,
counterbalance current/proposed flows across three comparable tasks. Measure
review time, time to the first verified/applied change, incorrect approval, and
understanding of unknown states. A 20% time reduction is a target hypothesis,
not an observed result or statistical-significance claim. Do not advance solely
on faster completion if judgment safety worsens.

Do not enable automatic model routing from one study or biased local histories.
Preserve the existing observe → evaluate → enforce policy and the A2A deferral.

Delivery proposals: [#171](https://github.com/memtomem/syncmill/issues/171),
[#172](https://github.com/memtomem/syncmill/issues/172),
[#173](https://github.com/memtomem/syncmill/issues/173), and
[#174](https://github.com/memtomem/syncmill/issues/174).
