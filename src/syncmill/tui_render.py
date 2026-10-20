"""What the Textual apps (``tui``, ``dashboard``) put on the terminal (#210).

Textual removes only BEL, BS, VT, FF and CR from text it displays; ESC, C1
controls (U+009B is CSI) and bidi overrides reach the terminal, so stored or
agent-authored text could write the clipboard (OSC 52), clear the screen or
reorder what a line appears to say. Three pieces close that:

- ``install_render_guard`` replaces ``textual.strip.Strip.render``, through which
  every compositor update turns widgets into terminal bytes. Each dangerous
  character in a segment becomes a character of the same cell width that a
  terminal does not interpret: U+200B, or U+FFFD where an old rich Unicode table
  (``UNICODE_VERSION``) gives the character a width. Layout was computed before
  this point, so a replacement must not change any width; deleting would (a
  ZWJ next to a removed ESC joins a different grapheme). A link whose URL has
  such a character is dropped.
- ``error_console`` replaces ``App.error_console``, which prints fatal errors
  outside ``Strip.render``; its output is escaped and uncoloured.
- ``PlainStatic``/``PlainLabel`` and ``plain`` keep markup off for text syncmill
  did not write: Textual parses ``[link=...]`` and ``[/b]`` in a string, and a
  malformed tag raises while the widget renders.

The guard drops characters silently; showing them as escapes, as the CLI does,
is the job of the renderer that builds the text.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text
from textual.widgets import Label, Select, Static

from syncmill.artifacts import printable_name
from syncmill.terminal import EscapedStderr

_BIDI = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
DANGEROUS = frozenset(
    [chr(c) for c in range(0x20)] + [chr(c) for c in range(0x7F, 0xA0)]
    + list(_BIDI) + ["\u2028", "\u2029"]
)


def _substitutions() -> dict[int, str]:
    """Each dangerous character's same-width replacement, under the rich
    Unicode table this process uses (read once, like rich itself)."""
    table = {}
    for ch in DANGEROUS:
        width = cell_len(ch)
        if width == cell_len("\u200b"):
            table[ord(ch)] = "\u200b"
        elif width == cell_len("\ufffd"):
            table[ord(ch)] = "\ufffd"
        else:
            raise RuntimeError(f"no same-width replacement for U+{ord(ch):04X}")
    return table


def _safe_link(url: str) -> bool:
    return printable_name(url) == url


def install_render_guard() -> None:
    """Replace ``Strip.render`` with the guarded version, once per process.

    Same output as Textual's for text without dangerous characters, same
    caching; called by the apps' entry points before ``App.run``, never at
    import."""
    from rich.color import ColorSystem
    from textual.strip import Strip

    if getattr(Strip.render, "_syncmill_guard", False):
        return
    table = _substitutions()

    def render(self: Strip, console: Console) -> str:
        if self._render_cache is None:
            color_system = console._color_system or ColorSystem.TRUECOLOR
            render_style = self.render_style
            parts = []
            for text, style, _ in self._segments:
                text = text.translate(table)
                if style is None:
                    parts.append(text)
                    continue
                if style.link is not None and not _safe_link(style.link):
                    style = style.update_link(None)
                parts.append(render_style(style, text, color_system=color_system))
            self._render_cache = "".join(parts)
        return self._render_cache

    render._syncmill_guard = True
    Strip.render = render


def error_console() -> Console:
    """A console for ``App.error_console``: Textual prints fatal tracebacks and
    exit messages through it, not through ``Strip.render``. No colour, so rich
    writes no escape sequence of its own, and everything is escaped."""
    return Console(
        file=EscapedStderr(), markup=False, highlight=False, color_system=None
    )


def plain(value: object) -> Text:
    """``value`` as plain text for a widget that parses markup in a ``str``
    (DataTable cells and column labels, Select labels)."""
    return Text(str(value))


class PlainStatic(Static):
    """A ``Static`` that never parses markup: what it shows is often stored text."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs["markup"] = False
        super().__init__(*args, **kwargs)


class PlainLabel(Label):
    """A ``Label`` that never parses markup."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs["markup"] = False
        super().__init__(*args, **kwargs)


def plain_select(options, **kwargs) -> Select:
    """A ``Select`` whose option labels are plain text."""
    return Select([(plain(label), value) for label, value in options], **kwargs)
