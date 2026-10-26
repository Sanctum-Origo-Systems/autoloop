"""Doctor subcommand — check runner framework."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from autoloop.claude_runner import run_claude
from autoloop.config import RepoContext

# A check fn may return WARN instead of True/False: reported, but does not fail doctor.
WARN: Literal["warn"] = "warn"

PROBE_FILE = "autoloop-doctor-probe.txt"
PROBE_REMOTE = "nowhere-remote"
PROBE_TIMEOUT = 300
PROBE_PROMPT = (
    "This is an autoloop doctor permission probe. Do these steps in order:\n"
    f"1. Use the Write tool to create {PROBE_FILE} containing the single line: probe\n"
    f"2. Run: git add {PROBE_FILE}\n"
    '3. Run: git commit -m "chore: autoloop doctor probe"\n'
    f"4. Run: git push {PROBE_REMOTE} HEAD\n"
    "If a step is denied or fails, do not try alternatives; move on to the next step. "
    "Finish with one line per step saying whether it succeeded."
)


@dataclass
class Check:
    name: str
    fn: Callable[[], tuple[bool | Literal["warn"], str]]
    fix_hint: str


@dataclass
class Result:
    name: str
    passed: bool
    message: str
    fix_hint: str
    warned: bool = False


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


def check_main_settings(ctx: RepoContext) -> tuple[bool | Literal["warn"], str]:
    """Check that origin/main:.claude/settings.json is readable JSON with allow rules.

    run_claude passes this file via --settings; if it can't be read, implement runs
    silently get no permission rules.
    """
    no_rules = "implement runs will have no permission rules"
    try:
        result = subprocess.run(
            ["git", "-C", str(ctx.repo_dir), "show", "origin/main:.claude/settings.json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return False, f"git not found; {no_rules}"
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        return False, f"cannot read origin/main:.claude/settings.json ({detail}); {no_rules}"
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return False, f"origin/main:.claude/settings.json is not valid JSON ({exc}); {no_rules}"

    permissions = data.get("permissions") if isinstance(data, dict) else None
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    if not isinstance(allow, list) or not allow:
        return WARN, "origin/main:.claude/settings.json has no permissions.allow rules"
    return True, f"origin/main:.claude/settings.json valid ({len(allow)} allow rules)"


def check_claude_settings_flag() -> tuple[bool, str]:
    """Check that the installed claude CLI supports --settings (used by run_claude)."""
    try:
        result = subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return False, "claude CLI not found"
    if result.returncode != 0:
        return False, "claude --help failed to run"
    if "--settings" in result.stdout + result.stderr:
        return True, "claude CLI supports --settings"

    try:
        version_result = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=10
        )
        version = version_result.stdout.strip() or version_result.stderr.strip() or "unknown"
    except FileNotFoundError:
        version = "unknown"
    return (
        False,
        f"installed claude CLI ({version}) does not support --settings; "
        "implement runs will have no permission rules",
    )


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60
    )


def check_live_probe(ctx: RepoContext) -> tuple[bool, str]:
    """Run run_claude in a throwaway worktree of origin/main and confirm rules apply.

    PASS if the agent can write and commit a probe file and its push to a nonexistent
    remote is reported in permission_denials. The worktree and branch are always removed.
    """
    from autoloop.config import AutoLoopConfig, load_config

    try:
        cfg = load_config(ctx.repo_dir / "autoloop.toml")
    except Exception:
        cfg = AutoLoopConfig()

    tmp_root = Path(tempfile.mkdtemp(prefix="autoloop-doctor-probe-"))
    worktree = tmp_root / "worktree"
    branch = f"autoloop/doctor-probe-{uuid.uuid4().hex[:8]}"
    try:
        added = _git(ctx.repo_dir, "worktree", "add", "-b", branch, str(worktree), "origin/main")
        if added.returncode != 0:
            return False, f"could not create probe worktree: {added.stderr.strip()}"

        result = run_claude(PROBE_PROMPT, cfg.triage_model, PROBE_TIMEOUT, repo_dir=worktree)
        cost = f"cost ${result.cost_usd:.4f}"
        if not result.success:
            reason = "timed out" if result.timed_out else "failed"
            return False, f"claude -p {reason} ({cost})"

        file_ok = (worktree / PROBE_FILE).exists()
        log = _git(worktree, "log", "--format=%H", "origin/main..HEAD", "--", PROBE_FILE)
        commit_ok = log.returncode == 0 and bool(log.stdout.strip())
        push_denied = any(
            "git push" in str((d.get("tool_input") or {}).get("command", ""))
            for d in result.permission_denials
            if isinstance(d, dict)
        )
    finally:
        _git(ctx.repo_dir, "worktree", "remove", "--force", str(worktree))
        _git(ctx.repo_dir, "branch", "-D", branch)
        shutil.rmtree(tmp_root, ignore_errors=True)

    problems = []
    if not file_ok:
        problems.append("probe file not written")
    if not commit_ok:
        problems.append("probe commit not made")
    if not push_denied:
        problems.append("push not in permission_denials")
    if problems:
        return False, f"{', '.join(problems)} ({cost})"
    return True, f"write and commit allowed, push denied ({cost})"


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
    """Verify implement resolves its working dir under ctx.worktree_dir when isolation is on.

    Uses a dry-run resolution: no worktree is created and no branch is switched.
    """
    from autoloop.config import load_config
    from autoloop.implement_issue import build_branch_name, resolve_working_dir

    config_path = ctx.repo_dir / "autoloop.toml"
    try:
        cfg = load_config(config_path)
    except Exception:
        return False, "could not load autoloop.toml to read isolation config"

    if cfg.implement_isolation != "worktree":
        return True, "Worktree isolation: disabled"

    probe_branch = build_branch_name({"number": 0, "title": "doctor probe"})
    try:
        resolved = resolve_working_dir(cfg.implement_isolation, ctx, probe_branch, dry_run=True)
    except Exception as exc:
        return False, f"Worktree isolation: enabled, but working dir resolution failed: {exc}"

    if not Path(resolved).resolve().is_relative_to(ctx.worktree_dir.resolve()):
        return (
            False,
            f"Worktree isolation: enabled, but implement resolves to {resolved}, "
            f"which is not under {ctx.worktree_dir}",
        )
    return True, f"Worktree isolation: enabled (implement runs in {resolved})"


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


def get_checks(ctx: RepoContext, live: bool = False) -> list[Check]:
    """Return the default set of doctor checks; live adds the paid run_claude probe."""
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
        Check(
            name="origin/main settings",
            fn=lambda: check_main_settings(ctx),
            fix_hint="commit .claude/settings.json with permissions.allow rules to main, "
            'then run "git fetch origin"',
        ),
        Check(
            name="claude --settings support",
            fn=check_claude_settings_flag,
            fix_hint="npm install -g @anthropic-ai/claude-code@latest",
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

    if live:
        checks.append(
            Check(
                name="Live permission probe",
                fn=lambda: check_live_probe(ctx),
                fix_hint="check .claude/settings.json on origin/main allows Write, git add "
                "and git commit, and denies git push",
            )
        )

    return checks


def run_checks(checks: list[Check]) -> list[Result]:
    """Run each check, print pass/fail, return results."""
    if not checks:
        print("No checks registered.")
        return []

    results: list[Result] = []
    for check in checks:
        try:
            status, message = check.fn()
        except Exception as exc:
            status = False
            message = str(exc)

        warned = status == WARN
        passed = warned or status is True
        results.append(
            Result(
                name=check.name,
                passed=passed,
                message=message,
                fix_hint=check.fix_hint,
                warned=warned,
            )
        )

        if warned:
            print(f"\033[33m!\033[0m {check.name}: {message}")
            print(f"  hint: {check.fix_hint}")
        elif passed:
            print(f"\033[32m✓\033[0m {check.name}: {message}")
        else:
            print(f"\033[31m✗\033[0m {check.name}: {message}")
            print(f"  hint: {check.fix_hint}")

    return results
