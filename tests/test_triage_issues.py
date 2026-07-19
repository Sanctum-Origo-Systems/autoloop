from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from autoloop.claude_runner import ClaudeResult
from autoloop.triage_issues import (
    _merge_steps,
    build_decomposition_comment,
    build_sub_issue_summary_comment,
    build_triage_prompt,
    fetch_issue_body,
    get_decomposition_depth,
    parse_file_discovery_response,
    parse_rewritten_body,
    parse_sub_issue_response,
    parse_triage_response,
    validate_decomposition,
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


# --- _merge_steps ---


def test_merge_steps_combines_files():
    a = {"order": 1, "title": "Add X", "points": 2, "files": ["src/a.py"], "why_first": "base"}
    b = {"order": 3, "title": "Add Y", "points": 1, "files": ["src/b.py"], "why_after": "depends"}
    merged = _merge_steps(a, b)
    assert set(merged["files"]) == {"src/a.py", "src/b.py"}
    assert merged["points"] == 3
    assert merged["title"] == "Add X + Add Y"
    assert merged["order"] == 1
    assert merged["depends_on"] == []


def test_merge_steps_deduplicates_shared_files():
    a = {"order": 1, "title": "A", "points": 1, "files": ["f.py", "g.py"]}
    b = {"order": 2, "title": "B", "points": 1, "files": ["f.py"]}
    merged = _merge_steps(a, b)
    assert merged["files"] == ["f.py", "g.py"]


def test_merge_steps_combines_why():
    a = {"order": 2, "title": "A", "points": 1, "files": [], "why_after": "reason A"}
    b = {"order": 3, "title": "B", "points": 1, "files": [], "why_after": "reason B"}
    merged = _merge_steps(a, b)
    assert "reason A" in merged["why_after"]
    assert "reason B" in merged["why_after"]


# --- validate_decomposition ---


def test_validate_decomposition_empty():
    assert validate_decomposition([]) == []


def test_validate_decomposition_single_step():
    steps = [{"order": 1, "title": "Only step", "points": 3, "files": ["a.py"]}]
    result = validate_decomposition(steps)
    assert len(result) == 1
    assert result[0]["title"] == "Only step"


def test_validate_decomposition_merges_shared_files():
    steps = [
        {"order": 1, "title": "Add fixture", "points": 1, "files": ["tests/conftest.py"]},
        {"order": 2, "title": "Add another fixture", "points": 1, "files": ["tests/conftest.py"]},
        {"order": 3, "title": "Unrelated work", "points": 3, "files": ["src/main.py"]},
    ]
    result = validate_decomposition(steps)
    assert len(result) == 2
    conftest_step = [s for s in result if "tests/conftest.py" in s["files"]][0]
    assert conftest_step["points"] == 2


def test_validate_decomposition_merges_small_steps():
    steps = [
        {"order": 1, "title": "Tiny fix", "points": 1, "files": ["a.py"]},
        {"order": 2, "title": "Normal work", "points": 3, "files": ["b.py"]},
    ]
    result = validate_decomposition(steps)
    assert len(result) == 1
    assert result[0]["points"] == 4


def test_validate_decomposition_caps_at_max():
    steps = [
        {"order": i, "title": f"Step {i}", "points": 2, "files": [f"file{i}.py"]}
        for i in range(1, 21)
    ]
    result = validate_decomposition(steps, max_sub_issues=12)
    assert len(result) <= 12


def test_validate_decomposition_renumbers_orders():
    steps = [
        {"order": 5, "title": "A", "points": 3, "files": ["a.py"], "depends_on": []},
        {"order": 10, "title": "B", "points": 3, "files": ["b.py"], "depends_on": [5]},
    ]
    result = validate_decomposition(steps)
    assert result[0]["order"] == 1
    assert result[1]["order"] == 2
    assert result[0]["depends_on"] == []
    assert result[1]["depends_on"] == [1]


def test_validate_decomposition_already_valid():
    steps = [
        {"order": 1, "title": "Core module", "points": 3, "files": ["src/core.py"]},
        {"order": 2, "title": "API layer", "points": 3, "files": ["src/api.py"]},
        {"order": 3, "title": "Tests", "points": 2, "files": ["tests/test_core.py"]},
    ]
    result = validate_decomposition(steps)
    assert len(result) == 3
    for i, step in enumerate(result, 1):
        assert step["order"] == i


def test_validate_decomposition_transitive_file_merge():
    steps = [
        {"order": 1, "title": "A", "points": 1, "files": ["x.py", "y.py"]},
        {"order": 2, "title": "B", "points": 1, "files": ["y.py", "z.py"]},
        {"order": 3, "title": "C", "points": 1, "files": ["z.py"]},
    ]
    result = validate_decomposition(steps)
    assert len(result) == 1
    assert set(result[0]["files"]) == {"x.py", "y.py", "z.py"}


def test_validate_decomposition_remaps_deps_after_merge():
    """Dependencies should be remapped to the absorbing step's new index."""
    steps = [
        {"order": 1, "title": "Install packages", "points": 1, "files": ["setup.py"]},
        {"order": 2, "title": "Configure deps", "points": 1, "files": ["setup.py"]},
        {
            "order": 3,
            "title": "Add endpoint",
            "points": 3,
            "files": ["src/api.py"],
            "depends_on": [2],
        },
    ]
    result = validate_decomposition(steps)
    assert len(result) == 2
    api_step = [s for s in result if "src/api.py" in s.get("files", [])][0]
    setup_step = [s for s in result if "setup.py" in s.get("files", [])][0]
    assert setup_step["order"] in api_step["depends_on"]


def test_validate_decomposition_drops_self_references():
    """A step should not depend on itself after a merge."""
    steps = [
        {"order": 1, "title": "Base model", "points": 1, "files": ["src/model.py"]},
        {
            "order": 2,
            "title": "Model validation",
            "points": 1,
            "files": ["src/model.py"],
            "depends_on": [1],
        },
        {"order": 3, "title": "API layer", "points": 3, "files": ["src/api.py"], "depends_on": [2]},
    ]
    result = validate_decomposition(steps)
    for step in result:
        assert step["order"] not in step["depends_on"]


def test_validate_decomposition_preserves_chain():
    """A dependency chain (1 -> 2 -> 3) with no merges should be preserved."""
    steps = [
        {
            "order": 1,
            "title": "Schema migration",
            "points": 3,
            "files": ["db/schema.py"],
            "depends_on": [],
        },
        {
            "order": 2,
            "title": "Data migration",
            "points": 3,
            "files": ["db/migrate.py"],
            "depends_on": [1],
        },
        {
            "order": 3,
            "title": "API update",
            "points": 3,
            "files": ["src/api.py"],
            "depends_on": [2],
        },
    ]
    result = validate_decomposition(steps)
    assert len(result) == 3
    assert result[0]["depends_on"] == []
    assert result[1]["depends_on"] == [1]
    assert result[2]["depends_on"] == [2]


def test_validate_decomposition_remaps_multiple_deps():
    """A step depending on multiple others should have all remapped correctly."""
    steps = [
        {"order": 1, "title": "Step A", "points": 3, "files": ["a.py"], "depends_on": []},
        {"order": 2, "title": "Step B", "points": 3, "files": ["b.py"], "depends_on": []},
        {"order": 3, "title": "Step C", "points": 3, "files": ["c.py"], "depends_on": [1, 2]},
    ]
    result = validate_decomposition(steps)
    assert len(result) == 3
    assert result[2]["depends_on"] == [1, 2]


def test_validate_decomposition_no_internal_metadata_leak():
    """The _original_orders tracking field should not appear in the output."""
    steps = [
        {"order": 1, "title": "A", "points": 3, "files": ["a.py"], "depends_on": []},
        {"order": 2, "title": "B", "points": 3, "files": ["b.py"], "depends_on": [1]},
    ]
    result = validate_decomposition(steps)
    for step in result:
        assert "_original_orders" not in step


def test_validate_decomposition_regression_20_criteria():
    """A 20-criterion spec with 5 logical components should produce 5-10 sub-issues."""
    decomposition = [
        # Component 1: Build system (3 criteria, 1 file)
        {
            "order": 1,
            "title": "Add SDK dependency",
            "points": 1,
            "depends_on": [],
            "files": ["pyproject.toml"],
        },
        {
            "order": 2,
            "title": "Update build config",
            "points": 1,
            "depends_on": [],
            "files": ["pyproject.toml"],
        },
        {
            "order": 3,
            "title": "Add dev dependencies",
            "points": 1,
            "depends_on": [],
            "files": ["pyproject.toml"],
        },
        # Component 2: New module (4 criteria, 1 file)
        {
            "order": 4,
            "title": "Add create_agent function",
            "points": 1,
            "depends_on": [],
            "files": ["src/agents.py"],
        },
        {
            "order": 5,
            "title": "Add run_agent function",
            "points": 1,
            "depends_on": [],
            "files": ["src/agents.py"],
        },
        {
            "order": 6,
            "title": "Add stop_agent function",
            "points": 1,
            "depends_on": [],
            "files": ["src/agents.py"],
        },
        {
            "order": 7,
            "title": "Add list_agents function",
            "points": 1,
            "depends_on": [],
            "files": ["src/agents.py"],
        },
        # Component 3: Integration (2 criteria, 2 files with overlap)
        {
            "order": 8,
            "title": "Wire agent pipeline",
            "points": 1,
            "depends_on": [],
            "files": ["src/pipeline.py"],
        },
        {
            "order": 9,
            "title": "Add error handling",
            "points": 1,
            "depends_on": [],
            "files": ["src/pipeline.py", "src/errors.py"],
        },
        # Component 4: Tests (8 criteria, 3 files)
        {
            "order": 10,
            "title": "Add mock_agent fixture",
            "points": 1,
            "depends_on": [],
            "files": ["tests/conftest.py"],
        },
        {
            "order": 11,
            "title": "Add mock_pipeline fixture",
            "points": 1,
            "depends_on": [],
            "files": ["tests/conftest.py"],
        },
        {
            "order": 12,
            "title": "Test create_agent",
            "points": 1,
            "depends_on": [],
            "files": ["tests/test_agents.py"],
        },
        {
            "order": 13,
            "title": "Test run_agent",
            "points": 1,
            "depends_on": [],
            "files": ["tests/test_agents.py"],
        },
        {
            "order": 14,
            "title": "Test stop_agent",
            "points": 1,
            "depends_on": [],
            "files": ["tests/test_agents.py"],
        },
        {
            "order": 15,
            "title": "Test list_agents",
            "points": 1,
            "depends_on": [],
            "files": ["tests/test_agents.py"],
        },
        {
            "order": 16,
            "title": "Test pipeline wiring",
            "points": 1,
            "depends_on": [],
            "files": ["tests/test_pipeline.py"],
        },
        {
            "order": 17,
            "title": "Test error handling",
            "points": 1,
            "depends_on": [],
            "files": ["tests/test_pipeline.py"],
        },
        # Component 5: Cleanup (3 criteria, no shared files)
        {
            "order": 18,
            "title": "Delete legacy agent module",
            "points": 1,
            "depends_on": [],
            "files": ["src/old_agent.py"],
        },
        {
            "order": 19,
            "title": "Delete legacy pipeline",
            "points": 1,
            "depends_on": [],
            "files": ["src/old_pipeline.py"],
        },
        {
            "order": 20,
            "title": "Update imports",
            "points": 1,
            "depends_on": [],
            "files": ["src/main.py"],
        },
    ]
    result = validate_decomposition(decomposition)
    assert len(result) <= 10, f"Expected ≤10 sub-issues, got {len(result)}"
    assert len(result) >= 5, f"Expected ≥5 sub-issues, got {len(result)}"
    for step in result:
        assert step["points"] >= 2, (
            f"Sub-issue '{step['title']}' is too small ({step['points']} pts)"
        )


# --- Decomposition constraints in triage prompt ---


def test_build_triage_prompt_includes_decomposition_constraints():
    cfg = _cfg()
    prompt = build_triage_prompt(cfg)
    assert "DECOMPOSITION CONSTRAINTS" in prompt
    assert "LOGICAL UNIT OF CHANGE" in prompt
    assert "same file" in prompt
    assert "12 sub-issues" in prompt
    assert "2 story points" in prompt


def test_build_triage_prompt_includes_size_calibration():
    cfg = _cfg()
    prompt = build_triage_prompt(cfg)
    assert "Sub-issue size calibration" in prompt
    assert "Too small" in prompt
    assert "Minimum viable" in prompt


def test_build_triage_prompt_includes_self_check():
    cfg = _cfg()
    prompt = build_triage_prompt(cfg)
    assert "self-check" in prompt.lower()
    assert "re-decompose" in prompt


# --- decompose_issue uses validate_decomposition ---


def test_decompose_issue_validates_decomposition(monkeypatch):
    """decompose_issue should consolidate micro-issues before creating sub-issues."""
    cfg = _cfg(repo="acme/widgets")
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    created_titles = []
    calls = []

    class FakeResult:
        returncode = 0
        stdout = "https://github.com/acme/widgets/issues/99"

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        if cmd[1] == "issue" and cmd[2] == "create":
            title_idx = cmd.index("--title") + 1
            created_titles.append(cmd[title_idx])
        return FakeResult()

    result = {
        "points": 5,
        "decomposition": [
            {
                "order": 1,
                "title": "Fix A",
                "points": 1,
                "depends_on": [],
                "files": ["src/x.py"],
                "why_first": "start",
            },
            {
                "order": 2,
                "title": "Fix B",
                "points": 1,
                "depends_on": [],
                "files": ["src/x.py"],
                "why_after": "same file",
            },
            {
                "order": 3,
                "title": "Fix C",
                "points": 3,
                "depends_on": [],
                "files": ["src/y.py"],
                "why_after": "separate",
            },
        ],
    }

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import decompose_issue

        decompose_issue(10, result, cfg, "parent summary")

    assert len(created_titles) == 2
    merged_title = [t for t in created_titles if "Fix A" in t and "Fix B" in t]
    assert len(merged_title) == 1


# --- fetch_issue_body ---


def test_fetch_issue_body_success():
    cfg = _cfg(repo="acme/widgets")

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"body": "Issue body text here"})

    with patch("autoloop.triage_issues.subprocess.run", return_value=FakeResult()):
        result = fetch_issue_body(42, cfg)

    assert result == "Issue body text here"


