"""E2E rehearsal: the real CLI against the real Test Sheet and IA's test_collection.

Opt-in, takes minutes: python -m pytest test_e2e_rehearsal.py::test_rehearsal --run-e2e -v -s
Run test_rehearsal alone: the upload-page e2e resets the Test Sheet afterward and wipes the
withdrawn row step 9a leaves for the hand clear check.
Each step label names the hand check it replaces.
"""

from __future__ import annotations

import codecs
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import IO, Any, Protocol, TextIO, TypeVar
from unittest.mock import patch

import internetarchive
import internetarchive.config
import pytest
from googleapiclient.errors import HttpError

import google_auth
from e2e_lock import LockHeld, LockLost, LockStillHeld, RehearsalLock, RunIdentity, acquire_lock
from e2e_sheet import (
    E2E_PROJECT,
    LOCK_TAB,
    E2ESheet,
    check_reset_allowed,
    load_fixture_grid,
    pad_grid,
    reset_test_sheet,
    set_cell,
    tab_ids,
)
from fake_sheets import FakeSheets, http_error
from fake_sheets import TARGET as FAKE_TARGET
from ia_bulk import (
    IA_HTTP_ADAPTER_KWARGS,
    ITEM_URL_PREFIX,
    CollectionConfirmed,
    CollectionMissing,
    build_sheets_service,
    TaskState,
    check_ia_collection,
    item_task_state,
)
from log_tab import LOG_TAB_HEADER
from project_config import DEFAULT_WITHDRAWN_DESCRIPTION, DEFAULT_WITHDRAWN_TITLE
from sheet_client import SheetClient

# For test_upload_page_drives_a_real_run_end_to_end: a real upload_server,
# reusing test_upload_server.py's own HTTP/SSE helpers rather than
# reinventing an SSE client here.
import page_runs
import stop_request
import upload_server
from test_upload_server import _post_json, _read_sse

REPO_ROOT = Path(__file__).resolve().parent
E2E_REGISTRY = REPO_ROOT / "e2e_fixtures" / "registry.json"
LIVE_REGISTRY = REPO_ROOT / "projects_registry.json"
FIXTURE_SHEET = REPO_ROOT / "e2e_fixtures" / "sheet.json"
# Both read at import, before conftest's autouse fixtures hide the real key and IA config from each test.
REAL_KEY_PATH = google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH
REAL_IA_ENVIRONMENT = {name: os.environ.get(name) for name in ("IA_CONFIG_FILE", "IA_ACCESS_KEY_ID", "IA_SECRET_ACCESS_KEY")}

IA_POLL_INTERVAL_SECONDS = 15
IA_POLL_TIMEOUT_SECONDS = 600
CLI_TIMEOUT_SECONDS = 900
PUMP_CHUNK_BYTES = 4096
PUMP_DRAIN_SECONDS = 10
# Twice the longest gap between check-ins, which is one CLI call; run_cli and wait_for_ia check in first.
LOCK_LEASE = timedelta(seconds=2 * CLI_TIMEOUT_SECONDS)

UPLOAD_COLUMNS = ("ia_identifier", "ia_uploaded", "ia_url", "ia_identifier_bib")
SYNC_COLUMNS = ("ia_sync_hash", "ia_last_synced.")
WITHDRAW_COLUMNS = ("Withdrawn", "ia_withdrawn")
BROKEN_FILENAME = "does-not-exist.jpg"
# ia_bulk.py prints this on stderr when IA refuses a request as rate limited and the run stops.
IA_RATE_LIMIT_NOTICE = "Internet Archive asked us to slow down"

# Grid indexes; header is 0, so Sheet row = index + 1.
FIRST_UPLOADED, SECOND_UPLOADED, BROKEN_ROW, THIRD_UPLOADED = 1, 2, 3, 4
# Index 5 is the row without a theme.
FOURTH_UPLOADED, FIFTH_UPLOADED = 6, 7
UPLOADED_ROWS = (FIRST_UPLOADED, SECOND_UPLOADED, THIRD_UPLOADED, FOURTH_UPLOADED, FIFTH_UPLOADED)

STEP_0 = "step 0 - reset (OPERATIONS §2 hand reset; 'Rehearsing the log tabs' intro)"
STEP_1 = "step 1 - validate (DEPLOYMENT §16 step 1; OPERATIONS §1)"
STEP_2 = "step 2 - upload (OPERATIONS 'Rehearsing the log tabs' step 1, first run)"
STEP_3 = "step 3 - upload again (OPERATIONS 'Rehearsing the log tabs' step 1, second run)"
STEP_4 = "step 4 - problem row (OPERATIONS 'Rehearsing the log tabs' step 2)"
STEP_4B = "step 4b - two files in one run, one progress bar each (OPERATIONS 'Rehearsing the log tabs' step 2, --limit 2)"
STEP_5 = "step 5 - items exist on IA (OPERATIONS pre-live checklist: zztest item eyeballed)"
STEP_6 = "step 6 - sync dry run (DEPLOYMENT §16 step 2)"
STEP_7 = "step 7 - sync an edit (OPERATIONS 'Rehearsing the log tabs' step 3, first run)"
STEP_8 = "step 8 - edit reached IA (OPERATIONS pre-live checklist: zztest item eyeballed)"
STEP_9 = "step 9 - quiet sync (OPERATIONS 'Rehearsing the log tabs' step 3, second run)"
STEP_9A = "step 9a - withdraw two items (OPERATIONS 'Withdrawing an item' steps 1-2)"
STEP_9A2 = "step 9a2 - the next run re-checks them (OPERATIONS 'Withdrawing an item' step 4)"
STEP_9B = "step 9b - restore one (OPERATIONS 'Withdrawing an item', putting it back)"
STEP_10 = "step 10 - tabs match log files (OPERATIONS 'Rehearsing the log tabs' step 4)"
STEP_11 = "step 11 - only expected cells changed (OPERATIONS 'Rehearsing the log tabs' step 5)"
STEP_12 = "step 12 - restore the broken filename (OPERATIONS 'Rehearsing the log tabs' step 2: put the cell back)"

Found = TypeVar("Found")
Returned = TypeVar("Returned")


class Finalizers(Protocol):
    def addfinalizer(self, finalizer: Callable[[], object]) -> None: ...


def _write_to_console(text: str, stream: TextIO) -> None:
    """A cp1252 console can't show ia's progress-bar block char; escape what it can't show instead of raising."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(text.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace"))
    stream.flush()


def _print_for_console(text: str) -> None:
    _write_to_console(f"{text}\n", sys.stdout)


def _echo_stdout(text: str) -> None:
    _write_to_console(text, sys.stdout)


def _echo_stderr(text: str) -> None:
    _write_to_console(text, sys.stderr)


def _echo_quietly(echo: Callable[[str], None], text: str) -> None:
    """A console that fails to write must not stop the pipe draining, or the child blocks."""
    try:
        echo(text.replace("\r\n", "\n"))
    except (OSError, ValueError):
        pass


def _pump(source: IO[bytes], echo: Callable[[str], None], captured: list[str]) -> None:
    """Chunks, not lines, so a progress bar's in-place redraws reach the console as they happen."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    # A trailing CR waits for the next read: it may be half of a CRLF.
    held_back = ""
    while chunk := source.read(PUMP_CHUNK_BYTES):
        text = decoder.decode(chunk)
        captured.append(text)
        pending = held_back + text
        held_back = "\r" if pending.endswith("\r") else ""
        _echo_quietly(echo, pending.removesuffix(held_back))
    tail = decoder.decode(b"", final=True)
    captured.append(tail)
    _echo_quietly(echo, held_back + tail)


def _joined(parts: list[str]) -> str:
    """CRLF becomes LF as in text mode, but a lone CR (a progress-bar redraw) is kept."""
    return "".join(parts).replace("\r\n", "\n")


