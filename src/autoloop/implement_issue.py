"""Implement the top ready GitHub issue via Claude.

Config-driven pipeline: all repo-specific constants are read from autoloop.toml
via load_config().
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import time
from datetime import UTC, datetime

from autoloop.claude_runner import ClaudeResult, run_claude
from autoloop.config import REPO_DIR, load_config

cfg = None

LOCKFILE = REPO_DIR / ".autoloop.lock"
LOG_FILE = REPO_DIR / "autoloop" / "run_history.jsonl"

EMPTY_BRANCH_DIAGNOSTIC = """\
No changes were produced by the implementation agent.
This usually means the agent could not act, not that the code is wrong.
Likely causes:
 1. Missing .claude/settings.json permissions (run: autoloop init to scaffold)
 2. An active Claude Code session in this directory (close it or run elsewhere)
 3. The inner claude invocation failed to start (check claude CLI auth)"""


# --- Pure functions (testable without mocking) ---


def parse_dependency_numbers(body: str) -> list[str]:
    """Extract dependency issue numbers from issue body."""
    return re.findall(r"Depends on:?\s*#(\d+)", body, re.IGNORECASE)


def build_branch_name(issue: dict) -> str:
    """Slugify issue into a branch name."""
    slug = re.sub(r"[^a-z0-9]+", "-", issue["title"].lower()).strip("-")[:50]
    return f"autoloop/{issue['number']}-{slug}"


def parse_and_strip_metric_targets(body: str) -> tuple[str, list[str]]:
    """Strip **Metric Target:** lines from an issue body."""
    targets = []
    cleaned_lines = []
    for line in body.splitlines(keepends=True):
        if re.match(r"\s*\*\*Metric Target:\*\*", line):
            targets.append(line.rstrip("\n").rstrip("\r"))
        else:
            cleaned_lines.append(line)
    return "".join(cleaned_lines), targets


def detect_issue_type(body: str) -> str:
    """Determine conventional commit type from issue body."""
    body_lower = (body or "").lower()
    if "## type\nbug" in body_lower:
        return "fix"
    if "## type\nrefactor" in body_lower:
        return "refactor"
    return "feat"


def build_pr_body(
    issue: dict,
    attempts: int = 0,
    duration: float = 0,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> str:
    """Build the PR description markdown."""
    body = (
        f"Closes #{issue['number']}\n\n"
        f"## Summary\n"
        f"{issue['title']}\n\n"
        f"## Test Plan\n"
        f"- `{cfg.verify_cmd}` — all tests pass\n"
        f"- `uv run ruff check && uv run ruff format --check` — clean\n\n"
    )
    if attempts > 0:
        body += (
            f"## AutoLoop Run Stats\n"
            f"- Attempts: {attempts}/{cfg.max_retries}\n"
            f"- Duration: {duration:.0f}s\n"
            f"- Input tokens: {input_tokens:,}\n"
            f"- Output tokens: {output_tokens:,}\n"
            f"- Cost: ${cost_usd:.2f}\n\n"
        )
    body += "Automated implementation by AutoLoop."
    return body


def collect_verification_errors(
    ahead_count: str,
    test_rc: int,
    test_out: str,
    lint_rc: int,
    fmt_rc: int,
    changed_files: list[str],
) -> list[str]:
    """Build error list from verification subprocess results."""
    errors = []
    if ahead_count.strip() == "0" or not ahead_count.strip():
        errors.append("No commits on branch")
    if test_rc != 0:
        errors.append(f"Tests failed:\n{test_out[-500:]}")
    if lint_rc != 0 or fmt_rc != 0:
        errors.append("Lint or format check failed")
    test_files = [f for f in changed_files if f.startswith("tests/") and f.endswith(".py")]
    if not test_files:
        errors.append("No test files were added or modified")
    return errors


# --- Lockfile ---


def acquire_lock() -> bool:
    """Acquire lockfile. Returns False if another run is active."""
    if LOCKFILE.exists():
        try:
            pid = int(LOCKFILE.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ProcessLookupError, ValueError):
            pass
    LOCKFILE.write_text(str(os.getpid()))
    return True


def release_lock():
    """Remove the lockfile."""
    LOCKFILE.unlink(missing_ok=True)


def log_run(
    issue_number: int,
    success: bool,
    attempts: int,
    duration: float,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
):
    """Append a JSON entry to the run history log."""
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "issue": issue_number,
        "success": success,
        "attempts": attempts,
        "duration_seconds": round(duration),
        "cost_usd": round(cost_usd, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# --- Active session detection ---


def detect_active_claude_session(project_dir: str | None = None) -> bool | None:
    """Check if an interactive Claude Code session is active in the project directory.

    Returns True if a session is detected, False if none found, or None if
    detection is inconclusive (tools unavailable).
    """
    if project_dir is None:
        project_dir = str(REPO_DIR)

    project_dir = os.path.realpath(project_dir)

    try:
        result = subprocess.run(
            ["pgrep", "-af", "claude"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return False

    pids = []
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if len(parts) > 1 and "--dangerously-skip-permissions" not in parts[1]:
            pids.append(pid)

    if not pids:
        return False

    if platform.system() == "Darwin":
        return _check_cwd_lsof(pids, project_dir)
    return _check_cwd_proc(pids, project_dir)


def _check_cwd_lsof(pids: list[int], project_dir: str) -> bool | None:
    """Use lsof to check if any pid has cwd matching project_dir (macOS)."""
    try:
        result = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-Fn"]
            + [item for pid in pids for item in ("-p", str(pid))],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        if line.startswith("n"):
            cwd = os.path.realpath(line[1:])
            if cwd == project_dir:
                return True

    return False


def _check_cwd_proc(pids: list[int], project_dir: str) -> bool | None:
    """Use /proc to check if any pid has cwd matching project_dir (Linux)."""
    checked_any = False
    for pid in pids:
        try:
            cwd = os.path.realpath(f"/proc/{pid}/cwd")
            checked_any = True
            if cwd == project_dir:
                return True
        except (OSError, PermissionError):
            continue

    return False if checked_any else None


# --- Subprocess functions ---


def parent_issue_number(issue: dict) -> int | None:
    """Extract the parent issue number from a sub-issue body, if any."""
    body = issue.get("body", "") or ""
    match = re.search(r"Parent issue: #(\d+)", body)
    return int(match.group(1)) if match else None


def priority_rank(issue: dict) -> int:
    """Rank an issue by its priority label (p0 < p1 < p2 < unlabeled)."""
    priority_order = {"p0": 0, "p1": 1, "p2": 2}
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    for p, rank in priority_order.items():
        if p in labels:
            return rank
    return 99


def select_top_issue(issues: list[dict]) -> dict | None:
    """Pick the top issue, keeping sub-issues of one parent together."""
    if not issues:
        return None

    eligible = [i for i in issues if dependencies_met(i)]
    if not eligible:
        return None

    groups: dict[int, list[dict]] = {}
    standalone: list[dict] = []
    for issue in eligible:
        parent = parent_issue_number(issue)
        if parent is not None:
            groups.setdefault(parent, []).append(issue)
        else:
            standalone.append(issue)

    best_sub = None
    if groups:
        best_parent = max(groups, key=lambda p: (len(groups[p]), -p))
        best_sub = min(groups[best_parent], key=lambda i: i["number"])

    best_standalone = None
    if standalone:
        best_standalone = sorted(standalone, key=priority_rank)[0]

    if best_sub and best_standalone:
        if priority_rank(best_standalone) < priority_rank(best_sub):
            return best_standalone
        return best_sub

    return best_sub or best_standalone


def get_top_ready_issue() -> dict | None:
    """Pick the top ready issue, grouping sub-issues by parent."""
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
            "number,title,body,labels",
            "--limit",
            "10",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return select_top_issue(json.loads(result.stdout))


def get_issue_by_number(number: int) -> dict | None:
    """Fetch a specific issue by number, ignoring labels and story points."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            cfg.repo,
            "--json",
            "number,title,body,labels",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def dependencies_met(issue: dict) -> bool:
    """Check if all issues in Dependencies field are closed."""
    body = issue.get("body", "") or ""
    deps = parse_dependency_numbers(body)
    for dep_num in deps:
        result = subprocess.run(
            ["gh", "issue", "view", dep_num, "--repo", cfg.repo, "--json", "state"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            state = json.loads(result.stdout).get("state", "")
            if state != "CLOSED":
                return False
    return True


def create_branch(issue: dict) -> str:
    """Create feature branch from latest main."""
    branch = build_branch_name(issue)
    subprocess.run(["git", "checkout", "main"], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "pull", "origin", "main"], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "checkout", "-b", branch], cwd=REPO_DIR, check=True)
    return branch


