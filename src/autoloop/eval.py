"""Eval command: deterministic performance tracking from run history + PR data.

Reads run_history.jsonl and GitHub PR data to compute builder performance
metrics. No LLM calls. Purely deterministic.
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from autoloop.config import RepoContext

_PR_LIMIT = 500

# Snapshots before this date counted triage rows as implement runs (#400), so their
# success and cost values are not comparable with later snapshots.
BASELINE_BREAK_DATE = "2026-10-26"

# A later non-autoloop PR counts as a fixup only if it merged within this many days.
DEFAULT_FIXUP_WINDOW_DAYS = 7

# Bookkeeping files whose overlap never makes a later PR a fixup.
FIXUP_ALWAYS_IGNORED = [
    "pyproject.toml",
    "uv.lock",
    "CHANGELOG*",
    "EVAL.md",
    "JEV.md",
    "autoloop/eval_snapshots/**",
]

# Release and eval-report PRs touch many files but are never fixups.
FIXUP_EXCLUDED_BRANCHES = ["chore/eval-*", "chore/release-*"]
FIXUP_EXCLUDED_TITLE_PREFIXES = [
    "chore: update changelog and version",
    "chore: update eval report",
]

DEFAULT_MIN_N = 20

# A report whose newest run_history row is older than this (relative to the report date)
# is flagged stale.
STALE_AFTER_HOURS = 48

# Marker for a rate whose numerator falls outside [0, denominator].
INVALID_RATE = "invalid"


def load_run_history(ctx: RepoContext) -> list[dict]:
    log_file = ctx.data_dir / "run_history.jsonl"
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


# Metric definitions rendered in the EVAL.md "Definitions" section. They describe the code
# in compute_snapshot (lifetime) and _compute_period_stats (period); change them together.
_IMPLEMENT_RUNS = (
    '`run_history` rows with type "implement" and a non-zero issue '
    "(triage, review and issue-0 rows are excluded)"
)
METRIC_DEFINITIONS = [
    {
        "metric": "First-attempt success",
        "numerator": "implement runs that succeeded with attempts == 1",
        "denominator": "implement runs with an outcome (pending runs excluded)",
        "counts": _IMPLEMENT_RUNS,
        "window": (
            "Overall: lifetime. Trend: change since the previous snapshot "
            "(snapshots without outcome counts use the attempt distribution)"
        ),
    },
    {
        "metric": "Human edit rate",
        "numerator": (
            "merged PRs with a non-autoloop PR merged within the fixup window that touches "
            "the same files (bookkeeping files, release and eval-report PRs are ignored)"
        ),
        "denominator": "merged PRs (open PRs are pending; closed unmerged PRs excluded)",
        "counts": f"PRs from `autoloop/` branches, up to the {_PR_LIMIT} most recent",
        "window": (
            "Overall: lifetime. Trend: new edits / new merges since the previous snapshot; "
            "invalid when edits exceed merges (older PRs flagged late)"
        ),
    },
    {
        "metric": "Avg cost/run",
        "numerator": "sum of `cost_usd` over implement runs",
        "denominator": "implement runs, pending included (cost is known when a run ends)",
        "counts": _IMPLEMENT_RUNS,
        "window": "Overall: lifetime. Trend: change in total cost / change in implement runs",
    },
]
PENDING_DEFINITION = (
    "Pending: a successful implement run whose PR is still open, or an open PR. Pending items "
    "are left out of outcome-dependent denominators and shown as a separate pending count. "
    "Any rate whose numerator exceeds its denominator renders as invalid."
)


def _open_prs(pr_data: list[dict] | None) -> list[dict]:
    return [p for p in pr_data or [] if not p.get("merged", False) and not p.get("closed", False)]


def _pending_run_filter(pr_data: list[dict] | None):
    """Return a predicate marking implement runs whose PR outcome is still pending."""
    open_prs = _open_prs(pr_data)
    open_numbers = {p.get("number") for p in open_prs if p.get("number") is not None}
    open_issues = {p.get("issue") for p in open_prs if p.get("issue")}

    def is_pending(run: dict) -> bool:
        if not run.get("success"):
            return False
        pr_number = run.get("pr_number")
        if pr_number is not None:
            return pr_number in open_numbers
        return run.get("issue") in open_issues

    return is_pending


def _parse_timestamp(value: str) -> datetime | None:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def latest_run_timestamp(runs: list[dict]) -> str | None:
    """Newest `timestamp` across all run_history rows, as ISO-8601 UTC, or None."""
    stamps = [ts for r in runs if (ts := _parse_timestamp(r.get("timestamp")))]
    if not stamps:
        return None
    return max(stamps).astimezone(UTC).isoformat()


def compute_snapshot(
    runs: list[dict],
    pr_data: list[dict] | None = None,
    date: str | None = None,
    module_prefixes: list[str] | None = None,
    fixup_window_days: int | None = None,
) -> dict:
    impl_runs = [
        r for r in runs if r.get("type", "implement") == "implement" and r.get("issue", 0) != 0
    ]

    total = len(impl_runs)
    is_pending = _pending_run_filter(pr_data)
    resolved_runs = [r for r in impl_runs if not is_pending(r)]
    resolved = len(resolved_runs)
    first_attempt = sum(1 for r in resolved_runs if r.get("success") and r.get("attempts", 1) == 1)
    first_attempt_rate = first_attempt / resolved if resolved else 0.0

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
                    "implement_run_count": 0,
                    "pending_count": 0,
                    "first_attempt_successes": 0,
                    "total_cost": 0.0,
                    "merged_count": 0,
                    "human_edit_count": 0,
                }
            modules[mod]["implementations"] += 1
            # Success and cost are only known for PRs whose issue has an implement run;
            # PRs opened by hand or before run history began are left out of both.
            # Open PRs count toward cost but are pending for success.
            issue = pr.get("issue")
            if issue and issue in issue_cost:
                modules[mod]["implement_run_count"] += 1
                modules[mod]["total_cost"] += issue_cost[issue]
                if not pr.get("merged", False) and not pr.get("closed", False):
                    modules[mod]["pending_count"] += 1
                elif pr.get("first_attempt_success", False):
                    modules[mod]["first_attempt_successes"] += 1
            if pr.get("merged", False):
                modules[mod]["merged_count"] += 1
                if pr.get("human_edited", False):
                    modules[mod]["human_edit_count"] += 1

    module_stats = {}
    for mod, stats in sorted(modules.items()):
        imp = stats["implementations"]
        n_runs = stats["implement_run_count"]
        n_resolved = n_runs - stats["pending_count"]
        fa = stats["first_attempt_successes"]
        merged = stats["merged_count"]
        module_stats[mod] = {
            "implementations": imp,
            "implement_run_count": n_runs,
            "resolved_run_count": n_resolved,
            "pending_count": stats["pending_count"],
            "first_attempt_rate": round(fa / n_resolved, 2) if n_resolved else 0.0,
            "avg_cost_usd": round(stats["total_cost"] / n_runs, 2) if n_runs else 0.0,
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

    total_prs = (
        sum(1 for p in pr_data if p.get("merged", False) or not p.get("closed", False))
        if pr_data
        else 0
    )

    snapshot = {
        "date": date or datetime.now().strftime("%Y-%m-%d"),
        "total_prs": total_prs,
        # Deprecated alias of total_prs (falls back to the implement-run count when there
        # is no PR data). Kept for one release so older readers of snapshots keep working.
        "total_implementations": total_prs if pr_data else total,
        "implement_run_count": total,
        "resolved_run_count": resolved,
        "pending_run_count": total - resolved,
        "first_attempt_count": first_attempt,
        "first_attempt_rate": round(first_attempt_rate, 2),
        "avg_cost_usd": round(avg_cost, 2),
        "avg_duration_seconds": round(avg_duration),
        "attempt_distribution": attempt_dist,
        "human_edit_rate": round(human_edit_rate, 2),
        "human_edit_count": human_edit_count,
        "merged_pr_count": merged_pr_count,
        "pending_pr_count": len(_open_prs(pr_data)),
        "closed_without_merge": closed_without_merge,
        "data_as_of": latest_run_timestamp(runs),
        "history_rows": len(runs),
        "modules": module_stats,
    }
    # The fixup rule pr_data was built with; a change breaks the edit-rate trend.
    if fixup_window_days is not None:
        snapshot["fixup_window_days"] = fixup_window_days
    return snapshot


def snapshot_total_prs(snapshot: dict) -> int:
    """PR count (merged + open) of a snapshot, reading the pre-#418 key as a fallback."""
    if "total_prs" in snapshot:
        return snapshot["total_prs"]
    return snapshot.get("total_implementations", 0)


