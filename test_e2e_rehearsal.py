"""E2E rehearsal: the real CLI against the real Test Sheet and IA's test_collection.

Opt-in, takes minutes: python -m pytest test_e2e_rehearsal.py --run-e2e -v -s
Each step label names the hand check it replaces.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

import google_auth
from e2e_sheet import (
    E2ESheet,
    build_sheets_service,
    check_reset_allowed,
    load_fixture_grid,
    pad_grid,
    reset_test_sheet,
    set_cell,
    tab_ids,
)
from ia_bulk import ITEM_URL_PREFIX, fetch_current_metadata
from log_tab import LOG_TAB_HEADER
from sheet_client import SheetClient

REPO_ROOT = Path(__file__).resolve().parent
E2E_REGISTRY = REPO_ROOT / "e2e_fixtures" / "registry.json"
LIVE_REGISTRY = REPO_ROOT / "projects_registry.json"
FIXTURE_SHEET = REPO_ROOT / "e2e_fixtures" / "sheet.json"
# Both read at import, before conftest's autouse fixtures hide the real key and IA config from each test.
REAL_KEY_PATH = google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH
CLI_ENVIRONMENT = {**os.environ, "PYTHONIOENCODING": "utf-8"}

IA_POLL_INTERVAL_SECONDS = 15
IA_POLL_TIMEOUT_SECONDS = 600
CLI_TIMEOUT_SECONDS = 900

UPLOAD_COLUMNS = ("ia_identifier", "ia_uploaded", "ia_url", "ia_identifier_bib")
SYNC_COLUMNS = ("ia_sync_hash", "ia_last_synced.")
BROKEN_FILENAME = "does-not-exist.jpg"

# Grid indexes; header is 0, so Sheet row = index + 1.
FIRST_UPLOADED, SECOND_UPLOADED, BROKEN_ROW, THIRD_UPLOADED, NOT_READY_ROW = 1, 2, 3, 4, 5
UPLOADED_ROWS = (FIRST_UPLOADED, SECOND_UPLOADED, THIRD_UPLOADED)

STEP_0 = "step 0 - reset (OPERATIONS §2 hand reset; 'Rehearsing the log tabs' intro)"
STEP_1 = "step 1 - validate (DEPLOYMENT §16 step 1; OPERATIONS §1)"
STEP_2 = "step 2 - upload (OPERATIONS 'Rehearsing the log tabs' step 1, first run)"
STEP_3 = "step 3 - upload again (OPERATIONS 'Rehearsing the log tabs' step 1, second run)"
STEP_4 = "step 4 - problem row (OPERATIONS 'Rehearsing the log tabs' step 2)"
STEP_5 = "step 5 - items exist on IA (OPERATIONS pre-live checklist: zztest item eyeballed)"
STEP_6 = "step 6 - sync dry run (DEPLOYMENT §16 step 2)"
STEP_7 = "step 7 - sync an edit (OPERATIONS 'Rehearsing the log tabs' step 3, first run)"
STEP_8 = "step 8 - edit reached IA (OPERATIONS pre-live checklist: zztest item eyeballed)"
STEP_9 = "step 9 - quiet sync (OPERATIONS 'Rehearsing the log tabs' step 3, second run)"
STEP_10 = "step 10 - tabs match log files (OPERATIONS 'Rehearsing the log tabs' step 4)"
STEP_11 = "step 11 - only expected cells changed (OPERATIONS 'Rehearsing the log tabs' step 5)"
STEP_12 = "step 12 - restore the broken filename (OPERATIONS 'Rehearsing the log tabs' step 2: put the cell back)"

Found = TypeVar("Found")


def _print_for_console(text: str) -> None:
    """A cp1252 console can't show ia's progress-bar block char; escape what it can't show instead of raising."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="backslashreplace").decode(encoding, errors="replace"))


def run_cli(step: str, command: str, *flags: str, log_dir: Path | None = None) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "ia_bulk.py", command, *flags, "--registry", str(E2E_REGISTRY), "--project", "e2e"]
    if log_dir is not None:
        argv += ["--log-dir", str(log_dir)]
    result = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=CLI_ENVIRONMENT,
        timeout=CLI_TIMEOUT_SECONDS,
        check=False,
    )
    _print_for_console(f"\n===== {step}\n$ ia_bulk.py {command} {' '.join(flags)}\n{result.stdout}{result.stderr}")
    return result


def output_of(result: subprocess.CompletedProcess[str]) -> str:
    return f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"


def expect(step: str, condition: bool, message: str) -> None:
    if not condition:
        pytest.fail(f"{step}: {message}")


