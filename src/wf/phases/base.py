from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from rich.console import Console

from wf.config import Config
from wf.state import State, UsageData

if TYPE_CHECKING:
    from wf.run_log import EventsWriter

PhaseStatus = Literal["ok", "needs_human_input", "failed", "paused"]


@dataclass
class RunContext:
    config: Config
    console: Console
    state_path: Path
    events: "EventsWriter"
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class PhaseResult:
    status: PhaseStatus
    qa_file: Path | None = None
    qa_answers_path: Path | None = None
    reason: str | None = None
    usage: UsageData | None = None

    @classmethod
    def ok(cls, usage: UsageData | None = None) -> "PhaseResult":
        return cls(status="ok", usage=usage)

    @classmethod
    def needs_human_input(cls, qa_file: Path, qa_answers_path: Path) -> "PhaseResult":
        return cls(status="needs_human_input", qa_file=qa_file, qa_answers_path=qa_answers_path)

    @classmethod
    def failed(cls, reason: str) -> "PhaseResult":
        return cls(status="failed", reason=reason)

    @classmethod
    def paused(
        cls,
        reason: str | None = None,
        usage: UsageData | None = None,
    ) -> "PhaseResult":
        return cls(status="paused", reason=reason, usage=usage)


@runtime_checkable
class Phase(Protocol):
    name: str

    def required_artifacts(self, config: Config, state: State) -> list[Path]: ...
    def produced_artifacts(self, config: Config, state: State) -> list[Path]: ...
    def required_tools(self) -> list[str]: ...
    def is_ready(self, config: Config, state: State) -> bool: ...
    def run(self, state: State, ctx: RunContext) -> PhaseResult: ...
