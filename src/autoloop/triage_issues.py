"""Cron 1: Triage untriaged GitHub issues via Claude."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autoloop.config import AutoLoopConfig

from autoloop.claude_runner import ClaudeResult, run_claude
from autoloop.config import REPO_DIR
from autoloop.create_issue import build_issue_body

LOG_FILE = REPO_DIR / "autoloop" / "run_history.jsonl"


def build_triage_prompt(cfg: AutoLoopConfig) -> str:
    """Build the triage prompt with config-supplied thresholds and commands."""
    return f"""\
Evaluate this GitHub issue for implementation readiness.

The project source tree and CLAUDE.md are provided below the requirements. Use
them as ground truth about the codebase:
- Validate file references. If the issue names a module, path, or function that
  does not appear in the project tree, treat the reference as invalid: lower your
  confidence in the estimate and call out the invalid reference in "reason".
- Assess feasibility. Judge whether the acceptance criteria are realistic for the
  codebase's actual architecture.
- Detect duplication. If the issue requests functionality that already exists in
  the tree, note the duplication in "reason" and reduce the readiness accordingly.

TEMPLATE REQUIREMENTS — reject if missing:
- Summary (one clear sentence)
- Type (bug/feature/refactor)
- Expected Behavior (specific and testable)
- Acceptance Criteria (at least one checkbox item)

"Files to Modify" is optional — do NOT reject for missing files.

REJECTION GUIDANCE:
- When rejecting, explain what is missing or vague at the module or function level.
- Do NOT suggest specific line numbers, variable names, or exact assertion text.
- Good: "Expected Behavior should describe observable output, not internal state"
- Bad: "add assertion 'PROFILE.md content appears at index 3 of system_prompt'"
- The goal is to tell the submitter WHAT to fix, not HOW to implement it.

SIZE ESTIMATION:
- 1 point: single file change, <50 lines
- 2 points: 1-3 files, new function + tests, <150 lines
- 3+ points: 4+ files, schema changes, new module, >150 lines

PROJECT COMMANDS:
- Test: {cfg.verify_cmd}
- Lint: {cfg.lint_command}

DECOMPOSITION CONSTRAINTS (for "needs-decomposition" verdict):
1. Decompose by LOGICAL UNIT OF CHANGE, not by individual acceptance criterion.
   Group related criteria that form a coherent PR (50-200 lines of change).
2. Group acceptance criteria that modify the same file(s) into ONE sub-issue.
3. Test files, fixtures, and related test helpers belong in ONE sub-issue.
4. Never create a sub-issue smaller than 2 story points (~50 lines of code).
   Merge anything smaller with a related sub-issue.
5. Never produce more than 12 sub-issues from a single parent issue.
6. If multiple criteria are trivial (< 10 lines each), group them by module or theme.

Sub-issue size calibration:
- 1 point (< 30 lines): Too small — MUST be merged with another sub-issue
- 2 points (30-80 lines): Minimum viable sub-issue
- 3 points (80-150 lines): Standard sub-issue
- 5 points (150-300 lines): Large — only split if clearly separable
- 8+ points (300+ lines): Must be further decomposed

Before returning a decomposition, self-check:
- Any sub-issue < 2 points? → merge it with a related neighbor
- Multiple sub-issues targeting the same file? → merge them
- More than 12 sub-issues? → re-decompose at a higher abstraction level

VERDICT:
- "ready" if template complete AND estimated ≤{cfg.max_story_points} points
- "needs-decomposition" if template complete BUT >{cfg.max_story_points} points
- "rejected" if template incomplete or vague

Respond with JSON only:
{{{{
  "verdict": "ready" | "needs-decomposition" | "rejected",
  "points": 1 | 2 | 3 | 5 | 8,
  "priority": "p0" | "p1" | "p2",
  "reason": "one line",
  "files_missing": true | false,
  "decomposition": [...]
}}}}

Include "decomposition" only if verdict is "needs-decomposition".
Each sub-issue: {{{{order, title, points, depends_on, files, why_first/why_after}}}}.
"""


FILE_DISCOVERY_PROMPT = """\
Given this issue and the project structure, identify files to modify and test.

Project structure:
{tree}

CLAUDE.md:
{claude_md}

Issue #{number}: {title}
{body}

Respond with JSON only:
{{
  "files_to_modify": [
    {{"path": "src/patina/example.py", "reason": "main"}},
    {{"path": "tests/test_example.py", "reason": "test coverage"}}
  ]
}}
"""

SUB_ISSUE_PROMPT = """\
Generate structured issue fields for this sub-issue of a decomposed parent.

