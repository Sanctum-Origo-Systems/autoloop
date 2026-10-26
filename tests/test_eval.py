"""Tests for the eval command: deterministic performance tracking."""

from __future__ import annotations

import json
import subprocess

from autoloop.config import RepoContext
from autoloop.eval import (
    _compute_period_stats,
    _detect_module_prefixes,
    _detect_post_merge_fixups,
    _extract_issue_from_branch,
    _md_label,
    _publish_via_pr,
    _sample_for_chart,
    classify_module,
    compare_snapshots,
    compute_snapshot,
    enrich_pr_data_with_runs,
    fetch_pr_data,
    format_comparison,
    format_cost_with_n,
    format_eval_md,
    format_rate_with_n,
    format_snapshot,
    format_trend,
    generate_eval_md,
    is_auto_merge_ready,
    load_all_snapshots,
    load_latest_snapshot,
    load_run_history,
    load_snapshot,
    main,
    material_change,
    migrate_unpublished_snapshots,
    module_implement_runs,
    save_snapshot,
    snapshot_implement_runs,
    snapshot_total_prs,
)


def _make_ctx(tmp_path):
    ctx = object.__new__(RepoContext)
    ctx.repo_dir = tmp_path
    ctx.data_dir = tmp_path / "autoloop"
    ctx.worktree_dir = ctx.data_dir / "worktrees"
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    return ctx


# --- load_run_history ---


def test_load_run_history_empty_when_no_file(tmp_path):
    ctx = _make_ctx(tmp_path)
    assert load_run_history(ctx) == []


def test_load_run_history_reads_entries(tmp_path):
    ctx = _make_ctx(tmp_path)
    log_file = ctx.data_dir / "run_history.jsonl"
    entries = [
        {"type": "implement", "issue": 1, "success": True, "attempts": 1},
        {"type": "implement", "issue": 2, "success": False, "attempts": 2},
    ]
    log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    result = load_run_history(ctx)
    assert len(result) == 2
    assert result[0]["issue"] == 1
    assert result[1]["issue"] == 2


def test_load_run_history_skips_blank_lines(tmp_path):
    ctx = _make_ctx(tmp_path)
    log_file = ctx.data_dir / "run_history.jsonl"
    log_file.write_text(json.dumps({"type": "implement", "issue": 1, "success": True}) + "\n\n")
    assert len(load_run_history(ctx)) == 1


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
    assert snap["modules"]["src/autoloop/"]["merged_clean_count"] == 1


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
    assert snap["total_implementations"] == 0


