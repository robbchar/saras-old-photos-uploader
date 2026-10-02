# Operations Runbook

How to actually run a batch, from Google Sheet to Internet Archive. Read
[`ARCHITECTURE.md`](ARCHITECTURE.md) first if you want to know *why* the tool
is shaped this way; read [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md) before you trust
a run's output.

**Current status: no `--live` run has ever happened.** Every log in `logs/` is
a `test_collection` run (`"live": false`). The first real run is still ahead,
and several things below have never been exercised against production.

## Google Cloud prerequisites

Provisioning the service account, its key, and sharing the Sheet now lives in
[`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — that's a one-time, per-machine setup
step, not part of running a batch. The five-minute check that the service
account actually works end to end is
[§16 of that document](DEPLOYMENT.md#16-verifying-the-service-account-by-hand);
run it on a new machine and after replacing the key.

## The pipeline

```
Google Sheet (read live)  →  validate  →  upload  →  sync-metadata
                                             (test)     (corrections)
                                                ↓
                                          upload --live
```

The Sheet is the source of truth, and every command reads it directly over
the Google Sheets API — see
[The Sheet is read live](decisions/SHEET-PROTOCOL.md#the-sheet-is-read-live-the-csv-becomes-the-offline-path).
That decision's offline CSV path was reversed on 2026-09-23: there is no CSV
export step and no CSV input.

Bringing a batch up from nothing to permanent Internet Archive items is the
four numbered phases below — validate, rehearse, go live, correct — matching
the pipeline diagram above exactly.

## 0. Reconcile file names (when rows don't resolve)

Not one of the four phases above — a prerequisite for the first of them,
needed only when it applies. Run it whenever `validate` (§1) reports rows
whose filename cell doesn't resolve against the drive, before trusting the
rest of that report.

```bash
# see what it would propose, without prompting or writing anything
python ia_bulk.py reconcile-files --project sarasoldphotos --live --dry-run

# work through the mismatches interactively
python ia_bulk.py reconcile-files --project sarasoldphotos --live
```

It reads the Sheet live, finds every row that *named* a file which
doesn't resolve, and proposes a correction for the ones it can — one row
at a time, nothing written until you press `[y]` or type one at `[e]`. A
row whose filename cell is blank is not yet catalogued rather than broken:
it is reported as a single count and never prompted about, so a run on the
full Sheet asks about the couple of hundred rows an operator can act on
rather than the ~2,900 that have no filename to correct. See
[`README.md`](../README.md#reconcile-files-correct-a-filename-cell-that-doesnt-match-the-drive)
for the prompt keys and exactly what it does and does not touch.

**Why before validate.** "Why this is a hard rule" below walks through the
real case this project already hit: of three rows that failed file
resolution in a nine-row rehearsal, two were real, unnoticed mismatches —
of two different kinds. `Finnis Meat Market.jpg` vs `Finnish Meat
Market.jpg` is a plain human typo. `Roy's Shell.jpg` vs ` Roy_s  Shell.jpg`
is not a typo at all: apostrophes and doubled spaces get mangled when
files are copied off source media, so a volunteer's transcription of that
one, correct by eye, still doesn't match the disk — no amount of care
would have caught it. `reconcile-files` needs both of its matching passes
because of that split: an exact match after normalization catches the
`Roy_s` kind, edit distance catches the `Finnis` kind. `validate`'s error
list and not-ready breakdown are only a useful signal if a row reported
broken is actually broken rather than mismatched in one of these two
ways, so working through both first is what makes the rest of
`validate`'s report worth trusting. See
[`docs/decisions/RECONCILIATION.md`](decisions/RECONCILIATION.md) for why
reconciliation only ever proposes a correction rather than applying one,
and why its exit code stays `0` while rows remain unresolved.

Safe to run repeatedly — a Sheet with nothing left to reconcile prints
`nothing to reconcile - every row with a filename resolves against the
drive` and does nothing else.

## 0b. Append rows for files that have no row (after reconciling)

The other half of the same job: once every row that names a file resolves,
files still unclaimed are genuinely uncatalogued, and this appends a
skeleton row — folder and filename cells only — for each of them.

```bash
# see what would be appended, grouped by folder
python ia_bulk.py append-rows --project sarasoldphotos --live --dry-run

# append for real
python ia_bulk.py append-rows --project sarasoldphotos --live
```

The order is not optional, and the tool enforces it: `append-rows` refuses
to run while any row names a file that does not resolve, because a typo'd
row and a missing row both present as "unclaimed file" — appending first
would create a duplicate row for every photograph a typo'd row already
means. Work through `reconcile-files` (§0) until it has nothing left, then
append. Safe to re-run: a second pass over an unchanged drive appends
nothing. See
[`README.md`](../README.md#append-rows--add-skeleton-rows-for-files-that-have-no-row)
for exactly what it writes and refuses.

## 1. Validate (before every upload)

> **Run this before every `upload`, every time.** It is not optional and it is
> not a first-run-only step. `validate` performs the *same* file resolution
> `upload` does, but uploads nothing and writes nothing, so every row it
> rejects is a row you fixed for free instead of discovering mid-run. See
> "Why this is a hard rule" below.

```bash
# against the e2e registry's Test Sheet (the Test Sheet holds the e2e grid; see
# docs/decisions/SHEET-PROTOCOL.md, "Test data is ephemeral")
python ia_bulk.py validate --registry e2e_fixtures/registry.json --project e2e

# against the real Sheet (still uploads nothing and writes nothing)
python ia_bulk.py validate --project sarasoldphotos --live
```

Exits `0` if every row passes, `1` otherwise. `--live` only chooses *which*
Sheet is read; it never makes this command write or upload anything.

Checks per row: `mediatype` is structurally required (it's
injected from the registry, so a blank one means the registry is wrong, not
the Sheet); each row's file is resolved against `files_dir` using
`file_template`; and, when present, `ia_identifier` matches
`COLLECTIONKEY-PROJECTID-NUMBER`, is registered in `projects_registry.json`,
and is unique.

**Proofread the header row by hand.** Header text becomes the IA field name,
so a typo (`Architectura Style`) ships on every item in the batch and needs a
correction run to undo. No tool can catch it: a misspelled field name looks
the same as an intentional one. The same goes for typos in values
(`Commerical Buildings`); fix those in the Sheet.

A human-filled field that is simply blank — no title yet, no filename yet —
is *not* an error. It marks the row **not ready**, a
different question from whether the row is valid; see
[`docs/DECISIONS.md`](DECISIONS.md), "A blank cell is not an error". Which
fields count is the project's `required_for_upload` list in
`projects_registry.json` — normalized column names, e.g. `["title",
"theme"]` — plus the file-resolution outcome. The registry key is required
with no default, and every name in it is checked against the Sheet's actual
headers at startup, so a typo (`"titel"` for `"title"`) fails loudly instead
of quietly marking every row not-ready forever.

`validate`'s report marks a not-ready row inline, and rolls the rest up into
per-field detail under the count it belongs to, instead of listing thousands
of identical rows:

```
[FAIL] row 7
    - no file found in '...' matching 'CD 1 01 53 34 2 Finnis Meat Market.jpg' (...)
[FAIL] row 41  (not yet catalogued)
    - no file found in '...' matching 'CD 1 02 11 38 4 Sunfower Dairy.jpg' (...)

2,898/2,900 rows passed

...

25 rows ready to upload (no identifier yet)
2,847 rows not yet assigned an identifier and not yet catalogued (missing
required fields) - waiting on data entry, not blocked by an error
    2,100 missing title: rows 190-2289
    1,900 missing file_name: rows 190-2089
    1,900 missing folder_on_lacie_drive: rows 190-2089
    940 missing theme: rows 190-1129
    (a row missing more than one field appears in more than one count above,
    so these do not sum to 2,847)
6 already uploaded
0 reserved but unconfirmed - will retry under existing identifier
```

Row 7 is genuinely broken (a filename that doesn't resolve) and is itemized
regardless of readiness. Row 41 carries the `(not yet catalogued)` marker
*and* an error — readiness and validity are different questions about the
same row, not alternatives, so both can be true at once. The great majority
of not-ready rows carry no error at all and are never itemized individually,
appearing only in the per-field detail above — a flat "N not yet catalogued"
total can't tell you whether the backlog is mostly missing filenames
(automatable) or mostly missing titles (it isn't); the per-field counts can.

Each of those lines ends with the rows behind its count, compressed into
ranges (`rows 190-2289`). That matters because a not-ready row prints as
`[PASS]` — a blank cell is not an error — so it is indistinguishable at a
glance from the thousands of rows that are simply fine, and this is the only
place it can be picked out. The detail sits under the lifecycle line that
counts it rather than in a block of its own; if some rows went not-ready
*after* uploading (a required column cleared by hand), that group gets its own
detail under its own line, because it needs different work.

Both halves of `file_template` appear in that detail. This project's is
`{folder_on_lacie_drive}/{file_name}`, so a row nobody has touched leaves two
blank cells and is counted once under each — which is why those two counts
track each other. The lines are ordered by count, highest first, ties broken
alphabetically.

`upload` deliberately does **not** repeat this detail on every run — see
[`docs/DECISIONS.md`](DECISIONS.md), "A blank cell is not an error". It
itemizes only the rows it would otherwise have uploaded (ready, but failing
validation) and gives the uncatalogued backlog one contained line instead:

```
[FAIL] row 7
    - no file found in '...' matching 'CD 1 01 53 34 2 Finnis Meat Market.jpg' (...)

1 row failed validation and will be skipped; the rest are uploaded, and this
command still exits non-zero so a partial run is never mistaken for a clean one

2,847 rows not yet catalogued (41 of them also have unresolvable filenames -
run `validate` to see them)
```

Only a row that was actually in this run's scope — ready, but broken —
affects `upload`'s exit code. A not-ready row was never going to be uploaded
regardless, so it does not flip the exit code; if it did, `upload` would
return non-zero on every run until all ~2,900 uncatalogued rows are filled
in, which trains an operator to stop trusting the exit code at all. See §2
below for `upload`'s modes.

A row whose `withdrawn` cell says yes is **held**: someone decided it must
not go out (see "Withdrawing an item" below). `validate` marks it
`(withdrawn - held back)` and counts it on its own line under its state —
``N rows withdrawn before upload - held back from `upload` while 'withdrawn'
says yes``, or, for an uploaded row, `N rows already uploaded and marked
withdrawn`. `upload` skips it, keeps any identifier it already reserved, and
prints `N rows held back from upload (withdrawn = yes)`. A held row does not
flip `upload`'s exit code; one that also carries an error still makes
`validate` exit 1.

It also prints a **field receipt** — every metadata field name the run would
create, plus the columns held back as `(LCPS Internal)` and the ones only used
to find each row's file (the columns `file_template` names, by their normalized
names). Read it. This receipt
has caught two real Sheet typos before they shipped as permanent IA fields.

**What `validate` does not check** — the values inside your columns. A Sheet
with every metadata column shifted into the wrong field still passes. See
[`KNOWN-ISSUES.md`](KNOWN-ISSUES.md#1-noindex-cannot-be-changed-by-sync-metadata).

`validate --json` prints the same findings as one JSON document, for
programs rather than people. That means the upload page. The timestamp,
banner and any refusal go to stderr, so stdout parses whole. Its shape is
recorded in
[`decisions/READINESS.md`](decisions/READINESS.md#validate---json-is-a-contract-not-a-second-report),
and `contract_fixtures/` holds a worked example of each form.

### Why this is a hard rule

On 2026-08-21 a nine-row rehearsal had three rows fail file resolution. One
was deliberate; two were real, unnoticed Sheet-vs-disk mismatches:

| Sheet said | Disk had | Difference |
|---|---|---|
| `…53 34 2 Finnis Meat Market.jpg` | `…53 34 2 Finnish Meat Market.jpg` | `Finnis` vs `Finnish` |
| `…53 50 01 Roy's Shell.jpg` | `…53 50 01  Roy_s Shell.jpg` | double space; `'` became `_` |

Both were caught, correctly — the tool resolves files by exact match then
case-insensitive stem match, and **never guesses** between near-misses. But
they were caught *during an upload run*, after other rows had already been
uploaded. A `validate` pass would have listed all three in about two seconds,
before anything happened.

The `Roy_s` case is worth understanding: apostrophes and doubled spaces get
mangled when files are copied off source media, so the Sheet cell a volunteer
typed by eye will not match the filename on disk. Expect this class of error
to recur across a 10,000-row collection.

## 2. Test run

**The Test Sheet holds the `e2e` project's fixture rows**, not
`sarasoldphotos`'s — see
[`decisions/SHEET-PROTOCOL.md`](decisions/SHEET-PROTOCOL.md#test-data-is-ephemeral).
Test-mode hand commands use `--registry e2e_fixtures/registry.json --project
e2e` in place of `--project sarasoldphotos` below; `--project sarasoldphotos`
is for `--live` against the real Sheet.

```bash
# against the e2e registry's Test Sheet (the normal path)
python ia_bulk.py upload --registry e2e_fixtures/registry.json --project e2e

# ...and again, recording the minted identifiers in the test Sheet
python ia_bulk.py upload --registry e2e_fixtures/registry.json --project e2e --write-identifier
```

Run it once without `--write-identifier` first: that mode
issues zero writes to the Sheet, so it is a rehearsal you can repeat freely.
`--dry-run` goes further and uploads nothing at all, printing a summary of the
identifiers it would mint and, with `--write-identifier`, the cells it would
write; add `-v` to list each one.

With no `--live`, the tool targets IA's `test_collection` sandbox and prepends
`zztest-<run's stamp>-` to each identifier before every network call — the
stamp is unique per invocation, so this run's items never collide with a
prior rehearsal's (see [`docs/DECISIONS.md`](DECISIONS.md), "Test identifiers
carry a per-run stamp"). The Sheet's `ia_identifier` keeps the real,
permanent identifier — never hand-write a `zztest-` identifier.

Test items auto-expire after roughly 30 days. **Do not construct the URL by
hand from `zztest-<identifier>`** — the stamp makes that guess wrong, and
it will land you on a different (possibly already-darkened) run's item
instead of your own. Get the real URL from the run itself: each row's
progress line and the log's `uploaded_as` field both print the full stamped
identifier, and the row's `ia_url` cell holds the exact
link once `--write-identifier` (or `--live`) has run. Spot-check a few of
those URLs in a browser and confirm the metadata fields are the ones you
meant, with the values you meant.

### Re-rehearsing a row that is already done

The rehearsal above repeats freely only while it writes nothing. Once
`--write-identifier` (or `--live`) has run, the row carries both a minted
identifier and an upload timestamp — and `classify_row()` in
`identifiers.py` reads exactly those two cells. Both filled means `DONE`,
and `DONE` rows are skipped from then on, so a later run prints

```
nothing to upload - every valid row is already marked uploaded
```

and exits having done nothing. **That output is correct behavior, not a
failure.** It is the same guard that stops a re-run of a `--live` batch
uploading the collection twice; it is only in the way during a rehearsal.

To make rehearsed rows uploadable again, clear four cells on each of them in
the **test** Sheet:

| Column | Written by |
|---|---|
| `ia_identifier` | the mint, before the upload |
| `ia_uploaded` | the confirm, after the upload |
| `ia_url` | the confirm |
| `ia_identifier_bib` | the confirm |

Clearing `ia_identifier` is what actually returns the row to `UNASSIGNED`.
The other three go with it so the row never sits in a state where its
timestamp and URL describe an item it no longer names — the next run
overwrites all three regardless.

**The numbers are not burned.** The next run re-mints the *same* number, and
because test mode prepends a fresh `zztest-<stamp>-` per invocation, it
creates a brand-new sandbox item with no collision (see §2 above). A repeat
rehearsal costs a four-column delete, never an identifier.

There is no command for this, deliberately — see
[`docs/DECISIONS.md`](DECISIONS.md), "The rehearsal reset is a hand edit, not
a command". Do it in the test Sheet only. **Never in the real one**, where
those four cells are the record that an item exists at all: clearing them
tells the next run to mint a second identifier for a photograph that is
already uploaded.

**This is not the `sync-metadata` reset — the two are opposites.**
`sync-metadata` only targets `DONE` rows, so clearing `ia_identifier` hides a
row from it entirely. Its reset is a single cell: clear `ia_sync_hash` to
re-send a row (§4, "Only a changed row is actually sent — and what to do if
yours isn't"). Doing *this* section's reset before a `sync-metadata` run
leaves it with no targets at all, and it returns before it even opens a log,
so there is not even a run record to explain the silence.

## 3. Live run

```bash
python ia_bulk.py upload --project sarasoldphotos --live
```

### Pre-live checklist

Nothing checks that these values are the right ones. A wrong value here puts
real files in the wrong place under a permanent identifier.

- [ ] `projects_registry.json` → `collection_key` still reads `"lcps"`, the
      first segment of every identifier this tool mints — see
      [The collection key is `lcps`](decisions/IDENTIFIERS.md#the-collection-key-is-lcps).
      Existing identifiers are checked against it, but new ones are simply
      minted under whatever it says. This is a different thing from the IA
      collection uploads land in; see the next item.
- [ ] `projects_registry.json` → `ia_collection` (currently
      `"sarasoldphotos"`) is the actual Internet Archive collection
      `upload --live` uploads into — taken from the registry automatically;
      there is no flag for it.
      **Do not confuse this with `collection_key` above; they are unrelated
      values, and it is a coincidence of spelling — not a code relationship —
      that `ia_collection` and the project id now share the same string.**
      Before it reads the Sheet, `upload --live` asks archive.org whether
      `ia_collection` **exists as a collection**. It refuses if it does not,
      and also if archive.org cannot be reached. What it cannot confirm is
      that it is the *right* collection: the parent `lcpsdigitalcollection`
      would pass too. That confirmation has to happen by hand, once, before
      the first `--live` run. **Done 2026-08-22**: `archive.org/details/sarasoldphotos` was
      checked by hand — it exists (title "Sara's Old Photos") and is already
      a child of the LCPS parent collection, `lcpsdigitalcollection`. Items
      are tagged into this subcollection alone; membership in the parent (and
      in `clatsopcountyhistoricalsociety` and `americana`) follows
      transitively, so listing multiple collections on each item is
      unnecessary. An earlier pass this same day recorded `ia_collection` as
      `lcpsdigitalcollection` itself — that collection is real and does
      exist, but it is the *parent*, not the subcollection the operator
      decided items should carry; this corrects it before any `--live` run.
      Re-check only if the registry value changes again.
- [ ] `validate --project <id> --live` was run **today, against the real
      Sheet, and exited 0** — not a validate of the test Sheet, and not
      yesterday's. See §1; this is the cheapest check on this list and the one
      most likely to find something.
- [ ] That same run's **"N rows ready to upload (no identifier yet)"** line was
      read, and N is the number you expect. **Exiting 0 no longer means "every
      row is ready"** — a Sheet of 2,900 uncatalogued rows exits 0 by design,
      because a not-ready row is waiting on data entry, not carrying an error.
      That count, not the exit code, is what tells you this run has the scope
      you think it has. It is the same set `upload` will plan, so an N that
      surprises you is worth resolving *before* anything permanent happens.
- [ ] The `ia` credentials belong to `admin@lcpsociety.org`:
      `ia configure --check` (on the Mac, `./.venv/bin/ia configure --check`)
      asks archive.org and prints
      `The credentials for "admin@lcpsociety.org" are valid`. Any other
      address, or `Your credentials are invalid`, is a stop.
- [ ] The Sheet is current and saved — the rows you intend to upload were
      filled in, and no edit is still sitting unsaved or as a pending
      suggestion. A `--live` run reads the Sheet directly; there is no CSV
      export step to redo, and nothing local to go stale.
- [ ] The e2e rehearsal passes on this checkout
      (`python -m pytest test_e2e_rehearsal.py --run-e2e -v -s`), **and**
      `python ia_bulk.py upload --project sarasoldphotos --live --dry-run`
      over the real Sheet named collection `sarasoldphotos` in its opening
      lines and printed `Internet Archive collection 'sarasoldphotos'
      confirmed on archive.org`, **and** the same command with `-v` plus the
      `--batch` or `--limit` the real run will use printed the identifiers
      and cells you expected. Without those, `-v` lists every ready row.
- [ ] The batch fits today's pacing plan — see "Pacing" below. The tool
      refuses a run that would take the Sheet's last 24 hours of uploads
      past 5,000, but it counts only this Sheet: test runs through the same
      account are up to you.
- [ ] **The one-upload-at-a-time lock actually refuses a second run on this
      Mac.** Start a test-mode `upload` (no `--live`, no `--dry-run`, so it
      takes the lock but uploads only to the sandbox) in one terminal and,
      while it runs, start another `upload` in a second terminal — both from
      the operating checkout. The second must exit refusing and name the first.
      Confirm this by hand because the Mac's `flock` path never runs under
      pytest — see
      [`decisions/QUOTA-AND-RUNS.md`](decisions/QUOTA-AND-RUNS.md#one-upload-runs-at-a-time-enforced-by-upload).

Identifiers are permanent. An item uploaded under the wrong identifier cannot
be renamed, only darkened by IA staff on request.

## Running an upload from the page

For a volunteer who shouldn't need a terminal at all. This is not a third way
to upload — it runs the exact `validate`/`upload` commands above, through a
browser instead of a command line. Which Sheet and collection it targets
(test or `--live`) is fixed by how the server was started, not a switch on
the page — see [`decisions/UPLOAD-PAGE.md`](decisions/UPLOAD-PAGE.md).

```bash
python ia_bulk.py serve --project sarasoldphotos --live
```

Leave that running, then open the bookmark:
`http://127.0.0.1:5277` — not `localhost`; the server listens on IPv4 only.
Default port is 5277 (`--port` to change it, matched by the URL you open).

On the page:

1. Pick a theme from the picker. Each one shows how many of its rows are
   ready to upload; a theme with nothing ready is listed but disabled, with
   the reason ("all uploaded", "3 not ready", "1 withdrawn").
2. Read the preview — the same counts and per-row reasons `validate --batch`
   would print, with **Re-check** if the Sheet has changed since. Rows
   marked withdrawn before upload are listed under **Withdrawn**, and an
   uploaded row marked withdrawn reads `(withdrawn)` in the uploaded list.
3. Press Start, then confirm in the dialog that restates the theme and count
   (and, in live mode, that the upload cannot be undone or renamed).
4. Watch it run: a live output pane streaming the upload's own console
   output, and a "N of M" progress figure. Stop asks to confirm, then stops
   after the current photo — the same clean stop a terminal `upload` gets on
   Ctrl-C (see
   [`decisions/QUOTA-AND-RUNS.md`, "An interrupt stops a run after the
   current item"](decisions/QUOTA-AND-RUNS.md#an-interrupt-stops-a-run-after-the-current-item)).
5. Read the result: uploaded/failed/unconfirmed/skipped counts, each with
   its rows named, exactly as `upload`'s own summary reports them.

Closing the browser tab, or even restarting the server, does not stop the
upload — the child process outlives both; reopening the page reattaches to
whatever is still running or shows the last result. See
[`decisions/UPLOAD-PAGE.md`, "The upload outlives the server"](decisions/UPLOAD-PAGE.md#the-upload-outlives-the-server).

**The page only ever stops its own run.** A run started from a terminal (or
from another page instance) shows on the page as "an upload is already
running" with no Stop button — go to the terminal it was started from and
press Ctrl-C there, same as always ("Stopping a run on purpose" above). The
one-upload-at-a-time lock is shared, so the page and the terminal can never
both be uploading at once, but each can only be stopped from where it was
started.

## 4. Corrections

The Sheet **is** the correction — edit the cell, then:

```bash
python ia_bulk.py sync-metadata --project sarasoldphotos --live --dry-run
python ia_bulk.py sync-metadata --project sarasoldphotos --live
```

Decoupled from upload and safe to re-run. Blank cell = leave alone; literal
`REMOVE_TAG` = delete that field. `noindex` cannot be changed this way —
see [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md#1-noindex-cannot-be-changed-by-sync-metadata).

The columns `file_template` names are never sent, and every push removes them
from the item; the field receipt lists them under "REMOVED from each item this
run pushes". So the first sync after items were uploaded carrying them pushes
every uploaded row once, and the dry run shows each as `(deleted)`.

The dry run prints each field it would change as a `now:` / `new:` pair, with
line breaks shown as `\n` (or `\r\n`) so each value stays on its line. A long
value edited past what fits on the line is shown from just before the edit,
starting with `...`. If the two lines still read the same, a line in
parentheses under them says why the field changes anyway: Internet Archive
holds several separate values that the sync replaces with one, the two differ
only in spaces, or they differ in a way that does not show on screen (an
invisible character, or an accent stored a different way).

Every row marked uploaded is checked against the item its own `ia_url` cell
names, so nothing needs a log and rows uploaded by different runs are each
targeted correctly. See [`DECISIONS.md`](DECISIONS.md), "The Sheet is the
correction". An item with no uploaded Sheet row is out of reach here; fix it
with the raw `ia` CLI (`ia metadata <identifier> --modify ...`) or its
archive.org edit page.

### Only a changed row is actually sent — and what to do if yours isn't

`sync-metadata` can be run by hand, the same way as the other commands above,
or hourly by a LaunchAgent on the Mac, which
`./install.sh --project sarasoldphotos --live --enable-agent` installs once
the first live runs are verified — see
[`DEPLOYMENT.md`](DEPLOYMENT.md#12-enabling-the-hourly-sync). The agent's
runs print to `logs/launchagent-sarasoldphotos.log` instead of a screen, each
one starting with a line giving the UTC time and the command (a library's
import warning can come just before it), and `doctor --live`'s `launch agent loaded`
line says whether its last run exited 0. Whichever way it gets run, most runs
have nothing to do, and it says so:

```
nothing to sync - all 3,842 uploaded rows already match their last push
```

**That message is good news, not a problem.** It means every photo already
uploaded still shows the description, title and other details currently in
the Sheet. The tool only sends a row to the website when that row's details
have actually changed since the last time it was sent — sending everything
every run, whether it changed or not, would be pointless and would bury the
one real edit anybody cares about under a wall of "nothing changed" lines.

To know whether a row changed, the tool keeps two columns of its own on the
far right of the Sheet: **`ia_sync_hash`** and **`ia_last_synced`**. Unlike
the other columns the tool owns, these two stay **visible, with a red
background** — the red means "the tool owns this, don't type here", and both
earn their place on screen. `ia_last_synced` records when a row last went out,
for a human to glance at. `ia_sync_hash` is what the tool actually checks; it
isn't meant to be read, only cleared — and clearing it is how you force a row
to send again (see the carve-out below).

**The rule about the tool's own columns hasn't changed, except for one
carve-out:**

> Never edit a column whose name starts with `ia_` — **unless** a row isn't
> syncing when it should, and then the *only* thing you may do is **clear**
> the `ia_sync_hash` cell. Never type anything into it.

Clearing that cell is always safe — worst case, the row gets sent again for
no reason, and the website just confirms nothing changed. Typing a value in
is the one thing that can actually cause a problem: it would have to be the
exact code the tool would have generated for that row, and there is no way to
guess it, so don't try.

Two situations, and what to do about each:

- **One row's edit isn't showing up on the site.** Find that row, clear the
  `ia_sync_hash` cell on it, and leave it blank. The next run will see the
  row has no hash on record and send it — ask whoever runs `sync-metadata` to
  kick one off.
- **Everything needs to go out again** (for example, a formatting change was
  applied to the whole Sheet). Clear the entire `ia_sync_hash` column — every
  cell in it, for every row. The next run will treat every uploaded row as
  changed and resend all of them.

Both of those are safe to do over the phone: "clear that one cell" or "clear
the whole column" is the entire instruction, and there's nothing to undo
afterward if it turns out not to have been needed.

### Checking a sync run you did not watch

Every real sync run ends with a one-line summary at the bottom of its log,
so you do not have to read the row-by-row lines above it:

```bash
tail -1 logs/sync-metadata-20260906T173949Z.jsonl
```

It gives `checked` / `pushed` / `changed` / `unchanged` / `already_synced`,
plus a `failures` list naming each item Internet Archive refused and why, and
a separate `skipped` list naming the rows the run declined to send at all.
`already_synced` is the hash gate at work — rows read, found to match their
last push, and never sent — and on a healthy run it is nearly the whole
Sheet. `failures` and `skipped` answer different questions: a failure
means the item was contacted, a skip means it was never touched. A run that
withdrew, restored or re-checked items also names them, under `withdrawn`,
`restored`, `clearing`, `clear` and `recheck_failures` (see "Withdrawing an
item" below). The same
numbers are what the run printed on screen — they come from one place and
cannot disagree. See
[`ARCHITECTURE.md`](ARCHITECTURE.md#the-run_summary-record).

If the registry names a `sync_log_tab`, the same summary is in the
spreadsheet too — see "Reading a run from the Sheet instead" below. Note that
a run which found every row already in sync writes **no** tab row, so silence
there means "nothing to do", not "nothing ran"; the JSONL always has the run.

To read the newest one without looking up its timestamp:

```bash
tail -n 1 "$(printf '%s\n' logs/sync-metadata-*.jsonl | sort | tail -n 1)" | python -m json.tool
```

On the Mac, `python` at the end of that pipe is `.venv/bin/python` — macOS
has no `python` command (see [`DEPLOYMENT.md`](DEPLOYMENT.md)).

Sorting the names *is* sorting by time — log filenames are UTC timestamps
precisely so a listing comes out in the order the runs happened (see
`open_log()`). Deliberately no `ls` here: a shell where `ls` is aliased to
a long listing feeds the whole `-rw-r--r-- ...` line into the command
substitution, and `tail` then reports `option used in invalid context`.

### Seeing the summary work, on purpose

**The Test Sheet holds the `e2e` grid; use `--registry
e2e_fixtures/registry.json --project e2e`** — see
[`decisions/SHEET-PROTOCOL.md`](decisions/SHEET-PROTOCOL.md#test-data-is-ephemeral).

**Do not clear `ia_identifier` to set this up.** Clearing those four cells is
the *upload* rehearsal reset — §2, ["Re-rehearsing a row that is already done"](#re-rehearsing-a-row-that-is-already-done) — and it does the
opposite of what is wanted here. `sync-metadata` corrects items that already
exist, so it needs rows that *are* marked uploaded: with a blank
`ia_identifier` every row reads `UNASSIGNED`, the run has no targets, and it
prints `nothing to sync - no row is marked uploaded yet` and returns
**before** it opens a log — so there is no summary to read either.

First, clear `ia_sync_hash` on the rows you want in the demo — otherwise the
hash gate correctly recognizes them as already synced and sends nothing.
Unlike "no row is marked uploaded yet" above, that outcome still opens a log
and writes a summary — `pushed` and `changed` both `0`, `already_synced`
equal to the whole Sheet — so a demo run over unedited hashes shows a
real record, just not an interesting one. Clear the hashes first for a
summary worth reading. Then run it against the test Sheet as it stands. Rows already
carry `zztest-` URLs from earlier rehearsals, and the command targets
whatever `ia_url` names, so rows uploaded under different stamps are each
handled correctly:

```bash
python ia_bulk.py sync-metadata --registry e2e_fixtures/registry.json --project e2e
```

With the hashes cleared, that gives a summary where `pushed` equals
`unchanged` — Internet Archive answers *no changes to `_meta.xml`* for every
row that already matches, and the run stamps every one of them so a repeat
of the same command shows nothing to do. To get more than one outcome into a
single record, stage the Sheet first:

| to see | do this first |
| --- | --- |
| `changed` | edit a Title or description cell on one `DONE` row (this alone changes its hash, so clearing is not needed for this row) |
| `skipped` | clear **only** `ia_url` on one `DONE` row, leaving `ia_identifier` and `ia_uploaded` set — the row never reaches the hash gate, so `ia_sync_hash` doesn't matter here |

The `skipped` setup is the "marked uploaded but its `ia_url` cell is
blank" case, and it is cheap to undo: restore the URL from that row's
upload log, or clear `ia_uploaded` to have `upload` do the row again.

Two limits on what a manual pass can show:

- **`--dry-run` writes no log at all**, so this has to be a real
  test-mode run.
- **`failures` cannot easily be staged by hand** — it needs Internet
  Archive to genuinely refuse a send. Expect `failures: []` on a
  rehearsal, and read that as normal rather than as a gap. That leg is
  covered by the test suite instead.

## Withdrawing an item

For the day a donor, family member or rights holder asks for a photo to come
down. Internet Archive never deletes an item, so "withdrawn" means: its files
are deleted (with IA's thumbnails and other derivatives), its title and
description become the project's withdrawn notice, every other field is
removed, and you ask IA to darken it. See
[`decisions/WITHDRAWAL.md`](decisions/WITHDRAWAL.md).

The Sheet needs two extra header cells first, once: `withdrawn` (yours) and
`ia_withdrawn` (the tool's). `sync-metadata` refuses to run with the first and
not the second. A Sheet without `withdrawn` never withdraws anything.

1. Type `yes` in the row's `withdrawn` cell (`y`, `true`, `x`, `1` or a
   ticked checkbox also work; blank, `no`, `n`, `false`, `0` or an unticked
   box mean keep it; anything else is an error and the row is left alone).
2. Preview, then run:

   ```bash
   python ia_bulk.py sync-metadata --project sarasoldphotos --live --dry-run
   python ia_bulk.py sync-metadata --project sarasoldphotos --live
   ```

   The dry run lists, for each item, the files it would delete (content
   first, `__ia_thumb.jpg` last), the fields it would change or remove, and
   what it leaves alone (`identifier`, `collection`, `mediatype`, and IA's
   own `_meta.xml`-style files), and notes that a real run first checks IA
   isn't running a task on the item or holding a paused or failed one. With a withdraw or
   restore pending, both
   commands first confirm the project's collection on archive.org, as
   `upload --live` does, and refuse if they cannot.

   The real run re-reads the Sheet just before each item's deletes and goes
   ahead only if the row is still where it was and still says `yes`; a row
   changed mid-run is refused by name, nothing is deleted from it, and the
   run exits 1 — the next run does what the Sheet says then. It then asks
   Internet Archive about the item's tasks: while IA is running one (usually
   the derive that follows an upload, for its first few minutes) or holds a
   paused or failed one, the row is refused by name — `Internet Archive is running a
   task on <item> (derive.php); nothing was deleted - run sync-metadata
   again later` — nothing is deleted, written or stamped, and the run exits
   1; the next run tries again. Deletes sent while IA was running a task on
   the item were paused for IA staff, so the tool no longer sends them then.
   Tasks merely queued don't hold it up. Otherwise, as soon as IA accepts
   the deletes, it fills `ia_withdrawn` with the time the withdrawal started,
   then replaces the text, prints
   `N items withdrawn (files deleted, text replaced)`, and ends by printing
   the identifiers and archive.org URLs to send to Internet Archive.
3. Ask Internet Archive to darken each item — only their staff can, and only
   they can undo it. Email them the identifiers and URLs the run printed; the
   tool never contacts IA itself. Until they do, the item page still exists,
   empty, showing the withdrawn notice. This can go at once; it does not wait
   for step 4.
4. Run `sync-metadata` again an hour or so later (or let the hourly agent),
   until every withdrawn item reports `clear`. IA processes deletes in its
   own queue and can rebuild the item's thumbnail from a file it has not
   removed yet, so every run re-checks every withdrawn item: once IA has no
   task open on the item it deletes whatever is left (while IA has any task
   open — even its own queued deletes — it deletes nothing that run; a
   running, paused or failed task is printed under the item),
   and while anything remains, or IA still has a task open for the item, it
   reports
   `N withdrawn items still clearing: <identifiers>`. **Accepted
   trade-off:** until a run reports the item clear, its thumbnail can still
   show the photograph — for hours if IA's queue is busy. The tool never waits
   on IA's queue. Re-checks never change the item's text or the Sheet.

If the hourly agent is enabled, it does steps 2 and 4 on its own within the
hour, so the dry run is for checking, not a gate. Never type into
`ia_withdrawn`; clearing it makes the tool treat the item as present again.

**When the agent ran it, read the Sync Log tab.** Nobody watches the agent's
console, so its darkening hand-off for step 3 is only in
`logs/launchagent-sarasoldphotos.log` on the Mac. The project's
`sync_log_tab` (the `Sync Log` tab) has a `withdrawn` row per item withdrawn,
naming the item it went to, which is enough to write the darkening request
(a withdraw that started but did not finish is a `failure` row reading
"withdrawal started on …" — ask for that item too), and a `clearing` row per item still clearing, whose detail says what was
deleted again, how many tasks IA still has queued (nothing was deleted that
run), that IA is running a task on it, or which paused or failed tasks IA
staff must release.
The console's "still clearing" line names the items only; that detail is in
the tab (and the run's JSONL), whoever ran it — the console prints it under
an item only when IA's tasks stopped that run's deletes.

**If a withdraw fails.** The run names the row and exits 1, and the
`sync_log_tab`, if the project has one, gets a `failure` row. A withdraw of an item Internet Archive does not have deletes
nothing and stamps nothing — check the row's `ia_url`. One that started but
did not finish ("withdrawal started on … but is incomplete") is finished by
the next run, and is still listed for darkening. A re-check that fails (IA
unreachable, or no such item) is named the same way and never reported clear.

**If the run warns `the withdrawn notice replaced the text of <item> but
ia_sync_hash was not written`.** IA refused every delete, but the notice
went up and the Sheet could not record it. Keeping `withdrawn` at `yes`
retries the withdraw; before setting it to `no`, clear that row's
`ia_sync_hash` cell, or the run will read the row as in sync and leave the
notice up.

**If the run warns `ia_withdrawn was not written`.** The deletes started but
the Sheet could not record it (the row moved mid-run, or the Sheet write
failed). Keep `withdrawn` at `yes` and run `sync-metadata` again: that run
finishes the withdraw and records it. Setting `withdrawn` to `no` before then
leaves the files deleted with nothing to put them back.

**If the run says `Internet Archive has paused N task(s) on <item>` or
`N failed task(s)`, `IA staff must release them`.** IA has put the item's
task on hold for its staff, or it failed (it shows as `paused` or `error` on
the item's task list). Neither resumes on
its own, so until IA staff release or cancel it, every run refuses that
item's withdraw (nothing is deleted, so it is not in the darkening hand-off
either), its re-check deletes nothing and reports it still clearing, and a
restore is refused. Email Internet Archive staff the identifier and ask them
to release the item's held tasks — in the same email as the darkening
request, which you are sending them anyway; for a withdraw that was refused,
ask for both. Then run `sync-metadata` again. See
[`KNOWN-ISSUES.md` #8](KNOWN-ISSUES.md#8-an-errored-or-paused-internet-archive-task-blocks-clear-and-restore).

**If an item stays "still clearing" for a day or more** and its `clearing`
rows in the Sync Log tab say "Internet Archive has N tasks queued on
<item>; nothing was deleted", an IA task is probably stuck in IA's queue (a paused or
failed one is named as IA staff's to release instead, as above). Ask
Internet Archive staff to clear it — see [`KNOWN-ISSUES.md` #8](KNOWN-ISSUES.md#8-an-errored-or-paused-internet-archive-task-blocks-clear-and-restore).

**Checking an item really is clear, by hand.** After a run reports it clear,
run this from the tool's folder on the Mac. The first line waits: paste the
item's identifier (the part of its `ia_url` cell after `/details/`) and press
Return.

```bash
read -r ITEM
./.venv/bin/ia list "$ITEM"
curl -sI "https://archive.org/services/img/$ITEM"
```

`ia list` should print only IA's own files (the identifier followed by
`_meta.xml`, `_files.xml`, `_meta.sqlite`, `_archive.torrent`, and
`_reviews.xml` if present), and `curl`'s
first line should be a `302` redirect to IA's placeholder, not `200`.

The e2e rehearsal leaves its row 3 withdrawn for exactly this check, but only
when run on its own:
`python -m pytest test_e2e_rehearsal.py::test_rehearsal --run-e2e -v -s`.
Running the whole file also runs the upload-page e2e test, which resets the
Test Sheet and wipes row 3. Afterwards, on the dev box, repeat
`python ia_bulk.py sync-metadata --registry e2e_fixtures/registry.json --project e2e`
until it reports that item clear, then check it as above — on the dev box,
use `ia` in place of `./.venv/bin/ia`.

**Putting it back.** Set the cell back to `no` (or clear it) and run
`sync-metadata`. It re-uploads the original from the drive — found the same
way `upload` finds it — then writes the Sheet's text back, and prints
`N items restored (file re-uploaded, text put back)`. Two things refuse the
row by name, leave it withdrawn and exit 1:

- **IA is still processing the withdrawal** ("Internet Archive is still
  processing this item's withdrawal"), or the tool could not ask. A file
  uploaded while the withdrawal's deletes are still queued would be deleted
  again. Nothing was uploaded; run `sync-metadata` again later — the hourly
  agent retries on its own. If it says instead that IA has paused or failed
  tasks on the item and "IA staff must release them", waiting won't help:
  ask IA staff, as above.
- **The original is not on the drive** ("restore refused: the original file
  was not found"), matches more than one file, or the row's file cells are
  blank. Put the file back (or fix the cells) first, or set the cell back to
  `yes`.

If the run warns `the restore of <item> landed but ia_withdrawn was not
cleared`, the file and text are back but the Sheet could not record it (the
row moved mid-run, or the Sheet write failed). Leave `withdrawn` at `no` and
let the next run finish: it restores the item again and clears the cell.
Setting `withdrawn` back to `yes` before then would leave the restored text
and photo up, with the re-check deleting only the files.

If IA has already darkened the item, ask them to undo that too.

**Before upload.** A row that says `yes` before it was ever uploaded is held:
`upload` skips it, keeps any identifier it already reserved, and prints how
many it held back; `validate` counts it as withdrawn, and the upload page
lists it under **Withdrawn**. Set it back to `no` and the next upload sends
it.

A **reserved** row (an `ia_identifier` but no `ia_uploaded`) whose upload
may already have landed is held the same way, and `sync-metadata` acts only
on rows marked uploaded, so nothing takes it down yet. To withdraw it: set
`withdrawn` to `no`, run `upload` (it retries the row under the identifier
it already reserved and confirms it), set `withdrawn` back to `yes`, and run
`sync-metadata`. The photo is public on Internet Archive from that upload
until the `sync-metadata` run, so run the two back to back.

**More than ten at once.** A run that would withdraw or restore more than 10
items in total refuses before sending anything and names the rows
(`refusing to run: this run would withdraw … more than the 10 one run may
change without --allow-bulk-withdraw`) — a fill-down or a paste over the
column looks exactly like that. The dry run shows the same refusal after its
preview. Every row whose `withdrawn` disagrees with `ia_withdrawn` counts,
even one that would be refused on its own (say, a restore whose original is
missing). Re-checks of items already withdrawn don't count. If every one is
meant:

```bash
python ia_bulk.py sync-metadata --project sarasoldphotos --live --allow-bulk-withdraw
```

The hourly agent never passes that flag, so a bulk change waits for someone at
the keyboard; each refused agent run leaves a `refused` row, naming the rows,
in the Sync Log tab and a `run_summary` with `refused` in its log. Restores re-upload files but are not counted in the 5,000/day
total — see [`KNOWN-ISSUES.md`](KNOWN-ISSUES.md#7-restores-are-not-counted-in-the-daily-upload-total).

## Pacing and batch limits

IA's limits are **500 items per upload run** and **5,000 per day**.

`chunk_rows()` groups rows into batches of 500 by default, but the loop just
walks through them — there is still no sleep between batches. **The daily
total is enforced across runs:** `upload` counts the items the Sheet's
`ia_uploaded` column shows in the last 24 hours, and refuses to start a run
that would take that past 5,000. The refusal names the room left and when the
whole run fits. Refusing rather than silently capping is deliberate — a run
that quietly stopped short would read as a complete one.

`upload --limit N` caps how many items a single
invocation uploads (counting rows actually ready to go out, not rows
scanned — see `README.md`), and `--chunk-size N` overrides the 500-item
batch size for that run. The two combine literally: `--limit 10
--chunk-size 3` uploads 10 items in batches of 3. A refused run names the
`--limit` that fits the room left — `--limit 200` after 4,800 went out
earlier in the day. The count is a rolling 24 hours rather than a calendar
day, because IA's own day boundary is unknown, and it reads only this Sheet:
a test run's uploads are recorded in the test Sheet, so a live run does not
count them (`decisions/QUOTA-AND-RUNS.md`, "A run may not exceed Internet
Archive's daily item cap"). `--allow-over-daily-cap` overrides the refusal,
and is only correct if you know IA has raised this account's cap. Both
`--limit` and `--chunk-size` are recorded in the run's
`run_header` log line (`ARCHITECTURE.md`, "Logging and resume") so a later
read of the log shows exactly what each run was capped at. If Internet
Archive's own rate limit shows up mid-run, the run now stops cleanly instead
of continuing to grind through failures and names the HTTP status IA sent —
see "Resuming after a rate-limit stop" below for which limit that may be. The
detection is best-effort and has fired on one real response so far
(`DECISIONS.md`, "Rate-limit detection uses a parsed status code, never
message text"), so treat `--limit` as the dependable
control and the detector as a bonus, not the other way around.

`upload --batch "<value>"` scopes a run to one batch — the rows whose
`batch_column` (named in the registry; `theme` for this project) holds that
value. It is how you upload a collection theme by theme rather than
front-to-back, and it narrows the scope before anything is counted, so
`--batch "Logging" --limit 100` uploads 100 of the Logging rows. Preview it
first with `validate --batch "<value>"`, which reports exactly the rows the
upload would take. A misspelled value is refused with the values actually
present in that column listed, so it never runs as a silent empty upload; the
batch is recorded in the `run_header` log line, which is the only field that
explains why a run uploaded 40 of 3,000 ready rows.

### "another upload is already running"

`upload` refuses to start while another upload is running from this
checkout, and names it:

```
another upload is already running (project sarasoldphotos, batch 'Logging', live, started 2026-09-24T14:02:11Z, pid 4312).
Two uploads at once can upload the same rows twice. Let that run finish, or stop it where it was started, then run this again.
```

Two runs at once can upload the same rows twice, so this is a hard stop.
Let the other run finish, or stop it in the terminal where it was started,
then run yours again. There is nothing to clear: the lock goes away with the
process that held it, even one that crashed. `--dry-run` and `validate`
never take the lock, so you can preview while a run is going. Why:
[`decisions/QUOTA-AND-RUNS.md`](decisions/QUOTA-AND-RUNS.md#one-upload-runs-at-a-time-enforced-by-upload).

## Resuming a failed run

**A failing row prints why, as it happens**, indented under its own progress
line in the same style `validate` uses:

```
[3/8] uploading zztest-...-lcps-sarasoldphotos-00003 (SOP CD 1/CD 1 01 51 26 1.jpg)
    - Error retrieving metadata from https://archive.org/metadata/... ReadTimeoutError: read timeout=12
```

Long messages are collapsed to one line and truncated; the log below keeps
the complete text. That distinction matters most for the transient case above
— a read timeout is archive.org being slow, not anything wrong with the row,
and a rerun picks it up. Without the message on screen there is no way to tell
that apart from a real refusal such as `Access Denied`.

Every run also writes `logs/<command>-<timestamp>.jsonl`, one line per row:

```json
{"identifier": "...", "file": "...", "status": "success|unchanged|failure|unconfirmed",
 "error": null, "http_status": null, "uploaded_as": "...", "live": false, "timestamp": "..."}
```

`identifier` is the real, permanent identifier; `uploaded_as` is what was
actually sent to IA. `http_status` is the HTTP status IA answered a `failure`
with (`403`, `503`, ...), or `null` when there was none — a timeout or a
dropped connection. `unconfirmed` means the item
reached Internet Archive but the Sheet could not be updated, because the row
no longer held the identifier the run reserved — someone edited the Sheet
mid-run. Rerun once it has settled; the row is picked up as reserved-but-
unconfirmed and retried under the same identifier. A `success` record also
carries `"already_on_ia": true` when Internet Archive already held the file,
so it was not re-sent (see below); an ordinary success has no such key.

To pick up after failures, rerun the same command. `ia_uploaded` is the
record of what is done, so a rerun resumes by itself: done rows are skipped,
and a reserved row is retried under its existing identifier. The log is an
audit record only; the tool never reads it back.

On a `--live` run, a resumed reserved row can also cost nothing: `upload_row`
passes `checksum=True`, so if that row's earlier upload landed and the item
has settled (it lists the file at a matching MD5 and has no pending tasks),
the file is skipped rather than re-sent. The row prints
`already on Internet Archive at this MD5, so it was not re-sent` and its log
record carries `"already_on_ia": true`. Its metadata was not re-sent either,
so run `sync-metadata` afterwards to push the Sheet's fields; collection and
mediatype stay as the earlier upload set them. An item that has not settled
yet, such as one uploaded moments before, gets the file re-sent under the
same key, which creates no duplicate. A rehearsal never meets the skip,
because every test run stamps a fresh `zztest-` item.

### Transient network failures are expected, and mostly absorbed

Real test runs hit `SSLEOFError` / `MaxRetryError` against
`s3.us.archive.org`, and read timeouts against `archive.org`. Each upload and
each metadata update now gets **three attempts**, backing off about 2s then 4s
with jitter, and prints a line each time it retries:

```
    - upload of 'lcps-sarasoldphotos-00042': attempt 1 of 3 failed (read timeout=12); retrying in 1.6s
```

A run that pauses for a few seconds mid-row is doing this, not hanging. A row
that fails all three attempts is logged as `failure` and the run moves on.
Rerun: the row has no `ia_uploaded`, so the next run retries it. Expect a
long run to need more than one pass.

Retries are for the network only. A refusal — `Access Denied`, a rejected
metadata field, any 4xx — fails on the first attempt, because a second one
gets the same answer. `429`/`503` also skip retry: they mean Internet Archive
is rate-limiting, which stops the run entirely ("Pacing and batch limits"
above) rather than being waited out row by row.

If Internet Archive sends a `Retry-After` header, it is honoured — but never
for longer than 30 seconds. A longer one is treated as "stop the run and come
back later" rather than sleeping through it, so a run cannot silently
stall for an hour inside a single row.

**What this has and has not been tested against.** The retry and rate-limit
handling is exercised against a fault-injecting stand-in for
`s3.us.archive.org` and a local server answering real status codes, so how
the `internetarchive` library behaves for a given response is settled. What is
*not* settled is what Internet Archive actually sends — whether the daily cap
arrives as a 429/503 at all (the one real rate limit seen so far was the queue
throttle below), and how a genuinely slow multi-megabyte upload behaves. No
`--live` run has ever happened. Treat `--limit` as the dependable
control and watch the first real run.

### Resuming after a rate-limit stop

A rate-limited run ends like this:

```
stopped: Internet Archive asked us to slow down (HTTP 503) after 3 items
2 uploaded this run - re-run later to resume: minutes to hours if IA's queue is busy, up to 24 hours if the 5,000/day cap was reached
```

The tool cannot tell which of IA's limits it hit, so read the `failure` line
just above the stop, or the last `failure` record in the log. On 2026-09-24 a
rehearsal was stopped by *"Please reduce your request rate. -
total_tasks_queued exceeds global_limit"*: IA's task queue was busy, not this
account's daily cap, so a rerun later the same day is worth trying. If the
last 24 hours of runs have already sent close to 5,000 items, the rerun's own
refusal says when there is room.
Either way the rerun resumes by itself. The status is also in the log, as the
failure record's `http_status` and the `run_summary`'s `rate_limit_status`.

### Stopping a run on purpose

Press Ctrl-C once. The run finishes the item it is uploading, records it in
the Sheet, and stops:

```
interrupt received: stopping after the current item. Interrupt again to stop now.
stopped: as requested, after 3 items
3 uploaded this run - run the same command again to pick up where it left off
```

Run the same command again to carry on. The rows it never reached are
picked up, and any it had reserved are retried under the identifiers they
already have. Press Ctrl-C a second time only if you must stop at once:
the run then ends without its summary; re-running is still safe. Why:
[`decisions/QUOTA-AND-RUNS.md`](decisions/QUOTA-AND-RUNS.md#an-interrupt-stops-a-run-after-the-current-item).

## Reading a run

```bash
# how many of each status in the newest log
grep -o '"status": "[a-z]*"' logs/upload-*.jsonl | sort | uniq -c

# just the failures, with their errors
grep '"status": "failure"' logs/upload-20260712T125326.jsonl
```

**A run started from the page logs under `logs/page-runs/<UTC>/` instead of
directly in `logs/`.** Each page run gets its own timestamped folder holding
`output.txt` (the child's captured console output), the same
`upload-*.jsonl` log a terminal run writes, and `page-run.json` (which pid,
project, batch and mode started it). Everything above still applies once
you're inside that folder — `grep`/`tail` the JSONL exactly the same way; the
only difference from a terminal run is where it lives on disk. See
[`ARCHITECTURE.md`, "`serve` and the upload page"](ARCHITECTURE.md#serve-and-the-upload-page).

Every real run also ends with a `run_summary` line — `tail -1` of its log
gives the whole run in one record, without the row lines above it. For
`upload` that is `attempted` / `succeeded`, a `failures` list, an
`unconfirmed` list, `not_attempted`, `rate_limited`, `stopped_by_request`
and `rate_limit_status`.
Read `unconfirmed`
first: those files **are** on Internet Archive but were never marked in the
Sheet. The next run retries them under the same identifiers; clearing their
`ia_identifier` first would mint second ones.
See [`ARCHITECTURE.md`](ARCHITECTURE.md#the-run_summary-record).

Symmetrically, `head -1` of the same log gives its `run_header`, whose
`planned` is how many items the run meant to upload after `--limit` (`null`
for `sync-metadata`). Set against the `run_summary`'s counts, that reads as
"N of planned" — how much of what the run intended to do actually happened.

### Reading a run from the Sheet instead

If the project's registry names `upload_log_tab` / `sync_log_tab`, that same
summary is also in the spreadsheet, in an `Upload Log` and a `Sync Log` tab.
This is the one that works from a phone, two states away, while someone reads
you the problem over the telephone — no SSH, no screen share, no talking
anyone through Terminal.

One row per run, then one row per problem. Each row names `when`, the `run`
(the log file to go and read for per-file detail), the `outcome`
(`summary`, `failure`, `unconfirmed` or `skipped`; a `sync-metadata` run
refused before sending anything — too many withdraws or restores, or a live
collection check that failed — is a `refused` row in place of `summary`,
with the reason as its detail), the `identifier`, and the `detail`. A clean run is a single row, and a sync run that found everything
already in sync writes nothing at all — the tab stays scannable on purpose.

The tabs are written to and never read from, so nothing there can affect a
future run, and editing or deleting rows in them is safe; a deleted tab is
recreated on the next run. If a mirror write fails you will see it on
stderr, and the run still succeeds — the JSONL on disk remains the record of
record.

### Rehearsing the log tabs

**This whole recipe is automated.** Run

```bash
python -m pytest test_e2e_rehearsal.py::test_rehearsal --run-e2e -v -s
```

Name `::test_rehearsal` on its own: the file's upload-page e2e test resets
the Test Sheet afterward, wiping the withdrawn row this run leaves for
"Withdrawing an item"'s hand clear check. Each command's output streams live under its step header, upload progress
bars included, as it would in a terminal (`-s` is what lets it through).

It rewrites the Test Sheet from `e2e_fixtures/` first, so the Test Sheet now
belongs to the `e2e` project; a hand rehearsal after it uses
`--registry e2e_fixtures/registry.json --project e2e` in place of
`--project sarasoldphotos` below — `--project sarasoldphotos` without
`--live` now reads those same `e2e` rows, so it is for `--live` against the
real Sheet only. Test data is ephemeral — see
[`DECISIONS.md`](decisions/SHEET-PROTOCOL.md#test-data-is-ephemeral).

Only one rehearsal runs at a time. Step 0 takes the Test Sheet's `E2E Lock`
tab, and a second run fails at step 0 naming the first; wait for it, then
re-run. How a stale lock clears is in
[`decisions/SHEET-PROTOCOL.md`, "One rehearsal at a time"](decisions/SHEET-PROTOCOL.md#one-rehearsal-at-a-time). A hand
rehearsal (the steps below) takes no lock, so check that the Test Sheet has
no `E2E Lock` tab before starting one.

After a passing run, rows 2, 3, 5, 7 and 8 are uploaded and synced, row 2's
`Title` is edited, row 2 has been withdrawn and restored, row 3 is left
withdrawn, and row 6 is still not ready (no theme); reset rows per
["Re-rehearsing a row that is already done"](#re-rehearsing-a-row-that-is-already-done)
(§2) before a hand upload.

| Manual step (command as given below) | Automated step |
|---|---|
| §2 "Re-rehearsing a row that is already done": clear the four `ia_` cells by hand | 0 — whole grid rewritten |
| Delete `Upload Log` / `Sync Log` to re-exercise creation | 0 |
| §1 / DEPLOYMENT §16 step 1: `python ia_bulk.py validate --registry e2e_fixtures/registry.json --project e2e` | 1 |
| Step 1: `python ia_bulk.py upload --registry e2e_fixtures/registry.json --project e2e --write-identifier --limit 1`, twice | 2, 3 |
| Step 2: break a filename, `python ia_bulk.py upload --registry e2e_fixtures/registry.json --project e2e --write-identifier --limit 2` | 4 (`--limit 1`), then 4b (`--limit 2`: two files, one progress bar each) |
| Pre-live checklist: open a `zztest-…` item and read it | 5, 8 |
| DEPLOYMENT §16 step 2: `python ia_bulk.py sync-metadata --registry e2e_fixtures/registry.json --project e2e --dry-run` | 6 |
| Step 3: edit a Title, `python ia_bulk.py sync-metadata --registry e2e_fixtures/registry.json --project e2e`, twice | 7, 9 |
| "Withdrawing an item" steps 1-2: `withdrawn` = yes on two rows, then `sync-metadata` | 9a |
| "Withdrawing an item" step 4: `sync-metadata` again, which re-checks them | 9a2 |
| "Withdrawing an item", putting it back: `withdrawn` = no on one row, then `sync-metadata` | 9b (after IA's queue for the item empties) |
| Step 4: `grep '<when value>' logs/<run value>` | 10 |
| Step 5: File → Version history, by eye | 11 |
| Step 2: put the cell back | 12 |

Each step's failure message names the manual step it stands for.

A repeatable pass against the test Sheet, for after any change to how runs
are mirrored. Every command here is test mode: it reads the test Sheet and
uploads to `test_collection` under `zztest-` identifiers, and `--live`
cannot run at all while `sheet_id` is still a `REPLACE_WITH…` placeholder.

**The first rehearsal on a Sheet is the only one that creates the tabs.**
Every later one exercises the append path instead. To exercise creation
again, delete the `Upload Log` and `Sync Log` tabs from the **test** Sheet
first — never from the real one, where they are the only copy of that
history outside the machine that ran the jobs.

The upload steps need rows that can actually upload. If every ready row is
already marked done, reset two or three of them — §2, ["Re-rehearsing a row that is already done"](#re-rehearsing-a-row-that-is-already-done).
Numbers never burn, so this is safe for `upload` — but it is exactly wrong
for the `sync-metadata` step, which needs rows that *are* marked uploaded
(see "Seeing the summary work, on purpose", above).

1. **One upload, twice.**

   ```bash
   python ia_bulk.py upload --registry e2e_fixtures/registry.json --project e2e --write-identifier --limit 1
   ```

   Run it twice. `Upload Log` should hold exactly **one** header row
   (`when | run | outcome | identifier | detail`), then one `summary` row per
   run, each `detail` matching the line the console printed. A second header
   row, or a tab recreated on the second run, is the defect this step exists
   to catch.

2. **A problem row.** Take a ready row — title and theme filled — and change
   its filename cell to a file that does not exist on the drive. That makes
   it ready but invalid, which `upload` holds back rather than sends.

   ```bash
   python ia_bulk.py upload --registry e2e_fixtures/registry.json --project e2e --write-identifier --limit 2
   ```

   Expect the `summary` row followed by a `skipped` row naming that row's
   identifier, with the bad filename in `detail`. Put the cell back.

3. **Sync, then the quiet run.** Edit a Title on one uploaded row, then:

   ```bash
   python ia_bulk.py sync-metadata --registry e2e_fixtures/registry.json --project e2e
   ```

   Expect `Sync Log` to gain a `summary` row. Run the same command again
   without touching the Sheet: this time **nothing** is appended, because
   every row now matches its last push. That silence is the rule that keeps
   an hourly job from writing a row an hour. If the second run *does* append,
   check first for a leftover staged row — a `DONE` row with a blank
   `ia_url` is skipped on every run, and a run with a skip is always
   mirrored.

4. **The tab and the log agree.** Copy the `when` and `run` values from any
   row, and find that line in the log it names:

   ```bash
   grep '<when value>' logs/<run value>
   ```

   It must match the file's closing `run_summary` line.

5. **The metadata tab was not touched.** Open the test Sheet's
   *File → Version history*. Apart from your own staging edits, the only
   cells the rehearsal changed on the metadata tab should be the four
   identifier columns on the rows it uploaded, plus `ia_sync_hash` /
   `ia_last_synced` on the rows it synced. Anything else there means
   telemetry reached the metadata, and is worth stopping for.

What a manual pass cannot show:

- **`failures` and `unconfirmed` cannot easily be staged by hand.** The first
  needs Internet Archive to genuinely refuse a send; the second needs the
  Sheet's confirm write to fail after an upload succeeded. Both are covered
  by the test suite. Expect neither on a rehearsal.
- **A refused mirror is a one-time check, not a rehearsal step.** To see it
  once, point `upload_log_tab` at another tab of the test Sheet that already
  holds content and rerun step 1: stderr names the tab and its first row,
  nothing is appended to it, and the upload still exits `0`. Put the
  registry back afterward.

## Development

```bash
python -m pytest
python -m ruff check .
python -m pyright
```

Run them from the repository root, which each covers whole; none takes a file
list. `pytest.ini` and `pyrightconfig.json` keep the untracked `data/` and
`logs/` folders out of the search. `pyright` is the command-line engine behind
the Pylance VS Code extension, so it reports what the editor would.
`pyrightconfig.json` pins it to Python 3.10, the oldest supported version.

CI runs the same three commands, plus the upload page's checks, on every
pull request; see [`CI.md`](CI.md).

Tests are pure-offline, and `conftest.py` enforces it. The one exception is
the opt-in e2e rehearsal: tests marked `e2e` and run with `--run-e2e` may
reach the network, since that is how it drives the real Test Sheet and IA's
`test_collection` — see README's
["E2E rehearsal (opt-in)"](../README.md#e2e-rehearsal-opt-in). For such a
test the guard also puts back the proxy settings it strips, and it re-arms
both after the test, so a plain run (no `--run-e2e`) skips them and
stays offline like every other test. From the start of the
run, any lookup of, connection to, or UDP send to a host that is not this
machine is refused the way a real failure would be (`connect_ex` returns
`ECONNREFUSED`). Only `localhost`, loopback and unspecified addresses count as
this machine; `*.localhost` is refused, since many resolvers send it to DNS. A
test that tries it fails at teardown, naming the host, even when the code under
test swallows the error; a test whose body already failed on the refusal is not
reported twice. An attempt in a shared (session- or module-scoped) fixture is
charged to the first test that sets the fixture up, one made while importing a
test module fails collection (even if the module then skips itself), and one
outside every test and collection, such as in a hook, fails the run. Proxy
variables are cleared and `NO_PROXY=*` is set, so a local or Windows system
proxy cannot hide a request.

Tests read an empty `ia` config and no `IA_*` credentials, never the
developer's own. The guard covers the test process only, so a subprocess a test
starts is not guarded, and it inherits `NO_PROXY=*`. It also does not see
asyncio connections on Windows: the default Proactor event loop connects without
calling `socket.connect`, so only its name lookups are guarded.
