"""Tests for the eval command: deterministic performance tracking."""

from __future__ import annotations

import json
import subprocess

from autoloop.eval import (
    _detect_module_prefixes,
    _detect_post_merge_fixups,
    _extract_issue_from_branch,
    classify_module,
    compare_snapshots,
    compute_snapshot,
    enrich_pr_data_with_runs,
    fetch_pr_data,
    format_comparison,
    format_eval_md,
    format_snapshot,
    format_trend,
    load_all_snapshots,
    load_latest_snapshot,
    load_run_history,
    load_snapshot,
    main,
    save_snapshot,
)


# --- load_run_history ---


def test_load_run_history_empty_when_no_file(tmp_path):
    assert load_run_history(tmp_path) == []


def test_load_run_history_reads_entries(tmp_path):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    entries = [
        {"type": "implement", "issue": 1, "success": True, "attempts": 1},
        {"type": "implement", "issue": 2, "success": False, "attempts": 2},
    ]
    log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    result = load_run_history(tmp_path)
    assert len(result) == 2
    assert result[0]["issue"] == 1
    assert result[1]["issue"] == 2


def test_load_run_history_skips_blank_lines(tmp_path):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(json.dumps({"type": "implement", "issue": 1, "success": True}) + "\n\n")
    assert len(load_run_history(tmp_path)) == 1


# --- classify_module ---


def test_classify_module_matches_prefix():
    files = ["src/autoloop/eval.py", "tests/test_eval.py"]
    assert classify_module(files, ["src/autoloop/"]) == "src/autoloop/"


def test_classify_module_returns_other_when_no_match():
    files = ["README.md"]
    assert classify_module(files, ["src/autoloop/"]) == "other"


def test_classify_module_matches_first_file():
    files = ["src/patina/mcp/server.py", "src/patina/store/db.py"]
    prefixes = ["src/patina/mcp/", "src/patina/store/"]
    assert classify_module(files, prefixes) == "src/patina/mcp/"


def test_classify_module_auto_detects_prefixes():
    files = ["src/autoloop/eval.py", "src/autoloop/cli.py"]
    assert classify_module(files) == "src/autoloop/"


# --- _detect_module_prefixes ---


def test_detect_module_prefixes_groups_by_directory():
    files = [
        "src/autoloop/eval.py",
        "src/autoloop/cli.py",
        "src/patina/mcp/server.py",
    ]
    prefixes = _detect_module_prefixes(files)
    assert "src/autoloop/" in prefixes or "src/patina/mcp/" in prefixes


def test_detect_module_prefixes_empty_for_flat_files():
    files = ["README.md", "pyproject.toml"]
    assert _detect_module_prefixes(files) == []


# --- _extract_issue_from_branch ---


def test_extract_issue_from_branch_standard():
    assert _extract_issue_from_branch("autoloop/42-feat-something") == 42


def test_extract_issue_from_branch_no_number():
    assert _extract_issue_from_branch("autoloop/feat-something") is None


def test_extract_issue_from_branch_multiple_numbers():
    assert _extract_issue_from_branch("autoloop/154-feat-add-v2") == 154


# --- compute_snapshot ---


def test_compute_snapshot_basic():
    runs = [
        {
            "type": "implement",
            "issue": 1,
            "success": True,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 120,
        },
        {
            "type": "implement",
            "issue": 2,
            "success": True,
            "attempts": 2,
            "cost_usd": 2.0,
            "duration_seconds": 300,
        },
        {
            "type": "implement",
            "issue": 3,
            "success": False,
            "attempts": 3,
            "cost_usd": 3.0,
            "duration_seconds": 600,
        },
    ]
    snap = compute_snapshot(runs, date="2026-09-14")
    assert snap["total_implementations"] == 3
    assert snap["first_attempt_rate"] == 0.33
    assert snap["avg_cost_usd"] == 2.0
    assert snap["avg_duration_seconds"] == 340
    assert snap["attempt_distribution"] == {"1": 1, "2": 1, "3+": 1}


