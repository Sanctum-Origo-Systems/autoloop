from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

from autoloop.create_issue import (
    DEFAULT_ACCEPTANCE,
    build_issue,
    build_issue_body,
    create_issues_from_spec,
    extract_files_from_spec,
    extract_problem_from_spec,
    fetch_issue,
    get_rejection_reason,
    parse_issue_sections,
    parse_spec_enhancements,
    prompt_multiline,
    prompt_optional,
    prompt_required,
    suggest_fields,
    update_issue,
)


def _cfg(**overrides):
    defaults = {"repo": "test-owner/test-repo", "triage_model": "sonnet"}
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# --- Pure function tests: build_issue_body ---


def test_build_issue_body_feature_all_sections():
    body = build_issue_body(
        summary="Add verbose flag",
        issue_type="feature",
        files="src/patina/cli.py",
        current_behavior="",
        expected="Prints one line per message",
        extra_criteria="CLI shows output",
        hints="Follow --dry-run pattern",
        deps="Depends on #43",
        context="See issue #40",
    )
    assert "## Summary\nAdd verbose flag" in body
    assert "## Type\nfeature" in body
    assert "## Files to Modify\n- src/patina/cli.py" in body
    assert "## Expected Behavior\nPrints one line per message" in body
    assert "## Acceptance Criteria" in body
    assert "## Implementation Hints\nFollow --dry-run pattern" in body
    assert "## Dependencies\nDepends on #43" in body
    assert "## Context\nSee issue #40" in body
    assert "## Story Points" in body


def test_build_issue_body_bug_includes_current_behavior():
    body = build_issue_body(
        summary="Fix crash",
        issue_type="bug",
        files="",
        current_behavior="It crashes on ingest",
        expected="No crash",
        extra_criteria="",
        hints="",
        deps="",
        context="",
    )
    assert "## Current Behavior\nIt crashes on ingest" in body


def test_build_issue_body_feature_omits_current_behavior():
    body = build_issue_body(
        summary="Add flag",
        issue_type="feature",
        files="",
        current_behavior="",
        expected="Flag works",
        extra_criteria="",
        hints="",
        deps="",
        context="",
    )
    assert "## Current Behavior" not in body


def test_build_issue_body_blank_files_shows_unknown():
    body = build_issue_body(
        summary="X",
        issue_type="feature",
        files="",
        current_behavior="",
        expected="Y",
        extra_criteria="",
        hints="",
        deps="",
        context="",
    )
    assert "## Files to Modify\nUnknown" in body


def test_build_issue_body_always_includes_default_acceptance():
    body = build_issue_body(
        summary="X",
        issue_type="feature",
        files="",
        current_behavior="",
        expected="Y",
        extra_criteria="",
        hints="",
        deps="",
        context="",
    )
    for criterion in DEFAULT_ACCEPTANCE:
        assert f"- [ ] {criterion}" in body


def test_build_issue_body_extra_criteria_appended():
    body = build_issue_body(
        summary="X",
        issue_type="feature",
        files="",
        current_behavior="",
        expected="Y",
        extra_criteria="API returns 200\nLatency < 100ms",
        hints="",
        deps="",
        context="",
    )
    assert "- [ ] API returns 200" in body
    assert "- [ ] Latency < 100ms" in body
    for criterion in DEFAULT_ACCEPTANCE:
        assert f"- [ ] {criterion}" in body


def test_build_issue_body_optional_sections_omitted_when_empty():
    body = build_issue_body(
        summary="X",
        issue_type="feature",
        files="",
        current_behavior="",
        expected="Y",
        extra_criteria="",
        hints="",
        deps="",
        context="",
    )
    assert "## Implementation Hints" not in body
    assert "## Dependencies" not in body
    assert "## Context" not in body


def test_build_issue_body_story_points_always_blank():
    body = build_issue_body(
        summary="X",
        issue_type="feature",
        files="",
        current_behavior="",
        expected="Y",
        extra_criteria="",
        hints="",
        deps="",
        context="",
    )
    assert "## Story Points\n<!-- Triage bot will estimate -->" in body


# --- Pure function tests: parse_issue_sections ---


