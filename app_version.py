# Semantic version of the whole tool (CLI, server, page); release flow in README.md "Versioning".
import re
from pathlib import Path

SEMVER_PATTERN = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")

# Changed only by the release-please release PR; see docs/CI.md.
VERSION_FILE_PATH = Path(__file__).resolve().parent / "version.txt"


def read_version_file(version_path: Path) -> str:
    try:
        version = version_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError(f"cannot read the app version from {version_path}: {error}") from error
    if not SEMVER_PATTERN.fullmatch(version):
        raise RuntimeError(f"{version_path} must hold a plain X.Y.Z version, found {version!r}")
    return version


APP_VERSION = read_version_file(VERSION_FILE_PATH)

# The version the last `setup` ran against in this checkout; logs/ is gitignored.
INSTALLED_VERSION_PATH = Path(__file__).resolve().parent / "logs" / "installed-version"


def read_installed_version(marker_path: Path) -> str | None:
    """None on a first setup, or when the marker is unreadable or not a version."""
    try:
        recorded = marker_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return recorded if SEMVER_PATTERN.fullmatch(recorded) else None


def record_installed_version(marker_path: Path) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(f"{APP_VERSION}\n", encoding="utf-8", newline="\n")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def update_line(previous_version: str | None) -> str | None:
    if previous_version is None or previous_version == APP_VERSION:
        return None
    direction = "updating" if _version_key(previous_version) < _version_key(APP_VERSION) else "downgrading"
    return f"{direction} from {previous_version} to {APP_VERSION}"
