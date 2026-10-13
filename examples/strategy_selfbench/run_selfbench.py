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
                      strategy, classify the cell from evidence the harness
                      collects itself, grade a finished patch, and record the
                      outcome to results.jsonl under a header row naming the
                      experiment; then print a win-rate table.

Outcomes: pass, wrong-answer, timeout and strategy-failed are scored;
infrastructure-error (no agent dispatched, syncmill died or did not finish its
run record, ...) is counted separately and left out of the rate.

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
import hashlib
import json
import os
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

# Each agent id syncmill can enable, and the argv[0] its command builder execs.
# Written out rather than imported: this script runs under a plain `python`
# that need not have syncmill's dependencies. A test pins it to the registry.
_AGENT_BINARIES = {"claude": "claude", "codex": "codex", "kimi-code": "kimi"}

# A cell's outcome comes from evidence, never from matching its stderr. Error
# text there is not the harness's to interpret: syncmill logs degraded optional
# backends verbatim on runs that succeed, and an agent's own tool output says
# "No such file or directory" on runs that simply lose. Both were once read as
# a broken environment.
OUTCOMES = ("pass", "wrong-answer", "timeout", "strategy-failed", "infrastructure-error")
INFRA = "infrastructure-error"
# A broken environment is no longer a crash, so it has to stop the sweep some
# other way: three cells in a row that reached no verdict.
MAX_CONSECUTIVE_INFRA = 3
_RUN_STATES = frozenset({"running", "pending", "no_changes", "failed", "recovery_required"})
_RECEIPTS = "receipts.jsonl"

# Stands in for one agent binary on the cell's PATH. It writes a receipt naming
# its pid and then execs the real binary, so the receipt's pid IS the agent:
# no supervising process, and the session, group, signals and exit status
# syncmill sees are the agent's own. If the exec fails the receipt is
# withdrawn, and only an unwithdrawn one counts as a dispatch.
_SHIM = r'''import json, os, signal, sys
binary, real, receipts, shim_dir = sys.argv[1:5]

def note(event, **extra):
    line = json.dumps({"event": event, "agent": binary, "pid": os.getpid(), **extra})
    fd = os.open(receipts, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)

note("exec")
# Python ignores these at startup, and an ignored signal stays ignored across
# exec. subprocess's restore_signals undoes the same thing for a normal launch.
for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
# Only the shim's own entry is removed: the agent keeps every directory the
# launcher added, and an agent that calls another one by name gets the real one.
path = os.pathsep.join(
    p for p in os.environ.get("PATH", "").split(os.pathsep) if p != shim_dir)
try:
    os.execve(real, [binary, *sys.argv[5:]], dict(os.environ, PATH=path))
except OSError as exc:
    note("exec-error", errno=exc.errno)
    sys.stderr.write(f"selfbench shim: cannot exec {real}: {exc}\n")
    os._exit(127)
'''


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


def cell_env(
    state_dir: Path, disabled: tuple[str, ...] = (), shim_dir: Path | None = None,
) -> dict:
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
    # Set after the ambient strip, so these are the only agent toggles a cell has.
    for agent_id in disabled:
        env[f"SYNCMILL_{agent_id.replace('-', '_').upper()}__ENABLED"] = "false"
    if shim_dir is not None:
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    return env


def resolve_agents(disabled: tuple[str, ...]) -> dict[str, str]:
    """The real binary behind every agent syncmill will enable, or refuse.

    The lineup is part of the experiment. A compete cell that silently runs
    without one of its agents measures a different contest, and a binary that
    cannot launch was being scored as that agent losing.
    """
    found: dict[str, str] = {}
    missing, shadowed = [], []
    # `uv run` puts the project's virtualenv first on PATH, ahead of the cell's
    # shims. An agent installed there would be launched without a receipt.
    venv_bin = REPO_ROOT / ".venv" / "bin"
    for agent_id, binary in _AGENT_BINARIES.items():
        if agent_id in disabled:
            continue
        if (venv_bin / binary).exists():
            shadowed.append(binary)
        real = shutil.which(binary)
        if real is None:
            missing.append(f"{binary} ({agent_id})")
        else:
            found[binary] = os.path.abspath(real)
    if missing:
        raise SystemExit(
            f"agent binaries not found: {', '.join(missing)}\n"
            "install them, or leave them out with --disable-agent ID "
            "(the lineup is part of the experiment fingerprint)"
        )
    if shadowed:
        raise SystemExit(
            f"{', '.join(shadowed)} installed in {venv_bin}, which `uv run` puts "
            "ahead of the harness's launch receipts; remove it from the project "
            "environment"
        )
    return found


