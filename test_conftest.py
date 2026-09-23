import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent
CONFTEST_SOURCE = (PROJECT_ROOT / "conftest.py").read_text(encoding="utf-8")


@pytest.fixture
def suite_under_real_conftest(pytester, monkeypatch):
    """A separate pytest process running the real conftest, so its guards are the only ones active."""
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [str(PROJECT_ROOT), os.environ.get("PYTHONPATH")]))
    )
    pytester.makeconftest(CONFTEST_SOURCE)
    return pytester


def test_a_swallowed_connection_still_fails_the_test_and_names_the_host(suite_under_real_conftest):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        def test_connects_and_swallows_the_error():
            try:
                with socket.socket() as sock:
                    sock.settimeout(1)
                    sock.connect(("192.0.2.1", 80))
            except OSError:
                pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*192.0.2.1*"])


def test_a_swallowed_name_lookup_still_fails_the_test_and_names_the_host(suite_under_real_conftest):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        def test_resolves_and_swallows_the_error():
            try:
                socket.getaddrinfo("archive.org", 443)
            except OSError:
                pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*archive.org*"])


SWALLOWED_REFUSALS = {
    "connect": "192.0.2.1",
    "connect_ex": "192.0.2.2",
    "sendto": "192.0.2.3",
    "getnameinfo": "192.0.2.4",
    "gethostbyaddr": "192.0.2.5",
    "getaddrinfo": "getaddrinfo.invalid",
    "gethostbyname": "gethostbyname.invalid",
    "gethostbyname_ex": "gethostbyname-ex.invalid",
}