def run_streaming(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    echo_stdout: Callable[[str], None] = _echo_stdout,
    echo_stderr: Callable[[str], None] = _echo_stderr,
) -> subprocess.CompletedProcess[str]:
    """subprocess.run(capture_output=True) that also echoes the child's output live; TimeoutExpired carries the partial output."""
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    # bufsize=0: each read returns whatever the child has written so far.
    with subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0) as process:
        assert process.stdout is not None and process.stderr is not None
        pumps = [
            threading.Thread(target=_pump, args=(process.stdout, echo_stdout, stdout_parts), daemon=True),
            threading.Thread(target=_pump, args=(process.stderr, echo_stderr, stderr_parts), daemon=True),
        ]
        for pump in pumps:
            pump.start()
        try:
            returncode = process.wait(timeout=timeout)
        except BaseException as interrupted:
            # As subprocess.run does: an interrupted step must not leave an upload running.
            process.kill()
            process.wait()
            _join_pumps(pumps)
            if isinstance(interrupted, subprocess.TimeoutExpired):
                raise subprocess.TimeoutExpired(
                    argv, timeout, output=_joined(stdout_parts), stderr=_joined(stderr_parts)
                ) from None
            raise
        # Bounded: a grandchild holding the pipe open must not hang the step.
        _join_pumps(pumps)
    return subprocess.CompletedProcess(argv, returncode, _joined(stdout_parts), _joined(stderr_parts))


def _join_pumps(pumps: list[threading.Thread]) -> None:
    for pump in pumps:
        pump.join(timeout=PUMP_DRAIN_SECONDS)


def cli_environment() -> dict[str, str]:
    """Read per call, so it carries the proxy settings the guard puts back for an e2e test; IA credentials are the real ones."""
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for name, value in REAL_IA_ENVIRONMENT.items():
        if value is None:
            environment.pop(name, None)
        else:
            environment[name] = value
    return environment


def run_cli(
    step: str, command: str, *flags: str, lock: RehearsalLock, log_dir: Path | None = None
) -> subprocess.CompletedProcess[str]:
    check_in(lock, step)
    argv = [sys.executable, "ia_bulk.py", command, *flags, "--registry", str(E2E_REGISTRY), "--project", E2E_PROJECT]
    if log_dir is not None:
        argv += ["--log-dir", str(log_dir)]
    # Header first: the command's output streams under it while it runs.
    _print_for_console(f"\n===== {step}\n$ ia_bulk.py {command} {' '.join(flags)}")
    # Unbuffered, so stdout interleaves with the progress bars as it would in a terminal.
    environment = {**cli_environment(), "PYTHONUNBUFFERED": "1"}
    try:
        return run_streaming(argv, cwd=REPO_ROOT, env=environment, timeout=CLI_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"{step}: ia_bulk.py {command} did not finish within {CLI_TIMEOUT_SECONDS // 60} min\n"
            f"--- stdout ---\n{exc.output or ''}\n--- stderr ---\n{exc.stderr or ''}"
        )


def output_of(result: subprocess.CompletedProcess[str]) -> str:
    return f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


def expect(step: str, condition: bool, message: str) -> None:
    if not condition:
        pytest.fail(f"{step}: {message}")


def expect_run(step: str, result: subprocess.CompletedProcess[str], exit_code: int, *texts: str) -> None:
    # Before the exit code: a throttled upload exits 1, which a step can expect.
    if IA_RATE_LIMIT_NOTICE in result.stderr:
        pytest.fail(
            f"{step}: Internet Archive is throttling uploads, not a defect in the tool; re-run later\n"
            f"{output_of(result)}"
        )
    if result.returncode != exit_code:
        pytest.fail(f"{step}: expected exit {exit_code}, got {result.returncode}\n{output_of(result)}")
    for text in texts:
        if text not in result.stdout:
            pytest.fail(f"{step}: expected {text!r} in stdout\n{output_of(result)}")


def expect_only_allowed_changes(step: str, fixture: list[list[str]], final: list[list[str]], allowed: set[tuple[int, str]]) -> None:
    """`allowed` holds (grid index, column) pairs that may differ from the fixture."""
    header = fixture[0]
    expect(step, len(final) == len(fixture), f"the data tab has {len(final)} rows, expected {len(fixture)}")
    # grid() pads to the header's width, so a longer row has a cell past the header.
    wider = [row_index + 1 for row_index, row in enumerate(final) if len(row) > len(header)]
    expect(step, not wider, f"rows {wider} have cells past the header")
    unexpected = [
        f"row {row_index + 1} {column!r}: {fixture[row_index][column_index]!r} -> {final[row_index][column_index]!r}"
        for row_index in range(len(fixture))
        for column_index, column in enumerate(header)
        if final[row_index][column_index] != fixture[row_index][column_index] and (row_index, column) not in allowed
    ]
    expect(step, not unexpected, "unexpected changes:\n" + "\n".join(unexpected))


def e2e_identifier(number: int) -> str:
    return f"lcps-{E2E_PROJECT}-{number:05d}"


def expect_recorded(step: str, sheet: RehearsalSheet, grid: list[list[str]], row_index: int, number: int) -> None:
    """The row holds its minted identifier, a zztest- item URL for it, and every upload cell."""
    identifier = e2e_identifier(number)
    recorded = sheet.cell(grid, row_index, "ia_identifier")
    expect(step, recorded == identifier, f"row {row_index + 1} ia_identifier is {recorded!r}, expected {identifier!r}")
    url = sheet.cell(grid, row_index, "ia_url")
    expect(
        step,
        url.startswith(f"{ITEM_URL_PREFIX}zztest-") and url.endswith(f"-{identifier}"),
        f"row {row_index + 1} ia_url is {url!r}",
    )
    expect(
        step,
        all(sheet.cell(grid, row_index, column) for column in UPLOAD_COLUMNS),
        f"row {row_index + 1} upload cells incomplete",
    )


def wait_for_ia(
    step: str,
    description: str,
    probe: Callable[[], Found | None],
    *,
    lock: RehearsalLock,
    on_timeout: str | Callable[[], str] | None = None,
) -> Found:
    """`on_timeout` may be a callable, read only at the timeout, for a message built from the last probe."""
    check_in(lock, step)
    deadline = time.monotonic() + IA_POLL_TIMEOUT_SECONDS
    while True:
        found = probe()
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            message = on_timeout() if callable(on_timeout) else on_timeout
            pytest.fail(f"{step}: {message or f'IA did not show {description}'} (waited {IA_POLL_TIMEOUT_SECONDS // 60} min)")
        time.sleep(IA_POLL_INTERVAL_SECONDS)


def real_ia_session() -> internetarchive.ArchiveSession:
    """A session with the credentials the CLI runs use; conftest hides them from this process, and IA's task queue needs them."""
    with patch.dict(os.environ):
        for name, value in REAL_IA_ENVIRONMENT.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        config = internetarchive.config.get_config()
    return internetarchive.get_session(config=config, http_adapter_kwargs=IA_HTTP_ADAPTER_KWARGS)


def ia_task_state(identifier: str) -> TaskState:
    return item_task_state(identifier, archive_session=real_ia_session())


def open_tasks(identifiers: Sequence[str]) -> dict[str, TaskState]:
    """The items IA has any task open on, in any state, with those tasks."""
    states = {identifier: ia_task_state(identifier) for identifier in identifiers}
    return {identifier: state for identifier, state in states.items() if state.total}


def still_busy_message(busy: dict[str, TaskState], refused: str) -> str:
    """A wait's timeout message; a paused or failed task needs IA staff, so re-running won't help."""
    message = f"{' and '.join(busy)} still busy at Internet Archive, so {refused}"
    held = [reason for identifier, state in busy.items() if (reason := state.held(identifier))]
    if not held:
        return f"{message}; re-run later"
    return (
        f"{message}: {'; '.join(held)} - re-running won't help until they do "
        "(see docs/OPERATIONS.md, 'Withdrawing an item')"
    )


def wait_until_idle(step: str, identifiers: Sequence[str], refused: str, *, lock: RehearsalLock) -> None:
    """Waits until IA has no task open on any of the items. Not just none running: a queued derive
    can start before the CLI's own check, which then refuses."""
    busy: dict[str, TaskState] = {}

    def idle() -> bool | None:
        busy.clear()
        busy.update(open_tasks(identifiers))
        return None if busy else True

    wait_for_ia(
        step,
        f"no open task on {' or '.join(identifiers)}",
        idle,
        lock=lock,
        on_timeout=lambda: still_busy_message(busy, refused),
    )


