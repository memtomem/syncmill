#!/usr/bin/env python3
"""Measure whether multi-agent strategies actually beat solo on small tasks.

The strategies exist; their relative win rate on real work is folklore. This
harness turns "compete feels better" into a number, in the repo's own grading
style: no LLM judge, each task carries a deterministic ``check`` script whose
exit status is the verdict. The published external evidence (CooperBench and
kin) says naive agent collaboration can HALVE success rates — this bench is
how syncmill finds out where its own strategies sit on that curve.

Honesty box: eight tasks is a smoke corpus, not a benchmark. It can tell you
"decompose loses to solo here, look closer"; it cannot tell you a strategy is
better in general. Grow the corpus before believing small deltas.

Modes:
  --validate-checks   run every task's check against its embedded reference
                      solution (no agents, no syncmill) — proves the corpus is
                      solvable and the graders are not broken. CI-safe.
  --dry-run           print the shell-quoted syncmill invocation for every
                      (task, arm) cell, without executing any. Only the scratch
                      repo path is symbolic — everything else is verbatim, so a
                      single cell can be reproduced by hand.
  (default)           for each (task, arm): make a scratch git repo seeded
                      with an empty mod.py, run syncmill with the arm's
                      strategy, apply the emitted patch, run the check, and
                      record {task, arm, success, wall_s} to results.jsonl;
                      then print a win-rate table.

Arms (edit ARMS below to taste):
  solo       route strategy pinned to a single agent — the baseline
  compete    all configured agents race, winner's patch is graded
  decompose  task is split and reassembled

Wall-clock timing lives only in results.jsonl (it is measurement data, not a
grading input). Grading itself is deterministic.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
TASKS = HERE / "tasks.jsonl"

ARMS: dict[str, list[str]] = {
    "solo": ["--strategy", "route", "--agent", "claude"],
    "compete": ["--strategy", "compete"],
    "decompose": ["--strategy", "decompose"],
}

# `uv run` resolves the project from the CWD, and every cell runs inside its own
# scratch repo — from there `syncmill` is simply not on the path, and the bench
# recorded "Failed to spawn" as an ordinary loss. Pinning --project makes the
# launcher independent of where the cell happens to run.
SYNCMILL = ["uv", "run", "--project", str(REPO_ROOT), "syncmill"]
CHECK_TIMEOUT_S = 60
# uv's own "I could not start the program" message. Distinguishing it from a
# strategy failing is the difference between an error and a data point.
LAUNCH_FAILURE_RE = re.compile(r"Failed to spawn|No such file or directory|command not found")


def load_tasks() -> list[dict]:
    tasks = []
    for line in TASKS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            tasks.append(json.loads(line))
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)), "duplicate task ids"
    return tasks


# Grading executes code an agent wrote. That is not a sandbox — it is a smaller
# blast radius: a scratch HOME so a stray write lands in the temp dir, an
# allowlisted environment so credentials are not in reach, and its own process
# group so a fork that outlives the timeout is killed with its children. Real
# containment needs a container; the README says so rather than implying this
# is one.
_GRADER_KEEP = ("PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "TEMP", "TMP")

# The trusted half of grading. The check body runs inside it, and the completion
# receipt is written only if that body returns normally — SystemExit included in
# what stops it. The receipt's path arrives in the environment and is removed
# before the body runs, so the graded code cannot learn where to forge it, and it
# lives outside the work directory so the code cannot find it by looking either.
_WRAPPER = 'import os, runpy, sys\n_receipt = os.environ.pop("_SELFBENCH_RECEIPT")\ntry:\n    runpy.run_path("check.py", run_name="__main__")\nexcept BaseException:\n    sys.exit(1)\nwith open(_receipt, "w") as fh:\n    fh.write("done")\n'


def grader_env(home: Path, receipt: Path) -> dict:
    env = {k: os.environ[k] for k in _GRADER_KEEP if k in os.environ}
    env["HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["_SELFBENCH_RECEIPT"] = str(receipt)
    return env


def run_check(workdir: Path, task: dict) -> bool:
    """Grade one attempt. A check that hangs is a failed attempt, not a crash.

    The code under test was written by an agent, so an accidental infinite loop
    is an ordinary outcome. Letting TimeoutExpired escape ended the whole bench
    on the first one — hours of completed runs thrown away to grade a single
    task as "error".

    The verdict is the receipt, not the exit status and not stdout. A module
    whose import raises SystemExit(0) ends the check at its first line with
    status zero; one that prints a sentinel and exits forges a pass, and "look"
    ends with "ok" besides. Only the wrapper writes the receipt, and only after
    the check body returns.
    """
    (workdir / "check.py").write_text(task["check"], encoding="utf-8")
    (workdir / "_wrapper.py").write_text(_WRAPPER, encoding="utf-8")
    home = workdir / ".grader-home"
    home.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as receipt_dir:
        receipt = Path(receipt_dir) / "receipt"
        popen_kwargs: dict = {}
        if hasattr(os, "setsid"):
            popen_kwargs["start_new_session"] = True
        # -E ignores PYTHON* variables: under PYTHONOPTIMIZE the asserts these
        # checks are built from are stripped, and every task would grade as
        # passed for any file that merely exports the right name.
        proc = subprocess.Popen(
            [sys.executable, "-E", "_wrapper.py"], cwd=workdir,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=grader_env(home, receipt), **popen_kwargs,
        )
        try:
            proc.communicate(timeout=CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            return False
        return proc.returncode == 0 and receipt.is_file()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and anything it spawned, not just the direct child.

    Killing only the direct child leaks the grandchild AND hangs the caller:
    the grandchild inherits the stdout pipe, so communicate() waits forever
    for an EOF that never arrives.
    """
    try:
        if hasattr(os, "killpg"):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:  # pragma: no cover - Windows
            proc.kill()
    except (ProcessLookupError, PermissionError):  # pragma: no cover
        proc.kill()


