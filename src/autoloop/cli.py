"""CLI entry point for autoloop."""

from __future__ import annotations

import argparse
import sys

from autoloop import __version__


def main():
    parser = argparse.ArgumentParser(
        prog="autoloop",
        description="Config-driven AI pipeline for triaging and implementing GitHub issues",
    )
    parser.add_argument("--version", action="version", version=f"autoloop {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser("init", help="Scaffold autoloop onto a new repo")
    init_parser.add_argument("--repo", required=True, help="GitHub owner/repo")
    init_parser.add_argument("--reviewer", default="", help="GitHub username for PR reviews")
    init_parser.add_argument(
        "--verify-cmd", default="uv run pytest", help="Verify command (default: uv run pytest)"
    )
    init_parser.add_argument("--dry-run", action="store_true", help="Preview without running")
    init_parser.add_argument("--skip-labels", action="store_true", help="Skip label creation")

    # plan
    plan_parser = subparsers.add_parser("plan", help="Create issues from a spec file")
    plan_parser.add_argument("--from-spec", required=True, metavar="PATH", help="Spec file path")
    plan_parser.add_argument(
        "--skip",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[],
        help="Enhancement numbers to skip (e.g. --skip 1,2)",
    )
    plan_parser.add_argument("--dry-run", action="store_true", help="Print without creating")

    # triage
    subparsers.add_parser("triage", help="Triage untriaged issues")

    # implement
    impl_parser = subparsers.add_parser("implement", help="Implement top ready issue")
    impl_parser.add_argument("--issue", type=int, metavar="NUMBER", help="Specific issue number")
    impl_parser.add_argument(
        "--max-issues", type=int, default=1, help="Max issues to implement (default: 1)"
    )
    impl_parser.add_argument(
        "--require-design", action="store_true", help="Require design review first"
    )

    # status
    subparsers.add_parser("status", help="Show last run, ready issues, next scheduled timers")

    # fix-pr
    fix_parser = subparsers.add_parser(
        "fix-pr", help="Fix a PR by rebasing on main and resolving conflicts"
    )
    fix_parser.add_argument("pr_number", type=int, help="PR number to fix")

    # auto-close-parent
    acp_parser = subparsers.add_parser(
        "auto-close-parent", help="Close parent issue when all sub-issues are done"
    )
    acp_parser.add_argument("pr_number", type=int, help="PR number to check")

    # version (also accessible via --version)
    subparsers.add_parser("version", help="Print installed version")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "version":
        print(f"autoloop {__version__}")

    elif args.command == "init":
        from autoloop.init import run_init

        run_init(args.repo, args.reviewer, args.verify_cmd, args.dry_run, args.skip_labels)

    elif args.command == "plan":
        from autoloop.config import load_config
        from autoloop.create_issue import create_issues_from_spec

        cfg = load_config()
        create_issues_from_spec(args.from_spec, skip=args.skip, cfg=cfg, dry_run=args.dry_run)

    elif args.command == "triage":
        from autoloop.triage_issues import main as triage_main

        triage_main()

    elif args.command == "implement":
        from autoloop.implement_issue import main as implement_main

        implement_main(
            issue=args.issue,
            max_issues=args.max_issues,
            require_design=args.require_design,
        )

    elif args.command == "status":
        _show_status()

    elif args.command == "fix-pr":
        from autoloop.config import load_config
        from autoloop.fix_pr import fix_pr

        cfg = load_config()
        success = fix_pr(args.pr_number, cfg)
        if not success:
            sys.exit(1)

    elif args.command == "auto-close-parent":
        from autoloop.auto_close_parent import check_and_close_parent
        from autoloop.config import load_config

        cfg = load_config()
        result = check_and_close_parent(args.pr_number, cfg=cfg)
        if result:
            print(f"Closed parent issue #{result}")
        else:
            print("No parent issue to close.")


def _show_status():
    """Show last run, ready issues, and next scheduled timers."""
    import json
    from pathlib import Path

    from autoloop.config import load_config

    cfg = load_config()

    log_file = Path.cwd() / "autoloop" / "run_history.jsonl"
    if log_file.exists():
        lines = log_file.read_text().strip().splitlines()
        if lines:
            last = json.loads(lines[-1])
            print(
                f"Last run: issue #{last['issue']} — "
                f"{'success' if last['success'] else 'failed'} — "
                f"${last.get('cost_usd', 0):.2f} — {last['timestamp']}"
            )
        else:
            print("No run history yet.")
    else:
        print("No run history yet.")

    import subprocess

    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            cfg.repo,
            "--label",
            "ready",
            "--state",
            "open",
            "--json",
            "number",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        issues = json.loads(result.stdout)
        print(f"Ready issues: {len(issues)}")
    else:
        print("Ready issues: (could not query)")

    prefix = cfg.timer_prefix
    try:
        timer_result = subprocess.run(
            ["systemctl", "--user", "list-timers"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        timer_result = None
    if timer_result and timer_result.returncode == 0 and prefix in timer_result.stdout:
        for line in timer_result.stdout.splitlines():
            if prefix in line:
                print(f"Timer: {line.strip()}")
    else:
        print("Scheduled timers: none found")


if __name__ == "__main__":
    main()
