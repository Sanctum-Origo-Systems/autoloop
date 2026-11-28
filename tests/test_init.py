from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from autoloop.init import (
    WORKFLOW_TEMPLATE,
    _extract_command_prefixes,
    build_settings_allowlist,
    check_checkout,
    create_review_queue_issue,
    infer_test_pattern,
    remote_matches_repo,
    run_init,
    update_gitignore,
    write_claude_settings,
    write_workflow,
)

GENERATED_FILES = (
    "autoloop.toml",
    ".claude/settings.json",
    ".github/workflows/autoloop-cleanup.yml",
    ".gitignore",
)


def _git_init(path, origin=None):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin is not None:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)


def _make_checkout(path, monkeypatch, origin="https://github.com/acme/widgets.git"):
    """Turn path into a git checkout of acme/widgets and chdir into it."""
    _git_init(path, origin)
    monkeypatch.chdir(path)


def _assert_nothing_written(path):
    for name in GENERATED_FILES:
        assert not (path / name).exists(), name


class TestRemoteMatchesRepo:
    def test_https_with_git_suffix(self):
        assert remote_matches_repo("https://github.com/owner/target.git", "owner/target")

    def test_ssh_with_git_suffix(self):
        assert remote_matches_repo("git@github.com:owner/target.git", "owner/target")

    def test_https_without_git_suffix(self):
        assert remote_matches_repo("https://github.com/owner/target", "owner/target")

    def test_ssh_without_git_suffix(self):
        assert remote_matches_repo("git@github.com:owner/target", "owner/target")

    def test_ssh_url_scheme(self):
        assert remote_matches_repo("ssh://git@github.com/owner/target.git", "owner/target")

    def test_trailing_slash_and_newline(self):
        assert remote_matches_repo("https://github.com/owner/target/\n", "owner/target")

    def test_case_insensitive(self):
        assert remote_matches_repo("git@github.com:Owner/Target.git", "owner/target")

    def test_different_repo(self):
        assert not remote_matches_repo("git@github.com:owner/other.git", "owner/target")

    def test_different_owner(self):
        assert not remote_matches_repo("https://github.com/other/target.git", "owner/target")

    def test_repo_name_prefix_does_not_match(self):
        assert not remote_matches_repo("https://github.com/owner/target-two.git", "owner/target")

    def test_local_path_does_not_match(self):
        assert not remote_matches_repo("/srv/git/target", "owner/target")


class TestCheckCheckout:
    def test_ok_at_top_level_with_matching_origin(self, tmp_path):
        _git_init(tmp_path, "git@github.com:owner/target.git")
        assert check_checkout(tmp_path, "owner/target") == []

    def test_not_a_git_repo(self, tmp_path):
        problems = check_checkout(tmp_path, "owner/target")
        assert len(problems) == 1
        assert "not inside a git work tree" in problems[0]

    def test_subdirectory_of_repo(self, tmp_path):
        _git_init(tmp_path, "git@github.com:owner/target.git")
        sub = tmp_path / "sub"
        sub.mkdir()
        problems = check_checkout(sub, "owner/target")
        assert len(problems) == 1
        assert "not the top level" in problems[0]
        assert str(tmp_path.resolve()) in problems[0]

    def test_no_origin(self, tmp_path):
        _git_init(tmp_path)
        problems = check_checkout(tmp_path, "owner/target")
        assert len(problems) == 1
        assert "no origin remote" in problems[0]

    def test_mismatched_origin(self, tmp_path):
        _git_init(tmp_path, "git@github.com:owner/other.git")
        problems = check_checkout(tmp_path, "owner/target")
        assert problems == [
            "origin is git@github.com:owner/other.git, which does not match --repo owner/target"
        ]

    @patch("autoloop.init.subprocess.run", side_effect=FileNotFoundError)
    def test_git_missing(self, mock_run, tmp_path):
        problems = check_checkout(tmp_path, "owner/target")
        assert len(problems) == 1
        assert "git not found" in problems[0]


