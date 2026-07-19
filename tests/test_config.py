from __future__ import annotations

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
    assert config.lint_command == "uv run ruff check && uv run ruff format --check"
    assert config.max_story_points == 2
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
