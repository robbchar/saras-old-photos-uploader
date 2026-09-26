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

import upload_lock
from upload_lock import LockHolder

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


# How many `-NNN` suffixes new_run_dir will try before giving up on a
# colliding timestamp. Three digits comfortably covers any realistic burst
# of same-second starts (the upload lock limits this to one run at a time
# in practice); if every one of these is somehow taken, something is
# seriously wrong and raising is more honest than silently reusing a dir.
_MAX_RUN_DIR_SUFFIX = 999


def new_run_dir(logs_base: Path, now: str) -> Path:
    """Create and return a unique `<logs_base>/page-runs/<now>[-NNN]/`.

    `now` is second-resolution (see upload_server._default_now_utc), so two
    runs started within the same second would otherwise collide on the same
    folder -- silently overwriting one run's page-run.json and truncating
    its output.txt. When `<now>` is already taken, a zero-padded `-002`,
    `-003`, ... suffix is appended until an unused name is found.

    The suffix is fixed-width and always longer than the bare timestamp, so
    newest_run_dir's lexicographic-max sort still picks the right folder: a
    suffixed name shares the bare timestamp as a prefix (so it always sorts
    after it), and two suffixed names of the same width compare the same way
    their suffix numbers do.

    Uses `mkdir(exist_ok=False)` in a loop (not a check-then-create) so a
    single process's own collision handling is itself race-free; a second
    server process spawning in the same instant is a separate, accepted
    residual window -- the upload lock backs that one.
    """
    base = logs_base / PAGE_RUNS_SUBDIR
    base.mkdir(parents=True, exist_ok=True)

    candidate = base / now
    suffix = 2
    while True:
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            if suffix > _MAX_RUN_DIR_SUFFIX:
                raise
            candidate = base / f"{now}-{suffix:03d}"
            suffix += 1


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
    """The run's `upload-*.jsonl` log, if the run has gotten far enough to write one.

    A run dir normally holds exactly one. When it somehow holds more than
    one (a reused/edge-case dir), the newest by name wins -- the filenames
    embed a sortable UTC stamp (see ia_bulk.open_log), so the lexicographic
    max is also the chronological max, consistent with newest_run_dir's own
    "pick by name" rule.
    """
    matches = sorted(run_dir.glob("upload-*.jsonl"))
    return matches[-1] if matches else None


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


# The record kind for the "an item's upload has started" marker (see
# ia_bulk.log_item_start). It carries a "record" key so read_progress's
# per-item (record-less) count never mistakes it for a completed item.
ITEM_START_RECORD = "item_start"


@dataclass(frozen=True)
class CurrentItem:
    """The item an in-progress run is uploading right now.

    Written as an item_start record just before the blocking upload call, so
    the page can name the photo in flight even though the underlying library
    reports no per-byte progress. `index` is the run's 1-based position; `file`
    is the row's file value, the human-readable handle on the photo."""

    index: int
    file: str

    def to_json(self) -> dict[str, object]:
        return {"index": self.index, "file": self.file}


def read_current_item(jsonl: Path | None) -> CurrentItem | None:
    """The item the run is uploading right now, or None when nothing is in flight.

    Uploads run one at a time, so the in-flight item is the last item_start
    record whose identifier has not yet appeared in a completion record. Once
    that item's success/failure record lands the marker resolves to None, until
    the next item starts."""
    if jsonl is None:
        return None
    records = _read_jsonl_records(jsonl)
    starts = [record for record in records if record.get("record") == ITEM_START_RECORD]
    if not starts:
        return None
    last = starts[-1]
    completed_ids = {record.get("identifier") for record in records if "record" not in record}
    if last.get("identifier") in completed_ids:
        return None
    return CurrentItem(index=int(last["index"]), file=str(last["file"]))  # type: ignore[arg-type]


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


@dataclass(frozen=True)
class Idle:
    """No upload lock held, and no run folder has ever been written."""

    kind: Literal["idle"] = "idle"

    def to_json(self) -> dict[str, object]:
        return {"kind": self.kind}


@dataclass(frozen=True)
class PageRunActive:
    """The lock's holder is this page's own newest run - safe to show live progress for."""

    batch: str
    live: bool
    started_at: str
    done: int
    planned: int | None
    current: CurrentItem | None = None
    kind: Literal["page_run_active"] = "page_run_active"

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "batch": self.batch,
            "live": self.live,
            "started_at": self.started_at,
            "done": self.done,
            "planned": self.planned,
            "current": None if self.current is None else self.current.to_json(),
        }


@dataclass(frozen=True)
class TerminalRunActive:
    """The lock is held by something other than this page's newest run (a CLI run, a
    stale/unmatched page run, or - when holder is None - a transient probe window)."""

    holder: LockHolder | None
    kind: Literal["terminal_run_active"] = "terminal_run_active"

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "holder": None if self.holder is None else {
                "started_at": self.holder.started_at,
                "project": self.holder.project,
                "batch": self.holder.batch,
                "live": self.holder.live,
            },
        }


@dataclass(frozen=True)
class Finished:
    """The lock is free, and the newest run folder holds a finished (or refused) attempt."""

    ending: Ending
    page_run: PageRun | None
    kind: Literal["finished"] = "finished"

    def to_json(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "ending": self.ending.to_json(),
            "page_run": None if self.page_run is None else self.page_run.to_json(),
        }


RunState = Idle | PageRunActive | TerminalRunActive | Finished


def compute_run_state(lock_path: Path, logs_base: Path) -> RunState:
    """What the upload page should show right now, from the lock and the newest run folder.

    The lock's holder pid is the only way to tell "this page's own run" apart
    from a terminal run started elsewhere (another page instance, or the `ia`
    CLI directly) - both hold the same lock, so only the pid distinguishes
    them. Calls upload_lock.running_upload through the module (not a bound
    import) so tests can monkeypatch it."""
    running = upload_lock.running_upload(lock_path)
    if running is not None:
        holder = running.holder
        newest = newest_run_dir(logs_base)
        if newest is not None:
            page_run = read_page_run(newest)
            if holder is not None and page_run is not None and holder.pid == page_run.pid:
                jsonl = find_jsonl(newest)
                done, planned = read_progress(jsonl)
                return PageRunActive(
                    batch=page_run.batch,
                    live=page_run.live,
                    started_at=page_run.started_at,
                    done=done,
                    planned=planned,
                    current=read_current_item(jsonl),
                )
        return TerminalRunActive(holder=holder)

    newest = newest_run_dir(logs_base)
    if newest is None:
        return Idle()
    return Finished(ending=read_ending(newest), page_run=read_page_run(newest))
