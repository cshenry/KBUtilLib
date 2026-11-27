"""lw-3: the W1 ontology loader.

Reads the CDM ontology store (``kbase.ontology_source.statements``, a triple
store) at ONE pinned Iceberg snapshot, plus a pinned KEGG KO list and a pinned
NCBI COG definitions file, and PRODUCES ROWS. It writes NOTHING to the lake: a
caller hands the rows to ``register('ontology_term', ...)``,
``register('function', ...)``, ``register_methods`` and ``register_links``.
(:func:`write_term_function_map` writes one local TSV, the persisted
term -> function-text map; that is a run artifact, not a lake write.)

THE RULE: no relationship between two entities is written into a
``<type>_result`` payload. Every relationship produced here is a link row of
one of three joining tables -- ``function_x_ontology_term``,
``function_x_function`` and ``ontology_term_x_ontology_term`` -- under a
``method`` row. Every emitted row is run through lw-1's
:func:`~kbutillib.domains.kbase.berdl.clearinghouse_links.validate_link_rows` /
``validate_method_rows`` before it leaves :func:`build_ontology_wave`; the
rules are not re-implemented here.

Entity and content rows carry no ``observed_at`` / ``ingest_batch_id``: the
write path owns the clock (as it does for links and methods).
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ...identity import canonical_parameter_set, parameter_set_hash
from ...identity.standardizers import STANDARDIZER_VERSION, entity_hash, standardize
from . import clearinghouse_links as cl
from . import clearinghouse_schema as schema

__all__ = [
    "ONTOLOGIES",
    "STATEMENTS_TABLE",
    "KEGG_KO_FETCH_DATE",
    "KO_FILE_SHA256",
    "COG_RELEASE",
    "COG_FILE_SHA256",
    "SYNONYM_PREDICATES",
    "UNUSED_SYNONYM_PREDICATES",
    "TERM_FUNCTION_MAP_COLUMNS",
    "PinnedFileError",
    "SnapshotResolutionError",
    "KoEntry",
    "CogEntry",
    "OntologyWaveResult",
    "read_pinned_text",
    "parse_ko_lines",
    "load_ko_file",
    "parse_cog_def_lines",
    "load_cog_file",
    "ko_function_text",
    "map_xref",
    "select_literal",
    "build_ontology_wave",
    "write_term_function_map",
    "resolve_snapshot_id",
    "fetch_statements",
    "berdl_query_fn",
    "run_ontology_wave",
]

STATEMENTS_TABLE = "kbase.ontology_source.statements"

#: The KEGG fetch date baked into the KO method names, and the staged file's
#: sha256 (research/kegg-ko-list-2026-11-24.tsv).
KEGG_KO_FETCH_DATE = "2026-11-24"
KO_FILE_SHA256 = "e8389cac2e990b0f45452cc01a05ab2ccaad98a1bb4106f9c4f1870d32d2ca70"

#: The staged NCBI COG definitions (research/cog-24.def.tab, COG2024 release).
COG_RELEASE = "cog-24"
COG_FILE_SHA256 = "f4f0007b356e81dac2afc46d062411bc65dce4de6d3c5d6ce4779687a7193de3"

#: Editable. ``prefixes`` are SUBJECT prefixes in the store (matched after the
#: ontology_term standardizer is applied to both sides); ``function`` says how
#: the ontology contributes function text:
#:   ``label_synonym`` -- rdfs:label ('label') and each oio:hasExactSynonym;
#:   ``description``   -- Pfam: labels are short names, only the selected
#:                        description is function text (relation 'definition');
#:   ``ko`` / ``cog``  -- from the pinned KEGG / COG file, not the store;
#:   ``None``          -- terms only, no function text.
#: ``source`` is ``statements`` (the store) or the pinned file kind.
ONTOLOGIES: dict[str, dict[str, Any]] = {
    "GO": {"prefixes": ["GO:"], "function": "label_synonym", "source": "statements"},
    "EC": {"prefixes": ["EC:"], "function": "label_synonym", "source": "statements"},
    "SEED_ROLE": {
        "prefixes": ["seed.role:", "<https://modelseed.org/template/roles/"],
        "function": "label_synonym",
        "source": "statements",
    },
    "InterPro": {"prefixes": ["IPR:"], "function": "label_synonym", "source": "statements"},
    "Pfam": {"prefixes": ["PFAM:"], "function": "description", "source": "statements"},
    "KO": {"prefixes": ["KO:"], "function": "ko", "source": "kegg-ko"},
    "COG": {"prefixes": ["COG:"], "function": "cog", "source": "ncbi-cog"},
    "Rhea": {"prefixes": ["RHEA:"], "function": None, "source": "statements"},
    "SEED_reaction": {"prefixes": ["seed.reaction:"], "function": None, "source": "statements"},
    "SEED_subsystem": {"prefixes": ["seed.subsystem:"], "function": None, "source": "statements"},
    "MetaCyc_pathway": {"prefixes": ["obo:metacyc.pathway_"], "function": None, "source": "statements"},
    "KEGG_pathway": {"prefixes": ["kegg.pathway:"], "function": None, "source": "statements"},
}

#: The ONLY synonym predicate used. Related / narrow / broad are ignored.
SYNONYM_PREDICATES = ("oio:hasExactSynonym",)
UNUSED_SYNONYM_PREDICATES = (
    "oio:hasRelatedSynonym",
    "oio:hasNarrowSynonym",
    "oio:hasBroadSynonym",
)

P_LABEL = "rdfs:label"
P_NAMESPACE = "oio:hasOBONamespace"
P_IAO_DEF = "IAO:0000115"
P_DCTERMS_DESC = "dcterms:description"
P_XREF = "oio:hasDbXref"
P_EQUIV = "owl:equivalentClass"
P_KO_EC = "kegg-ko:ec"

#: Predicates the store query asks for (hierarchy -- rdfs:subClassOf,
#: entailed_edge -- is deliberately NOT loaded).
STATEMENT_PREDICATES = (
    P_LABEL,
    *SYNONYM_PREDICATES,
    *UNUSED_SYNONYM_PREDICATES,  # fetched only so they can be counted as ignored
    P_NAMESPACE,
    P_IAO_DEF,
    P_DCTERMS_DESC,
    P_XREF,
    P_EQUIV,
)

TERM_FUNCTION_MAP_COLUMNS = (
    "term_curie",
    "relation",
    "standardized_text",
    "function_entity_hash",
    "status",
)

STATUS_LOADED = "loaded"
STATUS_QUARANTINED = "quarantined"
STATUS_NO_TEXT = "no_text"

#: ``<https://modelseed.org/template/roles/ftr00040>`` -> ``seed.role:ftr00040``.
_ROLE_URL_PREFIX = "<https://modelseed.org/template/roles/"

_EC_BRACKET = re.compile(r"\s*\[EC:([^\]]*)\]\s*$")
_SNAPSHOT_ID = re.compile(r"^[0-9]+$")
_VERSION = re.compile(r"^[A-Za-z0-9._+-]+$")

#: Raw xref prefix (lower-cased) -> the CURIE prefix of a loaded term.
_XREF_PREFIXES: dict[str, str] = {
    "go": "GO",
    "ec": "EC",
    "rhea": "RHEA",
    "pfam": "PFAM",
    "ipr": "IPR",
    "interpro": "IPR",
    "ko": "KO",
    "cog": "COG",
    "seed.role": "seed.role",
    "seed.reaction": "seed.reaction",
    "seed.subsystem": "seed.subsystem",
    "kegg.pathway": "kegg.pathway",
}


class PinnedFileError(ValueError):
    """A pinned input file did not hash to the expected sha256 (or is malformed)."""


class SnapshotResolutionError(RuntimeError):
    """The Iceberg snapshot id could not be resolved; pass ``snapshot_id``."""


# -- pinned files ------------------------------------------------------------


def read_pinned_text(path: str | Path, expected_sha256: str) -> str:
    """Read a pinned file, refusing on a sha256 mismatch. Never downloads."""
    data = Path(path).read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256.strip().lower():
        raise PinnedFileError(
            f"{path}: sha256 {actual} does not match the pinned {expected_sha256}; "
            "refusing to load. The loader never downloads or accepts an unpinned file."
        )
    return data.decode("utf-8")


@dataclass(frozen=True)
class KoEntry:
    ko_id: str          # 'K00001'
    raw_text: str       # 'ADH; alcohol dehydrogenase [EC:1.1.1.1]'
    name: str           # raw text with the EC bracket removed
    function_text: str  # 'alcohol dehydrogenase'
    ecs: tuple[str, ...]  # ('EC:1.1.1.1',)

    @property
    def curie(self) -> str:
        return f"KO:{self.ko_id}"


@dataclass(frozen=True)
class CogEntry:
    cog_id: str  # 'COG0593'
    name: str

    @property
    def curie(self) -> str:
        return f"COG:{self.cog_id}"


def ko_function_text(raw_text: str) -> tuple[str, str, tuple[str, ...]]:
    """Split a KO definition into ``(name, function_text, ecs)``.

    ``name`` is the text with a trailing `` [EC:...]`` bracket removed. The
    function text is the part after the FIRST ``'; '`` of that; with no
    ``'; '`` it is the whole name (gene symbols are not functions). ``ecs`` are
    the ``EC:`` CURIEs of the bracket, in order.
    """
    text = raw_text.strip()
    ecs: tuple[str, ...] = ()
    m = _EC_BRACKET.search(text)
    if m:
        ecs = tuple(f"EC:{tok}" for tok in m.group(1).split())
        text = text[: m.start()].rstrip()
    function = text.split("; ", 1)[1] if "; " in text else text
    return text, function.strip(), ecs


def parse_ko_lines(lines: Iterable[str]) -> list[KoEntry]:
    """Parse ``K00001<TAB>ADH; alcohol dehydrogenase [EC:1.1.1.1]`` lines.

    Raises:
        PinnedFileError: a line without a tab, a malformed id, an empty
        definition, or a duplicate id.
    """
    out: list[KoEntry] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, 1):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        ko_id, sep, raw = line.partition("\t")
        ko_id = ko_id.strip()
        if not sep or not re.fullmatch(r"K[0-9]{5}", ko_id) or not raw.strip():
            raise PinnedFileError(f"KO line {number}: expected 'Knnnnn<TAB>definition', got {line!r}")
        if ko_id in seen:
            raise PinnedFileError(f"KO line {number}: duplicate id {ko_id}")
        seen.add(ko_id)
        name, function, ecs = ko_function_text(raw)
        out.append(KoEntry(ko_id, raw.strip(), name, function, ecs))
    return out


def load_ko_file(path: str | Path, expected_sha256: str) -> list[KoEntry]:
    """Read the pinned KO list; refuses on a sha256 mismatch."""
    return parse_ko_lines(read_pinned_text(path, expected_sha256).splitlines())


def parse_cog_def_lines(lines: Iterable[str]) -> list[CogEntry]:
    """Parse ``cog-*.def.tab``: column 1 the COG id, column 3 the COG name."""
    out: list[CogEntry] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, 1):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 3 or not re.fullmatch(r"COG[0-9]{4}", cols[0]) or not cols[2].strip():
            raise PinnedFileError(f"COG line {number}: expected 'COGnnnn<TAB>category<TAB>name...', got {line!r}")
        if cols[0] in seen:
            raise PinnedFileError(f"COG line {number}: duplicate id {cols[0]}")
        seen.add(cols[0])
        out.append(CogEntry(cols[0], cols[2].strip()))
    return out


def load_cog_file(path: str | Path, expected_sha256: str) -> list[CogEntry]:
    """Read the pinned COG definitions; refuses on a sha256 mismatch."""
    return parse_cog_def_lines(read_pinned_text(path, expected_sha256).splitlines())


# -- statements --------------------------------------------------------------


def _is_english(language: Any) -> bool:
    return isinstance(language, str) and language.lower().split("-")[0] == "en"


def _is_untagged(language: Any) -> bool:
    return language is None or language == ""


def select_literal(literals: Iterable[tuple[str, Any]]) -> str | None:
    """Scalar literal selection: English-tagged first, then untagged,
    lexicographic tie-break. Other-language literals are never selected.

    ``literals`` are ``(value, language)`` pairs.
    """
    cleaned = [(v.strip(), lang) for v, lang in literals if isinstance(v, str) and v.strip()]
    for wanted in (_is_english, _is_untagged):
        pool = sorted(v for v, lang in cleaned if wanted(lang))
        if pool:
            return pool[0]
    return None


def _all_literals(literals: Iterable[tuple[str, Any]]) -> list[str]:
    """Every English-tagged or untagged literal, deduplicated, sorted (for
    multi-valued fields such as exact synonyms)."""
    return sorted(
        {
            v.strip()
            for v, lang in literals
            if isinstance(v, str) and v.strip() and (_is_english(lang) or _is_untagged(lang))
        }
    )


def _subject_curie(subject: str) -> str:
    if subject.startswith(_ROLE_URL_PREFIX) and subject.endswith(">"):
        return "seed.role:" + subject[len(_ROLE_URL_PREFIX) : -1]
    return subject


def map_xref(raw: str | None) -> str | None:
    """Map a raw ``oio:hasDbXref`` value / object to a loaded-CURIE spelling.

    ``'EC:1.1.1.1'`` -> ``'EC:1.1.1.1'``; ``'rhea:19363;'`` -> ``'RHEA:19363'``;
    ``'InterPro:IPR000001'`` -> ``'IPR:IPR000001'``; ``'GO:GO:0004022'`` ->
    ``'GO:0004022'``; the ModelSEED role URL form -> ``'seed.role:...'``.
    Returns None for a value outside the supported prefixes (``Reactome:``,
    ``kegg.reaction:``, blank nodes, ...).
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().rstrip(";").strip()
    if text.startswith(_ROLE_URL_PREFIX) and text.endswith(">"):
        text = "seed.role:" + text[len(_ROLE_URL_PREFIX) : -1]
    prefix, sep, body = text.partition(":")
    if not sep:
        return None
    canonical = _XREF_PREFIXES.get(prefix.strip().lower())
    if canonical is None:
        return None
    body = body.strip()
    if body.lower().startswith(canonical.lower() + ":"):
        body = body[len(canonical) + 1 :]
    if not body:
        return None
    return f"{canonical}:{body}"


