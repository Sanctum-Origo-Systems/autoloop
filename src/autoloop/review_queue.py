"""Review-queue module: find issue, fetch CI/gate data, format and post summary comment."""

from __future__ import annotations

import json
import re
import subprocess

from autoloop.config import AutoLoopConfig


def find_review_queue_issue(cfg: AutoLoopConfig) -> int:
    """Return the issue number of the first open issue labeled ``review-queue``.

    Raises ``RuntimeError`` when none exists.
    """
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            cfg.repo,
            "--label",
            "review-queue",
            "--state",
            "open",
            "--json",
            "number",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to list review-queue issues: {result.stderr.strip()}")
    issues = json.loads(result.stdout)
    if not issues:
        raise RuntimeError("No open issue with label 'review-queue' found")
    return issues[0]["number"]


def fetch_ci_status(pr_number: int, cfg: AutoLoopConfig) -> str:
    """Return ``'pass'``, ``'fail'``, or ``'pending'`` by parsing ``gh pr checks``."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "checks",
            str(pr_number),
            "--repo",
            cfg.repo,
            "--json",
            "bucket",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "pending"
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "pending"
    if not checks:
        return "pass"
    buckets = {c.get("bucket", "") for c in checks}
    if "fail" in buckets:
        return "fail"
    if "pending" in buckets:
        return "pending"
    return "pass"


def fetch_gate_flags(issue_number: int, cfg: AutoLoopConfig) -> dict:
    """Return a dict with keys ``owner_approved`` and ``needs_human`` from issue labels."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            cfg.repo,
            "--json",
            "labels",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"owner_approved": False, "needs_human": False}
    data = json.loads(result.stdout)
    label_names = {label["name"] for label in data.get("labels", [])}
    return {
        "owner_approved": "owner-approved" in label_names,
        "needs_human": "needs-human" in label_names,
    }


def _fetch_issue_body(issue_number: int, cfg: AutoLoopConfig) -> str:
    """Fetch the body text of an issue."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            cfg.repo,
            "--json",
            "body",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    data = json.loads(result.stdout)
    return data.get("body", "") or ""


def _parse_depends_on(body: str) -> list[int]:
    """Extract dependency issue numbers from a ``Depends on: #N`` pattern."""
    return [int(n) for n in re.findall(r"Depends on:?\s*#(\d+)", body, re.IGNORECASE)]


def order_by_dependencies(prs: list[dict], cfg: AutoLoopConfig) -> list[dict]:
    """Sort PRs so that a PR whose source issue depends-on #N appears after the PR for #N."""
    if not prs:
        return []

    issue_to_pr = {pr["issue_number"]: pr for pr in prs}

    # Build dependency map: issue_number -> [issue numbers it depends on within this set]
    deps: dict[int, list[int]] = {}
    for pr in prs:
        body = _fetch_issue_body(pr["issue_number"], cfg)
        dep_issues = _parse_depends_on(body)
        deps[pr["issue_number"]] = [d for d in dep_issues if d in issue_to_pr]

    # Topological sort (Kahn's algorithm)
    in_degree: dict[int, int] = {pr["issue_number"]: len(deps[pr["issue_number"]]) for pr in prs}

    queue = sorted(n for n, deg in in_degree.items() if deg == 0)
    ordered: list[dict] = []

    while queue:
        current = queue.pop(0)
        ordered.append(issue_to_pr[current])
        for issue_num, dep_list in deps.items():
            if current in dep_list:
                in_degree[issue_num] -= 1
                if in_degree[issue_num] == 0:
                    queue.append(issue_num)
                    queue.sort()

    # Append any remaining PRs (cycles) in original order
    seen = {pr["issue_number"] for pr in ordered}
    for pr in prs:
        if pr["issue_number"] not in seen:
            ordered.append(pr)

    return ordered


def format_summary(ordered_prs: list[dict]) -> str:
    """Return a markdown table with columns Order/PR/Issue/Title/CI/Gate."""
    lines = [
        "| Order | PR | Issue | Title | CI | Gate |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for i, pr in enumerate(ordered_prs, 1):
        gate = pr.get("gate", {})
        gate_parts = []
        if gate.get("owner_approved"):
            gate_parts.append("owner-approved")
        if gate.get("needs_human"):
            gate_parts.append("needs-human")
        gate_str = ", ".join(gate_parts) if gate_parts else "-"
        ci = pr.get("ci", "pending")
        lines.append(
            f"| {i} | #{pr['pr_number']} | #{pr['issue_number']}"
            f" | {pr['title']} | {ci} | {gate_str} |"
        )
    return "\n".join(lines)


def post_batch_summary(prs: list[dict], cfg: AutoLoopConfig) -> None:
    """Post a summary comment on the review-queue issue.

    Returns ``None`` without posting when *prs* is empty.
    """
    if not prs:
        return None

    rq_number = find_review_queue_issue(cfg)

    enriched = []
    for pr in prs:
        enriched_pr = dict(pr)
        enriched_pr["ci"] = fetch_ci_status(pr["pr_number"], cfg)
        enriched_pr["gate"] = fetch_gate_flags(pr["issue_number"], cfg)
        enriched.append(enriched_pr)

    ordered = order_by_dependencies(enriched, cfg)
    summary = format_summary(ordered)
    body = (
        "## Review Queue Summary\n\n"
        f"{summary}\n\n"
        "PRs are ordered by dependency chain — earlier entries must merge first."
    )

    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(rq_number),
            "--repo",
            cfg.repo,
            "--body",
            body,
        ],
        capture_output=True,
        text=True,
    )

    return None
