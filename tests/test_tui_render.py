"""What the Textual apps put on the terminal (#210): the render guard, the
error console and markup-off widgets."""

from __future__ import annotations

import ast
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style
from textual.app import App
from rich.color import ColorSystem
from textual.strip import Strip
from textual.widgets import DataTable

from syncmill import tui_render
from syncmill.tui_render import DANGEROUS, PlainStatic, plain, plain_select

SRC = Path(__file__).resolve().parents[1] / "src" / "syncmill"
HOSTILE = "a\x1b]52;c;QQ==\x07 csi\u009b2J bidi\u202eRTL nel\u0085 ls\u2028 del\x7f"


class _Console:
    _color_system = ColorSystem.TRUECOLOR


@pytest.fixture
def guard(monkeypatch):
    """The guard installed for one test; ``Strip.render`` restored after."""
    monkeypatch.setattr(Strip, "render", Strip.render)
    original = Strip.render
    tui_render.install_render_guard()
    return original


def _render(strip: Strip) -> str:
    return strip.render(_Console())


def _original(original_render, strip: Strip) -> str:
    """What Textual's own ``Strip.render`` emits for ``strip``, on a fresh
    strip: a strip caches its rendering, so reusing one would hand back the
    guard's output."""
    return original_render(Strip(list(strip)), _Console())


def _translated(text: str) -> str:
    return "".join("\u200b" if ch in DANGEROUS else ch for ch in text)


# ------------------------------------------------------------ render guard


def test_an_unstyled_segment_is_rendered_with_each_dangerous_character_replaced(guard):
    out = _render(Strip([Segment(HOSTILE)]))
    assert out == _translated(HOSTILE)
    assert not any(ch in DANGEROUS for ch in out)


def test_a_styled_segment_keeps_its_sgr_around_the_replaced_text(guard):
    style = Style(bold=True, color="red")
    out = _render(Strip([Segment(HOSTILE, style)]))
    assert out == guard(Strip([Segment(_translated(HOSTILE), style)]), _Console())
    assert out.startswith("\x1b[") and _translated(HOSTILE) in out


def test_text_without_dangerous_characters_renders_as_textual_does(guard):
    segments = [Segment("plain 첫 실행 \U0001F468\u200d\U0001F469", Style(italic=True)),
                Segment(" tail")]
    assert _render(Strip(segments)) == guard(Strip(segments), _Console())


def test_a_link_with_an_unsafe_url_is_dropped_and_a_printable_one_kept(guard):
    unsafe = _render(Strip([Segment("x", Style(link="x\u009d52;c;QQ==\u009c"))]))
    assert "\x1b]8" not in unsafe and "\u009d" not in unsafe
    safe = _render(Strip([Segment("x", Style(link="https://example.com/a"))]))
    assert "\x1b]8;" in safe and "https://example.com/a" in safe


def test_installing_twice_wraps_once(guard):
    installed = Strip.render
    tui_render.install_render_guard()
    assert Strip.render is installed


_WIDTH_CASES = ["a\u200d\x1bb", "a\u200d\x1b\u200db", "a\u200d\x1bb\u200d", "\r\n",
                "x\u0301\x1b\u0301", "a\u061cb", "\u202e\u200d\U0001F468"]


@pytest.mark.parametrize("text", _WIDTH_CASES)
def test_the_guard_keeps_the_cell_width_where_deletion_would_not(guard, text):
    assert cell_len(_render(Strip([Segment(text)]))) == cell_len(text)


def test_the_guard_keeps_the_cell_width_of_random_text(guard):
    # The property: what the guard emits is exactly as wide as what Textual
    # would have emitted. (Textual's own per-segment widths can disagree with
    # the whole line's when a ZWJ meets a segment boundary; that is not the
    # guard's to change.)
    alphabet = ["a", " ", "가", "中", "\u0301", "\u200d", "\u200c", "\ufe0f", "\ufeff",
                "\U0001F468", "\u00ad", *sorted(DANGEROUS)]
    rng = random.Random(210)
    for _ in range(20000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8)))
        cut = rng.randint(0, len(text))
        strip = Strip([Segment(text[:cut]), Segment(text[cut:])])
        assert cell_len(_render(strip)) == cell_len(_original(guard, strip)), ascii(text)


def test_the_guard_keeps_the_width_across_segments(guard):
    strip = Strip([Segment("a\u200d"), Segment("\x1b"), Segment("b")])
    assert cell_len(_render(strip)) == cell_len("a\u200d\x1bb")


