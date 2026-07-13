"""Fix a broken PR: merge conflicts, stale base, lint failures, or test failures.

Detects what's wrong with a PR and applies the appropriate fix:
- Stale base: rebase on main, push
- Merge conflicts: rebase, Claude resolves conflicts, push
- Lint/format failures: run ruff fix + format, commit, push
- Test failures: Claude fixes the code, verify, commit, push
- Combinations: rebase first, then fix checks
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autoloop.config import AutoLoopConfig

REPO_DIR = Path.cwd()

CONFLICT_PROMPT = """\
The following files have merge conflicts after rebasing on main.
Resolve each conflict by choosing the correct code. Keep both sides
where they don't contradict. Preserve all new functionality from the
feature branch while incorporating upstream changes from main.

Conflicting files:
{files}

After resolving, stage the fixed files with `git add` and do NOT commit —
the rebase will continue automatically.
"""

FIX_CHECKS_PROMPT = """\
This PR branch has failing checks after rebasing on main. Fix the issues
described below. Do not change the intent of the code — only fix what's broken.

Failing checks:
{errors}

After fixing, run `{verify_cmd}` and `{lint_cmd}` to confirm everything passes.
Stage and commit the fixes with message: "fix: resolve failing checks after rebase"
Do not run git push.
"""


@dataclass
class PrState:
    """Detected state of a PR."""

    number: int
    branch: str
    state: str
    mergeable: str
    has_conflicts: bool
    failing_checks: list[str] = field(default_factory=list)


def get_pr_info(pr_number: int, repo: str) -> PrState | None:
    """Fetch PR metadata: branch, state, mergeable status, and check results."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefName,state,mergeable,statusCheckRollup",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    data = json.loads(result.stdout)
    failing = []
    for check in data.get("statusCheckRollup", []):
        if check.get("conclusion") == "FAILURE":
            failing.append(check.get("name", "unknown"))

    return PrState(
        number=pr_number,
        branch=data["headRefName"],
        state=data["state"],
        mergeable=data.get("mergeable", "UNKNOWN"),
        has_conflicts=data.get("mergeable") == "CONFLICTING",
        failing_checks=failing,
    )


def checkout_branch(branch: str) -> bool:
    """Fetch and checkout the PR branch."""
    subprocess.run(["git", "fetch", "origin", branch], cwd=REPO_DIR, capture_output=True)
    result = subprocess.run(
        ["git", "checkout", branch], cwd=REPO_DIR, capture_output=True, text=True
    )
    return result.returncode == 0


def update_main() -> None:
    """Fetch latest main."""
    subprocess.run(["git", "fetch", "origin", "main"], cwd=REPO_DIR, capture_output=True)


def _parse_conflicting_files(rebase_output: str) -> list[str]:
    """Extract conflicting file paths from rebase stderr/stdout."""
    files = set()
    for match in re.finditer(r"CONFLICT \([^)]+\): .* in (.+)", rebase_output):
        files.add(match.group(1).strip())
    for match in re.finditer(r"CONFLICT \([^)]+\): Merge conflict in (.+)", rebase_output):
        files.add(match.group(1).strip())
    return sorted(files)