def _raw_prefix(raw: str | None) -> str:
    if not isinstance(raw, str) or ":" not in raw:
        return ""
    return raw.strip().partition(":")[0]


# -- build -------------------------------------------------------------------


@dataclass
class OntologyWaveResult:
    """Everything the loader produced. Nothing here has been written anywhere."""

    snapshot_id: str
    ko_sha256: str | None
    cog_sha256: str | None
    #: ``<type>_entity`` / ``<type>_content`` rows, without observed_at / batch id.
    entity_rows: dict[str, list[dict[str, Any]]]
    content_rows: dict[str, list[dict[str, Any]]]
    methods: list[dict[str, Any]]
    #: validated link rows per joining table (``metadata`` canonical JSON text).
    links: dict[str, list[dict[str, Any]]]
    term_function_map: list[dict[str, str]]
    report: dict[str, Any] = field(default_factory=dict)

    def dry_run_summary(self) -> dict[str, Any]:
        """Counts per table and per method, plus the run report. No row bodies."""
        per_method: dict[str, dict[str, int]] = {}
        for table, rows in self.links.items():
            for row in rows:
                per_method.setdefault(row["method_name"], {}).setdefault(table, 0)
                per_method[row["method_name"]][table] += 1
        return {
            "snapshot_id": self.snapshot_id,
            "ko_sha256": self.ko_sha256,
            "cog_sha256": self.cog_sha256,
            "tables": {
                **{f"ontology_term_{k}": len(self.entity_rows["ontology_term"]) for k in ("entity", "content")},
                **{f"function_{k}": len(self.entity_rows["function"]) for k in ("entity", "content")},
                "method": len(self.methods),
                **{table: len(rows) for table, rows in self.links.items()},
            },
            "methods": {m["method_name"]: per_method.get(m["method_name"], {}) for m in self.methods},
            "term_function_map_rows": len(self.term_function_map),
            "report": self.report,
        }


