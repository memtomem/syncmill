#!/usr/bin/env python3
"""Measure whether multi-agent strategies actually beat solo on small tasks.

The strategies exist; their relative win rate on real work is folklore. This
harness turns "compete feels better" into a number, in the repo's own grading
style: no LLM judge, each task carries a deterministic ``check`` script, and
the verdict is whether it runs to completion and exits cleanly. The published external evidence (CooperBench and
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
import contextlib
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

# The harness runs git inside a scratch repo it just created, and the README
# invokes it with a plain `python`, which need not have syncmill installed.
# Import the real routing-variable list off the source tree rather than keeping
# a second copy here: it is security-relevant and it grows (GIT_CONFIG_*,
# pathspec toggles), and a stale duplicate would isolate less than it claims.
sys.path.insert(0, str(REPO_ROOT / "src"))
from syncmill.git_env import isolated_git_env  # noqa: E402

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
# How long a cell gets to wind down after SIGINT before its group is killed.
# syncmill's own interrupt path stops its agents and finalizes the run; this
# only has to outlast that, not the work it was doing.
CELL_STOP_GRACE_S = 15
# Bound on collecting a killed process's output. A descendant in another session
# can still hold the pipes, and waiting for an EOF it never sends is a hang.
REAP_TIMEOUT_S = 5
# The launcher probe runs before the first cell, so `--timeout` does not bound
# it. Generous: a cold `uv` may build the project before it can answer at all.
PREFLIGHT_TIMEOUT_S = 300
# uv's and the shell's "I could not start the program". Distinguishing that
# from a strategy failing is the difference between an error and a data point.
#
# Deliberately NOT widened to import-failure strings. A launcher that dies
# during import reports none of these, which is a real hole — but it is closed
# by `preflight_launcher`, not here. Matching "ModuleNotFoundError" in a cell's
# stderr aborts a sweep that is working: syncmill's memory layer degrades
# rather than failing, and `SyncmillMemory._degrade` logs the caught exception
# — "ModuleNotFoundError: No module named 'mem0'" for an uninstalled optional
# backend — as a warning while the run goes on to succeed and emit a patch.
# The substring is evidence of nothing, in both directions.
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
# what stops it. That is what keeps an accidental pass out: a module that exits
# cleanly at import, or prints "ok", never reaches the receipt.
#
# It is not a boundary against the graded code. The path is popped from the
# environment and lives outside the work directory, so it is not in plain view,
# but it stays in this wrapper's globals and code that walks the interpreter's
# frames can read it. The threat model is agent-written code solving a task.
#
# `runpy` stays on purpose. Replacing it to write the receipt earlier meant
# rebuilding the `__main__` namespace by hand, and each attempt broke legitimate
# checks: pickling a check-defined class, dataclasses, `sys.argv[0]`,
# `__builtins__` as a dict. The tests pin those, so a future rewrite has to keep them.
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

    A pass needs both the receipt and a clean exit, so whatever runs after the
    body counts against it: an `atexit` hook that exits non-zero, a body that
    leaves `sys.argv` unusable for `runpy` to restore, a thread that keeps the
    interpreter from shutting down before the timeout. That understates a
    strategy in those rare cases, and it is chosen over the alternative. A
    receipt alone lets a body that forks hand its pass to the child while the
    parent fails or hangs.
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
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
            env=grader_env(home, receipt), **popen_kwargs,
        )
        try:
            proc.communicate(timeout=CHECK_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            return False
        except BaseException:
            # Ctrl-C reaches the harness, not the grader: its own session keeps
            # it out of the terminal's process group, so it would run on.
            _kill_tree(proc)
            _reap(proc)
            raise
        return proc.returncode == 0 and receipt.is_file()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and anything it spawned, not just the direct child.

    Killing only the direct child leaks the grandchild AND hangs the caller:
    the grandchild inherits the stdout pipe, so communicate() waits forever
    for an EOF that never arrives.

    The group is named by the pid, not looked up from it. Every process this
    harness kills was started as a session, so its group id is its pid, and
    that stays true after the leader is reaped. Asking `getpgid` then fails, and
    a child that outlived its parent was left running. POSIX keeps the id from
    being reused while any member of the group is alive.
    """
    if hasattr(os, "killpg"):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _reap(proc: subprocess.Popen) -> None:
    """Collect a killed process without waiting on pipes someone else holds."""
    try:
        proc.communicate(timeout=REAP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=REAP_TIMEOUT_S)


def _stop_cell(proc: subprocess.Popen) -> None:
    """End a cell so syncmill can stop its agents before it dies.

    syncmill starts every agent in a session of its own, so no signal to the
    cell's process group reaches them. A timeout used to SIGKILL that group,
    which killed syncmill before it could stop them, and they ran on while
    later cells were measured, spending tokens, rate limit, and the CPU that
    `wall_s` measures. SIGINT is the signal syncmill handles: its runner kills
    each agent's group on the way out, and `uv run` forwards it. SIGTERM would
    not do: a plain `syncmill run` installs no handler for it.

    The group is killed afterwards whatever happened, even if the leader has
    already been reaped. That is also the fallback when the launcher ignores
    SIGINT or outlives the grace period, and then its agents still leak. Only a
    container closes that.
    """
    try:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signal.SIGINT)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=CELL_STOP_GRACE_S)
    finally:
        _kill_tree(proc)
        _reap(proc)


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
    # Case-folded: pydantic-settings resolves env vars case-INSENSITIVELY, so
    # an ambient `syncmill_memory__db_path` configures the run exactly like the
    # upper-case spelling while sailing through a case-sensitive filter. That
    # is not a cosmetic miss — it points every arm at one shared memory store,
    # which is the precise contamination this function exists to prevent.
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(_AMBIENT_PREFIXES)}
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


