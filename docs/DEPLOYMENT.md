# Mac Deployment Runbook

Provisioning and upgrading the Mac that runs this pipeline: Python, the
service-account credentials, `ia` authentication, the hourly sync agent, and
`./install.sh`. Every step below is off-machine, in a human's hands, or a
judgment call — anything a script could converge on its own lives in
`ia_bulk.py setup` instead, not here.

**`<project>` throughout means the registry project id** from
`projects_registry.json` — for this collection that is **`sarasoldphotos`**
(see the "Project registry" section of [`README.md`](../README.md)).
Substitute it literally every time. So where a command below reads:

```bash
./install.sh --project <project>
```

what you actually type is:

```bash
./install.sh --project sarasoldphotos
```

The same goes for every `doctor` and `setup` command here. It is the project
id, not the macOS account name and not the Internet Archive collection —
those happen to share the word.

**Python commands run the checkout's own interpreter, `.venv/bin/python`.**
macOS has no `python` command, and its `python3` is too old and has none of
the dependencies. `.venv` exists once `./install.sh` (§10) has run. Wherever
[`OPERATIONS.md`](OPERATIONS.md) says `python` — `python ia_bulk.py …`, or
`python -m …` at the end of a pipe — type `.venv/bin/python` in its place on
this Mac: `.venv/bin/python ia_bulk.py …`,
`… | .venv/bin/python -m json.tool`. The same goes for `ia`, which is
`./.venv/bin/ia` here (§6).

**Every command after the clone (§2) runs from the checkout's root.** A new
Terminal window starts in your home folder, so `cd` back into the checkout
first.

## 1. Who this is for

This document covers getting the pipeline running on a Mac — installed,
authenticated, and (once ready) syncing on its own — not running a batch of
uploads. For that, start at [`docs/OPERATIONS.md`](OPERATIONS.md). You should
only need this document on install day, when replacing a credential, or when
upgrading the checkout.

## 2. Which account to do all of this from

**Do every step in this document while logged in as the `sarasoldphotos`
account** — the shared account the pipeline runs under day to day. That means
the clone, the service-account key, `ia configure`, `./install.sh`,
`--enable-agent`, and every later upgrade. This document calls it *the
operating account*.

The Mac has a second account, the one Robb uses for development. Both are
admins — "operating" and "development" here describe what each account is
*for*, not what privileges it has, so being an admin on the box is not a
reason to install from the other one. No step in this document is performed
from the development account. Keeping installation and operation in one
account is what makes this simple: one owner, one plist, no ownership to
transfer, and nothing to get backwards later.

> **Why it has to be this account and not another:** a macOS LaunchAgent is
> per-user. A plist in `~/Library/LaunchAgents` loads only for the account it
> belongs to, and fires only while that account has an active login session —
> not merely while the Mac is powered on. So:
>
> - The account that installs must be the account that runs. That is the whole
>   reason for the rule above.
> - The operating account needs **auto-login enabled** in System Settings, and
>   needs to **stay logged in**. Fast user switching away from it is fine;
>   logging all the way out is not.
>
> Get this wrong and `doctor` reports the agent as `UNKNOWN` or missing even
> though everything else converged correctly — see §15.

**Doing development later.** Change code in a separate clone — the development
account's own home, or another machine — then `git pull` here as the operating
account (§14). Do not edit *this* checkout from the development account: it
belongs to `sarasoldphotos`, so git refuses it as "dubious ownership", and the
service-account key is mode `0600` and unreadable from there regardless. Being
an admin does not change either — it means you *could* `sudo` your way in, not
that you should. That refusal is the system working: it keeps the checkout and
the account that runs it from drifting apart.

**Both `setup` and `doctor` resolve `~` to the account running them.** Follow
§2 and that is invisible. A plain `./install.sh` run from the development
account by mistake writes no plist — only `--enable-agent` does (§12).
`--enable-agent` run from there would put one in *that* account's
`~/Library/LaunchAgents`, and launchd loads every plist in that folder at
login, so the agent would start at that account's next login too. §13 says
how to remove it.

### Getting the checkout

Logged in as the operating account, open Terminal and clone the repository
into its home folder:

```bash
git clone <repo-url>
```

