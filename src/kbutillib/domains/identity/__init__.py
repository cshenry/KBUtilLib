"""Identity domain — canonical entity standardization and hashing.

This package spans genome and biochem entity types (function descriptions,
protein/DNA sequences, genome assemblies, ontology terms), so it lives
under ``kbutillib.domains.identity`` rather than under either
``domains.genome`` or ``domains.biochem``.

Sub-modules:
    standardizers — STANDARDIZER_VERSION, standardize, entity_hash,
                     parse_fasta_contigs, genome_hash_from_fasta,
                     canonical_payload, content_hash
    parameter_sets — canonical_parameter_set, parameter_set_hash,
                     DEFAULT_PARAMETER_SET_HASH, ParameterSetError

Pure standard library — safe to import eagerly (no lazy-loading needed).
"""

from .parameter_sets import (
    DEFAULT_PARAMETER_SET_HASH,
    ParameterSetError,
    canonical_parameter_set,
    parameter_set_hash,
)
from .standardizers import (
    STANDARDIZER_VERSION,
    canonical_payload,
    content_hash,
    entity_hash,
    genome_hash_from_fasta,
    parse_fasta_contigs,
    standardize,
)

__all__ = [
    "STANDARDIZER_VERSION",
    "standardize",
    "entity_hash",
    "parse_fasta_contigs",
    "genome_hash_from_fasta",
    "canonical_payload",
    "content_hash",
    "canonical_parameter_set",
    "parameter_set_hash",
    "DEFAULT_PARAMETER_SET_HASH",
    "ParameterSetError",
]
