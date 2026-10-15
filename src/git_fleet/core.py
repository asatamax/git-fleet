"""
git-fleet: Command multiple Git repositories like a fleet admiral.

A tool for managing multiple Git repositories at once - fetch, status check,
pull, push, and sync operations across your entire development directory.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)

from ._version import __version__
from .formatters import (
    OutputFormatter,
    StatusFilter,
    done_label,
    emit_json,
    escape,
    progress_label,
)
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


DEFAULT_MAX_WORKERS = 8
# Never searched for repositories: git metadata, and dependency trees that can hold
# thousands of vendored checkouts.
_DISCOVERY_PRUNED_DIRS = frozenset({".git", "node_modules"})


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
    default_branch: str = ""
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
    def is_off_default_branch(self) -> bool:
        """Check if HEAD is on a branch other than the default (unknown default counts as on)."""
        return bool(self.default_branch) and self.branch != self.default_branch

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
            "default_branch": self.default_branch,
            "off_default_branch": self.is_off_default_branch,
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
    warning: str = ""

    def to_dict(self) -> dict:
        d = {
            "path": str(self.path),
            "name": self.name,
            "success": self.success,
            "operation": self.operation,
            "message": self.message,
            "error": self.error,
        }
        if self.warning:
            d["warning"] = self.warning
        return d


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
    fetched_warned: int = 0
    pulled: int = 0
    pulled_failed: int = 0
    pulled_warned: int = 0
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
            fetched_warned=sum(1 for r in fetch_results if r.success and r.warning),
            pulled=sum(1 for r in pull_results if r.success),
            pulled_failed=sum(1 for r in pull_results if not r.success),
            pulled_warned=sum(1 for r in pull_results if r.success and r.warning),
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


class WorktreeUpstreamState(StrEnum):
    """Upstream state of a linked worktree's branch."""

    GONE = "gone"
    ALIVE = "alive"
    NONE = "none"
    DETACHED = "detached"
    MISSING = "missing"


@dataclass
class WorktreeInfo:
    """A linked worktree (the main worktree is never represented)."""

    path: Path
    branch: str | None
    head: str
    upstream_state: WorktreeUpstreamState
    locked: bool = False
    dirty_count: int | None = 0
    last_commit_date: datetime | None = None
    external: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "branch": self.branch,
            "head": self.head,
            "upstream_state": self.upstream_state.value,
            "locked": self.locked,
            "dirty_count": self.dirty_count,
            "last_commit_date": (
                self.last_commit_date.isoformat() if self.last_commit_date else None
            ),
            "external": self.external,
            "error": self.error,
        }


@dataclass
class WorktreeSummary:
    """Counts over a set of linked worktrees."""

    total: int = 0
    gone: int = 0
    no_upstream: int = 0
    detached: int = 0
    missing: int = 0
    dirty: int = 0
    errors: int = 0
    oldest_commit_date: datetime | None = None

    @classmethod
    def from_worktrees(cls, worktrees: list[WorktreeInfo]) -> WorktreeSummary:
        states = [w.upstream_state for w in worktrees]
        dates = [w.last_commit_date for w in worktrees if w.last_commit_date is not None]
        return cls(
            total=len(worktrees),
            gone=states.count(WorktreeUpstreamState.GONE),
            no_upstream=states.count(WorktreeUpstreamState.NONE),
            detached=states.count(WorktreeUpstreamState.DETACHED),
            missing=states.count(WorktreeUpstreamState.MISSING),
            dirty=sum(1 for w in worktrees if w.dirty_count),
            errors=sum(1 for w in worktrees if w.dirty_count is None),
            oldest_commit_date=min(dates) if dates else None,
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d["oldest_commit_date"] = (
            self.oldest_commit_date.isoformat() if self.oldest_commit_date else None
        )
        return d


@dataclass
class RepositoryWorktrees:
    """Linked worktrees of a single repository."""

    path: Path
    name: str
    worktrees: list[WorktreeInfo] = field(default_factory=list)
    error: str = ""

    @property
    def summary(self) -> WorktreeSummary:
        return WorktreeSummary.from_worktrees(self.worktrees)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "worktrees": [w.to_dict() for w in self.worktrees],
            "summary": self.summary.to_dict(),
            "error": self.error,
        }


@dataclass
class FleetWorktreeSummary:
    """Fleet-wide worktree counts."""

    repos: int = 0
    repos_with_worktrees: int = 0
    worktrees: WorktreeSummary = field(default_factory=WorktreeSummary)

    @classmethod
    def from_repositories(cls, repos: list[RepositoryWorktrees]) -> FleetWorktreeSummary:
        return cls(
            repos=len(repos),
            repos_with_worktrees=sum(1 for r in repos if r.worktrees),
            worktrees=WorktreeSummary.from_worktrees([w for r in repos for w in r.worktrees]),
        )

    def to_dict(self) -> dict:
        return {
            "repos": self.repos,
            "repos_with_worktrees": self.repos_with_worktrees,
            **self.worktrees.to_dict(),
        }


# =============================================================================
# Git Operations (Low-level)
# =============================================================================


