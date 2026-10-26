"""Tests for autoloop doctor check runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autoloop.claude_runner import ClaudeResult
from autoloop.doctor import (
    PROBE_FILE,
    PROBE_REMOTE,
    WARN,
    Check,
    Result,
    check_autoloop_toml,
    check_claude_cli_authenticated,
    check_claude_cli_installed,
    check_claude_session,
    check_claude_settings,
    check_claude_settings_flag,
    check_gh_cli_installed,
    check_jev,
    check_live_probe,
    check_main_settings,
    check_lockfile_ignored,
    check_verify_cmd,
    check_worktree_isolation,
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


def test_run_checks_warn_passes_with_hint(capsys):
    checks = [Check(name="soft", fn=lambda: (WARN, "meh"), fix_hint="tidy up")]
    results = run_checks(checks)

    assert results[0].passed is True
    assert results[0].warned is True
    out = capsys.readouterr().out
    assert "!" in out
    assert "✗" not in out
    assert "hint: tidy up" in out


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
    assert len(checks) == 11
    assert checks[0].name == "autoloop.toml"
    assert checks[1].name == ".claude/settings.json"
    assert checks[2].name == "claude CLI installed"
    assert checks[3].name == "claude CLI authenticated"
    assert checks[4].name == "gh CLI installed and authenticated"
    assert checks[5].name == "Claude Code session conflict"
    assert checks[6].name == "verify_cmd"
    assert checks[7].name == "Jev config"
    assert checks[8].name == ".autoloop.lock gitignored"
    assert checks[9].name == "origin/main settings"
    assert checks[10].name == "claude --settings support"
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

    assert len(results) == 11
    file_results = [r for r in results if r.name in ("autoloop.toml", ".claude/settings.json")]
    assert all(r.passed for r in file_results)
    out = capsys.readouterr().out
    assert "✓" in out


def test_get_checks_all_fail(repo_ctx, capsys):
    checks = get_checks(repo_ctx)
    results = run_checks(checks)

    assert len(results) == 11
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


# --- Worktree isolation check ---


def test_check_worktree_isolation_enabled(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n\n[implement]\nisolation = "worktree"\n')
    passed, msg = check_worktree_isolation(repo_ctx)
    assert passed is True
    assert "Worktree isolation: enabled" in msg
    assert str(repo_ctx.worktree_dir / "autoloop/0-doctor-probe") in msg


def test_check_worktree_isolation_enabled_is_dry_run(repo_ctx):
    """The doctor check creates no worktree and runs no git commands."""
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n\n[implement]\nisolation = "worktree"\n')
    with patch("autoloop.implement_issue.subprocess.run") as mock_run:
        passed, _ = check_worktree_isolation(repo_ctx)
    assert passed is True
    mock_run.assert_not_called()
    assert not repo_ctx.worktree_dir.exists()


def test_check_worktree_isolation_fails_when_path_not_under_worktree_dir(repo_ctx):
    """A resolved path outside ctx.worktree_dir is reported as a failure naming the mismatch."""
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n\n[implement]\nisolation = "worktree"\n')
    with patch(
        "autoloop.implement_issue.resolve_working_dir",
        return_value=repo_ctx.repo_dir,
    ):
        passed, msg = check_worktree_isolation(repo_ctx)
    assert passed is False
    assert str(repo_ctx.repo_dir) in msg
    assert str(repo_ctx.worktree_dir) in msg
    assert "not under" in msg


def test_check_worktree_isolation_fails_when_resolution_raises(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n\n[implement]\nisolation = "worktree"\n')
    with patch(
        "autoloop.implement_issue.resolve_working_dir",
        side_effect=RuntimeError("boom"),
    ):
        passed, msg = check_worktree_isolation(repo_ctx)
    assert passed is False
    assert "resolution failed" in msg
    assert "boom" in msg


def test_check_worktree_isolation_off(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n')
    with patch("autoloop.implement_issue.resolve_working_dir") as mock_resolve:
        passed, msg = check_worktree_isolation(repo_ctx)
    assert passed is True
    assert msg == "Worktree isolation: disabled"
    mock_resolve.assert_not_called()


def test_get_checks_includes_worktree_when_enabled(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n\n[implement]\nisolation = "worktree"\n')
    checks = get_checks(repo_ctx)
    names = [c.name for c in checks]
    assert "Worktree isolation" in names


def test_get_checks_excludes_worktree_when_off(repo_ctx):
    toml_file = repo_ctx.repo_dir / "autoloop.toml"
    toml_file.write_text('repo = "acme/widgets"\n')
    checks = get_checks(repo_ctx)
    names = [c.name for c in checks]
    assert "Worktree isolation" not in names


# --- origin/main settings check ---


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_check_main_settings_pass(repo_ctx):
    settings = json.dumps({"permissions": {"allow": ["Read", "Edit"]}})
    with patch("autoloop.doctor.subprocess.run", return_value=_completed(settings)) as mock_run:
        passed, msg = check_main_settings(repo_ctx)
    assert passed is True
    assert "2 allow rules" in msg
    cmd = mock_run.call_args[0][0]
    assert cmd == [
        "git",
        "-C",
        str(repo_ctx.repo_dir),
        "show",
        "origin/main:.claude/settings.json",
    ]


def test_check_main_settings_fails_on_git_error(repo_ctx):
    completed = _completed(returncode=128, stderr="fatal: invalid object name 'origin/main'")
    with patch("autoloop.doctor.subprocess.run", return_value=completed):
        passed, msg = check_main_settings(repo_ctx)
    assert passed is False
    assert "invalid object name" in msg
    assert "no permission rules" in msg


def test_check_main_settings_fails_when_git_missing(repo_ctx):
    with patch("autoloop.doctor.subprocess.run", side_effect=FileNotFoundError):
        passed, msg = check_main_settings(repo_ctx)
    assert passed is False
    assert "no permission rules" in msg


def test_check_main_settings_fails_on_invalid_json(repo_ctx):
    with patch("autoloop.doctor.subprocess.run", return_value=_completed("{not json")):
        passed, msg = check_main_settings(repo_ctx)
    assert passed is False
    assert "not valid JSON" in msg
    assert "no permission rules" in msg


def test_check_main_settings_warns_on_empty_allow(repo_ctx):
    settings = json.dumps({"permissions": {"allow": [], "deny": ["Bash(git push*)"]}})
    with patch("autoloop.doctor.subprocess.run", return_value=_completed(settings)):
        passed, msg = check_main_settings(repo_ctx)
    assert passed == WARN
    assert "permissions.allow" in msg


def test_check_main_settings_warns_on_missing_permissions(repo_ctx):
    with patch("autoloop.doctor.subprocess.run", return_value=_completed("{}")):
        passed, msg = check_main_settings(repo_ctx)
    assert passed == WARN
    assert "permissions.allow" in msg


# --- claude --settings support check ---


def test_check_claude_settings_flag_pass():
    help_text = "Options:\n  --settings <file-or-json>  Load additional settings\n"
    with patch("autoloop.doctor.subprocess.run", return_value=_completed(help_text)) as mock_run:
        passed, msg = check_claude_settings_flag()
    assert passed is True
    assert "--settings" in msg
    assert mock_run.call_args[0][0] == ["claude", "--help"]


def test_check_claude_settings_flag_missing_names_version():
    responses = [_completed("Options:\n  --model <model>\n"), _completed("1.0.0 (Claude Code)\n")]
    with patch("autoloop.doctor.subprocess.run", side_effect=responses) as mock_run:
        passed, msg = check_claude_settings_flag()
    assert passed is False
    assert "1.0.0 (Claude Code)" in msg
    assert "--settings" in msg
    assert mock_run.call_args_list[1][0][0] == ["claude", "--version"]


def test_check_claude_settings_flag_cli_not_found():
    with patch("autoloop.doctor.subprocess.run", side_effect=FileNotFoundError):
        passed, msg = check_claude_settings_flag()
    assert passed is False
    assert "not found" in msg


def test_check_claude_settings_flag_help_fails():
    with patch("autoloop.doctor.subprocess.run", return_value=_completed(returncode=1)):
        passed, msg = check_claude_settings_flag()
    assert passed is False
    assert "failed" in msg


# --- live probe gating ---


def test_get_checks_excludes_live_probe_by_default(repo_ctx):
    names = [c.name for c in get_checks(repo_ctx)]
    assert "Live permission probe" not in names


def test_get_checks_includes_live_probe_when_requested(repo_ctx):
    checks = get_checks(repo_ctx, live=True)
    assert checks[-1].name == "Live permission probe"


def test_default_doctor_run_makes_no_claude_p_call(repo_ctx):
    (repo_ctx.repo_dir / "autoloop.toml").write_text('repo = "acme/widgets"\n')
    with (
        patch("autoloop.doctor.subprocess.run", return_value=_completed("--settings")) as mock_run,
        patch("autoloop.doctor.run_claude") as mock_claude,
    ):
        run_checks(get_checks(repo_ctx))
    mock_claude.assert_not_called()
    for call in mock_run.call_args_list:
        cmd = call[0][0]
        assert not (isinstance(cmd, list) and cmd[:2] == ["claude", "-p"])


def test_cli_doctor_passes_live_flag(tmp_path, monkeypatch):
    from autoloop.cli import main

    monkeypatch.chdir(tmp_path)
    cases = [(["autoloop", "doctor"], False), (["autoloop", "doctor", "--live"], True)]
    for argv, expected in cases:
        with (
            patch("sys.argv", argv),
            patch("autoloop.doctor.get_checks", return_value=[]) as mock_get,
            patch("autoloop.doctor.run_checks", return_value=[]),
        ):
            main()
        assert mock_get.call_args.kwargs["live"] is expected


# --- live probe ---


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def probe_repo(repo_ctx, tmp_path_factory, monkeypatch):
    """A real repo at repo_ctx.repo_dir whose origin/main has .claude/settings.json."""
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "Test Bot")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "bot@example.com")
    origin = tmp_path_factory.mktemp("origin") / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], capture_output=True, check=True
    )
    repo = repo_ctx.repo_dir
    _git(repo, "init", "-b", "main")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Write"]}}')
    _git(repo, "add", ".claude/settings.json")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")
    return repo_ctx


def _assert_probe_cleaned_up(repo: Path, worktree: Path) -> None:
    assert not worktree.exists()
    assert str(worktree) not in _git(repo, "worktree", "list")
    assert _git(repo, "branch", "--list", "autoloop/doctor-probe-*").strip() == ""


def _probe_result(denials, cost=0.0123):
    return ClaudeResult("done", cost, 10, 20, 0, 0, success=True, permission_denials=denials)


_PUSH_DENIAL = {"tool_name": "Bash", "tool_input": {"command": f"git push {PROBE_REMOTE} HEAD"}}


def _commit_probe_file(repo_dir: Path) -> None:
    (repo_dir / PROBE_FILE).write_text("probe\n")
    _git(repo_dir, "add", PROBE_FILE)
    _git(repo_dir, "commit", "-m", "probe")


def test_live_probe_passes_and_cleans_up(probe_repo):
    seen = {}

    def fake_run_claude(prompt, model, timeout, *, repo_dir):
        seen["worktree"] = repo_dir
        branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
        assert branch.startswith("autoloop/doctor-probe-")
        _commit_probe_file(repo_dir)
        return _probe_result([_PUSH_DENIAL])

    with patch("autoloop.doctor.run_claude", side_effect=fake_run_claude) as mock_claude:
        passed, msg = check_live_probe(probe_repo)

    assert passed is True
    assert "$0.0123" in msg
    assert PROBE_REMOTE in mock_claude.call_args[0][0]
    assert not seen["worktree"].is_relative_to(probe_repo.repo_dir)
    _assert_probe_cleaned_up(probe_repo.repo_dir, seen["worktree"])


def test_live_probe_cleans_up_on_exception(probe_repo):
    seen = {}

    def boom(prompt, model, timeout, *, repo_dir):
        seen["worktree"] = repo_dir
        raise RuntimeError("claude exploded")

    with patch("autoloop.doctor.run_claude", side_effect=boom):
        with pytest.raises(RuntimeError, match="claude exploded"):
            check_live_probe(probe_repo)

    _assert_probe_cleaned_up(probe_repo.repo_dir, seen["worktree"])


def test_live_probe_fails_when_push_not_denied(probe_repo):
    seen = {}

    def fake_run_claude(prompt, model, timeout, *, repo_dir):
        seen["worktree"] = repo_dir
        _commit_probe_file(repo_dir)
        return _probe_result([])

    with patch("autoloop.doctor.run_claude", side_effect=fake_run_claude):
        passed, msg = check_live_probe(probe_repo)

    assert passed is False
    assert "push not in permission_denials" in msg
    assert "$0.0123" in msg
    _assert_probe_cleaned_up(probe_repo.repo_dir, seen["worktree"])


def test_live_probe_fails_when_nothing_written(probe_repo):
    with patch("autoloop.doctor.run_claude", return_value=_probe_result([_PUSH_DENIAL])):
        passed, msg = check_live_probe(probe_repo)

    assert passed is False
    assert "probe file not written" in msg
    assert "probe commit not made" in msg


def test_live_probe_fails_when_claude_fails(probe_repo):
    failed = ClaudeResult("", 0, 0, 0, 0, 0, success=False, timed_out=True)
    with patch("autoloop.doctor.run_claude", return_value=failed):
        passed, msg = check_live_probe(probe_repo)

    assert passed is False
    assert "timed out" in msg


def test_live_probe_fails_without_origin_main(repo_ctx):
    mock_claude = MagicMock()
    with patch("autoloop.doctor.run_claude", mock_claude):
        passed, msg = check_live_probe(repo_ctx)

    assert passed is False
    assert "could not create probe worktree" in msg
    mock_claude.assert_not_called()


# --- .autoloop.lock gitignore check ---


def test_check_lockfile_ignored_warns_when_not_ignored(repo_ctx):
    _git(repo_ctx.repo_dir, "init", "-q")
    passed, msg = check_lockfile_ignored(repo_ctx)
    assert passed is False
    assert ".autoloop.lock" in msg
    assert ".gitignore" in msg


def test_check_lockfile_ignored_warns_when_tracked(repo_ctx):
    _git(repo_ctx.repo_dir, "init", "-q")
    (repo_ctx.repo_dir / ".autoloop.lock").write_text("12345")
    _git(repo_ctx.repo_dir, "add", ".autoloop.lock")
    (repo_ctx.repo_dir / ".gitignore").write_text(".autoloop.lock\n")
    passed, msg = check_lockfile_ignored(repo_ctx)
    assert passed is False
    assert ".autoloop.lock" in msg
    assert "git rm --cached .autoloop.lock" in msg


def test_check_lockfile_ignored_passes_when_ignored_and_untracked(repo_ctx):
    _git(repo_ctx.repo_dir, "init", "-q")
    (repo_ctx.repo_dir / ".gitignore").write_text(".autoloop.lock\n")
    (repo_ctx.repo_dir / ".autoloop.lock").write_text("12345")
    passed, msg = check_lockfile_ignored(repo_ctx)
    assert passed is True
    assert "ignored and untracked" in msg


def test_check_lockfile_ignored_no_lock_warning_in_doctor_output(repo_ctx, capsys):
    _git(repo_ctx.repo_dir, "init", "-q")
    (repo_ctx.repo_dir / ".gitignore").write_text(".autoloop.lock\n")
    checks = [c for c in get_checks(repo_ctx) if c.name == ".autoloop.lock gitignored"]
    results = run_checks(checks)
    assert results[0].passed is True
    assert "✗" not in capsys.readouterr().out


def test_check_lockfile_ignored_skips_when_git_missing(repo_ctx):
    with patch("autoloop.doctor.subprocess.run", side_effect=FileNotFoundError):
        passed, msg = check_lockfile_ignored(repo_ctx)
    assert passed is True
    assert "skipping" in msg


def test_check_lockfile_ignored_skips_outside_git_repo(repo_ctx):
    passed, msg = check_lockfile_ignored(repo_ctx)
    assert passed is True
    assert "skipping" in msg
