"""git-fleet: Command multiple Git repositories like a fleet admiral."""

# Guard against deleted CWD (e.g. directory removed by another process).
# rich crashes on import if os.getcwd() fails, so recover before any imports.
import os

try:
    os.getcwd()
except (OSError, PermissionError):
    os.chdir(os.path.expanduser("~"))

from ._version import __version__
from .core import (
    FleetManager,
    FleetSummary,
    GitOperations,
    GitRepository,
    GlobalIdentity,
    MultiRootFleetManager,
    OperationResult,
    PullMode,
    RepositoryDiff,
    RepositoryIdentity,
    RepositoryStatus,
    SyncStatus,
    WorkingTreeStatus,
    app,
    get_global_identity,
    load_roots_file,
)
from .formatters import OutputFormatter
from .schema import get_tool_schema

__all__ = [
    # Version
    "__version__",
    # CLI
    "app",
    # Models
    "FleetSummary",
    "GlobalIdentity",
    "OperationResult",
    "PullMode",
    "RepositoryDiff",
    "RepositoryIdentity",
    "RepositoryStatus",
    "SyncStatus",
    "WorkingTreeStatus",
    # Operations
    "FleetManager",
    "GitOperations",
    "GitRepository",
    "MultiRootFleetManager",
    # Functions
    "get_global_identity",
    "get_tool_schema",
    "load_roots_file",
    # Formatters
    "OutputFormatter",
]
