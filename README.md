# autoloop

Config-driven AI pipeline that triages GitHub issues, implements them via Claude, and opens PRs — all from a single config file.

## Installation

### As a CLI tool (recommended)

Replace `<tag>` with the latest version from the [releases page](https://github.com/Sanctum-Origo-Systems/autoloop/tags) (e.g. `v0.1.7`):

```bash
uv tool install git+https://github.com/Sanctum-Origo-Systems/autoloop@<tag>
```

Verify it worked:

```bash
autoloop version
```

### Alternative: pip

```bash
pip install git+https://github.com/Sanctum-Origo-Systems/autoloop@<tag>
```

### Upgrading

```bash
# uv
uv tool install --force git+https://github.com/Sanctum-Origo-Systems/autoloop@v<new-version>

# pip
pip install --upgrade git+https://github.com/Sanctum-Origo-Systems/autoloop@v<new-version>
```

Verify: `autoloop version`

If running on a VPS with systemd timers, reinstall there too and restart any Claude Code sessions in tmux to pick up the new MCP tools.

### For contributors

```bash
git clone https://github.com/Sanctum-Origo-Systems/autoloop.git
cd autoloop
uv sync
uv run autoloop version
```

Contributions are currently limited to collaborators. A public issue board for ideas and feature requests will be available soon. In the meantime, reach out to andywidjaja@gmail.com.

## Prerequisites

- **Python 3.13+**
- **GitHub CLI (`gh`)** — [install](https://cli.github.com/) then run `gh auth login`
- **Claude Code CLI (`claude`)** — [install](https://docs.anthropic.com/en/docs/claude-code/overview) (requires Claude Max or Pro subscription)
- A GitHub repo with a test command (`pytest`, `npm test`, `make test`, etc.)

## Quick Start

### 1. Initialize your repo

```bash
cd ~/my-project
autoloop init --repo owner/repo --reviewer your-github-username --verify-cmd "pytest"
```

This creates three files:

| File | Purpose |
|------|---------|
| `autoloop.toml` | Pipeline configuration (models, timeouts, verify command) |
| `.github/workflows/autoloop-cleanup.yml` | CI workflow that cleans labels and auto-closes parent issues on merge |
| `.gitignore` | Adds `autoloop/run_history.jsonl` |

It also creates GitHub labels (`ready`, `rejected`, `in-progress`, etc.) used by the triage system.

Commit the generated files:

```bash
git add autoloop.toml .github/workflows/autoloop-cleanup.yml .gitignore
git commit -m "feat: add autoloop pipeline"
git push
```

### 2. Configure

Review `autoloop.toml` and adjust for your project. Key fields:

```toml
repo = "owner/repo"              # Your GitHub repo
verify_cmd = "pytest"             # Command that validates the implementation
lint_command = "ruff check && ruff format --check"
impl_model = "claude-opus-4-6[1m]"  # Model for implementation
triage_model = "sonnet"           # Model for triage (lighter, cheaper)
max_retries = 3                   # Retry attempts per issue
protected_paths = ["autoloop.toml"]  # Files the bot must never modify
```

### 3. Create an issue

Write a GitHub issue with clear structure. Autoloop works best with specific, testable issues.

**Good issue:**
```
## Summary
Add a --verbose flag to the CLI that prints each processed file

## Type
feature

## Expected Behavior
When --verbose is passed, each file name is printed to stdout as it's processed.
Without --verbose, only the summary count is shown.

## Acceptance Criteria
- [ ] --verbose flag accepted by argument parser
- [ ] Each file printed on its own line when flag is set
- [ ] Default behavior unchanged without the flag
```

**Bad issue (will be rejected by triage):**
```
Make the CLI better. It should show more stuff.
```

You don't need to list which files to modify — triage identifies the relevant files automatically from the issue description and codebase. Including file paths is helpful if you know them, but not required.

Issues can be any size. If an issue is too large, triage automatically decomposes it into ordered sub-issues with dependency tracking.

### 4. Triage

```bash
autoloop triage
```

Triage evaluates each untriaged issue and applies a label:

| Label | Meaning |
|-------|---------|
| `ready` | Template complete, small enough to implement |
| `needs-decomposition` | Template complete but too large — sub-issues are created automatically |
| `rejected` | Missing required fields (autoloop attempts to auto-fix and re-triage once) |
| `needs-human` | Touches protected paths or requires human judgment |

### 5. Implement

```bash
autoloop implement              # implements the top ready issue
autoloop implement --issue 42   # implements a specific issue
autoloop implement --max-issues 5  # implements up to 5 issues in sequence
```

For each issue, autoloop:
1. Creates a feature branch
2. Runs Claude to implement the changes
3. Runs your `verify_cmd` and lint checks
4. Reviews the implementation for quality
5. Retries on failure (up to `max_retries`)
6. Opens a PR with run stats (duration, cost, tokens)

### 6. Review and merge

Autoloop opens the PR but never merges it. You review and merge — that's the human gate. On merge, the CI workflow cleans up labels and auto-closes parent issues when all sub-issues are complete.

### 7. Fix broken PRs

```bash
autoloop fix-pr 42
```

Detects and fixes:
- **Stale base** — rebases on main
- **Merge conflicts** — rebases, Claude resolves conflicts
- **Lint failures** — runs ruff fix/format, falls back to Claude
- **Test failures** — Claude fixes the code, re-verifies

## Creating Issues from a Spec

```bash
autoloop plan --from-spec path/to/spec.md
```

Parses a markdown spec or PRD file for `## Enhancement N:` sections and creates a GitHub issue for each one.

> **Note:** The parser currently uses `## Enhancement` as the section marker. This is a legacy naming convention — sections can describe features, bug fixes, refactors, or any task. A future version will support a more semantically correct marker like `## Task`.

Example spec format:

```markdown
## Enhancement 1: Add user authentication

Add login and logout endpoints with session management.

**Problem:** No auth on API endpoints.

**File:** `src/api/auth.py`

## Enhancement 2: Add rate limiting

**Problem:** API allows unlimited requests.

**File:** `src/api/middleware.py`
```

The `**File:**` section is optional — include it if you know which files need modification, but the builder will determine the correct files from the description if omitted.

Each section becomes one issue. Triage then handles decomposition and dependency ordering if any issue is too large.

## Running Unattended

### With systemd timers (Linux VPS)

```ini
# ~/.config/systemd/user/autoloop-triage.service
[Service]
Type=oneshot
WorkingDirectory=/home/user/my-project
ExecStart=/home/user/.local/bin/autoloop triage

# ~/.config/systemd/user/autoloop-triage.timer
[Timer]
OnCalendar=*-*-* 00:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now autoloop-triage.timer
```

The `timer_prefix` in `autoloop.toml` controls which timers `autoloop status` looks for. Name your timers with your app's prefix — it doesn't have to be "autoloop":

```toml
# If your timers are named myapp-triage.timer / myapp-implement.timer:
timer_prefix = "myapp"

# Default matches autoloop-triage.timer / autoloop-implement.timer:
# timer_prefix = "autoloop"
```

This supports multiple repos on the same VPS — each repo has its own `autoloop.toml` with a distinct prefix.

### Mobile workflow

With a Claude Code session running in tmux on your VPS:
1. Review and merge a PR from your phone
2. "Implement the next ready issue" — triggers `autoloop implement`
3. "Check autoloop status" — see the new PR link
4. "Fix PR 42" — rebases and resolves any issues

## Configuration Reference

| Field | Default | Description |
|-------|---------|-------------|
| `repo` | — | GitHub `owner/repo` (required) |
| `triage_model` | `sonnet` | Claude model for triage |
| `impl_model` | `claude-opus-4-6[1m]` | Claude model for implementation |
| `impl_timeout` | `900` | Implementation timeout (seconds) |
| `triage_timeout` | `90` | Triage timeout (seconds) |
| `test_timeout` | `120` | Test command timeout (seconds) |
| `pr_reviewer` | — | GitHub username assigned to PRs |
| `max_retries` | `3` | Retry attempts per issue |
| `max_story_points` | `2` | Issues above this are decomposed |
| `verify_cmd` | `uv run pytest` | Command to validate implementation |
| `lint_command` | `uv run ruff check && uv run ruff format --check` | Lint check command |
| `timer_prefix` | `autoloop` | Systemd timer prefix for status detection (use your app name, e.g. `myapp`) |
| `protected_paths` | `["autoloop/"]` | Paths the bot must never modify |
| `triage_labels` | `["ready", "rejected", ...]` | Labels that indicate an issue has been triaged |

All fields can be overridden by environment variables (e.g. `AUTOLOOP_IMPL_MODEL`, `AUTOLOOP_TIMEOUT`).

## Commands

| Command | Description |
|---------|-------------|
| `autoloop init` | Scaffold autoloop onto a new repo |
| `autoloop plan` | Create issues from a spec file |
| `autoloop triage` | Triage untriaged issues |
| `autoloop implement` | Implement top ready issue |
| `autoloop fix-pr` | Fix a PR (rebase, resolve conflicts, fix checks) |
| `autoloop status` | Show last run, ready issues, timers |
| `autoloop auto-close-parent` | Close parent when all sub-issues done |
| `autoloop version` | Print installed version |

## License

Apache 2.0
