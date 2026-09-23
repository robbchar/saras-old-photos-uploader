"""The deployment check list, shared by `ia_bulk.py doctor` and `ia_bulk.py setup`.

A check that can be converged on-machine carries a fix(); one that cannot carries
only a remedy and points at docs/DEPLOYMENT.md.

Neither `setup` nor `doctor` reads a secret out of a credential: they check
placement and permissions, and read the service-account key's own
`client_email` so a remedy can name the address to share the Sheet with. No
private key, no access key, no token is ever read or printed.

This module must never import ia_bulk."""
from __future__ import annotations

import configparser
import enum
import importlib
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import google_auth
import launch_agent
import platform_probe
import sync_state
from googleapiclient.errors import HttpError
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
    # Replaces Check.remedy when this particular failure needs a different fix.
    remedy: str | None = None


@dataclass(frozen=True)
class Check:
    name: str
    probe: Callable[[], CheckOutcome]
    remedy: str
    fix: Callable[[], str] | None = None
    # False for checks the hourly sync does not depend on; their FAIL does not block --enable-agent.
    needed_by_agent: bool = True


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
            try:
                announce(f"  {check.fix()}")
            except Exception as exc:  # noqa: BLE001 - one unfixable check must not abandon the rest
                announce(f"  could not fix: {exc}")
                results.append((check, CheckOutcome(Status.FAIL, f"could not fix ({exc})")))
                continue
            outcome = _probe(check)
        results.append((check, outcome))
    return results


def format_report(results: list[tuple[Check, CheckOutcome]]) -> str:
    lines: list[str] = []
    for check, outcome in results:
        lines.append(f"[{outcome.status.value}] {check.name}: {outcome.detail}")
        if outcome.status is Status.FAIL:
            lines.append(f"    fix: {outcome.remedy or check.remedy}")
    return "\n".join(lines)


def exit_code(results: list[tuple[Check, CheckOutcome]]) -> int:
    return 1 if any(outcome.status is Status.FAIL for _, outcome in results) else 0


def agent_blocking_failures(results: list[tuple[Check, CheckOutcome]]) -> list[str]:
    """FAILing checks the hourly sync depends on. The agent's own checks are
    excluded because enabling is what fixes them."""
    return [
        check.name
        for check, outcome in results
        if check.needed_by_agent and outcome.status is Status.FAIL
    ]


KEY_MODE = 0o600
PLACEHOLDER_PREFIX = "REPLACE_WITH"


def _is_private(mode: int) -> bool:
    """Owner can read, group and other get nothing: 0600, or the stricter 0400."""
    return mode & 0o077 == 0 and mode & 0o400 != 0

@dataclass(frozen=True)
class InstallCommand:
    """The ./install.sh line a remedy names, runnable in zsh exactly as printed."""

    project_id: str
    # None for the checkout's own projects_registry.json, which install.sh reads by default.
    registry: Path | None = None

    def render(self, *, enable_agent: bool = False) -> str:
        arguments = ["./install.sh", "--project", self.project_id]
        if self.registry is not None:
            arguments += ["--registry", str(self.registry)]
        if enable_agent:
            arguments += ["--live", "--enable-agent"]
        return shlex.join(arguments)


_REQUIRED_MODULES = (
    "internetarchive",
    "googleapiclient.discovery",
    "google.oauth2.service_account",
)


def dependencies_check(install: InstallCommand) -> Check:
    """A statement of what this pipeline needs importable, not a live gate on the
    CLI: ia_bulk.py imports all three at module scope, so a CLI run that reaches
    this probe has already proved them present. It is meaningful to a caller that
    imports deployment on its own, and it keeps the requirement in the report."""

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
        remedy=f"{install.render()} (or: .venv/bin/pip install -r requirements.txt)",
    )


