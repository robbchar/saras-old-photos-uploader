import json
from pathlib import Path

import page_runs
import upload_lock
from upload_lock import LockHolder, RunningUpload


def _write_jsonl(run_dir, records):
    p = run_dir / "upload-20260925T000000Z.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return p


RUN_SUMMARY_BASE = {
    "record": "run_summary",
    "attempted": 1,
    "succeeded": 1,
    "failures": [],
    "unconfirmed": [],
    "not_attempted": 4,
    "rate_limited": False,
    "rate_limit_status": None,
    "stopped_by_request": False,
    "skipped": [],
}


# --- read_progress ---------------------------------------------------------


def test_read_progress_counts_per_item_records(tmp_path):
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 3, "batch": "Logging"},
        {"identifier": "a", "status": "success"},
        {"identifier": "b", "status": "failure"},
    ])
    assert page_runs.read_progress(page_runs.find_jsonl(tmp_path)) == (2, 3)


def test_read_progress_no_run_header_yet_has_no_planned(tmp_path):
    _write_jsonl(tmp_path, [
        {"identifier": "a", "status": "success"},
    ])
    assert page_runs.read_progress(page_runs.find_jsonl(tmp_path)) == (1, None)


def test_read_progress_no_jsonl_is_zero_and_none(tmp_path):
    assert page_runs.read_progress(page_runs.find_jsonl(tmp_path)) == (0, None)


def test_read_progress_tolerates_a_truncated_final_line(tmp_path):
    # A separate, still-running upload process can be killed (or caught by
    # Windows AV/file-locking) mid-flush of its last line - the valid records
    # written before it must still count.
    good_lines = "".join(json.dumps(r) + "\n" for r in [
        {"record": "run_header", "planned": 3},
        {"identifier": "a", "status": "success"},
    ])
    truncated_tail = '{"identifier": "b", "status": "s'  # no closing brace, no newline
    jsonl = tmp_path / "upload-20260925T000000Z.jsonl"
    jsonl.write_text(good_lines + truncated_tail, encoding="utf-8")

    assert page_runs.read_progress(jsonl) == (1, 3)


# --- read_current_item -----------------------------------------------------


def test_read_current_item_names_the_in_flight_item(tmp_path):
    # An item_start with no matching completion yet is the item uploading now.
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 3},
        {"identifier": "a", "status": "success"},
        {"record": "item_start", "identifier": "b", "file": "photos/b.jpg", "index": 2},
    ])
    current = page_runs.read_current_item(page_runs.find_jsonl(tmp_path))
    assert current == page_runs.CurrentItem(index=2, file="photos/b.jpg")


def test_read_current_item_is_none_once_the_started_item_completes(tmp_path):
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 3},
        {"record": "item_start", "identifier": "a", "file": "a.jpg", "index": 1},
        {"identifier": "a", "status": "success"},
    ])
    assert page_runs.read_current_item(page_runs.find_jsonl(tmp_path)) is None


def test_read_current_item_is_none_without_any_item_start(tmp_path):
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 3},
        {"identifier": "a", "status": "success"},
    ])
    assert page_runs.read_current_item(page_runs.find_jsonl(tmp_path)) is None


def test_read_current_item_is_none_when_no_jsonl(tmp_path):
    assert page_runs.read_current_item(page_runs.find_jsonl(tmp_path)) is None


def test_item_start_marker_is_not_counted_as_done(tmp_path):
    # The in-flight marker carries a "record" key so read_progress's per-item
    # (record-less) count never mistakes it for a completed item.
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 3},
        {"identifier": "a", "status": "success"},
        {"record": "item_start", "identifier": "b", "file": "b.jpg", "index": 2},
    ])
    assert page_runs.read_progress(page_runs.find_jsonl(tmp_path)) == (1, 3)


def test_current_item_to_json():
    assert page_runs.CurrentItem(index=2, file="b.jpg").to_json() == {
        "index": 2,
        "file": "b.jpg",
    }


# --- find_jsonl --------------------------------------------------------------


