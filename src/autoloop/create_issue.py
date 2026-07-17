"""Build a well-formed GitHub issue from the autoloop template."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autoloop.config import AutoLoopConfig

DEFAULT_ACCEPTANCE = [
    "New unit tests pass",
    "All existing tests pass (`uv run pytest`)",
    "`uv run ruff check && uv run ruff format --check` clean",
]

VALID_TYPES = {"bug", "feature", "refactor"}


def prompt_required(label: str) -> str:
    """Prompt until a non-empty value is provided."""
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print(f"  {label} is required.")


def prompt_optional(label: str, hint: str = "") -> str:
    """Prompt for an optional field. Returns empty string if skipped."""
    suffix = f" ({hint})" if hint else ""
    return input(f"{label}{suffix} [Enter to skip]: ").strip()


def prompt_multiline(label: str, hint: str = "") -> str:
    """Prompt for multi-line input. Empty line finishes."""
    suffix = f" ({hint})" if hint else ""
    print(f"{label}{suffix} (blank line to finish):")
    lines = []
    while True:
        line = input()
        if line == "":
            break
        lines.append(line)
    return "\n".join(lines)


def build_issue_body(
    summary: str,
    issue_type: str,
    files: str,
    current_behavior: str,
    expected: str,
    extra_criteria: str,
    hints: str,
    deps: str,
    context: str,
) -> str:
    """Assemble the markdown issue body from field values."""
    sections = [f"## Summary\n{summary}", f"## Type\n{issue_type}"]

    if files:
        file_lines = "\n".join(f"- {f}" for f in files.split("\n") if f.strip())
        sections.append(f"## Files to Modify\n{file_lines}")
    else:
        sections.append("## Files to Modify\nUnknown")

    if issue_type == "bug" and current_behavior:
        sections.append(f"## Current Behavior\n{current_behavior}")

    sections.append(f"## Expected Behavior\n{expected}")

    criteria_lines = [f"- [ ] {c}" for c in DEFAULT_ACCEPTANCE]
    if extra_criteria:
        for line in extra_criteria.split("\n"):
            line = line.strip().lstrip("- ").lstrip("[] ").strip()
            if line:
                criteria_lines.append(f"- [ ] {line}")
    sections.append("## Acceptance Criteria\n" + "\n".join(criteria_lines))

    if hints:
        sections.append(f"## Implementation Hints\n{hints}")

    if deps:
        sections.append(f"## Dependencies\n{deps}")

    if context:
        sections.append(f"## Context\n{context}")

    sections.append("## Story Points\n<!-- Triage bot will estimate -->")

    return "\n\n".join(sections)


SUGGEST_PROMPT = """\
Given this issue summary and the project structure, suggest fields for a GitHub issue.

Project structure:
{tree}

Issue type: {issue_type}
Summary: {summary}

Respond with JSON only:
{{
  "files": ["src/patina/example.py", "tests/test_example.py"],
  "current_behavior": "what happens now (only if type is bug, else empty string)",
  "expected_behavior": "specific, testable description of correct behavior",
  "acceptance_criteria": ["criterion 1", "criterion 2"],
  "implementation_hints": "function names and patterns to follow (no line numbers)"
}}

Rules:
- Reference files and function names, never line numbers.
- Expected behavior must be specific and testable.
- Acceptance criteria must be verifiable by running a test or command.
- Do not include generic criteria like "tests pass" or "lint clean" — those are added automatically.
"""


def suggest_fields(summary: str, issue_type: str, cfg: AutoLoopConfig) -> dict | None:
    """Call Claude to suggest issue fields based on summary and codebase."""
    if not shutil.which("claude"):
        return None

    tree = subprocess.run(
        ["find", "src/", "tests/", "-name", "*.py", "-not", "-path", "*__pycache__*"],
        capture_output=True,
        text=True,
        cwd=Path.cwd(),
    ).stdout

    prompt = SUGGEST_PROMPT.format(
        tree=tree[:3000],
        issue_type=issue_type,
        summary=summary,
    )

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", cfg.triage_model, prompt],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None

    text = result.stdout.strip()
    if "```" in text:
        text = text.split("```")[1].replace("json", "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return None


SPEC_TO_ISSUE_PROMPT = """\
Convert this spec task into structured GitHub issue fields.

Task spec:
{spec_text}

Issue type: feature

Respond with JSON only:
{{
  "title": "short title under 70 chars",
  "files": ["file1.py", "file2.py"],
  "expected_behavior": "specific, testable description of correct behavior",
  "acceptance_criteria": ["criterion 1", "criterion 2"],
  "deps": ""
}}

