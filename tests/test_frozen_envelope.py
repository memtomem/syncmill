"""Released run-report directories read by HEAD (#271, Run envelope family).

`tests/fixtures/persisted/envelope/<release>/runs/` holds run directories that
v0.1.2, v0.2.0 and v0.3.0 wrote through their own `syncmill run` / `board run`
with report recording on (see `freeze.py` there): `envelope.json`,
`result.json`, `trace.otlp.json` and, from v0.1.3 on, the `.syncmill-run-dir`
sentinel. `index.json` holds what each release's own CLI printed for
`report list` and `report show`, the oracle these tests compare HEAD with.

The readers: the schema (`contracts/run-envelope.schema.json`, the contract
tracegraph reads), `report.load_envelope` / `list_reports` behind
`report list` / `report show`, the TUI Reports tab, the dashboard REPORT pane,
and retention (`report._is_run_dir` through `RunReporter.start` and the
Supervisor's trace-only sweep), whose misread deletes data or leaks it.

Preflight blocks appear here only as `disabled`; the released preflight
records are the second part of this family. `trace.otlp.json` is evidence for
the digest and for retention ownership; its meaning is tracegraph's to test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner
from textual.widgets import OptionList

from syncmill.board import Board
from syncmill.cli import cli
from syncmill.config import ReportConfig, SyncmillConfig
from syncmill.dashboard import DashboardApp
from syncmill.models import Task, TaskStatus
from syncmill.report import RUN_DIR_SENTINEL, RunReporter, list_reports, load_envelope
from syncmill.tui import SyncmillTuiApp

from conftest import FakeAgent, FakeMemory, FakeRegistry
from test_supervisor import _supervisor, patched_worktree  # noqa: F401  (fixture)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "persisted" / "envelope"
HEAD_SCHEMA = Path(__file__).resolve().parent.parent / "contracts" / "run-envelope.schema.json"
RELEASES = ("v0.1.2", "v0.2.0", "v0.3.0")
SCENARIOS = {
    "completed", "nochange", "failed", "gate-pass", "gate-fail", "compete-team",
    "traced", "phoenix-error", "trace-error", "breaker-degraded", "multi-degraded",
    "trace-only", "board", "killed", "interrupted",
}
#: The one scenario that leaves a run directory without an envelope.
NO_ENVELOPE = "trace-only"


def _index(release: str) -> dict:
    return json.loads((FIXTURES / release / "index.json").read_text())


def _scenarios(release: str) -> list[str]:
    """Every scenario the release froze: SCENARIOS, plus any `completed-N` run
    freeze.py added to make name order differ from created_at order."""
    return sorted(_index(release)["scenarios"])


CASES = [(r, s) for r in RELEASES for s in _scenarios(r)]
ENVELOPE_CASES = [(r, s) for r, s in CASES if s != NO_ENVELOPE]


def _run_id(release: str, scenario: str) -> str:
    return _index(release)["scenarios"][scenario]["run_id"]


def _run_dir(release: str, scenario: str) -> Path:
    return FIXTURES / release / "runs" / _run_id(release, scenario)


def _doc(release: str, scenario: str) -> dict:
    return json.loads((_run_dir(release, scenario) / "envelope.json").read_bytes())


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _copy_runs(root: Path, *releases: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for release in releases:
        for run_dir in (FIXTURES / release / "runs").iterdir():
            shutil.copytree(run_dir, root / run_dir.name)
    return root


@pytest.fixture
def report_root(tmp_path, monkeypatch):
    """Point the CLI's `[integration.report] runs_dir` at a copy of the fixtures."""
    def make(*releases: str) -> Path:
        root = _copy_runs(tmp_path / "reports", *releases)
        monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(root))
        return root
    return make


# ── the fixtures are what the releases wrote ──────────────────────────────


