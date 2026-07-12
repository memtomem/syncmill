"""Tracegraph review-candidate contract and deterministic board projection."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


SCHEMA_VERSION = 1
KIND = "tracegraph.review-candidates"
DEFAULT_GOAL = "tracegraph-review"
_ITEM_NAMESPACE = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://syncmill.dev/review-items/tracegraph"
)


class ReviewCandidateError(ValueError):
    """The input is not a supported Tracegraph review-candidate report."""


class ReviewCandidate(BaseModel):
    """The body-free fields SyncMill consumes from one Tracegraph finding."""

    model_config = ConfigDict(extra="ignore", strict=True)

    run_id: str = Field(min_length=1, max_length=256, pattern=r"^\S+$")
    pattern_id: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    pattern_version: int = Field(ge=1)
    tool_key: str = Field(min_length=4, max_length=512, pattern=r"^\S+::\S+$")
    artifact_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReviewCandidateReport(BaseModel):
    """Supported v1 envelope; additive producer fields are deliberately discarded."""

    model_config = ConfigDict(extra="ignore", strict=True)

    schema_version: Literal[1] = SCHEMA_VERSION
    kind: Literal["tracegraph.review-candidates"] = KIND
    candidates: list[ReviewCandidate] = Field(default_factory=list)


@dataclass(frozen=True)
class ReviewItemSpec:
    """Immutable board projection for one exact Tracegraph candidate tuple."""

    item_id: str
    task: str
    inputs: tuple[str, ...]
    artifact_digest: str


def load_report(path: str | Path) -> ReviewCandidateReport:
    """Read and validate a complete report without retaining unknown fields."""
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError as exc:
        raise ReviewCandidateError(f"cannot read review-candidate report: {exc}") from exc
    try:
        return ReviewCandidateReport.model_validate_json(raw)
    except ValidationError as exc:
        first = exc.errors(include_input=False)[0]
        location = ".".join(str(part) for part in first.get("loc", ())) or "report"
        raise ReviewCandidateError(
            f"invalid review-candidate report at {location}: {first['msg']}"
        ) from exc


def item_spec(candidate: ReviewCandidate) -> ReviewItemSpec:
    """Project a candidate to a stable, body-free board item specification."""
    identity = json.dumps(
        [
            SCHEMA_VERSION,
            KIND,
            candidate.run_id,
            candidate.pattern_id,
            candidate.pattern_version,
            candidate.tool_key,
            candidate.artifact_digest,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    item_id = str(uuid.uuid5(_ITEM_NAMESPACE, identity))
    pattern = f"{candidate.pattern_id}@v{candidate.pattern_version}"
    return ReviewItemSpec(
        item_id=item_id,
        task=f"Review Tracegraph {pattern} finding for {candidate.tool_key}",
        inputs=(
            f"source:{KIND}@v{SCHEMA_VERSION}",
            f"run_id:{candidate.run_id}",
            f"pattern:{pattern}",
            f"tool_key:{candidate.tool_key}",
            f"artifact_digest:{candidate.artifact_digest}",
        ),
        artifact_digest=candidate.artifact_digest,
    )


def item_specs(report: ReviewCandidateReport) -> list[ReviewItemSpec]:
    return [item_spec(candidate) for candidate in report.candidates]
