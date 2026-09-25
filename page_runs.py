"""Per-run folders under `<logs_base>/page-runs/<UTC>/`: `page-run.json` and the run's JSONL log.

The upload page server drives one run at a time (see upload_lock.py) and
gives each attempt its own timestamped folder. This module is pure disk
logic - no HTTP, no subprocess spawning, no signals - so the server and
its tests can read what a run is doing (read_progress) and how it ended
(read_ending) without caring how the run itself was launched."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PAGE_RUNS_SUBDIR = "page-runs"
PAGE_RUN_FILENAME = "page-run.json"
OUTPUT_FILENAME = "output.txt"

# A refusal (e.g. "sheet_id is a placeholder") is reported as the tail of the
# child's captured console output, not the whole thing.
REFUSAL_REASON_LINE_LIMIT = 20


@dataclass(frozen=True)
class PageRun:
    """One page-driven upload attempt: who started it, and where its folder is.

    Written to page-run.json in its own run dir so a later process (the
    server after a restart, or a second request) can recognize "this is our
    run" by matching pid against the upload lock's holder."""

    pid: int
    project: str
    batch: str
    live: bool
    started_at: str
    dir: Path

    def to_json(self) -> dict[str, object]:
        # dir is the folder this lives in, not part of the stored record.
        return {
            "pid": self.pid,
            "project": self.project,
            "batch": self.batch,
            "live": self.live,
            "started_at": self.started_at,
        }

    @staticmethod
    def from_json(data: dict[str, object], run_dir: Path) -> "PageRun":
        return PageRun(
            pid=int(data["pid"]),  # type: ignore[arg-type]
            project=str(data["project"]),
            batch=str(data["batch"]),
            live=bool(data["live"]),
            started_at=str(data["started_at"]),
            dir=run_dir,
        )


def new_run_dir(logs_base: Path, now: str) -> Path:
    """Create and return `<logs_base>/page-runs/<now>/`."""
    run_dir = logs_base / PAGE_RUNS_SUBDIR / now
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def newest_run_dir(logs_base: Path) -> Path | None:
    """The child of `<logs_base>/page-runs/` with the lexicographically greatest
    name, since UTC stamps sort chronologically - or None if there are none yet."""
    base = logs_base / PAGE_RUNS_SUBDIR
    if not base.is_dir():
        return None
    children = [child for child in base.iterdir() if child.is_dir()]
    if not children:
        return None
    return max(children, key=lambda child: child.name)


def write_page_run(run: PageRun) -> None:
    path = run.dir / PAGE_RUN_FILENAME
    path.write_text(json.dumps(run.to_json()), encoding="utf-8")


def read_page_run(run_dir: Path) -> PageRun | None:
    path = run_dir / PAGE_RUN_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PageRun.from_json(data, run_dir)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def find_jsonl(run_dir: Path) -> Path | None:
    """The run's `upload-*.jsonl` log, if the run has gotten far enough to write one."""
    matches = sorted(run_dir.glob("upload-*.jsonl"))
    return matches[0] if matches else None


def _read_jsonl_records(jsonl: Path) -> list[dict[str, object]]:
    """Parse each line as one JSON record, skipping any line that won't parse.

    The writer (a separate, still-running upload process) can leave the final
    line truncated - killed mid-flush, or caught mid-write by Windows AV/file
    locking - so a live progress read must tolerate a partial or garbage line
    rather than raising. A read failure of the file itself (OSError) is not
    caught here; that's a real problem for the caller to handle."""
    text = jsonl.read_text(encoding="utf-8")
    records: list[dict[str, object]] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _find_run_header_planned(records: list[dict[str, object]]) -> int | None:
    for record in records:
        if record.get("record") == "run_header":
            planned = record.get("planned")
            return None if planned is None else int(planned)  # type: ignore[arg-type]
    return None


def _find_run_summary(records: list[dict[str, object]]) -> dict[str, object] | None:
    for record in records:
        if record.get("record") == "run_summary":
            return record
    return None


def read_progress(jsonl: Path | None) -> tuple[int, int | None]:
    """(done, planned) for the run's JSONL so far.

    done counts per-item records (they carry no "record" key); planned comes
    from the run_header, once one has been written. No JSONL yet -> (0, None)."""
    if jsonl is None:
        return 0, None
    records = _read_jsonl_records(jsonl)
    done = sum(1 for record in records if "record" not in record)
    planned = _find_run_header_planned(records)
    return done, planned


@dataclass(frozen=True)
class Refused:
    """No JSONL at all: the run never got as far as writing run_header."""

    reason_lines: list[str]
    kind: Literal["refused"] = "refused"

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind, "reason_lines": self.reason_lines}


@dataclass(frozen=True)
class Stopped:
    """Ended early because a stop was requested (see stop_request.py)."""

    summary: dict[str, object]
    planned: int | None
    kind: Literal["stopped"] = "stopped"

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind, "summary": self.summary, "planned": self.planned}


@dataclass(frozen=True)
class RateLimited:
    """Ended because Internet Archive throttled the run."""

    summary: dict[str, object]
    kind: Literal["rate_limited"] = "rate_limited"

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind, "summary": self.summary}


@dataclass(frozen=True)
class Completed:
    """Ran to the end on its own."""

    summary: dict[str, object]
    kind: Literal["completed"] = "completed"

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind, "summary": self.summary}


@dataclass(frozen=True)
class EndedWithoutSummary:
    """JSONL exists but has no run_summary line - the process died mid-run."""

    kind: Literal["ended_without_summary"] = "ended_without_summary"

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind}


Ending = Refused | Stopped | RateLimited | Completed | EndedWithoutSummary


def _read_refusal_reason_lines(run_dir: Path) -> list[str]:
    output_path = run_dir / OUTPUT_FILENAME
    if not output_path.is_file():
        return []
    text = output_path.read_text(encoding="utf-8", errors="replace")
    # Split on "\n" (not splitlines()) so a trailing "\r" from Windows-style
    # "\r\n" endings survives to be stripped explicitly, on any OS.
    lines = [line.rstrip("\r") for line in text.split("\n")]
    non_blank = [line for line in lines if line.strip()]
    return non_blank[-REFUSAL_REASON_LINE_LIMIT:]


def read_ending(run_dir: Path) -> Ending:
    """How a run ended, from its JSONL log (or, if it never wrote one, its console output)."""
    jsonl = find_jsonl(run_dir)
    if jsonl is None:
        return Refused(reason_lines=_read_refusal_reason_lines(run_dir))

    records = _read_jsonl_records(jsonl)
    summary = _find_run_summary(records)
    if summary is None:
        return EndedWithoutSummary()
    if summary.get("stopped_by_request"):
        return Stopped(summary=summary, planned=_find_run_header_planned(records))
    if summary.get("rate_limited"):
        return RateLimited(summary=summary)
    return Completed(summary=summary)
