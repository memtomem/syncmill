"""Body-free workspace hook collector for Kimi Code and Antigravity CLI."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from syncmill.agents import normalize_tool_key
from syncmill.models import ToolCallRecord, ToolOutcome, ToolTelemetry


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _mark_incomplete(spool: Path, code: str) -> None:
    marker = Path(f"{spool}.incomplete")
    marker.write_text(code, encoding="utf-8")
    os.chmod(marker, 0o600)


def _read_records(spool: Path) -> tuple[list[dict], int]:
    """Parsed records plus the count of unreadable (torn / non-dict) lines —
    the reader must surface a torn write as incompleteness, not silence."""
    if not spool.exists():
        return [], 0
    records: list[dict] = []
    unreadable = 0
    for line in spool.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            unreadable += 1
            continue
        if isinstance(item, dict):
            records.append(item)
        else:
            unreadable += 1
    return records, unreadable


def _next_sequence(spool: Path) -> int:
    """The next sequence number, tracked in a ``{spool}.seq`` sidecar.

    Deriving it from a record COUNT was the bug: a torn line is skipped by the
    reader, so the count under-reports and every later record reuses a live
    sequence number (which the reader then sorts by). The sidecar is one int
    read per event (also killing the O(n²) full-spool rescan); a missing or
    unreadable sidecar (first event, or a legacy spool) seeds from the highest
    sequence actually on disk — max, not count, so a torn line can only leave
    a harmless gap, never a collision with a readable record.
    """
    seq_path = Path(f"{spool}.seq")
    try:
        value = int(seq_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        records, _ = _read_records(spool)
        value = max(
            (r["sequence"] for r in records if isinstance(r.get("sequence"), int)),
            default=0,
        )
    return value + 1


def _append(spool: Path, *, provider: str, tool_key: str, outcome: str) -> None:
    sequence = _next_sequence(spool)
    record = {
        "source": provider,
        "sequence": sequence,
        "tool_key": tool_key,
        "outcome": outcome,
    }
    spool.parent.mkdir(parents=True, exist_ok=True)
    # Reserve the sequence BEFORE appending the record: a crash in between
    # skips a number (harmless gap); the reverse order could reuse one.
    seq_path = Path(f"{spool}.seq")
    seq_path.write_text(str(sequence), encoding="utf-8")
    os.chmod(seq_path, 0o600)
    with spool.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")
    os.chmod(spool, 0o600)


def collect_hook_event(
    *,
    provider: str,
    event: str,
    spool: Path,
    payload: dict,
    tool_map: dict[str, str] | None = None,
) -> None:
    """Project a provider hook payload without ever persisting its body fields."""
    lock = Path(f"{spool}.lock")
    state_path = Path(f"{spool}.state")
    with _locked(lock):
        if provider == "kimi-code":
            raw_name = payload.get("tool_name")
            if not isinstance(raw_name, str):
                _mark_incomplete(spool, "missing_tool_name")
                return
            tool_key = normalize_tool_key(raw_name, tool_map)
            if tool_key is None:
                _mark_incomplete(spool, "unqualified_tool_name")
                return
            outcome = "failed" if event == "failure" else "succeeded"
            _append(spool, provider=provider, tool_key=tool_key, outcome=outcome)
            return

        if provider != "antigravity":
            _mark_incomplete(spool, "unknown_provider")
            return

        conversation = payload.get("conversationId")
        step = payload.get("stepIdx")
        if not isinstance(conversation, str) or not isinstance(step, int):
            _mark_incomplete(spool, "missing_correlation")
            return
        correlation = f"{conversation}:{step}"
        state = {}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _mark_incomplete(spool, "invalid_state")
                return
        if event == "pre":
            tool_call = payload.get("toolCall")
            raw_name = tool_call.get("name") if isinstance(tool_call, dict) else None
            tool_key = normalize_tool_key(raw_name, tool_map) if isinstance(raw_name, str) else None
            if tool_key is None:
                _mark_incomplete(spool, "unqualified_tool_name")
                return
            state[correlation] = tool_key
            state_path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
            os.chmod(state_path, 0o600)
            return
        tool_key = state.pop(correlation, None)
        if not isinstance(tool_key, str):
            _mark_incomplete(spool, "missing_pre_event")
            return
        state_path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
        os.chmod(state_path, 0o600)
        outcome = "failed" if event == "failure" or bool(payload.get("error")) else "succeeded"
        _append(spool, provider=provider, tool_key=tool_key, outcome=outcome)


def load_hook_telemetry(spool: Path, *, source: str) -> ToolTelemetry:
    calls: list[ToolCallRecord] = []
    records, malformed = _read_records(spool)
    for item in records:
        try:
            outcome = ToolOutcome(item["outcome"])
            calls.append(
                ToolCallRecord(
                    sequence=int(item["sequence"]),
                    tool_key=str(item["tool_key"]),
                    outcome=outcome,
                )
            )
        except (KeyError, TypeError, ValueError):
            malformed += 1
    incomplete = Path(f"{spool}.incomplete").exists()
    return ToolTelemetry(
        source=source,
        complete=not incomplete and malformed == 0,
        calls=tuple(sorted(calls, key=lambda call: call.sequence)),
        malformed_count=malformed,
    )
