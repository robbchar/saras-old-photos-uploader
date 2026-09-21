import launch_agent

import deployment
from deployment import Check, CheckOutcome, Status


def passing(name="ok"):
    return Check(name=name, probe=lambda: CheckOutcome(Status.PASS, "fine"), remedy="none")


def failing(name="bad", fix=None):
    return Check(
        name=name,
        probe=lambda: CheckOutcome(Status.FAIL, "broken"),
        remedy="run the thing",
        fix=fix,
    )


def test_run_checks_returns_one_outcome_per_check_in_order():
    checks = [passing("first"), failing("second")]
    results = deployment.run_checks(checks)
    assert [check.name for check, _ in results] == ["first", "second"]
    assert [outcome.status for _, outcome in results] == [Status.PASS, Status.FAIL]


def test_run_checks_reports_a_raising_probe_as_unknown_not_fail():
    def explode():
        raise OSError("no idea")

    check = Check(name="flaky", probe=explode, remedy="try later")
    (_, outcome), = deployment.run_checks([check])
    assert outcome.status is Status.UNKNOWN
    assert "no idea" in outcome.detail


def test_format_report_prefixes_each_line_with_its_status():
    report = deployment.format_report(deployment.run_checks([passing("first"), failing("second")]))
    assert "[PASS] first" in report
    assert "[FAIL] second" in report


def test_format_report_prints_the_remedy_only_under_a_failure():
    report = deployment.format_report(deployment.run_checks([passing("first"), failing("second")]))
    assert "run the thing" in report
    assert report.index("run the thing") > report.index("[FAIL] second")
    assert report.count("run the thing") == 1


def test_exit_code_is_zero_when_nothing_failed():
    assert deployment.exit_code(deployment.run_checks([passing()])) == 0


def test_exit_code_is_zero_when_a_check_is_only_unknown():
    check = Check(name="offline", probe=lambda: CheckOutcome(Status.UNKNOWN, "no network"), remedy="x")
    assert deployment.exit_code(deployment.run_checks([check])) == 0


def test_exit_code_is_one_when_any_check_failed():
    assert deployment.exit_code(deployment.run_checks([passing(), failing()])) == 1


def test_converge_applies_fix_then_rechecks():
    state = {"broken": True}

    def probe():
        return CheckOutcome(Status.PASS, "fine") if not state["broken"] else CheckOutcome(Status.FAIL, "broken")

    def fix():
        state["broken"] = False
        return "unbroke it"

    check = Check(name="fixable", probe=probe, remedy="x", fix=fix)
    (_, outcome), = deployment.converge([check], announce=lambda _: None)
    assert outcome.status is Status.PASS


def test_converge_announces_before_it_acts():
    announced = []
    state = {"broken": True}

    def fix():
        assert announced, "fix ran before anything was announced"
        state["broken"] = False
        return "unbroke it"

    check = Check(
        name="fixable",
        probe=lambda: CheckOutcome(Status.PASS, "fine") if not state["broken"] else CheckOutcome(Status.FAIL, "broken"),
        remedy="x",
        fix=fix,
    )
    deployment.converge([check], announce=announced.append)
    assert any("fixable" in line for line in announced)


def test_converge_leaves_a_passing_check_alone_and_stays_quiet():
    announced = []
    deployment.converge([passing()], announce=announced.append)
    assert announced == []


def test_converge_does_not_call_fix_for_an_unknown_check():
    called = []
    check = Check(
        name="offline",
        probe=lambda: CheckOutcome(Status.UNKNOWN, "no network"),
        remedy="x",
        fix=lambda: called.append("fixed") or "fixed",
    )
    deployment.converge([check], announce=lambda _: None)
    assert called == []


def test_minimum_python_is_three_ten():
    assert deployment.MINIMUM_PYTHON == (3, 10)


from pathlib import Path

import project_config


def a_config(sheet_id="1realsheetid", test_sheet_id="1testsheetid"):
    """Mirrors project_config.ProjectConfig's required fields - read project_config.py:63-86
    and match it exactly; that dataclass is the source of truth, not this plan."""
    return project_config.ProjectConfig(
        project_id="demo",
        collection_key="lcps",
        mediatype="image",
        ia_collection="demo",
        sheet_id=sheet_id,
        test_sheet_id=test_sheet_id,
        sheet_tab="Sheet1",
        files_dir="./data",
        file_template="{file_name}",
        required_for_upload=("title",),
        photo_extensions=(".jpg",),
        batch_column=None,
    )


def test_key_present_check_fails_when_the_key_is_absent(tmp_path):
    outcome = deployment.key_present_check(tmp_path / "google-service-account.json").probe()
    assert outcome.status is Status.FAIL


def test_key_present_check_passes_when_the_key_is_there(tmp_path):
    key = tmp_path / "google-service-account.json"
    key.write_text('{"client_email": "x@y.iam.gserviceaccount.com"}', encoding="utf-8")
    assert deployment.key_present_check(key).probe().status is Status.PASS