def item_metadata(identifier: str) -> dict[str, Any] | None:
    """Not fetch_current_metadata: it returns None on any error, which would wait out the timeout instead of failing."""
    return dict(internetarchive.get_item(identifier, http_adapter_kwargs=IA_HTTP_ADAPTER_KWARGS).metadata) or None


def item_file_mtime(identifier: str, name: str) -> int | None:
    """When IA last stored the named file (epoch seconds), or None when the item has no such file."""
    item = internetarchive.get_item(identifier, http_adapter_kwargs=IA_HTTP_ADAPTER_KWARGS)
    for file in item.get_files():
        if file.name == name:
            return int(getattr(file, "mtime", 0) or 0)
    return None


def has_withdrawn_text(identifier: str) -> bool:
    """IA shows the withdrawn notice and no identifier-bib; the files may still be clearing."""
    metadata = item_metadata(identifier) or {}
    return (
        metadata.get("title") == DEFAULT_WITHDRAWN_TITLE
        and metadata.get("description") == DEFAULT_WITHDRAWN_DESCRIPTION
        and "identifier-bib" not in metadata
    )


class RehearsalSheet:
    """Reads the Test Sheet through the production client; writes only via e2e_sheet."""

    def __init__(self, service: Any, target: E2ESheet, header: list[str]) -> None:
        self._service = service
        self._target = target
        self._header = header

    def grid(self) -> list[list[str]]:
        rows = SheetClient(self._service, self._target.sheet_id, self._target.data_tab).read_grid()
        return pad_grid(rows, len(self._header))

    def cell(self, grid: list[list[str]], row_index: int, column: str) -> str:
        return grid[row_index][self._header.index(column)]

    def edit(self, row_index: int, column: str, value: str) -> None:
        set_cell(self._service, self._target, row_index + 1, self._header.index(column), value)

    def tab_id(self, tab: str) -> int | None:
        return tab_ids(self._service, self._target.sheet_id).get(tab)

    def log_rows(self, tab: str) -> list[list[str]]:
        if self.tab_id(tab) is None:
            return []
        return SheetClient(self._service, self._target.sheet_id, tab).read_grid()


def preflight() -> None:
    if not REAL_KEY_PATH.is_file():
        pytest.fail(f"preflight: no service-account key at {REAL_KEY_PATH}; copy .ignored/google-service-account.json into this checkout")
    # In a subprocess: in-process, conftest has pointed IA_CONFIG_FILE at an empty file.
    has_credentials = subprocess.run(
        [sys.executable, "-c", "import internetarchive, sys; sys.exit(0 if internetarchive.get_session().access_key else 1)"],
        cwd=REPO_ROOT,
        env=cli_environment(),
        check=False,
    )
    if has_credentials.returncode != 0:
        pytest.fail("preflight: the ia library has no credentials; run `ia configure` on this machine")


def run_summary_timestamp(log_file: Path) -> str | None:
    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    return next((record["timestamp"] for record in records if record.get("record") == "run_summary"), None)


def failing_on_lock_errors(label: str, action: Callable[[], Returned]) -> Returned:
    """A held or lost lock fails `label` with the lock's message, not a traceback."""
    try:
        return action()
    except (LockHeld, LockLost) as error:
        pytest.fail(f"{label}: {error}")


def take_lock(service: Any, target: E2ESheet, log_dir: Path, request: Finalizers) -> RehearsalLock:
    """Registers the release at once, so it runs after every later finalizer; step 12's still needs the lock."""
    run = RunIdentity(host=socket.gethostname(), pid=os.getpid(), checkout=str(REPO_ROOT), log_dir=str(log_dir))
    lock = failing_on_lock_errors(STEP_0, lambda: acquire_lock(service, target, run, LOCK_LEASE))
    request.addfinalizer(lambda: failing_on_lock_errors("teardown", lock.release))
    if lock.took_over_from is not None:
        _print_for_console(f"{STEP_0}: took over the expired lock of the rehearsal {lock.took_over_from.describe()}")
    return lock


def check_in(lock: RehearsalLock, step: str) -> None:
    failing_on_lock_errors(step, lock.check_in)


def restore_broken_filename(sheet: RehearsalSheet, lock: RehearsalLock, filename: str) -> None:
    # After a takeover the row is the other run's; restoring it would break that run.
    # A restore after a check-in error runs inside its handler, so a failed restore still reports that error.
    try:
        lock.check_in()
    except LockLost as lost:
        pytest.fail(f"{STEP_12}: {lost}")
    except LockStillHeld:
        _put_back_filename(sheet, filename)
        raise
    except Exception as error:
        # The error leaves the holder unknown; restore only once the Sheet shows the lock is still this run's.
        if not lock.holds():
            pytest.fail(f"{STEP_12}: the lock is no longer this run's, so the filename was left alone ({error!r})")
        _put_back_filename(sheet, filename)
        raise
    _put_back_filename(sheet, filename)


def _put_back_filename(sheet: RehearsalSheet, filename: str) -> None:
    sheet.edit(BROKEN_ROW, "File Name", filename)
    expect(STEP_12, sheet.cell(sheet.grid(), BROKEN_ROW, "File Name") == filename, "the broken filename was not restored")


# ---------------------------------------------------------------------------
# test_upload_page_drives_a_real_run_end_to_end: the same Test Sheet and
# test_collection, but driven through a real upload_server over HTTP -
# proving the server -> subprocess -> real IA -> SSE chain, not just the CLI
# test_rehearsal already exercises directly.
# ---------------------------------------------------------------------------

# e2e_fixtures/sheet.json's Theme value for rows 1-4; e2e_fixtures/registry.json
# names "theme" as this project's batch_column. Row 5 has no Theme and rows 6-7
# are step 4b's own batch, so all three are out of scope for this batch.
UPLOAD_PAGE_BATCH = "E2E"
# Grid indexes of the batch's rows, all ready after the reset; only test_rehearsal breaks BROKEN_ROW.
UPLOAD_PAGE_ROWS = (FIRST_UPLOADED, SECOND_UPLOADED, BROKEN_ROW, THIRD_UPLOADED)

STEP_UPLOAD_PAGE_PREDICT = "upload page e2e - predict via validate --json (Task 16)"
STEP_UPLOAD_PAGE_RUN = "upload page e2e - drive a real run through the server (Task 16)"


