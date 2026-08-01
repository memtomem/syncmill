> **Historical raw research — not the current product contract.** Tool versions,
> capabilities, and architectural claims below may be stale. Use the
> [README](README.md) and [user guide](docs/user-guide.md) for current usage and
> [CLAUDE.md](CLAUDE.md) for the maintainer contract.

# Orchestrating Claude Code, Gemini CLI, and Codex CLI into a unified multi-agent system

**All three major AI coding CLIs now support headless execution, MCP integration, and subprocess invocation — making a unified multi-agent system not just feasible but already implemented by dozens of open-source projects.** The convergence around MCP as a shared context protocol, git worktrees for workspace isolation, and supervisor-style orchestration patterns means the architectural primitives exist today. LangGraph can serve as the orchestration backbone, wrapping each CLI as a tool, while MCP memory servers provide the shared memory bus. This report covers every protocol, capability, and architectural pattern needed to build this system.

---

## Each CLI's inter-session and programmatic capabilities

### Claude Code: the most mature agent SDK

Claude Code offers the deepest programmatic integration of the three. Its **`-p` / `--print` flag** enables headless execution, processing a prompt, running tool calls, and exiting with output to stdout. The `--bare` flag skips auto-discovery for deterministic, fast subprocess invocation. Output formats include `text`, `json`, and `stream-json` (newline-delimited JSON for real-time streaming), and `--json-schema` forces structured output conforming to a JSON Schema.

The **Claude Agent SDK** (Python: `claude-agent-sdk`, TypeScript: `@anthropic-ai/claude-agent-sdk`) spawns the CLI as a subprocess and communicates via JSON-lines over stdin/stdout. The Python SDK provides both `query()` for one-shot calls and `ClaudeSDKClient` for multi-turn interactive sessions. Custom tools can be defined via the `@tool` decorator and injected as in-process MCP servers — no subprocess overhead.

For inter-session communication, Claude Code introduced **shared task lists** via the `CLAUDE_CODE_TASK_LIST_ID` environment variable. Multiple terminal sessions set to the same task list ID share a disk-backed task board (`~/.claude/tasks/<id>/`) with `TaskCreate`, `TaskUpdate`, `TaskGet`, and `TaskList` tools. Tasks support states (pending, in_progress, completed, blocked), dependencies via `addBlockedBy`, and ownership claims. This is production-ready as of v2.1.16 (January 2026).

Claude Code's **Agent Teams** feature (experimental, v2.1.32+) goes further: one session acts as "team lead," spawning 2–16 teammates, each a full independent Claude Code instance. Teammates communicate via `SendMessage` (peer-to-peer, not just parent-child), shared task lists, idle notifications, and broadcast. MCP integration is first-class, supporting `stdio`, `http`, and `sse` transports with OAuth, static headers, and dynamic authentication scripts.

### Gemini CLI: open-source with full MCP and A2A

Google's Gemini CLI (github.com/google-gemini/gemini-cli, **~100K GitHub stars**, Apache 2.0) supports headless mode via `-p`/`--prompt` or positional arguments. It outputs in `text`, `json`, or `stream-json` format, and the `--yolo` flag auto-approves all tool actions for fully unattended execution. Stdin piping works automatically in non-TTY environments.

**MCP is a first-class feature**, configured in `settings.json` (user or project scope) with support for stdio, SSE, and HTTP transports. Gemini CLI can discover MCP tools, execute them, access resources, and handle rich multi-part content. Management commands include `gemini mcp add`, `gemini mcp list`, and in-session `/mcp` for status inspection. Google also announced fully-managed MCP servers for Google Cloud services (BigQuery, GCE, GKE) with IAM controls.

Uniquely, Gemini CLI has added **A2A (Agent-to-Agent) protocol** support, enabling standardized communication with external AI agents via the `@a2a` tool and an A2A server package. The **extensions system** allows packaging custom tools, commands, hooks, sub-agents, and skills into installable bundles from GitHub repositories. The **hooks system** (7+ lifecycle events) provides middleware-like interception at every stage of the agent loop.