def test_key_present_check_has_no_fix_because_a_key_cannot_be_conjured(tmp_path):
    assert deployment.key_present_check(tmp_path / "k.json").fix is None


def test_key_mode_check_is_unknown_when_the_key_is_absent(tmp_path):
    assert deployment.key_mode_check(tmp_path / "k.json").probe().status is Status.UNKNOWN


def test_key_mode_check_reports_the_owner_so_a_handover_mismatch_is_visible(tmp_path, monkeypatch):
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: True)
    monkeypatch.setattr(deployment.platform_probe, "file_owner", lambda _: "shared")
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o600)
    assert "shared" in deployment.key_mode_check(key).probe().detail


def test_key_mode_check_passes_on_0o600_when_the_platform_has_posix_permissions(tmp_path, monkeypatch):
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: True)
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o600)
    assert deployment.key_mode_check(key).probe().status is Status.PASS


def test_key_mode_check_fails_on_a_group_readable_key(tmp_path, monkeypatch):
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: True)
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o644)
    assert deployment.key_mode_check(key).probe().status is Status.FAIL


def test_key_mode_check_is_unknown_when_the_platform_lacks_posix_permissions(tmp_path, monkeypatch):
    """Windows os.stat reports 0o666 for every file, so a mode comparison there
    would be a meaningless FAIL rather than an honest "can't tell"."""
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: False)
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o666)
    outcome = deployment.key_mode_check(key).probe()
    assert outcome.status is Status.UNKNOWN
    assert "posix" in outcome.detail.lower()


def test_key_mode_check_fix_chmods_to_600(tmp_path, monkeypatch):
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    chmodded = []
    monkeypatch.setattr(deployment.platform_probe, "set_file_mode", lambda p, m: chmodded.append((p, m)))
    deployment.key_mode_check(key).fix()
    assert chmodded == [(key, 0o600)]


def test_sheet_id_check_fails_on_the_placeholder():
    config = a_config(sheet_id="REPLACE_WITH_REAL_SHEET_ID")
    check = deployment.sheet_id_check(config, live=True, registry_path="projects_registry.json")
    assert check.probe().status is Status.FAIL
    assert "projects_registry.json" in check.remedy


def test_sheet_id_check_passes_on_a_real_id():
    outcome = deployment.sheet_id_check(a_config(), live=True, registry_path="r.json").probe()
    assert outcome.status is Status.PASS


def test_sheet_id_check_looks_at_the_mode_it_was_given():
    config = a_config(sheet_id="1real", test_sheet_id="REPLACE_WITH_TEST_SHEET_ID")
    assert deployment.sheet_id_check(config, live=True, registry_path="r.json").probe().status is Status.PASS
    assert deployment.sheet_id_check(config, live=False, registry_path="r.json").probe().status is Status.FAIL


def test_drive_check_is_unknown_when_the_directory_is_absent(tmp_path):
    outcome = deployment.drive_check(tmp_path / "LaCie").probe()
    assert outcome.status is Status.UNKNOWN


def test_drive_check_passes_for_a_readable_directory(tmp_path):
    assert deployment.drive_check(tmp_path).probe().status is Status.PASS


def test_python_version_check_fails_below_the_floor():
    assert deployment.python_version_check((3, 9)).probe().status is Status.FAIL


def test_python_version_check_passes_at_the_floor():
    assert deployment.python_version_check((3, 10)).probe().status is Status.PASS


def test_dependencies_check_passes_in_this_environment():
    assert deployment.dependencies_check().probe().status is Status.PASS


import json

import google_auth
import sync_state
from googleapiclient.errors import HttpError


def grid_with(*headers):
    return lambda: [list(headers), ["a-value"] * len(headers)]


def make_http_error(message="The caller does not have permission", status=403):
    """A realistic HttpError, as googleapiclient actually raises it: the real .resp
    needs a .status and .reason, and .content must be the raw JSON error body Google's
    API returns, not a plain string. Mirrors test_ia_bulk.py's make_http_error."""

    class _FakeHttpResponse:
        def __init__(self, status, reason):
            self.status = status
            self.reason = reason

    content = json.dumps({"error": {"message": message}}).encode("utf-8")
    return HttpError(
        _FakeHttpResponse(status, reason="Forbidden"),
        content,
        uri="https://sheets.googleapis.com/v4/spreadsheets/TEST_SHEET_ID/values/Sheet1",
    )


def test_sheet_reachable_check_passes_when_the_grid_comes_back():
    outcome = deployment.sheet_reachable_check(grid_with("identifier", "title"), "sa@x.com").probe()
    assert outcome.status is Status.PASS


