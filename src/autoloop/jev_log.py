from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from autoloop.config import RepoContext

REQUIRED_KEYS = (
    "point",
    "issue",
    "jev_call",
    "incumbent_call",
    "outcome",
    "ttft",
    "cost",
    "timestamp",
)

DECISIONS_FILE = "jev_decisions.jsonl"
AUTOLOOP_BRANCH_RE = re.compile(r"^autoloop/(\d+)-")
REVIEW_PASSED_MARKER = "Review passed"
REVIEW_FAILED_MARKERS = ("Semantic review failed", "Review found issues")
FINAL_OUTCOMES = {"MERGED": "merged", "CLOSED": "closed_unmerged"}


def decisions_path(ctx: RepoContext) -> Path:
    return ctx.data_dir / DECISIONS_FILE


def log_decision(record: dict, ctx: RepoContext) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in record]
    if missing:
        raise ValueError(f"Missing required keys: {', '.join(missing)}")
    log_file = decisions_path(ctx)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as f:
        f.write(json.dumps(record) + "\n")


def load_decisions(ctx: RepoContext) -> list[dict]:
    """Read every decision record from the data dir, skipping blank or malformed lines."""
    log_file = decisions_path(ctx)
    if not log_file.exists():
        return []
    records = []
    for line in log_file.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def parse_autoloop_branch(branch: str) -> int | None:
    """Return the issue number from an `autoloop/<issue>-…` branch name, else None."""
    match = AUTOLOOP_BRANCH_RE.match(branch or "")
    return int(match.group(1)) if match else None


def discover_autoloop_prs(cfg, limit: int = 1000) -> list[dict]:
    """List PRs (any state) whose head branch is `autoloop/<issue>-…`.

    Raises RuntimeError when `gh pr list` fails.
    """
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            cfg.repo,
            "--state",
            "all",
            "--limit",
            str(limit),
            "--json",
            "number,headRefName,baseRefName,title,createdAt,state",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh pr list failed: {result.stderr.strip()}")
    prs = []
    for item in json.loads(result.stdout or "[]"):
        issue = parse_autoloop_branch(item.get("headRefName", ""))
        if issue is None:
            continue
        prs.append(
            {
                "pr": item["number"],
                "issue": issue,
                "branch": item["headRefName"],
                "base": item.get("baseRefName") or "main",
                "title": item.get("title", ""),
                "created_at": item.get("createdAt", ""),
                "final_outcome": FINAL_OUTCOMES.get(item.get("state", "")),
            }
        )
    return prs


def is_auto_merge_recorded(records: list[dict], pr_number: int, issue_number: int) -> bool:
    """True if an auto-merge record matches the PR by `pr`, or by `issue` when it has no `pr`."""
    for record in records:
        if record.get("point") != "auto-merge":
            continue
        if record.get("pr") is not None:
            if record["pr"] == pr_number:
                return True
        elif record.get("issue") == issue_number:
            return True
    return False


def extract_review_verdict(comments: list[dict]) -> dict | None:
    """Return the incumbent verdict from the first autoloop review comment, else None."""
    for comment in comments:
        body = comment.get("body") or ""
        if any(marker in body for marker in REVIEW_FAILED_MARKERS):
            return {"approved": False, "feedback": body}
        if REVIEW_PASSED_MARKER in body:
            return {"approved": True, "feedback": body}
    return None


def format_issue_text(title: str, body: str | None) -> str:
    return f"Title: {title}\n\nBody: {body or ''}"


def _gh_pr_json(cfg, pr_number: int, fields: str) -> dict | None:
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", cfg.repo, "--json", fields],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def fetch_pr_comments(cfg, pr_number: int) -> list[dict] | None:
    data = _gh_pr_json(cfg, pr_number, "comments")
    return None if data is None else data.get("comments") or []


def fetch_issue_text(cfg, issue_number: int) -> str | None:
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number), "--repo", cfg.repo, "--json", "title,body"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    return format_issue_text(data.get("title", ""), data.get("body"))


def fetch_first_commit(cfg, pr_number: int) -> str | None:
    data = _gh_pr_json(cfg, pr_number, "commits")
    commits = (data or {}).get("commits") or []
    return commits[0].get("oid") if commits else None


def fetch_commit_diff(cfg, base: str, sha: str, max_chars: int) -> str | None:
    """Diff of `sha` against `base` (from their merge base), truncated to max_chars."""
    result = subprocess.run(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github.v3.diff",
            f"repos/{cfg.repo}/compare/{base}...{sha}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout[:max_chars]


def fetch_first_commit_diff(cfg, pr_number: int, base: str) -> str | None:
    """Diff of the PR's first commit against its base, truncated like the live review."""
    sha = fetch_first_commit(cfg, pr_number)
    if sha is None:
        return None
    return fetch_commit_diff(cfg, base, sha, cfg.diff_truncation)


def plan_auto_merge_backfill(
    cfg, prs: list[dict], records: list[dict]
) -> tuple[list[dict], dict[str, int]]:
    """Split discovered PRs into backfill candidates and skip counts by reason.

    Read-only: only calls `gh` to fetch comments, issue text, and the first-commit diff.
    """
    candidates: list[dict] = []
    skips: dict[str, int] = {}

    def skip(reason: str) -> None:
        skips[reason] = skips.get(reason, 0) + 1

    for pr in prs:
        if is_auto_merge_recorded(records, pr["pr"], pr["issue"]):
            skip("already_recorded")
            continue
        if pr["final_outcome"] is None:
            skip("open")
            continue
        comments = fetch_pr_comments(cfg, pr["pr"])
        if comments is None:
            skip("fetch_failed")
            continue
        verdict = extract_review_verdict(comments)
        if verdict is None:
            skip("no_incumbent")
            continue
        issue_text = fetch_issue_text(cfg, pr["issue"])
        diff = fetch_first_commit_diff(cfg, pr["pr"], pr["base"])
        if issue_text is None or diff is None:
            skip("fetch_failed")
            continue
        candidates.append({**pr, "incumbent_call": verdict, "issue_text": issue_text, "diff": diff})
    return candidates, skips
