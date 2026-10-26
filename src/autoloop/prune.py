"""Prune stale autoloop worktrees and local branches whose PRs are done.

Never deletes anything it can't prove is safe: dirty worktrees, open PRs, failed PR
lookups and branches with commits not on origin/main are always kept.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autoloop.config import AutoLoopConfig, RepoContext

BRANCH_PREFIX = "autoloop/"
BASE_REF = "origin/main"
UNKNOWN = "UNKNOWN"


@dataclass
class PruneItem:
    """Outcome for one worktree or branch."""

    kind: str  # "worktree" or "branch"
    name: str
    outcome: str
    reason: str = ""
    removed: bool = False
    needs_review: bool = False

    def line(self) -> str:
        text = f"{self.kind} {self.name}: {self.outcome}"
        return f"{text} — {self.reason}" if self.reason else text


@dataclass
class PruneResult:
    items: list[PruneItem] = field(default_factory=list)
    stale_worktrees: int = 0

    @property
    def removed(self) -> int:
        return sum(1 for i in self.items if i.removed)

    @property
    def kept(self) -> int:
        return sum(1 for i in self.items if not i.removed)

    @property
    def needs_review(self) -> int:
        return sum(1 for i in self.items if i.needs_review)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)


def list_worktrees(ctx: RepoContext) -> list[tuple[Path, str | None]]:
    """Return (path, branch) for every registered worktree, branch None when detached."""
    result = _git(["worktree", "list", "--porcelain"], ctx.repo_dir)
    if result.returncode != 0:
        return []
    worktrees: list[tuple[Path, str | None]] = []
    for block in result.stdout.strip().split("\n\n"):
        path = None
        branch = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree "))
            elif line.startswith("branch refs/heads/"):
                branch = line.removeprefix("branch refs/heads/")
        if path is not None:
            worktrees.append((path, branch))
    return worktrees


def list_autoloop_branches(ctx: RepoContext) -> list[str]:
    """Return local branch names under the autoloop/ prefix."""
    result = _git(
        ["for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}"],
        ctx.repo_dir,
    )
    if result.returncode != 0:
        return []
    return [b for b in result.stdout.splitlines() if b.strip()]


def get_pr_state(branch: str, cfg: AutoLoopConfig) -> str | None:
    """Return MERGED, OPEN or CLOSED for *branch*'s PR, None if it has none, UNKNOWN on error.

    With several PRs for one branch, OPEN wins over MERGED over CLOSED so an open PR
    always protects the branch.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                cfg.repo,
                "--head",
                branch,
                "--state",
                "all",
                "--json",
                "state",
            ],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    if result.returncode != 0:
        return UNKNOWN
    try:
        states = {pr["state"] for pr in json.loads(result.stdout)}
    except (json.JSONDecodeError, KeyError, TypeError):
        return UNKNOWN
    for state in ("OPEN", "MERGED", "CLOSED"):
        if state in states:
            return state
    return UNKNOWN if states else None


def is_dirty(wt_path: Path) -> bool:
    """True if the worktree has uncommitted tracked changes or git status fails."""
    result = _git(["status", "--porcelain", "--untracked-files=no"], wt_path)
    return result.returncode != 0 or bool(result.stdout.strip())


def is_fully_merged(branch: str, ctx: RepoContext) -> bool:
    """True if every commit on *branch* is reachable from origin/main."""
    result = _git(["rev-list", "--count", f"{BASE_REF}..refs/heads/{branch}"], ctx.repo_dir)
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) == 0
    except ValueError:
        return False


def delete_branch(branch: str, ctx: RepoContext) -> bool:
    """Delete the local *branch* only; the remote branch is never touched."""
    return _git(["branch", "-D", branch], ctx.repo_dir).returncode == 0


def remove_worktree(wt_path: Path, ctx: RepoContext) -> bool:
    """Remove the worktree at *wt_path*. Callers must check for tracked changes first."""
    # --force only overrides untracked files (e.g. .venv); tracked changes were checked first.
    return _git(["worktree", "remove", "--force", str(wt_path)], ctx.repo_dir).returncode == 0


