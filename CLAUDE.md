# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`wf` is a Python CLI that drives a software task through a linear pipeline of phases
(`explore → generate_branch_name → prepare → plan → implement → review → mr → notify →
cleanup_worktree`) with a backward edge from `review` to `plan` when review reports
critical issues. The intent and phase contracts are specified in
[docs/phases.md](docs/phases.md), the state schema in [docs/State.md](docs/State.md),
and the architecture in [docs/plan.md](docs/plan.md). Phase docs and many comments are
in Russian; keep that style when editing them.

Phases that need an LLM call `claude-agent-sdk` (which itself talks to `claude`). Phases
that need external systems (task tracker fetch, diff, MR creation, chat notifications) shell
out to user-defined binaries declared under `tools:` in the YAML config.

Implementation status: all phases (`explore`, `generate_branch_name`, `prepare`, `plan`,
`implement`, `review`, `mr`, `notify`, `cleanup_worktree`) are functional.

## Common commands

Defined in [Makefile](Makefile) — uses `uv`:

- `make dev` — create local `.venv` with editable install (use for development).
- `make install` / `make reinstall` — install/upgrade `wf` as a `uv` tool (puts `wf` on PATH).
- `make uninstall` / `make clean` — remove the tool / local build artifacts.

There is **no test suite, linter, or formatter configured** in [pyproject.toml](pyproject.toml).
Do not invent `pytest`/`ruff`/`mypy` commands — if you add tooling, add it to pyproject and
the Makefile in the same change.

CLI entry point is `wf.cli:app` (typer). Key commands:

- `wf start <TASK_ID> [--workflow coding] [--repo PATH] [--base-branch master]`
  — create state and immediately run the engine to completion or first blocker.
- `wf resume <TASK_ID> [--workflow coding]` — resume after QA answers or failure. Clears
  `terminal=failed` automatically.
- `wf status [--workflow coding]` — table of all tasks under `data_dir`.
- `wf remove <TASK_ID> [--workflow coding] [-y]` — delete the task's artifacts directory
  **and** its git worktree (via `git worktree remove --force` + `prune`). Asks for
  confirmation unless `-y`.

Engine exit codes: `0` terminal, `1` failed, `2` awaiting human input (QA),
`3` paused (фаза вернула `paused` — нужен ручной чекпоинт перед `wf resume`).

## Architecture

### Data layout (on disk)

Config and data live in two separate places:

- **Config** — `~/.wf/config.yaml` (or `$WF_CONFIG` if set). Auto-created from packaged
  [src/wf/default-config.yaml](src/wf/default-config.yaml) on the first `wf` invocation.
  The path is fixed and never derived from another env var.
- **Data** — directory pointed to by the `data_dir:` field in that config, default
  `~/wf`. All per-task state and artifacts live here, **not** in the working repo or
  the worktree, so `git status` stays clean. Artifacts are produced into:

```
<data_dir>/workflow/<workflow>/<task_id>/
├── state.json                # State model, atomic write via tmpfile + os.replace
├── events.jsonl              # append-only event stream for all phases
├── summary.md                # rendered task summary, regenerated after each phase
└── artifacts/
    ├── task.md
    ├── plan_<iter>.md
    ├── plan_qa_<iter>.md.template   # written by plan phase when it needs answers
    ├── plan_qa_<iter>.md            # filled by user (rename .template → this)
    ├── review_<iter>.md, feedback_<iter>.md
    └── worktree/<branch>/    # actual git worktree lives under task_dir
```

All artifact paths come from [artifacts.py](src/wf/artifacts.py) helpers — never hardcode.

### Core modules

- [src/wf/cli.py](src/wf/cli.py) — typer commands, wires Config + Workflow + State into Engine.
- [src/wf/engine.py](src/wf/engine.py) — phase loop. For each step: checks
  `required_artifacts` + `is_ready`, runs the phase, on `ok` appends a `PhaseRun` and asks
  the workflow's transition function for the next phase; on `paused` does the same
  transition but then exits with code 3 so the user can manually verify before
  `wf resume`; on `needs_human_input` it stops and tells the user where to write answers;
  on `failed` it sets `terminal=failed`.
