from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text()


class TestPlatformSupport:
    def test_section_exists(self):
        assert "## Platform Support" in README

    def test_macos_listed(self):
        assert "**macOS**" in README

    def test_linux_listed(self):
        assert "**Linux**" in README

    def test_windows_unsupported(self):
        assert "**Windows** — not supported" in README


class TestModeFraming:
    def test_section_before_quick_start(self):
        how_idx = README.index("## How It Works")
        qs_idx = README.index("## Quick Start")
        assert how_idx < qs_idx

    def test_table_has_local_and_unattended(self):
        assert "Local mode" in README
        assert "Unattended mode" in README

    def test_table_rows(self):
        assert "| Where |" in README
        assert "| Trigger |" in README
        assert "| Gate |" in README
        assert "| Best for |" in README


class TestQuickStartLocalMode:
    def test_labeled_as_local_mode(self):
        assert "## Quick Start (Local Mode)" in README

    def test_local_mode_notes_exist(self):
        assert "### Local Mode Notes" in README

    def test_settings_json_note(self):
        assert "`.claude/settings.json`" in README

    def test_concurrent_session_warning(self):
        assert "Do not run `autoloop implement` while a Claude Code session is open" in README


class TestRunningUnattended:
    def test_intro_line(self):
        assert "Once you trust the pipeline, automate it with timers on a Linux VPS." in README

    def test_systemd_content_preserved(self):
        assert "### With systemd timers (Linux VPS)" in README

    def test_mobile_workflow_preserved(self):
        assert "### Mobile workflow" in README
