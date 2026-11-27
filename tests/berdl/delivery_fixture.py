"""Rows for the golden delivery fixture (``fixtures/delivery_v1``) and its builder.

Run ``python -m tests.berdl.delivery_fixture <out_dir>`` (with ``PYTHONPATH=src``)
to regenerate the checked-in fixture. The rows are fully deterministic; only
the parquet file bytes depend on the pyarrow version, which is why the manifest
also carries ``content_sha256`` and the tests compare rebuilds on that.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from kbutillib.domains.identity import parameter_set_hash
from kbutillib.domains.kbase.berdl import clearinghouse_delivery as cd
from kbutillib.domains.kbase.berdl import clearinghouse_links as cl

OBSERVED_AT = "2026-11-27T00:00:00Z"
STANDARDIZER_VERSION = "2.0"
PRODUCER = "kbutillib-golden-fixture"


def h(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


G1, G2 = sorted([h("genome-1"), h("genome-2")])
P1 = h("protein-1")
GN1 = h("gene-1")
F1, F2 = sorted([h("function-1"), h("function-2")])
T1, T2 = sorted([h("term-1"), h("term-2")])
GENE_ID = f"KBDL:{G1[:16]}.1.1"
CONTIG = h("contig-1")

M_BAKTA = "bakta@1.11.4/kbdl-default"
M_KOFAM = "kofamscan@2024-01/ko-profiles"
M_ONTO = "ontology-source@2026-11/definitions"
PARAMS_BAKTA = "{}"
PARAMS_KOFAM = '{"e_value":"1e-5"}'
PARAMS_UNKNOWN = '{"parameter_status":"unknown"}'


def _method(name, algo, version, params, source, description):
    import json

    return {
        "method_name": name,
        "algorithm": algo,
        "algorithm_version": version,
        "parameters_json": params,
        "parameter_set_hash": parameter_set_hash(json.loads(params)),
        "source_dataset": source,
        "description": description,
    }


def method_rows():
    return [
        _method(M_BAKTA, "bakta", "1.11.4", PARAMS_BAKTA, "snapshot-2026-11-01", "Bakta annotation, default parameters."),
        _method(M_KOFAM, "kofamscan", "2024-01", PARAMS_KOFAM, "export-kofam-2024-01", "KofamScan KO assignment."),
        _method(M_ONTO, "ontology-source", "2026-11", PARAMS_UNKNOWN, "a" * 64, "Ontology definitions and xrefs."),
    ]


def _entity(entity_hash, entity_type):
    return {
        "entity_hash": entity_hash,
        "entity_type": entity_type,
        "standardizer_version": STANDARDIZER_VERSION,
    }


def table_rows():
    methods = method_rows()
    registry = sorted(
        {(m["parameter_set_hash"], m["parameters_json"]) for m in methods}
    )
    src_assertion = ["GO:0000001", "exactMatch", "EC:1.1.1.1"]
    inferred_payload = {"source": {"hit": {"score": "0.98"}}}
    return [
        ("function_x_ontology_term", [
            {"subject_hash": F1, "object_hash": T1, "relation": "label", "method_name": M_ONTO, "link_key": "",
             "metadata": cl.canonical_metadata({"basis": "definition", "field": "name"})},
            {"subject_hash": F2, "object_hash": T2, "relation": "maps_to", "method_name": M_ONTO,
             "link_key": cl.canonical_json_sha256(inferred_payload),
             "metadata": cl.canonical_metadata({"basis": "inferred", "evidence": inferred_payload})},
        ]),
        ("ontology_term_x_ontology_term", [
            {"subject_hash": T1, "object_hash": T2, "relation": "xref", "method_name": M_ONTO,
             "link_key": cl.canonical_json_sha256(src_assertion),
             "metadata": cl.canonical_metadata({"source_predicate": "exactMatch", "asserting_curie": "GO:0000001", "asserted_curie": "EC:1.1.1.1"})},
        ]),
        ("function_x_function", [
            {"subject_hash": F1, "object_hash": F2, "relation": "alias", "method_name": M_ONTO, "link_key": "GO:0000001",
             "metadata": cl.canonical_metadata({"via_term": "GO:0000001"})},
        ]),
        ("protein_x_function", [
            {"subject_hash": P1, "object_hash": F1, "relation": "annotated_as", "method_name": M_KOFAM, "link_key": "KO:K00001",
             "metadata": cl.canonical_metadata({"ko_id": "KO:K00001"})},
        ]),
        ("gene_x_protein", [
            {"subject_hash": GN1, "object_hash": P1, "relation": "encodes", "method_name": M_BAKTA, "link_key": GENE_ID,
             "metadata": cl.canonical_metadata({"gene_id": GENE_ID, "translation_table": 11})},
        ]),
        ("gene_x_genome", [
            {"subject_hash": GN1, "object_hash": G1, "relation": "occurs_in", "method_name": M_BAKTA, "link_key": GENE_ID,
             "metadata": cl.canonical_metadata({"gene_id": GENE_ID, "contig_hash": CONTIG, "begin": 1, "end": 300, "strand": 1, "partial_begin": False, "partial_end": False})},
        ]),
        ("genome_x_genome", [
            {"subject_hash": G1, "object_hash": G2, "relation": "ani", "method_name": M_BAKTA, "link_key": "",
             "metadata": cl.canonical_metadata({"ani": "97.5", "af_query": "0.91", "af_reference": "0.88", "original_orientation": "reversed"})},
        ]),
        ("parameter_set", [
            {"parameter_set_hash": ph, "canonical_json": pj} for ph, pj in registry
        ]),
        ("method", methods),
        ("genome_entity", [_entity(G1, "genome"), _entity(G2, "genome")]),
        ("protein_entity", [_entity(P1, "protein")]),
        ("gene_entity", [_entity(GN1, "gene")]),
        ("function_entity", [_entity(F1, "function"), _entity(F2, "function")]),
        ("ontology_term_entity", [_entity(T1, "ontology_term"), _entity(T2, "ontology_term")]),
        ("genome_alias", [
            {"entity_hash": G1, "entity_type": "genome", "alias_source": "ncbi_assembly", "alias_id": "GCF_000000001.1", "observation": "present"},
        ]),
    ]


def build(out_dir):
    return cd.build_delivery(
        out_dir, table_rows(), producer=PRODUCER, observed_at=OBSERVED_AT,
        standardizer_version=STANDARDIZER_VERSION,
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]))
