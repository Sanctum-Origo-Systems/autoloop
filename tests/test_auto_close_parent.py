from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from autoloop.auto_close_parent import (
    GhClient,
    all_siblings_closed,
    check_and_close_parent,
    close_parent_chain,
    close_parent_with_comment,
    count_subissues,
    parse_closes_ref,
    parse_parent_ref,
)


def test_ghclient_stores_repo():
    client = GhClient(repo="owner/repo")
    assert client.repo == "owner/repo"


def test_ghclient_requires_repo():
    import inspect

    sig = inspect.signature(GhClient.__init__)
    param = sig.parameters["repo"]
    assert param.default is inspect.Parameter.empty


def test_parse_parent_ref_valid():
    assert parse_parent_ref("Some text\nParent issue: #42\nmore text") == 42


def test_parse_parent_ref_missing_pattern():
    assert parse_parent_ref("This body has no parent reference at all.") is None


def test_parse_parent_ref_empty_body():
    assert parse_parent_ref("") is None


def test_parse_parent_ref_none_body():
    assert parse_parent_ref("") is None


def test_parse_parent_ref_malformed():
    assert parse_parent_ref("Parent issue: #") is None
    assert parse_parent_ref("Parent issue: 42") is None


def test_parse_closes_ref_valid():
    assert parse_closes_ref("This PR does stuff.\nCloses #57") == 57


def test_parse_closes_ref_missing_pattern():
    assert parse_closes_ref("No linked issue in this body.") is None


def test_parse_closes_ref_empty_body():
    assert parse_closes_ref("") is None


def test_parse_closes_ref_malformed():
    assert parse_closes_ref("Closes #") is None
    assert parse_closes_ref("Closes 57") is None


def _gh_with_open_issues(issues: list[dict]) -> MagicMock:
    gh = MagicMock()
    gh.list_open_issues.return_value = issues
    return gh


def test_all_siblings_closed_zero_open_siblings():
    gh = _gh_with_open_issues([{"number": 7, "body": "Unrelated open issue"}])
    assert all_siblings_closed(gh, 55) is True


def test_all_siblings_closed_one_open_sibling():
    gh = _gh_with_open_issues([{"number": 56, "body": "Parent issue: #55"}])
    assert all_siblings_closed(gh, 55) is False


def test_all_siblings_closed_multiple_open_siblings():
    gh = _gh_with_open_issues(
        [
            {"number": 56, "body": "Parent issue: #55"},
            {"number": 57, "body": "Parent issue: #55"},
            {"number": 99, "body": "Parent issue: #12"},
        ]
    )
    assert all_siblings_closed(gh, 55) is False


def test_all_siblings_closed_ignores_other_parents():
    gh = _gh_with_open_issues([{"number": 99, "body": "Parent issue: #12"}])
    assert all_siblings_closed(gh, 55) is True


def test_all_siblings_closed_empty_repo():
    gh = _gh_with_open_issues([])
    assert all_siblings_closed(gh, 55) is True


def test_count_subissues_counts_matching_parents():
    gh = MagicMock()
    gh.list_all_issues.return_value = [
        {"number": 56, "body": "Parent issue: #55"},
        {"number": 57, "body": "Parent issue: #55"},
        {"number": 99, "body": "Parent issue: #12"},
        {"number": 7, "body": "Unrelated"},
    ]
    assert count_subissues(gh, 55) == 2


def test_count_subissues_none_match():
    gh = MagicMock()
    gh.list_all_issues.return_value = [{"number": 7, "body": "Unrelated"}]
    assert count_subissues(gh, 55) == 0


def test_close_parent_with_comment_invokes_both_calls():
    gh = MagicMock()
    close_parent_with_comment(gh, 55, 3)

    gh.close_issue.assert_called_once_with(55)
    gh.comment_issue.assert_called_once()

    (num, body), _ = gh.comment_issue.call_args
    assert num == 55
    assert re.search(r"Auto-closed: All \d+ sub-issues are now complete\.", body)
    assert "3" in body


def _orchestration_gh(
    pr_body: str, issue_body: str, open_issues: list[dict], all_issues: list[dict]
) -> MagicMock:
    gh = MagicMock()
    gh.get_pr_body.return_value = pr_body
    gh.get_issue_body.return_value = issue_body
    gh.list_open_issues.return_value = open_issues
    gh.list_all_issues.return_value = all_issues
    return gh


