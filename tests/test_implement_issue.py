from __future__ import annotations

import json
import os

import autoloop.claude_runner as claude_runner
import autoloop.implement_issue as implement_issue
from autoloop.claude_runner import ClaudeResult
from autoloop.config import AutoLoopConfig
from autoloop.implement_issue import (
    EMPTY_BRANCH_DIAGNOSTIC,
    acquire_lock,
    build_branch_name,
    build_pr_body,
    cleanup_merged_labels,
    collect_verification_errors,
    create_branch,
    create_pr,
    design_gate,
    design_issue,
    design_required,
    detect_issue_type,
    ensure_clean_main,
    get_issue_by_number,
    has_design_comment,
    has_needs_design_label,
    implement,
    implement_single_issue,
    implement_targeted_issue,
    is_branch_empty,
    log_run,
    parent_issue_number,
    parse_and_strip_metric_targets,
    parse_dependency_numbers,
    parse_review_response,
    post_in_progress_comment,
    priority_rank,
    release_lock,
    review_implementation,
    select_top_issue,
    unblock_ready_issues,
)


def _test_cfg(**overrides):
    """Build an AutoLoopConfig with test defaults."""
    defaults = {
        "repo": "acme-corp/widget",
        "impl_model": "opus",
        "impl_timeout": 600,
        "test_timeout": 60,
        "pr_reviewer": "review-bot",
        "max_retries": 3,
        "verify_cmd": "echo ok",
    }
    defaults.update(overrides)
    return AutoLoopConfig(**defaults)


def _claude_result(
    cost_usd=1.5, input_tokens=1000, output_tokens=200, cache_read_tokens=500, success=True
):
    return ClaudeResult(
        text="",
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        success=success,
    )


# --- Pure function tests: parse_dependency_numbers ---


def test_parse_deps_single():
    assert parse_dependency_numbers("Depends on: #43") == ["43"]


def test_parse_deps_multiple():
    body = "Depends on: #43\nDepends on #44\nDepends on: #45"
    assert parse_dependency_numbers(body) == ["43", "44", "45"]


def test_parse_deps_none():
    assert parse_dependency_numbers("No dependencies here") == []


# --- Pure function tests: build_branch_name ---


def test_build_branch_name_basic():
    issue = {"number": 42, "title": "Add verbose flag"}
    assert build_branch_name(issue) == "autoloop/42-add-verbose-flag"


def test_build_branch_name_truncates_long_title():
    issue = {"number": 1, "title": "A" * 100}
    name = build_branch_name(issue)
    slug = name.split("/", 1)[1].split("-", 1)[1]
    assert len(slug) <= 50


# --- Pure function tests: parse_and_strip_metric_targets ---


def test_parse_metric_targets_no_targets():
    body = "## Summary\nAdd a feature\n\n## Acceptance Criteria\n- works"
    cleaned, targets = parse_and_strip_metric_targets(body)
    assert cleaned == body
    assert targets == []


def test_parse_metric_targets_single_target():
    body = "## Summary\nDo something\n**Metric Target:** latency < 100ms\n## End"
    cleaned, targets = parse_and_strip_metric_targets(body)
    assert "Metric Target" not in cleaned
    assert targets == ["**Metric Target:** latency < 100ms"]


# --- Pure function tests: detect_issue_type ---


def test_detect_issue_type_bug():
    assert detect_issue_type("## Summary\nFix\n\n## Type\nbug") == "fix"


def test_detect_issue_type_feature():
    assert detect_issue_type("## Summary\nAdd\n\n## Type\nfeature") == "feat"


def test_detect_issue_type_default_feat():
    assert detect_issue_type("no type section here") == "feat"


# --- Pure function tests: collect_verification_errors ---


def test_verification_no_errors():
    errors = collect_verification_errors(
        ahead_count="3",
        test_rc=0,
        test_out="",
        lint_rc=0,
        fmt_rc=0,
        changed_files=["tests/test_new.py", "src/x.py"],
    )
    assert errors == []


def test_verification_no_commits():
    errors = collect_verification_errors(
        ahead_count="0",
        test_rc=0,
        test_out="",
        lint_rc=0,
        fmt_rc=0,
        changed_files=["tests/test_new.py"],
    )
    assert "No commits on branch" in errors


def test_verification_tests_failed():
    errors = collect_verification_errors(
        ahead_count="1",
        test_rc=1,
        test_out="FAILED test_x",
        lint_rc=0,
        fmt_rc=0,
        changed_files=["tests/test_x.py"],
    )
    assert any("Tests failed" in e for e in errors)


