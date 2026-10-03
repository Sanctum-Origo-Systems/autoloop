from __future__ import annotations

import json

import pytest

import autoloop.review_queue as review_queue
from autoloop.config import AutoLoopConfig
from autoloop.review_queue import (
    _parse_depends_on,
    fetch_ci_status,
    fetch_gate_flags,
    find_review_queue_issue,
    format_summary,
    order_by_dependencies,
    post_batch_summary,
)


def _test_cfg(**overrides):
    defaults = {
        "repo": "acme-corp/widget",
    }
    defaults.update(overrides)
    return AutoLoopConfig(**defaults)


def _make_result(returncode=0, stdout="", stderr=""):
    return type("R", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()


# --- find_review_queue_issue ---


def test_find_review_queue_issue_returns_number(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(stdout=json.dumps([{"number": 42}]))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    assert find_review_queue_issue(cfg) == 42


def test_find_review_queue_issue_uses_cfg_repo(monkeypatch):
    cfg = _test_cfg(repo="org/repo")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _make_result(stdout=json.dumps([{"number": 1}]))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    find_review_queue_issue(cfg)
    assert "--repo" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "org/repo"


def test_find_review_queue_issue_raises_when_none(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(stdout="[]")

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="No open issue"):
        find_review_queue_issue(cfg)


def test_find_review_queue_issue_raises_on_gh_failure(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(returncode=1, stderr="auth required")

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Failed to list"):
        find_review_queue_issue(cfg)


# --- fetch_ci_status ---


def test_fetch_ci_status_pass(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(stdout=json.dumps([{"bucket": "pass"}]))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    assert fetch_ci_status(10, cfg) == "pass"


def test_fetch_ci_status_fail(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(stdout=json.dumps([{"bucket": "pass"}, {"bucket": "fail"}]))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    assert fetch_ci_status(10, cfg) == "fail"


def test_fetch_ci_status_pending(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(stdout=json.dumps([{"bucket": "pass"}, {"bucket": "pending"}]))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    assert fetch_ci_status(10, cfg) == "pending"


def test_fetch_ci_status_no_checks_returns_pass(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(stdout="[]")

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    assert fetch_ci_status(10, cfg) == "pass"


def test_fetch_ci_status_gh_failure_returns_pending(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(returncode=1)

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    assert fetch_ci_status(10, cfg) == "pending"


# --- fetch_gate_flags ---


def test_fetch_gate_flags_both_present(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        labels = [{"name": "owner-approved"}, {"name": "needs-human"}, {"name": "ready"}]
        return _make_result(stdout=json.dumps({"labels": labels}))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    flags = fetch_gate_flags(5, cfg)
    assert flags["owner_approved"] is True
    assert flags["needs_human"] is True


def test_fetch_gate_flags_none_present(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(stdout=json.dumps({"labels": [{"name": "ready"}]}))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    flags = fetch_gate_flags(5, cfg)
    assert flags["owner_approved"] is False
    assert flags["needs_human"] is False


def test_fetch_gate_flags_gh_failure(monkeypatch):
    cfg = _test_cfg()

    def fake_run(cmd, **kw):
        return _make_result(returncode=1)

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    flags = fetch_gate_flags(5, cfg)
    assert flags == {"owner_approved": False, "needs_human": False}


# --- _parse_depends_on ---


def test_parse_depends_on_single():
    assert _parse_depends_on("Depends on: #43") == [43]


def test_parse_depends_on_multiple():
    body = "Depends on: #10\nSome text\nDepends on #20"
    assert _parse_depends_on(body) == [10, 20]


def test_parse_depends_on_none():
    assert _parse_depends_on("No deps here") == []


# --- order_by_dependencies ---


def test_order_by_dependencies_no_deps(monkeypatch):
    cfg = _test_cfg()
    prs = [
        {"pr_number": 1, "issue_number": 10, "title": "A"},
        {"pr_number": 2, "issue_number": 20, "title": "B"},
    ]

    def fake_run(cmd, **kw):
        return _make_result(stdout=json.dumps({"body": "No deps"}))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    ordered = order_by_dependencies(prs, cfg)
    assert [p["issue_number"] for p in ordered] == [10, 20]


def test_order_by_dependencies_respects_deps(monkeypatch):
    cfg = _test_cfg()
    prs = [
        {"pr_number": 2, "issue_number": 20, "title": "B"},
        {"pr_number": 1, "issue_number": 10, "title": "A"},
    ]

    bodies = {
        "20": json.dumps({"body": "Depends on: #10"}),
        "10": json.dumps({"body": "No deps"}),
    }

    def fake_run(cmd, **kw):
        for i, arg in enumerate(cmd):
            if arg == "view":
                issue_num = cmd[i + 1]
                return _make_result(stdout=bodies[issue_num])
        return _make_result(stdout=json.dumps({"body": ""}))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    ordered = order_by_dependencies(prs, cfg)
    issues = [p["issue_number"] for p in ordered]
    assert issues.index(10) < issues.index(20)


def test_order_by_dependencies_empty():
    cfg = _test_cfg()
    assert order_by_dependencies([], cfg) == []


# --- format_summary ---


def test_format_summary_table_header():
    prs = [
        {
            "pr_number": 1,
            "issue_number": 10,
            "title": "Add feature",
            "ci": "pass",
            "gate": {"owner_approved": True, "needs_human": False},
        },
    ]
    result = format_summary(prs)
    assert "| Order | PR | Issue | Title | CI | Gate |" in result
    assert "| 1 | #1 | #10 | Add feature | pass | owner-approved |" in result


def test_format_summary_multiple_rows():
    prs = [
        {
            "pr_number": 1,
            "issue_number": 10,
            "title": "A",
            "ci": "pass",
            "gate": {},
        },
        {
            "pr_number": 2,
            "issue_number": 20,
            "title": "B",
            "ci": "fail",
            "gate": {"needs_human": True},
        },
    ]
    result = format_summary(prs)
    lines = result.strip().split("\n")
    assert len(lines) == 4  # header + separator + 2 rows
    assert "| 1 |" in lines[2]
    assert "| 2 |" in lines[3]


def test_format_summary_no_gate_shows_dash():
    prs = [
        {
            "pr_number": 1,
            "issue_number": 10,
            "title": "A",
            "ci": "pass",
            "gate": {},
        },
    ]
    result = format_summary(prs)
    assert "| - |" in result


# --- post_batch_summary ---


def test_post_batch_summary_empty_returns_none():
    cfg = _test_cfg()
    assert post_batch_summary([], cfg) is None


def test_post_batch_summary_empty_no_gh_calls(monkeypatch):
    cfg = _test_cfg()
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _make_result()

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    post_batch_summary([], cfg)
    assert calls == []


def test_post_batch_summary_posts_comment(monkeypatch):
    cfg = _test_cfg()
    prs = [
        {"pr_number": 5, "issue_number": 15, "title": "Fix widget"},
    ]
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "issue" in cmd and "list" in cmd:
            return _make_result(stdout=json.dumps([{"number": 99}]))
        if "pr" in cmd and "checks" in cmd:
            return _make_result(stdout=json.dumps([{"bucket": "pass"}]))
        if "issue" in cmd and "view" in cmd:
            if "labels" in cmd:
                return _make_result(stdout=json.dumps({"labels": []}))
            return _make_result(stdout=json.dumps({"body": ""}))
        return _make_result()

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    result = post_batch_summary(prs, cfg)
    assert result is None

    comment_calls = [c for c in calls if "comment" in c]
    assert len(comment_calls) == 1
    assert "99" in comment_calls[0]


def test_post_batch_summary_comment_contains_table(monkeypatch):
    cfg = _test_cfg()
    prs = [
        {"pr_number": 5, "issue_number": 15, "title": "Fix widget"},
    ]
    posted_body = {}

    def fake_run(cmd, **kw):
        if "issue" in cmd and "list" in cmd:
            return _make_result(stdout=json.dumps([{"number": 99}]))
        if "pr" in cmd and "checks" in cmd:
            return _make_result(stdout=json.dumps([{"bucket": "pass"}]))
        if "issue" in cmd and "view" in cmd:
            if "labels" in cmd:
                return _make_result(stdout=json.dumps({"labels": []}))
            return _make_result(stdout=json.dumps({"body": ""}))
        if "comment" in cmd:
            body_idx = cmd.index("--body") + 1
            posted_body["body"] = cmd[body_idx]
        return _make_result()

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    post_batch_summary(prs, cfg)

    body = posted_body["body"]
    assert "| Order | PR | Issue | Title | CI | Gate |" in body
    assert "#5" in body
    assert "#15" in body
