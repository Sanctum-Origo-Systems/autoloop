"""Tests for repo_dir parameter in MCP tools."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

from autoloop.mcp_server import _read_last_runs


# --- _read_last_runs ---


def test_read_last_runs_returns_both(tmp_path):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    lines = [
        json.dumps({"type": "implement", "issue": 10, "success": True, "timestamp": "t1"}),
        json.dumps(
            {
                "type": "review",
                "pr_number": 42,
                "success": False,
                "cost_usd": 0.08,
                "timestamp": "t2",
            }
        ),
    ]
    log_file.write_text("\n".join(lines) + "\n")

    last_impl, last_review = _read_last_runs(base=tmp_path)
    assert last_impl["issue"] == 10
    assert last_review["pr_number"] == 42
    assert last_review["type"] == "review"


def test_read_last_runs_no_file(tmp_path):
    last_impl, last_review = _read_last_runs(base=tmp_path)
    assert last_impl is None
    assert last_review is None


def test_read_last_runs_only_implement(tmp_path):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps({"type": "implement", "issue": 5, "success": True, "timestamp": "t1"}) + "\n"
    )

    last_impl, last_review = _read_last_runs(base=tmp_path)
    assert last_impl["issue"] == 5
    assert last_review is None


def test_read_last_runs_only_review(tmp_path):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps(
            {"type": "review", "pr_number": 7, "success": True, "cost_usd": 0.05, "timestamp": "t1"}
        )
        + "\n"
    )

    last_impl, last_review = _read_last_runs(base=tmp_path)
    assert last_impl is None
    assert last_review["pr_number"] == 7


# --- Fake FastMCP for testing tool registration ---


class _FakeServer:
    def __init__(self, name="test"):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator

    def run(self):
        pass


@pytest.fixture()
def mcp_tools(monkeypatch):
    """Register MCP tools using a fake FastMCP and return the tool dict."""
    fake_fastmcp_mod = types.ModuleType("fastmcp")

    server = _FakeServer()
    fake_fastmcp_mod.FastMCP = lambda name: server

    monkeypatch.setitem(sys.modules, "fastmcp", fake_fastmcp_mod)

    from importlib import reload

    import autoloop.mcp_server

    reload(autoloop.mcp_server)
    autoloop.mcp_server.main()
    return server.tools


# --- _spawn cwd parameter ---


def test_implement_passes_cwd(mcp_tools, tmp_path):
    """autoloop_implement passes repo_dir as cwd to Popen."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_implement"](issue=42, repo_dir=str(tmp_path))

    assert len(captured) == 1
    assert captured[0]["kwargs"]["cwd"] == str(tmp_path)
    assert "--issue" in captured[0]["cmd"]
    assert "42" in captured[0]["cmd"]
    assert result == "Started implementation of issue #42."


def test_implement_cwd_none_when_no_repo_dir(mcp_tools):
    """autoloop_implement passes cwd=None when repo_dir omitted."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_implement"](max_issues=2)

    assert len(captured) == 1
    assert captured[0]["kwargs"]["cwd"] is None
    assert result == "Started implementation (max 2 issue(s))."


def test_triage_passes_cwd(mcp_tools, tmp_path):
    """autoloop_triage passes repo_dir as cwd to Popen."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_triage"](repo_dir=str(tmp_path))

    assert len(captured) == 1
    assert captured[0]["kwargs"]["cwd"] == str(tmp_path)
    assert captured[0]["cmd"] == ["autoloop", "triage"]
    assert result == "Started triage run."


def test_triage_cwd_none_when_no_repo_dir(mcp_tools):
    """autoloop_triage passes cwd=None when repo_dir omitted."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        mcp_tools["autoloop_triage"]()

    assert len(captured) == 1
    assert captured[0]["kwargs"]["cwd"] is None


def test_preflight_passes_cwd(mcp_tools, tmp_path):
    """autoloop_preflight passes repo_dir as cwd to Popen."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_preflight"](repo_dir=str(tmp_path))

    assert len(captured) == 1
    assert captured[0]["kwargs"]["cwd"] == str(tmp_path)
    assert captured[0]["cmd"] == ["autoloop", "preflight"]
    assert result == "Started preflight checks."


def test_preflight_cwd_none_when_no_repo_dir(mcp_tools):
    """autoloop_preflight passes cwd=None when repo_dir omitted."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_preflight"]()

    assert len(captured) == 1
    assert captured[0]["kwargs"]["cwd"] is None
    assert result == "Started preflight checks."


def test_preflight_registered(mcp_tools):
    """autoloop_preflight appears in the MCP server's tool listing."""
    assert "autoloop_preflight" in mcp_tools