def test_compute_snapshot_filters_non_implement():
    runs = [
        {
            "type": "implement",
            "issue": 1,
            "success": True,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        },
        {
            "type": "triage",
            "issue": 2,
            "success": True,
            "attempts": 1,
            "cost_usd": 0.1,
            "duration_seconds": 10,
        },
        {
            "type": "review",
            "pr_number": 5,
            "success": True,
            "cost_usd": 0.2,
            "duration_seconds": 30,
        },
    ]
    snap = compute_snapshot(runs, date="2026-09-14")
    assert snap["total_implementations"] == 1
    assert snap["first_attempt_rate"] == 1.0


def test_compute_snapshot_empty_runs():
    snap = compute_snapshot([], date="2026-09-14")
    assert snap["total_implementations"] == 0
    assert snap["first_attempt_rate"] == 0.0
    assert snap["avg_cost_usd"] == 0.0


def test_compute_snapshot_with_pr_data():
    runs = [
        {
            "type": "implement",
            "issue": 1,
            "success": True,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        },
    ]
    pr_data = [
        {
            "number": 10,
            "merged": True,
            "closed": False,
            "changed_files": ["src/autoloop/eval.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 1,
        },
        {
            "number": 11,
            "merged": True,
            "closed": False,
            "changed_files": ["src/autoloop/cli.py"],
            "human_edited": True,
            "first_attempt_success": True,
            "issue": 2,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-14")
    assert snap["human_edit_rate"] == 0.5
    assert snap["human_edit_count"] == 1
    assert snap["merged_pr_count"] == 2
    assert "src/autoloop/" in snap["modules"]


def test_compute_snapshot_closed_without_merge():
    runs = []
    pr_data = [
        {
            "number": 10,
            "merged": False,
            "closed": True,
            "changed_files": [],
            "human_edited": False,
            "first_attempt_success": False,
            "issue": 1,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-14")
    assert snap["closed_without_merge"] == 1


def test_compute_snapshot_default_type_is_implement():
    runs = [{"issue": 1, "success": True, "attempts": 1, "cost_usd": 1.0, "duration_seconds": 60}]
    snap = compute_snapshot(runs, date="2026-09-14")
    assert snap["total_implementations"] == 1


# --- enrich_pr_data_with_runs ---


def test_enrich_pr_data_marks_multi_attempt():
    pr_data = [
        {"number": 10, "issue": 1, "first_attempt_success": True},
        {"number": 11, "issue": 2, "first_attempt_success": True},
    ]
    runs = [
        {"type": "implement", "issue": 1, "attempts": 1},
        {"type": "implement", "issue": 2, "attempts": 3},
    ]
    enriched = enrich_pr_data_with_runs(pr_data, runs)
    assert enriched[0]["first_attempt_success"] is True
    assert enriched[1]["first_attempt_success"] is False


def test_enrich_pr_data_no_matching_run():
    pr_data = [{"number": 10, "issue": 99, "first_attempt_success": True}]
    runs = [{"type": "implement", "issue": 1, "attempts": 2}]
    enriched = enrich_pr_data_with_runs(pr_data, runs)
    assert enriched[0]["first_attempt_success"] is True


# --- save_snapshot / load_snapshot ---


def test_save_and_load_snapshot(tmp_path):
    snap = {"date": "2026-09-14", "total_implementations": 10, "first_attempt_rate": 0.9}
    path = save_snapshot(snap, tmp_path)
    assert path.exists()
    assert path.name == "2026-09-14.json"

    loaded = load_snapshot("2026-09-14", tmp_path)
    assert loaded["total_implementations"] == 10


def test_load_snapshot_returns_none_for_missing(tmp_path):
    assert load_snapshot("2099-01-01", tmp_path) is None


def test_load_latest_snapshot(tmp_path):
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "2026-09-13.json").write_text(json.dumps({"date": "2026-09-13"}))
    (snap_dir / "2026-09-14.json").write_text(json.dumps({"date": "2026-09-14"}))

    latest = load_latest_snapshot(tmp_path)
    assert latest["date"] == "2026-09-14"


def test_load_latest_snapshot_none_when_empty(tmp_path):
    assert load_latest_snapshot(tmp_path) is None


def test_load_all_snapshots(tmp_path):
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "2026-09-13.json").write_text(json.dumps({"date": "2026-09-13"}))
    (snap_dir / "2026-09-14.json").write_text(json.dumps({"date": "2026-09-14"}))

    all_snaps = load_all_snapshots(tmp_path)
    assert len(all_snaps) == 2
    assert all_snaps[0]["date"] == "2026-09-13"


def test_load_all_snapshots_empty(tmp_path):
    assert load_all_snapshots(tmp_path) == []


# --- compare_snapshots ---


def test_compare_snapshots_basic():
    old = {
        "date": "2026-09-13",
        "total_implementations": 30,
        "first_attempt_rate": 0.86,
        "avg_cost_usd": 1.15,
        "avg_duration_seconds": 280,
        "human_edit_rate": 0.10,
        "modules": {
            "src/autoloop/": {"implementations": 10, "first_attempt_rate": 0.8},
        },
    }
    new = {
        "date": "2026-09-14",
        "total_implementations": 35,
        "first_attempt_rate": 0.89,
        "avg_cost_usd": 1.12,
        "avg_duration_seconds": 262,
        "human_edit_rate": 0.08,
        "modules": {
            "src/autoloop/": {"implementations": 12, "first_attempt_rate": 0.9},
        },
    }
    comp = compare_snapshots(old, new)
    assert comp["old_date"] == "2026-09-13"
    assert comp["new_date"] == "2026-09-14"
    assert comp["changes"]["first_attempt_rate"]["delta"] == 0.03
    assert comp["changes"]["avg_cost_usd"]["delta"] == -0.03
    assert comp["changes"]["total_implementations"]["delta"] == 5
    assert comp["module_changes"]["src/autoloop/"]["old_rate"] == 0.8
    assert comp["module_changes"]["src/autoloop/"]["new_rate"] == 0.9


def test_compare_snapshots_new_module():
    old = {
        "date": "2026-09-13",
        "modules": {},
        "first_attempt_rate": 0.8,
        "avg_cost_usd": 1.0,
        "avg_duration_seconds": 100,
        "human_edit_rate": 0.1,
        "total_implementations": 5,
    }
    new = {
        "date": "2026-09-14",
        "modules": {"src/new/": {"implementations": 3, "first_attempt_rate": 1.0}},
        "first_attempt_rate": 0.85,
        "avg_cost_usd": 0.9,
        "avg_duration_seconds": 90,
        "human_edit_rate": 0.05,
        "total_implementations": 8,
    }
    comp = compare_snapshots(old, new)
    assert "src/new/" in comp["module_changes"]
    assert comp["module_changes"]["src/new/"]["old_rate"] == 0
    assert comp["module_changes"]["src/new/"]["new_rate"] == 1.0


# --- format_snapshot ---


def test_format_snapshot_output():
    snap = {
        "date": "2026-09-14",
        "total_implementations": 35,
        "first_attempt_rate": 0.89,
        "avg_cost_usd": 1.12,
        "avg_duration_seconds": 262,
        "attempt_distribution": {"1": 31, "2": 3, "3+": 1},
        "human_edit_rate": 0.08,
        "human_edit_count": 3,
        "merged_pr_count": 38,
        "closed_without_merge": 0,
        "modules": {
            "src/mcp/": {"implementations": 12, "first_attempt_rate": 1.0},
            "src/ingest/": {"implementations": 5, "first_attempt_rate": 0.6},
        },
    }
    output = format_snapshot(snap)
    assert "Eval Snapshot (2026-09-14)" in output
    assert "Issues implemented:     35" in output
    assert "89%" in output
    assert "$1.12" in output
    assert "4m 22s" in output
    assert "1-try: 31" in output
    assert "3/38 PRs had post-merge fixups" in output
    assert "src/ingest/" in output
    assert "src/mcp/" in output


def test_format_snapshot_no_modules():
    snap = {
        "date": "2026-09-14",
        "total_implementations": 0,
        "first_attempt_rate": 0.0,
        "avg_cost_usd": 0.0,
        "avg_duration_seconds": 0,
        "attempt_distribution": {},
        "human_edit_rate": 0.0,
        "human_edit_count": 0,
        "merged_pr_count": 0,
        "closed_without_merge": 0,
        "modules": {},
    }
    output = format_snapshot(snap)
    assert "Eval Snapshot" in output
    assert "Retry hotspots:" not in output


# --- format_comparison ---


def test_format_comparison_output():
    comp = {
        "old_date": "2026-09-13",
        "new_date": "2026-09-14",
        "changes": {
            "first_attempt_rate": {"old": 0.86, "new": 0.89, "delta": 0.03},
            "avg_cost_usd": {"old": 1.15, "new": 1.12, "delta": -0.03},
            "avg_duration_seconds": {"old": 280, "new": 262, "delta": -18},
            "human_edit_rate": {"old": 0.10, "new": 0.08, "delta": -0.02},
            "total_implementations": {"old": 30, "new": 35, "delta": 5},
        },
        "module_changes": {
            "src/autoloop/": {"old_rate": 0.5, "new_rate": 0.6, "old_impl": 5, "new_impl": 6},
        },
    }
    output = format_comparison(comp)
    assert "2026-09-13 → 2026-09-14" in output
    assert "86%" in output
    assert "89%" in output
    assert "improving" in output


# --- format_trend ---


def test_format_trend_multiple_snapshots():
    snaps = [
        {
            "date": "2026-09-12",
            "total_implementations": 30,
            "first_attempt_rate": 0.80,
            "avg_cost_usd": 1.20,
            "human_edit_rate": 0.12,
        },
        {
            "date": "2026-09-13",
            "total_implementations": 33,
            "first_attempt_rate": 0.85,
            "avg_cost_usd": 1.15,
            "human_edit_rate": 0.10,
        },
        {
            "date": "2026-09-14",
            "total_implementations": 35,
            "first_attempt_rate": 0.89,
            "avg_cost_usd": 1.12,
            "human_edit_rate": 0.08,
        },
    ]
    output = format_trend(snaps)
    assert "Eval Trend" in output
    assert "2026-09-12" in output
    assert "2026-09-14" in output
    assert "Overall:" in output


def test_format_trend_empty():
    assert format_trend([]) == "No snapshots found."


def test_format_trend_single_snapshot():
    snaps = [
        {
            "date": "2026-09-14",
            "total_implementations": 10,
            "first_attempt_rate": 0.9,
            "avg_cost_usd": 1.0,
            "avg_duration_seconds": 60,
            "attempt_distribution": {},
            "human_edit_rate": 0.0,
            "human_edit_count": 0,
            "closed_without_merge": 0,
            "modules": {},
        }
    ]
    output = format_trend(snaps)
    assert "Eval Snapshot" in output


# --- main ---


def test_main_snapshot(tmp_path, monkeypatch, capsys):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 120,
            }
        )
        + "\n"
    )

    main(base=tmp_path)

    out = capsys.readouterr().out
    assert "Eval Snapshot" in out
    assert "Issues implemented:     1" in out

    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    assert snap_dir.exists()
    files = list(snap_dir.glob("*.json"))
    assert len(files) == 1


