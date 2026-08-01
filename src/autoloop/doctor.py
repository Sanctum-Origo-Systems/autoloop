"""Doctor command: validate environment prerequisites before first run."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from autoloop.config import REPO_DIR, AutoLoopConfig, load_config

PASS = "✓"
FAIL = "✗"


def check_config(config_path: Path | None = None) -> tuple[bool, str]:
    """Check that autoloop.toml exists and parses correctly."""
    path = config_path or (REPO_DIR / "autoloop.toml")
    try:
        load_config(path)
        return True, f"{PASS} autoloop.toml found and valid"
    except FileNotFoundError:
        return False, (
            f"{FAIL} autoloop.toml not found at {path}\n"
            "  Fix: run 'autoloop init --repo owner/repo' to create one"
        )
    except Exception as e:
        return False, (
            f"{FAIL} autoloop.toml has errors: {e}\n  Fix: check TOML syntax in autoloop.toml"
        )


def check_claude_settings(repo_dir: Path | None = None) -> tuple[bool, str]:
    """Check that .claude/settings.json exists in the repo."""
    base = repo_dir or REPO_DIR
    settings_path = base / ".claude" / "settings.json"
    if not settings_path.exists():
        return False, (
            f"{FAIL} .claude/settings.json not found\n"
            "  Fix: run 'autoloop init --repo owner/repo' to scaffold it"
        )
    try:
        data = json.loads(settings_path.read_text())
        perms = data.get("permissions", {})
        allow_count = len(perms.get("allow", []))
        return True, f"{PASS} .claude/settings.json found with {allow_count} permissions"
    except (json.JSONDecodeError, KeyError) as e:
        return False, (
            f"{FAIL} .claude/settings.json is invalid: {e}\n"
            "  Fix: check JSON syntax in .claude/settings.json"
        )


def check_claude_cli() -> tuple[bool, str]:
    """Check that claude CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, (
            f"{FAIL} claude CLI not installed\n  Fix: npm install -g @anthropic-ai/claude-code"
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"{FAIL} claude CLI timed out\n  Fix: check that claude CLI is working properly"
        )

    if result.returncode != 0:
        return False, (
            f"{FAIL} claude CLI failed: {result.stderr.strip()}\n  Fix: reinstall claude CLI"
        )

    version = result.stdout.strip()

    try:
        auth_result = subprocess.run(
            ["claude", "--print-api-key"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"{FAIL} claude CLI auth check timed out\n  Fix: run 'claude' to authenticate"
        )

    if auth_result.returncode != 0:
        return False, (
            f"{FAIL} claude CLI not authenticated (v{version})\n  Fix: run 'claude' to authenticate"
        )

    return True, f"{PASS} claude CLI installed ({version}) and authenticated"


def check_gh_cli() -> tuple[bool, str]:
    """Check that gh CLI is installed and authenticated."""
    try:
        result = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return False, (
            f"{FAIL} gh CLI not installed\n  Fix: https://cli.github.com/ or 'brew install gh'"
        )
    except subprocess.TimeoutExpired:
        return False, (f"{FAIL} gh CLI timed out\n  Fix: check that gh CLI is working properly")

    if result.returncode != 0:
        return False, (f"{FAIL} gh CLI failed: {result.stderr.strip()}\n  Fix: reinstall gh CLI")

    version_line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "unknown"

    try:
        auth_result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, (f"{FAIL} gh auth check timed out\n  Fix: run 'gh auth login'")

    if auth_result.returncode != 0:
        return False, (
            f"{FAIL} gh CLI not authenticated ({version_line})\n  Fix: run 'gh auth login'"
        )

    return True, f"{PASS} gh CLI installed and authenticated"


def check_no_active_session(repo_dir: Path | None = None) -> tuple[bool, str]:
    """Check that no active Claude Code session is running in the working directory."""
    base = (repo_dir or REPO_DIR).resolve()
    lock_file = base / ".autoloop.lock"
    if lock_file.exists():
        return False, (
            f"{FAIL} Active Claude Code session conflict (.autoloop.lock exists)\n"
            "  Fix: close the running session or remove .autoloop.lock if stale"
        )
    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            for pid_str in result.stdout.strip().splitlines():
                pid = pid_str.strip()
                if not pid:
                    continue
                try:
                    proc_cwd = os.readlink(f"/proc/{pid}/cwd")
                    if Path(proc_cwd).resolve() == base:
                        return False, (
                            f"{FAIL} Active Claude Code session detected in this directory"
                            f" (PID {pid})\n"
                            "  Fix: close the running Claude Code session before running autoloop"
                        )
                except OSError:
                    continue
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return True, f"{PASS} No active Claude Code session conflict"


def check_protected_paths(cfg: AutoLoopConfig, repo_dir: Path | None = None) -> tuple[bool, str]:
    """Check that protected_paths config references valid paths."""
    base = repo_dir or REPO_DIR
    invalid = []
    for p in cfg.protected_paths:
        full = base / p
        if not full.exists():
            invalid.append(p)
    if invalid:
        paths_str = ", ".join(invalid)
        return False, (
            f"{FAIL} Protected paths not found: {paths_str}\n"
            "  Fix: update protected_paths in autoloop.toml to match existing paths"
        )
    return True, f"{PASS} Protected paths config valid"


def check_verify_cmd(cfg: AutoLoopConfig, repo_dir: Path | None = None) -> tuple[bool, str]:
    """Run verify_cmd on the main branch and report pass/fail with output on failure."""
    cmd = cfg.verify_cmd
    if not cmd:
        return True, f"{PASS} No verify_cmd configured (skipped)"

    base = repo_dir or REPO_DIR

    on_main = False
    try:
        current = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(base),
            timeout=10,
        )
        on_main = current.returncode == 0 and current.stdout.strip() == "main"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        on_main = True

    run_dir = str(base)
    worktree_dir = None

    if not on_main:
        worktree_dir = tempfile.mkdtemp(prefix="autoloop-doctor-")
        try:
            wt_result = subprocess.run(
                ["git", "worktree", "add", "--detach", worktree_dir, "main"],
                capture_output=True,
                text=True,
                cwd=str(base),
                timeout=30,
            )
            if wt_result.returncode == 0:
                run_dir = worktree_dir
            else:
                shutil.rmtree(worktree_dir, ignore_errors=True)
                worktree_dir = None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.rmtree(worktree_dir, ignore_errors=True)
            worktree_dir = None

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=cfg.test_timeout,
            cwd=run_dir,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f'{FAIL} verify_cmd timed out after {cfg.test_timeout}s ("{cmd}")\n'
            "  Fix: check that verify_cmd completes in time, or increase test_timeout"
        )
    finally:
        _cleanup_worktree(worktree_dir, str(base))

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return False, (
            f'{FAIL} verify_cmd failed on main branch ("{cmd}" → exit {result.returncode})\n'
            f"  Output:\n{_indent(output)}\n"
            f"  Fix: resolve the errors above so verify_cmd passes on main"
        )

    return True, f'{PASS} verify_cmd passes on main branch ("{cmd}" → exit 0)'