def build_implementation_prompt(issue: dict) -> str:
    """Build the full prompt for the implementation agent."""
    claude_md = (REPO_DIR / "CLAUDE.md").read_text()

    comments = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue["number"]),
            "--repo",
            cfg.repo,
            "--json",
            "body,comments",
        ],
        capture_output=True,
        text=True,
    )
    full_context = issue["body"] or ""
    if comments.returncode == 0:
        data = json.loads(comments.stdout)
        for c in data.get("comments", []):
            body = c.get("body", "")
            if any(
                tag in body
                for tag in (
                    "Auto-triage",
                    "AutoLoop Attempt",
                    "Implementation Detail",
                    DESIGN_COMMENT_MARKER,
                )
            ):
                full_context += f"\n\n{body}"

    full_context, metric_targets = parse_and_strip_metric_targets(full_context)
    if metric_targets:
        logging.info(
            "Stripped %d metric target(s) from issue #%s: %s",
            len(metric_targets),
            issue["number"],
            metric_targets,
        )

    return (
        f"## Task\n\n"
        f"Implement GitHub issue #{issue['number']}: {issue['title']}\n\n"
        f"## Issue Details\n\n{full_context}\n\n"
        f"## Project Conventions\n\n{claude_md}\n\n"
        f"## Implementation Checklist\n\n"
        f"1. Read the files listed in 'Files to Modify'\n"
        f"2. Implement the changes described in the issue\n"
        f"3. Write comprehensive unit tests for every new/changed function\n"
        f"4. Run `{cfg.verify_cmd}` — all tests must pass\n"
        f"5. Run `uv run ruff check && uv run ruff format` — must be clean\n"
        f"6. If README.md needs updating (new tools, commands), update it\n"
        f"7. Stage and commit:\n"
        f"   `git add <specific files>`\n"
        f"   `git commit -m '<type>: <description> (#{issue['number']})'\n"
        f"   Types: fix (bugs), feat (features), refactor\n"
        f"   Keep first line under 70 chars\n\n"
        f"## Rules\n\n"
        f"- Never use real person or company names in test data\n"
        f"- Follow existing code patterns in this repo\n"
        f"- Do not add features beyond what the issue asks for\n"
        f"- Do not skip tests or lint\n"
        f"- Do not run git push\n"
    )