def test_main_trend(tmp_path, capsys):
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    for d in ("2026-09-12", "2026-09-13"):
        (snap_dir / f"{d}.json").write_text(
            json.dumps(
                {
                    "date": d,
                    "total_implementations": 10,
                    "first_attempt_rate": 0.9,
                    "avg_cost_usd": 1.0,
                    "human_edit_rate": 0.0,
                }
            )
        )

    main(trend=True, base=tmp_path)

    out = capsys.readouterr().out
    assert "Eval Trend" in out


def test_main_compare_latest_no_previous(tmp_path, capsys):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    main(compare="latest", base=tmp_path)

    out = capsys.readouterr().out
    assert "No previous snapshot" in out
    assert "Eval Snapshot" in out


def test_main_compare_latest_with_previous(tmp_path, capsys):
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "2026-09-13.json").write_text(
        json.dumps(
            {
                "date": "2026-09-13",
                "total_implementations": 10,
                "first_attempt_rate": 0.8,
                "avg_cost_usd": 1.5,
                "avg_duration_seconds": 200,
                "human_edit_rate": 0.1,
                "modules": {},
            }
        )
    )

    log_dir = tmp_path / "autoloop"
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    main(compare="latest", base=tmp_path)

    out = capsys.readouterr().out
    assert "Comparison:" in out
    assert "Snapshot saved" in out