def test_fetch_issue_body_failure():
    cfg = _cfg(repo="acme/widgets")

    class FakeResult:
        returncode = 1
        stdout = ""

    with patch("autoloop.triage_issues.subprocess.run", return_value=FakeResult()):
        result = fetch_issue_body(42, cfg)

    assert result == ""


def test_fetch_issue_body_null_body():
    cfg = _cfg(repo="acme/widgets")

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"body": None})

    with patch("autoloop.triage_issues.subprocess.run", return_value=FakeResult()):
        result = fetch_issue_body(42, cfg)

    assert result == ""


# --- get_decomposition_depth ---


def test_get_decomposition_depth_root_issue():
    cfg = _cfg()
    issue = {"number": 1, "body": "Just a regular issue body."}
    assert get_decomposition_depth(issue, cfg) == 0


def test_get_decomposition_depth_direct_child():
    cfg = _cfg()
    issue = {"number": 2, "body": "Sub-issue of #1. Some details."}

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"body": "Root issue with no parent reference."})

    with patch("autoloop.triage_issues.subprocess.run", return_value=FakeResult()):
        assert get_decomposition_depth(issue, cfg) == 1


def test_get_decomposition_depth_grandchild():
    cfg = _cfg()
    issue = {"number": 3, "body": "Sub-issue of #2. More details."}

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"body": "Sub-issue of #1. This is a child."})

    with patch("autoloop.triage_issues.subprocess.run", return_value=FakeResult()):
        assert get_decomposition_depth(issue, cfg) == 2