def _restore_real_ia_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest's autouse fixtures poison IA_CONFIG_FILE/IA_ACCESS_KEY_* for
    every test, in this process, so no test reaches real IA by accident.
    upload_server's default spawn_upload starts the upload child with a copy
    of THIS process's environment (see _default_spawn_upload), so that child
    needs the real values restored here before POSTing /api/runs - undone
    automatically at teardown like everything else monkeypatch touches."""
    for name, value in REAL_IA_ENVIRONMENT.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def _reap_spawned_upload_child(run_dir: Path) -> None:
    """Safety net for a timed-out SSE wait: reaps ONLY the specific Popen this
    test's own run spawned - tracked by upload_server itself, keyed by
    run_dir (see upload_server._spawned_upload_processes) - never anything
    found by pid guesswork or image name. A graceful stop, then a second
    (hard-stop) request, then a direct kill of this exact handle; each step
    bounded, so teardown itself can never hang."""
    process = upload_server._spawned_upload_processes.get(run_dir)
    if process is None or process.poll() is not None:
        return
    for patience_seconds in (60, 30):
        stop_request.request_stop(process.pid)
        try:
            process.wait(timeout=patience_seconds)
            return
        except subprocess.TimeoutExpired:
            continue
    process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail(f"teardown: the spawned upload child (pid {process.pid}) survived a kill of its own handle")


@pytest.mark.e2e
def test_rehearsal(tmp_path, request):
    log_dir = tmp_path / "logs"
    fixture = load_fixture_grid(FIXTURE_SHEET)
    header = fixture[0]

    preflight()
    target = check_reset_allowed(E2E_REGISTRY, LIVE_REGISTRY)
    service = build_sheets_service(REAL_KEY_PATH)
    sheet = RehearsalSheet(service, target, header)
    lock = take_lock(service, target, log_dir, request)

    reset_test_sheet(service, target, fixture)
    expect(STEP_0, sheet.grid() == fixture, "the data tab does not match the fixture after the reset")
    expect(STEP_0, sheet.tab_id(target.upload_log_tab) is None, "Upload Log survived the reset")
    expect(STEP_0, sheet.tab_id(target.sync_log_tab) is None, "Sync Log survived the reset")

    result = run_cli(STEP_1, "validate", lock=lock)
    expect_run(STEP_1, result, 0, "7/7 rows passed", "missing theme")

    result = run_cli(STEP_2, "upload", "--write-identifier", "--limit", "1", lock=lock, log_dir=log_dir)
    expect_run(STEP_2, result, 0, "1 file(s) uploaded successfully, 0 error(s)")
    upload_log = sheet.log_rows(target.upload_log_tab)
    expect(STEP_2, upload_log[:1] == [LOG_TAB_HEADER], f"Upload Log header is {upload_log[:1]}")
    expect(STEP_2, [row[2] for row in upload_log[1:]] == ["summary"], f"Upload Log rows: {upload_log[1:]}")
    expect(STEP_2, upload_log[1][4] in result.stdout, f"summary detail {upload_log[1][4]!r} is not the console line")
    grid = sheet.grid()
    expect_recorded(STEP_2, sheet, grid, FIRST_UPLOADED, 1)
    upload_log_id = sheet.tab_id(target.upload_log_tab)

    result = run_cli(STEP_3, "upload", "--write-identifier", "--limit", "1", lock=lock, log_dir=log_dir)
    expect_run(STEP_3, result, 0, "1 file(s) uploaded successfully, 0 error(s)")
    upload_log = sheet.log_rows(target.upload_log_tab)
    expect(STEP_3, sheet.tab_id(target.upload_log_tab) == upload_log_id, "Upload Log was recreated, not appended to")
    expect(STEP_3, upload_log.count(LOG_TAB_HEADER) == 1, "Upload Log has more than one header row")
    expect(STEP_3, [row[2] for row in upload_log[1:]] == ["summary", "summary"], f"Upload Log rows: {upload_log[1:]}")
    grid = sheet.grid()
    expect(STEP_3, sheet.cell(grid, SECOND_UPLOADED, "ia_identifier") == "lcps-e2e-00002", "row 3 did not get lcps-e2e-00002")

    # Before this run's own edit; run_cli checks in again before the CLI's writes.
    check_in(lock, STEP_4)
    restored_filename = fixture[BROKEN_ROW][header.index("File Name")]
    # A finalizer, so a failing later step still leaves the Test Sheet valid.
    request.addfinalizer(lambda: restore_broken_filename(sheet, lock, restored_filename))
    sheet.edit(BROKEN_ROW, "File Name", BROKEN_FILENAME)
    result = run_cli(STEP_4, "upload", "--write-identifier", "--limit", "1", lock=lock, log_dir=log_dir)
    expect_run(STEP_4, result, 1, "1 file(s) uploaded successfully, 0 error(s)", "skipped (failed validation, or moved in the Sheet mid-run)")
    upload_log = sheet.log_rows(target.upload_log_tab)
    expect(STEP_4, [row[2] for row in upload_log[3:]] == ["summary", "skipped"], f"Upload Log rows: {upload_log[3:]}")
    expect(STEP_4, BROKEN_FILENAME in upload_log[4][4], f"skipped detail is {upload_log[4][4]!r}")
    grid = sheet.grid()
    expect(STEP_4, sheet.cell(grid, THIRD_UPLOADED, "ia_identifier") == "lcps-e2e-00003", "row 5 did not get lcps-e2e-00003")
    expect(STEP_4, not any(sheet.cell(grid, BROKEN_ROW, column) for column in UPLOAD_COLUMNS), "row 4 was marked uploaded")

    result = run_cli(STEP_4B, "upload", "--write-identifier", "--limit", "2", lock=lock, log_dir=log_dir)
    # Row 4 is still broken, so it is skipped again.
    expect_run(STEP_4B, result, 1, "2 file(s) uploaded successfully, 0 error(s)", "skipped (failed validation, or moved in the Sheet mid-run)")
    bars = re.findall(r"uploading (e2e-\d+\.jpg)", result.stderr)
    expect(STEP_4B, list(dict.fromkeys(bars)) == ["e2e-06.jpg", "e2e-07.jpg"], f"progress bars were for {bars}")
    upload_log = sheet.log_rows(target.upload_log_tab)
    expect(STEP_4B, [row[2] for row in upload_log[5:]] == ["summary", "skipped"], f"Upload Log rows: {upload_log[5:]}")
    grid = sheet.grid()
    expect(STEP_4B, sheet.cell(grid, FOURTH_UPLOADED, "ia_identifier") == "lcps-e2e-00004", "row 7 did not get lcps-e2e-00004")
    expect(STEP_4B, sheet.cell(grid, FIFTH_UPLOADED, "ia_identifier") == "lcps-e2e-00005", "row 8 did not get lcps-e2e-00005")
    expect(STEP_4B, not any(sheet.cell(grid, BROKEN_ROW, column) for column in UPLOAD_COLUMNS), "row 4 was marked uploaded")

    identifiers = {row: sheet.cell(grid, row, "ia_url").removeprefix(ITEM_URL_PREFIX) for row in UPLOADED_ROWS}
    for identifier in identifiers.values():
        wait_for_ia(STEP_5, f"item {identifier}", lambda identifier=identifier: item_metadata(identifier), lock=lock)

    grid_before_sync = sheet.grid()
    result = run_cli(STEP_6, "sync-metadata", "--dry-run", lock=lock, log_dir=log_dir)
    expect_run(STEP_6, result, 0)
    expect(STEP_6, sheet.grid() == grid_before_sync, "the dry run changed the Sheet")
    expect(STEP_6, sheet.tab_id(target.sync_log_tab) is None, "the dry run created Sync Log")

    check_in(lock, STEP_7)
    edited_title = f"E2E fixture 1 (edited {time.strftime('%Y%m%dT%H%M%S')})"
    sheet.edit(FIRST_UPLOADED, "Title", edited_title)
    result = run_cli(STEP_7, "sync-metadata", lock=lock, log_dir=log_dir)
    expect_run(STEP_7, result, 0, "1 item(s) updated successfully, 4 unchanged, 0 error(s)")
    sync_log = sheet.log_rows(target.sync_log_tab)
    expect(STEP_7, sync_log[:1] == [LOG_TAB_HEADER], f"Sync Log header is {sync_log[:1]}")
    expect(STEP_7, [row[2] for row in sync_log[1:]] == ["summary"], f"Sync Log rows: {sync_log[1:]}")
    grid_after_sync = sheet.grid()
    expect(
        STEP_7,
        all(sheet.cell(grid_after_sync, row, column) for row in UPLOADED_ROWS for column in SYNC_COLUMNS),
        "sync columns not stamped on every uploaded row",
    )

    first_identifier = identifiers[FIRST_UPLOADED]
    wait_for_ia(
        STEP_8,
        f"the edited title on {first_identifier}",
        lambda: True if (item_metadata(first_identifier) or {}).get("title") == edited_title else None,
        lock=lock,
    )

    result = run_cli(STEP_9, "sync-metadata", lock=lock, log_dir=log_dir)
    expect_run(STEP_9, result, 0, "nothing to sync")
    expect(STEP_9, len(sheet.log_rows(target.sync_log_tab)) == len(sync_log), "the quiet run appended to Sync Log")
    expect(STEP_9, sheet.grid() == grid_after_sync, "the quiet run changed the Sheet")

    # Row 3 stays withdrawn after the rehearsal, for the hand clear check (OPERATIONS).
    second_identifier = identifiers[SECOND_UPLOADED]
    # The restore's proof: the original reappears with a newer mtime, since 9a's deletes may still be queued.
    original_mtime = item_file_mtime(first_identifier, "e2e-01.jpg")
    expect(STEP_9A, bool(original_mtime), f"{first_identifier} lists no mtime for e2e-01.jpg before the withdraw")
    # A withdraw is refused while IA runs or holds a task on the item (the upload's derive, here).
    wait_until_idle(
        STEP_9A, (first_identifier, second_identifier), "the withdraw would be refused", lock=lock
    )
    check_in(lock, STEP_9A)
    sheet.edit(FIRST_UPLOADED, "Withdrawn", "yes")
    sheet.edit(SECOND_UPLOADED, "Withdrawn", "yes")
    result = run_cli(STEP_9A, "sync-metadata", lock=lock, log_dir=log_dir)
    expect_run(
        STEP_9A, result, 0,
        "2 items withdrawn (files deleted, text replaced)",
        "until every withdrawn item reports clear",
    )
    for original in ("e2e-01.jpg", "e2e-02.jpg"):
        accepted = [
            line for line in result.stdout.splitlines()
            if "Internet Archive accepted deletes: " in line and original in line
        ]
        expect(STEP_9A, accepted != [], f"no accepted delete of {original} in the run's output")
    sync_log = sheet.log_rows(target.sync_log_tab)
    expect(STEP_9A, [row[2] for row in sync_log[-3:]] == ["summary", "withdrawn", "withdrawn"], f"Sync Log rows: {sync_log[1:]}")
    grid = sheet.grid()
    for row in (FIRST_UPLOADED, SECOND_UPLOADED):
        expect(STEP_9A, sheet.cell(grid, row, "ia_withdrawn") != "", f"row {row + 1} ia_withdrawn was not stamped")
    for identifier in (first_identifier, second_identifier):
        wait_for_ia(
            STEP_9A,
            f"the withdrawn notice on {identifier}",
            lambda identifier=identifier: True if has_withdrawn_text(identifier) else None,
            lock=lock,
        )

    grid_before_recheck = sheet.grid()
    result = run_cli(STEP_9A2, "sync-metadata", lock=lock, log_dir=log_dir)
    expect_run(STEP_9A2, result, 0)
    expect(
        STEP_9A2,
        re.search(r"withdrawn items? (still clearing|clear \()", result.stdout) is not None,
        f"no re-check report in:\n{result.stdout}",
    )
    expect(STEP_9A2, sheet.grid() == grid_before_recheck, "the re-check run changed the Sheet")

    # A restore is refused while IA still has the withdrawal's tasks queued; never restore blind.
    wait_until_idle(STEP_9B, (first_identifier,), "the restore would be refused", lock=lock)
    check_in(lock, STEP_9B)
    sheet.edit(FIRST_UPLOADED, "Withdrawn", "no")
    result = run_cli(STEP_9B, "sync-metadata", lock=lock, log_dir=log_dir)
    expect_run(STEP_9B, result, 0, "1 item restored (file re-uploaded, text put back)")
    sync_log = sheet.log_rows(target.sync_log_tab)
    expect(STEP_9B, "restored" in [row[2] for row in sync_log[-3:]], f"Sync Log rows: {sync_log[1:]}")
    grid = sheet.grid()
    expect(STEP_9B, sheet.cell(grid, FIRST_UPLOADED, "ia_withdrawn") == "", "ia_withdrawn was not cleared")
    expect(STEP_9B, sheet.cell(grid, SECOND_UPLOADED, "ia_withdrawn") != "", "row 3 lost its ia_withdrawn")
    wait_for_ia(
        STEP_9B,
        f"the restored title on {first_identifier}",
        lambda: True if (item_metadata(first_identifier) or {}).get("title") == edited_title else None,
        lock=lock,
    )
    wait_for_ia(
        STEP_9B,
        f"a re-uploaded e2e-01.jpg in {first_identifier}'s file list",
        lambda: True if (item_file_mtime(first_identifier, "e2e-01.jpg") or 0) > (original_mtime or 0) else None,
        lock=lock,
    )

    # Steps 10 and 11 only read, but check in so a lock lost after step 9 fails as one.
    check_in(lock, STEP_10)
    for tab in target.log_tabs:
        for when, run, *_ in sheet.log_rows(tab)[1:]:
            log_file = log_dir / run
            expect(STEP_10, log_file.is_file(), f"{tab} names {run}, which is not in {log_dir}")
            summary_timestamp = run_summary_timestamp(log_file)
            expect(STEP_10, summary_timestamp is not None, f"{tab} names {run}, which has no run_summary record")
            expect(STEP_10, summary_timestamp == when, f"{tab} row for {run}: when {when!r} is not its run_summary timestamp")

    check_in(lock, STEP_11)
    allowed = {(row, column) for row in UPLOADED_ROWS for column in UPLOAD_COLUMNS + SYNC_COLUMNS}
    allowed |= {(FIRST_UPLOADED, "Title"), (BROKEN_ROW, "File Name")}
    allowed |= {(row, column) for row in (FIRST_UPLOADED, SECOND_UPLOADED) for column in WITHDRAW_COLUMNS}
    expect_only_allowed_changes(STEP_11, fixture, sheet.grid(), allowed)
    # Step 12 runs as the finalizer registered at step 4.


@pytest.mark.e2e
def test_upload_page_drives_a_real_run_end_to_end(tmp_path, request, monkeypatch):
    """A real upload_server (test mode), driven over HTTP exactly as the
    page's frontend would drive it: POST /api/runs, then stream
    /api/runs/current/output to `finished`. The page's own reported result
    must equal what validate --json independently predicts for the same
    batch - i.e. the page's result equals reality, not just its own say-so.
    The run records the batch's rows to the Test Sheet and touches nothing else.
    """
    fixture = load_fixture_grid(FIXTURE_SHEET)
    header = fixture[0]

    preflight()
    _restore_real_ia_environment(monkeypatch)
    target = check_reset_allowed(E2E_REGISTRY, LIVE_REGISTRY)
    service = build_sheets_service(REAL_KEY_PATH)
    sheet = RehearsalSheet(service, target, header)
    lock = take_lock(service, target, tmp_path / "logs", request)

    reset_test_sheet(service, target, fixture)
    expect(STEP_UPLOAD_PAGE_PREDICT, sheet.grid() == fixture, "the data tab does not match the fixture after the reset")

    prediction = run_cli(STEP_UPLOAD_PAGE_PREDICT, "validate", f"--batch={UPLOAD_PAGE_BATCH}", "--json", lock=lock)
    expect(STEP_UPLOAD_PAGE_PREDICT, prediction.returncode == 0, f"validate --json refused:\n{output_of(prediction)}")
    expected_succeeded = json.loads(prediction.stdout)["ready_to_upload"]
    expect(
        STEP_UPLOAD_PAGE_PREDICT,
        expected_succeeded == len(UPLOAD_PAGE_ROWS),
        f"validate --json predicts {expected_succeeded} rows ready for batch {UPLOAD_PAGE_BATCH!r}, "
        f"expected {len(UPLOAD_PAGE_ROWS)}; the fixture may have changed",
    )

    check_in(lock, STEP_UPLOAD_PAGE_RUN)
    config = upload_server.ServerConfig(
        project=E2E_PROJECT,
        registry=str(E2E_REGISTRY),
        live=False,
        port=0,
        repo_root=REPO_ROOT,
        page_dir=REPO_ROOT / "upload_page",
    )
    with upload_server.serve_in_thread(config) as base_url:
        status, body = _post_json(f"{base_url}/api/runs", {"batch": UPLOAD_PAGE_BATCH})
        expect(STEP_UPLOAD_PAGE_RUN, status == 202, f"POST /api/runs: expected 202, got {status}: {body!r}")

        run_dir = page_runs.newest_run_dir(config.logs_base)
        if run_dir is None:
            pytest.fail(f"{STEP_UPLOAD_PAGE_RUN}: no run folder was created after POST /api/runs")
        request.addfinalizer(lambda: _reap_spawned_upload_child(run_dir))

        # IA queue delays during the real upload are not defects, hence the
        # generous overall budget - never an assertion on how long it took.
        events = _read_sse(f"{base_url}/api/runs/current/output", stop_on="finished", timeout=CLI_TIMEOUT_SECONDS)

    # Before any check on the run, so a lock lost during the wait fails as one.
    check_in(lock, STEP_UPLOAD_PAGE_RUN)
    finished = [event for event in events if event.event == "finished"]
    expect(STEP_UPLOAD_PAGE_RUN, bool(finished), f"no 'finished' event within {CLI_TIMEOUT_SECONDS}s")
    ending = json.loads(finished[-1].data)["ending"]

    if ending["kind"] == "rate_limited":
        pytest.fail(f"{STEP_UPLOAD_PAGE_RUN}: Internet Archive is throttling uploads, not a defect in the tool; re-run later")
    expect(
        STEP_UPLOAD_PAGE_RUN,
        ending["kind"] == "completed",
        f"the run ended as {ending['kind']!r}, not 'completed': {ending}",
    )
    summary = ending["summary"]
    failed = len(summary.get("failures", []))
    expect(
        STEP_UPLOAD_PAGE_RUN,
        summary.get("succeeded") == expected_succeeded and failed == 0,
        f"the page reports {summary.get('succeeded')} succeeded / {failed} failed; "
        f"validate --json predicted {expected_succeeded} ready to upload, 0 failed",
    )

    # The page's _upload_argv adds --write-identifier in test mode, so the run
    # records the batch's rows; nothing else in the data tab may change.
    final = sheet.grid()
    for number, row in enumerate(UPLOAD_PAGE_ROWS, start=1):
        expect_recorded(STEP_UPLOAD_PAGE_RUN, sheet, final, row, number)
    allowed = {(row, column) for row in UPLOAD_PAGE_ROWS for column in UPLOAD_COLUMNS}
    expect_only_allowed_changes(STEP_UPLOAD_PAGE_RUN, fixture, final, allowed)

    # The recorded rows must read as done, so the page stops offering the batch.
    after = run_cli(STEP_UPLOAD_PAGE_RUN, "validate", f"--batch={UPLOAD_PAGE_BATCH}", "--json", lock=lock)
    expect(STEP_UPLOAD_PAGE_RUN, after.returncode == 0, f"validate --json refused after the run:\n{output_of(after)}")
    still_ready = json.loads(after.stdout)["ready_to_upload"]
    expect(STEP_UPLOAD_PAGE_RUN, still_ready == 0, f"validate --json still predicts {still_ready} rows ready after the run")

    upload_log = sheet.log_rows(target.upload_log_tab)
    expect(STEP_UPLOAD_PAGE_RUN, upload_log[:1] == [LOG_TAB_HEADER], f"Upload Log header is {upload_log[:1]}")
    expect(STEP_UPLOAD_PAGE_RUN, [row[2] for row in upload_log[1:]] == ["summary"], f"Upload Log rows: {upload_log[1:]}")
    expect(
        STEP_UPLOAD_PAGE_RUN,
        f"{expected_succeeded} file(s) uploaded successfully, 0 error(s)" in upload_log[1][4],
        f"Upload Log summary detail is {upload_log[1][4]!r}",
    )


@pytest.mark.e2e
def test_archive_org_answers_the_collection_check_as_it_assumes(monkeypatch):
    """The real answers check_ia_collection's verdicts rest on: mediatype
    `collection` for a collection, `{}` for an unknown identifier. Read-only,
    so it needs neither the rehearsal lock nor the Test Sheet."""
    _restore_real_ia_environment(monkeypatch)

    assert check_ia_collection("test_collection") == CollectionConfirmed()
    assert check_ia_collection("lcps-e2e-no-such-collection") == CollectionMissing()


OTHER_RUN = RunIdentity(host="other-host", pid=222, checkout="C:/checkouts/other", log_dir="C:/tmp/other/logs")
RESTORED_CELL = ("'Test Sheet'!A4", [["e2e-03.jpg"]])


class RecordingRequest:
    """Stands in for pytest's request; tear_down runs its finalizers last-registered first, as pytest does."""

    def __init__(self) -> None:
        self.finalizers: list[Callable[[], object]] = []

    def addfinalizer(self, finalizer: Callable[[], object]) -> None:
        self.finalizers.append(finalizer)

    def tear_down(self) -> None:
        for finalizer in reversed(self.finalizers):
            finalizer()


