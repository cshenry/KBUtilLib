"""lw-9: the KBDL GENOME LOADER.

``load_kbdl_genomes`` turns KBDL ``Genome`` objects (CanonicalGenome JSON) and
their ``KBDLGenomeAnnotation`` results into clearinghouse rows and writes them
through ``register`` / ``register_aliases`` / ``register_methods`` /
``register_links``. It is a PUBLIC function: load additional genomes as they
come up in projects. The ``kbu clearinghouse load-kbdl-genomes`` verb wraps it.

THE RULE: no relationship between two entities is written into a
``<type>_result`` payload. Every relationship produced here is a link row of
``gene_x_genome``, ``gene_x_protein`` or ``protein_x_function`` under a
``method`` row. Every emitted row is run through lw-1's
:func:`~...clearinghouse_links.validate_link_rows` /
``validate_method_rows``; those rules are not re-implemented here.

WHAT A GENOME BECOMES
    genome        v2.0 hash over the object's contig sequences; content
                  ``fasta_reference`` = fasta object id (when the ledgers name
                  it), ``n_contigs``, ``total_length``; aliases ``kbdl_object``
                  and (when ``external_ids`` supplies them) ``ncbi_assembly``,
                  ``fitness_org``, ``img_taxon``.
    gene          entity/content from the feature's ``dna_sequence``, which is
                  CHECKED against the contig slice its location names
                  (reverse-complemented on ``-``); a mismatch is reported and
                  the feature skipped.
    protein       entity/content from ``protein_translation``.
    links         ``gene_x_genome`` (occurs_in; coordinates), ``gene_x_protein``
                  (encodes), both keyed by the namespaced gene id, under the
                  gene-call method; ``protein_x_function`` (annotated_as) per
                  annotation tool, under that tool's method.

GENE IDS are ``KBDL:<genome_hash[:16]>.<contig ordinal>.<cds ordinal>`` -- the
form the lw-1 validator enforces. The contig ordinal is the contig's 1-based
position in the object's ``contigs``; the cds ordinal is the feature's 1-based
rank among the single-location features on that contig ordered by
``(begin, end, feature id)``, assigned BEFORE any sequence check so a skipped
feature never renumbers its neighbours. The KBDL feature id and the source id
are kept in the ``gene_x_genome`` metadata (``feature_id``, ``source_id``).

METHODS. KBDL recorded no tool versions; the operator stated that every run
used the CURRENT tools. :data:`KBDL_ANNOTATOR_METHODS` is the lw-12 registry
(measured on poplar 2026-11-27 at KBDL revision 6839e84...) and is the ONLY
source of the methods this loader cites. A tool absent from it has its links
refused and reported; a version is never invented.

METHOD-NAME MAPPING (the registry's own ``method_name`` strings are upper-case
and TRANSYT's carries ``/ : @``, so they fail the lw-1 pattern
``^[a-z0-9][a-z0-9._-]*@[A-Za-z0-9._+-]+/[a-z0-9][a-z0-9._-]*$``). The loaded
name is ``<tool lower-cased, underscores kept>@<version>/kbdl-default``; for
TRANSYT the version in the NAME is ``sha256-<64 hex digest>``. Normalising the
NAME invents no version: ``algorithm``, ``algorithm_version`` (TRANSYT: the full
image reference; SOURCE_GFF: ``'unknown'``) and ``parameters_json`` keep the
measured raw values. See :data:`KBDL_ANNOTATOR_METHODS` (``registry_method_name``
holds the raw string).

IDEMPOTENT: methods, entities, content, aliases and links are all fill-only;
a re-run writes zero rows. CONCURRENT with other waves: each genome is one
batch under the existing namespace writer lock, and every entity is emitted in
the same batch as its first link. ``dry_run=True`` builds and validates every
row and reports counts; it touches no lake.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...identity import canonical_parameter_set, parameter_set_hash
from ...identity.standardizers import STANDARDIZER_VERSION, entity_hash, standardize
from . import clearinghouse_links as cl
from . import clearinghouse_schema as schema
from .clearinghouse_genome_inputs import (
    DEFAULT_ARCHIVE_DIRS,
    DEFAULT_OBJECT_DIR,
    DEFAULT_SWEEP_LEDGER,
    AnnotationArchiveIndex,
    GenomeInputError,
    fetch_genome_objects,
    live_results_for_prefix,
    read_genome_object,
    read_term_function_map,
)
from .clearinghouse_schema import CLEARINGHOUSE_NAMESPACE

__all__ = [
    "KBDL_ANNOTATOR_METHODS",
    "KBDL_ANNOTATOR_METHODS_MEASURED_AT",
    "KBDL_ANNOTATOR_METHODS_KBDL_REVISION",
    "KBDL_ANNOTATOR_METHODS_SHA256",
    "GENE_CALL_TOOLS",
    "ANNOTATION_RESULT_TOOLS",
    "TERM_FUNCTION_MAP_ENV",
    "GenomeObjectError",
    "classify_function_entry",
    "annotator_method_row",
    "annotator_method_rows",
    "GenomeRows",
    "GenomeReport",
    "LoadReport",
    "build_genome_rows",
    "load_kbdl_genomes",
]

#: When the registry was measured, and at which KBDL checkout.
KBDL_ANNOTATOR_METHODS_MEASURED_AT = "2026-11-27T20:19:24Z"
KBDL_ANNOTATOR_METHODS_KBDL_REVISION = "6839e84989ba0aa7d46cfaffe4c5a1d858440167"
#: sha256 of the lw-12 registry file this constant was checked in from
#: (KBERDL agent-io/prds/clearinghouse-link-waves-v1/research/kbdl-annotator-methods.json);
#: it is the ``source_dataset`` of every method row.
KBDL_ANNOTATOR_METHODS_SHA256 = "e937e88fc4461b62682780bf29c8abff2c52c16fa5d46b48b5a6fce397acb779"

TERM_FUNCTION_MAP_ENV = "KBU_CLEARINGHOUSE_TERM_FUNCTION_MAP"

#: tool -> registry entry. ``method_name`` is the NORMALISED name that passes
#: the lw-1 pattern; ``registry_method_name`` is the registry's raw string.
KBDL_ANNOTATOR_METHODS: dict[str, dict[str, Any]] = {
    'PROKKA': {
        'method_name': 'prokka@1.14.6/kbdl-default',
        'registry_method_name': 'PROKKA@1.14.6/kbdl-default',
        'algorithm': 'Prokka',
        'algorithm_version': '1.14.6',
        'version_kind': 'tool-version',
        'db_version': None,
        'image': 'kbutillib/prokka:latest',
        'image_digest': 'sha256:240c09ae8cc461790103d7e26e81d65ab518028230882e4513d2df169573c1c3',
        'parameters_json': '{"force":true,"gcode":11,"input":"one contig per nucleotide CDS","prefix":"prokka","quiet":true}',
    },
    'BAKTA': {
        'method_name': 'bakta@1.11.4/kbdl-default',
        'registry_method_name': 'BAKTA@1.11.4/kbdl-default',
        'algorithm': 'Bakta protein annotation',
        'algorithm_version': '1.11.4',
        'version_kind': 'tool-version',
        'db_version': '6.0',
        'image': 'kbutillib/bakta:latest',
        'image_digest': 'sha256:6718c6d0a9412eb4bbf345c7512141722464f7d6acdb9639de18c19188837dff',
        'parameters_json': '{"db_type":"full","ec_role_resolver":"absent","input":"proteins","ontology_description_maps":[]}',
    },
    'KOFAMSCAN': {
        'method_name': 'kofamscan@1.3.0/kbdl-default',
        'registry_method_name': 'KOFAMSCAN@1.3.0/kbdl-default',
        'algorithm': 'KofamScan',
        'algorithm_version': '1.3.0',
        'version_kind': 'tool-version',
        'db_version': '2025-11-03',
        'image': 'kbutillib/kofamscan:latest',
        'image_digest': 'sha256:e5321178f7e31c5f8c4c3ab1de0d571ca3cc706fccb5c5d7151ecc4a83ae4e15',
        'parameters_json': '{"input":"proteins","ko_function_map_kegg_release":"109.1","ko_list_version":"2025-11-03","ontology_description_maps":[],"output_format":"detail-tsv","profile_set":"2025-11-03","retain_significant_hits_only":true}',
    },
    'DRAM2': {
        'method_name': 'dram2@2.0.0-beta17/kbdl-default',
        'registry_method_name': 'DRAM2@2.0.0-beta17/kbdl-default',
        'algorithm': 'DRAM2',
        'algorithm_version': '2.0.0-beta17',
        'version_kind': 'tool-version',
        'db_version': 'kofam,dbcan,merops,vog',
        'image': None,
        'image_digest': None,
        'parameters_json': '{"annotate":true,"databases":["kofam","dbcan","merops","vog"],"extra_config_sha256":"f6dd8e69370364f1bc3822016a2b9f84718f59a20c940841b69a3f74c4f2e044","input":"proteins","nextflow_version":"24.10.5","profile":"conda"}',
    },
    'TRANSYT': {
        'method_name': 'transyt@sha256-92f6e78fbdb8dc1952ad0c4130776f4fe6f5cf524b3f4f3cbd30b3ddbe8c4fe7/kbdl-default',
        'registry_method_name': 'TRANSYT@kbutillib/transyt:latest@sha256:92f6e78fbdb8dc1952ad0c4130776f4fe6f5cf524b3f4f3cbd30b3ddbe8c4fe7/kbdl-default',
        'algorithm': 'TranSyT',
        'algorithm_version': 'kbutillib/transyt:latest@sha256:92f6e78fbdb8dc1952ad0c4130776f4fe6f5cf524b3f4f3cbd30b3ddbe8c4fe7',
        'version_kind': 'artifact-digest',
        'db_version': None,
        'image': 'kbutillib/transyt:latest',
        'image_digest': 'sha256:92f6e78fbdb8dc1952ad0c4130776f4fe6f5cf524b3f4f3cbd30b3ddbe8c4fe7',
        'parameters_json': '{"blast_bit_score":"50","blast_evalue":"1e-5","blast_identity":"50","blast_query_coverage":"50","blastp_db":"NULL","input":"proteins","jar_mode":3,"reference_database":"ModelSEED","tax_id":"required per genome"}',
    },
    'RAST_API': {
        'method_name': 'rast_api@2018-0531/kbdl-default',
        'registry_method_name': 'RAST_API@2018-0531/kbdl-default',
        'algorithm': 'RAST tutorial API',
        'algorithm_version': '2018-0531',
        'version_kind': 'tool-version',
        'db_version': '2018-0531',
        'image': None,
        'image_digest': None,
        'parameters_json': '{"input":"proteins","kmer_dataset":"2018-0531","rpc_method":"GenomeAnnotation.run_pipeline","split_terms":true,"stages":[{"kmer_v2_parameters":{},"name":"annotate_proteins_kmer_v2"},{"name":"annotate_proteins_similarity","similarity_parameters":{"annotate_hypothetical_only":1}}]}',
    },
    'BAKTA_CALL': {
        'method_name': 'bakta_call@1.11.4/kbdl-default',
        'registry_method_name': 'BAKTA_CALL@1.11.4/kbdl-default',
        'algorithm': 'Bakta structural gene calling',
        'algorithm_version': '1.11.4',
        'version_kind': 'tool-version',
        'db_version': '6.0',
        'image': 'kbutillib/bakta:1.11.4',
        'image_digest': 'sha256:6718c6d0a9412eb4bbf345c7512141722464f7d6acdb9639de18c19188837dff',
        'parameters_json': '{"auto_route":"bare FASTA outside classified Archaea","call_non_coding":true,"cds_overlap_discard_fraction":"0.5","db_type":"full","gene_caller":"auto","translation_table":11}',
    },
    'PYRODIGAL': {
        'method_name': 'pyrodigal@3.7.1/kbdl-default',
        'registry_method_name': 'PYRODIGAL@3.7.1/kbdl-default',
        'algorithm': 'Pyrodigal CDS calling',
        'algorithm_version': '3.7.1',
        'version_kind': 'tool-version',
        'db_version': None,
        'image': None,
        'image_digest': None,
        'parameters_json': '{"auto_route":"bare FASTA classified as Archaea","call_non_coding":true,"cds_overlap_drop_fraction":"0.5","explicit_inprocess_supported":true,"gene_caller":"auto","meta":false,"meta_fallback":false,"translation_table":11}',
    },
    'PYARAGORN': {
        'method_name': 'pyaragorn@0.3.0/kbdl-default',
        'registry_method_name': 'PYARAGORN@0.3.0/kbdl-default',
        'algorithm': 'PyARAGORN tRNA and tmRNA calling',
        'algorithm_version': '0.3.0',
        'version_kind': 'tool-version',
        'db_version': None,
        'image': None,
        'image_digest': None,
        'parameters_json': '{"call_non_coding":true,"linear":true,"translation_table":11}',
    },
    'PYBARRNAP': {
        'method_name': 'pybarrnap@0.5.2/kbdl-default',
        'registry_method_name': 'PYBARRNAP@0.5.2/kbdl-default',
        'algorithm': 'PyBarrnap rRNA calling',
        'algorithm_version': '0.5.2',
        'version_kind': 'tool-version',
        'db_version': None,
        'image': None,
        'image_digest': None,
        'parameters_json': '{"call_non_coding":true,"kingdoms_auto_archaea":["arc"],"kingdoms_explicit_inprocess":["bac","arc"],"quiet":true}',
    },
    'SOURCE_GFF': {
        'method_name': 'source_gff@unknown/kbdl-default',
        'registry_method_name': 'SOURCE_GFF@unknown/kbdl-default',
        'algorithm': 'Source-provided GFF feature import',
        'algorithm_version': 'unknown',
        'version_kind': None,
        'db_version': None,
        'image': None,
        'image_digest': None,
        'parameters_json': '{"ab_initio_gene_calling":false,"input":"FASTA plus GFF","source_calls":true,"translation_table":11}',
    },
}

#: Registry tools that may be a genome's gene-call method.
GENE_CALL_TOOLS = ("SOURCE_GFF", "PYRODIGAL", "BAKTA_CALL")
#: A result's tool key -> the registry tool. RAST's results say ``RAST``.
RESULT_TOOL_ALIASES = {"RAST": "RAST_API"}
#: Registry tools whose result links this loader writes.
ANNOTATION_RESULT_TOOLS = ("PROKKA", "BAKTA", "KOFAMSCAN", "DRAM2", "RAST_API")

#: Free-text namespaces (no classification), and the classified mixed ones.
_TEXT_NAMESPACES = {("BAKTA", "FUNCTION"), ("RAST_API", "role")}
_MIXED_NAMESPACES = {("PROKKA", "FUNCTION"), ("DRAM2", "FUNCTION")}
#: Term-id namespaces: (tool, namespace) -> the kind the values must classify as.
_TERM_NAMESPACES = {
    ("BAKTA", "GO"): "go",
    ("BAKTA", "EC"): "ec",
    ("BAKTA", "KO"): "ko",
    ("BAKTA", "COG"): "cog",
    ("KOFAMSCAN", "KO"): "ko",
}
_CURIE_PREFIX = {"go": "", "ec": "EC:", "ko": "KO:", "cog": "COG:"}

#: The EXPLICIT classification patterns for a mixed FUNCTION-list entry.
_PATTERNS = (
    ("ko", re.compile(r"^K\d{5}$")),
    ("cog", re.compile(r"^COG\d{4}$")),
    ("go", re.compile(r"^GO:\d{7}$")),
    ("ec", re.compile(r"^\d+\.(\d+|-)\.(\d+|-)\.(\d+|-|n\d+)$")),
    ("vog", re.compile(r"^VOG\d+$")),
)
_WHITESPACE = re.compile(r"\s")
_COMPLEMENT = str.maketrans("ACGTUNRYKMSWBDHVacgtunrykmswbdhv", "TGCAANYRMKSWVHDBtgcaanyrmkswvhdb")


class GenomeObjectError(ValueError):
    """A genome object is malformed. Nothing was written."""


# -- classification -----------------------------------------------------------


def classify_function_entry(value: str) -> tuple[str, str]:
    """Classify one entry of a mixed FUNCTION list by the explicit patterns.

    Returns ``(kind, value)`` with kind one of ``ko``, ``cog``, ``go``, ``ec``
    (term ids), ``vog`` (not loaded), ``symbol`` (a single token with no
    whitespace: a gene symbol, not loaded) or ``text`` (a function text).
    """
    text = value.strip()
    for kind, pattern in _PATTERNS:
        if pattern.match(text):
            return kind, text
    if text and not _WHITESPACE.search(text):
        return "symbol", text
    return "text", text


def _term_curie(kind: str, value: str) -> str:
    return f"{_CURIE_PREFIX[kind]}{value}"


# -- methods ------------------------------------------------------------------


def annotator_method_row(tool: str) -> dict[str, Any]:
    """The ``method`` row for a registry tool (no clock columns).

    Raises:
        KeyError: the tool is not in :data:`KBDL_ANNOTATOR_METHODS`.
    """
    entry = KBDL_ANNOTATOR_METHODS[tool]
    params = json.loads(entry["parameters_json"])
    bits = [
        f"{entry['algorithm']} {entry['algorithm_version']} as run by the KBDL service with its default parameters",
        f"version_kind {entry['version_kind']}",
    ]
    if entry["db_version"]:
        bits.append(f"db {entry['db_version']}")
    if entry["image_digest"]:
        bits.append(f"image {entry['image']} {entry['image_digest']}")
    bits.append(
        f"registry measured {KBDL_ANNOTATOR_METHODS_MEASURED_AT} on poplar at KBDL "
        f"{KBDL_ANNOTATOR_METHODS_KBDL_REVISION[:12]}; KBDL recorded no per-run tool versions, "
        "the operator stated every run used the current tools"
    )
    return {
        "method_name": entry["method_name"],
        "algorithm": entry["algorithm"],
        "algorithm_version": entry["algorithm_version"],
        "parameters_json": canonical_parameter_set(params),
        "parameter_set_hash": parameter_set_hash(params),
        "source_dataset": KBDL_ANNOTATOR_METHODS_SHA256,
        "description": "; ".join(bits) + ".",
    }


def annotator_method_rows(tools: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Method rows for ``tools`` (default: every registry tool), validated."""
    names = list(tools) if tools is not None else list(KBDL_ANNOTATOR_METHODS)
    methods, _registry = cl.validate_method_rows([annotator_method_row(t) for t in names])
    return methods


