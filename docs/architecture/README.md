# Architecture records

Full design records for syncmill's modules. [`CLAUDE.md`](../../CLAUDE.md) keeps the
design invariants and a short rule list per module, and says when to read the record
below before changing that module.

Each record was moved verbatim from `CLAUDE.md` on 2026-10-21. Where a record
disagrees with the code, a "Known stale statements" note at its top says so; the code
and `CLAUDE.md` win.

| Record | Covers |
|---|---|
| [extension-points.md](extension-points.md) | Memory backends, agents, strategies, quality gates, circuit breaker, capability matching, model routing |
| [memory.md](memory.md) | `memory.py` — namespaces, shares, memtomem parity, purge |
| [interactive-collaboration.md](interactive-collaboration.md) | A human-driven agent collaborating through `shared` |
| [gateway.md](gateway.md) | `gateway.py` — the read-only MCP gateway |
| [monitor.md](monitor.md) | `monitor.py` — the live tmux monitor |
| [session.md](session.md) | `session.py` — durable interactive sessions |
| [board.md](board.md) | `board.py` — the persistent work-board |
| [dashboard.md](dashboard.md) | `dashboard.py` — the board dashboard |
| [tui.md](tui.md) | `tui.py` + `tui_jobs.py` — the TUI command center |
| [teams.md](teams.md) | Named team profiles |
| [teamfile.md](teamfile.md) | `teamfile.py` — writing `syncmill.toml` |
| [report.md](report.md) | `report.py` — run identity and the run envelope |
| [tracer.md](tracer.md) | `tracer.py` — OTLP trace export |
| [preflight.md](preflight.md) | `preflight.py` — toolgraph preflight (advisory and strict) |
| [artifact-boundary.md](artifact-boundary.md) | The artifact filesystem boundary |
| [config.md](config.md) | `config.py` — sources, discovery, validation |
| [worktrees.md](worktrees.md) | `worktree.py` — worktrees, sweeps, patch capture |
