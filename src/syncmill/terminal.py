"""The one place syncmill writes to the terminal.

Everything the CLI prints may quote a stored record, an agent's output, a
provider's error, a configured path or an exception, and any of them can carry
characters a terminal interprets (C1 CSI, OSC sequences, bidi overrides, CR).
``echo``/``secho`` pass every message through ``printable_text`` before click
styles and writes it, so colour still works and line breaks survive while
nothing else non-printable reaches the terminal. ``echo_raw`` is the explicit
byte-exact exception; a test pins where it may be used and that nothing else in
the package writes to the terminal directly.

``install_console_guards`` covers the writers that are not echo calls: every
log line a handler formats (after filters, whatever field its format names)
and the interpreter's report of an uncaught exception, in any thread.
``EscapingArgumentParser`` covers argparse's own output.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any

import click

from syncmill.artifacts import printable_text


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


def echo_raw(message: Any = None, **kwargs: Any) -> None:
    """Write ``message`` unchanged. Only for output that is also data (a stored
    patch); the allowlist test pins where it may be called."""
    click.echo(message, **kwargs)


class EscapingArgumentParser(argparse.ArgumentParser):
    """argparse writes usage, help and errors itself (then exits), outside
    click; subparsers inherit the class, so they are covered too."""

    def _print_message(self, message: str, file=None) -> None:
        super()._print_message(printable_text(message) if message else message, file)


def _settle_record(record: logging.LogRecord) -> None:
    """Escape what a handler prints straight from the record, and fix what it
    would otherwise compute from live objects: the
    message (formatted now, args dropped -- a bad format string becomes the
    template and its arguments instead of an error) and the traceback
    (rendered into ``exc_text``, ``exc_info`` dropped, so a handler that renders
    tracebacks itself -- Rich's -- prints this text, which ``format`` escapes,
    rather than the exception object)."""
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 — must not raise out of the logging call
        message = f"{record.msg} {record.args!r}"
    record.msg = message
    record.args = None
    if record.exc_info:
        exc_type, exc, tb = record.exc_info
        if exc is not None:
            record.exc_text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip("\n")
        record.exc_info = None
    # Handlers also print record attributes outside ``format`` -- Rich draws
    # the path, level and time column itself -- so every string attribute
    # (name, pathname, threadName, string extra fields ...) is escaped here too.
    for key, value in list(vars(record).items()):
        if isinstance(value, str):
            setattr(record, key, printable_text(value))


def _guarded_make_record(original: Callable[..., logging.LogRecord]) -> Callable[..., logging.LogRecord]:
    """Wrap ``Logger.makeRecord`` (on the class, so loggers created earlier are
    covered) to settle each complete record -- ``extra`` included."""

    def make_record(self: logging.Logger, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = original(self, *args, **kwargs)
        _settle_record(record)
        return record

    make_record._syncmill_escaping = True  # type: ignore[attr-defined]
    return make_record


def _escaping_format(original: Callable[..., str]) -> Callable[..., str]:
    """Wrap ``Handler.format``: escape the text a handler is about to write,
    after filters ran and the formatter filled in every field, whatever its
    type. StreamHandler and RichHandler both write what ``format`` returns
    (Rich adds its own styling afterwards). Not covered: a handler subclass
    that overrides ``format`` itself; syncmill defines none."""

    def format(self: logging.Handler, record: logging.LogRecord) -> str:
        return printable_text(original(self, record))

    format._syncmill_escaping = True  # type: ignore[attr-defined]
    return format


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


def install_console_guards() -> None:
    """Escape every log record and the report of an uncaught exception, in
    the main thread or any other. Idempotent. Not covered: click's own help and
    usage text, which quotes the program name and option names the user typed."""
    # Closures per install, so a wrapper someone installs in between, around
    # one of these, cannot be made to call itself by a reinstall.
    if not getattr(logging.Logger.makeRecord, "_syncmill_escaping", False):
        logging.Logger.makeRecord = _guarded_make_record(logging.Logger.makeRecord)  # type: ignore[method-assign]
    if not getattr(logging.Handler.format, "_syncmill_escaping", False):
        logging.Handler.format = _escaping_format(logging.Handler.format)  # type: ignore[method-assign]
    sys.excepthook = _escaping_excepthook
    threading.excepthook = _escaping_thread_excepthook
