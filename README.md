# autoloop

Config-driven AI pipeline that triages GitHub issues, implements them via Claude, and opens PRs. One config file. One command to start.

## Prerequisites

- **Python 3.13+**
- **uv** (recommended): [install](https://docs.astral.sh/uv/getting-started/installation/) or use pip instead
- **Node.js 22+**: required by Claude Code CLI
- **GitHub CLI (`gh`)**: [install](https://cli.github.com/) then run `gh auth login`
- **Claude Code CLI (`claude`)**: `npm install -g @anthropic-ai/claude-code` (requires Claude Max or Pro subscription)
- A GitHub repo with a test command (`pytest`, `npm test`, `make test`, etc.)

## TLDR: First PR in 5 Minutes

```bash
uv tool install git+https://github.com/Sanctum-Origo-Systems/autoloop@main
cd your-repo
autoloop init --repo your-org/your-repo --verify-cmd "npm test"
autoloop doctor                           # verify environment
# Create a GitHub issue with a clear title and acceptance criteria
autoloop triage                           # evaluates and labels the issue
autoloop implement                        # builds it, opens a PR
# Review and merge the PR
```

That's it. Read on for [configuration](#configuration-reference), [scheduling](#running-unattended), and [issue writing tips](#4-create-an-issue).

---

## Platform Support

- **macOS**: fully supported (local mode)
- **Linux**: fully supported (local + unattended/VPS mode)
- **Windows**: not supported

## How It Works

| | Local mode | Unattended mode |
|---|---|---|
| Where | Your machine | VPS / remote server |
| Trigger | You run the command | systemd timer / cron |
| Gate | You review + merge PRs | Same. You're still the human gate |
| Best for | Evaluating autoloop, small projects | Trusted repos, overnight batch runs |

Most users start local. Graduate to unattended when you trust it.

## Installation

### As a CLI tool (recommended)

Replace `<tag>` with the latest version from the [releases page](https://github.com/Sanctum-Origo-Systems/autoloop/tags) (e.g. `v0.4.0`):

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
# uv for a specific version
uv tool install --force git+https://github.com/Sanctum-Origo-Systems/autoloop@v<new-version>

# uv for the latest version
uv tool install --force git+https://github.com/Sanctum-Origo-Systems/autoloop@main

# pip for a specific version
pip install --upgrade git+https://github.com/Sanctum-Origo-Systems/autoloop@v<new-version>

# pip for the latest version
pip install --upgrade git+https://github.com/Sanctum-Origo-Systems/autoloop@main
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

## Quick Start (Local Mode)

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

### Local Mode Notes

- `autoloop init` scaffolds the required `.claude/settings.json` permissions.
  If you skipped init, create one manually (see template).
- Do not run `autoloop implement` while a Claude Code session is open in the
  same project directory. Close it or move the session to a parent folder.

### 2. Verify your environment

```bash
autoloop doctor
```

Checks that everything is set up correctly before your first run:

```
✓ autoloop.toml found and valid
✓ .claude/settings.json found
✓ claude CLI installed (v2.1.160)
✓ claude CLI authenticated
✓ gh CLI installed and authenticated
✓ No active Claude Code session conflict
✓ verify_cmd passes ("pytest" → exit 0)
```

Each check reports pass or fail with a fix suggestion. Exit code 0 if all pass, 1 if any fail. Run this once after `autoloop init` and again if implementations start failing unexpectedly.

### 3. Configure

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

### 4. Create an issue

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

You don't need to list which files to modify. Triage identifies the relevant files automatically from the issue description and codebase. Including file paths is helpful if you know them, but not required.

Issues can be any size. If an issue is too large, triage automatically decomposes it into ordered sub-issues with dependency tracking.

### 5. Triage

```bash
autoloop triage
```

Triage evaluates each untriaged issue and applies a label:

| Label | Meaning |
|-------|---------|
| `ready` | Template complete, small enough to implement |
| `needs-decomposition` | Template complete but too large. Sub-issues are created automatically |
| `rejected` | Missing required fields (autoloop attempts to auto-fix and re-triage once) |
| `needs-human` | Touches protected paths or requires human judgment |

### 6. Implement

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

### 7. Review and merge

Autoloop opens the PR but never merges it. You review and merge. That's the human gate. On merge, the CI workflow cleans up labels and auto-closes parent issues when all sub-issues are complete.

### 8. Fix broken PRs

```bash
autoloop fix-pr 42
```

Detects and fixes:
- **Stale base**: rebases on main
- **Merge conflicts**: rebases, Claude resolves conflicts
- **Lint failures**: runs ruff fix/format, falls back to Claude
- **Test failures**: Claude fixes the code, re-verifies

### 9. Review a PR

```bash
autoloop review-pr 42
```

Runs the review stage on an existing PR without merging:
1. Checks out the PR branch
2. Runs the mutation gate (tests, lint, test-file check)
3. Runs a semantic review via Claude using the `review_model`
4. Posts findings as a PR comment
5. Applies the `needs-human` label if either gate or review fails

Use this to get automated review feedback on PRs that were opened manually or by external contributors. The command never merges — you always make the final call.

## Creating Issues from a Spec

```bash
autoloop plan --from-spec path/to/spec.md
```

Parses a markdown spec or PRD file for `## Task N:` sections and creates a GitHub issue for each one. The legacy `## Enhancement` tag is also supported for backward compatibility.

Example spec format:

```markdown
## Task 1: Add user authentication

Add login and logout endpoints with session management.

**Problem:** No auth on API endpoints.

**File:** `src/api/auth.py`

## Task 2: Add rate limiting

**Problem:** API allows unlimited requests.

**File:** `src/api/middleware.py`
```

The `**File:**` section is optional. Include it if you know which files need modification. The builder will determine the correct files from the description if omitted.

Each section becomes one issue. Triage then handles decomposition and dependency ordering if any issue is too large.

## Running Unattended

Once you trust the pipeline, automate it with timers on a Linux VPS.

### With systemd timers (Linux VPS)

Create a service and timer for both triage and implement. Adjust `OnCalendar` to set the frequency:

```ini
# ~/.config/systemd/user/myapp-triage.service
[Service]
Type=oneshot
WorkingDirectory=/home/user/my-project
ExecStart=/home/user/.local/bin/autoloop triage

# ~/.config/systemd/user/myapp-triage.timer
[Timer]
OnCalendar=*-*-* 00:00:00 UTC    # once daily at midnight
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# ~/.config/systemd/user/myapp-implement.service
[Service]
Type=oneshot
WorkingDirectory=/home/user/my-project
ExecStart=/home/user/.local/bin/autoloop implement --max-issues 5
TimeoutStartSec=7200

# ~/.config/systemd/user/myapp-implement.timer
[Timer]
OnCalendar=*-*-* 02:00:00 UTC    # once daily at 2am
Persistent=true

[Install]
WantedBy=timers.target
```

Common frequencies:
- Daily: `OnCalendar=*-*-* 00:00:00 UTC`
- Every 6 hours: `OnCalendar=*-*-* 00/6:00:00 UTC`
- Every 2 hours: `OnCalendar=*-*-* 00/2:00:00 UTC`

```bash
systemctl --user daemon-reload
systemctl --user enable --now myapp-triage.timer
systemctl --user enable --now myapp-implement.timer

# Enable lingering so timers run when you're not logged in
loginctl enable-linger $USER

# Verify timers are active
systemctl --user list-timers | grep myapp
```

The `timer_prefix` in `autoloop.toml` controls which timers `autoloop status` looks for. Name your timers with your app's prefix. It doesn't have to be "autoloop":

```toml
# If your timers are named myapp-triage.timer / myapp-implement.timer:
timer_prefix = "myapp"

# Default matches autoloop-triage.timer / autoloop-implement.timer:
# timer_prefix = "autoloop"
```

This supports multiple repos on the same VPS. Each repo has its own `autoloop.toml` with a distinct prefix.

### Mobile workflow

Requires a Claude Code session running in tmux on your VPS. Start it once:

```bash
ssh your-vps
cd ~/your-project
tmux new -s claude
claude
# Detach: Ctrl+B then D
```

The session persists after you disconnect. Reconnect anytime with `tmux attach`.

Configure the autoloop MCP server by adding to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "autoloop-mcp": {
      "command": "autoloop-mcp",
      "autoApprove": ["autoloop_status", "autoloop_triage", "autoloop_implement", "autoloop_fix_pr"]
    }
  }
}
```

Install the MCP server on the VPS with `uv tool install "autoloop[mcp] @ git+https://github.com/Sanctum-Origo-Systems/autoloop@<tag>"`, then restart the Claude Code session.

**Enable remote control:** In the Claude Code session on the VPS, run `/login` to authenticate, then enable remote connections in settings. This allows the Claude mobile app to send messages to the VPS session.

Two mobile apps, two roles:

- **GitHub mobile app** ([iOS](https://apps.apple.com/app/github/id1477376905) / [Android](https://play.google.com/store/apps/details?id=com.github.android)): review diffs, approve, and merge PRs
- **Claude mobile app** ([iOS](https://apps.apple.com/app/claude/id6473753684) / [Android](https://play.google.com/store/apps/details?id=com.anthropic.claude)): tap **Code** at the bottom, select your VPS session from the list, and use the chat to invoke autoloop commands

Example workflow from your phone:

1. **GitHub app**: review and merge a PR
2. **Claude app**: "Implement the next ready issue". Autoloop picks the top issue and starts working
3. **Claude app**: "Check autoloop status". See progress, ready issue count, next timer
4. **Claude app**: "Fix PR 42". Rebases and resolves conflicts or failing checks

## Configuration Reference

| Field | Default | Description |
|-------|---------|-------------|
| `repo` | (required) | GitHub `owner/repo` (required) |
| `triage_model` | `sonnet` | Claude model for triage |
| `impl_model` | `claude-opus-4-6[1m]` | Claude model for implementation |
| `review_model` | (defaults to `impl_model`) | Claude model for semantic PR review. Falls back to `impl_model` when omitted |
| `impl_timeout` | `900` | Implementation timeout (seconds) |
| `triage_timeout` | `90` | Triage timeout (seconds) |
| `test_timeout` | `120` | Test command timeout (seconds) |
| `pr_reviewer` | (none) | GitHub username assigned to PRs |
| `max_retries` | `3` | Retry attempts per issue |
| `max_story_points` | `3` | Issues above this are decomposed |
| `verify_cmd` | `uv run pytest` | Command to validate implementation |
| `lint_command` | `uv run ruff check && uv run ruff format --check` | Lint check command |
| `timer_prefix` | `autoloop` | Systemd timer prefix for status detection (use your app name, e.g. `myapp`) |
| `protected_paths` | `["autoloop/"]` | Paths the bot must never modify |
| `test_gate_skip_types` | `["refactor", "docs", "chore"]` | Issue types that skip the "must add test files" verification check |
| `triage_labels` | `["ready", "rejected", ...]` | Labels that indicate an issue has been triaged |

All fields can be overridden by environment variables (e.g. `AUTOLOOP_IMPL_MODEL`, `AUTOLOOP_TIMEOUT`).

## Commands

| Command | Description |
|---------|-------------|
| `autoloop init` | Scaffold autoloop onto a new repo |
| `autoloop doctor` | Verify environment and configuration |
| `autoloop plan` | Create issues from a spec file |
| `autoloop triage` | Triage untriaged issues |
| `autoloop implement` | Implement top ready issue |
| `autoloop fix-pr` | Fix a PR (rebase, resolve conflicts, fix checks) |
| `autoloop review-pr` | Review a PR (mutation gate + semantic review, no merge) |
| `autoloop status` | Show last run, ready issues, timers |
| `autoloop auto-close-parent` | Close parent when all sub-issues done |
| `autoloop version` | Print installed version |

## License

Apache 2.0
