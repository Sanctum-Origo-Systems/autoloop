import json

from autoloop.jev import GATE_HIGH, GATE_LOW
from autoloop.jev_report import (
    JevEntry,
    compute_automerge_stats,
    compute_triage_stats,
    parse_jev_decisions,
    render_jev_md,
)


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _triage_record(
    issue=1,
    well_formed=0.92,
    needs_decomp=0.09,
    verdict="ready",
    timestamp="2026-09-28T07:00:00+00:00",
    backfill=False,
    bias_warning=False,
    fallback=False,
    would_fallback=False,
):
    jev = {"well_formed": well_formed, "needs_decomposition": needs_decomp}
    if fallback:
        jev = {"fallback": True, "reason": "test fallback"}
    elif would_fallback:
        jev["would_fallback"] = True
    entry = {
        "point": "triage",
        "issue": issue,
        "jev_call": jev,
        "incumbent_call": {
            "verdict": verdict,
            "points": 2,
            "priority": "p1",
            "reason": "test",
        },
        "outcome": "incumbent",
        "ttft": 0.2,
        "cost": 0.0,
        "timestamp": timestamp,
    }
    if backfill:
        entry["backfill"] = True
    if bias_warning:
        entry["triage_bias_warning"] = True
    return entry


def _automerge_record(
    issue=10,
    pr=11,
    prob=0.75,
    approved=True,
    timestamp="2026-09-28T09:00:00+00:00",
    backfill=False,
    fallback=False,
):
    if fallback:
        jev = {"fallback": True, "reason": "test"}
    else:
        jev = {"meets_acceptance_criteria": prob}
    entry = {
        "point": "auto-merge",
        "issue": issue,
        "pr": pr,
        "jev_call": jev,
        "incumbent_call": {"approved": approved, "feedback": "test"},
        "outcome": "incumbent",
        "ttft": 0.3,
        "cost": 0.0,
        "timestamp": timestamp,
    }
    if backfill:
        entry["backfill"] = True
    return entry


def _make_triage_entry(
    well_formed=0.92,
    needs_decomp=0.09,
    verdict="ready",
    timestamp="2026-09-28T07:00:00+00:00",
    fallback=False,
    would_fallback=False,
    bias_warning=False,
):
    jev = {
        "well_formed": well_formed,
        "needs_decomposition": needs_decomp,
    }
    if fallback:
        jev = {"fallback": True, "reason": "test"}
    elif would_fallback:
        jev["would_fallback"] = True
    return JevEntry(
        timestamp=timestamp,
        issue=1,
        point="triage",
        jev_call=jev,
        incumbent_call={"verdict": verdict},
        outcome="incumbent",
        is_backfill=False,
        triage_bias_warning=bias_warning,
    )


def _make_automerge_entry(
    prob=0.75,
    approved=True,
    timestamp="2026-09-28T09:00:00+00:00",
    fallback=False,
):
    if fallback:
        jev = {"fallback": True, "reason": "test"}
    else:
        jev = {"meets_acceptance_criteria": prob}
    return JevEntry(
        timestamp=timestamp,
        issue=10,
        point="auto-merge",
        jev_call=jev,
        incumbent_call={"approved": approved, "feedback": "test"},
        outcome="incumbent",
        is_backfill=False,
        triage_bias_warning=False,
    )


def _empty_triage_stats():
    return {
        "n": 0,
        "agree_count": 0,
        "disagree_count": 0,
        "uncertain_count": 0,
        "agreement_rate": 0.0,
        "uncertainty_rate": 0.0,
        "disagreement_breakdown": {},
        "weekly_trends": [],
    }


def _empty_automerge_stats():
    return {
        "distribution": {
            f"Act: reject (below {GATE_LOW})": 0,
            f"Pipeline ({GATE_LOW}-{GATE_HIGH})": 0,
            f"Act: merge ({GATE_HIGH}+)": 0,
        },
        "mean": 0.0,
        "median": 0.0,
        "n_probabilities": 0,
        "n_with_outcome": 0,
        "agreement_with_outcome": 0.0,
    }


