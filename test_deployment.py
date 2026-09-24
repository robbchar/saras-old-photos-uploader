import json
import re
import shlex
from pathlib import Path

import pytest
from googleapiclient.errors import HttpError

import deployment
import google_auth
import launch_agent
import project_config
import sync_state
from deployment import Check, CheckOutcome, Status

DEMO_INSTALL = deployment.InstallCommand("demo")


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


def test_key_mode_check_reports_the_owner_so_a_wrong_account_is_visible(tmp_path, monkeypatch):
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


def test_key_mode_check_passes_on_the_stricter_0o400(tmp_path, monkeypatch):
    """A FAIL here would have setup's fix() chmod it to 0600, adding owner write."""
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: True)
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o400)
    assert deployment.key_mode_check(key).probe().status is Status.PASS


def test_key_mode_check_fails_on_a_key_its_owner_cannot_read(tmp_path, monkeypatch):
    key = tmp_path / "k.json"
    key.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: True)
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o200)
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
    fix = deployment.key_mode_check(key).fix
    assert fix is not None
    fix()
    assert chmodded == [(key, 0o600)]


def test_sheet_id_check_fails_on_the_placeholder():
    config = a_config(sheet_id="REPLACE_WITH_REAL_SHEET_ID")
    check = deployment.sheet_id_check(config, live=True, registry_path="projects_registry.json")
    outcome = check.probe()
    assert outcome.status is Status.FAIL
    assert "REPLACE_WITH_REAL_SHEET_ID" in outcome.detail
    assert "projects_registry.json" in check.remedy


def test_sheet_id_check_passes_on_a_real_id():
    outcome = deployment.sheet_id_check(a_config(), live=True, registry_path="r.json").probe()
    assert outcome.status is Status.PASS


def test_sheet_id_check_looks_at_the_mode_it_was_given():
    config = a_config(sheet_id="1real", test_sheet_id="REPLACE_WITH_TEST_SHEET_ID")
    assert deployment.sheet_id_check(config, live=True, registry_path="r.json").probe().status is Status.PASS
    assert deployment.sheet_id_check(config, live=False, registry_path="r.json").probe().status is Status.FAIL


def test_drive_check_is_unknown_when_the_directory_is_absent(tmp_path):
    outcome = deployment.drive_check(tmp_path / "unplugged").probe()
    assert outcome.status is Status.UNKNOWN


def test_drive_check_passes_for_a_readable_directory(tmp_path):
    assert deployment.drive_check(tmp_path).probe().status is Status.PASS


def test_drive_check_is_not_needed_by_the_agent_because_sync_metadata_never_reads_it(tmp_path):
    assert deployment.drive_check(tmp_path).needed_by_agent is False


def test_drive_check_names_no_particular_project_or_device():
    # A fixed path: tmp_path embeds the username, and the Mac's account is `sarasoldphotos`.
    files_dir = Path("/Volumes/drive/files")
    check = deployment.drive_check(files_dir)
    for project_specific in ("photo", "LaCie"):
        assert project_specific not in check.name
        assert project_specific not in check.remedy
    assert str(files_dir) in check.remedy


def test_format_report_prefers_an_outcomes_own_remedy():
    check = failing("bad")
    report = deployment.format_report([(check, CheckOutcome(Status.FAIL, "broken", "do this instead"))])
    assert "fix: do this instead" in report
    assert "run the thing" not in report


def test_agent_blocking_failures_names_only_fails_the_agent_needs():
    results = [
        (failing("key"), CheckOutcome(Status.FAIL, "missing")),
        (
            Check(name="drive", probe=lambda: CheckOutcome(Status.FAIL, "x"), remedy="r", needed_by_agent=False),
            CheckOutcome(Status.FAIL, "unreadable"),
        ),
        (passing("sheet"), CheckOutcome(Status.UNKNOWN, "offline")),
    ]
    assert deployment.agent_blocking_failures(results) == ["key"]


def test_python_version_check_fails_below_the_floor():
    assert deployment.python_version_check((3, 9), DEMO_INSTALL).probe().status is Status.FAIL


def test_python_version_check_passes_at_the_floor():
    assert deployment.python_version_check((3, 10), DEMO_INSTALL).probe().status is Status.PASS


def test_python_version_check_remedy_names_the_real_project():
    assert "./install.sh --project demo " in deployment.python_version_check((3, 9), DEMO_INSTALL).remedy


