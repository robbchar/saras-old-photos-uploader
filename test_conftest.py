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


def test_loopback_connections_are_allowed(suite_under_real_conftest):
    suite_under_real_conftest.makepyfile(
        """
        import socket

        def test_connects_to_a_local_server():
            socket.getaddrinfo("localhost", 80)
            with socket.create_server(("127.0.0.1", 0)) as server:
                with socket.create_connection(server.getsockname(), timeout=5):
                    pass
        """
    )
    result = suite_under_real_conftest.runpytest_subprocess()
    result.assert_outcomes(passed=1)


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
