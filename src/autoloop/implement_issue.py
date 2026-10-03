"""Implement the top ready GitHub issue via Claude.

Config-driven pipeline: all repo-specific constants are read from autoloop.toml
via load_config().
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import platform
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from autoloop.claude_runner import ClaudeResult, run_claude
from autoloop.config import AutoLoopConfig, RepoContext, load_config

cfg = None


class SystemicError(Exception):
    """Environment-wide failure that should abort the entire run.

    Raised by implement_single_issue when the failure is not issue-specific
    (e.g. auth/CLI errors, disk/network errors) so the caller can distinguish
    from non-systemic failures (returned as False) and skip to the next issue.
    """


EMPTY_BRANCH_DIAGNOSTIC = """\
No changes were produced by the implementation agent.
This usually means the agent could not act, not that the code is wrong.
Likely causes:
 1. Missing .claude/settings.json permissions (run: autoloop init to scaffold)
 2. An active Claude Code session in this directory (close it or run elsewhere)
 3. The inner claude invocation failed to start (check claude CLI auth)"""


# --- Worktree lifecycle helpers ---


def ensure_worktree(ctx: RepoContext, branch: str) -> Path:
    """Create a persistent worktree for *branch* under ctx.worktree_dir if it doesn't exist."""
    wt_path = ctx.worktree_dir / branch
    if wt_path.exists():
        return wt_path
    ctx.worktree_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(wt_path), "-B", branch, "origin/main"],
        cwd=ctx.repo_dir,
        check=True,
    )
    return wt_path


def reset_worktree_branch(worktree_path: Path, branch: str) -> None:
    """Reset the worktree branch to the latest origin/main."""
    subprocess.run(
        ["git", "checkout", "-B", branch, "origin/main"],
        cwd=worktree_path,
        check=True,
    )


def resolve_working_dir(isolation: str, ctx: RepoContext, branch: str) -> Path:
    """Return the working directory based on isolation mode.

    When isolation is 'worktree', creates/reuses a worktree and resets the branch.
    When isolation is 'off', returns Path.cwd().
    """
    if isolation == "worktree":
        wt_path = ensure_worktree(ctx, branch)
        reset_worktree_branch(wt_path, branch)
        return wt_path
    return Path.cwd()


# --- Pure functions (testable without mocking) ---


def parse_dependency_numbers(body: str) -> list[str]:
    """Extract dependency issue numbers from issue body."""
    return re.findall(r"Depends on:?\s*#(\d+)", body, re.IGNORECASE)


def build_branch_name(issue: dict) -> str:
    """Slugify issue into a branch name."""
    slug = re.sub(r"[^a-z0-9]+", "-", issue["title"].lower()).strip("-")[:50]
    return f"autoloop/{issue['number']}-{slug}"


def truncate_spec(body: str, max_chars: int, issue_url: str = "") -> str:
    """Truncate an issue spec to *max_chars*, preserving the beginning."""
    if max_chars <= 0 or len(body) <= max_chars:
        return body
    truncated = body[:max_chars]
    note = "\n\n[Issue body truncated."
    if issue_url:
        note += f" Full issue: {issue_url}"
    note += "]"
    return truncated + note


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


