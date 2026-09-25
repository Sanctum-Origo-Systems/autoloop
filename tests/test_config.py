from __future__ import annotations

import ast

import pytest
from autoloop.config import (
    AutoLoopConfig,
    load_config,
    touches_protected_path,
    verify_implementation,
)


def test_load_from_toml(autoloop_toml, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_TRIAGE_TIMEOUT",
        "AUTOLOOP_TEST_TIMEOUT",
        "AUTOLOOP_MAX_RETRIES",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)

    config = load_config(autoloop_toml)

    assert config.repo == "acme-corp/widget"
    assert config.triage_model == "haiku"
    assert config.impl_model == "opus"
    assert config.impl_timeout == 600
    assert config.triage_timeout == 45
    assert config.test_timeout == 60
    assert config.pr_reviewer == "review-bot"
    assert config.max_retries == 5
    assert config.tree_truncation == 2000
    assert config.diff_truncation == 6000
    assert config.error_truncation == 1500
    assert config.spec_truncation == 3000
    assert config.verify_cmd == "echo ok"
    assert config.lint_command == "echo lint"
    assert config.max_story_points == 3
    assert config.triage_labels == ["ready", "blocked"]


def test_load_config_sets_project_dir_from_config_path(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)

    subdir = tmp_path / "nested" / "project"
    subdir.mkdir(parents=True)
    toml_path = subdir / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')

    config = load_config(toml_path)
    assert config.project_dir == str(subdir.resolve())


def test_partial_toml_keeps_defaults(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)

    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('triage_model = "haiku"\n')

    config = load_config(toml_path)

    assert config.triage_model == "haiku"
    assert config.impl_model == "claude-opus-4-6[1m]"
    assert config.impl_timeout == 900
    assert config.verify_cmd == "uv run pytest"
    assert config.lint_command == ""
    assert config.max_story_points == 3
    assert len(config.triage_labels) == 6


def test_env_var_overrides_toml(autoloop_toml, monkeypatch):
    monkeypatch.setenv("AUTOLOOP_TRIAGE_MODEL", "sonnet")
    monkeypatch.setenv("AUTOLOOP_TIMEOUT", "1200")
    monkeypatch.delenv("AUTOLOOP_IMPL_MODEL", raising=False)
    monkeypatch.delenv("AUTOLOOP_REVIEWER", raising=False)
    monkeypatch.delenv("AUTOLOOP_TRIAGE_TIMEOUT", raising=False)
    monkeypatch.delenv("AUTOLOOP_TEST_TIMEOUT", raising=False)
    monkeypatch.delenv("AUTOLOOP_MAX_RETRIES", raising=False)
    monkeypatch.delenv("AUTOLOOP_REPO", raising=False)

    config = load_config(autoloop_toml)

    assert config.triage_model == "sonnet"
    assert config.impl_timeout == 1200
    assert config.impl_model == "opus"


def test_env_var_overrides_all_mapped_fields(autoloop_toml, monkeypatch):
    monkeypatch.setenv("AUTOLOOP_TRIAGE_MODEL", "env-triage")
    monkeypatch.setenv("AUTOLOOP_IMPL_MODEL", "env-impl")
    monkeypatch.setenv("AUTOLOOP_TIMEOUT", "999")
    monkeypatch.setenv("AUTOLOOP_TRIAGE_TIMEOUT", "30")
    monkeypatch.setenv("AUTOLOOP_TEST_TIMEOUT", "15")
    monkeypatch.setenv("AUTOLOOP_REVIEWER", "env-reviewer")
    monkeypatch.setenv("AUTOLOOP_MAX_RETRIES", "7")
    monkeypatch.setenv("AUTOLOOP_MAX_STORY_POINTS", "5")
    monkeypatch.setenv("AUTOLOOP_REPO", "env-org/env-repo")

    config = load_config(autoloop_toml)

    assert config.triage_model == "env-triage"
    assert config.impl_model == "env-impl"
    assert config.impl_timeout == 999
    assert config.triage_timeout == 30
    assert config.test_timeout == 15
    assert config.pr_reviewer == "env-reviewer"
    assert config.max_retries == 7
    assert config.max_story_points == 5
    assert config.repo == "env-org/env-repo"


def test_missing_toml_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config(tmp_path / "nonexistent.toml")


def test_missing_toml_error_includes_path(tmp_path):
    missing = tmp_path / "absent.toml"
    with pytest.raises(FileNotFoundError, match=str(missing)):
        load_config(missing)


def test_verify_implementation_passing_command():
    config = AutoLoopConfig(verify_cmd="true")
    assert verify_implementation(config) == 0


def test_verify_implementation_failing_command():
    config = AutoLoopConfig(verify_cmd="false")
    result = verify_implementation(config)
    assert result != 0


def test_verify_implementation_specific_exit_code():
    config = AutoLoopConfig(verify_cmd="exit 42")
    assert verify_implementation(config) == 42


