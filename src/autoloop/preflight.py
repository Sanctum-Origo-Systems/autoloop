"""Preflight checks: run verify and lint commands, report results."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from autoloop.config import AutoLoopConfig, RepoContext


def _run_command(cmd: str, cwd: Path | None = None) -> dict:
    """Run a shell command and return result dict with passed, elapsed, output."""
    start = time.monotonic()
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    elapsed = time.monotonic() - start
    output = result.stdout + result.stderr
    return {
        "skipped": False,
        "passed": result.returncode == 0,
        "elapsed": elapsed,
        "output": output,
    }


def run_preflight(cfg: AutoLoopConfig, ctx: RepoContext | None = None) -> dict:
    """Run preflight checks and return results for each command."""
    cwd = ctx.repo_dir if ctx else None
    results = {}

    results["verify_cmd"] = _run_command(cfg.verify_cmd, cwd=cwd)

    if cfg.lint_command:
        results["lint_command"] = _run_command(cfg.lint_command, cwd=cwd)
    else:
        results["lint_command"] = {
            "skipped": True,
            "passed": False,
            "elapsed": 0.0,
            "output": "",
        }

    return results