`<repo-url>` is the repository's clone address, from the **Code** button on
its GitHub page. On a Mac that has never had developer tools, that first `git`
opens a dialog offering to install the Command Line Tools instead; accept it,
then run the command again. Then `cd` into the folder the clone just created —
it is the name `git` printed in `Cloning into '…'`:

```bash
cd <checkout-folder>
```

**Clone it where it will stay.** The agent's plist (§12) records the
checkout's full path, so once the agent is enabled, moving or re-cloning the
checkout means re-running §12 from the new one.

## 3. Python

The pipeline needs **Python 3.10 or newer**. This isn't a language-syntax
requirement — it's what the Google client libraries (`google-auth`,
`google-api-core`) need. macOS ships Python 3.9.6, which does not qualify, and
`install.sh` deliberately **refuses rather than installing one for you**:

```
No Python 3.10+ on PATH. macOS ships 3.9.6, which the
Google libraries do not support. Install one, then re-run this script:
  brew install python@3.12
See docs/DEPLOYMENT.md, section "Python".
```

1. Install Homebrew if it isn't already present (see brew.sh).
2. `brew install python@3.12`
3. Re-run `./install.sh` (§10) — it searches `python3.13`, `python3.12`,
   `python3.11`, `python3.10`, then `python3`, in that order, and uses the
   first one on `PATH` that qualifies.

## 4. Service account

Reading and writing the Sheet uses a Google Cloud **service account** rather
than a person's sign-in — see
[`docs/decisions/FOUNDATIONS.md`](decisions/FOUNDATIONS.md#the-sheet-is-reached-as-a-service-account-not-as-a-person)
for why.

1. In the Google Cloud console, confirm the **Google Sheets API** is enabled
   for the project (**APIs & Services → Library**) — reading or writing the
   Sheet fails until it is, and this is easy to miss on a project that has
   never used the Sheets API before.
2. Under **IAM & Admin → Service Accounts**, create the service account (or
   reuse the existing one) in the project's Google Cloud project. It needs no
   project roles — all of its access comes from Sheet sharing (§5).
3. On its **Keys** tab, create a new JSON key. It downloads once and cannot
   be re-downloaded, so save it immediately.

   If key creation is refused outright, the likely cause is the
   organization policy **"Disable service account key creation"**
   (`iam.disableServiceAccountKeyCreation`), which Google turns on by default
   for newer organizations. Only an org-policy administrator can disable it
   for this project — this is a judgment call above the installer's pay
   grade, not something to work around.
4. Save it as `.ignored/google-service-account.json`, relative to the repo
   root. Everything under `.ignored/` is gitignored — never move it anywhere
   under the tracked tree.

**This repo is public. Never paste the service account's email address, the
Google Cloud project id, or a real spreadsheet id into any tracked file** —
including this one. Once the key is in place, get the address to share the
Sheet with (§5) from the machine itself instead of typing it anywhere.
`./install.sh` (§10) is safe to run this early — it creates `.venv`, tightens
the key's permissions, enables nothing, and ends with the same report `doctor`
prints:

```bash
./install.sh --project <project>
```

With the key present but the Sheet not yet shared, the `spreadsheet
reachable` check fails and its remedy line prints the exact address to
share with — read from the key file, never hardcoded. Other `[FAIL]` lines are
expected this early (`ia configure` is §6). If the test `sheet_id` is still a
placeholder, both Sheet checks say `not checked` instead of reading anything;
set it first.

**That printed line contains the real service-account address.** It is the one
place the tool deliberately reads a credential's contents, and it reads only
the key's own `client_email` — never a private key, never a token. Treat the
output accordingly: do not paste a `doctor` report into a public issue, a pull
request, or anywhere else in this repo without removing that address first.

## 5. Sharing the Sheet

Share both the project's real Sheet and its test Sheet (both ids live in
`projects_registry.json`) with the address from §4, as **Editor**, with
"Notify people" unchecked. Read-only is not enough — `upload` and
`sync-metadata` write back to the Sheet even in test mode.

The address is printed only while a Sheet refuses the key. Once the test Sheet
is shared, and while the real `sheet_id` is still a placeholder, nothing
prints it, so read it from the key itself:

```bash
grep client_email .ignored/google-service-account.json
```