def _method_row(name: str, algorithm: str, version: str, params: Mapping[str, Any], source: str, description: str) -> dict[str, Any]:
    return {
        "method_name": name,
        "algorithm": algorithm,
        "algorithm_version": version,
        "parameters_json": canonical_parameter_set(dict(params)),
        "parameter_set_hash": parameter_set_hash(dict(params)),
        "source_dataset": source,
        "description": description,
    }


def _ontologies_parameter() -> dict[str, Any]:
    return {name.lower(): list(spec["prefixes"]) for name, spec in sorted(ONTOLOGIES.items())}


def _std_prefix_table() -> list[tuple[str, str]]:
    """``(standardized prefix, ontology name)``, longest prefix first."""
    pairs = [
        (standardize("ontology_term", p), name)
        for name, spec in ONTOLOGIES.items()
        for p in spec["prefixes"]
        if not p.startswith("<")
    ]
    return sorted(pairs, key=lambda t: -len(t[0]))


def _ontology_of(std_curie: str, table: Sequence[tuple[str, str]]) -> str | None:
    for prefix, name in table:
        if std_curie.startswith(prefix):
            return name
    return None


def build_ontology_wave(
    statements: Iterable[Mapping[str, Any]],
    *,
    snapshot_id: str,
    ko_entries: Sequence[KoEntry] | None = None,
    ko_sha256: str | None = None,
    ko_fetch_date: str = KEGG_KO_FETCH_DATE,
    cog_entries: Sequence[CogEntry] | None = None,
    cog_sha256: str | None = None,
    cog_release: str = COG_RELEASE,
) -> OntologyWaveResult:
    """Turn store statements (read at ``snapshot_id``) plus parsed KO / COG
    entries into rows. Pure: no I/O, no writes.

    ``statements`` are mappings with ``subject, predicate, object, value,
    datatype, language``; rows for other subjects/predicates are ignored.
    """
    snapshot_id = str(snapshot_id)
    if not _SNAPSHOT_ID.match(snapshot_id):
        raise ValueError(f"snapshot_id {snapshot_id!r} must be an explicit Iceberg snapshot id (digits)")
    if ko_entries is not None and not (ko_sha256 and _VERSION.match(ko_sha256)):
        raise ValueError("ko_entries need the ko_sha256 they were read under (source_dataset)")
    if cog_entries is not None and not (cog_sha256 and _VERSION.match(cog_sha256)):
        raise ValueError("cog_entries need the cog_sha256 they were read under (source_dataset)")
    if not _VERSION.match(ko_fetch_date) or not _VERSION.match(cog_release):
        raise ValueError("ko_fetch_date / cog_release must be version-like tokens")

    prefix_table = _std_prefix_table()

    # -- group statements by (standardized) subject, dropping exact duplicates
    seen_stmt: set[tuple[Any, ...]] = set()
    by_subject: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    ignored_synonyms: dict[str, int] = defaultdict(int)
    for st in statements:
        key = tuple(st.get(c) for c in ("subject", "predicate", "object", "value", "datatype", "language"))
        if key in seen_stmt:
            continue
        seen_stmt.add(key)
        subject = st.get("subject")
        if not isinstance(subject, str):
            continue
        std = standardize("ontology_term", _subject_curie(subject))
        ont = _ontology_of(std, prefix_table)
        if ont is None or ONTOLOGIES[ont]["source"] != "statements":
            continue
        if st.get("predicate") in UNUSED_SYNONYM_PREDICATES:
            ignored_synonyms[st["predicate"]] += 1
            continue
        by_subject[std].append(st)

    # -- the term inventory: terms (with an rdfs:label) per ontology
    terms: dict[str, dict[str, Any]] = {}  # std curie -> info
    unlabelled = 0
    for std, sts in by_subject.items():
        ont = _ontology_of(std, prefix_table)
        lits = defaultdict(list)
        for st in sts:
            if st.get("value") is not None:
                lits[st.get("predicate")].append((st["value"], st.get("language")))
        label = select_literal(lits[P_LABEL])
        if label is None:
            unlabelled += 1
            continue
        definition = select_literal(lits[P_IAO_DEF])
        description_pred = P_IAO_DEF
        if definition is None:
            definition = select_literal(lits[P_DCTERMS_DESC])
            description_pred = P_DCTERMS_DESC
        terms[std] = {
            "ontology": ont,
            "label": label,
            "namespace": select_literal(lits[P_NAMESPACE]) or ont,
            "definition": definition,
            "definition_predicate": description_pred if definition else None,
            "exact_synonyms": _all_literals(lits[SYNONYM_PREDICATES[0]]),
            "statements": sts,
        }

    ko_terms: dict[str, KoEntry] = {}
    for entry in ko_entries or ():
        std = standardize("ontology_term", entry.curie)
        terms[std] = {
            "ontology": "KO",
            "label": entry.name,
            "namespace": "KO",
            "definition": entry.raw_text,
            "statements": [],
        }
        ko_terms[std] = entry
    cog_terms: dict[str, CogEntry] = {}
    for entry in cog_entries or ():
        std = standardize("ontology_term", entry.curie)
        terms[std] = {
            "ontology": "COG",
            "label": entry.name,
            "namespace": "COG",
            "definition": None,
            "statements": [],
        }
        cog_terms[std] = entry

    # -- method names
    m_def_store = f"ontology-definition@{snapshot_id}/kbase-ontology-source"
    m_syn_store = f"ontology-synonym@{snapshot_id}/kbase-ontology-source"
    m_xref_store = f"ontology-xref@{snapshot_id}/kbase-ontology-source"
    m_def_ko = f"ontology-definition@{ko_fetch_date}/kegg-ko"
    m_ko_ec = f"kegg-ko-ec@{ko_fetch_date}/kegg-ko"
    m_def_cog = f"ontology-definition@{cog_release}/ncbi-cog"

    # -- entities and function links
    function_text: dict[str, str] = {}     # function hash -> display text
    term_function_links: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
    # per term: (relation, field, method, function hash)
    map_rows: list[dict[str, str]] = []
    quarantined = 0
    seen_quarantine: set[tuple[str, str]] = set()

    def attach(term: str, relation: str, fld: str, method: str, text: str) -> str | None:
        """Register a text for a term; return its function hash or None if stop-listed."""
        nonlocal quarantined
        std_text = standardize("function", text)
        if not std_text:
            return None
        if schema.is_stoplisted_function(text):
            if (term, std_text) not in seen_quarantine:
                seen_quarantine.add((term, std_text))
                quarantined += 1
            return None
        fh = entity_hash("function", text)
        prior = function_text.get(fh)
        if prior is None or text < prior:
            function_text[fh] = text
        term_function_links[term].append((relation, fld, method, fh))
        return fh

    for std in sorted(terms):
        info = terms[std]
        ont = info["ontology"]
        mode = ONTOLOGIES[ont]["function"]
        chosen: tuple[str, str] | None = None  # (relation, text) for the map
        if mode == "label_synonym":
            chosen = ("label", info["label"])
            attach(std, "label", P_LABEL, m_def_store, info["label"])
            for syn in info["exact_synonyms"]:
                attach(std, "exact_synonym", SYNONYM_PREDICATES[0], m_syn_store, syn)
        elif mode == "description":
            if info["definition"]:
                chosen = ("definition", info["definition"])
                attach(std, "definition", info["definition_predicate"], m_def_store, info["definition"])
        elif mode == "ko":
            entry = ko_terms[std]
            chosen = ("definition", entry.function_text)
            attach(std, "definition", "kegg:definition", m_def_ko, entry.function_text)
        elif mode == "cog":
            entry = cog_terms[std]
            chosen = ("definition", entry.name)
            attach(std, "definition", "ncbi-cog:name", m_def_cog, entry.name)
        if mode is None:
            continue
        if chosen is None:
            map_rows.append(dict(zip(TERM_FUNCTION_MAP_COLUMNS, (std, "", "", "", STATUS_NO_TEXT))))
            continue
        relation, text = chosen
        std_text = standardize("function", text)
        if schema.is_stoplisted_function(text) or not std_text:
            map_rows.append(dict(zip(TERM_FUNCTION_MAP_COLUMNS, (std, relation, std_text, "", STATUS_QUARANTINED))))
        else:
            map_rows.append(
                dict(zip(TERM_FUNCTION_MAP_COLUMNS, (std, relation, std_text, entity_hash("function", text), STATUS_LOADED)))
            )

    # -- link rows
    thash = {std: entity_hash("ontology_term", std) for std in terms}
    fot_rows: list[dict[str, Any]] = []
    for term in sorted(term_function_links):
        for relation, fld, method, fh in term_function_links[term]:
            fot_rows.append(
                {
                    "subject_hash": fh,
                    "object_hash": thash[term],
                    "relation": relation,
                    "method_name": method,
                    "link_key": "",
                    "metadata": cl.canonical_metadata({"basis": "definition", "field": fld}),
                }
            )

    # alias cliques: every pair of distinct function texts attached to one term
    alias_rows: list[dict[str, Any]] = []
    largest = (0, "")
    for term in sorted(term_function_links):
        hashes = sorted({fh for _r, _f, _m, fh in term_function_links[term]})
        if len(hashes) > largest[0]:
            largest = (len(hashes), term)
        for i, a in enumerate(hashes):
            for b in hashes[i + 1 :]:
                alias_rows.append(
                    {
                        "subject_hash": a,
                        "object_hash": b,
                        "relation": "alias",
                        "method_name": m_syn_store,
                        "link_key": term,
                        "metadata": cl.canonical_metadata({"via_term": term}),
                    }
                )

    # term -> term links
    ott_rows: list[dict[str, Any]] = []
    unresolved: dict[tuple[str, str], int] = defaultdict(int)
    skipped_self = 0

    def xref_row(asserting: str, predicate: str, raw: str, asserted: str, relation: str, method: str) -> None:
        nonlocal skipped_self
        a_hash, b_hash = thash[asserting], thash[asserted]
        if a_hash == b_hash:
            skipped_self += 1
            return
        subject, obj = a_hash, b_hash
        if relation in schema.SYMMETRIC_RELATIONS and subject > obj:
            subject, obj = obj, subject
        ott_rows.append(
            {
                "subject_hash": subject,
                "object_hash": obj,
                "relation": relation,
                "method_name": method,
                "link_key": cl.canonical_json_sha256([asserting, predicate, raw]),
                "metadata": cl.canonical_metadata(
                    {"source_predicate": predicate, "asserting_curie": asserting, "asserted_curie": asserted}
                ),
            }
        )

    for std in sorted(terms):
        for st in terms[std]["statements"]:
            predicate = st.get("predicate")
            if predicate not in (P_XREF, P_EQUIV):
                continue
            raw = st.get("value") if st.get("value") is not None else st.get("object")
            if not isinstance(raw, str):
                continue
            asserted = map_xref(raw)
            if asserted is not None:
                asserted = standardize("ontology_term", asserted)
            if asserted is None or asserted not in terms:
                unresolved[(predicate, _raw_prefix(raw))] += 1
                continue
            if predicate == P_XREF:
                xref_row(std, predicate, raw, asserted, "xref", m_xref_store)
            else:
                xref_row(std, predicate, raw, asserted, "exact_match", m_xref_store)
    for std, entry in sorted(ko_terms.items()):
        for ec in entry.ecs:
            asserted = standardize("ontology_term", ec)
            if asserted in terms:
                xref_row(std, P_KO_EC, ec, asserted, "xref", m_ko_ec)
            else:
                unresolved[(P_KO_EC, "EC")] += 1

    # -- validate every table with lw-1's rules (collapses identical rows)
    links: dict[str, list[dict[str, Any]]] = {}
    collapsed: dict[str, int] = {}
    for table, rows in (
        (schema.FUNCTION_X_ONTOLOGY_TERM, fot_rows),
        (schema.FUNCTION_X_FUNCTION, alias_rows),
        (schema.ONTOLOGY_TERM_X_ONTOLOGY_TERM, ott_rows),
    ):
        checked = cl.validate_link_rows(table, rows)
        links[table] = checked["rows"]
        collapsed[table] = checked["collapsed_identical"]
        skipped_self += checked["skipped_self_pairs"]

    # -- methods (only for sources that were supplied)
    store_params = {
        "ontologies": _ontologies_parameter(),
        "synonym_predicates": list(SYNONYM_PREDICATES),
        "stoplist_version": schema.STOPLIST_VERSION,
    }
    store_src = snapshot_id
    methods = [
        _method_row(m_def_store, "ontology-definition", snapshot_id, store_params, store_src,
                    "Function text and term definitions read from kbase.ontology_source.statements at one pinned Iceberg snapshot."),
        _method_row(m_syn_store, "ontology-synonym", snapshot_id, store_params, store_src,
                    "oio:hasExactSynonym texts of kbase.ontology_source.statements terms, and the alias links between texts of one term."),
        _method_row(m_xref_store, "ontology-xref", snapshot_id, store_params, store_src,
                    "oio:hasDbXref (directed) and owl:equivalentClass (exact_match) assertions between loaded terms of kbase.ontology_source.statements."),
    ]
    if ko_entries is not None:
        ko_params = {"stoplist_version": schema.STOPLIST_VERSION}
        methods.append(_method_row(m_def_ko, "ontology-definition", ko_fetch_date, ko_params, ko_sha256,
                                   "KEGG Orthology definitions from the pinned rest.kegg.jp/list/ko snapshot; function text is the part after the first '; '."))
        methods.append(_method_row(m_ko_ec, "kegg-ko-ec", ko_fetch_date, {}, ko_sha256,
                                   "EC numbers in the bracket of a KEGG KO definition, as directed xref links to loaded EC terms."))
    if cog_entries is not None:
        methods.append(_method_row(m_def_cog, "ontology-definition", cog_release,
                                   {"stoplist_version": schema.STOPLIST_VERSION}, cog_sha256,
                                   "NCBI COG names from the pinned cog-*.def.tab release, loaded as by-definition function text."))
    methods, _registry = cl.validate_method_rows(methods)

    # -- entity / content rows
    term_entity = []
    term_content = []
    for std in sorted(terms):
        info = terms[std]
        h = thash[std]
        term_entity.append({"entity_hash": h, "entity_type": "ontology_term", "standardizer_version": STANDARDIZER_VERSION})
        term_content.append(
            {
                "entity_hash": h,
                "term_id": std,
                "name": info["label"],
                "namespace": info["namespace"],
                "definition": info["definition"],
                "standardizer_version": STANDARDIZER_VERSION,
            }
        )
    fn_entity = []
    fn_content = []
    for fh in sorted(function_text):
        fn_entity.append({"entity_hash": fh, "entity_type": "function", "standardizer_version": STANDARDIZER_VERSION})
        fn_content.append({"entity_hash": fh, "definition": function_text[fh], "standardizer_version": STANDARDIZER_VERSION})

    terms_per_ontology: dict[str, int] = defaultdict(int)
    for info in terms.values():
        terms_per_ontology[info["ontology"]] += 1
    report = {
        "terms_per_ontology": dict(sorted(terms_per_ontology.items())),
        "unlabelled_subjects_skipped": unlabelled,
        "function_texts_quarantined": quarantined,
        "stoplist_version": schema.STOPLIST_VERSION,
        "largest_alias_clique": {"term": largest[1], "function_texts": largest[0]},
        "unresolved_xrefs": [
            {"predicate": p, "prefix": pre, "count": n} for (p, pre), n in sorted(unresolved.items())
        ],
        "ignored_synonym_statements": dict(sorted(ignored_synonyms.items())),
        "skipped_self_pairs": skipped_self,
        "collapsed_identical": collapsed,
    }
    return OntologyWaveResult(
        snapshot_id=snapshot_id,
        ko_sha256=ko_sha256,
        cog_sha256=cog_sha256,
        entity_rows={"ontology_term": term_entity, "function": fn_entity},
        content_rows={"ontology_term": term_content, "function": fn_content},
        methods=methods,
        links=links,
        term_function_map=map_rows,
        report=report,
    )


