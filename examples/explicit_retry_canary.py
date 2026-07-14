#!/usr/bin/env python3
"""Run a deterministic SyncMill trace with optional explicit retry causality.

This is an operations canary, not a provider parser. It exercises the real Supervisor,
RouteStrategy, file tracer, and optional Phoenix tee without invoking a paid coding agent.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from syncmill.config import SyncmillConfig
from syncmill.models import (
    Task,
    TaskResult,
    TaskStatus,
    ToolCallRecord,
    ToolOutcome,
    ToolTelemetry,
)
from syncmill.supervisor import Supervisor


class _CanaryAgent:
    # Reuse a configured runner identity so the normal timeout/config lookup remains real.
    # The registry is replaced below, so no Codex subprocess is invoked.
    agent_id = "codex"

    def __init__(self, mode: str) -> None:
        self._mode = mode

    def execute(self, prompt: str, workdir: Path, timeout: int) -> TaskResult:
        first = ToolCallRecord(1, "syncmill::canary_probe", ToolOutcome.SUCCEEDED)
        if self._mode == "baseline":
            calls = (first,)
            status = TaskStatus.COMPLETED
            exit_code = 0
            error = None
        else:
            calls = (
                first,
                ToolCallRecord(
                    2,
                    "syncmill::canary_probe",
                    ToolOutcome.FAILED,
                    retry_of_sequence=1,
                ),
            )
            status = TaskStatus.FAILED
            exit_code = 1
            error = "controlled_canary_failure"
        return TaskResult(
            task_id=uuid4(),
            agent_id=self.agent_id,
            status=status,
            output="",
            exit_code=exit_code,
            duration_seconds=0.001,
            error=error,
            tool_telemetry=ToolTelemetry(
                source="controlled-canary",
                complete=True,
                calls=calls,
            ),
        )


class _CanaryRegistry:
    def __init__(self, agent: _CanaryAgent) -> None:
        self._agent = agent

    def available(self) -> list[_CanaryAgent]:
        return [self._agent]

    def get(self, agent_id: str) -> _CanaryAgent | None:
        return self._agent if agent_id == self._agent.agent_id else None


class _NullMemory:
    degraded = False

    async def register_agent(self, agent_id: str) -> None:
        return None

    async def search_context(self, query: str) -> str:
        return ""

    async def start_task_session(self, agent_id: str, task: Task) -> str:
        return "retry-canary-session"

    async def share_result(self, task: Task, result: TaskResult) -> None:
        return None

    async def end_task_session(self, session_id: str, result: TaskResult) -> None:
        return None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("baseline", "retry-failure"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--exporter", choices=("file", "otlp-http"), default="file")
    parser.add_argument("--endpoint", default="")
    parser.add_argument("--project", default="tracegraph-syncmill-e2e")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    output_dir = args.output_dir.resolve()
    config = SyncmillConfig(
        repo_root=str(args.repo_root.resolve()),
        agent_priority=["codex"],
        workspace={"root": str(output_dir / "workspaces"), "retain_on_failure": False},
        runs={"root": str(output_dir / "artifacts")},
        integration={
            "report": {
                "enabled": True,
                "runs_dir": str(output_dir / "runs"),
                "keep_last": 10,
            },
            "trace": {
                "enabled": True,
                "exporter": args.exporter,
                "dir": str(output_dir / "traces"),
                "endpoint": args.endpoint,
                "project_name": args.project,
                "batch_delay_seconds": 0.1,
                "timeout_seconds": 5,
                "tool_calls_enabled": True,
            },
        },
    )
    supervisor = Supervisor(config)
    supervisor._registry = _CanaryRegistry(_CanaryAgent(args.mode))
    supervisor._memory = _NullMemory()
    result = supervisor.run(Task(prompt="controlled retry causality canary"))
    run_id = supervisor.last_run_id
    if run_id is None:
        raise RuntimeError("Supervisor did not assign a run_id")
    trace_path = output_dir / "traces" / run_id / "trace.otlp.json"
    if not trace_path.is_file():
        raise RuntimeError(f"trace artifact was not written: {trace_path}")
    print(
        json.dumps(
            {
                "mode": args.mode,
                "run_id": run_id,
                "status": result.status.value,
                "trace_path": str(trace_path),
            },
            sort_keys=True,
        )
    )
    expected = TaskStatus.COMPLETED if args.mode == "baseline" else TaskStatus.FAILED
    return 0 if result.status is expected else 1


if __name__ == "__main__":
    raise SystemExit(main())
