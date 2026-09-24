"""Read-only access to the KOROS runs tree.

A domain-agnostic shared module: it enumerates KOROS projects and arcs and
parses their ``PROVENANCE.json`` into typed records, importing nothing from the
KING/KOROS backend or any of the five KING/KOROS repos. Two separate KIND apps
depend on it; nothing here is specific to either one's domain.

This package covers runs-tree access only. The run database and the CAC helpers
are separate concerns delivered in follow-on work.
"""

from __future__ import annotations

from .exceptions import (
    ContractVersionMismatch,
    KorosArcStoreError,
    RecordNotFound,
    RecordValidationError,
    RunsRootResolutionError,
)
from .records import ArcProvenance, ArcRecord, ProjectRecord, parse_provenance
from .store import KorosArcStore, resolve_runs_root

__all__ = [
    # Records
    "ProjectRecord",
    "ArcRecord",
    "ArcProvenance",
    "parse_provenance",
    # Store + resolution
    "KorosArcStore",
    "resolve_runs_root",
    # Exceptions
    "KorosArcStoreError",
    "RunsRootResolutionError",
    "RecordValidationError",
    "RecordNotFound",
    "ContractVersionMismatch",
]
