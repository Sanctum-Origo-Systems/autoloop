from __future__ import annotations

import json
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from autoloop.config import RepoContext
from autoloop.jev import GATE_HIGH, GATE_LOW, JEV_POINTS, _in_middle_band, band_for

MIN_N = 20
FOOTER_PREFIX = "---\n\n*Generated at "

# Calibration (#495). Each bin includes its lower edge; the last bin also includes 1.0.
CALIBRATION_BIN_EDGES = (0.0, 0.15, 0.35, 0.5, 0.65, 0.85, 1.0)
CALIBRATION_AGREEMENT_THRESHOLD = 0.95
CALIBRATION_MIN_CALLS = 30
# Jev's call on a probability is positive at or above this; band edges can't cross it.
CALIBRATION_CALL_THRESHOLD = 0.5
NOT_ENOUGH_DATA = "not enough data"


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
    # {probability: {low, high}} logged in jev_call["band"]; empty for rows logged before #494.
    band: dict = field(default_factory=dict)
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
    jev_call = dict(raw.get("jev_call") or raw.get("jev") or {})
    # The band is logging metadata, not one of Jev's answers, so it gets its own field.
    band = jev_call.pop("band", None) or {}
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
        band=band,
        raw=raw,
    )


def _config_band(
    point: str,
    prob: str,
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> tuple[float, float]:
    """The configured (low, high) band for point.prob, else the shared gate_low/gate_high.

    bands maps "point.probability" to (low, high), as returned by probability_bands().
    """
    return (bands or {}).get(f"{point}.{prob}", (gate_low, gate_high))


def _row_band(
    entry: JevEntry,
    prob: str,
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> tuple[float, float]:
    """The (low, high) band stored on the row for prob, else the configured band (#505)."""
    low, high = _config_band(entry.point, prob, gate_low, gate_high, bands)
    band = entry.band.get(prob) or {}
    return band.get("low", low), band.get("high", high)


def _is_uncertain(
    entry: JevEntry,
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> bool:
    """Re-bucket a row from its probabilities and its stored band (#494).

    Rows logged without a band keep their logged would_fallback flag.
    """
    jev_call = entry.jev_call
    if jev_call.get("fallback"):
        return True
    if not entry.band:
        return bool(jev_call.get("would_fallback"))
    for prob in JEV_POINTS.get(entry.point, ()):
        p = jev_call.get(prob)
        if p is None:
            continue
        if _in_middle_band(float(p), *_row_band(entry, prob, gate_low, gate_high, bands)):
            return True
    return False


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


def _compute_weekly_trends(
    entries: list[JevEntry],
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> list[dict]:
    weeks: dict[str, list[JevEntry]] = {}
    for e in entries:
        ws = _week_start(e.timestamp)
        if ws:
            weeks.setdefault(ws, []).append(e)
    trends = []
    for week in sorted(weeks):
        week_entries = weeks[week]
        total = len(week_entries)
        uncertain = sum(1 for e in week_entries if _is_uncertain(e, gate_low, gate_high, bands))
        agreed = 0
        disagreed = 0
        for e in week_entries:
            if _is_uncertain(e, gate_low, gate_high, bands):
                continue
            jev_v = _infer_triage_verdict(e.jev_call)
            inc_v = e.incumbent_call.get("verdict", "")
            if jev_v == inc_v:
                agreed += 1
            else:
                disagreed += 1
        trends.append(
            {
                "week": week,
                "n": total,
                "agree_count": agreed,
                "disagree_count": disagreed,
                "uncertain_count": uncertain,
                "agreement_rate": agreed / total if total else 0.0,
                "uncertainty_rate": uncertain / total if total else 0.0,
            }
        )
    return trends


def compute_triage_stats(
    entries: list[JevEntry],
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> dict:
    triage = [e for e in entries if e.point == "triage" and not e.triage_bias_warning]
    if not triage:
        return {
            "n": 0,
            "agree_count": 0,
            "disagree_count": 0,
            "uncertain_count": 0,
            "agreement_rate": 0.0,
            "disagreement_breakdown": {},
            "uncertainty_rate": 0.0,
            "weekly_trends": [],
        }
    agreements = 0
    disagreements: dict[str, int] = {}
    uncertain = 0
    for entry in triage:
        if _is_uncertain(entry, gate_low, gate_high, bands):
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
    disagree_count = sum(disagreements.values())
    return {
        "n": total,
        "agree_count": agreements,
        "disagree_count": disagree_count,
        "uncertain_count": uncertain,
        "agreement_rate": agreements / total if total else 0.0,
        "disagreement_breakdown": disagreements,
        "uncertainty_rate": uncertain / total if total else 0.0,
        "weekly_trends": _compute_weekly_trends(triage, gate_low, gate_high, bands),
    }


def compute_automerge_stats(
    entries: list[JevEntry],
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> dict:
    merge_entries = [e for e in entries if e.point == "auto-merge"]
    # Zone labels name the configured band for this probability (#505).
    cfg_low, cfg_high = _config_band(
        "auto-merge", "meets_acceptance_criteria", gate_low, gate_high, bands
    )
    reject_label = f"Act: reject (below {cfg_low})"
    fallback_label = f"Pipeline ({cfg_low}-{cfg_high})"
    merge_label = f"Act: merge ({cfg_high}+)"

    def zone_for(e: JevEntry, p: float) -> str:
        # Bucket by the band the row was logged with, else the configured band (#494, #505).
        low, high = _row_band(e, "meets_acceptance_criteria", gate_low, gate_high, bands)
        if p < low:
            return reject_label
        if p < high:
            return fallback_label
        return merge_label

    probabilities = []
    buckets = {
        reject_label: 0,
        fallback_label: 0,
        merge_label: 0,
    }
    for e in merge_entries:
        prob = e.jev_call.get("meets_acceptance_criteria")
        if prob is not None:
            probabilities.append(float(prob))
            buckets[zone_for(e, float(prob))] += 1
    mean = statistics.mean(probabilities) if probabilities else 0.0
    median = statistics.median(probabilities) if probabilities else 0.0
    agreed = 0
    total_with_outcome = 0
    zone_agreed = {reject_label: 0, fallback_label: 0, merge_label: 0}
    zone_total = {reject_label: 0, fallback_label: 0, merge_label: 0}
    for e in merge_entries:
        prob = e.jev_call.get("meets_acceptance_criteria")
        approved = e.incumbent_call.get("approved")
        if prob is not None and approved is not None:
            total_with_outcome += 1
            jev_approves = float(prob) >= 0.5
            if jev_approves == approved:
                agreed += 1
            zone = zone_for(e, float(prob))
            zone_total[zone] += 1
            if jev_approves == approved:
                zone_agreed[zone] += 1
    zone_agreement = {
        k: {"agreed": zone_agreed[k], "total": zone_total[k]}
        for k in [reject_label, fallback_label, merge_label]
    }
    return {
        "distribution": buckets,
        "mean": mean,
        "median": median,
        "n_probabilities": len(probabilities),
        "n_with_outcome": total_with_outcome,
        "agreement_with_outcome": (agreed / total_with_outcome if total_with_outcome else 0.0),
        "zone_agreement": zone_agreement,
    }


@dataclass(frozen=True)
class CalibrationPoint:
    point: str
    field: str
    positive_label: str
    always_positive: str
    always_negative: str


CALIBRATION_POINTS = {
    "triage.well_formed": CalibrationPoint(
        "triage", "well_formed", "not rejected", "always accept", "always reject"
    ),
    "triage.needs_decomposition": CalibrationPoint(
        "triage",
        "needs_decomposition",
        "needs-decomposition",
        "always decompose",
        "never decompose",
    ),
    "auto-merge.meets_acceptance_criteria": CalibrationPoint(
        "auto-merge", "meets_acceptance_criteria", "approved", "always approve", "always reject"
    ),
}


def _is_live(entry: JevEntry) -> bool:
    """True unless the row is tagged backfill, shadow, or any non-live source."""
    if entry.is_backfill or entry.raw.get("shadow"):
        return False
    return entry.raw.get("source", "live") == "live"


def _incumbent_positive(entry: JevEntry, spec: CalibrationPoint) -> bool | None:
    """Whether the incumbent's verdict was the positive outcome, or None if it has none."""
    if spec.point == "auto-merge":
        approved = entry.incumbent_call.get("approved")
        return None if approved is None else bool(approved)
    verdict = entry.incumbent_call.get("verdict")
    if not verdict:
        return None
    if spec.field == "needs_decomposition":
        return verdict == "needs-decomposition"
    return verdict != "rejected"


def _calibration_points(entries: list[JevEntry], key: str) -> list[tuple[float, bool]]:
    """(probability, incumbent positive) for live rows that have both."""
    spec = CALIBRATION_POINTS[key]
    points = []
    for e in entries:
        if e.point != spec.point or not _is_live(e):
            continue
        prob = e.jev_call.get(spec.field)
        positive = _incumbent_positive(e, spec)
        if prob is None or positive is None:
            continue
        points.append((float(prob), positive))
    return points


def _bin_label(low: float, high: float) -> str:
    return f"{low:g}–{high:g}"


def calibration_bins(entries: list[JevEntry], key: str) -> list[dict]:
    """One row per calibration bin, in order, including empty bins (n=0, share None)."""
    points = _calibration_points(entries, key)
    edges = CALIBRATION_BIN_EDGES
    bins = []
    for i, (low, high) in enumerate(zip(edges, edges[1:])):
        last = i == len(edges) - 2
        in_bin = [pos for p, pos in points if low <= p and (p < high or last and p <= high)]
        n = len(in_bin)
        positive = sum(in_bin)
        bins.append(
            {
                "label": _bin_label(low, high),
                "n": n,
                "positive": positive,
                "share": positive / n if n else None,
            }
        )
    return bins


def _edge_result(points: list[tuple[float, bool]], candidates: list[float], side: str) -> dict:
    """Widest qualifying edge on one side; candidates must be ordered widest first."""
    enough_data = False
    for edge in candidates:
        if side == "low":
            matched = [not pos for p, pos in points if p < edge]
        else:
            matched = [pos for p, pos in points if p >= edge]
        n = len(matched)
        if n < CALIBRATION_MIN_CALLS:
            continue
        enough_data = True
        rate = sum(matched) / n
        if rate >= CALIBRATION_AGREEMENT_THRESHOLD:
            return {"edge": edge, "agreement": rate, "n": n, "reason": None}
    reason = _no_edge_reason() if enough_data else NOT_ENOUGH_DATA
    return {"edge": None, "agreement": None, "n": 0, "reason": reason}


def _no_edge_reason() -> str:
    return f"no edge reaches {CALIBRATION_AGREEMENT_THRESHOLD:.0%}"


def suggest_band_edges(entries: list[JevEntry], key: str) -> dict:
    """Suggest the widest low and high band edges from the calibration bin edges.

    Below the low edge Jev calls the negative outcome; at or above the high edge it calls
    the positive one. An edge qualifies when Jev's call matched the incumbent on at least
    CALIBRATION_AGREEMENT_THRESHOLD of the CALIBRATION_MIN_CALLS or more calls it covers.
    """
    points = _calibration_points(entries, key)
    inner = CALIBRATION_BIN_EDGES[1:-1]
    lows = sorted((e for e in inner if e <= CALIBRATION_CALL_THRESHOLD), reverse=True)
    highs = sorted(e for e in inner if e >= CALIBRATION_CALL_THRESHOLD)
    return {
        "low": _edge_result(points, lows, "low"),
        "high": _edge_result(points, highs, "high"),
    }


def format_band_edges(edges: dict) -> str:
    low, high = edges["low"], edges["high"]
    if low["edge"] is None and high["edge"] is None:
        if NOT_ENOUGH_DATA == low["reason"] == high["reason"]:
            return NOT_ENOUGH_DATA
        return _no_edge_reason()
    parts = []
    for side, result in (("low", low), ("high", high)):
        if result["edge"] is None:
            parts.append(f"{side}: {result['reason']}")
        else:
            parts.append(f"{side} {result['edge']:g} ({result['agreement']:.0%}, n={result['n']})")
    return "; ".join(parts)


def baseline_agreement(entries: list[JevEntry], key: str) -> dict:
    """Agreement of always picking the incumbent's most common verdict (ties go positive)."""
    spec = CALIBRATION_POINTS[key]
    points = _calibration_points(entries, key)
    n = len(points)
    positive = sum(pos for _, pos in points)
    if positive >= n - positive:
        label, agreed = spec.always_positive, positive
    else:
        label, agreed = spec.always_negative, n - positive
    return {"label": label, "agreement": agreed / n if n else 0.0, "n": n}


def jev_agreement(entries: list[JevEntry], key: str) -> dict:
    """Agreement of Jev's own call (positive at or above CALIBRATION_CALL_THRESHOLD)."""
    points = _calibration_points(entries, key)
    n = len(points)
    agreed = sum((p >= CALIBRATION_CALL_THRESHOLD) == pos for p, pos in points)
    return {"agreement": agreed / n if n else 0.0, "n": n}


def compute_calibration(entries: list[JevEntry]) -> dict:
    return {
        key: {
            "positive_label": spec.positive_label,
            "bins": calibration_bins(entries, key),
            "edges": suggest_band_edges(entries, key),
            "baseline": baseline_agreement(entries, key),
            "jev": jev_agreement(entries, key),
        }
        for key, spec in CALIBRATION_POINTS.items()
    }


def _md_label(date_str: str) -> str:
    parts = date_str.split("-")
    if len(parts) >= 3:
        return f"{int(parts[1])}/{int(parts[2])}"
    return date_str


def _rate_with_n(rate: float, n: int, population: str) -> str:
    """Render a rate with its sample size, e.g. "82% (n=11 live)"."""
    return f"{rate:.0%} (n={n} {population})"


def _compute_verdict(triage_stats: dict, min_n: int = MIN_N) -> str:
    n = triage_stats.get("n", 0)
    agree = triage_stats.get("agree_count", 0)
    disagree = triage_stats.get("disagree_count", 0)
    confident = agree + disagree
    if n < min_n:
        return f"**Verdict:** too few decisions to judge (n={n} live)"
    if confident == 0:
        return f"**Verdict:** no confident calls yet (n={n} live, all uncertain)"
    rate = agree / confident
    return f"**Verdict:** Jev agreed on {_rate_with_n(rate, confident, 'live')} of confident calls"


def _band_overrides(
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> dict[str, tuple[float, float]]:
    """The per-probability bands that differ from the shared gate_low/gate_high.

    Like the calibration section (#494), identical bands count as no overrides.
    """
    if not bands or len(set(bands.values())) <= 1:
        return {}
    return {key: band for key, band in bands.items() if band != (gate_low, gate_high)}


def _band_line(
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> str:
    """The header Band line: the shared band, then each per-probability override (#505)."""
    parts = [f"{gate_low}–{gate_high} (from config)"]
    for key, (low, high) in _band_overrides(gate_low, gate_high, bands).items():
        parts.append(f"{key}: {low}–{high}")
    return f"**Band:** {'; '.join(parts)}"


def _render_legend(
    lines: list[str],
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> None:
    lines.append("## Legend")
    lines.append("")
    lines.append("- **Incumbent decision:** the human/pipeline decision that was actually used")
    lines.append("- **Fell back:** Jev returned an uncertain result and deferred to the incumbent")
    pipeline_text = "confidence too close to call; Jev defers to the incumbent"
    if _band_overrides(gate_low, gate_high, bands):
        # Probabilities have different bands, so list the edges used for each (#505).
        lines.append(f"- **Pipeline:** {pipeline_text}")
        for key, (low, high) in bands.items():
            lines.append(f"  - {key}: {low}–{high}")
    else:
        lines.append(f"- **Pipeline ({gate_low}–{gate_high}):** {pipeline_text}")
    lines.append("- **Backfilled:** a decision reconstructed from historical data, not made live")
    lines.append(
        "- **n=… live / n=… backfill:** the sample behind a number; live and backfilled"
        " decisions are never pooled"
    )
    lines.append("")


def _triage_chart_lines(triage_stats: dict, population: str = "live") -> list[str]:
    n = triage_stats.get("n", 0)
    if n == 0:
        return []
    counts = [
        triage_stats.get("agree_count", 0),
        triage_stats.get("disagree_count", 0),
        triage_stats.get("uncertain_count", 0),
    ]
    return [
        "```mermaid",
        "xychart-beta",
        f'    title "Triage: Jev vs Incumbent (n={n} {population})"',
        '    x-axis ["Agreed", "Disagreed", "Uncertain (fell back)"]',
        f'    y-axis "Decisions" 0 --> {max(counts) + 1}',
        f"    bar [{', '.join(str(c) for c in counts)}]",
        "```",
        "",
    ]


def _automerge_chart_lines(automerge_stats: dict, population: str = "live") -> list[str]:
    n_prob = automerge_stats.get("n_probabilities", 0)
    dist = automerge_stats.get("distribution", {})
    if n_prob == 0 or not dist:
        return []
    bucket_labels = ", ".join(f'"{k}"' for k in dist)
    bucket_values = ", ".join(str(v) for v in dist.values())
    max_val = max(dist.values(), default=1)
    return [
        "```mermaid",
        "xychart-beta",
        f'    title "Auto-Merge Confidence by Action Zone (n={n_prob} {population})"',
        f"    x-axis [{bucket_labels}]",
        f'    y-axis "Count" 0 --> {max_val + 1}',
        f"    bar [{bucket_values}]",
        "```",
        "",
    ]


def _weekly_chart_lines(triage_stats: dict, population: str = "live") -> list[str]:
    points = []
    for w in triage_stats.get("weekly_trends", []):
        agree = w.get("agree_count", 0)
        confident = agree + w.get("disagree_count", 0)
        if confident == 0:
            continue
        points.append((_md_label(w["week"]), confident, round(agree / confident * 100)))
    if not points:
        return []
    labels = ", ".join(f'"{label} (n={n} {population})"' for label, n, _ in points)
    values = ", ".join(str(v) for _, _, v in points)
    return [
        "```mermaid",
        "xychart-beta",
        '    title "Weekly Triage Agreement When Confident"',
        f"    x-axis [{labels}]",
        '    y-axis "% agreed" 0 --> 100',
        f"    bar [{values}]",
        "```",
        "",
    ]


def _chart_lines(triage_stats: dict, automerge_stats: dict, population: str) -> list[str]:
    return (
        _triage_chart_lines(triage_stats, population)
        + _automerge_chart_lines(automerge_stats, population)
        + _weekly_chart_lines(triage_stats, population)
    )


def _render_triage_section(
    lines: list[str],
    triage_stats: dict,
    heading: str = "## Triage Agreement",
    min_n: int = MIN_N,
    population: str = "live",
) -> None:
    lines.append(heading)
    lines.append("")
    n = triage_stats.get("n", 0)
    agree = triage_stats.get("agree_count", 0)
    disagree = triage_stats.get("disagree_count", 0)
    uncertain = triage_stats.get("uncertain_count", 0)
    lines.append(f"- **Agreed:** {agree} of {n}")
    lines.append(f"- **Disagreed:** {disagree} of {n}")
    lines.append(f"- **Uncertain (fell back):** {uncertain} of {n}")
    confident = agree + disagree
    if confident > 0:
        rate = agree / confident
        if n >= min_n:
            lines.append(
                f"- **When confident, agreed:** {agree} of {confident}"
                f" — {_rate_with_n(rate, confident, population)}"
            )
        else:
            lines.append(
                f"- **When confident, agreed:** {agree} of {confident}"
                f" (too few to judge, n={n} {population})"
            )
    if triage_stats.get("disagreement_breakdown"):
        lines.append("- **Disagreement breakdown:**")
        for cat, count in sorted(triage_stats["disagreement_breakdown"].items()):
            lines.append(f"  - {cat}: {count}")
    lines.append("")

    weekly = triage_stats.get("weekly_trends", [])
    if weekly:
        lines.append("### Weekly Trends")
        lines.append("")
        lines.append("| Week | Agreed | Disagreed | Uncertain | n |")
        lines.append("|------|--------|-----------|-----------|---|")
        for w in weekly:
            wn = w.get("n", 0)
            wa = w.get("agree_count", 0)
            wd = w.get("disagree_count", 0)
            wu = w.get("uncertain_count", 0)
            lines.append(f"| {_md_label(w['week'])} | {wa} | {wd} | {wu} | {wn} |")
        lines.append("")


def _render_automerge_section(
    lines: list[str],
    automerge_stats: dict,
    heading: str = "## Auto-Merge Agreement",
    min_n: int = MIN_N,
    population: str = "live",
) -> None:
    lines.append(heading)
    lines.append("")
    n_prob = automerge_stats.get("n_probabilities", 0)
    mean = automerge_stats["mean"]
    lines.append(f"- **Mean confidence:** {mean:.3f} (n={n_prob} {population})")
    md_conf = automerge_stats["median"]
    lines.append(f"- **Median confidence:** {md_conf:.3f} (n={n_prob} {population})")
    awo = automerge_stats["agreement_with_outcome"]
    n_out = automerge_stats.get("n_with_outcome", 0)
    agreed = round(awo * n_out)
    if n_out < min_n:
        # Agreement is judged on decisions with an incumbent outcome, so that is the n
        # that has to clear min_n, not the (larger) count of confidence scores.
        detail = "no outcomes yet" if n_out == 0 else f"{agreed}/{n_out} agreed"
        lines.append(
            f"- **Agreement with incumbent decision:** not enough {population} data yet;"
            f" n={n_out} {population} ({detail}, need {min_n} to judge)"
        )
    else:
        lines.append(
            f"- **Agreement with incumbent decision:** {agreed}/{n_out}"
            f" — {_rate_with_n(awo, n_out, population)}"
        )
    lines.append("")

    zone_agreement = automerge_stats.get("zone_agreement", {})
    if zone_agreement and any(v["total"] > 0 for v in zone_agreement.values()):
        lines.append("### Per-Zone Agreement")
        lines.append("")
        lines.append("| Zone | Agreed | Total | Agreement |")
        lines.append("|------|--------|-------|-----------|")
        for zone, data in zone_agreement.items():
            za = data["agreed"]
            zt = data["total"]
            if zt == 0:
                lines.append(f"| {zone} | — | 0 | no outcomes yet |")
            elif n_out < min_n:
                lines.append(f"| {zone} | {za} | {zt} | too few to judge |")
            else:
                lines.append(f"| {zone} | {za} | {zt} | {_rate_with_n(za / zt, zt, population)} |")
        lines.append("")


def probability_bands(
    bands: dict | None = None, gate_low: float = GATE_LOW, gate_high: float = GATE_HIGH
) -> dict[str, tuple[float, float]]:
    """(low, high) band per calibration key; bands=None uses the built-in DEFAULT_BANDS."""
    return {
        key: band_for(spec.point, spec.field, bands, gate_low, gate_high)
        for key, spec in CALIBRATION_POINTS.items()
    }


def _render_calibration_section(
    lines: list[str], calibration: dict, bands: dict[str, tuple[float, float]] | None = None
) -> None:
    # Per-probability bands are only shown when they differ (#494).
    show_bands = bool(bands) and len(set(bands.values())) > 1
    lines.append("## Calibration")
    lines.append("")
    lines.append(
        "Live rows only; shadow and backfilled rows are excluded. A suggested band edge"
        f" needs Jev to match the incumbent on at least {CALIBRATION_AGREEMENT_THRESHOLD:.0%}"
        f" of the calls beyond it, with at least n={CALIBRATION_MIN_CALLS} calls."
    )
    lines.append("")
    for key, data in calibration.items():
        lines.append(f"### {key}")
        lines.append("")
        lines.append(f"| Bin | n | % {data['positive_label']} |")
        lines.append("|-----|---|---|")
        for b in data["bins"]:
            share = "—" if b["share"] is None else f"{b['share']:.0%}"
            lines.append(f"| {b['label']} | n={b['n']} | {share} |")
        lines.append("")
        baseline = data["baseline"]
        jev = data["jev"]
        edges = data["edges"]
        edge_text = format_band_edges(edges)
        if edges["low"]["edge"] is None and edges["high"]["edge"] is None:
            edge_text += f" (n={baseline['n']})"
        if show_bands and key in bands:
            low, high = bands[key]
            lines.append(f"- **Band:** {low}–{high} (from config)")
        lines.append(f"- **Suggested band edges:** {edge_text}")
        if baseline["n"] == 0:
            lines.append("- **Baseline:** no live calls yet (n=0)")
        else:
            lines.append(
                f"- **Baseline:** {baseline['label']}: {baseline['agreement']:.0%},"
                f" n={baseline['n']} vs Jev: {jev['agreement']:.0%}, n={jev['n']}"
            )
        lines.append("")


def render_jev_md(
    triage_stats: dict,
    automerge_stats: dict,
    counts: dict,
    *,
    backfill_triage_stats: dict | None = None,
    backfill_automerge_stats: dict | None = None,
    calibration: dict | None = None,
    min_n: int = MIN_N,
    generated_at: str | None = None,
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict[str, tuple[float, float]] | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# JEV Report")
    lines.append("")
    lines.append(_compute_verdict(triage_stats, min_n))
    lines.append("")
    lines.append(_band_line(gate_low, gate_high, bands))
    lines.append("")

    # Charts go right after the verdict so readers see them without scrolling (#482).
    chart_lines = _chart_lines(triage_stats, automerge_stats, "live")
    if chart_lines:
        lines.append("## Charts")
        lines.append("")
        lines.extend(chart_lines)

    lines.append("## Counts")
    lines.append("")
    lines.append(f"- **Total entries:** {counts.get('total', 0)}")
    lines.append(f"- **Live entries:** {counts.get('live', 0)}")
    lines.append(f"- **Backfilled entries:** {counts.get('backfilled', 0)}")
    date_range = counts.get("date_range", "")
    if date_range:
        lines.append(f"- **Date range:** {date_range}")
    lines.append("")

    _render_triage_section(lines, triage_stats, min_n=min_n)
    _render_automerge_section(lines, automerge_stats, min_n=min_n)

    if calibration:
        _render_calibration_section(lines, calibration, bands)

    has_backfill = (
        backfill_triage_stats
        and backfill_triage_stats.get("n", 0) > 0
        or backfill_automerge_stats
        and backfill_automerge_stats.get("n_probabilities", 0) > 0
    )
    if has_backfill:
        lines.append("## Backfill")
        lines.append("")
        bf_triage = backfill_triage_stats or {}
        bf_automerge = backfill_automerge_stats or {}
        lines.extend(_chart_lines(bf_triage, bf_automerge, "backfill"))
        if bf_triage.get("n", 0) > 0:
            _render_triage_section(
                lines,
                bf_triage,
                "### Backfill Triage Agreement",
                min_n=min_n,
                population="backfill",
            )
        if bf_automerge.get("n_probabilities", 0) > 0:
            _render_automerge_section(
                lines,
                bf_automerge,
                "### Backfill Auto-Merge Agreement",
                min_n=min_n,
                population="backfill",
            )

    _render_legend(lines, gate_low, gate_high, bands)

    if generated_at:
        lines.append(f"{FOOTER_PREFIX}{generated_at}*")

    return "\n".join(lines)


def _without_footer(text: str) -> str:
    """Return JEV.md content without the generated-at footer, which changes every run."""
    return text.partition(FOOTER_PREFIX)[0].rstrip()


def _published_jev_md(ctx: RepoContext) -> str | None:
    """Return JEV.md as committed on main, or None if main has no JEV.md."""
    try:
        result = subprocess.run(
            ["git", "show", "main:JEV.md"],
            capture_output=True,
            text=True,
            cwd=str(ctx.repo_dir),
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _publish_via_pr(ctx: RepoContext, jev_path: Path, date: str) -> str:
    branch_name = f"chore/jev-report-{date}"
    pr_created = False
    repo_dir = str(ctx.repo_dir)
    try:
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            capture_output=True,
            text=True,
            cwd=repo_dir,
        )
    except FileNotFoundError:
        return "Error: git not found"
    if result.returncode != 0:
        return f"Error: could not create branch {branch_name}"

    try:
        try:
            result = subprocess.run(
                ["git", "add", str(jev_path)],
                capture_output=True,
                text=True,
                cwd=repo_dir,
            )
        except FileNotFoundError:
            return "Error: git not found"
        if result.returncode != 0:
            return "Error: git add failed"

        commit_msg = f"chore: update jev report ({date})"
        try:
            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                capture_output=True,
                text=True,
                cwd=repo_dir,
            )
        except FileNotFoundError:
            return "Error: git not found"
        if result.returncode != 0:
            return "Error: git commit failed"

        try:
            result = subprocess.run(
                ["git", "push", "-u", "origin", branch_name],
                capture_output=True,
                text=True,
                cwd=repo_dir,
            )
        except FileNotFoundError:
            return "Error: git not found"
        if result.returncode != 0:
            return f"Error: git push failed\n{result.stderr}"

        pr_title = f"chore: update jev report ({date})"
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    pr_title,
                    "--body",
                    "Automated JEV report update.",
                ],
                capture_output=True,
                text=True,
                cwd=repo_dir,
            )
        except FileNotFoundError:
            return "Error: gh not found"
        if result.returncode != 0:
            return f"Error: PR creation failed\n{result.stderr}"

        pr_created = True
        return f"PR created: {result.stdout.strip()}"
    finally:
        try:
            subprocess.run(
                ["git", "checkout", "main"],
                capture_output=True,
                cwd=repo_dir,
            )
            if not pr_created:
                subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    capture_output=True,
                    cwd=repo_dir,
                )
        except FileNotFoundError:
            pass


def build_report(
    parsed: ParsedDecisions,
    generated_at: str | None = None,
    *,
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict | None = None,
) -> str:
    """Compute every stat from parsed decision rows and render JEV.md.

    bands is the [jev.bands] config table; None uses the built-in DEFAULT_BANDS.
    """
    resolved = probability_bands(bands, gate_low, gate_high)
    triage_stats = compute_triage_stats(parsed.live, gate_low, gate_high, resolved)
    automerge_stats = compute_automerge_stats(parsed.live, gate_low, gate_high, resolved)

    backfill_triage_stats = compute_triage_stats(parsed.backfilled, gate_low, gate_high, resolved)
    backfill_automerge_stats = compute_automerge_stats(
        parsed.backfilled, gate_low, gate_high, resolved
    )

    all_entries = parsed.backfilled + parsed.live
    timestamps = [e.timestamp for e in all_entries if e.timestamp]
    if timestamps:
        date_range = f"{min(timestamps)[:10]} to {max(timestamps)[:10]}"
    else:
        date_range = ""

    counts = {
        "total": len(all_entries),
        "live": len(parsed.live),
        "backfilled": len(parsed.backfilled),
        "date_range": date_range,
    }

    return render_jev_md(
        triage_stats,
        automerge_stats,
        counts,
        backfill_triage_stats=backfill_triage_stats,
        backfill_automerge_stats=backfill_automerge_stats,
        calibration=compute_calibration(parsed.live),
        generated_at=generated_at,
        gate_low=gate_low,
        gate_high=gate_high,
        bands=resolved,
    )


def main(
    *,
    ctx: RepoContext,
    publish: bool = False,
    pr: bool = False,
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
    bands: dict | None = None,
):
    decisions_path = ctx.data_dir / "jev_decisions.jsonl"
    parsed = parse_jev_decisions(decisions_path)
    content = build_report(
        parsed,
        datetime.now().strftime("%Y-%m-%d %H:%M UTC"),
        gate_low=gate_low,
        gate_high=gate_high,
        bands=bands,
    )
    jev_path = ctx.repo_dir / "JEV.md"

    if not publish:
        jev_path.write_text(content)
        print(f"JEV.md written to {jev_path}")
        return

    repo_dir = str(ctx.repo_dir)

    try:
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            cwd=repo_dir,
        )
    except FileNotFoundError:
        print("Error: git not found")
        return
    if branch_result.returncode != 0 or branch_result.stdout.strip() != "main":
        print("Error: --publish must be run from the main branch")
        return

    published = _published_jev_md(ctx)
    if published is not None and _without_footer(published) == _without_footer(content):
        # Only the generated-at footer would change; don't open an identical PR (#482).
        print("JEV.md unchanged from main, skipping publish")
        return

    jev_path.write_text(content)
    print(f"JEV.md written to {jev_path}")

    date = datetime.now().strftime("%Y-%m-%d")

    if pr:
        print(_publish_via_pr(ctx, jev_path, date))
        return

    try:
        add_result = subprocess.run(["git", "add", str(jev_path)], cwd=repo_dir)
    except FileNotFoundError:
        print("Error: git not found")
        return
    if add_result.returncode != 0:
        print("Error: git add failed")
        return

    try:
        commit_result = subprocess.run(
            ["git", "commit", "-m", f"chore: update jev report ({date})"],
            cwd=repo_dir,
        )
    except FileNotFoundError:
        print("Error: git not found")
        return
    if commit_result.returncode != 0:
        print("Error: git commit failed")
        return

    print(f"JEV.md committed: chore: update jev report ({date})")

    try:
        push_result = subprocess.run(
            ["git", "push"],
            capture_output=True,
            text=True,
            cwd=repo_dir,
        )
    except FileNotFoundError:
        print("Error: git not found")
        return
    if push_result.returncode != 0:
        stderr = push_result.stderr
        if "rule violations" in stderr or "protected branch" in stderr:
            print(
                "Error: push failed — branch protection is enabled.\n"
                "Hint: use --publish --pr to create a PR instead."
            )
        else:
            print(f"Error: git push failed\n{stderr}")
        return