@pytest.mark.parametrize("text", _WIDTH_CASES)
def test_the_guard_keeps_the_width_of_a_cropped_and_padded_strip(guard, text):
    strip = Strip([Segment("pre "), Segment(text), Segment(" post")])
    for cells in range(1, strip.cell_length + 3):
        cropped = strip.crop_pad(strip.cell_length, 0, 2, None).crop(0, cells)
        assert cell_len(_render(cropped)) == cell_len(_original(guard, cropped)), (
            ascii(text), cells,
        )


def test_an_old_unicode_table_gets_a_same_width_replacement():
    # Under rich's Unicode 6.2 table U+061C is unassigned and one cell wide, so
    # U+200B (zero) would shift the line; U+FFFD is one cell too.
    probe = (
        "from rich.cells import cell_len\n"
        "from syncmill.tui_render import _substitutions\n"
        "table = _substitutions()\n"
        "assert table[0x61c] == '\\ufffd', table[0x61c]\n"
        "assert table[0x1b] == '\\u200b'\n"
        "text = 'a\\u061cb'\n"
        "assert cell_len(text.translate(table)) == cell_len(text) == 3\n"
    )
    env = {**os.environ, "UNICODE_VERSION": "6.2.0"}
    result = subprocess.run([sys.executable, "-c", probe], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------ error console


def test_the_error_console_escapes_and_writes_no_colour(capsys):
    console = tui_render.error_console()
    console.print(HOSTILE)
    err = capsys.readouterr().err
    assert "\\x1b]52;c;QQ==" in err and "\\x9b2J" in err and "\\u202e" in err
    assert not any(ch in err for ch in ("\x1b", "\u009b", "\u202e", "\x07"))


def test_both_apps_use_the_escaping_error_console(tmp_path):
    from syncmill.board import Board
    from syncmill.config import SyncmillConfig
    from syncmill.dashboard import DashboardApp
    from syncmill.tui import SyncmillTuiApp

    board = Board(tmp_path / "board.db")
    try:
        apps = [DashboardApp(board),
                SyncmillTuiApp(SyncmillConfig(), board=None, board_error=None,
                               toml_path=tmp_path / "syncmill.toml")]
        for app in apps:
            assert app.error_console.file.__class__.__name__ == "EscapedStderr"
    finally:
        board.close()


@pytest.mark.parametrize("entry", ["tui", "dashboard"])
def test_the_entry_points_install_the_guard_before_running(monkeypatch, tmp_path, entry):
    from syncmill import dashboard, tui

    calls = []
    monkeypatch.setattr(Strip, "render", Strip.render)
    monkeypatch.setattr(tui_render, "install_render_guard", lambda: calls.append("guard"))
    monkeypatch.setattr(App, "run", lambda self, **kw: calls.append("run"))
    if entry == "tui":
        from syncmill.config import SyncmillConfig

        monkeypatch.setattr(tui, "install_render_guard", tui_render.install_render_guard)
        tui.run_tui(SyncmillConfig(), board=None, board_error=None,
                    toml_path=tmp_path / "syncmill.toml")
    else:
        from syncmill.board import Board

        monkeypatch.setattr(dashboard, "install_render_guard",
                            tui_render.install_render_guard)
        board = Board(tmp_path / "board.db")
        try:
            dashboard.run_dashboard(board)
        finally:
            board.close()
    assert calls == ["guard", "run"]


# ------------------------------------------------------------------ markup


class _Probe(App):
    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self):
        yield PlainStatic(self.text, id="static")
        table = DataTable(id="table")
        yield table
        yield plain_select([(self.text, "v")], value="v", allow_blank=False, id="select")

    def on_mount(self):
        table = self.query_one("#table", DataTable)
        table.add_columns(plain("c"))
        table.add_row(plain(self.text))


def _visible(widget) -> str:
    return "".join(seg.text for y in range(widget.size.height)
                   for seg in widget.render_line(y))


@pytest.mark.parametrize("text", ["[/b] closing", "[link=https://evil.example]x[/link]",
                                  "codex [enabled]"])
def test_markup_in_stored_text_is_shown_literally(text):
    async def scenario():
        app = _Probe(text)
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.pause()
            assert text in _visible(app.query_one("#static"))
            assert text in _visible(app.query_one("#table"))
            select = app.query_one("#select")
            assert text in _visible(select.query_one("SelectCurrent #label"))
            select.expanded = True
            await pilot.pause()
            assert text in _visible(select.query_one("SelectOverlay"))

    import asyncio

    asyncio.run(scenario())