@pytest.mark.parametrize("release", RELEASES)
def test_every_release_froze_every_scenario(release):
    index = _index(release)
    assert index["release"] == release.lstrip("v")
    assert SCENARIOS <= set(index["scenarios"])
    extra = set(index["scenarios"]) - SCENARIOS
    assert all(s.startswith("completed-") for s in extra), extra
    ids = [entry["run_id"] for entry in index["scenarios"].values()]
    assert sorted(ids) == sorted(p.name for p in (FIXTURES / release / "runs").iterdir())
    # Sentinel from v0.1.3 on, for the reporter and the tracer alike.
    for scenario in _scenarios(release):
        has = (_run_dir(release, scenario) / RUN_DIR_SENTINEL).exists()
        assert has is (release != "v0.1.2"), scenario


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_the_fixture_digests_bind_the_stored_sidecars(release, scenario):
    """Fixture integrity, not reader coverage: no HEAD reader checks the sidecars."""
    run_dir = _run_dir(release, scenario)
    if scenario == NO_ENVELOPE:
        assert sorted(p.name for p in run_dir.iterdir() if p.name != RUN_DIR_SENTINEL) == [
            "trace.otlp.json"
        ]
        return
    result = _doc(release, scenario)["result"]
    if scenario == "killed":  # the skeleton `start` writes; finish never ran
        assert "artifact" not in result and "digest" not in result
        assert not (run_dir / "result.json").exists()
        return
    stored = (run_dir / "result.json").read_bytes()
    assert result["artifact"] == "result.json"
    assert result["digest"] == _sha(stored)
    patch = json.loads(stored)["patch"]
    expected_patch = None if patch is None else _sha(patch.encode("utf-8", "surrogateescape"))
    assert result["patch_digest"] == expected_patch
    trace = _doc(release, scenario)["trace"]
    if trace.get("status") == "ok":
        assert trace["artifact"] == "trace.otlp.json"
        assert trace["digest"] == _sha((run_dir / "trace.otlp.json").read_bytes())


# ── the schema ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("release", "scenario"), ENVELOPE_CASES)
def test_head_schema_accepts_the_released_envelope(release, scenario):
    doc = _doc(release, scenario)
    # Formats are checked where jsonschema can: `date` and the like always,
    # `date-time` only with the optional rfc3339-validator installed.
    checker = jsonschema.FormatChecker()
    jsonschema.Draft202012Validator(
        json.loads(HEAD_SCHEMA.read_text()), format_checker=checker
    ).validate(doc)
    # And the release's own schema, frozen beside it: the fixture is what it meant.
    own = json.loads((FIXTURES / release / "run-envelope.schema.json").read_text())
    jsonschema.Draft202012Validator(own, format_checker=checker).validate(doc)


# ── load_envelope / report show / report list ─────────────────────────────


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_load_envelope_returns_the_stored_document(release, scenario, tmp_path):
    raw = None
    if scenario != NO_ENVELOPE:
        raw = json.loads((_run_dir(release, scenario) / "envelope.json").read_bytes())
    root = _copy_runs(tmp_path / "reports", release)
    assert load_envelope(root, _run_id(release, scenario)) == raw


def _v(value: object) -> str:
    return str(value)


def _show_lines(doc: dict) -> list[str]:
    """`report show` for a stored envelope, from its raw values (the fixtures
    hold no control characters, so no escaping is involved)."""
    result, trace = doc["result"], doc["trace"]
    board = doc["board"]
    lines = [
        f"Run:        {doc['run_id']}",
        f"Created:    {doc['created_at']}",
        f"Finished:   {doc['finished_at']}",
        f"Strategy:   {doc['strategy']}",
        f"Team:       {doc['team'] or '—'}",
        f"Task:       {doc['task_id']}",
        f"Agents:     {', '.join(doc['agents']) or '—'}",
        f"Board:      {board['item_id']} (goal: {board['goal']})" if board else "Board:      —",
        f"Result:     {result['status']}"
        + (f" ({result['agent_id']})" if result["agent_id"] else ""),
        f"  winner:             {result['winner'] or '—'}",
        f"  exit code:          {_v(result['exit_code'])}",
        f"  duration:           {_v(result['duration_seconds'])}s",
        f"  gate passed:        {_v(result['gate_passed'])}",
        f"  files changed:      {_v(result['files_changed_count'])}",
        f"  artifact:           {result.get('artifact') or '—'}",
        f"  digest:             {result.get('digest') or '—'}",
        f"  patch digest:       {result['patch_digest'] or '—'}",
        f"Preflight:  {doc['preflight']['status']}",
        f"Trace:      {trace['status']}",
    ]
    if "stream" in trace:
        stream = trace["stream"]
        lines.append(
            f"  Phoenix stream:    {stream['status']} ({stream['exported_span_count']} "
            f"exported, {stream['dropped_span_count']} dropped)"
        )
    lines.append(f"Degraded:   {', '.join(doc['degraded']) or '—'}")
    return lines


