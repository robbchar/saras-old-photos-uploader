"""Guards for the macOS one-click launcher `start-upload-page-live.command`.

The launcher only runs on the Mac and can't be exercised off one, so these
checks cover what CAN still regress from a Windows or CI edit: its shell
syntax, its LF line endings (a CRLF slip reintroduces the `bad interpreter:
bash^M` failure `.gitattributes` guards against), its executable bit in git,
and - the subtlest - that the `serve` flags it passes still exist on the CLI,
so renaming one in `ia_bulk.py` without updating the launcher fails here
instead of at click time on the Mac.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ia_bulk import build_parser

LAUNCHER = Path(__file__).parent / "start-upload-page-live.command"


def test_launcher_uses_lf_line_endings():
    # CRLF here means "bad interpreter: bash^M" on the Mac; `.gitattributes`
    # forces LF, and this fails loudly if that ever stops holding.
    assert b"\r\n" not in LAUNCHER.read_bytes()


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not on PATH")
def test_launcher_has_valid_bash_syntax():
    # Pass the name relative to cwd, not an absolute path: Git Bash on Windows
    # mangles the backslashes in a Windows-style path argument.
    result = subprocess.run(
        ["bash", "-n", LAUNCHER.name],
        capture_output=True,
        text=True,
        cwd=LAUNCHER.parent,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_launcher_is_executable_in_git():
    # `.command` files must be executable to double-click; git tracks the bit
    # in the index even on Windows, where the filesystem does not.
    result = subprocess.run(
        ["git", "ls-files", "-s", "--", LAUNCHER.name],
        capture_output=True,
        text=True,
        cwd=LAUNCHER.parent,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split(" ", 1)[0] == "100755", result.stdout


def _launcher_serve_flags() -> set[str]:
    """The long options the launcher passes to `ia_bulk.py serve`."""
    text = LAUNCHER.read_text()
    start = text.index("ia_bulk.py serve")
    # The invocation is line-continued until the line that backgrounds it.
    end = text.index("&", start)
    return set(re.findall(r"--[a-z][a-z-]*", text[start:end]))


def _serve_parser_flags() -> set[str]:
    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    serve = subparsers.choices["serve"]
    return {opt for action in serve._actions for opt in action.option_strings}


def test_launcher_serve_flags_all_exist_on_the_cli():
    # Renaming a serve flag in ia_bulk without updating the launcher would only
    # surface on the Mac; this makes that drift fail in CI instead.
    missing = _launcher_serve_flags() - _serve_parser_flags()
    assert not missing, f"launcher passes serve flags the CLI rejects: {missing}"
