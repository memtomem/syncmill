"""The one place syncmill writes to the terminal.

Everything the CLI prints may quote a stored record, an agent's output, a
provider's error, a configured path or an exception, and any of them can carry
characters a terminal interprets (C1 CSI, OSC sequences, bidi overrides, CR).
``echo``/``secho`` pass every message through ``printable_text`` before click
styles and writes it, so colour still works and line breaks survive while
nothing else non-printable reaches the terminal. ``echo_raw`` is the explicit
byte-exact exception; a test pins where it may be used and that nothing else in
the package writes to the terminal directly.

``install_console_guards`` covers the interpreter's report of an uncaught
exception, in any thread; ``EscapingArgumentParser`` covers argparse's own
output. Console logging is not covered (#202).
"""

from __future__ import annotations

import argparse
import sys
import threading
import traceback
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
    """Escape the interpreter's report of an uncaught exception, in the main
    thread or any other. Idempotent. Console logging is not covered: escaping
    inside stdlib logging means guessing what each handler, formatter and
    filter reads, and attempts broke third-party formatters (#202). Click's
    own help and usage text is not covered either."""
    sys.excepthook = _escaping_excepthook
    threading.excepthook = _escaping_thread_excepthook
