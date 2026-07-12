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


def _read_records(spool: Path) -> list[dict]:
    if not spool.exists():
        return []
    records: list[dict] = []
    for line in spool.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _append(spool: Path, *, provider: str, tool_key: str, outcome: str) -> None:
    records = _read_records(spool)
    record = {
        "source": provider,
        "sequence": len(records) + 1,
        "tool_key": tool_key,
        "outcome": outcome,
    }
    spool.parent.mkdir(parents=True, exist_ok=True)
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
    malformed = 0
    for item in _read_records(spool):
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
