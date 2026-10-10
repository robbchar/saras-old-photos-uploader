"""Tests for subject_columns.py - building `subject` from the registry's listed columns."""
from __future__ import annotations

from column_map import build_column_map
from subject_columns import (
    check_subject_columns,
    split_subject_cell,
    subject_value,
    subject_values,
)

COLUMNS = ["theme", "subtheme_optional", "subject_terms_controlled_vocab"]
HEADER = ["Title", "Theme", "Subtheme (Optional)", "Subject Terms (Controlled Vocab)"]


def _row(theme="", subtheme="", terms=""):
    return {
        "title": "T",
        "theme": theme,
        "subtheme_optional": subtheme,
        "subject_terms_controlled_vocab": terms,
    }


def test_split_subject_cell_splits_on_semicolons_and_trims():
    assert split_subject_cell(" Buildings ;Streets; ") == ["Buildings", "Streets"]


def test_split_subject_cell_keeps_commas_inside_a_value():
    assert split_subject_cell("Astoria, Oregon") == ["Astoria, Oregon"]


def test_split_subject_cell_of_a_blank_cell_is_empty():
    assert split_subject_cell("   ") == []


def test_subject_values_follow_column_order():
    row = _row("Townscape & Built Environment", "Commercial Buildings", "Buildings; Streets")

    assert subject_values(row, COLUMNS) == [
        "Townscape & Built Environment", "Commercial Buildings", "Buildings", "Streets",
    ]


def test_subject_values_send_an_exact_repeat_once_at_its_first_place():
    row = _row("Buildings", "Commercial", "Buildings; Streets")

    assert subject_values(row, COLUMNS) == ["Buildings", "Commercial", "Streets"]


def test_subject_values_keeps_a_case_variant():
    assert subject_values(_row("Buildings", "", "buildings"), COLUMNS) == ["Buildings", "buildings"]


def test_subject_values_skips_a_cell_of_only_separators():
    assert subject_values(_row("Buildings", "", " ; ;"), COLUMNS) == ["Buildings"]


def test_subject_values_treat_a_missing_key_as_blank():
    assert subject_values({"theme": "Buildings"}, COLUMNS) == ["Buildings"]


def test_subject_value_is_plain_text_for_one_value():
    assert subject_value(_row("Buildings"), COLUMNS) == "Buildings"


def test_subject_value_is_a_list_for_several_values():
    assert subject_value(_row("Buildings", "Streets"), COLUMNS) == ["Buildings", "Streets"]


def test_subject_value_is_blank_when_every_listed_cell_is_blank():
    assert subject_value(_row(), COLUMNS) == ""


def test_check_subject_columns_accepts_columns_the_sheet_has():
    assert check_subject_columns(COLUMNS, build_column_map(HEADER), frozenset()) == []


def test_check_subject_columns_is_silent_when_none_are_listed():
    assert check_subject_columns([], build_column_map(["Subject"]), frozenset()) == []


def test_check_subject_columns_refuses_a_column_the_sheet_lacks():
    (problem,) = check_subject_columns(["theme", "subtheme"], build_column_map(HEADER), frozenset())

    assert problem.startswith(
        "subject_columns names 'subtheme', which is not a column in this Sheet. Known columns: "
    )


def test_check_subject_columns_refuses_a_tool_owned_column():
    column_map = build_column_map(HEADER + ["ia_url"])

    (problem,) = check_subject_columns(["ia_url"], column_map, frozenset())

    assert "'ia_url'" in problem and "this tool writes or generates" in problem


def test_check_subject_columns_refuses_a_held_back_column():
    column_map = build_column_map(HEADER + ["Notes (LCPS Internal)"])

    (problem,) = check_subject_columns(["notes_lcps_internal"], column_map, frozenset())

    assert "held back (LCPS Internal)" in problem


def test_check_subject_columns_refuses_a_file_location_column():
    column_map = build_column_map(HEADER + ["Folder"])

    (problem,) = check_subject_columns(["folder"], column_map, frozenset({"folder"}))

    assert "only locates each row's file" in problem


def test_check_subject_columns_refuses_a_standard_ia_field():
    """Sync removes every listed column from items, so this would delete real metadata."""
    column_map = build_column_map(HEADER + ["Description"])

    (problem,) = check_subject_columns(["description"], column_map, frozenset())

    assert "Internet Archive's own 'description' field" in problem


def test_check_subject_columns_refuses_a_sheet_with_its_own_subject_column():
    column_map = build_column_map(HEADER + ["Subject"])

    (problem,) = check_subject_columns(COLUMNS, column_map, frozenset())

    assert "the Sheet has its own 'subject' column" in problem


def test_check_subject_columns_messages_are_ascii():
    column_map = build_column_map(HEADER + ["Subject", "Description"])

    problems = check_subject_columns(["description", "nope"], column_map, frozenset())

    assert len(problems) == 3 and all(problem.isascii() for problem in problems)
