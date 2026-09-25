"""Tests for upload_server.py: the request guard, static serving, /api/health,
/api/status, and the three startup refusals. Uses serve_in_thread (binds
port 0, real sockets on loopback -- allowed by conftest's network guard) so
these exercise the actual HTTP stack, not a mocked one."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import page_runs
import upload_server

PROJECT = "astoriaphotos"


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
