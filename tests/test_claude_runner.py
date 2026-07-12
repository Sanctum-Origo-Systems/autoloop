from __future__ import annotations

import json
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


def test_run_claude_uses_repo_dir_as_cwd(monkeypatch):
    fake_run, captured = _fake_run(stdout=json.dumps(_FULL_RESPONSE))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    run_claude("p", "opus", 10)

    assert captured["kwargs"]["cwd"] == claude_runner.REPO_DIR


def test_run_claude_timeout_returns_failure(monkeypatch):
    fake_run, _ = _fake_run(raises=subprocess.TimeoutExpired(cmd="claude", timeout=1))
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("p", "opus", 1)

    assert result == ClaudeResult("", 0, 0, 0, 0, success=False)


def test_run_claude_nonzero_exit_returns_failure(monkeypatch):
    fake_run, _ = _fake_run(stdout="boom", returncode=1)
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    result = run_claude("p", "opus", 10)

    assert result.success is False
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
