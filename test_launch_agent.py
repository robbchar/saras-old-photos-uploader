import plistlib
import shlex
from pathlib import Path
from typing import NamedTuple

import pytest

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
INSTALL_SH = ia_bulk.REPO_ROOT / "install.sh"


class Invocation(NamedTuple):
    working_directory: Path
    script: str
    arguments: list[str]


def agent_invocation() -> Invocation:
    """The agent's argv, less the interpreter, from its WorkingDirectory."""
    spec = launch_agent.sync_agent_spec(ia_bulk.REPO_ROOT, "demo", OTHER_REGISTRY)
    _interpreter, script, *arguments = spec.program_arguments
    return Invocation(spec.working_directory, script, arguments)


def install_sh_handoff() -> tuple[str, str]:
    """The script and subcommand install.sh execs from its own directory with "$@"."""
    lines = INSTALL_SH.read_text(encoding="utf-8").splitlines()
    assert 'cd "$(dirname "$0")"' in lines, "install.sh no longer runs from its own directory"
    exec_lines = [line.strip() for line in lines if line.strip().startswith("exec ")]
    assert len(exec_lines) == 1, f"install.sh should hand off with one exec line: {exec_lines}"
    words = exec_lines[0].split()
    assert len(words) == 5 and words[-1] == '"$@"', (
        f'expected `exec <interpreter> <script> <subcommand> "$@"`: {exec_lines[0]!r}'
    )
    _exec, interpreter, script, subcommand, _forwarded = words
    assert Path(interpreter) == Path(".venv", "bin", "python"), f"install.sh runs {interpreter}"
    return script, subcommand


def install_invocation(*registry_arguments: str) -> Invocation:
    """The ./install.sh line setup prints, as the ia_bulk.py call install.sh makes."""
    setup_args = ia_bulk.build_parser().parse_args(["setup", "--project", "demo", *registry_arguments])
    printed = ia_bulk.install_command_for(setup_args).render(enable_agent=True)
    _install_sh, *arguments = shlex.split(printed)
    script, subcommand = install_sh_handoff()
    return Invocation(INSTALL_SH.parent, script, [subcommand, *arguments])


@pytest.mark.parametrize(
    ("invocation", "expected"),
    [
        pytest.param(
            agent_invocation,
            {"command": "sync-metadata", "project": "demo", "live": True, "registry": OTHER_REGISTRY},
            id="launch_agent",
        ),
        pytest.param(
            # Relative to where setup ran, not to where install.sh runs.
            lambda: install_invocation("--registry", OTHER_REGISTRY.name),
            {
                "command": "setup",
                "project": "demo",
                "live": True,
                "enable_agent": True,
                "registry": OTHER_REGISTRY,
            },
            id="install_other_registry",
        ),
        pytest.param(
            lambda: install_invocation("--registry", str(CHECKOUT_REGISTRY)),
            {
                "command": "setup",
                "project": "demo",
                "live": True,
                "enable_agent": True,
                "registry": CHECKOUT_REGISTRY,
            },
            id="install_checkout_registry",
        ),
    ],
)
def test_generated_command_lines_parse_with_the_real_parser(invocation, expected, monkeypatch):
    """Nothing runs these lines before the Mac does; the agent would exit 2 every hour."""
    # Run setup from outside the checkout: the printed line must still name the registry setup read.
    monkeypatch.chdir(OTHER_REGISTRY.parent)
    working_directory, script, arguments = invocation()
    # Relative paths and defaults such as --log-dir logs are read from the checkout.
    assert working_directory == ia_bulk.REPO_ROOT
    assert (working_directory / script).resolve() == Path(ia_bulk.__file__).resolve()
    parser = ia_bulk.build_parser()
    parsed = vars(parser.parse_args(arguments))
    parsed["registry"] = (working_directory / parsed["registry"]).resolve()
    # Whole namespace, so an extra flag such as --dry-run fails too.
    defaults = vars(parser.parse_args([expected["command"], "--project", expected["project"]]))
    assert parsed == {**defaults, **expected}


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
    assert not spec.output_path.parent.exists()
    launch_agent.write_plist(spec, tmp_path / "home")
    assert spec.output_path.parent.is_dir()
