import plistlib
from pathlib import Path

import launch_agent


def a_spec(tmp_path, project_id="demo"):
    return launch_agent.sync_agent_spec(tmp_path / "repo", project_id)


def test_sync_agent_spec_labels_the_agent_per_project(tmp_path):
    assert a_spec(tmp_path, "sarasoldphotos").label.endswith("sarasoldphotos")


def test_sync_agent_spec_runs_the_venv_interpreter_not_whatever_is_on_path(tmp_path):
    spec = a_spec(tmp_path)
    assert spec.program_arguments[0].endswith(str(Path(".venv") / "bin" / "python"))


def test_sync_agent_spec_runs_sync_metadata_live(tmp_path):
    arguments = a_spec(tmp_path).program_arguments
    assert "sync-metadata" in arguments
    assert "--live" in arguments


def test_sync_agent_spec_is_hourly(tmp_path):
    assert a_spec(tmp_path).interval == launch_agent.HOURLY


def test_render_plist_is_parseable_and_carries_the_interval(tmp_path):
    spec = a_spec(tmp_path)
    parsed = plistlib.loads(launch_agent.render_plist(spec).encode("utf-8"))
    assert parsed["StartInterval"] == launch_agent.HOURLY
    assert parsed["Label"] == spec.label


def test_render_plist_uses_absolute_program_paths(tmp_path):
    parsed = plistlib.loads(launch_agent.render_plist(a_spec(tmp_path)).encode("utf-8"))
    assert Path(parsed["ProgramArguments"][0]).is_absolute()
    assert Path(parsed["ProgramArguments"][1]).is_absolute()


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
    moved = launch_agent.sync_agent_spec(tmp_path / "elsewhere", "demo")
    assert launch_agent.plist_is_current(moved, home) is False