def test_parse_issue_sections_basic():
    body = "## Summary\nFix crash\n\n## Type\nbug\n\n## Expected Behavior\nNo crash"
    sections = parse_issue_sections(body)
    assert sections["Summary"] == "Fix crash"
    assert sections["Type"] == "bug"
    assert sections["Expected Behavior"] == "No crash"


def test_parse_issue_sections_multiline_content():
    body = "## Summary\nFix crash\n\n## Acceptance Criteria\n- [ ] Tests pass\n- [ ] Lint clean"
    sections = parse_issue_sections(body)
    assert "- [ ] Tests pass" in sections["Acceptance Criteria"]
    assert "- [ ] Lint clean" in sections["Acceptance Criteria"]


def test_parse_issue_sections_empty_body():
    assert parse_issue_sections("") == {}


# --- Pure function tests: get_rejection_reason ---


def test_get_rejection_reason_found():
    issue_data = {
        "comments": [
            {"body": "**Auto-triage — Rejected:** Expected Behavior is vague"},
        ],
    }
    assert get_rejection_reason(issue_data) == "Expected Behavior is vague"


def test_get_rejection_reason_not_found():
    issue_data = {"comments": [{"body": "Some other comment"}]}
    assert get_rejection_reason(issue_data) is None


def test_get_rejection_reason_no_comments():
    assert get_rejection_reason({"comments": []}) is None
    assert get_rejection_reason({}) is None


# --- Pure function tests: parse_spec_enhancements ---

SAMPLE_SPEC = """\
# My Spec

## Context

Some context here.

---

## Task 1: Feed Errors Back

**Problem:** Errors are lost on retry.

**File:** `autoloop/implement_issue.py`

**Change:** Pass errors forward.

---

## Task 2: Increase Timeout

**Problem:** Timeout too short.

**File:** `autoloop/implement_issue.py` and `autoloop/triage_issues.py`

**Change:** Make timeout configurable.

---

## Summary of Changes

| Task | File |
|------|------|
"""

SAMPLE_SPEC_LEGACY = """\
# My Spec

## Enhancement 1: Feed Errors Back

**Problem:** Errors are lost on retry.

**File:** `autoloop/implement_issue.py`

---

## Enhancement 2: Increase Timeout

**Problem:** Timeout too short.

**File:** `autoloop/implement_issue.py` and `autoloop/triage_issues.py`

---

## Summary of Changes

| Enhancement | File |
|-------------|------|
"""


def test_parse_spec_enhancements_from_file(tmp_path):
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(SAMPLE_SPEC)
    enhancements = parse_spec_enhancements(str(spec_file))
    assert len(enhancements) == 2
    assert enhancements[0]["title"] == "Feed Errors Back"
    assert enhancements[1]["title"] == "Increase Timeout"
    assert "Errors are lost" in enhancements[0]["body"]
    assert "Timeout too short" in enhancements[1]["body"]


def test_parse_spec_enhancements_stops_at_summary(tmp_path):
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(SAMPLE_SPEC)
    enhancements = parse_spec_enhancements(str(spec_file))
    for enh in enhancements:
        assert "Summary of Changes" not in enh["body"]


def test_parse_spec_enhancements_empty_file(tmp_path):
    spec_file = tmp_path / "spec.md"
    spec_file.write_text("# No enhancements here\n\nJust text.")
    assert parse_spec_enhancements(str(spec_file)) == []


# --- Pure function tests: extract_files_from_spec ---


def test_extract_files_single():
    body = "**File:** `autoloop/implement_issue.py`"
    assert extract_files_from_spec(body) == ["autoloop/implement_issue.py"]


def test_extract_files_multiple_with_and():
    body = "**File:** `autoloop/implement_issue.py` and `autoloop/triage_issues.py`"
    assert extract_files_from_spec(body) == [
        "autoloop/implement_issue.py",
        "autoloop/triage_issues.py",
    ]


def test_extract_files_none():
    assert extract_files_from_spec("No files mentioned") == []


# --- Pure function tests: extract_problem_from_spec ---


def test_extract_problem():
    body = "**Problem:** Errors are lost on retry.\n\n**File:** `x.py`"
    assert extract_problem_from_spec(body) == "Errors are lost on retry."


