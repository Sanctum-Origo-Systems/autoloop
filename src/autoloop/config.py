"""AutoLoop configuration: dataclass + TOML loader with env-var overrides."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def __getattr__(name: str):
    if name == "REPO_DIR":
        return Path.cwd()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class RepoContext:
    repo_dir: Path
    data_dir: Path = field(init=False)
    worktree_dir: Path = field(init=False)

    def __post_init__(self):
        self.repo_dir = Path(self.repo_dir).resolve()
        remote_url = self._get_remote_url()
        hash_input = remote_url if remote_url else str(self.repo_dir)
        digest = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
        self.data_dir = Path.home() / ".autoloop" / digest
        self.worktree_dir = self.data_dir / "worktrees"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._migrate_files()

    def _get_remote_url(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                cwd=self.repo_dir,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except FileNotFoundError:
            pass
        return None

    @classmethod
    def for_data_dir(cls, data_dir: Path, repo_dir: Path | None = None) -> RepoContext:
        ctx = object.__new__(cls)
        ctx.data_dir = Path(data_dir).resolve()
        ctx.repo_dir = Path(repo_dir).resolve() if repo_dir is not None else ctx.data_dir.parent
        ctx.worktree_dir = ctx.data_dir / "worktrees"
        return ctx

    def _migrate_files(self):
        for filename in ("run_history.l", "jev_decisions.l"):
            src = self.repo_dir / filename
            dst = self.data_dir / filename
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)


@dataclass
class AutoLoopConfig:
    repo: str = "Sanctum-Origo-Systems/patina"
    triage_model: str = "sonnet"
    impl_model: str = "claude-opus-4-6[1m]"
    review_model: str = ""
    impl_timeout: int = 900
    triage_timeout: int = 90
    test_timeout: int = 120
    pr_reviewer: str = "andywidjaja"
    max_retries: int = 3
    max_story_points: int = 3
    tree_truncation: int = 3000
    diff_truncation: int = 8000
    error_truncation: int = 2000
    spec_truncation: int = 4000
    verify_cmd: str = "uv run pytest"
    lint_command: str = ""
    test_pattern: str = "tests/*.py"
    timer_prefix: str = "autoloop"
    protected_paths: list[str] = field(default_factory=lambda: ["autoloop/"])
    test_gate_skip_types: list[str] = field(default_factory=lambda: ["refactor", "docs", "chore"])
    triage_labels: list[str] = field(
        default_factory=lambda: [
            "ready",
            "rejected",
            "needs-decomposition",
            "in-progress",
            "in-review",
            "needs-human",
        ]
    )
    max_pr_review_rounds: int = 3
    max_decomposition_depth: int = 2
    auto_merge_edit_rate_threshold: float = 0.05
    auto_merge_success_threshold: float = 0.90
    auto_merge_volume_floor: int = 10
    auto_merge_promotion_level: str = "module"
    auto_merge: bool = False
    jev_mode: str = "off"
    jev_api_key_env: str = "OPENROUTER_API_KEY"
    jev_model: str = "typesafe/jev-1.13"
    jev_timeout_seconds: int = 10
    jev_gate_low: float = 0.15
    jev_gate_high: float = 0.85
    test_integrity_guard: bool = False
    test_integrity_patterns: list[str] = field(
        default_factory=lambda: ["pytest.mark.skip", "pytest.mark.xfail"]
    )
    implement_isolation: str = "off"
    project_dir: str = ""


_ENV_MAP: dict[str, tuple[str, type]] = {
    "AUTOLOOP_TRIAGE_MODEL": ("triage_model", str),
    "AUTOLOOP_IMPL_MODEL": ("impl_model", str),
    "AUTOLOOP_TIMEOUT": ("impl_timeout", int),
    "AUTOLOOP_TRIAGE_TIMEOUT": ("triage_timeout", int),
    "AUTOLOOP_TEST_TIMEOUT": ("test_timeout", int),
    "AUTOLOOP_REVIEWER": ("pr_reviewer", str),
    "AUTOLOOP_MAX_RETRIES": ("max_retries", int),
    "AUTOLOOP_MAX_STORY_POINTS": ("max_story_points", int),
    "AUTOLOOP_REPO": ("repo", str),
}


def load_config(path: Path | None = None) -> AutoLoopConfig:
    """Load config with precedence: env vars > TOML file > dataclass defaults.

    Raises FileNotFoundError when the resolved config path does not exist.
    """
    config_path = path or Path.cwd() / "autoloop.toml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    config = AutoLoopConfig()
    config.project_dir = str(config_path.parent.resolve())

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    for key in (
        "repo",
        "triage_model",
        "impl_model",
        "review_model",
        "pr_reviewer",
        "verify_cmd",
        "lint_command",
        "timer_prefix",
        "test_pattern",
        "jev_api_key_env",
    ):
        if key in data:
            setattr(config, key, data[key])

    for key in (
        "impl_timeout",
        "triage_timeout",
        "test_timeout",
        "max_retries",
        "max_story_points",
        "tree_truncation",
        "diff_truncation",
        "error_truncation",
        "spec_truncation",
        "max_pr_review_rounds",
        "max_decomposition_depth",
    ):
        if key in data:
            setattr(config, key, int(data[key]))

    for key in (
        "auto_merge_edit_rate_threshold",
        "auto_merge_success_threshold",
    ):
        if key in data:
            setattr(config, key, float(data[key]))

    if "auto_merge_volume_floor" in data:
        config.auto_merge_volume_floor = int(data["auto_merge_volume_floor"])

    if "auto_merge" in data:
        config.auto_merge = bool(data["auto_merge"])

    if "auto_merge_promotion_level" in data:
        level = str(data["auto_merge_promotion_level"])
        if level not in ("repo", "module"):
            raise ValueError(
                f"auto_merge_promotion_level must be 'repo' or 'module', got '{level}'"
            )
        config.auto_merge_promotion_level = level

    jev = data.get("jev", {})
    _jev_known_keys = {"mode", "api_key_env", "timeout_seconds", "gate_low", "gate_high"}
    _jev_unexpected = sorted(set(jev) - _jev_known_keys)
    if _jev_unexpected:
        print(
            f"Warning: unexpected key(s) in [jev]: {', '.join(_jev_unexpected)}",
            file=sys.stderr,
        )
    if "mode" in jev:
        mode = str(jev["mode"])
        if mode not in ("off", "shadow", "gate"):
            raise ValueError(f"jev.mode must be 'off', 'shadow', or 'gate', got '{mode}'")
        config.jev_mode = mode
    if "api_key_env" in jev:
        config.jev_api_key_env = str(jev["api_key_env"])
    if "model" in jev:
        config.jev_model = str(jev["model"])
    if "timeout_seconds" in jev:
        config.jev_timeout_seconds = int(jev["timeout_seconds"])
    if "gate_low" in jev:
        config.jev_gate_low = float(jev["gate_low"])
    if "gate_high" in jev:
        config.jev_gate_high = float(jev["gate_high"])
    if "protected_paths" in data:
        config.protected_paths = list(data["protected_paths"])

    if "test_gate_skip_types" in data:
        config.test_gate_skip_types = list(data["test_gate_skip_types"])

    if "test_integrity_guard" in data:
        config.test_integrity_guard = bool(data["test_integrity_guard"])

    if "test_integrity_patterns" in data:
        config.test_integrity_patterns = list(data["test_integrity_patterns"])

    implement = data.get("implement", {})
    if "isolation" in implement:
        isolation = str(implement["isolation"])
        if isolation not in ("off", "worktree"):
            raise ValueError(f"implement.isolation must be 'off' or 'worktree', got '{isolation}'")
        config.implement_isolation = isolation

    if "triage_labels" in data:
        config.triage_labels = list(data["triage_labels"])

    if not config.review_model:
        config.review_model = config.impl_model

    for env_var, (attr, coerce) in _ENV_MAP.items():
        if value := os.environ.get(env_var):
            setattr(config, attr, coerce(value))

    if jev_mode_env := os.environ.get("JEV_MODE"):
        if jev_mode_env not in ("off", "shadow", "gate"):
            raise ValueError(f"jev.mode must be 'off', 'shadow', or 'gate', got '{jev_mode_env}'")
        config.jev_mode = jev_mode_env

    if not config.review_model:
        config.review_model = config.impl_model

    return config


def touches_protected_path(files: list[str], protected: list[str]) -> bool:
    """Return True if any file path starts with a protected prefix."""
    return any(f.startswith(p) for f in files for p in protected)


def verify_implementation(config: AutoLoopConfig) -> int:
    """Run the shell command in *config.verify_cmd* and return its exit code."""
    result = subprocess.run(
        config.verify_cmd,
        shell=True,
        capture_output=True,
    )
    return result.returncode
