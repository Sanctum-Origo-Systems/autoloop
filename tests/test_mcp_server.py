"""Tests for RepoContext threading in mcp_server.py."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
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


# --- _spawn: reaping, logging, non-blocking ---


def _wait_reaped(proc, timeout=5.0):
    deadline = time.monotonic() + timeout
    while proc.returncode is None and time.monotonic() < deadline:
        time.sleep(0.05)
    return proc.returncode


def test_spawn_log_path_names_autoloop_subcommand(tmp_path):
    """Log path lives under data_dir/mcp-runs and is named <UTC timestamp>-<command>.log."""
    import autoloop.mcp_server as mod

    path = mod._spawn_log_path(["autoloop", "fix-pr", "7"], str(tmp_path))

    assert path.parent == _make_context(str(tmp_path)).data_dir / "mcp-runs"
    assert re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z-fix-pr\.log", path.name)


def test_spawn_reaps_exited_child(tmp_path):
    """A spawned child that exits is reaped, leaving no zombie."""
    import autoloop.mcp_server as mod

    mod._spawn([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    proc = mod._spawned[-1]

    assert _wait_reaped(proc) == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)


def test_spawn_writes_output_to_log(tmp_path):
    """The child's stdout and stderr land in the log file under data_dir/mcp-runs."""
    import autoloop.mcp_server as mod

    script = "import sys; print('to-stdout'); print('to-stderr', file=sys.stderr)"
    log_path = mod._spawn([sys.executable, "-c", script], cwd=str(tmp_path))
    _wait_reaped(mod._spawned[-1])

    assert log_path.parent == _make_context(str(tmp_path)).data_dir / "mcp-runs"
    content = log_path.read_text()
    assert "to-stdout" in content
    assert "to-stderr" in content


def test_spawn_tool_return_contains_log_path(mcp_tools, tmp_path):
    """An MCP tool's return message includes the log path holding the run's output."""
    import autoloop.mcp_server as mod

    real_popen = subprocess.Popen

    def popen_stub(cmd, **kwargs):
        if cmd[0] == "autoloop":
            cmd = [sys.executable, "-c", "print('preflight-ran')"]
        return real_popen(cmd, **kwargs)

    with patch("autoloop.mcp_server.subprocess.Popen", popen_stub):
        result = mcp_tools["autoloop_preflight"](repo_dir=str(tmp_path))
    _wait_reaped(mod._spawned[-1])

    log_dir = _make_context(str(tmp_path)).data_dir / "mcp-runs"
    logs = list(log_dir.glob("*-preflight.log"))
    assert len(logs) == 1
    assert str(logs[0]) in result
    assert "preflight-ran" in logs[0].read_text()


