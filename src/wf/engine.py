from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from wf import artifacts, run_log
from wf.config import Config
from wf.phases.base import PhaseResult, RunContext
from wf.run_log import EventsWriter
from wf.state import PhaseRun, State, UsageData
from wf.workflow import RepeatAt, Workflow


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_tokens(n: int) -> str:
    if n < 10_000:
        return str(n)
    return f"{n / 1000:.1f}k"


def _accumulate_usage(state: State, usage: UsageData) -> None:
    state.total_input_tokens += usage.input_tokens
    state.total_output_tokens += usage.output_tokens
    state.total_cache_read_tokens += usage.cache_read_input_tokens
    state.total_cost_usd += usage.total_cost_usd


def _usage_suffix(usage: UsageData | None) -> str:
    if usage is None:
        return ""
    return (
        f"  in={_fmt_tokens(usage.input_tokens)}"
        f" out={_fmt_tokens(usage.output_tokens)}"
        f" cache_read={_fmt_tokens(usage.cache_read_input_tokens)}"
        f" ${usage.total_cost_usd:.4f}"
    )


class Engine:
    def __init__(
        self,
        workflow: Workflow,
        state: State,
        config: Config,
        state_path: Path,
        console: Console,
    ) -> None:
        self.workflow = workflow
        self.state = state
        self.config = config
        self.state_path = state_path
        self.console = console
        self.events_path = artifacts.events_path(config, state)
        self.summary_path = artifacts.summary_path(config, state)
        self.events: EventsWriter | None = None

    def _rerender(self) -> None:
        try:
            run_log.rerender_summary(self.events_path, self.summary_path, self.state)
        except Exception as e:
            # Summary is a derived file — failure to rerender shouldn't kill
            # the engine, but we want to know.
            self.console.print(f"[yellow]summary rerender failed:[/yellow] {e}")

    def step(self) -> PhaseResult:
        if self.state.terminal:
            self.console.print(f"[dim]terminal={self.state.terminal}, nothing to do[/dim]")
            return PhaseResult.ok()

        phase_name = self.state.current_stage
        phase_cls = self.workflow.phases.get(phase_name)
        if phase_cls is None:
            return self._fail(f"unknown phase '{phase_name}' in workflow '{self.workflow.name}'")

        phase = phase_cls()

        missing = [
            str(p) for p in phase.required_artifacts(self.config, self.state) if not p.exists()
        ]
        if missing:
            return self._fail(
                f"phase '{phase_name}' not ready, missing artifacts: {', '.join(missing)}"
            )
        if not phase.is_ready(self.config, self.state):
            return self._fail(f"phase '{phase_name}' reports not ready")

        wf_spec = self.config.workflows.get(self.state.workflow)
        pause_before = wf_spec.pause_before if wf_spec is not None else []
        if phase_name in pause_before and self.state.pause_consumed_for != phase_name:
            self.state.pause_consumed_for = phase_name
            self.state.save(self.state_path)
            self.console.print(
                f"[yellow]paused[/yellow] before phase='{phase_name}' "
                f"(pause_before)"
            )
            if self.state.branch:
                self.console.print(
                    f"  правки в IDE: wf open {self.state.task_id} --workflow {self.state.workflow}"
                )
            self.console.print(
                f"  then: wf resume {self.state.task_id} --workflow {self.state.workflow}"
            )
            return PhaseResult.paused(
                reason=f"pause_before: остановка перед фазой '{phase_name}'",
            )

        assert self.events is not None
        ctx = RunContext(
            config=self.config,
            console=self.console,
            state_path=self.state_path,
            events=self.events,
        )

        self.console.rule(f"[bold]{phase_name}[/bold]  iter={self.state.iteration}")
        started = _now()
        started_mono = time.monotonic()
        self.events.write(phase_name, self.state.iteration, "phase_start")
        try:
            result = phase.run(self.state, ctx)
        except Exception as e:
            self.events.write(
                phase_name,
                self.state.iteration,
                "phase_end",
                exit="error",
                duration_ms=int((time.monotonic() - started_mono) * 1000),
                error=repr(e),
            )
            self._rerender()
            return self._fail(f"phase '{phase_name}' raised: {e!r}")
        finished = _now()
        duration_ms = int((finished - started).total_seconds() * 1000)
        self.events.write(
            phase_name,
            self.state.iteration,
            "phase_end",
            exit=result.status,
            duration_ms=duration_ms,
        )

        if result.status == "ok":
            self.state.phases_completed.append(
                PhaseRun(
                    phase=phase_name,
                    iteration=self.state.iteration,
                    started_at=started,
                    finished_at=finished,
                    exit_code=0,
                    duration_ms=duration_ms,
                    usage=result.usage,
                )
            )
            if result.usage is not None:
                _accumulate_usage(self.state, result.usage)
            next_value = self.workflow.transitions[phase_name](self.state)
            _next_phase, _terminating = self._resolve_next(next_value, phase_name)
            self.state.last_error = None
            self.state.save(self.state_path)
            self._rerender()
            self.console.print(
                f"[green]ok[/green] {phase_name} ({duration_ms} ms)"
                f"{_usage_suffix(result.usage)}"
            )
            return result

        if result.status == "paused":
            self.state.phases_completed.append(
                PhaseRun(
                    phase=phase_name,
                    iteration=self.state.iteration,
                    started_at=started,
                    finished_at=finished,
                    exit_code=0,
                    duration_ms=duration_ms,
                    usage=result.usage,
                )
            )
            if result.usage is not None:
                _accumulate_usage(self.state, result.usage)
            next_value = self.workflow.transitions[phase_name](self.state)
            next_phase, terminating = self._resolve_next(next_value, phase_name)
            self.state.last_error = None
            self.state.save(self.state_path)
            self._rerender()

            self.console.print(
                f"[yellow]paused[/yellow] phase='{phase_name}' ({duration_ms} ms)"
                f"{_usage_suffix(result.usage)}"
            )
            if result.reason:
                self.console.print(f"  reason: {result.reason}")
            if not terminating:
                self.console.print(f"  next phase: [cyan]{next_phase}[/cyan]")
                self.console.print(
                    f"  then: wf resume {self.state.task_id} --workflow {self.state.workflow}"
                )
            return result

        if result.status == "needs_human_input":
            self.state.last_error = "awaiting_qa"
            self.state.save(self.state_path)
            self._rerender()
            self.console.print(
                f"[yellow]awaiting human input[/yellow] phase='{phase_name}'\n"
                f"  questions: {result.qa_file}\n"
                f"  put answers in: {result.qa_answers_path}\n"
                f"  then: wf resume {self.state.task_id} --workflow {self.state.workflow}"
            )
            return result

        self._rerender()
        return self._fail(result.reason or "unknown failure")

    def run(self) -> int:
        with EventsWriter(self.events_path) as events:
            self.events = events
            try:
                while True:
                    if self.state.terminal:
                        self.console.print(f"[bold]done[/bold] terminal={self.state.terminal}")
                        self._rerender()
                        return 0
                    result = self.step()
                    if result.status == "ok":
                        continue
                    if result.status == "failed":
                        return 1
                    if result.status == "needs_human_input":
                        return 2
                    if result.status == "paused":
                        return 3
                    return 1
            finally:
                self.events = None

    def _resolve_next(
        self, next_value: str | RepeatAt | None, phase_name: str
    ) -> tuple[str | None, bool]:
        """Применить результат transition: обновить current_stage / iteration / terminal.

        Возвращает (next_phase_name | None, terminating). Здесь — единственное
        место, где `state.iteration` инкрементируется (для `RepeatAt`), чтобы
        transitions могли оставаться чистыми функциями.
        """
        if next_value is None:
            if self.state.terminal is None:
                self.state.terminal = "done"
            return None, True
        if isinstance(next_value, RepeatAt):
            if next_value.phase not in self.workflow.phases:
                raise ValueError(
                    f"transition for phase '{phase_name}' returned RepeatAt("
                    f"'{next_value.phase}'), но такой фазы нет в workflow "
                    f"'{self.workflow.name}'"
                )
            self.state.iteration += 1
            self.state.current_stage = next_value.phase
            self.state.pause_consumed_for = None
            return next_value.phase, False
        self.state.current_stage = next_value
        self.state.pause_consumed_for = None
        return next_value, False

    def _fail(self, reason: str) -> PhaseResult:
        self.state.last_error = reason
        self.state.terminal = "failed"
        self.state.save(self.state_path)
        self.console.print(f"[red]failed[/red] {reason}")
        return PhaseResult.failed(reason)