def test_sheet_reachable_check_is_unknown_when_the_network_is_down():
    def offline():
        raise google_auth.AuthUnavailable("could not reach Google to authenticate")

    outcome = deployment.sheet_reachable_check(offline, "sa@x.com").probe()
    assert outcome.status is Status.UNKNOWN


def test_sheet_reachable_check_fails_when_the_sheet_is_not_shared():
    def forbidden():
        raise make_http_error(status=403)

    outcome = deployment.sheet_reachable_check(forbidden, "sa@x.com").probe()
    assert outcome.status is Status.FAIL


def test_sheet_reachable_check_fails_on_a_wrong_sheet_id():
    def not_found():
        raise make_http_error(message="Requested entity was not found.", status=404)

    outcome = deployment.sheet_reachable_check(not_found, "sa@x.com").probe()
    assert outcome.status is Status.FAIL


def test_sheet_reachable_check_is_unknown_on_a_transient_google_error():
    def flaky():
        raise make_http_error(message="Internal error encountered.", status=500)

    outcome = deployment.sheet_reachable_check(flaky, "sa@x.com").probe()
    assert outcome.status is Status.UNKNOWN


def test_sheet_reachable_check_names_the_address_to_share_with():
    def forbidden():
        raise make_http_error(status=403)

    assert "sa@x.com" in deployment.sheet_reachable_check(forbidden, "sa@x.com").remedy


def test_sync_columns_check_passes_when_both_columns_are_present():
    probe = grid_with("identifier", "title", *sync_state.SYNC_STATE_COLUMNS)
    assert deployment.sync_columns_check(probe).probe().status is Status.PASS


def test_sync_columns_check_fails_and_names_the_missing_column():
    probe = grid_with("identifier", "title", sync_state.IA_SYNC_HASH_COLUMN)
    outcome = deployment.sync_columns_check(probe).probe()
    assert outcome.status is Status.FAIL
    assert sync_state.IA_LAST_SYNCED_COLUMN in outcome.detail


def test_sync_columns_check_is_unknown_when_the_sheet_cannot_be_read():
    def offline():
        raise google_auth.AuthUnavailable("no network")

    assert deployment.sync_columns_check(offline).probe().status is Status.UNKNOWN


def test_sync_columns_check_fails_on_a_wrong_sheet_id():
    def not_found():
        raise make_http_error(message="Requested entity was not found.", status=404)

    assert deployment.sync_columns_check(not_found).probe().status is Status.FAIL


def test_agent_plist_check_fails_when_the_plist_is_stale(tmp_path):
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo")
    assert deployment.agent_plist_check(spec, tmp_path / "home").probe().status is Status.FAIL


def test_agent_plist_check_fix_writes_the_plist(tmp_path):
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo")
    home = tmp_path / "home"
    deployment.agent_plist_check(spec, home).fix()
    assert launch_agent.plist_is_current(spec, home) is True


def test_agent_loaded_check_is_unknown_when_launchctl_says_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment.platform_probe, "launchctl_print", lambda _: None)
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo")
    assert deployment.agent_loaded_check(spec).probe().status is Status.UNKNOWN


def test_agent_loaded_check_passes_and_reports_the_last_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deployment.platform_probe, "launchctl_print", lambda _: "\tlast exit code = 0\n"
    )
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo")
    outcome = deployment.agent_loaded_check(spec).probe()
    assert outcome.status is Status.PASS
    assert "0" in outcome.detail


def test_agent_loaded_check_fails_when_the_last_run_errored(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deployment.platform_probe, "launchctl_print", lambda _: "\tlast exit code = 1\n"
    )
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo")
    assert deployment.agent_loaded_check(spec).probe().status is Status.FAIL


def test_agent_loaded_check_has_no_fix_so_setup_never_loads_it_implicitly(tmp_path):
    # Loading is gated on --enable-agent, which setup does explicitly.
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo")
    assert deployment.agent_loaded_check(spec).fix is None


import re


def test_install_sh_python_floor_matches_the_one_python_enforces():
    script = Path("install.sh").read_text(encoding="utf-8")
    major = re.search(r"^MIN_PY_MAJOR=(\d+)$", script, re.MULTILINE)
    minor = re.search(r"^MIN_PY_MINOR=(\d+)$", script, re.MULTILINE)
    assert major and minor, "install.sh must declare MIN_PY_MAJOR and MIN_PY_MINOR"
    assert (int(major.group(1)), int(minor.group(1))) == deployment.MINIMUM_PYTHON


def test_install_sh_hands_off_to_setup_not_to_a_second_check_list():
    assert "ia_bulk.py setup" in Path("install.sh").read_text(encoding="utf-8")


def test_install_sh_does_not_install_an_interpreter():
    # The refusal message may *name* brew inside an echo; what must not exist is a
    # line that runs it.
    script = Path("install.sh").read_text(encoding="utf-8")
    assert not re.search(r"^\s*brew\s+install", script, re.MULTILINE)
