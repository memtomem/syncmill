"""resolve_run_id (S2) + the run-envelope writer (S3).

Resolver precedence: explicit > monitor-inner env (sentinel-gated) > fresh
uuid4. All hermetic: the "inner" state is just the env var + sentinel file,
exactly as `test_monitor.py` drives `inner_dir`.

Writer tests validate every generated envelope against the REAL
``contracts/run-envelope.schema.json`` and the redaction ``_scan`` (reused from
``test_contracts``), so the producer can never drift from the P0 contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from uuid import UUID, uuid4

import pytest
from test_contracts import CONTRACTS, _scan, _validator

import syncmill.monitor as M
import syncmill.report as R
from syncmill.config import ReportConfig
from syncmill.models import GateCheck, GateReport, TaskResult, TaskStatus
from syncmill.monitor import emit_patch_file
from syncmill.report import (
    RunReporter,
    build_reporter,
    list_reports,
    load_envelope,
    patch_envelope_board,
    render_report,
    resolve_run_id,
    sha256_digest,
    sweep_runs_dir,
    write_envelope,
)

ENVELOPE_SCHEMA = CONTRACTS / "run-envelope.schema.json"


def _make_inner(monkeypatch, tmp_path):
    """Make this process look like the in-tmux INNER: env var + sentinel file."""
    monkeypatch.setenv(M.INNER_ENV, str(tmp_path))
    (tmp_path / M.SENTINEL_NAME).write_text("")


def test_direct_mints_fresh_uuid(monkeypatch):
    monkeypatch.delenv(M.INNER_ENV, raising=False)
    monkeypatch.delenv(M.RUN_ID_ENV, raising=False)
    first = resolve_run_id()
    UUID(first)  # a real uuid4 string
    assert resolve_run_id() != first  # fresh per call


def test_explicit_wins_over_inner_env(monkeypatch, tmp_path):
    _make_inner(monkeypatch, tmp_path)
    monkeypatch.setenv(M.RUN_ID_ENV, "from-env")
    assert resolve_run_id("given") == "given"


def test_empty_explicit_counts_as_unset(monkeypatch):
    # The `-r ""` house rule: an empty string is "not provided", never an id.
    monkeypatch.delenv(M.INNER_ENV, raising=False)
    monkeypatch.delenv(M.RUN_ID_ENV, raising=False)
    UUID(resolve_run_id(""))


def test_inner_env_honored_with_sentinel(monkeypatch, tmp_path):
    _make_inner(monkeypatch, tmp_path)
    monkeypatch.setenv(M.RUN_ID_ENV, "outer-minted")
    assert resolve_run_id() == "outer-minted"


def test_run_id_env_ignored_without_inner_env(monkeypatch):
    # A stray exported SYNCMILL_RUN_ID alone must never leak into a normal run.
    monkeypatch.delenv(M.INNER_ENV, raising=False)
    monkeypatch.setenv(M.RUN_ID_ENV, "stray")
    assert resolve_run_id() != "stray"


def test_run_id_env_ignored_without_sentinel(monkeypatch, tmp_path):
    # Env var without the sentinel file → not inner (inner_dir's own gate).
    monkeypatch.setenv(M.INNER_ENV, str(tmp_path))  # no sentinel written
    monkeypatch.setenv(M.RUN_ID_ENV, "stray")
    assert resolve_run_id() != "stray"


def test_inner_without_run_id_env_falls_back_fresh(monkeypatch, tmp_path):
    # An older outer that didn't put SYNCMILL_RUN_ID on the snapshot → fresh id,
    # never a failure.
    _make_inner(monkeypatch, tmp_path)
    monkeypatch.delenv(M.RUN_ID_ENV, raising=False)
    UUID(resolve_run_id())


# ── Envelope writer (S3) ─────────────────────────────────────────────────────


def _report_config(tmp_path, **overrides) -> ReportConfig:
    kwargs = {"enabled": True, "runs_dir": str(tmp_path / "runs")}
    kwargs.update(overrides)
    return ReportConfig(**kwargs)


def _completed_result(**overrides) -> TaskResult:
    kwargs = dict(
        task_id=uuid4(),
        agent_id="claude",
        status=TaskStatus.COMPLETED,
        output="did the thing",
        exit_code=0,
        duration_seconds=1.5,
        files_changed=["a.py", "b.py"],
        gate=GateReport.from_checks([GateCheck(command="true", passed=True, exit_code=0)]),
        patch="diff --git a/a.py b/a.py\n",
    )
    kwargs.update(overrides)
    return TaskResult(**kwargs)


def _started_reporter(tmp_path, **meta) -> RunReporter:
    reporter = build_reporter(_report_config(tmp_path))
    kwargs = dict(
        run_id="run-1",
        task_id="task-1",
        strategy="route",
        team=None,
        agents=["claude", "codex"],
    )
    kwargs.update(meta)
    reporter.start(**kwargs)
    return reporter


def _envelope(tmp_path, run_id="run-1") -> dict:
    return json.loads(
        (tmp_path / "runs" / run_id / R.ENVELOPE_FILE).read_text()
    )


def test_build_reporter_disabled_returns_none():
    assert build_reporter(ReportConfig(enabled=False)) is None


def test_sha256_digest_known_vector():
    empty = hashlib.sha256(b"").hexdigest()
    assert sha256_digest(b"") == f"sha256:{empty}"
    assert sha256_digest(b"x").startswith("sha256:")
    assert len(sha256_digest(b"x")) == len("sha256:") + 64


def test_write_envelope_atomic_no_temp_litter(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_envelope(run_dir, {"v": 1})
    write_envelope(run_dir, {"v": 2})
    assert json.loads((run_dir / R.ENVELOPE_FILE).read_text()) == {"v": 2}
    assert [p.name for p in run_dir.iterdir()] == [R.ENVELOPE_FILE]


def test_write_envelope_failed_replace_preserves_existing(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_envelope(run_dir, {"v": 1})

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(R.os, "replace", _boom)
    with pytest.raises(OSError):
        write_envelope(run_dir, {"v": 2})
    monkeypatch.undo()
    # The previous envelope is intact and the failed temp file was unlinked.
    assert json.loads((run_dir / R.ENVELOPE_FILE).read_text()) == {"v": 1}
    assert [p.name for p in run_dir.iterdir()] == [R.ENVELOPE_FILE]


def test_skeleton_is_schema_valid_and_redaction_clean(tmp_path):
    _started_reporter(tmp_path)
    doc = _envelope(tmp_path)
    _validator(ENVELOPE_SCHEMA).validate(doc)
    assert _scan(doc) == []
    # An interrupted run leaves an honest artifact: failed, never-finished.
    assert doc["finished_at"] == doc["created_at"]
    assert doc["result"]["status"] == "failed"
    assert doc["preflight"] == {"status": "disabled"}
    assert doc["trace"] == {"status": "disabled"}
    assert doc["board"] is None
    assert doc["agents"] == ["claude", "codex"]
    assert doc["schema_version"] == 1  # int — tracegraph's strict != check


def test_finish_golden_completed(tmp_path):
    reporter = _started_reporter(tmp_path, team="python-team")
    result = _completed_result()
    path = reporter.finish(result, degraded=[])
    assert path == tmp_path / "runs" / "run-1" / R.ENVELOPE_FILE
    doc = _envelope(tmp_path)
    _validator(ENVELOPE_SCHEMA).validate(doc)
    assert _scan(doc) == []
    block = doc["result"]
    assert block["status"] == "completed"
    assert block["agent_id"] == "claude"
    assert block["winner"] == "claude"
    assert block["gate_passed"] is True
    assert block["files_changed_count"] == 2  # count only — never the path list
    assert block["artifact"] == R.RESULT_FILE  # relative to the envelope dir
    # The digest is of the EXACT result.json bytes on disk.
    result_bytes = (tmp_path / "runs" / "run-1" / R.RESULT_FILE).read_bytes()
    assert block["digest"] == sha256_digest(result_bytes)
    assert doc["team"] == "python-team"
    assert doc["finished_at"] >= doc["created_at"]
    # result.json is the one body-carrying artifact — output/patch live THERE.
    body = json.loads(result_bytes)
    assert body["output"] == "did the thing"
    assert body["patch"] == result.patch


def test_finish_failed_result(tmp_path):
    reporter = _started_reporter(tmp_path)
    result = _completed_result(
        status=TaskStatus.FAILED, exit_code=1, gate=None, patch=None, error="boom"
    )
    reporter.finish(result, degraded=["memory"])
    doc = _envelope(tmp_path)
    _validator(ENVELOPE_SCHEMA).validate(doc)
    assert doc["result"]["status"] == "failed"
    assert doc["result"]["winner"] is None
    assert doc["result"]["gate_passed"] is None
    assert doc["result"]["patch_digest"] is None
    assert doc["degraded"] == ["memory"]


def test_patch_digest_matches_emit_patch_file_bytes(tmp_path):
    # A non-UTF-8 patch (surrogateescape round-trip): the envelope digest must
    # verify the ACTUAL bytes `run --emit-patch` writes.
    raw = b"diff --git a/x b/x\n+\xff\xfe binary-ish\n"
    patch = raw.decode("utf-8", errors="surrogateescape")
    reporter = _started_reporter(tmp_path)
    reporter.finish(_completed_result(patch=patch), degraded=[])
    doc = _envelope(tmp_path)
    out = tmp_path / "winner.patch"
    assert emit_patch_file(out, {"status": "completed", "patch": patch})
    assert doc["result"]["patch_digest"] == sha256_digest(out.read_bytes())
    assert out.read_bytes() == raw


def test_digest_stability(tmp_path):
    # The same result must digest identically across runs — the join guarantee.
    result = _completed_result(task_id=uuid4())
    r1 = _started_reporter(tmp_path, run_id="run-a")
    r2 = _started_reporter(tmp_path, run_id="run-b")
    assert r1.finish(result, degraded=[]) is not None
    assert r2.finish(result, degraded=[]) is not None
    d1, d2 = _envelope(tmp_path, "run-a"), _envelope(tmp_path, "run-b")
    assert d1["result"]["digest"] == d2["result"]["digest"]
    assert d1["result"]["patch_digest"] == d2["result"]["patch_digest"]


def test_start_skeleton_write_failure_degrades_and_finish_recovers(
    tmp_path, monkeypatch
):
    reporter = build_reporter(_report_config(tmp_path))
    monkeypatch.setattr(R, "write_envelope", lambda *_: (_ for _ in ()).throw(OSError("nope")))
    reporter.start(
        run_id="run-1", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    assert reporter.degraded  # warned, never raised
    monkeypatch.undo()
    # finish retries: the run dir exists (created before the failed write) and the
    # final envelope lands.
    path = reporter.finish(_completed_result(), degraded=[])
    assert path is not None
    _validator(ENVELOPE_SCHEMA).validate(_envelope(tmp_path))


def test_unwritable_runs_dir_never_raises(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("")  # a FILE where the runs dir should go → mkdir fails
    reporter = build_reporter(_report_config(tmp_path, runs_dir=str(blocker / "runs")))
    reporter.start(
        run_id="run-1", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    assert reporter.degraded
    assert reporter.finish(_completed_result(), degraded=[]) is None  # still no raise


def test_unresolvable_home_runtimeerror_swallowed(tmp_path, monkeypatch):
    # expanduser() raises RuntimeError (not OSError) when ~ can't resolve (no-HOME
    # cron/systemd) — the board precedent: it must degrade, never crash the run.
    def _boom(self):
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(pathlib.Path, "expanduser", _boom)
    reporter = build_reporter(ReportConfig(enabled=True, runs_dir="~/runs"))
    reporter.start(
        run_id="run-1", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    assert reporter.degraded
    assert reporter.finish(_completed_result(), degraded=[]) is None


def test_patch_envelope_board(tmp_path):
    reporter = _started_reporter(tmp_path)
    reporter.finish(_completed_result(), degraded=[])
    assert patch_envelope_board(
        tmp_path / "runs", "run-1", item_id="item-9", goal="backend"
    )
    doc = _envelope(tmp_path)
    _validator(ENVELOPE_SCHEMA).validate(doc)
    assert doc["board"] == {"item_id": "item-9", "goal": "backend"}
    # The rest of the finished envelope is untouched.
    assert doc["result"]["status"] == "completed"


def test_patch_envelope_board_goal_none(tmp_path):
    _started_reporter(tmp_path)
    assert patch_envelope_board(tmp_path / "runs", "run-1", item_id="i", goal=None)
    doc = _envelope(tmp_path)
    _validator(ENVELOPE_SCHEMA).validate(doc)
    assert doc["board"] == {"item_id": "i", "goal": None}


def test_patch_envelope_board_missing_envelope_returns_false(tmp_path):
    assert not patch_envelope_board(tmp_path / "runs", "ghost", item_id="i", goal=None)


def _seed_run_dirs(runs_dir, names_oldest_first):
    runs_dir.mkdir(parents=True, exist_ok=True)
    base = time.time() - 10_000
    for i, name in enumerate(names_oldest_first):
        d = runs_dir / name
        d.mkdir()
        os.utime(d, (base + i, base + i))


def test_retention_drops_oldest_beyond_keep_last(tmp_path):
    runs = tmp_path / "runs"
    _seed_run_dirs(runs, ["old-1", "old-2", "old-3", "old-4"])
    reporter = build_reporter(_report_config(tmp_path, keep_last=3))
    reporter.start(
        run_id="current", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    kept = {d.name for d in runs.iterdir()}
    # keep_last counts the current run: current + the 2 newest others.
    assert kept == {"current", "old-3", "old-4"}


def test_retention_preserves_current_run_even_when_oldest(tmp_path):
    runs = tmp_path / "runs"
    _seed_run_dirs(runs, ["current", "new-1", "new-2", "new-3"])
    reporter = build_reporter(_report_config(tmp_path, keep_last=2))
    reporter.start(
        run_id="current", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    kept = {d.name for d in runs.iterdir()}
    assert "current" in kept
    assert kept == {"current", "new-3"}


def test_retention_keep_last_one_leaves_only_current(tmp_path):
    runs = tmp_path / "runs"
    _seed_run_dirs(runs, ["old-1", "old-2"])
    reporter = build_reporter(_report_config(tmp_path, keep_last=1))
    reporter.start(
        run_id="current", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    assert {d.name for d in runs.iterdir()} == {"current"}


def test_retention_rmtree_failure_does_not_abort(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    _seed_run_dirs(runs, ["old-1", "old-2", "old-3"])
    real_rmtree = R.shutil.rmtree

    def _flaky(path, *a, **kw):
        if pathlib.Path(path).name == "old-1":
            raise OSError("stuck")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(R.shutil, "rmtree", _flaky)
    reporter = build_reporter(_report_config(tmp_path, keep_last=1))
    reporter.start(
        run_id="current", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    kept = {d.name for d in runs.iterdir()}
    # old-1 is stuck (warned) but the sweep continued, the skeleton still landed,
    # and the reporter did not degrade over a retention hiccup.
    assert kept == {"current", "old-1"}
    assert (runs / "current" / R.ENVELOPE_FILE).exists()


def test_load_envelope_and_list_reports(tmp_path):
    for i, rid in enumerate(["run-a", "run-b"]):
        r = _started_reporter(tmp_path, run_id=rid)
        time.sleep(0.01)  # distinct created_at timestamps
        r.finish(_completed_result(), degraded=[])
    (tmp_path / "runs" / "garbage").mkdir()  # no envelope — skipped
    (tmp_path / "runs" / "run-c").mkdir()
    (tmp_path / "runs" / "run-c" / R.ENVELOPE_FILE).write_text("not json")
    docs = list_reports(tmp_path / "runs")
    assert [d["run_id"] for d in docs] == ["run-b", "run-a"]  # newest first
    assert load_envelope(tmp_path / "runs", "run-a")["run_id"] == "run-a"
    assert load_envelope(tmp_path / "runs", "ghost") is None
    assert list_reports(tmp_path / "does-not-exist") == []


def test_render_report_fields_and_no_bodies(tmp_path):
    reporter = _started_reporter(tmp_path, team="python-team")
    reporter.finish(_completed_result(), degraded=["memory"])
    patch_envelope_board(tmp_path / "runs", "run-1", item_id="item-9", goal="backend")
    text = render_report(_envelope(tmp_path))
    for expected in [
        "run-1",
        "route",
        "python-team",
        "claude, codex",
        "item-9",
        "completed",
        "sha256:",
        "memory",
    ]:
        assert expected in text
    assert "did the thing" not in text  # no bodies, by contract


def test_render_report_includes_phoenix_stream_receipt(tmp_path):
    reporter = _started_reporter(tmp_path)
    reporter.finish(_completed_result(), degraded=["phoenix"])
    envelope = _envelope(tmp_path)
    envelope["trace"] = {
        "status": "ok",
        "protocol": "otlp-json-file",
        "trace_id": "a" * 32,
        "artifact": "trace.otlp.json",
        "digest": "sha256:" + "b" * 64,
        "stream": {
            "backend": "phoenix",
            "protocol": "otlp-http/protobuf",
            "status": "error",
            "exported_span_count": 3,
            "dropped_span_count": 1,
        },
    }

    text = render_report(envelope)

    assert "Phoenix stream:    error (3 exported, 1 dropped)" in text


def test_retention_failure_does_not_cost_the_skeleton(tmp_path, monkeypatch):
    # codex review (S3): a sweep failure (listing race / permission hiccup on
    # OTHER runs' dirs) is warn-only — THIS run's skeleton must still land and
    # the reporter must not degrade.
    monkeypatch.setattr(
        R, "_sweep_retention", lambda *a: (_ for _ in ()).throw(OSError("race"))
    )
    reporter = build_reporter(_report_config(tmp_path))
    reporter.start(
        run_id="run-1", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    assert not reporter.degraded
    _validator(ENVELOPE_SCHEMA).validate(_envelope(tmp_path))


def test_retention_stat_race_sorts_entry_oldest_not_raises(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    _seed_run_dirs(runs, ["old-1", "old-2", "old-3"])
    real_stat = pathlib.Path.stat

    def _flaky_stat(self, **kw):
        if self.name == "old-2":  # vanished-concurrently / permission hiccup
            raise OSError("gone")
        return real_stat(self, **kw)

    monkeypatch.setattr(pathlib.Path, "stat", _flaky_stat)
    reporter = build_reporter(_report_config(tmp_path, keep_last=3))
    reporter.start(
        run_id="current", task_id="t", strategy="route", team=None, agents=["claude"]
    )
    monkeypatch.undo()
    # The unstat-able entry sorted oldest and became the removal candidate; its
    # rmtree also failed (same hiccup) and was tolerated per-entry. The point:
    # the sweep never raised, the reporter never degraded, the skeleton landed,
    # and the genuinely-newest entries were never touched.
    assert not reporter.degraded
    assert {"current", "old-1", "old-3"} <= {d.name for d in runs.iterdir()}
    assert (runs / "current" / R.ENVELOPE_FILE).exists()


# ── sweep_runs_dir: retention with no RunReporter (trace-only runs) ───────────


def test_sweep_runs_dir_drops_oldest_beyond_keep_last(tmp_path):
    runs = tmp_path / "runs"
    _seed_run_dirs(runs, ["old-1", "old-2", "old-3", "current"])
    sweep_runs_dir(str(runs), 3, "current")
    # Same semantics as the reporter's sweep: keep_last counts the current run.
    assert {d.name for d in runs.iterdir()} == {"current", "old-2", "old-3"}


def test_sweep_runs_dir_preserves_current_even_when_oldest(tmp_path):
    runs = tmp_path / "runs"
    _seed_run_dirs(runs, ["current", "new-1", "new-2"])
    sweep_runs_dir(str(runs), 2, "current")
    assert {d.name for d in runs.iterdir()} == {"current", "new-2"}


def test_sweep_runs_dir_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    runs = tmp_path / "runs"
    _seed_run_dirs(runs, ["old-1", "current"])
    sweep_runs_dir("~/runs", 1, "current")
    assert {d.name for d in runs.iterdir()} == {"current"}


def test_sweep_runs_dir_missing_dir_is_noop(tmp_path):
    sweep_runs_dir(str(tmp_path / "absent"), 3, "current")  # must not raise
    assert not (tmp_path / "absent").exists()


def test_sweep_runs_dir_unresolvable_home_degrades(tmp_path, monkeypatch):
    def _boom(self):
        raise RuntimeError("no home directory")

    monkeypatch.setattr(R.Path, "expanduser", _boom)
    sweep_runs_dir("~/runs", 3, "current")  # warns, never raises


def test_sweep_runs_dir_never_raises_on_unexpected_error(tmp_path, monkeypatch):
    # The wrapper is genuinely exception-safe rather than relying on a caller
    # guard: the Supervisor calls it inside a try whose `except BaseException`
    # marks the run artifact recovery-required, so a raising sweep would falsely
    # condemn a healthy run.
    def _boom(*a, **kw):
        raise ValueError("unexpected")

    monkeypatch.setattr(R, "_sweep_retention", _boom)
    sweep_runs_dir(str(tmp_path), 3, "current")
