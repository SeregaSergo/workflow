# 01. Покрыть тестами критические инварианты

**Приоритет:** P0

## Описание

В проекте нет тестов, линтера и форматера (явно зафиксировано в `CLAUDE.md`).
Все нетривиальные куски — regex маркеров, парсер git-команд в hook,
transition-функции, атомарная запись `state.json`, helper-ы `artifacts.*`,
`_parse_url`, `_extract_commit_subject`, `_SLUG` — без тестов рефакторить
опасно. Это база, на которой стоят все остальные задачи.

## Что сделать

- Подключить `pytest` как dev-dependency в `pyproject.toml`
  (`[dependency-groups] dev = [...]` или `[project.optional-dependencies]`).
- Добавить цель `make test` в [Makefile](../Makefile).
- Юнит-тесты:
  - regex-блоки во всех фазах ([explore.py](../src/wf/phases/explore.py),
    [plan.py](../src/wf/phases/plan.py), [review.py](../src/wf/phases/review.py),
    [implement.py](../src/wf/phases/implement.py)) с снэпшотами «грязного»
    вывода LLM — лишние пробелы, перепутанный порядок секций, отсутствие
    `===END===`, дубль `===END===` внутри блока;
  - [`review_next`](../src/wf/workflows/coding.py) — переход на `plan`,
    выход за `max_iterations`, путь без feedback (→ `mr`);
  - [`make_readonly_git_hook`](../src/wf/hooks.py) — матрица команд:
    `git status`, `git commit`, `cd x && git push`, `` `git reset` ``,
    `git -C /repo log`, `$(git checkout)`, `git --git-dir=... reset`;
  - [`State.save`](../src/wf/state.py) — атомарность при crash в середине
    записи (через `monkeypatch` на `os.replace`);
  - helper-ы из [artifacts.py](../src/wf/artifacts.py) — соответствие путей
    конфигу;
  - [`_parse_url`](../src/wf/phases/mr.py),
    [`_extract_commit_subject`](../src/wf/phases/implement.py),
    [`_SLUG`](../src/wf/phases/generate_branch_name.py).
- Интеграционный тест на временном git-репо (`tmp_path`):
  - `prepare` создаёт worktree в правильном месте;
  - `implement` фейлится при пустом `git status --porcelain`;
  - `implement` коммитит и проходит при наличии изменений;
  - `wf remove` чистит и worktree, и task_dir.
- Замокать `claude_agent_sdk.query` (async generator с фиксированным
  `AssistantMessage`/`ResultMessage`) — фазы должны работать end-to-end без
  реальных вызовов LLM.

## Критерии приёмки

- `make test` зелёный локально.
- Покрыты все упомянутые модули (≥80% по `coverage` для них).
- Команда документирована в Makefile и в `CLAUDE.md` (поправить фразу
  «no test suite … configured»).
