# Foundations

The choices everything else rests on: what talks to Internet Archive, what is
configuration, how the Sheet is reached, and what was accepted as a known
limit rather than missed.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md). Section titles here are cited verbatim
from code comments, so they are stable — if you rename one, grep for it
first.

## Use the `internetarchive` Python library, not `ia upload --spreadsheet`

The `ia` CLI can take a spreadsheet directly, which was the original plan. It
was dropped because a CLI invocation gives you one exit code for the whole
batch — you cannot tell which of 500 rows failed, or resume from the failure.
Driving the library row by row means every row produces its own logged
outcome,
which is what makes `--resume-from` possible.

Cost: the tool re-implements chunking and progress reporting that the CLI
would
have handled.

**2026-09-23:** `--resume-from` was removed with the CSV paths (#44). The
per-row outcome now feeds the per-row confirm write (`ia_uploaded`,
`ia_url`), which is what lets a rerun resume by itself, and the audit log.

## Generic to "a project", not hardcoded to photos

A second LCPS project is expected to reuse this pipeline, which is why the
registry has a `projects` map rather than a single hardcoded code, and why the
docs say "items" more than "photos".

## Technical configuration lives in the registry, not the command line

*Decided 2026-08-08, reversing the "accepted, not overlooked" item below.*

The target IA collection, the files directory, and the template that builds a
row's file path all move into the per-project block in
`projects_registry.json`. The command line keeps only what genuinely varies per
run: `--project`, `--live`, `--limit`.

The reversal is specifically about `--collection`. Leaving it as an
unvalidated
flag defaulting to `"lcps"` was defensible when the registry was barely used;
it is not defensible once the tool already reads a per-project registry block
to find the Sheet. A wrong `--collection` on a `--live` run pushes real files
into the wrong collection and reports success — and unlike `collection_key`,
nothing catches it. As a registry value it is confirmed once, in version
control, per project, instead of retyped correctly on every run forever.

The same reasoning puts `files_dir` and `file_template` there. A row's file
path is assembled from a root plus one or more Sheet columns, which is
plumbing; the people maintaining the Sheet should never have to think about
it.

**Completed 2026-09-23 (#44).** `--collection` and `--files-dir` had survived
as overrides on the `--csv` path, where `--collection` was still used as typed
and never checked against the registry. They were removed with that path.
The registry is now the only source of the collection and the files
directory, with no flag to override either.

## A live upload goes only into a collection archive.org confirms

*Decided 2026-09-29.*

A live upload sends items to the project's `ia_collection` from the
registry. Before this change, nothing checked that value against Internet
Archive. If a second project's entry had a typo, real files would go up
under permanent, unrenameable identifiers into a collection that does not
exist. The only guard was a person reading the value.

`upload --live` now reads the collection's metadata from archive.org before
it reads the Sheet. It exits 1 unless the item exists, its `mediatype` is
`collection`, and archive.org's answer is for exactly the string the upload
will send. The check and every upload take that string from one place,
`ProjectConfig.ia_collection_for(live)`.

- **Every unconfirmed result is refused.** That covers a missing item, an
  item that is not a collection, and a read that gave no verdict. A read
  gives no verdict when archive.org is unreachable, times out or answers
  with an error status. It also gives none when the answer lacks the item's
  metadata (`{"error": ...}`, or a sub-path's answer when `ia_collection`
  holds a `/`), or is for another identifier (archive.org answers `x/`,
  `./x` and `x?` with `x`'s item). It is refused anyway, because identifiers
  are permanent and a re-run costs nothing. The refusal names the parsed
  HTTP status, the exception's class when there is no status, or what was
  wrong with the answer, never an exception's message text (see "Rate-limit
  detection uses a parsed status code, never message text"). There is no
  override flag.
- **The refusal says whether waiting helps.** A 429, a 5xx or a failure with
  no status at all says to run it again later. A 4xx other than 429, or an
  answer that is not the item, will come back the same, so it points at
  `ia_collection` and the account's `ia configure` credentials instead.
- **It runs after the local flag checks and before the Sheet read.** A bad
  `--limit` or `--chunk-size`, or a blank `--batch`, still fails without a
  network call. A bad collection fails before the Sheet is read and before
  the run's log opens.
- **It says what it is waiting for.** It prints `asking archive.org whether
  Internet Archive collection '<name>' exists...` before the request, which
  retries like every other Internet Archive call and can take minutes while
  archive.org throttles. A Ctrl-C while it waits, or the page's Stop on the
  Mac, stops the run at once, as one during the Sheet read does (see "An
  interrupt stops a run after the current item"), with one line instead of a
  traceback.
- **The dry run runs it too.** `upload --live --dry-run` is the pre-live
  check, and it prints `Internet Archive collection '<name>' confirmed on
  archive.org` when the check passes. The upload page never runs a dry run.
- **Test mode skips it.** A test run uploads into `test_collection`, IA's
  sandbox, which no registry value controls.
- **Only `upload` runs it.** `validate` does not: it checks rows, and the
  upload page runs `validate` on every load. `doctor` could report the
  collection, but does not yet.
- **Some mistakes still pass.** A real collection that is the wrong one
  passes: the parent `lcpsdigitalcollection` would pass where
  `sarasoldphotos` is meant. So does a collection the org account may not
  add items to. So the registry value is still read by hand once, before a
  project's first live run
  ([`OPERATIONS.md`](../OPERATIONS.md#pre-live-checklist)).

The read uses the same retry policy as every other Internet Archive call
(`IA_HTTP_ADAPTER_KWARGS`). The metadata endpoint answers an unknown
identifier with an empty JSON object, `{}`, not a 404. `internetarchive`
5.11.1 reports that as `item.exists` being false. The opt-in e2e run
(`--run-e2e`) pins both answers the verdicts rest on against archive.org
itself: `test_collection` is confirmed, and an identifier nobody holds is
missing.

## The Sheet is reached as a service account, not as a person

**Settled 2026-09-18, reversing 2026-08-08.** Every Sheet read and write
authenticates with a Google Cloud service account whose JSON key lives at
`.ignored/google-service-account.json`. The Sheet is shared with the service
account's `...iam.gserviceaccount.com` address as Editor.

The OAuth user token it replaces fails an unattended machine in two ways.
Recovering an expired or revoked token needs a browser sign-in on that
machine, and the token belonged to a human account (`tools@`) that a
Workspace cleanup or password change could break without warning.

The 2026-08-08 reason for ruling a service account out was that it loses
per-person attribution in the Sheet's edit history. That attribution never
existed: every run authorized as the shared `tools@` account, so the history
named no one. Per-person attribution is also not a goal. LCPS is run by
volunteers, and anyone with access to the LCPS Mac in the building may run the
pipeline. Pipeline edits are attributed to the service account, and that is
accepted.

The key is loaded and a token fetched before any Sheet work starts. A missing,
unreadable, deleted or disabled key stops the run with a message saying which,
and a network failure at that point is reported as a network failure, never as
a credential problem. There is no fallback to OAuth.

The accepted cost: the key never expires and sits in a file on a shared
machine, so its reach is only the Sheets it has been shared with, not the
whole Cloud project. Replacing it is described in
[`OPERATIONS.md`](../OPERATIONS.md). Where it lives on the LCPS Mac and its
file permissions are settled by the Mac deployment work (issue #39), not
here.

An API key stays ruled out: it is read-only and reaches only publicly shared
Sheets.

## One version for the whole tool, kept on the Python side

*Decided 2026-09-29.* The version is `APP_VERSION` in `app_version.py`,
starting at `1.0.0` because the tool was already running live. Since
2026-09-30 `app_version.py` reads it from `version.txt`, which only the
release PR changes; see
[`CI-AND-RELEASES.md`](CI-AND-RELEASES.md#the-version-lives-in-versiontxt-and-changes-only-in-the-release-pr).
The page gets it from `GET /api/status`, not from its bundle.

Keeping it in `upload_page/package.json` was ruled out. `package.json` is a
build-stamp input, so every bump would force a rebuild and a committed `dist/`,
even for a Python-only fix. It would also version only the page when the page,
server and CLI ship as one checkout. `package.json` has no `version` field, so
there is only one version to read.

The commit SHA is already on `GET /api/health`, and `doctor` shows it next to
the version, so a version can be told apart from later untagged commits on
`main`. The upload page shows the version alone, pinned to its bottom-right
corner.

`setup` records the version it ran against in `logs/installed-version` and
prints `updating from X to Y` when the next run's version differs
(`downgrading from X to Y` when it went back, such as a checkout of an older
tag after a bad release). An upgrade
is `git pull && ./install.sh`, so by the time `setup` runs the old checkout is
gone; this marker is the only record of what came before. The line fires only
on a version change, never on a commit change alone. Merging any pending
release PR before updating the Mac puts every `feat`, `fix`, `perf`, `revert`
and breaking change under a new version. `refactor`, `build` and `chore`
changes, Dependabot's dependency bumps among them, release nothing by their
type and can reach the Mac under an unchanged version: the line reports releases, not every
change, and the commit printed beside the version tells those apart. The
marker is written on every run that gets past the refusals and reads the
registry, whatever the checks say, because the pull has already swapped the
code. It is not written when the registry can't be read: the operator fixes
the registry and re-runs `setup`, and that run still has to name the upgrade.

## Accepted, not overlooked

The final build review named these and chose to leave them. They are recorded
in [`KNOWN-ISSUES.md`](../KNOWN-ISSUES.md) with reproduction details:

- Chunking has no real pacing or checkpoint behavior
- ~~`--files-dir` does not constrain path resolution~~ **Closed 2026-09-23**
  — the flag went with the CSV paths (#44)
- ~~`--collection` keeps its `"lcps"` default and stays unvalidated~~
  **Reversed 2026-08-08** — see "Technical configuration lives in the
  registry" above
- ~~Ragged-CSV handling relies on a broad `except` in `run_rows`~~ **Closed
  2026-09-23** — `run_rows` went with the CSV paths (#44)
