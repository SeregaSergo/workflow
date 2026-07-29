from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from wf.config import Config
from wf.run_log import EventsWriter
from wf.state import UsageData

_PKG_TEMPLATES = Path(__file__).resolve().parent / "templates"
_TOOL_RESULT_PREVIEW = 500

# Дописка к пресетному системному промпту Claude Code (дефолтный system_prompt
# всех LLM-фаз). Пресет написан для интерактивной сессии, поэтому здесь гасим
# ожидание живого пользователя и фиксируем контракт вывода. Роли фаз описывают
# шаблоны; пофазные дополнения — через run_phase(system_prompt_append=...).
_SYSTEM_APPEND_BASE = """\
# wf pipeline

You are running unattended as one phase of an automated coding pipeline (wf).
There is no interactive user: nobody reads your messages mid-run, nobody can
answer a question or confirm anything, so never stop to ask or wait.

Your final reply is parsed by a program. The XML-tagged blocks described in
the task prompt are the only output contract: emit them exactly as specified
and keep any prose outside the blocks to a minimum."""


def _default_system_prompt(append_extra: str | None) -> dict[str, Any]:
    append = _SYSTEM_APPEND_BASE
    if append_extra:
        append = f"{append}\n\n{append_extra}"
    return {
        "type": "preset",
        "preset": "claude_code",
        "append": append,
        # Статичный системный промпт: динамические секции (cwd, git status,
        # auto-memory) CLI переносит в первое user-сообщение, префикс
        # промпт-кэша совпадает между фазами и задачами. Старые CLI молча
        # игнорируют флаг.
        "exclude_dynamic_sections": True,
    }


class ClaudeRunError(RuntimeError):
    """Поднимается, когда claude-agent-sdk завершился с is_error=True."""


def _format_result_error(info: dict[str, Any]) -> str:
    subtype = info.get("subtype") or "unknown"
    api_status = info.get("api_error_status")
    errors = info.get("errors") or []

    parts: list[str] = []
    if api_status == 429:
        parts.append("превышен лимит запросов Claude (HTTP 429)")
    elif api_status == 529:
        parts.append("сервис Claude перегружен (HTTP 529)")
    elif api_status is not None:
        parts.append(f"ошибка API Claude (HTTP {api_status})")
    elif subtype == "error_max_turns":
        parts.append("превышено максимальное число шагов SDK")
    elif subtype == "error_during_execution":
        parts.append("ошибка во время выполнения SDK")
    elif subtype and subtype != "success":
        parts.append(f"SDK вернул ошибочный результат: subtype={subtype}")
    else:
        parts.append("SDK вернул ошибочный результат")

    if errors:
        parts.append("; ".join(str(x) for x in errors))
    num_turns = info.get("num_turns")
    if num_turns is not None:
        parts.append(f"шагов: {num_turns}")
    return "; ".join(parts)


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_PKG_TEMPLATES)),
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


def render_template(name: str, ctx: dict[str, Any]) -> str:
    return _env().get_template(name).render(**ctx)


@dataclass
class RunSummary:
    text: str
    usage: UsageData | None = None


def _extract_usage(msg: Any, model: str | None) -> UsageData | None:
    raw = getattr(msg, "usage", None)
    cost = getattr(msg, "total_cost_usd", None)
    if raw is None and cost is None:
        return None
    if isinstance(raw, dict):
        get = raw.get
    else:
        get = lambda k, default=0: getattr(raw, k, default) if raw is not None else default
    return UsageData(
        input_tokens=int(get("input_tokens", 0) or 0),
        output_tokens=int(get("output_tokens", 0) or 0),
        cache_creation_input_tokens=int(get("cache_creation_input_tokens", 0) or 0),
        cache_read_input_tokens=int(get("cache_read_input_tokens", 0) or 0),
        total_cost_usd=float(cost or 0.0),
        model=model,
    )


def _usage_to_dict(u: UsageData | None) -> dict[str, int] | None:
    if u is None:
        return None
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": u.cache_creation_input_tokens,
        "cache_read_input_tokens": u.cache_read_input_tokens,
    }


def _normalize_tool_result_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


