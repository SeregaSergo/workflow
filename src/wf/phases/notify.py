from __future__ import annotations

import random
import time
from pathlib import Path

from wf.claude import render_template
from wf.config import Config
from wf.phases.base import PhaseResult, RunContext
from wf.runner import run_tool
from wf.state import State

_REVIEWERS_PER_MR = 2


def _pick_reviewers(pool: list[str], k: int = _REVIEWERS_PER_MR) -> list[str]:
    if not pool:
        return []
    return random.sample(pool, min(k, len(pool)))


class NotifyPhase:
    name = "notify"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def required_tools(self) -> list[str]:
        return ["notify"]

    def is_ready(self, config: Config, state: State) -> bool:
        wf_spec = config.workflows.get(state.workflow)
        return (
            "notify" in config.tools
            and bool(state.mr_url)
            and wf_spec is not None
            and bool(wf_spec.notify_channel_id)
        )

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        wf_spec = ctx.config.workflows[state.workflow]
        channel_id = wf_spec.notify_channel_id
        assert channel_id is not None  # guaranteed by is_ready

        picked = _pick_reviewers(wf_spec.reviewers)
        message = render_template(
            "notify_message.txt.j2",
            {
                "task_title": state.task_title or state.task_id,
                "mr_url": state.mr_url or "",
                "task_url": state.task_url or "",
                "reviewers": ", ".join(picked),
            },
        )

        cwd = Path(state.worktree) if state.worktree else None

        started = time.monotonic()
        result = run_tool(
            ctx.config,
            "notify",
            ctx={"channel_id": channel_id, "message": message},
            cwd=cwd,
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        ctx.events.write(
            self.name,
            state.iteration,
            "external_tool",
            name="notify",
            cmd=result.cmd,
            exit_code=result.exit_code,
            duration_ms=duration_ms,
            stdout_preview=result.stdout[:500],
            stderr_preview=result.stderr[:500],
        )

        if not result.ok:
            return PhaseResult.failed(
                f"notify exited {result.exit_code}: {result.stderr.strip()}"
            )

        ctx.events.write(
            self.name,
            state.iteration,
            "phase_note",
            text=f"notification sent to channel {channel_id}",
        )
        ctx.console.print("[green]notification sent[/green]")
        return PhaseResult.ok()
