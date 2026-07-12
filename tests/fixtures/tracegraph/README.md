# Vendored Tracegraph review-candidate contract

Byte-identical copies of Tracegraph's public schema and producer-derived v1
golden. SyncMill validates and imports this contract without installing
Tracegraph.

- Source repository: `tracegraph` (sibling repo)
- Source schema: `contracts/review-candidates.schema.json`
- Source golden: `tests/fixtures/review-candidates/v1.json`
- Source commit: `62feda2` ([Tracegraph PR #10](https://github.com/memtomem/tracegraph/pull/10)),
  which makes the digest refer to the exact normalized source artifact.

Regeneration is manual: re-copy both files after Tracegraph's producer golden
passes byte-for-byte, then update this provenance note.
