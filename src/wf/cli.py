from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from wf import artifacts
from wf import git as wfgit
from wf.config import Config, config_path, load_config
from wf.engine import Engine, _fmt_tokens
from wf.locks import TaskLocked, task_lock
from wf.state import State, now_utc
from wf.workflow import Workflow
from wf.workflows import WORKFLOWS

app = typer.Typer(no_args_is_help=True, add_completion=False, help="wf — developer pipeline orchestrator")
console = Console()


def _resolve_repo(repo: Path | None) -> Path:
    return (repo or Path.cwd()).resolve()


def _get_workflow(name: str, config: Config) -> Workflow:
    wf = WORKFLOWS.get(name)
    if wf is None:
        console.print(f"[red]Неизвестный workflow:[/red] {name}")
        console.print(f"Доступные: {', '.join(WORKFLOWS) or '(нет)'}")
        raise typer.Exit(code=2)
    problems = wf.validate()
    if problems:
        for p in problems:
            console.print(f"[red]workflow '{name}' broken:[/red] {p}")
        raise typer.Exit(code=2)
    missing = sorted(wf.required_tools() - config.tools.keys())
    if missing:
        console.print(
            f"[red]workflow '{name}' требует инструменты, не настроенные в "
            f"config.tools:[/red] {', '.join(missing)}"
        )
        console.print(
            f"  добавь их в [bold]tools:[/bold] в {config_path()} "
            f"(см. примеры в комментарии у поля)"
        )
        raise typer.Exit(code=2)
    wf_spec = config.workflows.get(name)
    if wf_spec is not None:
        bad = [p for p in wf_spec.pause_before if p not in wf.phases]
        if bad:
            console.print(
                f"[red]workflow '{name}': pause_before содержит "
                f"неизвестные фазы:[/red] {', '.join(bad)}"
            )
            console.print(f"  доступные фазы: {', '.join(wf.phases)}")
            raise typer.Exit(code=2)
    return wf


def _load_state(config: Config, workflow: str, task_id: str) -> tuple[State, Path]:
    state_path = config.state_path(workflow, task_id)
    if not state_path.exists():
        console.print(f"[red]state.json не найден:[/red] {state_path}")
        console.print(f"  запусти: [bold]wf start {task_id} --workflow {workflow}[/bold]")
        raise typer.Exit(code=1)
    return State.load(state_path), state_path


