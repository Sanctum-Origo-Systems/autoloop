import importlib.util
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from autoloop import jev_log
from autoloop.config import AutoLoopConfig, RepoContext
from autoloop.jev_log import (
    decisions_path,
    discover_autoloop_prs,
    extract_review_verdict,
    fetch_commit_diff,
    format_issue_text,
    is_auto_merge_recorded,
    load_decisions,
    log_decision,
    parse_autoloop_branch,
    plan_auto_merge_backfill,
)


def _make_ctx(data_dir, repo_dir=None):
    return RepoContext.for_data_dir(data_dir, repo_dir)


def test_log_decision_writes_record(tmp_path):
    ctx = _make_ctx(tmp_path)
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
    log_decision(record, ctx)

    log_file = tmp_path / "jev_decisions.jsonl"
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


def test_log_decision_creates_file(tmp_path):
    ctx = _make_ctx(tmp_path)
    log_file = tmp_path / "jev_decisions.jsonl"
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
        },
        ctx,
    )
    assert log_file.exists()
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1


def test_log_decision_appends_without_overwrite(tmp_path):
    ctx = _make_ctx(tmp_path)
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

    log_decision(record1, ctx)
    log_decision(record2, ctx)

    log_file = tmp_path / "jev_decisions.jsonl"
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["point"] == "triage"
    assert json.loads(lines[1])["point"] == "auto-merge"


def test_log_decision_rejects_missing_keys(tmp_path):
    ctx = _make_ctx(tmp_path)
    incomplete = {"point": "triage", "jev_call": "A"}
    with pytest.raises(ValueError, match="Missing required keys"):
        log_decision(incomplete, ctx)
    log_file = tmp_path / "jev_decisions.jsonl"
    assert not log_file.exists()


def test_log_decision_accepts_optional_pr_field(tmp_path):
    ctx = _make_ctx(tmp_path)
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
    log_decision(record, ctx)

    log_file = tmp_path / "jev_decisions.jsonl"
    written = json.loads(log_file.read_text().strip())
    assert written["pr"] == 55