#: What each scenario must show, written from the release's own measurements,
#: so an expectation helper that drifted with the renderer cannot pass alone.
SHOWN = {
    "completed": {"Result:": "completed (codex)", "files changed:": "1", "gate passed:": "None"},
    "nochange": {"files changed:": "0",
                 "patch digest:": _sha(b"")},
    "failed": {"Result:": "failed (none)", "winner:": "—", "exit code:": "-1",
               "patch digest:": "—"},
    "gate-pass": {"gate passed:": "True"},
    "gate-fail": {"Result:": "failed (codex)", "gate passed:": "False", "exit code:": "0",
                  "Strategy:": "pipeline"},
    "compete-team": {"Team:": "pair", "Agents:": "codex, kimi-code", "Strategy:": "compete"},
    "traced": {"Trace:": "ok"},
    "phoenix-error": {"Phoenix stream:": "error (0 exported, 2 dropped)", "Degraded:": "phoenix"},
    "trace-error": {"Trace:": "error", "Degraded:": "trace"},
    "breaker-degraded": {"Degraded:": "breaker"},
    "multi-degraded": {"Degraded:": "breaker, phoenix"},
    "board": {"Board:": None},  # checked against the item id below
    "killed": {"Result:": "failed", "exit code:": "None", "duration:": "Nones",
               "artifact:": "—", "digest:": "—"},
    "interrupted": {"Result:": "failed (none)", "exit code:": "1", "duration:": "0.0s"},
}


def _field(stdout: str, label: str) -> str:
    matches = [line for line in stdout.splitlines() if line.strip().startswith(label)]
    assert len(matches) == 1, (label, matches)
    return matches[0].strip()[len(label):].strip()


@pytest.mark.parametrize(("release", "scenario"), ENVELOPE_CASES)
def test_report_show_renders_the_released_envelope(release, scenario, report_root):
    report_root(release)
    run_id = _run_id(release, scenario)
    done = CliRunner().invoke(cli, ["report", "show", run_id])
    assert done.exit_code == 0, done.output
    release_show = _index(release)["scenarios"][scenario]["report_show"]
    assert release_show["returncode"] == 0
    # The release's own `report show` of the same bytes: no field changed meaning.
    assert done.stdout == release_show["stdout"]
    assert done.stdout.splitlines() == _show_lines(_doc(release, scenario))
    shown = SHOWN["completed"] if scenario.startswith("completed-") else SHOWN[scenario]
    for label, value in shown.items():
        if scenario == "board":
            board = _doc(release, scenario)["board"]
            assert board["goal"] == "frozen-goal"
            value = f"{board['item_id']} (goal: frozen-goal)"
        assert _field(done.stdout, label) == value, label


@pytest.mark.parametrize("release", RELEASES)
def test_report_show_refuses_a_run_dir_without_an_envelope(release, report_root):
    root = report_root(release)
    run_id = _run_id(release, NO_ENVELOPE)
    done = CliRunner().invoke(cli, ["report", "show", run_id])
    release_show = _index(release)["scenarios"][NO_ENVELOPE]["report_show"]
    assert (done.exit_code, release_show["returncode"]) == (1, 1)
    assert done.stdout == release_show["stdout"] == ""
    # The message names the configured reports root, which differs per machine.
    assert done.stderr == release_show["stderr"].replace(_index(release)["runs_dir"], str(root))
    assert done.stderr == f"Error: no report found for run {run_id!r} under {root}\n"