def test_spawn_returns_without_waiting(tmp_path):
    """_spawn returns while the child is still running."""
    import autoloop.mcp_server as mod

    start = time.monotonic()
    mod._spawn([sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(tmp_path))
    elapsed = time.monotonic() - start
    proc = mod._spawned[-1]
    try:
        assert elapsed < 10
        assert proc.returncode is None
    finally:
        proc.kill()
        _wait_reaped(proc)


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


# --- _get_timer_info ---


def _R(returncode=0, stdout=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


def _fake_systemctl_show(units: dict[str, str]):
    """Return a fake subprocess.run answering `systemctl --user show <unit>` from *units*."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[0] == "gh":
            return _R(stdout="[]")
        if cmd[0] == "systemctl" and "show" in cmd:
            unit = cmd[cmd.index("show") + 1]
            if unit in units:
                return _R(stdout=units[unit])
            return _R(stdout="NextElapseUSecRealtime=\nActiveState=inactive\n")
        return _R(returncode=1)

    fake_run.calls = calls
    return fake_run


ACTIVE_TRIAGE = "NextElapseUSecRealtime=Tue 2026-10-27 00:00:00 UTC\nActiveState=active\n"
ACTIVE_IMPLEMENT = "NextElapseUSecRealtime=Tue 2026-10-27 09:00:00 UTC\nActiveState=active\n"


def test_get_timer_info_active_timers():
    """Active triage and implement timers both report their next run and time remaining."""
    from datetime import UTC, datetime

    from autoloop.mcp_server import _get_timer_info

    fake_run = _fake_systemctl_show(
        {"autoloop-triage.timer": ACTIVE_TRIAGE, "autoloop-implement.timer": ACTIVE_IMPLEMENT}
    )
    now = datetime(2026, 10, 26, 20, 16, tzinfo=UTC)
    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        timers = _get_timer_info("autoloop", now=now)

    assert timers == {
        "triage": "2026-10-27 00:00 UTC (in 3h 44min)",
        "implement": "2026-10-27 09:00 UTC (in 12h 44min)",
    }
    units = [cmd[3] for cmd, _ in fake_run.calls]
    assert units == ["autoloop-triage.timer", "autoloop-implement.timer"]
    for cmd, kwargs in fake_run.calls:
        assert cmd[:3] == ["systemctl", "--user", "show"]
        assert "NextElapseUSecRealtime" in cmd and "ActiveState" in cmd
        assert kwargs["env"]["TZ"] == "UTC"


def test_get_timer_info_uses_prefix():
    """The timer prefix selects which units are queried."""
    from autoloop.mcp_server import _get_timer_info

    fake_run = _fake_systemctl_show({"widget-implement.timer": ACTIVE_IMPLEMENT})
    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        timers = _get_timer_info("widget")

    assert list(timers) == ["implement"]
    assert timers["implement"].startswith("2026-10-27 09:00 UTC (in ")


def test_get_timer_info_missing_or_inactive_timer_left_out():
    """A missing (not-found) or inactive timer is omitted from the result."""
    from autoloop.mcp_server import _get_timer_info

    fake_run = _fake_systemctl_show(
        {
            "autoloop-triage.timer": "NextElapseUSecRealtime=Tue 2026-10-27 00:00:00 UTC\n"
            "ActiveState=inactive\n",
        }
    )
    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        assert _get_timer_info("autoloop") == {}

    fake_run = _fake_systemctl_show(
        {
            "autoloop-triage.timer": ACTIVE_TRIAGE,
            "autoloop-implement.timer": "NextElapseUSecRealtime=n/a\nActiveState=active\n",
        }
    )
    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        assert list(_get_timer_info("autoloop")) == ["triage"]


def test_get_timer_info_systemctl_missing_returns_empty():
    """FileNotFoundError from systemctl returns {}."""
    from autoloop.mcp_server import _get_timer_info

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("systemctl")

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        assert _get_timer_info("autoloop") == {}


def test_get_timer_info_systemctl_failure_returns_empty():
    """systemctl exiting non-zero returns {}."""
    from autoloop.mcp_server import _get_timer_info

    def fake_run(cmd, **kwargs):
        return _R(returncode=1, stdout=ACTIVE_IMPLEMENT)

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        assert _get_timer_info("autoloop") == {}


def test_format_remaining():
    """Durations format as minutes, hours+minutes, or days+hours+minutes."""
    from autoloop.mcp_server import _format_remaining

    assert _format_remaining(5 * 60 + 30) == "5min"
    assert _format_remaining(12 * 3600 + 44 * 60) == "12h 44min"
    assert _format_remaining(2 * 86400 + 3 * 3600 + 1 * 60) == "2d 3h 1min"
    assert _format_remaining(-120) == "0min"


def test_parse_systemd_timestamp():
    """systemd timestamps parse as UTC; empty or 'n/a' values return None."""
    from datetime import UTC, datetime

    from autoloop.mcp_server import _parse_systemd_timestamp

    assert _parse_systemd_timestamp("Tue 2026-10-27 09:00:00 UTC") == datetime(
        2026, 10, 27, 9, 0, tzinfo=UTC
    )
    assert _parse_systemd_timestamp("") is None
    assert _parse_systemd_timestamp("n/a") is None


def test_status_reports_implement_timer_next_run(mcp_tools, tmp_path, monkeypatch):
    """autoloop_status includes the implement timer's next run when it is active."""
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

    fake_run = _fake_systemctl_show({"autoloop-implement.timer": ACTIVE_IMPLEMENT})
    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        result = mcp_tools["autoloop_status"](repo_dir=str(repo))

    assert "Next scheduled: implement: 2026-10-27 09:00 UTC (in " in result
    assert "no scheduled timers found" not in result


def test_no_legacy_run_history_path_in_mcp_server():
    """mcp_server.py must not reference the legacy autoloop/run_history.jsonl path."""
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "src" / "autoloop" / "mcp_server.py"
    content = src.read_text()
    assert '"autoloop" / "run_history.jsonl"' not in content
    assert "autoloop/run_history.jsonl" not in content
