import pytest

import app_version


def test_app_version_is_plain_semver():
    assert app_version.SEMVER_PATTERN.fullmatch(app_version.APP_VERSION)


def test_read_installed_version_is_none_without_a_marker(tmp_path):
    assert app_version.read_installed_version(tmp_path / "installed-version") is None


def test_read_installed_version_ignores_surrounding_whitespace(tmp_path):
    marker_path = tmp_path / "installed-version"
    marker_path.write_text("  1.2.3\n", encoding="utf-8")
    assert app_version.read_installed_version(marker_path) == "1.2.3"


def test_read_installed_version_is_none_for_a_garbled_marker(tmp_path):
    marker_path = tmp_path / "installed-version"
    marker_path.write_text("not a version", encoding="utf-8")
    assert app_version.read_installed_version(marker_path) is None


def test_read_installed_version_is_none_for_undecodable_bytes(tmp_path):
    marker_path = tmp_path / "installed-version"
    marker_path.write_bytes(b"\xff\xfe\x00")
    assert app_version.read_installed_version(marker_path) is None


def test_record_installed_version_round_trips_and_creates_the_folder(tmp_path):
    marker_path = tmp_path / "logs" / "installed-version"
    app_version.record_installed_version(marker_path)
    assert app_version.read_installed_version(marker_path) == app_version.APP_VERSION


def test_record_installed_version_writes_lf_only(tmp_path):
    marker_path = tmp_path / "installed-version"
    app_version.record_installed_version(marker_path)
    assert marker_path.read_bytes() == f"{app_version.APP_VERSION}\n".encode("ascii")


def test_update_line_is_none_on_a_first_setup():
    assert app_version.update_line(None) is None


def test_update_line_is_none_when_the_version_is_unchanged():
    assert app_version.update_line(app_version.APP_VERSION) is None


def test_update_line_names_both_versions_when_they_differ():
    assert app_version.update_line("0.9.0") == f"updating from 0.9.0 to {app_version.APP_VERSION}"


def test_update_line_calls_a_rollback_a_downgrade():
    assert app_version.update_line("99.0.0") == f"downgrading from 99.0.0 to {app_version.APP_VERSION}"


def test_update_line_compares_versions_numerically(monkeypatch):
    monkeypatch.setattr(app_version, "APP_VERSION", "1.10.0")
    assert app_version.update_line("1.9.0") == "updating from 1.9.0 to 1.10.0"


def test_app_version_comes_from_the_version_file():
    assert app_version.APP_VERSION == app_version.VERSION_FILE_PATH.read_text(encoding="utf-8").strip()


def test_read_version_file_ignores_surrounding_whitespace(tmp_path):
    version_path = tmp_path / "version.txt"
    version_path.write_text("  1.2.3\n", encoding="utf-8")
    assert app_version.read_version_file(version_path) == "1.2.3"


def test_read_version_file_names_the_file_when_it_is_missing(tmp_path):
    with pytest.raises(RuntimeError, match=r"version\.txt"):
        app_version.read_version_file(tmp_path / "version.txt")


def test_read_version_file_names_the_file_when_it_is_not_plain_semver(tmp_path):
    version_path = tmp_path / "version.txt"
    version_path.write_text("v1.2.3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"version\.txt"):
        app_version.read_version_file(version_path)


def test_read_version_file_names_the_file_when_it_is_undecodable(tmp_path):
    version_path = tmp_path / "version.txt"
    version_path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(RuntimeError, match=r"version\.txt"):
        app_version.read_version_file(version_path)