def test_compute_snapshot_total_implementations_excludes_closed_without_merge():
    runs = []
    pr_data = [
        {
            "number": 10,
            "merged": True,
            "closed": False,
            "changed_files": ["src/a/f.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 1,
        },
        {
            "number": 11,
            "merged": False,
            "closed": True,
            "changed_files": [],
            "human_edited": False,
            "first_attempt_success": False,
            "issue": 2,
        },
        {
            "number": 12,
            "merged": False,
            "closed": False,
            "changed_files": ["src/a/g.py"],
            "human_edited": False,
            "first_attempt_success": False,
            "issue": 3,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-14")
    assert snap["total_implementations"] == 2
    assert snap["merged_pr_count"] == 1
    assert snap["closed_without_merge"] == 1


def test_compute_snapshot_default_type_is_implement():
    runs = [{"issue": 1, "success": True, "attempts": 1, "cost_usd": 1.0, "duration_seconds": 60}]
    snap = compute_snapshot(runs, date="2026-09-14")
    assert snap["total_implementations"] == 1


def test_compute_snapshot_excludes_triage_and_issue_zero():
    """Mixed history: only real implementations (type=implement, issue!=0) are counted."""
    runs = [
        {
            "type": "implement",
            "issue": 42,
            "success": True,
            "attempts": 1,
            "cost_usd": 2.50,
            "duration_seconds": 300,
        },
        {
            "type": "implement",
            "issue": 99,
            "success": True,
            "attempts": 2,
            "cost_usd": 3.00,
            "duration_seconds": 400,
        },
        {
            "type": "triage",
            "issue": 0,
            "success": True,
            "attempts": 0,
            "issues_triaged": 3,
            "cost_usd": 0.10,
            "duration_seconds": 15,
        },
        {
            "issue": 0,
            "success": True,
            "attempts": 5,
            "cost_usd": 0.20,
            "duration_seconds": 25,
        },
        {
            "type": "review",
            "issue": 0,
            "success": True,
            "cost_usd": 0.30,
            "duration_seconds": 30,
        },
    ]
    snap = compute_snapshot(runs, date="2026-10-26")
    assert snap["total_implementations"] == 2
    assert snap["first_attempt_rate"] == 0.5
    assert snap["avg_cost_usd"] == 2.75
    assert snap["avg_duration_seconds"] == 350
    assert snap["attempt_distribution"] == {"1": 1, "2": 1, "3+": 0}


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
    ctx = _make_ctx(tmp_path)
    snap = {"date": "2026-09-14", "total_implementations": 10, "first_attempt_rate": 0.9}
    path = save_snapshot(snap, ctx)
    assert path.exists()
    assert path.name == "2026-09-14.json"

    loaded = load_snapshot("2026-09-14", ctx)
    assert loaded["total_implementations"] == 10


def test_load_snapshot_returns_none_for_missing(tmp_path):
    ctx = _make_ctx(tmp_path)
    assert load_snapshot("2099-01-01", ctx) is None


def test_load_latest_snapshot(tmp_path):
    ctx = _make_ctx(tmp_path)
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "2026-09-13.json").write_text(json.dumps({"date": "2026-09-13"}))
    (snap_dir / "2026-09-14.json").write_text(json.dumps({"date": "2026-09-14"}))

    latest = load_latest_snapshot(ctx)
    assert latest["date"] == "2026-09-14"


def test_load_latest_snapshot_none_when_empty(tmp_path):
    ctx = _make_ctx(tmp_path)
    assert load_latest_snapshot(ctx) is None


def test_load_all_snapshots(tmp_path):
    ctx = _make_ctx(tmp_path)
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    (snap_dir / "2026-09-13.json").write_text(json.dumps({"date": "2026-09-13"}))
    (snap_dir / "2026-09-14.json").write_text(json.dumps({"date": "2026-09-14"}))

    all_snaps = load_all_snapshots(ctx)
    assert len(all_snaps) == 2
    assert all_snaps[0]["date"] == "2026-09-13"


def test_load_all_snapshots_empty(tmp_path):
    ctx = _make_ctx(tmp_path)
    assert load_all_snapshots(ctx) == []


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
    assert "Total PRs:              35" in output
    assert "(31/35 implement runs)" in output
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
            "total_prs": {"old": 30, "new": 35, "delta": 5},
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

    main(ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "Eval Snapshot" in out
    assert "Total PRs:              0" in out
    assert "(1/1 implement runs)" in out

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

    main(trend=True, ctx=_make_ctx(tmp_path))

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

    main(compare="latest", ctx=_make_ctx(tmp_path))

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

    main(compare="latest", ctx=_make_ctx(tmp_path))

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
            "total_prs": {"old": 30, "new": 35, "delta": 5},
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
            "total_prs": {"old": 0, "new": 5, "delta": 5},
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

    main(ctx=_make_ctx(tmp_path), output="json")

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

    main(trend=True, ctx=_make_ctx(tmp_path), output="json")

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

    main(compare="latest", ctx=_make_ctx(tmp_path), output="json")

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

    main(compare="latest", ctx=_make_ctx(tmp_path), output="json")

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

    main(ctx=_make_ctx(tmp_path))

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
    assert "| Total PRs | 35 |" in output
    assert "(31/35 implement runs)" in output
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


# --- is_auto_merge_ready ---


def test_auto_merge_ready_all_conditions_met():
    assert is_auto_merge_ready(0.91, 0.0, 10) == "Yes"


def test_auto_merge_ready_high_success_many_prs():
    assert is_auto_merge_ready(0.95, 0.0, 50) == "Yes"


def test_auto_merge_ready_boundary_success_at_90():
    assert is_auto_merge_ready(0.90, 0.0, 10) == "No"


def test_auto_merge_ready_boundary_success_above_90():
    assert is_auto_merge_ready(0.91, 0.0, 10) == "Yes"


def test_auto_merge_ready_edit_rate_below_threshold():
    assert is_auto_merge_ready(0.95, 0.03, 25) == "Yes"


def test_auto_merge_ready_edit_rate_above_threshold():
    assert is_auto_merge_ready(0.95, 0.10, 25) == "No"


def test_auto_merge_ready_boundary_edit_rate_just_below():
    assert is_auto_merge_ready(0.95, 0.049, 10) == "Yes"


def test_auto_merge_ready_boundary_edit_rate_at_threshold():
    assert is_auto_merge_ready(0.95, 0.05, 10) == "No"


def test_auto_merge_ready_boundary_edit_rate_zero():
    assert is_auto_merge_ready(0.95, 0.0, 10) == "Yes"


def test_auto_merge_ready_boundary_pr_count_below_floor():
    assert is_auto_merge_ready(0.95, 0.0, 9) == "No"


def test_auto_merge_ready_boundary_pr_count_at_floor():
    assert is_auto_merge_ready(0.95, 0.0, 10) == "Yes"


def test_auto_merge_ready_custom_edit_threshold():
    assert is_auto_merge_ready(0.95, 0.09, 10, edit_rate_threshold=0.10) == "Yes"
    assert is_auto_merge_ready(0.95, 0.10, 10, edit_rate_threshold=0.10) == "No"


def test_auto_merge_ready_custom_success_threshold():
    assert is_auto_merge_ready(0.86, 0.0, 10, success_threshold=0.85) == "Yes"
    assert is_auto_merge_ready(0.85, 0.0, 10, success_threshold=0.85) == "No"


def test_auto_merge_ready_custom_volume_floor():
    assert is_auto_merge_ready(0.95, 0.0, 5, volume_floor=5) == "Yes"
    assert is_auto_merge_ready(0.95, 0.0, 4, volume_floor=5) == "No"


def test_auto_merge_ready_all_fail():
    assert is_auto_merge_ready(0.50, 0.10, 5) == "No"


def test_auto_merge_ready_many_total_prs_few_clean_merged():
    """25 total PRs but only 8 cleanly merged — should not promote with default floor."""
    assert is_auto_merge_ready(0.95, 0.0, 8) == "No"


def test_auto_merge_ready_exactly_at_volume_floor():
    """Exactly at volume floor promotes regardless of closed/abandoned count."""
    assert is_auto_merge_ready(0.95, 0.0, 10) == "Yes"


def test_auto_merge_ready_gate_uses_merged_clean_count_in_eval_md():
    """generate_eval_md passes merged_clean_count, not implementations."""
    modules = {
        "src/mod/": {
            "implementations": 30,
            "first_attempt_rate": 0.95,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
            "merged_clean_count": 9,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap])
    assert "| src/mod/ | 95% (n=30 runs) | $1.00 (n=30 runs) | 30 | No |" in content


def test_auto_merge_ready_gate_passes_with_clean_merged_in_eval_md():
    """generate_eval_md shows Yes when merged_clean_count >= volume_floor (default 10)."""
    modules = {
        "src/mod/": {
            "implementations": 30,
            "first_attempt_rate": 0.95,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
            "merged_clean_count": 10,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap])
    assert "| src/mod/ | 95% (n=30 runs) | $1.00 (n=30 runs) | 30 | Yes |" in content


def test_compute_snapshot_merged_clean_count_excludes_human_edits():
    """merged_clean_count in module stats excludes PRs with post-merge fixups."""
    runs = [
        {
            "type": "implement",
            "issue": i,
            "success": True,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        }
        for i in range(1, 4)
    ]
    pr_data = [
        {
            "number": 10,
            "merged": True,
            "closed": False,
            "changed_files": ["src/a/f.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 1,
        },
        {
            "number": 11,
            "merged": True,
            "closed": False,
            "changed_files": ["src/a/g.py"],
            "human_edited": True,
            "first_attempt_success": True,
            "issue": 2,
        },
        {
            "number": 12,
            "merged": False,
            "closed": True,
            "changed_files": ["src/a/h.py"],
            "human_edited": False,
            "first_attempt_success": False,
            "issue": 3,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-14")
    mod_stats = snap["modules"]["src/a/"]
    assert mod_stats["merged_clean_count"] == 1


# --- generate_eval_md ---


def _make_snapshot(
    date="2026-09-14",
    rate=0.89,
    cost=1.12,
    hr=0.08,
    total=35,
    modules=None,
):
    first = round(rate * total)
    rest = total - first
    return {
        "date": date,
        "total_implementations": total,
        "first_attempt_rate": rate,
        "avg_cost_usd": cost,
        "human_edit_rate": hr,
        "attempt_distribution": {"1": first, "2": rest, "3+": 0},
        "human_edit_count": 0,
        "merged_pr_count": total,
        "modules": modules or {},
    }


def test_generate_eval_md_overall_stats():
    snap = _make_snapshot(rate=0.89, cost=1.12, hr=0.08)
    content = generate_eval_md(snap, [snap])
    assert "# EVAL Report" in content
    assert "## Overall" in content
    assert "| Total PRs | 35 |" in content
    assert "| First-attempt success | 89% (n=35 implement runs) |" in content
    assert "| Avg cost/run | $1.12 (n=35 implement runs) |" in content
    assert "| Human edit rate | 8% (n=35 merged PRs) |" in content


def test_generate_eval_md_module_table():
    modules = {
        "src/autoloop/": {
            "implementations": 25,
            "first_attempt_rate": 0.92,
            "avg_cost_usd": 1.05,
            "human_edit_rate": 0.0,
            "merged_clean_count": 25,
        },
        "src/other/": {
            "implementations": 10,
            "first_attempt_rate": 0.70,
            "avg_cost_usd": 2.0,
            "human_edit_rate": 0.1,
            "merged_clean_count": 9,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap])
    assert "## Per-Module Breakdown" in content
    assert "| Module | Success | Avg Cost | Impl | Auto-merge ready? |" in content
    assert "| src/autoloop/ | 92% (n=25 runs) | $1.05 (n=25 runs) | 25 | Yes |" in content
    assert (
        "| src/other/ | insufficient data (n=10 runs) | insufficient data (n=10 runs) | 10 | No |"
        in content
    )


def test_generate_eval_md_auto_merge_boundary_in_table():
    modules = {
        "src/a/": {
            "implementations": 10,
            "first_attempt_rate": 0.90,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
            "merged_clean_count": 10,
        },
        "src/b/": {
            "implementations": 10,
            "first_attempt_rate": 0.91,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
            "merged_clean_count": 10,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap], min_n=10)
    assert "| src/a/ | 90% (n=10 runs) | $1.00 (n=10 runs) | 10 | No |" in content
    assert "| src/b/ | 91% (n=10 runs) | $1.00 (n=10 runs) | 10 | Yes |" in content


def test_generate_eval_md_trend_table():
    snaps = [
        _make_snapshot(date="2026-08-24", rate=0.80, cost=1.50, hr=0.12, total=28),
        _make_snapshot(date="2026-08-31", rate=0.83, cost=1.35, hr=0.10, total=30),
        _make_snapshot(date="2026-09-07", rate=0.87, cost=1.20, hr=0.09, total=33),
        _make_snapshot(date="2026-09-14", rate=0.89, cost=1.12, hr=0.08, total=35),
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert "## Trend" in content
    assert "| 2026-08-24 |" in content
    assert "| 2026-09-14 |" in content


def test_generate_eval_md_trend_table_shows_all():
    snaps = [_make_snapshot(date=f"2026-09-{i:02d}") for i in range(1, 7)]
    content = generate_eval_md(snaps[-1], snaps)
    assert "2026-09-01" in content
    assert "2026-09-03" in content
    assert "2026-09-06" in content


def test_generate_eval_md_mermaid_success_chart():
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, cost=1.50),
            "attempt_distribution": {"1": 20, "2": 5, "3+": 0},
            "human_edit_count": 2,
            "merged_pr_count": 20,
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.83, cost=1.40),
            "attempt_distribution": {"1": 25, "2": 7, "3+": 0},
            "human_edit_count": 3,
            "merged_pr_count": 25,
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert "```mermaid" in content
    assert "xychart-beta" in content
    assert 'title "Weekly First-Attempt Success Rate (2026)"' in content
    assert '"9/7 (n=25)"' in content
    assert '"9/14 (n=7)"' in content
    assert 'y-axis "Success %" 0 --> 100' in content
    assert "bar [80, 71]" in content


def test_generate_eval_md_mermaid_cost_chart():
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, cost=1.50),
            "attempt_distribution": {"1": 20, "2": 5, "3+": 0},
            "human_edit_count": 2,
            "merged_pr_count": 20,
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.83, cost=1.40),
            "attempt_distribution": {"1": 25, "2": 7, "3+": 0},
            "human_edit_count": 3,
            "merged_pr_count": 25,
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert 'title "Weekly Avg Cost/PR (2026)"' in content
    assert 'y-axis "Cost ($)" 0 --> ' in content
    assert "bar [1.50, 1.04]" in content


def test_generate_eval_md_mermaid_pie_chart():
    modules = {
        "src/autoloop/": {
            "implementations": 25,
            "first_attempt_rate": 0.92,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
        },
        "src/other/": {
            "implementations": 10,
            "first_attempt_rate": 0.80,
            "avg_cost_usd": 1.5,
            "human_edit_rate": 0.05,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap])
    assert "pie title Attempt Distribution by Module" in content
    assert '"src/autoloop/ (92%, 25 impl)" : 25' in content
    assert '"src/other/ (80%, 10 impl)" : 10' in content


def test_generate_eval_md_no_modules_skips_module_sections():
    snap = _make_snapshot(modules={})
    content = generate_eval_md(snap, [snap])
    assert "## Per-Module Breakdown" not in content
    assert "pie title" not in content


def test_generate_eval_md_mermaid_charts_from_data():
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, cost=2.00),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
            "human_edit_count": 0,
            "merged_pr_count": 8,
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.75, cost=1.50),
            "attempt_distribution": {"1": 14, "2": 6, "3+": 0},
            "human_edit_count": 0,
            "merged_pr_count": 16,
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    # Per-period: first=80%, second=6/10=60%
    assert "bar [80, 60]" in content
    # Per-period cost: first=2.00, second=(1.50*20 - 2.00*10)/10 = 1.00
    assert "bar [2.00, 1.00]" in content


# --- repo-level promotion ---


def test_generate_eval_md_repo_level_shows_gate_section():
    """promotion_level='repo' adds a repo-level auto-merge gate section."""
    modules = {
        "src/a/": {
            "implementations": 8,
            "first_attempt_rate": 0.95,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
            "merged_clean_count": 5,
        },
        "src/b/": {
            "implementations": 7,
            "first_attempt_rate": 0.90,
            "avg_cost_usd": 1.5,
            "human_edit_rate": 0.0,
            "merged_clean_count": 5,
        },
    }
    snap = _make_snapshot(modules=modules, rate=0.93, hr=0.0, total=15)
    snap["merged_pr_count"] = 15
    snap["human_edit_count"] = 0
    content = generate_eval_md(snap, [snap], promotion_level="repo")
    assert "## Auto-Merge Gate (repo-level)" in content
    assert "| **Ready?** | **Yes** | |" in content
    assert "## Per-Module Breakdown" in content


def test_generate_eval_md_repo_level_not_ready():
    """Repo-level gate shows No when aggregate metrics don't pass."""
    modules = {
        "src/a/": {
            "implementations": 3,
            "first_attempt_rate": 0.80,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.1,
            "merged_clean_count": 2,
        },
    }
    snap = _make_snapshot(modules=modules, rate=0.80, hr=0.10, total=3)
    snap["merged_pr_count"] = 3
    snap["human_edit_count"] = 1
    content = generate_eval_md(snap, [snap], promotion_level="repo")
    assert "| **Ready?** | **No** | |" in content


def test_generate_eval_md_module_level_no_gate_section():
    """promotion_level='module' (default) does not show repo-level gate section."""
    modules = {
        "src/a/": {
            "implementations": 10,
            "first_attempt_rate": 0.95,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
            "merged_clean_count": 10,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap], promotion_level="module")
    assert "## Auto-Merge Gate (repo-level)" not in content


def test_generate_eval_md_repo_level_custom_thresholds():
    """Repo-level gate respects custom threshold parameters."""
    modules = {
        "src/a/": {
            "implementations": 5,
            "first_attempt_rate": 0.86,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
            "merged_clean_count": 5,
        },
    }
    snap = _make_snapshot(modules=modules, rate=0.86, hr=0.0, total=5)
    snap["merged_pr_count"] = 5
    snap["human_edit_count"] = 0
    content = generate_eval_md(
        snap,
        [snap],
        promotion_level="repo",
        success_threshold=0.85,
        volume_floor=5,
    )
    assert "| **Ready?** | **Yes** | |" in content


def test_generate_eval_md_module_level_custom_volume_floor():
    """Module-level gate respects custom volume_floor."""
    modules = {
        "src/a/": {
            "implementations": 5,
            "first_attempt_rate": 0.95,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
            "merged_clean_count": 5,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap], volume_floor=5, min_n=5)
    assert "| src/a/ | 95% (n=5 runs) | $1.00 (n=5 runs) | 5 | Yes |" in content

    content_strict = generate_eval_md(snap, [snap], volume_floor=20, min_n=5)
    assert "| src/a/ | 95% (n=5 runs) | $1.00 (n=5 runs) | 5 | No |" in content_strict


# --- compute_snapshot module enhancements ---


def test_compute_snapshot_module_avg_cost():
    runs = [
        {
            "type": "implement",
            "issue": 1,
            "success": True,
            "attempts": 1,
            "cost_usd": 2.0,
            "duration_seconds": 60,
        },
        {
            "type": "implement",
            "issue": 2,
            "success": True,
            "attempts": 1,
            "cost_usd": 4.0,
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
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 2,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-14")
    mod = snap["modules"]["src/autoloop/"]
    assert mod["avg_cost_usd"] == 3.0
    assert mod["human_edit_rate"] == 0.0


def test_compute_snapshot_module_human_edit_rate():
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
            "human_edited": True,
            "first_attempt_success": True,
            "issue": 1,
        },
        {
            "number": 11,
            "merged": True,
            "closed": False,
            "changed_files": ["src/autoloop/cli.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 2,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-14")
    mod = snap["modules"]["src/autoloop/"]
    assert mod["human_edit_rate"] == 0.5


# --- main with --publish ---


def _fake_subprocess_on_main(calls=None):
    """Return a fake subprocess.run that reports branch as 'main'."""
    if calls is None:
        calls = []

    def _fake(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n")
        return subprocess.CompletedProcess(cmd, 0)

    return _fake


def test_main_publish_writes_snapshot(tmp_path, monkeypatch, capsys):
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

    calls = []
    monkeypatch.setattr("autoloop.eval.subprocess.run", _fake_subprocess_on_main(calls))

    main(publish=True, ctx=_make_ctx(tmp_path))

    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_files = list(snap_dir.glob("*.json"))
    assert len(snap_files) == 1
    snap = json.loads(snap_files[0].read_text())
    assert snap["total_implementations"] == 1


def test_main_publish_writes_eval_md(tmp_path, monkeypatch, capsys):
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

    monkeypatch.setattr("autoloop.eval.subprocess.run", _fake_subprocess_on_main())

    main(publish=True, ctx=_make_ctx(tmp_path))

    eval_md = tmp_path / "EVAL.md"
    assert eval_md.exists()
    content = eval_md.read_text()
    assert "# EVAL Report" in content
    assert "## Overall" in content
    assert "First-attempt success" in content


def test_main_publish_commits(tmp_path, monkeypatch, capsys):
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

    calls = []
    monkeypatch.setattr("autoloop.eval.subprocess.run", _fake_subprocess_on_main(calls))

    main(publish=True, ctx=_make_ctx(tmp_path))

    git_adds = [c for c in calls if c[:2] == ["git", "add"]]
    git_commits = [c for c in calls if c[:2] == ["git", "commit"]]
    assert len(git_adds) == 1
    assert len(git_commits) == 1
    assert "chore: update eval report" in git_commits[0][3]


def test_main_publish_rejects_non_main_branch(tmp_path, monkeypatch, capsys):
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

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="feature-branch\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "must be run from the main branch" in out
    assert not (tmp_path / "EVAL.md").exists()


def test_main_publish_handles_git_not_found(tmp_path, monkeypatch, capsys):
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

    def fake_run(cmd, **kw):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "git not found" in out


def test_main_publish_handles_git_commit_failure(tmp_path, monkeypatch, capsys):
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

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n")
        if cmd[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "git commit failed" in out
    assert "EVAL.md committed" not in out


def test_main_publish_handles_git_add_failure(tmp_path, monkeypatch, capsys):
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

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n")
        if cmd[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "git add failed" in out
    assert "EVAL.md committed" not in out


def test_main_no_publish_no_eval_md(tmp_path, monkeypatch, capsys):
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

    main(ctx=_make_ctx(tmp_path))

    eval_md = tmp_path / "EVAL.md"
    assert not eval_md.exists()


# --- CLI --publish flag ---


def test_cli_eval_publish_flag():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "--publish"])
    assert args.publish is True


def test_cli_eval_no_publish_default():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval"])
    assert args.publish is False


# --- CLI --pr flag ---


def test_cli_eval_pr_flag():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval", "--publish", "--pr"])
    assert args.publish is True
    assert args.pr is True


def test_cli_eval_no_pr_default():
    from autoloop.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["eval"])
    assert args.pr is False


# --- _publish_via_pr ---


def _fake_subprocess_all_ok(calls=None):
    """Return a fake subprocess.run where all commands succeed."""
    if calls is None:
        calls = []

    def _fake(cmd, **kw):
        calls.append(cmd)
        stdout = ""
        if cmd[:2] == ["gh", "pr"]:
            stdout = "https://github.com/owner/repo/pull/99\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return _fake


def test_publish_via_pr_creates_branch_and_pr(tmp_path, monkeypatch):
    snap_path = tmp_path / "autoloop" / "eval_snapshots" / "2026-09-19.json"
    snap_path.parent.mkdir(parents=True)
    snap_path.write_text("{}")
    eval_path = tmp_path / "EVAL.md"
    eval_path.write_text("# EVAL")

    calls = []
    monkeypatch.setattr("autoloop.eval.subprocess.run", _fake_subprocess_all_ok(calls))

    result = _publish_via_pr(_make_ctx(tmp_path), snap_path, eval_path, "2026-09-19")

    assert "PR created:" in result
    assert "https://github.com/owner/repo/pull/99" in result

    checkouts = [c for c in calls if c[:3] == ["git", "checkout", "-b"]]
    assert len(checkouts) == 1
    assert checkouts[0][3] == "chore/eval-2026-09-19"

    git_adds = [c for c in calls if c[:2] == ["git", "add"]]
    assert len(git_adds) == 1
    assert str(snap_path) in git_adds[0]
    assert str(eval_path) in git_adds[0]

    commits = [c for c in calls if c[:2] == ["git", "commit"]]
    assert len(commits) == 1
    assert "chore: update eval report (2026-09-19)" in commits[0][3]

    pushes = [c for c in calls if c[:2] == ["git", "push"]]
    assert len(pushes) == 1
    assert "chore/eval-2026-09-19" in pushes[0]

    pr_creates = [c for c in calls if c[:3] == ["gh", "pr", "create"]]
    assert len(pr_creates) == 1
    assert "chore: update eval report (2026-09-19)" in pr_creates[0]

    main_checkouts = [c for c in calls if c == ["git", "checkout", "main"]]
    assert len(main_checkouts) == 1


def test_publish_via_pr_handles_branch_creation_failure(tmp_path, monkeypatch):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text("{}")
    eval_path = tmp_path / "EVAL.md"
    eval_path.write_text("")

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "checkout", "-b"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="branch exists")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    result = _publish_via_pr(_make_ctx(tmp_path), snap_path, eval_path, "2026-09-19")

    assert "could not create branch" in result


def test_publish_via_pr_handles_push_failure(tmp_path, monkeypatch):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text("{}")
    eval_path = tmp_path / "EVAL.md"
    eval_path.write_text("")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="push error")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    result = _publish_via_pr(_make_ctx(tmp_path), snap_path, eval_path, "2026-09-19")

    assert "git push failed" in result
    main_checkouts = [c for c in calls if c == ["git", "checkout", "main"]]
    assert len(main_checkouts) == 1


def test_publish_via_pr_handles_pr_creation_failure(tmp_path, monkeypatch):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text("{}")
    eval_path = tmp_path / "EVAL.md"
    eval_path.write_text("")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="pr error")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    result = _publish_via_pr(_make_ctx(tmp_path), snap_path, eval_path, "2026-09-19")

    assert "PR creation failed" in result
    main_checkouts = [c for c in calls if c == ["git", "checkout", "main"]]
    assert len(main_checkouts) == 1