DESIGN_PROMPT = (
    "## Task\n\n"
    "Propose an implementation design for GitHub issue #{number}: {title}\n\n"
    "## Issue Details\n\n{body}\n\n"
    "## Project Conventions\n\n{conventions}\n\n"
    "## Instructions\n\n"
    "Write a concise implementation design proposal. Describe the approach, the\n"
    "functions or files to add or change, and the key edge cases to handle.\n"
    "Do not write the code — only the design. Do not modify any files.\n"
)


DESIGN_COMMENT_MARKER = "Implementation Design:"


def design_issue(issue: dict) -> str:
    """Generate an implementation design proposal for the issue via Claude."""
    claude_md = (REPO_DIR / "CLAUDE.md").read_text()
    prompt = DESIGN_PROMPT.format(
        number=issue["number"],
        title=issue["title"],
        body=issue.get("body", "") or "",
        conventions=claude_md,
    )
    return run_claude(prompt, cfg.impl_model, cfg.impl_timeout).text


def design_required(issue: dict, require_design: bool = False) -> bool:
    """Whether the issue must pass a design review before implementation."""
    if require_design:
        return True
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    return "design-required" in labels


def has_needs_design_label(issue: dict) -> bool:
    """Whether the issue still carries the 'needs-design' label."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    return "needs-design" in labels


def has_design_comment(number: int) -> bool:
    """Check the issue's comments for an existing Implementation Design."""
    result = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", cfg.repo, "--json", "comments"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    data = json.loads(result.stdout)
    return any(DESIGN_COMMENT_MARKER in c.get("body", "") for c in data.get("comments", []))


def post_design(number: int, design: str):
    """Post the implementation design as a comment on the issue."""
    comment = f"**{DESIGN_COMMENT_MARKER}**\n\n{design}"
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", cfg.repo, "--body", comment],
    )


def add_needs_design_label(number: int):
    """Add the 'needs-design' label to flag the issue for human review."""
    subprocess.run(
        ["gh", "issue", "edit", str(number), "--repo", cfg.repo, "--add-label", "needs-design"],
    )


