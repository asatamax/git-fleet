"""
git-fleet: Command multiple Git repositories like a fleet admiral.

A tool for managing multiple Git repositories at once - fetch, status check,
pull, push, and sync operations across your entire development directory.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ._version import __version__
from .formatters import OutputFormatter
from .schema import get_tool_schema

# =============================================================================
# Domain Models
# =============================================================================


class SyncStatus(StrEnum):
    """Repository sync status with remote."""

    CLEAN = "clean"
    AHEAD = "ahead"
    BEHIND = "behind"
    DIVERGED = "diverged"
    NO_UPSTREAM = "no_upstream"
    DETACHED = "detached"
    NO_REMOTE = "no_remote"
    ERROR = "error"


class WorkingTreeStatus(StrEnum):
    """Working tree status."""

    CLEAN = "clean"
    DIRTY = "dirty"


class PullMode(StrEnum):
    """Pull safety mode."""

    SMART = "smart"  # default: file-level overlap check for conflict-risk repos
    SAFE = "safe"  # skip all conflict-risk repos
    FORCE = "force"  # pull everything regardless


@dataclass
class RepositoryStatus:
    """Complete status of a Git repository."""

    path: Path
    name: str
    branch: str = ""
    remote_branch: str = ""
    sync_status: SyncStatus = SyncStatus.CLEAN
    ahead_count: int = 0
    behind_count: int = 0
    staged_count: int = 0
    unstaged_count: int = 0
    untracked_count: int = 0
    last_commit_date: datetime | None = None
    error_message: str = ""

    @property
    def working_tree_status(self) -> WorkingTreeStatus:
        """Check if working tree is dirty."""
        if self.staged_count > 0 or self.unstaged_count > 0 or self.untracked_count > 0:
            return WorkingTreeStatus.DIRTY
        return WorkingTreeStatus.CLEAN

    @property
    def needs_push(self) -> bool:
        return self.sync_status in (SyncStatus.AHEAD, SyncStatus.DIVERGED)

    @property
    def needs_pull(self) -> bool:
        return self.sync_status in (SyncStatus.BEHIND, SyncStatus.DIVERGED)

    @property
    def is_diverged(self) -> bool:
        return self.sync_status == SyncStatus.DIVERGED

    @property
    def has_conflict_risk(self) -> bool:
        """Check if there's a risk of conflict (diverged or dirty + behind)."""
        if self.is_diverged:
            return True
        if self.needs_pull and self.working_tree_status == WorkingTreeStatus.DIRTY:
            return True
        return False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON output."""
        return {
            "path": str(self.path),
            "name": self.name,
            "branch": self.branch,
            "remote_branch": self.remote_branch,
            "sync_status": self.sync_status.value,
            "ahead_count": self.ahead_count,
            "behind_count": self.behind_count,
            "staged_count": self.staged_count,
            "unstaged_count": self.unstaged_count,
            "untracked_count": self.untracked_count,
            "working_tree_status": self.working_tree_status.value,
            "needs_push": self.needs_push,
            "needs_pull": self.needs_pull,
            "has_conflict_risk": self.has_conflict_risk,
            "last_commit_date": (
                self.last_commit_date.isoformat() if self.last_commit_date else None
            ),
            "error_message": self.error_message,
        }


@dataclass
class OperationResult:
    """Result of a Git operation."""

    path: Path
    name: str
    success: bool
    operation: str
    message: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "success": self.success,
            "operation": self.operation,
            "message": self.message,
            "error": self.error,
        }


@dataclass
class FleetSummary:
    """Summary of fleet status."""

    total: int = 0
    clean: int = 0
    need_push: int = 0
    need_pull: int = 0
    diverged: int = 0
    dirty: int = 0
    conflict_risk: int = 0
    errors: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SyncOperationSummary:
    """Summary of sync operation results."""

    fetched: int = 0
    fetched_failed: int = 0
    pulled: int = 0
    pulled_failed: int = 0
    pushed: int = 0
    pushed_failed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_results(
        cls,
        fetch_results: list[OperationResult],
        pull_results: list[OperationResult],
        push_results: list[OperationResult],
    ) -> SyncOperationSummary:
        """Build sync operation summary from operation results."""
        return cls(
            fetched=sum(1 for r in fetch_results if r.success),
            fetched_failed=sum(1 for r in fetch_results if not r.success),
            pulled=sum(1 for r in pull_results if r.success),
            pulled_failed=sum(1 for r in pull_results if not r.success),
            pushed=sum(1 for r in push_results if r.success),
            pushed_failed=sum(1 for r in push_results if not r.success),
        )

    @classmethod
    def from_multi_root_results(
        cls,
        fetch_results: list[tuple[Path, list[OperationResult]]],
        pull_results: list[tuple[Path, list[OperationResult]]],
        push_results: list[tuple[Path, list[OperationResult]]],
    ) -> SyncOperationSummary:
        """Build sync operation summary from multi-root operation results."""
        # Flatten results from all roots
        flat_fetch = [r for _, results in fetch_results for r in results]
        flat_pull = [r for _, results in pull_results for r in results]
        flat_push = [r for _, results in push_results for r in results]
        return cls.from_results(flat_fetch, flat_pull, flat_push)


@dataclass
class RepositoryIdentity:
    """Git identity configuration for a repository."""

    path: Path
    name: str
    user_name: str = ""
    user_email: str = ""
    is_local_override: bool = False  # True if local config overrides global
    source: str = "global"  # "local", "global", "included", "system"
    source_file: str = ""  # Full path to the config file

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "user_name": self.user_name,
            "user_email": self.user_email,
            "is_local_override": self.is_local_override,
            "source": self.source,
            "source_file": self.source_file,
        }


@dataclass
class GlobalIdentity:
    """Global Git identity configuration."""

    user_name: str = ""
    user_email: str = ""

    def to_dict(self) -> dict:
        return {
            "user_name": self.user_name,
            "user_email": self.user_email,
        }


@dataclass
class RemoteInfo:
    """Information about a single Git remote."""

    name: str
    fetch_url: str
    push_url: str
    protocol: str  # ssh, https, git, file, etc.

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "fetch_url": self.fetch_url,
            "push_url": self.push_url,
            "protocol": self.protocol,
        }


@dataclass
class RepositoryRemotes:
    """Remote configuration for a repository."""

    path: Path
    name: str
    remotes: list[RemoteInfo]

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "remotes": [r.to_dict() for r in self.remotes],
        }


@dataclass
class RepositoryDiff:
    """File-level diff information for a repository."""

    path: Path
    name: str
    branch: str = ""
    staged_files: list[tuple[str, str]] = field(default_factory=list)
    unstaged_files: list[tuple[str, str]] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)

    @property
    def is_dirty(self) -> bool:
        return bool(self.staged_files or self.unstaged_files or self.untracked_files)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "branch": self.branch,
            "staged_files": [{"status": s, "file": f} for s, f in self.staged_files],
            "unstaged_files": [{"status": s, "file": f} for s, f in self.unstaged_files],
            "untracked_files": self.untracked_files,
            "staged_count": len(self.staged_files),
            "unstaged_count": len(self.unstaged_files),
            "untracked_count": len(self.untracked_files),
        }


# =============================================================================
# Git Operations (Low-level)
# =============================================================================


class GitOperations:
    """Low-level Git operations for a single repository."""

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Run a git command in the repository."""
        return subprocess.run(
            ["git", *args],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
            check=check,
        )

    def fetch_all(self) -> tuple[bool, str]:
        """Fetch all remotes."""
        try:
            result = self._run("fetch", "--all", "--prune", check=False)
            if result.returncode != 0:
                return False, result.stderr.strip()
            return True, ""
        except Exception as e:
            return False, str(e)

    def get_current_branch(self) -> str:
        """Get current branch name."""
        try:
            result = self._run("rev-parse", "--abbrev-ref", "HEAD", check=False)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    def get_remote_branch(self) -> str:
        """Get upstream remote branch."""
        try:
            result = self._run(
                "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    def get_ahead_behind(self) -> tuple[int, int]:
        """Get ahead/behind counts from remote."""
        try:
            result = self._run("rev-list", "--left-right", "--count", "@{u}...HEAD", check=False)
            if result.returncode == 0:
                parts = result.stdout.strip().split()
                if len(parts) == 2:
                    return int(parts[1]), int(parts[0])  # ahead, behind
        except Exception:
            pass
        return 0, 0

    def get_staged_count(self) -> int:
        """Count staged changes."""
        try:
            result = self._run("diff", "--cached", "--numstat", check=False)
            if result.returncode == 0:
                return len([line for line in result.stdout.strip().split("\n") if line])
        except Exception:
            pass
        return 0

    def get_unstaged_count(self) -> int:
        """Count unstaged changes."""
        try:
            result = self._run("diff", "--numstat", check=False)
            if result.returncode == 0:
                return len([line for line in result.stdout.strip().split("\n") if line])
        except Exception:
            pass
        return 0

    def get_untracked_count(self) -> int:
        """Count untracked files."""
        try:
            result = self._run("ls-files", "--others", "--exclude-standard", check=False)
            if result.returncode == 0:
                return len([line for line in result.stdout.strip().split("\n") if line])
        except Exception:
            pass
        return 0

    def get_status_porcelain(
        self,
    ) -> dict:
        """Get branch, ahead/behind, staged/unstaged/untracked counts in one command.

        Uses 'git status --porcelain=v2 --branch' to minimize subprocess calls.
        Returns a dict with keys: branch, remote_branch, ahead, behind,
        staged_count, unstaged_count, untracked_count.
        """
        info: dict = {
            "branch": "",
            "remote_branch": "",
            "ahead": 0,
            "behind": 0,
            "staged_count": 0,
            "unstaged_count": 0,
            "untracked_count": 0,
        }
        try:
            result = self._run("status", "--porcelain=v2", "--branch", check=False)
            if result.returncode != 0:
                return info
            for line in result.stdout.splitlines():
                if line.startswith("# branch.head "):
                    info["branch"] = line[len("# branch.head ") :]
                elif line.startswith("# branch.upstream "):
                    info["remote_branch"] = line[len("# branch.upstream ") :]
                elif line.startswith("# branch.ab "):
                    # Format: # branch.ab +<ahead> -<behind>
                    parts = line.split()
                    if len(parts) == 3:
                        info["ahead"] = abs(int(parts[1]))
                        info["behind"] = abs(int(parts[2]))
                elif line.startswith("1 ") or line.startswith("2 "):
                    # Changed entry: XY sub mH mI mW hH hI path
                    xy = line[2:4]
                    if xy[0] != ".":
                        info["staged_count"] += 1
                    if xy[1] != ".":
                        info["unstaged_count"] += 1
                elif line.startswith("u "):
                    # Unmerged entry: counts as both staged and unstaged
                    info["staged_count"] += 1
                    info["unstaged_count"] += 1
                elif line.startswith("? "):
                    info["untracked_count"] += 1
        except Exception:
            pass
        return info

    def get_staged_files(self) -> list[tuple[str, str]]:
        """Get staged files with their status."""
        try:
            result = self._run("diff", "--cached", "--name-status", check=False)
            if result.returncode == 0 and result.stdout.strip():
                files = []
                for line in result.stdout.strip().splitlines():
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        files.append((parts[0], parts[1]))
                return files
        except Exception:
            pass
        return []

    def get_unstaged_files(self) -> list[tuple[str, str]]:
        """Get unstaged modified files with their status."""
        try:
            result = self._run("diff", "--name-status", check=False)
            if result.returncode == 0 and result.stdout.strip():
                files = []
                for line in result.stdout.strip().splitlines():
                    parts = line.split("\t", 1)
                    if len(parts) == 2:
                        files.append((parts[0], parts[1]))
                return files
        except Exception:
            pass
        return []

    def get_untracked_files(self) -> list[str]:
        """Get untracked file names."""
        try:
            result = self._run("ls-files", "--others", "--exclude-standard", check=False)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()
        except Exception:
            pass
        return []

    def get_last_commit_date(self) -> datetime | None:
        """Get last commit date."""
        try:
            result = self._run("log", "-1", "--format=%cI", check=False)
            if result.returncode == 0 and result.stdout.strip():
                return datetime.fromisoformat(result.stdout.strip())
        except Exception:
            pass
        return None

    def get_user_name(self, local_only: bool = False) -> str:
        """Get configured user.name."""
        try:
            args = ["config"]
            if local_only:
                args.append("--local")
            args.append("user.name")
            result = self._run(*args, check=False)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    def get_user_email(self, local_only: bool = False) -> str:
        """Get configured user.email."""
        try:
            args = ["config"]
            if local_only:
                args.append("--local")
            args.append("user.email")
            result = self._run(*args, check=False)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return ""

    def get_config_source(self, key: str) -> tuple[str, str, str]:
        """Get config value with its source file.

        Returns:
            tuple of (value, source_type, source_file)
            source_type: "local", "global", "included", "system", or "unknown"
        """
        try:
            result = self._run("config", "--show-origin", key, check=False)
            if result.returncode == 0:
                output = result.stdout.strip()
                # Format: "file:/path/to/config\tvalue"
                if "\t" in output:
                    origin, value = output.split("\t", 1)
                    source_file = origin.replace("file:", "", 1)

                    # Determine source type from file path
                    # Order matters: check more specific patterns first
                    if source_file.endswith(".git/config"):
                        source_type = "local"
                    elif source_file.endswith("/etc/gitconfig"):
                        source_type = "system"
                    elif source_file.endswith("/.gitconfig") or source_file.endswith(
                        "/.config/git/config"
                    ):
                        # Only exact ~/.gitconfig or ~/.config/git/config is "global"
                        source_type = "global"
                    else:
                        # includeIf files like ~/.gitconfig-work, ~/.gitconfig-private
                        source_type = "included"

                    return value, source_type, source_file
        except Exception:
            pass
        return "", "unknown", ""

    def get_merge_base(self) -> str | None:
        """Get merge-base between HEAD and upstream."""
        try:
            result = self._run("merge-base", "HEAD", "@{u}", check=False)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return None

    def get_changed_files_between(self, ref1: str, ref2: str) -> set[str]:
        """Get set of files changed between two refs."""
        try:
            result = self._run("diff", "--name-only", ref1, ref2, check=False)
            if result.returncode == 0 and result.stdout.strip():
                return set(result.stdout.strip().splitlines())
        except Exception:
            pass
        return set()

    def get_dirty_files(self) -> set[str]:
        """Get all dirty files (staged + unstaged + untracked)."""
        files: set[str] = set()
        try:
            # Staged + unstaged
            result = self._run("diff", "--name-only", "HEAD", check=False)
            if result.returncode == 0 and result.stdout.strip():
                files.update(result.stdout.strip().splitlines())
            # Untracked
            result = self._run("ls-files", "--others", "--exclude-standard", check=False)
            if result.returncode == 0 and result.stdout.strip():
                files.update(result.stdout.strip().splitlines())
        except Exception:
            pass
        return files

    def has_file_conflicts(self) -> bool:
        """Check if local and remote changes overlap at file level.

        Returns True if there ARE conflicts (overlap found), False if safe.
        """
        merge_base = self.get_merge_base()
        if merge_base is None:
            return True  # can't determine, assume conflict

        remote_files = self.get_changed_files_between(merge_base, "@{u}")
        local_files = self.get_changed_files_between(merge_base, "HEAD")
        local_files |= self.get_dirty_files()

        return bool(local_files & remote_files)

    def pull(self) -> tuple[bool, str]:
        """Pull from remote."""
        try:
            result = self._run("pull", check=False)
            if result.returncode != 0:
                return False, result.stderr.strip() or result.stdout.strip()
            return True, result.stdout.strip()
        except Exception as e:
            return False, str(e)

    def push(self) -> tuple[bool, str]:
        """Push to remote."""
        try:
            result = self._run("push", check=False)
            if result.returncode != 0:
                return False, result.stderr.strip() or result.stdout.strip()
            return True, result.stderr.strip() or result.stdout.strip()
        except Exception as e:
            return False, str(e)

    def has_remotes(self) -> bool:
        """Check if any remotes are configured."""
        try:
            result = self._run("remote", check=False)
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            return False

    def is_detached(self) -> bool:
        """Check if HEAD is in detached state."""
        try:
            result = self._run("symbolic-ref", "HEAD", check=False)
            return result.returncode != 0
        except Exception:
            return True

    def get_remotes(self) -> list[RemoteInfo]:
        """Get all remotes with their URLs."""
        remotes = []
        try:
            # Get list of remote names
            result = self._run("remote", check=False)
            if result.returncode != 0:
                return []

            remote_names = [n.strip() for n in result.stdout.strip().split("\n") if n.strip()]

            for name in remote_names:
                # Get fetch URL
                fetch_result = self._run("remote", "get-url", name, check=False)
                fetch_url = fetch_result.stdout.strip() if fetch_result.returncode == 0 else ""

                # Get push URL (may differ from fetch URL)
                push_result = self._run("remote", "get-url", "--push", name, check=False)
                push_url = push_result.stdout.strip() if push_result.returncode == 0 else fetch_url

                # Determine protocol from URL
                protocol = self._detect_protocol(fetch_url)

                remotes.append(
                    RemoteInfo(
                        name=name,
                        fetch_url=fetch_url,
                        push_url=push_url,
                        protocol=protocol,
                    )
                )
        except Exception:
            pass
        return remotes

    @staticmethod
    def _detect_protocol(url: str) -> str:
        """Detect protocol from Git URL."""
        if not url:
            return "unknown"
        if url.startswith("https://"):
            return "https"
        if url.startswith("http://"):
            return "http"
        if url.startswith("git://"):
            return "git"
        if url.startswith("file://") or url.startswith("/"):
            return "file"
        if url.startswith("ssh://") or "@" in url:
            # SSH URLs: ssh://user@host/path or user@host:path
            return "ssh"
        return "unknown"


# =============================================================================
# Repository Manager
# =============================================================================


class GitRepository:
    """High-level interface for a single Git repository."""

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name
        self.ops = GitOperations(path)

    def has_remotes(self) -> bool:
        """Check if any remotes are configured."""
        return self.ops.has_remotes()

    def is_detached(self) -> bool:
        """Check if HEAD is in detached state."""
        return self.ops.is_detached()

    def get_status(self, fetch_first: bool = False) -> RepositoryStatus:
        """Get complete repository status."""
        status = RepositoryStatus(path=self.path, name=self.name)

        try:
            if fetch_first:
                success, error = self.ops.fetch_all()
                if not success:
                    status.sync_status = SyncStatus.ERROR
                    status.error_message = f"Fetch failed: {error}"
                    return status

            info = self.ops.get_status_porcelain()
            status.branch = info["branch"]
            status.remote_branch = info["remote_branch"]
            status.ahead_count = info["ahead"]
            status.behind_count = info["behind"]
            status.staged_count = info["staged_count"]
            status.unstaged_count = info["unstaged_count"]
            status.untracked_count = info["untracked_count"]

            if status.remote_branch:
                if status.ahead_count > 0 and status.behind_count > 0:
                    status.sync_status = SyncStatus.DIVERGED
                elif status.ahead_count > 0:
                    status.sync_status = SyncStatus.AHEAD
                elif status.behind_count > 0:
                    status.sync_status = SyncStatus.BEHIND
                else:
                    status.sync_status = SyncStatus.CLEAN
            elif status.branch == "(detached)":
                status.sync_status = SyncStatus.DETACHED
            elif self.ops.has_remotes():
                status.sync_status = SyncStatus.NO_UPSTREAM
            else:
                status.sync_status = SyncStatus.NO_REMOTE

            status.last_commit_date = self.ops.get_last_commit_date()

        except Exception as e:
            status.sync_status = SyncStatus.ERROR
            status.error_message = str(e)

        return status

    def fetch(self) -> OperationResult:
        """Fetch all remotes."""
        success, message = self.ops.fetch_all()
        return OperationResult(
            path=self.path,
            name=self.name,
            success=success,
            operation="fetch",
            message="Fetched successfully" if success else "",
            error=message if not success else "",
        )

    def has_file_conflicts(self) -> bool:
        """Check if local and remote changes overlap at file level."""
        return self.ops.has_file_conflicts()

    def pull(self) -> OperationResult:
        """Pull from remote."""
        success, message = self.ops.pull()
        return OperationResult(
            path=self.path,
            name=self.name,
            success=success,
            operation="pull",
            message=message if success else "",
            error=message if not success else "",
        )

    def push(self) -> OperationResult:
        """Push to remote."""
        success, message = self.ops.push()
        return OperationResult(
            path=self.path,
            name=self.name,
            success=success,
            operation="push",
            message=message if success else "",
            error=message if not success else "",
        )

    def get_identity(self) -> RepositoryIdentity:
        """Get repository identity configuration."""
        # Get email with source info (email is primary for identity)
        email, source_type, source_file = self.ops.get_config_source("user.email")
        name, _, _ = self.ops.get_config_source("user.name")

        # Check if there's a local override (for backward compatibility)
        local_name = self.ops.get_user_name(local_only=True)
        local_email = self.ops.get_user_email(local_only=True)
        has_local_override = bool(local_name or local_email)

        return RepositoryIdentity(
            path=self.path,
            name=self.name,
            user_name=name,
            user_email=email,
            is_local_override=has_local_override,
            source=source_type,
            source_file=source_file,
        )

    def get_remotes(self) -> RepositoryRemotes:
        """Get remote configuration for this repository."""
        remotes = self.ops.get_remotes()
        return RepositoryRemotes(
            path=self.path,
            name=self.name,
            remotes=remotes,
        )

    def get_diff(self) -> RepositoryDiff:
        """Get file-level diff information."""
        return RepositoryDiff(
            path=self.path,
            name=self.name,
            branch=self.ops.get_current_branch(),
            staged_files=self.ops.get_staged_files(),
            unstaged_files=self.ops.get_unstaged_files(),
            untracked_files=self.ops.get_untracked_files(),
        )


# =============================================================================
# Fleet Manager
# =============================================================================


class FleetManager:
    """Manage multiple Git repositories."""

    def __init__(
        self,
        root_path: Path,
        max_workers: int = 8,
        *,
        include_no_remote: bool = True,
        include_detached: bool = True,
    ):
        self.root_path = root_path.resolve()
        self.max_workers = max_workers
        self.include_no_remote = include_no_remote
        self.include_detached = include_detached
        self._repositories: list[GitRepository] | None = None

    def discover_repositories(self) -> list[GitRepository]:
        """Discover all Git repositories under root path."""
        if self._repositories is not None:
            return self._repositories

        repos = []
        for git_dir in self.root_path.rglob(".git"):
            if git_dir.is_dir():
                repo_path = git_dir.parent
                repos.append(GitRepository(repo_path))

        # Sort by path for consistent ordering
        repos.sort(key=lambda r: r.path)

        if not self.include_no_remote:
            repos = [r for r in repos if r.has_remotes()]
        if not self.include_detached:
            repos = [r for r in repos if not r.is_detached()]

        self._repositories = repos
        return repos

    def _execute_parallel(
        self,
        operation: Callable[[GitRepository], Any],
        repos: list[GitRepository] | None = None,
        sequential: bool = False,
    ) -> list:
        """Execute operation on repositories in parallel or sequentially."""
        if repos is None:
            repos = self.discover_repositories()

        results = []

        if sequential or len(repos) <= 1:
            for repo in repos:
                results.append(operation(repo))
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {executor.submit(operation, repo): repo for repo in repos}
                for future in as_completed(futures):
                    results.append(future.result())

        # Sort by path for consistent ordering
        results.sort(key=lambda r: r.path if hasattr(r, "path") else str(r))
        return results

    def get_all_status(
        self, fetch_first: bool = True, sequential: bool = False
    ) -> list[RepositoryStatus]:
        """Get status of all repositories."""
        return self._execute_parallel(
            lambda repo: repo.get_status(fetch_first=fetch_first),
            sequential=sequential,
        )

    def fetch_all(self, sequential: bool = False) -> list[OperationResult]:
        """Fetch all repositories."""
        return self._execute_parallel(
            lambda repo: repo.fetch(),
            sequential=sequential,
        )

    def pull_all(
        self,
        only_behind: bool = True,
        sequential: bool = False,
        dry_run: bool = False,
        mode: PullMode = PullMode.SMART,
        statuses: list[RepositoryStatus] | None = None,
    ) -> list[OperationResult]:
        """Pull all repositories that need pulling."""
        repos = self.discover_repositories()

        if only_behind:
            if statuses is None:
                statuses = self.get_all_status(fetch_first=True, sequential=sequential)
            if mode == PullMode.FORCE:
                repos_to_pull = [repo for repo, status in zip(repos, statuses) if status.needs_pull]
            elif mode == PullMode.SAFE:
                repos_to_pull = [
                    repo
                    for repo, status in zip(repos, statuses)
                    if status.needs_pull and not status.has_conflict_risk
                ]
            else:
                # SMART: safe repos + conflict-risk repos with no file overlap
                safe: list[GitRepository] = []
                needs_check: list[GitRepository] = []
                for repo, status in zip(repos, statuses):
                    if not status.needs_pull:
                        continue
                    if not status.has_conflict_risk:
                        safe.append(repo)
                    else:
                        needs_check.append(repo)
                if needs_check:
                    if sequential or len(needs_check) <= 1:
                        for repo in needs_check:
                            if not repo.has_file_conflicts():
                                safe.append(repo)
                    else:
                        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                            futures = {
                                executor.submit(repo.has_file_conflicts): repo
                                for repo in needs_check
                            }
                            for future in as_completed(futures):
                                if not future.result():
                                    safe.append(futures[future])
                repos_to_pull = safe
        else:
            repos_to_pull = repos

        if dry_run:
            return [
                OperationResult(
                    path=repo.path,
                    name=repo.name,
                    success=True,
                    operation="pull",
                    message="Would pull (dry-run)",
                )
                for repo in repos_to_pull
            ]

        return self._execute_parallel(
            lambda repo: repo.pull(),
            repos=repos_to_pull,
            sequential=sequential,
        )

    def push_all(
        self,
        only_ahead: bool = True,
        sequential: bool = False,
        dry_run: bool = False,
        statuses: list[RepositoryStatus] | None = None,
    ) -> list[OperationResult]:
        """Push all repositories that need pushing."""
        repos = self.discover_repositories()

        if only_ahead:
            if statuses is None:
                statuses = self.get_all_status(fetch_first=True, sequential=sequential)
            repos_to_push = [repo for repo, status in zip(repos, statuses) if status.needs_push]
        else:
            repos_to_push = repos

        if dry_run:
            return [
                OperationResult(
                    path=repo.path,
                    name=repo.name,
                    success=True,
                    operation="push",
                    message="Would push (dry-run)",
                )
                for repo in repos_to_push
            ]

        return self._execute_parallel(
            lambda repo: repo.push(),
            repos=repos_to_push,
            sequential=sequential,
        )

    def get_summary(self, statuses: list[RepositoryStatus]) -> FleetSummary:
        """Generate summary from statuses."""
        summary = FleetSummary(total=len(statuses))

        for status in statuses:
            if status.sync_status == SyncStatus.ERROR:
                summary.errors += 1
            elif (
                status.sync_status == SyncStatus.CLEAN
                and status.working_tree_status == WorkingTreeStatus.CLEAN
            ):
                summary.clean += 1

            if status.needs_push:
                summary.need_push += 1
            if status.needs_pull:
                summary.need_pull += 1
            if status.is_diverged:
                summary.diverged += 1
            if status.working_tree_status == WorkingTreeStatus.DIRTY:
                summary.dirty += 1
            if status.has_conflict_risk:
                summary.conflict_risk += 1

        return summary

    def get_all_identities(self, sequential: bool = False) -> list[RepositoryIdentity]:
        """Get identity configuration for all repositories."""
        return self._execute_parallel(
            lambda repo: repo.get_identity(),
            sequential=sequential,
        )

    def get_all_remotes(self, sequential: bool = False) -> list[RepositoryRemotes]:
        """Get remote configuration for all repositories."""
        return self._execute_parallel(
            lambda repo: repo.get_remotes(),
            sequential=sequential,
        )

    def get_all_diff(
        self, sequential: bool = False, dirty_only: bool = True
    ) -> list[RepositoryDiff]:
        """Get file-level diff for all repositories."""
        results = self._execute_parallel(
            lambda repo: repo.get_diff(),
            sequential=sequential,
        )
        if dirty_only:
            results = [r for r in results if r.is_dirty]
        return results


def get_global_identity() -> GlobalIdentity:
    """Get global Git identity configuration."""
    try:
        name_result = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True,
            text=True,
            check=False,
        )
        email_result = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True,
            text=True,
            check=False,
        )
        return GlobalIdentity(
            user_name=name_result.stdout.strip() if name_result.returncode == 0 else "",
            user_email=email_result.stdout.strip() if email_result.returncode == 0 else "",
        )
    except Exception:
        return GlobalIdentity()


