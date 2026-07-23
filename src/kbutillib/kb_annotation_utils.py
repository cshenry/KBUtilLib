"""Shim — re-exports from kbutillib.domains.genome.kb_annotation_utils.

The full implementation now lives in:
    kbutillib.domains.genome.kb_annotation_utils

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.genome.kb_annotation_utils import *  # noqa: F401,F403
from kbutillib.domains.genome.kb_annotation_utils import (  # noqa: F401
    KBAnnotationUtils,
    KBAnnotationUtilsImpl,
    ontology_translation,
    source_hash,
)

__all__ = [
    "KBAnnotationUtils",
    "KBAnnotationUtilsImpl",
    "source_hash",
    "ontology_translation",
]
