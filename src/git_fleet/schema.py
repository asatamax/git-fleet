"""MCP-compatible tool schema for AI agents."""

from __future__ import annotations

from ._version import __version__


def get_tool_schema() -> dict:
    """Generate MCP-compatible tool schema for AI agents."""
    return {
        "name": "git-fleet",
        "version": __version__,
        "description": "Command multiple Git repositories like a fleet admiral. Manage, monitor, and synchronize all Git repositories under a directory tree with parallel execution. Supports multi-root configuration with auto-resolution (env var, XDG, legacy) or explicit --roots option.",
        "usage": "git-fleet <command> [path] [options]",
        "tools": [
            {
                "name": "status",
                "description": "Show status of all Git repositories. Fetches from remotes (unless --no-fetch) and displays sync status (ahead/behind/diverged), working tree status (staged/unstaged/untracked), and last commit date. Use this to get an overview of all repositories.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories (default: current directory)",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON for machine parsing",
                            "default": False,
                        },
                        "no_fetch": {
                            "type": "boolean",
                            "description": "Skip fetching from remotes (faster but may show stale data)",
                            "default": False,
                        },
                        "sequential": {
                            "type": "boolean",
                            "description": "Run sequentially instead of parallel",
                            "default": False,
                        },
                        "include_no_remote": {
                            "type": "boolean",
                            "description": "Include repositories with no configured remotes",
                            "default": False,
                        },
                        "include_detached": {
                            "type": "boolean",
                            "description": "Include repositories with detached HEAD (e.g. SPM checkouts)",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "repositories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "name": {"type": "string"},
                                    "branch": {"type": "string"},
                                    "sync_status": {
                                        "type": "string",
                                        "enum": [
                                            "clean",
                                            "ahead",
                                            "behind",
                                            "diverged",
                                            "no_upstream",
                                            "detached",
                                            "no_remote",
                                            "error",
                                        ],
                                    },
                                    "ahead_count": {"type": "integer"},
                                    "behind_count": {"type": "integer"},
                                    "staged_count": {"type": "integer"},
                                    "unstaged_count": {"type": "integer"},
                                    "untracked_count": {"type": "integer"},
                                    "working_tree_status": {
                                        "type": "string",
                                        "enum": ["clean", "dirty"],
                                    },
                                    "needs_push": {"type": "boolean"},
                                    "needs_pull": {"type": "boolean"},
                                    "has_conflict_risk": {"type": "boolean"},
                                    "last_commit_date": {"type": "string", "format": "date-time"},
                                },
                            },
                        },
                        "summary": {
                            "type": "object",
                            "properties": {
                                "total": {"type": "integer"},
                                "clean": {"type": "integer"},
                                "need_push": {"type": "integer"},
                                "need_pull": {"type": "integer"},
                                "diverged": {"type": "integer"},
                                "dirty": {"type": "integer"},
                                "conflict_risk": {"type": "integer"},
                                "errors": {"type": "integer"},
                            },
                        },
                    },
                },
                "examples": [
                    {
                        "description": "Check status of all repos in ~/Development",
                        "command": "git-fleet status ~/Development --json",
                    },
                    {
                        "description": "Check status across all configured roots (auto-resolved)",
                        "command": "git-fleet status --json",
                    },
                    {
                        "description": "Quick status without fetching",
                        "command": "git-fleet status --json --no-fetch",
                    },
                ],
            },
            {
                "name": "who",
                "description": "Show Git identity (user.name and user.email) for all repositories. Useful for verifying correct email configuration before committing. Displays config source: local (.git/config), global (~/.gitconfig), included (includeIf conditional includes), or system (/etc/gitconfig).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories (default: current directory)",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON for machine parsing",
                            "default": False,
                        },
                        "sequential": {
                            "type": "boolean",
                            "description": "Run sequentially instead of parallel",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "global_identity": {
                            "type": "object",
                            "properties": {
                                "user_name": {"type": "string"},
                                "user_email": {"type": "string"},
                            },
                        },
                        "repositories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "name": {"type": "string"},
                                    "user_name": {"type": "string"},
                                    "user_email": {"type": "string"},
                                    "is_local_override": {"type": "boolean"},
                                    "source": {
                                        "type": "string",
                                        "enum": [
                                            "local",
                                            "global",
                                            "included",
                                            "system",
                                            "unknown",
                                        ],
                                        "description": "Config source: local (.git/config), global (~/.gitconfig), included (includeIf), system (/etc/gitconfig)",
                                    },
                                    "source_file": {
                                        "type": "string",
                                        "description": "Full path to the config file",
                                    },
                                },
                            },
                        },
                        "summary": {
                            "type": "object",
                            "properties": {
                                "total": {"type": "integer"},
                                "by_source": {
                                    "type": "object",
                                    "description": "Count by source type (local, global, included, etc.)",
                                },
                                "using_global": {"type": "integer"},
                                "local_override": {"type": "integer"},
                            },
                        },
                    },
                },
                "examples": [
                    {
                        "description": "Check identity for all repos across configured roots",
                        "command": "git-fleet who --json",
                    },
                    {
                        "description": "Check identity for repos in a specific directory",
                        "command": "git-fleet who ~/Development --json",
                    },
                ],
            },
            {
                "name": "diff",
                "description": "Show file-level changes (staged, unstaged, untracked) across all repositories. Only shows dirty repositories by default. Use --all to include clean repos.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories (default: current directory)",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON for machine parsing",
                            "default": False,
                        },
                        "all": {
                            "type": "boolean",
                            "description": "Show all repositories including clean ones",
                            "default": False,
                        },
                        "sequential": {
                            "type": "boolean",
                            "description": "Run sequentially instead of parallel",
                            "default": False,
                        },
                        "include_no_remote": {
                            "type": "boolean",
                            "description": "Include repositories with no configured remotes",
                            "default": False,
                        },
                        "include_detached": {
                            "type": "boolean",
                            "description": "Include repositories with detached HEAD (e.g. SPM checkouts)",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "repositories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "name": {"type": "string"},
                                    "branch": {"type": "string"},
                                    "staged_files": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "status": {
                                                    "type": "string",
                                                    "description": "Change type: M(odified), A(dded), D(eleted), R(enamed), C(opied)",
                                                },
                                                "file": {"type": "string"},
                                            },
                                        },
                                    },
                                    "unstaged_files": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "status": {"type": "string"},
                                                "file": {"type": "string"},
                                            },
                                        },
                                    },
                                    "untracked_files": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "staged_count": {"type": "integer"},
                                    "unstaged_count": {"type": "integer"},
                                    "untracked_count": {"type": "integer"},
                                },
                            },
                        },
                        "summary": {
                            "type": "object",
                            "properties": {
                                "total_repos": {"type": "integer"},
                                "dirty_repos": {"type": "integer"},
                                "total_staged": {"type": "integer"},
                                "total_unstaged": {"type": "integer"},
                                "total_untracked": {"type": "integer"},
                            },
                        },
                    },
                },
                "examples": [
                    {
                        "description": "Show file-level changes across configured roots",
                        "command": "git-fleet diff --json",
                    },
                    {
                        "description": "Show changes in a specific directory",
                        "command": "git-fleet diff ~/Development --json",
                    },
                ],
            },
            {
                "name": "fetch",
                "description": "Fetch all remotes for all repositories. Updates remote tracking branches without modifying local branches.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON",
                            "default": False,
                        },
                        "sequential": {
                            "type": "boolean",
                            "description": "Run sequentially instead of parallel",
                            "default": False,
                        },
                        "include_no_remote": {
                            "type": "boolean",
                            "description": "Include repositories with no configured remotes",
                            "default": False,
                        },
                        "include_detached": {
                            "type": "boolean",
                            "description": "Include repositories with detached HEAD (e.g. SPM checkouts)",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "examples": [
                    {
                        "description": "Fetch all repos",
                        "command": "git-fleet fetch ~/Development --json",
                    },
                    {
                        "description": "Fetch across all configured roots",
                        "command": "git-fleet fetch --json",
                    },
                ],
            },
            {
                "name": "pull",
                "description": "Pull repositories that are behind remote. Default smart mode checks file-level overlap for conflict-risk repos and pulls if files don't overlap. Use --safe to skip all conflict-risk repos, or --force to pull everything.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON",
                            "default": False,
                        },
                        "all": {
                            "type": "boolean",
                            "description": "Pull all repositories, not just those behind",
                            "default": False,
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Show what would be pulled without actually pulling",
                            "default": False,
                        },
                        "force": {
                            "type": "boolean",
                            "description": "Pull all regardless of conflict risk",
                            "default": False,
                        },
                        "safe": {
                            "type": "boolean",
                            "description": "Skip all conflict-risk repos without file-level check",
                            "default": False,
                        },
                        "sequential": {
                            "type": "boolean",
                            "description": "Run sequentially instead of parallel",
                            "default": False,
                        },
                        "include_no_remote": {
                            "type": "boolean",
                            "description": "Include repositories with no configured remotes",
                            "default": False,
                        },
                        "include_detached": {
                            "type": "boolean",
                            "description": "Include repositories with detached HEAD (e.g. SPM checkouts)",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "examples": [
                    {
                        "description": "Pull all repos that are behind (smart mode)",
                        "command": "git-fleet pull ~/Development --json",
                    },
                    {
                        "description": "Dry-run pull across configured roots",
                        "command": "git-fleet pull --json --dry-run",
                    },
                ],
            },
            {
                "name": "push",
                "description": "Push repositories that are ahead of remote. By default, only pushes repositories that have unpushed commits.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON",
                            "default": False,
                        },
                        "all": {
                            "type": "boolean",
                            "description": "Push all repositories, not just those ahead",
                            "default": False,
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Show what would be pushed without actually pushing",
                            "default": False,
                        },
                        "sequential": {
                            "type": "boolean",
                            "description": "Run sequentially instead of parallel",
                            "default": False,
                        },
                        "include_no_remote": {
                            "type": "boolean",
                            "description": "Include repositories with no configured remotes",
                            "default": False,
                        },
                        "include_detached": {
                            "type": "boolean",
                            "description": "Include repositories with detached HEAD (e.g. SPM checkouts)",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "examples": [
                    {
                        "description": "Push all repos that are ahead",
                        "command": "git-fleet push ~/Development --json",
                    },
                    {
                        "description": "Dry-run push across configured roots",
                        "command": "git-fleet push --json --dry-run",
                    },
                ],
            },
            {
                "name": "sync",
                "description": "Full synchronization: fetch all, pull (smart), then push. This is the recommended command for routine synchronization. Smart mode checks file-level overlap for conflict-risk repos.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON",
                            "default": False,
                        },
                        "dry_run": {
                            "type": "boolean",
                            "description": "Show what would happen without actually doing it",
                            "default": False,
                        },
                        "sequential": {
                            "type": "boolean",
                            "description": "Run sequentially instead of parallel",
                            "default": False,
                        },
                        "include_no_remote": {
                            "type": "boolean",
                            "description": "Include repositories with no configured remotes",
                            "default": False,
                        },
                        "include_detached": {
                            "type": "boolean",
                            "description": "Include repositories with detached HEAD (e.g. SPM checkouts)",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "examples": [
                    {
                        "description": "Sync all repositories",
                        "command": "git-fleet sync ~/Development --json",
                    },
                    {
                        "description": "Dry-run sync across configured roots",
                        "command": "git-fleet sync --json --dry-run",
                    },
                ],
            },
            {
                "name": "list",
                "description": "List all discovered Git repositories under the given path. Lightweight operation that doesn't fetch or analyze status. Use --remote to include remote URLs and protocols.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "paths": {
                            "type": "boolean",
                            "description": "Output only paths (one per line, for piping to fzf etc.)",
                            "default": False,
                        },
                        "remote": {
                            "type": "boolean",
                            "description": "Include remote URLs and protocols in output",
                            "default": False,
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "outputSchema": {
                    "type": "object",
                    "description": "When --remote is used, each repository includes remotes array",
                    "properties": {
                        "repositories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "name": {"type": "string"},
                                    "remotes": {
                                        "type": "array",
                                        "description": "Only present when --remote is used",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "name": {
                                                    "type": "string",
                                                    "description": "Remote name (e.g., origin, upstream)",
                                                },
                                                "fetch_url": {"type": "string"},
                                                "push_url": {"type": "string"},
                                                "protocol": {
                                                    "type": "string",
                                                    "enum": [
                                                        "ssh",
                                                        "https",
                                                        "http",
                                                        "git",
                                                        "file",
                                                        "unknown",
                                                    ],
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
                "examples": [
                    {
                        "description": "List all repos",
                        "command": "git-fleet list ~/Development --json",
                    },
                    {
                        "description": "List all repos across configured roots",
                        "command": "git-fleet list --json",
                    },
                    {
                        "description": "List repos with remote URLs (recommended for AI agents)",
                        "command": "git-fleet list --remote --json",
                    },
                    {
                        "description": "Output paths only for fzf integration",
                        "command": "git-fleet list --paths",
                    },
                ],
            },
            {
                "name": "remote",
                "description": "Show remote URLs and protocols for all repositories. Displays remote name, fetch/push URLs, and connection protocol (ssh, https, http, git, file).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Root path to scan for repositories (default: current directory)",
                            "default": ".",
                        },
                        "roots": {
                            "type": "string",
                            "description": "Path to roots file (overrides auto-resolution). Auto-resolved from: $GIT_FLEET_ROOTS env var → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
                        },
                        "json": {
                            "type": "boolean",
                            "description": "Output as JSON for machine parsing",
                            "default": False,
                        },
                        "sequential": {
                            "type": "boolean",
                            "description": "Run sequentially instead of parallel",
                            "default": False,
                        },
                    },
                    "required": [],
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "repositories": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "name": {"type": "string"},
                                    "remotes": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "name": {
                                                    "type": "string",
                                                    "description": "Remote name (e.g., origin, upstream)",
                                                },
                                                "fetch_url": {"type": "string"},
                                                "push_url": {"type": "string"},
                                                "protocol": {
                                                    "type": "string",
                                                    "enum": [
                                                        "ssh",
                                                        "https",
                                                        "http",
                                                        "git",
                                                        "file",
                                                        "unknown",
                                                    ],
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                        "summary": {
                            "type": "object",
                            "properties": {
                                "total_repos": {"type": "integer"},
                                "total_remotes": {"type": "integer"},
                                "by_protocol": {
                                    "type": "object",
                                    "description": "Count by protocol type (ssh, https, etc.)",
                                },
                            },
                        },
                    },
                },
                "examples": [
                    {
                        "description": "Show remotes for all repos across configured roots",
                        "command": "git-fleet remote --json",
                    },
                    {
                        "description": "Show remotes for repos in a specific directory",
                        "command": "git-fleet remote ~/Development --json",
                    },
                ],
            },
        ],
        "globalOptions": {
            "--json, -j": "Output in JSON format (recommended for AI agents)",
            "--sequential, -s": "Run operations sequentially instead of parallel",
            "--dry-run, -n": "Preview operations without executing (available for pull/push/sync)",
            "--roots, -r": "Path to roots file (overrides auto-resolution)",
            "--include-no-remote": "Include repositories with no configured remotes (status/fetch/pull/push/sync/diff)",
            "--include-detached": "Include repositories with detached HEAD (status/fetch/pull/push/sync/diff)",
        },
        "rootsFileAutoResolution": {
            "description": "When --roots is not specified, git-fleet automatically searches for a roots file",
            "priority": [
                "$GIT_FLEET_ROOTS environment variable (path to roots file)",
                "~/.config/git-fleet/roots (XDG-compliant)",
                "~/.git-fleet-roots (legacy fallback)",
            ],
            "fallback": "If no roots file is found, operates on current directory or specified path (single-root mode)",
        },
        "rootsFileFormat": {
            "description": "The roots file contains one repository root path per line",
            "features": [
                "Comments start with #",
                "Empty lines are ignored",
                "Environment variables: $HOME, $DEV_ROOT, ${VAR}",
                "Tilde expansion: ~/path",
                "Invalid/non-existent paths are silently skipped",
            ],
            "example": "# ~/.config/git-fleet/roots\n$HOME/work/repos\n$DEV_ROOT/projects\n~/personal",
        },
        "notes": [
            "All commands support --json for machine-readable output",
            "Parallel execution is the default for performance",
            "Roots file is auto-resolved: $GIT_FLEET_ROOTS → ~/.config/git-fleet/roots → ~/.git-fleet-roots",
            "Smart pull (default) checks file-level overlap before skipping conflict-risk repos; use --safe for conservative mode or --force to skip all checks",
            "Use 'status --json' first to understand the current state before making changes",
            "Use 'who' to verify Git identity configuration before committing",
            "Use 'remote --json' or 'list --remote --json' to get remote URLs and protocols",
            "Sync commands exclude no-remote and detached HEAD repos by default; use --include-no-remote and --include-detached to include them",
        ],
    }
