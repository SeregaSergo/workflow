from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import HookMatcher

from wf import artifacts, claude
from wf.config import Config
from wf.hooks import make_readonly_git_hook
from wf.markers import BlockSpec, BlocksError, parse_blocks
from wf.phases.base import PhaseResult, RunContext
from wf.state import State


_SCHEMA = [
    BlockSpec("review", required=True),
    BlockSpec("feedback", required=False),
]


class ReviewPhase:
    name = "review"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return [artifacts.task(config, state)]

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return [artifacts.review(config, state)]

    def required_tools(self) -> list[str]:
        return []

    def is_ready(self, config: Config, state: State) -> bool:
        return (
            artifacts.task(config, state).exists()
            and artifacts.plan(config, state).exists()
            and state.worktree is not None
            and Path(state.worktree).exists()
        )

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        task_md = artifacts.task(ctx.config, state)
        plan_md = artifacts.plan(ctx.config, state)
        review_md = artifacts.review(ctx.config, state)
        feedback_md = artifacts.feedback(ctx.config, state)

        if review_md.exists():
            return PhaseResult.ok()

        task_summary = task_md.read_text(encoding="utf-8")
        plan_summary = plan_md.read_text(encoding="utf-8")

        summary = claude.run_phase(
            template_name="review.md.j2",
            template_ctx={
                "task_summary": task_summary,
                "base_branch": state.base_branch,
                "branch": state.branch,
            },
            cwd=Path(state.worktree),  # type: ignore[arg-type]
            # read-only ревью: без Write/Edit/Agent; git-мутации дополнительно
            # режет read-only-git hook ниже.
            tools=["Read", "Glob", "Grep", "Bash"],
            setting_sources=["project", "user"],
            hooks={
                "PreToolUse": [
                    HookMatcher(matcher="Bash", hooks=[make_readonly_git_hook("review")])
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
            return PhaseResult.failed(f"review: {e.reason}; {summary_ref}")

        review_content = blocks["review"]
        review_md.parent.mkdir(parents=True, exist_ok=True)
        review_md.write_text(review_content + "\n", encoding="utf-8")

        feedback_content = blocks.get("feedback")
        has_fb = False
        if feedback_content is not None:
            feedback_md.write_text(feedback_content + "\n", encoding="utf-8")
            has_fb = True
        elif feedback_md.exists():
            ctx.console.print(
                f"[yellow]review: блок <feedback> отсутствует, но "
                f"{feedback_md.name} существует на диске; игнорирую файл "
                f"(приоритет — маркер в ответе)[/yellow]"
            )

        state.last_review_has_feedback = has_fb
        return PhaseResult.ok(usage=summary.usage)