def expect_run(step: str, result: subprocess.CompletedProcess[str], exit_code: int, *texts: str) -> None:
    if result.returncode != exit_code:
        pytest.fail(f"{step}: expected exit {exit_code}, got {result.returncode}\n{output_of(result)}")
    for text in texts:
        if text not in result.stdout:
            pytest.fail(f"{step}: expected {text!r} in stdout\n{output_of(result)}")


def wait_for_ia(step: str, description: str, probe: Callable[[], Found | None]) -> Found:
    deadline = time.monotonic() + IA_POLL_TIMEOUT_SECONDS
    while True:
        found = probe()
        if found is not None:
            return found
        if time.monotonic() >= deadline:
            pytest.fail(f"{step}: IA did not show {description} within {IA_POLL_TIMEOUT_SECONDS // 60} min")
        time.sleep(IA_POLL_INTERVAL_SECONDS)


def item_metadata(identifier: str) -> dict[str, Any] | None:
    return fetch_current_metadata(identifier) or None


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
        set_cell(self._service, self._target, row_index + 1, self._header.index(column) + 1, value)

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
        env=CLI_ENVIRONMENT,
        check=False,
    )
    if has_credentials.returncode != 0:
        pytest.fail("preflight: the ia library has no credentials; run `ia configure` on this machine")


def run_summary_timestamp(log_file: Path) -> str:
    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    return next(record["timestamp"] for record in records if record.get("record") == "run_summary")


