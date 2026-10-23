#!/usr/bin/env python3
"""Render the Korean TUI guide's deterministic 120x40 SVG screenshots.

The captures inject SYNTHETIC rows and detail text directly into the widgets
(no real runs, no provider calls) so the render is deterministic and free.
That means the fixtures here do not automatically follow the real detail/status
renderers — when those formats change, update the injected strings by hand so
the documented screens stay honest.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import re
import subprocess
import tempfile
import time
from pathlib import Path
from xml.etree import ElementTree

from textual.widgets import DataTable, Log, OptionList, Static, TabbedContent
from textual.widgets.option_list import Option

from syncmill.board import Board
from syncmill.config import SyncmillConfig
from syncmill.models import TokenUsage
from syncmill.room_compare import (
    CandidateRow,
    ConversationView,
    FileOverlap,
    GateCheckView,
    GateProfile,
    GateSettingsView,
    GateView,
    LineageView,
    MemberConversation,
    MissingMember,
    RoomComparison,
    RoundGroup,
    render_round_table,
)
from syncmill.tui import SyncmillTuiApp
from syncmill.tui_collaboration import render_candidate_details, render_candidate_output


REPO_ROOT = Path(__file__).resolve().parents[1]
ASSET_DIR = REPO_ROOT / "docs" / "assets" / "tui"
SCREENSHOTS = (
    "01-board.svg",
    "02-runs-operations.svg",
    "03-runs-artifacts-summary.svg",
    "04-runs-artifacts-diff.svg",
    "05-reports.svg",
    "06-teams.svg",
    "07-status.svg",
    "08-sessions.svg",
    "09-rooms-diff.svg",
    "10-rooms-output.svg",
    "11-rooms-details.svg",
    "12-rooms-compare.svg",
)
SCREEN_MARKERS = {
    "01-board.svg": ("Board", "board-01", "run item"),
    "02-runs-operations.svg": ("Operations", "running", "K force"),
    "03-runs-artifacts-summary.svg": ("Artifacts", "run-0001", "view:summary"),
    "04-runs-artifacts-diff.svg": ("Artifacts", "diff --git", "view:diff"),
    "05-reports.svg": ("Reports", "run-0001", "route completed"),
    "06-teams.svg": ("Teams", "starter", "a add"),
    "07-status.svg": ("Status", "backend: memtomem", "agents+memory"),
    "08-sessions.svg": ("Sessions", "session-01", "c checkpoint"),
    "09-rooms-diff.svg": ("Rooms", "room-0001", "d/o/m"),
    "10-rooms-output.svg": ("Rooms", "view:output", "d/o/m/c diff/output/details/compare"),
    "11-rooms-details.svg": ("Rooms", "view:details", "evidence as of"),
    "12-rooms-compare.svg": ("Rooms", "[Compare]", "gate settings"),
}


def _sample_rows() -> tuple[CandidateRow, CandidateRow]:
    """Synthetic comparison rows (fixed ids, times and digests) rendered by
    the REAL Details/Compare renderers, so the guide shows today's wording."""
    common = dict(
        attempt=1, selected=False, base_commit="0123456789ab",
        readable=True, unreadable_reason=None, warnings=(),
        created_at="2026-01-01T00:00:00Z", output_truncated=False,
        provider_session_id=None,
    )
    passed = CandidateRow(
        candidate_id="a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d", member_id="codex", provider_id="codex",
        state="completed", eligible=True, patch_sha256="fedcba9876543210",
        gate=GateView("passed", checks=(GateCheckView("pytest -q", True, 0, 4.1),)),
        duration_seconds=18.2,
        usage=TokenUsage(total_input_tokens=40_100, output_tokens=2_300),
        files_changed=("room-smoke.txt",), patch_bytes=146, patch_binary=False,
        patch_large=False, not_eligible_reason=None, apply_blockers=(),
        output_sha256="0011223344556677", exit_code=0, output_bytes=58,
        **common,
    )
    failed = CandidateRow(
        candidate_id="e5f6a7b8-9c0d-4e1f-8a2b-3c4d5e6f7a8b", member_id="claude", provider_id="claude",
        state="completed", eligible=False, patch_sha256="89abcdef01234567",
        gate=GateView("failed", checks=(GateCheckView("pytest -q", False, 1, 3.7),)),
        duration_seconds=21.5, usage=None,
        files_changed=("room-smoke.txt", "notes.md"), patch_bytes=311,
        patch_binary=False, patch_large=False,
        not_eligible_reason="quality gate failed",
        apply_blockers=("not eligible: quality gate failed",),
        output_sha256="7766554433221100", exit_code=0, output_bytes=73,
        **common,
    )
    return passed, failed


