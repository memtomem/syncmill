"""Run identity + the run-envelope writer (S2/S3 of the ecosystem plan).

Every execution path — direct ``run``, monitored (tmux OUTER/inner), and board
``run_item`` — shares ONE stable ``run_id`` (the join key of
``contracts/run-envelope.schema.json``). :func:`resolve_run_id` is the single
resolver the three paths converge on:

- **explicit** — the caller already owns an id: ``run_item`` passes its board
  lease/scope run_id so lease, ``board-run:{run_id}`` memory scope, and envelope
  are one identity (no double issuance).
- **monitor inner** — the tmux OUTER minted the id (it never builds a
  Supervisor) and propagated it via the 0600 env snapshot as ``SYNCMILL_RUN_ID``
  (never argv). Honored ONLY when ``inner_dir()`` confirms this process is the
  in-tmux inner (env var + ``.syncmill-inner`` sentinel), so a stray exported
  ``SYNCMILL_RUN_ID`` can never leak into a normal run.
- **fresh** — a direct run mints a uuid4.

The envelope writer (S3) consumes that id: when ``[integration.report]`` is
enabled, :class:`RunReporter` writes ``{runs_dir}/{run_id}/envelope.json`` — a
schema-valid, **body-free** join artifact (digests and counts only; prompts,
output, patch content, and file-path lists are forbidden by contract) — plus a
``result.json`` copy (via ``monitor._result_to_json``, the one result payload,
so it can't drift from the monitor's IPC file). The whole feature is
best-effort/degrade like memory: a report failure warns and the run, result
output, and cleanup continue unchanged. Disabled (the default),
:func:`build_reporter` returns ``None`` and no filesystem is touched.

A flat top-level module (gateway/monitor/session/board precedent). It imports
from ``monitor`` (``RUN_ID_ENV``/``inner_dir`` live beside ``INNER_ENV`` — the
same IPC channel — and ``_result_to_json`` is the shared result contract, the
``session.py`` private cross-import precedent); ``monitor`` never imports back,
keeping the graph acyclic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from syncmill.config import ReportConfig
from syncmill.models import TaskResult, TaskStatus
from syncmill.monitor import RUN_ID_ENV, _result_to_json, inner_dir

logger = logging.getLogger(__name__)

ENVELOPE_FILE = "envelope.json"
RESULT_FILE = "result.json"
SCHEMA_VERSION = 1  # int, matching tracegraph's strict `!=` major check
KIND = "syncmill.run-envelope"


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


# ── Envelope primitives ──────────────────────────────────────────────────────


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_digest(data: bytes) -> str:
    """The contract's digest form: ``sha256:<64 hex>``."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def write_envelope(run_dir: Path, doc: dict) -> Path:
    """Atomically write ``doc`` to ``<run_dir>/envelope.json`` and return the path.

    The breaker ``_save`` recipe — a unique temp file in the SAME directory (so
    ``os.replace`` stays on one filesystem and is atomic), then replace; the
    unique name keeps a ``finish``/``patch_envelope_board`` overlap collision-safe
    (monitor's fixed ``.tmp`` sibling is only safe with a single owner). Raises
    ``OSError``/``ValueError`` on failure — callers own the degrade.
    """
    path = run_dir / ENVELOPE_FILE
    fd, tmp = tempfile.mkstemp(dir=str(run_dir), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
    except BaseException:
        # Don't leak the temp file if the write/replace failed.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _sweep_retention(runs_dir: Path, keep_last: int, current_run_id: str) -> None:
    """Best-effort retention: drop the oldest run dirs beyond ``keep_last``.

    The current run always survives (excluded up front) and counts toward the
    total, so ``keep_last - 1`` OTHER runs are retained. Each removal is
    independent (one stuck dir can't strand the rest — the ``cleanup_all``
    discipline), and a per-entry ``stat`` race (a dir removed concurrently, a
    permission hiccup) sorts that entry oldest instead of raising — its
    ``rmtree`` then fails harmlessly if it's really gone. Listing errors bubble
    to the caller's own guard.
    """
    if not runs_dir.is_dir():
        return

    def _mtime(d: Path) -> float:
        try:
            return d.stat().st_mtime
        except OSError:
            return 0.0

    others = [d for d in runs_dir.iterdir() if d.is_dir() and d.name != current_run_id]
    others.sort(key=_mtime, reverse=True)
    for stale in others[max(keep_last - 1, 0) :]:
        try:
            shutil.rmtree(stale)
        except OSError:
            logger.warning("report retention: could not remove %s", stale)


# ── Per-run writer ───────────────────────────────────────────────────────────


class RunReporter:
    """Writes one run's envelope: a schema-valid skeleton at ``start`` (so even a
    SIGKILL'd run leaves a parseable, honest "failed" artifact for the contract's
    strict consumers), rewritten with the real result at ``finish``. Every method
    is best-effort — a failure degrades (warning + flag), never raises, and
    ``finish`` retries the directory setup if ``start`` failed."""

    def __init__(self, config: ReportConfig) -> None:
        self._config = config
        self._run_dir: Path | None = None
        self._run_id: str | None = None
        self._created_at: str | None = None
        self._meta: dict = {}
        self.degraded = False
        self.last_error: str | None = None

    def start(
        self,
        *,
        run_id: str,
        task_id: str,
        strategy: str,
        team: str | None,
        agents: list[str],
    ) -> None:
        """Create ``{runs_dir}/{run_id}/``, sweep retention, write the skeleton."""
        # Meta first, unconditionally — a failed start still leaves finish() able
        # to rebuild the whole document from scratch.
        self._run_id = run_id
        self._created_at = _utc_now()
        self._meta = {
            "task_id": task_id,
            "strategy": strategy,
            "team": team,
            "agents": list(agents),
        }
        try:
            run_dir = self._ensure_run_dir()
        except (OSError, ValueError, RuntimeError) as exc:
            self._degrade(f"failed to start run report: {exc}")
            return
        # Retention is a cleanup of OTHER runs — its failure (a listing race, a
        # permission hiccup) must never cost THIS run its skeleton, so it gets
        # its own guard rather than riding the skeleton's.
        try:
            _sweep_retention(run_dir.parent, self._config.keep_last, run_id)
        except (OSError, RuntimeError) as exc:
            logger.warning("report retention sweep failed: %s", exc)
        try:
            write_envelope(run_dir, self._skeleton_doc())
        except (OSError, ValueError, RuntimeError) as exc:
            self._degrade(f"failed to start run report: {exc}")

    def finish(
        self,
        result: TaskResult,
        *,
        degraded: list[str],
        trace: dict | None = None,
    ) -> Path | None:
        """Write ``result.json`` + the final envelope; return the envelope path.

        The result payload is ``monitor._result_to_json`` verbatim (the one
        contract); the envelope carries only its digest — plus the patch digest,
        computed over the same surrogateescape bytes ``emit_patch_file`` writes,
        so the digest verifies the actual ``--emit-patch`` file.

        ``trace`` is the tracer's envelope block (``tracer.flush_trace``),
        merged over the skeleton's ``{"status": "disabled"}`` — only at finish,
        so the skeleton (the SIGKILL path) stays honestly ``disabled``.
        """
        try:
            run_dir = self._ensure_run_dir()  # retry if start failed
            # ensure_ascii (the default) escapes any surrogateescape'd patch bytes
            # to \udXXX, so encoding to ascii never throws and the digest is of
            # the exact bytes written.
            payload = json.dumps(_result_to_json(result)).encode("ascii")
            fd, tmp = tempfile.mkstemp(dir=str(run_dir), suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(payload)
                os.replace(tmp, run_dir / RESULT_FILE)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            patch_digest = (
                sha256_digest(result.patch.encode("utf-8", errors="surrogateescape"))
                if result.patch is not None
                else None
            )
            doc = self._skeleton_doc()
            doc["finished_at"] = _utc_now()
            doc["degraded"] = list(degraded)
            if trace is not None:
                doc["trace"] = trace
            completed = result.status == TaskStatus.COMPLETED
            doc["result"] = {
                "status": "completed" if completed else "failed",
                "agent_id": result.agent_id,
                "winner": result.agent_id if completed else None,
                "exit_code": result.exit_code,
                "duration_seconds": result.duration_seconds,
                "gate_passed": result.gate.passed if result.gate is not None else None,
                # Count only — the path LIST is a forbidden body by contract.
                "files_changed_count": len(result.files_changed),
                "artifact": RESULT_FILE,  # relative to the envelope's directory
                "digest": sha256_digest(payload),
                "patch_digest": patch_digest,
            }
            return write_envelope(run_dir, doc)
        except (OSError, ValueError, RuntimeError) as exc:
            self._degrade(f"failed to finish run report: {exc}")
            return None

    def _ensure_run_dir(self) -> Path:
        """Resolve + create the run directory (memoized). ``expanduser`` sits
        inside the callers' try — it raises ``RuntimeError`` when ``~`` can't
        resolve (no-HOME cron/systemd; the board precedent)."""
        if self._run_dir is None:
            runs_dir = Path(self._config.runs_dir).expanduser()
            run_dir = runs_dir / (self._run_id or "")
            run_dir.mkdir(parents=True, exist_ok=True)
            self._run_dir = run_dir
        return self._run_dir

    def _skeleton_doc(self) -> dict:
        """A fully schema-VALID envelope for a run that has not (yet) reported
        finishing: honest ``result.status: "failed"``, ``finished_at`` mirroring
        ``created_at`` (the field is required non-nullable), every sub-block
        present. ``trace``/``preflight`` stay ``disabled`` in the skeleton even
        when those stages are on — their real blocks only exist at ``finish``,
        so a SIGKILL'd run honestly reports neither artifact."""
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": KIND,
            "run_id": self._run_id,
            "created_at": self._created_at,
            "finished_at": self._created_at,
            "strategy": self._meta.get("strategy"),
            "team": self._meta.get("team"),
            "task_id": self._meta.get("task_id"),
            "agents": self._meta.get("agents", []),
            "board": None,
            "preflight": {"status": "disabled"},
            "trace": {"status": "disabled"},
            "result": {
                "status": "failed",
                "agent_id": None,
                "winner": None,
                "exit_code": None,
                "duration_seconds": None,
                "gate_passed": None,
                "files_changed_count": None,
                "patch_digest": None,
            },
            "degraded": [],
        }

    def _degrade(self, msg: str) -> None:
        self.degraded = True
        self.last_error = msg
        logger.warning("run report degraded: %s", msg)


def build_reporter(config: ReportConfig) -> RunReporter | None:
    """The reporter, or ``None`` when reporting is disabled (the default) — the
    ``build_gate``/``build_breaker`` pattern: the disabled path never touches the
    filesystem and behavior is byte-identical to before."""
    if not config.enabled:
        return None
    return RunReporter(config)


def patch_envelope_board(
    runs_dir: str | Path,
    run_id: str,
    *,
    item_id: str,
    goal: str | None,
) -> bool:
    """Best-effort: set the finished envelope's ``board`` block (``run_item``
    calls this after ``board.apply_update`` — the envelope is finalized by
    ``run()``'s own finally first, so this is a read-modify-write). Returns
    ``False`` (with a warning) instead of raising on any failure."""
    try:
        run_dir = Path(runs_dir).expanduser() / run_id
        doc = json.loads((run_dir / ENVELOPE_FILE).read_text())
        if not isinstance(doc, dict):
            raise ValueError("envelope is not a JSON object")
        doc["board"] = {"item_id": item_id, "goal": goal}
        write_envelope(run_dir, doc)
        return True
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning("could not patch envelope board block for run %s: %s", run_id, exc)
        return False


# ── Read surface (CLI `report show` / `report list`) ─────────────────────────


def load_envelope(runs_dir: str | Path, run_id: str) -> dict | None:
    """Parse ``{runs_dir}/{run_id}/envelope.json``; ``None`` if absent/unreadable."""
    try:
        doc = json.loads(
            (Path(runs_dir).expanduser() / run_id / ENVELOPE_FILE).read_text()
        )
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError, RuntimeError):
        return None


