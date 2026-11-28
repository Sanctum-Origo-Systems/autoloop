from __future__ import annotations

from unittest.mock import MagicMock, patch

from autoloop.cli import build_parser
from autoloop.config import AutoLoopConfig


def test_review_queue_subcommand_registered():
    parser = build_parser()
    args = parser.parse_args(["review-queue"])
    assert args.command == "review-queue"


def test_review_queue_dispatch_calls_refresh_once():
    cfg = AutoLoopConfig(repo="acme-corp/widget")
    refresh = MagicMock()

    with (
        patch("autoloop.review_queue.refresh_review_queue", refresh),
        patch("autoloop.config.load_config", return_value=cfg),
        patch("autoloop.config.RepoContext"),
        patch("sys.argv", ["autoloop", "review-queue"]),
    ):
        from autoloop.cli import main

        main()

    refresh.assert_called_once_with(cfg)


def test_review_queue_runs_no_claude_session():
    cfg = AutoLoopConfig(repo="acme-corp/widget")

    with (
        patch("autoloop.review_queue.refresh_review_queue"),
        patch("autoloop.config.load_config", return_value=cfg),
        patch("autoloop.config.RepoContext"),
        patch("autoloop.claude_runner.run_claude") as run_claude,
        patch("autoloop.implement_issue.detect_active_claude_session") as detect,
        patch("sys.argv", ["autoloop", "review-queue"]),
    ):
        from autoloop.cli import main

        main()

    run_claude.assert_not_called()
    detect.assert_not_called()
