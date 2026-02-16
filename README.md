# git-fleet

Command multiple Git repositories like a fleet admiral.

A CLI tool for managing multiple Git repositories at once - fetch, status check, pull, push, and sync operations across your entire development directory with parallel execution.

## Installation

```bash
# Install with uv (recommended)
uv tool install git-fleet

# Or install from GitHub
uv tool install git+https://github.com/asatamax/git-fleet.git

# Or install with pip
pip install git-fleet
```

## Usage

```bash
# Show status of all repos in current directory
git-fleet status

# Show status of all repos in a specific directory
git-fleet status ~/Development

# Fetch all repos
git-fleet fetch ~/Development

# Pull repos that are behind (safe only - skips diverged/dirty)
git-fleet pull ~/Development

# Push repos that are ahead
git-fleet push ~/Development

# Full sync: fetch → pull → push
git-fleet sync ~/Development

# List all discovered repositories
git-fleet list ~/Development

# Check Git identity (user.name/email) for all repos
git-fleet who ~/Development
```

## Multi-Root Support

Manage repositories across multiple directory trees using a roots file:

```bash
# Create a roots file
cat > ~/.config/git-fleet/roots << 'EOF'
# Work repositories
$HOME/work/repos
$DEV_ROOT/projects

# Personal projects
~/personal
EOF
```

### Auto-Resolution

When `--roots` is not specified, git-fleet automatically looks for a roots file in this order:

1. `$GIT_FLEET_ROOTS` environment variable (path to roots file)
2. `~/.config/git-fleet/roots` (XDG-compliant)
3. `~/.git-fleet-roots` (legacy)

If a roots file is found, multi-root mode activates automatically:

```bash
# These are equivalent (when ~/.config/git-fleet/roots exists)
git-fleet status
git-fleet status --roots ~/.config/git-fleet/roots

# Override with explicit --roots
git-fleet status --roots /path/to/other/roots

# Or use environment variable (useful in CI/CD)
export GIT_FLEET_ROOTS=~/work-roots
git-fleet status
```

## Options

| Option | Short | Description |
|--------|-------|-------------|
| `--json` | `-j` | Output as JSON (recommended for AI agents) |
| `--sequential` | `-s` | Run operations sequentially instead of parallel |
| `--dry-run` | `-n` | Preview operations without executing |
| `--roots` | `-r` | Path to file containing repository root paths |
| `--no-fetch` | | Skip fetching before status check |
| `--all` | `-a` | Pull/push all repositories, not just those needing it |
| `--force` | `-f` | Pull even if there's conflict risk |
| `--paths` | `-p` | Output only paths (for piping to fzf etc.) |
| `--schema` | | Output MCP-compatible tool schema for AI agents |

## Features

- **Parallel execution**: Operations run in parallel by default for speed
- **Safety checks**: Pull skips diverged or dirty repositories unless `--force`
- **Identity verification**: `who` command shows Git user.name/email configuration
- **Multi-root support**: Manage repos across multiple directory trees
- **AI-friendly**: JSON output and MCP-compatible schema for AI agents
- **fzf integration**: `--paths` option for easy piping to fzf

## Shell Integration

### Recommended Aliases

Add these to your `~/.zshrc` or `~/.bashrc`:

```bash
# Quick access to common operations
alias gfst='git-fleet status'
alias gfsy='git-fleet sync'
alias gfps='git-fleet push'
alias gfpl='git-fleet pull'
alias gfwho='git-fleet who'
alias gfdiff='git-fleet diff'
```

### Fuzzy Repository Jumper (`gfcd`)

Jump to any managed repository with fuzzy search powered by [fzf](https://github.com/junegunn/fzf):

```bash
# Add to ~/.zshrc or ~/.bashrc
gfcd() {
  local repos dir
  repos=$(git-fleet list --paths)

  if [[ -n "$1" ]]; then
    local matches count
    matches=$(echo "$repos" | grep -i "$1")
    count=$(echo "$matches" | grep -c .)

    if (( count == 1 )); then
      cd "$matches"
      return
    fi
  fi

  dir=$(echo "$repos" | fzf \
    --query="${1:-}" \
    --preview 'git -C {} log --oneline --graph --decorate --color=always -10' \
    --preview-window=right:50%) || return 0
  [[ -n "$dir" ]] && cd "$dir"
}
```

**Usage:**

```bash
gfcd              # Open fzf with all repositories
gfcd myproject    # Exact match → instant cd, multiple matches → fzf with query
```

**Requirements:** [fzf](https://github.com/junegunn/fzf) (`brew install fzf`)

## Status Icons

| Icon | Meaning |
|------|---------|
| ✓ | Clean - in sync with remote |
| ⬆ N | Ahead - N commits to push |
| ⬇ N | Behind - N commits to pull |
| ⬆N ⬇M | Diverged - needs manual merge |
| ⚠ | Conflict risk - diverged or dirty + behind |

## Working Tree Status

| Symbol | Meaning |
|--------|---------|
| +N | N staged changes |
| ~N | N unstaged changes |
| ?N | N untracked files |
| clean | No local changes |

## License

MIT