def test_find_jsonl_none_when_absent(tmp_path):
    assert page_runs.find_jsonl(tmp_path) is None


def test_find_jsonl_finds_the_upload_file(tmp_path):
    p = _write_jsonl(tmp_path, [{"record": "run_header", "planned": 1}])
    assert page_runs.find_jsonl(tmp_path) == p


def test_find_jsonl_picks_the_newest_on_multiple_files(tmp_path):
    # A run dir normally holds exactly one upload-*.jsonl; on a reused or
    # edge-case dir holding more than one, the newest by name must win, not
    # the oldest (find_jsonl used to return sorted(...)[0]).
    older = _write_jsonl(tmp_path, [{"record": "run_header", "planned": 1}])
    newer = tmp_path / "upload-20260926T000000Z.jsonl"
    newer.write_text(json.dumps({"record": "run_header", "planned": 2}) + "\n", encoding="utf-8")
    assert older.name < newer.name, "test setup: newer must sort after older"
    assert page_runs.find_jsonl(tmp_path) == newer


# --- read_ending: refused (no jsonl) -----------------------------------------


def test_read_ending_refused_when_no_jsonl(tmp_path):
    (tmp_path / "output.txt").write_text("boom: sheet_id is a placeholder\n", encoding="utf-8")
    ending = page_runs.read_ending(tmp_path)
    assert ending.kind == "refused"
    assert "placeholder" in ending.reason_lines[-1]


def test_read_ending_refused_no_output_txt_is_empty_reason(tmp_path):
    ending = page_runs.read_ending(tmp_path)
    assert ending.kind == "refused"
    assert ending.reason_lines == []


def test_read_ending_refused_keeps_last_20_nonblank_lines(tmp_path):
    lines = [f"line {i}" for i in range(30)]
    text = "".join(f"{line}\n\n" for line in lines)  # blank line after each, to be dropped
    (tmp_path / "output.txt").write_text(text, encoding="utf-8")
    ending = page_runs.read_ending(tmp_path)
    assert ending.kind == "refused"
    assert len(ending.reason_lines) == 20
    assert ending.reason_lines == lines[-20:]


def test_read_ending_refused_strips_windows_line_endings(tmp_path):
    (tmp_path / "output.txt").write_bytes(b"first line\r\nsecond line\r\n")
    ending = page_runs.read_ending(tmp_path)
    assert ending.kind == "refused"
    assert ending.reason_lines == ["first line", "second line"]


# --- read_ending: jsonl present ----------------------------------------------


def test_read_ending_stopped(tmp_path):
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 5},
        {"identifier": "a", "status": "success"},
        {**RUN_SUMMARY_BASE, "stopped_by_request": True},
    ])
    ending = page_runs.read_ending(tmp_path)
    assert ending.kind == "stopped" and ending.summary["succeeded"] == 1
    assert ending.planned == 5


def test_read_ending_rate_limited(tmp_path):
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 5},
        {**RUN_SUMMARY_BASE, "rate_limited": True, "rate_limit_status": 429},
    ])
    ending = page_runs.read_ending(tmp_path)
    assert ending.kind == "rate_limited"
    assert ending.summary["rate_limit_status"] == 429


def test_read_ending_completed(tmp_path):
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 1},
        {**RUN_SUMMARY_BASE, "not_attempted": 0},
    ])
    ending = page_runs.read_ending(tmp_path)
    assert ending.kind == "completed"
    assert ending.summary["not_attempted"] == 0


def test_read_ending_without_summary(tmp_path):
    _write_jsonl(tmp_path, [
        {"record": "run_header", "planned": 5},
        {"identifier": "a", "status": "success"},
    ])
    ending = page_runs.read_ending(tmp_path)
    assert ending.kind == "ended_without_summary"


def test_read_ending_skips_a_garbage_middle_line(tmp_path):
    summary = {**RUN_SUMMARY_BASE, "not_attempted": 0}
    lines = [
        json.dumps({"record": "run_header", "planned": 1}),
        "not json at all {{{",
        json.dumps(summary),
    ]
    jsonl = tmp_path / "upload-20260925T000000Z.jsonl"
    jsonl.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ending = page_runs.read_ending(tmp_path)

    assert ending.kind == "completed"
    assert ending.summary == summary


