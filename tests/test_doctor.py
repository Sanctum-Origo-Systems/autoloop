"""Tests for autoloop doctor check runner."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from autoloop.doctor import (
    Check,
    Result,
    check_autoloop_toml,
    check_claude_cli_authenticated,
    check_claude_cli_installed,
    check_claude_session,
    check_claude_settings,
    check_gh_cli_installed,
    check_jev,
    check_verify_cmd,
    get_checks,
    run_checks,
)


def test_run_checks_all_pass(capsys):
    checks = [
        Check(name="alpha", fn=lambda: (True, "ok"), fix_hint="fix alpha"),
        Check(name="beta", fn=lambda: (True, "good"), fix_hint="fix beta"),
    ]
    results = run_checks(checks)

    assert len(results) == 2
    assert all(r.passed for r in results)
    assert results[0].name == "alpha"
    assert results[1].name == "beta"

    out = capsys.readouterr().out
    assert "✓" in out
    assert "✗" not in out


def test_run_checks_one_fails(capsys):
    checks = [
        Check(name="good", fn=lambda: (True, "fine"), fix_hint="n/a"),
        Check(name="bad", fn=lambda: (False, "broken"), fix_hint="run repair"),
    ]
    results = run_checks(checks)

    assert results[0].passed is True
    assert results[1].passed is False
    assert results[1].fix_hint == "run repair"

    out = capsys.readouterr().out
    assert "✓" in out
    assert "✗" in out
    assert "hint: run repair" in out


def test_run_checks_exception_in_fn(capsys):
    def boom():
        raise RuntimeError("unexpected error")

    checks = [
        Check(name="exploder", fn=boom, fix_hint="check logs"),
    ]
    results = run_checks(checks)

    assert len(results) == 1
    assert results[0].passed is False
    assert "unexpected error" in results[0].message

    out = capsys.readouterr().out
    assert "✗" in out
    assert "hint: check logs" in out


def test_run_checks_empty(capsys):
    results = run_checks([])

    assert results == []
    out = capsys.readouterr().out
    assert "No checks registered." in out


def test_result_dataclass():
    r = Result(name="test", passed=True, message="ok", fix_hint="none")
    assert r.name == "test"
    assert r.passed is True
    assert r.message == "ok"
    assert r.fix_hint == "none"


# --- autoloop.toml check ---


def test_check_autoloop_toml_exists_and_valid(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n')
    passed, msg = check_autoloop_toml(repo_ctx)
    assert passed is True
    assert "found and valid" in msg


def test_check_autoloop_toml_missing(repo_ctx):
    passed, msg = check_autoloop_toml(repo_ctx)
    assert passed is False
    assert "not found" in msg


def test_check_autoloop_toml_invalid_syntax(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text("[invalid toml\nno closing bracket")
    passed, msg = check_autoloop_toml(repo_ctx)
    assert passed is False
    assert "invalid" in msg


# --- .claude/settings.json check ---


def test_check_claude_settings_exists(repo_ctx):
    settings_dir = repo_ctx.repo_dir / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{}")
    passed, msg = check_claude_settings(repo_ctx)
    assert passed is True
    assert "found" in msg


def test_check_claude_settings_missing(repo_ctx):
    passed, msg = check_claude_settings(repo_ctx)
    assert passed is False
    assert "not found" in msg


# --- get_checks integration ---


def test_get_checks_returns_registered_checks(repo_ctx):
    checks = get_checks(repo_ctx)
    assert len(checks) == 8
    assert checks[0].name == "autoloop.toml"
    assert checks[1].name == ".claude/settings.json"
    assert checks[2].name == "claude CLI installed"
    assert checks[3].name == "claude CLI authenticated"
    assert checks[4].name == "gh CLI installed and authenticated"
    assert checks[5].name == "Claude Code session conflict"
    assert checks[6].name == "verify_cmd"
    assert checks[7].name == "Jev config"
    assert "autoloop init" in checks[0].fix_hint
    assert "autoloop init" in checks[1].fix_hint
    assert "autoloop doctor" in checks[6].fix_hint


def test_get_checks_all_pass(repo_ctx, capsys):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\nverify_cmd = "true"\n')
    settings_dir = repo_ctx.repo_dir / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{}")

    checks = get_checks(repo_ctx)
    results = run_checks(checks)

    assert len(results) == 8
    file_results = [r for r in results if r.name in ("autoloop.toml", ".claude/settings.json")]
    assert all(r.passed for r in file_results)
    out = capsys.readouterr().out
    assert "✓" in out


def test_get_checks_all_fail(repo_ctx, capsys):
    checks = get_checks(repo_ctx)
    results = run_checks(checks)

    assert len(results) == 8
    out = capsys.readouterr().out
    assert "✗" in out


def test_get_checks_resolves_paths_under_ctx(repo_ctx):
    """Verify that get_checks resolves file checks under ctx.repo_dir, not Path.cwd()."""
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n')
    settings_dir = repo_ctx.repo_dir / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{}")

    checks = get_checks(repo_ctx)
    toml_check = checks[0]
    settings_check = checks[1]

    passed_toml, _ = toml_check.fn()
    passed_settings, _ = settings_check.fn()

    assert passed_toml is True
    assert passed_settings is True


# --- claude CLI installed check ---


def test_check_claude_cli_installed_success():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="v2.1.160\n", stderr="")
    with patch("autoloop.doctor.subprocess.run", return_value=completed):
        passed, msg = check_claude_cli_installed()
    assert passed is True
    assert "v2.1.160" in msg


def test_check_claude_cli_installed_not_found():
    with patch("autoloop.doctor.subprocess.run", side_effect=FileNotFoundError):
        passed, msg = check_claude_cli_installed()
    assert passed is False
    assert "not found" in msg


def test_check_claude_cli_installed_nonzero_exit():
    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="error")
    with patch("autoloop.doctor.subprocess.run", return_value=completed):
        passed, msg = check_claude_cli_installed()
    assert passed is False
    assert "failed to run" in msg


def test_check_claude_cli_installed_version_on_stderr():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="v2.1.160")
    with patch("autoloop.doctor.subprocess.run", return_value=completed):
        passed, msg = check_claude_cli_installed()
    assert passed is True
    assert "v2.1.160" in msg


# --- claude CLI authenticated check ---


def test_check_claude_cli_authenticated_success():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="Logged in\n", stderr="")
    with patch("autoloop.doctor.subprocess.run", return_value=completed):
        passed, msg = check_claude_cli_authenticated()
    assert passed is True
    assert "authenticated" in msg


def test_check_claude_cli_authenticated_not_found():
    with patch("autoloop.doctor.subprocess.run", side_effect=FileNotFoundError):
        passed, msg = check_claude_cli_authenticated()
    assert passed is False
    assert "not found" in msg


def test_check_claude_cli_authenticated_not_authed():
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="not logged in"
    )
    with patch("autoloop.doctor.subprocess.run", return_value=completed):
        passed, msg = check_claude_cli_authenticated()
    assert passed is False
    assert "not authenticated" in msg


# --- gh CLI installed and authenticated check ---


def test_check_gh_cli_installed_success():
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Logged in to github.com\n", stderr=""
    )
    with patch("autoloop.doctor.subprocess.run", return_value=completed):
        passed, msg = check_gh_cli_installed()
    assert passed is True
    assert "installed and authenticated" in msg


def test_check_gh_cli_not_found():
    with patch("autoloop.doctor.subprocess.run", side_effect=FileNotFoundError):
        passed, msg = check_gh_cli_installed()
    assert passed is False
    assert "not found" in msg


def test_check_gh_cli_not_authenticated():
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="not logged in"
    )
    with patch("autoloop.doctor.subprocess.run", return_value=completed):
        passed, msg = check_gh_cli_installed()
    assert passed is False
    assert "not authenticated" in msg


# --- Claude Code session conflict check ---


def test_check_claude_session_no_conflict(repo_ctx):
    with patch("autoloop.implement_issue.detect_active_claude_session", return_value=False):
        passed, msg = check_claude_session(repo_ctx)
    assert passed is True
    assert "No active Claude Code session conflict" in msg


def test_check_claude_session_conflict_detected(repo_ctx):
    with patch("autoloop.implement_issue.detect_active_claude_session", return_value=True):
        passed, msg = check_claude_session(repo_ctx)
    assert passed is False
    assert "Active Claude Code session detected" in msg


def test_check_claude_session_detection_unavailable(repo_ctx):
    with patch("autoloop.implement_issue.detect_active_claude_session", return_value=None):
        passed, msg = check_claude_session(repo_ctx)
    assert passed is True
    assert "unavailable" in msg


# --- verify_cmd check ---


def test_check_verify_cmd_passes(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\nverify_cmd = "true"\n')
    passed, msg = check_verify_cmd(repo_ctx)
    assert passed is True
    assert "passes" in msg
    assert "exit 0" in msg


def test_check_verify_cmd_fails_with_output(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text(
        'repo = "acme/widgets"\nverify_cmd = "echo build-error-output >&2 && exit 1"\n'
    )
    passed, msg = check_verify_cmd(repo_ctx)
    assert passed is False
    assert "failed" in msg
    assert "exit 1" in msg
    assert "build-error-output" in msg


def test_check_verify_cmd_truncates_long_output(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    long_msg = "x" * 800
    toml_file.write_text(f'repo = "acme/widgets"\nverify_cmd = "echo {long_msg} && exit 1"\n')
    passed, msg = check_verify_cmd(repo_ctx)
    assert passed is False
    output_line = [line for line in msg.split("\n") if "Output:" in line][0]
    output_text = output_line.split("Output: ", 1)[1]
    assert len(output_text) <= 500


def test_check_verify_cmd_not_found(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\nverify_cmd = "nonexistent_command_abc123"\n')
    passed, msg = check_verify_cmd(repo_ctx)
    assert passed is False
    assert "nonexistent_command_abc123" in msg


def test_check_verify_cmd_empty(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\nverify_cmd = ""\n')
    passed, msg = check_verify_cmd(repo_ctx)
    assert passed is True
    assert "skipping" in msg


def test_check_verify_cmd_not_set(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\nverify_cmd = "  "\n')
    passed, msg = check_verify_cmd(repo_ctx)
    assert passed is True
    assert "skipping" in msg


def test_check_verify_cmd_no_config(repo_ctx):
    passed, msg = check_verify_cmd(repo_ctx)
    assert passed is False
    assert "could not load" in msg


# --- Jev config check ---


def test_check_jev_disabled(repo_ctx, monkeypatch):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n')
    monkeypatch.delenv("JEV_MODE", raising=False)
    passed, msg = check_jev(repo_ctx)
    assert passed is True
    assert "disabled" in msg


def test_check_jev_shadow_mode_api_key_set(repo_ctx, monkeypatch):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n\n[jev]\nmode = "shadow"\n')
    monkeypatch.delenv("JEV_MODE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-123")
    passed, msg = check_jev(repo_ctx)
    assert passed is True
    assert "shadow mode" in msg
    assert "API key set" in msg
    assert "MISSING" not in msg


def test_check_jev_shadow_mode_api_key_missing(repo_ctx, monkeypatch):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n\n[jev]\nmode = "shadow"\n')
    monkeypatch.delenv("JEV_MODE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    passed, msg = check_jev(repo_ctx)
    assert passed is False
    assert "shadow mode" in msg
    assert "API key MISSING" in msg
    assert "OPENROUTER_API_KEY" in msg


def test_check_jev_gate_mode_api_key_set(repo_ctx, monkeypatch):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n\n[jev]\nmode = "gate"\n')
    monkeypatch.delenv("JEV_MODE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-123")
    passed, msg = check_jev(repo_ctx)
    assert passed is True
    assert "gate mode" in msg
    assert "API key set" in msg


def test_check_jev_custom_api_key_env(repo_ctx, monkeypatch):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text(
        'repo = "acme/widgets"\n\n[jev]\nmode = "shadow"\napi_key_env = "MY_CUSTOM_KEY"\n'
    )
    monkeypatch.delenv("JEV_MODE", raising=False)
    monkeypatch.delenv("MY_CUSTOM_KEY", raising=False)
    passed, msg = check_jev(repo_ctx)
    assert passed is False
    assert "MY_CUSTOM_KEY" in msg


def test_check_jev_no_config(repo_ctx):
    passed, msg = check_jev(repo_ctx)
    assert passed is False
    assert "could not load" in msg
