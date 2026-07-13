"""Fix a PR that has a stale base or merge conflicts.

Rebases the PR branch on main, uses Claude to resolve any conflicts,
runs verification, and force-pushes the result.
"""

from __future__ import annotations

import json
import subprocess
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


def get_pr_branch(pr_number: int, repo: str) -> str | None:
    """Get the branch name for a PR number."""
    result = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "headRefName",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout).get("headRefName")


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

    diff_result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    conflicting = [f for f in diff_result.stdout.strip().split("\n") if f]
    return False, conflicting


def resolve_conflicts_with_claude(conflicting_files: list[str], cfg: AutoLoopConfig) -> bool:
    """Use Claude to resolve merge conflicts. Returns True if successful."""
    from autoloop.claude_runner import run_claude

    prompt = CONFLICT_PROMPT.format(files="\n".join(f"- {f}" for f in conflicting_files))
    result = run_claude(prompt, cfg.impl_model, cfg.impl_timeout)
    if not result.success:
        return False

    unresolved = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
    )
    return not unresolved.stdout.strip()


def continue_rebase() -> bool:
    """Continue the rebase after conflict resolution."""
    result = subprocess.run(
        ["git", "rebase", "--continue"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "GIT_EDITOR": "true"},
    )
    if result.returncode != 0:
        unresolved = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
        )
        if unresolved.stdout.strip():
            return False
        return continue_rebase()
    return True


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


def fix_pr(pr_number: int, cfg: AutoLoopConfig) -> bool:
    """Fix a PR by rebasing on main and resolving any conflicts.

    Returns True if the PR was successfully rebased and pushed.
    """
    print(f"Fixing PR #{pr_number}...")

    branch = get_pr_branch(pr_number, cfg.repo)
    if not branch:
        print(f"  Could not find branch for PR #{pr_number}.")
        return False

    print(f"  Branch: {branch}")

    if not checkout_branch(branch):
        print(f"  Could not checkout branch {branch}.")
        return False

    update_main()

    clean, conflicting_files = rebase_on_main()

    if clean:
        print("  Rebased cleanly (no conflicts).")
    else:
        print(f"  Conflicts in {len(conflicting_files)} file(s): {', '.join(conflicting_files)}")
        print("  Resolving with Claude...")

        if not resolve_conflicts_with_claude(conflicting_files, cfg):
            print("  Claude could not resolve all conflicts. Aborting.")
            abort_rebase()
            restore_main()
            return False

        if not continue_rebase():
            print("  Rebase --continue failed. Aborting.")
            abort_rebase()
            restore_main()
            return False

        print("  Conflicts resolved.")

    print("  Verifying...")
    passed, output = verify(cfg)
    if not passed:
        print(f"  Verification failed:\n{output[-500:]}")
        restore_main()
        return False

    print("  Verification passed. Pushing...")
    if not force_push(branch):
        print("  Push failed.")
        restore_main()
        return False

    print(f"  PR #{pr_number} fixed and pushed.")
    restore_main()
    return True
