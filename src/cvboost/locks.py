"""Advisory file locks (`flock`): the kernel drops them when the process dies (no stale PID files).

- `worker.lock`   held for the whole life of a `cvboost worker`: a second worker exits.
- `pipeline.lock` held while collect/triage/score/notify runs (worker cycle or a manual CLI step).
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class LockBusy(Exception):
    def __init__(self, path: Path, holder: str):
        super().__init__(f"{path.name} is held by {holder}")
        self.path = path
        self.holder = holder


class LockHandle:
    def __init__(self, fd: int, path: Path):
        self._fd: int | None = fd
        self.path = path

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self) -> LockHandle:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def holder_info(path: Path) -> str:
    """`pid N since TIME` as written by the holder, or 'another process'."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "another process"
    return text or "another process"


def try_lock(path: Path) -> LockHandle | None:
    """Take an exclusive lock without waiting. None if somebody else holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # no O_TRUNC: the holder's info must stay readable while we fail to get the lock
    try:
        # O_NOFOLLOW: a symlink planted at the lock path is refused, not followed
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise ValueError(f"cannot open lock file {path}: {exc.strerror}") from None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"pid {os.getpid()} since {datetime.now(UTC):%Y-%m-%d %H:%M:%S}Z".encode())
    return LockHandle(fd, path)


@contextmanager
def pipeline_lock(
    data_dir: Path,
    *,
    wait_s: float = 0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[LockHandle]:
    """Hold `pipeline.lock`, waiting up to `wait_s`. Raises LockBusy if still taken."""
    path = data_dir / "pipeline.lock"
    deadline = clock() + wait_s
    while True:
        handle = try_lock(path)
        if handle is not None:
            break
        if clock() >= deadline:
            raise LockBusy(path, holder_info(path))
        sleep(0.5)
    try:
        yield handle
    finally:
        handle.release()
