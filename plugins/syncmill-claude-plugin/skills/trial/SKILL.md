---
name: trial
description: Run syncmill's offline walkthrough. Two fake coding agents fix a failing test in a throwaway repository, verification rejects one of them, and the passing change is shown and applied. No login, no real agents, nothing outside a temporary folder.
allowed-tools: Bash(uvx --version) Bash(uvx --from syncmill==0.3.0 syncmill start --trial)
disable-model-invocation: true
---

# Try syncmill offline

syncmill runs several coding agents on one task, verifies each result with the
project's tests, and applies a change only when you say so. This walkthrough
shows that loop with two fake agents, so it needs no login and costs nothing.

1. Check that `uv` is available by running `uvx --version`. If the command is
   missing, stop. Tell the user that syncmill needs [uv](https://docs.astral.sh/uv/)
   and Python 3.12 or newer, that it runs on macOS and Linux only, and that they
   can run this skill again after installing uv.
2. Before running anything, tell the user in two sentences what will happen:
   - The first run downloads syncmill 0.3.0 and its dependencies, which can take
     a few minutes. Later runs take a few seconds.
   - Everything happens in a new temporary folder that is removed afterwards.
     Without a terminal to ask, the walkthrough applies the passing change on its
     own, but only to its own demo repository, never to the user's files.
3. Run exactly this command with a 10-minute timeout (600000 ms), from any
   directory:

   ```bash
   uvx --from syncmill==0.3.0 syncmill start --trial
   ```

4. If it fails, show the last lines of its output and stop. Do not retry with
   other flags or install anything.
5. If it succeeds, explain the output in plain words, following its numbered
   steps:
   - The demo repository has a bug: `add(2, 2)` returns 5, and its test fails.
   - Two agents (fake codex and fake kimi-code) each proposed a fix in their own
     isolated copy; the repository itself was not touched.
   - Both fixes were verified with the test. Candidate B took a shortcut
     (`return 4`) that passes one assertion and fails the other, so verification
     rejected it. Only A could be applied.
   - A's diff was shown, A was applied, and the test was run again before the
     demo repository changed.
6. End with how to use syncmill on a real project, in the user's own terminal.
   It needs the `claude`, `codex` or `kimi` CLI installed and logged in:

   ```bash
   uv tool install syncmill
   syncmill init
   syncmill doctor
   syncmill start
   ```

Do not run syncmill on the user's project, install anything, or change any of
the user's files from this skill.
