# CI and releases

## CI has no secrets and never runs the e2e rehearsal

*Decided 2026-09-30.* The CI jobs run the offline suite only. The rehearsal
needs the service-account key and a real `ia configure` login, and it writes
to the Test Sheet and IA's `test_collection`. Putting those credentials in a
public repository's Actions secrets would expose the Sheet and the org's IA
account to any workflow change. The network guard makes "offline" a checked
property, not a convention.

The one secret in the repository, `RELEASE_APP_PRIVATE_KEY`, is used only by
`release.yml`. It can't reach the Sheet or Internet Archive, but it is not
narrow. It mints tokens for the App `lcps-uploader-release`, whose Contents
write lets it push to any branch the rulesets don't guard. The App is not a
bypass actor in `main checks`, so a leaked key can't push straight to `main`.
A `pull_request` run from a branch in this repository (not a fork's or
Dependabot's) can read it too. Replacing it:
[`CI.md`, "The release token"](../CI.md#the-release-token).

## The version lives in `version.txt` and changes only in the release PR

*Decided 2026-09-30.* Bumping `APP_VERSION` inside each feature PR made the
number depend on merge order: two PRs opened from the same `main` both
claimed the same next version, and the tag was a separate manual step that
slipped (`v1.0.0` and `v1.1.0` were both tagged after the fact).

Now a PR declares its bump through its Conventional Commit title, and
release-please computes the number from everything merged since the last
release. The version sits in a plain `version.txt` so the release PR changes
data, never Python. `app_version.py` reads it at import and refuses to start
without a valid `X.Y.Z`.

Releasing is merging the release PR. release-please never pushes to `main`,
so the required checks apply to releases too.

Considered and rejected:

- **Tagging automatically when a merge changes `APP_VERSION`.** It kept the
  merge-order conflict.
- **Labels plus a custom release script.** It worked, but it duplicated
  release-please, and the PR titles were already Conventional Commits.
- **A bot committing the bump straight to `main`.** It needs a bypass of the
  required checks, and the numbers depend on merge order again.

## Releases are authored by a GitHub App, not a person

*Decided 2026-10-02.* release-please first ran with a fine-grained personal
access token, so the release PR, its commit and the 1.1.1 GitHub Release
were all attributed to the token's owner as if he had written them. GitHub
sends no notification about your own actions, so the first release PR also
appeared unannounced. Now each run mints a token from the GitHub App
`lcps-uploader-release`, and `lcps-uploader-release[bot]` is the author. The
tag itself records no author either way.

The switch also narrows a leak. The personal token acted as an admin whom
`main checks` lets bypass the required checks; the App is no bypass actor, and
each token it mints expires within an hour. The App's private key, like the
personal token, never expires.

A bot author alone doesn't announce the release PR to the owner: GitHub
notifies watchers, so the owner watches the repository's pull requests.

Considered and rejected:

- **Keeping the personal token and documenting where the release PR
  appears.** It fixed finding the PR, but not who it claims wrote it.
