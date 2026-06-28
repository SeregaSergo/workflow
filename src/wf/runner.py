from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

from wf.config import Config


_env = Environment(undefined=StrictUndefined, autoescape=False)
_env.filters["shquote"] = shlex.quote


def render(template_str: str, ctx: dict[str, Any]) -> str:
    return _env.from_string(template_str).render(**ctx)


@dataclass
class CmdResult:
    cmd: str
    exit_code: int
    stdout: str
    stderr: str

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
    argv = [os.path.expanduser(tok) for tok in shlex.split(cmd_str)]
    if not argv:
        return CmdResult(
            cmd=cmd_str,
            exit_code=127,
            stdout="",
            stderr=f"tools.{name}.cmd rendered to empty command",
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
        )
    return CmdResult(
        cmd=cmd_str,
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


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