def _prune_worktree(
    wt_path: Path,
    name: str,
    branch: str | None,
    ctx: RepoContext,
    cfg: AutoLoopConfig,
    dry_run: bool,
) -> tuple[PruneItem, bool]:
    """Decide and apply the outcome for one worktree. Returns (item, is_stale)."""
    if branch is None:
        return PruneItem("worktree", name, "kept (detached)", "no branch checked out"), False

    state = get_pr_state(branch, cfg)
    if state == UNKNOWN:
        return PruneItem("worktree", name, "kept (unknown PR state)", "gh lookup failed"), False
    if state == "OPEN":
        return PruneItem("worktree", name, "kept (open PR)"), False
    if state is None:
        return PruneItem("worktree", name, "kept (no PR)"), False

    if is_dirty(wt_path):
        return (
            PruneItem("worktree", name, "kept (dirty)", f"uncommitted tracked changes, PR {state}"),
            True,
        )

    drop_branch = state == "MERGED" or is_fully_merged(branch, ctx)
    if dry_run:
        outcome = "would remove" if drop_branch else "would remove worktree, keep branch"
        return PruneItem("worktree", name, outcome, f"PR {state}"), True

    if not remove_worktree(wt_path, ctx):
        return PruneItem("worktree", name, "kept (remove failed)", "git worktree remove"), True

    if not drop_branch:
        return (
            PruneItem(
                "worktree",
                name,
                "worktree removed, branch kept (needs human review)",
                f"PR {state}, {branch} has commits not on {BASE_REF}",
                removed=True,
                needs_review=True,
            ),
            True,
        )
    if not delete_branch(branch, ctx):
        return (
            PruneItem(
                "worktree",
                name,
                "worktree removed, branch kept (delete failed)",
                f"PR {state}",
                removed=True,
            ),
            True,
        )
    return PruneItem("worktree", name, "removed", f"PR {state}", removed=True), True


def _prune_branch(branch: str, ctx: RepoContext, cfg: AutoLoopConfig, dry_run: bool) -> PruneItem:
    """Decide and apply the outcome for a local branch with no worktree."""
    state = get_pr_state(branch, cfg)
    if state == UNKNOWN:
        return PruneItem("branch", branch, "kept (unknown PR state)", "gh lookup failed")
    if state == "OPEN":
        return PruneItem("branch", branch, "kept (open PR)")

    pr_label = f"PR {state}" if state else "no PR"
    if state != "MERGED" and not is_fully_merged(branch, ctx):
        return PruneItem(
            "branch",
            branch,
            "kept (needs human review)",
            f"{pr_label}, commits not on {BASE_REF}",
            needs_review=True,
        )
    if dry_run:
        return PruneItem("branch", branch, "would remove", pr_label)
    if not delete_branch(branch, ctx):
        return PruneItem("branch", branch, "kept (delete failed)", pr_label)
    return PruneItem("branch", branch, "removed", pr_label, removed=True)


def prune(ctx: RepoContext, cfg: AutoLoopConfig, *, dry_run: bool = False) -> PruneResult:
    """Prune autoloop worktrees and local autoloop/* branches whose PRs are done.

    With dry_run, nothing is removed; outcomes and stale_worktrees are still computed.
    """
    result = PruneResult()
    worktrees = list_worktrees(ctx)
    checked_out = {branch: path for path, branch in worktrees if branch}
    worktree_root = ctx.worktree_dir.resolve()

    handled: set[str] = set()
    for wt_path, branch in worktrees:
        resolved = wt_path.resolve()
        if not resolved.is_relative_to(worktree_root):
            continue
        name = str(resolved.relative_to(worktree_root))
        item, stale = _prune_worktree(wt_path, name, branch, ctx, cfg, dry_run)
        result.items.append(item)
        result.stale_worktrees += stale
        if branch:
            handled.add(branch)

    for branch in list_autoloop_branches(ctx):
        if branch in handled:
            continue
        if branch in checked_out:
            result.items.append(
                PruneItem("branch", branch, "kept (checked out)", str(checked_out[branch]))
            )
            continue
        result.items.append(_prune_branch(branch, ctx, cfg, dry_run))

    return result


def format_report(result: PruneResult) -> str:
    """Render one line per item, the needs-review list, and a summary of counts."""
    lines = [item.line() for item in result.items]
    if not lines:
        lines.append("Nothing to prune.")
    review = [i for i in result.items if i.needs_review]
    if review:
        lines.append("")
        lines.append("Needs human review:")
        lines.extend(f"  {i.kind} {i.name}: {i.reason}" for i in review)
    lines.append("")
    lines.append(
        f"Summary: {result.removed} removed, {result.kept} kept, "
        f"{result.needs_review} needing review"
    )
    return "\n".join(lines)


def main(ctx: RepoContext, cfg: AutoLoopConfig) -> int:
    """Entry point for `autoloop prune`. Always returns 0; kept items are not an error."""
    result = prune(ctx, cfg)
    print(format_report(result))
    return 0