async def _run_query(
    prompt: str,
    options: Any,
    events: EventsWriter,
    phase: str,
    iter: int,
    model: str | None,
) -> tuple[str, UsageData | None]:
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        SystemMessage,
        TextBlock,
        UserMessage,
        query,
    )

    # ToolUseBlock / ToolResultBlock / ThinkingBlock may not be exported by
    # every SDK version — fall back to attribute-based detection.
    try:
        from claude_agent_sdk import ToolUseBlock  # type: ignore
    except ImportError:
        ToolUseBlock = None  # type: ignore[assignment]
    try:
        from claude_agent_sdk import ToolResultBlock  # type: ignore
    except ImportError:
        ToolResultBlock = None  # type: ignore[assignment]
    try:
        from claude_agent_sdk import ThinkingBlock  # type: ignore
    except ImportError:
        ThinkingBlock = None  # type: ignore[assignment]

    collected: list[str] = []
    usage: UsageData | None = None
    last_result_error: dict[str, Any] | None = None

    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock) and block.text:
                        events.write(phase, iter, "assistant_text", text=block.text)
                        collected.append(block.text)
                    elif ToolUseBlock is not None and isinstance(block, ToolUseBlock):
                        events.write(
                            phase,
                            iter,
                            "tool_use",
                            id=getattr(block, "id", None),
                            name=getattr(block, "name", "?"),
                            input=getattr(block, "input", {}),
                        )
                    elif ThinkingBlock is not None and isinstance(block, ThinkingBlock):
                        thinking_text = getattr(block, "thinking", "") or ""
                        events.write(phase, iter, "thinking", len=len(thinking_text))
            elif isinstance(msg, UserMessage):
                content = getattr(msg, "content", None)
                if isinstance(content, list):
                    for block in content:
                        if ToolResultBlock is not None and isinstance(block, ToolResultBlock):
                            raw = getattr(block, "content", None)
                            normalized = _normalize_tool_result_content(raw)
                            preview = normalized[:_TOOL_RESULT_PREVIEW]
                            events.write(
                                phase,
                                iter,
                                "tool_result",
                                id=getattr(block, "tool_use_id", None),
                                is_error=bool(getattr(block, "is_error", False)),
                                content_preview=preview,
                            )
            elif isinstance(msg, SystemMessage):
                events.write(phase, iter, "system", subtype=getattr(msg, "subtype", "?"))
            elif isinstance(msg, ResultMessage):
                captured = _extract_usage(msg, model)
                if captured is not None:
                    usage = captured
                is_error = bool(getattr(msg, "is_error", False))
                subtype = getattr(msg, "subtype", None)
                api_error_status = getattr(msg, "api_error_status", None)
                events.write(
                    phase,
                    iter,
                    "result",
                    is_error=is_error,
                    subtype=subtype,
                    api_error_status=api_error_status,
                    duration_ms=getattr(msg, "duration_ms", None),
                    num_turns=getattr(msg, "num_turns", None),
                    stop_reason=getattr(msg, "stop_reason", None),
                    usage=_usage_to_dict(captured),
                    cost_usd=getattr(msg, "total_cost_usd", None),
                )
                if is_error:
                    last_result_error = {
                        "subtype": subtype,
                        "api_error_status": api_error_status,
                        "errors": getattr(msg, "errors", None),
                        "num_turns": getattr(msg, "num_turns", None),
                    }
    except ClaudeRunError:
        raise
    except Exception as e:
        if last_result_error is not None:
            raise ClaudeRunError(_format_result_error(last_result_error)) from e
        raise

    if last_result_error is not None:
        raise ClaudeRunError(_format_result_error(last_result_error))
    return "\n".join(collected), usage


def run_phase(
    *,
    template_name: str,
    template_ctx: dict[str, Any],
    cwd: Path,
    config: Config,
    events: EventsWriter,
    phase: str,
    iter: int,
    tools: list[str] | None = None,
    system_prompt: str | dict[str, Any] | None = None,
    system_prompt_append: str | None = None,
    model: str | None = None,
    setting_sources: list[str] | None = None,
    hooks: dict[str, Any] | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> RunSummary:
    from claude_agent_sdk import ClaudeAgentOptions

    prompt = render_template(template_name, template_ctx)

    claude_cfg = config.claude
    mdl = model or claude_cfg.phase_models.get(phase) or claude_cfg.model

    # None → пресет Claude Code с базовой допиской (+ system_prompt_append);
    # "" → полностью пустой промпт (тривиальные фазы); str/dict — как есть.
    if system_prompt is None:
        system_prompt = _default_system_prompt(system_prompt_append)

    opts_kwargs: dict[str, Any] = dict(
        cwd=str(cwd),
        permission_mode="bypassPermissions",
        system_prompt=system_prompt,
        model=mdl,
    )
    # tools — позитивный whitelist встроенных тулов: что вообще доступно фазе.
    # MCP-тулы им не режутся — они приходят через mcp_servers отдельно.
    # None → дефолтный набор Claude Code.
    if tools is not None:
        opts_kwargs["tools"] = tools
    if setting_sources is not None:
        opts_kwargs["setting_sources"] = setting_sources
    if hooks is not None:
        opts_kwargs["hooks"] = hooks
    if mcp_servers:
        opts_kwargs["mcp_servers"] = mcp_servers
    options = ClaudeAgentOptions(**opts_kwargs)

    if isinstance(system_prompt, dict):
        sp_mode = f"preset:{system_prompt.get('preset', '?')}"
    elif system_prompt == "":
        sp_mode = "empty"
    else:
        sp_mode = "custom"

    events.write(
        phase,
        iter,
        "llm_start",
        template=template_name,
        model=mdl,
        permission_mode="bypassPermissions",
        cwd=str(cwd),
        system_prompt=sp_mode,
        mcp_servers=sorted(mcp_servers) if mcp_servers else [],
    )

    text, usage = asyncio.run(_run_query(prompt, options, events, phase, iter, mdl))
    return RunSummary(text=text, usage=usage)