def test_check_and_close_parent_closes_when_last_sibling():
    all_issues = [
        {"number": 56, "body": "Parent issue: #55"},
        {"number": 57, "body": "Parent issue: #55"},
    ]
    gh = _orchestration_gh(
        pr_body="Closes #57",
        issue_body="Parent issue: #55",
        open_issues=[],
        all_issues=all_issues,
    )

    assert check_and_close_parent(42, gh) == 55
    gh.close_issue.assert_called_once_with(55)
    (num, body), _ = gh.comment_issue.call_args
    assert num == 55
    assert "All 2 sub-issues are now complete." in body


def test_check_and_close_parent_skips_when_sibling_open():
    gh = _orchestration_gh(
        pr_body="Closes #57",
        issue_body="Parent issue: #55",
        open_issues=[{"number": 56, "body": "Parent issue: #55"}],
        all_issues=[],
    )

    assert check_and_close_parent(42, gh) is None
    gh.close_issue.assert_not_called()
    gh.comment_issue.assert_not_called()


def test_check_and_close_parent_skips_when_no_parent_ref():
    gh = _orchestration_gh(
        pr_body="Closes #57",
        issue_body="A sub-issue with no parent reference.",
        open_issues=[],
        all_issues=[],
    )

    assert check_and_close_parent(42, gh) is None
    gh.close_issue.assert_not_called()
    gh.comment_issue.assert_not_called()


def test_check_and_close_parent_skips_when_no_closes_ref():
    gh = _orchestration_gh(
        pr_body="This PR has no Closes reference.",
        issue_body="",
        open_issues=[],
        all_issues=[],
    )

    assert check_and_close_parent(42, gh) is None
    gh.get_issue_body.assert_not_called()
    gh.close_issue.assert_not_called()
    gh.comment_issue.assert_not_called()


def test_check_and_close_parent_constructs_ghclient_from_cfg():
    fake_cfg = SimpleNamespace(repo="owner/other")

    with patch("autoloop.auto_close_parent.GhClient") as MockGhClient:
        mock_instance = MagicMock()
        mock_instance.get_pr_body.return_value = ""
        MockGhClient.return_value = mock_instance

        check_and_close_parent(1, cfg=fake_cfg)

        MockGhClient.assert_called_once_with(repo="owner/other")


def test_check_and_close_parent_cfg_repo_flows_to_gh_calls():
    fake_cfg = SimpleNamespace(repo="owner/other")
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        if "pr" in cmd and "view" in cmd:
            result.stdout = '{"body": "Closes #10"}'
        elif "issue" in cmd and "view" in cmd:
            result.stdout = '{"body": "Parent issue: #5"}'
        elif "issue" in cmd and "list" in cmd:
            result.stdout = "[]"
        else:
            result.stdout = ""
        return result

    with patch("autoloop.auto_close_parent.subprocess.run", side_effect=fake_run):
        check_and_close_parent(1, cfg=fake_cfg)

    assert len(calls) > 0
    for call in calls:
        repo_indices = [i for i, arg in enumerate(call) if arg == "--repo"]
        assert len(repo_indices) == 1, f"Expected exactly one --repo in {call}"
        assert call[repo_indices[0] + 1] == "owner/other", (
            f"Expected 'owner/other' after --repo in {call}"
        )


def test_check_and_close_parent_raises_without_gh_or_cfg():
    import pytest

    with pytest.raises(ValueError, match="Either gh or cfg must be provided"):
        check_and_close_parent(1)


def test_check_and_close_parent_gh_takes_precedence_over_cfg():
    fake_cfg = SimpleNamespace(repo="owner/other")
    gh = _orchestration_gh(
        pr_body="This PR has no Closes reference.",
        issue_body="",
        open_issues=[],
        all_issues=[],
    )

    with patch("autoloop.auto_close_parent.GhClient") as MockGhClient:
        check_and_close_parent(1, gh=gh, cfg=fake_cfg)
        MockGhClient.assert_not_called()


# --- close_parent_chain: recursive chain walking ---


