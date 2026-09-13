# strategy_selfbench — solo vs compete vs decompose, measured

The strategies ship; their relative win rate is folklore. External evidence
(CooperBench and kin) says naive agent collaboration can roughly halve success
rates on equal total work. This smoke bench measures where syncmill's own
strategies sit on that curve, in this repo's grading style: **no LLM judge** —
every task carries a deterministic `check` script, and its exit status is the
verdict.

## Layout

- `tasks.jsonl` — 8 small, self-contained Python tasks. Each line carries the
  agent-facing `prompt`, a `reference` solution (used only to validate the
  grader, never shown to agents), and the `check` script.
- `run_selfbench.py` — validator, dry-run planner, and runner/aggregator.
- `tests/test_strategy_selfbench_example.py` — the harness's own contract, run
  by CI (pure unit: no agents, no syncmill invocation).

## Use

```bash
# 1. prove the corpus is sound (CI-safe: no agents, no network)
python examples/strategy_selfbench/run_selfbench.py --validate-checks

# 2. print the shell-quoted invocation for every cell (paste one to reproduce it)
python examples/strategy_selfbench/run_selfbench.py --dry-run

# 3. the real thing (needs the dev setup + configured agents; ~24 agent runs)
python examples/strategy_selfbench/run_selfbench.py --arms solo,compete,decompose
#    re-running the same command resumes: completed cells are skipped
```

Results land in `results.jsonl` (one row per task × arm with `success` and
`wall_s`) plus a printed win-rate table. Rows are appended **as each run
finishes**, so an interrupted sweep still leaves everything it completed —
a full sweep is 24 agent runs and can take hours. The table reads a partial
file: a cell that never ran prints `.`, and each arm's rate divides by the
runs that arm actually completed, not by the corpus size. Grading is
deterministic; wall-clock appears only as measurement data.

A check that hangs grades as a failure, not as a crash — agent-written code
loops sometimes, and one such task used to end the whole sweep. A launcher that
cannot start syncmill at all is a hard error, not a loss: recorded as one it
produced a table of honest-looking zeros.

A pass is a receipt the graded code cannot write. The check body runs inside a
trusted wrapper, and the receipt lands only after that body returns normally —
its path arrives in the environment and is removed before the body starts, and
it lives outside the work directory. Exit status is not a verdict: a module
whose import raises `SystemExit(0)` used to pass every task.

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