def test_verify_implementation_uses_config_verify_cmd():
    config = AutoLoopConfig(verify_cmd="echo hello")
    assert verify_implementation(config) == 0


def test_touches_protected_path_match():
    assert touches_protected_path(["autoloop/config.py"], ["autoloop/"]) is True


def test_touches_protected_path_no_match():
    assert touches_protected_path(["src/patina/cli.py"], ["autoloop/"]) is False


def test_touches_protected_path_exact_file():
    assert touches_protected_path(["autoloop.toml"], ["autoloop.toml"]) is True


def test_touches_protected_path_empty_files():
    assert touches_protected_path([], ["autoloop/"]) is False


def test_touches_protected_path_multiple_protected():
    assert touches_protected_path(["scripts/demo.py"], ["autoloop/", "autoloop.toml"]) is False
    assert (
        touches_protected_path(["autoloop.toml", "src/main.py"], ["autoloop/", "autoloop.toml"])
        is True
    )


def test_lint_command_defaults_to_empty():
    config = AutoLoopConfig()
    assert config.lint_command == ""


def test_lint_command_loaded_from_toml(autoloop_toml, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
        "AUTOLOOP_TRIAGE_TIMEOUT",
        "AUTOLOOP_TEST_TIMEOUT",
        "AUTOLOOP_MAX_RETRIES",
        "AUTOLOOP_REPO",
    ):
        monkeypatch.delenv(var, raising=False)
    config = load_config(autoloop_toml)
    assert config.lint_command == "echo lint"


def test_test_pattern_default():
    config = AutoLoopConfig()
    assert config.test_pattern == "tests/*.py"


