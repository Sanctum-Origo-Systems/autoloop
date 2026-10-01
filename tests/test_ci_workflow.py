"""Tests for the CI workflow matrix configuration."""

import subprocess
from pathlib import Path

CI_WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "autoloop"

PATH_CWD_WHITELIST = [
    "cli.py",
    "config.py",
    "implement_issue.py",
    "init.py",
    "triage_issues.py",
    "auto_close_parent.py",
    "create_issue.py",
]


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


# --- check-path-cwd gate ---


def test_check_path_cwd_step_exists():
    content = _read_ci()
    assert "check-path-cwd" in content


def test_check_path_cwd_excludes_whitelisted_files():
    content = _read_ci()
    for f in PATH_CWD_WHITELIST:
        assert f"--exclude={f}" in content


def _run_grep(src_dir):
    return subprocess.run(
        ["grep", "-rn", "--include=*.py", r"Path\.cwd()", str(src_dir)]
        + [f"--exclude={f}" for f in PATH_CWD_WHITELIST],
        capture_output=True,
        text=True,
    )


def test_check_path_cwd_passes_when_only_whitelisted(tmp_path):
    src = tmp_path / "src" / "autoloop"
    src.mkdir(parents=True)
    for f in PATH_CWD_WHITELIST:
        (src / f).write_text("x = Path.cwd()\n")
    (src / "other.py").write_text("x = 1\n")

    result = _run_grep(src)
    assert result.returncode == 1


def test_check_path_cwd_fails_when_non_whitelisted(tmp_path):
    src = tmp_path / "src" / "autoloop"
    src.mkdir(parents=True)
    for f in PATH_CWD_WHITELIST:
        (src / f).write_text("x = Path.cwd()\n")
    (src / "other.py").write_text("x = Path.cwd()\n")

    result = _run_grep(src)
    assert result.returncode == 0
    assert "other.py" in result.stdout


def test_check_path_cwd_no_false_positives_on_current_codebase():
    result = _run_grep(SRC_DIR)
    assert result.returncode == 1, f"Disallowed Path.cwd() usage found:\n{result.stdout}"