def test_the_apps_notify_without_markup_by_default(tmp_path):
    from syncmill.board import Board
    from syncmill.dashboard import DashboardApp

    async def scenario():
        board = Board(tmp_path / "board.db")
        try:
            app = DashboardApp(board, refresh_seconds=3600)
            # With markup on, the toast's MarkupError escaped the layout and
            # ended the app.
            async with app.run_test(notifications=True) as pilot:
                app.notify("failed: [/b] in a provider error")
                for _ in range(3):
                    await pilot.pause(0.05)
                assert app.is_running
        finally:
            board.close()

    import asyncio

    asyncio.run(scenario())


# ------------------------------------------------------------------- guard

_TEXTUAL_MODULES = ("tui.py", "tui_collaboration.py", "dashboard.py")
_APPS = {"SyncmillTuiApp", "DashboardApp"}


def markup_sinks(source: str, path: str) -> list[str]:
    """Places in a Textual module where text could be rendered as markup:
    a ``Static``/``Label``/``Select`` built directly (the plain wrappers and
    ``plain_select`` exist for that); an ``OptionList`` without
    ``markup=False``; a DataTable row or column not wrapped in ``plain``;
    ``notify`` with ``markup=True`` or called on something other than the app
    (``Widget.notify`` passes ``markup=True`` itself)."""
    tree = ast.parse(source)
    found = []

    def keyword(call: ast.Call, name: str):
        return next((k.value for k in call.keywords if k.arg == name), None)

    def is_plain(node: ast.AST) -> bool:
        if isinstance(node, ast.Starred):
            node = node.value
            return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "map" and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "plain")
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "plain")

    for cls in [None, *[n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]]:
        nodes = ast.walk(tree) if cls is None else ast.walk(cls)
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            where = f"{path}:{node.lineno}"
            func = node.func
            if cls is None and isinstance(func, ast.Name):
                if func.id in ("Static", "Label", "Select"):
                    found.append(f"{where} {func.id}(")
                if func.id == "OptionList":
                    value = keyword(node, "markup")
                    if not (isinstance(value, ast.Constant) and value.value is False):
                        found.append(f"{where} OptionList without markup=False")
            if cls is None and isinstance(func, ast.Attribute) and func.attr in (
                "add_row", "add_rows", "add_column", "add_columns", "update_cell",
            ):
                if not all(is_plain(arg) for arg in node.args):
                    found.append(f"{where} {func.attr} without plain")
            if cls is not None and isinstance(func, ast.Attribute) and func.attr == "notify":
                receiver = ast.unparse(func.value)
                if receiver == "super()":  # the override itself
                    continue
                value = keyword(node, "markup")
                if value is not None and not (
                    isinstance(value, ast.Constant) and value.value is False
                ):
                    found.append(f"{where} notify markup")
                if not (receiver == "self.app" or (receiver == "self" and cls.name in _APPS)):
                    found.append(f"{where} {receiver}.notify")
    return sorted(set(found))


def test_textual_modules_render_no_text_as_markup():
    found = []
    for name in _TEXTUAL_MODULES:
        path = SRC / name
        found += markup_sinks(path.read_text(encoding="utf-8"), f"src/syncmill/{name}")
    assert found == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Static('x')\n", "m.py:1 Static("),
        ("Label('x')\n", "m.py:1 Label("),
        ("Select([])\n", "m.py:1 Select("),
        ("OptionList(id='a')\n", "m.py:1 OptionList without markup=False"),
        ("t.add_row(name)\n", "m.py:1 add_row without plain"),
        ("t.add_columns('a', 'b')\n", "m.py:1 add_columns without plain"),
        ("class S:\n    def f(self):\n        self.notify('x')\n", "m.py:3 self.notify"),
        ("class SyncmillTuiApp:\n    def f(self):\n        self.notify('x', markup=True)\n",
         "m.py:3 notify markup"),
    ],
)
def test_the_markup_guard_reports_each_sink(source, expected):
    assert expected in markup_sinks(source, "m.py")


def test_the_markup_guard_allows_the_plain_forms():
    source = (
        "PlainStatic('x')\nplain_select([], id='s')\nOptionList(id='a', markup=False)\n"
        "t.add_row(*map(plain, cells), key=k)\nt.add_columns(plain('a'))\n"
        "class DashboardApp:\n    def f(self):\n        self.notify('x')\n"
        "        super().notify('x', markup=markup)\n"
        "class M:\n    def f(self):\n        self.app.notify('x')\n"
    )
    assert markup_sinks(source, "m.py") == []