def design_gate(issue: dict, require_design: bool = False) -> bool:
    """Enforce the optional design review before implementation.

    Returns True if implementation should proceed, False if it should be
    skipped this run.
    """
    if not design_required(issue, require_design):
        return True

    number = issue["number"]
    if has_design_comment(number):
        if has_needs_design_label(issue):
            print(f"#{number}: design awaiting human approval, skipping.")
            return False
        return True

    print(f"#{number}: no design found, generating implementation design.")
    design = design_issue(issue)
    if design:
        post_design(number, design)
    add_needs_design_label(number)
    print(f"#{number}: design generated and needs-design added, skipping implementation.")
    return False


def post_attempt_failure(number: int, attempt: int, errors: str):
    """Post verification failure as a comment on the issue."""
    comment = f"**AutoLoop Attempt {attempt} failed:**\n\n```\n{errors[-2000:]}\n```"
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", cfg.repo, "--body", comment],
    )


def implement(issue: dict, previous_errors: str | None = None) -> ClaudeResult:
    """Run Claude to implement the issue. Optionally includes prior failure context."""
    prompt = build_implementation_prompt(issue)
    if previous_errors:
        prompt += (
            f"\n\n## Previous Attempt Failed\n\n"
            f"The last implementation attempt failed verification with these errors:\n"
            f"```\n{previous_errors[-2000:]}\n```\n\n"
            f"Fix these specific issues. Do not start from scratch"
            f" — build on what's already there.\n"
        )
    return run_claude(prompt, cfg.impl_model, cfg.impl_timeout)


def is_branch_empty(branch: str) -> bool:
    """Return True if the branch has zero commits ahead of main."""
    result = subprocess.run(
        ["git", "rev-list", "--count", f"main..{branch}"],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    )
    count = result.stdout.strip() if result.returncode == 0 else ""
    return count == "0" or count == ""


def verify_implementation(branch: str) -> tuple[bool, str]:
    """Verify the agent actually produced valid work."""
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"main..{branch}"],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    )
    tests = subprocess.run(
        cfg.verify_cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
        timeout=cfg.test_timeout,
    )
    lint = subprocess.run(
        ["uv", "run", "ruff", "check"],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    )
    fmt = subprocess.run(
        ["uv", "run", "ruff", "format", "--check", "."],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    )
    diff = subprocess.run(
        ["git", "diff", "--name-only", "main"],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    )
    changed = [f for f in diff.stdout.strip().split("\n") if f]

    errors = collect_verification_errors(
        ahead_count=ahead.stdout if ahead.returncode == 0 else "",
        test_rc=tests.returncode,
        test_out=tests.stdout,
        lint_rc=lint.returncode,
        fmt_rc=fmt.returncode,
        changed_files=changed,
    )
    if errors:
        return False, "\n".join(errors)
    return True, ""


REVIEW_PROMPT = """\
Review this implementation against the original issue.

Issue #{number}: {title}
{issue_body}

Diff:
{diff}

Evaluate:
1. Does the implementation satisfy each acceptance criterion?
2. Are the tests meaningful (not just pass-through stubs)?
3. Does the code follow existing patterns in the codebase?

Respond with JSON only:
{{
  "approved": true | false,
  "issues": ["issue 1", "issue 2"],
  "summary": "one line"
}}
"""


def parse_review_response(text: str) -> tuple[bool, str]:
    """Parse the review verdict JSON into an (approved, feedback) pair."""
    stripped = text.strip()
    if "```" in stripped:
        stripped = stripped.split("```")[1].replace("json", "").strip()
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, IndexError):
        return False, f"Review response was not valid JSON:\n{text[:500]}"
    if not isinstance(data, dict) or "approved" not in data:
        return False, f"Review response was malformed:\n{text[:500]}"
    if data.get("approved"):
        return True, data.get("summary", "") or ""
    issues = data.get("issues") or []
    if isinstance(issues, list) and issues:
        feedback = "Review found issues:\n" + "\n".join(f"- {i}" for i in issues)
    else:
        feedback = data.get("summary") or "Review rejected the implementation."
    return False, feedback


def review_implementation(issue: dict, branch: str) -> tuple[bool, str]:
    """Review the implementation for semantic quality via Claude."""
    diff = subprocess.run(
        ["git", "diff", f"main..{branch}"],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    ).stdout

    prompt = REVIEW_PROMPT.format(
        number=issue["number"],
        title=issue["title"],
        issue_body=issue.get("body", "") or "",
        diff=diff[:8000],
    )
    result = run_claude(prompt, cfg.impl_model, cfg.impl_timeout)
    if not result.success:
        return False, "Review call failed (timeout or non-zero exit)."
    return parse_review_response(result.text)


