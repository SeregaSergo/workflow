from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]


class TaskLocked(Exception):
    """Бросается, если `.lock` каталога задачи уже удерживает другой процесс."""

    def __init__(self, task_id: str, lock_path: Path, holder_pid: int | None) -> None:
        self.task_id = task_id
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        msg = f"task {task_id} уже выполняется"
        if holder_pid is not None:
            msg += f" (PID {holder_pid})"
        super().__init__(msg)


def _read_holder_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@contextmanager
def task_lock(task_dir: Path, task_id: str) -> Iterator[Path]:
    """Эксклюзивный non-blocking lock на `<task_dir>/.lock`.

    fd закрывается в `finally` — kernel снимает `flock` автоматически, в т.ч.
    после крэша процесса (stale lock сам рассасывается). На non-POSIX платформах
    кидаем `NotImplementedError`: msvcrt-вариант пока не реализован, см.
    `tasks/05-task-lock.md`.
    """
    if fcntl is None:
        raise NotImplementedError(
            "wf поддерживает блокировку задач только на POSIX (требуется fcntl); "
            "не запускай параллельные wf start/resume на этой платформе"
        )

    task_dir.mkdir(parents=True, exist_ok=True)
    lock_path = task_dir / ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TaskLocked(task_id, lock_path, _read_holder_pid(lock_path)) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        yield lock_path
    finally:
        os.close(fd)
