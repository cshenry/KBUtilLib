"""KBase platform domain — workspace, SDK, callback, narrative audit, reads.

Lazy loader: nothing is imported at ``import kbutillib`` time.
Heavy optional deps (requests_toolbelt, etc.) are only pulled when you
actually access a name from this namespace.

Example::

    from kbutillib.domains.kbase.kb_ws_utils import KBWSUtils
    from kbutillib.domains.kbase.kb_reads_utils import KBReadsUtils
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "KBWSUtils",
    "KBWSUtilsImpl",
    "KBSDKUtils",
    "KBSDKUtilsImpl",
    "KBCallbackUtils",
    "KBCallbackUtilsImpl",
    "KBReadsUtils",
    "KBReadsUtilsImpl",
    "Reads",
    "ReadSet",
    "Assembly",
    "AssemblySet",
    # narrative audit functions
    "app_run_cell_anchor",
    "is_audit_cell",
    "find_audit_cell_index",
    "extract_output_upas",
    "render_app_run_cell_markdown",
    "render_audit_cells",
    "data_dependencies_from_records",
    "compute_narrative_meta",
    "to_latest_ref",
]

_MODULE_MAP: dict[str, str] = {
    "KBWSUtils": "kb_ws_utils",
    "KBWSUtilsImpl": "kb_ws_utils",
    "KBSDKUtils": "kb_sdk_utils",
    "KBSDKUtilsImpl": "kb_sdk_utils",
    "KBCallbackUtils": "kb_callback_utils",
    "KBCallbackUtilsImpl": "kb_callback_utils",
    "KBReadsUtils": "kb_reads_utils",
    "KBReadsUtilsImpl": "kb_reads_utils",
    "Reads": "kb_reads_utils",
    "ReadSet": "kb_reads_utils",
    "Assembly": "kb_reads_utils",
    "AssemblySet": "kb_reads_utils",
    "app_run_cell_anchor": "kb_narrative_audit",
    "is_audit_cell": "kb_narrative_audit",
    "find_audit_cell_index": "kb_narrative_audit",
    "extract_output_upas": "kb_narrative_audit",
    "render_app_run_cell_markdown": "kb_narrative_audit",
    "render_audit_cells": "kb_narrative_audit",
    "data_dependencies_from_records": "kb_narrative_audit",
    "compute_narrative_meta": "kb_narrative_audit",
    "to_latest_ref": "kb_narrative_audit",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", package=__name__)
    obj = getattr(module, name)
    globals()[name] = obj
    return obj