_CASE_INSENSITIVE_FS_MSG = "case-insensitive filesystem"


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

    def get_remote_names(self) -> list[str]:
        """Get configured remote names."""
        try:
            result = self._run("remote", check=False)
            if result.returncode == 0 and result.stdout.strip():
                return [n.strip() for n in result.stdout.splitlines() if n.strip()]
        except Exception:
            pass
        return []

    def get_default_fetch_remote(self) -> str:
        """Get the remote that should be fetched for routine sync."""
        current_branch = self.get_current_branch()
        if current_branch and current_branch != "HEAD":
            try:
                result = self._run(
                    "config",
                    "--get",
                    f"branch.{current_branch}.remote",
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
            except Exception:
                pass

        remotes = self.get_remote_names()
        if "origin" in remotes:
            return "origin"
        return remotes[0] if remotes else ""

    def fetch_all(
        self,
        *,
        all_remotes: bool = True,
        prune: bool = True,
    ) -> tuple[bool, str, str]:
        """Fetch remotes. Returns (success, message_or_error, warning)."""
        try:
            if all_remotes:
                args = ["fetch", "--all"]
                if prune:
                    args.append("--prune")
            else:
                remote = self.get_default_fetch_remote()
                if not remote:
                    return True, "No remotes configured", ""

                args = ["fetch"]
                if prune:
                    args.append("--prune")
                args.append(remote)

            result = self._run(*args, check=False)
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if _CASE_INSENSITIVE_FS_MSG in stderr:
                    return True, "", "Case-insensitive ref conflict (partial fetch succeeded)"
                return False, stderr, ""
            return True, "", ""
        except Exception as e:
            return False, str(e), ""

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

    def get_default_branch(self) -> str:
        """Get the default branch name from origin/HEAD.

        Falls back to 'main' or 'master' if origin/HEAD is not set.
        """
        try:
            result = self._run("symbolic-ref", "refs/remotes/origin/HEAD", check=False)
            if result.returncode == 0:
                # Output: refs/remotes/origin/main -> extract "main" (keeps "feature/x" intact)
                return result.stdout.strip().removeprefix("refs/remotes/origin/")
        except Exception:
            pass
        # Fallback: check if main or master exists
        for name in ("main", "master"):
            try:
                result = self._run("rev-parse", "--verify", f"refs/heads/{name}", check=False)
                if result.returncode == 0:
                    return name
            except Exception:
                pass
        return ""

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
                    if len(parts) == 4:
                        info["ahead"] = abs(int(parts[2]))
                        info["behind"] = abs(int(parts[3]))
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

    def pull(self) -> tuple[bool, str, str]:
        """Pull from remote. Returns (success, message, warning)."""
        try:
            result = self._run("pull", check=False)
            if result.returncode != 0:
                stderr = result.stderr.strip() or result.stdout.strip()
                if _CASE_INSENSITIVE_FS_MSG in stderr:
                    return self._pull_via_merge()
                return False, stderr, ""
            return True, result.stdout.strip(), ""
        except Exception as e:
            return False, str(e), ""

    def _pull_via_merge(self) -> tuple[bool, str, str]:
        """Fallback pull via merge when git pull fails due to case-insensitive ref conflicts."""
        remote_branch = self.get_remote_branch()
        if not remote_branch:
            return False, "Cannot determine upstream branch for merge fallback", ""
        try:
            result = self._run("merge", remote_branch, check=False)
            if result.returncode != 0:
                return False, result.stderr.strip() or result.stdout.strip(), ""
            return (
                True,
                result.stdout.strip(),
                "Used merge fallback due to case-insensitive ref conflict",
            )
        except Exception as e:
            return False, str(e), ""

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
        return bool(self.get_remote_names())

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
            remote_names = self.get_remote_names()

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

    def list_worktrees(self) -> list[dict[str, Any]]:
        """Parse `git worktree list --porcelain -z` (first entry is the main worktree)."""
        # -z keeps paths containing newlines/tabs/quotes verbatim instead of C-quoted.
        result = self._run("worktree", "list", "--porcelain", "-z", check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git worktree list failed")

        entries: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for field_text in result.stdout.split("\0"):
            if not field_text:
                current = None
                continue
            key, _, value = field_text.partition(" ")
            if key == "worktree":
                current = {
                    "path": value,
                    "head": "",
                    "branch": None,
                    "locked": False,
                    "prunable": False,
                }
                entries.append(current)
            elif current is None:
                continue
            elif key == "HEAD":
                current["head"] = value
            elif key == "branch":
                current["branch"] = value
            elif key == "locked":
                current["locked"] = True
            elif key == "prunable":
                current["prunable"] = True
        return entries

    def get_branch_upstream_states(self) -> dict[str, WorktreeUpstreamState]:
        """Map each local branch ref (refs/heads/...) to its upstream state."""
        result = self._run(
            "for-each-ref",
            "--format=%(refname)%09%(upstream)%09%(upstream:track)",
            "refs/heads",
            check=False,
        )
        states: dict[str, WorktreeUpstreamState] = {}
        if result.returncode != 0:
            return states
        for line in result.stdout.splitlines():
            refname, _, rest = line.partition("\t")
            upstream, _, track = rest.partition("\t")
            if track == "[gone]":
                states[refname] = WorktreeUpstreamState.GONE
            elif upstream:
                states[refname] = WorktreeUpstreamState.ALIVE
            else:
                states[refname] = WorktreeUpstreamState.NONE
        return states

    def get_commit_infos(self, shas: list[str]) -> dict[str, tuple[str, datetime | None]]:
        """Map full commit SHAs to (short sha, committer date) in one git call."""
        if not shas:
            return {}

        def parse(stdout: str) -> dict[str, tuple[str, datetime | None]]:
            infos: dict[str, tuple[str, datetime | None]] = {}
            for line in stdout.splitlines():
                parts = line.split("\t")
                if len(parts) == 3:
                    infos[parts[0]] = (parts[1], datetime.fromisoformat(parts[2]))
            return infos

        fmt = "--format=%H%x09%h%x09%cI"
        result = self._run("log", "--no-walk=unsorted", fmt, *shas, check=False)
        if result.returncode == 0:
            return parse(result.stdout)

        # One unknown object (e.g. gc'd HEAD of a missing worktree) fails the batch.
        infos: dict[str, tuple[str, datetime | None]] = {}
        for sha in shas:
            single = self._run("log", "--no-walk", fmt, sha, check=False)
            if single.returncode == 0:
                infos.update(parse(single.stdout))
        return infos

    def count_status_lines(self) -> tuple[int | None, str]:
        """Count `git status --porcelain` lines; returns (None, error) on failure."""
        # Without --no-optional-locks, status may refresh and rewrite the index (index.lock).
        try:
            result = self._run("--no-optional-locks", "status", "--porcelain", check=False)
        except Exception as e:
            return None, str(e)
        if result.returncode != 0:
            return None, result.stderr.strip() or f"git status exited {result.returncode}"
        return len(result.stdout.splitlines()), ""

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
                success, error, _warning = self.ops.fetch_all()
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
            status.default_branch = self.ops.get_default_branch()

        except Exception as e:
            status.sync_status = SyncStatus.ERROR
            status.error_message = str(e)

        return status

    def fetch(
        self,
        *,
        all_remotes: bool = True,
        prune: bool = True,
    ) -> OperationResult:
        """Fetch remotes."""
        success, message, warning = self.ops.fetch_all(all_remotes=all_remotes, prune=prune)
        return OperationResult(
            path=self.path,
            name=self.name,
            success=success,
            operation="fetch",
            message=(message or "Fetched successfully") if success else "",
            error=message if not success else "",
            warning=warning,
        )

    def has_file_conflicts(self) -> bool:
        """Check if local and remote changes overlap at file level."""
        return self.ops.has_file_conflicts()

    def pull(self) -> OperationResult:
        """Pull from remote."""
        success, message, warning = self.ops.pull()
        return OperationResult(
            path=self.path,
            name=self.name,
            success=success,
            operation="pull",
            message=message if success else "",
            error=message if not success else "",
            warning=warning,
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

    def get_worktrees(self, max_status_workers: int = 1) -> RepositoryWorktrees:
        """Inventory linked worktrees (main worktree excluded)."""
        result = RepositoryWorktrees(path=self.path, name=self.name)
        try:
            entries = self.ops.list_worktrees()[1:]
            if not entries:
                return result

            upstream_states = self.ops.get_branch_upstream_states()
            shas = sorted({e["head"] for e in entries if e["head"].strip("0")})
            commit_infos = self.ops.get_commit_infos(shas)
            repo_root = self.path.resolve()

            for entry in entries:
                wt_path = Path(entry["path"])
                ref: str | None = entry["branch"]
                # Locked worktrees are never reported prunable, so check the directory too.
                missing = entry["prunable"] or not wt_path.is_dir()

                if missing:
                    state = WorktreeUpstreamState.MISSING
                elif ref is None:
                    state = WorktreeUpstreamState.DETACHED
                else:
                    state = upstream_states.get(ref, WorktreeUpstreamState.NONE)

                head = entry["head"]
                short_sha, commit_date = commit_infos.get(head, (head[:7], None))
                result.worktrees.append(
                    WorktreeInfo(
                        path=wt_path,
                        branch=ref.removeprefix("refs/heads/") if ref else None,
                        head=short_sha if head.strip("0") else "",
                        upstream_state=state,
                        locked=entry["locked"],
                        dirty_count=0,
                        last_commit_date=commit_date,
                        external=not wt_path.resolve().is_relative_to(repo_root),
                    )
                )

            # One repo can hold dozens of worktrees; serial `git status` dominates runtime.
            present = [
                w for w in result.worktrees if w.upstream_state != WorktreeUpstreamState.MISSING
            ]
            with ThreadPoolExecutor(max_workers=max(1, max_status_workers)) as executor:
                counts = executor.map(lambda w: GitOperations(w.path).count_status_lines(), present)
                for wt, (count, error) in zip(present, counts):
                    wt.dirty_count = count
                    wt.error = error
        except Exception as e:
            result.error = str(e)
        return result


# =============================================================================
# Fleet Manager
# =============================================================================


class FleetManager:
    """Manage multiple Git repositories."""

    def __init__(
        self,
        root_path: Path,
        max_workers: int = DEFAULT_MAX_WORKERS,
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
        # Pruned walk: rglob(".git") descended into every .git and node_modules tree.
        # followlinks=False matches rglob, which never recurses through symlinked dirs;
        # os.walk still lists a symlinked .git dir in dirnames, as rglob + is_dir() did.
        for dirpath, dirnames, _ in os.walk(self.root_path, followlinks=False):
            if ".git" in dirnames:
                repos.append(GitRepository(Path(dirpath)))
            dirnames[:] = [d for d in dirnames if d not in _DISCOVERY_PRUNED_DIRS]

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
        on_repo_done: Callable[[], None] | None = None,
    ) -> list:
        """Execute operation on repositories in parallel or sequentially."""
        if repos is None:
            repos = self.discover_repositories()

        results = []

        if sequential or len(repos) <= 1:
            for repo in repos:
                results.append(operation(repo))
                if on_repo_done:
                    on_repo_done()
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {executor.submit(operation, repo): repo for repo in repos}
                for future in as_completed(futures):
                    results.append(future.result())
                    if on_repo_done:
                        on_repo_done()

        # Sort by path for consistent ordering
        results.sort(key=lambda r: r.path if hasattr(r, "path") else str(r))
        return results

    def get_all_status(
        self,
        fetch_first: bool = True,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[RepositoryStatus]:
        """Get status of all repositories."""
        return self._execute_parallel(
            lambda repo: repo.get_status(fetch_first=fetch_first),
            sequential=sequential,
            on_repo_done=on_repo_done,
        )

    def fetch_all(
        self,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
        *,
        all_remotes: bool = True,
        prune: bool = True,
        repos: list[GitRepository] | None = None,
    ) -> list[OperationResult]:
        """Fetch all repositories (or only ``repos`` when given)."""
        return self._execute_parallel(
            lambda repo: repo.fetch(all_remotes=all_remotes, prune=prune),
            repos=repos,
            sequential=sequential,
            on_repo_done=on_repo_done,
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

    def get_all_identities(
        self,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[RepositoryIdentity]:
        """Get identity configuration for all repositories."""
        return self._execute_parallel(
            lambda repo: repo.get_identity(),
            sequential=sequential,
            on_repo_done=on_repo_done,
        )

    def get_all_remotes(
        self,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[RepositoryRemotes]:
        """Get remote configuration for all repositories."""
        return self._execute_parallel(
            lambda repo: repo.get_remotes(),
            sequential=sequential,
            on_repo_done=on_repo_done,
        )

    def get_all_diff(
        self,
        sequential: bool = False,
        dirty_only: bool = True,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[RepositoryDiff]:
        """Get file-level diff for all repositories."""
        results = self._execute_parallel(
            lambda repo: repo.get_diff(),
            sequential=sequential,
            on_repo_done=on_repo_done,
        )
        if dirty_only:
            results = [r for r in results if r.is_dirty]
        return results

    def get_all_worktrees(
        self,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[RepositoryWorktrees]:
        """Get linked worktrees for all repositories."""
        status_workers = 1 if sequential else self.max_workers
        return self._execute_parallel(
            lambda repo: repo.get_worktrees(max_status_workers=status_workers),
            sequential=sequential,
            on_repo_done=on_repo_done,
        )


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


def resolve_effective_roots(path: Path | None, roots: Path | None, console: Console) -> Path | None:
    """Resolve which roots file (if any) should drive multi-root mode.

    Precedence: --roots wins outright; an explicit path argument forces
    single-root mode (skipping roots-file auto-resolution entirely); with
    neither given, fall back to auto-resolution; otherwise None (single-root
    scan of the current directory).
    """
    if path is not None and roots is not None:
        console.print("[red]Error: PATH argument and --roots cannot be used together[/]")
        raise typer.Exit(1)

    if roots is not None:
        return roots
    if path is not None:
        return None
    return resolve_roots_file()


class MultiRootFleetManager:
    """Manage multiple Git repositories across multiple root directories."""

    def __init__(
        self,
        roots: list[Path],
        max_workers: int = DEFAULT_MAX_WORKERS,
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

    def _execute_repo_operation_across_roots(
        self,
        operation: Callable[[GitRepository], Any],
        *,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
        root_repos: list[tuple[Path, list[GitRepository]]] | None = None,
    ) -> list[tuple[Path, list[Any]]]:
        """Execute a repository operation across all roots with one worker pool."""
        if root_repos is None:
            root_repos = self.discover_all_repositories()
        results_by_root: dict[Path, list[Any]] = {root: [] for root, _ in root_repos}

        if sequential:
            for root, repos in root_repos:
                for repo in repos:
                    results_by_root[root].append(operation(repo))
                    if on_repo_done:
                        on_repo_done()
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(operation, repo): root
                    for root, repos in root_repos
                    for repo in repos
                }
                for future in as_completed(futures):
                    results_by_root[futures[future]].append(future.result())
                    if on_repo_done:
                        on_repo_done()

        for results in results_by_root.values():
            results.sort(key=lambda r: r.path if hasattr(r, "path") else str(r))

        return [(root, results_by_root[root]) for root, _ in root_repos]

    def get_all_identities(
        self,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[tuple[Path, list[RepositoryIdentity]]]:
        """Get identity configuration for all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            identities = fleet.get_all_identities(sequential=sequential, on_repo_done=on_repo_done)
            results.append((root, identities))
        return results

    def get_all_remotes(
        self,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[tuple[Path, list[RepositoryRemotes]]]:
        """Get remote configuration for all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            remotes = fleet.get_all_remotes(sequential=sequential, on_repo_done=on_repo_done)
            results.append((root, remotes))
        return results

    def get_all_diff(
        self,
        sequential: bool = False,
        dirty_only: bool = True,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[tuple[Path, list[RepositoryDiff]]]:
        """Get file-level diff for all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            diffs = fleet.get_all_diff(
                sequential=sequential, dirty_only=dirty_only, on_repo_done=on_repo_done
            )
            results.append((root, diffs))
        return results

    def get_all_status(
        self,
        fetch_first: bool = True,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[tuple[Path, list[RepositoryStatus]]]:
        """Get status for all repositories across all roots."""
        return self._execute_repo_operation_across_roots(
            lambda repo: repo.get_status(fetch_first=fetch_first),
            sequential=sequential,
            on_repo_done=on_repo_done,
        )

    def discover_all_repositories(self) -> list[tuple[Path, list[GitRepository]]]:
        """Discover all repositories across all roots."""
        results = []
        for root, fleet in self._fleet_managers.items():
            repos = fleet.discover_repositories()
            results.append((root, repos))
        return results

    def fetch_all(
        self,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
        *,
        all_remotes: bool = True,
        prune: bool = True,
        root_repos: list[tuple[Path, list[GitRepository]]] | None = None,
    ) -> list[tuple[Path, list[OperationResult]]]:
        """Fetch all repositories across all roots (or only ``root_repos`` when given)."""
        return self._execute_repo_operation_across_roots(
            lambda repo: repo.fetch(all_remotes=all_remotes, prune=prune),
            sequential=sequential,
            on_repo_done=on_repo_done,
            root_repos=root_repos,
        )

    def get_all_worktrees(
        self,
        sequential: bool = False,
        on_repo_done: Callable[[], None] | None = None,
    ) -> list[tuple[Path, list[RepositoryWorktrees]]]:
        """Get linked worktrees for all repositories across all roots."""
        status_workers = 1 if sequential else self.max_workers
        return self._execute_repo_operation_across_roots(
            lambda repo: repo.get_worktrees(max_status_workers=status_workers),
            sequential=sequential,
            on_repo_done=on_repo_done,
        )

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
        emit_json(get_tool_schema())
        raise typer.Exit()

    # If no command and no schema, show help
    if ctx.invoked_subcommand is None and not schema:
        # Let typer handle this with no_args_is_help
        pass


def get_console_and_formatter(
    json_output: bool, dry_run: bool = False
) -> tuple[Console, OutputFormatter]:
    """Create console and formatter."""
    # With --json, stdout carries only the JSON document (emit_json); every
    # diagnostic printed through this console goes to stderr instead.
    console = Console(force_terminal=not json_output, stderr=json_output)
    formatter = OutputFormatter(console, use_json=json_output, dry_run=dry_run)
    return console, formatter


def _create_progress_bar(console: Console) -> Progress:
    """Create a progress bar with bar, count, and percentage columns."""
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console,
    )


def _print_success_bullets(console: Console, results: list[OperationResult]) -> None:
    """Print successful repository names as a dim bullet list.

    Warnings and failures are skipped here because they're already surfaced
    by the existing ``⚠`` / ``✗`` lines.
    """
    for r in results:
        if r.success and not r.warning:
            console.print(f"    [dim]• {r.name}[/]")


@app.command()
def status(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories (overrides configured roots file)",
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
    dirty_only: bool = typer.Option(
        False,
        "--dirty",
        "-d",
        help=("Show only repositories not clean and in sync (display filter; ignored with --json)"),
    ),
    off_default: bool = typer.Option(
        False,
        "--off-default",
        "-o",
        help=(
            "Show only repositories on a branch other than the default branch; "
            "OR-combined with --dirty (display filter; ignored with --json)"
        ),
    ),
):
    """Show status of all repositories."""
    console, formatter = get_console_and_formatter(json_output)
    status_filter = StatusFilter(dirty=dirty_only, off_default=off_default)

    resolved_roots = resolve_effective_roots(path, roots, console)
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
            all_repos = multi_fleet.discover_all_repositories()
            total = sum(len(repos) for _, repos in all_repos)

            if not no_fetch:
                with _create_progress_bar(console) as progress:
                    task = progress.add_task("Fetching...", total=total)
                    fetch_results = multi_fleet.fetch_all(
                        sequential=sequential,
                        on_repo_done=lambda: progress.advance(task),
                    )
                fetch_total = sum(len(results) for _, results in fetch_results)
                fetch_success = sum(
                    sum(1 for r in results if r.success) for _, results in fetch_results
                )
                console.print(f"  Fetched {fetch_success}/{fetch_total} repositories")
                failed = [r for _, results in fetch_results for r in results if not r.success]
                if failed:
                    for r in failed:
                        console.print(f"    [red]✗ {r.name}: {r.error}[/]")
                warned = [
                    r for _, results in fetch_results for r in results if r.success and r.warning
                ]
                if warned:
                    for r in warned:
                        console.print(f"    [yellow]⚠ {r.name}: {r.warning}[/]")
                console.print()

            with _create_progress_bar(console) as progress:
                task = progress.add_task("Analyzing...", total=total)
                all_statuses = multi_fleet.get_all_status(
                    fetch_first=False,
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            if not no_fetch:
                multi_fleet.fetch_all(sequential=sequential)
            all_statuses = multi_fleet.get_all_status(fetch_first=False, sequential=sequential)

        summary = multi_fleet.get_summary(all_statuses)
        formatter.print_multi_root_status_list(all_statuses, summary, status_filter=status_filter)
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

            if not no_fetch:
                with _create_progress_bar(console) as progress:
                    task = progress.add_task("Fetching...", total=len(repos))
                    fetch_results = fleet.fetch_all(
                        sequential=sequential,
                        on_repo_done=lambda: progress.advance(task),
                    )
                fetch_success = sum(1 for r in fetch_results if r.success)
                console.print(f"  Fetched {fetch_success}/{len(fetch_results)} repositories")
                failed = [r for r in fetch_results if not r.success]
                if failed:
                    for r in failed:
                        console.print(f"    [red]✗ {r.name}: {r.error}[/]")
                warned = [r for r in fetch_results if r.success and r.warning]
                if warned:
                    for r in warned:
                        console.print(f"    [yellow]⚠ {r.name}: {r.warning}[/]")
                console.print()

            with _create_progress_bar(console) as progress:
                task = progress.add_task("Analyzing...", total=len(repos))
                statuses = fleet.get_all_status(
                    fetch_first=False,
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            repos = fleet.discover_repositories()
            if not no_fetch:
                fleet.fetch_all(sequential=sequential)
            statuses = fleet.get_all_status(
                fetch_first=False,
                sequential=sequential,
            )

        summary = fleet.get_summary(statuses)
        formatter.print_status_list(
            statuses, summary, target_path.resolve(), status_filter=status_filter
        )


@app.command()
def fetch(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories (overrides configured roots file)",
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

    resolved_roots = resolve_effective_roots(path, roots, console)
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
            all_repos = multi_fleet.discover_all_repositories()
            total = sum(len(repos) for _, repos in all_repos)
            with _create_progress_bar(console) as progress:
                task = progress.add_task("Fetching...", total=total)
                all_results = multi_fleet.fetch_all(
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
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
            repos = fleet.discover_repositories()
            with _create_progress_bar(console) as progress:
                task = progress.add_task("Fetching...", total=len(repos))
                results = fleet.fetch_all(
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            results = fleet.fetch_all(sequential=sequential)

        formatter.print_operation_results(results, "fetch")


@app.command()
def pull(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories (overrides configured roots file)",
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
    console, formatter = get_console_and_formatter(json_output, dry_run=dry_run)

    if force and safe:
        console.print("[red]Error: --force and --safe are mutually exclusive[/]")
        raise typer.Exit(1)

    if force:
        mode = PullMode.FORCE
    elif safe:
        mode = PullMode.SAFE
    else:
        mode = PullMode.SMART

    resolved_roots = resolve_effective_roots(path, roots, console)
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
                progress.add_task(f"{progress_label('pull', dry_run)}...", total=None)
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
                progress.add_task(f"{progress_label('pull', dry_run)}...", total=None)
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
        help="Root path to scan for repositories (overrides configured roots file)",
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
    console, formatter = get_console_and_formatter(json_output, dry_run=dry_run)

    resolved_roots = resolve_effective_roots(path, roots, console)
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
                progress.add_task(f"{progress_label('push', dry_run)}...", total=None)
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
                progress.add_task(f"{progress_label('push', dry_run)}...", total=None)
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
        help="Root path to scan for repositories (overrides configured roots file)",
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
    jobs: int = typer.Option(
        DEFAULT_MAX_WORKERS,
        "--jobs",
        "-J",
        min=1,
        help="Maximum number of repositories to process in parallel",
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
    all_remotes: bool = typer.Option(
        False,
        "--all-remotes",
        help="Fetch all remotes before syncing instead of only the upstream/origin remote",
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Prune deleted remote-tracking branches during the sync fetch step",
    ),
    dirty_only: bool = typer.Option(
        False,
        "--dirty",
        "-d",
        help=(
            "Show only repositories not clean and in sync in the final status "
            "(display filter; ignored with --json)"
        ),
    ),
    off_default: bool = typer.Option(
        False,
        "--off-default",
        "-o",
        help=(
            "Show only repositories on a branch other than the default branch; "
            "OR-combined with --dirty (display filter; ignored with --json)"
        ),
    ),
):
    """Sync all repositories: lightweight fetch, pull (smart), then push."""
    console, formatter = get_console_and_formatter(json_output, dry_run=dry_run)
    status_filter = StatusFilter(dirty=dirty_only, off_default=off_default)

    resolved_roots = resolve_effective_roots(path, roots, console)
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(
            root_paths,
            max_workers=jobs,
            include_no_remote=include_no_remote,
            include_detached=include_detached,
        )
        all_results = {"fetch": [], "pull": [], "push": []}
        all_repos = multi_fleet.discover_all_repositories()
        repo_total = sum(len(repos) for _, repos in all_repos)

        # Step 1: Fetch
        if not json_output:
            console.print("[bold]Step 1/4: Fetching all repositories...[/]")
            with _create_progress_bar(console) as progress:
                task = progress.add_task("  Fetching...", total=repo_total)
                fetch_results = multi_fleet.fetch_all(
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                    all_remotes=all_remotes,
                    prune=prune,
                )
        else:
            fetch_results = multi_fleet.fetch_all(
                sequential=sequential,
                all_remotes=all_remotes,
                prune=prune,
            )
        all_results["fetch"] = fetch_results

        if not json_output:
            total = sum(len(results) for _, results in fetch_results)
            success = sum(sum(1 for r in results if r.success) for _, results in fetch_results)
            console.print(f"  Fetched {success}/{total} repositories")
            failed = [r for _, results in fetch_results for r in results if not r.success]
            if failed:
                for r in failed:
                    console.print(f"    [red]✗ {r.name}: {r.error}[/]")
            warned = [r for _, results in fetch_results for r in results if r.success and r.warning]
            if warned:
                for r in warned:
                    console.print(f"    [yellow]⚠ {r.name}: {r.warning}[/]")
            console.print()

        # Get status once after fetch (no re-fetch needed)
        if not json_output:
            with _create_progress_bar(console) as progress:
                task = progress.add_task("  Analyzing...", total=repo_total)
                pre_statuses = multi_fleet.get_all_status(
                    fetch_first=False,
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            pre_statuses = multi_fleet.get_all_status(fetch_first=False, sequential=sequential)

        # Step 2: Pull (smart) - reuse pre-fetched statuses
        if not json_output:
            console.print(f"[bold]Step 2/4: {progress_label('pull', dry_run)} (smart)...[/]")

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
                console.print(f"  {done_label('pull', dry_run)} {success}/{total} repositories")
                _print_success_bullets(
                    console,
                    [r for _, results in pull_results for r in results],
                )
                failed = [r for _, results in pull_results for r in results if not r.success]
                if failed:
                    for r in failed:
                        console.print(f"    [red]✗ {r.name}: {r.error}[/]")
                warned = [
                    r for _, results in pull_results for r in results if r.success and r.warning
                ]
                if warned:
                    for r in warned:
                        console.print(f"    [yellow]⚠ {r.name}: {r.warning}[/]")
                console.print()
            else:
                console.print("  No repositories needed pulling\n")

        # Re-check status after pull (no fetch needed, pull changed ahead/behind)
        if not json_output:
            with _create_progress_bar(console) as progress:
                task = progress.add_task("  Analyzing...", total=repo_total)
                post_pull_statuses = multi_fleet.get_all_status(
                    fetch_first=False,
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            post_pull_statuses = multi_fleet.get_all_status(
                fetch_first=False, sequential=sequential
            )

        # Step 3: Push - reuse post-pull statuses
        if not json_output:
            console.print(f"[bold]Step 3/4: {progress_label('push', dry_run)}...[/]")

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
                console.print(f"  {done_label('push', dry_run)} {success}/{total} repositories")
                _print_success_bullets(
                    console,
                    [r for _, results in push_results for r in results],
                )
                failed = [r for _, results in push_results for r in results if not r.success]
                if failed:
                    for r in failed:
                        console.print(f"    [red]✗ {r.name}: {r.error}[/]")
                console.print()
            else:
                console.print("  No repositories needed pushing\n")

            # Step 4: Final status check
            console.print("[bold]Step 4/4: Checking final status...[/]")
            with _create_progress_bar(console) as progress:
                task = progress.add_task("  Checking...", total=repo_total)
                all_statuses = multi_fleet.get_all_status(
                    fetch_first=False,
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
            console.print()
            summary = multi_fleet.get_summary(all_statuses)
            sync_summary = SyncOperationSummary.from_multi_root_results(
                fetch_results, pull_results, push_results
            )
            formatter.print_multi_root_status_list(
                all_statuses, summary, sync_summary, status_filter=status_filter
            )

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
            emit_json(output)
        return

    # Single root mode
    target_path = path if path else Path(".")
    fleet = FleetManager(
        target_path,
        max_workers=jobs,
        include_no_remote=include_no_remote,
        include_detached=include_detached,
    )

    all_results = []
    repos = fleet.discover_repositories()
    repo_total = len(repos)

    # Step 1: Fetch
    if not json_output:
        console.print("[bold]Step 1/4: Fetching all repositories...[/]")
        with _create_progress_bar(console) as progress:
            task = progress.add_task("  Fetching...", total=repo_total)
            fetch_results = fleet.fetch_all(
                sequential=sequential,
                on_repo_done=lambda: progress.advance(task),
                all_remotes=all_remotes,
                prune=prune,
            )
    else:
        fetch_results = fleet.fetch_all(
            sequential=sequential,
            all_remotes=all_remotes,
            prune=prune,
        )
    all_results.extend(fetch_results)

    if not json_output:
        success = sum(1 for r in fetch_results if r.success)
        console.print(f"  Fetched {success}/{len(fetch_results)} repositories")
        failed = [r for r in fetch_results if not r.success]
        if failed:
            for r in failed:
                console.print(f"    [red]✗ {r.name}: {r.error}[/]")
        warned = [r for r in fetch_results if r.success and r.warning]
        if warned:
            for r in warned:
                console.print(f"    [yellow]⚠ {r.name}: {r.warning}[/]")
        console.print()

    # Get status once after fetch (no re-fetch needed)
    if not json_output:
        with _create_progress_bar(console) as progress:
            task = progress.add_task("  Analyzing...", total=repo_total)
            pre_statuses = fleet.get_all_status(
                fetch_first=False,
                sequential=sequential,
                on_repo_done=lambda: progress.advance(task),
            )
    else:
        pre_statuses = fleet.get_all_status(fetch_first=False, sequential=sequential)

    # Step 2: Pull (smart) - reuse pre-fetched statuses
    if not json_output:
        console.print(f"[bold]Step 2/4: {progress_label('pull', dry_run)} (smart)...[/]")

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
            console.print(
                f"  {done_label('pull', dry_run)} {success}/{len(pull_results)} repositories"
            )
            _print_success_bullets(console, pull_results)
            failed = [r for r in pull_results if not r.success]
            if failed:
                for r in failed:
                    console.print(f"    [red]✗ {r.name}: {r.error}[/]")
            warned = [r for r in pull_results if r.success and r.warning]
            if warned:
                for r in warned:
                    console.print(f"    [yellow]⚠ {r.name}: {r.warning}[/]")
            console.print()
        else:
            console.print("  No repositories needed pulling\n")

    # Re-check status after pull (no fetch needed, pull changed ahead/behind)
    if not json_output:
        with _create_progress_bar(console) as progress:
            task = progress.add_task("  Analyzing...", total=repo_total)
            post_pull_statuses = fleet.get_all_status(
                fetch_first=False,
                sequential=sequential,
                on_repo_done=lambda: progress.advance(task),
            )
    else:
        post_pull_statuses = fleet.get_all_status(fetch_first=False, sequential=sequential)

    # Step 3: Push - reuse post-pull statuses
    if not json_output:
        console.print(f"[bold]Step 3/4: {progress_label('push', dry_run)}...[/]")

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
            console.print(
                f"  {done_label('push', dry_run)} {success}/{len(push_results)} repositories"
            )
            _print_success_bullets(console, push_results)
            failed = [r for r in push_results if not r.success]
            if failed:
                for r in failed:
                    console.print(f"    [red]✗ {r.name}: {r.error}[/]")
            console.print()
        else:
            console.print("  No repositories needed pushing\n")

        # Step 4: Final status check
        console.print("[bold]Step 4/4: Checking final status...[/]")
        with _create_progress_bar(console) as progress:
            task = progress.add_task("  Checking...", total=repo_total)
            statuses = fleet.get_all_status(
                fetch_first=False,
                sequential=sequential,
                on_repo_done=lambda: progress.advance(task),
            )
        console.print()
        summary = fleet.get_summary(statuses)
        sync_summary = SyncOperationSummary.from_results(fetch_results, pull_results, push_results)
        formatter.print_status_list(
            statuses, summary, target_path.resolve(), sync_summary, status_filter=status_filter
        )

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
        emit_json(output)


@app.command(name="list")
def list_repos(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories (overrides configured roots file)",
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
        help="Output only paths (one per line, for piping to fzf etc.); ignored with --json",
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

    resolved_roots = resolve_effective_roots(path, roots, console)
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(root_paths)
        all_repos = multi_fleet.discover_all_repositories()

        if paths_only and not json_output:
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

        if paths_only and not json_output:
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
        help="Root path to scan for repositories (overrides configured roots file)",
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

    resolved_roots = resolve_effective_roots(path, roots, console)
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(root_paths)

        if not json_output:
            all_repos = multi_fleet.discover_all_repositories()
            total = sum(len(repos) for _, repos in all_repos)
            with _create_progress_bar(console) as progress:
                task = progress.add_task("Scanning...", total=total)
                all_identities = multi_fleet.get_all_identities(
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            all_identities = multi_fleet.get_all_identities(sequential=sequential)

        formatter.print_multi_root_identity_list(all_identities, global_identity)
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(target_path)

        if not json_output:
            repos = fleet.discover_repositories()
            with _create_progress_bar(console) as progress:
                task = progress.add_task("Scanning...", total=len(repos))
                identities = fleet.get_all_identities(
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            identities = fleet.get_all_identities(sequential=sequential)

        formatter.print_identity_list(identities, global_identity, target_path.resolve())


@app.command()
def diff(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories (overrides configured roots file)",
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

    resolved_roots = resolve_effective_roots(path, roots, console)
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
            all_repos_discovered = multi_fleet.discover_all_repositories()
            total = sum(len(repos) for _, repos in all_repos_discovered)
            total_repos_per_root = {root: len(repos) for root, repos in all_repos_discovered}
            with _create_progress_bar(console) as progress:
                task = progress.add_task("Scanning...", total=total)
                all_diffs = multi_fleet.get_all_diff(
                    sequential=sequential,
                    dirty_only=not all_repos,
                    on_repo_done=lambda: progress.advance(task),
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
            repos = fleet.discover_repositories()
            with _create_progress_bar(console) as progress:
                task = progress.add_task("Scanning...", total=len(repos))
                diffs = fleet.get_all_diff(
                    sequential=sequential,
                    dirty_only=not all_repos,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            repos = fleet.discover_repositories()
            diffs = fleet.get_all_diff(sequential=sequential, dirty_only=not all_repos)

        formatter.print_diff_list(diffs, target_path.resolve(), len(repos))


@app.command()
def remote(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories (overrides configured roots file)",
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

    resolved_roots = resolve_effective_roots(path, roots, console)
    if resolved_roots:
        # Multi-root mode
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(root_paths)

        if not json_output:
            all_repos = multi_fleet.discover_all_repositories()
            total = sum(len(repos) for _, repos in all_repos)
            with _create_progress_bar(console) as progress:
                task = progress.add_task("Scanning...", total=total)
                all_remotes = multi_fleet.get_all_remotes(
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            all_remotes = multi_fleet.get_all_remotes(sequential=sequential)

        formatter.print_multi_root_remote_list(all_remotes)
    else:
        # Single root mode
        target_path = path if path else Path(".")
        fleet = FleetManager(target_path)

        if not json_output:
            repos = fleet.discover_repositories()
            with _create_progress_bar(console) as progress:
                task = progress.add_task("Scanning...", total=len(repos))
                remotes = fleet.get_all_remotes(
                    sequential=sequential,
                    on_repo_done=lambda: progress.advance(task),
                )
        else:
            remotes = fleet.get_all_remotes(sequential=sequential)

        formatter.print_remote_list(remotes, target_path.resolve())


_T = TypeVar("_T")


def _run_with_progress(
    console: Console,
    show: bool,
    description: str,
    total: int,
    run: Callable[[Callable[[], None] | None], _T],
) -> _T:
    """Run ``run(on_repo_done)`` under its own progress bar when ``show`` is set."""
    if not show:
        return run(None)
    with _create_progress_bar(console) as progress:
        task = progress.add_task(description, total=total)
        return run(lambda: progress.advance(task))


def _print_fetch_results(console: Console, results: list[OperationResult]) -> None:
    """Print the fetch result line plus failures/warnings (same layout as `status`)."""
    success = sum(1 for r in results if r.success)
    console.print(f"  Fetched {success}/{len(results)} repositories")
    for r in results:
        if not r.success:
            console.print(f"    [red]✗ {escape(r.name)}: {escape(r.error)}[/]")
    for r in results:
        if r.success and r.warning:
            console.print(f"    [yellow]⚠ {escape(r.name)}: {escape(r.warning)}[/]")
    console.print()


@app.command()
def worktrees(
    path: Path = typer.Argument(
        None,
        help="Root path to scan for repositories (overrides configured roots file)",
    ),
    roots: Path = typer.Option(
        None,
        "--roots",
        "-r",
        help="File containing repository root paths (one per line)",
    ),
    detail: bool = typer.Option(
        False,
        "--detail",
        help="Show one table per repository listing each linked worktree",
    ),
    fetch_first: bool = typer.Option(
        False,
        "--fetch",
        help="Fetch (with prune) repositories with remotes first so gone upstreams are current",
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
):
    """Show linked worktrees per repository (read-only inventory of cleanup candidates)."""
    console, formatter = get_console_and_formatter(json_output)
    show_progress = not json_output

    resolved_roots = resolve_effective_roots(path, roots, console)
    if resolved_roots:
        root_paths = load_roots_file(resolved_roots)
        if not root_paths:
            console.print(f"[red]Error: No valid roots found in {resolved_roots}[/]")
            raise typer.Exit(1)

        multi_fleet = MultiRootFleetManager(root_paths)
        all_repos = multi_fleet.discover_all_repositories()
        total = sum(len(repos) for _, repos in all_repos)

        if fetch_first:
            fetchable = [(root, [r for r in repos if r.has_remotes()]) for root, repos in all_repos]
            fetch_results = _run_with_progress(
                console,
                show_progress,
                "Fetching...",
                sum(len(repos) for _, repos in fetchable),
                lambda done: multi_fleet.fetch_all(
                    sequential=sequential, on_repo_done=done, root_repos=fetchable
                ),
            )
            if show_progress:
                _print_fetch_results(console, [r for _, results in fetch_results for r in results])

        all_worktrees = _run_with_progress(
            console,
            show_progress,
            "Analyzing...",
            total,
            lambda done: multi_fleet.get_all_worktrees(sequential=sequential, on_repo_done=done),
        )
        formatter.print_multi_root_worktree_list(all_worktrees, detail=detail)
    else:
        target_path = path if path else Path(".")
        fleet = FleetManager(target_path)
        repos = fleet.discover_repositories()

        if fetch_first:
            fetchable_repos = [r for r in repos if r.has_remotes()]
            single_fetch_results = _run_with_progress(
                console,
                show_progress,
                "Fetching...",
                len(fetchable_repos),
                lambda done: fleet.fetch_all(
                    sequential=sequential, on_repo_done=done, repos=fetchable_repos
                ),
            )
            if show_progress:
                _print_fetch_results(console, single_fetch_results)

        repo_worktrees = _run_with_progress(
            console,
            show_progress,
            "Analyzing...",
            len(repos),
            lambda done: fleet.get_all_worktrees(sequential=sequential, on_repo_done=done),
        )
        formatter.print_worktree_list(repo_worktrees, target_path.resolve(), detail=detail)
