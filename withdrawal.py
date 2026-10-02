"""The `withdrawn` column: what a cell means, and which way a sync moves an item.

Knows nothing about Internet Archive or the Sheets API, so it never imports ia_bulk."""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from column_map import WITHDRAWN_COLUMN

# A run moving more items than this between withdrawn and present refuses without --allow-bulk-withdraw.
BULK_WITHDRAW_LIMIT = 10

# Folded before matching, so a checkbox's TRUE/FALSE reads like true/false.
_NO_SPELLINGS = frozenset({"", "no", "n", "false", "0"})
_YES_SPELLINGS = frozenset({"yes", "y", "true", "x", "1"})


class WithdrawnValue(Enum):
    NO = "no"
    YES = "yes"
    BROKEN = "broken"


def parse_withdrawn(cell: str | None) -> WithdrawnValue:
    text = (cell or "").strip().casefold()
    if text in _NO_SPELLINGS:
        return WithdrawnValue.NO
    if text in _YES_SPELLINGS:
        return WithdrawnValue.YES
    return WithdrawnValue.BROKEN


def withdrawn_error(cell: str) -> str:
    """The row error for a value that reads as neither yes nor no."""
    return (
        f"'{WITHDRAWN_COLUMN}' is {cell.strip()!r}, which reads as neither yes nor no - use "
        "yes, y, true, x or 1 to withdraw, or no, n, false, 0 or a blank cell to keep; nothing "
        "is done with this row until it does"
    )


def read_withdrawn_cell(row: Mapping[str, str]) -> tuple[WithdrawnValue, str | None]:
    """A row's `withdrawn` value and, when broken, its row error; absent reads as no."""
    cell = row.get(WITHDRAWN_COLUMN) or ""
    value = parse_withdrawn(cell)
    return value, withdrawn_error(cell) if value is WithdrawnValue.BROKEN else None


class SyncAction(Enum):
    UPDATE = "update"
    WITHDRAW = "withdraw"
    RESTORE = "restore"


def sync_action(value: WithdrawnValue, files_removed: bool) -> SyncAction:
    """`files_removed` is a non-blank `ia_withdrawn`; only a disagreement moves files."""
    if value is WithdrawnValue.BROKEN:
        raise ValueError("a broken withdrawn value has no sync action")
    if value is WithdrawnValue.YES and not files_removed:
        return SyncAction.WITHDRAW
    if value is WithdrawnValue.NO and files_removed:
        return SyncAction.RESTORE
    return SyncAction.UPDATE
