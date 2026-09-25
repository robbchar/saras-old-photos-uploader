"""The Sheet's log tabs - a run's own summary, mirrored where it can be read
from anywhere.

The point is remote diagnosis. When someone calls about a problem months from
now, the Sheet can be opened from a phone: no SSH, no screen sharing, no
talking a volunteer through Terminal to find a JSONL file on a Mac in an
office two hours away.

Telemetry only, in tabs of its own. Nothing written here is ever read back
into the canonical metadata columns - Sheet -> Internet Archive stays
one-directional, and this module has no way to write anywhere else: it is
handed a client already bound to a log tab, and the only call it makes is an
append. See docs/DECISIONS.md, "The Sheet's log tabs are telemetry, never an
input"."""
from __future__ import annotations

import sys
from typing import Protocol

LOG_TAB_HEADER = ["when", "run", "outcome", "identifier", "detail"]

# Which of a run_summary record's lists become rows, and the word each one is
# labelled with. Keyed off the record rather than off SyncSummary/UploadSummary
# so this module imports nothing from ia_bulk (which imports it), and - more
# usefully - so the tab and the JSONL cannot disagree: they are rendered from
# the same object. A record simply lacking one of these keys contributes no
# rows, which is how one function serves both commands without branching on
# which one wrote the record.
PROBLEM_KINDS = (
    ("failures", "failure"),
    ("unconfirmed", "unconfirmed"),
    ("skipped", "skipped"),
)


def log_tab_rows(record: dict, run: str, headline: str) -> list[list[str]]:
    """The rows one run contributes to its log tab: its summary, then one row
    per problem it named.

    A clean run is a single row. That is deliberate - 10,000 successful
    uploads would otherwise put 10,000 rows in a tab whose whole value is that
    a person can scan it. The per-file detail is not lost; it stays in the
    run's JSONL, and `run` names the file to go and read.

    `headline` is the command's own closing console line, passed in rather
    than rebuilt here: the tab should say exactly what the operator saw, and
    each command already renders that line from the same summary object this
    record came from. For an upload that stopped early, that line also names why."""
    when = record.get("timestamp", "")
    rows = [[when, run, "summary", "", headline]]
    for key, outcome in PROBLEM_KINDS:
        for entry in record.get(key, ()):
            rows.append([when, run, outcome, entry.get("identifier", ""), entry.get("error", "")])
    return rows


class LogTabWriter(Protocol):
    """The two calls this module makes, and the only two. Stated as a
    protocol rather than typed as SheetClient so that what the mirror is able
    to do is visible here, in the module that promises it cannot touch the
    metadata columns."""

    def ensure_tab(self, header: list[str]) -> None: ...

    def append_rows(self, rows: list[list[str]]) -> None: ...


def mirror_run(client: LogTabWriter, record: dict, run: str, headline: str) -> None:
    """Append one run's summary to its log tab. Reports a failure on stderr
    rather than raising it.

    Never raising is the point, not a convenience. By the time this is
    called the run is over: files are on Internet Archive under permanent
    identifiers and the Sheet has already been written. A Sheets hiccup while
    writing telemetry that turned a successful upload into a failed one would
    invite a rerun - and a rerun is exactly what mints a second identifier
    for a photograph that already has one.

    The tab is ensured on every run rather than once at setup. It is one
    cheap read against a spreadsheet the run is already talking to, and it
    means a tab someone deletes or renames repairs itself on the next run
    instead of silently swallowing every run after it."""
    try:
        client.ensure_tab(LOG_TAB_HEADER)
        client.append_rows(log_tab_rows(record, run, headline))
    except Exception as exc:
        print(
            f"could not mirror this run into its Sheet log tab: {exc}. The run itself "
            "completed; this affects only the Sheet's copy of the log, which is telemetry. "
            f"The run's own log is still on disk as {run}.",
            file=sys.stderr,
        )