def load_roots_file(roots_file: Path) -> list[Path]:
    """Load repository roots from a file (one path per line).

    Supports:
    - Comments starting with #
    - Environment variables: $HOME, ${HOME}, $DEV_ROOT, etc.
    - Tilde expansion: ~/path
    """
    roots = []
    try:
        with open(roots_file.expanduser()) as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if line and not line.startswith("#"):
                    # Expand environment variables first, then tilde
                    expanded = os.path.expandvars(line)
                    path = Path(expanded).expanduser()
                    if path.exists() and path.is_dir():
                        roots.append(path)
    except FileNotFoundError:
        pass
    return roots


def resolve_roots_file() -> Path | None:
    """Auto-resolve roots file from environment and standard locations.

    Priority order:
    1. $GIT_FLEET_ROOTS environment variable
    2. ~/.config/git-fleet/roots (XDG-compliant)
    3. ~/.git-fleet-roots (legacy fallback)
    """
    env_roots = os.environ.get("GIT_FLEET_ROOTS")
    if env_roots:
        env_path = Path(env_roots).expanduser()
        if env_path.exists() and env_path.is_file():
            return env_path

    xdg_path = Path.home() / ".config" / "git-fleet" / "roots"
    if xdg_path.exists() and xdg_path.is_file():
        return xdg_path

    legacy_path = Path.home() / ".git-fleet-roots"
    if legacy_path.exists() and legacy_path.is_file():
        return legacy_path

    return None


