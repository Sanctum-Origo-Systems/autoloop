# Autoloop

Config-driven AI pipeline for triaging and implementing GitHub issues.

## Project Structure

- `src/autoloop/` — package source
  - `cli.py` — CLI entry point with subcommand dispatch
  - `config.py` — `AutoLoopConfig` dataclass + TOML loader
  - `claude_runner.py` — shared helper to invoke `claude -p` and parse JSON output
  - `implement_issue.py` — implementation pipeline (branch, implement, verify, review, PR)
  - `triage_issues.py` — triage pipeline (evaluate, decompose, approve/reject)
  - `create_issue.py` — issue template builder and spec parser
  - `auto_close_parent.py` — close parent issues when all sub-issues complete
  - `fix_pr.py` — fix broken PRs (rebase, resolve conflicts, fix lint/test failures)
  - `init.py` — scaffold autoloop onto a new repo
  - `mcp_server.py` — MCP server for remote control
- `tests/` — pytest test suite
- `templates/` — CI workflow template

## Commands

```bash
uv run pytest              # run tests
uv run ruff check          # lint
uv run ruff format --check # format check
uv run ruff format .       # auto-format
```

## Conventions

- Python 3.13+, stdlib only (no external dependencies for core)
- `ruff` for linting and formatting, line length 100
- Every new function gets a test. Every fix gets a regression test.
- Config fields are added to `AutoLoopConfig` dataclass, TOML loader, and config reference in README
- Module-level `REPO_DIR = Path.cwd()` — never use script-relative paths
- Functions that call `gh` or `git` take `cfg` as a parameter or use the module-level `cfg`
- MCP tools use `_spawn()` to fully detach subprocesses
- `subprocess.run` calls to system tools (`systemctl`, `pgrep`) must catch `FileNotFoundError`
- No real person or company names in test data
- Commit messages follow conventional commits: `feat:`, `fix:`, `chore:`, `docs:`
- Always create a feature branch from main before implementing. Never commit directly to main.
