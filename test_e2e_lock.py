import copy
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from googleapiclient.errors import HttpError

from e2e_lock import LockHeld, LockHolder, LockLost, RunIdentity, acquire_lock
from e2e_sheet import LOCK_TAB, E2ESheet

TARGET = E2ESheet(sheet_id="test-sheet-id", data_tab="Test Sheet", upload_log_tab="Upload Log", sync_log_tab="Sync Log")
LEASE = timedelta(minutes=30)
START = datetime(2026, 9, 24, 20, 0, 0, tzinfo=timezone.utc)
THIS_RUN = RunIdentity(host="this-host", pid=111, checkout="C:/checkouts/this", log_dir="C:/tmp/this/logs")
OTHER_RUN = RunIdentity(host="other-host", pid=222, checkout="C:/checkouts/other", log_dir="C:/tmp/other/logs")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "Bad Request"


def http_error(message: str, status: int = 400) -> HttpError:
    """Shaped like googleapiclient's: .resp has .status and .reason; .content is Google's JSON error body."""
    return HttpError(_Response(status), json.dumps({"error": {"message": message}}).encode("utf-8"))


class _Request:
    def __init__(self, run: Callable[[], dict]) -> None:
        self._run = run

    def execute(self) -> dict:
        return self._run()


class _Values:
    def __init__(self, sheets: "FakeSheets") -> None:
        self._sheets = sheets

    def get(self, spreadsheetId: str, range: str) -> _Request:
        return _Request(lambda: {"values": self._sheets.rows(_title_of(range))})

    def update(self, spreadsheetId: str, range: str, valueInputOption: str, body: dict) -> _Request:
        def run() -> dict:
            self._sheets.value_updates.append((range, body["values"]))
            return {}

        return _Request(run)


class _Spreadsheets:
    def __init__(self, sheets: "FakeSheets") -> None:
        self._sheets = sheets

    def values(self) -> _Values:
        return _Values(self._sheets)

    def get(self, spreadsheetId: str, fields: str) -> _Request:
        tabs = [{"properties": {"title": title, "sheetId": tab_id}} for title, tab_id in self._sheets.tabs.items()]
        return _Request(lambda: {"sheets": tabs})

    def batchUpdate(self, spreadsheetId: str, body: dict) -> _Request:
        return _Request(lambda: self._sheets.apply(body["requests"]))


def _title_of(a1_range: str) -> str:
    return a1_range.split("!")[0].strip("'").replace("''", "'")


class FakeSheets:
    """A Test Sheet in memory. batchUpdate applies every request or none, and rejects what the real API rejects."""

    def __init__(self) -> None:
        self.tabs: dict[str, int] = {TARGET.data_tab: 0}
        self.cells: dict[int, dict[tuple[int, int], str]] = {0: {}}
        self.value_updates: list[tuple[str, list[list[str]]]] = []
        # Another run's move, made just before this run's next batchUpdate reaches the API.
        self.before_next_batch: Callable[[], None] | None = None
        self.fail_next_batch: Exception | None = None
        # The batch lands, but its response is lost.
        self.fail_after_next_batch: Exception | None = None

    def spreadsheets(self) -> _Spreadsheets:
        return _Spreadsheets(self)

    def rows(self, title: str) -> list[list[str]]:
        if title not in self.tabs:
            raise http_error(f"Unable to parse range: {title}")
        cells = self.cells[self.tabs[title]]
        height = max((row for row, _ in cells), default=-1) + 1
        width = max((column for _, column in cells), default=-1) + 1
        return [[cells.get((row, column), "") for column in range(width)] for row in range(height)]

    def put_tab(self, title: str, tab_id: int, rows: list[list[str]]) -> None:
        self.tabs[title] = tab_id
        self.cells[tab_id] = {(r, c): value for r, row in enumerate(rows) for c, value in enumerate(row)}

    def delete_tab(self, title: str) -> None:
        del self.cells[self.tabs.pop(title)]

    def apply(self, requests: list[dict]) -> dict:
        move, self.before_next_batch = self.before_next_batch, None
        if move is not None:
            move()
        failure, self.fail_next_batch = self.fail_next_batch, None
        if failure is not None:
            raise failure
        tabs, cells = copy.deepcopy(self.tabs), copy.deepcopy(self.cells)
        for request in requests:
            _apply_one(request, tabs, cells)
        self.tabs, self.cells = tabs, cells
        lost_response, self.fail_after_next_batch = self.fail_after_next_batch, None
        if lost_response is not None:
            raise lost_response
        return {}


def _apply_one(request: dict, tabs: dict[str, int], cells: dict[int, dict[tuple[int, int], str]]) -> None:
    ((kind, detail),) = request.items()
    if kind == "addSheet":
        title, tab_id = detail["properties"]["title"], detail["properties"]["sheetId"]
        if title in tabs:
            raise http_error(f'A sheet with the name "{title}" already exists.')
        if tab_id in cells:
            raise http_error(f"A sheet with the id {tab_id} already exists.")
        tabs[title], cells[tab_id] = tab_id, {}
    elif kind == "deleteSheet":
        tab_id = detail["sheetId"]
        if tab_id not in cells:
            raise http_error(f"No grid with id: {tab_id}")
        del cells[tab_id]
        del tabs[next(title for title, existing in tabs.items() if existing == tab_id)]
    elif kind == "updateCells":
        start = detail["start"]
        if start["sheetId"] not in cells:
            raise http_error(f"No grid with id: {start['sheetId']}")
        assert detail["fields"] == "userEnteredValue"
        for r, row in enumerate(detail["rows"]):
            for c, value in enumerate(row["values"]):
                cells[start["sheetId"]][(start["rowIndex"] + r, start["columnIndex"] + c)] = value["userEnteredValue"]["stringValue"]
    else:
        raise AssertionError(f"the fake does not model {kind}")