def snapshot_implement_runs(snapshot: dict) -> int:
    """Number of implement runs behind the run-history averages of a snapshot."""
    if "implement_run_count" in snapshot:
        return snapshot["implement_run_count"]
    dist = snapshot.get("attempt_distribution") or {}
    return sum(dist.get(k, 0) for k in ("1", "2", "3+"))


def module_implement_runs(stats: dict) -> int:
    """Implement runs behind a module's success and cost; pre-#432 snapshots used its PRs."""
    if "implement_run_count" in stats:
        return stats["implement_run_count"]
    return stats.get("implementations", 0)


def snapshot_resolved_runs(snapshot: dict) -> int:
    """Implement runs behind first-attempt success; pre-#452 snapshots had no pending split."""
    if "resolved_run_count" in snapshot:
        return snapshot["resolved_run_count"]
    return snapshot_implement_runs(snapshot)


def module_resolved_runs(stats: dict) -> int:
    """Implement runs behind a module's first-attempt success, pending runs excluded."""
    if "resolved_run_count" in stats:
        return stats["resolved_run_count"]
    return module_implement_runs(stats)


def _warn_invalid(label: str, detail: str) -> None:
    print(f"Warning: {label} {detail}, rendered as {INVALID_RATE}", file=sys.stderr)


def bounded_rate(numerator: float, denominator: float, label: str) -> float | str | None:
    """numerator / denominator rounded to 2 places.

    Returns None when there is no denominator, and INVALID_RATE (with a warning) when the
    numerator falls outside [0, denominator], so a rate can never exceed 100%.
    """
    if denominator <= 0:
        return None
    if numerator < 0 or numerator > denominator:
        _warn_invalid(label, f"numerator {numerator} outside [0, {denominator}]")
        return INVALID_RATE
    return round(numerator / denominator, 2)


def is_valid_rate(rate) -> bool:
    return isinstance(rate, int | float) and not isinstance(rate, bool) and 0 <= rate <= 1


def format_pct(rate, label: str = "rate") -> str:
    """Render a rate as a percentage: "—" when missing, INVALID_RATE outside [0, 1]."""
    if rate is None:
        return "—"
    if not is_valid_rate(rate):
        if rate != INVALID_RATE:
            _warn_invalid(label, f"value {rate!r} outside [0, 1]")
        return INVALID_RATE
    return f"{rate:.0%}"


def format_rate_with_n(rate: float, n: int, population: str, min_n: int = DEFAULT_MIN_N) -> str:
    """Render a rate with its sample size, or "insufficient data" when n is below min_n."""
    if n < min_n:
        return f"insufficient data (n={n} {population})"
    return f"{format_pct(rate, population)} (n={n} {population})"


