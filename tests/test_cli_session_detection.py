"""Tests for session detection in CLI entry points (plan, fix-pr, review-pr)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autoloop.cli import main


def _cfg(**overrides):
    defaults = {
        "repo": "test-owner/test-repo",
        "triage_model": "sonnet",
        "project_dir": "",
        "implement_isolation": "off",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# --- plan ---


def test_plan_aborts_when_active_session_detected(monkeypatch, capsys, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("## Task 1: Foo\n\nBody.\n")
    monkeypatch.setattr("sys.argv", ["autoloop", "plan", "--from-spec", str(spec)])
    with (
        patch("autoloop.config.load_config", return_value=_cfg()),
        patch(
            "autoloop.implement_issue.detect_active_claude_session",
            return_value=True,
        ),
    ):
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "Active Claude Code session detected" in out


def test_plan_proceeds_when_no_active_session(monkeypatch, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("## Task 1: Foo\n\nBody.\n")
    monkeypatch.setattr("sys.argv", ["autoloop", "plan", "--from-spec", str(spec)])
    with (
        patch("autoloop.config.load_config", return_value=_cfg()),
        patch(
            "autoloop.implement_issue.detect_active_claude_session",
            return_value=False,
        ),
        patch("autoloop.create_issue.create_issues_from_spec") as mock_create,
    ):
        main()
    mock_create.assert_called_once()


# --- fix-pr ---


def test_fix_pr_never_calls_session_detection(monkeypatch):
    """fix-pr must not call detect_active_claude_session in any code path."""
    monkeypatch.setattr("sys.argv", ["autoloop", "fix-pr", "42"])
    with (
        patch("autoloop.config.load_config", return_value=_cfg()),
        patch(
            "autoloop.implement_issue.detect_active_claude_session",
        ) as mock_detect,
        patch("autoloop.fix_pr.fix_pr", return_value=True),
    ):
        main()
    mock_detect.assert_not_called()


# --- review-pr ---


def test_review_pr_never_calls_session_detection(monkeypatch):
    """review-pr must not call detect_active_claude_session in any code path."""
    monkeypatch.setattr("sys.argv", ["autoloop", "review-pr", "42"])
    with (
        patch("autoloop.config.load_config", return_value=_cfg()),
        patch(
            "autoloop.implement_issue.detect_active_claude_session",
        ) as mock_detect,
        patch("autoloop.cli.review_pr", return_value={"success": True}),
    ):
        main()
    mock_detect.assert_not_called()