def held_lock(tmp_path: Path) -> tuple[FakeSheets, RehearsalLock, RecordingRequest]:
    sheets, request = FakeSheets(), RecordingRequest()
    return sheets, take_lock(sheets, FAKE_TARGET, tmp_path / "logs", request), request


def test_print_for_console_survives_a_non_utf8_console(monkeypatch):
    """A cp1252 console can't encode ia's progress-bar block char; this must not raise."""
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    _print_for_console("uploading e2e-01.jpg: 100%|██████████| 1/1")


def test_a_throttled_run_fails_as_ia_throttling_not_a_defect():
    throttled = subprocess.CompletedProcess(
        args=["ia_bulk.py", "upload"],
        returncode=1,
        stdout="0 file(s) uploaded successfully, 1 error(s)\n",
        stderr="stopped: Internet Archive asked us to slow down (HTTP 503) after 1 item\n",
    )

    with pytest.raises(pytest.fail.Exception, match="step 2: Internet Archive is throttling uploads"):
        expect_run("step 2", throttled, 0)


def test_a_throttled_run_fails_as_ia_throttling_even_when_exit_1_was_expected():
    throttled = subprocess.CompletedProcess(
        args=["ia_bulk.py", "upload"],
        returncode=1,
        stdout="0 file(s) uploaded successfully, 1 error(s)\n",
        stderr="stopped: Internet Archive asked us to slow down (HTTP 503) after 1 item\n",
    )

    with pytest.raises(pytest.fail.Exception, match="step 4: Internet Archive is throttling uploads"):
        expect_run("step 4", throttled, 1, "1 file(s) uploaded successfully, 0 error(s)")


