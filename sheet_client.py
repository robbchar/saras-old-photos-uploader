"""Thin transport over the Sheets API. Knows about cells and ranges; knows
nothing about identifiers, projects, or Internet Archive."""
from __future__ import annotations

from dataclasses import dataclass


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def quote_tab(tab: str) -> str:
    """A1 notation requires a sheet name to be single-quoted unless it is
    made only of letters, digits and underscores, with any embedded single
    quote doubled: `Sara's Photos` is written `'Sara''s Photos'`.

    Quoting unconditionally rather than only when it is strictly needed:
    `'Sheet1'!A1` is equally valid for a name that would not have required
    it, so there is no case to get wrong, and no predicate to keep in sync
    with Google's rules about which characters force quoting.

    Without this, a tab named with a space - which is what a Google Sheet tab
    is usually called - made every read and every write fail, and the error
    surfaced as a generic HttpError that cmd_validate reports as "check that
    'sheet_tab' names the tab exactly (case-sensitive)", sending the operator
    to re-verify the one thing that was already right."""
    return "'" + tab.replace("'", "''") + "'"


@dataclass(frozen=True)
class CellUpdate:
    a1: str
    value: str


class SheetClient:
    def __init__(self, service, spreadsheet_id: str, tab: str) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id
        self._tab = tab

    def append_only_tab(self, tab: str) -> AppendOnlyTab:
        """The same spreadsheet, one tab over, and appendable only.

        A run that also writes a log tab is still one spreadsheet and one
        authenticated session; rebuilding a client through
        build_sheet_client() would re-run the OAuth check and build a second
        service for no reason.

        It is an AppendOnlyTab rather than another SheetClient because a
        SheetClient would carry write_cells along with it, and what this is
        for is telemetry - handing the log-tab writer an object that COULD
        overwrite the metadata columns, and then relying on it not to, is a
        promise rather than a property. See docs/DECISIONS.md, "The Sheet's
        log tabs are telemetry, never an input"."""
        return AppendOnlyTab(SheetClient(self._service, self._spreadsheet_id, tab))

    def read_grid(self) -> list[list[str]]:
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=quote_tab(self._tab))
            .execute()
        )
        return response.get("values", [])

    def write_cells(self, updates: list[CellUpdate]) -> None:
        """One batch request per call regardless of cell count - the Sheets API
        counts a batch as a single request against the 60/minute/user quota."""
        if not updates:
            return

        body = {
            "valueInputOption": "RAW",
            "data": [
                {"range": f"{quote_tab(self._tab)}!{update.a1}", "values": [[update.value]]}
                for update in updates
            ],
        }
        self._service.spreadsheets().values().batchUpdate(
            spreadsheetId=self._spreadsheet_id, body=body
        ).execute()

    def ensure_tab(self, header: list[str]) -> None:
        """Make this client's tab exist and carry `header` as its first row.

        Three cases, and the third is the point:

        - The tab is missing: it is created and the header appended.
        - The tab exists and is empty, or already starts with `header`:
          nothing to do beyond writing the header to an empty one, so a tab
          an operator made by hand before the first run works the same as one
          created here.
        - The tab exists and starts with something else: refused. A tab name
          mistyped as some other real tab - an archived copy of the metadata,
          a donor's notes - would otherwise have every run quietly append
          rows underneath content nobody meant to touch. Only `sheet_tab`
          itself is caught in configuration; every other tab in the
          spreadsheet is not, and this is the check that covers them.

        The tab list is read rather than creating and catching an
        "already exists" error, because every run calls this and the common
        path is the one that must be cheap.

        Raising is safe here: the only caller is the log-tab mirror, which
        reports rather than propagates. A refusal shows up as one clear line
        on stderr and costs the run nothing."""
        existing = (
            self._service.spreadsheets()
            .get(spreadsheetId=self._spreadsheet_id, fields="sheets.properties.title")
            .execute()
        )
        titles = {
            sheet.get("properties", {}).get("title") for sheet in existing.get("sheets", [])
        }
        if self._tab not in titles:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self._spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": {"title": self._tab}}}]},
            ).execute()
            self.append_rows([header])
            return

        first_row = self._first_row()
        if not first_row:
            self.append_rows([header])
            return
        if first_row[: len(header)] != header:
            raise ValueError(
                f"tab '{self._tab}' already holds something that is not a log: its first row "
                f"is {first_row[:len(header)]}, not {header}. Refusing to append run summaries "
                "underneath content this was not meant to touch - point the log tab at its own "
                "tab, or rename the one in the way"
            )

    def _first_row(self) -> list[str]:
        """This tab's row 1, without reading the rest of it - a log tab grows
        without bound, and ensure_tab() runs on every single run."""
        response = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=self._spreadsheet_id, range=f"{quote_tab(self._tab)}!1:1")
            .execute()
        )
        rows = response.get("values", [])
        return rows[0] if rows else []

    def append_rows(self, rows: list[list[str]]) -> None:
        """One append request per call - the API finds the end of the tab's
        data and adds every row after it, so no row index is computed (or
        raced over) on this side. RAW for the same reason write_cells uses
        it: a filename starting with "=" must land as text, never be
        interpreted as a formula. INSERT_ROWS so the append adds rows rather
        than overwriting whatever happens to sit in the grid below the
        table."""
        if not rows:
            return

        self._service.spreadsheets().values().append(
            spreadsheetId=self._spreadsheet_id,
            range=quote_tab(self._tab),
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()


class AppendOnlyTab:
    """One tab of a spreadsheet, which can be created and appended to and
    nothing else.

    Deliberately has no write_cells and no read_grid: it is what the log-tab
    writer is handed, and the narrowness IS the safety property. Wraps a
    SheetClient rather than reimplementing the two calls, so the quoting,
    RAW/INSERT_ROWS choices and batching rules stay in one place."""

    def __init__(self, client: SheetClient) -> None:
        self._client = client

    def ensure_tab(self, header: list[str]) -> None:
        self._client.ensure_tab(header)

    def append_rows(self, rows: list[list[str]]) -> None:
        self._client.append_rows(rows)
