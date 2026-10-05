from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path


@dataclass
class JevEntry:
    timestamp: str
    issue: int
    point: str
    jev_call: dict
    incumbent_call: dict
    outcome: str | None = None
    is_backfill: bool = False
    triage_bias_warning: bool = False
    pr: int | None = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class ParsedDecisions:
    backfilled: list[JevEntry]
    live: list[JevEntry]


def parse_jev_decisions(path: str | Path) -> ParsedDecisions:
    path = Path(path)
    backfilled: list[JevEntry] = []
    live: list[JevEntry] = []
    if not path.exists():
        return ParsedDecisions(backfilled=backfilled, live=live)
    text = path.read_text()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        entry = _parse_entry(raw)
        if entry.is_backfill:
            backfilled.append(entry)
        else:
            live.append(entry)
    return ParsedDecisions(backfilled=backfilled, live=live)


def _infer_point(raw: dict) -> str:
    if "point" in raw:
        return raw["point"]
    inc = raw.get("incumbent_call") or raw.get("incumbent") or {}
    if "verdict" in inc:
        return "triage"
    jev = raw.get("jev_call") or raw.get("jev") or {}
    if "meets_acceptance_criteria" in jev:
        return "auto-merge"
    if "approved" in inc:
        return "auto-merge"
    return "triage"


def _parse_entry(raw: dict) -> JevEntry:
    point = _infer_point(raw)
    jev_call = raw.get("jev_call") or raw.get("jev") or {}
    incumbent_call = raw.get("incumbent_call") or raw.get("incumbent") or {}
    is_backfill = bool(
        raw.get("backfill") or raw.get("forensic_backfill") or raw.get("is_backfill")
    )
    return JevEntry(
        timestamp=raw.get("timestamp", ""),
        issue=raw.get("issue", 0),
        point=point,
        jev_call=jev_call,
        incumbent_call=incumbent_call,
        outcome=raw.get("outcome"),
        is_backfill=is_backfill,
        triage_bias_warning=bool(raw.get("triage_bias_warning", False)),
        pr=raw.get("pr"),
        raw=raw,
    )


def _is_uncertain(jev_call: dict) -> bool:
    return bool(jev_call.get("fallback") or jev_call.get("would_fallback"))


def _infer_triage_verdict(jev_call: dict) -> str:
    needs_decomp = jev_call.get("needs_decomposition", 0.0)
    well_formed = jev_call.get("well_formed", 0.0)
    if needs_decomp >= 0.5:
        return "needs-decomposition"
    if well_formed < 0.85:
        return "rejected"
    return "ready"


