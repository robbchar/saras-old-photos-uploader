"""Tests for upload_server.py: the request guard, static serving, /api/health,
/api/status, and the three startup refusals. Uses serve_in_thread (binds
port 0, real sockets on loopback -- allowed by conftest's network guard) so
these exercise the actual HTTP stack, not a mocked one."""
from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path

import page_runs
import upload_lock
import upload_server

PROJECT = "astoriaphotos"
FIXTURES = Path(__file__).resolve().parent / "contract_fixtures"


def _get(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        response = urllib.request.urlopen(request)
        return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _post(url: str, headers: dict[str, str] | None = None, body: bytes = b"{}") -> tuple[int, bytes]:
    request = urllib.request.Request(url, method="POST", headers=headers or {}, data=body)
    try:
        response = urllib.request.urlopen(request)
        return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def _post_json(url: str, payload: object) -> tuple[int, bytes]:
    return _post(url, headers={"Content-Type": "application/json"}, body=json.dumps(payload).encode("utf-8"))


def _write_registry(tmp_path: Path, project: str = PROJECT) -> Path:
    registry = {
        "collection_key": "lcps",
        "projects": {
            project: {
                "mediatype": "image",
                "ia_collection": f"{project}collection",
                "sheet_id": "realsheetid123",
                "test_sheet_id": "testsheetid456",
                "sheet_tab": "Sheet1",
                "files_dir": "./data",
                "file_template": "{file_name}",
                "required_for_upload": ["title"],
            }
        },
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry), encoding="utf-8")
    return path


def _write_dist(page_dir: Path, stamp: str = "abc123", index_html: str = "<!doctype html><title>t</title>") -> None:
    dist = page_dir / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "build-stamp.json").write_text(json.dumps({"stamp": stamp}), encoding="utf-8")
    (dist / "index.html").write_text(index_html, encoding="utf-8")


def _make_config(
    tmp_path: Path,
    *,
    project: str = PROJECT,
    live: bool = False,
    port: int = 0,
    write_dist: bool = True,
    write_registry: bool = True,
) -> upload_server.ServerConfig:
    registry_path = _write_registry(tmp_path, project) if write_registry else tmp_path / "no-such-registry.json"
    page_dir = tmp_path / "upload_page"
    if write_dist:
        _write_dist(page_dir)
    return upload_server.ServerConfig(
        project=project,
        registry=str(registry_path),
        live=live,
        port=port,
        repo_root=tmp_path,
        page_dir=page_dir,
    )


def _raise_run_validate(argv: list[str]) -> tuple[str, str, int]:
    raise NotImplementedError("run_validate is not wired up in this test")


def _raise_spawn_upload(argv: list[str], out_path: Path, cwd: Path) -> int:
    raise NotImplementedError("spawn_upload is not wired up in this test")


def _fake_deps(*, commit: str = "deadbeef", **overrides) -> upload_server.ServerDeps:
    """A ServerDeps whose run_validate/spawn_upload raise unless overridden;
    read_commit always returns `commit` unless a caller overrides that too.

    Built via dataclasses.replace (not ServerDeps(**{...base, **overrides}))
    because unpacking a plain dict whose values are a union of differently-
    shaped callables defeats pyright's per-field checking -- replace() keeps
    each override checked against its own field's type.
    """
    base = upload_server.ServerDeps(
        run_validate=_raise_run_validate,
        spawn_upload=_raise_spawn_upload,
        read_commit=lambda: commit,
    )
    return replace(base, **overrides) if overrides else base


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


def test_health_reports_stamp_mode_project(tmp_path):
    cfg = _make_config(tmp_path, project=PROJECT, live=False)
    deps = _fake_deps(commit="deadbeef")
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _get(base + "/api/health")
        assert status == 200
        assert json.loads(body) == {
            "commit": "deadbeef",
            "bundle_stamp": "abc123",
            "live": False,
            "project": PROJECT,
        }


def test_health_reflects_live_flag(tmp_path):
    cfg = _make_config(tmp_path, project=PROJECT, live=True)
    deps = _fake_deps(commit="cafef00d")
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _get(base + "/api/health")
        assert status == 200
        payload = json.loads(body)
        assert payload["live"] is True
        assert payload["commit"] == "cafef00d"


# ---------------------------------------------------------------------------
# Request guard
# ---------------------------------------------------------------------------


