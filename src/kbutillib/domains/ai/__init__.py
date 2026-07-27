"""AI / curation domain — Argo LLM gateway, AI curation, protein language models.

Lazy loader: nothing is imported at ``import kbutillib`` time.
Heavy optional deps (httpx, requests, etc.) are only pulled when you
actually access a name from this namespace.

Example::

    from kbutillib.domains.ai.argo_utils import ArgoUtils
    from kbutillib.domains.ai.ai_curation_utils import AICurationUtils
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "ArgoUtils",
    "ArgoUtilsImpl",
    "llm_label",
    "AICurationUtils",
    "AICurationUtilsImpl",
    "KBPLMUtils",
    "KBPLMUtilsImpl",
]

_MODULE_MAP: dict[str, str] = {
    "ArgoUtils": "argo_utils",
    "ArgoUtilsImpl": "argo_utils",
    "llm_label": "argo_utils",
    "AICurationUtils": "ai_curation_utils",
    "AICurationUtilsImpl": "ai_curation_utils",
    "KBPLMUtils": "kb_plm_utils",
    "KBPLMUtilsImpl": "kb_plm_utils",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", package=__name__)
    obj = getattr(module, name)
    globals()[name] = obj
    return obj
