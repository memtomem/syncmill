from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest


ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "tui-guide.ko.md"
ASSET_DIR = ROOT / "docs" / "assets" / "tui"
SCREENS = (
    ("01-board.svg", "Board", "run item"),
    ("02-runs-operations.svg", "Operations", "K force"),
    ("03-runs-artifacts-summary.svg", "Artifacts", "view:summary"),
    ("04-runs-artifacts-diff.svg", "Artifacts", "diff --git"),
    ("05-reports.svg", "Reports", "route completed"),
    ("06-teams.svg", "Teams", "starter"),
    ("07-status.svg", "Status", "backend: memtomem"),
    ("08-sessions.svg", "Sessions", "session-01"),
    ("09-rooms-diff.svg", "Rooms", "room candidate"),
    ("10-rooms-output.svg", "Rooms", "view:output"),
    ("11-rooms-details.svg", "Rooms", "view:details"),
)


@pytest.mark.parametrize(("filename", "tab", "marker"), SCREENS)
def test_tui_guide_svg_is_valid_and_screen_specific(filename, tab, marker):
    path = ASSET_DIR / filename
    root = ElementTree.parse(path).getroot()
    text = "".join(root.itertext()).replace("\xa0", " ")

    assert root.tag.endswith("svg")
    # Pins the COMMITTED assets' 120x40 geometry. A rich/textual upgrade alone
    # cannot trip this; after regenerating the assets with
    # `uv run python scripts/render_tui_guide.py`, a geometry change lands here
    # and this expected viewBox must be updated alongside the SVGs.
    assert root.attrib["viewBox"] == "0 0 1482 1026.0"
    assert tab in text
    assert marker in text
    assert "1-7 tabs" in text

    raw = path.read_text(encoding="utf-8")
    for private in ("/Users/", "/private/", "/tmp/", "API_KEY", "sk-"):
        assert private not in raw
    assert filename in GUIDE.read_text(encoding="utf-8")


def test_tui_guide_markers_exist_in_committed_and_fresh_render():
    result = subprocess.run(
        [sys.executable, "scripts/render_tui_guide.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verified 11 TUI guide screenshots" in result.stdout
