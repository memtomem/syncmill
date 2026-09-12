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
import subprocess
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


def test_corpus_is_solvable_and_the_graders_discriminate(bench):
    """Every check passes its reference solution and fails an empty seed.

    A check that passes the empty seed is not grading the task.
    """
    assert bench.validate_checks() == 0


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


def test_the_receipt_path_is_hidden_from_the_graded_code(bench, tmp_path):
    """The check cannot write the receipt itself: it never learns the path."""
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
        lambda state: seen.append(state) or {"HOME": str(state)},
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
        {"task": "a", "arm": "solo", "success": True, "wall_s": 1.0},
        {"task": "a", "arm": "compete", "success": True, "wall_s": 2.0},
        {"task": "b", "arm": "solo", "success": False, "wall_s": 3.0},
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

    written = [json.loads(line) for line in out.read_text().splitlines() if line]
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


def test_launcher_failure_is_not_recorded_as_a_loss(bench, tmp_path, monkeypatch):
    """`uv run` from a scratch repo could not find syncmill at all.

    Every cell then recorded a loss and the sweep printed a table of zeros that
    read like a finding.
    """
    monkeypatch.setattr(bench, "seed_repo", lambda wd, task: None)
    monkeypatch.setattr(
        bench.subprocess, "Popen",
        lambda argv, **kw: _FakeProc(1, "error: Failed to spawn: `syncmill`\n"),
    )
    with pytest.raises(SystemExit, match="not a benchmark result"):
        bench.bench(["solo"], 1, tmp_path / "out.jsonl", dry_run=False)


def test_an_interrupted_sweep_resumes_instead_of_restarting(bench, tmp_path, monkeypatch):
    out = tmp_path / "results.jsonl"
    tasks = bench.load_tasks()
    out.write_text(
        json.dumps({"task": tasks[0]["id"], "arm": "solo",
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
            env=bench.isolated_git_env(),
        ).stdout
        # Hand the tree back exactly as seeded: the harness applies the patch
        # itself, and a pre-applied tree would grade without exercising that.
        (wd / "mod.py").write_text(seeded, encoding="utf-8")
        patch.write_text(diff, encoding="utf-8")
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

    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    assert len(rows) == 1, rows
    assert rows[0] == {"task": "slugify", "arm": "solo",
                       "success": True, "wall_s": rows[0]["wall_s"]}

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

    def __init__(self, returncode: int, stderr: str = "", hang: bool = False) -> None:
        self.returncode = returncode
        self._stderr = stderr
        self._hang = hang
        self.killed = False

    def communicate(self, timeout=None):
        if self._hang and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="probe", timeout=timeout)
        return "", self._stderr


def test_a_launcher_that_dies_during_import_is_caught_before_any_cell(bench, monkeypatch):
    """`ModuleNotFoundError` prints none of the spawn messages the stderr net looks for.

    Graded per cell it became an ordinary strategy loss — the table of
    honest-looking zeros this harness exists to prevent. It is caught by asking
    the launcher to identify itself once, BEFORE the first cell, which is
    positive evidence rather than an absence of known strings.
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
    'mem0'" on the stderr of a run that goes on to succeed. Treating that
    substring as proof the launcher never started aborts a sweep that is
    working, and discards every cell already measured.
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
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()
            if line.strip()]
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
    """The probe is the only thing left catching a launcher that dies on import.

    Nothing else pins it: the stderr net deliberately does not match import
    failures, because that substring also appears when a working run's memory
    backend degrades. So the wiring itself is the contract — a real sweep probes
    first, and `--dry-run`, which starts no launcher at all, must not pay for it.
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
