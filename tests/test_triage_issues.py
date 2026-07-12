from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from autoloop.claude_runner import ClaudeResult
from autoloop.triage_issues import (
    build_decomposition_comment,
    build_sub_issue_summary_comment,
    build_triage_prompt,
    parse_file_discovery_response,
    parse_rewritten_body,
    parse_sub_issue_response,
    parse_triage_response,
    validate_discovered_files,
)


def _cfg(**overrides):
    defaults = {
        "repo": "test-owner/test-repo",
        "triage_model": "sonnet",
        "triage_timeout": 90,
        "max_story_points": 2,
        "verify_cmd": "uv run pytest",
        "lint_command": "uv run ruff check && uv run ruff format --check",
        "tree_truncation": 3000,
        "triage_labels": [
            "ready",
            "rejected",
            "needs-decomposition",
            "in-progress",
            "in-review",
            "needs-human",
        ],
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# --- Pure function tests: parse_triage_response ---


def test_parse_triage_response_valid_json():
    stdout = json.dumps({"verdict": "ready", "points": 1, "priority": "p1", "reason": "ok"})
    result = parse_triage_response(stdout)
    assert result["verdict"] == "ready"
    assert result["points"] == 1


def test_parse_triage_response_code_fence():
    stdout = '```json\n{"verdict": "rejected", "reason": "vague"}\n```'
    result = parse_triage_response(stdout)
    assert result["verdict"] == "rejected"
    assert result["reason"] == "vague"


def test_parse_triage_response_invalid_json():
    result = parse_triage_response("not json")
    assert result["verdict"] == "rejected"
    assert "Failed to parse" in result["reason"]


def test_parse_triage_response_empty():
    result = parse_triage_response("")
    assert result["verdict"] == "rejected"


# --- Pure function tests: parse_file_discovery_response ---


def test_parse_file_discovery_response_valid():
    data = {"files_to_modify": [{"path": "src/x.py", "reason": "main"}]}
    result = parse_file_discovery_response(json.dumps(data))
    assert len(result) == 1
    assert result[0]["path"] == "src/x.py"


def test_parse_file_discovery_response_code_fence():
    data = {"files_to_modify": [{"path": "src/y.py", "reason": "test"}]}
    stdout = f"```json\n{json.dumps(data)}\n```"
    result = parse_file_discovery_response(stdout)
    assert len(result) == 1


def test_parse_file_discovery_response_invalid():
    assert parse_file_discovery_response("garbage") == []


def test_parse_file_discovery_response_empty():
    assert parse_file_discovery_response("") == []


# --- Pure function tests: validate_discovered_files ---


def test_validate_discovered_files_existing(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").touch()
    files = [
        {"path": "src/real.py", "reason": "exists"},
        {"path": "src/fake.py", "reason": "missing"},
    ]
    result = validate_discovered_files(files, tmp_path)
    assert len(result) == 1
    assert result[0]["path"] == "src/real.py"


def test_validate_discovered_files_test_files_pass(tmp_path):
    files = [{"path": "tests/test_new.py", "reason": "new test"}]
    result = validate_discovered_files(files, tmp_path)
    assert len(result) == 1


# --- Pure function tests: parse_sub_issue_response ---


def test_parse_sub_issue_response_valid():
    data = {
        "expected_behavior": "works correctly",
        "acceptance_criteria": ["test passes"],
    }
    result = parse_sub_issue_response(json.dumps(data))
    assert result is not None
    assert result["expected_behavior"] == "works correctly"


def test_parse_sub_issue_response_code_fence():
    data = {"expected_behavior": "ok", "acceptance_criteria": []}
    stdout = f"```json\n{json.dumps(data)}\n```"
    result = parse_sub_issue_response(stdout)
    assert result is not None


def test_parse_sub_issue_response_missing_field():
    assert parse_sub_issue_response(json.dumps({"other": "data"})) is None


def test_parse_sub_issue_response_invalid():
    assert parse_sub_issue_response("not json") is None


def test_parse_sub_issue_response_not_dict():
    assert parse_sub_issue_response(json.dumps([1, 2, 3])) is None


# --- Pure function tests: parse_rewritten_body ---


def test_parse_rewritten_body_plain():
    body = "## Summary\nFixed the thing\n\n## Type\nfeature"
    assert parse_rewritten_body(body) == body


def test_parse_rewritten_body_code_fence():
    body = "```markdown\n## Summary\nFixed\n```"
    result = parse_rewritten_body(body)
    assert result is not None
    assert "## Summary" in result


def test_parse_rewritten_body_no_headers():
    assert parse_rewritten_body("Just some text, no headers") is None


def test_parse_rewritten_body_empty():
    assert parse_rewritten_body("") is None


# --- Pure function tests: build_decomposition_comment ---


def test_build_decomposition_comment_basic():
    result = {
        "points": 5,
        "decomposition": [
            {
                "order": 1,
                "title": "Add model",
                "points": 2,
                "depends_on": [],
                "files": ["src/model.py"],
                "why_first": "Foundation",
            },
            {
                "order": 2,
                "title": "Add API",
                "points": 3,
                "depends_on": [1],
                "files": ["src/api.py"],
                "why_after": "Needs model",
            },
        ],
    }
    comment = build_decomposition_comment(result)
    assert "5 points" in comment
    assert "Add model" in comment
    assert "Add API" in comment
    assert "Step 1" in comment
    assert "Foundation" in comment


def test_build_decomposition_comment_no_deps():
    result = {
        "points": 3,
        "decomposition": [
            {
                "order": 1,
                "title": "Step A",
                "points": 3,
                "depends_on": [],
                "files": [],
            },
        ],
    }
    comment = build_decomposition_comment(result)
    assert "—" in comment


# --- Pure function tests: build_sub_issue_summary_comment ---


def test_build_sub_issue_summary_comment():
    comment = build_sub_issue_summary_comment(10, [11, 12, 13])
    assert "#10" in comment
    assert "#11" in comment
    assert "#12" in comment
    assert "#13" in comment
    assert "3 sub-issue(s)" in comment


def test_build_sub_issue_summary_comment_single():
    comment = build_sub_issue_summary_comment(5, [6])
    assert "1 sub-issue(s)" in comment


# --- build_triage_prompt uses cfg values ---


def test_build_triage_prompt_uses_max_story_points():
    cfg = _cfg(max_story_points=3)
    prompt = build_triage_prompt(cfg)
    assert "≤3 points" in prompt
    assert ">3 points" in prompt
    assert "≤2 points" not in prompt


def test_build_triage_prompt_default_threshold():
    cfg = _cfg(max_story_points=2)
    prompt = build_triage_prompt(cfg)
    assert "≤2 points" in prompt
    assert ">2 points" in prompt


def test_build_triage_prompt_uses_verify_cmd():
    cfg = _cfg(verify_cmd="make test")
    prompt = build_triage_prompt(cfg)
    assert "make test" in prompt
    assert "uv run pytest" not in prompt


def test_build_triage_prompt_uses_lint_command():
    cfg = _cfg(lint_command="make lint")
    prompt = build_triage_prompt(cfg)
    assert "make lint" in prompt
    assert "uv run ruff" not in prompt


def test_build_triage_prompt_includes_project_commands_section():
    cfg = _cfg()
    prompt = build_triage_prompt(cfg)
    assert "PROJECT COMMANDS:" in prompt
    assert "Test:" in prompt
    assert "Lint:" in prompt


# --- No bare constants at module level ---


def test_no_bare_repo_constant():
    import autoloop.triage_issues as mod

    assert not hasattr(mod, "REPO"), "Module should not have a bare REPO constant"


def test_no_bare_triage_model_constant():
    import autoloop.triage_issues as mod

    assert not hasattr(mod, "TRIAGE_MODEL"), "Module should not have a bare TRIAGE_MODEL constant"


def test_no_bare_triage_labels_constant():
    import autoloop.triage_issues as mod

    assert not hasattr(mod, "TRIAGE_LABELS"), "Module should not have a bare TRIAGE_LABELS constant"


def test_no_bare_triage_prompt_constant():
    import autoloop.triage_issues as mod

    assert not hasattr(mod, "TRIAGE_PROMPT"), (
        "Module should not have a bare TRIAGE_PROMPT constant; use build_triage_prompt(cfg)"
    )


# --- Subprocess functions use cfg.repo ---


def test_list_untriaged_issues_uses_cfg_repo():
    cfg = _cfg(repo="acme/widgets")
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0
        stdout = json.dumps([])

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import list_untriaged_issues

        list_untriaged_issues(cfg)

    assert len(calls) == 1
    assert "--repo" in calls[0]
    assert calls[0][calls[0].index("--repo") + 1] == "acme/widgets"


def test_list_untriaged_issues_filters_by_cfg_triage_labels():
    cfg = _cfg(triage_labels=["custom-label"])
    labeled = [
        {
            "number": 1,
            "title": "A",
            "body": "",
            "labels": [{"name": "custom-label"}],
        },
        {"number": 2, "title": "B", "body": "", "labels": []},
    ]

    class FakeResult:
        returncode = 0
        stdout = json.dumps(labeled)

    with patch("autoloop.triage_issues.subprocess.run", return_value=FakeResult()):
        from autoloop.triage_issues import list_untriaged_issues

        result = list_untriaged_issues(cfg)

    assert len(result) == 1
    assert result[0]["number"] == 2


def test_reject_issue_uses_cfg_repo():
    cfg = _cfg(repo="acme/widgets")
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import reject_issue

        reject_issue(42, "bad issue", cfg)

    assert len(calls) == 2
    for call in calls:
        assert "--repo" in call
        assert call[call.index("--repo") + 1] == "acme/widgets"


def test_approve_issue_uses_cfg_repo():
    cfg = _cfg(repo="acme/widgets")
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import approve_issue

        approve_issue(42, "p1", "looks good", cfg)

    assert len(calls) == 2
    for call in calls:
        assert "--repo" in call
        assert call[call.index("--repo") + 1] == "acme/widgets"


def test_enrich_issue_with_files_uses_cfg_repo():
    cfg = _cfg(repo="acme/widgets")
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import enrich_issue_with_files

        enrich_issue_with_files(42, [{"path": "src/x.py", "reason": "main"}], cfg)

    assert len(calls) == 1
    assert "--repo" in calls[0]
    assert calls[0][calls[0].index("--repo") + 1] == "acme/widgets"


def test_apply_rewrite_uses_cfg_repo():
    cfg = _cfg(repo="acme/widgets")
    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import apply_rewrite

        apply_rewrite(42, "new body", cfg)

    assert len(calls) == 3
    for call in calls:
        assert "--repo" in call
        assert call[call.index("--repo") + 1] == "acme/widgets"


def test_create_sub_issues_uses_cfg_repo(monkeypatch):
    cfg = _cfg(repo="acme/widgets")
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0
        stdout = "https://github.com/acme/widgets/issues/99"

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    result = {
        "decomposition": [
            {
                "order": 1,
                "title": "Step 1",
                "points": 1,
                "depends_on": [],
                "files": ["src/x.py"],
            },
        ],
    }

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import create_sub_issues

        created = create_sub_issues(10, result, cfg)

    assert len(created) == 1
    gh_calls = [c for c in calls if c[0] == "gh"]
    for call in gh_calls:
        assert "--repo" in call
        assert call[call.index("--repo") + 1] == "acme/widgets"


# --- Claude calls use cfg.triage_model ---


def test_evaluate_issue_uses_cfg_triage_model(monkeypatch):
    cfg = _cfg(triage_model="opus")
    captured = {}

    def fake_load():
        return "src/patina/store.py\n", "# CLAUDE.md"

    monkeypatch.setattr("autoloop.triage_issues.load_project_context", fake_load)

    def fake_run_claude(prompt, model, timeout):
        captured["model"] = model
        captured["timeout"] = timeout
        return ClaudeResult(
            json.dumps(
                {
                    "verdict": "ready",
                    "points": 1,
                    "priority": "p1",
                    "reason": "ok",
                }
            ),
            0.01,
            100,
            50,
            0,
            True,
        )

    monkeypatch.setattr("autoloop.triage_issues.run_claude", fake_run_claude)

    from autoloop.triage_issues import evaluate_issue

    issue = {"number": 1, "title": "Test", "body": "body"}
    evaluate_issue(issue, cfg)

    assert captured["model"] == "opus"
    assert captured["timeout"] == 90


def test_evaluate_issue_uses_cfg_triage_timeout(monkeypatch):
    cfg = _cfg(triage_timeout=45)

    def fake_load():
        return "src/patina/store.py\n", "# CLAUDE.md"

    monkeypatch.setattr("autoloop.triage_issues.load_project_context", fake_load)

    captured = {}

    def fake_run_claude(prompt, model, timeout):
        captured["timeout"] = timeout
        return ClaudeResult(
            json.dumps({"verdict": "ready", "points": 1, "priority": "p1", "reason": "ok"}),
            0.01,
            100,
            50,
            0,
            True,
        )

    monkeypatch.setattr("autoloop.triage_issues.run_claude", fake_run_claude)

    from autoloop.triage_issues import evaluate_issue

    evaluate_issue({"number": 1, "title": "T", "body": "b"}, cfg)
    assert captured["timeout"] == 45


def test_evaluate_issue_uses_cfg_tree_truncation(monkeypatch):
    cfg = _cfg(tree_truncation=10)
    long_tree = "a" * 100

    def fake_load():
        return long_tree, "# CLAUDE.md"

    monkeypatch.setattr("autoloop.triage_issues.load_project_context", fake_load)

    captured = {}

    def fake_run_claude(prompt, model, timeout):
        captured["prompt"] = prompt
        return ClaudeResult(
            json.dumps({"verdict": "ready", "points": 1, "priority": "p1", "reason": "ok"}),
            0.01,
            100,
            50,
            0,
            True,
        )

    monkeypatch.setattr("autoloop.triage_issues.run_claude", fake_run_claude)

    from autoloop.triage_issues import evaluate_issue

    evaluate_issue({"number": 1, "title": "T", "body": "b"}, cfg)
    assert "a" * 100 not in captured["prompt"]
    assert "a" * 10 in captured["prompt"]


# --- log_run writes to the correct file ---


def test_log_run_writes_jsonl(tmp_path, monkeypatch):
    log_file = tmp_path / "run_history.jsonl"
    monkeypatch.setattr("autoloop.triage_issues.LOG_FILE", log_file)

    from autoloop.triage_issues import log_run

    log_run(42, True, 1, 10.0, 0.05)

    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["issue"] == 42
    assert entry["success"] is True
