"""Computes a content-hash "stamp" for the committed upload-page bundle.

The stamp lets the server (and the Mac `doctor` check) detect a stale
`dist/` build without re-running the front-end build: it hashes the exact
set of files that feed `yarn build`, so any edit to those inputs changes the
stamp, and `yarn build` writes the freshly computed stamp into
`dist/build-stamp.json` for later comparison.

This module is the Python half of a cross-language contract: the Node
build script `scripts/build-stamp.mjs` (added in a later task) must
reproduce STAMP_ALGORITHM byte-for-byte, so both sides agree on the stamp
for the same source tree without ever talking to each other.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

STAMP_ALGORITHM = (
    "build-stamp-v1: sha256 over sorted (relpath, sha256(LF-normalized "
    "bytes)) pairs for index.html, package.json, yarn.lock, tsconfig.json, "
    "vite.config.ts, vitest.config.ts, and everything under src/"
)

_TOP_LEVEL_INPUT_FILES = (
    "index.html",
    "package.json",
    "yarn.lock",
    "tsconfig.json",
    "vite.config.ts",
    "vitest.config.ts",
)


def _input_files(page_dir: Path) -> list[Path]:
    """Every build-input file under page_dir, per STAMP_ALGORITHM.

    Top-level files are included only if present (a fixture or a real page
    directory may be missing one); every file under src/ is included
    recursively. dist/, node_modules/, and dotfile directories are never
    walked into (src/ is the only directory walked, so this mostly matters
    for future-proofing if src/ ever nests one of those names).
    """
    files: list[Path] = []
    for name in _TOP_LEVEL_INPUT_FILES:
        candidate = page_dir / name
        if candidate.is_file():
            files.append(candidate)

    src_dir = page_dir / "src"
    if src_dir.is_dir():
        for path in src_dir.rglob("*"):
            if not path.is_file():
                continue
            if _is_excluded(path.relative_to(page_dir)):
                continue
            files.append(path)

    return files


def _is_excluded(relpath: Path) -> bool:
    parts = relpath.parts
    if "dist" in parts or "node_modules" in parts:
        return True
    # Any dotfile directory component (e.g. .git, .cache) excludes the file.
    # The file's own final component is not itself a directory, so it is
    # not checked here -- only the directories it lives under.
    return any(part.startswith(".") for part in parts[:-1])


def _posix_relpath(path: Path, page_dir: Path) -> str:
    return PurePosixPath(path.relative_to(page_dir).as_posix()).as_posix()


def _file_hash(path: Path) -> str:
    raw = path.read_bytes()
    normalized = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def compute_build_stamp(page_dir: Path) -> str:
    """sha256 hex stamp over the front-end build inputs under page_dir.

    See STAMP_ALGORITHM and the module docstring for the exact algorithm;
    this must match scripts/build-stamp.mjs byte-for-byte.
    """
    files = _input_files(page_dir)
    files.sort(key=lambda p: _posix_relpath(p, page_dir))

    manifest_parts: list[str] = []
    for path in files:
        relpath = _posix_relpath(path, page_dir)
        filehash = _file_hash(path)
        manifest_parts.append(f"{relpath}\n{filehash}\n")

    manifest = "".join(manifest_parts)
    return hashlib.sha256(manifest.encode("utf-8")).hexdigest()


def read_committed_stamp(page_dir: Path) -> str | None:
    """The `stamp` field of <page_dir>/dist/build-stamp.json, or None.

    Guarded against every way this can fail to exist or be well-formed: no
    dist/ dir, no build-stamp.json, unreadable file, invalid JSON, or a
    JSON value that isn't an object with a string `stamp` field.
    """
    stamp_path = page_dir / "dist" / "build-stamp.json"
    try:
        data = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    stamp = data.get("stamp") if isinstance(data, dict) else None
    return stamp if isinstance(stamp, str) else None


def bundle_is_current(page_dir: Path) -> bool:
    """True when the committed dist/ bundle's stamp matches its source."""
    committed = read_committed_stamp(page_dir)
    if committed is None:
        return False
    return committed == compute_build_stamp(page_dir)