Gemini CLI does **not** have native inter-session messaging, but sessions can be saved/resumed via `/chat save <tag>` and `--resume`, and `GEMINI.md` files provide persistent cross-session context at global, project, and subdirectory levels.

### OpenAI Codex CLI: Rust-native with MCP server mode

Codex CLI (github.com/openai/codex, **60K+ stars**, rewritten in Rust) provides non-interactive mode via `codex exec "prompt"`. The `--json` flag outputs JSONL events, `--output-schema` constrains output to a JSON Schema, and `--full-auto` enables workspace-write sandbox with autonomous execution. A TypeScript SDK (`@openai/codex-sdk`) allows programmatic thread management.

Codex CLI's standout feature is its **MCP server mode**: `codex mcp-server` runs Codex as a long-lived MCP server over stdio, exposing two tools — `codex()` to start sessions and `codex-reply()` to continue them. This means **any MCP client can orchestrate Codex programmatically**, including Claude Code and Gemini CLI. The official OpenAI cookbook demonstrates multi-agent orchestration via the Agents SDK consuming Codex as an MCP server.

For inter-session coordination, Codex supports session resume (`codex resume --last` or by session ID), session forking (`codex fork`), and shared configuration via `~/.codex/config.toml` and `AGENTS.md` files. Its sandboxing system uses platform-native isolation: Apple Seatbelt on macOS, Bubblewrap + seccomp on Linux, and restricted tokens on Windows. There is also a cross-tool plugin (`openai/codex-plugin-cc`) that lets Claude Code invoke Codex as a delegate for review and rescue workflows.

---

## MCP as the shared memory bus

**MCP (Model Context Protocol) has emerged as the de facto shared context layer for multi-agent systems.** Originally created by Anthropic in November 2024, it is now governed by the Agentic AI Foundation under the Linux Foundation, backed by AWS, Anthropic, Google, Microsoft, and OpenAI. All three CLIs support it natively.

The architectural pattern is straightforward: **all agents connect to the same MCP server(s)**, which provide shared tool discovery, shared resources, and standardized JSON-RPC communication regardless of which CLI tool connects. Several MCP memory servers are purpose-built for this:

- **agent-memory-store** — SQLite-backed with hybrid search (FTS5 BM25 + vector similarity + Reciprocal Rank Fusion), providing `search_context`, `write_context`, `get_state`, and `set_state` tools. All data lives in a single SQLite database.
- **mcp-memory-service** — Persistent memory with REST API, knowledge graph, and autonomous consolidation. Documented as an inter-agent messaging bus using tag-based filtering (`msg:cluster`) for cross-agent signals.
- **Redis Agent Memory Server** — Redis-backed with vector search, multi-provider LLM support, and sub-millisecond latency for hot state.
- **Agent-MCP** (github.com/rinadelph/Agent-MCP) — Purpose-built multi-agent framework through MCP with shared memory that "scales infinitely," task decomposition, and real-time visualization.

**AgentMux** (github.com/stevehuang0115/agentmux) provides a concrete implementation: all agents (Claude Code, Gemini CLI, Codex) connect to a single MCP server endpoint that provides task management, status updates, and inter-agent messaging. The project includes MCP configuration for both Claude Code (`.mcp.json`) and Gemini CLI (`settings.json`) connecting to the same server.

