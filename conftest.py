import errno
import ipaddress
import socket

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture(scope="session")
def _missing_service_account_key_path(tmp_path_factory):
    return tmp_path_factory.mktemp("no-real-key") / "google-service-account.json"


@pytest.fixture(autouse=True)
def _no_test_reads_the_real_service_account_key(monkeypatch, _missing_service_account_key_path):
    """No test should ever reach the real key; point the default at a path that does not exist."""
    # By dotted name, so google_auth is first imported after pytest_configure installs the network guard.
    monkeypatch.setattr("google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH", _missing_service_account_key_path)


@pytest.fixture(scope="session")
def _empty_ia_config_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("no-real-ia-config") / "ia.ini"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_test_reads_the_real_ia_config(monkeypatch, _empty_ia_config_path):
    """Must be a file that exists: internetarchive skips a missing IA_CONFIG_FILE and reads ~/.config."""
    monkeypatch.setenv("IA_CONFIG_FILE", str(_empty_ia_config_path))
    monkeypatch.delenv("IA_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("IA_SECRET_ACCESS_KEY", raising=False)


@pytest.fixture(autouse=True)
def _no_test_takes_the_real_upload_lock(monkeypatch, tmp_path):
    """The real lock is the checkout's .ignored/upload.lock; a test holding it would refuse a real upload."""
    monkeypatch.setattr("upload_lock.UPLOAD_LOCK_PATH", tmp_path / "upload-lock" / "upload.lock")


def _is_local(host: object) -> bool:
    """None or "" means this machine, as do "localhost", loopback and unspecified addresses."""
    if host in (None, "", b""):
        return True
    host_text = host.decode() if isinstance(host, bytes) else str(host)
    name = host_text.lower().rstrip(".")
    # Not *.localhost: many resolvers ignore RFC 6761 and send those names to DNS.
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name.split("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _host_of(address: object) -> object:
    """AF_INET/AF_INET6 addresses are tuples; anything else (an AF_UNIX path) is local."""
    return address[0] if isinstance(address, tuple) else None


# Each guarded call, how to find the host in its arguments, and what a real failure raises.
_GUARDED_LOOKUPS = {
    "getaddrinfo": (lambda host, *_args, **_kwargs: host, socket.gaierror),
    "gethostbyname": (lambda host: host, socket.gaierror),
    "gethostbyname_ex": (lambda host: host, socket.gaierror),
    "gethostbyaddr": (lambda address: address, socket.herror),
    "getnameinfo": (lambda sockaddr, _flags: _host_of(sockaddr), socket.gaierror),
}
# Each guarded method, how to find the host, and whether a real failure returns an errno instead of raising.
_GUARDED_SOCKET_METHODS = {
    "connect": (_host_of, False),
    "connect_ex": (_host_of, True),
    "sendto": (lambda _data, *flags_and_address: _host_of(flags_and_address[-1]), False),
}
_REFUSAL_TEXT = "the suite must not reach the network"
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class _NetworkGuard:
    """Refuses non-local lookups and connections, and records each so a refusal the code swallows is still reported."""

    def __init__(self) -> None:
        self._attempts: list[str] = []
        self._patches = pytest.MonkeyPatch()
        self._proxy_patches = pytest.MonkeyPatch()
        self._network_allowed = False

    def install(self) -> None:
        for name, (host_of, error) in _GUARDED_LOOKUPS.items():
            self._patches.setattr(socket, name, self._guarded_lookup(getattr(socket, name), host_of, error))
        for name, (host_of, returns_errno) in _GUARDED_SOCKET_METHODS.items():
            real_method = getattr(socket.socket, name)
            guarded = self._guarded_socket_method(real_method, host_of, returns_errno)
            self._patches.setattr(socket.socket, name, guarded)
        self._strip_proxies()

    def uninstall(self) -> None:
        self._proxy_patches.undo()
        self._patches.undo()

    def allow_network(self) -> None:
        """For one opted-in e2e test: sockets pass and the stripped proxy settings are back."""
        self._network_allowed = True
        self._proxy_patches.undo()

    def refuse_network(self) -> None:
        if self._network_allowed:
            self._network_allowed = False
            self._strip_proxies()

    def _strip_proxies(self) -> None:
        # A loopback proxy would be the only connection the guard sees; "*" also overrides a Windows registry proxy.
        for variable in _PROXY_VARIABLES:
            self._proxy_patches.delenv(variable, raising=False)
        self._proxy_patches.setenv("NO_PROXY", "*")
        self._proxy_patches.setenv("no_proxy", "*")

    def mark(self) -> int:
        return len(self._attempts)

    def claim_since(self, mark: int) -> list[str]:
        """Attempts since `mark`, removed so an enclosing window does not report them again."""
        claimed = self._attempts[mark:]
        del self._attempts[mark:]
        return claimed

    def _refusal_unless_local(self, action: str, host: object) -> str | None:
        """The refusal message for a non-local host, after recording the attempt; None for a local one."""
        if self._network_allowed or _is_local(host):
            return None
        self._attempts.append(f"{action} {host!r}")
        return f"test tried to {action} {host!r}; {_REFUSAL_TEXT}"

    def _guarded_lookup(self, real_lookup, host_of, error):
        def guarded(*args, **kwargs):
            refusal = self._refusal_unless_local("look up", host_of(*args, **kwargs))
            if refusal is not None:
                raise error(refusal)
            return real_lookup(*args, **kwargs)

        return guarded

    def _guarded_socket_method(self, real_method, host_of, returns_errno: bool):
        def guarded(sock, *args, **kwargs):
            refusal = self._refusal_unless_local("reach", host_of(*args, **kwargs))
            if refusal is not None:
                if returns_errno:
                    return errno.ECONNREFUSED
                raise OSError(refusal)
            return real_method(sock, *args, **kwargs)

        return guarded


def _caused_by_a_refusal(error: BaseException | None) -> bool:
    """Whether the guard's refusal is `error` or anywhere in its cause/context chain."""
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        if _REFUSAL_TEXT in str(error):
            return True
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return False


def _describe(attempts: list[str]) -> str:
    return "tried to reach the network: " + ", ".join(attempts)


_GUARD = pytest.StashKey[_NetworkGuard]()
_ITEM_MARK = pytest.StashKey[int]()
_ITEM_FAILED_ON_A_REFUSAL = pytest.StashKey[bool]()


def pytest_addoption(parser):
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="run the e2e rehearsal against the real Test Sheet and IA test_collection",
    )


def pytest_configure(config):
    """Installed for the whole run, so collection and session/module fixtures are guarded too."""
    config.addinivalue_line("markers", "e2e: real Test Sheet and IA test_collection; needs --run-e2e")
    guard = _NetworkGuard()
    guard.install()
    config.stash[_GUARD] = guard


def pytest_unconfigure(config):
    guard = config.stash.get(_GUARD, None)
    if guard is not None:
        guard.uninstall()


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-e2e"):
        return
    skip_e2e = pytest.mark.skip(reason="e2e rehearsal: pass --run-e2e to run it")
    for item in items:
        if item.get_closest_marker("e2e") is not None:
            item.add_marker(skip_e2e)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    guard = collector.config.stash[_GUARD]
    mark = guard.mark()
    report = yield
    attempts = guard.claim_since(mark)
    # A skip counts too: "probe the network, skip the module if offline" would otherwise hide the attempt.
    if attempts and not report.failed:
        report.outcome = "failed"
        report.longrepr = f"collecting {collector.nodeid or 'the session'} {_describe(attempts)}"
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Before any fixture, so a shared fixture's attempt is charged to the test that first sets it up."""
    guard = item.config.stash[_GUARD]
    item.stash[_ITEM_MARK] = guard.mark()
    # Only an opted-in e2e test may reach the network; refused again at its teardown.
    if item.config.getoption("--run-e2e") and item.get_closest_marker("e2e") is not None:
        guard.allow_network()
    else:
        guard.refuse_network()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Fail at teardown, since code under test may swallow the refusal; unless an earlier phase failed on it."""
    report = yield
    if report.failed and call.excinfo is not None and _caused_by_a_refusal(call.excinfo.value):
        item.stash[_ITEM_FAILED_ON_A_REFUSAL] = True
    if call.when == "teardown":
        guard = item.config.stash[_GUARD]
        guard.refuse_network()
        # No mark means setup never reached ours; leave the attempts for pytest_sessionfinish.
        attempts = guard.claim_since(item.stash.get(_ITEM_MARK, guard.mark()))
        if attempts and not item.stash.get(_ITEM_FAILED_ON_A_REFUSAL, False):
            report.outcome = "failed"
            report.longrepr = f"test {_describe(attempts)}"
    return report


def pytest_sessionfinish(session):
    """Attempts outside every collection and test, e.g. in pytest_collection_modifyitems, fail the run."""
    attempts = session.config.stash[_GUARD].claim_since(0)
    if attempts:
        session.config.get_terminal_writer().line(f"\nthe run {_describe(attempts)}", red=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