@pytest.mark.e2e
def test_rehearsal(tmp_path):
    log_dir = tmp_path / "logs"
    fixture = load_fixture_grid(FIXTURE_SHEET)
    header = fixture[0]

    preflight()
    target = check_reset_allowed(E2E_REGISTRY, LIVE_REGISTRY)
    service = build_sheets_service(REAL_KEY_PATH)
    sheet = RehearsalSheet(service, target, header)

    reset_test_sheet(service, target, fixture)
    expect(STEP_0, sheet.grid() == fixture, "the data tab does not match the fixture after the reset")
    expect(STEP_0, sheet.tab_id(target.upload_log_tab) is None, "Upload Log survived the reset")
    expect(STEP_0, sheet.tab_id(target.sync_log_tab) is None, "Sync Log survived the reset")

    result = run_cli(STEP_1, "validate")
    expect_run(STEP_1, result, 0, "5/5 rows passed", "missing theme")

    result = run_cli(STEP_2, "upload", "--write-identifier", "--limit", "1", log_dir=log_dir)
    expect_run(STEP_2, result, 0, "1 file(s) uploaded successfully, 0 error(s)")
    upload_log = sheet.log_rows(target.upload_log_tab)
    expect(STEP_2, upload_log[:1] == [LOG_TAB_HEADER], f"Upload Log header is {upload_log[:1]}")
    expect(STEP_2, [row[2] for row in upload_log[1:]] == ["summary"], f"Upload Log rows: {upload_log[1:]}")
    expect(STEP_2, upload_log[1][4] in result.stdout, f"summary detail {upload_log[1][4]!r} is not the console line")
    grid = sheet.grid()
    expect(STEP_2, sheet.cell(grid, FIRST_UPLOADED, "ia_identifier") == "lcps-e2e-00001", "row 2 did not get lcps-e2e-00001")
    first_url = sheet.cell(grid, FIRST_UPLOADED, "ia_url")
    expect(
        STEP_2,
        first_url.startswith(f"{ITEM_URL_PREFIX}zztest-") and first_url.endswith("-lcps-e2e-00001"),
        f"row 2 ia_url is {first_url!r}",
    )
    expect(STEP_2, all(sheet.cell(grid, FIRST_UPLOADED, column) for column in UPLOAD_COLUMNS), "row 2 upload cells incomplete")
    upload_log_id = sheet.tab_id(target.upload_log_tab)

    result = run_cli(STEP_3, "upload", "--write-identifier", "--limit", "1", log_dir=log_dir)
    expect_run(STEP_3, result, 0, "1 file(s) uploaded successfully, 0 error(s)")
    upload_log = sheet.log_rows(target.upload_log_tab)
    expect(STEP_3, sheet.tab_id(target.upload_log_tab) == upload_log_id, "Upload Log was recreated, not appended to")
    expect(STEP_3, upload_log.count(LOG_TAB_HEADER) == 1, "Upload Log has more than one header row")
    expect(STEP_3, [row[2] for row in upload_log[1:]] == ["summary", "summary"], f"Upload Log rows: {upload_log[1:]}")
    grid = sheet.grid()
    expect(STEP_3, sheet.cell(grid, SECOND_UPLOADED, "ia_identifier") == "lcps-e2e-00002", "row 3 did not get lcps-e2e-00002")

    sheet.edit(BROKEN_ROW, "File Name", BROKEN_FILENAME)
    result = run_cli(STEP_4, "upload", "--write-identifier", "--limit", "1", log_dir=log_dir)
    expect_run(STEP_4, result, 1, "1 file(s) uploaded successfully, 0 error(s)", "skipped (failed validation)")
    upload_log = sheet.log_rows(target.upload_log_tab)
    expect(STEP_4, [row[2] for row in upload_log[3:]] == ["summary", "skipped"], f"Upload Log rows: {upload_log[3:]}")
    expect(STEP_4, BROKEN_FILENAME in upload_log[4][4], f"skipped detail is {upload_log[4][4]!r}")
    grid = sheet.grid()
    expect(STEP_4, sheet.cell(grid, THIRD_UPLOADED, "ia_identifier") == "lcps-e2e-00003", "row 5 did not get lcps-e2e-00003")
    expect(STEP_4, not any(sheet.cell(grid, BROKEN_ROW, column) for column in UPLOAD_COLUMNS), "row 4 was marked uploaded")

    identifiers = {row: sheet.cell(grid, row, "ia_url").removeprefix(ITEM_URL_PREFIX) for row in UPLOADED_ROWS}
    for identifier in identifiers.values():
        wait_for_ia(STEP_5, f"item {identifier}", lambda identifier=identifier: item_metadata(identifier))

    grid_before_sync = sheet.grid()
    result = run_cli(STEP_6, "sync-metadata", "--dry-run", log_dir=log_dir)
    expect_run(STEP_6, result, 0)
    expect(STEP_6, sheet.grid() == grid_before_sync, "the dry run changed the Sheet")
    expect(STEP_6, sheet.tab_id(target.sync_log_tab) is None, "the dry run created Sync Log")

    edited_title = f"E2E fixture 1 (edited {time.strftime('%Y%m%dT%H%M%S')})"
    sheet.edit(FIRST_UPLOADED, "Title", edited_title)
    result = run_cli(STEP_7, "sync-metadata", log_dir=log_dir)
    expect_run(STEP_7, result, 0, "1 item(s) updated successfully, 2 unchanged, 0 error(s)")
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
    )

    result = run_cli(STEP_9, "sync-metadata", log_dir=log_dir)
    expect_run(STEP_9, result, 0, "nothing to sync")
    expect(STEP_9, len(sheet.log_rows(target.sync_log_tab)) == len(sync_log), "the quiet run appended to Sync Log")
    expect(STEP_9, sheet.grid() == grid_after_sync, "the quiet run changed the Sheet")

    for tab in target.log_tabs:
        for when, run, *_ in sheet.log_rows(tab)[1:]:
            log_file = log_dir / run
            expect(STEP_10, log_file.is_file(), f"{tab} names {run}, which is not in {log_dir}")
            expect(STEP_10, run_summary_timestamp(log_file) == when, f"{tab} row for {run}: when {when!r} is not its run_summary timestamp")

    allowed = {(row, column) for row in UPLOADED_ROWS for column in UPLOAD_COLUMNS + SYNC_COLUMNS}
    allowed |= {(FIRST_UPLOADED, "Title"), (BROKEN_ROW, "File Name")}
    final = sheet.grid()
    expect(STEP_11, len(final) == len(fixture), f"the data tab has {len(final)} rows, expected {len(fixture)}")
    unexpected = [
        f"row {row_index + 1} {column!r}: {fixture[row_index][column_index]!r} -> {final[row_index][column_index]!r}"
        for row_index in range(len(fixture))
        for column_index, column in enumerate(header)
        if final[row_index][column_index] != fixture[row_index][column_index] and (row_index, column) not in allowed
    ]
    expect(STEP_11, not unexpected, "unexpected changes:\n" + "\n".join(unexpected))

    restored_filename = fixture[BROKEN_ROW][header.index("File Name")]
    sheet.edit(BROKEN_ROW, "File Name", restored_filename)
    grid_after_restore = sheet.grid()
    expect(
        STEP_12,
        sheet.cell(grid_after_restore, BROKEN_ROW, "File Name") == restored_filename,
        "the broken filename was not restored",
    )


def test_print_for_console_survives_a_non_utf8_console(monkeypatch):
    """A cp1252 console can't encode ia's progress-bar block char; this must not raise."""
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    _print_for_console("uploading e2e-01.jpg: 100%|██████████| 1/1")