def _sample_comparison() -> RoomComparison:
    passed, failed = _sample_rows()
    group = RoundGroup(
        number=1, round_id="round-0001", state="complete", starting_tree="snapshot",
        gate_profile=GateProfile("commands", ("pytest -q",)),
        rows=(passed, failed),
        missing=(
            MissingMember("kimi-code", "kimi-code", "no_candidate", ("member state: timeout",)),
        ),
        overlap=FileOverlap(
            ("a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d", "e5f6a7b8-9c0d-4e1f-8a2b-3c4d5e6f7a8b"), ("room-smoke.txt",),
            {"a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d": (), "e5f6a7b8-9c0d-4e1f-8a2b-3c4d5e6f7a8b": ("notes.md",)}, {}, (), (),
        ),
        warnings=(),
        lineage=LineageView("recorded", requested="continue-own", tree="snapshot"),
        gate_settings=GateSettingsView(
            "recorded", kind="commands", commands=("pytest -q",),
            timeout_seconds=300, consistency="consistent",
            env_allowlist_status="recorded", env_allowlist=None,
        ),
        conversation=ConversationView(
            "recorded",
            members={
                member: MemberConversation(
                    session="cold", context="full", since_round=None,
                    candidates=(), omitted=False, memory="none",
                )
                for member in ("claude", "codex", "kimi-code")
            },
        ),
    )
    return RoomComparison(
        room_id="room-0001", room_state="awaiting_decision", revision=3,
        selected_candidate_id=None, source_status="current", source_reason=None,
        round_count=1, groups=(group,),
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _sample_repo(root: Path) -> Path:
    repo = root / "sample-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Syncmill Guide")
    _git(repo, "config", "user.email", "guide@example.invalid")
    (repo / "README.md").write_text("# Sample project\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-qm", "sample baseline")
    return repo


def _config(root: Path, repo: Path, board_db: Path) -> SyncmillConfig:
    return SyncmillConfig(
        repo_root=str(repo),
        agent_priority=["codex", "claude", "kimi-code"],
        codex={"enabled": True, "timeout": 120},
        claude={"enabled": False, "timeout": 120},
        kimi_code={"enabled": False, "timeout": 120},
        board={"enabled": True, "db_path": str(board_db)},
        rooms={"enabled": True},
        runs={"root": str(root / "runs")},
        workspace={"root": str(root / "workspaces")},
        tui={"state_dir": str(root / "tui")},
        integration={"report": {"runs_dir": str(root / "reports")}},
        teams={
            "starter": {
                "agents": ["codex"],
                "strategy": "route",
                "goals": ["onboarding"],
                "capabilities": {"codex": ["python", "docs"]},
            }
        },
        default_team="starter",
    )


def _set_options(widget: OptionList, rows: list[tuple[str, str]]) -> None:
    widget.clear_options()
    for label, value in rows:
        widget.add_option(Option(label, id=value))
    if rows:
        widget.highlighted = 0


# One pilot.pause() drains only the messages queued when it starts, then
# waits until the CPU looks idle -- and a starved process on a loaded machine
# looks idle too. A tab switch starts a CHAIN of messages (TabActivated ->
# focus -> DescendantFocus -> TabPane.Focused), and so does a list highlight
# (OptionHighlighted -> the app's real detail lookup). A link still queued
# after the pauses either re-activates the previous tab, which then refreshes
# from the empty stores, or overwrites the fixture written below (#192). Each
# pause advances every chain by at least one link whatever the load, so the
# capture waits for this many consecutive pauses in the expected state (two
# already suffice with the idle wait disabled; the rest is margin).
_SETTLE_PAUSES = 10
_SETTLE_SECONDS = 20.0


