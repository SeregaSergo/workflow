# Финальная фаза `cleanup_worktree` в coding-воркфлоу

**Дата:** 2026-05-20

## Проблема

После завершения coding-воркфлоу (`… → mr → notify → done`) на диске остаётся git
worktree в `<task_dir>/worktree/<branch>/`. Дальше он не нужен: MR создан, уведомление
отправлено, локальная ветка существует в основном репозитории. Единственный способ
почистить worktree сейчас — `wf remove`, но он удаляет вообще всю задачу (state.json,
events.jsonl, summary.md, artifacts/). Это не то, что нужно после успешного завершения:
хочется сохранить артефакты для post-mortem и `wf status`, но снять с диска несколько
гигабайт исходников.

## Решение

Добавляем новую фазу `cleanup_worktree` — последнюю в цепочке coding-воркфлоу, между
`notify` и терминальным состоянием. Она вызывает существующий идемпотентный
`wfgit.remove_worktree`, обнуляет `state.worktree` и сохраняет state. Артефакты и
state.json остаются нетронутыми.

### Цепочка фаз

Было: `explore → generate_branch_name → prepare → plan → implement → review → mr → notify`.

Станет: `… → mr → notify → cleanup_worktree`.

В [workflows/coding.py](../../../src/wf/workflows/coding.py) меняем:

- В `phases` добавляем `"cleanup_worktree"` после `"notify"`.
- Транзишены: `"notify": lambda _s: "cleanup_worktree"`, `"cleanup_worktree": terminal`.

### Контракт фазы

Реализует [Phase](../../../src/wf/phases/base.py#L56) протокол:

| Метод | Поведение |
|---|---|
| `name` | `"cleanup_worktree"` |
| `required_artifacts` | `[]` |
| `produced_artifacts` | `[]` |
| `required_tools` | `[]` — использует только `wfgit.remove_worktree` (как фаза `prepare`) |
| `is_ready` | всегда `True` — все edge cases обрабатываются внутри `run()` |

Логика `run(state, ctx)`:

1. **`state.worktree is None`** → `phase_note text="skipped: no worktree in state"`,
   `PhaseResult.ok()`. Покрывает случай, когда `prepare` пропустили, или фаза
   запускается повторно после уже выполненной чистки.
2. **Иначе** — снапшот `path = state.worktree`, вызов
   `wfgit.remove_worktree(state.repo_path, path)`. Функция уже идемпотентна (все
   `run_shell` с `check=False`, плюс `shutil.rmtree(..., ignore_errors=True)`); бросать
   не должна.
3. После вызова проверяем `path.exists()`:
   - **`False`** (успех или worktree уже не было): `state.worktree = None`,
     `state.save(ctx.state_path)`, `phase_note text="worktree removed: <path>"`, зелёная
     плашка в console. `PhaseResult.ok()`.
   - **`True`** (удаление не сработало, например — занятые файлы): жёлтый warn в console,
     `phase_note level="warn" text="failed to remove worktree: <path>"`,
     **`state.worktree` оставляем как есть** (чтобы юзер мог дочистить через
     `wf remove`). Всё равно `PhaseResult.ok()`.

### Почему `ok` даже при неудаче

Фаза `cleanup_worktree` идёт после `notify`. К моменту её запуска MR уже создан и
уведомление отправлено — основная работа выполнена. Если по какой-то причине worktree
не удаляется, валить задачу в `terminal=failed` неправильно: пользователь увидит
"failed" у завершённой по сути задачи и потеряет уверенность в пайплайне. Warn в
консоль и в events даёт сигнал без ломки UX. Остаток дочищается командой
`wf remove`.

### Мутация state

`state.worktree = None` плюс явный `state.save(ctx.state_path)` внутри фазы. Это тот же
паттерн, что в [phases/mr.py](../../../src/wf/phases/mr.py) (где выставляется `mr_url`)
и в [phases/prepare.py](../../../src/wf/phases/prepare.py) (где выставляются `branch` и
`worktree`). Engine тоже сохраняет state после фазы через `PhaseRun.append`, но к тому
моменту наша мутация должна быть уже на диске, чтобы корректно отразиться в `wf status`.

### Изменения по файлам

| Файл | Изменение |
|---|---|
| [src/wf/phases/cleanup_worktree.py](../../../src/wf/phases/cleanup_worktree.py) | **Новый.** Класс `CleanupWorktreePhase` с описанным контрактом. |
| [src/wf/phases/__init__.py](../../../src/wf/phases/__init__.py) | Импорт и `PHASES["cleanup_worktree"] = CleanupWorktreePhase`. |
| [src/wf/workflows/coding.py](../../../src/wf/workflows/coding.py) | Добавить `"cleanup_worktree"` в список фаз; заменить `"notify": terminal` на `"notify": lambda _s: "cleanup_worktree"`; добавить `"cleanup_worktree": terminal`. |
| [docs/phases.md](../../phases.md) | Добавить раздел с контрактом фазы `cleanup_worktree` (на русском, в существующем стиле). |
| [CLAUDE.md](../../../CLAUDE.md) | Обновить строку пайплайна в секции "Project" (`… → notify → cleanup_worktree`) и список фаз в "Implementation status". |

### Что **не** делаем (YAGNI)

- **Не удаляем ничего, кроме worktree.** Артефакты, state.json, events.jsonl,
  summary.md сохраняются. Это явное требование: для `wf status` и post-mortem нужна
  история.
- **Не обобщаем фазу в `cleanup` с шагами.** Сейчас единственное действие — удаление
  worktree; имя `cleanup_worktree` отражает это явно. Если в будущем появятся другие
  cleanup-шаги (временные ветки, кэши) — заведём отдельные фазы или переименуем
  тогда, не сейчас.
- **Не добавляем флаг "пропустить cleanup".** Если кому-то нужно сохранить worktree,
  можно убрать фазу из workflow вручную (своя версия `coding`). Конфиг для одной
  опт-аутной опции — избыточно.
- **Не пишем тесты.** В репозитории нет тестового фреймворка
  (см. [CLAUDE.md](../../../CLAUDE.md) — "There is no test suite, linter, or formatter
  configured"). Matching existing convention.

### Совместимость

Существующие задачи с `current_stage="notify"` после обновления:
- Если `terminal != "done"`, `wf resume` доведёт их до `cleanup_worktree`. Это
  ожидаемое поведение — старый worktree почистится при следующем `resume`.
- Если задача уже в `terminal="done"`, `wf resume` ничего не делает (engine выходит
  сразу). Старые worktree этих задач придётся чистить через `wf remove` — это
  одноразовая стоимость миграции, перевешивается тем, что новые задачи всегда сами
  убирают за собой.

## Открытые вопросы / решения для этапа плана

Нет. Дизайн полностью определён.
