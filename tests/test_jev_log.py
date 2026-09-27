import json

import pytest

from autoloop.jev_log import log_decision


def test_log_decision_writes_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = {
        "point": "auto-merge",
        "issue": 42,
        "jev_call": {"meets_acceptance_criteria": 0.9},
        "incumbent_call": {"approved": True, "feedback": "ok"},
        "outcome": "incumbent",
        "ttft": 1.2,
        "cost": 0.003,
        "timestamp": "2026-09-25T00:00:00Z",
    }
    log_decision(record)

    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    assert log_file.exists()
    written = json.loads(log_file.read_text().strip())
    for key in (
        "point",
        "issue",
        "jev_call",
        "incumbent_call",
        "outcome",
        "ttft",
        "cost",
        "timestamp",
    ):
        assert key in written
    assert written["point"] == "auto-merge"
    assert written["issue"] == 42
    assert written["jev_call"] == {"meets_acceptance_criteria": 0.9}
    assert written["incumbent_call"] == {"approved": True, "feedback": "ok"}
    assert written["outcome"] == "incumbent"
    assert written["ttft"] == 1.2
    assert written["cost"] == 0.003
    assert written["timestamp"] == "2026-09-25T00:00:00Z"


def test_log_decision_creates_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    assert not log_file.exists()

    log_decision(
        {
            "point": "triage",
            "issue": 1,
            "jev_call": {"well_formed": 0.9},
            "incumbent_call": {"verdict": "ready"},
            "outcome": "incumbent",
            "ttft": 0.5,
            "cost": 0.001,
            "timestamp": "2026-01-01T00:00:00Z",
        }
    )
    assert log_file.exists()
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1


def test_log_decision_appends_without_overwrite(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record1 = {
        "point": "triage",
        "issue": 1,
        "jev_call": {"well_formed": 0.9},
        "incumbent_call": {"verdict": "ready"},
        "outcome": "incumbent",
        "ttft": 1.0,
        "cost": 0.002,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    record2 = {
        "point": "auto-merge",
        "issue": 2,
        "jev_call": {"meets_acceptance_criteria": 0.8},
        "incumbent_call": {"approved": False, "feedback": "nope"},
        "outcome": "incumbent",
        "ttft": 2.0,
        "cost": 0.004,
        "timestamp": "2026-01-02T00:00:00Z",
    }

    log_decision(record1)
    log_decision(record2)

    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["point"] == "triage"
    assert json.loads(lines[1])["point"] == "auto-merge"


def test_log_decision_rejects_missing_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    incomplete = {"point": "triage", "jev_call": "A"}
    with pytest.raises(ValueError, match="Missing required keys"):
        log_decision(incomplete)
    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    assert not log_file.exists()


def test_log_decision_accepts_optional_pr_field(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = {
        "point": "auto-merge",
        "issue": 10,
        "jev_call": {"meets_acceptance_criteria": 0.95},
        "incumbent_call": {"approved": True, "feedback": "lgtm"},
        "outcome": "incumbent",
        "ttft": 0.3,
        "cost": 0.001,
        "timestamp": "2026-01-01T00:00:00Z",
        "pr": 55,
    }
    log_decision(record)

    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    written = json.loads(log_file.read_text().strip())
    assert written["pr"] == 55
