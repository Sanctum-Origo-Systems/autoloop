"""Tests that the suite never touches the real ~/.autoloop data dir (#401)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from autoloop.config import RepoContext
from tests import conftest


def test_path_home_points_at_temp_dir(isolated_home, tmp_path_factory):
    assert Path.home() == isolated_home
    assert Path.home() != conftest.REAL_HOME
    assert isolated_home.is_relative_to(tmp_path_factory.getbasetemp())


def test_repo_context_data_dir_under_temp_home(isolated_home, request):
    # Built from the real checkout: the same remote URL that leaked fixture rows before.
    ctx = RepoContext(repo_dir=request.config.rootpath)

    assert ctx.data_dir.is_relative_to(isolated_home / ".autoloop")
    assert not ctx.data_dir.is_relative_to(conftest.REAL_AUTOLOOP_DIR)


@pytest.fixture()
def probe_guard(tmp_path):
    guarded = tmp_path / "realhome" / ".autoloop"
    guarded.mkdir(parents=True)
    excluded = guarded / "abc123" / "worktrees" / "checkout"
    guard = conftest.WriteGuard(guarded, exclude=(excluded,))
    conftest.ACTIVE_GUARDS.append(guard)
    yield guard, guarded, excluded
    conftest.ACTIVE_GUARDS.remove(guard)


def test_write_guard_flags_writes_under_root(probe_guard):
    guard, guarded, _ = probe_guard
    history = guarded / "abc123" / "run_history.jsonl"

    history.parent.mkdir()
    history.write_text('{"type": "review", "pr_number": 42}\n')
    with open(history, "a") as f:
        f.write("{}\n")
    os.close(os.open(guarded / "touched", os.O_WRONLY | os.O_CREAT))
    shutil.copyfile(history, guarded / "copy.jsonl")
    (guarded / "copy.jsonl").rename(guarded / "moved.jsonl")

    assert "test_write_guard_flags_writes_under_root" in guard.violations[0]
    assert any(f"os.mkdir {history.parent}" in v for v in guard.violations)
    assert sum(f"open {history}" in v for v in guard.violations) == 2
    assert any(f"open {guarded / 'touched'}" in v for v in guard.violations)
    assert any(f"shutil.copyfile {guarded / 'copy.jsonl'}" in v for v in guard.violations)
    assert any(f"os.rename {guarded / 'moved.jsonl'}" in v for v in guard.violations)


def test_write_guard_ignores_reads_excluded_and_outside_paths(probe_guard, tmp_path):
    guard, guarded, excluded = probe_guard
    existing = guarded / "run_history.jsonl"
    existing.write_text("{}\n")
    excluded.parent.mkdir(parents=True)
    guard.violations.clear()

    existing.read_text()
    os.close(os.open(existing, os.O_RDONLY))
    excluded.mkdir()
    (excluded / "cache.pyc").write_bytes(b"")
    (tmp_path / "elsewhere.txt").write_text("x")

    assert guard.violations == []


def test_write_guard_flags_attempted_write_to_real_autoloop_dir(monkeypatch):
    # Parent dir does not exist, so the open fails after the audit hook has seen it and
    # nothing reaches the real disk. The suite-wide guard is swapped out for the probe.
    probe = conftest.REAL_AUTOLOOP_DIR / "missing-dir-guard-probe" / "run_history.jsonl"
    guard = conftest.WriteGuard(conftest.REAL_AUTOLOOP_DIR)
    monkeypatch.setattr(conftest, "ACTIVE_GUARDS", [guard])

    with pytest.raises(FileNotFoundError):
        open(probe, "a")

    assert len(guard.violations) == 1
    assert str(probe) in guard.violations[0]


@pytest.fixture()
def repo_tree_probe(tmp_path):
    tree = tmp_path / "checkout"
    (tree / "tests").mkdir(parents=True)
    (tree / "src" / "autoloop").mkdir(parents=True)
    guard = conftest.WriteGuard(tree, ignored_names=conftest.REPO_TREE_IGNORED_NAMES)
    conftest.ACTIVE_GUARDS.append(guard)
    yield guard, tree
    conftest.ACTIVE_GUARDS.remove(guard)


def test_repo_tree_guard_covers_directory_containing_tests(request):
    assert conftest.REPO_TREE == Path(__file__).resolve().parent.parent
    assert conftest.REPO_TREE_GUARD in conftest.ACTIVE_GUARDS
    assert conftest.REPO_TREE_GUARD.root == conftest.REPO_TREE


def test_repo_tree_guard_flags_legacy_run_history_write(repo_tree_probe):
    guard, tree = repo_tree_probe
    legacy = tree / "autoloop" / "run_history.jsonl"

    legacy.parent.mkdir()
    legacy.write_text('{"type": "review", "pr_number": 42}\n')

    assert any(f"os.mkdir {legacy.parent}" in v for v in guard.violations)
    assert any(f"open {legacy}" in v for v in guard.violations)


@pytest.mark.parametrize(
    "rel",
    [
        ".pytest_cache/v/cache/lastfailed",
        "tests/__pycache__/test_x.cpython-313.pyc",
        "src/autoloop/__pycache__/cli.cpython-313.pyc",
        ".ruff_cache/0.1/cache",
        ".venv/lib/site.py",
        ".coverage",
        ".coverage.host.1234",
    ],
)
def test_repo_tree_guard_ignores_tooling_paths(repo_tree_probe, rel):
    guard, tree = repo_tree_probe
    target = tree / rel

    if not target.parent.exists():
        target.parent.mkdir(parents=True)
    target.write_text("x")

    assert guard.violations == []


def test_suite_did_not_write_to_real_autoloop_dir():
    """Guard: fails, naming each offending path, if any test wrote under the real ~/.autoloop.

    Sorted to run last by conftest.pytest_collection_modifyitems.
    """
    violations = conftest.REAL_HOME_GUARD.violations
    assert not violations, "Tests wrote under the real ~/.autoloop:\n" + "\n".join(violations)


def test_suite_did_not_write_to_repo_tree():
    """Guard: fails, naming each offending path, if any test wrote into the repo working tree.

    Sorted to run last by conftest.pytest_collection_modifyitems.
    """
    violations = conftest.REPO_TREE_GUARD.violations
    assert not violations, "Tests wrote into the repo working tree:\n" + "\n".join(violations)