def test_get_decomposition_depth_no_body():
    cfg = _cfg()
    issue = {"number": 1, "body": None}
    assert get_decomposition_depth(issue, cfg) == 0


# --- triage_issue caps depth ---


def test_triage_issue_caps_depth_2_routes_to_ready(monkeypatch):
    """A depth-2 sub-issue with needs-decomposition verdict should be approved instead."""
    cfg = _cfg()

    def fake_load():
        return "src/module.py\n", "# CLAUDE.md"

    monkeypatch.setattr("autoloop.triage_issues.load_project_context", fake_load)

    def fake_run_claude(prompt, model, timeout):
        return ClaudeResult(
            json.dumps(
                {
                    "verdict": "needs-decomposition",
                    "points": 8,
                    "priority": "p1",
                    "reason": "large issue",
                    "decomposition": [
                        {"order": 1, "title": "Part A", "points": 4, "depends_on": [], "files": []},
                        {"order": 2, "title": "Part B", "points": 4, "depends_on": [], "files": []},
                    ],
                }
            ),
            0.01,
            100,
            50,
            0,
            True,
        )

    monkeypatch.setattr("autoloop.triage_issues.run_claude", fake_run_claude)

    def fake_get_depth(issue, cfg):
        return 2

    monkeypatch.setattr("autoloop.triage_issues.get_decomposition_depth", fake_get_depth)

    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import triage_issue

        triage_issue({"number": 5, "title": "Test", "body": "Sub-issue of #3."}, cfg)

    label_calls = [c for c in calls if "edit" in c and "--add-label" in c]
    assert any("ready" in c[c.index("--add-label") + 1] for c in label_calls)
    create_calls = [c for c in calls if "create" in c]
    assert len(create_calls) == 0