CHANGES_FIXTURE = [["File Name", "ia_identifier"], ["a.jpg", ""], ["b.jpg", ""]]


def test_expect_only_allowed_changes_accepts_an_allowed_change():
    final = [["File Name", "ia_identifier"], ["a.jpg", "lcps-e2e-00001"], ["b.jpg", ""]]

    expect_only_allowed_changes("step 11", CHANGES_FIXTURE, final, {(1, "ia_identifier")})


def test_expect_only_allowed_changes_names_an_unexpected_cell_by_its_sheet_row():
    final = [["File Name", "ia_identifier"], ["a.jpg", "lcps-e2e-00001"], ["b.jpg", "lcps-e2e-00002"]]

    with pytest.raises(pytest.fail.Exception, match=r"step 11: unexpected changes:\nrow 3 'ia_identifier': '' -> 'lcps-e2e-00002'$"):
        expect_only_allowed_changes("step 11", CHANGES_FIXTURE, final, {(1, "ia_identifier")})


def test_expect_only_allowed_changes_rejects_a_missing_row():
    with pytest.raises(pytest.fail.Exception, match=r"step 11: the data tab has 2 rows, expected 3"):
        expect_only_allowed_changes("step 11", CHANGES_FIXTURE, CHANGES_FIXTURE[:2], set())


def test_expect_only_allowed_changes_rejects_a_cell_past_the_header():
    final = [["File Name", "ia_identifier"], ["a.jpg", "", "stray"], ["b.jpg", ""]]

    with pytest.raises(pytest.fail.Exception, match=r"step 11: rows \[2\] have cells past the header"):
        expect_only_allowed_changes("step 11", CHANGES_FIXTURE, final, set())


def test_a_cli_timeout_fails_with_the_step_and_partial_output(tmp_path, monkeypatch):
    _, lock, _ = held_lock(tmp_path)

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ia_bulk.py", timeout=1, output="uploading e2e-01.jpg", stderr=None)

    monkeypatch.setattr(sys.modules[__name__], "run_streaming", time_out)

    with pytest.raises(pytest.fail.Exception, match=r"(?s)step 2: ia_bulk.py upload did not finish.*uploading e2e-01\.jpg"):
        run_cli("step 2", "upload", lock=lock)


def _run_child(script: str, timeout: float = 30) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    echoed_stdout: list[str] = []
    echoed_stderr: list[str] = []
    result = run_streaming(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        timeout=timeout,
        echo_stdout=echoed_stdout.append,
        echo_stderr=echoed_stderr.append,
    )
    return result, echoed_stdout, echoed_stderr


