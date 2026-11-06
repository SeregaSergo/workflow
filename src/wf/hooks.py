from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
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


def blocked_git_reason(cmd: str) -> str | None:
    """None, если все git-вызовы в Bash-команде read-only, иначе причина запрета."""
    for m in _GIT_INVOCATION_RE.finditer(cmd):
        invocation = m.group(1)
        try:
            tokens = shlex.split(invocation)
        except ValueError:
            return "не удалось распарсить git-команду; запрещено"
        if not tokens or tokens[0] != "git":
            continue
        sub = _git_subcommand(tokens[1:])
        if sub not in _GIT_READONLY_SUBCOMMANDS:
            allowed = ", ".join(sorted(_GIT_READONLY_SUBCOMMANDS))
            return (
                f"разрешены только read-only git команды ({allowed}); "
                f"запрошено: git {sub or '???'}"
            )
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
        reason = blocked_git_reason(input_data.get("tool_input", {}).get("command", ""))
        if reason is None:
            return {}
        return {"decision": "block", "reason": f"{phase_name}: {reason}"}

    return _hook


# ---- command hooks for interactive sessions (`python -m wf.hooks ...`) --------
#
# Устанавливаются в <worktree>/.claude/settings.local.json на время раунда
# правок (см. fixround.py). Модуль намеренно не тянет тяжёлых импортов: хук
# запускается на каждый Bash-вызов.

# Claude Code обрезает вывод хука в контекст на 10 000 символах.
_SESSION_CONTEXT_LIMIT = 9_500


def _cli_git_readonly() -> int:
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return 0
    reason = blocked_git_reason(data.get("tool_input", {}).get("command", ""))
    if reason is None:
        return 0
    print(
        f"wf: {reason}. Идёт раунд правок: историю ветки двигает разработчик, "
        "коммит и push делает wf (`wf push` или `wf resume`).",
        file=sys.stderr,
    )
    return 2


def _cli_session_start(brief_path: str) -> int:
    path = Path(brief_path)
    if not path.exists():
        return 0
    text = path.read_text(encoding="utf-8")
    if len(text) > _SESSION_CONTEXT_LIMIT:
        note = f"\n\n…(обрезано; полный текст: {path})"
        text = text[: _SESSION_CONTEXT_LIMIT - len(note)] + note
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": text,
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["git-readonly"]:
        return _cli_git_readonly()
    if argv[:1] == ["session-start"] and len(argv) == 2:
        return _cli_session_start(argv[1])
    print("usage: python -m wf.hooks git-readonly | session-start <brief>", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
