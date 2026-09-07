"""Tests for sync_state.py - the per-row sync state sync-metadata keeps in
the Sheet."""
from __future__ import annotations

import pytest

from sync_state import sync_hash


def test_sync_hash_is_stable_for_the_same_content():
    assert sync_hash({"title": "Pier 39"}) == sync_hash({"title": "Pier 39"})


def test_sync_hash_ignores_key_order():
    """Reordering columns in the Sheet is not an edit, and must not re-push
    4,000 rows."""
    assert sync_hash({"title": "Pier 39", "date": "1908"}) == sync_hash(
        {"date": "1908", "title": "Pier 39"}
    )


def test_sync_hash_changes_when_a_value_changes():
    assert sync_hash({"title": "Pier 39"}) != sync_hash({"title": "Pier 40"})


def test_sync_hash_changes_when_a_field_is_added():
    assert sync_hash({"title": "Pier 39"}) != sync_hash({"title": "Pier 39", "date": "1908"})


def test_sync_hash_changes_when_a_field_is_removed():
    """Clearing a cell removes the field from what is sent (blank means
    "leave alone"), so the hash changes and the row pushes once. Internet
    Archive reports it unchanged, the row is stamped, and it goes quiet."""
    assert sync_hash({"title": "Pier 39", "date": "1908"}) != sync_hash({"title": "Pier 39"})


def test_sync_hash_distinguishes_values_that_would_run_together():
    """A naive "".join of keys and values collides here. json.dumps does not."""
    assert sync_hash({"a": "xy", "b": "z"}) != sync_hash({"a": "x", "b": "yz"})


def test_sync_hash_handles_non_ascii_text():
    """ensure_ascii=False, so the hash is over the real text. Escaped or not
    it would be stable, but it must not RAISE - donor metadata carries
    accented place names."""
    assert len(sync_hash({"title": "Astoria, Orégon"})) == 64


def test_sync_hash_of_no_fields_is_a_real_hash_not_an_empty_string():
    """A row whose every metadata cell is blank still gets a hash, so it
    stamps and stops re-pushing like any other row."""
    assert len(sync_hash({})) == 64


from column_map import build_column_map
from sync_state import MissingSyncColumns, SyncColumns, locate_sync_columns


def test_locate_sync_columns_finds_both_columns_by_index():
    column_map = build_column_map(["Title", "ia_sync_hash", "ia_last_synced"])
    assert locate_sync_columns(column_map) == SyncColumns(ia_sync_hash=1, ia_last_synced=2)


def test_locate_sync_columns_takes_the_first_of_a_duplicated_column():
    """check_column_map already reports duplicate headers as a defect that
    stops the run. Picking the first here is only so this function has one
    definite answer rather than depending on dict iteration order."""
    column_map = build_column_map(["ia_sync_hash", "ia_last_synced", "ia_sync_hash"])
    assert locate_sync_columns(column_map).ia_sync_hash == 0


def test_locate_sync_columns_names_every_missing_column_at_once():
    """One pass, not one run per missing column - an operator adding columns
    to a Sheet should be told everything to add before they go and do it."""
    column_map = build_column_map(["Title", "file"])
    with pytest.raises(MissingSyncColumns) as excinfo:
        locate_sync_columns(column_map)
    message = str(excinfo.value)
    assert "ia_sync_hash" in message
    assert "ia_last_synced" in message


def test_locate_sync_columns_message_says_what_to_do():
    """The reader is a volunteer with a spreadsheet open, not a developer."""
    column_map = build_column_map(["Title"])
    with pytest.raises(MissingSyncColumns) as excinfo:
        locate_sync_columns(column_map)
    assert "add them as header cells" in str(excinfo.value)


def test_sync_columns_renders_a1_references():
    columns = SyncColumns(ia_sync_hash=6, ia_last_synced=7)
    assert columns.cell(columns.ia_sync_hash, 4) == "G4"
    assert columns.cell(columns.ia_last_synced, 4) == "H4"
