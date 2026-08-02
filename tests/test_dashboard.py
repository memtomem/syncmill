"""dashboard.py — the Phase 3 TUI over the work-board.

Drives the REAL ``Board`` against ``tmp_path/"board.db"`` with the injected clock
(the ``test_board.py`` pattern) and the Textual app **headless** via
``App.run_test()`` — hermetic: no real TTY, no timer waits. Every app is built
with ``refresh_seconds=3600`` so the interval timer can never fire mid-test; every
poll tick is an explicit ``pilot.press("r")`` / ``action_refresh()``. Assertions
go against app-owned seams (``status_text`` / ``detail_text`` /
``last_action_error``) rather than Textual widget internals, which shift across
majors.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from syncmill.board import Board, BoardError, Evidence, WorkState
from syncmill.dashboard import (
    DEFAULT_REFRESH_SECONDS,
    EDIT_STATES,
    FILTER_CYCLE,
    MIN_REFRESH_SECONDS,
    DashboardApp,
    _age,
    _status_line,
    clamp_refresh,
    take_snapshot,
)
from textual.widgets import Checkbox, DataTable, Input, OptionList, Select


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def board(tmp_path, clock) -> Board:
    bd = Board(str(tmp_path / "board.db"), now=clock)
    yield bd
    bd.close()  # idempotent — safe after a test already closed it


def seed(board: Board) -> dict[str, str]:
    """A small board across states: a planned item, a blocked+human-required one,
    and a review one carrying evidence. Returns ids by label."""
    planned = board.add("write the parser", goal="g1")
    blocked = board.add("fix the flaky test", goal="g1", human_required=True)
    board.transition(blocked.id, WorkState.BLOCKED.value, actor="test", note="needs creds")
    review = board.add("wire the CLI", goal="g2")
    board.transition(
        review.id,
        WorkState.REVIEW.value,
        actor="test",
        evidence=Evidence(kind="gate", ref="pytest -q", passed=True, at=1000.0, by="gate"),
    )
    return {"planned": planned.id, "blocked": blocked.id, "review": review.id}


def drive(coro):
    """Run one headless app scenario. Deliberately ``asyncio.run`` (a fresh loop
    Textual owns per test), NOT ``memory.run_async`` — the dashboard never touches
    memory, and the shared-module-loop rule is about memtomem."""
    return asyncio.run(coro)


def make_app(board: Board, clock: Clock) -> DashboardApp:
    return DashboardApp(board, refresh_seconds=3600, now=clock)


# ── pure layer ───────────────────────────────────────────────────────────────


def test_take_snapshot_reads_items_stats_decisions(board, clock):
    ids = seed(board)
    board.add_decision("use uv, not pip", scope="g1")
    snap = take_snapshot(board, now=clock)
    assert [i.id for i in snap.items] == [ids["planned"], ids["blocked"], ids["review"]]
    assert snap.stats.total == 3
    assert snap.stats.by_state == {"planned": 1, "blocked": 1, "review": 1}
    assert snap.decisions_count == 1
    assert snap.detail is None and snap.detail_transitions == []
    assert snap.at == clock.t


def test_take_snapshot_state_filter(board, clock):
    ids = seed(board)
    snap = take_snapshot(board, state="blocked", now=clock)
    assert [i.id for i in snap.items] == [ids["blocked"]]
    # stats stay whole-board — the filter narrows the table, not the health view
    assert snap.stats.total == 3


def test_take_snapshot_selected_id_populates_detail(board, clock):
    ids = seed(board)
    snap = take_snapshot(board, selected_id=ids["review"], now=clock)
    assert snap.detail is not None and snap.detail.id == ids["review"]
    assert snap.detail_transitions  # the planned→review audit row
    assert snap.detail_transitions[-1]["to_state"] == "review"


def test_take_snapshot_unknown_selected_id_is_detailless(board, clock):
    seed(board)
    snap = take_snapshot(board, selected_id="not-a-uuid", now=clock)
    assert snap.detail is None and snap.detail_transitions == []


def test_take_snapshot_closed_board_raises_boarderror(board, clock):
    board.close()
    with pytest.raises(BoardError):
        take_snapshot(board, now=clock)


def test_clamp_refresh():
    assert clamp_refresh(0.1) == MIN_REFRESH_SECONDS
    assert clamp_refresh(2.0) == 2.0
    assert clamp_refresh(DEFAULT_REFRESH_SECONDS) == DEFAULT_REFRESH_SECONDS


def test_age_buckets():
    assert _age(1000.0, 990.0) == "10s"
    assert _age(1000.0, 1005.0) == "0s"  # clock skew never goes negative
    assert _age(1000.0, 1000.0 - 300) == "5m"
    assert _age(1000.0, 1000.0 - 3 * 3600) == "3h"
    assert _age(1000.0, 1000.0 - 2 * 86400) == "2d"


def test_status_line_variants(board, clock):
    seed(board)
    snap = take_snapshot(board, now=clock)
    line = _status_line(snap, None, None)
    assert "items:3" in line
    assert "blocked=1" in line and "planned=1" in line and "review=1" in line
    assert "filter:all" in line and "decisions:0" in line
    assert "DEGRADED" not in line
    assert "filter:blocked" in _status_line(snap, "blocked", None)
    degraded = _status_line(snap, None, "list failed: boom")
    assert "DEGRADED: list failed: boom" in degraded
    assert "no data yet" in _status_line(None, None, None)


def test_edit_states_exclude_running_and_filter_cycle_shape():
    assert "running" not in EDIT_STATES
    assert set(EDIT_STATES) == {"planned", "blocked", "review", "done"}
    assert FILTER_CYCLE[0] is None
    assert set(FILTER_CYCLE[1:]) == {s.value for s in WorkState}


# ── the app, headless ────────────────────────────────────────────────────────


def test_initial_load_renders_items_and_status(board, clock):
    seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#items", DataTable).row_count == 3
            assert "items:3" in app.status_text

    drive(scenario())


def test_poll_picks_up_out_of_band_writer(board, clock, tmp_path):
    seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#items", DataTable).row_count == 3
            # a second Board on the same db — the live `board run` writer stand-in
            writer = Board(str(tmp_path / "board.db"), now=clock)
            try:
                writer.add("out-of-band item", goal="g3")
            finally:
                writer.close()
            await pilot.press("r")
            await pilot.pause()
            assert app.query_one("#items", DataTable).row_count == 4
            assert "items:4" in app.status_text

    drive(scenario())


def test_filter_cycles_states(board, clock):
    seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f")  # all → planned
            await pilot.pause()
            table = app.query_one("#items", DataTable)
            assert table.row_count == 1
            assert "filter:planned" in app.status_text
            for _ in range(len(FILTER_CYCLE) - 1):  # cycle back around to all
                await pilot.press("f")
            await pilot.pause()
            assert table.row_count == 3
            assert "filter:all" in app.status_text

    drive(scenario())


def test_select_row_shows_detail_with_evidence_and_history(board, clock):
    ids = seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#items", DataTable)
            table.move_cursor(row=table.get_row_index(ids["review"]))
            await pilot.pause()
            assert f"id:       {ids['review']}" in app.detail_text
            assert "evidence:" in app.detail_text and "[gate/pass] pytest -q" in app.detail_text
            assert "history:" in app.detail_text and "→ review" in app.detail_text

    drive(scenario())


def test_apply_update_transitions_and_refreshes(board, clock):
    ids = seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._apply_update(ids["planned"], state="review", note="looks good")
            await pilot.pause()
            item = board.get(ids["planned"])
            assert item.state is WorkState.REVIEW
            audit = board.transitions(ids["planned"])
            assert audit[-1]["actor"] == "human" and audit[-1]["note"] == "looks good"
            assert app.last_action_error is None
            # the immediate post-edit refresh re-rendered the table
            table = app.query_one("#items", DataTable)
            assert table.get_row(ids["planned"])[1] == "review"

    drive(scenario())


def test_sign_off_promotes_to_done(board, clock):
    ids = seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._apply_update(ids["review"], state="done", sign_off=True)
            item = board.get(ids["review"])
            assert item.state is WorkState.DONE
            assert any(e.kind == "human" and e.passed for e in item.evidence)

    drive(scenario())


def test_done_gate_rejection_keeps_app_alive(board, clock):
    ids = seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app._apply_update(ids["planned"], state="done")  # no verification
            assert "without verification" in app.last_action_error
            assert board.get(ids["planned"]).state is WorkState.PLANNED
            await pilot.press("r")  # still responsive
            await pilot.pause()
            assert "items:3" in app.status_text

    drive(scenario())


def test_nothing_to_update_is_guarded(board, clock):
    ids = seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            # note-only mirrors the CLI's UsageError: not an update
            assert not app._apply_update(ids["planned"], note="just a note")
            assert app.last_action_error is None  # a guard, not a board rejection

    drive(scenario())


def test_edit_modal_end_to_end(board, clock):
    ids = seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#items", DataTable)
            table.move_cursor(row=table.get_row_index(ids["review"]))
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            # fill the modal via widget values (typing into Input is version-shifty)
            app.screen.query_one("#edit-state", Select).value = "done"
            app.screen.query_one("#edit-note", Input).value = "ship it"
            app.screen.query_one("#edit-sign-off", Checkbox).value = True
            await pilot.click("#edit-apply")
            await pilot.pause()
            item = board.get(ids["review"])
            assert item.state is WorkState.DONE
            assert any(e.kind == "human" for e in item.evidence)
            audit = board.transitions(ids["review"])
            assert audit[-1]["actor"] == "human" and audit[-1]["note"] == "ship it"

    drive(scenario())


def test_quick_actions_unblock_and_clear_human(board, clock):
    ids = seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#items", DataTable)
            table.move_cursor(row=table.get_row_index(ids["blocked"]))
            await pilot.pause()
            await pilot.press("h")
            await pilot.pause()
            assert board.get(ids["blocked"]).human_required is False
            await pilot.press("u")
            await pilot.pause()
            item = board.get(ids["blocked"])
            assert item.state is WorkState.PLANNED
            audit = board.transitions(ids["blocked"])
            assert audit[-1]["note"] == "unblocked" and audit[-1]["actor"] == "human"

    drive(scenario())


def test_degraded_poll_keeps_last_snapshot(board, clock):
    seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query_one("#items", DataTable).row_count == 3
            board.close()  # a mid-run read failure (sqlite error → BoardError)
            await pilot.press("r")
            await pilot.pause()
            assert "DEGRADED" in app.status_text
            # the last good snapshot stays on screen
            assert app.query_one("#items", DataTable).row_count == 3
            assert "items:3" in app.status_text

    drive(scenario())


def test_stats_overlay_shows_and_dismisses(board, clock):
    seed(board)

    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            from syncmill.dashboard import StatsScreen

            assert isinstance(app.screen, StatsScreen)
            body = app.screen.query_one("#stats-body")
            # render_board_stats verbatim — same text as `board stats`
            assert "items: 3" in str(body.render())
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, StatsScreen)

    drive(scenario())


# ── TEAM pane (P3) ────────────────────────────────────────────────────────────


from syncmill.config import SyncmillConfig  # noqa: E402
from syncmill.dashboard import TeamEditModal, TeamScreen  # noqa: E402
from syncmill.teamfile import load_config_at, render_team  # noqa: E402

_TEAM_TOML = """[teams.demo]
agents = ["codex"]
goals = ["backend"]

