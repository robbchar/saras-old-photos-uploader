# The upload page

Why the local upload page (`ia_bulk.py serve` + `upload_page/`, issue #29) is
shaped the way it is. The pipeline behavior the page merely displays — the
one-upload-at-a-time lock, the clean stop on interrupt, and `validate --json`'s
contract — is already recorded in [`QUOTA-AND-RUNS.md`](QUOTA-AND-RUNS.md) and
[`READINESS.md`](READINESS.md); none of it is repeated here.

One of the decision records indexed by
[`../DECISIONS.md`](../DECISIONS.md).

## The upload outlives the server

*Decided 2026-09-24, in the page's design review (#29).*

A page run is a plain `ia_bulk.py upload` child process, started in its own
session (POSIX `start_new_session`; Windows `CREATE_NEW_PROCESS_GROUP`) with
its combined stdout/stderr redirected to a file,
`logs/page-runs/<UTC>/output.txt` — never a pipe, since a pipe dies with the
server and the child would die on its next print. So a server crash, a
KeepAlive restart, or a closed browser tab never stops the upload: the child
keeps writing to its own file, the upload lock keeps recording it as the
running upload, and the next `GET /api/status` — from a reopened tab or a
freshly restarted server — reads the same lock and the same run folder and
picks the run back up exactly where it was. Nothing about "is a run going"
lives in the server's own memory; see "The lock is the single source of
truth" below.

## The lock is the single source of truth

*Decided 2026-09-24, in the page's design review (#29).*

`page_runs.compute_run_state()` never asks the page's own bookkeeping whether
an upload is going — it asks `upload_lock.running_upload()`, the same OS lock
every terminal `upload` already takes (see
[`QUOTA-AND-RUNS.md`, "One upload runs at a time, enforced by `upload`"](QUOTA-AND-RUNS.md#one-upload-runs-at-a-time-enforced-by-upload)).
The page's own `page-run.json` (pid, project, batch, mode, start time, written
into the run's folder at spawn) exists only to tell the page's *own* run apart
from a terminal run holding the same lock, by comparing the lock holder's pid
against that record's pid — never by trusting the record on its own. A
page-run folder whose pid does not match the lock's current holder describes a
finished (or refused) run, whatever it claims about itself.

## No banner in live mode

*Decided 2026-09-24, in the page's design review (#29).*

Live is the mode a volunteer runs every day; an always-on banner in that mode
would stop being read within a week. Permanence is instead stated exactly
where the decision that needs it is made: the Start button's own label
("Upload 42 items to Internet Archive") and the confirmation dialog that
follows it, which in live mode adds that the upload cannot be undone or
renamed. Test mode is the unusual one — a volunteer rehearsing, or someone
testing the page itself — and gets a persistent teal banner ("TEST MODE —
uploads go to test_collection and expire in about 30 days") for the whole
session, not only at the moment of starting.

## The confirmation dialog appears in both modes

*Decided 2026-09-24, in the page's design review (#29).*

Test mode gets the same confirmation dialog as live, even though nothing it
does is permanent, so a rehearsal practices the same clicks a live run needs.
A volunteer's only chance to build the habit of reading the dialog before
Start is in the mode where getting it wrong costs nothing.

## Progress and results come from the JSONL, never from printed text

*Decided 2026-09-24, in the page's design review (#29).*

The running-output pane shows the child's console output from `output.txt`
over Server-Sent Events, a line at a time (byte offsets preserved for resume),
but the page never parses it. One transport detail: a progress-bar line the
`internetarchive` library rewrites in place with a bare carriage return is sent
as only its final visible text (the tail after the last `\r`) — a raw `\r`
cannot cross an SSE `data:` field, which the browser reads as a line terminator.
What the page *acts on*, the "N of M" progress figure and the finished
screen's counts, comes from `page_runs.read_progress()`/`read_ending()`
reading the run's own `upload-*.jsonl` log: the same structured `run_header`/
`run_summary` records `upload` always wrote, long before the page existed (see
[`ARCHITECTURE.md`, "Logging and resume"](../ARCHITECTURE.md#logging-and-resume)).
A wording change to a printed line can never silently break the page, because
the page was never reading that line's text to begin with.

## KeepAlive restarts only on failure

*Decided 2026-09-24, in the page's design review (#29).*

Piece 6 (deployment — a later PR, not this one) will run the server as a
LaunchAgent with `KeepAlive: {SuccessfulExit: false}`: launchd restarts it only
after it exits non-zero. So `run_server()` exits `0`, after printing why, for
a refusal a restart cannot fix — a registry that will not load or isn't the
expected shape, a project the registry doesn't have, or `upload_page/dist/`
missing a committed bundle stamp *or* holding one that's stale (its stamp no
longer matches the source under `upload_page/src/` — see `build_stamp.py`).
Either way the page was never built for what's on disk now, and a restart
can't build it. Anything else that ends the server counts as a failure a
restart might actually cure. Recorded here, ahead of piece 6, because it
already constrains what "exit clean" has to mean in `upload_server.py` today.

Piece 6 now exists in both halves — a person double-clicking to serve the
page in LIVE mode (see [`../MAC-LAUNCHER.md`](../MAC-LAUNCHER.md)), and a
LaunchAgent that runs the server unattended (see "The page agent is always-on,
and one of two mutually exclusive models" below). A Mac uses one or the other,
never both.

## The server's request guard

*Built 2026-09-25 (#29, piece 5).*

Binding to `127.0.0.1` alone does not stop a page open in the same browser, on
some other origin, from addressing this port by name. So every request is
checked before it reaches any route (`UploadPageHandler._passes_guard`):

- **Host** must be exactly `127.0.0.1:<port>` or `localhost:<port>` — the
  port the server actually bound, so the same check holds whether it is on
  its configured port or, as in tests, an ephemeral one. Anything else is
  refused with `403`; this is the guard against DNS rebinding.
- **Origin**, when the request carries one, must be `http://127.0.0.1:<port>`
  or `http://localhost:<port>`; refused with `403` otherwise.
- **POST Content-Type** must be `application/json` on every route, checked
  before the body is even read; anything else is refused with `415`. This
  forces a CORS preflight the server never approves, so a form submission or
  a fetch from an unrelated page cannot reach a route by accident.

The server binds `127.0.0.1` only — IPv4 loopback, never `0.0.0.0`, never a
hostname. In development, Vite's dev-server proxy (`upload_page/vite.config.ts`)
rewrites the proxied request's `Origin` to the upload server's own, so the
guard never needs a special case for the dev origin — see
[`upload_page/README.md`](../../upload_page/README.md).

## The run-state model

*Built 2026-09-25 (#29, piece 5).*

`page_runs.compute_run_state(lock_path, logs_base)` is the one function that
decides what the page shows, derived from two independent facts on disk — the
upload lock's holder, and the newest folder under `logs/page-runs/` — read
fresh on every request, never cached in the server's own memory. It returns
one of four states:

- **idle** — no lock held, and no page-run folder exists yet. Start is
  enabled.
- **page_run_active** — the lock is held, and its holder's pid matches the
  newest page-run folder's `page-run.json`. This is the page's own run:
  batch, mode, start time, and live progress (`done`/`planned`, read from the
  run's JSONL) are shown, and Stop is offered.
- **terminal_run_active** — the lock is held, but not by the page's own
  newest run: a run started from a terminal, another page instance, or a
  stale/unmatched page-run record. Start is disabled; no output or Stop is
  offered, since the page has nothing of its own to show for a run it did not
  start.
- **finished** — the lock is free, and a page-run folder exists. Its ending
  (`page_runs.read_ending()`) is one of `completed`, `stopped`,
  `rate_limited`, `ended_without_summary` (a JSONL with no closing
  `run_summary` — the process died mid-run), or `refused` (no JSONL at all;
  the last lines of `output.txt` are shown as the reason, uninterpreted).

Deriving both the "who's running" and "what happened" questions from disk
alone, rather than from anything the server remembers, is what lets a
restarted server or a second browser tab reconstruct the same state a moment
later — see "The upload outlives the server" and "The lock is the single
source of truth" above.

## The page agent is always-on, and one of two mutually exclusive models

*Built 2026-09-28 (#29, piece 6).*

On a Mac, `setup --enable-upload-page` writes and loads a per-user
LaunchAgent (`org.lcpsociety.iabulk.uploadpage.<project>`) that runs `serve`
under `KeepAlive {SuccessfulExit: false}` — the schedule "KeepAlive restarts
only on failure" above anticipated, now built: `AgentSpec` gained a typed
`Schedule` (`IntervalSchedule | KeepAliveSchedule`), and `render_plist` picks
the plist keys from it; the hourly sync agent's own plist is unchanged.
Writing the plist *is* enabling it, mirroring `--enable-agent` — there is no
separate load step. Mode follows `setup`'s own `--live`; unlike the sync
agent, a test-mode page is a legitimate rehearsal, so `--enable-upload-page`
allows it. Only `--offline` is refused, since the checks the gate reads need
the network to verify.

`doctor` reports the agent's health with the same three generic per-agent
checks the sync agent gets (plist current, log directory, loaded), plus
three page-specific ones: whether `upload_page/dist/` matches its source
(`bundle current`, pairing with the server's own stale-bundle startup
refusal); whether `/api/health` answers on loopback (`server answering`);
and whether the commit it reports matches the checkout's `HEAD` (`server
running current code`) — the only one of the six that FAILs rather than
reports `UNKNOWN` when it cannot be confirmed, because a confirmed mismatch
is worth restarting over: its fix runs `launchctl kickstart -k`, so an
ordinary `git pull` + `./install.sh` restarts a stale server without a
separate `--enable-upload-page` re-run. See
[`DEPLOYMENT.md`, "Enabling the upload page agent"](../DEPLOYMENT.md#17-enabling-the-upload-page-agent).

**A Mac runs this agent or the manual Dock launcher
(`start-upload-page-live.command`, #84) — never both.** Both bind the same
port and both run `serve --live`; whichever starts second fails to bind.
This is a deployment choice made once per Mac — neither mechanism detects or
refuses the other on its own.

## The color scheme follows the computer until someone picks one

*Decided 2026-09-29 (#83).*

The page has a light and a dark color scheme, drawn from lcpsociety.org
(warm paper, near-black, the logo's gold), and a three-way control in the
header: **Auto**, **Light**, **Dark**. Auto is the default and follows the
computer's own setting, live. Picking Light or Dark saves that choice in the
browser (`localStorage`, key `upload-page:color-scheme`) and it wins from
then on; picking Auto again forgets it. Auto exists because a volunteer who
flips the page to try it out otherwise has no way back to "whatever this Mac
does".

The scheme is an attribute (`data-color-scheme`) on the page root, not a
`prefers-color-scheme` media query, so a saved choice can override the OS.
`index.html` sets it with a small inline script before the first paint —
without it the page would flash the wrong scheme while the bundle loads —
and `useColorScheme` keeps it current afterwards. The two read the same
storage key; a test runs the inline script to hold them together.

Gold stays decorative in light mode (a rule along the header's top edge)
because it is too pale for text on a light background; in dark mode it
becomes the accent. Test mode moved from amber to teal so its banner never
reads as the brand color. The display font, Josefin Sans (the society
site's), is bundled into `dist/` rather than loaded from Google, so the page
makes no third-party requests.
