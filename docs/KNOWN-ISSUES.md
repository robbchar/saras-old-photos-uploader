# Known Issues

Verified against the code and the real data in `data/` as of 2026-08-08. Each
entry was reproduced, not inferred. Ordered by how much damage it can do to a
`--live` run.

These are the open ones. Issues fixed since this file was written are recorded
at the bottom under [Fixed](#fixed), so the reasoning survives. Numbers are
never reused, so a fixed entry leaves a gap (#3 and #4).

## 1. `noindex` cannot be changed by `sync-metadata`

**Severity: low — fails loudly.**

IA treats `noindex` as read-only after upload:

```
400 {"success":false,"error":"Can't modify read-only field 'noindex'"}
```

Recorded against items `00001`–`00005` in the 2026-07-08 sync logs. `noindex`
*can* be set at upload time (a `noindex` column in the Sheet uploads like any
other field), but it must be right the first time.

**Implication:** decide the `noindex` policy for the collection before the live
upload, not after.

## 2. Batch pacing is manual, though the daily total is enforced

**Severity: low. Daily cap enforced 2026-08-23; pacing still manual.**

`chunk_rows()` groups rows into 500s to match IA's per-run limit, but the loop
still has no pacing (no sleep between batches) and no running counter *across*
a day's separate runs — a second run started the same day does not know what
the first one spent. What a single run can no longer do is exceed the
5,000/day cap by itself: `upload` refuses to start such a run and names the
fix (`--limit`), with `--allow-over-daily-cap` as the explicit override.
Beyond that: `upload --limit N`
now caps how many items a single invocation uploads at all (an operator can
size a day's runs by hand with a number the tool enforces), `--chunk-size` makes the 500-per-run batch size an
overridable flag instead of a constant, and a detected rate-limit response
now stops a run cleanly instead of grinding through the rest of the batch as
unexplained failures — though that detector is unverified against a real
response; see `DECISIONS.md`, "Rate-limit detection matches a status
code...".

**Mitigation today:** pace `--limit` across the day's runs by hand — the tool
enforces the cap per run, not per day; see
[`OPERATIONS.md`](OPERATIONS.md#pacing-and-batch-limits).

Tracked as its own, non-blocking issue: **#75**. Whatever counts a day's
uploads must look in both places a run can log to: a terminal run's
`logs/upload-*.jsonl` and a page run's
`logs/page-runs/<UTC>/upload-*.jsonl` — the local upload page (`ia_bulk.py
serve`) spends the same daily quota through the same account, just logged
under its own per-run folder. See
[`ARCHITECTURE.md`, "`serve` and the upload page"](ARCHITECTURE.md#serve-and-the-upload-page).

## 5. `check_file_exists`'s `is_file()` catch has no test for the case it exists for

*Found 2026-08-22, during the row-readiness effort — pre-existing, not
introduced by it. Reasons corrected 2026-08-22 after the claims below were
checked by running them; the conclusion is unchanged.*

**Severity: latent — tested generically, untested for its actual purpose.**

`validate_rows`' redundant `is_file()` check (`ia_bulk.py`, inside the
`if check_file_exists:` block) is documented, in this project's own build
history, as load-bearing: it is what catches an internal space in a
multi-segment folder cell that `resolve()` normalizes away, when the Sheet
path's earlier file resolution and this later disk re-check disagree. It
looks like a purely decorative, redundant safety net and is not — the comment
above `SHEET_REQUIRED_COLUMNS` already warns that this is "a completely
different mechanism" from the required-columns check nearby and that removing
it "is a different (and wrong) change" from anything that constant's own
shrink calls for.

The block is **not** untested in general.
`test_validate_sheet_rows_flags_a_file_missing_from_disk` drives it through
`validate_sheet_rows` with a genuine on-disk mismatch and asserts the
`"file not found"` message. A refactor that removed the call would not go
green. (Until 2026-09-23 the tests that pinned it all ran through the CSV
path; they went with that path, and this Sheet-path test replaced them.)

What *is* uncovered is narrower, and it is the scenario that makes the check
load-bearing rather than merely redundant: an internal space in a
multi-segment folder cell that makes the earlier file resolution and this
later disk re-check disagree. The existing test is a generic missing file,
which the resolver alone would already have caught in a real run. So the
check's *ordinary* behavior is tested; its *reason for existing* is not.

An earlier version of this entry claimed nothing referenced `check_file_exists`
or asserted that message at all, and that deleting the `is_file()` call "would
pass the entire suite green". Both were wrong, and the irony is worth stating
plainly, because the paragraph directly below warns about exactly this: this
entry reached a right conclusion — the check deserves a test aimed at the case
it was written for — by a route nobody had run. It has now been run, and the
conclusion survives on better grounds.

The warning below is unchanged and still stands. Earlier in the same
row-readiness effort, a planning document confidently instructed an
implementer working directly beside this check to "run the existing test that
covers the internal-space case" — stated as settled fact. No test covers that
case; the implementer caught the false premise and said so rather than
fabricating coverage. A doc asserting a right conclusion ("don't touch this
check") for a wrong reason ("here is the test proving it's safe") is not safe
to leave standing, because the next reader has no way to tell the reason was
never checked.

Reproducing the underlying scenario is likely platform-specific: the
originally observed disagreement was Windows-specific (`Path.is_dir()`
normalizing away a trailing space that `Path.iterdir()` on the same path does
not), so a fixture built on a different OS may not reproduce it at all — which
is itself part of why no test exists yet.

**Mitigation today:** none — this is a test-coverage gap, not a behavior
change. Recorded here so it is not lost the next time this file is reviewed.

## 6. Repeated IA fields cannot be written from the Sheet

**Severity: low — no current need, but it silently flattens.**

*Found 2026-09-23, while removing the CSV paths.* Internet Archive stores a
repeated field (several `subject` values, say) under indexed keys:
`subject[0]`, `subject[1]`. Only the CSV paths could write those. From the
Sheet, `normalize_header()` strips the brackets (`subject[0]` becomes
`subject0`), and a cell like `a; b` ships as one string, not two values. The
last CSV sync (2026-07-12) edited a repeated `colors` field on a test item.

It matters if LCPS wants several subject or name values per item. The export
already has `Subject Terms (Controlled Vocab)` and `Names (Last, First M.)`,
which are naturally multi-valued.

**Mitigation today:** the raw `ia` CLI —
`ia metadata <identifier> --modify 'subject[1]:...'` — or the item's
archive.org edit page. Neither is recorded in the Sheet.

## Fixed

### `--collection` was unvalidated on `--live` (was #3)

*Fixed 2026-09-23 by removing the CSV paths (#44).* `--collection` was used
as typed and never checked against the registry or against IA, so a wrong
value pushed real files into the wrong collection and reported success. By
then its `"lcps"` default was already gone (2026-08-08) and passing it on the
Sheet path was an error; it survived only on `upload --csv`. With that path
removed the flag is gone, and the registry's `ia_collection` is the only
source. See [`DECISIONS.md`](decisions/FOUNDATIONS.md#technical-configuration-lives-in-the-registry-not-the-command-line).

The related `collection_key` question was settled 2026-08-23 — see
[`DECISIONS.md`](DECISIONS.md#still-open).

### `--files-dir` did not constrain path resolution (was #4)

*Fixed 2026-09-23 by removing the CSV paths (#44).* On the `--csv` paths,
`Path(files_dir) / row["file"]` would resolve `../` or an absolute path
outside the intended directory. The Sheet path never had this problem after
2026-08-23: `resolve_file()` treats `files_dir` as a hard boundary, refusing
anything that resolves outside it and any blank folder cell. With the CSV
paths gone, `--files-dir` is gone too, and every file is resolved that way.

### No retry or backoff on transient network failures

*Fixed 2026-09-02, closing issue #5.* A transient failure talking to
archive.org failed that row for the whole run — a `ReadTimeout` with nothing
wrong with the data, the Sheet or the file. Recovery was correct but manual:
the row was never marked done, so the operator re-ran to chase a flake.

`retry_ia_call()` now wraps the network call inside `upload_row()` and
`update_metadata_row()`: three attempts, backing off ~2s then ~4s with jitter.
It retries connection errors, timeouts and 500/502/504, and does **not** retry
refusals (403, 400, any 4xx) or 429/503 — those still stop the run so the
operator resumes later. Reading `internetarchive` 5.10.1's source showed
the real gap was the S3 file transfer, which had no retry of any kind, rather
than the metadata read, which already had three. See
[`DECISIONS.md`](decisions/QUOTA-AND-RUNS.md#retry-covers-transport-failures-never-refusals).

The same change closed a second, quieter gap: a rate limit arriving on the
metadata GET *inside* `internetarchive.upload()` used to reach the tool with
no status at all, so it read as an ordinary failure and the run ground on
repeating it. `IA_RETRY` and a `__context__` walk recover the real status
without reading message text. See
[`DECISIONS.md`](decisions/QUOTA-AND-RUNS.md#a-status-the-metadata-call-strips-is-recovered-still-without-reading-text).

Re-running is still the recovery for a row that fails all three attempts: it
has no `ia_uploaded`, so the next run retries it.

### `validate` passed CSVs whose metadata was silently misaligned

*Fixed 2026-08-08.* `validate` inspected four columns and forwarded everything
else to IA unexamined, so a malformed header row shifted every later column by
a position and still reported all rows passing. This was live in the repo's own
`data/upload.csv`: `Photographs` uploaded as `Description`, `Sara Meyer` as
`Photographer / Studio`, and `600 Marine Dr. (1960)` as `Construction Date` —
each one column off, caused by the unquoted comma in `Names (Last, First M.)`
splitting one header cell into two.

`check_row_shape()` now rejects any row whose field count disagrees with the
header (`csv.DictReader` exposes surplus fields under the `None` restkey and
missing ones as `None` values), and `check_header()` rejects headers with
surrounding whitespace, duplicates, and case variants of the columns the script
reads by name. Header problems are reported as row 1. `data/upload.csv` was
corrected in the same change.

### A row with more fields than the header crashed that row

*Fixed 2026-08-08 by the same `check_row_shape()` check.* Surplus fields landed
in a list under the `None` key and `upload_row`'s metadata comprehension called
`.strip()` on it — `AttributeError: 'list' object has no attribute 'strip'`,
visible in three real logs (`upload-20260708T131606`, `-20260708T132619`,
`-20260712T122426`). The mirror case (a *short* row, giving `None` values) had
been papered over by the `(value or "")` guards in `a0944be`, which stopped the
crash without noticing the misalignment behind it. Both are now caught before
any network call.

*2026-09-23:* both CSV entries above are history. `check_header()` and
`check_row_shape()` were removed with the CSV paths (#44). The Sheet path's
`check_column_map()` and `check_grid_shape()` cover the same ground for a
grid, where an unquoted comma cannot split a header.
