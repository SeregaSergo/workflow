from __future__ import annotations

from pathlib import Path

from wf.config import Config
from wf.state import State


def _dir(config: Config, state: State) -> Path:
    return config.artifacts_dir(state.workflow, state.task_id)


def _task_dir(config: Config, state: State) -> Path:
    return config.task_dir(state.workflow, state.task_id)


def task(config: Config, state: State) -> Path:
    return _dir(config, state) / "task.md"


def task_qa(config: Config, state: State) -> Path:
    return _dir(config, state) / "task_qa.md"


def task_qa_answers(config: Config, state: State) -> Path:
    return _dir(config, state) / "task_qa_answers.md"


def plan(config: Config, state: State) -> Path:
    return _dir(config, state) / f"plan_{state.iteration}.md"


def plan_qa(config: Config, state: State) -> Path:
    return _dir(config, state) / f"plan_qa_{state.iteration}.md"


def plan_qa_answers(config: Config, state: State) -> Path:
    return _dir(config, state) / f"plan_qa_answers_{state.iteration}.md"


def review(config: Config, state: State) -> Path:
    return _dir(config, state) / f"review_{state.iteration}.md"


def feedback(config: Config, state: State) -> Path:
    return _dir(config, state) / f"feedback_{state.iteration}.md"


def previous_feedback(config: Config, state: State) -> Path | None:
    if state.iteration <= 1:
        return None
    p = _dir(config, state) / f"feedback_{state.iteration - 1}.md"
    return p if p.exists() else None


def fix_brief(config: Config, state: State) -> Path:
    return _dir(config, state) / f"fix_brief_{state.fix_rounds}.md"


def mr_comments(config: Config, state: State) -> Path:
    return _dir(config, state) / f"mr_comments_{state.fix_rounds}.md"


def fix_settings_backup(config: Config, state: State) -> Path:
    return _task_dir(config, state) / "settings.local.json.bak"


def qa_notes(config: Config, state: State) -> Path:
    return _dir(config, state) / "qa_notes.md"


def events_path(config: Config, state: State) -> Path:
    return _task_dir(config, state) / "events.jsonl"


def summary_path(config: Config, state: State) -> Path:
    return _task_dir(config, state) / "summary.md"
