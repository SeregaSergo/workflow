from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TerminalState = Literal["done", "max_iter_reached", "failed"]


class UsageData(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    total_cost_usd: float = 0.0
    model: str | None = None


class PhaseRun(BaseModel):
    # Tolerate legacy `log_path` field present in old state.json files.
    model_config = ConfigDict(extra="ignore")

    phase: str
    iteration: int
    started_at: datetime
    finished_at: datetime
    exit_code: int
    duration_ms: int
    usage: UsageData | None = None


class State(BaseModel):
    task_id: str
    workflow: str
    repo_path: Path
    worktree: Path | None = None
    task_url: str | None = None
    task_title: str | None = None
    branch: str | None = None
    base_branch: str
    current_stage: str
    iteration: int = 1
    max_iterations: int = 3
    phases_completed: list[PhaseRun] = Field(default_factory=list)
    last_review_has_feedback: bool = False
    pause_consumed_for: str | None = None
    mr_url: str | None = None
    terminal: TerminalState | None = None
    last_error: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    created_at: datetime
    updated_at: datetime

    @classmethod
    def load(cls, path: Path) -> State:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        self.updated_at = datetime.now(timezone.utc)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump_json(indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
