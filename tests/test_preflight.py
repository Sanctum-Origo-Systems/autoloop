"""Tests for autoloop preflight command."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def _cfg(**overrides):
    defaults = {
        "verify_cmd": "echo ok",
        "lint_command": "echo lint-ok",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _ok(stdout="", stderr="", returncode=0):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


def test_run_preflight_pass():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="all passed\n")

    with patch("autoloop.preflight.subprocess.run", fake_run):
        from autoloop.preflight import run_preflight

        result = run_preflight(_cfg())

    assert result["verify_cmd"]["passed"] is True
    assert result["verify_cmd"]["skipped"] is False
    assert result["verify_cmd"]["elapsed"] >= 0
    assert result["lint_command"]["passed"] is True
    assert result["lint_command"]["skipped"] is False


def test_run_preflight_verify_fails():
    def fake_run(cmd, **kwargs):
        if "echo ok" in cmd:
            return _ok(returncode=1, stdout="", stderr="FAILED test_x\nTraceback:\n  ...")
        return _ok()

    with patch("autoloop.preflight.subprocess.run", fake_run):
        from autoloop.preflight import run_preflight

        result = run_preflight(_cfg())

    assert result["verify_cmd"]["passed"] is False
    assert "FAILED test_x" in result["verify_cmd"]["output"]
    assert "Traceback" in result["verify_cmd"]["output"]


def test_run_preflight_lint_skipped_when_empty():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="ok\n")

    with patch("autoloop.preflight.subprocess.run", fake_run):
        from autoloop.preflight import run_preflight

        result = run_preflight(_cfg(lint_command=""))

    assert result["lint_command"]["skipped"] is True
    assert result["verify_cmd"]["passed"] is True


def test_run_preflight_lint_skipped_when_none():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="ok\n")

    with patch("autoloop.preflight.subprocess.run", fake_run):
        from autoloop.preflight import run_preflight

        result = run_preflight(_cfg(lint_command=None))

    assert result["lint_command"]["skipped"] is True


def test_run_preflight_lint_fails():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "lint" in cmd:
            return _ok(returncode=1, stderr="lint error\n")
        return _ok(stdout="ok\n")

    with patch("autoloop.preflight.subprocess.run", fake_run):
        from autoloop.preflight import run_preflight

        result = run_preflight(_cfg())

    assert result["verify_cmd"]["passed"] is True
    assert result["lint_command"]["passed"] is False
    assert "lint error" in result["lint_command"]["output"]


def test_run_preflight_output_combines_stdout_and_stderr():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1, stdout="stdout line\n", stderr="stderr line\n")

    with patch("autoloop.preflight.subprocess.run", fake_run):
        from autoloop.preflight import run_preflight

        result = run_preflight(_cfg())

    assert "stdout line" in result["verify_cmd"]["output"]
    assert "stderr line" in result["verify_cmd"]["output"]


def test_run_preflight_with_ctx_passes_cwd(repo_ctx):
    """Verify that run_preflight passes ctx.repo_dir as cwd to subprocess.run."""
    cwd_values = []

    def fake_run(cmd, **kwargs):
        cwd_values.append(kwargs.get("cwd"))
        return _ok(stdout="ok\n")

    with patch("autoloop.preflight.subprocess.run", fake_run):
        from autoloop.preflight import run_preflight

        run_preflight(_cfg(), repo_ctx)

    assert all(cwd == repo_ctx.repo_dir for cwd in cwd_values)


def test_run_preflight_with_ctx_resolves_under_tmp_path(repo_ctx):
    """Verify commands execute under ctx.repo_dir, not the real cwd."""
    captured_cwds = []

    def fake_run(cmd, **kwargs):
        captured_cwds.append(kwargs.get("cwd"))
        return _ok(stdout="ok\n")

    with patch("autoloop.preflight.subprocess.run", fake_run):
        from autoloop.preflight import run_preflight

        run_preflight(_cfg(), repo_ctx)

    for cwd in captured_cwds:
        assert cwd is not None
        assert str(cwd).startswith(str(repo_ctx.repo_dir))
