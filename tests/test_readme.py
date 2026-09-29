from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text()


class TestTldrQuickstart:
    def test_section_exists(self):
        assert "## TLDR: First PR in 5 Minutes" in README

    def test_appears_before_detailed_content(self):
        tldr_idx = README.index("## TLDR")
        how_idx = README.index("## How It Works")
        assert tldr_idx < how_idx

    def test_quickstart_includes_init(self):
        assert "autoloop init" in README[: README.index("## Platform Support")]

    def test_quickstart_includes_doctor(self):
        assert "autoloop doctor" in README[: README.index("## Platform Support")]

    def test_quickstart_includes_triage(self):
        assert "autoloop triage" in README[: README.index("## Platform Support")]

    def test_quickstart_includes_implement(self):
        assert "autoloop implement" in README[: README.index("## Platform Support")]

    def test_ends_with_clear_outcome(self):
        tldr_section = README[README.index("## TLDR") : README.index("## Platform Support")]
        assert "Review and merge the PR" in tldr_section

    def test_links_to_detailed_sections(self):
        tldr_section = README[README.index("## TLDR") : README.index("## Platform Support")]
        assert "#configuration" in tldr_section.lower() or "configuration" in tldr_section

    def test_under_30_lines(self):
        tldr_start = README.index("## TLDR")
        tldr_end = README.index("---", tldr_start)
        tldr_lines = README[tldr_start:tldr_end].strip().splitlines()
        assert len(tldr_lines) <= 30

    def test_install_uses_tagged_version(self):
        tldr_section = README[README.index("## TLDR") : README.index("---")]
        assert "@main" not in tldr_section
        assert "@v" in tldr_section

    def test_session_conflict_warning(self):
        tldr_section = README[README.index("## TLDR") : README.index("---")]
        assert "Close any Claude Code sessions" in tldr_section
        assert "parent folder" in tldr_section


class TestPlatformSupport:
    def test_section_exists(self):
        assert "## Platform Support" in README

    def test_macos_listed(self):
        assert "**macOS**" in README

    def test_linux_listed(self):
        assert "**Linux**" in README

    def test_windows_unsupported(self):
        assert "**Windows**: not supported" in README


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
        assert "Do not run `autoloop implement`, `triage`, `review-pr`, `fix-pr`, or `plan`" in README


class TestRunningUnattended:
    def test_intro_line(self):
        assert "Once you trust the pipeline, automate it with timers on a Linux VPS." in README

    def test_systemd_content_preserved(self):
        assert "### With systemd timers (Linux VPS)" in README

    def test_mobile_workflow_preserved(self):
        assert "### Mobile workflow" in README
