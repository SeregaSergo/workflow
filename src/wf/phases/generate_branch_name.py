from __future__ import annotations

import re
from pathlib import Path

from wf import artifacts, claude
from wf.config import Config
from wf.phases.base import PhaseResult, RunContext
from wf.state import State

_SLUG = re.compile(r"^[a-z0-9][a-z0-9\-]{0,59}$")


class GenerateBranchNamePhase:
    name = "generate_branch_name"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return [artifacts.task(config, state)]

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def required_tools(self) -> list[str]:
        return []

    def is_ready(self, config: Config, state: State) -> bool:
        return artifacts.task(config, state).exists()

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        task_md = artifacts.task(ctx.config, state)
        task_summary = task_md.read_text(encoding="utf-8")

        cwd = Path(state.worktree) if state.worktree else Path(state.repo_path)
        summary = claude.run_phase(
            template_name="branch_name.md.j2",
            template_ctx={
                "task_summary": task_summary,
            },
            cwd=cwd,
            config=ctx.config,
            allowed_tools=[],
            setting_sources=["user"],
            events=ctx.events,
            phase=self.name,
            iter=state.iteration,
        )

        lines = [l.strip().strip("`'\"") for l in summary.text.splitlines() if l.strip()]
        slug = lines[-1] if lines else ""
        if not _SLUG.match(slug):
            return PhaseResult.failed(
                f"generated slug '{slug}' violates naming rules; "
                f"см. summary.md (## {self.name} #{state.iteration})"
            )

        name = f"{state.task_id}-{slug}"
        state.branch = name
        ctx.console.print(f"branch: [cyan]{name}[/cyan]")
        return PhaseResult.ok(usage=summary.usage)
