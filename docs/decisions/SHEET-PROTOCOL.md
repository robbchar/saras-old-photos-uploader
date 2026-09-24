# The Sheet protocol

Reading the Sheet live, the reserve → upload → confirm ordering, the guards
against a Sheet edited mid-run, and how corrections get back out.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md). Section titles here are cited verbatim
from code comments, so they are stable — if you rename one, grep for it
first.

## The Sheet is read live; the CSV becomes the offline path

*Decided 2026-08-08, before implementation.*

`validate` and `upload` read rows from the Google Sheet over the API rather
than from a hand-exported CSV. Two reasons, in order of weight.

`upload` has to hold a Sheet connection anyway in order to write identifiers
back. Reading from the same place removes the "which copy is current" question
outright instead of adding a dependency to answer it.

And the export step was itself a source of defects. The traps documented in
`CSV-PREPARATION.md` (retired 2026-09-23; see the Fixed entries in
[`KNOWN-ISSUES.md`](../KNOWN-ISSUES.md#fixed)) — above all a comma inside an
unquoted header splitting one column into two and shifting every field after
it
— are artifacts of CSV *parsing*, not of the data. The Sheets API returns
cells
as a grid, so a header containing a comma is just a header containing a comma.
That failure mode is structurally absent on this path.

The CSV path stays for offline and dry-run work and keeps its own header
validation, since the traps are real for anything hand-prepared.

**Reversed 2026-09-23 (#44): the CSV paths are removed.** `upload --csv`,
`sync-metadata --csv` and `validate --csv` are gone, with `--collection`,
`--files-dir`, `--resume-from` and `--from-log`. The Sheet is the only input.
The fallback had become a second pipeline that skipped every Sheet-path
guard: it could upload identifiers the Sheet never recorded, so a later Sheet
run could mint the same permanent number; it took an unchecked collection; it
shipped CSV headers verbatim as IA field names; it resolved files from
anywhere; it did not stop on a rate limit; and `sync-metadata --csv` ignored
`--dry-run`. Every new guard had to be written twice, and one copy had
already been missed. It had not been used since 2026-07-12, and never with
`--live`.

Nothing it did is lost. Dry runs are `upload --dry-run`, `sync-metadata
--dry-run` and `validate`. A rerun resumes from `ia_uploaded`. Corrections go
through the Sheet, and an item with no Sheet row is fixed with the raw `ia`
CLI or the archive.org edit page. During a Google outage, wait.

`validate --csv` went too. It checked the old upload-CSV schema (a
pre-assigned `identifier`, plus `file` and `mediatype` columns), which
nothing consumes once `upload --csv` is gone, and it would reject a CSV
prepared for pasting into the Sheet. Its one unique value, validating with
no network, was not worth keeping a second schema.

One gap was the CSV path's alone to fill: repeated IA fields (`subject[0]`,
`subject[1]`). The Sheet path cannot write them — see
[`KNOWN-ISSUES.md`](../KNOWN-ISSUES.md#6-repeated-ia-fields-cannot-be-written-from-the-sheet) §6. That is not a reason to keep a
CSV path, because hand-editing a file outside the Sheet defeats the source
of truth.

Run logs are still written, as audit records. The tool no longer reads them
back.

## A row's identity is its `file_template` columns, not its `ia_identifier`

*Decided 2026-08-16, after review found the first version of the mid-run-edit
guard was checking its own write.*

The guard's first version compared `ia_identifier` at the target row against
the value the run reserved. That is tautological on the reserve→confirm leg:
reserve had written that exact value at that exact index moments earlier, so
the check could only ever pass. It also ran only before the confirm, leaving
the read→reserve window — which is the *whole run*, since row numbers are
fixed by one initial read and the last chunk's reserve write happens hours
later — entirely unguarded. A row deleted in that window shifts a completed
row up into a target's position, and the reserve write overwrites a permanent
identifier and its live archive.org URL, silently, exit 0.

A row's identity has to be something this tool never writes, or the check is
circular. The `file_template` columns are exactly that: they are the
operator's
own data, the tool only ever reads them, and they are already in memory per
row. So the fingerprint is the row's template candidate, captured from the raw
cells before resolution rewrites them, and it is re-checked before **both**
writes. `ia_identifier` is still checked alongside it — blank for a row about
to be reserved, ours once reserved — because it catches a different thing:
somebody else claiming the row.

The residual gap was two rows whose template columns are identical — the
"two rows resolving to the same photograph" case tracked as issue #1. **Its
consequence was a misattributed write, not a withheld one.** With two
UNASSIGNED rows pointing at the same file and a row deleted above them, the
fingerprints still matched after the shift, so the guard passed: the item
uploaded carrying one row's metadata while the identifier, timestamp, URL
and bib landed on the *other* row, leaving the first unassigned and due to
be re-minted next run. Closed on 2026-08-29 — see "A fingerprint only proves
identity while it is unique" below.

## The Sheet is the correction

*Decided 2026-08-23.*

`validate` and `upload` moved to reading the Sheet live; `sync-metadata` did
not, and was the last command still requiring a hand-made CSV. That left the
round trip the whole "Sheet is the source of truth" premise promises — fix a
description in the Sheet, have it show up on the site — impossible without
exporting a CSV by hand first. Worse, `--csv` was a required POSITIONAL, so
`sync-metadata --project X` did not fail informatively; it printed an argparse
usage error about a missing `csv`.

The Sheet already held everything needed. `upload`'s confirm write records
`ia_uploaded` (this row is done) and `ia_url` (the item it became), and
`ia_url` carries the per-run `zztest-` stamp in test mode. So the Sheet path
needs no log, no CSV, and no stamp arithmetic: read `ia_url`, send that row's
current columns. It handles a Sheet whose rows were uploaded by *different*
runs, under different stamps, without knowing that happened.

Four choices:

- **Scope is `RowState.DONE` and nothing else.** An UNASSIGNED row has no item
  to correct; a RESERVED row's upload never confirmed, and `upload` already
  retries those.
- **Every DONE row is sent, every run.** *Reversed below, "A row pushes only
  when its content changed" — this held for a hand-run command over a few
  hundred rows and stopped holding at ~4,000 rows on an hourly schedule.*
- **A blank cell means "leave this field alone", as on the `--csv` path.**
  Treating the Sheet as literally canonical — blank means delete — is more
  faithful in principle, but an accidental clear, a bad paste, or a row shift
  would silently strip metadata from a permanent public item with no undo.
  `REMOVE_TAG` deletes, deliberately and visibly, and one rule holds across
  both paths.
- **Mode mismatches are refused, not reported afterwards.** The Sheet is
  itself the mode boundary (test and live are different spreadsheets), so an
  `ia_url` pointing the wrong way means the wrong Sheet is in the registry or
  someone pasted across. Sending a live correction to a rehearsal item, or the
  reverse, is not fixed by rerunning.

`sheet_metadata_fields()` is shared with `upload`, so a column that uploads
but does not sync — or the reverse — cannot exist.

**2026-09-23:** with the `--csv` path removed, "as on the `--csv` path" and
"across both paths" above describe one path. The blank-cell rule stands on
its own reason: an accidental clear must never strip metadata from a
permanent item.

## A row pushes only when its content changed

*Decided 2026-09-07, reversing "Every DONE row is sent, every run" above —
issue #24.*

That decision was right for what it was written against: a command a person
ran by hand, over a few hundred rows, whose idempotence Internet Archive
guarantees by answering *no changes to `_meta.xml`* for an item that already
matches. It bought real simplicity — no per-row state, no extra `ia_` column,
no second place for the Sheet and the item to drift apart.

It does not survive either of the two things that changed. At ~4,000 items on
the hourly schedule of issue #27 — now the LaunchAgent that
`./install.sh --project <project> --live --enable-agent` installs, see
[`DEPLOYMENT.md`](../DEPLOYMENT.md#12-enabling-the-hourly-sync) — it is
~4,000 pointless writes an hour. And it
makes the run log useless, which is the worse half: a real edit is
indistinguishable from the background noise, so the log cannot answer the one
question anybody asks it.

So each row now carries a hash of what it last successfully pushed, in an
`ia_sync_hash` column, with `ia_last_synced` beside it for a human to read. A
row is sent only when its current content hashes differently.

Both columns were originally to be hidden, like the tool's other columns.
**Reversed 2026-09-21:** they stay visible with a red background instead. The
red carries the same "don't type here" signal hiding did, while keeping the
two things hiding threw away — `ia_last_synced` is worth glancing at, and
clearing an `ia_sync_hash` cell is the only way to force a row to re-send,
which needs a cell an operator can actually select.

The state lives in the Sheet, not a local file: it survives the machine being
wiped or replaced, and it gives a non-technical operator a recovery lever that
works over the phone.

Four choices went into the shape:

- **The hash is over exactly what would be sent.** `metadata_to_send()` is one
  definition shared by the sender and the hasher, so they cannot disagree
  about what a row means. A hash over anything else either re-pushes a row
  forever or silently swallows an edit. Being derived from
  `sheet_metadata_fields()`, it excludes the six `ia_` columns and the
  `(LCPS Internal)` ones automatically.
- **The hash is captured at read time and stamped unchanged.** Identity is
  checked late — the moved-row guard runs against a fresh read before the
  stamp write — but content is captured early. Re-reading the row at write
  time would stamp an edit made mid-run as already-synced, and that edit would
  be lost permanently with nothing to notice it.
- **Only a successful push stamps, and Internet Archive's `unchanged` counts
  as one.** A failure leaves the cell untouched so the row retries. But
  "no changes to `_meta.xml`" means the item already matches the Sheet — a
  successful reconciliation. Treating it as a non-success would leave exactly
  the rows this gating exists to quiet re-pushing every hour, forever.
- **One stamp batch per chunk, not per run and not per row.** The Mac this
  runs on sleeps and shuts down unpredictably, including mid-run. A single
  end-of-run batch stamps nothing when the run is killed; a per-row write
  exceeds the Sheets API's 60 writes/minute/user. Per chunk is one request per
  chunk and costs a kill at most one chunk's stamps.

Failure modes are deliberately safe. Deleting the column, clearing cells, or
pasting over them causes at worst a spurious re-sync, which Internet Archive
reports as unchanged. The only way to cause a *missed* sync is to type the
exact current hash of a row you just edited — which is why the operator rule
is to **clear** `ia_sync_hash`, never to type into it.

Two levers follow, both phone-instruction sized: clear one row's
`ia_sync_hash` to re-sync that row; clear the column to re-sync everything.

`sync-metadata` refuses to run without both columns, in test mode as well as
live. The alternative — falling back to pushing everything — makes the failure
this feature exists to remove into its own silent fallback state, and under an
unattended schedule nothing would ever fail to say so.

## `sync-metadata --csv` reads its targets from the upload log

*Decided 2026-08-23, closing a defect introduced by the per-run stamp above.*

"Test identifiers carry a per-run stamp" solved rehearsal collisions on
`upload` and silently broke `sync-metadata`, which nothing noticed because
that command had never been run.

`cmd_sync_metadata` derived its target the same way `upload` does, by calling
`effective_identifier()` with `run_stamp()`. But a stamp is unique to the
invocation, so a correction run computed `zztest-<today's stamp>-<identifier>`
for an item created under *yesterday's* stamp — an identifier that has never
existed. Every row would have failed, with a message about the item not being
found rather than about the real cause. Test-mode `sync-metadata` was
unusable, which left `--live` as the only way to exercise it: the worst
possible place to run something for the first time.

The mapping already existed. Every upload log line records both `identifier`
(the real, permanent one) and `uploaded_as` (what actually went over the
wire), precisely so a later reader can tell what landed where. `--from-log`
points at that log and `load_uploaded_as()` reads the pairs out of it.

Three choices went into the shape:

- **Required in test mode, optional with `--live`.** Live identifiers are
  unstamped, so there is nothing to look up. In test mode there is no value
  the CSV could carry that would work — the operator is told never to author a
  `zztest-` identifier by hand, and `check_identifier` would reject one
  anyway — so refusing is the only honest answer.
- **A miss is an error, never a fall back.** Recomputing the target when the
  log does not name a row is exactly the bug being fixed, and it fails
  silently. Rows the log does not record are reported before anything is sent,
  all-or-nothing like the rest of the CSV path.
- **Mode-filtered, like `--resume-from`.** A test log records where a
  `zztest-` item went and says nothing about the real one. Both flags read
  logs through the same `_read_log_results()`, which drops entries from the
  other mode and tolerates a truncated line.

`--from-log` is deliberately separate from `--resume-from` rather than folded
into it: they read the same file for opposite purposes. `--resume-from` says
which rows to SKIP; `--from-log` says where the rows that remain should be
SENT. A single flag doing both would make "skip what is done" and "correct
what is done" the same instruction, which they are not.

**Retired 2026-09-23 (#44).** `sync-metadata --csv`, `--from-log`,
`load_uploaded_as()` and `check_uploaded_as()` are removed. The Sheet path
never needed them: `ia_url` records the exact item each row became, stamp
included (see "The Sheet is the correction" above). Upload logs still carry
`uploaded_as`, for a human reading them.

## `--resume-from` filters on run mode

A test-mode success and a live-mode success are indistinguishable by
identifier — only `uploaded_as` differs. Without a mode check,
`--live --resume-from <test-log>` would skip every row as "already done" and
report a successful live run that uploaded nothing.

So `load_prior_successes()` matches on the log's `live` field. Log lines
written before that field existed record no mode and match **neither**, so old
logs simply never skip anything rather than skipping in the wrong direction.

**Retired 2026-09-23 (#44).** `--resume-from` and `load_prior_successes()`
are removed. `ia_uploaded` is the record of what is done, and the test and
live Sheets are separate spreadsheets, so a test success can never mark a
live row done.

## A fingerprint only proves identity while it is unique

*Decided 2026-08-29, closing issue #1 — the misattributed-write gap the
"row's identity is its `file_template` columns" note above deferred.*

The guard's fingerprint says "the row at this position still describes the
same photograph". That inference silently assumes no *other* row carries the
same fingerprint: with two rows resolving to one file, a row shift leaves a
matching fingerprint at the target's position while the physical row
underneath is a different one, and the write-back lands on the wrong row —
the item uploads under one row's metadata, the identifier and URL are
recorded on the other, and the first row stays unassigned and is minted a
*second* permanent identifier next run.

Issue #1's fix direction asked for a row identity that does not depend on
`file_template` being unique. Every candidate for such a key fails the
section-above constraint that identity must be something this tool never
writes (a hidden key column is the tool's own write; a wider fingerprint
over the metadata columns just moves the same collision one duplicate-row
away). So instead of finding a key that survives duplicates, duplicates
themselves are refused — which they deserve on their own merits, since two
rows claiming one photograph is exactly the "duplicate row mints a second
permanent identifier" case `reconcile-files` already refuses to create. Two
layers, because duplicates have two ways in:

- **Present at the initial read: refused by `resolve_sheet_files()`.** Rows
  resolving to the same file are all errors — every row in the group, not
  all-but-the-first, because the tool usually cannot know which row is the
  wrong one and flagging the rest would silently elect a winner. It *can*
  know in one case: a group holding exactly one row that has already
  uploaded. That row's identifier is permanent and its row is the only link
  between the identifier and its metadata — including the `ia_url`
  `sync-metadata` reads to find its targets — so it is named as the one to
  keep and only the others are offered for deletion. (Two uploaded rows in
  one group is a worse problem than this check can adjudicate, so it falls
  back to the symmetrical wording.) Keyed on
  `claim_key()` of the *resolved* path, the same key the reconcile survey
  uses: the resolver is deliberately forgiving (case, extension), so two
  rows can spell one disk file differently and a raw-cell comparison would
  miss them. Shared by `validate` and `upload`, so the pair shows up as
  blocked rows long before a live run.
- **Introduced mid-run: the guard distrusts a duplicated fingerprint.**
  `split_moved_targets()` counts each fingerprint across the fresh read; a
  target whose fingerprint appears on more than one row is filed as moved —
  the safe direction, skip and report — because position plus a non-unique
  fingerprint proves nothing. It goes out on a rerun once the Sheet is
  untangled.

  **On the reserve leg only.** After reserve, the target's row carries this
  run's own number, and `check_claimed_identifiers()` has already proved
  that number unique across the whole Sheet — so the `ia_identifier`
  comparison is by itself a complete proof of identity, since a shift puts a
  row that does *not* carry our number underneath. Applying the ambiguity
  veto to the confirm leg as well only produced false positives, and the
  cheapest of them was expensive: a volunteer appending a duplicate row
  (which shifts nothing) between the upload and the confirm write left a
  live Internet Archive item recorded nowhere, with the next run's own
  duplicate refusal then blocking the very row that needed finishing.

Together the original inference is made sound: a write proceeds only when
the fingerprint at the target's position matches, was unique in the initial
read (validation — a duplicate there never becomes a target), is unique in
the fresh read *or* the row already holds this run's proven-unique number
(guard), and the `ia_identifier` cell holds exactly what the protocol step
expects. A pure row shift can no longer satisfy all of that against the
wrong physical row.

## The Sheet's log tabs are telemetry, never an input

Every real run mirrors its own summary into a tab of the spreadsheet it is
already talking to: `upload` into `upload_log_tab`, `sync-metadata` into
`sync_log_tab`, both named in the project's registry entry.

The point is **remote diagnosis**. When someone calls about a problem months
from now, the Sheet can be opened from anywhere — no SSH, no screen sharing,
no talking a volunteer through Terminal to find a JSONL file on a Mac in an
office two hours away.

**The direction never reverses.** Nothing in a log tab is ever read back
into the canonical metadata columns; `Sheet → Internet Archive` stays
one-directional. That is enforced by construction rather than by care:

- The writer is handed an `AppendOnlyTab` (`SheetClient.append_only_tab()`),
  a type with `ensure_tab` and `append_rows` and nothing else. Handing it a
  `SheetClient` would carry `write_cells` along with it, and "it could
  overwrite the metadata columns but does not" is a promise; a type without
  the method is a property.
- A `upload_log_tab` or `sync_log_tab` equal to `sheet_tab` is refused at
  config load. It is the one configuration mistake with a permanent cost —
  run summaries appended onto the photographs — and by the time a run is
  appending it is far too late to catch.
- Any *other* existing tab is caught at write time instead: if the tab is
  already there and its first row is neither empty nor the log header,
  `ensure_tab()` refuses. Configuration can only know about `sheet_tab`; a
  name mistyped as some other real tab — an archived copy of the metadata, a
  donor's notes — would otherwise collect telemetry underneath it, quietly,
  on every run.

**A tab each, not one shared.** An hourly sync and a once-a-week upload
interleaved in one tab would bury the upload rows someone opened the Sheet
to find. The tab name already says which command wrote the row, so there is
no `command` column.

**A row per run, plus a row per problem.** A clean run is one row. Mirroring
every per-file record would put 10,000 rows in a tab whose entire value is
that a person can scan it; the per-file detail is not lost, and the `run`
column names the JSONL to go and read for it. The columns are:

| column | holds |
| --- | --- |
| `when` | the summary record's UTC timestamp |
| `run` | the run's own log file name, e.g. `upload-20260917T180211Z.jsonl` |
| `outcome` | `summary`, `failure`, `unconfirmed` or `skipped` |
| `identifier` | the row's permanent identifier — blank on the `summary` row |
| `detail` | the run's closing console line, or that problem's own error |

The rows are rendered from `summary.as_record(live)` — the very object
written to the JSONL a line earlier — so the tab and the file cannot
disagree about what happened.

**A quiet sync run is not mirrored.** An hourly sync's steady state is
"every row already matches its last push": it sends nothing and finds
nothing wrong. A row an hour is roughly 9,000 a year, burying the handful
that report an actual problem. Such a run is still whole in its own JSONL,
and per-row *did my edit land* is already answered by `ia_last_synced` in
the Sheet itself. A run that pushed nothing **because everything failed**
still lands in the tab — that is precisely what someone opens the Sheet to
find. `upload` needs no equivalent rule: a run with nothing to upload
returns before a log is opened at all.

**A failed mirror can never fail the run.** By the time it is written, files
are on Internet Archive under permanent identifiers and the Sheet has
already been updated. A Sheets hiccup while writing *telemetry* that turned
a successful upload into a failed one would invite a rerun — and a rerun is
what mints a second identifier for a photograph that already has one. The
failure is reported on stderr, naming the JSONL that is still on disk.

**Off unless configured.** A registry entry with neither key does no read,
no create and no append. There is no default tab name, because a default
would have a run create a tab in a Sheet whose owner never asked for one.

The tab is ensured on *every* run rather than once at setup. It costs a tab
listing and a one-row read against a spreadsheet the run is already talking
to, and it means a tab someone deletes or renames repairs itself on the next
run rather than silently swallowing every run after it. A tab an operator
creates by hand before the first run is adopted and given its header, so it
ends up identical to one this created.

## The rehearsal reset is a hand edit, not a command

*Decided 2026-09-19 — issue #37.*

A row that has been rehearsed with `--write-identifier` is `DONE` forever,
and rehearsing it again means clearing four cells in the test Sheet by hand.
A `reset-test-sheet` subcommand was considered to do that clearing, and
rejected.

The work it saves is four cell deletions in a browser, done rarely, by the
one person who rehearses. What it would cost is a command whose entire job
is to destroy the tool's own record of what it uploaded — which needs its
own guards, its own tests, and a refusal path proving it cannot be aimed at
the real Sheet. `ProjectConfig.sheet_id_for()` already keeps *runs* off the
real Sheet in test mode, but a reset command is a new way in, and the real
Sheet is one config value away from the test one.

The deletion being manual is also the point. In the real Sheet those four
cells are the only local record that an IA item exists; clearing them tells
the next run to mint a second permanent identifier for a photograph that is
already uploaded. That is not an action worth making convenient. It is
written down instead — see `docs/OPERATIONS.md`, §2, "Re-rehearsing a row
that is already done".

This does not generalize to the `sync-metadata` reset, which is one cell
(`ia_sync_hash`), is safe by construction — a wrongly cleared row re-sends
and IA reports it `unchanged` — and needs no command either.

**Amended 2026-09-23:** the e2e rehearsal automates this reset for the Test
Sheet, in test code behind a guard — see "Test data is ephemeral". The hand
edit above is still the recipe for a manual rehearsal, and there is still no
reset command.

## Test data is ephemeral

*Decided 2026-09-23.*

The Test Sheet and every `test_collection` item may be wiped and rebuilt at
any time. Anyone rehearsing against them should expect it. The e2e rehearsal
(`test_e2e_rehearsal.py`) does exactly that on every run: it rewrites the
Test Sheet's data tab from `e2e_fixtures/sheet.json` and deletes its log tabs.

Ephemeral means disposable, not reusable. Internet Archive never releases an
identifier, so a rehearsal still mints fresh `zztest-<stamp>-…` names; see
"Test identifiers carry a per-run stamp".

The rewrite lives in `e2e_sheet.py`, test code only; `ia_bulk.py` has no
reset command. Every write goes through `check_reset_allowed`, which refuses
unless the e2e registry's own `sheet_id` is a `REPLACE_WITH…` placeholder and
its `test_sheet_id` is no project's live `sheet_id` in
`projects_registry.json`. It compares against live IDs only: sharing the Test
Sheet with `sarasoldphotos`'s `test_sheet_id` is intended. It also refuses a
placeholder `test_sheet_id`, and a log tab named like the data tab, since the
reset deletes the log tabs.

The live-ID check is empty until a real `sheet_id` is registered, so the reset
also looks at the Sheet itself: it refuses to clear a data tab with more rows
than the fixture. A real Sheet has thousands of rows.