Rules:
- Title should be concise and action-oriented.
- Reference files and function names, never line numbers.
- Expected behavior must be specific and testable.
- Acceptance criteria must be verifiable by running a test or command.
- Do not include generic criteria like "tests pass" or "lint clean" — those are added automatically.
- deps should contain "Depends on: #N" if the spec mentions dependencies, else empty string.
"""


def parse_spec_enhancements(spec_path: str) -> list[dict]:
    """Parse a spec markdown file into task/enhancement sections."""
    text = Path(spec_path).read_text()
    enhancements = []
    current_title = None
    current_lines = []

    for line in text.split("\n"):
        match = re.match(r"^## (?:Task|Enhancement)\s*\d*[.:]\s*(.+)", line)
        if match:
            if "## Enhancement" in line:
                print("Note: '## Enhancement' is supported but '## Task' is preferred.")
            if current_title is not None:
                enhancements.append(
                    {
                        "title": current_title,
                        "body": "\n".join(current_lines).strip(),
                    }
                )
            current_title = match.group(1).strip()
            current_lines = []
        elif current_title is not None:
            if re.match(r"^## ((?:Task|Enhancement)\s*\d+|Summary|Critical Files)", line):
                enhancements.append(
                    {
                        "title": current_title,
                        "body": "\n".join(current_lines).strip(),
                    }
                )
                current_title = None
                current_lines = []
            else:
                current_lines.append(line)

    if current_title is not None:
        enhancements.append(
            {
                "title": current_title,
                "body": "\n".join(current_lines).strip(),
            }
        )

    return enhancements


def spec_to_issue_fields(enhancement: dict, cfg: AutoLoopConfig) -> dict | None:
    """Use Claude to convert a spec enhancement into structured issue fields."""
    if not shutil.which("claude"):
        return None

    prompt = SPEC_TO_ISSUE_PROMPT.format(spec_text=enhancement["body"][:4000])

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", cfg.triage_model, prompt],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0:
        return None

    text = result.stdout.strip()
    if "```" in text:
        text = text.split("```")[1].replace("json", "").strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        return None


def extract_files_from_spec(body: str) -> list[str]:
    """Extract file paths mentioned in **File:** or **Files:** lines."""
    files = []
    for match in re.finditer(r"\*\*Files?:\*\*\s*(.+)", body):
        line = match.group(1)
        for part in re.findall(r"`([^`]+)`", line):
            part = part.strip()
            if part:
                files.append(part)
        if not files:
            for part in line.split(" and "):
                part = part.strip().strip("`")
                if part:
                    files.append(part)
    return files


def extract_problem_from_spec(body: str) -> str:
    """Extract the **Problem:** text from the spec body."""
    match = re.search(r"\*\*Problem:\*\*\s*(.+?)(?:\n\n|\n\*\*)", body, re.DOTALL)
    if match:
        return match.group(1).strip()
    return ""


def create_issues_from_spec(
    spec_path: str,
    skip: list[int],
    cfg: AutoLoopConfig,
    *,
    dry_run: bool = False,
) -> None:
    """Parse a spec file and create GitHub issues for each enhancement."""
    enhancements = parse_spec_enhancements(spec_path)
    if not enhancements:
        print("No tasks found in spec.")
        return

    print(f"Found {len(enhancements)} task(s) in {spec_path}\n")

    for i, enh in enumerate(enhancements, 1):
        if i in skip:
            print(f"  Skipping Task {i}: {enh['title']}")
            continue

        print(f"  Task {i}: {enh['title']}")

        files = extract_files_from_spec(enh["body"])
        problem = extract_problem_from_spec(enh["body"])

        print("    Generating structured fields via Claude...")
        fields = spec_to_issue_fields(enh, cfg)

        if fields:
            title = fields.get("title", enh["title"])
            files_str = "\n".join(fields.get("files") or files)
            expected = fields.get("expected_behavior", "")
            extra_criteria = "\n".join(fields.get("acceptance_criteria") or [])
            deps = fields.get("deps", "")
        else:
            print("    Claude unavailable — using spec text directly.")
            title = enh["title"]
            files_str = "\n".join(files)
            expected = problem
            extra_criteria = ""
            deps = ""

        body = build_issue_body(
            summary=problem or title,
            issue_type="feature",
            files=files_str,
            current_behavior="",
            expected=expected,
            extra_criteria=extra_criteria,
            hints=f"See {spec_path} Task {i} for the full spec.",
            deps=deps,
            context=f"Source spec: {spec_path}",
        )

        if dry_run:
            print(f"\n--- Task {i}: {title} ---\n")
            print(f"**Title:** {title}\n")
            print(body)
            print("\n**Implementation Detail comment would contain the full spec section.**\n")
            print("---")
            continue

        result = subprocess.run(
            ["gh", "issue", "create", "--repo", cfg.repo, "--title", title, "--body", body],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"    Failed to create issue: {result.stderr.strip()}")
            continue

        issue_url = result.stdout.strip()
        issue_number = issue_url.rstrip("/").split("/")[-1]
        print(f"    Created: {issue_url}")

        detail_comment = f"**Implementation Detail:**\n\n{enh['body']}"
        subprocess.run(
            [
                "gh",
                "issue",
                "comment",
                issue_number,
                "--repo",
                cfg.repo,
                "--body",
                detail_comment,
            ],
            capture_output=True,
        )
        print("    Posted implementation detail comment.")

    print("\nDone.")


def parse_issue_sections(body: str) -> dict[str, str]:
    """Parse a markdown issue body into section name -> content."""
    sections = {}
    current = None
    lines = []
    for line in body.split("\n"):
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(lines).strip()
            current = line[3:].strip()
            lines = []
        else:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines).strip()
    return sections


def fetch_issue(number: int, cfg: AutoLoopConfig) -> dict:
    """Fetch an issue's title, body, labels, and comments from GitHub."""
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(number),
            "--repo",
            cfg.repo,
            "--json",
            "title,body,labels,comments",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Failed to fetch issue #{number}.")
        sys.exit(1)
    return json.loads(result.stdout)