def python_version_check(version: tuple[int, int], install: InstallCommand) -> Check:
    def probe() -> CheckOutcome:
        running = ".".join(str(part) for part in version)
        if version < MINIMUM_PYTHON:
            floor = ".".join(str(part) for part in MINIMUM_PYTHON)
            return CheckOutcome(Status.FAIL, f"running {running}, need {floor}+")
        return CheckOutcome(Status.PASS, running)

    return Check(
        name="python version",
        probe=probe,
        remedy=(
            f"install Python 3.10+ and re-run {install.render()} - see docs/DEPLOYMENT.md"
        ),
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
        if not platform_probe.has_posix_permissions():
            return CheckOutcome(
                Status.UNKNOWN, "POSIX permissions cannot be checked on this platform"
            )
        owner = platform_probe.file_owner(key_path) or "unknown"
        # Owner is reported, never asserted: install and operation share one
        # account, so an owner that is not the caller is worth seeing, not guessing at.
        if not _is_private(mode):
            return CheckOutcome(
                Status.FAIL, f"mode {mode:04o}, owner {owner}, want {KEY_MODE:04o} or 0400"
            )
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


# The two keys `ia` needs to write to Internet Archive. Their presence is checked;
# their values are never read, printed or logged.
_IA_S3_KEYS = ("access", "secret")

IA_CONFIGURE_REMEDY = (
    "run `ia configure` as the account that runs the pipeline - see docs/DEPLOYMENT.md, "
    'section "`ia configure`"'
)


def _resolve_ia_config(config_file: str | None) -> tuple[Path, configparser.RawConfigParser]:
    """Where `internetarchive` itself would look, and what it parsed there.

    Imported inside the call, not at module scope: dependencies_check exists to
    report a missing `internetarchive`, so this module has to import without it."""
    from internetarchive.config import parse_config_file

    resolved, _is_xdg, parser = parse_config_file(config_file)
    return Path(resolved), parser


def ia_credentials_check(config_file: str | None = None) -> Check:
    """The credential that grants write on the shared org account - the one thing
    the hourly agent needs to do its actual job, and the only credential nothing
    else here covers. Local and offline: no call to Internet Archive is made."""

    def probe() -> CheckOutcome:
        try:
            path, parser = _resolve_ia_config(config_file)
        except configparser.Error as exc:
            return CheckOutcome(Status.FAIL, f"the ia config file is not parseable ({exc})")
        if not path.is_file():
            return CheckOutcome(Status.FAIL, f"no ia credentials at {path}")
        missing = [key for key in _IA_S3_KEYS if not parser.get("s3", key, fallback=None)]
        if missing:
            return CheckOutcome(Status.FAIL, f"{path} has no s3 {', '.join(missing)}")
        return CheckOutcome(Status.PASS, str(path))

    # No fix(): a human types those credentials into an interactive prompt.
    return Check(name="ia credentials", probe=probe, remedy=IA_CONFIGURE_REMEDY)


def ia_credentials_mode_check(config_file: str | None = None) -> Check:
    def probe() -> CheckOutcome:
        path, _parser = _resolve_ia_config(config_file)
        mode = platform_probe.file_mode(path)
        if mode is None:
            return CheckOutcome(Status.UNKNOWN, f"no ia config at {path} to check")
        if not platform_probe.has_posix_permissions():
            return CheckOutcome(
                Status.UNKNOWN, "POSIX permissions cannot be checked on this platform"
            )
        if not _is_private(mode):
            return CheckOutcome(Status.FAIL, f"{path}: mode {mode:04o}, want {KEY_MODE:04o} or 0400")
        return CheckOutcome(Status.PASS, f"mode {mode:04o}")

    # No fix(), unlike the service-account key: this file lives outside the
    # checkout, so setup reports on it rather than chmodding someone's home.
    return Check(
        name="ia credentials permissions",
        probe=probe,
        remedy="chmod 600 the ia config file named above - see docs/DEPLOYMENT.md",
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

    # sync-metadata never reads the drive, so this does not gate the agent.
    return Check(
        name="files drive",
        probe=probe,
        # FAIL only when the path exists, so the drive is attached; access or the path is wrong.
        remedy=(
            "give this account read access to it, or correct files_dir in the project's "
            f"registry entry (currently {files_dir})"
        ),
        needed_by_agent=False,
    )


# 403 (not shared) and 404 (wrong ID) are real, actionable misconfiguration, as is
# 400 (a range naming no tab, or an ID naming no native Sheet); every other
# HttpError status, and any transport error, means only "could not tell".
_SHEET_MISCONFIGURED_STATUSES = (403, 404)
_SHEET_BAD_REQUEST_STATUS = 400

SheetProbe = Callable[[], list[list[str]]]
# sync-metadata's own pre-send gate over the grid: None when it would proceed, else why not.
SheetRefusal = Callable[[list[list[str]]], "str | None"]

AUTH_REMEDY = (
    'fix the service account key as the line above says - see docs/DEPLOYMENT.md, section '
    '"Service account"'
)
BAD_REQUEST_REMEDY = (
    "check that sheet_id names a native Google Sheet (not an uploaded Excel file) and that "
    "sheet_tab names its tab exactly as the Sheet shows it (case-sensitive), in the "
    "project's registry entry"
)
SHEET_REFUSED_REMEDY = (
    "see the `spreadsheet reachable` line: share the Sheet as Editor with the address it "
    "names, or correct the sheet_id"
)


class SheetNotChecked(Exception):
    """Raised by a SheetProbe that knows a read would prove nothing, e.g. a placeholder sheet ID."""


def _read_grid_or_outcome(
    read_grid: SheetProbe, refused_remedy: str | None = None
) -> tuple[list[list[str]] | None, CheckOutcome | None]:
    """Shared by both Sheet checks so one read failure is classified one way."""
    try:
        return read_grid(), None
    except SheetNotChecked as exc:
        return None, CheckOutcome(Status.UNKNOWN, f"not checked - {exc}")
    except google_auth.AuthUnavailable as exc:
        if exc.transient:
            return None, CheckOutcome(Status.UNKNOWN, f"could not authenticate ({exc})")
        return None, CheckOutcome(Status.FAIL, f"could not authenticate ({exc})", AUTH_REMEDY)
    except HttpError as exc:
        if exc.resp.status in _SHEET_MISCONFIGURED_STATUSES:
            return None, CheckOutcome(
                Status.FAIL, f"the Sheet refused this service account ({exc})", refused_remedy
            )
        if exc.resp.status == _SHEET_BAD_REQUEST_STATUS:
            return None, CheckOutcome(
                Status.FAIL, f"Google Sheets rejected the request ({exc})", BAD_REQUEST_REMEDY
            )
        return None, CheckOutcome(Status.UNKNOWN, f"Google Sheets returned an error ({exc})")
    except OSError as exc:
        return None, CheckOutcome(Status.UNKNOWN, f"could not reach the Sheet ({exc})")


# Named so a caller can single these two out without re-spelling them. They are
# the only checks that need the network, hence the only ones a de-facto offline
# machine turns into UNKNOWN.
SHEET_REACHABLE_CHECK = "spreadsheet reachable"
SYNC_COLUMNS_CHECK = "sync state columns"
LIVE_SHEET_CHECKS = (SHEET_REACHABLE_CHECK, SYNC_COLUMNS_CHECK)


def unverified_sheet_checks(results: list[tuple[Check, CheckOutcome]]) -> list[str]:
    """Sheet checks that did not come back PASS, UNKNOWN included.

    For a caller about to start unattended live traffic, "could not tell" is not
    good enough. Deliberately separate from exit_code(), which is unchanged:
    `doctor` still exits 0 on UNKNOWN, and every other caller keeps that rule."""
    return [
        check.name
        for check, outcome in results
        if check.name in LIVE_SHEET_CHECKS and outcome.status is not Status.PASS
    ]


def sheet_reachable_check(read_grid: SheetProbe, sharing_target: str) -> Check:
    def probe() -> CheckOutcome:
        grid, failure = _read_grid_or_outcome(read_grid)
        if failure is not None:
            return failure
        assert grid is not None
        # A Viewer share reads fine; Sheets offers no read-only way to confirm Editor.
        return CheckOutcome(Status.PASS, f"read {len(grid)} rows (edit access is not checked)")

    return Check(
        name=SHEET_REACHABLE_CHECK,
        probe=probe,
        remedy=f"share the Sheet as Editor with {sharing_target}",
    )


def sync_columns_check(read_grid: SheetProbe, sync_refusal: SheetRefusal) -> Check:
    """sync_refusal is passed in, not imported, because this module never
    imports ia_bulk; it keeps the gate and the agent refusing the same Sheets."""

    def probe() -> CheckOutcome:
        grid, failure = _read_grid_or_outcome(read_grid, SHEET_REFUSED_REMEDY)
        if failure is not None:
            return failure
        assert grid is not None
        try:
            refusal = sync_refusal(grid)
        except Exception as exc:  # noqa: BLE001 - see the FAIL below; not _probe's UNKNOWN
            # A header row that will not map is confirmed-broken. Letting _probe's
            # blanket handler call it UNKNOWN would hide a duplicated header.
            return CheckOutcome(Status.FAIL, f"could not check the Sheet for sync-metadata ({exc})")
        if refusal is not None:
            return CheckOutcome(Status.FAIL, refusal)
        return CheckOutcome(Status.PASS, "passes every check sync-metadata makes before sending")

    return Check(
        name=SYNC_COLUMNS_CHECK,
        probe=probe,
        remedy=(
            "fix the Sheet as the line above says. The sync columns are "
            f"{' and '.join(sync_state.SYNC_STATE_COLUMNS)}, one header each, left visible "
            "with a red background - see docs/DEPLOYMENT.md"
        ),
    )


def agent_plist_check(spec: launch_agent.AgentSpec, home: Path, install: InstallCommand) -> Check:
    """No fix(): launchd loads every plist in LaunchAgents at login, and a rewrite
    alone never reaches the loaded job, so only --enable-agent writes it and reloads."""

    def probe() -> CheckOutcome:
        target = launch_agent.plist_path(spec, home)
        if launch_agent.plist_is_current(spec, home):
            return CheckOutcome(Status.PASS, str(target))
        if target.exists():
            return CheckOutcome(Status.FAIL, f"{target} does not match this checkout and registry")
        return CheckOutcome(
            Status.UNKNOWN, f"no plist at {target} - the hourly agent is not enabled for this account"
        )

    return Check(
        name="launch agent plist",
        probe=probe,
        remedy=(
            f"{install.render(enable_agent=True)}, from the account that "
            "runs the agent, rewrites it and reloads the agent; if that was just run and this "
            "still fails, the plist could not be written "
            '- see docs/DEPLOYMENT.md, section "Checking a machine later"'
        ),
        needed_by_agent=False,
    )


def agent_loaded_check(spec: launch_agent.AgentSpec, install: InstallCommand) -> Check:
    # Relative, as the operator reads them from the checkout they run install.sh in.
    stdout_log = spec.stdout_path.relative_to(spec.working_directory).as_posix()
    stderr_log = spec.stderr_path.relative_to(spec.working_directory).as_posix()

    def probe() -> CheckOutcome:
        output = platform_probe.launchctl_print(spec.label)
        if output is None:
            # Not loaded, no launchctl, or a different account's session - all
            # "could not tell", and loading is --enable-agent's job, never a fix().
            return CheckOutcome(Status.UNKNOWN, f"{spec.label} is not loaded for this account")
        last_exit = platform_probe.parse_last_exit(output)
        pid = platform_probe.parse_pid(output)
        running = f", running as pid {pid}" if pid is not None else ""
        if last_exit is None:
            return CheckOutcome(Status.PASS, f"loaded, has not run yet{running}")
        if last_exit != 0:
            return CheckOutcome(Status.FAIL, f"loaded, last run exited {last_exit}{running}")
        return CheckOutcome(Status.PASS, f"loaded, last run exited 0{running}")

    return Check(
        name="launch agent loaded",
        probe=probe,
        remedy=(
            f"read {stdout_log} and {stderr_log} for why the last run failed; to "
            "reload the agent, "
            "log in as the operating account and run "
            f"{install.render(enable_agent=True)}"
        ),
        needed_by_agent=False,
    )
