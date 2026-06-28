# 15. Подгружать проектный CLAUDE.md в LLM-фазах

**Приоритет:** P2

## Описание

Проектный `CLAUDE.md` сейчас в контекст модели не попадает ни в одной
LLM-фазе. Из-за этого LLM работает без проектных конвенций (язык
документации, паттерны, ограничения вроде «не плодить тесты без
просьбы») — приходится дублировать эти инструкции в каждом шаблоне
фазы или мириться с расхождениями.

Причины — две, независимые, обе блокируют загрузку:

1. **`setting_sources` в [claude.py](../src/wf/claude.py:263).** В
   [phases/plan.py](../src/wf/phases/plan.py:75) передаётся
   `setting_sources=["user"]` → грузится только `~/.claude/CLAUDE.md`.
   В [phases/implement.py](../src/wf/phases/implement.py:70) и
   [phases/review.py](../src/wf/phases/review.py:53) `setting_sources`
   не передаётся вовсе — для `claude-agent-sdk` это означает «не
   подтягивать filesystem-настройки и memory-файлы», т.е. ни проектная,
   ни user-память не грузятся.
2. **Расположение файла.** [copy_claude_dir](../src/wf/git.py#L146)
   копирует `<repo>/.claude/CLAUDE.md` → `<worktree>/.claude/CLAUDE.md`,
   но SDK как project memory ищет `<cwd>/CLAUDE.md` в корне. Корневой
   `<repo>/CLAUDE.md` (стандартное место) сам попадает в worktree через
   `git worktree add`, если закоммичен — но если CLAUDE.md лежит только
   в `.claude/` (нестандартная раскладка), для модели он остаётся
   невидимым.

## Что сделать

### 1. `setting_sources=["project", "user"]` в LLM-фазах

В четырёх фазах, где проектные конвенции релевантны, явно передавать
`setting_sources=["project", "user"]`:

- [phases/explore.py](../src/wf/phases/explore.py:59) — формирует
  `task.md`, стиль/язык должны соответствовать репозиторию.
- [phases/plan.py](../src/wf/phases/plan.py:66) — заменить текущий
  `["user"]` на `["project", "user"]`.
- [phases/implement.py](../src/wf/phases/implement.py:70) — добавить
  параметр.
- [phases/review.py](../src/wf/phases/review.py:53) — добавить параметр.

`generate_branch_name` оставить без изменений: это короткий
генератор имени, проектная память ему не нужна и только увеличит
число токенов.

Конфигом не выносить — хардкод. YAGNI: если появится потребность
отключать project memory per-workflow, добавим поле позже.

### 2. Fallback в `copy_claude_dir`

В [git.py](../src/wf/git.py#L146) после копирования содержимого
`.claude/` добавить: если в `<worktree>/CLAUDE.md` файла нет, но есть
`<repo>/.claude/CLAUDE.md`, скопировать его в `<worktree>/CLAUDE.md`.
Это покрывает репозитории с нестандартной раскладкой (CLAUDE.md
гитигнорируется и лежит только в `.claude/`).

Перезаписывать существующий корневой `CLAUDE.md` нельзя — он может
быть tracked-файлом из git.

Дополнительно: после такого fallback-копирования дописать `CLAUDE.md`
в `<worktree>/.git/info/exclude`, чтобы синтетический файл не
показывался в `git status` worktree (CLAUDE.md проекта явно требует
держать `git status` чистым).

### 3. Проверка побочных эффектов от `["project"]`

С `setting_sources=["project"]` SDK помимо `CLAUDE.md` будет
подхватывать `.claude/settings.json` и `.claude/settings.local.json` из
worktree, если они там окажутся. [copy_claude_dir](../src/wf/git.py#L8)
их не копирует (`CLAUDE_ALLOW_LIST` ограничен `agents/commands/skills/CLAUDE.md`),
но если в самом репозитории `.claude/settings.json` закоммичен —
он попадёт в worktree через git.

Решения два: (а) ничего не делать (это явная пользовательская
конвенция — закоммитил `.claude/settings.json`, получи его эффект);
(б) предупредить в README/CLAUDE.md проекта `wf`, что закоммиченный
`.claude/settings.json` будет влиять на поведение фаз. Выбрать (а) +
короткую заметку в [CLAUDE.md](../CLAUDE.md) → секция «Conventions».

### 4. Наблюдаемость

В `events.jsonl` уже пишется `llm_start` с `cwd`. Достаточно убедиться
вручную (на тестовом запуске) по `system`-событиям SDK, что project
memory подхвачена. Отдельного логирования факта загрузки CLAUDE.md
не добавлять — это деталь реализации SDK.

## Критерии приёмки

- На тестовом репо с корневым `CLAUDE.md`, содержащим маркерную
  инструкцию (например, «в ответе всегда упоминай слово ПРОЕКТ-OK»),
  ответы модели в фазах `explore`, `plan`, `implement`, `review`
  содержат этот маркер. В `generate_branch_name` — не содержат.
- На репо без `CLAUDE.md` фазы не падают (graceful no-op).
- На репо, где `CLAUDE.md` лежит только в `.claude/CLAUDE.md`,
  fallback кладёт его в корень worktree, маркер из критерия выше
  по-прежнему виден в ответах.
- `git status` внутри worktree чист в случае fallback-копирования
  (CLAUDE.md в `.git/info/exclude`).
- Поведение `generate_branch_name` не меняется — у него
  `setting_sources` не задан, проектная память не подтягивается.

## Заметки

- `CLAUDE.local.md` в worktree не появится: git его игнорирует, а
  через `copy_claude_dir` мы его не тащим. Это ожидаемое ограничение —
  user memory `~/.claude/CLAUDE.md` остаётся единственным local-вариантом.
- В `explore` `cwd` равен пути исходного репо (worktree ещё не
  создан), так что project memory читается из корня репозитория
  пользователя — это корректное поведение.
