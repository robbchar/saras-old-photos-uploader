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


def _is_loopback(host: object) -> bool:
    if host in (None, "", b""):
        return True
    host_text = host.decode() if isinstance(host, bytes) else str(host)
    if host_text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host_text.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _no_test_reaches_the_network(monkeypatch):
    """Refuse and record non-loopback attempts; fail at teardown, since code under test may swallow the refusal."""
    attempts: list[str] = []
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def refuse_unless_loopback(action: str, host: object) -> None:
        if not _is_loopback(host):
            attempts.append(f"{action} {host!r}")
            raise OSError(f"test tried to {action} {host!r}; the suite must not reach the network")

    def guarded_getaddrinfo(host, *args, **kwargs):
        refuse_unless_loopback("resolve", host)
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(sock, address):
        if sock.family != getattr(socket, "AF_UNIX", None):
            refuse_unless_loopback("connect to", address[0])
        return real_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family != getattr(socket, "AF_UNIX", None):
            refuse_unless_loopback("connect to", address[0])
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
    if attempts:
        pytest.fail("test tried to reach the network: " + ", ".join(attempts), pytrace=False)
