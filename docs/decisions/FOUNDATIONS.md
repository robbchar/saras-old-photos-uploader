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

## Generic to "a project", not hardcoded to photos

A second LCPS project is expected to reuse this pipeline, which is why the
registry has a `projects` map rather than a single hardcoded code, and why the
docs say "items" more than "photos".

## Technical configuration lives in the registry, not the command line

*Decided 2026-08-08, reversing the "accepted, not overlooked" item above.*

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

## Accepted, not overlooked

The final build review named these and chose to leave them. They are recorded
in [`KNOWN-ISSUES.md`](../KNOWN-ISSUES.md) with reproduction details:

- Chunking has no real pacing or checkpoint behavior
- `--files-dir` does not constrain path resolution
- ~~`--collection` keeps its `"lcps"` default and stays unvalidated~~
  **Reversed 2026-08-08** — see "Technical configuration lives in the
registry"
  below
- Ragged-CSV handling relies on a broad `except` in `run_rows`
