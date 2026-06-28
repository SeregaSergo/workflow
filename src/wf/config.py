from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from rich.console import Console


class Defaults(BaseModel):
    max_iterations: int = 3


class ToolSpec(BaseModel):
    cmd: str


class WorkflowSpec(BaseModel):
    notify_channel_id: str | None = None
    reviewers: list[str] = Field(default_factory=list)
    pause_before: list[str] = Field(default_factory=list)


class ClaudeSettings(BaseModel):
    permission_mode: str = "bypassPermissions"
    model: str | None = None
    phase_models: dict[str, str] = Field(default_factory=dict)


class Config(BaseModel):
    version: int = 1
    data_dir: Path = Path("~/wf")
    defaults: Defaults = Field(default_factory=Defaults)
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)

    @property
    def data_dir_path(self) -> Path:
        return Path(os.path.expanduser(str(self.data_dir))).resolve()

    def workflow_dir(self, workflow: str) -> Path:
        return self.data_dir_path / "workflow" / workflow

    def task_dir(self, workflow: str, task_id: str) -> Path:
        return self.workflow_dir(workflow) / task_id

    def state_path(self, workflow: str, task_id: str) -> Path:
        return self.task_dir(workflow, task_id) / "state.json"

    def artifacts_dir(self, workflow: str, task_id: str) -> Path:
        return self.task_dir(workflow, task_id) / "artifacts"


def _package_default_config() -> Path:
    return Path(__file__).resolve().parent / "default-config.yaml"


def config_path() -> Path:
    override = os.environ.get("WF_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path("~/.wf/config.yaml").expanduser()


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        default = _package_default_config()
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(default, path)
        Console(stderr=True).print(
            f"[yellow]>> создан {path}[/yellow]\n"
            f"[dim]   данные воркфлоу будут лежать в {Config().data_dir_path} "
            f"(поправь поле data_dir: при желании)[/dim]"
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
