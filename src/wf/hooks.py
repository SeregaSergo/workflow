from __future__ import annotations

import re
import shlex
from typing import Any, Awaitable, Callable

# Extracts each `git ...` invocation up to the next shell metacharacter, so
# chained commands like `cd x && git status` are inspected one by one.
_GIT_INVOCATION_RE = re.compile(r"(?:^|[;|&(\n`])\s*(git\b[^;|&)\n`]*)")

# Read-only subcommands Claude is allowed to run. Anything that mutates the
# repo (commit/push/reset/checkout/rebase/...) — and anything we can't
# classify — is blocked.
_GIT_READONLY_SUBCOMMANDS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "blame",
        "ls-files",
        "ls-tree",
        "cat-file",
        "rev-parse",
        "rev-list",
        "describe",
        "shortlog",
        "for-each-ref",
        "merge-base",
        "name-rev",
    }
)

# `git`-level options that consume the next token as their value.
_GIT_GLOBAL_OPTS_WITH_VALUE = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix"}
)


def _git_subcommand(tokens: list[str]) -> str | None:
    """Return the first positional token after global git options, or None."""
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        if tok in _GIT_GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return tok
    return None


def make_readonly_git_hook(
    phase_name: str,
) -> Callable[[Any, str | None, Any], Awaitable[dict[str, Any]]]:
    """PreToolUse hook factory: blocks any non-read-only `git` invocation in Bash.

    Error messages are prefixed with `phase_name` so the block reason makes it
    obvious which phase rejected the command.
    """

    async def _hook(
        input_data: Any, tool_use_id: str | None, context: Any
    ) -> dict[str, Any]:
        if input_data.get("tool_name") != "Bash":
            return {}
        cmd = input_data.get("tool_input", {}).get("command", "")
        for m in _GIT_INVOCATION_RE.finditer(cmd):
            invocation = m.group(1)
            try:
                tokens = shlex.split(invocation)
            except ValueError:
                return {
                    "decision": "block",
                    "reason": f"{phase_name}: не удалось распарсить git-команду; запрещено",
                }
            if not tokens or tokens[0] != "git":
                continue
            sub = _git_subcommand(tokens[1:])
            if sub not in _GIT_READONLY_SUBCOMMANDS:
                allowed = ", ".join(sorted(_GIT_READONLY_SUBCOMMANDS))
                return {
                    "decision": "block",
                    "reason": (
                        f"{phase_name}: разрешены только read-only git команды ({allowed}); "
                        f"запрошено: git {sub or '???'}"
                    ),
                }
        return {}

    return _hook