def test_triage_issue_caps_depth_1_small_points_routes_to_ready(monkeypatch):
    """A depth-1 sub-issue with <=5 points should be approved instead of decomposed."""
    cfg = _cfg()

    def fake_load():
        return "src/module.py\n", "# CLAUDE.md"

    monkeypatch.setattr("autoloop.triage_issues.load_project_context", fake_load)

    def fake_run_claude(prompt, model, timeout):
        return ClaudeResult(
            json.dumps(
                {
                    "verdict": "needs-decomposition",
                    "points": 5,
                    "priority": "p1",
                    "reason": "medium issue",
                    "decomposition": [
                        {"order": 1, "title": "Part A", "points": 3, "depends_on": [], "files": []},
                        {"order": 2, "title": "Part B", "points": 2, "depends_on": [], "files": []},
                    ],
                }
            ),
            0.01,
            100,
            50,
            0,
            True,
        )

    monkeypatch.setattr("autoloop.triage_issues.run_claude", fake_run_claude)

    def fake_get_depth(issue, cfg):
        return 1

    monkeypatch.setattr("autoloop.triage_issues.get_decomposition_depth", fake_get_depth)

    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import triage_issue

        triage_issue({"number": 6, "title": "Test", "body": "Sub-issue of #1."}, cfg)

    label_calls = [c for c in calls if "edit" in c and "--add-label" in c]
    assert any("ready" in c[c.index("--add-label") + 1] for c in label_calls)
    create_calls = [c for c in calls if "create" in c]
    assert len(create_calls) == 0