def get_rejection_reason(issue_data: dict) -> str | None:
    """Extract the rejection reason from auto-triage comments."""
    for comment in issue_data.get("comments", []):
        body = comment.get("body", "")
        match = re.search(r"\*\*Auto-triage — Rejected:\*\*\s*(.*)", body)
        if match:
            return match.group(1).strip()
    return None


def prompt_edit(label: str, current: str, multiline: bool = False) -> str:
    """Show current value and let user keep or replace it."""
    print(f"\n{label}:")
    if current:
        for line in current.split("\n"):
            print(f"  > {line}")
    else:
        print("  (empty)")
    choice = input("  [Enter to keep, 'e' to edit]: ").strip().lower()
    if choice != "e":
        return current
    if multiline:
        while True:
            result = prompt_multiline(f"New {label}")
            if result:
                return result
            print(f"  {label} cannot be empty. Try again.")
    return prompt_required(f"New {label}")


def edit_issue(number: int, cfg: AutoLoopConfig) -> tuple[str, str]:
    """Fetch a rejected issue, let user fix fields, return (title, body)."""
    issue_data = fetch_issue(number, cfg)
    title = issue_data["title"]
    sections = parse_issue_sections(issue_data.get("body", "") or "")

    reason = get_rejection_reason(issue_data)
    if reason:
        print(f"\nRejection reason: {reason}")
    else:
        print("\nNo rejection reason found — editing anyway.")

    summary = prompt_edit("Summary", sections.get("Summary", title))
    issue_type = sections.get("Type", "feature").strip().lower()

    raw_files = sections.get("Files to Modify", "")
    files_text = "\n".join(
        line.lstrip("- ").strip() for line in raw_files.split("\n") if line.strip()
    )
    if files_text.lower() == "unknown":
        files_text = ""
    files = prompt_edit("Files to Modify", files_text, multiline=True)

    current_behavior = ""
    if issue_type == "bug":
        current_behavior = prompt_edit(
            "Current Behavior",
            sections.get("Current Behavior", ""),
            multiline=True,
        )

    expected = prompt_edit("Expected Behavior", sections.get("Expected Behavior", ""))

    existing_criteria = sections.get("Acceptance Criteria", "")
    extra_lines = []
    for line in existing_criteria.split("\n"):
        cleaned = line.strip().lstrip("- ").lstrip("[] ").lstrip("[ ] ").strip()
        if cleaned and not any(d in cleaned for d in DEFAULT_ACCEPTANCE):
            extra_lines.append(cleaned)
    extra_criteria = prompt_edit(
        "Additional Acceptance Criteria (beyond defaults)",
        "\n".join(extra_lines),
        multiline=True,
    )

    hints = prompt_edit(
        "Implementation Hints",
        sections.get("Implementation Hints", ""),
        multiline=True,
    )
    deps = prompt_edit("Dependencies", sections.get("Dependencies", ""))
    context = prompt_edit("Context", sections.get("Context", ""), multiline=True)

    body = build_issue_body(
        summary,
        issue_type,
        files,
        current_behavior,
        expected,
        extra_criteria,
        hints,
        deps,
        context,
    )
    return summary, body


def update_issue(number: int, title: str, body: str, cfg: AutoLoopConfig):
    """Update the issue on GitHub and remove the rejected label."""
    subprocess.run(
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            cfg.repo,
            "--title",
            title,
            "--body",
            body,
        ],
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
    print(f"Issue #{number} updated. 'rejected' label removed.")


