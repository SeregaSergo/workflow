from __future__ import annotations

import time
from pathlib import Path

from wf.config import Config
from wf.phases.base import PhaseResult, RunContext
from wf.runner import run_tool, tool_context
from wf.state import State


def _parse_url(stdout: str) -> str | None:
    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            return line
        return None
    return None


class MrPhase:
    name = "mr"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def required_tools(self) -> list[str]:
        return ["create_mr"]

    def is_ready(self, config: Config, state: State) -> bool:
        return (
            state.worktree is not None
            and Path(state.worktree).exists()
            and "create_mr" in config.tools
        )

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        if state.mr_url:
            ctx.console.print(f"[dim]mr_url уже задан: {state.mr_url}[/dim]")
            return PhaseResult.ok()

        worktree = Path(state.worktree)  # type: ignore[arg-type]

        started = time.monotonic()
        result = run_tool(
            ctx.config,
            "create_mr",
            ctx={**tool_context(state), "worktree": str(worktree)},
            cwd=worktree,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        ctx.events.write(
            self.name,
            state.iteration,
            "external_tool",
            name="create_mr",
            cmd=result.cmd,
            shell=result.shell,
            exit_code=result.exit_code,
            duration_ms=duration_ms,
            stdout_preview=result.stdout[:500],
            stderr_preview=result.stderr[:500],
        )

        if not result.ok:
            return PhaseResult.failed(
                f"create_mr exited {result.exit_code}: {result.stderr.strip()}"
            )

        summary_ref = f"см. summary.md (## {self.name} #{state.iteration})"
        url = _parse_url(result.stdout)
        if not url:
            return PhaseResult.failed(
                f"create_mr не вернул URL в stdout ({summary_ref})"
            )

        state.mr_url = url
        ctx.events.write(
            self.name, state.iteration, "phase_note", text=f"mr_url={url}"
        )
        ctx.console.print(f"[green]mr_url[/green]: {url}")
        return PhaseResult.ok()