def test_run_streaming_keeps_a_progress_bars_redraws_on_one_line():
    script = "import sys; sys.stderr.buffer.write(b'\\r 0%\\r100%\\n'); sys.stdout.buffer.write(b'done\\r\\n')"

    result, echoed_stdout, echoed_stderr = _run_child(script)

    assert (result.returncode, result.stdout, result.stderr) == (0, "done\n", "\r 0%\r100%\n")
    assert ("".join(echoed_stdout), "".join(echoed_stderr)) == ("done\n", "\r 0%\r100%\n")


def test_run_streaming_decodes_a_character_split_across_writes():
    block = "█".encode()
    script = (
        "import sys, time; err = sys.stderr.buffer; "
        f"err.write({block[:1]!r}); err.flush(); time.sleep(0.3); err.write({block[1:]!r})"
    )

    result, _, echoed_stderr = _run_child(script)

    assert result.stderr == "█"
    assert "".join(echoed_stderr) == "█"


def test_run_streaming_passes_the_exit_code_through():
    result, _, _ = _run_child("import sys; sys.exit(3)")

    assert result.returncode == 3


def test_run_streaming_echoes_before_the_child_exits_and_a_timeout_keeps_the_partial_output():
    script = "import sys, time; print('uploading e2e-01.jpg', flush=True); time.sleep(60)"
    echoed: list[str] = []

    with pytest.raises(subprocess.TimeoutExpired) as timed_out:
        run_streaming(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env=dict(os.environ),
            # Room for a slow interpreter start on a loaded machine.
            timeout=10,
            echo_stdout=echoed.append,
            echo_stderr=echoed.append,
        )

    assert "uploading e2e-01.jpg" in "".join(echoed)
    assert "uploading e2e-01.jpg" in timed_out.value.output


def test_run_streaming_echoes_a_crlf_split_across_reads_as_one_newline():
    script = "import sys, time; out = sys.stdout.buffer; out.write(b'done\\r'); out.flush(); time.sleep(0.3); out.write(b'\\n')"

    result, echoed_stdout, _ = _run_child(script)

    assert result.stdout == "done\n"
    assert "".join(echoed_stdout) == "done\n"


def test_run_streaming_keeps_draining_when_the_console_write_fails():
    def broken_console(_text: str) -> None:
        raise OSError("console gone")

    # Larger than a pipe buffer: an undrained pipe would block the child.
    script = "import sys; sys.stderr.buffer.write(b'x' * 200_000)"

    result = run_streaming(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        timeout=30,
        echo_stdout=broken_console,
        echo_stderr=broken_console,
    )

    assert len(result.stderr) == 200_000


def test_run_streaming_kills_the_child_when_interrupted(monkeypatch):
    children: list[subprocess.Popen[bytes]] = []
    original_wait = subprocess.Popen.wait

    def interrupted_wait(self: subprocess.Popen[bytes], timeout: float | None = None) -> int:
        if timeout is None:
            return original_wait(self)
        children.append(self)
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_wait)

    with pytest.raises(KeyboardInterrupt):
        _run_child("import time; time.sleep(60)")

    assert children[0].poll() is not None


def test_cli_environment_uses_the_real_ia_settings_and_the_current_proxy(monkeypatch):
    monkeypatch.setitem(REAL_IA_ENVIRONMENT, "IA_CONFIG_FILE", "real.ini")
    monkeypatch.setitem(REAL_IA_ENVIRONMENT, "IA_ACCESS_KEY_ID", None)
    monkeypatch.setenv("IA_ACCESS_KEY_ID", "hidden")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")

    environment = cli_environment()

    assert environment["IA_CONFIG_FILE"] == "real.ini"
    assert "IA_ACCESS_KEY_ID" not in environment
    assert environment["HTTPS_PROXY"] == "http://proxy.invalid:3128"


def test_real_ia_session_carries_the_cli_runs_credentials_not_this_processs_empty_config(monkeypatch, tmp_path):
    real_config = tmp_path / "real-ia.ini"
    real_config.write_text("\n".join(["[s3]", "access = real-access", "secret = real-secret", ""]), encoding="utf-8")
    monkeypatch.setitem(REAL_IA_ENVIRONMENT, "IA_CONFIG_FILE", str(real_config))
    monkeypatch.setitem(REAL_IA_ENVIRONMENT, "IA_ACCESS_KEY_ID", None)
    monkeypatch.setitem(REAL_IA_ENVIRONMENT, "IA_SECRET_ACCESS_KEY", None)
    config_before = os.environ["IA_CONFIG_FILE"]

    session = real_ia_session()

    assert (session.access_key, session.secret_key) == ("real-access", "real-secret")
    assert os.environ["IA_CONFIG_FILE"] == config_before


def test_real_ia_session_prefers_the_cli_runs_key_environment(monkeypatch):
    monkeypatch.setitem(REAL_IA_ENVIRONMENT, "IA_ACCESS_KEY_ID", "env-access")
    monkeypatch.setitem(REAL_IA_ENVIRONMENT, "IA_SECRET_ACCESS_KEY", "env-secret")

    session = real_ia_session()

    assert (session.access_key, session.secret_key) == ("env-access", "env-secret")
    assert "IA_ACCESS_KEY_ID" not in os.environ


class CatalogTask:
    def __init__(self, status: str) -> None:
        self.task_dict = {"status": status, "cmd": "derive.php"}


def test_ia_task_state_asks_ia_for_the_catalog_with_the_credentialed_session(monkeypatch):
    session = object()
    monkeypatch.setattr("test_e2e_rehearsal.real_ia_session", lambda: session)
    calls: list[dict[str, Any]] = []

    def fake_get_tasks(**kwargs: Any) -> set[CatalogTask]:
        calls.append(kwargs)
        return {CatalogTask("queued"), CatalogTask("running")}

    monkeypatch.setattr(internetarchive, "get_tasks", fake_get_tasks)

    assert ia_task_state("lcps-x-00001") == TaskState(queued=("derive.php",), running=("derive.php",))
    assert calls[0]["archive_session"] is session
    assert calls[0]["identifier"] == "lcps-x-00001"
    assert calls[0]["params"] == {"catalog": 1, "history": 0}


def test_open_tasks_names_every_item_with_any_task_open_even_a_queued_one(monkeypatch):
    """A queued derive can start running before the CLI's own check, so only idle is safe."""
    states = {
        "idle": TaskState(),
        "queued": TaskState(queued=("derive.php",)),
        "running": TaskState(running=("derive.php",)),
        "paused": TaskState(paused=("archive.php",)),
    }
    monkeypatch.setattr("test_e2e_rehearsal.ia_task_state", states.__getitem__)

    assert open_tasks(list(states)) == {name: states[name] for name in ("queued", "running", "paused")}


def test_still_busy_message_says_to_re_run_later_while_ia_is_working():
    busy = {"a": TaskState(queued=("derive.php",)), "b": TaskState(running=("derive.php",))}

    assert still_busy_message(busy, "the withdraw would be refused") == (
        "a and b still busy at Internet Archive, so the withdraw would be refused; re-run later"
    )


def test_still_busy_message_says_ia_staff_must_release_a_paused_task():
    busy = {"a": TaskState(paused=("archive.php",)), "b": TaskState(running=("derive.php",))}

    assert still_busy_message(busy, "the restore would be refused") == (
        "a and b still busy at Internet Archive, so the restore would be refused: Internet Archive "
        "has paused 1 task(s) on a; IA staff must release them - re-running won't help until they "
        "do (see docs/OPERATIONS.md, 'Withdrawing an item')"
    )


def test_waiting_until_idle_times_out_naming_what_ia_still_holds(tmp_path, monkeypatch):
    _, lock, _ = held_lock(tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "IA_POLL_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        "test_e2e_rehearsal.ia_task_state",
        {"a": TaskState(), "b": TaskState(paused=("archive.php",))}.__getitem__,
    )

    with pytest.raises(
        pytest.fail.Exception,
        match=re.escape(
            "step 9a: b still busy at Internet Archive, so the withdraw would be refused: Internet "
            "Archive has paused 1 task(s) on b; IA staff must release them"
        ),
    ):
        wait_until_idle("step 9a", ("a", "b"), "the withdraw would be refused", lock=lock)


