from __future__ import annotations

from unittest.mock import patch

from autoloop.cli import build_parser


def test_init_test_pattern_flag_parsed():
    parser = build_parser()
    args = parser.parse_args(["init", "--repo", "acme/widgets", "--test-pattern", "*/tests/*"])
    assert args.test_pattern == "*/tests/*"


def test_init_test_pattern_defaults_to_none():
    parser = build_parser()
    args = parser.parse_args(["init", "--repo", "acme/widgets"])
    assert args.test_pattern is None


def test_init_dispatch_passes_flags_by_name():
    argv = [
        "autoloop",
        "init",
        "--repo",
        "acme/widgets",
        "--verify-cmd",
        "./scripts/verify.sh",
        "--test-pattern",
        "*/tests/*",
        "--dry-run",
        "--skip-labels",
    ]
    with (
        patch("autoloop.init.run_init") as run_init,
        patch("sys.argv", argv),
    ):
        from autoloop.cli import main

        main()

    run_init.assert_called_once_with(
        "acme/widgets",
        "",
        "./scripts/verify.sh",
        dry_run=True,
        skip_labels=True,
        test_pattern="*/tests/*",
        force=False,
    )
