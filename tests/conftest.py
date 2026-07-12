from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def autoloop_toml(tmp_path: Path) -> Path:
    """Write a minimal autoloop.toml and return its path."""
    toml_path = tmp_path / "autoloop.toml"
    toml_path.write_text(
        'repo = "acme-corp/widget"\n'
        'triage_model = "haiku"\n'
        'impl_model = "opus"\n'
        "impl_timeout = 600\n"
        "triage_timeout = 45\n"
        "test_timeout = 60\n"
        'pr_reviewer = "review-bot"\n'
        "max_retries = 5\n"
        "tree_truncation = 2000\n"
        "diff_truncation = 6000\n"
        "error_truncation = 1500\n"
        "spec_truncation = 3000\n"
        'verify_cmd = "echo ok"\n'
        'lint_command = "echo lint"\n'
        "max_story_points = 3\n"
        'triage_labels = ["ready", "blocked"]\n'
    )
    return toml_path
