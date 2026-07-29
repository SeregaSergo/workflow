# 16. Фаза `address_comments` — правки и ответы на комментарии MR (через MCP)

**Приоритет:** P2

## Описание

Сейчас пайплайн `coding` заканчивается так: `… → mr → notify →
cleanup_worktree`. После того как фаза [`mr`](../src/wf/phases/mr.py) создала
merge request, пользователь идёт в GitLab, смотрит MR — и часто хочет что-то
поправить. Механизма «оставить комментарии в MR и попросить workflow их
разобрать» нет.

Нужна новая LLM-фаза **`address_comments`**, которая идёт сразу за `mr`:

1. Движок **останавливается перед фазой** (через существующий
   `pause_before`), пользователь оставляет комментарии в тредах MR в GitLab.
2. По `wf resume` фаза запускает LLM с доступом к **GitLab MCP-серверу**: модель
   сама читает открытые треды MR, по каждому решает — **внести правку в код**
   или **просто ответить**, вносит правки и **отвечает на все комментарии** (в
   т.ч. на те, по которым были правки) прямо через MCP. Ответы **короткие**,
   без воды. Коммит и пуш делает сама фаза (LLM git не трогает).
3. Фаза **повторяется** (итеративный цикл): после разбора очередной пачки
   комментариев снова встаёт на паузу. Пользователь читает правки/ответы,
   при желании оставляет новые комментарии и снова `wf resume`. Когда на
   очередном `resume` открытых комментариев нет — цикл завершён, workflow идёт
   дальше в `notify`.

Порядок относительно `notify`: **сначала цикл правок, потом `notify`.** Логика —
пользователь сам вычитывает и полирует MR, а ревьюверов в чат пингуют уже по
готовому.

Инструменты получения комментариев и ответа на них реализуются **через MCP**, а
не через shell-инструменты из `tools:` (как `create_mr`/`notify`). Готовый
GitLab MCP-сервер снимает необходимость писать `glab`-обёртки, а LLM работает с
тредами MR нативно.

### Ключевое ограничение: та же учётка, что и у ревьювера

Workflow может коммитить и комментировать **под той же учётной записью
GitLab**, что и человек-ревьювер (это может быть один и тот же пользователь).
Поэтому «последний комментарий не от бота» **нельзя** определять по автору.
Решение: каждый ответ бота начинается с **маркер-строки** (по умолчанию `BOT:`).
Тред «требует ответа», если он не resolved и **последний комментарий не
начинается с маркера**. Фильтрацию по маркеру и простановку маркера в ответах
делает сама модель по инструкции из шаблона (см. §6). Это устойчиво к совпадению
учёток и остаётся итеративным: ответ бота делает последний комментарий «своим» →
тред выпадает; пользователь дописывает свой комментарий → тред снова попадает в
обработку.

### Компромисс порядка «ответ → пуш»

MCP-инструменты вызывает LLM **внутри своего прогона**, а коммит/пуш фаза делает
**после** прогона. Значит, ответы в тредах публикуются **до** пуша правок (окно —
секунды). В этом контексте риск низкий: ревьювер и автор — часто одна и та же
учётка, это «разговор с самим собой» на собственном MR. Строгий порядок «пуш →
ответ» потребовал бы двух отдельных прогонов LLM (правки → пуш → отдельный
прогон только на ответы), что дороже и сложнее — сознательно не делаем (см.
YAGNI). Если пуш падает — фаза падает (`failed`), ответы уже опубликованы; это
задокументированное поведение.

## Как это работает (обзор)

```
explore → generate_branch_name → prepare → plan → implement → review
      → mr → address_comments ⤾ → notify → cleanup_worktree
```

Цикл строится **на существующем механизме `pause_before` — без изменений в
движке**:

- `pause_before: [address_comments]` заставляет движок встать на паузу *перед*
  фазой (см. [engine.py](../src/wf/engine.py), блок `pause_before`). Выход с
  кодом 3, пользователь оставляет комментарии, `wf resume`.
- Транзишн фазы возвращает **строку `"address_comments"`** (обычный переход на
  себя, **не** `RepeatAt` — итерацию `review`-петли трогать не нужно), если в
  этом прогоне были разобраны комментарии. `_resolve_next` в
  [engine.py](../src/wf/engine.py) на любом переходе сбрасывает
  `pause_consumed_for=None`, поэтому на следующем шаге `pause_before` снова
  срабатывает → фаза опять встаёт на паузу.
