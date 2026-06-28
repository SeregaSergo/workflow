# 13. Вынести магические константы в конфиг workflow

**Приоритет:** P3

## Описание

Несколько политик-параметров зашиты в код вместо конфига:

- `_REVIEWERS_PER_MR = 2` в [phases/notify.py](../src/wf/phases/notify.py:13).
- Лимит commit subject 72 символа в
  [phases/implement.py](../src/wf/phases/implement.py:26)
  (`line[:72]` в `_extract_commit_subject` и упоминание в
  [templates/implement.md.j2](../src/wf/templates/implement.md.j2:46)).
- `max_iterations: int = 3` в [config.py](../src/wf/config.py:11) уже в
  `Defaults`, но не в `WorkflowSpec` — нельзя задать per-workflow.

Это политика, а не код: для разных команд может понадобиться 1 или 3
ревьювера, более длинный subject (если внутренний git это позволяет),
или больше итераций ревью на сложных задачах.

## Что сделать

- Расширить `WorkflowSpec` в [config.py](../src/wf/config.py):
  ```python
  class WorkflowSpec(BaseModel):
      tools: list[str] = Field(default_factory=list)
      notify_channel_id: str | None = None
      reviewers: list[str] = Field(default_factory=list)
      reviewers_per_mr: int = 2
      commit_subject_max_len: int = 72
      max_iterations: int | None = None  # переопределяет defaults.max_iterations
  ```
- В [phases/notify.py](../src/wf/phases/notify.py) читать
  `wf_spec.reviewers_per_mr` вместо константы.
- В [phases/implement.py](../src/wf/phases/implement.py) пробросить
  `commit_subject_max_len` в `_extract_commit_subject` через параметр
  (или прочитать из `ctx.config`).
- В [cli.py](../src/wf/cli.py:80) при создании State использовать
  `wf_spec.max_iterations if wf_spec.max_iterations is not None else
  config.defaults.max_iterations`.
- Документировать новые поля в [default-config.yaml](../src/wf/default-config.yaml)
  закомментированными, со значениями по умолчанию.
- Шаблон [implement.md.j2](../src/wf/templates/implement.md.j2):
  параметризовать `max 72 characters` через jinja-переменную
  `commit_subject_max_len`, прокинуть из [phases/implement.py](../src/wf/phases/implement.py)
  в `template_ctx`.

## Критерии приёмки

- Изменение числа ревьюверов, длины commit subject и `max_iterations` —
  правкой YAML, без перекомпиляции.
- Обратная совместимость: конфиги без новых полей работают (значения
  по умолчанию совпадают с прежним поведением).
- Тесты ([задача 01](./01-tests.md)) проверяют все три параметра.
