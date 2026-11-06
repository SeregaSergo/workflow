from __future__ import annotations

from pathlib import Path

from wf import git
from wf.config import Config
from wf.phases.base import PhaseResult, RunContext
from wf.state import State


class PreparePhase:
    name = "prepare"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def required_tools(self) -> list[str]:
        return []

    def is_ready(self, config: Config, state: State) -> bool:
        return bool(state.branch)

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        if not state.branch:
            return PhaseResult.failed("branch name is empty")

        repo = Path(state.repo_path).resolve()
        if not git.is_repo(repo):
            return PhaseResult.failed(f"not a git repository: {repo}")

        worktree_path = (
            ctx.config.task_dir(state.workflow, state.task_id) / "worktree" / state.branch
        )

        ctx.console.print(f"git fetch origin in {repo}")
        try:
            git.fetch(repo)
        except Exception as e:
            ctx.console.print(f"[yellow]fetch failed:[/yellow] {e}; continuing with local refs")

        try:
            wt = git.prepare_worktree(
                repo_path=repo,
                base_branch=state.base_branch,
                new_branch=state.branch,
                worktree_path=worktree_path,
            )
        except Exception as e:
            return PhaseResult.failed(f"worktree setup failed: {e}")

        copied = git.copy_claude_dir(repo, wt)
        if copied:
            ctx.console.print(f"copied .claude entries: {', '.join(p.name for p in copied)}")
        else:
            ctx.console.print("[dim]no .claude dir to copy[/dim]")

        state.worktree = wt
        try:
            state.base_commit = git.merge_base(wt, state.base_branch)
        except RuntimeError as e:
            ctx.console.print(f"[yellow]не удалось записать base_commit:[/yellow] {e}")
        ctx.console.print(f"worktree: [cyan]{wt}[/cyan]  branch: [cyan]{state.branch}[/cyan]")
        return PhaseResult.ok()