def data_freshness(snapshot: dict) -> tuple[str, bool]:
    """Return the "data as of" line for a snapshot and whether its source data is stale.

    Stale means the newest run_history row is more than STALE_AFTER_HOURS older than the
    start (00:00 UTC) of the report date, or that no row carries a timestamp.
    """
    as_of = snapshot.get("data_as_of")
    rows = snapshot.get("history_rows", 0)
    line = f"data as of {as_of or 'unknown'}, {rows} history rows"
    as_of_ts = _parse_timestamp(as_of) if as_of else None
    if as_of_ts is None:
        return line, True
    report_ts = datetime.strptime(snapshot["date"], "%Y-%m-%d").replace(tzinfo=UTC)
    return line, report_ts - as_of_ts > timedelta(hours=STALE_AFTER_HOURS)


def _freshness_lines(snapshot: dict) -> list[str]:
    line, stale = data_freshness(snapshot)
    lines = [f"Report date {snapshot['date']}: {line}."]
    if stale:
        lines.append("")
        lines.append(
            f"**Stale data:** the newest run_history row is more than {STALE_AFTER_HOURS} h "
            "older than the report date (or has no timestamp). Check the history source "
            "before using these numbers."
        )
    lines.append("")
    return lines


def _definitions_lines() -> list[str]:
    lines = ["## Definitions", ""]
    lines.append("| Metric | Numerator | Denominator | Counts | Window |")
    lines.append("|--------|-----------|-------------|--------|--------|")
    for d in METRIC_DEFINITIONS:
        lines.append(
            f"| {d['metric']} | {d['numerator']} | {d['denominator']} | {d['counts']} "
            f"| {d['window']} |"
        )
    lines.append("")
    lines.append(PENDING_DEFINITION)
    lines.append("")
    return lines


def format_cost_with_n(cost: float, n: int, population: str, min_n: int = DEFAULT_MIN_N) -> str:
    """Render an average cost with its sample size, or "insufficient data" below min_n."""
    if n < min_n:
        return f"insufficient data (n={n} {population})"
    return f"${cost:.2f} (n={n} {population})"


def _crosses_baseline_break(snapshot: dict, prev: dict | None) -> bool:
    return prev is not None and prev["date"] < BASELINE_BREAK_DATE <= snapshot["date"]


def _crosses_fixup_rule_break(snapshot: dict, prev: dict | None) -> bool:
    """True when the fixup window changed (or was first recorded) since `prev`."""
    return prev is not None and prev.get("fixup_window_days") != snapshot.get("fixup_window_days")


def fetch_pr_data(
    repo: str,
    fixup_window_days: int = DEFAULT_FIXUP_WINDOW_DAYS,
    fixup_ignore: list[str] | None = None,
) -> list[dict]:
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
                "number,title,state,mergedAt,closedAt,headRefName,files,author",
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
            non_autoloop_merged.append(
                {
                    "merged_at": merged_at,
                    "files": files,
                    "branch": branch,
                    "title": pr.get("title", ""),
                }
            )

    fixup_numbers = _detect_post_merge_fixups(
        autoloop_prs, non_autoloop_merged, fixup_window_days, fixup_ignore
    )

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


def _is_fixup_ignored(path: str, patterns: list[str]) -> bool:
    return any(PurePosixPath(path).full_match(p) for p in patterns)


def _is_excluded_fixup_pr(nap: dict) -> bool:
    branch = nap.get("branch") or ""
    title = nap.get("title") or ""
    return any(fnmatch.fnmatchcase(branch, p) for p in FIXUP_EXCLUDED_BRANCHES) or any(
        title.startswith(p) for p in FIXUP_EXCLUDED_TITLE_PREFIXES
    )


def _detect_post_merge_fixups(
    autoloop_prs: list[tuple[dict, set[str]]],
    non_autoloop_merged: list[dict],
    window_days: int = DEFAULT_FIXUP_WINDOW_DAYS,
    ignore: list[str] | None = None,
) -> set[int]:
    """Return autoloop PR numbers edited by a later non-autoloop PR.

    A later PR counts only if it merged within `window_days` of the autoloop PR, shares
    a file outside FIXUP_ALWAYS_IGNORED and `ignore`, and is not a release or eval PR.
    """
    patterns = FIXUP_ALWAYS_IGNORED + list(ignore or [])
    window = timedelta(days=window_days)
    candidates = []
    for nap in non_autoloop_merged:
        nap_ts = _parse_timestamp(nap["merged_at"])
        if nap_ts is None or _is_excluded_fixup_pr(nap):
            continue
        files = {f for f in nap["files"] if not _is_fixup_ignored(f, patterns)}
        if files:
            candidates.append((nap_ts, files))

    fixup_numbers: set[int] = set()
    for pr, files in autoloop_prs:
        merged_ts = _parse_timestamp(pr.get("mergedAt"))
        if merged_ts is None or not files:
            continue
        for nap_ts, nap_files in candidates:
            if merged_ts < nap_ts <= merged_ts + window and nap_files & files:
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
    impl_runs = [
        r for r in runs if r.get("type", "implement") == "implement" and r.get("issue", 0) != 0
    ]
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


def _published_snapshot_dir(ctx: RepoContext) -> Path:
    return ctx.repo_dir / "autoloop" / "eval_snapshots"


def _daily_snapshot_dir(ctx: RepoContext) -> Path:
    return ctx.data_dir / "eval_snapshots"


def save_snapshot(snapshot: dict, ctx: RepoContext, *, published: bool = False) -> Path:
    """Write a snapshot to the data dir, or to the repo when it is being published."""
    snap_dir = _published_snapshot_dir(ctx) if published else _daily_snapshot_dir(ctx)
    snap_dir.mkdir(parents=True, exist_ok=True)
    path = snap_dir / f"{snapshot['date']}.json"
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
        f.write("\n")
    return path


def load_snapshot(date: str, ctx: RepoContext) -> dict | None:
    for snap_dir in (_published_snapshot_dir(ctx), _daily_snapshot_dir(ctx)):
        path = snap_dir / f"{date}.json"
        if path.exists():
            return json.loads(path.read_text())
    return None


