import json
from pathlib import Path

import pytest

from e2e_sheet import (
    E2ESheet,
    ResetRefused,
    check_reset_allowed,
    load_fixture_grid,
    pad_grid,
    reset_test_sheet,
    set_cell,
    tab_ids,
)
from project_config import load_project_config

TEST_SHEET_ID = "test-sheet-id"

REPO_ROOT = Path(__file__).resolve().parent
FIXTURES = REPO_ROOT / "e2e_fixtures"
REAL_HEADER_QUIRKS = ("Place ", "ia_last_synced.", "Notes (LCPS Internal)")


def e2e_block(**overrides: str) -> dict[str, str]:
    block = {
        "sheet_id": "REPLACE_WITH_NEVER_LIVE",
        "test_sheet_id": TEST_SHEET_ID,
        "sheet_tab": "Test Sheet",
        "upload_log_tab": "Upload Log",
        "sync_log_tab": "Sync Log",
    }
    block.update(overrides)
    return block


def write_registries(
    tmp_path: Path, block: dict[str, str], live_projects: dict[str, dict[str, str]]
) -> tuple[Path, Path]:
    e2e_path = tmp_path / "e2e.json"
    live_path = tmp_path / "live.json"
    e2e_path.write_text(json.dumps({"collection_key": "lcps", "projects": {"e2e": block}}), encoding="utf-8")
    live_path.write_text(json.dumps({"collection_key": "lcps", "projects": live_projects}), encoding="utf-8")
    return e2e_path, live_path


def test_guard_returns_the_test_sheet_when_nothing_live_matches(tmp_path):
    e2e_path, live_path = write_registries(
        tmp_path, e2e_block(), {"photos": {"sheet_id": "real-sheet-id", "test_sheet_id": TEST_SHEET_ID}}
    )

    assert check_reset_allowed(e2e_path, live_path) == E2ESheet(
        sheet_id=TEST_SHEET_ID, data_tab="Test Sheet", upload_log_tab="Upload Log", sync_log_tab="Sync Log"
    )


def test_guard_allows_sharing_a_live_projects_test_sheet(tmp_path):
    e2e_path, live_path = write_registries(
        tmp_path, e2e_block(), {"photos": {"sheet_id": "REPLACE_WITH_REAL_SHEET_ID", "test_sheet_id": TEST_SHEET_ID}}
    )

    assert check_reset_allowed(e2e_path, live_path).sheet_id == TEST_SHEET_ID


def test_guard_refuses_a_live_sheet_id_in_any_project(tmp_path):
    e2e_path, live_path = write_registries(
        tmp_path,
        e2e_block(),
        {"photos": {"sheet_id": "real-sheet-id"}, "maps": {"sheet_id": TEST_SHEET_ID}},
    )

    with pytest.raises(ResetRefused, match="live sheet_id"):
        check_reset_allowed(e2e_path, live_path)


def test_guard_refuses_an_e2e_registry_with_a_real_sheet_id(tmp_path):
    e2e_path, live_path = write_registries(tmp_path, e2e_block(sheet_id="some-real-id"), {})

    with pytest.raises(ResetRefused, match="placeholder.*'some-real-id'"):
        check_reset_allowed(e2e_path, live_path)


@pytest.mark.parametrize("key", ["test_sheet_id", "sheet_tab", "upload_log_tab", "sync_log_tab"])
def test_guard_refuses_a_missing_setting(tmp_path, key):
    block = e2e_block()
    del block[key]
    e2e_path, live_path = write_registries(tmp_path, block, {})

    with pytest.raises(ResetRefused, match=key):
        check_reset_allowed(e2e_path, live_path)


@pytest.mark.parametrize("key", ["upload_log_tab", "sync_log_tab"])
def test_guard_refuses_a_log_tab_named_like_the_data_tab(tmp_path, key):
    e2e_path, live_path = write_registries(tmp_path, e2e_block(**{key: "Test Sheet"}), {})

    with pytest.raises(ResetRefused, match="as a log tab"):
        check_reset_allowed(e2e_path, live_path)


