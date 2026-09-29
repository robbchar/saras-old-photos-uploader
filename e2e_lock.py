"""One e2e rehearsal at a time: a lock tab on the Test Sheet (test_e2e_rehearsal.py).

Ownership is the sheetId of the tab this run last put there; each check-in swaps in a new one.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, TypeGuard

from googleapiclient.errors import HttpError

from e2e_sheet import LOCK_TAB, E2ESheet
from utc_time import format_utc as format_time
from utc_time import parse_utc as parse_time
from utc_time import utc_now

# The lock tab's rows, in order.
FIELDS = ("host", "pid", "checkout", "log dir", "started", "checked in", "expires")
MAX_SHEET_ID = 2**31 - 1
# Every tab's id and cells in one request; the Test Sheet holds a few rows.
SNAPSHOT_FIELDS = "sheets(properties(sheetId,title),data(rowData(values(formattedValue))))"


class LockHeld(Exception):
    """Another rehearsal holds the lock, the lock tab names no rehearsal, or it vanished mid-takeover (re-run)."""


class LockLost(Exception):
    """This run's lock tab is gone: another run took it over, or it was deleted."""


class LockStillHeld(Exception):
    """A check-in or release failed, but the Sheet shows the lock is still this run's."""


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
    """The tab's id and rows from one request, so both describe the same moment."""
    response = service.spreadsheets().get(spreadsheetId=target.sheet_id, fields=SNAPSHOT_FIELDS).execute()
    for sheet in response.get("sheets", []):
        if sheet["properties"]["title"] != LOCK_TAB:
            continue
        grid = (sheet.get("data") or [{}])[0]
        rows = [[cell.get("formattedValue", "") for cell in row.get("values", [])] for row in grid.get("rowData", [])]
        return _LockTab(sheet["properties"]["sheetId"], LockHolder.from_rows(rows))
    return None


def _held_by(lock_tab: _LockTab | None, run: RunIdentity) -> TypeGuard[_LockTab]:
    return lock_tab is not None and lock_tab.holder is not None and lock_tab.holder.run == run


def _may_have_landed(error: Exception) -> bool:
    """A lost response or a server error; a 4xx means the API refused the batch."""
    return not isinstance(error, HttpError) or error.resp.status >= 500


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


def _new_tab_id(replacing: int | None) -> int:
    """From the OS's randomness: a seeded `random` would hand two runs the same id."""
    while True:
        tab_id = secrets.randbelow(MAX_SHEET_ID) + 1
        if tab_id != replacing:
            return tab_id


def _write_rows(tab_id: int, rows: list[list[str]]) -> dict[str, Any]:
    return {
        "updateCells": {
            "start": {"sheetId": tab_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": value}} for value in row]} for row in rows],
            "fields": "userEnteredValue",
        }
    }


def _swap_requests(old_tab_id: int | None, new_tab_id: int, holder: LockHolder) -> list[dict[str, Any]]:
    """Deleting by the old id makes the swap fail if another run replaced that tab first."""
    deletion = [] if old_tab_id is None else [{"deleteSheet": {"sheetId": old_tab_id}}]
    return [
        *deletion,
        {"addSheet": {"properties": {"sheetId": new_tab_id, "title": LOCK_TAB}}},
        _write_rows(new_tab_id, holder.rows()),
    ]


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
        """Swaps in a tab under a new id, with the lease extended from now; raises LockLost if the tab is no longer this run's."""
        now = self._clock()
        holder = replace(self.holder, checked_in=now, expires=now + self._lease)
        new_tab_id = _new_tab_id(self.tab_id)

        def swap_landed(_error: Exception, now_on_sheet: _LockTab | None) -> bool:
            return _held_by(now_on_sheet, self.holder.run) and now_on_sheet.tab_id == new_tab_id

        self._on_own_tab(lambda old_tab_id: _swap_requests(old_tab_id, new_tab_id, holder), swap_landed)
        self.tab_id, self.holder = new_tab_id, holder

    def release(self) -> None:
        """Deletes this run's lock tab; raises LockLost, touching nothing, if it is no longer this run's."""
        # Whole seconds, as the lock tab records `started`.
        began = self._clock().replace(microsecond=0)

        def delete_landed(error: Exception, now_on_sheet: _LockTab | None) -> bool:
            if not _may_have_landed(error) or _held_by(now_on_sheet, self.holder.run):
                return False
            # A holder started before this release took the lock over; one started after took the freed lock.
            other_holder = now_on_sheet.holder if now_on_sheet is not None else None
            return other_holder is None or other_holder.started >= began

        self._on_own_tab(lambda tab_id: [{"deleteSheet": {"sheetId": tab_id}}], delete_landed)

    def holds(self) -> bool:
        """Whether the lock tab on the Sheet is still this run's; raises if the Sheet cannot be read."""
        return _held_by(_read_lock_tab(self._service, self._target), self.holder.run)

    def _on_own_tab(
        self,
        requests_for: Callable[[int], list[dict[str, Any]]],
        landed: Callable[[Exception, _LockTab | None], bool],
    ) -> None:
        """Sends `requests_for(tab_id)`; after an error, `landed` judges the re-read Sheet."""
        for retrying in (False, True):
            tried_tab_id = self.tab_id
            try:
                _batch_update(self._service, self._target, requests_for(tried_tab_id))
                return
            except Exception as error:
                now_on_sheet = _read_lock_tab(self._service, self._target)
                if landed(error, now_on_sheet):
                    return
                if not _held_by(now_on_sheet, self.holder.run):
                    raise LockLost(self._lost_message(now_on_sheet)) from None
                self.tab_id = now_on_sheet.tab_id
                if now_on_sheet.tab_id == tried_tab_id or retrying:
                    raise LockStillHeld(f"the '{LOCK_TAB}' tab update failed, but the tab still names this run: {error!r}") from error
                # An earlier swap whose re-read failed left a newer tab of this run's; retry from it once.

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
    tab_id = _new_tab_id(previous_tab_id)
    try:
        _batch_update(service, target, _swap_requests(previous_tab_id, tab_id, holder))
    except Exception as error:
        # Either the batch landed and only its response was lost, or another run won a race for the lock.
        try:
            now_on_sheet = _read_lock_tab(service, target)
        except Exception:
            # Whether the batch landed is unknown; a refused one left nothing to undo, and its id may be another run's tab.
            if _may_have_landed(error):
                with suppress(Exception):
                    _batch_update(service, target, [{"deleteSheet": {"sheetId": tab_id}}])
            raise
        if not (_held_by(now_on_sheet, run) and now_on_sheet.tab_id == tab_id):
            if now_on_sheet is None and previous_tab_id is not None:
                raise LockHeld(f"the '{LOCK_TAB}' tab was deleted while this run was taking it over; re-run") from None
            if now_on_sheet is None or now_on_sheet.tab_id == previous_tab_id:
                raise
            refusal = _refusal(now_on_sheet, clock())
            if refusal is None:
                raise
            raise refusal from None
    took_over_from = current.holder if current is not None else None
    return RehearsalLock(service, target, tab_id, holder, lease, clock, took_over_from)
