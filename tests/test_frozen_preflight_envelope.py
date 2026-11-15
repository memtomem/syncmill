"""Released run-report directories with a preflight block, read by HEAD (#271).

The second part of the Run envelope family. `tests/fixtures/persisted/envelope/
<release>/preflight/` is its own reports root: run directories that v0.1.2,
v0.2.0 and v0.3.0 wrote through their own `syncmill run` with report recording
and toolgraph preflight on, against a fake `toolgraph` that answers with the
release's vendored goldens (see `freeze.py --part preflight`). `index.json`
there holds the release's own `report list` / `report show` output.

HEAD reads these bytes in few places: the schema (most of the value here — the
`ok` branch, the strict `allOf`, `graphState` / `controlPlanArtifact`, and the
no-artifact error shapes), `load_envelope`, the one `Preflight:` line of
`report show` (also behind the TUI Reports tab and the dashboard REPORT pane),
`report list` membership, and retention, which must remove a run dir whole,
nested `preflight/` directory included. No HEAD code reads `preflight.json`,
`preflight/eval-*.json` or the control files back; the checks on them are
fixture integrity and are labelled so.

Expected values come from the raw JSON, the release's own output, or the
measurements written below — never from `render_report`, `_is_run_dir` or
`run_preflight`.
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
from syncmill.report import RUN_DIR_SENTINEL, RunReporter, load_envelope
from syncmill.tui import SyncmillTuiApp

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures" / "persisted" / "envelope"
HEAD_SCHEMA = REPO / "contracts" / "run-envelope.schema.json"
HEAD_GOLDENS = REPO / "tests" / "fixtures" / "toolgraph"
#: Every release frozen here, from the directories on disk.
RELEASES = tuple(sorted(p.parent.parent.name for p in FIXTURES.glob("v*/preflight/index.json")))

ANY = object()  # present; what it binds to is checked separately


def _block(status: str, mode: str, **fields: object) -> dict:
    return {"status": status, "mode": mode, "profile": "review", **fields}


def _ok(**fields: object) -> dict:
    return _block("ok", "advisory", graph_generation=42, artifact="preflight.json",
                  digest=ANY, **fields)


def _strict(status: str, decision: str, evaluations: int, blocks: int, warnings: int,
            **fields: object) -> dict:
    return _block(status, "strict", enforcement_decision=decision,
                  evaluation_count=evaluations, block_count=blocks,
                  warning_count=warnings, **fields)


def _aggregate(**fields: object) -> dict:
    return {"artifact": "preflight.json", "digest": ANY, **fields}


GRAPH_STATE = {"generation": 42, "instance_id": "test-instance"}


def _control(mode: str, producer: str, warnings: int, *, persisted: bool = True,
             **fields: object) -> dict:
    block = _block("ok" if persisted else "error", mode, graph_generation=42,
                   graph_state=GRAPH_STATE, warning_count=warnings, evaluation_count=1,
                   producer_decision=producer, **fields)
    if persisted:
        block.update(artifact="control-preflight.json", digest=ANY,
                     control_plan={"artifact": "control-plan.json", "digest": ANY})
    return block


EVAL0, EVAL1 = "preflight/eval-000.json", "preflight/eval-001.json"
CONTROL_FILES = {"control-plan.json", "control-preflight.json"}


def _expected(release: str) -> dict[str, tuple[dict, set[str], bool]]:
    """scenario -> (envelope preflight block, preflight files in the run dir,
    blocked), as measured on the release CLIs (#271 part 2 plan). A blocked run
    is FAILED with agent `preflight` and exit code 1."""
    v012 = release == "v0.1.2"
    rows = {
        "advisory-allow": (_ok(warning_count=0), {"preflight.json"}, False),
        "advisory-drift": (_ok(warning_count=1), {"preflight.json"}, False),
        "advisory-unresolved": (
            _block("unresolved_identity", "advisory", graph_generation=42, warning_count=0,
                   artifact="preflight.json", digest=ANY), {"preflight.json"}, False),
        "advisory-unmapped": (_block("unresolved_identity", "advisory"), set(), False),
        "advisory-error": (_block("error", "advisory"), set(), False),
        "advisory-timeout": (_block("timeout", "advisory"), set(), False),
        "strict-allow": (_strict("ok", "allow", 1, 0, 0, graph_generation=42, **_aggregate()),
                         {"preflight.json", EVAL0}, False),
        "strict-warn": (_strict("ok", "allow", 1, 0, 1, graph_generation=42, **_aggregate()),
                        {"preflight.json", EVAL0}, False),
        "strict-block": (_strict("ok", "block", 1, 1, 0, graph_generation=42, **_aggregate()),
                         {"preflight.json", EVAL0}, True),
        "strict-timeout": (_strict("timeout", "block", 1, 1, 0, **_aggregate()),
                           {"preflight.json"}, True),
        "strict-unresolved": (
            _strict("unresolved_identity", "block", 1, 1, 0, graph_generation=42,
                    **_aggregate()), {"preflight.json", EVAL0}, True),
        "strict-unavailable": (_strict("error", "block", 1, 1, 0, **_aggregate()),
                               {"preflight.json"}, True),
        "strict-unavailable-warn": (_strict("error", "allow", 1, 0, 1, **_aggregate()),
                                    {"preflight.json"}, False),
        "strict-multi-allow": (
            _strict("ok", "allow", 2, 0, 0, graph_generation=42, **_aggregate()),
            {"preflight.json", EVAL0, EVAL1}, False),
        "strict-multi-mixed": (
            _strict("error", "block", 2, 1, 0, graph_generation=42, **_aggregate()),
            {"preflight.json", EVAL0}, True),
        "strict-gen-mismatch": (_strict("error", "block", 2, 1, 0, **_aggregate()),
                                {"preflight.json", EVAL0, EVAL1}, True),
        "strict-unmapped": (_strict("unresolved_identity", "block", 1, 1, 0, **_aggregate()),
                            {"preflight.json"}, True),
        # v0.1.2 wrote eval files first and the aggregate last, cleaning up
        # nothing; v0.2.0+ strips references to eval files it could not write
        # and removes what it wrote when the aggregate fails.
        "strict-persist-eval": (
            _strict("error", "block", 1, 1, 0, graph_generation=42) if v012 else
            _strict("error", "block", 1, 1, 0, graph_generation=42, **_aggregate()),
            set() if v012 else {"preflight.json"}, True),
        "strict-persist-aggregate": (
            _strict("error", "block", 1, 1, 0, graph_generation=42),
            {EVAL0} if v012 else set(), True),
        "killed-after-preflight": ({"status": "disabled"}, {"preflight.json", EVAL0}, False),
    }
    if not v012:  # control-plan preflight exists from v0.2.0
        rows |= {
            "control-allow": (_control("advisory", "advisory_allow", 0), CONTROL_FILES, False),
            "control-strict-allow": (
                _control("strict", "advisory_allow", 0, enforcement_decision="allow",
                         block_count=0), CONTROL_FILES, False),
            "control-finding": (_control("advisory", "advisory_warn", 2), CONTROL_FILES, False),
            "control-strict-finding": (
                _control("strict", "advisory_warn", 1, enforcement_decision="block",
                         block_count=1), CONTROL_FILES, True),
            "control-error": (_block("error", "advisory"), set(), False),
            "control-strict-error": (_strict("error", "block", 0, 1, 0), set(), True),
            "control-strict-error-warn": (_strict("error", "allow", 0, 0, 1), set(), False),
            "control-strict-timeout": (_strict("timeout", "block", 0, 1, 0), set(), True),
            "control-persist": (_control("advisory", "advisory_allow", 0, persisted=False),
                                set(), False),
            "control-strict-persist": (
                _control("strict", "advisory_allow", 0, persisted=False,
                         enforcement_decision="block", block_count=1), set(), True),
        }
    return rows


#: Producer replies that are not a golden's bytes (freeze.py says why).
SYNTHETIC = {"strict-multi-allow", "strict-multi-mixed", "strict-gen-mismatch",
             "control-finding", "control-strict-finding"}
#: Scenarios reached through a write the release's own writer was made to fail.
INJECTED = {"strict-persist-eval": "eval-000.json", "strict-persist-aggregate": "preflight.json",
            "control-persist": "control-preflight.json",
            "control-strict-persist": "control-preflight.json"}


def _index(release: str) -> dict:
    return json.loads((FIXTURES / release / "preflight" / "index.json").read_text())


def _scenarios(release: str) -> list[str]:
    return sorted(_index(release)["scenarios"])


CASES = [(r, s) for r in RELEASES for s in _scenarios(r)]


def _base(scenario: str) -> str:
    """`advisory-allow-N` runs exist only to tell created_at from name order."""
    head, _, tail = scenario.rpartition("-")
    return head if head == "advisory-allow" and tail.isdigit() else scenario


def _run_id(release: str, scenario: str) -> str:
    return _index(release)["scenarios"][scenario]["run_id"]


def _run_dir(release: str, scenario: str) -> Path:
    return FIXTURES / release / "preflight" / "runs" / _run_id(release, scenario)


def _doc(release: str, scenario: str) -> dict:
    return json.loads((_run_dir(release, scenario) / "envelope.json").read_bytes())


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git_blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _matches(got: object, want: object) -> bool:
    if want is ANY:
        return True
    if isinstance(want, dict):
        return (isinstance(got, dict) and set(got) == set(want)
                and all(_matches(got[k], v) for k, v in want.items()))
    return got == want


def _preflight_files(run_dir: Path) -> set[str]:
    owned = {"envelope.json", "result.json", RUN_DIR_SENTINEL}
    return {str(p.relative_to(run_dir)) for p in run_dir.rglob("*")
            if p.is_file() and str(p.relative_to(run_dir)) not in owned}


def _copy_runs(root: Path, *releases: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for release in releases:
        for run_dir in (FIXTURES / release / "preflight" / "runs").iterdir():
            shutil.copytree(run_dir, root / run_dir.name)
    return root


@pytest.fixture
def report_root(tmp_path, monkeypatch):
    def make(*releases: str) -> Path:
        root = _copy_runs(tmp_path / "reports", *releases)
        monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(root))
        return root
    return make


# ── the fixtures are what the releases wrote ──────────────────────────────


def test_the_frozen_releases_are_all_present():
    assert {"v0.1.2", "v0.2.0", "v0.3.0"} <= set(RELEASES)


@pytest.mark.parametrize("release", RELEASES)
def test_every_release_froze_every_preflight_scenario(release):
    index = _index(release)
    assert index["release"] == release.lstrip("v")
    names = set(index["scenarios"])
    assert set(_expected(release)) <= names
    assert {_base(n) for n in names} == set(_expected(release))
    ids = [entry["run_id"] for entry in index["scenarios"].values()]
    assert sorted(ids) == sorted(p.name for p in (FIXTURES / release / "preflight" / "runs").iterdir())
    for scenario in names:
        has = (_run_dir(release, scenario) / RUN_DIR_SENTINEL).exists()
        assert has is (release != "v0.1.2"), scenario
    # The fake read the release's goldens, which are HEAD's vendored bytes.
    goldens = index["goldens"]
    control = {"control-preflight-v1.json", "control-preflight.schema.json"}
    assert (control <= set(goldens)) is (release != "v0.1.2")
    for name, blob in goldens.items():
        assert _git_blob((HEAD_GOLDENS / name).read_bytes()) == blob, name
    # Which replies are synthetic and which writes were made to fail, by name.
    present = set(_expected(release))
    assert {n for n, e in index["scenarios"].items() if "synthetic" in e} == SYNTHETIC & present
    injected = {n: e["injected_write_failure"]["destination"]
                for n, e in index["scenarios"].items() if "injected_write_failure" in e}
    assert injected == {n: d for n, d in INJECTED.items() if n in present}


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_the_preflight_block_and_files_are_what_the_release_wrote(release, scenario):
    """Fixture integrity, not reader coverage: no HEAD reader opens these files.
    The exact key set keeps a fixture that lost a field from passing the schema
    test by omission."""
    block, files, blocked = _expected(release)[_base(scenario)]
    doc = _doc(release, scenario)
    preflight = doc["preflight"]
    assert _matches(preflight, block), preflight
    run_dir = _run_dir(release, scenario)
    assert _preflight_files(run_dir) == files
    result = doc["result"]
    if blocked:
        assert (result["status"], result["agent_id"], result["exit_code"]) == (
            "failed", "preflight", 1)
        assert result["duration_seconds"] == 0.0
    elif scenario == "killed-after-preflight":
        assert (result["status"], result["agent_id"]) == ("failed", None)
    else:
        assert (result["status"], result["agent_id"]) == ("completed", "codex")
    if "artifact" in preflight:
        assert preflight["digest"] == _sha((run_dir / preflight["artifact"]).read_bytes())
    if "control_plan" in preflight:
        plan = (run_dir / "control-plan.json").read_bytes()
        assert preflight["control_plan"]["digest"] == _sha(plan)
        stored = json.loads((run_dir / "control-preflight.json").read_bytes())
        assert stored["plan_digest"] == _sha(plan)
    aggregate = run_dir / "preflight.json"
    if preflight.get("mode") == "strict" and aggregate.exists():
        manifest = json.loads(aggregate.read_bytes())
        assert manifest["kind"] == "syncmill.toolgraph-enforcement"
        assert manifest["decision"] == preflight["enforcement_decision"]
        assert len(manifest["evaluations"]) == preflight["evaluation_count"]
        referenced = set()
        for row in manifest["evaluations"]:
            assert ("artifact" in row) is ("digest" in row), row
            if "artifact" in row:
                referenced.add(row["artifact"])
                assert row["digest"] == _sha((run_dir / row["artifact"]).read_bytes())
        # Every eval file on disk is referenced, and nothing referenced is absent.
        assert referenced == {f for f in files if f.startswith("preflight/")}


def test_the_released_writers_disagree_on_a_failed_write():
    """The two persistence-failure shapes are why both writers are frozen."""
    old = _expected("v0.1.2")
    new = _expected("v0.3.0")
    for scenario in ("strict-persist-eval", "strict-persist-aggregate"):
        assert old[scenario][1] != new[scenario][1]
        for release in RELEASES:
            assert _preflight_files(_run_dir(release, scenario)) == _expected(release)[scenario][1]


# ── the schema ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_head_schema_accepts_the_released_preflight_envelope(release, scenario):
    doc = _doc(release, scenario)
    checker = jsonschema.FormatChecker()
    jsonschema.Draft202012Validator(
        json.loads(HEAD_SCHEMA.read_text()), format_checker=checker
    ).validate(doc)
    # The release's own schema, frozen by part 1: the fixture is what it meant.
    own = json.loads((FIXTURES / release / "run-envelope.schema.json").read_text())
    jsonschema.Draft202012Validator(own, format_checker=checker).validate(doc)


# ── load_envelope / report show / report list ─────────────────────────────


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_load_envelope_returns_the_stored_document(release, scenario, tmp_path):
    raw = json.loads((_run_dir(release, scenario) / "envelope.json").read_bytes())
    root = _copy_runs(tmp_path / "reports", release)
    assert load_envelope(root, _run_id(release, scenario)) == raw


def _show_lines(doc: dict) -> list[str]:
    """`report show` from the raw values (no control characters in the fixtures)."""
    result, trace = doc["result"], doc["trace"]
    board = doc["board"]
    return [
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
        f"  exit code:          {result['exit_code']}",
        f"  duration:           {result['duration_seconds']}s",
        f"  gate passed:        {result['gate_passed']}",
        f"  files changed:      {result['files_changed_count']}",
        f"  artifact:           {result.get('artifact') or '—'}",
        f"  digest:             {result.get('digest') or '—'}",
        f"  patch digest:       {result['patch_digest'] or '—'}",
        f"Preflight:  {doc['preflight']['status']}",
        f"Trace:      {trace['status']}",
        f"Degraded:   {', '.join(doc['degraded']) or '—'}",
    ]


def _field(stdout: str, label: str) -> str:
    matches = [line for line in stdout.splitlines() if line.strip().startswith(label)]
    assert len(matches) == 1, (label, matches)
    return matches[0].strip()[len(label):].strip()


@pytest.mark.parametrize(("release", "scenario"), CASES)
def test_report_show_renders_the_released_preflight_envelope(release, scenario, report_root):
    report_root(release)
    done = CliRunner().invoke(cli, ["report", "show", _run_id(release, scenario)])
    assert done.exit_code == 0, done.output
    release_show = _index(release)["scenarios"][scenario]["report_show"]
    assert release_show["returncode"] == 0
    assert done.stdout == release_show["stdout"]
    assert done.stdout.splitlines() == _show_lines(_doc(release, scenario))
    block, _, blocked = _expected(release)[_base(scenario)]
    assert _field(done.stdout, "Preflight:") == block["status"]
    if blocked:
        assert _field(done.stdout, "Result:") == "failed (preflight)"
        assert _field(done.stdout, "exit code:") == "1"
        assert _field(done.stdout, "duration:") == "0.0s"
    # Observed, not a contract: a strict run that enforcement blocked still shows
    # the producer's `ok`; only the Result line tells it was blocked.
    if block.get("enforcement_decision") == "block" and block["status"] == "ok":
        assert _field(done.stdout, "Preflight:") == "ok"


def _newest_first(*releases: str) -> list[dict]:
    docs = [_doc(r, s) for r in releases for s in _scenarios(r)]
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
    names = sorted(d["run_id"] for d in _newest_first(release))
    assert [d["run_id"] for d in _newest_first(release)] not in (names, names[::-1])


# ── the TUI Reports tab and the dashboard REPORT pane ─────────────────────


def _shown(screen, selector: str) -> str:
    return str(screen.query_one(selector).render())


async def _settle(pilot, ready, *, tries: int = 200) -> None:
    for _ in range(tries):
        await pilot.pause(0)
        if ready():
            return
    raise AssertionError("the UI never reached the expected state")


def _prompts(options: OptionList) -> list[str]:
    return [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]


#: Shown in each UI: the plainest block run and the two blocked shapes, one of
#: them with a producer `ok`. Every scenario's text is D's; the UIs reuse it.
UI_RELEASE = "v0.3.0"
UI_SCENARIOS = ("strict-block", "control-strict-timeout", "advisory-unmapped")


def _ui_expectation() -> tuple[list[dict], dict[str, str]]:
    docs = _newest_first(UI_RELEASE)
    shown = {
        _run_id(UI_RELEASE, s): _index(UI_RELEASE)["scenarios"][s]["report_show"]["stdout"]
        .rstrip("\n")
        for s in UI_SCENARIOS
    }
    for run_id, text in shown.items():
        assert text.splitlines() == _show_lines(next(d for d in docs if d["run_id"] == run_id))
    return docs, shown


def test_tui_reports_tab_shows_released_preflight_runs(report_root, tmp_path):
    root = report_root(UI_RELEASE)
    docs, shown = _ui_expectation()

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
            await _settle(pilot, lambda: options.option_count == len(docs))
            assert _prompts(options) == [
                f"{d['run_id']} {d['strategy']} {d['result']['status']}" for d in docs
            ]
            for run_id, want in shown.items():
                options.highlighted = [d["run_id"] for d in docs].index(run_id)
                await _settle(pilot, lambda: _shown(app, "#report-detail") == want)

    asyncio.run(scenario())


class _Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


def test_dashboard_report_pane_shows_released_preflight_runs(report_root, tmp_path):
    root = report_root(UI_RELEASE)
    docs, shown = _ui_expectation()
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
            await _settle(pilot, lambda: options.option_count == len(docs))
            assert _prompts(options) == [
                f"{d['run_id']}  {d['created_at']}  {d['strategy']}  "
                f"{d['result']['status']}  {d['result']['agent_id'] or '—'}"
                for d in docs
            ]
            for run_id, want in shown.items():
                options.highlighted = [d["run_id"] for d in docs].index(run_id)
                await _settle(pilot, lambda: _shown(app.screen, "#report-detail") == want)

    try:
        asyncio.run(scenario())
    finally:
        board.close()


# ── retention removes a released preflight run dir whole ──────────────────


CURRENT = "current-run"
KEEP_LAST = 10
#: Forced oldest (swept) and newest (kept): each holds a nonempty `preflight/`.
#: v0.1.2's is owned only through the envelope fallback (no sentinel).
SWEPT_NESTED = (("v0.1.2", "strict-persist-aggregate"), ("v0.3.0", "strict-allow"))
KEPT_NESTED = ("v0.2.0", "strict-multi-allow")


def test_run_reporter_retention_removes_released_preflight_dirs_whole(tmp_path):
    root = _copy_runs(tmp_path / "reports", *RELEASES)
    (root / CURRENT).mkdir()
    (root / CURRENT / RUN_DIR_SENTINEL).write_text("")
    swept = {_run_id(r, s) for r, s in SWEPT_NESTED}
    kept_nested = _run_id(*KEPT_NESTED)
    for name in (*swept, kept_nested):
        assert any((root / name / "preflight").iterdir()), name
    others = sorted(
        (p.name for p in root.iterdir() if p.name not in {*swept, kept_nested, CURRENT}),
        key=lambda n: hashlib.sha256(n.encode()).hexdigest(),
    )
    order = [*sorted(swept), *others, kept_nested]  # oldest first
    base = 1_700_000_000
    os.utime(root / CURRENT, (base, base))  # the oldest, and kept anyway
    for i, name in enumerate(order, start=1):
        os.utime(root / name, (base + i, base + i))
    # Every released dir here is owned (sentinel, or a v0.1.2 envelope naming
    # its own directory), so the newest KEEP_LAST - 1 survive beside the current run.
    assert len(order) > KEEP_LAST
    expected = {*order[-(KEEP_LAST - 1):], CURRENT}

    reporter = RunReporter(ReportConfig(enabled=True, runs_dir=str(root), keep_last=KEEP_LAST))
    reporter.start(run_id=CURRENT, task_id="t", strategy="route", team=None, agents=["codex"])
    assert not reporter.degraded
    assert {p.name for p in root.iterdir()} == expected
    assert not swept & expected
    release, scenario = KEPT_NESTED
    source = _run_dir(release, scenario)
    for path in sorted(source.rglob("*")):
        if path.is_file():
            relative = path.relative_to(source)
            assert (root / kept_nested / relative).read_bytes() == path.read_bytes()
