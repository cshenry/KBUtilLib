"""Shim — re-exports from kbutillib.domains.genome.mmseqs_utils.

The full implementation now lives in:
    kbutillib.domains.genome.mmseqs_utils

This file is kept for backwards compatibility only.  All old imports continue
to work without modification.
"""

from kbutillib.domains.genome.mmseqs_utils import *  # noqa: F401,F403
from kbutillib.domains.genome.mmseqs_utils import (  # noqa: F401
    MMSeqsUtils,
    MMSeqsUtilsImpl,
)

__all__ = [
    "MMSeqsUtils",
    "MMSeqsUtilsImpl",
]
