"""Раунды ручных правок поверх работы агента (`wf open` / `wf push` / `wf rebase`).

Раунд открывается на любой паузе пайплайна. В режиме ревью (по умолчанию) HEAD
worktree-а отсоединяется и мягко сдвигается на точку ответвления, поэтому в IDE
вся работа на ветке видна как staged, а правки раунда — как unstaged. В режиме
ветки (`wf open --branch`) HEAD остаётся на ветке, чтобы с её историей можно
было работать обычным git; `wf rebase` в этом режиме сам делает rebase на
свежий base_branch. Claude в этом worktree получает контекст задачи через
SessionStart-хук и не может мутировать git. Закрытие раунда возвращает HEAD на
ветку, коммитит дельту и пушит, если ветка уже на remote; переписанную историю —
с `--force-with-lease`.
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.markup import escape

from wf import artifacts, git, run_log
from wf.claude import render_template, run_phase
from wf.config import Config
from wf.markers import BlockSpec, commit_subject, parse_blocks
from wf.run_log import EventsWriter
from wf.runner import run_shell, run_tool, tool_context
from wf.state import FixMode, FixRound, State, now_utc

_PHASE = "fix_round"
# Отдельная секция в summary.md и ключ для claude.phase_models.
_COMMIT_PHASE = "commit_message"
_COMMIT_SCHEMA = [BlockSpec("commit_message")]
_SUBJECT_MAX = 72
_DIFF_INLINE = 30_000
_BRANCH_COMMITS_INLINE = 30
_SETTINGS = Path(".claude") / "settings.local.json"
# Пока агент пишет и ревьюит код, рабочей копией владеет фаза coding.
_AGENT_STAGES = frozenset({"coding", "review"})
_MR_COMMENTS_INLINE = 4_000
_TASK_INLINE = 3_000
_MODE_LABEL: dict[FixMode, str] = {"review": "ревью", "branch": "ветка"}


class FixRoundError(RuntimeError):
    pass


def _worktree_path(config: Config, state: State) -> Path:
    if state.worktree:
        return Path(state.worktree)
    assert state.branch
    return config.task_dir(state.workflow, state.task_id) / "worktree" / state.branch


def open_round(
    config: Config,
    state: State,
    state_path: Path,
    console: Console,
    mode: FixMode = "review",
) -> Path:
    """Открывает раунд (переоткрывает или переключает режим текущего), возвращает worktree."""
    _check_can_open(state)
    if mode == "branch":
        try:
            git.fetch(Path(state.repo_path))
        except RuntimeError as e:
            console.print(f"[yellow]git fetch не удался — работаю с локальными ref-ами:[/yellow] {e}")
    wt = _start_round(config, state, state_path, console, mode)
    brief = _prepare_session(config, state, wt, console)
    _announce(config, state, wt, brief, console)
    return wt


def rebase(config: Config, state: State, state_path: Path, console: Console) -> bool:
    """`wf rebase`: rebase ветки на свежий base_branch в раунде режима ветки.

    Если до команды не было ни открытого раунда, ни незакоммиченных правок, после
    rebase раунд сразу закрывается и ветка пушится (force-with-lease). Иначе раунд
    остаётся открытым в прежнем режиме — закрывает его `wf push` / `wf resume`.
    Возвращает False, если rebase остановился на конфликтах: тогда раунд остаётся
    открытым в режиме ветки и worktree открывается в IDE.
    """
    _check_can_open(state)
    assert state.branch
    repo = Path(state.repo_path)
    git.fetch(repo)
    onto = git.resolve_base_ref(repo, state.base_branch)
    ref = f"refs/heads/{state.branch}"
    if git.branch_exists(repo, state.branch) and git.is_ancestor(repo, onto, ref):
        console.print(f"[dim]ветка {state.branch} уже основана на {onto} — rebase не нужен[/dim]")
        remote = git.remote_sha(repo, state.branch)
        if state.fix_round is not None or (remote and remote != git.rev_parse(repo, ref)):
            console.print(f"  запушить ветку: [bold]wf push {state.task_id}[/bold]")
        return True

    prev = state.fix_round.mode if state.fix_round else None
    wt = _start_round(config, state, state_path, console, "branch")
    _require_idle(wt)
    rnd = state.fix_round
    assert rnd is not None
    auto_close = prev is None and not git.porcelain(wt)
    before = git.rev_parse(wt)
    console.print(f"rebase [cyan]{state.branch}[/cyan] на {onto}")
    if not git.rebase(wt, onto):
        rnd.notes.append(f"rebase на {onto} (с {before[:12]}) остановился на конфликтах")
        state.save(state_path)
        brief = _prepare_session(config, state, wt, console, rebase_onto=onto)
        console.print(
            f"[yellow]конфликты:[/yellow] {', '.join(git.conflicted_files(wt)) or '?'}"
        )
        console.print(
            "  разреши их, затем git add + git rebase --continue в worktree "
            "(передумал — git rebase --abort)"
        )
        _announce(config, state, wt, brief, console)
        return False

    after = git.rev_parse(wt)
    rnd.notes.append(f"rebase на {onto}: {before[:12]} → {after[:12]}")
    state.save(state_path)
    console.print(f"[green]rebase[/green] {before[:12]} → {after[:12]}")
    if auto_close:
        finish(config, state, state_path, console)
        return True

    stash_conflicts = git.conflicted_files(wt)
    if stash_conflicts:
        console.print(
            "[yellow]незакоммиченные правки не легли поверх rebase[/yellow] "
            f"(копия — в git stash): {', '.join(stash_conflicts)}"
        )
    elif prev == "review":
        _start_round(config, state, state_path, console, "review")
    _prepare_session(config, state, wt, console)
    console.print(
        f"раунд правок #{rnd.number} открыт (режим {_MODE_LABEL[rnd.mode]}); закончить: "
        f"[bold]wf push {state.task_id}[/bold] или [bold]wf resume {state.task_id}[/bold]"
    )
    return True


def finish(
    config: Config,
    state: State,
    state_path: Path,
    console: Console,
    message: str | None = None,
) -> None:
    """Закрывает открытый раунд (если есть), коммитит изменения и пушит ветку.

    Идемпотентно: повторный вызов после упавшего коммита/пуша доделывает
    оставшееся.
    """
    if not state.worktree or not Path(state.worktree).exists():
        if state.fix_round is not None:
            state.fix_round = None
            state.save(state_path)
        raise FixRoundError("worktree нет — нечего коммитить (открой раунд: wf open)")
    if not state.branch:
        raise FixRoundError("в state нет ветки")
    wt = Path(state.worktree)
    _require_idle(wt)
    conflicts = git.conflicted_files(wt)
    if conflicts:
        raise FixRoundError(f"в worktree неразрешённые конфликты: {', '.join(conflicts)}")
    rnd = state.fix_round
    notes: list[str] = []

    if rnd is not None and rnd.mode == "review":
        git.leave_fix_mode(wt, rnd.head, state.branch)
    _require_on_branch(wt, state.branch)
    if rnd is not None:
        _remove_session_settings(config, state, wt)
        state.fix_round = None
        notes.append(
            f"раунд открыт {rnd.opened_at.astimezone():%Y-%m-%d %H:%M}, "
            f"режим {_MODE_LABEL[rnd.mode]}, база {rnd.base[:12]}"
        )
        notes.extend(rnd.notes)
    old_base = state.base_commit
    try:
        new_base = _refresh_base(wt, state)
    except RuntimeError:
        new_base = old_base
    if old_base and new_base != old_base:
        notes.append(f"точка ответвления {old_base[:12]} → {new_base[:12]}")
    state.save(state_path)

    sha: str | None = None
    subject = ""
    if git.stage_all(wt):
        iter_ = rnd.number if rnd else state.fix_rounds
        msg = message or _generate_message(config, state, wt, iter_, console)
        if not msg:
            what = f"review fixes (round {rnd.number})" if rnd else "manual fixes"
            msg = f"[{state.task_id}] {what}"
        sha = git.commit_all(wt, msg)
        subject = msg.strip().splitlines()[0]
    if sha:
        stat = git.shortstat(wt, sha)
        notes.append(f"коммит {sha[:12]} «{subject}»: {stat}")
        console.print(f"[green]коммит[/green] {sha[:12]}  {escape(subject)}")
        console.print(f"  [dim]{stat}[/dim]")
    else:
        notes.append("изменений нет")
        console.print("[dim]изменений нет[/dim]")

    pushed = False
    try:
        pushed = _push(state, wt, notes, console)
    except RuntimeError as e:
        notes.append(f"push не удался: {str(e).splitlines()[0]}")
        raise
    finally:
        if rnd is not None or sha or pushed:
            _log(config, state, rnd, notes)
    if state.push_lease is not None:
        state.push_lease = None
        state.save(state_path)


def _generate_message(
    config: Config, state: State, wt: Path, iter_: int, console: Console
) -> str | None:
    """Сообщение коммита по staged-дельте от Claude; None — не вышло, нужен fallback."""
    prefix = f"[{state.task_id}] "
    max_len = max(_SUBJECT_MAX - len(prefix), 40)
    console.print("[dim]сообщение коммита: спрашиваю Claude…[/dim]")
    started = now_utc()
    subject: str | None = None
    error = ""
    with EventsWriter(artifacts.events_path(config, state)) as events:
        events.write(_COMMIT_PHASE, iter_, "phase_start")
        try:
            diff = git.staged_diff(wt)
            commits = git.log_oneline(wt, f"{state.base_commit}..HEAD") if state.base_commit else ""
            summary = run_phase(
                template_name="fix_commit_message.md.j2",
                template_ctx={
                    "task_id": state.task_id,
                    "task_title": state.task_title or "",
                    "commits": "\n".join(commits.splitlines()[:_BRANCH_COMMITS_INLINE]),
                    "stat": git.staged_diff(wt, stat=True),
                    "diff": diff[:_DIFF_INLINE],
                    "truncated": len(diff) > _DIFF_INLINE,
                    "max_len": max_len,
                },
                cwd=wt,
                config=config,
                events=events,
                phase=_COMMIT_PHASE,
                iter=iter_,
                tools=[],  # всё нужное уже в промпте
                setting_sources=["user"],
                system_prompt="",
            )
            blocks = parse_blocks(summary.text, _COMMIT_SCHEMA)
            subject = commit_subject(blocks["commit_message"], max_len)
        # Сообщение — не повод не закоммитить: любой сбой (нет claude, лимиты,
        # кривой ответ) уходит в стандартное сообщение.
        except Exception as e:
            error = str(e).splitlines()[0] if str(e) else type(e).__name__
        if subject is None and not error:
            error = "пустой <commit_message>"
        if error:
            events.write(_COMMIT_PHASE, iter_, "phase_note", text=f"не сгенерировано: {error}")
            console.print(
                f"[yellow]сообщение коммита не сгенерировано ({escape(error)}) — "
                "беру стандартное[/yellow]"
            )
        events.write(
            _COMMIT_PHASE,
            iter_,
            "phase_end",
            exit="failed" if error else "ok",
            duration_ms=int((now_utc() - started).total_seconds() * 1000),
        )
    return prefix + subject if subject else None


def _push(state: State, wt: Path, notes: list[str], console: Console) -> bool:
    assert state.branch
    remote = git.remote_sha(wt, state.branch)
    if remote is not None and not git.is_ancestor(wt, remote, f"refs/heads/{state.branch}"):
        # История переписана (или на remote есть чужие коммиты). Force-push только
        # против sha, который wf видел, когда локальная ветка содержала весь remote.
        if state.push_lease != remote:
            raise FixRoundError(
                f"ветка {state.branch} разошлась с origin/{state.branch}: force-push wf делает "
                "только после rebase в раунде (wf open --branch / wf rebase) и только если "
                "на remote с тех пор ничего не появилось — подтяни remote "
                f"(git pull --rebase в {wt}) или запушь вручную"
            )
        git.push(wt, state.branch, lease=remote)
        notes.append(f"force-push origin/{state.branch} (было {remote[:12]})")
        console.print(f"[green]force-push[/green] origin/{state.branch}")
        return True

    ahead = git.unpushed_commits(wt, state.branch)
    if ahead is None and state.mr_url:
        ahead = 1
    if ahead:
        git.push(wt, state.branch)
        notes.append(f"push origin/{state.branch}")
        console.print(f"[green]push[/green] origin/{state.branch}")
        return True
    if ahead is None:
        console.print("[dim]ветки ещё нет на remote — её запушит фаза mr[/dim]")
    return False


def _check_can_open(state: State) -> None:
    if not state.branch:
        raise FixRoundError("ветка ещё не создана — правки возможны после фазы prepare")
    if state.current_stage in _AGENT_STAGES and state.terminal != "done":
        raise FixRoundError(
            f"агент ещё работает над кодом (стадия {state.current_stage}) — "
            "раунд правок можно открыть на следующей паузе"
        )


def _start_round(
    config: Config, state: State, state_path: Path, console: Console, mode: FixMode
) -> Path:
    """Готовит worktree и открывает раунд в режиме `mode` или переключает в него текущий."""
    assert state.branch
    repo = Path(state.repo_path)
    wt = _worktree_path(config, state)
    if git.ensure_worktree(repo, state.branch, wt):
        git.copy_claude_dir(repo, wt)
        console.print(f"[green]worktree создан заново:[/green] {wt}")
        # Раунд жил в удалённом worktree — открываем новый.
        state.fix_round = None
        state.worktree = wt
        state.save(state_path)
    state.worktree = wt

    rnd = state.fix_round
    if rnd is None:
        _require_idle(wt)
        _require_on_branch(wt, state.branch)
        base = _refresh_base(wt, state)
        if mode == "branch":
            state.push_lease = _lease(wt, state.branch)
            head = git.rev_parse(wt)
        else:
            head = _enter_review(wt, base, state.branch)
        state.fix_rounds += 1
        state.fix_round = FixRound(
            number=state.fix_rounds, mode=mode, head=head, base=base, opened_at=now_utc()
        )
    elif rnd.mode != mode:
        _require_idle(wt)
        if mode == "branch":
            state.push_lease = _lease(wt, state.branch)
            git.leave_fix_mode(wt, rnd.head, state.branch)
        else:
            _require_on_branch(wt, state.branch)
            rnd.base = _refresh_base(wt, state)
            rnd.head = _enter_review(wt, rnd.base, state.branch)
        rnd.mode = mode
        console.print(f"раунд правок #{rnd.number}: режим [cyan]{_MODE_LABEL[mode]}[/cyan]")
    state.save(state_path)
    return wt


def _require_idle(wt: Path) -> None:
    op = git.operation_in_progress(wt)
    if op:
        raise FixRoundError(
            f"в {wt} не завершён git {op} — доведи его (git {op} --continue) "
            f"или отмени (git {op} --abort)"
        )


def _require_on_branch(wt: Path, branch: str) -> None:
    current = git.attached_branch(wt)
    if current != branch:
        raise FixRoundError(
            f"в worktree HEAD не на ветке '{branch}' ({current or 'detached'}) — "
            f"сделай git checkout {branch} в {wt}"
        )


def _refresh_base(wt: Path, state: State) -> str:
    """Точка ответвления ветки; обновляет и `state.base_commit` — после rebase она другая."""
    state.base_commit = git.fork_point(wt, state.base_branch, state.base_commit)
    return state.base_commit


def _lease(wt: Path, branch: str) -> str | None:
    """sha origin/<branch>, против которого после rebase можно будет сделать force-push."""
    remote = git.remote_sha(wt, branch)
    if remote is not None and not git.is_ancestor(wt, remote, f"refs/heads/{branch}"):
        raise FixRoundError(
            f"на origin/{branch} есть коммиты, которых нет в локальной ветке — "
            f"подтяни их (git pull --rebase в {wt}) и повтори"
        )
    return remote


def _enter_review(wt: Path, base: str, branch: str) -> str:
    try:
        return git.enter_fix_mode(wt, base)
    except RuntimeError:
        run_shell(["git", "-C", str(wt), "checkout", "-q", branch], check=False)
        raise


def _prepare_session(
    config: Config, state: State, wt: Path, console: Console, rebase_onto: str = ""
) -> Path:
    brief = _write_brief(config, state, wt, console, rebase_onto)
    _install_session_settings(config, state, wt, brief)
    return brief


def _announce(config: Config, state: State, wt: Path, brief: Path, console: Console) -> None:
    rnd = state.fix_round
    assert rnd is not None
    console.print(
        f"[green]раунд правок #{rnd.number}[/green] {state.task_id}  "
        f"ветка [cyan]{state.branch}[/cyan]  режим [cyan]{_MODE_LABEL[rnd.mode]}[/cyan]"
    )
    console.print(f"  worktree: {wt}")
    if rnd.mode == "review":
        console.print("  Source Control: staged — всё, что уже на ветке; changes — правки раунда")
    else:
        console.print(
            "  HEAD на ветке: rebase и прочий git — руками; переписанную историю "
            "wf запушит с --force-with-lease"
        )
    console.print(f"  контекст для Claude: {brief}")
    _open_ide(config, state, wt, console)
    console.print(
        f"  закончить: [bold]wf push {state.task_id}[/bold] (коммит + push, пайплайн стоит) "
        f"или [bold]wf resume {state.task_id}[/bold] (коммит + дальше по пайплайну)"
    )


def _log(config: Config, state: State, rnd: FixRound | None, notes: list[str]) -> None:
    events_path = artifacts.events_path(config, state)
    iter_ = rnd.number if rnd else state.fix_rounds
    duration_ms = int((now_utc() - rnd.opened_at).total_seconds() * 1000) if rnd else 0
    with EventsWriter(events_path) as events:
        events.write(_PHASE, iter_, "phase_start")
        for text in notes:
            events.write(_PHASE, iter_, "phase_note", text=text)
        events.write(_PHASE, iter_, "phase_end", exit="ok", duration_ms=duration_ms)
    run_log.rerender_summary(events_path, artifacts.summary_path(config, state), state)


def _clip(text: str, limit: int, path: Path) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n\n…(обрезано, полностью — {path})"


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def _fetch_mr_comments(config: Config, state: State, wt: Path, console: Console) -> str | None:
    if "fetch_mr_comments" not in config.tools or not state.mr_url:
        return None
    res = run_tool(config, "fetch_mr_comments", tool_context(state), cwd=wt, timeout=120)
    if not res.ok:
        console.print(
            f"[yellow]fetch_mr_comments exited {res.exit_code}:[/yellow] {res.stderr.strip()[:300]}"
        )
        return None
    path = artifacts.mr_comments(config, state)
    path.write_text(res.stdout, encoding="utf-8")
    return res.stdout


def _write_brief(
    config: Config, state: State, wt: Path, console: Console, rebase_onto: str = ""
) -> Path:
    rnd = state.fix_round
    assert rnd is not None
    base_ref = git.resolve_base_ref(wt, state.base_branch)
    # В режиме ревью HEAD стоит на точке ответвления, в режиме ветки ветка может
    # быть посреди rebase — в обоих случаях коммиты считаем от ref-а ветки.
    commits_range = (
        f"{rnd.base}..{rnd.head}"
        if rnd.mode == "review"
        else f"{base_ref}..refs/heads/{state.branch}"
    )
    mr_comments = _fetch_mr_comments(config, state, wt, console)
    task_md = artifacts.task(config, state)
    files = [
        p
        for p in (
            task_md,
            artifacts.plan(config, state),
            artifacts.review(config, state),
            artifacts.mr_comments(config, state),
        )
        if p.exists()
    ]
    task_text = _read(task_md)
    brief = artifacts.fix_brief(config, state)
    content = render_template(
        "fix_brief.md.j2",
        {
            "task_id": state.task_id,
            "task_title": state.task_title or "",
            "task_url": state.task_url or "",
            "mr_url": state.mr_url or "",
            "branch": state.branch,
            "base_branch": state.base_branch,
            "base": rnd.base[:12],
            "base_ref": base_ref,
            "mode": rnd.mode,
            "rebase_onto": rebase_onto,
            "conflicts": git.conflicted_files(wt) if rebase_onto else [],
            "round": rnd.number,
            "commits": git.log_oneline(wt, commits_range),
            "mr_comments": (
                _clip(mr_comments, _MR_COMMENTS_INLINE, artifacts.mr_comments(config, state))
                if mr_comments and mr_comments.strip()
                else ""
            ),
            "task": _clip(task_text, _TASK_INLINE, task_md) if task_text else "",
            "files": [str(p) for p in files],
        },
    )
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(content, encoding="utf-8")
    return brief


def _is_ours(path: Path) -> bool:
    return "-m wf.hooks" in path.read_text(encoding="utf-8")


def _install_session_settings(config: Config, state: State, wt: Path, brief: Path) -> None:
    path = wt / _SETTINGS
    backup = artifacts.fix_settings_backup(config, state)
    if path.exists() and not backup.exists() and not _is_ours(path):
        shutil.move(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    py = shlex.quote(sys.executable)
    settings = {
        # Чтобы Claude читал план/ревью/комментарии MR без запроса прав.
        "permissions": {
            "additionalDirectories": [str(config.artifacts_dir(state.workflow, state.task_id))]
        },
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{py} -m wf.hooks session-start {shlex.quote(str(brief))}",
                        }
                    ],
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": f"{py} -m wf.hooks git-readonly"}],
                }
            ],
        },
    }
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ignored = run_shell(
        ["git", "-C", str(wt), "check-ignore", "-q", str(_SETTINGS)], check=False
    ).ok
    if not ignored:
        git.exclude_in_worktree(wt, f"/{_SETTINGS}")


def _remove_session_settings(config: Config, state: State, wt: Path) -> None:
    path = wt / _SETTINGS
    if path.exists() and _is_ours(path):
        path.unlink()
    backup = artifacts.fix_settings_backup(config, state)
    if backup.exists():
        shutil.move(backup, path)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _open_ide(config: Config, state: State, wt: Path, console: Console) -> None:
    if "open_ide" not in config.tools:
        console.print("  [dim]tools.open_ide не настроен — открой worktree в IDE сам[/dim]")
        return
    res = run_tool(config, "open_ide", tool_context(state), cwd=wt, timeout=60)
    if not res.ok:
        console.print(
            f"  [yellow]open_ide exited {res.exit_code}:[/yellow] {res.stderr.strip()[:300]}"
        )
