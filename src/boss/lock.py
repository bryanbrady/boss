"""Per-issue locks, so two boss processes on this machine never run the same issue.

A label swap can't claim an issue atomically: `watch` and a manual `boss run` could both
see it queued. The lock is an flock on a file under BOSS_HOME. The kernel drops it when
the process exits, so a crash never leaves one behind.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class Locked(RuntimeError):
    pass


@contextmanager
def issue_lock(home: Path, repo: str, number: int) -> Iterator[None]:
    path = home / "locks" / f"{repo.replace('/', '-')}-{number}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o644), "r+") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = file.read().strip() or "?"
            raise Locked(f"#{number} is already being run by boss (pid {holder})") from None
        file.truncate()
        file.write(str(os.getpid()))
        file.flush()
        try:
            yield
        finally:
            file.seek(0)
            file.truncate()
            fcntl.flock(file, fcntl.LOCK_UN)
