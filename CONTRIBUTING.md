# Contributing to SyncMill

Thank you for your interest in SyncMill. This document covers the development
setup, what a reviewable pull request looks like, and the one-time Contributor
License Agreement.

SyncMill orchestrates several CLI coding agents over a single task and gives
them shared long-term memory. [CLAUDE.md](CLAUDE.md) is the architectural
reference — read the design invariants there before changing orchestration,
memory, or the strategy layer.

## Development setup

SyncMill uses [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
uv sync --group dev        # install dependencies and create .venv
uv run syncmill doctor     # validate the repository, agents, and stores
uv run pytest -q           # the hermetic test suite
uv run ruff check .        # lint, including tests
```

The test suite is fully hermetic. It never touches the real `~/.memtomem`
database, the network, or the agent binaries, because `tests/conftest.py`
supplies fake agents, memory backends, and registries. Keep it that way: new
behavior gets a hermetic test, and anything that can only be proven against a
real store goes behind an opt-in environment variable.

Some changes require an opt-in real smoke before they can be reviewed:

```bash
SYNCMILL_TEST_MEMTOMEM_REAL=1 uv run pytest -q \
  tests/test_memory_backend_memtomem_integration.py   # the memtomem add flow or pin
SYNCMILL_TEST_MEMORY_MIGRATION_REAL=1 uv run pytest -q \
  tests/test_memory_migration_integration.py          # migration adapters
SYNCMILL_TEST_LANGGRAPH_REAL=1 uv run pytest -q \
  tests/test_memory_backend_langgraph_integration.py  # LangGraph SDK pins
```

Running the agents for real needs the `claude`, `codex`, and `kimi` CLIs
installed and authenticated. Note that programmatic runs may be billed
separately from interactive use.

## Pull request guidelines

1. Keep one concern per pull request, and describe what changed and why.
2. Add or update tests. A test should fail if you remove the code it guards —
   check that it does.
3. Run `uv run ruff check .` and `uv run pytest -q` before pushing, and run any
   real smoke your change requires.
4. Add a `CHANGELOG.md` entry under `## Unreleased` for anything user-visible,
   including documentation corrections. Later review rounds are easy to forget
   here; the entry is part of the change, not a release-time chore.
5. Update `CLAUDE.md` when you change an architectural contract, and the user
   guides when you change a command. A documentation test requires every
   non-hidden command to appear in the README or a user guide.
6. Sign the CLA on your first pull request — see below.

## Releases

Releases are tag-driven and published to PyPI through trusted publishing; there
are no stored API tokens. `scripts/release_preflight.py` enforces the contract
before anything is uploaded: the tag, `pyproject.toml`, `uv.lock`, and a dated
`CHANGELOG.md` heading must all name one version, the tag commit must be on
`main`, that commit's own main-push CI run must have succeeded, and both
artifacts must install into a clean environment and import.

- `v0.2.0` publishes to PyPI.
- `test-v0.2.0a1` publishes the same distribution version to TestPyPI as a
  rehearsal.

**A version is spent once it is uploaded.** Both tag forms map to the same
distribution version, and the publish step tolerates pre-existing filenames, so
a corrected build re-tagged as `a2` cannot replace what an index already
serves. The workflow verifies the published bytes against what it built and
fails when they differ. If a release needs changing after upload, raise the
version rather than re-tagging.

## Contributor License Agreement (CLA)

Before we can merge your first pull request, you need to sign the
[Contributor License Agreement](CLA.md). The CLA workflow comments on your pull
request with instructions — you sign by replying with:

> I have read the CLA Document and I hereby sign the CLA

You only need to sign once per GitHub account. Your signature is stored in
`signatures/v1/cla.json` on the `cla-signatures` branch, and a `cla` status on
your pull request turns green once every contributor to it has signed. SyncMill
and [memtomem](https://github.com/memtomem/memtomem) keep independent signature
stores, so contributors to both sign in each repository.

The CLA is adapted from the Apache Software Foundation Individual Contributor
License Agreement with one additional section covering future licensing rights.
This preserves DAPADA Inc.'s ability to adopt different license terms for the
Work in the future (for example, a dual-licensing arrangement) without needing
to re-collect consent from every contributor. The CLA does not change the
current license of the Work, which remains Apache License 2.0.

For questions about the CLA, contact contact@dapada.co.kr.

## Reporting issues

Open an issue with what you ran, what you expected, and what happened. For an
orchestration problem, include the strategy, the agents involved, and the run
id — `syncmill report show <run_id>` prints the run envelope when report
recording is enabled.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