def _basic_counts():
    return {
        "total": 10,
        "live": 7,
        "backfilled": 3,
        "date_range": "2026-09-25 to 2026-10-05",
    }


# --- parse_jev_decisions ---


class TestParseJevDecisions:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "jev_decisions.jsonl"
        f.write_text("")
        result = parse_jev_decisions(f)
        assert result.backfilled == []
        assert result.live == []

    def test_nonexistent_file(self, tmp_path):
        result = parse_jev_decisions(tmp_path / "nope.jsonl")
        assert result.backfilled == []
        assert result.live == []

    def test_separates_backfill_and_live(self, tmp_path):
        records = [
            _triage_record(issue=1, backfill=False),
            _triage_record(issue=2, backfill=True),
            _automerge_record(issue=3, backfill=False),
            _automerge_record(issue=4, backfill=True),
        ]
        f = _write_jsonl(tmp_path / "decisions.jsonl", records)
        result = parse_jev_decisions(f)
        assert len(result.live) == 2
        assert len(result.backfilled) == 2
        assert {e.issue for e in result.live} == {1, 3}
        assert {e.issue for e in result.backfilled} == {2, 4}

    def test_all_backfilled(self, tmp_path):
        records = [
            _triage_record(issue=1, backfill=True),
            _automerge_record(issue=2, backfill=True),
        ]
        f = _write_jsonl(tmp_path / "decisions.jsonl", records)
        result = parse_jev_decisions(f)
        assert len(result.live) == 0
        assert len(result.backfilled) == 2

    def test_forensic_backfill_treated_as_backfill(self, tmp_path):
        record = _automerge_record(issue=5)
        record["forensic_backfill"] = True
        f = _write_jsonl(tmp_path / "decisions.jsonl", [record])
        result = parse_jev_decisions(f)
        assert len(result.backfilled) == 1
        assert result.backfilled[0].is_backfill is True

    def test_is_backfill_field(self, tmp_path):
        record = _triage_record(issue=6)
        record["is_backfill"] = True
        f = _write_jsonl(tmp_path / "decisions.jsonl", [record])
        result = parse_jev_decisions(f)
        assert len(result.backfilled) == 1

    def test_old_format_triage_inferred(self, tmp_path):
        record = {
            "timestamp": "2026-09-25T16:54:06+00:00",
            "issue": 220,
            "incumbent": {"verdict": "rejected", "points": 2},
            "jev": {"fallback": True, "reason": "HTTP 400"},
            "error": None,
        }
        f = _write_jsonl(tmp_path / "decisions.jsonl", [record])
        result = parse_jev_decisions(f)
        assert len(result.live) == 1
        assert result.live[0].point == "triage"
        assert result.live[0].incumbent_call["verdict"] == "rejected"

    def test_preserves_entry_fields(self, tmp_path):
        record = _automerge_record(issue=42, pr=43, prob=0.85)
        f = _write_jsonl(tmp_path / "decisions.jsonl", [record])
        result = parse_jev_decisions(f)
        entry = result.live[0]
        assert entry.issue == 42
        assert entry.pr == 43
        assert entry.point == "auto-merge"
        assert entry.jev_call["meets_acceptance_criteria"] == 0.85

    def test_triage_bias_warning_preserved(self, tmp_path):
        record = _triage_record(issue=7, bias_warning=True)
        f = _write_jsonl(tmp_path / "decisions.jsonl", [record])
        result = parse_jev_decisions(f)
        assert result.live[0].triage_bias_warning is True

    def test_blank_lines_skipped(self, tmp_path):
        f = tmp_path / "decisions.jsonl"
        content = (
            json.dumps(_triage_record(issue=1))
            + "\n\n"
            + json.dumps(_triage_record(issue=2))
            + "\n"
        )
        f.write_text(content)
        result = parse_jev_decisions(f)
        assert len(result.live) == 2


# --- compute_triage_stats ---