@patch("autoloop.init.create_review_queue_issue")
@patch("autoloop.init.create_labels")
class TestRunInitCheckoutGuard:
    def test_subdirectory_exits_nonzero_without_writing(
        self, mock_labels, mock_rq, tmp_path, monkeypatch, capsys
    ):
        _git_init(tmp_path, "git@github.com:owner/target.git")
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.chdir(sub)
        with pytest.raises(SystemExit) as exc:
            run_init("owner/target", verify_cmd="pytest")
        assert exc.value.code != 0
        _assert_nothing_written(sub)
        _assert_nothing_written(tmp_path)
        mock_labels.assert_not_called()
        mock_rq.assert_not_called()
        out = capsys.readouterr().out
        assert "not the top level" in out
        assert "Nothing was changed" in out

    def test_mismatched_origin_exits_nonzero_without_writing(
        self, mock_labels, mock_rq, tmp_path, monkeypatch, capsys
    ):
        _make_checkout(tmp_path, monkeypatch, origin="git@github.com:owner/other.git")
        with pytest.raises(SystemExit) as exc:
            run_init("owner/target", verify_cmd="pytest")
        assert exc.value.code != 0
        _assert_nothing_written(tmp_path)
        mock_labels.assert_not_called()
        mock_rq.assert_not_called()
        assert "git@github.com:owner/other.git" in capsys.readouterr().out

    def test_matching_origin_writes_files(self, mock_labels, mock_rq, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch, origin="git@github.com:owner/target.git")
        run_init("owner/target", verify_cmd="pytest")
        for name in GENERATED_FILES:
            assert (tmp_path / name).exists(), name
        mock_labels.assert_called_once()
        mock_rq.assert_called_once()

    def test_force_skips_both_checks(self, mock_labels, mock_rq, tmp_path, monkeypatch):
        # Subdirectory of a repo whose origin is a different repo: fails both checks.
        _git_init(tmp_path, "git@github.com:owner/other.git")
        sub = tmp_path / "sub"
        sub.mkdir()
        monkeypatch.chdir(sub)
        with patch("autoloop.init.check_checkout") as mock_check:
            run_init("owner/target", verify_cmd="pytest", force=True)
        mock_check.assert_not_called()
        for name in GENERATED_FILES:
            assert (sub / name).exists(), name

    def test_force_works_outside_git(self, mock_labels, mock_rq, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_init("owner/target", verify_cmd="pytest", force=True)
        assert (tmp_path / "autoloop.toml").exists()

    def test_dry_run_reports_failure_and_prints_plan(
        self, mock_labels, mock_rq, tmp_path, monkeypatch, capsys
    ):
        _make_checkout(tmp_path, monkeypatch, origin="git@github.com:owner/other.git")
        run_init("owner/target", verify_cmd="pytest", dry_run=True)
        _assert_nothing_written(tmp_path)
        out = capsys.readouterr().out
        assert "error: origin is git@github.com:owner/other.git" in out
        for name in GENERATED_FILES:
            assert f"[dry-run] write {name}" in out
        mock_labels.assert_called_once_with("owner/target", dry_run=True)
        mock_rq.assert_called_once_with("owner/target", dry_run=True)

    def test_dry_run_reports_ok(self, mock_labels, mock_rq, tmp_path, monkeypatch, capsys):
        _make_checkout(tmp_path, monkeypatch, origin="https://github.com/owner/target.git")
        run_init("owner/target", verify_cmd="pytest", dry_run=True, skip_labels=True)
        out = capsys.readouterr().out
        assert "ok: top level of a checkout of owner/target" in out
        mock_rq.assert_called_once_with("owner/target", dry_run=True)


class TestCliInitArgs:
    @patch("autoloop.init.run_init")
    def test_cli_passes_force_and_dry_run_by_keyword(self, mock_run_init, monkeypatch):
        from autoloop.cli import main

        monkeypatch.setattr(
            "sys.argv", ["autoloop", "init", "--repo", "owner/target", "--dry-run", "--force"]
        )
        main()
        _, kwargs = mock_run_init.call_args
        assert kwargs["dry_run"] is True
        assert kwargs["force"] is True
        assert kwargs["skip_labels"] is False


class TestExtractCommandPrefixes:
    def test_single_command(self):
        result = _extract_command_prefixes("pytest")
        assert result == ["Bash(pytest*)"]

    def test_compound_and(self):
        result = _extract_command_prefixes("ruff check && ruff format --check")
        assert "Bash(ruff check*)" in result
        assert "Bash(ruff format --check*)" in result

    def test_compound_or(self):
        result = _extract_command_prefixes("cmd1 || cmd2")
        assert "Bash(cmd1*)" in result
        assert "Bash(cmd2*)" in result

    def test_compound_semicolon(self):
        result = _extract_command_prefixes("cmd1 ; cmd2")
        assert "Bash(cmd1*)" in result
        assert "Bash(cmd2*)" in result

    def test_prefix_deduplication(self):
        result = _extract_command_prefixes("uv run ruff check && uv run ruff format --check")
        assert "Bash(uv run ruff check*)" in result
        assert "Bash(uv run ruff format --check*)" in result

    def test_empty_string(self):
        result = _extract_command_prefixes("")
        assert result == []

    def test_uv_run_pytest(self):
        result = _extract_command_prefixes("uv run pytest")
        assert result == ["Bash(uv run pytest*)"]

    def test_npm_test(self):
        result = _extract_command_prefixes("npm test")
        assert result == ["Bash(npm test*)"]


class TestBuildSettingsAllowlist:
    def test_verify_cmd_included(self):
        settings = build_settings_allowlist("pytest")
        allow = settings["permissions"]["allow"]
        assert "Bash(pytest*)" in allow

    def test_lint_cmd_included(self):
        settings = build_settings_allowlist("pytest", lint_cmd="ruff check")
        allow = settings["permissions"]["allow"]
        assert "Bash(pytest*)" in allow
        assert "Bash(ruff check*)" in allow

    def test_base_allowlist_present(self):
        settings = build_settings_allowlist("pytest")
        allow = settings["permissions"]["allow"]
        assert "Read" in allow
        assert "Edit" in allow
        assert "Write" in allow
        assert "Bash(git status)" in allow
        assert "Bash(git add *)" in allow
        assert "Bash(git commit *)" in allow

    def test_deny_list_present(self):
        settings = build_settings_allowlist("pytest")
        deny = settings["permissions"]["deny"]
        assert "Bash(git push*)" in deny
        assert "Bash(gh *)" in deny
        assert "Bash(git reset*)" in deny
        assert "Bash(rm -rf*)" in deny

    def test_push_and_gh_not_allowed(self):
        """Regression for #515: doctor --live requires the agent's push to be denied."""
        settings = build_settings_allowlist("uv run pytest")
        allow = settings["permissions"]["allow"]
        deny = settings["permissions"]["deny"]
        assert not [e for e in allow if e.startswith(("Bash(git push", "Bash(gh"))]
        assert "Bash(git push*)" in deny
        assert "Bash(gh *)" in deny

    def test_no_duplicates(self):
        settings = build_settings_allowlist("pytest", lint_cmd="pytest")
        allow = settings["permissions"]["allow"]
        assert allow.count("Bash(pytest*)") == 1

    def test_empty_lint_cmd_no_extra_entries(self):
        settings_no_lint = build_settings_allowlist("pytest", lint_cmd="")
        settings_with_lint = build_settings_allowlist("pytest", lint_cmd="ruff check")
        assert len(settings_with_lint["permissions"]["allow"]) > len(
            settings_no_lint["permissions"]["allow"]
        )


class TestWriteClaudeSettings:
    def test_creates_settings_fresh(self, tmp_path, capsys):
        write_claude_settings(tmp_path, "pytest")
        path = tmp_path / ".claude" / "settings.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "Bash(pytest*)" in data["permissions"]["allow"]
        assert "Read" in data["permissions"]["allow"]
        out = capsys.readouterr().out
        assert "created .claude/settings.json" in out

    def test_creates_claude_dir(self, tmp_path):
        write_claude_settings(tmp_path, "pytest")
        assert (tmp_path / ".claude").is_dir()

    def test_skips_existing_with_all_entries(self, tmp_path, capsys):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        needed = build_settings_allowlist("pytest")
        settings_path.write_text(json.dumps(needed, indent=2) + "\n")

        write_claude_settings(tmp_path, "pytest")
        out = capsys.readouterr().out
        assert "already exists with all needed entries" in out

    def test_skips_existing_reports_missing(self, tmp_path, capsys):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"allow": ["Read"]}}) + "\n")

        write_claude_settings(tmp_path, "pytest")
        out = capsys.readouterr().out
        assert "skipping creation" in out
        assert "Entries autoloop needs but are not present" in out
        assert "Bash(pytest*)" in out

    def test_skips_existing_reports_missing_deny(self, tmp_path, capsys):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        needed = build_settings_allowlist("pytest")
        needed["permissions"]["deny"] = ["Bash(rm -rf*)"]
        original = json.dumps(needed, indent=2) + "\n"
        settings_path.write_text(original)

        write_claude_settings(tmp_path, "pytest")
        out = capsys.readouterr().out
        assert "skipping creation" in out
        assert "Deny entries autoloop needs but are not present" in out
        assert "+ Bash(git push*)" in out
        assert "+ Bash(gh *)" in out
        assert "+ Bash(rm -rf*)" not in out
        assert "Entries autoloop needs but are not present" not in out
        assert settings_path.read_text() == original

    def test_never_overwrites_existing(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        settings_path = claude_dir / "settings.json"
        original = '{"permissions": {"allow": ["Read"]}}\n'
        settings_path.write_text(original)

        write_claude_settings(tmp_path, "pytest")
        assert settings_path.read_text() == original

    def test_verify_cmd_propagation(self, tmp_path):
        write_claude_settings(tmp_path, "npm test")
        path = tmp_path / ".claude" / "settings.json"
        data = json.loads(path.read_text())
        assert "Bash(npm test*)" in data["permissions"]["allow"]

    def test_lint_cmd_propagation(self, tmp_path):
        write_claude_settings(tmp_path, "pytest", lint_cmd="ruff check && ruff format --check")
        path = tmp_path / ".claude" / "settings.json"
        data = json.loads(path.read_text())
        allow = data["permissions"]["allow"]
        assert "Bash(ruff check*)" in allow
        assert "Bash(ruff format --check*)" in allow


class TestRunInitSettings:
    @patch("autoloop.init.create_labels")
    def test_init_creates_settings(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="pytest", skip_labels=True)
        path = tmp_path / ".claude" / "settings.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "Bash(pytest*)" in data["permissions"]["allow"]

    @patch("autoloop.init.create_labels")
    def test_init_passes_lint_cmd(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="pytest", lint_cmd="ruff check", skip_labels=True)
        path = tmp_path / ".claude" / "settings.json"
        data = json.loads(path.read_text())
        assert "Bash(ruff check*)" in data["permissions"]["allow"]

    @patch("autoloop.init.create_labels")
    def test_init_protected_paths_excludes_settings(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="pytest", skip_labels=True)
        toml_content = (tmp_path / "autoloop.toml").read_text()
        assert ".claude/settings.json" not in toml_content
        assert "autoloop.toml" in toml_content

    @patch("autoloop.init.create_labels")
    def test_init_next_steps_mentions_settings(self, mock_labels, tmp_path, monkeypatch, capsys):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="pytest", skip_labels=True)
        out = capsys.readouterr().out
        assert ".claude/settings.json" in out