# -- reports ------------------------------------------------------------------


@dataclass
class GenomeReport:
    genome_object_id: str
    genome_hash: str | None = None
    genome_id: str | None = None
    gene_call_tool: str | None = None
    gene_call_tool_defaulted: bool = False
    features: int = 0
    genes_loaded: int = 0
    proteins_loaded: int = 0
    genes_without_annotation: int = 0
    annotation_results_found: list[str] = field(default_factory=list)
    annotation_results_used: list[str] = field(default_factory=list)
    annotation_source: str = "none"
    skips: Counter = field(default_factory=Counter)
    skipped_features: dict[str, list[str]] = field(default_factory=dict)
    refused_tools: Counter = field(default_factory=Counter)
    rows: dict[str, int] = field(default_factory=dict)
    rows_by_method: dict[str, dict[str, int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    written: dict[str, int] = field(default_factory=dict)
    already_present: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "genome_object_id": self.genome_object_id,
            "genome_hash": self.genome_hash,
            "genome_id": self.genome_id,
            "gene_call_tool": self.gene_call_tool,
            "gene_call_tool_defaulted": self.gene_call_tool_defaulted,
            "features": self.features,
            "genes_loaded": self.genes_loaded,
            "proteins_loaded": self.proteins_loaded,
            "genes_without_annotation": self.genes_without_annotation,
            "annotation_results_found": list(self.annotation_results_found),
            "annotation_results_used": list(self.annotation_results_used),
            "annotation_source": self.annotation_source,
            "skips": dict(sorted(self.skips.items())),
            "skipped_features": {k: list(v) for k, v in sorted(self.skipped_features.items())},
            "refused_tools": dict(sorted(self.refused_tools.items())),
            "rows": dict(sorted(self.rows.items())),
            "rows_by_method": {m: dict(sorted(t.items())) for m, t in sorted(self.rows_by_method.items())},
            "notes": list(self.notes),
            "written": dict(sorted(self.written.items())),
            "already_present": dict(sorted(self.already_present.items())),
        }


@dataclass
class LoadReport:
    """What :func:`load_kbdl_genomes` did (or, under ``dry_run``, would build)."""

    dry_run: bool
    namespace: str
    genomes: list[GenomeReport] = field(default_factory=list)
    #: ids that could not be loaded at all, with the reason. Nothing written for them.
    failed: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _sum(dicts: Sequence[Mapping[str, int]]) -> dict[str, int]:
        total: Counter = Counter()
        for d in dicts:
            total.update(d)
        return dict(sorted(total.items()))

    @property
    def rows_per_table(self) -> dict[str, int]:
        return self._sum([g.rows for g in self.genomes])

    @property
    def rows_per_method(self) -> dict[str, dict[str, int]]:
        out: dict[str, Counter] = {}
        for g in self.genomes:
            for method, tables in g.rows_by_method.items():
                out.setdefault(method, Counter()).update(tables)
        return {m: dict(sorted(c.items())) for m, c in sorted(out.items())}

    @property
    def skips(self) -> dict[str, int]:
        return self._sum([g.skips for g in self.genomes])

    @property
    def written(self) -> dict[str, int]:
        return self._sum([g.written for g in self.genomes])

    @property
    def already_present(self) -> dict[str, int]:
        return self._sum([g.already_present for g in self.genomes])

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "namespace": self.namespace,
            "genomes_loaded": len(self.genomes),
            "genomes_failed": dict(sorted(self.failed.items())),
            "rows_per_table": self.rows_per_table,
            "rows_per_method": self.rows_per_method,
            "skips": self.skips,
            "written": self.written,
            "already_present": self.already_present,
            "genomes": [g.to_dict() for g in self.genomes],
        }


