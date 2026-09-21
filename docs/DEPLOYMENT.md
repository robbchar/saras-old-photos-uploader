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

## 2. The two accounts

Two macOS accounts are involved, and they play different roles:

- **The installing account** — whoever sets the machine up (Robb, on-site).
  This account runs `./install.sh`, creates the service-account key, and
  authenticates `ia`.
- **The operating account** — the shared account the pipeline actually runs
  under day to day. At handover, the installer `chown`s the checkout to this
  account so its ownership matches who runs it.

> **The single easiest thing to get wrong on install day:** a macOS
> LaunchAgent is per-user. A plist placed in `~/Library/LaunchAgents` only
> loads for the account it belongs to, and it only fires while that account
> has an active login session — not merely while the Mac is powered on. That
> means:
>
> - `--enable-agent` (§12) must be run **from the operating account**, after
>   logging in as it — not from the installing account, and not by `sudo`-ing
>   into it.
> - The operating account needs **auto-login enabled** in System Settings, and
>   needs to **stay logged in** (fast user switching away from it is fine;
>   logging all the way out is not).
>
> Get this wrong and `doctor` will report the agent as `UNKNOWN` or missing
> even though everything else converged correctly — see §15.

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
`--live` command is refused while this is still a placeholder — `doctor`'s
`live spreadsheet ID` check fails until it's set.

## 9. The sync columns

`sync-metadata` needs two tool-owned columns on the real Sheet beyond
`upload`'s own four: **`ia_sync_hash`** and **`ia_last_synced`**. Add both as
headers on the Sheet, then hide both columns — they're bookkeeping, not
something a cataloguer should be editing by hand. `doctor`'s `sync state
columns` check fails, naming both, until they exist.

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
./install.sh --project <project> --enable-agent
```

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

To check it after enabling:

```bash
python ia_bulk.py doctor --project <project>
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

## 14. Upgrading

```bash
git pull
./install.sh --project <project>
```

The same command as install day — there is no separate upgrade path. It
converges whatever the new checkout needs (new dependencies, a changed plist
if `launch_agent.py` changed) and leaves an already-loaded agent loaded.

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

Pass `--offline` to skip the checks that need the network (`spreadsheet
reachable`, `sync state columns`) entirely, e.g. when checking a machine that
happens to be offline right now.
