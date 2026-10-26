from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from autoloop.config import RepoContext

# Captured at import, before any test redirects HOME.
REAL_HOME = Path.home()
REAL_AUTOLOOP_DIR = REAL_HOME / ".autoloop"
GUARD_TEST_NAME = "test_suite_did_not_write_to_real_autoloop_dir"

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
# Audit event -> positions of the path arguments that the event writes to.
_WRITE_EVENTS = {
    "os.mkdir": (0,),
    "os.rename": (0, 1),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.symlink": (1,),
    "os.link": (1,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "os.chmod": (0,),
    "shutil.copyfile": (1,),
    "shutil.rmtree": (0,),
}


class WriteGuard:
    """Record in-process filesystem writes under `root`, ignoring paths under `exclude`."""

    def __init__(self, root: Path, exclude: tuple[Path, ...] = ()):
        self.root = Path(os.path.abspath(root))
        self.exclude = tuple(Path(os.path.abspath(p)) for p in exclude)
        self.violations: list[str] = []

    def audit(self, event: str, args: tuple) -> None:
        if event == "open":
            path, mode, flags = args
            if mode is not None:
                if not any(c in mode for c in "wax+"):
                    return
            elif not flags & _WRITE_FLAGS:
                return
            paths = (path,)
        elif event in _WRITE_EVENTS:
            paths = tuple(args[i] for i in _WRITE_EVENTS[event])
        else:
            return
        for path in paths:
            if not isinstance(path, (str, bytes, os.PathLike)):
                continue
            target = Path(os.path.abspath(os.fsdecode(path)))
            if not target.is_relative_to(self.root):
                continue
            if any(target.is_relative_to(p) for p in self.exclude):
                continue
            test = os.environ.get("PYTEST_CURRENT_TEST", "<outside a test>")
            self.violations.append(f"{test}: {event} {target}")


# Audit hooks cannot be removed, so one hook dispatches to whichever guards are active.
ACTIVE_GUARDS: list[WriteGuard] = []
REAL_HOME_GUARD: WriteGuard | None = None


def _dispatch_audit(event: str, args: tuple) -> None:
    for guard in ACTIVE_GUARDS:
        guard.audit(event, args)


def pytest_configure(config: pytest.Config) -> None:
    global REAL_HOME_GUARD
    # The checkout itself may live under ~/.autoloop/<hash>/worktrees; pytest writes caches there.
    REAL_HOME_GUARD = WriteGuard(REAL_AUTOLOOP_DIR, exclude=(config.rootpath,))
    ACTIVE_GUARDS.append(REAL_HOME_GUARD)
    sys.addaudithook(_dispatch_audit)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run the real-home guard test last so it sees writes from every other test."""
    items.sort(key=lambda item: item.name == GUARD_TEST_NAME)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch) -> Path:
    """Point HOME at a per-test temp dir so Path.home() never resolves to the real home."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture()
def repo_ctx(tmp_path: Path) -> RepoContext:
    """Create a RepoContext pointing at tmp_path for test isolation."""
    home = tmp_path / "fakehome"
    home.mkdir()
    completed = subprocess.CompletedProcess(
        args=["git", "remote", "get-url", "origin"],
        returncode=128,
        stdout="",
        stderr="fatal: no remote",
    )
    with (
        patch("autoloop.config.Path.home", return_value=home),
        patch("autoloop.config.subprocess.run", return_value=completed),
    ):
        return RepoContext(repo_dir=tmp_path)


@pytest.fixture()
def autoloop_toml(tmp_path: Path) -> Path:
    """Write a minimal autoloop.toml and return its path."""
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text(
        'repo = "acme-corp/widget"\n'
        'triage_model = "haiku"\n'
        'impl_model = "opus"\n'
        "impl_timeout = 600\n"
        "triage_timeout = 45\n"
        "test_timeout = 60\n"
        'pr_reviewer = "review-bot"\n'
        "max_retries = 5\n"
        "tree_truncation = 2000\n"
        "diff_truncation = 6000\n"
        "error_truncation = 1500\n"
        "spec_truncation = 3000\n"
        'verify_cmd = "echo ok"\n'
        'lint_command = "echo lint"\n'
        "max_story_points = 3\n"
        'triage_labels = ["ready", "blocked"]\n'
    )
    return toml_path
