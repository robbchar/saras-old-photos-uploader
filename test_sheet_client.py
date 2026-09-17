import pytest

from sheet_client import CellUpdate, SheetClient, column_letter, quote_tab


class FakeValues:
    """Records requests instead of issuing them."""

    def __init__(self, grid, get_response=None):
        self.grid = grid
        self.batch_update_calls = []
        self.append_calls = []
        self.get_spreadsheet_id = None
        self.batch_update_spreadsheet_id = None
        self.append_spreadsheet_id = None
        self._get_response = get_response if get_response is not None else {"values": grid}

    def get(self, spreadsheetId, range):
        self.get_spreadsheet_id = spreadsheetId
        self.get_range = range
        return _Executable(self._get_response)

    def batchUpdate(self, spreadsheetId, body):
        self.batch_update_spreadsheet_id = spreadsheetId
        self.batch_update_calls.append(body)
        return _Executable({"totalUpdatedCells": len(body["data"])})

    def append(self, spreadsheetId, range, valueInputOption, insertDataOption, body):
        self.append_spreadsheet_id = spreadsheetId
        self.append_calls.append(
            {
                "range": range,
                "valueInputOption": valueInputOption,
                "insertDataOption": insertDataOption,
                "body": body,
            }
        )
        return _Executable({"updates": {"updatedRows": len(body["values"])}})


class _Executable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeService:
    """`spreadsheets()` returns self, so the spreadsheet-level calls
    (`get`, `batchUpdate`) sit here and the value-level ones behind
    `values()` - the same split the real client has."""

    def __init__(self, grid, get_response=None, tabs=("Donor Photos",)):
        self.values_api = FakeValues(grid, get_response)
        self.tabs = list(tabs)
        self.sheet_batch_update_calls = []

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_api

    def get(self, spreadsheetId, fields):
        return _Executable(
            {"sheets": [{"properties": {"title": title}} for title in self.tabs]}
        )

    def batchUpdate(self, spreadsheetId, body):
        self.sheet_batch_update_calls.append(body)
        for request in body["requests"]:
            self.tabs.append(request["addSheet"]["properties"]["title"])
        return _Executable({"replies": []})


@pytest.mark.parametrize("index,letter", [(0, "A"), (25, "Z"), (26, "AA"), (27, "AB")])
def test_column_letter(index, letter):
    assert column_letter(index) == letter