def _with_long_run(root: Path) -> list[dict]:
    """Add a test-made long run to `root`; return every report newest first.

    Every released run started and finished before the next began, so
    created_at and finished_at order agree on them. This one (a released
    envelope started before all of them and finished after all of them) tells
    the two orders apart: newest first means by created_at, so it comes last."""
    released = _newest_first(*RELEASES)
    long_run = {**_doc("v0.1.2", "completed"), "run_id": "probe-long-run",
                "created_at": "2026-01-01T00:00:00+00:00",
                "finished_at": "2099-01-01T00:00:00+00:00"}
    assert long_run["created_at"] < min(d["created_at"] for d in released)
    assert long_run["finished_at"] > max(d["finished_at"] for d in released)
    (root / "probe-long-run").mkdir()
    (root / "probe-long-run" / "envelope.json").write_text(json.dumps(long_run))
    return [*released, long_run]


def _newest_first(*releases: str) -> list[dict]:
    docs = [
        _doc(r, s) for r in releases for s in _scenarios(r) if s != NO_ENVELOPE
    ]
    return sorted(docs, key=lambda d: d["created_at"], reverse=True)


def _list_row(doc: dict) -> str:
    r = doc["result"]
    return (f"  {doc['run_id']}  {doc['created_at']}  {doc['strategy']}  "
            f"{r['status']}  {r['agent_id'] or '—'}")


@pytest.mark.parametrize("release", RELEASES)
def test_report_list_matches_the_release_listing(release, report_root):
    report_root(release)
    done = CliRunner().invoke(cli, ["report", "list"])
    assert done.exit_code == 0, done.output
    assert done.stdout == _index(release)["report_list"]
    rows = [line for line in done.stdout.splitlines() if line.startswith("  ")]
    assert rows == [_list_row(d) for d in _newest_first(release)]
    # Name order is not the answer: a listing that skipped the sort would differ.
    names = sorted(d["run_id"] for d in _newest_first(release))
    assert [d["run_id"] for d in _newest_first(release)] not in (names, names[::-1])


def test_report_list_orders_every_release_and_skips_foreign_entries(report_root):
    root = report_root(*RELEASES)
    (root / "not-a-run").mkdir()
    (root / "not-a-run" / "notes.txt").write_text("someone else's\n")
    (root / "torn").mkdir()
    (root / "torn" / "envelope.json").write_text('{"run_id": "torn"')
    (root / "stray.json").write_text("{}")
    expected = _with_long_run(root)
    done = CliRunner().invoke(cli, ["report", "list"])
    assert done.exit_code == 0, done.output
    rows = [line for line in done.stdout.splitlines() if line.startswith("  ")]
    assert rows == [_list_row(d) for d in expected]
    assert [d["run_id"] for d in list_reports(root)] == [d["run_id"] for d in expected]


# ── the TUI Reports tab and the dashboard REPORT pane ─────────────────────


def _shown(screen, selector: str) -> str:
    """What the widget displays (not the app's cache of what it last sent)."""
    return str(screen.query_one(selector).render())


async def _settle(pilot, ready, *, tries: int = 200) -> None:
    for _ in range(tries):
        await pilot.pause(0)
        if ready():
            return
    raise AssertionError("the UI never reached the expected state")


def _prompts(options: OptionList) -> list[str]:
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


def _release_show(doc: dict) -> str:
    """The release's own `report show` text; for the test-made long run, its
    fields from the raw values."""
    for release in RELEASES:
        for scenario, entry in _index(release)["scenarios"].items():
            if entry["run_id"] == doc["run_id"]:
                return entry["report_show"]["stdout"].rstrip("\n")
    assert doc["run_id"] == "probe-long-run"
    return "\n".join(_show_lines(doc))