def test_verification_lint_failed():
    errors = collect_verification_errors(
        ahead_count="1",
        test_rc=0,
        test_out="",
        lint_rc=1,
        fmt_rc=0,
        changed_files=["tests/test_x.py"],
    )
    assert "Lint or format check failed" in errors


def test_verification_no_test_files():
    errors = collect_verification_errors(
        ahead_count="1",
        test_rc=0,
        test_out="",
        lint_rc=0,
        fmt_rc=0,
        changed_files=["src/patina/store.py"],
    )
    assert "No test files were added or modified" in errors


def test_verification_multiple_errors():
    errors = collect_verification_errors(
        ahead_count="0",
        test_rc=1,
        test_out="fail",
        lint_rc=1,
        fmt_rc=1,
        changed_files=[],
    )
    assert len(errors) == 4


# --- Config-driven tests: build_pr_body uses cfg ---


def test_build_pr_body_uses_cfg_verify_cmd(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(verify_cmd="make test"))

    issue = {"number": 42, "title": "Add flag"}
    body = build_pr_body(issue)

    assert "`make test`" in body
    assert "uv run pytest" not in body


def test_build_pr_body_uses_cfg_max_retries(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(max_retries=5))

    issue = {"number": 42, "title": "Add flag"}
    body = build_pr_body(issue, attempts=2, duration=120.5, cost_usd=3.42)

    assert "Attempts: 2/5" in body


def test_build_pr_body_includes_autoloop_stats(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())

    issue = {"number": 42, "title": "Add flag"}
    body = build_pr_body(
        issue,
        attempts=2,
        duration=120.5,
        cost_usd=3.42,
        input_tokens=45000,
        output_tokens=2300,
    )
    assert "## AutoLoop Run Stats" in body
    assert "Automated implementation by AutoLoop" in body


def test_build_pr_body_no_stats_when_zero_calls(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())

    issue = {"number": 42, "title": "Add flag"}
    body = build_pr_body(issue)
    assert "AutoLoop Run Stats" not in body


# --- Config-driven tests: subprocess calls use cfg.repo ---


def test_get_top_ready_issue_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "[]"})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    implement_issue.get_top_ready_issue()

    assert "--repo" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


def test_get_issue_by_number_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": '{"number": 28, "title": "T", "body": "", "labels": []}',
                "stderr": "",
            },
        )()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    get_issue_by_number(28)

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


def test_dependencies_met_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": '{"state": "CLOSED"}'})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    implement_issue.dependencies_met({"body": "Depends on: #10"})

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


def test_has_design_comment_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": '{"comments": []}', "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    has_design_comment(44)

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


def test_post_design_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    implement_issue.post_design(44, "design text")

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


def test_post_attempt_failure_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    implement_issue.post_attempt_failure(42, 2, "Tests failed")

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


def test_cleanup_merged_labels_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        if cmd[:3] == ["gh", "issue", "list"]:
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    cleanup_merged_labels()

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


def test_unblock_ready_issues_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        if cmd[:3] == ["gh", "issue", "list"]:
            return type("R", (), {"returncode": 0, "stdout": "[]", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    unblock_ready_issues()

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


# --- Config-driven tests: create_pr uses cfg.pr_reviewer ---


def test_create_pr_uses_cfg_reviewer(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(pr_reviewer="review-bot"))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    create_pr({"number": 42, "title": "Add feature", "body": "## Type\nfeature"}, "autoloop/42-x")

    pr_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert len(pr_calls) == 1
    cmd = pr_calls[0]
    assert cmd[cmd.index("--assignee") + 1] == "review-bot"


def test_create_pr_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    create_pr({"number": 42, "title": "Add feature", "body": "## Type\nfeature"}, "autoloop/42-x")

    pr_calls = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    cmd = pr_calls[0]
    assert cmd[cmd.index("--repo") + 1] == "my-org/my-repo"


# --- Config-driven tests: implement uses cfg.impl_model / cfg.impl_timeout ---


def test_implement_uses_cfg_model_and_timeout(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(impl_model="haiku", impl_timeout=1800))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["timeout"] = kwargs.get("timeout")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue, "build_implementation_prompt", lambda issue: "prompt")
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    implement({"number": 1, "title": "Test", "body": ""})

    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "haiku"
    assert captured["timeout"] == 1800


# --- Config-driven tests: design_issue uses cfg.impl_model ---


def test_design_issue_uses_cfg_model(monkeypatch, tmp_path):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(impl_model="sonnet"))
    (tmp_path / "CLAUDE.md").write_text("# Project\nTest project")
    monkeypatch.setattr(implement_issue, "REPO_DIR", tmp_path)
    captured = {}

    def _json_result(text):
        return type(
            "R",
            (),
            {"returncode": 0, "stdout": json.dumps({"result": text}), "stderr": ""},
        )()

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _json_result("design proposal")

    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)
    design_issue({"number": 1, "title": "Test", "body": ""})

    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "sonnet"


