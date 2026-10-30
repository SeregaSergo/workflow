# wf — оркестратор пайплайна разработки

`wf` — это Python-CLI, который проводит задачу через линейный конвейер
фаз. Пока реализован только пайплайн для написания кода по задаче. Для шагов, требующих рассуждений, `wf` вызывает Claude через
[`claude-agent-sdk`](https://github.com/anthropics/claude-agent-sdk-python); для
шагов, требующих внешних систем (получение задачи из трекера, diff, создание MR,
уведомления в чат), — пользовательские утилиты, объявленные в конфиге.

## Конвейер

```
explore → generate_branch_name → prepare → coding → review → mr → notify → cleanup_worktree
                                              ↑_________|
                                          (backward edge, если ревью нашло критичные замечания)
```

| Фаза | Что делает | Чем |
|---|---|---|
| `explore` | Читает задачу из трекера, исследует контекст, формулирует заголовок и открытые вопросы | LLM + внешний инструмент |
| `generate_branch_name` | Генерирует имя ветки по задаче | LLM |
| `prepare` | Переключается на базовую ветку, тянет изменения, создаёт изолированный `git worktree`, копирует разрешённые части `.claude/` | git |
| `coding` | Исследует код, строит план (сохраняется артефактом), меняет код и делает коммит — единственная фаза, мутирующая worktree; при необходимости задаёт уточняющие вопросы (QA-петля) | LLM |
| `review` | Делает ревью diff'а; при критичных замечаниях возвращает на `coding` со следующей итерацией | LLM + внешний инструмент |
| `mr` | Создаёт merge request | внешний инструмент `create_mr` |
| `notify` | Отправляет уведомление о MR в чат | внешний инструмент `notify` |
| `cleanup_worktree` | Удаляет worktree, созданный в `prepare` | git |


Цикл `review → coding` ограничен `max_iterations` (по умолчанию 3).

## Требования

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (Makefile поставит его автоматически, если нет)
- `git`
- Рабочий доступ к `claude` (через `claude-agent-sdk`)
- Любые CLI-утилиты, которые вы пропишете в `tools:` (например, `glab`/`gh` для MR,
  свой бинарь для уведомлений)

## Установка

```bash
# Установить wf глобально как uv tool (положит `wf` в PATH)
make install

# После правок кода — переустановить
make reinstall

# Для разработки: локальный .venv с editable-установкой
make dev
source .venv/bin/activate

# Удалить инструмент и конфиг (~/.wf/); каталог данных удаляется вручную
make uninstall
```

`make help` показывает все доступные цели.

## Конфигурация

При первом запуске `wf` создаёт `~/.wf/config.yaml` из упакованного дефолта
[src/wf/default-config.yaml](src/wf/default-config.yaml). Путь к конфигу фиксирован
(можно переопределить переменной `WF_CONFIG`).

Ключевые поля:

```yaml
data_dir: ~/wf            # где хранятся state и артефакты всех задач

defaults:
  max_iterations: 3       # потолок цикла review → coding

claude:
  permission_mode: bypassPermissions
  model: null             # модель по умолчанию для всех фаз (null = выбирает SDK)
  phase_models:           # override модели по фазам
    coding: claude-opus-4-7

# Внешние утилиты, которые шеллятся фазами. cmd рендерится через jinja2.
tools:
  fetch_task:
    cmd: "youtrack get {{task_id}}"
  fetch_article:            # необязательный: чем тянуть статью-документацию
    cmd: "youtrack article get {{article_id}}"
  create_mr:
    cmd: "glab mr create --source-branch {{branch}} --target {{base_branch}} --title {{task_id|shquote}} --yes"
  notify:
    cmd: "mynotify {{channel_id}} {{message|shquote}}"

workflows:
  coding:
    notify_channel_id: "REPLACE_ME"   # канал для уведомлений о MR
    reviewers: []                     # пул ревьюверов (notify выберет 2 случайно)
    pause_before: []                  # фазы, перед которыми движок остановится для ручной проверки
```

Контракты внешних утилит:

- **`fetch_task`** — обязателен для `explore`. Фаза не запускает его сама: из `cmd`
  вырезается плейсхолдер `{{task_id}}`, а остаток подставляется в промпт как команда,
  которую модель вызывает через `Bash` с нужным id.
- **`fetch_article`** — необязателен, для `explore`. Так же, но плейсхолдер —
  `{{article_id}}`: команда для статьи/документации, если задача ссылается на неё.
  Не настроен — блок про статьи просто не попадает в промпт.
- **`create_mr`** — `cwd` = worktree. Команда должна печатать URL MR последней непустой
  строкой stdout (`http(s)://…`). Ненулевой exit или отсутствие URL = ошибка фазы.
- **`notify`** — `cwd` = worktree; дополнительно в контексте `channel_id`, `reviewers` и
  готовое `message`.

В контексте любого инструмента доступны поля задачи (`runner.tool_context`): `task_id`,
`task_url`, `task_title`, `workflow`, `branch`, `base_branch`, `repo_path`, `worktree`,
`mr_url`, `iteration`. Незаполненные приходят пустой строкой, а не ошибкой рендера.

Отрендеренная строка по умолчанию разбивается `shlex.split` и запускается напрямую, без
шелла (`$VAR` не раскрывается, `~` — только в виде `~/`). Если в **шаблоне** `cmd` есть
shell-операторы (`&& || ; | & > < $( \``) — команда целиком уходит в `/bin/sh -c`, так что
можно писать цепочки:

```yaml
tools:
  notify:
    cmd: >-
      mynotify {{channel_id}} {{message|shquote}}
      && youtrack add-comment {{task_id}} {{("мр " ~ mr_url)|shquote}}
```

Режим определяется по шаблону, а не по значениям: `|` внутри `{{…}}` — это jinja-фильтр и
шелл не включает, а метасимволы из подставленного текста ничего не переключают. Аргументы
с пробелами/переводами строк/кавычками прогоняйте через `|shquote` в обоих режимах.

Фаза, для которой нужный инструмент не настроен, не запустится (`wf start` падает с
понятной ошибкой). Если MR/уведомления не нужны — можно использовать workflow без
этих фаз.

## Использование

```bash
# Создать задачу и сразу прогнать конвейер до конца или до первого блокера.
# Запускайте из каталога целевого репозитория (или укажите --repo).
wf start ABC-123
wf start ABC-123 --workflow coding --repo /path/to/repo --base-branch main

# Продолжить после ответов на QA или после сбоя (сбрасывает terminal=failed)
wf resume ABC-123

# Таблица всех задач: фаза, итерация, ветка, токены, стоимость
wf status

# Логи задачи: summary.md (по умолчанию) или сырой events.jsonl
wf logs ABC-123
wf logs ABC-123 --raw --tail 50

# Удалить артефакты задачи и связанный worktree
wf remove ABC-123 -y
```

### Коды выхода движка

| Код | Значение |
|---|---|
| `0` | задача завершена (`terminal=done`) |
| `1` | сбой (`terminal=failed`) — почините причину и `wf resume` |
| `2` | нужен ввод человека (QA) — заполните файл вопросов и `wf resume` |
| `3` | пауза перед фазой из `pause_before` — проверьте результат и `wf resume` |

### Цикл human-in-the-loop (QA)

Когда фаза нуждается в уточнениях, она пишет файл с вопросами и выходит с кодом 2.
CLI печатает оба пути. Для `coding` это соглашение «переименуй файл»: фаза создаёт
`plan_qa_<iter>.md.template`, вы заполняете его и переименовываете в
`plan_qa_<iter>.md`, затем `wf resume` — фаза увидит ответы и продолжит.

## Где что лежит на диске

Конфиг и данные разнесены, чтобы `git status` в рабочем репозитории оставался чистым:

- **Конфиг** — `~/.wf/config.yaml` (или `$WF_CONFIG`).
- **Данные** — каталог из `data_dir` (по умолчанию `~/wf`):

```
<data_dir>/workflow/<workflow>/<task_id>/
├── state.json      # модель состояния (атомарная запись)
├── events.jsonl    # append-only поток событий всех фаз
├── summary.md      # человекочитаемая сводка, перерисовывается после каждой фазы
└── artifacts/
    ├── task.md
    ├── plan_<iter>.md, plan_qa_<iter>.md(.template)
    ├── review_<iter>.md, feedback_<iter>.md
    └── worktree/<branch>/   # сам git worktree
```

## Workflows

Конвейер описывается как `Workflow = {phases, start, transitions}`, где transitions —
чистые функции `State -> следующая фаза`. Встроенный workflow — `coding`
([src/wf/workflows/coding.py](src/wf/workflows/coding.py)). Свой workflow добавляется
модулем в [src/wf/workflows/](src/wf/workflows/) и регистрацией в `WORKFLOWS`; затем
`wf start --workflow <name>` подхватит его. Как добавлять фазы и workflows — в
[CLAUDE.md](CLAUDE.md).
