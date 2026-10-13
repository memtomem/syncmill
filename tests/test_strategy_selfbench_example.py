"""Contract for the strategy self-bench harness.

The harness produces the number someone will quote when deciding whether
compete or decompose is worth its cost, so its failure modes are measurement
failures: a grader that passes on an empty file grades nothing, a hung check
that aborts the sweep loses every completed run, and a rate computed over the
corpus rather than over what ran reports a half-finished arm as having lost.

Pure unit tests — no agents, no syncmill invocation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


_EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "examples" / "strategy_selfbench" / "run_selfbench.py"
)


class _FakeProc:
    """Stands in for a cell's syncmill process without launching anything."""

    def __init__(self, returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self._stderr = stderr

    def communicate(self, timeout=None):
        return "", self._stderr


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("_selfbench", _EXAMPLE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _result_rows(out: Path) -> list[dict]:
    """A results file's rows, minus the experiment header that opens it.

    Strict on purpose: every line must parse and only the first may be a
    header, so a test reading rows also notices a stray header or a torn write.
    """
    if not out.exists():
        return []
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if rows and "experiment" in rows[0]:
        rows = rows[1:]
    assert not any("experiment" in r for r in rows), rows
    return rows


def _leave_cell_evidence(env: dict, run_state: str | None,
                         agents=("claude",), pids=None) -> None:
    """Write what a real cell leaves behind: launch receipts and a run record.

    The shims write the receipts and syncmill writes the record; a fake
    launcher that does neither is a cell where no agent ever ran.
    """
    runs_root = Path(env["SYNCMILL_RUNS__ROOT"])
    state = runs_root.parent
    state.mkdir(parents=True, exist_ok=True)
    pids = pids or [999_000 + i for i in range(len(agents))]
    with (state / "receipts.jsonl").open("a", encoding="utf-8") as fh:
        for agent, pid in zip(agents, pids):
            fh.write(json.dumps({"event": "exec", "agent": agent, "pid": pid}) + "\n")
    if run_state is not None:
        run = runs_root / "project" / "runs" / "run-1"
        run.mkdir(parents=True)
        (run / "manifest.json").write_text(json.dumps({"state": run_state}),
                                           encoding="utf-8")


def test_corpus_is_solvable_and_the_graders_discriminate(bench):
    """Every check passes its reference solution and fails an empty seed.

    A check that passes the empty seed is not grading the task.
    """
    assert bench.validate_checks() == 0


def _task(bench, task_id: str) -> dict:
    return next(t for t in bench.load_tasks() if t["id"] == task_id)


# The toposort reference as it shipped before #135, frozen rather than derived
# from the live one: this is the artifact the check must reject, and a copy
# spliced out of the current text would drift with it. Its defect is the last
# four lines — the first node absent from the output may only be blocked BY a
# cycle, not on one.
_OLD_TOPOSORT_REFERENCE = """\
def toposort(edges):
    nodes = set(edges)
    for vs in edges.values(): nodes.update(vs)
    indeg = {n: 0 for n in nodes}
    for k, vs in edges.items():
        for _ in vs: indeg[k] += 1
    ready = sorted(n for n in nodes if indeg[n] == 0)
    dependents = {n: [] for n in nodes}
    for k, vs in edges.items():
        for v in vs: dependents[v].append(k)
    out = []
    import heapq
    heapq.heapify(ready)
    while ready:
        n = heapq.heappop(ready)
        out.append(n)
        for d in dependents[n]:
            indeg[d] -= 1
            if indeg[d] == 0: heapq.heappush(ready, d)
    if len(out) != len(nodes):
        cyc = sorted(n for n in nodes if n not in out)
        raise ValueError(f'cycle involving {cyc[0]}')
    return out
"""

# 'a' and 'b' both wait on the {x, y} cycle and sort before it, so the first
# absent name is a blocked node, not a cycle node.
_BLOCKED_BEFORE_CYCLE = {"x": ["y"], "y": ["x"], "b": ["x"], "a": ["b"]}

# The same whole-token extraction the check's `names()` uses: 'c' is a
# substring of 'cycle', so a message naming the wrong node would otherwise
# look like it named a right one.
_NODE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def _node_tokens(exc: BaseException) -> set[str]:
    return set(_NODE_TOKEN_RE.findall(str(exc)))


def _nodes_of(edges: dict) -> set[str]:
    return set(edges) | {v for vs in edges.values() for v in vs}


def _on_cycle(edges: dict, node: str) -> bool:
    """A node is on a cycle iff it can reach itself along dependency edges."""
    frontier, seen = list(edges.get(node, [])), set()
    while frontier:
        n = frontier.pop()
        if n not in seen:
            seen.add(n)
            frontier.extend(edges.get(n, []))
    return node in seen


def _load_toposort(source: str):
    namespace: dict = {}
    exec(source, namespace)
    return namespace["toposort"]


def test_the_toposort_check_rejects_naming_a_node_blocked_by_the_cycle(
    bench, tmp_path
):
    """The prompt asks for a node IN the cycle, and the check must hold to that.

    The shipped reference named the first node absent from the output, which may
    only be waiting on the cycle. The check missed it because its one blocked
    node sorted after the cycle; a solution that passed it could still break
    the stated contract.

    The old implementation is shown to work before the check sees it. The
    grader's wrapper folds every exception into a failed verdict, so without
    these positive controls a broken copy would read as "rejected" too.
    """
    old = _load_toposort(_OLD_TOPOSORT_REFERENCE)
    assert old({"b": ["a"], "c": ["b"]}) == ["a", "b", "c"]  # a working toposort...
    with pytest.raises(ValueError) as excinfo:
        old(_BLOCKED_BEFORE_CYCLE)
    # ...with exactly the old defect: it names the blocked node, not x or y
    assert _node_tokens(excinfo.value) & _nodes_of(_BLOCKED_BEFORE_CYCLE) == {"a"}

    (tmp_path / "mod.py").write_text(_OLD_TOPOSORT_REFERENCE, encoding="utf-8")
    assert bench.run_check(tmp_path, _task(bench, "toposort")) is False


def test_the_toposort_reference_names_a_node_on_a_cycle(bench):
    """Over many cyclic graphs, not just the handful the check spells out."""
    toposort = _load_toposort(_task(bench, "toposort")["reference"])
    rng = random.Random(135)
    names = [chr(c) for c in range(ord("a"), ord("a") + 8)]
    cyclic = 0
    for _ in range(500):
        edges = {
            n: rng.sample(names, rng.randint(0, 2)) for n in rng.sample(names, 6)
        }
        try:
            toposort(edges)
        except ValueError as e:
            cyclic += 1
            # Held to the contract the check holds agents to: whole tokens, and
            # at least one of them a node that can reach itself.
            named = _node_tokens(e) & _nodes_of(edges)
            assert named and any(_on_cycle(edges, n) for n in named), (edges, str(e))
    assert cyclic > 100  # the sample must actually exercise the cycle path


def test_task_ids_are_unique(bench):
    tasks = bench.load_tasks()
    assert len(tasks) >= 5
    assert len({t["id"] for t in tasks}) == len(tasks)


def test_a_hanging_check_is_a_failure_not_a_crash(bench, tmp_path, monkeypatch):
    """Agent-written code loops sometimes; that grades as fail, not as abort."""
    monkeypatch.setattr(bench, "CHECK_TIMEOUT_S", 1)
    assert bench.run_check(tmp_path, {"check": "while True:\n    pass\n"}) is False


def test_grading_cannot_see_the_reviewer_credentials_or_home(bench, tmp_path, monkeypatch):
    """Graded code is agent-written; it runs with a scratch home and no secrets."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "super-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "also-secret")
    real_home = os.environ.get("HOME", "")

    task = {"check": "import os\n"
                     "print(os.environ.get('ANTHROPIC_API_KEY', 'absent'))\n"
                     "print(os.environ.get('AWS_SECRET_ACCESS_KEY', 'absent'))\n"
                     "print(os.environ['HOME'])\n"
                     "print('ok')\n"}
    (tmp_path / "mod.py").write_text("", encoding="utf-8")
    assert bench.run_check(tmp_path, task) is True

    # Read what the grader actually saw by running the same env directly.
    env = bench.grader_env(tmp_path / ".grader-home", tmp_path / "receipt")
    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["HOME"].startswith(str(tmp_path))
    assert env["HOME"] != real_home


def test_a_grader_that_outlives_its_timeout_is_killed_with_its_children(
    bench, tmp_path, monkeypatch
):
    """A fork that survives its parent keeps burning the machine for the sweep.

    Killing only the direct child leaves the grandchild running; the marker file
    it appends to is how the test can tell.
    """
    monkeypatch.setattr(bench, "CHECK_TIMEOUT_S", 2)
    marker = tmp_path / "alive"
    (tmp_path / "spawn_me.py").write_text(textwrap.dedent(f"""
        import time
        while True:
            open({str(marker)!r}, "a").write("x")
            time.sleep(0.1)
    """), encoding="utf-8")
    check = textwrap.dedent(f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, {str(tmp_path / "spawn_me.py")!r}])
        time.sleep(60)
    """)

    # Run it on a thread with a deadline: killing only the direct child does not
    # merely leak the grandchild, it HANGS — the grandchild inherits the stdout
    # pipe, so communicate() waits forever for an EOF that never comes, and the
    # sweep stops dead. A hang has to read as a failure here, not as a pause.
    verdict: list = []
    worker = threading.Thread(
        target=lambda: verdict.append(bench.run_check(tmp_path, {"check": check})),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive(), "run_check hung: the grader's children were not reaped"
    assert verdict == [False], verdict
    # The grandchild must have actually run, or this proves nothing.
    at_kill = marker.stat().st_size
    assert at_kill > 0, "the grandchild never started; the test would be vacuous"
    time.sleep(1.0)
    assert marker.stat().st_size == at_kill, "a grandchild survived the timeout"


def test_a_module_that_just_exits_cleanly_does_not_pass(bench, tmp_path):
    """Exit status is not a verdict.

    `raise SystemExit(0)` at import ends the check at its first line with status
    zero, and every task graded as passed for a file that implemented nothing.
    """
    task = bench.load_tasks()[0]
    (tmp_path / "mod.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    assert bench.run_check(tmp_path, task) is False


@pytest.mark.parametrize(
    "impl",
    ['raise SystemExit(0)\n',
     'print("ok"); raise SystemExit(0)\n',
     'print("look"); raise SystemExit(0)\n',
     'import os; os._exit(0)\n'],
    ids=["silent-exit", "forged-sentinel", "endswith-ok", "hard-exit"],
)
def test_a_pass_cannot_be_forged_from_inside_the_graded_module(bench, tmp_path, impl):
    """The verdict is the wrapper's receipt, not exit status or stdout.

    Exit status alone passed a module that implemented nothing; a printed
    sentinel was forgeable, and "look" ends with "ok" besides. `os._exit(0)`
    skips the wrapper's own exception handling entirely, which is why the
    receipt — written only after the check body returns — is the verdict.
    """
    task = bench.load_tasks()[0]
    (tmp_path / "mod.py").write_text(impl, encoding="utf-8")
    assert bench.run_check(tmp_path, task) is False


@pytest.mark.parametrize(
    "tail",
    ["import atexit, os\natexit.register(os._exit, 1)\n",
     "import sys\nsys.argv = None\n"],
    ids=["atexit-exits-nonzero", "argv-cleared"],
)
def test_what_runs_after_the_body_counts_against_it(bench, tmp_path, tail):
    """A pass needs a clean exit as well as the receipt.

    Both bodies run to completion. The first then exits non-zero from an
    `atexit` hook. The second leaves `sys.argv` unusable, and `runpy` fails
    restoring it. Both fail, and the README says so: the alternative, a
    receipt alone, is what lets a forking body pass.
    """
    task = {"check": "print('ok')\n" + tail}
    assert bench.run_check(tmp_path, task) is False


def test_a_check_whose_shutdown_hangs_is_a_failure(bench, tmp_path, monkeypatch):
    """A thread left running holds interpreter shutdown past the timeout."""
    monkeypatch.setattr(bench, "CHECK_TIMEOUT_S", 2)
    check = ("import threading, time\n"
             "threading.Thread(target=time.sleep, args=(60,)).start()\n")
    assert bench.run_check(tmp_path, {"check": check}) is False


@pytest.mark.parametrize(
    "check",
    ["import os\npid = os.fork()\nif pid:\n"
     "    os.waitpid(pid, 0)\n    raise AssertionError('parent fails')\n",
     "import os, time\npid = os.fork()\nif pid:\n"
     "    os.waitpid(pid, 0)\n    time.sleep(60)\n"],
    ids=["parent-fails", "parent-hangs"],
)
@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_forked_child_cannot_pass_for_a_failing_parent(
    bench, tmp_path, monkeypatch, check
):
    """The child inherits the wrapper, finishes the body and writes the receipt.

    Only the clean-exit requirement stops it, which is why a receipt alone is
    not the verdict and why a timeout is a failure even with a receipt.
    """
    monkeypatch.setattr(bench, "CHECK_TIMEOUT_S", 3)
    assert bench.run_check(tmp_path, {"check": check}) is False


@pytest.mark.parametrize(
    "check",
    [
        "import pickle\n"
        "class Item:\n"
        "    def __init__(self, v): self.v = v\n"
        "assert pickle.loads(pickle.dumps(Item(3))).v == 3\n",
        "from __future__ import annotations\n"
        "from dataclasses import dataclass, KW_ONLY\n"
        "@dataclass\n"
        "class P:\n"
        "    a: int\n"
        "    _: KW_ONLY\n"
        "    b: int = 0\n"
        "assert P(1, b=2).b == 2\n",
        "import sys\nassert sys.argv[0] == __file__, (sys.argv[0], __file__)\n",
        "import sys\nassert __name__ == '__main__'\n"
        "assert sys.modules['__main__'].__dict__ is globals()\n",
        "assert __builtins__['len']([1]) == 1\n",
    ],
    ids=["pickle-a-check-class", "dataclass-kw-only", "argv0-is-file",
         "registered-main", "builtins-is-a-dict"],
)
def test_a_check_runs_in_the_namespace_runpy_gives_it(bench, tmp_path, check):
    """What a legitimate check sees, pinned for any rewrite of the wrapper.

    Each of these passes under `runpy` and failed under a hand-built
    replacement: pickle and dataclasses resolve names through the registered
    `__main__` module, a check may compare its argv to its file, and runpy
    hands the body `__builtins__` as a dict, not the module.
    """
    assert bench.run_check(tmp_path, {"check": check}) is True


def test_a_check_that_does_not_compile_is_a_failure(bench, tmp_path):
    """A syntax error is the body failing, not the grader crashing."""
    assert bench.run_check(tmp_path, {"check": "def (:\n"}) is False


def test_the_receipt_path_is_not_in_the_graded_codes_environment(bench, tmp_path):
    """The path is popped before the body runs, so it is not in plain view.

    That is all this shows. The path stays reachable through the wrapper's
    frame; the receipt stops accidental passes, not a determined forger.
    """
    task = {"check": "import os\n"
                     "assert '_SELFBENCH_RECEIPT' not in os.environ\n"}
    (tmp_path / "mod.py").write_text("", encoding="utf-8")
    assert bench.run_check(tmp_path, task) is True


def test_a_complete_final_row_missing_its_newline_is_kept(bench, tmp_path):
    """Truncating every unterminated tail deleted real results."""
    out = tmp_path / "results.jsonl"
    complete = {"task": "a", "arm": "solo", "success": True, "wall_s": 1.0}
    out.write_text(json.dumps(complete), encoding="utf-8")  # no trailing newline
    bench.append_record(out, {"task": "b", "arm": "solo",
                              "success": False, "wall_s": 2.0})
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert rows[0] == complete, rows
    assert len(rows) == 2, rows


def test_cell_state_is_not_created_inside_the_scratch_repo(bench, tmp_path, monkeypatch):
    """syncmill snapshots the repo; state underneath it breaks the capture."""
    seen: list = []
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: seen.append(wd))
    monkeypatch.setattr(bench, "run_check", lambda wd, task: False)
    monkeypatch.setattr(
        bench, "cell_env",
        lambda state, *a: seen.append(state) or {"HOME": str(state)},
    )
    monkeypatch.setattr(bench.subprocess, "Popen", lambda argv, **kw: _FakeProc(1, ""))
    bench.bench(["solo"], 1, tmp_path / "out.jsonl", dry_run=False)

    repos = [Path(x) for x in seen[0::2]]
    states = [Path(x) for x in seen[1::2]]
    for repo, state in zip(repos, states):
        assert state.parent == repo.parent, (repo, state)
        assert repo not in state.parents, (repo, state)


def test_a_torn_final_line_is_repaired_before_appending(bench, tmp_path):
    """A crash between the write and its newline must not fuse two records."""
    out = tmp_path / "results.jsonl"
    out.write_text('{"task": "a", "arm": "solo", "success": true, "wall_s"',
                   encoding="utf-8")
    bench.append_record(out, {"task": "b", "arm": "solo",
                              "success": False, "wall_s": 2.0})
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert rows == [{"task": "b", "arm": "solo", "success": False, "wall_s": 2.0}], rows


def test_a_failing_check_is_reported_false(bench, tmp_path):
    assert bench.run_check(tmp_path, {"check": "raise SystemExit(1)\n"}) is False
    assert bench.run_check(tmp_path, {"check": "print('ok')\n"}) is True


def test_table_renders_a_partial_sweep(bench):
    """results.jsonl is written per run, so a partial file is normal to read."""
    results = [
        {"task": "a", "arm": "solo", "outcome": "pass", "success": True, "wall_s": 1.0},
        {"task": "a", "arm": "compete", "outcome": "pass", "success": True, "wall_s": 2.0},
        {"task": "b", "arm": "solo", "outcome": "wrong-answer", "success": False,
         "wall_s": 3.0},
        # b × compete never ran
    ]
    table = bench.render_table(results, ["solo", "compete"])
    assert "a[++]" in table, table
    assert "b[-.]" in table, table
    # The rate divides by what the arm COMPLETED. compete went 1-for-1; over the
    # two-task corpus the same arm would read 50%, which is a different claim.
    assert "| compete | 1/1 | 100% |" in table, table
    assert "| solo | 1/2 | 50% |" in table, table


def test_results_are_written_as_each_run_finishes(bench, tmp_path, monkeypatch):
    """A sweep is hours long; a crash on the last cell must not erase the rest."""
    out = tmp_path / "results.jsonl"
    calls = {"n": 0}

    def fake_popen(argv, **kwargs):
        calls["n"] += 1
        if calls["n"] > 2:
            raise KeyboardInterrupt
        return _FakeProc(1, "")

    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    monkeypatch.setattr(bench, "run_check", lambda wd, task: False)
    monkeypatch.setattr(bench.subprocess, "Popen", fake_popen)

    with pytest.raises(KeyboardInterrupt):
        bench.bench(["solo"], 1, out, dry_run=False)

    written = _result_rows(out)
    assert len(written) == 2, written
    assert {r["task"] for r in written} <= {t["id"] for t in bench.load_tasks()}


def test_dry_run_prints_the_real_prompt_quoted(bench, capsys):
    """A dry run you cannot paste is not showing you the invocation."""
    assert bench.bench(["solo"], 1, Path("unused"), dry_run=True) == 0
    out = capsys.readouterr().out
    first = bench.load_tasks()[0]
    assert first["prompt"] in out or first["prompt"].replace("'", "'\"'\"'") in out
    assert "<prompt:" not in out
    assert "--strategy route --agent claude" in out


def test_a_cell_gets_its_own_home_and_state(bench, tmp_path, monkeypatch):
    """Isolation is the measurement, not just hygiene.

    syncmill copies a successful result — prompt and output — into the shared
    namespace, and the sweep runs one prompt through three arms in a row. On the
    ambient store the second arm could retrieve the first arm's answer, so the
    arms were never independent; it also wrote into the developer's real
    ``~/.syncmill`` and ``~/.memtomem``.
    """
    monkeypatch.setenv("SYNCMILL_MEMORY__DB_PATH", "/real/store.db")
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/real/other.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "keep-me")

    env = bench.cell_env(tmp_path / "state")
    assert env["HOME"].startswith(str(tmp_path))
    for key in ("SYNCMILL_WORKSPACE__ROOT", "SYNCMILL_RUNS__ROOT",
                "SYNCMILL_MEMORY__DB_PATH", "SYNCMILL_MEMORY__MEMORY_DIR"):
        assert env[key].startswith(str(tmp_path)), (key, env[key])
    # An ambient pointer at the real store must not survive.
    assert "/real/" not in env["SYNCMILL_MEMORY__DB_PATH"]
    assert "MEMTOMEM_STORAGE__SQLITE_PATH" not in env
    # Credentials do survive, or nothing runs at all.
    assert env["ANTHROPIC_API_KEY"] == "keep-me"


def test_isolation_uses_the_names_syncmill_actually_reads(bench, tmp_path):
    """A misspelled setting is isolation that looks applied and is not.

    `WorkspaceConfig` is singular, so the variable is SYNCMILL_WORKSPACE__ROOT;
    the plural spelling was ignored and the workspace fell back to the real
    ~/.syncmill.
    """
    from syncmill import config as syncmill_config

    env = bench.cell_env(tmp_path / "state")
    settings = syncmill_config.SyncmillConfig()
    for key in env:
        if not key.startswith("SYNCMILL_"):
            continue
        section, _, field = key[len("SYNCMILL_"):].partition("__")
        obj = getattr(settings, section.lower(), None)
        assert obj is not None, f"{key}: no settings section named {section.lower()!r}"
        assert hasattr(obj, field.lower()), f"{key}: {section.lower()} has no {field.lower()!r}"


def test_two_cells_do_not_share_state(bench, tmp_path):
    first = bench.cell_env(tmp_path / "a")
    second = bench.cell_env(tmp_path / "b")
    assert first["SYNCMILL_MEMORY__DB_PATH"] != second["SYNCMILL_MEMORY__DB_PATH"]
    assert first["HOME"] != second["HOME"]


def test_a_launcher_that_cannot_start_stops_the_sweep_without_scoring(
    bench, tmp_path, monkeypatch, capsys,
):
    """`uv run` from a scratch repo could not find syncmill at all.

    Every cell then recorded a loss and the sweep printed a table of zeros that
    read like a finding. Without a launch receipt no cell is scored, and three
    in a row stop the sweep: a launcher that breaks mid-sweep is caught too,
    not only one the preflight probe saw.
    """
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    launched = []
    monkeypatch.setattr(
        bench.subprocess, "Popen",
        lambda argv, **kw: launched.append(argv)
        or _FakeProc(1, "error: Failed to spawn: `syncmill`\n"),
    )
    out = tmp_path / "out.jsonl"
    assert bench.bench(["solo"], 1, out, dry_run=False) == 1

    rows = _result_rows(out)
    assert len(launched) == bench.MAX_CONSECUTIVE_INFRA == len(rows)
    assert {r["outcome"] for r in rows} == {"infrastructure-error"}, rows
    assert {r["reason"] for r in rows} == {"no agent dispatched"}, rows
    captured = capsys.readouterr()
    assert "not a benchmark result" in captured.err
    assert "| solo | 0/0 | n/a | 3 | n/a |" in captured.out, captured.out


def test_two_infrastructure_errors_in_a_row_do_not_stop_the_sweep(
    bench, tmp_path, monkeypatch,
):
    tasks = bench.load_tasks()[:4]
    launches = iter([None, None, "no_changes", None, None])

    def launch(argv, **kw):
        state = next(launches)
        if state is not None:
            _leave_cell_evidence(kw["env"], state)
            return _FakeProc(0, "")
        return _FakeProc(1, "")

    monkeypatch.setattr(bench, "load_tasks", lambda: tasks)
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    monkeypatch.setattr(bench.subprocess, "Popen", launch)
    out = tmp_path / "out.jsonl"
    assert bench.bench(["solo"], 1, out, dry_run=False) == 0
    assert [r["outcome"] for r in _result_rows(out)] == [
        "infrastructure-error", "infrastructure-error", "wrong-answer",
        "infrastructure-error"]


def test_an_interrupted_sweep_resumes_instead_of_restarting(bench, tmp_path, monkeypatch):
    out = tmp_path / "results.jsonl"
    tasks = bench.load_tasks()
    out.write_text(
        json.dumps({"experiment": bench.experiment_fingerprint(tasks, 1)}) + "\n"
        + json.dumps({"task": tasks[0]["id"], "arm": "solo", "outcome": "pass",
                      "success": True, "wall_s": 1.0}) + "\n"
        + "{truncated partial line\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    monkeypatch.setattr(bench, "run_check", lambda wd, task: False)
    monkeypatch.setattr(bench.subprocess, "Popen", lambda argv, **kw: _FakeProc(1, ""))
    bench.bench(["solo"], 1, out, dry_run=False)
    rows = [json.loads(line) for line in out.read_text().splitlines() if line.strip()
            and not line.startswith("{truncated")]
    done = [r for r in rows if r.get("task") == tasks[0]["id"] and r.get("arm") == "solo"]
    # the pre-existing cell is kept and not re-run
    assert len(done) == 1 and done[0]["success"] is True, rows


def _counting_cells(bench, monkeypatch, tasks=None) -> list:
    """Stub every cell to a quick loss and record each launch's argv."""
    if tasks is not None:
        monkeypatch.setattr(bench, "load_tasks", lambda: tasks)
    launched: list = []
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    monkeypatch.setattr(bench, "run_check", lambda wd, task: False)
    monkeypatch.setattr(bench.subprocess, "Popen",
                        lambda argv, **kw: launched.append(argv) or _FakeProc(1, ""))
    return launched


@pytest.mark.parametrize("existing", [None, "", "\n  \n"])
def test_a_new_sweep_opens_its_file_with_the_experiment_header(
    bench, tmp_path, monkeypatch, existing,
):
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    _counting_cells(bench, monkeypatch, [task])
    out = tmp_path / "results.jsonl"
    if existing is not None:
        out.write_text(existing, encoding="utf-8")
    bench.bench(["solo"], 7, out, dry_run=False)

    first = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert first == {"experiment": {
        "format": 2,
        "corpus_sha256": bench.experiment_fingerprint([task], 7)["corpus_sha256"],
        "arms": {name: list(argv) for name, argv in bench.ARMS.items()},
        "timeout": 7,
        "check_timeout": bench.CHECK_TIMEOUT_S,
        "launcher": list(bench.SYNCMILL),
        "disabled_agents": [],
    }}, first
    assert [(r["task"], r["arm"]) for r in _result_rows(out)] == [("slugify", "solo")]


def test_the_corpus_digest_changes_with_any_task_field(bench):
    tasks = bench.load_tasks()
    base = bench.experiment_fingerprint(tasks, 1)["corpus_sha256"]
    for field in ("prompt", "check", "reference"):
        edited = [dict(t) for t in tasks]
        edited[-1][field] += "\n"
        assert bench.experiment_fingerprint(edited, 1)["corpus_sha256"] != base, field


def test_a_file_without_an_experiment_header_is_refused_untouched(
    bench, tmp_path, monkeypatch,
):
    """Rows from before fingerprints say nothing about what produced them.

    Adopting them under the current header would certify them as this
    experiment's, which is the silent mix the header exists to stop.
    """
    tasks = bench.load_tasks()
    launched = _counting_cells(bench, monkeypatch)
    out = tmp_path / "results.jsonl"
    legacy = json.dumps({"task": tasks[0]["id"], "arm": "solo",
                         "success": True, "wall_s": 1.0}) + "\n"
    out.write_text(legacy, encoding="utf-8")
    with pytest.raises(SystemExit, match="no experiment header.*new --out"):
        bench.bench(["solo"], 1, out, dry_run=False)
    assert launched == []
    assert out.read_text(encoding="utf-8") == legacy


def test_a_file_with_a_second_header_is_refused(bench, tmp_path, monkeypatch):
    tasks = bench.load_tasks()
    launched = _counting_cells(bench, monkeypatch)
    header = json.dumps({"experiment": bench.experiment_fingerprint(tasks, 1)})
    out = tmp_path / "results.jsonl"
    out.write_text(header + "\n" + header + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="more than one experiment header"):
        bench.bench(["solo"], 1, out, dry_run=False)
    assert launched == []


def _edit_corpus(bench, monkeypatch, task):
    edited = dict(task, check=task["check"] + "\nassert True\n")
    monkeypatch.setattr(bench, "load_tasks", lambda: [edited])


@pytest.mark.parametrize(
    ("changed", "edit"),
    [
        ("corpus_sha256", _edit_corpus),
        ("arms", lambda bench, mp, task: mp.setitem(
            bench.ARMS, "compete", ["--strategy", "compete", "--agent", "codex"])),
        ("timeout", None),
        ("check_timeout", lambda bench, mp, task: mp.setattr(
            bench, "CHECK_TIMEOUT_S", bench.CHECK_TIMEOUT_S + 1)),
        ("launcher", lambda bench, mp, task: mp.setattr(
            bench, "SYNCMILL", [*bench.SYNCMILL, "--verbose"])),
    ],
)
def test_resuming_a_different_experiment_is_refused(
    bench, tmp_path, monkeypatch, changed, edit,
):
    """The old resume key was (task, arm) alone.

    After an edit to a check, an arm, or the timeout, every completed cell was
    skipped and its old verdict reported as the new experiment's. An arm the
    resume does not select still counts: `compete` changes under a solo sweep.
    """
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    launched = _counting_cells(bench, monkeypatch, [task])
    out = tmp_path / "results.jsonl"
    bench.bench(["solo"], 1, out, dry_run=False)
    before = out.read_text(encoding="utf-8")
    launched.clear()

    timeout = 2 if changed == "timeout" else 1
    if edit is not None:
        edit(bench, monkeypatch, task)
    with pytest.raises(SystemExit, match=rf"\({changed} changed\).*new --out"):
        bench.bench(["solo"], timeout, out, dry_run=False)
    assert launched == []
    assert out.read_text(encoding="utf-8") == before


def test_resuming_with_another_selection_of_arms_is_the_same_experiment(
    bench, tmp_path, monkeypatch,
):
    """The header records every arm, so adding one to a sweep is a resume."""
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    launched = _counting_cells(bench, monkeypatch, [task])
    out = tmp_path / "results.jsonl"
    bench.bench(["solo"], 1, out, dry_run=False)
    bench.bench(["solo", "compete"], 1, out, dry_run=False)

    assert [argv[argv.index("--strategy") + 1] for argv in launched] == [
        "route", "compete"]
    assert [r["arm"] for r in _result_rows(out)] == ["solo", "compete"]


def test_a_cell_the_plan_names_twice_runs_once(bench, tmp_path, monkeypatch):
    """`done` has to learn about a cell as soon as its row is written."""
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    launched = _counting_cells(bench, monkeypatch, [task])
    out = tmp_path / "results.jsonl"
    bench.bench(["solo", "solo"], 1, out, dry_run=False)
    assert len(launched) == 1, launched
    assert len(_result_rows(out)) == 1


def test_main_drops_repeated_arms_in_order(bench, tmp_path, monkeypatch):
    """`--arms solo,solo` ran every solo cell twice and tabled the arm twice."""
    seen = []
    monkeypatch.setattr(bench, "preflight_launcher", lambda: None)
    monkeypatch.setattr(bench, "bench", lambda arms, *a, **k: seen.append(arms) or 0)
    monkeypatch.setattr(bench.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        bench.sys, "argv",
        ["run_selfbench.py", "--arms", "compete,solo,compete, solo",
         "--out", str(tmp_path / "r.jsonl")],
    )
    assert bench.main() == 0
    assert seen == [["compete", "solo"]]


@pytest.mark.parametrize("line", ["null", "42", '["task", "arm", "success", "wall_s"]'])
def test_a_json_line_that_is_not_an_object_is_not_a_row(bench, tmp_path, monkeypatch, line):
    """Each one passed the header check and then crashed the resume."""
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    launched = _counting_cells(bench, monkeypatch, [task])
    out = tmp_path / "results.jsonl"
    header = json.dumps({"experiment": bench.experiment_fingerprint([task], 1)})
    out.write_text(header + "\n" + line + "\n", encoding="utf-8")
    assert bench.load_results(out) == []
    bench.bench(["solo"], 1, out, dry_run=False)
    assert len(launched) == 1


def test_an_interrupted_header_write_leaves_no_file_behind(bench, tmp_path, monkeypatch):
    """A torn header would make the same experiment's next run refuse its file."""
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    launched = _counting_cells(bench, monkeypatch, [task])

    def interrupted(src, dst):
        raise KeyboardInterrupt

    runs = tmp_path / "runs"
    runs.mkdir()
    out = runs / "results.jsonl"
    with monkeypatch.context() as m:
        m.setattr(bench.os, "replace", interrupted)
        with pytest.raises(KeyboardInterrupt):
            bench.bench(["solo"], 1, out, dry_run=False)
    assert list(runs.iterdir()) == []
    assert launched == []

    bench.bench(["solo"], 1, out, dry_run=False)
    assert len(_result_rows(out)) == 1


def test_a_dry_run_does_not_claim_the_results_file(bench, tmp_path):
    out = tmp_path / "results.jsonl"
    assert bench.bench(["solo"], 1, out, dry_run=True) == 0
    assert not out.exists()


def test_grader_ignores_python_env_that_would_strip_asserts(bench, tmp_path, monkeypatch):
    """PYTHONOPTIMIZE removes the asserts these checks are built from."""
    monkeypatch.setenv("PYTHONOPTIMIZE", "1")
    assert bench.run_check(tmp_path, {"check": "assert False\n"}) is False


def test_arms_use_real_strategy_names(bench):
    """The arms name strategies syncmill actually implements."""
    from syncmill import planning

    source = Path(planning.__file__).read_text(encoding="utf-8")
    for arm, argv in bench.ARMS.items():
        strategy = argv[argv.index("--strategy") + 1]
        assert f'== "{strategy}"' in source, (arm, strategy)


def _only_the_launcher(bench, monkeypatch, handler):
    """Route the cell's syncmill invocation to `handler`, everything else to git.

    `bench.subprocess` IS the stdlib module, so patching Popen on it also
    captures the harness's own `git init`/`add`/`commit`/`apply` calls. A cell
    that has to seed a real repository and apply a real patch therefore needs
    the fake to stand in for the launcher ONLY and hand every other process
    back to the real implementation.
    """
    real_popen = bench.subprocess.Popen

    def dispatch(argv, **kw):
        if list(argv[:len(bench.SYNCMILL)]) == list(bench.SYNCMILL):
            return handler(argv, **kw)
        return real_popen(argv, **kw)

    monkeypatch.setattr(bench.subprocess, "Popen", dispatch)


def _launcher_that_solves(bench, task_id: str):
    """A stand-in syncmill that emits a REAL patch solving one task.

    The patch is produced by git itself rather than hand-written, so what the
    sweep applies is the same artifact shape `--emit-patch` produces; a patch
    the harness could not apply would make this test pass for the wrong reason.
    """
    tasks = {t["id"]: t for t in bench.load_tasks()}

    def fake_popen(argv, **kw):
        wd = Path(kw["cwd"])
        patch = Path(argv[argv.index("--emit-patch") + 1])
        seeded = (wd / "mod.py").read_text(encoding="utf-8")
        (wd / "mod.py").write_text(tasks[task_id]["reference"], encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff"], cwd=wd, capture_output=True, text=True, check=True,
            # The helper reads the repository the harness just seeded. Ambient
            # GIT_DIR would point it elsewhere and fail the winning-path test
            # for a reason that has nothing to do with what it measures.
            env=bench.harness_git_env(),
        ).stdout
        # Hand the tree back exactly as seeded: the harness applies the patch
        # itself, and a pre-applied tree would grade without exercising that.
        (wd / "mod.py").write_text(seeded, encoding="utf-8")
        patch.write_text(diff, encoding="utf-8")
        _leave_cell_evidence(kw["env"], "pending")
        return _FakeProc(0, "")

    return fake_popen


def test_a_solved_cell_is_recorded_and_counted_as_a_win(bench, tmp_path, monkeypatch):
    """The one path every other test leaves unconstrained: a cell that WINS.

    Every other test in this file drives a loss, a crash, a timeout, or reads a
    success it wrote into the fixture itself. With none of them covering the
    winning path, `success = applied and run_check(...)` could be replaced by
    `success = False` and the whole suite stayed green while the real sweep
    reported zero for every strategy — a table that reads like a finding.

    So this drives the full width of one cell: seed a real repo, emit a real
    patch, apply it, grade it, and read the number back off both the persisted
    row and the rendered table.
    """
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    monkeypatch.setattr(bench, "load_tasks", lambda: [task])
    _only_the_launcher(bench, monkeypatch, _launcher_that_solves(bench, "slugify"))

    out = tmp_path / "results.jsonl"
    assert bench.bench(["solo"], 120, out, dry_run=False) == 0

    rows = _result_rows(out)
    assert len(rows) == 1, rows
    assert rows[0] == {"task": "slugify", "arm": "solo", "outcome": "pass",
                       "success": True, "wall_s": rows[0]["wall_s"],
                       "dispatched": ["claude"], "run_state": "pending",
                       "reason": "run record pending"}

    table = bench.render_table(rows, ["solo"])
    assert "| solo | 1/1 | 100% |" in table, table
    assert "slugify[+]" in table, table


def test_ambient_lowercase_syncmill_vars_do_not_reach_a_cell(bench, tmp_path, monkeypatch):
    """Isolation has to be case-folded, because the config layer is.

    pydantic-settings resolves `syncmill_memory__db_path` exactly like the
    upper-case spelling, so a case-sensitive filter leaves every arm pointed at
    one shared memory store — and the second arm can then retrieve the first
    arm's answer. The arms stop being independent while the sweep still prints
    three separate numbers.
    """
    monkeypatch.setenv("syncmill_memory__db_path", "/tmp/leaked-shared.db")
    monkeypatch.setenv("MeMtOmEm_HOME", "/tmp/leaked-memtomem")
    env = bench.cell_env(tmp_path / "state")
    leaked = [k for k in env if k.upper().startswith(("SYNCMILL_", "MEMTOMEM_"))
              and not k.startswith("SYNCMILL_")]
    assert leaked == [], leaked
    assert env["SYNCMILL_MEMORY__DB_PATH"].startswith(str(tmp_path)), env


def test_git_commands_do_not_inherit_ambient_repository_routing(bench, tmp_path, monkeypatch):
    """`cwd` does not bind git to a repository; the environment can redirect it.

    With an ambient GIT_DIR the seed would commit into the developer's own
    repository and the sweep would grade a tree it never wrote.
    """
    elsewhere = tmp_path / "elsewhere.git"
    monkeypatch.setenv("GIT_DIR", str(elsewhere))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/tmp/hooks'")
    wd = tmp_path / "repo"
    wd.mkdir()
    bench.seed_repo(wd, {"id": "probe"})
    assert (wd / ".git").is_dir(), "seed must create its own repository"
    assert not elsewhere.exists(), "ambient GIT_DIR must not be written to"


class _FakeProbe:
    """A launcher answering (or refusing to answer) `--version`."""

    stdout = stderr = None  # pipes the bounded reap would close

    def __init__(self, returncode: int, stderr: str = "", hang: bool = False) -> None:
        self.returncode = returncode
        self._stderr = stderr
        self._hang = hang
        self.killed = False

    def communicate(self, timeout=None):
        if self._hang and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout)
        return "", self._stderr

    def wait(self, timeout=None):
        return self.returncode


def test_a_launcher_that_dies_during_import_is_caught_before_any_cell(bench, monkeypatch):
    """A launcher that cannot import syncmill is caught before the first cell.

    Graded per cell it once became an ordinary strategy loss — the table of
    honest-looking zeros this harness exists to prevent. Its cells now reach
    no verdict and stop the sweep, but the probe stops it before any of them.
    """
    probe = _FakeProbe(
        1, "Traceback (most recent call last):\nModuleNotFoundError: No module named 'syncmill'\n")
    monkeypatch.setattr(bench.subprocess, "Popen", lambda argv, **kw: probe)
    with pytest.raises(SystemExit, match="not a benchmark result"):
        bench.preflight_launcher()


def test_a_launcher_that_never_answers_does_not_hang_the_sweep(bench, monkeypatch):
    """The probe runs before the first cell, so `--timeout` does not bound it."""
    probe = _FakeProbe(0, hang=True)
    killed = []
    monkeypatch.setattr(bench.subprocess, "Popen", lambda argv, **kw: probe)
    monkeypatch.setattr(bench, "_kill_tree", lambda p: killed.append(p))
    with pytest.raises(SystemExit, match="did not answer --version"):
        bench.preflight_launcher()
    assert killed == [probe], "a stalled probe must not be left running"


def test_a_working_launcher_passes_preflight_silently(bench, monkeypatch):
    monkeypatch.setattr(bench.subprocess, "Popen",
                        lambda argv, **kw: _FakeProbe(0, ""))
    assert bench.preflight_launcher() is None


def test_a_degraded_memory_warning_is_not_mistaken_for_a_launcher_failure(
    bench, tmp_path, monkeypatch,
):
    """syncmill degrades rather than failing, and says so on stderr.

    `SyncmillMemory._degrade` logs the caught exception verbatim, so an
    uninstalled optional backend puts "ModuleNotFoundError: No module named
    'mem0'" on the stderr of a run that goes on to succeed. A stderr net once
    read that as a launcher that never started and aborted a working sweep.
    Nothing reads stderr now; this keeps it that way.
    """
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    monkeypatch.setattr(bench, "load_tasks", lambda: [task])
    solver = _launcher_that_solves(bench, "slugify")

    def noisy(argv, **kw):
        proc = solver(argv, **kw)
        proc._stderr = ("memory unavailable during init: "
                        "ModuleNotFoundError: No module named 'mem0'\n")
        return proc

    _only_the_launcher(bench, monkeypatch, noisy)
    out = tmp_path / "results.jsonl"
    assert bench.bench(["solo"], 120, out, dry_run=False) == 0
    rows = _result_rows(out)
    assert [r["success"] for r in rows] == [True], rows


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--arms", "solo"], 1),
        (["--arms", "solo", "--dry-run"], 0),
    ],
)
def test_main_probes_the_launcher_before_a_real_sweep_only(
    bench, tmp_path, monkeypatch, argv, expected,
):
    """The probe catches a launcher that dies on import before any cell runs.

    The wiring itself is the contract — a real sweep probes first, and
    `--dry-run`, which starts no launcher at all, must not pay for it.
    """
    calls = []
    monkeypatch.setattr(bench, "preflight_launcher", lambda: calls.append(1))
    monkeypatch.setattr(bench, "bench", lambda *a, **k: 0)
    monkeypatch.setattr(bench.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(
        bench.sys, "argv",
        ["run_selfbench.py", *argv, "--out", str(tmp_path / "r.jsonl")],
    )
    assert bench.main() == 0
    assert len(calls) == expected


def test_validate_checks_does_not_probe_the_launcher(bench, monkeypatch):
    """It runs no agents and no syncmill — requiring a working launcher would
    make the one CI-safe mode depend on the dev environment."""
    calls = []
    monkeypatch.setattr(bench, "preflight_launcher", lambda: calls.append(1))
    monkeypatch.setattr(bench, "validate_checks", lambda: 0)
    monkeypatch.setattr(bench.sys, "argv", ["run_selfbench.py", "--validate-checks"])
    assert bench.main() == 0
    assert calls == []


def test_a_successful_cell_is_never_reclassified_as_a_launcher_failure(
    bench, tmp_path, monkeypatch,
):
    """Exiting zero with a patch in hand is proof the launcher started.

    A stderr net once looked for "No such file or directory", and syncmill's
    memory layer puts exactly that on the stderr of a run that succeeds: it
    degrades instead of failing and logs the caught exception verbatim, so a
    missing optional store reads as `FileNotFoundError: [Errno 2] No such file
    or directory`. Classified as an environment problem it ended the sweep and
    discarded every cell already measured.
    """
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    monkeypatch.setattr(bench, "load_tasks", lambda: [task])
    solver = _launcher_that_solves(bench, "slugify")

    def noisy(argv, **kw):
        proc = solver(argv, **kw)
        proc._stderr = ("memory unavailable during init: FileNotFoundError: "
                        "[Errno 2] No such file or directory: '/x/memtomem.db'\n")
        return proc

    _only_the_launcher(bench, monkeypatch, noisy)
    out = tmp_path / "results.jsonl"
    assert bench.bench(["solo"], 120, out, dry_run=False) == 0
    rows = _result_rows(out)
    assert [r["success"] for r in rows] == [True], rows


def test_the_seed_ignores_the_developers_global_git_configuration(
    bench, tmp_path, monkeypatch,
):
    """`isolated_git_env` drops ambient variables but keeps HOME, and git reads
    `$HOME/.gitconfig` — so a `core.hooksPath` there ran during seeding. A hook
    that rejects the commit ends the sweep; one that edits the tree changes the
    seed every arm is then measured against.
    """
    # NOT tmp_path/"home": the suite's own HOME isolation already owns that name.
    home = tmp_path / "devhome"
    hooks = tmp_path / "hooks"
    home.mkdir()
    hooks.mkdir()
    marker = tmp_path / "HOOK_RAN"
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    (home / ".gitconfig").write_text(
        f"[core]\n\thooksPath = {hooks}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    wd = tmp_path / "repo"
    wd.mkdir()
    bench.seed_repo(wd, {"id": "probe"})
    assert (wd / ".git").is_dir()
    assert not marker.exists(), "a global core.hooksPath must not run in the seed"



# --- cell teardown: timeouts and Ctrl-C (#132, #133) -------------------------
#
# syncmill starts each agent in a session of its own, so nothing the harness
# sends to the cell's process group reaches them. These stand-in launchers do
# the same: each starts an "agent" in its own session that appends to a beat
# file, which is how a test can tell whether it is still alive. Its output goes
# nowhere, as an agent's goes into syncmill's own pipes, not the harness's.
#
# A fake writes `ready` only once every process it started is beating, so a
# test acting on `ready` acts on a running cell, never a starting one.

_FAKE_PRELUDE = (
    "import os, signal, subprocess, sys, time\n"
    "d = os.environ['SELFBENCH_FAKE_DIR']\n"
    "def record(pid):\n"
    "    with open(os.path.join(d, 'pids'), 'a') as fh:\n"
    "        fh.write('%d\\n' % pid)\n"
    "record(os.getpid())\n"
    "def beat(name, **kw):\n"
    "    proc = subprocess.Popen(\n"
    "        [sys.executable, '-c',\n"
    "         'import time\\nwhile True:\\n'\n"
    "         '    open(%r, \"a\").write(\"x\")\\n    time.sleep(0.05)\\n'\n"
    "         % os.path.join(d, name)],\n"
    "        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)\n"
    "    record(proc.pid)\n"
    "    return proc\n"
    "def beating(name):\n"
    "    path = os.path.join(d, name)\n"
    "    while not (os.path.exists(path) and os.path.getsize(path)):\n"
    "        time.sleep(0.01)\n"
    "def ready():\n"
    "    open(os.path.join(d, 'ready'), 'w').write('1')\n"
    "    while True:\n"
    "        time.sleep(0.1)\n"
)

# Behaves like syncmill on Ctrl-C: kills the agent's group, then exits.
_COOPERATIVE_LAUNCHER = _FAKE_PRELUDE + (
    "agent = beat('beat', start_new_session=True)\n"
    "def stop(signum, frame):\n"
    "    os.killpg(agent.pid, signal.SIGKILL)\n"
    "    agent.wait()\n"
    "    sys.exit(130)\n"
    "signal.signal(signal.SIGINT, stop)\n"
    "beating('beat')\n"
    "ready()\n"
)

# Takes SIGINT but is slow to act on it, so a second Ctrl-C lands in the grace
# period. It says when the first one arrived.
_SLOW_LAUNCHER = _FAKE_PRELUDE + (
    "agent = beat('beat', start_new_session=True)\n"
    "def stop(signum, frame):\n"
    "    open(os.path.join(d, 'got_sigint'), 'w').write('1')\n"
    "    time.sleep(60)\n"
    "signal.signal(signal.SIGINT, stop)\n"
    "beating('beat')\n"
    "ready()\n"
)

# Ignores SIGINT, so only the group kill can end it. Its `child` stays in the
# launcher's group, as syncmill does under `uv run`, and ignores SIGINT too
# (SIG_IGN survives exec): killing the leader alone would leave it running.
_DEAF_LAUNCHER = _FAKE_PRELUDE + (
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "agent = beat('beat', start_new_session=True)\n"
    "child = beat('child_beat')\n"
    "beating('beat')\n"
    "beating('child_beat')\n"
    "ready()\n"
)


def _wait_for(path: Path, deadline_s: float = 30.0) -> None:
    end = time.monotonic() + deadline_s
    while not (path.is_file() and path.stat().st_size > 0):
        assert time.monotonic() < end, f"{path} never appeared"
        time.sleep(0.05)


def _beating(beat: Path, window_s: float = 0.6) -> bool:
    before = beat.stat().st_size
    time.sleep(window_s)
    return beat.stat().st_size != before


def _kill_recorded(pids_file: Path) -> None:
    """Kill every process a fake recorded, and each one's group."""
    if not pids_file.is_file():
        return
    for line in pids_file.read_text().split():
        pid = int(line)
        for kill in (os.killpg, os.kill):
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def fake_dir(tmp_path, monkeypatch):
    """The fakes' shared state dir; teardown kills everything they started.

    A regression that leaks processes then fails its test without leaving them
    running on the machine.
    """
    fake = tmp_path / "fake"
    fake.mkdir()
    monkeypatch.setenv("SELFBENCH_FAKE_DIR", str(fake))
    yield fake
    _kill_recorded(fake / "pids")


def _use_fake_launcher(bench, monkeypatch, tmp_path, source: str) -> None:
    """Point the sweep at `source`, for one task."""
    script = tmp_path / "launcher.py"
    script.write_text(source, encoding="utf-8")
    monkeypatch.setattr(bench, "SYNCMILL", [sys.executable, str(script)])
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    monkeypatch.setattr(bench, "load_tasks", lambda: [task])


class _FailsOnceReady(subprocess.Popen):
    """A real child whose first wait ends in `error`, once `ready` exists.

    Waiting for `ready` makes the failure land on a running cell, and makes a
    timeout a fact the test controls rather than a race against startup.
    """

    ready: Path
    error: BaseException

    def communicate(self, input=None, timeout=None):
        if not getattr(self, "_failed", False):
            self._failed = True
            _wait_for(self.ready)
            raise self.error
        return super().communicate(input, timeout)


def _launch_failing_once(bench, monkeypatch, ready: Path, error) -> list:
    launched: list = []

    def launch(argv, **kw):
        proc = _FailsOnceReady(argv, **kw)
        proc.ready, proc.error = ready, error
        launched.append(proc)
        return proc

    _only_the_launcher(bench, monkeypatch, launch)
    return launched


def test_a_timed_out_cell_lets_syncmill_stop_its_agents(
    bench, tmp_path, monkeypatch, fake_dir
):
    """SIGINT first: the launcher's group kill never reaches an agent's session.

    The old timeout SIGKILLed the launcher's group, which ended syncmill before
    it could stop agents it had started in sessions of their own. They kept
    running through every later cell.
    """
    monkeypatch.setattr(bench, "CELL_STOP_GRACE_S", 30)
    _use_fake_launcher(bench, monkeypatch, tmp_path, _COOPERATIVE_LAUNCHER)
    _launch_failing_once(bench, monkeypatch, fake_dir / "ready",
                         subprocess.TimeoutExpired("syncmill", 600))
    out = tmp_path / "results.jsonl"
    t0 = time.monotonic()
    assert bench.bench(["solo"], 600, out, dry_run=False) == 0
    elapsed = time.monotonic() - t0

    assert not _beating(fake_dir / "beat"), "the cell's agent outlived its timeout"
    # It went by SIGINT, not by sitting out the 30-second grace first.
    assert elapsed < 10, elapsed
    rows = _result_rows(out)
    assert [r["success"] for r in rows] == [False], rows


def test_ctrl_c_stops_the_running_cell_and_its_agents(
    bench, tmp_path, monkeypatch, fake_dir
):
    """The interrupt reaches the harness, not the cell, which has its own session.

    Unwinding without stopping the cell left syncmill and its agents running,
    and the resume the README offers then started a second copy beside them.
    """
    _use_fake_launcher(bench, monkeypatch, tmp_path, _COOPERATIVE_LAUNCHER)
    launched = _launch_failing_once(bench, monkeypatch, fake_dir / "ready",
                                    KeyboardInterrupt())
    out = tmp_path / "results.jsonl"
    with pytest.raises(KeyboardInterrupt):
        bench.bench(["solo"], 600, out, dry_run=False)

    assert len(launched) == 1
    assert launched[0].poll() is not None, "the launcher outlived the Ctrl-C"
    assert not _beating(fake_dir / "beat"), "the cell's agent outlived the Ctrl-C"
    # An interrupted cell is not a result; resuming runs it again.
    assert _result_rows(out) == []


def test_a_second_ctrl_c_during_the_grace_period_forces_the_kill(
    bench, tmp_path, fake_dir
):
    """Real signals, delivered to a real sweep blocked in a real wait.

    The first Ctrl-C interrupts the cell's `communicate()` and starts the grace
    period; the launcher is slow to wind down, and a second Ctrl-C must not
    then wait it out. The sweep runs in a process of its own, so the signals
    are the ones a terminal sends and the harness's handling is unmodified.
    """
    script = tmp_path / "launcher.py"
    script.write_text(_SLOW_LAUNCHER, encoding="utf-8")
    out = tmp_path / "results.jsonl"
    driver = tmp_path / "driver.py"
    driver.write_text(textwrap.dedent(f"""
        import importlib.util, sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("b", {str(_EXAMPLE)!r})
        b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(b)
        b.SYNCMILL = [sys.executable, {str(script)!r}]
        b.CELL_STOP_GRACE_S = 60
        task = next(t for t in b.load_tasks() if t["id"] == "slugify")
        b.load_tasks = lambda: [task]
        b.bench(["solo"], 600, Path({str(out)!r}), dry_run=False)
    """), encoding="utf-8")
    sweep = subprocess.Popen([sys.executable, str(driver)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _wait_for(fake_dir / "ready")
        sweep.send_signal(signal.SIGINT)
        _wait_for(fake_dir / "got_sigint")  # the launcher is in its grace period
        t0 = time.monotonic()
        sweep.send_signal(signal.SIGINT)
        _, err = sweep.communicate(timeout=30)
        elapsed = time.monotonic() - t0

        assert elapsed < 2 * 5 + 5, elapsed  # not the 60-second grace
        assert b"KeyboardInterrupt" in err, err[-500:]
        launcher_pid = int((fake_dir / "pids").read_text().split()[0])
        with pytest.raises(ProcessLookupError):
            os.kill(launcher_pid, 0)
        assert _result_rows(out) == []
    finally:
        if sweep.poll() is None:
            sweep.kill()
            sweep.wait()


def test_a_launcher_that_ignores_sigint_is_killed_after_the_grace_period(
    bench, tmp_path, monkeypatch, fake_dir
):
    """The fallback is bounded, and what it cannot reach is the documented residue.

    A launcher that ignores SIGINT dies by the group kill, and so does the
    child it keeps in its own group. Its agent, in a session of its own,
    survives: that is the leak only a container closes. The fixture kills it.
    """
    monkeypatch.setattr(bench, "CELL_STOP_GRACE_S", 1)
    _use_fake_launcher(bench, monkeypatch, tmp_path, _DEAF_LAUNCHER)
    launched = _launch_failing_once(bench, monkeypatch, fake_dir / "ready",
                                    subprocess.TimeoutExpired("syncmill", 600))
    t0 = time.monotonic()
    assert bench.bench(["solo"], 600, tmp_path / "out.jsonl", dry_run=False) == 0
    elapsed = time.monotonic() - t0

    assert launched[0].returncode == -signal.SIGKILL, launched[0].returncode
    assert elapsed < 10 + 1 + 2 * bench.REAP_TIMEOUT_S, elapsed
    assert not _beating(fake_dir / "child_beat"), "the launcher's own group outlived it"
    assert _beating(fake_dir / "beat"), "expected the residue the README documents"


def test_ctrl_c_during_grading_kills_the_grader(bench, tmp_path, monkeypatch):
    """The graded code runs in its own session too, so a Ctrl-C never reaches it."""
    started = tmp_path / "grader.started"
    check = (f"open({str(started)!r}, 'w').write('up')\n"
             "import time\ntime.sleep(60)\n")
    launched = _record_graders(bench, monkeypatch)

    def interrupted(proc, timeout):
        _wait_for(started)
        raise KeyboardInterrupt

    monkeypatch.setattr(bench, "_wait_unreaped", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            bench.run_check(tmp_path, {"check": check})
        assert len(launched) == 1
        assert launched[0].poll() is not None, "the grader outlived the Ctrl-C"
    finally:
        for proc in launched:  # a regression must not leave it sleeping
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


def _record_graders(bench, monkeypatch) -> list:
    """Hand back every grader process run_check starts, so a test can find it."""
    launched: list = []
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        proc = real_popen(argv, **kw)
        if "_wrapper.py" in argv:
            launched.append(proc)
        return proc

    monkeypatch.setattr(bench.subprocess, "Popen", popen)
    return launched


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_grader_child_that_outlives_its_reaped_parent_is_killed(
    bench, tmp_path, monkeypatch
):
    """The group is named by pid, so it can still be killed after the leader exits.

    The grader forks a child that keeps running in its process group, then
    exits. By the time the interrupt lands the leader is gone; asking
    `getpgid` about it fails, and an old cleanup gave up there, leaving the
    child running.
    """
    started, beat = tmp_path / "child.pid", tmp_path / "child.beat"
    check = textwrap.dedent(f"""
        import os, time
        if os.fork() == 0:
            os.close(1)
            os.close(2)
            open({str(started)!r}, "w").write(str(os.getpid()))
            while True:
                open({str(beat)!r}, "a").write("x")
                time.sleep(0.05)
        while not os.path.exists({str(started)!r}):
            time.sleep(0.01)
    """)
    real_wait = bench._wait_unreaped

    def interrupted_after_exit(proc, timeout):
        assert real_wait(proc, timeout), "the grader's leader never exited"
        raise KeyboardInterrupt

    monkeypatch.setattr(bench, "_wait_unreaped", interrupted_after_exit)
    try:
        with pytest.raises(KeyboardInterrupt):
            bench.run_check(tmp_path, {"check": check})
        _wait_for(beat)
        assert not _beating(beat), "the grader's child outlived the Ctrl-C"
    finally:
        if started.is_file():
            try:
                os.kill(int(started.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


# Stops its agent on SIGINT like syncmill, but says goodbye in bytes that are
# not UTF-8 on the way out.
_NOISY_LAUNCHER = _FAKE_PRELUDE + (
    "agent = beat('beat', start_new_session=True)\n"
    "def stop(signum, frame):\n"
    "    os.killpg(agent.pid, signal.SIGKILL)\n"
    "    agent.wait()\n"
    "    os.write(1, b'\\xff\\xfe not utf-8\\n')\n"
    "    os.write(2, b'\\xff\\xfe not utf-8\\n')\n"
    "    os._exit(130)\n"
    "signal.signal(signal.SIGINT, stop)\n"
    "beating('beat')\n"
    "ready()\n"
)


def test_undecodable_output_during_teardown_does_not_replace_the_ctrl_c(
    bench, tmp_path, monkeypatch, fake_dir
):
    """Output the harness discards must not raise while it is being discarded.

    Decoding it strictly turned a Ctrl-C into a UnicodeDecodeError, so the
    caller saw a crash where it should have seen the interrupt.
    """
    _use_fake_launcher(bench, monkeypatch, tmp_path, _NOISY_LAUNCHER)
    _launch_failing_once(bench, monkeypatch, fake_dir / "ready", KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        bench.bench(["solo"], 600, tmp_path / "out.jsonl", dry_run=False)
    assert not _beating(fake_dir / "beat"), "the cell's agent outlived the Ctrl-C"


def test_a_check_that_prints_bytes_that_are_not_utf8_is_still_graded(bench, tmp_path):
    """Graded code may print anything; that must not crash the sweep."""
    check = ("import sys\n"
             "sys.stdout.buffer.write(b'\\xff\\xfe\\n')\n"
             "sys.stdout.flush()\n"
             "sys.stderr.buffer.write(b'\\xff\\n')\n")
    assert bench.run_check(tmp_path, {"check": check}) is True


# ── Outcomes from positive evidence (#114, #136) ─────────────────────────────


def test_the_agent_binaries_are_the_ones_syncmill_execs(bench):
    """The shims only see launches of the names syncmill actually runs."""
    from syncmill.agents import _AGENT_CONSTRUCTORS

    assert set(bench._AGENT_BINARIES) == set(_AGENT_CONSTRUCTORS)
    for agent_id, make in _AGENT_CONSTRUCTORS.items():
        assert make()._build_command("x")[0] == bench._AGENT_BINARIES[agent_id]


_VERDICT = dict(
    dispatched=[("claude", 1)], exec_errors=0, malformed=False, expected=set(),
    timed_out=False, agents_running=False, run_state="pending",
    state_reason="pending", returncode=0, patch_exists=True,
)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({}, ("grade", "run record pending")),
        ({"malformed": True, "exec_errors": 1},
         ("infrastructure-error", "unreadable launch receipt")),
        ({"exec_errors": 1}, ("infrastructure-error", "an agent binary failed to exec")),
        ({"dispatched": [], "timed_out": True, "agents_running": True},
         ("infrastructure-error", "no agent dispatched")),
        ({"expected": {"claude", "codex"}, "timed_out": True, "agents_running": True},
         ("infrastructure-error", "not dispatched: codex")),
        ({"timed_out": True, "agents_running": True, "run_state": "recovery_required"},
         ("timeout", "agents running at the deadline")),
        ({"timed_out": True, "run_state": "failed"},
         ("infrastructure-error", "stalled after its agents exited")),
        ({"run_state": None, "state_reason": "0 run records"},
         ("infrastructure-error", "0 run records")),
        ({"run_state": "running"}, ("infrastructure-error", "run record running")),
        ({"run_state": "recovery_required"},
         ("infrastructure-error", "run record recovery_required")),
        ({"returncode": 1}, ("infrastructure-error", "run record pending with exit 1")),
        ({"patch_exists": False},
         ("infrastructure-error", "run record pending with exit 0")),
        ({"run_state": "no_changes", "patch_exists": False},
         ("wrong-answer", "completed without changes")),
        ({"run_state": "no_changes", "returncode": 1},
         ("infrastructure-error", "run record no_changes with exit 1")),
        ({"run_state": "failed", "returncode": -9},
         ("infrastructure-error", "run record failed with exit -9")),
        ({"run_state": "failed", "returncode": 1},
         ("strategy-failed", "run record failed")),
    ],
    ids=lambda v: v[1] if isinstance(v, tuple) else None,
)
def test_each_classification_rule(bench, change, expected):
    """Each case also satisfies every later rule's trigger, so precedence bites."""
    assert bench.classify(**{**_VERDICT, **change}) == expected


def test_receipts_count_only_an_exec_that_was_not_withdrawn(bench, tmp_path):
    path = tmp_path / "receipts.jsonl"
    lines = [{"event": "exec", "agent": "claude", "pid": 10},
             {"event": "exec", "agent": "codex", "pid": 11},
             {"event": "exec-error", "agent": "codex", "pid": 11, "errno": 2}]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    assert bench.read_receipts(path) == ([("claude", 10)], 1, False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "exec", "agent": "claude"}\n')
    assert bench.read_receipts(path)[2] is True
    assert bench.read_receipts(tmp_path / "absent") == ([], 0, False)
    unreadable = tmp_path / "a-directory"
    unreadable.mkdir()
    assert bench.read_receipts(unreadable) == ([], 0, True)


def test_the_cell_is_stopped_even_if_the_deadline_check_fails(
    bench, tmp_path, monkeypatch, fake_dir,
):
    """The evidence is read before the stop; an error there must not skip it."""
    _use_fake_launcher(bench, monkeypatch, tmp_path, _WORKING_AT_DEADLINE)
    _launch_failing_once(bench, monkeypatch, fake_dir / "ready",
                         subprocess.TimeoutExpired("syncmill", 600))

    def broken(pid):
        raise RuntimeError("probe failed")

    monkeypatch.setattr(bench, "_group_alive", broken)
    with pytest.raises(RuntimeError, match="probe failed"):
        bench.bench(["solo"], 600, tmp_path / "results.jsonl", dry_run=False)
    assert not _beating(fake_dir / "beat"), "the agent outlived a failed deadline check"


def test_a_run_record_must_be_exactly_one_known_state(bench, tmp_path):
    root = tmp_path / "projects"
    assert bench.read_run_state(root) == (None, "0 run records")
    run = root / "p" / "runs" / "r1"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text('{"state": "applied"}', encoding="utf-8")
    assert bench.read_run_state(root) == (None, "run record state 'applied'")
    (run / "manifest.json").write_text('{"state": "failed"}', encoding="utf-8")
    assert bench.read_run_state(root) == ("failed", "failed")
    (run / "manifest.json").write_text("[]", encoding="utf-8")
    assert bench.read_run_state(root) == (None, "run record state None")
    for bad in ('{"state": []}', '{"state": {}}'):
        (run / "manifest.json").write_text(bad, encoding="utf-8")
        assert bench.read_run_state(root)[0] is None, bad
    other = root / "p" / "runs" / "r2"
    other.mkdir()
    (other / "manifest.json").write_text('{"state": "failed"}', encoding="utf-8")
    assert bench.read_run_state(root) == (None, "2 run records")


def _real_agent(tmp_path: Path, name: str, body: str, where: str = "real") -> str:
    real = tmp_path / where / name
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    real.chmod(0o755)
    return str(real)


def _shim_env(shim_dir: Path) -> dict:
    return {**os.environ, "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}"}


def test_the_shim_is_the_agent_process(bench, tmp_path):
    """One pid; argv, output and exit status as a direct launch sees them."""
    real = _real_agent(tmp_path, "claude", """
        import os, sys
        print(os.getpid(), sys.argv, flush=True)
        sys.stderr.write("to stderr\\n")
        sys.exit(7)
    """)
    state = tmp_path / "state"
    shim_dir = bench.write_shims(state, {"claude": real})

    def launch(env):
        proc = subprocess.Popen(["claude", "a b", "--c"], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        out, err = proc.communicate(timeout=30)
        pid, _, argv = out.partition(" ")
        assert pid == str(proc.pid), out
        return proc.pid, (proc.returncode, argv, err)

    direct_env = {**os.environ,
                  "PATH": f"{Path(real).parent}{os.pathsep}{os.environ['PATH']}"}
    _, direct = launch(direct_env)
    pid, shimmed = launch(_shim_env(shim_dir))
    assert shimmed == direct
    assert direct[0] == 7 and direct[2] == "to stderr\n", direct
    assert bench.read_receipts(state / "receipts.jsonl") == ([("claude", pid)], 0, False)


def test_the_shim_does_not_leave_signals_ignored(bench, tmp_path):
    """Python ignores SIGPIPE, and an ignored signal survives exec.

    Through an unrepaired shim the agent could not be killed by a closed pipe:
    `kill -PIPE $$` printed "survived" and exited 0 instead of dying.
    """
    real = tmp_path / "real" / "claude"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\nkill -PIPE $$\necho survived\n", encoding="utf-8")
    real.chmod(0o755)
    shim_dir = bench.write_shims(tmp_path / "state", {"claude": str(real)})
    proc = subprocess.run(["claude"], capture_output=True, text=True,
                          env=_shim_env(shim_dir), timeout=30)
    assert proc.returncode == -signal.SIGPIPE, (proc.returncode, proc.stdout)
    assert "survived" not in proc.stdout


def test_a_binary_that_cannot_exec_is_not_a_dispatch(bench, tmp_path):
    real = tmp_path / "real" / "claude"
    real.parent.mkdir(parents=True)
    real.write_text("not a program\n", encoding="utf-8")  # not executable
    state = tmp_path / "state"
    shim_dir = bench.write_shims(state, {"claude": str(real)})
    proc = subprocess.run(["claude"], capture_output=True, text=True,
                          env=_shim_env(shim_dir), timeout=30)
    assert proc.returncode == 127
    assert "cannot exec" in proc.stderr
    assert bench.read_receipts(state / "receipts.jsonl") == ([], 1, False)


def test_an_agent_calling_another_by_name_reaches_the_real_one(bench, tmp_path):
    """The shim takes itself off PATH, so only syncmill's launches are counted."""
    marker = tmp_path / "inner-ran"
    inner = _real_agent(tmp_path, "codex", f"""
        import os
        open({str(marker)!r}, "w").write(os.environ["PATH"])
    """, where="inner")
    outer = _real_agent(tmp_path, "claude", """
        import subprocess, sys
        sys.exit(subprocess.run(["codex"]).returncode)
    """)
    state = tmp_path / "state"
    shim_dir = bench.write_shims(state, {"claude": outer, "codex": inner})
    # The real codex sits AFTER the shims, the way an installed CLI does.
    env = {**os.environ, "PATH": os.pathsep.join(
        [str(shim_dir), str(Path(inner).parent), os.environ["PATH"]])}
    proc = subprocess.run(["claude"], env=env, timeout=30)
    assert proc.returncode == 0
    assert str(shim_dir) not in marker.read_text().split(os.pathsep)
    dispatched, _, _ = bench.read_receipts(state / "receipts.jsonl")
    assert [agent for agent, _ in dispatched] == ["claude"]


def test_an_agent_group_with_a_live_member_counts_as_running(bench):
    """The leader can exit while a descendant still holds the agent's pipes."""
    leader = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & echo $!"], stdout=subprocess.PIPE, text=True,
        start_new_session=True)
    child = int(leader.stdout.readline())
    leader.wait(timeout=10)
    try:
        assert bench._group_alive(leader.pid) is True
    finally:
        os.kill(child, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while bench._group_alive(leader.pid):
        assert time.monotonic() < deadline, "group never emptied"
        time.sleep(0.05)


def test_resolve_agents_refuses_a_missing_binary_unless_disabled(
    bench, tmp_path, monkeypatch,
):
    found = {"claude": "/opt/claude", "codex": "/opt/codex"}
    monkeypatch.setattr(bench.shutil, "which", lambda name: found.get(name))
    monkeypatch.setattr(bench, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit, match=r"(?s)not found: kimi \(kimi-code\).*--disable-agent"):
        bench.resolve_agents(())
    assert bench.resolve_agents(("kimi-code",)) == found


def test_an_agent_in_the_project_venv_would_bypass_the_receipts(
    bench, tmp_path, monkeypatch,
):
    monkeypatch.setattr(bench.shutil, "which", lambda name: f"/opt/{name}")
    monkeypatch.setattr(bench, "REPO_ROOT", tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "codex").write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="codex installed in"):
        bench.resolve_agents(())


def test_a_disabled_agent_is_switched_off_in_the_cell_and_in_the_header(
    bench, tmp_path, monkeypatch,
):
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    envs, shims = [], []

    def launch(argv, **kw):
        envs.append(kw["env"])
        shims.append(sorted(p.name for p in
                            Path(kw["env"]["PATH"].split(os.pathsep)[0]).iterdir()))
        return _FakeProc(1, "")

    monkeypatch.setattr(bench, "load_tasks", lambda: [task])
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    monkeypatch.setattr(bench.subprocess, "Popen", launch)
    out = tmp_path / "results.jsonl"
    bench.bench(["solo"], 1, out, dry_run=False, disabled=("kimi-code",),
                agents={"claude": "/opt/claude", "codex": "/opt/codex"})

    env = envs[0]
    assert env["SYNCMILL_KIMI_CODE__ENABLED"] == "false"
    assert not any(k.endswith("__ENABLED") for k in env if "KIMI" not in k), env
    assert shims == [["_shim.py", "claude", "codex"]]
    header = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
    assert header["experiment"]["disabled_agents"] == ["kimi-code"]


def test_main_passes_the_lineup_to_the_sweep(bench, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(bench, "preflight_launcher", lambda: None)
    monkeypatch.setattr(bench, "resolve_agents",
                        lambda disabled: {"claude": "/opt/claude"})
    monkeypatch.setattr(bench, "bench", lambda *a, **k: seen.update(k) or 0)
    monkeypatch.setattr(bench.shutil, "which", lambda name: "/usr/bin/uv")
    monkeypatch.setattr(bench.sys, "argv", [
        "run_selfbench.py", "--arms", "compete", "--disable-agent", "kimi-code",
        "--disable-agent", "codex", "--disable-agent", "codex",
        "--out", str(tmp_path / "r.jsonl")])
    assert bench.main() == 0
    assert seen == {"disabled": ("codex", "kimi-code"),
                    "agents": {"claude": "/opt/claude"}}


@pytest.mark.parametrize("argv", [
    ["--arms", "solo", "--disable-agent", "claude"],
    ["--arms", "solo", "--disable-agent", "gemini"],
])
def test_main_rejects_a_lineup_it_cannot_run(bench, tmp_path, monkeypatch, argv):
    monkeypatch.setattr(bench, "preflight_launcher", lambda: None)
    monkeypatch.setattr(bench, "bench", lambda *a, **k: pytest.fail("ran"))
    monkeypatch.setattr(bench.sys, "argv",
                        ["run_selfbench.py", *argv, "--out", str(tmp_path / "r")])
    with pytest.raises(SystemExit) as exc:
        bench.main()
    assert exc.value.code == 2


def test_compete_without_one_of_its_agents_is_not_scored(bench, tmp_path, monkeypatch):
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    monkeypatch.setattr(bench, "load_tasks", lambda: [task])
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    monkeypatch.setattr(
        bench.subprocess, "Popen",
        lambda argv, **kw: _leave_cell_evidence(kw["env"], "failed")
        or _FakeProc(1, ""))
    out = tmp_path / "results.jsonl"
    bench.bench(["compete"], 1, out, dry_run=False,
                agents={"claude": "/opt/claude", "codex": "/opt/codex"})
    [row] = _result_rows(out)
    assert (row["outcome"], row["reason"]) == (
        "infrastructure-error", "not dispatched: codex"), row


def test_an_agent_saying_no_such_file_is_a_loss_and_the_sweep_goes_on(
    bench, tmp_path, monkeypatch,
):
    """#136: the agent's own tool output stopped the sweep as a broken environment."""
    tasks = bench.load_tasks()[:2]
    monkeypatch.setattr(bench, "load_tasks", lambda: tasks)
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    launched = []

    def launch(argv, **kw):
        launched.append(argv)
        _leave_cell_evidence(kw["env"], "failed")
        return _FakeProc(1, "cat: notes.txt: No such file or directory\n")

    monkeypatch.setattr(bench.subprocess, "Popen", launch)
    out = tmp_path / "results.jsonl"
    assert bench.bench(["solo"], 1, out, dry_run=False) == 0
    assert len(launched) == 2
    assert [r["outcome"] for r in _result_rows(out)] == ["strategy-failed"] * 2


def test_a_format_1_results_file_is_refused_unchanged(bench, tmp_path, monkeypatch):
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    launched = _counting_cells(bench, monkeypatch, [task])
    header = dict(bench.experiment_fingerprint([task], 1), format=1)
    del header["disabled_agents"]
    out = tmp_path / "results.jsonl"
    body = (json.dumps({"experiment": header}) + "\n"
            + json.dumps({"task": "slugify", "arm": "solo", "success": False,
                          "wall_s": 1.0}) + "\n")
    out.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit, match=r"disabled_agents, format changed"):
        bench.bench(["solo"], 1, out, dry_run=False)
    assert launched == []
    assert out.read_text(encoding="utf-8") == body


def test_a_row_without_a_known_outcome_is_not_read(bench, tmp_path):
    out = tmp_path / "results.jsonl"
    rows = [{"task": "a", "arm": "solo", "success": True, "wall_s": 1.0},
            {"task": "b", "arm": "solo", "outcome": "won", "success": True, "wall_s": 1.0},
            {"task": "c", "arm": "solo", "outcome": "timeout", "success": False,
             "wall_s": 1.0}]
    out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert [r["task"] for r in bench.load_results(out)] == ["c"]


def test_the_rate_leaves_out_what_reached_no_verdict(bench):
    def row(task, arm, outcome, wall=10.0):
        return {"task": task, "arm": arm, "outcome": outcome,
                "success": outcome == "pass", "wall_s": wall}

    table = bench.render_table([
        row("a", "solo", "pass", 10.0), row("b", "solo", "infrastructure-error", 500.0),
        row("a", "compete", "timeout"), row("b", "compete", "pass"),
        row("a", "decompose", "infrastructure-error"),
    ], ["solo", "compete", "decompose"])
    assert "| solo | 1/1 | 100% | 1 | 10 |" in table, table
    assert "| compete | 1/2 | 50% | 0 | 10 |" in table, table
    assert "| decompose | 0/0 | n/a | 1 | n/a |" in table, table
    assert "a[+-!]" in table and "b[!+.]" in table, table


_RECEIPT_PRELUDE = _FAKE_PRELUDE + (
    "def receipt(pid):\n"
    "    path = os.path.join(\n"
    "        os.path.dirname(os.environ['SYNCMILL_RUNS__ROOT']), 'receipts.jsonl')\n"
    "    with open(path, 'a') as fh:\n"
    "        fh.write('{\"event\": \"exec\", \"agent\": \"claude\", \"pid\": %d}\\n' % pid)\n"
)

# syncmill with its agent still working at the deadline.
_WORKING_AT_DEADLINE = _RECEIPT_PRELUDE + (
    "agent = beat('beat', start_new_session=True)\n"
    "receipt(agent.pid)\n"
    "def stop(signum, frame):\n"
    "    os.killpg(agent.pid, signal.SIGKILL)\n"
    "    agent.wait()\n"
    "    sys.exit(130)\n"
    "signal.signal(signal.SIGINT, stop)\n"
    "beating('beat')\n"
    "ready()\n"
)

# syncmill stuck after its only agent has exited.
_STALLED_AFTER_AGENT = _RECEIPT_PRELUDE + (
    "agent = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)\n"
    "agent.wait()\n"
    "receipt(agent.pid)\n"
    "ready()\n"
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_WORKING_AT_DEADLINE, ("timeout", "agents running at the deadline")),
        (_STALLED_AFTER_AGENT,
         ("infrastructure-error", "stalled after its agents exited")),
    ],
    ids=["working", "stalled"],
)
def test_a_deadline_is_scored_only_while_an_agent_is_working(
    bench, tmp_path, monkeypatch, fake_dir, source, expected,
):
    _use_fake_launcher(bench, monkeypatch, tmp_path, source)
    _launch_failing_once(bench, monkeypatch, fake_dir / "ready",
                         subprocess.TimeoutExpired("syncmill", 600))
    out = tmp_path / "results.jsonl"
    bench.bench(["solo"], 600, out, dry_run=False)
    [row] = _result_rows(out)
    assert (row["outcome"], row["reason"]) == expected, row
    if (fake_dir / "beat").exists():
        assert not _beating(fake_dir / "beat"), "the agent outlived the deadline"


_FAKE_CLAUDE = """
import json, os, sys, time
open({started!r}, "w").write(str(os.getpid()))
if {hang!r}:
    time.sleep(300)
open("mod.py", "w").write({reference!r})
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "result": "done", "session_id": "00000000-0000-4000-8000-000000000000"}}))
sys.exit({exit_code!r})
"""


def _controlled_path(tmp_path: Path) -> str:
    """`uv` and `git` only, so no installed agent CLI can stand in for a fake."""
    bin_dir = tmp_path / "tools"
    bin_dir.mkdir()
    for tool in ("uv", "git"):
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    return os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"])


@pytest.mark.skipif(os.environ.get("SYNCMILL_TEST_SELFBENCH_REAL") != "1",
                    reason="set SYNCMILL_TEST_SELFBENCH_REAL=1 to run real syncmill")
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("solves", ("pass", "pending")),
        ("exits-3", ("strategy-failed", "failed")),
        ("cannot-exec", ("infrastructure-error", "failed")),
        ("hangs", ("timeout", "recovery_required")),
    ],
)
def test_real_syncmill_leaves_the_evidence_the_classifier_reads(
    bench, tmp_path, monkeypatch, case, expected,
):
    """Production shape: real `syncmill run`, real shims, a fake `claude` binary.

    The other tests write receipts and run records themselves; this one proves
    syncmill and the shim produce exactly what they imitate.
    """
    task = next(t for t in bench.load_tasks() if t["id"] == "slugify")
    monkeypatch.setattr(bench, "load_tasks", lambda: [task])
    monkeypatch.setenv("PATH", _controlled_path(tmp_path))
    started = tmp_path / "agent-started"
    real = _real_agent(tmp_path, "claude", _FAKE_CLAUDE.format(
        started=str(started), hang=case == "hangs", reference=task["reference"],
        exit_code=3 if case == "exits-3" else 0))
    if case == "cannot-exec":
        Path(real).chmod(0o644)
    if case == "hangs":
        # The deadline lands once the agent body is running, not on a timer.
        _launch_failing_once(bench, monkeypatch, started,
                             subprocess.TimeoutExpired("syncmill", 600))
    out = tmp_path / "results.jsonl"
    bench.bench(["solo"], 600, out, dry_run=False, agents={"claude": real})
    [row] = _result_rows(out)
    assert (row["outcome"], row["run_state"]) == expected, row
    if case == "cannot-exec":
        assert row["reason"] == "an agent binary failed to exec", row
        assert row["dispatched"] == [], row
        return
    assert row["dispatched"] == ["claude"], row
    if case == "hangs":
        agent = int(started.read_text())
        deadline = time.monotonic() + 30
        while bench._group_alive(agent):
            assert time.monotonic() < deadline, "the timed-out agent kept running"
            time.sleep(0.1)


# ── Grader and probe teardown (#152, #153) ───────────────────────────────────

_DETACHED_HOLDER = """
import os, sys, time
if os.fork() == 0:
    os.setsid()  # out of the group the timeout kills, still holding the pipes
    open({pid_file!r}, "w").write(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
while not os.path.exists({pid_file!r}):
    time.sleep(0.01)
time.sleep(60)
"""


def _kill_pid_in(pid_file: Path) -> None:
    if pid_file.is_file():
        try:
            os.kill(int(pid_file.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _returns_within(fn, deadline_s: float) -> tuple[list, threading.Thread]:
    """Run `fn` on a thread: what it returned or raised ([] if still running)."""
    outcome: list = []

    def target():
        try:
            outcome.append(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - reported back to the test
            outcome.append(("raised", exc))

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout=deadline_s)
    return outcome, worker


def _record_processes(bench, monkeypatch) -> list:
    launched: list = []
    real_popen = subprocess.Popen

    def popen(argv, **kw):
        launched.append(real_popen(argv, **kw))
        return launched[-1]

    monkeypatch.setattr(bench.subprocess, "Popen", popen)
    return launched


def _clean_up(pid_file: Path, launched: list, worker: threading.Thread | None) -> None:
    """Kill everything a timeout test started, even when the fix regressed."""
    _kill_pid_in(pid_file)
    for proc in launched:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.kill()
        except OSError:
            pass
    if worker is not None:
        worker.join(timeout=10)
    for proc in launched:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


# The configured timeout, two bounded reap waits, and room for a slow machine.
_TIMEOUT_TEST_DEADLINE_S = 1 + 2 * 1 + 5


def test_a_grader_timeout_does_not_wait_on_a_descendant_that_left_its_group(
    bench, tmp_path, monkeypatch,
):
    """#152: the group kill cannot reach it, and it holds the pipes open.

    The timeout path then waited for an EOF that only came when that
    descendant exited: 10 s past a 1 s timeout when this was filed.
    """
    monkeypatch.setattr(bench, "CHECK_TIMEOUT_S", 1)
    monkeypatch.setattr(bench, "REAP_TIMEOUT_S", 1)
    pid_file = tmp_path / "holder.pid"
    check = _DETACHED_HOLDER.format(pid_file=str(pid_file))
    launched = _record_processes(bench, monkeypatch)
    worker = None
    try:
        outcome, worker = _returns_within(
            lambda: bench.run_check(tmp_path, {"check": check}), _TIMEOUT_TEST_DEADLINE_S)
        assert pid_file.is_file(), "the detached holder never started"
        assert outcome == [("ok", False)], outcome or "run_check is still waiting"
    finally:
        _clean_up(pid_file, launched, worker)


def test_a_probe_timeout_does_not_wait_on_a_descendant_that_left_its_group(
    bench, tmp_path, monkeypatch,
):
    """#152, for the launcher probe: the one wait `--timeout` does not cover."""
    pid_file = tmp_path / "holder.pid"
    script = tmp_path / "launcher.py"
    script.write_text(_DETACHED_HOLDER.format(pid_file=str(pid_file)), encoding="utf-8")
    monkeypatch.setattr(bench, "SYNCMILL", [sys.executable, str(script)])
    monkeypatch.setattr(bench, "PREFLIGHT_TIMEOUT_S", 1)
    monkeypatch.setattr(bench, "REAP_TIMEOUT_S", 1)
    launched = _record_processes(bench, monkeypatch)
    worker = None
    try:
        outcome, worker = _returns_within(
            bench.preflight_launcher, _TIMEOUT_TEST_DEADLINE_S)
        assert pid_file.is_file(), "the detached holder never started"
        assert outcome, "preflight_launcher is still waiting"
        kind, exc = outcome[0]
        assert kind == "raised" and isinstance(exc, SystemExit), outcome
        assert "did not answer --version" in str(exc)
    finally:
        _clean_up(pid_file, launched, worker)


def test_a_grade_that_returns_normally_leaves_no_child_running(bench, tmp_path):
    """#153: closing its pipes let a forked child outlive the grade.

    `communicate()` returns at EOF, which came as soon as the child closed its
    copies, and nothing killed the group on that path. The child ran on
    through later cells, loading the CPU their `wall_s` measures.
    """
    started, beat = tmp_path / "child.pid", tmp_path / "child.beat"
    check = textwrap.dedent(f"""
        import os, time
        if os.fork() == 0:
            os.close(1)
            os.close(2)
            open({str(started)!r}, "w").write(str(os.getpid()))
            while True:
                open({str(beat)!r}, "a").write("x")
                time.sleep(0.05)
        while not (os.path.exists({str(beat)!r}) and os.path.getsize({str(beat)!r})):
            time.sleep(0.01)
    """)
    try:
        assert bench.run_check(tmp_path, {"check": check}) is True
        _wait_for(beat)
        assert not _beating(beat), "the grader's child outlived a normal grade"
    finally:
        _kill_pid_in(started)


@pytest.mark.parametrize("check", ["pass\n", "import time\ntime.sleep(60)\n"],
                         ids=["returns", "times-out"])
def test_the_grader_group_is_killed_before_its_leader_is_reaped(
    bench, tmp_path, monkeypatch, check,
):
    """Killing a group by an id nobody holds any more could hit someone else's.

    An exited leader that has not been reaped keeps its pid, so the group id
    stays this grader's for as long as the kill takes. Pid reuse itself cannot
    be staged in a test; the order that rules it out can.
    """
    monkeypatch.setattr(bench, "CHECK_TIMEOUT_S", 1)
    seen: list = []
    real_kill = bench._kill_tree

    def kill(proc):
        seen.append(proc.returncode)  # None until something reaps it
        real_kill(proc)

    monkeypatch.setattr(bench, "_kill_tree", kill)
    bench.run_check(tmp_path, {"check": check})
    assert seen == [None], seen
