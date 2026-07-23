"""Genome domain — genome, annotation, and similarity utilities for KBUtilLib.

Sub-modules:
    kb_genome_utils     — KBGenomeUtils, KBGenomeUtilsImpl
    kb_annotation_utils — KBAnnotationUtils, KBAnnotationUtilsImpl
    mmseqs_utils        — MMSeqsUtils, MMSeqsUtilsImpl
    skani_utils         — SKANIUtils, SKANIUtilsImpl

Imports here are lazy to avoid pulling in optional heavy dependencies
(httpx, requests_toolbelt) at package-init time.
"""


def __getattr__(name: str):  # noqa: ANN001
    """Lazy attribute access — only imports sub-module symbols when first accessed."""
    import importlib

    # Mapping: public name -> (submodule, attribute_in_submodule)
    _lazy_map = {
        # kb_genome_utils
        "KBGenomeUtils": ("kb_genome_utils", "KBGenomeUtils"),
        "KBGenomeUtilsImpl": ("kb_genome_utils", "KBGenomeUtilsImpl"),
        "genetic_code_standard": ("kb_genome_utils", "genetic_code_standard"),
        # kb_annotation_utils
        "KBAnnotationUtils": ("kb_annotation_utils", "KBAnnotationUtils"),
        "KBAnnotationUtilsImpl": ("kb_annotation_utils", "KBAnnotationUtilsImpl"),
        "source_hash": ("kb_annotation_utils", "source_hash"),
        "ontology_translation": ("kb_annotation_utils", "ontology_translation"),
        # mmseqs_utils
        "MMSeqsUtils": ("mmseqs_utils", "MMSeqsUtils"),
        "MMSeqsUtilsImpl": ("mmseqs_utils", "MMSeqsUtilsImpl"),
        # skani_utils
        "SKANIUtils": ("skani_utils", "SKANIUtils"),
        "SKANIUtilsImpl": ("skani_utils", "SKANIUtilsImpl"),
    }

    if name in _lazy_map:
        submodule_name, attr = _lazy_map[name]
        module = importlib.import_module(f"kbutillib.domains.genome.{submodule_name}")
        return getattr(module, attr)

    raise AttributeError(f"module 'kbutillib.domains.genome' has no attribute {name!r}")


__all__ = [
    # kb_genome_utils
    "KBGenomeUtils",
    "KBGenomeUtilsImpl",
    "genetic_code_standard",
    # kb_annotation_utils
    "KBAnnotationUtils",
    "KBAnnotationUtilsImpl",
    "source_hash",
    "ontology_translation",
    # mmseqs_utils
    "MMSeqsUtils",
    "MMSeqsUtilsImpl",
    # skani_utils
    "SKANIUtils",
    "SKANIUtilsImpl",
]
