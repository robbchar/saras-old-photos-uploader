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