# --- CLI integration ---


def test_cli_eval_subparser():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval"])
    assert args.command == "eval"
    assert args.compare is None
    assert args.trend is False


def test_cli_eval_compare_flag():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "--compare", "latest"])
    assert args.compare == "latest"


def test_cli_eval_compare_no_value():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "--compare"])
    assert args.compare == "latest"


def test_cli_eval_trend_flag():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "--trend"])
    assert args.trend is True


def test_cli_eval_output_json_flag():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "--output", "json"])
    assert args.output == "json"


def test_cli_eval_output_default_is_none():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval"])
    assert args.output is None


# --- _detect_post_merge_fixups ---


def test_detect_post_merge_fixups_finds_fixup():
    autoloop_prs = [
        ({"number": 10, "mergedAt": "2026-09-10T00:00:00Z"}, {"src/autoloop/eval.py"}),
    ]
    non_autoloop_merged = [
        {"merged_at": "2026-09-12T00:00:00Z", "files": {"src/autoloop/eval.py"}},
    ]
    fixups = _detect_post_merge_fixups(autoloop_prs, non_autoloop_merged)
    assert 10 in fixups


def test_detect_post_merge_fixups_no_overlap():
    autoloop_prs = [
        ({"number": 10, "mergedAt": "2026-09-10T00:00:00Z"}, {"src/autoloop/eval.py"}),
    ]
    non_autoloop_merged = [
        {"merged_at": "2026-09-12T00:00:00Z", "files": {"src/autoloop/cli.py"}},
    ]
    fixups = _detect_post_merge_fixups(autoloop_prs, non_autoloop_merged)
    assert len(fixups) == 0