class MultiRootFleetManager:
    """Manage multiple Git repositories across multiple root directories."""

    def __init__(
        self,
        roots: list[Path],
        max_workers: int = 8,
        *,
        include_no_remote: bool = True,
        include_detached: bool = True,
    ):
        self.roots = [r.resolve() for r in roots]
        self.max_workers = max_workers
        self._fleet_managers: dict[Path, FleetManager] = {
            root: FleetManager(
                root,
                max_workers,
                include_no_remote=include_no_remote,
                include_detached=include_detached,
            )
            for root in self.roots
        }

    def get_all_identities(
        self, sequential: bool = False
    ) -> list[tuple[Path, list[RepositoryIdentity]]]:
        """Get identity configuration for all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            identities = fleet.get_all_identities(sequential=sequential)
            results.append((root, identities))
        return results

    def get_all_remotes(
        self, sequential: bool = False
    ) -> list[tuple[Path, list[RepositoryRemotes]]]:
        """Get remote configuration for all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            remotes = fleet.get_all_remotes(sequential=sequential)
            results.append((root, remotes))
        return results

    def get_all_diff(
        self, sequential: bool = False, dirty_only: bool = True
    ) -> list[tuple[Path, list[RepositoryDiff]]]:
        """Get file-level diff for all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            diffs = fleet.get_all_diff(sequential=sequential, dirty_only=dirty_only)
            results.append((root, diffs))
        return results

    def get_all_status(
        self, fetch_first: bool = True, sequential: bool = False
    ) -> list[tuple[Path, list[RepositoryStatus]]]:
        """Get status for all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            statuses = fleet.get_all_status(fetch_first=fetch_first, sequential=sequential)
            results.append((root, statuses))
        return results

    def discover_all_repositories(self) -> list[tuple[Path, list[GitRepository]]]:
        """Discover all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            repos = fleet.discover_repositories()
            results.append((root, repos))
        return results

    def fetch_all(self, sequential: bool = False) -> list[tuple[Path, list[OperationResult]]]:
        """Fetch all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            fetch_results = fleet.fetch_all(sequential=sequential)
            results.append((root, fetch_results))
        return results

    def pull_all(
        self,
        only_behind: bool = True,
        sequential: bool = False,
        dry_run: bool = False,
        mode: PullMode = PullMode.SMART,
        all_statuses: list[tuple[Path, list[RepositoryStatus]]] | None = None,
    ) -> list[tuple[Path, list[OperationResult]]]:
        """Pull all repositories across all roots."""
        statuses_by_root: dict[Path, list[RepositoryStatus]] = {}
        if all_statuses is not None:
            statuses_by_root = {root: statuses for root, statuses in all_statuses}

        results = []
        for root, fleet in self._fleet_managers.items():
            pull_results = fleet.pull_all(
                only_behind=only_behind,
                sequential=sequential,
                dry_run=dry_run,
                mode=mode,
                statuses=statuses_by_root.get(root),
            )
            results.append((root, pull_results))
        return results

    def push_all(
        self,
        only_ahead: bool = True,
        sequential: bool = False,
        dry_run: bool = False,
        all_statuses: list[tuple[Path, list[RepositoryStatus]]] | None = None,
    ) -> list[tuple[Path, list[OperationResult]]]:
        """Push all repositories across all roots."""
        statuses_by_root: dict[Path, list[RepositoryStatus]] = {}
        if all_statuses is not None:
            statuses_by_root = {root: statuses for root, statuses in all_statuses}

        results = []
        for root, fleet in self._fleet_managers.items():
            push_results = fleet.push_all(
                only_ahead=only_ahead,
                sequential=sequential,
                dry_run=dry_run,
                statuses=statuses_by_root.get(root),
            )
            results.append((root, push_results))
        return results

    def get_summary(self, all_statuses: list[tuple[Path, list[RepositoryStatus]]]) -> FleetSummary:
        """Generate combined summary from all statuses."""
        all_status_list = []
        for _, statuses in all_statuses:
            all_status_list.extend(statuses)

        summary = FleetSummary(total=len(all_status_list))
        for status in all_status_list:
            if status.sync_status == SyncStatus.ERROR:
                summary.errors += 1
            elif (
                status.sync_status == SyncStatus.CLEAN
                and status.working_tree_status == WorkingTreeStatus.CLEAN
            ):
                summary.clean += 1

            if status.needs_push:
                summary.need_push += 1
            if status.needs_pull:
                summary.need_pull += 1
            if status.is_diverged:
                summary.diverged += 1
            if status.working_tree_status == WorkingTreeStatus.DIRTY:
                summary.dirty += 1
            if status.has_conflict_risk:
                summary.conflict_risk += 1

        return summary


