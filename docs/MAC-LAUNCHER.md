# One-click launch: the upload page on the Mac

A double-click launcher — and an optional Dock button — that starts the
upload page in **LIVE mode** and opens it in the browser. It is the manual
half of deployment "piece 6": a person clicks it to run the page, as
opposed to the hourly sync agent (see [`DEPLOYMENT.md`](DEPLOYMENT.md) §12),
which runs on its own in the background.

The launcher is [`start-upload-page-live.command`](../start-upload-page-live.command)
at the checkout root. It runs:

```
.venv/bin/python ia_bulk.py serve --project sarasoldphotos --registry projects_registry.json --live --port 5277
```

then opens `http://127.0.0.1:5277` once the server is listening. Closing the
Terminal window stops the server.

## What this Mac needs (and does not)

- **The Python venv**, from `./install.sh` — see [`DEPLOYMENT.md`](DEPLOYMENT.md) §10.
- **A current checkout.** The built page bundle (`upload_page/dist`) is
  committed to the repo, so `git pull` brings it along. This Mac never runs
  `yarn` or Node — the bundle is built and committed by a developer before
  deployment.
- **The real `sheet_id`.** Until it is set (see [`DEPLOYMENT.md`](DEPLOYMENT.md)
  §8), the server still starts and the page still loads, but a validate or
  upload from the page fails with a placeholder-sheet-id message. That is
  expected before go-live, not a launcher bug.

Bring the checkout up to date before a run:

```bash
cd <checkout>
git pull
```

If the launcher is not executable after a fresh clone, mark it once:

```bash
chmod +x ./start-upload-page-live.command
```

## Putting it in the Dock

Two ways. The first needs no setup; the second gives a real app icon.

### Simple: drag the file to the Dock

Drag `start-upload-page-live.command` onto the **right-hand side** of the
Dock (the side nearest the Trash, where files and folders live). Click it to
launch. Nothing to configure — the script finds its own checkout.

### Nicer: wrap it in an app

An app pins to the main (left) side of the Dock and can carry a custom icon.

1. Open **Script Editor** (Applications -> Utilities).
2. New document, and paste this, replacing the path with your checkout's
   full path:

   ```applescript
   set launcher to "<checkout>/start-upload-page-live.command"
   tell application "Terminal"
       activate
       do script quoted form of launcher
   end tell
   ```

3. **File -> Export**, set **File Format: Application**, name it something
   like `LCPS Upload Page`, and save it to `Applications`.
4. Open the app once to confirm it launches the page, then drag it from
   `Applications` into the Dock.
5. Optional: to change its icon, select the app in Finder, **File -> Get
   Info**, and drag an image onto the icon in the top-left of the Info
   window.

## Starting and stopping

- **Start:** click the Dock item (or double-click the `.command`). A Terminal
  window opens showing the **LIVE MODE** banner, and the browser opens to the
  page.
- **Stop:** close that Terminal window (or press Ctrl-C in it). The server
  stops with it.

## Verifying it on the Mac

Run through this once on the Mac after deploying (it cannot be tested off a
Mac):

- [ ] `git pull` succeeds and `upload_page/dist/build-stamp.json` exists.
- [ ] Clicking the launcher opens a Terminal window with the LIVE MODE banner.
- [ ] The browser opens to `http://127.0.0.1:5277` and the upload page loads.
- [ ] Closing the Terminal window stops the server: reopening
      `http://127.0.0.1:5277` no longer connects.
- [ ] Before the real `sheet_id` is set: the page loads, and a validate from
      the page reports a placeholder-sheet-id error (expected).
- [ ] After the real `sheet_id` is set: a validate from the page reads the
      Sheet without that error.
