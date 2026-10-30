from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from wf.phases.base import Phase
from wf.state import State


@dataclass(frozen=True)
class RepeatAt:
    """Повторить фазу `phase` со следующей итерации.

    Transition возвращает этот объект, когда нужно зациклиться на предыдущую
    фазу (например, `review → coding` при наличии feedback). Движок сам
    инкрементирует `state.iteration` и выставляет `current_stage = phase`,
    чтобы transitions оставались чистыми функциями.
    """

    phase: str


Transition = Callable[[State], "str | RepeatAt | None"]


def terminal(_: State) -> str | None:
    """Sentinel transition meaning 'no next phase, workflow finished'."""
    return None


@dataclass
class Workflow:
    name: str
    phases: dict[str, type[Phase]]
    start: str
    transitions: dict[str, Transition]

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.start not in self.phases:
            problems.append(f"start phase '{self.start}' is not in phases")
        for n in self.phases:
            if n not in self.transitions:
                problems.append(f"phase '{n}' has no transition entry")
        for n in self.transitions:
            if n not in self.phases:
                problems.append(f"transition for unknown phase '{n}'")
        # TODO: transitions могут возвращать RepeatAt(phase); проверить, что
        # phase ∈ self.phases, статически нельзя — это требует вызова
        # transition с реальным State. Проверка выполняется в рантайме
        # движком при обработке результата.
        return problems

    def required_tools(self) -> set[str]:
        """Объединение `required_tools()` всех фаз воркфлоу."""
        result: set[str] = set()
        for phase_cls in self.phases.values():
            result.update(phase_cls().required_tools())
        return result