def test_guard_refuses_an_unknown_project(tmp_path):
    e2e_path, live_path = write_registries(tmp_path, e2e_block(), {})

    with pytest.raises(ResetRefused, match="no project 'other'"):
        check_reset_allowed(e2e_path, live_path, project="other")


def test_guard_refuses_an_unreadable_live_registry(tmp_path):
    e2e_path, _ = write_registries(tmp_path, e2e_block(), {})

    with pytest.raises(ResetRefused, match="cannot read"):
        check_reset_allowed(e2e_path, tmp_path / "missing.json")


def test_guard_refuses_a_live_registry_without_projects(tmp_path):
    e2e_path, live_path = write_registries(tmp_path, e2e_block(), {})
    live_path.write_text(json.dumps({"collection_key": "lcps"}), encoding="utf-8")

    with pytest.raises(ResetRefused, match="no projects"):
        check_reset_allowed(e2e_path, live_path)


TARGET = E2ESheet(sheet_id=TEST_SHEET_ID, data_tab="Test Sheet", upload_log_tab="Upload Log", sync_log_tab="Sync Log")


class FakeRequest:
    def __init__(self, response: dict | None = None) -> None:
        self._response = response or {}

    def execute(self) -> dict:
        return self._response


class FakeValues:
    def __init__(self, calls: list, rows: list[list[str]]) -> None:
        self._calls = calls
        self._rows = rows

    def get(self, **kwargs) -> FakeRequest:
        self._calls.append(("read", kwargs))
        return FakeRequest({"values": self._rows})

    def clear(self, **kwargs) -> FakeRequest:
        self._calls.append(("clear", kwargs))
        return FakeRequest()

    def update(self, **kwargs) -> FakeRequest:
        self._calls.append(("update", kwargs))
        return FakeRequest()


class FakeSpreadsheets:
    def __init__(self, calls: list, tabs: dict[str, int], rows: list[list[str]]) -> None:
        self._calls = calls
        self._tabs = tabs
        self._rows = rows

    def values(self) -> FakeValues:
        return FakeValues(self._calls, self._rows)

    def get(self, **kwargs) -> FakeRequest:
        self._calls.append(("get", kwargs))
        sheets = [{"properties": {"title": title, "sheetId": sheet_id}} for title, sheet_id in self._tabs.items()]
        return FakeRequest({"sheets": sheets})

    def batchUpdate(self, **kwargs) -> FakeRequest:
        self._calls.append(("batchUpdate", kwargs))
        return FakeRequest()


class FakeSheetsService:
    """Records every Sheets API call; answers spreadsheets().get from `tabs` and a values read from `rows`."""

    def __init__(self, tabs: dict[str, int], rows: list[list[str]] | None = None) -> None:
        self.calls: list = []
        self._tabs = tabs
        self._rows = rows or []

    def spreadsheets(self) -> FakeSpreadsheets:
        return FakeSpreadsheets(self.calls, self._tabs, self._rows)


def test_tab_ids_maps_titles_to_sheet_ids():
    service = FakeSheetsService({"Test Sheet": 0, "Upload Log": 7})

    assert tab_ids(service, TEST_SHEET_ID) == {"Test Sheet": 0, "Upload Log": 7}


def test_reset_clears_writes_and_deletes_existing_log_tabs():
    service = FakeSheetsService({"Test Sheet": 0, "Upload Log": 7, "Sync Log": 9, "Other": 3})
    grid = [["Title"], ["E2E fixture 1"]]

    reset_test_sheet(service, TARGET, grid)

    read, clear, update, _, delete = service.calls
    assert read == ("read", {"spreadsheetId": TEST_SHEET_ID, "range": "'Test Sheet'"})
    assert clear ==("clear", {"spreadsheetId": TEST_SHEET_ID, "range": "'Test Sheet'", "body": {}})
    assert update == (
        "update",
        {"spreadsheetId": TEST_SHEET_ID, "range": "'Test Sheet'!A1", "valueInputOption": "RAW", "body": {"values": grid}},
    )
    assert delete == (
        "batchUpdate",
        {"spreadsheetId": TEST_SHEET_ID, "body": {"requests": [{"deleteSheet": {"sheetId": 7}}, {"deleteSheet": {"sheetId": 9}}]}},
    )


