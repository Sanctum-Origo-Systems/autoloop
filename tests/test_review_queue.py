from __future__ import annotations

import json
import re

import pytest

import autoloop.review_queue as review_queue
from autoloop.config import AutoLoopConfig
from autoloop.review_queue import (
    build_queue_body,
    escape_cell,
    fetch_ci_status,
    fetch_gate_flags,
    find_overlaps,
    find_review_queue_issue,
    format_done_table,
    format_open_table,
    format_summary,
    order_by_dependencies,
    parse_closes_number,
    parse_dependency_numbers,
    post_batch_summary,
    priority_rank,
    read_section,
    refresh_review_queue,
    resolve_mergeable,
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


# --- parse_dependency_numbers ---


def test_parse_depends_on_single():
    assert parse_dependency_numbers("Depends on: #43") == [43]


def test_parse_depends_on_multiple():
    body = "Depends on: #10\nSome text\nDepends on #20"
    assert parse_dependency_numbers(body) == [10, 20]


def test_parse_depends_on_none():
    assert parse_dependency_numbers("No deps here") == []


def test_parse_depends_on_list_on_one_line():
    assert parse_dependency_numbers("Depends on: #13, #14, #9, #12") == [13, 14, 9, 12]


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


def test_order_by_dependencies_after_all_deps_not_just_first(monkeypatch):
    # Regression (#470): only the first dependency on the line was used for ordering.
    cfg = _test_cfg()
    prs = [
        {"pr_number": 3, "issue_number": 5, "title": "C"},
        {"pr_number": 1, "issue_number": 10, "title": "A"},
        {"pr_number": 2, "issue_number": 20, "title": "B"},
    ]

    bodies = {
        "5": json.dumps({"body": "Depends on: #10, #20"}),
        "10": json.dumps({"body": "No deps"}),
        "20": json.dumps({"body": "No deps"}),
    }

    def fake_run(cmd, **kw):
        return _make_result(stdout=bodies[cmd[cmd.index("view") + 1]])

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    issues = [p["issue_number"] for p in order_by_dependencies(prs, cfg)]
    assert issues.index(5) > issues.index(10)
    assert issues.index(5) > issues.index(20)


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


# --- refresh_review_queue ---


def _pr(number, title="Change", body="", draft=False, mergeable="MERGEABLE", labels=(), files=()):
    return {
        "number": number,
        "title": title,
        "body": body,
        "isDraft": draft,
        "mergeable": mergeable,
        "labels": [{"name": n} for n in labels],
        "files": [{"path": p, "additions": 1, "deletions": 0} for p in files],
    }


def _closed(number, closed_at, merged_at=None, title="Change", body=""):
    return {
        "number": number,
        "title": title,
        "body": body,
        "closedAt": closed_at,
        "mergedAt": merged_at,
    }


class FakeGh:
    """Dispatch mocked ``gh`` calls for refresh_review_queue tests."""

    def __init__(
        self,
        prs,
        issues=None,
        states=None,
        ci=None,
        mergeable_views=None,
        rq=99,
        rq_body="",
        closed=None,
    ):
        self.prs = prs
        self.closed = closed or []
        self.issues = issues or {}
        self.states = states or {}
        self.ci = ci or {}
        self.mergeable_views = mergeable_views or {}
        self.rq = rq
        self.rq_body = rq_body
        self.calls = []
        self.edits = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[1:3] == ["issue", "list"]:
            return _make_result(stdout=json.dumps([{"number": self.rq}] if self.rq else []))
        if cmd[1:3] == ["pr", "list"]:
            state = cmd[cmd.index("--state") + 1]
            return _make_result(stdout=json.dumps(self.closed if state == "closed" else self.prs))
        if cmd[1:3] == ["pr", "checks"]:
            bucket = self.ci.get(int(cmd[3]), "pass")
            return _make_result(stdout=json.dumps([{"bucket": bucket}]))
        if cmd[1:3] == ["pr", "view"]:
            values = self.mergeable_views.get(int(cmd[3]), ["UNKNOWN"])
            value = values.pop(0) if len(values) > 1 else values[0]
            return _make_result(stdout=json.dumps({"mergeable": value}))
        if cmd[1:3] == ["issue", "view"]:
            number = int(cmd[3])
            if number == self.rq:
                return _make_result(stdout=json.dumps({"body": self.rq_body}))
            issue = self.issues.get(number, {})
            return _make_result(
                stdout=json.dumps(
                    {
                        "body": issue.get("body", ""),
                        "labels": [{"name": n} for n in issue.get("labels", ())],
                    }
                )
            )
        if cmd[1] == "api":
            number = int(cmd[2].rsplit("/", 1)[1])
            return _make_result(stdout=self.states.get(number, "closed") + "\n")
        if cmd[1:3] == ["issue", "edit"]:
            path = cmd[cmd.index("--body-file") + 1]
            with open(path) as f:
                self.edits.append({"cmd": cmd, "body": f.read()})
            return _make_result()
        raise AssertionError(f"unexpected gh call: {cmd}")


def _refresh(monkeypatch, fake):
    monkeypatch.setattr(review_queue.subprocess, "run", fake)
    monkeypatch.setattr(review_queue, "_sleep", lambda s: None)
    refresh_review_queue(_test_cfg())
    assert fake.edits, "expected a gh issue edit call"
    return fake.edits[-1]["body"]


def _open_rows(body):
    """Parse the Open table into a list of row dicts in display order."""
    section = read_section(body, "open")
    rows = []
    for line in section.splitlines()[2:]:
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        rows.append(
            {
                "next": cells[0],
                "pr": int(cells[1].lstrip("#")),
                "issue": cells[2],
                "title": cells[3],
                "state": cells[4],
                "ci": cells[5],
                "notes": cells[6],
            }
        )
    return rows


def _states(body):
    return {r["pr"]: r["state"] for r in _open_rows(body)}


def _next_prs(body):
    return [r["pr"] for r in _open_rows(body) if r["next"] == "next"]


def test_refresh_edits_review_queue_issue_with_body_file(monkeypatch):
    fake = FakeGh([_pr(1, body="Closes #10")], rq=77)
    _refresh(monkeypatch, fake)
    cmd = fake.edits[0]["cmd"]
    assert cmd[:4] == ["gh", "issue", "edit", "77"]
    assert "--body-file" in cmd
    assert cmd[cmd.index("--repo") + 1] == "acme-corp/widget"


def test_refresh_removes_body_tempfile(monkeypatch):
    import os

    fake = FakeGh([_pr(1)])
    _refresh(monkeypatch, fake)
    cmd = fake.edits[0]["cmd"]
    assert not os.path.exists(cmd[cmd.index("--body-file") + 1])


def test_refresh_no_edit_when_no_review_queue_issue(monkeypatch):
    fake = FakeGh([_pr(1)], rq=None)
    monkeypatch.setattr(review_queue.subprocess, "run", fake)
    assert refresh_review_queue(_test_cfg()) is None
    assert not [c for c in fake.calls if c[1:3] == ["issue", "edit"]]


def test_refresh_raises_and_skips_edit_when_pr_list_fails(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["issue", "list"]:
            return _make_result(stdout=json.dumps([{"number": 99}]))
        return _make_result(returncode=1, stderr="boom")

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Failed to list open PRs"):
        refresh_review_queue(_test_cfg())
    assert not [c for c in calls if c[1:3] == ["issue", "edit"]]


def test_refresh_does_not_use_batch_summary_helpers(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("must not be called")

    for name in ("post_batch_summary", "order_by_dependencies", "format_summary"):
        monkeypatch.setattr(review_queue, name, boom)
    _refresh(monkeypatch, FakeGh([_pr(1)]))


def test_refresh_blocked_by_open_pr_then_ready_when_removed(monkeypatch):
    dependent = _pr(2, body="Closes #20\n\nDepends on: #1")
    fake = FakeGh([_pr(1, body="Closes #10"), dependent], states={1: "open"})
    body = _refresh(monkeypatch, fake)
    assert _states(body)[2] == "blocked"
    assert "blocked by #1" in _open_rows(body)[1]["notes"]

    fake.prs = [dependent]
    fake.states = {1: "closed"}
    body = _refresh(monkeypatch, fake)
    row = _open_rows(body)[0]
    assert row["pr"] == 2
    assert row["state"] == "ready"
    assert row["next"] == "next"


def test_refresh_blocked_by_dependency_in_linked_issue_body(monkeypatch):
    fake = FakeGh(
        [_pr(3, body="Closes #30"), _pr(4, body="Closes #40")],
        issues={30: {"body": "Depends on: #5"}, 40: {"body": "Depends on: #6"}},
        states={5: "open", 6: "closed"},
    )
    body = _refresh(monkeypatch, fake)
    assert _states(body) == {3: "blocked", 4: "ready"}


def test_refresh_needs_update_for_conflict_or_ci_fail(monkeypatch):
    fake = FakeGh(
        [
            _pr(1, draft=True),
            _pr(2, body="Depends on: #50"),
            _pr(3, mergeable="CONFLICTING"),
            _pr(4),
            _pr(5),
        ],
        ci={4: "fail"},
        states={50: "open"},
    )
    body = _refresh(monkeypatch, fake)
    assert _states(body) == {
        1: "draft",
        2: "blocked",
        3: "needs update",
        4: "needs update",
        5: "ready",
    }
    assert [r["pr"] for r in _open_rows(body)] == [5, 3, 4, 2, 1]


def test_refresh_draft_wins_over_conflict_and_dependency(monkeypatch):
    fake = FakeGh(
        [
            _pr(1, draft=True, mergeable="CONFLICTING", body="Depends on: #50"),
            _pr(2, mergeable="CONFLICTING"),
            _pr(3, body="Depends on: #50"),
            _pr(4),
        ],
        states={50: "open"},
    )
    body = _refresh(monkeypatch, fake)
    assert _states(body)[1] == "draft"
    assert [r["pr"] for r in _open_rows(body)] == [4, 2, 3, 1]


def test_refresh_orders_by_priority_then_number(monkeypatch):
    fake = FakeGh(
        [
            _pr(1),
            _pr(2, labels=["p2"]),
            _pr(3, labels=["p1"]),
            _pr(4, labels=["p0"]),
            _pr(5, labels=["p1"]),
            _pr(6, body="Closes #60"),
        ],
        issues={60: {"labels": ["p0"]}},
    )
    body = _refresh(monkeypatch, fake)
    assert [r["pr"] for r in _open_rows(body)] == [4, 6, 3, 5, 2, 1]


def test_refresh_next_skips_pending_ci(monkeypatch):
    fake = FakeGh([_pr(1), _pr(2), _pr(3)], ci={1: "pending"})
    body = _refresh(monkeypatch, fake)
    assert _next_prs(body) == [2]


def test_refresh_no_next_when_none_qualifies(monkeypatch):
    fake = FakeGh([_pr(1), _pr(2, draft=True), _pr(3, mergeable="CONFLICTING")], ci={1: "pending"})
    body = _refresh(monkeypatch, fake)
    assert _next_prs(body) == []
    assert "next" not in [r["next"] for r in _open_rows(body)]


def test_refresh_row_links_issue_and_includes_all_authors(monkeypatch):
    # gh pr list is called without --author, so PRs from anyone are listed.
    fake = FakeGh([_pr(7, body="Closes #70"), _pr(8, body="Unrelated change by a contributor")])
    body = _refresh(monkeypatch, fake)
    rows = {r["pr"]: r for r in _open_rows(body)}
    assert rows[7]["issue"] == "#70"
    assert rows[8]["issue"] == "-"
    pr_list = next(c for c in fake.calls if c[1:3] == ["pr", "list"])
    assert "--author" not in pr_list
    assert "--state" in pr_list and pr_list[pr_list.index("--state") + 1] == "open"


def test_refresh_mergeable_unknown_resolves_on_retry(monkeypatch):
    fake = FakeGh(
        [_pr(1, mergeable="UNKNOWN"), _pr(2, mergeable="UNKNOWN")],
        mergeable_views={1: ["UNKNOWN", "CONFLICTING"], 2: ["UNKNOWN", "MERGEABLE"]},
    )
    body = _refresh(monkeypatch, fake)
    rows = {r["pr"]: r for r in _open_rows(body)}
    assert rows[1]["state"] == "needs update"
    assert rows[2]["state"] == "ready"
    assert "merge status pending" not in rows[2]["notes"]


def test_refresh_mergeable_stays_unknown_is_ready_with_note(monkeypatch):
    sleeps = []
    fake = FakeGh([_pr(1, mergeable="UNKNOWN")], mergeable_views={1: ["UNKNOWN"]})
    monkeypatch.setattr(review_queue.subprocess, "run", fake)
    monkeypatch.setattr(review_queue, "_sleep", sleeps.append)
    refresh_review_queue(_test_cfg())
    row = _open_rows(fake.edits[0]["body"])[0]
    assert row["state"] == "ready"
    assert "merge status pending" in row["notes"]
    assert len(sleeps) == review_queue._MERGEABLE_RETRIES
    assert len([c for c in fake.calls if c[1:3] == ["pr", "view"]]) == len(sleeps)


def test_refresh_escapes_title(monkeypatch):
    fake = FakeGh([_pr(1, title="fix a | b\nsecond line cc @someuser")])
    body = _refresh(monkeypatch, fake)
    row = _open_rows(body)[0]
    assert "\\|" in row["title"]
    assert "@someuser" not in body
    assert len(read_section(body, "open").splitlines()) == 3  # header + separator + 1 row
    assert row["state"] == "ready"


def test_refresh_empty_open_list(monkeypatch):
    body = _refresh(monkeypatch, FakeGh([]))
    assert read_section(body, "open") == "_No open PRs._"
    assert read_section(body, "done") == "_None yet._"


def _done_rows(body):
    """Parse the Done table into a list of row dicts in display order."""
    rows = []
    for line in read_section(body, "done").splitlines()[2:]:
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        rows.append(
            {
                "pr": int(cells[0].lstrip("#")),
                "issue": cells[1],
                "title": cells[2],
                "result": cells[3],
            }
        )
    return rows


def test_refresh_overlap_notes_name_other_prs(monkeypatch):
    fake = FakeGh(
        [
            _pr(1, files=["src/a.py", "README.md"]),
            _pr(2, files=["src/a.py"]),
            _pr(3, files=["src/c.py"]),
        ]
    )
    body = _refresh(monkeypatch, fake)
    rows = {r["pr"]: r for r in _open_rows(body)}
    assert rows[1]["notes"] == "overlaps #2"
    assert rows[2]["notes"] == "overlaps #1"
    assert "overlaps" not in rows[3]["notes"]
    # Overlap is informational: all three stay ready.
    assert {r["state"] for r in rows.values()} == {"ready"}


def test_refresh_lists_files_for_open_prs(monkeypatch):
    fake = FakeGh([_pr(1)])
    _refresh(monkeypatch, fake)
    pr_list = next(
        c for c in fake.calls if c[1:3] == ["pr", "list"] and c[c.index("--state") + 1] == "open"
    )
    assert "files" in pr_list[pr_list.index("--json") + 1].split(",")


def test_refresh_done_merged_and_closed_entries(monkeypatch):
    fake = FakeGh(
        [_pr(1)],
        closed=[
            _closed(41, "2026-01-15T10:00:00Z", "2026-01-15T10:00:00Z", body="Closes #40"),
            _closed(42, "2026-01-16T10:00:00Z", title="Abandoned"),
        ],
    )
    body = _refresh(monkeypatch, fake)
    rows = {r["pr"]: r for r in _done_rows(body)}
    assert rows[41]["result"] == "merged 2026-01-15"
    assert rows[41]["issue"] == "#40"
    assert rows[42]["result"] == "closed, not merged"
    assert rows[42]["issue"] == "-"
    pr_list = next(
        c for c in fake.calls if c[1:3] == ["pr", "list"] and c[c.index("--state") + 1] == "closed"
    )
    assert set(pr_list[pr_list.index("--json") + 1].split(",")) >= {
        "number",
        "title",
        "body",
        "closedAt",
        "mergedAt",
    }


def test_refresh_done_kept_when_no_prs_open(monkeypatch):
    fake = FakeGh(
        [],
        closed=[
            _closed(5, "2026-01-10T00:00:00Z", "2026-01-10T00:00:00Z"),
            _closed(6, "2026-01-11T00:00:00Z", "2026-01-11T00:00:00Z", body="Closes #60"),
        ],
    )
    body = _refresh(monkeypatch, fake)
    assert read_section(body, "open") == "_No open PRs._"
    rows = _done_rows(body)
    assert [r["pr"] for r in rows] == [6, 5]
    assert rows[0]["result"] == "merged 2026-01-11"


def test_refresh_done_newest_first_capped_at_ten(monkeypatch):
    closed = [
        _closed(n, f"2026-02-{n:02d}T12:00:00Z", f"2026-02-{n:02d}T12:00:00Z") for n in range(1, 13)
    ]
    shuffled = [closed[i] for i in (4, 11, 0, 7, 2, 9, 5, 1, 10, 3, 8, 6)]
    body = _refresh(monkeypatch, FakeGh([], closed=shuffled))
    assert [r["pr"] for r in _done_rows(body)] == list(range(12, 2, -1))


@pytest.mark.parametrize(
    "rq_body",
    ["Hand-written body with no markers", build_queue_body("old table", "| #3 | - | Stale | x |")],
)
def test_refresh_done_ignores_existing_body(monkeypatch, rq_body):
    fake = FakeGh(
        [_pr(1)],
        rq_body=rq_body,
        closed=[_closed(8, "2026-03-01T00:00:00Z", "2026-03-01T00:00:00Z")],
    )
    body = _refresh(monkeypatch, fake)
    assert [r["pr"] for r in _done_rows(body)] == [8]
    assert "Stale" not in body
    assert "old table" not in body
    expected = build_queue_body("", review_queue.format_done_table(fake.closed))
    assert read_section(body, "done") == read_section(expected, "done")


def test_refresh_done_escapes_title(monkeypatch):
    fake = FakeGh(
        [],
        closed=[_closed(9, "2026-03-02T00:00:00Z", title="fix a | b\nsecond line cc @someuser")],
    )
    body = _refresh(monkeypatch, fake)
    rows = _done_rows(body)
    assert len(rows) == 1
    assert "\\|" in rows[0]["title"]
    assert "\n" not in rows[0]["title"]
    assert "@someuser" not in body
    assert rows[0]["result"] == "closed, not merged"


def test_refresh_raises_and_skips_edit_when_closed_pr_list_fails(monkeypatch):
    fake = FakeGh([_pr(1)])

    def fake_run(cmd, **kw):
        if cmd[1:3] == ["pr", "list"] and cmd[cmd.index("--state") + 1] == "closed":
            fake.calls.append(cmd)
            return _make_result(returncode=1, stderr="boom")
        return fake(cmd, **kw)

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    monkeypatch.setattr(review_queue, "_sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="Failed to list closed PRs"):
        refresh_review_queue(_test_cfg())
    assert not fake.edits


# --- helpers for refresh_review_queue ---


def test_parse_closes_number():
    assert parse_closes_number("Summary\n\nCloses #12") == 12
    assert parse_closes_number("fixes #3 and closes #4") == 3
    assert parse_closes_number("No link") is None


def test_escape_cell():
    assert escape_cell("a|b\r\nc @x") == "a\\|b c @​x"


def test_priority_rank_prefers_pr_labels_then_issue():
    assert priority_rank({"p1"}, {"p0"}) == 1
    assert priority_rank(set(), {"p2"}) == 2
    assert priority_rank({"bug"}, set()) == 3


def test_resolve_mergeable_no_retry_when_known(monkeypatch):
    def fail(*a, **kw):
        raise AssertionError("should not re-query")

    monkeypatch.setattr(review_queue.subprocess, "run", fail)
    monkeypatch.setattr(review_queue, "_sleep", fail)
    assert resolve_mergeable(1, "MERGEABLE", _test_cfg()) == "MERGEABLE"


def test_format_open_table_marks_next():
    rows = [
        {
            "pr_number": 1,
            "issue_number": 10,
            "title": "A",
            "state": "ready",
            "ci": "pass",
            "priority": 3,
            "notes": [],
            "next": True,
        }
    ]
    assert "| next | #1 | #10 | A | ready | pass |  |" in format_open_table(rows)


def test_find_overlaps():
    prs = [
        _pr(1, files=["a.py", "b.py"]),
        _pr(2, files=["b.py"]),
        _pr(3, files=["a.py"]),
        _pr(4, files=["z.py"]),
        _pr(5),
    ]
    assert find_overlaps(prs) == {1: [2, 3], 2: [1], 3: [1]}


def test_format_done_table_empty():
    assert format_done_table([]) == "_None yet._"


def test_format_done_table_row():
    table = format_done_table(
        [_closed(4, "2026-01-15T09:00:00Z", "2026-01-15T09:00:00Z", "T", "Closes #3")]
    )
    assert "| #4 | #3 | T | merged 2026-01-15 |" in table


def test_read_section_missing_returns_none():
    assert read_section("plain body", "done") is None
    assert read_section(build_queue_body("t", "d"), "open") == "t"
