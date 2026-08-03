"""Safe child environments for Git operations bound to an explicit checkout."""

from __future__ import annotations

import os
from collections.abc import Mapping


# Git documents these variables as repository, worktree, index, ref namespace,
# shallow-boundary, or object-database routing controls. An ambient value can
# make a command with an explicit cwd read or mutate a different repository.
GIT_REPOSITORY_ROUTING_ENV = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_SHALLOW_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
)


# Git also accepts configuration through the environment, and configuration can
# execute code: core.hooksPath points at scripts git runs itself, include.path
# pulls in an arbitrary config file, and alias.<name> entries beginning with "!"
# are shell commands. An ambient value therefore survives an explicit cwd the way
# the routing variables above do — it does not redirect *which* repository is
# read, it redirects what running a command in that repository *does*. syncmill
# runs git inside worktrees an agent can write to, so these are stripped too.
#
# GIT_CONFIG_KEY_<n>/GIT_CONFIG_VALUE_<n> are numbered, hence the prefix match in
# is_git_routing_var. Variables that merely *configure* an external helper the
# user legitimately relies on (GIT_SSH_COMMAND, GIT_ASKPASS, GIT_EXEC_PATH) are
# deliberately kept: stripping them breaks real authentication and custom git
# installs, and unlike the above they are not a syncmill-introduced surface.
GIT_CONFIG_INJECTION_ENV = frozenset(
    {
        "GIT_CONFIG",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    }
)

# Git also lets the environment redefine what a PATHSPEC means, which silently
# changes the answer to a question rather than where it is asked. syncmill asks
# git two security-relevant pathspec questions — is this state root ignored
# (`check-ignore`), is this identity marker tracked (`ls-files`) — and both
# read as a benign "no" when the pathspec stops meaning what the caller wrote:
# GIT_LITERAL_PATHSPECS turns `:(icase)…` magic into a filename nothing
# matches, and the glob/icase toggles reinterpret a plain path. Stripped for
# the same reason as the routing variables above.
GIT_PATHSPEC_SEMANTICS_ENV = frozenset(
    {
        "GIT_LITERAL_PATHSPECS",
        "GIT_GLOB_PATHSPECS",
        "GIT_NOGLOB_PATHSPECS",
        "GIT_ICASE_PATHSPECS",
    }
)

_NUMBERED_CONFIG_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def is_git_routing_var(key: str) -> bool:
    """True if ``key`` routes a Git command's repository, injects its config, or
    redefines pathspec semantics.

    The single predicate behind every isolated child environment syncmill builds
    (git calls here, agent and gate subprocesses in ``agents.py``/``gates.py``),
    so the three can't drift on what "isolated" means.
    """
    return (
        key in GIT_REPOSITORY_ROUTING_ENV
        or key in GIT_CONFIG_INJECTION_ENV
        or key in GIT_PATHSPEC_SEMANTICS_ENV
        or key.startswith(_NUMBERED_CONFIG_PREFIXES)
    )


def isolated_git_env(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy the process environment without ambient Git routing or config injection."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not is_git_routing_var(key)
    }
    if overrides:
        env.update(overrides)
    return env
