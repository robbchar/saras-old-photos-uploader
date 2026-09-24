"""Every call that cannot be exercised off macOS. Kept in one file so the logic
above it stays testable on Windows and only this seam is unverifiable."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
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
    if sys.platform == "win32":
        return None
    try:
        import pwd  # macOS/Linux only; absent on Windows.

        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (ImportError, KeyError, OSError):
        return None


def set_file_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)


def has_posix_permissions() -> bool:
    """Windows os.stat reports a fixed mode for every file, so a chmod-style
    check is meaningless there - only a POSIX platform can answer it."""
    return os.name == "posix"


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


NO_LAUNCHCTL = "launchctl is not available on this platform"


def _gui_domain() -> str | None:
    """The per-user launchd domain, or None where there is no getuid (Windows).
    One place, so all three launchctl calls behave the same off macOS."""
    getuid = getattr(os, "getuid", None)  # Dynamic lookup: tests fake getuid on Windows.
    return f"gui/{getuid()}" if getuid else None


def launchctl_print(label: str) -> str | None:
    """None means not loaded, or no launchctl at all - the caller reports UNKNOWN."""
    domain = _gui_domain()
    if domain is None:
        return None
    try:
        result = _launchctl("print", f"{domain}/{label}")
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def launchctl_bootstrap(plist_path: Path) -> tuple[bool, str]:
    """(loaded, message). Structural, not a message the caller has to read: the
    one command whose purpose is to load the agent has to be able to fail."""
    domain = _gui_domain()
    if domain is None:
        return False, NO_LAUNCHCTL
    try:
        result = _launchctl("bootstrap", domain, str(plist_path))
    except OSError as exc:
        return False, f"could not run launchctl ({exc})"
    if result.returncode == 0:
        return True, f"loaded {plist_path.name}"
    return False, f"launchctl bootstrap failed: {result.stderr.strip()}"


def launchctl_bootout(label: str) -> tuple[bool, str]:
    """(unloaded, message), for the same reason launchctl_bootstrap returns one."""
    domain = _gui_domain()
    if domain is None:
        return False, NO_LAUNCHCTL
    try:
        result = _launchctl("bootout", f"{domain}/{label}")
    except OSError as exc:
        return False, f"could not run launchctl ({exc})"
    if result.returncode == 0:
        return True, f"unloaded {label}"
    return False, f"launchctl bootout failed: {result.stderr.strip()}"


# Above launchd's default 20s ExitTimeOut, after which it SIGKILLs the job.
UNLOAD_TIMEOUT_SECONDS = 30.0
_UNLOAD_POLL_SECONDS = 0.5


def wait_until_unloaded(label: str, timeout: float = UNLOAD_TIMEOUT_SECONDS) -> bool:
    """True once launchctl no longer lists label. bootout can return while a
    running job is still exiting, and a bootstrap over it fails."""
    deadline = time.monotonic() + timeout
    while launchctl_print(label) is not None:
        if time.monotonic() >= deadline:
            return False
        time.sleep(_UNLOAD_POLL_SECONDS)
    return True