def harness_git_env() -> dict:
    """A git environment bound to the scratch repository and nothing else.

    `isolated_git_env` drops the ambient routing and config-injection
    variables, but it keeps HOME — and git reads `$HOME/.gitconfig`, so a
    developer's own `core.hooksPath` still runs during seeding. A hook that
    rejects the commit ends the sweep; one that edits the tree changes the seed
    every arm is measured against. Both config files are pointed at the null
    device so the seed is the same on every machine.
    """
    return isolated_git_env({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    })


def seed_repo(wd: Path, task: dict) -> None:
    (wd / "mod.py").write_text(
        f'"""Implement here. Task: {task["id"]}."""\n', encoding="utf-8"
    )
    # `cwd` does not bind a git command to a repository: an ambient GIT_DIR,
    # GIT_WORK_TREE or GIT_INDEX_FILE redirects it, and GIT_CONFIG_PARAMETERS
    # or core.hooksPath makes it run someone else's script. Seeding would then
    # commit the developer's files and the sweep would grade the wrong tree.
    git_env = harness_git_env()
    for cmd in (["git", "init", "-q"],
                ["git", "add", "-A"],
                ["git", "-c", "user.email=bench@local", "-c", "user.name=bench",
                 "commit", "-qm", "seed"]):
        subprocess.run(cmd, cwd=wd, check=True, capture_output=True, env=git_env)


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
                cwd=wd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace",
                env=cell_env(state), **popen_kwargs,
            )
            try:
                _, stderr = cell_proc.communicate(timeout=timeout_s)
                ran = cell_proc.returncode == 0 and patch.is_file()
                stderr = stderr or ""
            except subprocess.TimeoutExpired:
                _stop_cell(cell_proc)
                ran = False
            except BaseException:
                # A Ctrl-C reaches the harness, not the cell, which is in a
                # session of its own. Unwinding without stopping it left syncmill
                # and its agents running, and a resume then started a second
                # copy of the same cell beside the first.
                _stop_cell(cell_proc)
                raise
            # A launcher that cannot start is not a strategy losing. Recording it
            # as one produced a table of honest-looking zeros.
            #
            # Only asked of a cell that did NOT produce a patch. Exiting zero
            # with a patch in hand is proof the launcher started, and it
            # outranks any string in stderr: syncmill's memory layer degrades
            # instead of failing and logs the caught exception verbatim, so a
            # missing optional store puts "FileNotFoundError: [Errno 2] No such
            # file or directory" on the stderr of a run that just succeeded.
            # Classifying that as an environment problem aborts the sweep and
            # throws away every cell already measured.
            if not ran and LAUNCH_FAILURE_RE.search(stderr):
                raise SystemExit(
                    f"syncmill could not be launched for [{task['id']} × {arm}]:\n"
                    f"{stderr.strip()[:500]}\n"
                    "this is an environment problem, not a benchmark result"
                )
            if ran:
                applied = subprocess.run(
                    ["git", "apply", str(patch)], cwd=wd, capture_output=True,
                    env=harness_git_env(),
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


def preflight_launcher() -> None:
    """Prove the launcher runs BEFORE any cell is graded, not after.

    Matching stderr per cell is a net with holes in it by construction — a
    launcher that dies during import prints a traceback none of the spawn
    messages appear in, and the sweep records the cell as a strategy losing.
    Asking the launcher to identify itself once is positive evidence instead
    of an absence of known strings, and it costs one subprocess per sweep.

    Lives in ``main`` rather than ``bench`` on purpose: ``bench`` is what the
    tests drive directly with a fake launcher, and a real probe there would
    make the suite reach for the network and the dev environment.
    """
    popen_kwargs: dict = {}
    if hasattr(os, "setsid"):
        popen_kwargs["start_new_session"] = True
    probe = subprocess.Popen(
        SYNCMILL + ["--version"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", **popen_kwargs,
    )
    try:
        out, err = probe.communicate(timeout=PREFLIGHT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # Unbounded, this is the one wait `--timeout` does not cover: it happens
        # before the first cell, so a stalled `uv` hangs the sweep where every
        # later stall is bounded. Its own session for the same reason a cell has
        # one — killing the direct child leaves uv's children holding the pipes.
        _kill_tree(probe)
        probe.communicate()
        raise SystemExit(
            f"syncmill did not answer --version within {PREFLIGHT_TIMEOUT_S}s\n"
            "this is an environment problem, not a benchmark result"
        )
    except BaseException:
        _kill_tree(probe)
        _reap(probe)
        raise
    if probe.returncode != 0:
        raise SystemExit(
            "syncmill could not be launched:\n"
            f"{(err or out).strip()[:500]}\n"
            "this is an environment problem, not a benchmark result"
        )


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
    if not args.dry_run:
        preflight_launcher()
    return bench(arms, args.timeout, args.out, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