def validate_checks() -> int:
    failures = 0
    for task in load_tasks():
        with tempfile.TemporaryDirectory() as td:
            wd = Path(td)
            (wd / "mod.py").write_text(task["reference"], encoding="utf-8")
            ok = run_check(wd, task)
            # the check must also FAIL on the empty seed, or it grades nothing
            (wd / "mod.py").write_text("", encoding="utf-8")
            fails_empty = not run_check(wd, task)
            # ...and on a module that exits cleanly without implementing
            # anything. Import-time SystemExit(0) ends the check at its first
            # line with status 0, which used to read as a pass.
            (wd / "mod.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            fails_exit = not run_check(wd, task)
        status = "ok" if (ok and fails_empty and fails_exit) else "BROKEN"
        if status != "ok":
            failures += 1
        print(f"{task['id']:>14}: reference={'pass' if ok else 'FAIL'} "
              f"empty-seed={'fail (good)' if fails_empty else 'PASSES (bad)'} "
              f"exit0-seed={'fail (good)' if fails_exit else 'PASSES (bad)'} -> {status}")
    if failures:
        print(f"{failures} broken graders — fix the corpus before benching",
              file=sys.stderr)
        return 1
    print("all graders validated")
    return 0


# Ambient configuration that would otherwise reach into the developer's real
# installation. Provider credentials are deliberately NOT stripped — without them
# no agent runs at all — but everything that decides WHERE state lands is.
_AMBIENT_PREFIXES = ("SYNCMILL_", "MEMTOMEM_")


def cell_env(state_dir: Path) -> dict:
    """Environment for one cell: its own home, its own state, nothing shared.

    Two things were wrong with inheriting the ambient environment. The obvious
    one is contamination — syncmill's defaults are `~/.syncmill/workspaces`,
    `~/.syncmill/projects` and `~/.memtomem/memtomem.db`, so a benchmark sweep
    wrote into the developer's real store. The subtle one invalidates the
    measurement: `share_result()` copies a successful result, prompt and output
    included, into the shared namespace, and the sweep runs the same prompt
    through three arms in a row. The second arm could retrieve the first arm's
    answer, so the arms were never independent.
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(_AMBIENT_PREFIXES)}
    home = state_dir / "home"
    home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_STATE_HOME"] = str(home / ".state")
    # `WorkspaceConfig` is singular, so the variable is SYNCMILL_WORKSPACE__ROOT.
    # The plural spelling was silently ignored and the workspace fell back to
    # the real ~/.syncmill — isolation that looked applied and was not.
    env["SYNCMILL_WORKSPACE__ROOT"] = str(state_dir / "workspaces")
    env["SYNCMILL_RUNS__ROOT"] = str(state_dir / "projects")
    env["SYNCMILL_MEMORY__DB_PATH"] = str(state_dir / "memtomem" / "memtomem.db")
    env["SYNCMILL_MEMORY__MEMORY_DIR"] = str(state_dir / "memtomem" / "memories")
    return env


def seed_repo(wd: Path, task: dict) -> None:
    (wd / "mod.py").write_text(
        f'"""Implement here. Task: {task["id"]}."""\n', encoding="utf-8"
    )
    for cmd in (["git", "init", "-q"],
                ["git", "add", "-A"],
                ["git", "-c", "user.email=bench@local", "-c", "user.name=bench",
                 "commit", "-qm", "seed"]):
        subprocess.run(cmd, cwd=wd, check=True, capture_output=True)


def load_results(out_path: Path) -> list[dict]:
    """Existing rows, so an interrupted sweep resumes instead of restarting.

    Rows that do not parse are dropped rather than fatal: the last line of an
    interrupted file can be a partial write.
    """
    if not out_path.is_file():
        return []
    rows = []
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if {"task", "arm", "success", "wall_s"} <= set(row):
            rows.append(row)
    return rows


def append_record(out_path: Path, record: dict) -> None:
    """Append one row, repairing a torn final line first.

    A crash between the write and its newline leaves a partial record; appending
    after it concatenates two objects into one unparseable line, so the repair
    has to happen on disk and not only in the reader.
    """
    if out_path.is_file():
        blob = out_path.read_text(encoding="utf-8")
        if blob and not blob.endswith("\n"):
            head, _, tail = blob.rpartition("\n")
            try:
                json.loads(tail)
            except json.JSONDecodeError:
                # Torn mid-write: drop it, or the next append fuses two objects
                # into one unparseable line.
                blob = head + "\n" if head else ""
            else:
                # Complete, just missing its newline — a real result. Truncating
                # every unterminated tail deleted these.
                blob = blob + "\n"
            out_path.write_text(blob, encoding="utf-8")
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def bench(arms: list[str], timeout_s: int, out_path: Path, dry_run: bool) -> int:
    tasks = load_tasks()
    plan = [(t, a) for t in tasks for a in arms]
    if dry_run:
        # Runnable as printed, not merely illustrative: a dry run exists so one
        # cell can be reproduced by hand. `$SCRATCH` is left unquoted so the
        # shell expands it, and the `cd` is part of the line because syncmill
        # reads the repo from the working directory — pasted from the repo root
        # without it, the run would target this checkout.
        print('# set SCRATCH to an empty git repo seeded with mod.py, e.g.')
        print('#   SCRATCH=$(mktemp -d) && git -C "$SCRATCH" init -q')
        print()
        for task, arm in plan:
            argv = SYNCMILL + ["run", task["prompt"], *ARMS[arm], "--emit-patch"]
            print(f"[{task['id']} × {arm}] "
                  + 'cd "$SCRATCH" && ' + shlex.join(argv) + ' "$SCRATCH/winner.patch"')
        print(f"\n{len(plan)} runs planned; nothing executed (--dry-run)")
        print("note: a real sweep also isolates each cell's HOME and syncmill "
              "state; these lines use your ambient configuration")
        return 0

    # Written as each run finishes, not at the end. A full sweep is 24 agent
    # runs at up to --timeout each; a crash, a hang, or a Ctrl-C on hour three
    # used to leave nothing behind at all — and then truncating the file on the
    # next start deleted exactly what the incremental write had preserved.
    results = load_results(out_path)
    done = {(r["task"], r["arm"]) for r in results}
    if done:
        print(f"resuming: {len(done)} cell(s) already in {out_path}")
    for task, arm in plan:
        if (task["id"], arm) in done:
            continue
        with tempfile.TemporaryDirectory() as td:
            # Siblings, never nested: syncmill snapshots the repo, and state
            # created underneath it changes the tree after the fingerprint is
            # taken — the capture guard then fails before any agent runs.
            cell = Path(td)
            wd = cell / "repo"
            wd.mkdir()
            seed_repo(wd, task)
            patch = cell / "winner.patch"
            state = cell / "state"
            t0 = time.monotonic()
            stderr = ""
            popen_kwargs: dict = {}
            if hasattr(os, "setsid"):
                popen_kwargs["start_new_session"] = True
            # Its own session for the same reason grading has one: a timed-out
            # cell otherwise leaves agent processes running and holding the
            # pipes, which leaks work and can hang the sweep outright.
            cell_proc = subprocess.Popen(
                SYNCMILL + ["run", task["prompt"], *ARMS[arm],
                            "--emit-patch", str(patch)],
                cwd=wd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=cell_env(state), **popen_kwargs,
            )
            try:
                _, stderr = cell_proc.communicate(timeout=timeout_s)
                ran = cell_proc.returncode == 0 and patch.is_file()
                stderr = stderr or ""
            except subprocess.TimeoutExpired:
                _kill_tree(cell_proc)
                cell_proc.communicate()
                ran = False
            # A launcher that cannot start is not a strategy losing. Recording it
            # as one produced a table of honest-looking zeros.
            if LAUNCH_FAILURE_RE.search(stderr):
                raise SystemExit(
                    f"syncmill could not be launched for [{task['id']} × {arm}]:\n"
                    f"{stderr.strip()[:500]}\n"
                    "this is an environment problem, not a benchmark result"
                )
            if ran:
                applied = subprocess.run(
                    ["git", "apply", str(patch)], cwd=wd, capture_output=True
                ).returncode == 0
            else:
                applied = False
            success = applied and run_check(wd, task)
            wall = round(time.monotonic() - t0, 1)
        record = {"task": task["id"], "arm": arm,
                  "success": success, "wall_s": wall}
        results.append(record)
        append_record(out_path, record)
        print(f"[{task['id']} × {arm}] {'PASS' if success else 'fail'} ({wall}s)")

    print(f"\nwrote {out_path}\n")
    print(render_table(results, arms))
    return 0


def render_table(results: list[dict], arms: list[str]) -> str:
    lines = ["| arm | wins | rate | mean wall_s |", "|---|---|---|---|"]
    tasks = sorted({r["task"] for r in results})
    for arm in arms:
        rows = [r for r in results if r["arm"] == arm]
        wins = sum(r["success"] for r in rows)
        mean = sum(r["wall_s"] for r in rows) / max(len(rows), 1)
        # Denominator is the runs this arm actually completed. Dividing by the
        # corpus size would quietly report a half-finished arm as having lost.
        rate = wins / len(rows) if rows else 0.0
        lines.append(f"| {arm} | {wins}/{len(rows)} | {rate:.0%} | {mean:.0f} |")
    lines.append("")
    # `.` is a cell the sweep never reached. Rendering a partial results.jsonl
    # has to work — it is now written incrementally, so an interrupted run is
    # exactly the case someone wants to read.
    by_cell = {(r["task"], r["arm"]): r["success"] for r in results}
    lines.append("per-task: " + ", ".join(
        t + "[" + "".join(
            {True: "+", False: "-", None: "."}[by_cell.get((t, a))]
            for a in arms) + "]"
        for t in tasks))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--arms", default="solo,compete,decompose",
                    help=f"comma list from {sorted(ARMS)}")
    ap.add_argument("--timeout", type=int, default=900,
                    help="per-run timeout seconds (default 900)")
    ap.add_argument("--out", type=Path, default=HERE / "results.jsonl")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate-checks", action="store_true")
    args = ap.parse_args()

    if args.validate_checks:
        return validate_checks()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        ap.error(f"unknown arms {unknown}; choose from {sorted(ARMS)}")
    if not args.dry_run and shutil.which("uv") is None:
        ap.error("uv not found on PATH — run from a machine with the dev setup")
    return bench(arms, args.timeout, args.out, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
