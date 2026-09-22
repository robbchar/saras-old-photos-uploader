"""Rendering and placement for the per-user LaunchAgent that runs the hourly sync.

Parameterized rather than hardcoded to one agent: a second project's own agent
could reuse this same rendering under the same install."""
from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

HOURLY = 3600
LABEL_PREFIX = "org.lcpsociety.iabulk.sync"


@dataclass(frozen=True)
class AgentSpec:
    project_id: str
    label: str
    program_arguments: list[str]
    interval: int
    stdout_path: Path
    stderr_path: Path
    working_directory: Path


def sync_agent_spec(repo_root: Path, project_id: str, registry_path: Path | str) -> AgentSpec:
    repo_root = Path(repo_root).resolve()
    # Absolute, so the agent reads the file setup checked, whatever its working directory.
    registry_path = Path(registry_path).resolve()
    return AgentSpec(
        project_id=project_id,
        label=f"{LABEL_PREFIX}.{project_id}",
        program_arguments=[
            str(repo_root / ".venv" / "bin" / "python"),
            str(repo_root / "ia_bulk.py"),
            "sync-metadata",
            "--project",
            project_id,
            "--live",
            "--registry",
            str(registry_path),
        ],
        interval=HOURLY,
        stdout_path=repo_root / "logs" / f"launchagent-{project_id}.out",
        stderr_path=repo_root / "logs" / f"launchagent-{project_id}.err",
        working_directory=repo_root,
    )


def render_plist(spec: AgentSpec) -> str:
    # RunAtLoad on: enabling the agent, and each login of the operating account,
    # syncs now rather than after an idle hour. launchd still coalesces intervals
    # missed while asleep into a single run on wake.
    body = {
        "Label": spec.label,
        "ProgramArguments": list(spec.program_arguments),
        "StartInterval": spec.interval,
        "RunAtLoad": True,
        "WorkingDirectory": str(spec.working_directory),
        "StandardOutPath": str(spec.stdout_path),
        "StandardErrorPath": str(spec.stderr_path),
    }
    return plistlib.dumps(body).decode("utf-8")


def plist_path(spec: AgentSpec, home: Path) -> Path:
    return Path(home) / "Library" / "LaunchAgents" / f"{spec.label}.plist"


def write_plist(spec: AgentSpec, home: Path) -> str:
    target = plist_path(spec, home)
    target.parent.mkdir(parents=True, exist_ok=True)
    # launchd does not create intermediate directories for stdio redirection, and
    # logs/ is gitignored - absent on a fresh clone, so the job would not spawn.
    for stdio_path in (spec.stdout_path, spec.stderr_path):
        stdio_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n": text mode would emit CRLF on Windows, so plist_is_current
    # would never match what render_plist produces.
    target.write_text(render_plist(spec), encoding="utf-8", newline="\n")
    return f"wrote {target}"


def plist_is_current(spec: AgentSpec, home: Path) -> bool:
    target = plist_path(spec, home)
    try:
        return target.read_text(encoding="utf-8") == render_plist(spec)
    except OSError:
        return False
