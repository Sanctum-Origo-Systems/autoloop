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


def _read_last_run() -> dict | None:
    """Read the last entry from run_history.jsonl."""
    log_file = Path.cwd() / "autoloop" / "run_history.jsonl"
    if not log_file.exists():
        return None
    lines = log_file.read_text().strip().splitlines()
    if not lines:
        return None
    return json.loads(lines[-1])


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
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("MCP server requires the 'mcp' package. Install with:")
        print("  uv tool install 'autoloop[mcp]'")
        raise SystemExit(1)

    server = FastMCP("autoloop-mcp")

    def _spawn(cmd: list[str]) -> None:
        """Fully detach a subprocess so it doesn't block the MCP connection."""
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @server.tool()
    def autoloop_implement(issue: int | None = None, max_issues: int = 1) -> str:
        """Trigger autoloop implementation. Starts async, returns immediately."""
        cmd = ["autoloop", "implement"]
        if issue is not None:
            cmd.extend(["--issue", str(issue)])
        cmd.extend(["--max-issues", str(max_issues)])

        _spawn(cmd)

        if issue:
            return f"Started implementation of issue #{issue}."
        return f"Started implementation (max {max_issues} issue(s))."

    @server.tool()
    def autoloop_triage() -> str:
        """Trigger autoloop triage of untriaged issues."""
        _spawn(["autoloop", "triage"])
        return "Started triage run."

    @server.tool()
    def autoloop_fix_pr(pr_number: int) -> str:
        """Fix a PR by rebasing on main and resolving any merge conflicts."""
        _spawn(["autoloop", "fix-pr", str(pr_number)])
        return f"Started fix-pr for PR #{pr_number}."

    @server.tool()
    def autoloop_status() -> str:
        """Check last run result, active runs, ready issue count, and next scheduled runs."""
        from autoloop.config import load_config

        parts = []
        cfg = load_config()

        last = _read_last_run()
        if last:
            status = "success" if last["success"] else "failed"
            parts.append(
                f"Last run: issue #{last['issue']} — {status} — "
                f"${last.get('cost_usd', 0):.2f} — {last['timestamp']}"
            )
        else:
            parts.append("Last run: no history")

        lockfile = Path.cwd() / ".autoloop.lock"
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