- Один `wf resume` = один раунд разбора + пауза перед следующим.
- Раунд, в котором открытых тредов **ноль**, — сигнал «готово»: транзишн
  возвращает `"notify"`, workflow продолжается.

Никакой новой логики пауз/итераций в движке не требуется — только новая фаза,
её транзишн и запись в `pause_before`.

## Что сделать

### 1. State — [state.py](../src/wf/state.py)

Добавить в модель `State` два поля:

```python
mr_comment_round: int = 0                     # счётчик прогонов фазы; имена артефактов и iter для summary
mr_comments_addressed_last_run: bool = False  # разобрал ли последний прогон ≥1 тред; вход транзишна
```

Оба со значениями по умолчанию — старые `state.json` читаются без миграции.

### 2. Config — [config.py](../src/wf/config.py) и [default-config.yaml](../src/wf/default-config.yaml)

- В `WorkflowSpec` добавить поле маркера:

  ```python
  class WorkflowSpec(BaseModel):
      notify_channel_id: str | None = None
      reviewers: list[str] = Field(default_factory=list)
      pause_before: list[str] = Field(default_factory=list)
      bot_comment_marker: str = "BOT:"   # префикс каждого ответа бота; по нему модель отличает свои ответы
  ```

- MCP-сервер и его включение для фазы настраиваются **штатно** (см.
  [CLAUDE.md](../CLAUDE.md), раздел про MCP), никакого нового кода в резолве не
  нужно — фаза берёт `ctx.config.mcp_for_phase("address_comments")`:

  ```yaml
  # верхнеуровневый mcp_servers: — описание GitLab MCP-сервера (пример; подставь свой)
  mcp_servers:
    gitlab:
      command: npx
      args: ["-y", "@zereight/mcp-gitlab"]
      env:
        GITLAB_PERSONAL_ACCESS_TOKEN: "${GITLAB_TOKEN}"
        GITLAB_API_URL: "https://gitlab.example.com/api/v4"

  # включаем сервер для фазы
  claude:
    phase_mcp_servers:
      address_comments: [gitlab]
  ```

- В [default-config.yaml](../src/wf/default-config.yaml) — добавить
  закомментированный пример выше и комментарий, что для включения цикла правок
  нужно: (а) описать GitLab MCP-сервер в `mcp_servers:`, (б) включить его для
  `address_comments` в `claude.phase_mcp_servers`, (в) добавить
  `address_comments` в `pause_before` нужного workflow.

Дефолтные `pause_before` и `phase_mcp_servers` пустые. Включение цикла — это
добавление `address_comments` в `pause_before`; **наличие фазы в `pause_before`
считается сигналом «цикл включён»** (без паузы комментариев в MR всё равно нет,
так что делать фазе нечего). Когда цикл включён, GitLab MCP-сервер
**обязателен** — без него фаза падает (см. §5). Когда `address_comments` не в
`pause_before`, фаза уходит вперёд и MCP не требуется — так coding-workflow без
правочного цикла работает без GitLab MCP.

### 3. MCP-сервер: требуемые возможности

Фаза не завязана на конкретный сервер — она отдаёт LLM все инструменты
`mcp__<server>__*`, включённые для `address_comments`. Достаточно любого GitLab
MCP-сервера, который умеет:

- **перечислить дискуссии/треды MR** — с телом заметок, автором, флагом
  `resolved` (по `mr_url` либо `project`+`merge_request_iid`; iid/проект модель
  берёт из `mr_url`);
- **опубликовать ответ в тред** (создать note в существующей дискуссии MR).

Желателен, но не обязателен, инструмент resolve — в MVP не используем (дедуп
держит маркер).

MCP-серверу нужен GitLab PAT с правом писать заметки в MR (env-переменная в
`mcp_servers.<server>.env`, как в примере выше).

### 4. Артефакты — [artifacts.py](../src/wf/artifacts.py)

Добавить хелпер (индекс — `state.mr_comment_round`):

```python
def mr_replies(config, state) -> Path:   # _dir / f"mr_replies_{state.mr_comment_round}.md"
```

Сырые треды отдельным артефактом не сохраняем — MCP-обращения (tool_use/
tool_result) уже пишутся в `events.jsonl` через `claude.run_phase` и попадают в
`summary.md`. Пути — только через хелперы.

