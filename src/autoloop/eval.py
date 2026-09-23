"""Eval command: deterministic performance tracking from run history + PR data.

Reads run_history.jsonl and GitHub PR data to compute builder performance
metrics. No LLM calls. Purely deterministic.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_PR_LIMIT = 500


def load_run_history(base: Path | None = None) -> list[dict]:
    log_file = (base or Path.cwd()) / "autoloop" / "run_history.jsonl"
    if not log_file.exists():
        return []
    entries = []
    for line in log_file.read_text().strip().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def classify_module(changed_files: list[str], prefixes: list[str] | None = None) -> str:
    if prefixes is None:
        prefixes = _detect_module_prefixes(changed_files)
    sorted_prefixes = sorted(prefixes, key=len, reverse=True)
    counts: dict[str, int] = {}
    for f in changed_files:
        for p in sorted_prefixes:
            if f.startswith(p):
                counts[p] = counts.get(p, 0) + 1
                break
    if not counts:
        return "other"
    return max(counts, key=lambda p: counts[p])


def _detect_module_prefixes(changed_files: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    for f in changed_files:
        parts = f.split("/")
        if len(parts) >= 3 and parts[0] == "src":
            if "." not in parts[2]:
                prefix = "/".join(parts[:3]) + "/"
            else:
                prefix = "/".join(parts[:2]) + "/"
            seen[prefix] = seen.get(prefix, 0) + 1
        elif len(parts) >= 2 and parts[0] == "src":
            prefix = "/".join(parts[:2]) + "/"
            seen[prefix] = seen.get(prefix, 0) + 1
    return sorted(seen, key=lambda p: seen[p], reverse=True)


def compute_snapshot(
    runs: list[dict],
    pr_data: list[dict] | None = None,
    date: str | None = None,
    module_prefixes: list[str] | None = None,
    prev_snapshot: dict | None = None,
) -> dict:
    impl_runs = [r for r in runs if r.get("type", "implement") == "implement"]

    total = len(impl_runs)
    first_attempt = sum(1 for r in impl_runs if r.get("success") and r.get("attempts", 1) == 1)
    first_attempt_rate = first_attempt / total if total else 0.0

    costs = [r.get("cost_usd", 0) for r in impl_runs]
    avg_cost = sum(costs) / len(costs) if costs else 0.0

    durations = [r.get("duration_seconds", 0) for r in impl_runs]
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    attempt_dist = {"1": 0, "2": 0, "3+": 0}
    for r in impl_runs:
        a = r.get("attempts", 1)
        if a == 1:
            attempt_dist["1"] += 1
        elif a == 2:
            attempt_dist["2"] += 1
        else:
            attempt_dist["3+"] += 1

    issue_cost: dict[int, float] = {}
    for r in impl_runs:
        issue = r.get("issue", 0)
        if issue:
            issue_cost[issue] = r.get("cost_usd", 0)

    modules: dict[str, dict] = {}
    if pr_data:
        effective_prefixes = module_prefixes
        if effective_prefixes is None:
            all_files = [f for pr in pr_data for f in pr.get("changed_files", [])]
            effective_prefixes = _detect_module_prefixes(all_files)
        for pr in pr_data:
            files = pr.get("changed_files", [])
            mod = classify_module(files, effective_prefixes)
            if mod not in modules:
                modules[mod] = {
                    "implementations": 0,
                    "first_attempt_successes": 0,
                    "total_cost": 0.0,
                    "merged_count": 0,
                    "human_edit_count": 0,
                }
            modules[mod]["implementations"] += 1
            if pr.get("first_attempt_success", False):
                modules[mod]["first_attempt_successes"] += 1
            issue = pr.get("issue")
            if issue and issue in issue_cost:
                modules[mod]["total_cost"] += issue_cost[issue]
            if pr.get("merged", False):
                modules[mod]["merged_count"] += 1
                if pr.get("human_edited", False):
                    modules[mod]["human_edit_count"] += 1

    module_stats = {}
    for mod, stats in sorted(modules.items()):
        imp = stats["implementations"]
        fa = stats["first_attempt_successes"]
        merged = stats["merged_count"]
        module_stats[mod] = {
            "implementations": imp,
            "first_attempt_rate": round(fa / imp, 2) if imp else 0.0,
            "avg_cost_usd": round(stats["total_cost"] / imp, 2) if imp else 0.0,
            "human_edit_rate": round(stats["human_edit_count"] / merged, 2) if merged else 0.0,
            "merged_clean_count": merged - stats["human_edit_count"],
        }

    human_edit_rate = 0.0
    human_edit_count = 0
    merged_pr_count = 0
    closed_without_merge = 0
    if pr_data:
        merged_prs = [p for p in pr_data if p.get("merged", False)]
        merged_pr_count = len(merged_prs)
        human_edit_count = sum(1 for p in merged_prs if p.get("human_edited", False))
        human_edit_rate = human_edit_count / merged_pr_count if merged_pr_count else 0.0
        closed_without_merge = sum(
            1 for p in pr_data if not p.get("merged", False) and p.get("closed", False)
        )

    snapshot = {
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "total_implementations": total,
        "first_attempt_rate": round(first_attempt_rate, 2),
        "avg_cost_usd": round(avg_cost, 2),
        "avg_duration_seconds": round(avg_duration),
        "attempt_distribution": attempt_dist,
        "human_edit_rate": round(human_edit_rate, 2),
        "human_edit_count": human_edit_count,
        "merged_pr_count": merged_pr_count,
        "closed_without_merge": closed_without_merge,
        "modules": module_stats,
    }

    if prev_snapshot is not None:
        prev_total = prev_snapshot.get("total_implementations", 0)
        snapshot["period_implementations"] = total - prev_total
        prev_date = prev_snapshot["date"]
        period_runs = [r for r in impl_runs if r.get("timestamp", "")[:10] > prev_date]
        if period_runs:
            period_first = sum(
                1 for r in period_runs if r.get("success") and r.get("attempts", 1) == 1
            )
            period_costs = [r.get("cost_usd", 0) for r in period_runs]
            snapshot["period_first_attempt_rate"] = round(period_first / len(period_runs), 2)
            snapshot["period_avg_cost_usd"] = round(sum(period_costs) / len(period_costs), 2)

    return snapshot


def fetch_pr_data(repo: str) -> list[dict]:
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--limit",
                str(_PR_LIMIT),
                "--json",
                "number,state,mergedAt,closedAt,headRefName,files,author",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []

    raw_prs = json.loads(result.stdout)

    if len(raw_prs) >= _PR_LIMIT:
        print(
            f"Warning: {_PR_LIMIT} PR limit reached, oldest PRs excluded from eval",
            file=sys.stderr,
        )

    autoloop_prs = []
    non_autoloop_merged = []
    for pr in raw_prs:
        branch = pr.get("headRefName", "")
        merged_at = pr.get("mergedAt")
        files = set(f.get("path", "") for f in (pr.get("files") or []))
        if branch.startswith("autoloop/"):
            autoloop_prs.append((pr, files))
        elif merged_at:
            non_autoloop_merged.append({"merged_at": merged_at, "files": files})

    fixup_numbers = _detect_post_merge_fixups(autoloop_prs, non_autoloop_merged)

    pr_data = []
    for pr, files in autoloop_prs:
        branch = pr.get("headRefName", "")
        merged = pr.get("mergedAt") is not None
        closed = pr.get("state") == "CLOSED"

        pr_data.append(
            {
                "number": pr.get("number"),
                "merged": merged,
                "closed": closed and not merged,
                "changed_files": list(files),
                "human_edited": pr["number"] in fixup_numbers,
                "first_attempt_success": False,
                "issue": _extract_issue_from_branch(branch),
            }
        )

    return pr_data


def _detect_post_merge_fixups(
    autoloop_prs: list[tuple[dict, set[str]]],
    non_autoloop_merged: list[dict],
) -> set[int]:
    fixup_numbers: set[int] = set()
    for pr, files in autoloop_prs:
        merged_at = pr.get("mergedAt")
        if not merged_at or not files:
            continue
        for nap in non_autoloop_merged:
            if nap["merged_at"] > merged_at and nap["files"] & files:
                fixup_numbers.add(pr["number"])
                break
    return fixup_numbers


def _extract_issue_from_branch(branch: str) -> int | None:
    parts = branch.replace("autoloop/", "", 1).split("-")
    for part in parts:
        if part.isdigit():
            return int(part)
    return None


def enrich_pr_data_with_runs(pr_data: list[dict], runs: list[dict]) -> list[dict]:
    impl_runs = [r for r in runs if r.get("type", "implement") == "implement"]
    issue_attempts: dict[int, int] = {}
    for r in impl_runs:
        issue = r.get("issue", 0)
        if issue:
            attempts = r.get("attempts", 1)
            issue_attempts[issue] = max(issue_attempts.get(issue, 0), attempts)

    for pr in pr_data:
        issue = pr.get("issue")
        if issue and issue in issue_attempts:
            pr["first_attempt_success"] = issue_attempts[issue] == 1

    return pr_data


def save_snapshot(snapshot: dict, base: Path | None = None) -> Path:
    snap_dir = (base or Path.cwd()) / "autoloop" / "eval_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / f"{snapshot['date']}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
        f.write("\n")
    return path


def load_snapshot(date: str, base: Path | None = None) -> dict | None:
    path = (base or Path.cwd()) / "autoloop" / "eval_snapshots" / f"{date}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def load_latest_snapshot(base: Path | None = None) -> dict | None:
    snap_dir = (base or Path.cwd()) / "autoloop" / "eval_snapshots"
    if not snap_dir.exists():
        return None
    files = sorted(snap_dir.glob("*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text())


def load_all_snapshots(base: Path | None = None) -> list[dict]:
    snap_dir = (base or Path.cwd()) / "autoloop" / "eval_snapshots"
    if not snap_dir.exists():
        return []
    snapshots = []
    for f in sorted(snap_dir.glob("*.json")):
        snapshots.append(json.loads(f.read_text()))
    return snapshots


def compare_snapshots(old: dict, new: dict) -> dict:
    comparison = {"old_date": old["date"], "new_date": new["date"], "changes": {}}

    for key in ("first_attempt_rate", "avg_cost_usd", "avg_duration_seconds", "human_edit_rate"):
        old_val = old.get(key, 0)
        new_val = new.get(key, 0)
        comparison["changes"][key] = {
            "old": old_val,
            "new": new_val,
            "delta": round(new_val - old_val, 4),
        }

    comparison["changes"]["total_implementations"] = {
        "old": old.get("total_implementations", 0),
        "new": new.get("total_implementations", 0),
        "delta": new.get("total_implementations", 0) - old.get("total_implementations", 0),
    }

    old_modules = old.get("modules", {})
    new_modules = new.get("modules", {})
    all_mods = sorted(set(old_modules) | set(new_modules))
    module_changes = {}
    for mod in all_mods:
        o = old_modules.get(mod, {})
        n = new_modules.get(mod, {})
        module_changes[mod] = {
            "old_rate": o.get("first_attempt_rate", 0),
            "new_rate": n.get("first_attempt_rate", 0),
            "old_impl": o.get("implementations", 0),
            "new_impl": n.get("implementations", 0),
        }
    comparison["module_changes"] = module_changes
    return comparison


def format_snapshot(snapshot: dict) -> str:
    lines = [f"Eval Snapshot ({snapshot['date']})"]
    lines.append(f"  Issues implemented:     {snapshot['total_implementations']}")

    rate = snapshot["first_attempt_rate"]
    total = snapshot["total_implementations"]
    first = round(rate * total)
    lines.append(f"  First-attempt success:  {rate:.0%} ({first}/{total})")

    lines.append(f"  Avg cost/PR:            ${snapshot['avg_cost_usd']:.2f}")

    dur = snapshot["avg_duration_seconds"]
    minutes = int(dur) // 60
    seconds = int(dur) % 60
    lines.append(f"  Avg duration:           {minutes}m {seconds:02d}s")

    dist = snapshot.get("attempt_distribution", {})
    if dist:
        parts = [f"{k}-try: {v}" for k, v in dist.items() if v > 0]
        if parts:
            lines.append(f"  Attempt distribution:   {', '.join(parts)}")

    hr = snapshot["human_edit_rate"]
    hc = snapshot.get("human_edit_count", 0)
    merged_count = snapshot.get("merged_pr_count", 0)
    lines.append(
        f"  Human edit rate:        {hr:.0%} ({hc}/{merged_count} PRs had post-merge fixups)"
    )

    cwm = snapshot.get("closed_without_merge", 0)
    if cwm:
        lines.append(f"  Closed without merge:   {cwm}")

    modules = snapshot.get("modules", {})
    if modules:
        lines.append("  Retry hotspots:")
        for mod, stats in sorted(modules.items(), key=lambda x: x[1]["first_attempt_rate"]):
            imp = stats["implementations"]
            fa = round(stats["first_attempt_rate"] * imp)
            lines.append(
                f"    {mod:<20s} {stats['first_attempt_rate']:.0%} first-attempt ({fa}/{imp})"
            )

    return "\n".join(lines)


def format_comparison(comparison: dict) -> str:
    lines = [f"Comparison: {comparison['old_date']} → {comparison['new_date']}"]

    changes = comparison["changes"]

    def _pct(key: str, label: str, is_pct: bool = False) -> str:
        c = changes[key]
        if is_pct:
            old_s = f"{c['old']:.0%}"
            new_s = f"{c['new']:.0%}"
            delta = c["delta"] * 100
            sign = "+" if delta >= 0 else ""
            return f"  {label:<24s} {old_s} → {new_s} ({sign}{delta:.0f}%)"
        else:
            return ""

    def _dollar(key: str, label: str) -> str:
        c = changes[key]
        old_val = c["old"]
        new_val = c["new"]
        if old_val != 0:
            pct = (new_val - old_val) / old_val * 100
            sign = "+" if pct >= 0 else ""
            return f"  {label:<24s} ${old_val:.2f} → ${new_val:.2f} ({sign}{pct:.0f}%)"
        else:
            delta = c["delta"]
            sign = "+" if delta >= 0 else ""
            return f"  {label:<24s} ${old_val:.2f} → ${new_val:.2f} ({sign}${abs(delta):.2f})"

    def _int(key: str, label: str) -> str:
        c = changes[key]
        delta = c["delta"]
        sign = "+" if delta >= 0 else ""
        return f"  {label:<24s} {c['old']} → {c['new']} ({sign}{delta})"

    lines.append(_pct("first_attempt_rate", "First-attempt success:", is_pct=True))
    lines.append(_dollar("avg_cost_usd", "Avg cost/PR:"))
    lines.append(_int("total_implementations", "Total implementations:"))
    lines.append(_pct("human_edit_rate", "Human edit rate:", is_pct=True))

    mc = comparison.get("module_changes", {})
    if mc:
        lines.append("  Module changes:")
        for mod, stats in sorted(mc.items()):
            old_r = stats["old_rate"]
            new_r = stats["new_rate"]
            if old_r != new_r:
                direction = "improving" if new_r > old_r else "declining"
                lines.append(f"    {mod:<20s} {old_r:.0%} → {new_r:.0%} ({direction})")

    return "\n".join(lines)


def format_trend(snapshots: list[dict]) -> str:
    if not snapshots:
        return "No snapshots found."
    if len(snapshots) == 1:
        return format_snapshot(snapshots[0])

    lines = ["Eval Trend"]
    lines.append(
        f"  {'Date':<14s} {'Impl':>6s} {'1st-attempt':>12s} {'Avg cost':>10s} {'Human edits':>12s}"
    )
    lines.append("  " + "-" * 58)
    for i, s in enumerate(snapshots):
        if "period_implementations" in s:
            impl = s["period_implementations"]
            rate = s.get("period_first_attempt_rate", s.get("first_attempt_rate", 0))
            cost = s.get("period_avg_cost_usd", s.get("avg_cost_usd", 0))
        elif i > 0:
            impl = s["total_implementations"] - snapshots[i - 1]["total_implementations"]
            rate = s.get("first_attempt_rate", 0)
            cost = s.get("avg_cost_usd", 0)
        else:
            impl = s["total_implementations"]
            rate = s.get("first_attempt_rate", 0)
            cost = s.get("avg_cost_usd", 0)
        lines.append(
            f"  {s['date']:<14s} {impl:>6d} "
            f"{rate:>11.0%} ${cost:>8.2f} "
            f"{s.get('human_edit_rate', 0):>11.0%}"
        )

    if len(snapshots) >= 2:
        first = snapshots[0]
        last = snapshots[-1]
        delta_rate = last.get("first_attempt_rate", 0) - first.get("first_attempt_rate", 0)
        delta_cost = last.get("avg_cost_usd", 0) - first.get("avg_cost_usd", 0)
        sign_r = "+" if delta_rate >= 0 else ""
        sign_c = "+" if delta_cost >= 0 else ""
        lines.append("")
        lines.append(
            f"  Overall: first-attempt {sign_r}{delta_rate:.0%}, cost {sign_c}${delta_cost:.2f}"
        )

    return "\n".join(lines)


def format_eval_md(snapshot: dict) -> str:
    lines = ["# Eval Report", ""]
    lines.append(f"**Date:** {snapshot['date']}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")

    rate = snapshot["first_attempt_rate"]
    total = snapshot["total_implementations"]
    first = round(rate * total)
    lines.append(f"| Issues implemented | {total} |")
    lines.append(f"| First-attempt success | {rate:.0%} ({first}/{total}) |")
    lines.append(f"| Avg cost/PR | ${snapshot['avg_cost_usd']:.2f} |")

    dur = snapshot["avg_duration_seconds"]
    minutes = int(dur) // 60
    seconds = int(dur) % 60
    lines.append(f"| Avg duration | {minutes}m {seconds:02d}s |")

    hr = snapshot["human_edit_rate"]
    hc = snapshot.get("human_edit_count", 0)
    merged = snapshot.get("merged_pr_count", 0)
    lines.append(f"| Human edit rate | {hr:.0%} ({hc}/{merged} PRs) |")

    cwm = snapshot.get("closed_without_merge", 0)
    if cwm:
        lines.append(f"| Closed without merge | {cwm} |")

    dist = snapshot.get("attempt_distribution", {})
    active_dist = {k: v for k, v in dist.items() if v > 0}
    if active_dist:
        lines.append("")
        lines.append("## Attempt Distribution")
        lines.append("")
        lines.append(", ".join(f"{k}-try: {v}" for k, v in active_dist.items()))

    modules = snapshot.get("modules", {})
    if modules:
        lines.append("")
        lines.append("## Module Performance")
        lines.append("")
        lines.append("| Module | First-attempt rate | Implementations |")
        lines.append("|--------|-------------------|-----------------|")
        for mod, stats in sorted(modules.items(), key=lambda x: x[1]["first_attempt_rate"]):
            imp = stats["implementations"]
            fa = round(stats["first_attempt_rate"] * imp)
            lines.append(f"| {mod} | {stats['first_attempt_rate']:.0%} ({fa}/{imp}) | {imp} |")

    lines.append("")
    return "\n".join(lines)


def is_auto_merge_ready(
    success_rate: float,
    edit_rate: float,
    merged_clean_count: int,
    edit_rate_threshold: float = 0.05,
) -> str:
    if success_rate > 0.9 and edit_rate < edit_rate_threshold and merged_clean_count >= 20:
        return "Yes"
    return "No"


def generate_eval_md(
    snapshot: dict, all_snapshots: list[dict], edit_rate_threshold: float = 0.05
) -> str:
    lines = ["# EVAL Report", ""]

    lines.append("## Overall")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    rate = snapshot.get("first_attempt_rate", 0)
    lines.append(f"| First-attempt success | {rate:.0%} |")
    cost = snapshot.get("avg_cost_usd", 0)
    lines.append(f"| Avg cost/PR | ${cost:.2f} |")
    hr = snapshot.get("human_edit_rate", 0)
    lines.append(f"| Human edit rate | {hr:.0%} |")
    lines.append("")

    modules = snapshot.get("modules", {})
    if modules:
        lines.append("## Per-Module Breakdown")
        lines.append("")
        lines.append("| Module | Success | Avg Cost | Impl | Auto-merge ready? |")
        lines.append("|--------|---------|----------|------|--------------------|")
        for mod, stats in sorted(modules.items()):
            success = stats.get("first_attempt_rate", 0)
            avg_c = stats.get("avg_cost_usd", 0)
            prs = stats.get("implementations", 0)
            edit_r = stats.get("human_edit_rate", 0)
            clean = stats.get("merged_clean_count", 0)
            auto = is_auto_merge_ready(success, edit_r, clean, edit_rate_threshold)
            lines.append(f"| {mod} | {success:.0%} | ${avg_c:.2f} | {prs} | {auto} |")
        lines.append("")

    recent = all_snapshots
    if recent:
        lines.append("## Trend")
        lines.append("")
        lines.append("| Date (UTC) | Implementations | First-attempt | Avg Cost | Human Edits |")
        lines.append("|------|----------------|---------------|----------|-------------|")
        for i, s in enumerate(recent):
            if "period_implementations" in s:
                impl = s["period_implementations"]
                rate = s.get("period_first_attempt_rate", s.get("first_attempt_rate", 0))
                cost = s.get("period_avg_cost_usd", s.get("avg_cost_usd", 0))
            elif i > 0:
                impl = s.get("total_implementations", 0) - recent[i - 1].get(
                    "total_implementations", 0
                )
                rate = s.get("first_attempt_rate", 0)
                cost = s.get("avg_cost_usd", 0)
            else:
                impl = s.get("total_implementations", 0)
                rate = s.get("first_attempt_rate", 0)
                cost = s.get("avg_cost_usd", 0)
            lines.append(
                f"| {s['date']} | {impl} "
                f"| {rate:.0%} "
                f"| ${cost:.2f} "
                f"| {s.get('human_edit_rate', 0):.0%} |"
            )
        lines.append("")

    if recent:
        dates = ", ".join(f'"{s["date"]}"' for s in recent)
        success_vals = ", ".join(str(round(s.get("first_attempt_rate", 0) * 100)) for s in recent)
        lines.append("```mermaid")
        lines.append("xychart-beta")
        lines.append('    title "First-Attempt Success Rate (UTC)"')
        lines.append(f"    x-axis [{dates}]")
        lines.append('    y-axis "Success %" 0 --> 100')
        lines.append(f"    line [{success_vals}]")
        lines.append("```")
        lines.append("")

        cost_vals = ", ".join(f"{s.get('avg_cost_usd', 0):.2f}" for s in recent)
        lines.append("```mermaid")
        lines.append("xychart-beta")
        lines.append('    title "Avg Cost/PR (UTC)"')
        lines.append(f"    x-axis [{dates}]")
        lines.append('    y-axis "Cost ($)"')
        lines.append(f"    line [{cost_vals}]")
        lines.append("```")
        lines.append("")

    if modules:
        lines.append("```mermaid")
        lines.append(
            "%%{init: {'theme': 'base', 'themeVariables': {'pie1': '#4CAF50', 'pie2': '#2196F3', 'pie3': '#FF9800', 'pie4': '#E91E63', 'pie5': '#9C27B0', 'pie6': '#00BCD4', 'pieTitleTextColor': '#aaa', 'pieLegendTextColor': '#aaa', 'pieSectionTextColor': '#fff'}}}%%"
        )
        lines.append("pie title Attempt Distribution by Module")
        for mod, stats in sorted(modules.items()):
            rate = round(stats.get("first_attempt_rate", 0) * 100)
            prs = stats.get("implementations", 0)
            lines.append(f'    "{mod} ({rate}%, {prs} impl)" : {prs}')
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def material_change(new: dict, old: dict) -> bool:
    return (
        abs(new["first_attempt_rate"] - old["first_attempt_rate"]) > 0.05
        or abs(new["human_edit_rate"] - old["human_edit_rate"]) > 0.05
        or new["total_implementations"] - old["total_implementations"] >= 5
        or set(new.get("modules", {}).keys()) != set(old.get("modules", {}).keys())
    )


def _publish_via_pr(base: Path, snapshot_path: Path, eval_path: Path, date: str) -> str:
    branch_name = f"chore/eval-{date}"
    pr_created = False
    try:
        result = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            capture_output=True,
            text=True,
            cwd=str(base),
        )
    except FileNotFoundError:
        return "Error: git not found"
    if result.returncode != 0:
        return f"Error: could not create branch {branch_name}"

    try:
        try:
            result = subprocess.run(
                ["git", "add", str(snapshot_path), str(eval_path)],
                capture_output=True,
                text=True,
                cwd=str(base),
            )
        except FileNotFoundError:
            return "Error: git not found"
        if result.returncode != 0:
            return "Error: git add failed"

        commit_msg = f"chore: update eval report ({date})"
        try:
            result = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                capture_output=True,
                text=True,
                cwd=str(base),
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
                cwd=str(base),
            )
        except FileNotFoundError:
            return "Error: git not found"
        if result.returncode != 0:
            return f"Error: git push failed\n{result.stderr}"

        pr_title = f"chore: update eval report ({date})"
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--title",
                    pr_title,
                    "--body",
                    "Automated eval report update.",
                ],
                capture_output=True,
                text=True,
                cwd=str(base),
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
                cwd=str(base),
            )
            if not pr_created:
                subprocess.run(
                    ["git", "branch", "-D", branch_name],
                    capture_output=True,
                    cwd=str(base),
                )
        except FileNotFoundError:
            pass


def main(
    compare: str | None = None,
    trend: bool = False,
    repo: str | None = None,
    base: Path | None = None,
    output: str | None = None,
    publish: bool = False,
    pr: bool = False,
):
    effective_base = base or Path.cwd()

    if publish:
        try:
            branch_result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(effective_base),
            )
        except FileNotFoundError:
            print("Error: git not found")
            return
        if branch_result.returncode != 0 or branch_result.stdout.strip() != "main":
            print("Error: --publish must be run from the main branch")
            return

        runs = load_run_history(effective_base)
        pr_data = fetch_pr_data(repo) if repo else []
        pr_data = enrich_pr_data_with_runs(pr_data, runs)
        previous = load_latest_snapshot(effective_base)
        snapshot = compute_snapshot(runs, pr_data, prev_snapshot=previous)

        path = save_snapshot(snapshot, effective_base)
        print(f"Snapshot saved to {path}")

        all_snaps = load_all_snapshots(effective_base)
        content = generate_eval_md(snapshot, all_snaps)
        eval_path = effective_base / "EVAL.md"
        eval_path.write_text(content)

        date = snapshot["date"]

        if previous and not material_change(snapshot, previous):
            print("No material change, skipping EVAL.md commit")
            return

        print("Material change detected, publishing EVAL.md")

        if pr:
            print(_publish_via_pr(effective_base, path, eval_path, date))
            return

        try:
            add_result = subprocess.run(
                ["git", "add", str(path), str(eval_path)], cwd=str(effective_base)
            )
        except FileNotFoundError:
            print("Error: git not found")
            return
        if add_result.returncode != 0:
            print("Error: git add failed")
            return

        try:
            commit_result = subprocess.run(
                ["git", "commit", "-m", f"chore: update eval report ({date})"],
                cwd=str(effective_base),
            )
        except FileNotFoundError:
            print("Error: git not found")
            return
        if commit_result.returncode != 0:
            print("Error: git commit failed")
            return

        print(f"EVAL.md committed: chore: update eval report ({date})")

        try:
            push_result = subprocess.run(
                ["git", "push"],
                capture_output=True,
                text=True,
                cwd=str(effective_base),
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
        return

    if trend:
        snapshots = load_all_snapshots(effective_base)
        if output == "json":
            print(json.dumps(snapshots))
            return
        print(format_trend(snapshots))
        return

    if compare == "latest":
        runs = load_run_history(effective_base)
        pr_data = fetch_pr_data(repo) if repo else []
        pr_data = enrich_pr_data_with_runs(pr_data, runs)
        previous = load_latest_snapshot(effective_base)
        current = compute_snapshot(runs, pr_data, prev_snapshot=previous)
        if previous is None:
            if output == "json":
                print(json.dumps(current))
            else:
                print("No previous snapshot to compare against.")
                print(format_snapshot(current))
            path = save_snapshot(current, effective_base)
            if output != "json":
                print(f"\nSnapshot saved to {path}")
            return

        comparison = compare_snapshots(previous, current)
        if output == "json":
            print(json.dumps(comparison))
        else:
            print(format_comparison(comparison))

        path = save_snapshot(current, effective_base)
        if output != "json":
            print(f"\nSnapshot saved to {path}")
        return

    runs = load_run_history(effective_base)
    pr_data = fetch_pr_data(repo) if repo else []
    pr_data = enrich_pr_data_with_runs(pr_data, runs)
    previous = load_latest_snapshot(effective_base)
    snapshot = compute_snapshot(runs, pr_data, prev_snapshot=previous)

    if output == "json":
        print(json.dumps(snapshot))
    else:
        print(format_snapshot(snapshot))
    path = save_snapshot(snapshot, effective_base)
    if output != "json":
        print(f"\nSnapshot saved to {path}")
