"""Control-plan projection and Toolgraph batch-consumer tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import jsonschema
import pytest

from syncmill.config import SyncmillConfig, ToolgraphConfig
from syncmill.planning import (
    build_control_plan,
    build_run_plan,
    build_workflow_control_plan,
)
from syncmill.preflight import (
    CONTROL_PLAN_FILE,
    CONTROL_PREFLIGHT_FILE,
    canonical_control_plan,
    run_preflight,
)


def _syncmill_config(strategy: str = "compete", **toolgraph) -> SyncmillConfig:
    tg = {
        "enabled": True,
        "mode": "strict",
        "control_plan_enabled": True,
        "candidate_tools": ["workspace::read", "workspace::write"],
        "agent_map": {"claude": "claude", "codex": "codex"},
        **toolgraph,
    }
    return SyncmillConfig(
        default_strategy=strategy,
        integration={"report": {"enabled": True}, "toolgraph": tg},
    )


@pytest.mark.parametrize(
    "strategy", ["route", "pipeline", "compete", "council", "decompose"]
)
def test_control_plan_matches_strategy_call_bound(strategy):
    config = _syncmill_config(strategy)
    agents = ["claude", "codex"]
    plan = build_control_plan(config, agents, run_id="run-1")
    expected = build_run_plan(config, agents).maximum_agent_calls
    declared = sum(
        node.get("max_invocations", 0) for node in plan["nodes"] if node["agent_call"]
    )
    assert declared == expected == plan["limits"]["maximum_agent_calls"]
    assert plan["nodes"][0]["kind"] == "start"
    assert plan["nodes"][-1]["kind"] == "end"
    assert plan["limits"]["maximum_cycles"] == 0


def test_control_plan_is_body_free_but_declares_validator_presence():
    config = _syncmill_config("compete")
    config.gate.commands = ["pytest -q", "ruff check ."]
    plan = build_control_plan(config, ["claude", "codex"], run_id="run-2")
    serialized = json.dumps(plan)
    assert "pytest" not in serialized and "ruff" not in serialized
    assert "prompt" not in serialized and "output" not in serialized
    validator = next(node for node in plan["nodes"] if node["kind"] == "validator")
    assert validator["validator_scope"] == "workspace_result"


def test_decompose_model_fanout_is_explicitly_bounded():
    config = _syncmill_config("decompose")
    config.decompose.max_subtasks = 7
    plan = build_control_plan(config, ["claude", "codex"], run_id="run-3")
    edge = next(row for row in plan["edges"] if row["to"] == "subtasks")
    assert edge == {
        "from": "plan",
        "to": "subtasks",
        "kind": "fanout",
        "decided_by": "model",
        "max_fan_out": 7,
    }


def test_judge_falls_back_to_lead_and_stays_declared():
    config = _syncmill_config("compete")
    config.selection.mode = "judge"
    config.selection.judge_agent = "not-in-lineup"
    plan = build_control_plan(config, ["claude", "codex"], run_id="run-4")
    judge = next(node for node in plan["nodes"] if node["id"] == "judge")
    assert judge["principals"] == ["claude"]
    assert plan["limits"]["maximum_agent_calls"] == 3


def test_unmapped_participant_is_not_silently_omitted():
    config = _syncmill_config("route")
    plan = build_control_plan(config, ["claude", "kimi-code"], run_id="run-5")
    ids = [row["id"] for row in plan["principals"]]
    assert ids[0] == "claude"
    assert ids[1].startswith("syncmill-unmapped-")


def _control_cfg(*, mode="strict", **overrides) -> ToolgraphConfig:
    values = {
        "enabled": True,
        "mode": mode,
        "control_plan_enabled": True,
        "candidate_tools": ["workspace::read", "workspace::write"],
        "agent_map": {"claude": "claude", "codex": "codex"},
        **overrides,
    }
    return ToolgraphConfig(**values)


def _fake_control_run(*, findings=None, rejected=None, mutate=None):
    seen: dict = {}

    def fake(argv, *args, **kwargs):
        seen["argv"] = argv
        plan_path = Path(argv[2])
        raw_plan = plan_path.read_bytes()
        plan = json.loads(raw_plan)
        evaluations = []
        for principal in plan["principals"]:
            rows = list(rejected or []) if principal["id"] == "codex" else []
            rejected_candidates = {row["candidate"] for row in rows}
            evaluations.append(
                {
                    "principal": principal["id"],
                    "agent_found": not principal["id"].startswith("syncmill-unmapped-"),
                    "eligible": (
                        []
                        if principal["id"].startswith("syncmill-unmapped-")
                        else [
                            tool
                            for tool in principal["candidate_tools"]
                            if tool not in rejected_candidates
                        ]
                    ),
                    "rejected": rows,
                }
            )
        warnings = bool(findings) or any(
            not row["agent_found"] or row["rejected"] for row in evaluations
        )
        doc = {
            "schema_version": 1,
            "kind": "toolgraph.control-preflight",
            "run_id": plan["run_id"],
            "created_at": "2026-08-12T00:00:00+00:00",
            "mode": "advisory",
            "profile": argv[argv.index("--profile") + 1],
            "plan_digest": f"sha256:{hashlib.sha256(raw_plan).hexdigest()}",
            "graph_state": {"instance_id": "graph-1", "generation": 9},
            "decision": "advisory_warn" if warnings else "advisory_allow",
            "evaluations": evaluations,
            "findings": list(findings or []),
        }
        if mutate is not None:
            mutate(doc)
        out = Path(argv[argv.index("--out") + 1])
        out.write_text(json.dumps(doc), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    fake.seen = seen
    return fake


def _plan() -> dict:
    return build_control_plan(
        _syncmill_config(), ["claude", "codex"], run_id="control-run"
    )


def test_workflow_control_plan_projects_fanout_join_retries_and_gate():
    config = _syncmill_config()
    config.workflow.max_parallel = 2
    config.gate.commands = ["pytest -q"]
    workflow_nodes = [
        {
            "id": "inspect",
            "agent": "claude",
            "needs": [],
            "max_attempts": 2,
            "timeout_seconds": 120,
        },
        {
            "id": "research",
            "agent": "codex",
            "needs": [],
            "max_attempts": 1,
            "timeout_seconds": 180,
        },
        {
            "id": "final",
            "agent": "claude",
            "needs": ["inspect", "research"],
            "max_attempts": 3,
            "timeout_seconds": 240,
        },
    ]
    plan = build_workflow_control_plan(
        config,
        workflow_nodes,
        run_id="wf-0123456789abcdef",
        final_node="final",
    )

    schema = json.loads(
        (
            Path(__file__).parent / "fixtures/toolgraph/control-plan.schema.json"
        ).read_text()
    )
    jsonschema.Draft202012Validator(schema).validate(plan)
    serialized = canonical_control_plan(plan)
    assert serialized == canonical_control_plan(plan)
    assert b"prompt" not in serialized and b"pytest" not in serialized
    assert plan["limits"] == {
        "maximum_agent_calls": 6,
        "maximum_parallelism": 2,
        "maximum_cycles": 0,
    }
    assert any(node["id"] == "control:fanout:roots" for node in plan["nodes"])
    assert any(node["id"] == "control:join:final" for node in plan["nodes"])
    assert any(node["id"] == "control:validator:final" for node in plan["nodes"])
    final = next(node for node in plan["nodes"] if node["id"] == "workflow:final")
    assert final["max_invocations"] == 3
    assert final["timeout_seconds"] == 240


def test_control_preflight_persists_frozen_plan_bytes_exactly(monkeypatch, tmp_path):
    fake = _fake_control_run()
    monkeypatch.setattr("syncmill.preflight._sync_run", fake)
    frozen = canonical_control_plan(_plan())

    outcome = run_preflight(
        _control_cfg(),
        run_id="control-run",
        agent_ids=["claude", "codex"],
        out_dir=tmp_path,
        control_plan=frozen,
    )

    assert outcome is not None and not outcome.should_block
    assert (tmp_path / CONTROL_PLAN_FILE).read_bytes() == frozen
    assert outcome.block["producer_decision"] == "advisory_allow"


def test_strict_control_preflight_is_one_call_and_persists_exact_evidence(
    monkeypatch, tmp_path
):
    fake = _fake_control_run()
    monkeypatch.setattr("syncmill.preflight._sync_run", fake)
    outcome = run_preflight(
        _control_cfg(),
        run_id="control-run",
        agent_ids=["claude", "codex"],
        out_dir=tmp_path,
        control_plan=_plan(),
    )
    assert outcome is not None and outcome.should_block is False
    assert outcome.block["enforcement_decision"] == "allow"
    assert outcome.block["graph_state"] == {"instance_id": "graph-1", "generation": 9}
    assert outcome.artifact_path == tmp_path / CONTROL_PREFLIGHT_FILE
    assert (tmp_path / CONTROL_PLAN_FILE).exists()
    assert fake.seen["argv"][:2] == ["toolgraph", "control-preflight"]


def test_strict_control_finding_blocks_before_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "syncmill.preflight._sync_run",
        _fake_control_run(findings=[{"code": "CALL_BOUND_TOO_LOW"}]),
    )
    outcome = run_preflight(
        _control_cfg(),
        run_id="control-run",
        agent_ids=["claude", "codex"],
        out_dir=tmp_path,
        control_plan=_plan(),
    )
    assert outcome is not None and outcome.should_block
    assert outcome.block_reasons == ("CONTROL_PLAN_FINDING",)
    assert outcome.block["enforcement_decision"] == "block"


def test_strict_control_applies_existing_reason_policies(monkeypatch, tmp_path):
    rejected = [
        {
            "candidate": "workspace::write",
            "reason": "DRIFTED",
            "tool_key": "workspace::write",
        }
    ]
    monkeypatch.setattr(
        "syncmill.preflight._sync_run", _fake_control_run(rejected=rejected)
    )
    outcome = run_preflight(
        _control_cfg(),
        run_id="control-run",
        agent_ids=["claude", "codex"],
        out_dir=tmp_path,
        control_plan=_plan(),
    )
    assert outcome is not None and outcome.should_block is False
    assert outcome.block["warning_count"] == 1
    blocked = run_preflight(
        _control_cfg(drift_policy="block"),
        run_id="control-run",
        agent_ids=["claude", "codex"],
        out_dir=tmp_path,
        control_plan=_plan(),
    )
    assert blocked is not None and blocked.should_block
    assert blocked.block_reasons == ("DRIFTED",)


def test_digest_mismatch_is_contract_invalid_and_not_persisted(monkeypatch, tmp_path):
    def corrupt(doc):
        doc["plan_digest"] = "sha256:" + "0" * 64

    monkeypatch.setattr(
        "syncmill.preflight._sync_run", _fake_control_run(mutate=corrupt)
    )
    outcome = run_preflight(
        _control_cfg(),
        run_id="control-run",
        agent_ids=["claude", "codex"],
        out_dir=tmp_path,
        control_plan=_plan(),
    )
    assert outcome is not None and outcome.should_block
    assert outcome.block_reasons == ("CONTRACT_INVALID",)
    assert not (tmp_path / CONTROL_PLAN_FILE).exists()
    assert not (tmp_path / CONTROL_PREFLIGHT_FILE).exists()


def test_advisory_control_unavailability_warns_and_continues(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "syncmill.preflight._sync_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )
    outcome = run_preflight(
        _control_cfg(mode="advisory"),
        run_id="control-run",
        agent_ids=["claude", "codex"],
        out_dir=tmp_path,
        control_plan=_plan(),
    )
    assert outcome is not None and outcome.should_block is False
    assert outcome.block["status"] == "error"
    assert any("not found" in warning for warning in outcome.warnings)
