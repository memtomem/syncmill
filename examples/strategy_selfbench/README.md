# strategy_selfbench — solo vs compete vs decompose, measured

The strategies ship; their relative win rate is folklore. External evidence
(CooperBench and kin) says naive agent collaboration can roughly halve success
rates on equal total work. This smoke bench measures where syncmill's own
strategies sit on that curve, in this repo's grading style: **no LLM judge** —
every task carries a deterministic `check` script, and the verdict is whether
it runs to completion and exits cleanly.

## Layout

- `tasks.jsonl` — 8 small, self-contained Python tasks. Each line carries the
  agent-facing `prompt`, a `reference` solution (used only to validate the
  grader, never shown to agents), and the `check` script.
- `run_selfbench.py` — validator, dry-run planner, and runner/aggregator.
- `tests/test_strategy_selfbench_example.py` — the harness's own contract, run
  by CI (no agents, no syncmill invocation). One test runs real syncmill with a
  fake agent binary: `SYNCMILL_TEST_SELFBENCH_REAL=1`.

## Use

```bash
# 1. prove the corpus is sound (CI-safe: no agents, no network)
python examples/strategy_selfbench/run_selfbench.py --validate-checks

# 2. print the shell-quoted invocation for every cell (paste one to reproduce it)
python examples/strategy_selfbench/run_selfbench.py --dry-run

# 3. the real thing (needs the dev setup + configured agents; ~24 agent runs)
python examples/strategy_selfbench/run_selfbench.py --arms solo,compete,decompose
#    re-running the same command resumes: completed cells are skipped
#    an agent CLI you do not have installed: add --disable-agent kimi-code
```

Results land in `results.jsonl` (one row per task × arm with its `outcome`,
`success` and `wall_s`) plus a printed win-rate table. Rows are appended **as each run
finishes**, so an interrupted sweep still leaves everything it completed —
a full sweep is 24 agent runs and can take hours. The table reads a partial
file: a cell that never ran prints `.`, and each arm's rate divides by the
runs that arm actually completed, not by the corpus size. Grading is
deterministic; wall-clock appears only as measurement data.

Each cell gets one outcome. `pass`, `wrong-answer`, `timeout` and
`strategy-failed` are scored. `infrastructure-error` is counted in its own
column and left out of the rate, because it is not a strategy losing. The
outcome comes from evidence the harness collects itself, never from reading
stderr:

- **Launch receipts.** Each agent binary on the cell's PATH is a shim that
  records its pid and then execs the real CLI, so the agent keeps the pid,
  signals and exit status syncmill sees. No receipt means no agent ran. A
  compete cell must launch every enabled agent and a solo cell its pinned one.
- **syncmill's run record.** The `manifest.json` every run writes says whether
  it finished (`pending` is graded, `no_changes` is a `wrong-answer`, `failed`
  is `strategy-failed`) or did not (`running`, `recovery_required`).
- **The deadline.** A cell past `--timeout` is a scored `timeout` only if an
  agent's process group is still alive at that moment; if every agent had
  already exited, syncmill stalled after them and the cell is infrastructure.

Three infrastructure errors in a row stop the sweep, keeping the rows already
written: a launcher that breaks mid-sweep is caught the same way as one that
never started. The lineup is part of the experiment: every agent binary
syncmill would enable must be installed, and `--disable-agent ID` leaves one
out of every cell and records that in the header.

What this cannot see: a provider that rejects a launched agent (an expired
token, a rate limit, an outage) still dispatched it, so that cell is scored as
`strategy-failed`. A deadline that lands between two agent phases reads as no
agent running, and a launch that fails inside syncmill before the shim starts,
in a strategy other than solo or compete, leaves no trace.

The file opens with an experiment header: a digest of the loaded corpus, the
argv of every arm in `ARMS`, `--timeout`, the grader's timeout and the
launcher, and the agents left out with `--disable-agent`. A resume must
match it exactly, so editing a task's check, an arm,
or the timeout and re-running into the same file is refused rather than
reported with the old cells' verdicts. Use a new `--out` for the new
experiment. A file written before the header existed is refused the same way,
since nothing records what produced it. Selecting a different subset of arms
is still a resume. The header does not cover everything a verdict depends
on: a new agent CLI version or model behind the same argv, a change to
syncmill itself, to the grading wrapper, or to the Python that runs the checks
all resume as the same experiment.

A check that hangs grades as a failure, not as a crash — agent-written code
loops sometimes, and one such task used to end the whole sweep. A launcher that
cannot start syncmill at all is caught by a probe before the first cell, not
recorded as losses: recorded as them it produced a table of honest-looking
zeros.

A cell that runs past `--timeout`, or a sweep you stop with Ctrl-C, gets
SIGINT first. syncmill starts every agent in a session of its own, so only
syncmill can stop them, and SIGINT is the signal it handles. The cell then has
15 seconds before its process group is killed. An interrupted cell writes no
row, so the resume runs it again from scratch. A launcher that ignores SIGINT
still leaves its agents running after that kill; only a container prevents it.

A pass is a receipt. The check body runs inside a trusted wrapper, and the
receipt lands only after that body returns normally. That stops the accidental
passes exit status allowed: a module whose import raises `SystemExit(0)`, or
one that prints `ok`, used to pass every task. The receipt is not a boundary
against the graded code. Its path is removed from the environment and kept
outside the work directory, but code that walks the interpreter's frames can
still read it.

A pass also needs a clean exit. Whatever runs after the body counts against
it: an `atexit` hook that exits non-zero, or a thread that keeps the interpreter
from shutting down before the timeout. That can understate a strategy in rare
cases. The receipt alone would be worse, because a body that forks can hand its
pass to the child while the parent fails.

Grading runs agent-written Python. It gets a scratch `HOME`, an allowlisted
environment with no credentials in it, and its own process group so a fork that
outlives the timeout is killed with its children — but **this is not a sandbox**:
the code can still reach the filesystem and the network. Run the sweep with
agents you would already trust to write into this repo, or put the whole thing
in a container.

Each cell runs with its own `HOME` and its own syncmill/memtomem state
directories. That is not only hygiene: syncmill copies a successful result —
prompt and output included — into the shared namespace, and the sweep runs one
prompt through three arms in a row, so on the ambient store the second arm can
retrieve the first arm's answer. Provider credentials are the one thing passed
through, since without them no agent runs.

## Reading the numbers honestly

Eight tasks is a smoke corpus, not a benchmark: it can tell you "decompose
lost to solo here, look closer", never "strategy X is better in general".
Grow the corpus (and re-run `--validate-checks` — every grader must pass its
reference AND fail the empty seed) before believing small deltas. If an arm
beats solo here, the interesting question is *why*; if it loses, that is the
CooperBench result reproducing locally, and worth a design note.
