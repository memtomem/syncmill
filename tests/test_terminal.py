from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from syncmill import terminal
from syncmill.artifacts import printable_name, printable_text, terminal_json
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


# What click would change in text written off a terminal: it strips SGR colour
# and cannot write bytes that are not UTF-8 without decoding them first.
PATCH = (b"diff --git a/x b/x\n+" + HOSTILE.encode()
         + b" \x1b[31mred\x1b[0m caf\xe9\r\n")


def test_echo_patch_writes_the_stored_bytes_off_a_terminal():
    assert _run(lambda: terminal.echo_patch(PATCH)).stdout_bytes == PATCH


def test_echo_patch_escapes_at_a_terminal(monkeypatch):
    monkeypatch.setattr(terminal, "_stdout_is_terminal", lambda: True)
    out = _run(lambda: terminal.echo_patch(PATCH), color=True).output
    assert _safe(out) and "\x1b" not in out, ascii(out)
    assert out == printable_text(PATCH.decode("utf-8", errors="surrogateescape"))
    assert out.endswith("caf\\xe9\\x0d\n")


def test_echo_patch_refuses_what_is_not_bytes():
    with pytest.raises(TypeError, match="not NoneType"):
        terminal.echo_patch(None)


# ------------------------------------------- uncaught exceptions, argparse

_PROBE = r"""
from syncmill.terminal import install_console_guards
install_console_guards()
install_console_guards()
raise RuntimeError("uncaught x\u009b2J\x1b]52;c;QQ==\x07\u202ey")
"""


def test_an_uncaught_exception_report_is_escaped():
    # A real interpreter: pytest handles exceptions itself.
    proc = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, timeout=60)
    err = proc.stderr.decode("utf-8")
    assert proc.returncode == 1
    assert _safe(err), ascii(err)
    assert "RuntimeError: uncaught x\\x9b2J\\x1b]52;c;QQ==\\x07\\u202ey" in err
    assert err.count("Traceback (most recent call last)") == 1


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
    ``encoding``; ``echo_patch`` imported only by cli.py and named only inside
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
                if alias.name == "echo_patch" and (path != _RAW_MODULE or alias.asname):
                    found.add(f"{where} echo_patch import")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("click", "sys") and (
                    alias.asname or alias.name not in ("click", "sys")
                ):
                    found.add(f"{where} import {alias.name}")
        elif isinstance(node, ast.Attribute) and node.attr == "echo_patch":
            found.add(f"{where} .echo_patch")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "click" and node.attr in _CLICK_WRITERS:
                found.add(f"{where} click.{node.attr}")
            elif node.value.id == "sys" and node.attr in _STREAMS:
                parent = parents.get(node)
                if not (isinstance(parent, ast.Attribute) and parent.attr in _STREAM_QUERIES):
                    found.add(f"{where} sys.{node.attr}")
        elif isinstance(node, ast.Name) and node.id == "echo_patch":
            if path != _RAW_MODULE or function_of(node) not in _RAW_FUNCTIONS:
                found.add(f"{where} echo_patch")
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
        ("from .terminal import echo_patch\n", "m.py:1 echo_patch import"),
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


def test_the_guard_limits_echo_patch_to_the_patch_commands_in_cli():
    source = (
        "from syncmill.terminal import echo_patch\n"
        "def diff_run():\n    echo_patch(1)\n\n"
        "def other():\n    raw = echo_patch\n"
    )
    assert terminal_writes(source, "src/syncmill/cli.py") == ["src/syncmill/cli.py:6 echo_patch"]
    assert "src/syncmill/tui.py:1 echo_patch import" in terminal_writes(source, "src/syncmill/tui.py")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from syncmill.terminal import echo_patch as raw\n", "src/syncmill/cli.py:1 echo_patch import"),
        ("from syncmill import terminal\nterminal.echo_patch(1)\n", "src/syncmill/cli.py:2 .echo_patch"),
    ],
)
def test_the_guard_rejects_renamed_or_qualified_echo_patch(source, expected):
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


