import pytest
from googleapiclient.errors import HttpError

from fake_sheets import TARGET, FakeSheets, http_error


def test_a_refused_batch_fails_the_test_that_expected_its_after_batch_hook():
    sheets = FakeSheets()
    sheets.fail_next_batch = http_error("No grid with id: 42")
    sheets.fail_after_next_batch = TimeoutError("timed out")

    with pytest.raises(pytest.fail.Exception, match="never ran"):
        sheets.spreadsheets().batchUpdate(spreadsheetId=TARGET.sheet_id, body={"requests": []}).execute()


def test_a_quoted_title_keeps_its_bang():
    sheets = FakeSheets()
    sheets.put_tab("Log!2", 5, [])

    sheets.spreadsheets().values().update(
        spreadsheetId=TARGET.sheet_id, range="'Log!2'!B1", valueInputOption="RAW", body={"values": [["x"]]}
    ).execute()

    assert sheets.rows("Log!2") == [["", "x"]]


def test_writing_to_a_missing_tab_is_the_apis_parse_error():
    sheets = FakeSheets()

    with pytest.raises(HttpError, match="Unable to parse range"):
        sheets.write("'Gone'!A1", [["x"]])