- [src/wf/workflow.py](src/wf/workflow.py) + [src/wf/workflows/coding.py](src/wf/workflows/coding.py)
  — a `Workflow` is `{phases, start, transitions}`. Transitions are pure functions
  `State -> next_phase_name | None`. The `review → plan` loop and `max_iterations` cap
  live in `review_next` in [coding.py](src/wf/workflows/coding.py).
- [src/wf/state.py](src/wf/state.py) — pydantic v2 `State` model; `save()` is atomic
  (tmp file + `fsync` + `os.replace`). `current_stage` and `iteration` are the source of
  truth for "where are we".
- [src/wf/phases/base.py](src/wf/phases/base.py) — `Phase` protocol and `PhaseResult`
  (`ok` / `needs_human_input(qa_file, qa_answers_path)` / `failed` / `paused(reason)`).
  New phases implement this protocol and are registered in
  [src/wf/phases/__init__.py](src/wf/phases/__init__.py).
- [src/wf/claude.py](src/wf/claude.py) — the only place that talks to `claude-agent-sdk`.
  `run_phase()` renders a jinja2 template from
  [src/wf/templates/](src/wf/templates/) and streams SDK messages
  (assistant text, tool_use, tool_result, thinking length, system, result)
  as JSON-line events into the task-wide `events.jsonl`. Returns assistant
  text and usage data — XML-block parsing happens in the phase. По умолчанию
  фазы получают пресетный системный промпт Claude Code
  (`{"type": "preset", "preset": "claude_code", "append": …,
  "exclude_dynamic_sections": True}`): базовый `append`
  (`_SYSTEM_APPEND_BASE`) гасит интерактивные ожидания пресета — «пользователя
  нет, финальный ответ парсится программой»; пофазное дополнение передаётся
  через `system_prompt_append` (см. `plan`), а `system_prompt=""` даёт
  полностью пустой промпт (так делает `generate_branch_name`).
- [src/wf/run_log.py](src/wf/run_log.py) — `EventsWriter` (append-only jsonl
  writer used by the engine) and `rerender_summary(events_path, summary_path,
  state)` which the engine calls after every phase to regenerate
  `<task_dir>/summary.md` from the full event stream.
- [src/wf/runner.py](src/wf/runner.py) — `run_tool(name, ctx)` renders `tools.<name>.cmd`
  via jinja2 and shells out with `subprocess.run` (no shell=True; `shlex.split`).
  `run_shell()` is the lower-level helper used by `git.py`.
- [src/wf/git.py](src/wf/git.py) — worktree operations. Worktrees are created at
  `<task_dir>/worktree/<branch>` (under `data_dir`, not next to the repo as the older
  plan.md describes). `copy_claude_dir` only copies the `CLAUDE_ALLOW_LIST` entries
  (`agents`, `commands`, `skills`, `CLAUDE.md`) — don't copy the whole `.claude/`
  directory, settings would leak.
- [src/wf/artifacts.py](src/wf/artifacts.py) — single source of truth for artifact paths.
  Always go through these helpers; don't hardcode filenames in phases.
- [src/wf/hooks.py](src/wf/hooks.py) — `make_readonly_git_hook(phase_name)` builds a
  `claude-agent-sdk` `PreToolUse` hook that inspects every Bash command, splits on shell
  metacharacters, and blocks any `git` invocation whose first positional subcommand isn't
  in `_GIT_READONLY_SUBCOMMANDS` (status/diff/log/show/blame/ls-files/etc). Used by
  `implement` and `review` to let the LLM read repo state via `Bash` but prevent it from
  committing/checking-out/resetting — the engine controls those.

### MCP-серверы для LLM-фаз

