"""Backfill Jev shadow decisions for issues that got 403 during the API key outage.

Usage:
    OPENROUTER_API_KEY=<key> uv run python scripts/backfill_jev.py [--dry-run]

Reads jev_decisions.jsonl for 403 entries with issue numbers, fetches the
current issue body from GitHub (which matches what the incumbent saw), calls
Jev triage(), and appends a new record tagged as "backfill".

Safe to run multiple times — skips issues that already have a non-fallback entry.
"""

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def main():
    dry_run = "--dry-run" in sys.argv

    log_file = Path("autoloop/jev_decisions.jsonl")
    if not log_file.exists():
        print("No jev_decisions.jsonl found")
        return

    entries = [json.loads(l) for l in log_file.read_text().strip().splitlines() if l.strip()]

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
            ["gh", "issue", "view", str(issue_num), "--repo",
             "Sanctum-Origo-Systems/autoloop", "--json", "title,body"],
            capture_output=True, text=True,
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


if __name__ == "__main__":
    main()
