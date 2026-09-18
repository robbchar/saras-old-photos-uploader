import pytest

from log_tab import LOG_TAB_HEADER, log_tab_rows, mirror_run


def _upload_record(**overrides):
    record = {
        "record": "run_summary",
        "timestamp": "2026-09-17T18:02:11Z",
        "live": True,
        "attempted": 2,
        "succeeded": 2,
        "failures": [],
        "unconfirmed": [],
        "not_attempted": 0,
        "rate_limited": False,
        "skipped": [],
    }
    record.update(overrides)
    return record


def test_a_clean_run_is_one_row():
    """The steady state. 10,000 good uploads must not put 10,000 rows in the
    Sheet - the tab is read by a person scrolling it, and a tab nobody can
    scan is a tab nobody opens."""
    rows = log_tab_rows(
        _upload_record(), run="upload-20260917T180211Z.jsonl", headline="2 file(s) uploaded successfully, 0 error(s)"
    )

    assert rows == [
        [
            "2026-09-17T18:02:11Z",
            "upload-20260917T180211Z.jsonl",
            "summary",
            "",
            "2 file(s) uploaded successfully, 0 error(s)",
        ]
    ]


def test_each_problem_gets_its_own_row_naming_the_item_and_the_reason():
    """The count alone is what sends someone back to the JSONL. A row per
    problem is the whole reason the tab is worth writing."""
    record = _upload_record(
        succeeded=0,
        attempted=1,
        failures=[{"identifier": "lcps-sarasoldphotos-00341", "error": "HTTP 503 from ia"}],
        skipped=[{"identifier": "lcps-sarasoldphotos-00902", "error": "file not found"}],
    )

    rows = log_tab_rows(record, run="upload-20260917T180211Z.jsonl", headline="0 file(s) uploaded successfully, 1 error(s)")

    assert [row[2:] for row in rows[1:]] == [
        ["failure", "lcps-sarasoldphotos-00341", "HTTP 503 from ia"],
        ["skipped", "lcps-sarasoldphotos-00902", "file not found"],
    ]


def test_an_uploaded_but_unrecorded_item_is_labelled_as_such():
    """The outcome a reader must not mistake for a failure: the photograph IS
    on Internet Archive, and the Sheet is what is wrong."""
    record = _upload_record(
        unconfirmed=[{"identifier": "lcps-sarasoldphotos-00007", "error": "the Sheet write failed"}]
    )

    rows = log_tab_rows(record, run="upload-20260917T180211Z.jsonl", headline="1 file(s) uploaded successfully, 0 error(s)")

    assert rows[1][2] == "unconfirmed"
    assert rows[1][3] == "lcps-sarasoldphotos-00007"


def test_a_sync_record_needs_no_special_casing():
    """Both commands' summaries name their problems under the same keys, so
    one function serves both tabs. A sync record simply has no `unconfirmed`
    list - it writes nothing to the Sheet that could fail to land."""
    record = {
        "record": "run_summary",
        "timestamp": "2026-09-17T19:00:04Z",
        "live": True,
        "checked": 4212,
        "pushed": 1,
        "changed": 1,
        "unchanged": 0,
        "already_synced": 4211,
        "failures": [{"identifier": "lcps-sarasoldphotos-00055", "error": "Access Denied"}],
        "skipped": [],
    }

    rows = log_tab_rows(record, run="sync-metadata-20260917T190004Z.jsonl", headline="1 item(s) updated successfully, 0 unchanged, 1 error(s)")

    assert len(rows) == 2
    assert rows[0][2] == "summary"
    assert rows[1][2:] == ["failure", "lcps-sarasoldphotos-00055", "Access Denied"]


def test_every_row_is_as_wide_as_the_header():
    """A short row would shift the columns of whatever the Sheet renders
    beside it, and a tab whose columns do not line up is unreadable exactly
    when someone is reading it under pressure."""
    record = _upload_record(
        failures=[{"identifier": "lcps-sarasoldphotos-00341", "error": "HTTP 503 from ia"}]
    )

    rows = log_tab_rows(record, run="upload-20260917T180211Z.jsonl", headline="a headline")

    assert all(len(row) == len(LOG_TAB_HEADER) for row in rows)


class RecordingLogTab:
    """Stands in for sheet_client.AppendOnlyTab. Carries the cell-writing
    methods a real SheetClient would have, each of them a failure, so a test
    catches the mirror reaching for one instead of quietly proving that a
    stub without them cannot be misused."""

    def write_cells(self, updates):
        raise AssertionError("the log tab writer must never write cells")

    def read_grid(self):
        raise AssertionError("the log tab writer must never read the grid")

    def __init__(self, fail_on=None):
        self.ensured = []
        self.appended = []
        self._fail_on = fail_on

    def ensure_tab(self, header):
        if self._fail_on == "ensure_tab":
            raise RuntimeError("Sheets API returned 503")
        self.ensured.append(header)

    def append_rows(self, rows):
        if self._fail_on == "append_rows":
            raise RuntimeError("Sheets API returned 503")
        self.appended.append(rows)


def test_mirroring_creates_the_tab_then_appends_the_run():
    client = RecordingLogTab()

    mirror_run(client, _upload_record(), run="upload-1.jsonl", headline="a headline")

    assert client.ensured == [LOG_TAB_HEADER]
    assert client.appended == [
        [["2026-09-17T18:02:11Z", "upload-1.jsonl", "summary", "", "a headline"]]
    ]


def test_mirroring_only_ever_appends(capsys):
    """The one-directional guarantee. Asserted through stderr rather than
    through the stub's shape: mirror_run catches every exception, so a stub
    that merely lacks write_cells would stay green if this module started
    calling it - the AttributeError would be swallowed and reported. A silent
    stderr is what actually proves only the two expected calls were made."""
    client = RecordingLogTab()

    mirror_run(client, _upload_record(), run="upload-1.jsonl", headline="a headline")

    assert capsys.readouterr().err == ""
    assert client.ensured and client.appended


@pytest.mark.parametrize("failing_call", ["ensure_tab", "append_rows"])
def test_a_failed_mirror_is_reported_and_never_raised(failing_call, capsys):
    """The run is already over and items already exist on Internet Archive
    under permanent identifiers. A Sheets hiccup while writing telemetry must
    not turn a successful upload into a failed one - a run reported as failed
    invites a rerun, which is what mints a second identifier for the same
    photograph."""
    client = RecordingLogTab(fail_on=failing_call)

    mirror_run(client, _upload_record(), run="upload-1.jsonl", headline="a headline")

    err = capsys.readouterr().err
    assert "Sheets API returned 503" in err
    assert "The run itself completed" in err
