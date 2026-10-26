from __future__ import annotations

import json
import logging
import subprocess

import autoloop.claude_runner as claude_runner
from autoloop.claude_runner import ClaudeResult, run_claude

_FULL_RESPONSE = {
    "result": "the response text",
    "total_cost_usd": 0.03978475,
    "usage": {
        "input_tokens": 2335,
        "cache_read_input_tokens": 16832,
        "cache_creation_input_tokens": 2903,
        "output_tokens": 62,
    },
}


def _fake_run(stdout="", returncode=0, raises=None, stderr=""):
    """Build a fake subprocess.run capturing the command it received."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()

    return fake_run, captured


def test_run_claude_parses_full_json(monkeypatch):
    fake_run, _ = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("do the thing", "opus", 900)

    assert result.success is True
    assert result.text == "the response text"
    assert result.cost_usd == 0.03978475
    assert result.input_tokens == 2335
    assert result.output_tokens == 62
    assert result.cache_read_tokens == 16832
    assert result.cache_creation_tokens == 2903


def test_run_claude_invokes_json_output_format(monkeypatch):
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("prompt text", "sonnet", 90)

    cmd = captured["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[-1] == "prompt text"
    assert captured["kwargs"]["timeout"] == 90


def test_run_claude_missing_usage_defaults_to_zero(monkeypatch):
    fake_run, _ = _fake_run(stdout=json.dumps({"result": "hi", "total_cost_usd": 1.5}))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("p", "opus", 10)

    assert result.success is True
    assert result.text == "hi"
    assert result.cost_usd == 1.5
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_read_tokens == 0
    assert result.cache_creation_tokens == 0


def test_run_claude_defaults_cwd_to_none(monkeypatch):
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10)

    assert captured["kwargs"]["cwd"] is None


def test_run_claude_no_module_level_repo_dir():
    """claude_runner should not have a module-level REPO_DIR attribute."""
    assert not hasattr(claude_runner, "REPO_DIR"), (
        "claude_runner should not have REPO_DIR; cwd is resolved at call time"
    )


def test_run_claude_uses_repo_dir_as_cwd(tmp_path, monkeypatch):
    """run_claude should pass repo_dir through to subprocess as cwd."""
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=tmp_path)

    assert captured["kwargs"]["cwd"] == tmp_path


def test_run_claude_timeout_returns_failure(monkeypatch):
    fake_run, _ = _fake_run(raises=subprocess.TimeoutExpired(cmd="claude", timeout=1))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("p", "opus", 1)

    assert result == ClaudeResult("", 0, 0, 0, 0, 0, success=False, timed_out=True)
    assert result.timed_out is True


def test_run_claude_nonzero_exit_returns_failure(monkeypatch):
    fake_run, _ = _fake_run(stdout="boom", returncode=1)
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("p", "opus", 10)

    assert result.success is False
    assert result.timed_out is False
    assert result.cost_usd == 0
    assert result.input_tokens == 0


def test_run_claude_does_not_raise_on_timeout(monkeypatch):
    fake_run, _ = _fake_run(raises=subprocess.TimeoutExpired(cmd="claude", timeout=1))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("p", "opus", 1)
    assert isinstance(result, ClaudeResult)


def test_run_claude_invalid_json_falls_back_to_text(monkeypatch):
    fake_run, _ = _fake_run(stdout="plain text, not json")
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("p", "opus", 10)

    assert result.success is True
    assert result.text == "plain text, not json"
    assert result.cost_usd == 0
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_read_tokens == 0
    assert result.cache_creation_tokens == 0


# --- allowed tools tests ---


def _write_settings(repo_dir, settings):
    """Write a .claude/settings.json fixture."""
    claude_dir = repo_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps(settings))


def test_allowed_tools_from_settings(tmp_path, monkeypatch):
    """Writes a settings fixture with permissions.allow and asserts --allowedTools."""
    _write_settings(tmp_path, {"permissions": {"allow": ["Edit", "Write", "Bash(git add*)"]}})
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("do stuff", "opus", 10, repo_dir=tmp_path)

    cmd = captured["cmd"]
    idx = cmd.index("--allowedTools")
    dash_idx = cmd.index("--", idx + 1)
    entries = cmd[idx + 1 : dash_idx]
    assert entries == ["Edit", "Write", "Bash(git add*)"]
    assert cmd[-1] == "do stuff"


def test_deny_entries_not_in_command(tmp_path, monkeypatch):
    """When permissions.deny is present, none of those entries appear in argv."""
    _write_settings(
        tmp_path,
        {
            "permissions": {
                "allow": ["Edit"],
                "deny": ["Bash(rm*)", "Write"],
            }
        },
    )
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=tmp_path)

    cmd = captured["cmd"]
    assert "Bash(rm*)" not in cmd
    assert "Write" not in cmd


def test_no_settings_file_no_allowed_tools(tmp_path, monkeypatch):
    """No settings.json → no --allowedTools, command matches original shape."""
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("prompt text", "sonnet", 90, repo_dir=tmp_path)

    cmd = captured["cmd"]
    assert "--allowedTools" not in cmd
    assert "--" not in cmd
    assert cmd == ["claude", "-p", "--model", "sonnet", "--output-format", "json", "prompt text"]


def test_malformed_json_no_allowed_tools(tmp_path, monkeypatch):
    """Malformed JSON in settings.json does not raise and adds no --allowedTools."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{not valid json")

    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=tmp_path)

    assert "--allowedTools" not in captured["cmd"]