def test_fix_pr_passes_cwd(mcp_tools, tmp_path):
    """autoloop_fix_pr passes repo_dir as cwd to Popen."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_fix_pr"](pr_number=99, repo_dir=str(tmp_path))

    assert len(captured) == 1
    assert captured[0]["kwargs"]["cwd"] == str(tmp_path)
    assert captured[0]["cmd"] == ["autoloop", "fix-pr", "99"]
    assert result == "Started fix-pr for PR #99."


def test_fix_pr_cwd_none_when_no_repo_dir(mcp_tools):
    """autoloop_fix_pr passes cwd=None when repo_dir omitted."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        mcp_tools["autoloop_fix_pr"](pr_number=1)

    assert len(captured) == 1
    assert captured[0]["kwargs"]["cwd"] is None


def test_review_pr_registered(mcp_tools):
    """autoloop_review_pr appears in the MCP server's tool listing."""
    assert "autoloop_review_pr" in mcp_tools


def test_review_pr_returns_cost_from_return_dict(mcp_tools, tmp_path, monkeypatch):
    """autoloop_review_pr returns cost info from review_pr() return dict."""
    import asyncio

    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    review_result = {
        "success": True,
        "cost_usd": 0.12,
        "input_tokens": 1500,
        "output_tokens": 300,
        "cache_read_tokens": 100,
    }

    with patch("autoloop.cli.review_pr", return_value=review_result):
        result = asyncio.run(mcp_tools["autoloop_review_pr"](pr_number=55, repo_dir=str(tmp_path)))

    assert "passed" in result
    assert "$0.12" in result
    assert "1,500 input" in result
    assert "300 output" in result


def test_review_pr_failure_returns_status(mcp_tools, tmp_path, monkeypatch):
    """autoloop_review_pr returns failure status with cost."""
    import asyncio

    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    review_result = {
        "success": False,
        "cost_usd": 0.08,
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 50,
    }

    with patch("autoloop.cli.review_pr", return_value=review_result):
        result = asyncio.run(mcp_tools["autoloop_review_pr"](pr_number=55, repo_dir=str(tmp_path)))

    assert "failed" in result
    assert "$0.08" in result


def test_status_with_repo_dir(mcp_tools, tmp_path, monkeypatch):
    """autoloop_status resolves paths from repo_dir when provided."""
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')

    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {"issue": 10, "success": True, "cost_usd": 0.75, "timestamp": "2026-07-18T10:00:00"}
        )
        + "\n"
    )

    (tmp_path / ".autoloop.lock").write_text("")

    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if cmd[0] == "pgrep":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        if cmd[0] == "systemctl":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        result = mcp_tools["autoloop_status"](repo_dir=str(tmp_path))

    assert "Last implement: issue #10" in result
    assert "success" in result
    assert "implementation" in result


def test_status_shows_both_implement_and_review(mcp_tools, tmp_path, monkeypatch):
    """autoloop_status shows last implement and last review side-by-side."""
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')

    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    lines = [
        json.dumps(
            {"issue": 10, "success": True, "cost_usd": 0.75, "timestamp": "2026-07-18T10:00:00"}
        ),
        json.dumps(
            {
                "type": "review",
                "issue": 0,
                "pr_number": 42,
                "success": False,
                "cost_usd": 0.12,
                "timestamp": "2026-07-18T11:00:00",
            }
        ),
    ]
    (log_dir / "run_history.jsonl").write_text("\n".join(lines) + "\n")

    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if cmd[0] == "pgrep":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        if cmd[0] == "systemctl":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        result = mcp_tools["autoloop_status"](repo_dir=str(tmp_path))

    assert "Last implement: issue #10" in result
    assert "Last review: PR #42" in result
    assert "failed" in result


def test_status_without_repo_dir_uses_cwd(mcp_tools, tmp_path, monkeypatch):
    """autoloop_status uses cwd when repo_dir is None."""
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')

    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setattr("autoloop.mcp_server.Path.cwd", lambda: tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if cmd[0] == "pgrep":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        if cmd[0] == "systemctl":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        result = mcp_tools["autoloop_status"]()

    assert "Last run: no history" in result
    assert "Active: idle" in result


def test_status_invalid_repo_dir_raises(mcp_tools, tmp_path):
    """autoloop_status raises FileNotFoundError for a dir without autoloop.toml."""
    bad_dir = tmp_path / "nonexistent"
    bad_dir.mkdir()

    with pytest.raises(FileNotFoundError):
        mcp_tools["autoloop_status"](repo_dir=str(bad_dir))
