"""Every call that cannot be exercised off macOS. Kept in one file so the logic
above it stays testable on Windows and only this seam is unverifiable."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_LAST_EXIT = re.compile(r"last exit code\s*=\s*(\d+)")
_PID = re.compile(r"^\s*pid\s*=\s*(\d+)", re.MULTILINE)


def current_user() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def file_mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def file_owner(path: Path) -> str | None:
    try:
        import pwd  # macOS/Linux only; absent on Windows.

        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (ImportError, KeyError, OSError):
        return None


def set_file_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def is_readable_directory(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.R_OK)


def parse_last_exit(output: str) -> int | None:
    match = _LAST_EXIT.search(output)
    return int(match.group(1)) if match else None


def parse_pid(output: str) -> int | None:
    match = _PID.search(output)
    return int(match.group(1)) if match else None


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, check=False
    )


def launchctl_print(label: str) -> str | None:
    """None means not loaded, or no launchctl at all - the caller reports UNKNOWN."""
    try:
        result = _launchctl("print", f"gui/{os.getuid()}/{label}")
    except (OSError, AttributeError):
        return None
    return result.stdout if result.returncode == 0 else None


def launchctl_bootstrap(plist_path: Path) -> str:
    result = _launchctl("bootstrap", f"gui/{os.getuid()}", str(plist_path))
    if result.returncode == 0:
        return f"loaded {plist_path.name}"
    return f"launchctl bootstrap failed: {result.stderr.strip()}"


def launchctl_bootout(label: str) -> str:
    result = _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    if result.returncode == 0:
        return f"unloaded {label}"
    return f"launchctl bootout failed: {result.stderr.strip()}"
