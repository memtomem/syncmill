"""Durable interactive agent sessions in tmux (#17, v0.4).

The complement to the live monitor (#16): where ``--monitor`` gives an *ephemeral*
observability view of a *programmatic* concurrent run, this launches a
*long-lived* **interactive** agent — the bare ``claude``/``codex``/``kimi`` REPL a
human types into — inside a **dedicated, reattachable** tmux session that survives
detach (``Ctrl-b d`` / terminal close / SSH drop) and can be reattached by name.
It makes syncmill's interactive-collaboration mode first-class: drive one agent
yourself (no programmatic billing) while ``syncmill run --exclude <agent>``
orchestrates the rest, the two sides meeting only in the ``shared`` memtomem
namespace.

**Not an agent, not a strategy.** The interactive session is a separate human-
driven process — it NEVER enters ``AgentRunner.execute``/``aexecute`` (that would
re-incur programmatic billing and break agent purity), so the per-agent launch
command lives here as a standalone :data:`_INTERACTIVE` map, deliberately
*separate* from the programmatic ``CLIAgent._build_command``. This module touches
neither agents nor memory — it is pure tmux + process launch.

**Reuses #16's tmux primitives** (``monitor.py`` stays UNCHANGED): the control/
attach helpers ``_tmux``/``_tmux_attach``, the snapshot skip-set
``_SNAPSHOT_SKIP_ENV``, and the inner-handshake var name ``INNER_ENV`` (so the
session snapshot can deliberately EXCLUDE it). These are private (``_``-prefixed)
cross-module imports — the same established intra-package pattern as
``cli.py``'s ``from syncmill.supervisor import _matched_order`` — so they are a
de-facto API any monitor.py refactor must preserve. NOTE for tests: because
``_tmux``/``_tmux_attach`` are imported by NAME, they are rebound into THIS
module's globals — patch ``session._tmux`` (the lookup site), NOT ``monitor._tmux``
(patch where a name is LOOKED UP, not where it is defined).

**Why a session-specific shim** (not ``monitor._ENV_SHIM``): a detached tmux
session inherits the (possibly stale) tmux *server* environment, not the client's,
so — exactly like the monitor — the interactive agent must be given the outer
shell's env (PATH/auth/``SYNCMILL_*``) via a ``0600`` snapshot file + an exec shim,
never via argv (no secrets in ``ps``). :data:`_SESSION_ENV_SHIM` differs from the
monitor's in two ways: it **self-unlinks** the secret env file (the detached pane's
shim runs *asynchronously after* ``new-session`` returns, so the parent can't
safely delete it without a use-after-free — the shim consuming it is race-free and
shrinks the secret window to "until the pane is scheduled"), unlinking in a
``finally`` *before* parsing so a corrupt snapshot still doesn't linger; and the
session snapshot omits ``INNER_ENV`` so the interactive process never carries
``SYNCMILL_MONITOR_DIR`` (which, if it pointed at a monitor run dir, would
false-trip ``monitor.inner_dir``).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Collection
from pathlib import Path
from typing import NamedTuple

from syncmill.platform_support import require_supported_platform
from syncmill.artifacts import printable_name
from syncmill.terminal import printable_command, secho
from syncmill.monitor import (
    INNER_ENV,
    RUN_ID_ENV,
    _SNAPSHOT_SKIP_ENV,
    _tmux,
    _tmux_attach,
)

# ── Identity: the interactive launch command + deterministic session names ──

#: agent id → the bare INTERACTIVE (REPL) argv. Deliberately SEPARATE from each
#: agent's programmatic ``CLIAgent._build_command`` (which adds ``-p``/``exec``/
#: ``--json`` etc.) — the interactive session is not a syncmill agent. This
#: duplicates the ``kimi-code → kimi`` binary fact in ``KimiCodeAgent`` on purpose:
#: interactive mode does not route through the registry (it must run an agent even
#: when that agent is ``enabled=false`` for orchestration). Keyed by the REAL agent
#: id, so the hyphenated ``kimi-code`` is a verbatim key.
_INTERACTIVE: dict[str, list[str]] = {
    "claude": ["claude"],
    "codex": ["codex"],
    "kimi-code": ["kimi"],
}

#: Session-name prefix. Names are ``syncmill-session-<agent>`` — DETERMINISTIC
#: (unlike the monitor's ``syncmill-<uuid8>``), which is the whole durability/
#: reattach key: ``start`` finds an existing session by recomputing the same name.
_SESSION_PREFIX = "syncmill-session"

#: Ownership marker written into each run dir. The stale-dir sweep requires it
#: before ``rmtree`` (the #16 R5 lesson — a prefix match alone would delete any
#: same-prefixed tmp dir). Distinct from the monitor's ``.syncmill-inner`` so a
#: session run dir is never mistaken for a monitor inner dir, and vice versa.
_SESSION_SENTINEL = ".syncmill-session"

#: A run dir older than this (and carrying the sentinel) is assumed orphaned and
#: swept on the next ``start``. Far longer than the moment the shim needs the env
#: file, so a live session's (env-consumed, sentinel-only) dir is never at risk.
_STALE_SESSION_DIR_SECONDS = 24 * 3600

#: The exec shim run as the pane's process: it loads the ``0600`` env snapshot the
#: outer wrote, REPLACES the pane's inherited (possibly stale tmux-server) env with
#: it — preserving only the tmux-owned pane vars (``TERM``/``TMUX``/``TMUX_PANE``,
#: kept in lockstep with ``_SNAPSHOT_SKIP_ENV``) — then ``execvp``s the bare agent
#: binary. The env file is **unlinked in a ``finally`` immediately after the read,
#: BEFORE parsing**, so even a corrupt snapshot leaves no secret behind; the
#: parent never deletes it (the shim runs async after ``new-session`` returns —
#: deleting from the parent would race the read). Secrets ride the file, never argv.
_SESSION_ENV_SHIM = (
    "import os,sys,json\n"
    "keep={k:os.environ[k] for k in ('TERM','TMUX','TMUX_PANE') if k in os.environ}\n"
    "p=os.path.join(sys.argv[1],'env')\n"
    "try:\n"
    "    data=open(p).read()\n"
    "finally:\n"
    "    try: os.unlink(p)\n"
    "    except OSError: pass\n"
    "snap=json.loads(data)\n"
    "os.environ.clear(); os.environ.update(snap); os.environ.update(keep)\n"
    "os.execvp(sys.argv[2], sys.argv[2:])\n"
)


class SessionLaunchError(RuntimeError):
    """A durable session couldn't be created/attached. The CLI converts it to a
    clean error and exits non-zero — there is NO fallback (``session`` IS the tmux
    feature, unlike ``--monitor`` which falls back to a normal run; that contract
    difference is why this is a dedicated exception, not ``MonitorLaunchError``)."""


class SessionInfo(NamedTuple):
    """One live syncmill interactive session: its tmux name and whether a client
    is currently attached."""

    name: str
    attached: bool


def _safe(agent_id: str) -> str:
    """A filename/tmux-name-safe form of an agent id (the real ids are already
    safe; this only guards a stray path separator)."""
    return agent_id.replace("/", "_").replace(os.sep, "_")


def _exact(name: str) -> str:
    """A tmux ``target-session`` spec forcing EXACT matching. tmux resolves a raw
    target by exact-THEN-prefix-THEN-glob, so a raw lookup of
    ``syncmill-session-claude`` would also match a foreign ``syncmill-session-claude-x``
    when the exact one is absent — defeating the exact-known-name scoping. The
    leading ``=`` makes the match exact-only. Used for EVERY ``-t`` target
    (``has-session``/``kill-session``/``attach-session``/pane cosmetics); never on
    ``-s`` (the literal name to create) or ``-c`` (a directory)."""
    return f"={name}"


def session_name(agent_id: str) -> str:
    """The deterministic tmux session name for an agent — the reattach key."""
    return f"{_SESSION_PREFIX}-{_safe(agent_id)}"


def interactive_agents() -> list[str]:
    """The agent ids that can run interactively (the keys of :data:`_INTERACTIVE`)."""
    return list(_INTERACTIVE)


def default_interactive_agent(priority: list[str]) -> str | None:
    """The first agent in ``priority`` that has an interactive mapping, else None.

    Used as ``session start``'s default agent. Walks ``agent_priority`` (not the
    enabled registry) so a disabled-for-orchestration agent still counts — and
    skips any priority entry lacking an interactive mapping rather than failing.
    """
    for agent_id in priority:
        if agent_id in _INTERACTIVE:
            return agent_id
    return None


def _known_session_names() -> dict[str, str]:
    """Map every session name we could ever create → its agent id. Lifecycle
    operations (``list``/``stop --all``) match against this EXACT set rather than
    the ``syncmill-session-`` prefix, so they can never touch a monitor run even if
    the user sets ``SYNCMILL_MONITOR__SESSION_PREFIX=syncmill-session``."""
    return {session_name(agent_id): agent_id for agent_id in _INTERACTIVE}


# ── tmux state queries ─────────────────────────────────────────────────────


def session_exists(name: str) -> bool:
    """Whether a tmux session ``name`` currently exists (rc 0 from has-session).
    False when no server is running (has-session then errors)."""
    return _tmux("has-session", "-t", _exact(name)).returncode == 0


def list_sessions() -> list[SessionInfo]:
    """Active syncmill interactive sessions (name + attached), or ``[]`` when no
    tmux server is running.

    Filters tmux's session list to the EXACT names in :func:`_known_session_names`
    (never a prefix match — see that function). ``#{session_attached}`` is a client
    *count*, so attached = ``int > 0`` (not ``== "1"``); malformed lines are
    skipped defensively.
    """
    res = _tmux("list-sessions", "-F", "#{session_name} #{session_attached}")
    if res.returncode != 0:
        return []  # no server / no sessions
    known = _known_session_names()
    sessions: list[SessionInfo] = []
    for line in res.stdout.splitlines():
        name, _, attached = line.strip().rpartition(" ")
        if not name or name not in known:
            continue
        a = attached.strip()
        if not a.isdigit():
            continue  # malformed attached count — skip defensively
        sessions.append(SessionInfo(name, int(a) > 0))
    return sessions


# ── Launch (create) ────────────────────────────────────────────────────────


def _write_session_env_snapshot(
    run_dir: Path, excluded_env: Collection[str] = ()
) -> None:
    """Write the outer process's environment to ``<run_dir>/env`` (mode ``0600``)
    for :data:`_SESSION_ENV_SHIM` to restore.

    Excludes the tmux-owned ``_SNAPSHOT_SKIP_ENV`` (the shim re-keeps those from the
    pane) AND the monitor IPC vars ``INNER_ENV``/``RUN_ID_ENV`` — even an
    already-exported ``SYNCMILL_MONITOR_DIR`` must not reach the interactive process
    (if it pointed at a monitor run dir holding ``.syncmill-inner`` it would
    false-trip ``monitor.inner_dir``), and a stale ``SYNCMILL_RUN_ID`` must not ride
    along with it into ``resolve_run_id``. A FILE, not argv, keeps secrets out of
    ``ps`` and the tmux command line. ``excluded_env`` lets isolated callers strip
    routing variables that would redirect tools outside their assigned worktree.
    """
    snapshot = {
        k: v
        for k, v in os.environ.items()
        if k not in _SNAPSHOT_SKIP_ENV
        and k not in (INNER_ENV, RUN_ID_ENV)
        and k not in excluded_env
    }
    fd = os.open(str(run_dir / "env"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(snapshot, fh)


def _create_session(
    name: str,
    agent_id: str,
    workdir: str,
    excluded_env: Collection[str] = (),
) -> None:
    """Create the detached tmux session running ``agent_id``'s interactive binary
    through the env-restoring shim, in ``workdir``. Raises :class:`SessionLaunchError`
    on any setup/launch failure (cleaning the run dir — safe, the shim never ran).

    No ``remain-on-exit``: the session SHOULD vanish when the user quits the agent
    (the opposite of the monitor, which keeps the result on screen).
    """
    try:
        run_dir = Path(tempfile.mkdtemp(prefix="syncmill-session-"))
    except OSError as exc:
        # mkdtemp can fail (missing/unwritable TMPDIR) before run_dir exists — convert
        # to SessionLaunchError so the CLI reports a clean error instead of an uncaught
        # traceback (the documented contract is "Raises SessionLaunchError on any
        # setup/launch failure"). Separate from the setup/launch try below, whose
        # except rmtrees a run_dir that does not exist yet at this point.
        raise SessionLaunchError(str(exc)) from exc
    try:
        (run_dir / _SESSION_SENTINEL).write_text("")  # ownership marker for the sweep
        if excluded_env:
            _write_session_env_snapshot(run_dir, excluded_env)
        else:
            _write_session_env_snapshot(run_dir)
        shim_cmd = [
            sys.executable, "-c", _SESSION_ENV_SHIM, str(run_dir), *_INTERACTIVE[agent_id]
        ]
        created = _tmux("new-session", "-d", "-s", name, "-c", workdir, *shim_cmd)
    except (OSError, ValueError) as exc:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise SessionLaunchError(str(exc)) from exc
    if created.returncode != 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise SessionLaunchError(created.stderr.strip() or "tmux new-session failed")
    # The success-path run dir is NOT removed here: the shim self-unlinks the env
    # file (consuming the secret) and the sentinel-only remainder is reaped by the
    # stale-dir sweep — deleting it from the parent would race the async shim read.
    with contextlib.suppress(Exception):  # pane titles are best-effort cosmetics
        _tmux("set-option", "-t", _exact(name), "pane-border-status", "top")
        _tmux("set-option", "-t", _exact(name), "pane-border-format", " #{pane_title} ")
        _tmux("select-pane", "-t", _exact(name), "-T", f"syncmill · {agent_id} (interactive)")


# ── Lifecycle: start / attach / stop ───────────────────────────────────────


def _resolve_target(target: str) -> str | None:
    """A bare agent id → its deterministic session name; a full KNOWN session name
    → itself; anything else → ``None``.

    Returning ``None`` for an unknown target is load-bearing: it keeps lifecycle
    ops scoped to syncmill interactive sessions, so a single-target ``stop``/``attach``
    can never touch a foreign tmux session — including a monitor run that merely
    shares the ``syncmill-session-`` prefix (e.g. ``SYNCMILL_MONITOR__SESSION_PREFIX``
    set to ``syncmill-session``). Same guarantee the ``--all`` path already has.
    """
    if target in _INTERACTIVE:
        return session_name(target)
    if target in _known_session_names():
        return target
    return None


def _attach_and_report(name: str, agent_id: str) -> int:
    """Attach (FOREGROUND, blocks until detach/exit), then report the outcome and
    return an exit code: still-running (detached) → 0; ended (gone) → 0; attach
    itself failed while the session lives → surface + return the tmux rc.

    An isolated session's name and agent id come from its stored record, so
    both are shown through ``printable_name``, and the reattach command is
    printed only when the agent id can be shown unchanged."""
    rc = _tmux_attach(_exact(name))
    shown, agent = printable_name(str(name)), printable_name(str(agent_id))
    reattach = printable_command("syncmill", "session", "attach", args=[agent_id])
    if session_exists(name):
        if rc != 0:
            secho(
                f"[syncmill] could not attach to '{shown}' (tmux exit {rc}); it is "
                f"still running — reattach: {reattach}",
                fg="red",
            )
            return rc
        secho(
            f"[syncmill] detached — '{shown}' is still running. "
            f"reattach: {reattach}",
            fg="yellow",
        )
        return 0
    secho(f"[syncmill] interactive '{agent}' session ended.", fg="green")
    return 0


def _print_collab_reminder(agent_id: str) -> None:
    """Print the one operational rule of interactive collaboration: the session
    must target the ``shared`` namespace EXPLICITLY (its default is cwd-scoped and
    invisible to syncmill)."""
    secho(
        f"[syncmill] collaborate via the 'shared' memory namespace — target it "
        f"EXPLICITLY from this session:\n"
        f"           write: mem_add(content=..., namespace=\"shared\")\n"
        f"           read:  mem_agent_search(query=..., include_shared=True)\n"
        f"           orchestrate the rest: syncmill run --exclude {agent_id} "
        f"-s compete \"...\"",
        fg="cyan",
    )


def start_session(agent_id: str, workdir: str) -> int:
    """Start a new durable interactive session for ``agent_id`` (or reattach to the
    existing one), attach, and return an exit code.

    Reattach is by deterministic name: an existing session is reused as-is
    (skipping creation) — the acceptance criterion ("survives detach and can be
    reattached"). A fresh session sweeps stale run dirs, creates the detached
    session, and prints the ``shared``-namespace collaboration reminder before the
    foreground attach blocks. Raises :class:`SessionLaunchError` on a create failure.
    """
    require_supported_platform()
    name = session_name(agent_id)
    if session_exists(name):
        secho(
            f"[syncmill] reattaching to interactive '{agent_id}' session '{name}'",
            fg="yellow",
        )
        return _attach_and_report(name, agent_id)
    _sweep_stale_session_dirs()  # bound the /tmp env-snapshot footprint (best-effort)
    _create_session(name, agent_id, workdir)
    secho(
        f"[syncmill] started interactive '{agent_id}' session '{name}' in "
        f"{printable_name(str(workdir))}",
        fg="green",
    )
    _print_collab_reminder(agent_id)
    return _attach_and_report(name, agent_id)


def attach_session(target: str) -> int:
    """Reattach to an existing interactive session (``target`` = agent id or full
    KNOWN session name). Raises :class:`SessionLaunchError` if ``target`` is not a
    known syncmill session or no such session exists (a foreign/monitor session is
    never attached)."""
    name = _resolve_target(target)
    if name is None or not session_exists(name):
        raise SessionLaunchError(f"no such session: {target}")
    return _attach_and_report(name, _known_session_names()[name])


def stop_sessions(target: str | None, all_: bool) -> int:
    """Kill the session for ``target`` (agent id or full KNOWN name), or every
    known interactive session when ``all_``. Returns the number actually killed
    (idempotent: a missing/unknown target is a clean 0). BOTH paths only ever touch
    the EXACT known session names — an unknown target resolves to ``None`` and a
    foreign/monitor session sharing the prefix is never killed."""
    if all_:
        names = [n for n in _known_session_names() if session_exists(n)]
    else:
        name = _resolve_target(target) if target else None
        names = [name] if name and session_exists(name) else []
    killed = 0
    for name in names:
        if _tmux("kill-session", "-t", _exact(name)).returncode == 0:
            killed += 1
    return killed


# ── Housekeeping ───────────────────────────────────────────────────────────


def _sweep_stale_session_dirs() -> None:
    """Remove orphaned ``syncmill-session-*`` run dirs left by past launches.

    Sentinel-guarded (proof of ownership — never ``rmtree`` a same-prefixed dir we
    didn't create) and age-bounded. Fully best-effort: any error is ignored. After
    the shim self-unlinks the env file a real run dir holds only the sentinel, so
    sweeping even a live session's old dir is harmless.
    """
    tmp = Path(tempfile.gettempdir())
    try:
        entries = list(tmp.glob("syncmill-session-*"))
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if (
                entry.is_dir()
                and (entry / _SESSION_SENTINEL).is_file()
                and time.time() - entry.stat().st_mtime > _STALE_SESSION_DIR_SECONDS
            ):
                shutil.rmtree(entry, ignore_errors=True)
