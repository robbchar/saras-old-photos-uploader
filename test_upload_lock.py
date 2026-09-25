import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

import upload_lock
from upload_lock import LockHolder, RunningUpload, UploadLockHeld

PROJECT_ROOT = Path(__file__).resolve().parent

HOLDER = LockHolder(
    pid=4312,
    started_at="2026-09-24T14:02:11Z",
    project="astoriaphotos",
    batch="Waterfront",
    live=False,
)

# Holds the lock until the parent kills it; prints its own pid, since a Windows venv launcher's pid differs.
CHILD_HOLDS_THE_LOCK = (
    "import os, sys\n"
    "from pathlib import Path\n"
    "import upload_lock\n"
    "holder = upload_lock.LockHolder(pid=os.getpid(), started_at='2026-09-24T15:00:00Z',"
    " project='astoriaphotos', batch=None, live=True)\n"
    "upload_lock.acquire(Path(sys.argv[1]), holder)\n"
    "print('held', os.getpid(), flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.fixture
def lock_path(tmp_path):
    return tmp_path / "logs" / "upload.lock"


@pytest.fixture
def no_retry(monkeypatch):
    """A held lock is refused on the first try, so tests don't wait out the retry window."""
    monkeypatch.setattr(upload_lock, "ACQUIRE_ATTEMPTS", 1)


def test_acquire_creates_the_logs_folder_a_fresh_checkout_lacks(lock_path):
    upload_lock.acquire(lock_path, HOLDER).release()

    assert lock_path.exists()


def test_a_second_acquire_is_refused_and_names_the_holder(lock_path, no_retry):
    with upload_lock.acquire(lock_path, HOLDER):
        with pytest.raises(UploadLockHeld) as refusal:
            upload_lock.acquire(lock_path, HOLDER)

    assert refusal.value.running == RunningUpload(HOLDER)
    assert str(refusal.value) == (
        "another upload is already running (project astoriaphotos, batch 'Waterfront', "
        "test mode, started 2026-09-24T14:02:11Z, pid 4312)"
    )


def test_a_whole_sheet_live_run_describes_itself_that_way():
    holder = LockHolder(
        pid=7, started_at="2026-09-24T14:02:11Z", project="sarasoldphotos", batch=None, live=True
    )

    assert holder.describe() == (
        "project sarasoldphotos, whole Sheet, live, started 2026-09-24T14:02:11Z, pid 7"
    )


def test_the_lock_is_free_again_after_release(lock_path):
    upload_lock.acquire(lock_path, HOLDER).release()
    upload_lock.acquire(lock_path, HOLDER).release()


def test_release_twice_is_harmless(lock_path):
    lock = upload_lock.acquire(lock_path, HOLDER)
    lock.release()
    lock.release()


def test_release_removes_the_holder_record(lock_path):
    upload_lock.acquire(lock_path, HOLDER).release()

    assert not lock_path.with_suffix(".holder.json").exists()


def test_running_upload_is_none_when_nothing_holds_the_lock(lock_path):
    assert upload_lock.running_upload(lock_path) is None

    upload_lock.acquire(lock_path, HOLDER).release()

    assert upload_lock.running_upload(lock_path) is None


def test_running_upload_names_the_holder_and_lets_go(lock_path, no_retry):
    with upload_lock.acquire(lock_path, HOLDER):
        assert upload_lock.running_upload(lock_path) == RunningUpload(HOLDER)
        with pytest.raises(UploadLockHeld):
            upload_lock.acquire(lock_path, HOLDER)

    assert upload_lock.running_upload(lock_path) is None


def test_overlapping_probes_never_report_a_phantom_upload(lock_path):
    """Two in-process probes landing in the same instant must not see each other's momentary hold."""
    upload_lock.acquire(lock_path, HOLDER).release()
    results: list[RunningUpload | None] = []
    results_lock = threading.Lock()

    def probe_repeatedly() -> None:
        for _ in range(2000):
            result = upload_lock.running_upload(lock_path)
            with results_lock:
                results.append(result)

    threads = [threading.Thread(target=probe_repeatedly) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    phantoms = [result for result in results if result is not None]
    assert len(results) == 4000
    assert phantoms == []


def test_a_held_lock_with_no_holder_record_is_not_a_running_upload(lock_path):
    """_PROBE_LOCK serializes probes only within one process. Across processes a
    probe can momentarily hold the lock; a held lock with no record beside it is
    that, not a run - only acquire() writes a record. Uses the real OS lock."""
    lock_path.parent.mkdir(parents=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        assert upload_lock._try_lock(fd)  # hold it raw, as a bare probe would, writing no record

        assert upload_lock.running_upload(lock_path) is None
    finally:
        upload_lock._unlock(fd)
        os.close(fd)


def test_a_holder_record_left_by_a_crashed_run_is_ignored(lock_path):
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.with_suffix(".holder.json").write_text(
        '{"pid": 1, "started_at": "2026-09-01T00:00:00Z", "project": "old",'
        ' "batch": null, "live": true}',
        encoding="utf-8",
    )

    assert upload_lock.running_upload(lock_path) is None
    with upload_lock.acquire(lock_path, HOLDER):
        assert upload_lock.running_upload(lock_path) == RunningUpload(HOLDER)


def test_an_unreadable_holder_record_still_reports_a_running_upload(lock_path, no_retry):
    with upload_lock.acquire(lock_path, HOLDER):
        lock_path.with_suffix(".holder.json").write_text("not json", encoding="utf-8")

        assert upload_lock.running_upload(lock_path) == RunningUpload(None)
        with pytest.raises(UploadLockHeld, match="its details are not recorded"):
            upload_lock.acquire(lock_path, HOLDER)


def test_acquire_waits_out_a_brief_hold(lock_path):
    """running_upload() takes the lock for an instant; a run starting then must not be refused."""
    first = upload_lock.acquire(lock_path, HOLDER)
    releaser = threading.Timer(0.3, first.release)
    releaser.start()
    try:
        second = upload_lock.acquire(lock_path, HOLDER)
    finally:
        releaser.join()

    second.release()


def test_a_lock_held_by_another_process_is_refused_and_freed_when_it_dies(lock_path, monkeypatch):
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_HOLDS_THE_LOCK, str(lock_path)],
        cwd=PROJECT_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        line = child.stdout.readline().split()
        if line[:1] != ["held"]:
            child.kill()
            pytest.fail(f"the child never took the lock: {line!r} {child.communicate()[1]}")
        monkeypatch.setattr(upload_lock, "ACQUIRE_ATTEMPTS", 1)

        with pytest.raises(UploadLockHeld) as refusal:
            upload_lock.acquire(lock_path, HOLDER)

        assert refusal.value.running == RunningUpload(
            LockHolder(
                pid=int(line[1]),
                started_at="2026-09-24T15:00:00Z",
                project="astoriaphotos",
                batch=None,
                live=True,
            )
        )
    finally:
        child.kill()
        child.wait(timeout=10)

    # Windows may take a moment to release a dead process's lock.
    monkeypatch.setattr(upload_lock, "ACQUIRE_ATTEMPTS", 50)
    upload_lock.acquire(lock_path, HOLDER).release()


def fake_fcntl(operations: list[int], held_elsewhere: bool) -> types.ModuleType:
    module = types.ModuleType("fcntl")
    setattr(module, "LOCK_EX", 2)
    setattr(module, "LOCK_NB", 4)
    setattr(module, "LOCK_UN", 8)

    def flock(fd: int, operation: int) -> None:
        operations.append(operation)
        if held_elsewhere and operation != 8:
            raise BlockingIOError(11, "Resource temporarily unavailable")

    setattr(module, "flock", flock)
    return module


def load_upload_lock_as_on_the_mac(monkeypatch, fcntl_module: types.ModuleType):
    """A separate module object with the fcntl branch loaded; the real upload_lock is untouched."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "fcntl", fcntl_module)
    spec = importlib.util.spec_from_file_location(
        "upload_lock_as_on_the_mac", PROJECT_ROOT / "upload_lock.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass looks its own module up in sys.modules.
    monkeypatch.setitem(sys.modules, "upload_lock_as_on_the_mac", module)
    spec.loader.exec_module(module)
    return module


def test_on_the_mac_the_lock_is_an_exclusive_non_blocking_flock(monkeypatch, lock_path):
    operations: list[int] = []
    on_the_mac = load_upload_lock_as_on_the_mac(monkeypatch, fake_fcntl(operations, False))

    on_the_mac.acquire(lock_path, HOLDER).release()

    assert operations == [2 | 4, 8]


def test_on_the_mac_a_flock_that_would_block_is_refused(monkeypatch, lock_path):
    operations: list[int] = []
    on_the_mac = load_upload_lock_as_on_the_mac(monkeypatch, fake_fcntl(operations, True))
    monkeypatch.setattr(on_the_mac, "ACQUIRE_ATTEMPTS", 1)

    with pytest.raises(on_the_mac.UploadLockHeld):
        on_the_mac.acquire(lock_path, HOLDER)


def test_on_the_mac_the_probe_takes_and_drops_a_free_lock(monkeypatch, lock_path):
    operations: list[int] = []
    on_the_mac = load_upload_lock_as_on_the_mac(monkeypatch, fake_fcntl(operations, False))
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()

    assert on_the_mac.running_upload(lock_path) is None
    assert operations == [2 | 4, 8]


def test_on_the_mac_the_probe_names_a_held_lock_without_unlocking_it(monkeypatch, lock_path):
    operations: list[int] = []
    on_the_mac = load_upload_lock_as_on_the_mac(monkeypatch, fake_fcntl(operations, True))
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.with_suffix(".holder.json").write_text(
        json.dumps(dataclasses.asdict(HOLDER)), encoding="utf-8"
    )

    running = on_the_mac.running_upload(lock_path)

    assert running is not None and running.holder is not None
    assert dataclasses.asdict(running.holder) == dataclasses.asdict(HOLDER)
    assert operations == [2 | 4]
