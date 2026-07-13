"""Tests for autoloop fix-pr command."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from autoloop.fix_pr import (
    abort_rebase,
    checkout_branch,
    fix_pr,
    force_push,
    get_pr_branch,
    rebase_on_main,
    restore_main,
    update_main,
    verify,
)


def _cfg(**overrides):
    defaults = {
        "repo": "acme-corp/widget",
        "impl_model": "opus",
        "impl_timeout": 600,
        "test_timeout": 60,
        "verify_cmd": "echo ok",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _ok(stdout="", returncode=0):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


# --- get_pr_branch ---


def test_get_pr_branch_returns_branch_name():
    def fake_run(cmd, **kwargs):
        return _ok(stdout='{"headRefName": "autoloop/42-fix-bug"}')

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert get_pr_branch(42, "acme/repo") == "autoloop/42-fix-bug"


def test_get_pr_branch_returns_none_on_failure():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1)

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert get_pr_branch(42, "acme/repo") is None


def test_get_pr_branch_uses_repo():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ok(stdout='{"headRefName": "branch"}')

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        get_pr_branch(42, "my-org/my-repo")

    assert "--repo" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


# --- checkout_branch ---


def test_checkout_branch_returns_true_on_success():
    def fake_run(cmd, **kwargs):
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert checkout_branch("feature-branch") is True


def test_checkout_branch_returns_false_on_failure():
    calls = [0]

    def fake_run(cmd, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            return _ok()
        return _ok(returncode=1)

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert checkout_branch("bad-branch") is False


def test_checkout_branch_fetches_first():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        checkout_branch("my-branch")

    assert calls[0][:2] == ["git", "fetch"]
    assert "my-branch" in calls[0]
    assert calls[1][:2] == ["git", "checkout"]


# --- update_main ---


def test_update_main_fetches_origin():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        update_main()

    assert calls[0] == ["git", "fetch", "origin", "main"]


# --- rebase_on_main ---


def test_rebase_clean():
    def fake_run(cmd, **kwargs):
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        clean, files = rebase_on_main()

    assert clean is True
    assert files == []


def test_rebase_with_conflicts():
    calls = [0]

    def fake_run(cmd, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            return _ok(returncode=1, stdout="CONFLICT")
        return _ok(stdout="src/main.py\nsrc/utils.py\n")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        clean, files = rebase_on_main()

    assert clean is False
    assert files == ["src/main.py", "src/utils.py"]


def test_rebase_with_conflicts_no_files():
    calls = [0]

    def fake_run(cmd, **kwargs):
        calls[0] += 1
        if calls[0] == 1:
            return _ok(returncode=1)
        return _ok(stdout="")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        clean, files = rebase_on_main()

    assert clean is False
    assert files == []


# --- verify ---


def test_verify_passes():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="all tests passed")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        passed, output = verify(_cfg())

    assert passed is True
    assert "all tests passed" in output


def test_verify_fails():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1, stdout="FAILED test_x")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        passed, output = verify(_cfg())

    assert passed is False


def test_verify_uses_cfg_verify_cmd():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        verify(_cfg(verify_cmd="make test"))

    assert captured["cmd"] == "make test"
    assert captured["kwargs"]["shell"] is True


def test_verify_uses_cfg_test_timeout():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        verify(_cfg(test_timeout=30))

    assert captured["timeout"] == 30


# --- force_push ---


def test_force_push_success():
    def fake_run(cmd, **kwargs):
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert force_push("my-branch") is True


def test_force_push_failure():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1)

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert force_push("my-branch") is False


def test_force_push_uses_force_with_lease():
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        force_push("my-branch")

    assert "--force-with-lease" in captured["cmd"]
    assert "my-branch" in captured["cmd"]


# --- abort_rebase ---


def test_abort_rebase_calls_git():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        abort_rebase()

    assert calls[0][:3] == ["git", "rebase", "--abort"]


# --- restore_main ---


def test_restore_main_checks_out_main():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        restore_main()

    assert calls[0][:3] == ["git", "checkout", "main"]


# --- fix_pr orchestration ---


def test_fix_pr_clean_rebase_success(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "gh" and "view" in cmd:
            return _ok(stdout='{"headRefName": "autoloop/42-fix"}')
        if isinstance(cmd, str):
            return _ok(stdout="tests passed")
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        result = fix_pr(42, cfg)

    assert result is True
    out = capsys.readouterr().out
    assert "Rebased cleanly" in out
    assert "fixed and pushed" in out


def test_fix_pr_returns_false_when_branch_not_found(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        return _ok(returncode=1)

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        result = fix_pr(42, cfg)

    assert result is False
    out = capsys.readouterr().out
    assert "Could not find branch" in out


def test_fix_pr_returns_false_when_verification_fails(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "gh" and "view" in cmd:
            return _ok(stdout='{"headRefName": "autoloop/42-fix"}')
        if isinstance(cmd, str) and cfg.verify_cmd in cmd:
            return _ok(returncode=1, stdout="FAILED")
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        result = fix_pr(42, cfg)

    assert result is False
    out = capsys.readouterr().out
    assert "Verification failed" in out


def test_fix_pr_with_conflicts_resolved(capsys):
    cfg = _cfg()
    rebase_calls = [0]

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "gh" and "view" in cmd:
            return _ok(stdout='{"headRefName": "autoloop/42-fix"}')
        if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"]:
            if "--abort" in cmd or "--continue" in cmd:
                return _ok()
            rebase_calls[0] += 1
            if rebase_calls[0] == 1:
                return _ok(returncode=1)
            return _ok()
        if isinstance(cmd, list) and "diff" in cmd and "--diff-filter=U" in cmd:
            if rebase_calls[0] <= 1:
                return _ok(stdout="src/conflict.py\n")
            return _ok(stdout="")
        if isinstance(cmd, str):
            return _ok(stdout="tests passed")
        return _ok()

    def fake_resolve(files, cfg):
        return True

    with (
        patch("autoloop.fix_pr.subprocess.run", fake_run),
        patch("autoloop.fix_pr.resolve_conflicts_with_claude", fake_resolve),
    ):
        result = fix_pr(42, cfg)

    assert result is True
    out = capsys.readouterr().out
    assert "Conflicts in 1 file(s)" in out
    assert "Conflicts resolved" in out


def test_fix_pr_aborts_when_claude_fails(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "gh" and "view" in cmd:
            return _ok(stdout='{"headRefName": "autoloop/42-fix"}')
        if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"]:
            if "--abort" in cmd:
                return _ok()
            return _ok(returncode=1)
        if isinstance(cmd, list) and "diff" in cmd and "--diff-filter=U" in cmd:
            return _ok(stdout="src/conflict.py\n")
        return _ok()

    def fake_resolve(files, cfg):
        return False

    with (
        patch("autoloop.fix_pr.subprocess.run", fake_run),
        patch("autoloop.fix_pr.resolve_conflicts_with_claude", fake_resolve),
    ):
        result = fix_pr(42, cfg)

    assert result is False
    out = capsys.readouterr().out
    assert "could not resolve" in out


def test_fix_pr_returns_false_when_push_fails(capsys):
    cfg = _cfg()
    push_attempted = [False]

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "gh" and "view" in cmd:
            return _ok(stdout='{"headRefName": "autoloop/42-fix"}')
        if isinstance(cmd, list) and "push" in cmd:
            push_attempted[0] = True
            return _ok(returncode=1)
        if isinstance(cmd, str):
            return _ok(stdout="tests passed")
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        result = fix_pr(42, cfg)

    assert result is False
    assert push_attempted[0] is True
    out = capsys.readouterr().out
    assert "Push failed" in out
