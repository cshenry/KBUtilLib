"""Read-only access to the KOROS runs tree.

A domain-agnostic shared module: it enumerates KOROS projects and arcs and
parses their ``PROVENANCE.json`` into typed records, importing nothing from the
KING/KOROS backend or any of the five KING/KOROS repos. Two separate KIND apps
depend on it; nothing here is specific to either one's domain.

This package covers runs-tree access, the per-user run database, and the CAC
conformance helpers.
"""

from __future__ import annotations

from .conformance import (
    CONTRACT_VERSION,
    check_contract_version,
    module_id,
)
from .exceptions import (
    ContractVersionMismatch,
    KorosArcStoreError,
    RecordNotFound,
    RecordValidationError,
    RunsRootResolutionError,
)
from .identity import (
    canonical_json,
    derive_analysis_id,
    derive_record_id,
    display_id,
)
from .records import (
    AnalysisRecord,
    ArcProvenance,
    ArcRecord,
    ProjectRecord,
    TrustTier,
    floor_tier,
    parse_provenance,
)
from .run_db import RunDatabase, resolve_db_path
from .store import KorosArcStore, resolve_runs_root

__all__ = [
    # Records
    "ProjectRecord",
    "ArcRecord",
    "ArcProvenance",
    "AnalysisRecord",
    "parse_provenance",
    # Trust tiers
    "TrustTier",
    "floor_tier",
    # Identity helpers
    "derive_analysis_id",
    "derive_record_id",
    "canonical_json",
    "display_id",
    # Store + resolution
    "KorosArcStore",
    "resolve_runs_root",
    # Run database
    "RunDatabase",
    "resolve_db_path",
    # CAC conformance helpers
    "module_id",
    "check_contract_version",
    "CONTRACT_VERSION",
    # Exceptions
    "KorosArcStoreError",
    "RunsRootResolutionError",
    "RecordValidationError",
    "RecordNotFound",
    "ContractVersionMismatch",
]
