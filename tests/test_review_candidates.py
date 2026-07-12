"""Tracegraph v1 contract validation and deterministic board projection."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from syncmill.review_candidates import (
    ReviewCandidate,
    ReviewCandidateError,
    item_spec,
    load_report,
)


FIXTURES = Path(__file__).parent / "fixtures" / "tracegraph"
SCHEMA = FIXTURES / "review-candidates.schema.json"
GOLDEN = FIXTURES / "v1.json"


def test_vendored_golden_validates_and_loads():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))

    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(golden)
    report = load_report(GOLDEN)

    assert report.schema_version == 1
    assert len(report.candidates) == 1
    assert report.candidates[0].tool_key == "syncmill::board_stats"


def test_additive_fields_are_accepted_but_discarded(tmp_path):
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    doc["private_future"] = {"secret": "must not survive"}
    doc["candidates"][0]["producer_hint"] = "ignored"
    source = tmp_path / "additive.json"
    source.write_text(json.dumps(doc), encoding="utf-8")

    report = load_report(source)

    assert report.model_dump() == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_empty_report_is_valid(tmp_path):
    source = tmp_path / "empty.json"
    source.write_text(
        json.dumps(
            {"schema_version": 1, "kind": "tracegraph.review-candidates", "candidates": []}
        ),
        encoding="utf-8",
    )
    assert load_report(source).candidates == []


@pytest.mark.parametrize(
    ("mutate", "location"),
    [
        (lambda doc: doc.update(schema_version=2), "schema_version"),
        (lambda doc: doc.pop("schema_version"), "schema_version"),
        (lambda doc: doc.update(kind="other"), "kind"),
        (lambda doc: doc.pop("kind"), "kind"),
        (lambda doc: doc["candidates"][0].update(tool_key="board_stats"), "tool_key"),
        (lambda doc: doc["candidates"][0].pop("run_id"), "run_id"),
    ],
)
def test_invalid_contract_is_rejected_without_echoing_values(tmp_path, mutate, location):
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    mutate(doc)
    source = tmp_path / "bad.json"
    source.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ReviewCandidateError, match=location) as exc_info:
        load_report(source)

    assert "syncmill::board_stats" not in str(exc_info.value)


def test_malformed_and_missing_files_are_clean_errors(tmp_path):
    malformed = tmp_path / "bad.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(ReviewCandidateError, match="invalid review-candidate"):
        load_report(malformed)
    with pytest.raises(ReviewCandidateError, match="cannot read"):
        load_report(tmp_path / "missing.json")


def test_item_identity_is_stable_and_every_contract_field_matters():
    base = load_report(GOLDEN).candidates[0]
    expected = item_spec(base)
    assert item_spec(ReviewCandidate.model_validate(base.model_dump())) == expected

    variants = [
        base.model_copy(update={"run_id": "other-run"}),
        base.model_copy(update={"pattern_id": "tool-failure"}),
        base.model_copy(update={"pattern_version": 2}),
        base.model_copy(update={"tool_key": "syncmill::shared_read"}),
        base.model_copy(update={"artifact_digest": "sha256:" + "0" * 64}),
    ]
    assert len({item_spec(candidate).item_id for candidate in [base, *variants]}) == 6
