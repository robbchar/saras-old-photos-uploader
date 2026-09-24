# IA Bulk Upload CLI

A small Python CLI for validating, uploading, and syncing metadata for
Internet Archive items in bulk, reading a project's Google Sheet directly
and live over the Sheets API. Built for the Lower Columbia Preservation
Society's (LCPS) Astoria historical photo archive, but kept generic to "a
project" so a second LCPS project can reuse the same pipeline.

## Docs

- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — runbook: how to run a batch,
  pre-live checklist, resuming, batch limits. **Start here to run something.**
- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — provisioning and upgrading the
  Mac that runs this: Python, credentials, the hourly sync agent, `./install.sh`.
- [`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md) — verified defects and gaps,
  with reproductions.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full design: Sheet column
  mapping, identifier scheme, chunking, logging, safety rail.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — why it's built this way, including
  designs that were tried and reversed.

## Identifier scheme
`COLLECTIONKEY-PROJECTID-NUMBER` — all lowercase, hyphen-separated. The
registry refuses a collection key or project id that isn't lowercase letters
and digits only (see [`docs/decisions/IDENTIFIERS.md`](docs/decisions/IDENTIFIERS.md#registry-ids-must-be-lowercase-letters-and-digits)).
- COLLECTIONKEY: `lcps`, settled — see
  [The collection key is `lcps`](docs/decisions/IDENTIFIERS.md#the-collection-key-is-lcps).
  Not the IA collection items land in (`ia_collection`).
- PROJECTID: short project code, e.g. `photosexample` (illustrative only —
  see `projects_registry.json` for the actual registered codes; tracked in
  a small project registry, not invented ad hoc per script run)
- NUMBER: 5-digit zero-padded sequential number, unique per project
Identifiers are permanent once uploaded — never reused, never renamed.
Original filenames/donor folder structure are NOT part of the identifier;
they go in the `identifier-bib` metadata field instead.

## Project registry

Everything technical about a project — the target IA collection, where its
files live, how a row's file path is built, and which fields a human must
fill in before a row can upload — lives in `projects_registry.json`, never on
the command line. See [`docs/DECISIONS.md`](docs/DECISIONS.md), "Technical
configuration lives in the registry, not the command line".

```json
{
  "collection_key": "lcps",
  "projects": {
    "photosexample": {
      "mediatype": "image",
      "ia_collection": "lcpsdigitalcollection",
      "sheet_id": "...",
      "test_sheet_id": "...",
      "sheet_tab": "TestSheet",
      "upload_log_tab": "Upload Log",
      "sync_log_tab": "Sync Log",
      "files_dir": "./data",
      "file_template": "{folder_on_lacie_drive}/{file_name}",
      "batch_column": "theme",
      "required_for_upload": ["title", "theme"]
    }
  }
}
```

`upload_log_tab` and `sync_log_tab` are optional and name the tabs each
command mirrors its run summary into — one row per run, plus one row per
problem, in the same spreadsheet the run is already reading. They exist so a
problem can be diagnosed months later from a Sheet anyone can open, without
reaching the JSONL logs on the machine that ran it. Telemetry only: nothing
in a log tab is ever read back, a name colliding with `sheet_tab` is refused
at startup, and a failed mirror write is reported without failing the run.
Leave a key out and that command writes no tab at all. See
[`docs/DECISIONS.md`](docs/DECISIONS.md), "The Sheet's log tabs are telemetry,
never an input".

`batch_column` is optional and names the normalized column `--batch` matches
against — the column that says which theme, donation or sitting a row belongs
to. It has no default: a project without one simply cannot be scoped by
batch, and `--batch` on such a project is refused rather than quietly
uploading everything. See [`docs/DECISIONS.md`](docs/DECISIONS.md), "A run is
scoped to a batch by value; the column is registry configuration".

`required_for_upload` names the normalized columns (not raw Sheet header
text) a human must fill in before a row is ready to upload — a blank one
marks the row not-ready rather than invalid, and a typo in this list is a
hard startup error rather than a silent no-op. See
[`docs/DECISIONS.md`](docs/DECISIONS.md), "A blank cell is not an error".

## Setup

On the Mac that runs the pipeline, one command creates `.venv`, installs
`requirements.txt` into it, and ends by running `setup` (below) — see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md), "Install":

```bash
./install.sh --project sarasoldphotos
```

On a development machine, install the dependencies into whatever environment
you use instead:

```bash
pip install -r requirements.txt
```

Requires `internetarchive` to be authenticated against the shared org
account (`ia configure`) before running `upload` or `sync-metadata`. Sheet
commands also need the Google service account key saved at
`.ignored/google-service-account.json` — see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md), "Service account".

## Commands

### `validate` — check a project's Sheet, no writes

`--project` is required (it looks up the project's block in
`projects_registry.json`). `validate` reads the project's Sheet live over the
Google Sheets API (its test Sheet, unless `--live` is passed).

```bash
# read the project's Sheet
python ia_bulk.py validate --project sarasoldphotos

# report on one batch only, the same scope `upload --batch` would run
python ia_bulk.py validate --project sarasoldphotos --batch "Logging"
```

`--batch` narrows the report to the rows whose registry-configured
`batch_column` holds that value, through the same code `upload --batch` uses —
so the preview is the run. See `upload` below for the flag in full.

For the Google Cloud setup this requires (the service account, its key file,
and sharing the Sheet with it) see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md), sections "Service account" and
"Sharing the Sheet"; for
the reserve/upload/confirm protocol and registry fields in full see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and "Project registry" above.

It checks the header for colliding or empty field names and any data
row longer than the header, injects `mediatype` from the registry, resolves
each row's file against `files_dir` using `file_template` (three passes: an
exact filename match, then a case-insensitive match against the full name, then
the same ignoring a trailing extension — two matching candidates is a failure
naming both, never a silent pick; `files_dir` is a hard boundary, so a cell that
resolves outside it is refused, as is a blank folder cell), then prints a
pass/fail report per row, a receipt of which fields will upload, a lifecycle
summary (rows ready to upload / already uploaded / reserved but unconfirmed),
and advisory suggestions for renaming a column to a standard IA field name.
Every column the tool itself writes is `ia_`-prefixed (`ia_identifier`,
`ia_identifier_bib`, `ia_uploaded`, `ia_url`); a blank `ia_identifier` is
normal for a new row, not an error — it's assigned by `upload` later. A set
`ia_identifier` must be unique in the Sheet and match the
`COLLECTIONKEY-PROJECTID-NUMBER` scheme (lowercase, 5-digit zero-padded
NUMBER), with the prefix registered in `projects_registry.json` **and its
`PROJECTID` equal to the run's own `--project`** — another registered
project's identifier is refused, not accepted (issue #2). A `--project` that
isn't in the registry stops the run before any row is reported. The Sheet's
own `identifier` column (if it has one) is ordinary donor metadata, untouched
by this tool. `date` is optional: `upload` fills a blank `date` with `[n.d.]`
rather than omitting it.

Header problems are reported as row 1. `validate` exits non-zero if anything
fails. Always run it before `upload`.

### `upload` — mint identifiers, upload, record the result

Reads the project's Sheet live.

```bash
# rehearse against the test Sheet: uploads as zztest-…, writes nothing back
python ia_bulk.py upload --project sarasoldphotos

# same, but record the minted identifiers in the TEST Sheet
python ia_bulk.py upload --project sarasoldphotos --write-identifier

# see what it would do without doing any of it
python ia_bulk.py upload --project sarasoldphotos --dry-run

# upload one theme only, 100 of them
python ia_bulk.py upload --project sarasoldphotos --batch "Logging" --limit 100
```

`--batch` scopes the run to the rows whose `batch_column` (from the registry)
holds that value; only the value goes on the command line. Matching ignores
case and surrounding whitespace, and a blank cell never matches. The scope is
applied before anything is counted, so `--limit` means "this many **of the
batch**", and the not-yet-catalogued count covers the batch alone. A value no
row carries is refused with the values actually present listed, rather than
run as an empty upload — `validate --batch "…"` previews the same scope
through the same code.

| Mode | Reads | Uploads as | Writes back |
|---|---|---|---|
| default | test Sheet | `zztest-…` → `test_collection` | nothing |
| `--write-identifier` | test Sheet | `zztest-…` → `test_collection` | test Sheet |
| `--live` | real Sheet | real identifier → registry's `ia_collection` | real Sheet, always |
| `--dry-run` | either | nothing | nothing — prints the intended writes |

Per chunk of 500 rows `upload` does four things **in this order**:

1. **reserve** — one batch write putting the minted `ia_identifier` in the Sheet
2. **upload** — row by row, to Internet Archive, under that identifier
3. **confirm** — one batch write of `ia_uploaded`, `ia_url` and
   `ia_identifier_bib`, but only for rows whose upload succeeded
4. **log** — per-row outcomes appended to the JSONL log

Reserving first is deliberate. Uploading first would let a crash strand an
item on Internet Archive that the Sheet has no record of, and the next run's
`max+1` would then mint that same number onto a different photograph —
permanently. Reserving first degrades the same crash into an unused gap in the
sequence, which is harmless. Batching matters too: the Sheets API allows 60
writes per minute per user and counts a batch as one request, so ~10,000 rows
cost about 40 requests rather than 10,000.

A row is chosen by what its two tool-owned columns already hold:

| `ia_identifier` | `ia_uploaded` | Action |
|---|---|---|
| blank | blank | mint, reserve, upload |
| set | blank | upload under the **existing** identifier, never re-mint |
| set | set | skip entirely |

The Sheet must already have all four `ia_` columns as headers
(`ia_identifier`, `ia_uploaded`, `ia_url`, `ia_identifier_bib`); `upload`
refuses in every mode until they exist, so a rehearsal never succeeds where
the real run would fail.

**Editing the Sheet while a run is in progress.** Row numbers are positional,
so inserting or deleting a row shifts every row below it — and a run holds row
numbers from the read it started with, which on a full-collection run is hours
before the last chunk writes. Before **every** write, reserve and confirm
alike, the run re-reads the Sheet and checks two things about each target row:
that its `file_template` columns still describe the same photograph (a
fingerprint this tool never writes, so the check cannot pass by verifying its
own earlier write), and that `ia_identifier` is blank or already ours. A
mismatch — including a column inserted or removed — is reported and the write
is withheld, rather than landing on a different photograph. Nothing is lost:
rerun once the Sheet has settled and every unrecorded row is picked up.

If the Sheet becomes unreadable mid-run, or one of the four `ia_` columns is
renamed or deleted, the run stops cleanly with the reason on stderr, still
prints its summary and log path, and exits non-zero.

Two rows resolving to the same file would defeat that fingerprint — identical
`file_template` cells are identical fingerprints — so they are refused
outright: `validate` and `upload` flag both rows of such a pair as errors
(two rows cannot claim one photograph; that would mint it two permanent
identifiers) — and if one of them has already uploaded, it is named as the
row to keep rather than offered for deletion. If a duplicate appears before
a run reserves its numbers, the guard treats the now ambiguous fingerprint
as unable to prove anything and skips the row like any other moved one; once
a row holds the run's own number, that number is the proof instead, so a
duplicate appearing later cannot withhold the write that records the upload.
See
[`SHEET-PROTOCOL.md`](docs/decisions/SHEET-PROTOCOL.md#a-fingerprint-only-proves-identity-while-it-is-unique).

That said, the check is a safety net, not a licence. So: **avoid editing the
Sheet while a run is in progress.**

**A failing row is skipped, not fatal.** One unresolvable file in row 9,000
does not block the other 9,999; the failures are listed with their errors, the
valid rows upload, and the command exits non-zero so a partial run is never
mistaken for a clean one.

**A rate limit stops the run, not just one row.** If an
upload fails with what looks like Internet Archive's rate limit, the run
stops rather than grinding through the rest of the batch as unexplained
failures — everything already uploaded that run, in this chunk or an earlier
one, is still confirmed in the Sheet first. This detector is best-effort: no
`--live` run has ever happened, so no real rate-limit response has ever been
captured, and it may not fire on one — see `docs/DECISIONS.md`, "Still open".
`--limit` (below) is the operator-controlled fallback either way.

**`--limit` and `--chunk-size`.**

```bash
# upload at most 100 items this run, in the default batches of 500
python ia_bulk.py upload --project sarasoldphotos --write-identifier --limit 100

# 10 items total, in batches of 3 - not 10 batches of 3
python ia_bulk.py upload --project sarasoldphotos --write-identifier --limit 10 --chunk-size 3
```

`--limit` counts *planned* upload targets — rows that are valid, ready
(nothing required left blank), and not already done — never raw Sheet rows
scanned. On a Sheet with 2,900 uncatalogued rows and 150 ready ones,
`--limit 100` uploads 100 of the 150 ready rows, not the first 100 rows
read. `--chunk-size` overrides the reserve/upload/confirm batch size
(default 500, Internet Archive's per-run cap) for the rows `--limit` leaves;
the two combine literally, as shown above. Both are recorded in the
`run_header` record every upload log starts with (see
`docs/ARCHITECTURE.md`).

**The 5,000/day cap is enforced.** Internet Archive allows 5,000 items per
account per day. A run planning more than that is refused before anything is
uploaded, naming the fix: `--limit 5000`. It refuses rather than silently capping, because a run that
quietly stopped short would read as a complete one. `--allow-over-daily-cap`
overrides it, and is only correct if you know IA has raised this account's
cap. The refusal applies in test mode too: a rehearsal uploads through the
same account and spends the same quota.

Other behavior:

- Processes rows in chunks of 500 by default (Internet
  Archive's per-run batch limit), overridable via `--chunk-size`
- Uploads each row via the `internetarchive` Python library (not the `ia`
  CLI), so per-row success/failure is captured directly
- Retries a row through transient network failures — three attempts, backing
  off about 2s then 4s, printing a line each time. A refusal (`Access Denied`,
  a rejected field, any 4xx) is not retried, and a rate limit still stops the
  run instead. A `Retry-After` header is honoured up to 30s, never longer. See
  [`docs/DECISIONS.md`](docs/decisions/QUOTA-AND-RUNS.md#retry-covers-transport-failures-never-refusals)
- Writes a timestamped JSONL log to `logs/upload-<timestamp>.jsonl`, one
  line per row: `{identifier, file, status, error, uploaded_as, live, timestamp}`.
  Timestamps are ISO-8601 UTC with an explicit `Z`, as is the `ia_uploaded`
  cell written back to the Sheet. The log is an audit record; the tool never
  reads it back
- The collection and the files directory come from the project's registry
  entry, never from a flag
- A rerun resumes by itself: `ia_uploaded` is the record of what is done

### `sync-metadata` — update metadata on already-uploaded items

The Sheet is the correction. Fix a description in the Sheet, run this, and it
is on the site:

```bash
python ia_bulk.py sync-metadata --project sarasoldphotos --dry-run
python ia_bulk.py sync-metadata --project sarasoldphotos
```

It reads the Sheet live, takes every row marked uploaded (`ia_uploaded` set),
and sends that row's current metadata to the item named in its `ia_url` cell.
Nothing has to be re-derived: `ia_url` is what `upload`'s confirm write
recorded, so it already names the exact item — including the per-run
`zztest-` stamp in test mode, even when different rows were uploaded by
different runs.

The fields sent are the same ones `upload` sends (`sheet_metadata_fields()`),
so the two commands cannot disagree about what a row means. Tool-owned `ia_`
columns, `(LCPS Internal)` columns, and the generated `mediatype`/
`collection` are all excluded — Internet Archive will not change an item's
mediatype after upload anyway.

**Only a row whose content actually changed is sent.** The Sheet must already
carry two more tool-owned columns beyond `upload`'s four —
`ia_sync_hash` and `ia_last_synced` — and `sync-metadata` refuses to run
without any of the six, in test mode as well as live: without `upload`'s
own four it cannot confirm which item a row's stamp belongs to, and without
the two hash columns it has nowhere to record what it last pushed. Each
successfully-pushed row is
stamped with a hash of what it sent; the next run skips a row whose hash
still matches, and a run with nothing to push prints `nothing to sync - all N
uploaded rows already match their last push` rather than resending everything
— that message is the healthy steady state, not a failure. If a row's edit
isn't showing up, or everything needs to go out again, see
[`docs/OPERATIONS.md`](docs/OPERATIONS.md#only-a-changed-row-is-actually-sent--and-what-to-do-if-yours-isnt)
for the two recovery levers (clear one row's `ia_sync_hash`, or clear the
whole column) — never type a value into that column by hand. See
[`docs/decisions/SHEET-PROTOCOL.md`](docs/decisions/SHEET-PROTOCOL.md#a-row-pushes-only-when-its-content-changed)
for why.

Pushing and stamping happen in batches of 500 rows at a time, overridable
with `--chunk-size` — the same flag `upload` has, but for a different reason:
`sync-metadata` doesn't create items, so IA's per-run item cap doesn't apply
to it. The batching here is to stay under the Google Sheets API's 60
writes-per-minute-per-user quota (one write per chunk, not per row), and so
that a run interrupted mid-way keeps every chunk it finished stamping instead
of losing all of them.

A blank cell means **leave this field alone**, not "delete it" — so an
accidental cell clear can never strip metadata from a permanent public item.
To actually delete a field, put the literal `REMOVE_TAG` in that cell (the
same sentinel the official `ia` CLI's `--modify field:REMOVE_TAG` uses).

`--live` reads the real Sheet and targets the real, permanent items. The
command refuses to send a live correction to a `zztest-` item, or a rehearsal
correction to a real one.

An item with no uploaded Sheet row cannot be reached this way. Fix it with
the raw `ia` CLI (`ia metadata <identifier> --modify ...`) or the item's
edit page on archive.org.

### `reconcile-files` — correct a filename cell that doesn't match the drive

```bash
python ia_bulk.py reconcile-files --project sarasoldphotos --dry-run
python ia_bulk.py reconcile-files --project sarasoldphotos
```

It reads the Sheet live and resolves every row's file the same way
`validate`/`upload` do. Rows that resolve are left alone entirely — never
prompted about, never written to. So are rows nobody has catalogued yet:
a row whose filename cell is blank asserted no file, so there is nothing
to be wrong and nothing to propose — they are counted in one line
(`2,914 rows not yet catalogued - no filename to reconcile, skipped`) and
never prompted about, the same not-ready-versus-broken split
[`docs/decisions/READINESS.md`](docs/decisions/READINESS.md) draws for
`validate` and `upload`. For each row that named a file and does **not**
resolve,
it looks for a single best match among the files still unclaimed in that
row's own folder and asks before touching the Sheet:

```
row 7  'CD 1 01 53 34 2 Finnis Meat Market.jpg'  does not resolve in 'SOP CD 1'
       proposed: 'CD 1 01 53 34 2 Finnish Meat Market.jpg'   (edit distance 1)
       [y] accept   [n] not this one   [e] type it   [l] list unclaimed   [q] stop
       >
```

Prompt keys: `[y]` accept the proposal (only offered when there is one);
`[n]` leave this row alone and move on; `[e]` type a filename yourself — it
is resolved against disk the same way a proposal is, so a typo here is
caught and re-asked rather than written; `[l]` list every unclaimed file in
the row's folder; `[q]` stop the run — whatever was already accepted before
`[q]` has already been written, so it's safe to rerun later and pick up
where this left off. When more than one file matches equally well,
reconciliation asks nothing and leaves the row alone, naming every match on
screen — the same "never guess between two" rule `resolve_file()` follows
everywhere else; see
[`docs/decisions/RECONCILIATION.md`](docs/decisions/RECONCILIATION.md).

A header defect — two Sheet columns whose names normalize to the same IA
field, or one that normalizes to nothing — stops the run before anything is
proposed, because it corrupts every row identically and would send the
correction to a different column than the one the value was read from. A
single *data* row longer than the header is skipped and named instead; the
rest of the run proceeds.

It writes only the `file_name` column — whichever Sheet column
`file_template`'s last segment names — never `folder_on_lacie_drive`, and
never anything else on the row. A wrong folder cell is left for a human;
reconciliation only ever searches inside the folder a row already names.

A file accepted for one row is removed from the pool offered to every row
still to come in the same run, so two misspelled rows in one folder can
never both be pointed at the same photograph.

Before each batch of accepted corrections is written, the Sheet is read
again and any correction whose row no longer holds the filename it was
matched against is dropped and reported rather than written — a session can
run for an hour on a Sheet other volunteers are editing, and one row
inserted in that time would otherwise shift every later write onto the
wrong photograph.

Exits `0` whether or not every row ends up resolved — rows left for later
are the normal state of a ~10,000-row backlog worked over many sessions,
not a failure; see
[`docs/decisions/RECONCILIATION.md`](docs/decisions/RECONCILIATION.md#exit-code-is-0-while-work-remains).
Unless `--dry-run` is passed, it writes a timestamped log to `--log-dir`
(default `logs/`), one line per row considered, recording what was
proposed and what was decided.

Which files on disk even count as photographs — and so can ever be
proposed — is the project's `photo_extensions` in
`projects_registry.json`: optional, defaulting to `.jpg`/`.jpeg`/`.tif`/
`.tiff`/`.png` when a project doesn't set one. That default is what keeps
the contact-sheet PDFs already sitting alongside the photos on the drive
from ever being offered as a match.

`--live` reads and writes the real Sheet instead of the test one, same as
every other command.

### `append-rows` — add skeleton rows for files that have no row

```bash
python ia_bulk.py append-rows --project sarasoldphotos --dry-run
python ia_bulk.py append-rows --project sarasoldphotos
```

Walks every folder under `files_dir` — including folders no row names,
which is the point — and appends one row per photo file that no row
claims: the folder and filename cells only, every other column left blank
for the cataloguer. It removes the transcription work, never the
cataloguing work. New rows land at the bottom of the Sheet grouped by
folder A–Z, filenames A–Z within, and `photo_extensions` decides what
counts as a photo, so the contact-sheet PDFs on the drive never get rows.

It **refuses to run while any row names a file that does not resolve**,
with no override flag: to the survey, a typo'd row and a missing row both
look like an unclaimed file, so appending past one could add a second row
for a photograph the typo'd row already means. Run `reconcile-files` until
every such row is fixed — or blank the filename cell of one that cannot
be, which marks the row not-yet-catalogued (see
[`docs/decisions/RECONCILIATION.md`](docs/decisions/RECONCILIATION.md),
"Reconciliation ships before append"). Rows nobody has catalogued yet are
counted in one line and never block anything. A data row longer than the
header is *fatal* here, not skipped as in `reconcile-files` — a misread
row can make the file it really means look unclaimed, and append trusts
the whole survey at once.

Photo files sitting at the top of `files_dir`, outside any folder, cannot
be expressed as rows by a folder/name `file_template`; they are named on
screen rather than silently ignored.

Safe to re-run: appended rows resolve, so their files are claimed and a
second run over an unchanged drive appends nothing. Unless `--dry-run` is
passed, each run writes a timestamped log to `--log-dir` (default
`logs/`), one line per appended row.

### `doctor` and `setup` — check the machine, and fix what can be fixed

```bash
python ia_bulk.py doctor --project sarasoldphotos
python ia_bulk.py doctor --project sarasoldphotos --live
python ia_bulk.py setup --project sarasoldphotos
```

`doctor` reports whether this machine can run the pipeline — Python and the
dependencies, the Google key and the `ia` credentials and their permissions,
the spreadsheet id, whether the Sheet answers, its sync columns, the files
drive, and the LaunchAgent — one `PASS`/`FAIL`/`UNKNOWN` line each, and
changes nothing. `setup` first fixes what it can on its own (the key file's
permissions), then prints the same report; `./install.sh` ends by running it.
Both check the test Sheet unless `--live` is passed, and `--offline` skips
the checks that need the network.

`setup --live --enable-agent` also installs and loads the hourly
`sync-metadata --live` LaunchAgent. Run it as `./install.sh --project
sarasoldphotos --live --enable-agent`, from the operating account, only once
the first live runs are verified by hand. See
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md), "Enabling the hourly sync" and
"Checking a machine later".

## Safety rail

By default every command targets IA's `test_collection` sandbox. The Sheet's
`ia_identifier` column always holds the
real, permanent identifier — never author a `zztest-` identifier by hand.
Instead, `upload` and `sync-metadata` automatically prepend
`zztest-<run's stamp>-` to the real identifier for every network call (e.g.
`lcps-astoriaphotos-00001` becomes
`zztest-20260819t144907-lcps-astoriaphotos-00001`) unless `--live` is
passed. The stamp is unique per invocation, so a rehearsal never collides
with a previous rehearsal's items — see
[`docs/DECISIONS.md`](docs/decisions/IDENTIFIERS.md#test-identifiers-carry-a-per-run-stamp).
Pass `--live` to target the real collection with the real identifier as-is,
with no stamp — do this deliberately, never as a default.

```bash
python ia_bulk.py upload --project sarasoldphotos --live
```

**Before any `--live` run**, check both of these by hand — nothing in the
tool pins either value (see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#known-gaps)):
- `projects_registry.json`'s `collection_key` still reads `"lcps"` — see
  [The collection key is `lcps`](docs/decisions/IDENTIFIERS.md#the-collection-key-is-lcps).
  This value never reaches Internet Archive.
- the project's `ia_collection` in `projects_registry.json`, which nothing
  checks against Internet Archive — for
  `sarasoldphotos`, already confirmed by hand against archive.org on
  2026-08-22 (see [`docs/DECISIONS.md`](docs/DECISIONS.md#still-open)); a
  second project's registry entry would need the same one-time check.

`--dry-run` is the cheapest way to check the second one: it prints every
identifier it would mint and every cell it would write, and touches nothing.

Every command reads the Sheet live; there is no CSV export step. The
offline CSV paths were removed on 2026-09-23 — see
[`docs/DECISIONS.md`](docs/decisions/SHEET-PROTOCOL.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).

## Tests

```bash
python -m pytest test_ia_bulk.py -v
```

### E2E rehearsal (opt-in)

```bash
python -m pytest test_e2e_rehearsal.py --run-e2e -v -s
```

Drives the real CLI against the Test Sheet and IA's `test_collection` — the
automated form of `docs/OPERATIONS.md`, "Rehearsing the log tabs". Takes a few
minutes and is skipped without `--run-e2e`. Needs the service-account key at
`.ignored/google-service-account.json` in the checkout being run (a fresh
worktree has none) and `ia configure` done on the machine. It rewrites the
Test Sheet every run; test data is ephemeral.

**The Test Sheet holds the `e2e` project's grid.** Test-mode hand commands
use `--registry e2e_fixtures/registry.json --project e2e`; `--project
sarasoldphotos` without `--live` now reads those same rows, so save it for
`--live` against the real Sheet. After a passing run, rows 2, 3 and 5 are
uploaded and synced, row 2's `Title` is edited, and row 6 is still not ready
(no theme) — reset rows per
[`docs/OPERATIONS.md`, "Re-rehearsing a row that is already done"](docs/OPERATIONS.md#re-rehearsing-a-row-that-is-already-done)
before a hand upload.

## Linting and type checking

```bash
python -m ruff check .        # style/lint (unused imports, bug-prone patterns, ...)
python -m pyright ia_bulk.py test_ia_bulk.py conftest.py test_conftest.py e2e_sheet.py test_e2e_sheet.py test_e2e_rehearsal.py   # static type checking (same engine as VS Code's Pylance)
```

`pyright` is the command-line engine behind the Pylance VS Code extension —
running it here gives the same diagnostics Pylance would show in the editor,
without needing VS Code open.
