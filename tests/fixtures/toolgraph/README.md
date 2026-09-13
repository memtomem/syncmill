# Vendored toolgraph preflight contract fixtures

Copies of the preflight and body-free control-plan artifact goldens (and their
schemas) that toolgraph produces, so syncmill's hermetic suite can validate its
consumer side of the contract **without** toolgraph or Neo4j installed.

- Source repository: `toolgraph` (sibling repo)
- Source paths: `contracts/preflight.schema.json`, `contracts/fixtures/preflight-*.json`
- Control paths: `contracts/control-plan.schema.json`,
  `contracts/control-preflight.schema.json`, and their v1 fixtures
- Source commit: `3a6a2dc61dadef4c857f0e3bde888ab9703af127`
  ("P0: add preflight artifact contract schema and fixtures (G1)")

Regeneration is manual: re-copy from the source paths above and update the
commit hash here. `tests/test_contracts.py` treats these as the consumer
contract — `preflight-v2.json` must be **rejected** (unknown major
schema_version) and `preflight-additive.json` must be **tolerated** (unknown
additive fields ignored). The control contract follows the same major/additive
rule and is also scanned for bodies, credentials, and absolute paths.
