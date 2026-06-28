# 08. Гибкий парсер URL для tools.create_mr

**Приоритет:** P2

## Описание

В [phases/mr.py](../src/wf/phases/mr.py) функция `_parse_url` требует,
чтобы URL мерж-реквеста был последней непустой строкой stdout:

```python
def _parse_url(stdout: str) -> str | None:
    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            return line
        return None
```

Это жёсткое требование к интеграции:
- `gh pr create --json url --jq .url` — пишет URL, но `gh` может
  добавить служебные строки;
- `glab mr create -o json` — пишет JSON, URL внутри поля;
- многие CLI добавляют ANSI escape-коды (`\x1b[...m`), и `startswith("http")`
  ломается.

Пользователю приходится оборачивать каждый CLI в shell-скрипт.

## Что сделать

- Расширить `ToolSpec` в [config.py](../src/wf/config.py) опциональным
  полем `output`:
  ```python
  class ToolOutputSpec(BaseModel):
      format: Literal["last_line_url", "json", "regex"] = "last_line_url"
      jsonpath: str | None = None   # для format=json, напр. "$.web_url"
      pattern: str | None = None    # для format=regex, с именованной группой `url`

  class ToolSpec(BaseModel):
      cmd: str
      output: ToolOutputSpec = Field(default_factory=ToolOutputSpec)
  ```
- Создать `src/wf/output_parsers.py` с функцией:
  ```python
  def parse_tool_output(spec: ToolOutputSpec, stdout: str) -> str | None: ...
  ```
  Перед парсингом стрипать ANSI-escape-последовательности (regex
  `\x1b\[[0-9;]*[A-Za-z]`).
- Для `format=json` использовать минимальный jsonpath (Python `jsonpath-ng`
  как новая зависимость, либо самописная поддержка `$.foo.bar`).
- Для `format=regex` — `re.search(pattern)` с группой `url`.
- В [phases/mr.py](../src/wf/phases/mr.py) использовать `parse_tool_output`
  вместо локального `_parse_url`.
- Обновить [default-config.yaml](../src/wf/default-config.yaml) с
  примерами для `gh`, `glab`, текстового CLI.
- Тесты в составе [задачи 01](./01-tests.md):
  - `gh pr create --json url --jq .url` (text);
  - `glab mr create -o json` (json + jsonpath);
  - вывод с ANSI-цветами;
  - кастомный регекс.

## Критерии приёмки

- Три типичных CLI (`gh`, `glab` JSON, текстовый) работают без shell-обёрток.
- Обратная совместимость: конфиги без поля `output` продолжают работать
  как раньше (`format=last_line_url`).
