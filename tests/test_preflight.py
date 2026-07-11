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

from syncmill.config import ToolgraphConfig
from syncmill.preflight import (
    PREFLIGHT_FILE,
    PreflightOutcome,
    _build_request,
    run_preflight,
)

VENDORED = Path(__file__).resolve().parent / "fixtures" / "toolgraph"
REQUEST_SCHEMA = Path(__file__).resolve().parent.parent / "contracts" / "preflight-request.schema.json"


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
