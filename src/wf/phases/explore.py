from __future__ import annotations

import re
from pathlib import Path

from wf import artifacts, claude
from wf.config import Config
from wf.markers import BlockSpec, BlocksError, parse_blocks
from wf.phases.base import PhaseResult, RunContext
from wf.state import State


_SCHEMA = [
    BlockSpec("task_url", required=False),
    BlockSpec("task_title", required=False),
    BlockSpec("main_task_info", required=True),
    BlockSpec("additional_info", required=False),
]


def _strip_placeholder(cmd: str, name: str) -> str:
    return re.sub(r"\s*\{\{\s*" + name + r"\s*\}\}\s*", " ", cmd).strip()


class ExplorePhase:
    name = "explore"

    def required_artifacts(self, config: Config, state: State) -> list[Path]:
        return []

    def produced_artifacts(self, config: Config, state: State) -> list[Path]:
        return [artifacts.task(config, state)]

    def required_tools(self) -> list[str]:
        return ["fetch_task"]

    def is_ready(self, config: Config, state: State) -> bool:
        return True

    def _fetch_cmd(self, config: Config) -> str | None:
        spec = config.tools.get("fetch_task")
        if spec is None:
            return None
        return _strip_placeholder(spec.cmd, "task_id")

    def _fetch_article_cmd(self, config: Config) -> str:
        # Опциональный инструмент: если статьи в трекере не используются,
        # его просто нет в tools: — фаза работает как раньше.
        spec = config.tools.get("fetch_article")
        if spec is None:
            return ""
        return _strip_placeholder(spec.cmd, "article_id")

    def run(self, state: State, ctx: RunContext) -> PhaseResult:
        task_md = artifacts.task(ctx.config, state)
        if task_md.exists():
            return PhaseResult.ok()

        fetch_cmd = self._fetch_cmd(ctx.config)
        if not fetch_cmd:
            return PhaseResult.failed(
                "tool 'fetch_task' is not configured in tools:"
            )

        fetch_article_cmd = self._fetch_article_cmd(ctx.config)

        cwd = Path(state.worktree) if state.worktree else Path(state.repo_path)

        mcp_servers, missing = ctx.config.mcp_for_phase(self.name)
        for name in missing:
            ctx.console.print(
                f"[yellow]explore: MCP-сервер '{name}' включён в "
                f"claude.phase_mcp_servers, но не описан в mcp_servers:[/yellow]"
            )

        summary = claude.run_phase(
            template_name="explore.md.j2",
            template_ctx={
                "task_id": state.task_id,
                "fetch_cmd": fetch_cmd,
                "fetch_article_cmd": fetch_article_cmd,
            },
            cwd=cwd,
            config=ctx.config,
            # tools режет только встроенные тулы; MCP-серверы (wiki) приходят
            # отдельно через mcp_servers и остаются доступны под bypassPermissions.
            tools=["Bash", "Read", "Glob", "Grep"],
            setting_sources=["project", "user"],
            events=ctx.events,
            phase=self.name,
            iter=state.iteration,
            mcp_servers=mcp_servers,
        )

        try:
            blocks = parse_blocks(summary.text, _SCHEMA)
        except BlocksError as e:
            return PhaseResult.failed(
                f"explore: {e.reason}; см. summary.md (## {self.name} #{state.iteration})"
            )

        main_info = blocks["main_task_info"]

        task_url = blocks.get("task_url", "")
        if task_url:
            state.task_url = task_url
        else:
            ctx.console.print(
                "[yellow]explore: <task_url> section is empty[/yellow]"
            )

        task_title = " ".join(blocks.get("task_title", "").split())
        if task_title:
            state.task_title = task_title
        else:
            ctx.console.print(
                "[yellow]explore: <task_title> section is empty[/yellow]"
            )

        additional = blocks.get("additional_info", "")

        parts = [f"# Task {state.task_id}\n\n{main_info}\n"]
        if additional:
            parts.append(f"\n## Additional info\n\n{additional}\n")
        task_md.parent.mkdir(parents=True, exist_ok=True)
        task_md.write_text("".join(parts), encoding="utf-8")

        return PhaseResult.ok(usage=summary.usage)
