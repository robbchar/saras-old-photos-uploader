import argparse
import plistlib
import re
import shlex
from pathlib import Path

import pytest

import deployment
import ia_bulk
import launch_agent


def a_spec(tmp_path, project_id="demo"):
    return launch_agent.sync_agent_spec(tmp_path / "repo", project_id, tmp_path / "registry.json")


def test_sync_agent_spec_labels_the_agent_per_project(tmp_path):
    assert a_spec(tmp_path, "sarasoldphotos").label.endswith("sarasoldphotos")


def test_sync_agent_spec_runs_the_venv_interpreter_not_whatever_is_on_path(tmp_path):
    spec = a_spec(tmp_path)
    assert spec.program_arguments[0].endswith(str(Path(".venv") / "bin" / "python"))


OTHER_REGISTRY = (ia_bulk.REPO_ROOT.parent / "other_registry.json").resolve()
CHECKOUT_REGISTRY = (ia_bulk.REPO_ROOT / ia_bulk.DEFAULT_REGISTRY).resolve()


def strict_parser() -> argparse.ArgumentParser:
    """build_parser() without prefix matching, so a renamed flag fails rather than abbreviates."""
    parser = ia_bulk.build_parser()
    subcommands = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    for each_parser in [parser, *subcommands.choices.values()]:
        each_parser.allow_abbrev = False
    return parser


def agent_command_line() -> list[str]:
    """The agent's argv, less the interpreter."""
    _interpreter, *command_line = launch_agent.sync_agent_spec(
        ia_bulk.REPO_ROOT, "demo", OTHER_REGISTRY
    ).program_arguments
    return command_line


def install_command_line(registry: Path | None) -> list[str]:
    """The printed ./install.sh line, as the ia_bulk.py call install.sh forwards it to."""
    _install_sh, *arguments = shlex.split(
        deployment.InstallCommand("demo", registry).render(enable_agent=True)
    )
    install_sh = (ia_bulk.REPO_ROOT / "install.sh").read_text()
    forwarded = re.search(r'^exec \S+ (\S+) (\S+) "\$@"$', install_sh, re.MULTILINE)
    assert forwarded, "install.sh no longer execs a script with its own arguments"
    script, subcommand = forwarded.groups()
    return [script, subcommand, *arguments]


@pytest.mark.parametrize(
    ("command_line", "expected"),
    [
        pytest.param(
            agent_command_line,
            {"command": "sync-metadata", "project": "demo", "live": True, "registry": OTHER_REGISTRY},
            id="launch agent",
        ),
        pytest.param(
            lambda: install_command_line(OTHER_REGISTRY),
            {
                "command": "setup",
                "project": "demo",
                "live": True,
                "enable_agent": True,
                "registry": OTHER_REGISTRY,
            },
            id="install command, other registry",
        ),
        pytest.param(
            lambda: install_command_line(None),
            {
                "command": "setup",
                "project": "demo",
                "live": True,
                "enable_agent": True,
                "registry": CHECKOUT_REGISTRY,
            },
            id="install command, checkout registry",
        ),
    ],
)
def test_generated_command_lines_parse_with_the_real_parser(command_line, expected):
    """Nothing runs these lines before the Mac does; the agent would exit 2 every hour."""
    script, *arguments = command_line()
    assert (ia_bulk.REPO_ROOT / script).resolve() == Path(ia_bulk.__file__).resolve()
    parsed = vars(strict_parser().parse_args(arguments))
    # Both run from the checkout, so a relative registry is read from there.
    parsed["registry"] = (ia_bulk.REPO_ROOT / parsed["registry"]).resolve()
    assert {key: parsed[key] for key in expected} == expected


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
    assert not spec.stderr_path.parent.exists()
    launch_agent.write_plist(spec, tmp_path / "home")
    assert spec.stdout_path.parent.is_dir()
    assert spec.stderr_path.parent.is_dir()