Parent issue: #{parent_number}
Parent summary: {parent_summary}

Sub-issue: {step_title}
Files: {step_files}
Reason for ordering: {step_reason}

Respond with JSON only:
{{
  "expected_behavior": "specific, testable description",
  "acceptance_criteria": ["criterion 1", "criterion 2"]
}}

Rules:
- Expected behavior must describe observable outputs, not repeat the title.
- Acceptance criteria must be verifiable by running a test or command.
- Do not include generic criteria like "tests pass" or "lint clean".
- Reference function names and modules, not line numbers.
"""

REWRITE_PROMPT = """\
This GitHub issue was rejected by automated triage. Rewrite the issue body so it
addresses the rejection reason and passes triage on the next attempt.

REJECTION REASON:
{reason}

CURRENT ISSUE BODY:
{body}

The rewritten body MUST satisfy every template requirement:
- Summary (one clear sentence)
- Type (bug/feature/refactor)
- Expected Behavior (specific and testable — describe observable output)
- Acceptance Criteria (at least one checkbox item, each verifiable)

Rules:
- Fix only what the rejection flagged; preserve the original intent and scope.
- Keep the existing `## ` section headers.
- Reference function names and modules, not line numbers.
- Respond with the full rewritten issue body as markdown ONLY — no preamble,
  no surrounding code fences, no commentary.
