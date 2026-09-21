import pytest

import platform_probe


LOADED_OUTPUT = """\
gui/501/org.lcpsociety.iabulk.sync = {
	active count = 0
	state = waiting
	pid = 4821
	last exit code = 0
}
"""

NEVER_RAN_OUTPUT = """\
gui/501/org.lcpsociety.iabulk.sync = {
	active count = 0
	state = waiting
	last exit code = (never exited)
}
"""


def test_parse_last_exit_reads_the_code():
    assert platform_probe.parse_last_exit(LOADED_OUTPUT) == 0


def test_parse_last_exit_is_none_when_the_agent_has_never_run():
    assert platform_probe.parse_last_exit(NEVER_RAN_OUTPUT) is None


def test_parse_last_exit_is_none_for_output_it_does_not_recognize():
    assert platform_probe.parse_last_exit("nothing useful here") is None


def test_parse_pid_reads_a_running_agent():
    assert platform_probe.parse_pid(LOADED_OUTPUT) == 4821


def test_parse_pid_is_none_when_not_running():
    assert platform_probe.parse_pid(NEVER_RAN_OUTPUT) is None


def test_file_mode_is_none_for_a_missing_file(tmp_path):
    assert platform_probe.file_mode(tmp_path / "nope") is None


def test_file_mode_returns_permission_bits_only(tmp_path):
    target = tmp_path / "key.json"
    target.write_text("{}", encoding="utf-8")
    mode = platform_probe.file_mode(target)
    assert mode is not None
    assert mode == mode & 0o777


def test_is_readable_directory_is_false_for_a_missing_path(tmp_path):
    assert platform_probe.is_readable_directory(tmp_path / "lacie") is False


def test_is_readable_directory_is_false_for_a_file(tmp_path):
    target = tmp_path / "a-file"
    target.write_text("x", encoding="utf-8")
    assert platform_probe.is_readable_directory(target) is False


def test_is_readable_directory_is_true_for_a_real_directory(tmp_path):
    assert platform_probe.is_readable_directory(tmp_path) is True


def test_has_posix_permissions_is_true_on_posix(monkeypatch):
    monkeypatch.setattr(platform_probe.os, "name", "posix")
    assert platform_probe.has_posix_permissions() is True


def test_has_posix_permissions_is_false_elsewhere(monkeypatch):
    monkeypatch.setattr(platform_probe.os, "name", "nt")
    assert platform_probe.has_posix_permissions() is False


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _with_uid(monkeypatch, uid=501):
    monkeypatch.setattr(platform_probe.os, "getuid", lambda: uid, raising=False)


def test_launchctl_bootstrap_reports_success_structurally(tmp_path, monkeypatch):
    _with_uid(monkeypatch)
    monkeypatch.setattr(platform_probe, "_launchctl", lambda *a: _FakeCompleted())
    loaded, message = platform_probe.launchctl_bootstrap(tmp_path / "a.plist")
    assert loaded is True
    assert "a.plist" in message


def test_launchctl_bootstrap_reports_failure_structurally(tmp_path, monkeypatch):
    """A failed bootstrap used to be a string the caller printed and ignored, so
    the one command whose job is loading the agent could not fail."""
    _with_uid(monkeypatch)
    monkeypatch.setattr(
        platform_probe, "_launchctl", lambda *a: _FakeCompleted(returncode=5, stderr="Bootstrap failed: 5")
    )
    loaded, message = platform_probe.launchctl_bootstrap(tmp_path / "a.plist")
    assert loaded is False
    assert "Bootstrap failed: 5" in message


def test_launchctl_bootout_reports_success_structurally(monkeypatch):
    _with_uid(monkeypatch)
    monkeypatch.setattr(platform_probe, "_launchctl", lambda *a: _FakeCompleted())
    unloaded, message = platform_probe.launchctl_bootout("org.example.job")
    assert unloaded is True
    assert "org.example.job" in message


def test_launchctl_bootout_reports_failure_structurally(monkeypatch):
    _with_uid(monkeypatch)
    monkeypatch.setattr(
        platform_probe, "_launchctl", lambda *a: _FakeCompleted(returncode=3, stderr="No such process")
    )
    unloaded, message = platform_probe.launchctl_bootout("org.example.job")
    assert unloaded is False
    assert "No such process" in message


def test_bootstrap_and_bootout_refuse_where_there_is_no_getuid(tmp_path, monkeypatch):
    """launchctl_print already guarded os.getuid(); these two did not, so
    `setup --enable-agent` raised AttributeError off macOS instead of refusing."""
    monkeypatch.delattr(platform_probe.os, "getuid", raising=False)
    monkeypatch.setattr(
        platform_probe, "_launchctl", lambda *a: pytest.fail("ran launchctl without a uid")
    )
    assert platform_probe.launchctl_bootstrap(tmp_path / "a.plist") == (False, platform_probe.NO_LAUNCHCTL)
    assert platform_probe.launchctl_bootout("org.example.job") == (False, platform_probe.NO_LAUNCHCTL)
    assert platform_probe.launchctl_print("org.example.job") is None
