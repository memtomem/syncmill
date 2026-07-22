"""Read-only tmux viewer for a comparison-room round (#0.1.2 Phase 2).

A room round already tees each member's output to a private bounded log
(:class:`syncmill.rooms.RoundLogSink`). This module surfaces those logs live:
``room attach`` (and the TUI Rooms ``t`` key) open a **read-only** tmux session
with one ``tail -F`` pane per member. It is observation-only — the panes run
``tail``, never a provider — so there is no steering here (interactive steering
stays in ``session``).

It is deliberately a flat top-level module (the gateway/monitor/session/board
precedent) that **reuses monitor's tmux primitives** (``_tmux``/``_tmux_attach``/
``tmux_available``) rather than reimplementing them, and follows session.py's
ownership discipline: a deterministic name is the reattach key, and lifecycle
operations target EXACT names (``=name``) so they can never touch a foreign
session. Two invariants carry over from session.py: it is **not** an agent and
**not** a strategy (it touches neither agents nor memory — only tmux and the
room store's already-written logs), and — unlike the round itself, whose
execution never touches tmux — an explicit attach with no tmux available is a
hard error (this IS the tmux feature, so there is no fallback).

Round execution is untouched: the viewer only ever runs on an explicit user
attach, reading logs the round already wrote, so a tmux failure here can never
affect a running round.

Two attach state machines, deliberately separate because their lifecycles
differ:

* **External** (not inside tmux) — a FOREGROUND ``attach-session`` that blocks
  until the user detaches (``Ctrl-b d``); the caller (the CLI, or the TUI under
  ``App.suspend``) regains control on return.
* **Inside tmux** (``$TMUX`` set) — a non-blocking ``switch-client -E -t
  =<session>``; nesting an attach is refused by tmux, and switch does not block,
  so there is no suspend/return dance — the user returns with ``Prefix + L``.
"""

from __future__ import annotations

import os

from syncmill.monitor import _tmux, _tmux_attach, tmux_available
from syncmill.rooms import RoomStore

#: Ownership marker. A room_id is a uuid4, so a name is unforgeably this room's;
#: lifecycle kills scope to ``{prefix}{room_id}-`` and can never hit a foreign
#: session even though tmux otherwise resolves targets by prefix.
_VIEWER_PREFIX = "syncmill-room-"

#: Terminal room states whose viewers a sweep may reclaim.
_TERMINAL_ROOM_STATES = {"applied", "closed", "stale"}


class RoomViewerError(RuntimeError):
    """A room viewer could not be opened. Like ``SessionLaunchError``, this has no
    fallback — the viewer IS the tmux feature — so the CLI surfaces it directly."""


def _exact(name: str) -> str:
    """A tmux ``target-session`` spec forcing EXACT matching (session.py's rule):
    tmux resolves a raw target exact-THEN-prefix, so ``=name`` is what keeps a
    lookup from matching a longer foreign session when the exact one is absent."""
    return f"={name}"


def viewer_session_name(room_id: str, round_number: int) -> str:
    """The deterministic tmux session name for a room round — the reattach key."""
    return f"{_VIEWER_PREFIX}{room_id}-r{round_number}"


def _session_exists(name: str) -> bool:
    return _tmux("has-session", "-t", _exact(name)).returncode == 0


def _resolve_round(store: RoomStore, room_id: str, round_number: int | None) -> dict:
    """The requested round (or the latest), validated to exist."""
    rounds = store.list_rounds(room_id)
    if not rounds:
        raise RoomViewerError(f"room {room_id} has no rounds yet")
    if round_number is None:
        return rounds[-1]
    for row in rounds:
        if int(row["number"]) == round_number:
            return row
    raise RoomViewerError(f"room {room_id} has no round {round_number}")


def ensure_viewer(
    store: RoomStore, room_id: str, round_number: int | None = None
) -> str:
    """Ensure a read-only viewer session exists for a round and return its name.

    Idempotent: an existing session is returned as-is (the reattach path). A
    round with no per-member monitor rows (one that predates the progress table)
    is "terminal unavailable" — there is nothing live to follow — and raises
    rather than opening an empty session.
    """
    if not tmux_available():
        raise RoomViewerError("tmux is not available; the room viewer requires tmux")
    store.get_room(room_id)  # raises RoomError for an unknown room
    round_row = _resolve_round(store, room_id, round_number)
    members = store.list_round_members(round_row["id"])
    if not members:
        raise RoomViewerError(
            f"round {round_row['number']} has no monitor data "
            "(it predates per-member progress); terminal unavailable"
        )
    name = viewer_session_name(room_id, int(round_row["number"]))
    if _session_exists(name):
        return name
    _create_viewer(store, name, round_row["number"], members)
    return name


def _log_abspath(store: RoomStore, log_path: str | None) -> str | None:
    """Resolve a member's DB-stored relative log path under the artifact root."""
    if not log_path:
        return None
    return str(store.candidates.root / log_path)