def write_term_function_map(rows: Sequence[Mapping[str, str]], path: str | Path) -> Path:
    """Write the persisted TERM -> function-text map as TSV (with a header).

    W3 and the seed loader read this file to link term-assigning tool output;
    they never re-parse the ontology sources.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(TERM_FUNCTION_MAP_COLUMNS)
        for row in rows:
            writer.writerow([row[c] for c in TERM_FUNCTION_MAP_COLUMNS])
    return path


# -- reading the store -------------------------------------------------------

QueryFn = Callable[[str], list[dict[str, Any]]]


def berdl_query_fn(utils: Any, *, page_size: int = 1000) -> QueryFn:
    """Adapt ``KBBERDLUtils`` to the ``sql -> rows`` callable used here."""

    def run(sql: str) -> list[dict[str, Any]]:
        result = utils.paginate_query(sql, page_size=page_size)
        if not result.get("success"):
            raise RuntimeError(f"BERDL query failed: {str(result.get('error'))[:300]}")
        return list(result.get("data") or [])

    return run


def resolve_snapshot_id(query_fn: QueryFn) -> str:
    """Resolve the CURRENT Iceberg snapshot id of the statements table, once.

    Reads the table's ``.snapshots`` metadata table. The BERDL MCP SQL
    endpoint refuses metadata-table queries (HTTP 400, observed 2026-11-27), so
    from there this raises :class:`SnapshotResolutionError`; resolve the id
    in-pod (Spark) and pass ``snapshot_id`` explicitly instead.
    """
    try:
        rows = query_fn(f"SELECT snapshot_id, committed_at FROM {STATEMENTS_TABLE}.snapshots")
    except Exception as exc:  # transport detail is not the point
        raise SnapshotResolutionError(
            f"could not read {STATEMENTS_TABLE}.snapshots ({exc}); resolve the snapshot id "
            "where metadata tables are readable and pass snapshot_id explicitly"
        ) from exc
    if not rows:
        raise SnapshotResolutionError(f"{STATEMENTS_TABLE}.snapshots returned no rows")
    newest = max(rows, key=lambda r: str(r.get("committed_at")))
    return str(newest["snapshot_id"])


def _like_pattern(prefix: str) -> str:
    return prefix.replace("'", "''").replace("_", "\\_") + "%"


def fetch_statements(
    query_fn: QueryFn,
    snapshot_id: str,
    *,
    as_of: bool = True,
    only: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Read the statements the loader uses, ALL AS OF ``snapshot_id``.

    One query per statements-sourced ontology, restricted to the predicates in
    :data:`STATEMENT_PREDICATES`. ``as_of=False`` drops the snapshot clause
    (an unpinned read, for the real-data check script only). ``only`` limits
    the read to the named :data:`ONTOLOGIES` (also for that script).
    """
    if not _SNAPSHOT_ID.match(str(snapshot_id)):
        raise ValueError(f"snapshot_id {snapshot_id!r} must be an explicit Iceberg snapshot id (digits)")
    source = f"{STATEMENTS_TABLE} VERSION AS OF {int(snapshot_id)}" if as_of else STATEMENTS_TABLE
    predicates = ", ".join("'" + p + "'" for p in STATEMENT_PREDICATES)
    rows: list[dict[str, Any]] = []
    for name, spec in ONTOLOGIES.items():
        if spec["source"] != "statements" or (only is not None and name not in only):
            continue
        likes = " OR ".join(f"subject LIKE '{_like_pattern(p)}'" for p in spec["prefixes"])
        sql = (
            f"SELECT subject, predicate, object, value, datatype, language FROM {source} "
            f"WHERE predicate IN ({predicates}) AND ({likes}) "
            "ORDER BY subject, predicate, object, value, language"
        )
        rows.extend(query_fn(sql))
    return rows


