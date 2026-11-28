"""Review-queue module: find issue, fetch CI/gate data, rebuild the review-queue body."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time

from autoloop.config import REVIEW_QUEUE_LABEL, AutoLoopConfig


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
            REVIEW_QUEUE_LABEL,
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
        raise RuntimeError(f"No open issue with label '{REVIEW_QUEUE_LABEL}' found")
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


# "Depends on:" followed by a run of #N refs separated by commas, spaces, or "and".
# Separators use [ \t] so a list never spills onto the next line.
_DEPENDS_ON_RE = re.compile(
    r"Depends on:?\s*(#\d+(?:(?:[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+|[ \t]+)#\d+)*)",
    re.IGNORECASE,
)


def parse_dependency_numbers(body: str) -> list[int]:
    """Extract every issue number from all ``Depends on: #A, #B and #C`` lists in *body*.

    Numbers are returned in order of appearance with duplicates removed.
    """
    numbers: list[int] = []
    for match in _DEPENDS_ON_RE.finditer(body):
        for n in re.findall(r"#(\d+)", match.group(1)):
            if int(n) not in numbers:
                numbers.append(int(n))
    return numbers


# --- Review-queue issue body: Open table built from live PR state ---

_MERGEABLE_RETRIES = 3
_MERGEABLE_RETRY_DELAY = 2.0
_CLOSED_FETCH_LIMIT = 50
DONE_LIMIT = 10

STATE_ORDER = ("ready", "needs update", "blocked", "draft")
_PRIORITY_LABELS = ("p0", "p1", "p2")

_CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s*#(\d+)", re.IGNORECASE)

# Zero-width space inserted after "@" so titles never produce a mention.
_ZWSP = "​"


def _sleep(seconds: float) -> None:
    """Sleep between mergeability re-queries (patched in tests)."""
    time.sleep(seconds)


def parse_closes_number(body: str) -> int | None:
    """Return the issue number from the first ``Closes #N`` in a PR *body*, or ``None``."""
    match = _CLOSES_RE.search(body or "")
    return int(match.group(1)) if match else None


def escape_cell(text: str) -> str:
    """Make *text* safe for a markdown table cell.

    ``|`` becomes ``\\|``, newlines become spaces, and ``@`` is followed by a zero-width
    space so the body never contains a raw ``@user`` mention.
    """
    text = re.sub(r"\r\n|\r|\n", " ", text or "")
    text = text.replace("|", "\\|")
    return text.replace("@", "@" + _ZWSP)


def _label_names(labels: list[dict] | None) -> set[str]:
    return {label.get("name", "") for label in labels or []}


def priority_rank(pr_labels: set[str], issue_labels: set[str]) -> int:
    """Return 0/1/2 for p0/p1/p2 (PR labels first, then linked issue), 3 when unlabelled."""
    for labels in (pr_labels, issue_labels):
        for rank, name in enumerate(_PRIORITY_LABELS):
            if name in labels:
                return rank
    return len(_PRIORITY_LABELS)


def _list_open_prs(cfg: AutoLoopConfig) -> list[dict]:
    """Return every open PR in the repo, regardless of author."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            cfg.repo,
            "--state",
            "open",
            "--limit",
            "500",
            "--json",
            "number,title,body,isDraft,mergeable,labels,files",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to list open PRs: {result.stderr.strip()}")
    return json.loads(result.stdout or "[]")


def _list_closed_prs(cfg: AutoLoopConfig) -> list[dict]:
    """Return recently closed (merged or unmerged) PRs, enough to pick the latest few.

    Uses the REST pulls endpoint sorted by last update, so a PR opened long ago but closed
    recently is still included. ``gh pr list`` orders by creation date and would miss it.
    REST fields are mapped to the ``closedAt``/``mergedAt`` keys ``format_done_table`` reads.
    """
    endpoint = (
        f"repos/{cfg.repo}/pulls?state=closed&sort=updated&direction=desc"
        f"&per_page={_CLOSED_FETCH_LIMIT}"
    )
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to list closed PRs: {result.stderr.strip()}")
    return [
        {
            "number": pr["number"],
            "title": pr.get("title", ""),
            "body": pr.get("body") or "",
            "closedAt": pr.get("closed_at"),
            "mergedAt": pr.get("merged_at"),
        }
        for pr in json.loads(result.stdout or "[]")
    ]


def _fetch_mergeable(pr_number: int, cfg: AutoLoopConfig) -> str:
    """Return the PR's ``mergeable`` value (``MERGEABLE``/``CONFLICTING``/``UNKNOWN``)."""
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number), "--repo", cfg.repo, "--json", "mergeable"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "UNKNOWN"
    try:
        return json.loads(result.stdout).get("mergeable") or "UNKNOWN"
    except json.JSONDecodeError:
        return "UNKNOWN"


