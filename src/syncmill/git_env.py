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


def isolated_git_env(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy the process environment without ambient Git repository routing."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in GIT_REPOSITORY_ROUTING_ENV
    }
    if overrides:
        env.update(overrides)
    return env
