from __future__ import annotations

import ast
import io
import json
import logging
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from syncmill import terminal
from syncmill.artifacts import printable_name, terminal_json
from syncmill.terminal import echo, secho

SRC = Path(__file__).resolve().parents[1] / "src" / "syncmill"
HOSTILE = "a\u009b2J\x1b]52;c;QQ==\x07\u202eb\rc 첫 실행"
RAW = ("\u009b", "\x1b]52", "\x07", "\u202e", "\r")


def _run(fn, **invoke) -> click.testing.Result:
    @click.command()
    def show():
        fn()

    return CliRunner().invoke(show, **invoke)


def _safe(text: str) -> bool:
    return not any(raw in text for raw in RAW)


# ------------------------------------------------------------------ echo


def test_echo_escapes_controls_and_keeps_lines_tabs_and_korean():
    out = _run(lambda: echo("one\n\t" + HOSTILE)).output
    assert _safe(out)
    assert out == "one\n\t" + printable_name(HOSTILE) + "\n"


def test_secho_styles_the_escaped_text():
    out = _run(lambda: secho(HOSTILE, fg="red"), color=True).output
    assert out == click.style(printable_name(HOSTILE), fg="red") + "\n"
    assert _safe(out.replace("\x1b[31m", "").replace("\x1b[0m", ""))


def test_keyword_arguments_pass_through():
    res = _run(lambda: (echo("to-err" + HOSTILE, err=True), echo("no-newline", nl=False)))
    assert res.stdout == "no-newline"
    assert res.stderr == "to-err" + printable_name(HOSTILE) + "\n"


def test_non_text_messages_are_untouched():
    assert _run(lambda: (echo(), echo(None))).output == "\n\n"


def test_bytes_and_objects_are_escaped_too(tmp_path):
    raw = HOSTILE.encode() + b" \xff"
    out = _run(lambda: (echo(raw), echo(tmp_path / ("p" + HOSTILE)))).output
    assert _safe(out), ascii(out)
    assert out.splitlines()[0] == printable_name(HOSTILE) + " \\xff"
    assert printable_name("p" + HOSTILE) in out.splitlines()[1]


def test_escaping_is_idempotent_on_already_escaped_output():
    value = {"name": HOSTILE, "ko": "첫 실행"}
    for text in (printable_name(HOSTILE), terminal_json(value)):
        assert _run(lambda t=text: echo(t)).output == text + "\n"


def test_echo_raw_writes_the_message_unchanged():
    assert _run(lambda: terminal.echo_raw(HOSTILE, nl=False)).output == HOSTILE


# --------------------------------------------------- logging and excepthook

_PROBE = r"""
import logging, sys
from syncmill.terminal import install_console_guards
install_console_guards()
install_console_guards()
hostile = "x\u009b2J\x1b]52;c;QQ==\x07\u202ey"
log = logging.getLogger("probe.no.handlers")
log.info("info is below the threshold " + hostile)
try:
    raise ValueError("inner " + hostile)
except ValueError:
    log.warning("warned " + hostile, exc_info=True)
raise RuntimeError("uncaught " + hostile)
"""


def test_console_logging_and_uncaught_exceptions_are_escaped():
    # A real interpreter: pytest installs its own handlers, so lastResort and
    # sys.excepthook are only exercised outside it.
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, timeout=60
    )
    err = proc.stderr.decode("utf-8")
    assert proc.returncode == 1
    assert _safe(err), ascii(err)
    assert "info is below the threshold" not in err
    assert "warned x\\x9b2J\\x1b]52;c;QQ==\\x07\\u202ey" in err
    assert "ValueError: inner x\\x9b2J" in err  # the logged traceback
    assert "RuntimeError: uncaught x\\x9b2J" in err  # the excepthook
    assert err.count("Traceback (most recent call last)") == 2


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(logging, "_logRecordFactory", logging.getLogRecordFactory())
    terminal.install_console_guards()
    terminal.install_console_guards()  # idempotent


def _hostile_error() -> ValueError:
    try:
        raise ValueError("inner " + HOSTILE)
    except ValueError as exc:
        return exc


def test_records_are_escaped_for_any_handler(guarded):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    log = logging.getLogger("syncmill.test.terminal.plain")
    log.addHandler(handler)
    log.propagate = False
    try:
        exc = _hostile_error()
        log.warning("warned %s", HOSTILE, exc_info=(type(exc), exc, exc.__traceback__))
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    assert _safe(out), ascii(out)
    assert "warned " + printable_name(HOSTILE) in out
    assert "ValueError: inner " in out and "Traceback (most recent call last)" in out


def test_a_rich_traceback_handler_prints_the_escaped_record(guarded):
    # What `serve` gets: the MCP server installs RichHandler(rich_tracebacks=True),
    # which would render exc_info itself.
    from rich.console import Console
    from rich.logging import RichHandler

    stream = io.StringIO()
    handler = RichHandler(console=Console(file=stream, width=200), rich_tracebacks=True)
    log = logging.getLogger("syncmill.test.terminal.rich")
    log.addHandler(handler)
    log.propagate = False
    try:
        exc = _hostile_error()
        log.warning("warned " + HOSTILE, exc_info=(type(exc), exc, exc.__traceback__))
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    assert _safe(out), ascii(out)
    assert "\\x9b2J" in out


