"""Textual widgets and renderers for project sessions and comparison rooms."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea

from syncmill.config import SyncmillConfig
from syncmill.session import interactive_agents


class _CollabModal(ModalScreen[dict | bool | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    def action_cancel(self) -> None:
        self.dismiss(None)


class SessionOpenModal(_CollabModal):
    def __init__(self, config: SyncmillConfig):
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        choices = [
            (agent, agent)
            for agent in self._config.agent_priority
            if agent in interactive_agents()
        ]
        with Vertical(classes="form-dialog"):
            yield Label("Open isolated native CLI session")
            if not choices:
                yield Static("No interactive agents are configured.", classes="warning")
                yield Button("Cancel", id="cancel")
                return
            yield Select(choices, value=choices[0][1], allow_blank=False, id="agent")
            yield Static(
                "The provider runs in a persistent isolated worktree. "
                "Attach separately; source changes require checkpoint + apply.",
                classes="warning",
            )
            with Horizontal(classes="form-buttons"):
                yield Button("Open", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        value = self.query_one("#agent", Select).value
        self.dismiss({"agent": value})


class RoomCreateModal(_CollabModal):
    def __init__(self, config: SyncmillConfig):
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        enabled = [
            agent
            for agent in self._config.agent_priority
            if self._config.agent_config(agent).enabled
        ][:3]
        with Vertical(classes="wide-dialog"):
            yield Label("Create comparison room")
            yield TextArea(id="room-prompt", language=None)
            yield Input(value=", ".join(enabled), id="room-agents")
            yield Static(
                "Creating a room records its prompt and members without calling a "
                "provider. Starting a round with x makes one isolated provider call "
                "per listed agent and preserves every candidate.",
                classes="warning",
            )
            with Horizontal(classes="form-buttons"):
                yield Button("Create", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        self.dismiss(
            {
                "prompt": self.query_one("#room-prompt", TextArea).text,
                "agents": self.query_one("#room-agents", Input).value,
            }
        )


class CollaborationConfirmModal(_CollabModal):
    def __init__(self, title: str, detail: str, action: str):
        super().__init__()
        self._title = title
        self._detail = detail
        self._action = action

    def compose(self) -> ComposeResult:
        with Vertical(classes="wide-dialog"):
            yield Label(self._title)
            yield Static(self._detail)
            yield Checkbox("I reviewed this exact action", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button(self._action, variant="warning", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the action", severity="warning")
            return
        self.dismiss(True)


def _patch_is_binary(head: bytes) -> bool:
    """Whether a patch head carries binary content. Derived — no stored flag:
    patches are captured with `git diff --binary`, whose binary hunks are
    marked in-band."""
    return (
        b"GIT binary patch" in head
        or b"\nBinary files " in head
        or head.startswith(b"Binary files ")
        or b"\x00" in head
    )


def render_session(value: dict) -> str:
    return "\n".join(
        [
            f"Session: {value.get('session_id')}",
            f"Agent: {value.get('agent_id')}",
            f"State: {value.get('state')} | live: {value.get('live', False)}",
            f"Source: {'STALE' if value.get('source_stale') else 'clean snapshot'}",
            f"Worktree: {value.get('worktree_path')}",
            f"Checkpoints: {', '.join(value.get('checkpoints', [])) or '-'}",
            "",
            "x attach | c checkpoint | k close",
        ]
    )


def render_room(room: dict, candidates: list[dict], events: list[dict]) -> str:
    members = ", ".join(member["member_id"] for member in room.get("members", []))
    tail = events[-5:]
    lines = [
        f"Room: {room.get('id')}",
        f"State: {room.get('state')} | revision: {room.get('revision')}",
        f"Rounds: {room.get('round_count')} | members: {members}",
        f"Selected: {room.get('selected_candidate_id') or '-'}",
        f"Apply run: {room.get('apply_run_id') or '-'}",
        f"Applied run: {room.get('applied_run_id') or '-'}",
        "",
        "Recent events:",
        *(f"{event['sequence']} {event['kind']}" for event in tail),
        "",
        "x run round | e select candidate | g apply selected",
    ]
    return "\n".join(lines)


def _gate_label(gate: dict | None) -> str:
    if gate is None:
        return "not configured"
    return "passed" if gate.get("passed") else "failed"


def render_candidate_diff(
    manifest: dict,
    patch_head: bytes,
    *,
    truncated: bool,
    binary: bool | None = None,
) -> str:
    """The candidate Diff view: the digest-bound patch window, binary-marked.

    ``binary`` is the loader's whole-stream verdict (a binary hunk may sit
    beyond the displayed head); ``None`` falls back to sniffing the head.
    """
    lines = [
        f"Candidate: {manifest.get('candidate_id')}  [Diff]",
        f"Patch: {manifest.get('patch_bytes')} bytes {manifest.get('patch_sha256')}",
    ]
    if _patch_is_binary(patch_head) if binary is None else binary:
        lines.append("[contains binary patch content]")
    lines.extend(["", patch_head.decode("utf-8", errors="replace") or "(empty patch)"])
    if truncated:
        lines.append("\n[... truncated — press d again to load more ...]")
    return "\n".join(lines)


def _usage_line(manifest: dict) -> str | None:
    """A ``Usage: in 40.1k (cache 85%), out 2.3k, $0.42`` line from the
    manifest's optional best-effort ``usage`` block — None (omit the line)
    when absent or malformed. Every part is individually optional."""
    raw = manifest.get("usage")
    if not isinstance(raw, dict):
        return None

    def _k(n: int) -> str:
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    def _int(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    total_input = _int(raw.get("total_input_tokens"))
    output_tokens = _int(raw.get("output_tokens"))
    cache_read = _int(raw.get("cache_read_tokens"))
    cost = raw.get("total_cost_usd")
    parts = []
    # OverflowError: an absurd int from a corrupt manifest overflows the float
    # divisions/formatting below, and this renderer sits outside the poll's
    # catch set — malformed telemetry must omit the line, never kill the TUI.
    try:
        if total_input is not None:
            head = f"in {_k(total_input)}"
            if cache_read is not None and total_input > 0:
                head += f" (cache {cache_read / total_input:.0%})"
            parts.append(head)
        if output_tokens is not None:
            parts.append(f"out {_k(output_tokens)}")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            parts.append(f"${cost:.2f}")
    except OverflowError:
        return None
    return f"Usage: {', '.join(parts)}" if parts else None


def render_candidate_output(manifest: dict) -> str:
    """The candidate Output view: state/error/gate first, then the bounded
    output — a failed candidate's review starts with why it failed."""
    lines = [
        f"Candidate: {manifest.get('candidate_id')}  [Output]",
        f"State: {manifest.get('state')} | exit: {manifest.get('exit_code')} | "
        f"{manifest.get('duration_seconds')}s",
    ]
    if manifest.get("error"):
        lines.append(f"Error: {manifest['error']}")
    lines.append(f"Gate: {_gate_label(manifest.get('gate'))}")
    usage = _usage_line(manifest)
    if usage:
        lines.append(usage)
    if manifest.get("output_truncated"):
        lines.append("[stored output was truncated at capture]")
    # `or ""`: a manifest with an explicit `"output": null` must render, not
    # TypeError out of the room poll.
    output = manifest.get("output") or ""
    if len(output) > 100_000:
        output = output[:50_000] + "\n\n[... display truncated ...]\n\n" + output[-50_000:]
    lines.extend(["", output or "(no output)"])
    return "\n".join(lines)