def _cleanup_worktree(worktree_dir: str | None, repo_dir: str) -> None:
    """Remove a temporary git worktree if one was created."""
    if not worktree_dir:
        return
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_dir],
            capture_output=True,
            text=True,
            cwd=repo_dir,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        shutil.rmtree(worktree_dir, ignore_errors=True)


def check_test_pattern(cfg: AutoLoopConfig, repo_dir: Path | None = None) -> tuple[bool, str]:
    """If test_pattern is set, verify it matches at least one file."""
    pattern = cfg.test_pattern
    if not pattern:
        return True, f"{PASS} No test_pattern configured (skipped)"

    base = repo_dir or REPO_DIR
    full_pattern = str(base / pattern)
    matches = glob.glob(full_pattern, recursive=True)
    if not matches:
        return False, (
            f'{FAIL} test_pattern "{pattern}" matches no files\n'
            "  Fix: update test_pattern in autoloop.toml to match your test files"
        )

    return True, f'{PASS} test_pattern "{pattern}" matches {len(matches)} file(s)'


def _indent(text: str, prefix: str = "    ") -> str:
    """Indent each line of text."""
    return "\n".join(prefix + line for line in text.splitlines())


def run_doctor(
    config_path: Path | None = None,
    repo_dir: Path | None = None,
    run_verify: bool = True,
) -> list[tuple[bool, str]]:
    """Run all doctor checks and return list of (passed, message) tuples."""
    results = []

    passed, msg = check_config(config_path)
    results.append((passed, msg))

    if not passed:
        return results

    cfg = load_config(config_path or (repo_dir or REPO_DIR) / "autoloop.toml")

    results.append(check_claude_settings(repo_dir))
    results.append(check_claude_cli())
    results.append(check_gh_cli())
    results.append(check_no_active_session(repo_dir))
    results.append(check_protected_paths(cfg, repo_dir))

    if run_verify:
        results.append(check_verify_cmd(cfg, repo_dir))

    results.append(check_test_pattern(cfg, repo_dir))

    return results