def test_read_grid_returns_rows_from_the_named_tab():
    service = FakeService([["Title"], ["Alderbrook Hall"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    assert client.read_grid() == [["Title"], ["Alderbrook Hall"]]
    assert service.values_api.get_range == "'Donor Photos'"
    assert service.values_api.get_spreadsheet_id == "SHEET_ID"


def test_write_cells_issues_a_single_batch_request():
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    client.write_cells([CellUpdate("B2", "x"), CellUpdate("B3", "y")])

    assert len(service.values_api.batch_update_calls) == 1
    body = service.values_api.batch_update_calls[0]
    assert body["data"] == [
        {"range": "'Donor Photos'!B2", "values": [["x"]]},
        {"range": "'Donor Photos'!B3", "values": [["y"]]},
    ]
    assert service.values_api.batch_update_spreadsheet_id == "SHEET_ID"


def test_write_cells_with_nothing_to_write_makes_no_request():
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    client.write_cells([])

    assert service.values_api.batch_update_calls == []


def test_read_grid_with_empty_sheet_returns_empty_list():
    # Google omits the "values" key entirely for empty ranges
    service = FakeService([], get_response={})
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    result = client.read_grid()

    assert result == []
    assert service.values_api.get_spreadsheet_id == "SHEET_ID"


def test_append_rows_issues_a_single_append_request():
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    client.append_rows([["SOP CD 1", "a.jpg", ""], ["SOP CD 1", "b.jpg", ""]])

    assert len(service.values_api.append_calls) == 1
    call = service.values_api.append_calls[0]
    assert call["range"] == "'Donor Photos'"
    # RAW for the same reason write_cells uses it: a filename starting with
    # "=" must land as text, never be interpreted as a formula.
    assert call["valueInputOption"] == "RAW"
    # INSERT_ROWS so the append adds rows rather than overwriting whatever
    # happens to sit in the grid below the table.
    assert call["insertDataOption"] == "INSERT_ROWS"
    assert call["body"] == {"values": [["SOP CD 1", "a.jpg", ""], ["SOP CD 1", "b.jpg", ""]]}
    assert service.values_api.append_spreadsheet_id == "SHEET_ID"


def test_append_rows_with_nothing_to_append_makes_no_request():
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    client.append_rows([])

    assert service.values_api.append_calls == []


def test_append_rows_quotes_a_tab_name_containing_an_apostrophe():
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Sara's Photos")

    client.append_rows([["x"]])

    assert service.values_api.append_calls[0]["range"] == "'Sara''s Photos'"


@pytest.mark.parametrize(
    "tab,quoted",
    [
        ("Sheet1", "'Sheet1'"),
        ("Donor Photos", "'Donor Photos'"),
        ("Sara's Photos", "'Sara''s Photos'"),
        ("2024/25", "'2024/25'"),
    ],
)
def test_quote_tab_wraps_the_name_and_doubles_embedded_quotes(tab, quoted):
    """A1 notation needs the sheet name quoted unless it is purely
    alphanumeric, and an embedded apostrophe doubled. Quoting is always
    valid, so every name takes the same path."""
    assert quote_tab(tab) == quoted


def test_read_grid_quotes_a_tab_name_containing_an_apostrophe():
    """An unquoted apostrophe terminates the quoted name early, so the range
    Google receives is not the tab the operator named."""
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Sara's Photos")

    client.read_grid()

    assert service.values_api.get_range == "'Sara''s Photos'"


def test_write_cells_quotes_a_tab_name_containing_an_apostrophe():
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Sara's Photos")

    client.write_cells([CellUpdate("B2", "x")])

    body = service.values_api.batch_update_calls[0]
    assert body["data"] == [{"range": "'Sara''s Photos'!B2", "values": [["x"]]}]


def test_ensure_tab_creates_a_missing_tab_with_its_header_row():
    """A tab nobody created by hand is the normal case: the operator sets up
    the metadata Sheet, and the log tabs appear the first time a run writes
    one. Requiring manual setup would make the first real run the moment
    someone discovers a step they skipped."""
    service = FakeService([["Title"]], tabs=["Donor Photos"])
    client = SheetClient(service, "SHEET_ID", "Upload Log")

    client.ensure_tab(["when", "run", "outcome"])

    assert service.sheet_batch_update_calls == [
        {"requests": [{"addSheet": {"properties": {"title": "Upload Log"}}}]}
    ]
    assert service.values_api.append_calls[0]["body"]["values"] == [
        ["when", "run", "outcome"]
    ]


def test_ensure_tab_leaves_an_existing_tab_alone():
    """Every run calls this. Re-adding the header each time would push a row
    of column names into the middle of the log, and re-creating the tab would
    fail the whole append."""
    service = FakeService([["Title"]], tabs=["Donor Photos", "Upload Log"])
    client = SheetClient(service, "SHEET_ID", "Upload Log")

    client.ensure_tab(["when", "run", "outcome"])

    assert service.sheet_batch_update_calls == []
    assert service.values_api.append_calls == []


def test_for_tab_reuses_the_connection_and_changes_only_the_tab():
    """A run that also writes a log tab must not authenticate a second time
    or build a second service - it is the same spreadsheet, one tab over."""
    service = FakeService([["Title"]])
    client = SheetClient(service, "SHEET_ID", "Donor Photos")

    log_client = client.for_tab("Upload Log")

    log_client.append_rows([["x"]])
    assert service.values_api.append_calls[0]["range"] == "'Upload Log'"
    assert service.values_api.append_spreadsheet_id == "SHEET_ID"
    # and the original is untouched
    client.append_rows([["y"]])
    assert service.values_api.append_calls[1]["range"] == "'Donor Photos'"