def load_latest_snapshot(ctx: RepoContext) -> dict | None:
    """Return the last published snapshot — the baseline for material_change."""
    snap_dir = _published_snapshot_dir(ctx)
    if not snap_dir.exists():
        return None
    files = sorted(snap_dir.glob("*.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text())


def load_all_snapshots(ctx: RepoContext) -> list[dict]:
    """Return daily and published snapshots by date; the published copy wins on a tie."""
    by_date: dict[str, Path] = {}
    for snap_dir in (_daily_snapshot_dir(ctx), _published_snapshot_dir(ctx)):
        if snap_dir.exists():
            for f in snap_dir.glob("*.json"):
                by_date[f.stem] = f
    return [json.loads(by_date[d].read_text()) for d in sorted(by_date)]


def migrate_unpublished_snapshots(ctx: RepoContext) -> list[Path]:
    """Move untracked or uncommitted repo snapshots into the data dir.

    Untracked and newly staged files are moved out of the repo. Modified tracked files
    are copied to the data dir and restored to their committed version. Returns the
    data-dir paths written.
    """
    rel_dir = _published_snapshot_dir(ctx).relative_to(ctx.repo_dir)
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--", str(rel_dir)],
            capture_output=True,
            text=True,
            cwd=str(ctx.repo_dir),
        )
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []

    migrated = []
    daily_dir = _daily_snapshot_dir(ctx)
    for line in (result.stdout or "").splitlines():
        code, rel = line[:2], line[3:].strip()
        src = ctx.repo_dir / rel
        if not rel.endswith(".json") or not src.is_file():
            continue
        if code == "??":
            restore_cmd, remove = None, True
        elif code[0] == "A":
            restore_cmd, remove = ["git", "rm", "--cached", "-q", "--", rel], True
        elif "M" in code:
            restore_cmd, remove = ["git", "checkout", "HEAD", "--", rel], False
        else:
            continue

        daily_dir.mkdir(parents=True, exist_ok=True)
        dst = daily_dir / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
        if restore_cmd:
            try:
                restore = subprocess.run(
                    restore_cmd, capture_output=True, text=True, cwd=str(ctx.repo_dir)
                )
            except FileNotFoundError:
                return migrated
            if restore.returncode != 0:
                continue
        if remove:
            src.unlink()
        migrated.append(dst)
    return migrated


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

    old_prs = snapshot_total_prs(old)
    new_prs = snapshot_total_prs(new)
    comparison["changes"]["total_prs"] = {
        "old": old_prs,
        "new": new_prs,
        "delta": new_prs - old_prs,
    }
    # Deprecated alias of total_prs, kept for one release.
    comparison["changes"]["total_implementations"] = dict(comparison["changes"]["total_prs"])

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
    lines.append(f"  Total PRs:              {snapshot_total_prs(snapshot)}")

    rate = snapshot["first_attempt_rate"]
    runs = snapshot_resolved_runs(snapshot)
    first = round(rate * runs)
    pending = snapshot.get("pending_run_count", 0)
    lines.append(
        f"  First-attempt success:  {format_pct(rate)} ({first}/{runs} implement runs), "
        f"pending: {pending}"
    )

    lines.append(f"  Avg cost/run:           ${snapshot['avg_cost_usd']:.2f}")

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
    pending_prs = snapshot.get("pending_pr_count", 0)
    lines.append(
        f"  Human edit rate:        {format_pct(hr)} ({hc}/{merged_count} PRs had post-merge "
        f"fixups), pending: {pending_prs}"
    )

    cwm = snapshot.get("closed_without_merge", 0)
    if cwm:
        lines.append(f"  Closed without merge:   {cwm}")

    lines.append(f"  Freshness:              {data_freshness(snapshot)[0]}")

    modules = snapshot.get("modules", {})
    if modules:
        lines.append("  Retry hotspots:")
        for mod, stats in sorted(modules.items(), key=lambda x: x[1]["first_attempt_rate"]):
            n_runs = module_resolved_runs(stats)
            fa = round(stats["first_attempt_rate"] * n_runs)
            lines.append(
                f"    {mod:<20s} {stats['first_attempt_rate']:.0%} first-attempt ({fa}/{n_runs} runs)"
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
    lines.append(_dollar("avg_cost_usd", "Avg cost/run:"))
    lines.append(_int("total_prs", "Total PRs:"))
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


def _sample_for_chart(snapshots: list[dict]) -> list[dict]:
    """Sample snapshots for chart display — one per ISO week, last snapshot wins."""
    if not snapshots:
        return []
    weeks: dict[tuple[int, int], dict] = {}
    for s in snapshots:
        dt = datetime.strptime(s["date"], "%Y-%m-%d")
        iso_year, iso_week, _ = dt.isocalendar()
        key = (iso_year, iso_week)
        weeks[key] = s
    return [weeks[k] for k in sorted(weeks)]


def _md_label(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}"


def _period_impl(snapshot: dict, prev: dict | None) -> int:
    if prev is None:
        return snapshot_total_prs(snapshot)
    return max(0, snapshot_total_prs(snapshot) - snapshot_total_prs(prev))


def _compute_period_stats(snapshot: dict, prev: dict | None) -> dict:
    if prev is None:
        dist = snapshot.get("attempt_distribution")
        n = sum(dist.get(k, 0) for k in ("1", "2", "3+")) if dist else 0
        return {
            "period_first_attempt_rate": snapshot.get("first_attempt_rate"),
            "period_avg_cost_usd": snapshot.get("avg_cost_usd"),
            "period_human_edit_rate": snapshot.get("human_edit_rate"),
            "period_n": n,
        }

    curr_dist = snapshot.get("attempt_distribution")
    prev_dist = prev.get("attempt_distribution")
    if not curr_dist or not prev_dist:
        return {}

    curr_runs = sum(curr_dist.get(k, 0) for k in ("1", "2", "3+"))
    prev_runs = sum(prev_dist.get(k, 0) for k in ("1", "2", "3+"))
    period_runs = curr_runs - prev_runs

    if period_runs <= 0:
        return {
            "period_first_attempt_rate": None,
            "period_avg_cost_usd": None,
            "period_human_edit_rate": None,
            "period_n": 0,
        }

    # Rates go through bounded_rate: a numerator outside [0, denominator] is invalid (#452).
    # These formulas are described by METRIC_DEFINITIONS; keep the two in step.
    if all(k in s for s in (snapshot, prev) for k in ("resolved_run_count", "first_attempt_count")):
        period_first = snapshot["first_attempt_count"] - prev["first_attempt_count"]
        period_resolved = snapshot["resolved_run_count"] - prev["resolved_run_count"]
    else:
        period_first = max(0, curr_dist.get("1", 0) - prev_dist.get("1", 0))
        period_resolved = period_runs
    period_first_attempt_rate = bounded_rate(
        period_first, period_resolved, f"{snapshot.get('date')} period first-attempt success"
    )

    curr_total_cost = snapshot.get("avg_cost_usd", 0) * curr_runs
    prev_total_cost = prev.get("avg_cost_usd", 0) * prev_runs
    period_avg_cost = round(max(0, curr_total_cost - prev_total_cost) / period_runs, 2)

    if _crosses_fixup_rule_break(snapshot, prev):
        # Edit counts under different fixup rules do not diff; restart from lifetime.
        period_human_edit_rate = snapshot.get("human_edit_rate")
    else:
        period_edits = snapshot.get("human_edit_count", 0) - prev.get("human_edit_count", 0)
        period_merged = snapshot.get("merged_pr_count", 0) - prev.get("merged_pr_count", 0)
        period_human_edit_rate = bounded_rate(
            period_edits, period_merged, f"{snapshot.get('date')} period human edit rate"
        )

    return {
        "period_first_attempt_rate": period_first_attempt_rate,
        "period_avg_cost_usd": period_avg_cost,
        "period_human_edit_rate": period_human_edit_rate,
        "period_n": period_runs,
    }


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
        prev = snapshots[i - 1] if i > 0 else None
        impl = _period_impl(s, prev)
        pstats = _compute_period_stats(s, prev) or {
            k: s.get(k)
            for k in ("period_first_attempt_rate", "period_avg_cost_usd", "period_human_edit_rate")
        }

        prate = pstats.get("period_first_attempt_rate")
        rate_str = f"{format_pct(prate, s['date'] + ' first-attempt'):>11s}"

        pcost = pstats.get("period_avg_cost_usd")
        cost_str = f"{'—':>9s}" if pcost is None else f"${pcost:>8.2f}"

        phr = pstats.get("period_human_edit_rate")
        hr_str = f"{format_pct(phr, s['date'] + ' human edit rate'):>11s}"

        lines.append(f"  {s['date']:<14s} {impl:>6d} {rate_str} {cost_str} {hr_str}")

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
    lines.extend(_freshness_lines(snapshot))
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")

    rate = snapshot["first_attempt_rate"]
    runs = snapshot_resolved_runs(snapshot)
    first = round(rate * runs)
    pending_runs = snapshot.get("pending_run_count", 0)
    lines.append(f"| Total PRs | {snapshot_total_prs(snapshot)} |")
    lines.append(
        f"| First-attempt success | {format_pct(rate)} ({first}/{runs} implement runs), "
        f"pending: {pending_runs} |"
    )
    lines.append(f"| Avg cost/run | ${snapshot['avg_cost_usd']:.2f} |")

    dur = snapshot["avg_duration_seconds"]
    minutes = int(dur) // 60
    seconds = int(dur) % 60
    lines.append(f"| Avg duration | {minutes}m {seconds:02d}s |")

    hr = snapshot["human_edit_rate"]
    hc = snapshot.get("human_edit_count", 0)
    merged = snapshot.get("merged_pr_count", 0)
    pending_prs = snapshot.get("pending_pr_count", 0)
    lines.append(
        f"| Human edit rate | {format_pct(hr)} ({hc}/{merged} PRs), pending: {pending_prs} |"
    )

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
            n_runs = module_resolved_runs(stats)
            fa = round(stats["first_attempt_rate"] * n_runs)
            lines.append(
                f"| {mod} | {format_pct(stats['first_attempt_rate'])} ({fa}/{n_runs} runs) | {imp} |"
            )

    lines.append("")
    lines.extend(_definitions_lines())
    return "\n".join(lines)


def is_auto_merge_ready(
    success_rate: float,
    edit_rate: float,
    merged_clean_count: int,
    edit_rate_threshold: float = 0.05,
    success_threshold: float = 0.90,
    volume_floor: int = 10,
) -> str:
    if (
        success_rate > success_threshold
        and edit_rate < edit_rate_threshold
        and merged_clean_count >= volume_floor
    ):
        return "Yes"
    return "No"


def generate_eval_md(
    snapshot: dict,
    all_snapshots: list[dict],
    edit_rate_threshold: float = 0.05,
    success_threshold: float = 0.90,
    volume_floor: int = 10,
    promotion_level: str = "module",
    annotations: list[str] | None = None,
    min_n: int = DEFAULT_MIN_N,
) -> str:
    lines = ["# EVAL Report", ""]
    lines.extend(_freshness_lines(snapshot))

    lines.append("## Overall")
    lines.append("")
    lines.append("| Metric | Value | Pending |")
    lines.append("|--------|-------|---------|")
    lines.append(f"| Total PRs | {snapshot_total_prs(snapshot)} | — |")
    runs = snapshot_implement_runs(snapshot)
    resolved = snapshot_resolved_runs(snapshot)
    pending_runs = snapshot.get("pending_run_count", 0)
    rate = snapshot.get("first_attempt_rate", 0)
    rate_s = format_rate_with_n(rate, resolved, "implement runs", min_n)
    lines.append(f"| First-attempt success | {rate_s} | pending: {pending_runs} |")
    cost = snapshot.get("avg_cost_usd", 0)
    lines.append(f"| Avg cost/run | ${cost:.2f} (n={runs} implement runs) | included |")
    hr = snapshot.get("human_edit_rate", 0)
    merged = snapshot.get("merged_pr_count", 0)
    pending_prs = snapshot.get("pending_pr_count", 0)
    population = "merged PRs"
    window = snapshot.get("fixup_window_days")
    if window is not None:
        population += f"; edited by a human PR within {window} days"
    hr_s = format_rate_with_n(hr, merged, population, min_n)
    lines.append(f"| Human edit rate | {hr_s} | pending: {pending_prs} |")
    lines.append("")
    lines.extend(_definitions_lines())

    gate_kwargs = {
        "edit_rate_threshold": edit_rate_threshold,
        "success_threshold": success_threshold,
        "volume_floor": volume_floor,
    }

    modules = snapshot.get("modules", {})
    if modules:
        if promotion_level == "repo":
            repo_success_rate = snapshot.get("first_attempt_rate", 0)
            repo_edit_rate = snapshot.get("human_edit_rate", 0)
            repo_merged_clean = snapshot.get("merged_pr_count", 0) - snapshot.get(
                "human_edit_count", 0
            )
            repo_auto = is_auto_merge_ready(
                repo_success_rate, repo_edit_rate, repo_merged_clean, **gate_kwargs
            )

            lines.append("## Auto-Merge Gate (repo-level)")
            lines.append("")
            lines.append("| Metric | Value | Threshold |")
            lines.append("|--------|-------|-----------|")
            success_s = format_pct(repo_success_rate, "repo success rate")
            edit_s = format_pct(repo_edit_rate, "repo edit rate")
            lines.append(f"| Success rate | {success_s} | >{success_threshold:.0%} |")
            lines.append(f"| Edit rate | {edit_s} | <{edit_rate_threshold:.0%} |")
            lines.append(f"| Clean merges | {repo_merged_clean} | ≥{volume_floor} |")
            lines.append(f"| **Ready?** | **{repo_auto}** | |")
            lines.append("")

        lines.append("## Per-Module Breakdown")
        lines.append("")
        lines.append("| Module | Success | Avg Cost | Impl | Auto-merge ready? |")
        lines.append("|--------|---------|----------|------|--------------------|")
        for mod, stats in sorted(modules.items()):
            success = stats.get("first_attempt_rate", 0)
            avg_c = stats.get("avg_cost_usd", 0)
            prs = stats.get("implementations", 0)
            n_runs = module_implement_runs(stats)
            edit_r = stats.get("human_edit_rate", 0)
            clean = stats.get("merged_clean_count", 0)
            auto = is_auto_merge_ready(success, edit_r, clean, **gate_kwargs)
            success_s = format_rate_with_n(success, module_resolved_runs(stats), "runs", min_n)
            cost_s = format_cost_with_n(avg_c, n_runs, "runs", min_n)
            lines.append(f"| {mod} | {success_s} | {cost_s} | {prs} | {auto} |")
        lines.append("")

    recent = all_snapshots
    if recent:
        lines.append("## Trend")
        lines.append("")
        lines.append("| Date (UTC) | New PRs | First-attempt | Avg Cost | Human Edits |")
        lines.append("|------|---------|---------------|----------|-------------|")
        has_break = False
        fixup_breaks: list[dict] = []
        for i, s in enumerate(recent):
            prev = recent[i - 1] if i > 0 else None
            impl = _period_impl(s, prev)
            is_break = _crosses_baseline_break(s, prev)
            has_break = has_break or is_break
            edit_break = _crosses_fixup_rule_break(s, prev)
            if edit_break:
                fixup_breaks.append(s)
            pstats = _compute_period_stats(s, None if is_break else prev) or {
                k: s.get(k)
                for k in (
                    "period_first_attempt_rate",
                    "period_avg_cost_usd",
                    "period_human_edit_rate",
                )
            }

            prate = pstats.get("period_first_attempt_rate")
            rate_str = format_pct(prate, f"{s['date']} first-attempt")

            pcost = pstats.get("period_avg_cost_usd")
            cost_str = "—" if pcost is None else f"${pcost:.2f}"

            phr = pstats.get("period_human_edit_rate")
            hr_str = format_pct(phr, f"{s['date']} human edit rate")

            is_marked = is_break or edit_break
            date_str = f"{s['date']} (baseline break)" if is_marked else s["date"]
            lines.append(f"| {date_str} | {impl} | {rate_str} | {cost_str} | {hr_str} |")
        if has_break:
            lines.append("")
            lines.append(
                f"> Baseline break at {BASELINE_BREAK_DATE}: earlier snapshots counted triage "
                "runs as implementations (#400), so their first-attempt and cost values are "
                "not comparable with later rows."
            )
        for s in fixup_breaks:
            window = s.get("fixup_window_days")
            rule = f"a {window}-day window" if window is not None else "no recorded window"
            lines.append("")
            lines.append(
                f"> Baseline break at {s['date']}: human edits are counted under {rule} from "
                "this row on, so earlier human edit rates are not comparable with later rows."
            )
        lines.append("")

    if recent:
        chart_snaps = _sample_for_chart(recent)
        if chart_snaps:
            chart_points: list[dict] = []
            for i, s in enumerate(chart_snaps):
                prev = chart_snaps[i - 1] if i > 0 else None
                if _crosses_baseline_break(s, prev):
                    prev = None
                pstats = _compute_period_stats(s, prev)
                if not pstats:
                    continue
                rate = pstats.get("period_first_attempt_rate")
                if not is_valid_rate(rate):
                    continue
                chart_points.append(
                    {
                        "date": s["date"],
                        "rate": rate,
                        "cost": pstats.get("period_avg_cost_usd") or 0.0,
                        "n": pstats.get("period_n", 0),
                    }
                )

            if chart_points:
                year = chart_points[-1]["date"][:4]
                dates = ", ".join(f'"{_md_label(p["date"])} (n={p["n"]})"' for p in chart_points)
                success_vals = ", ".join(str(round(p["rate"] * 100)) for p in chart_points)

                lines.append("```mermaid")
                lines.append("xychart-beta horizontal")
                lines.append(f'    title "Weekly First-Attempt Success Rate ({year})"')
                lines.append(f"    x-axis [{dates}]")
                lines.append('    y-axis "Success %" 0 --> 100')
                lines.append(f"    bar [{success_vals}]")
                lines.append("```")
                if annotations:
                    lines.append("")
                    lines.append("> " + " · ".join(annotations))
                lines.append("")

                cost_vals = ", ".join(f"{p['cost']:.2f}" for p in chart_points)
                cost_nums = [p["cost"] for p in chart_points]
                y_max = max(1, int(max(cost_nums, default=0)) + 1)
                lines.append("```mermaid")
                lines.append("xychart-beta horizontal")
                lines.append(f'    title "Weekly Avg Cost/PR ({year})"')
                lines.append(f"    x-axis [{dates}]")
                lines.append(f'    y-axis "Cost ($)" 0 --> {y_max}')
                lines.append(f"    bar [{cost_vals}]")
                lines.append("```")
                if annotations:
                    lines.append("")
                    lines.append("> " + " · ".join(annotations))
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
        or snapshot_total_prs(new) - snapshot_total_prs(old) >= 5
        or snapshot_implement_runs(new) - snapshot_implement_runs(old) >= 5
        or set(new.get("modules", {}).keys()) != set(old.get("modules", {}).keys())
    )


MAX_EVAL_BRANCH_SUFFIX = 20


def _run(ctx: RepoContext, cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ctx.repo_dir))


