from __future__ import annotations

import json
import re

import pytest

import autoloop.review_queue as review_queue
from autoloop.config import AutoLoopConfig, RepoContext
from autoloop.review_queue import (
    build_queue_body,
    escape_cell,
    fetch_ci_status,
    fetch_gate_flags,
    find_overlaps,
    find_review_queue_issue,
    format_done_table,
    format_open_table,
    parse_closes_number,
    parse_dependency_numbers,
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


def _rest_pr(pr):
    """Render a ``_closed`` dict the way the REST pulls endpoint returns it."""
    return {
        "number": pr["number"],
        "title": pr["title"],
        "body": pr["body"] or None,
        "state": "closed",
        "closed_at": pr["closedAt"],
        "merged_at": pr["mergedAt"],
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
            return _make_result(stdout=json.dumps(self.prs))
        if cmd[1] == "api" and "/pulls?" in cmd[2]:
            return _make_result(stdout=json.dumps([_rest_pr(pr) for pr in self.closed]))
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
        if cmd[1] == "api" and "/pulls?" in cmd[2]:
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


# --- _list_closed_prs: REST pulls endpoint sorted by update time ---


def test_list_closed_prs_uses_pulls_endpoint_sorted_by_updated(monkeypatch):
    calls = []
    rest = [
        _rest_pr(_closed(3, "2026-03-05T00:00:00Z", "2026-03-05T00:00:00Z", "Old PR")),
        _rest_pr(_closed(90, "2026-03-01T00:00:00Z", title="New PR", body="Closes #9")),
    ]

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return _make_result(stdout=json.dumps(rest))

    monkeypatch.setattr(review_queue.subprocess, "run", fake_run)
    prs = review_queue._list_closed_prs(_test_cfg())

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:2] == ["gh", "api"]
    assert cmd[2].startswith("repos/acme-corp/widget/pulls?")
    assert "state=closed&sort=updated&direction=desc" in cmd[2]
    assert "per_page=50" in cmd[2]
    assert prs == [
        _closed(3, "2026-03-05T00:00:00Z", "2026-03-05T00:00:00Z", "Old PR"),
        _closed(90, "2026-03-01T00:00:00Z", title="New PR", body="Closes #9"),
    ]


def test_list_closed_prs_raises_on_gh_failure(monkeypatch):
    monkeypatch.setattr(
        review_queue.subprocess, "run", lambda cmd, **kw: _make_result(returncode=1, stderr="x")
    )
    with pytest.raises(RuntimeError, match="Failed to list closed PRs"):
        review_queue._list_closed_prs(_test_cfg())


def test_refresh_done_orders_by_close_time_not_fetch_order(monkeypatch):
    # The endpoint sorts by update time; a PR updated after closing can come back first.
    fake = FakeGh(
        [],
        closed=[
            _closed(1, "2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z"),
            _closed(2, "2026-04-03T00:00:00Z"),
            _closed(3, "2026-04-02T00:00:00Z", "2026-04-02T00:00:00Z"),
        ],
    )
    body = _refresh(monkeypatch, fake)
    assert [r["pr"] for r in _done_rows(body)] == [2, 3, 1]


# --- implement_issue calls refresh_review_queue ---


def _stub_implement_main(monkeypatch, tmp_path, refresh):
    """Stub implement_issue.main()'s collaborators; *refresh* replaces refresh_review_queue."""
    import autoloop.implement_issue as implement_issue
    from autoloop.prune import PruneResult

    cfg = _test_cfg(implement_isolation="worktree")
    monkeypatch.setattr(implement_issue, "cfg", cfg)
    monkeypatch.setattr(implement_issue, "cleanup_merged_labels", lambda: None)
    monkeypatch.setattr(implement_issue, "unblock_ready_issues", lambda: None)
    monkeypatch.setattr(implement_issue, "prune", lambda ctx, cfg: PruneResult())
    monkeypatch.setattr(implement_issue, "refresh_review_queue", refresh)
    return implement_issue, cfg, RepoContext(tmp_path)


def _stub_targeted(monkeypatch, refresh):
    import autoloop.implement_issue as implement_issue

    cfg = _test_cfg()
    monkeypatch.setattr(implement_issue, "cfg", cfg)
    monkeypatch.setattr(implement_issue, "refresh_review_queue", refresh)
    monkeypatch.setattr(
        implement_issue,
        "get_issue_by_number",
        lambda n: {"number": n, "title": "Targeted", "body": "", "labels": []},
    )
    monkeypatch.setattr(implement_issue, "dependencies_met", lambda issue: True)
    monkeypatch.setattr(implement_issue, "implement_single_issue", lambda issue, **kw: True)
    return implement_issue, cfg


def test_implement_targeted_issue_refreshes_review_queue(monkeypatch):
    calls = []
    implement_issue, cfg = _stub_targeted(monkeypatch, calls.append)

    assert implement_issue.implement_targeted_issue(28) is True
    assert calls == [cfg]


def test_implement_targeted_issue_refresh_failure_is_logged(monkeypatch, caplog):
    def boom(cfg):
        raise RuntimeError("gh exploded")

    implement_issue, _ = _stub_targeted(monkeypatch, boom)

    with caplog.at_level("ERROR"):
        assert implement_issue.implement_targeted_issue(28) is True
    assert "Failed to refresh review queue" in caplog.text
    assert "gh exploded" in caplog.text


def test_main_batch_refreshes_review_queue_once_after_loop(monkeypatch, tmp_path, capsys):
    events = []
    implement_issue, cfg, ctx = _stub_implement_main(
        monkeypatch, tmp_path, lambda c: events.append(("refresh", c))
    )
    queue = [
        {"number": 1, "title": "One", "body": "", "labels": []},
        {"number": 2, "title": "Two", "body": "", "labels": []},
    ]
    monkeypatch.setattr(implement_issue, "get_top_ready_issue", lambda: queue.pop(0))
    monkeypatch.setattr(
        implement_issue,
        "implement_single_issue",
        lambda issue, **kw: events.append(("implement", issue["number"])) or True,
    )
    assert implement_issue.main(ctx=ctx, max_issues=2) is None
    assert events == [("implement", 1), ("implement", 2), ("refresh", cfg)]
    assert "Implemented 2 issue(s) this run." in capsys.readouterr().out


def test_main_batch_refresh_failure_is_logged(monkeypatch, tmp_path, capsys, caplog):
    def boom(cfg):
        raise RuntimeError("gh exploded")

    implement_issue, _, ctx = _stub_implement_main(monkeypatch, tmp_path, boom)
    queue = [{"number": 1, "title": "One", "body": "", "labels": []}]
    monkeypatch.setattr(implement_issue, "get_top_ready_issue", lambda: queue.pop(0))
    monkeypatch.setattr(implement_issue, "implement_single_issue", lambda issue, **kw: True)

    with caplog.at_level("ERROR"):
        assert implement_issue.main(ctx=ctx, max_issues=1) is None
    assert "Implemented 1 issue(s) this run." in capsys.readouterr().out
    assert "Failed to refresh review queue" in caplog.text
    assert "gh exploded" in caplog.text
    assert not (tmp_path / ".autoloop.lock").exists()
