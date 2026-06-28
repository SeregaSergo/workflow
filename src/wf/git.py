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
        _exclude_in_worktree(worktree_path, "CLAUDE.md")

    return copied


def _exclude_in_worktree(worktree_path: Path, pattern: str) -> None:
    """Дописывает `pattern` в `<worktree>/.git/info/exclude` (создаёт файл при
    необходимости), чтобы синтетический файл не появлялся в `git status`.
    Идемпотентно — повтор записи не делает.

    В worktree `.git` — это файл-указатель на `gitdir`; читаем его, чтобы
    найти настоящий каталог.
    """
    gitdir = _resolve_worktree_gitdir(worktree_path)
    if gitdir is None:
        return
    info_dir = gitdir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude_file = info_dir / "exclude"
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    lines = {l.strip() for l in existing.splitlines() if l.strip() and not l.lstrip().startswith("#")}
    if pattern in lines:
        return
    suffix = "" if existing.endswith("\n") or not existing else "\n"
    with exclude_file.open("a", encoding="utf-8") as f:
        f.write(f"{suffix}{pattern}\n")


def _resolve_worktree_gitdir(worktree_path: Path) -> Path | None:
    """Возвращает путь к `gitdir` worktree-а (содержимое файла `.git: gitdir: …`)
    или сам каталог `.git`, если это полноценный репозиторий. None — если
    структура нераспознана."""
    dot_git = worktree_path / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        for line in dot_git.read_text(encoding="utf-8").splitlines():
            if line.startswith("gitdir:"):
                raw = line[len("gitdir:") :].strip()
                p = Path(raw)
                if not p.is_absolute():
                    p = (worktree_path / p).resolve()
                return p
    return None
