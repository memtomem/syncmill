"""Run identity for the run-envelope contract (S2 of the ecosystem plan).

Every execution path — direct ``run``, monitored (tmux OUTER/inner), and board
``run_item`` — must share ONE stable ``run_id`` (the join key of
``contracts/run-envelope.schema.json``; the envelope writer itself is a later
stage). :func:`resolve_run_id` is the single resolver the three paths converge
on:

- **explicit** — the caller already owns an id: ``run_item`` passes its board
  lease/scope run_id so lease, ``board-run:{run_id}`` memory scope, and envelope
  are one identity (no double issuance).
- **monitor inner** — the tmux OUTER minted the id (it never builds a
  Supervisor) and propagated it via the 0600 env snapshot as ``SYNCMILL_RUN_ID``
  (never argv). Honored ONLY when ``inner_dir()`` confirms this process is the
  in-tmux inner (env var + ``.syncmill-inner`` sentinel), so a stray exported
  ``SYNCMILL_RUN_ID`` can never leak into a normal run.
- **fresh** — a direct run mints a uuid4.

A flat top-level module (gateway/monitor/session/board precedent). It imports
from ``monitor`` (``RUN_ID_ENV`` lives beside ``INNER_ENV`` — the same IPC
channel); ``monitor`` never imports back, keeping the graph acyclic.
"""

from __future__ import annotations

import os
from uuid import uuid4

from syncmill.monitor import RUN_ID_ENV, inner_dir


def resolve_run_id(explicit: str | None = None) -> str:
    """Resolve this run's stable id: explicit > monitor-inner env > fresh uuid4.

    An empty ``explicit`` counts as unset (the ``-r ""`` house rule); an inner
    process whose snapshot lacks ``SYNCMILL_RUN_ID`` (an older outer) falls back
    to a fresh id rather than failing.
    """
    if explicit:
        return explicit
    if inner_dir() is not None:
        env_id = os.environ.get(RUN_ID_ENV)
        if env_id:
            return env_id
    return str(uuid4())
