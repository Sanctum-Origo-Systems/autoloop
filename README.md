# autoloop

Config-driven AI pipeline for triaging and implementing GitHub issues.

## Installation

```bash
# Recommended (uv)
uv tool install git+https://github.com/Sanctum-Origo-Systems/autoloop@v0.1.4

# Alternative (pip)
pip install git+https://github.com/Sanctum-Origo-Systems/autoloop@v0.1.4
```

## Quick Start

```bash
cd ~/my-project
autoloop init --repo owner/repo --verify-cmd "pytest"
autoloop triage
autoloop implement
autoloop implement --issue 42
```

## Commands

| Command | Description |
|---------|-------------|
| `autoloop init` | Scaffold autoloop onto a new repo |
| `autoloop plan` | Create issues from a spec file |
| `autoloop triage` | Triage untriaged issues |
| `autoloop implement` | Implement top ready issue |
| `autoloop status` | Show last run, ready issues, timers |
| `autoloop fix-pr` | Fix a PR by rebasing and resolving conflicts |
| `autoloop auto-close-parent` | Close parent when all sub-issues done |
| `autoloop version` | Print installed version |

## Requirements

- Python 3.13+
- GitHub CLI (`gh`) installed and authenticated
- Claude Code CLI (`claude`) installed

## License

Apache 2.0
