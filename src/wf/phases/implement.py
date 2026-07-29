from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import HookMatcher

from wf import artifacts, claude, git
from wf.config import Config
from wf.hooks import make_readonly_git_hook
from wf.markers import BlockSpec, BlocksError, parse_blocks
from wf.phases.base import PhaseResult, RunContext
from wf.runner import run_shell
from wf.state import State


_SCHEMA = [
    BlockSpec("commit_message", required=False, allow_empty=True),
]


def _extract_commit_subject(text: str) -> str | None:
    try:
        blocks = parse_blocks(text, _SCHEMA)
    except BlocksError:
        return None
    raw = blocks.get("commit_message")
    if not raw:
        return None
    for line in raw.splitlines():
        line = line.strip().strip('"').strip("'").rstrip(".").strip()
        if line:
            return line[:72]
    return None


class ImplementPhase:
    name = "implement"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return [artifacts.plan(config, state)]

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def required_tools(self) -> list[str]:
        return []

    def is_ready(self, config: Config, state: State) -> bool:
        return (
            state.worktree is not None
            and Path(state.worktree).exists()
            and artifacts.plan(config, state).exists()
        )

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        plan_md = artifacts.plan(ctx.config, state)
        plan_qa_md = artifacts.plan_qa(ctx.config, state)

        plan_summary = plan_md.read_text(encoding="utf-8")
        qa_answers: str | None = None
        if plan_qa_md.exists():
            qa_answers = plan_qa_md.read_text(encoding="utf-8")

        worktree = Path(state.worktree)  # type: ignore[arg-type]

        head_before = run_shell(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True
        ).stdout.strip()

        summary = claude.run_phase(
            template_name="implement.md.j2",
            template_ctx={
                "plan_summary": plan_summary,
                "qa_answers": qa_answers,
            },
            cwd=worktree,
            # tools — позитивный whitelist: implement может ровно это и ничего
            # больше. Agent (субагент) сюда намеренно не включён → исследование
            # остаётся работой фазы plan, а implement идёт строго по её карте и
            # не переисследует репозиторий (это был главный пожиратель токенов).
            tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
            setting_sources=["project", "user"],
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="Bash", hooks=[make_readonly_git_hook("implement")])
                ]
            },
            config=ctx.config,
            events=ctx.events,
            phase=self.name,
            iter=state.iteration,
        )

        summary_ref = f"см. summary.md (## {self.name} #{state.iteration})"

        porcelain = run_shell(
            ["git", "-C", str(worktree), "status", "--porcelain"], check=True
        ).stdout
        if not porcelain.strip():
            return PhaseResult.failed(
                "implement: Claude не внёс изменений в worktree"
            )

        commit_msg = _extract_commit_subject(summary.text)
        if commit_msg is None:
            ctx.console.print(
                f"[yellow]implement: <commit_message> не найден, использую fallback; {summary_ref}[/yellow]"
            )
            commit_msg = f"implement plan (iteration {state.iteration})"

        run_shell(["git", "-C", str(worktree), "add", "-A"], check=True)
        run_shell(
            ["git", "-C", str(worktree), "commit", "-m", commit_msg], check=True
        )

        porcelain_after = run_shell(
            ["git", "-C", str(worktree), "status", "--porcelain"], check=True
        ).stdout
        if porcelain_after.strip():
            return PhaseResult.failed(
                "implement: рабочая копия не чиста после коммита"
            )

        # Did this run produce a commit? Guards against `git add` finding nothing.
        head_after = run_shell(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True
        ).stdout.strip()
        if head_after == head_before:
            return PhaseResult.failed(
                "implement: HEAD не сдвинулся, ни одного коммита не создано"
            )

        try:
            base_ref = git.resolve_base_ref(worktree, state.base_branch)
        except RuntimeError as e:
            return PhaseResult.failed(f"implement: {e}")

        # Does the branch carry anything over the base? A new commit can still
        # leave zero diff vs base (e.g. edits that revert the branch to base),
        # which would produce an empty MR downstream.
        if git.commits_ahead(worktree, base_ref) <= 0:
            return PhaseResult.failed(
                "implement: на ветке нет коммитов поверх base_branch"
            )

        return PhaseResult.ok(usage=summary.usage)