def test_close_parent_chain_single_level():
    gh = MagicMock()
    gh.get_issue_body.return_value = "Parent issue: #10"
    gh.list_open_issues.return_value = []
    gh.list_all_issues.return_value = [
        {"number": 20, "body": "Parent issue: #10"},
        {"number": 21, "body": "Parent issue: #10"},
    ]

    closed = close_parent_chain(gh, 20)
    assert closed == [10]
    gh.close_issue.assert_called_once_with(10)


def test_close_parent_chain_two_levels():
    """#30 → parent #20 → parent #10. Both should close."""
    bodies = {30: "Parent issue: #20", 20: "Parent issue: #10", 10: ""}

    gh = MagicMock()
    gh.get_issue_body.side_effect = lambda n: bodies.get(n, "")
    gh.list_open_issues.return_value = []
    gh.list_all_issues.return_value = [
        {"number": 30, "body": "Parent issue: #20"},
        {"number": 20, "body": "Parent issue: #10"},
    ]

    closed = close_parent_chain(gh, 30)
    assert closed == [20, 10]
    assert gh.close_issue.call_count == 2


def test_close_parent_chain_stops_when_sibling_open():
    """#30 → parent #20 → parent #10. #20 has an open sibling, so #10 stays open."""
    bodies = {30: "Parent issue: #20", 20: "Parent issue: #10"}

    gh = MagicMock()
    gh.get_issue_body.side_effect = lambda n: bodies.get(n, "")
    gh.list_open_issues.return_value = [{"number": 31, "body": "Parent issue: #20"}]
    gh.list_all_issues.return_value = []

    closed = close_parent_chain(gh, 30)
    assert closed == []
    gh.close_issue.assert_not_called()


def test_close_parent_chain_stops_when_no_parent():
    gh = MagicMock()
    gh.get_issue_body.return_value = "No parent reference here"

    closed = close_parent_chain(gh, 50)
    assert closed == []


def test_close_parent_chain_respects_max_depth():
    """Chain of 10 levels, but max_depth=3 stops after 3."""
    gh = MagicMock()
    gh.get_issue_body.side_effect = lambda n: f"Parent issue: #{n - 1}"
    gh.list_open_issues.return_value = []
    gh.list_all_issues.return_value = [{"number": 99, "body": "Parent issue: #1"}]

    closed = close_parent_chain(gh, 10, max_depth=3)
    assert len(closed) == 3


def test_close_parent_chain_partial_close():
    """#30 → parent #20 (all closed) → parent #10 (sibling open). Only #20 closes."""
    bodies = {30: "Parent issue: #20", 20: "Parent issue: #10"}
    open_siblings_of = {20: [], 10: [{"number": 21, "body": "Parent issue: #10"}]}

    gh = MagicMock()
    gh.get_issue_body.side_effect = lambda n: bodies.get(n, "")
    gh.list_open_issues.side_effect = lambda: open_siblings_of.get(
        parse_parent_ref(gh.get_issue_body.call_args[0][0]), []
    )

    call_count = [0]
    original_get_body = gh.get_issue_body.side_effect

    def tracking_get_body(n):
        return original_get_body(n)

    gh.get_issue_body.side_effect = tracking_get_body

    def list_open_for_parent():
        call_count[0] += 1
        if call_count[0] <= 1:
            return []
        return [{"number": 21, "body": "Parent issue: #10"}]

    gh.list_open_issues.side_effect = list_open_for_parent
    gh.list_all_issues.return_value = [{"number": 30, "body": "Parent issue: #20"}]

    closed = close_parent_chain(gh, 30)
    assert closed == [20]


# --- check_and_close_parent with chain ---


def test_check_and_close_parent_walks_chain():
    """Merged PR closes #30, which has parent #20, which has parent #10. All close."""
    bodies = {30: "Parent issue: #20", 20: "Parent issue: #10", 10: ""}

    gh = MagicMock()
    gh.get_pr_body.return_value = "Closes #30"
    gh.get_issue_body.side_effect = lambda n: bodies.get(n, "")
    gh.list_open_issues.return_value = []
    gh.list_all_issues.return_value = [
        {"number": 30, "body": "Parent issue: #20"},
        {"number": 20, "body": "Parent issue: #10"},
    ]

    result = check_and_close_parent(42, gh)
    assert result == 20
    assert gh.close_issue.call_count == 2