"""


# --- Pure functions (testable without mocking) ---


def parse_triage_response(stdout: str) -> dict:
    """Extract JSON verdict from Claude's triage output."""
    text = stdout.strip()
    if "```" in text:
        text = text.split("```")[1].replace("json", "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return {"verdict": "rejected", "reason": "Failed to parse triage response"}


def parse_file_discovery_response(stdout: str) -> list[dict]:
    """Extract file list JSON from Claude's file discovery output."""
    text = stdout.strip()
    if "```" in text:
        text = text.split("```")[1].replace("json", "").strip()
    try:
        return json.loads(text).get("files_to_modify", [])
    except (json.JSONDecodeError, IndexError):
        return []


def validate_discovered_files(files: list[dict], repo_dir: Path) -> list[dict]:
    """Filter to files that exist or are new test files."""
    return [f for f in files if (repo_dir / f["path"]).exists() or f["path"].startswith("tests/")]


def parse_sub_issue_response(stdout: str) -> dict | None:
    """Extract sub-issue fields JSON from Claude's output."""
    text = stdout.strip()
    if "```" in text:
        text = text.split("```")[1].replace("json", "").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return None
    if not isinstance(data, dict) or "expected_behavior" not in data:
        return None
    return data


def parse_rewritten_body(stdout: str) -> str | None:
    """Extract a rewritten issue body from Claude's output."""
    text = stdout.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    if "## " not in text:
        return None
    return text


def _merge_steps(a: dict, b: dict) -> dict:
    """Merge two decomposition steps into one consolidated step."""
    files_a = a.get("files", [])
    files_b = b.get("files", [])
    merged_files = sorted(set(files_a) | set(files_b))
    points = a.get("points", 1) + b.get("points", 1)
    why_a = a.get("why_first") or a.get("why_after", "")
    why_b = b.get("why_first") or b.get("why_after", "")
    why = "; ".join(filter(None, [why_a, why_b]))
    return {
        "order": min(a.get("order", 1), b.get("order", 1)),
        "title": a["title"] + " + " + b["title"],
        "points": points,
        "depends_on": [],
        "files": merged_files,
        "why_after": why,
    }


def validate_decomposition(decomposition: list[dict], max_sub_issues: int = 12) -> list[dict]:
    """Consolidate over-decomposed sub-issues by merging shared-file and tiny steps."""
    if len(decomposition) <= 1:
        return list(decomposition)

    steps = [dict(s) for s in decomposition]

    # Pass 1: Merge steps that share any file
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(steps):
            files_i = set(steps[i].get("files", []))
            if not files_i:
                i += 1
                continue
            j = i + 1
            while j < len(steps):
                files_j = set(steps[j].get("files", []))
                if files_i & files_j:
                    steps[i] = _merge_steps(steps[i], steps[j])
                    files_i = set(steps[i].get("files", []))
                    del steps[j]
                    changed = True
                else:
                    j += 1
            i += 1

    # Pass 2: Absorb steps with < 2 story points into nearest neighbor
    changed = True
    while changed and len(steps) > 1:
        changed = False
        for i, s in enumerate(steps):
            if s.get("points", 0) < 2:
                neighbor = i + 1 if i + 1 < len(steps) else i - 1
                steps[neighbor] = _merge_steps(steps[neighbor], steps[i])
                del steps[i]
                changed = True
                break

    # Pass 3: Force-merge smallest pairs until at or below cap
    while len(steps) > max_sub_issues:
        min_idx = min(range(len(steps)), key=lambda i: steps[i].get("points", 0))
        neighbor = min_idx + 1 if min_idx + 1 < len(steps) else min_idx - 1
        steps[neighbor] = _merge_steps(steps[neighbor], steps[min_idx])
        del steps[min_idx]

    for i, step in enumerate(steps, 1):
        step["order"] = i
        step["depends_on"] = []

    return steps


def build_decomposition_comment(result: dict) -> str:
    """Build markdown table from decomposition array."""
    rows = []
    for step in result.get("decomposition", []):
        deps = ", ".join(f"Step {d}" for d in step.get("depends_on", [])) or "—"
        files = ", ".join(f"`{f}`" for f in step.get("files", []))
        rows.append(f"| {step['order']} | {step['title']} | {step['points']} | {deps} | {files} |")
    table = (
        f"**Auto-triage:** Estimated at {result['points']} points"
        f" — needs decomposition.\n\n"
        f"| Order | Sub-issue | Pts | Depends on | Files |\n"
        f"|-------|-----------|-----|------------|-------|\n" + "\n".join(rows)
    )
    why_lines = []
    for step in result.get("decomposition", []):
        reason = step.get("why_first") or step.get("why_after", "")
        if reason:
            why_lines.append(f"- Step {step['order']}: {reason}")
    if why_lines:
        table += "\n\n**Why this order:**\n" + "\n".join(why_lines)
    table += (
        "\n\nCreate sub-issues using the issue template. Use `Depends on: #N`\n"
        "(real issue numbers) in the Dependencies field. The implementation bot\n"
        "skips issues whose dependencies aren't merged yet."
    )
    return table


def build_sub_issue_summary_comment(parent_number: int, sub_issues: list[int]) -> str:
    """Build the parent comment listing the created sub-issue numbers."""
    lines = "\n".join(f"- #{n}" for n in sub_issues)
    return (
        f"**Auto-triage — Sub-issues created:**\n\n"
        f"Decomposed #{parent_number} into {len(sub_issues)} sub-issue(s):\n{lines}"
    )


def log_run(
    issue_number: int,
    success: bool,
    attempts: int,
    duration: float,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
):
    """Append a JSON entry to the run history log."""
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "issue": issue_number,
        "success": success,
        "attempts": attempts,
        "duration_seconds": round(duration),
        "cost_usd": round(cost_usd, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
    }
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


# --- Subprocess functions ---


def load_project_context() -> tuple[str, str]:
    """Return the project source tree and CLAUDE.md contents for prompt context."""
    tree = subprocess.run(
        ["find", "src/", "tests/", "-name", "*.py", "-not", "-path", "*__pycache__*"],
        capture_output=True,
        text=True,
        cwd=REPO_DIR,
    ).stdout
    claude_md = (REPO_DIR / "CLAUDE.md").read_text()
    return tree, claude_md


def list_untriaged_issues(cfg: AutoLoopConfig) -> list[dict]:
    """Fetch open issues that have no triage labels yet."""
    triage_labels = set(cfg.triage_labels)
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            cfg.repo,
            "--state",
            "open",
            "--json",
            "number,title,body,labels",
            "--limit",
            "50",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    issues = json.loads(result.stdout)
    return [
        i for i in issues if not any(lbl["name"] in triage_labels for lbl in i.get("labels", []))
    ]


def evaluate_issue(issue: dict, cfg: AutoLoopConfig) -> tuple[dict, ClaudeResult]:
    """Run Claude to evaluate an issue against the triage prompt."""
    tree, claude_md = load_project_context()
    triage_prompt = build_triage_prompt(cfg)
    prompt = (
        triage_prompt
        + "\n\nPROJECT STRUCTURE:\n"
        + tree[: cfg.tree_truncation]
        + "\n\nCLAUDE.md:\n"
        + claude_md
        + f"\n\nIssue #{issue['number']}: {issue['title']}\n\n"
        + (issue.get("body") or "")
    )
    result = run_claude(prompt, cfg.triage_model, cfg.triage_timeout)
    if not result.success:
        verdict = {
            "verdict": "rejected",
            "reason": "Triage timed out — issue body may be too large",
        }
        return verdict, result
    return parse_triage_response(result.text), result


def discover_files(issue: dict, cfg: AutoLoopConfig) -> tuple[list[dict], ClaudeResult]:
    """Ask Claude to identify relevant files for an issue."""
    tree, claude_md = load_project_context()
    prompt = FILE_DISCOVERY_PROMPT.format(
        tree=tree[: cfg.tree_truncation],
        claude_md=claude_md,
        number=issue["number"],
        title=issue["title"],
        body=issue["body"] or "",
    )

    result = run_claude(prompt, cfg.triage_model, cfg.triage_timeout)
    if not result.success:
        return [], result

    files = parse_file_discovery_response(result.text)
    return validate_discovered_files(files, REPO_DIR), result


def enrich_issue_with_files(number: int, files: list[dict], cfg: AutoLoopConfig):
    """Comment with discovered files so the implementation agent sees them."""
    file_lines = "\n".join(f"- `{f['path']}` — {f['reason']}" for f in files)
    comment = f"**Auto-triage — File Discovery:**\n\nIdentified files to modify:\n{file_lines}"
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", cfg.repo, "--body", comment],
    )