def test_publish_via_pr_handles_git_not_found(tmp_path, monkeypatch):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text("{}")
    eval_path = tmp_path / "EVAL.md"
    eval_path.write_text("")

    def fake_run(cmd, **kw):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    result = _publish_via_pr(_make_ctx(tmp_path), snap_path, eval_path, "2026-09-19")

    assert "git not found" in result


def test_publish_via_pr_checkouts_main_on_add_failure(tmp_path, monkeypatch):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text("{}")
    eval_path = tmp_path / "EVAL.md"
    eval_path.write_text("")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "add"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    result = _publish_via_pr(_make_ctx(tmp_path), snap_path, eval_path, "2026-09-19")

    assert "git add failed" in result
    main_checkouts = [c for c in calls if c == ["git", "checkout", "main"]]
    assert len(main_checkouts) == 1


def test_publish_via_pr_checkouts_main_on_commit_failure(tmp_path, monkeypatch):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text("{}")
    eval_path = tmp_path / "EVAL.md"
    eval_path.write_text("")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    result = _publish_via_pr(_make_ctx(tmp_path), snap_path, eval_path, "2026-09-19")

    assert "git commit failed" in result
    main_checkouts = [c for c in calls if c == ["git", "checkout", "main"]]
    assert len(main_checkouts) == 1


# --- main with --publish --pr ---


