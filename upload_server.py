"""HTTP server behind the upload page: static bundle + a small JSON API.

Serves the built `upload_page/dist` bundle and a JSON API the page's
frontend polls and drives. Runs under `ia_bulk.py serve` (Task 9), which is
why this module must NEVER `import ia_bulk` at module top: Task 9 adds
`import upload_server` to the top of ia_bulk.py, and a top-level
`import ia_bulk` here would make the two modules import each other before
either has finished loading. The two things this module needs from ia_bulk
-- load_registry (a plain json.load) and the TEST_COLLECTION constant -- are
obtained without a module-top import; see _load_registry and
_test_collection.

Binds 127.0.0.1 only. Every request is checked by a small guard (Host,
Origin, POST Content-Type) before it reaches any route -- see
UploadPageHandler._passes_guard.
"""
from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import build_stamp
import page_runs
import project_config
import stop_request
import upload_lock

# This file lives at the repo root (same level as ia_bulk.py), so its own
# directory IS the repo root -- used as `cwd` for the default read_commit,
# which must report the checkout's commit regardless of the server's cwd.
_MODULE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    """Which project/mode/bundle this server instance serves.

    page_dir has no dataclass-level default. A default that depends on
    another field (repo_root) can't be expressed as a plain class-level
    value without widening page_dir's declared type to `Path | None` for
    every reader (verified against this project's pyright config: a bare
    `= None` default on a `Path`-typed field is a reportAssignmentType
    error). build_config_from_args is where "page_dir defaults to
    repo_root/'upload_page'" actually lives; callers that build a
    ServerConfig directly (e.g. tests) apply the same default themselves.
    """

    project: str
    registry: str
    live: bool
    port: int
    repo_root: Path
    page_dir: Path

    @property
    def logs_base(self) -> Path:
        return self.repo_root / "logs"


def build_config_from_args(args, repo_root: Path) -> ServerConfig:
    """Builds a ServerConfig from the `serve` subcommand's parsed args.

    Used by ia_bulk.cmd_serve (Task 9); args is an argparse.Namespace with
    `project`, `registry`, `live`, `port` (matching every other cmd_* in
    ia_bulk.py, none of which type-annotate `args` either).
    """
    return ServerConfig(
        project=args.project,
        registry=args.registry,
        live=args.live,
        port=args.port,
        repo_root=repo_root,
        page_dir=repo_root / "upload_page",
    )


# ---------------------------------------------------------------------------
# Deps: every side effect the server performs, injectable for tests
# ---------------------------------------------------------------------------

RunValidate = Callable[[list[str]], "tuple[str, str, int]"]
SpawnUpload = Callable[[list[str], Path, Path], int]
SendStop = Callable[[int], None]
NowUtc = Callable[[], str]
ReadCommit = Callable[[], str]