### 5. Фаза — `src/wf/phases/address_comments.py`

`name = "address_comments"`, `required_tools() -> []` (shell-инструментов у фазы
нет). `is_ready`: `state.mr_url` задан и worktree существует. Наличие
MCP-сервера проверяется мягко в `run()` (фаза опциональна).

`run(state, ctx)`:

1. `mcp_servers, mcp_tools, missing = ctx.config.mcp_for_phase(self.name)`;
   по каждому `missing` — предупреждение в консоль (как в
   [explore.py](../src/wf/phases/explore.py)).
2. **Gate «цикл включён» + обязательный MCP.**
   `wf_spec = ctx.config.workflows.get(state.workflow)`;
   `engaged = wf_spec is not None and self.name in wf_spec.pause_before`.
   - Если **не** `engaged` (фазы нет в `pause_before` — цикл не запрашивали) →
     `state.mr_comments_addressed_last_run = False` (иначе устаревший `True`
     увёл бы транзишн обратно в `address_comments`, а без паузы петлю ничто не
     остановит — infinite loop), лог (жёлтым) «цикл правок не включён (добавь
     `address_comments` в `pause_before`), пропускаю» + `PhaseResult.ok()`
     (→ `notify`). MCP не нужен.
   - Если `engaged`, но `mcp_servers` пуст → `PhaseResult.failed(
     "address_comments: цикл включён (pause_before), но GitLab MCP-сервер не
     настроен — опиши его в mcp_servers: и включи в
     claude.phase_mcp_servers.address_comments")`. (Если сервер указан в
     `phase_mcp_servers`, но отсутствует в `mcp_servers:`, он окажется в
     `missing` из шага 1 — упомянуть его имя в сообщении.)
3. `state.mr_comment_round += 1` (номер этого прогона, 1-based; используется как
   `iter` для `summary.md` и в именах артефактов).
4. Снимок `HEAD` (`git rev-parse HEAD`).
5. Запустить LLM (`claude.run_phase`, шаблон `address_comments.md.j2`):
   - `allowed_tools=["Read","Write","Edit","Glob","Grep","Bash", *mcp_tools]`;
   - `mcp_servers=mcp_servers`;
   - `setting_sources=["project","user"]`;
   - read-only-git-хук из [hooks.py](../src/wf/hooks.py) на `Bash` (LLM читает
     состояние репо, но **не** коммитит/пушит);
   - `phase=self.name`, `iter=state.mr_comment_round`;
   - контекст шаблона: `mr_url`, `branch`, `base_branch`, `bot_marker`
     (= `wf_spec.bot_comment_marker`), сводка задачи (`task.md`), план
     (`plan_<iter>.md`).
6. Распарсить блоки ответа (`markers.parse_blocks`, схема — см. «Протокол
   вывода»): `<addressed>` (обяз., JSON-массив) и `<commit_message>` (необяз.).
   Битый JSON/схема → `PhaseResult.failed(...; см. summary.md
   (## address_comments #<round>))`.
7. `addressed = json.loads(<addressed>)`. Если `addressed` пуст **и** worktree
   чист → работы не было: `state.mr_comments_addressed_last_run = False`, лог
   «нет открытых комментариев», `PhaseResult.ok()` (→ `notify`).
8. Если worktree грязный (`git status --porcelain` непустой):
   - subject из `<commit_message>` (санитизация как в
     [implement.py](../src/wf/phases/implement.py), ≤72), fallback
     `address MR comments (round N)` с жёлтым предупреждением;
   - `git add -A && git commit -m <subject>`;
   - `git push origin <branch>` (ветка уже на remote — её запушил `create_mr`;
     remote — `origin`, как и в [git.py](../src/wf/git.py)). Если пуш упал →
     `PhaseResult.failed`.

   Если worktree чист (были только ответы) — коммит/пуш пропускаем.
9. Сохранить `addressed` в артефакт `mr_replies_<round>.md` (аудит: id, changed,
   текст ответа).
10. `state.mr_comments_addressed_last_run = True`,
    `PhaseResult.ok(usage=summary.usage)` → транзишн снова встаёт на паузу.

Зарегистрировать фазу в [phases/__init__.py](../src/wf/phases/__init__.py).

### 6. Шаблон — `src/wf/templates/address_comments.md.j2`

