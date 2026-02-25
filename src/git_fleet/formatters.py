"""Output formatters for console and JSON display."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.table import Table

if TYPE_CHECKING:
    from .core import (
        FleetSummary,
        GitRepository,
        GlobalIdentity,
        OperationResult,
        RepositoryDiff,
        RepositoryIdentity,
        RepositoryRemotes,
        RepositoryStatus,
        SyncOperationSummary,
    )


def compute_unique_root_names(roots: list[Path]) -> dict[Path, str]:
    """Compute unique display names for root paths.

    When multiple roots share the same directory name, parent directory
    components are added until each name becomes unique.

    Args:
        roots: List of root Path objects

    Returns:
        Dictionary mapping root path to display name
    """
    if len(roots) <= 1:
        return {root: root.name for root in roots}

    # Group by name
    name_groups: dict[str, list[Path]] = defaultdict(list)
    for root in roots:
        name_groups[root.name].append(root)

    result: dict[Path, str] = {}

    for name, group in name_groups.items():
        if len(group) == 1:
            # Unique name - use as is
            result[group[0]] = name
        else:
            # Duplicates exist - add parent directories to make unique
            unique_names = _make_paths_unique(group)
            for root, unique_name in zip(group, unique_names):
                result[root] = unique_name

    return result


def compute_unique_display_names(
    items: list[Any],
    name_attr: str = "name",
    path_attr: str = "path",
) -> dict[Path, str]:
    """Compute unique display names for items with duplicate names.

    When multiple items share the same name, parent directory components
    are added until each name becomes unique.

    Args:
        items: List of objects with name and path attributes
        name_attr: Name of the attribute containing the item name
        path_attr: Name of the attribute containing the item path

    Returns:
        Dictionary mapping path to display name
    """
    # Group by name
    name_groups: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        name = getattr(item, name_attr)
        name_groups[name].append(item)

    result: dict[Path, str] = {}

    for name, group in name_groups.items():
        if len(group) == 1:
            # Unique name - use as is
            path = getattr(group[0], path_attr)
            result[path] = name
        else:
            # Duplicates exist - add parent directories to make unique
            paths = [getattr(item, path_attr) for item in group]
            unique_names = _make_paths_unique(paths)
            for path, unique_name in zip(paths, unique_names):
                result[path] = unique_name

    return result


def _make_paths_unique(paths: list[Path]) -> list[str]:
    """Generate shortest unique display names for a list of paths.

    For each path, adds parent directory components until the name
    is unique among all paths.

    Args:
        paths: List of Path objects to make unique

    Returns:
        List of unique display names in the same order as input paths
    """
    # Split each path into parts (reversed for easier processing)
    path_parts_list = [list(reversed(p.parts)) for p in paths]

    result = []
    for i, parts in enumerate(path_parts_list):
        depth = 1
        while depth <= len(parts):
            candidate = "/".join(reversed(parts[:depth]))

            # Check if unique among all other paths
            is_unique = True
            for j, other_parts in enumerate(path_parts_list):
                if i != j:
                    other_depth = min(depth, len(other_parts))
                    other_candidate = "/".join(reversed(other_parts[:other_depth]))
                    if candidate == other_candidate:
                        is_unique = False
                        break

            if is_unique:
                result.append(candidate)
                break
            depth += 1
        else:
            # Couldn't make unique (shouldn't happen with real paths)
            result.append("/".join(reversed(parts)))

    return result


class OutputFormatter:
    """Format output for console or JSON."""

    def __init__(self, console: Console, use_json: bool = False):
        self.console = console
        self.use_json = use_json

    def print_status_list(
        self,
        statuses: list[RepositoryStatus],
        summary: FleetSummary,
        root_path: Path,
        sync_summary: SyncOperationSummary | None = None,
    ):
        """Print status list."""
        if self.use_json:
            self._print_status_json(statuses, summary)
        else:
            self._print_status_table(statuses, summary, root_path, sync_summary)

    def _get_relative_path(self, path: Path, root: Path) -> str:
        """Get relative path from root."""
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    def _print_status_table(
        self,
        statuses: list[RepositoryStatus],
        summary: FleetSummary,
        root_path: Path,
        sync_summary: SyncOperationSummary | None = None,
    ):
        """Print rich table output."""
        # Compute unique display names for duplicate repo names
        display_names = compute_unique_display_names(statuses)

        table = Table(title=f"Fleet Status: {root_path}")

        table.add_column("Repository", style="cyan", no_wrap=True)
        table.add_column("Branch")
        table.add_column("Sync", justify="center")
        table.add_column("Working Tree", justify="center")
        table.add_column("Last Commit", justify="right")

        for status in statuses:
            repo_display = display_names.get(status.path, status.name)

            # Sync status with icons
            sync_icon = self._get_sync_icon(status)

            # Working tree status
            wt_status = self._get_working_tree_display(status)

            # Last commit date
            last_commit = self._format_date(status.last_commit_date)

            # Add warning for conflict risk
            if status.has_conflict_risk:
                repo_display = f"[bold red]⚠ {repo_display}[/]"

            branch_display = self._get_branch_display(status)

            table.add_row(repo_display, branch_display, sync_icon, wt_status, last_commit)

        self.console.print(table)
        self.console.print()
        self._print_summary_table(summary, sync_summary)

    def _get_branch_display(self, status: RepositoryStatus) -> str:
        """Get branch name with color based on default branch."""
        if status.branch == status.default_branch:
            return f"[blue]{status.branch}[/]"
        return f"[green]{status.branch}[/]"

    def _get_sync_icon(self, status: RepositoryStatus) -> str:
        """Get sync status icon."""
        from .core import SyncStatus

        match status.sync_status:
            case SyncStatus.CLEAN:
                return "[green]✓[/]"
            case SyncStatus.AHEAD:
                return f"[yellow]⬆ {status.ahead_count}[/]"
            case SyncStatus.BEHIND:
                return f"[blue]⬇ {status.behind_count}[/]"
            case SyncStatus.DIVERGED:
                return f"[red]⬆{status.ahead_count} ⬇{status.behind_count}[/]"
            case SyncStatus.NO_UPSTREAM:
                return "[dim]no upstream[/]"
            case SyncStatus.DETACHED:
                return "[dim]detached[/]"
            case SyncStatus.NO_REMOTE:
                return "[dim]no remote[/]"
            case SyncStatus.ERROR:
                return f"[red]✗ {status.error_message[:20]}[/]"
            case _:
                return "[dim]?[/]"

    def _get_working_tree_display(self, status: RepositoryStatus) -> str:
        """Get working tree status display."""
        from .core import WorkingTreeStatus

        if status.working_tree_status == WorkingTreeStatus.CLEAN:
            return "[green]clean[/]"

        parts = []
        if status.staged_count > 0:
            parts.append(f"[green]+{status.staged_count}[/]")
        if status.unstaged_count > 0:
            parts.append(f"[yellow]~{status.unstaged_count}[/]")
        if status.untracked_count > 0:
            parts.append(f"[red]?{status.untracked_count}[/]")

        return " ".join(parts)

    def _format_date(self, dt: datetime | None) -> str:
        """Format datetime for display."""
        if dt is None:
            return "[dim]unknown[/]"

        now = datetime.now(dt.tzinfo)
        delta = now - dt

        if delta.days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                minutes = delta.seconds // 60
                return f"[green]{minutes}m ago[/]"
            return f"[green]{hours}h ago[/]"
        elif delta.days == 1:
            return "[green]yesterday[/]"
        elif delta.days < 7:
            return f"[yellow]{delta.days}d ago[/]"
        elif delta.days < 30:
            weeks = delta.days // 7
            return f"[yellow]{weeks}w ago[/]"
        else:
            return f"[red]{dt.strftime('%Y-%m-%d')}[/]"

    def _print_summary_table(
        self,
        summary: FleetSummary,
        sync_summary: SyncOperationSummary | None = None,
    ):
        """Print summary."""
        parts = [f"[bold]Total:[/] {summary.total}"]

        if summary.clean > 0:
            parts.append(f"[green]✓ Clean:[/] {summary.clean}")
        if summary.need_push > 0:
            parts.append(f"[yellow]⬆ Need push:[/] {summary.need_push}")
        if summary.need_pull > 0:
            parts.append(f"[blue]⬇ Need pull:[/] {summary.need_pull}")
        if summary.diverged > 0:
            parts.append(f"[red]⬆⬇ Diverged:[/] {summary.diverged}")
        if summary.dirty > 0:
            parts.append(f"[yellow]✎ Dirty:[/] {summary.dirty}")
        if summary.conflict_risk > 0:
            parts.append(f"[bold red]⚠ Conflict risk:[/] {summary.conflict_risk}")
        if summary.errors > 0:
            parts.append(f"[red]✗ Errors:[/] {summary.errors}")

        self.console.print(" | ".join(parts))

        # Print sync operation summary if provided
        if sync_summary:
            sync_parts = []
            if sync_summary.fetched > 0 or sync_summary.fetched_failed > 0:
                if sync_summary.fetched_failed > 0:
                    sync_parts.append(
                        f"[cyan]Fetched:[/] {sync_summary.fetched}"
                        f" [red]({sync_summary.fetched_failed} failed)[/]"
                    )
                else:
                    sync_parts.append(f"[cyan]Fetched:[/] {sync_summary.fetched}")
            if sync_summary.pulled > 0 or sync_summary.pulled_failed > 0:
                if sync_summary.pulled_failed > 0:
                    sync_parts.append(
                        f"[blue]Pulled:[/] {sync_summary.pulled}"
                        f" [red]({sync_summary.pulled_failed} failed)[/]"
                    )
                else:
                    sync_parts.append(f"[blue]Pulled:[/] {sync_summary.pulled}")
            if sync_summary.pushed > 0 or sync_summary.pushed_failed > 0:
                if sync_summary.pushed_failed > 0:
                    sync_parts.append(
                        f"[yellow]Pushed:[/] {sync_summary.pushed}"
                        f" [red]({sync_summary.pushed_failed} failed)[/]"
                    )
                else:
                    sync_parts.append(f"[yellow]Pushed:[/] {sync_summary.pushed}")
            if sync_parts:
                self.console.print("[bold]Synced:[/] " + " | ".join(sync_parts))

    def _print_status_json(self, statuses: list[RepositoryStatus], summary: FleetSummary):
        """Print JSON output."""
        output = {
            "repositories": [s.to_dict() for s in statuses],
            "summary": summary.to_dict(),
        }
        self.console.print(json.dumps(output, indent=2, default=str))

    def print_operation_results(self, results: list[OperationResult], operation: str):
        """Print operation results."""
        if self.use_json:
            self._print_operation_json(results)
        else:
            self._print_operation_table(results, operation)

    def _print_operation_table(self, results: list[OperationResult], operation: str):
        """Print operation results as table."""
        if not results:
            self.console.print(f"[dim]No repositories to {operation}[/]")
            return

        # Compute unique display names for duplicate repo names
        display_names = compute_unique_display_names(results)

        table = Table(title=f"{operation.title()} Results")
        table.add_column("Repository", style="cyan")
        table.add_column("Status", justify="center")
        table.add_column("Message")

        success_count = 0
        for result in results:
            repo_display = display_names.get(result.path, result.name)
            if result.success:
                success_count += 1
                status = "[green]✓[/]"
                message = result.message[:50] if result.message else "OK"
            else:
                status = "[red]✗[/]"
                message = f"[red]{result.error[:50]}[/]" if result.error else "Failed"

            table.add_row(repo_display, status, message)

        self.console.print(table)
        self.console.print(f"\n[bold]Success:[/] {success_count}/{len(results)}")

    def _print_operation_json(self, results: list[OperationResult]):
        """Print operation results as JSON."""
        output = {
            "results": [r.to_dict() for r in results],
            "summary": {
                "total": len(results),
                "success": sum(1 for r in results if r.success),
                "failed": sum(1 for r in results if not r.success),
            },
        }
        self.console.print(json.dumps(output, indent=2))

    def print_repo_list(self, repos: list[GitRepository], root_path: Path):
        """Print simple repository list."""
        if self.use_json:
            # Compute unique display names for JSON output
            display_names = compute_unique_display_names(repos)
            output = {
                "root": str(root_path),
                "count": len(repos),
                "repositories": [
                    {
                        "path": str(r.path),
                        "name": r.name,
                        "display_name": display_names.get(r.path, r.name),
                    }
                    for r in repos
                ],
            }
            self.console.print(json.dumps(output, indent=2))
        else:
            # Compute unique display names for duplicate repo names
            display_names = compute_unique_display_names(repos)
            self.console.print(f"[bold]Found {len(repos)} repositories in {root_path}[/]\n")
            for repo in repos:
                repo_display = display_names.get(repo.path, repo.name)
                self.console.print(f"  [cyan]{repo_display}[/]")

    def print_identity_list(
        self,
        identities: list[RepositoryIdentity],
        global_identity: GlobalIdentity,
        root_path: Path,
    ):
        """Print identity list for all repositories."""
        if self.use_json:
            self._print_identity_json(identities, global_identity, root_path)
        else:
            self._print_identity_table(identities, global_identity, root_path)

    def _print_identity_table(
        self,
        identities: list[RepositoryIdentity],
        global_identity: GlobalIdentity,
        root_path: Path,
    ):
        """Print identity table output."""
        # Compute unique display names for duplicate repo names
        display_names = compute_unique_display_names(identities)

        # Print global identity first
        self.console.print("[bold]Global Identity:[/]")
        self.console.print(f"  [dim]user.name:[/]  {global_identity.user_name}")
        self.console.print(f"  [dim]user.email:[/] {global_identity.user_email}")
        self.console.print()

        # Create table
        table = Table(title=f"Repository Identities: {root_path}")

        table.add_column("Repository", style="cyan", no_wrap=True)
        table.add_column("user.name")
        table.add_column("user.email")
        table.add_column("Source", justify="center")

        source_counts: dict[str, int] = {}

        for identity in identities:
            repo_display = display_names.get(identity.path, identity.name)
            source_type = identity.source

            # Count by source type
            source_counts[source_type] = source_counts.get(source_type, 0) + 1

            # Color based on source type
            if source_type == "local":
                color = "magenta"
            elif source_type == "included":
                color = "yellow"
            elif source_type == "system":
                color = "blue"
            elif source_type == "global":
                color = "dim"
            else:  # unknown
                color = "red"

            name_display = f"[{color}]{identity.user_name}[/]"
            email_display = f"[{color}]{identity.user_email}[/]"
            source_display = f"[{color}]{source_type}[/]"

            table.add_row(repo_display, name_display, email_display, source_display)

        self.console.print(table)
        self.console.print()

        # Build summary line
        summary_parts = [f"[bold]Total:[/] {len(identities)}"]
        for src, count in sorted(source_counts.items()):
            if src == "local":
                summary_parts.append(f"[magenta]Local:[/] {count}")
            elif src == "included":
                summary_parts.append(f"[yellow]Included:[/] {count}")
            elif src == "global":
                summary_parts.append(f"[dim]Global:[/] {count}")
            elif src == "system":
                summary_parts.append(f"[blue]System:[/] {count}")
            else:
                summary_parts.append(f"[red]Unknown:[/] {count}")
        self.console.print(" | ".join(summary_parts))

    def _print_identity_json(
        self,
        identities: list[RepositoryIdentity],
        global_identity: GlobalIdentity,
        root_path: Path,
    ):
        """Print identity list as JSON."""
        # Count by source type
        source_counts: dict[str, int] = {}
        for i in identities:
            source_counts[i.source] = source_counts.get(i.source, 0) + 1

        output = {
            "root": str(root_path),
            "global_identity": global_identity.to_dict(),
            "repositories": [i.to_dict() for i in identities],
            "summary": {
                "total": len(identities),
                "by_source": source_counts,
                # Backward compatibility
                "using_global": sum(1 for i in identities if not i.is_local_override),
                "local_override": sum(1 for i in identities if i.is_local_override),
            },
        }
        self.console.print(json.dumps(output, indent=2))

    def print_multi_root_identity_list(
        self,
        all_identities: list[tuple[Path, list[RepositoryIdentity]]],
        global_identity: GlobalIdentity,
    ):
        """Print identity list for multiple roots."""
        if self.use_json:
            self._print_multi_root_identity_json(all_identities, global_identity)
        else:
            self._print_multi_root_identity_table(all_identities, global_identity)

    def _print_multi_root_identity_table(
        self,
        all_identities: list[tuple[Path, list[RepositoryIdentity]]],
        global_identity: GlobalIdentity,
    ):
        """Print multi-root identity table output."""
        # Flatten all identities and compute unique display names
        all_items = [identity for _, identities in all_identities for identity in identities]
        display_names = compute_unique_display_names(all_items)

        # Compute unique root names
        roots = [root for root, _ in all_identities]
        root_names = compute_unique_root_names(roots)

        # Print global identity first
        self.console.print("[bold]Global Identity:[/]")
        self.console.print(f"  [dim]user.name:[/]  {global_identity.user_name}")
        self.console.print(f"  [dim]user.email:[/] {global_identity.user_email}")
        self.console.print()

        # Create table
        root_count = len(all_identities)
        table = Table(title=f"Repository Identities ({root_count} roots)")

        table.add_column("Root", style="yellow", no_wrap=True)
        table.add_column("Repository", style="cyan", no_wrap=True)
        table.add_column("user.name")
        table.add_column("user.email")
        table.add_column("Source", justify="center")

        source_counts: dict[str, int] = {}
        total_count = 0

        for root, identities in all_identities:
            root_name = root_names.get(root, root.name)
            for identity in identities:
                total_count += 1
                repo_display = display_names.get(identity.path, identity.name)
                source_type = identity.source

                # Count by source type
                source_counts[source_type] = source_counts.get(source_type, 0) + 1

                # Color based on source type
                if source_type == "local":
                    color = "magenta"
                elif source_type == "included":
                    color = "yellow"
                elif source_type == "system":
                    color = "blue"
                elif source_type == "global":
                    color = "dim"
                else:  # unknown
                    color = "red"

                name_display = f"[{color}]{identity.user_name}[/]"
                email_display = f"[{color}]{identity.user_email}[/]"
                source_display = f"[{color}]{source_type}[/]"

                table.add_row(root_name, repo_display, name_display, email_display, source_display)

        self.console.print(table)
        self.console.print()

        # Build summary line
        summary_parts = [f"[bold]Total:[/] {total_count}"]
        for src, count in sorted(source_counts.items()):
            if src == "local":
                summary_parts.append(f"[magenta]Local:[/] {count}")
            elif src == "included":
                summary_parts.append(f"[yellow]Included:[/] {count}")
            elif src == "global":
                summary_parts.append(f"[dim]Global:[/] {count}")
            elif src == "system":
                summary_parts.append(f"[blue]System:[/] {count}")
            else:
                summary_parts.append(f"[red]Unknown:[/] {count}")
        self.console.print(" | ".join(summary_parts))

    def _print_multi_root_identity_json(
        self,
        all_identities: list[tuple[Path, list[RepositoryIdentity]]],
        global_identity: GlobalIdentity,
    ):
        """Print multi-root identity list as JSON."""
        # Compute unique root names
        roots = [root for root, _ in all_identities]
        root_names = compute_unique_root_names(roots)

        total = 0
        using_global = 0
        local_override = 0
        source_counts: dict[str, int] = {}

        roots_data = []
        for root, identities in all_identities:
            total += len(identities)
            using_global += sum(1 for i in identities if not i.is_local_override)
            local_override += sum(1 for i in identities if i.is_local_override)
            for i in identities:
                source_counts[i.source] = source_counts.get(i.source, 0) + 1
            roots_data.append(
                {
                    "root": str(root),
                    "root_name": root_names.get(root, root.name),
                    "repositories": [i.to_dict() for i in identities],
                }
            )

        output = {
            "global_identity": global_identity.to_dict(),
            "roots": roots_data,
            "summary": {
                "total": total,
                "by_source": source_counts,
                # Backward compatibility
                "using_global": using_global,
                "local_override": local_override,
            },
        }
        self.console.print(json.dumps(output, indent=2))

    def print_multi_root_status_list(
        self,
        all_statuses: list[tuple[Path, list[RepositoryStatus]]],
        summary: FleetSummary,
        sync_summary: SyncOperationSummary | None = None,
    ):
        """Print status list for multiple roots."""
        if self.use_json:
            self._print_multi_root_status_json(all_statuses, summary)
        else:
            self._print_multi_root_status_table(all_statuses, summary, sync_summary)

    def _print_multi_root_status_table(
        self,
        all_statuses: list[tuple[Path, list[RepositoryStatus]]],
        summary: FleetSummary,
        sync_summary: SyncOperationSummary | None = None,
    ):
        """Print multi-root status table output."""
        # Flatten all statuses and compute unique display names
        all_items = [status for _, statuses in all_statuses for status in statuses]
        display_names = compute_unique_display_names(all_items)

        # Compute unique root names
        roots = [root for root, _ in all_statuses]
        root_names = compute_unique_root_names(roots)

        root_count = len(all_statuses)
        table = Table(title=f"Fleet Status ({root_count} roots)")

        table.add_column("Root", style="yellow", no_wrap=True)
        table.add_column("Repository", style="cyan", no_wrap=True)
        table.add_column("Branch")
        table.add_column("Sync", justify="center")
        table.add_column("Working Tree", justify="center")
        table.add_column("Last Commit", justify="right")

        for root, statuses in all_statuses:
            root_name = root_names.get(root, root.name)
            for status in statuses:
                sync_icon = self._get_sync_icon(status)
                wt_status = self._get_working_tree_display(status)
                last_commit = self._format_date(status.last_commit_date)

                repo_display = display_names.get(status.path, status.name)
                if status.has_conflict_risk:
                    repo_display = f"[bold red]⚠ {repo_display}[/]"

                branch_display = self._get_branch_display(status)

                table.add_row(
                    root_name, repo_display, branch_display, sync_icon, wt_status, last_commit
                )

        self.console.print(table)
        self.console.print()
        self._print_summary_table(summary, sync_summary)

    def _print_multi_root_status_json(
        self,
        all_statuses: list[tuple[Path, list[RepositoryStatus]]],
        summary: FleetSummary,
    ):
        """Print multi-root status as JSON."""
        # Compute unique root names
        roots = [root for root, _ in all_statuses]
        root_names = compute_unique_root_names(roots)

        roots_data = []
        for root, statuses in all_statuses:
            roots_data.append(
                {
                    "root": str(root),
                    "root_name": root_names.get(root, root.name),
                    "repositories": [s.to_dict() for s in statuses],
                }
            )

        output = {
            "roots": roots_data,
            "summary": summary.to_dict(),
        }
        self.console.print(json.dumps(output, indent=2, default=str))

    def print_multi_root_repo_list(
        self,
        all_repos: list[tuple[Path, list[GitRepository]]],
    ):
        """Print repository list for multiple roots."""
        # Flatten all repos and compute unique display names
        all_items = [repo for _, repos in all_repos for repo in repos]
        display_names = compute_unique_display_names(all_items)

        # Compute unique root names
        roots = [root for root, _ in all_repos]
        root_names = compute_unique_root_names(roots)

        if self.use_json:
            total = sum(len(repos) for _, repos in all_repos)
            roots_data = []
            for root, repos in all_repos:
                roots_data.append(
                    {
                        "root": str(root),
                        "root_name": root_names.get(root, root.name),
                        "count": len(repos),
                        "repositories": [
                            {
                                "path": str(r.path),
                                "name": r.name,
                                "display_name": display_names.get(r.path, r.name),
                            }
                            for r in repos
                        ],
                    }
                )
            output = {
                "roots": roots_data,
                "total": total,
            }
            self.console.print(json.dumps(output, indent=2))
        else:
            total = sum(len(repos) for _, repos in all_repos)
            self.console.print(f"[bold]Found {total} repositories in {len(all_repos)} roots[/]\n")
            for root, repos in all_repos:
                self.console.print(f"[yellow]{root_names.get(root, root.name)}[/]:")
                for repo in repos:
                    repo_display = display_names.get(repo.path, repo.name)
                    self.console.print(f"  [cyan]{repo_display}[/]")
                self.console.print()

    def print_multi_root_operation_results(
        self,
        all_results: list[tuple[Path, list[OperationResult]]],
        operation: str,
    ):
        """Print operation results for multiple roots."""
        # Flatten all results and compute unique display names
        all_items = [result for _, results in all_results for result in results]
        display_names = compute_unique_display_names(all_items)

        # Compute unique root names
        roots = [root for root, _ in all_results]
        root_names = compute_unique_root_names(roots)

        if self.use_json:
            total = sum(len(results) for _, results in all_results)
            success = sum(sum(1 for r in results if r.success) for _, results in all_results)
            roots_data = []
            for root, results in all_results:
                roots_data.append(
                    {
                        "root": str(root),
                        "root_name": root_names.get(root, root.name),
                        "results": [r.to_dict() for r in results],
                    }
                )
            output = {
                "roots": roots_data,
                "summary": {
                    "total": total,
                    "success": success,
                    "failed": total - success,
                },
            }
            self.console.print(json.dumps(output, indent=2))
        else:
            total = 0
            success_count = 0

            flat_results = []
            for root, results in all_results:
                for result in results:
                    flat_results.append((root_names.get(root, root.name), result))
                    total += 1
                    if result.success:
                        success_count += 1

            if not flat_results:
                self.console.print(f"[dim]No repositories to {operation}[/]")
                return

            table = Table(title=f"{operation.title()} Results")
            table.add_column("Root", style="yellow")
            table.add_column("Repository", style="cyan")
            table.add_column("Status", justify="center")
            table.add_column("Message")

            for root_name, result in flat_results:
                repo_display = display_names.get(result.path, result.name)
                if result.success:
                    status = "[green]✓[/]"
                    message = result.message[:50] if result.message else "OK"
                else:
                    status = "[red]✗[/]"
                    message = f"[red]{result.error[:50]}[/]" if result.error else "Failed"

                table.add_row(root_name, repo_display, status, message)

            self.console.print(table)
            self.console.print(f"\n[bold]Success:[/] {success_count}/{total}")

    def print_remote_list(
        self,
        remotes: list[RepositoryRemotes],
        root_path: Path,
    ):
        """Print remote list for all repositories."""
        if self.use_json:
            self._print_remote_json(remotes, root_path)
        else:
            self._print_remote_table(remotes, root_path)

    def _print_remote_table(
        self,
        remotes: list[RepositoryRemotes],
        root_path: Path,
    ):
        """Print remote table output."""
        # Compute unique display names for duplicate repo names
        display_names = compute_unique_display_names(remotes)

        table = Table(title=f"Repository Remotes: {root_path}")

        table.add_column("Repository", style="cyan", no_wrap=True)
        table.add_column("Remote", style="blue")
        table.add_column("URL")
        table.add_column("Protocol", justify="center")

        protocol_counts: dict[str, int] = {}
        total_repos = len(remotes)
        total_remotes = 0

        for repo_remotes in remotes:
            repo_display_name = display_names.get(repo_remotes.path, repo_remotes.name)

            if not repo_remotes.remotes:
                table.add_row(repo_display_name, "[dim]none[/]", "[dim]-[/]", "[dim]-[/]")
                protocol_counts["none"] = protocol_counts.get("none", 0) + 1
            else:
                for i, remote in enumerate(repo_remotes.remotes):
                    total_remotes += 1
                    protocol_counts[remote.protocol] = protocol_counts.get(remote.protocol, 0) + 1

                    # Color based on protocol
                    protocol_color = self._get_protocol_color(remote.protocol)
                    protocol_display = f"[{protocol_color}]{remote.protocol}[/]"

                    # Show push URL if different from fetch URL
                    url_display = remote.fetch_url
                    if remote.push_url != remote.fetch_url:
                        url_display = f"{remote.fetch_url}\n[dim]push: {remote.push_url}[/]"

                    # Only show repo name on first row
                    repo_display = repo_display_name if i == 0 else ""
                    table.add_row(repo_display, remote.name, url_display, protocol_display)

        self.console.print(table)
        self.console.print()

        # Build summary line
        summary_parts = [f"[bold]Repos:[/] {total_repos}", f"[bold]Remotes:[/] {total_remotes}"]
        for proto, count in sorted(protocol_counts.items()):
            color = self._get_protocol_color(proto)
            summary_parts.append(f"[{color}]{proto.upper()}:[/] {count}")
        self.console.print(" | ".join(summary_parts))

    def _get_protocol_color(self, protocol: str) -> str:
        """Get color for protocol display."""
        colors = {
            "ssh": "green",
            "https": "blue",
            "http": "yellow",
            "git": "cyan",
            "file": "magenta",
            "none": "dim",
            "unknown": "red",
        }
        return colors.get(protocol, "white")

    def _print_remote_json(
        self,
        remotes: list[RepositoryRemotes],
        root_path: Path,
    ):
        """Print remote list as JSON."""
        protocol_counts: dict[str, int] = {}
        for repo_remotes in remotes:
            for remote in repo_remotes.remotes:
                protocol_counts[remote.protocol] = protocol_counts.get(remote.protocol, 0) + 1
            if not repo_remotes.remotes:
                protocol_counts["none"] = protocol_counts.get("none", 0) + 1

        output = {
            "root": str(root_path),
            "repositories": [r.to_dict() for r in remotes],
            "summary": {
                "total_repos": len(remotes),
                "total_remotes": sum(len(r.remotes) for r in remotes),
                "by_protocol": protocol_counts,
            },
        }
        self.console.print(json.dumps(output, indent=2))

    def print_multi_root_remote_list(
        self,
        all_remotes: list[tuple[Path, list[RepositoryRemotes]]],
    ):
        """Print remote list for multiple roots."""
        if self.use_json:
            self._print_multi_root_remote_json(all_remotes)
        else:
            self._print_multi_root_remote_table(all_remotes)

    def _print_multi_root_remote_table(
        self,
        all_remotes: list[tuple[Path, list[RepositoryRemotes]]],
    ):
        """Print multi-root remote table output."""
        # Flatten all remotes and compute unique display names
        all_items = [
            repo_remotes for _, remotes_list in all_remotes for repo_remotes in remotes_list
        ]
        display_names = compute_unique_display_names(all_items)

        # Compute unique root names
        roots = [root for root, _ in all_remotes]
        root_names = compute_unique_root_names(roots)

        root_count = len(all_remotes)
        table = Table(title=f"Repository Remotes ({root_count} roots)")

        table.add_column("Root", style="yellow", no_wrap=True)
        table.add_column("Repository", style="cyan", no_wrap=True)
        table.add_column("Remote", style="blue")
        table.add_column("URL")
        table.add_column("Protocol", justify="center")

        protocol_counts: dict[str, int] = {}
        total_repos = 0
        total_remotes = 0

        for root, repo_remotes_list in all_remotes:
            root_name = root_names.get(root, root.name)
            for repo_remotes in repo_remotes_list:
                total_repos += 1
                repo_display_name = display_names.get(repo_remotes.path, repo_remotes.name)

                if not repo_remotes.remotes:
                    table.add_row(
                        root_name, repo_display_name, "[dim]none[/]", "[dim]-[/]", "[dim]-[/]"
                    )
                    protocol_counts["none"] = protocol_counts.get("none", 0) + 1
                else:
                    for i, remote in enumerate(repo_remotes.remotes):
                        total_remotes += 1
                        protocol_counts[remote.protocol] = (
                            protocol_counts.get(remote.protocol, 0) + 1
                        )

                        protocol_color = self._get_protocol_color(remote.protocol)
                        protocol_display = f"[{protocol_color}]{remote.protocol}[/]"

                        url_display = remote.fetch_url
                        if remote.push_url != remote.fetch_url:
                            url_display = f"{remote.fetch_url}\n[dim]push: {remote.push_url}[/]"

                        # Only show root and repo name on first row
                        root_display = root_name if i == 0 else ""
                        repo_display = repo_display_name if i == 0 else ""
                        table.add_row(
                            root_display, repo_display, remote.name, url_display, protocol_display
                        )

        self.console.print(table)
        self.console.print()

        # Build summary line
        summary_parts = [f"[bold]Repos:[/] {total_repos}", f"[bold]Remotes:[/] {total_remotes}"]
        for proto, count in sorted(protocol_counts.items()):
            color = self._get_protocol_color(proto)
            summary_parts.append(f"[{color}]{proto.upper()}:[/] {count}")
        self.console.print(" | ".join(summary_parts))

    def _print_multi_root_remote_json(
        self,
        all_remotes: list[tuple[Path, list[RepositoryRemotes]]],
    ):
        """Print multi-root remote list as JSON."""
        # Compute unique root names
        roots = [root for root, _ in all_remotes]
        root_names = compute_unique_root_names(roots)

        total_repos = 0
        total_remotes = 0
        protocol_counts: dict[str, int] = {}

        roots_data = []
        for root, repo_remotes_list in all_remotes:
            total_repos += len(repo_remotes_list)
            for repo_remotes in repo_remotes_list:
                total_remotes += len(repo_remotes.remotes)
                for remote in repo_remotes.remotes:
                    protocol_counts[remote.protocol] = protocol_counts.get(remote.protocol, 0) + 1
                if not repo_remotes.remotes:
                    protocol_counts["none"] = protocol_counts.get("none", 0) + 1

            roots_data.append(
                {
                    "root": str(root),
                    "root_name": root_names.get(root, root.name),
                    "repositories": [r.to_dict() for r in repo_remotes_list],
                }
            )

        output = {
            "roots": roots_data,
            "summary": {
                "total_repos": total_repos,
                "total_remotes": total_remotes,
                "by_protocol": protocol_counts,
            },
        }
        self.console.print(json.dumps(output, indent=2))

    def print_repo_list_with_remotes(
        self,
        repos: list[GitRepository],
        remotes: list[RepositoryRemotes],
        root_path: Path,
    ):
        """Print repository list with remote info."""
        if self.use_json:
            self._print_repo_list_with_remotes_json(repos, remotes, root_path)
        else:
            self._print_repo_list_with_remotes_table(repos, remotes, root_path)

    def _print_repo_list_with_remotes_table(
        self,
        repos: list[GitRepository],
        remotes: list[RepositoryRemotes],
        root_path: Path,
    ):
        """Print repository list with remote info as table."""
        # Compute unique display names for duplicate repo names
        display_names = compute_unique_display_names(repos)
        # Build lookup by path
        remotes_by_path = {str(r.path): r for r in remotes}

        table = Table(title=f"Repositories with Remotes: {root_path}")
        table.add_column("Repository", style="cyan", no_wrap=True)
        table.add_column("Remote", style="blue")
        table.add_column("URL")
        table.add_column("Protocol", justify="center")

        for repo in repos:
            repo_display_name = display_names.get(repo.path, repo.name)
            repo_remotes = remotes_by_path.get(str(repo.path))

            if repo_remotes and repo_remotes.remotes:
                for i, remote in enumerate(repo_remotes.remotes):
                    protocol_color = self._get_protocol_color(remote.protocol)
                    protocol_display = f"[{protocol_color}]{remote.protocol}[/]"
                    repo_display = repo_display_name if i == 0 else ""
                    table.add_row(repo_display, remote.name, remote.fetch_url, protocol_display)
            else:
                table.add_row(repo_display_name, "[dim]none[/]", "[dim]-[/]", "[dim]-[/]")

        self.console.print(table)

    def _print_repo_list_with_remotes_json(
        self,
        repos: list[GitRepository],
        remotes: list[RepositoryRemotes],
        root_path: Path,
    ):
        """Print repository list with remote info as JSON."""
        remotes_by_path = {str(r.path): r for r in remotes}

        repo_data = []
        for repo in repos:
            repo_remotes = remotes_by_path.get(str(repo.path))
            repo_data.append(
                {
                    "path": str(repo.path),
                    "name": repo.name,
                    "remotes": repo_remotes.remotes if repo_remotes else [],
                }
            )

        output = {
            "root": str(root_path),
            "count": len(repos),
            "repositories": [
                {
                    "path": str(r.path),
                    "name": r.name,
                    "remotes": (
                        remotes_by_path[str(r.path)].to_dict()["remotes"]
                        if str(r.path) in remotes_by_path
                        else []
                    ),
                }
                for r in repos
            ],
        }
        self.console.print(json.dumps(output, indent=2))

    def print_multi_root_repo_list_with_remotes(
        self,
        all_repos: list[tuple[Path, list[GitRepository]]],
        all_remotes: list[tuple[Path, list[RepositoryRemotes]]],
    ):
        """Print multi-root repository list with remote info."""
        if self.use_json:
            self._print_multi_root_repo_list_with_remotes_json(all_repos, all_remotes)
        else:
            self._print_multi_root_repo_list_with_remotes_table(all_repos, all_remotes)

    def _print_multi_root_repo_list_with_remotes_table(
        self,
        all_repos: list[tuple[Path, list[GitRepository]]],
        all_remotes: list[tuple[Path, list[RepositoryRemotes]]],
    ):
        """Print multi-root repository list with remote info as table."""
        # Flatten all repos and compute unique display names
        all_items = [repo for _, repos in all_repos for repo in repos]
        display_names = compute_unique_display_names(all_items)

        # Compute unique root names
        roots = [root for root, _ in all_repos]
        root_names = compute_unique_root_names(roots)

        # Build lookup by root and path
        remotes_lookup: dict[str, dict[str, RepositoryRemotes]] = {}
        for root, remotes_list in all_remotes:
            remotes_lookup[str(root)] = {str(r.path): r for r in remotes_list}

        total = sum(len(repos) for _, repos in all_repos)
        table = Table(title=f"Repositories with Remotes ({len(all_repos)} roots, {total} repos)")

        table.add_column("Root", style="yellow", no_wrap=True)
        table.add_column("Repository", style="cyan", no_wrap=True)
        table.add_column("Remote", style="blue")
        table.add_column("URL")
        table.add_column("Protocol", justify="center")

        for root, repos in all_repos:
            root_name = root_names.get(root, root.name)
            root_remotes = remotes_lookup.get(str(root), {})

            for repo in repos:
                repo_display_name = display_names.get(repo.path, repo.name)
                repo_remotes = root_remotes.get(str(repo.path))

                if repo_remotes and repo_remotes.remotes:
                    for i, remote in enumerate(repo_remotes.remotes):
                        protocol_color = self._get_protocol_color(remote.protocol)
                        protocol_display = f"[{protocol_color}]{remote.protocol}[/]"
                        root_display = root_name if i == 0 else ""
                        repo_display = repo_display_name if i == 0 else ""
                        table.add_row(
                            root_display,
                            repo_display,
                            remote.name,
                            remote.fetch_url,
                            protocol_display,
                        )
                else:
                    table.add_row(
                        root_name, repo_display_name, "[dim]none[/]", "[dim]-[/]", "[dim]-[/]"
                    )

        self.console.print(table)

    def _print_multi_root_repo_list_with_remotes_json(
        self,
        all_repos: list[tuple[Path, list[GitRepository]]],
        all_remotes: list[tuple[Path, list[RepositoryRemotes]]],
    ):
        """Print multi-root repository list with remote info as JSON."""
        # Compute unique root names
        roots = [root for root, _ in all_repos]
        root_names = compute_unique_root_names(roots)

        remotes_lookup: dict[str, dict[str, RepositoryRemotes]] = {}
        for root, remotes_list in all_remotes:
            remotes_lookup[str(root)] = {str(r.path): r for r in remotes_list}

        total = sum(len(repos) for _, repos in all_repos)
        roots_data = []

        for root, repos in all_repos:
            root_remotes = remotes_lookup.get(str(root), {})
            repos_data = []

            for repo in repos:
                repo_remotes = root_remotes.get(str(repo.path))
                repos_data.append(
                    {
                        "path": str(repo.path),
                        "name": repo.name,
                        "remotes": (repo_remotes.to_dict()["remotes"] if repo_remotes else []),
                    }
                )

            roots_data.append(
                {
                    "root": str(root),
                    "root_name": root_names.get(root, root.name),
                    "count": len(repos),
                    "repositories": repos_data,
                }
            )

        output = {
            "roots": roots_data,
            "total": total,
        }
        self.console.print(json.dumps(output, indent=2))

    # -----------------------------------------------------------------
    # Diff output
    # -----------------------------------------------------------------

    def print_diff_list(
        self,
        diffs: list[RepositoryDiff],
        root_path: Path,
        total_repos: int,
    ):
        """Print file-level diff list."""
        if self.use_json:
            self._print_diff_json(diffs, root_path, total_repos)
        else:
            self._print_diff_rich(diffs, root_path, total_repos)

    def _print_diff_rich(
        self,
        diffs: list[RepositoryDiff],
        root_path: Path,
        total_repos: int,
    ):
        """Print file-level diff as Rich formatted output."""
        if not diffs:
            self.console.print(f"[green]All {total_repos} repositories are clean[/]")
            return

        display_names = compute_unique_display_names(diffs)

        self.console.print(
            f"[bold]Dirty repositories: {len(diffs)}/{total_repos}[/] in {root_path}\n"
        )

        for diff in diffs:
            repo_display = display_names.get(diff.path, diff.name)
            branch_display = f" [blue]({diff.branch})[/]" if diff.branch else ""
            self.console.print(f"[bold cyan]{repo_display}[/]{branch_display}")

            if diff.staged_files:
                self.console.print("  [green]Staged:[/]")
                for status, filename in diff.staged_files:
                    self.console.print(f"    [green]{status}[/]  {filename}")

            if diff.unstaged_files:
                self.console.print("  [yellow]Unstaged:[/]")
                for status, filename in diff.unstaged_files:
                    self.console.print(f"    [yellow]{status}[/]  {filename}")

            if diff.untracked_files:
                self.console.print("  [red]Untracked:[/]")
                for filename in diff.untracked_files:
                    self.console.print(f"    [red]?[/]  {filename}")

            self.console.print()

        self._print_diff_summary(diffs, total_repos)

    def _print_diff_json(
        self,
        diffs: list[RepositoryDiff],
        root_path: Path,
        total_repos: int,
    ):
        """Print file-level diff as JSON."""
        output = {
            "root": str(root_path),
            "repositories": [d.to_dict() for d in diffs],
            "summary": self._build_diff_summary_dict(diffs, total_repos),
        }
        self.console.print(json.dumps(output, indent=2))

    def print_multi_root_diff_list(
        self,
        all_diffs: list[tuple[Path, list[RepositoryDiff]]],
        total_repos_per_root: dict[Path, int],
    ):
        """Print file-level diff for multiple roots."""
        if self.use_json:
            self._print_multi_root_diff_json(all_diffs, total_repos_per_root)
        else:
            self._print_multi_root_diff_rich(all_diffs, total_repos_per_root)

    def _print_multi_root_diff_rich(
        self,
        all_diffs: list[tuple[Path, list[RepositoryDiff]]],
        total_repos_per_root: dict[Path, int],
    ):
        """Print multi-root file-level diff as Rich formatted output."""
        all_items = [d for _, diffs in all_diffs for d in diffs]
        display_names = compute_unique_display_names(all_items)

        roots = [root for root, _ in all_diffs]
        root_names = compute_unique_root_names(roots)

        total_repos = sum(total_repos_per_root.values())
        total_dirty = len(all_items)

        if total_dirty == 0:
            self.console.print(f"[green]All {total_repos} repositories are clean[/]")
            return

        self.console.print(
            f"[bold]Dirty repositories: {total_dirty}/{total_repos} across {len(roots)} roots[/]\n"
        )

        for root, diffs in all_diffs:
            if not diffs:
                continue
            root_name = root_names.get(root, root.name)
            self.console.print(f"[bold yellow]{root_name}[/]")

            for diff in diffs:
                repo_display = display_names.get(diff.path, diff.name)
                branch_display = f" [blue]({diff.branch})[/]" if diff.branch else ""
                self.console.print(f"  [bold cyan]{repo_display}[/]{branch_display}")

                if diff.staged_files:
                    self.console.print("    [green]Staged:[/]")
                    for status, filename in diff.staged_files:
                        self.console.print(f"      [green]{status}[/]  {filename}")

                if diff.unstaged_files:
                    self.console.print("    [yellow]Unstaged:[/]")
                    for status, filename in diff.unstaged_files:
                        self.console.print(f"      [yellow]{status}[/]  {filename}")

                if diff.untracked_files:
                    self.console.print("    [red]Untracked:[/]")
                    for filename in diff.untracked_files:
                        self.console.print(f"      [red]?[/]  {filename}")

            self.console.print()

        self._print_diff_summary(all_items, total_repos)

    def _print_multi_root_diff_json(
        self,
        all_diffs: list[tuple[Path, list[RepositoryDiff]]],
        total_repos_per_root: dict[Path, int],
    ):
        """Print multi-root file-level diff as JSON."""
        roots = [root for root, _ in all_diffs]
        root_names = compute_unique_root_names(roots)

        total_repos = sum(total_repos_per_root.values())
        all_items = [d for _, diffs in all_diffs for d in diffs]

        roots_data = []
        for root, diffs in all_diffs:
            roots_data.append(
                {
                    "root": str(root),
                    "root_name": root_names.get(root, root.name),
                    "repositories": [d.to_dict() for d in diffs],
                }
            )

        output = {
            "roots": roots_data,
            "summary": self._build_diff_summary_dict(all_items, total_repos),
        }
        self.console.print(json.dumps(output, indent=2))

    def _print_diff_summary(self, diffs: list[RepositoryDiff], total_repos: int):
        """Print diff summary line."""
        total_staged = sum(len(d.staged_files) for d in diffs)
        total_unstaged = sum(len(d.unstaged_files) for d in diffs)
        total_untracked = sum(len(d.untracked_files) for d in diffs)
        parts = [f"[bold]Dirty:[/] {len(diffs)}/{total_repos}"]
        if total_staged:
            parts.append(f"[green]Staged:[/] {total_staged}")
        if total_unstaged:
            parts.append(f"[yellow]Unstaged:[/] {total_unstaged}")
        if total_untracked:
            parts.append(f"[red]Untracked:[/] {total_untracked}")
        self.console.print(" | ".join(parts))

    @staticmethod
    def _build_diff_summary_dict(diffs: list[RepositoryDiff], total_repos: int) -> dict:
        """Build diff summary dictionary for JSON output."""
        return {
            "total_repos": total_repos,
            "dirty_repos": len(diffs),
            "total_staged": sum(len(d.staged_files) for d in diffs),
            "total_unstaged": sum(len(d.unstaged_files) for d in diffs),
            "total_untracked": sum(len(d.untracked_files) for d in diffs),
        }