def build_issue(
    cfg: AutoLoopConfig,
    issue_type: str | None = None,
    suggest: bool = True,
) -> tuple[str, str]:
    """Interactively build the issue. Returns (title, body)."""
    summary = prompt_required("Summary")

    if issue_type is None:
        while True:
            issue_type = input("Type (bug / feature / refactor): ").strip().lower()
            if issue_type in VALID_TYPES:
                break
            print(f"  Must be one of: {', '.join(sorted(VALID_TYPES))}")

    suggestions = None
    if suggest:
        print("\nGenerating suggestions from codebase...")
        suggestions = suggest_fields(summary, issue_type, cfg)

    if suggestions:
        print("Suggestions ready. Press Enter to accept each, or 'e' to edit.\n")
        s_files = "\n".join(suggestions.get("files") or [])
        files = prompt_edit("Files to Modify", s_files, multiline=True)

        current_behavior = ""
        if issue_type == "bug":
            current_behavior = prompt_edit(
                "Current Behavior",
                suggestions.get("current_behavior", ""),
                multiline=True,
            )

        expected = prompt_edit(
            "Expected Behavior",
            suggestions.get("expected_behavior", ""),
        )

        print("\nDefault acceptance criteria (always included):")
        for item in DEFAULT_ACCEPTANCE:
            print(f"  - [ ] {item}")
        s_criteria = "\n".join(suggestions.get("acceptance_criteria") or [])
        extra_criteria = prompt_edit(
            "Additional Acceptance Criteria (beyond defaults)",
            s_criteria,
            multiline=True,
        )

        hints = prompt_edit(
            "Implementation Hints",
            suggestions.get("implementation_hints", ""),
            multiline=True,
        )
    else:
        if suggest:
            if shutil.which("claude"):
                print("Could not generate suggestions. Falling back to manual entry.\n")
            else:
                print("claude CLI not found. Using manual entry.\n")

        files = prompt_multiline("Files to Modify", "one per line, e.g. src/patina/store.py")

        current_behavior = ""
        if issue_type == "bug":
            current_behavior = prompt_multiline(
                "Current Behavior",
                "what happens now? include error messages",
            )

        expected = prompt_required("Expected Behavior")

        print("\nDefault acceptance criteria (always included):")
        for item in DEFAULT_ACCEPTANCE:
            print(f"  - [ ] {item}")
        extra_criteria = prompt_multiline(
            "Additional Acceptance Criteria",
            "one per line, beyond the defaults above",
        )

        hints = prompt_multiline("Implementation Hints", "optional")

    deps = prompt_optional("Dependencies", "e.g. Depends on #43")
    context = prompt_multiline("Context", "links, related issues, etc.")

    body = build_issue_body(
        summary,
        issue_type,
        files,
        current_behavior,
        expected,
        extra_criteria,
        hints,
        deps,
        context,
    )
    return summary, body


def main():
    from autoloop import __version__
    from autoloop.config import load_config

    cfg = load_config()

    parser = argparse.ArgumentParser(description="Create or edit a GitHub issue")
    parser.add_argument(
        "--version",
        action="version",
        version=f"autoloop {__version__}",
    )
    parser.add_argument(
        "--type",
        choices=sorted(VALID_TYPES),
        default=None,
        help="Issue type (skips the type prompt)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print markdown to stdout instead of submitting",
    )
    parser.add_argument(
        "--edit",
        type=int,
        metavar="NUMBER",
        default=None,
        help="Edit an existing issue (e.g. fix a rejected issue)",
    )
    parser.add_argument(
        "--no-suggest",
        action="store_true",
        help="Skip Claude-powered field suggestions",
    )
    parser.add_argument(
        "--from-spec",
        metavar="PATH",
        default=None,
        help="Create issues from a spec markdown file (one per ## Task section)",
    )
    parser.add_argument(
        "--skip",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[],
        help="Task numbers to skip when using --from-spec (e.g. --skip 1,2)",
    )
    args = parser.parse_args()

    if args.from_spec:
        create_issues_from_spec(args.from_spec, skip=args.skip, cfg=cfg, dry_run=args.dry_run)
    elif args.edit:
        title, body = edit_issue(args.edit, cfg)
        if args.dry_run:
            print("\n--- Updated Issue Markdown ---\n")
            print(f"**Title:** {title}\n")
            print(body)
            return
        update_issue(args.edit, title, body, cfg)
    else:
        title, body = build_issue(cfg, issue_type=args.type, suggest=not args.no_suggest)
        if args.dry_run:
            print("\n--- Issue Markdown ---\n")
            print(f"**Title:** {title}\n")
            print(body)
            return
        result = subprocess.run(
            ["gh", "issue", "create", "--repo", cfg.repo, "--title", title, "--body", body],
            text=True,
        )
        if result.returncode == 0:
            print("Issue created.")
        else:
            print("Failed to create issue. Printing markdown so you can copy-paste:\n")
            print(f"**Title:** {title}\n")
            print(body)
            sys.exit(1)


if __name__ == "__main__":
    main()