def _week_start(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return ""
    monday = dt.date() - timedelta(days=dt.weekday())
    return monday.isoformat()


def _compute_weekly_trends(entries: list[JevEntry]) -> list[dict]:
    weeks: dict[str, list[JevEntry]] = {}
    for e in entries:
        ws = _week_start(e.timestamp)
        if ws:
            weeks.setdefault(ws, []).append(e)
    trends = []
    for week in sorted(weeks):
        week_entries = weeks[week]
        total = len(week_entries)
        uncertain = sum(1 for e in week_entries if _is_uncertain(e.jev_call))
        agreed = 0
        for e in week_entries:
            if _is_uncertain(e.jev_call):
                continue
            jev_v = _infer_triage_verdict(e.jev_call)
            inc_v = e.incumbent_call.get("verdict", "")
            if jev_v == inc_v:
                agreed += 1
        trends.append(
            {
                "week": week,
                "agreement_rate": agreed / total if total else 0.0,
                "uncertainty_rate": uncertain / total if total else 0.0,
            }
        )
    return trends


def compute_triage_stats(entries: list[JevEntry]) -> dict:
    triage = [e for e in entries if e.point == "triage" and not e.triage_bias_warning]
    if not triage:
        return {
            "agreement_rate": 0.0,
            "disagreement_breakdown": {},
            "uncertainty_rate": 0.0,
            "weekly_trends": [],
        }
    agreements = 0
    disagreements: dict[str, int] = {}
    uncertain = 0
    for entry in triage:
        if _is_uncertain(entry.jev_call):
            uncertain += 1
            continue
        jev_verdict = _infer_triage_verdict(entry.jev_call)
        inc_verdict = entry.incumbent_call.get("verdict", "")
        if jev_verdict == inc_verdict:
            agreements += 1
        else:
            key = f"{jev_verdict}-vs-{inc_verdict}"
            disagreements[key] = disagreements.get(key, 0) + 1
    total = len(triage)
    return {
        "agreement_rate": agreements / total if total else 0.0,
        "disagreement_breakdown": disagreements,
        "uncertainty_rate": uncertain / total if total else 0.0,
        "weekly_trends": _compute_weekly_trends(triage),
    }


def compute_automerge_stats(entries: list[JevEntry]) -> dict:
    merge_entries = [e for e in entries if e.point == "auto-merge"]
    probabilities = []
    for e in merge_entries:
        prob = e.jev_call.get("meets_acceptance_criteria")
        if prob is not None:
            probabilities.append(float(prob))
    buckets = {
        "0.0-0.2": 0,
        "0.2-0.4": 0,
        "0.4-0.6": 0,
        "0.6-0.8": 0,
        "0.8-1.0": 0,
    }
    for p in probabilities:
        if p < 0.2:
            buckets["0.0-0.2"] += 1
        elif p < 0.4:
            buckets["0.2-0.4"] += 1
        elif p < 0.6:
            buckets["0.4-0.6"] += 1
        elif p < 0.8:
            buckets["0.6-0.8"] += 1
        else:
            buckets["0.8-1.0"] += 1
    mean = statistics.mean(probabilities) if probabilities else 0.0
    median = statistics.median(probabilities) if probabilities else 0.0
    agreed = 0
    total_with_outcome = 0
    for e in merge_entries:
        prob = e.jev_call.get("meets_acceptance_criteria")
        approved = e.incumbent_call.get("approved")
        if prob is not None and approved is not None:
            total_with_outcome += 1
            jev_approves = float(prob) >= 0.5
            if jev_approves == approved:
                agreed += 1
    return {
        "distribution": buckets,
        "mean": mean,
        "median": median,
        "agreement_with_outcome": (agreed / total_with_outcome if total_with_outcome else 0.0),
    }


def _md_label(date_str: str) -> str:
    parts = date_str.split("-")
    if len(parts) >= 3:
        return f"{int(parts[1])}/{int(parts[2])}"
    return date_str


def render_jev_md(triage_stats: dict, automerge_stats: dict, counts: dict) -> str:
    lines: list[str] = []
    lines.append("# JEV Report")
    lines.append("")

    lines.append("## Counts")
    lines.append("")
    lines.append(f"- **Total entries:** {counts.get('total', 0)}")
    lines.append(f"- **Live entries:** {counts.get('live', 0)}")
    lines.append(f"- **Backfilled entries:** {counts.get('backfilled', 0)}")
    date_range = counts.get("date_range", "")
    if date_range:
        lines.append(f"- **Date range:** {date_range}")
    lines.append("")

    lines.append("## Triage Calibration")
    lines.append("")
    ar = triage_stats["agreement_rate"]
    ur = triage_stats["uncertainty_rate"]
    lines.append(f"- **Agreement rate:** {ar:.1%}")
    lines.append(f"- **Uncertainty rate:** {ur:.1%}")
    if triage_stats.get("disagreement_breakdown"):
        lines.append("- **Disagreement breakdown:**")
        for cat, count in sorted(triage_stats["disagreement_breakdown"].items()):
            lines.append(f"  - {cat}: {count}")
    lines.append("")

    lines.append("## Auto-Merge Calibration")
    lines.append("")
    lines.append(f"- **Mean confidence:** {automerge_stats['mean']:.3f}")
    md_conf = automerge_stats["median"]
    lines.append(f"- **Median confidence:** {md_conf:.3f}")
    awo = automerge_stats["agreement_with_outcome"]
    lines.append(f"- **Agreement with outcome:** {awo:.1%}")
    lines.append("")

    dist = automerge_stats.get("distribution", {})
    if dist:
        bucket_labels = ", ".join(f'"{k}"' for k in dist)
        bucket_values = ", ".join(str(v) for v in dist.values())
        max_val = max(dist.values(), default=1)
        lines.append("```mermaid")
        lines.append("xychart-beta")
        lines.append('    title "Auto-Merge Confidence Distribution"')
        lines.append(f"    x-axis [{bucket_labels}]")
        lines.append(f'    y-axis "Count" 0 --> {max_val + 1}')
        lines.append(f"    bar [{bucket_values}]")
        lines.append("```")
        lines.append("")

    weekly = triage_stats.get("weekly_trends", [])
    if weekly:
        wl = ", ".join(f'"{_md_label(w["week"])}"' for w in weekly)
        av = ", ".join(str(round(w["agreement_rate"] * 100)) for w in weekly)
        uv = ", ".join(str(round(w["uncertainty_rate"] * 100)) for w in weekly)
        lines.append("```mermaid")
        lines.append("xychart-beta")
        lines.append('    title "Weekly Agreement & Uncertainty Trends"')
        lines.append(f"    x-axis [{wl}]")
        lines.append('    y-axis "%" 0 --> 100')
        lines.append(f"    line [{av}]")
        lines.append(f"    line [{uv}]")
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