def test_main_publish_pr(tmp_path, monkeypatch, capsys):
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

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        stdout = ""
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            stdout = "main\n"
        elif cmd[:3] == ["gh", "pr", "create"]:
            stdout = "https://github.com/owner/repo/pull/42\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)

    main(publish=True, pr=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "PR created:" in out

    checkouts = [c for c in calls if c[:3] == ["git", "checkout", "-b"]]
    assert len(checkouts) == 1


def test_main_publish_pr_rejects_non_main(tmp_path, monkeypatch, capsys):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
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

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="feature\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)

    main(publish=True, pr=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "must be run from the main branch" in out


# --- main --publish push failure suggests --pr ---


def test_main_publish_push_failure_suggests_pr(tmp_path, monkeypatch, capsys):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
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

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="remote: error: GH013: Repository rule violations found",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "branch protection" in out
    assert "--pr" in out


def test_main_publish_push_generic_failure(tmp_path, monkeypatch, capsys):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    (log_dir / "run_history.jsonl").write_text(
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

    def fake_run(cmd, **kw):
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="main\n")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network error")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "git push failed" in out
    assert "--pr" not in out


# --- nested module path fixes (#174) ---


def test_classify_module_picks_deepest_prefix():
    """When nested prefixes match, classify to the deepest (longest) one."""
    files = ["src/patina/mcp/server.py"]
    prefixes = ["src/patina/", "src/patina/mcp/"]
    assert classify_module(files, prefixes) == "src/patina/mcp/"


def test_classify_module_picks_deepest_prefix_reverse_order():
    """Deepest match wins regardless of prefix list order."""
    files = ["src/patina/mcp/server.py"]
    prefixes = ["src/patina/mcp/", "src/patina/"]
    assert classify_module(files, prefixes) == "src/patina/mcp/"


def test_classify_module_primary_module_by_count():
    """PR with files in multiple modules is classified by majority file count."""
    files = [
        "src/patina/mcp/server.py",
        "src/patina/mcp/tools.py",
        "src/patina/store.py",
    ]
    prefixes = ["src/patina/", "src/patina/mcp/"]
    assert classify_module(files, prefixes) == "src/patina/mcp/"


def test_classify_module_shallow_file_not_swallowed_by_parent():
    """A file directly under src/patina/ goes to src/patina/, not a nested module."""
    files = ["src/patina/store.py"]
    prefixes = ["src/patina/", "src/patina/mcp/", "src/patina/adapters/"]
    assert classify_module(files, prefixes) == "src/patina/"


def test_compute_snapshot_nested_modules_correct_success():
    """Per-module first-attempt rate is correct for repos with nested module paths."""
    runs = [
        {
            "type": "implement",
            "issue": i,
            "success": True,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        }
        for i in range(1, 6)
    ]
    runs[2]["attempts"] = 2
    pr_data = [
        {
            "number": 1,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/store.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 1,
        },
        {
            "number": 2,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/config.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 2,
        },
        {
            "number": 3,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/mcp/server.py"],
            "human_edited": False,
            "first_attempt_success": False,
            "issue": 3,
        },
        {
            "number": 4,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/mcp/tools.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 4,
        },
        {
            "number": 5,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/adapters/github.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 5,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-19")

    modules = snap["modules"]
    assert "src/patina/" in modules
    assert "src/patina/mcp/" in modules
    assert "src/patina/adapters/" in modules

    assert modules["src/patina/"]["implementations"] == 2
    assert modules["src/patina/"]["first_attempt_rate"] == 1.0
    assert modules["src/patina/mcp/"]["implementations"] == 2
    assert modules["src/patina/mcp/"]["first_attempt_rate"] == 0.5
    assert modules["src/patina/adapters/"]["implementations"] == 1
    assert modules["src/patina/adapters/"]["first_attempt_rate"] == 1.0


def test_compute_snapshot_no_double_counting():
    """Per-module PRs sum to total implementations, no double-counting."""
    runs = [
        {
            "type": "implement",
            "issue": i,
            "success": True,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        }
        for i in range(1, 6)
    ]
    pr_data = [
        {
            "number": 1,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/store.py", "src/patina/mcp/server.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 1,
        },
        {
            "number": 2,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/mcp/tools.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 2,
        },
        {
            "number": 3,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/adapters/github.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 3,
        },
        {
            "number": 4,
            "merged": True,
            "closed": False,
            "changed_files": ["src/patina/config.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 4,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-19")

    total_module_prs = sum(m["implementations"] for m in snap["modules"].values())
    assert total_module_prs == len(pr_data)


def test_compute_snapshot_cross_module_pr_classified_once():
    """A PR touching files across nested modules goes to exactly one module."""
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
            "number": 1,
            "merged": True,
            "closed": False,
            "changed_files": [
                "src/patina/store.py",
                "src/patina/mcp/server.py",
                "src/patina/mcp/tools.py",
            ],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": 1,
        },
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-19")

    total_module_prs = sum(m["implementations"] for m in snap["modules"].values())
    assert total_module_prs == 1
    assert "src/patina/mcp/" in snap["modules"]


def test_generate_eval_md_pie_chart_title():
    """Pie chart title reflects PR count distribution, not success distribution."""
    modules = {
        "src/patina/": {
            "implementations": 10,
            "first_attempt_rate": 0.80,
            "avg_cost_usd": 1.0,
            "human_edit_rate": 0.0,
        },
        "src/patina/mcp/": {
            "implementations": 5,
            "first_attempt_rate": 0.60,
            "avg_cost_usd": 1.5,
            "human_edit_rate": 0.0,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap])
    assert "pie title Attempt Distribution by Module" in content
    assert "Success Distribution" not in content


# --- material_change ---


def test_material_change_no_change():
    old = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    new = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    assert material_change(new, old) is False


def test_material_change_small_rate_diff_not_material():
    old = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    new = {
        "first_attempt_rate": 0.84,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    assert material_change(new, old) is False


def test_material_change_first_attempt_rate_triggers():
    old = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    new = {
        "first_attempt_rate": 0.86,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    assert material_change(new, old) is True


def test_material_change_human_edit_rate_triggers():
    old = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    new = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.16,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    assert material_change(new, old) is True


def test_material_change_implementations_triggers():
    old = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    new = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 35,
        "modules": {"src/autoloop/": {}},
    }
    assert material_change(new, old) is True


def test_material_change_four_implementations_not_enough():
    old = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    new = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 34,
        "modules": {"src/autoloop/": {}},
    }
    assert material_change(new, old) is False


def test_material_change_new_module_triggers():
    old = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}},
    }
    new = {
        "first_attempt_rate": 0.80,
        "human_edit_rate": 0.10,
        "total_implementations": 30,
        "modules": {"src/autoloop/": {}, "src/new/": {}},
    }
    assert material_change(new, old) is True


# --- main --publish skips commit on no material change ---


def test_main_publish_skips_commit_no_material_change(tmp_path, monkeypatch, capsys):
    """When metrics haven't changed, --publish saves snapshot but skips commit."""
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

    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    previous = {
        "date": "2026-09-18",
        "total_implementations": 1,
        "first_attempt_rate": 1.0,
        "avg_cost_usd": 1.0,
        "avg_duration_seconds": 120,
        "attempt_distribution": {"1": 1, "2": 0, "3+": 0},
        "human_edit_rate": 0.0,
        "human_edit_count": 0,
        "merged_pr_count": 0,
        "closed_without_merge": 0,
        "modules": {},
    }
    (snap_dir / "2026-09-18.json").write_text(json.dumps(previous))

    calls = []
    monkeypatch.setattr("autoloop.eval.subprocess.run", _fake_subprocess_on_main(calls))

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "No material change, skipping EVAL.md commit" in out

    git_commits = [c for c in calls if c[:2] == ["git", "commit"]]
    assert len(git_commits) == 0

    snap_files = list(snap_dir.glob("*.json"))
    assert len(snap_files) == 2


def test_main_publish_commits_on_material_change(tmp_path, monkeypatch, capsys):
    """When metrics change beyond thresholds, --publish commits."""
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    entries = [
        {
            "type": "implement",
            "issue": i,
            "success": True,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 120,
        }
        for i in range(1, 11)
    ]
    log_file = log_dir / "run_history.jsonl"
    log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    previous = {
        "date": "2026-09-18",
        "total_implementations": 1,
        "first_attempt_rate": 1.0,
        "avg_cost_usd": 1.0,
        "avg_duration_seconds": 120,
        "attempt_distribution": {"1": 1, "2": 0, "3+": 0},
        "human_edit_rate": 0.0,
        "human_edit_count": 0,
        "merged_pr_count": 0,
        "closed_without_merge": 0,
        "modules": {},
    }
    (snap_dir / "2026-09-18.json").write_text(json.dumps(previous))

    calls = []
    monkeypatch.setattr("autoloop.eval.subprocess.run", _fake_subprocess_on_main(calls))

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "Material change detected, publishing EVAL.md" in out

    git_commits = [c for c in calls if c[:2] == ["git", "commit"]]
    assert len(git_commits) == 1


def test_main_publish_first_run_commits(tmp_path, monkeypatch, capsys):
    """First --publish with no previous snapshot always commits."""
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

    calls = []
    monkeypatch.setattr("autoloop.eval.subprocess.run", _fake_subprocess_on_main(calls))

    main(publish=True, ctx=_make_ctx(tmp_path))

    out = capsys.readouterr().out
    assert "Material change detected, publishing EVAL.md" in out

    git_commits = [c for c in calls if c[:2] == ["git", "commit"]]
    assert len(git_commits) == 1


# --- regression: per-module column label (#179) ---


def test_generate_eval_md_module_column_says_impl_not_prs():
    """Column header says 'Impl' (not 'PRs') and pie chart says 'impl' (not 'PRs')."""
    modules = {
        "src/autoloop/": {
            "implementations": 25,
            "first_attempt_rate": 0.92,
            "avg_cost_usd": 1.05,
            "human_edit_rate": 0.0,
            "merged_clean_count": 10,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap])
    assert "| Impl |" in content
    assert "| PRs |" not in content
    assert "25 impl)" in content
    assert "25 PRs)" not in content


# --- per-period stats (#194) ---


def test_format_trend_shows_period_deltas():
    """Trend table shows per-period deltas computed from consecutive total_implementations."""
    snaps = [
        {
            "date": "2026-09-18",
            "total_implementations": 125,
            "first_attempt_rate": 0.58,
            "avg_cost_usd": 1.44,
            "human_edit_rate": 0.15,
        },
        {
            "date": "2026-09-19",
            "total_implementations": 130,
            "first_attempt_rate": 0.58,
            "avg_cost_usd": 1.46,
            "human_edit_rate": 0.15,
        },
    ]
    output = format_trend(snaps)
    assert "125" in output
    lines = output.split("\n")
    row_09_19 = [ln for ln in lines if "2026-09-19" in ln][0]
    assert "     5" in row_09_19
    assert "130" not in row_09_19


def test_format_trend_delta_never_negative():
    """_period_impl clamps to 0 even if snapshots are inconsistent."""
    from autoloop.eval import _period_impl

    prev = {"total_implementations": 100}
    curr = {"total_implementations": 5}
    assert _period_impl(curr, prev) == 0


def test_generate_eval_md_trend_shows_deltas():
    """EVAL.md trend table shows per-period implementation deltas."""
    snaps = [
        _make_snapshot(date="2026-08-24", rate=0.80, cost=1.50, hr=0.12, total=28),
        _make_snapshot(date="2026-08-31", rate=0.83, cost=1.35, hr=0.10, total=30),
    ]
    content = generate_eval_md(snaps[-1], snaps)
    lines = content.split("\n")
    row_08_31 = [ln for ln in lines if "2026-08-31" in ln][0]
    assert "| 2 " in row_08_31
    assert "| 30 " not in row_08_31


def test_generate_eval_md_overall_stays_cumulative():
    """Overall section uses cumulative values."""
    snap = _make_snapshot(date="2026-09-19", rate=0.58, cost=1.46, total=130)
    content = generate_eval_md(snap, [snap])
    lines = content.split("\n")
    overall_idx = next(i for i, ln in enumerate(lines) if "## Overall" in ln)
    overall_section = "\n".join(lines[overall_idx : overall_idx + 10])
    assert "58%" in overall_section
    assert "$1.46" in overall_section


# --- population mismatch fix (#198) ---


def test_compute_snapshot_total_from_pr_data_not_runs():
    """total_implementations comes from PR count when pr_data is present."""
    runs = [
        {
            "type": "implement",
            "issue": i,
            "success": i <= 3,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        }
        for i in range(1, 6)
    ]
    pr_data = [
        {
            "number": 10 + i,
            "merged": True,
            "closed": False,
            "changed_files": [f"src/mod/f{i}.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": i,
        }
        for i in range(1, 4)
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-24")
    assert snap["total_implementations"] == 3
    assert snap["first_attempt_rate"] == 0.6
    assert snap["avg_cost_usd"] == 1.0
    assert snap["avg_duration_seconds"] == 60


def test_compute_snapshot_total_from_runs_when_no_pr_data():
    """Without pr_data, total_implementations falls back to run_history count."""
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
    snap = compute_snapshot(runs, date="2026-09-24")
    assert snap["total_implementations"] == 1


def test_compute_snapshot_module_total_matches_pr_total():
    """Sum of per-module implementations equals total_implementations."""
    runs = [
        {
            "type": "implement",
            "issue": i,
            "success": True,
            "attempts": 1 if i <= 5 else 2,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        }
        for i in range(1, 8)
    ]
    pr_data = [
        {
            "number": 10 + i,
            "merged": True,
            "closed": False,
            "changed_files": [f"src/autoloop/f{i}.py"],
            "human_edited": False,
            "first_attempt_success": True,
            "issue": i,
        }
        for i in range(1, 5)
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-09-24")
    assert snap["total_implementations"] == 4
    total_module = sum(m["implementations"] for m in snap["modules"].values())
    assert total_module == snap["total_implementations"]


# --- _compute_period_stats (#232) ---


def _make_full_snapshot(
    date="2026-09-14",
    rate=0.89,
    cost=1.12,
    hr=0.08,
    total=35,
    attempt_dist=None,
    human_edit_count=3,
    merged_pr_count=38,
):
    return {
        "date": date,
        "total_implementations": total,
        "first_attempt_rate": rate,
        "avg_cost_usd": cost,
        "human_edit_rate": hr,
        "attempt_distribution": attempt_dist or {"1": 31, "2": 3, "3+": 1},
        "human_edit_count": human_edit_count,
        "merged_pr_count": merged_pr_count,
        "modules": {},
    }


def test_compute_period_stats_no_previous():
    snap = _make_full_snapshot(rate=0.80, cost=1.50, hr=0.10)
    result = _compute_period_stats(snap, None)
    assert result["period_first_attempt_rate"] == 0.80
    assert result["period_avg_cost_usd"] == 1.50
    assert result["period_human_edit_rate"] == 0.10


def test_compute_period_stats_basic():
    prev = _make_full_snapshot(
        date="2026-09-07",
        rate=0.80,
        cost=1.50,
        total=30,
        attempt_dist={"1": 20, "2": 5, "3+": 5},
        human_edit_count=3,
        merged_pr_count=25,
    )
    curr = _make_full_snapshot(
        date="2026-09-14",
        rate=0.83,
        cost=1.40,
        total=35,
        attempt_dist={"1": 25, "2": 7, "3+": 5},
        human_edit_count=4,
        merged_pr_count=30,
    )
    result = _compute_period_stats(curr, prev)
    # period: 7 new runs (37 - 30), 5 first-attempt (25 - 20)
    assert result["period_first_attempt_rate"] == round(5 / 7, 2)
    # period cost: (1.40*37 - 1.50*30) / 7
    expected_cost = round((1.40 * 37 - 1.50 * 30) / 7, 2)
    assert result["period_avg_cost_usd"] == expected_cost
    # period edits: (4 - 3) / (30 - 25) = 1/5 = 0.2
    assert result["period_human_edit_rate"] == 0.2


def test_compute_period_stats_zero_impl_period():
    prev = _make_full_snapshot(
        attempt_dist={"1": 20, "2": 5, "3+": 5},
        human_edit_count=3,
        merged_pr_count=25,
    )
    curr = _make_full_snapshot(
        attempt_dist={"1": 20, "2": 5, "3+": 5},
        human_edit_count=3,
        merged_pr_count=25,
    )
    result = _compute_period_stats(curr, prev)
    assert result["period_first_attempt_rate"] is None
    assert result["period_avg_cost_usd"] is None
    assert result["period_human_edit_rate"] is None


def test_compute_period_stats_missing_attempt_distribution():
    prev = {"date": "2026-09-07", "first_attempt_rate": 0.80, "avg_cost_usd": 1.50}
    curr = _make_full_snapshot()
    result = _compute_period_stats(curr, prev)
    assert result == {}


def test_compute_period_stats_no_merged_prs_in_period():
    prev = _make_full_snapshot(
        attempt_dist={"1": 20, "2": 5, "3+": 5},
        human_edit_count=3,
        merged_pr_count=25,
    )
    curr = _make_full_snapshot(
        attempt_dist={"1": 22, "2": 6, "3+": 5},
        human_edit_count=3,
        merged_pr_count=25,
    )
    result = _compute_period_stats(curr, prev)
    assert result["period_first_attempt_rate"] == round(2 / 3, 2)
    assert result["period_human_edit_rate"] is None


def test_compute_period_stats_negative_cost_clamped_to_zero():
    prev = _make_full_snapshot(
        attempt_dist={"1": 80, "2": 8, "3+": 4},
        cost=0.82,
        human_edit_count=5,
        merged_pr_count=90,
    )
    curr = _make_full_snapshot(
        attempt_dist={"1": 81, "2": 8, "3+": 4},
        cost=0.81,
        human_edit_count=5,
        merged_pr_count=91,
    )
    result = _compute_period_stats(curr, prev)
    assert result["period_avg_cost_usd"] >= 0


# --- format_trend with period values (#232) ---


def test_format_trend_uses_period_values():
    snaps = [
        {
            "date": "2026-09-07",
            "total_implementations": 30,
            "first_attempt_rate": 0.80,
            "avg_cost_usd": 1.50,
            "human_edit_rate": 0.12,
            "period_first_attempt_rate": 0.80,
            "period_avg_cost_usd": 1.50,
            "period_human_edit_rate": 0.12,
        },
        {
            "date": "2026-09-14",
            "total_implementations": 35,
            "first_attempt_rate": 0.83,
            "avg_cost_usd": 1.40,
            "human_edit_rate": 0.10,
            "period_first_attempt_rate": 1.00,
            "period_avg_cost_usd": 0.80,
            "period_human_edit_rate": 0.0,
        },
    ]
    output = format_trend(snaps)
    lines = output.split("\n")
    row_14 = [ln for ln in lines if "2026-09-14" in ln][0]
    assert "100%" in row_14
    assert "0.80" in row_14
    assert "0%" in row_14


def test_format_trend_falls_back_to_cumulative_for_old_snapshots():
    snaps = [
        {
            "date": "2026-09-07",
            "total_implementations": 30,
            "first_attempt_rate": 0.80,
            "avg_cost_usd": 1.50,
            "human_edit_rate": 0.12,
        },
        {
            "date": "2026-09-14",
            "total_implementations": 35,
            "first_attempt_rate": 0.83,
            "avg_cost_usd": 1.40,
            "human_edit_rate": 0.10,
        },
    ]
    output = format_trend(snaps)
    lines = output.split("\n")
    row_07 = [ln for ln in lines if "2026-09-07" in ln][0]
    assert "80%" in row_07
    assert "1.50" in row_07
    assert "12%" in row_07


def test_format_trend_shows_dash_for_zero_impl_period():
    snaps = [
        {
            "date": "2026-09-07",
            "total_implementations": 30,
            "first_attempt_rate": 0.80,
            "avg_cost_usd": 1.50,
            "human_edit_rate": 0.12,
            "period_first_attempt_rate": 0.80,
            "period_avg_cost_usd": 1.50,
            "period_human_edit_rate": 0.12,
        },
        {
            "date": "2026-09-14",
            "total_implementations": 30,
            "first_attempt_rate": 0.80,
            "avg_cost_usd": 1.50,
            "human_edit_rate": 0.12,
            "period_first_attempt_rate": None,
            "period_avg_cost_usd": None,
            "period_human_edit_rate": None,
        },
    ]
    output = format_trend(snaps)
    lines = output.split("\n")
    row_14 = [ln for ln in lines if "2026-09-14" in ln][0]
    assert "—" in row_14
    assert "80%" not in row_14


# --- generate_eval_md with period values (#232) ---


def test_generate_eval_md_trend_uses_period_values():
    snaps = [
        _make_snapshot(date="2026-09-07", rate=0.80, cost=1.50, hr=0.12, total=30),
        {
            **_make_snapshot(date="2026-09-14", rate=0.83, cost=1.40, hr=0.10, total=35),
            "period_first_attempt_rate": 1.00,
            "period_avg_cost_usd": 0.80,
            "period_human_edit_rate": 0.0,
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    lines = content.split("\n")
    row_14 = [ln for ln in lines if "2026-09-14" in ln][0]
    assert "100%" in row_14
    assert "$0.80" in row_14
    assert "0%" in row_14


def test_generate_eval_md_trend_dash_for_zero_impl():
    snaps = [
        _make_snapshot(date="2026-09-07", rate=0.80, cost=1.50, hr=0.12, total=30),
        {
            **_make_snapshot(date="2026-09-14", rate=0.80, cost=1.50, hr=0.12, total=30),
            "period_first_attempt_rate": None,
            "period_avg_cost_usd": None,
            "period_human_edit_rate": None,
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    lines = content.split("\n")
    row_14 = [ln for ln in lines if "2026-09-14" in ln][0]
    assert "—" in row_14


def test_generate_eval_md_overall_stays_cumulative_with_period_values():
    snap = {
        **_make_snapshot(date="2026-09-14", rate=0.58, cost=1.46, hr=0.15, total=130),
        "period_first_attempt_rate": 1.00,
        "period_avg_cost_usd": 0.50,
        "period_human_edit_rate": 0.0,
    }
    content = generate_eval_md(snap, [snap])
    lines = content.split("\n")
    overall_idx = next(i for i, ln in enumerate(lines) if "## Overall" in ln)
    overall_section = "\n".join(lines[overall_idx : overall_idx + 10])
    assert "58%" in overall_section
    assert "$1.46" in overall_section
    assert "15%" in overall_section


def test_main_stores_period_stats(tmp_path, capsys):
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

    main(ctx=_make_ctx(tmp_path))

    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_file = list(snap_dir.glob("*.json"))[0]
    snap = json.loads(snap_file.read_text())
    assert "period_first_attempt_rate" in snap
    assert "period_avg_cost_usd" in snap
    assert "period_human_edit_rate" in snap


def test_main_period_stats_with_previous(tmp_path, capsys):
    snap_dir = tmp_path / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True)
    previous = {
        "date": "2026-09-13",
        "total_implementations": 5,
        "first_attempt_rate": 0.80,
        "avg_cost_usd": 1.50,
        "avg_duration_seconds": 120,
        "attempt_distribution": {"1": 4, "2": 1, "3+": 0},
        "human_edit_rate": 0.0,
        "human_edit_count": 0,
        "merged_pr_count": 0,
        "closed_without_merge": 0,
        "modules": {},
    }
    (snap_dir / "2026-09-13.json").write_text(json.dumps(previous))

    log_dir = tmp_path / "autoloop"
    log_file = log_dir / "run_history.jsonl"
    entries = [
        {
            "type": "implement",
            "issue": i,
            "success": True,
            "attempts": 1,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        }
        for i in range(1, 8)
    ]
    log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    main(ctx=_make_ctx(tmp_path))

    snap_files = sorted(snap_dir.glob("*.json"))
    new_snap = json.loads(snap_files[-1].read_text())
    assert new_snap["period_first_attempt_rate"] is not None
    assert new_snap["period_avg_cost_usd"] is not None


# --- ISO weekly resampling (#374) ---


def test_sample_for_chart_iso_weekly():
    """One point per ISO week, in chronological order."""
    snaps = [
        {"date": "2026-07-06"},  # W28
        {"date": "2026-07-13"},  # W29
        {"date": "2026-07-20"},  # W30
    ]
    result = _sample_for_chart(snaps)
    assert len(result) == 3
    assert result[0]["date"] == "2026-07-06"
    assert result[2]["date"] == "2026-07-20"


def test_sample_for_chart_last_snapshot_per_week():
    """Multiple snapshots in same ISO week — last one wins."""
    snaps = [
        {"date": "2026-09-01"},  # W36 (Tue)
        {"date": "2026-09-02"},  # W36 (Wed)
        {"date": "2026-09-03"},  # W36 (Thu)
        {"date": "2026-09-08"},  # W37
    ]
    result = _sample_for_chart(snaps)
    assert len(result) == 2
    assert result[0]["date"] == "2026-09-03"
    assert result[1]["date"] == "2026-09-08"


def test_sample_for_chart_empty():
    assert _sample_for_chart([]) == []


def test_md_label_format():
    assert _md_label("2026-09-07") == "9/7"
    assert _md_label("2026-01-15") == "1/15"
    assert _md_label("2026-12-01") == "12/1"


def test_chart_labels_md_format():
    """Chart x-axis labels use M/D format with n count."""
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, total=10),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.70, total=20),
            "attempt_distribution": {"1": 14, "2": 6, "3+": 0},
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert '"9/7 (n=10)"' in content
    assert '"9/14 (n=10)"' in content
    assert '"09-07' not in content


def test_chart_per_period_not_cumulative():
    """Chart plots per-period rates, not cumulative values."""
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, cost=2.00),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
            "human_edit_count": 0,
            "merged_pr_count": 8,
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.75, cost=1.50),
            "attempt_distribution": {"1": 14, "2": 6, "3+": 0},
            "human_edit_count": 0,
            "merged_pr_count": 16,
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    # Cumulative would be [80, 75]; per-period is [80, 60] (6/10)
    assert "bar [80, 60]" in content
    assert "bar [80, 75]" not in content


def test_chart_cost_y_axis_starts_at_zero():
    """Cost chart y-axis starts at 0."""
    snaps = [
        _make_snapshot(date="2026-09-07", cost=1.50),
        _make_snapshot(date="2026-09-14", cost=1.12),
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert 'y-axis "Cost ($)" 0 --> ' in content


def test_chart_annotations():
    """Annotation lines appear below each chart when provided."""
    snaps = [
        _make_snapshot(date="2026-09-07"),
        _make_snapshot(date="2026-09-14"),
    ]
    content = generate_eval_md(snaps[-1], snaps, annotations=["9/18: model change"])
    assert "> 9/18: model change" in content
    occurrences = content.count("> 9/18: model change")
    assert occurrences == 2


def test_chart_no_annotations_by_default():
    """No annotation lines when annotations parameter is not provided."""
    snaps = [
        _make_snapshot(date="2026-09-07"),
        _make_snapshot(date="2026-09-14"),
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert "\n> " not in content


def test_chart_title_includes_year():
    """Chart titles include the year from the data."""
    snaps = [
        _make_snapshot(date="2026-09-07"),
        _make_snapshot(date="2026-09-14"),
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert 'title "Weekly First-Attempt Success Rate (2026)"' in content
    assert 'title "Weekly Avg Cost/PR (2026)"' in content
    assert 'title "First-Attempt Success Rate (UTC)"' not in content


# --- #392: first-attempt charts don't separate no-data from 0% ---


def test_chart_no_bar_for_pending_only_week():
    """A week with only pending outcomes (no finished runs) shows no bar."""
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, total=10),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.80, total=10),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert "bar [80]" in content
    assert "9/14" not in content.split("```")[1]


def test_chart_no_false_zero_for_empty_week():
    """Weeks with no finished outcomes are omitted, not plotted as 0%."""
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, total=10),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.80, total=10),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
        },
        {
            **_make_snapshot(date="2026-09-21", rate=0.70, total=20),
            "attempt_distribution": {"1": 14, "2": 6, "3+": 0},
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    # Week 2 (09-14) has no new outcomes — should not appear as 0%
    assert "bar [80, 60]" in content
    assert ", 0," not in content.split("bar")[1].split("]")[0]


def test_chart_bars_show_n():
    """Each bar label includes the n (count of finished outcomes)."""
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.75, total=8),
            "attempt_distribution": {"1": 6, "2": 2, "3+": 0},
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.65, total=25),
            "attempt_distribution": {"1": 14, "2": 3, "3+": 0},
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    assert "(n=8)" in content
    assert "(n=9)" in content


def test_chart_values_match_table():
    """Chart bar values match the trend table rates for the same week."""
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, cost=2.00, total=10),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
            "human_edit_count": 0,
            "merged_pr_count": 8,
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.70, cost=1.50, total=20),
            "attempt_distribution": {"1": 14, "2": 6, "3+": 0},
            "human_edit_count": 1,
            "merged_pr_count": 18,
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    lines = content.split("\n")
    table_row = [ln for ln in lines if "2026-09-14" in ln][0]
    assert "60%" in table_row
    assert "bar [80, 60]" in content