# -- the pure row builder ------------------------------------------------------


@dataclass
class GenomeRows:
    """Everything built for ONE genome. Nothing here has been written."""

    genome_object_id: str
    genome_hash: str
    #: ``<type>_entity`` / ``<type>_content`` rows, without the clock columns.
    entity_rows: dict[str, list[dict[str, Any]]]
    content_rows: dict[str, list[dict[str, Any]]]
    #: genome alias rows ``{entity_hash, alias_source, alias_id}``.
    alias_rows: list[dict[str, str]]
    methods: list[dict[str, Any]]
    #: validated link rows per joining table (``metadata`` canonical JSON text).
    links: dict[str, list[dict[str, Any]]]
    report: GenomeReport


def _revcomp(sequence: str) -> str:
    return sequence.translate(_COMPLEMENT)[::-1]


def _clean(sequence: Any) -> str:
    return standardize("gene", sequence) if isinstance(sequence, str) else ""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_object(genome: Any) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(genome, Mapping):
        raise GenomeObjectError("a genome object is a JSON object")
    genome_id = genome.get("genome_id")
    contigs = genome.get("contigs")
    features = genome.get("features")
    if not isinstance(genome_id, str) or not genome_id:
        raise GenomeObjectError("genome object has no genome_id")
    if not isinstance(contigs, list) or not contigs:
        raise GenomeObjectError("genome object has no contigs")
    if not isinstance(features, list):
        raise GenomeObjectError("genome object has no features list")
    for index, contig in enumerate(contigs):
        if not isinstance(contig, Mapping) or not isinstance(contig.get("id"), str) or not isinstance(contig.get("sequence"), str):
            raise GenomeObjectError(f"contig {index} needs a string id and sequence")
    ids = [c["id"] for c in contigs]
    if len(set(ids)) != len(ids):
        raise GenomeObjectError("duplicate contig ids")
    return genome_id, list(contigs), list(features)