def _default_run_validate(argv: list[str]) -> tuple[str, str, int]:
    """Runs argv to completion, capturing everything.

    Generic on purpose: this module doesn't know what argv means (Task 6
    builds the actual `[sys.executable, "ia_bulk.py", "validate", ...]`),
    it just knows how to run *some* command and hand back its output.
    """
    result = subprocess.run(argv, capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode


def _default_spawn_upload(argv: list[str], out_path: Path, cwd: Path) -> int:
    """Starts argv as a detached child, redirecting its combined output to out_path.

    Runs in its own process group (POSIX: start_new_session; Windows:
    CREATE_NEW_PROCESS_GROUP) so stop_request.request_stop's CTRL_BREAK /
    SIGINT reaches only this child, matching stop_request.py's contract.
    Real wiring (building argv, tracking the pid) is Task 7's job; this
    default just knows how to run *some* argv and hand back its pid.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    with open(out_path, "wb") as out_file:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=out_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            start_new_session=(sys.platform != "win32"),
        )
    return process.pid


def _default_now_utc() -> str:
    """A UTC stamp in the pipeline's own directory-sortable format (see ia_bulk.open_log)."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _default_read_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_MODULE_DIR,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    commit = result.stdout.strip()
    return commit or "unknown"


@dataclass
class ServerDeps:
    """Every side effect the server performs, each with a real default.

    `ServerDeps()` is production-ready on its own. A test overrides only
    the fields it cares about, e.g. `ServerDeps(read_commit=lambda: "dead")`
    -- a plain dataclass field is enough for this since none of the
    defaults are mutable objects (they're all plain functions), so there's
    no need for field(default_factory=...) anywhere here. See
    test_upload_server.py's `_fake_deps` for the pattern later tasks reuse.
    """

    run_validate: RunValidate = _default_run_validate
    spawn_upload: SpawnUpload = _default_spawn_upload
    send_stop: SendStop = stop_request.request_stop
    now_utc: NowUtc = _default_now_utc
    read_commit: ReadCommit = _default_read_commit


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


class _StartupRefusal(Exception):
    """A startup problem a launchd restart can't fix.

    run_server prints this and returns 0 instead of letting it propagate --
    any OTHER exception during startup is left to propagate so launchd
    restarts the server (a transient problem, unlike these three).
    """


def _load_registry(registry_path: str) -> dict:
    """Mirrors ia_bulk.load_registry (which is nothing but `json.load`)
    without importing ia_bulk at module load time -- see the module
    docstring's note on the circular-import constraint."""
    with open(registry_path, encoding="utf-8") as handle:
        return json.load(handle)


def _test_collection() -> str:
    """ia_bulk.TEST_COLLECTION, fetched lazily so this module never imports
    ia_bulk at load time (ia_bulk imports upload_server at ITS module top,
    added in Task 9) -- see the module docstring."""
    import ia_bulk  # local: see the module docstring

    return ia_bulk.TEST_COLLECTION


def _check_startup(config: ServerConfig) -> project_config.ProjectConfig:
    """The three refusals a restart can't fix, checked in order.

    Returns the resolved ProjectConfig (reused for /api/status's
    `collection`, so it isn't parsed twice) when none of them apply.
    """
    try:
        registry = _load_registry(config.registry)
    except (OSError, ValueError) as error:
        raise _StartupRefusal(f"cannot load registry '{config.registry}': {error}") from error

    try:
        resolved = project_config.load_project_config(registry, config.project)
    except (project_config.ConfigError, KeyError) as error:
        raise _StartupRefusal(str(error)) from error

    if build_stamp.read_committed_stamp(config.page_dir) is None:
        raise _StartupRefusal(
            f"no committed bundle stamp under {config.page_dir / 'dist'} "
            "- build the upload page (yarn build) before serving"
        )

    return resolved


# ---------------------------------------------------------------------------
# The server and its request handler
# ---------------------------------------------------------------------------


class _UploadServer(ThreadingHTTPServer):
    """Stores the per-run state UploadPageHandler reads via self.server.

    This is the "ThreadingHTTPServer subclass storing config/deps" half of
    the two designs the spec allows; make_handler is kept as the documented
    factory Tasks 6-8 call, but the state itself lives here rather than on
    the handler class.
    """

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler_cls: type[BaseHTTPRequestHandler],
        config: ServerConfig,
        deps: ServerDeps,
        resolved_project: project_config.ProjectConfig,
        commit: str,
    ) -> None:
        super().__init__(address, handler_cls)
        self.config = config
        self.deps = deps
        self.project_config = resolved_project
        # Cached once at server start (matches /api/health's contract); a
        # restart is required to pick up a new commit, which is fine -- the
        # server itself is what launchd restarts on a deploy.
        self.commit = commit


def make_handler(config: ServerConfig, deps: ServerDeps) -> type[BaseHTTPRequestHandler]:
    """Returns the request handler class to bind to the server.

    config/deps are accepted for interface symmetry with the rest of this
    module's factories, but state actually lives on the server instance
    (_UploadServer), which the handler reads via self.server -- see
    UploadPageHandler.app_server. Kept as a function (rather than exporting
    UploadPageHandler directly) so a later task has one seam to swap in a
    different handler class without touching every call site.
    """
    del config, deps
    return UploadPageHandler