def test_empty_allow_list_no_allowed_tools(tmp_path, monkeypatch):
    """Empty permissions.allow → no --allowedTools."""
    _write_settings(tmp_path, {"permissions": {"allow": []}})
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=tmp_path)

    assert "--allowedTools" not in captured["cmd"]


def test_no_permissions_key_no_allowed_tools(tmp_path, monkeypatch):
    """Settings without permissions key → no --allowedTools."""
    _write_settings(tmp_path, {"some_other_key": True})
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=tmp_path)

    assert "--allowedTools" not in captured["cmd"]


def test_untrusted_workspace_logs_warning(tmp_path, monkeypatch, caplog):
    """stderr containing 'has not been trusted' triggers a warning log."""
    _write_settings(tmp_path, {"permissions": {"allow": ["Edit"]}})
    fake_run, _ = _fake_run(
        stdout=json.dumps(_FULL_RESPONSE),
        stderr="Warning: workspace /foo has not been trusted by the user",
    )
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger="autoloop.claude_runner"):
        run_claude("p", "opus", 10, repo_dir=tmp_path)

    assert any("has not been trusted" in r.message for r in caplog.records)


def test_no_untrusted_warning_when_stderr_clean(tmp_path, monkeypatch, caplog):
    """No warning is logged when stderr does not contain 'has not been trusted'."""
    _write_settings(tmp_path, {"permissions": {"allow": ["Edit"]}})
    fake_run, _ = _fake_run(stdout=json.dumps(_FULL_RESPONSE), stderr="")
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger="autoloop.claude_runner"):
        run_claude("p", "opus", 10, repo_dir=tmp_path)

    assert not any("has not been trusted" in r.message for r in caplog.records)


def test_settings_read_from_repo_dir_not_cwd(tmp_path, monkeypatch):
    """run_claude reads settings from the given repo_dir, not process cwd."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _write_settings(worktree, {"permissions": {"allow": ["Edit", "Write"]}})

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)

    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=worktree)

    cmd = captured["cmd"]
    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    dash_idx = cmd.index("--", idx + 1)
    assert cmd[idx + 1 : dash_idx] == ["Edit", "Write"]


def test_prompt_after_double_dash(tmp_path, monkeypatch):
    """Prompt is the final argv element, immediately after '--'."""
    _write_settings(tmp_path, {"permissions": {"allow": ["Edit"]}})
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("my prompt", "opus", 10, repo_dir=tmp_path)

    cmd = captured["cmd"]
    dash_idx = cmd.index("--")
    assert cmd[dash_idx + 1] == "my prompt"
    assert cmd[-1] == "my prompt"
    idx = cmd.index("--allowedTools")
    allow_entries = cmd[idx + 1 : dash_idx]
    assert "my prompt" not in allow_entries
