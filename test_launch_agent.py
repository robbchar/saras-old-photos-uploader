import plistlib
from pathlib import Path

import launch_agent


def a_spec(tmp_path, project_id="demo"):
    return launch_agent.sync_agent_spec(tmp_path / "repo", project_id, tmp_path / "registry.json")


def test_sync_agent_spec_labels_the_agent_per_project(tmp_path):
    assert a_spec(tmp_path, "sarasoldphotos").label.endswith("sarasoldphotos")


def test_sync_agent_spec_runs_the_venv_interpreter_not_whatever_is_on_path(tmp_path):
    spec = a_spec(tmp_path)
    assert spec.program_arguments[0].endswith(str(Path(".venv") / "bin" / "python"))


def test_sync_agent_spec_runs_sync_metadata_live(tmp_path):
    arguments = a_spec(tmp_path).program_arguments
    assert "sync-metadata" in arguments
    assert "--live" in arguments


def test_sync_agent_spec_reads_the_registry_setup_was_given(tmp_path):
    """setup gates on --registry, so an agent reading the default would sync a
    Sheet nothing checked."""
    arguments = launch_agent.sync_agent_spec(
        tmp_path / "repo", "demo", tmp_path / "alt.json"
    ).program_arguments
    assert arguments[arguments.index("--registry") + 1] == str((tmp_path / "alt.json").resolve())


def test_sync_agent_spec_makes_a_relative_registry_absolute(tmp_path, monkeypatch):
    # The agent's WorkingDirectory is the checkout, not wherever setup was run from.
    monkeypatch.chdir(tmp_path)
    arguments = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", "alt.json").program_arguments
    registry_argument = arguments[arguments.index("--registry") + 1]
    assert registry_argument == str(tmp_path.resolve() / "alt.json")


def test_plist_is_current_is_false_once_the_registry_changes(tmp_path):
    home = tmp_path / "home"
    launch_agent.write_plist(a_spec(tmp_path), home)
    other = launch_agent.sync_agent_spec(tmp_path / "repo", "demo", tmp_path / "other.json")
    assert launch_agent.plist_is_current(other, home) is False


def test_sync_agent_spec_is_hourly(tmp_path):
    assert a_spec(tmp_path).interval == launch_agent.HOURLY


def test_render_plist_is_parseable_and_carries_the_interval(tmp_path):
    spec = a_spec(tmp_path)
    parsed = plistlib.loads(launch_agent.render_plist(spec).encode("utf-8"))
    assert parsed["StartInterval"] == launch_agent.HOURLY
    assert parsed["Label"] == spec.label


def test_render_plist_uses_absolute_program_paths(tmp_path):
    arguments = plistlib.loads(launch_agent.render_plist(a_spec(tmp_path)).encode("utf-8"))["ProgramArguments"]
    script = next(argument for argument in arguments if argument.endswith("ia_bulk.py"))
    assert Path(arguments[0]).is_absolute()
    assert Path(script).is_absolute()


def test_sync_agent_spec_runs_python_unbuffered(tmp_path):
    # Both streams share one file; buffered stdout would land after stderr written later.
    assert a_spec(tmp_path).program_arguments[1] == "-u"


def test_render_plist_sends_stdout_and_stderr_to_one_log_file(tmp_path):
    parsed = plistlib.loads(launch_agent.render_plist(a_spec(tmp_path, "demo")).encode("utf-8"))
    assert parsed["StandardOutPath"] == parsed["StandardErrorPath"]
    assert Path(parsed["StandardOutPath"]).name == "launchagent-demo.log"


def test_render_plist_runs_at_load(tmp_path):
    # Enabling the agent syncs straight away rather than after an idle hour, and
    # a login run with no changed rows is a no-op under the #24 hash gate.
    parsed = plistlib.loads(launch_agent.render_plist(a_spec(tmp_path)).encode("utf-8"))
    assert parsed["RunAtLoad"] is True


def test_plist_path_lands_in_the_users_launchagents(tmp_path):
    home = tmp_path / "home"
    path = launch_agent.plist_path(a_spec(tmp_path), home)
    assert path.parent == home / "Library" / "LaunchAgents"
    assert path.name.endswith(".plist")


def test_write_plist_creates_the_directory_and_the_file(tmp_path):
    home = tmp_path / "home"
    launch_agent.write_plist(a_spec(tmp_path), home)
    assert launch_agent.plist_path(a_spec(tmp_path), home).exists()


def test_plist_is_current_is_false_before_it_is_written(tmp_path):
    assert launch_agent.plist_is_current(a_spec(tmp_path), tmp_path / "home") is False


def test_plist_is_current_is_true_right_after_writing(tmp_path):
    home = tmp_path / "home"
    spec = a_spec(tmp_path)
    launch_agent.write_plist(spec, home)
    assert launch_agent.plist_is_current(spec, home) is True


def test_plist_is_current_is_false_once_the_repo_moves(tmp_path):
    home = tmp_path / "home"
    launch_agent.write_plist(a_spec(tmp_path), home)
    moved = launch_agent.sync_agent_spec(tmp_path / "elsewhere", "demo", tmp_path / "registry.json")
    assert launch_agent.plist_is_current(moved, home) is False


def test_write_plist_puts_lf_line_endings_on_disk_not_crlf(tmp_path):
    # read_text() universal-newline-decodes \r\n back to \n, so comparing decoded
    # text (as plist_is_current does) can't catch a write that used CRLF on disk.
    home = tmp_path / "home"
    spec = a_spec(tmp_path)
    launch_agent.write_plist(spec, home)
    on_disk = launch_agent.plist_path(spec, home).read_bytes()
    assert b"\r\n" not in on_disk


def test_write_plist_creates_the_stdio_directory_launchd_will_not(tmp_path):
    """logs/ is gitignored, so a fresh clone has none. launchd creates no
    intermediate directories for StandardOutPath/StandardErrorPath, so the job
    either fails to spawn or its output vanishes."""
    spec = a_spec(tmp_path)
    assert not spec.log_path.parent.exists()
    launch_agent.write_plist(spec, tmp_path / "home")
    assert spec.log_path.parent.is_dir()
