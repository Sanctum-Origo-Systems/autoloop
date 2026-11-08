"""Backfill Jev shadow decisions.

Usage:
    OPENROUTER_API_KEY=<key> uv run python scripts/backfill_jev.py [--dry-run]
    uv run python scripts/backfill_jev.py --point auto-merge --dry-run

Triage (default): reads jev_decisions.jsonl for 403 entries with issue numbers,
fetches the current issue body from GitHub (which matches what the incumbent
saw), calls Jev triage(), and appends a new record tagged as "backfill".
Safe to run multiple times — skips issues that already have a non-fallback entry.

Auto-merge (--point auto-merge): discovers `autoloop/<issue>-…` PRs, skips
those already recorded in the RepoContext data dir or with no autoloop review
comment, and lists the rest. Only --dry-run is supported; it makes no Jev calls
and writes nothing.
"""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from autoloop.jev_log import (
    decisions_path,
    discover_autoloop_prs,
    load_decisions,
    plan_auto_merge_backfill,
)


def backfill_triage(dry_run: bool) -> None:
    log_file = Path("autoloop/jev_decisions.jsonl")
    if not log_file.exists():
        print("No jev_decisions.jsonl found")
        return

    entries = [
        json.loads(line) for line in log_file.read_text().strip().splitlines() if line.strip()
    ]

    # Find issues with 403 failures
    failed_issues = set()
    succeeded_issues = set()
    incumbent_verdicts = {}
    for e in entries:
        issue = e.get("issue")
        if not issue:
            continue
        jev = e.get("jev", {})
        reason = jev.get("reason", "") if jev else ""
        if "403" in reason or "400" in reason:
            failed_issues.add(issue)
            incumbent_verdicts[issue] = e.get("incumbent")
        elif not jev.get("fallback", True):
            succeeded_issues.add(issue)

    # Skip issues that already have real Jev data
    backfill_issues = sorted(failed_issues - succeeded_issues)
    print(f"Issues to backfill: {len(backfill_issues)}")
    if not backfill_issues:
        print("Nothing to backfill")
        return

    for issue_num in backfill_issues:
        # Fetch current issue body from GitHub
        result = subprocess.run(
            [
                "gh",
                "issue",
                "view",
                str(issue_num),
                "--repo",
                "Sanctum-Origo-Systems/autoloop",
                "--json",
                "title,body",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  #{issue_num}: failed to fetch issue — skipping")
            continue

        data = json.loads(result.stdout)
        issue_text = f"Title: {data.get('title', '')}\n\nBody:\n{data.get('body', '')}"

        if dry_run:
            print(f"  #{issue_num}: would call Jev triage (dry-run)")
            continue

        # Call Jev
        try:
            from autoloop.jev import triage

            jev_result = triage(issue_text)
        except Exception as exc:
            print(f"  #{issue_num}: Jev error — {exc}")
            continue

        # Build backfill record
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "issue": issue_num,
            "incumbent": incumbent_verdicts.get(issue_num),
            "jev": jev_result,
            "error": None,
            "backfill": True,
        }

        with open(log_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        wf = jev_result.get("well_formed", "fallback")
        nd = jev_result.get("needs_decomposition", "—")
        fb = " (fallback)" if jev_result.get("fallback") else ""
        print(f"  #{issue_num}: well_formed={wf} needs_decomp={nd}{fb}")

    print("Done")


def backfill_auto_merge(cfg, ctx, dry_run: bool) -> int:
    if not dry_run:
        print("Auto-merge backfill only supports --dry-run for now")
        return 1

    print(f"Decisions file: {decisions_path(ctx)}")
    records = load_decisions(ctx)
    try:
        prs = discover_autoloop_prs(cfg)
    except RuntimeError as exc:
        print(exc)
        return 1

    candidates, skips = plan_auto_merge_backfill(cfg, prs, records)
    print(f"PRs to backfill: {len(candidates)}")
    for c in candidates:
        approved = c["incumbent_call"]["approved"]
        print(
            f"  PR #{c['pr']} (issue #{c['issue']}, {c['branch']}): "
            f"incumbent approved={approved}, diff {len(c['diff'])} chars — "
            "would call Jev should_auto_merge (dry-run)"
        )
    print(f"Skipped: {sum(skips.values())}")
    for reason, count in sorted(skips.items()):
        print(f"  {reason}: {count}")
    return 0


def main(argv=None, *, cfg=None, ctx=None) -> int:
    parser = argparse.ArgumentParser(description="Backfill Jev shadow decisions")
    parser.add_argument("--point", choices=("triage", "auto-merge"), default="triage")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.point == "auto-merge":
        from autoloop.config import RepoContext, load_config

        cfg = cfg or load_config()
        ctx = ctx or RepoContext(Path.cwd())
        return backfill_auto_merge(cfg, ctx, args.dry_run)

    backfill_triage(args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