class TestUpdateGitignore:
    def test_creates_gitignore_with_lock_entry(self, tmp_path):
        update_gitignore(tmp_path)
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert ".autoloop.lock" in lines

    def test_appends_lock_entry_preserving_content(self, tmp_path):
        path = tmp_path / ".gitignore"
        path.write_text("node_modules/\n*.pyc\n")
        update_gitignore(tmp_path)
        content = path.read_text()
        assert content.startswith("node_modules/\n*.pyc\n")
        assert ".autoloop.lock" in content.splitlines()

    def test_appends_newline_when_missing_trailing_newline(self, tmp_path):
        path = tmp_path / ".gitignore"
        path.write_text("node_modules/")
        update_gitignore(tmp_path)
        lines = path.read_text().splitlines()
        assert lines[0] == "node_modules/"
        assert ".autoloop.lock" in lines

    def test_idempotent_when_entries_present(self, tmp_path):
        path = tmp_path / ".gitignore"
        original = "dist/\n.autoloop.lock\nautoloop/run_history.jsonl\n"
        path.write_text(original)
        update_gitignore(tmp_path)
        assert path.read_text() == original

    def test_repeated_runs_no_duplicates(self, tmp_path):
        update_gitignore(tmp_path)
        update_gitignore(tmp_path)
        update_gitignore(tmp_path)
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        assert lines.count(".autoloop.lock") == 1
        assert lines.count("autoloop/run_history.jsonl") == 1

    def test_similar_entry_does_not_count_as_lock(self, tmp_path):
        path = tmp_path / ".gitignore"
        path.write_text("other.autoloop.lock\n")
        update_gitignore(tmp_path)
        assert ".autoloop.lock" in path.read_text().splitlines()

    @patch("autoloop.init.create_labels")
    def test_run_init_gitignores_lock(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="pytest", skip_labels=True)
        assert ".autoloop.lock" in (tmp_path / ".gitignore").read_text().splitlines()


