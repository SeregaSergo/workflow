# 03. Синхронизировать документацию с реальным API

**Приоритет:** P0

## Описание

Документация в `docs/` рассинхронизирована с кодом:

- [docs/plan.md](../docs/plan.md) описывает команды `wf init`, `wf next`,
  `wf run`, `wf reset` — в [cli.py](../src/wf/cli.py) реально есть
  `start`/`resume`/`status`/`remove`/`logs`.
- Там же worktree описан «рядом с репо» (`../<branch_name>`) — фактически
  он создаётся в `<task_dir>/worktree/<branch>` внутри `$WF_HOME`
  ([prepare.py](../src/wf/phases/prepare.py), [git.py](../src/wf/git.py)).
- [docs/State.md](../docs/State.md) показывает JSON с ~11 полями, в
  [state.py](../src/wf/state.py) — ~24 поля (плюс вложенные `UsageData`,
  `PhaseRun`).
- [docs/phases.md](../docs/phases.md) упоминает `task_qa_1.md` для фазы
  `explore` — в [explore.py](../src/wf/phases/explore.py) QA-цикл не
  реализован (явно отмечено в [CLAUDE.md](../CLAUDE.md)).

## Что сделать

- Переписать [docs/plan.md](../docs/plan.md):
  - актуальные команды: `start`, `resume`, `status`, `remove`, `logs`;
  - exit codes: 0 (done), 1 (failed), 2 (awaiting QA), 3 (paused);
  - реальный путь worktree (`<task_dir>/worktree/<branch>` в `$WF_HOME`);
  - реальные фазы пайплайна включая `mr` и `notify`;
  - актуальный состав `Стек` (claude-agent-sdk, не `claude -p`).
- Переписать [docs/State.md](../docs/State.md):
  - полная схема `State` из [state.py](../src/wf/state.py): все 24 поля
    с типами и значениями по умолчанию;
  - схемы `UsageData` и `PhaseRun`;
  - семантика `terminal`, `last_review_has_feedback`, `iteration`/
    `max_iterations`.
- Поправить [docs/phases.md](../docs/phases.md):
  - убрать упоминание `task_qa_1.md` из фазы explore (либо явно пометить
    «не реализовано, см. issue»);
  - привести в соответствие с маркер-протоколом (фазы возвращают блоки в
    `summary.text`, не пишут файлы из промпта).
- Удалить или переименовать [docs/docs-mellow-penguin.md](../docs/docs-mellow-penguin.md),
  если это случайный артефакт (предварительно посмотреть содержимое).

## Критерии приёмки

- Любой пример или команда из доки воспроизводится дословно.
- Имена полей `State` и команды CLI в доке совпадают с кодом 1-в-1.
- В `CLAUDE.md` нет противоречий с `docs/plan.md`.