def test_test_pattern_loaded_from_toml(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('test_pattern = "src/**/*.test.ts"\n')
    config = load_config(toml_path)
    assert config.test_pattern == "src/**/*.test.ts"


def test_test_pattern_empty_from_toml(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('test_pattern = ""\n')
    config = load_config(toml_path)
    assert config.test_pattern == ""


def test_review_model_defaults_to_impl_model(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('impl_model = "claude-sonnet-4-6"\n')
    config = load_config(toml_path)
    assert config.review_model == "claude-sonnet-4-6"


def test_review_model_explicit_value_preserved(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('impl_model = "opus"\nreview_model = "sonnet"\n')
    config = load_config(toml_path)
    assert config.review_model == "sonnet"
    assert config.impl_model == "opus"


def test_review_model_defaults_to_dataclass_impl_model(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')
    config = load_config(toml_path)
    assert config.review_model == config.impl_model
    assert config.review_model == "claude-opus-4-6[1m]"


def test_test_gate_skip_types_default():
    config = AutoLoopConfig()
    assert config.test_gate_skip_types == ["refactor", "docs", "chore"]


def test_test_gate_skip_types_custom_from_toml(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('test_gate_skip_types = ["docs"]\n')
    config = load_config(toml_path)
    assert config.test_gate_skip_types == ["docs"]


def test_protected_paths_default():
    config = AutoLoopConfig()
    assert config.protected_paths == ["autoloop/"]


def test_protected_paths_loaded_from_toml(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('protected_paths = ["autoloop/", "autoloop.toml", ".github/"]\n')
    config = load_config(toml_path)
    assert config.protected_paths == ["autoloop/", "autoloop.toml", ".github/"]


def test_max_pr_review_rounds_default():
    config = AutoLoopConfig()
    assert config.max_pr_review_rounds == 3


def test_max_pr_review_rounds_loaded_from_toml(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text("max_pr_review_rounds = 7\n")
    config = load_config(toml_path)
    assert config.max_pr_review_rounds == 7


def test_max_pr_review_rounds_omitted_uses_default(tmp_path, monkeypatch):
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')
    config = load_config(toml_path)
    assert config.max_pr_review_rounds == 3


def test_no_default_config_path_at_module_scope():
    """DEFAULT_CONFIG_PATH must not exist as a module-level constant in config.py."""
    import autoloop.config as mod

    source = ast.parse(open(mod.__file__).read())
    names = {
        target.id
        for node in ast.walk(source)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "DEFAULT_CONFIG_PATH" not in names


def test_no_repo_dir_assigned_at_module_scope():
    """REPO_DIR must not be assigned via Path.cwd() at module scope."""
    import autoloop.config as mod

    source = ast.parse(open(mod.__file__).read())
    names = {
        target.id
        for node in ast.walk(source)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "REPO_DIR" not in names


def test_load_config_resolves_from_cwd(tmp_path, monkeypatch):
    """load_config() without a path loads autoloop.toml from the current directory."""
    for var in (
        "AUTOLOOP_TRIAGE_MODEL",
        "AUTOLOOP_IMPL_MODEL",
        "AUTOLOOP_TIMEOUT",
        "AUTOLOOP_REVIEWER",
    ):
        monkeypatch.delenv(var, raising=False)
    toml = tmp_path / "autoloop.toml"
    toml.write_text('repo = "cwd-org/cwd-repo"\n')
    monkeypatch.chdir(tmp_path)

    config = load_config()
    assert config.repo == "cwd-org/cwd-repo"
    assert config.project_dir == str(tmp_path.resolve())


def test_load_config_no_path_missing_toml(tmp_path, monkeypatch):
    """load_config() raises FileNotFoundError when cwd has no autoloop.toml."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError, match="Config file not found"):
        load_config()


# --- auto-merge gate config fields ---


def test_auto_merge_success_threshold_default():
    config = AutoLoopConfig()
    assert config.auto_merge_success_threshold == 0.90


def test_auto_merge_volume_floor_default():
    config = AutoLoopConfig()
    assert config.auto_merge_volume_floor == 10


def test_auto_merge_promotion_level_default():
    config = AutoLoopConfig()
    assert config.auto_merge_promotion_level == "module"


def test_auto_merge_success_threshold_from_toml(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text("auto_merge_success_threshold = 0.95\n")
    config = load_config(toml_path)
    assert config.auto_merge_success_threshold == 0.95


def test_auto_merge_volume_floor_from_toml(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text("auto_merge_volume_floor = 25\n")
    config = load_config(toml_path)
    assert config.auto_merge_volume_floor == 25


def test_auto_merge_promotion_level_repo(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('auto_merge_promotion_level = "repo"\n')
    config = load_config(toml_path)
    assert config.auto_merge_promotion_level == "repo"


def test_auto_merge_promotion_level_invalid(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('auto_merge_promotion_level = "invalid"\n')
    with pytest.raises(ValueError, match="must be 'repo' or 'module'"):
        load_config(toml_path)


# --- jev config block ---


def test_jev_defaults():
    config = AutoLoopConfig()
    assert config.jev_mode == "off"
    assert config.jev_api_key_env == "AI_GATEWAY_API_KEY"
    assert config.jev_timeout_seconds == 10
    assert config.jev_gate_low == 0.15
    assert config.jev_gate_high == 0.85
    assert config.jev_endpoint == ""


def test_jev_absent_block_yields_defaults(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT", "JEV_MODE"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')
    config = load_config(toml_path)
    assert config.jev_mode == "off"
    assert config.jev_api_key_env == "AI_GATEWAY_API_KEY"
    assert config.jev_timeout_seconds == 10
    assert config.jev_gate_low == 0.15
    assert config.jev_gate_high == 0.85
    assert config.jev_endpoint == ""


def test_jev_full_block_from_toml(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT", "JEV_MODE"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text(
        'repo = "acme-corp/widget"\n'
        "\n"
        "[jev]\n"
        'mode = "shadow"\n'
        'api_key_env = "CUSTOM_KEY"\n'
        "timeout_seconds = 30\n"
        "gate_low = 0.20\n"
        "gate_high = 0.90\n"
        'endpoint = "https://api.example.com/jev"\n'
    )
    config = load_config(toml_path)
    assert config.jev_mode == "shadow"
    assert config.jev_api_key_env == "CUSTOM_KEY"
    assert config.jev_timeout_seconds == 30
    assert config.jev_gate_low == 0.20
    assert config.jev_gate_high == 0.90
    assert config.jev_endpoint == "https://api.example.com/jev"


def test_jev_endpoint_loaded_from_toml(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('[jev]\nendpoint = "https://api.example.com/jev"\n')
    config = load_config(toml_path)
    assert config.jev_endpoint == "https://api.example.com/jev"


def test_jev_mode_gate_valid(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT", "JEV_MODE"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('[jev]\nmode = "gate"\n')
    config = load_config(toml_path)
    assert config.jev_mode == "gate"


def test_jev_mode_invalid_raises(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT", "JEV_MODE"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('[jev]\nmode = "auto"\n')
    with pytest.raises(ValueError, match="got 'auto'"):
        load_config(toml_path)


def test_jev_mode_invalid_lists_accepted(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT", "JEV_MODE"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('[jev]\nmode = "bogus"\n')
    with pytest.raises(ValueError, match="'off', 'shadow', or 'gate'"):
        load_config(toml_path)


def test_jev_mode_env_var_override(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JEV_MODE", "gate")
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('[jev]\nmode = "off"\n')
    config = load_config(toml_path)
    assert config.jev_mode == "gate"


def test_jev_mode_env_var_override_no_toml_block(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JEV_MODE", "shadow")
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')
    config = load_config(toml_path)
    assert config.jev_mode == "shadow"


def test_jev_mode_env_var_invalid_raises(tmp_path, monkeypatch):
    for var in ("AUTOLOOP_TRIAGE_MODEL", "AUTOLOOP_IMPL_MODEL", "AUTOLOOP_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JEV_MODE", "auto")
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text('repo = "acme-corp/widget"\n')
    with pytest.raises(ValueError, match="got 'auto'"):
        load_config(toml_path)
