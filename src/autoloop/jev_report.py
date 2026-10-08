from __future__ import annotations

import json
import statistics
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from autoloop.config import RepoContext
from autoloop.jev import GATE_LOW, GATE_HIGH


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
                "n": total,
                "agree_count": agreed,
                "uncertain_count": uncertain,
                "agreement_rate": agreed / total if total else 0.0,
                "uncertainty_rate": uncertain / total if total else 0.0,
            }
        )
    return trends


def compute_triage_stats(entries: list[JevEntry]) -> dict:
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
    disagree_count = sum(disagreements.values())
    return {
        "n": total,
        "agree_count": agreements,
        "disagree_count": disagree_count,
        "uncertain_count": uncertain,
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
    gate_low = GATE_LOW
    gate_high = GATE_HIGH
    reject_label = f"<{gate_low}"
    fallback_label = f"{gate_low}-{gate_high}"
    merge_label = f">={gate_high}"
    buckets = {
        reject_label: 0,
        fallback_label: 0,
        merge_label: 0,
    }
    for p in probabilities:
        if p < gate_low:
            buckets[reject_label] += 1
        elif p < gate_high:
            buckets[fallback_label] += 1
        else:
            buckets[merge_label] += 1
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
        "n_probabilities": len(probabilities),
        "n_with_outcome": total_with_outcome,
        "agreement_with_outcome": (agreed / total_with_outcome if total_with_outcome else 0.0),
    }


def _md_label(date_str: str) -> str:
    parts = date_str.split("-")
    if len(parts) >= 3:
        return f"{int(parts[1])}/{int(parts[2])}"
    return date_str


def _render_triage_section(
    lines: list[str], triage_stats: dict, heading: str = "## Triage Agreement"
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
        lines.append(f"- **When confident, agreed:** {agree} of {confident} ({rate:.1%})")
    if triage_stats.get("disagreement_breakdown"):
        lines.append("- **Disagreement breakdown:**")
        for cat, count in sorted(triage_stats["disagreement_breakdown"].items()):
            lines.append(f"  - {cat}: {count}")
    lines.append("")

    weekly = triage_stats.get("weekly_trends", [])
    if weekly:
        lines.append("### Weekly Trends")
        lines.append("")
        lines.append("| Week | Agreed | Uncertain | n |")
        lines.append("|------|--------|-----------|---|")
        for w in weekly:
            wn = w.get("n", 0)
            wa = w.get("agree_count", 0)
            wu = w.get("uncertain_count", 0)
            lines.append(f"| {_md_label(w['week'])} | {wa} | {wu} | {wn} |")
        lines.append("")


def _render_automerge_section(
    lines: list[str], automerge_stats: dict, heading: str = "## Auto-Merge Agreement"
) -> None:
    lines.append(heading)
    lines.append("")
    n_prob = automerge_stats.get("n_probabilities", 0)
    lines.append(f"- **Mean confidence:** {automerge_stats['mean']:.3f} (n={n_prob})")
    md_conf = automerge_stats["median"]
    lines.append(f"- **Median confidence:** {md_conf:.3f} (n={n_prob})")
    awo = automerge_stats["agreement_with_outcome"]
    n_out = automerge_stats.get("n_with_outcome", 0)
    lines.append(
        f"- **Agreement with incumbent decision:** {awo:.1%} ({round(awo * n_out)}/{n_out})"
    )
    lines.append("")

    dist = automerge_stats.get("distribution", {})
    if dist:
        bucket_labels = ", ".join(f'"{k}"' for k in dist)
        bucket_values = ", ".join(str(v) for v in dist.values())
        max_val = max(dist.values(), default=1)
        lines.append("```mermaid")
        lines.append("xychart-beta")
        lines.append('    title "Auto-Merge Confidence by Action Zone"')
        lines.append(f"    x-axis [{bucket_labels}]")
        lines.append(f'    y-axis "Count" 0 --> {max_val + 1}')
        lines.append(f"    bar [{bucket_values}]")
        lines.append("```")
        lines.append("")


def render_jev_md(
    triage_stats: dict,
    automerge_stats: dict,
    counts: dict,
    *,
    backfill_triage_stats: dict | None = None,
    backfill_automerge_stats: dict | None = None,
) -> str:
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

    _render_triage_section(lines, triage_stats)
    _render_automerge_section(lines, automerge_stats)

    has_backfill = (
        backfill_triage_stats
        and backfill_triage_stats.get("n", 0) > 0
        or backfill_automerge_stats
        and backfill_automerge_stats.get("n_probabilities", 0) > 0
    )
    if has_backfill:
        lines.append("## Backfill")
        lines.append("")
        if backfill_triage_stats and backfill_triage_stats.get("n", 0) > 0:
            _render_triage_section(lines, backfill_triage_stats, "### Backfill Triage Agreement")
        if backfill_automerge_stats and backfill_automerge_stats.get("n_probabilities", 0) > 0:
            _render_automerge_section(
                lines, backfill_automerge_stats, "### Backfill Auto-Merge Agreement"
            )

    return "\n".join(lines)


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


def main(*, ctx: RepoContext, publish: bool = False, pr: bool = False):
    decisions_path = ctx.data_dir / "jev_decisions.jsonl"
    parsed = parse_jev_decisions(decisions_path)

    triage_stats = compute_triage_stats(parsed.live)
    automerge_stats = compute_automerge_stats(parsed.live)

    backfill_triage_stats = compute_triage_stats(parsed.backfilled)
    backfill_automerge_stats = compute_automerge_stats(parsed.backfilled)

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

    content = render_jev_md(
        triage_stats,
        automerge_stats,
        counts,
        backfill_triage_stats=backfill_triage_stats,
        backfill_automerge_stats=backfill_automerge_stats,
    )
    jev_path = ctx.repo_dir / "JEV.md"
    jev_path.write_text(content)
    print(f"JEV.md written to {jev_path}")

    if not publish:
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
