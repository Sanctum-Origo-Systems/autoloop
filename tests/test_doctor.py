"""Tests for autoloop doctor command."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from autoloop.doctor import (
    FAIL,
    PASS,
    check_claude_cli,
    check_claude_settings,
    check_config,
    check_gh_cli,
    check_no_active_session,
    check_protected_paths,
    check_test_pattern,
    check_verify_cmd,
    run_doctor,
)


def _cfg(**overrides):
    defaults = {
        "verify_cmd": "echo ok",
        "test_timeout": 120,
        "test_pattern": "tests/*.py",
        "protected_paths": ["autoloop/"],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _ok(stdout="", stderr="", returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# --- check_config ---


def test_check_config_valid(tmp_path):
    toml = tmp_path / "autoloop.toml"
    toml.write_text('repo = "acme-corp/widget"\nverify_cmd = "echo ok"\n')
    passed, msg = check_config(toml)
    assert passed is True
    assert PASS in msg
    assert "autoloop.toml found and valid" in msg


def test_check_config_missing(tmp_path):
    passed, msg = check_config(tmp_path / "missing.toml")
    assert passed is False
    assert FAIL in msg
    assert "not found" in msg
    assert "autoloop init" in msg


def test_check_config_invalid_toml(tmp_path):
    toml = tmp_path / "autoloop.toml"
    toml.write_text("this is not valid [[[toml")
    passed, msg = check_config(toml)
    assert passed is False
    assert FAIL in msg
    assert "errors" in msg


# --- check_claude_settings ---


def test_check_claude_settings_found(tmp_path):
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    settings_file = settings_dir / "settings.json"
    settings_file.write_text(json.dumps({"permissions": {"allow": ["a", "b", "c"]}}))
    passed, msg = check_claude_settings(tmp_path)
    assert passed is True
    assert PASS in msg
    assert "3 permissions" in msg


def test_check_claude_settings_missing(tmp_path):
    passed, msg = check_claude_settings(tmp_path)
    assert passed is False
    assert FAIL in msg
    assert "not found" in msg
    assert "autoloop init" in msg


def test_check_claude_settings_invalid_json(tmp_path):
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{bad json")
    passed, msg = check_claude_settings(tmp_path)
    assert passed is False
    assert FAIL in msg
    assert "invalid" in msg


def test_check_claude_settings_no_permissions_key(tmp_path):
    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text("{}")
    passed, msg = check_claude_settings(tmp_path)
    assert passed is True
    assert "0 permissions" in msg


# --- check_claude_cli ---


def test_check_claude_cli_installed_and_authed():
    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _ok(stdout="v2.1.160")
        if "--print-api-key" in cmd:
            return _ok(stdout="sk-ant-xxx")
        return _ok()

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_claude_cli()
    assert passed is True
    assert PASS in msg
    assert "v2.1.160" in msg
    assert "authenticated" in msg


def test_check_claude_cli_not_installed():
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude not found")

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_claude_cli()
    assert passed is False
    assert FAIL in msg
    assert "not installed" in msg


def test_check_claude_cli_not_authed():
    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _ok(stdout="v2.1.160")
        if "--print-api-key" in cmd:
            return _ok(returncode=1, stderr="not authenticated")
        return _ok()

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_claude_cli()
    assert passed is False
    assert FAIL in msg
    assert "not authenticated" in msg


def test_check_claude_cli_version_fails():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1, stderr="something broke")

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_claude_cli()
    assert passed is False
    assert FAIL in msg
    assert "failed" in msg


def test_check_claude_cli_timeout():
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=10)

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_claude_cli()
    assert passed is False
    assert FAIL in msg
    assert "timed out" in msg


def test_check_claude_cli_auth_timeout():
    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _ok(stdout="v2.1.160")
        raise subprocess.TimeoutExpired(cmd="claude", timeout=10)

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_claude_cli()
    assert passed is False
    assert FAIL in msg
    assert "timed out" in msg


# --- check_gh_cli ---


def test_check_gh_cli_installed_and_authed():
    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _ok(stdout="gh version 2.48.0 (2024-03-13)")
        if "auth" in cmd:
            return _ok(stdout="Logged in to github.com")
        return _ok()

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_gh_cli()
    assert passed is True
    assert PASS in msg
    assert "authenticated" in msg


def test_check_gh_cli_not_installed():
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("gh not found")

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_gh_cli()
    assert passed is False
    assert FAIL in msg
    assert "not installed" in msg


def test_check_gh_cli_not_authed():
    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _ok(stdout="gh version 2.48.0")
        if "auth" in cmd:
            return _ok(returncode=1, stderr="not logged in")
        return _ok()

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_gh_cli()
    assert passed is False
    assert FAIL in msg
    assert "not authenticated" in msg


def test_check_gh_cli_version_fails():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1, stderr="broken")

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_gh_cli()
    assert passed is False
    assert FAIL in msg
    assert "failed" in msg


def test_check_gh_cli_timeout():
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=10)

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_gh_cli()
    assert passed is False
    assert FAIL in msg
    assert "timed out" in msg


def test_check_gh_cli_auth_timeout():
    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _ok(stdout="gh version 2.48.0")
        raise subprocess.TimeoutExpired(cmd="gh", timeout=10)

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_gh_cli()
    assert passed is False
    assert FAIL in msg
    assert "timed out" in msg


# --- check_no_active_session ---


def test_check_no_active_session_clean(tmp_path):
    passed, msg = check_no_active_session(tmp_path)
    assert passed is True
    assert PASS in msg
    assert "No active" in msg


def test_check_no_active_session_locked(tmp_path):
    (tmp_path / ".autoloop.lock").write_text("")
    passed, msg = check_no_active_session(tmp_path)
    assert passed is False
    assert FAIL in msg
    assert ".autoloop.lock" in msg


# --- check_protected_paths ---


def test_check_protected_paths_valid(tmp_path):
    (tmp_path / "autoloop").mkdir()
    cfg = _cfg(protected_paths=["autoloop/"])
    passed, msg = check_protected_paths(cfg, tmp_path)
    assert passed is True
    assert PASS in msg


def test_check_protected_paths_invalid(tmp_path):
    cfg = _cfg(protected_paths=["nonexistent/"])
    passed, msg = check_protected_paths(cfg, tmp_path)
    assert passed is False
    assert FAIL in msg
    assert "nonexistent/" in msg


def test_check_protected_paths_multiple_invalid(tmp_path):
    cfg = _cfg(protected_paths=["a/", "b/"])
    passed, msg = check_protected_paths(cfg, tmp_path)
    assert passed is False
    assert "a/" in msg
    assert "b/" in msg


def test_check_protected_paths_partial_valid(tmp_path):
    (tmp_path / "autoloop").mkdir()
    cfg = _cfg(protected_paths=["autoloop/", "missing/"])
    passed, msg = check_protected_paths(cfg, tmp_path)
    assert passed is False
    assert "missing/" in msg


# --- check_verify_cmd ---


def test_check_verify_cmd_passes():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="all passed\n")

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_verify_cmd(_cfg())
    assert passed is True
    assert PASS in msg
    assert "exit 0" in msg


def test_check_verify_cmd_fails():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1, stdout="FAILED\n", stderr="error detail\n")

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_verify_cmd(_cfg())
    assert passed is False
    assert FAIL in msg
    assert "exit 1" in msg
    assert "FAILED" in msg
    assert "error detail" in msg


def test_check_verify_cmd_empty():
    passed, msg = check_verify_cmd(_cfg(verify_cmd=""))
    assert passed is True
    assert "skipped" in msg


def test_check_verify_cmd_timeout():
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_verify_cmd(_cfg())
    assert passed is False
    assert FAIL in msg
    assert "timed out" in msg


def test_check_verify_cmd_not_found():
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("not found")

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_verify_cmd(_cfg(verify_cmd="nonexistent-tool test"))
    assert passed is False
    assert FAIL in msg
    assert "not found" in msg


def test_check_verify_cmd_output_includes_both_streams():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1, stdout="stdout line\n", stderr="stderr line\n")

    with patch("autoloop.doctor.subprocess.run", fake_run):
        passed, msg = check_verify_cmd(_cfg())
    assert "stdout line" in msg
    assert "stderr line" in msg


# --- check_test_pattern ---


def test_check_test_pattern_matches(tmp_path):
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text("")
    (tests_dir / "test_bar.py").write_text("")
    cfg = _cfg(test_pattern="tests/*.py")
    passed, msg = check_test_pattern(cfg, tmp_path)
    assert passed is True
    assert PASS in msg
    assert "2 file(s)" in msg


def test_check_test_pattern_no_matches(tmp_path):
    cfg = _cfg(test_pattern="tests/*.py")
    passed, msg = check_test_pattern(cfg, tmp_path)
    assert passed is False
    assert FAIL in msg
    assert "matches no files" in msg


def test_check_test_pattern_empty():
    cfg = _cfg(test_pattern="")
    passed, msg = check_test_pattern(cfg)
    assert passed is True
    assert "skipped" in msg


# --- run_doctor ---


def test_run_doctor_all_pass(tmp_path):
    toml = tmp_path / "autoloop.toml"
    toml.write_text('repo = "acme-corp/widget"\nverify_cmd = "echo ok"\ntest_pattern = ""\n')

    settings_dir = tmp_path / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text('{"permissions": {"allow": []}}')

    (tmp_path / "autoloop").mkdir()

    def fake_subprocess(cmd, **kwargs):
        if isinstance(cmd, list) and "--version" in cmd:
            return _ok(stdout="v1.0")
        if isinstance(cmd, list) and "--print-api-key" in cmd:
            return _ok(stdout="sk-ant-xxx")
        if isinstance(cmd, list) and "auth" in cmd:
            return _ok(stdout="Logged in")
        return _ok(stdout="ok\n")

    with patch("autoloop.doctor.subprocess.run", fake_subprocess):
        results = run_doctor(config_path=toml, repo_dir=tmp_path, run_verify=False)

    for passed, msg in results:
        assert passed is True, f"Check failed: {msg}"


def test_run_doctor_config_missing_stops_early(tmp_path):
    results = run_doctor(config_path=tmp_path / "missing.toml", repo_dir=tmp_path)
    assert len(results) == 1
    assert results[0][0] is False
    assert "not found" in results[0][1]


def test_run_doctor_returns_failures(tmp_path):
    toml = tmp_path / "autoloop.toml"
    toml.write_text(
        'repo = "acme-corp/widget"\nverify_cmd = "echo ok"\n'
        'test_pattern = ""\nprotected_paths = []\n'
    )

    def fake_subprocess(cmd, **kwargs):
        if isinstance(cmd, list) and "--version" in cmd:
            return _ok(stdout="v1.0")
        if isinstance(cmd, list) and "--print-api-key" in cmd:
            return _ok(stdout="sk-ant-xxx")
        if isinstance(cmd, list) and "auth" in cmd:
            return _ok(stdout="Logged in")
        return _ok()

    with patch("autoloop.doctor.subprocess.run", fake_subprocess):
        results = run_doctor(config_path=toml, repo_dir=tmp_path, run_verify=False)

    failed = [msg for passed, msg in results if not passed]
    assert any(".claude/settings.json" in msg for msg in failed)
