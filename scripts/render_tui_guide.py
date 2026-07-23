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
import re
import subprocess
import tempfile
from pathlib import Path
from xml.etree import ElementTree

from textual.widgets import DataTable, Log, OptionList, Static
from textual.widgets.option_list import Option

from syncmill.board import Board
from syncmill.config import SyncmillConfig
from syncmill.tui import SyncmillTuiApp


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
    "10-rooms-output.svg": ("Rooms", "view:output", "o/m diff/output/details"),
    "11-rooms-details.svg": ("Rooms", "view:details", "o/m diff/output/details"),
}


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


async def _capture(
    app: SyncmillTuiApp, pilot, filename: str, rendered: dict[str, str]
) -> None:
    # Let Textual recompute auto-height Static widgets after fixture updates.
    await pilot.pause()
    svg = app.export_screenshot(
        title=f"syncmill TUI guide — {filename.removesuffix('.svg')}",
        simplify=True,
    )
    # Rich assigns a random numeric namespace to every exported SVG. It has no
    # visual meaning. Rich also emits indentation on otherwise blank lines;
    # normalize both so generated assets pass Git's whitespace checks.
    normalized = re.sub(r"terminal-\d+", "terminal-syncmill", svg)
    rendered[filename] = re.sub(r"[ \t]+(?=\n|$)", "", normalized)


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
    app = SyncmillTuiApp(
        config,
        board=board,
        board_error=None,
        toml_path=toml,
        refresh_seconds=3600,
        initial_tab="board",
    )
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
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
            await _capture(app, pilot, SCREENSHOTS[0], rendered)

            await pilot.press("2")
            await pilot.pause()
            _set_options(
                app.query_one("#job-list", OptionList),
                [("job-0001 running selected board-0001", "/sample/job-0001")],
            )
            log = app.query_one("#job-log", Log)
            log.clear()
            log.write_line("[syncmill] Agent: codex (forced, no fallback)")
            log.write_line("[codex] inspecting sample project")
            log.write_line("[codex] writing beginner example...")
            app.query_one("#job-status", Static).update(
                "running | exit:- | cwd:/sample/project"
            )
            await _capture(app, pilot, SCREENSHOTS[1], rendered)

            await pilot.press("v")
            await pilot.pause()
            _set_options(
                app.query_one("#artifact-list", OptionList),
                [("run-0001 pending codex route", "run-0001")],
            )
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
            await _capture(app, pilot, SCREENSHOTS[2], rendered)

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
            await _capture(app, pilot, SCREENSHOTS[3], rendered)

            await pilot.press("3")
            await pilot.pause()
            _set_options(
                app.query_one("#report-list", OptionList),
                [("run-0001 route completed", "run-0001")],
            )
            app.query_one("#report-detail", Static).update(
                "Run: run-0001\nStrategy: route\nStatus: completed\n"
                "Team: starter\nTask: beginner-guide\nAgent: codex\n"
                "Winner: codex | Exit: 0 | Duration: 12.4s\n"
                "Gate: passed | Files changed: 1\n"
                "Artifact: run-0001 | Digest: 0123456789abcdef\n"
                "Preflight: allow | Trace: sample-trace"
            )
            await _capture(app, pilot, SCREENSHOTS[4], rendered)

            await pilot.press("4")
            await pilot.pause()
            await _capture(app, pilot, SCREENSHOTS[5], rendered)

            await pilot.press("5")
            await pilot.pause()
            app.query_one("#memory-status", Static).update(
                "Memory: ready | backend: memtomem | project: sample"
            )
            await _capture(app, pilot, SCREENSHOTS[6], rendered)

            await pilot.press("6")
            await pilot.pause()
            _set_options(
                app.query_one("#session-list", OptionList),
                [("session-01 codex open live", "session-01")],
            )
            app.query_one("#session-detail", Static).update(
                "Session: session-01\nAgent: codex\nState: open | live: True\n"
                "Source: clean snapshot\nWorktree: /sample/workspaces/session-01\n"
                "Checkpoints: run-0001\n\nx attach | c checkpoint | k close"
            )
            app.query_one("#session-status", Static).update(
                "sessions:1 | selected:session-01"
            )
            await _capture(app, pilot, SCREENSHOTS[7], rendered)

            await pilot.press("7")
            await pilot.pause()
            _set_options(
                app.query_one("#room-list", OptionList),
                [("room-0001 completed rounds=1", "room-0001")],
            )
            _set_options(
                app.query_one("#candidate-list", OptionList),
                [("candidate-01 codex completed ✓", "candidate-01")],
            )
            app.query_one("#room-detail", Static).update(
                "Room: room-0001\nState: completed | revision: 3\n"
                "Members: codex\nSelected: candidate-01\nApplied run: -\n\n"
                "Recent events\nround 1 completed\ncandidate selected"
            )
            app.query_one("#room-status", Static).update(
                "rooms:1 | candidates:1 | view:diff"
            )
            app.query_one("#candidate-detail", Static).update(
                "Candidate: candidate-01 | Diff\n"
                "sha256: fedcba9876543210\n\n"
                "diff --git a/room-smoke.txt b/room-smoke.txt\n"
                "new file mode 100644\n--- /dev/null\n+++ b/room-smoke.txt\n"
                "@@ -0,0 +1 @@\n+room candidate"
            )
            await _capture(app, pilot, SCREENSHOTS[8], rendered)

            app.query_one("#room-status", Static).update(
                "rooms:1 | candidates:1 | view:output"
            )
            app.query_one("#candidate-detail", Static).update(
                "Candidate: candidate-01 | Output\n"
                "State: completed | exit: 0 | duration: 18.2s\n"
                "Gate: passed\n\n"
                "Created room-smoke.txt in the isolated candidate worktree."
            )
            await _capture(app, pilot, SCREENSHOTS[9], rendered)

            app.query_one("#room-status", Static).update(
                "rooms:1 | candidates:1 | view:details"
            )
            app.query_one("#candidate-detail", Static).update(
                "Candidate: candidate-01 | Details\n"
                "Member: codex | round: 1 | attempt: 1\n"
                "State: completed | eligible: yes\n"
                "Duration: 18.2s | exit: 0\n"
                "Files: room-smoke.txt\nPatch: 146 bytes\n"
                "sha256: fedcba9876543210 | output truncated: no"
            )
            await _capture(app, pilot, SCREENSHOTS[10], rendered)
    finally:
        board.close()
    return rendered