def write_shims(state_dir: Path, agents: dict[str, str]) -> Path:
    """One receipt-writing stand-in per agent binary, in the cell's own state."""
    shim_dir = state_dir / "shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    script = shim_dir / "_shim.py"
    script.write_text(_SHIM, encoding="utf-8")
    receipts = state_dir / _RECEIPTS
    for binary, real in agents.items():
        wrapper = shim_dir / binary
        # -I: the agent's PYTHON* variables must not change how the shim runs.
        argv = [sys.executable, "-I", str(script), binary, real, str(receipts),
                str(shim_dir)]
        wrapper.write_text(f'#!/bin/sh\nexec {shlex.join(argv)} "$@"\n',
                           encoding="utf-8")
        wrapper.chmod(0o755)
    return shim_dir


def read_receipts(path: Path) -> tuple[list[tuple[str, int]], int, bool]:
    """(dispatched (binary, pid) pairs, exec failures, any line unreadable)."""
    if not path.is_file():
        return [], 0, False
    execs: list[tuple[str, int]] = []
    failed: set[int] = set()
    malformed = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not (isinstance(event, dict) and isinstance(event.get("agent"), str)
                and type(event.get("pid")) is int):
            malformed = True
        elif event.get("event") == "exec":
            execs.append((event["agent"], event["pid"]))
        elif event.get("event") == "exec-error":
            failed.add(event["pid"])
        else:
            malformed = True
    return [(a, pid) for a, pid in execs if pid not in failed], len(failed), malformed