def resolve_mergeable(pr_number: int, initial: str | None, cfg: AutoLoopConfig) -> str:
    """Re-query an ``UNKNOWN`` mergeable value a few times with a short delay."""
    mergeable = initial or "UNKNOWN"
    for _ in range(_MERGEABLE_RETRIES):
        if mergeable != "UNKNOWN":
            break
        _sleep(_MERGEABLE_RETRY_DELAY)
        mergeable = _fetch_mergeable(pr_number, cfg)
    return mergeable


def _fetch_issue_details(issue_number: int, cfg: AutoLoopConfig) -> dict:
    """Return ``{"body": str, "labels": set[str]}`` for an issue (empty on failure)."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--repo",
            cfg.repo,
            "--json",
            "body,labels",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"body": "", "labels": set()}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"body": "", "labels": set()}
    return {"body": data.get("body", "") or "", "labels": _label_names(data.get("labels"))}


def _is_open(number: int, cfg: AutoLoopConfig) -> bool:
    """Return True when issue or PR *number* is open. Fails closed (True) on gh errors."""
    result = subprocess.run(
        ["gh", "api", f"repos/{cfg.repo}/issues/{number}", "--jq", ".state"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return True
    return result.stdout.strip().lower() != "closed"


def find_overlaps(prs: list[dict]) -> dict[int, list[int]]:
    """Map each PR number to the other PRs that change at least one of the same files.

    PRs that share no file with another PR are left out.
    """
    files = {pr["number"]: {f.get("path", "") for f in pr.get("files") or []} for pr in prs}
    overlaps: dict[int, list[int]] = {}
    for number, paths in files.items():
        others = sorted(n for n, p in files.items() if n != number and paths & p)
        if others:
            overlaps[number] = others
    return overlaps


def build_open_rows(prs: list[dict], cfg: AutoLoopConfig) -> list[dict]:
    """Classify each open PR and return rows ordered by state, priority, then PR number.

    State precedence is draft > blocked > needs update > ready. The first ``ready`` row
    whose CI is not pending gets ``next=True``. A PR sharing a changed file with another
    open PR gets an ``overlaps #N`` note, which does not affect its state.
    """
    open_pr_numbers = {pr["number"] for pr in prs}
    overlaps = find_overlaps(prs)
    open_cache: dict[int, bool] = {}

    def dep_is_open(number: int) -> bool:
        if number in open_pr_numbers:
            return True
        if number not in open_cache:
            open_cache[number] = _is_open(number, cfg)
        return open_cache[number]

    rows = []
    for pr in prs:
        number = pr["number"]
        pr_body = pr.get("body", "") or ""
        issue_number = parse_closes_number(pr_body)
        issue = (
            _fetch_issue_details(issue_number, cfg)
            if issue_number is not None
            else {"body": "", "labels": set()}
        )

        deps = []
        for dep in parse_dependency_numbers(pr_body) + parse_dependency_numbers(issue["body"]):
            if dep not in deps and dep not in (number, issue_number):
                deps.append(dep)
        blocking = [d for d in deps if dep_is_open(d)]

        ci = fetch_ci_status(number, cfg)
        notes = []
        if pr.get("isDraft"):
            state = "draft"
        elif blocking:
            state = "blocked"
            notes.append("blocked by " + ", ".join(f"#{d}" for d in blocking))
        else:
            mergeable = resolve_mergeable(number, pr.get("mergeable"), cfg)
            if mergeable == "CONFLICTING":
                notes.append("merge conflict")
            if ci == "fail":
                notes.append("CI failing")
            if notes:
                state = "needs update"
            else:
                state = "ready"
                if mergeable == "UNKNOWN":
                    notes.append("merge status pending")
        if number in overlaps:
            notes.append("overlaps " + ", ".join(f"#{n}" for n in overlaps[number]))

        rows.append(
            {
                "pr_number": number,
                "issue_number": issue_number,
                "title": pr.get("title", ""),
                "state": state,
                "ci": ci,
                "priority": priority_rank(_label_names(pr.get("labels")), issue["labels"]),
                "notes": notes,
                "next": False,
            }
        )

    rows.sort(key=lambda r: (STATE_ORDER.index(r["state"]), r["priority"], r["pr_number"]))
    for row in rows:
        if row["state"] == "ready" and row["ci"] != "pending":
            row["next"] = True
            break
    return rows


def format_open_table(rows: list[dict]) -> str:
    """Return the Open markdown table with columns Next/PR/Issue/Title/State/CI/Notes."""
    if not rows:
        return "_No open PRs._"
    lines = [
        "| Next | PR | Issue | Title | State | CI | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        issue = f"#{row['issue_number']}" if row["issue_number"] is not None else "-"
        lines.append(
            f"| {'next' if row['next'] else ''} | #{row['pr_number']} | {issue}"
            f" | {escape_cell(row['title'])} | {row['state']} | {row['ci']}"
            f" | {'; '.join(row['notes'])} |"
        )
    return "\n".join(lines)


def format_done_table(closed_prs: list[dict]) -> str:
    """Return the Done markdown table of the most recently closed PRs, newest first.

    Built only from *closed_prs*, never from a previous body, and capped at ``DONE_LIMIT``.
    """
    latest = sorted(closed_prs, key=lambda pr: pr.get("closedAt") or "", reverse=True)
    latest = latest[:DONE_LIMIT]
    if not latest:
        return "_None yet._"
    lines = [
        "| PR | Issue | Title | Result |",
        "| --- | --- | --- | --- |",
    ]
    for pr in latest:
        issue_number = parse_closes_number(pr.get("body", "") or "")
        issue = f"#{issue_number}" if issue_number is not None else "-"
        merged_at = pr.get("mergedAt")
        result = f"merged {merged_at[:10]}" if merged_at else "closed, not merged"
        lines.append(
            f"| #{pr['number']} | {issue} | {escape_cell(pr.get('title', ''))} | {result} |"
        )
    return "\n".join(lines)


def _section_markers(name: str) -> tuple[str, str]:
    return f"<!-- review-queue:{name}:start -->", f"<!-- review-queue:{name}:end -->"


def read_section(body: str, name: str) -> str | None:
    """Return the text between the ``name`` section markers in *body*, or ``None``."""
    start, end = _section_markers(name)
    pattern = re.escape(start) + r"\n?(.*?)\n?" + re.escape(end)
    match = re.search(pattern, body or "", re.DOTALL)
    return match.group(1) if match else None


def build_queue_body(open_table: str, done: str) -> str:
    """Return the review-queue issue body with marker-wrapped Open and Done sections."""
    open_start, open_end = _section_markers("open")
    done_start, done_end = _section_markers("done")
    return (
        "## Review Queue\n\n"
        "### Open\n\n"
        f"{open_start}\n{open_table}\n{open_end}\n\n"
        "### Done\n\n"
        f"{done_start}\n{done}\n{done_end}\n"
    )


def refresh_review_queue(cfg: AutoLoopConfig) -> None:
    """Rebuild the review-queue issue body from live PR state.

    Does nothing when no review-queue issue exists. The Done section is recomputed from
    the most recently closed PRs on every refresh, even when no PRs are open; the
    current issue body is never read.
    """
    try:
        rq_number = find_review_queue_issue(cfg)
    except RuntimeError:
        return None

    rows = build_open_rows(_list_open_prs(cfg), cfg)
    done = format_done_table(_list_closed_prs(cfg))
    body = build_queue_body(format_open_table(rows), done)

    fd, path = tempfile.mkstemp(prefix="review-queue-", suffix=".md")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        result = subprocess.run(
            ["gh", "issue", "edit", str(rq_number), "--repo", cfg.repo, "--body-file", path],
            capture_output=True,
            text=True,
        )
    finally:
        os.unlink(path)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to edit review-queue issue: {result.stderr.strip()}")
    return None
