from __future__ import annotations

import argparse


def _build_parser():
    """Build the implement subcommand parser matching cli.py."""
    parser = argparse.ArgumentParser(prog="autoloop")
    subparsers = parser.add_subparsers(dest="command")

    impl_parser = subparsers.add_parser("implement")
    impl_parser.add_argument("--issue", type=int, metavar="NUMBER")
    impl_parser.add_argument("--max-issues", type=int, default=1)
    impl_parser.add_argument("--require-design", action="store_true")
    impl_parser.add_argument("--auto-fix", action="store_true")
    impl_parser.add_argument("--max-pr-review-rounds", type=int, default=None, metavar="N")

    return parser


def test_implement_auto_fix_flag():
    parser = _build_parser()
    args = parser.parse_args(["implement", "--auto-fix"])
    assert args.auto_fix is True


def test_implement_no_auto_fix_by_default():
    parser = _build_parser()
    args = parser.parse_args(["implement"])
    assert args.auto_fix is False


def test_implement_max_pr_review_rounds_flag():
    parser = _build_parser()
    args = parser.parse_args(["implement", "--max-pr-review-rounds", "5"])
    assert args.max_pr_review_rounds == 5


def test_implement_max_pr_review_rounds_default_is_none():
    parser = _build_parser()
    args = parser.parse_args(["implement"])
    assert args.max_pr_review_rounds is None


def test_implement_auto_fix_with_max_rounds():
    parser = _build_parser()
    args = parser.parse_args(["implement", "--auto-fix", "--max-pr-review-rounds", "10"])
    assert args.auto_fix is True
    assert args.max_pr_review_rounds == 10


def test_implement_existing_flags_unchanged():
    parser = _build_parser()
    args = parser.parse_args(["implement", "--issue", "42", "--max-issues", "3", "--auto-fix"])
    assert args.issue == 42
    assert args.max_issues == 3
    assert args.auto_fix is True
    assert args.require_design is False
