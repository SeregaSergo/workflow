from __future__ import annotations

import shutil
from pathlib import Path

from wf.runner import run_shell

CLAUDE_ALLOW_LIST = ("agents", "commands", "skills", "CLAUDE.md")


def _git(repo: Path, *args: str, check: bool = True) -> str:
    res = run_shell(["git", "-C", str(repo), *args], check=check)
    return res.stdout.strip()


def is_repo(path: Path) -> bool:
    return run_shell(
        ["git", "-C", str(path), "rev-parse", "--git-dir"], check=False
    ).ok


def current_branch(repo: Path) -> str:
    """Имя текущей ветки. Бросает RuntimeError при DETACHED HEAD."""
    res = run_shell(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], check=False
    )
    if not res.ok:
        raise RuntimeError(
            f"не удалось определить текущую ветку в {repo} "
            "(возможно, DETACHED HEAD — сделай git checkout <branch>)"
        )
    return res.stdout.strip()


def fetch(repo: Path, remote: str = "origin") -> None:
    _git(repo, "fetch", remote, "--prune")


def worktree_exists_for_branch(repo: Path, branch: str) -> Path | None:
    out = _git(repo, "worktree", "list", "--porcelain")
    current_path: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree ") :])
        elif line.startswith("branch ") and current_path is not None:
            ref = line[len("branch ") :]
            if ref == f"refs/heads/{branch}" and current_path.exists():
                return current_path
    return None


def _worktree_paths(repo: Path) -> list[Path]:
    out = _git(repo, "worktree", "list", "--porcelain")
    return [Path(line[len("worktree ") :]) for line in out.splitlines() if line.startswith("worktree ")]


def branch_exists(repo: Path, branch: str) -> bool:
    res = run_shell(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/heads/{branch}"],
        check=False,
    )
    return res.ok


def remote_branch_exists(repo: Path, base_branch: str, remote: str = "origin") -> bool:
    res = run_shell(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/remotes/{remote}/{base_branch}"],
        check=False,
    )
    return res.ok


def resolve_base_ref(repo: Path, base_branch: str, remote: str = "origin") -> str:
    """Вернёт `origin/<base>` если remote-ветка есть, иначе локальный `<base>`.

    Бросает RuntimeError, если ни того, ни другого нет.
    """
    if remote_branch_exists(repo, base_branch, remote):
        return f"{remote}/{base_branch}"
    if branch_exists(repo, base_branch):
        return base_branch
    raise RuntimeError(
        f"base branch '{base_branch}' not found locally or on remote '{remote}'"
    )


def commits_ahead(repo: Path, base_ref: str) -> int:
    """`git rev-list --count <base_ref>..HEAD` → int."""
    out = _git(repo, "rev-list", "--count", f"{base_ref}..HEAD")
    return int(out)


def rev_parse(repo: Path, ref: str = "HEAD") -> str:
    return _git(repo, "rev-parse", "--verify", ref)


def attached_branch(repo: Path) -> str | None:
    """Имя ветки, на которую указывает HEAD, или None при detached HEAD."""
    res = run_shell(
        ["git", "-C", str(repo), "symbolic-ref", "-q", "--short", "HEAD"], check=False
    )
    return res.stdout.strip() if res.ok else None


def porcelain(repo: Path) -> str:
    return _git(repo, "status", "--porcelain")


def conflicted_files(repo: Path) -> list[str]:
    return _git(repo, "diff", "--name-only", "--diff-filter=U").splitlines()


_IN_PROGRESS = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
)


def operation_in_progress(repo: Path) -> str | None:
    """Незавершённая git-операция в worktree (`rebase`, `merge`, …) или None."""
    args = [a for name, _ in _IN_PROGRESS for a in ("--git-path", name)]
    lines = _git(repo, "rev-parse", *args).splitlines()
    for line, (_, op) in zip(lines, _IN_PROGRESS):
        path = Path(line)
        if not path.is_absolute():
            path = repo / path
        if path.exists():
            return op
    return None


def remote_sha(repo: Path, branch: str, remote: str = "origin") -> str | None:
    """sha `<remote>/<branch>` по локальным ref-ам; None — такой ветки на remote нет."""
    res = run_shell(
        ["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/remotes/{remote}/{branch}"],
        check=False,
    )
    return res.stdout.strip() if res.ok else None


def is_ancestor(repo: Path, ancestor: str, ref: str) -> bool:
    return run_shell(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, ref], check=False
    ).ok


def rebase(repo: Path, onto: str) -> bool:
    """`git rebase --autostash <onto>`; False — rebase остановился на конфликтах."""
    res = run_shell(["git", "-C", str(repo), "rebase", "--autostash", onto], check=False)
    if res.ok:
        return True
    if operation_in_progress(repo) == "rebase":
        return False
    raise RuntimeError(f"git rebase {onto} не удался: {(res.stderr or res.stdout).strip()}")


