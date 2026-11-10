"""Backfill Jev shadow decisions.

Usage:
    OPENROUTER_API_KEY=<key> uv run python scripts/backfill_jev.py [--dry-run]
    uv run python scripts/backfill_jev.py --point auto-merge --dry-run

Triage (default): reads jev_decisions.jsonl in the RepoContext data dir
(~/.autoloop/<id>/, the same file auto-merge mode and JEV.md use) for 403 entries
with issue numbers, fetches the current issue body from the current repo (the
working directory's checkout, which matches what the incumbent saw), calls Jev
triage(), and appends a new record tagged as "backfill" to that file. Safe to run
multiple times — skips issues that already have a non-fallback entry or any earlier
backfill record, fallback or not. --dry-run lists the issues and writes nothing.

Auto-merge (--point auto-merge): discovers closed or merged `autoloop/<issue>-…`
PRs, skips those already recorded in the RepoContext data dir or with no autoloop
review comment, calls Jev should_auto_merge() in shadow mode on the current issue
text and the PR's first-commit diff, and appends one record per PR tagged as
"backfill". Append only; safe to run multiple times. --dry-run lists the PRs and
skip counts, makes no Jev calls, and writes nothing.
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from autoloop.jev_log import (
    decisions_path,
    discover_autoloop_prs,
    load_decisions,
    log_decision,
    plan_auto_merge_backfill,
)

BACKFILL_INPUTS = {"issue_text": "current", "diff": "first_commit"}


def is_triage_backfilled(records: list[dict], issue_number: int) -> bool:
    """True when the issue has a triage record tagged `backfill: true`, fallback or not."""
    return any(
        r.get("backfill")
        and r.get("point", "triage") == "triage"
        and r.get("issue") == issue_number
        for r in records
    )


def backfill_triage(ctx, dry_run: bool) -> None:
    log_file = decisions_path(ctx)
    print(f"Decisions file: {log_file}")
    if not log_file.exists():
        print("No jev_decisions.jsonl found")
        return

    entries = load_decisions(ctx)

    # Find issues with 403 failures
    failed_issues = set()
    succeeded_issues = set()
    incumbent_verdicts = {}
    for e in entries:
        issue = e.get("issue")
        if not issue:
            continue
        jev = e.get("jev") or {}
        reason = jev.get("reason", "")
        if "403" in reason or "400" in reason:
            failed_issues.add(issue)
            incumbent_verdicts[issue] = e.get("incumbent")
        elif not jev.get("fallback", True):
            succeeded_issues.add(issue)

    # Skip issues that already have real Jev data or an earlier backfill
    backfill_issues = sorted(
        i for i in failed_issues - succeeded_issues if not is_triage_backfilled(entries, i)
    )
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
                "--json",
                "title,body",
            ],
            capture_output=True,
            text=True,
            cwd=ctx.repo_dir,
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

        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        wf = jev_result.get("well_formed", "fallback")
        nd = jev_result.get("needs_decomposition", "—")
        fb = " (fallback)" if jev_result.get("fallback") else ""
        print(f"  #{issue_num}: well_formed={wf} needs_decomp={nd}{fb}")

    print("Done")


def auto_merge_backfill_record(cfg, candidate: dict) -> dict:
    """Call Jev should_auto_merge in shadow mode and build the tagged backfill record."""
    from autoloop.jev import should_auto_merge

    t0 = time.monotonic()
    jev_result = should_auto_merge(
        candidate["issue_text"],
        candidate["diff"],
        api_key_env=cfg.jev_api_key_env,
        model=cfg.jev_model,
        timeout=cfg.jev_timeout_seconds,
        gate_low=cfg.jev_gate_low,
        gate_high=cfg.jev_gate_high,
        mode="shadow",
    )
    latency = time.monotonic() - t0
    return {
        "point": "auto-merge",
        "issue": candidate["issue"],
        "pr": candidate["pr"],
        "jev_call": jev_result,
        "incumbent_call": candidate["incumbent_call"],
        "outcome": "incumbent",
        "ttft": round(jev_result.get("latency", latency), 3),
        "cost": jev_result.get("cost", 0.0),
        "timestamp": candidate["created_at"],
        "backfill": True,
        "backfill_inputs": dict(BACKFILL_INPUTS),
        "final_outcome": candidate["final_outcome"],
        "backfilled_at": datetime.now(UTC).isoformat(),
    }


def backfill_auto_merge(cfg, ctx, dry_run: bool) -> int:
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
        label = (
            f"  PR #{c['pr']} (issue #{c['issue']}, {c['branch']}): "
            f"incumbent approved={approved}, diff {len(c['diff'])} chars"
        )
        if dry_run:
            print(f"{label} — would call Jev should_auto_merge (dry-run)")
            continue
        record = auto_merge_backfill_record(cfg, c)
        if record["jev_call"].get("fallback"):
            # Outage: leave the PR unrecorded so a later run can retry it.
            print(f"{label} — Jev error: {record['jev_call'].get('reason', '')}")
            skips["jev_error"] = skips.get("jev_error", 0) + 1
            continue
        log_decision(record, ctx)
        mac = record["jev_call"].get("meets_acceptance_criteria")
        print(f"{label} — jev meets_acceptance_criteria={mac}")
    print(f"Skipped: {sum(skips.values())}")
    for reason, count in sorted(skips.items()):
        print(f"  {reason}: {count}")
    return 0


def main(argv=None, *, cfg=None, ctx=None) -> int:
    parser = argparse.ArgumentParser(description="Backfill Jev shadow decisions")
    parser.add_argument("--point", choices=("triage", "auto-merge"), default="triage")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from autoloop.config import RepoContext, load_config

    ctx = ctx or RepoContext(Path.cwd())
    if args.point == "auto-merge":
        cfg = cfg or load_config()
        return backfill_auto_merge(cfg, ctx, args.dry_run)

    backfill_triage(ctx, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
