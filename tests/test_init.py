from __future__ import annotations

import json
from unittest.mock import patch

from autoloop.init import (
    _extract_command_prefixes,
    build_settings_allowlist,
    run_init,
    write_claude_settings,
)


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
        assert "Bash(gh *)" in allow

    def test_deny_list_present(self):
        settings = build_settings_allowlist("pytest")
        deny = settings["permissions"]["deny"]
        assert "Bash(git push --force*)" in deny
        assert "Bash(git reset --hard*)" in deny
        assert "Bash(rm -rf*)" in deny

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
        monkeypatch.chdir(tmp_path)
        run_init("acme/widgets", verify_cmd="pytest", skip_labels=True)
        path = tmp_path / ".claude" / "settings.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert "Bash(pytest*)" in data["permissions"]["allow"]

    @patch("autoloop.init.create_labels")
    def test_init_passes_lint_cmd(self, mock_labels, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_init("acme/widgets", verify_cmd="pytest", lint_cmd="ruff check", skip_labels=True)
        path = tmp_path / ".claude" / "settings.json"
        data = json.loads(path.read_text())
        assert "Bash(ruff check*)" in data["permissions"]["allow"]

    @patch("autoloop.init.create_labels")
    def test_init_protected_paths_includes_settings(self, mock_labels, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        run_init("acme/widgets", verify_cmd="pytest", skip_labels=True)
        toml_content = (tmp_path / "autoloop.toml").read_text()
        assert ".claude/settings.json" in toml_content

    @patch("autoloop.init.create_labels")
    def test_init_next_steps_mentions_settings(self, mock_labels, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        run_init("acme/widgets", verify_cmd="pytest", skip_labels=True)
        out = capsys.readouterr().out
        assert ".claude/settings.json" in out