def _merge_results(results: Sequence[Mapping[str, Any]], feature_id: str) -> dict[str, dict[str, list[str]]]:
    """Union of every result's assertions for one gene: tool -> ns -> values."""
    merged: dict[str, dict[str, list[str]]] = {}
    for result in results:
        entry = result.get(feature_id)
        if not isinstance(entry, Mapping):
            continue
        for tool, namespaces in entry.items():
            if not isinstance(namespaces, Mapping):
                continue
            for namespace, values in namespaces.items():
                if not isinstance(values, list):
                    continue
                bucket = merged.setdefault(str(tool), {}).setdefault(str(namespace), [])
                for value in values:
                    if isinstance(value, str) and value not in bucket:
                        bucket.append(value)
    return merged


def build_genome_rows(
    genome: Mapping[str, Any],
    *,
    genome_object_id: str,
    annotation_results: Sequence[Mapping[str, Any]] = (),
    term_function_map: Mapping[str, Mapping[str, str]],
    external: Mapping[str, Any] | None = None,
) -> GenomeRows:
    """Build and validate every row for one genome. Pure: no I/O, no writes.

    Raises:
        GenomeObjectError: a malformed object, an unknown ``gene_call_tool``.
        LinkRefusedError: a built row broke an lw-1 rule (a bug in the builder).
    """
    external = dict(external or {})
    report = GenomeReport(genome_object_id=genome_object_id)
    genome_id, contigs, features = _parse_object(genome)
    report.genome_id = genome_id
    report.features = len(features)

    # -- genome
    contig_clean = [_clean(c["sequence"]) for c in contigs]
    if not any(contig_clean):
        raise GenomeObjectError("every contig is empty")
    genome_hash = entity_hash("genome", [c["sequence"] for c in contigs])
    report.genome_hash = genome_hash
    contig_hash = [hashlib.sha256(s.encode("utf-8")).hexdigest() for s in contig_clean]
    contig_index = {c["id"]: i for i, c in enumerate(contigs)}

    # -- the gene-call method
    call_tool = external.get("gene_call_tool")
    if call_tool is None:
        call_tool = "PYRODIGAL"
        report.gene_call_tool_defaulted = True
        report.notes.append(
            "no gene_call_tool in external_ids: defaulted to PYRODIGAL (the 'else' of the contract); "
            "pass gene_call_tool='SOURCE_GFF' for a genome built from the source's own calls"
        )
    if call_tool not in GENE_CALL_TOOLS:
        raise GenomeObjectError(f"gene_call_tool {call_tool!r} must be one of {list(GENE_CALL_TOOLS)}")
    report.gene_call_tool = call_tool
    call_method = KBDL_ANNOTATOR_METHODS[call_tool]["method_name"]
    translation_table = int(json.loads(KBDL_ANNOTATOR_METHODS[call_tool]["parameters_json"]).get("translation_table", 11))

    # -- classify the features and assign gene ids (before any sequence check)
    single: list[tuple[int, int, int, str, dict[str, Any]]] = []  # contig idx, begin, end, id, parsed
    for position, feature in enumerate(features):
        if not isinstance(feature, Mapping):
            report.skips["feature_not_an_object"] += 1
            continue
        fid = feature.get("id")
        if not isinstance(fid, str) or not fid:
            report.skips["feature_without_id"] += 1
            continue
        locations = feature.get("location")
        if not isinstance(locations, list) or not locations:
            report.skips["feature_without_location"] += 1
            report.skipped_features.setdefault("feature_without_location", []).append(fid)
            continue
        if len(locations) != 1:
            report.skips["multi_location_feature"] += 1
            report.skipped_features.setdefault("multi_location_feature", []).append(fid)
            continue
        loc = locations[0]
        if not (isinstance(loc, list) and len(loc) == 4 and isinstance(loc[0], str) and _is_int(loc[1]) and _is_int(loc[3])):
            report.skips["malformed_location"] += 1
            report.skipped_features.setdefault("malformed_location", []).append(fid)
            continue
        contig_id, start, strand_raw, length = loc
        if contig_id not in contig_index:
            report.skips["unknown_contig"] += 1
            report.skipped_features.setdefault("unknown_contig", []).append(fid)
            continue
        strand = {"+": 1, 1: 1, "-": -1, -1: -1}.get(strand_raw) if not isinstance(strand_raw, bool) else None
        if strand is None or start < 1 or length < 2:
            report.skips["malformed_location"] += 1
            report.skipped_features.setdefault("malformed_location", []).append(fid)
            continue
        begin, end = (start, start + length - 1) if strand == 1 else (start - length + 1, start)
        if begin < 1:
            report.skips["malformed_location"] += 1
            report.skipped_features.setdefault("malformed_location", []).append(fid)
            continue
        single.append((contig_index[contig_id], begin, end, fid, {"feature": feature, "strand": strand}))
    ordinals: dict[str, tuple[int, int]] = {}
    counters: Counter = Counter()
    for cidx, begin, end, fid, _parsed in sorted(single, key=lambda t: (t[0], t[1], t[2], t[3])):
        counters[cidx] += 1
        ordinals[fid] = (cidx + 1, counters[cidx])

    # -- annotation merge and the per-gene loop
    results = list(annotation_results)
    term_map = dict(term_function_map)
    entity_rows: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in schema.ENTITY_TYPES}
    content_rows: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in schema.ENTITY_TYPES}
    function_display: dict[str, str] = {}
    gxg: list[dict[str, Any]] = []
    gxp: list[dict[str, Any]] = []
    pxf: list[dict[str, Any]] = []
    methods_used: set[str] = set()

    def add_entity(etype: str, h: str, content: Mapping[str, Any]) -> None:
        entity_rows[etype].setdefault(
            h, {"entity_hash": h, "entity_type": etype, "standardizer_version": STANDARDIZER_VERSION}
        )
        content_rows[etype].setdefault(
            h, {"entity_hash": h, **content, "standardizer_version": STANDARDIZER_VERSION}
        )

    for _cidx, begin, end, fid, parsed in sorted(single, key=lambda t: (t[0], t[1], t[2], t[3])):
        feature = parsed["feature"]
        strand = parsed["strand"]
        contig = contigs[_cidx]
        sequence = _clean(feature.get("dna_sequence"))
        protein = _clean(feature.get("protein_translation"))
        if not sequence or not protein:
            report.skips["feature_without_sequence"] += 1
            report.skipped_features.setdefault("feature_without_sequence", []).append(fid)
            continue
        full = contig_clean[_cidx]
        if end > len(full):
            report.skips["location_outside_contig"] += 1
            report.skipped_features.setdefault("location_outside_contig", []).append(fid)
            continue
        piece = full[begin - 1 : end]
        expected = piece if strand == 1 else _revcomp(piece)
        if expected != sequence:
            report.skips["dna_sequence_mismatch"] += 1
            report.skipped_features.setdefault("dna_sequence_mismatch", []).append(fid)
            continue
        contig_ordinal, cds_ordinal = ordinals[fid]
        gene_id = f"KBDL:{genome_hash[:16]}.{contig_ordinal}.{cds_ordinal}"
        gene_hash = entity_hash("gene", sequence)
        protein_hash = entity_hash("protein", protein)
        add_entity("gene", gene_hash, {"sequence": sequence, "seq_length": len(sequence), "protein_entity_hash": None})
        add_entity("protein", protein_hash, {"sequence": protein, "seq_length": len(protein)})
        gxg.append(
            {
                "subject_hash": gene_hash,
                "object_hash": genome_hash,
                "relation": "occurs_in",
                "method_name": call_method,
                "link_key": gene_id,
                "metadata": cl.canonical_metadata(
                    {
                        "gene_id": gene_id,
                        "contig_hash": contig_hash[_cidx],
                        "begin": begin,
                        "end": end,
                        "strand": strand,
                        "partial_begin": False,
                        "partial_end": False,
                        "feature_id": fid,
                        "source_id": feature.get("source_id") or fid,
                    }
                ),
            }
        )
        gxp.append(
            {
                "subject_hash": gene_hash,
                "object_hash": protein_hash,
                "relation": "encodes",
                "method_name": call_method,
                "link_key": gene_id,
                "metadata": cl.canonical_metadata({"gene_id": gene_id, "translation_table": translation_table}),
            }
        )
        methods_used.add(call_method)
        report.genes_loaded += 1

        merged = _merge_results(results, fid)
        if not merged:
            report.genes_without_annotation += 1
        for raw_tool, namespaces in sorted(merged.items()):
            tool = RESULT_TOOL_ALIASES.get(raw_tool, raw_tool)
            for namespace, values in sorted(namespaces.items()):
                where = f"{tool}/{namespace}"
                if tool == "TRANSYT":
                    report.skips[f"transyt_not_loaded:{where}"] += len(values)
                    continue
                if tool not in KBDL_ANNOTATOR_METHODS or tool not in ANNOTATION_RESULT_TOOLS:
                    report.skips[f"tool_not_in_registry:{raw_tool}"] += len(values)
                    report.refused_tools[raw_tool] += len(values)
                    continue
                if tool == "KOFAMSCAN" and namespace == "FUNCTION":
                    report.skips["kofamscan_function_ignored"] += len(values)
                    continue
                key = (tool, namespace)
                if key not in _TEXT_NAMESPACES and key not in _MIXED_NAMESPACES and key not in _TERM_NAMESPACES:
                    report.skips[f"unsupported_namespace:{where}"] += len(values)
                    continue
                method = KBDL_ANNOTATOR_METHODS[tool]["method_name"]
                for value in values:
                    if key in _TEXT_NAMESPACES:
                        kind, item = "text", value.strip()
                    else:
                        kind, item = classify_function_entry(value)
                        if key in _TERM_NAMESPACES and kind != _TERM_NAMESPACES[key]:
                            report.skips[f"malformed_term_id:{where}"] += 1
                            continue
                    if not item:
                        report.skips[f"empty_value:{where}"] += 1
                        continue
                    if kind == "vog":
                        report.skips[f"vog_not_loaded:{where}"] += 1
                        continue
                    if kind == "symbol":
                        report.skips[f"gene_symbol_not_loaded:{where}"] += 1
                        continue
                    if kind == "text":
                        if schema.is_stoplisted_function(item):
                            report.skips[f"stoplisted_function_text:{where}"] += 1
                            continue
                        fh = entity_hash("function", item)
                        prior = function_display.get(fh)
                        if prior is None or item < prior:
                            function_display[fh] = item
                        pxf.append(
                            {
                                "subject_hash": protein_hash,
                                "object_hash": fh,
                                "relation": "annotated_as",
                                "method_name": method,
                                "link_key": "",
                                "metadata": "{}",
                            }
                        )
                        methods_used.add(method)
                        continue
                    curie = standardize("ontology_term", _term_curie(kind, item))
                    row = term_map.get(curie)
                    if row is None:
                        report.skips[f"term_absent_from_map:{where}"] += 1
                        continue
                    if row.get("status") != "loaded" or not row.get("function_entity_hash"):
                        report.skips[f"term_function_text_{row.get('status') or 'unusable'}:{where}"] += 1
                        continue
                    fh = row["function_entity_hash"]
                    function_display.setdefault(fh, row["standardized_text"])
                    meta: dict[str, Any] = {"term_id": curie}
                    if tool == "KOFAMSCAN":
                        meta["ko_id"] = curie
                    pxf.append(
                        {
                            "subject_hash": protein_hash,
                            "object_hash": fh,
                            "relation": "annotated_as",
                            "method_name": method,
                            "link_key": curie,
                            "metadata": cl.canonical_metadata(meta),
                        }
                    )
                    methods_used.add(method)

    # function entity / content rows for every function a link names
    linked_functions = {r["object_hash"] for r in pxf}
    for fh in sorted(linked_functions):
        add_entity("function", fh, {"definition": function_display[fh]})

    # genome entity / content
    fasta_ref = external.get("fasta_object_id")
    if not fasta_ref:
        report.notes.append("fasta_reference unknown (no fasta_object_id in external_ids): content.fasta_reference is NULL")
    assembly = external.get("ncbi_assembly")
    if not isinstance(assembly, str):
        assembly = sorted(assembly)[0] if assembly else None
    add_entity(
        "genome",
        genome_hash,
        {
            "fasta_reference": fasta_ref or None,
            "taxon_id": str(external["taxon_id"]) if external.get("taxon_id") not in (None, "") else None,
            "assembly_accession": assembly or None,
            "n_contigs": sum(1 for s in contig_clean if s),
            "total_length": sum(len(s) for s in contig_clean),
            "is_closed": None,
        },
    )
    aliases = [{"entity_hash": genome_hash, "alias_source": "kbdl_object", "alias_id": genome_object_id}]
    for source in ("ncbi_assembly", "fitness_org", "img_taxon"):
        value = external.get(source)
        for alias_id in ([value] if isinstance(value, str) else list(value or ())):
            if alias_id:
                aliases.append({"entity_hash": genome_hash, "alias_source": source, "alias_id": str(alias_id)})
    for alias in aliases:
        schema.validate_alias("genome", alias["entity_hash"], alias["alias_source"], alias["alias_id"], "present")

    # -- validate every table with lw-1's rules
    links: dict[str, list[dict[str, Any]]] = {}
    for table, rows in (
        (schema.GENE_X_GENOME, gxg),
        (schema.GENE_X_PROTEIN, gxp),
        (schema.PROTEIN_X_FUNCTION, pxf),
    ):
        checked = cl.validate_link_rows(table, rows)
        links[table] = checked["rows"]
    used_tools = sorted(t for t, e in KBDL_ANNOTATOR_METHODS.items() if e["method_name"] in methods_used)
    methods = annotator_method_rows(used_tools)

    report.proteins_loaded = len(entity_rows["protein"])
    for table, rows in links.items():
        report.rows[table] = len(rows)
        for row in rows:
            report.rows_by_method.setdefault(row["method_name"], {}).setdefault(table, 0)
            report.rows_by_method[row["method_name"]][table] += 1
    report.rows["method"] = len(methods)
    report.rows["genome_alias"] = len(aliases)
    for etype in schema.ENTITY_TYPES:
        if entity_rows[etype]:
            report.rows[f"{etype}_entity"] = len(entity_rows[etype])
            report.rows[f"{etype}_content"] = len(content_rows[etype])
    return GenomeRows(
        genome_object_id=genome_object_id,
        genome_hash=genome_hash,
        entity_rows={t: list(rows.values()) for t, rows in entity_rows.items()},
        content_rows={t: list(rows.values()) for t, rows in content_rows.items()},
        alias_rows=aliases,
        methods=methods,
        links=links,
        report=report,
    )