def _settled_on(app: SyncmillTuiApp, tab: str, pane: str | None) -> bool:
    if app.query_one("#tabs", TabbedContent).active != tab:
        return False
    if pane is not None and app.query_one("#runs-subtabs", TabbedContent).active != pane:
        return False
    # The Artifacts listing is a thread worker; its result would replace the
    # fixture list.
    if any(not worker.is_finished for worker in app.workers):
        return False
    focused = app.focused
    if tab == "status-tab":
        # Status has no focusable list; switching to it clears focus.
        return focused is None
    container = app.query_one(f"#{pane or tab}")
    return focused is not None and container in focused.ancestors_with_self


async def _settle(
    app: SyncmillTuiApp, pilot, tab: str, pane: str | None = None
) -> None:
    deadline = time.monotonic() + _SETTLE_SECONDS
    stable = 0
    while stable < _SETTLE_PAUSES:
        if time.monotonic() > deadline:
            raise RuntimeError(f"the TUI did not settle on {pane or tab}")
        # delay=0 skips the idle heuristic: the count decides, not the CPU.
        await pilot.pause(0)
        stable = stable + 1 if _settled_on(app, tab, pane) else 0


async def _capture(
    app: SyncmillTuiApp,
    pilot,
    filename: str,
    rendered: dict[str, str],
    tab: str,
    pane: str | None = None,
) -> None:
    # Also lets Textual recompute auto-height Static widgets after the
    # fixture updates.
    await _settle(app, pilot, tab, pane)
    # Settling counts pauses, not time, so the tab underline can still be
    # sliding toward the new tab.
    await pilot.wait_for_animation()
    svg = app.export_screenshot(
        title=f"syncmill TUI guide — {filename.removesuffix('.svg')}",
        simplify=True,
    )
    # Rich assigns a random numeric namespace to every exported SVG. It has no
    # visual meaning. Rich also emits indentation on otherwise blank lines;
    # normalize both so generated assets pass Git's whitespace checks.
    normalized = re.sub(r"terminal-\d+", "terminal-syncmill", svg)
    rendered[filename] = re.sub(r"[ \t]+(?=\n|$)", "", normalized)


class _GuideApp(SyncmillTuiApp):
    def _start_memory_probe(self) -> None:
        # Screenshots inject synthetic status below. A live probe could finish
        # after that update and overwrite it depending on machine speed.
        pass