class TestInferTestPattern:
    def test_uv_run_pytest(self):
        assert infer_test_pattern("uv run pytest") == "tests/*.py"

    def test_pytest_bare(self):
        assert infer_test_pattern("pytest") == "tests/*.py"

    def test_pytest_with_args(self):
        assert infer_test_pattern("pytest -x --tb=short") == "tests/*.py"

    def test_npm_test(self):
        assert infer_test_pattern("npm test") == "src/**/*.test.*"

    def test_jest(self):
        assert infer_test_pattern("jest") == "src/**/*.test.*"

    def test_vitest(self):
        assert infer_test_pattern("vitest") == "src/**/*.test.*"

    def test_npm_run_build(self):
        assert infer_test_pattern("npm run build") == ""

    def test_cargo_build(self):
        assert infer_test_pattern("cargo build") == ""

    def test_make_build(self):
        assert infer_test_pattern("make build") == ""

    def test_unrecognized_returns_none(self):
        assert infer_test_pattern("./run_checks.sh") is None

    def test_empty_string(self):
        assert infer_test_pattern("") is None

    def test_case_insensitive(self):
        assert infer_test_pattern("UV RUN PYTEST") == "tests/*.py"

    def test_build_takes_precedence_over_test(self):
        assert infer_test_pattern("npm run build && npm test") == ""