def reject_issue(number: int, reason: str, cfg: AutoLoopConfig):
    """Label issue as rejected and comment with the reason."""
    subprocess.run(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            cfg.repo,
            "--add-label",
            "rejected",
        ],
    )
    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(number),
            "--repo",
            cfg.repo,
            "--body",
            f"**Auto-triage — Rejected:** {reason}",
        ],
    )


def rewrite_issue_body(
    issue: dict, reason: str, cfg: AutoLoopConfig
) -> tuple[str | None, ClaudeResult]:
    """Ask Claude to rewrite a rejected issue body to address the reason."""
    prompt = REWRITE_PROMPT.format(reason=reason, body=issue.get("body") or "")
    result = run_claude(prompt, cfg.triage_model, cfg.triage_timeout)
    if not result.success:
        return None, result
    return parse_rewritten_body(result.text), result


def apply_rewrite(number: int, body: str, cfg: AutoLoopConfig):
    """Update the issue body, drop the 'rejected' label, and note the auto-fix."""
    subprocess.run(
        ["gh", "issue", "edit", str(number), "--repo", cfg.repo, "--body", body],
    )
    subprocess.run(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            cfg.repo,
            "--remove-label",
            "rejected",
        ],
        capture_output=True,
    )
    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(number),
            "--repo",
            cfg.repo,
            "--body",
            "**Auto-triage — Auto-fix:** Rewrote the issue body to address the "
            "rejection reason and re-triaging once.",
        ],
    )


def approve_issue(number: int, priority: str, reason: str, cfg: AutoLoopConfig):
    """Label issue as ready with priority and comment."""
    subprocess.run(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            cfg.repo,
            "--add-label",
            f"ready,{priority}",
        ],
    )
    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(number),
            "--repo",
            cfg.repo,
            "--body",
            f"**Auto-triage — Ready ({priority}):** {reason}",
        ],
    )


def suggest_sub_issue_fields(
    parent_number: int,
    parent_summary: str,
    step: dict,
    cfg: AutoLoopConfig,
) -> dict | None:
    """Ask Claude for a specific Expected Behavior + Acceptance Criteria."""
    if not shutil.which("claude"):
        return None

    why = step.get("why_first") or step.get("why_after", "")
    prompt = SUB_ISSUE_PROMPT.format(
        parent_number=parent_number,
        parent_summary=parent_summary,
        step_title=step["title"],
        step_files=", ".join(step.get("files", [])),
        step_reason=why,
    )
    result = run_claude(prompt, cfg.triage_model, cfg.triage_timeout)
    if not result.success:
        return None
    return parse_sub_issue_response(result.text)


def create_sub_issues(
    parent_number: int,
    result: dict,
    cfg: AutoLoopConfig,
    parent_summary: str = "",
) -> list[int]:
    """Create sub-issues from a decomposition and return their numbers."""
    step_to_issue: dict[int, int] = {}
    created: list[int] = []
    for step in result.get("decomposition", []):
        dep_refs = [
            f"#{step_to_issue[d]}" for d in step.get("depends_on", []) if d in step_to_issue
        ]
        deps = "Depends on: " + ", ".join(dep_refs) if dep_refs else ""

        fields = suggest_sub_issue_fields(parent_number, parent_summary, step, cfg)
        if fields:
            expected = fields.get("expected_behavior") or step["title"]
            extra_criteria = "\n".join(fields.get("acceptance_criteria", []))
        else:
            expected = step["title"]
            extra_criteria = ""

        why = step.get("why_first") or step.get("why_after", "")
        body = build_issue_body(
            summary=step["title"],
            issue_type="feature",
            files="\n".join(step.get("files", [])),
            current_behavior="",
            expected=expected,
            extra_criteria=extra_criteria,
            hints=f"Sub-issue of #{parent_number}. {why}".strip(),
            deps=deps,
            context=f"Parent issue: #{parent_number}",
        )

        proc = subprocess.run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                cfg.repo,
                "--title",
                step["title"],
                "--body",
                body,
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            issue_url = proc.stdout.strip()
            issue_num = int(issue_url.rstrip("/").split("/")[-1])
            step_to_issue[step["order"]] = issue_num
            created.append(issue_num)

    return created


