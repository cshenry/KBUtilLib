"""External data services domain — BV-BRC, RCSB PDB.

Lazy loader: nothing is imported at ``import kbutillib`` time.
Heavy optional deps (requests, etc.) are only pulled when you
actually access a name from this namespace.

Example::

    from kbutillib.domains.external.bvbrc_utils import BVBRCUtils
    from kbutillib.domains.external.rcsb_pdb_utils import RCSBPDBUtils
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "BVBRCUtils",
    "BVBRCUtilsImpl",
    "RCSBPDBUtils",
    "RCSBPDBUtilsImpl",
    "KBUniProtUtils",
    "KBUniProtUtilsImpl",
    "PatricWSUtils",
    "PatricWSUtilsImpl",
    "KBDLServiceUtils",
    "KBDLServiceUtilsImpl",
]

_MODULE_MAP: dict[str, str] = {
    "BVBRCUtils": "bvbrc_utils",
    "BVBRCUtilsImpl": "bvbrc_utils",
    "RCSBPDBUtils": "rcsb_pdb_utils",
    "RCSBPDBUtilsImpl": "rcsb_pdb_utils",
    "KBUniProtUtils": "kb_uniprot_utils",
    "KBUniProtUtilsImpl": "kb_uniprot_utils",
    "PatricWSUtils": "patric_ws_utils",
    "PatricWSUtilsImpl": "patric_ws_utils",
    "KBDLServiceUtils": "kbdl_service_utils",
    "KBDLServiceUtilsImpl": "kbdl_service_utils",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", package=__name__)
    obj = getattr(module, name)
    globals()[name] = obj
    return obj
