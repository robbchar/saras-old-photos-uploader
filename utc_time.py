"""The one UTC timestamp format this tool records (`ia_uploaded`, logs, the e2e lock tab), and reading it back."""
from __future__ import annotations

from datetime import datetime, timezone

UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(UTC_TIMESTAMP_FORMAT)


def parse_utc(text: str) -> datetime:
    """Raises ValueError for anything but UTC_TIMESTAMP_FORMAT."""
    return datetime.strptime(text, UTC_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