# -- writing ------------------------------------------------------------------

#: Link tables in dependency order (endpoints are entities written just before).
_LINK_ORDER = (schema.GENE_X_GENOME, schema.GENE_X_PROTEIN, schema.PROTEIN_X_FUNCTION)
#: Entity types in dependency order.
_ENTITY_ORDER = ("genome", "gene", "protein", "function")


def _write_genome(cap: Any, rows: GenomeRows, *, tenant: str, dataset: str) -> None:
    """Write one genome as ONE batch under the namespace writer lock.

    Order: methods, entities + content, aliases, links. Every step is
    fill-only, so a re-run (or a concurrent wave that wrote the same entity)
    appends nothing it does not have to.
    """
    report = rows.report
    lock = cap._writer_lock(tenant=tenant, dataset=dataset)  # noqa: SLF001
    try:
        observed_at, batch_id = cap._alias_clock()  # noqa: SLF001

        methods = cap._register_methods(rows.methods, tenant=tenant, dataset=dataset)  # noqa: SLF001
        report.written["method"] = len(methods["written"])
        report.already_present["method"] = len(methods["already_present"])

        for etype in _ENTITY_ORDER:
            for kind, source in (("entity", rows.entity_rows), ("content", rows.content_rows)):
                wanted = source[etype]
                if not wanted:
                    continue
                held = cap._held_values(  # noqa: SLF001
                    schema.table_name(etype, kind), "entity_hash", sorted(r["entity_hash"] for r in wanted)
                )
                fresh = [r for r in wanted if r["entity_hash"] not in held]
                label = f"{etype}_{kind}"
                report.already_present[label] = len(wanted) - len(fresh)
                report.written[label] = len(fresh)
                if fresh:
                    columns = schema.column_names(etype, kind)
                    cap.register(
                        etype,
                        [
                            {c: {**r, "observed_at": observed_at, "ingest_batch_id": batch_id}.get(c) for c in columns}
                            for r in fresh
                        ],
                        kind=kind,
                        tenant=tenant,
                        dataset=dataset,
                    )

        alias_report = cap.register_aliases("genome", rows.alias_rows, tenant=tenant, dataset=dataset)
        report.written["genome_alias"] = len(alias_report["written"])
        report.already_present["genome_alias"] = len(alias_report["already_present"])

        for table in _LINK_ORDER:
            if not rows.links.get(table):
                continue
            result = cap._register_links(  # noqa: SLF001
                table, rows.links[table], methods=None, tenant=tenant, dataset=dataset
            )
            report.written[table] = result["written"]
            report.already_present[table] = result["already_present"]
    finally:
        lock.release()