def lock_holder(sheets: FakeSheets) -> LockHolder | None:
    return LockHolder.from_rows(sheets.rows(LOCK_TAB))


@pytest.fixture
def clock() -> Clock:
    return Clock(START)


@pytest.fixture
def sheets() -> FakeSheets:
    return FakeSheets()


def test_acquiring_a_free_lock_writes_this_run_into_the_lock_tab(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == lock.tab_id
    assert lock_holder(sheets) == LockHolder(THIS_RUN, started=START, checked_in=START, expires=START + LEASE)
    assert lock.took_over_from is None


def test_acquiring_refuses_while_another_run_holds_an_unexpired_lock(sheets, clock):
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)
    clock.advance(minutes=29)

    with pytest.raises(LockHeld) as held:
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    for detail in ("another e2e rehearsal", "other-host", "pid 222", "C:/checkouts/other", "C:/tmp/other/logs", "2026-09-24T20:30:00Z"):
        assert detail in str(held.value)
    assert sheets.tabs[LOCK_TAB] == other.tab_id
    assert lock_holder(sheets) == other.holder


def test_acquiring_takes_over_an_expired_lock(sheets, clock):
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)
    clock.advance(minutes=30)

    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert lock.took_over_from == other.holder
    assert sheets.tabs[LOCK_TAB] == lock.tab_id != other.tab_id
    assert lock_holder(sheets) == lock.holder


def test_two_runs_racing_for_a_free_lock_leave_it_with_the_first(sheets, clock):
    others: list = []
    sheets.before_next_batch = lambda: others.append(acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock))

    with pytest.raises(LockHeld, match="other-host"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == others[0].tab_id
    assert lock_holder(sheets) == others[0].holder


def test_two_runs_racing_for_an_expired_lock_leave_it_with_the_first(sheets, clock):
    acquire_lock(sheets, TARGET, RunIdentity("dead-host", 333, "C:/checkouts/dead", "C:/tmp/dead/logs"), LEASE, clock)
    clock.advance(minutes=45)
    others: list = []
    sheets.before_next_batch = lambda: others.append(acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock))

    with pytest.raises(LockHeld, match="other-host"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert lock_holder(sheets) == others[0].holder


def test_acquiring_refuses_a_lock_tab_that_names_no_run(sheets, clock):
    sheets.put_tab(LOCK_TAB, 7, [["typed by hand"]])

    with pytest.raises(LockHeld, match="names no rehearsal.*delete the tab"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == 7


@pytest.mark.parametrize("error", [http_error("Internal error encountered.", status=500), TimeoutError("timed out")])
def test_acquiring_reraises_an_error_that_is_not_a_race(sheets, clock, error):
    sheets.fail_next_batch = error

    with pytest.raises(type(error)):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert LOCK_TAB not in sheets.tabs


@pytest.mark.parametrize("error", [http_error("Internal error encountered.", status=500), TimeoutError("timed out")])
def test_acquiring_keeps_a_lock_whose_batch_landed_though_its_response_was_lost(sheets, clock, error):
    sheets.fail_after_next_batch = error

    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == lock.tab_id
    assert lock_holder(sheets) == lock.holder


def test_checking_in_extends_the_lease_from_now(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=10)

    lock.check_in()

    now = START + timedelta(minutes=10)
    assert lock_holder(sheets) == LockHolder(THIS_RUN, started=START, checked_in=now, expires=now + LEASE)


def test_a_run_that_checked_in_still_refuses_another_run_past_its_first_lease(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=20)
    lock.check_in()
    clock.advance(minutes=20)

    with pytest.raises(LockHeld, match="this-host"):
        acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)


def test_checking_in_after_a_takeover_names_the_new_holder_and_leaves_its_lock_alone(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=31)
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)

    with pytest.raises(LockLost, match="lost the Test Sheet lock.*took it over.*other-host.*collision"):
        lock.check_in()

    assert lock_holder(sheets) == other.holder


def test_checking_in_after_the_lock_tab_was_deleted_says_so(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    sheets.delete_tab(LOCK_TAB)

    with pytest.raises(LockLost, match="'E2E Lock' tab was deleted"):
        lock.check_in()

    assert LOCK_TAB not in sheets.tabs


def test_checking_in_reraises_an_api_error_that_is_not_a_lost_lock(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    sheets.fail_next_batch = http_error("Internal error encountered.", status=500)

    with pytest.raises(HttpError):
        lock.check_in()


def test_releasing_deletes_the_lock_tab_so_the_next_run_can_start(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    lock.release()

    assert LOCK_TAB not in sheets.tabs
    assert acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock).took_over_from is None


def test_releasing_after_a_takeover_leaves_the_new_holders_lock(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=31)
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)

    with pytest.raises(LockLost, match="other-host"):
        lock.release()

    assert sheets.tabs[LOCK_TAB] == other.tab_id


def test_holder_rows_read_back_as_the_same_holder():
    holder = LockHolder(THIS_RUN, started=START, checked_in=START, expires=START + LEASE)

    assert LockHolder.from_rows(holder.rows()) == holder
