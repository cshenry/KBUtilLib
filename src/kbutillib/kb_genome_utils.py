"""Shim — re-exports from kbutillib.domains.genome.kb_genome_utils.

The full implementation now lives in:
    kbutillib.domains.genome.kb_genome_utils

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.genome.kb_genome_utils import *  # noqa: F401,F403
from kbutillib.domains.genome.kb_genome_utils import (  # noqa: F401
    KBGenomeUtils,
    KBGenomeUtilsImpl,
    genetic_code_standard,
)

__all__ = [
    "KBGenomeUtils",
    "KBGenomeUtilsImpl",
    "genetic_code_standard",
]