If the Google Workspace that owns the Sheet restricts sharing outside its own
domain, this share is blocked until a Workspace admin allows it
(admin.google.com → Apps → Google Workspace → Drive and Docs → Sharing
settings) — the service account's address is, by definition, outside the
Workspace's domain. Another admin-console blocker, not something `doctor`
can see or fix.

## 6. `ia configure`

The `ia` credentials need to be created once, against the shared org account
(`admin@lcpsociety.org`) — no environment variables, no per-user credentials.

**Use the checkout's own `ia`, `./.venv/bin/ia`.** A stock Mac has no `ia`
command, and you do not need to install one separately: `ia` is a
command-line script that ships inside the `internetarchive` Python package,
which is one of this pipeline's own dependencies, so §10's `./install.sh` puts
it in `.venv` along with everything else. A global `ia` left on `PATH` by some
earlier install reads and writes the same per-user config file, so it would do
just as well — but nothing below depends on one.

> **If you are installing in order, this is the one step that runs out of
> sequence.** Do §10 before this section. That first `./install.sh` will
> report `ia credentials` as `FAIL` and exit non-zero — that is correct and
> expected, because you have not created them yet. Come back here, then re-run
> `./install.sh` and watch that check go green.

Then:

```bash
./.venv/bin/ia configure
```

This is interactive: it prompts for `Email address:` and `Password:` — the
org account's — and nothing else. It logs in with them, writes the keys
archive.org hands back (not the password) to a config file, and ends by
printing `Config saved to: <path>`, naming that file. Do this once, as the
operating account (§2).

**The pipeline itself never runs the `ia` command.** `ia_bulk.py` imports the
`internetarchive` library directly and calls it in-process. The CLI matters
here only because `ia configure` is the supported way to create the
credentials file that the library then reads.

**Where it writes.** Reading `internetarchive`'s own source
(`internetarchive/config.py`, `parse_config_file()`, at the exact version
`requirements.txt` pins), the config file is chosen in this order, and the
first one that already exists wins:

1. the `IA_CONFIG_FILE` environment variable, if set
2. `$XDG_CONFIG_HOME/internetarchive/ia.ini`, which is `~/.config/internetarchive/ia.ini` when `XDG_CONFIG_HOME` is unset
3. `~/.config/ia.ini` (legacy)
4. `~/.ia` (older legacy)

On a Mac with no prior `ia` install and no `XDG_CONFIG_HOME` set, none of the
legacy files exist yet, so `ia configure` creates
**`~/.config/internetarchive/ia.ini`**. `write_config_file()` also `chmod`s
that file to `0600` itself — no manual `chmod` needed. Still, confirm it on
the actual machine rather than trusting this document blindly:

```bash
ls -la ~/.config/internetarchive/ia.ini
```

Expect one line, starting `-rw-------` and ending `ia.ini`.

If nothing is there, the `Config saved to:` line names the file `ia configure`
actually wrote, and `doctor`'s `ia credentials` check (below) names the file
the pipeline actually reads — `no ia credentials at <path>` when it finds
nothing there.

`doctor` covers this from Python's side with two checks, using
`internetarchive`'s own path-precedence logic rather than a second copy of the
list above:

- **`ia credentials`** — `PASS` naming the resolved path when the file is there
  and carries both S3 keys; `FAIL` with `ia configure` as the remedy when it is
  absent, unparseable, or missing a key. It reads presence only: the key values
  are never printed, logged, or sent anywhere, and no call is made to Internet
  Archive. A credential that exists but has been revoked at archive.org still
  reports `PASS` — only a real run can tell you that.
- **`ia credentials permissions`** — the same scrutiny the Google key gets:
  `0600` or the stricter `0400` passes, anything group- or world-readable
  fails, and it is `UNKNOWN` where the platform has no POSIX permissions. It has no
  automatic fix: the file lives outside the checkout, so `setup` reports on it
  rather than chmodding someone's home directory.

## 7. `files_dir` and the LaCie drive

`files_dir` in `projects_registry.json` must point at wherever the project's
photos actually live on this machine.

1. Plug in the LaCie drive and confirm its mount path, e.g. `ls /Volumes`.
2. Open `projects_registry.json` and set the `<project>` entry's `files_dir`
   to that path (or a path under it).