async def _render_all(root: Path) -> dict[str, str]:
    repo = _sample_repo(root)
    board = Board(str(root / "board.db"))
    board.add(
        "README에 첫 실행 예시를 추가하고 검증해 줘",
        goal="onboarding",
        human_required=True,
    )
    board.add_decision("첫 성공 흐름은 Codex 하나로 시작한다", scope="onboarding")
    config = _config(root, repo, board.db_path)
    toml = repo / "syncmill.toml"
    toml.write_text(
        "config_version = 2\n"
        'repo_root = "."\n'
        'agent_priority = ["codex", "claude", "kimi-code"]\n'
        'default_team = "starter"\n\n'
        "[codex]\nenabled = true\ntimeout_seconds = 120\n\n"
        "[claude]\nenabled = false\ntimeout_seconds = 120\n\n"
        "[kimi_code]\nenabled = false\ntimeout_seconds = 120\n\n"
        f'[board]\nenabled = true\ndb_path = "{board.db_path}"\n\n'
        f'[runs]\nroot = "{root / "runs"}"\n\n'
        f'[workspace]\nroot = "{root / "workspaces"}"\n\n'
        f'[tui]\nstate_dir = "{root / "tui"}"\n\n'
        "[rooms]\nenabled = true\n\n"
        f'[integration.report]\nruns_dir = "{root / "reports"}"\n\n'
        '[teams.starter]\nagents = ["codex"]\nstrategy = "route"\n'
        'goals = ["onboarding"]\n\n'
        '[teams.starter.capabilities]\ncodex = ["python", "docs"]\n',
        encoding="utf-8",
    )
    rendered: dict[str, str] = {}
    app = _GuideApp(
        config,
        board=board,
        board_error=None,
        toml_path=toml,
        refresh_seconds=3600,
        initial_tab="board",
    )
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(app, pilot, "board-tab")
            table = app.query_one("#board-table", DataTable)
            table.clear()
            table.add_row(
                "board-01",
                "planned",
                "onboarding",
                "README에 첫 실행 예시 추가",
                "-",
                "H!",
                key="board-01",
            )
            table.move_cursor(row=0)
            # The highlight's own detail lookup must finish before the fixture
            # is written, or it replaces it with "(not found)".
            await _settle(app, pilot, "board-tab")
            app.query_one("#board-detail", Static).update(
                "Item: board-01\nState: planned\nGoal: onboarding\n"
                "Task: README에 첫 실행 예시를 추가하고 검증해 줘\n"
                "Owner: -\nDependencies: -\nHuman required: yes\n"
                "Evidence: -\n\nDecisions\n"
                "첫 성공 흐름은 Codex 하나로 시작한다\n\nHistory\ncreated"
            )
            app.query_one("#board-status", Static).update(
                "items:1 | filter:all | decisions:1"
            )
            await _capture(app, pilot, SCREENSHOTS[0], rendered, "board-tab")

            await pilot.press("2")
            await _settle(app, pilot, "runs-tab", "operations-pane")
            _set_options(
                app.query_one("#job-list", OptionList),
                [("job-0001 running selected board-0001", "/sample/job-0001")],
            )
            await _settle(app, pilot, "runs-tab", "operations-pane")
            log = app.query_one("#job-log", Log)
            log.clear()
            log.write_line("[syncmill] Agent: codex (forced, no fallback)")
            log.write_line("[codex] inspecting sample project")
            log.write_line("[codex] writing beginner example...")
            app.query_one("#job-status", Static).update(
                "running | exit:- | cwd:/sample/project"
            )
            await _capture(
                app, pilot, SCREENSHOTS[1], rendered, "runs-tab", "operations-pane"
            )

            await pilot.press("v")
            await _settle(app, pilot, "runs-tab", "artifacts-pane")
            _set_options(
                app.query_one("#artifact-list", OptionList),
                [("run-0001 pending codex route", "run-0001")],
            )
            await _settle(app, pilot, "runs-tab", "artifacts-pane")
            app.query_one("#artifact-status", Static).update(
                "artifacts:1 | view:summary"
            )
            app.query_one("#artifact-detail", Static).update(
                "Run: run-0001\n"
                "State: pending\nStrategy: route\nAgent: codex\n"
                "Created: 2026-01-01T00:00:00Z\n"
                "Patch: 182 bytes | sha256: 0123456789abcdef\n"
                "Files: README.md\n\n"
                "Result: completed | exit: 0 | 12.4s\n"
                "Beginner example added and checks passed."
            )
            await _capture(
                app, pilot, SCREENSHOTS[2], rendered, "runs-tab", "artifacts-pane"
            )

            app.query_one("#artifact-status", Static).update(
                "artifacts:1 | view:diff"
            )
            app.query_one("#artifact-detail", Static).update(
                "Run: run-0001\nPatch: 182 bytes\n"
                "sha256: 0123456789abcdef\n\n"
                "diff --git a/README.md b/README.md\n"
                "--- a/README.md\n+++ b/README.md\n"
                "@@ -1 +1,4 @@\n # Sample project\n"
                "+\n+## First run\n+Run doctor, then start with Codex."
            )
            await _capture(
                app, pilot, SCREENSHOTS[3], rendered, "runs-tab", "artifacts-pane"
            )

            await pilot.press("3")
            await _settle(app, pilot, "reports-tab")
            _set_options(
                app.query_one("#report-list", OptionList),
                [("run-0001 route completed", "run-0001")],
            )
            await _settle(app, pilot, "reports-tab")
            app.query_one("#report-detail", Static).update(
                "Run: run-0001\nStrategy: route\nStatus: completed\n"
                "Team: starter\nTask: beginner-guide\nAgent: codex\n"
                "Winner: codex | Exit: 0 | Duration: 12.4s\n"
                "Gate: passed | Files changed: 1\n"
                "Artifact: run-0001 | Digest: 0123456789abcdef\n"
                "Preflight: allow | Trace: sample-trace"
            )
            await _capture(app, pilot, SCREENSHOTS[4], rendered, "reports-tab")

            await pilot.press("4")
            await _capture(app, pilot, SCREENSHOTS[5], rendered, "teams-tab")

            await pilot.press("5")
            await _settle(app, pilot, "status-tab")
            app.query_one("#memory-status", Static).update(
                "Memory: ready | backend: memtomem | project: sample"
            )
            await _capture(app, pilot, SCREENSHOTS[6], rendered, "status-tab")

            await pilot.press("6")
            await _settle(app, pilot, "sessions-tab")
            _set_options(
                app.query_one("#session-list", OptionList),
                [("session-01 codex open live", "session-01")],
            )
            await _settle(app, pilot, "sessions-tab")
            app.query_one("#session-detail", Static).update(
                "Session: session-01\nAgent: codex\nState: open | live: True\n"
                "Source: clean snapshot\nWorktree: /sample/workspaces/session-01\n"
                "Checkpoints: run-0001\n\nx attach | c checkpoint | k close"
            )
            app.query_one("#session-status", Static).update(
                "sessions:1 | selected:session-01"
            )
            await _capture(app, pilot, SCREENSHOTS[7], rendered, "sessions-tab")

            await pilot.press("7")
            await _settle(app, pilot, "rooms-tab")
            _set_options(
                app.query_one("#room-list", OptionList),
                [("room-0001 completed rounds=1", "room-0001")],
            )
            # The room highlight's lookup rebuilds the candidate list, so it
            # must finish before the candidate fixture goes in.
            await _settle(app, pilot, "rooms-tab")
            _set_options(
                app.query_one("#candidate-list", OptionList),
                [("a1b2c3d4 codex completed ✓", "a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d")],
            )
            await _settle(app, pilot, "rooms-tab")
            app.query_one("#room-detail", Static).update(
                "Room: room-0001\nState: completed | revision: 3\n"
                "Members: codex\nSelected: a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d\nApplied run: -\n\n"
                "Recent events\nround 1 completed\ncandidate selected"
            )
            app.query_one("#room-status", Static).update(
                "rooms:1 | candidates:1 | view:diff"
            )
            app.query_one("#candidate-detail", Static).update(
                "Candidate: a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d | Diff\n"
                "sha256: fedcba9876543210\n\n"
                "diff --git a/room-smoke.txt b/room-smoke.txt\n"
                "new file mode 100644\n--- /dev/null\n+++ b/room-smoke.txt\n"
                "@@ -0,0 +1 @@\n+room candidate"
            )
            await _capture(app, pilot, SCREENSHOTS[8], rendered, "rooms-tab")

            app.query_one("#room-status", Static).update(
                "rooms:1 | candidates:1 | view:output"
            )
            app.query_one("#candidate-detail", Static).update(
                render_candidate_output(
                    {
                        "candidate_id": "a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                        "state": "completed",
                        "exit_code": 0,
                        "duration_seconds": 18.2,
                        "output": "Created room-smoke.txt in the isolated candidate worktree.",
                    }
                )
            )
            await _capture(app, pilot, SCREENSHOTS[9], rendered, "rooms-tab")

            app.query_one("#room-status", Static).update(
                "rooms:1 | candidates:1 | view:details"
            )
            app.query_one("#candidate-detail", Static).update(
                render_candidate_details(
                    _sample_rows()[0],
                    round_number=1,
                    round_prompt="Add a room smoke file",
                    as_of="12:00:00",
                    group=_sample_comparison().groups[0],
                )
            )
            await _capture(app, pilot, SCREENSHOTS[10], rendered, "rooms-tab")

            app.query_one("#room-status", Static).update(
                "rooms:1 | candidates:2 | view:compare"
            )
            app.query_one("#candidate-detail", Static).update(
                render_round_table(
                    _sample_comparison(),
                    1,
                    highlighted_id="a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d",
                    as_of="12:00:00",
                )
            )
            await _capture(app, pilot, SCREENSHOTS[11], rendered, "rooms-tab")
    finally:
        board.close()
    return rendered


