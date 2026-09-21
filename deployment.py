"""The deployment check list, shared by `ia_bulk.py doctor` and `ia_bulk.py setup`.

A check that can be converged on-machine carries a fix(); one that cannot carries
only a remedy and points at docs/DEPLOYMENT.md."""
from __future__ import annotations

import enum
import importlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import platform_probe
from project_config import ProjectConfig

# Set by google-auth/google-api-core, not by language syntax. macOS ships 3.9.6.
MINIMUM_PYTHON = (3, 10)


class Status(enum.Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CheckOutcome:
    status: Status
    detail: str


@dataclass(frozen=True)
class Check:
    name: str
    probe: Callable[[], CheckOutcome]
    remedy: str
    fix: Callable[[], str] | None = None


def _probe(check: Check) -> CheckOutcome:
    """A probe that raises could not tell, which is UNKNOWN - never FAIL."""
    try:
        return check.probe()
    except Exception as exc:  # noqa: BLE001 - any probe failure is "could not tell"
        return CheckOutcome(Status.UNKNOWN, f"could not check ({exc})")


def run_checks(checks: list[Check]) -> list[tuple[Check, CheckOutcome]]:
    return [(check, _probe(check)) for check in checks]


def converge(checks: list[Check], announce: Callable[[str], None]) -> list[tuple[Check, CheckOutcome]]:
    """Fix what can be fixed, then re-check. Silent about what was already correct."""
    results: list[tuple[Check, CheckOutcome]] = []
    for check in checks:
        outcome = _probe(check)
        if outcome.status is Status.FAIL and check.fix is not None:
            announce(f"{check.name}: {outcome.detail} - fixing")
            announce(f"  {check.fix()}")
            outcome = _probe(check)
        results.append((check, outcome))
    return results


def format_report(results: list[tuple[Check, CheckOutcome]]) -> str:
    lines: list[str] = []
    for check, outcome in results:
        lines.append(f"[{outcome.status.value}] {check.name}: {outcome.detail}")
        if outcome.status is Status.FAIL:
            lines.append(f"    fix: {check.remedy}")
    return "\n".join(lines)


def exit_code(results: list[tuple[Check, CheckOutcome]]) -> int:
    return 1 if any(outcome.status is Status.FAIL for _, outcome in results) else 0


KEY_MODE = 0o600
PLACEHOLDER_PREFIX = "REPLACE_WITH"

_REQUIRED_MODULES = (
    "internetarchive",
    "googleapiclient.discovery",
    "google.oauth2.service_account",
)


def dependencies_check() -> Check:
    def probe() -> CheckOutcome:
        missing = []
        for name in _REQUIRED_MODULES:
            try:
                importlib.import_module(name)
            except ImportError:
                missing.append(name)
        if missing:
            return CheckOutcome(Status.FAIL, f"not importable: {', '.join(missing)}")
        return CheckOutcome(Status.PASS, f"{len(_REQUIRED_MODULES)} packages importable")

    return Check(
        name="dependencies",
        probe=probe,
        remedy="./install.sh (or: .venv/bin/pip install -r requirements.txt)",
    )


def python_version_check(version: tuple[int, int]) -> Check:
    def probe() -> CheckOutcome:
        running = ".".join(str(part) for part in version)
        if version < MINIMUM_PYTHON:
            floor = ".".join(str(part) for part in MINIMUM_PYTHON)
            return CheckOutcome(Status.FAIL, f"running {running}, need {floor}+")
        return CheckOutcome(Status.PASS, running)

    return Check(
        name="python version",
        probe=probe,
        remedy="install Python 3.10+ and re-run ./install.sh - see docs/DEPLOYMENT.md",
    )


def key_present_check(key_path: Path) -> Check:
    def probe() -> CheckOutcome:
        if not key_path.exists():
            return CheckOutcome(Status.FAIL, f"no key at {key_path}")
        return CheckOutcome(Status.PASS, str(key_path))

    return Check(
        name="service account key",
        probe=probe,
        remedy=f"download the JSON key and save it as {key_path} - see docs/DEPLOYMENT.md",
    )


def key_mode_check(key_path: Path) -> Check:
    def probe() -> CheckOutcome:
        mode = platform_probe.file_mode(key_path)
        if mode is None:
            return CheckOutcome(Status.UNKNOWN, f"no key at {key_path} to check")
        owner = platform_probe.file_owner(key_path) or "unknown"
        # Owner is reported, never asserted: the checkout is chowned to the shared
        # account at handover, so a mismatch is news rather than an error.
        if mode != KEY_MODE:
            return CheckOutcome(Status.FAIL, f"mode {mode:04o}, owner {owner}, want {KEY_MODE:04o}")
        return CheckOutcome(Status.PASS, f"mode {mode:04o}, owner {owner}")

    def fix() -> str:
        platform_probe.set_file_mode(key_path, KEY_MODE)
        return f"chmod {KEY_MODE:04o} {key_path}"

    return Check(
        name="service account key permissions",
        probe=probe,
        remedy=f"chmod 600 {key_path}",
        fix=fix,
    )


def sheet_id_check(config: ProjectConfig, live: bool, registry_path: str) -> Check:
    mode = "live" if live else "test"

    def probe() -> CheckOutcome:
        sheet_id = config.sheet_id_for(live)
        if sheet_id.startswith(PLACEHOLDER_PREFIX):
            return CheckOutcome(Status.FAIL, f"{mode}-mode sheet_id is still '{sheet_id}'")
        return CheckOutcome(Status.PASS, f"{mode}-mode sheet_id set")

    return Check(
        name=f"{mode} spreadsheet ID",
        probe=probe,
        remedy=f"set the {mode}-mode sheet ID for '{config.project_id}' in {registry_path}",
    )


def drive_check(files_dir: Path) -> Check:
    def probe() -> CheckOutcome:
        if not files_dir.exists():
            # Not mounted is "could not tell what is on it", not "broken".
            return CheckOutcome(Status.UNKNOWN, f"{files_dir} is not there - drive unplugged?")
        if not platform_probe.is_readable_directory(files_dir):
            return CheckOutcome(Status.FAIL, f"{files_dir} is not a readable directory")
        return CheckOutcome(Status.PASS, str(files_dir))

    return Check(
        name="photo drive",
        probe=probe,
        remedy=f"plug in the LaCie drive, or correct files_dir so it points at {files_dir}",
    )
