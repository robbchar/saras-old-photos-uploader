# IA Bulk Upload CLI — Architecture

This is the design reference. For running a batch see
[`OPERATIONS.md`](OPERATIONS.md); for verified defects see
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md); for rationale and reversed decisions see
[`DECISIONS.md`](DECISIONS.md).

## Purpose
Single-script CLI (`ia_bulk.py`) for validating, uploading, and syncing
metadata for Internet Archive items from a project's Google Sheet, read live
over the Sheets API — see [`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).
The Sheet is the only input; the offline CSV paths were removed on
2026-09-23. Generic to "a
project" so a second LCPS project can reuse this pipeline — see
`projects_registry.json`.

## Sheet source and column mapping
`validate`/`upload` read a project's Sheet (its test Sheet by default, its
real one with `--live`) via `build_sheet_client()` → `SheetClient.read_grid()`,
then `grid_to_rows()` turns the grid into a `list[dict[str, str]]`, one dict
per row keyed by normalized header, which every downstream function
(`validate_rows`, `upload_row`, `effective_identifier`, the whole `zztest-`
safety rail) takes as its input.

**Header normalization.** `normalize_header()` (`column_map.py`) is the one
rule that turns a Sheet header into an IA metadata field name: lowercase,
strip surrounding whitespace, drop punctuation (hyphens and underscores kept,
since IA field names like `identifier-bib` use them), collapse whitespace and
repeated underscores to single underscores. It does not correct typos — a
misspelled header ships as-is — see
[`DECISIONS.md`](decisions/READINESS.md#a-malformed-header-is-rejected-never-auto-corrected).
`ColumnMap.field_names` is the resulting `{raw header: normalized name}` map
for every header the Sheet had that run; `check_column_map()` rejects two
headers that normalize to the same field name (a silent per-row overwrite)
and a header that normalizes to an empty name, filing both under row 1.
`check_grid_shape()` flags a data row with more fields than the header, with its real row number, since the Sheets API already omits trailing
empty cells (a short row is not a defect) but never inserts anything.

**Held-back columns.** A header containing `(LCPS Internal)`, matched
case-insensitively, is recorded in `ColumnMap.held_back` and excluded by
`uploadable_fields()` — it is normalized and reported on (so its transform is
still visible), but never uploaded.

**Tool-owned columns.** `RESERVED_FIELDS` (`column_map.py`) is six `ia_`-prefixed
columns plus `file`, all excluded by `uploadable_fields()` so the tool's own
bookkeeping never ships as IA metadata. `upload` writes four of them:
`ia_identifier`, `ia_uploaded`, `ia_url`, `ia_identifier_bib`. `sync-metadata`
owns the other two, `ia_sync_hash` and `ia_last_synced`, which record what a
row last successfully pushed so an unchanged row is not resent — see
[`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#a-row-pushes-only-when-its-content-changed).
The `ia_` prefix is a naming convention only; `RESERVED_FIELDS` being an
explicit set rather than a `startswith("ia_")` rule is what actually does the
excluding, and is itself the enforcement — see
[`DECISIONS.md`](decisions/IDENTIFIERS.md#tool-owned-sheet-columns-are-all-ia_-prefixed).
The Sheet's own `identifier` column, if it has one, is ordinary donor metadata
(the archival reference the donor supplied), never the minted IA identifier.
`upload`'s four `ia_` columns must already exist as Sheet headers before
`upload` will run, in every mode including the default rehearsal — see
[`DECISIONS.md`](decisions/IDENTIFIERS.md#the-four-ia_-columns-are-required-in-every-mode-including-the-safe-one).
`sync-metadata`'s two are required the same way, by that command alone — see
[`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#a-row-pushes-only-when-its-content-changed).

`format_field_receipt()` prints, before anything permanent happens, exactly
which normalized fields will upload and which are held back. `file` and the
six `ia_` columns never reach this list at all — `uploadable_fields()`
already excludes them via `RESERVED_FIELDS` (see "Tool-owned columns" above).
The receipt separates two different reasons a column does not ship. "NOT
uploaded — Internet Archive reserves these names" is `identifier` alone
(`DROPPED_BY_UPLOAD_ROW`): the one name `upload_row` itself strips that
isn't already tool-owned, since the Sheet's own `Identifier` column, if it
has one, is ordinary donor metadata rather than something `RESERVED_FIELDS`
would catch earlier. "uploaded with a value this tool generates" is
`mediatype` and `collection` (`ia_fields.PIPELINE_OWNED_FIELDS`): those
fields *are* sent, but `upload_from_sheet` overwrites `row['mediatype']`
from the registry and `upload_row` sets `metadata['collection']`
unconditionally, so a Sheet column of either name has its own value
discarded. That section prints only when such a column actually exists —
it is a collision warning, not a standing disclaimer. See
[`DECISIONS.md`](decisions/FILES-AND-METADATA.md#sheet-metadata-is-filtered-at-the-upload-boundary-not-in-upload_row).

**File resolution.** A row's file is *resolved*, not constructed from a
path template: `resolve_file()` looks in the folder named by `file_template`'s
substituted columns for an exact filename match, then a case-insensitive
stem match, and raises rather than picks between two candidates that share a
stem — see
[`DECISIONS.md`](decisions/FILES-AND-METADATA.md#a-file-is-found-by-resolution-not-by-constructing-a-path).
The resolved name (which can differ from what the Sheet cell says) becomes
both `row["file"]` and `ia_identifier_bib`.

**Required columns.** `mediatype` is injected from the registry
(structurally required, never a Sheet column); `file` is resolved, not
required, since its presence is fully determined by file resolution (see
"Readiness" below); `title` is in the project's `required_for_upload` list;
`ia_identifier` is optional until `upload` mints one. `SHEET_REQUIRED_COLUMNS`
is `("mediatype",)` — everything else is either resolved or a readiness
question. `date` is optional and free-form (IA doesn't enforce a date
format): `upload_row` fills a blank `date` with `[n.d.]` (the standard
archival "no date" abbreviation) rather than omitting the field, so every IA
item ends up with a date value either way.

## Identifier scheme
See `.claude/CLAUDE.md` for the full identifier scheme and project
registry rationale. `projects_registry.json` holds each project's
`ia_collection`, Sheet IDs, `file_template`, and `required_for_upload` list,
plus the shared `collection_key`; `validate` rejects any `ia_identifier`
whose prefix isn't registered there — and, since issue #2, any
whose `PROJECTID` names a registered project other than the run's own
`--project`. `sync-metadata` targets the item `ia_url`
names rather than an identifier column, so it makes the `--project` half of
that check against that item and does not run the registry-prefix check at
all. See
[`IDENTIFIERS.md`](decisions/IDENTIFIERS.md#an-identifier-is-checked-against-the-runs-project-not-the-whole-registry).

The permanent identifier always holds the real, permanent value — `check_identifier`
only accepts the registry's actual `collection_key` as the first segment.
There is no separate "test" identifier form in the Sheet; see
"Safety rail" below for how test runs are kept safe instead. The permanent
identifier is minted by `upload` and written to
`ia_identifier` (see "Sheet source and column mapping" above and
[`DECISIONS.md`](decisions/IDENTIFIERS.md#identifiers-are-minted-by-upload-and-written-back-to-the-sheet)).

## Readiness
A row can be **not-ready** (a human hasn't filled in what it needs yet) or
**invalid** (it asserted something and got it wrong) — orthogonal questions
about the same row, both tracked on `RowValidation`: `errors`/`is_valid` for
validity, unchanged in meaning, and a new `missing_fields` list whose
`readiness` property (`Readiness.READY`/`Readiness.NOT_READY`) is derived from
it, never stored separately. The reasoning — why blank and wrong are different
kinds of failure, why this isn't a fourth `RowState`, and why `validate` and
`upload` report the backlog differently — lives entirely in
[`DECISIONS.md`, "A blank cell is not an error"](decisions/READINESS.md#a-blank-cell-is-not-an-error);
this section only covers the mechanism.

**Two sources feed `missing_fields`, always in this order:** the project's
`required_for_upload` list (normalized column names, e.g. `["title", "theme"]`,
checked by `validate_sheet_rows`) and the `file_template` columns that were
blank (found by `resolve_sheet_files`, which never calls the file resolver at
all for a blank or whitespace-only candidate — see `FileOutcomes.blank`).
Classification happens at that point, in `resolve_sheet_files`, because it is
the last point the raw candidate still exists: afterward, a row nobody
touched and a row with a typo'd filename are both `row["file"] == ""` and
indistinguishable. A non-blank candidate that fails to resolve is recorded in
`FileOutcomes.errors` instead — a real error, not a readiness fact. So are
ALL rows of a group resolving to the same disk file (keyed through
`claim_key()`, like the reconcile survey): two rows cannot claim one
photograph, and identical `file_template` cells would also blind the
mid-run-edit guard's fingerprint. Only the remedy differs between them — a
group holding exactly one already-uploaded row names that row as the one to
keep, since its identifier is permanent and its row is the only link to that
identifier's metadata. See
[`SHEET-PROTOCOL.md`](decisions/SHEET-PROTOCOL.md#a-fingerprint-only-proves-identity-while-it-is-unique).

**`required_for_upload` is a registry key, not a code constant.** It has no
default (a missing key is a hard `ConfigError` from `load_project_config`),
and `check_required_for_upload()` cross-checks every name in it against the
Sheet's actual normalized headers at startup, failing loudly on a typo rather
than silently marking every row not-ready forever.

**Reporting.** `format_readiness_breakdown()` counts not-ready rows by which
field is missing (derived from each result's own `missing_fields`, never a
hardcoded list), printed by `validate`. `upload` does not print this
breakdown — see `DECISIONS.md` as linked above — and only a row that was
actually in scope (ready, but failing validation) affects `upload`'s exit
code.

**One verdict, three readers.** `RowValidation.verdict` (`UploadVerdict.READY`/
`INVALID`/`NOT_READY`) combines the two questions into one answer to "is
this row uploadable": not-ready takes precedence over invalid. It does not
say whether the row is already uploaded — `classify_row` still does.
`validate`'s lifecycle summary buckets by it within each lifecycle state,
`plan_upload_targets` targets `READY` rows that are not `DONE`, and
`upload_from_sheet` itemizes `INVALID` and counts `NOT_READY` from it — none
of them recompute the rule, so `upload` targets exactly the rows `validate`
reports as "ready to upload" plus those "reserved but unconfirmed".
Filtering on `is_valid` alone
once uploaded an uncatalogued row under a permanent identifier with no title;
that is the drift this closes.

## The reserve → upload → confirm protocol
Per chunk, `SheetUploadRun.execute()` does four things in this order:

1. **verify** — re-reads the Sheet and checks, per target, that its
   `file_template` columns still fingerprint the same photograph, that —
   before the reserve write only — no OTHER row in the fresh read carries
   the same fingerprint (a duplicated fingerprint proves nothing about which
   physical row is underneath; after reserve the row's own proven-unique
   number is the stronger proof, and vetoing there would withhold the
   confirm write for edits that shifted nothing — see
   [`SHEET-PROTOCOL.md`](decisions/SHEET-PROTOCOL.md#a-fingerprint-only-proves-identity-while-it-is-unique)),
   and that `ia_identifier` is still blank or already ours (see
   [`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#a-rows-identity-is-its-file_template-columns-not-its-ia_identifier)
   for why the fingerprint, not `ia_identifier`, is what makes this check
   meaningful). Targets that moved are reported and skipped, never written to.
   Before the reserve write only, `check_claimed_identifiers()` additionally
   compares this run's *minted* numbers against every `ia_identifier` the
   fresh read holds, anywhere in the Sheet. The per-target check above sees
   only a target's own row, so a number claimed on a row this run is not
   targeting is invisible to it — and that is the case that mints a
   duplicate. A collision stops the whole run rather than dropping one
   target: every number came out of the same `max+1` arithmetic over the
   same stale read, so one collision means the maximum was wrong and the
   rest are suspect. Nothing is reserved or uploaded at that point, so a
   rerun re-mints from the Sheet's current state.
2. **reserve** — one batch write (`write_cells_if_any`) putting each target's
   minted `ia_identifier` in the Sheet, before any upload happens.
3. **upload** — row by row, via `upload_row()`/`internetarchive.upload()`, so
   each row gets its own logged outcome.
4. **verify, then confirm** — re-verifies the rows that actually succeeded,
   then one batch write of `ia_uploaded`, `ia_url`, and `ia_identifier_bib` —
   only for rows whose upload succeeded and whose fingerprint still matches.

Reserving before uploading is deliberate: uploading first would let a crash
strand an item on Internet Archive that the Sheet has no record of, and the
next run's minting would then reuse that same number for a different
photograph, permanently — see
[`DECISIONS.md`](decisions/IDENTIFIERS.md#identifiers-are-minted-by-upload-and-written-back-to-the-sheet).
A `write` that fails mid-protocol (`SheetUploadRun._write()`) prints a clean
message and stops the run rather than raising, and a rate-limited row
(`is_rate_limit_error()`) stops the run after finishing the current chunk's
confirm write, so nothing already uploaded is left reserved-but-unconfirmed —
see "Chunking" below. A stop request (the first Ctrl-C; see
`decisions/QUOTA-AND-RUNS.md`, "An interrupt stops a run after the current
item") ends the run the same way: it is checked before each item and before
each chunk's reserve write, so the item in flight finishes and is confirmed
and no further chunk is reserved.

A row is chosen for this run based on its own two tool-owned columns
(`classify_row()` → `RowState.UNASSIGNED`/`RESERVED`/`DONE`): blank
`ia_identifier` means mint-and-reserve, a set `ia_identifier` with blank
`ia_uploaded` means retry under the existing identifier (crash recovery,
never re-mint), both set means skip entirely.

### One run at a time

The protocol assumes a run owns the rows it reserved until it confirms them.
A second run would read those rows as RESERVED and retry them under the same
identifiers mid-upload, so `cmd_upload` holds `upload_lock`'s OS lock for the
whole run and refuses to start while another run holds it (`--dry-run`
excepted). See `decisions/QUOTA-AND-RUNS.md`, "One upload runs at a time,
enforced by `upload`".

## Chunking
`upload` processes targets in batches of 500 (IA's per-run batch
limit) by default, via `chunk_rows()`. This is a real checkpoint boundary:
each chunk gets its own re-read of the Sheet, reserve write, uploads and
confirm write. It is not one `ia upload --spreadsheet` call per chunk — each
row is uploaded individually through the `internetarchive` Python library so
outcomes are captured per-row. `upload --chunk-size N` overrides the batch
size for that run (`SheetUploadRun.chunk_size`, threaded into `chunk_rows()`).

A run may not exceed `DAILY_ITEM_CAP` (5,000, IA's per-account daily
limit). It refuses rather than silently capping and names the fix,
`--limit`, with `--allow-over-daily-cap` as the explicit override.

`upload --limit N` caps how many *planned* upload targets
(valid, ready, not already done — `plan_upload_targets()`'s own output) a
single invocation processes at all, applied before chunking: `--limit 10
--chunk-size 3` means 10 items total, in batches of 3. It counts targets
in scope, never raw Sheet rows scanned, and it and `--chunk-size` are both
recorded in the run's `run_header` log record (below) so a capped or
rechunked run stays reconstructable later. See `DECISIONS.md`, "`--limit`
counts planned targets...".

If an upload fails with what `is_rate_limit_error()` recognizes as
Internet Archive's rate limit, `SheetUploadRun.execute()` stops the whole
run after confirming whatever it already uploaded in the current chunk (and
any earlier chunk), rather than treating it as one more per-row failure and
continuing. The stop names the parsed status but not a cause, because a 503
does not say which of IA's limits fired. The detector has fired on one real
response so far (a queue throttle, 2026-09-24) — see `DECISIONS.md`, "Still
open". A stop request (the first Ctrl-C; see `decisions/QUOTA-AND-RUNS.md`,
"An interrupt stops a run after the current item") ends the run the same
way: it is checked before each item and before each chunk's reserve write,
so the item in flight finishes and is confirmed and no further chunk is
reserved.

## Progress output
`upload`/`sync-metadata` print a `[position/total] ...` line to stdout
before each row, plus a `X uploaded successfully, Y error(s)` summary line
before the final `log written to <path>` line, so a run is never silently
quiet. `upload_row` also passes `verbose=True` through to
`internetarchive.upload()`, which prints its own `tqdm` byte-progress bar
per file — that's IA's own upload status, not something this tool
fabricates. It also passes `checksum=True`, so re-running `upload` over
rows whose files haven't changed skips re-uploading (and re-triggering
IA's `derive` task) for anything already present with a matching MD5.

## Correcting an uploaded item

`sync-metadata` reads the Sheet live like `validate` and `upload`. Its scope
is every row `classify_row()` calls DONE, and its target for each is the
identifier in that row's own `ia_url` cell — which `upload`'s confirm write
put there, complete with the per-run `zztest-` stamp in test mode. Nothing is
re-derived, so a Sheet whose rows were uploaded by different runs under
different stamps is handled without the command knowing that happened.

The fields sent come from `sheet_metadata_fields()`, shared with `upload`, so
a column that uploads but does not sync cannot exist. Blank cells are dropped
by `update_metadata_row()` — blank means "leave this field alone", and
`REMOVE_TAG` deletes: the `internetarchive` library (like the `ia` CLI's
`--modify field:REMOVE_TAG`) treats that exact string as a delete sentinel.

A DONE row is sent only when its content changed since its last successful
push. `plan_sync_targets()` hashes each row's `metadata_to_send()` output
(`sync_hash()`) and compares it against `ia_sync_hash`; `split_unchanged()`
sorts the result into `to_push`/`already_synced`. Only `to_push` is sent —
this reverses the original "every DONE row is sent every run", which was
correct for a hand-run command over a few hundred rows and stopped holding at
~4,000 rows on an hourly schedule (issue #27; the hash gate itself is issue
#24). That schedule is now a LaunchAgent that
`./install.sh --project <project> --live --enable-agent` installs — see
[`DEPLOYMENT.md`](DEPLOYMENT.md#12-enabling-the-hourly-sync); its stdout and
stderr both go to `logs/launchagent-<project>.log`, each run dated by the
line `main()` prints before loading anything (see
[`QUOTA-AND-RUNS.md`](decisions/QUOTA-AND-RUNS.md#the-agents-output-is-one-dated-file-never-rotated)
for what stays undated), and `doctor`'s
`launch agent loaded` check reports its last exit. See
[`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#a-row-pushes-only-when-its-content-changed).
IA's *no changes to `_meta.xml`* response still becomes `MetadataUnchanged`
and is counted as `unchanged` rather than a failure, and — unlike a genuine
failure — it stamps `ia_sync_hash`/`ia_last_synced` just like a real change
would, since the item now provably matches the Sheet. `sync-metadata` refuses
to run at all without both columns present as Sheet headers, in every mode —
see "Tool-owned columns" above.

## Reconciling filenames

`reconcile-files` reads the Sheet live like `validate`/`upload`, but
neither uploads nor changes metadata. `survey_files()` resolves every row
against `files_dir` the same way `resolve_sheet_files()` does, and returns
a `FileSurvey` splitting the result into `claimed` (every disk file some
row currently resolves to) and `unclaimed` (every file matching the
project's `photo_extensions`, in a row's own folder, that no row claims;
both keyed through `claim_key()`, so two rows spelling one folder
differently — `SOP CD 1` and `sop cd 1`, one folder on a case-insensitive
filesystem — cannot end up in two disjoint namespaces with the duplicate
check missing between them) —
plus `unresolved`/`wanted`, the row numbers that failed and what each
one's filename cell said. A row whose `file_template` cells are blank is
counted in `not_ready` and appears in none of the other three: it asserted
no file, so it is not-ready rather than broken (the same split
`resolve_sheet_files()` draws between `errors` and `blank`), and
`cmd_reconcile_files` reports the count in one line rather than raising a
prompt with no proposal and no candidates for each of the ~2,900
uncatalogued rows. Unlike `resolve_sheet_files()`, it never mutates
the rows it's given: reconciliation shows a row's own cells to a human
before any decision is made, so they have to still read exactly as
written.

`propose_match()` (`reconcile.py`) looks for a single best match for an
unresolved row among that row's `unclaimed` candidates — see
[`DECISIONS.md`](decisions/RECONCILIATION.md) for the two-pass matching
strategy and its guards. `cmd_reconcile_files` prompts about each
proposal via `prompt_for_decision()` and, on acceptance, batches a
`CellUpdate` for that row's filename column alone, flushing every
`RECONCILE_FLUSH_EVERY` (25) accepted rows so a long interactive session
doesn't hold hundreds of accepted corrections in memory, unsaved, until
the very end.

**Accepted corrections are re-checked before they are written.**
`flush()` re-reads the grid and drops any pending update whose row no
longer holds the filename it was matched against, saying so on screen. This
is the same hazard `upload`'s `sheet_row_fingerprints()` guard exists for,
one cell wide: `reconcile-files` has the longest read-to-write window in
the tool — an interactive session over a shared Sheet — so a row inserted
or deleted mid-session would otherwise shift every later write by one. A
re-read that fails stops the run rather than writing unverified.
Reconciliation is also the **only** command that writes a `file_template`
column, which `sheet_row_fingerprints()` otherwise relies on being
never-written; the two never run together, and a correction landing during
an `upload` makes that row fingerprint as moved and be skipped, which is
the safe direction.

**The candidate pool shrinks as the run progresses.** `claimed` is a
snapshot built once, before any row is decided, but the candidates offered
to each row are re-filtered against it on *every* iteration of the loop —
not computed once before the loop starts, and `unclaimed` itself is never
recomputed. Accepting a file for one row adds it to `claimed` immediately,
so a later row in the same folder never sees it as a candidate again.
Without that per-iteration filter, two misspelled rows in one folder that
both happen to be within matching distance of the same single file on
disk could each be proposed it — and each accepted onto it — pointing two
Sheet rows at one photograph. A review caught exactly this: the original
implementation filtered `unclaimed` against `claimed` only once, before
the loop started.

`log_decision()` writes reconciliation's own JSONL shape — one line per
row *considered*, not per row acted on:
`{row, folder, wanted, status, chosen, proposed, matches, reason,
timestamp}`, with `status` one of
`accepted`/`typed`/`rejected`/`no_candidate`/`ambiguous`/`stopped`. Every
key is on every line, empty where it does not apply. `chosen` is what was
written and so is empty on every path but an acceptance; `proposed` is what
the tool put forward, which is the only record of *what* a rejection turned
down; `matches` names the files an `ambiguous` row could not be chosen
between. `accepted` versus `typed` comes from how the operator answered
(`[y]` versus `[e]`), not from comparing the two strings — a name typed at
`[e]` is still typed when it happens to equal the proposal.
This is a different record than `upload`/`sync-metadata`'s per-row-result
log below, because it answers a different question later: not "did this
identifier upload", but "what did a human decide about this row, and
why". `--dry-run` opens no log at all — nothing here was decided, only
printed.

## Appending skeleton rows

`append-rows` is reconciliation's other half: once every row that names a
file resolves, files still unclaimed are genuinely uncatalogued, and
`cmd_append_rows` appends one row per such file — values in the
`file_template` columns only, placed by the real header's column indexes
and padded to its width, every other cell blank for the cataloguer.

It does **not** reuse `survey_files()`'s `unclaimed` map, and the
difference is the point: that map only lists folders some catalogued row
already names (reconcile can only prompt about rows that exist), while
`scan_unclaimed_files()` walks every subdirectory of `files_dir` — a
brand-new donor folder with zero rows is exactly what append exists to
pick up. Both sides key through `claim_key()`. Photo files at the top of
`files_dir`, outside any folder, are reported rather than silently
skipped: a folder/name template cannot express a row for them.

Two gates are stricter than reconcile's. Any unresolved row is fatal (see
[`DECISIONS.md`](decisions/RECONCILIATION.md), "Reconciliation ships
before append") — a typo'd row and a missing row both present as an
unclaimed file, so appending past one duplicates a photograph's row. And a
structurally shifted data row, which reconcile merely skips, is fatal
here: reconcile's operator approves rows one at a time, but append trusts
the whole survey at once, and a misread row can make the file it really
means look unclaimed.

The write is `SheetClient.append_rows()` — a single `values.append` call
with `RAW` (a filename starting with `=` must land as text, the same
reason `write_cells` uses it) and `INSERT_ROWS` (add rows, never overwrite
whatever sits below the table). The API finds the end of the data itself,
so no row index is computed — or raced over — on this side; there is no
moved-row window to guard the way reconcile's `flush()` must. Idempotence
comes from the drive and the Sheet, not from any state the command keeps:
appended rows resolve on the next survey, so their files are claimed and a
rerun over an unchanged drive appends nothing. The run log
(`append-rows-<timestamp>.jsonl`, `{folder, name, status, timestamp}`) is
written only after the append call succeeds — it records what happened,
not what was attempted.

## Logging and resume
Every `upload`/`sync-metadata` run writes a timestamped JSONL log to
`logs/<command>-<timestamp>.jsonl`, one line per row:
`{identifier, file, status, error, uploaded_as, live, timestamp}`.
Every `timestamp` this tool records is ISO-8601 UTC with an explicit `Z`
(`utc_timestamp()`), as is the `ia_uploaded` cell written back to the Sheet.
Local time repeats an hour during the DST fall-back transition, so a run
spanning it would stamp a later chunk with an earlier wall-clock time — the
same reason `run_stamp()` uses UTC.

`upload`'s `log_run_header()` writes one more record as the log's
**first** line, before any row result: `{record: "run_header", timestamp,
project, live, dry_run, sheet_id, collection, files_dir, file_template,
columns, held_back, required_for_upload, limit, chunk_size, batch,
batch_column}`. `columns` and `held_back` come from that run's `ColumnMap`
(every header the Sheet had, and which were excluded as `(LCPS Internal)`);
`required_for_upload`, `limit`, `chunk_size` and `batch` are the
readiness/scope/batching rules in effect that run. `sheet_id` and
`collection` name what the run actually used, not what the registry
configures — a test run records `test_collection` and the test Sheet ID,
because a receipt naming the real, permanent collection for items that went
somewhere else describes a run that never happened. `batch_column` is
written even when `batch` is null, since the value alone means nothing
without the column it was matched against. All of these can change between runs even though none of them
changes per row within one, which is why they are captured once here
rather than left to be reconstructed later from a Sheet that has since
moved on.

### The `run_summary` record
Every real run of `sync-metadata` **or** `upload` ends with one more record
as the log's **last** line. It exists so a scheduled, unattended run
produces something a program can read without parsing console output or
replaying every row line above it, and so the Sheet's log tabs have
something to mirror.

Both commands write `{record: "run_summary", timestamp, live, …}`; the
fields after `live` are each command's own, because the two runs do
different things and a shared vocabulary would have to lie about one of
them. An upload has no `unchanged` — it either created the item or did
not — and a sync has no `unconfirmed`, since it writes nothing to the
Sheet that could fail to land.

#### `sync-metadata`

`{… checked, pushed, changed, unchanged, already_synced, failures,
skipped}`. The counts mean:

| field | meaning |
| --- | --- |
| `checked` | rows the run evaluated — every row it read. `checked − pushed − len(skipped) − already_synced` is the rows not marked uploaded. |
| `pushed` | rows actually sent to Internet Archive. Always `changed + unchanged + len(failures)`. |
| `changed` | sends IA accepted as a change. |
| `unchanged` | IA's *no changes to `_meta.xml`* — the idempotence signal a full re-sync is run to see, kept as its own count rather than folded into `changed`. |
| `already_synced` | rows the hash gate found already matching their last push and never sent at all. On the steady state this is nearly the whole Sheet; see `DECISIONS.md`, "A row pushes only when its content changed". |
| `failures` | `{identifier, error}` per row IA refused. |
| `skipped` | `{identifier, error}` per row the run declined to send at all. |

#### `upload`

`{… attempted, succeeded, failures, unconfirmed, not_attempted,
rate_limited, stopped_by_request, rate_limit_status, skipped}`. The counts mean:

| field | meaning |
| --- | --- |
| `attempted` | rows actually sent to IA this run, refused or not. Derived: always `succeeded + len(failures)`. |
| `succeeded` | files that reached Internet Archive. |
| `failures` | `{identifier, error}` per row IA refused. Nothing was created and the identifier is still free. |
| `unconfirmed` | `{identifier, error}` per row that IS on Internet Archive but was never marked in the Sheet. |
| `not_attempted` | rows the run stopped short of. See the overlap note below. |
| `rate_limited` | `true` when IA said *slow down* and the run stopped early rather than finishing. Derived: `rate_limit_status` is not `null`. |
| `stopped_by_request` | `true` when an interrupt (Ctrl-C, or the upload page's Stop once it exists) ended the run between items. The rows it never reached count under `not_attempted`. Never `true` together with `rate_limited`. |
| `rate_limit_status` | the parsed status (`429` or `503`) the run stopped on, else `null`. It does not say which of IA's limits fired. |
| `skipped` | `{identifier, error}` per row nothing was sent for — held back by validation, or moved in the Sheet mid-run. |

`unconfirmed` is the one to read first. A refused send is recoverable by
rerunning; an unconfirmed row is a photograph that exists on Internet
Archive under a permanent identifier the Sheet does not know about, so the
next run reads the row as un-uploaded and would upload it *again* under a
second identifier. Keeping it out of `failures` is the whole reason the
list is separate.

`not_attempted` is the only number here that **overlaps** the lists rather
than partitioning against them: it is the console's own "the run stopped
early" figure, which counts a row this run declined to touch whether or not
that row also appears under `skipped`. The counts are not a partition and
must not be summed — `attempted` is the only derived total.

`already_synced` is its own count rather than folded into `checked` or
`skipped`: unlike `skipped`, nothing was wrong with these rows — the hash
gate is why `sync-metadata` can run hourly at all, and collapsing "quietly
correct" into either of the other two would make a healthy run's summary
look identical to an unhealthy one.

`failures` and `skipped` are separate lists on purpose: a failure means the
item was contacted and the edit refused; a skip means nothing was sent.
Months later that is the difference between "this item may not be in the
state I intended" and "this item was not touched", which one flat list
destroys.

No count is stored beside the list it counts — `failed` and `pushed` are
derived properties of `SyncSummary`, `failed` and `attempted` of
`UploadSummary` — and `sync_summary_lines()` / `upload_summary_lines()`
render the console's closing lines from that same object, so the number a
person reads and the number a program reads cannot drift apart. Upload's
half of this arrived late: its closing counts lived in a local dict that
console output alone consumed, which is precisely the drift the pairing
exists to prevent.

The summary is written by `try_log_run_summary()`, which reports a write
failure on stderr instead of raising. It is a record *of* the run, not a
step *in* it, and by the time it is written permanent metadata has already
changed — reporting a successful sync as failed would invite a rerun.

The logs are audit records. Nothing in the tool reads them back: since
2026-09-23, when the CSV paths and their `--resume-from`/`--from-log` flags
were removed, the Sheet's `ia_uploaded` and `ia_url` cells are the only
record a run consults. `log_result()` appends per row with no atomic write,
so a run killed mid-write can leave a truncated final line; read a log with
that in mind.

**`dry_run` is always `False` in a real log.** `upload_from_sheet` returns
on the `if dry_run:` branch (nothing uploaded, nothing logged) before
`open_log()`/`log_run_header()` are ever reached, so no log a real run
produces can show `dry_run: true` — that value is real and exercised by
`log_run_header()`'s own unit tests calling it directly with `dry_run=True`,
but it is not something to expect varying in `logs/*.jsonl`.

`identifier` is always the real, permanent identifier. `uploaded_as` is the
identifier actually sent to IA for that row (see "Safety rail" below), so
you can see exactly what landed on the site. `live` records which mode
(test vs. `--live`) produced that row's result. `http_status` is the status
`parsed_status_code()` found on a `failure` (never read from the message), and
`null` on every other record or when the failure carried none.

A rerun resumes by itself: `ia_uploaded` is the record of what is done, so
done rows are skipped and a reserved row is retried under its existing
identifier.

## The Sheet's log tabs
The same `run_summary` record is also mirrored into the spreadsheet, so a
run can be diagnosed months later by anyone who can open the Sheet — the
JSONL lives on whichever machine ran the job. `upload` writes
`upload_log_tab`, `sync-metadata` writes `sync_log_tab`; both are optional
registry keys, and a command whose key is absent writes no tab at all.

`log_tab.py` renders a record as rows — one `summary` row, then one row per
entry in the record's `failures`, `unconfirmed` and `skipped` lists — and
appends them through `mirror_run()`. It is fed the record rather than the
summary object, which keeps it free of any import from `ia_bulk` (which
imports it) and, more usefully, makes the tab and the JSONL the same data
rendered twice: `try_log_run_summary()` returns the record it wrote, and
that same dict is what reaches the Sheet, so the two cannot differ even by a
timestamp.

`mirror_run_to_log_tab()` in `ia_bulk.py` is the single call site for both
commands. It reuses the run's own connection one tab over
(`SheetClient.append_only_tab()`) rather than authenticating again, and what
it hands the writer is an `AppendOnlyTab` — a type carrying `ensure_tab` and
`append_rows` and no `write_cells` at all, so the mirror cannot reach the
metadata columns even by mistake. `mirror_run()` catches everything and reports on
stderr — by the time it runs, items exist on Internet Archive under
permanent identifiers, and a telemetry failure reported as a failed run
would invite the rerun that mints a second identifier.

A sync run that pushed nothing and found nothing wrong is not mirrored
(`sync_run_is_worth_mirroring()`); see `DECISIONS.md`, "The Sheet's log tabs
are telemetry, never an input", for that and the rest of the reasoning.

## `sync-metadata`'s "unchanged" status
IA's metadata-update endpoint returns an HTTP 400 with
`{"error": "no changes to _meta.xml"}` when every field in the request
already matches what's on the item — i.e. nothing was wrong, there was
just nothing to do. `update_metadata_row` detects that specific error and
raises `MetadataUnchanged` instead of `RuntimeError`; `SheetSyncRun.execute`
catches it separately, logs the row as `"status": "unchanged"` (not `"failure"`),
counts it in `PushOutcome`'s `unchanged` (which `SyncSummary` reports), and
stamps `ia_sync_hash` as for a real change — subject to the same late
identity check (`_verified`) — so a row that's already correct doesn't
inflate the error count or flip the exit code.

## Safety rail
Default target is `test_collection`; `--live` is required to target the
real collection and use the real identifier as-is. When not `--live`,
`effective_identifier()` prepends `zztest-<run's stamp>-` to the real
identifier for every network call (e.g.
`zztest-20260819t144907-lcps-sarasoldphotos-00001`) — this happens
automatically, in code, rather than requiring the Sheet to already
contain test-prefixed identifiers. The Sheet's
`ia_identifier` column never needs to change between a test run and a
`--live` run.

The stamp (`run_stamp()`) is computed once per invocation and shared by
every row that run touches, so a rehearsal's items group together and never
collide with a previous rehearsal's — see
[`docs/DECISIONS.md`](decisions/IDENTIFIERS.md#test-identifiers-carry-a-per-run-stamp)
for why a bare `zztest-` prefix made every fresh-Sheet rehearsal collide
with the last one. (A *resumed* run is its own invocation with its own
stamp, so its items land under a second stamp, not the original run's.)
`--live` identifiers never carry a stamp: they are the permanent, public
ones and must stay a pure function of the Sheet.

## Known gaps
Verified defects with reproductions live in
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md). The design-level gaps are below.

Nothing in the tool pins `projects_registry.json`'s `collection_key`. The
value itself is settled (see
[The collection key is `lcps`](decisions/IDENTIFIERS.md#the-collection-key-is-lcps),
which also lists what a change would break), but a change to it is not
detected.

The target IA collection is the project's `ia_collection` in
`projects_registry.json`, and only there: there is no collection flag. (The
old `--collection` flag defaulted to `"lcps"`, which is not a real Internet
Archive collection; it was removed with the CSV paths on 2026-09-23.)
Nothing still validates `ia_collection` against IA itself at
runtime, so confirm it by hand once, in version control, before the first
`--live` run — `upload --dry-run` prints everything the run would do
without doing any of it. See `DECISIONS.md`, "Technical configuration lives
in the registry". (`ia_collection` for this project was confirmed by hand
against archive.org on 2026-08-22 — see `DECISIONS.md`, "Still open" — but
the tool itself still does not check this automatically, and a second
project's registry entry would need the same manual confirmation.)

`validate` cannot tell whether a well-formed header is *semantically* right:
a misspelled header ships as a misspelled IA field on every item. The header
row is proofread by hand instead (`OPERATIONS.md` §1).

Repeated IA fields (`subject[0]`, `subject[1]`) cannot be written from the
Sheet — see `KNOWN-ISSUES.md` §6.
