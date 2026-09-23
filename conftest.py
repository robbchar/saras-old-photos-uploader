import ipaddress
import socket

import pytest

import google_auth

pytest_plugins = ["pytester"]


@pytest.fixture(scope="session")
def _missing_service_account_key_path(tmp_path_factory):
    return tmp_path_factory.mktemp("no-real-key") / "google-service-account.json"


@pytest.fixture(autouse=True)
def _no_test_reads_the_real_service_account_key(monkeypatch, _missing_service_account_key_path):
    """No test should ever reach the real key; point the default at a path that does not exist."""
    monkeypatch.setattr(
        google_auth, "DEFAULT_SERVICE_ACCOUNT_KEY_PATH", _missing_service_account_key_path
    )


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


def _is_local(host: object) -> bool:
    """None or "" means this machine, as do localhost names (RFC 6761), loopback and unspecified addresses."""
    if host in (None, "", b""):
        return True
    host_text = host.decode() if isinstance(host, bytes) else str(host)
    name = host_text.lower().rstrip(".")
    if name == "localhost" or name.endswith(".localhost"):
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
_GUARDED_SOCKET_METHODS = {
    "connect": lambda address: _host_of(address),
    "connect_ex": lambda address: _host_of(address),
    "sendto": lambda _data, *flags_and_address: _host_of(flags_and_address[-1]),
}
_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


class _NetworkGuard:
    """Refuses non-local lookups and connections, and records each so a refusal the code swallows is still reported."""

    def __init__(self) -> None:
        self._attempts: list[str] = []
        self._patches = pytest.MonkeyPatch()

    def install(self) -> None:
        for name, (host_of, error) in _GUARDED_LOOKUPS.items():
            self._patches.setattr(socket, name, self._guarded_lookup(getattr(socket, name), host_of, error))
        for name, host_of in _GUARDED_SOCKET_METHODS.items():
            real_method = getattr(socket.socket, name)
            self._patches.setattr(socket.socket, name, self._guarded_socket_method(real_method, host_of))
        # A loopback proxy would be the only connection the guard sees; "*" also overrides a Windows registry proxy.
        for variable in _PROXY_VARIABLES:
            self._patches.delenv(variable, raising=False)
        self._patches.setenv("NO_PROXY", "*")
        self._patches.setenv("no_proxy", "*")

    def uninstall(self) -> None:
        self._patches.undo()

    def mark(self) -> int:
        return len(self._attempts)

    def claim_since(self, mark: int) -> list[str]:
        """Attempts since `mark`, removed so an enclosing window does not report them again."""
        claimed = self._attempts[mark:]
        del self._attempts[mark:]
        return claimed

    def _refuse_unless_local(self, action: str, host: object, error: type[OSError]) -> None:
        if not _is_local(host):
            self._attempts.append(f"{action} {host!r}")
            raise error(f"test tried to {action} {host!r}; the suite must not reach the network")

    def _guarded_lookup(self, real_lookup, host_of, error):
        def guarded(*args, **kwargs):
            self._refuse_unless_local("look up", host_of(*args, **kwargs), error)
            return real_lookup(*args, **kwargs)

        return guarded

    def _guarded_socket_method(self, real_method, host_of):
        def guarded(sock, *args, **kwargs):
            self._refuse_unless_local("reach", host_of(*args, **kwargs), OSError)
            return real_method(sock, *args, **kwargs)

        return guarded


def _describe(attempts: list[str]) -> str:
    return "tried to reach the network: " + ", ".join(attempts)


_GUARD = pytest.StashKey[_NetworkGuard]()
_ITEM_MARK = pytest.StashKey[int]()
_ITEM_ALREADY_FAILED = pytest.StashKey[bool]()


def pytest_configure(config):
    """Installed for the whole run, so collection and session/module fixtures are guarded too."""
    guard = _NetworkGuard()
    guard.install()
    config.stash[_GUARD] = guard


def pytest_unconfigure(config):
    guard = config.stash.get(_GUARD, None)
    if guard is not None:
        guard.uninstall()


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    guard = collector.config.stash[_GUARD]
    mark = guard.mark()
    report = yield
    attempts = guard.claim_since(mark)
    if attempts and report.passed:
        report.outcome = "failed"
        report.longrepr = f"collecting {collector.nodeid or 'the session'} {_describe(attempts)}"
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Before any fixture, so a shared fixture's attempt is charged to the test that first sets it up."""
    item.stash[_ITEM_MARK] = item.config.stash[_GUARD].mark()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Fail at teardown, since code under test may swallow the refusal; unless an earlier phase already failed."""
    report = yield
    if report.failed:
        item.stash[_ITEM_ALREADY_FAILED] = True
    if call.when == "teardown":
        attempts = item.config.stash[_GUARD].claim_since(item.stash[_ITEM_MARK])
        if attempts and not item.stash.get(_ITEM_ALREADY_FAILED, False):
            report.outcome = "failed"
            report.longrepr = f"test {_describe(attempts)}"
    return report
