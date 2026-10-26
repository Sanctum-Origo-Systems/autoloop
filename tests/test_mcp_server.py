"""Tests for RepoContext threading in mcp_server.py."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

from autoloop.config import RepoContext
from autoloop.mcp_server import _make_context, _read_last_runs


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


# --- _make_context per-repo isolation ---


def test_make_context_different_repos_produce_different_data_dirs(tmp_path):
    """Two different repo_dir values produce distinct data_dir values via _make_context."""
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()

    ctx_a = _make_context(str(repo_a))
    ctx_b = _make_context(str(repo_b))

    assert ctx_a.data_dir != ctx_b.data_dir
    assert ctx_a.repo_dir == repo_a.resolve()
    assert ctx_b.repo_dir == repo_b.resolve()


def test_make_context_same_repo_produces_same_data_dir(tmp_path):
    """The same repo_dir always resolves to the same data_dir."""
    repo = tmp_path / "repo"
    repo.mkdir()

    ctx1 = _make_context(str(repo))
    ctx2 = _make_context(str(repo))

    assert ctx1.data_dir == ctx2.data_dir


def test_make_context_none_uses_cwd(tmp_path, monkeypatch):
    """_make_context(None) resolves to the current working directory."""
    monkeypatch.chdir(tmp_path)

    ctx = _make_context(None)

    assert ctx.repo_dir == tmp_path.resolve()


# --- Per-repo isolation through MCP tool ---


def test_status_per_repo_isolation(mcp_tools, tmp_path, monkeypatch):
    """Two autoloop_status calls with different repo_dir values resolve distinct data_dirs."""
    import autoloop.mcp_server as mod

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    for d in (repo_a, repo_b):
        d.mkdir()
        (d / "autoloop.toml").write_text('repo = "acme-corp/widget"\n')

    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    contexts = []
    orig = mod._make_context

    def spy(repo_dir):
        ctx = orig(repo_dir)
        contexts.append(ctx)
        return ctx

    monkeypatch.setattr(mod, "_make_context", spy)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if cmd[0] == "pgrep":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        if cmd[0] == "systemctl":
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        mcp_tools["autoloop_status"](repo_dir=str(repo_a))
        mcp_tools["autoloop_status"](repo_dir=str(repo_b))

    assert len(contexts) == 2
    assert contexts[0].data_dir != contexts[1].data_dir


def test_no_path_cwd_in_mcp_server():
    """grep-equivalent: Path.cwd() must not appear in mcp_server.py source."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "autoloop" / "mcp_server.py"
    content = src.read_text()
    assert "Path.cwd()" not in content


# --- _read_last_runs reads the data-dir run history ---


def _write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _history_ctx(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return RepoContext.for_data_dir(tmp_path / "data", repo_dir=repo)


LEGACY_ROWS = [
    {"type": "implement", "issue": 99, "success": True, "timestamp": "t9"},
    {"type": "review", "pr_number": 999, "success": True, "timestamp": "t9"},
]


def test_read_last_runs_returns_latest_from_data_dir(tmp_path):
    """The most recent implement and review rows in ctx.data_dir are returned."""
    ctx = _history_ctx(tmp_path)
    _write_rows(
        ctx.data_dir / "run_history.jsonl",
        [
            {"type": "implement", "issue": 1, "success": True, "timestamp": "t1"},
            {"type": "review", "pr_number": 10, "success": True, "timestamp": "t2"},
            {"type": "implement", "issue": 2, "success": False, "timestamp": "t3"},
            {"type": "review", "pr_number": 20, "success": False, "timestamp": "t4"},
        ],
    )

    last_impl, last_review = _read_last_runs(ctx)

    assert last_impl["issue"] == 2
    assert last_review["pr_number"] == 20


def test_read_last_runs_ignores_legacy_file(tmp_path):
    """Rows in <repo>/autoloop/run_history.jsonl never override the data-dir rows."""
    ctx = _history_ctx(tmp_path)
    _write_rows(ctx.repo_dir / "autoloop" / "run_history.jsonl", LEGACY_ROWS)
    _write_rows(
        ctx.data_dir / "run_history.jsonl",
        [
            {"type": "implement", "issue": 3, "success": True, "timestamp": "t1"},
            {"type": "review", "pr_number": 30, "success": True, "timestamp": "t2"},
        ],
    )

    last_impl, last_review = _read_last_runs(ctx)

    assert last_impl["issue"] == 3
    assert last_review["pr_number"] == 30


def test_read_last_runs_no_fallback_to_legacy_file(tmp_path):
    """With only the legacy file present, nothing is returned."""
    ctx = _history_ctx(tmp_path)
    _write_rows(ctx.repo_dir / "autoloop" / "run_history.jsonl", LEGACY_ROWS)

    assert _read_last_runs(ctx) == (None, None)


def test_read_last_runs_missing_data_dir_file(tmp_path):
    """A missing data-dir run_history.jsonl returns empty results without raising."""
    ctx = _history_ctx(tmp_path)

    assert not (ctx.data_dir / "run_history.jsonl").exists()
    assert _read_last_runs(ctx) == (None, None)


def test_status_reports_data_dir_runs(mcp_tools, tmp_path, monkeypatch):
    """autoloop_status reports the last implement and review rows from ctx.data_dir."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "autoloop.toml").write_text('repo = "acme-corp/widget"\n')

    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    _write_rows(
        _make_context(str(repo)).data_dir / "run_history.jsonl",
        [
            {"type": "implement", "issue": 4, "success": True, "cost_usd": 0.5, "timestamp": "t1"},
            {
                "type": "review",
                "pr_number": 40,
                "success": False,
                "cost_usd": 0.1,
                "timestamp": "t2",
            },
        ],
    )
    _write_rows(repo / "autoloop" / "run_history.jsonl", LEGACY_ROWS)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        result = mcp_tools["autoloop_status"](repo_dir=str(repo))

    assert "Last implement: issue #4 — success — $0.50 — t1" in result
    assert "Last review: PR #40 — failed — $0.10 — t2" in result
    assert "#99" not in result


def test_no_legacy_run_history_path_in_mcp_server():
    """mcp_server.py must not reference the legacy autoloop/run_history.jsonl path."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "autoloop" / "mcp_server.py"
    content = src.read_text()
    assert '"autoloop" / "run_history.jsonl"' not in content
    assert "autoloop/run_history.jsonl" not in content
