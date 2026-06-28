from __future__ import annotations

from pathlib import Path

from wf import artifacts, claude
from wf.config import Config
from wf.markers import BlockSpec, BlocksError, parse_blocks
from wf.phases.base import PhaseResult, RunContext
from wf.state import State


_SCHEMA = [
    BlockSpec("plan_ready", required=False),
    BlockSpec("plan_qa", required=False),
]


class PlanPhase:
    name = "plan"

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

        if plan_md.exists():
            return PhaseResult.ok()

        if plan_qa_tpl.exists():
            ctx.console.print(
                f"[yellow]plan: заполни ответы в {plan_qa_tpl.name} и переименуй в "
                f"{plan_qa_md.name}[/yellow]"
            )
            return PhaseResult.needs_human_input(plan_qa_tpl, plan_qa_md)

        qa_answers: str | None = None
        if plan_qa_md.exists():
            qa_answers = plan_qa_md.read_text(encoding="utf-8")
            if plan_qa_tpl.exists():
                plan_qa_tpl.unlink()

        previous_feedback: str | None = None
        if prev_fb is not None:
            previous_feedback = prev_fb.read_text(encoding="utf-8")

        task_summary = task_md.read_text(encoding="utf-8")

        summary = claude.run_phase(
            template_name="plan.md.j2",
            template_ctx={
                "task_summary": task_summary,
                "previous_feedback": previous_feedback,
                "qa_answers": qa_answers,
            },
            cwd=Path(state.worktree),
            allowed_tools=["Read", "Glob", "Grep"],
            setting_sources=["project", "user"],
            config=ctx.config,
            events=ctx.events,
            phase=self.name,
            iter=state.iteration,
        )

        summary_ref = f"см. summary.md (## {self.name} #{state.iteration})"

        try:
            blocks = parse_blocks(summary.text, _SCHEMA)
        except BlocksError as e:
            return PhaseResult.failed(f"plan: {e.reason}; {summary_ref}")

        ready_content = blocks.get("plan_ready")
        qa_content = blocks.get("plan_qa")

        if ready_content is not None and qa_content is not None:
            return PhaseResult.failed(
                f"plan: LLM вернул и <plan_ready>, и <plan_qa> — переспроси; {summary_ref}"
            )

        if ready_content is not None:
            plan_md.parent.mkdir(parents=True, exist_ok=True)
            plan_md.write_text(ready_content + "\n", encoding="utf-8")
            return PhaseResult.ok(usage=summary.usage)

        if qa_content is not None:
            plan_qa_tpl.parent.mkdir(parents=True, exist_ok=True)
            plan_qa_tpl.write_text(qa_content + "\n", encoding="utf-8")
            ctx.console.print(
                f"[yellow]plan: заполни ответы в {plan_qa_tpl.name} и переименуй в "
                f"{plan_qa_md.name}[/yellow]"
            )
            return PhaseResult.needs_human_input(plan_qa_tpl, plan_qa_md)

        return PhaseResult.failed(
            f"plan: LLM не вернул ни <plan_ready>, ни <plan_qa>; {summary_ref}"
        )
