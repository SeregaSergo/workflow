from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import HookMatcher

from wf import artifacts, claude, git
from wf.config import Config
from wf.hooks import make_readonly_git_hook
from wf.markers import BlockSpec, BlocksError, commit_subject, parse_blocks
from wf.phases.base import PhaseResult, RunContext
from wf.runner import run_shell
from wf.state import State


_SCHEMA = [
    BlockSpec("plan", required=False),
    BlockSpec("plan_qa", required=False),
    BlockSpec("commit_message", required=False, allow_empty=True),
]

# Пресет Claude Code склоняет модель задавать уточняющие вопросы в тексте —
# направляем их в единственный рабочий канал, блок <plan_qa>.
_SYSTEM_APPEND = (
    "Phase role: implementer. If you are blocked on a decision only a human can "
    "make, the ONLY channel to reach them is the <plan_qa> block described in "
    "the task prompt — a question asked any other way will never be seen."
)


def _porcelain(worktree: Path) -> str:
    return run_shell(
        ["git", "-C", str(worktree), "status", "--porcelain"], check=True
    ).stdout.strip()


class CodingPhase:
    name = "coding"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return [artifacts.task(config, state)]

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return [artifacts.plan(config, state)]

    def required_tools(self) -> list[str]:
        return []

    def is_ready(self, config: Config, state: State) -> bool:
        return (
            state.worktree is not None
            and Path(state.worktree).exists()
            and artifacts.task(config, state).exists()
        )

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        task_md = artifacts.task(ctx.config, state)
        plan_md = artifacts.plan(ctx.config, state)
        plan_qa_md = artifacts.plan_qa(ctx.config, state)
        plan_qa_tpl = plan_qa_md.parent / (plan_qa_md.name + ".template")
        prev_fb = artifacts.previous_feedback(ctx.config, state)

        if plan_qa_tpl.exists():
            ctx.console.print(
                f"[yellow]coding: заполни ответы в {plan_qa_tpl.name} и переименуй в "
                f"{plan_qa_md.name}[/yellow]"
            )
            return PhaseResult.needs_human_input(plan_qa_tpl, plan_qa_md)

        qa_answers: str | None = None
        if plan_qa_md.exists():
            qa_answers = plan_qa_md.read_text(encoding="utf-8")

        previous_feedback: str | None = None
        if prev_fb is not None:
            previous_feedback = prev_fb.read_text(encoding="utf-8")

        task_summary = task_md.read_text(encoding="utf-8")
        worktree = Path(state.worktree)  # type: ignore[arg-type]

        head_before = run_shell(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True
        ).stdout.strip()

        summary = claude.run_phase(
            template_name="coding.md.j2",
            template_ctx={
                "task_summary": task_summary,
                "previous_feedback": previous_feedback,
                "qa_answers": qa_answers,
            },
            cwd=worktree,
            # tools — позитивный whitelist. Agent (субагент) намеренно не
            # включён: фаза не разбегается в параллельные Explore, это был
            # главный пожиратель токенов. Коммитит сама фаза, поэтому
            # git-мутации режет read-only-git hook.
            tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
            setting_sources=["project", "user"],
            system_prompt_append=_SYSTEM_APPEND,
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="Bash", hooks=[make_readonly_git_hook("coding")])
                ]
            },
            config=ctx.config,
            events=ctx.events,
            phase=self.name,
            iter=state.iteration,
        )

        summary_ref = f"см. summary.md (## {self.name} #{state.iteration})"

        try:
            blocks = parse_blocks(summary.text, _SCHEMA)
        except BlocksError as e:
            return PhaseResult.failed(f"coding: {e.reason}; {summary_ref}")

        qa_content = blocks.get("plan_qa")
        if qa_content is not None:
            plan_qa_tpl.parent.mkdir(parents=True, exist_ok=True)
            plan_qa_tpl.write_text(qa_content + "\n", encoding="utf-8")
            if _porcelain(worktree):
                ctx.console.print(
                    "[yellow]coding: в worktree остались незакоммиченные изменения — "
                    "после resume фаза продолжит с них[/yellow]"
                )
            ctx.console.print(
                f"[yellow]coding: заполни ответы в {plan_qa_tpl.name} и переименуй в "
                f"{plan_qa_md.name}[/yellow]"
            )
            return PhaseResult.needs_human_input(plan_qa_tpl, plan_qa_md)

        plan_content = blocks.get("plan")
        if plan_content is not None:
            plan_md.parent.mkdir(parents=True, exist_ok=True)
            plan_md.write_text(plan_content + "\n", encoding="utf-8")
        else:
            ctx.console.print(
                f"[yellow]coding: <plan> не найден, {plan_md.name} не записан; {summary_ref}[/yellow]"
            )

        if not _porcelain(worktree):
            return PhaseResult.failed("coding: Claude не внёс изменений в worktree")

        commit_msg = commit_subject(blocks.get("commit_message"))
        if commit_msg is None:
            ctx.console.print(
                f"[yellow]coding: <commit_message> не найден, использую fallback; {summary_ref}[/yellow]"
            )
            commit_msg = f"implement task (iteration {state.iteration})"

        run_shell(["git", "-C", str(worktree), "add", "-A"], check=True)
        run_shell(
            ["git", "-C", str(worktree), "commit", "-m", commit_msg], check=True
        )

        if _porcelain(worktree):
            return PhaseResult.failed("coding: рабочая копия не чиста после коммита")

        # Did this run produce a commit? Guards against `git add` finding nothing.
        head_after = run_shell(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"], check=True
        ).stdout.strip()
        if head_after == head_before:
            return PhaseResult.failed(
                "coding: HEAD не сдвинулся, ни одного коммита не создано"
            )

        try:
            base_ref = git.resolve_base_ref(worktree, state.base_branch)
        except RuntimeError as e:
            return PhaseResult.failed(f"coding: {e}")

        # Does the branch carry anything over the base? A new commit can still
        # leave zero diff vs base (e.g. edits that revert the branch to base),
        # which would produce an empty MR downstream.
        if git.commits_ahead(worktree, base_ref) <= 0:
            return PhaseResult.failed(
                "coding: на ветке нет коммитов поверх base_branch"
            )

        return PhaseResult.ok(usage=summary.usage)