def test_waiting_until_idle_returns_once_no_task_is_open(tmp_path, monkeypatch):
    _, lock, _ = held_lock(tmp_path)
    answers = iter([TaskState(queued=("derive.php",)), TaskState()])
    monkeypatch.setattr(sys.modules[__name__], "IA_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("test_e2e_rehearsal.ia_task_state", lambda identifier: next(answers))

    wait_until_idle("step 9b", ("a",), "the restore would be refused", lock=lock)

    with pytest.raises(StopIteration):
        next(answers)


def test_run_summary_timestamp_is_none_without_a_run_summary(tmp_path):
    log_file = tmp_path / "run.jsonl"
    log_file.write_text('{"record": "row"}\n', encoding="utf-8")

    assert run_summary_timestamp(log_file) is None


def test_an_unthrottled_wrong_exit_still_fails_as_a_wrong_exit():
    failed = subprocess.CompletedProcess(args=["ia_bulk.py", "upload"], returncode=1, stdout="", stderr="")

    with pytest.raises(pytest.fail.Exception, match="step 2: expected exit 0, got 1"):
        expect_run("step 2", failed, 0)


def test_a_held_lock_fails_step_0_naming_the_other_run(tmp_path):
    sheets, request = FakeSheets(), RecordingRequest()
    acquire_lock(sheets, FAKE_TARGET, OTHER_RUN, LOCK_LEASE)

    with pytest.raises(pytest.fail.Exception, match=re.escape(f"{STEP_0}: another e2e rehearsal holds the Test Sheet")):
        take_lock(sheets, FAKE_TARGET, tmp_path / "logs", request)

    assert request.finalizers == []


def test_the_lock_is_released_after_the_step_12_restore_has_used_it(tmp_path):
    sheets, lock, request = held_lock(tmp_path)
    sheet = RehearsalSheet(sheets, FAKE_TARGET, ["File Name"])
    request.addfinalizer(lambda: restore_broken_filename(sheet, lock, "e2e-03.jpg"))

    request.tear_down()

    assert sheets.value_updates == [RESTORED_CELL]
    assert LOCK_TAB not in sheets.tabs


def test_restoring_the_filename_writes_nothing_once_the_lock_is_lost(tmp_path):
    sheets, lock, _ = held_lock(tmp_path)
    sheets.delete_tab(LOCK_TAB)

    with pytest.raises(pytest.fail.Exception, match=re.escape(f"{STEP_12}: this run lost the Test Sheet lock")):
        restore_broken_filename(RehearsalSheet(sheets, FAKE_TARGET, ["File Name"]), lock, "e2e-03.jpg")

    assert sheets.value_updates == []


def test_restoring_the_filename_still_restores_after_a_check_in_error_that_is_not_a_lost_lock(tmp_path):
    sheets, lock, _ = held_lock(tmp_path)
    error = http_error("Internal error encountered.", status=500)
    sheets.fail_next_batch = error

    def lose_the_network() -> None:
        sheets.fail_next_get = TimeoutError("timed out")

    # The check-in's own re-read shows the lock held; a second read would fail.
    sheets.after_next_get = lose_the_network

    with pytest.raises(LockStillHeld) as raised:
        restore_broken_filename(RehearsalSheet(sheets, FAKE_TARGET, ["File Name"]), lock, "e2e-03.jpg")

    assert raised.value.__cause__ is error
    assert sheets.value_updates == [RESTORED_CELL]


def test_a_restore_that_fails_after_a_check_in_error_still_reports_the_check_in_error(tmp_path):
    sheets, lock, _ = held_lock(tmp_path)
    sheets.fail_next_batch = http_error("Internal error encountered.", status=500)
    sheets.delete_tab(FAKE_TARGET.data_tab)

    with pytest.raises(HttpError, match="Unable to parse range") as raised:
        restore_broken_filename(RehearsalSheet(sheets, FAKE_TARGET, ["File Name"]), lock, "e2e-03.jpg")

    assert isinstance(raised.value.__context__, LockStillHeld)


def test_restoring_the_filename_after_a_check_in_whose_re_read_failed_restores_a_lock_still_held(tmp_path):
    sheets, lock, _ = held_lock(tmp_path)
    sheets.fail_next_batch = http_error("Internal error encountered.", status=500)
    sheets.fail_next_get = TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        restore_broken_filename(RehearsalSheet(sheets, FAKE_TARGET, ["File Name"]), lock, "e2e-03.jpg")

    assert sheets.value_updates == [RESTORED_CELL]


def test_restoring_the_filename_after_a_check_in_whose_re_read_failed_leaves_another_runs_row_alone(tmp_path):
    sheets, lock, _ = held_lock(tmp_path)
    sheets.delete_tab(LOCK_TAB)
    acquire_lock(sheets, FAKE_TARGET, OTHER_RUN, LOCK_LEASE)
    sheets.fail_next_get = TimeoutError("timed out")

    with pytest.raises(pytest.fail.Exception, match=re.escape(f"{STEP_12}: the lock is no longer this run's")):
        restore_broken_filename(RehearsalSheet(sheets, FAKE_TARGET, ["File Name"]), lock, "e2e-03.jpg")

    assert sheets.value_updates == []


def test_running_the_cli_checks_in_first(tmp_path, monkeypatch):
    sheets, lock, _ = held_lock(tmp_path)
    first_tab_id = lock.tab_id
    tab_id_at_start: list[int] = []

    def started(argv, **_kwargs):
        tab_id_at_start.append(sheets.tabs[LOCK_TAB])
        return subprocess.CompletedProcess(argv, 0, "", "")

    # run_streaming, not subprocess: an unstubbed spawn would start a real upload.
    monkeypatch.setattr(sys.modules[__name__], "run_streaming", started)

    run_cli("step 2", "upload", lock=lock)

    assert tab_id_at_start == [lock.tab_id]
    assert lock.tab_id != first_tab_id


def test_running_the_cli_after_the_lock_was_lost_fails_before_the_cli_starts(tmp_path, monkeypatch):
    sheets, lock, _ = held_lock(tmp_path)
    sheets.delete_tab(LOCK_TAB)
    started: list = []
    monkeypatch.setattr(sys.modules[__name__], "run_streaming", lambda argv, **_kwargs: started.append(argv))

    with pytest.raises(pytest.fail.Exception, match=re.escape("step 2: this run lost the Test Sheet lock")):
        run_cli("step 2", "upload", lock=lock)

    assert started == []


def test_waiting_for_ia_after_the_lock_was_lost_fails_before_probing(tmp_path):
    sheets, lock, _ = held_lock(tmp_path)
    sheets.delete_tab(LOCK_TAB)
    probes: list = []

    with pytest.raises(pytest.fail.Exception, match=re.escape("step 5: this run lost the Test Sheet lock")):
        wait_for_ia("step 5", "item zztest-x", lambda: probes.append(1) or True, lock=lock)

    assert probes == []


def test_waiting_for_ia_that_times_out_says_what_it_was_given(tmp_path, monkeypatch):
    _, lock, _ = held_lock(tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "IA_POLL_TIMEOUT_SECONDS", 0)

    with pytest.raises(pytest.fail.Exception, match=re.escape("queue for x is still busy; re-run later")):
        wait_for_ia("step 9b", "x's queue empty", lambda: None, lock=lock, on_timeout="queue for x is still busy; re-run later")


def test_item_file_mtime_reads_the_named_file_and_is_none_when_it_is_absent(monkeypatch):
    class Listed:
        def __init__(self, name: str, mtime: str | None) -> None:
            self.name = name
            if mtime is not None:
                self.mtime = mtime

    class Item:
        def get_files(self):
            return [Listed("e2e-01.jpg_meta.xml", "5"), Listed("e2e-01.jpg", "1700000000"), Listed("e2e-02.jpg", None)]

    monkeypatch.setattr(internetarchive, "get_item", lambda identifier, **_kwargs: Item())

    assert item_file_mtime("x", "e2e-01.jpg") == 1700000000
    assert item_file_mtime("x", "e2e-02.jpg") == 0
    assert item_file_mtime("x", "e2e-03.jpg") is None