class TestComputeTriageStats:
    def test_empty_entries(self):
        result = compute_triage_stats([])
        assert result["n"] == 0
        assert result["agree_count"] == 0
        assert result["disagree_count"] == 0
        assert result["uncertain_count"] == 0
        assert result["agreement_rate"] == 0.0
        assert result["disagreement_breakdown"] == {}
        assert result["uncertainty_rate"] == 0.0
        assert result["weekly_trends"] == []

    def test_all_agree(self):
        entries = [
            _make_triage_entry(well_formed=0.92, needs_decomp=0.09, verdict="ready"),
            _make_triage_entry(well_formed=0.93, needs_decomp=0.08, verdict="ready"),
        ]
        result = compute_triage_stats(entries)
        assert result["n"] == 2
        assert result["agree_count"] == 2
        assert result["disagree_count"] == 0
        assert result["uncertain_count"] == 0
        assert result["agreement_rate"] == 1.0
        assert result["disagreement_breakdown"] == {}
        assert result["uncertainty_rate"] == 0.0

    def test_disagreement_ready_vs_rejected(self):
        entries = [
            _make_triage_entry(
                well_formed=0.92,
                needs_decomp=0.09,
                verdict="rejected",
            ),
        ]
        result = compute_triage_stats(entries)
        assert result["n"] == 1
        assert result["agree_count"] == 0
        assert result["disagree_count"] == 1
        assert result["agreement_rate"] == 0.0
        assert result["disagreement_breakdown"] == {"ready-vs-rejected": 1}

    def test_needs_decomposition_agreement(self):
        entries = [
            _make_triage_entry(
                well_formed=0.88,
                needs_decomp=0.78,
                verdict="needs-decomposition",
            ),
        ]
        result = compute_triage_stats(entries)
        assert result["agree_count"] == 1
        assert result["agreement_rate"] == 1.0

    def test_low_well_formed_inferred_rejected(self):
        entries = [
            _make_triage_entry(
                well_formed=0.70,
                needs_decomp=0.10,
                verdict="rejected",
            ),
        ]
        result = compute_triage_stats(entries)
        assert result["agree_count"] == 1
        assert result["agreement_rate"] == 1.0

    def test_uncertainty_rate_with_would_fallback(self):
        entries = [
            _make_triage_entry(
                well_formed=0.92,
                needs_decomp=0.09,
                verdict="ready",
            ),
            _make_triage_entry(
                well_formed=0.88,
                needs_decomp=0.19,
                verdict="ready",
                would_fallback=True,
            ),
        ]
        result = compute_triage_stats(entries)
        assert result["n"] == 2
        assert result["agree_count"] == 1
        assert result["uncertain_count"] == 1
        assert result["uncertainty_rate"] == 0.5
        assert result["agreement_rate"] == 0.5

    def test_fallback_counts_as_uncertain(self):
        entries = [
            _make_triage_entry(verdict="ready", fallback=True),
        ]
        result = compute_triage_stats(entries)
        assert result["uncertain_count"] == 1
        assert result["uncertainty_rate"] == 1.0
        assert result["agreement_rate"] == 0.0

    def test_excludes_bias_warning_entries(self):
        entries = [
            _make_triage_entry(
                well_formed=0.92,
                needs_decomp=0.09,
                verdict="ready",
            ),
            _make_triage_entry(
                well_formed=0.50,
                needs_decomp=0.09,
                verdict="ready",
                bias_warning=True,
            ),
        ]
        result = compute_triage_stats(entries)
        assert result["n"] == 1
        assert result["agreement_rate"] == 1.0

    def test_all_bias_warning_returns_empty(self):
        entries = [
            _make_triage_entry(
                well_formed=0.92,
                needs_decomp=0.09,
                verdict="ready",
                bias_warning=True,
            ),
        ]
        result = compute_triage_stats(entries)
        assert result["n"] == 0
        assert result["agreement_rate"] == 0.0
        assert result["weekly_trends"] == []

    def test_excludes_automerge_entries(self):
        entries = [
            _make_automerge_entry(prob=0.75, approved=True),
        ]
        result = compute_triage_stats(entries)
        assert result["n"] == 0
        assert result["agreement_rate"] == 0.0
        assert result["weekly_trends"] == []

    def test_weekly_trends_grouped_by_week(self):
        entries = [
            _make_triage_entry(
                well_formed=0.92,
                needs_decomp=0.09,
                verdict="ready",
                timestamp="2026-09-28T07:00:00+00:00",
            ),
            _make_triage_entry(
                well_formed=0.91,
                needs_decomp=0.11,
                verdict="ready",
                timestamp="2026-10-05T07:00:00+00:00",
            ),
        ]
        result = compute_triage_stats(entries)
        trends = result["weekly_trends"]
        assert len(trends) == 2
        assert trends[0]["week"] == "2026-09-28"
        assert trends[1]["week"] == "2026-10-05"
        assert trends[0]["n"] == 1
        assert trends[0]["agree_count"] == 1
        assert trends[0]["agreement_rate"] == 1.0
        assert trends[1]["agreement_rate"] == 1.0

    def test_mixed_agreement_and_disagreement(self):
        entries = [
            _make_triage_entry(
                well_formed=0.92,
                needs_decomp=0.09,
                verdict="ready",
            ),
            _make_triage_entry(
                well_formed=0.92,
                needs_decomp=0.09,
                verdict="rejected",
            ),
            _make_triage_entry(
                well_formed=0.88,
                needs_decomp=0.78,
                verdict="needs-decomposition",
            ),
        ]
        result = compute_triage_stats(entries)
        assert result["n"] == 3
        assert result["agree_count"] == 2
        assert result["disagree_count"] == 1
        assert abs(result["agreement_rate"] - 2 / 3) < 0.001
        assert result["disagreement_breakdown"] == {"ready-vs-rejected": 1}


