# Semantic version of the whole tool (CLI, server, page); bump rules in README.md "Versioning".
import re
from pathlib import Path

APP_VERSION = "1.0.0"

SEMVER_PATTERN = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")

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


def update_line(previous_version: str | None) -> str | None:
    if previous_version is None or previous_version == APP_VERSION:
        return None
    return f"updating from {previous_version} to {APP_VERSION}"