def ensure_clean_main():
    """Reset to a clean main branch, discarding any leftover state."""
    subprocess.run(["git", "checkout", "--", "."], cwd=REPO_DIR)
    subprocess.run(["git", "checkout", "main"], cwd=REPO_DIR)
    subprocess.run(["git", "pull", "--ff-only", "origin", "main"], cwd=REPO_DIR)


def cleanup_branch(branch: str):
    """Delete failed branch locally and remotely."""
    subprocess.run(["git", "checkout", "main"], cwd=REPO_DIR)
    subprocess.run(["git", "branch", "-D", branch], cwd=REPO_DIR)
    subprocess.run(
        ["git", "push", "origin", "--delete", branch],
        cwd=REPO_DIR,
        capture_output=True,
    )


def create_pr(
    issue: dict,
    branch: str,
    attempts: int = 0,
    duration: float = 0,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
):
    """Create PR with conventional format."""
    issue_type = detect_issue_type(issue.get("body", ""))
    title = f"{issue_type}: {issue['title'][:60]} (#{issue['number']})"
    body = build_pr_body(
        issue,
        attempts=attempts,
        duration=duration,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            cfg.repo,
            "--title",
            title,
            "--body",
            body,
            "--head",
            branch,
            "--base",
            "main",
            "--assignee",
            cfg.pr_reviewer,
        ],
        cwd=REPO_DIR,
    )


def unblock_ready_issues():
    """Re-check blocked issues and restore ready label if deps are met."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            cfg.repo,
            "--label",
            "blocked",
            "--state",
            "open",
            "--json",
            "number,title,body,labels",
            "--limit",
            "50",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    for issue in json.loads(result.stdout):
        if dependencies_met(issue):
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue["number"]),
                    "--repo",
                    cfg.repo,
                    "--remove-label",
                    "blocked",
                    "--add-label",
                    "ready",
                ],
            )
            print(f"  Unblocked #{issue['number']}: {issue['title']}")


def cleanup_merged_labels():
    """Remove in-review label from closed issues whose PR already merged."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            cfg.repo,
            "--label",
            "in-review",
            "--state",
            "closed",
            "--json",
            "number",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    for issue in json.loads(result.stdout):
        subprocess.run(
            [
                "gh",
                "issue",
                "edit",
                str(issue["number"]),
                "--repo",
                cfg.repo,
                "--remove-label",
                "in-review",
            ],
        )


def post_in_progress_comment(number: int):
    """Comment on the issue noting the bot has started implementing it."""
    comment = (
        "**AutoLoop:** The implementation bot has started working on "
        "this issue. It will open a PR when implementation is complete."
    )
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", cfg.repo, "--body", comment],
    )


def label_in_review(number: int):
    """Move issue from in-progress to in-review."""
    subprocess.run(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            cfg.repo,
            "--remove-label",
            "in-progress",
            "--add-label",
            "in-review",
        ],
    )


# --- Orchestration ---