# -- the public entry point ----------------------------------------------------


def _split_namespace(namespace: str) -> tuple[str, str]:
    tenant, sep, dataset = namespace.partition(".")
    if not sep or not tenant or not dataset or "." in dataset:
        raise ValueError(f"namespace {namespace!r} must be '<tenant>.<dataset>'")
    return tenant, dataset


def load_kbdl_genomes(
    genome_object_ids: Sequence[str],
    *,
    namespace: str = CLEARINGHOUSE_NAMESPACE,
    external_ids: Mapping[str, Mapping[str, Any]] | None = None,
    dry_run: bool = False,
    term_function_map: str | Path | Mapping[str, Mapping[str, str]] | None = None,
    genome_objects: Mapping[str, Mapping[str, Any]] | None = None,
    object_dir: str | Path | None = None,
    fetch_missing: bool = False,
    archive_dirs: Sequence[str | Path] | None = None,
    extra_annotation_files: Sequence[str | Path] = (),
    index_cache_path: str | Path | None = None,
    live_fallback: bool = False,
    sweep_ledger: str | Path = DEFAULT_SWEEP_LEDGER,
    capability: Any = None,
) -> LoadReport:
    """Load KBDL genomes (and their annotation results) into the clearinghouse.

    Args:
        genome_object_ids: KBDL ``Genome`` object ids (CanonicalGenome JSON).
        namespace: ``<tenant>.<dataset>`` write target; default production.
        external_ids: ``{genome_object_id: {ncbi_assembly, fitness_org,
            img_taxon, fasta_object_id, taxon_id, gene_call_tool}}`` (see
            :func:`~...clearinghouse_genome_inputs.build_external_ids`). A genome
            absent from it loads with only its ``kbdl_object`` alias and the
            ``PYRODIGAL`` gene-call method (reported as a default).
        dry_run: build and validate every row, report counts, touch no lake.
        term_function_map: W1's persisted TERM -> function-text TSV (path) or
            the parsed mapping; default ``$KBU_CLEARINGHOUSE_TERM_FUNCTION_MAP``.
            REQUIRED: a missing map would silently drop every GO/EC/KO/COG link.
        genome_objects: already-parsed objects keyed by id (skips the disk).
        object_dir: where ``<id>.json`` objects are cached
            (default :data:`DEFAULT_OBJECT_DIR`).
        fetch_missing: download a missing object through KBDL (read-only;
            works only where KBDL answers).
        archive_dirs / extra_annotation_files / index_cache_path: where to look
            for annotation results (default: the sweeper archive and its
            Dropbox mirror).
        live_fallback: for a genome with nothing archived, fetch the jobs the
            sweep ledger names for its prefix through KBDL.
        capability: a :class:`ClearinghouseCapability` (default: a new one).

    Returns:
        A :class:`LoadReport`.

    Raises:
        ValueError: a bad namespace or a missing term map.
        BerdlLoadRefusedError: a real run off-pod (nothing is written).
    """
    tenant, dataset = _split_namespace(namespace)
    if term_function_map is None:
        term_function_map = os.environ.get(TERM_FUNCTION_MAP_ENV)
    if term_function_map is None:
        raise ValueError(
            "term_function_map is required (W1's persisted term -> function-text TSV): "
            f"pass it or set ${TERM_FUNCTION_MAP_ENV}. Without it every GO/EC/KO/COG link "
            "would be dropped silently."
        )
    term_map = (
        read_term_function_map(term_function_map)
        if isinstance(term_function_map, (str, Path))
        else dict(term_function_map)
    )
    ids = list(dict.fromkeys(genome_object_ids))
    cap = None
    if not dry_run:
        if capability is None:
            from .clearinghouse_capability import ClearinghouseCapability  # noqa: PLC0415

            capability = ClearinghouseCapability()
        cap = capability
        cap._require_in_pod("load_kbdl_genomes")  # noqa: SLF001 - refuse off-pod before any work

    directory = Path(object_dir or DEFAULT_OBJECT_DIR).expanduser()
    index = AnnotationArchiveIndex(
        archive_dirs if archive_dirs is not None else DEFAULT_ARCHIVE_DIRS,
        extra_files=extra_annotation_files,
        cache_path=index_cache_path,
    )
    report = LoadReport(dry_run=dry_run, namespace=namespace)
    external_ids = external_ids or {}
    for object_id in ids:
        try:
            if genome_objects is not None and object_id in genome_objects:
                genome = genome_objects[object_id]
            else:
                path = directory / f"{object_id}.json"
                if not path.exists() and fetch_missing:
                    fetch_genome_objects([object_id], directory)
                if not path.exists():
                    raise GenomeInputError(
                        f"genome object {object_id} is not in {directory} (pass genome_objects=, "
                        "download it with fetch_genome_objects where KBDL answers, or use fetch_missing=True)"
                    )
                genome = read_genome_object(path)
            prefix = genome.get("genome_id") if isinstance(genome, Mapping) else None
            files = index.files_for(prefix) if isinstance(prefix, str) else []
            results = [AnnotationArchiveIndex.load(f) for f in files]
            names = [Path(f).name for f in files]
            source = "archive" if files else "none"
            if not files and live_fallback and isinstance(prefix, str):
                live = live_results_for_prefix(prefix, ledger_path=sweep_ledger)
                results = [r for _jid, r in live]
                names = [f"live:{jid}" for jid, _r in live]
                source = "live" if live else "none"
            built = build_genome_rows(
                genome,
                genome_object_id=object_id,
                annotation_results=results,
                term_function_map=term_map,
                external=external_ids.get(object_id),
            )
        except (GenomeInputError, GenomeObjectError, cl.LinkRefusedError, ValueError) as exc:
            report.failed[object_id] = f"{type(exc).__name__}: {exc}"
            continue
        built.report.annotation_results_found = names
        built.report.annotation_results_used = list(names)
        built.report.annotation_source = source
        if not names:
            built.report.notes.append(
                "no annotation result found for this genome's gene-id prefix: genes and proteins load with no function links"
            )
        if not dry_run:
            _write_genome(cap, built, tenant=tenant, dataset=dataset)
        report.genomes.append(built.report)
    return report