def test_chart_zero_percent_shown_when_all_failed():
    """0% bar IS shown when outcomes exist but all failed."""
    snaps = [
        {
            **_make_snapshot(date="2026-09-07", rate=0.80, total=10),
            "attempt_distribution": {"1": 8, "2": 2, "3+": 0},
        },
        {
            **_make_snapshot(date="2026-09-14", rate=0.50, total=14),
            "attempt_distribution": {"1": 8, "2": 4, "3+": 2},
        },
    ]
    content = generate_eval_md(snaps[-1], snaps)
    # Period: 14-10=4 runs, 8-8=0 first-attempt → 0%
    assert "bar [80, 0]" in content
    assert "(n=4)" in content


# --- explicit populations and n (#418) ---


def test_compute_snapshot_pr_count_and_run_count_differ():
    """PR count and implement-run count are separate populations with their own n."""
    runs = [
        {"type": "implement", "issue": 1, "success": True, "attempts": 1, "cost_usd": 1.0},
        {"type": "implement", "issue": 2, "success": False, "attempts": 3, "cost_usd": 3.0},
        {"type": "triage", "issue": 0, "success": True, "cost_usd": 0.1},
    ]
    pr_data = [
        {"number": 10, "merged": True, "closed": False, "issue": 1},
        {"number": 11, "merged": True, "closed": False, "issue": 2, "human_edited": True},
        {"number": 12, "merged": False, "closed": False, "issue": 3},
        {"number": 13, "merged": True, "closed": False, "issue": 4},
        {"number": 14, "merged": False, "closed": True, "issue": 5},
    ]
    snap = compute_snapshot(runs, pr_data, date="2026-10-26")
    assert snap["total_prs"] == 4
    assert snap["implement_run_count"] == 2
    assert snap["merged_pr_count"] == 3
    assert snap["first_attempt_rate"] == 0.5
    assert snap["avg_cost_usd"] == 2.0


