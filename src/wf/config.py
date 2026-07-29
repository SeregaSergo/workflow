from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from rich.console import Console


class Defaults(BaseModel):
    max_iterations: int = 3


class ToolSpec(BaseModel):
    cmd: str


class McpServerSpec(BaseModel):
    """Описание MCP-сервера для LLM-фаз. Форма повторяет конфиг
    claude-agent-sdk / блок `mcpServers` из ~/.claude.json, чтобы запись
    можно было скопировать почти как есть.

    stdio-сервер: type/command/args/env; http|sse-сервер: type/url/headers.
    """

    type: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    def to_sdk(self) -> dict[str, Any]:
        kind = self.type or ("http" if self.url else "stdio")
        if kind in ("http", "sse"):
            cfg: dict[str, Any] = {"type": kind, "url": self.url}
            if self.headers:
                cfg["headers"] = dict(self.headers)
            return cfg
        cfg = {"type": "stdio", "command": self.command}
        if self.args:
            cfg["args"] = list(self.args)
        if self.env:
            cfg["env"] = dict(self.env)
        return cfg


class WorkflowSpec(BaseModel):
    notify_channel_id: str | None = None
    reviewers: list[str] = Field(default_factory=list)
    pause_before: list[str] = Field(default_factory=list)


class ClaudeSettings(BaseModel):
    model: str | None = None
    phase_models: dict[str, str] = Field(default_factory=dict)
    # Какие MCP-серверы (по именам из верхнеуровневого mcp_servers:) доступны
    # каждой LLM-фазе. Ключ — имя фазы, значение — список имён серверов.
    phase_mcp_servers: dict[str, list[str]] = Field(default_factory=dict)


class Config(BaseModel):
    version: int = 1
    data_dir: Path = Path("~/wf")
    defaults: Defaults = Field(default_factory=Defaults)
    tools: dict[str, ToolSpec] = Field(default_factory=dict)
    mcp_servers: dict[str, McpServerSpec] = Field(default_factory=dict)
    claude: ClaudeSettings = Field(default_factory=ClaudeSettings)
    workflows: dict[str, WorkflowSpec] = Field(default_factory=dict)

    @property
    def data_dir_path(self) -> Path:
        return Path(os.path.expanduser(str(self.data_dir))).resolve()

    def mcp_for_phase(
        self, phase: str
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """MCP-конфиг для фазы: (серверы для SDK, имена серверов, которых нет
        в mcp_servers:).

        Под bypassPermissions MCP-инструменты доступны фазе автоматически, как
        только сервер передан в SDK, — отдельный allow-список не нужен.

        Серверы, включённые для фазы через claude.phase_mcp_servers, но не
        описанные в верхнеуровневом mcp_servers:, возвращаются во втором
        списке — фаза сама решает, как об этом сообщить.
        """
        servers: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for name in self.claude.phase_mcp_servers.get(phase, []):
            spec = self.mcp_servers.get(name)
            if spec is None:
                missing.append(name)
                continue
            servers[name] = spec.to_sdk()
        return servers, missing

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