def test_bad_host_is_rejected(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/api/health", headers={"Host": "evil.com"})
        assert status == 403
        assert "error" in json.loads(body)


def test_present_foreign_origin_is_rejected(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/api/health", headers={"Origin": "http://evil.example"})
        assert status == 403
        assert "error" in json.loads(body)


def test_absent_origin_is_allowed(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, _body = _get(base + "/api/health")
        assert status == 200


def test_matching_origin_is_allowed(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, _body = _get(base + "/api/health", headers={"Origin": base})
        assert status == 200


def test_post_without_json_content_type_is_415(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _post(base + "/api/anything", headers={"Content-Type": "text/plain"})
        assert status == 415
        assert "error" in json.loads(body)


def test_post_with_json_content_type_passes_the_guard(tmp_path):
    # No POST routes exist yet in this task; a correctly-typed POST should
    # clear the guard and reach routing (404), never 415.
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, _body = _post(base + "/api/anything", headers={"Content-Type": "application/json"})
        assert status == 404


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------


def test_root_serves_committed_index(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/")
        assert status == 200
        assert body == b"<!doctype html><title>t</title>"


def test_missing_static_file_is_404(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/assets/does-not-exist.js")
        assert status == 404
        assert "error" in json.loads(body)


def test_static_path_escape_is_404(tmp_path):
    # A secret file that sits next to (not under) dist -- must never be served.
    (tmp_path / "upload_page" / "secret.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "upload_page" / "secret.txt").write_text("do not serve me", encoding="utf-8")
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, _body = _get(base + "/../secret.txt")
        assert status == 404


def test_static_asset_served_with_content(tmp_path):
    cfg = _make_config(tmp_path)
    (cfg.page_dir / "dist" / "assets").mkdir(parents=True, exist_ok=True)
    (cfg.page_dir / "dist" / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/assets/app.js")
        assert status == 200
        assert body == b"console.log(1)"


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------


def test_status_reports_collection_and_run_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(page_runs, "compute_run_state", lambda lock_path, logs_base: page_runs.Idle())
    cfg = _make_config(tmp_path, live=False)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/api/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["live"] is False
        assert payload["project"] == PROJECT
        assert payload["collection"] == "test_collection"
        assert payload["run"] == {"kind": "idle"}


def test_status_reports_real_collection_when_live(tmp_path, monkeypatch):
    monkeypatch.setattr(page_runs, "compute_run_state", lambda lock_path, logs_base: page_runs.Idle())
    cfg = _make_config(tmp_path, live=True)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/api/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["collection"] == f"{PROJECT}collection"


def test_status_reports_page_run_active_state(tmp_path, monkeypatch):
    active = page_runs.PageRunActive(
        batch="B1", live=False, started_at="20260101T000000Z", done=3, planned=10
    )
    monkeypatch.setattr(page_runs, "compute_run_state", lambda lock_path, logs_base: active)
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/api/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["run"] == {
            "kind": "page_run_active",
            "batch": "B1",
            "live": False,
            "started_at": "20260101T000000Z",
            "done": 3,
            "planned": 10,
        }


def test_status_reports_finished_state(tmp_path, monkeypatch):
    finished = page_runs.Finished(
        ending=page_runs.Completed(summary={"uploaded": 5}), page_run=None
    )
    monkeypatch.setattr(page_runs, "compute_run_state", lambda lock_path, logs_base: finished)
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/api/status")
        assert status == 200
        payload = json.loads(body)
        assert payload["run"] == {
            "kind": "finished",
            "ending": {"kind": "completed", "summary": {"uploaded": 5}},
            "page_run": None,
        }


# ---------------------------------------------------------------------------
# /api/themes and /api/preview
# ---------------------------------------------------------------------------


def test_themes_passes_validate_json_through(tmp_path):
    all_doc = (FIXTURES / "validate-all.json").read_text(encoding="utf-8")
    deps = _fake_deps(run_validate=lambda argv: (all_doc, "12:00 checked\n", 1))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _get(base + "/api/themes")
        assert status == 200
        # Byte-for-byte, not just value-equal: dict equality would still pass
        # a json.loads-then-json.dumps re-serialization that reorders keys,
        # which is exactly the regression this pins against.
        assert body.decode("utf-8") == all_doc
        assert json.loads(body) == json.loads(all_doc)


def test_themes_response_content_type_is_json(tmp_path):
    all_doc = (FIXTURES / "validate-all.json").read_text(encoding="utf-8")
    deps = _fake_deps(run_validate=lambda argv: (all_doc, "", 1))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        request = urllib.request.Request(base + "/api/themes")
        response = urllib.request.urlopen(request)
        assert response.headers.get("Content-Type") == "application/json"


def test_themes_builds_the_real_validate_argv(tmp_path):
    seen = {}

    def fake(argv):
        seen["argv"] = argv
        return ("{}", "", 0)

    deps = _fake_deps(run_validate=fake)
    cfg = _make_config(tmp_path, project=PROJECT, live=False)
    with upload_server.serve_in_thread(cfg, deps) as base:
        _get(base + "/api/themes")
    # The DEFAULT run_validate does `subprocess.run(argv, ...)` verbatim --
    # this route is responsible for the full real command (see
    # upload_server._default_run_validate's docstring).
    assert seen["argv"][0] == upload_server.sys.executable
    assert seen["argv"][1:4] == ["ia_bulk.py", "validate", "--project"]
    assert "--registry" in seen["argv"]
    assert "--json" in seen["argv"]
    assert "--live" not in seen["argv"]
    assert "--batch" not in " ".join(seen["argv"])


def test_themes_adds_live_flag_when_configured_live(tmp_path):
    seen = {}

    def fake(argv):
        seen["argv"] = argv
        return ("{}", "", 0)

    deps = _fake_deps(run_validate=fake)
    cfg = _make_config(tmp_path, project=PROJECT, live=True)
    with upload_server.serve_in_thread(cfg, deps) as base:
        _get(base + "/api/themes")
    assert "--live" in seen["argv"]


def test_preview_passes_validate_json_through(tmp_path):
    batch_doc = (FIXTURES / "validate-batch.json").read_text(encoding="utf-8")
    deps = _fake_deps(run_validate=lambda argv: (batch_doc, "", 1))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _get(base + "/api/preview?batch=logging")
        assert status == 200
        # Byte-for-byte, not just value-equal -- see the themes test's
        # comment: dict equality alone wouldn't catch a re-serialization
        # that reorders keys but keeps the same values.
        assert body.decode("utf-8") == batch_doc
        assert json.loads(body) == json.loads(batch_doc)


def test_preview_response_content_type_is_json(tmp_path):
    batch_doc = (FIXTURES / "validate-batch.json").read_text(encoding="utf-8")
    deps = _fake_deps(run_validate=lambda argv: (batch_doc, "", 1))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        request = urllib.request.Request(base + "/api/preview?batch=logging")
        response = urllib.request.urlopen(request)
        assert response.headers.get("Content-Type") == "application/json"


def test_preview_sends_batch_in_equals_form(tmp_path):
    seen = {}
    batch_doc = (FIXTURES / "validate-batch.json").read_text(encoding="utf-8")

    def fake(argv):
        seen["argv"] = argv
        return (batch_doc, "", 1)

    deps = _fake_deps(run_validate=fake)
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        _get(base + "/api/preview?batch=" + urllib.parse.quote("-weird"))
    assert "--batch=-weird" in seen["argv"]


def test_preview_missing_batch_is_400(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/api/preview")
        assert status == 400
        assert json.loads(body) == {"error": "batch is required"}


def test_preview_empty_batch_value_is_400(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _get(base + "/api/preview?batch=")
        assert status == 400
        assert json.loads(body) == {"error": "batch is required"}


def test_empty_stdout_is_refusal_502(tmp_path):
    deps = _fake_deps(run_validate=lambda argv: ("", "sheet_id is a placeholder\n", 1))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _get(base + "/api/themes")
        assert status == 502
        assert json.loads(body)["error"] == "sheet_id is a placeholder"


def test_whitespace_only_stdout_is_refusal_502(tmp_path):
    deps = _fake_deps(run_validate=lambda argv: ("   \n", "row 4 is broken\n", 1))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _get(base + "/api/themes")
        assert status == 502
        assert json.loads(body)["error"] == "row 4 is broken"


def test_empty_stdout_and_stderr_uses_fallback_message(tmp_path):
    deps = _fake_deps(run_validate=lambda argv: ("", "", 1))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _get(base + "/api/themes")
        assert status == 502
        assert json.loads(body)["error"] == "validate refused"


def test_empty_stdout_reason_is_last_nonempty_stderr_line(tmp_path):
    stderr = "warming up...\n\nsheet_id is a placeholder\n"
    deps = _fake_deps(run_validate=lambda argv: ("", stderr, 1))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _get(base + "/api/themes")
        assert status == 502
        assert json.loads(body)["error"] == "sheet_id is a placeholder"


# ---------------------------------------------------------------------------
# POST /api/runs
# ---------------------------------------------------------------------------


def test_post_runs_spawns_and_writes_page_run(tmp_path, monkeypatch):
    spawned = {}

    def fake_spawn(argv, out_path, cwd):
        spawned["argv"] = argv
        spawned["out_path"] = out_path
        spawned["cwd"] = cwd
        return 4242

    monkeypatch.setattr(page_runs, "compute_run_state", lambda lock_path, logs_base: page_runs.Idle())
    deps = _fake_deps(spawn_upload=fake_spawn, now_utc=lambda: "20260925T130000Z")
    cfg = _make_config(tmp_path, project=PROJECT, live=False)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _post_json(base + "/api/runs", {"batch": "Logging"})
    assert status == 202
    assert json.loads(body) == {"started_at": "20260925T130000Z"}
    assert "--batch=Logging" in spawned["argv"]
    assert "--log-dir" in spawned["argv"]
    assert "--live" not in spawned["argv"]
    assert spawned["cwd"] == cfg.repo_root

    run_dir = page_runs.newest_run_dir(tmp_path / "logs")
    assert run_dir is not None
    saved = page_runs.read_page_run(run_dir)
    assert saved is not None
    assert saved.pid == 4242
    assert saved.project == PROJECT
    assert saved.batch == "Logging"
    assert saved.live is False
    assert saved.started_at == "20260925T130000Z"
    assert spawned["out_path"] == run_dir / "output.txt"


def test_post_runs_adds_live_flag_when_configured_live(tmp_path, monkeypatch):
    spawned = {}

    def fake_spawn(argv, out_path, cwd):
        spawned["argv"] = argv
        return 1

    monkeypatch.setattr(page_runs, "compute_run_state", lambda lock_path, logs_base: page_runs.Idle())
    deps = _fake_deps(spawn_upload=fake_spawn, now_utc=lambda: "20260925T130000Z")
    cfg = _make_config(tmp_path, project=PROJECT, live=True)
    with upload_server.serve_in_thread(cfg, deps) as base:
        _post_json(base + "/api/runs", {"batch": "Logging"})
    assert "--live" in spawned["argv"]


def test_post_runs_409_when_a_run_is_going(tmp_path, monkeypatch):
    monkeypatch.setattr(
        page_runs, "compute_run_state", lambda lock_path, logs_base: page_runs.TerminalRunActive(holder=None)
    )
    called = []
    deps = _fake_deps(spawn_upload=lambda argv, out_path, cwd: called.append(argv) or 1)
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _post_json(base + "/api/runs", {"batch": "Logging"})
    assert status == 409
    assert "error" in json.loads(body)
    assert called == []


def test_post_runs_409_when_a_page_run_is_active(tmp_path, monkeypatch):
    active = page_runs.PageRunActive(
        batch="B1", live=False, started_at="20260101T000000Z", done=1, planned=5
    )
    monkeypatch.setattr(page_runs, "compute_run_state", lambda lock_path, logs_base: active)
    called = []
    deps = _fake_deps(spawn_upload=lambda argv, out_path, cwd: called.append(argv) or 1)
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, _body = _post_json(base + "/api/runs", {"batch": "Logging"})
    assert status == 409
    assert called == []


def test_post_runs_allowed_when_previous_run_finished(tmp_path, monkeypatch):
    # Finished means the lock is free (the previous run ended, however it
    # ended) -- unlike an active run, it must NOT block starting a new one;
    # the page's own flow is "choose another theme" -> Start right after.
    finished = page_runs.Finished(
        ending=page_runs.Completed(summary={"uploaded": 5}), page_run=None
    )
    monkeypatch.setattr(page_runs, "compute_run_state", lambda lock_path, logs_base: finished)
    spawned = {}

    def fake_spawn(argv, out_path, cwd):
        spawned["argv"] = argv
        return 7777

    deps = _fake_deps(spawn_upload=fake_spawn, now_utc=lambda: "20260925T140000Z")
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _post_json(base + "/api/runs", {"batch": "Logging"})
    assert status == 202
    assert json.loads(body) == {"started_at": "20260925T140000Z"}
    assert "argv" in spawned

    run_dir = page_runs.newest_run_dir(tmp_path / "logs")
    assert run_dir is not None
    saved = page_runs.read_page_run(run_dir)
    assert saved is not None
    assert saved.pid == 7777


def test_post_runs_missing_batch_is_400(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _post_json(base + "/api/runs", {})
    assert status == 400
    assert json.loads(body) == {"error": "batch is required"}


def test_post_runs_empty_batch_is_400(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _post_json(base + "/api/runs", {"batch": ""})
    assert status == 400
    assert json.loads(body) == {"error": "batch is required"}


def test_post_runs_malformed_json_is_400(tmp_path):
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, body = _post(base + "/api/runs", headers={"Content-Type": "application/json"}, body=b"{not json")
    assert status == 400


def test_post_runs_oversized_body_is_400_not_buffered(tmp_path):
    # A Content-Length past _MAX_JSON_BODY_BYTES is refused outright -- this
    # pins that the cap is enforced (not a real memory-exhaustion test, which
    # would be impractical to run in the suite).
    oversized_batch = "x" * (upload_server._MAX_JSON_BODY_BYTES + 1)
    body = json.dumps({"batch": oversized_batch}).encode("utf-8")
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, _fake_deps()) as base:
        status, _body = _post(base + "/api/runs", headers={"Content-Type": "application/json"}, body=body)
    assert status == 400


# ---------------------------------------------------------------------------
# POST /api/runs/current/stop
# ---------------------------------------------------------------------------


def _write_matching_page_run(tmp_path: Path, pid: int, now: str = "20260925T130000Z") -> Path:
    run_dir = page_runs.new_run_dir(tmp_path / "logs", now)
    page_runs.write_page_run(
        page_runs.PageRun(pid=pid, project=PROJECT, batch="Logging", live=False, started_at=now, dir=run_dir)
    )
    return run_dir


def _running(pid: int) -> upload_lock.RunningUpload:
    return upload_lock.RunningUpload(
        holder=upload_lock.LockHolder(
            pid=pid, started_at="20260925T130000Z", project=PROJECT, batch="Logging", live=False
        )
    )


def test_stop_signals_only_the_page_runs_pid(tmp_path, monkeypatch):
    holder_pid = 5150
    _write_matching_page_run(tmp_path, holder_pid)
    monkeypatch.setattr(upload_lock, "running_upload", lambda lock_path: _running(holder_pid))
    sent = []
    deps = _fake_deps(send_stop=lambda pid: sent.append(pid))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, _body = _post_json(base + "/api/runs/current/stop", {})
    assert status == 202
    assert sent == [holder_pid]


def test_stop_409_when_holder_pid_does_not_match_page_run(tmp_path, monkeypatch):
    _write_matching_page_run(tmp_path, pid=111)
    monkeypatch.setattr(upload_lock, "running_upload", lambda lock_path: _running(222))
    sent = []
    deps = _fake_deps(send_stop=lambda pid: sent.append(pid))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _post_json(base + "/api/runs/current/stop", {})
    assert status == 409
    assert json.loads(body) == {"error": "no page run to stop"}
    assert sent == []


def test_stop_409_when_nothing_running(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_lock, "running_upload", lambda lock_path: None)
    sent = []
    deps = _fake_deps(send_stop=lambda pid: sent.append(pid))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status, body = _post_json(base + "/api/runs/current/stop", {})
    assert status == 409
    assert json.loads(body) == {"error": "no page run to stop"}
    assert sent == []


def test_stop_is_idempotent(tmp_path, monkeypatch):
    holder_pid = 6060
    _write_matching_page_run(tmp_path, holder_pid)
    monkeypatch.setattr(upload_lock, "running_upload", lambda lock_path: _running(holder_pid))
    sent = []
    deps = _fake_deps(send_stop=lambda pid: sent.append(pid))
    cfg = _make_config(tmp_path)
    with upload_server.serve_in_thread(cfg, deps) as base:
        status1, _body1 = _post_json(base + "/api/runs/current/stop", {})
        status2, _body2 = _post_json(base + "/api/runs/current/stop", {})
    assert status1 == 202
    assert status2 == 202
    assert sent == [holder_pid]


def test_stop_lock_serializes_concurrent_requests(tmp_path, monkeypatch):
    """Two near-simultaneous stop POSTs (double-click, a retried fetch, two
    open tabs) must not both slip past the idempotency check before either
    records it -- that would fire send_stop twice, and per stop_request's
    own contract a second signal is a HARD stop, not a no-op. Forces the
    race deterministically: the first call to send_stop blocks (holding
    _UploadServer.stop_lock) until this test releases it, giving a second,
    concurrent POST every chance to race past the check if the lock were
    missing. Every wait is bounded so a regression fails fast instead of
    hanging the suite.
    """
    holder_pid = 7070
    _write_matching_page_run(tmp_path, holder_pid)
    monkeypatch.setattr(upload_lock, "running_upload", lambda lock_path: _running(holder_pid))

    call_count_lock = threading.Lock()
    calls: list[int] = []
    first_call_entered = threading.Event()
    release_first_call = threading.Event()

    def fake_send_stop(pid: int) -> None:
        with call_count_lock:
            calls.append(pid)
            is_first_call = len(calls) == 1
        if is_first_call:
            first_call_entered.set()
            released = release_first_call.wait(timeout=5)
            assert released, "test setup: release_first_call was never signaled"

    deps = _fake_deps(send_stop=fake_send_stop)
    cfg = _make_config(tmp_path)
    results: list[tuple[int, bytes]] = []

    with upload_server.serve_in_thread(cfg, deps) as base:

        def post_stop() -> None:
            results.append(_post_json(base + "/api/runs/current/stop", {}))

        first = threading.Thread(target=post_stop)
        first.start()
        assert first_call_entered.wait(timeout=5), "first stop request never reached send_stop"

        second = threading.Thread(target=post_stop)
        second.start()
        # If stop_lock weren't held across the whole check-then-add-then-signal
        # section, `second` could reach send_stop right now (`calls` still has
        # only the first entry). Give it a real chance to do so, then prove it
        # didn't: still blocked behind the lock, not a second send_stop call.
        second.join(timeout=1.0)
        assert second.is_alive(), "second stop request should still be blocked behind stop_lock"
        assert calls == [holder_pid]

        release_first_call.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive()
        assert not second.is_alive()

    assert calls == [holder_pid]
    assert len(results) == 2
    assert [status for status, _body in results] == [202, 202]


# ---------------------------------------------------------------------------
# Default spawn_upload: the process-group spawn that makes Stop reachable
# ---------------------------------------------------------------------------


def test_default_spawn_upload_uses_process_group_and_utf8_env(tmp_path, monkeypatch):
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.pid = 9999

    monkeypatch.setattr(upload_server.subprocess, "Popen", _FakePopen)
    out_path = tmp_path / "run" / "output.txt"

    pid = upload_server._default_spawn_upload(["ia_bulk.py", "upload"], out_path, tmp_path)

    assert pid == 9999
    kwargs = captured["kwargs"]

    if sys.platform == "win32":
        assert kwargs["creationflags"] == upload_server.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True

    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert kwargs["stdout"].name == str(out_path)
    assert kwargs["stderr"] == upload_server.subprocess.STDOUT

    # Never DETACHED_PROCESS/CREATE_NO_WINDOW -- CTRL_BREAK must reach a child
    # that still shares the console (see stop_request.request_stop).
    creationflags = kwargs.get("creationflags", 0)
    detached = getattr(upload_server.subprocess, "DETACHED_PROCESS", None)
    no_window = getattr(upload_server.subprocess, "CREATE_NO_WINDOW", None)
    if detached is not None:
        assert not (creationflags & detached)
    if no_window is not None:
        assert not (creationflags & no_window)


# ---------------------------------------------------------------------------
# Startup refusals
# ---------------------------------------------------------------------------


def test_startup_refuses_when_bundle_missing_returns_0(tmp_path, capsys):
    cfg = _make_config(tmp_path, write_dist=False)
    result = upload_server.run_server(cfg, _fake_deps())
    assert result == 0
    assert "bundle" in capsys.readouterr().err.lower()


def test_startup_refuses_when_project_unknown_returns_0(tmp_path, capsys):
    cfg = _make_config(tmp_path, project="not-a-registered-project")
    result = upload_server.run_server(cfg, _fake_deps())
    assert result == 0
    err = capsys.readouterr().err
    assert "not-a-registered-project" in err


def test_startup_refuses_when_registry_missing_returns_0(tmp_path, capsys):
    cfg = _make_config(tmp_path, write_registry=False)
    result = upload_server.run_server(cfg, _fake_deps())
    assert result == 0
    assert "registry" in capsys.readouterr().err.lower()


def test_startup_refuses_when_registry_is_malformed_json_returns_0(tmp_path, capsys):
    cfg = _make_config(tmp_path)
    Path(cfg.registry).write_text("{not valid json", encoding="utf-8")
    result = upload_server.run_server(cfg, _fake_deps())
    assert result == 0
    assert "registry" in capsys.readouterr().err.lower()