# =============================================================================
# CLI Application
# =============================================================================


app = typer.Typer(
    name="git-fleet",
    help="Command multiple Git repositories like a fleet admiral.",
    no_args_is_help=True,
)


def version_callback(value: bool):
    """Print version and exit."""
    if value:
        print(f"git-fleet {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
    schema: bool = typer.Option(
        False,
        "--schema",
        help="Output MCP-compatible tool schema for AI agents",
    ),
):
    """git-fleet: Command multiple Git repositories like a fleet admiral."""
    if schema:
        print(json.dumps(get_tool_schema(), indent=2))
        raise typer.Exit()

    # If no command and no schema, show help
    if ctx.invoked_subcommand is None and not schema:
        # Let typer handle this with no_args_is_help
        pass


def get_console_and_formatter(json_output: bool) -> tuple[Console, OutputFormatter]:
    """Create console and formatter."""
    console = Console(force_terminal=not json_output)
    formatter = OutputFormatter(console, use_json=json_output)
    return console, formatter


@app.command()
def status(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    sequential: bool = typer.Option(
        False,
        "--sequential",
        "-s",
        help="Run sequentially instead of parallel",
    ),
    no_fetch: bool = typer.Option(
        False,
        "--no-fetch",
        help="Skip fetching before status check",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
    include_no_remote: bool = typer.Option(
        False,
        "--include-no-remote",
        help="Include repositories with no configured remotes",
    ),
    include_detached: bool = typer.Option(
        False,
        "--include-detached",
        help="Include repositories with detached HEAD (e.g. SPM checkouts)",
    ),
):
    """Show status of all repositories."""
    console, formatter = get_console_and_formatter(json_output)

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(
            root_paths,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task(
                    "Fetching and analyzing..." if not no_fetch else "Analyzing...",
                    total=None,
                )
                all_statuses = multi_fleet.get_all_status(
                    fetch_first=not no_fetch, sequential=sequential
                )
        else:
            all_statuses = multi_fleet.get_all_status(
                fetch_first=not no_fetch, sequential=sequential
            )

        summary = multi_fleet.get_summary(all_statuses)
        formatter.print_multi_root_status_list(all_statuses, summary)
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(
            target_path,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Scanning repositories...", total=None)
                repos = fleet.discover_repositories()

            console.print(f"Found [bold]{len(repos)}[/] repositories\n")

            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task(
                    "Fetching and analyzing..." if not no_fetch else "Analyzing...",
                    total=None,
                )
                statuses = fleet.get_all_status(
                    fetch_first=not no_fetch,
                    sequential=sequential,
                )
        else:
            repos = fleet.discover_repositories()
            statuses = fleet.get_all_status(
                fetch_first=not no_fetch,
                sequential=sequential,
            )

        summary = fleet.get_summary(statuses)
        formatter.print_status_list(statuses, summary, target_path.resolve())


@app.command()
def fetch(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    sequential: bool = typer.Option(
        False,
        "--sequential",
        "-s",
        help="Run sequentially instead of parallel",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
    include_no_remote: bool = typer.Option(
        False,
        "--include-no-remote",
        help="Include repositories with no configured remotes",
    ),
    include_detached: bool = typer.Option(
        False,
        "--include-detached",
        help="Include repositories with detached HEAD (e.g. SPM checkouts)",
    ),
):
    """Fetch all repositories."""
    console, formatter = get_console_and_formatter(json_output)

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(
            root_paths,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Fetching all repositories...", total=None)
                all_results = multi_fleet.fetch_all(sequential=sequential)
        else:
            all_results = multi_fleet.fetch_all(sequential=sequential)

        formatter.print_multi_root_operation_results(all_results, "fetch")
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(
            target_path,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Fetching all repositories...", total=None)
                results = fleet.fetch_all(sequential=sequential)
        else:
            results = fleet.fetch_all(sequential=sequential)

        formatter.print_operation_results(results, "fetch")


@app.command()
def pull(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    sequential: bool = typer.Option(
        False,
        "--sequential",
        "-s",
        help="Run sequentially instead of parallel",
    ),
    all_repos: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Pull all repositories, not just those behind",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Show what would be pulled without actually pulling",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Pull all regardless of conflict risk",
    ),
    safe: bool = typer.Option(
        False,
        "--safe",
        help="Skip all conflict-risk repos without file-level check",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
    include_no_remote: bool = typer.Option(
        False,
        "--include-no-remote",
        help="Include repositories with no configured remotes",
    ),
    include_detached: bool = typer.Option(
        False,
        "--include-detached",
        help="Include repositories with detached HEAD (e.g. SPM checkouts)",
    ),
):
    """Pull repositories that are behind remote.

    By default (smart mode), checks file-level overlap for conflict-risk repos
    and pulls them if changed files don't overlap. Use --safe to skip all
    conflict-risk repos, or --force to pull everything.
    """
    console, formatter = get_console_and_formatter(json_output)

    if force and safe:
        console.print("[red]Error: --force and --safe are mutually exclusive[/]")
        raise typer.Exit(1)

    if force:
        mode = PullMode.FORCE
    elif safe:
        mode = PullMode.SAFE
    else:
        mode = PullMode.SMART

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(
            root_paths,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Pulling repositories...", total=None)
                all_results = multi_fleet.pull_all(
                    only_behind=not all_repos,
                    sequential=sequential,
                    dry_run=dry_run,
                    mode=mode,
                )
        else:
            all_results = multi_fleet.pull_all(
                only_behind=not all_repos,
                sequential=sequential,
                dry_run=dry_run,
                mode=mode,
            )

        formatter.print_multi_root_operation_results(all_results, "pull")
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(
            target_path,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        # In safe mode, show conflict warnings upfront
        if mode == PullMode.SAFE and not all_repos:
            statuses = fleet.get_all_status(fetch_first=True, sequential=sequential)
            conflict_repos = [s for s in statuses if s.has_conflict_risk and s.needs_pull]

            if conflict_repos and not json_output:
                console.print(
                    "[bold red]⚠ Warning: The following repositories have conflict risk:[/]\n"
                )
                for s in conflict_repos:
                    console.print(f"  [red]{s.name}[/] - ", end="")
                    if s.is_diverged:
                        console.print(f"diverged ({s.ahead_count} ahead, {s.behind_count} behind)")
                    else:
                        console.print("dirty working tree + behind remote")
                console.print(
                    "\n[dim]Remove --safe for smart mode (file-level check),"
                    " use --force to pull all, or resolve manually.[/]\n"
                )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Pulling repositories...", total=None)
                results = fleet.pull_all(
                    only_behind=not all_repos,
                    sequential=sequential,
                    dry_run=dry_run,
                    mode=mode,
                )
        else:
            results = fleet.pull_all(
                only_behind=not all_repos,
                sequential=sequential,
                dry_run=dry_run,
                mode=mode,
            )

        formatter.print_operation_results(results, "pull")


@app.command()
def push(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    sequential: bool = typer.Option(
        False,
        "--sequential",
        "-s",
        help="Run sequentially instead of parallel",
    ),
    all_repos: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Push all repositories, not just those ahead",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Show what would be pushed without actually pushing",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
    include_no_remote: bool = typer.Option(
        False,
        "--include-no-remote",
        help="Include repositories with no configured remotes",
    ),
    include_detached: bool = typer.Option(
        False,
        "--include-detached",
        help="Include repositories with detached HEAD (e.g. SPM checkouts)",
    ),
):
    """Push repositories that are ahead of remote."""
    console, formatter = get_console_and_formatter(json_output)

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(
            root_paths,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Pushing repositories...", total=None)
                all_results = multi_fleet.push_all(
                    only_ahead=not all_repos,
                    sequential=sequential,
                    dry_run=dry_run,
                )
        else:
            all_results = multi_fleet.push_all(
                only_ahead=not all_repos,
                sequential=sequential,
                dry_run=dry_run,
            )

        formatter.print_multi_root_operation_results(all_results, "push")
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(
            target_path,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Pushing repositories...", total=None)
                results = fleet.push_all(
                    only_ahead=not all_repos,
                    sequential=sequential,
                    dry_run=dry_run,
                )
        else:
            results = fleet.push_all(
                only_ahead=not all_repos,
                sequential=sequential,
                dry_run=dry_run,
            )

        formatter.print_operation_results(results, "push")


@app.command()
def sync(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    sequential: bool = typer.Option(
        False,
        "--sequential",
        "-s",
        help="Run sequentially instead of parallel",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        "-n",
        help="Show what would happen without actually doing it",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
    include_no_remote: bool = typer.Option(
        False,
        "--include-no-remote",
        help="Include repositories with no configured remotes",
    ),
    include_detached: bool = typer.Option(
        False,
        "--include-detached",
        help="Include repositories with detached HEAD (e.g. SPM checkouts)",
    ),
):
    """Sync all repositories: fetch, pull (smart), then push."""
    console, formatter = get_console_and_formatter(json_output)

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(
            root_paths,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )
        all_results = {"fetch": [], "pull": [], "push": []}

        # Step 1: Fetch
        if not json_output:
            console.print("[bold]Step 1/4: Fetching all repositories...[/]")

        fetch_results = multi_fleet.fetch_all(sequential=sequential)
        all_results["fetch"] = fetch_results

        if not json_output:
            total = sum(len(results) for _, results in fetch_results)
            success = sum(sum(1 for r in results if r.success) for _, results in fetch_results)
            console.print(f"  Fetched {success}/{total} repositories\n")

        # Get status once after fetch (no re-fetch needed)
        pre_statuses = multi_fleet.get_all_status(fetch_first=False, sequential=sequential)

        # Step 2: Pull (smart) - reuse pre-fetched statuses
        if not json_output:
            console.print("[bold]Step 2/4: Pulling repositories (smart)...[/]")

        pull_results = multi_fleet.pull_all(
            only_behind=True,
            sequential=sequential,
            dry_run=dry_run,
            all_statuses=pre_statuses,
        )
        all_results["pull"] = pull_results

        if not json_output:
            total = sum(len(results) for _, results in pull_results)
            if total > 0:
                success = sum(sum(1 for r in results if r.success) for _, results in pull_results)
                console.print(f"  Pulled {success}/{total} repositories\n")
            else:
                console.print("  No repositories needed pulling\n")

        # Re-check status after pull (no fetch needed, pull changed ahead/behind)
        post_pull_statuses = multi_fleet.get_all_status(fetch_first=False, sequential=sequential)

        # Step 3: Push - reuse post-pull statuses
        if not json_output:
            console.print("[bold]Step 3/4: Pushing repositories...[/]")

        push_results = multi_fleet.push_all(
            only_ahead=True,
            sequential=sequential,
            dry_run=dry_run,
            all_statuses=post_pull_statuses,
        )
        all_results["push"] = push_results

        if not json_output:
            total = sum(len(results) for _, results in push_results)
            if total > 0:
                success = sum(sum(1 for r in results if r.success) for _, results in push_results)
                console.print(f"  Pushed {success}/{total} repositories\n")
            else:
                console.print("  No repositories needed pushing\n")

            # Step 4: Final status check
            console.print("[bold]Step 4/4: Checking final status...[/]\n")
            all_statuses = multi_fleet.get_all_status(fetch_first=False, sequential=sequential)
            summary = multi_fleet.get_summary(all_statuses)
            sync_summary = SyncOperationSummary.from_multi_root_results(
                fetch_results, pull_results, push_results
            )
            formatter.print_multi_root_status_list(all_statuses, summary, sync_summary)

            if summary.conflict_risk > 0:
                console.print(
                    f"\n[bold red]⚠ {summary.conflict_risk} repositories need manual attention[/]"
                )
        else:
            all_statuses = multi_fleet.get_all_status(fetch_first=False, sequential=sequential)
            summary = multi_fleet.get_summary(all_statuses)
            sync_summary = SyncOperationSummary.from_multi_root_results(
                fetch_results, pull_results, push_results
            )

            output = {
                "fetch": [
                    {"root": str(root), "results": [r.to_dict() for r in results]}
                    for root, results in fetch_results
                ],
                "pull": [
                    {"root": str(root), "results": [r.to_dict() for r in results]}
                    for root, results in pull_results
                ],
                "push": [
                    {"root": str(root), "results": [r.to_dict() for r in results]}
                    for root, results in push_results
                ],
                "status": [
                    {"root": str(root), "statuses": [s.to_dict() for s in statuses]}
                    for root, statuses in all_statuses
                ],
                "summary": summary.to_dict(),
                "sync_operations": sync_summary.to_dict(),
            }
            console.print(json.dumps(output, indent=2))
        return

    # Single root mode
    target_path = path if path else Path(".")
    fleet = FleetManager(
        target_path,
        include_no_remote=include_no_remote,
        include_detached=include_detached,
    )

    all_results = []

    # Step 1: Fetch
    if not json_output:
        console.print("[bold]Step 1/4: Fetching all repositories...[/]")

    fetch_results = fleet.fetch_all(sequential=sequential)
    all_results.extend(fetch_results)

    if not json_output:
        success = sum(1 for r in fetch_results if r.success)
        console.print(f"  Fetched {success}/{len(fetch_results)} repositories\n")

    # Get status once after fetch (no re-fetch needed)
    pre_statuses = fleet.get_all_status(fetch_first=False, sequential=sequential)

    # Step 2: Pull (smart) - reuse pre-fetched statuses
    if not json_output:
        console.print("[bold]Step 2/4: Pulling repositories (smart)...[/]")

    pull_results = fleet.pull_all(
        only_behind=True,
        sequential=sequential,
        dry_run=dry_run,
        statuses=pre_statuses,
    )
    all_results.extend(pull_results)

    if not json_output:
        if pull_results:
            success = sum(1 for r in pull_results if r.success)
            console.print(f"  Pulled {success}/{len(pull_results)} repositories\n")
        else:
            console.print("  No repositories needed pulling\n")

    # Re-check status after pull (no fetch needed, pull changed ahead/behind)
    post_pull_statuses = fleet.get_all_status(fetch_first=False, sequential=sequential)

    # Step 3: Push - reuse post-pull statuses
    if not json_output:
        console.print("[bold]Step 3/4: Pushing repositories...[/]")

    push_results = fleet.push_all(
        only_ahead=True,
        sequential=sequential,
        dry_run=dry_run,
        statuses=post_pull_statuses,
    )
    all_results.extend(push_results)

    if not json_output:
        if push_results:
            success = sum(1 for r in push_results if r.success)
            console.print(f"  Pushed {success}/{len(push_results)} repositories\n")
        else:
            console.print("  No repositories needed pushing\n")

        # Step 4: Final status check
        console.print("[bold]Step 4/4: Checking final status...[/]\n")
        statuses = fleet.get_all_status(fetch_first=False, sequential=sequential)
        summary = fleet.get_summary(statuses)
        sync_summary = SyncOperationSummary.from_results(fetch_results, pull_results, push_results)
        formatter.print_status_list(statuses, summary, target_path.resolve(), sync_summary)

        if summary.conflict_risk > 0:
            console.print(
                f"\n[bold red]⚠ {summary.conflict_risk} repositories need manual attention[/]"
            )
    else:
        statuses = fleet.get_all_status(fetch_first=False, sequential=sequential)
        summary = fleet.get_summary(statuses)
        sync_summary = SyncOperationSummary.from_results(fetch_results, pull_results, push_results)

        output = {
            "fetch": [r.to_dict() for r in fetch_results],
            "pull": [r.to_dict() for r in pull_results],
            "push": [r.to_dict() for r in push_results],
            "status": [s.to_dict() for s in statuses],
            "summary": summary.to_dict(),
            "sync_operations": sync_summary.to_dict(),
        }
        console.print(json.dumps(output, indent=2))


@app.command(name="list")
def list_repos(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    paths_only: bool = typer.Option(
        False,
        "--paths",
        "-p",
        help="Output only paths (one per line, for piping to fzf etc.)",
    ),
    show_remote: bool = typer.Option(
        False,
        "--remote",
        help="Include remote URLs and protocols in output",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
):
    """List all discovered repositories."""
    console, formatter = get_console_and_formatter(json_output)

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(root_paths)
        all_repos = multi_fleet.discover_all_repositories()

        if paths_only:
            for _, repos in all_repos:
                for repo in repos:
                    print(repo.path)
        elif show_remote:
            all_remotes = multi_fleet.get_all_remotes()
            formatter.print_multi_root_repo_list_with_remotes(all_repos, all_remotes)
        else:
            formatter.print_multi_root_repo_list(all_repos)
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(target_path)
        repos = fleet.discover_repositories()

        if paths_only:
            for repo in repos:
                print(repo.path)
        elif show_remote:
            remotes = fleet.get_all_remotes()
            formatter.print_repo_list_with_remotes(repos, remotes, target_path.resolve())
        else:
            formatter.print_repo_list(repos, target_path.resolve())


@app.command()
def who(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    sequential: bool = typer.Option(
        False,
        "--sequential",
        "-s",
        help="Run sequentially instead of parallel",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
):
    """Show Git identity (user.name/email) for all repositories."""
    console, formatter = get_console_and_formatter(json_output)
    global_identity = get_global_identity()

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(root_paths)

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Scanning repositories...", total=None)
                all_identities = multi_fleet.get_all_identities(sequential=sequential)
        else:
            all_identities = multi_fleet.get_all_identities(sequential=sequential)

        formatter.print_multi_root_identity_list(all_identities, global_identity)
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(target_path)

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Scanning repositories...", total=None)
                identities = fleet.get_all_identities(sequential=sequential)
        else:
            identities = fleet.get_all_identities(sequential=sequential)

        formatter.print_identity_list(identities, global_identity, target_path.resolve())


@app.command()
def diff(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    sequential: bool = typer.Option(
        False,
        "--sequential",
        "-s",
        help="Run sequentially instead of parallel",
    ),
    all_repos: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Show all repositories including clean ones",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
    include_no_remote: bool = typer.Option(
        False,
        "--include-no-remote",
        help="Include repositories with no configured remotes",
    ),
    include_detached: bool = typer.Option(
        False,
        "--include-detached",
        help="Include repositories with detached HEAD (e.g. SPM checkouts)",
    ),
):
    """Show file-level changes (staged, unstaged, untracked) across repositories."""
    console, formatter = get_console_and_formatter(json_output)

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(
            root_paths,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Scanning for changes...", total=None)
                all_diffs = multi_fleet.get_all_diff(
                    sequential=sequential, dirty_only=not all_repos
                )
        else:
            all_diffs = multi_fleet.get_all_diff(sequential=sequential, dirty_only=not all_repos)

        all_repos_discovered = multi_fleet.discover_all_repositories()
        total_repos_per_root = {root: len(repos) for root, repos in all_repos_discovered}

        formatter.print_multi_root_diff_list(all_diffs, total_repos_per_root)
    else:
        target_path = path if path else Path(".")
        fleet = FleetManager(
            target_path,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Scanning for changes...", total=None)
                repos = fleet.discover_repositories()
                diffs = fleet.get_all_diff(sequential=sequential, dirty_only=not all_repos)
        else:
            repos = fleet.discover_repositories()
            diffs = fleet.get_all_diff(sequential=sequential, dirty_only=not all_repos)

        formatter.print_diff_list(diffs, target_path.resolve(), len(repos))


@app.command()
def remote(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        "-j",
        help="Output as JSON",
    ),
    sequential: bool = typer.Option(
        False,
        "--sequential",
        "-s",
        help="Run sequentially instead of parallel",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
):
    """Show remote URLs and protocols for all repositories."""
    console, formatter = get_console_and_formatter(json_output)

    resolved_roots = roots or resolve_roots_file()
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(root_paths)

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Scanning repositories...", total=None)
                all_remotes = multi_fleet.get_all_remotes(sequential=sequential)
        else:
            all_remotes = multi_fleet.get_all_remotes(sequential=sequential)

        formatter.print_multi_root_remote_list(all_remotes)
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(target_path)

        if not json_output:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Scanning repositories...", total=None)
                remotes = fleet.get_all_remotes(sequential=sequential)
        else:
            remotes = fleet.get_all_remotes(sequential=sequential)

        formatter.print_remote_list(remotes, target_path.resolve())
