# Artifact filesystem boundary (#166)

> Design record for `artifacts.py`, moved verbatim from `CLAUDE.md`. `CLAUDE.md` keeps a
> short rule list for this topic; this file holds the full reasoning, history and test map.

`ArtifactStore` uses descriptor-relative `ArtifactIO` operations beneath the
canonical configured state root. Project/run IDs must be single components;
UUID-only names are not required. Directories below that root must not be
symlinks, and artifact files must be regular, singly linked files. Reads, atomic
writes, receipts, locks, and pruning share this boundary. Open directory handles
are reused within an operation and their identities rechecked before use;
public reads release their own run handles on return, including inside a held
apply lock. Mutation helpers retain their handles through the complete operation.

An explicit manifest `run_id` or `project_id` must match its location. Missing
legacy identity fields remain readable without migration or read-time writes.
Unsafe access raises `ArtifactError`; listings continue to omit invalid records, and `list_detailed` reports each omitted directory or symlink entry as a `SkippedRun` (the raw name plus a fixed reason category — never a path or manifest content; render names through `printable_name`, since run ids may carry control characters) so `runs list`/`conversations list` (a stderr notice) and the TUI Artifacts pane (`DEGRADED: N unreadable`) never present a corrupt-only store as a healthy empty one (#170). A skipped entry is never repaired, reset or pruned, and a runs root replaced mid-enumeration (`ESTALE` on the root) still raises instead of being misfiled as per-run damage.
Use `read_result` for result sidecars instead of reopening `RunArtifact.path`.
Apply/check/rollback pass the same digest-verified patch bytes to Git over stdin,
including non-UTF8 data, rather than reopening a mutable patch path. Existing
CAS, lock ordering, rollback and manual-recovery semantics remain in force.

The configured state root and its ancestors are trusted. This boundary prevents
path traversal and symlink redirection; it is not a sandbox against an actor
with the same OS credentials who can mutate files or relocate open directories.
If the runs directory is removed/replaced, restart the TUI or reopen the store
after restoring the intended directory. Identity mismatches never silently adopt
a new directory. Supported platforms remain macOS and Linux.
