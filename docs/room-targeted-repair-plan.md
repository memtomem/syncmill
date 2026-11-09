# Targeted room repair: design for #173

Status: **APPROVED** 2026-11-09, not implemented. Baseline: main @ `132e6c4`. The proposed
answers in section 14 are the approved ones. Implementation follows #171 on the roadmap.
Issue: [#173](https://github.com/memtomem/syncmill/issues/173). Semantic boundary:
[ADR-0005](adr/adr-0005-evidence-first-review.md).

## 1. Goal

After comparing Room candidates, a user can write feedback on one candidate and run
**only the member that produced it** against that feedback. The result is a new
candidate, verified again, that the user compares and selects explicitly.

Non-goals:

- No automatic repair loop. Every repair is one human-confirmed provider call.
- No change to all-member `room round`, or to `syncmill continue`.
- No repair for ordinary `run` artifacts. ADR-0005 makes the Room candidate the unit.
- Agents never see the feedback store. They receive a prompt string, as they do today.
- Whether this saves review time stays a hypothesis for #190. The existence of a
  comment feature is not the success criterion.

## 2. Measured facts this design rests on

All of these ran on fake agents in a sandbox, on the locked environment.

**A retained worktree does not hold its candidate's bytes.** `run_round` captures the
patch and then runs the gate in the same worktree. With a gate that writes a file:

| Round | Patch digest | `files_changed` | Gate |
| --- | --- | --- | --- |
| 1 | `e04af41017` | `calc.py` | passed |
| 2 (the agent changed nothing) | `f1a6bd1257` | `calc.py`, `gate-debris.txt` | passed |

Round 2's candidate carries a file no agent wrote, and applying it put that file in the
source. This is an existing defect, [#268](https://github.com/memtomem/syncmill/issues/268).
A `python3 -m unittest` gate did not leak `__pycache__` in the same setup, because macOS's
system Python writes bytecode to `sys.pycache_prefix`.

**A candidate's tree can be rebuilt at the same path.** The worktree held an agent commit,
a binary change, a mode change, a delete, a rename, a symlink change, a file with no
trailing newline, an untracked non-ASCII path and a changed `.gitignore`. It was then left
with gate debris, ignored directories and an embedded repository. `cleanup_worktree`, then
`git worktree add` with the same branch and path, then `git apply --binary`:

| Check | Result |
| --- | --- |
| Patch digest before and after | `0be0b65e8153` both |
| Debris, embedded repository | gone |
| Ignored `.venv/` | **gone**, the cost of this approach |
| Mode | `0755` |
| Worktree registered at that path | once |

**Real sessions resume across that rebuild.** Claude Code 2.1.319 and Codex CLI 0.167.0, in
one-member rooms. Round 1's prompt holds a random codeword, then the checkout is rebuilt, and
round 2 asks for the codeword without stating it. The control round passes `--fresh-session`.

| Provider | Round 2 after rebuild | Session id | Control after rebuild, no session |
| --- | --- | --- | --- |
| Claude | `resume`, codeword returned | unchanged | `UNKNOWN` |
| Codex | `resume`, codeword returned | unchanged | `UNKNOWN` |

In both resumed runs, `note.txt` held both rounds' lines, a file left in the old checkout was
gone, and the rebuilt digest equalled the candidate's. Kimi Code was not measured, and this
used existing `room round` with the rebuild run by a script, since no targeted round exists yet.

**Hooks.** With `post-index-change` and `post-checkout` hooks in the control repository:

| Step | Firings |
| --- | --- |
| `cleanup_worktree` | 0 |
| `worktree add` and `apply` with `-c core.hooksPath=/dev/null` | 0 |
| The existing `capture_changes` | 2 |

The last row is an existing exposure, [#269](https://github.com/memtomem/syncmill/issues/269).

**Repairing in place was tried first and rejected.** `reset --hard`, `clean`, `apply` kept
ignored files, but needed a rule for each of: hooks, empty patches, embedded repositories,
and ignore rules that the candidate itself changes. When a candidate adds `.venv/` to
`.gitignore`, `reset` removes that rule before `clean` runs, so `.venv/` is deleted anyway.
The rebuild has none of those cases, and it reuses the recipe `WorktreePool.acquire` already
runs for `branch-from`.

**Every round path assumes all members.** `run_round` iterates `room["members"]`.
`round_conversation_status` requires entries for exactly the members its caller passes,
and its three callers pass the room's. `room_compare._missing_members` reports any
member with no candidate as `no_candidate`. `--fresh-session` calls
`clear_member_sessions`, which clears every member.

**Round prompts reach memory.** `run_round` searches memory with the round prompt, and
`apply` builds `Task(prompt=round_row["prompt"])` for `share_result`, which stores it
verbatim. So anything rendered into `rounds.prompt` reaches memory on apply.

## 3. User flow

```text
syncmill room compare ROOM
syncmill room feedback add CAND --kind change [--file PATH] --text "..."
syncmill room feedback add CAND --kind question --text "..."
syncmill room feedback list ROOM [--candidate CAND]
syncmill room repair CAND --preview
    # target member/provider, one provider call, session mode, gate settings,
    # the exact requests and prompt size, then a confirmation token
syncmill room repair CAND --confirm TOKEN
syncmill room compare ROOM --round N
syncmill room diff NEW --against CAND
syncmill room feedback resolve FEEDBACK addressed|not-addressed|withdrawn --on NEW
syncmill room select ROOM NEW
```

`feedback add`, `list` and `resolve` make no provider call. `repair` without `--preview`
or `--confirm` is a usage error, so nothing can start a provider without a preview.

## 4. Feedback store

A new append-only table, following `room_judgments`:

```sql
CREATE TABLE IF NOT EXISTS room_feedback (
    id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    record_type TEXT NOT NULL,           -- request | resolution
    request_id TEXT REFERENCES room_feedback(id),  -- set on a resolution
    sequence INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(room_id, sequence)
);
```

`request_id` lives outside the JSON so that a resolution whose body is damaged can still
be attributed to its request.

Record bodies, both with `schema_version`:

- `request`: `patch_sha256`, `kind` (`change` or `question`), `text`, `anchor`,
  `carried_from`, `surface`.
- `resolution`: `status` (`addressed`, `not_addressed`, `withdrawn`),
  `evaluated_candidate_id`, `evaluated_patch_sha256`, `surface`.

Rules:

- **A resolution binds to evidence.** `addressed` and `not_addressed` need an evaluated
  candidate in the same room, of the same member as the request's candidate, from a later
  round, in state `completed`, and its digest is recorded. The writer refuses anything else,
  and `feedback_status` reads a record that fails these checks as `unknown`. `withdrawn` has
  no evaluated candidate, and both fields are null.
- **Status is derived, never stored.** One validator, `feedback_status`, shared by the
  writer and every reader, returns `open`, `sent`, `addressed`, `not_addressed`,
  `withdrawn` or `unknown`. `sent` means a repair round's valid provenance names the
  request **and** that round produced a completed candidate. A request named only by
  failed rounds reads `open`, so a retry sends it again by default. A damaged latest
  record reads `unknown`, never an older status, the rule `room_outcome_status` follows.
- **Only a human resolves.** Nothing parses an agent's output for a claim of having
  addressed a request. Resolution needs an explicit `resolve`.
- **Anchors bind to the patch.** An anchor is `null` or `{"path": P}`, where `P` must be
  in that candidate's `files_changed`. Candidate artifacts are immutable, so an anchor
  never goes stale on its own candidate.
- **Nothing carries forward by itself.** Requests stay on the candidate they were
  written against. `room feedback carry FEEDBACK --to NEW` writes a new request on
  `NEW`, keeping the anchor only if the path is still in `NEW`'s `files_changed` and
  otherwise making it file-less, and says which happened. Positions are never
  reinterpreted against another patch.
- **No revision change.** Writing feedback changes neither room state nor `revision`,
  as judgments do not, so it cannot invalidate a confirmed selection.
- **Text** uses `_normalize_human_text` (control characters refused, newline and tab
  kept) with an 8 KiB limit per request. It goes through `printable_text` on display and
  never into `room_events`.
- **Memory.** SyncMill writes no feedback text to memory and searches with none
  (section 5.4). An agent can still quote a request in its output, and `apply` shares the
  selected candidate's output as it does today. That is not filtered.
- **States.** Writable in `awaiting_decision` and `selected`, listed explicitly and not
  derived from `_TERMINAL_ROOM_STATES`. Readable in every state until the room is pruned.
  `prune_rooms` deletes the rows, children before parents.

Hunk anchors are PR 5. The first version anchors to a file.

## 5. Targeted round

`run_round` gains `target: RepairPlan | None`. With `None`, and in any room's history,
all-member rounds keep writing today's v2 records. A plan excludes `lineage`,
`base_candidate_id`, `fork` and `fresh_session`.

### 5.1 Which candidate can be repaired

Checked in `preflight_round` and again inside `start_round`'s transaction:

- The room is `awaiting_decision`, with no recovery pending.
- The candidate belongs to the room and its state is `completed`. A gate failure is
  allowed, since that is a main use. A provider failure or timeout is not.
- It is its member's **newest completed** candidate. Later failed or timed-out attempts
  of that member do not disqualify it, which is what makes a failed repair retryable.
  A later completed candidate does: an older tree no longer matches the member's
  provider conversation.
- It holds no gitlink, that is, its patch has no mode `160000`. `capture_changes` records
  an embedded repository as one, and a rebuild cannot recreate the repository.
- Its `base_commit` equals the target workspace's `base_commit`.
- The artifact passes `verify_candidate_artifact`.
- The member's provider is still enabled.

`max_rounds` applies. A repair consumes a round number, which bounds repeated repair.

### 5.2 Starting tree

After the lease is held and before any prompt is built, the target's checkout is rebuilt
from the parent's patch, at the path and branch its `room_workspaces` row records:

```text
cleanup_worktree(control, worktree)
git -c core.hooksPath=/dev/null worktree add -b <branch> <path> <base>
git -c core.hooksPath=/dev/null apply --binary -   (skipped for an empty patch)
capture_changes(worktree, base)  must have the parent's digest
```

The `worktree add` and `apply` steps move out of `WorktreePool.acquire` into one helper that
both call, so repair and `branch-from` share a recipe. A mismatch or any failure goes to the
existing recovery path before a provider starts. Nothing is lost, since the parent's patch is
in its artifact.

**The cost: ignored files do not survive.** Installed dependencies and caches an agent or gate
left in the checkout are gone, as they are after `branch-from`. A gate that relies on them can
fail on the repair until the agent reinstalls them. The preview states this.

**What the digest check proves.** The tree is patch-equivalent to the parent as
`capture_changes` sees it, and that is all `apply` ever consumes. It does not prove byte
equality for content a clean or smudge filter hides from Git. Filters come from repository
configuration an agent could write, and the rebuild does not defend against that, matching the
stated trust model: the state root blocks traversal and redirection, not a same-credential
actor. The flag stops hooks during `worktree add` and `apply` only. It does not make the control
repository trusted, and the final `capture_changes` still runs hooks until that is fixed
separately.

Only the target's checkout is touched, and its path is unchanged, which keeps provider sessions
affine to it. Every member's workspace metadata is still validated, but only the target must be
live, so a damaged peer workspace does not block a repair. The next all-member round still fails
on it, as today.

### 5.3 Session

The existing seed path applies to the target alone.

| Stored session | Mode shown in preview | Prompt |
| --- | --- | --- |
| Present, conversations on | `resume` | Delta checkpoint |
| Absent, or conversations off | `cold`, with the note that the provider starts without the earlier conversation | Full checkpoint |
| Present, `--cold` passed | `cold`, explicit | Full checkpoint |

`--cold` clears only the target's session, through `clear_member_session`. It never
calls `clear_member_sessions`.

A resume the provider rejects is **not** retried cold. It becomes a failed candidate,
and the parent stays the member's newest completed candidate, so `room repair CAND --cold
--preview` works next. Its token differs because the session differs.

### 5.4 Prompt

The rendered batch is **not** stored in `rounds.prompt`, which is what keeps feedback out
of memory:

- `rounds.prompt` holds the user's optional free text, or else the fixed line
  `Repair candidate CAND`.
- Memory is searched with the room's original prompt, not the round's.
- The agent's task section is rendered at round start from the plan's feedback records:

```text
Repair your candidate CAND. Your working tree was rebuilt from exactly that
candidate's patch. Ignored files, such as installed dependencies, are not there.

Change requests:
1. [calc.py] ...

Questions (answer these in your response):
1. ...
```

The rendering is deterministic, and its digest is recorded (section 5.5), so a preview,
its confirmation and the worker agree on the bytes. A batch that does not fit
`_ROOM_PROMPT_MAX_BYTES` beside the selected lessons is refused at preview, never cut.
Lessons work as they do in `room round`, including `--skip-lesson` and `--no-lessons`.

By default the batch is the candidate's `open` requests. `--feedback ID` (repeatable)
narrows it, and each id must be `open` on this candidate. An optional free-text prompt may
be added. A repair with no requests and no prompt is refused.

There is no resend. A request reads `sent` only after a repair completed, and by then the
parent is no longer its member's newest completed candidate. If the new candidate did not
meet the request, `room feedback carry` moves it there as a new `open` request.

### 5.5 Records

**Only a repair round writes provenance v3.** It adds a `repair` lineage and tree kind,
legal only at version 3:

```json
{"kind": "repair", "member_id": "...", "parent_candidate_id": "...",
 "parent_patch_sha256": "...", "parent_round_number": 3,
 "feedback_ids": ["..."], "agent_task_sha256": "..."}
```

`round_provenance_status` validates it from inputs it already receives: the member is the
room's, the parent is that member's completed candidate from an earlier round with the
same digest, and no later completed candidate of that member precedes this round.
`start_round` also validates, in the transaction that inserts the round, what that
function cannot see: the confirmed plan (section 6). `feedback_status` treats a feedback
id that does not resolve to a request on the parent candidate as `unknown`.

**All-member rounds do not change.** They keep writing v2 records with today's rule. After
a repair, a peer that sat out has no candidate in `round_number - 1`, so `_continued_parents`
records `None`, meaning not recorded, which v2 already allows. `starts_from` reports it as
`{"kind": "continued", "parent_candidate_id": null, "parent_round_number": null}`. This loses
one piece of lineage display for those peers. It keeps a
validator rule, and the participation history it would need, out of this change. The
target's parent in the next round is the repair candidate, as usual.

**Conversation records do not change version.** One helper,
`round_expected_members(room, provenance_check)`, returns the target alone for a round
whose provenance is a valid repair, and the room's members otherwise. The three callers of
`round_conversation_status` pass it instead of `room["members"]`. So the entry set is
bound to the single target recorded in provenance, a repair's conversation record is a v2
record with one entry, and a one-entry record on an all-member round stays malformed.

A peer's next delta needs no new rule: its watermark did not move, and the checkpoint
selects `round_number >= watermark` excluding its own, so the repair's output reaches it.
A test pins this.

### 5.6 Gate and selection

The gate runs on the new candidate through the existing loop, with settings recorded by
that round. Nothing is inherited from the parent. Selection, apply, `room judge` and
`room promote` need no change, since a repair candidate has the same row and bindings as
any other and shares the room's fingerprint and snapshot commit.

## 6. The repair plan

The preview builds one immutable document, the **repair plan**, and its token is the
SHA-256 of the canonical JSON, following `room_raw.preview_token`:

- room id and `revision`
- candidate id, patch and output digests
- member, provider, and the provider's effective execution settings as a digest of its
  `agent_config`, which covers timeouts and turn limits
- session mode and a digest of the stored session id
- each feedback id with its record digest and derived status
- the rendered task digest, lesson ids, and gate settings
- a provider call count of 1

`--confirm TOKEN` rebuilds the plan and proceeds only on an equal token.

**PR 1 runs in the foreground only,** so a refusal is an error the user sees at once. A
detached worker discards its output (`launch_room_worker`), and a refusal in `start_round`
inserts no round, so a detached refusal would look like a launch still pending. PR 4 adds
`--detach` with the TUI, because orchestration never runs inside Textual. There the plan,
not the token, travels to the worker: the free text and lessons over anonymous descriptors as
they do today, the rest as JSON over a third descriptor. The worker records a body-free
`repair.refused` room event with the reason, which `room show` already prints, and the launcher
says to look there if no round appears.

`start_round` re-derives what can change between confirm and claim, inside the transaction
that inserts the round: `revision`, the parent rules in 5.1, the stored session digest, and
each feedback id's status and record digest. Any difference refuses the round before a
provider starts. A request added after confirmation is not in the plan and stays `open`.
Every round and selection bumps `revision`, so a stale preview and a second submission both
refuse.

## 7. Read models

- `room_compare` shows a repair round as targeted, with its parent and target member, and
  warns if a repair round holds candidates or `round_members` rows for anyone else.
- `_missing_members` expects only the target when the round's record is a valid repair. A
  malformed record keeps today's behaviour and warning, so damage stays visible.
- `room compare --json` gains additive fields: `repair` per round (member, parent id and
  digest, feedback count) and per-candidate feedback counts by status. `starts_from` gains
  the kind `repair`. **No feedback text** enters the body-free document or the default
  export. Raw export of feedback waits for a later `room_raw` item kind.
- `room diff NEW --against OLD` shows the tree delta. It needs both candidates on the same
  `base_commit` and a live control repository: build each tree with a private index and
  `git apply --cached`, treating an empty patch as the base tree, then `git diff --binary`
  the two trees. Otherwise it prints "delta unavailable" and why, and never falls back to
  comparing patch text. It is built on request, never on polling.

## 8. Failure behaviour, against #173's acceptance criteria

| Situation | Result | Next action printed |
| --- | --- | --- |
| Repair runs | One provider call; peers' rows, artifacts and worktrees untouched; source untouched | Compare, diff, select |
| Provider error or timeout | New failed candidate; parent and feedback intact; requests read `open` | `room repair CAND --preview`, or with `--cold`. If `max_rounds` is used up: select an eligible candidate, or `room close ROOM` then `room create` |
| Ctrl-C | Room goes to `recovery_required`, as any interrupted round does. Parent and feedback stay readable, but **no candidate can be selected** | `room close ROOM`, then `room create`; `room diff` still prints every patch (open decision 4) |
| Worker killed outright | Room stays `running` until its lease expires, 15 minutes by default, and `room close` refuses meanwhile. Parent and feedback stay readable | After expiry, `room recover ROOM`, then as for Ctrl-C |
| Request not met by a completed repair | The request reads `sent` on the old candidate | `room feedback carry FEEDBACK --to NEW`, then repair `NEW` |
| Duplicate submission | Token refused, since `revision` moved | Preview again |
| Lost session | Failed candidate, never a silent cold start | `--cold --preview` |
| Source drift | Room goes `stale` through the existing check; feedback readable until pruned | `room create` |
| A newer completed candidate exists | Refused before any state change | Repair the newer one; if the older one is eligible, `room round --lineage branch-from` |
| Anchor path missing on carry | File-less request, stated at carry time | Edit or withdraw |
| Room `selected` | Refused, as `room round` is today | `room close ROOM`, then `room create` (open decision 2) |

## 9. Delivery

Each PR is usable alone and gets the implementation cross-review.

1. **Targeted engine.** Shared rebuild helper, plan and token, `run_round(target=)`, provenance
   v3, `round_expected_members`, `_missing_members`, and `room repair CAND --prompt ...
   --preview / --confirm`.
2. **Feedback.** Table, `feedback_status`, `feedback add/list/resolve/carry`, batch prompt.
3. **Delta.** `room diff --against`, compare fields.
4. **TUI and `--detach`.** Rooms actions, using the same seams and rules as `j`/`p`, and the
   detached path with its `repair.refused` event (section 6).
5. **Hunk anchors,** only if #190's evidence asks for them.

Implementation stays behind #171 under the roadmap's order; this design need not.

## 10. Compatibility

- Additive table. No backfill.
- v1 and v2 records keep their rules byte for byte, and all-member rounds keep writing v2.
  A repair's v3 provenance read by an older binary shows as malformed and is not believed.
- An older binary's `prune_rooms` would fail its foreign key on a room with feedback rows,
  and the room stays. This is the same limit `room_judgments` introduced.
- `kind: syncmill.room-compare` stays `schema_version` 1 if the new `starts_from` kind counts
  as additive. Review should challenge this.

## 11. Separate from this design

Both are existing behaviour, and this design fixes neither.

- **[#268](https://github.com/memtomem/syncmill/issues/268): gate debris.** A gate's files enter
  a `continue-own` round's next patch, and every `run` strategy's winner patch. Repair is immune
  because it rebuilds first.
- **[#269](https://github.com/memtomem/syncmill/issues/269): repository-local hooks run during
  capture.** `isolated_git_env` strips ambient configuration but not hooks in the control
  repository, so `capture_changes` runs them after an agent's turn.

## 12. Tests

- Measured probes become tests: the rebuild digest across the change kinds in section 2, an
  empty patch, an embedded repository, a `.gitignore` change in each direction, hooks not firing
  during `worktree add` and `apply`, and a gate that writes a file before a repair.
- `branch-from` behaves the same after its recipe moves into the shared helper.
- Resolution: a candidate from another room, another member, an earlier round or a failed state
  is refused by the writer and reads `unknown` when stored directly.
- All-member argv, prompts and records pinned identical, including after a repair.
- One-candidate repair: exactly one provider process; peers' artifact bytes, worktree status
  and session ids unchanged.
- `--cold` keeps peers' session ids. Remove the targeted call and watch that test fail.
- Retry: a failed repair leaves the parent repairable, requests `open`, and `--cold` works.
- Carry: after a completed repair, including one whose gate failed, the old candidate is refused
  and a carried request is `open` on the new one.
- A killed worker: the room stays `running`, `room close` refuses, and after lease expiry
  `room recover` then `room close` succeed.
- PR 4: a feedback resolve between confirm and claim leaves a `repair.refused` event under
  `--detach`.
- Peer delta after a skipped round carries the repair's output.
- Memory: no feedback text in the search query, or in the shared task after apply.
- Validator rows: each malformed v3 case; a `repair` tree below version 3; v1 and v2 records
  unchanged; a one-entry conversation on an all-member round malformed.
- Plan: every bound field changes the token; a second confirm refuses; a feedback resolve and
  a session change between confirm and claim each refuse the round.
- `_missing_members`: valid repair, malformed record, in-progress.
- `feedback_status`: every state, a damaged latest resolution reads `unknown` for its own
  request and leaves other requests alone.
- Prune removes feedback rows. Bodies absent from compare JSON and default export.
- Real provider smoke before release: Claude and Codex resume, one lost-session case.

## 13. Alternatives rejected

| Alternative | Why not |
| --- | --- |
| Continue the retained worktree as it stands | Measured: its bytes can differ from the candidate, so gate debris would enter the repair's patch |
| Refuse when the worktree digest differs | Refuses every repair in a repo whose gate writes a file |
| Reset and clean the checkout in place | Keeps ignored files, but needed a rule per case and still lost `.venv/` when a candidate changed `.gitignore` (section 2) |
| A new child room per repair | Loses the shared baseline that makes comparison meaningful |
| `room round --member M` | Exposes partial rounds with no binding to a candidate or to feedback |
| Store status on the request row | Mutable state that can disagree with round records |
| Name skipped peers' real parents in a new `continued` version | Needs participation history in the validator and a second rule for the same record, for one display field |

## 14. Open decisions

1. **Older candidates.** Proposed: refuse. Repairing an older candidate means its tree and the
   provider conversation disagree. A later version could allow it cold only.
2. **Repair after `select`.** Proposed: refuse, matching `room round`. There is no unselect
   today, so someone who selected and changed their mind needs a new room.
3. **Questions.** Proposed: a question goes through the same repair call, and its answer is read
   in the candidate's output. A question-only batch still produces a candidate, with an unchanged
   digest when the agent edits nothing.
4. **Crash during a repair.** Proposed: accept for now. An interrupted repair strands the room in
   `recovery_required` like any round, so a good earlier candidate can no longer be selected. A
   lease-safe recovery back to `awaiting_decision` would help every round type, which makes it
   its own change rather than part of this one.
5. **Ignored files on repair.** Proposed: accept the loss, matching `branch-from`, and state it
   in the preview and the agent's prompt. Claude and Codex resume across the rebuild (section 2).
   Kimi Code still needs measuring before PR 1 merges.
