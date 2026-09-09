"""MCP server for remote control of the autoloop build pipeline.

Install with the mcp extra:
    uv tool install 'autoloop[mcp]'

Configure in .mcp.json:
    {"mcpServers": {"autoloop-mcp": {"command": "uv", "args": ["run", "autoloop-mcp"]}}}
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _read_last_runs(base: Path | None = None) -> tuple[dict | None, dict | None]:
    """Read the last implement and last review entries from run_history.jsonl."""
    log_file = (base or Path.cwd()) / "autoloop" / "run_history.jsonl"
    if not log_file.exists():
        return None, None
    lines = log_file.read_text().strip().splitlines()
    if not lines:
        return None, None
    last_impl = None
    last_review = None
    for line in reversed(lines):
        entry = json.loads(line)
        run_type = entry.get("type", "implement")
        if run_type == "review" and last_review is None:
            last_review = entry
        elif run_type != "review" and last_impl is None:
            last_impl = entry
        if last_impl and last_review:
            break
    return last_impl, last_review


def _get_timer_info(prefix: str = "autoloop") -> dict[str, str]:
    """Parse systemd timer state for timers matching the given prefix."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "list-timers"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {}
    if result.returncode != 0:
        return {}
    timers = {}
    for line in result.stdout.splitlines():
        if f"{prefix}-triage" in line:
            parts = line.split()
            left_idx = next((i for i, p in enumerate(parts) if "left" in p.lower()), None)
            if left_idx and left_idx >= 2:
                timers["triage"] = f"in {parts[left_idx - 2]} {parts[left_idx - 1]}"
        elif f"{prefix}-implement" in line:
            parts = line.split()
            left_idx = next((i for i, p in enumerate(parts) if "left" in p.lower()), None)
            if left_idx and left_idx >= 2:
                timers["implement"] = f"in {parts[left_idx - 2]} {parts[left_idx - 1]}"
    return timers


def _is_process_running(name: str) -> bool:
    """Check if a process matching the given command name is running."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def main():
    try:
        from fastmcp import FastMCP
    except ImportError:
        print("MCP server requires the 'fastmcp' package. Install with:")
        print("  uv tool install 'autoloop[mcp]'")
        raise SystemExit(1)

    server = FastMCP("autoloop-mcp")

    def _spawn(cmd: list[str], cwd: str | None = None) -> None:
        """Fully detach a subprocess so it doesn't block the MCP connection."""
        subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @server.tool()
    def autoloop_implement(
        issue: int | None = None, max_issues: int = 1, repo_dir: str | None = None
    ) -> str:
        """Trigger autoloop implementation. Starts async, returns immediately.

        Args:
            issue: Specific issue number to implement.
            max_issues: Maximum number of issues to implement.
            repo_dir: Target repository directory. Defaults to server's working directory.
        """
        cmd = ["autoloop", "implement"]
        if issue is not None:
            cmd.extend(["--issue", str(issue)])
        cmd.extend(["--max-issues", str(max_issues)])

        _spawn(cmd, cwd=repo_dir)

        if issue:
            return f"Started implementation of issue #{issue}."
        return f"Started implementation (max {max_issues} issue(s))."

    @server.tool()
    def autoloop_triage(repo_dir: str | None = None) -> str:
        """Trigger autoloop triage of untriaged issues.

        Args:
            repo_dir: Target repository directory. Defaults to server's working directory.
        """
        _spawn(["autoloop", "triage"], cwd=repo_dir)
        return "Started triage run."

    @server.tool()
    def autoloop_fix_pr(pr_number: int, repo_dir: str | None = None) -> str:
        """Fix a PR by rebasing on main and resolving any merge conflicts.

        Args:
            pr_number: The PR number to fix.
            repo_dir: Target repository directory. Defaults to server's working directory.
        """
        _spawn(["autoloop", "fix-pr", str(pr_number)], cwd=repo_dir)
        return f"Started fix-pr for PR #{pr_number}."

    @server.tool()
    async def autoloop_review_pr(pr_number: int, repo_dir: str | None = None) -> str:
        """Review a PR (mutation gate + semantic review, no merge).

        Returns the review result with cost information. Runs in a thread
        so it does not block the MCP server for other callers.

        Args:
            pr_number: The PR number to review.
            repo_dir: Target repository directory. Defaults to server's working directory.
        """
        import asyncio

        from autoloop.cli import review_pr
        from autoloop.config import load_config

        base = Path(repo_dir) if repo_dir else Path.cwd()
        cfg = load_config(path=base / "autoloop.toml")

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, review_pr, pr_number, cfg)

        status = "passed" if result["success"] else "failed"
        cost = result["cost_usd"]
        inp = result["input_tokens"]
        out = result["output_tokens"]
        return (
            f"Review {status} for PR #{pr_number}. "
            f"Cost: ${cost:.2f}, tokens: {inp:,} input / {out:,} output."
        )

    @server.tool()
    def autoloop_preflight(repo_dir: str | None = None) -> str:
        """Run preflight checks (verify and lint commands) on the current branch.

        Args:
            repo_dir: Target repository directory. Defaults to server's working directory.
        """
        _spawn(["autoloop", "preflight"], cwd=repo_dir)
        return "Started preflight checks."

    @server.tool()
    def autoloop_status(repo_dir: str | None = None) -> str:
        """Check last run result, active runs, ready issue count, and next scheduled runs.

        Args:
            repo_dir: Target repository directory. Defaults to server's working directory.
        """
        from autoloop.config import load_config

        base = Path(repo_dir) if repo_dir else Path.cwd()
        parts = []
        cfg = load_config(path=base / "autoloop.toml")

        last_impl, last_review = _read_last_runs(base)
        if last_impl:
            status = "success" if last_impl["success"] else "failed"
            parts.append(
                f"Last implement: issue #{last_impl['issue']} — {status} — "
                f"${last_impl.get('cost_usd', 0):.2f} — {last_impl['timestamp']}"
            )
        if last_review:
            status = "success" if last_review["success"] else "failed"
            parts.append(
                f"Last review: PR #{last_review['pr_number']} — {status} — "
                f"${last_review.get('cost_usd', 0):.2f} — {last_review['timestamp']}"
            )
        if not last_impl and not last_review:
            parts.append("Last run: no history")

        lockfile = base / ".autoloop.lock"
        active = []
        if lockfile.exists():
            active.append("implementation")
        if _is_process_running("autoloop triage"):
            active.append("triage")
        parts.append(f"Active: {', '.join(active)}" if active else "Active: idle")

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
                "number",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            count = len(json.loads(result.stdout))
            parts.append(f"Ready issues: {count}")

        timers = _get_timer_info(cfg.timer_prefix)
        if timers:
            timer_strs = [f"{k}: {v}" for k, v in timers.items()]
            parts.append(f"Next scheduled: {', '.join(timer_strs)}")
        else:
            parts.append("Next scheduled: no scheduled timers found")

        return "\n".join(parts)

    server.run()


if __name__ == "__main__":
    main()