Beyond MCP, practical shared memory approaches include **SQLite** (most common for local setups, used by CrewAI's long-term memory and many MCP servers), **Redis** (dominant in production for its Pub/Sub messaging, Streams for event sourcing, and native vector search — 43% developer adoption for AI agent data storage), and **git worktrees** as the filesystem-level isolation pattern where each agent works in its own worktree on its own branch.

---

## LangGraph orchestration patterns and their trade-offs

LangGraph provides a graph-based state machine framework that is **fully LLM-agnostic** — each agent in a multi-agent system can use a different provider (Anthropic, Google, OpenAI) simultaneously. The `langgraph-supervisor` package (PyPI: v0.0.31, MIT licensed) implements the primary orchestration pattern.

**The Supervisor pattern** uses a central LLM-powered agent whose "tools" are other agents. The supervisor receives input, routes to specialized agents via handoff tools, evaluates results, and either routes further or returns the final response. Each registered agent becomes a callable tool through `create_handoff_tool`. The supervisor supports `output_mode` of `"full_history"` (include all messages) or `"last_message"` (only final response), and hierarchical composition where supervisors manage other supervisors.

LangGraph supports five additional patterns beyond the basic supervisor:

- **Hierarchical** — Multi-level supervisors, each managing teams. A top-level supervisor routes to team-level supervisors that manage individual agents. Best for large-scale systems.
- **Swarm** (via `langgraph-swarm`) — Agents hand off control to each other dynamically with no central coordinator. Remembers the last active agent. Best for exploratory tasks with fluid boundaries.
- **Sequential pipeline** — Fixed-order processing where each agent builds on prior results. Simplest and most deterministic.
- **Parallel scatter-gather** — Tasks run simultaneously across agents, results merged. Good for independent sub-tasks.
- **Shared scratchpad** — All agents share a common message history, providing full transparency at the cost of verbosity.

**For orchestrating external CLIs**, LangGraph wraps each tool as a Python function using `subprocess.run()`:

```python
@tool
def claude_code(task: str, cwd: str = "/project") -> str:
    result = subprocess.run(
        ["claude", "-p", task, "--output-format", "text", "--bare"],
        capture_output=True, text=True, cwd=cwd, timeout=300
    )
    return result.stdout or result.stderr
```

State management uses typed `TypedDict` state objects with reducer functions for merging concurrent updates. **Short-term memory** persists via checkpointers (InMemorySaver, PostgresSaver, RedisSaver), while **long-term memory** uses a cross-thread `BaseStore`. Human-in-the-loop is first-class, with `interrupt` primitives that pause execution, save state via checkpointing, and resume after human approval, editing, or rejection.

Compared to alternatives: **CrewAI** offers easier setup with role-based teams but less control; **AutoGen** excels at conversational multi-agent debate but is being superseded by Microsoft Agent Framework; **OpenAI Agents SDK** is simplest but locked to OpenAI models. LangGraph provides the most flexibility and production-readiness (v1.0, LangSmith observability) at the cost of a steeper learning curve.

---

## The ecosystem already exists: 50+ orchestration projects

The multi-agent AI coding orchestrator ecosystem has **exploded in 2025–2026**, with over 50 actively maintained projects cataloged in the awesome-agent-orchestrators list. Several explicitly combine all three CLIs:

**Bernstein** (github.com/chernistry/bernstein) is a deterministic Python scheduler that spawns parallel Claude Code, Codex, and Gemini CLI agents, verifies output with tests, and auto-commits — using **zero LLM tokens on coordination** since scheduling is purely deterministic. It supports cross-model code review, circuit breakers for misbehaving agents, and git worktree isolation.

**AgentMux** (github.com/stevehuang0115/agentmux) provides a web dashboard with real-time monitoring, terminal streaming via WebSocket, ticket management, and MCP integration for inter-agent communication across Claude Code, Codex, and Gemini CLI.

**Agent-Council** (github.com/yogirk/agent-council) convenes a panel of CLI-based agents for structured multi-stage deliberation, peer review, and consensus synthesis through a three-stage process with quorum-based voting.

**OpenClaw-Claude-Code** (github.com/Enderfga/openclaw-claude-code) provides a unified `ISession` interface driving any CLI tool, with multi-model proxy (Anthropic ↔ OpenAI format translation), council consensus voting, git worktree isolation, and per-model cost tracking.

**Claude Code Bridge** enables real-time multi-AI collaboration via split-pane terminals with async messaging, cross-AI orchestration, daemon architecture, and persistent per-AI context. **Parallel Code** is a desktop app for running all three CLIs side by side in isolated git worktrees.

---

## Recommended architecture for the unified system

Based on the research, the optimal architecture combines a **LangGraph supervisor** as the orchestration backbone, **MCP memory servers** as the shared context layer, and **git worktrees** for workspace isolation.

### System design

The supervisor agent (running any LLM) receives tasks and routes them based on each CLI's strengths: **Claude Code for complex multi-file refactoring** and architecture decisions, **Gemini CLI for large codebase analysis** leveraging its 1M-token context window and generous free tier, and **Codex CLI for autonomous background tasks** with its cloud sandbox and native CI/CD integration. Each CLI runs in its own git worktree to prevent conflicts.

For shared state, an MCP memory server (SQLite-backed for local development, Redis-backed for production) acts as the common memory bus. All three CLIs connect to this server via their respective MCP configurations. The memory server provides `get_state`/`set_state` for coordination, `search_context`/`write_context` for semantic memory, and task management tools for work distribution.

### Task decomposition and routing

The supervisor decomposes tasks using a capability matrix: complexity score (high → Claude Code), context size (large → Gemini CLI), autonomy requirements (fire-and-forget → Codex CLI), and cost sensitivity (budget-constrained → Gemini CLI's free tier). For critical tasks, the council pattern sends the same prompt to multiple agents and synthesizes via voting, with **quorum requiring 2/3 agreement** before accepting results.

### Error handling and fallback

Each agent invocation includes a timeout, exit code checking, and output validation. **Circuit breakers** disable agents that fail repeatedly (as Bernstein implements). Fallback routing sends failed tasks to an alternative CLI — for example, if Codex CLI times out, the task falls back to Claude Code. All work happens in isolated worktrees, so failures never corrupt the main branch. **Quality gates** (lint, type-check, test suite, PII scan) run before any merge to the main branch.

### Concrete implementation path

The fastest path to a working system: (1) Install all three CLIs and configure them with MCP pointing to a shared `agent-memory-store` instance. (2) Create a LangGraph supervisor that wraps each CLI as a subprocess tool using their headless modes (`claude -p --bare`, `gemini -p --yolo`, `codex exec --full-auto`). (3) Use `git worktree` to create isolated workspaces per agent. (4) Add LangGraph checkpointing (PostgresSaver or RedisSaver) for persistent orchestration state. (5) Implement human-in-the-loop gates via LangGraph's `interrupt` primitive for destructive operations.

Alternatively, for a lighter-weight approach, skip LangGraph entirely and use **Bernstein** for deterministic scheduling or **AgentMux** for MCP-native coordination — both already handle the full lifecycle of multi-CLI orchestration without requiring a framework.

---

## Conclusion

The three-CLI multi-agent system is not a speculative architecture — it is an active, rapidly maturing ecosystem. The key insight is that **MCP has unified the integration surface**: since Claude Code, Gemini CLI, and Codex CLI all speak MCP, a shared MCP memory server eliminates the need for custom inter-agent protocols. Codex CLI's unique ability to run as an MCP server means it can be consumed directly by the other two CLIs without any orchestrator. Claude Code's shared task lists and Agent Teams provide the most sophisticated built-in multi-agent coordination, while Gemini CLI's A2A protocol support opens a path to cross-framework agent communication. LangGraph's provider-agnostic supervisor pattern is the natural orchestration layer, but deterministic schedulers like Bernstein demonstrate that LLM-powered orchestration is not always necessary — and can be more reliable and cost-effective. The dominant architectural pattern across all successful implementations is git worktrees for isolation, MCP for shared context, and test-based quality gates for verification.
