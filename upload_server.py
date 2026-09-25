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

# Populated by _default_spawn_upload, keyed by the run's own directory
# (out_path.parent, matching page_runs.PageRun.dir) rather than pid, since a
# pid can be reused after the child exits but a run directory never is.
# Task 8's SSE handler reads this to ask "has this run's child exited yet"
# via Popen.poll() without sending it a signal. A module-level dict (not an
# attribute on _UploadServer) because the default spawn_upload is a plain
# function matching the ServerDeps.spawn_upload signature -- it has no
# reference to the server instance that called it.
_spawned_upload_processes: dict[Path, subprocess.Popen[bytes]] = {}


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


def _process_group_kwargs() -> tuple[int, bool]:
    """(creationflags, start_new_session) that put the upload child in its own
    process group, so stop_request.request_stop's CTRL_BREAK (Windows) /
    SIGINT (POSIX) reaches only this child -- never the whole console session
    the server itself is running in.

    NEVER DETACHED_PROCESS or CREATE_NO_WINDOW here: CTRL_BREAK requires the
    child to still share the console (see stop_request.py's module docstring).
    """
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP, False
    return 0, True


def _default_spawn_upload(argv: list[str], out_path: Path, cwd: Path) -> int:
    """Starts argv as a child in its own process group, redirecting its
    combined stdout+stderr to out_path.

    out_path is opened in binary mode; PYTHONIOENCODING forces the child's
    own text output to UTF-8 regardless of the console's codepage (see
    docs/decisions -- "Console encoding"), so out_path is UTF-8 too and
    Task 8's SSE can slice it by byte offset without ever splitting a
    multi-byte character.

    Keeps the Popen in _spawned_upload_processes, keyed by out_path.parent
    (the run's directory) -- see that dict's comment.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    creationflags, start_new_session = _process_group_kwargs()
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with open(out_path, "wb") as out_file:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=out_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
    _spawned_upload_processes[out_path.parent] = process
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


def _last_nonempty_line(text: str) -> str | None:
    """The last non-blank line of `text`, or None when every line is blank.

    Used to pick validate's most relevant refusal reason out of stderr --
    validate may print several diagnostic lines before the one that
    actually explains why it produced no stdout.
    """
    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]
    return non_empty[-1] if non_empty else None


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


def _describe_run_state(state: page_runs.PageRunActive | page_runs.TerminalRunActive) -> str:
    """The 409 reason POST /api/runs gives when a run is actively going.

    "Actively going" means the upload lock is held -- PageRunActive or
    TerminalRunActive, never Idle or Finished. Both of those leave the lock
    free (Finished is a *past* run's ending, not a current one), so a new
    run is allowed to start over them -- see _handle_start_run's gate.
    """
    if isinstance(state, page_runs.PageRunActive):
        return f"a page run for batch '{state.batch}' is already in progress (started {state.started_at})"
    return state.holder.describe() if state.holder is not None else "another run holds the upload lock"


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
        # Run directories POST /api/runs/current/stop has already signaled --
        # makes the stop route idempotent (a second POST acks 202 without
        # sending a second, hard-stop signal). See UploadPageHandler._handle_stop_run.
        self.stopped_runs: set[Path] = set()


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
        elif path == "/api/themes":
            self._handle_themes()
        elif path == "/api/preview":
            self._handle_preview()
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
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/runs":
            self._handle_start_run()
        elif path == "/api/runs/current/stop":
            self._handle_stop_run()
        else:
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

    def _validate_argv(self, batch: str | None = None) -> list[str]:
        """Builds the real `validate --json` command line.

        The default run_validate just runs argv verbatim (see
        _default_run_validate's docstring) -- this is the one place that
        turns "run validate" into the actual
        `python ia_bulk.py validate --project P --registry R [--live]
        [--batch=value] --json` command a real run_validate executes.
        `--batch` is passed in equals form so a value starting with `-`
        (e.g. `-weird`) is never mistaken for a flag by argparse.
        """
        config = self.app_server.config
        argv = [
            sys.executable,
            "ia_bulk.py",
            "validate",
            "--project",
            config.project,
            "--registry",
            config.registry,
        ]
        if config.live:
            argv.append("--live")
        if batch is not None:
            argv.append(f"--batch={batch}")
        argv.append("--json")
        return argv

    def _handle_themes(self) -> None:
        stdout, stderr, _returncode = self.app_server.deps.run_validate(self._validate_argv())
        self._respond_with_validate_output(stdout, stderr)

    def _handle_preview(self) -> None:
        query = urllib.parse.urlsplit(self.path).query
        values = urllib.parse.parse_qs(query).get("batch")
        batch = values[0] if values else ""
        if not batch:
            self._send_error(400, "batch is required")
            return
        stdout, stderr, _returncode = self.app_server.deps.run_validate(self._validate_argv(batch))
        self._respond_with_validate_output(stdout, stderr)

    def _upload_argv(self, batch: str, run_dir: Path) -> list[str]:
        """Builds the real `upload` command line, mirroring _validate_argv's
        convention: `--batch` in equals form so a value starting with `-`
        (e.g. `-weird`) is never mistaken for a flag by argparse.
        """
        config = self.app_server.config
        argv = [
            sys.executable,
            "ia_bulk.py",
            "upload",
            "--project",
            config.project,
            "--registry",
            config.registry,
        ]
        if config.live:
            argv.append("--live")
        argv.append(f"--batch={batch}")
        argv.extend(["--log-dir", str(run_dir)])
        return argv

    def _read_json_body(self) -> object:
        """Parses the POST body as JSON. A missing/empty body parses to `{}`;
        `None` signals a *present* body that isn't valid JSON, distinct from
        that empty-body case."""
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _handle_start_run(self) -> None:
        body = self._read_json_body()
        if body is None:
            self._send_error(400, "invalid JSON body")
            return
        batch = body.get("batch") if isinstance(body, dict) else None
        if not isinstance(batch, str) or not batch:
            self._send_error(400, "batch is required")
            return

        server = self.app_server
        config = server.config
        state = page_runs.compute_run_state(upload_lock.UPLOAD_LOCK_PATH, config.logs_base)
        # Only refuse when the lock is actually held (a run is going right
        # now). Idle and Finished both leave the lock free -- Finished is a
        # *past* run's ending, not a current one -- so a new run is allowed
        # to start over either of them.
        if isinstance(state, (page_runs.PageRunActive, page_runs.TerminalRunActive)):
            self._send_error(409, _describe_run_state(state))
            return

        now = server.deps.now_utc()
        run_dir = page_runs.new_run_dir(config.logs_base, now)
        out_path = run_dir / page_runs.OUTPUT_FILENAME
        pid = server.deps.spawn_upload(self._upload_argv(batch, run_dir), out_path, config.repo_root)
        page_runs.write_page_run(
            page_runs.PageRun(
                pid=pid,
                project=config.project,
                batch=batch,
                live=config.live,
                started_at=now,
                dir=run_dir,
            )
        )
        self._send_json(202, {"started_at": now})

    def _handle_stop_run(self) -> None:
        """Signals the page's own run, guarding against pid reuse.

        The lock's holder and the newest page-run folder are two independent
        records of "what's running"; only signaling when they agree on the
        pid rules out a stale page-run folder pointing at a pid the OS has
        since handed to an unrelated process.
        """
        server = self.app_server
        running = upload_lock.running_upload(upload_lock.UPLOAD_LOCK_PATH)
        holder = running.holder if running is not None else None

        newest = page_runs.newest_run_dir(server.config.logs_base)
        page_run = page_runs.read_page_run(newest) if newest is not None else None

        if holder is None or page_run is None or holder.pid != page_run.pid:
            self._send_error(409, "no page run to stop")
            return

        # Idempotent: a second POST acks 202 without signaling again -- a
        # second real signal is stop_request's hard-stop escalation, which
        # only the operator pressing Stop twice should trigger.
        if page_run.dir not in server.stopped_runs:
            server.stopped_runs.add(page_run.dir)
            server.deps.send_stop(holder.pid)
        self._send_json(202, {})

    def _respond_with_validate_output(self, stdout: str, stderr: str) -> None:
        """Empty stdout is the refusal signal -- never the exit code.

        Nearly every real Sheet exits 1 because some row is broken, so a
        non-zero returncode is normal and carries no meaning here; only an
        empty (or whitespace-only) stdout means validate refused to run at
        all. On success, stdout is forwarded VERBATIM as the response body
        -- never parsed and re-serialized -- so the page's zod validates
        the exact document `validate --json` produced.
        """
        if not stdout.strip():
            self._send_error(502, _last_nonempty_line(stderr) or "validate refused")
            return
        body = stdout.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