# --- Config-driven tests: verify_implementation uses cfg.verify_cmd with shell=True ---


def test_verify_implementation_uses_cfg_verify_cmd_with_shell(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(verify_cmd="make test", test_timeout=30))
    captured_calls = []

    def fake_run(cmd_or_str, **kwargs):
        captured_calls.append({"cmd": cmd_or_str, "kwargs": kwargs})
        if isinstance(cmd_or_str, str) and "make test" in cmd_or_str:
            return type("R", (), {"returncode": 0, "stdout": "passed", "stderr": ""})()
        if isinstance(cmd_or_str, list) and cmd_or_str[:3] == ["git", "rev-list", "--count"]:
            return type("R", (), {"returncode": 0, "stdout": "1\n", "stderr": ""})()
        if isinstance(cmd_or_str, list) and "ruff" in cmd_or_str:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if isinstance(cmd_or_str, list) and cmd_or_str[:3] == ["git", "diff", "--name-only"]:
            return type("R", (), {"returncode": 0, "stdout": "tests/test_x.py\n", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    valid, _ = implement_issue.verify_implementation("branch")

    test_calls = [c for c in captured_calls if c["cmd"] == "make test"]
    assert len(test_calls) == 1
    assert test_calls[0]["kwargs"]["shell"] is True
    assert test_calls[0]["kwargs"]["cwd"] == implement_issue.REPO_DIR
    assert test_calls[0]["kwargs"]["timeout"] == 30
    assert valid is True


# --- Config-driven tests: build_implementation_prompt uses cfg.verify_cmd ---


def test_build_implementation_prompt_references_cfg_verify_cmd(monkeypatch, tmp_path):
    monkeypatch.setattr(
        implement_issue, "cfg", _test_cfg(verify_cmd="npm test", repo="my-org/my-repo")
    )
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Project\nTest project")
    monkeypatch.setattr(implement_issue, "REPO_DIR", tmp_path)

    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)

    issue = {"number": 1, "title": "Test", "body": "details"}
    prompt = implement_issue.build_implementation_prompt(issue)

    assert "`npm test`" in prompt
    assert "uv run pytest" not in prompt


# --- Config-driven tests: review_implementation uses cfg.impl_model ---


def test_review_implementation_uses_cfg_model(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(impl_model="haiku"))
    captured = {}

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[0] == "git":
            return type("R", (), {"returncode": 0, "stdout": "diff content", "stderr": ""})()
        captured["cmd"] = cmd
        return type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {"result": '{"approved": true, "issues": [], "summary": "ok"}'}
                ),
                "stderr": "",
            },
        )()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    review_implementation({"number": 1, "title": "T", "body": ""}, "branch")

    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "haiku"


# --- Lockfile tests ---


def test_acquire_lock_succeeds_when_no_lockfile(tmp_path, monkeypatch):
    lock_path = tmp_path / ".autoloop.lock"
    monkeypatch.setattr(implement_issue, "LOCKFILE", lock_path)
    assert acquire_lock() is True
    assert lock_path.exists()
    assert lock_path.read_text().strip() == str(os.getpid())


def test_acquire_lock_fails_when_pid_alive(tmp_path, monkeypatch):
    lock_path = tmp_path / ".autoloop.lock"
    lock_path.write_text(str(os.getpid()))
    monkeypatch.setattr(implement_issue, "LOCKFILE", lock_path)
    assert acquire_lock() is False


def test_acquire_lock_succeeds_when_pid_stale(tmp_path, monkeypatch):
    lock_path = tmp_path / ".autoloop.lock"
    lock_path.write_text("999999999")
    monkeypatch.setattr(implement_issue, "LOCKFILE", lock_path)
    assert acquire_lock() is True


def test_release_lock_removes_file(tmp_path, monkeypatch):
    lock_path = tmp_path / ".autoloop.lock"
    lock_path.write_text(str(os.getpid()))
    monkeypatch.setattr(implement_issue, "LOCKFILE", lock_path)
    release_lock()
    assert not lock_path.exists()


# --- log_run tests ---