[teams.zeta]
agents = ["claude"]
"""


def make_team_app(board, clock, tmp_path, body: str | None = _TEAM_TOML):
    """An app with the TEAM pane wired: config loaded FROM the tmp toml file
    (the CLI recipe — load + pass the same path the writes target)."""
    p = tmp_path / "syncmill.toml"
    if body is not None:
        p.write_text(body)
    config = load_config_at(p)
    app = DashboardApp(
        board, refresh_seconds=3600, now=clock, config=config, toml_path=p
    )
    return app, p


def test_team_screen_lists_marks_default_and_details(board, clock, tmp_path):
    async def scenario():
        app, p = make_team_app(
            board, clock, tmp_path, 'default_team = "zeta"\n\n' + _TEAM_TOML
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert isinstance(app.screen, TeamScreen)
            option_list = app.screen.query_one("#team-list")
            prompts = [
                str(option_list.get_option_at_index(i).prompt)
                for i in range(option_list.option_count)
            ]
            assert prompts == ["demo", "zeta * (default)"]
            # detail is the shared renderer verbatim — byte-identical to `team show`
            cfg = app._config
            assert app.team_detail_text == render_team("demo", cfg.teams["demo"], cfg)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, TeamScreen)

    drive(scenario())


def test_team_screen_highlight_switches_detail(board, clock, tmp_path):
    async def scenario():
        app, p = make_team_app(board, clock, tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            option_list = app.screen.query_one("#team-list")
            option_list.highlighted = 1  # zeta
            await pilot.pause()
            cfg = app._config
            assert app.team_detail_text == render_team("zeta", cfg.teams["zeta"], cfg)

    drive(scenario())


def test_t_without_config_notifies_and_lives(board, clock):
    async def scenario():
        app = make_app(board, clock)  # board-only construction — no TEAM inputs
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert not isinstance(app.screen, TeamScreen)
            assert app.is_running

    drive(scenario())


def test_apply_team_save_success_writes_and_reloads(board, clock, tmp_path):
    async def scenario():
        app, p = make_team_app(board, clock, tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            ok = app._apply_team_save(
                {
                    "name": "new-team",
                    "agents": "kimi-code, claude",
                    "strategy": "compete",
                    "goals": "porting",
                    "capabilities": "kimi-code=rust,ffi",
                }
            )
            assert ok, app.last_team_error
            assert app.last_team_error is None
            assert "[teams.new-team]" in p.read_text()
            # the reload made the new team visible to the app's config snapshot
            team = app._config.teams["new-team"]
            assert team.agents == ["kimi-code", "claude"]
            assert team.capabilities == {"kimi-code": ["rust", "ffi"]}

    drive(scenario())


def test_apply_team_save_validation_failure_mirrors_and_leaves_file(
    board, clock, tmp_path
):
    async def scenario():
        app, p = make_team_app(board, clock, tmp_path)
        before = p.read_text()
        async with app.run_test() as pilot:
            await pilot.pause()
            # "backend" already belongs to demo — rejected, file untouched, app alive
            ok = app._apply_team_save(
                {"name": "b", "agents": "claude", "strategy": None,
                 "goals": "backend", "capabilities": ""}
            )
            assert not ok
            assert "declared by two teams" in app.last_team_error
            assert p.read_text() == before
            assert app.is_running

    drive(scenario())


def test_reload_teams_survives_a_malformed_toml(board, clock, tmp_path):
    async def scenario():
        # A mid-edit malformed syncmill.toml (tomllib raises a ValueError
        # subclass) must keep the cached config view + mirror the error —
        # never kill the app. The TUI's _reload_config catch-set, mirrored.
        app, p = make_team_app(board, clock, tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = app._config
            p.write_text("[teams.broken\n", encoding="utf-8")  # unclosed table
            app._reload_teams()
            assert app._config is before
            assert "config reload failed" in (app.last_team_error or "")
            assert app.is_running

    drive(scenario())


def test_apply_team_save_write_failure_mirrors(board, clock, tmp_path):
    async def scenario():
        # config from init kwargs; the write target's parent doesn't exist → the
        # an unwritable directory becomes TeamFileError → mirror, app alive
        # (the lock is taken first, so it reports the missing directory)
        config = SyncmillConfig(teams={"demo": {"agents": ["codex"]}})
        app = DashboardApp(
            board,
            refresh_seconds=3600,
            now=clock,
            config=config,
            toml_path=tmp_path / "no-such-dir" / "syncmill.toml",
        )
        async with app.run_test() as pilot:
            await pilot.pause()
            ok = app._apply_team_save(
                {"name": "t", "agents": "codex", "strategy": None,
                 "goals": "", "capabilities": ""}
            )
            assert not ok
            assert "cannot lock" in app.last_team_error
            assert app.is_running

    drive(scenario())


def test_apply_default_team_set_and_clear(board, clock, tmp_path):
    async def scenario():
        app, p = make_team_app(board, clock, tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app._apply_default_team("demo"), app.last_team_error
            assert 'default_team = "demo"' in p.read_text()
            assert app._config.default_team == "demo"  # reloaded
            assert app._apply_default_team(None)
            assert "default_team" not in p.read_text()
            assert app._config.default_team is None

    drive(scenario())


def test_apply_default_team_unknown_mirrors(board, clock, tmp_path):
    async def scenario():
        app, p = make_team_app(board, clock, tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app._apply_default_team("ghost")
            assert "unknown team" in app.last_team_error
            assert "default_team" not in p.read_text()

    drive(scenario())


def test_team_edit_modal_end_to_end(board, clock, tmp_path):
    async def scenario():
        app, p = make_team_app(board, clock, tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("e")  # edit the highlighted team (demo)
            await pilot.pause()
            assert isinstance(app.screen, TeamEditModal)
            # prefilled from the highlighted profile; rename → creates a new table
            app.screen.query_one("#team-name", Input).value = "demo2"
            app.screen.query_one("#team-agents", Input).value = "claude"
            app.screen.query_one("#team-goals", Input).value = ""
            await pilot.click("#team-edit-apply")
            await pilot.pause()
            assert app.last_team_error is None
            assert "[teams.demo2]" in p.read_text()
            assert app._config.teams["demo2"].agents == ["claude"]
            assert isinstance(app.screen, TeamScreen)  # pane refreshed, still open

    drive(scenario())


def test_team_screen_u_toggles_default(board, clock, tmp_path):
    async def scenario():
        app, p = make_team_app(board, clock, tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("u")  # highlighted = demo → set default
            await pilot.pause()
            assert 'default_team = "demo"' in p.read_text()
            option_list = app.screen.query_one("#team-list")
            assert "* (default)" in str(option_list.get_option_at_index(0).prompt)
            await pilot.press("u")  # toggle off
            await pilot.pause()
            assert "default_team" not in p.read_text()

    drive(scenario())


# ── REPORT pane (ecosystem P2 / Stage 3) ──────────────────────────────


from syncmill.dashboard import ReportScreen  # noqa: E402
from syncmill.report import render_report  # noqa: E402


def _seed_report(runs_dir, run_id, created_at, *, agent="claude", status="completed"):
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True)
    doc = {
        "schema_version": 1,
        "kind": "syncmill.run-envelope",
        "run_id": run_id,
        "created_at": created_at,
        "finished_at": created_at,
        "strategy": "route",
        "team": None,
        "task_id": f"task-{run_id}",
        "agents": [agent],
        "board": None,
        "preflight": {"status": "disabled"},
        "trace": {"status": "disabled"},
        "result": {"status": status, "agent_id": agent},
        "degraded": [],
    }
    (run_dir / "envelope.json").write_text(json.dumps(doc))
    return doc


def make_report_app(board, clock, runs_dir):
    config = SyncmillConfig(
        integration={"report": {"enabled": False, "runs_dir": str(runs_dir)}}
    )
    return DashboardApp(board, refresh_seconds=3600, now=clock, config=config)


def test_report_screen_lists_newest_and_renders_verbatim(board, clock, tmp_path):
    runs = tmp_path / "runs"
    old = _seed_report(runs, "run-old", "2026-07-10T00:00:00+00:00")
    new = _seed_report(
        runs, "run-new", "2026-07-11T00:00:00+00:00", agent="codex"
    )

    async def scenario():
        app = make_report_app(board, clock, runs)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, ReportScreen)
            option_list = app.screen.query_one("#report-list", OptionList)
            prompts = [
                str(option_list.get_option_at_index(i).prompt)
                for i in range(option_list.option_count)
            ]
            assert "run-new" in prompts[0] and "codex" in prompts[0]
            assert "run-old" in prompts[1]
            # The viewer works while report recording is disabled and shares the
            # exact `report show` renderer.
            assert app.report_detail_text == render_report(new)

            option_list.highlighted = 1
            await pilot.pause()
            assert app.report_detail_text == render_report(old)
            await pilot.press("p")
            await pilot.pause()
            assert not isinstance(app.screen, ReportScreen)

    drive(scenario())


def test_report_screen_refreshes_and_preserves_selection(board, clock, tmp_path):
    runs = tmp_path / "runs"
    old = _seed_report(runs, "run-old", "2026-07-10T00:00:00+00:00")
    _seed_report(runs, "run-new", "2026-07-11T00:00:00+00:00")

    async def scenario():
        app = make_report_app(board, clock, runs)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            option_list = app.screen.query_one("#report-list", OptionList)
            option_list.highlighted = 1  # run-old
            await pilot.pause()
            assert app.report_detail_text == render_report(old)

            _seed_report(runs, "run-latest", "2026-07-12T00:00:00+00:00")
            await pilot.press("r")
            await pilot.pause()
            assert option_list.option_count == 3
            assert app.report_detail_text == render_report(old)

    drive(scenario())


def test_report_screen_empty_or_corrupt_storage_stays_alive(board, clock, tmp_path):
    runs = tmp_path / "runs"
    corrupt = runs / "corrupt"
    corrupt.mkdir(parents=True)
    (corrupt / "envelope.json").write_text("not json")

    async def scenario():
        app = make_report_app(board, clock, runs)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(app.screen, ReportScreen)
            assert app.report_detail_text == "(no run reports found)"
            assert app.screen.query_one("#report-list", OptionList).option_count == 0
            assert app.is_running
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ReportScreen)

    drive(scenario())


def test_p_without_config_notifies_and_lives(board, clock):
    async def scenario():
        app = make_app(board, clock)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert not isinstance(app.screen, ReportScreen)
            assert app.is_running

    drive(scenario())