def test_dependencies_check_passes_when_imported_and_installed_as_pinned():
    """Importing deployment imports all three, so only the pin comparison can fail here."""
    assert deployment.dependencies_check(DEMO_INSTALL).probe().status is Status.PASS


def test_dependencies_check_fails_when_the_installed_version_is_not_the_pin(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("internetarchive==0.0.1\n")

    outcome = deployment.dependencies_check(DEMO_INSTALL, requirements).probe()

    assert outcome.status is Status.FAIL
    assert "requirements.txt pins 0.0.1" in outcome.detail


def test_dependencies_check_fails_when_the_pin_is_only_a_floor(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("internetarchive>=5.0\n")

    outcome = deployment.dependencies_check(DEMO_INSTALL, requirements).probe()

    assert outcome.status is Status.FAIL
    assert "does not pin internetarchive" in outcome.detail


@pytest.mark.parametrize(
    "line",
    [
        "internetarchive==5.11.1",
        "internetarchive==5.11.1  # note",
        "internetarchive==5.11.1   ",
        "internetarchive[all]==5.11.1",
        "internetarchive == 5.11.1",
        "internetarchive==5.11.1; python_version >= '3.10'",
    ],
)
def test_pinned_version_reads_valid_pip_pin_syntax(line):
    assert deployment.pinned_version(f"urllib3>=2.0\n{line}\npytest>=8.0\n", "internetarchive") == "5.11.1"


@pytest.mark.parametrize(
    "requirements",
    ["internetarchive>=5.0\n", "internetarchive-extras==1.0\n", "# internetarchive==5.11.1\n", ""],
)
def test_pinned_version_is_none_without_an_exact_pin_of_that_package(requirements):
    assert deployment.pinned_version(requirements, "internetarchive") is None


def test_dependencies_check_remedy_names_the_real_project():
    assert deployment.dependencies_check(DEMO_INSTALL).remedy.startswith("./install.sh --project demo ")


def test_install_command_names_the_project():
    assert deployment.InstallCommand("sarasoldphotos").render() == "./install.sh --project sarasoldphotos"


def test_install_command_for_the_agent_adds_live_and_enable_agent():
    assert (
        deployment.InstallCommand("sarasoldphotos").render(enable_agent=True)
        == "./install.sh --project sarasoldphotos --live --enable-agent"
    )


def test_install_command_quotes_a_project_id_the_shell_would_split():
    # --project is typed by a person; the refusal that echoes it runs before the registry is read.
    assert deployment.InstallCommand("two words").render() == "./install.sh --project 'two words'"


def test_install_command_repeats_a_non_default_registry():
    """The plist records the registry, so a re-run without it enables a different agent."""
    install = deployment.InstallCommand("demo", Path("/srv/alt registry.json"))
    assert install.render(enable_agent=True) == (
        f"./install.sh --project demo --registry {shlex.quote(str(Path('/srv/alt registry.json')))} "
        "--live --enable-agent"
    )


def test_every_command_carrying_remedy_repeats_the_registry(tmp_path):
    install = deployment.InstallCommand("demo", tmp_path / "alt.json")
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "alt.json")
    remedies = [
        deployment.dependencies_check(install).remedy,
        deployment.python_version_check((3, 9), install).remedy,
        deployment.agent_plist_check(spec, tmp_path / "home", install).remedy,
        deployment.agent_loaded_check(spec, install).remedy,
    ]
    for remedy in remedies:
        assert f"--registry {shlex.quote(str(tmp_path / 'alt.json'))}" in remedy, remedy


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


def test_sheet_reachable_check_does_not_claim_edit_access_it_never_tested():
    """A Viewer share reads fine, and sync-metadata needs Editor."""
    outcome = deployment.sheet_reachable_check(grid_with("title"), "sa@x.com").probe()
    assert "edit access is not checked" in outcome.detail


def test_sheet_reachable_check_is_unknown_when_the_network_is_down():
    def offline():
        raise google_auth.AuthUnavailable("could not reach Google to authenticate", transient=True)

    outcome = deployment.sheet_reachable_check(offline, "sa@x.com").probe()
    assert outcome.status is Status.UNKNOWN


def test_sheet_reachable_check_fails_when_google_rejects_the_key():
    """A revoked or malformed key is confirmed-broken; UNKNOWN let doctor exit 0 on it."""

    def rejected():
        raise google_auth.AuthUnavailable("Google rejected the service account key")

    outcome = deployment.sheet_reachable_check(rejected, "sa@x.com").probe()
    assert outcome.status is Status.FAIL
    assert outcome.remedy == deployment.AUTH_REMEDY


def test_sheet_reachable_check_fails_on_a_tab_the_sheet_does_not_have():
    def bad_range():
        raise make_http_error(message="Unable to parse range: Shet1", status=400)

    outcome = deployment.sheet_reachable_check(bad_range, "sa@x.com").probe()
    assert outcome.status is Status.FAIL
    assert outcome.remedy == deployment.BAD_REQUEST_REMEDY


def test_sheet_reachable_check_is_unknown_when_the_probe_declines_to_read():
    def placeholder():
        raise deployment.SheetNotChecked("the test-mode sheet_id is still a placeholder")

    outcome = deployment.sheet_reachable_check(placeholder, "sa@x.com").probe()
    assert outcome.status is Status.UNKNOWN
    assert "placeholder" in outcome.detail


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


def proceeds(grid):
    return None


def never_consulted(grid):
    pytest.fail("sync_refusal ran without a Sheet to judge")


def test_sync_columns_check_passes_when_sync_metadata_would_proceed():
    probe = grid_with("identifier", "title", *sync_state.SYNC_STATE_COLUMNS)
    assert deployment.sync_columns_check(probe, proceeds).probe().status is Status.PASS


def test_sync_columns_check_hands_sync_metadata_the_whole_grid():
    seen = []
    probe = grid_with("identifier", "title")
    deployment.sync_columns_check(probe, lambda grid: seen.append(grid)).probe()
    assert seen == [[["identifier", "title"], ["a-value", "a-value"]]]


def test_sync_columns_check_fails_with_sync_metadatas_own_refusal():
    refusal = f"the Sheet has no column(s) named {sync_state.IA_LAST_SYNCED_COLUMN}"
    outcome = deployment.sync_columns_check(grid_with("title"), lambda grid: refusal).probe()
    assert outcome.status is Status.FAIL
    assert outcome.detail == refusal


def test_sync_columns_check_points_a_refused_read_at_sharing_not_the_header_row():
    def forbidden():
        raise make_http_error(status=403)

    outcome = deployment.sync_columns_check(forbidden, never_consulted).probe()
    assert outcome.status is Status.FAIL
    assert outcome.remedy == deployment.SHEET_REFUSED_REMEDY


def test_sync_columns_check_is_unknown_when_the_sheet_cannot_be_read():
    def offline():
        raise google_auth.AuthUnavailable("no network", transient=True)

    assert deployment.sync_columns_check(offline, never_consulted).probe().status is Status.UNKNOWN


def test_sync_columns_check_fails_on_a_wrong_sheet_id():
    def not_found():
        raise make_http_error(message="Requested entity was not found.", status=404)

    assert deployment.sync_columns_check(not_found, never_consulted).probe().status is Status.FAIL


def test_agent_plist_check_fails_when_the_plist_is_stale(tmp_path):
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    home = tmp_path / "home"
    target = launch_agent.plist_path(spec, home)
    target.parent.mkdir(parents=True)
    target.write_text("<plist>from an older checkout</plist>", encoding="utf-8")
    assert deployment.agent_plist_check(spec, home, DEMO_INSTALL).probe().status is Status.FAIL


def test_agent_plist_check_is_unknown_not_fail_when_the_agent_was_never_enabled(tmp_path):
    """FAIL would have setup's fix() create the plist, and launchd loads every
    plist in LaunchAgents at login - a live agent nobody enabled."""
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    outcome = deployment.agent_plist_check(spec, tmp_path / "home", DEMO_INSTALL).probe()
    assert outcome.status is Status.UNKNOWN
    assert "not enabled" in outcome.detail


def test_converge_never_creates_a_plist_that_was_not_there(tmp_path):
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    home = tmp_path / "home"
    deployment.converge([deployment.agent_plist_check(spec, home, DEMO_INSTALL)], announce=lambda _: None)
    assert not launch_agent.plist_path(spec, home).exists()


def test_converge_leaves_a_stale_plist_for_enable_agent_to_rewrite_and_reload(tmp_path):
    """A rewrite alone never reaches the loaded job, and from a second checkout
    it would repoint the live agent at that checkout."""
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    home = tmp_path / "home"
    target = launch_agent.plist_path(spec, home)
    target.parent.mkdir(parents=True)
    target.write_text("<plist>from another checkout</plist>", encoding="utf-8")
    deployment.converge([deployment.agent_plist_check(spec, home, DEMO_INSTALL)], announce=lambda _: None)
    assert target.read_text(encoding="utf-8") == "<plist>from another checkout</plist>"


def test_agent_checks_do_not_block_the_enabling_that_fixes_them(tmp_path):
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    assert deployment.agent_plist_check(spec, tmp_path / "home", DEMO_INSTALL).needed_by_agent is False
    assert deployment.agent_loaded_check(spec, DEMO_INSTALL).needed_by_agent is False


def test_agent_loaded_check_remedy_is_a_command_setup_accepts(tmp_path):
    """--enable-agent without --live is refused, so a remedy without it cannot work."""
    remedy = deployment.agent_loaded_check(
        launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json"), DEMO_INSTALL
    ).remedy
    assert "--live --enable-agent" in remedy


def test_agent_loaded_check_remedy_names_the_real_project_and_its_logs(tmp_path):
    remedy = deployment.agent_loaded_check(
        launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json"), DEMO_INSTALL
    ).remedy
    assert "./install.sh --project demo --live --enable-agent" in remedy
    assert "logs/launchagent-demo.out" in remedy
    assert "logs/launchagent-demo.err" in remedy


def test_agent_plist_check_has_no_fix_so_only_enable_agent_writes_it(tmp_path):
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    assert deployment.agent_plist_check(spec, tmp_path / "home", DEMO_INSTALL).fix is None


def test_agent_loaded_check_is_unknown_when_launchctl_says_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(deployment.platform_probe, "launchctl_print", lambda _: None)
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    assert deployment.agent_loaded_check(spec, DEMO_INSTALL).probe().status is Status.UNKNOWN


def test_agent_loaded_check_passes_and_reports_the_last_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deployment.platform_probe, "launchctl_print", lambda _: "\tlast exit code = 0\n"
    )
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    outcome = deployment.agent_loaded_check(spec, DEMO_INSTALL).probe()
    assert outcome.status is Status.PASS
    assert "0" in outcome.detail


