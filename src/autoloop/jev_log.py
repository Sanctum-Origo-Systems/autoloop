from __future__ import annotations

import json

from autoloop.config import RepoContext

REQUIRED_KEYS = (
    "point",
    "issue",
    "jev_call",
    "incumbent_call",
    "outcome",
    "ttft",
    "cost",
    "timestamp",
)


def log_decision(record: dict, ctx: RepoContext) -> None:
    missing = [k for k in REQUIRED_KEYS if k not in record]
    if missing:
        raise ValueError(f"Missing required keys: {', '.join(missing)}")
    log_file = ctx.data_dir / "jev_decisions.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as f:
        f.write(json.dumps(record) + "\n")
