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
                "One isolated provider call per listed agent. The first slice "
                "allows each built-in provider once and preserves every candidate.",
                classes="warning",
            )
            yield Checkbox("I understand the provider calls and want to create it", id="confirmed")
            with Horizontal(classes="form-buttons"):
                yield Button("Create", variant="primary", id="apply")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "apply":
            self.dismiss(None)
            return
        if not self.query_one("#confirmed", Checkbox).value:
            self.app.notify("confirm the provider-call warning", severity="warning")
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


def render_candidate(manifest: dict) -> str:
    gate = manifest.get("gate")
    gate_label = "not configured" if gate is None else ("passed" if gate["passed"] else "failed")
    output = manifest.get("output", "")
    if len(output) > 100_000:
        output = output[:50_000] + "\n\n[... display truncated ...]\n\n" + output[-50_000:]
    return "\n".join(
        [
            f"Candidate: {manifest.get('candidate_id')}",
            f"Member: {manifest.get('member_id')} ({manifest.get('provider_id')})",
            f"State: {manifest.get('state')} | eligible: {manifest.get('eligible')}",
            f"Gate: {gate_label}",
            f"Patch: {manifest.get('patch_bytes')} bytes {manifest.get('patch_sha256')}",
            f"Files: {', '.join(manifest.get('files_changed', [])) or '-'}",
            "",
            output,
        ]
    )
