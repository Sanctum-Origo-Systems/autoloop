"""Preflight checks: run verify and lint commands, report results."""

from __future__ import annotations

import subprocess
import time

from autoloop.config import AutoLoopConfig


def _run_command(cmd: str) -> dict:
    """Run a shell command and return result dict with passed, elapsed, output."""
    start = time.monotonic()
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    elapsed = time.monotonic() - start
    output = result.stdout + result.stderr
    return {
        "skipped": False,
        "passed": result.returncode == 0,
        "elapsed": elapsed,
        "output": output,
    }


def run_preflight(cfg: AutoLoopConfig) -> dict:
    """Run preflight checks and return results for each command."""
    results = {}

    results["verify_cmd"] = _run_command(cfg.verify_cmd)

    if cfg.lint_command:
        results["lint_command"] = _run_command(cfg.lint_command)
    else:
        results["lint_command"] = {
            "skipped": True,
            "passed": False,
            "elapsed": 0.0,
            "output": "",
        }

    return results