По стилю существующих шаблонов ([implement.md.j2](../src/wf/templates/implement.md.j2),
[review.md.j2](../src/wf/templates/review.md.j2)). Ключевые инструкции модели:

1. Через GitLab MCP-инструменты получи открытые треды дискуссий MR `{{mr_url}}`.
2. **Пропусти** треды, которые resolved, и те, чей **последний** комментарий
   начинается с маркера `{{bot_marker}}` (это твои прошлые ответы).
3. По каждому оставшемуся треду реши: нужна ли правка кода. Если да — внеси её
   (`Read`/`Edit`/`Write`; состояние git смотри через `Bash`, коммитить/пушить
   **нельзя** — это делает wf).
4. Ответь через GitLab MCP на **каждый** такой тред, включая те, где была
   правка. Каждый ответ **начинай** с `{{bot_marker}} `. Ответы **короткие**
   (1–2 предложения), без пересказа диффа.
5. Формат финального ответа (парсит программа):
   - блок `<addressed>` — **валидный JSON-массив**
     `[{"id": "<discussion_id>", "changed": true|false, "reply": "<текст ответа>"}]`
     по каждому треду, на который ты ответил (пустой массив `[]`, если ни один
     тред не требовал ответа);
   - необязательный блок `<commit_message>` — subject коммита (только если менял
     код), ≤72 символов.

### 7. Workflow — [workflows/coding.py](../src/wf/workflows/coding.py)

- В `phases` добавить `"address_comments"` между `"mr"` и `"notify"`.
- Транзишн (чистая функция от `State`):

  ```python
  def address_comments_next(s: State) -> str:
      return "address_comments" if s.mr_comments_addressed_last_run else "notify"
  ```

- В `transitions`: `"mr": lambda _s: "address_comments"`,
  `"address_comments": address_comments_next`; `"notify"`/`"cleanup_worktree"`
  без изменений.

## Протокол вывода LLM

| Блок | Обяз. | Содержимое |
|---|---|---|
| `<addressed>` | да | JSON `[{"id","changed","reply"}]`, по одному на тред; `[]` если ответов не было |
| `<commit_message>` | нет (`allow_empty=True`) | subject коммита, если менялся код |

Ответы в треды публикует **сама модель** через MCP; `<addressed>` — это отчёт
для фазы (аудит-артефакт + счётчик «была ли работа» для транзишна), а не
источник отправки. Битый JSON или нарушение схемы → `PhaseResult.failed(...; см.
summary.md (## address_comments #<round>))` — та же UX, что и у других фаз.

## Граничные случаи

- **Нет комментариев на `resume`** → `<addressed>` пуст, worktree чист → фаза
  `ok()`, транзишн → `notify` (сигнал «готово»).
- **Раунд «только ответы»** (правок нет) → коммит/пуш пропускаются, ответы уже
  опубликованы моделью через MCP.
- **Цикл не включён** (`address_comments` не в `pause_before`) → фаза логирует и
  уходит вперёд; MCP не требуется. Так coding-workflow без правочного цикла
  работает без GitLab MCP.
- **Цикл включён, но MCP не настроен** → `PhaseResult.failed` с понятным
  сообщением (какие ключи заполнить). MCP **обязателен**, когда цикл включён.
- **Битый `<addressed>`** → `PhaseResult.failed` со ссылкой на `summary.md`.
- **Пуш упал** → `PhaseResult.failed` (ответы к этому моменту уже
  опубликованы — см. «Компромисс порядка»).
- **Одинаковая учётка бота и ревьювера** → дедуп по маркеру `bot_comment_marker`
  в начале последнего комментария, не по автору; фильтрует сама модель.
- **Runaway-цикл невозможен**: каждый раунд гейтится ручным `wf resume`
  (`pause_before`), человек — это «часы» цикла.

## Что НЕ делаем (YAGNI)

- Нет shell-инструментов `fetch_mr_comments`/`reply_mr_comment` — всё через MCP.
- Нет второго прогона LLM ради строгого порядка «пуш → ответ» (см. «Компромисс
  порядка»): в контексте «сам себе ревьювер» окно «ответ до пуша» некритично.
- Нет авто-resolve тредов (дедуп закрывает маркер).
- Нет отдельного конфига «личность ревьювера».
- Нет изменений в движке/механизме пауз — переиспользуем `pause_before`.
- Нет хранения id отвеченных тредов в state — дедуп держит маркер, а каждый
  раунд и так гейтится ручным resume.
```