def list_reports(runs_dir: str | Path) -> list[dict]:
    """Every parseable envelope under ``runs_dir``, newest first (``created_at``
    is ISO-8601, so the string sorts chronologically). Best-effort per dir —
    an unparseable or foreign entry is skipped, never fatal."""
    try:
        root = Path(runs_dir).expanduser()
        if not root.is_dir():
            return []
        docs = [d for e in sorted(root.iterdir()) if (d := load_envelope(root, e.name))]
    except (OSError, RuntimeError):
        return []
    docs.sort(key=lambda doc: str(doc.get("created_at") or ""), reverse=True)
    return docs


def render_report(doc: dict) -> str:
    """Human rendering of one envelope (the ``board show`` label style) — shared
    verbatim by ``report show`` (and any later dashboard slice, the
    ``render_item`` precedent). The envelope carries no bodies by contract, so
    neither does this."""
    board = doc.get("board")
    board_text = (
        f"{board.get('item_id')} (goal: {board.get('goal') or '—'})"
        if isinstance(board, dict)
        else "—"
    )
    result = doc.get("result") or {}
    degraded = doc.get("degraded") or []
    lines = [
        f"Run:        {doc.get('run_id')}",
        f"Created:    {doc.get('created_at')}",
        f"Finished:   {doc.get('finished_at')}",
        f"Strategy:   {doc.get('strategy')}",
        f"Team:       {doc.get('team') or '—'}",
        f"Task:       {doc.get('task_id')}",
        f"Agents:     {', '.join(doc.get('agents') or []) or '—'}",
        f"Board:      {board_text}",
        f"Result:     {result.get('status')}"
        + (f" ({result.get('agent_id')})" if result.get("agent_id") else ""),
        f"  winner:             {result.get('winner') or '—'}",
        f"  exit code:          {result.get('exit_code')}",
        f"  duration:           {result.get('duration_seconds')}s",
        f"  gate passed:        {result.get('gate_passed')}",
        f"  files changed:      {result.get('files_changed_count')}",
        f"  artifact:           {result.get('artifact') or '—'}",
        f"  digest:             {result.get('digest') or '—'}",
        f"  patch digest:       {result.get('patch_digest') or '—'}",
        f"Preflight:  {(doc.get('preflight') or {}).get('status')}",
        f"Trace:      {(doc.get('trace') or {}).get('status')}",
        f"Degraded:   {', '.join(degraded) or '—'}",
    ]
    return "\n".join(lines)
