"""The deployment check list, shared by `ia_bulk.py doctor` and `ia_bulk.py setup`.

A check that can be converged on-machine carries a fix(); one that cannot carries
only a remedy and points at docs/DEPLOYMENT.md."""
from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass

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
