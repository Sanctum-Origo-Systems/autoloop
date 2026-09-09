"""Tests for the review-pr CLI subcommand."""

from __future__ import annotations

import json
from pathlib import Path
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


def _claude_result(
    text="", success=True, cost_usd=0, input_tokens=0, output_tokens=0, cache_read_tokens=0
):
    return SimpleNamespace(
        text=text,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
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
        assert result["success"] is False
        checkout_calls = [
            c
            for c in mock_run.call_args_list
            if c
            == call(
                ["gh", "pr", "checkout", "42", "--repo", "acme-corp/widget"],
                capture_output=True,
                text=True,
            )
        ]
        assert len(checkout_calls) == 1

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
        assert result["success"] is True

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

        assert result["success"] is False
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

        assert result["success"] is False
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


class TestReviewPrBranchRestore:
    def test_restores_original_branch_on_success(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix", "body": ""})
        review_json = json.dumps({"approved": True, "summary": "ok"})

        all_calls = []

        def tracking_dispatch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            all_calls.append(cmd)
            if isinstance(cmd, list) and cmd[:2] == ["git", "rev-parse"]:
                return _ok(stdout="main\n")
            return _make_dispatcher(pr_data)(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=tracking_dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json),
            ),
        ):
            review_pr(42, cfg)

        restore_calls = [
            c
            for c in all_calls
            if isinstance(c, list) and c[:2] == ["git", "checkout"] and "main" in c
        ]
        assert len(restore_calls) == 1

    def test_restores_original_branch_on_failure(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix", "body": ""})
        review_json = json.dumps({"approved": False, "issues": ["bad"], "summary": "no"})

        all_calls = []

        def tracking_dispatch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            all_calls.append(cmd)
            if isinstance(cmd, list) and cmd[:2] == ["git", "rev-parse"]:
                return _ok(stdout="my-feature\n")
            return _make_dispatcher(pr_data)(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=tracking_dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json),
            ),
        ):
            review_pr(42, cfg)

        restore_calls = [
            c for c in all_calls if isinstance(c, list) and c == ["git", "checkout", "my-feature"]
        ]
        assert len(restore_calls) == 1


class TestReviewPrCostTracking:
    def test_success_logs_review_entry_with_cost_fields(self, tmp_path):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix bug", "body": ""})
        review_json = json.dumps({"approved": True, "summary": "looks good"})
        dispatch = _make_dispatcher(pr_data)
        log_file = tmp_path / "autoloop" / "run_history.jsonl"

        with (
            patch("subprocess.run", side_effect=dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(
                    text=review_json,
                    cost_usd=0.12,
                    input_tokens=1500,
                    output_tokens=300,
                    cache_read_tokens=100,
                ),
            ),
            patch("autoloop.implement_issue.LOG_FILE", log_file),
        ):
            result = review_pr(42, cfg)

        assert result["success"] is True
        assert result["cost_usd"] == 0.12
        assert result["input_tokens"] == 1500
        assert result["output_tokens"] == 300
        assert result["cache_read_tokens"] == 100
        assert log_file.exists()
        entry = json.loads(log_file.read_text().strip())
        assert entry["type"] == "review"
        assert entry["pr_number"] == 42
        assert entry["success"] is True
        assert entry["cost_usd"] == 0.12
        assert entry["input_tokens"] == 1500
        assert entry["output_tokens"] == 300
        assert entry["cache_read_tokens"] == 100
        assert "duration_seconds" in entry
        assert "timestamp" in entry

    def test_failure_logs_review_entry(self, tmp_path):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix bug", "body": ""})
        review_json = json.dumps(
            {"approved": False, "issues": ["missing tests"], "summary": "needs work"}
        )
        dispatch = _make_dispatcher(pr_data)
        log_file = tmp_path / "autoloop" / "run_history.jsonl"

        with (
            patch("subprocess.run", side_effect=dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json, cost_usd=0.08),
            ),
            patch("autoloop.implement_issue.LOG_FILE", log_file),
        ):
            result = review_pr(42, cfg)

        assert result["success"] is False
        assert result["cost_usd"] == 0.08
        entry = json.loads(log_file.read_text().strip())
        assert entry["type"] == "review"
        assert entry["pr_number"] == 42
        assert entry["success"] is False
        assert entry["cost_usd"] == 0.08

    def test_success_comment_includes_cost_line(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix bug", "body": ""})
        review_json = json.dumps({"approved": True, "summary": "looks good"})
        dispatch = _make_dispatcher(pr_data)

        all_calls = []
        original_dispatch = dispatch

        def tracking_dispatch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            all_calls.append((cmd, kwargs))
            return original_dispatch(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=tracking_dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(
                    text=review_json, cost_usd=0.15, input_tokens=2000, output_tokens=500
                ),
            ),
            patch("autoloop.implement_issue.LOG_FILE", Path("/dev/null")),
        ):
            review_pr(42, cfg)

        comment_calls = [
            c for c, kw in all_calls if isinstance(c, list) and c[:3] == ["gh", "pr", "comment"]
        ]
        assert len(comment_calls) >= 1
        body_idx = comment_calls[0].index("--body") + 1
        body = comment_calls[0][body_idx]
        assert "Review cost:" in body
        assert "$0.15" in body
        assert "2,000 input" in body
        assert "500 output" in body

    def test_failure_comment_includes_cost_line(self):
        cfg = _cfg()
        pr_data = json.dumps({"headRefName": "fix/42", "title": "Fix bug", "body": ""})
        review_json = json.dumps({"approved": False, "issues": ["bad tests"], "summary": "no"})
        dispatch = _make_dispatcher(pr_data)

        all_calls = []
        original_dispatch = dispatch

        def tracking_dispatch(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            all_calls.append((cmd, kwargs))
            return original_dispatch(*args, **kwargs)

        with (
            patch("subprocess.run", side_effect=tracking_dispatch),
            patch(
                "autoloop.claude_runner.run_claude",
                return_value=_claude_result(text=review_json, cost_usd=0.10),
            ),
            patch("autoloop.implement_issue.LOG_FILE", Path("/dev/null")),
        ):
            review_pr(42, cfg)

        comment_calls = [
            c for c, kw in all_calls if isinstance(c, list) and c[:3] == ["gh", "pr", "comment"]
        ]
        assert len(comment_calls) >= 1
        body_idx = comment_calls[0].index("--body") + 1
        body = comment_calls[0][body_idx]
        assert "Review cost:" in body
        assert "$0.10" in body
