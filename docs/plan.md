# План: оркестратор пайплайна разработчика (`wf`)

## Context

Нужен CLI-инструмент, который проводит задачу через линейный пайплайн фаз
(Explore → Generate branch name → Prepare → Plan → Implement → Review → MR),
описанный в [docs/phases.md](docs/phases.md), с возможностью петли
Review → Plan (новая итерация) и хранением состояния в JSON по схеме из
[docs/State.md](docs/State.md). Внешние помощники (получение задачи из YouTrack,
получение diff'a, создание MR) — отдельные бинарники, объявляемые в YAML-конфиге;
основную интеллектуальную работу выполняет Claude Code. Цель — иметь возможность
запустить `wf init ABC-123` в корне репозитория и довести задачу до MR
полуавтоматически, с прозрачным стейтом и возможностью продолжить с любой фазы.

## Стек

- **Python 3.12+**, упакован как CLI через `uv tool install` (пользователь делает
  `uv tool install git+...` либо `uvx wf`, и получает в PATH команду `wf`).
- **claude-agent-sdk** для фаз с LLM — он под капотом дёргает `claude -p`,
  но даёт типизированные события (assistant/tool/result), нормальную обработку
  ошибок и потоковый вывод.
- **typer** для CLI, **pydantic v2** для моделей стейта/конфига,
  **PyYAML** для конфига, **jinja2** для шаблонов промптов и команд внешних
  инструментов, **rich** для красивого `wf status`.
- **subprocess + git CLI** для worktree-операций (без GitPython —
  меньше зависимостей, надёжнее).

## Архитектура

### Структура данных на диске

Конфиг и данные разнесены:

- конфиг: `~/.wf/config.yaml` (или `$WF_CONFIG`, если задан);
- данные: каталог из поля `data_dir:` в этом конфиге, по умолчанию `~/wf`.

```
~/.wf/config.yaml                     # пользовательский конфиг (поверх дефолта)

<data_dir>/                            # ~/wf по умолчанию
└── workflow/
    └── coding/
        └── ABC-123/
            ├── state.json            # формат из docs/State.md + поля runtime
            ├── events.jsonl          # единый append-only поток событий за таск
            ├── summary.md            # отчёт, ререндерится после каждой фазы
            └── artifacts/
                ├── task_1.md
                ├── task_qa_1.md      # optional
                ├── plan_1.md
                ├── plan_qa_1.md      # optional
                ├── review_1.md
                ├── task_2.md         # сгенерирован review_1, триггерит iter 2
                ├── plan_2.md
                └── review_2.md
```

Worktree создаётся отдельно (`../<branch_name>` рядом с репо) согласно
phases.md, в нём же копируется `.claude`. Артефакты живут в `data_dir`,
а не в worktree — чтобы не мусорить в индекс git и не зависеть от
`git clean`.

### Модуль `state.py`

`State` — pydantic-модель полей из State.md + служебные:
`current_stage`, `iteration`, `phase_history: list[PhaseRun]` (имя, ts,
exit_code, duration_ms), `last_error`. Чтение/запись — атомарно
(tmp-файл + `os.replace`).

### Модуль `config.py`

Pydantic-модель + загрузка YAML. Структура:

```yaml
version: 1
data_dir: ~/wf
defaults:
  max_iterations: 3

tools:
  fetch_task:
    cmd: "yt-fetch {{ task_id }}"
    output_model: json_model
  get_diff:
    cmd: "git diff {{ base_branch }}..HEAD"
    output_model: text
  create_mr:
    cmd: "yt-mr-create --task {{ task_id }} --branch {{ branch }}"
    output_model: json_model
```

### Пайплайн и фазы (`pipeline.py`, `phases/`)

Базовый класс `Phase` с методами `inputs() / run(state, ctx) / outputs()`,
плюс `is_done(state) -> bool` для идемпотентности (фаза скипается, если
её output-артефакт уже есть и не передан `--force`). Конкретные фазы:

1. **explore** — рендерит `tools.fetch_task.cmd`, кладёт stdout в
   `task_{i}.md`, затем дёргает claude-agent-sdk с шаблоном
   `templates/explore.md.j2`, который просит дополнить пояснениями и
   собрать `task_qa_{i}.md` если он требуется. Останавливает выполнение, если есть `task_qa_{i}.md`.
2. **generate_branch_name** — короткий claude-agent-sdk вызов с
   `templates/branch_name.md.j2`; результат пишется в `state.branch`.
   Валидация по regex (kebab-case, без спецсимволов).
3. **prepare** — чистый shell: `git fetch`, `git checkout base_branch`,
   `git pull`, `git worktree add ../<branch> -b <branch>`, `cp -r .claude
   ../<branch>/`. Заполняет `state.worktree`.
4. **plan** — claude-agent-sdk с `templates/plan.md.j2`, cwd=worktree,
   читает `task_{i}.md` и код проекта, пишет `plan_{i}.md` (+ опц.
   `plan_qa_{i}.md`). Останавливает выполнение, если есть `plan_qa_{i}.md`.
5. **implement** — claude-agent-sdk с `templates/implement.md.j2`,
   получает task+plan, делает правки, запускает тесты, коммитит.
   Внутренний цикл: до `implement_retries` попыток; если тесты не
   сходятся — следующий заход с фидбеком от прошлой попытки.
   Успех = коммит создан и `git status --porcelain` пуст.
6. **review** — рендерит `tools.get_diff.cmd`, передаёт diff в claude
   через `templates/review.md.j2`; ожидает `review_{i}.md` и, если есть
   критичные замечания, `task_{i+1}.md`.
7. **mr** — рендерит `tools.create_mr.cmd`, считает фазу финальной.

### State-машина

Линейная, плюс одна обратная транзакция:

```
explore → generate_branch_name → prepare → plan → implement → review → ?
                                                                 │
                                                                 ├── task_{i+1}.md есть → plan (iter+1)
                                                                 └── иначе → mr
```

Лимит `max_iterations` (default 3) — после исчерпания фаза `review`
не может перейти на фазу plan, делается пометкой в
логе.

### CLI (`cli.py`, typer)

- `wf init <task_id> --workflow coding(enum: coding)`
  — создаёт каталог задачи, кладёт стартовый `state.json`, запоминает
  `repo_path`, `base_branch`.
- `wf resume <task_id> --workflow coding(enum: coding)` — продолжить выполнение.
- `wf status` — таблица: незавершенные воркфлоу, текущая фаза, итерация, последние артефакты, exit-коды последних запусков (через `rich`).

### Интеграция с Claude через claude-agent-sdk

Один тонкий враппер `claude.py::run_phase(prompt_template, state, extra_ctx)`:

- рендерит jinja2-промпт (в контекст идут поля стейта, итерация, пути
  артефактов, список TODO),
- запускает `ClaudeSDKClient` с `cwd=state.worktree`,
  `allowed_tools=config.claude.allowed_tools`,
- стримит SDK-сообщения (assistant_text, tool_use, tool_result, thinking,
  system, result) как JSON-строки в общий для таска `events.jsonl`,
- движок после каждой фазы вызывает `run_log.rerender_summary(...)`,
  который из `events.jsonl` собирает читаемый `summary.md`.

Шаблоны промптов в `src/wf/templates/` версионируются вместе с
инструментом, но пользователь может оверрайдить любой шаблон, положив
файл с тем же именем в `<data_dir>/templates/`.

### Запуск внешних инструментов (`runner.py`)

Рендерит `tools.<name>.cmd` через jinja2, шеллит через `subprocess.run`
с тайм-аутом. Не-LLM фазы (`mr`, `notify`) пишут результат запуска в
`events.jsonl` как событие `external_tool` (cmd, exit_code, duration_ms,
короткие превью stdout/stderr). Нулевой код = успех; иначе фаза падает
с читаемым сообщением.

## Критические файлы для реализации

- [src/wf/cli.py](src/wf/cli.py) — точки входа
- [src/wf/config.py](src/wf/config.py) — модель и загрузка конфига
- [src/wf/state.py](src/wf/state.py) — модель стейта и атомарная запись
- [src/wf/pipeline.py](src/wf/pipeline.py) — state-машина и транзиции
- [src/wf/phases/](src/wf/phases/) — реализации фаз
- [src/wf/claude.py](src/wf/claude.py) — обёртка над claude-agent-sdk
- [src/wf/runner.py](src/wf/runner.py) — запуск внешних бинарников
- [src/wf/git.py](src/wf/git.py) — worktree-операции
- [src/wf/templates/](src/wf/templates/) — jinja2 шаблоны промптов
- [default-config.yaml](default-config.yaml) — дефолтный конфиг
- [pyproject.toml](pyproject.toml) — `[project.scripts] wf = "wf.cli:app"`

## План реализации

1. **Каркас**: pyproject.toml с typer/pydantic/PyYAML/jinja2/rich/
   claude-agent-sdk, скелет CLI, `wf --help` работает.
2. **State + Config**: модели, загрузка/сохранение, `wf init`, `wf status`.
3. **Runner внешних команд**: jinja2-рендер, subprocess, событие
   `external_tool` в `events.jsonl`.
4. **Git/worktree-обёртки** и фаза `prepare`.
5. **claude.py-враппер** и одна простейшая фаза (`generate_branch_name`)
   — чтобы убедиться, что SDK работает end-to-end.
6. **Фазы explore / plan / implement / review** с jinja2-шаблонами
   промптов.
7. **Pipeline / state-машина**: `wf next`, `wf run`, петля
   review→plan, лимит итераций.
8. **Фаза mr**, опции `--force`, `wf phase`, `wf reset`, `wf logs`.
9. **Тесты**: pytest, фейковый репо в tmp_path, mock claude-agent-sdk
   (через подменяемый класс клиента) + интеграционный сценарий
   review→plan→mr.

## Verification

- `uv tool install --from . wf && wf --help` → видно команды.
- `wf init TEST-1 --workflow coding` на тестовом репо → создан
  `~/wf/workflow/coding/TEST-1/state.json`, стартовая фаза
  `explore`.
- Конфиг с фейковыми инструментами (`echo` вместо `yt-fetch` и т.п.) +
  замоканный SDK-клиент: `wf run` доходит до фазы `mr`, в логе видно,
  что петля review→plan отрабатывает при искусственно созданном
  `task_2.md`.
- `pytest`: state-машина, идемпотентность фаз, лимит итераций,
  атомарная запись стейта, рендер шаблонов команд.
- Реальный smoke на маленькой задаче в YouTrack: `wf init <real_id>`
  → `wf run` → проверить, что MR создаётся, артефакты разложены
  правильно.

## Открытые вопросы (можно отложить)

- Параллельный запуск нескольких задач — текущая схема позволяет
  (разные подпапки `<data_dir>/workflow/coding/<task_id>`), но claude-cli
  может конфликтовать на квотах; если станет проблемой — добавить
  семафор в `<data_dir>/locks/`.
- Расширение под другие workflow (не только `coding`) — структура уже
  предусматривает (`workflows.<name>.phases`), новые фазы добавляются
  как классы + шаблон.
- Откат после неудачного `mr` — пока на пользователе.