def test_detect_post_merge_fixups_ignores_earlier_merge():
    autoloop_prs = [
        ({"number": 10, "mergedAt": "2026-09-12T00:00:00Z"}, {"src/autoloop/eval.py"}),
    ]
    non_autoloop_merged = [
        {"merged_at": "2026-09-10T00:00:00Z", "files": {"src/autoloop/eval.py"}},
    ]
    fixups = _detect_post_merge_fixups(autoloop_prs, non_autoloop_merged)
    assert len(fixups) == 0


def test_detect_post_merge_fixups_skips_unmerged():
    autoloop_prs = [
        ({"number": 10, "mergedAt": None}, {"src/autoloop/eval.py"}),
    ]
    non_autoloop_merged = [
        {"merged_at": "2026-09-12T00:00:00Z", "files": {"src/autoloop/eval.py"}},
    ]
    fixups = _detect_post_merge_fixups(autoloop_prs, non_autoloop_merged)
    assert len(fixups) == 0


# --- fetch_pr_data FileNotFoundError ---


def test_fetch_pr_data_returns_empty_on_missing_gh(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    assert fetch_pr_data("owner/repo") == []


def test_fetch_pr_data_warns_when_limit_reached(monkeypatch, capsys):
    from autoloop.eval import _PR_LIMIT

    prs = [
        {
            "number": i,
            "state": "MERGED",
            "mergedAt": "2026-09-10T00:00:00Z",
            "closedAt": None,
            "headRefName": f"autoloop/{i}-feat-thing",
            "files": [{"path": "src/autoloop/eval.py"}],
            "author": {"login": "bot"},
        }
        for i in range(_PR_LIMIT)
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(prs), stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    fetch_pr_data("owner/repo")

    err = capsys.readouterr().err
    assert "500 PR limit reached" in err
    assert "oldest PRs excluded from eval" in err


def test_fetch_pr_data_no_warning_under_limit(monkeypatch, capsys):
    prs = [
        {
            "number": i,
            "state": "MERGED",
            "mergedAt": "2026-09-10T00:00:00Z",
            "closedAt": None,
            "headRefName": f"autoloop/{i}-feat-thing",
            "files": [{"path": "src/autoloop/eval.py"}],
            "author": {"login": "bot"},
        }
        for i in range(10)
    ]

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(prs), stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    fetch_pr_data("owner/repo")

    err = capsys.readouterr().err
    assert err == ""


# --- first_attempt_success default ---


def test_enrich_pr_data_no_matching_run_keeps_false():
    pr_data = [{"number": 10, "issue": 99, "first_attempt_success": False}]
    runs = [{"type": "implement", "issue": 1, "attempts": 2}]
    enriched = enrich_pr_data_with_runs(pr_data, runs)
    assert enriched[0]["first_attempt_success"] is False


# --- format_comparison dollar format ---


def test_format_comparison_dollar_shows_percentage():
    comp = {
        "old_date": "2026-09-13",
        "new_date": "2026-09-14",
        "changes": {
            "first_attempt_rate": {"old": 0.86, "new": 0.89, "delta": 0.03},
            "avg_cost_usd": {"old": 1.15, "new": 1.12, "delta": -0.03},
            "avg_duration_seconds": {"old": 280, "new": 262, "delta": -18},
            "human_edit_rate": {"old": 0.10, "new": 0.08, "delta": -0.02},
            "total_implementations": {"old": 30, "new": 35, "delta": 5},
        },
        "module_changes": {},
    }
    output = format_comparison(comp)
    assert "$1.15" in output
    assert "$1.12" in output
    assert "-3%" in output
    assert "-0%" not in output


def test_format_comparison_dollar_zero_old():
    comp = {
        "old_date": "2026-09-13",
        "new_date": "2026-09-14",
        "changes": {
            "first_attempt_rate": {"old": 0.0, "new": 0.5, "delta": 0.5},
            "avg_cost_usd": {"old": 0.0, "new": 1.0, "delta": 1.0},
            "avg_duration_seconds": {"old": 0, "new": 100, "delta": 100},
            "human_edit_rate": {"old": 0.0, "new": 0.1, "delta": 0.1},
            "total_implementations": {"old": 0, "new": 5, "delta": 5},
        },
        "module_changes": {},
    }
    output = format_comparison(comp)
    assert "$1.00" in output


# --- format_snapshot uses merged_pr_count ---


def test_format_snapshot_uses_merged_pr_count():
    snap = {
        "date": "2026-09-14",
        "total_implementations": 35,
        "first_attempt_rate": 0.89,
        "avg_cost_usd": 1.12,
        "avg_duration_seconds": 262,
        "attempt_distribution": {},
        "human_edit_rate": 0.1,
        "human_edit_count": 4,
        "merged_pr_count": 40,
        "closed_without_merge": 0,
        "modules": {},
    }
    output = format_snapshot(snap)
    assert "4/40 PRs had post-merge fixups" in output
    assert "4/35" not in output


# --- --output json ---


def test_main_output_json_snapshot(tmp_path, capsys):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 120,
            }
        )
        + "\n"
    )

    main(base=tmp_path, output="json")

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["total_implementations"] == 1
    assert data["first_attempt_rate"] == 1.0
    assert "Eval Snapshot" not in out
    assert "Snapshot saved" not in out


