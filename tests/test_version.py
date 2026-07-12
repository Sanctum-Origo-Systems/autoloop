from __future__ import annotations

import tomllib
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


def _read_version() -> str:
    with open(REPO_DIR / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_init_version_matches_pyproject():
    from autoloop import __version__

    assert __version__ == _read_version()


def test_cli_version_flag(capsys):
    import sys
    from unittest.mock import patch

    from autoloop.cli import main

    with patch.object(sys, "argv", ["autoloop", "--version"]):
        try:
            main()
        except SystemExit as e:
            assert e.code == 0
    captured = capsys.readouterr()
    assert _read_version() in captured.out
