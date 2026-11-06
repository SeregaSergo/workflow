from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TerminalState = Literal["done", "max_iter_reached", "failed"]
FixMode = Literal["review", "branch"]


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


class FixRound(BaseModel):
    """Открытый раунд ручных правок (`wf open`).

    В режиме `review` HEAD worktree-а отсоединён и стоит на `base`, а ветка
    по-прежнему указывает на `head` — так вся работа на ветке видна в IDE как
    staged, а правки раунда — как unstaged. В режиме `branch` (`wf open
    --branch`, `wf rebase`) HEAD остаётся на ветке, чтобы с её историей можно
    было работать обычным git; `head`/`base` — на момент входа в режим.
    """

    number: int
    mode: FixMode = "review"
    head: str
    base: str
    opened_at: datetime
    # Что случилось за раунд (rebase и т.п.) — уходит в events.jsonl при закрытии.
    notes: list[str] = Field(default_factory=list)


class State(BaseModel):
    task_id: str
    workflow: str
    repo_path: Path
    worktree: Path | None = None
    task_url: str | None = None
    task_title: str | None = None
    branch: str | None = None
    base_branch: str
    # Точка ответвления ветки от base_branch. Нужна, когда ветку уже смержили
    # и merge-base с base_branch совпадает с HEAD.
    base_commit: str | None = None
    current_stage: str
    iteration: int = 1
    max_iterations: int = 3
    phases_completed: list[PhaseRun] = Field(default_factory=list)
    last_review_has_feedback: bool = False
    pause_consumed_for: str | None = None
    mr_url: str | None = None
    fix_round: FixRound | None = None
    fix_rounds: int = 0
    # sha origin/<branch>, поверх которого wf разрешено переписать историю ветки
    # (push --force-with-lease): пишется при входе в режим ветки, если локальная
    # ветка содержит всё с remote, и сбрасывается, когда раунд закрыт.
    push_lease: str | None = None
    qa_notes_posted: bool = False
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
