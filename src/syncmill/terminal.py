"""The one place syncmill writes to the terminal.

Everything the CLI prints may quote a stored record, an agent's output, a
provider's error, a configured path or an exception, and any of them can carry
characters a terminal interprets (C1 CSI, OSC sequences, bidi overrides, CR).
``echo``/``secho`` pass every message through ``printable_text`` before click
styles and writes it, so colour still works and line breaks survive while
nothing else non-printable reaches the terminal. ``echo_patch`` is the one
writer that can be byte-exact, and only off a terminal; a test pins where it may
be used and that nothing else in the package writes to the terminal directly.

``install_console_guards`` covers the interpreter's report of an uncaught
exception, in any thread, and syncmill's own log records; ``EscapingArgumentParser``
covers argparse's own output.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import shlex
import sys
import threading
import traceback
from collections.abc import Sequence
from typing import Any

import click

from syncmill.artifacts import printable_name, printable_text


def _escaped(message: Any) -> Any:
    """The display form of anything echo may be handed. ``None`` stays (an
    empty line); bytes are decoded the way a stored patch is, so an invalid
    byte shows as ``\\xNN``; any other object is shown as ``str()`` -- click
    would stringify it anyway, after this point, unescaped."""
    if message is None:
        return None
    if isinstance(message, (bytes, bytearray)):
        message = bytes(message).decode("utf-8", errors="surrogateescape")
    return printable_text(str(message))


def echo(message: Any = None, **kwargs: Any) -> None:
    click.echo(_escaped(message), **kwargs)


def secho(message: Any = None, **kwargs: Any) -> None:
    click.secho(_escaped(message), **kwargs)


NO_COMMAND = "a value has characters that cannot be shown, so no command is printed"


def shell_arg(value: str) -> str | None:
    """``value`` quoted for a command the user copies, or ``None`` when the
    terminal cannot show it unchanged: an escaped argument in a printed command
    names a different file, run or session, so such a command is not printed."""
    return shlex.quote(value) if printable_name(value) == value else None


def printable_command(
    *words: object, args: Sequence[object] = (), options: Sequence[object] = ()
) -> str:
    """One copy-paste command line: the fixed ``words``, the positional
    ``args``, then ``options``, each quoted; or a short notice when any of them
    cannot be shown unchanged (see ``shell_arg``).

    A positional value that starts with ``-`` would be parsed as an option
    (``--discard=x`` selects a different, destructive action), so then the
    options come first and ``--`` ends option parsing before the values."""
    positional = [str(a) for a in args]
    if any(a.startswith("-") for a in positional):
        argv = [*map(str, words), *map(str, options), "--", *positional]
    else:
        argv = [*map(str, words), *positional, *map(str, options)]
    quoted = [shell_arg(word) for word in argv]
    if any(word is None for word in quoted):
        return f"({NO_COMMAND})"
    return " ".join(quoted)


def _stdout_is_terminal() -> bool:
    return sys.stdout is not None and sys.stdout.isatty()


def echo_patch(data: bytes) -> None:
    """Print a stored patch: escaped at a terminal, as ``echo`` shows any
    bytes; otherwise the stored bytes unchanged, because redirected output is
    data (``> fix.patch``, ``| git apply``). Bytes reach click's binary
    writer, which neither decodes them nor strips ANSI sequences off a
    terminal, as it does for text."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"a patch is bytes, not {type(data).__name__}")
    if _stdout_is_terminal():
        echo(data, nl=False)
    else:
        click.echo(bytes(data), nl=False)


class EscapedStderr(io.TextIOBase):
    """A text stream that writes to whatever ``sys.stderr`` is, escaped, for a
    library that prints through a stream of its own (the TUI's error console)."""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        stream = sys.stderr
        if stream is not None:
            stream.write(printable_text(text))
        return len(text)

    def flush(self) -> None:
        stream = sys.stderr
        if stream is not None:
            stream.flush()


class EscapingArgumentParser(argparse.ArgumentParser):
    """argparse writes usage, help and errors itself (then exits), outside
    click; subparsers inherit the class, so they are covered too."""

    def _print_message(self, message: str, file=None) -> None:
        super()._print_message(printable_text(message) if message else message, file)


