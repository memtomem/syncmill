from __future__ import annotations

import ast
import io
import json
import logging
import subprocess
import sys
import threading
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
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(logging.Logger, "makeRecord", logging.Logger.makeRecord)
    monkeypatch.setattr(logging.Handler, "format", logging.Handler.format)
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

_THREAD_PROBE = r"""
import threading
from syncmill.terminal import install_console_guards
install_console_guards()
def boom():
    raise RuntimeError("t\u009b2J\x1b]52;c;QQ\x07\u202ey")
thread = threading.Thread(target=boom, name="w\u009bname")
thread.start()
thread.join()
"""


def test_a_worker_thread_exception_is_escaped_with_its_name():
    proc = subprocess.run([sys.executable, "-c", _THREAD_PROBE], capture_output=True, timeout=60)
    err = proc.stderr.decode("utf-8")
    assert _safe(err), ascii(err)
    assert "Exception in thread w\\x9bname:" in err
    assert "RuntimeError: t\\x9b2J" in err


def test_a_factory_installed_before_the_guards_keeps_its_fields(monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(logging.Logger, "makeRecord", logging.Logger.makeRecord)
    monkeypatch.setattr(logging.Handler, "format", logging.Handler.format)
    monkeypatch.setattr(logging, "_logRecordFactory", logging.getLogRecordFactory())
    previous = logging.getLogRecordFactory()

    def with_request_id(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.request_id = "req-7"
        return record

    logging.setLogRecordFactory(with_request_id)
    terminal.install_console_guards()
    terminal.install_console_guards()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(request_id)s %(message)s"))
    log = logging.getLogger("syncmill.test.terminal.chain")
    log.addHandler(handler)
    log.propagate = False
    try:
        log.warning("hello " + HOSTILE)
    finally:
        log.removeHandler(handler)
    assert stream.getvalue() == "req-7 hello " + printable_name(HOSTILE) + "\n"


def test_reinstalling_over_a_wrapper_of_the_guard_does_not_recurse(monkeypatch):
    # cli() installs the guards on every invocation; a makeRecord wrapper
    # installed in between that wraps the escaping one must not end up
    # calling itself.
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    monkeypatch.setattr(logging.Logger, "makeRecord", logging.Logger.makeRecord)
    monkeypatch.setattr(logging.Handler, "format", logging.Handler.format)
    terminal.install_console_guards()
    escaping = logging.Logger.makeRecord

    def tagging(self, *args, **kwargs):
        record = escaping(self, *args, **kwargs)
        record.tag = "t"
        return record

    monkeypatch.setattr(logging.Logger, "makeRecord", tagging)
    terminal.install_console_guards()
    record = logging.getLogger("syncmill.test.terminal.reinstall").makeRecord(
        "n", logging.WARNING, "f", 1, "m %s", (HOSTILE,), None
    )
    assert record.tag == "t" and record.args is None
    assert logging.StreamHandler().format(record) == "m " + printable_name(HOSTILE)


def test_every_field_a_formatter_can_print_is_escaped(guarded):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logging.Formatter("%(name)s|%(threadName)s|%(funcName)s|%(user)s|%(message)s")
    )
    log = logging.getLogger("syncmill.test.name" + HOSTILE)
    log.addHandler(handler)
    log.propagate = False
    thread = threading.current_thread()
    original = thread.name
    thread.name = "thread" + HOSTILE
    try:
        log.warning("m", extra={"user": "u" + HOSTILE})
    finally:
        thread.name = original
        log.removeHandler(handler)
    out = stream.getvalue()
    assert _safe(out), ascii(out)
    assert out.count(printable_name(HOSTILE)) == 3  # logger name, thread, extra


def test_a_non_string_extra_and_a_filter_cannot_bypass_the_guard(guarded, tmp_path):
    class Append(logging.Filter):
        def filter(self, record):
            record.msg = f"{record.msg} +{HOSTILE}"
            return True

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(file)s %(message)s"))
    handler.addFilter(Append())
    log = logging.getLogger("syncmill.test.terminal.late")
    log.addHandler(handler)
    log.propagate = False
    try:
        log.warning("m", extra={"file": tmp_path / ("p" + HOSTILE)})
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    assert _safe(out), ascii(out)
    assert out.count(printable_name(HOSTILE)) == 2  # the Path and the filter's text


def test_rich_metadata_drawn_from_the_record_is_escaped(guarded):
    # Rich renders the path column from record.pathname itself, after format().
    from rich.console import Console
    from rich.logging import RichHandler

    stream = io.StringIO()
    handler = RichHandler(console=Console(file=stream, width=300), show_path=True)
    log = logging.getLogger("syncmill.test.terminal.richpath")
    record = log.makeRecord(
        "n", logging.WARNING, "/tmp/x" + HOSTILE + ".py", 7, "m", None, None
    )
    handler.handle(record)
    out = stream.getvalue()
    assert _safe(out), ascii(out)


# The guard is deliberately stricter than "no call to click.echo": tracking what
# a name refers to through imports and scopes is where a scanner goes wrong, so
# outside terminal.py the ways to reach a writer are not allowed at all.
_TERMINAL_MODULE = "src/syncmill/terminal.py"
_RAW_MODULE, _RAW_FUNCTIONS = "src/syncmill/cli.py", {"diff_run", "room_diff"}
_CLICK_WRITERS = {"echo", "secho", "echo_via_pager", "termui", "utils",
                  "get_text_stream", "get_binary_stream"}
_STREAMS = {"stdout", "stderr", "__stdout__", "__stderr__"}
_STREAM_QUERIES = {"isatty", "fileno", "encoding"}


def terminal_writes(source: str, path: str) -> list[str]:
    """Ways ``source`` (at repo-relative ``path``) could reach the terminal
    without the escaping writers, by rule rather than by alias resolution:
    no ``print``; no ``from click``/``from sys`` imports and no aliasing of
    either module, so ``click.``/``sys.`` always mean the modules; no click
    writer attribute; a ``sys`` stream only to ask ``isatty``/``fileno``/
    ``encoding``; ``echo_raw`` imported only by cli.py and named only inside
    the two patch commands."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def function_of(node: ast.AST) -> str:
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"

    found = set()
    for node in ast.walk(tree):
        where = f"{path}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Name) and node.id == "print":
            found.add(f"{where} print")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and (module == "sys" or module.split(".")[0] == "click"):
                found.add(f"{where} from {module} import")
            for alias in node.names:
                if alias.name == "echo_raw" and (path != _RAW_MODULE or alias.asname):
                    found.add(f"{where} echo_raw import")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("click", "sys") and (
                    alias.asname or alias.name not in ("click", "sys")
                ):
                    found.add(f"{where} import {alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr == "echo_raw":
            found.add(f"{where} .echo_raw")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "click" and node.attr in _CLICK_WRITERS:
                found.add(f"{where} click.{node.attr}")
            elif node.value.id == "sys" and node.attr in _STREAMS:
                parent = parents.get(node)
                if not (isinstance(parent, ast.Attribute) and parent.attr in _STREAM_QUERIES):
                    found.add(f"{where} sys.{node.attr}")
        elif isinstance(node, ast.Name) and node.id == "echo_raw":
            if path != _RAW_MODULE or function_of(node) not in _RAW_FUNCTIONS:
                found.add(f"{where} echo_raw")
    return sorted(found)


def test_nothing_outside_terminal_py_writes_to_the_terminal_directly():
    root = SRC.parents[1]
    found = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if relative != _TERMINAL_MODULE:
            found += terminal_writes(path.read_text(encoding="utf-8"), relative)
    assert found == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import click\nclick.echo(1)\n", "m.py:2 click.echo"),
        ("import click\nclick.secho(1)\n", "m.py:2 click.secho"),
        ("import click\nclick.termui.echo(1)\n", "m.py:2 click.termui"),
        ("import click\nclick.utils.echo(1)\n", "m.py:2 click.utils"),
        ("import click as c\nc.echo(1)\n", "m.py:1 import click"),
        ("from click import echo\n", "m.py:1 from click import"),
        ("from click.termui import echo as e\n", "m.py:1 from click.termui import"),
        ("print(1)\n", "m.py:1 print"),
        ("f = print\n", "m.py:1 print"),
        ("import sys\nsys.stderr.write('x')\n", "m.py:2 sys.stderr"),
        ("import sys\nsys.stdout.buffer.write(b'x')\n", "m.py:2 sys.stdout"),
        ("import sys\nout = sys.stdout\n", "m.py:2 sys.stdout"),
        ("import sys as s\n", "m.py:1 import sys"),
        ("from sys import stderr\n", "m.py:1 from sys import"),
        ("from .terminal import echo_raw\n", "m.py:1 echo_raw import"),
    ],
)
def test_the_guard_reports_each_way_to_the_terminal(source, expected):
    assert expected in terminal_writes(source, "m.py")


def test_the_guard_allows_the_wrappers_and_stream_queries():
    source = (
        "import sys\nfrom syncmill.terminal import echo, secho\n"
        "def f(x):\n    echo(x)\n    secho(x)\n    return sys.stdout.isatty()\n"
    )
    assert terminal_writes(source, "m.py") == []


def test_the_guard_limits_echo_raw_to_the_patch_commands_in_cli():
    source = (
        "from syncmill.terminal import echo_raw\n"
        "def diff_run():\n    echo_raw(1)\n\n"
        "def other():\n    raw = echo_raw\n"
    )
    assert terminal_writes(source, "src/syncmill/cli.py") == ["src/syncmill/cli.py:6 echo_raw"]
    assert "src/syncmill/tui.py:1 echo_raw import" in terminal_writes(source, "src/syncmill/tui.py")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from syncmill.terminal import echo_raw as raw\n", "src/syncmill/cli.py:1 echo_raw import"),
        ("from syncmill import terminal\nterminal.echo_raw(1)\n", "src/syncmill/cli.py:2 .echo_raw"),
    ],
)
def test_the_guard_rejects_renamed_or_qualified_echo_raw(source, expected):
    assert expected in terminal_writes(source, "src/syncmill/cli.py")


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


def _room_store(tmp_path, monkeypatch):
    import types

    from syncmill.rooms import RoomStore

    store = RoomStore(tmp_path / "rooms")
    room = store.create_room(repo_root=tmp_path, prompt="p" + HOSTILE, agents=["claude"])
    store._conn.execute("UPDATE rooms SET state=? WHERE id=?", ("open" + HOSTILE, room["id"]))
    coordinator = types.SimpleNamespace(store=store, repo_root=tmp_path, close=lambda: None)
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), coordinator),
    )
    return store, room


def test_room_list_with_a_hostile_stored_state_is_escaped(monkeypatch, tmp_path):
    from syncmill.cli import cli

    store, room = _room_store(tmp_path, monkeypatch)
    try:
        res = CliRunner().invoke(cli, ["room", "list"], color=True)
    finally:
        store.close()
    assert res.exit_code == 0, res.output
    assert room["id"] in res.output and printable_name("open" + HOSTILE) in res.output
    assert _safe(res.output), ascii(res.output)


def test_team_list_and_show_escape_configured_values(monkeypatch):
    from syncmill.cli import cli

    monkeypatch.setenv(
        "SYNCMILL_TEAMS",
        json.dumps({"demo": {"agents": ["codex"], "goals": ["g" + HOSTILE], "strategy": "route"}}),
    )
    for args in (["team", "list"], ["team", "show", "demo"]):
        res = CliRunner().invoke(cli, args, color=True)
        assert res.exit_code == 0, res.output
        assert printable_name("g" + HOSTILE) in res.output, args
        assert _safe(res.output.replace("\x1b[1m", "").replace("\x1b[0m", "")), ascii(res.output)


def test_board_decisions_escape_stored_text(monkeypatch, tmp_path):
    from syncmill.board import Board
    from syncmill.cli import cli

    db = tmp_path / "board.db"
    board = Board(str(db))
    board.add_decision("decide " + HOSTILE, scope="s" + HOSTILE)
    board.close()
    monkeypatch.setenv("SYNCMILL_BOARD__ENABLED", "true")
    monkeypatch.setenv("SYNCMILL_BOARD__DB_PATH", str(db))
    res = CliRunner().invoke(cli, ["board", "decisions"], color=True)
    assert res.exit_code == 0, res.output
    assert printable_name("decide " + HOSTILE) in res.output
    assert _safe(res.output), ascii(res.output)


def test_diff_still_prints_the_stored_patch_unchanged(monkeypatch, tmp_path):
    # Escaping a patch is #199's decision; this slice must not change it.
    import hashlib

    from syncmill.artifacts import ArtifactStore
    from syncmill.cli import cli

    patch = ("diff --git a/x b/x\n+" + HOSTILE + "\n").encode()
    store = ArtifactStore(tmp_path / "store", "project")
    run_dir = store.root / "run-1"
    run_dir.mkdir()
    (run_dir / "winner.patch").write_bytes(patch)
    (run_dir / "manifest.json").write_text(
        json.dumps({"state": "pending", "patch_sha256": hashlib.sha256(patch).hexdigest()})
    )
    monkeypatch.setattr("syncmill.cli._project_artifacts", lambda repo: (tmp_path, None, store))
    res = CliRunner().invoke(cli, ["diff", "run-1"], color=True)
    assert res.exit_code == 0, res.output
    assert res.stdout_bytes == patch



def test_room_diff_still_prints_the_stored_patch_unchanged(monkeypatch, tmp_path):
    import types

    from syncmill.cli import cli

    patch = ("diff --git a/x b/x\n+" + HOSTILE + "\n").encode()
    store = types.SimpleNamespace(load_candidate=lambda candidate_id: {"patch": patch})
    coordinator = types.SimpleNamespace(store=store, close=lambda: None)
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), coordinator),
    )
    res = CliRunner().invoke(cli, ["room", "diff", "cand-1"], color=True)
    assert res.exit_code == 0, res.output
    assert res.stdout_bytes == patch

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