def test_main_output_json_trend(tmp_path, capsys):
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    for d in ("2026-09-12", "2026-09-13"):
        (snap_dir / f"{d}.json").write_text(
            json.dumps(
                {
                    "date": d,
                    "total_implementations": 10,
                    "first_attempt_rate": 0.9,
                    "avg_cost_usd": 1.0,
                    "human_edit_rate": 0.0,
                }
            )
        )

    main(trend=True, base=tmp_path, output="json")

    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 2
    assert data[0]["date"] == "2026-09-12"
    assert "Eval Trend" not in out


def test_main_output_json_compare_no_previous(tmp_path, capsys):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    main(compare="latest", base=tmp_path, output="json")

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["total_implementations"] == 1
    assert "No previous snapshot" not in out


def test_main_output_json_compare_with_previous(tmp_path, capsys):
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "2026-09-13.json").write_text(
        json.dumps(
            {
                "date": "2026-09-13",
                "total_implementations": 10,
                "first_attempt_rate": 0.8,
                "avg_cost_usd": 1.5,
                "avg_duration_seconds": 200,
                "human_edit_rate": 0.1,
                "modules": {},
            }
        )
    )

    log_dir = tmp_path / "autoloop"
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 60,
            }
        )
        + "\n"
    )

    main(compare="latest", base=tmp_path, output="json")

    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["old_date"] == "2026-09-13"
    assert "changes" in data
    assert "Snapshot saved" not in out