def _diff_store(monkeypatch, tmp_path):
    import hashlib

    from syncmill.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "store", "project")
    run_dir = store.root / "run-1"
    run_dir.mkdir()
    (run_dir / "winner.patch").write_bytes(PATCH)
    (run_dir / "manifest.json").write_text(
        json.dumps({"state": "pending", "patch_sha256": hashlib.sha256(PATCH).hexdigest()})
    )
    monkeypatch.setattr("syncmill.cli._project_artifacts", lambda repo: (tmp_path, None, store))


def _room_diff_store(monkeypatch, tmp_path):
    import types

    store = types.SimpleNamespace(load_candidate=lambda candidate_id: {"patch": PATCH})
    coordinator = types.SimpleNamespace(store=store, close=lambda: None)
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), coordinator),
    )


_DIFF_COMMANDS = [(["diff", "run-1"], _diff_store), (["room", "diff", "cand-1"], _room_diff_store)]


@pytest.mark.parametrize(("args", "seed"), _DIFF_COMMANDS)
def test_a_redirected_diff_is_the_stored_patch_byte_for_byte(monkeypatch, tmp_path, args, seed):
    from syncmill.cli import cli

    seed(monkeypatch, tmp_path)
    res = CliRunner().invoke(cli, args)
    assert res.exit_code == 0, res.output
    assert res.stdout_bytes == PATCH


@pytest.mark.parametrize(("args", "seed"), _DIFF_COMMANDS)
def test_a_diff_at_a_terminal_is_escaped(monkeypatch, tmp_path, args, seed):
    from syncmill.cli import cli

    seed(monkeypatch, tmp_path)
    monkeypatch.setattr(terminal, "_stdout_is_terminal", lambda: True)
    res = CliRunner().invoke(cli, args, color=True)
    assert res.exit_code == 0, res.output
    assert _safe(res.output) and "\x1b" not in res.output, ascii(res.output)
    assert res.output == printable_text(PATCH.decode("utf-8", errors="surrogateescape"))


def test_room_diff_reports_a_patch_that_is_not_bytes(monkeypatch, tmp_path):
    import types

    from syncmill.cli import cli

    store = types.SimpleNamespace(load_candidate=lambda candidate_id: {"patch": None})
    coordinator = types.SimpleNamespace(store=store, close=lambda: None)
    monkeypatch.setattr(
        "syncmill.cli._room_coordinator",
        lambda repo: (tmp_path, types.SimpleNamespace(), coordinator),
    )
    res = CliRunner().invoke(cli, ["room", "diff", "cand-1"])
    assert res.exit_code == 1
    assert "Traceback" not in res.output and "Error:" in res.output


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


# ---------------------------------------------------------- program name


@pytest.mark.parametrize(("args", "marker"), [(["--help"], "Usage: "),
                                               (["run", "--no-such-option"], "Usage: ")])
def test_the_program_name_is_escaped_in_usage_lines(args, marker):
    """click prints the program name (``sys.argv[0]``) itself, outside any
    error message; an executable or symlink can be named with any bytes."""
    from syncmill.cli import cli

    res = CliRunner().invoke(cli, args, prog_name="sync" + HOSTILE + "\nmill")
    out = res.output
    assert _safe(out), ascii(out)
    line = next(row for row in out.splitlines() if marker in row)
    assert "sync" + printable_name(HOSTILE + "\n") + "mill" in line


def test_the_program_name_is_escaped_in_version():
    # A real interpreter: click's version callback keeps the first program name
    # it sees for the rest of the process, so CliRunner order would decide.
    code = "import sys; from syncmill.cli import cli; cli(['--version'], prog_name=sys.argv[1])"
    proc = subprocess.run([sys.executable, "-c", code, "sync" + HOSTILE + "\nmill"],
                          capture_output=True, timeout=60)
    out = proc.stdout.decode("utf-8")
    assert proc.returncode == 0, proc.stderr
    assert _safe(out), ascii(out)
    assert out == "sync" + printable_name(HOSTILE + "\n") + "mill, version " + out.rsplit(" ", 1)[1]
