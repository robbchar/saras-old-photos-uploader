"""Tests for utc_time.py - the tool's one recorded UTC timestamp format."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from utc_time import format_utc, parse_utc, utc_now


def test_format_utc_writes_iso_8601_with_a_trailing_z():
    assert format_utc(datetime(2026, 9, 29, 12, 5, 7, tzinfo=timezone.utc)) == "2026-09-29T12:05:07Z"


def test_format_utc_converts_another_zone_to_utc():
    pacific = timezone(timedelta(hours=-7))

    assert format_utc(datetime(2026, 9, 29, 5, 0, 0, tzinfo=pacific)) == "2026-09-29T12:00:00Z"


def test_parse_utc_reads_back_what_format_utc_writes():
    moment = datetime(2026, 9, 29, 12, 5, 7, tzinfo=timezone.utc)

    assert parse_utc(format_utc(moment)) == moment


def test_parse_utc_rejects_a_timestamp_with_no_zone():
    with pytest.raises(ValueError):
        parse_utc("2026-09-29T12:05:07")


def test_utc_now_is_timezone_aware_utc():
    assert utc_now().utcoffset() == timedelta(0)
