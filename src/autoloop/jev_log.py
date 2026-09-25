import json
from pathlib import Path

REQUIRED_KEYS = ("point", "jev_call", "incumbent_call", "outcome", "ttft", "cost", "timestamp")


def log_decision(record: dict) -> None:
    log_file = Path.cwd() / "autoloop" / "jev_decisions.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a") as f:
        f.write(json.dumps(record) + "\n")