LLM-фазам можно дать доступ к MCP-серверам. Серверы описываются один раз в
верхнеуровневом `mcp_servers:` конфига (форма записи повторяет блок `mcpServers`
из `~/.claude.json` — stdio: `command`/`args`/`env`; http|sse: `url`/`headers`),
а включаются пофазно через `claude.phase_mcp_servers: {<phase>: [<server>, …]}`.
По умолчанию ни одна фаза MCP не получает.

`Config.mcp_for_phase(phase)` в [config.py](src/wf/config.py) резолвит это в
`(mcp_servers_dict, ["mcp__<server>__*", …], missing)`; фаза передаёт словарь в
`claude.run_phase(..., mcp_servers=…)` и добавляет паттерны в `allowed_tools`
(имена MCP-инструментов — `mcp__<server>__<tool>`). Опираться на
`setting_sources` для загрузки MCP **нельзя**: так грузится только проектный
`.mcp.json`, а user-scope серверы из `~/.claude.json` — нет, поэтому сервер
передаётся в SDK явно. Пока подключено только к `explore`
([phases/explore.py](src/wf/phases/explore.py)) — для новой фазы повтори тот же
резолв и проброс.

### LLM output protocol

Phases that call the LLM parse structured blocks from `summary.text`, not files. Each
template instructs the model to wrap each output in an `<xml_tag>…</xml_tag>` block;
the phase declares a `[BlockSpec(...)]` schema and calls
[`markers.parse_blocks`](src/wf/markers.py). Order of blocks in the LLM reply does not
matter — each tag is looked up independently. Current blocks:

| Phase | Blocks (see [phases/](src/wf/phases/)) |
|---|---|
| `explore` | `<task_url>`, `<task_title>`, `<additional_info>` (all optional) + required `<main_task_info>` |
| `plan` | `<plan_ready>` *xor* `<plan_qa>` (both optional in schema; phase rejects the "both present" case) |
| `implement` | `<commit_message>` (subject only, `allow_empty=True`; falls back if missing or empty) |
| `review` | `<review>` (required) + optional `<feedback>` (its presence sets `state.last_review_has_feedback`, which drives the `review → plan` loop) |

When adding an LLM phase, prefer this XML-tag pattern over writing files from the
prompt — it's easier to validate and less fragile than relying on the model to use
the `Write` tool.

### Human-in-the-loop QA flow

When a phase needs clarifications it writes a questions file and returns
`needs_human_input(qa_file, qa_answers_path)`. The engine prints both paths and exits
with code 2. User edits the answers file, runs `wf resume`, and the phase reads it on
re-entry.

The canonical implementation is now [phases/plan.py](src/wf/phases/plan.py), not
`explore.py` (explore currently has no QA loop). Plan uses a single-file rename
convention: it writes `plan_qa_<iter>.md.template` with questions, asks the user to fill
it and rename to `plan_qa_<iter>.md`, then on re-entry sees the renamed file and proceeds.
Don't add a separate `*_qa_answers.md` file for new phases — match this pattern.

### The `implement` phase contract

[phases/implement.py](src/wf/phases/implement.py) is the only phase that mutates the
worktree. It:

1. Snapshots `HEAD` before invoking the LLM.
2. Runs the LLM with `Read/Write/Edit/Glob/Grep/Bash` and the read-only-git hook from
   [hooks.py](src/wf/hooks.py) — so the model can inspect repo state but **cannot** commit.
3. After the run, requires `git status --porcelain` to be non-empty (or fails:
   "Claude не внёс изменений").
4. Extracts `<commit_message>` (≤72 chars, sanitized) — falls back to
   `implement plan (iteration N)` with a yellow warning if absent or empty.
5. Runs `git add -A && git commit -m <subject>`, then asserts the worktree is clean,
   `HEAD` moved, and `commits_ahead(worktree, base_ref) > 0` — otherwise fails so a
   downstream MR can't be empty.

If you change this contract, also update the template at
[templates/implement.md.j2](src/wf/templates/implement.md.j2).