def test_extract_problem_multiline():
    body = "**Problem:** Errors are lost.\nThis wastes retries.\n\n**Change:** Fix it."
    result = extract_problem_from_spec(body)
    assert "Errors are lost" in result
    assert "wastes retries" in result


def test_extract_problem_none():
    assert extract_problem_from_spec("No problem here") == ""


# --- I/O tests: prompt functions ---


def test_prompt_required_rejects_empty_then_accepts(monkeypatch):
    inputs = iter(["", "  ", "actual value"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    result = prompt_required("Summary")
    assert result == "actual value"


def test_prompt_optional_returns_empty_on_enter(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    result = prompt_optional("Hints", "optional")
    assert result == ""


def test_prompt_optional_returns_value(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "some hint")
    result = prompt_optional("Hints")
    assert result == "some hint"


def test_prompt_multiline_collects_until_blank(monkeypatch):
    inputs = iter(["line one", "line two", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    result = prompt_multiline("Files")
    assert result == "line one\nline two"


def test_prompt_multiline_empty_immediately(monkeypatch):
    inputs = iter([""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    result = prompt_multiline("Files")
    assert result == ""


# --- cfg.triage_model tests ---


def test_suggest_fields_uses_cfg_triage_model(monkeypatch):
    cfg = _cfg(triage_model="opus")
    captured = {}
    call_count = [0]

    class FakeFind:
        returncode = 0
        stdout = "src/patina/store.py\n"

    class FakeClaude:
        returncode = 0
        stdout = json.dumps(
            {
                "files": ["src/patina/store.py"],
                "current_behavior": "",
                "expected_behavior": "works",
                "acceptance_criteria": [],
                "implementation_hints": "",
            }
        )

    def selective_run(cmd, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return FakeFind()
        captured["cmd"] = cmd
        return FakeClaude()

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
    with patch("autoloop.create_issue.subprocess.run", side_effect=selective_run):
        suggest_fields("Fix something", "bug", cfg)

    assert "--model" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--model") + 1] == "opus"


def test_suggest_fields_returns_parsed_json(monkeypatch):
    cfg = _cfg()
    suggestion_json = json.dumps(
        {
            "files": ["src/patina/agent/runtime.py"],
            "current_behavior": "PROFILE.md not loaded",
            "expected_behavior": "PROFILE.md loaded into system prompt",
            "acceptance_criteria": ["system_prompt includes profile content"],
            "implementation_hints": "Mirror load_soul() pattern in config.py",
        }
    )

    class FakeResult:
        returncode = 0
        stdout = suggestion_json

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
    with patch("autoloop.create_issue.subprocess.run", return_value=FakeResult()):
        result = suggest_fields("Fix profile loading", "bug", cfg)
    assert result is not None
    assert result["files"] == ["src/patina/agent/runtime.py"]
    assert "Mirror load_soul" in result["implementation_hints"]


def test_suggest_fields_returns_none_when_no_claude(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    assert suggest_fields("Fix something", "bug", cfg) is None


def test_suggest_fields_returns_none_on_bad_json(monkeypatch):
    cfg = _cfg()

    class FakeResult:
        returncode = 0
        stdout = "not json at all"

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
    with patch("autoloop.create_issue.subprocess.run", return_value=FakeResult()):
        assert suggest_fields("Fix something", "bug", cfg) is None


def test_suggest_fields_returns_none_on_timeout(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")

    class FakeFind:
        returncode = 0
        stdout = "src/patina/store.py\n"

    call_count = 0

    def selective_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return FakeFind()
        raise subprocess.TimeoutExpired(cmd="claude", timeout=30)

    with patch("autoloop.create_issue.subprocess.run", side_effect=selective_run):
        assert suggest_fields("Fix something", "bug", cfg) is None


# --- cfg.repo tests ---


def test_fetch_issue_uses_cfg_repo():
    cfg = _cfg(repo="acme/widgets")
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"title": "test", "body": "", "labels": [], "comments": []})

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.create_issue.subprocess.run", side_effect=fake_run):
        fetch_issue(42, cfg)

    assert len(calls) == 1
    assert "--repo" in calls[0]
    assert calls[0][calls[0].index("--repo") + 1] == "acme/widgets"


def test_update_issue_uses_cfg_repo():
    cfg = _cfg(repo="acme/widgets")
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.create_issue.subprocess.run", side_effect=fake_run):
        update_issue(7, "title", "body", cfg)

    assert len(calls) == 2
    for call in calls:
        assert "--repo" in call
        assert call[call.index("--repo") + 1] == "acme/widgets"


def test_create_issues_from_spec_uses_cfg_repo(tmp_path, monkeypatch):
    cfg = _cfg(repo="acme/widgets")
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(
        "## Task 1: Add feature\n\n**Problem:** Missing feature.\n\n**File:** `src/foo.py`\n"
    )

    monkeypatch.setattr("shutil.which", lambda cmd: None)
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0
        stdout = "https://github.com/acme/widgets/issues/99"

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.create_issue.subprocess.run", side_effect=fake_run):
        create_issues_from_spec(str(spec_file), skip=[], cfg=cfg)

    gh_calls = [c for c in calls if c[0] == "gh"]
    assert len(gh_calls) >= 1
    for call in gh_calls:
        assert "--repo" in call
        assert call[call.index("--repo") + 1] == "acme/widgets"


# --- No bare constants ---


def test_no_bare_repo_constant():
    import autoloop.create_issue as mod

    assert not hasattr(mod, "REPO"), "Module should not have a bare REPO constant"


def test_no_bare_triage_model_constant():
    import autoloop.create_issue as mod

    assert not hasattr(mod, "TRIAGE_MODEL"), "Module should not have a bare TRIAGE_MODEL constant"


# --- I/O tests: build_issue ---


def test_build_issue_feature_happy_path(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    inputs = iter(
        [
            "Add verbose flag",
            "feature",
            "src/patina/cli.py",
            "",
            "Prints one line per message",
            "",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    title, body = build_issue(cfg)
    assert title == "Add verbose flag"
    assert "## Type\nfeature" in body


def test_build_issue_type_from_arg_skips_prompt(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    inputs = iter(
        [
            "Refactor extraction",
            "",
            "Cleaner code",
            "",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    title, body = build_issue(cfg, issue_type="refactor")
    assert title == "Refactor extraction"
    assert "## Type\nrefactor" in body


def test_build_issue_bug_prompts_current_behavior(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    inputs = iter(
        [
            "Fix crash on ingest",
            "bug",
            "",
            "It crashes with IndexError",
            "",
            "No crash",
            "",
            "",
            "",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
    title, body = build_issue(cfg)
    assert "## Current Behavior\nIt crashes with IndexError" in body


# --- Backward compatibility: ## Enhancement still works ---


def test_parse_spec_enhancements_legacy_tag(tmp_path):
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(SAMPLE_SPEC_LEGACY)
    enhancements = parse_spec_enhancements(str(spec_file))
    assert len(enhancements) == 2
    assert enhancements[0]["title"] == "Feed Errors Back"
    assert enhancements[1]["title"] == "Increase Timeout"


def test_parse_spec_enhancements_mixed_tags(tmp_path):
    spec = "## Task 1: First Item\n\nBody one.\n\n## Enhancement 2: Second Item\n\nBody two.\n"
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(spec)
    enhancements = parse_spec_enhancements(str(spec_file))
    assert len(enhancements) == 2
    assert enhancements[0]["title"] == "First Item"
    assert enhancements[1]["title"] == "Second Item"


def test_parse_spec_enhancements_without_number(tmp_path):
    spec = "## Task: Add auth\n\nBody here.\n"
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(spec)
    enhancements = parse_spec_enhancements(str(spec_file))
    assert len(enhancements) == 1
    assert enhancements[0]["title"] == "Add auth"


def test_parse_spec_enhancements_legacy_prints_notice(tmp_path, capsys):
    spec = "## Enhancement 1: Old Style\n\nBody.\n"
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(spec)
    parse_spec_enhancements(str(spec_file))
    captured = capsys.readouterr()
    assert "'## Enhancement' is supported but '## Task' is preferred" in captured.out


def test_parse_spec_enhancements_task_no_notice(tmp_path, capsys):
    spec = "## Task 1: New Style\n\nBody.\n"
    spec_file = tmp_path / "spec.md"
    spec_file.write_text(spec)
    parse_spec_enhancements(str(spec_file))
    captured = capsys.readouterr()
    assert "Enhancement" not in captured.out