def read_run_state(projects_root: Path) -> tuple[str | None, str]:
    """The cell's one run record state, or None and why it cannot be read.

    syncmill writes `manifest.json` for every run that got past creating its
    artifact, `running` first and a terminal state when it finishes, and
    nothing a cell can switch off. A cell has its own store, so anything but
    exactly one record is itself the finding.
    """
    manifests = sorted(projects_root.glob("*/runs/*/manifest.json"))
    if len(manifests) != 1:
        return None, f"{len(manifests)} run records"
    try:
        doc = json.loads(manifests[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable run record"
    state = doc.get("state") if isinstance(doc, dict) else None
    if state not in _RUN_STATES:
        return None, f"run record state {state!r}"
    return state, state


def _group_alive(pid: int) -> bool:
    """Whether the process group an agent leads still has a member.

    syncmill starts each agent as a session leader, so its group id is its
    pid, and POSIX keeps that id from being reused while the group has a
    member. Asking about the leader alone missed a descendant still holding
    the agent's pipes after the leader exited.
    """
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def expected_dispatch(arm: str, agents: dict[str, str]) -> set[str]:
    """Agent binaries a cell of this arm must launch for its result to count.

    A pinned agent must run. Compete runs every enabled agent at once, so one
    that never launched is a missing competitor, not one that lost. Other
    strategies decide for themselves which agents run, so nothing is required.
    """
    argv = ARMS[arm]
    if "--agent" in argv:
        return {_AGENT_BINARIES[argv[argv.index("--agent") + 1]]}
    if argv[argv.index("--strategy") + 1] == "compete":
        return set(agents)
    return set()


def classify(
    *,
    dispatched: list[tuple[str, int]],
    exec_errors: int,
    malformed: bool,
    expected: set[str],
    timed_out: bool,
    agents_running: bool,
    run_state: str | None,
    state_reason: str,
    returncode: int | None,
    patch_exists: bool,
) -> tuple[str, str]:
    """(outcome, reason) for one cell; outcome "grade" means grade the patch.

    Scored only on positive evidence that a strategy ran and reached an end:
    an agent the harness saw launch, and syncmill's own record of how the run
    finished. Everything else is the environment, counted but not scored.

    Known limits, both stated in the README: a provider that rejects the agent
    (expired token, rate limit, outage) still dispatched it, so that cell is
    scored; and a deadline that lands between two agent phases reads as no
    agent running.
    """
    if malformed:
        return INFRA, "unreadable launch receipt"
    if exec_errors:
        return INFRA, "an agent binary failed to exec"
    if not dispatched:
        return INFRA, "no agent dispatched"
    missing = expected - {agent for agent, _ in dispatched}
    if missing:
        return INFRA, f"not dispatched: {', '.join(sorted(missing))}"
    if timed_out:
        # Asked at the deadline: an agent still working means the strategy ran
        # out of budget; none left means syncmill stalled after its agents.
        if agents_running:
            return "timeout", "agents running at the deadline"
        return INFRA, "stalled after its agents exited"
    if run_state is None:
        return INFRA, state_reason
    if run_state in ("running", "recovery_required"):
        return INFRA, f"run record {run_state}"
    if run_state == "pending" and returncode == 0 and patch_exists:
        return "grade", "run record pending"
    # A clean exit is required here too: a terminal record is written before
    # cleanup, and a cell that died after it did not finish.
    if run_state == "no_changes" and returncode == 0:
        return "wrong-answer", "completed without changes"
    if run_state == "failed":
        return "strategy-failed", "run record failed"
    return INFRA, f"run record {run_state} with exit {returncode}"


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


def experiment_fingerprint(
    tasks: list[dict], timeout_s: int, disabled: tuple[str, ...] = (),
) -> dict:
    """The experiment's settings a resume has to match.

    Resuming used to match on (task, arm) alone, so a sweep re-run after editing
    a check, an arm's argv, or `--timeout` skipped those cells and reported the
    old numbers as current ones.

    The corpus is digested as loaded, not as file bytes: that is what runs, and
    reformatting the file changes no verdict. Every arm in ARMS is recorded, not
    only the selected ones, so a resume may select a different subset while an
    edit to any arm still counts. The agents left out with --disable-agent are
    part of it: compete without kimi is a different contest.

    It is not everything a verdict depends on. The agents' versions and models,
    syncmill's own code, the grading wrapper and the Python that runs the checks
    are all absent, and a change to any of them resumes as the same experiment.
    """
    corpus = json.dumps(tasks, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"))
    return {
        "format": 2,
        "corpus_sha256": hashlib.sha256(corpus.encode("utf-8")).hexdigest(),
        "arms": {name: list(argv) for name, argv in sorted(ARMS.items())},
        "timeout": timeout_s,
        "check_timeout": CHECK_TIMEOUT_S,
        "launcher": list(SYNCMILL),
        "disabled_agents": sorted(disabled),
    }


def _header_of(line: str) -> dict | None:
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(row, dict) and isinstance(row.get("experiment"), dict):
        return row["experiment"]
    return None


def _write_atomically(path: Path, text: str) -> None:
    """Replace `path` whole. A header torn by a crash would refuse the resume."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def claim_results_file(out_path: Path, fingerprint: dict) -> None:
    """Make `out_path` this experiment's file, or refuse to resume into it.

    A new or blank file gets the header as its first line. Anything else must
    already start with this exact fingerprint and carry no other header. A file
    with rows and no header predates fingerprints: nothing says which corpus,
    arms or timeout produced it, so it is refused rather than adopted.
    """
    lines = []
    if out_path.is_file():
        lines = [ln for ln in out_path.read_text(encoding="utf-8").splitlines()
                 if ln.strip()]
    if not lines:
        _write_atomically(out_path, json.dumps({"experiment": fingerprint}) + "\n")
        return
    headers = [i for i, ln in enumerate(lines) if _header_of(ln) is not None]
    if not headers:
        raise SystemExit(
            f"{out_path} has rows but no experiment header, so they cannot be "
            "matched to this corpus, these arms, or this timeout — use a new --out"
        )
    if headers != [0]:
        raise SystemExit(
            f"{out_path} holds more than one experiment header, or one that is "
            "not its first line — use a new --out"
        )
    stored = _header_of(lines[0])
    # Compared after a JSON round trip, which is the form the stored one took.
    current = json.loads(json.dumps(fingerprint))
    if stored != current:
        differ = sorted(k for k in stored.keys() | current.keys()
                        if stored.get(k) != current.get(k))
        raise SystemExit(
            f"{out_path} was written by a different experiment "
            f"({', '.join(differ)} changed); resuming would report its rows as "
            "this one's — use a new --out"
        )


def load_results(out_path: Path) -> list[dict]:
    """Existing rows, so an interrupted sweep resumes instead of restarting.

    Rows that do not parse are dropped rather than fatal: the last line of an
    interrupted file can be a partial write. The experiment header carries none
    of the row fields, so it is never read as a row, and neither is a row
    without a known outcome.
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
        # Valid JSON that is not an object is not a row either; a list of the
        # four field names passed the key test and crashed the resume.
        if (isinstance(row, dict)
                and {"task", "arm", "outcome", "success", "wall_s"} <= set(row)
                and row["outcome"] in OUTCOMES):
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


def bench(
    arms: list[str], timeout_s: int, out_path: Path, dry_run: bool, *,
    disabled: tuple[str, ...] = (), agents: dict[str, str] | None = None,
) -> int:
    """Run every (task, arm) cell not already in `out_path`.

    `agents` maps each enabled agent binary to the real one (`resolve_agents`);
    a shim for each records the launches a cell is classified by. Callers that
    pass none get no shims, and a cell counts only the receipts it finds.
    """
    agents = agents or {}
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
              "state and records agent launches through PATH shims; these "
              "lines use your ambient configuration")
        return 0

    # Written as each run finishes, not at the end. A full sweep is 24 agent
    # runs at up to --timeout each; a crash, a hang, or a Ctrl-C on hour three
    # used to leave nothing behind at all — and then truncating the file on the
    # next start deleted exactly what the incremental write had preserved.
    claim_results_file(out_path, experiment_fingerprint(tasks, timeout_s, disabled))
    results = load_results(out_path)
    done = {(r["task"], r["arm"]) for r in results}
    if done:
        print(f"resuming: {len(done)} cell(s) already in {out_path}")
    consecutive_infra = 0
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
            receipts = state / _RECEIPTS
            shim_dir = write_shims(state, agents) if agents else None
            t0 = time.monotonic()
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
                env=cell_env(state, disabled, shim_dir), **popen_kwargs,
            )
            timed_out = agents_running = False
            try:
                cell_proc.communicate(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                agents_running = any(
                    _group_alive(pid) for _, pid in read_receipts(receipts)[0])
                _stop_cell(cell_proc)
            except BaseException:
                # A Ctrl-C reaches the harness, not the cell, which is in a
                # session of its own. Unwinding without stopping it left syncmill
                # and its agents running, and a resume then started a second
                # copy of the same cell beside the first.
                _stop_cell(cell_proc)
                raise
            dispatched, exec_errors, malformed = read_receipts(receipts)
            run_state, state_reason = read_run_state(state / "projects")
            outcome, reason = classify(
                dispatched=dispatched, exec_errors=exec_errors, malformed=malformed,
                expected=expected_dispatch(arm, agents), timed_out=timed_out,
                agents_running=agents_running, run_state=run_state,
                state_reason=state_reason, returncode=cell_proc.returncode,
                patch_exists=patch.is_file(),
            )
            if outcome == "grade":
                applied = subprocess.run(
                    ["git", "apply", str(patch)], cwd=wd, capture_output=True,
                    env=harness_git_env(),
                ).returncode == 0
                outcome = "pass" if applied and run_check(wd, task) else "wrong-answer"
            wall = round(time.monotonic() - t0, 1)
        record = {"task": task["id"], "arm": arm, "outcome": outcome,
                  "success": outcome == "pass", "wall_s": wall,
                  "dispatched": sorted(agent for agent, _ in dispatched),
                  "run_state": run_state, "reason": reason}
        results.append(record)
        append_record(out_path, record)
        # The plan can name a cell twice when a caller repeats an arm; the
        # second visit has to find it done, not run it again.
        done.add((task["id"], arm))
        print(f"[{task['id']} × {arm}] {outcome} ({wall}s)"
              + (f": {reason}" if outcome == INFRA else ""))
        consecutive_infra = consecutive_infra + 1 if outcome == INFRA else 0
        if consecutive_infra >= MAX_CONSECUTIVE_INFRA:
            print(f"\nwrote {out_path}\n")
            print(render_table(results, arms))
            print(f"\nstopped: {consecutive_infra} cells in a row reached no "
                  "verdict; this is an environment problem, not a benchmark "
                  "result. Completed cells are kept; rerun to resume.",
                  file=sys.stderr)
            return 1

    print(f"\nwrote {out_path}\n")
    print(render_table(results, arms))
    return 0


def render_table(results: list[dict], arms: list[str]) -> str:
    lines = ["| arm | wins/scored | rate | infra | mean wall_s |",
             "|---|---|---|---|---|"]
    tasks = sorted({r["task"] for r in results})
    for arm in arms:
        rows = [r for r in results if r["arm"] == arm]
        # Infrastructure errors are not losses. Dividing by them reported a
        # broken environment as a strategy's poor showing.
        scored = [r for r in rows if r["outcome"] != INFRA]
        wins = sum(r["outcome"] == "pass" for r in scored)
        # Denominator is the runs this arm actually completed. Dividing by the
        # corpus size would quietly report a half-finished arm as having lost.
        rate = f"{wins / len(scored):.0%}" if scored else "n/a"
        mean = (f"{sum(r['wall_s'] for r in scored) / len(scored):.0f}"
                if scored else "n/a")
        lines.append(f"| {arm} | {wins}/{len(scored)} | {rate} | "
                     f"{len(rows) - len(scored)} | {mean} |")
    lines.append("")
    # `.` is a cell the sweep never reached and `!` one that reached no verdict.
    # Rendering a partial results.jsonl has to work — it is written
    # incrementally, so an interrupted run is exactly the case someone reads.
    by_cell = {(r["task"], r["arm"]): r["outcome"] for r in results}

    def mark(outcome: str | None) -> str:
        if outcome is None:
            return "."
        return {"pass": "+", INFRA: "!"}.get(outcome, "-")

    lines.append("per-task: " + ", ".join(
        t + "[" + "".join(mark(by_cell.get((t, a))) for a in arms) + "]"
        for t in tasks))
    return "\n".join(lines)


def preflight_launcher() -> None:
    """Prove the launcher runs BEFORE the first cell, not three cells in.

    A launcher that dies, during import or otherwise, dispatches no agent, so
    its cells are not scored and three in a row stop the sweep. That catches
    it, but only after three cells of setup. Asking the launcher to identify
    itself once costs one subprocess and fails before any of them.

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
    ap.add_argument("--disable-agent", action="append", default=[], metavar="ID",
                    choices=sorted(_AGENT_BINARIES),
                    help="leave an agent out of every cell (repeatable)")
    ap.add_argument("--validate-checks", action="store_true")
    args = ap.parse_args()

    if args.validate_checks:
        return validate_checks()
    # Order-preserving: `--arms solo,solo` ran every solo cell twice and printed
    # the arm twice in the table.
    arms = list(dict.fromkeys(a.strip() for a in args.arms.split(",") if a.strip()))
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        ap.error(f"unknown arms {unknown}; choose from {sorted(ARMS)}")
    disabled = tuple(sorted(set(args.disable_agent)))
    for arm in arms:
        argv = ARMS[arm]
        if "--agent" in argv and argv[argv.index("--agent") + 1] in disabled:
            ap.error(f"arm {arm} runs {argv[argv.index('--agent') + 1]}, "
                     "which --disable-agent leaves out")
    if not args.dry_run and shutil.which("uv") is None:
        ap.error("uv not found on PATH — run from a machine with the dev setup")
    agents: dict[str, str] = {}
    if not args.dry_run:
        preflight_launcher()
        agents = resolve_agents(disabled)
    return bench(arms, args.timeout, args.out, args.dry_run,
                 disabled=disabled, agents=agents)


if __name__ == "__main__":
    raise SystemExit(main())
