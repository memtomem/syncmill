from __future__ import annotations

import asyncio
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest
import textual.pilot


ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "scripts" / "render_tui_guide.py"
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
    ("12-rooms-compare.svg", "Rooms", "view:compare"),
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


def _underlined_tabs(svg: str) -> list[str]:
    """Tab labels whose highlight underline sits exactly beneath them. The
    highlight is the underline-row segment drawn with full glyphs only (its
    neighbours end in the half glyphs), and it must cover a label exactly: a
    capture taken mid-slide covers none, or the wrong one."""
    rows: dict[float, list[tuple[float, str]]] = {}
    for element in ElementTree.fromstring(svg).iter():
        if element.tag.endswith("text") and element.text and element.text != "\n":
            rows.setdefault(float(element.get("y")), []).append(
                (float(element.get("x")), element.text)
            )
    ys = sorted(rows)
    underlined = []
    for above, y in zip(ys, ys[1:]):
        if not all(set(text) <= set("━╸╺") for _, text in rows[y]):
            continue
        highlights = [(x, text) for x, text in rows[y] if set(text) == {"━"}]
        assert len(highlights) == 1, rows[y]
        x, text = highlights[0]
        labels = [
            label for lx, label in rows[above] if lx == x and len(label) == len(text)
        ]
        assert labels, f"underline at x={x} covers no tab label"
        underlined.extend(labels)
    return underlined


@pytest.mark.parametrize(("filename", "tab", "marker"), SCREENS)
def test_tui_guide_underlines_the_screens_tab(filename, tab, marker):
    # The text comparison ignores which tab is underlined; a capture taken
    # while the underline was still sliding showed it beneath the previous tab.
    expected = ["Runs", tab] if tab in ("Operations", "Artifacts") else [tab]
    svg = (ASSET_DIR / filename).read_text(encoding="utf-8")
    assert _underlined_tabs(svg) == expected


def test_tui_guide_enables_board_before_the_first_success_flow():
    # The first-success walkthrough adds a Board item, but a freshly
    # initialized project leaves [board] disabled (LIMITED tab) — the guide
    # must tell readers to enable it first.
    text = GUIDE.read_text(encoding="utf-8")
    setup = text.index("[board]\nenabled = true")
    assert setup < text.index("## 첫 성공 권장 동선")
    assert "SYNCMILL_BOARD__ENABLED=true" in text