class TestWriteTomlTestPattern:
    @patch("autoloop.init.create_labels")
    def test_pytest_writes_test_pattern(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="uv run pytest", skip_labels=True)
        content = (tmp_path / "autoloop.toml").read_text()
        assert 'test_pattern = "tests/*.py"' in content

    @patch("autoloop.init.create_labels")
    def test_npm_build_writes_empty_test_pattern(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="npm run build", skip_labels=True)
        content = (tmp_path / "autoloop.toml").read_text()
        assert 'test_pattern = ""' in content

    @patch("autoloop.init.create_labels")
    def test_test_pattern_has_comment(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="uv run pytest", skip_labels=True)
        content = (tmp_path / "autoloop.toml").read_text()
        for line in content.splitlines():
            if "test_pattern" in line and "=" in line:
                assert "#" in line
                break
        else:
            raise AssertionError("test_pattern line not found in TOML")

    @patch("autoloop.init.create_labels")
    def test_test_pattern_loadable_by_config(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="uv run pytest", skip_labels=True)
        from autoloop.config import load_config

        cfg = load_config(tmp_path / "autoloop.toml")
        assert cfg.test_pattern == "tests/*.py"

    @patch("autoloop.init.create_labels")
    def test_npm_test_pattern_roundtrips(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="npm test", skip_labels=True)
        from autoloop.config import load_config

        cfg = load_config(tmp_path / "autoloop.toml")
        assert cfg.test_pattern == "src/**/*.test.*"


class TestWriteTomlTriageLabels:
    @patch("autoloop.init.create_labels")
    def test_triage_labels_include_review_queue(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="uv run pytest", skip_labels=True)
        from autoloop.config import load_config

        cfg = load_config(tmp_path / "autoloop.toml")
        assert "review-queue" in cfg.triage_labels