def _screen_text(svg: str) -> str:
    """The SVG's complete terminal text, normalized for the two known
    focus/animation variations that carry no documented meaning: the CSS
    ``<style>`` block (focused-vs-unfocused border colors live there, not in
    the text) and the tab-underline half-glyphs (``╸``/``╺`` mark the animated
    highlight edges and shift with focus timing)."""
    root = ElementTree.fromstring(svg)
    parts = [
        element.text
        for element in root.iter()
        if not element.tag.endswith("style") and element.text
    ]
    return _normalize_text("".join(parts))


def _normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    return text.replace("╸", "━").replace("╺", "━")


def _screen_lines(svg: str) -> list[str]:
    """The ``<text>`` elements' text, one string per terminal row, for a
    readable diff. Diagnostics only — the check itself compares
    ``_screen_text``. Rows are keyed by the raw ``y`` attribute in document
    order (Rich exports rows top to bottom), so no coordinate is parsed and a
    malformed one cannot abort the report."""
    rows: dict[str | None, list[str]] = {}
    for element in ElementTree.fromstring(svg).iter():
        if element.tag.endswith("text") and element.text:
            rows.setdefault(element.get("y"), []).append(element.text)
    # Rich ends each row with its own newline; drop exactly that one and
    # spell out any other, so the diff is one line per row and an added
    # newline is still visible.
    return [
        _normalize_text("".join(parts)).removesuffix("\n").replace("\n", "\\n")
        for parts in rows.values()
    ]


