"""Tests for the review-pr CLI subcommand."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import call, patch

import pytest

from autoloop.cli import main, review_pr
from autoloop.config import AutoLoopConfig


def _cfg(**overrides):
    defaults = {
        "repo": "acme-corp/widget",
        "impl_model": "opus",
        "review_model": "sonnet",
        "impl_timeout": 600,
        "test_timeout": 60,
        "verify_cmd": "echo ok",
        "lint_command": "",
        "test_pattern": "tests/*.py",
        "test_gate_skip_types": ["refactor", "docs", "chore"],
        "diff_truncation": 8000,
    }
    defaults.update(overrides)
    return AutoLoopConfig(**defaults)


def _ok(stdout="", returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


def _claude_result(text="", success=True):
    return SimpleNamespace(
        text=text,
        cost_usd=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        success=success,
        timed_out=False,
    )


def _make_dispatcher(pr_data, overrides=None):
    """Build a subprocess.run dispatcher keyed on command prefix."""
    table = {
        ("gh", "pr", "checkout"): _ok(),
        ("gh", "pr", "view"): _ok(stdout=pr_data),
        ("git", "rev-list"): _ok(stdout="3\n"),
        ("git", "diff", "--name-only"): _ok(stdout="tests/test_x.py"),
        ("git", "diff"): _ok(stdout="diff content"),
        ("gh", "pr", "comment"): _ok(),
        ("gh", "pr", "edit"): _ok(),
    }
    if overrides:
        table.update(overrides)

    def dispatch(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, str):
            return table.get(("shell",), _ok())
        for prefix in sorted(table, key=len, reverse=True):
            if tuple(cmd[: len(prefix)]) == prefix:
                return table[prefix]
        return _ok()

    return dispatch


class TestReviewPrHelp:
    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            with patch("sys.argv", ["autoloop", "review-pr", "--help"]):
                main()
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "review-pr" in captured.out

    def test_subcommand_in_usage(self, capsys):
        with pytest.raises(SystemExit):
            with patch("sys.argv", ["autoloop", "--help"]):
                main()
        captured = capsys.readouterr()
        assert "review-pr" in captured.out


class TestReviewPrHandler:
    def test_checkout_failure_returns_false(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix", "body": ""})
        dispatch = _make_dispatcher(pr_data, {("gh", "pr", "checkout"): _ok(returncode=1)})
        with patch("subprocess.run", side_effect=dispatch) as mock_run:
            result = review_pr(42, cfg)
        assert result is False
        assert mock_run.call_args_list[0] == call(
            ["gh", "pr", "checkout", "42", "--repo", "acme-corp/widget"],
            capture_output=True,
            text=True,
        )

    def test_full_success_path(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix bug", "body": "## Type\nbug"})
        review_json = json.dumps({"approved": True, "summary": "looks good"})
        dispatch = _make_dispatcher(pr_data)

        with (
            patch("subprocess.run", side_effect=dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json),
            ),
        ):
            result = review_pr(42, cfg)
        assert result is True

    def test_gate_failure_posts_comment_and_label(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix bug", "body": ""})
        review_json = json.dumps({"approved": True, "summary": "ok"})
        dispatch = _make_dispatcher(pr_data, {("git", "rev-list"): _ok(stdout="0\n")})

        all_calls = []
        original_dispatch = dispatch

        def tracking_dispatch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            all_calls.append(cmd)
            return original_dispatch(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=tracking_dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json),
            ),
        ):
            result = review_pr(42, cfg)

        assert result is False
        comment_calls = [
            c for c in all_calls if isinstance(c, list) and c[:3] == ["gh", "pr", "comment"]
        ]
        assert len(comment_calls) >= 1
        label_calls = [
            c
            for c in all_calls
            if isinstance(c, list) and "--add-label" in c and "needs-human" in c
        ]
        assert len(label_calls) == 1

    def test_review_failure_posts_comment_and_label(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix bug", "body": ""})
        review_json = json.dumps(
            {
                "approved": False,
                "issues": ["missing tests"],
                "summary": "needs work",
            }
        )
        dispatch = _make_dispatcher(pr_data)

        all_calls = []
        original_dispatch = dispatch

        def tracking_dispatch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            all_calls.append(cmd)
            return original_dispatch(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=tracking_dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json),
            ),
        ):
            result = review_pr(42, cfg)

        assert result is False
        comment_calls = [
            c for c in all_calls if isinstance(c, list) and c[:3] == ["gh", "pr", "comment"]
        ]
        assert len(comment_calls) >= 1

    def test_no_merge_command_on_failure(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix", "body": ""})
        review_json = json.dumps({"approved": False, "issues": ["bad"], "summary": "no"})
        dispatch = _make_dispatcher(pr_data)

        all_calls = []
        original_dispatch = dispatch

        def tracking_dispatch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            all_calls.append(cmd)
            return original_dispatch(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=tracking_dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json),
            ),
        ):
            review_pr(42, cfg)

        for cmd in all_calls:
            if isinstance(cmd, list):
                assert "merge" not in " ".join(str(c) for c in cmd)

    def test_uses_review_model_not_impl_model(self):
        cfg = _cfg(review_model="custom-review-model")
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix", "body": ""})
        review_json = json.dumps({"approved": True, "summary": "ok"})
        dispatch = _make_dispatcher(pr_data)

        with (
            patch("subprocess.run", side_effect=dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json),
            ) as mock_claude,
        ):
            review_pr(42, cfg)

        assert mock_claude.call_args[0][1] == "custom-review-model"

    def test_needs_human_label_applied_on_gate_failure(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix", "body": ""})
        review_json = json.dumps({"approved": True, "summary": "ok"})
        dispatch = _make_dispatcher(pr_data, {("git", "rev-list"): _ok(stdout="0\n")})

        all_calls = []
        original_dispatch = dispatch

        def tracking_dispatch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            all_calls.append(cmd)
            return original_dispatch(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=tracking_dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json),
            ),
        ):
            review_pr(42, cfg)

        label_calls = [
            c
            for c in all_calls
            if isinstance(c, list) and "--add-label" in c and "needs-human" in c
        ]
        assert len(label_calls) == 1