def test_compute_snapshot_keeps_old_total_implementations_key():
    pr_data = [
        {"number": 10, "merged": True, "closed": False},
        {"number": 11, "merged": False, "closed": True},
    ]
    snap = compute_snapshot([], pr_data, date="2026-10-26")
    assert snap["total_implementations"] == snap["total_prs"] == 1


def test_compute_snapshot_no_pr_data_counts_zero_prs():
    runs = [{"type": "implement", "issue": 1, "success": True, "attempts": 1}]
    snap = compute_snapshot(runs, [], date="2026-10-26")
    assert snap["total_prs"] == 0
    assert snap["implement_run_count"] == 1


def test_snapshot_total_prs_reads_old_key():
    assert snapshot_total_prs({"total_implementations": 30}) == 30
    assert snapshot_total_prs({"total_prs": 12, "total_implementations": 99}) == 12
    assert snapshot_total_prs({}) == 0


def test_snapshot_implement_runs_falls_back_to_attempt_distribution():
    assert snapshot_implement_runs({"implement_run_count": 43}) == 43
    assert snapshot_implement_runs({"attempt_distribution": {"1": 5, "2": 2, "3+": 1}}) == 8
    assert snapshot_implement_runs({}) == 0


def test_format_rate_with_n_above_and_below_floor():
    assert format_rate_with_n(0.84, 43, "implement runs") == "84% (n=43 implement runs)"
    assert format_rate_with_n(0.5, 19, "merged PRs") == "insufficient data (n=19 merged PRs)"
    assert format_rate_with_n(0.5, 4, "merged PRs", min_n=4) == "50% (n=4 merged PRs)"


def test_generate_eval_md_shows_both_n_values():
    snap = {
        **_make_snapshot(rate=0.84, cost=1.47, hr=0.43),
        "total_prs": 115,
        "total_implementations": 115,
        "implement_run_count": 43,
        "merged_pr_count": 60,
    }
    content = generate_eval_md(snap, [snap])
    assert "| Total PRs | 115 |" in content
    assert "| First-attempt success | 84% (n=43 implement runs) |" in content
    assert "| Avg cost/run | $1.47 (n=43 implement runs) |" in content
    assert "| Human edit rate | 43% (n=60 merged PRs) |" in content


def test_generate_eval_md_below_floor_renders_insufficient_data():
    snap = {
        **_make_snapshot(rate=1.0, hr=0.0, total=5),
        "implement_run_count": 5,
        "merged_pr_count": 3,
    }
    content = generate_eval_md(snap, [snap])
    assert "| First-attempt success | insufficient data (n=5 implement runs) |" in content
    assert "| Human edit rate | insufficient data (n=3 merged PRs) |" in content


def test_generate_eval_md_min_n_is_configurable():
    snap = {
        **_make_snapshot(rate=1.0, hr=0.0, total=5),
        "implement_run_count": 5,
        "merged_pr_count": 3,
    }
    content = generate_eval_md(snap, [snap], min_n=3)
    assert "| First-attempt success | 100% (n=5 implement runs) |" in content
    assert "| Human edit rate | 0% (n=3 merged PRs) |" in content


def test_generate_eval_md_reads_old_total_implementations_key():
    """Snapshots written before #418 (no total_prs) still feed the trend table."""
    old = _make_snapshot(date="2026-10-20", total=30)
    new = _make_snapshot(date="2026-10-21", total=34)
    assert "total_prs" not in old and "total_prs" not in new
    content = generate_eval_md(new, [old, new])
    assert "| Total PRs | 34 |" in content
    row = [ln for ln in content.split("\n") if ln.startswith("| 2026-10-21")][0]
    assert row.split("|")[2].strip() == "4"


def test_generate_eval_md_marks_baseline_break():
    before = {
        **_make_snapshot(date="2026-10-25", rate=0.5, total=200),
        "attempt_distribution": {"1": 100, "2": 100, "3+": 0},
    }
    after = {
        **_make_snapshot(date="2026-10-26", rate=0.84, cost=1.47, total=43),
        "total_prs": 205,
        "implement_run_count": 43,
        "attempt_distribution": {"1": 36, "2": 7, "3+": 0},
    }
    content = generate_eval_md(after, [before, after])
    row = [ln for ln in content.split("\n") if ln.startswith("| 2026-10-26")][0]
    assert "2026-10-26 (baseline break)" in row
    # Period stats restart at the break instead of diffing against triage-inflated rows.
    assert "84%" in row
    assert "$1.47" in row
    assert "Baseline break at 2026-10-26" in content
    assert "(#400)" in content
    before_row = [ln for ln in content.split("\n") if ln.startswith("| 2026-10-25")][0]
    assert "baseline break" not in before_row


