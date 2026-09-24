"""One upload at a time: an OS file lock `upload` holds for its whole run.

A second run reading the Sheet mid-chunk would retry the first run's RESERVED
rows under the same permanent identifiers. The OS drops the lock when its
process dies, so a crashed run never leaves it stuck."""
from __future__ import annotations

import errno
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Under the repo root, never --log-dir: page runs log to per-run folders.
UPLOAD_LOCK_PATH = Path(__file__).resolve().parent / "logs" / "upload.lock"

# Covers running_upload()'s momentary probe and Windows' delayed release of a dead process's lock.
ACQUIRE_ATTEMPTS = 20
ACQUIRE_RETRY_SECONDS = 0.1

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EDEADLK):
                return False
            raise
        return True

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    # flock, not lockf: a process drops lockf locks when it closes any descriptor for the file.
    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@dataclass(frozen=True)
class LockHolder:
    """The run holding the lock, recorded beside it so a refusal can name it."""

    pid: int
    started_at: str
    project: str
    batch: str | None
    live: bool

    def describe(self) -> str:
        scope = f"batch '{self.batch}'" if self.batch else "whole Sheet"
        mode = "live" if self.live else "test mode"
        return f"project {self.project}, {scope}, {mode}, started {self.started_at}, pid {self.pid}"


@dataclass(frozen=True)
class RunningUpload:
    """An upload holds the lock; `holder` is None when its record can't be read."""

    holder: LockHolder | None

    def describe(self) -> str:
        return self.holder.describe() if self.holder else "its details are not recorded"


class UploadLockHeld(Exception):
    """Raised by acquire() when another run holds the lock."""

    def __init__(self, running: RunningUpload) -> None:
        super().__init__(f"another upload is already running ({running.describe()})")
        self.running = running


class HeldUploadLock:
    """The lock this process holds; release() is safe to call twice."""

    def __init__(self, fd: int, lock_path: Path) -> None:
        self._fd: int | None = fd
        self._lock_path = lock_path

    def release(self) -> None:
        if self._fd is None:
            return
        _remove_holder(self._lock_path)
        _unlock(self._fd)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> HeldUploadLock:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def acquire(lock_path: Path, holder: LockHolder) -> HeldUploadLock:
    """Take the lock, or raise UploadLockHeld naming the run that has it."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    if not _lock_with_retries(fd):
        os.close(fd)
        raise UploadLockHeld(RunningUpload(_read_holder(lock_path)))
    _write_holder(lock_path, holder)
    return HeldUploadLock(fd, lock_path)


def running_upload(lock_path: Path) -> RunningUpload | None:
    """Which upload holds the lock, if any; takes it for an instant to find out."""
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except FileNotFoundError:
        return None
    try:
        if _try_lock(fd):
            _unlock(fd)
            return None
        return RunningUpload(_read_holder(lock_path))
    finally:
        os.close(fd)


def _lock_with_retries(fd: int) -> bool:
    for attempt in range(ACQUIRE_ATTEMPTS):
        if attempt:
            time.sleep(ACQUIRE_RETRY_SECONDS)
        if _try_lock(fd):
            return True
    return False


def _holder_path(lock_path: Path) -> Path:
    # A separate file: Windows locks are mandatory, so no other process can read a locked byte.
    return lock_path.with_suffix(".holder.json")


def _write_holder(lock_path: Path, holder: LockHolder) -> None:
    # Best effort: a missing record only costs a refusal its details.
    try:
        _holder_path(lock_path).write_text(json.dumps(asdict(holder)), encoding="utf-8")
    except OSError:
        pass


def _remove_holder(lock_path: Path) -> None:
    # Best effort: Windows won't delete a file a probe has open, and a stale record is ignored once the lock is free.
    try:
        _holder_path(lock_path).unlink(missing_ok=True)
    except OSError:
        pass


def _read_holder(lock_path: Path) -> LockHolder | None:
    try:
        record = json.loads(_holder_path(lock_path).read_text(encoding="utf-8"))
        batch = record["batch"]
        return LockHolder(
            pid=int(record["pid"]),
            started_at=str(record["started_at"]),
            project=str(record["project"]),
            batch=None if batch is None else str(batch),
            live=bool(record["live"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None