# --- Ending.to_json ------------------------------------------------------------


def test_ending_to_json_round_trips_kind_and_fields(tmp_path):
    (tmp_path / "output.txt").write_text("boom\n", encoding="utf-8")
    ending = page_runs.read_ending(tmp_path)
    assert ending.to_json() == {"kind": "refused", "reason_lines": ["boom"]}


def test_ending_to_json_stopped(tmp_path):
    summary = {**RUN_SUMMARY_BASE, "stopped_by_request": True}
    _write_jsonl(tmp_path, [{"record": "run_header", "planned": 5}, summary])
    ending = page_runs.read_ending(tmp_path)
    assert ending.to_json() == {"kind": "stopped", "summary": summary, "planned": 5}


def test_ending_to_json_rate_limited(tmp_path):
    summary = {**RUN_SUMMARY_BASE, "rate_limited": True, "rate_limit_status": 429}
    _write_jsonl(tmp_path, [summary])
    ending = page_runs.read_ending(tmp_path)
    assert ending.to_json() == {"kind": "rate_limited", "summary": summary}


def test_ending_to_json_completed(tmp_path):
    _write_jsonl(tmp_path, [RUN_SUMMARY_BASE])
    ending = page_runs.read_ending(tmp_path)
    assert ending.to_json() == {"kind": "completed", "summary": RUN_SUMMARY_BASE}


def test_ending_to_json_ended_without_summary(tmp_path):
    _write_jsonl(tmp_path, [{"record": "run_header", "planned": 5}])
    ending = page_runs.read_ending(tmp_path)
    assert ending.to_json() == {"kind": "ended_without_summary"}


# --- PageRun / page-run.json ---------------------------------------------------


def test_write_then_read_page_run_round_trips(tmp_path):
    run_dir = tmp_path / "page-runs" / "20260925T120000Z"
    run_dir.mkdir(parents=True)
    run = page_runs.PageRun(
        pid=4312,
        project="astoriaphotos",
        batch="Logging",
        live=False,
        started_at="2026-09-25T12:00:00Z",
        dir=run_dir,
    )
    page_runs.write_page_run(run)

    read_back = page_runs.read_page_run(run_dir)

    assert read_back == run


def test_write_page_run_creates_page_run_json(tmp_path):
    run_dir = tmp_path
    run = page_runs.PageRun(
        pid=1, project="p", batch="b", live=True, started_at="t", dir=run_dir,
    )
    page_runs.write_page_run(run)
    assert (run_dir / "page-run.json").exists()


def test_read_page_run_missing_file_is_none(tmp_path):
    assert page_runs.read_page_run(tmp_path) is None


def test_read_page_run_corrupt_json_is_none(tmp_path):
    (tmp_path / "page-run.json").write_text("not json", encoding="utf-8")
    assert page_runs.read_page_run(tmp_path) is None


# --- new_run_dir / newest_run_dir ----------------------------------------------


def test_new_run_dir_creates_the_folder(tmp_path):
    logs_base = tmp_path / "logs"
    run_dir = page_runs.new_run_dir(logs_base, "20260925T120000Z")
    assert run_dir == logs_base / "page-runs" / "20260925T120000Z"
    assert run_dir.is_dir()


def test_new_run_dir_same_timestamp_gets_a_distinct_suffixed_folder(tmp_path):
    # now_utc is second-resolution: two runs started in the same second must
    # not collide on one folder (that would overwrite the first's
    # page-run.json and truncate its output.txt).
    logs_base = tmp_path / "logs"
    first = page_runs.new_run_dir(logs_base, "20260925T120000Z")
    second = page_runs.new_run_dir(logs_base, "20260925T120000Z")
    third = page_runs.new_run_dir(logs_base, "20260925T120000Z")
    assert first.is_dir() and second.is_dir() and third.is_dir()
    assert len({first, second, third}) == 3
    assert first == logs_base / "page-runs" / "20260925T120000Z"
    # The suffixed names must still sort after the bare timestamp, since
    # newest_run_dir picks the lexicographically greatest child name.
    assert sorted([first, second, third], key=lambda p: p.name) == [first, second, third]
    assert page_runs.newest_run_dir(logs_base) == third


