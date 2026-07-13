"""Tests for autoloop fix-pr command."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from autoloop.fix_pr import (
    PrState,
    _get_unmerged_files,
    _parse_conflicting_files,
    abort_rebase,
    checkout_branch,
    commit_fixes,
    continue_rebase,
    fix_pr,
    force_push,
    get_pr_info,
    has_staged_changes,
    is_behind_main,
    lint_check,
    rebase_on_main,
    restore_main,
    run_lint_fix,
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
        "lint_command": "echo lint-ok",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _ok(stdout="", returncode=0):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


# --- get_pr_info ---


def test_get_pr_info_returns_state():
    data = {
        "headRefName": "autoloop/42-fix",
        "state": "OPEN",
        "mergeable": "CONFLICTING",
        "statusCheckRollup": [
            {"name": "test", "conclusion": "FAILURE", "status": "COMPLETED"},
            {"name": "lint", "conclusion": "SUCCESS", "status": "COMPLETED"},
        ],
    }

    def fake_run(cmd, **kwargs):
        return _ok(stdout=json.dumps(data))

    import json

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        pr = get_pr_info(42, "acme/repo")

    assert pr.branch == "autoloop/42-fix"
    assert pr.state == "OPEN"
    assert pr.has_conflicts is True
    assert pr.failing_checks == ["test"]


def test_get_pr_info_no_failures():
    import json

    data = {
        "headRefName": "branch",
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "statusCheckRollup": [
            {"name": "test", "conclusion": "SUCCESS", "status": "COMPLETED"},
        ],
    }

    def fake_run(cmd, **kwargs):
        return _ok(stdout=json.dumps(data))

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        pr = get_pr_info(1, "acme/repo")

    assert pr.has_conflicts is False
    assert pr.failing_checks == []


def test_get_pr_info_returns_none_on_failure():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1)

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert get_pr_info(42, "acme/repo") is None


def test_get_pr_info_uses_repo():
    import json

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _ok(
            stdout=json.dumps(
                {
                    "headRefName": "b",
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "statusCheckRollup": [],
                }
            )
        )

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        get_pr_info(42, "my-org/my-repo")

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


# --- is_behind_main ---


def test_is_behind_main_true():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="3\n")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert is_behind_main("my-branch") is True


def test_is_behind_main_false():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="0\n")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert is_behind_main("my-branch") is False


def test_is_behind_main_handles_failure():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1)

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert is_behind_main("my-branch") is False


# --- _parse_conflicting_files ---


def test_parse_conflicting_files_merge_conflict():
    output = "CONFLICT (content): Merge conflict in src/main.py\nauto-merging src/ok.py\n"
    assert _parse_conflicting_files(output) == ["src/main.py"]


def test_parse_conflicting_files_multiple():
    output = (
        "CONFLICT (content): Merge conflict in src/a.py\n"
        "CONFLICT (content): Merge conflict in src/b.py\n"
    )
    assert _parse_conflicting_files(output) == ["src/a.py", "src/b.py"]


def test_parse_conflicting_files_no_conflicts():
    assert _parse_conflicting_files("Successfully rebased") == []


# --- _get_unmerged_files ---


def test_get_unmerged_files_finds_uu():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="UU src/conflict.py\nM  src/clean.py\n")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert _get_unmerged_files() == ["src/conflict.py"]


def test_get_unmerged_files_finds_aa():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="AA src/both_added.py\n")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert _get_unmerged_files() == ["src/both_added.py"]


def test_get_unmerged_files_empty():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="M  src/clean.py\n")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert _get_unmerged_files() == []


# --- rebase_on_main ---


def test_rebase_clean():
    def fake_run(cmd, **kwargs):
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        clean, files = rebase_on_main()

    assert clean is True
    assert files == []


def test_rebase_with_conflicts_from_output():
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rebase"]:
            return _ok(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in src/main.py\n",
            )
        return _ok(stdout="")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        clean, files = rebase_on_main()

    assert clean is False
    assert files == ["src/main.py"]


def test_rebase_falls_back_to_unmerged_files():
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rebase"]:
            return _ok(returncode=1, stdout="error: could not apply\n")
        if cmd[:2] == ["git", "status"]:
            return _ok(stdout="UU src/conflict.py\n")
        return _ok(stdout="")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        clean, files = rebase_on_main()

    assert clean is False
    assert files == ["src/conflict.py"]


# --- continue_rebase ---


def test_continue_rebase_succeeds():
    def fake_run(cmd, **kwargs):
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert continue_rebase() is True


def test_continue_rebase_fails_with_unresolved():
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "rebase", "--continue"]:
            return _ok(returncode=1)
        if cmd[:2] == ["git", "status"]:
            return _ok(stdout="UU src/conflict.py\n")
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert continue_rebase() is False


def test_continue_rebase_respects_max_rounds(tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "rebase", "--continue"]:
            return _ok(returncode=1)
        if cmd[:2] == ["git", "status"]:
            return _ok(stdout="")
        return _ok()

    with (
        patch("autoloop.fix_pr.subprocess.run", fake_run),
        patch("autoloop.fix_pr.REPO_DIR", tmp_path),
    ):
        (tmp_path / ".git" / "rebase-merge").mkdir(parents=True)
        assert continue_rebase(max_rounds=3) is False


# --- run_lint_fix ---


def test_run_lint_fix_succeeds():
    def fake_run(cmd, **kwargs):
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        fixed, output = run_lint_fix(_cfg())

    assert fixed is True


def test_run_lint_fix_fails():
    calls = [0]

    def fake_run(cmd, **kwargs):
        calls[0] += 1
        if calls[0] == 3:
            return _ok(returncode=1, stdout="lint error")
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        fixed, output = run_lint_fix(_cfg())

    assert fixed is False


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


# --- lint_check ---


def test_lint_check_passes():
    def fake_run(cmd, **kwargs):
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        passed, _ = lint_check(_cfg())

    assert passed is True


def test_lint_check_fails():
    def fake_run(cmd, **kwargs):
        return _ok(returncode=1, stdout="ruff error")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        passed, output = lint_check(_cfg())

    assert passed is False
    assert "ruff error" in output


# --- has_staged_changes ---


def test_has_staged_changes_true():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="M  src/file.py\n")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert has_staged_changes() is True


def test_has_staged_changes_false():
    def fake_run(cmd, **kwargs):
        return _ok(stdout="")

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert has_staged_changes() is False


# --- commit_fixes ---


def test_commit_fixes_success():
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _ok()

    with patch("autoloop.fix_pr.subprocess.run", fake_run):
        assert commit_fixes() is True

    assert calls[0][:2] == ["git", "add"]
    assert calls[1][:2] == ["git", "commit"]


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


def _fake_pr_info(
    branch="autoloop/42-fix",
    state="OPEN",
    mergeable="MERGEABLE",
    has_conflicts=False,
    failing_checks=None,
):
    return PrState(
        number=42,
        branch=branch,
        state=state,
        mergeable=mergeable,
        has_conflicts=has_conflicts,
        failing_checks=failing_checks or [],
    )


def test_fix_pr_clean_rebase_no_check_failures(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, str):
            return _ok(stdout="ok")
        return _ok()

    with (
        patch("autoloop.fix_pr.get_pr_info", return_value=_fake_pr_info()),
        patch("autoloop.fix_pr.is_behind_main", return_value=True),
        patch("autoloop.fix_pr.subprocess.run", fake_run),
    ):
        result = fix_pr(42, cfg)

    assert result is True
    out = capsys.readouterr().out
    assert "Rebased cleanly" in out
    assert "fixed and pushed" in out


def test_fix_pr_nothing_to_fix(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        return _ok()

    with (
        patch("autoloop.fix_pr.get_pr_info", return_value=_fake_pr_info()),
        patch("autoloop.fix_pr.is_behind_main", return_value=False),
        patch("autoloop.fix_pr.subprocess.run", fake_run),
    ):
        result = fix_pr(42, cfg)

    assert result is True
    out = capsys.readouterr().out
    assert "Nothing to fix" in out


def test_fix_pr_pr_not_found(capsys):
    cfg = _cfg()

    with patch("autoloop.fix_pr.get_pr_info", return_value=None):
        result = fix_pr(42, cfg)

    assert result is False
    out = capsys.readouterr().out
    assert "Could not find" in out


def test_fix_pr_pr_already_merged(capsys):
    cfg = _cfg()

    with patch("autoloop.fix_pr.get_pr_info", return_value=_fake_pr_info(state="MERGED")):
        result = fix_pr(42, cfg)

    assert result is False
    out = capsys.readouterr().out
    assert "merged" in out


def test_fix_pr_conflicts_resolved(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"]:
            if "--abort" in cmd or "--continue" in cmd:
                return _ok()
            return _ok(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in src/a.py\n",
            )
        if isinstance(cmd, list) and cmd[:2] == ["git", "status"]:
            return _ok(stdout="")
        if isinstance(cmd, str):
            return _ok(stdout="ok")
        return _ok()

    with (
        patch("autoloop.fix_pr.get_pr_info", return_value=_fake_pr_info(has_conflicts=True)),
        patch("autoloop.fix_pr.is_behind_main", return_value=True),
        patch("autoloop.fix_pr.resolve_conflicts_with_claude", return_value=True),
        patch("autoloop.fix_pr.subprocess.run", fake_run),
    ):
        result = fix_pr(42, cfg)

    assert result is True
    out = capsys.readouterr().out
    assert "Conflicts resolved" in out


def test_fix_pr_conflict_resolution_fails(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "rebase"]:
            if "--abort" in cmd:
                return _ok()
            return _ok(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in src/a.py\n",
            )
        if isinstance(cmd, list) and cmd[:2] == ["git", "status"]:
            return _ok(stdout="UU src/a.py\n")
        return _ok()

    with (
        patch("autoloop.fix_pr.get_pr_info", return_value=_fake_pr_info(has_conflicts=True)),
        patch("autoloop.fix_pr.is_behind_main", return_value=True),
        patch("autoloop.fix_pr.resolve_conflicts_with_claude", return_value=False),
        patch("autoloop.fix_pr.subprocess.run", fake_run),
    ):
        result = fix_pr(42, cfg)

    assert result is False
    out = capsys.readouterr().out
    assert "could not resolve" in out


def test_fix_pr_lint_failure_auto_fixed(capsys):
    cfg = _cfg()
    lint_calls = [0]

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, str) and cfg.lint_command in cmd:
            lint_calls[0] += 1
            if lint_calls[0] == 1:
                return _ok(returncode=1, stdout="ruff error")
            return _ok()
        if isinstance(cmd, str):
            return _ok(stdout="ok")
        return _ok()

    with (
        patch(
            "autoloop.fix_pr.get_pr_info",
            return_value=_fake_pr_info(failing_checks=["lint"]),
        ),
        patch("autoloop.fix_pr.is_behind_main", return_value=False),
        patch("autoloop.fix_pr.subprocess.run", fake_run),
    ):
        result = fix_pr(42, cfg)

    assert result is True
    out = capsys.readouterr().out
    assert "Lint auto-fixed" in out


def test_fix_pr_test_failure_claude_fixes(capsys):
    cfg = _cfg()
    verify_calls = [0]

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, str) and cfg.verify_cmd in cmd:
            verify_calls[0] += 1
            if verify_calls[0] <= 1:
                return _ok(returncode=1, stdout="FAILED test_x")
            return _ok(stdout="ok")
        if isinstance(cmd, str):
            return _ok()
        return _ok()

    with (
        patch(
            "autoloop.fix_pr.get_pr_info",
            return_value=_fake_pr_info(failing_checks=["test"]),
        ),
        patch("autoloop.fix_pr.is_behind_main", return_value=False),
        patch("autoloop.fix_pr.fix_checks_with_claude", return_value=True),
        patch("autoloop.fix_pr.subprocess.run", fake_run),
    ):
        result = fix_pr(42, cfg)

    assert result is True
    out = capsys.readouterr().out
    assert "Asking Claude to fix" in out


def test_fix_pr_push_failure(capsys):
    cfg = _cfg()

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and "push" in cmd:
            return _ok(returncode=1)
        if isinstance(cmd, str):
            return _ok(stdout="ok")
        return _ok()

    with (
        patch("autoloop.fix_pr.get_pr_info", return_value=_fake_pr_info()),
        patch("autoloop.fix_pr.is_behind_main", return_value=True),
        patch("autoloop.fix_pr.subprocess.run", fake_run),
    ):
        result = fix_pr(42, cfg)

    assert result is False
    out = capsys.readouterr().out
    assert "Push failed" in out