def merge_base(repo: Path, base_branch: str) -> str:
    return _git(repo, "merge-base", resolve_base_ref(repo, base_branch), "HEAD")


def fork_point(repo: Path, base_branch: str, recorded: str | None) -> str:
    """Коммит, от которого считается diff ветки (то же, что показывает MR).

    Обычно это merge-base с base_branch. Если ветку уже влили в base_branch,
    merge-base совпадает с HEAD — тогда берём точку, записанную в state.
    """
    head = rev_parse(repo)
    base = merge_base(repo, base_branch)
    if base != head:
        return base
    if recorded:
        return recorded
    raise RuntimeError(
        f"ветка уже влита в '{base_branch}', а точка ответвления не записана "
        "в state.base_commit — не из чего посчитать diff"
    )


def enter_fix_mode(repo: Path, base: str) -> str:
    """Отсоединяет HEAD и мягко сдвигает его на `base`; возвращает прежний HEAD.

    Ветка остаётся на месте, индекс и файлы не меняются: всё, что на ветке
    поверх `base`, становится staged, а новые правки появляются как unstaged.
    """
    head = rev_parse(repo)
    _git(repo, "checkout", "-q", "--detach")
    _git(repo, "reset", "-q", "--soft", base)
    return head


def leave_fix_mode(repo: Path, head: str, branch: str) -> None:
    """Возвращает HEAD на ветку; в рабочей копии остаётся только дельта раунда.

    Коммиты, сделанные во время раунда поверх detached HEAD (например, кнопкой
    Commit в IDE), не теряются: их содержимое остаётся в индексе и попадёт в
    коммит раунда.
    """
    current = attached_branch(repo)
    if current == branch:
        return
    if current is not None:
        raise RuntimeError(
            f"во время раунда HEAD переключили на ветку '{current}', "
            f"ожидалась '{branch}' — верни её вручную"
        )
    tip = rev_parse(repo, f"refs/heads/{branch}")
    if tip != head:
        raise RuntimeError(
            f"ветка '{branch}' сдвинулась во время раунда ({head[:12]} → {tip[:12]}); "
            "разберись вручную"
        )
    _git(repo, "reset", "-q", "--soft", head)
    _git(repo, "symbolic-ref", "HEAD", f"refs/heads/{branch}")


def stage_all(repo: Path) -> bool:
    """`git add -A`; True, если в индексе есть что коммитить."""
    _git(repo, "add", "-A")
    return not run_shell(["git", "-C", str(repo), "diff", "--cached", "--quiet"], check=False).ok


def staged_diff(repo: Path, *, stat: bool = False) -> str:
    """Дельта индекса относительно HEAD (с `stat` — только `--stat`)."""
    return _git(repo, "diff", "--cached", "--no-color", "--no-ext-diff", *(["--stat"] if stat else []))


def commit_all(repo: Path, message: str) -> str | None:
    """`git add -A` + commit; возвращает sha коммита или None, если коммитить нечего."""
    if not stage_all(repo):
        return None
    _git(repo, "commit", "-q", "-m", message)
    return rev_parse(repo)


def shortstat(repo: Path, commit: str) -> str:
    return _git(repo, "show", "--shortstat", "--format=", commit)


def log_oneline(repo: Path, rev_range: str) -> str:
    return _git(repo, "log", "--oneline", "--no-decorate", rev_range)


def unpushed_commits(repo: Path, branch: str, remote: str = "origin") -> int | None:
    """Сколько коммитов ветки нет на remote; None — ветки на remote нет."""
    if not remote_branch_exists(repo, branch, remote):
        return None
    return int(_git(repo, "rev-list", "--count", f"{remote}/{branch}..{branch}"))


def push(repo: Path, branch: str, remote: str = "origin", *, lease: str | None = None) -> None:
    """Пушит ветку; с `lease` — force-push, если ветка на remote всё ещё на `lease`."""
    if lease is None:
        _git(repo, "push", "-q", remote, branch)
    else:
        _git(repo, "push", "-q", f"--force-with-lease=refs/heads/{branch}:{lease}", remote, branch)