def test_newest_run_dir_picks_the_lexicographic_max(tmp_path):
    logs_base = tmp_path / "logs"
    page_runs.new_run_dir(logs_base, "20260925T110000Z")
    latest = page_runs.new_run_dir(logs_base, "20260925T130000Z")
    page_runs.new_run_dir(logs_base, "20260925T120000Z")

    assert page_runs.newest_run_dir(logs_base) == latest


def test_newest_run_dir_none_when_no_runs_yet(tmp_path):
    assert page_runs.newest_run_dir(tmp_path / "logs") is None


def test_newest_run_dir_none_when_page_runs_dir_missing(tmp_path):
    assert page_runs.newest_run_dir(tmp_path) is None


# --- compute_run_state ----------------------------------------------------


def test_idle_when_free_and_no_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_lock, "running_upload", lambda p: None)
    assert page_runs.compute_run_state(tmp_path / ".lock", tmp_path / "logs").kind == "idle"


def test_page_run_active_when_holder_pid_matches(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    run_dir = page_runs.new_run_dir(logs, "20260925T120000Z")
    page_runs.write_page_run(page_runs.PageRun(pid=999, project="p", batch="Logging",
                                               live=False, started_at="t", dir=run_dir))
    _write_jsonl(run_dir, [{"record": "run_header", "planned": 2},
                           {"identifier": "a", "status": "success"}])
    monkeypatch.setattr(upload_lock, "running_upload",
        lambda p: RunningUpload(LockHolder(pid=999, started_at="t", project="p",
                                           batch="Logging", live=False)))
    st = page_runs.compute_run_state(tmp_path / ".lock", logs)
    assert st.kind == "page_run_active" and (st.done, st.planned) == (1, 2)


def test_page_run_active_includes_the_in_flight_item(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    run_dir = page_runs.new_run_dir(logs, "20260925T120000Z")
    page_runs.write_page_run(page_runs.PageRun(pid=999, project="p", batch="Logging",
                                               live=False, started_at="t", dir=run_dir))
    _write_jsonl(run_dir, [{"record": "run_header", "planned": 3},
                           {"identifier": "a", "status": "success"},
                           {"record": "item_start", "identifier": "b", "file": "b.jpg", "index": 2}])
    monkeypatch.setattr(upload_lock, "running_upload",
        lambda p: RunningUpload(LockHolder(pid=999, started_at="t", project="p",
                                           batch="Logging", live=False)))
    st = page_runs.compute_run_state(tmp_path / ".lock", logs)
    assert st.kind == "page_run_active"
    assert st.current == page_runs.CurrentItem(index=2, file="b.jpg")


def test_terminal_run_when_holder_pid_differs(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    run_dir = page_runs.new_run_dir(logs, "20260925T120000Z")
    page_runs.write_page_run(page_runs.PageRun(pid=1, project="p", batch="b",
                                               live=True, started_at="t", dir=run_dir))
    monkeypatch.setattr(upload_lock, "running_upload",
        lambda p: RunningUpload(LockHolder(pid=2, started_at="t2", project="p",
                                           batch="Waterfront", live=True)))
    st = page_runs.compute_run_state(tmp_path / ".lock", logs)
    assert st.kind == "terminal_run_active"
    assert st.holder is not None and st.holder.batch == "Waterfront"


def test_transient_holder_none_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_lock, "running_upload", lambda p: RunningUpload(None))
    st = page_runs.compute_run_state(tmp_path / ".lock", tmp_path / "logs")
    assert st.kind == "terminal_run_active" and st.holder is None


def test_finished_when_free_with_a_run(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    run_dir = page_runs.new_run_dir(logs, "20260925T120000Z")
    _write_jsonl(run_dir, [{"record": "run_header", "planned": 1},
                           {"record": "run_summary", "attempted": 1, "succeeded": 1,
                            "failures": [], "unconfirmed": [], "not_attempted": 0,
                            "rate_limited": False, "rate_limit_status": None,
                            "stopped_by_request": False, "skipped": []}])
    monkeypatch.setattr(upload_lock, "running_upload", lambda p: None)
    st = page_runs.compute_run_state(tmp_path / ".lock", logs)
    assert st.kind == "finished" and st.ending.kind == "completed"


def test_terminal_run_active_when_lock_held_but_no_run_dir_yet(tmp_path, monkeypatch):
    # Lock acquired before the page has written page-run.json for this run yet
    # (or logs_base has no page-runs at all) - can't be "our" run, so terminal.
    monkeypatch.setattr(upload_lock, "running_upload",
        lambda p: RunningUpload(LockHolder(pid=2, started_at="t2", project="p",
                                           batch=None, live=True)))
    st = page_runs.compute_run_state(tmp_path / ".lock", tmp_path / "logs")
    assert st.kind == "terminal_run_active"
    assert st.holder is not None and st.holder.pid == 2


def test_finished_page_run_is_none_when_page_run_json_missing(tmp_path, monkeypatch):
    # A run dir exists (JSONL got written) but page-run.json is missing/corrupt.
    logs = tmp_path / "logs"
    run_dir = page_runs.new_run_dir(logs, "20260925T120000Z")
    _write_jsonl(run_dir, [{"record": "run_header", "planned": 1}])
    monkeypatch.setattr(upload_lock, "running_upload", lambda p: None)
    st = page_runs.compute_run_state(tmp_path / ".lock", logs)
    assert st.kind == "finished" and st.page_run is None


# --- RunState.to_json -------------------------------------------------------


def test_idle_to_json():
    assert page_runs.Idle().to_json() == {"kind": "idle"}


def test_page_run_active_to_json():
    state = page_runs.PageRunActive(batch="Logging", live=False, started_at="t", done=1, planned=2)
    assert state.to_json() == {
        "kind": "page_run_active",
        "batch": "Logging",
        "live": False,
        "started_at": "t",
        "done": 1,
        "planned": 2,
        "current": None,
    }


def test_page_run_active_to_json_with_current_item():
    state = page_runs.PageRunActive(
        batch="Logging", live=False, started_at="t", done=1, planned=2,
        current=page_runs.CurrentItem(index=2, file="b.jpg"),
    )
    assert state.to_json()["current"] == {"index": 2, "file": "b.jpg"}


def test_terminal_run_active_to_json_with_holder():
    holder = LockHolder(pid=2, started_at="t2", project="p", batch="Waterfront", live=True)
    state = page_runs.TerminalRunActive(holder=holder)
    assert state.to_json() == {
        "kind": "terminal_run_active",
        "holder": {"started_at": "t2", "project": "p", "batch": "Waterfront", "live": True},
    }


def test_terminal_run_active_to_json_with_holder_none():
    state = page_runs.TerminalRunActive(holder=None)
    assert state.to_json() == {"kind": "terminal_run_active", "holder": None}


def test_finished_to_json_with_page_run(tmp_path):
    run_dir = tmp_path / "page-runs" / "20260925T120000Z"
    run_dir.mkdir(parents=True)
    ending = page_runs.Refused(reason_lines=["boom"])
    pr = page_runs.PageRun(pid=1, project="p", batch="b", live=True, started_at="t", dir=run_dir)
    state = page_runs.Finished(ending=ending, page_run=pr)
    assert state.to_json() == {
        "kind": "finished",
        "ending": {"kind": "refused", "reason_lines": ["boom"]},
        "page_run": {"pid": 1, "project": "p", "batch": "b", "live": True, "started_at": "t"},
    }


def test_finished_to_json_with_page_run_none():
    ending = page_runs.Refused(reason_lines=[])
    state = page_runs.Finished(ending=ending, page_run=None)
    assert state.to_json() == {
        "kind": "finished",
        "ending": {"kind": "refused", "reason_lines": []},
        "page_run": None,
    }