def test_log_run_writes_json_entry(tmp_path, monkeypatch):
    log_path = tmp_path / "run_history.jsonl"
    monkeypatch.setattr(implement_issue, "LOG_FILE", log_path)
    log_run(17, True, 2, 120.0, 5.00, 45000, 2300, 16832)

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["issue"] == 17
    assert entry["success"] is True


# --- create_branch tests ---


def test_create_branch_calls_in_order(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    calls = []
    issue = {"number": 18, "title": "Pull latest main"}

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    branch = create_branch(issue)

    assert calls[0] == ["git", "checkout", "main"]
    assert calls[1] == ["git", "pull", "origin", "main"]
    assert calls[2][0:3] == ["git", "checkout", "-b"]
    assert branch in calls[2]


# --- ensure_clean_main tests ---


def test_ensure_clean_main_calls_three_git_commands(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    ensure_clean_main()

    assert len(calls) == 3
    assert calls[0] == ["git", "checkout", "--", "."]
    assert calls[1] == ["git", "checkout", "main"]
    assert calls[2] == ["git", "pull", "--ff-only", "origin", "main"]


# --- implement_single_issue tests ---

_FAKE_ISSUE = {"number": 42, "title": "Add feature", "body": "## Type\nfeature", "labels": []}


def test_implement_single_issue_returns_true_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    log_path = tmp_path / "run_history.jsonl"
    monkeypatch.setattr(implement_issue, "LOG_FILE", log_path)

    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and cmd[:3] == ["git", "rev-list", "--count"]:
            return type("R", (), {"returncode": 0, "stdout": "1\n", "stderr": ""})()
        if isinstance(cmd, str):
            return type("R", (), {"returncode": 0, "stdout": "passed", "stderr": ""})()
        if isinstance(cmd, list) and "ruff" in cmd:
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        if isinstance(cmd, list) and cmd[:3] == ["git", "diff", "--name-only"]:
            return type("R", (), {"returncode": 0, "stdout": "tests/test_x.py\n", "stderr": ""})()
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    monkeypatch.setattr(
        implement_issue, "implement", lambda issue, previous_errors=None: _claude_result()
    )
    monkeypatch.setattr(implement_issue, "create_branch", lambda issue: "autoloop/42-add-feature")
    monkeypatch.setattr(implement_issue, "create_pr", lambda *a, **kw: None)
    monkeypatch.setattr(implement_issue, "label_in_review", lambda n: None)
    monkeypatch.setattr(implement_issue, "review_implementation", lambda issue, branch: (True, ""))

    result = implement_single_issue(_FAKE_ISSUE)
    assert result is True
    assert log_path.exists()
    entry = json.loads(log_path.read_text().strip())
    assert entry["success"] is True


def test_implement_single_issue_uses_cfg_max_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(max_retries=2))
    log_path = tmp_path / "run_history.jsonl"
    monkeypatch.setattr(implement_issue, "LOG_FILE", log_path)

    attempt_count = [0]

    def fake_implement(issue, previous_errors=None):
        attempt_count[0] += 1
        return _claude_result()

    monkeypatch.setattr(
        implement_issue.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(implement_issue, "implement", fake_implement)
    monkeypatch.setattr(implement_issue, "create_branch", lambda issue: "autoloop/42-x")
    monkeypatch.setattr(implement_issue, "cleanup_branch", lambda branch: None)
    monkeypatch.setattr(implement_issue, "is_branch_empty", lambda branch: False)
    monkeypatch.setattr(
        implement_issue, "verify_implementation", lambda branch: (False, "Tests failed")
    )
    monkeypatch.setattr(implement_issue, "post_attempt_failure", lambda n, a, e: None)

    implement_single_issue(_FAKE_ISSUE)
    assert attempt_count[0] == 2


def test_implement_single_issue_returns_false_after_all_retries(monkeypatch, tmp_path):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    log_path = tmp_path / "run_history.jsonl"
    monkeypatch.setattr(implement_issue, "LOG_FILE", log_path)

    attempt_count = [0]

    def fake_implement(issue, previous_errors=None):
        attempt_count[0] += 1
        return _claude_result()

    monkeypatch.setattr(
        implement_issue.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )
    monkeypatch.setattr(implement_issue, "implement", fake_implement)
    monkeypatch.setattr(implement_issue, "create_branch", lambda issue: "autoloop/42-x")
    monkeypatch.setattr(implement_issue, "cleanup_branch", lambda branch: None)
    monkeypatch.setattr(implement_issue, "is_branch_empty", lambda branch: False)
    monkeypatch.setattr(
        implement_issue, "verify_implementation", lambda branch: (False, "Tests failed")
    )
    monkeypatch.setattr(implement_issue, "post_attempt_failure", lambda n, a, e: None)

    result = implement_single_issue(_FAKE_ISSUE)
    assert result is False
    assert attempt_count[0] == 3


def test_implement_single_issue_catches_exception_returns_false(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    monkeypatch.setattr(implement_issue, "ensure_clean_main", lambda: None)
    monkeypatch.setattr(implement_issue, "design_gate", lambda i, require_design=False: True)

    def exploding_run(cmd, **kwargs):
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(implement_issue.subprocess, "run", exploding_run)

    result = implement_single_issue(
        {"number": 99, "title": "Exploding issue", "body": "", "labels": []}
    )
    assert result is False


def test_implement_single_issue_logs_summed_token_totals(monkeypatch, tmp_path):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    log_path = tmp_path / "run_history.jsonl"
    monkeypatch.setattr(implement_issue, "LOG_FILE", log_path)

    results = iter(
        [
            _claude_result(cost_usd=1.0, input_tokens=100, output_tokens=10, cache_read_tokens=5),
            _claude_result(cost_usd=2.5, input_tokens=250, output_tokens=30, cache_read_tokens=15),
        ]
    )
    monkeypatch.setattr(
        implement_issue, "implement", lambda issue, previous_errors=None: next(results)
    )
    monkeypatch.setattr(implement_issue, "create_branch", lambda issue: "autoloop/42-test")
    monkeypatch.setattr(implement_issue, "cleanup_branch", lambda branch: None)
    monkeypatch.setattr(implement_issue, "post_attempt_failure", lambda n, a, e: None)
    monkeypatch.setattr(implement_issue, "create_pr", lambda *a, **kw: None)
    monkeypatch.setattr(implement_issue, "label_in_review", lambda n: None)
    monkeypatch.setattr(implement_issue, "review_implementation", lambda issue, branch: (True, ""))
    monkeypatch.setattr(implement_issue, "is_branch_empty", lambda branch: False)

    verify_calls = [0]

    def fake_verify(branch):
        verify_calls[0] += 1
        if verify_calls[0] < 2:
            return False, "attempt 1 failed"
        return True, ""

    monkeypatch.setattr(implement_issue, "verify_implementation", fake_verify)
    monkeypatch.setattr(
        implement_issue.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    assert implement_single_issue(_FAKE_ISSUE) is True
    entry = json.loads(log_path.read_text().strip())
    assert entry["cost_usd"] == 3.5
    assert entry["input_tokens"] == 350
    assert entry["output_tokens"] == 40
    assert entry["cache_read_tokens"] == 20


# --- implement with previous_errors ---


def test_implement_appends_previous_errors_to_prompt(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue, "build_implementation_prompt", lambda issue: "BASE_PROMPT")
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    implement({"number": 1, "title": "Test", "body": ""}, previous_errors="test failure output")

    prompt_arg = captured["cmd"][-1]
    assert "Previous Attempt Failed" in prompt_arg
    assert "test failure output" in prompt_arg


def test_implement_no_previous_errors_omits_section(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue, "build_implementation_prompt", lambda issue: "BASE_PROMPT")
    monkeypatch.setattr(claude_runner.subprocess, "run", fake_run)

    implement({"number": 1, "title": "Test", "body": ""})

    prompt_arg = captured["cmd"][-1]
    assert "Previous Attempt Failed" not in prompt_arg


# --- parent_issue_number tests ---


def test_parent_issue_number_found():
    issue = {"body": "Some text\nParent issue: #28\nmore"}
    assert parent_issue_number(issue) == 28


def test_parent_issue_number_none_when_standalone():
    issue = {"body": "A standalone issue with no parent reference"}
    assert parent_issue_number(issue) is None


# --- priority_rank tests ---


def test_priority_rank_orders_labels():
    p0 = {"labels": [{"name": "p0"}]}
    p1 = {"labels": [{"name": "p1"}]}
    p2 = {"labels": [{"name": "p2"}]}
    assert priority_rank(p0) < priority_rank(p1) < priority_rank(p2)


# --- select_top_issue tests ---


def _sub(number, parent, priority=None):
    labels = [{"name": priority}] if priority else []
    return {
        "number": number,
        "title": f"Step for #{parent}",
        "body": f"Parent issue: #{parent}",
        "labels": labels,
    }


def _standalone(number, priority=None):
    labels = [{"name": priority}] if priority else []
    return {
        "number": number,
        "title": f"Standalone #{number}",
        "body": "No parent here",
        "labels": labels,
    }


def test_select_top_issue_empty_returns_none():
    assert select_top_issue([]) is None


def test_select_top_issue_prefers_group_with_most_sub_issues(monkeypatch):
    monkeypatch.setattr(implement_issue, "dependencies_met", lambda i: True)
    issues = [
        _sub(101, parent=30),
        _sub(102, parent=28),
        _sub(103, parent=28),
    ]
    chosen = select_top_issue(issues)
    assert parent_issue_number(chosen) == 28


def test_select_top_issue_returns_lowest_step_within_group(monkeypatch):
    monkeypatch.setattr(implement_issue, "dependencies_met", lambda i: True)
    issues = [
        _sub(103, parent=28),
        _sub(101, parent=28),
        _sub(102, parent=28),
    ]
    chosen = select_top_issue(issues)
    assert chosen["number"] == 101


def test_select_top_issue_filters_unmet_dependencies(monkeypatch):
    monkeypatch.setattr(implement_issue, "dependencies_met", lambda i: i["number"] != 10)
    issues = [
        _standalone(10, priority="p0"),
        _standalone(11, priority="p1"),
    ]
    chosen = select_top_issue(issues)
    assert chosen["number"] == 11


# --- design_required tests ---


def test_design_required_true_with_flag():
    issue = {"number": 1, "labels": []}
    assert design_required(issue, require_design=True) is True


def test_design_required_true_with_label():
    issue = {"number": 1, "labels": [{"name": "design-required"}]}
    assert design_required(issue) is True


def test_design_required_false_without_flag_or_label():
    issue = {"number": 1, "labels": [{"name": "ready"}]}
    assert design_required(issue) is False


# --- has_needs_design_label tests ---


def test_has_needs_design_label_true():
    issue = {"labels": [{"name": "needs-design"}, {"name": "ready"}]}
    assert has_needs_design_label(issue) is True


def test_has_needs_design_label_false():
    issue = {"labels": [{"name": "ready"}]}
    assert has_needs_design_label(issue) is False


# --- design_gate tests ---


def test_design_gate_skips_when_not_required():
    issue = {"number": 44, "title": "T", "body": "b", "labels": []}
    assert design_gate(issue, require_design=False) is True


def test_design_gate_generates_design_when_missing(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    issue = {"number": 44, "title": "T", "body": "b", "labels": []}
    events = {}

    monkeypatch.setattr(implement_issue, "has_design_comment", lambda n: False)
    monkeypatch.setattr(implement_issue, "design_issue", lambda i: "generated design")
    monkeypatch.setattr(implement_issue, "post_design", lambda n, d: events.update(posted=(n, d)))
    monkeypatch.setattr(
        implement_issue, "add_needs_design_label", lambda n: events.update(labeled=n)
    )

    assert design_gate(issue, require_design=True) is False
    assert events["posted"] == (44, "generated design")


def test_design_gate_proceeds_when_design_approved(monkeypatch):
    issue = {"number": 44, "title": "T", "body": "b", "labels": [{"name": "design-required"}]}
    monkeypatch.setattr(implement_issue, "has_design_comment", lambda n: True)
    assert design_gate(issue, require_design=True) is True


# --- parse_review_response tests ---


def test_parse_review_response_approved():
    approved, feedback = parse_review_response(
        '{"approved": true, "issues": [], "summary": "looks good"}'
    )
    assert approved is True
    assert feedback == "looks good"


def test_parse_review_response_rejected_lists_issues():
    approved, feedback = parse_review_response(
        '{"approved": false, "issues": ["missing test", "wrong pattern"], "summary": "no"}'
    )
    assert approved is False
    assert "missing test" in feedback


def test_parse_review_response_non_json_is_failure():
    approved, feedback = parse_review_response("this is not json at all")
    assert approved is False
    assert "not valid JSON" in feedback


def test_parse_review_response_strips_code_fence():
    text = '```json\n{"approved": true, "issues": [], "summary": "ok"}\n```'
    approved, feedback = parse_review_response(text)
    assert approved is True


# --- implement_targeted_issue tests ---


def test_implement_targeted_issue_bypasses_ready_and_points(monkeypatch, capsys):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    targeted = {"number": 28, "title": "Five point issue", "body": "", "labels": []}
    monkeypatch.setattr(implement_issue, "get_issue_by_number", lambda n: targeted)
    monkeypatch.setattr(implement_issue, "dependencies_met", lambda issue: True)

    implemented = []
    monkeypatch.setattr(
        implement_issue,
        "implement_single_issue",
        lambda issue, require_design=False: implemented.append(issue["number"]) or True,
    )

    assert implement_targeted_issue(28) is True
    assert implemented == [28]


def test_implement_targeted_issue_aborts_on_unmet_deps(monkeypatch, capsys):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    targeted = {"number": 28, "title": "Blocked", "body": "Depends on: #5", "labels": []}
    monkeypatch.setattr(implement_issue, "get_issue_by_number", lambda n: targeted)
    monkeypatch.setattr(implement_issue, "dependencies_met", lambda issue: False)

    assert implement_targeted_issue(28) is False
    assert "dependencies not met" in capsys.readouterr().out


def test_implement_targeted_issue_aborts_when_fetch_fails(monkeypatch, capsys):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg())
    monkeypatch.setattr(implement_issue, "get_issue_by_number", lambda n: None)

    assert implement_targeted_issue(28) is False
    assert "could not fetch issue" in capsys.readouterr().out


# --- main() tests ---


def test_main_default_implements_one_issue(monkeypatch, tmp_path, capsys):
    lock_path = tmp_path / ".autoloop.lock"
    monkeypatch.setattr(implement_issue, "LOCKFILE", lock_path)
    monkeypatch.setattr(implement_issue, "load_config", lambda path=None: _test_cfg())

    issues = [{"number": 1, "title": "Issue one", "body": "", "labels": []}]
    call_count = [0]

    def fake_get_top():
        if call_count[0] < len(issues):
            return issues[call_count[0]]
        return None

    monkeypatch.setattr(implement_issue, "get_top_ready_issue", fake_get_top)
    monkeypatch.setattr(implement_issue, "cleanup_merged_labels", lambda: None)
    monkeypatch.setattr(implement_issue, "unblock_ready_issues", lambda: None)

    def fake_implement_single(issue, require_design=False):
        call_count[0] += 1
        return True

    monkeypatch.setattr(implement_issue, "implement_single_issue", fake_implement_single)

    implement_issue.cfg = None
    implement_issue.main()
    out = capsys.readouterr().out
    assert "Implemented 1 issue(s) this run." in out
    assert call_count[0] == 1


def test_main_no_ready_issues_prints_message(monkeypatch, tmp_path, capsys):
    lock_path = tmp_path / ".autoloop.lock"
    monkeypatch.setattr(implement_issue, "LOCKFILE", lock_path)
    monkeypatch.setattr(implement_issue, "load_config", lambda path=None: _test_cfg())
    monkeypatch.setattr(implement_issue, "get_top_ready_issue", lambda: None)
    monkeypatch.setattr(implement_issue, "cleanup_merged_labels", lambda: None)
    monkeypatch.setattr(implement_issue, "unblock_ready_issues", lambda: None)

    implement_issue.cfg = None
    implement_issue.main()
    out = capsys.readouterr().out
    assert "No more ready issues." in out


def test_main_issue_flag_targets_specific_issue(monkeypatch, tmp_path):
    lock_path = tmp_path / ".autoloop.lock"
    monkeypatch.setattr(implement_issue, "LOCKFILE", lock_path)
    monkeypatch.setattr(implement_issue, "load_config", lambda path=None: _test_cfg())
    monkeypatch.setattr(implement_issue, "cleanup_merged_labels", lambda: None)
    monkeypatch.setattr(implement_issue, "unblock_ready_issues", lambda: None)

    targeted = []
    monkeypatch.setattr(
        implement_issue,
        "implement_targeted_issue",
        lambda number, require_design=False: targeted.append(number) or True,
    )

    implement_issue.cfg = None
    implement_issue.main(issue=28)
    assert targeted == [28]


# --- post_in_progress_comment tests ---


def test_post_in_progress_comment_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    post_in_progress_comment(42)

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"
    body = captured["cmd"][captured["cmd"].index("--body") + 1]
    assert "bot" in body.lower()


# --- label_in_review tests ---


def test_label_in_review_uses_cfg_repo(monkeypatch):
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(repo="my-org/my-repo"))
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    implement_issue.label_in_review(42)

    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "my-org/my-repo"


# --- is_branch_empty tests ---


def test_is_branch_empty_true_when_zero_commits(monkeypatch):
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "0\n", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    assert is_branch_empty("autoloop/42-feature") is True


def test_is_branch_empty_true_when_command_fails(monkeypatch):
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "error"})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    assert is_branch_empty("autoloop/42-feature") is True


def test_is_branch_empty_false_when_commits_exist(monkeypatch):
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 0, "stdout": "3\n", "stderr": ""})()

    monkeypatch.setattr(implement_issue.subprocess, "run", fake_run)
    assert is_branch_empty("autoloop/42-feature") is False


