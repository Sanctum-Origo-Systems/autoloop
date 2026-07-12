"""Update CHANGELOG.md and close completed parent issues.

Run daily via cron or manually:
    autoloop update-changelog
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autoloop.config import AutoLoopConfig

ROLLING_DAYS = 14
REPO_DIR = Path.cwd()


def fetch_merged_prs(cfg: AutoLoopConfig, since_days: int = 7) -> list[dict]:
    """Fetch PRs merged in the last N days."""
    since = (datetime.now(UTC) - timedelta(days=since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            cfg.repo,
            "--state",
            "merged",
            "--json",
            "number,title,body,mergedAt",
            "--limit",
            "50",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    prs = json.loads(result.stdout)
    return [p for p in prs if p.get("mergedAt", "") >= since]


def extract_cost(body: str) -> float:
    """Extract cost from PR body's AutoLoop Run Stats section."""
    match = re.search(r"- Cost: \$(\d+\.?\d*)", body or "")
    return float(match.group(1)) if match else 0.0


def extract_issue_number(body: str) -> int | None:
    """Extract linked issue number from Closes #N."""
    match = re.search(r"Closes #(\d+)", body or "")
    return int(match.group(1)) if match else None


def existing_entries(repo_dir: Path) -> set[int]:
    """Return PR numbers already in the changelog."""
    changelog = repo_dir / "eval" / "cognitive" / "CHANGELOG.md"
    if not changelog.exists():
        return set()
    numbers = set()
    for match in re.finditer(r"PR #(\d+)", changelog.read_text()):
        numbers.add(int(match.group(1)))
    return numbers


def append_entries(prs: list[dict], repo_dir: Path) -> int:
    """Append new PR entries to CHANGELOG.md. Returns count added."""
    changelog = repo_dir / "eval" / "cognitive" / "CHANGELOG.md"
    known = existing_entries(repo_dir)
    new_prs = [p for p in prs if p["number"] not in known]
    if not new_prs:
        return 0

    changelog.parent.mkdir(parents=True, exist_ok=True)

    by_date: dict[str, list[str]] = {}
    for pr in sorted(new_prs, key=lambda p: p["mergedAt"]):
        date = pr["mergedAt"][:10]
        cost = extract_cost(pr.get("body", ""))
        issue = extract_issue_number(pr.get("body", ""))
        cost_str = f" (${cost:.2f})" if cost else ""
        issue_str = f" #{issue}" if issue else ""
        entry = f"- {pr['title']}{issue_str}{cost_str} (PR #{pr['number']})"
        by_date.setdefault(date, []).append(entry)

    new_lines = []
    for date in sorted(by_date.keys(), reverse=True):
        new_lines.append(f"\n## {date}")
        new_lines.extend(by_date[date])

    if changelog.exists():
        existing_text = changelog.read_text()
    else:
        existing_text = "# Cognitive Changelog\n"

    header = "# Cognitive Changelog\n"
    rest = existing_text.replace(header, "", 1)
    changelog.write_text(header + "\n".join(new_lines) + "\n" + rest)

    return len(new_prs)


def trim_changelog(repo_dir: Path) -> None:
    """Move entries older than ROLLING_DAYS to the archive."""
    changelog = repo_dir / "eval" / "cognitive" / "CHANGELOG.md"
    archive = repo_dir / "eval" / "cognitive" / "changelog-archive.md"
    if not changelog.exists():
        return

    cutoff = (datetime.now(UTC) - timedelta(days=ROLLING_DAYS)).strftime("%Y-%m-%d")
    lines = changelog.read_text().split("\n")

    keep: list[str] = []
    archive_lines: list[str] = []
    current_date = None

    for line in lines:
        date_match = re.match(r"^## (\d{4}-\d{2}-\d{2})", line)
        if date_match:
            current_date = date_match.group(1)

        if current_date and current_date < cutoff:
            archive_lines.append(line)
        else:
            keep.append(line)

    if archive_lines:
        changelog.write_text("\n".join(keep))
        archive_text = "\n".join(archive_lines) + "\n"
        if archive.exists():
            archive_text = archive_text + archive.read_text()
        archive.write_text(archive_text)


def close_completed_parents(cfg: AutoLoopConfig) -> None:
    """Close parent issues whose sub-issues are all complete."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            cfg.repo,
            "--label",
            "needs-decomposition",
            "--state",
            "open",
            "--json",
            "number",
            "--limit",
            "50",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    for parent in json.loads(result.stdout):
        parent_num = parent["number"]
        all_issues = subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                cfg.repo,
                "--state",
                "all",
                "--json",
                "number,body,state",
                "--limit",
                "100",
                "--search",
                f"Parent issue: #{parent_num}",
            ],
            capture_output=True,
            text=True,
        )
        if all_issues.returncode != 0:
            continue
        subs = [
            i
            for i in json.loads(all_issues.stdout)
            if f"Parent issue: #{parent_num}" in (i.get("body") or "")
        ]
        if subs and all(s["state"] == "CLOSED" for s in subs):
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "close",
                    str(parent_num),
                    "--repo",
                    cfg.repo,
                    "--comment",
                    f"Auto-closed: All {len(subs)} sub-issues complete.",
                ],
            )
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(parent_num),
                    "--repo",
                    cfg.repo,
                    "--remove-label",
                    "needs-decomposition",
                ],
            )
            print(f"  Closed parent #{parent_num}")


def main():
    from autoloop.config import load_config

    cfg = load_config()
    subprocess.run(["git", "checkout", "main"], cwd=REPO_DIR)
    subprocess.run(["git", "pull", "--ff-only", "origin", "main"], cwd=REPO_DIR)

    prs = fetch_merged_prs(cfg, since_days=ROLLING_DAYS)
    added = append_entries(prs, REPO_DIR)
    trim_changelog(REPO_DIR)

    close_completed_parents(cfg)

    if added:
        print(f"Added {added} new entries to CHANGELOG.md")
    else:
        print("No new entries to add.")


if __name__ == "__main__":
    main()
