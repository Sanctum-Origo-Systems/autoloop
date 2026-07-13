"""Tests for status display in CLI and MCP server."""

from __future__ import annotations

import json
from unittest.mock import patch

from autoloop.mcp_server import _get_timer_info, _is_process_running, _read_last_run


# --- _read_last_run ---


def test_read_last_run_returns_last_entry(tmp_path, monkeypatch):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps({"issue": 1, "success": True, "cost_usd": 0.50})
        + "\n"
        + json.dumps({"issue": 2, "success": False, "cost_usd": 1.00})
        + "\n"
    )
    monkeypatch.setattr("autoloop.mcp_server.Path.cwd", lambda: tmp_path)

    result = _read_last_run()
    assert result["issue"] == 2
    assert result["success"] is False


def test_read_last_run_returns_none_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("autoloop.mcp_server.Path.cwd", lambda: tmp_path)
    assert _read_last_run() is None


def test_read_last_run_returns_none_when_empty_file(tmp_path, monkeypatch):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text("")
    monkeypatch.setattr("autoloop.mcp_server.Path.cwd", lambda: tmp_path)
    assert _read_last_run() is None


# --- _get_timer_info ---


TIMER_OUTPUT = """\
NEXT                            LEFT          LAST                            PASSED       UNIT                      ACTIVATES
Sun 2026-07-13 20:00:00 UTC     23min left    Sun 2026-07-13 02:00:30 UTC     17h ago      patina-implement.timer    patina-implement.service
Mon 2026-07-14 00:00:00 UTC     4h 23min left Sun 2026-07-13 18:00:30 UTC     1h ago       patina-triage.timer       patina-triage.service
"""

AUTOLOOP_TIMER_OUTPUT = """\
NEXT                            LEFT          LAST                            PASSED       UNIT                        ACTIVATES
Sun 2026-07-13 20:00:00 UTC     23min left    Sun 2026-07-13 02:00:30 UTC     17h ago      autoloop-implement.timer    autoloop-implement.service
Mon 2026-07-14 00:00:00 UTC     4h 23min left Sun 2026-07-13 18:00:30 UTC     1h ago       autoloop-triage.timer       autoloop-triage.service
"""


def _fake_systemctl(stdout, returncode=0):
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()

    return fake_run


def test_get_timer_info_default_prefix_finds_autoloop_timers():
    with patch("autoloop.mcp_server.subprocess.run", _fake_systemctl(AUTOLOOP_TIMER_OUTPUT)):
        timers = _get_timer_info()
    assert "triage" in timers
    assert "implement" in timers


def test_get_timer_info_default_prefix_misses_patina_timers():
    with patch("autoloop.mcp_server.subprocess.run", _fake_systemctl(TIMER_OUTPUT)):
        timers = _get_timer_info()
    assert timers == {}


def test_get_timer_info_patina_prefix_finds_patina_timers():
    with patch("autoloop.mcp_server.subprocess.run", _fake_systemctl(TIMER_OUTPUT)):
        timers = _get_timer_info("patina")
    assert "triage" in timers
    assert "implement" in timers


def test_get_timer_info_patina_prefix_misses_autoloop_timers():
    with patch("autoloop.mcp_server.subprocess.run", _fake_systemctl(AUTOLOOP_TIMER_OUTPUT)):
        timers = _get_timer_info("patina")
    assert timers == {}


def test_get_timer_info_returns_empty_when_systemctl_missing():
    def raise_fnf(cmd, **kwargs):
        raise FileNotFoundError

    with patch("autoloop.mcp_server.subprocess.run", raise_fnf):
        assert _get_timer_info() == {}


def test_get_timer_info_returns_empty_on_nonzero_exit():
    with patch("autoloop.mcp_server.subprocess.run", _fake_systemctl("", returncode=1)):
        assert _get_timer_info() == {}


def test_get_timer_info_returns_empty_when_no_matching_timers():
    output = "NEXT  LEFT  LAST  PASSED  UNIT  ACTIVATES\n"
    with patch("autoloop.mcp_server.subprocess.run", _fake_systemctl(output)):
        assert _get_timer_info() == {}


# --- _is_process_running ---


def test_is_process_running_true_when_found():
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "12345\n", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        assert _is_process_running("autoloop triage") is True


def test_is_process_running_false_when_not_found():
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    with patch("autoloop.mcp_server.subprocess.run", fake_run):
        assert _is_process_running("autoloop triage") is False


def test_is_process_running_false_when_pgrep_missing():
    def raise_fnf(cmd, **kwargs):
        raise FileNotFoundError

    with patch("autoloop.mcp_server.subprocess.run", raise_fnf):
        assert _is_process_running("autoloop triage") is False


# --- CLI _show_status timer detection ---


def test_cli_show_status_uses_timer_prefix(tmp_path, monkeypatch, capsys):
    from autoloop.config import AutoLoopConfig

    monkeypatch.setattr(
        "autoloop.config.load_config",
        lambda path=None: AutoLoopConfig(repo="owner/repo", timer_prefix="patina"),
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if cmd[0] == "systemctl":
            return type(
                "R",
                (),
                {
                    "returncode": 0,
                    "stdout": TIMER_OUTPUT,
                    "stderr": "",
                },
            )()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("subprocess.run", fake_run):
        import autoloop.cli

        autoloop.cli._show_status()

    out = capsys.readouterr().out
    assert "patina-triage" in out or "patina-implement" in out
    assert "none found" not in out


def test_cli_show_status_default_prefix_misses_patina(tmp_path, monkeypatch, capsys):
    from autoloop.config import AutoLoopConfig

    monkeypatch.setattr(
        "autoloop.config.load_config",
        lambda path=None: AutoLoopConfig(repo="owner/repo", timer_prefix="autoloop"),
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "gh":
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        if cmd[0] == "systemctl":
            return type(
                "R",
                (),
                {
                    "returncode": 0,
                    "stdout": TIMER_OUTPUT,
                    "stderr": "",
                },
            )()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with patch("subprocess.run", fake_run):
        import autoloop.cli

        autoloop.cli._show_status()

    out = capsys.readouterr().out
    assert "none found" in out


# --- Config: timer_prefix ---


def test_timer_prefix_default():
    from autoloop.config import AutoLoopConfig

    assert AutoLoopConfig().timer_prefix == "autoloop"


def test_timer_prefix_loaded_from_toml(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('timer_prefix = "patina"\n')

    from autoloop.config import load_config

    config = load_config(toml_path)
    assert config.timer_prefix == "patina"


def test_timer_prefix_keeps_default_when_not_in_toml(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "owner/repo"\n')

    from autoloop.config import load_config

    config = load_config(toml_path)
    assert config.timer_prefix == "autoloop"
