# CI and releases

GitHub Actions runs every automated check on each pull request to `main` and
on each push to `main`. The workflows are in `.github/workflows/`.

## What runs

| Check | Where | Commands |
|---|---|---|
| `python-tests` | Ubuntu and macOS × Python 3.10 and 3.14 | `python -m pytest` |
| `python-static` | Ubuntu, Python 3.10 | `python -m ruff check .`, `python -m pyright` |
| `upload-page` | Ubuntu, Node from `upload_page/.node-version` | `yarn install --immutable`, `yarn typecheck`, `yarn test` |
| `pr-title` | Ubuntu, pull requests only | `python pr_title.py "<title>"` |

They are the same commands as
[`OPERATIONS.md`, "Development"](OPERATIONS.md#development) and
[`upload_page/README.md`](../upload_page/README.md), so a local run predicts CI.

- **No secrets, no network.** The suite is offline (`conftest.py`'s network
  guard) and the e2e rehearsal is skipped without `--run-e2e`, which CI never
  passes. Run the rehearsal by hand before a release that touches the Sheet
  or IA paths.
- **`dist/` is checked by pytest.** `test_committed_bundle_is_current` fails
  when `upload_page/dist/` is stale, so CI needs no `yarn build`.
- **No path filters.** Every job runs on every PR; a required check that is
  skipped would block the merge.
- **macOS** covers the deployment target. Windows (the dev box) is covered by
  local runs.

## Required checks

`main` requires `python-tests` (all four matrix entries), `python-static`,
`upload-page` and `pr-title` to pass before a PR can merge. An admin can
override. The rule is a repository ruleset named `main checks`.

## Pinned versions

- `ruff` and `pyright` are pinned exactly in `requirements.txt`, so a new
  release can't redden an unrelated PR.
- `ruff.toml` holds ruff's rule selection at its pre-0.16 default (`E4`,
  `E7`, `E9`, `F`). ruff 0.16 turned on a much larger default set; widening
  the rules is its own change, not a side effect of an upgrade.
- Node is pinned in `upload_page/.node-version` (read by CI and by local
  version managers). Yarn comes from `packageManager` via corepack.
- Dependabot opens one grouped PR per ecosystem each week (`pip`, `npm`,
  `github-actions`), titled `chore(deps…)`, so it never triggers a release.
  Retitle to `fix(deps): …` before merging if a runtime dependency change
  should ship.
- `internetarchive` is excluded from Dependabot: its exact pin is deliberate
  (see [`decisions/QUOTA-AND-RUNS.md`](decisions/QUOTA-AND-RUNS.md)).
- **A Dependabot `npm` PR fails `python-tests` until `dist/` is rebuilt.**
  `package.json` and `yarn.lock` are build-stamp inputs. Check out its branch,
  run `yarn build` in `upload_page/`, commit `dist/`, and push.

### Bumping

- `ruff`, `pyright` and the GitHub Actions versions arrive through
  Dependabot PRs.
- `upload_page/.node-version` and the Python matrix in
  `.github/workflows/ci.yml` are edited by hand.
- Node 25 and later no longer bundle corepack, so moving `.node-version`
  past 24 means replacing the `corepack enable` step in `ci.yml`.

## Releasing

The version is the single line in `version.txt`. Only the release PR changes
it. `app_version.py` reads it for `--version`, `doctor`, `setup` and
`GET /api/status`.

1. **Every PR title declares its bump**, as a Conventional Commit:
   - `fix: …`, `perf: …`, `revert: …` — patch
   - `feat: …` — minor
   - `feat!: …` (any type with `!`) or a `BREAKING CHANGE: …` line in the
     PR description — major
   - `test`, `ci`, `docs`, `chore`, `refactor`, `build`, `style` — no
     release
   - `Release-As: 2.0.0` on its own line in the PR description forces an
     exact number.

   The `pr-title` check enforces the format; choosing the right type is on
   the author. GitHub titles a revert PR `Revert "…"`, which the check
   rejects; retitle it `revert: …`.
2. **After a releasable merge**, release-please opens (or updates) one PR,
   `chore(main): release X.Y.Z`. It changes only `version.txt` and
   `.release-please-manifest.json`. The number is the last release plus the
   largest pending bump, whatever order the PRs merged in.
3. **Merging the release PR** tags `vX.Y.Z` and publishes a GitHub Release
   with notes from the included PRs.

Merge the release PR right after each feature to release one version per
change, or let it collect several. Either way, **merge any pending release
PR before updating the Mac**: the Mac runs `main`, and `setup` only prints
`updating from X to Y` when `version.txt` changed.

Squash commits are built from the PR title and description (repository
setting), which is how the title's type and any `Release-As`/`BREAKING CHANGE`
line reach `main`.

Because the description becomes the commit body, a line in it that starts
with `Release-As:` or `BREAKING CHANGE:` is read as a release instruction.
Mention those words mid-sentence or in code spans when you only mean to talk
about them.

## The release token

`release.yml` runs release-please with the repository secret
`RELEASE_PLEASE_TOKEN`, a fine-grained personal access token. The default
Actions token can't be used: pull requests it opens don't trigger workflows,
so the release PR would never get CI and could never pass the required
checks.

- Owner `robbchar`, repository access: this repository only
- Permissions: Contents, Issues, Pull requests — read and write
- **Expires: YYYY-MM-DD** (update this line when renewing)

When it expires, the `Release` workflow fails on the next push to `main`.
Nothing else breaks. Renew it with the same permissions, then
`gh secret set RELEASE_PLEASE_TOKEN -R robbchar/saras-old-photos-uploader`,
and re-run the failed `Release` run.