def test_tui_reports_tab_shows_every_released_run(report_root, tmp_path):
    root = report_root(*RELEASES)
    expected = _with_long_run(root)

    async def scenario():
        app = SyncmillTuiApp(
            SyncmillConfig(board={"enabled": False},
                           tui={"state_dir": str(tmp_path / "jobs")},
                           integration={"report": {"runs_dir": str(root)}}),
            board=None, board_error=None, toml_path=tmp_path / "syncmill.toml",
            refresh_seconds=3600, initial_tab="reports",
        )
        async with app.run_test() as pilot:
            options = app.query_one("#report-list", OptionList)
            await _settle(pilot, lambda: options.option_count == len(expected))
            assert _prompts(options) == [
                f"{d['run_id']} {d['strategy']} {d['result']['status']}" for d in expected
            ]
            for row, doc in enumerate(expected):
                options.highlighted = row
                want = _release_show(doc)
                await _settle(pilot, lambda: _shown(app, "#report-detail") == want)

    asyncio.run(scenario())


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def test_dashboard_report_pane_shows_every_released_run(report_root, tmp_path):
    root = report_root(*RELEASES)
    expected = _with_long_run(root)
    board = Board(str(tmp_path / "board.db"), now=_Clock())

    async def scenario():
        app = DashboardApp(
            board, refresh_seconds=3600, now=_Clock(),
            config=SyncmillConfig(integration={"report": {"runs_dir": str(root)}}),
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await _settle(pilot, lambda: app.screen.query("#report-list"))
            options = app.screen.query_one("#report-list", OptionList)
            await _settle(pilot, lambda: options.option_count == len(expected))
            assert _prompts(options) == [
                f"{d['run_id']}  {d['created_at']}  {d['strategy']}  "
                f"{d['result']['status']}  {d['result']['agent_id'] or '—'}"
                for d in expected
            ]
            for row, doc in enumerate(expected):
                options.highlighted = row
                want = _release_show(doc)
                await _settle(pilot, lambda: _shown(app.screen, "#report-detail") == want)

    try:
        asyncio.run(scenario())
    finally:
        board.close()


# ── retention ownership over released run dirs ─────────────────────────────


CURRENT = "current-run"
KEEP_LAST = 10
MARKER = "kept-by-the-current-run"
MARKER_BYTES = b"written before the sweep\n"


def _variant(root: Path, name: str, doc: object) -> None:
    """A test-made directory holding a changed copy of a released envelope."""
    (root / name).mkdir()
    (root / name / "envelope.json").write_text(json.dumps(doc))


def _retention_root(root: Path) -> tuple[set[str], set[str]]:
    """Seed `root` with every released run dir plus labelled ownership probes.
    Returns (owned, not_owned) directory names. Each probe breaks ONE predicate
    of `_is_run_dir`; the owned probe shows the probe-making keeps ownership."""
    _copy_runs(root, *RELEASES)
    owned = {
        _run_id(r, s) for r in RELEASES for s in _scenarios(r)
        if not (r == "v0.1.2" and s == NO_ENVELOPE)
    }
    # v0.1.2 wrote no sentinel and a trace-only run writes no envelope: nothing
    # proves the directory is syncmill's, so HEAD never removes it.
    not_owned = {_run_id("v0.1.2", NO_ENVELOPE)}

    released = _doc("v0.1.2", "completed")
    _variant(root, "probe-owned", {**released, "run_id": "probe-owned"})
    owned.add("probe-owned")
    _variant(root, "probe-renamed", released)  # run_id names another directory
    _variant(root, "probe-kind-changed",
             {**released, "run_id": "probe-kind-changed", "kind": "someone.else"})
    _variant(root, "probe-kind-removed",
             {k: v for k, v in {**released, "run_id": "probe-kind-removed"}.items() if k != "kind"})
    _variant(root, "probe-not-object", [released])
    (root / "probe-torn").mkdir()
    (root / "probe-torn" / "envelope.json").write_text(
        json.dumps({**released, "run_id": "probe-torn"})[:-1]
    )
    (root / "probe-foreign").mkdir()
    shutil.copyfile(_run_dir("v0.1.2", "completed") / "result.json",
                    root / "probe-foreign" / "result.json")
    not_owned |= {"probe-renamed", "probe-kind-changed", "probe-kind-removed",
                  "probe-not-object", "probe-torn", "probe-foreign"}

    # The current run exists already and is the OLDEST: it must survive anyway.
    # It holds a marker, so a sweep that removed it and a writer that then
    # recreated it cannot pass for one that left it alone.
    (root / CURRENT).mkdir()
    (root / CURRENT / RUN_DIR_SENTINEL).write_text("")
    (root / CURRENT / MARKER).write_bytes(MARKER_BYTES)
    # Distinct, explicit mtimes in an order unrelated to names or releases, so
    # owned and foreign directories interleave around the retention cutoff.
    names = sorted(p.name for p in root.iterdir() if p.name != CURRENT)
    names.sort(key=lambda n: hashlib.sha256(n.encode()).hexdigest())
    base = 1_700_000_000
    os.utime(root / CURRENT, (base, base))
    for i, name in enumerate(names, start=1):
        os.utime(root / name, (base + i, base + i))
    assert set(names) == owned | not_owned
    return owned, not_owned


def _survivors(root: Path, owned: set[str], not_owned: set[str]) -> set[str]:
    newest_owned = sorted(owned, key=lambda n: (root / n).stat().st_mtime, reverse=True)
    kept = set(newest_owned[: KEEP_LAST - 1])
    # The cutoff must fall inside the owned set, with foreign dirs on both sides.
    assert len(owned) > KEEP_LAST
    order = sorted(owned | not_owned, key=lambda n: (root / n).stat().st_mtime)
    first_kept = min(order.index(n) for n in kept)
    assert any(order.index(n) > first_kept for n in not_owned)
    assert any(order.index(n) < first_kept for n in not_owned)
    return kept | not_owned | {CURRENT}


def test_run_reporter_retention_owns_released_dirs_only(tmp_path):
    root = tmp_path / "reports"
    owned, not_owned = _retention_root(root)
    expected = _survivors(root, owned, not_owned)
    reporter = RunReporter(ReportConfig(enabled=True, runs_dir=str(root), keep_last=KEEP_LAST))
    reporter.start(run_id=CURRENT, task_id="t", strategy="route", team=None, agents=["codex"])
    assert not reporter.degraded
    assert {p.name for p in root.iterdir()} == expected
    assert (root / CURRENT / MARKER).read_bytes() == MARKER_BYTES


def _trace_only(tmp_path, root: Path, trace_dir: str):
    config = SyncmillConfig(integration={
        "report": {"enabled": False, "runs_dir": str(root), "keep_last": KEEP_LAST},
        "trace": {"enabled": True, "dir": trace_dir},
    })
    return _supervisor(FakeRegistry([FakeAgent("claude")]), FakeMemory(), config=config)


def test_supervisor_trace_only_sweep_owns_released_dirs_only(patched_worktree, tmp_path):  # noqa: F811
    root = tmp_path / "reports"
    owned, not_owned = _retention_root(root)
    expected = _survivors(root, owned, not_owned)
    result = _trace_only(tmp_path, root, "").run(Task(prompt="go"), run_id=CURRENT)
    assert result.status is TaskStatus.COMPLETED
    assert {p.name for p in root.iterdir()} == expected
    assert (root / CURRENT / "trace.otlp.json").exists()
    assert (root / CURRENT / MARKER).read_bytes() == MARKER_BYTES


def test_supervisor_never_sweeps_a_custom_trace_dir(patched_worktree, tmp_path):  # noqa: F811
    # Both settings name the seeded root: a sweep that ignored the custom
    # trace.dir would prune report.runs_dir, which is this same directory.
    root = tmp_path / "reports"
    _retention_root(root)
    before = {p.name for p in root.iterdir()}
    result = _trace_only(tmp_path, root, str(root)).run(Task(prompt="go"), run_id=CURRENT)
    assert result.status is TaskStatus.COMPLETED
    assert {p.name for p in root.iterdir()} == before
    assert (root / CURRENT / "trace.otlp.json").exists()
    assert (root / CURRENT / MARKER).read_bytes() == MARKER_BYTES