def implement_single_issue(issue: dict, require_design: bool = False) -> bool:
    """Implement one issue end-to-end. Returns True if PR created successfully."""
    try:
        from autoloop.config import touches_protected_path
        from autoloop.create_issue import extract_files_from_spec

        body = issue.get("body") or ""
        mentioned_files = extract_files_from_spec(body)
        if touches_protected_path(mentioned_files, cfg.protected_paths):
            print(f"  #{issue['number']}: touches protected path, skipping.")
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue["number"]),
                    "--repo",
                    cfg.repo,
                    "--add-label",
                    "needs-human",
                ],
            )
            return False

        ensure_clean_main()

        if not design_gate(issue, require_design):
            return False

        start_time = time.time()
        claude_results: list[ClaudeResult] = []
        final_attempt = 0
        success = False

        print(f"Implementing #{issue['number']}: {issue['title']}")

        subprocess.run(
            [
                "gh",
                "issue",
                "edit",
                str(issue["number"]),
                "--repo",
                cfg.repo,
                "--remove-label",
                "ready",
                "--add-label",
                "in-progress",
            ],
        )
        post_in_progress_comment(issue["number"])

        branch = create_branch(issue)
        print(f"  Branch: {branch}")

        last_errors = None
        empty_branch_failure = False
        for attempt in range(1, cfg.max_retries + 1):
            print(f"  Attempt {attempt}/{cfg.max_retries}...")
            claude_results.append(implement(issue, previous_errors=last_errors))
            final_attempt = attempt

            if is_branch_empty(branch):
                print(f"  {EMPTY_BRANCH_DIAGNOSTIC}")
                post_attempt_failure(issue["number"], attempt, EMPTY_BRANCH_DIAGNOSTIC)
                empty_branch_failure = True
                break

            valid, errors = verify_implementation(branch)
            if not valid:
                print(f"  Verification failed:\n{errors}")
                last_errors = errors
                post_attempt_failure(issue["number"], attempt, errors)
                continue

            print("  Verification passed. Reviewing implementation...")
            approved, feedback = review_implementation(issue, branch)
            if not approved:
                print(f"  Review failed:\n{feedback}")
                last_errors = feedback
                post_attempt_failure(issue["number"], attempt, feedback)
                continue

            success = True
            print("  Review passed.")
            break

        elapsed = time.time() - start_time
        total_cost = sum(r.cost_usd for r in claude_results)
        total_input = sum(r.input_tokens for r in claude_results)
        total_output = sum(r.output_tokens for r in claude_results)
        total_cache_read = sum(r.cache_read_tokens for r in claude_results)

        if not success:
            if empty_branch_failure:
                print("  Implementation produced no changes. Labeling needs-human.")
            else:
                print("  All retries exhausted. Labeling needs-human.")
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue["number"]),
                    "--repo",
                    cfg.repo,
                    "--remove-label",
                    "in-progress",
                    "--add-label",
                    "ready",
                    "--add-label",
                    "needs-human",
                ],
            )
            cleanup_branch(branch)
            log_run(
                issue["number"],
                False,
                final_attempt,
                elapsed,
                total_cost,
                total_input,
                total_output,
                total_cache_read,
            )
            return False

        subprocess.run(["git", "push", "-u", "origin", branch], cwd=REPO_DIR)
        create_pr(
            issue,
            branch,
            attempts=final_attempt,
            duration=elapsed,
            cost_usd=total_cost,
            input_tokens=total_input,
            output_tokens=total_output,
        )
        label_in_review(issue["number"])
        print(f"  PR created for #{issue['number']}.")

        subprocess.run(["git", "checkout", "main"], cwd=REPO_DIR)

        print(f"\n--- AutoLoop Run Stats (#{issue['number']}) ---")
        print(f"  Duration: {elapsed:.0f}s")
        print(f"  Claude calls: {len(claude_results)}")
        print(f"  Input tokens: {total_input:,}")
        print(f"  Output tokens: {total_output:,}")
        print(f"  Cost: ${total_cost:.2f}")
        log_run(
            issue["number"],
            True,
            final_attempt,
            elapsed,
            total_cost,
            total_input,
            total_output,
            total_cache_read,
        )

        return True
    except Exception:
        logging.exception("implement_single_issue failed for #%s", issue.get("number"))
        return False


def implement_targeted_issue(number: int, require_design: bool = False) -> bool:
    """Implement a specific issue by number, bypassing label and point checks."""
    issue = get_issue_by_number(number)
    if not issue:
        print(f"#{number}: could not fetch issue, aborting.")
        return False

    if not dependencies_met(issue):
        print(f"#{number}: dependencies not met, aborting.")
        return False

    success = implement_single_issue(issue, require_design=require_design)
    print(f"\nImplemented {1 if success else 0} issue(s) this run.")
    return success


def main(issue=None, max_issues=1, require_design=False):
    global cfg
    if cfg is None:
        cfg = load_config()

    session_detected = detect_active_claude_session()
    if session_detected is True:
        print(
            "Active Claude Code session detected in this directory.\n"
            "Close it, or move the Claude Code session to a parent folder."
        )
        return

    if not acquire_lock():
        print("Another implementation is running. Exiting.")
        return

    try:
        cleanup_merged_labels()
        unblock_ready_issues()

        if issue is not None:
            implement_targeted_issue(issue, require_design=require_design)
            return

        implemented = 0
        while implemented < max_issues:
            top_issue = get_top_ready_issue()
            if not top_issue:
                print("No more ready issues.")
                break

            success = implement_single_issue(top_issue, require_design=require_design)
            if success:
                implemented += 1
            else:
                break

        print(f"\nImplemented {implemented} issue(s) this run.")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