def test_every_way_of_reaching_the_network_is_refused_and_reported(suite_under_real_conftest):
    """Lookups must refuse with gaierror/herror, as a real failure would, or the body fails instead of passing."""
    suite_under_real_conftest.makepyfile(
        """
        import errno
        import socket

        import pytest

        def connect():
            with socket.socket() as sock:
                sock.settimeout(1)
                sock.connect(("192.0.2.1", 80))

        def connect_ex():
            with socket.socket() as sock:
                sock.settimeout(1)
                try:
                    result = sock.connect_ex(("192.0.2.2", 80))
                except OSError:
                    pytest.fail("connect_ex raised; a real failure returns an errno")
                assert result == errno.ECONNREFUSED

        def sendto():
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.sendto(b"x", ("192.0.2.3", 53))

        @pytest.mark.parametrize("reach", [connect, connect_ex, sendto])
        def test_swallows_a_refused_send(reach):
            try:
                reach()
            except OSError:
                pass

        @pytest.mark.parametrize(
            "lookup",
            [
                lambda: socket.getnameinfo(("192.0.2.4", 80), 0),
                lambda: socket.getaddrinfo("getaddrinfo.invalid", 443),
                lambda: socket.gethostbyname("gethostbyname.invalid"),
                lambda: socket.gethostbyname_ex("gethostbyname-ex.invalid"),
            ],
        )
        def test_swallows_a_refused_lookup(lookup):
            try:
                lookup()
            except socket.gaierror:
                pass

        def test_swallows_a_refused_reverse_lookup():
            try:
                socket.gethostbyaddr("192.0.2.5")
            except socket.herror:
                pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=len(SWALLOWED_REFUSALS), errors=len(SWALLOWED_REFUSALS))
    output = result.stdout.str()
    assert [host for host in SWALLOWED_REFUSALS.values() if host not in output] == []


def test_a_refusal_the_test_does_not_swallow_is_reported_once(suite_under_real_conftest):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        def test_resolves_without_catching():
            socket.getaddrinfo("archive.org", 443)
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(failed=1, errors=0)


def test_a_swallowed_refusal_is_reported_when_the_test_fails_for_another_reason(suite_under_real_conftest):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        def test_swallows_then_fails_an_assertion():
            try:
                socket.getaddrinfo("unrelated-failure.invalid", 443)
            except OSError:
                pass
            assert False
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(failed=1, errors=1)
    result.stdout.fnmatch_lines(["*unrelated-failure.invalid*"])


def test_a_proxy_does_not_hide_a_request_from_the_guard(suite_under_real_conftest, monkeypatch):
    """Through a loopback proxy the only connection is local; the guard must see the real host instead."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    # This run's own guard set these; the inner run must set them itself.
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    suite_under_real_conftest.makepyfile(
        """
        import requests

        def test_requests_through_the_proxy_and_swallows_the_error():
            try:
                requests.get("https://archive.org", timeout=5)
            except requests.RequestException:
                pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*archive.org*"])


def test_a_session_scoped_fixture_is_guarded_and_charged_to_the_first_test_using_it(
    suite_under_real_conftest,
):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        import pytest

        @pytest.fixture(scope="session")
        def shared_client():
            try:
                socket.getaddrinfo("session-fixture.invalid", 443)
            except OSError:
                pass

        def test_first_user(shared_client):
            pass

        def test_second_user(shared_client):
            pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*ERROR at teardown of test_first_user*", "*session-fixture.invalid*"])


def test_reaching_the_network_while_importing_a_test_module_fails_collection(
    suite_under_real_conftest,
):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        try:
            socket.getaddrinfo("import-time.invalid", 443)
        except OSError:
            pass

        def test_never_runs():
            pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*import-time.invalid*"])


def test_a_module_that_skips_after_a_refused_probe_still_fails_collection(suite_under_real_conftest):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        import pytest

        try:
            socket.getaddrinfo("offline-probe.invalid", 443)
        except OSError:
            pytest.skip("offline", allow_module_level=True)

        def test_never_runs():
            pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*offline-probe.invalid*"])


def test_an_attempt_outside_every_test_fails_the_run(suite_under_real_conftest):
    """A hook in a directory conftest runs after collection and before any test; no test window claims it."""
    hooks_dir = suite_under_real_conftest.mkpydir("hooks")
    (hooks_dir / "conftest.py").write_text(
        "import socket\n"
        "\n"
        "def pytest_collection_modifyitems(items):\n"
        "    try:\n"
        '        socket.getaddrinfo("modify-items.invalid", 443)\n'
        "    except OSError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    (hooks_dir / "test_passes.py").write_text("def test_passes():\n    pass\n", encoding="utf-8")
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*modify-items.invalid*"])


LOCAL_HOSTS = [
    "localhost",
    "LOCALHOST",
    "localhost.",
    b"localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "::",
]


def test_local_names_and_addresses_are_allowed(suite_under_real_conftest):
    """Swallows the OS's own resolution errors; a refusal would still be recorded and fail at teardown."""
    suite_under_real_conftest.makepyfile(
        f"""
        import socket

        import pytest

        @pytest.mark.parametrize("host", {LOCAL_HOSTS!r})
        def test_resolves_a_local_host(host):
            try:
                socket.getaddrinfo(host, 80)
            except socket.gaierror:
                pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=len(LOCAL_HOSTS))


def test_loopback_connections_are_allowed(suite_under_real_conftest):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        import pytest

        def test_connects_to_a_local_server():
            with socket.create_server(("127.0.0.1", 0)) as server:
                with socket.create_connection(server.getsockname(), timeout=5):
                    pass

        def test_connect_ex_to_a_local_server():
            with socket.create_server(("127.0.0.1", 0)) as server:
                with socket.socket() as sock:
                    assert sock.connect_ex(server.getsockname()) == 0

        @pytest.mark.skipif(not socket.has_ipv6, reason="no IPv6 on this machine")
        def test_connects_to_an_ipv6_local_server():
            with socket.create_server(("::1", 0), family=socket.AF_INET6) as server:
                with socket.create_connection(server.getsockname()[:2], timeout=5):
                    pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=3)


IA_CREDENTIALS_ARE_ABSENT = """
    from internetarchive.config import get_config

    def test_sees_no_ia_credentials():
        assert get_config().get("s3", {}).get("access") is None
    """


def test_no_test_reads_an_ia_config_file_on_the_developers_machine(
    suite_under_real_conftest, monkeypatch, tmp_path
):
    """internetarchive falls back to the XDG config when IA_CONFIG_FILE names a missing file."""
    planted_config = tmp_path / "xdg" / "internetarchive" / "ia.ini"
    planted_config.parent.mkdir(parents=True)
    planted_config.write_text(
        "[s3]\naccess = planted-access\nsecret = planted-secret\n", encoding="utf-8"
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("IA_CONFIG_FILE", raising=False)
    suite_under_real_conftest.makepyfile(IA_CREDENTIALS_ARE_ABSENT)
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=1)


def test_no_test_reads_ia_credentials_from_the_environment(suite_under_real_conftest, monkeypatch):
    monkeypatch.setenv("IA_ACCESS_KEY_ID", "planted-access")
    monkeypatch.setenv("IA_SECRET_ACCESS_KEY", "planted-secret")
    suite_under_real_conftest.makepyfile(IA_CREDENTIALS_ARE_ABSENT)
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=1)