class TestWriteTomlJevBlock:
    @patch("autoloop.init.create_labels")
    def test_jev_block_present(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="uv run pytest", skip_labels=True)
        content = (tmp_path / "autoloop.toml").read_text()
        assert "[jev]" in content
        assert 'mode = "off"' in content

    @patch("autoloop.init.create_labels")
    def test_jev_block_has_shadow_comment(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        run_init("acme/widgets", verify_cmd="uv run pytest", skip_labels=True)
        content = (tmp_path / "autoloop.toml").read_text()
        assert "JEV_MODE=shadow" in content

    @patch("autoloop.init.create_labels")
    def test_jev_block_loadable_by_config(self, mock_labels, tmp_path, monkeypatch):
        _make_checkout(tmp_path, monkeypatch)
        monkeypatch.delenv("JEV_MODE", raising=False)
        run_init("acme/widgets", verify_cmd="uv run pytest", skip_labels=True)
        from autoloop.config import load_config

        cfg = load_config(tmp_path / "autoloop.toml")
        assert cfg.jev_mode == "off"


class TestWorkflowTemplate:
    def test_template_has_checkout_step(self):
        assert "actions/checkout@v4" in WORKFLOW_TEMPLATE

    def test_template_installs_from_main(self):
        assert "@main" in WORKFLOW_TEMPLATE

    def test_template_no_pinned_version(self):
        assert "autoloop@v" not in WORKFLOW_TEMPLATE

    def test_template_no_format_placeholders(self):
        assert "{version}" not in WORKFLOW_TEMPLATE


class TestCreateReviewQueueIssue:
    @patch("autoloop.init.subprocess.run")
    def test_creates_and_pins_when_no_existing(self, mock_run, capsys):
        mock_run.side_effect = [
            # gh issue list returns empty
            type(mock_run.return_value)(returncode=0, stdout="[]\n", stderr=""),
            # gh issue create returns URL
            type(mock_run.return_value)(
                returncode=0,
                stdout="https://github.com/acme/widgets/issues/42\n",
                stderr="",
            ),
            # gh issue pin succeeds
            type(mock_run.return_value)(returncode=0, stdout="", stderr=""),
        ]

        result = create_review_queue_issue("acme/widgets")
        assert result == 42

        calls = mock_run.call_args_list
        assert len(calls) == 3

        list_cmd = calls[0][0][0]
        assert "gh" in list_cmd
        assert "issue" in list_cmd
        assert "list" in list_cmd
        assert "--label" in list_cmd
        assert "review-queue" in list_cmd

        create_cmd = calls[1][0][0]
        assert "gh" in create_cmd
        assert "issue" in create_cmd
        assert "create" in create_cmd
        assert "--label" in create_cmd
        assert "review-queue" in create_cmd
        assert "--title" in create_cmd
        assert "Review Queue" in create_cmd

        pin_cmd = calls[2][0][0]
        assert pin_cmd == ["gh", "issue", "pin", "42", "--repo", "acme/widgets"]

        out = capsys.readouterr().out
        assert "created and pinned review-queue issue: #42" in out

    @patch("autoloop.init.subprocess.run")
    def test_skips_when_existing_issue(self, mock_run, capsys):
        mock_run.return_value = type(mock_run.return_value)(
            returncode=0, stdout='[{"number": 7}]\n', stderr=""
        )

        result = create_review_queue_issue("acme/widgets")
        assert result == 7

        mock_run.assert_called_once()
        out = capsys.readouterr().out
        assert "already exists: #7" in out

    @patch("autoloop.init.subprocess.run")
    def test_dry_run_skips_all_commands(self, mock_run, capsys):
        result = create_review_queue_issue("acme/widgets", dry_run=True)
        assert result is None
        mock_run.assert_not_called()
        out = capsys.readouterr().out
        assert "[dry-run]" in out

    @patch("autoloop.init.subprocess.run")
    def test_create_failure_returns_none(self, mock_run, capsys):
        mock_run.side_effect = [
            type(mock_run.return_value)(returncode=0, stdout="[]\n", stderr=""),
            type(mock_run.return_value)(returncode=1, stdout="", stderr="label not found"),
        ]

        result = create_review_queue_issue("acme/widgets")
        assert result is None
        out = capsys.readouterr().out
        assert "failed to create review-queue issue" in out

    @patch("autoloop.init.subprocess.run")
    def test_pin_failure_still_returns_number(self, mock_run, capsys):
        mock_run.side_effect = [
            type(mock_run.return_value)(returncode=0, stdout="[]\n", stderr=""),
            type(mock_run.return_value)(
                returncode=0,
                stdout="https://github.com/acme/widgets/issues/99\n",
                stderr="",
            ),
            type(mock_run.return_value)(returncode=1, stdout="", stderr="pin error"),
        ]

        result = create_review_queue_issue("acme/widgets")
        assert result == 99
        out = capsys.readouterr().out
        assert "warning: failed to pin" in out
        assert "created review-queue issue: #99 (pinning failed)" in out
        assert "created and pinned" not in out


class TestWriteWorkflow:
    def test_creates_workflow_file(self, tmp_path, capsys):
        write_workflow(tmp_path)
        path = tmp_path / ".github" / "workflows" / "autoloop-cleanup.yml"
        assert path.exists()
        out = capsys.readouterr().out
        assert "created .github/workflows/autoloop-cleanup.yml" in out

    def test_workflow_has_checkout_before_python(self, tmp_path):
        write_workflow(tmp_path)
        path = tmp_path / ".github" / "workflows" / "autoloop-cleanup.yml"
        content = path.read_text()
        checkout_pos = content.index("actions/checkout@v4")
        python_pos = content.index("actions/setup-python@v5")
        assert checkout_pos < python_pos

    def test_workflow_installs_from_main(self, tmp_path):
        write_workflow(tmp_path)
        path = tmp_path / ".github" / "workflows" / "autoloop-cleanup.yml"
        content = path.read_text()
        assert "autoloop@main" in content
        assert "autoloop@v" not in content

    def test_workflow_skips_existing(self, tmp_path, capsys):
        path = tmp_path / ".github" / "workflows" / "autoloop-cleanup.yml"
        path.parent.mkdir(parents=True)
        path.write_text("existing")
        write_workflow(tmp_path)
        assert path.read_text() == "existing"
        out = capsys.readouterr().out
        assert "already exists" in out

    def test_workflow_valid_github_actions_expressions(self, tmp_path):
        write_workflow(tmp_path)
        path = tmp_path / ".github" / "workflows" / "autoloop-cleanup.yml"
        content = path.read_text()
        assert "${{ secrets.GITHUB_TOKEN }}" in content
        assert "${{ github.event.pull_request.number }}" in content


def _load_workflow(tmp_path):
    import yaml

    write_workflow(tmp_path)
    path = tmp_path / ".github" / "workflows" / "autoloop-cleanup.yml"
    return yaml.safe_load(path.read_text())


def _triggers(workflow):
    # YAML 1.1 parses a bare ``on`` key as boolean True.
    return workflow.get("on", workflow.get(True))


class TestReviewQueueJob:
    def test_workflow_parses_as_yaml_with_review_queue_job(self, tmp_path):
        workflow = _load_workflow(tmp_path)
        assert "review-queue" in workflow["jobs"]

    def test_triggered_by_pull_request_closed_and_synchronize(self, tmp_path):
        types = _triggers(_load_workflow(tmp_path))["pull_request"]["types"]
        assert "closed" in types
        assert "synchronize" in types

    def test_triggered_by_all_pull_request_types(self, tmp_path):
        types = _triggers(_load_workflow(tmp_path))["pull_request"]["types"]
        assert set(types) == {
            "opened",
            "reopened",
            "closed",
            "synchronize",
            "edited",
            "ready_for_review",
            "converted_to_draft",
        }

    def test_has_workflow_dispatch_and_commented_workflow_run(self, tmp_path):
        workflow = _load_workflow(tmp_path)
        triggers = _triggers(workflow)
        assert "workflow_dispatch" in triggers
        assert "workflow_run" not in triggers
        assert "# workflow_run:" in WORKFLOW_TEMPLATE

    def test_existing_jobs_keep_merged_condition(self, tmp_path):
        jobs = _load_workflow(tmp_path)["jobs"]
        assert jobs["cleanup"]["if"] == "github.event.pull_request.merged == true"
        assert jobs["auto-close-parent"]["if"] == "github.event.pull_request.merged == true"

    def test_review_queue_job_has_no_merged_condition(self, tmp_path):
        job = _load_workflow(tmp_path)["jobs"]["review-queue"]
        assert "if" not in job

    def test_concurrency_does_not_cancel_in_progress(self, tmp_path):
        job = _load_workflow(tmp_path)["jobs"]["review-queue"]
        assert job["concurrency"] == {"group": "review-queue", "cancel-in-progress": False}

    def test_permissions(self, tmp_path):
        perms = _load_workflow(tmp_path)["jobs"]["review-queue"]["permissions"]
        assert perms["issues"] == "write"
        assert perms["pull-requests"] == "read"
        assert perms["checks"] == "read"

    def test_checkout_uses_default_branch_ref(self, tmp_path):
        steps = _load_workflow(tmp_path)["jobs"]["review-queue"]["steps"]
        checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))
        assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"

    def test_installs_like_auto_close_parent_and_runs_review_queue(self, tmp_path):
        jobs = _load_workflow(tmp_path)["jobs"]
        steps = jobs["review-queue"]["steps"]
        acp_install = next(
            s for s in jobs["auto-close-parent"]["steps"] if "pip" in s.get("run", "")
        )
        install = next(s for s in steps if "pip" in s.get("run", ""))
        assert install["run"] == acp_install["run"]
        last = steps[-1]
        assert last["run"] == "autoloop review-queue"
        assert last["env"] == {"GH_TOKEN": "${{ github.token }}"}