def _eval_branch_action(ctx: RepoContext, branch: str) -> tuple[str, str]:
    """Decide how an eval publish can use `branch`.

    Returns (action, detail): "create" (branch is free), "recreate" (only a stale local
    copy exists — merged into origin/main, or its PR is merged or closed), "reuse" (it has
    an open PR; detail is the PR URL), "taken", or "error" (detail is the message).
    """
    local = _run(ctx, ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    remote = _run(ctx, ["git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}"])
    on_remote = remote.returncode == 0 and bool((remote.stdout or "").strip())

    listed = _run(
        ctx, ["gh", "pr", "list", "--head", branch, "--state", "all", "--json", "state,url"]
    )
    if listed.returncode != 0:
        return "error", f"could not list PRs for {branch}\n{listed.stderr}"
    try:
        prs = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return "error", f"could not parse PRs for {branch}"

    open_pr = next((p for p in prs if p.get("state") == "OPEN"), None)
    if open_pr:
        return "reuse", open_pr.get("url", "")
    if on_remote:
        return "taken", ""
    if local.returncode != 0:
        return "create", ""
    if prs:
        return "recreate", ""
    merged = _run(
        ctx, ["git", "merge-base", "--is-ancestor", f"refs/heads/{branch}", "origin/main"]
    )
    return ("recreate", "") if merged.returncode == 0 else ("taken", "")


def _choose_eval_branch(ctx: RepoContext, date: str) -> tuple[str, str, str]:
    """Return (branch, action, detail) for the first usable chore/eval-<date>[-N] branch."""
    base = f"chore/eval-{date}"
    for n in range(1, MAX_EVAL_BRANCH_SUFFIX + 1):
        branch = base if n == 1 else f"{base}-{n}"
        action, detail = _eval_branch_action(ctx, branch)
        if action != "taken":
            return branch, action, detail
    return base, "error", f"no free branch name for {base}"


def _restore_to_head(ctx: RepoContext, paths: list[Path]) -> None:
    """Reset `paths` to their HEAD version; remove any that HEAD does not track."""
    for path in paths:
        rel = path.relative_to(ctx.repo_dir).as_posix()
        if _run(ctx, ["git", "cat-file", "-e", f"HEAD:{rel}"]).returncode == 0:
            _run(ctx, ["git", "checkout", "HEAD", "--", rel])
        else:
            _run(ctx, ["git", "rm", "--cached", "-q", "--ignore-unmatch", "--", rel])
            path.unlink(missing_ok=True)


def _publish_via_pr(ctx: RepoContext, snapshot_path: Path, eval_path: Path, date: str) -> str:
    """Commit the snapshot and EVAL.md on an eval branch and open (or update) its PR.

    On failure both files are restored to their HEAD state on main.
    """
    paths = [snapshot_path, eval_path]
    branch_name = ""
    created = False
    published = False
    try:
        branch_name, action, detail = _choose_eval_branch(ctx, date)
        if action == "error":
            return f"Error: {detail}"

        if action == "reuse":
            # The PR branch has its own EVAL.md and snapshot, so main must be clean to switch.
            contents = {p: p.read_bytes() for p in paths}
            _restore_to_head(ctx, paths)
            result = _run(ctx, ["git", "fetch", "origin", branch_name])
            if result.returncode != 0:
                return f"Error: could not fetch {branch_name}\n{result.stderr}"
            result = _run(ctx, ["git", "checkout", "-B", branch_name, "FETCH_HEAD"])
            if result.returncode != 0:
                return f"Error: could not check out {branch_name}"
            for path, data in contents.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        else:
            if action == "recreate":
                _run(ctx, ["git", "branch", "-D", branch_name])
            result = _run(ctx, ["git", "checkout", "-b", branch_name])
            if result.returncode != 0:
                return f"Error: could not create branch {branch_name}"
            created = True

        result = _run(ctx, ["git", "add", str(snapshot_path), str(eval_path)])
        if result.returncode != 0:
            return "Error: git add failed"

        commit_msg = f"chore: update eval report ({date})"
        result = _run(ctx, ["git", "commit", "-m", commit_msg])
        if result.returncode != 0:
            return "Error: git commit failed"

        result = _run(ctx, ["git", "push", "-u", "origin", branch_name])
        if result.returncode != 0:
            return f"Error: git push failed\n{result.stderr}"

        if action == "reuse":
            published = True
            return f"PR updated: {detail}"

        pr_title = f"chore: update eval report ({date})"
        result = _run(
            ctx,
            ["gh", "pr", "create", "--title", pr_title, "--body", "Automated eval report update."],
        )
        if result.returncode != 0:
            return f"Error: PR creation failed\n{result.stderr}"

        published = True
        return f"PR created: {result.stdout.strip()}"
    except FileNotFoundError as e:
        return f"Error: {e.filename or 'git'} not found"
    finally:
        try:
            if not published:
                _restore_to_head(ctx, paths)
            _run(ctx, ["git", "checkout", "main"])
            if not published:
                _restore_to_head(ctx, paths)
                if created:
                    _run(ctx, ["git", "branch", "-D", branch_name])
        except FileNotFoundError:
            pass


def main(
    compare: str | None = None,
    trend: bool = False,
    repo: str | None = None,
    *,
    ctx: RepoContext,
    output: str | None = None,
    publish: bool = False,
    pr: bool = False,
    force: bool = False,
    auto_merge_edit_rate_threshold: float = 0.05,
    auto_merge_success_threshold: float = 0.90,
    auto_merge_volume_floor: int = 10,
    auto_merge_promotion_level: str = "module",
    eval_min_n: int = DEFAULT_MIN_N,
    fixup_window_days: int = DEFAULT_FIXUP_WINDOW_DAYS,
    fixup_ignore: list[str] | None = None,
):
    repo_dir = str(ctx.repo_dir)

    if publish:
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

        migrate_unpublished_snapshots(ctx)

        runs = load_run_history(ctx)
        pr_data = fetch_pr_data(repo, fixup_window_days, fixup_ignore) if repo else []
        pr_data = enrich_pr_data_with_runs(pr_data, runs)
        snapshot = compute_snapshot(runs, pr_data, fixup_window_days=fixup_window_days)

        previous = load_latest_snapshot(ctx)
        snapshot.update(_compute_period_stats(snapshot, previous))

        daily_path = save_snapshot(snapshot, ctx)

        changed = previous is None or material_change(snapshot, previous)
        if not changed and not force:
            print(f"Snapshot saved to {daily_path}")
            print("No material change, skipping EVAL.md commit")
            return

        path = save_snapshot(snapshot, ctx, published=True)
        print(f"Snapshot saved to {path}")

        all_snaps = load_all_snapshots(ctx)
        content = generate_eval_md(
            snapshot,
            all_snaps,
            edit_rate_threshold=auto_merge_edit_rate_threshold,
            success_threshold=auto_merge_success_threshold,
            volume_floor=auto_merge_volume_floor,
            promotion_level=auto_merge_promotion_level,
            min_n=eval_min_n,
        )
        eval_path = ctx.repo_dir / "EVAL.md"
        eval_path.write_text(content)

        date = snapshot["date"]

        if changed:
            print("Material change detected, publishing EVAL.md")
        else:
            print("No material change, publishing EVAL.md anyway (--force)")

        if pr:
            print(_publish_via_pr(ctx, path, eval_path, date))
            return

        try:
            add_result = subprocess.run(["git", "add", str(path), str(eval_path)], cwd=repo_dir)
        except FileNotFoundError:
            print("Error: git not found")
            return
        if add_result.returncode != 0:
            print("Error: git add failed")
            return

        try:
            commit_result = subprocess.run(
                ["git", "commit", "-m", f"chore: update eval report ({date})"],
                cwd=repo_dir,
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
        return

    if trend:
        snapshots = load_all_snapshots(ctx)
        if output == "json":
            print(json.dumps(snapshots))
            return
        print(format_trend(snapshots))
        return

    if compare == "latest":
        runs = load_run_history(ctx)
        pr_data = fetch_pr_data(repo, fixup_window_days, fixup_ignore) if repo else []
        pr_data = enrich_pr_data_with_runs(pr_data, runs)
        current = compute_snapshot(runs, pr_data, fixup_window_days=fixup_window_days)
        previous = load_latest_snapshot(ctx)
        current.update(_compute_period_stats(current, previous))
        if previous is None:
            if output == "json":
                print(json.dumps(current))
            else:
                print("No previous snapshot to compare against.")
                print(format_snapshot(current))
            path = save_snapshot(current, ctx)
            if output != "json":
                print(f"\nSnapshot saved to {path}")
            return

        comparison = compare_snapshots(previous, current)
        if output == "json":
            print(json.dumps(comparison))
        else:
            print(format_comparison(comparison))

        path = save_snapshot(current, ctx)
        if output != "json":
            print(f"\nSnapshot saved to {path}")
        return

    runs = load_run_history(ctx)
    pr_data = fetch_pr_data(repo, fixup_window_days, fixup_ignore) if repo else []
    pr_data = enrich_pr_data_with_runs(pr_data, runs)
    snapshot = compute_snapshot(runs, pr_data, fixup_window_days=fixup_window_days)
    previous = load_latest_snapshot(ctx)
    snapshot.update(_compute_period_stats(snapshot, previous))

    if output == "json":
        print(json.dumps(snapshot))
    else:
        print(format_snapshot(snapshot))
    path = save_snapshot(snapshot, ctx)
    if output != "json":
        print(f"\nSnapshot saved to {path}")