### Adding a new phase

1. Implement the `Phase` protocol — see [phases/plan.py](src/wf/phases/plan.py) for the
   canonical LLM + QA pattern, or [phases/explore.py](src/wf/phases/explore.py) for a
   simpler LLM-only example.
2. Register it in [phases/__init__.py](src/wf/phases/__init__.py).
3. Wire it into the workflow's `phases` + `transitions` in
   [workflows/coding.py](src/wf/workflows/coding.py) (or a new workflow module).
4. If it produces files, add helpers to [artifacts.py](src/wf/artifacts.py) rather than
   constructing paths inline.
5. If it uses an LLM, add a jinja2 template under
   [src/wf/templates/](src/wf/templates/) and call `claude.run_phase`. Pass
   `events=ctx.events`, `phase=self.name`, `iter=state.iteration` — the
   engine owns the events writer and rerenders `summary.md` from it.
   Prefer parsing `<xml_tag>…</xml_tag>` blocks from `summary.text` via
   [`markers.parse_blocks`](src/wf/markers.py) over relying on `expected_outputs` files
   (see the LLM output protocol table above). Declare a `BlockSpec` schema per phase
   and let the parser raise `BlocksError` — translate that into `PhaseResult.failed`
   with a `; см. summary.md (## <phase> #<iter>)` suffix so the same UX as existing
   phases is preserved. If the phase must let the LLM run `Bash` but shouldn't mutate
   git, pass the read-only hook from [hooks.py](src/wf/hooks.py) as
   `hooks={"PreToolUse": [HookMatcher(matcher="Bash", …)]}`.

### Adding a new workflow

Create a module under [src/wf/workflows/](src/wf/workflows/) exporting a `Workflow`
instance and add it to `WORKFLOWS` in `workflows/__init__.py`. `wf start --workflow <name>`
will pick it up automatically; `Workflow.validate()` checks that `start` and every
transition reference a registered phase.

Transitions — **pure functions** `State -> str | RepeatAt | None` и не должны
мутировать `state` (тесты вызывают transition дважды с одним и тем же state и
ожидают одинаковый результат). Возможные возвращаемые значения:

- `"<phase_name>"` — обычный переход на указанную фазу;
- `None` — workflow завершён, движок выставит `state.terminal = "done"`;
- `RepeatAt("<phase_name>")` (импортируется из `wf.workflow`) — повторить
  фазу со следующей итерации; движок сам увеличит `state.iteration` и
  выставит `current_stage`. Граничные условия (например,
  `iteration >= max_iterations`) проверяет сама transition, возвращая `None`
  вместо `RepeatAt`. Канонический пример — `review_next` в
  [workflows/coding.py](src/wf/workflows/coding.py).

`Workflow.validate()` не проверяет, что target-фаза у `RepeatAt` зарегистрирована
(это требует вызова transition с реальным `State`); такая ошибка ловится
движком в рантайме.

## Conventions

- Python 3.12+, typed (`from __future__ import annotations` everywhere).
- Pydantic v2 for models; `model_validate` / `model_dump_json` (not v1 spellings).
- Paths are `pathlib.Path`, not strings.
- User-facing console output goes through `rich.Console`; per-task LLM/tool
  runs append to `<task_dir>/events.jsonl` via `ctx.events`, and
  `<task_dir>/summary.md` is regenerated by the engine after every phase.
- Russian is used for docs and user-facing CLI messages — match existing tone when
  editing those; code identifiers and comments-on-logic stay in English.
- LLM-фазы (`explore`, `plan`, `implement`, `review`) запускаются с
  `setting_sources=["project", "user"]`, поэтому в контекст модели подтягивается
  не только проектный `CLAUDE.md`, но и закоммиченный `.claude/settings.json` /
  `.claude/settings.local.json` из репо, если такие есть. Это сознательная
  пользовательская конвенция: закоммитил настройки — получи их эффект на
  поведение фаз.
