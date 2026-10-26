"""Shared helper: invoke `claude -p` with JSON output and parse real usage."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


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
    permission_denials: list[dict] = field(default_factory=list)


def _fetch_main_settings(repo_dir: Path) -> str | None:
    """Return origin/main:.claude/settings.json content, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "show", "origin/main:.claude/settings.json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return result.stdout


def run_claude(
    prompt: str, model: str, timeout: int, *, repo_dir: Path | None = None
) -> ClaudeResult:
    """Run `claude -p` with JSON output and parse the response.

    When repo_dir is set, fetches .claude/settings.json from origin/main and
    passes it via --settings so allow/deny rules apply in untrusted worktrees.

    Returns ClaudeResult(success=False) on timeout or non-zero exit without
    raising. Falls back to success=True with zeroed usage if the response is
    not valid JSON (e.g. plain text output).
    """
    settings_file: Path | None = None
    try:
        if repo_dir is not None:
            settings_content = _fetch_main_settings(repo_dir)
            if settings_content is not None:
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", prefix="autoloop-settings-", delete=False
                )
                tmp.write(settings_content)
                tmp.close()
                settings_file = Path(tmp.name)

        cmd = ["claude", "-p", "--model", model, "--output-format", "json"]
        if settings_file is not None:
            cmd.extend(["--settings", str(settings_file)])
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
    finally:
        if settings_file is not None:
            settings_file.unlink(missing_ok=True)

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
            permission_denials=data.get("permission_denials") or [],
        )
    except (json.JSONDecodeError, KeyError):
        return ClaudeResult(result.stdout, 0, 0, 0, 0, 0, success=True)