def extract_linked_issue_number(title: str, body: str) -> int | None:
    """Extract linked issue number from PR title or body.

    Looks for 'Closes #N', 'Fixes #N', 'Resolves #N' in body,
    or '(#N)' in title.
    """
    for pattern in (
        r"(?:closes|fixes|resolves)\s+#(\d+)",
        r"\(#(\d+)\)",
    ):
        match = re.search(pattern, f"{body}\n{title}", re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def detect_issue_type(body: str, title: str = "") -> str:
    """Determine conventional commit type from issue body, falling back to title prefix."""
    body_lower = (body or "").lower()
    if "## type\nbug" in body_lower:
        return "fix"
    if "## type\nrefactor" in body_lower:
        return "refactor"
    if "## type\nmigration" in body_lower:
        return "refactor"
    if "## type\ndocs" in body_lower:
        return "docs"
    if "## type\nchore" in body_lower:
        return "chore"
    if "## type\n" not in body_lower and title:
        match = _TYPE_PREFIX_RE.match(title)
        if match:
            return match.group(1).lower()
    return "feat"


_TYPE_PREFIX_RE = re.compile(r"^(fix|feat|refactor|docs|chore):\s*", re.IGNORECASE)


def strip_type_prefix(title: str) -> str:
    """Remove a leading conventional-commit type prefix from a title."""
    return _TYPE_PREFIX_RE.sub("", title)


def build_pr_body(
    issue: dict,
    attempts: int = 0,
    duration: float = 0,
    cost_usd: float = 0.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> str:
    """Build the PR description markdown."""
    body = (
        f"Closes #{issue['number']}\n\n"
        f"## Summary\n"
        f"{issue['title']}\n\n"
        f"## Test Plan\n"
        f"- `{cfg.verify_cmd}` — all tests pass\n"
    )
    if cfg.lint_command:
        body += f"- `{cfg.lint_command}` — clean\n"
    body += "\n"
    if attempts > 0:
        total_tokens = input_tokens + cache_creation_tokens + cache_read_tokens + output_tokens
        body += (
            f"## AutoLoop Run Stats\n"
            f"- Attempts: {attempts}/{cfg.max_retries}\n"
            f"- Duration: {duration:.0f}s\n"
            f"- Input tokens: {input_tokens:,}\n"
            f"- Cache creation tokens: {cache_creation_tokens:,}\n"
            f"- Cache read tokens: {cache_read_tokens:,}\n"
            f"- Output tokens: {output_tokens:,}\n"
            f"- Total tokens: {total_tokens:,}\n"
            f"- Cost: ${cost_usd:.2f}\n\n"
        )
    body += "Automated implementation by AutoLoop."
    return body


def collect_verification_errors(
    ahead_count: str,
    test_rc: int,
    test_out: str,
    lint_rc: int,
    changed_files: list[str],
    test_pattern: str = "tests/*.py",
    issue_type: str = "feat",
    test_gate_skip_types: list[str] | None = None,
) -> list[str]:
    """Build error list from verification subprocess results."""
    errors = []
    if ahead_count.strip() == "0" or not ahead_count.strip():
        errors.append("No commits on branch")
    if test_rc != 0:
        errors.append(f"Tests failed:\n{test_out[-500:]}")
    if lint_rc != 0:
        errors.append("Lint or format check failed")
    skip_types = test_gate_skip_types if test_gate_skip_types is not None else []
    if test_pattern and issue_type not in skip_types:
        test_files = [f for f in changed_files if fnmatch.fnmatch(f, test_pattern)]
        if not test_files:
            errors.append("No test files were added or modified")
    return errors


# --- Lockfile ---


def acquire_lock(repo_dir: Path) -> bool:
    """Acquire lockfile. Returns False if another run is active."""
    lockfile = repo_dir / ".autoloop.lock"
    if lockfile.exists():
        try:
            pid = int(lockfile.read_text().strip())
            os.kill(pid, 0)
            return False
        except (ProcessLookupError, ValueError):
            pass
    lockfile.write_text(str(os.getpid()))
    return True


def release_lock(repo_dir: Path):
    """Remove the lockfile."""
    lockfile = repo_dir / ".autoloop.lock"
    lockfile.unlink(missing_ok=True)


def log_run(
    issue_number: int,
    success: bool,
    attempts: int,
    duration: float,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    run_type: str = "implement",
    pr_number: int | None = None,
    repo_dir: Path | None = None,
    auto_merge: dict | None = None,
):
    """Append a JSON entry to the run history log."""
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "type": run_type,
        "issue": issue_number,
        "success": success,
        "attempts": attempts,
        "duration_seconds": round(duration),
        "cost_usd": round(cost_usd, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
    }
    if pr_number is not None:
        entry["pr_number"] = pr_number
    if auto_merge is not None:
        entry["auto_merge"] = auto_merge
    log_file = (repo_dir or Path()) / "autoloop" / "run_history.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# --- Active session detection ---


def _get_ppid_via_proc(pid: int) -> int | None:
    """Read parent PID from /proc (Linux)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.split(")")[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _get_ppid_via_ps(pid: int) -> int | None:
    """Read parent PID via ps (portable fallback for macOS, etc.)."""
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip())
    except (FileNotFoundError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def _get_own_pid_chain() -> set[int]:
    """Collect PIDs from the current process up through all ancestors."""
    chain = set()
    pid = os.getpid()
    use_proc = Path("/proc").is_dir()
    while pid > 0:
        chain.add(pid)
        ppid = _get_ppid_via_proc(pid) if use_proc else _get_ppid_via_ps(pid)
        if ppid is None:
            if use_proc:
                ppid = _get_ppid_via_ps(pid)
            if ppid is None:
                break
        if ppid in chain or ppid <= 0:
            break
        pid = ppid
    return chain


def detect_active_claude_session(project_dir: str) -> bool | None:
    """Check if an interactive Claude Code session is active in the project directory.

    Returns True if a session is detected, False if none found, or None if
    detection is inconclusive (tools unavailable).
    """
    project_dir = os.path.realpath(project_dir)
    logging.debug("detect_active_claude_session: resolved project_dir=%s", project_dir)

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
        if len(parts) <= 1:
            continue
        cmdline = parts[1]
        if "--dangerously-skip-permissions" in cmdline:
            continue
        if ".claude/shell-snapshots/" in cmdline:
            continue
        pids.append(pid)

    if not pids:
        return False

    own_chain = _get_own_pid_chain()
    pids = [p for p in pids if p not in own_chain]
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
            logging.debug("detect_active_claude_session: session cwd=%s", cwd)
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
            logging.debug("detect_active_claude_session: session cwd=%s", cwd)
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


def create_branch(issue: dict, repo_dir: Path | None = None) -> str:
    """Create feature branch from latest main, cleaning up any stale branch first."""
    branch = build_branch_name(issue)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir, check=True)
    subprocess.run(["git", "pull", "origin", "main"], cwd=repo_dir, check=True)
    subprocess.run(
        ["git", "push", "origin", "--delete", branch],
        cwd=repo_dir,
        capture_output=True,
    )
    subprocess.run(["git", "checkout", "-B", branch], cwd=repo_dir, check=True)
    return branch


def build_implementation_prompt(issue: dict, repo_dir: Path | None = None) -> str:
    """Build the full prompt for the implementation agent."""
    base = repo_dir or Path()
    claude_md = (base / "CLAUDE.md").read_text()

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
        comment_bodies = [c.get("body", "") for c in data.get("comments", []) if c.get("body")]
        if comment_bodies:
            full_context += "\n\n--- Issue Comments ---"
            for i, body in enumerate(comment_bodies, 1):
                full_context += f"\n\nComment {i}:\n{body}"

    full_context, metric_targets = parse_and_strip_metric_targets(full_context)
    if metric_targets:
        logging.info(
            "Stripped %d metric target(s) from issue #%s: %s",
            len(metric_targets),
            issue["number"],
            metric_targets,
        )

    issue_url = f"https://github.com/{cfg.repo}/issues/{issue['number']}"
    full_context = truncate_spec(full_context, cfg.spec_truncation, issue_url)

    issue_type = detect_issue_type(issue.get("body", "") or "", issue.get("title", ""))
    skip_types = cfg.test_gate_skip_types if cfg.test_gate_skip_types else []

    prompt = (
        f"## Task\n\n"
        f"Implement GitHub issue #{issue['number']}: {issue['title']}\n\n"
        f"## Issue Details\n\n{full_context}\n\n"
        f"## Project Conventions\n\n{claude_md}\n\n"
        f"## Implementation Checklist\n\n"
        f"1. Read the files listed in 'Files to Modify'\n"
        f"2. Implement the changes described in the issue\n"
    )

    step = 3
    if cfg.test_pattern and issue_type not in skip_types:
        prompt += f"{step}. Add or update tests matching the nearest existing test file\n"
        step += 1
    prompt += f"{step}. Run `{cfg.verify_cmd}` — all tests must pass\n"
    step += 1
    if cfg.lint_command:
        prompt += f"{step}. Run `{cfg.lint_command}` — must be clean\n"
        step += 1
    prompt += f"{step}. If README.md needs updating (new tools, commands), update it\n"
    step += 1
    commit_types = sorted({"fix", "feat", "refactor"} | set(skip_types))
    types_str = ", ".join(commit_types)
    prompt += (
        f"{step}. Stage and commit:\n"
        f"   `git add <specific files>`\n"
        f"   `git commit -m '<type>: <description> (#{issue['number']})'\n"
        f"   Types: {types_str}\n"
        f"   Keep first line under 70 chars\n\n"
        f"## Rules\n\n"
        f"- Follow existing code patterns in this repo\n"
        f"- Do not add features beyond what the issue asks for\n"
        f"- When verification fails, fix the implementation — do not delete, skip,"
        f" or loosen tests, and do not add broad exception handlers to force green\n"
        f"- When acceptance criteria are ambiguous, take the most conservative reading,"
        f" state the assumption in the PR body, and label needs-human if criteria conflict\n"
    )
    if cfg.test_pattern or cfg.lint_command:
        skippable = " or ".join(
            part
            for part in (
                "tests" if cfg.test_pattern else "",
                "lint" if cfg.lint_command else "",
            )
            if part
        )
        prompt += f"- Do not skip {skippable}\n"
    prompt += "- Do not run git push\n"

    return prompt


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


def design_issue(issue: dict, repo_dir: Path | None = None) -> str:
    """Generate an implementation design proposal for the issue via Claude."""
    base = repo_dir or Path()
    claude_md = (base / "CLAUDE.md").read_text()
    prompt = DESIGN_PROMPT.format(
        number=issue["number"],
        title=issue["title"],
        body=issue.get("body", "") or "",
        conventions=claude_md,
    )
    return run_claude(prompt, cfg.impl_model, cfg.impl_timeout, repo_dir=repo_dir).text


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


def build_timeout_comment(attempt: int, timeout_seconds: int) -> str:
    """Build an actionable guidance comment for implementation timeout."""
    return (
        f"**AutoLoop Attempt {attempt} failed: implementation timeout ({timeout_seconds}s)**\n\n"
        f"Possible fixes:\n"
        f"- Increase timeout: set `impl_timeout = {timeout_seconds * 2}` in autoloop.toml\n"
        f"- Or set env var: `AUTOLOOP_TIMEOUT={timeout_seconds * 2}`\n"
        f"- Decompose the issue into smaller sub-issues (target ≤ 2 story points)\n"
        f"- Add implementation hints to the issue body to reduce exploration time"
    )


def post_timeout_failure(number: int, attempt: int, timeout_seconds: int):
    """Post timeout failure with actionable guidance as a comment on the issue."""
    comment = build_timeout_comment(attempt, timeout_seconds)
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", cfg.repo, "--body", comment],
    )


def post_attempt_failure(number: int, attempt: int, errors: str):
    """Post verification failure as a comment on the issue."""
    comment = f"**AutoLoop Attempt {attempt} failed:**\n\n```\n{errors[-2000:]}\n```"
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", cfg.repo, "--body", comment],
    )


def implement(
    issue: dict, previous_errors: str | None = None, *, repo_dir: Path | None = None
) -> ClaudeResult:
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
    return run_claude(prompt, cfg.impl_model, cfg.impl_timeout, repo_dir=repo_dir)


def is_branch_empty(branch: str, repo_dir: Path | None = None) -> bool:
    """Return True if the branch has zero commits ahead of main."""
    result = subprocess.run(
        ["git", "rev-list", "--count", f"main..{branch}"],
        capture_output=True,
        text=True,
        cwd=repo_dir,
    )
    count = result.stdout.strip() if result.returncode == 0 else ""
    return count == "0" or count == ""


def mutation_gate(branch: str, issue_type: str, repo_dir: Path | None = None) -> None:
    """Verify tests actually exercise the implementation by reverting source files.

    Raises RuntimeError if tests still pass after reverting source-only changes.
    """
    skip_types = cfg.test_gate_skip_types if cfg.test_gate_skip_types else []
    if issue_type in skip_types:
        return

    if not cfg.test_pattern:
        return

    diff_result = subprocess.run(
        ["git", "diff", "--name-status", f"main..{branch}"],
        capture_output=True,
        text=True,
        cwd=repo_dir,
    )
    changed_files: list[tuple[str, str]] = []
    for line in diff_result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2:
            changed_files.append((parts[0], parts[1]))

    source_files = [
        (status, f) for status, f in changed_files if not fnmatch.fnmatch(f, cfg.test_pattern)
    ]
    if not source_files:
        return

    modified = [f for status, f in source_files if status != "A"]
    added = [f for status, f in source_files if status == "A"]
    all_paths = [f for _, f in source_files]
    base = repo_dir or Path()

    try:
        if modified:
            subprocess.run(
                ["git", "checkout", "main", "--"] + modified,
                cwd=repo_dir,
                check=True,
            )
        for f in added:
            os.remove(base / f)
        result = subprocess.run(
            cfg.verify_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=cfg.test_timeout,
        )
        if result.returncode == 0:
            raise RuntimeError("tests pass without the implementation")
    finally:
        subprocess.run(
            ["git", "checkout", branch, "--"] + all_paths,
            cwd=repo_dir,
        )


def scan_test_integrity_violations(diff_text: str, patterns: list[str]) -> list[str]:
    """Scan a unified diff of test files for patterns that weaken tests.

    Returns a list of human-readable violation descriptions.
    """
    violations = []
    for line in diff_text.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            stripped = line[1:].strip()
            if stripped.startswith("def test_"):
                violations.append(f"Deleted test function: {stripped}")
            elif stripped.startswith("class Test"):
                violations.append(f"Deleted test class: {stripped}")
            elif stripped.startswith("assert ") or stripped.startswith("assert("):
                violations.append(f"Removed assertion: {stripped}")
        elif line.startswith("+") and not line.startswith("+++"):
            stripped = line[1:].strip()
            for pattern in patterns:
                if pattern in stripped:
                    violations.append(f"Added weakening marker: {stripped}")
                    break
            if re.match(r"except\s*:", stripped) or re.match(r"except\s+Exception\s*:", stripped):
                violations.append(f"Added bare exception handler: {stripped}")
    return violations


def check_test_integrity(branch: str, repo_dir: Path | None = None) -> list[str]:
    """Run the test-integrity guard on test file diffs.

    Returns a list of violations, empty if the guard is disabled or no issues found.
    """
    if not cfg.test_integrity_guard:
        return []

    if not cfg.test_pattern:
        return []

    diff_files = subprocess.run(
        ["git", "diff", "--name-only", f"main..{branch}"],
        capture_output=True,
        text=True,
        cwd=repo_dir,
    )
    test_files = [
        f
        for f in diff_files.stdout.strip().split("\n")
        if f and fnmatch.fnmatch(f, cfg.test_pattern)
    ]
    if not test_files:
        return []

    diff = subprocess.run(
        ["git", "diff", f"main..{branch}", "--"] + test_files,
        capture_output=True,
        text=True,
        cwd=repo_dir,
    )
    return scan_test_integrity_violations(diff.stdout, cfg.test_integrity_patterns)


def verify_implementation(
    branch: str, issue_body: str = "", title: str = "", repo_dir: Path | None = None
) -> tuple[bool, str]:
    """Verify the agent actually produced valid work."""
    cwd = repo_dir or Path()
    ahead = subprocess.run(
        ["git", "rev-list", "--count", f"main..{branch}"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    tests = subprocess.run(
        cfg.verify_cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=cfg.test_timeout,
    )
    if cfg.lint_command:
        lint = subprocess.run(
            cfg.lint_command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        lint_rc = lint.returncode
    else:
        lint_rc = 0
    diff = subprocess.run(
        ["git", "diff", "--name-only", "main"],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    changed = [f for f in diff.stdout.strip().split("\n") if f]

    errors = collect_verification_errors(
        ahead_count=ahead.stdout if ahead.returncode == 0 else "",
        test_rc=tests.returncode,
        test_out=tests.stdout,
        lint_rc=lint_rc,
        changed_files=changed,
        test_pattern=cfg.test_pattern,
        issue_type=detect_issue_type(issue_body, title),
        test_gate_skip_types=cfg.test_gate_skip_types,
    )
    if errors:
        return False, "\n".join(errors)
    return True, ""


REVIEW_PROMPT = """\
Review this implementation against the original issue.

Issue #{number}: {title}
{issue_body}

Files changed in this PR ({file_count} files):
{changed_files}

IMPORTANT: Only report issues found in the files listed above.
Do not reference files not in this list.

Diff:
{diff}

Evaluate:
1. Does the implementation satisfy each acceptance criterion?
2. Are the tests meaningful (not just pass-through stubs)?
3. Does the code follow existing patterns in the codebase?
4. ONLY report issues you can directly point to in the diff above. \
Do not infer, assume, or speculate about files or changes not shown. \
If you are uncertain whether something is in the diff, do not report it.

Respond with JSON only:
{{
  "approved": true | false,
  "issues": ["issue 1", "issue 2"],
  "summary": "one line"
}}
"""


def build_changed_files_manifest(file_list: list[str]) -> str:
    """Format a list of changed files as a bulleted manifest for the review prompt."""
    if not file_list:
        return "- (no files changed)"
    return "\n".join(f"- {f}" for f in file_list)


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


def review_implementation(
    issue: dict, branch: str, pr_number: int | None = None, repo_dir: Path | None = None
) -> tuple[bool, str]:
    """Review the implementation for semantic quality via Claude.

    When pr_number is provided, uses `gh pr diff` for the canonical GitHub diff.
    Falls back to `git diff` for pre-PR reviews during implementation.
    """
    if pr_number is not None:
        diff = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", cfg.repo],
            capture_output=True,
            text=True,
        ).stdout

        name_only = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "--repo", cfg.repo, "--name-only"],
            capture_output=True,
            text=True,
        ).stdout
    else:
        subprocess.run(
            ["git", "fetch", "origin", "main"],
            capture_output=True,
            cwd=repo_dir,
        )
        diff = subprocess.run(
            ["git", "diff", f"main..{branch}"],
            capture_output=True,
            text=True,
            cwd=repo_dir,
        ).stdout

        name_only = subprocess.run(
            ["git", "diff", "--name-only", f"main..{branch}"],
            capture_output=True,
            text=True,
            cwd=repo_dir,
        ).stdout
    changed_files = [f for f in name_only.strip().split("\n") if f]

    prompt = REVIEW_PROMPT.format(
        number=issue["number"],
        title=issue["title"],
        issue_body=issue.get("body", "") or "",
        changed_files=build_changed_files_manifest(changed_files),
        file_count=len(changed_files),
        diff=diff[:8000],
    )
    result = run_claude(prompt, cfg.impl_model, cfg.impl_timeout, repo_dir=repo_dir)
    if not result.success:
        approved, feedback = False, "Review call failed (timeout or non-zero exit)."
    else:
        approved, feedback = parse_review_response(result.text)

    if cfg.jev_mode == "shadow":
        from autoloop.config import RepoContext
        from autoloop.jev import should_auto_merge
        from autoloop.jev_log import log_decision

        issue_text = f"Title: {issue.get('title', '')}\n\nBody:\n{issue.get('body') or ''}"
        jev_result = None
        jev_error = None
        t0 = time.monotonic()
        try:
            jev_result = should_auto_merge(
                issue_text,
                diff,
                api_key_env=cfg.jev_api_key_env,
                model=cfg.jev_model,
                timeout=cfg.jev_timeout_seconds,
                gate_low=cfg.jev_gate_low,
                gate_high=cfg.jev_gate_high,
                mode=cfg.jev_mode,
            )
        except Exception as exc:
            jev_error = str(exc)
        latency = time.monotonic() - t0

        record = {
            "point": "auto-merge",
            "issue": issue["number"],
            "jev_call": jev_result if jev_error is None else {"error": jev_error},
            "incumbent_call": {"approved": approved, "feedback": feedback},
            "outcome": "incumbent",
            "ttft": round((jev_result or {}).get("latency", latency), 3),
            "cost": (jev_result or {}).get("cost", 0.0),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if pr_number is not None:
            record["pr"] = pr_number
        try:
            base = repo_dir or Path()
            ctx = RepoContext.for_data_dir(base / "autoloop")
            log_decision(record, ctx)
        except Exception:
            logging.exception("Failed to log Jev auto-merge decision")

    return approved, feedback


def ensure_clean_main(repo_dir: Path | None = None):
    """Reset to a clean main branch, discarding any leftover state."""
    subprocess.run(["git", "checkout", "--", "."], cwd=repo_dir)
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir)
    subprocess.run(["git", "pull", "--ff-only", "origin", "main"], cwd=repo_dir)


def cleanup_branch(branch: str, repo_dir: Path | None = None):
    """Delete failed branch locally and remotely."""
    subprocess.run(["git", "checkout", "main"], cwd=repo_dir)
    subprocess.run(["git", "branch", "-D", branch], cwd=repo_dir)
    subprocess.run(
        ["git", "push", "origin", "--delete", branch],
        cwd=repo_dir,
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
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    repo_dir: Path | None = None,
) -> int | None:
    """Create PR with conventional format. Returns PR number or None."""
    issue_type = detect_issue_type(issue.get("body", ""), issue.get("title", ""))
    clean_title = strip_type_prefix(issue["title"])[:60]
    title = f"{issue_type}: {clean_title} (#{issue['number']})"
    body = build_pr_body(
        issue,
        attempts=attempts,
        duration=duration,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
    )
    result = subprocess.run(
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
        capture_output=True,
        text=True,
        cwd=repo_dir,
    )
    if result.returncode == 0 and result.stdout.strip():
        match = re.search(r"/pull/(\d+)", result.stdout.strip())
        if match:
            return int(match.group(1))
    return None


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


# --- Auto-fix loop ---


def run_auto_fix_loop(
    pr_number: int, issue: dict, cfg: AutoLoopConfig, repo_dir: Path | None = None
) -> None:
    """Bounded review→fix loop on a PR. Labels needs-human on exhaustion."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    if "needs-human" in labels:
        return

    last_review_output = ""
    for round_num in range(1, cfg.max_pr_review_rounds + 1):
        review = subprocess.run(
            ["autoloop", "review-pr", str(pr_number)],
            capture_output=True,
            text=True,
            cwd=repo_dir,
        )
        last_review_output = review.stdout
        print(
            f"  Auto-fix round {round_num}/{cfg.max_pr_review_rounds}: "
            f"review {'passed' if review.returncode == 0 else 'failed'}"
        )

        if review.returncode == 0:
            return

        if round_num < cfg.max_pr_review_rounds:
            subprocess.run(
                ["autoloop", "fix-pr", str(pr_number)],
                capture_output=True,
                text=True,
                cwd=repo_dir,
            )

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
    comment = (
        f"**AutoLoop auto-fix exhausted ({cfg.max_pr_review_rounds} rounds):**\n\n"
        f"```\n{last_review_output[-2000:]}\n```"
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


# --- Auto-merge ---


def wait_for_ci(pr_number: int, timeout: int = 600, poll_interval: int = 30) -> bool:
    """Poll GitHub CI checks until they complete or timeout. Returns True if all pass."""
    deadline = time.time() + timeout
    while time.time() < deadline:
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
            time.sleep(poll_interval)
            continue
        try:
            checks = json.loads(result.stdout)
        except json.JSONDecodeError:
            time.sleep(poll_interval)
            continue
        if not checks:
            return True
        buckets = {c.get("bucket", "") for c in checks}
        if "pending" in buckets:
            time.sleep(poll_interval)
            continue
        return "fail" not in buckets
    return False


def try_auto_merge(
    branch: str, pr_number: int | None, repo_dir: Path | None = None
) -> tuple[str, dict]:
    """Attempt auto-merge after review passes. Returns (decision, gate_metrics)."""
    if not cfg.auto_merge:
        return "skipped-disabled", {}

    if pr_number is None:
        return "skipped-disabled", {}

    from autoloop.eval import (
        compute_snapshot,
        enrich_pr_data_with_runs,
        fetch_pr_data,
        is_auto_merge_ready,
        load_run_history,
    )

    runs = load_run_history()
    pr_data = fetch_pr_data(cfg.repo)
    pr_data = enrich_pr_data_with_runs(pr_data, runs)
    snapshot = compute_snapshot(runs, pr_data)

    success_rate = snapshot.get("first_attempt_rate", 0)
    edit_rate = snapshot.get("human_edit_rate", 0)
    merged_clean = snapshot.get("merged_pr_count", 0) - snapshot.get("human_edit_count", 0)

    gates = {
        "success_rate": success_rate,
        "edit_rate": edit_rate,
        "merged_clean_count": merged_clean,
    }

    ready = is_auto_merge_ready(
        success_rate,
        edit_rate,
        merged_clean,
        edit_rate_threshold=cfg.auto_merge_edit_rate_threshold,
        success_threshold=cfg.auto_merge_success_threshold,
        volume_floor=cfg.auto_merge_volume_floor,
    )

    if ready != "Yes":
        return "skipped-unqualified", gates

    from autoloop.config import touches_protected_path

    diff = subprocess.run(
        ["git", "diff", "--name-only", f"main..{branch}"],
        capture_output=True,
        text=True,
        cwd=repo_dir,
    )
    changed_files = [f for f in diff.stdout.strip().split("\n") if f]
    if touches_protected_path(changed_files, cfg.protected_paths):
        return "skipped-protected", gates

    print(f"  Auto-merge: waiting for CI on PR #{pr_number}...")
    if not wait_for_ci(pr_number):
        return "skipped-ci-failed", gates

    result = subprocess.run(
        [
            "gh",
            "pr",
            "merge",
            str(pr_number),
            "--repo",
            cfg.repo,
            "--squash",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"  Auto-merge: PR #{pr_number} merged.")
        return "merged", gates

    return "skipped-merge-failed", gates


# --- Orchestration ---


def implement_single_issue(
    issue: dict,
    require_design: bool = False,
    auto_fix: bool = False,
    ctx: RepoContext | None = None,
) -> bool:
    """Implement one issue end-to-end. Returns True if PR created successfully."""
    repo_dir = ctx.repo_dir if ctx else None
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

        ensure_clean_main(repo_dir)

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

        branch = create_branch(issue, repo_dir)
        print(f"  Branch: {branch}")

        last_errors = None
        empty_branch_failure = False
        timeout_failure = False
        approved = False
        for attempt in range(1, cfg.max_retries + 1):
            print(f"  Attempt {attempt}/{cfg.max_retries}...")
            result = implement(issue, previous_errors=last_errors, repo_dir=repo_dir)
            claude_results.append(result)
            final_attempt = attempt

            if result.timed_out:
                print(f"  Implementation timed out after {cfg.impl_timeout}s.")
                post_timeout_failure(issue["number"], attempt, cfg.impl_timeout)
                timeout_failure = True
                break

            if is_branch_empty(branch, repo_dir):
                print(f"  {EMPTY_BRANCH_DIAGNOSTIC}")
                post_attempt_failure(issue["number"], attempt, EMPTY_BRANCH_DIAGNOSTIC)
                empty_branch_failure = True
                break

            valid, errors = verify_implementation(
                branch,
                issue_body=issue.get("body", ""),
                title=issue.get("title", ""),
                repo_dir=repo_dir,
            )
            if not valid:
                print(f"  Verification failed:\n{errors}")
                last_errors = errors
                post_attempt_failure(issue["number"], attempt, errors)
                continue

            try:
                mutation_gate(
                    branch,
                    detect_issue_type(issue.get("body", ""), issue.get("title", "")),
                    repo_dir,
                )
            except (RuntimeError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
                gate_msg = f"Mutation gate error: {e}"
                print(f"  {gate_msg}")
                last_errors = gate_msg
                post_attempt_failure(issue["number"], attempt, gate_msg)
                continue

            print("  Verification passed. Reviewing implementation...")
            approved, feedback = review_implementation(issue, branch, repo_dir=repo_dir)
            if not approved:
                print(f"  Review failed:\n{feedback}")
                last_errors = feedback
                post_attempt_failure(issue["number"], attempt, feedback)
                if auto_fix:
                    success = True
                    break
                continue

            success = True
            print("  Review passed.")
            break

        elapsed = time.time() - start_time
        total_cost = sum(r.cost_usd for r in claude_results)
        total_input = sum(r.input_tokens for r in claude_results)
        total_output = sum(r.output_tokens for r in claude_results)
        total_cache_read = sum(r.cache_read_tokens for r in claude_results)
        total_cache_creation = sum(r.cache_creation_tokens for r in claude_results)
        total_all = total_input + total_cache_creation + total_cache_read + total_output

        if not success:
            if timeout_failure:
                print("  Implementation timed out. Labeling needs-human.")
            elif empty_branch_failure:
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
            cleanup_branch(branch, repo_dir)
            log_run(
                issue["number"],
                False,
                final_attempt,
                elapsed,
                total_cost,
                total_input,
                total_output,
                total_cache_read,
                total_cache_creation,
                repo_dir=repo_dir,
            )
            return False

        integrity_violations = check_test_integrity(branch, repo_dir)

        subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo_dir)
        pr_number = create_pr(
            issue,
            branch,
            attempts=final_attempt,
            duration=elapsed,
            cost_usd=total_cost,
            input_tokens=total_input,
            output_tokens=total_output,
            cache_creation_tokens=total_cache_creation,
            cache_read_tokens=total_cache_read,
            repo_dir=repo_dir,
        )
        label_in_review(issue["number"])
        print(f"  PR created for #{issue['number']}.")

        if integrity_violations:
            logging.warning(
                "Test-integrity guard found %d violation(s) in #%s:\n%s",
                len(integrity_violations),
                issue["number"],
                "\n".join(f"  - {v}" for v in integrity_violations),
            )
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
            if pr_number is not None:
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

        if auto_fix and pr_number is not None and not approved:
            run_auto_fix_loop(pr_number, issue, cfg, repo_dir)

        if integrity_violations:
            auto_merge_decision, auto_merge_gates = "skipped-integrity", {}
        else:
            auto_merge_decision, auto_merge_gates = try_auto_merge(branch, pr_number, repo_dir)
        if cfg.auto_merge:
            print(f"  Auto-merge: {auto_merge_decision}")

        subprocess.run(["git", "checkout", "main"], cwd=repo_dir)

        print(f"\n--- AutoLoop Run Stats (#{issue['number']}) ---")
        print(f"  Duration: {elapsed:.0f}s")
        print(f"  Claude calls: {len(claude_results)}")
        print(f"  Input tokens: {total_input:,}")
        print(f"  Cache creation tokens: {total_cache_creation:,}")
        print(f"  Cache read tokens: {total_cache_read:,}")
        print(f"  Output tokens: {total_output:,}")
        print(f"  Total tokens: {total_all:,}")
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
            total_cache_creation,
            repo_dir=repo_dir,
            auto_merge={
                "decision": auto_merge_decision,
                "gates": auto_merge_gates,
            },
        )

        return True
    except Exception as exc:
        logging.exception("implement_single_issue failed for #%s", issue.get("number"))
        raise SystemicError(str(exc)) from exc


def implement_targeted_issue(
    number: int,
    require_design: bool = False,
    auto_fix: bool = False,
    ctx: RepoContext | None = None,
) -> bool:
    """Implement a specific issue by number, bypassing label and point checks."""
    issue = get_issue_by_number(number)
    if not issue:
        print(f"#{number}: could not fetch issue, aborting.")
        return False

    if not dependencies_met(issue):
        print(f"#{number}: dependencies not met, aborting.")
        return False

    success = implement_single_issue(
        issue, require_design=require_design, auto_fix=auto_fix, ctx=ctx
    )
    print(f"\nImplemented {1 if success else 0} issue(s) this run.")
    return success


def main(
    ctx: RepoContext | None = None, issue=None, max_issues=1, require_design=False, auto_fix=False
):
    global cfg
    if cfg is None:
        cfg = load_config()

    if ctx is None:
        repo_dir = Path(cfg.project_dir) if cfg.project_dir else Path()
        ctx = RepoContext(repo_dir)

    logging.debug("main: resolved project_dir=%s from ctx", ctx.repo_dir)
    if cfg.implement_isolation != "worktree":
        session_detected = detect_active_claude_session(str(ctx.repo_dir))
        if session_detected is True:
            print(
                "Active Claude Code session detected in this directory.\n"
                "Close it, or move the Claude Code session to a parent folder."
            )
            return

    if not acquire_lock(ctx.repo_dir):
        print("Another implementation is running. Exiting.")
        return

    try:
        cleanup_merged_labels()
        unblock_ready_issues()

        if issue is not None:
            implement_targeted_issue(
                issue, require_design=require_design, auto_fix=auto_fix, ctx=ctx
            )
            return

        implemented = 0
        while implemented < max_issues:
            top_issue = get_top_ready_issue()
            if not top_issue:
                print("No more ready issues.")
                break

            try:
                success = implement_single_issue(
                    top_issue, require_design=require_design, auto_fix=auto_fix, ctx=ctx
                )
            except SystemicError as exc:
                print(f"Systemic failure, aborting run: {exc}")
                break

            if success:
                implemented += 1

        print(f"\nImplemented {implemented} issue(s) this run.")
    finally:
        release_lock(ctx.repo_dir)


if __name__ == "__main__":
    main()
