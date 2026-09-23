import argparse
import contextlib
import dataclasses
import io
import json
import re
import shlex
import tempfile
from argparse import Namespace
from pathlib import Path

import internetarchive
import internetarchive.session
import pytest
import requests
import urllib3
from requests.adapters import HTTPAdapter
from googleapiclient.errors import HttpError

import deployment
import google_auth
import ia_bulk
from column_map import build_column_map, grid_to_rows
from ia_bulk import (
    load_registry,
    check_identifier,
    check_required_for_upload,
    resolve_sheet_files,
    claim_key,
    survey_files,
    validate_rows,
    validate_sheet_rows,
    validate_sheet_grid,
    RowValidation,
    Readiness,
    effective_identifier,
    run_stamp,
    log_result,
    build_parser,
    build_sheet_client,
    format_field_receipt,
    format_lifecycle_summary,
    format_missing_field_lines,
    format_row_numbers,
    _format_result_lines,
    _pluralize,
    main,
    CHUNK_SIZE,
    is_rate_limit_error,
    UploadFailed,
    batch_row_numbers,
    BatchScopeError,
)
from project_config import ProjectConfig, DEFAULT_PHOTO_EXTENSIONS


class FakeResponse:
    def __init__(self, ok, status_code=200, text=""):
        self.ok = ok
        self.status_code = status_code
        self.text = text


# A fixed stand-in for run_stamp(), threaded into every test that needs a
# deterministic non-live identifier - see docs/DECISIONS.md, "Test
# identifiers carry a per-run stamp". Tests that instead need to prove the
# stamp is computed once per run (not once per row/chunk) monkeypatch
# ia_bulk.run_stamp with their own counting fake instead of this constant.
FIXED_STAMP = "20260819t090000"


def test_load_registry_reads_json(tmp_path):
    registry_path = tmp_path / "projects_registry.json"
    registry_path.write_text(
        json.dumps({"collection_key": "lcps", "projects": {"astoriaphotos": {}}}),
        encoding="utf-8",
    )

    registry = load_registry(registry_path)

    assert registry == {"collection_key": "lcps", "projects": {"astoriaphotos": {}}}


def make_registry():
    """Two projects, deliberately. A single-project registry cannot express
    the difference between "this prefix belongs to no project" and "this
    prefix belongs to somebody else's project", which is the whole of issue
    #2 - and it is what let that bug sit undetected."""
    return {"collection_key": "lcps", "projects": {"astoriaphotos": {}, "otherproject": {}}}


def make_sheet_registry(files_dir=".", **project_overrides):
    """A full project_config-shaped registry, as opposed to make_registry()'s
    bare {collection_key, projects} shell - load_project_config needs every
    REQUIRED_KEYS field populated. sheet_id/test_sheet_id are deliberately
    different values (not "the same string twice") so a test that reads the
    wrong one is distinguishable from one that reads the right one - the
    exact gap the Task 7 review found in the SheetClient tests."""
    project = {
        "mediatype": "image",
        "ia_collection": "lcpsociety",
        "sheet_id": "REAL_SHEET_ID",
        "test_sheet_id": "TEST_SHEET_ID",
        "sheet_tab": "Sheet1",
        "files_dir": files_dir,
        "file_template": "{file}",
        "required_for_upload": ["title"],
    }
    project.update(project_overrides)
    return {"collection_key": "lcps", "projects": {"astoriaphotos": project}}


def _sheet_config(**overrides) -> ProjectConfig:
    """A ProjectConfig with the same defaults as make_sheet_registry(), for
    tests that need the dataclass itself (e.g. to call a function that takes
    ProjectConfig directly) rather than a registry dict routed through
    load_project_config(). Every field is overridable, not only
    required_for_upload - files_dir and file_template are exercised by other
    tasks in this same plan that reuse this helper.

    Built via dataclasses.replace() on a fully-typed base rather than
    ProjectConfig(**defaults): unpacking a plain dict mixing str and
    tuple[str] values makes every field's type just "str | tuple[str]" to a
    type checker, which cannot narrow any individual parameter at the `**`
    unpack - ProjectConfig is a frozen dataclass, so replace() is both the
    idiomatic way to override a subset of fields and the one that keeps each
    field's real type intact."""
    base = ProjectConfig(
        project_id="astoriaphotos",
        collection_key="lcps",
        mediatype="image",
        ia_collection="lcpsociety",
        sheet_id="REAL_SHEET_ID",
        test_sheet_id="TEST_SHEET_ID",
        sheet_tab="Sheet1",
        files_dir=".",
        file_template="{file}",
        required_for_upload=("title",),
        photo_extensions=DEFAULT_PHOTO_EXTENSIONS,
        batch_column=None,
    )
    return dataclasses.replace(base, **overrides)


class FakeSheetClient:
    """Stands in for sheet_client.SheetClient in cmd_validate tests -
    build_sheet_client is the one seam those tests monkeypatch, so nothing
    here ever touches google_auth or googleapiclient.discovery."""

    def __init__(self, grid):
        self._grid = grid

    def read_grid(self):
        return self._grid


class RaisingSheetClient:
    """Stands in for SheetClient when a test needs read_grid() to raise -
    e.g. an HttpError from a wrong tab name or a not-yet-shared Sheet."""

    def __init__(self, exc):
        self._exc = exc

    def read_grid(self):
        raise self._exc


def make_http_error(message="Unable to parse range: Sheet1", status=400):
    """A realistic HttpError, as googleapiclient actually raises it: the
    real .resp needs a .status and .reason, and .content must be the raw
    JSON error body Google's API returns, not a plain string."""

    class _FakeHttpResponse:
        def __init__(self, status, reason):
            self.status = status
            self.reason = reason

    content = json.dumps({"error": {"message": message}}).encode("utf-8")
    return HttpError(
        _FakeHttpResponse(status, reason="Bad Request"),
        content,
        uri="https://sheets.googleapis.com/v4/spreadsheets/TEST_SHEET_ID/values/Sheet1",
    )


def test_check_identifier_accepts_valid_registered_identifier():
    errors = check_identifier(
        "lcps-astoriaphotos-00001",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert errors == []


def test_check_identifier_rejects_bad_scheme():
    errors = check_identifier(
        "LCPS_astoriaphotos_1",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert len(errors) == 1
    assert "does not match scheme" in errors[0]


def test_check_identifier_rejects_unknown_prefix():
    errors = check_identifier(
        "lcps-unknownproject-00001",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert len(errors) == 1
    assert "not found in project registry" in errors[0]


def test_check_identifier_rejects_zztest_prefix_since_rows_always_hold_real_identifiers():
    # A row always holds the real, permanent identifier — "zztest-" prefixing
    # is applied by effective_identifier() at network-call time, never authored.
    errors = check_identifier(
        "zztest-astoriaphotos-00001",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert len(errors) == 1
    assert "not found in project registry" in errors[0]


def test_check_identifier_rejects_duplicate():
    seen = {"lcps-astoriaphotos-00001": 2}
    errors = check_identifier(
        "lcps-astoriaphotos-00001",
        row_number=5,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers=seen,
    )
    assert len(errors) == 1
    assert "duplicates row 2" in errors[0]


def test_check_identifier_rejects_empty():
    errors = check_identifier(
        "",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert errors == ["missing required column 'identifier'"]


def test_check_identifier_column_name_defaults_to_identifier():
    """Pins the default column_name's wording exactly."""
    errors = check_identifier(
        "LCPS_astoriaphotos_1",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert errors == [
        "identifier 'LCPS_astoriaphotos_1' does not match scheme COLLECTIONKEY-PROJECTID-NUMBER"
    ]


def test_check_identifier_names_the_column_it_checked():
    """A Sheet with both a donor 'Identifier' column (untouched metadata)
    and 'ia_identifier' (the tool's minted one) needs its error messages to
    say WHICH column is wrong - the bare word 'identifier' is ambiguous
    between the two and sends a volunteer to fix the wrong one."""
    errors = check_identifier(
        "lcps-astoriaphotos-00001",
        row_number=5,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={"lcps-astoriaphotos-00001": 2},
        column_name="ia_identifier",
    )
    assert errors == ["ia_identifier 'lcps-astoriaphotos-00001' duplicates row 2"]


def test_check_identifier_names_the_column_for_a_blank_value_too():
    errors = check_identifier(
        "",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
        column_name="ia_identifier",
    )
    assert errors == ["missing required column 'ia_identifier'"]


# --- issue #2: an identifier must belong to the run's OWN project ---------


def test_check_identifier_rejects_another_registered_projects_identifier():
    """Issue #2. 'lcps-otherproject-00099' is a perfectly well-formed
    identifier of a project the registry knows - it is simply not THIS run's
    project. Accepting it files an item under another project's numbering
    under a name that can never be renamed."""
    errors = check_identifier(
        "lcps-otherproject-00099",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert len(errors) == 1
    assert "otherproject" in errors[0]
    assert "astoriaphotos" in errors[0]


def test_check_identifier_accepts_the_runs_own_project():
    errors = check_identifier(
        "lcps-astoriaphotos-00001",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert errors == []


def test_check_identifier_keeps_unknown_prefix_distinct_from_wrong_project():
    """Two different mistakes needing two different fixes: an unregistered
    prefix means the identifier is wrong, a wrong-project prefix means
    --project may be the thing that is wrong. One message for both would
    send the operator to the wrong file."""
    unknown = check_identifier(
        "lcps-nosuchproject-00001",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    wrong_project = check_identifier(
        "lcps-otherproject-00001",
        row_number=3,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
    )
    assert "not found in project registry" in unknown[0]
    assert "not found in project registry" not in wrong_project[0]


def test_check_identifier_names_the_column_for_a_wrong_project_too():
    """The Sheet path's ia_identifier/identifier ambiguity applies to this
    message exactly as it does to every other one."""
    errors = check_identifier(
        "lcps-otherproject-00099",
        row_number=2,
        registry=make_registry(),
        project_id="astoriaphotos",
        seen_identifiers={},
        column_name="ia_identifier",
    )
    assert errors[0].startswith("ia_identifier ")


def test_validate_sheet_rows_rejects_another_projects_identifier(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"x")
    rows = [
        {
            "ia_identifier": "lcps-otherproject-00099",
            "file": "a.jpg",
            "mediatype": "image",
            "title": "First",
        }
    ]

    results = validate_sheet_rows(rows, tmp_path, make_registry(), "astoriaphotos")

    assert not results[0].is_valid
    assert any("otherproject" in e for e in results[0].errors)


def test_validate_sheet_rows_flags_a_file_missing_from_disk(tmp_path):
    """The disk re-check after file resolution; see docs/KNOWN-ISSUES.md #5."""
    rows = [
        {
            "ia_identifier": "",
            "file": "does-not-exist.jpg",
            "mediatype": "image",
            "title": "First photo",
        }
    ]

    results = validate_sheet_rows(rows, tmp_path, make_registry(), "astoriaphotos")

    assert not results[0].is_valid
    assert results[0].errors == [f"file not found: {tmp_path / 'does-not-exist.jpg'}"]


def test_validate_rows_passes_a_fully_valid_row(tmp_path):
    (tmp_path / "photo1.jpg").write_bytes(b"fake-image-bytes")
    rows = [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "photo1.jpg",
            "mediatype": "image",
            "title": "First photo",
            "date": "1958",
        }
    ]

    results = validate_rows(
        rows, files_dir=tmp_path, registry=make_registry(), project_id="astoriaphotos"
    )

    assert len(results) == 1
    assert results[0].is_valid
    assert results[0].errors == []


def test_validate_rows_flags_missing_file():
    rows = [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "does-not-exist.jpg",
            "mediatype": "image",
            "title": "First photo",
            "date": "1958",
        }
    ]

    results = validate_rows(
        rows, files_dir="/tmp", registry=make_registry(), project_id="astoriaphotos"
    )

    assert not results[0].is_valid
    assert any("file not found" in e for e in results[0].errors)


def test_validate_rows_flags_missing_required_metadata(tmp_path):
    (tmp_path / "photo1.jpg").write_bytes(b"fake-image-bytes")
    rows = [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "photo1.jpg",
            "mediatype": "",
            "title": "",
            "date": "1958",
        }
    ]

    results = validate_rows(
        rows, files_dir=tmp_path, registry=make_registry(), project_id="astoriaphotos"
    )

    assert not results[0].is_valid
    assert "missing required column 'mediatype'" in results[0].errors
    assert "missing required column 'title'" in results[0].errors


def test_validate_rows_does_not_require_date(tmp_path):
    (tmp_path / "photo1.jpg").write_bytes(b"fake-image-bytes")
    rows = [
        {
            "identifier": "lcps-astoriaphotos-00001",
            "file": "photo1.jpg",
            "mediatype": "image",
            "title": "First photo",
            "date": "",
        }
    ]

    results = validate_rows(
        rows, files_dir=tmp_path, registry=make_registry(), project_id="astoriaphotos"
    )

    assert results[0].is_valid


def test_validate_rows_row_numbers_start_at_2_for_header():
    rows = [
        {
            "identifier": "",
            "file": "",
            "mediatype": "",
            "title": "",
            "date": "",
        }
    ]

    results = validate_rows(
        rows, files_dir="/tmp", registry=make_registry(), project_id="astoriaphotos"
    )

    assert results[0].row_number == 2


def test_survey_files_separates_resolved_rows_from_unresolved(tmp_path):
    """The claimed set is what stops two rows being pointed at one file -
    the misattribution hazard in issue #1. It is built before any matching."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Good.jpg").write_bytes(b"x")
    (folder / "Finnish Meat Market.jpg").write_bytes(b"x")
    (folder / "contact sheet.pdf").write_bytes(b"x")

    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [
        {"folder": "SOP CD 1", "name": "Good.jpg"},
        {"folder": "SOP CD 1", "name": "Finnis Meat Market.jpg"},
    ]

    survey = survey_files(rows, config)

    assert survey.unresolved == {3: "SOP CD 1"}
    assert survey.wanted == {3: "Finnis Meat Market.jpg"}
    assert survey.claimed == {claim_key("SOP CD 1/Good.jpg")}
    # the resolved file is not offered as a candidate, and the PDF is excluded
    assert survey.unclaimed == {"SOP CD 1": ["Finnish Meat Market.jpg"]}


def test_claim_key_folds_case_on_macos_where_normcase_is_the_identity(monkeypatch):
    """os.path.normcase goes by platform convention, not the actual
    filesystem: on macOS it is the identity even though the default
    filesystem (APFS) is case-insensitive like Windows's. Without a fold of
    our own, `SOP CD 1` and `sop cd 1` get two disjoint claim namespaces
    there and the two-rows-one-file guard silently misses. The exact-value
    assert is what exercises the darwin branch on any host: Windows's
    normcase would flip the slash to a backslash, POSIX's would keep the
    capitals."""
    monkeypatch.setattr("sys.platform", "darwin")
    assert claim_key("SOP CD 1/Photo.JPG") == "sop cd 1/photo.jpg"
    assert claim_key("SOP CD 1/Photo.JPG") == claim_key("sop cd 1/photo.jpg")


def test_scan_unclaimed_files_covers_folders_no_row_names(tmp_path):
    """The whole reason append cannot reuse survey_files' unclaimed: that
    map only lists folders some catalogued row already names, but a brand-new
    donor folder with zero rows is exactly what append exists to pick up."""
    from ia_bulk import scan_unclaimed_files

    folder = tmp_path / "SOP CD 2 COE"
    folder.mkdir()
    (folder / "002_westport_tunnel.JPG").write_bytes(b"x")
    (folder / "001_seaside_beach.JPG").write_bytes(b"x")
    config = _sheet_config(files_dir=str(tmp_path))

    unclaimed, outside = scan_unclaimed_files(set(), config)

    assert unclaimed == {"SOP CD 2 COE": ["001_seaside_beach.JPG", "002_westport_tunnel.JPG"]}
    assert outside == []


def test_scan_unclaimed_files_excludes_claimed_files_and_non_photos(tmp_path):
    from ia_bulk import scan_unclaimed_files

    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Taken.jpg").write_bytes(b"x")
    (folder / "Free.jpg").write_bytes(b"x")
    (folder / "contact sheet.pdf").write_bytes(b"x")
    config = _sheet_config(files_dir=str(tmp_path))

    unclaimed, _ = scan_unclaimed_files({claim_key("SOP CD 1/Taken.jpg")}, config)

    assert unclaimed == {"SOP CD 1": ["Free.jpg"]}


def test_scan_unclaimed_files_omits_a_folder_with_nothing_unclaimed(tmp_path):
    """No empty entries: a folder whose every photo is claimed has nothing
    to append, and an empty list would still render a folder heading."""
    from ia_bulk import scan_unclaimed_files

    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Taken.jpg").write_bytes(b"x")
    config = _sheet_config(files_dir=str(tmp_path))

    unclaimed, _ = scan_unclaimed_files({claim_key("SOP CD 1/Taken.jpg")}, config)

    assert unclaimed == {}


def test_scan_unclaimed_files_reports_photos_outside_any_folder(tmp_path):
    """A photo at the top of files_dir cannot be represented by a
    folder/name template, so it cannot get a row - but it must be COUNTED,
    not silently invisible, or a stray file at the drive root never gets
    catalogued and nobody is ever told."""
    from ia_bulk import scan_unclaimed_files

    (tmp_path / "stray.jpg").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Good.jpg").write_bytes(b"x")
    config = _sheet_config(files_dir=str(tmp_path))

    unclaimed, outside = scan_unclaimed_files(set(), config)

    assert unclaimed == {"SOP CD 1": ["Good.jpg"]}
    assert outside == ["stray.jpg"]


def test_survey_files_files_a_blank_filename_cell_as_not_ready_not_unresolved(tmp_path):
    """A row that asserted no file is not-ready, not broken - the same split
    resolve_sheet_files() draws between `errors` and `blank`. Filed as
    unresolved it becomes a prompt with no proposal and no candidates, once
    per uncatalogued row, which on the real Sheet is ~2,900 of them."""
    (tmp_path / "SOP CD 1").mkdir()
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    survey = survey_files([{"folder": "SOP CD 1", "name": ""}], config)
    assert survey.unresolved == {}
    assert survey.wanted == {}
    assert survey.not_ready == [2]


def test_survey_files_claims_one_file_once_across_case_divergent_folder_cells(tmp_path):
    """`SOP CD 1` and `sop cd 1` are one folder on a case-insensitive
    filesystem. Keyed by the raw folder cell the two rows get two disjoint
    namespaces, the file a resolved row already claims still shows up as
    unclaimed under the other spelling, and two rows can be pointed at one
    photograph."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Good.jpg").write_bytes(b"x")
    (folder / "Finnish Meat Market.jpg").write_bytes(b"x")

    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [
        {"folder": "SOP CD 1", "name": "Good.jpg"},
        {"folder": "sop cd 1", "name": "Finnis Meat Market.jpg"},
    ]

    survey = survey_files(rows, config)

    assert "Good.jpg" not in survey.unclaimed.get("sop cd 1", [])


def test_survey_files_lists_an_uppercase_extension_as_a_candidate(tmp_path):
    """161 of the 189 sample files are `.JPG`. A regression here would make
    a whole folder invisible as candidates, and would present as "the tool
    just never proposes anything" rather than as an error."""
    folder = tmp_path / "SOP CD 2 COE"
    folder.mkdir()
    (folder / "001_seaside_beach.JPG").write_bytes(b"x")

    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    survey = survey_files([{"folder": "SOP CD 2 COE", "name": "Nothing Like It.jpg"}], config)

    assert survey.unclaimed == {"SOP CD 2 COE": ["001_seaside_beach.JPG"]}


def test_validate_rows_default_required_columns_still_requires_identifier_and_file(tmp_path):
    """Pins validate_rows' required_columns default at REQUIRED_UPLOAD_COLUMNS.
    If the default were ever flipped to SHEET_REQUIRED_COLUMNS (which excludes
    identifier only), a row with both blank would become "valid" for identifier,
    and effective_identifier("", live=False, stamp) returns just
    "zztest-<stamp>-" - not a real identifier."""
    rows = [
        {
            "identifier": "",
            "file": "",
            "mediatype": "image",
            "title": "First photo",
            "date": "1958",
        }
    ]

    results = validate_rows(
        rows, files_dir=tmp_path, registry=make_registry(), project_id="astoriaphotos"
    )

    assert not results[0].is_valid
    assert "missing required column 'identifier'" in results[0].errors
    assert "missing required column 'file'" in results[0].errors


def test_validate_rows_identifier_column_lets_the_sheet_path_read_ia_identifier(tmp_path):
    """After Task 9 the Sheet's own 'identifier' column holds a donor
    reference like 'CD 1 01 53 58 1 Central SS', not a minted IA
    identifier - running check_identifier's COLLECTIONKEY-PROJECTID-NUMBER
    regex against it would fail every row for the wrong reason.
    identifier_column lets the Sheet path point validate_rows at
    'ia_identifier' instead; the default ('identifier') is pinned
    separately above."""
    (tmp_path / "photo1.jpg").write_bytes(b"x")
    rows = [
        {
            "identifier": "CD 1 01 53 58 1 Central SS",
            "ia_identifier": "",
            "file": "photo1.jpg",
            "mediatype": "image",
            "title": "First photo",
        }
    ]

    results = validate_rows(
        rows,
        files_dir=tmp_path,
        registry=make_registry(),
        project_id="astoriaphotos",
        required_columns=("mediatype", "title", "file"),
        identifier_column="ia_identifier",
    )

    assert results[0].is_valid
    assert results[0].identifier == ""


def test_format_report_attributes_header_errors_to_row_1():
    from ia_bulk import format_report

    report = format_report([RowValidation(row_number=1, identifier="", errors=["the header row is blank"])])

    assert "row 1" in report
    assert "the header row is blank" in report


def test_format_report_shows_pass_and_fail_with_summary():
    from ia_bulk import format_report

    results = [
        RowValidation(row_number=2, identifier="lcps-astoriaphotos-00001", errors=[]),
        RowValidation(
            row_number=3,
            identifier="lcps-astoriaphotos-00002",
            errors=["file not found: /tmp/missing.jpg"],
        ),
    ]

    report = format_report(results)

    assert "[PASS] row 2 lcps-astoriaphotos-00001" in report
    assert "[FAIL] row 3 lcps-astoriaphotos-00002" in report
    assert "file not found: /tmp/missing.jpg" in report
    assert "1/2 rows passed" in report


def test_format_report_does_not_duplicate_the_row_number_for_a_blank_identifier():
    """A blank identifier is the normal state of an unassigned Sheet row
    (RowState.UNASSIGNED), not a special case worth restating the row
    number for - "[PASS] row 2 (row 2)" said nothing "[PASS] row 2" didn't
    already say, and reads as a bug on every passing Sheet row."""
    from ia_bulk import format_report

    report = format_report([RowValidation(row_number=2, identifier="", errors=[])])

    assert "[PASS] row 2" in report
    assert "(row 2)" not in report


def test_field_receipt_lists_uploadable_fields_and_held_back_ones():
    column_map = build_column_map(
        ["Title", "Genre / Form", "Notes (LCPS Internal)", "identifier"]
    )

    receipt = format_field_receipt(column_map)

    assert "title" in receipt
    assert "genre_form" in receipt
    assert "held back" in receipt
    assert "Notes (LCPS Internal)" in receipt
    # reserved columns are never uploaded, so they must not read as fields
    assert "identifier," not in receipt


def test_field_receipt_says_a_mediatype_or_collection_column_has_its_value_ignored():
    """upload_from_sheet overwrites row['mediatype'] from the registry and
    upload_row sets metadata['collection'] unconditionally, so a Sheet column
    of either name never ships its own value. Listing them under "will upload
    these metadata fields" told the operator the opposite, on the receipt
    printed immediately before something permanent happens."""
    column_map = build_column_map(["Title", "Mediatype", "Collection"])

    receipt = format_field_receipt(column_map)

    will_upload, _, rest = receipt.partition("uploaded with a value this tool generates")
    assert "title" in will_upload
    # the whole point: neither may read as a field whose value is sent
    assert "mediatype" not in will_upload
    assert "collection" not in will_upload
    assert "mediatype" in rest
    assert "collection" in rest
    assert "IGNORED" in receipt


def test_field_receipt_omits_the_generated_section_when_no_column_collides():
    """The section is a collision warning, not a standing disclaimer - a Sheet
    with no mediatype/collection column of its own has nothing to be warned
    about, and an always-on section is what teaches an operator to skip it."""
    column_map = build_column_map(["Title", "Genre / Form"])

    receipt = format_field_receipt(column_map)

    assert "uploaded with a value this tool generates" not in receipt


def test_field_receipt_lists_identifier_as_reserved_not_as_generated():
    """`identifier` is in both DROPPED_BY_UPLOAD_ROW and
    PIPELINE_OWNED_FIELDS. Listing it twice, under two different headings,
    reads as two different columns."""
    column_map = build_column_map(["Title", "identifier"])

    receipt = format_field_receipt(column_map)

    assert receipt.count("identifier") == 1
    assert "Internet Archive reserves these names" in receipt
    assert "uploaded with a value this tool generates" not in receipt


def test_sheet_structure_validation_files_a_grid_shape_error_under_its_own_row_not_row_1():
    """check_grid_shape's message already names the real row number (e.g.
    "row 3 has..."); filing it under row 1 regardless - which an earlier
    version of this function did - puts a row-3 problem under the heading a
    volunteer reads as "the header row"."""
    from ia_bulk import sheet_structure_validation

    grid = [
        ["Title", "file"],
        ["First", "photo1.jpg"],
        ["Second", "photo2.jpg", "unexpected extra cell"],
    ]
    column_map = build_column_map(grid[0])

    results = sheet_structure_validation(column_map, grid)

    assert len(results) == 1
    assert results[0].row_number == 3
    assert "more field(s) than the header" in results[0].errors[0]


def test_sheet_structure_validation_files_a_header_collision_under_row_1():
    """Two headers colliding is genuinely a header-level problem - not
    about any one data row - so it stays under row 1."""
    from ia_bulk import sheet_structure_validation

    grid = [["Genre / Form", "Genre_ Form"], ["a", "b"]]
    column_map = build_column_map(grid[0])

    results = sheet_structure_validation(column_map, grid)

    assert len(results) == 1
    assert results[0].row_number == 1
    assert "genre_form" in results[0].errors[0]


def test_sheet_structure_validation_files_a_header_problem_and_a_shape_problem_separately():
    """Both defects can be present in the same Sheet at once, and must be
    filed under their own distinct rows rather than merged into a single
    row-1 entry."""
    from ia_bulk import sheet_structure_validation

    grid = [
        ["Genre / Form", "Genre_ Form", "Title"],
        ["a", "b", "First"],
        ["c", "d", "Second", "unexpected extra cell"],
    ]
    column_map = build_column_map(grid[0])

    results = sheet_structure_validation(column_map, grid)

    by_row = {result.row_number: result.errors[0] for result in results}
    assert set(by_row) == {1, 3}
    assert "genre_form" in by_row[1]
    assert "more field(s) than the header" in by_row[3]


def test_lifecycle_summary_counts_each_state():
    rows = [
        {"ia_identifier": "", "ia_uploaded": ""},
        {"ia_identifier": "", "ia_uploaded": ""},
        {"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""},
        {"ia_identifier": "lcps-astoriaphotos-00002", "ia_uploaded": "2026-08-08T10:00:00"},
    ]
    # row_results must line up 1:1 with rows, in order; all pass here so
    # this test pins pure lifecycle counting, independent of the
    # fails-validation case pinned separately below.
    row_results = [RowValidation(row_number=i + 2, identifier="") for i in range(len(rows))]

    summary = format_lifecycle_summary(rows, row_results)

    assert "2 rows ready to upload" in summary
    assert "1 already uploaded" in summary
    assert "1 reserved but unconfirmed" in summary


def test_lifecycle_summary_uses_singular_row_for_a_count_of_one():
    rows = [{"ia_identifier": "", "ia_uploaded": ""}]
    row_results = [RowValidation(row_number=2, identifier="")]

    summary = format_lifecycle_summary(rows, row_results)

    assert "1 row ready to upload" in summary
    assert "1 rows ready to upload" not in summary


def test_pluralize_separates_thousands():
    """README quotes `2,914 rows not yet catalogued` as sample output, and
    the real Sheet's headline counts sit near 3,000 - the separator is what
    keeps the doc's sample and the tool's output the same string."""
    assert _pluralize(2914, "row") == "2,914 rows"
    assert _pluralize(1, "row") == "1 row"


def test_lifecycle_summary_does_not_count_a_failed_unassigned_row_as_ready():
    """The bug the coordinator caught: classify_row() alone can't see
    validation results, so a row with a blank identifier that actually
    failed validation (missing title, say) was counted as "ready to
    upload" right next to a report saying that same row failed. Counts
    must be cross-referenced against row_results, and a failed-but-
    unassigned row must be called out separately rather than folded into
    either bucket silently."""
    rows = [{"ia_identifier": "", "ia_uploaded": ""}]
    row_results = [
        RowValidation(row_number=2, identifier="", errors=["missing required column 'title'"])
    ]

    summary = format_lifecycle_summary(rows, row_results)

    assert "0 rows ready to upload" in summary
    assert "1 row" in summary and "failed validation" in summary


def test_lifecycle_summary_does_not_count_a_failed_done_row_as_already_uploaded():
    """Same contradiction as the UNASSIGNED case, in the DONE bucket: a row
    classify_row() calls DONE (has both identifier and ia_uploaded) but that
    now fails validation (a duplicate identifier, say) is not cleanly
    "already uploaded" - it needs a human to look, not silent inclusion in
    a bucket that implies everything is fine."""
    rows = [{"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": "2026-08-08T10:00:00"}]
    row_results = [
        RowValidation(
            row_number=2,
            identifier="lcps-astoriaphotos-00001",
            errors=["identifier 'lcps-astoriaphotos-00001' duplicates row 5"],
        )
    ]

    summary = format_lifecycle_summary(rows, row_results)

    assert "0 already uploaded" in summary
    assert "1 row already uploaded but now fail validation" in summary


def test_lifecycle_summary_does_not_count_a_failed_reserved_row_as_will_retry():
    """The coordinator's exact example: rows with a duplicate identifier or
    an unregistered project prefix were reported as "3 reserved but
    unconfirmed - will retry under existing identifier" - a forward-looking
    promise a row failing identifier validation cannot keep."""
    rows = [{"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""}]
    row_results = [
        RowValidation(
            row_number=2,
            identifier="lcps-astoriaphotos-00001",
            errors=["identifier 'lcps-astoriaphotos-00001' duplicates row 5"],
        )
    ]

    summary = format_lifecycle_summary(rows, row_results)

    assert "0 reserved but unconfirmed" in summary
    assert "1 row reserved but invalid" in summary
    assert "will NOT retry automatically" in summary


def test_lifecycle_summary_counts_always_sum_to_the_total_row_count():
    """Every row falls into exactly one of the six buckets (three
    classify_row() states, each split into valid/invalid), so their counts
    must always add up to len(rows) - a regression that double-counts or
    drops a row on some branch would break this without necessarily
    breaking any single-bucket assertion."""
    rows = [
        {"ia_identifier": "", "ia_uploaded": ""},
        {"ia_identifier": "", "ia_uploaded": ""},
        {"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""},
        {"ia_identifier": "lcps-astoriaphotos-00002", "ia_uploaded": "2026-08-08T10:00:00"},
    ]
    row_results = [
        RowValidation(row_number=2, identifier="", errors=[]),
        RowValidation(row_number=3, identifier="", errors=["missing required column 'title'"]),
        RowValidation(
            row_number=4,
            identifier="lcps-astoriaphotos-00001",
            errors=["identifier 'lcps-astoriaphotos-00001' duplicates row 9"],
        ),
        RowValidation(row_number=5, identifier="lcps-astoriaphotos-00002", errors=[]),
    ]

    summary = format_lifecycle_summary(rows, row_results)

    counts = [int(match) for match in re.findall(r"^(\d+)", summary, flags=re.MULTILINE)]
    assert sum(counts) == len(rows)


def test_lifecycle_summary_raises_on_mismatched_lengths_instead_of_silently_truncating():
    """zip(rows, row_results) truncates to the shorter list without
    raising - a caller passing the wrong list (e.g. the combined report
    instead of just the row results) would silently get wrong-but-
    plausible-looking counts instead of an obvious failure. That is exactly
    the kind of silent wrongness this function exists to prevent, so a
    length mismatch must raise rather than zip quietly."""
    with pytest.raises(ValueError, match="same length"):
        format_lifecycle_summary(
            rows=[{"identifier": ""}, {"identifier": ""}],
            row_results=[RowValidation(row_number=2, identifier="")],
        )


def _one_row_in(state: str, kind: str) -> tuple[list[dict[str, str]], list[RowValidation]]:
    """One row/result pair shaped to land in exactly the (state, kind)
    lifecycle-summary bucket named by its arguments - the 3 (classify_row
    state) x 3 (readiness/validity bucket) cross Task 7's rendering fans
    out into, plus a fourth `kind` used only to pin the precedence rule
    between not-ready and invalid.

    `state` is classify_row()'s own vocabulary ("unassigned"/"reserved"/
    "done" - see identifiers.RowState/classify_row). `kind` is the bucket
    format_lifecycle_summary now counts: "ready_valid" (catalogued and
    passes validation - the headline count), "ready_invalid" (catalogued
    but fails validation), "not_ready" (missing a required field, no
    validation errors), or "not_ready_and_invalid" (both at once - must
    still land in not_ready alone, per the precedence rule)."""
    row_shapes = {
        "unassigned": {"ia_identifier": "", "ia_uploaded": ""},
        "reserved": {"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": ""},
        "done": {
            "ia_identifier": "lcps-astoriaphotos-00001",
            "ia_uploaded": "2026-08-08T10:00:00",
        },
    }
    row = dict(row_shapes[state])
    identifier = row["ia_identifier"]

    if kind == "ready_valid":
        result = RowValidation(row_number=2, identifier=identifier)
    elif kind == "ready_invalid":
        result = RowValidation(row_number=2, identifier=identifier, errors=["some validation error"])
    elif kind == "not_ready":
        result = RowValidation(row_number=2, identifier=identifier, missing_fields=["title"])
    elif kind == "not_ready_and_invalid":
        result = RowValidation(
            row_number=2,
            identifier=identifier,
            errors=["some validation error"],
            missing_fields=["title"],
        )
    else:
        raise ValueError(f"_one_row_in: unknown kind {kind!r}")

    return [row], [result]


def _mixed_grid() -> tuple[list[dict[str, str]], list[RowValidation]]:
    """Nine rows, one per (state, bucket) combination format_lifecycle_summary
    now counts - proves all nine sum to len(rows) in a single call, not just
    in isolation per bucket the way test_every_bucket_is_reachable does."""
    rows: list[dict[str, str]] = []
    results: list[RowValidation] = []
    row_number = 2
    for state in ("unassigned", "done", "reserved"):
        for kind in ("ready_valid", "ready_invalid", "not_ready"):
            one_rows, one_results = _one_row_in(state, kind)
            one_results[0].row_number = row_number
            rows.extend(one_rows)
            results.extend(one_results)
            row_number += 1
    return rows, results


@pytest.mark.parametrize("state", ["unassigned", "reserved", "done"])
@pytest.mark.parametrize("kind", ["ready_valid", "ready_invalid", "not_ready"])
def test_every_bucket_is_reachable(state, kind):
    rows, results = _one_row_in(state, kind)
    summary = format_lifecycle_summary(rows, results)
    assert summary  # each combination renders without raising


def test_counts_sum_to_total_across_mixed_rows():
    rows, results = _mixed_grid()  # 9 rows, one per bucket
    summary = format_lifecycle_summary(rows, results)
    counted = sum(int(n) for n in re.findall(r"^(\d+) ", summary, re.MULTILINE))
    assert counted == len(rows)


def test_not_ready_takes_precedence_over_invalid():
    """A row that is both must be counted ONCE, in not-ready - never
    doubled, and never miscounted as merely invalid."""
    rows, results = _one_row_in("unassigned", "not_ready_and_invalid")
    summary = format_lifecycle_summary(rows, results)
    counted = sum(int(n) for n in re.findall(r"^(\d+) ", summary, re.MULTILINE))
    assert counted == 1
    # Not just "the numbers add up to one" - assert directly that the row
    # landed in the not-ready line and never in an "invalid"/"failed
    # validation" line, which a mutation that instead counted it as
    # invalid-only could still satisfy the sum-to-one check above.
    lines = summary.splitlines()
    assert (
        "1 row not yet assigned an identifier and not yet catalogued (missing "
        "required fields) - waiting on data entry, not blocked by an error"
    ) in lines
    assert not any("failed validation" in line for line in lines)


def test_lifecycle_summary_prints_exact_text_for_a_not_ready_done_row():
    """Global-constraint exact-line pin: asserts the full rendered TEXT of
    a new (Task 7) line via splitlines() membership, not a substring of the
    whole blob, so a mutation that changes wording or indentation while
    keeping the arithmetic correct still fails. Uses the DONE state's
    not-ready line specifically since that is the least intuitive of the
    nine buckets (a row already uploaded that is somehow still missing a
    required field - see format_lifecycle_summary's docstring)."""
    rows, results = _one_row_in("done", "not_ready")
    summary = format_lifecycle_summary(rows, results)
    assert (
        "1 row already uploaded but missing required fields - a required "
        "column was cleared after upload; needs a human to look, not an "
        "automatic retry"
    ) in summary.splitlines()


class _RecordingSheetsValues:
    """Records the exact spreadsheetId/range passed to values().get(), so a
    test can tell a client reading the correct Sheet apart from one reading
    a hardcoded-wrong one - the gap the Task 7 review found (all 7 of that
    task's original tests stayed green even with the wrong spreadsheetId
    hardcoded into both SheetClient methods)."""

    def __init__(self, response):
        self.get_calls = []
        self._response = response

    def get(self, spreadsheetId, range):
        self.get_calls.append((spreadsheetId, range))
        return _RecordingExecutable(self._response)


class _RecordingExecutable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _RecordingSheetsService:
    def __init__(self, response):
        self.values_api = _RecordingSheetsValues(response)

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_api


def test_build_sheet_client_reads_the_real_sheet_id_when_live(monkeypatch):
    fake_service = _RecordingSheetsService({"values": [["Title"]]})
    monkeypatch.setattr(
        "ia_bulk.google_auth.load_service_account_credentials", lambda key_path: "FAKE_CREDS"
    )
    monkeypatch.setattr("ia_bulk.googleapiclient.discovery.build", lambda *a, **k: fake_service)
    config = ProjectConfig(
        project_id="astoriaphotos",
        collection_key="lcps",
        mediatype="image",
        ia_collection="lcpsociety",
        sheet_id="REAL_SHEET_ID",
        test_sheet_id="TEST_SHEET_ID",
        sheet_tab="Donor Photos",
        files_dir=".",
        file_template="{file}",
        required_for_upload=("title",),
        photo_extensions=DEFAULT_PHOTO_EXTENSIONS,
        batch_column=None,
    )

    client = build_sheet_client(config, live=True)
    client.read_grid()

    assert fake_service.values_api.get_calls == [("REAL_SHEET_ID", "'Donor Photos'")]


def test_build_sheet_client_reads_the_test_sheet_id_when_not_live(monkeypatch):
    fake_service = _RecordingSheetsService({"values": [["Title"]]})
    monkeypatch.setattr(
        "ia_bulk.google_auth.load_service_account_credentials", lambda key_path: "FAKE_CREDS"
    )
    monkeypatch.setattr("ia_bulk.googleapiclient.discovery.build", lambda *a, **k: fake_service)
    config = ProjectConfig(
        project_id="astoriaphotos",
        collection_key="lcps",
        mediatype="image",
        ia_collection="lcpsociety",
        sheet_id="REAL_SHEET_ID",
        test_sheet_id="TEST_SHEET_ID",
        sheet_tab="Donor Photos",
        files_dir=".",
        file_template="{file}",
        required_for_upload=("title",),
        photo_extensions=DEFAULT_PHOTO_EXTENSIONS,
        batch_column=None,
    )

    client = build_sheet_client(config, live=False)
    client.read_grid()

    assert fake_service.values_api.get_calls == [("TEST_SHEET_ID", "'Donor Photos'")]


def test_build_sheet_client_passes_credentials_through_to_discovery_build(monkeypatch):
    captured = {}

    def fake_load_service_account_credentials(key_path):
        captured["key_path"] = key_path
        return "FAKE_CREDS"

    def fake_build(api, version, credentials):
        captured["api"] = api
        captured["version"] = version
        captured["credentials"] = credentials
        return _RecordingSheetsService({"values": []})

    monkeypatch.setattr(
        "ia_bulk.google_auth.load_service_account_credentials",
        fake_load_service_account_credentials,
    )
    monkeypatch.setattr("ia_bulk.googleapiclient.discovery.build", fake_build)
    config = ProjectConfig(
        project_id="astoriaphotos",
        collection_key="lcps",
        mediatype="image",
        ia_collection="lcpsociety",
        sheet_id="REAL_SHEET_ID",
        test_sheet_id="TEST_SHEET_ID",
        sheet_tab="Sheet1",
        files_dir=".",
        file_template="{file}",
        required_for_upload=("title",),
        photo_extensions=DEFAULT_PHOTO_EXTENSIONS,
        batch_column=None,
    )

    build_sheet_client(config, live=True)

    assert captured["credentials"] == "FAKE_CREDS"
    assert captured["api"] == "sheets"
    assert captured["version"] == "v4"
    assert captured["key_path"] == google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH


def test_cmd_validate_reads_the_sheet_and_injects_mediatype(
    tmp_path, monkeypatch, capsys
):
    """Proves mandatory addition #2: mediatype is never a Sheet column, so
    without injecting it from the registry this row would fail with
    "missing required column 'mediatype'" and the exit code/pass-count
    assertions below would flip. Phase 2 (Task 9) requires the file to
    actually resolve on disk, so files_dir points at a real directory
    containing the named file - a regression that broke resolution would
    fail this test too."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "file"], ["First photo", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1/1 rows passed" in out
    assert "will upload these metadata fields:" in out
    assert "1 row ready to upload" in out
    assert "suggestions (advisory - nothing is changed automatically):" in out


def test_cmd_validate_does_not_treat_a_blank_ia_identifier_as_an_error(tmp_path, monkeypatch, capsys):
    """A blank ia_identifier is the normal starting state of every new
    Sheet row under minting, not an error - this is the behavior SHEET_REQUIRED_COLUMNS (which
    excludes ia_identifier) exists to produce. Uses 'ia_identifier', not
    'identifier': after Task 9 the latter is ordinary donor metadata."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "file", "ia_identifier"], ["First photo", "photo1.jpg", ""]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "missing required column" not in out


def test_cmd_validate_does_not_scheme_check_a_donor_identifier_column_and_says_it_will_not_ship(
    tmp_path, monkeypatch, capsys
):
    """The real Sheet's own 'Identifier' column holds the donor's archival
    reference (e.g. 'CD 1 01 53 58 1 Central SS'), not a minted IA identifier,
    so it must NOT be checked against the COLLECTIONKEY-PROJECTID-NUMBER
    scheme, which it would never match.

    It also must not be advertised as a field that will upload. It never
    does - upload_row strips the `identifier` key because that name is
    Internet Archive's own item identifier - and this test previously asserted
    the receipt listed it among the uploadable fields, which pinned the lie in
    place. A receipt an operator learns to disbelieve is worse than none."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [
        ["Title", "file", "Identifier"],
        ["First photo", "photo1.jpg", "CD 1 01 53 58 1 Central SS"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1/1 rows passed" in out
    assert "does not match scheme" not in out
    assert "will upload these metadata fields:\n  title\n" in out
    assert "NOT uploaded - Internet Archive reserves these names:\n  identifier" in out


def test_cmd_validate_names_ia_identifier_not_identifier_in_a_duplicate_error(
    tmp_path, monkeypatch, capsys
):
    """A Sheet with BOTH a donor 'Identifier' column (distinct values,
    ordinary metadata) and a duplicated 'ia_identifier' (the tool's minted
    one) must say which column the duplicate is actually in - a bare
    'identifier' would be ambiguous between the two and send a volunteer to
    edit the wrong cell."""
    from ia_bulk import cmd_validate

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    grid = [
        ["Title", "file", "Identifier", "ia_identifier"],
        ["First", "a.jpg", "Donor Ref A", "lcps-astoriaphotos-00001"],
        ["Second", "b.jpg", "Donor Ref B", "lcps-astoriaphotos-00001"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "ia_identifier 'lcps-astoriaphotos-00001' duplicates row 2" in out


def test_cmd_validate_fails_a_row_whose_file_cannot_be_resolved_with_the_resolvers_message(
    tmp_path, monkeypatch, capsys
):
    """Phase 2's reversal of Task 8's Phase 1 exemption: the disk check is
    back, and a row whose file_template candidate matches nothing on disk
    must fail with the resolver's OWN message (naming the folder and the
    name it looked for), not the generic disk-check 'file not found' or
    'missing required column' that would say the same thing without
    naming what was actually searched for.

    Distinguishing resolve_file()'s own wording ("no file found in ...
    matching ...") from the generic checks matters: without it, this test
    would pass even if cmd_validate silently discarded the resolver's
    message and fell back to validate_rows' plain disk-existence check,
    which would produce a superficially similar-looking failure by
    accident (the raw, never-verified Sheet cell value happens to embed
    the same folder/name substrings) rather than by design."""
    from ia_bulk import cmd_validate

    (tmp_path / "SOP CD1").mkdir()  # folder exists, the named file does not
    grid = [["Title", "file"], ["First photo", "SOP CD1/Nothing Here"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "[FAIL] row 2" in out
    assert "no file found in" in out
    assert "SOP CD1" in out and "Nothing Here" in out
    assert "file not found:" not in out


def test_cmd_validate_fails_on_an_ambiguous_file_naming_both_candidates(tmp_path, monkeypatch, capsys):
    """Two files sharing a stem (a JPEG and a TIFF master, say) must never be
    picked between silently - an item's identifier is permanent - so the
    row fails, and the report names both candidates."""
    from ia_bulk import cmd_validate

    folder = tmp_path / "SOP CD5"
    folder.mkdir()
    (folder / "Liberty.jpg").write_bytes(b"x")
    (folder / "Liberty.tif").write_bytes(b"x")

    grid = [["Title", "file"], ["Liberty", "SOP CD5/Liberty"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "Liberty.jpg" in out and "Liberty.tif" in out


def test_cmd_validate_fails_fast_when_file_template_names_a_column_the_sheet_lacks(
    tmp_path, monkeypatch, capsys
):
    """A file_template referencing a column absent from the Sheet's actual
    header row (a registry typo, or a Sheet whose columns changed) must be
    caught once at startup via check_file_template, not surfaced as the
    same resolution failure repeated on every single row."""
    from ia_bulk import cmd_validate

    grid = [["Title"], ["First photo"]]  # no "file" column at all
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "file_template" in err
    assert "'file'" in err


def test_cmd_validate_resolves_a_filename_missing_its_extension_and_writes_identifier_bib(
    tmp_path, monkeypatch
):
    """225 of 234 real rows have no extension in the Sheet. resolve_file()
    finds the actual file on disk, and both row['file'] and
    row['ia_identifier_bib'] must end up holding the RESOLVED name (with
    its real extension) - not the Sheet's literal cell value."""
    from ia_bulk import cmd_validate

    (tmp_path / "Alderbrook Hall.jpg").write_bytes(b"x")
    captured_rows = []

    def fake_validate_rows(rows, files_dir, registry, project_id, **kwargs):
        captured_rows.extend(rows)
        return [RowValidation(row_number=i + 2, identifier="") for i in range(len(rows))]

    monkeypatch.setattr("ia_bulk.validate_rows", fake_validate_rows)
    grid = [["Title", "file"], ["Alderbrook Hall", "Alderbrook Hall"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)

    assert captured_rows[0]["file"] == "Alderbrook Hall.jpg"
    assert captured_rows[0]["ia_identifier_bib"] == "Alderbrook Hall.jpg"


def test_cmd_validate_resolves_using_a_two_column_file_template_like_the_real_registry(
    tmp_path, monkeypatch, capsys
):
    """projects_registry.json's real file_template joins two Sheet columns
    ('{file_on_array}/{identifier}'), not one - proves cmd_validate's
    wiring isn't accidentally hardcoded to a single-placeholder template."""
    from ia_bulk import cmd_validate

    folder = tmp_path / "SOP CD1"
    folder.mkdir()
    (folder / "CD 1 01 53 58 1 Central SS.jpg").write_bytes(b"x")

    grid = [
        ["Title", "File on Array", "Identifier"],
        ["Central School", "SOP CD1", "CD 1 01 53 58 1 Central SS"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            make_sheet_registry(files_dir=str(tmp_path), file_template="{file_on_array}/{identifier}")
        ),
        encoding="utf-8",
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "1/1 rows passed" in out


def test_cmd_validate_flags_colliding_sheet_headers(tmp_path, monkeypatch, capsys):
    """Proves mandatory addition #1: check_column_map is wired into the
    report. mediatype/title are satisfied and the Sheet path never checks
    file existence, so the ONLY possible source of a failure is the header
    collision itself."""
    from ia_bulk import cmd_validate

    grid = [
        ["Genre / Form", "Genre_ Form", "file", "Title"],
        ["a", "b", "photo1.jpg", "First"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "both normalize to field name 'genre_form'" in out


def test_cmd_validate_flags_a_sheet_row_longer_than_the_header(tmp_path, monkeypatch, capsys):
    """Proves mandatory addition #1: check_grid_shape is wired into the
    report - AND that the problem is filed under the row it's actually
    about (row 2, the single data row here), not under row 1. A volunteer
    reads "row 1" as the header row; a row-2 problem filed there is
    confusing even though the message text itself already names row 2."""
    from ia_bulk import cmd_validate

    grid = [
        ["Title", "file"],
        ["First", "photo1.jpg", "unexpected extra cell"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "more field(s) than the header" in out
    assert "[FAIL] row 2" in out
    assert "[FAIL] row 1" not in out


def test_cmd_validate_reports_a_header_collision_and_a_shape_error_under_their_own_rows(
    tmp_path, monkeypatch, capsys
):
    """Both a genuinely header-level defect (a collision) and a specific
    row's shape defect can be present in the same Sheet at once, and must
    be filed under their own distinct rows rather than merged into a single
    row-1 entry."""
    from ia_bulk import cmd_validate

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    grid = [
        ["Genre / Form", "Genre_ Form", "Title", "file"],
        ["a", "b", "First", "a.jpg"],
        ["c", "d", "Second", "b.jpg", "unexpected extra cell"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "[FAIL] row 1" in out
    assert "[FAIL] row 3" in out
    assert "both normalize to field name 'genre_form'" in out
    assert "more field(s) than the header" in out


def status_lines(out: str) -> list[str]:
    """The "[PASS]/[FAIL] row N" headings of a report, in printed order -
    without their indented error lines. Lets a test assert the exact set,
    order and verdict of the rows reported, which a substring check on any
    single heading cannot: a row reported twice with opposite verdicts
    satisfies both `"[FAIL] row 3" in out` and `"[PASS] row 3" in out`."""
    return [line for line in out.splitlines() if line.startswith("[")]


def test_cmd_validate_reports_a_long_row_once_as_failed_not_twice_with_opposite_verdicts(
    tmp_path, monkeypatch, capsys
):
    """A structural problem with a specific data row must be folded into
    that row's own result, not reported as a second, parallel entry beside
    it. Filing it separately made a long row print twice with contradicting
    verdicts ("[FAIL] row 3" from check_grid_shape, "[PASS] row 3" from
    validate_rows), inflated the "N/M rows passed" denominator past the
    number of data rows the Sheet actually has, and left the row counted as
    "ready to upload" in the lifecycle summary - a failing row promised as
    uploadable, printed directly beneath the report failing it.

    Row 3 is long but otherwise complete (title present, identifier blank
    which is normal, no header defect anywhere), so the shape error is the
    only possible source of a failure and the only possible source of an
    entry outside the three data rows."""
    from ia_bulk import cmd_validate

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    (tmp_path / "c.jpg").write_bytes(b"x")
    grid = [
        ["Title", "Date", "file"],
        ["First photo", "1912", "a.jpg"],
        ["Second photo", "1913", "b.jpg", "unexpected extra cell"],
        ["Third photo", "1914", "c.jpg"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    # each row exactly once, in order, under its own number - no duplicate
    # row 3 and no phantom row-1 entry standing in for a data row's problem
    assert status_lines(out) == ["[PASS] row 2", "[FAIL] row 3", "[PASS] row 4"]
    # the error is filed under row 3's heading, not merely mentioned somewhere
    assert "[FAIL] row 3\n    - row 3 has 1 more field(s) than the header" in out
    # the denominator is the real number of data rows (3), not 3 + one
    # parallel structural entry
    assert "2/3 rows passed" in out
    # and the failing row is counted as failed, not as ready to upload
    assert "2 rows ready to upload (no identifier yet)" in out
    assert "1 row not yet assigned an identifier but failed validation" in out


def test_cmd_validate_keeps_a_header_level_error_under_row_1_when_the_last_data_row_is_also_long(
    tmp_path, monkeypatch, capsys
):
    """The companion hazard to folding per-row structural errors into
    row_results: a header-level entry is row 1, and `row_results[1 - 2]` is
    `row_results[-1]` - Python's negative indexing would quietly file a
    header collision under the LAST data row and drop the row-1 heading
    entirely. Here the last data row is ALSO long, so a wrong fold lands the
    collision on a row that already fails for its own reason and would
    otherwise look plausible."""
    from ia_bulk import cmd_validate

    (tmp_path / "a.jpg").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    grid = [
        ["Genre / Form", "Genre_ Form", "Title", "file"],  # colliding headers -> a header-level error
        ["a", "b", "First photo", "a.jpg"],
        ["c", "d", "Second photo", "b.jpg", "unexpected extra cell"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert status_lines(out) == ["[FAIL] row 1", "[PASS] row 2", "[FAIL] row 3"]
    assert "[FAIL] row 1\n    - columns 'Genre / Form' and 'Genre_ Form' both normalize" in out
    assert "[FAIL] row 3\n    - row 3 has 1 more field(s) than the header" in out


@pytest.mark.parametrize("live", [True, False])
def test_cmd_validate_passes_live_flag_and_project_config_through_to_build_sheet_client(
    tmp_path, monkeypatch, live
):
    """Coordinator-flagged CRITICAL gap: only the live=True case was ever
    exercised, so cmd_validate could have been hardcoded to
    build_sheet_client(config, True) - always reading the REAL Sheet
    regardless of --live - and every one of the 167 tests at the time would
    still have passed, because every other Sheet test's stub discards
    `live` entirely. Parametrizing over both values is what pins the
    argument actually flows through, in both directions, rather than one
    direction happening to be right by coincidence."""
    from ia_bulk import cmd_validate

    captured = {}

    def fake_build_sheet_client(config, live):
        captured["live"] = live
        captured["project_id"] = config.project_id
        return FakeSheetClient([["Title", "file"], []])

    monkeypatch.setattr("ia_bulk.build_sheet_client", fake_build_sheet_client)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=live)

    cmd_validate(args)

    assert captured["live"] is live
    assert captured["project_id"] == "astoriaphotos"


def test_cmd_validate_rejects_an_unknown_project_before_touching_the_sheet(tmp_path, monkeypatch):
    """load_project_config's ConfigError must not be swallowed - an unknown
    --project has to fail loudly rather than silently reading nothing."""
    from ia_bulk import cmd_validate
    from project_config import ConfigError

    calls = []
    monkeypatch.setattr(
        "ia_bulk.build_sheet_client", lambda config, live: calls.append(config) or FakeSheetClient([])
    )

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    args = Namespace(project="nosuchproject", registry=str(registry_path), live=False)

    with pytest.raises(ConfigError, match="nosuchproject"):
        cmd_validate(args)

    assert calls == []


def test_cmd_validate_rejects_an_unreplaced_placeholder_sheet_id_before_touching_the_network(
    tmp_path, monkeypatch, capsys
):
    """projects_registry.json ships sheet_id/test_sheet_id as
    REPLACE_WITH_* until a human edits in the real Google Sheet ID. Asking
    Google about a placeholder produces an opaque 404/permission error that
    doesn't say what to fix; catching it before the network call and naming
    the registry file directly is what makes the first-run failure
    actionable."""
    from ia_bulk import cmd_validate

    def _must_not_reach_the_network(config, live):
        raise AssertionError("build_sheet_client must not be called for an unreplaced placeholder")

    monkeypatch.setattr("ia_bulk.build_sheet_client", _must_not_reach_the_network)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(test_sheet_id="REPLACE_WITH_TEST_SHEET_ID")), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "REPLACE_WITH_TEST_SHEET_ID" in err
    assert str(registry_path) in err


def test_cmd_validate_turns_an_http_error_reading_the_sheet_into_an_actionable_message(
    tmp_path, monkeypatch, capsys
):
    """A wrong tab name (or an unshared/deleted Sheet) surfaces from the API
    as googleapiclient.errors.HttpError, e.g. "Unable to parse range:
    Sheet1" - which by itself doesn't tell anyone to go edit 'sheet_tab' in
    the registry. This must be caught and turned into a message naming the
    spreadsheet ID, the tab, and the registry file, not left as a raw
    traceback."""
    from ia_bulk import cmd_validate

    monkeypatch.setattr(
        "ia_bulk.build_sheet_client",
        lambda config, live: RaisingSheetClient(make_http_error("Unable to parse range: Sheet1")),
    )

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(sheet_tab="Sheet1")), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "TEST_SHEET_ID" in err
    assert "Sheet1" in err
    assert str(registry_path) in err
    assert "sheet_tab" in err


def test_cmd_validate_turns_unavailable_google_credentials_into_a_clean_error(
    tmp_path, monkeypatch, capsys
):
    """A missing or rejected key ends the run with a message, not a traceback."""
    from ia_bulk import cmd_validate

    def _no_key(key_path):
        raise google_auth.AuthUnavailable(f"missing service account key at {key_path}.")

    monkeypatch.setattr("ia_bulk.google_auth.load_service_account_credentials", _no_key)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "missing service account key" in err


def test_sheet_read_error_names_the_service_account_to_share_with(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_validate

    key_path = tmp_path / "key.json"
    key_path.write_text(
        json.dumps({"client_email": "sheets-sync@example.iam.gserviceaccount.com"}), encoding="utf-8"
    )
    monkeypatch.setattr("ia_bulk.google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH", key_path)
    monkeypatch.setattr(
        "ia_bulk.build_sheet_client",
        lambda config, live: RaisingSheetClient(
            make_http_error("The caller does not have permission", status=403)
        ),
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "sheets-sync@example.iam.gserviceaccount.com" in err
    assert "as Editor" in err


def test_sheet_read_error_names_the_key_path_when_the_key_cannot_be_read(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_validate

    key_path = tmp_path / "missing-key.json"
    monkeypatch.setattr("ia_bulk.google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH", key_path)
    monkeypatch.setattr(
        "ia_bulk.build_sheet_client",
        lambda config, live: RaisingSheetClient(
            make_http_error("The caller does not have permission", status=403)
        ),
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)
    err = capsys.readouterr().err

    assert str(key_path) in err


def test_cmd_validate_flags_a_header_only_sheet_as_an_error_not_a_false_green(
    tmp_path, monkeypatch, capsys
):
    """An empty read is far more likely to mean a wrong tab, an unpopulated
    copy of the Sheet, or a Sheet never actually shared with the service
    account than a real project with zero rows - reporting "0/0 rows
    passed" and exiting 0 would be a false green from a command whose
    entire job is catching exactly this kind of problem. And a "0/1 rows
    passed" line must never appear here either - the "1" would be a
    synthetic entry standing in for zero real rows, which reads as
    nonsense arithmetic."""
    from ia_bulk import cmd_validate

    grid = [["Title", "file"]]  # header row only, zero data rows
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "no data rows" in out
    assert "rows passed" not in out


def test_cmd_validate_flags_a_completely_empty_sheet_as_an_error(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_validate

    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient([]))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "no data rows" in out
    assert "rows passed" not in out


def test_cmd_validate_still_reports_a_header_collision_when_the_sheet_has_no_data_rows(
    tmp_path, monkeypatch, capsys
):
    """The no-data-rows short-circuit must not swallow a genuine header
    problem - a colliding header is worth surfacing even on an otherwise
    empty Sheet, since fixing it is a prerequisite to populating the Sheet
    correctly in the first place."""
    from ia_bulk import cmd_validate

    grid = [["Genre / Form", "Genre_ Form"]]  # colliding header, zero data rows
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "both normalize to field name 'genre_form'" in out
    assert "no data rows" in out
    assert "rows passed" not in out


def test_cmd_validate_prints_test_mode_and_the_test_sheet_id_by_default(tmp_path, monkeypatch, capsys):
    """Run mode is this project's core safety design, so it - and exactly
    which spreadsheet/tab back it - must be visible in the output, not just
    implied by which flags were passed on the command line."""
    from ia_bulk import cmd_validate

    grid = [["Title"], ["First"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"), sheet_tab="Donor Photos")
        ),
        encoding="utf-8",
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)
    out = capsys.readouterr().out

    assert "test mode" in out
    assert "TEST_SHEET_ID" in out
    assert "REAL_SHEET_ID" not in out
    assert "Donor Photos" in out


def test_cmd_validate_prints_live_mode_and_the_real_sheet_id_when_live(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_validate

    grid = [["Title"], ["First"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            make_sheet_registry(files_dir=str(tmp_path / "no-photos-here"), sheet_tab="Donor Photos")
        ),
        encoding="utf-8",
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=True)

    cmd_validate(args)
    out = capsys.readouterr().out

    assert "live mode" in out
    assert "REAL_SHEET_ID" in out
    assert "TEST_SHEET_ID" not in out


def test_cmd_validate_injects_mediatype_from_the_registry_not_a_hardcoded_value(tmp_path, monkeypatch):
    """Every existing fixture happens to use mediatype="image", so a
    hardcoded row["mediatype"] = "image" would pass all of them - proving
    only "some non-empty value", never "from ProjectConfig.mediatype".
    mediatype is permanent on IA once uploaded and is never printed
    anywhere, so nothing else in this suite would catch a wrong source -
    a distinctive fixture value plus capturing the actual row dict passed
    to validate_rows is the only way to pin it."""
    from ia_bulk import cmd_validate

    captured_rows = []

    def fake_validate_rows(rows, files_dir, registry, project_id, **kwargs):
        captured_rows.extend(rows)
        return [RowValidation(row_number=i + 2, identifier="") for i in range(len(rows))]

    monkeypatch.setattr("ia_bulk.validate_rows", fake_validate_rows)
    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "file"], ["First photo", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path), mediatype="phonorecord")),
        encoding="utf-8",
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)

    assert captured_rows == [
        {
            "title": "First photo",
            "file": "photo1.jpg",
            "mediatype": "phonorecord",
            "ia_identifier_bib": "photo1.jpg",
        }
    ]


def test_cmd_validate_passes_only_the_row_results_to_the_lifecycle_summary_not_the_combined_report(
    tmp_path, monkeypatch
):
    """Coordinator-caught gap (3a): passing the combined `results` list
    (which also carries sheet_structure_validation()'s row-1/shape
    entries) instead of validate_rows()'s own row_results would misalign
    zip(rows, row_results) - silently, since zip() truncates rather than
    raising. A Sheet with BOTH a structural error (a header collision) and
    a normal data row is what makes the combined list a different LENGTH
    than `rows`, so this is pinned by inspecting exactly what cmd_validate
    hands to format_lifecycle_summary, not by relying on the length guard
    added to format_lifecycle_summary itself to happen to fire."""
    from ia_bulk import cmd_validate

    captured = {}

    def fake_format_lifecycle_summary(rows, row_results):
        captured["rows"] = rows
        captured["row_results"] = row_results
        return "captured"

    monkeypatch.setattr("ia_bulk.format_lifecycle_summary", fake_format_lifecycle_summary)

    (tmp_path / "a.jpg").write_bytes(b"x")
    grid = [
        ["Genre / Form", "Genre_ Form", "Title", "file"],  # colliding headers -> a structural error
        ["a", "b", "First photo", "a.jpg"],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)

    assert len(captured["rows"]) == 1
    assert len(captured["row_results"]) == 1


def test_cmd_validate_prints_correct_lifecycle_counts_for_a_mix_of_states_end_to_end(
    tmp_path, monkeypatch, capsys
):
    """End-to-end version of 3a/3b: a real Sheet with one row in each of
    the six (state x valid/invalid) combinations, run through the actual
    cmd_validate/validate_rows/format_lifecycle_summary wiring - not just
    format_lifecycle_summary in isolation, which was already correct on its
    own and is exactly why the coordinator's mutations (fabricated
    all-valid results; the combined report instead of row_results) slipped
    past every previous test: the only existing end-to-end summary
    assertion was on an all-passing run, where every mutation happens to
    look identical to correct behavior."""
    from ia_bulk import cmd_validate

    for name in ("ready.jpg", "done.jpg", "reserved.jpg"):
        (tmp_path / name).write_bytes(b"x")

    # Uses "ia_identifier", not "identifier": after Task 9 the latter is
    # ordinary donor metadata and no longer drives classify_row() at all.
    #
    # The "fails validation" row in each pair names a FILE that resolves to
    # nothing (missing*.jpg is deliberately never created above), not a
    # blank title - since the SHEET_REQUIRED_COLUMNS shrink, a blank title
    # is a readiness fact rather than a validation error, so it can no
    # longer stand in for "this row is invalid" here.
    grid = [
        ["Title", "file", "ia_identifier", "ia_uploaded"],
        ["Ready", "ready.jpg", "", ""],
        ["Ready but broken", "missing.jpg", "", ""],  # unresolvable file -> fails, still unassigned
        ["Done", "done.jpg", "lcps-astoriaphotos-00001", "2026-01-01T00:00:00"],
        ["Done but broken", "missing2.jpg", "lcps-astoriaphotos-00002", "2026-01-01T00:00:00"],  # unresolvable file -> fails
        ["Reserved", "reserved.jpg", "lcps-astoriaphotos-00003", ""],
        ["Reserved but broken", "missing3.jpg", "lcps-astoriaphotos-00004", ""],  # unresolvable file -> fails
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    exit_code = cmd_validate(args)
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "3/6 rows passed" in out
    assert "1 row ready to upload (no identifier yet)" in out
    assert "1 row not yet assigned an identifier but failed validation" in out
    assert "1 already uploaded" in out
    assert "1 row already uploaded but now fail validation" in out
    assert "1 reserved but unconfirmed - will retry under existing identifier" in out
    assert "1 row reserved but invalid" in out


def test_cmd_validate_output_encodes_cleanly_under_a_restrictive_windows_console_codepage(
    tmp_path, monkeypatch, capsys
):
    """Finding 5 (fix round 2) replaced an em dash in ia_fields.py with an
    ASCII hyphen, but nothing pinned it - restoring the em dash would pass
    every other test in this suite. cp437 is the default codepage on many
    non-UTF-8 Windows consoles; encoding the full validate output as plain
    ascii (a stricter test - anything ascii-safe is cp437-safe too) is what
    would have caught it, since a character that can't encode there raises
    UnicodeEncodeError and truncates the human's report mid-run on exactly
    the machine this task hands off to."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "Photographer", "file"], ["First photo", "Jane Doe", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)
    out = capsys.readouterr().out

    # The row must actually reach the suggestions section (not short-circuit
    # on a template/resolution error) for this to be a meaningful check of
    # the FULL report's encoding, not just the banner line's.
    assert "suggestions (advisory - nothing is changed automatically):" in out
    out.encode("ascii")  # raises UnicodeEncodeError if any non-ASCII character slipped in


def test_cmd_validate_prints_an_actual_suggestion_not_just_the_heading(tmp_path, monkeypatch, capsys):
    """Only the "suggestions (advisory ...)" heading was previously
    asserted anywhere - deleting the loop that prints each suggestion,
    keeping only the heading, passed every test. This is one of the three
    things the Phase 1 handoff session exists to exercise."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    grid = [["Title", "Photographer", "file"], ["First photo", "Jane Doe", "photo1.jpg"]]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    args = Namespace(project="astoriaphotos", registry=str(registry_path), live=False)

    cmd_validate(args)
    out = capsys.readouterr().out

    assert "photographer" in out
    assert "creator" in out


def test_chunk_rows_splits_into_groups_of_chunk_size():
    from ia_bulk import chunk_rows

    rows = [{"n": i} for i in range(1250)]

    chunks = list(chunk_rows(rows, chunk_size=500))

    assert [len(c) for c in chunks] == [500, 500, 250]
    assert chunks[0][0] == {"n": 0}
    assert chunks[2][-1] == {"n": 1249}


def test_chunk_rows_handles_empty_list():
    from ia_bulk import chunk_rows

    assert list(chunk_rows([], chunk_size=500)) == []


def test_open_log_creates_log_dir_and_returns_timestamped_path(tmp_path):
    from ia_bulk import open_log

    log_dir = tmp_path / "logs"

    log_path = open_log(log_dir, "upload")

    assert log_dir.is_dir()
    assert log_path.parent == log_dir
    assert log_path.name.startswith("upload-")
    assert log_path.suffix == ".jsonl"


def test_log_result_appends_one_json_line(tmp_path):
    from ia_bulk import log_result

    log_path = tmp_path / "upload-test.jsonl"

    log_result(log_path, "lcps-astoriaphotos-00001", "photo1.jpg", "success", live=False)
    log_result(log_path, "lcps-astoriaphotos-00002", "photo2.jpg", "failure", live=False, error="timeout")

    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["identifier"] == "lcps-astoriaphotos-00001"
    assert first["status"] == "success"
    assert first["error"] is None
    second = json.loads(lines[1])
    assert second["status"] == "failure"
    assert second["error"] == "timeout"


def test_recorded_timestamps_are_utc_with_an_explicit_offset(tmp_path, monkeypatch):
    """`ia_uploaded` is the permanent record of when an item was published
    and the log is what any later audit reads. Both were
    naive local time, which repeats an hour during the DST fall-back
    transition - so a run spanning it stamped a later chunk with an earlier
    wall-clock time, unrecoverably. run_stamp() already used UTC for exactly
    this reason; these did not."""
    import time as time_module

    from ia_bulk import log_result, upload_timestamp, utc_timestamp

    # A timezone whose local time differs from UTC, so "did it use gmtime"
    # is answerable rather than coincidental.
    monkeypatch.setattr(time_module, "localtime", time_module.gmtime)
    fixed = time_module.gmtime(0)
    monkeypatch.setattr(time_module, "gmtime", lambda *a: fixed)

    assert utc_timestamp() == "1970-01-01T00:00:00Z"
    assert upload_timestamp() == "1970-01-01T00:00:00Z"

    log_path = tmp_path / "upload-test.jsonl"
    log_result(log_path, "lcps-astoriaphotos-00001", "a.jpg", "success", live=True)
    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry["timestamp"] == "1970-01-01T00:00:00Z"


def test_open_log_names_the_file_in_utc(tmp_path, monkeypatch):
    """So a directory listing sorts in the order the runs actually happened."""
    import time as time_module

    from ia_bulk import open_log

    epoch = time_module.gmtime(0)
    monkeypatch.setattr(time_module, "gmtime", lambda *a: epoch)

    assert open_log(tmp_path, "upload").name == "upload-19700101T000000Z.jsonl"


def test_run_header_is_the_first_line_of_the_log(tmp_path):
    """log_run_header() is called once, before any log_result() call, and
    must be the log's first line so `head -1 <log>` always answers "what did
    this run send, under what field names, and what did it require" -
    verbatim from the Task 11 brief, adapted to the current ProjectConfig
    shape (_sheet_config(), since the brief's REGISTRY_FIXTURE/
    load_project_config(..., "sarahsoldphotos") no longer exist) and
    extended with required_for_upload, which did not exist when the brief
    was written but is now part of what determined this run's scope."""
    from ia_bulk import log_run_header

    log_path = tmp_path / "upload.jsonl"
    column_map = build_column_map(["Title", "Notes (LCPS Internal)"])
    config = _sheet_config(required_for_upload=("title",))

    log_run_header(log_path, config, column_map, live=False, dry_run=False)
    log_result(log_path, "lcps-astoriaphotos-00001", "a.jpg", "success", False)

    first, second = log_path.read_text(encoding="utf-8").splitlines()
    header = json.loads(first)
    assert header["record"] == "run_header"
    assert header["project"] == "astoriaphotos"
    assert header["live"] is False
    assert header["dry_run"] is False
    assert header["sheet_id"] == "TEST_SHEET_ID"
    # The collection this run TARGETED - a test run's items go to
    # test_collection, not to the registry's own ia_collection.
    assert header["collection"] == "test_collection"
    assert header["files_dir"] == "."
    assert header["file_template"] == "{file}"
    assert header["columns"] == {
        "Title": "title",
        "Notes (LCPS Internal)": "notes_lcps_internal",
    }
    assert header["held_back"] == ["Notes (LCPS Internal)"]
    assert header["required_for_upload"] == ["title"]
    # Task 12: a run that stopped at --limit, or used a non-default
    # --chunk-size, is not reconstructable without both numbers recorded
    # alongside everything else the header already captures. Neither flag
    # was passed here, so both read as "the run's own default".
    assert header["limit"] is None
    assert header["chunk_size"] == CHUNK_SIZE
    assert json.loads(second)["status"] == "success"


def test_run_header_records_live_mode_and_the_real_sheet_id(tmp_path):
    """A separate case from the test-mode one above rather than a second
    assertion bolted onto it: sheet_id_for() branches on `live`, and a run
    header that silently logged the test Sheet ID during a --live run (or
    vice versa) would be the exact kind of defect this record exists to make
    visible months later."""
    from ia_bulk import log_run_header

    log_path = tmp_path / "upload.jsonl"
    column_map = build_column_map(["Title"])
    config = _sheet_config(required_for_upload=("title",))

    log_run_header(log_path, config, column_map, live=True, dry_run=False)

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["live"] is True
    assert header["sheet_id"] == "REAL_SHEET_ID"


def test_effective_identifier_prepends_zztest_and_the_given_stamp_when_not_live():
    """See docs/DECISIONS.md, "Test identifiers carry a per-run stamp" - a
    bare zztest- prefix made a test run's identifiers a pure function of the
    real ones, so a fresh Sheet (always minting from 00001) reproduced the
    exact identifiers of a prior run and collided with darkened items."""
    assert (
        effective_identifier("lcps-astoriaphotos-00001", live=False, stamp="20260819t144907")
        == "zztest-20260819t144907-lcps-astoriaphotos-00001"
    )


def test_effective_identifier_ignores_the_stamp_and_returns_identifier_unchanged_when_live():
    """The single most important property this task can produce: a --live
    identifier is the permanent public address of an archival item and must
    stay a pure function of the Sheet. A stamp leaking into it would be
    worse than not doing this task at all - so this is asserted with the
    stamp parameter actually supplied (not omitted), proving the live branch
    receives it and still ignores it, rather than merely never being asked to
    handle it."""
    assert (
        effective_identifier("lcps-astoriaphotos-00001", live=True, stamp="20260819t144907")
        == "lcps-astoriaphotos-00001"
    )


def test_effective_identifier_applies_one_given_stamp_identically_across_different_identifiers():
    """A run mints many identifiers but must compute run_stamp() only once -
    this pins effective_identifier's half of that contract: handed the SAME
    stamp twice, as a real run would, it embeds that exact stamp in both
    results rather than anything call-order-dependent."""
    stamp = "20260819t144907"
    first = effective_identifier("lcps-astoriaphotos-00001", live=False, stamp=stamp)
    second = effective_identifier("lcps-astoriaphotos-00002", live=False, stamp=stamp)
    assert first == "zztest-20260819t144907-lcps-astoriaphotos-00001"
    assert second == "zztest-20260819t144907-lcps-astoriaphotos-00002"


def test_run_stamp_is_lowercase_and_ia_identifier_safe():
    """IA identifiers are restricted to lowercase letters, digits, underscore,
    period and hyphen. run_stamp() is spliced directly between two literal
    hyphens in effective_identifier(), so anything outside [a-z0-9] in the
    stamp itself (uppercase, a colon from a naive isoformat(), whitespace)
    would produce a malformed identifier."""
    assert re.fullmatch(r"[a-z0-9]+", run_stamp())


def test_effective_identifier_requires_stamp_to_be_passed_explicitly():
    """`stamp` is a required parameter, not a defaulted one - see
    docs/DECISIONS.md, "Test identifiers carry a per-run stamp": a default
    would leave every call site free to silently fall back to it, which
    reopens exactly the collision this task exists to close. Calling with
    only `identifier` and `live` must raise TypeError rather than quietly
    succeeding."""
    with pytest.raises(TypeError):
        effective_identifier("lcps-astoriaphotos-00001", live=False)  # type: ignore[call-arg]


def test_upload_row_succeeds_when_library_returns_ok_responses(tmp_path, monkeypatch):
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "1958",
    }
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured["identifier"] = identifier
        captured["files"] = files
        captured["metadata"] = metadata
        captured["kwargs"] = kwargs
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)

    assert captured["identifier"] == "zztest-lcps-astoriaphotos-00001"
    assert captured["files"] == [str(tmp_path / "photo1.jpg")]
    assert captured["metadata"]["mediatype"] == "image"
    assert captured["metadata"]["collection"] == "test_collection"
    assert "identifier" not in captured["metadata"]
    assert captured["kwargs"]["verbose"] is True
    assert captured["kwargs"]["checksum"] is True


def test_upload_row_raises_when_library_returns_failed_response(tmp_path, monkeypatch):
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "1958",
    }

    def fake_upload(identifier, files, metadata, **kwargs):
        return [FakeResponse(ok=False, status_code=503, text="Service Unavailable")]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    with pytest.raises(RuntimeError, match="503"):
        upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)


def test_upload_row_defaults_blank_date_to_undated_placeholder(tmp_path, monkeypatch):
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "",
    }
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured["metadata"] = metadata
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)

    assert captured["metadata"]["date"] == "[n.d.]"


def test_upload_row_defaults_missing_date_key_to_undated_placeholder(tmp_path, monkeypatch):
    """A row dict with no 'date' key at all still uploads as undated."""
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": None,
    }
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured["metadata"] = metadata
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)

    assert captured["metadata"]["date"] == "[n.d.]"


def test_upload_row_preserves_free_form_date_when_present(tmp_path, monkeypatch):
    from ia_bulk import upload_row

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {
        "identifier": "lcps-astoriaphotos-00001",
        "file": "photo1.jpg",
        "mediatype": "image",
        "title": "First photo",
        "date": "circa 1930",
    }
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured["metadata"] = metadata
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(row, target_identifier="zztest-lcps-astoriaphotos-00001", collection="test_collection", files_dir=tmp_path)

    assert captured["metadata"]["date"] == "circa 1930"


def test_update_metadata_row_succeeds_when_library_returns_ok_response(monkeypatch):
    from ia_bulk import update_metadata_row

    row = {"identifier": "lcps-astoriaphotos-00001", "title": "Updated title"}
    captured = {}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        captured["identifier"] = identifier
        captured["metadata"] = metadata
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")

    assert captured["identifier"] == "zztest-lcps-astoriaphotos-00001"
    assert captured["metadata"] == {"title": "Updated title"}


def test_update_metadata_row_drops_blank_cells_instead_of_clearing_the_field(monkeypatch):
    """A blank cell must mean 'leave this field alone', not 'clear it'."""
    from ia_bulk import update_metadata_row

    row = {"identifier": "lcps-astoriaphotos-00001", "title": "Updated title", "description": ""}
    captured = {}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        captured["metadata"] = metadata
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")

    assert "description" not in captured["metadata"]


def test_update_metadata_row_passes_remove_tag_through_to_clear_a_field(monkeypatch):
    """REMOVE_TAG is the internetarchive library's (and the official `ia`
    CLI's) sentinel value for deleting an existing metadata field - it must
    not be filtered out the way a blank cell is."""
    from ia_bulk import update_metadata_row

    row = {"identifier": "lcps-astoriaphotos-00001", "description": "REMOVE_TAG"}
    captured = {}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        captured["metadata"] = metadata
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")

    assert captured["metadata"]["description"] == "REMOVE_TAG"


def test_update_metadata_row_raises_when_library_returns_failed_response(monkeypatch):
    from ia_bulk import update_metadata_row

    row = {"identifier": "lcps-astoriaphotos-00001", "title": "Updated title"}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        return FakeResponse(ok=False, status_code=400, text="Bad Request")

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    with pytest.raises(RuntimeError, match="400"):
        update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")


def test_update_metadata_row_raises_metadata_unchanged_when_ia_reports_no_changes(monkeypatch):
    from ia_bulk import update_metadata_row, MetadataUnchanged

    row = {"identifier": "lcps-astoriaphotos-00001", "title": "Same title"}

    def fake_modify_metadata(identifier, metadata, **kwargs):
        return FakeResponse(
            ok=False,
            status_code=400,
            text=json.dumps({"success": False, "error": "no changes to _meta.xml"}),
        )

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify_metadata)

    with pytest.raises(MetadataUnchanged):
        update_metadata_row(row, target_identifier="zztest-lcps-astoriaphotos-00001")


def test_build_parser_validate_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["validate", "--project", "astoriaphotos"])
    assert args.command == "validate"
    assert args.project == "astoriaphotos"
    assert args.registry == "projects_registry.json"
    assert args.live is False


@pytest.mark.parametrize("subcommand", ["validate", "upload", "sync-metadata"])
def test_build_parser_requires_project_on_every_subcommand(subcommand):
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([subcommand])


def test_build_parser_upload_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["upload", "--project", "astoriaphotos"])
    assert args.command == "upload"
    assert args.project == "astoriaphotos"
    assert args.live is False
    assert args.write_identifier is False
    assert args.dry_run is False


def test_build_parser_upload_subcommand_accepts_write_identifier_and_dry_run():
    parser = build_parser()
    args = parser.parse_args(
        ["upload", "--project", "astoriaphotos", "--write-identifier", "--dry-run"]
    )
    assert args.write_identifier is True
    assert args.dry_run is True


def test_build_parser_sync_metadata_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["sync-metadata", "--project", "astoriaphotos"])
    assert args.command == "sync-metadata"
    assert args.project == "astoriaphotos"
    assert args.live is False
    assert args.dry_run is False


def test_main_dispatches_to_cmd_validate(monkeypatch):
    calls = []
    monkeypatch.setattr("ia_bulk.cmd_validate", lambda args: calls.append(args.project) or 0)

    exit_code = main(["validate", "--project", "astoriaphotos"])

    assert exit_code == 0
    assert calls == ["astoriaphotos"]


@pytest.mark.parametrize(
    "subcommand,removed_flag",
    [
        ("upload", ["--csv", "items.csv"]),
        ("upload", ["--collection", "sarasoldphotos"]),
        ("upload", ["--files-dir", "data"]),
        ("upload", ["--resume-from", "logs/upload-x.jsonl"]),
        ("sync-metadata", ["--csv", "updates.csv"]),
        ("sync-metadata", ["--resume-from", "logs/sync-metadata-x.jsonl"]),
        ("sync-metadata", ["--from-log", "logs/upload-x.jsonl"]),
        ("validate", ["--csv", "items.csv"]),
        ("validate", ["--files-dir", "data"]),
    ],
)
def test_main_rejects_the_retired_csv_path_flags(monkeypatch, subcommand, removed_flag):
    sent = []
    monkeypatch.setattr("ia_bulk.upload_row", lambda *args, **kwargs: sent.append(args))
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda *args, **kwargs: sent.append(args))
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda *args, **kwargs: sent.append(args))

    with pytest.raises(SystemExit) as excinfo:
        main([subcommand, "--project", "astoriaphotos", *removed_flag])

    assert excinfo.value.code == 2
    assert sent == []


def test_build_parser_accepts_doctor():
    args = ia_bulk.build_parser().parse_args(["doctor", "--project", "demo"])
    assert args.command == "doctor"


def test_doctor_defaults_to_test_mode_like_every_other_command():
    args = ia_bulk.build_parser().parse_args(["doctor", "--project", "demo"])
    assert args.live is False


def test_main_dispatches_to_cmd_doctor(monkeypatch):
    called = []
    monkeypatch.setattr(ia_bulk, "cmd_doctor", lambda args: called.append(args.command) or 0)
    assert ia_bulk.main(["doctor", "--project", "demo"]) == 0
    assert called == ["doctor"]


def test_doctor_subparser_exposes_no_mutating_flags():
    """Documents the parser surface only - does not by itself prove `doctor`
    never mutates. See test_cmd_doctor_never_calls_a_failing_checks_fix for
    the behavioral guarantee."""
    parser = ia_bulk.build_parser()
    doctor = next(
        action.choices["doctor"]
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    flags = {action.dest for action in doctor._actions}
    assert "enable_agent" not in flags
    assert "fix" not in flags


def test_cmd_doctor_never_calls_a_failing_checks_fix(monkeypatch):
    """The property that actually matters: cmd_doctor must call run_checks,
    never converge, so a FAIL never triggers that check's fix(). A FAIL with
    a fix is the only shape where the two implementations differ."""
    fix_calls = []

    def fix() -> str:
        fix_calls.append(True)
        return "fixed"

    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="invented",
                probe=lambda: deployment.CheckOutcome(deployment.Status.FAIL, "nope"),
                remedy="do the thing",
                fix=fix,
            )
        ],
    )
    args = ia_bulk.build_parser().parse_args(["doctor", "--project", "demo"])
    exit_code = ia_bulk.cmd_doctor(args)

    assert fix_calls == []
    assert exit_code == 1


def test_cmd_doctor_returns_one_when_a_check_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="invented",
                probe=lambda: deployment.CheckOutcome(deployment.Status.FAIL, "nope"),
                remedy="do the thing",
            )
        ],
    )
    args = ia_bulk.build_parser().parse_args(["doctor", "--project", "demo"])
    assert ia_bulk.cmd_doctor(args) == 1
    assert "do the thing" in capsys.readouterr().out


def test_cmd_doctor_returns_zero_when_everything_passes(monkeypatch):
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="invented",
                probe=lambda: deployment.CheckOutcome(deployment.Status.PASS, "fine"),
                remedy="none",
            )
        ],
    )
    args = ia_bulk.build_parser().parse_args(["doctor", "--project", "demo"])
    assert ia_bulk.cmd_doctor(args) == 0


def test_build_parser_accepts_setup_with_enable_agent():
    args = ia_bulk.build_parser().parse_args(["setup", "--project", "demo", "--enable-agent"])
    assert args.command == "setup"
    assert args.enable_agent is True


def test_setup_does_not_enable_the_agent_by_default():
    args = ia_bulk.build_parser().parse_args(["setup", "--project", "demo"])
    assert args.enable_agent is False


def test_main_dispatches_to_cmd_setup(monkeypatch):
    called = []
    monkeypatch.setattr(ia_bulk, "cmd_setup", lambda args: called.append(args.command) or 0)
    assert ia_bulk.main(["setup", "--project", "demo"]) == 0
    assert called == ["setup"]


def test_cmd_setup_without_enable_agent_never_calls_launchctl(monkeypatch):
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: pytest.fail("setup loaded the agent without --enable-agent"),
    )
    args = ia_bulk.build_parser().parse_args(["setup", "--project", "demo"])
    assert ia_bulk.cmd_setup(args) == 0


def test_cmd_setup_with_enable_agent_bootstraps_it(monkeypatch):
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)
    loaded = []
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: (loaded.append(path), (True, "loaded"))[1],
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)
    args = ia_bulk.build_parser().parse_args(
        ["setup", "--project", "sarasoldphotos", "--live", "--enable-agent"]
    )
    ia_bulk.cmd_setup(args)
    assert len(loaded) == 1


def test_cmd_setup_reports_when_it_changed_nothing(monkeypatch, capsys):
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="already fine",
                probe=lambda: deployment.CheckOutcome(deployment.Status.PASS, "fine"),
                remedy="none",
            )
        ],
    )
    args = ia_bulk.build_parser().parse_args(["setup", "--project", "demo"])
    assert ia_bulk.cmd_setup(args) == 0
    assert "nothing to change" in capsys.readouterr().out


def test_cmd_setup_converges_and_reports_what_it_fixed(monkeypatch, capsys):
    """Companion to the quiet-path test above: a check that starts FAIL and has
    a fix() that clears it. This is the only case that exercises cmd_setup's
    own `changes` bookkeeping - the quiet-path test's single PASSing check
    never calls announce() at all, so it can't tell a working closure from a
    broken one."""
    fixed_calls = []

    def fix() -> str:
        fixed_calls.append(True)
        return "fixed it"

    attempts = []

    def probe() -> deployment.CheckOutcome:
        attempts.append(True)
        if len(attempts) == 1:
            return deployment.CheckOutcome(deployment.Status.FAIL, "not yet")
        return deployment.CheckOutcome(deployment.Status.PASS, "fine now")

    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(name="needs a fix", probe=probe, remedy="do the thing", fix=fix)
        ],
    )
    args = ia_bulk.build_parser().parse_args(["setup", "--project", "demo"])
    assert ia_bulk.cmd_setup(args) == 0
    out = capsys.readouterr().out
    assert fixed_calls == [True]
    assert "nothing to change" not in out
    assert "fixing" in out
    assert "fixed it" in out


def test_cmd_setup_with_enable_agent_announces_before_it_bootstraps(monkeypatch, capsys):
    """Both risky actions on a shared machine - permission changes and loading
    the agent - must be announced before they happen, never only after. The
    fake bootstrap asserts at call time (not on final stdout order), so it
    catches a version that hoists launchctl_bootstrap(plist) into a variable
    assigned before the two announce() calls: the real side effect would fire
    before anything is printed, even though the final stdout text order would
    look identical."""
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)

    def fake_bootstrap(path):
        already_printed = capsys.readouterr().out
        assert "loading" in already_printed
        assert "starts a live sync run now, and again at every login" in already_printed
        return True, "loaded it"

    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_bootstrap", fake_bootstrap)
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)
    monkeypatch.setattr(ia_bulk.platform_probe, "current_user", lambda: "sarasoldphotos")
    args = ia_bulk.build_parser().parse_args(
        ["setup", "--project", "sarasoldphotos", "--live", "--enable-agent"]
    )
    ia_bulk.cmd_setup(args)


# ---------------------------------------------------------------------------
# --enable-agent: verify first, then enable. Every test below exists because
# the alternative is an unattended live sync nobody asked for, against a Sheet
# nothing checked - and an Internet Archive identifier cannot be renamed.
# ---------------------------------------------------------------------------


def _setup_args(*extra):
    return ia_bulk.build_parser().parse_args(
        ["setup", "--project", "sarasoldphotos", *extra]
    )


# What _setup_args' operator pastes back into zsh; `<project>` there is a redirect.
RUNNABLE_ENABLE_COMMAND = "./install.sh --project sarasoldphotos --live --enable-agent"


def _stub_plist_write(monkeypatch, written=None):
    """load_sync_agent writes the plist; never into the real ~/Library/LaunchAgents."""

    def fake_write(spec, home):
        if written is not None:
            written.append(spec.label)
        return f"wrote {spec.label}.plist"

    monkeypatch.setattr(ia_bulk.launch_agent, "write_plist", fake_write)


def _explode_on_launchctl(monkeypatch):
    """Any launchctl call, or any plist write, is a failure for the refusal tests.
    The write comes first in load_sync_agent and would land in the real home."""
    for name in ("launchctl_bootstrap", "launchctl_bootout", "launchctl_print"):
        monkeypatch.setattr(
            ia_bulk.platform_probe,
            name,
            lambda *a, _name=name: pytest.fail(f"setup called {_name}"),
        )
    monkeypatch.setattr(
        ia_bulk.launch_agent,
        "write_plist",
        lambda *a: pytest.fail("setup wrote the plist"),
    )


def test_cmd_setup_refuses_enable_agent_without_live(monkeypatch, capsys):
    """The agent's ProgramArguments end in --live, but the checks gating it run
    in whatever mode setup was given. Implying --live here is the silent
    widening that makes a live write surprising, so it refuses instead."""
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: pytest.fail("setup ran checks before refusing"),
    )
    _explode_on_launchctl(monkeypatch)

    assert ia_bulk.cmd_setup(_setup_args("--enable-agent")) == 1
    err = capsys.readouterr().err
    assert "--live" in err
    assert RUNNABLE_ENABLE_COMMAND in err


def test_cmd_setup_refuses_enable_agent_with_offline(monkeypatch, capsys):
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: pytest.fail("setup ran checks before refusing"),
    )
    _explode_on_launchctl(monkeypatch)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--offline", "--enable-agent")) == 1
    err = capsys.readouterr().err
    assert "--offline" in err
    assert RUNNABLE_ENABLE_COMMAND in err


def test_cmd_setup_still_allows_offline_without_enable_agent(monkeypatch):
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    assert ia_bulk.cmd_setup(_setup_args("--offline")) == 0


def test_cmd_setup_does_not_enable_the_agent_when_a_check_failed(monkeypatch, capsys):
    """CRITICAL: a machine with a FAILing key or FAILing sync columns used to
    bootstrap a plist with RunAtLoad and --live anyway."""
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="service account key",
                probe=lambda: deployment.CheckOutcome(deployment.Status.FAIL, "no key"),
                remedy="download the key",
            )
        ],
    )
    _explode_on_launchctl(monkeypatch)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 1
    captured = capsys.readouterr()
    assert "NOT enabled" in captured.err
    assert RUNNABLE_ENABLE_COMMAND in captured.err
    assert "[FAIL] service account key" in captured.out


def test_cmd_setup_enables_the_agent_when_every_check_passes(monkeypatch):
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="fine",
                probe=lambda: deployment.CheckOutcome(deployment.Status.PASS, "fine"),
                remedy="none",
            ),
            *_sheet_checks_that(deployment.Status.PASS),
        ],
    )
    _stub_plist_write(monkeypatch)
    loaded = []
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: (loaded.append(path), (True, "loaded"))[1],
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 0
    assert len(loaded) == 1


def _sheet_checks_that(status, detail="fine"):
    return [
        deployment.Check(
            name=name,
            probe=lambda _status=status, _detail=detail: deployment.CheckOutcome(_status, _detail),
            remedy="check the Sheet",
        )
        for name in deployment.LIVE_SHEET_CHECKS
    ]


def test_cmd_setup_does_not_enable_the_agent_when_the_live_sheet_is_unverified(monkeypatch, capsys):
    """An install-day machine on someone else's wifi: both Sheet probes raise,
    _probe turns each into UNKNOWN, exit_code stays 0 - and a RunAtLoad --live
    agent used to bootstrap without the sheet_id, the sharing or the sync
    columns ever having been confirmed. Same hole as the two Criticals, reached
    by a different route, and the reason --offline is refused."""
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="python version",
                probe=lambda: deployment.CheckOutcome(deployment.Status.PASS, "3.12"),
                remedy="none",
            ),
            *_sheet_checks_that(deployment.Status.UNKNOWN, "could not reach the Sheet"),
        ],
    )
    _explode_on_launchctl(monkeypatch)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 1
    err = capsys.readouterr().err
    assert "could not be verified" in err
    assert "NOT enabled" in err
    assert RUNNABLE_ENABLE_COMMAND in err
    for name in deployment.LIVE_SHEET_CHECKS:
        assert name in err


def test_doctor_still_exits_zero_on_the_same_unverified_sheet(monkeypatch):
    """The stricter rule is the --enable-agent gate's alone. UNKNOWN keeps its
    meaning everywhere else, doctor included."""
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: _sheet_checks_that(
            deployment.Status.UNKNOWN, "could not reach the Sheet"
        ),
    )
    args = ia_bulk.build_parser().parse_args(["doctor", "--project", "sarasoldphotos", "--live"])
    assert ia_bulk.cmd_doctor(args) == 0


def test_cmd_setup_enables_the_agent_when_a_check_is_only_unknown(monkeypatch):
    """UNKNOWN outside the two Sheet checks still does not block - the files
    drive being unplugged is no reason to refuse to enable the agent."""
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="files drive",
                probe=lambda: deployment.CheckOutcome(deployment.Status.UNKNOWN, "unplugged?"),
                remedy="plug it in",
            ),
            *_sheet_checks_that(deployment.Status.PASS),
        ],
    )
    _stub_plist_write(monkeypatch)
    loaded = []
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: (loaded.append(path), (True, "loaded"))[1],
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 0
    assert len(loaded) == 1


def test_cmd_setup_returns_non_zero_when_the_bootstrap_failed(monkeypatch, capsys):
    """agent_loaded_check reports "not loaded" as UNKNOWN by design, so exit_code
    stays 0 - the one command whose purpose is loading the agent could not fail."""
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: (False, "launchctl bootstrap failed: Bootstrap failed: 5"),
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 1
    assert "not loaded" in capsys.readouterr().err


def test_cmd_setup_boots_out_an_already_loaded_agent_before_bootstrapping(monkeypatch, capsys):
    """launchd holds its own copy of the plist from bootstrap time, so after
    `git pull && ./install.sh` changes ProgramArguments the running job still
    executes the old command while doctor reports both checks PASS."""
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)
    calls = []
    monkeypatch.setattr(
        ia_bulk.platform_probe, "launchctl_print", lambda label: "\tlast exit code = 0\n"
    )
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "wait_until_unloaded",
        lambda label: (calls.append("wait"), True)[1],
    )
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootout",
        lambda label: (calls.append("bootout"), (True, "unloaded"))[1],
    )
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: (calls.append("bootstrap"), (True, "loaded"))[1],
    )

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 0
    assert calls == ["bootout", "wait", "bootstrap"]
    out = capsys.readouterr().out
    assert "unloading it first" in out
    assert "a sync running right now is stopped" in out


def test_cmd_setup_announces_the_bootout_before_it_happens(monkeypatch, capsys):
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)
    monkeypatch.setattr(
        ia_bulk.platform_probe, "launchctl_print", lambda label: "\tlast exit code = 0\n"
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "wait_until_unloaded", lambda label: True)

    def fake_bootout(label):
        assert "unloading it first" in capsys.readouterr().out
        return True, "unloaded"

    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_bootout", fake_bootout)
    monkeypatch.setattr(
        ia_bulk.platform_probe, "launchctl_bootstrap", lambda path: (True, "loaded")
    )
    ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent"))


def test_cmd_setup_does_not_bootout_an_agent_that_is_not_loaded(monkeypatch):
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootout",
        lambda label: pytest.fail("booted out an agent that was never loaded"),
    )
    monkeypatch.setattr(
        ia_bulk.platform_probe, "launchctl_bootstrap", lambda path: (True, "loaded")
    )
    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 0


def test_cmd_setup_writes_the_plist_only_when_enabling_and_before_bootstrapping(monkeypatch):
    """launchd loads every plist in LaunchAgents at login, so a plain install
    that wrote one started a live agent nobody enabled."""
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    calls = []
    monkeypatch.setattr(
        ia_bulk.launch_agent,
        "write_plist",
        lambda spec, home: (calls.append("write"), "wrote it")[1],
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: (calls.append("bootstrap"), (True, "loaded"))[1],
    )

    assert ia_bulk.cmd_setup(_setup_args("--live")) == 0
    assert calls == []
    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 0
    assert calls == ["write", "bootstrap"]


def test_cmd_setup_does_not_bootstrap_when_the_plist_cannot_be_written(monkeypatch, capsys):
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])

    def unwritable(spec, home):
        raise PermissionError("Operation not permitted")

    _explode_on_launchctl(monkeypatch)
    monkeypatch.setattr(ia_bulk.launch_agent, "write_plist", unwritable)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 1
    captured = capsys.readouterr()
    assert "could not write" in captured.out
    assert "not loaded" in captured.err


def test_cmd_setup_does_not_bootstrap_when_a_failed_bootout_leaves_the_agent_loaded(
    monkeypatch, capsys
):
    """Bootstrapping over a still-loaded agent fails anyway, and hides why."""
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)
    monkeypatch.setattr(
        ia_bulk.platform_probe, "launchctl_print", lambda label: "\tlast exit code = 0\n"
    )
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootout",
        lambda label: (False, "launchctl bootout failed: Boot-out failed: 5"),
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "wait_until_unloaded", lambda label: False)
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: pytest.fail("bootstrapped over an agent that is still loaded"),
    )

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 1
    captured = capsys.readouterr()
    assert "Boot-out failed: 5" in captured.out
    assert "not loaded" in captured.err


def test_cmd_setup_bootstraps_once_an_in_progress_bootout_finishes(monkeypatch):
    """launchctl bootout exits 36 while a running job is still stopping."""
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)
    monkeypatch.setattr(
        ia_bulk.platform_probe, "launchctl_print", lambda label: "\tlast exit code = 0\n"
    )
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootout",
        lambda label: (False, "launchctl bootout failed: Boot-out failed: 36: Operation now in progress"),
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "wait_until_unloaded", lambda label: True)
    loaded = []
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: (loaded.append(path), (True, "loaded"))[1],
    )

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 0
    assert len(loaded) == 1


def test_cmd_setup_does_not_bootstrap_until_the_old_agent_is_gone(monkeypatch, capsys):
    monkeypatch.setattr(ia_bulk, "build_deployment_checks", lambda args, include_network: [])
    _stub_plist_write(monkeypatch)
    monkeypatch.setattr(
        ia_bulk.platform_probe, "launchctl_print", lambda label: "\tlast exit code = 0\n"
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_bootout", lambda label: (True, "unloaded"))
    monkeypatch.setattr(ia_bulk.platform_probe, "wait_until_unloaded", lambda label: False)
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: pytest.fail("bootstrapped while the old agent was still registered"),
    )

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 1
    assert "still registered" in capsys.readouterr().out


def _write_registry(path):
    path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    return path


def test_enable_agent_points_the_agent_at_the_registry_setup_checked(tmp_path, monkeypatch):
    """The gate reads --registry; an agent reading projects_registry.json instead
    would run an hourly live sync against a Sheet the gate never saw."""
    registry_path = _write_registry(tmp_path / "alt.json")
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: _sheet_checks_that(deployment.Status.PASS),
    )
    written = []
    monkeypatch.setattr(
        ia_bulk.launch_agent,
        "write_plist",
        lambda spec, home: (written.append(spec), f"wrote {spec.label}.plist")[1],
    )
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_bootstrap", lambda path: (True, "loaded"))
    args = ia_bulk.build_parser().parse_args(
        ["setup", "--project", "astoriaphotos", "--registry", str(registry_path),
         "--live", "--enable-agent"]
    )

    assert ia_bulk.cmd_setup(args) == 0
    arguments = written[0].program_arguments
    assert arguments[arguments.index("--registry") + 1] == str(registry_path.resolve())


def _plist_check_outcome(registry_path, home, monkeypatch):
    monkeypatch.setattr(ia_bulk.Path, "home", lambda: home)
    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "astoriaphotos", "--registry", str(registry_path)]
    )
    checks = ia_bulk.build_deployment_checks(args, include_network=False)
    (plist_check,) = [check for check in checks if check.name == "launch agent plist"]
    return plist_check.probe()


def test_doctor_passes_the_plist_enable_agent_wrote_for_the_same_registry(tmp_path, monkeypatch):
    registry_path = _write_registry(tmp_path / "alt.json")
    home = tmp_path / "home"
    ia_bulk.launch_agent.write_plist(
        ia_bulk.launch_agent.sync_agent_spec(ia_bulk.REPO_ROOT, "astoriaphotos", registry_path), home
    )
    assert _plist_check_outcome(registry_path, home, monkeypatch).status is deployment.Status.PASS


def test_doctor_flags_a_plist_enabled_for_a_different_registry(tmp_path, monkeypatch):
    home = tmp_path / "home"
    ia_bulk.launch_agent.write_plist(
        ia_bulk.launch_agent.sync_agent_spec(
            ia_bulk.REPO_ROOT, "astoriaphotos", _write_registry(tmp_path / "alt.json")
        ),
        home,
    )
    outcome = _plist_check_outcome(_write_registry(tmp_path / "other.json"), home, monkeypatch)
    assert outcome.status is deployment.Status.FAIL
    assert "registry" in outcome.detail


def test_cmd_setup_reenables_an_agent_whose_last_run_failed(tmp_path, monkeypatch):
    """The loaded check's FAIL used to block the very reload its remedy prescribes."""
    spec = ia_bulk.launch_agent.sync_agent_spec(tmp_path, "sarasoldphotos", tmp_path / "registry.json")
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.agent_loaded_check(spec, deployment.InstallCommand("sarasoldphotos")),
            *_sheet_checks_that(deployment.Status.PASS),
        ],
    )
    _stub_plist_write(monkeypatch)
    launchd: dict[str, str | None] = {"listing": "\tlast exit code = 1\n"}
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda label: launchd["listing"])

    def bootout(label):
        launchd["listing"] = None
        return True, "unloaded"

    def bootstrap(path):
        launchd["listing"] = "\tpid = 42\n\tlast exit code = (never exited)\n"
        return True, "loaded"

    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_bootout", bootout)
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_bootstrap", bootstrap)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 0
    assert launchd["listing"] == "\tpid = 42\n\tlast exit code = (never exited)\n"


def test_cmd_setup_enables_the_agent_despite_a_failing_files_drive(tmp_path, monkeypatch, capsys):
    """sync-metadata never reads the drive, so its FAIL is reported but does not gate."""
    not_a_directory = tmp_path / "files"
    not_a_directory.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.drive_check(not_a_directory),
            *_sheet_checks_that(deployment.Status.PASS),
        ],
    )
    _stub_plist_write(monkeypatch)
    loaded = []
    monkeypatch.setattr(ia_bulk.platform_probe, "launchctl_print", lambda _: None)
    monkeypatch.setattr(
        ia_bulk.platform_probe,
        "launchctl_bootstrap",
        lambda path: (loaded.append(path), (True, "loaded"))[1],
    )

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 1
    captured = capsys.readouterr()
    assert len(loaded) == 1
    assert "NOT enabled" not in captured.err
    assert ia_bulk.AGENT_ENABLED_DESPITE_FAILS in captured.err
    assert "[FAIL] files drive" in captured.out


def test_cmd_setup_names_the_failures_rather_than_the_network_when_both_block(monkeypatch, capsys):
    """An offline machine with a FAIL: the FAIL leads, and the Sheet checks are still named."""
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="service account key",
                probe=lambda: deployment.CheckOutcome(deployment.Status.FAIL, "no key"),
                remedy="download the key",
            ),
            *_sheet_checks_that(deployment.Status.UNKNOWN, "could not authenticate"),
        ],
    )
    _explode_on_launchctl(monkeypatch)

    assert ia_bulk.cmd_setup(_setup_args("--live", "--enable-agent")) == 1
    err = capsys.readouterr().err
    assert err.index("service account key failed") < err.index("came back UNKNOWN too")
    for name in deployment.LIVE_SHEET_CHECKS:
        assert name in err
    assert "no network" not in err


@pytest.mark.parametrize(
    "content",
    [pytest.param(b"{not json", id="malformed"), pytest.param(b'{"x": "caf\xe9"}', id="not_utf8")],
)
def test_cmd_setup_reports_a_broken_registry_instead_of_a_traceback(tmp_path, capsys, content):
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(content)
    args = ia_bulk.build_parser().parse_args(
        ["setup", "--project", "astoriaphotos", "--registry", str(registry_path)]
    )
    assert ia_bulk.cmd_setup(args) == 1
    assert str(registry_path) in capsys.readouterr().err


def test_cmd_setup_does_not_claim_the_machine_matches_when_a_check_failed(monkeypatch, capsys):
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: [
            deployment.Check(
                name="ia credentials",
                probe=lambda: deployment.CheckOutcome(deployment.Status.FAIL, "no ia config"),
                remedy="run ia configure",
            )
        ],
    )
    assert ia_bulk.cmd_setup(_setup_args()) == 1
    out = capsys.readouterr().out
    assert "already matches" not in out
    assert "[FAIL] ia credentials" in out

def test_cmd_doctor_reports_a_broken_registry_instead_of_a_traceback(tmp_path, capsys):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{not json", encoding="utf-8")
    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "astoriaphotos", "--registry", str(registry_path)]
    )
    assert ia_bulk.cmd_doctor(args) == 1
    assert str(registry_path) in capsys.readouterr().err


def test_cmd_doctor_reports_a_missing_registry_instead_of_a_traceback(tmp_path, capsys):
    missing = tmp_path / "nope.json"
    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "astoriaphotos", "--registry", str(missing)]
    )
    assert ia_bulk.cmd_doctor(args) == 1
    assert str(missing) in capsys.readouterr().err


def test_cmd_doctor_reports_an_unregistered_project_instead_of_a_traceback(tmp_path, capsys):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "nosuchproject", "--registry", str(registry_path)]
    )
    assert ia_bulk.cmd_doctor(args) == 1
    assert capsys.readouterr().err.strip()


def test_build_deployment_checks_reads_the_sheet_once_for_both_sheet_checks(tmp_path, monkeypatch):
    """Two closures each building their own client meant two token fetches and
    two full reads of a ~10,000-row Sheet per read-only `doctor` run."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    clients_built = []

    class _OneGrid:
        def read_grid(self):
            header = [*SHEET_HEADER, "ia_sync_hash", "ia_last_synced"]
            return [header, ["a"] * len(header)]

    monkeypatch.setattr(
        ia_bulk,
        "build_sheet_client",
        lambda config, live: (clients_built.append(live), _OneGrid())[1],
    )
    monkeypatch.setattr(ia_bulk, "sheet_sharing_target", lambda: "sa@example.invalid")

    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "astoriaphotos", "--registry", str(registry_path)]
    )
    checks = ia_bulk.build_deployment_checks(args, include_network=True)
    sheet_checks = [
        check for check in checks if check.name in ("spreadsheet reachable", "sync state columns")
    ]
    assert len(sheet_checks) == 2
    for check in sheet_checks:
        assert check.probe().status is deployment.Status.PASS
    assert len(clients_built) == 1


def test_build_deployment_checks_covers_the_ia_credential(tmp_path):
    """Ten checks covered Python, deps, the Google key, the Sheet, the drive and
    the agent - and none covered the credential that grants write on Internet
    Archive, the only one the hourly agent needs to do its actual job."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "astoriaphotos", "--registry", str(registry_path)]
    )
    names = [check.name for check in ia_bulk.build_deployment_checks(args, include_network=False)]
    assert "ia credentials" in names
    assert "ia credentials permissions" in names


def test_build_deployment_checks_skips_the_sheet_read_while_the_sheet_id_is_a_placeholder(
    tmp_path, monkeypatch
):
    """Reading it earned a 404 FAIL whose remedy said to share the Sheet."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(test_sheet_id="REPLACE_WITH_TEST_SHEET_ID")),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ia_bulk, "build_sheet_client", lambda config, live: pytest.fail("read a placeholder ID")
    )
    monkeypatch.setattr(ia_bulk, "sheet_sharing_target", lambda: "sa@example.invalid")
    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "astoriaphotos", "--registry", str(registry_path)]
    )
    checks = ia_bulk.build_deployment_checks(args, include_network=True)
    for check in checks:
        if check.name in deployment.LIVE_SHEET_CHECKS:
            outcome = check.probe()
            assert outcome.status is deployment.Status.UNKNOWN
            assert "placeholder" in outcome.detail


@pytest.mark.parametrize(
    "extra, dropped, data_rows, expected",
    [
        pytest.param([], None, 1, None, id="ready"),
        pytest.param(["title"], None, 1, "both normalize", id="own_columns_collide"),
        pytest.param([""], None, 1, "empty field name", id="blank_header"),
        pytest.param([], "ia_last_synced", 1, "ia_last_synced", id="sync_column_missing"),
        pytest.param([], "ia_identifier_bib", 1, "ia_identifier_bib", id="write_back_column_missing"),
        pytest.param([], "file", 1, "file_template", id="file_template_column_missing"),
        pytest.param([], None, 0, "no data rows", id="no_data_rows"),
    ],
)
def test_the_sync_check_refuses_what_sync_metadata_refuses(
    tmp_path, monkeypatch, extra, dropped, data_rows, expected
):
    """The gate used to PASS Sheets the agent then refused every hour."""
    header = [*SHEET_HEADER, "ia_sync_hash", "ia_last_synced", *extra]
    if dropped is not None:
        header.remove(dropped)
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")

    class _Grid:
        def read_grid(self):
            return [header, *(["a"] * len(header) for _ in range(data_rows))]

    monkeypatch.setattr(ia_bulk, "build_sheet_client", lambda config, live: _Grid())
    monkeypatch.setattr(ia_bulk, "sheet_sharing_target", lambda: "sa@example.invalid")
    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "astoriaphotos", "--registry", str(registry_path)]
    )
    sync_check = next(
        check
        for check in ia_bulk.build_deployment_checks(args, include_network=True)
        if check.name == deployment.SYNC_COLUMNS_CHECK
    )
    outcome = sync_check.probe()
    config = ia_bulk.load_project_config(make_sheet_registry(), "astoriaphotos")
    refusal = ia_bulk.sync_header_refusal(build_column_map(header), config, str(registry_path))

    if expected is None:
        assert outcome.status is deployment.Status.PASS
        assert refusal is None
    else:
        assert outcome.status is deployment.Status.FAIL
        assert expected in outcome.detail
        # The no-data-rows refusal is read_sheet's, not the header gate's.
        assert (refusal is not None) == bool(data_rows)


def test_cmd_setup_reuses_the_same_repo_root_build_deployment_checks_uses():
    assert ia_bulk.REPO_ROOT == Path(ia_bulk.__file__).resolve().parent


def test_every_install_sh_remedy_names_the_required_project_flag(tmp_path):
    """setup's --project is required (build_parser), and install.sh execs
    `ia_bulk.py setup "$@"` verbatim - so a remedy that tells the operator to
    run ./install.sh (with or without --enable-agent) but leaves out
    --project hands them a command argparse rejects before anything runs.
    Built through the real build_deployment_checks(), not hand-made Check
    objects, so this sees the actual remedy strings doctor/setup print."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(make_sheet_registry()), encoding="utf-8")
    args = ia_bulk.build_parser().parse_args(
        ["doctor", "--project", "astoriaphotos", "--registry", str(registry_path)]
    )
    checks = ia_bulk.build_deployment_checks(args, include_network=False)
    install_sh_remedies = [check.remedy for check in checks if "install.sh" in check.remedy]

    assert install_sh_remedies, "expected at least one check to remedy via install.sh"
    for remedy in install_sh_remedies:
        assert "./install.sh --project astoriaphotos" in remedy, (
            f"remedy names install.sh but not the real --project: {remedy!r}"
        )
        # Pasted into zsh, `<project>` is a redirect from a file named "project".
        assert "<" not in remedy and ">" not in remedy, f"remedy has a placeholder: {remedy!r}"
        # Without it, re-running enables an agent on the default registry, and doctor still FAILs.
        assert f"--registry {shlex.quote(str(registry_path.resolve()))}" in remedy, remedy


def test_install_command_omits_the_checkouts_own_registry():
    args = _setup_args("--registry", str(ia_bulk.REPO_ROOT / "projects_registry.json"))
    assert ia_bulk.install_command_for(args).registry is None


def test_cmd_setup_refusal_repeats_a_non_default_registry(tmp_path, monkeypatch, capsys):
    """Run as printed, a refusal that dropped --registry would enable a live agent
    on the default registry's Sheet instead."""
    monkeypatch.setattr(
        ia_bulk,
        "build_deployment_checks",
        lambda args, include_network: pytest.fail("setup ran checks before refusing"),
    )
    _explode_on_launchctl(monkeypatch)
    registry_path = tmp_path / "alt.json"

    assert ia_bulk.cmd_setup(_setup_args("--registry", str(registry_path), "--enable-agent")) == 1
    assert (
        "./install.sh --project sarasoldphotos "
        f"--registry {shlex.quote(str(registry_path.resolve()))} --live --enable-agent"
    ) in capsys.readouterr().err


@pytest.mark.parametrize(
    ("blocking", "unverified"),
    [
        (["service account key"], []),
        ([], ["spreadsheet reachable"]),
        (["service account key"], ["spreadsheet reachable"]),
    ],
)
def test_agent_not_enabled_message_names_the_real_project(blocking, unverified):
    message = ia_bulk.agent_not_enabled_message(
        blocking, unverified, deployment.InstallCommand("sarasoldphotos")
    )
    assert RUNNABLE_ENABLE_COMMAND in message
    assert "<project>" not in message


# ---------------------------------------------------------------------------
# Task 10: the Sheet path's reserve -> upload -> confirm protocol.
#
# Every assertion below that matters is an ORDERED SEQUENCE, not a membership
# or substring check. The safety argument for this whole command is that the
# reserve write lands BEFORE the upload, and `"write" in kinds` cannot tell
# [read, write, upload] from [read, upload, write].
# ---------------------------------------------------------------------------

_A1_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _a1_to_indexes(a1):
    match = _A1_RE.match(a1)
    assert match, f"not an A1 reference: {a1!r}"
    column = 0
    for character in match.group(1):
        column = column * 26 + (ord(character) - ord("A") + 1)
    return column - 1, int(match.group(2)) - 1


class SheetUploadRecorder:
    """One ordered event log spanning BOTH the Sheet client and upload_row.

    Keeping reads, writes and uploads in a single list is the point: the
    reserve/confirm protocol is a claim about ORDER across two different
    collaborators, and two separate per-collaborator lists cannot express it."""

    def __init__(self):
        self.events = []

    @property
    def kinds(self):
        return [event[0] for event in self.events]

    @property
    def writes(self):
        return [event[1] for event in self.events if event[0] == "write"]

    @property
    def uploads(self):
        return [event[1] for event in self.events if event[0] == "upload"]


class RecordingLogTab:
    """A client bound to a log tab. Deliberately has no write_cells: the tab
    writer is handed one of these, so there is no method by which telemetry
    could reach the metadata columns even if the code tried."""

    def __init__(self, tab, fails=False):
        self.tab = tab
        self.ensured = []
        self.appended = []
        self._fails = fails

    def ensure_tab(self, header):
        if self._fails:
            raise RuntimeError("Sheets API returned 503")
        self.ensured.append(header)

    def append_rows(self, rows):
        if self._fails:
            raise RuntimeError("Sheets API returned 503")
        self.appended.extend(rows)


class RecordingSheetClient:
    """Stands in for SheetClient on the upload path. Unlike FakeSheetClient it
    also accepts writes, and APPLIES them to its own grid - so the confirm
    step's re-read sees what the reserve step wrote, exactly as a real Sheet
    would. `before_read` is the hook a mid-run-edit test uses to change the
    grid out from under the run between two reads."""

    def __init__(
        self,
        grid,
        recorder,
        before_read=None,
        raise_on_write=None,
        raise_on_read=None,
        raise_on_log_tab=False,
    ):
        self.grid = [list(row) for row in grid]
        self._recorder = recorder
        self._before_read = before_read
        self._raise_on_write = raise_on_write
        self._raise_on_read = raise_on_read
        self._raise_on_log_tab = raise_on_log_tab
        self.read_count = 0
        self.write_count = 0
        # tab name -> the LogTab handed out for it. Each one records what was
        # written to that tab, so a test can assert on the log tab separately
        # from the metadata tab - which is the whole property under test.
        self.log_tabs = {}

    def append_only_tab(self, tab):
        self.log_tabs.setdefault(tab, RecordingLogTab(tab, fails=self._raise_on_log_tab))
        return self.log_tabs[tab]

    def read_grid(self):
        self.read_count += 1
        if self._before_read is not None:
            self._before_read(self.grid, self.read_count)
        self._recorder.events.append(("read", None))
        if self._raise_on_read == self.read_count:
            raise RuntimeError("Sheets API returned 503")
        return [list(row) for row in self.grid]

    def write_cells(self, updates):
        pairs = [(update.a1, update.value) for update in updates]
        assert pairs, "write_cells must never be called with an empty batch"
        self.write_count += 1
        self._recorder.events.append(("write", pairs))
        if self._raise_on_write == self.write_count:
            raise RuntimeError("Sheets API returned 503")
        for a1, value in pairs:
            column, row_index = _a1_to_indexes(a1)
            while len(self.grid) <= row_index:
                self.grid.append([])
            row = self.grid[row_index]
            while len(row) <= column:
                row.append("")
            row[column] = value


def _row_records(log_path):
    """A log's per-row records, without its run-level ones. "record" marks a
    run-level line - the header, and the closing run summary - and a row
    carries no such key."""
    entries = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    return [entry for entry in entries if "record" not in entry]


def make_upload_stub(recorder, fail_for=(), captured=None):
    def fake_upload_row(row, target_identifier, collection, files_dir):
        recorder.events.append(("upload", target_identifier))
        if captured is not None:
            captured.append(
                {"row": dict(row), "collection": collection, "files_dir": str(files_dir)}
            )
        if target_identifier in fail_for:
            raise RuntimeError("boom")

    return fake_upload_row


# A=Title, B=file, C=ia_identifier, D=ia_uploaded, E=ia_url, F=ia_identifier_bib
SHEET_HEADER = ["Title", "file", "ia_identifier", "ia_uploaded", "ia_url", "ia_identifier_bib"]
FIXED_TIMESTAMP = "2026-08-19T09:00:00"


def make_upload_args(tmp_path, registry_path, **overrides):
    args = Namespace(
        project="astoriaphotos",
        registry=str(registry_path),
        live=False,
        write_identifier=False,
        dry_run=False,
        log_dir=str(tmp_path / "logs"),
    )
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def setup_sheet_upload(
    tmp_path,
    monkeypatch,
    grid,
    files=("photo1.jpg",),
    fail_for=(),
    captured=None,
    before_read=None,
    registry=None,
    raise_on_write=None,
    raise_on_read=None,
    raise_on_log_tab=False,
    timestamps=None,
):
    """Builds the whole Sheet-upload world: files on disk, a registry, a
    recording client monkeypatched over build_sheet_client (the single seam),
    and upload_row stubbed so nothing touches the network. Also pins the
    confirm timestamp so a confirm batch can be asserted as an exact ordered
    sequence rather than 'a cell whose value is some string', and pins
    run_stamp() to FIXED_STAMP for the same reason - a caller that needs to
    prove the stamp is computed once per run rather than once per row/chunk
    re-monkeypatches ia_bulk.run_stamp itself after calling this."""
    for name in files:
        (tmp_path / name).write_bytes(b"x")

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(registry or make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )

    recorder = SheetUploadRecorder()
    client = RecordingSheetClient(
        grid,
        recorder,
        before_read=before_read,
        raise_on_write=raise_on_write,
        raise_on_read=raise_on_read,
        raise_on_log_tab=raise_on_log_tab,
    )
    build_calls = []

    def fake_build_sheet_client(config, live):
        build_calls.append((config, live))
        return client

    # A list of timestamps hands out one per call, so a multi-chunk test can
    # tell "stamped when its own chunk ran" from "stamped once for the whole
    # run" - on a full-collection run those differ by hours.
    remaining = list(timestamps or [])

    def fake_upload_timestamp():
        return remaining.pop(0) if remaining else FIXED_TIMESTAMP

    monkeypatch.setattr("ia_bulk.build_sheet_client", fake_build_sheet_client)
    monkeypatch.setattr("ia_bulk.upload_row", make_upload_stub(recorder, fail_for, captured))
    monkeypatch.setattr("ia_bulk.upload_timestamp", fake_upload_timestamp)
    monkeypatch.setattr("ia_bulk.run_stamp", lambda: FIXED_STAMP)

    return recorder, client, registry_path, build_calls


def test_cmd_upload_default_mode_writes_nothing_to_the_sheet_and_only_uploads_prefixed_identifiers(
    tmp_path, monkeypatch, capsys
):
    """The single most important property of this command: with neither
    --live nor --write-identifier, the operator's Sheet is untouched and
    nothing reaches Internet Archive under a real, permanent identifier.
    Asserted as 'the recorded call list holds exactly one read and no writes
    at all', not as 'no write of the wrong thing' - the latter still passes if
    a write happens with contents the test did not think to check."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.kinds == ["read", "upload", "upload"]
    assert recorder.writes == []
    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",
    ]
    assert client.grid == [list(row) for row in grid]


def test_cmd_upload_writes_the_run_header_as_the_first_line_of_the_sheet_path_log(
    tmp_path, monkeypatch, capsys
):
    """The bug this test exists to catch: log_run_header() was implemented
    and covered by tests that call it directly, but nothing in cmd_upload's
    Sheet path (upload_from_sheet) ever called it, so no real run wrote one.
    Driving the real entry point (cmd_upload -> upload_from_sheet, the same
    path the CLI takes) rather than calling log_run_header() directly is the
    point - a direct-call test could not have caught this. The header must be
    line 1, not merely present somewhere in the file, since a human doing
    `head -1` depends on that exact position."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    assert exit_code == 0
    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    assert len(log_files) == 1
    lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    # Header, the one row, then the closing run summary.
    assert len(lines) == 3

    header = json.loads(lines[0])
    assert header["record"] == "run_header"
    assert header["project"] == "astoriaphotos"
    assert header["live"] is False
    assert header["dry_run"] is False
    assert header["sheet_id"] == "TEST_SHEET_ID"
    # The collection this run TARGETED - a test run's items go to
    # test_collection, not to the registry's own ia_collection.
    assert header["collection"] == "test_collection"
    assert header["required_for_upload"] == ["title"]
    assert header["columns"]["Title"] == "title"
    # Task 12: neither --limit nor --chunk-size was passed, so the header
    # records the run's own defaults rather than omitting the fields.
    assert header["limit"] is None
    assert header["chunk_size"] == CHUNK_SIZE

    result = json.loads(lines[1])
    assert result["status"] == "success"
    assert result["identifier"] == "lcps-astoriaphotos-00001"


def test_cmd_upload_survives_a_run_header_write_failure_and_still_uploads(
    tmp_path, monkeypatch, capsys
):
    """The run header is a receipt for later, not part of the upload itself -
    a disk-full or permissions problem hit while writing it must not stop a
    run that is about to create permanent Internet Archive items, the same
    way a Sheet write failing mid-run is reported cleanly rather than left to
    crash (see SheetUploadRun._write). log_run_header() is monkeypatched to
    raise, standing in for that failure without needing an actually-unwritable
    log_dir on every platform this runs on."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)
    monkeypatch.setattr(
        "ia_bulk.log_run_header",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "could not write the run-header record" in captured.err
    assert "disk full" in captured.err
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]

    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    # The header failed to write, so no run_header record exists - proving
    # the failure was swallowed rather than silently retried or masked. The
    # run's own records are all still there: the row result, then the closing
    # summary.
    records = [json.loads(line) for line in lines]
    assert not any(entry.get("record") == "run_header" for entry in records)
    assert records[0]["status"] == "success"


def test_cmd_upload_with_write_identifier_reserves_then_uploads_then_confirms(
    tmp_path, monkeypatch, capsys
):
    """Reserve BEFORE upload is the entire ordering argument: uploading first
    would let a crash strand an item on IA the Sheet has no record of, and the
    next run's max+1 would mint that same number onto a different photograph.
    The exact event sequence is asserted, so a reordering fails here even
    though every individual call would still be 'present'. The read before the
    reserve write is the mid-run-edit guard: row numbers come from the initial
    read and are re-verified against a fresh one before every write."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.kinds == ["read", "read", "write", "upload", "read", "write"]
    assert recorder.writes == [
        [("C2", "lcps-astoriaphotos-00001")],
        [
            ("D2", FIXED_TIMESTAMP),
            ("E2", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
            ("F2", "photo1.jpg"),
        ],
    ]
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]


def test_cmd_upload_reserved_row_uploads_under_its_existing_identifier_and_mints_nothing(
    tmp_path, monkeypatch, capsys
):
    """RESERVED is the crash-recovery state: a number reached the Sheet but
    the upload never confirmed. Re-minting would burn a second permanent
    number on the same photograph, so the run must reuse the existing one and
    issue NO reserve write at all - which is why the event sequence has no
    write before the upload."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "lcps-astoriaphotos-00042", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00042"]
    # The pre-reserve read still happens (the guard runs whether or not there
    # is anything to reserve); the reserve WRITE does not.
    assert recorder.kinds == ["read", "read", "upload", "read", "write"]
    assert recorder.writes == [
        [
            ("D2", FIXED_TIMESTAMP),
            ("E2", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00042"),
            ("F2", "photo1.jpg"),
        ]
    ]
    assert client.grid[1][2] == "lcps-astoriaphotos-00042"


def test_cmd_upload_skips_a_done_row_entirely_and_mints_above_its_number(
    tmp_path, monkeypatch, capsys
):
    """A DONE row is not re-uploaded, and its number still counts when the
    next one is minted - otherwise the run would hand an already-used
    permanent identifier to a different photograph."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["Done photo", "photo1.jpg", "lcps-astoriaphotos-00007", "2026-01-01", "u", "photo1.jpg"],
        ["New photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00008"]
    assert recorder.writes == [
        [("C3", "lcps-astoriaphotos-00008")],
        [
            ("D3", FIXED_TIMESTAMP),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00008"),
            ("F3", "photo2.jpg"),
        ],
    ]


def test_cmd_upload_skips_an_invalid_row_uploads_the_valid_ones_and_exits_non_zero(
    tmp_path, monkeypatch, capsys
):
    """One typo in row
    9,000 must not block the other 9,999, but a partial run must never be
    mistaken for a clean one. See docs/DECISIONS.md, "On the Sheet path,
    `upload` uploads the valid rows and reports the rest".

    Row 2's file is broken (present but resolves to nothing) rather than
    its title being blank - a blank required_for_upload column is now a
    readiness fact, not a validation error (see the SHEET_REQUIRED_COLUMNS
    shrink), so a blank title no longer produces the genuinely INVALID row
    this test needs."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "does-not-exist.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo2.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    out = capsys.readouterr().out

    assert exit_code == 1
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]
    assert recorder.writes == [
        [("C3", "lcps-astoriaphotos-00001")],
        [
            ("D3", FIXED_TIMESTAMP),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
            ("F3", "photo2.jpg"),
        ],
    ]
    assert "[FAIL] row 2" in out
    assert _unresolved_message(tmp_path, "does-not-exist.jpg") in out


def test_cmd_upload_dry_run_writes_nothing_uploads_nothing_and_prints_what_it_would_mint(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(
        make_upload_args(tmp_path, registry_path, write_identifier=True, dry_run=True)
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert recorder.kinds == ["read"]
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "lcps-astoriaphotos-00001" in out
    assert "C2 = lcps-astoriaphotos-00001" in out
    # The real run would upload under the STAMPED identifier, not the bare
    # permanent one - a dry run that only ever showed the real identifier
    # would misrepresent what --live's absence actually means. See
    # docs/DECISIONS.md, "Test identifiers carry a per-run stamp".
    assert (
        f"would mint 'lcps-astoriaphotos-00001' and upload it as "
        f"'zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001'"
    ) in out
    assert not (tmp_path / "logs").exists()


def test_cmd_upload_dry_run_without_write_identifier_says_it_would_write_nothing(
    tmp_path, monkeypatch, capsys
):
    """Pins the false branch of --write-identifier under --dry-run too: a
    rehearsal that describes writes it would never actually make is worse
    than no rehearsal."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert recorder.writes == []
    assert "would write nothing to the Sheet" in out
    assert "C2 = " not in out


@pytest.mark.parametrize(
    "live,expected_sheet_id,expected_collection,expected_target",
    [
        (True, "REAL_SHEET_ID", "lcpsociety", "lcps-astoriaphotos-00001"),
        (False, "TEST_SHEET_ID", "test_collection", f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
    ],
)
def test_cmd_upload_passes_the_live_flag_through_and_picks_the_matching_sheet_and_collection(
    tmp_path, monkeypatch, capsys, live, expected_sheet_id, expected_collection, expected_target
):
    """Both directions, deliberately: Task 8 shipped a version that hardcoded
    build_sheet_client(config, True) - always the production Sheet - and
    passed all 167 tests because only live=True was ever exercised."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    captured = []
    recorder, client, registry_path, build_calls = setup_sheet_upload(
        tmp_path, monkeypatch, grid, captured=captured
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, live=live))
    capsys.readouterr()

    assert exit_code == 0
    assert len(build_calls) == 1
    config, passed_live = build_calls[0]
    assert passed_live is live
    assert config.sheet_id_for(passed_live) == expected_sheet_id
    assert [call["collection"] for call in captured] == [expected_collection]
    assert recorder.uploads == [expected_target]


def test_cmd_upload_live_writes_back_even_without_write_identifier(tmp_path, monkeypatch, capsys):
    """--live always records: an item that exists on Internet Archive under a
    permanent identifier the Sheet does not know about is the one outcome
    this protocol exists to prevent."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, live=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.kinds == ["read", "read", "write", "upload", "read", "write"]
    assert recorder.writes[0] == [("C2", "lcps-astoriaphotos-00001")]
    assert recorder.writes[1] == [
        ("D2", FIXED_TIMESTAMP),
        ("E2", "https://archive.org/details/lcps-astoriaphotos-00001"),
        ("F2", "photo1.jpg"),
    ]


@pytest.mark.parametrize(
    "live,expected_uploaded_as",
    [
        (False, f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
        (True, "lcps-astoriaphotos-00001"),
    ],
)
def test_cmd_upload_sheet_path_logs_the_item_each_row_was_uploaded_as(
    tmp_path, monkeypatch, capsys, live, expected_uploaded_as
):
    """`identifier` stays the real one; `uploaded_as` is the stamped target in
    test mode and the bare identifier live."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, live=live))
    capsys.readouterr()

    assert exit_code == 0
    entry = _row_records(next((tmp_path / "logs").glob("upload-*.jsonl")))[0]
    assert entry["identifier"] == "lcps-astoriaphotos-00001"
    assert entry["uploaded_as"] == expected_uploaded_as
    assert entry["status"] == "success"


def test_cmd_upload_sheet_path_prints_a_progress_line_per_row(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert f"[1/2] uploading zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001 (photo1.jpg)" in out
    assert f"[2/2] uploading zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002 (photo2.jpg)" in out


def _target(identifier, newly_minted=True, row_number=2):
    """An UploadTarget carrying only what check_claimed_identifiers reads."""
    from ia_bulk import UploadTarget

    return UploadTarget(
        row={"file": "photo1.jpg"},
        row_number=row_number,
        identifier=identifier,
        uploaded_as=identifier,
        identifier_bib="photo1.jpg",
        newly_minted=newly_minted,
        source_fingerprint="photo1.jpg",
    )


def _snapshot(claimed):
    from ia_bulk import SheetColumns, SheetSnapshot

    return SheetSnapshot(
        columns=SheetColumns(ia_identifier=2, ia_uploaded=3, ia_url=4, ia_identifier_bib=5),
        column_map=build_column_map(SHEET_HEADER),
        grid=[],
        fingerprints={},
        claimed_identifiers=frozenset(claimed),
    )


def test_check_claimed_identifiers_stops_when_a_minted_number_was_taken_elsewhere():
    """plan_upload_targets mints the whole run's numbers up front as max+1,
    max+2, ... from one read that can be hours old by the last chunk.
    split_moved_targets only inspects a target's OWN row, so a number written
    to a row this run is not targeting is invisible to it - and that is the
    case that mints a duplicate."""
    from ia_bulk import check_claimed_identifiers

    targets = [_target("lcps-astoriaphotos-00001"), _target("lcps-astoriaphotos-00002")]

    reason = check_claimed_identifiers(targets, _snapshot({"lcps-astoriaphotos-00002"}))

    assert reason is not None
    assert "lcps-astoriaphotos-00002" in reason
    assert "Nothing has been reserved or uploaded" in reason


def test_check_claimed_identifiers_passes_when_nothing_was_taken():
    from ia_bulk import check_claimed_identifiers

    targets = [_target("lcps-astoriaphotos-00001")]

    assert check_claimed_identifiers(targets, _snapshot({"lcps-astoriaphotos-00099"})) is None


def test_check_claimed_identifiers_ignores_a_reserved_rows_own_identifier():
    """A RESERVED row's identifier is already in the Sheet by definition -
    that is what RESERVED means. Checking it would stop every retry run on
    its own reservation."""
    from ia_bulk import check_claimed_identifiers

    targets = [_target("lcps-astoriaphotos-00007", newly_minted=False)]

    assert check_claimed_identifiers(targets, _snapshot({"lcps-astoriaphotos-00007"})) is None


def test_cmd_upload_stops_when_a_minted_identifier_is_claimed_before_reserve(
    tmp_path, monkeypatch, capsys
):
    """The whole run stops, not just the colliding row: every number it holds
    came out of the same max+1 arithmetic over the same stale read, so one
    collision means the max was wrong and the rest are suspect too."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
        # Not ready (no title), so never a target of this run - which is
        # exactly why split_moved_targets cannot see a number landing here.
        ["", "photo3.jpg", "", "", "", ""],
    ]

    def claim_a_number_elsewhere(live_grid, read_count):
        # Read 1 is the initial grid read, read 2 the pre-reserve guard.
        if read_count == 2:
            live_grid[3][2] = "lcps-astoriaphotos-00002"

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        before_read=claim_a_number_elsewhere,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    # Nothing permanent happened: no upload, and no cell written.
    assert recorder.uploads == []
    assert recorder.writes == []
    assert "were claimed in the Sheet after this run read it" in captured.err
    assert "lcps-astoriaphotos-00002" in captured.err
    assert exit_code == 1


def test_cmd_upload_confirm_leg_does_not_trip_the_claimed_identifier_guard(
    tmp_path, monkeypatch, capsys
):
    """After reserve, this run's own numbers ARE in the Sheet. Running the
    check on the confirm leg would flag every one of them and stop every
    ordinary run."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
    ]

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]
    # reserve wrote the identifier, confirm wrote the other three cells
    assert len(recorder.writes) == 2
    assert "were claimed in the Sheet" not in captured.err
    assert exit_code == 0


def test_cmd_upload_confirm_skips_a_row_whose_identifier_changed_underneath_it(
    tmp_path, monkeypatch, capsys
):
    """If a human inserts or deletes rows mid-run the indices shift, and the
    confirm batch would otherwise stamp one photograph's URL onto another's
    row. The identifier at the target row is re-read and compared before
    anything is written there."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]

    def edit_between_reserve_and_confirm(live_grid, read_count):
        # Read 1 is the initial grid read, read 2 the pre-reserve guard, read 3
        # the pre-confirm guard - so editing on 3 lands in the reserve->confirm
        # window specifically.
        if read_count == 3:
            live_grid[1][2] = "lcps-astoriaphotos-99999"

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        before_read=edit_between_reserve_and_confirm,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    # Row 2 uploaded fine; only its Sheet write-back is withheld. Row 3 is
    # untouched by the edit and is confirmed normally. The confirm batch is
    # asserted BEFORE the exit code deliberately: "it wrote row 2's URL onto
    # whatever now sits at row 2" is the failure that matters, and an
    # exit-code assertion firing first would hide which one went wrong.
    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",
    ]
    assert recorder.writes[1] == [
        ("D3", FIXED_TIMESTAMP),
        ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002"),
        ("F3", "photo2.jpg"),
    ]
    assert "row 2 is no longer the row this run planned for" in captured.err
    assert "IS on Internet Archive" in captured.err
    assert exit_code == 1

    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    logged = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    # Row records only: "record" marks a run-level line (the header, the
    # closing run summary), and a row carries no such key.
    entries = [entry for entry in logged if "record" not in entry]
    statuses = {entry["identifier"]: entry["status"] for entry in entries}
    assert statuses["lcps-astoriaphotos-00001"] == "unconfirmed"
    assert statuses["lcps-astoriaphotos-00002"] == "success"


def test_cmd_upload_writes_the_resolved_path_to_ia_identifier_bib_not_the_sheets_raw_cell(
    tmp_path, monkeypatch, capsys
):
    """225 of 234 filenames in the real Sheet carry no extension, so the
    reference that was actually uploaded differs from what the Sheet says.
    ia_identifier_bib must record what was uploaded, and a stale value already
    sitting in that column must not survive."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "Liberty", "", "", "", "STALE/whatever"]]
    captured = []
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("Liberty.tif",), captured=captured
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.writes[1][2] == ("F2", "Liberty.tif")
    assert captured[0]["row"]["identifier-bib"] == "Liberty.tif"


def test_cmd_upload_through_main_runs_against_the_sheet(
    tmp_path, monkeypatch, capsys
):
    """Driven through main() so the parser is under test, not just cmd_upload."""
    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = main(
        [
            "upload",
            "--project",
            "astoriaphotos",
            "--registry",
            str(registry_path),
            "--log-dir",
            str(tmp_path / "logs"),
        ]
    )
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]


def test_cmd_upload_never_sends_tool_owned_or_held_back_columns_as_ia_metadata(
    tmp_path, monkeypatch, capsys
):
    """upload_row turns every key it is handed into an Internet Archive
    metadata field, and IA metadata is permanent. The tool's own ia_ columns
    and anything marked (LCPS Internal) must be filtered out before it ever
    sees them; identifier-bib and mediatype are generated instead of read from
    a column. See docs/DECISIONS.md."""
    from ia_bulk import cmd_upload

    header = SHEET_HEADER + ["Donor notes (LCPS Internal)"]
    grid = [header, ["First photo", "photo1.jpg", "", "", "", "", "do not publish"]]
    captured = []
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, captured=captured
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    assert exit_code == 0
    metadata_row = captured[0]["row"]
    assert sorted(metadata_row) == ["file", "identifier-bib", "mediatype", "title"]
    assert metadata_row["identifier-bib"] == "photo1.jpg"
    assert metadata_row["mediatype"] == "image"
    # files_dir must come from the registry, not a "." default - upload_row
    # joins it to row['file'] to find the bytes it sends.
    assert captured[0]["files_dir"] == str(tmp_path)


def test_cmd_upload_refuses_a_sheet_without_the_ia_write_back_columns(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_upload

    grid = [["Title", "file"], ["First photo", "photo1.jpg"]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorder.uploads == []
    assert recorder.writes == []
    assert "ia_identifier" in captured.err
    assert "ia_uploaded" in captured.err


def test_cmd_upload_refuses_a_header_collision_before_uploading_anything(
    tmp_path, monkeypatch, capsys
):
    """A header-level defect silently overwrites one column's data on EVERY
    row, so unlike a bad row it cannot be routed around by skipping."""
    from ia_bulk import cmd_upload

    header = SHEET_HEADER + ["Title!"]
    grid = [header, ["First photo", "photo1.jpg", "", "", "", "", "collides"]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorder.uploads == []
    assert recorder.writes == []
    assert "normalize to field name 'title'" in captured.out


def test_cmd_upload_sheet_path_refuses_an_unreplaced_placeholder_sheet_id(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    registry = make_sheet_registry(files_dir=str(tmp_path), sheet_id="REPLACE_WITH_REAL_SHEET_ID")
    recorder, client, registry_path, build_calls = setup_sheet_upload(
        tmp_path, monkeypatch, grid, registry=registry
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, live=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert build_calls == []
    assert recorder.events == []
    assert "placeholder" in captured.err


def test_cmd_upload_records_a_failed_upload_without_confirming_it(tmp_path, monkeypatch, capsys):
    """A row whose upload raised is reserved (the number is spent - gaps are
    harmless, collisions are not) but never confirmed, so the next run sees it
    as RESERVED and retries under the same identifier."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        fail_for=(f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",),
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 1
    assert recorder.writes == [
        [("C2", "lcps-astoriaphotos-00001"), ("C3", "lcps-astoriaphotos-00002")],
        [
            ("D3", FIXED_TIMESTAMP),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002"),
            ("F3", "photo2.jpg"),
        ],
    ]


def test_cmd_upload_does_not_reserve_onto_a_row_that_shifted_after_the_initial_read(
    tmp_path, monkeypatch, capsys
):
    """The read->reserve window, which the confirm guard cannot cover.

    `read_grid()` fires once, then chunk N's reserve write fires after chunks
    1..N-1 have finished uploading - hours later on a full-collection run. If
    someone deletes a row in between, every row below shifts up and the reserve
    write lands on a different photograph. Checking `ia_identifier` at confirm
    time cannot catch it: reserve wrote that value at that index moments
    earlier, so the check would be verifying its own write.

    Here the human deletes row 2. Row 4 (already DONE, holding the permanent
    identifier ...-00007 and its live archive.org URL) slides up into row 3,
    which is the row this run planned to reserve ...-00008 onto. Unguarded,
    that overwrites a completed row's permanent identifier and URL and exits
    0. The fingerprint - the file_template columns, which this tool never
    writes - is what notices."""
    from ia_bulk import cmd_upload

    done_row = [
        "Done photo",
        "photo3.jpg",
        "lcps-astoriaphotos-00007",
        "2026-01-01T00:00:00",
        "https://archive.org/details/lcps-astoriaphotos-00007",
        "photo3.jpg",
    ]
    grid = [
        SHEET_HEADER,
        ["Filler photo", "photo1.jpg", "lcps-astoriaphotos-00006", "2026-01-01T00:00:00", "u", "photo1.jpg"],
        ["Photo one", "photo2.jpg", "", "", "", ""],
        list(done_row),
    ]

    def human_deletes_row_two(live_grid, read_count):
        if read_count == 2:
            del live_grid[1]

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        before_read=human_deletes_row_two,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    # The completed row is untouched: same permanent identifier, same URL.
    assert client.grid[2] == done_row
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "row 3 is no longer the row this run planned for" in captured.err
    assert exit_code == 1


def test_cmd_upload_catches_a_shift_that_an_identifier_check_alone_cannot_see(
    tmp_path, monkeypatch, capsys
):
    """The de-tautologising case, isolated.

    Deleting row 2 slides two *unassigned* rows up one. Every candidate row
    still has a blank `ia_identifier`, so a guard that only looked at that
    column would wave both through and reserve ...-00007 onto the photograph
    that was supposed to get ...-00008. Only the fingerprint - the
    file_template columns, which this tool never writes - notices that row 3
    now describes a different photograph."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["Filler", "photo1.jpg", "lcps-astoriaphotos-00006", "2026-01-01T00:00:00", "u", "photo1.jpg"],
        ["Photo one", "photo2.jpg", "", "", "", ""],
        ["Photo two", "photo3.jpg", "", "", "", ""],
    ]

    def human_deletes_row_two(live_grid, read_count):
        if read_count == 2:
            del live_grid[1]

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        before_read=human_deletes_row_two,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert recorder.writes == []
    assert recorder.uploads == []
    assert "row 3 is no longer the row this run planned for" in captured.err
    assert "row 4 is no longer the row this run planned for" in captured.err
    assert exit_code == 1


def _guard_snapshot(fingerprints, grid):
    """A SheetSnapshot shaped for split_moved_targets tests: real fingerprints
    and grid, write-back columns at the SHEET_HEADER positions."""
    from ia_bulk import SheetColumns, SheetSnapshot

    return SheetSnapshot(
        columns=SheetColumns(ia_identifier=2, ia_uploaded=3, ia_url=4, ia_identifier_bib=5),
        column_map=build_column_map(SHEET_HEADER),
        grid=grid,
        fingerprints=fingerprints,
        claimed_identifiers=frozenset(),
    )


def test_split_moved_targets_cannot_trust_a_fingerprint_two_rows_share():
    """Issue #1's guard-level half. A fingerprint proves 'still the same row'
    only while exactly one row carries it: with two rows resolving to the
    same file, a row shift leaves the SAME fingerprint sitting at the
    target's position while the physical row underneath is a different one,
    and the write lands on the wrong row. Duplicates present at the initial
    read are refused by resolve_sheet_files(); this covers the duplicate that
    APPEARS mid-run, which only the fresh read can see. A shared fingerprint
    is treated as unable to prove anything - the target is filed as moved,
    the safe direction."""
    from ia_bulk import split_moved_targets

    grid = [
        SHEET_HEADER,
        ["Photo one", "photo1.jpg", "", "", "", ""],
        ["Inserted duplicate", "photo1.jpg", "", "", "", ""],
    ]
    target = _target("lcps-astoriaphotos-00001")

    still_there, moved = split_moved_targets(
        [target], _guard_snapshot({2: "photo1.jpg", 3: "photo1.jpg"}, grid), reserved_already=False
    )

    assert still_there == []
    assert moved == [target]

    # The contrast case: the same target passes once its fingerprint is
    # unique again - proving the ambiguity check, not something else, is
    # what filed it as moved above.
    still_there, moved = split_moved_targets(
        [target], _guard_snapshot({2: "photo1.jpg", 3: "photo2.jpg"}, grid), reserved_already=False
    )
    assert still_there == [target]
    assert moved == []


def test_cmd_upload_refuses_two_rows_resolving_to_the_same_file(tmp_path, monkeypatch, capsys):
    """Issue #1's precondition, end to end: both duplicate rows are blocked
    at validation, nothing is uploaded for either, and the run exits
    non-zero. Without this refusal the run would mint two permanent
    identifiers for one photograph - and hand the mid-run-edit guard two
    rows it cannot tell apart."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Same photo again", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    # Only the untangled row 4 uploads; the duplicate pair is skipped whole.
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]
    assert recorder.writes == []
    assert "the same file as row 3" in captured.out
    assert "the same file as row 2" in captured.out
    assert exit_code == 1


def test_cmd_upload_skips_the_write_when_a_duplicate_file_row_is_inserted_mid_run(
    tmp_path, monkeypatch, capsys
):
    """Issue #1's actual failure sequence, replayed. The run plans row 2
    (photo1) and row 3 (photo2); a human then inserts a new row AT row 2
    naming photo1 again, shifting the planned rows down one. At the
    pre-reserve read, row 2 still shows a matching fingerprint and a blank
    ia_identifier - but the row underneath is the inserted one, and writing
    there records the reservation against the wrong photograph while the
    planned row stays unassigned, due to be minted AGAIN next run. The
    fingerprint is duplicated in the fresh read, so the guard must refuse to
    treat it as identity: nothing is written, nothing is uploaded."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]

    def human_inserts_a_duplicate_at_row_two(live_grid, read_count):
        if read_count == 2:
            live_grid.insert(1, ["Same photo again", "photo1.jpg", "", "", "", ""])

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        before_read=human_inserts_a_duplicate_at_row_two,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    # The inserted row must NOT have received the reservation - that write
    # landing is the misattribution this issue is about.
    assert client.grid[1] == ["Same photo again", "photo1.jpg", "", "", "", ""]
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "row 2 is no longer the row this run planned for" in captured.err
    assert "row 3 is no longer the row this run planned for" in captured.err
    assert exit_code == 1


def test_cmd_upload_still_confirms_when_a_duplicate_row_is_appended_after_the_upload(
    tmp_path, monkeypatch, capsys
):
    """The ambiguity check belongs to the reserve leg only. Here the duplicate
    arrives AFTER both items are on Internet Archive, appended at the end of
    the Sheet - an edit that shifts nothing, so both targets are still exactly
    where the run planned them, and by now each carries this run's own
    identifier (proved unique across the Sheet at reserve). Refusing the
    confirm write here would leave a live item recorded nowhere, and next
    run's duplicate refusal would block the very row that needs finishing."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]

    def human_appends_a_duplicate_before_the_confirm_read(live_grid, read_count):
        # 1 = initial, 2 = pre-reserve, 3 = pre-confirm.
        if read_count == 3:
            live_grid.append(["Same photo again", "photo1.jpg", "", "", "", ""])

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        before_read=human_appends_a_duplicate_before_the_confirm_read,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",
    ]
    # Both rows are confirmed, not just the one whose file nothing duplicated.
    assert recorder.writes[-1] == [
        ("D2", FIXED_TIMESTAMP),
        ("E2", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
        ("F2", "photo1.jpg"),
        ("D3", FIXED_TIMESTAMP),
        ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002"),
        ("F3", "photo2.jpg"),
    ]
    assert "no longer the row this run planned for" not in captured.err
    assert exit_code == 0


def test_cmd_upload_aborts_the_whole_run_when_a_column_is_inserted_mid_run(
    tmp_path, monkeypatch, capsys
):
    """Column positions are cached from the initial read too. A column
    inserted mid-run shifts every write-back column one to the right, so every
    cell this run would write lands in the wrong column - not just the wrong
    row.

    That is equally true of every remaining chunk, so the run stops rather
    than re-reading and re-reporting the whole Sheet on the way to the same
    conclusion (20 reads and 10,000 stderr lines on a full run). Two rows and
    CHUNK_SIZE 1: the sequence must stop at the first verify read.

    The message must also say a COLUMN moved - "row N is no longer the row
    this run planned for" is wrong in kind here and sends the operator to look
    at the wrong thing."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]

    def human_inserts_a_column(live_grid, read_count):
        if read_count == 2:
            for row in live_grid:
                row.insert(1, "Photographer" if row is live_grid[0] else "unknown")

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        before_read=human_inserts_a_column,
    )
    monkeypatch.setattr("ia_bulk.CHUNK_SIZE", 1)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert recorder.kinds == ["read", "read"]
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "the Sheet's columns moved" in captured.err
    assert "no longer the row this run planned for" not in captured.err
    # The filename is the only durable handle left: nothing was uploaded, so
    # the identifier exists nowhere, and the row number is what went stale.
    assert "File: 'photo1.jpg'" in captured.err
    assert "2 rows not attempted" in captured.out
    assert "log written to" in captured.out
    assert exit_code == 1


def test_cmd_upload_reports_a_sheets_read_failure_during_verify_instead_of_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """The same defect as the write-failure one, on the other half of the same
    step: the guard added two Sheets READS per chunk - roughly 80 across a
    full-collection run spanning hours - and one transient 503 among them must
    not end a run that has already created thousands of permanent Internet
    Archive items with a stack trace.

    Read 3 is the pre-confirm verify, so both items are on Internet Archive by
    the time it fails."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg"), raise_on_read=3
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    # Reserve was written; the confirm batch never was.
    assert recorder.writes == [
        [("C2", "lcps-astoriaphotos-00001"), ("C3", "lcps-astoriaphotos-00002")]
    ]
    assert "the Sheet could not be re-read" in captured.err
    assert "Sheets API returned 503" in captured.err
    assert "2 file(s) uploaded successfully" in captured.out
    assert "uploaded but NOT recorded in the Sheet" in captured.out
    assert "stderr" in captured.out
    assert "log written to" in captured.out

    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    logged = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    # Row records only: "record" marks a run-level line (the header, the
    # closing run summary), and a row carries no such key.
    entries = [entry for entry in logged if "record" not in entry]
    assert [entry["status"] for entry in entries] == [
        "success",
        "success",
        "unconfirmed",
        "unconfirmed",
    ]


def test_cmd_upload_reports_a_deleted_ia_column_instead_of_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """Renaming one of the four ia_ columns mid-run makes
    locate_write_back_columns raise inside the guard. Nothing is written, so it
    is fail-safe on data, but an unhandled MissingWriteBackColumns is still a
    bare traceback - and README promises a mismatch is *reported*."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]

    def human_renames_the_url_column(live_grid, read_count):
        if read_count == 2:
            live_grid[0][4] = "Notes"

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, before_read=human_renames_the_url_column
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert recorder.writes == []
    assert recorder.uploads == []
    assert "a column this run writes to is gone" in captured.err
    assert "ia_url" in captured.err
    assert "log written to" in captured.out


def test_cmd_upload_reserves_and_confirms_once_per_chunk_not_once_per_run(
    tmp_path, monkeypatch, capsys
):
    """Every other Sheet test runs 1-3 rows against CHUNK_SIZE = 500, so none
    of them ever reaches a second chunk - and both "hoist the reserve into one
    batch for the whole run" and "defer every confirm to the end" survive them
    untouched. The second is a shippable defect: one confirm batch of 10,000
    rows x 3 cells would likely exceed the Sheets request limit and lose the
    entire run's write-back.

    The full ordered sequence is asserted, chunk boundaries included."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        timestamps=["2026-08-19T09:00:00", "2026-08-19T11:30:00"],
    )
    monkeypatch.setattr("ia_bulk.CHUNK_SIZE", 1)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.kinds == [
        "read",
        "read", "write", "upload", "read", "write",
        "read", "write", "upload", "read", "write",
    ]
    assert recorder.writes == [
        [("C2", "lcps-astoriaphotos-00001")],
        [
            ("D2", "2026-08-19T09:00:00"),
            ("E2", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
            ("F2", "photo1.jpg"),
        ],
        [("C3", "lcps-astoriaphotos-00002")],
        [
            # Its own chunk's time, not the time chunk 1 started.
            ("D3", "2026-08-19T11:30:00"),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002"),
            ("F3", "photo2.jpg"),
        ],
    ]


def test_cmd_upload_sheet_path_computes_run_stamp_once_for_the_whole_run_not_once_per_chunk(
    tmp_path, monkeypatch, capsys
):
    """run_stamp() must be computed once per run and threaded through, never
    recomputed per row or per chunk - see docs/DECISIONS.md, "Test
    identifiers carry a per-run stamp". setup_sheet_upload's default
    run_stamp fake always returns the same FIXED_STAMP, which cannot tell
    'computed once' from 'computed twice and happened to agree' - a fake that
    returns a NEW value on every call can. With CHUNK_SIZE forced to 1, this
    run spans two chunks; if plan_upload_targets ever moved the run_stamp()
    call from upload_from_sheet (once, before chunking starts) down into a
    per-row or per-chunk position, chunk 2 would be stamped with the second
    call's value and this test's ordered-sequence assertion would catch the
    mismatch, not just note 'a stamp was present'."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )
    monkeypatch.setattr("ia_bulk.CHUNK_SIZE", 1)

    stamp_calls = []

    def fake_run_stamp():
        stamp_calls.append(len(stamp_calls))
        return f"stamp{stamp_calls[-1]}"

    monkeypatch.setattr("ia_bulk.run_stamp", fake_run_stamp)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 0
    assert stamp_calls == [0]
    assert recorder.uploads == [
        "zztest-stamp0-lcps-astoriaphotos-00001",
        "zztest-stamp0-lcps-astoriaphotos-00002",
    ]


def test_cmd_upload_treats_a_number_in_an_invalid_row_as_spent(tmp_path, monkeypatch, capsys):
    """A number that appears anywhere in the Sheet is spent, whatever the state
    of the row holding it. Minting from valid rows only would re-issue a number
    an invalid row already holds - which is the permanent collision the whole
    reserve-first design exists to prevent.

    The row's file is broken (present but resolves to nothing), not its
    title blank - a blank required_for_upload column is now a readiness
    fact rather than a validation error (see the SHEET_REQUIRED_COLUMNS
    shrink), so this test needs a different way to make the row genuinely
    INVALID while it still holds ...-00050."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        # File does not resolve, so it fails validation - but it still holds
        # ...-00050.
        ["First photo", "does-not-exist.jpg", "lcps-astoriaphotos-00050", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo2.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 1  # the invalid row was skipped
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00051"]
    assert recorder.writes[0] == [("C3", "lcps-astoriaphotos-00051")]


def test_cmd_upload_reports_a_sheets_write_failure_instead_of_a_traceback(
    tmp_path, monkeypatch, capsys
):
    """A 503, an expired token or a revoked share during the confirm write is
    an ordinary operational event. The operator still needs the summary and the
    log path - recovery is correct either way (the row stays RESERVED and the
    next run reuses the identifier), but they should not have to derive that
    from a stack trace."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, raise_on_write=2
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Sheets API returned 503" in captured.err
    assert "1 file(s) uploaded successfully" in captured.out
    assert "log written to" in captured.out

    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    logged = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    # Row records only: "record" marks a run-level line (the header, the
    # closing run summary), and a row carries no such key.
    entries = [entry for entry in logged if "record" not in entry]
    assert [entry["status"] for entry in entries] == ["success", "unconfirmed"]


def test_cmd_upload_write_failure_names_the_service_account_to_share_with(
    tmp_path, monkeypatch, capsys
):
    """The same write failure also has to tell the operator who the Sheet
    must be shared with, as Editor, so re-sharing is a one-step fix."""
    from ia_bulk import cmd_upload

    key_path = tmp_path / "key.json"
    key_path.write_text(
        json.dumps({"client_email": "sheets-sync@example.iam.gserviceaccount.com"}), encoding="utf-8"
    )
    monkeypatch.setattr("ia_bulk.google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH", key_path)

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, raise_on_write=2
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "sheets-sync@example.iam.gserviceaccount.com" in captured.err
    assert "as Editor" in captured.err


# ---------------------------------------------------------------------------
# Task 12: --limit, --chunk-size, and Internet Archive rate-limit detection.
#
# These drive the real entry point (cmd_upload -> upload_from_sheet ->
# SheetUploadRun.execute) through setup_sheet_upload()/make_upload_args().
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_exc,expected",
    [
        (lambda: UploadFailed("failed with status 503: SlowDown", status_code=503), True),
        (lambda: UploadFailed("failed with status 429: Too Many Requests", status_code=429), True),
        (lambda: UploadFailed("failed with status 404: not found", status_code=404), False),
        (lambda: RuntimeError("connection reset by peer"), False),
    ],
)
def test_is_rate_limit_error(make_exc, expected):
    """Matches only the PARSED status_code attribute UploadFailed carries -
    never str(exc) or any server-supplied text. See is_rate_limit_error()'s
    own comment and docs/DECISIONS.md, "Rate-limit detection uses a parsed
    status code, never message text" for what could and could not be
    verified about what a live rate-limit response looks like once it has
    passed through the installed `internetarchive` library."""
    assert is_rate_limit_error(make_exc()) is expected


def test_is_rate_limit_error_ignores_a_status_code_merely_mentioned_in_unrelated_response_text():
    """The exact bug review found in the string-scanning version this
    replaced: a 404 whose body happens to quote 'status 503' - a mirrored
    error, a proxied message, an echoed request - must NOT be treated as a
    rate limit. The real status_code (404) is what governs the decision;
    the arbitrary text is never consulted at all."""
    exc = UploadFailed(
        "upload of 'x' failed with status 404: mirror of upstream failure (status 503: SlowDown)",
        status_code=404,
    )
    assert is_rate_limit_error(exc) is False


def test_is_rate_limit_error_does_not_match_status_codes_that_merely_contain_429_or_503():
    """A pure substring scan over message text would also match 'status
    5031' or 'status 42900' - digits adjacent to a coincidental match.
    status_code is a parsed int, so 5031 and 503 are simply different
    integers; there is no substring for either to accidentally contain."""
    assert is_rate_limit_error(UploadFailed("boom", status_code=5031)) is False
    assert is_rate_limit_error(UploadFailed("boom", status_code=42900)) is False


def test_is_rate_limit_error_reads_the_structured_status_from_requests_httperror():
    """Verified path from tracing the installed internetarchive 5.10.1's
    Item.upload_file(): a real S3 failure surfaces as
    `requests.exceptions.HTTPError` re-raised with `response=exc.response`
    passed through unchanged, even though the message text has been rebuilt
    from the S3 XML body and no longer contains the status code anywhere.
    is_rate_limit_error() must still catch this via `.response.status_code`,
    not just via ia_bulk's own UploadFailed.status_code. Uses a real
    requests.Response (not the local FakeResponse shim) because
    HTTPError.__init__ is typed to accept `response: Response | None`."""
    response = requests.Response()
    response.status_code = 503
    exc = requests.exceptions.HTTPError(
        " error uploading photo1.jpg to lcps-astoriaphotos-00001, Please reduce your request "
        "rate. - some/resource",
        response=response,
    )
    assert is_rate_limit_error(exc) is True


# --- retry with backoff (issue #5) ------------------------------------------
#
# The classification table below is the point of the feature: a transient
# transport failure must be absorbed, and a real refusal must NOT be, because
# retrying a refusal costs the operator time and tells them nothing new.
# 429/503 are deliberately NOT retryable - they already have a stronger
# response than retrying (stop the run, resume tomorrow); see
# is_rate_limit_error() and docs/decisions/QUOTA-AND-RUNS.md.


def http_error_with_status(status_code):
    """A requests.HTTPError carrying a real, parsed status - the shape
    is_retryable_ia_error() reads for anything that is not our own
    UploadFailed. Uses a real requests.Response for the same reason
    test_is_rate_limit_error_reads_the_structured_status_from_requests_httperror
    does: HTTPError.__init__ is typed to accept `Response | None`."""
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError("boom", response=response)


@pytest.mark.parametrize(
    "make_exc, expected, why",
    [
        (lambda: requests.exceptions.ReadTimeout("read timeout=12"), True,
         "the exact failure issue #5 was filed about"),
        (lambda: requests.exceptions.ConnectTimeout("connect timed out"), True, "transport"),
        (lambda: requests.exceptions.ConnectionError("connection reset"), True, "transport"),
        (lambda: http_error_with_status(500), True, "server-side; no reason a retry repeats it"),
        (lambda: http_error_with_status(502), True, "bad gateway"),
        (lambda: http_error_with_status(504), True, "gateway timeout"),
        (lambda: UploadFailed("failed with status 500", status_code=500), True,
         "same rule via our own parsed status_code"),
        (lambda: UploadFailed("failed with status 503: SlowDown", status_code=503), False,
         "rate limit - stops the run instead, never retried here"),
        (lambda: UploadFailed("failed with status 429", status_code=429), False, "rate limit"),
        (lambda: http_error_with_status(503), False, "rate limit, via .response.status_code"),
        (lambda: http_error_with_status(403), False, "Access Denied is a real refusal"),
        (lambda: http_error_with_status(400), False, "a rejected metadata field is rejected again"),
        (lambda: UploadFailed("failed with status 404", status_code=404), False, "real refusal"),
        (lambda: ValueError("the row has no 'file' value"), False,
         "our own guard - a problem in the data, not the network"),
        (lambda: RuntimeError("returned an unprepared Request"), False, "our own guard"),
    ],
)
def test_is_retryable_ia_error(make_exc, expected, why):
    from ia_bulk import is_retryable_ia_error

    assert is_retryable_ia_error(make_exc()) is expected, why


def test_is_retryable_ia_error_treats_an_unrecognized_exception_as_a_refusal():
    """No parsed status and not a known transport failure means we cannot
    tell that repeating the call is safe, so we do not. Failing the row costs
    one re-run; repeating something unrecognized could cost more."""
    from ia_bulk import is_retryable_ia_error

    assert is_retryable_ia_error(Exception("something unrecognized")) is False


def test_retry_ia_call_returns_the_first_attempts_value_without_sleeping(monkeypatch):
    from ia_bulk import retry_ia_call

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: pytest.fail("slept on a success"))

    assert retry_ia_call(lambda: "done", "uploading photo1.jpg") == "done"


def test_retry_ia_call_absorbs_a_transient_failure_and_returns_the_retrys_value(
    monkeypatch, capsys
):
    from ia_bulk import retry_ia_call

    slept = []
    monkeypatch.setattr("ia_bulk.time.sleep", slept.append)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.exceptions.ReadTimeout("read timeout=12")
        return "done"

    assert retry_ia_call(flaky, "uploading photo1.jpg") == "done"
    assert len(attempts) == 2
    assert len(slept) == 1
    # The operator has to see that a flake was absorbed; silence here would
    # make a slow run look like a hung one.
    printed = capsys.readouterr().out
    assert "attempt 1 of 3" in printed
    assert "read timeout=12" in printed


def test_retry_ia_call_reraises_the_last_failure_after_exhausting_attempts(monkeypatch):
    """The exception that escapes must be the real one, unwrapped: the
    callers' `except Exception` branches log str(exc) and hand the object to
    is_rate_limit_error(), and both need the original."""
    from ia_bulk import retry_ia_call, RETRY_ATTEMPTS

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: None)
    attempts = []
    final = requests.exceptions.ReadTimeout("read timeout=12")

    def always_times_out():
        attempts.append(1)
        raise final

    with pytest.raises(requests.exceptions.ReadTimeout) as caught:
        retry_ia_call(always_times_out, "uploading photo1.jpg")

    assert caught.value is final
    assert len(attempts) == RETRY_ATTEMPTS


def test_retry_ia_call_does_not_retry_a_refusal(monkeypatch):
    from ia_bulk import retry_ia_call

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: pytest.fail("slept on a refusal"))
    attempts = []

    def refused():
        attempts.append(1)
        raise UploadFailed("failed with status 403: Access Denied", status_code=403)

    with pytest.raises(UploadFailed):
        retry_ia_call(refused, "uploading photo1.jpg")

    assert len(attempts) == 1


def test_retry_ia_call_does_not_retry_a_rate_limit(monkeypatch):
    """A 503 must reach the caller on the FIRST attempt, still carrying its
    status_code, so SheetUploadRun's is_rate_limit_error() branch stops the
    run exactly as promptly as it did before retry existed."""
    from ia_bulk import retry_ia_call

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: pytest.fail("slept on a rate limit"))
    attempts = []

    def rate_limited():
        attempts.append(1)
        raise UploadFailed("failed with status 503: SlowDown", status_code=503)

    with pytest.raises(UploadFailed) as caught:
        retry_ia_call(rate_limited, "uploading photo1.jpg")

    assert len(attempts) == 1
    assert is_rate_limit_error(caught.value) is True


def test_retry_delay_grows_and_never_returns_zero():
    """Equal jitter, not full jitter: every wait is at least half its
    ceiling. A backoff that can randomly pick ~0s does not actually wait out
    a slow archive.org, which is the only thing the wait is for."""
    from ia_bulk import retry_delay, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS

    first = [retry_delay(0) for _ in range(200)]
    second = [retry_delay(1) for _ in range(200)]

    assert min(first) >= RETRY_BASE_SECONDS / 2
    assert max(first) <= RETRY_BASE_SECONDS
    assert min(second) >= RETRY_BASE_SECONDS
    assert max(second) <= RETRY_BASE_SECONDS * 2
    # Jitter is real, not a constant dressed up as one.
    assert len(set(first)) > 1
    # Growth is capped rather than unbounded.
    assert retry_delay(50) <= RETRY_MAX_SECONDS


def test_upload_row_retries_a_transient_failure_from_the_library(tmp_path, monkeypatch):
    """The S3 transfer is the one IA call with no retry of any kind
    underneath it: internetarchive 5.10.1 deliberately does NOT mount its
    retrying HTTP adapter on s3.us.archive.org (session.py: "IA-S3 requires a
    more complicated retry workflow"), and upload_file()'s own `retries`
    argument defaults to 0 and only ever fires on a 503. This is the case the
    feature exists for."""
    from ia_bulk import upload_row

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: None)
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {"identifier": "lcps-astoriaphotos-00001", "file": "photo1.jpg", "mediatype": "image"}
    calls = []

    def flaky_upload(identifier, files, metadata, **kwargs):
        calls.append(identifier)
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("read timeout=12")
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", flaky_upload)

    upload_row(
        row,
        target_identifier="zztest-lcps-astoriaphotos-00001",
        collection="test_collection",
        files_dir=tmp_path,
    )

    assert len(calls) == 2


def test_upload_row_retries_a_not_ok_500_response(tmp_path, monkeypatch):
    """A failed *Response* rather than a raised exception becomes
    upload_row()'s own UploadFailed, and has to classify by the same rule."""
    from ia_bulk import upload_row

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: None)
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {"identifier": "lcps-astoriaphotos-00001", "file": "photo1.jpg", "mediatype": "image"}
    calls = []

    def flaky_upload(identifier, files, metadata, **kwargs):
        calls.append(identifier)
        if len(calls) == 1:
            return [FakeResponse(ok=False, status_code=500, text="Internal Server Error")]
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", flaky_upload)

    upload_row(
        row,
        target_identifier="zztest-lcps-astoriaphotos-00001",
        collection="test_collection",
        files_dir=tmp_path,
    )

    assert len(calls) == 2


def test_upload_row_does_not_retry_a_503(tmp_path, monkeypatch):
    """Guards the interaction retry could most easily break: 503 keeps its
    old meaning - fail this row at once so the run can stop - instead of
    being slowly retried first."""
    from ia_bulk import upload_row

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: pytest.fail("slept on a rate limit"))
    (tmp_path / "photo1.jpg").write_bytes(b"data")
    row = {"identifier": "lcps-astoriaphotos-00001", "file": "photo1.jpg", "mediatype": "image"}
    calls = []

    def rate_limited_upload(identifier, files, metadata, **kwargs):
        calls.append(identifier)
        return [FakeResponse(ok=False, status_code=503, text="SlowDown")]

    monkeypatch.setattr(internetarchive, "upload", rate_limited_upload)

    with pytest.raises(UploadFailed) as caught:
        upload_row(
            row,
            target_identifier="zztest-lcps-astoriaphotos-00001",
            collection="test_collection",
            files_dir=tmp_path,
        )

    assert len(calls) == 1
    assert is_rate_limit_error(caught.value) is True


def test_upload_row_does_not_retry_a_blank_filename(tmp_path, monkeypatch):
    """The blank-file guard is defence against sending the whole data tree
    into one permanent item. Retrying it would be pointless and would triple
    the time spent reaching the same refusal."""
    from ia_bulk import upload_row

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: pytest.fail("slept on a data error"))
    monkeypatch.setattr(
        internetarchive, "upload", lambda *a, **k: pytest.fail("reached the network")
    )

    with pytest.raises(ValueError, match="no 'file' value"):
        upload_row(
            {"identifier": "lcps-astoriaphotos-00001", "file": "  "},
            target_identifier="zztest-lcps-astoriaphotos-00001",
            collection="test_collection",
            files_dir=tmp_path,
        )


def test_update_metadata_row_retries_a_transient_failure(monkeypatch):
    from ia_bulk import update_metadata_row

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: None)
    calls = []

    def flaky_modify(identifier, metadata, **kwargs):
        calls.append(identifier)
        if len(calls) == 1:
            raise requests.exceptions.ConnectionError("connection reset by peer")
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", flaky_modify)

    update_metadata_row(
        {"identifier": "lcps-astoriaphotos-00001", "title": "New title"},
        "zztest-lcps-astoriaphotos-00001",
    )

    assert len(calls) == 2


def test_update_metadata_row_does_not_retry_metadata_unchanged(monkeypatch):
    """MetadataUnchanged is a normal, expected outcome the sync loop counts
    separately - not a failure, and not something to repeat three times."""
    from ia_bulk import update_metadata_row, MetadataUnchanged

    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: pytest.fail("slept on an unchanged row"))
    calls = []

    def unchanged(identifier, metadata, **kwargs):
        calls.append(identifier)
        return FakeResponse(
            ok=False, status_code=400, text=json.dumps({"error": "no changes to _meta.xml"})
        )

    monkeypatch.setattr(internetarchive, "modify_metadata", unchanged)

    with pytest.raises(MetadataUnchanged):
        update_metadata_row(
            {"identifier": "lcps-astoriaphotos-00001", "title": "New title"},
            "zztest-lcps-astoriaphotos-00001",
        )

    assert len(calls) == 1


# --- recovering a status the metadata call strips -----------------------------
#
# internetarchive's session.get_metadata() re-raises every failure as
# `type(exc)(error_msg)` - a fresh exception of the same class built from the
# message alone - which drops `.response` and with it the only structured
# status this codebase is willing to read. Two changes recover it, both
# verified against a local server answering real status codes rather than
# reasoned about: IA_RETRY makes a retried status arrive as a real HTTPError
# instead of an opaque RetryError, and parsed_status_code() walks the implicit
# __context__ chain that still holds the original exception.


def stripped_like_get_metadata(status_code) -> requests.exceptions.HTTPError:
    """Reproduces exactly what session.get_metadata() does to an exception:
    catches the real one and re-raises `type(exc)(error_msg)`, losing
    `.response` but leaving the original as the new exception's implicit
    __context__. Built by raising for real rather than by hand-assembling a
    chain, so the test breaks if that chaining ever stops happening."""
    response = requests.Response()
    response.status_code = status_code
    try:
        try:
            raise requests.exceptions.HTTPError(
                f"{status_code} Server Error: for url: https://archive.org/metadata/x",
                response=response,
            )
        except Exception as exc:
            raise type(exc)(f"Error retrieving metadata from https://archive.org/metadata/x, {exc}")
    except requests.exceptions.HTTPError as stripped:
        return stripped
    raise AssertionError("unreachable: the inner raise always fires")


def test_parsed_status_code_reads_a_status_the_metadata_call_stripped():
    from ia_bulk import parsed_status_code

    exc = stripped_like_get_metadata(503)

    # The premise: the exception itself really has lost it.
    assert exc.response is None
    assert parsed_status_code(exc) == 503


def test_is_rate_limit_error_catches_a_503_the_metadata_call_stripped():
    """The gap this closes. `internetarchive.upload()` fetches the item's
    metadata before transferring anything, so a rate limit can surface from
    that GET rather than from S3. Stripped of its status it read as an
    ordinary failure, and the run ground on through 500 rows of the same
    503 instead of stopping."""
    assert is_rate_limit_error(stripped_like_get_metadata(503)) is True
    assert is_rate_limit_error(stripped_like_get_metadata(429)) is True


def test_is_retryable_ia_error_reads_a_chained_status_both_ways():
    from ia_bulk import is_retryable_ia_error

    assert is_retryable_ia_error(stripped_like_get_metadata(500)) is True
    assert is_retryable_ia_error(stripped_like_get_metadata(403)) is False
    # A stripped rate limit stays non-retryable, exactly as an unstripped one
    # does - recovering the status must not quietly reclassify it.
    assert is_retryable_ia_error(stripped_like_get_metadata(503)) is False


def test_parsed_status_code_prefers_the_exceptions_own_status_over_a_chained_one():
    """An UploadFailed raised while handling something else must report its
    own status, not the older one further down the chain."""
    from ia_bulk import parsed_status_code

    try:
        raise stripped_like_get_metadata(503)
    except Exception:
        outer = UploadFailed("failed with status 403: Access Denied", status_code=403)

    outer.__context__ = stripped_like_get_metadata(503)
    assert parsed_status_code(outer) == 403


def test_parsed_status_code_returns_none_when_nothing_in_the_chain_has_a_status():
    from ia_bulk import parsed_status_code

    try:
        try:
            raise requests.exceptions.ReadTimeout("read timeout=12")
        except Exception as exc:
            raise type(exc)(f"Error retrieving metadata from https://archive.org/metadata/x, {exc}")
    except Exception as stripped:
        assert parsed_status_code(stripped) is None


def test_parsed_status_code_terminates_on_a_self_referential_chain():
    """A cycle in __context__ is possible - CPython only breaks the cycle it
    can see when setting the context, and one can also be assigned by hand.
    Walking it without a guard would hang the run rather than fail a row."""
    from ia_bulk import parsed_status_code

    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first

    assert parsed_status_code(first) is None


def test_ia_retry_matches_the_librarys_own_policy_except_for_raise_on_status():
    """IA_RETRY replaces internetarchive's default adapter policy, so it must
    match it in every respect but the one being changed - otherwise this
    silently alters how often and on what the library retries. Compared
    against a session the library built itself, so the test fails if a future
    version changes those defaults out from under us."""
    from ia_bulk import IA_RETRY

    library_adapter = internetarchive.get_session().get_adapter("https://archive.org/metadata/x")
    assert isinstance(library_adapter, HTTPAdapter)
    library_default = library_adapter.max_retries

    assert library_default.raise_on_status is True
    assert IA_RETRY.raise_on_status is False

    assert IA_RETRY.total == library_default.total
    assert IA_RETRY.connect == library_default.connect
    assert IA_RETRY.read == library_default.read
    assert IA_RETRY.status_forcelist == library_default.status_forcelist
    assert IA_RETRY.backoff_factor == library_default.backoff_factor
    assert IA_RETRY.allowed_methods is not None
    assert library_default.allowed_methods is not None
    assert set(IA_RETRY.allowed_methods) == set(library_default.allowed_methods)
    assert IA_RETRY.respect_retry_after_header == library_default.respect_retry_after_header


def test_upload_row_asks_for_the_status_preserving_adapter(tmp_path, monkeypatch):
    from ia_bulk import upload_row, IA_RETRY

    (tmp_path / "photo1.jpg").write_bytes(b"data")
    captured = {}

    def fake_upload(identifier, files, metadata, **kwargs):
        captured.update(kwargs)
        return [FakeResponse(ok=True)]

    monkeypatch.setattr(internetarchive, "upload", fake_upload)

    upload_row(
        {"identifier": "lcps-astoriaphotos-00001", "file": "photo1.jpg", "mediatype": "image"},
        target_identifier="zztest-lcps-astoriaphotos-00001",
        collection="test_collection",
        files_dir=tmp_path,
    )

    assert captured["http_adapter_kwargs"] == {"max_retries": IA_RETRY}


def test_update_metadata_row_asks_for_the_status_preserving_adapter(monkeypatch):
    from ia_bulk import update_metadata_row, IA_RETRY

    captured = {}

    def fake_modify(identifier, metadata, **kwargs):
        captured.update(kwargs)
        return FakeResponse(ok=True)

    monkeypatch.setattr(internetarchive, "modify_metadata", fake_modify)

    update_metadata_row({"identifier": "x", "title": "New title"}, "zztest-x")

    assert captured["http_adapter_kwargs"] == {"max_retries": IA_RETRY}


def test_fetch_current_metadata_asks_for_the_status_preserving_adapter(monkeypatch):
    """The dry run is not retried, but it reads the same endpoint - and a
    session built with a different policy is a second, divergent way of
    talking to Internet Archive."""
    from ia_bulk import fetch_current_metadata, IA_RETRY

    captured = {}

    class FakeItem:
        metadata = {"title": "Existing"}

    def fake_get_item(identifier, **kwargs):
        captured.update(kwargs)
        return FakeItem()

    monkeypatch.setattr(internetarchive, "get_item", fake_get_item)

    assert fetch_current_metadata("zztest-x") == {"title": "Existing"}
    assert captured["http_adapter_kwargs"] == {"max_retries": IA_RETRY}


# --- a hostile Retry-After cannot stall a run ---------------------------------


def response_with_retry_after(value):
    """A urllib3 response carrying (or not carrying) a Retry-After header -
    the object Retry.get_retry_after() is handed."""
    headers = {} if value is None else {"Retry-After": value}
    return urllib3.HTTPResponse(headers=headers)


def test_ia_retry_caps_a_hostile_retry_after():
    """urllib3 honours Retry-After by calling time.sleep() on it UNCAPPED -
    DEFAULT_BACKOFF_MAX bounds the exponential path only, not this one. A
    `Retry-After: 3600` would therefore sleep an hour inside a single call,
    three times over, and the run would look hung. The header is still
    honoured, just bounded."""
    from ia_bulk import IA_RETRY, RETRY_AFTER_MAX_SECONDS

    assert IA_RETRY.get_retry_after(response_with_retry_after("3600")) == RETRY_AFTER_MAX_SECONDS


def test_ia_retry_honours_a_reasonable_retry_after_unchanged():
    from ia_bulk import IA_RETRY

    assert IA_RETRY.get_retry_after(response_with_retry_after("5")) == 5


def test_ia_retry_passes_through_an_absent_retry_after():
    from ia_bulk import IA_RETRY

    assert IA_RETRY.get_retry_after(response_with_retry_after(None)) is None


def test_ia_retry_keeps_its_cap_through_urllib3s_own_countdown():
    """urllib3 does not reuse the Retry object it is given - it counts down by
    calling increment(), which builds a fresh copy through new(). A cap that
    lived only on the original instance would silently vanish on the very
    first retry, which is the only time it matters."""
    from ia_bulk import IA_RETRY, RETRY_AFTER_MAX_SECONDS

    counted_down = IA_RETRY.increment(method="GET", url="/metadata/x")

    assert isinstance(counted_down, type(IA_RETRY))
    assert counted_down.get_retry_after(response_with_retry_after("3600")) == RETRY_AFTER_MAX_SECONDS


# --- the S3 leg, exercised rather than reasoned about -------------------------
#
# s3.us.archive.org is hardcoded in internetarchive's item.py, so a local
# server cannot stand in for it. A requests transport adapter mounted on that
# host can. These tests drive the REAL Item.upload_file() - the code the whole
# retry feature exists for - with no network, by mounting one canned adapter on
# archive.org (so the metadata step succeeds and the S3 leg is actually
# reached) and one fault-injecting adapter on s3.us.archive.org.


ITEM_METADATA_DOCUMENT = {
    "created": 1,
    "d1": "ia600000.us.archive.org",
    "d2": "ia800000.us.archive.org",
    "dir": "/0/items/some-identifier",
    "files": [],
    "item_size": 0,
    "metadata": {"identifier": "some-identifier", "mediatype": "image"},
    "server": "ia600000.us.archive.org",
    "uniq": 1,
    "workable_servers": ["ia600000.us.archive.org"],
}

S3_SLOWDOWN_XML = (
    b"<?xml version='1.0' encoding='UTF-8'?><Error><Code>SlowDown</Code>"
    b"<Message>Please reduce your request rate.</Message>"
    b"<Resource>some/resource</Resource></Error>"
)
S3_ACCESS_DENIED_XML = (
    b"<?xml version='1.0' encoding='UTF-8'?><Error><Code>AccessDenied</Code>"
    b"<Message>Access Denied</Message><Resource>some/resource</Resource></Error>"
)


def _canned(request, status_code, body, content_type):
    response = requests.Response()
    response.status_code = status_code
    response.raw = io.BytesIO(body)
    response.headers["Content-Type"] = content_type
    response.url = request.url or ""
    response.request = request
    return response


class CannedMetadataAdapter(HTTPAdapter):
    """archive.org answers a real, minimal item-metadata document, so the
    upload reaches S3 instead of failing before it."""

    def send(self, request, *args, **kwargs):
        return _canned(request, 200, json.dumps(ITEM_METADATA_DOCUMENT).encode(), "application/json")


class FaultInjectingS3Adapter(HTTPAdapter):
    """s3.us.archive.org answers whatever failure the test asks for, and
    counts the attempts so a test can prove a retry did or did not happen."""

    def __init__(self, fault):
        super().__init__()
        self.fault = fault
        self.calls = []

    def send(self, request, *args, **kwargs):
        self.calls.append(request.url)
        if isinstance(self.fault, Exception):
            raise self.fault
        status_code, body = self.fault
        return _canned(request, status_code, body, "text/xml")


def upload_row_against_s3_fault(fault, tmp_path, monkeypatch):
    """Runs the real upload_row() against the real internetarchive library,
    with S3 replaced by a fault-injecting adapter. Returns that adapter so the
    caller can count attempts.

    internetarchive.upload() is wrapped rather than replaced: the wrapper adds
    the prepared session and then calls the real function, so upload_row's own
    metadata building, response checking and retry all run for real.
    """
    session = internetarchive.session.ArchiveSession()
    # upload() refuses to send without credentials; these never leave the
    # process, since no adapter here opens a socket.
    session.access_key = "fake-access-key"
    session.secret_key = "fake-secret-key"
    s3_adapter = FaultInjectingS3Adapter(fault)
    session.mount("https://archive.org", CannedMetadataAdapter())
    session.mount("https://s3.us.archive.org", s3_adapter)

    real_upload = internetarchive.upload

    def upload_through_the_prepared_session(identifier, **kwargs):
        # The prepared session already carries the adapters under test, so the
        # adapter policy argument would be ignored anyway; dropping it keeps
        # get_item from being handed two ways to build a session.
        kwargs.pop("http_adapter_kwargs", None)
        return real_upload(identifier, archive_session=session, **kwargs)

    monkeypatch.setattr(internetarchive, "upload", upload_through_the_prepared_session)
    monkeypatch.setattr("ia_bulk.time.sleep", lambda _: None)

    (tmp_path / "photo1.jpg").write_bytes(b"pretend-jpeg-bytes")
    return s3_adapter


def run_upload_row(tmp_path):
    from ia_bulk import upload_row

    upload_row(
        {"identifier": "lcps-astoriaphotos-00001", "file": "photo1.jpg", "mediatype": "image"},
        target_identifier="some-identifier",
        collection="test_collection",
        files_dir=tmp_path,
    )


def test_upload_row_succeeds_against_the_s3_harness_when_no_fault_is_injected(tmp_path, monkeypatch):
    """The control. Without it every test below could pass because the harness
    is broken in a way that always fails, rather than because the classifier
    works."""
    s3 = upload_row_against_s3_fault((200, b""), tmp_path, monkeypatch)

    run_upload_row(tmp_path)

    assert len(s3.calls) == 1
    assert any("s3.us.archive.org" in url for url in s3.calls)


def test_upload_row_reads_a_real_s3_slowdown_as_a_rate_limit(tmp_path, monkeypatch):
    """Until now this was reasoned about from the library's source: that a
    real S3 failure re-raises HTTPError with `response=exc.response` passed
    through, so the parsed status survives even though the message is rebuilt
    from the XML body and loses it. Exercised here instead - and the 503 must
    NOT be retried, because a rate limit stops the run."""
    s3 = upload_row_against_s3_fault((503, S3_SLOWDOWN_XML), tmp_path, monkeypatch)

    with pytest.raises(Exception) as caught:
        run_upload_row(tmp_path)

    assert len(s3.calls) == 1
    assert is_rate_limit_error(caught.value) is True
    # The rebuilt message really has lost the status - the classifier is not
    # quietly succeeding by reading text.
    assert "503" not in str(caught.value)


def test_upload_row_retries_a_real_s3_read_timeout(tmp_path, monkeypatch):
    """The scenario the feature was filed for, driven through the real
    library: a slow S3 transfer times out and the row is retried rather than
    failed."""
    from ia_bulk import RETRY_ATTEMPTS

    s3 = upload_row_against_s3_fault(
        requests.exceptions.ReadTimeout("read timeout=12"), tmp_path, monkeypatch
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        run_upload_row(tmp_path)

    assert len(s3.calls) == RETRY_ATTEMPTS


def test_upload_row_retries_a_real_s3_500(tmp_path, monkeypatch):
    from ia_bulk import RETRY_ATTEMPTS

    s3 = upload_row_against_s3_fault(
        (500, b"<Error><Code>InternalError</Code><Message>oops</Message></Error>"),
        tmp_path,
        monkeypatch,
    )

    with pytest.raises(Exception):
        run_upload_row(tmp_path)

    assert len(s3.calls) == RETRY_ATTEMPTS


def test_upload_row_does_not_retry_a_real_s3_access_denied(tmp_path, monkeypatch):
    """A refusal costs one attempt, not three. Access Denied is what a
    misconfigured collection or a revoked key looks like, and no amount of
    waiting changes it."""
    s3 = upload_row_against_s3_fault((403, S3_ACCESS_DENIED_XML), tmp_path, monkeypatch)

    with pytest.raises(Exception) as caught:
        run_upload_row(tmp_path)

    assert len(s3.calls) == 1
    assert is_rate_limit_error(caught.value) is False


def test_cmd_upload_limit_stops_after_n_planned_targets(tmp_path, monkeypatch, capsys):
    """--limit counts PLANNED upload targets (valid, ready, not already
    done) - not Sheet rows scanned. Five ready, unassigned rows with
    --limit 2 must upload exactly the first two, in row order, and leave the
    rest untouched and unmarked."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in range(1, 6)
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=tuple(f"photo{n}.jpg" for n in range(1, 6)),
    )

    exit_code = cmd_upload(
        make_upload_args(tmp_path, registry_path, write_identifier=True, limit=2)
    )
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",
    ]
    # Rows 3-5 were never reserved: the Sheet cells for them are untouched.
    assert client.grid[3] == ["Photo 3", "photo3.jpg", "", "", "", ""]
    assert client.grid[4] == ["Photo 4", "photo4.jpg", "", "", "", ""]
    assert client.grid[5] == ["Photo 5", "photo5.jpg", "", "", "", ""]


def test_cmd_upload_stops_the_run_on_a_rate_limit_instead_of_grinding_through_failures(
    tmp_path, monkeypatch, capsys
):
    """Without this the run would attempt all 5 rows against a server that
    has already said 'slow down', reporting 3 more unexplained failures and
    burning through however much of today's 5,000-item quota happens to
    remain."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in range(1, 6)
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=tuple(f"photo{n}.jpg" for n in range(1, 6)),
    )

    def fake_upload_row(row, target_identifier, collection, files_dir):
        recorder.events.append(("upload", target_identifier))
        if target_identifier.endswith("00003"):
            raise UploadFailed(
                f"upload of '{target_identifier}' failed with status 503: SlowDown",
                status_code=503,
            )

    monkeypatch.setattr("ia_bulk.upload_row", fake_upload_row)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-0000{n}" for n in (1, 2, 3)
    ]
    assert (
        "stopped: Internet Archive reported a rate limit after 3 items"
        in captured.err.splitlines()
    )
    assert "2 uploaded this run - resume by re-running tomorrow" in captured.out.splitlines()
    assert exit_code == 1


def test_cmd_upload_confirms_successes_that_happened_before_a_rate_limit_stopped_the_run(
    tmp_path, monkeypatch, capsys
):
    """A rate-limit stop must not leave the rows that DID upload stuck
    RESERVED-but-unconfirmed - see SheetUploadRun.execute's own reserve ->
    upload -> confirm protocol. Skipping this would make tomorrow's run
    re-upload photo1.jpg and photo2.jpg under a second identifier, because
    their ia_uploaded cell would still read blank."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in range(1, 6)
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=tuple(f"photo{n}.jpg" for n in range(1, 6)),
    )

    def fake_upload_row(row, target_identifier, collection, files_dir):
        recorder.events.append(("upload", target_identifier))
        if target_identifier.endswith("00003"):
            raise UploadFailed(
                f"upload of '{target_identifier}' failed with status 503: SlowDown",
                status_code=503,
            )

    monkeypatch.setattr("ia_bulk.upload_row", fake_upload_row)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    assert exit_code == 1
    assert recorder.writes == [
        [
            ("C2", "lcps-astoriaphotos-00001"),
            ("C3", "lcps-astoriaphotos-00002"),
            ("C4", "lcps-astoriaphotos-00003"),
            ("C5", "lcps-astoriaphotos-00004"),
            ("C6", "lcps-astoriaphotos-00005"),
        ],
        [
            ("D2", FIXED_TIMESTAMP),
            ("E2", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"),
            ("F2", "photo1.jpg"),
            ("D3", FIXED_TIMESTAMP),
            ("E3", f"https://archive.org/details/zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002"),
            ("F3", "photo2.jpg"),
        ],
    ]


def test_cmd_upload_limit_and_chunk_size_combine_as_total_then_batch_size(
    tmp_path, monkeypatch, capsys
):
    """--limit 10 --chunk-size 3 must mean '10 rows total, in chunks of 3' -
    not any other reading of the two together. 12 ready rows exercise both:
    only the first 10 are ever reserved or uploaded, arriving in reserve
    batches of 3, 3, 3, then 1."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in range(1, 13)
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=tuple(f"photo{n}.jpg" for n in range(1, 13)),
    )

    exit_code = cmd_upload(
        make_upload_args(
            tmp_path, registry_path, write_identifier=True, limit=10, chunk_size=3
        )
    )
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-{n:05d}" for n in range(1, 11)
    ]
    reserve_batch_sizes = [len(batch) for batch in recorder.writes[0::2]]
    assert reserve_batch_sizes == [3, 3, 3, 1]


def test_cmd_upload_limit_counts_planned_targets_on_a_mixed_sheet(tmp_path, monkeypatch, capsys):
    """The uniformly-ready grid in the test above cannot tell 'slices
    plan_upload_targets()'s OUTPUT' apart from 'slices raw Sheet rows before
    readiness-filtering' - both implementations upload the same first two
    identifiers there. This Sheet interleaves not-ready rows (blank title)
    among the ready ones so the two implementations predict DIFFERENT
    uploads: a raw-row slice would see only rows 2-3 (Photo 1, then a
    blank-title row that never gets planned at all) and upload just Photo 1;
    slicing plan_upload_targets()'s own output - the four READY rows, in
    order - uploads Photo 1 and Photo 3."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        ["Photo 1", "photo1.jpg", "", "", "", ""],
        ["", "photo2.jpg", "", "", "", ""],  # not-ready: blank title
        ["Photo 3", "photo3.jpg", "", "", "", ""],
        ["", "photo4.jpg", "", "", "", ""],  # not-ready: blank title
        ["Photo 5", "photo5.jpg", "", "", "", ""],
        ["", "photo6.jpg", "", "", "", ""],  # not-ready: blank title
        ["Photo 7", "photo7.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=tuple(f"photo{n}.jpg" for n in range(1, 8)),
    )

    exit_code = cmd_upload(
        make_upload_args(tmp_path, registry_path, write_identifier=True, limit=2)
    )
    capsys.readouterr()

    # not_ready rows never affect the exit code (only blocked/failure/
    # unconfirmed/not_attempted do), so a clean run with 3 not-yet-catalogued
    # rows still exits 0.
    assert exit_code == 0
    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",
    ]
    # Photo 5 (row 6) and Photo 7 (row 8) were never reserved.
    assert client.grid[5] == ["Photo 5", "photo5.jpg", "", "", "", ""]
    assert client.grid[7] == ["Photo 7", "photo7.jpg", "", "", "", ""]


@pytest.mark.parametrize("limit", [0, -1])
def test_cmd_upload_rejects_a_non_positive_limit(tmp_path, monkeypatch, capsys, limit):
    """0 or a negative --limit would slice plan_upload_targets()'s output
    down to nothing (or, for the raw-Python-slicing sense of a negative
    index, something else entirely) and let the run report success having
    uploaded nothing - silently doing the wrong thing rather than failing
    loudly. Checked before any Sheet I/O: recorder.kinds stays empty."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["Photo 1", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, limit=limit))
    err = capsys.readouterr().err

    assert exit_code == 1
    assert recorder.kinds == []
    assert err.splitlines() == [
        f"--limit must be a positive number of items, not {limit}. A run with nothing to "
        "upload is what dropping --limit already means - drop it instead of passing zero "
        "or a negative number."
    ]


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_cmd_upload_rejects_a_non_positive_chunk_size(tmp_path, monkeypatch, capsys, chunk_size):
    """0 raises ValueError inside chunk_rows() (range() forbids a zero
    step) - a bare traceback in place of the run summary. -1 is worse:
    chunk_rows()'s range(0, len(rows), -1) yields no chunks at all, so the
    run silently uploads nothing and still reports success. Both are
    rejected up front instead, before any Sheet I/O."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["Photo 1", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, chunk_size=chunk_size))
    err = capsys.readouterr().err

    assert exit_code == 1
    assert recorder.kinds == []
    assert err.splitlines() == [
        f"--chunk-size must be a positive number of items, not {chunk_size}. Zero raises "
        "inside chunk_rows(); a negative value silently produces zero chunks, uploading "
        "nothing while the run still reports success."
    ]


def test_run_header_records_limit_and_chunk_size_when_set(tmp_path, monkeypatch, capsys):
    """Task 11's header exists to make a run reconstructable; a run that
    stopped at --limit, or used a non-default --chunk-size, is not
    reconstructable without both numbers recorded alongside everything else
    the header already captures."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in range(1, 4)
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg", "photo3.jpg")
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, limit=2, chunk_size=1))
    capsys.readouterr()

    assert exit_code == 0
    log_files = list((tmp_path / "logs").glob("upload-*.jsonl"))
    header = json.loads(log_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert header["limit"] == 2
    assert header["chunk_size"] == 1


def test_cmd_upload_prints_the_field_receipt_before_uploading(tmp_path, monkeypatch, capsys):
    """`upload` is where something permanent happens, so it must show the same
    receipt `validate` does rather than assume the operator ran validate first
    and remembers what it said."""
    from ia_bulk import cmd_upload

    header = SHEET_HEADER + ["Donor notes (LCPS Internal)", "Identifier"]
    grid = [header, ["First photo", "photo1.jpg", "", "", "", "", "private", "CD 1 01"]]
    recorder, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "will upload these metadata fields:\n  title\n" in out
    assert "NOT uploaded - Internet Archive reserves these names:\n  identifier" in out
    assert "held back (LCPS Internal):\n  Donor notes (LCPS Internal)" in out


def test_row_validation_is_ready_when_no_fields_missing():
    result = RowValidation(row_number=2, identifier="")
    assert result.readiness is Readiness.READY
    assert result.missing_fields == []


def test_row_validation_is_not_ready_when_fields_missing():
    result = RowValidation(row_number=2, identifier="", missing_fields=["title"])
    assert result.readiness is Readiness.NOT_READY


def test_readiness_is_independent_of_validity():
    """The combination that motivates the whole design: a row nobody has
    catalogued yet whose filename is also wrong."""
    result = RowValidation(
        row_number=2,
        identifier="",
        errors=["no file found in '/data' matching 'Finnis.jpg'"],
        missing_fields=["title"],
    )
    assert result.readiness is Readiness.NOT_READY
    assert result.is_valid is False


def test_required_for_upload_naming_a_missing_column_is_an_error():
    column_map = build_column_map(["Title", "Theme", "File Name"])
    config = _sheet_config(required_for_upload=("titel",))
    errors = check_required_for_upload(config, column_map)
    assert errors == [
        "required_for_upload names 'titel', which is not a column in this Sheet. "
        "Known columns: file_name, theme, title"
    ]


def test_required_for_upload_matching_every_column_passes():
    column_map = build_column_map(["Title", "Theme"])
    config = _sheet_config(required_for_upload=("title", "theme"))
    assert check_required_for_upload(config, column_map) == []


def test_every_missing_name_is_reported_not_just_the_first():
    column_map = build_column_map(["Title"])
    config = _sheet_config(required_for_upload=("titel", "thmee"))
    errors = check_required_for_upload(config, column_map)
    assert len(errors) == 2


def _unresolved_message(directory: Path, name: str) -> str:
    """resolve_file()'s own wording, rebuilt here so the tests below can
    assert the message character-for-character instead of settling for a
    substring. The message embeds an absolute path that varies with
    tmp_path, which is the only reason it has to be interpolated rather
    than written out literally - `.resolve()` because that is what
    resolve_file() itself formats into the message."""
    return (
        f"no file found in '{directory.resolve()}' matching '{name}' (looked for an "
        "exact filename match, then a case-insensitive match ignoring extension)"
    )


def test_a_blank_candidate_is_recorded_as_blank_not_as_an_error(tmp_path):
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "", "name": ""}]
    outcomes = resolve_sheet_files(rows, config)
    assert outcomes.errors == {}
    assert outcomes.blank == {2: ["folder", "name"]}


def test_a_blank_filename_never_reaches_the_resolver(tmp_path):
    """The cryptic "matching ''" message must be unreachable, not merely
    unlikely. Folder present and filename blank is the shape that produced
    it: pre-change the candidate reached resolve_file() and it complained
    about matching '', so this asserts errors is EMPTY rather than asserting
    over an empty collection, which would pass unconditionally.

    The folder has to exist for this to test what it claims - without the
    mkdir the resolver would fail on the missing folder before it ever got
    as far as matching a name, and the test would pass for the wrong
    reason."""
    (tmp_path / "SOP CD 1").mkdir()
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "SOP CD 1", "name": ""}]
    outcomes = resolve_sheet_files(rows, config)
    assert outcomes.errors == {}
    assert outcomes.blank == {2: ["name"]}


def test_a_blank_folder_beside_a_filled_filename_is_still_not_ready(tmp_path):
    """A half-catalogued row: the operator named a file but not its folder.
    It routes to `blank` naming only the folder cell, so the report can say
    which cell is missing rather than lumping it in with untouched rows -
    and NOT to `errors`, because a blank cell is not a wrong answer."""
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "", "name": "Finnis Meat Market.jpg"}]
    outcomes = resolve_sheet_files(rows, config)
    assert outcomes.blank == {2: ["folder"]}
    assert outcomes.errors == {}


def test_a_present_candidate_that_does_not_resolve_is_an_error_not_blank(tmp_path):
    """THE reclassification guard. A broken row silently downgraded to
    not-ready is a row nobody ever fixes."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "SOP CD 1", "name": "Finnis Meat Market.jpg"}]
    outcomes = resolve_sheet_files(rows, config)
    assert outcomes.blank == {}
    assert 2 in outcomes.errors
    assert outcomes.errors[2] == _unresolved_message(folder, "Finnis Meat Market.jpg")


def test_a_broken_filename_stays_an_error_even_beside_a_genuinely_blank_row(tmp_path):
    """Both errors here are real rows from a rehearsal against the actual
    drive: 'Finnis' for 'Finnish' (a typo in the Sheet) and "Roy's" for
    'Roy_s' (the apostrophe sanitized away when the files were copied).
    Each names a file the operator believes exists, so each is a defect to
    fix - and neither may be filed as "nobody has got to this row yet"
    just because the blank row 2 in the same run legitimately is.

    Run together on purpose: the reclassification failure is a routing
    bug, and routing is only observable when both destinations are in
    play at once. Exact dict equality on BOTH buckets, so a row leaking
    from one to the other fails the assertion from either side."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    (folder / "Roy_s Shell.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [
        {"folder": "", "name": ""},
        {"folder": "SOP CD 1", "name": "Finnis Meat Market.jpg"},
        {"folder": "SOP CD 1", "name": "Roy's Shell.jpg"},
    ]

    outcomes = resolve_sheet_files(rows, config)

    assert outcomes.blank == {2: ["folder", "name"]}
    assert outcomes.errors == {
        3: _unresolved_message(folder, "Finnis Meat Market.jpg"),
        4: _unresolved_message(folder, "Roy's Shell.jpg"),
    }
    # The unverified candidate must not survive as row['file'] for ANY
    # failing row, blank or broken - left in place it would resolve as a
    # literal path for the later disk check and mask the failure.
    assert [row["file"] for row in rows] == ["", "", ""]


def _duplicate_claim_message(resolved: str, other_rows: list[int]) -> str:
    """resolve_sheet_files()'s own wording for a duplicate-file row, rebuilt
    here so the tests below assert it character-for-character - same rule as
    _unresolved_message()."""
    label = "row" if len(other_rows) == 1 else "rows"
    listed = ", ".join(str(n) for n in other_rows)
    return (
        f"resolves to '{resolved}' - the same file as {label} {listed}. Two rows cannot "
        "claim one photograph: delete the duplicate row, or point it at the right file"
    )


def test_two_rows_resolving_to_the_same_file_are_both_errors(tmp_path):
    """Issue #1's precondition, refused at the source. Two rows claiming one
    file is how the mid-run-edit guard's fingerprint stops proving identity -
    and, guard aside, it is how one photograph gets two permanent
    identifiers. BOTH rows are flagged, not just the second: the tool cannot
    know which of the two is the wrong one, and flagging one of them silently
    elects the other as correct.

    The clean row 4 must keep its resolution: a duplicate pair is those two
    rows' problem, not the run's."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    (folder / "Roy_s Shell.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [
        {"folder": "SOP CD 1", "name": "Finnish Meat Market.jpg"},
        {"folder": "SOP CD 1", "name": "Finnish Meat Market.jpg"},
        {"folder": "SOP CD 1", "name": "Roy_s Shell.jpg"},
    ]

    outcomes = resolve_sheet_files(rows, config)

    assert outcomes.blank == {}
    assert outcomes.errors == {
        2: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [3]),
        3: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [2]),
    }
    # Same invariant the other error paths keep: a row filed in `errors`
    # carries no 'file' value that later disk checks or an upload could
    # coincidentally use - and no resolved bib either, since that value is
    # only ever meant to record what actually uploaded.
    assert [row["file"] for row in rows] == ["", "", "SOP CD 1/Roy_s Shell.jpg"]
    assert [row.get("ia_identifier_bib", "") for row in rows[:2]] == ["", ""]


def test_duplicate_file_detection_sees_through_spelling_differences(tmp_path):
    """The resolver is deliberately forgiving - case-insensitive, extension
    optional - so two rows can spell the SAME disk file differently and a
    check on the raw cells would miss them. The claim is keyed on what the
    rows RESOLVE to, the same claim_key() the reconcile survey uses, so
    forgiveness in the resolver cannot reopen the duplicate hole."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [
        {"folder": "SOP CD 1", "name": "Finnish Meat Market.jpg"},
        {"folder": "SOP CD 1", "name": "finnish meat market"},
    ]

    outcomes = resolve_sheet_files(rows, config)

    assert outcomes.errors == {
        2: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [3]),
        3: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [2]),
    }
    assert [row["file"] for row in rows] == ["", ""]


def test_three_rows_claiming_one_file_each_name_both_of_the_others(tmp_path):
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [{"folder": "SOP CD 1", "name": "Finnish Meat Market.jpg"} for _ in range(3)]

    outcomes = resolve_sheet_files(rows, config)

    assert outcomes.errors == {
        2: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [3, 4]),
        3: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [2, 4]),
        4: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [2, 3]),
    }


def test_an_already_uploaded_duplicate_row_is_named_as_the_one_to_keep(tmp_path):
    """When exactly one claimant has already uploaded, "the tool cannot know
    which row is the wrong one" no longer holds. That row's identifier is
    permanent and its Sheet row is the only thing tying the identifier to its
    metadata - including the ia_url `sync-metadata` reads to find its targets
    - so the symmetrical "delete the duplicate row" wording was inviting the
    one deletion that loses something irreplaceable. Both rows are still
    errors; only the remedy differs."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [
        {
            "folder": "SOP CD 1",
            "name": "Finnish Meat Market.jpg",
            "ia_identifier": "lcps-astoriaphotos-00001",
            "ia_uploaded": FIXED_TIMESTAMP,
            "ia_url": "https://archive.org/details/lcps-astoriaphotos-00001",
        },
        {"folder": "SOP CD 1", "name": "Finnish Meat Market.jpg"},
    ]

    outcomes = resolve_sheet_files(rows, config)

    assert outcomes.errors == {
        2: (
            "resolves to 'SOP CD 1/Finnish Meat Market.jpg' - the same file as row 3. Two "
            "rows cannot claim one photograph: this row has already uploaded, so it is the "
            "one to keep - fix or delete row 3 instead"
        ),
        3: (
            "resolves to 'SOP CD 1/Finnish Meat Market.jpg' - the same file as row 2. Two "
            "rows cannot claim one photograph: row 2 has already uploaded and must be kept, "
            "so delete this row, or point it at the right file"
        ),
    }


def test_two_already_uploaded_duplicate_rows_get_the_symmetrical_wording(tmp_path):
    """One photograph already holding two permanent identifiers is a worse
    problem than this function can adjudicate - neither row can be deleted
    without losing an identifier's only record - so it falls back to the
    symmetrical wording rather than electing one of them as the keeper."""
    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"")
    config = _sheet_config(files_dir=str(tmp_path), file_template="{folder}/{name}")
    rows = [
        {
            "folder": "SOP CD 1",
            "name": "Finnish Meat Market.jpg",
            "ia_identifier": f"lcps-astoriaphotos-0000{number}",
            "ia_uploaded": FIXED_TIMESTAMP,
        }
        for number in (1, 2)
    ]

    outcomes = resolve_sheet_files(rows, config)

    assert outcomes.errors == {
        2: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [3]),
        3: _duplicate_claim_message("SOP CD 1/Finnish Meat Market.jpg", [2]),
    }


def _sheet_row(**cells: str) -> dict[str, str]:
    """One Sheet row for the readiness tests below. `folder`/`name` are
    short aliases for the two file_template columns _validate's on-disk
    layout below uses (folder_on_lacie_drive/file_name - the real names
    from projects_registry.json), so a test reads as "which cell is
    missing or wrong" rather than repeating the long column names
    throughout. mediatype is always present: SHEET_REQUIRED_COLUMNS still
    checks it structurally, and these tests are about
    required_for_upload/missing_fields, not that check."""
    folder = cells.pop("folder", "SOP CD1")
    name = cells.pop("name", "Finnish Meat Market.jpg")
    row = {
        "mediatype": "image",
        "title": "A Title",
        "theme": "Townscape",
        "folder_on_lacie_drive": folder,
        "file_name": name,
    }
    row.update(cells)
    return row


def _validate(
    rows: list[dict[str, str]], required_for_upload: tuple[str, ...], tmp_path: Path
) -> tuple[list[RowValidation], list[RowValidation]]:
    """Wires resolve_sheet_files into validate_sheet_grid exactly as
    cmd_validate's own two call sites do - so these tests exercise the
    same path FileOutcomes actually travels, not a hand-assembled
    substitute for it.

    Takes the calling test's own tmp_path rather than managing a hidden
    directory of its own: a test asserting the resolver's exact message
    (via _unresolved_message(), per this plan's exact-assertion rule)
    needs a directory it can name back, which a directory this helper
    created and never exposed would not allow.

    Sets up one real file, 'SOP CD1/Finnish Meat Market.jpg', matching
    _sheet_row's defaults, so an unmodified row resolves cleanly and a
    test only has to override the one cell it wants broken or blank."""
    (tmp_path / "SOP CD1").mkdir()
    (tmp_path / "SOP CD1" / "Finnish Meat Market.jpg").write_bytes(b"x")

    config = _sheet_config(
        files_dir=str(tmp_path),
        file_template="{folder_on_lacie_drive}/{file_name}",
        required_for_upload=required_for_upload,
    )
    outcomes = resolve_sheet_files(rows, config)
    return validate_sheet_grid(rows, make_registry(), config, [], outcomes)


def test_a_blank_required_field_is_not_an_error(tmp_path):
    rows = [_sheet_row(title="", theme="Townscape")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].errors == []
    assert results[0].missing_fields == ["title"]
    assert results[0].readiness is Readiness.NOT_READY


def test_a_broken_filename_produces_exactly_one_error(tmp_path):
    """The duplicate 'missing required column file' line must be gone. Count
    the errors - asserting 'contains the resolver message' passes just as
    well with the redundant line still there. Exact message equality (not
    a substring) per this plan's rule: a prior review found weakening
    exact assertions to substrings dropped a mutation's catch rate."""
    rows = [_sheet_row(title="Finnish Meat Market", theme="Townscape", name="Finnis.jpg")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert len(results[0].errors) == 1
    assert results[0].errors[0] == _unresolved_message(tmp_path / "SOP CD1", "Finnis.jpg")


def test_a_broken_filename_in_an_uncatalogued_row_is_both(tmp_path):
    rows = [_sheet_row(title="", theme="", name="Finnis.jpg")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].readiness is Readiness.NOT_READY
    assert results[0].is_valid is False


def test_missing_fields_names_the_blank_template_cells_too(tmp_path):
    rows = [_sheet_row(title="A Title", theme="Townscape", folder="", name="")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].missing_fields == ["folder_on_lacie_drive", "file_name"]


def test_missing_fields_combines_both_of_its_sources_on_one_row(tmp_path):
    """Task 6 review gap, closed here: missing_fields is populated from two
    places - validate_rows() seeds it from blank required_for_upload
    columns, then validate_sheet_grid() extends it with blank
    file_template cells (see the ordering note on
    RowValidation.missing_fields and the comment above the .extend() call
    in validate_sheet_grid). Every existing test exercises only ONE of the
    two sources at a time; this pins the combined list, in the documented
    order (required_for_upload names first, then blank template cells),
    for a row missing a required column AND a template cell at once - a
    cross-task contract Task 8 depends on that nothing previously
    guarded."""
    rows = [_sheet_row(title="", theme="Townscape", folder="", name="")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].missing_fields == ["title", "folder_on_lacie_drive", "file_name"]


def test_a_field_named_by_both_sources_is_listed_once_not_twice(tmp_path):
    """missing_fields' two sources can name the SAME column. Listing a
    file_template column in required_for_upload - here ("title",
    "file_name") - is a natural edit, and check_required_for_upload accepts
    it because file_name IS a real column in the Sheet. validate_sheet_rows
    then seeds the name and validate_sheet_grid extends the same name on
    again from the blank template cells.

    Left duplicated, ONE not-ready row reported "2 missing file_name" - a
    per-field count larger than the number of rows it counts - and dragged
    in the overlap parenthetical, which told the operator the counts don't
    sum when for a single missing field they trivially do. Both halves are
    pinned: the deduplicated list, and the rendered breakdown as an exact
    ordered line list so a re-introduced duplicate fails from either side."""
    rows = [_sheet_row(title="A Title", name="")]
    _, results = _validate(rows, required_for_upload=("title", "file_name"), tmp_path=tmp_path)
    assert results[0].missing_fields == ["file_name"]
    assert format_missing_field_lines(results) == ["    1 missing file_name: row 2"]


# --- Task 8: `validate` reporting - not-ready marker + per-field breakdown ---
#
# Global-constraint note: the brief's own draft asserted five of these via
# `"some text" in breakdown` (substring-of-the-whole-blob). Per this plan's
# exact-assertion rule (a prior review found weakening exact assertions to
# substrings dropped a mutation's catch rate from 19 to 16), every one of
# those five is rewritten below as exact LINE membership -
# `"...exact line..." in breakdown.splitlines()` - derived from the
# implementation's actual leading whitespace, never a substring check.


def test_not_ready_rows_are_marked_in_the_itemised_list():
    results = [
        RowValidation(2, "", errors=["no file found …"], missing_fields=["title"]),
    ]
    lines = _format_result_lines(results)
    assert lines[0] == "[FAIL] row 2  (not yet catalogued)"


def test_ready_rows_are_not_marked():
    results = [RowValidation(2, "", errors=["no file found …"])]
    lines = _format_result_lines(results)
    assert lines[0] == "[FAIL] row 2"


def test_breakdown_counts_a_row_missing_two_fields_in_both():
    results = [
        RowValidation(2, "", missing_fields=["title", "theme"]),
        RowValidation(3, "", missing_fields=["title"]),
    ]
    lines = format_missing_field_lines(results)
    # The "N rows not yet catalogued" header this used to assert is now the
    # lifecycle line these lines sit under - see
    # test_the_missing_fields_are_listed_under_the_line_that_counts_them.
    # Was `assert "2 missing title" in breakdown` - the real line carries
    # 4 leading spaces, which a substring check would not have pinned.
    assert "    2 missing title: rows 2-3" in lines
    # Was `assert "1 missing theme" in breakdown` - same reasoning.
    assert "    1 missing theme: row 2" in lines


def test_breakdown_says_the_counts_overlap():
    results = [RowValidation(2, "", missing_fields=["title", "theme"])]
    # Was `assert "more than one count" in format_readiness_breakdown(results)`
    # (substring) - rewritten to the exact overlap-parenthetical line,
    # including its leading spaces and the row-count it interpolates.
    lines = format_missing_field_lines(results)
    assert (
        "    (a row missing more than one field appears in more than one "
        "count above, so these do not sum to 1)"
    ) in lines


def test_breakdown_field_list_follows_the_data_not_a_hardcoded_pair():
    results = [RowValidation(2, "", missing_fields=["photographer_studio"])]
    # Was `assert "1 missing photographer_studio" in
    # format_readiness_breakdown(results)` (substring) - rewritten to exact
    # line membership, proving the field name is read from missing_fields
    # itself rather than a hardcoded title/theme pair.
    lines = format_missing_field_lines(results)
    assert "    1 missing photographer_studio: row 2" in lines


def test_breakdown_is_empty_when_every_row_is_ready():
    assert format_missing_field_lines([RowValidation(2, "")]) == []


def test_breakdown_omits_the_overlap_parenthetical_when_every_row_misses_exactly_one_field():
    """Guards the `if any(len(result.missing_fields) > 1 ...)` in
    format_readiness_breakdown: delete that `if` mentally and every test
    above still passes, because each of them has at least one row missing
    more than one field. Here two not-ready rows each miss exactly one
    (different) field, so 1 + 1 == 2 - the per-field counts genuinely sum
    to the not-ready total - and the overlap parenthetical, which exists
    to warn that they DON'T sum, must be absent entirely. An
    always-printed parenthetical would tell the operator the numbers
    don't add up when they do, which is actively wrong here."""
    results = [
        RowValidation(2, "", missing_fields=["title"]),
        RowValidation(3, "", missing_fields=["file_name"]),
    ]
    lines = format_missing_field_lines(results)
    assert lines == [
        "    1 missing file_name: row 3",
        "    1 missing title: row 2",
    ]


def test_breakdown_orders_by_count_then_breaks_ties_alphabetically():
    """Every other assertion in this file checks `"..." in lines`
    (membership), never position - a mutation collapsing the sort key to
    `item[0]` alone (plain alphabetical) would still pass all of them,
    because membership doesn't care what order the lines come in. This
    pins the full ordered output: title (count 2) must sort before
    file_name/theme (both count 1, tied) - proving count-descending is
    applied at all - and file_name must sort before theme within that
    tie - proving the tie-break is alphabetical, not insertion order or
    anything else."""
    results = [
        RowValidation(2, "", missing_fields=["title", "theme"]),
        RowValidation(3, "", missing_fields=["title", "file_name"]),
    ]
    lines = format_missing_field_lines(results)
    assert lines == [
        "    2 missing title: rows 2-3",
        "    1 missing file_name: row 3",
        "    1 missing theme: row 2",
        "    (a row missing more than one field appears in more than one "
        "count above, so these do not sum to 2)",
    ]


def test_the_not_ready_lifecycle_line_uses_the_singular_for_one_row():
    """No existing fixture produces exactly one not-ready row - every one
    of them uses two or three - so the singular branch of _pluralize on this
    line has never been rendered by a test. Pins it explicitly. It used to
    live on the standalone breakdown's own header; that header is gone, and
    this lifecycle line is what replaced it."""
    rows = [{"ia_identifier": "", "ia_uploaded": ""}]
    results = [RowValidation(2, "", missing_fields=["title"])]

    lines = format_lifecycle_summary(rows, results).splitlines()

    assert lines[1] == (
        "1 row not yet assigned an identifier and not yet catalogued (missing "
        "required fields) - waiting on data entry, not blocked by an error"
    )
    assert lines[2] == "    1 missing title: row 2"


# --- Task 9: `upload` reporting and exit code ---
#
# `validate` owns the data (the per-field breakdown of the ~2,900-row
# backlog); `upload` owns the run. These tests pin the split: `upload`
# itemizes only rows it would otherwise have uploaded (`blocked` - ready,
# but invalid), gives one contained line for the rest (`not_ready`), and -
# the point of this task - never lets a not-ready row affect the exit code.
# A not-ready row was never in this run's scope to begin with, so counting
# it against the exit code would return 1 on every run for months until all
# ~3,000 rows are catalogued, training the operator to ignore the exit code
# entirely.
#
# Global-constraint note: the brief's own draft asserted two of these
# against captured multi-line stdout with `"..." in output` (substring-of-
# the-whole-blob) - one of them a *truncated* line, which would pass even if
# the untranscribed rest of the line were wrong. Both are rewritten below to
# exact line membership via `.splitlines()`.


def _run_upload_and_capture(
    rows: list[dict[str, str]],
    required_for_upload: tuple[str, ...] = ("title", "theme"),
) -> tuple[str, int]:
    """Runs `upload` end-to-end against an in-memory grid built from
    _sheet_row rows, for the report-shape/exit-code tests below. Every row
    these tests exercise is either genuinely broken (an unresolvable
    filename) or not-ready (a blank title/theme) or both, so
    plan_upload_targets() never returns a real target and no network call -
    not even upload_row - is ever reached. build_sheet_client is
    monkeypatched anyway, as the single seam every other Sheet-path test
    replaces, so a bug that DID produce a target still could not reach past
    it onto a real credential lookup.

    stdout is captured via redirect_stdout rather than the capsys fixture:
    these tests take no fixtures at all (matching the brief's own
    signatures), so the helper has to do its own capturing."""
    from ia_bulk import cmd_upload

    headers = [
        "mediatype",
        "title",
        "theme",
        "folder_on_lacie_drive",
        "file_name",
        "ia_identifier",
        "ia_uploaded",
        "ia_url",
        "ia_identifier_bib",
    ]
    grid = [headers] + [[row.get(header, "") for header in headers] for row in rows]

    with tempfile.TemporaryDirectory() as files_dir:
        registry = make_sheet_registry(
            files_dir=files_dir,
            file_template="{folder_on_lacie_drive}/{file_name}",
            required_for_upload=list(required_for_upload),
        )
        registry_path = Path(files_dir) / "registry.json"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        args = make_upload_args(Path(files_dir), registry_path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exit_code = cmd_upload(args)

    return buffer.getvalue(), exit_code


def _run_upload(rows: list[dict[str, str]]) -> str:
    output, _ = _run_upload_and_capture(rows)
    return output


def _run_upload_exit_code(rows: list[dict[str, str]]) -> int:
    _, exit_code = _run_upload_and_capture(rows)
    return exit_code


def test_upload_itemises_only_ready_rows_that_are_broken():
    """`upload` itemizes only rows it would otherwise have uploaded - a
    not-ready row was never in scope, so it must never get a [FAIL] line of
    its own, only a mention in the one contained not-ready summary below."""
    output = _run_upload(rows=[
        _sheet_row(title="A", theme="B", name="Finnis.jpg"),    # ready + broken
        _sheet_row(title="", theme="", name="AlsoBroken.jpg"),  # not ready + broken
    ])
    lines = output.splitlines()
    # Global constraint: exact LINE membership, not `"..." in output`. Both
    # the bare and not-yet-catalogued-marker forms are checked for row 3 -
    # checking only the bare form would pass by accident against the OLD
    # behavior too, since that form appends the marker onto the SAME line
    # ("[FAIL] row 3  (not yet catalogued)"), never as its own line.
    assert "[FAIL] row 2" in lines
    assert "[FAIL] row 3" not in lines
    assert "[FAIL] row 3  (not yet catalogued)" not in lines


def test_upload_contains_the_broken_subcount_inside_the_not_ready_total():
    """The sub-count must read as CONTAINED within the not-ready total - in
    parentheses, inside the sentence - never as a second, disjoint number
    beside it, which is how two adjacent numbers are read otherwise."""
    output = _run_upload(rows=[_sheet_row(title="", theme="", name="Broken.jpg")])
    # Global constraint: exact LINE membership of the FULL line, not a
    # truncated substring - a truncated check would still pass even if the
    # untranscribed rest of the line were wrong.
    assert (
        "1 row not yet catalogued (1 of them also has an unresolvable "
        "filename - run `validate` to see them)"
    ) in output.splitlines()


def test_upload_pluralizes_the_subcount_when_more_than_one_broken_row_is_not_ready():
    """The brief's own test only ever renders the singular 'has' (one
    broken not-ready row). This pins the 'have' branch at count 2, so both
    forms of the has/have switch are actually exercised, not just written."""
    output = _run_upload(rows=[
        _sheet_row(title="", theme="", name="Broken1.jpg"),
        _sheet_row(title="", theme="", name="Broken2.jpg"),
    ])
    assert (
        "2 rows not yet catalogued (2 of them also have an unresolvable "
        "filename - run `validate` to see them)"
    ) in output.splitlines()


def test_upload_shows_no_per_field_breakdown():
    """validate owns the data, upload owns the run - decision 2. Repeating
    the per-field breakdown here would make `upload` loud about the backlog
    again, which is the entire thing this change exists to stop."""
    output = _run_upload(rows=[_sheet_row(title="", theme="")])
    assert "missing title" not in output


def test_upload_exits_zero_when_only_not_ready_rows_are_broken():
    """The point of this task: a row nobody has catalogued yet was never in
    this run's scope, so it must not turn the exit code non-zero."""
    assert _run_upload_exit_code(rows=[_sheet_row(title="", theme="", name="Broken.jpg")]) == 0


def test_upload_exits_one_when_a_ready_row_is_broken():
    """The other half of the same rule: a row that WAS in scope (ready) and
    still failed validation must still exit non-zero."""
    assert _run_upload_exit_code(rows=[_sheet_row(title="A", theme="B", name="Finnis.jpg")]) == 1


def test_cmd_upload_exit_code_ignores_a_not_ready_broken_row_during_a_real_run(
    tmp_path, monkeypatch
):
    """The two `_run_upload_exit_code` tests above never reach real
    execution - both their only rows are invalid, so plan_upload_targets()
    never hands back a target and the run returns through the early
    'nothing to upload' branch. This is the third place a not-ready row
    could leak into the exit code: the summary printed AFTER a real upload
    run used to OR the raw skipped-row count (not-ready rows included) into
    its return value too. Here a genuinely ready row uploads successfully
    alongside a not-ready row whose filename is also broken; the exit code
    must still be 0."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["", "does-not-exist.jpg", "", "", "", ""],
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))

    assert exit_code == 0
    assert recorder.uploads == [f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001"]


# --- Final review: not-ready rows must never enter a run's scope ---
#
# Task 6 shrank SHEET_REQUIRED_COLUMNS to ("mediatype",), which moved a
# blank-title / blank-file row from INVALID to VALID + NOT_READY. Task 9
# taught upload_from_sheet to itemize and exit-code on readiness, but the
# actual scope filter lives in plan_upload_targets, which filtered on
# is_valid alone - so ~2,900 uncatalogued rows were still planned and
# uploaded under permanent, unrenameable identifiers.
#
# Every pre-existing upload test kept its not-ready rows ALSO invalid (an
# unresolvable filename, or an empty files_dir), so none of them could see
# it. The tests below deliberately use the shape that was missing: a row
# that is NOT_READY and genuinely VALID, with a real file on disk.


def test_plan_upload_targets_skips_a_not_ready_row_that_is_genuinely_valid(tmp_path):
    """THE regression guard. Blank title, filename resolving to a real file
    on disk - so is_valid is True and nothing upstream objects - must still
    produce no target. Before this fix it produced one, and a real
    photograph would have uploaded under a permanent identifier carrying no
    title and no theme.

    is_valid is asserted explicitly rather than assumed: if a later change
    made this row invalid again, the empty-targets assertion would still
    pass, but for the old reason rather than the one this test exists to
    pin."""
    from ia_bulk import plan_upload_targets

    rows = [_sheet_row(title="", theme="Townscape")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].is_valid is True
    assert results[0].readiness is Readiness.NOT_READY

    targets = plan_upload_targets(
        rows, results, _sheet_config(), live=False, fingerprints={}, stamp=FIXED_STAMP
    )
    assert targets == []


def test_plan_upload_targets_skips_a_wholly_uncatalogued_row(tmp_path):
    """The ~2,900-row case, and the more dangerous of the two: nobody has
    touched this row at all, so resolve_sheet_files leaves row['file'] as
    '' - and Path(files_dir) / '' is files_dir ITSELF, which
    internetarchive uploads recursively. row['file'] is asserted to be
    blank so the test states plainly why an escaping target here is not
    merely a wrong item but the entire data tree in one."""
    from ia_bulk import plan_upload_targets

    rows = [_sheet_row(title="", theme="", folder="", name="")]
    _, results = _validate(rows, required_for_upload=("title", "theme"), tmp_path=tmp_path)
    assert results[0].is_valid is True
    assert rows[0]["file"] == ""

    targets = plan_upload_targets(
        rows, results, _sheet_config(), live=False, fingerprints={}, stamp=FIXED_STAMP
    )
    assert targets == []


def test_cmd_upload_uploads_nothing_for_a_not_ready_row_whose_file_exists(
    tmp_path, monkeypatch, capsys
):
    """End-to-end counterpart, through cmd_upload with write-back on. The
    grid's one row has a blank Title (required_for_upload defaults to
    ["title"]) and names photo1.jpg, which setup_sheet_upload really does
    create - so this is the valid-but-not-ready shape, not a broken row.

    Asserted as 'the recorded event log holds exactly the one grid read and
    nothing else' rather than 'no upload of the wrong identifier': the
    latter still passes if something uploads under an identifier the test
    did not think to name. recorder.events spans writes as well as uploads,
    so a reserve write for an out-of-scope row fails this too."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["", "photo1.jpg", "", "", "", ""],
    ]
    recorder, _, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    output = capsys.readouterr().out

    assert recorder.uploads == []
    assert recorder.events == [("read", None)]
    assert exit_code == 0
    # `upload` reports the row as backlog, never as something it skipped
    # because of an error - and says so on its own line.
    assert "1 row not yet catalogued" in output.splitlines()


def test_upload_row_refuses_a_blank_file_value(tmp_path, monkeypatch):
    """Defence in depth, and the reason it is a raise rather than trust in
    plan_upload_targets: this is the most damaging single call in the
    system. internetarchive.upload is monkeypatched to blow up rather than
    merely recorded, so the test proves the refusal happens BEFORE the
    library is reached, not that the library was called with something
    harmless."""
    from ia_bulk import upload_row

    def exploding_upload(*args, **kwargs):
        raise AssertionError("internetarchive.upload must never be reached for a blank file")

    monkeypatch.setattr(internetarchive, "upload", exploding_upload)

    with pytest.raises(ValueError) as excinfo:
        upload_row(
            {"file": "", "title": "A photo", "mediatype": "image"},
            target_identifier="zztest-lcps-astoriaphotos-00001",
            collection="test_collection",
            files_dir=tmp_path,
        )

    assert str(excinfo.value) == (
        "upload of 'zztest-lcps-astoriaphotos-00001' refused: the row has no 'file' value, "
        "and uploading a blank filename would send the entire files_dir recursively into "
        "one permanent item"
    )


def test_upload_row_refuses_a_whitespace_only_file_value(tmp_path, monkeypatch):
    """A cell holding a space is not blank to `if not row["file"]`, but it
    IS blank after .strip() - and it resolves to files_dir just the same.
    The guard has to read the stripped value, so this pins that it does."""
    from ia_bulk import upload_row

    monkeypatch.setattr(
        internetarchive,
        "upload",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not be reached")),
    )

    with pytest.raises(ValueError):
        upload_row(
            {"file": "   ", "title": "A photo"},
            target_identifier="zztest-lcps-astoriaphotos-00001",
            collection="test_collection",
            files_dir=tmp_path,
        )


def _three_ready_row_grid():
    return [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
        ["Third photo", "photo3.jpg", "", "", "", ""],
    ]


def test_cmd_upload_refuses_a_sheet_run_over_the_daily_item_cap(
    tmp_path, monkeypatch, capsys
):
    """`.claude/CLAUDE.md`: "IA batch limits: 500 items per upload run,
    5000/day". CHUNK_SIZE covered the per-run half; nothing covered the daily
    half, so a bare `upload --live` on a fully catalogued Sheet planned every
    ready row and relied on best-effort 429/503 detection - which has never
    been observed against a live response - to notice."""
    from ia_bulk import cmd_upload

    monkeypatch.setattr("ia_bulk.DAILY_ITEM_CAP", 2)
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        _three_ready_row_grid(),
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    err = capsys.readouterr().err

    # Refuses rather than silently capping: a run that quietly stops short
    # reads as a complete one.
    assert recorder.uploads == []
    assert recorder.writes == []
    assert "would upload 3 items, over Internet Archive's 2/day cap" in err
    assert "--limit 2" in err
    assert exit_code == 1


def test_cmd_upload_daily_cap_is_satisfied_by_limit(tmp_path, monkeypatch, capsys):
    """--limit is the ordinary way through: the cap is checked AFTER the
    slice, so a Sheet with more ready rows than the cap is not itself an
    error - running at all of them in one day is."""
    from ia_bulk import cmd_upload

    monkeypatch.setattr("ia_bulk.DAILY_ITEM_CAP", 2)
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        _three_ready_row_grid(),
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
    )

    exit_code = cmd_upload(
        make_upload_args(tmp_path, registry_path, write_identifier=True, limit=2)
    )

    assert len(recorder.uploads) == 2
    assert "over Internet Archive's" not in capsys.readouterr().err
    assert exit_code == 0


def test_cmd_upload_daily_cap_can_be_overridden_explicitly(tmp_path, monkeypatch, capsys):
    """The cap is Internet Archive's, and an account whose cap has been
    raised is a real case - but it has to be stated, not assumed."""
    from ia_bulk import cmd_upload

    monkeypatch.setattr("ia_bulk.DAILY_ITEM_CAP", 2)
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        _three_ready_row_grid(),
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
    )

    exit_code = cmd_upload(
        make_upload_args(
            tmp_path, registry_path, write_identifier=True, allow_over_daily_cap=True
        )
    )

    assert len(recorder.uploads) == 3
    assert "over Internet Archive's" not in capsys.readouterr().err
    assert exit_code == 0


def test_sheet_upload_run_computes_the_uploadable_set_once_per_run(tmp_path):
    """The column map is fixed for a whole run, so deriving the uploadable
    set inside sheet_upload_metadata meant a 10,000-row upload rebuilding the
    same set 10,000 times, each rebuild scanning every header."""
    from column_map import build_column_map
    from ia_bulk import SheetColumns, SheetUploadRun
    from sheet_client import SheetClient

    column_map = build_column_map(["Title", "file", "Notes (LCPS Internal)"])
    calls = {"n": 0}
    real = column_map.uploadable_fields

    def counting_uploadable_fields():
        calls["n"] += 1
        return real()

    object.__setattr__(column_map, "uploadable_fields", counting_uploadable_fields)

    run = SheetUploadRun(
        # never touched: `uploadable` reads only the column map
        client=SheetClient(None, "SHEET_ID", "Sheet1"),
        columns=SheetColumns(ia_identifier=2, ia_uploaded=3, ia_url=4, ia_identifier_bib=5),
        column_map=column_map,
        mediatype="image",
        file_template="{file}",
        files_dir=str(tmp_path),
        collection="test_collection",
        live=False,
        write_back=False,
        log_path=tmp_path / "log.jsonl",
    )

    first = run.uploadable
    second = run.uploadable

    assert first == second == frozenset({"title"})
    assert calls["n"] == 1


def _sync_registry(tmp_path):
    path = tmp_path / "projects_registry.json"
    path.write_text(json.dumps(make_registry()), encoding="utf-8")
    return path


SYNC_STAMP = "20260823t161331"
SYNC_URL = f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001"


# The sync path needs two more columns than the upload path: sync-metadata
# records what it last pushed so it can send only the rows that changed.
# G=ia_sync_hash, H=ia_last_synced.
SYNC_SHEET_HEADER = SHEET_HEADER + ["ia_sync_hash", "ia_last_synced"]


def _synced_grid(rows=None):
    """A Sheet whose rows are already uploaded, as upload's confirm write
    leaves it: ia_identifier, ia_uploaded and ia_url all populated, and the
    two sync-state cells still blank - so every row reads as changed and
    pushes, which is what the pre-hash-gating tests all assume."""
    default = [[
        "Stone Customshouse", "photo1.jpg",
        "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg",
        "", "",
    ]]
    return [SYNC_SHEET_HEADER] + (default if rows is None else rows)


def test_read_sheet_snapshot_exposes_the_column_map():
    """SheetSyncRun re-locates its two sync-state columns in the fresh read
    to detect that they moved, the same way upload compares SheetColumns.
    The snapshot already builds a ColumnMap; keeping it costs nothing and
    saves a second parse of the same grid."""
    from ia_bulk import read_sheet_snapshot

    client = FakeSheetClient(_synced_grid())
    snapshot = read_sheet_snapshot(client, "{file}")

    assert snapshot.column_map.field_names["ia_sync_hash"] == "ia_sync_hash"


def test_plan_sync_targets_hashes_what_the_row_would_send(tmp_path):
    """Computed at READ time and carried on the target. Re-deriving it at
    write time would stamp a mid-run human edit as already-synced, and that
    edit would be lost permanently with nothing to notice it."""
    from ia_bulk import plan_sync_targets
    from ia_fields import metadata_to_send
    from sync_state import sync_hash

    column_map, rows = grid_to_rows(_synced_grid())
    targets, problems = plan_sync_targets(
        rows, column_map, live=False, project_id="astoriaphotos", file_template="{file}"
    )

    assert problems == []
    assert targets[0].content_hash == sync_hash(metadata_to_send(targets[0].metadata))


def test_plan_sync_targets_reads_the_stored_hash_off_the_row():
    from ia_bulk import plan_sync_targets

    grid = _synced_grid([[
        "Stone Customshouse", "photo1.jpg",
        "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg",
        "deadbeef", "2026-09-01T00:00:00Z",
    ]])
    column_map, rows = grid_to_rows(grid)
    targets, _ = plan_sync_targets(
        rows, column_map, live=False, project_id="astoriaphotos", file_template="{file}"
    )

    assert targets[0].stored_hash == "deadbeef"


def test_plan_sync_targets_treats_a_blank_hash_cell_as_no_stored_hash():
    """A never-synced row, and a row whose hash cell an operator cleared to
    force a re-sync, are the same case and must both push."""
    from ia_bulk import plan_sync_targets

    column_map, rows = grid_to_rows(_synced_grid())
    targets, _ = plan_sync_targets(
        rows, column_map, live=False, project_id="astoriaphotos", file_template="{file}"
    )

    assert targets[0].stored_hash == ""
    assert targets[0].content_hash != ""


def test_plan_sync_targets_fingerprints_the_row_for_the_moved_row_guard():
    """The fingerprint is the file_template candidate from the RAW cells -
    the same value sheet_row_fingerprints() produces - so it can be compared
    against a fresh read of the Sheet, which has raw cells in it."""
    from ia_bulk import plan_sync_targets, sheet_row_fingerprints

    column_map, rows = grid_to_rows(_synced_grid())
    targets, _ = plan_sync_targets(
        rows, column_map, live=False, project_id="astoriaphotos", file_template="{file}"
    )

    assert targets[0].source_fingerprint == sheet_row_fingerprints(rows, "{file}")[2]
    assert targets[0].source_fingerprint != ""


def test_sync_target_is_never_newly_minted():
    """split_moved_targets reads this attribute. A sync target addresses a
    row that uploaded long ago, so it is never a number this run minted -
    and the guard is always called on the post-reserve leg."""
    from ia_bulk import plan_sync_targets

    column_map, rows = grid_to_rows(_synced_grid())
    targets, _ = plan_sync_targets(
        rows, column_map, live=False, project_id="astoriaphotos", file_template="{file}"
    )

    assert targets[0].newly_minted is False


def _sync_target(row_number=2, content_hash="new", stored_hash=""):
    from ia_bulk import SyncTarget

    return SyncTarget(
        row_number=row_number,
        identifier=f"lcps-astoriaphotos-{row_number:05d}",
        uploaded_as=f"zztest-{SYNC_STAMP}-lcps-astoriaphotos-{row_number:05d}",
        metadata={"title": "Pier 39"},
        content_hash=content_hash,
        stored_hash=stored_hash,
    )


def test_split_unchanged_pushes_a_row_whose_content_changed():
    from ia_bulk import split_unchanged

    to_push, already = split_unchanged([_sync_target(content_hash="b", stored_hash="a")])

    assert len(to_push) == 1
    assert already == []


def test_split_unchanged_holds_back_a_row_that_already_matches():
    from ia_bulk import split_unchanged

    to_push, already = split_unchanged([_sync_target(content_hash="a", stored_hash="a")])

    assert to_push == []
    assert len(already) == 1


def test_split_unchanged_pushes_a_row_that_has_never_synced():
    """A blank stored hash. Also the state an operator creates deliberately
    by clearing the cell to force a re-sync."""
    from ia_bulk import split_unchanged

    to_push, already = split_unchanged([_sync_target(content_hash="a", stored_hash="")])

    assert len(to_push) == 1
    assert already == []


def test_split_unchanged_preserves_order():
    """Rows are reported to a human by row number; reordering them would make
    the output disagree with the Sheet on screen next to it."""
    from ia_bulk import split_unchanged

    targets = [
        _sync_target(row_number=2, content_hash="a", stored_hash=""),
        _sync_target(row_number=3, content_hash="b", stored_hash="b"),
        _sync_target(row_number=4, content_hash="c", stored_hash=""),
    ]
    to_push, already = split_unchanged(targets)

    assert [t.row_number for t in to_push] == [2, 4]
    assert [t.row_number for t in already] == [3]


def _sync_sheet_args(tmp_path, registry_path, **overrides):
    args = Namespace(
        project="astoriaphotos",
        registry=str(registry_path),
        live=False,
        log_dir=str(tmp_path / "logs"),
        dry_run=False,
        chunk_size=CHUNK_SIZE,
    )
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def _setup_sync_sheet(tmp_path, monkeypatch, grid, sent, client=None, registry=None):
    """RecordingSheetClient, not FakeSheetClient: sync-metadata now WRITES to
    the Sheet (the hash stamp), and it re-reads before each write for the
    moved-row guard, so the fake has to apply writes to its own grid the way
    a real Sheet would.

    Returns (registry_path, client) - a test that only cares about what was
    sent can ignore the second element."""
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(registry or make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    if client is None:
        client = RecordingSheetClient(grid, SheetUploadRecorder())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr(
        "ia_bulk.update_metadata_row",
        lambda metadata, target: sent.append((target, metadata)),
    )
    return registry_path, client


def _pushed_rows(client):
    """The (row_number, hash) pairs a run stamped, read back off the fake
    Sheet's grid - i.e. what a NEXT run would see, which is the property that
    actually matters."""
    header = client.grid[0]
    hash_index = header.index("ia_sync_hash")
    return [
        (index + 1, row[hash_index])
        for index, row in enumerate(client.grid)
        if index > 0 and hash_index < len(row) and row[hash_index]
    ]


def _run_sync_twice(tmp_path, monkeypatch, grid, edit=None):
    """A run, then a second run over the Sheet the first one left behind -
    the only way to test change detection, since the state IS the Sheet.
    `edit` mutates the grid between the two runs."""
    from ia_bulk import cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(grid, SheetUploadRecorder())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)

    first, second = [], []

    def record_into(destination):
        # Bound as a default argument, not captured by closure: two stubs
        # closing over one rebound name is the kind of thing that reads as
        # correct and silently records both runs into the same list.
        return lambda metadata, target, out=destination: out.append(target)

    monkeypatch.setattr("ia_bulk.update_metadata_row", record_into(first))
    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    if edit is not None:
        edit(client.grid)

    monkeypatch.setattr("ia_bulk.update_metadata_row", record_into(second))
    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    return first, second, exit_code


def test_sync_over_an_unedited_sheet_pushes_nothing_and_says_so(
    tmp_path, monkeypatch, capsys
):
    """Acceptance criterion 1. The whole point: on an hourly schedule the
    steady state must be silent."""
    first, second, exit_code = _run_sync_twice(tmp_path, monkeypatch, _synced_grid())
    out = capsys.readouterr().out

    assert len(first) == 1
    assert second == []
    assert "already match their last push" in out
    assert exit_code == 0


def _all_sync_log_lines(tmp_path):
    """Every record in every sync-metadata log the run has written so far, in
    write order.

    Deliberately not "the one log file this run wrote": open_log() names a
    log by wall-clock second, so two cmd_sync_metadata() calls inside one
    fast test can legitimately collide on the same filename and append to
    it. Reading the whole directory and diffing by record count is robust to
    that collision either way - one growing file, or two separate ones."""
    lines: list[str] = []
    for path in sorted((tmp_path / "logs").glob("sync-metadata-*.jsonl")):
        lines.extend(path.read_text(encoding="utf-8").strip().splitlines())
    return [json.loads(line) for line in lines if line]


def test_sync_over_an_unedited_sheet_still_writes_a_log(tmp_path, monkeypatch, capsys):
    """The steady state is the MOST common outcome on an hourly schedule, so
    it is exactly the run an unattended operator most needs a record of -
    OPERATIONS.md's tail-the-latest-log recipe depends on every run leaving
    one. The console line stays the single quiet sentence; only the log gets
    the new record, with the real (mostly zero) numbers in it."""
    from ia_bulk import cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(_synced_grid(), SheetUploadRecorder())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda metadata, target: None)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))  # first run: pushes and stamps
    capsys.readouterr()
    before = _all_sync_log_lines(tmp_path)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))  # steady state
    out = capsys.readouterr().out
    after = _all_sync_log_lines(tmp_path)

    new_entries = after[len(before):]
    assert any(entry["record"] == "run_header" for entry in new_entries)
    summary = new_entries[-1]
    assert summary["record"] == "run_summary"
    assert summary["checked"] == 1
    assert summary["changed"] == 0
    assert summary["unchanged"] == 0
    assert summary["already_synced"] == 1
    assert "already match their last push" in out
    assert "log written to" in out
    assert exit_code == 0


def test_sync_pushes_exactly_the_row_whose_cell_was_edited(tmp_path, monkeypatch):
    """Acceptance criterion 2."""
    rows = [
        ["Photo 1", "photo1.jpg", "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001",
         "photo1.jpg", "", ""],
        ["Photo 2", "photo2.jpg", "lcps-astoriaphotos-00002", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002",
         "photo2.jpg", "", ""],
    ]

    def retitle_the_second_row(grid):
        grid[2][0] = "Photo 2, corrected"

    first, second, _ = _run_sync_twice(
        tmp_path, monkeypatch, _synced_grid(rows), edit=retitle_the_second_row
    )

    assert len(first) == 2
    assert second == [f"zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002"]


def test_clearing_one_hash_cell_re_syncs_only_that_row(tmp_path, monkeypatch):
    """Acceptance criterion 3, first half. The operational lever, and the
    single carve-out to the "never edit an ia_ column" rule: clear the cell,
    never type into it."""
    rows = [
        ["Photo 1", "photo1.jpg", "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001",
         "photo1.jpg", "", ""],
        ["Photo 2", "photo2.jpg", "lcps-astoriaphotos-00002", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002",
         "photo2.jpg", "", ""],
    ]

    def clear_the_second_rows_hash(grid):
        grid[2][6] = ""

    first, second, _ = _run_sync_twice(
        tmp_path, monkeypatch, _synced_grid(rows), edit=clear_the_second_rows_hash
    )

    assert len(first) == 2
    assert second == [f"zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002"]


def test_clearing_the_whole_hash_column_re_syncs_everything(tmp_path, monkeypatch):
    """Acceptance criterion 3, second half."""
    rows = [
        ["Photo 1", "photo1.jpg", "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001",
         "photo1.jpg", "", ""],
        ["Photo 2", "photo2.jpg", "lcps-astoriaphotos-00002", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002",
         "photo2.jpg", "", ""],
    ]

    def clear_the_column(grid):
        for row in grid[1:]:
            row[6] = ""

    first, second, _ = _run_sync_twice(
        tmp_path, monkeypatch, _synced_grid(rows), edit=clear_the_column
    )

    assert len(first) == 2
    assert len(second) == 2


def test_sync_summary_counts_the_rows_it_held_back(tmp_path, monkeypatch, capsys):
    rows = [
        ["Photo 1", "photo1.jpg", "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001",
         "photo1.jpg", "", ""],
        ["Photo 2", "photo2.jpg", "lcps-astoriaphotos-00002", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002",
         "photo2.jpg", "", ""],
    ]

    def retitle_the_second_row(grid):
        grid[2][0] = "Photo 2, corrected"

    _run_sync_twice(tmp_path, monkeypatch, _synced_grid(rows), edit=retitle_the_second_row)
    out = capsys.readouterr().out

    assert "1 row already in sync" in out


def test_sync_stamps_a_row_that_pushed_successfully(tmp_path, monkeypatch):
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, client = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    assert len(sent) == 1
    assert len(_pushed_rows(client)) == 1


def test_sync_stamps_the_read_time_hash_and_a_timestamp(tmp_path, monkeypatch):
    """Both cells, and the hash is the one computed from the row as READ."""
    from ia_bulk import cmd_sync_metadata, plan_sync_targets

    grid = _synced_grid()
    column_map, rows = grid_to_rows(grid)
    expected = plan_sync_targets(
        rows, column_map, live=False, project_id="astoriaphotos", file_template="{file}"
    )[0][0].content_hash

    sent = []
    registry_path, client = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)
    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    assert client.grid[1][6] == expected
    assert client.grid[1][7] != ""


def test_sync_does_not_stamp_a_row_whose_push_failed(tmp_path, monkeypatch):
    """The cell is left untouched so the row retries on the next run. A
    failure means the item may be in a state nobody intended; stamping it
    would declare it settled."""
    from ia_bulk import cmd_sync_metadata

    def boom(metadata, target):
        raise RuntimeError("Internet Archive returned 503")

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(_synced_grid(), SheetUploadRecorder())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr("ia_bulk.update_metadata_row", boom)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    assert exit_code == 1
    assert _pushed_rows(client) == []


def test_sync_stamps_a_row_internet_archive_reports_unchanged(tmp_path, monkeypatch):
    """"no changes to _meta.xml" means the item already matches the Sheet -
    a successful reconciliation, not a failure. Leaving it unstamped would
    make exactly the rows this feature exists to quiet re-push every hour,
    forever."""
    from ia_bulk import MetadataUnchanged, cmd_sync_metadata

    def unchanged(metadata, target):
        raise MetadataUnchanged(target)

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(_synced_grid(), SheetUploadRecorder())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr("ia_bulk.update_metadata_row", unchanged)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    assert len(_pushed_rows(client)) == 1


def test_sync_stamps_each_chunk_as_it_goes(tmp_path, monkeypatch):
    """Interruption tolerance, pinned. The Mac this runs on sleeps and shuts
    down unpredictably, including mid-run. A run that dies during chunk 2
    must leave chunk 1 stamped, so the rerun pushes only the remainder."""
    from ia_bulk import cmd_sync_metadata

    rows = [
        [f"Photo {n}", f"photo{n}.jpg", f"lcps-astoriaphotos-{n:05d}",
         "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-{n:05d}",
         f"photo{n}.jpg", "", ""]
        for n in range(1, 5)
    ]
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(_synced_grid(rows), SheetUploadRecorder())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)

    def die_on_the_third(metadata, target):
        if target.endswith("00003"):
            raise KeyboardInterrupt
    monkeypatch.setattr("ia_bulk.update_metadata_row", die_on_the_third)

    with pytest.raises(KeyboardInterrupt):
        cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, chunk_size=2))

    # Chunk 1 (rows 2-3) was stamped before chunk 2 was attempted.
    assert [row for row, _ in _pushed_rows(client)] == [2, 3]


def test_sync_does_not_stamp_a_row_that_moved_between_read_and_stamp(
    tmp_path, monkeypatch, capsys
):
    """Row numbers are positional. If a human deletes a row above ours
    mid-run, our row number now addresses a different photograph - stamping
    there would mark a row synced that never was, and withhold its metadata
    forever. Skip and report; it goes out on the next run."""
    from ia_bulk import cmd_sync_metadata

    rows = [
        ["Photo 1", "photo1.jpg", "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001",
         "photo1.jpg", "", ""],
        ["Photo 2", "photo2.jpg", "lcps-astoriaphotos-00002", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002",
         "photo2.jpg", "", ""],
    ]

    def delete_the_first_data_row(grid, read_count):
        if read_count == 2:          # the guard's re-read, after the initial one
            del grid[1]

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(
        _synced_grid(rows), SheetUploadRecorder(), before_read=delete_the_first_data_row
    )
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda metadata, target: None)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    err = capsys.readouterr().err

    # Row 2 now holds what was row 3. Neither row may be stamped at row 2.
    assert _pushed_rows(client) == []
    assert "edited while the run was in progress" in err


def test_sync_does_not_blame_a_mid_run_edit_for_a_blank_fingerprint(
    tmp_path, monkeypatch, capsys
):
    """A row whose file_template cell is already blank fingerprints as ""
    (sheet_row_fingerprints()), which can never match - so split_moved_
    targets() files it as moved on EVERY run, whether or not the Sheet was
    touched. `file` is a RESERVED_FIELDS column, so clearing it does not
    change the content hash by itself; this row also has a genuine pending
    edit (blank stored hash), so it is a push candidate that then fails the
    moved-row guard for a reason that has nothing to do with anyone editing
    the Sheet mid-run. The message must not say otherwise."""
    from ia_bulk import cmd_sync_metadata

    grid = _synced_grid([[
        "Stone Customshouse", "", "lcps-astoriaphotos-00001",
        "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg", "", "",
    ]])
    sent = []
    registry_path, client = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    err = capsys.readouterr().err

    assert len(sent) == 1
    assert _pushed_rows(client) == []
    assert "the Sheet was edited while the run was in progress" not in err
    assert "no file_template fingerprint" in err
    assert "IS on Internet Archive" in err


def test_sync_stamps_normally_when_nothing_moved(tmp_path, monkeypatch):
    """The guard must not fire on the ordinary case - a false positive here
    means a row re-pushes every hour forever."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, client = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    assert len(_pushed_rows(client)) == 1


def test_sync_does_not_stamp_when_the_sync_columns_moved(tmp_path, monkeypatch, capsys):
    """A column inserted mid-run makes every cached column index wrong, so
    every cell this run would write lands in the wrong column."""
    from ia_bulk import cmd_sync_metadata

    def insert_a_column(grid, read_count):
        if read_count == 2:
            for row in grid:
                row.insert(0, "new")

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(
        _synced_grid(), SheetUploadRecorder(), before_read=insert_a_column
    )
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda metadata, target: None)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    assert "columns moved" in capsys.readouterr().err


def test_sync_survives_a_failed_re_read(tmp_path, monkeypatch, capsys):
    """A transient 503 on the guard's read must not end a run with a stack
    trace after it has already changed permanent public metadata. The chunk
    goes unstamped and re-pushes next run."""
    from ia_bulk import cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(_synced_grid(), SheetUploadRecorder(), raise_on_read=2)
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda metadata, target: None)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    err = capsys.readouterr().err

    assert _pushed_rows(client) == []
    assert "could not be re-read" in err


def test_sync_reports_and_continues_when_the_stamp_write_fails(tmp_path, monkeypatch, capsys):
    """An exception inside write_cells_if_any must be reported, not raised:
    the metadata is already on Internet Archive by this point, and an
    unstamped row simply re-pushes next run and reports unchanged there.
    Stopping the run instead would trade that harmless repeat for leaving
    every later chunk unpushed."""
    from ia_bulk import cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(_synced_grid(), SheetUploadRecorder(), raise_on_write=1)
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda metadata, target: None)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    err = capsys.readouterr().err

    assert _pushed_rows(client) == []
    assert "IS on Internet Archive" in err


def test_sync_from_sheet_refuses_a_sheet_without_the_sync_state_columns(
    tmp_path, monkeypatch, capsys
):
    """Without somewhere to record what it pushed, this command would send
    every row on every run and nothing would fail to say so. On an hourly
    schedule that is silent, permanent noise - the exact failure hash gating
    exists to remove, so it must not be the fallback when a column is
    missing."""
    from ia_bulk import cmd_sync_metadata

    grid = [SHEET_HEADER] + [[
        "Stone Customshouse", "photo1.jpg",
        "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg",
    ]]
    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    err = capsys.readouterr().err

    assert exit_code == 1
    assert sent == []
    assert "ia_sync_hash" in err
    assert "ia_last_synced" in err


def test_sync_from_sheet_refuses_missing_sync_columns_in_live_mode_too(
    tmp_path, monkeypatch, capsys
):
    """A rehearsal that gates where the real run would not is not a
    rehearsal. Same reasoning as the four write-back columns being required
    in every mode."""
    from ia_bulk import cmd_sync_metadata

    live_url = "https://archive.org/details/lcps-astoriaphotos-00001"
    grid = [SHEET_HEADER] + [[
        "Stone Customshouse", "photo1.jpg",
        "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z", live_url, "photo1.jpg",
    ]]
    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, live=True))

    assert exit_code == 1
    assert sent == []
    assert "ia_sync_hash" in capsys.readouterr().err


def test_sync_from_sheet_refuses_a_sheet_without_ia_identifier_bib(
    tmp_path, monkeypatch, capsys
):
    """ia_identifier_bib is the one write-back column that fails SILENTLY
    when it is missing, unlike its three companions: without ia_identifier
    or ia_uploaded no row classifies DONE (refused earlier, at "no row is
    marked uploaded yet"), and without ia_url every row becomes a reported
    problem. Without ia_identifier_bib alone, rows classify DONE, the hash
    gate passes them, permanent metadata goes out to Internet Archive, and
    only then does _verified() discover the column is gone and stamp
    nothing - on every chunk, forever. Refused up front instead, before
    anything is sent."""
    from ia_bulk import cmd_sync_metadata

    header = [
        "Title", "file", "ia_identifier", "ia_uploaded", "ia_url",
        "ia_sync_hash", "ia_last_synced",
    ]
    grid = [header] + [[
        "Stone Customshouse", "photo1.jpg", "lcps-astoriaphotos-00001",
        "2026-08-23T16:13:31Z", SYNC_URL, "", "",
    ]]
    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    err = capsys.readouterr().err

    assert exit_code == 1
    assert sent == []
    assert "ia_identifier_bib" in err


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_sync_from_sheet_rejects_a_non_positive_chunk_size(
    tmp_path, monkeypatch, capsys, chunk_size
):
    """Same failure shape as upload's own --chunk-size guard (see
    test_cmd_upload_rejects_a_non_positive_chunk_size): 0 raises inside
    chunk_rows() (range() forbids a zero step); -1 silently yields zero
    chunks, so the run pushes nothing and still reports success. Checked
    before any Sheet I/O."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)

    exit_code = cmd_sync_metadata(
        _sync_sheet_args(tmp_path, registry_path, chunk_size=chunk_size)
    )
    err = capsys.readouterr().err

    assert exit_code == 1
    assert sent == []
    assert err.splitlines() == [
        f"--chunk-size must be a positive number of items, not {chunk_size}. Zero raises "
        "inside chunk_rows(); a negative value silently produces zero chunks, pushing "
        "nothing while the run still reports success."
    ]


def test_sync_from_sheet_refuses_a_file_template_naming_a_column_the_sheet_lacks(
    tmp_path, monkeypatch, capsys
):
    """sync-metadata did not care about file_template until the moved-row
    guard arrived - the guard's fingerprint is built from those columns.
    sheet_row_fingerprints() fingerprints a row it cannot resolve as "",
    which never matches, so a broken template would report EVERY row as
    moved, stamp nothing, and re-push everything forever without ever
    failing. Refused up front instead.

    A header check only: no disk access, so a correction still does not
    depend on the photo drive being attached."""
    from ia_bulk import cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path), file_template="{no_such_column}")),
        encoding="utf-8",
    )
    sent = []
    client = RecordingSheetClient(_synced_grid(), SheetUploadRecorder())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr(
        "ia_bulk.update_metadata_row",
        lambda metadata, target: sent.append((target, metadata)),
    )

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    err = capsys.readouterr().err

    assert exit_code == 1
    assert sent == []
    assert "no_such_column" in err


def test_sync_from_sheet_still_runs_with_a_valid_file_template(tmp_path, monkeypatch):
    """The refusal above must not fire on the ordinary case."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)

    assert cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path)) == 0
    assert len(sent) == 1


def test_sync_from_sheet_sends_the_sheets_own_metadata_to_the_recorded_item(
    tmp_path, monkeypatch, capsys
):
    """The round trip the Sheet-as-source-of-truth model promises: edit a
    description in the Sheet, run this, it is on the site. No log, no
    flags - ia_url is the Sheet's own record of which item the row became, so
    nothing has to be re-derived."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert len(sent) == 1
    target, metadata = sent[0]
    assert target == f"zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001"
    assert metadata["title"] == "Stone Customshouse"
    assert "1 item(s) updated successfully" in out
    assert exit_code == 0


def test_sync_from_sheet_never_sends_tool_owned_or_pipeline_owned_columns(
    tmp_path, monkeypatch
):
    """Same filter upload uses (sheet_metadata_fields), so the two commands
    cannot disagree about what a row means. mediatype matters twice here:
    Internet Archive will not change it after upload."""
    from ia_bulk import cmd_sync_metadata

    header = SHEET_HEADER + [
        "ia_sync_hash", "ia_last_synced",
        "Notes (LCPS Internal)", "Mediatype", "Identifier",
    ]
    grid = [header] + [[
        "Stone Customshouse", "photo1.jpg",
        "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg",
        "", "",
        "donor phone number", "texts", "CD 1 01 53 58 1 Central SS",
    ]]
    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    _, metadata = sent[0]
    assert "title" in metadata
    for excluded in (
        "notes_lcps_internal",
        "ia_identifier", "ia_uploaded", "ia_url", "ia_identifier_bib",
        "ia_sync_hash", "ia_last_synced",
        "mediatype",
        "identifier",
        "file",
    ):
        assert excluded not in metadata, excluded


def test_sync_from_sheet_skips_rows_that_are_not_uploaded_yet(tmp_path, monkeypatch):
    """An UNASSIGNED row has no item to correct, and a RESERVED row whose
    upload never confirmed is upload's problem, not this command's."""
    from ia_bulk import cmd_sync_metadata

    grid = _synced_grid([
        ["Uploaded", "photo1.jpg", "lcps-astoriaphotos-00001",
         "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg"],
        ["Never uploaded", "photo2.jpg", "", "", "", ""],
        ["Reserved only", "photo3.jpg", "lcps-astoriaphotos-00002", "", "", ""],
    ])
    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    assert [target for target, _ in sent] == [f"zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001"]
    assert exit_code == 0


def test_sync_from_sheet_reports_an_uploaded_row_with_no_usable_url(
    tmp_path, monkeypatch, capsys
):
    """A row the operator edited expecting the edit to reach the site must not
    be silently skipped."""
    from ia_bulk import cmd_sync_metadata

    grid = _synced_grid([
        ["Lost its url", "photo1.jpg", "lcps-astoriaphotos-00001",
         "2026-08-23T16:13:31Z", "", "photo1.jpg"],
    ])
    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert sent == []
    assert "does not look like an Internet Archive item URL" in out
    assert exit_code == 1


def test_sync_from_sheet_refuses_to_send_a_live_correction_to_a_test_item(
    tmp_path, monkeypatch, capsys
):
    """Not recoverable by rerunning, so it is refused rather than reported
    afterwards."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, live=True))
    out = capsys.readouterr().out

    assert sent == []
    assert "Refusing to send a live correction to a rehearsal item" in out
    assert exit_code == 1


def test_sync_from_sheet_refuses_to_send_a_test_correction_to_a_real_item(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_sync_metadata

    grid = _synced_grid([
        ["Real item", "photo1.jpg", "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z",
         "https://archive.org/details/lcps-astoriaphotos-00001", "photo1.jpg"],
    ])
    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert sent == []
    assert "Refusing to send a rehearsal correction to a permanent item" in out
    assert exit_code == 1


def test_sync_from_sheet_refuses_an_item_belonging_to_another_project(
    tmp_path, monkeypatch, capsys
):
    """Issue #2 on the Sheet path. This command sends metadata to whatever
    item ia_url names, so a cell pointing at another project's item does not
    misfile this row - it overwrites that project's item."""
    from ia_bulk import cmd_sync_metadata

    grid = _synced_grid([
        ["Somebody else's item", "photo1.jpg", "lcps-astoriaphotos-00001",
         "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-otherproject-00099",
         "photo1.jpg"],
    ])
    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert sent == []
    assert "does not belong to this run's --project astoriaphotos" in out
    assert exit_code == 1


def test_item_project_id_reads_through_a_test_stamp():
    """The stamp is dropped, not parsed - a test item's project is the one in
    the real identifier it wraps."""
    from ia_bulk import item_project_id

    assert item_project_id(f"zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001", live=False) == (
        "astoriaphotos"
    )
    assert item_project_id("lcps-astoriaphotos-00001", live=True) == "astoriaphotos"
    assert item_project_id("something-a-human-pasted", live=True) is None


def test_sync_from_sheet_counts_an_unchanged_item_as_unchanged_not_a_failure(
    tmp_path, monkeypatch, capsys
):
    """This is what makes "send every DONE row" idempotent, so no per-row
    change detection and no fifth ia_ column are needed."""
    from ia_bulk import MetadataUnchanged, cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = FakeSheetClient(_synced_grid())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)

    def unchanged(metadata, target):
        raise MetadataUnchanged(target)

    monkeypatch.setattr("ia_bulk.update_metadata_row", unchanged)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert "0 item(s) updated successfully, 1 unchanged, 0 error(s)" in out
    assert exit_code == 0


def test_sync_from_sheet_logs_an_already_current_item_as_unchanged(tmp_path, monkeypatch, capsys):
    """Row 2's item already matches; row 3's takes the edit."""
    from ia_bulk import MetadataUnchanged, cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _two_synced_rows(), sent)

    def unchanged_for_the_first_item(metadata, target):
        if target.endswith("-00001"):
            raise MetadataUnchanged(target)

    monkeypatch.setattr("ia_bulk.update_metadata_row", unchanged_for_the_first_item)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    capsys.readouterr()

    assert exit_code == 0
    log_file = next((tmp_path / "logs").glob("sync-metadata-*.jsonl"))
    statuses = {entry["identifier"]: entry["status"] for entry in _row_records(log_file)}
    assert statuses == {
        "lcps-astoriaphotos-00001": "unchanged",
        "lcps-astoriaphotos-00002": "success",
    }


def test_sync_from_sheet_dry_run_shows_what_would_change_not_just_field_names(
    tmp_path, monkeypatch, capsys
):
    """Listing field names alone made the dry run unable to answer the one
    question it is run to answer - "did my edit get picked up?" - because
    editing a description on a row that already had one produced byte-
    identical output."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)
    monkeypatch.setattr(
        "ia_bulk.fetch_current_metadata",
        lambda identifier: {"title": "Stone Customshuose"},
    )

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert sent == []
    assert f"zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001" in out
    # the typo and its correction are both visible, in place
    assert "now: Stone Customshuose" in out
    assert "new: Stone Customshouse" in out
    assert "1 of 1 item would change" in out
    assert not (tmp_path / "logs").exists()
    assert exit_code == 0


def test_sync_from_sheet_dry_run_reports_an_item_that_already_matches(
    tmp_path, monkeypatch, capsys
):
    """The run's `unchanged` count, visible BEFORE anything is sent."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)
    monkeypatch.setattr(
        "ia_bulk.fetch_current_metadata",
        lambda identifier: {"title": "Stone Customshouse"},
    )

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert "0 of 1 item would change" in out
    assert "1 already match" in out
    assert "now:" not in out


def test_sync_from_sheet_dry_run_survives_an_item_it_cannot_read(
    tmp_path, monkeypatch, capsys
):
    """A dry run that cannot reach one item should still report the rest."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)
    monkeypatch.setattr("ia_bulk.fetch_current_metadata", lambda identifier: None)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert "could not read its current metadata" in out
    assert "1 item could not be read" in out
    assert exit_code == 0


def test_sync_dry_run_reports_the_gate_before_reading_anything(
    tmp_path, monkeypatch, capsys
):
    """Preview in the destination's vocabulary, and say what is left
    untouched. It also cuts the dry run's Internet Archive reads from one per
    uploaded row to one per CHANGED row - on the steady state, from ~4,000
    to none."""
    from ia_bulk import cmd_sync_metadata

    reads = []
    monkeypatch.setattr(
        "ia_bulk.fetch_current_metadata", lambda identifier: reads.append(identifier) or {}
    )

    rows = [
        ["Photo 1", "photo1.jpg", "lcps-astoriaphotos-00001", "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00001",
         "photo1.jpg", "", ""],
    ]
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = RecordingSheetClient(_synced_grid(rows), SheetUploadRecorder())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)
    monkeypatch.setattr("ia_bulk.update_metadata_row", lambda metadata, target: None)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))   # stamps row 2
    reads.clear()
    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert reads == []
    assert "already in sync" in out
    assert "would not be sent" in out


def test_sync_dry_run_does_not_stamp_anything(tmp_path, monkeypatch):
    """--dry-run sends nothing, so there is nothing to record having sent.
    Stamping here would make the next real run skip the rows the dry run
    only previewed."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, client = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), sent)
    monkeypatch.setattr("ia_bulk.fetch_current_metadata", lambda identifier: {})

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, dry_run=True))

    assert sent == []
    assert _pushed_rows(client) == []


def test_sync_dry_run_reports_a_never_stamped_row_as_having_no_push_on_record(
    monkeypatch, capsys
):
    """A blank hash means never stamped, not edited."""
    from ia_bulk import print_sync_dry_run

    monkeypatch.setattr("ia_bulk.fetch_current_metadata", lambda identifier: {})

    print_sync_dry_run([_sync_target(content_hash="new", stored_hash="")], [], [])
    gate_line = capsys.readouterr().out.splitlines()[0]

    assert gate_line == (
        "1 uploaded row; 1 with no push on record, 0 changed since their last push, "
        "0 already in sync and would not be sent"
    )


def test_sync_dry_run_reports_a_row_with_a_stale_hash_as_changed(monkeypatch, capsys):
    """A non-blank hash that no longer matches is a real edit."""
    from ia_bulk import print_sync_dry_run

    monkeypatch.setattr("ia_bulk.fetch_current_metadata", lambda identifier: {})

    print_sync_dry_run([_sync_target(content_hash="new", stored_hash="old")], [], [])
    gate_line = capsys.readouterr().out.splitlines()[0]

    assert gate_line == (
        "1 uploaded row; 0 with no push on record, 1 changed since their last push, "
        "0 already in sync and would not be sent"
    )


def test_metadata_changes_treats_a_blank_cell_as_leave_alone():
    """update_metadata_row drops blanks, so a dry run that called one a change
    would predict something the real run does not do."""
    from ia_bulk import metadata_changes

    assert metadata_changes({"title": "", "date": "   "}, {"title": "On IA"}) == []


def test_metadata_changes_shows_remove_tag_as_a_deletion():
    from ia_bulk import metadata_changes

    assert metadata_changes({"rights": "REMOVE_TAG"}, {"rights": "CC0"}) == [
        ("rights", "CC0", "(deleted)")
    ]


def test_metadata_changes_ignores_remove_tag_for_a_field_that_is_not_there():
    """Removing what is not present changes nothing."""
    from ia_bulk import metadata_changes

    assert metadata_changes({"rights": "REMOVE_TAG"}, {"title": "T"}) == []


def test_metadata_changes_reports_a_field_internet_archive_does_not_have_yet():
    from ia_bulk import metadata_changes

    assert metadata_changes({"description": "New"}, {}) == [
        ("description", "(not set)", "New")
    ]


def test_metadata_changes_joins_a_repeated_ia_field_before_comparing():
    """Internet Archive returns a list for a field that occurs more than once;
    comparing that to a string would report every such field as changed."""
    from ia_bulk import metadata_changes

    assert metadata_changes({"subject": "a; b"}, {"subject": ["a", "b"]}) == []


def test_metadata_changes_elides_a_very_long_value():
    from ia_bulk import DRY_RUN_VALUE_WIDTH, metadata_changes

    (_, current, new) = metadata_changes({"description": "x" * 500}, {"description": "y"})[0]
    assert current == "y"
    assert len(new) == DRY_RUN_VALUE_WIDTH
    # ASCII: a Windows console codepage that cannot encode U+2026 raises
    # UnicodeEncodeError and truncates the report mid-run.
    assert new.endswith("...")
    assert new.isascii()


# --- issue #4: a failing row's error reaches the console, not only the log ---


def test_format_row_error_collapses_a_multi_line_message_to_one_line():
    """Internet Archive's S3 failures carry a multi-line XML body. Dumped raw
    under a progress line it swamps the [N/M] rhythm the operator reads."""
    from ia_bulk import format_row_error

    exc = RuntimeError("failed with status 503:\n  <Error>\n    <Code>SlowDown</Code>\n  </Error>")

    line = format_row_error(exc)

    assert "\n" not in line
    assert line == "failed with status 503: <Error> <Code>SlowDown</Code> </Error>"


def test_format_row_error_truncates_a_very_long_message():
    """The log keeps the complete text; the console keeps the rhythm."""
    from ia_bulk import CONSOLE_ERROR_WIDTH, format_row_error

    line = format_row_error(RuntimeError("x" * 5000))

    assert len(line) == CONSOLE_ERROR_WIDTH
    assert line.endswith("...")
    assert line.isascii()


def test_format_row_error_leaves_a_short_message_alone():
    from ia_bulk import format_row_error

    message = "Error retrieving metadata: ReadTimeoutError, read timeout=12"
    assert format_row_error(RuntimeError(message)) == message


def test_cmd_upload_prints_why_a_row_failed_not_only_the_count(
    tmp_path, monkeypatch, capsys
):
    """Before this, the entire console output for a failed row was
    "0 file(s) uploaded successfully, 1 error(s)" plus a path to a JSONL log
    - a dead end for a volunteer comfortable with spreadsheets and not with
    code. The information already existed in the log's `error` field; it just
    never reached the screen."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg",)
    )

    def timing_out(row, target_identifier, collection, files_dir):
        raise RuntimeError(
            "Error retrieving metadata from https://archive.org/metadata/"
            f"{target_identifier}\nReadTimeoutError: read timeout=12"
        )

    monkeypatch.setattr("ia_bulk.upload_row", timing_out)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert "ReadTimeoutError: read timeout=12" in out
    # under the failing row, in validate's per-row error style
    assert "    - Error retrieving metadata" in out
    assert "0 file(s) uploaded successfully, 1 error(s)" in out
    assert exit_code == 1


def test_sync_from_sheet_prints_why_a_row_failed(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = FakeSheetClient(_synced_grid())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)

    def refused(metadata, target):
        raise RuntimeError("no such item")

    monkeypatch.setattr("ia_bulk.update_metadata_row", refused)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert "    - no such item" in out
    assert "0 item(s) updated successfully, 0 unchanged, 1 error(s)" in out
    assert exit_code == 1


def test_a_successful_row_prints_no_error_line(tmp_path, monkeypatch, capsys):
    """The error line is a signal, not a banner."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert "    - " not in out
    assert exit_code == 0


def test_not_attempted_does_not_double_count_a_moved_row_from_an_earlier_chunk(
    tmp_path, monkeypatch, capsys
):
    """Pins the `settled` accounting in SheetUploadRun.execute().

    The count used to be `total - done - len(moved)`, computed against only
    the CURRENT chunk's moved list. A row reported as moved in an EARLIER
    chunk was therefore counted twice: once when it was reported, and again
    inside `total - done` when a later chunk stopped the run. A three-target
    run could report "4 rows not attempted".

    This is the shape no shipped test had, which is why the fix went in
    unpinned - restoring the old formula left the whole suite green. It needs
    all three of: more than one chunk, a moved row in an EARLIER chunk, and a
    run-stopping failure in a LATER one.

    Three targets at chunk_size 1:
      chunk 1 - row 2 has moved, so it is reported and settled. Its reserve
                batch is empty, so no Sheet write happens and the run
                continues to the next chunk.
      chunk 2 - row 3 verifies clean and its reserve write fails, stopping
                the run.

    Correct: 1 moved + 2 never attempted = 3. The old formula gave 4.
    """
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in (1, 2, 3)
    ]

    def move_row_2_before_the_first_chunk_verifies(live_grid, read_count):
        # read 1 is the initial grid read; read 2 is chunk 1's pre-reserve
        # guard. Changing the file cell changes that row's fingerprint, which
        # is what split_moved_targets compares.
        if read_count == 2:
            live_grid[1][1] = "somethingelse.jpg"

    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        before_read=move_row_2_before_the_first_chunk_verifies,
        # chunk 1 writes nothing - its only target moved, so the reserve batch
        # is empty - which makes chunk 2's reserve the run's first real write.
        raise_on_write=1,
    )

    exit_code = cmd_upload(
        make_upload_args(tmp_path, registry_path, write_identifier=True, chunk_size=1)
    )
    out = capsys.readouterr().out

    # The count itself: 1 moved + 2 never reached. Not 4.
    assert "3 rows not attempted" in out
    assert "4 rows not attempted" not in out
    # and the run really did stop before uploading anything
    assert recorder.uploads == []
    assert exit_code == 1


def test_not_attempted_summary_says_where_to_look(tmp_path, monkeypatch, capsys):
    """The wording is load-bearing and was also unpinned: the covering test
    asserted only that the count appeared, so reverting "see the reason on
    stderr above" to a vaguer "see the messages above" left the suite green.
    The reason for stopping is printed to STDERR while this summary is on
    STDOUT, so an operator who piped one away needs to be told which stream
    to go looking in."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in (1, 2)
    ]
    recorder, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        raise_on_write=1,
    )

    cmd_upload(
        make_upload_args(tmp_path, registry_path, write_identifier=True, chunk_size=1)
    )
    out = capsys.readouterr().out

    assert "not attempted - the run stopped early; see the reason on stderr above" in out


# --- issue #3: sync-metadata's live path, which writes to permanent items ---


def _live_grid():
    """A Sheet whose rows were uploaded by a --live run: ia_url names the
    real, unstamped item."""
    return _synced_grid([
        ["Stone Customshouse", "photo1.jpg", "lcps-astoriaphotos-00001",
         "2026-08-23T16:13:31Z",
         "https://archive.org/details/lcps-astoriaphotos-00001", "photo1.jpg"],
    ])


def test_sync_from_sheet_sends_a_live_correction_to_the_real_item(
    tmp_path, monkeypatch, capsys
):
    """The only success-case test of a path that writes permanent metadata to
    real archival items. Every other live-mode assertion in the suite is a
    refusal, which proves the guards fire but never that a correction the
    operator meant to make actually goes out.

    Internet Archive can darken an item on request but cannot rename it, and
    metadata edits are permanent enough to be worth failing loudly over -
    `upload` has success coverage for its live path; this did not."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _live_grid(), sent)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, live=True))
    out = capsys.readouterr().out

    assert len(sent) == 1
    target, metadata = sent[0]
    # the real, permanent identifier - no zztest- prefix, no run stamp
    assert target == "lcps-astoriaphotos-00001"
    assert not target.startswith("zztest-")
    assert metadata["title"] == "Stone Customshouse"
    assert "1 item(s) updated successfully" in out
    assert exit_code == 0


def test_sync_from_sheet_live_reads_the_real_spreadsheet_not_the_test_one(
    tmp_path, monkeypatch, capsys
):
    """A live correction read from the TEST Sheet would send that Sheet's
    metadata to real items. sheet_id and test_sheet_id are deliberately
    different values in the fixture so this is answerable rather than
    coincidental."""
    from ia_bulk import cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    # Two DIFFERENT grids behind the two spreadsheet ids, so reading the wrong
    # one sends the wrong metadata and this test can actually fail. Asserting
    # only on the printed sheet id would prove the right id was reported, not
    # that it was the one read.
    grids = {
        "REAL_SHEET_ID": _live_grid(),
        "TEST_SHEET_ID": _synced_grid([
            ["REHEARSAL TITLE", "photo1.jpg", "lcps-astoriaphotos-00001",
             "2026-08-23T16:13:31Z",
             "https://archive.org/details/lcps-astoriaphotos-00001", "photo1.jpg"],
        ]),
    }
    monkeypatch.setattr(
        "ia_bulk.build_sheet_client",
        lambda config, live: FakeSheetClient(grids[config.sheet_id_for(live)]),
    )
    sent = []
    monkeypatch.setattr(
        "ia_bulk.update_metadata_row",
        lambda metadata, target: sent.append((target, metadata)),
    )

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, live=True))
    out = capsys.readouterr().out

    assert "live mode" in out
    assert "REAL_SHEET_ID" in out
    assert "TEST_SHEET_ID" not in out
    # the metadata that went out came from the REAL Sheet
    assert sent[0][1]["title"] == "Stone Customshouse"
    assert sent[0][1]["title"] != "REHEARSAL TITLE"


def test_sync_from_sheet_live_records_the_mode_in_its_log(tmp_path, monkeypatch):
    """The log's `live` field is the audit record of which mode a run used;
    a live run recorded as a test run would misstate what touched real items."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _live_grid(), sent)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, live=True))

    log_file = next((tmp_path / "logs").glob("sync-metadata-*.jsonl"))
    entries = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").strip().splitlines()
    ]
    header = [e for e in entries if e.get("record") == "run_header"]
    # "record" marks a run-level line (header, closing summary); a row result
    # carries no such key.
    rows = [e for e in entries if "record" not in e]

    assert header and header[0]["live"] is True
    assert [e["status"] for e in rows] == ["success"]
    assert all(e["live"] is True for e in rows)
    assert rows[0]["uploaded_as"] == "lcps-astoriaphotos-00001"


def test_sync_from_sheet_live_reports_a_failure_without_claiming_success(
    tmp_path, monkeypatch, capsys
):
    """A refused live edit must not read as a clean run - this is the command
    whose failures are least recoverable."""
    from ia_bulk import cmd_sync_metadata

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )
    client = FakeSheetClient(_live_grid())
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: client)

    def refused(metadata, target):
        raise RuntimeError("Access Denied - This item has been taken offline")

    monkeypatch.setattr("ia_bulk.update_metadata_row", refused)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, live=True))
    out = capsys.readouterr().out

    assert "    - Access Denied - This item has been taken offline" in out
    assert "0 item(s) updated successfully, 0 unchanged, 1 error(s)" in out
    assert exit_code == 1


# Issue #25: the machine-consumable run summary


def _sync_log_entries(tmp_path):
    log_file = next((tmp_path / "logs").glob("sync-metadata-*.jsonl"))
    return [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").strip().splitlines()
    ]


def _two_synced_rows():
    """Two already-uploaded rows, each naming its own test item."""
    return _synced_grid([
        ["Stone Customshouse", "photo1.jpg", "lcps-astoriaphotos-00001",
         "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg"],
        ["Flavel House", "photo2.jpg", "lcps-astoriaphotos-00002",
         "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002",
         "photo2.jpg"],
    ])


def test_sync_from_sheet_ends_with_a_machine_readable_summary(tmp_path, monkeypatch):
    """A scheduled, unattended run has to be reviewable without a human
    reading console output or replaying every per-row record."""
    from ia_bulk import cmd_sync_metadata

    sent = []
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _two_synced_rows(), sent)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    summary = _sync_log_entries(tmp_path)[-1]

    assert summary["record"] == "run_summary"
    assert summary["checked"] == 2
    assert summary["pushed"] == 2
    assert summary["changed"] == 2
    assert summary["unchanged"] == 0
    assert summary["failures"] == []
    assert summary["skipped"] == []


def test_the_summary_names_each_failing_row_and_why_it_failed(tmp_path, monkeypatch):
    """The failure list is what makes the summary actionable rather than
    merely countable - a run reporting "1 error(s)" and nothing else sends
    the reader back to the per-row lines this record exists to replace."""
    from ia_bulk import cmd_sync_metadata

    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _two_synced_rows(), [])

    def refuse_the_second(metadata, target):
        if target.endswith("00002"):
            raise RuntimeError("Access Denied - This item has been taken offline")

    monkeypatch.setattr("ia_bulk.update_metadata_row", refuse_the_second)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    summary = _sync_log_entries(tmp_path)[-1]

    assert summary["failures"] == [{
        "identifier": "lcps-astoriaphotos-00002",
        "error": "Access Denied - This item has been taken offline",
    }]
    assert summary["changed"] == 1
    assert summary["pushed"] == 2


def test_a_row_that_was_never_sent_is_skipped_not_failed(tmp_path, monkeypatch):
    """The distinction the summary exists to preserve. A failure means the
    item was contacted and refused the edit; a skip means nothing was sent at
    all. Flattening the two would leave a reader months from now unable to
    tell "this item may not be in the state I intended" from "this item was
    never touched"."""
    from ia_bulk import cmd_sync_metadata

    grid = _synced_grid([
        ["Stone Customshouse", "photo1.jpg", "lcps-astoriaphotos-00001",
         "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg"],
        # Marked uploaded, but nothing says which item it became.
        ["Flavel House", "photo2.jpg", "lcps-astoriaphotos-00002",
         "2026-08-23T16:13:31Z", "", "photo2.jpg"],
    ])
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, [])

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))

    summary = _sync_log_entries(tmp_path)[-1]

    assert summary["failures"] == []
    assert [entry["identifier"] for entry in summary["skipped"]] == [
        "lcps-astoriaphotos-00002"
    ]
    assert "ia_url" in summary["skipped"][0]["error"]
    assert summary["pushed"] == 1
    assert summary["checked"] == 2
    assert exit_code == 1


def test_the_summary_and_the_console_cannot_disagree_about_a_mixed_run(
    tmp_path, monkeypatch, capsys
):
    """The acceptance criterion, exercised where drift would actually show:
    one run producing all four outcomes at once. Both the record and the
    lines printed come from one SyncSummary, so this pins that they still do
    - and that `pushed` stays the sum of the three send results rather than a
    fourth number kept alongside them."""
    from ia_bulk import cmd_sync_metadata, MetadataUnchanged

    grid = _synced_grid([
        ["Stone Customshouse", "photo1.jpg", "lcps-astoriaphotos-00001",
         "2026-08-23T16:13:31Z", SYNC_URL, "photo1.jpg"],
        ["Flavel House", "photo2.jpg", "lcps-astoriaphotos-00002",
         "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00002",
         "photo2.jpg"],
        ["Astoria Column", "photo3.jpg", "lcps-astoriaphotos-00003",
         "2026-08-23T16:13:31Z",
         f"https://archive.org/details/zztest-{SYNC_STAMP}-lcps-astoriaphotos-00003",
         "photo3.jpg"],
        # Marked uploaded, but nothing records which item it became.
        ["Liberty Theatre", "photo4.jpg", "lcps-astoriaphotos-00004",
         "2026-08-23T16:13:31Z", "", "photo4.jpg"],
    ])
    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, grid, [])

    def one_of_each(metadata, target):
        if target.endswith("00002"):
            raise MetadataUnchanged(target)
        if target.endswith("00003"):
            raise RuntimeError("Access Denied - This item has been taken offline")

    monkeypatch.setattr("ia_bulk.update_metadata_row", one_of_each)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    summary = _sync_log_entries(tmp_path)[-1]

    assert summary["checked"] == 4
    assert summary["changed"] == 1
    assert summary["unchanged"] == 1
    assert len(summary["failures"]) == 1
    assert len(summary["skipped"]) == 1
    # pushed counts the rows sent, and only those: the skipped row is not one.
    assert summary["pushed"] == 3
    assert summary["pushed"] == (
        summary["changed"] + summary["unchanged"] + len(summary["failures"])
    )
    assert (
        f"{summary['changed']} item(s) updated successfully, "
        f"{summary['unchanged']} unchanged, {len(summary['failures'])} error(s)"
    ) in out
    assert f"{len(summary['skipped'])} row skipped (not safely targetable)" in out
    assert exit_code == 1


def test_dry_run_writes_no_summary_because_it_writes_no_log(tmp_path, monkeypatch, capsys):
    """A rehearsal sends nothing, so it has no run to summarize. Writing one
    anyway would leave logs whose summaries describe work that never
    happened, in the same directory a real run's summaries are read from."""
    from ia_bulk import cmd_sync_metadata

    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), [])

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path, dry_run=True))

    assert list((tmp_path / "logs").glob("sync-metadata-*.jsonl")) == []


def test_an_unwritable_summary_does_not_fail_a_run_that_reached_the_archive(
    tmp_path, monkeypatch, capsys
):
    """The summary is a record of the run, not part of it. A full disk at the
    last line must not turn a clean sync into a reported failure - the run
    already changed permanent metadata, and reporting it as failed invites a
    rerun of work that succeeded."""
    from ia_bulk import cmd_sync_metadata

    registry_path, _ = _setup_sync_sheet(tmp_path, monkeypatch, _synced_grid(), [])

    def full_disk(log_path, record):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("ia_bulk.log_run_summary", full_disk)

    exit_code = cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "1 item(s) updated successfully" in captured.out
    assert "No space left on device" in captured.err


# Task 5: The prompt tests
def test_prompt_accepts_a_proposal(capsys):
    from ia_bulk import Decision, prompt_for_decision
    from reconcile import Proposal

    decision = prompt_for_decision(
        3, "SOP CD 1", "Finnis.jpg", Proposal("Finnish.jpg", "edit distance 1"),
        ["Finnish.jpg"], _sheet_config(), set(), read_line=lambda _: "y",
    )
    assert decision == Decision(action="accept", filename="Finnish.jpg")


def test_prompt_rejects_without_a_filename():
    from ia_bulk import Decision, prompt_for_decision
    from reconcile import Proposal

    decision = prompt_for_decision(
        3, "SOP CD 1", "Finnis.jpg", Proposal("Finnish.jpg", "edit distance 1"),
        ["Finnish.jpg"], _sheet_config(), set(), read_line=lambda _: "n",
    )
    assert decision == Decision(action="reject", filename="")


def test_prompt_typed_name_is_resolved_before_it_is_accepted(tmp_path):
    """The whole point: a typed name goes through the same resolve_file()
    every other path uses, so it cannot introduce a new broken cell - and a
    name typed without its extension resolves anyway."""
    from ia_bulk import prompt_for_decision

    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Finnish Meat Market.jpg").write_bytes(b"x")
    config = _sheet_config(files_dir=str(tmp_path))

    answers = iter(["e", "Finnish Meat Market"])   # no extension
    decision = prompt_for_decision(
        3, "SOP CD 1", "Finnis.jpg", None, [], config, set(),
        read_line=lambda _: next(answers),
    )
    assert decision.action == "accept"
    assert decision.filename == "Finnish Meat Market.jpg"   # the RESOLVED name
    # how it was answered, not what was answered - the decision log has to
    # tell a typed name from an accepted proposal even when they agree
    assert decision.typed is True


def test_prompt_reprompts_when_a_typed_name_does_not_resolve(tmp_path, capsys):
    from ia_bulk import prompt_for_decision

    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Real.jpg").write_bytes(b"x")
    config = _sheet_config(files_dir=str(tmp_path))

    answers = iter(["e", "Nonexistent", "e", "Real", ])
    decision = prompt_for_decision(
        3, "SOP CD 1", "Finnis.jpg", None, [], config, set(),
        read_line=lambda _: next(answers),
    )
    assert decision.filename == "Real.jpg"
    assert "no file found" in capsys.readouterr().out.lower()


def test_prompt_refuses_a_typed_name_another_row_already_claims(tmp_path, capsys):
    """Typing must not be a way around the one-file-one-row rule."""
    from ia_bulk import prompt_for_decision

    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Taken.jpg").write_bytes(b"x")
    (folder / "Free.jpg").write_bytes(b"x")
    config = _sheet_config(files_dir=str(tmp_path))

    answers = iter(["e", "Taken", "e", "Free"])
    decision = prompt_for_decision(
        3, "SOP CD 1", "Finnis.jpg", None, [], config,
        {claim_key("SOP CD 1/Taken.jpg")}, read_line=lambda _: next(answers),
    )
    assert decision.filename == "Free.jpg"
    assert "already" in capsys.readouterr().out.lower()


def test_prompt_lists_unclaimed_files_on_demand(capsys):
    from ia_bulk import prompt_for_decision

    answers = iter(["l", "n"])
    prompt_for_decision(
        3, "SOP CD 1", "Finnis.jpg", None, ["Alpha.jpg", "Beta.jpg"],
        _sheet_config(), set(), read_line=lambda _: next(answers),
    )
    out = capsys.readouterr().out
    assert "Alpha.jpg" in out and "Beta.jpg" in out


def test_prompt_reprints_the_keys_after_a_bounce_back_to_the_prompt(tmp_path, capsys):
    """After [l]'s listing or a failed [e], the operator lands back at the
    key-choice '>' - but the keys row has scrolled away, and a bare '>'
    does not say which prompt this is. Reprinting it makes the bounce
    visible."""
    from ia_bulk import prompt_for_decision

    folder = tmp_path / "SOP CD 1"
    folder.mkdir()
    (folder / "Real.jpg").write_bytes(b"x")
    config = _sheet_config(files_dir=str(tmp_path))

    answers = iter(["l", "e", "Nonexistent", "e", "", "n"])
    prompt_for_decision(
        3, "SOP CD 1", "Finnis.jpg", None, ["Real.jpg"], config, set(),
        read_line=lambda _: next(answers),
    )
    out = capsys.readouterr().out
    # once up front, then after the listing, the failed resolve, and the
    # empty entry
    assert out.count("[e] type it") == 4


def test_prompt_stops_the_run():
    from ia_bulk import Decision, prompt_for_decision

    decision = prompt_for_decision(
        3, "SOP CD 1", "Finnis.jpg", None, [], _sheet_config(), set(),
        read_line=lambda _: "q",
    )
    assert decision == Decision(action="stop", filename="")


# --- Task 6: the reconcile-files command ---

RECONCILE_HEADER = ["Folder on LaCie Drive", "File Name", "Title"]


def _setup_reconcile(tmp_path, monkeypatch, sheet_rows, disk, decisions, header=None,
                     grid_after=None):
    """A reconcile world: files on disk, a registry, a client that records
    every write, and a canned answer per prompt."""
    for folder, names in disk.items():
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        for name in names:
            (tmp_path / folder / name).write_bytes(b"x")

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            make_sheet_registry(
                files_dir=str(tmp_path),
                file_template="{folder_on_lacie_drive}/{file_name}",
                required_for_upload=["title"],
            )
        ),
        encoding="utf-8",
    )

    written = []

    class RecordingClient:
        def __init__(self, grid):
            self._grid = grid
            self._reads = 0

        def read_grid(self):
            # `grid_after` is what the Sheet says on every read AFTER the
            # first - i.e. what flush()'s re-read sees once somebody else has
            # edited the Sheet mid-session.
            self._reads += 1
            if grid_after is not None and self._reads > 1:
                return grid_after
            return self._grid

        def write_cells(self, updates):
            written.extend(updates)

    grid = [header or RECONCILE_HEADER] + sheet_rows
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: RecordingClient(grid))

    answers = iter(decisions)
    monkeypatch.setattr("ia_bulk.prompt_for_decision", lambda *a, **k: next(answers))
    return registry_path, written


def _reconcile_args(tmp_path, registry_path, **overrides):
    args = Namespace(
        project="astoriaphotos",
        registry=str(registry_path),
        live=False,
        dry_run=False,
        log_dir=str(tmp_path / "logs"),
    )
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def test_cmd_reconcile_files_writes_only_the_file_name_column(tmp_path, monkeypatch):
    """The acceptance criterion made mechanical: whatever else changes, the
    write batch must never contain another column letter. File Name is the
    second header, so column B."""
    from ia_bulk import Decision, cmd_reconcile_files

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "A title"]],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [Decision(action="accept", filename="Finnish Meat Market.jpg")],
    )

    assert cmd_reconcile_files(_reconcile_args(tmp_path, registry_path)) == 0
    assert {update.a1[0] for update in written} == {"B"}


def test_cmd_reconcile_files_applies_an_accepted_correction(tmp_path, monkeypatch):
    from ia_bulk import CellUpdate, Decision, cmd_reconcile_files

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "A title"]],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [Decision(action="accept", filename="Finnish Meat Market.jpg")],
    )

    cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))

    assert written == [CellUpdate("B2", "Finnish Meat Market.jpg")]


def test_cmd_reconcile_files_writes_nothing_when_rejected(tmp_path, monkeypatch):
    from ia_bulk import Decision, cmd_reconcile_files

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "A title"]],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [Decision(action="reject", filename="")],
    )

    cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))

    assert written == []


def _reconcile_log_lines(tmp_path):
    logs = sorted((tmp_path / "logs").glob("reconcile-files-*.jsonl"))
    assert len(logs) == 1
    return [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]


def test_cmd_reconcile_files_logs_what_was_decided_about_every_row(tmp_path, monkeypatch):
    """One line per row *considered*. Prompt-per-proposal leaves no other
    record of what a volunteer decided, so a rejected proposal has to say
    what was turned down and an ambiguous row has to name the files it
    could not choose between - the console says both and used to be the
    only place that did."""
    from ia_bulk import Decision, cmd_reconcile_files

    registry_path, _ = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [
            ["SOP CD 1", "Finnis Meat Market.jpg", "A title"],
            ["SOP CD 1", "Alderbrok Hall.jpg", "Another title"],
            ["SOP CD 2", "Liberty.jpg", "A third title"],
        ],
        {
            "SOP CD 1": ["Finnish Meat Market.jpg", "Alderbrook Hall.jpg"],
            "SOP CD 2": ["Liberty.JPG", "liberty.jpeg"],
        },
        [
            Decision(action="accept", filename="Finnish Meat Market.jpg"),
            Decision(action="reject", filename=""),
        ],
    )

    assert cmd_reconcile_files(_reconcile_args(tmp_path, registry_path)) == 0
    accepted, rejected, ambiguous = _reconcile_log_lines(tmp_path)

    assert accepted["row"] == 2
    assert accepted["folder"] == "SOP CD 1"
    assert accepted["wanted"] == "Finnis Meat Market.jpg"
    assert accepted["status"] == "accepted"
    assert accepted["chosen"] == "Finnish Meat Market.jpg"
    assert accepted["proposed"] == "Finnish Meat Market.jpg"
    assert accepted["reason"] == "edit distance 1"
    assert accepted["matches"] == []
    assert accepted["timestamp"].endswith("Z")

    assert rejected["row"] == 3
    assert rejected["status"] == "rejected"
    assert rejected["chosen"] == ""
    assert rejected["proposed"] == "Alderbrook Hall.jpg"   # what was turned down
    assert rejected["reason"] == "edit distance 1"

    assert ambiguous["row"] == 4
    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["chosen"] == ""
    assert sorted(ambiguous["matches"]) == ["Liberty.JPG", "liberty.jpeg"]


def test_cmd_reconcile_files_logs_a_typed_name_as_typed_even_when_it_matches_the_proposal(
    tmp_path, monkeypatch
):
    """`typed` records how the operator answered, not whether the two
    strings agree. Comparing them called a name a human typed at [e]
    `accepted`, claiming the tool proposed something it did not."""
    from ia_bulk import Decision, cmd_reconcile_files

    registry_path, _ = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "A title"]],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [Decision(action="accept", filename="Finnish Meat Market.jpg", typed=True)],
    )

    cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))
    (entry,) = _reconcile_log_lines(tmp_path)

    assert entry["status"] == "typed"
    assert entry["chosen"] == "Finnish Meat Market.jpg"
    assert entry["proposed"] == "Finnish Meat Market.jpg"


def test_cmd_reconcile_files_logs_a_stop(tmp_path, monkeypatch):
    from ia_bulk import Decision, cmd_reconcile_files

    registry_path, _ = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "A title"]],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [Decision(action="stop", filename="")],
    )

    cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))
    (entry,) = _reconcile_log_lines(tmp_path)

    assert entry["status"] == "stopped"
    assert entry["chosen"] == ""


def test_cmd_reconcile_files_drops_a_correction_whose_row_moved_mid_session(tmp_path, monkeypatch, capsys):
    """The longest read-to-write window in the tool: an hour of prompting
    can sit between the grid read that fixed the row number and the write
    that uses it, on a Sheet several volunteers share. One row inserted in
    that hour shifts every later write by one, putting a filename on the
    wrong photograph. flush() re-reads and drops anything whose cell no
    longer says what it said when it matched."""
    from ia_bulk import Decision, cmd_reconcile_files

    original = ["SOP CD 1", "Finnis Meat Market.jpg", "A title"]
    inserted = ["SOP CD 1", "Someone Else's Row.jpg", "Inserted since"]

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [original],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [Decision(action="accept", filename="Finnish Meat Market.jpg")],
        grid_after=[RECONCILE_HEADER, inserted, original],
    )

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert written == []
    assert "was NOT written" in out
    assert "0 filename(s) corrected" in out
    assert "1 row still unresolved" in out
    assert exit_code == 0


def test_cmd_reconcile_files_writes_when_the_row_still_says_what_it_said(tmp_path, monkeypatch):
    """The other half of the guard: an unrelated edit elsewhere in the Sheet
    must not stop a correction whose own row is untouched."""
    from ia_bulk import CellUpdate, Decision, cmd_reconcile_files

    original = ["SOP CD 1", "Finnis Meat Market.jpg", "A title"]

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [original],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [Decision(action="accept", filename="Finnish Meat Market.jpg")],
        grid_after=[RECONCILE_HEADER, original, ["SOP CD 2", "Appended.jpg", "Later"]],
    )

    cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))

    assert written == [CellUpdate("B2", "Finnish Meat Market.jpg")]


def test_cmd_reconcile_files_refuses_a_sheet_whose_headers_collide(tmp_path, monkeypatch, capsys):
    """Two headers normalizing to `file_name` corrupt every row identically,
    so - unlike a bad row - the defect cannot be routed around by skipping
    it. It is not just a missing warning: grid_to_rows reads the LAST such
    column while the write targets the FIRST, so the correction would land
    in a column nothing reads, leaving the stale value in place and the run
    reporting success."""
    from ia_bulk import cmd_reconcile_files

    def refuse(*a, **k):
        raise AssertionError("a Sheet with a header defect must not be reconciled")

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "Finnis Meat Market.jpg", "A title"]],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [],
        header=["Folder on LaCie Drive", "File Name", "File  name", "Title"],
    )
    monkeypatch.setattr("ia_bulk.prompt_for_decision", refuse)

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert written == []
    assert "refusing to reconcile anything" in captured.err
    assert exit_code == 1


def test_cmd_reconcile_files_skips_a_row_whose_cells_are_shifted(tmp_path, monkeypatch, capsys):
    """A data row longer than the header may have every cell shifted against
    it, so which cell a correction would land in is exactly what is in
    doubt. One bad row is skipped, not fatal - the rest of the run still
    works, which is the split READINESS.md draws."""
    from ia_bulk import CellUpdate, Decision, cmd_reconcile_files

    prompted = []

    def record(row_number, folder, wanted, proposal, candidates, config, claimed):
        prompted.append(row_number)
        return Decision(action="accept", filename=proposal.filename)

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [
            ["SOP CD 1", "Finnis Meat Market.jpg", "A title", "an extra cell"],
            ["SOP CD 1", "Alderbrok Hall.jpg", "Another title"],
        ],
        {"SOP CD 1": ["Finnish Meat Market.jpg", "Alderbrook Hall.jpg"]},
        [],
    )
    monkeypatch.setattr("ia_bulk.prompt_for_decision", record)

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert prompted == [3]
    assert written == [CellUpdate("B3", "Alderbrook Hall.jpg")]
    assert "may have cells shifted" in out
    assert exit_code == 0


def test_cmd_reconcile_files_refuses_one_file_for_two_case_divergent_folder_cells(tmp_path, monkeypatch):
    """Two rows naming one photograph mint two permanent identifiers for one
    image. On Windows `SOP CD 1` and `sop cd 1` are the same folder, so
    unless both sides of the claimed check are normalized the second row is
    proposed - and accepts - the file the first row just took."""
    from ia_bulk import CellUpdate, Decision, cmd_reconcile_files

    def accept_whatever_is_proposed(row_number, folder, wanted, proposal, candidates, config, claimed):
        if proposal is None:
            return Decision(action="reject", filename="")
        return Decision(action="accept", filename=proposal.filename)

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [
            ["SOP CD 1", "Finnis Meat Market.jpg", "A title"],
            ["sop cd 1", "Finnish Meet Market.jpg", "Another title"],
        ],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [],
    )
    monkeypatch.setattr("ia_bulk.prompt_for_decision", accept_whatever_is_proposed)

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))

    assert written == [CellUpdate("B2", "Finnish Meat Market.jpg")]
    assert exit_code == 0


def test_cmd_reconcile_files_never_prompts_about_an_uncatalogued_row(tmp_path, monkeypatch, capsys):
    """The live Sheet is ~3,000 rows of which ~2,900 carry no filename at
    all. A row that asserted nothing has no proposal, no candidates and
    nothing an operator could decide - prompting about it buries the
    handful of genuinely fixable rows under thousands of identical
    non-problems, which is the exact failure the readiness split exists to
    prevent (docs/decisions/READINESS.md)."""
    from ia_bulk import Decision, cmd_reconcile_files

    prompted = []

    def record(row_number, folder, wanted, proposal, candidates, config, claimed):
        prompted.append(row_number)
        assert proposal is not None
        return Decision(action="accept", filename=proposal.filename)

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "A title"]]
        + [["SOP CD 1", "", ""] for _ in range(8)],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [],
    )
    monkeypatch.setattr("ia_bulk.prompt_for_decision", record)

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert prompted == [2]                      # the one real mismatch, not all nine
    assert "1 row named a file that does not resolve" in out
    assert "8 rows not yet catalogued" in out   # one contained line, not eight
    assert exit_code == 0


def test_cmd_reconcile_files_dry_run_says_nothing_about_uncatalogued_rows(tmp_path, monkeypatch, capsys):
    """--dry-run floods the same way a real run prompts: one
    `no candidate in ''` line per uncatalogued row."""
    from ia_bulk import cmd_reconcile_files

    registry_path, _ = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "", ""] for _ in range(8)],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [],
    )

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert "no candidate" not in out
    assert "8 rows not yet catalogued" in out
    assert "nothing to reconcile" in out
    assert exit_code == 0


def test_cmd_reconcile_files_dry_run_neither_prompts_nor_writes(tmp_path, monkeypatch, capsys):
    """--dry-run writes no log either, matching upload --dry-run, which
    returns before open_log()."""
    from ia_bulk import cmd_reconcile_files

    def refuse(*a, **k):
        raise AssertionError("--dry-run must not prompt")

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "A title"]],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [],
    )
    monkeypatch.setattr("ia_bulk.prompt_for_decision", refuse)

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert written == []
    assert not (tmp_path / "logs").exists()
    assert "Finnish Meat Market.jpg" in out
    assert exit_code == 0


def test_cmd_reconcile_files_on_a_clean_sheet_proposes_nothing(tmp_path, monkeypatch, capsys):
    """Safe to re-run: no drive changes, no mismatches, nothing proposed and
    nothing written."""
    from ia_bulk import cmd_reconcile_files

    def refuse(*a, **k):
        raise AssertionError("a resolvable row must not be prompted about")

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Good.jpg", "A title"]],
        {"SOP CD 1": ["Good.jpg"]},
        [],
    )
    monkeypatch.setattr("ia_bulk.prompt_for_decision", refuse)

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))

    assert written == []
    assert "nothing to reconcile" in capsys.readouterr().out
    assert exit_code == 0


def test_cmd_reconcile_files_flushes_before_stopping_on_q(tmp_path, monkeypatch):
    """[q] is a clean stop: corrections accepted before it reach the Sheet.
    Only an abrupt kill can lose a partial batch."""
    from ia_bulk import CellUpdate, Decision, cmd_reconcile_files

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [
            ["SOP CD 1", "Finnis Meat Market.jpg", "A title"],
            ["SOP CD 1", "Alderbrok Hall.jpg", "Another"],
        ],
        {"SOP CD 1": ["Finnish Meat Market.jpg", "Alderbrook Hall.jpg"]},
        [
            Decision(action="accept", filename="Finnish Meat Market.jpg"),
            Decision(action="stop", filename=""),
        ],
    )

    cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))

    assert written == [CellUpdate("B2", "Finnish Meat Market.jpg")]


def test_cmd_reconcile_files_exits_zero_with_rows_still_unresolved(tmp_path, monkeypatch, capsys):
    """Non-zero while work remains would return non-zero for months and teach
    the operator to ignore it - the trap upload's readiness split avoids."""
    from ia_bulk import Decision, cmd_reconcile_files

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Nothing Like This.jpg", "A title"]],
        {"SOP CD 1": ["Completely Different.jpg"]},
        [Decision(action="reject", filename="")],
    )

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))

    assert written == []
    assert "1 row still unresolved" in capsys.readouterr().out
    assert exit_code == 0


def test_cmd_reconcile_files_does_not_reoffer_a_file_claimed_earlier_this_run(tmp_path, monkeypatch):
    """Two rows in the same folder, both unresolvable, can each be within
    matching distance of the SAME single file on disk. Once row 2 claims it,
    row 3 must not be proposed it again - that is the exact misattribution
    FileSurvey's own docstring promises cannot happen, and 'unclaimed' alone
    cannot prevent it: it is a snapshot taken once, before the run started.

    Rather than a canned decision queue, this uses a fake prompt that mirrors
    what a real operator can do: accept whatever is proposed, or - when
    nothing is proposed - there is nothing to press [y] on. If the candidate
    pool has not been re-filtered against what earlier rows in this same run
    claimed, row 3 would still see the file as a candidate, propose_match
    would propose it a second time, and this fake would accept it too."""
    from ia_bulk import CellUpdate, Decision, cmd_reconcile_files

    def accept_whatever_is_proposed(row_number, folder, wanted, proposal, candidates, config, claimed):
        if proposal is None:
            return Decision(action="reject", filename="")
        return Decision(action="accept", filename=proposal.filename)

    registry_path, written = _setup_reconcile(
        tmp_path,
        monkeypatch,
        [
            ["SOP CD 1", "Finnis Meat Market.jpg", "A title"],
            ["SOP CD 1", "Finnish Meet Market.jpg", "Another title"],
        ],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
        [],
    )
    monkeypatch.setattr("ia_bulk.prompt_for_decision", accept_whatever_is_proposed)

    exit_code = cmd_reconcile_files(_reconcile_args(tmp_path, registry_path))

    assert written == [CellUpdate("B2", "Finnish Meat Market.jpg")]
    assert exit_code == 0


# --- append-rows: skeleton rows for files no row claims ---

def _setup_append(tmp_path, monkeypatch, sheet_rows, disk, header=None):
    """An append world: files on disk, a registry, and a client that records
    every appended batch."""
    for folder, names in disk.items():
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        for name in names:
            (tmp_path / folder / name).write_bytes(b"x")

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            make_sheet_registry(
                files_dir=str(tmp_path),
                file_template="{folder_on_lacie_drive}/{file_name}",
                required_for_upload=["title"],
            )
        ),
        encoding="utf-8",
    )

    appended = []

    class RecordingClient:
        def __init__(self, grid):
            self._grid = grid

        def read_grid(self):
            return self._grid

        def append_rows(self, rows):
            appended.extend(rows)

    grid = [header or RECONCILE_HEADER] + sheet_rows
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: RecordingClient(grid))
    return registry_path, appended


def _append_args(tmp_path, registry_path, **overrides):
    args = Namespace(
        project="astoriaphotos",
        registry=str(registry_path),
        live=False,
        dry_run=False,
        log_dir=str(tmp_path / "logs"),
    )
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def test_parser_append_rows_defaults():
    args = build_parser().parse_args(["append-rows", "--project", "astoriaphotos"])
    assert args.command == "append-rows"
    assert args.registry == "projects_registry.json"
    assert args.live is False
    assert args.dry_run is False
    assert args.log_dir == "logs"


def test_cmd_append_rows_appends_a_row_per_unclaimed_file_in_folder_then_name_order(
    tmp_path, monkeypatch, capsys
):
    """Files in folders no row names are exactly the point - see
    scan_unclaimed_files. Rows land grouped by folder A-Z, names A-Z within,
    padded to the header's width."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Good.jpg", "A title"]],
        {
            "SOP CD 2 COE": ["002_westport.JPG", "001_seaside.JPG"],
            "SOP CD 1": ["Good.jpg", "Extra.jpg"],
        },
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert appended == [
        ["SOP CD 1", "Extra.jpg", ""],
        ["SOP CD 2 COE", "001_seaside.JPG", ""],
        ["SOP CD 2 COE", "002_westport.JPG", ""],
    ]
    assert "3 rows appended" in out
    assert exit_code == 0


def test_cmd_append_rows_places_values_by_the_real_header_not_by_position(tmp_path, monkeypatch):
    """The two file columns can sit anywhere in the header. The appended row
    must carry its values at those columns - 'first two cells' would write a
    folder name into Title."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["A title", "SOP CD 1", "Good.jpg"]],
        {"SOP CD 1": ["Good.jpg", "Extra.jpg"]},
        header=["Title", "Folder on LaCie Drive", "File Name"],
    )

    cmd_append_rows(_append_args(tmp_path, registry_path))

    assert appended == [["", "SOP CD 1", "Extra.jpg"]]


def test_cmd_append_rows_refuses_while_any_row_fails_to_resolve(tmp_path, monkeypatch, capsys):
    """To the tool, "no row yet" and "row with a typo" both look like an
    unclaimed file (docs/decisions/RECONCILIATION.md, "Reconciliation ships
    before append"). Appending while any row is unresolved would add a
    second row for a photograph the typo'd row already means - so the guard
    is hard, with no override flag."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Finnis Meat Market.jpg", "A title"]],
        {"SOP CD 1": ["Finnish Meat Market.jpg"]},
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert appended == []
    assert "row 2" in captured.out
    assert "reconcile-files" in captured.err
    assert exit_code == 1


def test_cmd_append_rows_is_not_blocked_by_uncatalogued_rows(tmp_path, monkeypatch, capsys):
    """A row with blank file cells asserted no file - it cannot be hiding a
    typo, so it must not hold the append hostage. Counted in one line, the
    same split reconcile draws."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Good.jpg", "A title"], ["", "", ""]],
        {"SOP CD 1": ["Good.jpg", "Extra.jpg"]},
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert appended == [["SOP CD 1", "Extra.jpg", ""]]
    assert "1 row not yet catalogued" in out
    assert exit_code == 0


def test_cmd_append_rows_appends_nothing_when_every_file_is_claimed(tmp_path, monkeypatch, capsys):
    """Safe to re-run: rows a previous run appended resolve, so their files
    are claimed and a second run over an unchanged drive is a no-op."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Good.jpg", "A title"]],
        {"SOP CD 1": ["Good.jpg"]},
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))

    assert appended == []
    assert "nothing to append" in capsys.readouterr().out
    assert not (tmp_path / "logs").exists()
    assert exit_code == 0


def test_cmd_append_rows_dry_run_appends_nothing_and_writes_no_log(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Good.jpg", "A title"]],
        {"SOP CD 1": ["Good.jpg", "Extra.jpg"]},
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert appended == []
    assert not (tmp_path / "logs").exists()
    assert "Extra.jpg" in out
    assert "would be appended" in out
    assert exit_code == 0


def test_cmd_append_rows_dry_run_shows_the_two_target_cells_by_their_sheet_names(
    tmp_path, monkeypatch, capsys
):
    """The dry run's job is confidence about the real write: name the two
    Sheet columns as the header actually spells them and show which value
    lands in each, rather than a joined folder/name string a reader must
    mentally re-split. The header here is reordered so the test also fails
    if the display hardcodes column names instead of reading the Sheet's."""
    from ia_bulk import cmd_append_rows

    registry_path, _ = _setup_append(
        tmp_path,
        monkeypatch,
        [["A title", "SOP CD 1", "Good.jpg"]],
        {"SOP CD 1": ["Good.jpg", "Extra.jpg"]},
        header=["Title", "Folder on LaCie Drive", "File Name"],
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path, dry_run=True))
    out = capsys.readouterr().out

    assert "Folder on LaCie Drive" in out
    assert "File Name" in out
    assert "'SOP CD 1'" in out
    assert "'Extra.jpg'" in out
    assert "SOP CD 1/Extra.jpg" not in out
    assert "left blank" in out
    assert exit_code == 0


def test_cmd_append_rows_logs_each_appended_row(tmp_path, monkeypatch):
    from ia_bulk import cmd_append_rows

    registry_path, _ = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Good.jpg", "A title"]],
        {"SOP CD 1": ["Good.jpg", "Extra.jpg"]},
    )

    cmd_append_rows(_append_args(tmp_path, registry_path))

    (log_file,) = (tmp_path / "logs").glob("append-rows-*.jsonl")
    entries = [
        json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()
    ]
    assert [(e["folder"], e["name"], e["status"]) for e in entries] == [
        ("SOP CD 1", "Extra.jpg", "appended")
    ]
    assert entries[0]["timestamp"]


def test_cmd_append_rows_counts_photos_outside_any_folder(tmp_path, monkeypatch, capsys):
    """A photo at the top of files_dir cannot get a row from a folder/name
    template - but silence would leave it uncatalogued with nobody told."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Good.jpg", "A title"]],
        {"SOP CD 1": ["Good.jpg"]},
    )
    (tmp_path / "stray.jpg").write_bytes(b"x")

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert appended == []
    assert "stray.jpg" in out
    assert "outside any folder" in out
    assert exit_code == 0


def test_cmd_append_rows_refuses_a_sheet_whose_headers_collide(tmp_path, monkeypatch, capsys):
    """Same reasoning as reconcile: a header defect corrupts every row
    identically, so nothing read through it can be trusted."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "Good.jpg", "Good.jpg", "A title"]],
        {"SOP CD 1": ["Good.jpg"]},
        header=["Folder on LaCie Drive", "File Name", "File  name", "Title"],
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))

    assert appended == []
    assert "refusing to append" in capsys.readouterr().err
    assert exit_code == 1


def test_cmd_append_rows_refuses_when_any_row_may_be_shifted(tmp_path, monkeypatch, capsys):
    """STRICTER than reconcile, which skips a shifted row: reconcile only
    edits rows an operator approves one at a time, but append trusts the
    whole survey at once. A shifted row's file claim may be misread, making
    the file it really means look unclaimed - and appending that file is the
    duplicate-row misattribution. One suspect row taints the set."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [
            ["SOP CD 1", "Good.jpg", "A title", "an extra cell"],
            ["SOP CD 1", "Fine.jpg", "Another"],
        ],
        {"SOP CD 1": ["Good.jpg", "Fine.jpg", "Extra.jpg"]},
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))

    assert appended == []
    assert "shifted" in capsys.readouterr().err
    assert exit_code == 1


def test_cmd_append_rows_refuses_a_template_with_no_folder_part(tmp_path, monkeypatch, capsys):
    """scan_unclaimed_files assumes a <folder>/<name> drive layout. A
    single-field template like {file} cannot express what the scan finds,
    and writing a folder name into the one file column would be silently
    wrong - refuse instead."""
    from ia_bulk import cmd_append_rows

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path), file_template="{file}")),
        encoding="utf-8",
    )
    appended = []

    class RecordingClient:
        def read_grid(self):
            return [["File", "Title"], ["Good.jpg", "A title"]]

        def append_rows(self, rows):
            appended.extend(rows)

    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: RecordingClient())

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))

    assert appended == []
    assert "folder" in capsys.readouterr().err
    assert exit_code == 1


def test_cmd_append_rows_refuses_when_a_template_column_is_missing(tmp_path, monkeypatch, capsys):
    """file_template names {file_name}; a Sheet with no such column has
    nowhere to put the name half of a skeleton row."""
    from ia_bulk import cmd_append_rows

    registry_path, appended = _setup_append(
        tmp_path,
        monkeypatch,
        [["SOP CD 1", "A title"]],
        {"SOP CD 1": ["Good.jpg"]},
        header=["Folder on LaCie Drive", "Title"],
    )

    exit_code = cmd_append_rows(_append_args(tmp_path, registry_path))

    assert appended == []
    assert "file_name" in capsys.readouterr().err
    assert exit_code == 1


# --- --batch: scoping a run to one value of a registry-configured column ---


def _batch_rows(*themes):
    """Rows carrying only what batch scoping reads. Row numbers run from 2:
    the header is row 1, exactly as validate_rows and plan_upload_targets
    number them."""
    return [{"identifier": "", "title": f"row {n}", "theme": theme}
            for n, theme in enumerate(themes, start=2)]


def _batch_column_map():
    return build_column_map(["Identifier", "Title", "Theme"])


def test_batch_scope_selects_only_rows_whose_configured_column_matches():
    scope = batch_row_numbers(
        _batch_rows("Logging", "Fishing", "Logging"),
        _sheet_config(batch_column="theme"),
        _batch_column_map(),
        "Logging",
        "projects_registry.json",
    )

    assert scope == {2, 4}


def test_batch_scope_folds_case_and_surrounding_whitespace():
    """The cells are hand-typed in a Sheet, so 'Logging ' and 'logging' are
    one batch, not three."""
    scope = batch_row_numbers(
        _batch_rows("  logging ", "LOGGING", "Fishing"),
        _sheet_config(batch_column="theme"),
        _batch_column_map(),
        " Logging",
        "projects_registry.json",
    )

    assert scope == {2, 3}


def test_batch_scope_never_matches_a_blank_cell():
    """An uncatalogued row has no theme yet. It must not join whatever batch
    happens to be running."""
    scope = batch_row_numbers(
        _batch_rows("Logging", "", "   "),
        _sheet_config(batch_column="theme"),
        _batch_column_map(),
        "Logging",
        "projects_registry.json",
    )

    assert scope == {2}


def test_batch_on_a_project_with_no_batch_column_is_refused():
    """Never a silent unfiltered run: the whole point of --batch is to narrow
    the scope, so a --batch that uploaded everything is the worst outcome."""
    with pytest.raises(BatchScopeError) as exc:
        batch_row_numbers(
            _batch_rows("Logging"),
            _sheet_config(batch_column=None),
            _batch_column_map(),
            "Logging",
            "some/registry.json",
        )

    message = str(exc.value)
    assert "batch_column" in message
    assert "some/registry.json" in message
    assert "astoriaphotos" in message


def test_a_batch_column_the_sheet_does_not_have_is_refused_by_name():
    """Same failure mode check_required_for_upload guards: left alone this
    reads every row's batch as blank, matches nothing, and looks like 'that
    batch is already uploaded'."""
    with pytest.raises(BatchScopeError) as exc:
        batch_row_numbers(
            _batch_rows("Logging"),
            _sheet_config(batch_column="subject"),
            _batch_column_map(),
            "Logging",
            "projects_registry.json",
        )

    message = str(exc.value)
    assert "'subject'" in message
    assert "not a column in this Sheet" in message
    assert "theme" in message  # the known columns, so the fix is visible


def test_a_batch_value_matching_no_row_is_refused_and_lists_what_is_there():
    """A typo'd --batch would otherwise read as 'nothing to upload - every
    valid row is already marked uploaded'."""
    with pytest.raises(BatchScopeError) as exc:
        batch_row_numbers(
            _batch_rows("Logging", "Fishing", "logging"),
            _sheet_config(batch_column="theme"),
            _batch_column_map(),
            "Loging",
            "projects_registry.json",
        )

    message = str(exc.value)
    assert "'Loging'" in message
    assert "Fishing" in message
    assert "Logging" in message
    # De-duplicated the same way matching folds: 'logging' is not a fourth value
    assert message.count("ogging") == 1


def test_a_batch_column_that_is_blank_in_every_row_says_so():
    with pytest.raises(BatchScopeError) as exc:
        batch_row_numbers(
            _batch_rows("", "  "),
            _sheet_config(batch_column="theme"),
            _batch_column_map(),
            "Logging",
            "projects_registry.json",
        )

    assert "empty in every row" in str(exc.value)


def test_an_empty_batch_value_is_refused_rather_than_meaning_every_row():
    with pytest.raises(BatchScopeError) as exc:
        batch_row_numbers(
            _batch_rows("Logging"),
            _sheet_config(batch_column="theme"),
            _batch_column_map(),
            "   ",
            "projects_registry.json",
        )

    assert "--batch" in str(exc.value)


def test_the_listed_values_are_capped_so_a_wide_column_stays_readable():
    themes = [f"Theme {n:02d}" for n in range(1, 31)]

    with pytest.raises(BatchScopeError) as exc:
        batch_row_numbers(
            _batch_rows(*themes),
            _sheet_config(batch_column="theme"),
            _batch_column_map(),
            "nope",
            "projects_registry.json",
        )

    message = str(exc.value)
    assert "Theme 01" in message
    assert "Theme 30" not in message
    assert "10 more" in message


def test_plan_upload_targets_mints_only_for_rows_in_scope():
    """A scoped run must not mint a number for a row it is not going to
    upload: next_identifiers() takes max+1 and never refills, so a minted-
    then-discarded number would leave a permanent gap in the sequence."""
    from ia_bulk import plan_upload_targets

    rows = [
        {"ia_identifier": "", "ia_uploaded": "", "title": "One", "theme": "Logging"},
        {"ia_identifier": "", "ia_uploaded": "", "title": "Two", "theme": "Fishing"},
        {"ia_identifier": "", "ia_uploaded": "", "title": "Three", "theme": "Logging"},
    ]
    results = [RowValidation(row_number=n, identifier="") for n in (2, 3, 4)]

    targets = plan_upload_targets(
        rows, results, _sheet_config(), live=False, fingerprints={}, stamp=FIXED_STAMP,
        scope={2, 4},
    )

    assert [(target.row_number, target.identifier) for target in targets] == [
        (2, "lcps-astoriaphotos-00001"),
        (4, "lcps-astoriaphotos-00002"),
    ]


def test_plan_upload_targets_still_reads_every_row_for_numbers_already_spent():
    """The hazard scoping introduces: an out-of-scope row holding
    lcps-astoriaphotos-00007 has spent that number permanently, and a batch
    that only looked at its own rows would mint it a second time."""
    from ia_bulk import plan_upload_targets

    rows = [
        {"ia_identifier": "lcps-astoriaphotos-00007", "ia_uploaded": "yes",
         "title": "Other batch", "theme": "Fishing"},
        {"ia_identifier": "", "ia_uploaded": "", "title": "Mine", "theme": "Logging"},
    ]
    results = [RowValidation(row_number=n, identifier="") for n in (2, 3)]

    targets = plan_upload_targets(
        rows, results, _sheet_config(), live=False, fingerprints={}, stamp=FIXED_STAMP,
        scope={3},
    )

    assert [target.identifier for target in targets] == ["lcps-astoriaphotos-00008"]


BATCH_SHEET_HEADER = SHEET_HEADER + ["Theme"]


def _batch_grid():
    return [
        BATCH_SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", "", "Logging"],
        ["Second photo", "photo2.jpg", "", "", "", "", "Fishing"],
        ["Third photo", "photo3.jpg", "", "", "", "", "logging"],
    ]


def _batch_registry(tmp_path, **overrides):
    return make_sheet_registry(files_dir=str(tmp_path), batch_column="theme", **overrides)


def test_cmd_upload_batch_uploads_only_the_rows_in_that_batch(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_upload

    recorder, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        _batch_grid(),
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        registry=_batch_registry(tmp_path),
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, batch="Logging"))
    capsys.readouterr()

    assert exit_code == 0
    assert recorder.uploads == [
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00001",
        f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",
    ]


def test_cmd_upload_batch_composes_with_limit_as_that_many_of_the_batch(
    tmp_path, monkeypatch, capsys
):
    """--limit already means "this many of the rows actually in scope", and
    --batch narrows what in-scope means - it must not become "this many rows
    read, then filtered"."""
    from ia_bulk import cmd_upload

    grid = [
        BATCH_SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", "", "Fishing"],
        ["Second photo", "photo2.jpg", "", "", "", "", "Logging"],
        ["Third photo", "photo3.jpg", "", "", "", "", "Logging"],
    ]
    captured = []
    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        registry=_batch_registry(tmp_path),
        captured=captured,
    )

    exit_code = cmd_upload(
        make_upload_args(tmp_path, registry_path, batch="Logging", limit=1)
    )
    capsys.readouterr()

    assert exit_code == 0
    # The Fishing row is first in the Sheet, so an unscoped --limit 1 would
    # upload it - the one thing this must not do.
    assert [entry["row"]["title"] for entry in captured] == ["Second photo"]


def test_cmd_upload_refuses_a_batch_the_project_has_no_column_for(
    tmp_path, monkeypatch, capsys
):
    """Never a silent unfiltered run: without this the flag would be ignored
    and all three rows would upload."""
    from ia_bulk import cmd_upload

    recorder, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        _batch_grid(),
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        registry=make_sheet_registry(files_dir=str(tmp_path)),
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, batch="Logging"))
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "batch_column" in err
    assert recorder.uploads == []


def test_cmd_upload_refuses_a_batch_value_no_row_carries(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_upload

    recorder, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        _batch_grid(),
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        registry=_batch_registry(tmp_path),
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path, batch="Loging"))
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "matches no row" in err
    assert recorder.uploads == []


def test_cmd_upload_counts_only_the_batch_as_not_yet_catalogued(
    tmp_path, monkeypatch, capsys
):
    """The scope is narrowed before anything counts: an uncatalogued row in
    another batch is not this run's business to report."""
    from ia_bulk import cmd_upload

    grid = [
        BATCH_SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", "", "Logging"],
        ["", "photo2.jpg", "", "", "", "", "Fishing"],
        ["", "photo3.jpg", "", "", "", "", "Logging"],
    ]
    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        registry=_batch_registry(tmp_path),
    )

    cmd_upload(make_upload_args(tmp_path, registry_path, batch="Logging"))
    out = capsys.readouterr().out

    assert "1 row not yet catalogued" in out


def test_cmd_validate_batch_reports_only_the_rows_in_that_batch(
    tmp_path, monkeypatch, capsys
):
    """validate previews exactly what upload would do, through the same
    scoping code - the two commands must not define the scope differently."""
    from ia_bulk import cmd_validate

    for name in ("photo1.jpg", "photo2.jpg", "photo3.jpg"):
        (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(
        "ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(_batch_grid())
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_batch_registry(tmp_path)), encoding="utf-8")

    exit_code = cmd_validate(
        Namespace(
            project="astoriaphotos", registry=str(registry_path),
            live=False, batch="Logging",
        )
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "2/2 rows passed" in out
    assert "2 rows ready to upload" in out


def test_cmd_validate_refuses_a_batch_value_no_row_carries(tmp_path, monkeypatch, capsys):
    from ia_bulk import cmd_validate

    for name in ("photo1.jpg", "photo2.jpg", "photo3.jpg"):
        (tmp_path / name).write_bytes(b"x")
    monkeypatch.setattr(
        "ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(_batch_grid())
    )
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(_batch_registry(tmp_path)), encoding="utf-8")

    exit_code = cmd_validate(
        Namespace(
            project="astoriaphotos", registry=str(registry_path),
            live=False, batch="Loging",
        )
    )
    err = capsys.readouterr().err

    assert exit_code == 1
    assert "matches no row" in err


def test_build_parser_accepts_batch_on_both_upload_and_validate():
    parser = build_parser()

    validate_args = parser.parse_args(
        ["validate", "--project", "p", "--batch", "Logging"]
    )
    upload_args = parser.parse_args(["upload", "--project", "p", "--batch", "Logging"])

    assert validate_args.batch == "Logging"
    assert upload_args.batch == "Logging"


def test_build_parser_leaves_batch_unset_by_default():
    parser = build_parser()

    assert parser.parse_args(["validate", "--project", "p"]).batch is None
    assert parser.parse_args(["upload", "--project", "p"]).batch is None


def test_the_run_header_records_the_batch_a_scoped_run_ran(tmp_path, monkeypatch, capsys):
    """Reconstructability, the same reason --limit and --chunk-size are in
    here: months later, "why did this run upload 40 of 3,000 rows" cannot be
    answered from the rest of the record if the batch it was scoped to is
    missing."""
    from ia_bulk import cmd_upload

    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        _batch_grid(),
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        registry=_batch_registry(tmp_path),
    )

    cmd_upload(make_upload_args(tmp_path, registry_path, batch="Logging"))
    capsys.readouterr()

    log_file = next((tmp_path / "logs").glob("upload-*.jsonl"))
    header = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])

    assert header["batch"] == "Logging"
    assert header["batch_column"] == "theme"


def test_the_run_header_of_an_unscoped_run_says_so_rather_than_omitting_it(
    tmp_path, monkeypatch, capsys
):
    from ia_bulk import cmd_upload

    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        _batch_grid(),
        files=("photo1.jpg", "photo2.jpg", "photo3.jpg"),
        registry=_batch_registry(tmp_path),
    )

    cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    log_file = next((tmp_path / "logs").glob("upload-*.jsonl"))
    header = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])

    assert header["batch"] is None
    assert header["batch_column"] == "theme"


# --- naming the rows behind a count ---


def test_row_numbers_names_a_single_row_in_the_singular():
    assert format_row_numbers([189]) == "row 189"


def test_row_numbers_collapses_a_contiguous_block_to_one_range():
    """The whole reason this is ranges rather than a capped list: the
    uncatalogued backlog is one long block of appended skeleton rows, and
    printing 2,847 numbers - or the first ten and a truncation - tells an
    operator less than '190-3036' does."""
    assert format_row_numbers(range(190, 3037)) == "rows 190-3036"


def test_row_numbers_separates_disjoint_blocks():
    assert format_row_numbers([12, 13, 14, 40, 42, 43]) == "rows 12-14, 40, 42-43"


def test_row_numbers_sorts_and_deduplicates_before_grouping():
    """Callers collect these while walking results, not in sorted order, and
    two sources can name the same row."""
    assert format_row_numbers([40, 12, 13, 12]) == "rows 12-13, 40"


def test_row_numbers_caps_the_ranges_it_prints_and_says_how_many_it_dropped():
    """Compression handles the common shape; this handles the pathological
    one - hundreds of scattered single rows, where every range is one row
    long and no compression is possible."""
    scattered = list(range(2, 42, 2))  # 20 rows, no two adjacent

    assert format_row_numbers(scattered, max_ranges=8) == (
        "rows 2, 4, 6, 8, 10, 12, 14, 16, and 12 more ranges"
    )


def test_row_numbers_of_nothing_is_empty():
    assert format_row_numbers([]) == ""


def test_a_two_row_block_still_reads_as_a_range():
    assert format_row_numbers([12, 13]) == "rows 12-13"


def test_missing_field_lines_name_the_row_behind_a_count_of_one():
    """The bucket an operator cannot pick out of the report above: a
    not-ready row prints as [PASS], indistinguishable at a glance from the
    thousands of rows that are simply fine."""
    results = [RowValidation(189, "", missing_fields=["file_name"])]

    assert format_missing_field_lines(results) == ["    1 missing file_name: row 189"]


def test_missing_field_lines_name_the_rows_per_field_not_per_bucket():
    """A row missing two fields is named under both, which is what makes the
    lines actionable - 'go fix title on these, file_name on those'."""
    results = [
        RowValidation(2, "", missing_fields=["title", "theme"]),
        RowValidation(3, "", missing_fields=["title"]),
    ]

    lines = format_missing_field_lines(results)

    assert "    2 missing title: rows 2-3" in lines
    assert "    1 missing theme: row 2" in lines


# --- the missing-field detail sits under the count it belongs to ---


def _unassigned(**cells):
    return {"ia_identifier": "", "ia_uploaded": "", **cells}


def _uploaded(**cells):
    return {"ia_identifier": "lcps-astoriaphotos-00001", "ia_uploaded": "2026-09-06", **cells}


def _line_index(lines, fragment):
    return next(index for index, line in enumerate(lines) if fragment in line)


def test_the_missing_fields_are_listed_under_the_line_that_counts_them():
    """They used to print as a second block below the whole summary, whose
    header re-stated a count the summary had already given - see the decision
    record. The detail belongs to its count, not to the report."""
    rows = [_unassigned()]
    results = [RowValidation(189, "", missing_fields=["file_name"])]

    lines = format_lifecycle_summary(rows, results).splitlines()

    parent = _line_index(lines, "not yet assigned an identifier and not yet catalogued")
    assert lines[parent + 1] == "    1 missing file_name: row 189"


def test_not_ready_rows_in_different_lifecycle_states_keep_their_details_apart():
    """The reason the detail attaches per line rather than moving wholesale
    under one of them: an uncatalogued row and a row whose title was cleared
    AFTER it uploaded need different work, and merging them into one
    "2 missing title" would hide that."""
    rows = [_unassigned(), _uploaded()]
    results = [
        RowValidation(2, "", missing_fields=["title"]),
        RowValidation(40, "", missing_fields=["title"]),
    ]

    lines = format_lifecycle_summary(rows, results).splitlines()

    unassigned = _line_index(lines, "not yet assigned an identifier and not yet catalogued")
    uploaded = _line_index(lines, "already uploaded but missing required fields")
    assert lines[unassigned + 1] == "    1 missing title: row 2"
    assert lines[uploaded + 1] == "    1 missing title: row 40"


def test_the_overlap_note_belongs_to_its_own_block_and_names_that_blocks_total():
    """Two not-ready rows here, but only one of them is in the block the note
    prints under - a note reading "do not sum to 3" would be counting rows
    from a different bucket."""
    rows = [_unassigned(), _unassigned(), _uploaded()]
    results = [
        RowValidation(2, "", missing_fields=["title", "theme"]),
        RowValidation(3, "", missing_fields=["title"]),
        RowValidation(40, "", missing_fields=["title"]),
    ]

    lines = format_lifecycle_summary(rows, results).splitlines()

    unassigned = _line_index(lines, "not yet assigned an identifier and not yet catalogued")
    assert lines[unassigned + 1] == "    2 missing title: rows 2-3"
    assert lines[unassigned + 2] == "    1 missing theme: row 2"
    assert lines[unassigned + 3] == (
        "    (a row missing more than one field appears in more than one "
        "count above, so these do not sum to 2)"
    )
    # The already-uploaded block's one row misses exactly one field, so its
    # counts genuinely do sum - the note must not appear there.
    uploaded = _line_index(lines, "already uploaded but missing required fields")
    assert lines[uploaded + 1] == "    1 missing title: row 40"
    assert "count above" not in lines[uploaded + 1]


def test_a_ready_line_gets_no_detail_lines_under_it():
    rows = [_unassigned(), _unassigned()]
    results = [RowValidation(2, ""), RowValidation(3, "", missing_fields=["title"])]

    lines = format_lifecycle_summary(rows, results).splitlines()

    ready = _line_index(lines, "ready to upload")
    assert not lines[ready + 1].startswith("    ")


def test_missing_field_lines_of_rows_that_are_all_ready_is_empty():
    assert format_missing_field_lines([RowValidation(2, "")]) == []


def test_cmd_validate_prints_the_missing_field_detail_once_not_as_a_second_block(
    tmp_path, monkeypatch, capsys
):
    """End-to-end: the standalone breakdown block and its re-stated header are
    gone, and the detail appears exactly once, under its count."""
    from ia_bulk import cmd_validate

    (tmp_path / "photo1.jpg").write_bytes(b"x")
    # Row 3 is catalogued except for its file cell, so exactly one field is
    # missing - which keeps the "printed once" assertion below unambiguous.
    grid = [
        ["Title", "file"],
        ["First photo", "photo1.jpg"],
        ["Second photo", ""],
    ]
    monkeypatch.setattr("ia_bulk.build_sheet_client", lambda config, live: FakeSheetClient(grid))
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(make_sheet_registry(files_dir=str(tmp_path))), encoding="utf-8"
    )

    cmd_validate(
        Namespace(project="astoriaphotos", registry=str(registry_path), live=False)
    )
    out = capsys.readouterr().out
    lines = out.splitlines()

    assert "1 row not yet catalogued" not in lines
    assert sum(1 for line in lines if "missing file" in line) == 1
    parent = _line_index(lines, "not yet assigned an identifier and not yet catalogued")
    assert lines[parent + 1] == "    1 missing file: row 3"


def test_the_run_header_records_the_collection_the_run_actually_targeted(tmp_path):
    """The header used to record the registry's ia_collection whatever mode
    the run was in, so a test run's receipt named the real, permanent
    collection while its items went to test_collection. Inferable from the
    `live` field beside it, but only if the reader already knows the rule -
    and this record exists so a reader months later does not have to."""
    from ia_bulk import log_run_header, TEST_COLLECTION

    log_path = tmp_path / "upload.jsonl"
    column_map = build_column_map(["Title"])
    config = _sheet_config(required_for_upload=("title",))

    log_run_header(log_path, config, column_map, live=False, dry_run=False)

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["collection"] == TEST_COLLECTION
    assert header["collection"] != config.ia_collection


def test_a_live_run_header_records_the_registrys_own_collection(tmp_path):
    from ia_bulk import log_run_header

    log_path = tmp_path / "upload.jsonl"
    column_map = build_column_map(["Title"])
    config = _sheet_config(required_for_upload=("title",))

    log_run_header(log_path, config, column_map, live=True, dry_run=False)

    header = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert header["collection"] == "lcpsociety"


# Issue #26: the upload run's own machine-readable summary (#25 built only
# sync-metadata's half), and the Sheet log tabs that mirror it.


def _upload_log_entries(tmp_path):
    log_file = next((tmp_path / "logs").glob("upload-*.jsonl"))
    return [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").strip().splitlines()
    ]


def test_upload_ends_with_a_machine_readable_summary(tmp_path, monkeypatch):
    """Upload's closing counts lived only in console output, so an unattended
    run left nothing a program could read without replaying every per-row
    record - the gap #25 closed for sync-metadata and not for upload."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo1.jpg", "photo2.jpg")
    )

    cmd_upload(make_upload_args(tmp_path, registry_path))

    summary = _upload_log_entries(tmp_path)[-1]

    assert summary["record"] == "run_summary"
    assert summary["succeeded"] == 2
    assert summary["attempted"] == 2
    assert summary["failures"] == []
    assert summary["unconfirmed"] == []
    assert summary["skipped"] == []
    assert summary["not_attempted"] == 0
    assert summary["rate_limited"] is False


def test_the_upload_summary_names_each_failed_row_and_why(tmp_path, monkeypatch):
    """A summary reporting "1 error(s)" and nothing else sends the reader back
    to the per-row lines it exists to replace. The identifier recorded is the
    row's permanent one, matching the per-row records above it."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        fail_for=(f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",),
    )

    cmd_upload(make_upload_args(tmp_path, registry_path))

    summary = _upload_log_entries(tmp_path)[-1]

    assert summary["failures"] == [
        {"identifier": "lcps-astoriaphotos-00002", "error": "boom"}
    ]
    assert summary["succeeded"] == 1
    assert summary["attempted"] == 2


def test_the_upload_summary_separates_uploaded_but_unrecorded_from_refused(
    tmp_path, monkeypatch, capsys
):
    """The distinction the summary exists to preserve, and the one that costs
    the most to get wrong. A refused send created nothing and left the
    identifier free; an unconfirmed row IS on Internet Archive but unmarked in
    the Sheet, so a later run reads it as un-uploaded and would send the same
    photograph again under a second permanent identifier. A flat failure list
    would flatten the two."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, raise_on_write=2
    )

    cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    capsys.readouterr()

    summary = _upload_log_entries(tmp_path)[-1]

    assert summary["failures"] == []
    assert [entry["identifier"] for entry in summary["unconfirmed"]] == [
        "lcps-astoriaphotos-00001"
    ]
    assert summary["succeeded"] == 1


def test_a_row_held_back_by_validation_is_skipped_not_failed(tmp_path, monkeypatch, capsys):
    """A skipped row was never sent, so nothing about the item changed. Folded
    into `failures` it would read as "Internet Archive refused this", sending
    a reader months later to look at an item that was never contacted."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    # Only photo2 exists on disk, so row 1 is READY (every required column is
    # filled) but invalid - which is what `blocked` means. A blank required
    # column would make it NOT_READY instead, and a not-ready row was never
    # in this run's scope to skip.
    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, files=("photo2.jpg",)
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    summary = _upload_log_entries(tmp_path)[-1]

    assert summary["failures"] == []
    assert len(summary["skipped"]) == 1
    assert "photo1.jpg" in summary["skipped"][0]["error"]
    assert summary["succeeded"] == 1
    assert exit_code == 1


def test_a_rate_limited_run_says_so_in_its_summary(tmp_path, monkeypatch, capsys):
    """An unattended run that stopped early looks, in every count except this
    flag, like a run that simply had little to do. The reader has to be able
    to tell "finished" from "stopped, resume tomorrow" without parsing
    stderr."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in range(1, 6)
    ]
    recorder, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=tuple(f"photo{n}.jpg" for n in range(1, 6)),
    )

    def rate_limited_on_the_third(row, target_identifier, collection, files_dir):
        recorder.events.append(("upload", target_identifier))
        if target_identifier.endswith("00003"):
            raise UploadFailed(
                f"upload of '{target_identifier}' failed with status 503: SlowDown",
                status_code=503,
            )

    monkeypatch.setattr("ia_bulk.upload_row", rate_limited_on_the_third)

    cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    summary = _upload_log_entries(tmp_path)[-1]

    assert summary["rate_limited"] is True
    assert summary["succeeded"] == 2
    assert summary["not_attempted"] == 2
    assert [entry["identifier"] for entry in summary["failures"]] == [
        "lcps-astoriaphotos-00003"
    ]


def test_the_upload_console_tail_and_the_summary_record_cannot_disagree(
    tmp_path, monkeypatch, capsys
):
    """Both are rendered from one UploadSummary. Pinned end to end because the
    two used to be computed separately - a counts dict for the console and
    nothing at all for a program - which is exactly the drift #25 closed for
    sync-metadata."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    _, _, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        fail_for=(f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",),
    )

    cmd_upload(make_upload_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    summary = _upload_log_entries(tmp_path)[-1]

    assert (
        f"{summary['succeeded']} file(s) uploaded successfully, "
        f"{len(summary['failures'])} error(s)" in out.splitlines()
    )


def test_an_upload_summary_that_cannot_be_written_does_not_fail_the_run(
    tmp_path, monkeypatch, capsys
):
    """The summary is a record OF the run, not a step IN it, and it is written
    last - by the time it fails, items already exist on Internet Archive under
    permanent identifiers. A run reported as failed invites a rerun, which is
    the one outcome this must never cause."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    _, _, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    def refuse(log_path, record):
        raise OSError("disk full")

    monkeypatch.setattr("ia_bulk.log_run_summary", refuse)

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "disk full" in captured.err
    assert "1 file(s) uploaded successfully" in captured.out


# Issue #26: mirroring a run's summary into the Sheet's own log tab.


def _log_tab_registry(tmp_path, **overrides):
    return make_sheet_registry(files_dir=str(tmp_path), upload_log_tab="Upload Log", **overrides)


def test_an_upload_run_appends_its_summary_to_the_configured_log_tab(
    tmp_path, monkeypatch, capsys
):
    """The point is remote diagnosis: when someone calls months from now, the
    Sheet opens from anywhere, and the JSONL on a Mac in the office does
    not."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    _, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, registry=_log_tab_registry(tmp_path)
    )

    cmd_upload(make_upload_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    tab = client.log_tabs["Upload Log"]
    assert tab.ensured == [["when", "run", "outcome", "identifier", "detail"]]
    assert len(tab.appended) == 1
    when, run, outcome, identifier, detail = tab.appended[0]
    assert outcome == "summary"
    assert identifier == ""
    # The tab says exactly what the operator saw on screen, and names the
    # JSONL to go and read for the per-file detail.
    assert detail in out.splitlines()
    assert run.startswith("upload-") and run.endswith(".jsonl")
    # The tab and the JSONL are one record rendered twice, down to the
    # timestamp: `when` has to find its own line in the file it names. Two
    # as_record() calls a second apart would leave it naming nothing.
    assert when == _upload_log_entries(tmp_path)[-1]["timestamp"]


def test_each_failed_row_gets_its_own_line_in_the_log_tab(tmp_path, monkeypatch, capsys):
    """A count alone sends the caller back to the file this tab exists to
    replace."""
    from ia_bulk import cmd_upload

    grid = [
        SHEET_HEADER,
        ["First photo", "photo1.jpg", "", "", "", ""],
        ["Second photo", "photo2.jpg", "", "", "", ""],
    ]
    _, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        registry=_log_tab_registry(tmp_path),
        fail_for=(f"zztest-{FIXED_STAMP}-lcps-astoriaphotos-00002",),
    )

    cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    rows = client.log_tabs["Upload Log"].appended
    assert [row[2:] for row in rows[1:]] == [
        ["failure", "lcps-astoriaphotos-00002", "boom"]
    ]


def test_no_log_tab_is_written_when_the_registry_names_none(tmp_path, monkeypatch, capsys):
    """Absent means off. A default tab name would have every run create a tab
    in a Sheet whose owner never asked for one."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    _, client, registry_path, _ = setup_sheet_upload(tmp_path, monkeypatch, grid)

    cmd_upload(make_upload_args(tmp_path, registry_path))
    capsys.readouterr()

    assert client.log_tabs == {}


def test_a_dry_run_writes_nothing_to_the_log_tab(tmp_path, monkeypatch, capsys):
    """A dry run uploads nothing, so it has no run to report. Writing a row
    saying so would put runs in the tab that never happened."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    _, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, registry=_log_tab_registry(tmp_path)
    )

    cmd_upload(make_upload_args(tmp_path, registry_path, dry_run=True))
    capsys.readouterr()

    assert client.log_tabs == {}


def test_a_log_tab_that_cannot_be_written_does_not_fail_the_upload(
    tmp_path, monkeypatch, capsys
):
    """Acceptance criterion 3 of #26, end to end. The files are already on
    Internet Archive under permanent identifiers by the time this runs; a
    failed telemetry write reported as a failed upload would invite the one
    thing that costs something - a rerun."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        registry=_log_tab_registry(tmp_path),
        raise_on_log_tab=True,
    )

    exit_code = cmd_upload(make_upload_args(tmp_path, registry_path=tmp_path / "registry.json"))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "1 file(s) uploaded successfully" in captured.out
    assert "could not mirror this run into its Sheet log tab" in captured.err
    assert "Sheets API returned 503" in captured.err


def test_the_log_tab_client_cannot_write_to_the_metadata_columns(
    tmp_path, monkeypatch, capsys
):
    """#26's standing constraint - Sheet -> Internet Archive stays
    one-directional - asserted structurally rather than by inspection: what
    the mirror is handed has no cell-write method at all."""
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER, ["First photo", "photo1.jpg", "", "", "", ""]]
    _, client, registry_path, _ = setup_sheet_upload(
        tmp_path, monkeypatch, grid, registry=_log_tab_registry(tmp_path)
    )

    cmd_upload(make_upload_args(tmp_path, registry_path, write_identifier=True))
    captured = capsys.readouterr()

    # Nothing on stderr is what proves the mirror made no call the log tab
    # could not serve: mirror_run catches everything, so a stray write_cells
    # would surface here and nowhere else.
    assert captured.err == ""
    # Every cell write this run made went to the metadata tab's client; the
    # log tab saw appends and nothing else.
    assert client.write_count > 0
    assert client.log_tabs["Upload Log"].appended


def _sync_log_registry(tmp_path):
    return make_sheet_registry(files_dir=str(tmp_path), sync_log_tab="Sync Log")


def test_a_sync_run_that_changed_something_appends_to_its_own_log_tab(
    tmp_path, monkeypatch, capsys
):
    """Sync gets its own tab rather than sharing upload's: an hourly job and
    a once-a-week upload interleaved in one tab would bury the upload rows
    someone opened the Sheet to find."""
    from ia_bulk import cmd_sync_metadata

    registry_path, client = _setup_sync_sheet(
        tmp_path, monkeypatch, _two_synced_rows(), [], registry=_sync_log_registry(tmp_path)
    )

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    rows = client.log_tabs["Sync Log"].appended
    assert len(rows) == 1
    assert rows[0][2] == "summary"
    assert rows[0][1].startswith("sync-metadata-")
    assert rows[0][4] in out.splitlines()


def test_a_sync_run_with_nothing_to_do_leaves_the_log_tab_alone(
    tmp_path, monkeypatch, capsys
):
    """The steady state of an hourly job, and the reason the tab stays
    readable. A row every hour saying "4,212 already in sync" would be 9,000
    rows a year, burying the handful that report an actual problem. The run
    is still fully recorded in its own JSONL."""
    from ia_bulk import cmd_sync_metadata

    registry_path, client = _setup_sync_sheet(
        tmp_path, monkeypatch, _two_synced_rows(), [], registry=_sync_log_registry(tmp_path)
    )

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    client.log_tabs.clear()

    # Second run: every row now matches its last push, so the hash gate holds
    # them all back and the run does nothing.
    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    assert "already match their last push" in out
    assert client.log_tabs.get("Sync Log") is None or client.log_tabs["Sync Log"].appended == []


def test_a_sync_run_that_only_failed_still_reaches_the_log_tab(tmp_path, monkeypatch, capsys):
    """The quiet-run guard must not silence the runs that matter: a run that
    pushed nothing because everything was refused is precisely what someone
    opens the Sheet to find."""
    from ia_bulk import cmd_sync_metadata

    registry_path, client = _setup_sync_sheet(
        tmp_path, monkeypatch, _two_synced_rows(), [], registry=_sync_log_registry(tmp_path)
    )

    def refused(metadata, target):
        raise RuntimeError("Access Denied - This item has been taken offline")

    monkeypatch.setattr("ia_bulk.update_metadata_row", refused)

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    capsys.readouterr()

    rows = client.log_tabs["Sync Log"].appended
    assert [row[2] for row in rows] == ["summary", "failure", "failure"]
    assert "Access Denied" in rows[1][4]


def test_a_row_moved_mid_run_reaches_the_summary_and_the_log_tab(
    tmp_path, monkeypatch, capsys
):
    """Both causes of "nothing was sent for this row" have to survive into the
    summary: validation held it back, or the Sheet was edited underneath the
    run. The second is the one someone telephones about - it means a
    volunteer was editing while the run was going - and it is the one that
    exists nowhere else at run level, since `not_attempted` is a bare count
    with no identifier in it.

    Two rows at chunk_size 1: row 1's file cell changes before chunk 1's
    pre-reserve guard reads it, so the run reports it moved and sends
    nothing for it.
    """
    from ia_bulk import cmd_upload

    grid = [SHEET_HEADER] + [
        [f"Photo {n}", f"photo{n}.jpg", "", "", "", ""] for n in (1, 2)
    ]

    def move_row_1_before_the_first_chunk_verifies(live_grid, read_count):
        if read_count == 2:
            live_grid[1][1] = "somethingelse.jpg"

    _, client, registry_path, _ = setup_sheet_upload(
        tmp_path,
        monkeypatch,
        grid,
        files=("photo1.jpg", "photo2.jpg"),
        registry=_log_tab_registry(tmp_path),
        before_read=move_row_1_before_the_first_chunk_verifies,
    )

    cmd_upload(
        make_upload_args(tmp_path, registry_path, write_identifier=True, chunk_size=1)
    )
    capsys.readouterr()

    summary = _upload_log_entries(tmp_path)[-1]

    assert [entry["identifier"] for entry in summary["skipped"]] == [
        "lcps-astoriaphotos-00001"
    ]
    assert "edited while the run was in progress" in summary["skipped"][0]["error"]
    # and it reaches the tab, which is where it would actually be read
    rows = client.log_tabs["Upload Log"].appended
    assert [row[2:4] for row in rows[1:]] == [["skipped", "lcps-astoriaphotos-00001"]]


def test_a_sync_run_with_only_skips_mirrors_the_line_the_operator_saw(
    tmp_path, monkeypatch, capsys
):
    """The nothing-to-push path prints "nothing to sync - ..." and never the
    usual "N updated, N unchanged, N error(s)" line. It is still mirrored when
    a row was skipped, and the tab has to say what the operator saw rather
    than a count line that appeared nowhere on screen."""
    from ia_bulk import cmd_sync_metadata

    registry_path, client = _setup_sync_sheet(
        tmp_path, monkeypatch, _two_synced_rows(), [], registry=_sync_log_registry(tmp_path)
    )
    # First run stamps both rows, so the second finds nothing to push.
    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    capsys.readouterr()
    client.log_tabs.clear()

    # Marked uploaded but naming no item: skipped before the hash gate.
    ia_url_column = client.grid[0].index("ia_url")
    client.grid[2][ia_url_column] = ""

    cmd_sync_metadata(_sync_sheet_args(tmp_path, registry_path))
    out = capsys.readouterr().out

    rows = client.log_tabs["Sync Log"].appended
    assert rows[0][2] == "summary"
    assert rows[0][4].startswith("nothing to sync")
    assert rows[0][4] in out.splitlines()
    assert [row[2:4] for row in rows[1:]] == [["skipped", "lcps-astoriaphotos-00002"]]
