"""One e2e rehearsal at a time: a lock tab on the Test Sheet (test_e2e_rehearsal.py).

A run owns the lock through the sheetId of the tab it created. Every write after
that targets that id, so a lock another run has since taken over is never touched.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from googleapiclient.errors import HttpError

from e2e_sheet import LOCK_TAB, E2ESheet, tab_ids
from sheet_client import quote_tab

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# The lock tab's rows, in order; a check-in rewrites the rows from "checked in" on.
FIELDS = ("host", "pid", "checkout", "log dir", "started", "checked in", "expires")
CHECK_IN_ROW = FIELDS.index("checked in")
MAX_SHEET_ID = 2**31 - 1


class LockHeld(Exception):
    """Another rehearsal holds the lock, or the lock tab names no rehearsal."""


class LockLost(Exception):
    """This run's lock tab is gone: another run took it over, or it was deleted."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_time(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_time(text: str) -> datetime:
    return datetime.strptime(text, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RunIdentity:
    host: str
    pid: int
    checkout: str
    log_dir: str


@dataclass(frozen=True)
class LockHolder:
    run: RunIdentity
    started: datetime
    checked_in: datetime
    expires: datetime

    def rows(self) -> list[list[str]]:
        values = (
            self.run.host,
            str(self.run.pid),
            self.run.checkout,
            self.run.log_dir,
            format_time(self.started),
            format_time(self.checked_in),
            format_time(self.expires),
        )
        return [[field, value] for field, value in zip(FIELDS, values)]

    @classmethod
    def from_rows(cls, rows: list[list[str]]) -> LockHolder | None:
        """None for a tab this module did not write."""
        cells = {row[0]: row[1] for row in rows if len(row) >= 2}
        try:
            return cls(
                run=RunIdentity(host=cells["host"], pid=int(cells["pid"]), checkout=cells["checkout"], log_dir=cells["log dir"]),
                started=parse_time(cells["started"]),
                checked_in=parse_time(cells["checked in"]),
                expires=parse_time(cells["expires"]),
            )
        except (KeyError, ValueError):
            return None

    def describe(self) -> str:
        return (
            f"started {format_time(self.started)} on {self.run.host} (pid {self.run.pid}) from {self.run.checkout}, "
            f"last checked in {format_time(self.checked_in)}, logs in {self.run.log_dir}"
        )


@dataclass(frozen=True)
class _LockTab:
    tab_id: int
    holder: LockHolder | None


def _read_lock_tab(service: Any, target: E2ESheet) -> _LockTab | None:
    tab_id = tab_ids(service, target.sheet_id).get(LOCK_TAB)
    if tab_id is None:
        return None
    response = service.spreadsheets().values().get(spreadsheetId=target.sheet_id, range=quote_tab(LOCK_TAB)).execute()
    return _LockTab(tab_id, LockHolder.from_rows(response.get("values", [])))


def _refusal(lock_tab: _LockTab, now: datetime) -> LockHeld | None:
    """Why this run may not take `lock_tab`; None once it has expired."""
    holder = lock_tab.holder
    if holder is None:
        return LockHeld(f"the '{LOCK_TAB}' tab on the Test Sheet names no rehearsal; if none is running, delete the tab by hand")
    if now < holder.expires:
        return LockHeld(
            f"another e2e rehearsal holds the Test Sheet: {holder.describe()}. Wait for it to finish and re-run. "
            f"Its lock expires at {format_time(holder.expires)} if it stops checking in; if it is not running, "
            f"delete the '{LOCK_TAB}' tab on the Test Sheet to clear it now"
        )
    return None


def _write_rows(tab_id: int, first_row: int, rows: list[list[str]]) -> dict[str, Any]:
    return {
        "updateCells": {
            "start": {"sheetId": tab_id, "rowIndex": first_row, "columnIndex": 0},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": value}} for value in row]} for row in rows],
            "fields": "userEnteredValue",
        }
    }


def _batch_update(service: Any, target: E2ESheet, requests: list[dict[str, Any]]) -> None:
    """All or nothing: the API applies no request of a batch if any one is invalid."""
    service.spreadsheets().batchUpdate(spreadsheetId=target.sheet_id, body={"requests": requests}).execute()


class RehearsalLock:
    """This run's hold on the Test Sheet; build it via acquire_lock."""

    def __init__(
        self,
        service: Any,
        target: E2ESheet,
        tab_id: int,
        holder: LockHolder,
        lease: timedelta,
        clock: Callable[[], datetime],
        took_over_from: LockHolder | None,
    ) -> None:
        self._service = service
        self._target = target
        self._lease = lease
        self._clock = clock
        self.tab_id = tab_id
        self.holder = holder
        self.took_over_from = took_over_from

    def check_in(self) -> None:
        """Extends the lease from now; raises LockLost if the tab is no longer this run's."""
        now = self._clock()
        holder = replace(self.holder, checked_in=now, expires=now + self._lease)
        self._on_own_tab([_write_rows(self.tab_id, CHECK_IN_ROW, holder.rows()[CHECK_IN_ROW:])])
        self.holder = holder

    def release(self) -> None:
        """Deletes this run's lock tab; raises LockLost, touching nothing, if it is no longer this run's."""
        self._on_own_tab([{"deleteSheet": {"sheetId": self.tab_id}}])

    def _on_own_tab(self, requests: list[dict[str, Any]]) -> None:
        try:
            _batch_update(self._service, self._target, requests)
        except HttpError:
            now_on_sheet = _read_lock_tab(self._service, self._target)
            if now_on_sheet is None or now_on_sheet.tab_id != self.tab_id:
                raise LockLost(self._lost_message(now_on_sheet)) from None
            raise

    def _lost_message(self, now_on_sheet: _LockTab | None) -> str:
        if now_on_sheet is None:
            cause = f"the '{LOCK_TAB}' tab was deleted"
        elif now_on_sheet.holder is None:
            cause = f"the '{LOCK_TAB}' tab was replaced with one that names no rehearsal"
        else:
            cause = f"another e2e rehearsal took it over ({now_on_sheet.holder.describe()})"
        return (
            f"this run lost the Test Sheet lock: {cause}. Any failure in this run since its last check-in "
            f"({format_time(self.holder.checked_in)}) is likely that collision, not a defect"
        )


def acquire_lock(
    service: Any, target: E2ESheet, run: RunIdentity, lease: timedelta, clock: Callable[[], datetime] = utc_now
) -> RehearsalLock:
    """Takes a free or expired lock; raises LockHeld otherwise, and when another run wins a race for it."""
    current = _read_lock_tab(service, target)
    now = clock()
    if current is not None:
        refusal = _refusal(current, now)
        if refusal is not None:
            raise refusal

    previous_tab_id = current.tab_id if current is not None else None
    holder = LockHolder(run, started=now, checked_in=now, expires=now + lease)
    tab_id = random.randint(1, MAX_SHEET_ID)
    # Deleting by the old id makes a takeover fail if another run replaced the tab first.
    requests = [] if previous_tab_id is None else [{"deleteSheet": {"sheetId": previous_tab_id}}]
    requests += [
        {"addSheet": {"properties": {"sheetId": tab_id, "title": LOCK_TAB}}},
        _write_rows(tab_id, 0, holder.rows()),
    ]
    try:
        _batch_update(service, target, requests)
    except HttpError:
        now_on_sheet = _read_lock_tab(service, target)
        if now_on_sheet is not None and now_on_sheet.tab_id != previous_tab_id:
            refusal = _refusal(now_on_sheet, clock())
            if refusal is not None:
                raise refusal from None
        raise
    took_over_from = current.holder if current is not None else None
    return RehearsalLock(service, target, tab_id, holder, lease, clock, took_over_from)