def test_agent_loaded_check_fails_when_the_last_run_errored(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deployment.platform_probe, "launchctl_print", lambda _: "\tlast exit code = 1\n"
    )
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    assert deployment.agent_loaded_check(spec, DEMO_INSTALL).probe().status is Status.FAIL


def test_agent_loaded_check_has_no_fix_so_setup_never_loads_it_implicitly(tmp_path):
    # Loading is gated on --enable-agent, which setup does explicitly.
    spec = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json")
    assert deployment.agent_loaded_check(spec, DEMO_INSTALL).fix is None


def test_install_sh_python_floor_matches_the_one_python_enforces():
    script = Path("install.sh").read_text(encoding="utf-8")
    major = re.search(r"^MIN_PY_MAJOR=(\d+)$", script, re.MULTILINE)
    minor = re.search(r"^MIN_PY_MINOR=(\d+)$", script, re.MULTILINE)
    assert major and minor, "install.sh must declare MIN_PY_MAJOR and MIN_PY_MINOR"
    assert (int(major.group(1)), int(minor.group(1))) == deployment.MINIMUM_PYTHON


def test_install_sh_hands_off_to_setup_not_to_a_second_check_list():
    assert "ia_bulk.py setup" in Path("install.sh").read_text(encoding="utf-8")


