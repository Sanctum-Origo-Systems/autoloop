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


def _get_timer_info() -> dict[str, str]:
    """Parse systemd timer state for autoloop timers."""
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
        if "autoloop-triage" in line:
            parts = line.split()
            left_idx = next((i for i, p in enumerate(parts) if "left" in p.lower()), None)
            if left_idx and left_idx >= 2:
                timers["triage"] = f"in {parts[left_idx - 2]} {parts[left_idx - 1]}"
        elif "autoloop-implement" in line:
            parts = line.split()
            left_idx = next((i for i, p in enumerate(parts) if "left" in p.lower()), None)
            if left_idx and left_idx >= 2:
                timers["implement"] = f"in {parts[left_idx - 2]} {parts[left_idx - 1]}"
    return timers


def main():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("MCP server requires the 'mcp' package. Install with:")
        print("  uv tool install 'autoloop[mcp]'")
        raise SystemExit(1)

    server = FastMCP("autoloop-mcp")

    @server.tool()
    def autoloop_implement(issue: int | None = None, max_issues: int = 1) -> str:
        """Trigger autoloop implementation. Starts async, returns immediately."""
        cmd = ["autoloop", "implement"]
        if issue is not None:
            cmd.extend(["--issue", str(issue)])
        cmd.extend(["--max-issues", str(max_issues)])

        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if issue:
            return f"Started implementation of issue #{issue}."
        return f"Started implementation (max {max_issues} issue(s))."

    @server.tool()
    def autoloop_triage() -> str:
        """Trigger autoloop triage of untriaged issues."""
        subprocess.Popen(
            ["autoloop", "triage"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return "Started triage run."

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
        if lockfile.exists():
            parts.append("Active: implementation running")
        else:
            parts.append("Active: idle")

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

        timers = _get_timer_info()
        if timers:
            timer_strs = [f"{k}: {v}" for k, v in timers.items()]
            parts.append(f"Next scheduled: {', '.join(timer_strs)}")
        else:
            parts.append("Next scheduled: no scheduled timers found")

        return "\n".join(parts)

    server.run()


if __name__ == "__main__":
    main()
