from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, TextIO

from wf.state import State


_XML_BLOCK_RE = re.compile(r"<(\w+)>([\s\S]*?)</\1>")
_TOOL_KEY_ARG: dict[str, tuple[str, ...]] = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "Glob": ("pattern",),
}
_TRUNCATE = 80
_CONTENT_PREVIEW = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventsWriter:
    """Append-only JSON Lines writer for phase events.

    One event per line. Each `write()` call flushes immediately so the file
    stays valid up to the last completed event even on a crash.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fp: TextIO = path.open("a", encoding="utf-8")

    def write(self, phase: str, iter: int, type: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": _now_iso(),
            "phase": phase,
            "iter": iter,
            "type": type,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False)
        self._fp.write(line)
        self._fp.write("\n")
        self._fp.flush()

    def close(self) -> None:
        if not self._fp.closed:
            self._fp.close()

    def __enter__(self) -> "EventsWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ---- aggregation & rendering -------------------------------------------------


@dataclass
class _ToolCall:
    num: int
    name: str
    input: dict[str, Any] | Any
    is_error: bool = False


@dataclass
class _PhaseSection:
    phase: str
    iter: int
    occurrence: int  # 1, 2, ... when same (phase, iter) appears multiple times
    started_at: str | None = None
    ended_at: str | None = None
    exit: str = "incomplete"
    duration_ms: int | None = None
    result_duration_ms: int | None = None
    num_turns: int | None = None
    cost_usd: float | None = None
    usage: dict[str, int] = field(default_factory=dict)
    assistant_text: list[str] = field(default_factory=list)
    tool_calls: list[_ToolCall] = field(default_factory=list)
    external_tools: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    has_phase_end: bool = False


@dataclass
class _Totals:
    first_ts: str | None = None
    last_ts: str | None = None
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_create: int = 0
    cost_usd: float = 0.0
    elapsed_ms: int = 0


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fp:
        for lineno, raw in enumerate(fp, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                # Skip malformed lines silently — file may have been truncated
                # mid-write on crash. Could log a warning but we don't have
                # a logger here.
                continue


def _aggregate(events: Iterable[dict[str, Any]]) -> tuple[list[_PhaseSection], _Totals]:
    sections: list[_PhaseSection] = []
    totals = _Totals()
    # (phase, iter) -> occurrence count
    occurrences: dict[tuple[str, int], int] = {}
    # Current "open" section per (phase, iter, occurrence) — drives where new
    # events get appended. We track by (phase, iter) since events have those fields.
    current: _PhaseSection | None = None
    # Map tool_use_id -> tool name for matching tool_result.is_error.
    tool_by_id: dict[str, str] = {}
    tool_counter = 0

    for ev in events:
        ts = ev.get("ts")
        if totals.first_ts is None:
            totals.first_ts = ts
        totals.last_ts = ts

        ev_type = ev.get("type")
        phase = ev.get("phase", "?")
        it = int(ev.get("iter", 0))

        if ev_type == "phase_start":
            occurrences[(phase, it)] = occurrences.get((phase, it), 0) + 1
            current = _PhaseSection(
                phase=phase,
                iter=it,
                occurrence=occurrences[(phase, it)],
                started_at=ts,
            )
            sections.append(current)
            tool_counter = 0
            tool_by_id = {}
            continue

        # If we somehow see events before any phase_start (shouldn't happen),
        # synthesize a section.
        if current is None or (current.phase, current.iter) != (phase, it):
            occurrences[(phase, it)] = occurrences.get((phase, it), 0) + 1
            current = _PhaseSection(
                phase=phase,
                iter=it,
                occurrence=occurrences[(phase, it)],
                started_at=ts,
            )
            sections.append(current)
            tool_counter = 0
            tool_by_id = {}

        if ev_type == "assistant_text":
            text = ev.get("text", "")
            if text:
                current.assistant_text.append(text)
        elif ev_type == "tool_use":
            tool_counter += 1
            tu_id = ev.get("id") or f"_anon_{tool_counter}"
            tool_by_id[tu_id] = ev.get("name", "?")
            current.tool_calls.append(
                _ToolCall(
                    num=tool_counter,
                    name=ev.get("name", "?"),
                    input=ev.get("input", {}),
                )
            )
        elif ev_type == "tool_result":
            if ev.get("is_error"):
                tu_id = ev.get("id")
                # Find matching tool_call in current section and mark it
                for tc in current.tool_calls:
                    if tu_id and tool_by_id.get(tu_id) == tc.name:
                        # crude: mark last matching name — good enough for stats
                        tc.is_error = True
                        break
        elif ev_type == "thinking":
            pass  # length only, not rendered
        elif ev_type == "system":
            pass
        elif ev_type == "result":
            current.num_turns = ev.get("num_turns")
            current.cost_usd = ev.get("cost_usd")
            current.result_duration_ms = ev.get("duration_ms")
            usage = ev.get("usage") or {}
            if usage:
                current.usage = {
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                    "cache_read_input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
                    "cache_creation_input_tokens": int(
                        usage.get("cache_creation_input_tokens", 0) or 0
                    ),
                }
                totals.input_tokens += current.usage["input_tokens"]
                totals.output_tokens += current.usage["output_tokens"]
                totals.cache_read += current.usage["cache_read_input_tokens"]
                totals.cache_create += current.usage["cache_creation_input_tokens"]
            if current.cost_usd is not None:
                totals.cost_usd += float(current.cost_usd)
            if current.num_turns is not None:
                totals.num_turns += int(current.num_turns)
        elif ev_type == "phase_end":
            current.ended_at = ts
            current.exit = ev.get("exit", "ok")
            current.duration_ms = ev.get("duration_ms")
            current.has_phase_end = True
            if current.duration_ms is not None:
                totals.elapsed_ms += int(current.duration_ms)
        elif ev_type == "external_tool":
            current.external_tools.append(
                {
                    "name": ev.get("name", "?"),
                    "cmd": ev.get("cmd", ""),
                    "exit_code": ev.get("exit_code"),
                    "duration_ms": ev.get("duration_ms"),
                }
            )
        elif ev_type == "phase_note":
            text = ev.get("text", "")
            if text:
                current.notes.append(text)

    return sections, totals


def _truncate(s: str, limit: int = _TRUNCATE) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _format_tool_call(name: str, raw_input: Any) -> str:
    if not isinstance(raw_input, dict):
        return _truncate(json.dumps(raw_input, ensure_ascii=False))
    if not raw_input:
        return ""
    if name == "Grep":
        pattern = raw_input.get("pattern", "")
        path = raw_input.get("path")
        if path:
            return _truncate(f'"{pattern}" in {path}')
        return _truncate(str(pattern))
    keys = _TOOL_KEY_ARG.get(name)
    if keys:
        for k in keys:
            v = raw_input.get(k)
            if v is not None:
                return _truncate(str(v))
        return ""
    return _truncate(json.dumps(raw_input, ensure_ascii=False))


def _extract_xml_blocks(text: str) -> list[tuple[str, str]]:
    """Find top-level <tag>...</tag> blocks in order of appearance.

    Uses the same regex shape as wf.markers but without schema validation —
    summary should show whatever the model produced, even broken blocks.
    """
    return [(m.group(1), m.group(2).strip()) for m in _XML_BLOCK_RE.finditer(text)]


def _fmt_duration_ms(ms: int | None) -> str:
    if ms is None:
        return "?"
    return f"{ms / 1000:.1f}s"


def _fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "?"
    return f"${cost:.4f}"


def _fmt_turns(n: int | None) -> str:
    return "?" if n is None else str(n)


def _section_heading(s: _PhaseSection) -> str:
    suffix = ""
    if s.occurrence == 2:
        suffix = " (resumed)"
    elif s.occurrence > 2:
        suffix = f" (resumed {s.occurrence - 1})"
    exit_label = s.exit if s.has_phase_end else "incomplete"
    dur = _fmt_duration_ms(s.duration_ms if s.has_phase_end else s.result_duration_ms)
    return (
        f"## {s.phase} #{s.iter}{suffix} — {exit_label}  "
        f"({dur}, {_fmt_turns(s.num_turns)} turns, {_fmt_cost(s.cost_usd)})"
    )


def _render_phase_section(s: _PhaseSection) -> str:
    parts: list[str] = [_section_heading(s)]

    joined_text = "\n".join(s.assistant_text)
    blocks = _extract_xml_blocks(joined_text)
    if blocks:
        parts.append("### Output blocks")
        rendered_blocks: list[str] = []
        for tag, content in blocks:
            rendered_blocks.append(f"<{tag}>\n{content}\n</{tag}>")
        parts.append("\n\n".join(rendered_blocks))
    elif s.assistant_text:
        # LLM phase but no XML blocks — useful to know the response existed.
        parts.append("### Output blocks")
        parts.append("_(no XML blocks found)_")

    if s.tool_calls:
        parts.append(f"### Tool calls ({len(s.tool_calls)})")
        lines: list[str] = []
        for tc in s.tool_calls:
            arg = _format_tool_call(tc.name, tc.input)
            err = " [error]" if tc.is_error else ""
            lines.append(f"{tc.num}.  {tc.name}  {arg}{err}")
        parts.append("```\n" + "\n".join(lines) + "\n```")

    if s.external_tools:
        parts.append(f"### External tools ({len(s.external_tools)})")
        lines = []
        for n, et in enumerate(s.external_tools, 1):
            dur = _fmt_duration_ms(et.get("duration_ms"))
            ec = et.get("exit_code")
            lines.append(
                f"{n}.  {et.get('name', '?')}  exit={ec} dur={dur}  {_truncate(et.get('cmd', ''))}"
            )
        parts.append("```\n" + "\n".join(lines) + "\n```")

    if s.notes:
        parts.append("### Notes")
        parts.append("\n".join(f"- {n}" for n in s.notes))

    # Stats
    stat_lines: list[str] = ["### Stats"]
    if s.tool_calls:
        counts: dict[str, int] = {}
        for tc in s.tool_calls:
            counts[tc.name] = counts.get(tc.name, 0) + 1
        stat_lines.append("| Tool | Count |")
        stat_lines.append("|------|-------|")
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            stat_lines.append(f"| {name} | {count} |")
        stat_lines.append("")
    if s.usage:
        stat_lines.append(
            f"tokens: in={s.usage.get('input_tokens', 0)} "
            f"out={s.usage.get('output_tokens', 0)} "
            f"cache_read={s.usage.get('cache_read_input_tokens', 0)} "
            f"cache_create={s.usage.get('cache_creation_input_tokens', 0)}"
        )
    parts.append("\n".join(stat_lines))

    return "\n\n".join(parts)


def _render_totals(state: State, totals: _Totals) -> str:
    lines: list[str] = []
    title_task = state.task_title or state.task_id
    lines.append(f"# {state.task_id} — {title_task}")
    lines.append(
        f"branch: {state.branch or '-'}   "
        f"workflow: {state.workflow}   "
        f"base: {state.base_branch}"
    )
    lines.append(
        f"current_stage: {state.current_stage}   terminal: {state.terminal or '-'}"
    )
    elapsed_s = totals.elapsed_ms / 1000.0 if totals.elapsed_ms else 0.0
    lines.append(
        f"total: turns={totals.num_turns}  "
        f"tokens(in/out)={totals.input_tokens}/{totals.output_tokens}  "
        f"cache_read={totals.cache_read}  "
        f"cost=${totals.cost_usd:.4f}  "
        f"elapsed={elapsed_s:.1f}s"
    )
    return "\n".join(lines)


def rerender_summary(events_path: Path, summary_path: Path, state: State) -> None:
    """Read all events, aggregate, write `summary.md` atomically.

    No fsync — performance matters more than durability for a derived file.
    """
    sections, totals = _aggregate(_iter_events(events_path))
    header = _render_totals(state, totals)
    body_parts = [_render_phase_section(s) for s in sections]
    content = header + "\n\n---\n\n" + "\n\n---\n\n".join(body_parts) + "\n"

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".summary-", suffix=".md", dir=summary_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, summary_path)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
