from __future__ import annotations

import json
import sys

import pytest

from syncmill.artifacts import printable_name, terminal_json

HOSTILE = (
    [chr(c) for c in range(0x80, 0xA0)]  # C1, including U+009B (CSI)
    + ["\x7f", "\u00ad", "\u061c", "\u200e", "\u200f", "\u2028", "\u2029", "\ufeff"]
    + [chr(c) for c in range(0x202A, 0x202F)]  # bidi embeddings and overrides
    + [chr(c) for c in range(0x2066, 0x206A)]  # bidi isolates
    + ["\U000e0001", "\udc80"]  # non-BMP format character, lone surrogate
)


def _raw_non_printables(text: str) -> list[str]:
    return [ch for ch in text if ch != "\n" and not ch.isprintable()]


@pytest.mark.parametrize("ch", HOSTILE, ids=lambda ch: f"U+{ord(ch):04X}")
def test_a_non_printable_is_escaped_and_decodes_back(ch):
    value = {f"k{ch}": [f"v{ch}", {"nested": ch}]}
    text = terminal_json(value)
    assert _raw_non_printables(text) == []
    assert json.loads(text) == value


def test_printable_non_ascii_stays_literal():
    value = {"prompt": "첫 실행 예시를 추가해 줘", "emoji": "✓ 🚀", "path": "a/é.txt"}
    text = terminal_json(value)
    assert "첫 실행 예시를 추가해 줘" in text and "✓ 🚀" in text and "a/é.txt" in text
    assert json.loads(text) == value


def test_escapes_cover_exactly_what_printable_name_escapes():
    for ch in HOSTILE:
        assert printable_name(ch) != ch
        assert ch not in terminal_json(ch)
    assert terminal_json("\u009b") == '"\\u009b"'
    assert terminal_json("\U000e0001") == '"\\udb40\\udc01"'


def test_c0_controls_keep_the_json_escapes():
    value = "a\x1b[2J\nb\tc\x00"
    assert terminal_json(value) == json.dumps(value)


def test_layout_matches_json_dumps_for_printable_values():
    value = {"b": [1, 2.5, None, True], "a": {"x": "y"}}
    assert terminal_json(value) == json.dumps(value, indent=2)
    assert terminal_json(value, sort_keys=True) == json.dumps(
        value, indent=2, sort_keys=True
    )
    assert terminal_json(value, indent=0) == json.dumps(value, indent=0)


def test_every_code_point_round_trips_as_value_and_key():
    every = [chr(c) for c in range(sys.maxunicode + 1)]
    values = terminal_json(every, indent=0)
    keys = terminal_json(dict.fromkeys(every, 0), indent=0)
    assert _raw_non_printables(values) == [] and _raw_non_printables(keys) == []
    assert json.loads(values) == every
    assert json.loads(keys) == dict.fromkeys(every, 0)


def test_adjacent_surrogates_decode_as_ensure_ascii_output_does():
    # Two lone code points that form a pair when escaped. The stores cannot
    # produce one (json.loads already joins an escaped pair, SQLite text is
    # UTF-8, surrogateescape yields low surrogates only), and the stdlib default
    # joins them too; the helper follows it rather than inventing another rule.
    pair = chr(0xD800) + chr(0xDC00)
    for value in (pair, {pair: 1, "\U00010000": 2}):
        assert json.loads(terminal_json(value)) == json.loads(json.dumps(value))


@pytest.mark.parametrize(
    ("indent", "error"), [("\t", TypeError), (True, TypeError), (None, TypeError), (-1, ValueError)]
)
def test_an_indent_that_could_put_controls_outside_strings_is_rejected(indent, error):
    with pytest.raises(error):
        terminal_json({"a": 1}, indent=indent)
