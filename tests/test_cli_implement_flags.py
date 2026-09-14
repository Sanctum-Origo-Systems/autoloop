from __future__ import annotations

from autoloop.cli import build_parser


def test_implement_auto_fix_flag():
    parser = build_parser()
    args = parser.parse_args(["implement", "--auto-fix"])
    assert args.auto_fix is True


def test_implement_no_auto_fix_by_default():
    parser = build_parser()
    args = parser.parse_args(["implement"])
    assert args.auto_fix is False


def test_implement_max_pr_review_rounds_flag():
    parser = build_parser()
    args = parser.parse_args(["implement", "--max-pr-review-rounds", "5"])
    assert args.max_pr_review_rounds == 5


def test_implement_max_pr_review_rounds_default_is_none():
    parser = build_parser()
    args = parser.parse_args(["implement"])
    assert args.max_pr_review_rounds is None


def test_implement_auto_fix_with_max_rounds():
    parser = build_parser()
    args = parser.parse_args(["implement", "--auto-fix", "--max-pr-review-rounds", "10"])
    assert args.auto_fix is True
    assert args.max_pr_review_rounds == 10


def test_implement_existing_flags_unchanged():
    parser = build_parser()
    args = parser.parse_args(["implement", "--issue", "42", "--max-issues", "3", "--auto-fix"])
    assert args.issue == 42
    assert args.max_issues == 3
    assert args.auto_fix is True
    assert args.require_design is False
