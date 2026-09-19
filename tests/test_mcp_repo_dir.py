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


# --- autoloop_triage flag parameters ---


def test_triage_drain_mode(mcp_tools):
    """autoloop_triage passes --drain and --max-rounds when drain=True."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_triage"](drain=True)

    assert captured[0]["cmd"] == ["autoloop", "triage", "--drain", "--max-rounds", "5"]
    assert "drain mode" in result
    assert "max 5 rounds" in result


def test_triage_drain_custom_max_rounds(mcp_tools):
    """autoloop_triage passes custom --max-rounds value."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_triage"](drain=True, max_rounds=3)

    assert captured[0]["cmd"] == ["autoloop", "triage", "--drain", "--max-rounds", "3"]
    assert "max 3 rounds" in result


def test_triage_single_issue(mcp_tools):
    """autoloop_triage passes --issue for single-issue targeting."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_triage"](issue=42)

    assert captured[0]["cmd"] == ["autoloop", "triage", "--issue", "42"]
    assert "issue #42" in result


def test_triage_no_flags_single_pass(mcp_tools):
    """autoloop_triage with defaults produces base command and simple message."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_triage"]()

    assert captured[0]["cmd"] == ["autoloop", "triage"]
    assert result == "Started triage run."


def test_triage_drain_with_issue(mcp_tools):
    """autoloop_triage combines drain and issue flags."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        mcp_tools["autoloop_triage"](drain=True, issue=7)

    cmd = captured[0]["cmd"]
    assert "--drain" in cmd
    assert "--max-rounds" in cmd
    assert "--issue" in cmd
    assert "7" in cmd


def test_triage_max_rounds_ignored_without_drain(mcp_tools):
    """max_rounds is not passed when drain is False."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        mcp_tools["autoloop_triage"](max_rounds=10)

    assert captured[0]["cmd"] == ["autoloop", "triage"]


# --- autoloop_implement auto-fix parameters ---


def test_implement_auto_fix(mcp_tools):
    """autoloop_implement passes --auto-fix and --max-pr-review-rounds."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_implement"](auto_fix=True)

    cmd = captured[0]["cmd"]
    assert "--auto-fix" in cmd
    assert "--max-pr-review-rounds" in cmd
    assert "3" in cmd
    assert "auto-fix" in result
    assert "max 3 review rounds" in result


def test_implement_auto_fix_custom_rounds(mcp_tools):
    """autoloop_implement passes custom --max-pr-review-rounds."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_implement"](auto_fix=True, max_pr_review_rounds=5)

    cmd = captured[0]["cmd"]
    assert cmd == [
        "autoloop",
        "implement",
        "--max-issues",
        "1",
        "--auto-fix",
        "--max-pr-review-rounds",
        "5",
    ]
    assert "max 5 review rounds" in result


def test_implement_auto_fix_with_issue(mcp_tools):
    """autoloop_implement combines --issue and --auto-fix."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_implement"](issue=99, auto_fix=True)

    cmd = captured[0]["cmd"]
    assert "--issue" in cmd
    assert "99" in cmd
    assert "--auto-fix" in cmd
    assert "issue #99" in result
    assert "auto-fix" in result


def test_implement_no_auto_fix_omits_flags(mcp_tools):
    """--auto-fix and --max-pr-review-rounds are not passed when auto_fix is False."""
    captured = []

    def fake_popen(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})

    with patch("autoloop.mcp_server.subprocess.Popen", fake_popen):
        result = mcp_tools["autoloop_implement"](issue=10)

    cmd = captured[0]["cmd"]
    assert "--auto-fix" not in cmd
    assert "--max-pr-review-rounds" not in cmd
    assert "auto-fix" not in result


# --- autoloop_eval ---


def test_eval_registered(mcp_tools):
    """autoloop_eval appears in the MCP server's tool listing."""
    assert "autoloop_eval" in mcp_tools


def test_eval_snapshot_default(mcp_tools, tmp_path):
    """autoloop_eval with defaults returns snapshot summary."""
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 120,
            }
        )
        + "\n"
    )

    result = mcp_tools["autoloop_eval"](repo_dir=str(tmp_path))

    assert "Eval Snapshot" in result
    assert "Issues implemented:     1" in result

    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    assert snap_dir.exists()
    assert len(list(snap_dir.glob("*.json"))) == 1


def test_eval_snapshot_no_history(mcp_tools, tmp_path):
    """autoloop_eval returns empty snapshot when no run history exists."""
    result = mcp_tools["autoloop_eval"](repo_dir=str(tmp_path))

    assert "Eval Snapshot" in result
    assert "Issues implemented:     0" in result


def test_eval_trend(mcp_tools, tmp_path):
    """autoloop_eval with trend=True returns trend output."""
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    for d in ("2026-09-12", "2026-09-13"):
        (snap_dir / f"{d}.json").write_text(
            json.dumps(
                {
                    "date": d,
                    "total_implementations": 10,
                    "first_attempt_rate": 0.9,
                    "avg_cost_usd": 1.0,
                    "human_edit_rate": 0.0,
                }
            )
        )

    result = mcp_tools["autoloop_eval"](trend=True, repo_dir=str(tmp_path))

    assert "Eval Trend" in result


def test_eval_trend_empty(mcp_tools, tmp_path):
    """autoloop_eval with trend=True and no snapshots returns appropriate message."""
    result = mcp_tools["autoloop_eval"](trend=True, repo_dir=str(tmp_path))

    assert "No snapshots found" in result