def test_log_decision_writes_to_data_dir_not_cwd(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    ctx = _make_ctx(data_dir)
    record = {
        "point": "triage",
        "issue": 7,
        "jev_call": {"well_formed": 0.85},
        "incumbent_call": {"verdict": "ready"},
        "outcome": "incumbent",
        "ttft": 0.4,
        "cost": 0.002,
        "timestamp": "2026-06-15T00:00:00Z",
    }
    log_decision(record, ctx)

    assert (data_dir / "jev_decisions.jsonl").exists()
    assert not (tmp_path / "jev_decisions.jsonl").exists()
    assert not (tmp_path / "autoloop" / "jev_decisions.jsonl").exists()


# --- Applied band (#494) ---


def _shadow_record(point, jev_call):
    return {
        "point": point,
        "issue": 3,
        "jev_call": jev_call,
        "incumbent_call": {"verdict": "ready"},
        "outcome": "incumbent",
        "ttft": 0.1,
        "cost": 0.0,
        "timestamp": "2026-11-01T00:00:00Z",
    }


def _logged_band(tmp_path, point, jev_call, cfg):
    log_decision(_shadow_record(point, jev_call), _make_ctx(tmp_path), cfg)
    return json.loads((tmp_path / "jev_decisions.jsonl").read_text())["jev_call"]["band"]


def test_log_decision_stores_default_band_per_probability(tmp_path):
    jev_call = {"well_formed": 0.9, "needs_decomposition": 0.3, "would_fallback": False}

    band = _logged_band(tmp_path, "triage", jev_call, AutoLoopConfig())

    assert band["needs_decomposition"] == {"low": 0.35, "high": 0.85}
    assert band["well_formed"] == {"low": 0.15, "high": 0.85}


def test_log_decision_stores_band_for_auto_merge(tmp_path):
    band = _logged_band(
        tmp_path, "auto-merge", {"meets_acceptance_criteria": 0.9}, AutoLoopConfig()
    )

    assert band == {"meets_acceptance_criteria": {"low": 0.15, "high": 0.85}}


def test_log_decision_band_reflects_custom_gate_edges(tmp_path):
    cfg = AutoLoopConfig(jev_gate_low=0.2, jev_gate_high=0.8)
    jev_call = {"well_formed": 0.9, "needs_decomposition": 0.3}

    band = _logged_band(tmp_path, "triage", jev_call, cfg)

    assert band["well_formed"] == {"low": 0.2, "high": 0.8}
    # The default needs_decomposition override only sets low; high follows gate_high.
    assert band["needs_decomposition"] == {"low": 0.35, "high": 0.8}


def test_log_decision_band_reflects_per_probability_override(tmp_path):
    cfg = AutoLoopConfig(jev_bands={"triage": {"well_formed": {"low": 0.25, "high": 0.9}}})
    jev_call = {"well_formed": 0.9, "needs_decomposition": 0.3}

    band = _logged_band(tmp_path, "triage", jev_call, cfg)

    assert band["well_formed"] == {"low": 0.25, "high": 0.9}
    assert band["needs_decomposition"] == {"low": 0.15, "high": 0.85}


def test_log_decision_without_cfg_stores_no_band(tmp_path):
    log_decision(_shadow_record("triage", {"well_formed": 0.9}), _make_ctx(tmp_path))

    written = json.loads((tmp_path / "jev_decisions.jsonl").read_text())
    assert "band" not in written["jev_call"]


def test_log_decision_error_record_stores_no_band(tmp_path):
    record = _shadow_record("triage", {"error": "boom"})
    log_decision(record, _make_ctx(tmp_path), AutoLoopConfig())

    written = json.loads((tmp_path / "jev_decisions.jsonl").read_text())
    assert written["jev_call"] == {"error": "boom"}


def test_log_decision_does_not_mutate_callers_jev_call(tmp_path):
    jev_call = {"meets_acceptance_criteria": 0.9}
    log_decision(_shadow_record("auto-merge", jev_call), _make_ctx(tmp_path), AutoLoopConfig())

    assert jev_call == {"meets_acceptance_criteria": 0.9}


def test_applied_bands_only_covers_probabilities_present():
    bands = jev_log.applied_bands("triage", {"well_formed": 0.9}, AutoLoopConfig())

    assert bands == {"well_formed": {"low": 0.15, "high": 0.85}}


# --- Auto-merge backfill (read-only half) ---

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "backfill_jev.py"
FIRST_SHA = "a" * 40
HEAD_SHA = "f" * 40
PASSED_COMMENT = "**Review passed:** mutation gate and semantic review both passed."
FAILED_COMMENT = "**Semantic review failed:**\nReview found issues:\n- missing tests"


def _load_backfill_script():
    spec = importlib.util.spec_from_file_location("backfill_jev", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cfg():
    return AutoLoopConfig(repo="acme-corp/widget", diff_truncation=50)


def _pr(
    number,
    branch,
    base="main",
    title="feat: thing",
    created_at="2026-10-01T12:00:00Z",
    state="MERGED",
):
    return {
        "number": number,
        "headRefName": branch,
        "baseRefName": base,
        "title": title,
        "createdAt": created_at,
        "state": state,
    }


class FakeGh:
    """Dispatch fake `gh` subprocess calls from in-memory PR, issue, and diff data."""

    def __init__(self, prs=(), comments=None, commits=None, issues=None, diffs=None):
        self.prs = list(prs)
        self.comments = comments or {}
        self.commits = commits or {}
        self.issues = issues or {}
        self.diffs = diffs or {}
        self.calls = []

    def _ok(self, args, payload):
        out = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[:3] == ["gh", "pr", "list"]:
            return self._ok(args, self.prs)
        if args[:3] == ["gh", "pr", "view"]:
            number, fields = int(args[3]), args[-1]
            if fields == "comments":
                return self._ok(args, {"comments": self.comments.get(number, [])})
            if fields == "commits":
                return self._ok(args, {"commits": self.commits.get(number, [])})
        if args[:3] == ["gh", "issue", "view"]:
            issue = self.issues.get(int(args[3]))
            if issue is not None:
                return self._ok(args, issue)
        if args[:2] == ["gh", "api"]:
            sha = args[-1].rsplit("...", 1)[1]
            if sha in self.diffs:
                return self._ok(args, self.diffs[sha])
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")


def _fake_pr_12(**overrides):
    data = {
        "prs": [_pr(12, "autoloop/12-foo")],
        "comments": {12: [{"body": PASSED_COMMENT}]},
        "commits": {12: [{"oid": FIRST_SHA}, {"oid": HEAD_SHA}]},
        "issues": {12: {"title": "Add foo", "body": "Do the foo."}},
        "diffs": {FIRST_SHA: "diff --git a/foo.py b/foo.py\n+first", HEAD_SHA: "head"},
    }
    data.update(overrides)
    return FakeGh(**data)


def _run_dry(fake, ctx, capsys):
    script = _load_backfill_script()
    with (
        patch("autoloop.jev_log.subprocess.run", side_effect=fake),
        patch("autoloop.jev.should_auto_merge") as jev_mock,
    ):
        code = script.main(["--point", "auto-merge", "--dry-run"], cfg=_cfg(), ctx=ctx)
    assert code == 0
    jev_mock.assert_not_called()
    return capsys.readouterr().out


def _auto_merge_record(**fields):
    record = {
        "point": "auto-merge",
        "jev_call": {},
        "incumbent_call": {"approved": True, "feedback": ""},
        "outcome": "incumbent",
        "ttft": 0.1,
        "cost": 0.0,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    record.update(fields)
    return record


def _skip_count(out, reason):
    match = re.search(rf"^\s+{reason}: (\d+)$", out, re.MULTILINE)
    return int(match.group(1)) if match else 0


def test_auto_merge_dry_run_lists_pr_without_calling_jev(tmp_path, capsys):
    out = _run_dry(_fake_pr_12(), _make_ctx(tmp_path), capsys)
    assert "PRs to backfill: 1" in out
    assert "PR #12 (issue #12, autoloop/12-foo)" in out
    assert "approved=True" in out


def test_auto_merge_dry_run_leaves_existing_file_unchanged(tmp_path, capsys):
    ctx = _make_ctx(tmp_path)
    log_file = tmp_path / "jev_decisions.jsonl"
    log_file.write_text(json.dumps(_auto_merge_record(issue=99, pr=199)) + "\n")
    before = log_file.read_bytes()

    out = _run_dry(_fake_pr_12(), ctx, capsys)

    assert "PR #12 (" in out
    assert log_file.read_bytes() == before


def test_auto_merge_dry_run_creates_no_file(tmp_path, capsys):
    data_dir = tmp_path / "data"
    ctx = _make_ctx(data_dir)

    out = _run_dry(_fake_pr_12(), ctx, capsys)

    assert "PR #12 (" in out
    assert not (data_dir / "jev_decisions.jsonl").exists()
    assert not data_dir.exists()


def test_auto_merge_dry_run_skips_pr_recorded_by_pr(tmp_path, capsys):
    ctx = _make_ctx(tmp_path)
    log_decision(_auto_merge_record(issue=12, pr=12), ctx)

    out = _run_dry(_fake_pr_12(), ctx, capsys)

    assert "PRs to backfill: 0" in out
    assert "PR #12 (" not in out
    assert _skip_count(out, "already_recorded") == 1


def test_auto_merge_dry_run_skips_pr_recorded_by_issue_without_pr(tmp_path, capsys):
    ctx = _make_ctx(tmp_path)
    log_decision(_auto_merge_record(issue=12), ctx)

    out = _run_dry(_fake_pr_12(), ctx, capsys)

    assert "PRs to backfill: 0" in out
    assert _skip_count(out, "already_recorded") == 1


def test_auto_merge_dry_run_skips_pr_without_review_comment(tmp_path, capsys):
    fake = _fake_pr_12(comments={12: [{"body": "Thanks, looks interesting."}]})

    out = _run_dry(fake, _make_ctx(tmp_path), capsys)

    assert "PRs to backfill: 0" in out
    assert _skip_count(out, "no_incumbent") == 1


def test_is_auto_merge_recorded_ignores_other_points_and_prs():
    records = [
        {"point": "triage", "issue": 12},
        _auto_merge_record(issue=12, pr=40),
    ]
    assert not is_auto_merge_recorded(records, 12, 12)
    assert is_auto_merge_recorded(records, 40, 7)


def test_extract_review_verdict_passed():
    assert extract_review_verdict([{"body": PASSED_COMMENT}])["approved"] is True


@pytest.mark.parametrize(
    "body",
    [
        "**Semantic review failed:**\nThe change is incomplete.",
        "Review found issues:\n- missing tests",
    ],
)
def test_extract_review_verdict_failed(body):
    assert extract_review_verdict([{"body": body}])["approved"] is False


def test_extract_review_verdict_uses_first_review_comment():
    comments = [
        {"body": "Unrelated chatter"},
        {"body": FAILED_COMMENT},
        {"body": PASSED_COMMENT},
    ]
    verdict = extract_review_verdict(comments)
    assert verdict["approved"] is False
    assert verdict["feedback"] == FAILED_COMMENT
    assert extract_review_verdict(list(reversed(comments)))["approved"] is True


def test_extract_review_verdict_none_without_review_comment():
    assert extract_review_verdict([{"body": "hello"}, {"body": None}]) is None
    assert extract_review_verdict([]) is None


def test_plan_fetches_first_commit_diff_and_formats_issue_text():
    fake = _fake_pr_12()
    cfg = _cfg()
    with (
        patch("autoloop.jev_log.subprocess.run", side_effect=fake),
        patch("autoloop.jev_log.fetch_commit_diff", wraps=jev_log.fetch_commit_diff) as diff_spy,
    ):
        prs = discover_autoloop_prs(cfg)
        candidates, skips = plan_auto_merge_backfill(cfg, prs, [])

    assert skips == {}
    diff_spy.assert_called_once_with(cfg, "main", FIRST_SHA, cfg.diff_truncation)
    api_calls = [c for c in fake.calls if c[:2] == ["gh", "api"]]
    assert [c[-1] for c in api_calls] == [f"repos/acme-corp/widget/compare/main...{FIRST_SHA}"]
    (candidate,) = candidates
    assert candidate["issue_text"] == "Title: Add foo\n\nBody: Do the foo."
    assert candidate["diff"] == "diff --git a/foo.py b/foo.py\n+first"
    assert candidate["incumbent_call"]["approved"] is True


def test_fetch_commit_diff_truncates():
    fake = FakeGh(diffs={FIRST_SHA: "x" * 200})
    with patch("autoloop.jev_log.subprocess.run", side_effect=fake):
        assert fetch_commit_diff(_cfg(), "main", FIRST_SHA, 50) == "x" * 50
        assert fetch_commit_diff(_cfg(), "main", HEAD_SHA, 50) is None


def test_plan_skips_pr_without_commits_as_fetch_failed():
    fake = _fake_pr_12(commits={})
    with patch("autoloop.jev_log.subprocess.run", side_effect=fake):
        candidates, skips = plan_auto_merge_backfill(_cfg(), discover_autoloop_prs(_cfg()), [])
    assert candidates == []
    assert skips == {"fetch_failed": 1}


def test_format_issue_text():
    assert format_issue_text("T", "B") == "Title: T\n\nBody: B"
    assert format_issue_text("T", None) == "Title: T\n\nBody: "


def test_discover_ignores_non_autoloop_branches():
    fake = FakeGh(
        prs=[
            _pr(1, "feature/x"),
            _pr(2, "autoloop/abc-foo"),
            _pr(3, "fix/autoloop/4-foo"),
            _pr(12, "autoloop/12-foo"),
        ]
    )
    with patch("autoloop.jev_log.subprocess.run", side_effect=fake):
        prs = discover_autoloop_prs(_cfg())
    assert [(p["pr"], p["issue"]) for p in prs] == [(12, 12)]


def test_discover_raises_when_gh_fails():
    failed = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
    with (
        patch("autoloop.jev_log.subprocess.run", return_value=failed),
        pytest.raises(RuntimeError, match="boom"),
    ):
        discover_autoloop_prs(_cfg())


def test_parse_autoloop_branch():
    assert parse_autoloop_branch("autoloop/483-backfill") == 483
    assert parse_autoloop_branch("feature/x") is None
    assert parse_autoloop_branch("") is None


def test_load_decisions_skips_blank_and_malformed_lines(tmp_path):
    (tmp_path / "jev_decisions.jsonl").write_text('{"point": "triage"}\n\nnot json\n[1]\n')
    assert load_decisions(_make_ctx(tmp_path)) == [{"point": "triage"}]
    assert load_decisions(_make_ctx(tmp_path / "missing")) == []


def test_auto_merge_reads_repo_context_data_dir_not_repo_local(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    repo_local = tmp_path / "autoloop" / "jev_decisions.jsonl"
    repo_local.parent.mkdir()
    repo_local.write_text(json.dumps(_auto_merge_record(issue=12, pr=12)) + "\n")
    ctx = _make_ctx(tmp_path / "data")

    assert decisions_path(ctx) == tmp_path / "data" / "jev_decisions.jsonl"
    out = _run_dry(_fake_pr_12(), ctx, capsys)

    assert f"Decisions file: {tmp_path / 'data' / 'jev_decisions.jsonl'}" in out
    assert "PR #12 (" in out
    assert _skip_count(out, "already_recorded") == 0


def test_discover_maps_state_to_final_outcome_and_created_at():
    fake = FakeGh(
        prs=[
            _pr(1, "autoloop/1-a", state="MERGED", created_at="2026-09-01T00:00:00Z"),
            _pr(2, "autoloop/2-b", state="CLOSED"),
            _pr(3, "autoloop/3-c", state="OPEN"),
        ]
    )
    with patch("autoloop.jev_log.subprocess.run", side_effect=fake):
        prs = discover_autoloop_prs(_cfg())
    assert [p["final_outcome"] for p in prs] == ["merged", "closed_unmerged", None]
    assert prs[0]["created_at"] == "2026-09-01T00:00:00Z"
    assert "createdAt,state" in fake.calls[0][-1]


def test_plan_skips_open_pr():
    fake = _fake_pr_12(prs=[_pr(12, "autoloop/12-foo", state="OPEN")])
    with patch("autoloop.jev_log.subprocess.run", side_effect=fake):
        candidates, skips = plan_auto_merge_backfill(_cfg(), discover_autoloop_prs(_cfg()), [])
    assert candidates == []
    assert skips == {"open": 1}
    assert [c for c in fake.calls if c[:3] == ["gh", "pr", "view"]] == []


def test_backfill_cli_without_point_runs_triage(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path / "data")
    _write_triage_log(ctx, {"issue": 5, "jev": {"reason": "HTTP 403"}})
    script = _load_backfill_script()

    with (
        patch.object(script.subprocess, "run", return_value=ISSUE_VIEW_OK) as run_mock,
        patch.object(script, "backfill_auto_merge") as auto_merge_mock,
    ):
        code = script.main(["--dry-run"], ctx=ctx)

    assert code == 0
    auto_merge_mock.assert_not_called()
    assert run_mock.call_args.args[0][:4] == ["gh", "issue", "view", "5"]
    out = capsys.readouterr().out
    assert "Issues to backfill: 1" in out
    assert "#5: would call Jev triage (dry-run)" in out


# --- Triage backfill ---

ISSUE_VIEW_OK = subprocess.CompletedProcess([], 0, stdout='{"title": "t", "body": "b"}', stderr="")
TRIAGE_403 = {"issue": 5, "incumbent": {"verdict": "ready"}, "jev": {"reason": "HTTP 403"}}
UNCERTAIN_RESULT = {"well_formed": 0.5, "fallback": True, "reason": "uncertain band"}


def _write_triage_log(ctx, *records):
    log_file = decisions_path(ctx)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    log_file.write_text("".join(json.dumps(r) + "\n" for r in records))
    return log_file


def _run_triage(ctx, dry_run=False, jev_result=UNCERTAIN_RESULT):
    script = _load_backfill_script()
    with (
        patch.object(script.subprocess, "run", return_value=ISSUE_VIEW_OK) as run_mock,
        patch("autoloop.jev.triage", return_value=dict(jev_result)) as jev_mock,
    ):
        script.backfill_triage(ctx, dry_run)
    return run_mock, jev_mock


def test_triage_backfill_reads_and_appends_data_dir_not_repo_local(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path / "data")
    log_file = _write_triage_log(ctx, TRIAGE_403)

    _, jev_mock = _run_triage(ctx)

    jev_mock.assert_called_once_with("Title: t\n\nBody:\nb")
    lines = log_file.read_text().splitlines()
    assert len(lines) == 2
    record = json.loads(lines[1])
    assert record["issue"] == 5
    assert record["backfill"] is True
    assert record["incumbent"] == {"verdict": "ready"}
    assert record["jev"] == UNCERTAIN_RESULT
    assert f"Decisions file: {log_file}" in capsys.readouterr().out
    assert not (tmp_path / "autoloop" / "jev_decisions.jsonl").exists()


def test_triage_backfill_ignores_repo_local_log(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    repo_local = tmp_path / "autoloop" / "jev_decisions.jsonl"
    repo_local.parent.mkdir()
    repo_local.write_text(json.dumps(TRIAGE_403) + "\n")
    before = repo_local.read_bytes()

    run_mock, jev_mock = _run_triage(_make_ctx(tmp_path / "data"))

    assert "No jev_decisions.jsonl found" in capsys.readouterr().out
    run_mock.assert_not_called()
    jev_mock.assert_not_called()
    assert repo_local.read_bytes() == before


def test_triage_backfill_issue_view_uses_current_repo(tmp_path):
    ctx = _make_ctx(tmp_path / "data", repo_dir=tmp_path / "checkout")
    _write_triage_log(ctx, TRIAGE_403)

    run_mock, _ = _run_triage(ctx, dry_run=True)

    run_mock.assert_called_once()
    assert run_mock.call_args.args[0] == ["gh", "issue", "view", "5", "--json", "title,body"]
    assert "--repo" not in run_mock.call_args.args[0]
    assert run_mock.call_args.kwargs["cwd"] == (tmp_path / "checkout").resolve()


def test_triage_backfill_second_run_adds_nothing(tmp_path, capsys):
    ctx = _make_ctx(tmp_path / "data")
    log_file = _write_triage_log(ctx, TRIAGE_403)

    _run_triage(ctx)
    after_first = log_file.read_bytes()
    assert len(after_first.splitlines()) == 2
    assert json.loads(after_first.splitlines()[1])["jev"]["fallback"] is True

    run_mock, jev_mock = _run_triage(ctx)

    assert log_file.read_bytes() == after_first
    run_mock.assert_not_called()
    jev_mock.assert_not_called()
    assert "Nothing to backfill" in capsys.readouterr().out


def test_triage_backfill_skips_existing_fallback_backfill_record(tmp_path):
    ctx = _make_ctx(tmp_path / "data")
    earlier = {"issue": 5, "jev": {"fallback": True, "reason": "uncertain"}, "backfill": True}
    log_file = _write_triage_log(ctx, TRIAGE_403, earlier)
    before = log_file.read_bytes()

    run_mock, jev_mock = _run_triage(ctx)

    assert log_file.read_bytes() == before
    run_mock.assert_not_called()
    jev_mock.assert_not_called()


def test_triage_backfill_dry_run_writes_nothing(tmp_path, capsys):
    ctx = _make_ctx(tmp_path / "data")
    log_file = _write_triage_log(ctx, TRIAGE_403)
    before = log_file.read_bytes()

    _, jev_mock = _run_triage(ctx, dry_run=True)

    assert log_file.read_bytes() == before
    jev_mock.assert_not_called()
    out = capsys.readouterr().out
    assert "Issues to backfill: 1" in out
    assert "#5: would call Jev triage (dry-run)" in out


def test_is_triage_backfilled():
    script = _load_backfill_script()
    records = [
        {"issue": 5, "jev": {"fallback": True}, "backfill": True},
        {"issue": 6, "jev": {"reason": "HTTP 403"}},
        _auto_merge_record(issue=7, pr=8, backfill=True),
        {"point": "triage", "issue": 9, "jev_call": {}, "backfill": True},
    ]
    assert script.is_triage_backfilled(records, 5)
    assert not script.is_triage_backfilled(records, 6)
    assert not script.is_triage_backfilled(records, 7)
    assert script.is_triage_backfilled(records, 9)
