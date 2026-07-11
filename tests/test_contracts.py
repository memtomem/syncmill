"""P0 contract tests: run-envelope schema, preflight request, vendored toolgraph goldens.

Hermetic — pure file-based, no agents, no memtomem, no subprocess, no Neo4j.
Two contracts are pinned here:

* producer side — every envelope fixture under ``contracts/fixtures/`` validates
  against ``contracts/run-envelope.schema.json`` (the deliberate ``envelope-v2``
  major-version fixture must NOT validate: consumers reject unknown majors);
* consumer side — the vendored toolgraph preflight goldens under
  ``tests/fixtures/toolgraph/`` validate against the vendored schema copy, with
  the same v2-rejected / additive-tolerated pair.

The forbidden-content scan is the redaction contract: no prompt/output/patch
bodies, no credential-shaped strings, and no absolute paths in any artifact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest

REPO = Path(__file__).resolve().parent.parent
CONTRACTS = REPO / "contracts"
ENVELOPE_FIXTURES = CONTRACTS / "fixtures"
VENDORED = Path(__file__).resolve().parent / "fixtures" / "toolgraph"

VALID_ENVELOPES = ["envelope-direct.json", "envelope-traced.json", "envelope-board.json", "envelope-additive.json"]
VALID_PREFLIGHTS = [
    "preflight-allow.json",
    "preflight-warn.json",
    "preflight-unresolved-identity.json",
    "preflight-additive.json",
]

# Keys whose PRESENCE anywhere in an artifact violates the data-classification
# contract (bodies and secrets live outside the envelope, linked by digest).
FORBIDDEN_KEYS = {
    "prompt",
    "output",
    "stdout",
    "stderr",
    "patch",
    "env",
    "credential",
    "password",
    "token",
    "api_key",
}

_CREDENTIAL_PATTERNS = [
    re.compile(r"://[^/\s]+:[^/\s]+@"),  # URI userinfo user:pass@
    re.compile(r"password\s*=", re.IGNORECASE),
    re.compile(r"token\s*=", re.IGNORECASE),
    re.compile(r"api[_-]?key", re.IGNORECASE),
]
_ABS_PATH = re.compile(r"^(/|[A-Za-z]:[\\/])")


def _validator(schema_path: Path) -> jsonschema.Draft202012Validator:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _walk(node, keys: list, values: list) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            _walk(value, keys, values)
    elif isinstance(node, list):
        for item in node:
            _walk(item, keys, values)
    else:
        values.append(node)


def _scan(doc) -> list[str]:
    """Structured redaction scan: forbidden keys (case-insensitive), absolute-path
    values (POSIX and Windows), and credential-shaped strings."""
    keys: list = []
    values: list = []
    _walk(doc, keys, values)
    violations = [f"key:{k}" for k in keys if k.lower() in FORBIDDEN_KEYS]
    for value in values:
        if isinstance(value, str):
            if _ABS_PATH.match(value):
                violations.append(f"abs-path:{value}")
            for pattern in _CREDENTIAL_PATTERNS:
                if pattern.search(value):
                    violations.append(f"cred:{value}")
    return violations


# --- producer side: run envelope -------------------------------------------------


@pytest.mark.parametrize("name", VALID_ENVELOPES)
def test_envelope_fixture_validates(name: str):
    _validator(CONTRACTS / "run-envelope.schema.json").validate(_load(ENVELOPE_FIXTURES / name))


def test_envelope_v2_is_rejected():
    doc = _load(ENVELOPE_FIXTURES / "envelope-v2.json")
    assert doc["schema_version"] == 2
    errors = list(_validator(CONTRACTS / "run-envelope.schema.json").iter_errors(doc))
    assert any(list(e.absolute_path) == ["schema_version"] for e in errors)


def test_envelope_additive_carries_unknown_fields():
    doc = _load(ENVELOPE_FIXTURES / "envelope-additive.json")
    schema = json.loads((CONTRACTS / "run-envelope.schema.json").read_text(encoding="utf-8"))
    assert set(doc) - set(schema["properties"]), "additive fixture has no unknown top-level field"


def test_envelope_disabled_blocks_always_present():
    """Consumers never branch on absence: every sub-block exists even when off."""
    for name in VALID_ENVELOPES:
        doc = _load(ENVELOPE_FIXTURES / name)
        for block in ("preflight", "trace", "result"):
            assert "status" in doc[block], f"{name}: {block} block missing status"


def test_preflight_request_fixture_validates():
    _validator(CONTRACTS / "preflight-request.schema.json").validate(
        _load(ENVELOPE_FIXTURES / "preflight-request.json")
    )


# --- consumer side: vendored toolgraph preflight goldens -------------------------


@pytest.mark.parametrize("name", VALID_PREFLIGHTS)
def test_vendored_preflight_validates(name: str):
    _validator(VENDORED / "preflight.schema.json").validate(_load(VENDORED / name))


def test_vendored_preflight_v2_is_rejected():
    doc = _load(VENDORED / "preflight-v2.json")
    assert doc["schema_version"] == 2
    errors = list(_validator(VENDORED / "preflight.schema.json").iter_errors(doc))
    assert any(list(e.absolute_path) == ["schema_version"] for e in errors)


# --- redaction contract over every fixture ---------------------------------------


@pytest.mark.parametrize(
    "path",
    sorted(ENVELOPE_FIXTURES.glob("*.json")) + sorted(VENDORED.glob("*.json")),
    ids=lambda p: p.name,
)
def test_no_forbidden_content(path: Path):
    assert _scan(_load(path)) == [], f"{path.name} violates the redaction contract"


def test_scanner_catches_planted_violations():
    """Adversarial guard: the scanner must FLAG a planted body / secret / abs path
    (incl. uppercase keys and Windows paths) — else the clean-fixture scan is vacuous."""
    poisoned = {
        "schema_version": 1,
        "STDOUT": "leaked agent output",
        "result": {"artifact": "C:\\Users\\me\\result.json", "note": "postgres://u:p@h/db"},
    }
    found = _scan(poisoned)
    assert any(v.startswith("key:") for v in found)
    assert any(v.startswith("abs-path:") for v in found)
    assert any(v.startswith("cred:") for v in found)
