"""syncmill's console log handler (#202): its records print escaped, through
their own handler, without touching any other logger, handler or record."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys

import pytest

from syncmill import terminal
from syncmill.artifacts import printable_text

ESC, BEL, RLO, CSI = chr(0x1B), chr(0x07), chr(0x202E), chr(0x9B)
HOSTILE = f"a{CSI}2J{ESC}]52;c;QQ=={BEL}{RLO}b{chr(13)}c 첫 실행"
RAW = (CSI, ESC, BEL, RLO, chr(13))


def _safe(text: str) -> bool:
    return not any(raw in text for raw in RAW)


@pytest.fixture
def log(capsys):
    """A syncmill logger printing through the installed handler, to capsys's
    stderr (the handler looks ``sys.stderr`` up per record). The autouse
    ``restore_syncmill_logger`` fixture undoes the installation."""
    terminal._install_log_handler()
    capsys.readouterr()
    return logging.getLogger("syncmill.test_console_logging")


@pytest.fixture
def set_level():
    """``Logger.setLevel`` with restore; assigning ``.level`` directly would
    skip the cache reset ``isEnabledFor`` relies on."""
    changed = []

    def _set(logger: logging.Logger, level: int) -> None:
        changed.append((logger, logger.level))
        logger.setLevel(level)

    yield _set
    for logger, level in reversed(changed):
        logger.setLevel(level)


# ------------------------------------------------------------- installation


def test_install_console_guards_routes_syncmill_records_to_one_handler():
    terminal.install_console_guards()
    terminal.install_console_guards()
    logger = logging.getLogger("syncmill")
    assert [type(h) for h in logger.handlers] == [terminal._ConsoleLogHandler]
    assert logger.propagate is False
    assert logger.handlers[0].level == logging.NOTSET


_PARITY = r"""
import logging, sys
if sys.argv[1] == "install":
    from syncmill import terminal
    terminal._install_log_handler()
log = logging.getLogger("syncmill.parity")
log.info("not shown")
log.warning("plain %s %d", "ok", 7)
try:
    raise RuntimeError("boom")
except RuntimeError:
    log.warning("with traceback", exc_info=True)