def test_triage_issue_allows_decomposition_depth_0(monkeypatch):
    """A root issue (depth 0) should still be decomposed normally."""
    cfg = _cfg()

    def fake_load():
        return "src/module.py\n", "# CLAUDE.md"

    monkeypatch.setattr("autoloop.triage_issues.load_project_context", fake_load)

    def fake_run_claude(prompt, model, timeout):
        return ClaudeResult(
            json.dumps(
                {
                    "verdict": "needs-decomposition",
                    "points": 8,
                    "priority": "p1",
                    "reason": "large issue",
                    "decomposition": [
                        {"order": 1, "title": "Part A", "points": 4, "depends_on": [], "files": []},
                        {"order": 2, "title": "Part B", "points": 4, "depends_on": [], "files": []},
                    ],
                }
            ),
            0.01,
            100,
            50,
            0,
            True,
        )

    monkeypatch.setattr("autoloop.triage_issues.run_claude", fake_run_claude)

    def fake_get_depth(issue, cfg):
        return 0

    monkeypatch.setattr("autoloop.triage_issues.get_decomposition_depth", fake_get_depth)
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    calls: list[list[str]] = []

    class FakeResult:
        returncode = 0
        stdout = "https://github.com/acme/widgets/issues/99"

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return FakeResult()

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import triage_issue

        triage_issue({"number": 7, "title": "Test", "body": "Root issue."}, cfg)

    label_calls = [c for c in calls if "edit" in c and "--add-label" in c]
    assert any("needs-decomposition" in c[c.index("--add-label") + 1] for c in label_calls)


# --- decompose_issue closes parent ---


def test_decompose_issue_closes_parent_after_children(monkeypatch):
    """decompose_issue should close the parent issue after filing sub-issues."""
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
        "points": 5,
        "decomposition": [
            {
                "order": 1,
                "title": "Step 1",
                "points": 3,
                "depends_on": [],
                "files": ["src/a.py"],
            },
            {
                "order": 2,
                "title": "Step 2",
                "points": 2,
                "depends_on": [],
                "files": ["src/b.py"],
            },
        ],
    }

    with patch("autoloop.triage_issues.subprocess.run", side_effect=fake_run):
        from autoloop.triage_issues import decompose_issue

        decompose_issue(10, result, cfg, "parent summary")

    close_calls = [c for c in calls if c[0] == "gh" and "close" in c]
    assert len(close_calls) == 1
    close_cmd = close_calls[0]
    assert "10" in close_cmd
    assert "--repo" in close_cmd
    assert close_cmd[close_cmd.index("--repo") + 1] == "acme/widgets"
    comment_idx = close_cmd.index("--comment") + 1
    assert "Decomposed into" in close_cmd[comment_idx]
    assert "sub-issues" in close_cmd[comment_idx]