# --- compute_automerge_stats ---


class TestComputeAutomergeStats:
    def test_empty_entries(self):
        result = compute_automerge_stats([])
        assert result["mean"] == 0.0
        assert result["median"] == 0.0
        assert result["n_probabilities"] == 0
        assert result["n_with_outcome"] == 0
        assert result["agreement_with_outcome"] == 0.0
        assert all(v == 0 for v in result["distribution"].values())

    def test_exactly_three_gate_aligned_buckets(self):
        result = compute_automerge_stats([])
        keys = list(result["distribution"].keys())
        assert len(keys) == 3
        assert keys[0] == f"Act: reject (below {GATE_LOW})"
        assert keys[1] == f"Pipeline ({GATE_LOW}-{GATE_HIGH})"
        assert keys[2] == f"Act: merge ({GATE_HIGH}+)"

    def test_distribution_gate_buckets(self):
        entries = [
            _make_automerge_entry(prob=0.1),
            _make_automerge_entry(prob=0.3),
            _make_automerge_entry(prob=0.5),
            _make_automerge_entry(prob=0.7),
            _make_automerge_entry(prob=0.9),
        ]
        result = compute_automerge_stats(entries)
        dist = result["distribution"]
        assert dist[f"Act: reject (below {GATE_LOW})"] == 2
        assert dist[f"Pipeline ({GATE_LOW}-{GATE_HIGH})"] == 1
        assert dist[f"Act: merge ({GATE_HIGH}+)"] == 2

    def test_mean_and_median(self):
        entries = [
            _make_automerge_entry(prob=0.2),
            _make_automerge_entry(prob=0.4),
            _make_automerge_entry(prob=0.6),
        ]
        result = compute_automerge_stats(entries)
        assert abs(result["mean"] - 0.4) < 0.001
        assert abs(result["median"] - 0.4) < 0.001
        assert result["n_probabilities"] == 3

    def test_agreement_with_outcome(self):
        entries = [
            _make_automerge_entry(prob=0.8, approved=True),
            _make_automerge_entry(prob=0.3, approved=False),
            _make_automerge_entry(prob=0.8, approved=False),
        ]
        result = compute_automerge_stats(entries)
        assert abs(result["agreement_with_outcome"] - 2 / 3) < 0.001
        assert result["n_with_outcome"] == 3

    def test_excludes_triage_entries(self):
        entries = [
            _make_triage_entry(well_formed=0.92, needs_decomp=0.09, verdict="ready"),
            _make_automerge_entry(prob=0.75, approved=True),
        ]
        result = compute_automerge_stats(entries)
        assert abs(result["mean"] - 0.75) < 0.001
        assert result["n_probabilities"] == 1

    def test_fallback_excluded_from_probability(self):
        entries = [
            _make_automerge_entry(prob=0.5, approved=True),
            _make_automerge_entry(fallback=True),
        ]
        result = compute_automerge_stats(entries)
        assert abs(result["mean"] - 0.5) < 0.001
        assert result["n_probabilities"] == 1

    def test_boundary_0_goes_to_reject_bucket(self):
        entries = [_make_automerge_entry(prob=0.0)]
        result = compute_automerge_stats(entries)
        assert result["distribution"][f"Act: reject (below {GATE_LOW})"] == 1

    def test_boundary_at_gate_low_goes_to_fallback(self):
        entries = [_make_automerge_entry(prob=GATE_LOW)]
        result = compute_automerge_stats(entries)
        assert result["distribution"][f"Pipeline ({GATE_LOW}-{GATE_HIGH})"] == 1

    def test_boundary_at_gate_high_goes_to_merge(self):
        entries = [_make_automerge_entry(prob=GATE_HIGH)]
        result = compute_automerge_stats(entries)
        assert result["distribution"][f"Act: merge ({GATE_HIGH}+)"] == 1

    def test_boundary_1_goes_to_merge_bucket(self):
        entries = [_make_automerge_entry(prob=1.0)]
        result = compute_automerge_stats(entries)
        assert result["distribution"][f"Act: merge ({GATE_HIGH}+)"] == 1

    def test_single_entry(self):
        entries = [_make_automerge_entry(prob=0.55, approved=True)]
        result = compute_automerge_stats(entries)
        assert abs(result["mean"] - 0.55) < 0.001
        assert abs(result["median"] - 0.55) < 0.001
        assert result["agreement_with_outcome"] == 1.0
        assert result["n_probabilities"] == 1
        assert result["n_with_outcome"] == 1