def test_generate_eval_md_no_baseline_break_after_cutover():
    a = _make_snapshot(date="2026-10-26", total=40)
    b = _make_snapshot(date="2026-10-27", total=45)
    content = generate_eval_md(b, [a, b])
    assert "baseline break" not in content.lower()


def test_material_change_on_implement_run_growth_without_prs():
    old = {**_make_snapshot(total=10), "total_prs": 0, "implement_run_count": 10}
    new = {**_make_snapshot(total=10), "total_prs": 0, "implement_run_count": 15}
    assert material_change(new, old)


def test_material_change_reads_old_key_for_pr_growth():
    old = _make_snapshot(total=30)
    new = {**_make_snapshot(total=30), "total_prs": 35, "implement_run_count": 30}
    assert material_change(new, old)


def test_compare_snapshots_reports_total_prs_and_old_key():
    old = {"date": "2026-10-25", "total_implementations": 30}
    new = {"date": "2026-10-26", "total_prs": 35, "total_implementations": 35}
    comp = compare_snapshots(old, new)
    assert comp["changes"]["total_prs"] == {"old": 30, "new": 35, "delta": 5}
    assert comp["changes"]["total_implementations"]["delta"] == 5
    assert "Total PRs:" in format_comparison(comp)


def test_main_publish_passes_eval_min_n(tmp_path, monkeypatch):
    log_dir = tmp_path / "autoloop"
    log_dir.mkdir()
    entry = {"type": "implement", "issue": 1, "success": True, "attempts": 1, "cost_usd": 1.0}
    (log_dir / "run_history.jsonl").write_text(json.dumps(entry) + "\n")
    monkeypatch.setattr("autoloop.eval.subprocess.run", _fake_subprocess_on_main([]))

    main(publish=True, ctx=_make_ctx(tmp_path), eval_min_n=1)

    content = (tmp_path / "EVAL.md").read_text()
    assert "| First-attempt success | 100% (n=1 implement runs) |" in content


# --- unpublished snapshots live in the data dir (#402) ---

_REAL_RUN = subprocess.run
_REAL_COMPUTE_SNAPSHOT = compute_snapshot


def _make_split_ctx(tmp_path):
    """RepoContext whose data dir is outside the repo, as in production."""
    ctx = object.__new__(RepoContext)
    ctx.repo_dir = tmp_path / "repo"
    ctx.data_dir = tmp_path / "data"
    ctx.worktree_dir = ctx.data_dir / "worktrees"
    ctx.repo_dir.mkdir(parents=True, exist_ok=True)
    ctx.data_dir.mkdir(parents=True, exist_ok=True)
    return ctx


def _git(ctx, *args):
    return _REAL_RUN(
        ["git", *args], cwd=ctx.repo_dir, capture_output=True, text=True, check=True
    ).stdout


def _init_repo(ctx, monkeypatch):
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)
    _git(ctx, "init", "-q", "-b", "main")
    _git(ctx, "config", "user.email", "bot@example.com")
    _git(ctx, "config", "user.name", "Test Bot")
    _git(ctx, "config", "commit.gpgsign", "false")
    (ctx.repo_dir / "README.md").write_text("widget\n")
    (ctx.repo_dir / "EVAL.md").write_text("# Eval\n")
    _git(ctx, "add", "README.md", "EVAL.md")
    _git(ctx, "commit", "-q", "-m", "init")


def _runs(total, retried):
    """`total` implement runs, the first `retried` of which needed a second attempt."""
    return [
        {
            "type": "implement",
            "issue": i,
            "success": True,
            "attempts": 2 if i <= retried else 1,
            "cost_usd": 1.0,
            "duration_seconds": 60,
        }
        for i in range(1, total + 1)
    ]


def _write_runs(ctx, runs):
    (ctx.data_dir / "run_history.jsonl").write_text("\n".join(json.dumps(r) for r in runs) + "\n")


def _commit_published(ctx, snapshot):
    path = save_snapshot(snapshot, ctx, published=True)
    _git(ctx, "add", str(path))
    _git(ctx, "commit", "-q", "-m", f"chore: update eval report ({snapshot['date']})")


def _patch_eval_env(monkeypatch, calls, date_holder):
    """Run real git, fake `git push` / `gh`, and pin the snapshot date."""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:1] == ["gh"]:
            stdout = "https://github.com/acme-corp/widget/pull/7\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return _REAL_RUN(cmd, **kw)

    def fake_compute(runs, pr_data=None, date=None, module_prefixes=None):
        return _REAL_COMPUTE_SNAPSHOT(runs, pr_data, date=date_holder[0])

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    monkeypatch.setattr("autoloop.eval.compute_snapshot", fake_compute)


def test_save_snapshot_defaults_to_data_dir(tmp_path):
    ctx = _make_split_ctx(tmp_path)
    path = save_snapshot({"date": "2026-10-01"}, ctx)
    assert path == ctx.data_dir / "eval_snapshots" / "2026-10-01.json"
    assert not (ctx.repo_dir / "autoloop" / "eval_snapshots").exists()


def test_save_snapshot_published_goes_to_repo(tmp_path):
    ctx = _make_split_ctx(tmp_path)
    path = save_snapshot({"date": "2026-10-01"}, ctx, published=True)
    assert path == ctx.repo_dir / "autoloop" / "eval_snapshots" / "2026-10-01.json"
    assert not (ctx.data_dir / "eval_snapshots").exists()


def test_load_latest_snapshot_ignores_daily_snapshots(tmp_path):
    ctx = _make_split_ctx(tmp_path)
    save_snapshot({"date": "2026-10-01"}, ctx, published=True)
    save_snapshot({"date": "2026-10-05"}, ctx)
    assert load_latest_snapshot(ctx)["date"] == "2026-10-01"


def test_load_latest_snapshot_none_with_only_daily_snapshots(tmp_path):
    ctx = _make_split_ctx(tmp_path)
    save_snapshot({"date": "2026-10-05"}, ctx)
    assert load_latest_snapshot(ctx) is None


def test_load_all_snapshots_unions_dirs_repo_wins(tmp_path):
    ctx = _make_split_ctx(tmp_path)
    save_snapshot({"date": "2026-10-01", "source": "daily"}, ctx)
    save_snapshot({"date": "2026-10-02", "source": "daily"}, ctx)
    save_snapshot({"date": "2026-10-02", "source": "repo"}, ctx, published=True)
    save_snapshot({"date": "2026-10-03", "source": "repo"}, ctx, published=True)

    snaps = load_all_snapshots(ctx)
    assert [s["date"] for s in snaps] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert [s["source"] for s in snaps] == ["daily", "repo", "repo"]


def test_load_snapshot_prefers_repo_then_data_dir(tmp_path):
    ctx = _make_split_ctx(tmp_path)
    save_snapshot({"date": "2026-10-01", "source": "daily"}, ctx)
    save_snapshot({"date": "2026-10-01", "source": "repo"}, ctx, published=True)
    save_snapshot({"date": "2026-10-02", "source": "daily"}, ctx)
    assert load_snapshot("2026-10-01", ctx)["source"] == "repo"
    assert load_snapshot("2026-10-02", ctx)["source"] == "daily"


def test_migrate_moves_untracked_and_restores_modified(tmp_path, monkeypatch):
    ctx = _make_split_ctx(tmp_path)
    _init_repo(ctx, monkeypatch)
    _commit_published(ctx, {"date": "2026-10-01", "v": "committed"})
    save_snapshot({"date": "2026-10-01", "v": "edited"}, ctx, published=True)
    save_snapshot({"date": "2026-10-02", "v": "untracked"}, ctx, published=True)
    staged = save_snapshot({"date": "2026-10-03", "v": "staged"}, ctx, published=True)
    _git(ctx, "add", str(staged))

    migrated = migrate_unpublished_snapshots(ctx)

    daily = ctx.data_dir / "eval_snapshots"
    assert sorted(p.name for p in migrated) == [
        "2026-10-01.json",
        "2026-10-02.json",
        "2026-10-03.json",
    ]
    assert json.loads((daily / "2026-10-01.json").read_text())["v"] == "edited"
    assert json.loads((daily / "2026-10-02.json").read_text())["v"] == "untracked"
    assert json.loads((daily / "2026-10-03.json").read_text())["v"] == "staged"
    assert _git(ctx, "status", "--porcelain") == ""
    assert load_latest_snapshot(ctx) == {"date": "2026-10-01", "v": "committed"}


def test_migrate_noop_outside_git_repo(tmp_path):
    ctx = _make_split_ctx(tmp_path)
    save_snapshot({"date": "2026-10-01"}, ctx, published=True)
    assert migrate_unpublished_snapshots(ctx) == []
    assert (ctx.repo_dir / "autoloop" / "eval_snapshots" / "2026-10-01.json").exists()


