"""Tests for RepoContext threading in mcp_server.py."""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from autoloop.mcp_server import _make_context


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