def _escaping_excepthook(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    sys.stderr.write(printable_text(text))


def _escaping_thread_excepthook(args: threading.ExceptHookArgs) -> None:
    """``threading.excepthook``'s report, escaped, thread name included."""
    if args.exc_type is SystemExit:
        return
    stream = sys.stderr
    if stream is None:  # interpreter shutdown, as the default hook allows
        return
    name = args.thread.name if args.thread is not None else threading.get_ident()
    text = "".join(
        traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
    )
    stream.write(printable_text(f"Exception in thread {name}:\n{text}"))


class _EscapingFormatter(logging.Formatter):
    """lastResort's format (the message, then any traceback or stack), escaped.

    The message is one line: ``printable_name`` shows an LF in an id, a path or
    an exception string as ``\\x0a``, so an argument cannot forge a log line of
    its own (#198). The traceback and stack keep their lines: the finished
    string is escaped with ``printable_text``, which also covers a traceback
    another formatter cached on the record first. ``msg`` and ``args`` are left
    as they are."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        return printable_name(super().formatMessage(record))

    def format(self, record: logging.LogRecord) -> str:
        return printable_text(super().format(record))


class _ConsoleLogHandler(logging.StreamHandler):
    """Where syncmill's own log records are printed: like ``logging.lastResort``
    it writes to whatever ``sys.stderr`` is when the record arrives (CliRunner,
    pytest and Textual swap it), but everything it writes is escaped, including
    the report of a record that failed to format."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)
        self.setFormatter(_EscapingFormatter())

    @property
    def stream(self):
        return sys.stderr

    def handleError(self, record: logging.LogRecord) -> None:
        """The stdlib report (``logging.Handler.handleError``), built in a
        buffer and escaped before it is written: the stdlib version writes the
        failing exception and the record's ``msg``/``args`` to ``sys.stderr``
        unescaped."""
        stream = sys.stderr
        if not (logging.raiseExceptions and stream):
            return
        t, v, tb = sys.exc_info()
        try:
            buf = io.StringIO()
            buf.write("--- Logging error ---\n")
            traceback.print_exception(t, v, tb, None, buf)
            buf.write("Call stack:\n")
            # The first frame outside the logging package is the caller.
            frame = tb.tb_frame if tb is not None else None
            while frame and os.path.dirname(frame.f_code.co_filename) == _LOGGING_DIR:
                frame = frame.f_back
            if frame:
                traceback.print_stack(frame, file=buf)
            else:
                buf.write(f"Logged from file {record.filename}, line {record.lineno}\n")
            try:
                buf.write("Message: %r\nArguments: %s\n" % (record.msg, record.args))
            except RecursionError:
                raise
            except Exception:
                buf.write(
                    "Unable to print the message and arguments - possible formatting "
                    "error.\nUse the traceback above to help find the error.\n"
                )
            stream.write(printable_text(buf.getvalue()))
        except OSError:
            pass
        finally:
            del t, v, tb


_LOGGING_DIR = os.path.dirname(logging.__file__)


def _install_log_handler() -> None:
    """Print syncmill's records through ``_ConsoleLogHandler`` only. With
    ``propagate`` off they never reach a handler someone else installed on the
    root (``serve``'s RichHandler), so no other handler, formatter or filter
    ever sees, or has to be guessed about, a syncmill record, and nothing
    outside the ``syncmill`` hierarchy is touched. The handler has no level of
    its own: a record prints at its logger's effective level, as it did through
    the root's handlers, rather than at lastResort's fixed WARNING."""
    logger = logging.getLogger("syncmill")
    if not any(isinstance(h, _ConsoleLogHandler) for h in logger.handlers):
        logger.addHandler(_ConsoleLogHandler())
    logger.propagate = False


def install_console_guards() -> None:
    """Escape the interpreter's report of an uncaught exception, in the main
    thread or any other, and every record logged under ``syncmill``. Idempotent.
    Other libraries' log records (memtomem, mcp, uvicorn, asyncio) are not
    covered: escaping inside stdlib logging means guessing what each handler,
    formatter and filter reads, and attempts broke third-party formatters
    (#202). Click's own help and usage text is not covered either."""
    sys.excepthook = _escaping_excepthook
    threading.excepthook = _escaping_thread_excepthook
    _install_log_handler()
