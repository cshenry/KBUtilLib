"""Literature domain — gene-literature research clients and shared cache.

Lazy loader: nothing is imported at ``import kbutillib`` time.  Heavy
optional deps (requests, etc.) are only pulled when you actually access a
name from this namespace.

Example::

    from kbutillib.domains.literature import EuropePMCClient, alias_query

    client = EuropePMCClient()
    hits = client.search(alias_query("dnaA", ["Acinetobacter baylyi", "ADP1"]))

    from kbutillib.domains.literature import GeneLiteratureUtils

    lit = GeneLiteratureUtils()
    annotation = lit.annotate_genes_with_papers(genes, organism)
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "DEFAULT_MENTION_THRESHOLD",
    "DEFAULT_SOURCES",
    "EVIDENCE_CLASSES",
    "Evidence",
    "EuropePMCClient",
    "EuropePMCClientImpl",
    "Gene",
    "GeneEntry",
    "GeneLiteratureUtils",
    "GeneLiteratureUtilsImpl",
    "LiteratureCache",
    "Organism",
    "PaperAnnotation",
    "PaperBLASTUtils",
    "PaperBLASTUtilsImpl",
    "PaperEntry",
    "PaperIds",
    "Provenance",
    "SourceStatus",
    "alias_query",
    "canonical_paper_id",
    "default_cache_path",
    "is_sequencing_scope",
    "merge_ids",
    "normalise_paper_id",
    "parse_stats",
    "resolve_cache_path",
    "split_db_id",
    "uniprot_accessions",
]

_MODULE_MAP: dict[str, str] = {
    "EuropePMCClient": "europepmc",
    "EuropePMCClientImpl": "europepmc",
    "alias_query": "europepmc",
    "canonical_paper_id": "europepmc",
    "merge_ids": "europepmc",
    "normalise_paper_id": "europepmc",
    "LiteratureCache": "cache",
    "default_cache_path": "cache",
    "resolve_cache_path": "cache",
    "PaperBLASTUtils": "paperblast",
    "PaperBLASTUtilsImpl": "paperblast",
    "parse_stats": "paperblast",
    "split_db_id": "paperblast",
    "DEFAULT_MENTION_THRESHOLD": "annotate",
    "DEFAULT_SOURCES": "annotate",
    "EVIDENCE_CLASSES": "annotate",
    "Evidence": "annotate",
    "Gene": "annotate",
    "GeneEntry": "annotate",
    "GeneLiteratureUtils": "annotate",
    "GeneLiteratureUtilsImpl": "annotate",
    "Organism": "annotate",
    "PaperAnnotation": "annotate",
    "PaperEntry": "annotate",
    "PaperIds": "annotate",
    "Provenance": "annotate",
    "SourceStatus": "annotate",
    "is_sequencing_scope": "annotate",
    "uniprot_accessions": "annotate",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", package=__name__)
    obj = getattr(module, name)
    globals()[name] = obj
    return obj
