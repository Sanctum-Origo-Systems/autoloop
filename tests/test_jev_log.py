import json

import pytest

from autoloop.jev_log import log_decision


def test_log_decision_writes_record(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    record = {
        "point": "p1",
        "jev_call": "A",
        "incumbent_call": "B",
        "outcome": "jev_wins",
        "ttft": 1.2,
        "cost": 0.003,
        "timestamp": "2026-09-25T00:00:00Z",
    }
    log_decision(record)

    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    assert log_file.exists()
    written = json.loads(log_file.read_text().strip())
    for key in ("point", "jev_call", "incumbent_call", "outcome", "ttft", "cost", "timestamp"):
        assert key in written
    assert written["point"] == "p1"
    assert written["jev_call"] == "A"
    assert written["incumbent_call"] == "B"
    assert written["outcome"] == "jev_wins"
    assert written["ttft"] == 1.2
    assert written["cost"] == 0.003
    assert written["timestamp"] == "2026-09-25T00:00:00Z"


def test_log_decision_creates_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    assert not log_file.exists()

    log_decision(
        {
            "point": "x",
            "jev_call": "A",
            "incumbent_call": "B",
            "outcome": "tie",
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
        "point": "p1",
        "jev_call": "A",
        "incumbent_call": "B",
        "outcome": "jev_wins",
        "ttft": 1.0,
        "cost": 0.002,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    record2 = {
        "point": "p2",
        "jev_call": "C",
        "incumbent_call": "D",
        "outcome": "incumbent_wins",
        "ttft": 2.0,
        "cost": 0.004,
        "timestamp": "2026-01-02T00:00:00Z",
    }

    log_decision(record1)
    log_decision(record2)

    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["point"] == "p1"
    assert json.loads(lines[1])["point"] == "p2"


def test_log_decision_rejects_missing_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    incomplete = {"point": "p1", "jev_call": "A"}
    with pytest.raises(ValueError, match="Missing required keys"):
        log_decision(incomplete)
    log_file = tmp_path / "autoloop" / "jev_decisions.jsonl"
    assert not log_file.exists()