def test_tui_guide_screen_text_matches_a_fresh_deterministic_render():
    # --check compares the COMPLETE normalized screen text of every committed
    # SVG against a fresh render (only focus-timing style/underline variation
    # is excluded), so any content drift fails CI — not just three markers.
    result = subprocess.run(
        [sys.executable, "scripts/render_tui_guide.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Verified 12 TUI guide screenshots" in result.stdout


@pytest.fixture(scope="module")
def renderer():
    spec = importlib.util.spec_from_file_location("_render_tui_guide", RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def committed_copy(renderer, tmp_path, monkeypatch):
    for name in renderer.SCREENSHOTS:
        shutil.copy(ASSET_DIR / name, tmp_path / name)
    monkeypatch.setattr(renderer, "ASSET_DIR", tmp_path)
    return {
        name: (tmp_path / name).read_text(encoding="utf-8")
        for name in renderer.SCREENSHOTS
    }


def test_render_does_not_depend_on_the_idle_wait(renderer, monkeypatch, tmp_path):
    # #192: a starved process looks idle, so on a loaded runner Textual's idle
    # wait returned with a tab-focus or highlight message still queued; it then
    # switched the capture back to the previous tab or overwrote a fixture.
    # Without the wait that happens on every run, not one in thirty.
    async def no_idle_wait(*args, **kwargs):
        await asyncio.sleep(0)

    monkeypatch.setattr(textual.pilot, "wait_for_idle", no_idle_wait)
    rendered = asyncio.run(renderer._render_all(tmp_path))

    stale = {
        name: reason
        for name in renderer.SCREENSHOTS
        if (
            reason := renderer._stale_reason(
                name, (ASSET_DIR / name).read_text(encoding="utf-8"), rendered[name]
            )
        )
    }
    assert not stale, "\n".join(
        f"{name}:\n" + "\n".join(reason) for name, reason in stale.items()
    )


def _diff_rows(lines: list[str], prefix: str) -> list[str]:
    return [
        line[1:]
        for line in lines
        if line.startswith(prefix) and not line.startswith(prefix * 3)
    ]


def test_check_prints_the_changed_row_of_a_stale_screen(
    renderer, committed_copy, capsys
):
    # #192: a CI failure named only the stale screen. The report must show
    # which row changed, so the next occurrence explains itself.
    rendered = dict(committed_copy)
    rendered["06-teams.svg"] = rendered["06-teams.svg"].replace(
        "starter", "stArter", 1
    )

    assert renderer._write_or_check(rendered, check=True) == 1

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "TUI guide screenshots are stale: 06-teams.svg"
    removed = _diff_rows(out, "-")
    added = _diff_rows(out, "+")
    assert len(removed) == 1 and len(added) == 1
    assert "starter" in removed[0]
    assert added[0] == removed[0].replace("starter", "stArter", 1)
    assert "--- committed/06-teams.svg" in out
    assert "+++ rendered/06-teams.svg" in out


def test_check_passes_and_prints_no_diff_when_every_screen_matches(
    renderer, committed_copy, capsys
):
    assert renderer._write_or_check(dict(committed_copy), check=True) == 0
    out = capsys.readouterr().out
    assert out.startswith("Verified 12 TUI guide screenshots")
    assert "---" not in out


def test_check_names_a_missing_committed_screen(
    renderer, committed_copy, tmp_path, capsys
):
    (tmp_path / "07-status.svg").unlink()

    assert renderer._write_or_check(dict(committed_copy), check=True) == 1

    out = capsys.readouterr().out.splitlines()
    assert out[0] == "TUI guide screenshots are stale: 07-status.svg"
    assert out[out.index("07-status.svg:") + 1] == "  committed file is missing"


def test_screen_diff_is_bounded(renderer, committed_copy):
    # A wholesale change (another screen entirely) must not flood the log.
    diff = renderer._screen_diff(
        "06-teams.svg", committed_copy["06-teams.svg"], committed_copy["01-board.svg"]
    )
    assert len(diff) == renderer._MAX_DIFF_LINES + 1
    assert diff[-1].startswith("... (") and diff[-1].endswith(" more diff lines)")


def _stale_report(renderer, committed_copy, capsys, name, edit):
    rendered = dict(committed_copy)
    rendered[name] = edit(rendered[name])
    assert renderer._write_or_check(rendered, check=True) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0] == f"TUI guide screenshots are stale: {name}"
    return out


def test_an_unparseable_row_coordinate_does_not_abort_the_report(
    renderer, committed_copy, capsys
):
    # The row view must never raise: a report that dies prints nothing.
    out = _stale_report(
        renderer, committed_copy, capsys, "06-teams.svg",
        lambda svg: svg.replace('y="27"', 'y="27px"', 1).replace("starter", "stArter", 1),
    )
    added = _diff_rows(out, "+")
    assert len(added) == 1 and "stArter" in added[0]


def test_an_extra_row_newline_shows_in_the_diff(renderer, committed_copy, capsys):
    # Only the row's own trailing newline is dropped; a second one is a
    # real difference and must stay visible.
    out = _stale_report(
        renderer, committed_copy, capsys, "06-teams.svg",
        lambda svg: svg.replace(">\n</text>", ">\n\n</text>", 1),
    )
    removed = _diff_rows(out, "-")
    added = _diff_rows(out, "+")
    assert len(removed) == 1 and added == [removed[0] + "\\n"]


def test_a_difference_outside_the_rows_says_the_row_view_is_identical(
    renderer, committed_copy, capsys
):
    out = _stale_report(
        renderer, committed_copy, capsys, "06-teams.svg",
        lambda svg: svg.replace("<style>", "<desc>hidden</desc><style>", 1),
    )
    assert out[out.index("06-teams.svg:") + 1] == (
        "  (the per-row view is identical; the difference does not show per row)"
    )