log.error("with stack", stack_info=True)
"""


def test_benign_output_is_byte_identical_to_last_resort():
    """Without the handler a syncmill record prints through logging.lastResort;
    with it, the same records print the same bytes."""
    before = subprocess.run([sys.executable, "-c", _PARITY, "plain"],
                            capture_output=True, timeout=60)
    after = subprocess.run([sys.executable, "-c", _PARITY, "install"],
                           capture_output=True, timeout=60)
    assert before.returncode == after.returncode == 0
    assert b"plain ok 7\n" in before.stderr
    assert b"not shown" not in before.stderr
    assert after.stderr == before.stderr


def test_the_stream_is_looked_up_per_record(log, monkeypatch):
    first, second = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stderr", first)
    log.warning("one")
    monkeypatch.setattr(sys, "stderr", second)
    log.warning("two")
    assert (first.getvalue(), second.getvalue()) == ("one\n", "two\n")


# ----------------------------------------------------------------- escaping


def test_message_arguments_and_traceback_are_escaped(log, capsys):
    log.warning("id %s", HOSTILE)
    log.warning("lit " + HOSTILE)
    try:
        raise RuntimeError("exc " + HOSTILE)
    except RuntimeError:
        log.warning("failed", exc_info=True)
    log.warning("stacked", stack_info=True)
    err = capsys.readouterr().err
    assert _safe(err), ascii(err)
    escaped = printable_text(HOSTILE)
    assert f"id {escaped}\n" in err
    assert f"lit {escaped}\n" in err
    assert f"RuntimeError: exc {escaped}\n" in err
    assert "첫 실행" in err
    assert "Stack (most recent call last):" in err


def test_a_message_is_one_line_and_a_traceback_keeps_its_lines(log, capsys):
    """An LF in an argument cannot forge a log line (#198); the traceback, the
    one intentionally multi-line part, keeps its lines."""
    try:
        raise RuntimeError("first\nsecond")
    except RuntimeError:
        log.warning("item %s failed: %s", "id\n[syncmill] forged", "a\nb", exc_info=True)
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == "item id\\x0a[syncmill] forged failed: a\\x0ab"
    assert lines[1] == "Traceback (most recent call last):"
    assert lines[-2:] == ["RuntimeError: first", "second"]
    assert not any(line.startswith("[syncmill]") for line in lines)


def test_a_traceback_cached_raw_by_another_formatter_is_still_escaped(log, capsys):
    """A handler on a child logger runs first; its plain Formatter caches the raw
    traceback on the record, and ours reuses that cache as it is."""

    class Plain(logging.Handler):
        def emit(self, record):
            logging.Formatter().format(record)

    plain = Plain()
    log.addHandler(plain)
    try:
        try:
            raise RuntimeError("exc " + HOSTILE)
        except RuntimeError:
            log.warning("failed", exc_info=True)
    finally:
        log.removeHandler(plain)
    err = capsys.readouterr().err
    assert _safe(err), ascii(err)
    assert f"RuntimeError: exc {printable_text(HOSTILE)}\n" in err


def test_the_record_keeps_its_message_and_arguments(log):
    seen = []

    class Keep(logging.Filter):
        def filter(self, record):
            seen.append(record)
            return True

    handler = logging.getLogger("syncmill").handlers[0]
    handler.addFilter(Keep())
    log.warning("id %s", HOSTILE)
    assert (seen[0].msg, seen[0].args) == ("id %s", (HOSTILE,))


# ----------------------------------------------------- a record that fails


class _BadStr:
    def __str__(self):
        raise ValueError("str " + HOSTILE)

    def __repr__(self):
        return "BadStr" + HOSTILE


class _BadBoth(_BadStr):
    def __repr__(self):
        raise ValueError("repr " + HOSTILE)


def test_a_failing_str_is_reported_escaped_with_its_traceback(log, capsys):
    log.warning("v %s", _BadStr())
    err = capsys.readouterr().err
    assert _safe(err), ascii(err)
    assert err.startswith("--- Logging error ---\nTraceback (most recent call last):\n")
    assert f"ValueError: str {printable_text(HOSTILE)}\n" in err
    assert "Call stack:\n" in err
    assert __file__ in err.split("Call stack:\n", 1)[1]
    assert "Message: 'v %s'\n" in err
    assert "Arguments: (BadStr" in err


def test_a_bad_format_string_is_reported_escaped(log, capsys):
    # The argument's repr is hostile: repr() of a plain string would already be
    # escaped, so only the report's own escaping can make this safe.
    log.warning("fmt %d", _BadStr())
    err = capsys.readouterr().err
    assert _safe(err), ascii(err)
    assert "TypeError" in err
    assert f"Arguments: (BadStr{printable_text(HOSTILE)},)\n" in err


def test_unprintable_message_and_arguments_keep_the_original_traceback(log, capsys):
    log.warning("v %s", _BadBoth())
    err = capsys.readouterr().err
    assert _safe(err), ascii(err)
    assert f"ValueError: str {printable_text(HOSTILE)}\n" in err
    assert "Unable to print the message and arguments - possible formatting error.\n" in err


def test_no_report_when_raise_exceptions_is_off(log, capsys, monkeypatch):
    monkeypatch.setattr(logging, "raiseExceptions", False)
    log.warning("v %s", _BadStr())
    assert capsys.readouterr().err == ""


def test_no_report_and_no_error_without_stderr(log, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    log.warning("v %s", _BadStr())
    log.warning("plain")


def test_an_os_error_writing_the_report_is_swallowed(log, monkeypatch):
    class Broken(io.StringIO):
        def write(self, text):
            raise OSError("closed")

    monkeypatch.setattr(sys, "stderr", Broken())
    log.warning("plain")
    log.warning("v %s", _BadStr())


def test_a_recursion_error_while_reporting_propagates(log):
    class Deep(_BadStr):
        def __repr__(self):
            raise RecursionError("deep")

    with pytest.raises(RecursionError):
        log.warning("v %s", Deep())


# -------------------------------------------------------------------- level


def test_the_root_level_still_decides(log, capsys, set_level):
    set_level(logging.root, logging.WARNING)
    log.info("hidden")
    assert capsys.readouterr().err == ""
    # Declared change: lastResort would also drop this INFO (its own floor is
    # WARNING); the handler has no floor, as serve's root handler had none.
    set_level(logging.root, logging.INFO)
    log.info("shown " + HOSTILE)
    assert capsys.readouterr().err == f"shown {printable_text(HOSTILE)}\n"


def test_a_lowered_syncmill_logger_level_prints(log, capsys, set_level):
    set_level(logging.root, logging.WARNING)
    set_level(log, logging.INFO)
    log.info("child info")
    assert capsys.readouterr().err == "child info\n"


# ------------------------------------------- other loggers stay untouched


def test_uvicorn_access_formatter_still_formats_its_records(log, set_level):
    from uvicorn.logging import AccessFormatter

    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.setFormatter(AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    access = logging.getLogger("test_console_logging.uvicorn.access")
    access.addHandler(handler)
    access.propagate = False
    set_level(access, logging.INFO)
    try:
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/mcp", "1.1", 200)
    finally:
        access.removeHandler(handler)
    assert out.getvalue() == '127.0.0.1:1 - "GET /mcp HTTP/1.1" 200 OK\n'


_SERVE = r"""
import logging, sys
from syncmill import terminal
terminal.install_console_guards()
# What MCPServer.__init__ does under `syncmill serve`.
from mcp.server.mcpserver.utilities.logging import configure_logging
from rich.logging import RichHandler
configure_logging("INFO")
assert [type(h) for h in logging.root.handlers] == [RichHandler], logging.root.handlers
assert logging.root.handlers[0].rich_tracebacks
hostile = sys.argv[1]
logging.getLogger("syncmill.gateway").info("SM-INFO %s", hostile)
try:
    raise RuntimeError("SM-EXC " + hostile)
except RuntimeError:
    logging.getLogger("syncmill.gateway").warning("SM-TB", exc_info=True)
logging.getLogger("foreign").warning("FOREIGN-OK")
"""


def test_under_serve_syncmill_records_bypass_rich_and_others_still_reach_it():
    proc = subprocess.run([sys.executable, "-c", _SERVE, HOSTILE],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "COLUMNS": "200"})
    assert proc.returncode == 0, proc.stderr
    err = proc.stderr
    escaped = printable_text(HOSTILE)
    assert err.count("SM-INFO") == 1
    assert f"SM-INFO {escaped}\n" in err.splitlines(keepends=True)
    assert f"RuntimeError: SM-EXC {escaped}\n" in err.splitlines(keepends=True)
    assert "SM-TB\nTraceback (most recent call last):\n" in err
    # Rich's columns (level name) mark the foreign record as Rich-rendered.
    foreign = [line for line in err.splitlines() if "FOREIGN-OK" in line]
    assert len(foreign) == 1 and "WARNING" in foreign[0], err
    assert _safe(err), ascii(err)


# --------------------------------------------------------- the real entry


_ENTRY = r"""
import sys
from syncmill import terminal
if sys.argv[1] == "without-handler":
    terminal._install_log_handler = lambda: None
from syncmill.cli import cli
cli(sys.argv[2:])
"""


@pytest.mark.parametrize("mode", ["with-handler", "without-handler"])
def test_a_cli_warning_from_configuration_is_escaped(tmp_path, git_repo, mode):
    """``run --dry-run`` warns about a capability key naming no known agent;
    that key comes from the environment. Without the handler (the bite check)
    it prints raw through lastResort."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    key = "gh" + HOSTILE + "ost"
    env = {**os.environ, "HOME": str(home),
           "SYNCMILL_CAPABILITIES__AGENTS": json.dumps({key: ["python"]})}
    proc = subprocess.run(
        [sys.executable, "-c", _ENTRY, mode, "run", "--dry-run", "--repo", str(git_repo), "do"],
        capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    line = next(row for row in proc.stderr.splitlines(keepends=True) if "unknown agent id" in row)
    if mode == "with-handler":
        assert _safe(proc.stderr), ascii(proc.stderr)
        assert f"unknown agent id(s): gh{printable_text(HOSTILE)}ost (" in line
    else:
        assert not _safe(proc.stderr)


_WORKER = r"""
import logging, sys
from syncmill import terminal, tui_jobs
if sys.argv[1] == "without-handler":
    terminal._install_log_handler = lambda: None
sys.argv = ["tui_jobs"]
try:
    tui_jobs.main()  # no subcommand: the guards are installed, then argparse exits
except SystemExit:
    pass
logging.getLogger("syncmill.tui_jobs").warning("worker %s", sys.argv_hostile)
"""


@pytest.mark.parametrize("mode", ["with-handler", "without-handler"])
def test_the_job_worker_entry_installs_the_handler(mode):
    code = _WORKER.replace("sys.argv_hostile", repr(HOSTILE))
    proc = subprocess.run([sys.executable, "-c", code, mode],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    if mode == "with-handler":
        assert _safe(proc.stderr), ascii(proc.stderr)
        assert f"worker {printable_text(HOSTILE)}\n" in proc.stderr
    else:
        assert not _safe(proc.stderr)
