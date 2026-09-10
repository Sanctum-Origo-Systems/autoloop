"""CLI entry point for autoloop."""

from __future__ import annotations

import argparse
import json
import subprocess
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

    # review-pr
    review_parser = subparsers.add_parser(
        "review-pr", help="Review a PR (mutation gate + semantic review, no merge)"
    )
    review_parser.add_argument("pr_number", type=int, help="PR number to review")

    # auto-close-parent
    acp_parser = subparsers.add_parser(
        "auto-close-parent", help="Close parent issue when all sub-issues are done"
    )
    acp_parser.add_argument("pr_number", type=int, help="PR number to check")

    # doctor
    subparsers.add_parser("doctor", help="Run environment and config checks")

    # preflight
    subparsers.add_parser("preflight", help="Run verify and lint commands on the current branch")

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

    elif args.command == "review-pr":
        from autoloop.config import load_config

        cfg = load_config()
        result = review_pr(args.pr_number, cfg)
        if not result["success"]:
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

    elif args.command == "doctor":
        from autoloop.doctor import get_checks, run_checks

        results = run_checks(get_checks())
        if any(not r.passed for r in results):
            sys.exit(1)

    elif args.command == "preflight":
        from autoloop.config import load_config
        from autoloop.preflight import run_preflight

        cfg = load_config()
        results = run_preflight(cfg)
        any_failed = False
        for name, result in results.items():
            if result["skipped"]:
                print(f"  {name}: skipped")
                continue
            status = "pass" if result["passed"] else "FAIL"
            print(f"  {name}: {status} ({result['elapsed']:.2f}s)")
            if not result["passed"]:
                any_failed = True
                print(result["output"])
        sys.exit(1 if any_failed else 0)


def review_pr(pr_number, cfg):
    """Review a PR: checkout, run mutation gate + semantic review, post findings.

    Never merges. Applies needs-human label on failure.
    Restores the previous branch after review.
    Returns a dict with keys: success, cost_usd, input_tokens, output_tokens,
    cache_read_tokens.
    """
    import time

    import autoloop.implement_issue as impl
    from autoloop.claude_runner import run_claude
    from autoloop.config import REPO_DIR

    _zero_result = {
        "success": False,
        "cost_usd": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
    }

    impl.cfg = cfg
    start_time = time.time()

    original_branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    ).stdout.strip()

    checkout = subprocess.run(
        ["gh", "pr", "checkout", str(pr_number), "--repo", cfg.repo],
        capture_output=True,
        text=True,
    )
    if checkout.returncode != 0:
        print(f"Failed to checkout PR #{pr_number}")
        elapsed = time.time() - start_time
        impl.log_run(
            issue_number=0,
            success=False,
            attempts=1,
            duration=elapsed,
            cost_usd=0,
            run_type="review",
            pr_number=pr_number,
        )
        return _zero_result

    try:
        pr_view = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                cfg.repo,
                "--json",
                "headRefName,title,body",
            ],
            capture_output=True,
            text=True,
        )
        if pr_view.returncode != 0:
            print(f"Failed to get PR #{pr_number} info")
            elapsed = time.time() - start_time
            impl.log_run(
                issue_number=0,
                success=False,
                attempts=1,
                duration=elapsed,
                cost_usd=0,
                run_type="review",
                pr_number=pr_number,
            )
            return _zero_result

        pr_data = json.loads(pr_view.stdout)
        branch = pr_data["headRefName"]
        body = pr_data.get("body", "") or ""

        gate_passed, gate_errors = impl.verify_implementation(branch, issue_body=body)

        diff = subprocess.run(
            ["git", "diff", f"main..{branch}"],
            capture_output=True,
            text=True,
            cwd=REPO_DIR,
        ).stdout

        prompt = impl.REVIEW_PROMPT.format(
            number=pr_number,
            title=pr_data["title"],
            issue_body=body,
            diff=diff[: cfg.diff_truncation],
        )
        result = run_claude(prompt, cfg.review_model, cfg.impl_timeout)
        if result.success:
            review_passed, review_feedback = impl.parse_review_response(result.text)
        else:
            review_passed, review_feedback = (
                False,
                "Review call failed (timeout or non-zero exit).",
            )

        cost_line = (
            f"\n\n**Review cost:** ${result.cost_usd:.2f}, "
            f"tokens: {result.input_tokens:,} input / {result.output_tokens:,} output"
        )

        findings = []
        if not gate_passed:
            findings.append(f"**Mutation gate failed:**\n```\n{gate_errors}\n```")
        if not review_passed:
            findings.append(f"**Semantic review failed:**\n{review_feedback}")

        elapsed = time.time() - start_time
        success = not findings

        if findings:
            comment = "\n\n".join(findings) + cost_line
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(pr_number),
                    "--repo",
                    cfg.repo,
                    "--body",
                    comment,
                ],
            )
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "edit",
                    str(pr_number),
                    "--repo",
                    cfg.repo,
                    "--add-label",
                    "needs-human",
                ],
            )
        else:
            comment = (
                "**Review passed:** mutation gate and semantic review both passed." + cost_line
            )
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "comment",
                    str(pr_number),
                    "--repo",
                    cfg.repo,
                    "--body",
                    comment,
                ],
            )

        impl.log_run(
            issue_number=0,
            success=success,
            attempts=1,
            duration=elapsed,
            cost_usd=result.cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            run_type="review",
            pr_number=pr_number,
        )

        if success:
            subprocess.run(
                [
                    "gh",
                    "pr",
                    "edit",
                    str(pr_number),
                    "--repo",
                    cfg.repo,
                    "--remove-label",
                    "needs-human",
                ],
                capture_output=True,
            )

        return {
            "success": success,
            "cost_usd": result.cost_usd,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cache_read_tokens": result.cache_read_tokens,
        }
    finally:
        subprocess.run(
            ["git", "checkout", original_branch],
            capture_output=True,
            cwd=REPO_DIR,
        )


def _show_status():
    """Show last run, ready issues, and next scheduled timers."""
    from autoloop.config import load_config
    from autoloop.mcp_server import _read_last_runs

    cfg = load_config()

    last_impl, last_review = _read_last_runs()
    if last_impl:
        print(
            f"Last implement: issue #{last_impl['issue']} — "
            f"{'success' if last_impl['success'] else 'failed'} — "
            f"${last_impl.get('cost_usd', 0):.2f} — {last_impl['timestamp']}"
        )
    if last_review:
        print(
            f"Last review: PR #{last_review['pr_number']} — "
            f"{'success' if last_review['success'] else 'failed'} — "
            f"${last_review.get('cost_usd', 0):.2f} — {last_review['timestamp']}"
        )
    if not last_impl and not last_review:
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