def ensure_worktree(repo_path: Path, branch: str, worktree_path: Path, remote: str = "origin") -> bool:
    """Создаёт worktree ветки заново, если его удалили (например, cleanup_worktree).

    Возвращает True, если worktree пришлось создать.
    """
    repo_path = repo_path.resolve()
    worktree_path = worktree_path.resolve()
    # Сверяемся по пути, а не по ветке: при отсоединённом HEAD (раунд правок в
    # режиме ревью, незавершённый rebase) git не пишет ветку в `worktree list`.
    if worktree_path.exists() and worktree_path in _worktree_paths(repo_path):
        return False
    if worktree_path.exists():
        raise RuntimeError(f"{worktree_path} существует, но это не worktree ветки '{branch}'")
    run_shell(["git", "-C", str(repo_path), "worktree", "prune"], check=False)
    try:
        fetch(repo_path, remote)
    except RuntimeError:
        pass
    if branch_exists(repo_path, branch):
        _git(repo_path, "worktree", "add", str(worktree_path), branch)
    elif remote_branch_exists(repo_path, branch, remote):
        _git(
            repo_path, "worktree", "add", "--track", "-b", branch,
            str(worktree_path), f"{remote}/{branch}",
        )
    else:
        raise RuntimeError(f"ветка '{branch}' не найдена ни локально, ни на '{remote}'")
    return True


def prepare_worktree(
    repo_path: Path,
    base_branch: str,
    new_branch: str,
    *,
    worktree_path: Path,
    remote: str = "origin",
) -> Path:
    repo_path = repo_path.resolve()
    worktree_path = worktree_path.resolve()

    existing = worktree_exists_for_branch(repo_path, new_branch)
    if existing is not None:
        if existing == worktree_path:
            return existing
        raise RuntimeError(
            f"branch '{new_branch}' is already checked out at {existing}; refusing to create another worktree at {worktree_path}"
        )

    if worktree_path.exists():
        raise RuntimeError(
            f"worktree path already exists: {worktree_path}; remove it or pick another name"
        )

    if remote_branch_exists(repo_path, base_branch, remote):
        base_ref = f"{remote}/{base_branch}"
    elif branch_exists(repo_path, base_branch):
        base_ref = base_branch
    else:
        raise RuntimeError(
            f"base branch '{base_branch}' not found locally or on remote '{remote}'"
        )

    if branch_exists(repo_path, new_branch):
        _git(repo_path, "worktree", "add", str(worktree_path), new_branch)
    else:
        _git(repo_path, "worktree", "add", str(worktree_path), "-b", new_branch, base_ref)
    return worktree_path


def remove_worktree(repo_path: Path, worktree_path: Path) -> None:
    """Удаляет git worktree и подчищает метаданные.

    Работает идемпотентно: если ворктри уже нет на диске или git о нём не знает,
    всё равно запускает `prune`, чтобы убрать висячие записи.
    """
    repo_path = repo_path.resolve()
    worktree_path = worktree_path.resolve()
    if worktree_path.exists():
        run_shell(
            ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree_path)],
            check=False,
        )
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    run_shell(["git", "-C", str(repo_path), "worktree", "prune"], check=False)


def copy_claude_dir(repo_path: Path, worktree_path: Path) -> list[Path]:
    src = (repo_path / ".claude").resolve()
    if not src.exists() or not src.is_dir():
        return []
    dst = (worktree_path / ".claude").resolve()
    dst.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for name in CLAUDE_ALLOW_LIST:
        s = src / name
        if not s.exists():
            continue
        d = dst / name
        if s.is_dir():
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)
        copied.append(d)

    # Fallback для нестандартной раскладки: если в репо CLAUDE.md лежит только
    # в `.claude/`, продублировать его в корень worktree, чтобы SDK подхватил
    # project memory (он ищет <cwd>/CLAUDE.md). Корневой CLAUDE.md из git
    # (если он tracked) не перезаписываем.
    root_claude_md = (worktree_path / "CLAUDE.md").resolve()
    src_claude_md = src / "CLAUDE.md"
    if not root_claude_md.exists() and src_claude_md.exists() and src_claude_md.is_file():
        shutil.copy2(src_claude_md, root_claude_md)
        copied.append(root_claude_md)
        exclude_in_worktree(worktree_path, "/CLAUDE.md")

    return copied


def exclude_in_worktree(worktree_path: Path, pattern: str) -> None:
    """Дописывает `pattern` в `info/exclude` репозитория (создаёт файл при
    необходимости), чтобы синтетический файл не появлялся в `git status`.
    Идемпотентно — повтор записи не делает.

    Для worktree git читает `info/exclude` только из общего `$GIT_COMMON_DIR`,
    а не из per-worktree gitdir, — путь берём у самого git.
    """
    res = run_shell(
        ["git", "-C", str(worktree_path), "rev-parse", "--git-path", "info/exclude"],
        check=False,
    )
    if not res.ok:
        return
    exclude_file = Path(res.stdout.strip())
    if not exclude_file.is_absolute():
        exclude_file = (worktree_path / exclude_file).resolve()
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    lines = {l.strip() for l in existing.splitlines() if l.strip() and not l.lstrip().startswith("#")}
    if pattern in lines:
        return
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    with exclude_file.open("a", encoding="utf-8") as f:
        f.write(f"{suffix}{pattern}\n")
