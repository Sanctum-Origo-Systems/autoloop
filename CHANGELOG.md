# Changelog

## v0.4.1

- `autoloop_review_pr` MCP tool: review PRs remotely via MCP
- Track and report cost for review-pr runs (tokens, cost in PR comment + run_history.jsonl)
- Review-pr removes `needs-human` label when review passes after prior failure

## v0.4.0

- Mutation gate: detect dead-but-green code before opening PRs, with test_gate_skip_types bypass
- `review-pr` CLI subcommand: PR checkout, mutation gate, semantic review, verdict comment, needs-human labeling
- Duplicate issue detection at triage: flag overlapping issues before labeling ready
- `review_model` and `test_gate_skip_types` config fields in autoloop.toml
- Fix-pr posts result comments on PRs (success or failure reason)

## v0.3.5

- Migrate from mcp SDK v1 to fastmcp v3 (removes `mcp<2` version pin workaround)

## v0.3.4

- Fix session detection blocking implement when Claude Code runs in a different directory

## v0.3.3

- `autoloop doctor` command: validates environment, config, CLI auth, verify_cmd, and session conflicts before first run
- Skip test-file gate for refactor/migration/docs issues instead of failing valid work
- Truncate oversized issue bodies to `spec_truncation` limit with link to full issue
- Actionable timeout error messages with fix suggestions posted to GitHub
- README: TLDR quickstart section, doctor documentation, em dash cleanup

## v0.3.2

- Pin `mcp<2` to fix broken MCP server import with mcp v2.0.0

## v0.3.1

- Skip decomposition when validation collapses to a single sub-issue; mark parent `ready` instead
- Raise `max_story_points` default from 2 to 3; include in generated `autoloop.toml`

## v0.3.0

- Multi-language support: triage and implementation prompts use project commands from config instead of hardcoded Python tools
- Default `lint_command` to empty string; no longer assumes ruff on unconfigured projects
- `autoloop preflight` command and MCP tool to validate build environment before implementation
- Fix cleanup workflow: add missing checkout step, install from `@main` instead of pinned tag

## v0.2.3

- Fix verification using hardcoded ruff commands instead of `lint_command` from config

## v0.2.2

- Configurable `test_pattern` replaces hardcoded `tests/*.py` check; supports any language
- `autoloop init` infers `test_pattern` from `--verify-cmd` (empty for build-only projects)
- Fix permission scaffold using invalid colon syntax (`Bash(git add:*)` → `Bash(git add *)`)

## v0.2.1

- MCP tools accept `repo_dir` parameter for multi-repo support from a single session
- Fix `validate_decomposition` clearing dependency chains; now remaps references after merging

## v0.2.0

- `autoloop init` scaffolds `.claude/settings.json` with minimal permissions for headless runs
- Detect active Claude Code session before implement; fail fast with actionable error
- Empty-branch diagnostic: report probable causes instead of misleading lint/test failures
- Cap decomposition depth at 2 levels; close parent issues after children are filed
- README restructured with platform support (macOS/Linux), mode framing, and local-mode notes

## v0.1.8

- Validate decomposition: merge shared-file steps, absorb tiny sub-issues, cap at 12 per parent
- Rename spec parser tag from `## Enhancement` to `## Task` with backward compatibility

## v0.1.7

- Recursive auto-close walks nested parent chains
- Expanded README with quickstart guide, config reference, and mobile workflow

## v0.1.6

- Complete fix-pr: handles merge conflicts, stale base, lint failures, and test failures
- MCP tool `autoloop_fix_pr` for remote PR fixing

## v0.1.5

- Fix conflict detection using rebase output and git status
- Fix infinite recursion in `continue_rebase`

## v0.1.4

- Add `fix-pr` command and `autoloop_fix_pr` MCP tool
- Fix PR by rebasing on main and resolving conflicts with Claude

## v0.1.3

- Configurable `timer_prefix` for systemd timer detection in status
- Status command and MCP tool detect timers by project-specific prefix

## v0.1.2

- Fully detach MCP subprocess calls to prevent connection blocking
- Dynamic version in generated workflow template
- Remove tracked `__pycache__` files

## v0.1.1

- Remove patina-specific `update_changelog.py` from generic package

## v0.1.0

- Initial extraction as standalone package
- CLI with subcommands: init, plan, triage, implement, status, auto-close-parent, version
- MCP server with tools: autoloop_implement, autoloop_triage, autoloop_status
- Config-driven via `autoloop.toml`
- Graceful `systemctl` handling on macOS
