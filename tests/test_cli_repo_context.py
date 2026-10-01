"""Tests that cli.py constructs and dispatches RepoContext to pipeline entry points."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autoloop.cli import main


def _mock_ctx(repo_dir=None, data_dir=None):
    ctx = SimpleNamespace()
    ctx.repo_dir = repo_dir or Path("/fake/repo")
    ctx.data_dir = data_dir or Path("/fake/data")
    ctx.worktree_dir = ctx.data_dir / "worktrees"
    return ctx


def _cfg(**overrides):
    defaults = {
        "repo": "test-owner/test-repo",
        "triage_model": "sonnet",
        "project_dir": "",
        "implement_isolation": "off",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestCliPassesCtxToTriage:
    def test_triage_receives_ctx_as_first_arg(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["autoloop", "triage"])
        mock_ctx = _mock_ctx(repo_dir=tmp_path)

        with (
            patch("autoloop.config.RepoContext", return_value=mock_ctx) as mock_rc,
            patch("autoloop.triage_issues.main") as mock_triage,
        ):
            main()

        mock_rc.assert_called_once()
        mock_triage.assert_called_once()
        args, kwargs = mock_triage.call_args
        assert args[0] is mock_ctx

    def test_triage_ctx_has_correct_repo_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["autoloop", "triage"])
        mock_ctx = _mock_ctx(repo_dir=tmp_path)

        with (
            patch("autoloop.config.RepoContext", return_value=mock_ctx),
            patch("autoloop.triage_issues.main") as mock_triage,
        ):
            main()

        ctx_passed = mock_triage.call_args[0][0]
        assert ctx_passed.repo_dir == tmp_path


class TestCliPassesCtxToImplement:
    def test_implement_receives_ctx_as_first_arg(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["autoloop", "implement"])
        mock_ctx = _mock_ctx(repo_dir=tmp_path)

        with (
            patch("autoloop.config.RepoContext", return_value=mock_ctx),
            patch("autoloop.config.load_config", return_value=_cfg()),
            patch("autoloop.implement_issue.main") as mock_impl,
        ):
            main()

        mock_impl.assert_called_once()
        args, kwargs = mock_impl.call_args
        assert args[0] is mock_ctx


class TestCliPassesCtxToFixPr:
    def test_fix_pr_receives_ctx(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["autoloop", "fix-pr", "42"])
        mock_ctx = _mock_ctx(repo_dir=tmp_path)

        with (
            patch("autoloop.config.RepoContext", return_value=mock_ctx),
            patch("autoloop.config.load_config", return_value=_cfg()),
            patch(
                "autoloop.implement_issue.detect_active_claude_session",
                return_value=False,
            ),
            patch("autoloop.fix_pr.fix_pr", return_value=True) as mock_fix,
        ):
            main()

        mock_fix.assert_called_once()
        assert mock_fix.call_args[0][0] is mock_ctx


class TestCliPassesCtxToAutoCloseParent:
    def test_auto_close_parent_receives_ctx(self, monkeypatch, tmp_path):
        monkeypatch.setattr("sys.argv", ["autoloop", "auto-close-parent", "42"])
        mock_ctx = _mock_ctx(repo_dir=tmp_path)

        with (
            patch("autoloop.config.RepoContext", return_value=mock_ctx),
            patch("autoloop.config.load_config", return_value=_cfg()),
            patch(
                "autoloop.auto_close_parent.check_and_close_parent", return_value=None
            ) as mock_acp,
        ):
            main()

        mock_acp.assert_called_once()
        assert mock_acp.call_args[0][0] is mock_ctx


class TestRepoContextUsesRepoDirNotCwd:
    def test_pipeline_uses_ctx_repo_dir_not_cwd(self, tmp_path, monkeypatch):
        """Verify ctx.repo_dir is used for file ops, not process cwd."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        monkeypatch.chdir(other_dir)

        from autoloop.implement_issue import acquire_lock, release_lock

        acquired = acquire_lock(repo_dir)
        assert acquired is True
        assert (repo_dir / ".autoloop.lock").exists()
        assert not (other_dir / ".autoloop.lock").exists()
        release_lock(repo_dir)
