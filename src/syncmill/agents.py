"""Agent abstraction — wraps CLI tools as pure functions (prompt in, result out)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from time import monotonic
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from syncmill.config import AgentConfig, SyncmillConfig
from syncmill.models import TaskResult, TaskStatus

# ANSI escape sequence stripper (pattern from autocli)
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _clean_output(raw: str | bytes | None) -> str:
    """ANSI-strip and trim output that may be str, bytes, or None.

    subprocess.run yields *bytes* in TimeoutExpired.stdout even with text=True,
    so the timeout path must decode before ANSI-stripping.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return _strip_ansi(raw).strip()


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class AgentRunner(Protocol):
    """Any CLI agent that can execute a coding task.

    Agents are pure functions: prompt in, TaskResult out.
    They know nothing about memtomem, other agents, or the Supervisor.
    """

    @property
    def agent_id(self) -> str: ...

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult: ...


# ── Base implementation ──────────────────────────────────────────────────


class CLIAgent:
    """Base class for CLI-based agents using subprocess.run."""

    def __init__(self, agent_id: str, config: AgentConfig):
        self._agent_id = agent_id
        self._config = config

    @property
    def agent_id(self) -> str:
        return self._agent_id

    def _build_command(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def _parse_output(self, stdout: str) -> str:
        """Extract meaningful output from CLI stdout. Override per agent."""
        return _strip_ansi(stdout).strip()

    def _is_error_output(self, stdout: str) -> str | None:
        """Check if CLI output indicates an error. Override per agent.
        Returns error message if error, None if OK."""
        return None

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        task_id = uuid4()
        cmd = self._build_command(prompt)
        start = monotonic()

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(workdir),
                timeout=timeout,
                env={**os.environ},
            )
            duration = monotonic() - start
            output = self._parse_output(proc.stdout)

            # Check for CLI-specific error indicators in stdout
            output_error = self._is_error_output(proc.stdout)
            is_failed = proc.returncode != 0 or output_error is not None
            error_msg = output_error or (_strip_ansi(proc.stderr).strip() or None) if is_failed else None

            return TaskResult(
                task_id=task_id,
                agent_id=self._agent_id,
                status=TaskStatus.FAILED if is_failed else TaskStatus.COMPLETED,
                output=output,
                exit_code=proc.returncode,
                duration_seconds=round(duration, 2),
                error=error_msg,
            )
        except subprocess.TimeoutExpired as exc:
            duration = monotonic() - start
            # subprocess.run already SIGKILLs the timed-out child before raising;
            # nothing further to terminate here.
            return TaskResult(
                task_id=task_id,
                agent_id=self._agent_id,
                status=TaskStatus.FAILED,
                output=_clean_output(exc.stdout),
                exit_code=-1,
                duration_seconds=round(duration, 2),
                error=f"Timed out after {timeout}s",
            )
        except FileNotFoundError:
            return TaskResult(
                task_id=task_id,
                agent_id=self._agent_id,
                status=TaskStatus.FAILED,
                output="",
                exit_code=-1,
                duration_seconds=0.0,
                error=f"CLI tool '{cmd[0]}' not found in PATH",
            )
        except OSError as exc:
            # PermissionError, bad cwd, ENOEXEC, etc. — don't crash the task.
            return TaskResult(
                task_id=task_id,
                agent_id=self._agent_id,
                status=TaskStatus.FAILED,
                output="",
                exit_code=-1,
                duration_seconds=round(monotonic() - start, 2),
                error=f"Failed to launch '{cmd[0]}': {exc}",
            )


# ── Claude Code ──────────────────────────────────────────────────────────


class ClaudeAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("claude", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        return [
            "claude",
            "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(self._config.max_turns),
        ]

    def _parse_output(self, stdout: str) -> str:
        """Claude --output-format json → {"type":"result","result":"...",...}"""
        cleaned = _strip_ansi(stdout).strip()
        parsed = _try_parse_json(cleaned)
        if isinstance(parsed, dict):
            return parsed.get("result", "").strip()
        return cleaned

    def _is_error_output(self, stdout: str) -> str | None:
        parsed = _try_parse_json(_strip_ansi(stdout).strip())
        if isinstance(parsed, dict) and parsed.get("is_error"):
            return parsed.get("result", "Unknown Claude error")
        return None


# ── Gemini CLI ───────────────────────────────────────────────────────────


class GeminiAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("gemini", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        return [
            "gemini",
            "-p", prompt,
            "--output-format", "json",
            "--yolo",
        ]

    def _parse_output(self, stdout: str) -> str:
        """Gemini --output-format json returns a JSON object."""
        cleaned = _strip_ansi(stdout).strip()
        parsed = _try_parse_json(cleaned)
        if isinstance(parsed, dict):
            return parsed.get("modelResponse", parsed.get("response", json.dumps(parsed)))
        return cleaned


# ── Codex CLI ────────────────────────────────────────────────────────────


class CodexAgent(CLIAgent):
    def __init__(self, config: AgentConfig | None = None):
        super().__init__("codex", config or AgentConfig())

    def _build_command(self, prompt: str) -> list[str]:
        return [
            "codex", "exec",
            prompt,
            "--json",
            "--full-auto",
        ]

    def _parse_output(self, stdout: str) -> str:
        """Codex --json returns JSONL (one JSON object per line)."""
        cleaned = _strip_ansi(stdout).strip()
        messages: list[str] = []
        for line in cleaned.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                # Codex JSONL: look for agent_message items
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        text = item.get("text", "").strip()
                        if text:
                            messages.append(text)
            except json.JSONDecodeError:
                continue
        return "\n".join(messages) if messages else cleaned


# ── Helpers ──────────────────────────────────────────────────────────────


def _try_parse_json(text: str) -> dict | list | None:
    """Try to parse text as JSON. Returns parsed object or None."""
    # Try the whole text first
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # Try line-by-line for JSONL or mixed output
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith(("{", "[")):
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


# ── Registry ─────────────────────────────────────────────────────────────

_AGENT_CONSTRUCTORS: dict[str, type[CLIAgent]] = {
    "claude": ClaudeAgent,
    "gemini": GeminiAgent,
    "codex": CodexAgent,
}


class AgentRegistry:
    """Manages agent instances ordered by config priority."""

    def __init__(self, config: SyncmillConfig):
        unknown = [a for a in config.agent_priority if a not in _AGENT_CONSTRUCTORS]
        if unknown:
            valid = ", ".join(sorted(_AGENT_CONSTRUCTORS))
            raise ValueError(
                f"Unknown agent id(s) in agent_priority: {', '.join(unknown)}. "
                f"Valid agents: {valid}."
            )
        self._agents: list[AgentRunner] = []
        for agent_id in config.agent_priority:
            agent_cfg = config.agent_config(agent_id)
            if not agent_cfg.enabled:
                continue
            self._agents.append(_AGENT_CONSTRUCTORS[agent_id](agent_cfg))

    def available(self) -> list[AgentRunner]:
        """Return enabled agents in priority order."""
        return list(self._agents)

    def get(self, agent_id: str) -> AgentRunner | None:
        """Get a specific agent by id."""
        for agent in self._agents:
            if agent.agent_id == agent_id:
                return agent
        return None