def test_a_bad_format_string_does_not_raise_from_the_logging_call(guarded):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    log = logging.getLogger("syncmill.test.terminal.badfmt")
    log.addHandler(handler)
    log.propagate = False
    try:
        log.warning("two %s %s", "only-one" + HOSTILE)
    finally:
        log.removeHandler(handler)
    assert "two %s %s" in stream.getvalue() and _safe(stream.getvalue())


def test_argparse_output_is_escaped(capsys):
    parser = terminal.EscapingArgumentParser(prog="worker")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run").add_argument("--job-dir", required=True)
    for argv in (["--bad" + HOSTILE], ["run", "--job-dir", "x", "--bad" + HOSTILE]):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
        err = capsys.readouterr().err
        assert _safe(err), ascii(err)
        assert "error:" in err


# -------------------------------------------------------------- the guard

_TERMINAL_WRITERS = {
    "click.echo", "click.secho", "click.utils.echo", "click.termui.secho",
    "sys.stdout.write", "sys.stderr.write", "sys.stdout.buffer.write",
    "sys.stderr.buffer.write", "builtins.print",
}
_RAW_ALLOWED = {("cli.py", "diff_run"), ("cli.py", "room_diff")}


def _aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> the dotted name it was imported as, so ``from click import
    echo as e`` or ``import sys as s`` resolve to what they are."""
    names = {"print": "builtins.print"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _resolve(node: ast.AST, names: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return names.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_resolve(node.value, names)}.{node.attr}"
    return "?"


def terminal_writes(source: str, filename: str) -> list[str]:
    """Calls that reach the terminal without the escaping wrappers, resolving
    import aliases; ``echo_raw`` only inside the allowlisted functions."""
    tree = ast.parse(source)
    names = _aliases(tree)
    owner: dict[int, str] = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                owner.setdefault(id(node), func.name)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _resolve(node.func, names)
        if target in _TERMINAL_WRITERS:
            found.add(f"{filename}:{node.lineno} {target}")
        elif target == "syncmill.terminal.echo_raw":
            if (filename, owner.get(id(node), "<module>")) not in _RAW_ALLOWED:
                found.add(f"{filename}:{node.lineno} echo_raw")
    return sorted(found)


def test_nothing_outside_terminal_py_writes_to_the_terminal_directly():
    found = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "terminal.py":
            continue
        found += terminal_writes(path.read_text(encoding="utf-8"), path.name)
    assert found == []


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("import click\ndef f(x):\n    click.echo(x)\n", "click.echo"),
        ("import click as c\ndef f(x):\n    c.secho(x)\n", "click.secho"),
        ("from click import echo\ndef f(x):\n    echo(x)\n", "click.echo"),
        ("from click import echo as say\ndef f(x):\n    say(x)\n", "click.echo"),
        ("def f(x):\n    print(x)\n", "builtins.print"),
        ("import sys\ndef f(x):\n    sys.stderr.write(x)\n", "sys.stderr.write"),
        ("import sys as s\ndef f(x):\n    s.stdout.buffer.write(x)\n", "sys.stdout.buffer.write"),
        ("from sys import stderr\ndef f(x):\n    stderr.write(x)\n", "sys.stderr.write"),
    ],
)
def test_the_guard_reports_a_direct_write(source, target):
    line = source.rstrip("\n").count("\n") + 1  # the call is the last line
    assert terminal_writes(source, "m.py") == [f"m.py:{line} {target}"]


def test_the_wrappers_are_not_reported():
    source = "from syncmill.terminal import echo, secho\ndef f(x):\n    echo(x)\n    secho(x)\n"
    assert terminal_writes(source, "m.py") == []


def test_the_guard_limits_echo_raw_to_the_patch_commands():
    source = (
        "from syncmill.terminal import echo_raw as raw\n"
        "def diff_run():\n    raw(1)\n\n"
        "def other():\n    raw(2)\n"
    )
    assert terminal_writes(source, "cli.py") == ["cli.py:6 echo_raw"]


# ------------------------------------------------------- end to end, cli


def test_a_listing_with_hostile_stored_fields_is_escaped(monkeypatch, tmp_path):
    from syncmill.cli import cli

    runs = tmp_path / "reports"
    run_dir = runs / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "envelope.json").write_text(json.dumps({
        "kind": "syncmill.run-envelope", "run_id": "run-1", "created_at": "now",
        "strategy": "route" + HOSTILE,
        "result": {"status": "completed", "agent_id": "codex" + HOSTILE},
    }))
    monkeypatch.setenv("SYNCMILL_INTEGRATION__REPORT__RUNS_DIR", str(runs))
    res = CliRunner().invoke(cli, ["report", "list"], color=True)
    assert res.exit_code == 0, res.output
    assert "run-1" in res.output
    assert _safe(res.output), ascii(res.output)


# --------------------------------------------------------- source hygiene


def test_source_files_hold_no_raw_control_or_bidi_characters():
    # A raw U+202E or C1 control in source displays differently from how it
    # runs ("Trojan Source"), and a test meant to cover one is easy to write
    # with a different character than intended. Spell them as escapes.
    root = Path(__file__).resolve().parents[1]
    found = []
    for directory in ("src", "tests", "scripts"):
        for path in sorted((root / directory).rglob("*.py")):
            for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
                bad = sorted({f"U+{ord(c):04X}" for c in line if not c.isprintable() and c != "\t"})
                if bad:
                    found.append(f"{path.relative_to(root)}:{number} {' '.join(bad)}")
    assert found == []

