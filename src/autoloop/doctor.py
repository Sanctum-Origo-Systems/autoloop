"""Doctor subcommand — check runner framework."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable

from autoloop.config import RepoContext


@dataclass
class Check:
    name: str
    fn: Callable[[], tuple[bool, str]]
    fix_hint: str


@dataclass
class Result:
    name: str
    passed: bool
    message: str
    fix_hint: str


def check_autoloop_toml(ctx: RepoContext) -> tuple[bool, str]:
    """Validate that autoloop.toml exists and parses without error."""
    from autoloop.config import load_config

    config_path = ctx.repo_dir / "autoloop.toml"
    try:
        load_config(config_path)
    except FileNotFoundError:
        return False, "autoloop.toml not found"
    except Exception as exc:
        return False, f"autoloop.toml invalid: {exc}"
    return True, "autoloop.toml found and valid"


def check_claude_settings(ctx: RepoContext) -> tuple[bool, str]:
    """Validate that .claude/settings.json exists in the repo root."""
    settings_path = ctx.repo_dir / ".claude" / "settings.json"
    if not settings_path.exists():
        return False, ".claude/settings.json not found"
    return True, ".claude/settings.json found"


def check_claude_cli_installed() -> tuple[bool, str]:
    """Check that the claude CLI is installed."""
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return False, "claude CLI not found"
    if result.returncode != 0:
        return False, "claude CLI failed to run"
    version = result.stdout.strip() or result.stderr.strip()
    return True, f"claude CLI installed ({version})"


def check_claude_cli_authenticated() -> tuple[bool, str]:
    """Check that the claude CLI is authenticated."""
    try:
        result = subprocess.run(
            ["claude", "auth", "status"], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return False, "claude CLI not found"
    if result.returncode != 0:
        return False, "claude CLI not authenticated"
    return True, "claude CLI authenticated"


def check_gh_cli_installed() -> tuple[bool, str]:
    """Check that the gh CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return False, "gh CLI not found"
    if result.returncode != 0:
        return False, "gh CLI not authenticated"
    return True, "gh CLI installed and authenticated"


def check_claude_session(ctx: RepoContext) -> tuple[bool, str]:
    """Check if an active Claude Code session is running in the working directory."""
    from autoloop.implement_issue import detect_active_claude_session

    result = detect_active_claude_session(str(ctx.repo_dir))
    if result is True:
        return False, "Active Claude Code session detected in this directory"
    if result is None:
        return True, "Session detection unavailable (pgrep not found), skipping"
    return True, "No active Claude Code session conflict"


def check_jev(ctx: RepoContext) -> tuple[bool, str]:
    """Validate Jev configuration when enabled."""
    import os

    from autoloop.config import load_config

    config_path = ctx.repo_dir / "autoloop.toml"
    try:
        cfg = load_config(config_path)
    except Exception:
        return False, "could not load autoloop.toml to read jev config"

    if cfg.jev_mode == "off":
        return True, "Jev: disabled"

    if os.environ.get(cfg.jev_api_key_env):
        return True, f"Jev: {cfg.jev_mode} mode, API key set"

    return (
        False,
        f"Jev: {cfg.jev_mode} mode, API key MISSING (set env var {cfg.jev_api_key_env}, or set jev.mode = 'off')",
    )


def check_worktree_isolation(ctx: RepoContext) -> tuple[bool, str]:
    """Report worktree isolation status when enabled."""
    from autoloop.config import load_config

    config_path = ctx.repo_dir / "autoloop.toml"
    try:
        cfg = load_config(config_path)
    except Exception:
        return False, "could not load autoloop.toml to read isolation config"

    if cfg.implement_isolation != "worktree":
        return True, ""

    worktree_path = ctx.worktree_dir
    return True, f"Worktree isolation: enabled (path: {worktree_path})"


def check_verify_cmd(ctx: RepoContext) -> tuple[bool, str]:
    """Run the repo's configured verify_cmd and report pass/fail."""
    from autoloop.config import load_config

    config_path = ctx.repo_dir / "autoloop.toml"
    try:
        cfg = load_config(config_path)
    except Exception:
        return False, "could not load autoloop.toml to read verify_cmd"

    if not cfg.verify_cmd or not cfg.verify_cmd.strip():
        return True, "verify_cmd not set, skipping"

    try:
        result = subprocess.run(
            cfg.verify_cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=cfg.test_timeout,
            cwd=ctx.repo_dir,
        )
    except FileNotFoundError:
        return False, f'verify_cmd command not found: "{cfg.verify_cmd}"'
    except subprocess.TimeoutExpired:
        return False, f'verify_cmd timed out after {cfg.test_timeout}s: "{cfg.verify_cmd}"'

    if result.returncode == 0:
        return True, f'verify_cmd passes ("{cfg.verify_cmd}" → exit 0)'

    combined = (result.stdout + result.stderr).strip()
    tail = combined[-500:] if len(combined) > 500 else combined
    msg = f'verify_cmd failed ("{cfg.verify_cmd}" → exit {result.returncode})'
    if tail:
        msg += f"\n  Output: {tail}"
    return False, msg


def get_checks(ctx: RepoContext) -> list[Check]:
    """Return the default set of doctor checks."""
    checks = [
        Check(
            name="autoloop.toml",
            fn=lambda: check_autoloop_toml(ctx),
            fix_hint='run "autoloop init" to generate it',
        ),
        Check(
            name=".claude/settings.json",
            fn=lambda: check_claude_settings(ctx),
            fix_hint='run "autoloop init" to scaffold it, or create manually',
        ),
        Check(
            name="claude CLI installed",
            fn=check_claude_cli_installed,
            fix_hint="npm install -g @anthropic-ai/claude-code",
        ),
        Check(
            name="claude CLI authenticated",
            fn=check_claude_cli_authenticated,
            fix_hint='run "claude auth login"',
        ),
        Check(
            name="gh CLI installed and authenticated",
            fn=check_gh_cli_installed,
            fix_hint='see https://cli.github.com for installation, then run "gh auth login"',
        ),
        Check(
            name="Claude Code session conflict",
            fn=lambda: check_claude_session(ctx),
            fix_hint="close the Claude Code session, or run it from a different directory",
        ),
        Check(
            name="verify_cmd",
            fn=lambda: check_verify_cmd(ctx),
            fix_hint='resolve the error above, then re-run "autoloop doctor"',
        ),
        Check(
            name="Jev config",
            fn=lambda: check_jev(ctx),
            fix_hint="set the API key env var, or set jev.mode = 'off' in autoloop.toml",
        ),
    ]

    from autoloop.config import load_config

    config_path = ctx.repo_dir / "autoloop.toml"
    try:
        cfg = load_config(config_path)
        if cfg.implement_isolation == "worktree":
            checks.append(
                Check(
                    name="Worktree isolation",
                    fn=lambda: check_worktree_isolation(ctx),
                    fix_hint='set implement.isolation = "off" in autoloop.toml to disable',
                ),
            )
    except Exception:
        pass

    return checks


def run_checks(checks: list[Check]) -> list[Result]:
    """Run each check, print pass/fail, return results."""
    if not checks:
        print("No checks registered.")
        return []

    results: list[Result] = []
    for check in checks:
        try:
            passed, message = check.fn()
        except Exception as exc:
            passed = False
            message = str(exc)

        results.append(
            Result(name=check.name, passed=passed, message=message, fix_hint=check.fix_hint)
        )

        if passed:
            print(f"\033[32m✓\033[0m {check.name}: {message}")
        else:
            print(f"\033[31m✗\033[0m {check.name}: {message}")
            print(f"  hint: {check.fix_hint}")

    return results
