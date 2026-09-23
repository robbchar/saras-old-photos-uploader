# LCPS Archive Upload Project

Uploading ~10,000 historical Astoria photos (donated collection) Lower Columbia Preservation Society (LCPS), a
nonprofit, these are going to be put into the LCPS collection on Internet Archive. A second LCPS project may reuse this same pipeline later,
so keep things generic to "a project" rather than hardcoded to photos.

## Identifier scheme
`COLLECTIONKEY-PROJECTID-NUMBER` — all lowercase, hyphen-separated.
- COLLECTIONKEY: LCPS's IA collection identifier (confirm before real runs)
- PROJECTID: short project code, e.g. `photosexample` (illustrative only —
  see `projects_registry.json` for the actual registered codes; tracked in
  a small project registry, not invented ad hoc per script run)
- NUMBER: 5-digit zero-padded sequential number, unique per project
Identifiers are permanent once uploaded — never reused, never renamed.
Original filenames/donor folder structure are NOT part of the identifier;
they go in the `identifier-bib` metadata field instead.

## Tooling
- `ia` CLI (internetarchive Python package), authenticated via `ia configure`
  against the shared org account `admin@lcpsociety.org` — no env vars, no
  per-user credentials.
- Upload: `ia_bulk.py upload` reads the Sheet and uploads through the
  `internetarchive` library. Every item is sent with a `mediatype` — it is
  NOT optional, defaults to `data` and can't be changed after upload if
  omitted.
- Metadata updates: `ia_bulk.py sync-metadata` pushes Sheet edits to
  already-uploaded items — fully decoupled from upload, safe to run
  repeatedly.
- IA batch limits: 500 items per upload run, 5000/day — `ia_bulk.py upload`
  chunks by 500 and refuses a run over 5000 unless
  `--allow-over-daily-cap` is passed; never submit the full set in one call.
- Testing: `ia_bulk.py` targets `collection:test_collection` (IA's sandbox,
  auto-expires ~30 days) by default, and automatically prepends
  `zztest-<run's stamp>-` to the real identifier for every network call
  unless `--live` is passed. The stamp is unique per invocation (one stamp
  per run, shared by every row it touches), so a rehearsal never collides
  with a previous rehearsal's items — see `docs/DECISIONS.md`, "Test
  identifiers carry a per-run stamp". The Sheet's `ia_identifier` column
  always holds real, permanent identifiers — never author a `zztest-`
  identifier by hand in it.

## Source of truth
Canonical metadata lives in a Google Sheet (replacing the old emailed-CSV
workflow). Every `ia_bulk.py` command reads the Sheet live over the Sheets
API; there is no CSV export step, and `ia_bulk.py` takes no CSV input (the
`--csv` paths were removed 2026-09-23).

## temp/memory/your files
Any file that is used only locally, that should not be part of the project, should be written to the .ignored/ directory. That includes any memory files, temp files (such as scripts or test files), and really anything that needs to be written to disk but is not part of the project.

## Memory
Anything that is relevant to how this application works and the history/derivation of how it works should be saved as memory. Put that in a memory directory and read from it when needed and update things periodically.