class UploadPageHandler(BaseHTTPRequestHandler):
    """Static bundle + JSON API, guarded against DNS-rebinding and foreign origins.

    Quiet by design: log_message is a no-op so a normal request never prints
    anything. The server's own start/stop is logged by run_server /
    serve_in_thread; an unhandled exception inside a request is still
    printed by socketserver's default handle_error, which this class does
    not override.
    """

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass

    @property
    def app_server(self) -> _UploadServer:
        # self.server is typed as socketserver.BaseServer by typeshed; this
        # cast is the one place that narrows it back to what run_server /
        # serve_in_thread actually construct.
        return cast(_UploadServer, self.server)

    # -- request guard ------------------------------------------------

    def _passes_guard(self) -> bool:
        """Host/Origin/Content-Type checks that run before any route.

        Uses the server's actual bound port (not config.port) so this works
        whether the server was bound to a fixed port or, as in tests,
        port 0.
        """
        port = self.app_server.server_address[1]
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            self._send_error(403, f"unrecognized Host header {host!r}")
            return False

        origin = self.headers.get("Origin")
        if origin is not None and origin not in (
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        ):
            self._send_error(403, f"unrecognized Origin header {origin!r}")
            return False

        return True

    # -- routing --------------------------------------------------------

    def do_GET(self) -> None:
        if not self._passes_guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/health":
            self._handle_health()
        elif path == "/api/status":
            self._handle_status()
        elif path.startswith("/api/"):
            self._send_error(404, f"no such route: {path}")
        else:
            self._serve_static(path)

    def do_POST(self) -> None:
        if not self._passes_guard():
            return
        # Forces a CORS preflight the server never approves -- see the
        # module docstring / request-guard notes in the task brief.
        content_type = self.headers.get("Content-Type", "")
        if content_type.split(";")[0].strip().lower() != "application/json":
            self._send_error(415, "Content-Type must be application/json")
            return
        # No POST routes yet -- Task 7 adds /api/runs and /api/runs/current/stop.
        self._send_error(404, f"no such route: {self.path}")

    # -- routes -----------------------------------------------------------

    def _handle_health(self) -> None:
        server = self.app_server
        self._send_json(
            200,
            {
                "commit": server.commit,
                "bundle_stamp": build_stamp.read_committed_stamp(server.config.page_dir),
                "live": server.config.live,
                "project": server.config.project,
            },
        )

    def _handle_status(self) -> None:
        server = self.app_server
        config = server.config
        collection = server.project_config.ia_collection if config.live else _test_collection()
        run_state = page_runs.compute_run_state(upload_lock.UPLOAD_LOCK_PATH, config.logs_base)
        self._send_json(
            200,
            {
                "live": config.live,
                "project": config.project,
                "collection": collection,
                "run": run_state.to_json(),
            },
        )

    def _serve_static(self, path: str) -> None:
        """Serves a file under page_dir/dist; `/` maps to index.html.

        Resolved with os.path.normpath and checked against the dist root
        before anything is opened, so a path with `..` segments can never
        read a file outside dist -- see the task's "never serve outside
        dist" requirement.
        """
        dist = self.app_server.config.page_dir / "dist"
        dist_norm = os.path.normpath(str(dist))
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        candidate = os.path.normpath(os.path.join(dist_norm, relative))

        inside_dist = candidate == dist_norm or candidate.startswith(dist_norm + os.sep)
        if not inside_dist:
            self._send_error(404, "not found")
            return

        file_path = Path(candidate)
        if not file_path.is_file():
            self._send_error(404, "not found")
            return

        content_type, _ = mimetypes.guess_type(str(file_path))
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- response helpers ---------------------------------------------

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})


# ---------------------------------------------------------------------------
# Running the server
# ---------------------------------------------------------------------------


def _build_server(
    address: tuple[str, int],
    config: ServerConfig,
    deps: ServerDeps,
    resolved_project: project_config.ProjectConfig,
) -> _UploadServer:
    handler_cls = make_handler(config, deps)
    commit = deps.read_commit()
    return _UploadServer(address, handler_cls, config, deps, resolved_project, commit)


def run_server(config: ServerConfig, deps: ServerDeps | None = None) -> int:
    """Validates startup preconditions, then serves forever.

    Returns 0 for a refusal a launchd restart can't fix (unknown project,
    bad registry, missing/stale bundle) after printing why to stderr. Any
    other failure -- most notably the port already being in use -- is left
    to propagate as an exception, so launchd restarts the server.
    """
    deps = deps if deps is not None else ServerDeps()
    try:
        resolved_project = _check_startup(config)
    except _StartupRefusal as refusal:
        print(f"upload_server: refusing to start - {refusal}", file=sys.stderr)
        return 0

    server = _build_server(("127.0.0.1", config.port), config, deps, resolved_project)
    print(
        f"upload_server: listening on http://127.0.0.1:{config.port} "
        f"(project={config.project}, live={config.live})",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        print("upload_server: stopped", file=sys.stderr)
    return 0


@contextmanager
def serve_in_thread(config: ServerConfig, deps: ServerDeps | None = None) -> Iterator[str]:
    """Test helper: binds on port 0, serves in a daemon thread, yields the base URL.

    Runs the same startup validation as run_server -- a misconfigured test
    fixture should fail loudly here, not hang a background thread forever
    -- but does NOT swallow _StartupRefusal the way run_server does; that
    exit-0-and-print contract is specific to how launchd drives run_server.
    """
    deps = deps if deps is not None else ServerDeps()
    resolved_project = _check_startup(config)
    server = _build_server(("127.0.0.1", 0), config, deps, resolved_project)
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{actual_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
