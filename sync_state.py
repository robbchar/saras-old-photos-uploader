"""Per-row sync state, kept in the Sheet.

`sync-metadata` used to send every uploaded row on every run. Internet
Archive answers "no changes to _meta.xml" for an item that already matches,
so that was safe - but at ~4,000 items on an hourly schedule it is ~4,000
pointless writes an hour, and it makes the run log useless: a real edit is
indistinguishable from the background noise. See docs/DECISIONS.md, "A row
pushes only when its content changed".

The state lives in the Sheet rather than a local file for two reasons: it
survives the machine being wiped or replaced, and it gives a non-technical
operator a recovery lever that can be described over the phone - clear a
row's `ia_sync_hash` cell to re-sync that row, clear the column to re-sync
everything.

Knows about hashes, columns and cells. Knows nothing about Internet
Archive, so it never imports ia_bulk."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from column_map import ColumnMap, IA_LAST_SYNCED_COLUMN, IA_SYNC_HASH_COLUMN
from sheet_client import column_letter


def sync_hash(metadata: dict[str, str]) -> str:
    """A row's content, as one opaque string to compare against next run.

    Takes the metadata dict as it would actually be SENT - the output of
    ia_fields.metadata_to_send() - not the raw row. Hashing the raw cells
    instead would make the hash disagree with the push about what a row
    means, and a hash that disagrees with the push either re-pushes a row
    forever or silently swallows an edit.

    sort_keys=True so reordering columns in the Sheet is not an edit.
    ensure_ascii=False so the digest is over the real text rather than its
    escaped form; either is stable, but the real text is the thing being
    described. json.dumps rather than concatenation because "xy"+"z" and
    "x"+"yz" must not collide.

    The full 64-character digest is stored. It lands in a hidden column no
    one reads, so there is nothing to gain by truncating it and a collision
    would silently withhold a correction."""
    payload = json.dumps(metadata, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


SYNC_STATE_COLUMNS = (IA_SYNC_HASH_COLUMN, IA_LAST_SYNCED_COLUMN)


class MissingSyncColumns(Exception):
    """The Sheet has no column for the sync state this command keeps.

    Checked before anything is sent, and in BOTH modes. Without the columns
    every row would push on every run and nothing would ever fail to say so
    - under an unattended hourly schedule that is silent, permanent noise,
    which is the exact failure this feature exists to remove. It must not be
    its own fallback state."""


@dataclass(frozen=True)
class SyncColumns:
    """Zero-based grid indexes of the two columns `sync-metadata` writes.

    Same shape and method name as ia_bulk.SheetColumns, deliberately: the two
    are read side by side in SheetSyncRun and a reader should not have to
    hold two spellings of one idea."""

    ia_sync_hash: int
    ia_last_synced: int

    def cell(self, column_index: int, row_number: int) -> str:
        return f"{column_letter(column_index)}{row_number}"


def locate_sync_columns(column_map: ColumnMap) -> SyncColumns:
    """Where the sync-state columns are in the grid right now.

    Required in both test and live mode, like the four write-back columns
    (see docs/DECISIONS.md, "The four `ia_` columns are required in every
    mode, including the safe one"). A rehearsal that gates where the real run
    would not is not a rehearsal.

    Sync-path only, unlike those four: `upload` and `validate` neither read
    nor write these columns, so making them refuse over an absence would be a
    requirement with nothing behind it."""
    indexes: dict[str, int] = {}
    for index, header in enumerate(column_map.headers):
        field_name = column_map.field_names[header]
        if field_name in SYNC_STATE_COLUMNS and field_name not in indexes:
            indexes[field_name] = index

    missing = [name for name in SYNC_STATE_COLUMNS if name not in indexes]
    if missing:
        raise MissingSyncColumns(
            f"the Sheet has no column(s) named {', '.join(missing)}. `sync-metadata` "
            f"records what it last pushed in {', '.join(SYNC_STATE_COLUMNS)} so it can send "
            "only the rows that actually changed; add them as header cells (any position, "
            "spelling exactly as shown - far right and hidden is fine) before syncing."
        )

    return SyncColumns(
        ia_sync_hash=indexes[IA_SYNC_HASH_COLUMN],
        ia_last_synced=indexes[IA_LAST_SYNCED_COLUMN],
    )
