"""Identity domain — canonical entity standardization and hashing.

This package spans genome and biochem entity types (function descriptions,
protein/DNA sequences, genome assemblies, ontology terms), so it lives
under ``kbutillib.domains.identity`` rather than under either
``domains.genome`` or ``domains.biochem``.

Sub-modules:
    standardizers — STANDARDIZER_VERSION, standardize, entity_hash,
                     canonical_payload, content_hash

Pure standard library — safe to import eagerly (no lazy-loading needed).
"""

from .standardizers import (
    STANDARDIZER_VERSION,
    canonical_payload,
    content_hash,
    entity_hash,
    standardize,
)

__all__ = [
    "STANDARDIZER_VERSION",
    "standardize",
    "entity_hash",
    "canonical_payload",
    "content_hash",
]