def _create_viewer(
    store: RoomStore, name: str, round_number: int, members: list[dict]
) -> None:
    """Create the detached read-only session: one ``tail -F`` pane per member."""
    logs = [(m["member_id"], _log_abspath(store, m.get("log_path"))) for m in members]
    logs = [(mid, path) for mid, path in logs if path]
    if not logs:
        raise RoomViewerError("no member logs to follow for this round")
    # Seed each log so `tail -F` opens cleanly even before the first write.
    for _, path in logs:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a"):
                pass
        except OSError:
            pass  # best-effort; tail -F tolerates a not-yet-existent file
    first_member, first_log = logs[0]
    created = _tmux(
        "new-session", "-d", "-s", name, "tail", "-n", "+1", "-F", first_log
    )
    if created.returncode != 0:
        raise RoomViewerError(
            created.stderr.strip() or "tmux new-session failed for the room viewer"
        )
    # Best-effort cosmetics + one pane per remaining member. A pane failure must
    # not tear down a viewer that already shows at least the first member.
    _tmux("set-option", "-t", _exact(name), "pane-border-status", "top")
    _tmux("set-option", "-t", _exact(name), "pane-border-format", " #{pane_title} ")
    _tmux("select-pane", "-t", _exact(name), "-T", f"{first_member} (round {round_number})")
    for member_id, path in logs[1:]:
        res = _tmux(
            "split-window", "-t", _exact(name), "-d", "-P", "-F", "#{pane_id}",
            "tail", "-n", "+1", "-F", path,
        )
        pane_id = res.stdout.strip()
        if res.returncode == 0 and pane_id:
            _tmux("select-pane", "-t", pane_id, "-T", member_id)
        _tmux("select-layout", "-t", _exact(name), "tiled")


def attach_viewer(name: str) -> int:
    """Attach to a viewer session, choosing the state machine by context.

    Returns a tmux exit code (0 on a clean external detach or a successful
    in-tmux switch). Not wrapped in any Textual suspend — the TUI caller wraps
    THIS in ``App.suspend`` for the external path; the CLI needs no wrapper.
    """
    if os.environ.get("TMUX"):
        # Inside tmux: nesting an attach is refused, and switch does not block,
        # so there is no suspend/return dance. -E resets to the pane env.
        return _tmux("switch-client", "-E", "-t", _exact(name)).returncode
    # External: foreground attach that blocks until the user detaches.
    return _tmux_attach(_exact(name))


def in_tmux() -> bool:
    """Whether this process is running inside a tmux client (selects the state
    machine and the correct return hint to print)."""
    return bool(os.environ.get("TMUX"))


def close_room_viewers(store: RoomStore, room_id: str) -> int:
    """Kill every viewer session for a room's rounds. Best-effort, returns the
    count killed. Called from close/prune; a missing tmux server is a no-op.

    Scoped to ``{prefix}{room_id}-``: room_id is a uuid4, so this can only ever
    match this room's own viewers, never a foreign session.
    """
    if not tmux_available():
        return 0
    killed = 0
    prefix = f"{_VIEWER_PREFIX}{room_id}-"
    for name in _list_viewer_sessions():
        if name.startswith(prefix) and _tmux(
            "kill-session", "-t", _exact(name)
        ).returncode == 0:
            killed += 1
    return killed


def sweep_orphan_viewers(store: RoomStore) -> int:
    """Kill viewer sessions whose room no longer exists or is terminal.

    The analogue of ``cleanup_orphaned_worktrees`` / ``_sweep_stale_session_dirs``:
    a crashed attach can leave a viewer for a room that has since been closed or
    pruned. Best-effort; returns the count reclaimed.
    """
    if not tmux_available():
        return 0
    live = {room["id"]: room["state"] for room in store.list_rooms()}
    killed = 0
    for name in _list_viewer_sessions():
        room_id = _room_id_from_name(name)
        if room_id is None:
            continue
        state = live.get(room_id)
        if state is None or state in _TERMINAL_ROOM_STATES:
            if _tmux("kill-session", "-t", _exact(name)).returncode == 0:
                killed += 1
    return killed


def _list_viewer_sessions() -> list[str]:
    """Every live tmux session name carrying the viewer prefix (or ``[]`` when no
    server is running)."""
    res = _tmux("list-sessions", "-F", "#{session_name}")
    if res.returncode != 0:
        return []
    return [
        line.strip()
        for line in res.stdout.splitlines()
        if line.strip().startswith(_VIEWER_PREFIX)
    ]


def _room_id_from_name(name: str) -> str | None:
    """Recover the room_id from a viewer session name, or ``None`` if it does not
    parse. The name is ``{prefix}{room_id}-r{n}``; room_id is a uuid4 (contains
    hyphens but no ``-r`` followed by digits at the end), so strip the trailing
    ``-r<digits>`` suffix."""
    if not name.startswith(_VIEWER_PREFIX):
        return None
    body = name[len(_VIEWER_PREFIX):]
    marker = body.rfind("-r")
    if marker <= 0 or not body[marker + 2:].isdigit():
        return None
    return body[:marker]
