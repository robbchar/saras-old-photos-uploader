from pathlib import Path

import build_stamp

FIXTURE = Path(__file__).resolve().parent / "build_stamp_fixture"

# Pinned so a later JS task (scripts/build-stamp.mjs) can assert it produces
# the same value over the same fixture tree -- drift between the two
# implementations fails a test instead of surfacing at runtime.
EXPECTED_FIXTURE_STAMP = (
    "9ce5f51497020ddc6be31a8afc13411500a3c5da0fdee86f1614b5d4506c01a2"
)


def test_compute_build_stamp_is_stable_and_crlf_insensitive(tmp_path):
    stamp = build_stamp.compute_build_stamp(FIXTURE)
    assert len(stamp) == 64 and all(c in "0123456789abcdef" for c in stamp)
    # Copy the tree with CRLF endings; the stamp must not change.
    dst = tmp_path / "crlf"
    for p in FIXTURE.rglob("*"):
        if p.is_file():
            rel = p.relative_to(FIXTURE)
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            (dst / rel).write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    assert build_stamp.compute_build_stamp(dst) == stamp


def test_fixture_stamp_is_pinned():
    assert build_stamp.compute_build_stamp(FIXTURE) == EXPECTED_FIXTURE_STAMP


def test_read_committed_stamp_missing_returns_none(tmp_path):
    # No dist dir at all under this page_dir.
    assert build_stamp.read_committed_stamp(tmp_path) is None


def test_bundle_is_current_true_when_written_stamp_matches(tmp_path):
    for p in FIXTURE.rglob("*"):
        if p.is_file():
            rel = p.relative_to(FIXTURE)
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_bytes(p.read_bytes())
    stamp = build_stamp.compute_build_stamp(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "build-stamp.json").write_text(
        '{"stamp": "%s"}' % stamp, encoding="utf-8"
    )
    assert build_stamp.bundle_is_current(tmp_path) is True


def test_bundle_is_current_false_when_stale(tmp_path):
    for p in FIXTURE.rglob("*"):
        if p.is_file():
            rel = p.relative_to(FIXTURE)
            (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp_path / rel).write_bytes(p.read_bytes())
    stamp = build_stamp.compute_build_stamp(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "build-stamp.json").write_text(
        '{"stamp": "%s"}' % stamp, encoding="utf-8"
    )
    # Mutate a src file after the stamp was written; the stamp is now stale.
    (tmp_path / "src" / "a.ts").write_bytes(b"export const a = 2\n")
    assert build_stamp.bundle_is_current(tmp_path) is False
