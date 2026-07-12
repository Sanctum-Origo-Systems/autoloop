"""Shared helper: invoke `claude -p` with JSON output and parse real usage."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from autoloop.config import REPO_DIR


@dataclass
class ClaudeResult:
    """Outcome of a single `claude -p` call with exact token and cost data."""

    text: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    success: bool


def run_claude(prompt: str, model: str, timeout: int) -> ClaudeResult:
    """Run `claude -p` with JSON output and parse the response.

    Returns ClaudeResult(success=False) on timeout or non-zero exit without
    raising. Falls back to success=True with zeroed usage if the response is
    not valid JSON (e.g. plain text output).
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", model, "--output-format", "json", prompt],
            cwd=REPO_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ClaudeResult("", 0, 0, 0, 0, success=False)

    if result.returncode != 0:
        return ClaudeResult("", 0, 0, 0, 0, success=False)

    try:
        data = json.loads(result.stdout)
        usage = data.get("usage", {})
        return ClaudeResult(
            text=data.get("result", ""),
            cost_usd=data.get("total_cost_usd", 0),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            success=True,
        )
    except (json.JSONDecodeError, KeyError):
        return ClaudeResult(result.stdout, 0, 0, 0, 0, success=True)