3. `doctor`'s `files drive` check reports `UNKNOWN` (not `FAIL`) if the path
   doesn't exist — the drive being unplugged means "could not tell what's on
   it," not "broken." See §15. It never blocks `--enable-agent` (§12):
   `sync-metadata` does not read the drive.

## 8. The real `sheet_id`

`projects_registry.json` ships with a placeholder:

```json
"sheet_id": "REPLACE_WITH_REAL_SHEET_ID"
```

Replace it with the real Sheet's id (from its URL) for `<project>`. Every
`--live` command is refused while this is still a placeholder.

`doctor` checks whichever mode you ask it for, so the `live spreadsheet ID`
check only exists on a run that passes `--live`:

```bash
.venv/bin/python ia_bulk.py doctor --project <project> --live
```

Without `--live` the same run checks `test spreadsheet ID` instead and says
nothing at all about the real Sheet.

## 9. The sync columns

`sync-metadata` needs two tool-owned columns on the real Sheet beyond
`upload`'s own four: **`ia_sync_hash`** and **`ia_last_synced`**. Add both as
headers on the Sheet. Spelling must match exactly; position does not matter.

**Leave them visible, and give both columns a red background.** They are
tool-owned, so nobody should be typing in them casually — but hiding them
would throw away the two things they are good for:

- **`ia_last_synced`** records when a row last went out. That is genuinely
  useful to glance at, and you cannot glance at a hidden column.
- **`ia_sync_hash`** is how a re-sync gets forced. `sync-metadata` sends a row
  only when its content hashes differently from what this cell records, so
  **clearing the cell makes the row send again on the next run** — a row whose
  item on archive.org looks wrong, even though the Sheet says it synced. That
  is the manual override, and it needs a cell you can select.

The red background is the signal: *the tool owns this, don't type here unless
you know why.* A cataloguer never needs to touch either column.

```bash
.venv/bin/python ia_bulk.py doctor --project <project> --live
```

`doctor`'s `sync state columns` check runs the checks `sync-metadata` makes
on the Sheet before it sends anything, so it fails on what would make the
agent refuse: no data rows, either sync column missing, a header problem (any
two headers that normalize to the same name, so the second would be silently
ignored, or a blank or punctuation-only header), a missing `upload` write-back
column, or a `file_template` naming a column the Sheet lacks. As in §8, this
check reads the Sheet the run names: **without `--live` it reads the test
Sheet**, and a `PASS` there says nothing about the real one.

## 10. Install

```bash
./install.sh --project <project>
```

This is the one command that brings a machine up to date, whether it's the
first run on an empty Mac or the twentieth. In order, it:

1. Finds a Python 3.10+ on `PATH` (§3), refusing if none qualifies.
2. Creates `.venv` if it doesn't already exist, and deletes and recreates it
   when its Python is missing or older than 3.10 — a venv built from macOS's
   3.9, or from a Homebrew Python since removed.
3. Installs/upgrades `pip`, then installs `requirements.txt` into `.venv`.
4. Runs `ia_bulk.py setup --project <project>`, which converges every check
   in `deployment.py` it can fix on its own (key file permissions) and then
   re-verifies everything, printing a `[PASS]`/`[FAIL]`/`[UNKNOWN]` report.
   It never writes the LaunchAgent plist — launchd loads every plist in
   `~/Library/LaunchAgents` at login, so writing one is enabling the agent,
   and a rewrite alone never reaches the loaded job. Both are §12's job.

**Safe to re-run.** Running it again on a machine that already matches the
checkout prints `nothing to change; this machine already matches the
checkout.` and the same report — it does not undo or duplicate anything. When
something it cannot fix on its own is still failing, it prints `nothing setup
can change on its own; each [FAIL] below says what to do.` instead.

`setup` refuses outright, before running any check, if `--enable-agent` is
passed without `--live` or together with `--offline` (§12). Both refusals print
the command to run instead. There is no override flag.

### If it fails partway

`install.sh` rebuilds a `.venv` whose Python is missing or too old, but not
one broken some other way: if creation fails partway through (disk full,
interrupted) after the interpreter was linked, the next run keeps the
directory and then fails less clearly at the `pip install` step. If
`install.sh` fails and you're not sure why, the safe recovery is:

```bash
rm -rf .venv
./install.sh --project <project>
```

## 11. Credentials: where, what they grant, how to rotate

| Credential | Path | Grants | Rotate |
|---|---|---|---|
| Google service-account key | `.ignored/google-service-account.json` | Read/write on whatever Sheets are shared with its address as Editor — nothing else; it has no Google Cloud project roles (§4) | Create a new key on the service account's **Keys** tab in the Cloud console, replace the file, then delete the old key on that same tab |
| `ia` config (§6) | `~/.config/internetarchive/ia.ini` (confirm on-machine) | Full access to the shared org Internet Archive account — upload, edit metadata, delete | Re-run `./.venv/bin/ia configure` with the org credentials; if the org password itself is rotated, do this immediately after |

Neither credential ever belongs in the git checkout's tracked tree or in
chat/email — both are gitignored or live outside the repo entirely.

Every edit the tool makes to the Sheet shows up in the Sheet's own **File →
Version history** as the service account, not as whichever person or account
actually ran the command — worth knowing before you go looking for a human
name there.

## 12. Enabling the hourly sync

Once — and only once — the first live runs (`upload --live`, `sync-metadata
--live`) have been verified by hand per
[`docs/OPERATIONS.md`](OPERATIONS.md#3-live-run), log in as **the operating
account** (§2) and run:

```bash
./install.sh --project <project> --live --enable-agent
```

**`--live` is required here, and is not implied.** The agent it installs runs
`sync-metadata --live` on the *real* Sheet. Without `--live`, `setup` would
verify the *test* Sheet — its id, its sharing, its sync columns — and then
start an hourly live sync against a real Sheet nothing had checked. Rather than
infer what you meant, `setup` refuses and names this command. `--offline` is
refused with `--enable-agent` for the same reason: the Sheet checks it skips
are exactly the ones that gate enabling a live agent.

**The agent reads the registry `setup` checked.** The plist runs
`sync-metadata --project <project> --live --registry <path>`, with the
absolute path of the registry `setup` was given — this checkout's
`projects_registry.json` unless you passed `--registry`. If you did, give
`doctor` the same `--registry` (§15). Checked against any other registry,
`launch agent plist` reports a plist that does not match, because the agent is
not syncing what that `doctor` run looked at. Every `./install.sh` command
`setup` and `doctor` print repeats that `--registry`, so running one as
printed re-enables the same agent.

**It enables nothing if a check the agent needs failed.** `setup` converges,
re-checks, and only then writes the plist and loads the agent. A `[FAIL]` line
on anything the hourly sync depends on — a missing key, a placeholder sheet
id, absent sync columns — prints the report, names the failed checks, says the
agent was **not** enabled, and exits non-zero. There is no `--force`. Three
checks are reported but never block: `files drive` (`sync-metadata` never
reads the drive), and the two `launch agent` checks, because enabling is what
fixes them — including a `launch agent loaded` `FAIL` left by the agent's last
run exiting non-zero.

**And nothing if the live Sheet could not be checked.** `spreadsheet
reachable` and `sync state columns` must come back `PASS` here, not `UNKNOWN`.
A machine that simply has no working network — install day on someone else's
wifi — turns both into "could not tell", which everywhere else is not a
failure. For this one command it is: enabling on it would start an hourly live
sync against a Sheet whose id, sharing and sync columns were never confirmed,
which is exactly what refusing `--offline` is for. `setup` says which check
could not be verified and loads nothing; each `UNKNOWN` line says why. A key
Google rejects, or a `sheet_tab` naming no tab, is a `FAIL` with its own fix
line, not an `UNKNOWN`. An error on Google's side while issuing the token — any
HTTP 5xx, a 502 included — says nothing about the key, so it stays `UNKNOWN`:
re-run in a few minutes. When there are `FAIL`s as well, `setup` names those
first and the `UNKNOWN` Sheet checks after them — an `UNKNOWN` Sheet check is
often only a consequence of a `FAIL`.

If a check that does not block fails — the files drive, say — `setup` still
enables the agent, says so (`the hourly sync agent IS enabled`), and exits
non-zero for that `FAIL`. Read the message, not just the exit status, before
re-running.

**`spreadsheet reachable` proves read access, not Editor.** A Sheet shared as
Viewer passes it, and the Sheets API has no read-only way to tell the two
apart. The first live runs this section requires are what prove Editor:
`upload --live` stops at its first write, before anything reaches Internet
Archive, on a Sheet it cannot edit.

`UNKNOWN` on any **other** check still does not block — the drive being
unplugged is not a reason to refuse. And this stricter rule is this gate's
alone: `doctor` still exits **0** on an `UNKNOWN` Sheet check (§15).

**This starts a live sync immediately.** `--enable-agent` loads a LaunchAgent
with `RunAtLoad` set, so bootstrapping it runs `sync-metadata --live` right
then, and again at every future login of the operating account. That's
deliberate — waiting up to an hour to discover the agent doesn't work is
worse — and it's safe because a sync only pushes rows whose content actually
changed since the last push; a run with nothing changed prints `nothing to
sync - all N uploaded rows already match their last push` and sends no
corrections to the Sheet or Internet Archive (§4 of `OPERATIONS.md`). This is exactly why
`--enable-agent` must wait until *after* the first live runs are verified by
hand: the first thing it does is run for real.

The agent fires hourly (`StartInterval` = 3600 seconds) after that. If the
Mac is asleep when one or more intervals would have fired, launchd runs it
**once** on wake, not once per missed hour.

**Re-running it after an upgrade re-loads the agent.** launchd keeps its own
copy of the plist from the moment it was bootstrapped, so rewriting the file
alone changes nothing about the running job. When the agent is already loaded,
`--enable-agent` says so, boots it out — stopping a sync that happens to be
running at that moment — waits up to 30 seconds for launchd to let go of it,
and bootstraps the new definition, which means another immediate live sync.
If the old agent is still registered after the wait, or `launchctl` refuses
the bootstrap, `setup` exits non-zero and says the agent was not loaded,
rather than reporting success it cannot confirm. The new plist is already
written by then, so launchd still loads it at the next login; `doctor --live`
shows what is loaded right now.

To check it after enabling — `--live`, because that is the Sheet the agent
you just started is syncing:

```bash
.venv/bin/python ia_bulk.py doctor --project <project> --live
tail -20 logs/launchagent-<project>.err
```

## 13. Uninstalling the agent

```bash
launchctl bootout gui/$(id -u)/org.lcpsociety.iabulk.sync.<project>
rm ~/Library/LaunchAgents/org.lcpsociety.iabulk.sync.<project>.plist
```

Run this as the account the agent is loaded for. `doctor` will then report
both `launch agent` checks as `UNKNOWN`: `loaded` because it can't tell "not
loaded" from "no session for this account", and `plist` because the agent is
not enabled. A plain `./install.sh` — an upgrade, say — leaves it that way;
only §12 writes the plist again. Remove the plist, not just the bootout:
launchd would load it again at the next login.

**If `--enable-agent` was ever run from another account**, that account has
its own plist, which launchd loads at every login of that account (§2). Log in
there and run the same two commands.

## 14. Upgrading

```bash
git pull
./install.sh --project <project>
```

The same command as install day — there is no separate upgrade path. It
converges whatever the new checkout needs (new dependencies, key permissions)
and leaves the agent alone: an enabled agent stays loaded, and one never
enabled stays that way.

`internetarchive` is pinned to an exact version, so the library changes only
when a `git pull` moves that pin, never just because a newer release is out.
Why, and how the pin is bumped: [QUOTA-AND-RUNS.md](decisions/QUOTA-AND-RUNS.md#internetarchive-is-pinned-exactly).

**If the plist changed, the running agent is still the old one.** When a
`git pull` changes what the agent should run, `doctor` and `setup` report
`[FAIL] launch agent plist: … does not match this checkout and registry` —
`setup` does not rewrite it, because launchd holds its own copy from the moment
it was bootstrapped and a rewrite alone would not reach it. Re-run §12 from the
operating account; it rewrites the plist, boots the agent out, and bootstraps
the new definition:

```bash
./install.sh --project <project> --live --enable-agent
```

That starts another live sync immediately, the same as the first time.

## 15. Checking a machine later

```bash
.venv/bin/python ia_bulk.py doctor --project <project>
```

Read-only — it changes nothing on the machine, in the Sheet, or on Internet
Archive. Run it any time something seems off, after replacing a credential,
or as a routine check.

Each check reports one of three states, never just pass/fail:

- **`PASS`** — confirmed working.
- **`FAIL`** — confirmed broken; the line below it names the fix. `doctor`
  exits non-zero if any check fails.
- **`UNKNOWN`** — could not tell, not "broken." The drive being unplugged, no
  network, an agent not enabled yet, or a permission model `doctor` can't
  express (Windows, for testing) all report `UNKNOWN` rather than `FAIL`. A
  placeholder sheet id is a `FAIL` on its own ID check; the two Sheet checks
  it leaves unread say `not checked`, which is `UNKNOWN`. `doctor` exits **0** when
  every failing check is `UNKNOWN` — conflating "couldn't check" with
  "broken" would make the report untrustworthy on exactly the days (no
  network, drive unplugged) when you most need to trust it.

`doctor` checks the mode it is given, so on a machine that is running live
traffic, check the live side too — without `--live` it reports on the test
Sheet and the test sheet id:

```bash
.venv/bin/python ia_bulk.py doctor --project <project> --live
```

Pass `--offline` to skip the checks that need the network (`spreadsheet
reachable`, `sync state columns`) entirely, e.g. when checking a machine that
happens to be offline right now.

### If `launch agent plist` still says FAIL

`FAIL` here means a plist exists but does not match this checkout and
registry. No plist at all is `UNKNOWN`: the agent is not enabled, and §12 is
the whole answer. In order of likelihood:

1. **A `git pull` changed what the agent should run** and §12 has not been
   re-run since. Re-run it (§14).
2. **You are looking at the wrong home, checkout or registry** (§2). `doctor`
   run from the development account reports on that account's own plist, one
   run from a second clone compares the plist with that clone, and one given a
   different `--registry` than §12 was compares it with that registry. Log in
   as `sarasoldphotos` and check again from the checkout the agent runs, with
   the `--registry` §12 was given, if any.
3. **The write was tried and failed.** This one applies only after §12 has
   just run and the check still says `FAIL` — usually `~/Library/LaunchAgents`
   is not writable by the account running the script. Confirm you are the
   operating account (§2) and re-run §12.

Whatever the cause, the plist is generated from the checkout and registry, so
replacing it is safe:
`rm ~/Library/LaunchAgents/org.lcpsociety.iabulk.sync.<project>.plist` and
re-run §12, which writes it fresh and reloads the agent.

## 16. Verifying the service account by hand

`doctor` covers placement, permissions and whether the Sheet answers. It does
not exercise a real read through the whole stack, does not preview a sync, and
never shows you the failure message you will see when the key is gone. Run
these three on a new machine, and after replacing the key, before trusting a
real run. They need about five minutes and change nothing. `< /dev/null`
detaches each command from the terminal, so nothing could stop and wait for a
sign-in even if it tried.

1. **Read the test Sheet.** Expect the normal readiness report and no sign-in
   prompt:

   ```bash
   .venv/bin/python ia_bulk.py validate --project <project> < /dev/null
   ```

2. **Preview a sync.** Expect the usual summary line, for example
   `10 uploaded rows; … 10 already in sync and would not be sent`:

   ```bash
   .venv/bin/python ia_bulk.py sync-metadata --project <project> --dry-run < /dev/null
   ```

3. **See the failure message once.** Move the key aside, run `validate`, and
   expect a single line starting `could not authenticate to Google Sheets:
   missing service account key at …` with no traceback. Then put the key back:

   ```bash
   mv .ignored/google-service-account.json .ignored/google-service-account.json.off
   .venv/bin/python ia_bulk.py validate --project <project>
   mv .ignored/google-service-account.json.off .ignored/google-service-account.json
   ```

For a full round trip — a real edit reaching Internet Archive, and the Sheet's
version history showing the service account as its editor — follow
[4. Corrections](OPERATIONS.md#4-corrections) against the test Sheet: change
one uploaded row's title, sync, check the item, then change it back and sync
again.