def test_reset_skips_the_delete_when_no_log_tab_exists():
    service = FakeSheetsService({"Test Sheet": 0})

    reset_test_sheet(service, TARGET, [["Title"]])

    assert [name for name, _ in service.calls] == ["read", "clear", "update", "get"]


def test_reset_allows_a_data_tab_no_longer_than_the_grid():
    service = FakeSheetsService({"Test Sheet": 0}, rows=[["Title"], ["old"]])

    reset_test_sheet(service, TARGET, [["Title"], ["E2E fixture 1"]])

    assert [name for name, _ in service.calls] == ["read", "clear", "update", "get"]


def test_reset_refuses_a_data_tab_longer_than_the_grid_and_writes_nothing():
    service = FakeSheetsService({"Test Sheet": 0}, rows=[["Title"], ["a"], ["b"]])

    with pytest.raises(ResetRefused, match="3 rows, more than the 2-row fixture"):
        reset_test_sheet(service, TARGET, [["Title"], ["E2E fixture 1"]])

    assert [name for name, _ in service.calls] == ["read"]


def test_set_cell_writes_one_cell_on_the_data_tab():
    service = FakeSheetsService({})

    set_cell(service, TARGET, row_number=2, column_index=2, value="edited")

    assert service.calls == [
        (
            "update",
            {"spreadsheetId": TEST_SHEET_ID, "range": "'Test Sheet'!C2", "valueInputOption": "RAW", "body": {"values": [["edited"]]}},
        )
    ]


def test_load_fixture_grid_lays_rows_out_by_header(tmp_path):
    path = tmp_path / "sheet.json"
    path.write_text(json.dumps({"header": ["A", "B", "C"], "rows": [{"C": "3", "A": "1"}]}), encoding="utf-8")

    assert load_fixture_grid(path) == [["A", "B", "C"], ["1", "", "3"]]


def test_load_fixture_grid_rejects_a_column_not_in_the_header(tmp_path):
    path = tmp_path / "sheet.json"
    path.write_text(json.dumps({"header": ["A"], "rows": [{"Titel": "x"}]}), encoding="utf-8")

    with pytest.raises(ValueError, match="Titel"):
        load_fixture_grid(path)


def test_pad_grid_restores_trailing_blanks_the_api_omits():
    assert pad_grid([["a"], ["b", "c"]], 3) == [["a", "", ""], ["b", "c", ""]]


def test_checked_in_registry_loads_as_the_e2e_project():
    registry = json.loads((FIXTURES / "registry.json").read_text(encoding="utf-8"))

    config = load_project_config(registry, "e2e")

    assert config.files_dir == "e2e_fixtures/files"


def test_checked_in_registry_passes_the_guard_against_the_real_registry():
    target = check_reset_allowed(FIXTURES / "registry.json", REPO_ROOT / "projects_registry.json")

    assert target.data_tab == "Test Sheet"


def test_checked_in_grid_keeps_the_real_header_quirks():
    header = load_fixture_grid(FIXTURES / "sheet.json")[0]

    assert all(quirk in header for quirk in REAL_HEADER_QUIRKS)


def test_checked_in_grid_has_four_ready_rows_and_one_without_a_theme():
    grid = load_fixture_grid(FIXTURES / "sheet.json")
    theme = grid[0].index("Theme")

    assert [bool(row[theme]) for row in grid[1:]] == [True, True, True, True, False]


def test_every_checked_in_row_names_a_file_that_exists():
    grid = load_fixture_grid(FIXTURES / "sheet.json")
    folder, name = grid[0].index("Folder on LaCie Drive"), grid[0].index("File Name")

    missing = [row[name] for row in grid[1:] if not (FIXTURES / "files" / row[folder] / row[name]).is_file()]

    assert missing == []
