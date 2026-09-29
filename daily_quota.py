"""How much of Internet Archive's daily item cap the last 24 hours spent, read from `ia_uploaded` cells.

Rolling 24 hours, not a calendar day: IA's day boundary is unknown, and every upload since
any midnight is also inside the last 24 hours. See docs/decisions/QUOTA-AND-RUNS.md."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

DAILY_WINDOW = timedelta(hours=24)
# What utc_timestamp() in ia_bulk.py writes.
UPLOADED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_uploaded_at(cell: str) -> datetime | None:
    """None for anything but the tool's own UTC format, including pre-2026-08-23 naive local times."""
    try:
        return datetime.strptime(cell.strip(), UPLOADED_AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class DailyQuota:
    cap: int
    measured_at: datetime
    # Oldest first.
    uploads_in_window: tuple[datetime, ...]

    @property
    def used(self) -> int:
        return len(self.uploads_in_window)

    @property
    def room_left(self) -> int:
        return max(self.cap - self.used, 0)

    def allows(self, run_size: int) -> bool:
        # An empty run spends nothing, even when an override already took the window past the cap.
        return run_size == 0 or self.used + run_size <= self.cap

    def room_opens_at(self, run_size: int) -> datetime | None:
        """When enough uploads leave the window for the run to fit; None if it exceeds the cap on its own."""
        if run_size > self.cap:
            return None
        if self.allows(run_size):
            return self.measured_at
        must_leave_window = self.used + run_size - self.cap
        return self.uploads_in_window[must_leave_window - 1] + DAILY_WINDOW


def measure_daily_quota(uploaded_at_cells: Iterable[str], now: datetime, cap: int) -> DailyQuota:
    window_start = now - DAILY_WINDOW
    uploaded_at = (parse_uploaded_at(cell) for cell in uploaded_at_cells)
    in_window = sorted(moment for moment in uploaded_at if moment is not None and moment > window_start)
    return DailyQuota(cap=cap, measured_at=now, uploads_in_window=tuple(in_window))


def describe_refusal(quota: DailyQuota, run_size: int) -> str:
    """`upload`'s refusal. Names a time to come back because the upload page cannot pass --limit."""
    cap = f"Internet Archive's {quota.cap}/day cap"
    if quota.used:
        room = f"room for {quota.room_left}" if quota.room_left else "no room"
        situation = (
            f"this run's {run_size} items would put the account over {cap}: this Sheet shows "
            f"{quota.used} uploaded in the last 24 hours, leaving {room}."
        )
    else:
        situation = f"this run would upload {run_size} items, over {cap} for the account."

    ways_through = []
    if quota.room_left:
        ways_through.append(f"Pass --limit {quota.room_left} (or less) to upload that many now.")
    fits_at = quota.room_opens_at(run_size)
    if fits_at is None:
        ways_through.append("The run is over the cap on its own, so the rest has to wait for a later day.")
    else:
        ways_through.append(f"The whole run fits after {_local_and_utc(_round_up_to_minute(fits_at))}.")

    return "\n".join(
        [
            situation,
            " ".join(ways_through),
            "Identifiers are minted fresh each run, so nothing is lost by splitting it. Pass "
            "--allow-over-daily-cap to override if you know this account's cap has been raised.",
        ]
    )


def _round_up_to_minute(moment: datetime) -> datetime:
    truncated = moment.replace(second=0, microsecond=0)
    return truncated if truncated == moment else truncated + timedelta(minutes=1)


def _local_and_utc(moment: datetime) -> str:
    """Local time for whoever is at the machine; UTC to match the Sheet."""
    return f"{moment.astimezone():%Y-%m-%d %H:%M %Z} ({moment:%Y-%m-%d %H:%M} UTC)"