# Bounds one screen's diff so a wholesale change cannot flood the CI log.
_MAX_DIFF_LINES = 60


def _screen_diff(name: str, committed: str, rendered: str) -> list[str]:
    diff = list(
        difflib.unified_diff(
            _screen_lines(committed),
            _screen_lines(rendered),
            f"committed/{name}",
            f"rendered/{name}",
            n=1,
            lineterm="",
        )
    )
    if not diff:
        return ["  (the per-row view is identical; the difference does not show per row)"]
    if len(diff) > _MAX_DIFF_LINES:
        omitted = len(diff) - _MAX_DIFF_LINES
        diff = diff[:_MAX_DIFF_LINES] + [f"... ({omitted} more diff lines)"]
    return diff


def _stale_reason(name: str, committed: str | None, rendered: str) -> list[str]:
    """Why ``name`` is stale, as printable lines; empty when it is current."""
    if committed is None:
        return ["  committed file is missing"]
    try:
        committed_text = _screen_text(committed)
        rendered_text = _screen_text(rendered)
    except ElementTree.ParseError as exc:
        return [f"  SVG does not parse: {exc}"]
    # The COMPLETE normalized screen text must match — markers alone
    # would let any drift outside three strings pass CI unnoticed.
    if committed_text != rendered_text:
        return _screen_diff(name, committed, rendered)
    missing = [m for m in SCREEN_MARKERS[name] if m not in rendered_text]
    if missing:
        return ["  rendered text lacks markers: " + ", ".join(missing)]
    return []


def _write_or_check(rendered: dict[str, str], *, check: bool) -> int:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    stale: dict[str, list[str]] = {}
    for name in SCREENSHOTS:
        path = ASSET_DIR / name
        value = rendered[name]
        if check:
            committed = path.read_text(encoding="utf-8") if path.is_file() else None
            reason = _stale_reason(name, committed, value)
            if reason:
                stale[name] = reason
        else:
            path.write_text(value, encoding="utf-8")
    if stale:
        print("TUI guide screenshots are stale: " + ", ".join(stale))
        for name, reason in stale.items():
            print(f"\n{name}:")
            print("\n".join(reason))
        return 1
    print(
        f"{'Verified' if check else 'Rendered'} {len(SCREENSHOTS)} "
        f"TUI guide screenshots in {ASSET_DIR}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Fail unless every committed SVG's complete normalized screen text "
            "matches a fresh deterministic render (focus-only style/underline "
            "variation excluded) and carries each screen's semantic markers."
        ),
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="syncmill-tui-guide-") as raw:
        rendered = asyncio.run(_render_all(Path(raw)))
    return _write_or_check(rendered, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
