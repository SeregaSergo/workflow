# 14. Подключить LSP к фазам через MCP-сервер

**Приоритет:** P3

## Описание

В фазах `plan` и `review` модель ищет по коду исключительно через
`Grep`/`Glob` — текстовый поиск без понимания символов. На больших
кодовых базах это шумно: одинаковые имена в разных пакетах, перегрузки,
ре-экспорты, генерики. LSP даёт точные `find-references`,
`go-to-definition`, `workspace/symbol`.

В [phases/plan.py](../src/wf/phases/plan.py:74) в `allowed_tools` уже
лежит строка `"LSP"`, но это no-op: `claude-agent-sdk` не имеет
встроенного LSP-тула (проверено в
`.venv/.../claude_agent_sdk/types.py` — упоминаний нет), а
[claude.py](../src/wf/claude.py) не пробрасывает `mcp_servers` в
`ClaudeAgentOptions`. Сейчас разрешение просто игнорируется.

## Что сделать

- В [config.py](../src/wf/config.py) добавить секцию `mcp_servers:` в
  верхнеуровневый `Config` (имя → команда/аргументы/env), плюс
  per-phase whitelist в `WorkflowSpec`/`Defaults` — какие MCP-серверы
  стартовать в каждой фазе (LSP нужен plan/review, не нужен implement).
- В [claude.py](../src/wf/claude.py) `run_phase` принять параметр
  `mcp_servers` и пробросить в `ClaudeAgentOptions(mcp_servers=…)`.
  Имена MCP-тулов имеют префикс `mcp__<server>__<tool>`; нужно
  расширить `allowed_tools` фильтром по этому префиксу либо явно
  перечислять разрешённые MCP-тулы в шаблоне фазы.
- Выбрать LSP-MCP-сервер. Кандидаты:
  - [`mcp-language-server`](https://github.com/isaacphi/mcp-language-server) — Go-обвязка над `gopls`/`pyright`/`rust-analyzer`/etc.
  - встроить через `npx` запуск в конфиге, чтобы не тащить бинарь в репо.
- В шаблонах [plan.md.j2](../src/wf/templates/plan.md.j2) и
  [review.md.j2](../src/wf/templates/review.md.j2) добавить секцию
  «Tools»: коротко описать, когда предпочесть LSP-вызов над `Grep`
  (поиск всех использований символа, прыжок в определение функции
  из другого файла). Без подсказки модель не переключится.
- Из [phases/plan.py](../src/wf/phases/plan.py:74) убрать или заменить
  фейковый `"LSP"` на реальные `mcp__lsp__*`-имена.

## Критерии приёмки

- В `events.jsonl` фазы `plan` на реальной задаче видны `tool_use`
  события с `name="mcp__lsp__find_references"` (или аналог).
- Конфиг MCP-серверов задаётся в YAML, без правки кода.
- Если MCP-сервер не запустился — фаза падает с понятной ошибкой,
  а не молча работает без LSP (либо явно деградирует с предупреждением
  в консоль — выбрать политику в задаче).
- Контракт фазы `implement` не меняется: LSP туда не подключается,
  read-only-hook из [hooks.py](../src/wf/hooks.py) остаётся.

## Заметки

- MCP-серверы стартуют per-query, это overhead. Возможно, стоит
  переиспользовать процесс между итерациями `review → plan` —
  но это уже оптимизация после MVP.
- LSP-сервер сам по себе требует прогретого индекса (gopls/pyright
  индексируют проект при старте). На холодном старте первая фаза
  будет медленнее — учесть в таймаутах.
