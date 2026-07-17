# Changelog

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
