"""Test support: a stateful in-memory Test Sheet for the e2e lock and rehearsal tests."""

import copy
import json
from collections.abc import Callable

import pytest
from googleapiclient.errors import HttpError

from e2e_sheet import E2ESheet

TARGET = E2ESheet(sheet_id="test-sheet-id", data_tab="Test Sheet", upload_log_tab="Upload Log", sync_log_tab="Sync Log")
# The spreadsheets().get masks the code sends; the cells one was checked against the live API.
PROPERTIES_MASK = "sheets.properties(sheetId,title)"
CELLS_MASK = "sheets(properties(sheetId,title),data(rowData(values(formattedValue))))"


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


def _check_spreadsheet(spreadsheet_id: str) -> None:
    assert spreadsheet_id == TARGET.sheet_id, f"the fake holds only {TARGET.sheet_id}, not {spreadsheet_id}"


class _Values:
    def __init__(self, sheets: "FakeSheets") -> None:
        self._sheets = sheets

    def get(self, spreadsheetId: str, range: str) -> _Request:
        _check_spreadsheet(spreadsheetId)
        return _Request(lambda: {"values": self._sheets.rows(_title_of(range))})

    def update(self, spreadsheetId: str, range: str, valueInputOption: str, body: dict) -> _Request:
        _check_spreadsheet(spreadsheetId)

        def run() -> dict:
            self._sheets.value_updates.append((range, body["values"]))
            self._sheets.write(range, body["values"])
            return {}

        return _Request(run)


class _Spreadsheets:
    def __init__(self, sheets: "FakeSheets") -> None:
        self._sheets = sheets

    def values(self) -> _Values:
        return _Values(self._sheets)

    def get(self, spreadsheetId: str, fields: str) -> _Request:
        _check_spreadsheet(spreadsheetId)
        assert fields in (PROPERTIES_MASK, CELLS_MASK), f"the fake does not model the mask {fields}"
        return _Request(lambda: self._sheets.spreadsheet(with_cells=fields == CELLS_MASK))

    def batchUpdate(self, spreadsheetId: str, body: dict) -> _Request:
        _check_spreadsheet(spreadsheetId)
        return _Request(lambda: self._sheets.apply(body["requests"]))


def _title_of(a1_range: str) -> str:
    """A quoted title runs to its last quote, so a '!' inside it stays part of the title."""
    if a1_range.startswith("'"):
        return a1_range[1 : a1_range.rindex("'")].replace("''", "'")
    return a1_range.split("!")[0]


def _cell_of(a1_range: str) -> tuple[int, int]:
    """`'Tab'!C2` to (1, 2): 0-based row and column."""
    cell = a1_range.rsplit("!", 1)[1]
    letters = cell.rstrip("0123456789")
    column = 0
    for letter in letters:
        column = column * 26 + ord(letter) - ord("A") + 1
    return int(cell[len(letters):]) - 1, column - 1


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
        # Another run's move, made just after this run's next batchUpdate lands.
        self.after_next_batch: Callable[[], None] | None = None
        # Another run's move, made just after this run's next spreadsheets().get is answered.
        self.after_next_get: Callable[[], None] | None = None
        self.fail_next_get: Exception | None = None

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

    def write(self, a1_range: str, rows: list[list[str]]) -> None:
        title = _title_of(a1_range)
        if title not in self.tabs:
            raise http_error(f"Unable to parse range: {a1_range}")
        first_row, first_column = _cell_of(a1_range)
        cells = self.cells[self.tabs[title]]
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                cells[(first_row + r, first_column + c)] = value

    def spreadsheet(self, with_cells: bool) -> dict:
        """Empty cells come back as `{}` and trailing empty rows are left out, as the real API does."""
        failure, self.fail_next_get = self.fail_next_get, None
        if failure is not None:
            raise failure
        sheets = []
        for title, tab_id in self.tabs.items():
            sheet: dict = {"properties": {"title": title, "sheetId": tab_id}}
            if with_cells:
                row_data = [{"values": [{"formattedValue": value} if value else {} for value in row]} for row in self.rows(title)]
                sheet["data"] = [{"rowData": row_data}]
            sheets.append(sheet)
        move, self.after_next_get = self.after_next_get, None
        if move is not None:
            move()
        return {"sheets": sheets}

    def apply(self, requests: list[dict]) -> dict:
        move, self.before_next_batch = self.before_next_batch, None
        failure, self.fail_next_batch = self.fail_next_batch, None
        lost_response, self.fail_after_next_batch = self.fail_after_next_batch, None
        move_after, self.after_next_batch = self.after_next_batch, None
        if move is not None:
            move()
        tabs, cells = copy.deepcopy(self.tabs), copy.deepcopy(self.cells)
        try:
            if failure is not None:
                raise failure
            for request in requests:
                _apply_one(request, tabs, cells)
        except Exception:
            # pytest.fail is a BaseException, so the lock code's `except Exception` cannot swallow it.
            if lost_response is not None or move_after is not None:
                pytest.fail("the batch did not land, so its fail_after_next_batch/after_next_batch hook never ran")
            raise
        self.tabs, self.cells = tabs, cells
        if move_after is not None:
            move_after()
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
