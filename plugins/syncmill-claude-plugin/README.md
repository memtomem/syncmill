# syncmill plugin for Claude Code

Try [syncmill](https://github.com/memtomem/syncmill) from inside Claude Code. The
plugin adds one skill, `/syncmill:trial`, which runs syncmill's offline
walkthrough: two fake coding agents fix a failing test, verification rejects one
of them, and the passing change is shown and applied. It needs no login and calls
no real agent.

## Install

```
/plugin marketplace add memtomem/syncmill
/plugin install syncmill@syncmill
/syncmill:trial
```

## What this plugin runs

- One command, pre-approved for the skill and run only when you type
  `/syncmill:trial`:

  ```bash
  uvx --from syncmill==0.3.0 syncmill start --trial
  ```

  `uvx` downloads syncmill 0.3.0 and its dependencies into uv's cache on the
  first run, which can take a few minutes; later runs take a few seconds.
- The walkthrough works in a new temporary folder with its own `HOME`, state and
  an allowlisted environment, and removes the folder when it finishes. It never
  reads or changes your project, your Git configuration or your agent logins.
  Inside Claude Code there is no terminal to ask, so it applies the passing change
  on its own, to its own demo repository only.
- It also runs `uvx --version` to check that uv is installed.

The plugin ships no hooks, no MCP server, and no scripts or binaries of its own.

## Requirements

- macOS or Linux (syncmill refuses Windows).
- [uv](https://docs.astral.sh/uv/), which provides `uvx`.
- Git on `PATH`.

## After the walkthrough

To use syncmill on a real project, run it in your own terminal. It needs the
`claude`, `codex` or `kimi` CLI installed and logged in:

```bash
uv tool install syncmill
syncmill init
syncmill doctor
syncmill start
```

See the [user guide](https://github.com/memtomem/syncmill/blob/main/docs/user-guide.md).