def test_install_sh_rebuilds_a_venv_whose_python_does_not_qualify():
    """`[ -d .venv ]` alone reused a 3.9 or dangling venv on every run."""
    script = Path("install.sh").read_text(encoding="utf-8")
    rebuild = script.index("! qualifies ./.venv/bin/python")
    assert script.index("rm -rf .venv", rebuild) < script.index('-m venv .venv')


def test_install_sh_does_not_install_an_interpreter():
    # The refusal message may *name* brew inside an echo; what must not exist is a
    # line that runs it.
    script = Path("install.sh").read_text(encoding="utf-8")
    assert not re.search(r"^\s*brew\s+install", script, re.MULTILINE)


def test_converge_records_a_fail_when_fix_raises_and_keeps_going():
    """The real case on the target: a key placed by another account makes
    os.chmod raise PermissionError mid-convergence. Every later check used to
    be abandoned."""

    def exploding_fix() -> str:
        raise PermissionError("Operation not permitted")

    checks = [failing("unfixable", fix=exploding_fix), failing("later")]
    results = deployment.converge(checks, announce=lambda _: None)

    assert [check.name for check, _ in results] == ["unfixable", "later"]
    assert results[0][1].status is Status.FAIL
    assert "Operation not permitted" in results[0][1].detail


def test_converge_report_carries_both_the_error_and_the_remedy():
    def exploding_fix() -> str:
        raise OSError("read-only file system")

    results = deployment.converge([failing("unfixable", fix=exploding_fix)], announce=lambda _: None)
    report = deployment.format_report(results)
    assert "read-only file system" in report
    assert "run the thing" in report