# --- Empty branch short-circuit in implement_single_issue ---


def test_implement_single_issue_empty_branch_no_retries(monkeypatch, tmp_path):
    """Empty branch after attempt 1 short-circuits with no retries."""
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(max_retries=3))
    log_path = tmp_path / "run_history.jsonl"
    monkeypatch.setattr(implement_issue, "LOG_FILE", log_path)

    attempt_count = [0]

    def fake_implement(issue, previous_errors=None):
        attempt_count[0] += 1
        return _claude_result()

    monkeypatch.setattr(implement_issue, "implement", fake_implement)
    monkeypatch.setattr(implement_issue, "create_branch", lambda issue: "autoloop/42-x")
    monkeypatch.setattr(implement_issue, "cleanup_branch", lambda branch: None)
    monkeypatch.setattr(implement_issue, "is_branch_empty", lambda branch: True)

    posted_comments = []
    monkeypatch.setattr(
        implement_issue,
        "post_attempt_failure",
        lambda n, a, e: posted_comments.append(e),
    )
    monkeypatch.setattr(
        implement_issue.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    result = implement_single_issue(_FAKE_ISSUE)
    assert result is False
    assert attempt_count[0] == 1
    assert len(posted_comments) == 1
    assert "No changes were produced" in posted_comments[0]
    assert "Missing .claude/settings.json" in posted_comments[0]


def test_implement_single_issue_empty_branch_posts_diagnostic(monkeypatch, tmp_path, capsys):
    """Diagnostic message is printed and posted, not lint/test noise."""
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(max_retries=3))
    log_path = tmp_path / "run_history.jsonl"
    monkeypatch.setattr(implement_issue, "LOG_FILE", log_path)

    monkeypatch.setattr(
        implement_issue, "implement", lambda issue, previous_errors=None: _claude_result()
    )
    monkeypatch.setattr(implement_issue, "create_branch", lambda issue: "autoloop/42-x")
    monkeypatch.setattr(implement_issue, "cleanup_branch", lambda branch: None)
    monkeypatch.setattr(implement_issue, "is_branch_empty", lambda branch: True)
    monkeypatch.setattr(implement_issue, "post_attempt_failure", lambda n, a, e: None)
    monkeypatch.setattr(
        implement_issue.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    implement_single_issue(_FAKE_ISSUE)
    output = capsys.readouterr().out
    assert "No changes were produced" in output
    assert "Implementation produced no changes" in output


def test_implement_single_issue_nonempty_branch_still_retries(monkeypatch, tmp_path):
    """Non-empty branch failures (real test/lint errors) retain retry behavior."""
    monkeypatch.setattr(implement_issue, "cfg", _test_cfg(max_retries=3))
    log_path = tmp_path / "run_history.jsonl"
    monkeypatch.setattr(implement_issue, "LOG_FILE", log_path)

    attempt_count = [0]

    def fake_implement(issue, previous_errors=None):
        attempt_count[0] += 1
        return _claude_result()

    def fake_verify(branch):
        return False, "Tests failed:\nsome test output"

    monkeypatch.setattr(implement_issue, "implement", fake_implement)
    monkeypatch.setattr(implement_issue, "create_branch", lambda issue: "autoloop/42-x")
    monkeypatch.setattr(implement_issue, "cleanup_branch", lambda branch: None)
    monkeypatch.setattr(implement_issue, "is_branch_empty", lambda branch: False)
    monkeypatch.setattr(implement_issue, "verify_implementation", fake_verify)
    monkeypatch.setattr(implement_issue, "post_attempt_failure", lambda n, a, e: None)
    monkeypatch.setattr(
        implement_issue.subprocess,
        "run",
        lambda *a, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    result = implement_single_issue(_FAKE_ISSUE)
    assert result is False
    assert attempt_count[0] == 3


def test_empty_branch_diagnostic_content():
    """Verify the diagnostic lists all three probable causes."""
    assert "No changes were produced" in EMPTY_BRANCH_DIAGNOSTIC
    assert "Missing .claude/settings.json permissions" in EMPTY_BRANCH_DIAGNOSTIC
    assert "active Claude Code session" in EMPTY_BRANCH_DIAGNOSTIC
    assert "inner claude invocation failed to start" in EMPTY_BRANCH_DIAGNOSTIC
