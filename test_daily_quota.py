"""Tests for daily_quota.py - the account's uploads in the last 24 hours, read
from the Sheet's `ia_uploaded` cells."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from daily_quota import DAILY_WINDOW, describe_refusal, measure_daily_quota, parse_uploaded_at
from utc_time import format_utc as stamp

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def hours_ago(hours: float) -> str:
    return stamp(NOW - timedelta(hours=hours))


def test_parse_uploaded_at_reads_the_utc_timestamp_the_tool_writes():
    assert parse_uploaded_at("2026-09-24T17:00:00Z") == datetime(
        2026, 9, 24, 17, 0, 0, tzinfo=timezone.utc
    )


def test_parse_uploaded_at_tolerates_surrounding_whitespace():
    assert parse_uploaded_at(" 2026-09-24T17:00:00Z ") is not None


def test_parse_uploaded_at_ignores_a_blank_cell():
    assert parse_uploaded_at("") is None


def test_parse_uploaded_at_ignores_a_value_the_tool_never_writes():
    assert parse_uploaded_at("yes") is None


def test_parse_uploaded_at_ignores_a_pre_utc_timestamp_with_no_zone():
    """Written as naive local time before 2026-08-23; the instant is unknowable."""
    assert parse_uploaded_at("2026-08-19T09:00:00") is None


def test_the_window_is_24_hours():
    assert DAILY_WINDOW == timedelta(hours=24)


def test_measure_counts_uploads_in_the_last_24_hours():
    quota = measure_daily_quota([hours_ago(1), hours_ago(23)], now=NOW, cap=10)

    assert quota.used == 2


def test_measure_skips_uploads_older_than_24_hours():
    quota = measure_daily_quota([hours_ago(1), hours_ago(25)], now=NOW, cap=10)

    assert quota.used == 1


def test_measure_skips_an_upload_exactly_24_hours_old():
    """It has just left the window, matching when room_opens_at says room opens."""
    quota = measure_daily_quota([hours_ago(24)], now=NOW, cap=10)

    assert quota.used == 0


def test_measure_counts_an_upload_stamped_in_the_future():
    """Another machine's clock running fast still spent the quota."""
    quota = measure_daily_quota([stamp(NOW + timedelta(minutes=5))], now=NOW, cap=10)

    assert quota.used == 1


def test_measure_skips_cells_it_cannot_read():
    quota = measure_daily_quota(["", "yes", "2026-09-29T11:00:00", hours_ago(1)], now=NOW, cap=10)

    assert quota.used == 1


def test_room_left_is_the_cap_less_what_was_used():
    quota = measure_daily_quota([hours_ago(1)] * 3, now=NOW, cap=10)

    assert quota.room_left == 7


def test_room_left_is_never_negative():
    """Possible when a run was let through with --allow-over-daily-cap."""
    quota = measure_daily_quota([hours_ago(1)] * 12, now=NOW, cap=10)

    assert quota.room_left == 0


def test_allows_a_run_that_fits_exactly():
    quota = measure_daily_quota([hours_ago(1)] * 3, now=NOW, cap=10)

    assert quota.allows(7)


def test_allows_an_empty_run_when_the_window_is_already_over_the_cap():
    """A batch with nothing left to upload must reach "nothing to upload", not a refusal."""
    quota = measure_daily_quota([hours_ago(1)] * 12, now=NOW, cap=10)

    assert quota.allows(0)


def test_does_not_allow_a_run_one_over_the_room_left():
    quota = measure_daily_quota([hours_ago(1)] * 3, now=NOW, cap=10)

    assert not quota.allows(8)


def test_room_opens_when_the_oldest_upload_leaves_the_window():
    quota = measure_daily_quota([hours_ago(2), hours_ago(20)], now=NOW, cap=2)

    assert quota.room_opens_at(1) == NOW - timedelta(hours=20) + DAILY_WINDOW


def test_room_opens_once_enough_uploads_leave_the_window_for_the_whole_run():
    cells = [hours_ago(1), hours_ago(5), hours_ago(20)]
    quota = measure_daily_quota(cells, now=NOW, cap=3)

    assert quota.room_opens_at(2) == NOW - timedelta(hours=5) + DAILY_WINDOW


def test_room_opens_at_rejects_a_run_that_already_fits():
    quota = measure_daily_quota([hours_ago(1)], now=NOW, cap=3)

    with pytest.raises(ValueError):
        quota.room_opens_at(2)


def test_room_never_opens_for_a_run_over_the_cap_on_its_own():
    quota = measure_daily_quota([], now=NOW, cap=3)

    assert quota.room_opens_at(4) is None


def test_refusal_with_nothing_uploaded_names_the_run_and_the_cap():
    quota = measure_daily_quota([], now=NOW, cap=5000)

    assert "this run would upload 6000 items, over Internet Archive's 5000/day cap" in (
        describe_refusal(quota, 6000)
    )


def test_refusal_with_earlier_uploads_names_them_and_the_room_left():
    quota = measure_daily_quota([hours_ago(1)] * 4800, now=NOW, cap=5000)

    assert "this Sheet shows 4800 uploaded in the last 24 hours, leaving room for 200" in (
        describe_refusal(quota, 350)
    )


def test_refusal_offers_the_room_left_as_a_limit():
    quota = measure_daily_quota([hours_ago(1)] * 4800, now=NOW, cap=5000)

    assert "Pass --limit 200 (or less)" in describe_refusal(quota, 350)


def test_refusal_with_no_room_left_offers_no_limit():
    quota = measure_daily_quota([hours_ago(1)] * 5000, now=NOW, cap=5000)
    message = describe_refusal(quota, 10)

    assert "leaving no room" in message
    assert "--limit" not in message


def test_refusal_says_when_the_whole_run_fits_in_utc():
    quota = measure_daily_quota([hours_ago(3), hours_ago(1)], now=NOW, cap=2)

    assert "(2026-09-30 09:00 UTC)" in describe_refusal(quota, 1)


def test_refusal_rounds_the_time_the_run_fits_up_to_the_minute():
    """Rounding down would name a minute when the run still does not fit."""
    quota = measure_daily_quota([stamp(NOW - timedelta(hours=3, seconds=30))], now=NOW, cap=1)

    assert "(2026-09-30 09:00 UTC)" in describe_refusal(quota, 1)


def test_refusal_for_a_run_over_the_cap_on_its_own_says_it_must_be_split():
    quota = measure_daily_quota([], now=NOW, cap=5000)
    message = describe_refusal(quota, 6000)

    assert "over the cap on its own" in message
    assert "fits after" not in message


def test_refusal_for_a_run_over_the_cap_with_no_room_left_says_when_room_opens():
    """No --limit fits now, so "the rest" has nothing to be the rest of."""
    quota = measure_daily_quota([hours_ago(3), hours_ago(1)], now=NOW, cap=2)
    message = describe_refusal(quota, 3)

    assert "split with --limit once room opens, after" in message
    assert "(2026-09-30 09:00 UTC)" in message
    assert "the rest" not in message


def test_refusal_names_the_override():
    quota = measure_daily_quota([], now=NOW, cap=5000)

    assert "--allow-over-daily-cap" in describe_refusal(quota, 6000)