def _get_unmerged_files() -> list[str]:
    """Get files with unresolved merge conflicts from git status."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    files = []
    for line in result.stdout.splitlines():
        if line[:2] in ("UU", "AA", "UD", "DU"):
            files.append(line[3:].strip())
    return files


def is_behind_main(branch: str) -> bool:
    """Check if the branch is behind origin/main."""
    result = subprocess.run(
        ["git", "rev-list", "--count", f"{branch}..origin/main"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    return int(result.stdout.strip() or "0") > 0


def rebase_on_main() -> tuple[bool, list[str]]:
    """Attempt to rebase on main. Returns (clean, conflicting_files)."""
    result = subprocess.run(
        ["git", "rebase", "origin/main"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, []

    combined = result.stdout + result.stderr
    conflicting = _parse_conflicting_files(combined)
    if not conflicting:
        conflicting = _get_unmerged_files()
    return False, conflicting


def resolve_conflicts_with_claude(conflicting_files: list[str], cfg: AutoLoopConfig) -> bool:
    """Use Claude to resolve merge conflicts. Returns True if successful."""
    from autoloop.claude_runner import run_claude

    prompt = CONFLICT_PROMPT.format(files="\n".join(f"- {f}" for f in conflicting_files))
    result = run_claude(prompt, cfg.impl_model, cfg.impl_timeout)
    if not result.success:
        return False

    return not _get_unmerged_files()


def continue_rebase(max_rounds: int = 10) -> bool:
    """Continue the rebase after conflict resolution.

    Handles multi-commit rebases where each commit may need a continue.
    Returns False if unresolved conflicts remain or max_rounds exceeded.
    """
    for _ in range(max_rounds):
        result = subprocess.run(
            ["git", "rebase", "--continue"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_EDITOR": "true"},
        )
        if result.returncode == 0:
            return True
        if _get_unmerged_files():
            return False
        rebase_dir = REPO_DIR / ".git" / "rebase-merge"
        if not rebase_dir.exists():
            return True
    return False


def run_lint_fix(cfg: AutoLoopConfig) -> tuple[bool, str]:
    """Run ruff fix and format to auto-fix lint issues. Returns (fixed, output)."""
    fix_result = subprocess.run(
        ["uv", "run", "ruff", "check", "--fix"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    fmt_result = subprocess.run(
        ["uv", "run", "ruff", "format", "."],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )

    check_result = subprocess.run(
        cfg.lint_command,
        shell=True,
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    output = fix_result.stdout + fmt_result.stdout + check_result.stdout + check_result.stderr
    return check_result.returncode == 0, output


def fix_checks_with_claude(errors: str, cfg: AutoLoopConfig) -> bool:
    """Use Claude to fix failing test/lint issues. Returns True if successful."""
    from autoloop.claude_runner import run_claude

    prompt = FIX_CHECKS_PROMPT.format(
        errors=errors[-2000:],
        verify_cmd=cfg.verify_cmd,
        lint_cmd=cfg.lint_command,
    )
    result = run_claude(prompt, cfg.impl_model, cfg.impl_timeout)
    return result.success


def verify(cfg: AutoLoopConfig) -> tuple[bool, str]:
    """Run verify_cmd and return (passed, output)."""
    result = subprocess.run(
        cfg.verify_cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
        timeout=cfg.test_timeout,
    )
    return result.returncode == 0, result.stdout + result.stderr


def lint_check(cfg: AutoLoopConfig) -> tuple[bool, str]:
    """Run lint command and return (passed, output)."""
    result = subprocess.run(
        cfg.lint_command,
        shell=True,
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    )
    return result.returncode == 0, result.stdout + result.stderr


def has_staged_changes() -> bool:
    """Check if there are staged or unstaged changes to commit."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def commit_fixes() -> bool:
    """Stage all changes and commit."""
    subprocess.run(["git", "add", "-A"], cwd=REPO_DIR, capture_output=True)
    result = subprocess.run(
        ["git", "commit", "-m", "fix: resolve failing checks after rebase"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def force_push(branch: str) -> bool:
    """Force-push the rebased branch."""
    result = subprocess.run(
        ["git", "push", "--force-with-lease", "origin", branch],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def abort_rebase() -> None:
    """Abort an in-progress rebase."""
    subprocess.run(["git", "rebase", "--abort"], cwd=REPO_DIR, capture_output=True)


def restore_main() -> None:
    """Return to main branch."""
    subprocess.run(["git", "checkout", "main"], cwd=REPO_DIR, capture_output=True)


def _handle_rebase(cfg: AutoLoopConfig) -> bool:
    """Rebase on main, resolving conflicts if needed. Returns True on success."""
    clean, conflicting_files = rebase_on_main()

    if clean:
        print("  Rebased cleanly (no conflicts).")
        return True

    if not conflicting_files:
        print("  Rebase failed but no conflicting files detected. Aborting.")
        abort_rebase()
        return False

    print(f"  Conflicts in {len(conflicting_files)} file(s): {', '.join(conflicting_files)}")
    print("  Resolving with Claude...")

    if not resolve_conflicts_with_claude(conflicting_files, cfg):
        print("  Claude could not resolve all conflicts. Aborting.")
        abort_rebase()
        return False

    if not continue_rebase():
        print("  Rebase --continue failed. Aborting.")
        abort_rebase()
        return False

    print("  Conflicts resolved.")
    return True


def _handle_checks(cfg: AutoLoopConfig) -> bool:
    """Fix lint and test failures. Returns True on success."""
    lint_ok, lint_output = lint_check(cfg)
    test_ok, test_output = verify(cfg)

    if lint_ok and test_ok:
        return True

    if not lint_ok:
        print("  Lint check failed. Running auto-fix...")
        fixed, fix_output = run_lint_fix(cfg)
        if fixed:
            print("  Lint auto-fixed.")
        else:
            print("  Lint auto-fix insufficient. Asking Claude...")
            if not fix_checks_with_claude(lint_output, cfg):
                print("  Claude could not fix lint issues.")
                return False

    if not test_ok:
        print("  Tests failed. Asking Claude to fix...")
        if not fix_checks_with_claude(test_output, cfg):
            print("  Claude could not fix test failures.")
            return False

    print("  Re-verifying after fixes...")
    lint_ok, _ = lint_check(cfg)
    test_ok, test_output = verify(cfg)
    if not lint_ok or not test_ok:
        print(f"  Still failing after fix attempt:\n{test_output[-500:]}")
        return False

    print("  All checks pass.")
    return True


def fix_pr(pr_number: int, cfg: AutoLoopConfig) -> bool:
    """Fix a PR by detecting issues and applying appropriate fixes.

    Handles: stale base, merge conflicts, lint failures, test failures,
    and combinations thereof. Returns True if the PR was fixed and pushed.
    """
    print(f"Fixing PR #{pr_number}...")

    pr = get_pr_info(pr_number, cfg.repo)
    if not pr:
        print(f"  Could not find PR #{pr_number}.")
        return False

    if pr.state != "OPEN":
        print(f"  PR #{pr_number} is {pr.state.lower()}, nothing to fix.")
        return False

    print(f"  Branch: {pr.branch}")

    if not checkout_branch(pr.branch):
        print(f"  Could not checkout branch {pr.branch}.")
        return False

    update_main()
    needs_rebase = is_behind_main(pr.branch)
    needs_check_fix = bool(pr.failing_checks)

    if not needs_rebase and not needs_check_fix:
        print("  PR is up-to-date and checks pass. Nothing to fix.")
        restore_main()
        return True

    if needs_rebase:
        print(f"  Branch is behind main.{' Has conflicts.' if pr.has_conflicts else ''}")
        if not _handle_rebase(cfg):
            restore_main()
            return False

    print("  Running checks...")
    if not _handle_checks(cfg):
        restore_main()
        return False

    if has_staged_changes():
        print("  Committing fixes...")
        commit_fixes()

    print("  Pushing...")
    if not force_push(pr.branch):
        print("  Push failed.")
        restore_main()
        return False

    print(f"  PR #{pr_number} fixed and pushed.")
    restore_main()
    return True
