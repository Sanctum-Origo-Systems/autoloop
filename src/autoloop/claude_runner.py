"""Shared helper: invoke `claude -p` with JSON output and parse real usage."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ClaudeResult:
    """Outcome of a single `claude -p` call with exact token and cost data."""

    text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    success: bool
    timed_out: bool = False


def _read_allowed_tools(repo_dir: Path | None) -> list[str]:
    """Read permissions.allow from <repo_dir>/.claude/settings.json."""
    if repo_dir is None:
        return []
    settings_path = repo_dir / ".claude" / "settings.json"
    try:
        data = json.loads(settings_path.read_text())
        allow = data.get("permissions", {}).get("allow", [])
        if isinstance(allow, list):
            return [str(e) for e in allow if e]
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return []


def run_claude(
    prompt: str, model: str, timeout: int, *, repo_dir: Path | None = None
) -> ClaudeResult:
    """Run `claude -p` with JSON output and parse the response.

    Returns ClaudeResult(success=False) on timeout or non-zero exit without
    raising. Falls back to success=True with zeroed usage if the response is
    not valid JSON (e.g. plain text output).
    """
    allowed_tools = _read_allowed_tools(repo_dir)
    cmd = ["claude", "-p", "--model", model, "--output-format", "json"]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools, "--"]
    cmd.append(prompt)

    try:
        result = subprocess.run(
            cmd,
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ClaudeResult("", 0, 0, 0, 0, 0, success=False, timed_out=True)

    if "has not been trusted" in (result.stderr or ""):
        logger.warning(
            "Workspace %s has not been trusted; allowedTools list was ignored",
            repo_dir,
        )

    if result.returncode != 0:
        return ClaudeResult("", 0, 0, 0, 0, 0, success=False)

    try:
        data = json.loads(result.stdout)
        usage = data.get("usage", {})
        return ClaudeResult(
            text=data.get("result", ""),
            cost_usd=data.get("total_cost_usd", 0),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
            success=True,
        )
    except (json.JSONDecodeError, KeyError):
        return ClaudeResult(result.stdout, 0, 0, 0, 0, 0, success=True)