def test_converge_announces_that_the_fix_failed():
    announced = []

    def exploding_fix() -> str:
        raise OSError("nope")

    deployment.converge([failing("unfixable", fix=exploding_fix)], announce=announced.append)
    assert any("could not fix" in line for line in announced)


def test_converge_leaves_a_failing_check_with_no_fix_untouched():
    """key_present_check and dependencies_check are both FAIL-with-no-fix, so
    dropping converge's `is not None` guard would TypeError on a new Mac."""
    check = failing("no fix here", fix=None)
    (returned, outcome), = deployment.converge([check], announce=lambda _: None)
    assert returned is check
    assert outcome.status is Status.FAIL
    assert outcome.detail == "broken"


def test_sync_columns_check_fails_rather_than_unknown_when_the_refusal_itself_raises():
    """_probe's blanket handler would call this UNKNOWN. A header row this tool
    cannot map is confirmed-broken, not unknowable."""

    def broken(grid):
        raise ValueError("header cell is not text")

    outcome = deployment.sync_columns_check(grid_with("title"), broken).probe()
    assert outcome.status is Status.FAIL


def test_agent_plist_check_remedy_leads_with_install_sh_then_the_runbook(tmp_path):
    """On the `doctor` path no fix() ran, so the likeliest cause is simply that
    ./install.sh was never run for this account - that has to come first. The
    write-failure case only applies on the `setup` path, and stays secondary."""
    remedy = deployment.agent_plist_check(
        launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "registry.json"),
        tmp_path / "home",
        DEMO_INSTALL,
    ).remedy
    assert remedy.startswith("./install.sh --project demo --live --enable-agent")
    assert remedy.index("install.sh") < remedy.index("could not be written")
    assert "DEPLOYMENT.md" in remedy


def test_unverified_sheet_checks_names_an_unknown_sheet_check():
    """UNKNOWN counts as unverified here, and only here: exit_code still ignores
    it, so `doctor` keeps exiting 0 on a machine that is merely offline."""
    results = [
        (passing("python version"), CheckOutcome(Status.PASS, "3.12")),
        (
            passing(deployment.SHEET_REACHABLE_CHECK),
            CheckOutcome(Status.UNKNOWN, "could not authenticate"),
        ),
        (passing(deployment.SYNC_COLUMNS_CHECK), CheckOutcome(Status.PASS, "both present")),
    ]
    assert deployment.unverified_sheet_checks(results) == [deployment.SHEET_REACHABLE_CHECK]
    assert deployment.exit_code(results) == 0


def test_unverified_sheet_checks_is_empty_when_both_sheet_checks_pass():
    results = [
        (passing(deployment.SHEET_REACHABLE_CHECK), CheckOutcome(Status.PASS, "read 10 rows")),
        (passing(deployment.SYNC_COLUMNS_CHECK), CheckOutcome(Status.PASS, "both present")),
        (passing("files drive"), CheckOutcome(Status.UNKNOWN, "unplugged?")),
    ]
    assert deployment.unverified_sheet_checks(results) == []


def test_the_sheet_checks_carry_the_names_the_agent_gate_looks_for():
    """The gate matches by name, so a renamed check would silently stop blocking."""
    reachable = deployment.sheet_reachable_check(grid_with("title"), "sa@x.com")
    columns = deployment.sync_columns_check(grid_with("title"), proceeds)
    assert reachable.name in deployment.LIVE_SHEET_CHECKS
    assert columns.name in deployment.LIVE_SHEET_CHECKS


