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

    from kbutillib.domains.literature import GeneFactsUtils

    facts = GeneFactsUtils()
    answer = facts.gene_facts(
        genes[0], organism, annotation.for_gene(genes[0]["id"]),
        model="gpt-4o", api_type="openai", endpoint={},
    )
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "DEFAULT_MENTION_THRESHOLD",
    "DEFAULT_SOURCES",
    "EVIDENCE_CLASSES",
    "FACTS_JSON_SCHEMA",
    "FACT_TYPES",
    "MAESTRO_API_TYPE",
    "PROMPT_VERSION",
    "SCHEMA_VERSION",
    "WORKER_ENV_KEYS",
    "BatchHandle",
    "BatchManifest",
    "CapabilityError",
    "Citation",
    "Evidence",
    "EuropePMCClient",
    "EuropePMCClientImpl",
    "Fact",
    "Gene",
    "GeneEntry",
    "GeneFacts",
    "GeneFactsUtils",
    "GeneFactsUtilsImpl",
    "GeneLiteratureUtils",
    "GeneLiteratureUtilsImpl",
    "LiteratureCache",
    "MaestroBatchUtils",
    "MaestroBatchUtilsImpl",
    "Organism",
    "PaperAnnotation",
    "PaperBLASTUtils",
    "PaperBLASTUtilsImpl",
    "PaperEntry",
    "PaperIds",
    "PapersUsed",
    "Provenance",
    "RejectedFact",
    "SourceStatus",
    "alias_query",
    "build_batch_prompt",
    "canonical_paper_id",
    "collect_gene_facts_batches",
    "default_cache_path",
    "is_sequencing_scope",
    "merge_ids",
    "normalise_paper_id",
    "parse_stats",
    "resolve_cache_path",
    "split_db_id",
    "submit_gene_facts_batches",
    "uniprot_accessions",
    "verify_facts",
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
    "FACTS_JSON_SCHEMA": "facts",
    "FACT_TYPES": "facts",
    "PROMPT_VERSION": "facts",
    "SCHEMA_VERSION": "facts",
    "CapabilityError": "facts",
    "Citation": "facts",
    "Fact": "facts",
    "GeneFacts": "facts",
    "GeneFactsUtils": "facts",
    "GeneFactsUtilsImpl": "facts",
    "PapersUsed": "facts",
    "RejectedFact": "facts",
    "verify_facts": "facts",
    "MAESTRO_API_TYPE": "maestro_batch",
    "WORKER_ENV_KEYS": "maestro_batch",
    "BatchHandle": "maestro_batch",
    "BatchManifest": "maestro_batch",
    "MaestroBatchUtils": "maestro_batch",
    "MaestroBatchUtilsImpl": "maestro_batch",
    "build_batch_prompt": "maestro_batch",
    "collect_gene_facts_batches": "maestro_batch",
    "submit_gene_facts_batches": "maestro_batch",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_MAP.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f".{module_name}", package=__name__)
    obj = getattr(module, name)
    globals()[name] = obj
    return obj
