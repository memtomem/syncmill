"""Errors that carry a subprocess's output (git, tmux, a verification command).

The CLI prints every error message on one line, so that an LF inside an id or
path cannot start a line of its own (#198). A command's output is the one body
that is meant to keep its lines; an error built here records it separately, and
the CLI shows it as a labelled block under the head, each line prefixed and
escaped on its own. Output of one line stays on the head's line, for the CLI
and for every other reader of ``str(error)`` (a TUI field, a stored reason).

This module imports nothing from syncmill, so every module that runs a command
can use it without an import cycle.
"""

from __future__ import annotations

from typing import TypeVar

E = TypeVar("E", bound=BaseException)


def command_error(error_type: type[E], head: str, output: str) -> E:
    """``error_type("<head>:\\n<output>")``, kept as ``command_output``; for
    output of one line ``error_type("<head>: <output>")``; ``error_type(head)``
    when the output is blank."""
    output = output.strip()
    if not output:
        return error_type(head)
    if "\n" not in output:
        return error_type(f"{head}: {output}")
    error = error_type(f"{head}:\n{output}")
    error.command_output = output  # type: ignore[attr-defined]
    return error