# --- render_jev_md ---


class TestRenderJevMd:
    def test_contains_counts_section(self):
        md = render_jev_md(
            _empty_triage_stats(),
            _empty_automerge_stats(),
            {
                "total": 100,
                "live": 60,
                "backfilled": 40,
                "date_range": "2026-09-25 to 2026-10-05",
            },
        )
        assert "## Counts" in md
        assert "**Total entries:** 100" in md
        assert "**Live entries:** 60" in md
        assert "**Backfilled entries:** 40" in md
        assert "2026-09-25 to 2026-10-05" in md

    def test_uses_agreement_not_calibration(self):
        md = render_jev_md(_empty_triage_stats(), _empty_automerge_stats(), _basic_counts())
        assert "Calibration" not in md
        assert "## Triage Agreement" in md
        assert "## Auto-Merge Agreement" in md

    def test_triage_shows_counts_and_confident_stat(self):
        stats = {
            "n": 20,
            "agree_count": 10,
            "disagree_count": 2,
            "uncertain_count": 8,
            "agreement_rate": 0.5,
            "uncertainty_rate": 0.4,
            "disagreement_breakdown": {"ready-vs-rejected": 2},
            "weekly_trends": [],
        }
        md = render_jev_md(stats, _empty_automerge_stats(), _basic_counts())
        assert "**Agreed:** 10 of 20" in md
        assert "**Disagreed:** 2 of 20" in md
        assert "**Uncertain (fell back):** 8 of 20" in md
        assert "**When confident, agreed:** 10 of 12 (83.3%)" in md
        assert "ready-vs-rejected: 2" in md

    def test_automerge_shows_n_with_rates(self):
        stats = {
            "distribution": _empty_automerge_stats()["distribution"],
            "mean": 0.45,
            "median": 0.42,
            "n_probabilities": 15,
            "n_with_outcome": 12,
            "agreement_with_outcome": 0.667,
        }
        md = render_jev_md(_empty_triage_stats(), stats, _basic_counts())
        assert "(n=15)" in md
        assert "incumbent decision" in md
        assert "8/12" in md

    def test_contains_xychart_bar_block_with_gate_buckets(self):
        stats = _empty_automerge_stats()
        stats["distribution"][f"Act: reject (below {GATE_LOW})"] = 5
        stats["distribution"][f"Pipeline ({GATE_LOW}-{GATE_HIGH})"] = 3
        stats["distribution"][f"Act: merge ({GATE_HIGH}+)"] = 7
        stats["n_probabilities"] = 15
        md = render_jev_md(_empty_triage_stats(), stats, _basic_counts())
        assert "```mermaid" in md
        assert "xychart-beta" in md
        assert "bar [" in md
        assert "Action Zone" in md
        assert f'"Pipeline ({GATE_LOW}-{GATE_HIGH})"' in md

    def test_weekly_trends_rendered_as_table(self):
        triage = {
            "n": 10,
            "agree_count": 5,
            "disagree_count": 0,
            "uncertain_count": 5,
            "agreement_rate": 0.5,
            "uncertainty_rate": 0.5,
            "disagreement_breakdown": {},
            "weekly_trends": [
                {
                    "week": "2026-09-23",
                    "n": 6,
                    "agree_count": 3,
                    "uncertain_count": 2,
                    "agreement_rate": 0.5,
                    "uncertainty_rate": 0.33,
                },
                {
                    "week": "2026-09-30",
                    "n": 4,
                    "agree_count": 2,
                    "uncertain_count": 1,
                    "agreement_rate": 0.5,
                    "uncertainty_rate": 0.25,
                },
            ],
        }
        md = render_jev_md(triage, _empty_automerge_stats(), _basic_counts())
        assert "### Weekly Trends" in md
        assert "| Week | Agreed | Uncertain | n |" in md
        assert "| 9/23 | 3 | 2 | 6 |" in md
        assert "| 9/30 | 2 | 1 | 4 |" in md
        assert "line [" not in md

    def test_no_multiline_chart(self):
        triage = {
            "n": 2,
            "agree_count": 2,
            "disagree_count": 0,
            "uncertain_count": 0,
            "agreement_rate": 1.0,
            "uncertainty_rate": 0.0,
            "disagreement_breakdown": {},
            "weekly_trends": [
                {
                    "week": "2026-09-23",
                    "n": 1,
                    "agree_count": 1,
                    "uncertain_count": 0,
                    "agreement_rate": 1.0,
                    "uncertainty_rate": 0.0,
                },
            ],
        }
        md = render_jev_md(triage, _empty_automerge_stats(), _basic_counts())
        assert "line [" not in md

    def test_no_date_range_when_missing(self):
        md = render_jev_md(
            _empty_triage_stats(),
            _empty_automerge_stats(),
            {"total": 0, "live": 0, "backfilled": 0},
        )
        assert "Date range" not in md

    def test_no_weekly_table_when_no_trends(self):
        md = render_jev_md(
            _empty_triage_stats(),
            _empty_automerge_stats(),
            _basic_counts(),
        )
        assert "Weekly Trends" not in md

    def test_empty_disagreement_not_rendered(self):
        stats = _empty_triage_stats()
        stats["n"] = 1
        stats["agree_count"] = 1
        stats["agreement_rate"] = 1.0
        md = render_jev_md(stats, _empty_automerge_stats(), _basic_counts())
        assert "Disagreement breakdown" not in md

    def test_backfill_section_rendered_separately(self):
        backfill_triage = {
            "n": 5,
            "agree_count": 3,
            "disagree_count": 1,
            "uncertain_count": 1,
            "agreement_rate": 0.6,
            "uncertainty_rate": 0.2,
            "disagreement_breakdown": {},
            "weekly_trends": [],
        }
        md = render_jev_md(
            _empty_triage_stats(),
            _empty_automerge_stats(),
            _basic_counts(),
            backfill_triage_stats=backfill_triage,
        )
        assert "## Backfill" in md
        assert "### Backfill Triage Agreement" in md
        assert "3 of 5" in md

    def test_no_backfill_section_when_empty(self):
        md = render_jev_md(
            _empty_triage_stats(),
            _empty_automerge_stats(),
            _basic_counts(),
            backfill_triage_stats=_empty_triage_stats(),
            backfill_automerge_stats=_empty_automerge_stats(),
        )
        assert "## Backfill" not in md