def run_ontology_wave(
    query_fn: QueryFn,
    *,
    ko_path: str | Path | None,
    ko_sha256: str | None,
    cog_path: str | Path | None = None,
    cog_sha256: str | None = None,
    snapshot_id: str | None = None,
    output_dir: str | Path | None = None,
) -> OntologyWaveResult:
    """Resolve ONE snapshot, read the pinned files and the store, build rows.

    The KO / COG files are refused on a sha256 mismatch BEFORE the store is
    touched. When ``output_dir`` is given the term -> function map is written
    there as ``term_function_map.tsv``. Nothing is written to the lake.
    """
    ko_entries = load_ko_file(ko_path, ko_sha256) if ko_path is not None else None
    cog_entries = load_cog_file(cog_path, cog_sha256) if cog_path is not None else None
    snapshot = str(snapshot_id) if snapshot_id is not None else resolve_snapshot_id(query_fn)
    statements = fetch_statements(query_fn, snapshot)
    result = build_ontology_wave(
        statements,
        snapshot_id=snapshot,
        ko_entries=ko_entries,
        ko_sha256=ko_sha256 if ko_entries is not None else None,
        cog_entries=cog_entries,
        cog_sha256=cog_sha256 if cog_entries is not None else None,
    )
    if output_dir is not None:
        write_term_function_map(result.term_function_map, Path(output_dir) / "term_function_map.tsv")
    return result
