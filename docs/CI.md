# CI and releases

GitHub Actions runs every automated check on each pull request to `main` and
on each push to `main`. The workflows are in `.github/workflows/`.

## What runs

| Check | Where | Commands |
|---|---|---|
| `python-tests` | Ubuntu and macOS, Python 3.12 (the Mac's, see [`DEPLOYMENT.md`, "Python"](DEPLOYMENT.md#3-python)) | `python -m pytest` |
| `python-static` | Ubuntu, Python 3.10 | `python -m ruff check .`, `python -m pyright` |
| `upload-page` | Ubuntu, Node from `upload_page/.node-version` | `yarn install --immutable`, `yarn typecheck`, `yarn test` |
| `ci-passed` | Ubuntu, after the three above | none; fails unless `python-tests`, `python-static` and `upload-page` all passed |
| `pr-title` | Ubuntu, pull requests only, in its own `pr-title.yml` so a title or description edit reruns only it | `python pr_title.py "<title>"` |

They are the same commands as
[`OPERATIONS.md`, "Development"](OPERATIONS.md#development) and
[`upload_page/README.md`](../upload_page/README.md), so a local run predicts CI.

- **No secrets, no network.** The suite is offline (`conftest.py`'s network
  guard) and the e2e rehearsal is skipped without `--run-e2e`, which CI never
  passes. Run the rehearsal by hand before a release that touches the Sheet
  or IA paths.
- **`dist/` is checked by pytest.** `test_committed_bundle_is_current` fails
  when `upload_page/dist/` is stale, so CI needs no `yarn build`.
- **No path filters.** Every job runs on every PR. A workflow that a path
  filter skips reports nothing, so `ci-passed` would wait forever; a job
  skipped by its own `if:` fails `ci-passed`.
- **macOS** covers the deployment target. Windows (the dev box) is covered by
  local runs.

## Required checks

`main` requires `ci-passed` and `pr-title` to pass before a PR can merge. An
admin can override. The rule is a repository ruleset named `main checks`;
renaming either job means updating it.

`ci-passed` stands in for the jobs it needs, so adding or renaming a matrix
entry never touches the ruleset. It runs with `if: always()` and fails unless
every job it needs passed: a skipped required check would count as passing.
A new job gates merges only once it is in `ci-passed`'s `needs`.

Retargeting a PR to `main` runs only `pr-title`; push a commit, or close and
reopen the PR, to run CI.

## Pinned versions

- `ruff` and `pyright` are pinned exactly in `requirements.txt`, so a new
  release can't redden an unrelated PR.
- `ruff.toml` holds ruff's rule selection at its pre-0.16 default (`E4`,
  `E7`, `E9`, `F`). ruff 0.16 turned on a much larger default set; widening
  the rules is its own change, not a side effect of an upgrade.
- Node is pinned in `upload_page/.node-version` (read by CI and by local
  version managers). Yarn comes from `packageManager` via corepack.
- Dependabot opens one grouped PR per ecosystem each week (`pip`, `npm`,
  `github-actions`), titled `chore(deps…)`, so its title never triggers a
  release. Its description quotes upstream release notes, though, which can
  trigger one (see [Releasing](#releasing)), so clear the extended
  description in the merge dialog before squashing it. Retitle to `fix(deps): …` before merging if a
  runtime dependency change should ship.
- `internetarchive` is excluded from Dependabot: its exact pin is deliberate
  (see [`decisions/QUOTA-AND-RUNS.md`](decisions/QUOTA-AND-RUNS.md)).
- **A Dependabot `npm` PR fails `python-tests` until `dist/` is rebuilt.**
  `package.json` and `yarn.lock` are build-stamp inputs. Check out its branch,
  run `yarn build` in `upload_page/`, commit `dist/`, and push.

### Bumping

- `ruff`, `pyright` and the GitHub Actions versions arrive through
  Dependabot PRs.
- `upload_page/.node-version` and the Python matrix in
  `.github/workflows/ci.yml` are edited by hand. Keep the matrix on the
  Python the Mac runs. The oldest supported version, 3.10, is covered by
  `python-static`: pyright checks against it.
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
   - `Release-As: 2.0.0` on its own line at the end of the PR description
     forces an exact number.

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

Because the description becomes the commit body, release-please parses it
too, and some text in it releases:

- **`BREAKING-CHANGE:`** (hyphen) makes a major release anywhere in the
  description, even inside a code span or quoted HTML. Only leaving the token
  out is safe.
- **`BREAKING CHANGE:`** (space) makes a major release at the start of a
  line, even in a code fence. Mid-sentence or in a code span, it is just text.
- **A line starting with a lowercase type and `: `** (`fix: …`,
  `feat(x): …`) after a blank line counts as one more commit. So does, in any
  case, a `Word: …` line followed only by blank lines, indented lines and
  more `Word: …` or `Closes #12` lines (`Fix: …` releases a patch).

`Release-As:` counts only in that closing run. A `- ` bullet keeps a
`BREAKING CHANGE:` or type line inert; an indent does too, except inside
that run.

## The release token

`release.yml` runs release-please with the repository secret
`RELEASE_PLEASE_TOKEN`, a fine-grained personal access token. The default
Actions token can't be used: pull requests it opens don't trigger workflows,
so the release PR would never get CI and could never pass the required
checks.

- Owner `robbchar`, repository access: this repository only
- Permissions: Contents, Issues, Pull requests — read and write
- **Expires: never** (created 2026-09-30)

It has no expiry, so it stays valid until revoked. If it may have leaked, or
its owner loses access, revoke it and create a replacement with the same
permissions. Store the new one with
`gh secret set RELEASE_PLEASE_TOKEN -R robbchar/saras-old-photos-uploader`.
Until then, the `Release` workflow fails on each push to `main`; nothing else
breaks. Re-run the failed `Release` run once the secret is replaced.
