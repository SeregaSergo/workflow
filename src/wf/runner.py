from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from wf.config import Config
from wf.state import State


_env = Environment(undefined=StrictUndefined, autoescape=False)
_env.filters["shquote"] = shlex.quote

# Jinja-выражения вырезаются перед поиском shell-операторов: `|` в
# {{message|shquote}} — фильтр, а не пайп.
_JINJA_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", re.S)
_SHELL_METACHARS = ("&&", "||", ";", "|", "&", ">", "<", "$(", "`", "\n")


def render(template_str: str, ctx: dict[str, Any]) -> str:
    return _env.from_string(template_str).render(**ctx)


def needs_shell(template_str: str) -> bool:
    """Есть ли в самом шаблоне cmd shell-операторы.

    Проверяется шаблон, а не отрендеренная строка: режим запуска задаёт автор
    конфига, подставленные значения (message, task_title и т.п.) на выбор не
    влияют и не могут «случайно» включить shell.
    """
    return any(m in _JINJA_RE.sub("", template_str) for m in _SHELL_METACHARS)


@dataclass
class CmdResult:
    cmd: str
    exit_code: int
    stdout: str
    stderr: str
    shell: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def run_tool(
    config: Config,
    name: str,
    ctx: dict[str, Any],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
) -> CmdResult:
    spec = config.tools.get(name)
    if spec is None:
        raise KeyError(f"tool '{name}' not configured in tools:")
    cmd_str = render(spec.cmd, ctx)
    shell = needs_shell(spec.cmd)
    if shell:
        # /bin/sh сам разберёт операторы, кавычки и ~ — argv не строим.
        argv = ["/bin/sh", "-c", cmd_str]
    else:
        argv = [os.path.expanduser(tok) for tok in shlex.split(cmd_str)]
    if not argv or (shell and not cmd_str.strip()):
        return CmdResult(
            cmd=cmd_str,
            exit_code=127,
            stdout="",
            stderr=f"tools.{name}.cmd rendered to empty command",
            shell=shell,
        )
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        return CmdResult(
            cmd=cmd_str,
            exit_code=127,
            stdout="",
            stderr=(
                f"executable not found: {argv[0]!r} (tool '{name}', rendered cmd: {cmd_str!r}).\n"
                f"Fix tools.{name}.cmd in your wf config: the first token must be on $PATH "
                f"or be an absolute path. ~ is expanded only when written as ~/ or ~user/ — "
                f"forms like ~. or ~something stay literal. $VAR is never expanded "
                f"(wf doesn't run cmd through a shell). OSError: {e}"
            ),
            shell=shell,
        )
    return CmdResult(
        cmd=cmd_str,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        shell=shell,
    )


def tool_context(state: State) -> dict[str, Any]:
    """Общий контекст, доступный в cmd любого внешнего инструмента.

    Фаза добавляет к нему свои ключи (channel_id/message у notify и т.п.).
    Пустые значения отдаются как "" — StrictUndefined ругается только на
    отсутствующие имена, так что шаблон конфига не ломается на задаче, где,
    скажем, ещё нет mr_url.
    """
    return {
        "task_id": state.task_id,
        "task_url": state.task_url or "",
        "task_title": state.task_title or state.task_id,
        "workflow": state.workflow,
        "branch": state.branch or "",
        "base_branch": state.base_branch,
        "repo_path": str(state.repo_path),
        "worktree": str(state.worktree) if state.worktree else "",
        "mr_url": state.mr_url or "",
        "iteration": state.iteration,
    }


def run_shell(
    cmd: list[str] | str,
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    check: bool = False,
) -> CmdResult:
    if isinstance(cmd, str):
        argv = shlex.split(cmd)
        printable = cmd
    else:
        argv = cmd
        printable = " ".join(shlex.quote(a) for a in argv)
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    result = CmdResult(
        cmd=printable,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    if check and not result.ok:
        raise RuntimeError(
            f"command failed (exit={result.exit_code}): {printable}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result