def test_eval_compare_no_previous(mcp_tools, tmp_path):
    """autoloop_eval with compare=True and no previous snapshot shows current."""
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    result = mcp_tools["autoloop_eval"](compare=True, repo_dir=str(tmp_path))

    assert "No previous snapshot to compare against" in result
    assert "Eval Snapshot" in result


def test_eval_compare_with_previous(mcp_tools, tmp_path):
    """autoloop_eval with compare=True shows comparison when previous snapshot exists."""
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "2026-09-13.json").write_text(
        json.dumps(
            {
                "date": "2026-09-13",
                "total_implementations": 10,
                "first_attempt_rate": 0.8,
                "avg_cost_usd": 1.5,
                "avg_duration_seconds": 200,
                "human_edit_rate": 0.1,
                "modules": {},
            }
        )
    )

    log_dir = tmp_path / "autoloop"
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    result = mcp_tools["autoloop_eval"](compare=True, repo_dir=str(tmp_path))

    assert "Comparison:" in result
    assert "2026-09-13" in result


def test_eval_publish(mcp_tools, tmp_path):
    """autoloop_eval with publish=True generates EVAL.md, commits, and pushes."""
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 120,
            }
        )
        + "\n"
    )

    captured = []

    def fake_run(cmd, **kwargs):
        captured.append({"cmd": cmd, "kwargs": kwargs})
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        result = mcp_tools["autoloop_eval"](publish=True, repo_dir=str(tmp_path))

    assert "Eval Snapshot" in result
    assert "EVAL.md generated and committed" in result

    md_path = tmp_path / "EVAL.md"
    assert md_path.exists()
    md_content = md_path.read_text()
    assert "# EVAL Report" in md_content

    git_cmds = [c["cmd"] for c in captured]
    add_cmds = [c for c in git_cmds if c[:2] == ["git", "add"]]
    assert len(add_cmds) == 1
    assert str(md_path) in add_cmds[0]

    commit_cmds = [c for c in git_cmds if c[:2] == ["git", "commit"]]
    assert len(commit_cmds) == 1
    assert "chore: update eval report" in commit_cmds[0][3]

    push_cmds = [c for c in git_cmds if c[:2] == ["git", "push"]]
    assert len(push_cmds) == 1


def test_eval_publish_false_no_commit(mcp_tools, tmp_path):
    """autoloop_eval without publish does not write EVAL.md or commit."""
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    result = mcp_tools["autoloop_eval"](repo_dir=str(tmp_path))

    assert "EVAL.md" not in result
    assert not (tmp_path / "EVAL.md").exists()


def test_eval_uses_cwd_when_no_repo_dir(mcp_tools, tmp_path, monkeypatch):
    """autoloop_eval uses cwd when repo_dir is omitted."""
    monkeypatch.setattr("autoloop.mcp_server.Path.cwd", lambda: tmp_path)

    result = mcp_tools["autoloop_eval"]()

    assert "Eval Snapshot" in result


def test_eval_with_config_loads_repo(mcp_tools, tmp_path, monkeypatch):
    """autoloop_eval loads repo from config when autoloop.toml exists."""
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')

    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    fetch_calls = []

    def fake_fetch(repo):
        fetch_calls.append(repo)
        return []

    with patch("autoloop.eval.fetch_pr_data", fake_fetch):
        mcp_tools["autoloop_eval"](repo_dir=str(tmp_path))

    assert len(fetch_calls) == 1
    assert fetch_calls[0] == "acme-corp/widget"


def test_eval_publish_pr(mcp_tools, tmp_path):
    """autoloop_eval with publish=True, pr=True calls _publish_via_pr."""
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 120,
            }
        )
        + "\n"
    )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        stdout = ""
        if cmd[:3] == ["gh", "pr", "create"]:
            stdout = "https://github.com/owner/repo/pull/55\n"
        return type("R", (), {"returncode": 0, "stdout": stdout, "stderr": ""})()

    with patch("autoloop.eval.subprocess.run", fake_run):
        result = mcp_tools["autoloop_eval"](publish=True, pr=True, repo_dir=str(tmp_path))

    assert "PR created:" in result
    assert "https://github.com/owner/repo/pull/55" in result

    md_path = tmp_path / "EVAL.md"
    assert md_path.exists()

    checkouts = [c for c in calls if c[:3] == ["git", "checkout", "-b"]]
    assert len(checkouts) == 1
    assert checkouts[0][3].startswith("chore/eval-")

    snap_adds = [c for c in calls if c[:2] == ["git", "add"]]
    assert len(snap_adds) == 1
    assert str(md_path) in snap_adds[0]
    assert any("eval_snapshots" in arg for arg in snap_adds[0])

    main_checkouts = [c for c in calls if c == ["git", "checkout", "main"]]
    assert len(main_checkouts) == 1


def test_eval_publish_push_failure_suggests_pr(mcp_tools, tmp_path):
    """MCP autoloop_eval non-PR publish detects branch protection on push failure."""
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "push"]:
            return type(
                "R",
                (),
                {
                    "returncode": 1,
                    "stdout": "",
                    "stderr": "remote: error: GH013: Repository rule violations found",
                },
            )()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        result = mcp_tools["autoloop_eval"](publish=True, repo_dir=str(tmp_path))

    assert "branch protection" in result
    assert "pr=True" in result