IA_CONFIG_WITH_KEYS = "[s3]\naccess = AAAA\nsecret = BBBB\n"


def test_ia_credentials_check_fails_when_there_is_no_config(tmp_path):
    missing = tmp_path / "ia.ini"
    outcome = deployment.ia_credentials_check(str(missing)).probe()
    assert outcome.status is Status.FAIL
    assert str(missing) in outcome.detail


def test_ia_credentials_check_remedy_is_ia_configure(tmp_path):
    assert "ia configure" in deployment.ia_credentials_check(str(tmp_path / "ia.ini")).remedy


def test_ia_credentials_check_has_no_fix_because_a_human_types_them(tmp_path):
    assert deployment.ia_credentials_check(str(tmp_path / "ia.ini")).fix is None


def test_ia_credentials_check_passes_on_a_config_with_both_s3_keys(tmp_path):
    config = tmp_path / "ia.ini"
    config.write_text(IA_CONFIG_WITH_KEYS, encoding="utf-8")
    outcome = deployment.ia_credentials_check(str(config)).probe()
    assert outcome.status is Status.PASS
    assert str(config) in outcome.detail


def test_ia_credentials_check_never_reports_the_secret_values(tmp_path):
    config = tmp_path / "ia.ini"
    config.write_text(IA_CONFIG_WITH_KEYS, encoding="utf-8")
    outcome = deployment.ia_credentials_check(str(config)).probe()
    assert "AAAA" not in outcome.detail
    assert "BBBB" not in outcome.detail


def test_ia_credentials_check_fails_on_a_config_missing_the_s3_keys(tmp_path):
    config = tmp_path / "ia.ini"
    config.write_text("[general]\nscreenname = someone\n", encoding="utf-8")
    outcome = deployment.ia_credentials_check(str(config)).probe()
    assert outcome.status is Status.FAIL
    assert "secret" in outcome.detail


def test_ia_credentials_check_fails_on_an_unparseable_config(tmp_path):
    config = tmp_path / "ia.ini"
    config.write_text("this is not an ini file\n", encoding="utf-8")
    assert deployment.ia_credentials_check(str(config)).probe().status is Status.FAIL


def test_ia_credentials_check_makes_no_network_call(tmp_path, monkeypatch):
    """Presence and location only - `doctor` stays usable with no network, and
    never spends a write credential to prove it works."""
    import socket

    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: pytest.fail("ia credentials check opened a socket")
    )
    config = tmp_path / "ia.ini"
    config.write_text(IA_CONFIG_WITH_KEYS, encoding="utf-8")
    assert deployment.ia_credentials_check(str(config)).probe().status is Status.PASS


def test_ia_credentials_mode_check_is_unknown_when_the_config_is_absent(tmp_path):
    check = deployment.ia_credentials_mode_check(str(tmp_path / "ia.ini"))
    assert check.probe().status is Status.UNKNOWN


def test_ia_credentials_mode_check_fails_on_a_group_readable_config(tmp_path, monkeypatch):
    config = tmp_path / "ia.ini"
    config.write_text(IA_CONFIG_WITH_KEYS, encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: True)
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o644)
    assert deployment.ia_credentials_mode_check(str(config)).probe().status is Status.FAIL


def test_ia_credentials_mode_check_passes_on_0o600(tmp_path, monkeypatch):
    config = tmp_path / "ia.ini"
    config.write_text(IA_CONFIG_WITH_KEYS, encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: True)
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o600)
    assert deployment.ia_credentials_mode_check(str(config)).probe().status is Status.PASS


def test_ia_credentials_mode_check_is_unknown_where_posix_permissions_do_not_apply(tmp_path, monkeypatch):
    config = tmp_path / "ia.ini"
    config.write_text(IA_CONFIG_WITH_KEYS, encoding="utf-8")
    monkeypatch.setattr(deployment.platform_probe, "has_posix_permissions", lambda: False)
    monkeypatch.setattr(deployment.platform_probe, "file_mode", lambda _: 0o666)
    assert deployment.ia_credentials_mode_check(str(config)).probe().status is Status.UNKNOWN


def test_ia_credentials_mode_check_has_no_fix_outside_the_checkout(tmp_path):
    assert deployment.ia_credentials_mode_check(str(tmp_path / "ia.ini")).fix is None
