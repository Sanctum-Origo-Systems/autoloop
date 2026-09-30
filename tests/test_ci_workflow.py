"""Tests for the CI workflow matrix configuration."""

from pathlib import Path

CI_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"


def _read_ci():
    return CI_WORKFLOW.read_text()


def test_ci_workflow_exists():
    assert CI_WORKFLOW.exists()


def test_matrix_includes_ubuntu():
    content = _read_ci()
    assert "ubuntu-latest" in content


def test_matrix_includes_macos():
    content = _read_ci()
    assert "macos-latest" in content


def test_matrix_os_list_has_both_runners():
    content = _read_ci()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("os:"):
            assert "ubuntu-latest" in stripped
            assert "macos-latest" in stripped
            break
    else:
        raise AssertionError("No 'os:' line found in CI workflow matrix")


def test_runs_on_uses_matrix_os():
    content = _read_ci()
    assert "${{ matrix.os }}" in content


def test_ci_runs_pytest():
    content = _read_ci()
    assert "pytest" in content


def test_ci_runs_ruff_check():
    content = _read_ci()
    assert "ruff check" in content


def test_ci_runs_ruff_format_check():
    content = _read_ci()
    assert "ruff format --check" in content


def test_ci_triggers_on_merge_group():
    content = _read_ci()
    assert "merge_group" in content


def test_ci_bans_path_cwd():
    """CI must have a step that greps for Path.cwd() in src/autoloop/ and fails if found."""
    content = _read_ci()
    assert "Path.cwd()" in content, "CI workflow missing Path.cwd() ban step"
    assert "src/autoloop/" in content, "Path.cwd() ban must target src/autoloop/"


def test_ci_ban_path_cwd_fails_on_match():
    """The ban step must exit non-zero when grep finds a match."""
    content = _read_ci()
    assert "exit 1" in content, "Path.cwd() ban step must exit 1 on match"