def render_candidate_details(
    manifest: dict, round_number: int | None, *, patch_binary: bool
) -> str:
    """The candidate Details view: full metadata incl. per-check gate results,
    digests, and truncation state."""
    gate = manifest.get("gate")
    gate_lines = [f"Gate: {_gate_label(gate)}"]
    if gate is not None:
        gate_lines[0] += f" (score: {gate.get('score')})"
        for check in gate.get("checks") or []:
            gate_lines.append(
                f"  {'✓' if check.get('passed') else '×'} {check.get('command')} "
                f"(exit {check.get('exit_code')}, {check.get('duration_seconds')}s)"
            )
    lines = [
        f"Candidate: {manifest.get('candidate_id')}  [Details]",
        f"Member: {manifest.get('member_id')} ({manifest.get('provider_id')})",
        f"Round: {round_number if round_number is not None else '?'} | "
        f"attempt: {manifest.get('attempt')}",
        f"State: {manifest.get('state')} | eligible: {manifest.get('eligible')}",
        f"Exit: {manifest.get('exit_code')} | duration: {manifest.get('duration_seconds')}s",
    ]
    if manifest.get("error"):
        lines.append(f"Error: {manifest['error']}")
    lines.extend(gate_lines)
    lines.extend(
        [
            # `or []`: an explicit `"files_changed": null` reaches this view
            # verbatim (load_candidate_patch_head does not rebuild the field),
            # and str.join(None) would blank the pane via the poll's catch set.
            f"Files: {', '.join(manifest.get('files_changed') or []) or '-'}",
            f"Patch: {manifest.get('patch_bytes')} bytes | "
            f"binary: {'yes' if patch_binary else 'no'}",
            f"Patch sha256: {manifest.get('patch_sha256')}",
            f"Output: {manifest.get('output_bytes')} bytes | "
            f"truncated: {bool(manifest.get('output_truncated'))}",
            f"Output sha256: {manifest.get('output_sha256')}",
            f"Session: {manifest.get('provider_session_id') or '-'}",
            f"Created: {manifest.get('created_at')} | base: {manifest.get('base_commit')}",
        ]
    )
    usage = _usage_line(manifest)
    if usage:
        lines.append(usage)
    return "\n".join(lines)
