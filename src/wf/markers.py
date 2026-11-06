from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class BlockSpec:
    name: str
    required: bool = True
    allow_empty: bool = False


class BlocksError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def commit_subject(raw: str | None, limit: int = 72) -> str | None:
    """Первая непустая строка ответа модели как subject коммита (без кавычек и точки)."""
    if not raw:
        return None
    for line in raw.splitlines():
        line = line.strip().strip('"').strip("'").rstrip(".").strip()
        if line:
            return line[:limit]
    return None


def parse_blocks(text: str, schema: Sequence[BlockSpec]) -> dict[str, str]:
    result: dict[str, str] = {}
    for spec in schema:
        pattern = rf"<{re.escape(spec.name)}>(.*?)</{re.escape(spec.name)}>"
        matches = re.findall(pattern, text, re.DOTALL)
        if len(matches) > 1:
            raise BlocksError(f"duplicate block '{spec.name}'")
        if not matches:
            if spec.required:
                raise BlocksError(f"missing required block '{spec.name}'")
            continue
        content = matches[0].strip()
        if not content and not spec.allow_empty:
            raise BlocksError(f"empty block '{spec.name}'")
        result[spec.name] = content
    return result
