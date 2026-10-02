# Withdrawal

Pulling an item back off Internet Archive after upload, and putting it back.
Indexed in [`../DECISIONS.md`](../DECISIONS.md).

## Withdrawing deletes the files, replaces the text, and leaves darkening to a person

*Decided 2026-10-01.*

Internet Archive gives an uploader no way to delete an item: identifiers are
never released, and only an item's *files* can be deleted. `noindex` is
read-only after upload
([`KNOWN-ISSUES.md` #1](../KNOWN-ISSUES.md#1-noindex-cannot-be-changed-by-sync-metadata)).
Darkening — taking an item fully offline — is done only by IA staff, on
request, and only they can undo it.

So a withdraw does the two parts the tool can do.

It deletes every original in the item that is not IA's own, each with IA's
cascade, so its derivatives go too, and with `x-archive-keep-old-version: 0`,
so IA keeps no `history/` copy. A derivative is deleted by name only once no
original is left to cascade it (a leftover a re-check finds), so no delete
is sent for a file another delete already covers. Content files go first and IA's item tile
(`__ia_thumb.jpg`) last; in a pass where IA refused a content delete, the
tile is kept, because IA would rebuild it from the file still there. Any 2xx
answer is an accepted delete, and a 404 counts as already gone. IA's own
system files (`_meta.xml`, `_files.xml`, `_meta.sqlite`, `_archive.torrent`,
`_reviews.xml`) cannot be deleted and stay.

And it replaces the item's text: `title` and `description` become the
project's `withdrawn_title` and `withdrawn_description` (registry keys,
defaulting to `Withdrawn` and *This item has been withdrawn by the Lower
Columbia Preservation Society.*), and every other field the row sends — plus
the `identifier-bib` and `date` that `upload` generates — is removed with
`REMOVE_TAG`. `identifier`, `collection` and `mediatype` are never sent.

The third part, asking IA to darken the item, stays with a person: it is a
request to people, not an API call. After a live withdraw the run prints the
identifiers and archive.org URLs to send, including any withdraw that started
but did not finish.

A field IA holds that the Sheet never had (one added by hand on archive.org)
is not removed: the tool clears only what it sends.

## Withdrawal is a Sheet column, and `sync-metadata` makes IA match it

*Decided 2026-10-01.*

One person-edited column, `withdrawn`, says which side an item should be on;
one tool-written column, `ia_withdrawn`, records which side it is on — the UTC
time the withdrawal started (IA accepted the deletes), blank while the files
are present. `sync-metadata` acts only where the two disagree: yes and blank
withdraws, no and stamped restores. A value that reads as neither yes nor no
is a row error, and that row is left alone.

Living in `sync-metadata` rather than a new command puts the reverse
direction in the same place — set the cell back to no and the original is
re-uploaded and the Sheet's text written back (see "A restore waits until
Internet Archive has finished the withdrawal", below) — and gives both
directions the hourly schedule, the per-row log and the identity checks for
free. A row withdrawn before it was ever uploaded is simply held: `upload`
skips it and keeps any identifier already reserved (see
[`READINESS.md`](READINESS.md#a-withdrawn-row-is-held-not-not-ready-or-broken)).
A reserved row whose upload landed without being confirmed is held too, and
`sync-metadata` only acts on uploaded rows, so nothing takes it down. The
route is to confirm it first: set `withdrawn` to no, run `upload` (which
retries under the reserved identifier and confirms), set `withdrawn` back to
yes, and run `sync-metadata`. The photo is public in between (see
[`OPERATIONS.md`, "Withdrawing an item"](../OPERATIONS.md#withdrawing-an-item)).

What a withdrawn row hashes is the withdrawn notice, not its cells: edits to
its other cells wait for a restore, and flipping the column always pushes
(see [`SHEET-PROTOCOL.md`, "A row pushes only when its content changed"](SHEET-PROTOCOL.md#a-row-pushes-only-when-its-content-changed)).
Clearing `ia_withdrawn` by hand forces the tool to treat the item as present,
the same override role clearing `ia_sync_hash` has. A Sheet without the
`withdrawn` column never withdraws or restores anything, whatever
`ia_withdrawn` holds, so deleting the column cannot mass-restore. Nor can it
republish the text: without the column, a row whose `ia_withdrawn` is set,
or whose `ia_sync_hash` is the hash of the withdrawn notice (both columns
deleted), would otherwise push its Sheet text over the notice. Each one is
skipped by name instead — nothing is sent for it, and the run exits 1 —
until the `withdrawn` column is put back beside `ia_withdrawn`. The hash
check is best effort: a withdrawn row whose notice wording or Sheet columns
changed since it was stamped no longer matches it.

Files move only in the row's own item. Before planning a withdraw, a re-check
or a restore, the run checks that the item its `ia_url` names is this row's
`ia_identifier` (live) or `zztest-<stamp>-<ia_identifier>` for some stamp
(test mode); anything else — a URL pasted from another row, say — is refused
by name, and nothing is deleted or uploaded.

Each withdraw re-reads the Sheet just before its deletes and goes ahead only
if the row is still where the run read it (its `file_template` fingerprint and
`ia_identifier`) **and** its `withdrawn` cell still reads yes. The re-read is
before the delete rather than after, and per withdraw rather than per chunk,
because a delete cannot be sent again. With the re-read before its mark
(below), a withdraw costs two Sheet reads and one write, at most ten
withdraws a run unless the limit below is overridden. A row that fails it
is refused by name, nothing is deleted, and the run exits 1 — a row un-ticked
mid-run included, so a destructive step never changes course unseen; the next
run follows what the Sheet says then. A row whose `file_template` cells are
blank cannot be confirmed, so it is not withdrawn until they are filled in. A
withdraw of an item Internet Archive does not have fails by name: nothing is
deleted or stamped, and the darkening hand-off does not list it.

`ia_withdrawn` is written as soon as IA accepts a delete, even if a later
delete or the text write fails: right after the deletes, after one more
re-read confirms the row has not moved, and before the text write — not with
the chunk's batch at its end — so a run interrupted from then on (Ctrl-C
during the text write, a shutdown, a crash) has already recorded every
withdrawal it started. Once the text landed, the notice's hash goes with the
chunk's batch; losing it costs one re-push of the notice. A withdraw that
never started repeats whole on the next run; one that started is finished by
the next run's metadata push and re-check (below). If even the
`ia_withdrawn` write is lost — the row moved, or the Sheet write failed —
the run names the items and says to keep `withdrawn` at yes: setting it to
no before the next run would leave the files deleted with nothing to restore
them.

A withdraw IA refused entirely is not marked, but its notice is still
written, so when the notice landed its hash is written at once. Without it,
setting `withdrawn` back to no would read as already in sync — the old
hash still matches the Sheet — and leave the notice on the item for good;
with it, that row pushes the Sheet's text back. If that hash cannot be
written, the run says to clear `ia_sync_hash` before setting `withdrawn` to
no.

## A withdraw converges across runs, and the tool never waits on IA

*Decided 2026-10-01, after a hand spike on `test_collection`.*

One pass of cascade deletes does not reliably remove the photograph. The spike
saw it fail two ways. Deleting the tile first let IA rebuild `__ia_thumb.jpg`
from an original whose delete it had not processed yet. And a derive that ran
ahead of queued deletes created a `_thumb.jpg` after the original's cascade
had been submitted, so the cascade missed it and IA built a new tile from it.
Both times the item page showed the photograph after every delete had been
accepted. A second pass, with nothing left queued, cleared the item.

So a withdraw is not one action but a state the tool keeps converging on.
The withdraw run deletes content before the tile and marks `ia_withdrawn`
when the deletes are accepted. Every later `sync-metadata` run re-checks each
withdrawn, stamped item:

- It first asks IA's task catalog about the item; while any task is open on
  it — queued, running, paused or failed — it deletes nothing and the item is
  *still clearing*. A queued task is most likely the withdraw's own delete:
  sending it again every run would only pile up duplicate tasks (see "A
  delete waits while IA runs or holds a task on the item", below).
- It lists the item's files. Whatever is not IA's own is deleted again, in
  the same order and with the same header (originals, or leftover
  derivatives once no original is left), and the item is reported *still
  clearing*.
- Once only IA's own files are left, it asks IA's task catalog again
  whether anything is still open for the item; anything queued, running,
  paused or errored is *still clearing*.
- With no task open, it lists the files once more — a derive can finish
  between the first listing and the task query and add a photo file — and
  reports the item *clear* only if that listing is still IA's own files
  alone. A false *clear* is the costly mistake: operators stop re-running
  there, and a photo left behind stays public.

An item Internet Archive does not have, or one the re-check cannot reach, is
that item's re-check failure — named, counted as an error, never *clear*.
Re-checks never touch the text, never write to the Sheet (no restamp), and
never count toward the per-run limit.

The tool never waits on IA's queue: on `test_collection` the spike watched a
derive sit queued for over an hour and a half. Blocking a run on it would hold
the hourly agent hostage to IA's load. The accepted trade-off is that the
item tile can stay visible until a later run clears it — hours if IA's queue
is backed up. The operator runs `sync-metadata` again (or lets the hourly
agent) until every item reports clear; the darkening request can go at once.
An IA task that errors, or that IA pauses for its staff, stays in the
catalog and keeps an item *still clearing* until IA staff act — see
[`KNOWN-ISSUES.md` #8](../KNOWN-ISSUES.md#8-an-errored-or-paused-internet-archive-task-blocks-clear-and-restore).

## A restore waits until Internet Archive has finished the withdrawal

*Decided 2026-10-01, during the e2e rehearsal's review.*

A restore re-uploads the item's original and writes the Sheet's text back.
If IA still has the withdrawal's deletes queued, a delete processed after the
re-upload removes the file again, and the Sheet says "restored" over an empty
item. The spike saw IA hold a queue for hours.

So a restore first asks IA's task catalog whether anything is still open
(queued, running, paused or errored) for the item. While anything is — or
when that question cannot be answered — the restore is refused by name,
nothing is uploaded, `ia_withdrawn` keeps its value, the run exits 1, and the
next run tries again. A paused or errored task is named as IA staff's to
release, not as IA "still processing": waiting will not clear it. Like a
re-check, it never waits.

The original is found the same way `upload` finds it, from `files_dir` and
`file_template` — the only time `sync-metadata` reads the drive. One that is
missing, matches more than one file, or whose `file_template` cells are blank
refuses the row by name, nothing is sent, and the item stays withdrawn. So
does one that is not the file `ia_identifier_bib` says was uploaded — a
filename cell edited while the item was withdrawn, say: a restore never puts
a different photograph into the permanent item. An
upload into an existing item does not set its metadata, so after re-uploading
the restore writes the text with its own metadata call: the Sheet's fields,
plus the `identifier-bib` and `date` `upload` would generate, and the notice
removed where the Sheet's title or description cell is blank. Success stamps
the hash and clears `ia_withdrawn`; a failure leaves both, and the restore
repeats next run. A restore that landed but whose clear could not be written
(the row moved, or the Sheet write failed) is named in a warning: a `yes`
typed before the next run finishes it would find `ia_withdrawn` still set,
skip the withdraw, and leave the restored text up.

## A delete waits while IA runs or holds a task on the item

*Decided 2026-10-02, after the e2e rehearsal on `test_collection`.*

The rehearsal withdrew two items about a minute after uploading them, while
each item's first `derive.php` was running. IA accepted every delete, then
put the delete tasks on hold for its staff: status `paused`, `wait_admin` set,
no task log, unchanged nine hours later. Deletes sent while an item was idle,
or had a derive merely queued, processed normally — with the same
`x-archive-keep-old-version: 0` header, so the header is not the cause. A
paused task never resumes by itself, so the restore guard refused forever and
the re-check never reported clear, while their messages said IA was "still
processing".

So before a withdraw's deletes — after its fresh Sheet read — the tool asks
IA's task catalog about the item. While a task is running, paused or failed,
the row is refused by name: nothing is deleted, written or stamped, it is not
"withdrawal started" and not in the darkening hand-off, the run exits 1, and
the next run tries again. A failed task holds the item for IA staff just as a
paused one does, so deletes sent behind it might never run. A query that
cannot be answered refuses the same way; a withdraw never deletes blind.
Queued tasks don't block a withdraw: deletes sent then processed normally.
The re-check asks the same question before its deletes and waits for more:
while IA has any task open on the item, queued included, it deletes nothing
and reports the item still clearing with the reason. A paused or failed task
is named as IA staff's to release in the withdraw, the re-check and the
restore, because waiting cannot clear it; the operator is emailing IA staff
for the darkening anyway. A task whose state cannot be read counts as
running: never deleted past, never clear.

The tool still never waits on IA: a refused withdraw is retried by the next
run, as the hourly agent would. Waiting out a running derive costs a few
minutes after an upload, and a withdraw is rarely that close to one.

## One run may move at most ten items, and the limit is per run

*Decided 2026-10-01.*

A fill-down or a paste over the `withdrawn` column looks, to the tool,
exactly like a decision to withdraw a whole batch — and the hourly agent
would carry it out unattended. So a run whose withdraws plus restores exceed
10 refuses entirely before anything is sent, and names the rows, unless
`--allow-bulk-withdraw` is passed; the dry run previews the run, shows the
same refusal and exits 1. A refused real run still writes its log — a
`run_summary` whose `refused` says why — and a `refused` row in the
`sync_log_tab`, because the hourly agent's refusal is otherwise only on a
console nobody reads; the dry run writes neither, as always. Only rows whose `withdrawn` cell and
`ia_withdrawn` disagree count — every uploaded one, counted before any
per-row refusal, so a row refused for another reason (a missing original, an
`ia_url` that is not its own item) still counts: a paste over the column is
just as much a mistake when some of the rows it hit could not be acted on.
Already-withdrawn, stamped rows (re-checks)
don't, and neither does a withdraw that started but did not finish: its
`ia_withdrawn` is stamped, so it returns as an ordinary update plus a
re-check (unless that stamp was lost too, when it repeats as a withdraw). A withdraw that never started (refused before its deletes, an item
IA does not have, a file list that could not be read) and a restore that
failed both repeat as a withdraw or restore next run, and count again.

Per run rather than a rolling window: the danger is one bad edit, which a
per-run cap stops on the first run after it, and a window would need state
the Sheet does not keep. Restores re-upload a file but are not counted in the
rolling 5,000/day total, which reads `ia_uploaded` — see
[`KNOWN-ISSUES.md` #7](../KNOWN-ISSUES.md#7-restores-are-not-counted-in-the-daily-upload-total).

A live run with any withdraw or restore to send also runs the archive.org
collection check `upload --live` uses
([`FOUNDATIONS.md`, "A live upload goes only into a collection archive.org confirms"](FOUNDATIONS.md#a-live-upload-goes-only-into-a-collection-archiveorg-confirms))
and refuses the run when archive.org cannot confirm the collection or cannot
be reached, recorded the same way as a bulk-limit refusal. Re-checks and test-mode runs skip it: a re-check only deletes
from an item already withdrawn, and test mode targets `test_collection`.
