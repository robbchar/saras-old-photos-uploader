# Mac Deployment Runbook

Provisioning and upgrading the Mac that runs this pipeline: Python, the
service-account credentials, `ia` authentication, the hourly sync agent, and
`./install.sh`. Every step below is off-machine, in a human's hands, or a
judgment call — anything a script could converge on its own lives in
`ia_bulk.py setup` instead, not here.

`<project>` throughout means the registry project id from
`projects_registry.json` — currently `sarasoldphotos` (see the "Project
registry" section of [`README.md`](../README.md)). Substitute it literally
when you type a command.

## 1. Who this is for

This document covers getting the pipeline running on a Mac — installed,
authenticated, and (once ready) syncing on its own — not running a batch of
uploads. For that, start at [`docs/OPERATIONS.md`](OPERATIONS.md). You should
only need this document on install day, when replacing a credential, or when
upgrading the checkout.

## 2. Which account to do all of this from

**Do every step in this document while logged in as the shared operating
account** — the one the pipeline runs under day to day. That means the clone,
the service-account key, `ia configure`, `./install.sh`, `--enable-agent`, and
every later upgrade.

The Mac also has an admin account for development. It is not used for any step
here. Keeping installation and operation in one account is what makes this
simple: one owner, one plist, no ownership to transfer, and nothing to get
backwards later.

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

**Doing development later.** If you need to change code, do it in your own
clone, in your own home directory or on another machine, then `git pull` here
as the operating account (§14). Do not edit this checkout from the admin
account: it is owned by the operating account, so git will refuse it as
"dubious ownership" and the key is mode 600 and unreadable to you anyway. That
refusal is the system working — it means the checkout and the account that
runs it have not drifted apart.

**Both `setup` and `doctor` resolve `~` to the account running them.** Follow
§2 and that is invisible. If you ever do run `./install.sh` from the admin
account by mistake, two things happen: a second plist lands in *that* account's
`~/Library/LaunchAgents` where nothing will ever load it (§13 says how to
remove it), and `doctor` run from there reports `[PASS] launch agent plist`
about a file that has nothing to do with the running agent.

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
Sheet with (§5) from the machine itself instead of typing it anywhere:

```bash
python ia_bulk.py doctor --project <project>
```

With the key present but the Sheet not yet shared, the `spreadsheet
reachable` check fails and its remedy line prints the exact address to
share with — read from the key file, never hardcoded.

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

If the Google Workspace that owns the Sheet restricts sharing outside its own
domain, this share is blocked until a Workspace admin allows it
(admin.google.com → Apps → Google Workspace → Drive and Docs → Sharing
settings) — the service account's address is, by definition, outside the
Workspace's domain. Another admin-console blocker, not something `doctor`
can see or fix.

## 6. `ia configure`

The `ia` CLI needs to be authenticated, once, against the shared org account
(`admin@lcpsociety.org`) — no environment variables, no per-user credentials.

```bash
ia configure
```

This is interactive: it prompts for the org account's email and password
(and, if the account has one, a 2FA code) and writes them to a config file. Do
this once, as whichever account will run the pipeline day to day.

**Where it writes.** Reading `internetarchive`'s own source
(`internetarchive/config.py`, `parse_config_file()` — this repo pins
`internetarchive>=5.0`), the config file is chosen in this order, and the
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
# expect: -rw-------  ...  ia.ini
```

If nothing is there, `ia whoami` will tell you where it actually looked.

`doctor` covers this from Python's side with two checks, using
`internetarchive`'s own path-precedence logic rather than a second copy of the
list above:

- **`ia credentials`** — `PASS` naming the resolved path when the file is there
  and carries both S3 keys; `FAIL` with `ia configure` as the remedy when it is
  absent, unparseable, or missing a key. It reads presence only: the key values
  are never printed, logged, or sent anywhere, and no call is made to Internet
  Archive. A credential that exists but has been revoked at archive.org still
  reports `PASS` — only a real run can tell you that.
- **`ia credentials permissions`** — the same `0600` scrutiny the Google key
  gets, `UNKNOWN` where the platform has no POSIX permissions. It has no
  automatic fix: the file lives outside the checkout, so `setup` reports on it
  rather than chmodding someone's home directory.

## 7. `files_dir` and the LaCie drive

`files_dir` in `projects_registry.json` must point at wherever the project's
photos actually live on this machine.

1. Plug in the LaCie drive and confirm its mount path, e.g. `ls /Volumes`.
2. Open `projects_registry.json` and set the `<project>` entry's `files_dir`
   to that path (or a path under it).
3. `doctor`'s `photo drive` check reports `UNKNOWN` (not `FAIL`) if the path
   doesn't exist — the drive being unplugged means "could not tell what's on
   it," not "broken." See §15.

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
python ia_bulk.py doctor --project <project> --live
```

Without `--live` the same run checks `test spreadsheet ID` instead and says
nothing at all about the real Sheet.

## 9. The sync columns

`sync-metadata` needs two tool-owned columns on the real Sheet beyond
`upload`'s own four: **`ia_sync_hash`** and **`ia_last_synced`**. Add both as
headers on the Sheet, then hide both columns — they're bookkeeping, not
something a cataloguer should be editing by hand.

```bash
python ia_bulk.py doctor --project <project> --live
```

`doctor`'s `sync state columns` check fails, naming whichever is missing, until
both exist — and fails again if a Sheet ends up with two headers that both
normalize to one of these names, since the second would be silently ignored.
As in §8, this check reads the Sheet the run names: **without `--live` it reads
the test Sheet**, and a `PASS` there says nothing about the real one.

## 10. Install

```bash
./install.sh --project <project>
```

This is the one command that brings a machine up to date, whether it's the
first run on an empty Mac or the twentieth. In order, it:

1. Finds a Python 3.10+ on `PATH` (§3), refusing if none qualifies.
2. Creates `.venv` if it doesn't already exist.
3. Installs/upgrades `pip`, then installs `requirements.txt` into `.venv`.
4. Runs `ia_bulk.py setup --project <project>`, which converges every check
   in `deployment.py` it can fix on its own (key file permissions, the
   LaunchAgent plist's contents) and then re-verifies everything, printing a
   `[PASS]`/`[FAIL]`/`[UNKNOWN]` report.

**Safe to re-run.** Running it again on a machine that already matches the
checkout prints `nothing to change; this machine already matches the
checkout.` and the same report — it does not undo or duplicate anything.

`setup` refuses outright, before running any check, if `--enable-agent` is
passed without `--live` or together with `--offline` (§12). Both refusals print
the command to run instead. There is no override flag.

### If it fails partway

If `.venv` creation fails partway through (disk full, interrupted), the next
`./install.sh` sees the directory already exists, skips recreating it, and
then fails less clearly at the `pip install` step against a broken venv. If
`install.sh` fails and you're not sure why, the safe recovery is:

```bash
rm -rf .venv
./install.sh --project <project>
```

## 11. Credentials: where, what they grant, how to rotate

| Credential | Path | Grants | Rotate |
|---|---|---|---|
| Google service-account key | `.ignored/google-service-account.json` | Read/write on whatever Sheets are shared with its address as Editor — nothing else; it has no Google Cloud project roles (§4) | Create a new key on the service account's **Keys** tab in the Cloud console, replace the file, then delete the old key on that same tab |
| `ia` config (§6) | `~/.config/internetarchive/ia.ini` (confirm on-machine) | Full access to the shared org Internet Archive account — upload, edit metadata, delete | Re-run `ia configure` with the org credentials; if the org password itself is rotated, do this immediately after |

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

**It enables nothing if any check failed.** `setup` converges, re-checks, and
only then loads the agent. A single `[FAIL]` line — a missing key, a
placeholder sheet id, absent sync columns — prints the report, says the agent
was **not** enabled, and exits non-zero. There is no `--force`.

**And nothing if the live Sheet could not be checked.** `spreadsheet
reachable` and `sync state columns` must come back `PASS` here, not `UNKNOWN`.
A machine that simply has no working network — install day on someone else's
wifi — turns both into "could not tell", which everywhere else is not a
failure. For this one command it is: enabling on it would start an hourly live
sync against a Sheet whose id, sharing and sync columns were never confirmed,
which is exactly what refusing `--offline` is for. `setup` says which check
could not be verified and loads nothing.

`UNKNOWN` on any **other** check still does not block — the LaCie drive being
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
`--enable-agent` says so, boots it out, and bootstraps the new definition —
which means another immediate live sync. If `launchctl` refuses either step,
`setup` exits non-zero and says the agent was not loaded, rather than reporting
success it cannot confirm.

To check it after enabling — `--live`, because that is the Sheet the agent
you just started is syncing:

```bash
python ia_bulk.py doctor --project <project> --live
tail -20 logs/launchagent-<project>.err
```

## 13. Uninstalling the agent

```bash
launchctl bootout gui/$(id -u)/org.lcpsociety.iabulk.sync.<project>
rm ~/Library/LaunchAgents/org.lcpsociety.iabulk.sync.<project>.plist
```

Run this as the account the agent is loaded for. `doctor` will then report
the `launch agent loaded` check as `UNKNOWN` (not loaded, or no session for
this account — it can't tell which) and `launch agent plist` as `FAIL` until
`./install.sh` (§10) writes the plist again.

**If `./install.sh` was ever run from another account**, that account has its
own never-loaded plist, because every run writes one into the home of whoever
ran it (§2). Log in there and remove it:

```bash
rm ~/Library/LaunchAgents/org.lcpsociety.iabulk.sync.<project>.plist
```

No `launchctl bootout` is needed for that copy — it was never loaded. Follow
§2 and this situation does not arise.

## 14. Upgrading

```bash
git pull
./install.sh --project <project>
```

The same command as install day — there is no separate upgrade path. It
converges whatever the new checkout needs (new dependencies, a changed plist
if `launch_agent.py` changed) and leaves an already-loaded agent loaded.

**If the plist changed, the running agent is still the old one.** launchd holds
its own copy from the moment it was bootstrapped; rewriting the file does not
reach it, and `doctor` will report both `launch agent plist` and `launch agent
loaded` as `PASS` while the job on the machine executes the previous command.
To make a changed plist take effect, re-run §12 from the operating account —
it boots the agent out and bootstraps the new definition:

```bash
./install.sh --project <project> --live --enable-agent
```

That starts another live sync immediately, the same as the first time.

## 15. Checking a machine later

```bash
python ia_bulk.py doctor --project <project>
```

Read-only — it changes nothing on the machine, in the Sheet, or on Internet
Archive. Run it any time something seems off, after replacing a credential,
or as a routine check.

Each check reports one of three states, never just pass/fail:

- **`PASS`** — confirmed working.
- **`FAIL`** — confirmed broken; the line below it names the fix. `doctor`
  exits non-zero if any check fails.
- **`UNKNOWN`** — could not tell, not "broken." The drive being unplugged, no
  network, or a permission model `doctor` can't express (Windows, for
  testing) all report `UNKNOWN` rather than `FAIL`. `doctor` exits **0** when
  every failing check is `UNKNOWN` — conflating "couldn't check" with
  "broken" would make the report untrustworthy on exactly the days (no
  network, drive unplugged) when you most need to trust it.

`doctor` checks the mode it is given, so on a machine that is running live
traffic, check the live side too — without `--live` it reports on the test
Sheet and the test sheet id:

```bash
python ia_bulk.py doctor --project <project> --live
```

Pass `--offline` to skip the checks that need the network (`spreadsheet
reachable`, `sync state columns`) entirely, e.g. when checking a machine that
happens to be offline right now.

### If `launch agent plist` still says FAIL

In order of likelihood:

1. **`./install.sh` has never been run for this account.** `doctor` only
   reports; it never writes the plist. This is the ordinary first-run state,
   and §10 is the whole answer:

   ```bash
   ./install.sh --project <project>
   ```

2. **The file exists but does not match this checkout** — a `git pull` changed
   what the agent should run and `./install.sh` has not been run since. Run §14.
3. **You are looking at the wrong home** (§2). `doctor` run from the admin
   account reports on a plist nothing loads. Log in as the operating account
   and check again.
4. **The write was tried and failed.** This one applies only after
   `./install.sh` (which converges the plist) has just run and the check still
   says `FAIL` — usually `~/Library/LaunchAgents` is not writable by the
   account running the script. Confirm you are the operating account (§2) and
   re-run §10.

Whatever the cause, the plist is regenerated from the checkout every time, so
deleting it is safe: `rm ~/Library/LaunchAgents/org.lcpsociety.iabulk.sync.<project>.plist`
and re-run §10.

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
   python ia_bulk.py validate --project <project> < /dev/null
   ```

2. **Preview a sync.** Expect the usual summary line, for example
   `10 uploaded rows; … 10 already in sync and would not be sent`:

   ```bash
   python ia_bulk.py sync-metadata --project <project> --dry-run < /dev/null
   ```

3. **See the failure message once.** Move the key aside, run `validate`, and
   expect a single line starting `could not authenticate to Google Sheets:
   missing service account key at …` with no traceback. Then put the key back:

   ```bash
   mv .ignored/google-service-account.json .ignored/google-service-account.json.off
   python ia_bulk.py validate --project <project>
   mv .ignored/google-service-account.json.off .ignored/google-service-account.json
   ```

For a full round trip — a real edit reaching Internet Archive, and the Sheet's
version history showing the service account as its editor — follow
[4. Corrections](OPERATIONS.md#4-corrections) against the test Sheet: change
one uploaded row's title, sync, check the item, then change it back and sync
again.
