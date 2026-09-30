# CI and releases

## CI has no secrets and never runs the e2e rehearsal

*Decided 2026-09-30.* The CI jobs run the offline suite only. The rehearsal
needs the service-account key and a real `ia configure` login, and it writes
to the Test Sheet and IA's `test_collection`. Putting those credentials in a
public repository's Actions secrets would expose the Sheet and the org's IA
account to any workflow change. The network guard makes "offline" a checked
property, not a convention.

The one secret in the repository, `RELEASE_PLEASE_TOKEN`, is used only by
`release.yml`. It can open pull requests and push tags on this repository.
It can't reach the Sheet or Internet Archive.

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

Releasing is merging the release PR. A bot never pushes to `main`, so the
required checks apply to releases too.

Considered and rejected:

- **Tagging automatically when a merge changes `APP_VERSION`.** It kept the
  merge-order conflict.
- **Labels plus a custom release script.** It worked, but it duplicated
  release-please, and the PR titles were already Conventional Commits.
- **A bot committing the bump straight to `main`.** It needs a bypass of the
  required checks, and the numbers depend on merge order again.