def test_migrate_handles_git_not_found(tmp_path, monkeypatch):
    ctx = _make_split_ctx(tmp_path)

    def fake_run(cmd, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr("autoloop.eval.subprocess.run", fake_run)
    assert migrate_unpublished_snapshots(ctx) == []


def test_publish_pr_no_change_day_leaves_tree_clean(tmp_path, monkeypatch, capsys):
    ctx = _make_split_ctx(tmp_path)
    _init_repo(ctx, monkeypatch)
    runs = _runs(50, 0)
    _commit_published(ctx, _REAL_COMPUTE_SNAPSHOT(runs, [], date="2026-10-01"))
    _write_runs(ctx, runs)

    calls = []
    _patch_eval_env(monkeypatch, calls, ["2026-10-02"])
    main(publish=True, pr=True, ctx=ctx)

    out = capsys.readouterr().out
    assert "No material change, skipping EVAL.md commit" in out
    assert _git(ctx, "status", "--porcelain") == ""
    assert (ctx.data_dir / "eval_snapshots" / "2026-10-02.json").exists()
    assert not (ctx.repo_dir / "autoloop" / "eval_snapshots" / "2026-10-02.json").exists()
    assert (ctx.repo_dir / "EVAL.md").read_text() == "# Eval\n"
    assert _git(ctx, "branch", "--format=%(refname:short)").split() == ["main"]
    assert not [c for c in calls if c[:1] == ["gh"]]


def test_publish_pr_publish_day_commits_snapshot_and_leaves_tree_clean(
    tmp_path, monkeypatch, capsys
):
    ctx = _make_split_ctx(tmp_path)
    _init_repo(ctx, monkeypatch)
    _commit_published(ctx, _REAL_COMPUTE_SNAPSHOT(_runs(50, 0), [], date="2026-10-01"))
    _write_runs(ctx, _runs(50, 10))

    calls = []
    _patch_eval_env(monkeypatch, calls, ["2026-10-02"])
    main(publish=True, pr=True, ctx=ctx)

    out = capsys.readouterr().out
    assert "Material change detected, publishing EVAL.md" in out
    assert "PR created:" in out
    assert _git(ctx, "status", "--porcelain") == ""

    branch = "chore/eval-2026-10-02"
    committed = _git(ctx, "show", "--name-only", "--format=", branch).split()
    assert sorted(committed) == ["EVAL.md", "autoloop/eval_snapshots/2026-10-02.json"]

    # Once the PR merges, the new snapshot is the baseline.
    _git(ctx, "merge", "-q", "--ff-only", branch)
    assert load_latest_snapshot(ctx)["date"] == "2026-10-02"


def test_publish_pr_gradual_drift_eventually_publishes(tmp_path, monkeypatch, capsys):
    """-2 points/day never crosses the threshold day-over-day, but does against the baseline."""
    ctx = _make_split_ctx(tmp_path)
    _init_repo(ctx, monkeypatch)
    _commit_published(ctx, _REAL_COMPUTE_SNAPSHOT(_runs(50, 0), [], date="2026-10-01"))

    calls = []
    date_holder = ["2026-10-01"]
    _patch_eval_env(monkeypatch, calls, date_holder)

    for day, retried in (("2026-10-02", 1), ("2026-10-03", 2)):
        date_holder[0] = day
        _write_runs(ctx, _runs(50, retried))
        main(publish=True, pr=True, ctx=ctx)
        out = capsys.readouterr().out
        assert "No material change, skipping EVAL.md commit" in out
        assert _git(ctx, "status", "--porcelain") == ""
        assert (ctx.repo_dir / "EVAL.md").read_text() == "# Eval\n"
        assert (ctx.data_dir / "eval_snapshots" / f"{day}.json").exists()
        assert load_latest_snapshot(ctx)["date"] == "2026-10-01"

    date_holder[0] = "2026-10-04"
    _write_runs(ctx, _runs(50, 3))
    main(publish=True, pr=True, ctx=ctx)

    out = capsys.readouterr().out
    assert "Material change detected, publishing EVAL.md" in out
    assert "PR created:" in out
    assert _git(ctx, "status", "--porcelain") == ""
    committed = _git(ctx, "show", "--name-only", "--format=", "chore/eval-2026-10-04").split()
    assert "autoloop/eval_snapshots/2026-10-04.json" in committed


def test_publish_migrates_legacy_untracked_snapshots(tmp_path, monkeypatch, capsys):
    """Untracked snapshots left by older versions move to the data dir; history is kept."""
    ctx = _make_split_ctx(tmp_path)
    _init_repo(ctx, monkeypatch)
    runs = _runs(50, 0)
    _commit_published(ctx, _REAL_COMPUTE_SNAPSHOT(runs, [], date="2026-10-01"))
    save_snapshot(_REAL_COMPUTE_SNAPSHOT(runs, [], date="2026-10-02"), ctx, published=True)
    _write_runs(ctx, runs)

    _patch_eval_env(monkeypatch, [], ["2026-10-03"])
    main(publish=True, pr=True, ctx=ctx)

    assert "No material change" in capsys.readouterr().out
    assert _git(ctx, "status", "--porcelain") == ""
    assert [s["date"] for s in load_all_snapshots(ctx)] == [
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
    ]


# --- per-module stats only over PRs with an implement run (#432) ---


def _module_pr(number, issue, *, merged=True, human_edited=False):
    return {
        "number": number,
        "merged": merged,
        "closed": not merged,
        "changed_files": [f"src/widget/f{number}.py"],
        "human_edited": human_edited,
        "issue": issue,
    }


def test_compute_snapshot_module_rates_only_over_prs_with_runs():
    runs = [
        {"type": "implement", "issue": 1, "success": True, "attempts": 1, "cost_usd": 1.0},
        {"type": "implement", "issue": 2, "success": True, "attempts": 2, "cost_usd": 3.0},
    ]
    pr_data = [_module_pr(10, 1), _module_pr(11, 2), _module_pr(12, 3), _module_pr(13, None)]
    pr_data = enrich_pr_data_with_runs(pr_data, runs)
    snap = compute_snapshot(runs, pr_data, date="2026-10-26")
    mod = snap["modules"]["src/widget/"]
    assert mod["implementations"] == 4
    assert mod["implement_run_count"] == 2
    assert mod["first_attempt_rate"] == 0.5
    assert mod["avg_cost_usd"] == 2.0


def test_compute_snapshot_module_without_runs_has_zero_run_count():
    pr_data = [_module_pr(10, 1), _module_pr(11, 2)]
    snap = compute_snapshot([], pr_data, date="2026-10-26")
    mod = snap["modules"]["src/widget/"]
    assert mod["implementations"] == 2
    assert mod["implement_run_count"] == 0


def test_compute_snapshot_module_ignores_first_attempt_flag_without_run():
    """A PR carrying first_attempt_success but no matching run is not counted."""
    pr = {**_module_pr(10, 7), "first_attempt_success": True}
    snap = compute_snapshot([], [pr], date="2026-10-26")
    mod = snap["modules"]["src/widget/"]
    assert mod["implement_run_count"] == 0
    assert mod["first_attempt_rate"] == 0.0


def test_compute_snapshot_module_human_edit_rate_still_over_merged_prs():
    runs = [{"type": "implement", "issue": 1, "success": True, "attempts": 1, "cost_usd": 1.0}]
    pr_data = [
        _module_pr(10, 1, human_edited=True),
        _module_pr(11, 2),
        _module_pr(12, None, human_edited=True),
        _module_pr(13, None),
        _module_pr(14, 3, merged=False),
    ]
    pr_data = enrich_pr_data_with_runs(pr_data, runs)
    snap = compute_snapshot(runs, pr_data, date="2026-10-26")
    mod = snap["modules"]["src/widget/"]
    assert mod["human_edit_rate"] == 0.5
    assert mod["merged_clean_count"] == 2
    assert mod["implement_run_count"] == 1


def test_generate_eval_md_module_without_runs_renders_insufficient_data():
    pr_data = [_module_pr(10, 1), _module_pr(11, 2)]
    snap = compute_snapshot([], pr_data, date="2026-10-26")
    content = generate_eval_md(snap, [snap])
    row = next(line for line in content.splitlines() if line.startswith("| src/widget/ |"))
    assert row == (
        "| src/widget/ | insufficient data (n=0 runs) | insufficient data (n=0 runs) | 2 | No |"
    )
    assert "0%" not in row
    assert "$0.00" not in row


def test_generate_eval_md_module_at_min_n_renders_rate_and_cost():
    modules = {
        "src/widget/": {
            "implementations": 30,
            "implement_run_count": 20,
            "first_attempt_rate": 0.85,
            "avg_cost_usd": 1.25,
            "human_edit_rate": 0.0,
            "merged_clean_count": 30,
        },
    }
    snap = _make_snapshot(modules=modules)
    content = generate_eval_md(snap, [snap])
    assert "| src/widget/ | 85% (n=20 runs) | $1.25 (n=20 runs) | 30 | No |" in content


def test_module_implement_runs_reads_new_key():
    assert module_implement_runs({"implementations": 5, "implement_run_count": 2}) == 2


def test_module_implement_runs_falls_back_to_implementations():
    assert module_implement_runs({"implementations": 5}) == 5


def test_format_cost_with_n_below_min_n():
    assert format_cost_with_n(0.0, 3, "runs", min_n=20) == "insufficient data (n=3 runs)"


def test_format_cost_with_n_at_min_n():
    assert format_cost_with_n(1.5, 20, "runs", min_n=20) == "$1.50 (n=20 runs)"


def test_format_snapshot_module_hotspots_use_run_count():
    snap = {
        "date": "2026-10-26",
        "total_prs": 4,
        "implement_run_count": 2,
        "first_attempt_rate": 0.5,
        "avg_cost_usd": 2.0,
        "avg_duration_seconds": 0,
        "human_edit_rate": 0.0,
        "modules": {
            "src/widget/": {
                "implementations": 4,
                "implement_run_count": 2,
                "first_attempt_rate": 0.5,
            },
        },
    }
    assert "50% first-attempt (1/2 runs)" in format_snapshot(snap)
    assert "| src/widget/ | 50% (1/2 runs) | 4 |" in format_eval_md(snap)


# --- eval --publish --force (#432) ---


def test_main_publish_force_publishes_without_material_change(tmp_path, monkeypatch, capsys):
    ctx = _make_split_ctx(tmp_path)
    _init_repo(ctx, monkeypatch)
    runs = _runs(50, 0)
    _commit_published(ctx, _REAL_COMPUTE_SNAPSHOT(runs, [], date="2026-10-01"))
    _write_runs(ctx, runs)

    _patch_eval_env(monkeypatch, [], ["2026-10-02"])
    main(publish=True, force=True, ctx=ctx)

    out = capsys.readouterr().out
    assert "No material change, publishing EVAL.md anyway (--force)" in out
    assert "# EVAL Report" in (ctx.repo_dir / "EVAL.md").read_text()
    published = ctx.repo_dir / "autoloop" / "eval_snapshots" / "2026-10-02.json"
    assert published.exists()
    assert load_latest_snapshot(ctx)["date"] == "2026-10-02"
    assert _git(ctx, "status", "--porcelain") == ""
    assert _git(ctx, "log", "-1", "--format=%s").strip() == "chore: update eval report (2026-10-02)"


def test_main_publish_without_force_still_skips(tmp_path, monkeypatch, capsys):
    ctx = _make_split_ctx(tmp_path)
    _init_repo(ctx, monkeypatch)
    runs = _runs(50, 0)
    _commit_published(ctx, _REAL_COMPUTE_SNAPSHOT(runs, [], date="2026-10-01"))
    _write_runs(ctx, runs)

    calls = []
    _patch_eval_env(monkeypatch, calls, ["2026-10-02"])
    main(publish=True, ctx=ctx)

    out = capsys.readouterr().out
    assert "No material change, skipping EVAL.md commit" in out
    assert (ctx.repo_dir / "EVAL.md").read_text() == "# Eval\n"
    assert not (ctx.repo_dir / "autoloop" / "eval_snapshots" / "2026-10-02.json").exists()
    assert not [c for c in calls if c[:2] == ["git", "commit"]]


def test_cli_eval_force_flag():
    from autoloop.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["eval", "--publish", "--force"]).force is True
    assert parser.parse_args(["eval"]).force is False


def _dispatch_eval(argv):
    from unittest.mock import MagicMock, patch

    from autoloop.cli import main as cli_main

    called = {}

    def fake_eval_main(**kwargs):
        called.update(kwargs)

    with (
        patch("autoloop.eval.main", fake_eval_main),
        patch("autoloop.config.load_config", return_value=MagicMock()),
        patch("autoloop.config.RepoContext", return_value="fake_ctx"),
        patch("sys.argv", ["autoloop", *argv]),
    ):
        cli_main()
    return called


def test_cli_eval_passes_force_to_eval_main():
    called = _dispatch_eval(["eval", "--publish", "--force"])
    assert called["publish"] is True
    assert called["force"] is True


def test_cli_eval_force_defaults_false_in_eval_main():
    called = _dispatch_eval(["eval", "--publish"])
    assert called["force"] is False