def decompose_issue(
    number: int,
    result: dict,
    cfg: AutoLoopConfig,
    parent_summary: str = "",
):
    """Label the parent needs-decomposition, create sub-issues, post summary."""
    subprocess.run(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            cfg.repo,
            "--add-label",
            "needs-decomposition",
        ],
    )
    validated = dict(result)
    validated["decomposition"] = validate_decomposition(result.get("decomposition", []))
    comment = build_decomposition_comment(validated)
    subprocess.run(
        [
            "gh",
            "issue",
            "comment",
            str(number),
            "--repo",
            cfg.repo,
            "--body",
            comment,
        ],
    )
    sub_issues = create_sub_issues(number, validated, cfg, parent_summary)
    if sub_issues:
        subprocess.run(
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "--repo",
                cfg.repo,
                "--body",
                build_sub_issue_summary_comment(number, sub_issues),
            ],
        )


# --- Orchestration ---


def triage_issue(issue: dict, cfg: AutoLoopConfig, auto_fix: bool = True) -> list[ClaudeResult]:
    """Evaluate a single issue and apply the appropriate label."""
    results: list[ClaudeResult] = []
    verdict, eval_result = evaluate_issue(issue, cfg)
    results.append(eval_result)

    if verdict["verdict"] == "rejected":
        if auto_fix:
            new_body, rewrite_result = rewrite_issue_body(issue, verdict["reason"], cfg)
            results.append(rewrite_result)
            if new_body:
                apply_rewrite(issue["number"], new_body, cfg)
                results.extend(triage_issue({**issue, "body": new_body}, cfg, auto_fix=False))
                return results
        reject_issue(issue["number"], verdict["reason"], cfg)
        return results

    if verdict.get("files_missing", False):
        files, disc_result = discover_files(issue, cfg)
        results.append(disc_result)
        if files:
            enrich_issue_with_files(issue["number"], files, cfg)

    if verdict["verdict"] == "ready":
        from autoloop.config import touches_protected_path
        from autoloop.create_issue import extract_files_from_spec

        body = issue.get("body") or ""
        mentioned_files = extract_files_from_spec(body)
        if touches_protected_path(mentioned_files, cfg.protected_paths):
            print(f"  #{issue['number']}: touches protected path, routing to needs-human")
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "edit",
                    str(issue["number"]),
                    "--repo",
                    cfg.repo,
                    "--add-label",
                    "needs-human",
                ],
            )
            subprocess.run(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(issue["number"]),
                    "--repo",
                    cfg.repo,
                    "--body",
                    "**Auto-triage — needs-human:** issue targets protected paths"
                    f" ({', '.join(mentioned_files)}). Requires manual implementation.",
                ],
            )
            return results
        approve_issue(issue["number"], verdict["priority"], verdict["reason"], cfg)
    elif verdict["verdict"] == "needs-decomposition":
        decompose_issue(issue["number"], verdict, cfg, issue.get("body") or "")

    return results


def main():
    from autoloop.config import load_config

    cfg = load_config()
    start_time = time.time()
    results: list[ClaudeResult] = []

    issues = list_untriaged_issues(cfg)
    if not issues:
        print("No untriaged issues found.")
        return
    for issue in issues:
        print(f"Triaging #{issue['number']}: {issue['title']}")
        results.extend(triage_issue(issue, cfg))

    if results:
        elapsed = time.time() - start_time
        total_cost = sum(r.cost_usd for r in results)
        total_input = sum(r.input_tokens for r in results)
        total_output = sum(r.output_tokens for r in results)
        total_cache_read = sum(r.cache_read_tokens for r in results)
        print("\n--- AutoLoop Triage Stats ---")
        print(f"  Duration: {elapsed:.0f}s")
        print(f"  Claude calls: {len(results)}")
        print(f"  Input tokens: {total_input:,}")
        print(f"  Output tokens: {total_output:,}")
        print(f"  Cost: ${total_cost:.2f}")
        log_run(
            0,
            True,
            len(issues),
            elapsed,
            total_cost,
            total_input,
            total_output,
            total_cache_read,
        )


if __name__ == "__main__":
    main()
