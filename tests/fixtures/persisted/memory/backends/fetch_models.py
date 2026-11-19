"""Fetch the FastEmbed models the frozen Mem0 and LangGraph stores were embedded
with, at the exact revisions ``models.json`` records, into a FastEmbed cache.

    python tests/fixtures/persisted/memory/backends/fetch_models.py <cache-dir>

Run with an interpreter that has ``huggingface_hub`` (a FastEmbed dependency),
e.g. ``uv run --extra mem0 --extra langgraph python ...``. Every release's
``models.json`` must name the same revision of each model. Only the files a
``models.json`` lists are fetched, and each is checked against its sha256. The
cache's ``refs/main`` is pointed at that revision, because FastEmbed resolves
``main`` when it loads a model offline (``HF_HUB_OFFLINE=1``), which is how the
tests read the stores.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download

HERE = Path(__file__).resolve().parent


def wanted() -> dict[str, dict]:
    models: dict[str, dict] = {}
    for manifest in sorted(HERE.glob("v*/models.json")):
        for model, spec in json.loads(manifest.read_text(encoding="utf-8")).items():
            if models.setdefault(model, spec) != spec:
                sys.exit(f"{manifest}: {model} differs from another release's models.json")
    if not models:
        sys.exit("no models.json found")
    return models


def main() -> None:
    cache = Path(sys.argv[1]).resolve()
    for model, spec in wanted().items():
        if not model.startswith("models--"):
            sys.exit(f"unexpected cache directory name {model}")
        repo_id = model[len("models--"):].replace("--", "/", 1)
        for entry in spec["files"]:
            name, digest = entry["name"], entry["sha256"]
            path = Path(hf_hub_download(
                repo_id, name, revision=spec["revision"], cache_dir=str(cache)))
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != digest:
                sys.exit(f"{repo_id}@{spec['revision']} {name}: sha256 {actual} != {digest}")
        refs = cache / model / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "main").write_text(spec["revision"], encoding="utf-8")
        print(f"{repo_id}@{spec['revision']}: {len(spec['files'])} files")


if __name__ == "__main__":
    main()