def test_main_default_output_unchanged(tmp_path, capsys):
    """Default (no --output) still produces human-readable format."""
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text(
        json.dumps(
            {
                "type": "implement",
                "issue": 1,
                "success": True,
                "attempts": 1,
                "cost_usd": 1.0,
                "duration_seconds": 120,
            }
        )
        + "\n"
    )

    main(base=tmp_path)

    out = capsys.readouterr().out
    assert "Eval Snapshot" in out
    assert "Snapshot saved" in out


# --- format_eval_md ---


def test_format_eval_md_basic():
    snap = {
        "date": "2026-09-14",
        "total_implementations": 35,
        "first_attempt_rate": 0.89,
        "avg_cost_usd": 1.12,
        "avg_duration_seconds": 262,
        "attempt_distribution": {"1": 31, "2": 3, "3+": 1},
        "human_edit_rate": 0.08,
        "human_edit_count": 3,
        "merged_pr_count": 38,
        "closed_without_merge": 0,
        "modules": {
            "src/mcp/": {"implementations": 12, "first_attempt_rate": 1.0},
            "src/ingest/": {"implementations": 5, "first_attempt_rate": 0.6},
        },
    }
    output = format_eval_md(snap)
    assert "# Eval Report" in output
    assert "**Date:** 2026-09-14" in output
    assert "| Issues implemented | 35 |" in output
    assert "89%" in output
    assert "$1.12" in output
    assert "4m 22s" in output
    assert "## Attempt Distribution" in output
    assert "1-try: 31" in output
    assert "## Module Performance" in output
    assert "src/ingest/" in output
    assert "src/mcp/" in output


def test_format_eval_md_no_modules():
    snap = {
        "date": "2026-09-14",
        "total_implementations": 0,
        "first_attempt_rate": 0.0,
        "avg_cost_usd": 0.0,
        "avg_duration_seconds": 0,
        "attempt_distribution": {},
        "human_edit_rate": 0.0,
        "human_edit_count": 0,
        "merged_pr_count": 0,
        "closed_without_merge": 0,
        "modules": {},
    }
    output = format_eval_md(snap)
    assert "# Eval Report" in output
    assert "## Module Performance" not in output
    assert "## Attempt Distribution" not in output


def test_format_eval_md_closed_without_merge():
    snap = {
        "date": "2026-09-14",
        "total_implementations": 5,
        "first_attempt_rate": 0.8,
        "avg_cost_usd": 1.0,
        "avg_duration_seconds": 120,
        "attempt_distribution": {"1": 4, "2": 1},
        "human_edit_rate": 0.0,
        "human_edit_count": 0,
        "merged_pr_count": 4,
        "closed_without_merge": 2,
        "modules": {},
    }
    output = format_eval_md(snap)
    assert "Closed without merge | 2" in output