def _write_or_check(rendered: dict[str, str], *, check: bool) -> int:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    stale: list[str] = []
    for name in SCREENSHOTS:
        path = ASSET_DIR / name
        value = rendered[name]
        if check:
            if not path.is_file():
                stale.append(name)
                continue
            committed = path.read_text(encoding="utf-8")
            try:
                committed_root = ElementTree.fromstring(committed)
                rendered_root = ElementTree.fromstring(value)
            except ElementTree.ParseError:
                stale.append(name)
                continue
            # Focus timing may alter selection borders and cursor glyphs without
            # changing the documented screen. Check screen-specific semantic text
            # in both renders; tests separately validate SVG shape/private data.
            committed_text = "".join(committed_root.itertext()).replace("\xa0", " ")
            rendered_text = "".join(rendered_root.itertext()).replace("\xa0", " ")
            markers = SCREEN_MARKERS[name]
            if any(
                marker not in committed_text or marker not in rendered_text
                for marker in markers
            ):
                stale.append(name)
        else:
            path.write_text(value, encoding="utf-8")
    if stale:
        print("TUI guide screenshots are stale: " + ", ".join(stale))
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
            "Fail unless every committed SVG parses and both the committed and "
            "freshly rendered captures carry each screen's semantic markers "
            "(not a byte-equality comparison)."
        ),
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="syncmill-tui-guide-") as raw:
        rendered = asyncio.run(_render_all(Path(raw)))
    return _write_or_check(rendered, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