@app.command()
def start(
    task_id: str = typer.Argument(..., help="Идентификатор задачи, например ABC-123"),
    workflow: str = typer.Option("coding", "--workflow", "-w", help="Имя workflow"),
    repo: Path | None = typer.Option(None, "--repo", help="Путь к репозиторию (по умолчанию cwd)"),
    base_branch: str | None = typer.Option(None, "--base-branch", help="Базовая ветка"),
) -> None:
    """Создаёт каталог задачи и стартовый state.json."""
    config = load_config()
    wf = _get_workflow(workflow, config)

    task_dir = config.task_dir(workflow, task_id)
    state_path = config.state_path(workflow, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        with task_lock(task_dir, task_id):
            if state_path.exists():
                console.print(f"[yellow]state.json уже существует:[/yellow] {state_path}")
                raise typer.Exit(code=1)

            repo_path = _resolve_repo(repo)
            try:
                base = base_branch or wfgit.current_branch(repo_path)
            except RuntimeError as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(code=1)
            now = now_utc()

            state = State(
                task_id=task_id,
                workflow=workflow,
                repo_path=repo_path,
                base_branch=base,
                current_stage=wf.start,
                iteration=1,
                max_iterations=config.defaults.max_iterations,
                created_at=now,
                updated_at=now,
            )

            config.artifacts_dir(workflow, task_id).mkdir(parents=True, exist_ok=True)
            state.save(state_path)

            console.print(f"[green]ok[/green] инициализирована задача [bold]{task_id}[/bold] (workflow={workflow})")
            console.print(f"  state: {state_path}")
            console.print(f"  artifacts: {config.artifacts_dir(workflow, task_id)}")
            console.print(f"  current_stage: [cyan]{wf.start}[/cyan]  iteration: 1")

            engine = Engine(workflow=wf, state=state, config=config, state_path=state_path, console=console)
            raise typer.Exit(code=engine.run())
    except TaskLocked as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def resume(
    task_id: str = typer.Argument(...),
    workflow: str = typer.Option("coding", "--workflow", "-w"),
) -> None:
    """Продолжить с текущей фазы (после ответов на QA или сбоя)."""
    config = load_config()
    wf = _get_workflow(workflow, config)
    state, state_path = _load_state(config, workflow, task_id)
    task_dir = config.task_dir(workflow, task_id)
    try:
        with task_lock(task_dir, task_id):
            if state.terminal == "failed":
                state.terminal = None
                state.last_error = None
                state.save(state_path)
            engine = Engine(workflow=wf, state=state, config=config, state_path=state_path, console=console)
            raise typer.Exit(code=engine.run())
    except TaskLocked as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def status(
    workflow: str | None = typer.Option(None, "--workflow", "-w", help="Фильтр по workflow"),
) -> None:
    """Показать все задачи и их состояние."""
    config = load_config()
    root = config.data_dir_path / "workflow"
    if not root.exists():
        console.print("[dim]Задач пока нет.[/dim]")
        return

    rows: list[State] = []
    workflows = [workflow] if workflow else [p.name for p in root.iterdir() if p.is_dir()]
    for wf_name in workflows:
        wf_dir = root / wf_name
        if not wf_dir.exists():
            continue
        for task_dir in sorted(wf_dir.iterdir()):
            state_file = task_dir / "state.json"
            if state_file.exists():
                try:
                    rows.append(State.load(state_file))
                except Exception as e:
                    console.print(f"[red]ошибка чтения[/red] {state_file}: {e}")

    if not rows:
        console.print("[dim]Задач пока нет.[/dim]")
        return

    table = Table(title="wf — задачи", show_lines=False)
    table.add_column("task_id", style="bold")
    table.add_column("workflow")
    table.add_column("stage", style="cyan")
    table.add_column("iter", justify="right")
    table.add_column("branch")
    table.add_column("terminal")
    table.add_column("tok in", justify="right")
    table.add_column("tok out", justify="right")
    table.add_column("cost $", justify="right")
    table.add_column("note", style="dim")
    table.add_column("updated", style="dim")
    for s in rows:
        table.add_row(
            s.task_id,
            s.workflow,
            s.current_stage,
            f"{s.iteration}/{s.max_iterations}",
            s.branch or "-",
            s.terminal or "-",
            _fmt_tokens(s.total_input_tokens),
            _fmt_tokens(s.total_output_tokens),
            f"{s.total_cost_usd:.4f}",
            (s.last_error or "")[:32],
            s.updated_at.astimezone().strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)


@app.command()
def remove(
    task_id: str = typer.Argument(..., help="Идентификатор задачи, например ABC-123"),
    workflow: str = typer.Option("coding", "--workflow", "-w", help="Имя workflow"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Не спрашивать подтверждение"),
) -> None:
    """Удалить все артефакты задачи и связанные git worktree."""
    config = load_config()
    task_dir = config.task_dir(workflow, task_id)
    state_path = config.state_path(workflow, task_id)

    if not task_dir.exists():
        console.print(f"[yellow]каталог задачи не найден:[/yellow] {task_dir}")
        raise typer.Exit(code=1)

    state: State | None = None
    if state_path.exists():
        try:
            state = State.load(state_path)
        except Exception as e:
            console.print(f"[yellow]не удалось прочитать state.json:[/yellow] {e}")

    console.print(f"Будут удалены данные задачи [bold]{task_id}[/bold] (workflow={workflow}):")
    console.print(f"  task_dir: {task_dir}")
    if state and state.worktree:
        console.print(f"  worktree: {state.worktree}  (repo: {state.repo_path}, branch: {state.branch or '-'})")

    if not yes:
        confirm = typer.confirm("Подтвердить удаление?", default=False)
        if not confirm:
            console.print("[dim]отменено[/dim]")
            raise typer.Exit(code=1)

    if state and state.worktree and state.repo_path:
        try:
            wfgit.remove_worktree(state.repo_path, state.worktree)
            console.print(f"[green]ok[/green] worktree удалён: {state.worktree}")
        except Exception as e:
            console.print(f"[yellow]не удалось удалить worktree[/yellow] {state.worktree}: {e}")

    shutil.rmtree(task_dir, ignore_errors=False)
    console.print(f"[green]ok[/green] задача [bold]{task_id}[/bold] удалена ({task_dir})")


@app.command()
def logs(
    task_id: str = typer.Argument(..., help="Идентификатор задачи, например ABC-123"),
    workflow: str = typer.Option("coding", "--workflow", "-w", help="Имя workflow"),
    raw: bool = typer.Option(False, "--raw", help="Показать events.jsonl вместо summary.md"),
    tail: int | None = typer.Option(None, "--tail", help="Печатать только последние N строк"),
) -> None:
    """Показать summary.md (или events.jsonl с --raw) для задачи."""
    config = load_config()
    state, _ = _load_state(config, workflow, task_id)

    path = artifacts.events_path(config, state) if raw else artifacts.summary_path(config, state)
    if not path.exists():
        console.print(f"[red]файл не найден:[/red] {path}")
        raise typer.Exit(code=1)

    console.print(f"── {path} ──")
    content = path.read_text(encoding="utf-8")
    if tail is not None and tail > 0:
        lines = content.splitlines()
        content = "\n".join(lines[-tail:])
    typer.echo(content)


if __name__ == "__main__":
    app()
