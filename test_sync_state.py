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
