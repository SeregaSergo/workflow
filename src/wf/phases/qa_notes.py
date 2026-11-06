from __future__ import annotations

import json
import time
from pathlib import Path

from claude_agent_sdk import HookMatcher

from wf import artifacts, claude, git
from wf.config import Config
from wf.hooks import make_readonly_git_hook
from wf.markers import BlockSpec, BlocksError, parse_blocks
from wf.phases.base import PhaseResult, RunContext
from wf.runner import CmdResult, run_tool, strip_placeholder, tool_context, with_arg
from wf.state import State

_SCHEMA = [BlockSpec("qa_notes", required=True)]

# Тег задачи, при котором тестирование не нужно и заметка не пишется.
_SKIP_TAG = "noqa"


class QaNotesPhase:
    """Пишет QA-заметку в qa_notes.md и публикует её в задачу через
    tools.qa_comment (и переводит задачу дальше, если так написана команда)."""

    name = "qa_notes"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return [artifacts.qa_notes(config, state)]

    def required_tools(self) -> list[str]:
        return ["fetch_task", "qa_comment"]

    def is_ready(self, config: Config, state: State) -> bool:
        return (
            bool(state.branch)
            and state.worktree is not None
            and Path(state.worktree).exists()
        )

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        if state.qa_notes_posted:
            ctx.console.print("[dim]QA-заметка уже опубликована[/dim]")
            return PhaseResult.ok()

        worktree = Path(state.worktree)  # type: ignore[arg-type]
        fetch_template = ctx.config.tools["fetch_task"].cmd

        started = time.monotonic()
        fetched = run_tool(
            ctx.config,
            "fetch_task",
            tool_context(state),
            cwd=worktree,
            cmd_template=with_arg(fetch_template, "task_id"),
        )
        self._log_tool(ctx, state, "fetch_task", fetched, started)
        if not fetched.ok:
            return PhaseResult.failed(
                f"qa_notes: fetch_task exited {fetched.exit_code}: {fetched.stderr.strip()}"
            )
        try:
            task = json.loads(fetched.stdout)
        except json.JSONDecodeError:
            return PhaseResult.failed(
                "qa_notes: fetch_task вернул не JSON — не из чего взять теги задачи"
            )
        tags = [str(t).strip().lower() for t in task.get("tags") or []]
        if _SKIP_TAG in tags:
            ctx.events.write(
                self.name, state.iteration, "phase_note", text="тег NoQA — заметка не нужна"
            )
            ctx.console.print("[dim]тег NoQA — QA-заметка не нужна[/dim]")
            return PhaseResult.ok()

        notes_md = artifacts.qa_notes(ctx.config, state)
        usage = None
        if notes_md.exists():
            ctx.console.print(f"[dim]использую ранее сгенерированный {notes_md.name}[/dim]")
        else:
            try:
                base = git.fork_point(worktree, state.base_branch, state.base_commit)
            except RuntimeError as e:
                return PhaseResult.failed(f"qa_notes: {e}")

            summary = claude.run_phase(
                template_name="qa_notes.md.j2",
                template_ctx={
                    "task_id": state.task_id,
                    "branch": state.branch,
                    "base": base,
                    "task_json": fetched.stdout.strip(),
                    "fetch_cmd": strip_placeholder(fetch_template, "task_id"),
                },
                cwd=worktree,
                tools=["Read", "Glob", "Grep", "Bash"],
                setting_sources=["project", "user"],
                hooks={
                    "PreToolUse": [
                        HookMatcher(matcher="Bash", hooks=[make_readonly_git_hook("qa_notes")])
                    ]
                },
                config=ctx.config,
                events=ctx.events,
                phase=self.name,
                iter=state.iteration,
            )
            try:
                blocks = parse_blocks(summary.text, _SCHEMA)
            except BlocksError as e:
                return PhaseResult.failed(
                    f"qa_notes: {e.reason}; см. summary.md (## {self.name} #{state.iteration})"
                )
            notes_md.parent.mkdir(parents=True, exist_ok=True)
            notes_md.write_text(blocks["qa_notes"] + "\n", encoding="utf-8")
            usage = summary.usage

        text = notes_md.read_text(encoding="utf-8").strip()
        if not text:
            return PhaseResult.failed(
                f"qa_notes: {notes_md} пуст — впиши текст или удали файл, "
                f"чтобы сгенерировать заново, и сделай wf resume"
            )
        ctx.console.print(f"[green]QA-заметка[/green] {notes_md}:")
        ctx.console.print(text, markup=False, highlight=False)

        started = time.monotonic()
        posted = run_tool(
            ctx.config,
            "qa_comment",
            ctx={**tool_context(state), "qa_notes": text},
            cwd=worktree,
        )
        self._log_tool(ctx, state, "qa_comment", posted, started)
        if not posted.ok:
            return PhaseResult.failed(
                f"qa_comment exited {posted.exit_code}: {posted.stderr.strip()} "
                f"(комментарий мог уже уйти — проверь задачу перед wf resume; "
                f"текст сохранён в {notes_md.name} и будет переиспользован)"
            )

        state.qa_notes_posted = True
        ctx.console.print("[green]QA-заметка опубликована[/green]")
        return PhaseResult.ok(usage=usage)

    def _log_tool(
        self, ctx: RunContext, state: State, name: str, result: CmdResult, started: float
    ) -> None:
        ctx.events.write(
            self.name,
            state.iteration,
            "external_tool",
            name=name,
            cmd=result.cmd,
            shell=result.shell,
            exit_code=result.exit_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout_preview=result.stdout[:500],
            stderr_preview=result.stderr[:500],
        )
