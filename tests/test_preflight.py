"""run_preflight (S5) — advisory toolgraph preflight against the vendored goldens.

Hermetic: ``syncmill.preflight.subprocess.run`` is monkeypatched (the lookup
site) with a fake that writes one vendored golden's bytes to the ``--out`` path
in argv, so the whole matrix runs with no toolgraph binary or Neo4j.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

import syncmill.preflight as preflight_module
from syncmill.config import ToolgraphConfig
from syncmill.preflight import (
    PREFLIGHT_FILE,
    _build_request,
    run_preflight,
)

VENDORED = Path(__file__).resolve().parent / "fixtures" / "toolgraph"
REQUEST_SCHEMA = Path(__file__).resolve().parent.parent / "contracts" / "preflight-request.schema.json"
STRICT_SCHEMA = Path(__file__).resolve().parent.parent / "contracts" / "strict-preflight-manifest.schema.json"
ENVELOPE_SCHEMA = Path(__file__).resolve().parent.parent / "contracts" / "run-envelope.schema.json"
ENVELOPE_FIXTURE = Path(__file__).resolve().parent.parent / "contracts" / "fixtures" / "envelope-direct.json"


def _golden(name: str) -> bytes:
    return (VENDORED / f"{name}.json").read_bytes()


def _cfg(**kw) -> ToolgraphConfig:
    base = dict(enabled=True, agent_map={"codex": "codex", "kimi-code": "kimi"})
    base.update(kw)
    return ToolgraphConfig(**base)


def _fake_run(golden_name: str | None = None, *, raw: bytes | None = None,
              returncode: int = 0, exc: BaseException | None = None):
    """Build a subprocess.run stand-in: writes the golden (or raw bytes) to the
    ``--out`` path found in argv, or raises ``exc``. Records the argv it saw."""
    seen: dict = {}

    def fake(argv, *args, **kwargs):
        seen["argv"] = argv
        if exc is not None:
            raise exc
        out_idx = argv.index("--out")
        out_path = Path(argv[out_idx + 1])
        payload = raw if raw is not None else _golden(golden_name)
        if payload is not None:
            out_path.write_bytes(payload)
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="boom")

    fake.seen = seen
    return fake


def _patch(monkeypatch, fake):
    monkeypatch.setattr("syncmill.preflight.subprocess.run", fake)


# ── disabled / mapping ───────────────────────────────────────────────────────


def test_disabled_returns_none(monkeypatch):
    called = []
    monkeypatch.setattr("syncmill.preflight.subprocess.run",
                        lambda *a, **k: called.append(a))
    assert run_preflight(ToolgraphConfig(), run_id="r", agent_ids=["codex"], out_dir=None) is None
    assert called == []  # disabled → no subprocess


def test_disabled_strict_rollback_returns_none_without_subprocess(monkeypatch):
    called = []
    monkeypatch.setattr(
        "syncmill.preflight.subprocess.run", lambda *a, **k: called.append(a)
    )
    cfg = ToolgraphConfig(enabled=False, mode="strict")
    assert run_preflight(cfg, run_id="r", agent_ids=["codex"], out_dir=None) is None
    assert called == []


def test_all_unmapped_is_unresolved_identity_no_subprocess(monkeypatch, tmp_path):
    fake = _fake_run("preflight-allow")
    _patch(monkeypatch, fake)
    out = run_preflight(_cfg(agent_map={}), run_id="r", agent_ids=["claude"], out_dir=tmp_path)
    assert out.block["status"] == "unresolved_identity"
    assert "argv" not in fake.seen  # subprocess never invoked
    assert any("agent_map" in w for w in out.warnings)


def test_unmapped_id_warns_but_lead_runs(monkeypatch, tmp_path):
    fake = _fake_run("preflight-allow")
    _patch(monkeypatch, fake)
    # claude is unmapped (warn), codex maps → lead
    out = run_preflight(_cfg(), run_id="r", agent_ids=["claude", "codex"], out_dir=tmp_path)
    assert out.block["status"] == "ok"
    assert any("claude" in w for w in out.warnings)
    assert fake.seen["argv"][2] == "codex"  # lead = mapped codex (after `command`)


def test_hyphenated_key_maps_verbatim(monkeypatch, tmp_path):
    fake = _fake_run("preflight-allow")
    _patch(monkeypatch, fake)
    run_preflight(_cfg(), run_id="r", agent_ids=["kimi-code"], out_dir=tmp_path)
    assert fake.seen["argv"][2] == "kimi"  # kimi-code → kimi verbatim


# ── decision → status matrix (vendored goldens) ──────────────────────────────


def test_allow_maps_ok_and_copies_artifact(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run("preflight-allow"))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "ok"
    assert out.block["warning_count"] == 0
    assert out.block["mode"] == "advisory"
    assert out.block["profile"] == "review"
    assert out.block["graph_generation"] == 42
    assert out.artifact_path == tmp_path / PREFLIGHT_FILE
    assert out.artifact_path.read_bytes() == _golden("preflight-allow")  # byte-identical
    import hashlib
    digest = hashlib.sha256(_golden("preflight-allow")).hexdigest()
    assert out.block["digest"] == f"sha256:{digest}"


def test_warn_maps_ok_with_warning_count(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run("preflight-warn"))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "ok"
    assert out.block["warning_count"] == 3  # three rejected rows in the golden
    assert any("rejected" in w for w in out.warnings)


def test_unresolved_identity_golden(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run("preflight-unresolved-identity"))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "unresolved_identity"
    assert any("identity" in w for w in out.warnings)


def test_additive_fields_tolerated(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run("preflight-additive"))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "ok"
    assert out.block["graph_generation"] == 43


def test_v2_rejected_no_artifact(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run("preflight-v2"))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "error"
    assert out.artifact_path is None
    assert not (tmp_path / PREFLIGHT_FILE).exists()  # unknown major never copied
    assert any("schema_version" in w for w in out.warnings)


# ── failure classes (all → run continues) ────────────────────────────────────


def test_binary_missing(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run(exc=FileNotFoundError()))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "error"
    assert any("not found" in w for w in out.warnings)


def test_timeout(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run(exc=subprocess.TimeoutExpired("toolgraph", 5)))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "timeout"
    assert any("timed out" in w for w in out.warnings)


def test_nonzero_exit(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run(raw=b"", returncode=2))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "error"
    assert any("exited 2" in w for w in out.warnings)


def test_bad_json(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run(raw=b"not json{{{"))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "error"


@pytest.mark.parametrize("mutate,label", [
    (lambda d: d.pop("graph_generation"), "missing graph_generation"),
    (lambda d: d.update(graph_generation="oops"), "non-int graph_generation"),
    (lambda d: d.update(graph_generation=True), "bool graph_generation"),
    (lambda d: d.update(rejected={"not": "a list"}), "non-list rejected"),
    (lambda d: d.update(decision="advisory_maybe"), "unknown decision"),
])
def test_malformed_v1_rejected_not_copied(monkeypatch, tmp_path, mutate, label):
    # A right-major but malformed v1 artifact would otherwise build an invalid
    # "ok" envelope block (e.g. graph_generation=None); it must be rejected as an
    # advisory error and never copied (codex R1).
    doc = json.loads(_golden("preflight-allow"))
    mutate(doc)
    _patch(monkeypatch, _fake_run(raw=json.dumps(doc).encode()))
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "error"
    assert out.artifact_path is None
    assert not (tmp_path / PREFLIGHT_FILE).exists()
    assert any("malformed" in w for w in out.warnings)


def test_ok_block_uses_requested_profile(monkeypatch, tmp_path):
    # The block's profile is the REQUESTED profile (a valid enum), not a
    # possibly-bad echoed field — keeps the envelope schema-valid.
    doc = json.loads(_golden("preflight-allow"))
    doc["profile"] = "not-a-profile"  # a bad echo must not reach the block
    _patch(monkeypatch, _fake_run(raw=json.dumps(doc).encode()))
    out = run_preflight(_cfg(profile="explore"), run_id="r", agent_ids=["codex"], out_dir=tmp_path)
    assert out.block["status"] == "ok"
    assert out.block["profile"] == "explore"


# ── out_dir=None (report disabled) ───────────────────────────────────────────


def test_no_out_dir_runs_but_discards_artifact(monkeypatch, tmp_path):
    fake = _fake_run("preflight-allow")
    _patch(monkeypatch, fake)
    out = run_preflight(_cfg(), run_id="r", agent_ids=["codex"], out_dir=None)
    assert out.block["status"] == "ok"
    assert out.artifact_path is None  # nothing pretends an artifact exists
    assert "argv" in fake.seen  # subprocess still ran (its identity value)
    # no stray files anywhere under tmp_path
    assert list(tmp_path.rglob(PREFLIGHT_FILE)) == []


# ── request contract ─────────────────────────────────────────────────────────


def test_build_request_validates_against_schema():
    cfg = _cfg(candidate_tools=["memtomem::mem_search", "syncmill::shared_read"])
    request = _build_request(cfg, run_id="rid-1", agent="codex")
    schema = json.loads(REQUEST_SCHEMA.read_text())
    jsonschema.Draft202012Validator(schema).validate(request)
    assert request["agent"] == "codex"
    assert request["policy_profile"] == "review"


# ── P3.1 strict enforcement ─────────────────────────────────────────────────


def _strict_cfg(**kw) -> ToolgraphConfig:
    base = dict(
        enabled=True,
        mode="strict",
        profile="review",
        candidate_tools=["alpha::read"],
        agent_map={"codex": "codex", "claude": "claude"},
    )
    base.update(kw)
    return ToolgraphConfig(**base)


def _strict_doc(agent, run_id, *, rejected=None, generation=42):
    rejected = rejected or []
    rejected_candidates = {row["candidate"] for row in rejected}
    return {
        "schema_version": 1,
        "kind": "toolgraph.preflight",
        "run_id": run_id,
        "created_at": "2026-07-12T00:00:00+00:00",
        "graph_generation": generation,
        "agent": agent,
        "agent_found": True,
        "profile": "review",
        "mode": "advisory",
        "eligible": [] if "alpha::read" in rejected_candidates else ["alpha::read"],
        "rejected": rejected,
        "features": [],
        "decision": "advisory_warn" if rejected else "advisory_allow",
    }


def _strict_fake(builder):
    def fake(argv, *args, **kwargs):
        agent = argv[2]
        run_id = argv[argv.index("--run-id") + 1]
        out_path = Path(argv[argv.index("--out") + 1])
        out_path.write_text(json.dumps(builder(agent, run_id)))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return fake


def _validate_envelope_block(block):
    envelope = json.loads(ENVELOPE_FIXTURE.read_text())
    envelope["preflight"] = block
    jsonschema.Draft202012Validator(json.loads(ENVELOPE_SCHEMA.read_text())).validate(envelope)


def test_strict_evaluates_all_identities_and_writes_manifest(monkeypatch, tmp_path):
    _patch(monkeypatch, _strict_fake(lambda agent, run_id: _strict_doc(agent, run_id)))
    out = run_preflight(
        _strict_cfg(), run_id="strict-1", agent_ids=["claude", "codex"], out_dir=tmp_path
    )
    assert out.should_block is False
    assert out.block["enforcement_decision"] == "allow"
    assert out.block["evaluation_count"] == 2
    manifest = json.loads((tmp_path / PREFLIGHT_FILE).read_text())
    jsonschema.Draft202012Validator(json.loads(STRICT_SCHEMA.read_text())).validate(manifest)
    assert manifest["kind"] == "syncmill.toolgraph-enforcement"
    assert [row["toolgraph_agent"] for row in manifest["evaluations"]] == ["claude", "codex"]
    assert (tmp_path / "preflight" / "eval-000.json").exists()


def _failing_atomic_write(monkeypatch, *, fail_when):
    real = preflight_module._atomic_write

    def failing(raw, dest):
        if fail_when(dest):
            raise OSError("disk full")
        return real(raw, dest)

    monkeypatch.setattr(preflight_module, "_atomic_write", failing)


def test_strict_eval_persist_failure_is_recorded_in_the_manifest(
    monkeypatch, tmp_path
):
    # Persist ordering: evals are written BEFORE the manifest is built, so a
    # failed eval write becomes a blocking reason the ON-DISK manifest carries.
    # The old order froze the manifest bytes first — the disk could read
    # `allow` for a run that was correctly blocked.
    _patch(monkeypatch, _strict_fake(lambda agent, run_id: _strict_doc(agent, run_id)))
    _failing_atomic_write(monkeypatch, fail_when=lambda dest: "eval-" in dest.name)
    out = run_preflight(
        _strict_cfg(), run_id="strict-p1", agent_ids=["claude"], out_dir=tmp_path
    )
    assert out.should_block is True
    assert "EVIDENCE_PERSIST_FAILED" in out.block_reasons
    assert out.block["status"] == "error"
    manifest = json.loads((tmp_path / PREFLIGHT_FILE).read_text())
    assert manifest["decision"] == "block"
    assert "EVIDENCE_PERSIST_FAILED" in manifest["block_reasons"]
    _validate_envelope_block(out.block)


def test_strict_mid_loop_persist_failure_strips_dangling_references(
    monkeypatch, tmp_path
):
    # eval-000 lands, eval-001 fails: the manifest must reference only files
    # that actually exist — the failed/remaining rows drop artifact/digest
    # (the same no-artifact shape the error/timeout rows use) instead of
    # pointing consumers at nonexistent evidence they can never verify.
    _patch(monkeypatch, _strict_fake(lambda agent, run_id: _strict_doc(agent, run_id)))
    _failing_atomic_write(
        monkeypatch, fail_when=lambda dest: dest.name == "eval-001.json"
    )
    out = run_preflight(
        _strict_cfg(), run_id="strict-p3", agent_ids=["claude", "codex"],
        out_dir=tmp_path,
    )
    assert out.should_block is True
    assert "EVIDENCE_PERSIST_FAILED" in out.block_reasons
    manifest = json.loads((tmp_path / PREFLIGHT_FILE).read_text())
    jsonschema.Draft202012Validator(
        json.loads(STRICT_SCHEMA.read_text())
    ).validate(manifest)
    rows = manifest["evaluations"]
    assert rows[0]["artifact"] == "preflight/eval-000.json"
    assert (tmp_path / rows[0]["artifact"]).exists()
    assert "artifact" not in rows[1] and "digest" not in rows[1]
    _validate_envelope_block(out.block)


def test_strict_manifest_persist_failure_blocks_and_leaves_no_evidence(
    monkeypatch, tmp_path
):
    # A failed manifest write must not leave raw allow-reading eval artifacts
    # behind with no aggregate — a blocked run's dir would misrepresent it.
    _patch(monkeypatch, _strict_fake(lambda agent, run_id: _strict_doc(agent, run_id)))
    _failing_atomic_write(
        monkeypatch, fail_when=lambda dest: dest.name == PREFLIGHT_FILE
    )
    out = run_preflight(
        _strict_cfg(), run_id="strict-p2", agent_ids=["claude"], out_dir=tmp_path
    )
    assert out.should_block is True
    assert "EVIDENCE_PERSIST_FAILED" in out.block_reasons
    assert out.artifact_path is None
    assert out.block["enforcement_decision"] == "block"
    assert not (tmp_path / PREFLIGHT_FILE).exists()
    assert not (tmp_path / "preflight" / "eval-000.json").exists()
    _validate_envelope_block(out.block)


def test_strict_unknown_blocks_and_drift_warns_by_default(monkeypatch, tmp_path):
    def builder(agent, run_id):
        reason = "TOOL_NOT_FOUND" if agent == "claude" else "DRIFTED"
        return _strict_doc(
            agent, run_id,
            rejected=[{"candidate": "alpha::read", "tool_key": None if reason == "TOOL_NOT_FOUND" else "alpha::read", "reason": reason}],
        )
    _patch(monkeypatch, _strict_fake(builder))
    out = run_preflight(
        _strict_cfg(), run_id="strict-2", agent_ids=["claude", "codex"], out_dir=tmp_path
    )
    assert out.should_block is True
    assert "TOOL_NOT_FOUND" in out.block_reasons
    assert "DRIFTED" not in out.block_reasons
    assert out.block["warning_count"] == 1


def test_strict_policy_overrides(monkeypatch, tmp_path):
    def drift(agent, run_id):
        return _strict_doc(
            agent, run_id,
            rejected=[{"candidate": "alpha::read", "tool_key": "alpha::read", "reason": "DRIFTED"}],
        )
    _patch(monkeypatch, _strict_fake(drift))
    blocked = run_preflight(
        _strict_cfg(drift_policy="block"), run_id="strict-3", agent_ids=["codex"], out_dir=tmp_path
    )
    assert blocked.should_block and blocked.block_reasons == ("DRIFTED",)

    missing = run_preflight(
        _strict_cfg(unknown_policy="warn"), run_id="strict-4", agent_ids=["kimi-code"], out_dir=tmp_path
    )
    assert missing.should_block is False
    assert missing.block["enforcement_decision"] == "allow"


def test_strict_unavailable_default_block_and_warn_override(monkeypatch, tmp_path):
    _patch(monkeypatch, _fake_run(exc=subprocess.TimeoutExpired("toolgraph", 5)))
    blocked = run_preflight(
        _strict_cfg(), run_id="strict-5", agent_ids=["codex"], out_dir=tmp_path
    )
    assert blocked.should_block and "TOOLGRAPH_UNAVAILABLE" in blocked.block_reasons
    allowed = run_preflight(
        _strict_cfg(unavailable_policy="warn"), run_id="strict-6", agent_ids=["codex"], out_dir=tmp_path
    )
    assert allowed.should_block is False


def test_strict_mismatched_evidence_always_blocks(monkeypatch, tmp_path):
    _patch(monkeypatch, _strict_fake(lambda agent, run_id: _strict_doc(agent, "wrong-run")))
    out = run_preflight(
        _strict_cfg(unavailable_policy="warn", unknown_policy="warn"),
        run_id="strict-7", agent_ids=["codex"], out_dir=tmp_path,
    )
    assert out.should_block
    assert out.block_reasons == ("CONTRACT_INVALID",)
    assert not (tmp_path / "preflight" / "eval-000.json").exists()


def test_strict_generation_mismatch_blocks(monkeypatch, tmp_path):
    _patch(monkeypatch, _strict_fake(
        lambda agent, run_id: _strict_doc(agent, run_id, generation=1 if agent == "claude" else 2)
    ))
    out = run_preflight(
        _strict_cfg(), run_id="strict-8", agent_ids=["claude", "codex"], out_dir=tmp_path
    )
    assert out.should_block
    assert "GRAPH_GENERATION_MISMATCH" in out.block_reasons
    assert out.block["status"] == "error"
    _validate_envelope_block(out.block)


def test_strict_missing_rejected_is_contract_invalid(monkeypatch, tmp_path):
    def missing_rejected(agent, run_id):
        doc = _strict_doc(agent, run_id)
        doc.pop("rejected")
        return doc

    _patch(monkeypatch, _strict_fake(missing_rejected))
    out = run_preflight(
        _strict_cfg(), run_id="strict-9", agent_ids=["codex"], out_dir=tmp_path
    )
    assert out.should_block
    assert out.block_reasons == ("CONTRACT_INVALID",)
    assert any("rejected is not a list" in warning for warning in out.warnings)


def test_strict_without_artifact_directory_fails_closed(monkeypatch):
    _patch(monkeypatch, _strict_fake(lambda agent, run_id: _strict_doc(agent, run_id)))
    out = run_preflight(
        _strict_cfg(), run_id="strict-10", agent_ids=["codex"], out_dir=None
    )
    assert out.should_block
    assert "EVIDENCE_PERSIST_UNAVAILABLE" in out.block_reasons
    assert out.block["status"] == "error"
    _validate_envelope_block(out.block)


def test_strict_block_count_matches_distinct_reasons(monkeypatch, tmp_path):
    def unknown(agent, run_id):
        return _strict_doc(
            agent,
            run_id,
            rejected=[{"candidate": "alpha::read", "tool_key": None, "reason": "TOOL_NOT_FOUND"}],
        )
    _patch(monkeypatch, _strict_fake(unknown))
    out = run_preflight(
        _strict_cfg(), run_id="strict-11", agent_ids=["claude", "codex"], out_dir=tmp_path
    )
    assert out.block_reasons == ("TOOL_NOT_FOUND",)
    assert out.block["block_count"] == 1
