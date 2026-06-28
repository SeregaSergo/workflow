from __future__ import annotations

from pathlib import Path

from wf import git as wfgit
from wf.config import Config
from wf.phases.base import PhaseResult, RunContext
from wf.state import State


class CleanupWorktreePhase:
    name = "cleanup_worktree"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def required_tools(self) -> list[str]:
        return []

    def is_ready(self, config: Config, state: State) -> bool:
        return True

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        if state.worktree is None:
            ctx.events.write(
                self.name,
                state.iteration,
                "phase_note",
                text="skipped: state.worktree пуст",
            )
            ctx.console.print("[dim]нет worktree для удаления[/dim]")
            return PhaseResult.ok()

        path = Path(state.worktree)
        wfgit.remove_worktree(Path(state.repo_path), path)

        if path.exists():
            ctx.events.write(
                self.name,
                state.iteration,
                "phase_note",
                text=f"WARN: не удалось удалить worktree: {path}",
            )
            ctx.console.print(
                f"[yellow]не удалось удалить worktree:[/yellow] {path}"
            )
            return PhaseResult.ok()

        state.worktree = None
        ctx.events.write(
            self.name,
            state.iteration,
            "phase_note",
            text=f"worktree удалён: {path}",
        )
        ctx.console.print(f"[green]worktree удалён:[/green] {path}")
        return PhaseResult.ok()
