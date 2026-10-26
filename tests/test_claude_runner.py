from __future__ import annotations

import json
import subprocess
from pathlib import Path

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


def _fake_run(stdout="", returncode=0, raises=None):
    """Build a fake subprocess.run capturing the command it received."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        if raises is not None:
            raise raises
        return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()

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


# --- Settings via --settings tests ---

_SETTINGS_JSON = json.dumps({"permissions": {"allow": ["Edit", "Write"]}})


def _fake_run_routed(
    *,
    git_stdout=None,
    git_returncode=0,
    git_raises=None,
    claude_stdout="",
    claude_returncode=0,
    claude_raises=None,
):
    """Build a fake subprocess.run that routes git vs claude calls."""
    call_log = []
    captured_settings = {}

    def fake_run(cmd, **kwargs):
        call_log.append({"cmd": list(cmd), "kwargs": kwargs})

        if cmd[0] == "git":
            if git_raises is not None:
                raise git_raises
            return type(
                "R",
                (),
                {
                    "returncode": git_returncode,
                    "stdout": git_stdout or "",
                    "stderr": "",
                },
            )()

        if "--settings" in cmd:
            idx = cmd.index("--settings")
            path = Path(cmd[idx + 1])
            if path.exists():
                captured_settings["path"] = path
                captured_settings["content"] = path.read_text()

        if claude_raises is not None:
            raise claude_raises
        return type(
            "R",
            (),
            {
                "returncode": claude_returncode,
                "stdout": claude_stdout,
                "stderr": "",
            },
        )()

    return fake_run, call_log, captured_settings


def test_settings_passed_when_git_show_succeeds(tmp_path, monkeypatch):
    """With valid origin/main settings, claude gets --settings pointing outside repo_dir."""
    fake_run, call_log, captured = _fake_run_routed(
        git_stdout=_SETTINGS_JSON,
        claude_stdout=json.dumps(_FULL_RESPONSE),
    )
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("do stuff", "opus", 900, repo_dir=tmp_path)

    claude_call = next(c for c in call_log if c["cmd"][0] == "claude")
    cmd = claude_call["cmd"]
    assert "--settings" in cmd
    settings_path = Path(cmd[cmd.index("--settings") + 1])
    assert not str(settings_path).startswith(str(tmp_path))
    assert captured["content"] == _SETTINGS_JSON
    assert not settings_path.exists()


def test_settings_uses_origin_main_content(tmp_path, monkeypatch):
    """The --settings file contains origin/main content, not the working tree's."""
    main_settings = json.dumps({"permissions": {"allow": ["Edit"]}})
    fake_run, _, captured = _fake_run_routed(
        git_stdout=main_settings,
        claude_stdout=json.dumps(_FULL_RESPONSE),
    )
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=tmp_path)

    assert captured["content"] == main_settings


def test_no_settings_when_git_show_fails(tmp_path, monkeypatch):
    """When git show fails, command matches today's format with no --settings."""
    fake_run, call_log, _ = _fake_run_routed(
        git_returncode=128,
        claude_stdout=json.dumps(_FULL_RESPONSE),
    )
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=tmp_path)

    claude_call = next(c for c in call_log if c["cmd"][0] == "claude")
    assert claude_call["cmd"] == ["claude", "-p", "--model", "opus", "--output-format", "json", "p"]


def test_no_settings_when_git_returns_invalid_json(tmp_path, monkeypatch):
    """When git show returns non-JSON, command matches today's format."""
    fake_run, call_log, _ = _fake_run_routed(
        git_stdout="not valid json",
        claude_stdout=json.dumps(_FULL_RESPONSE),
    )
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10, repo_dir=tmp_path)

    claude_call = next(c for c in call_log if c["cmd"][0] == "claude")
    assert claude_call["cmd"] == ["claude", "-p", "--model", "opus", "--output-format", "json", "p"]


def test_settings_file_removed_on_timeout(tmp_path, monkeypatch):
    """Temp settings file is deleted even when claude times out."""
    settings_path_holder = {}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return type("R", (), {"returncode": 0, "stdout": _SETTINGS_JSON, "stderr": ""})()
        if "--settings" in cmd:
            idx = cmd.index("--settings")
            path = Path(cmd[idx + 1])
            settings_path_holder["path"] = path
            assert path.exists()
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("p", "opus", 1, repo_dir=tmp_path)

    assert result.timed_out is True
    assert not settings_path_holder["path"].exists()


def test_prompt_is_last_argv_with_settings(tmp_path, monkeypatch):
    """Prompt is always the final argv element, even with --settings."""
    fake_run, call_log, _ = _fake_run_routed(
        git_stdout=_SETTINGS_JSON,
        claude_stdout=json.dumps(_FULL_RESPONSE),
    )
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("the prompt", "opus", 10, repo_dir=tmp_path)

    claude_call = next(c for c in call_log if c["cmd"][0] == "claude")
    assert claude_call["cmd"][-1] == "the prompt"
