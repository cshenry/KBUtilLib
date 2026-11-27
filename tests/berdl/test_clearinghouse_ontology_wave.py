"""lw-3: the W1 ontology loader.

The fixture under ``fixtures/ontology_wave/`` is REAL data: ``statements.json``
is rows of ``kbase.ontology_source.statements`` (read 2026-11-27) for GO:0004022
(with exact, related, narrow and broad synonyms and xrefs), EC:1.1.1.1, a SEED
role, a SEED reaction, two Rhea terms, a Pfam term with a description and an
InterPro term; ``ko_list_slice.tsv`` is three lines of the pinned KEGG KO list
and ``cog_def_slice.tab`` three lines of NCBI's cog-24.def.tab. The expected
rows below are written out BY HAND (hashes via hashlib, not via the loader).
Statements marked SYNTHETIC in a test are built there, because the store has
no such case among the fixture subjects.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from kbutillib.domains.kbase.berdl import clearinghouse_links as cl
from kbutillib.domains.kbase.berdl import clearinghouse_ontology_wave as ow
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema

from .test_clearinghouse_links import WRITE_NS, _cap, _isolated, _MiniLake  # noqa: F401

FIX = Path(__file__).parent / "fixtures" / "ontology_wave"
SNAP = "4242424242"
KO_SHA = hashlib.sha256((FIX / "ko_list_slice.tsv").read_bytes()).hexdigest()
COG_SHA = hashlib.sha256((FIX / "cog_def_slice.tab").read_bytes()).hexdigest()

M_DEF = f"ontology-definition@{SNAP}/kbase-ontology-source"
M_SYN = f"ontology-synonym@{SNAP}/kbase-ontology-source"
M_XREF = f"ontology-xref@{SNAP}/kbase-ontology-source"
M_KO = "ontology-definition@2026-11-24/kegg-ko"
M_KO_EC = "kegg-ko-ec@2026-11-24/kegg-ko"
M_COG = "ontology-definition@cog-24/ncbi-cog"


def H(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def T(curie: str) -> str:
    """ontology_term entity hash of an already-standardized CURIE."""
    return H(curie)


def F(std_text: str) -> str:
    """function entity hash of an already-standardized text."""
    return H(std_text)


def statements():
    return json.loads((FIX / "statements.json").read_text(encoding="utf-8"))


def ko_entries():
    return ow.load_ko_file(FIX / "ko_list_slice.tsv", KO_SHA)


def cog_entries():
    return ow.load_cog_file(FIX / "cog_def_slice.tab", COG_SHA)


def build(stmts=None, **kw):
    return ow.build_ontology_wave(
        statements() if stmts is None else stmts,
        snapshot_id=SNAP,
        ko_entries=kw.pop("ko_entries", ko_entries()),
        ko_sha256=KO_SHA,
        cog_entries=kw.pop("cog_entries", cog_entries()),
        cog_sha256=COG_SHA,
        **kw,
    )


def meta(**kw):
    return json.dumps(kw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def link(subject, obj, relation, method, link_key, **metadata):
    return {
        "subject_hash": subject,
        "object_hash": obj,
        "relation": relation,
        "method_name": method,
        "link_key": link_key,
        "metadata": meta(**metadata),
    }


def defn(field):
    return {"basis": "definition", "field": field}


def xkey(asserting, predicate, raw):
    return hashlib.sha256(json.dumps([asserting, predicate, raw], separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def keyed(rows):
    return sorted(rows, key=lambda r: (r["subject_hash"], r["object_hash"], r["relation"], r["method_name"], r["link_key"]))


# ---------------------------------------------------------------------------
# the hand-written expectation
# ---------------------------------------------------------------------------

GO_LABEL = "alcohol dehydrogenase (nad+) activity"
GO_EXACT = "aldo-keto reductase (nad) activity"
EC_LABEL = "alcohol dehydrogenase"
EC_EXACT = "aldehyde reductase"
ROLE = "(+)-caryolan-1-ol synthase (ec 4.2.1.138)"
IPR = "kringle"
PFAM = "7 transmembrane receptor (rhodopsin family)"
KO4 = "(r,r)-butanediol dehydrogenase / meso-butanediol dehydrogenase / diacetyl reductase"
COG1 = "glutamate-1-semialdehyde aminotransferase"
COG3 = "anion-transporting atpase, arsa/get3 family"
COG593 = "chromosomal replication initiation atpase dnaa"

EXPECTED_TERMS = {
    # curie: (name, namespace, definition)
    "EC:1.1.1.1": ("alcohol dehydrogenase", "EC", None),
    "GO:0004022": (
        "alcohol dehydrogenase (NAD+) activity",
        "molecular_function",
        "Catalysis of the reaction: an alcohol + NAD+ = an aldehyde or ketone + NADH + H+.",
    ),
    "IPR:IPR000001": ("Kringle", "InterPro", None),
    "PFAM:PF00001": ("7tm_1", "Pfam", "7 transmembrane receptor (rhodopsin family)"),
    "RHEA:19363": ("(2E,6E)-farnesyl diphosphate + diphosphate => isopentenyl diphosphate + (2E)-geranyl diphosphate", "Rhea", None),
    "RHEA:19364": ("isopentenyl diphosphate + (2E)-geranyl diphosphate <=> (2E,6E)-farnesyl diphosphate + diphosphate", "Rhea", None),
    "SEED.REACTION:rxn01466": ("Geranyl-diphosphate:isopentenyl-diphosphate geranyltrans-transferase", "SEED_reaction", None),
    "SEED.ROLE:0000000000001": ("(+)-caryolan-1-ol synthase (EC 4.2.1.138)", "SEED_ROLE", None),
    "KO:K00001": ("ADH; alcohol dehydrogenase", "KO", "ADH; alcohol dehydrogenase [EC:1.1.1.1]"),
    "KO:K00004": (
        "BDH, butB; (R,R)-butanediol dehydrogenase / meso-butanediol dehydrogenase / diacetyl reductase",
        "KO",
        "BDH, butB; (R,R)-butanediol dehydrogenase / meso-butanediol dehydrogenase / diacetyl reductase [EC:1.1.1.4 1.1.1.- 1.1.1.303]",
    ),
    "KO:K00243": ("K00243; uncharacterized protein", "KO", "K00243; uncharacterized protein"),
    "COG:COG0001": ("Glutamate-1-semialdehyde aminotransferase", "COG", None),
    "COG:COG0003": ("Anion-transporting ATPase, ArsA/GET3 family", "COG", None),
    "COG:COG0593": ("Chromosomal replication initiation ATPase DnaA", "COG", None),
}

EXPECTED_FUNCTIONS = {
    GO_LABEL: "alcohol dehydrogenase (NAD+) activity",
    GO_EXACT: "aldo-keto reductase (NAD) activity",
    EC_LABEL: "alcohol dehydrogenase",
    EC_EXACT: "aldehyde reductase",
    ROLE: "(+)-caryolan-1-ol synthase (EC 4.2.1.138)",
    IPR: "Kringle",
    PFAM: "7 transmembrane receptor (rhodopsin family)",
    KO4: "(R,R)-butanediol dehydrogenase / meso-butanediol dehydrogenase / diacetyl reductase",
    COG1: "Glutamate-1-semialdehyde aminotransferase",
    COG3: "Anion-transporting ATPase, ArsA/GET3 family",
    COG593: "Chromosomal replication initiation ATPase DnaA",
}

EXPECTED_FOT = [
    link(F(GO_LABEL), T("GO:0004022"), "label", M_DEF, "", **defn("rdfs:label")),
    link(F(GO_EXACT), T("GO:0004022"), "exact_synonym", M_SYN, "", **defn("oio:hasExactSynonym")),
    link(F(EC_LABEL), T("EC:1.1.1.1"), "label", M_DEF, "", **defn("rdfs:label")),
    link(F(EC_EXACT), T("EC:1.1.1.1"), "exact_synonym", M_SYN, "", **defn("oio:hasExactSynonym")),
    link(F(ROLE), T("SEED.ROLE:0000000000001"), "label", M_DEF, "", **defn("rdfs:label")),
    link(F(IPR), T("IPR:IPR000001"), "label", M_DEF, "", **defn("rdfs:label")),
    link(F(PFAM), T("PFAM:PF00001"), "definition", M_DEF, "", **defn("dcterms:description")),
    link(F(EC_LABEL), T("KO:K00001"), "definition", M_KO, "", **defn("kegg:definition")),
    link(F(KO4), T("KO:K00004"), "definition", M_KO, "", **defn("kegg:definition")),
    link(F(COG1), T("COG:COG0001"), "definition", M_COG, "", **defn("ncbi-cog:name")),
    link(F(COG3), T("COG:COG0003"), "definition", M_COG, "", **defn("ncbi-cog:name")),
    link(F(COG593), T("COG:COG0593"), "definition", M_COG, "", **defn("ncbi-cog:name")),
]


def _alias(a_text, b_text, via, method=M_SYN):
    a, b = sorted((F(a_text), F(b_text)))
    return link(a, b, "alias", method, via, via_term=via)


EXPECTED_ALIAS = [
    _alias(GO_LABEL, GO_EXACT, "GO:0004022"),
    _alias(EC_LABEL, EC_EXACT, "EC:1.1.1.1"),
]


def _xref(asserting, predicate, raw, asserted, method=M_XREF):
    return link(
        T(asserting), T(asserted), "xref", method, xkey(asserting, predicate, raw),
        asserted_curie=asserted, asserting_curie=asserting, source_predicate=predicate,
    )


EXPECTED_OTT = [
    _xref("GO:0004022", "oio:hasDbXref", "EC:1.1.1.1", "EC:1.1.1.1"),
    _xref("SEED.REACTION:rxn01466", "oio:hasDbXref", "rhea:19364", "RHEA:19364"),
    _xref("SEED.REACTION:rxn01466", "oio:hasDbXref", "rhea:19363;", "RHEA:19363"),
    _xref("KO:K00001", "kegg-ko:ec", "EC:1.1.1.1", "EC:1.1.1.1", M_KO_EC),
]


def test_fixture_yields_exactly_the_hand_written_rows():
    r = build()
    # ontology_term entity + content
    assert sorted(x["entity_hash"] for x in r.entity_rows["ontology_term"]) == sorted(T(c) for c in EXPECTED_TERMS)
    assert all(x["entity_type"] == "ontology_term" and x["standardizer_version"] == "2.0" for x in r.entity_rows["ontology_term"])
    got = {c["term_id"]: (c["name"], c["namespace"], c["definition"]) for c in r.content_rows["ontology_term"]}
    assert got == EXPECTED_TERMS
    assert {c["term_id"]: c["entity_hash"] for c in r.content_rows["ontology_term"]} == {c: T(c) for c in EXPECTED_TERMS}
    # function entity + content
    assert sorted(x["entity_hash"] for x in r.entity_rows["function"]) == sorted(F(t) for t in EXPECTED_FUNCTIONS)
    assert {c["entity_hash"]: c["definition"] for c in r.content_rows["function"]} == {F(t): d for t, d in EXPECTED_FUNCTIONS.items()}
    # links
    assert keyed(r.links["function_x_ontology_term"]) == keyed(EXPECTED_FOT)
    assert keyed(r.links["function_x_function"]) == keyed(EXPECTED_ALIAS)
    assert keyed(r.links["ontology_term_x_ontology_term"]) == keyed(EXPECTED_OTT)
    # the four joining tables the loader does not use stay absent
    assert set(r.links) == {"function_x_ontology_term", "function_x_function", "ontology_term_x_ontology_term"}


def test_entity_and_content_rows_carry_no_server_columns():
    r = build()
    for rows in (*r.entity_rows.values(), *r.content_rows.values()):
        for row in rows:
            assert "observed_at" not in row and "ingest_batch_id" not in row
    for rows in r.links.values():
        for row in rows:
            assert "observed_at" not in row and "ingest_batch_id" not in row
    for m in r.methods:
        assert "observed_at" not in m and "ingest_batch_id" not in m


def test_methods_names_versions_sources_and_parameters():
    r = build()
    by = {m["method_name"]: m for m in r.methods}
    assert set(by) == {M_DEF, M_SYN, M_XREF, M_KO, M_KO_EC, M_COG}
    for name in (M_DEF, M_SYN, M_XREF):
        assert by[name]["algorithm_version"] == SNAP and by[name]["source_dataset"] == SNAP
        params = json.loads(by[name]["parameters_json"])
        assert params["synonym_predicates"] == ["oio:hasExactSynonym"]
        assert params["ontologies"]["go"] == ["GO:"] and "ko" in params["ontologies"]
    assert by[M_KO]["algorithm_version"] == "2026-11-24" and by[M_KO]["source_dataset"] == KO_SHA
    assert by[M_KO_EC]["parameters_json"] == "{}" and by[M_KO_EC]["source_dataset"] == KO_SHA
    assert by[M_COG]["algorithm_version"] == "cog-24" and by[M_COG]["source_dataset"] == COG_SHA
    for m in r.methods:  # static description: no run counts
        assert not any(ch.isdigit() for ch in m["description"])


def test_every_emitted_link_passes_register_links_validation():
    r = build()
    entity_rows = {}
    for etype in ("function", "ontology_term"):
        entity_rows[f"{etype}_entity"] = [
            dict(e, observed_at="2026-11-27T00:00:00Z", ingest_batch_id="PRE") for e in r.entity_rows[etype]
        ]
    lake = _MiniLake(rows=entity_rows)
    cap = _cap(lake)
    written = {}
    for table, rows in r.links.items():
        out = cap.register_links(table, copy.deepcopy(rows), methods=r.methods)
        assert out["written"] == len(rows) and out["already_present"] == 0
        written[table] = out
    # a second, identical pass is fill-only: nothing new
    for table, rows in r.links.items():
        again = cap.register_links(table, copy.deepcopy(rows), methods=r.methods)
        assert again["written"] == 0 and again["already_present"] == len(rows)
    assert cap.register_methods  # the same methods registered above


def test_validators_accept_the_methods_and_links_directly():
    r = build()
    cl.validate_method_rows(r.methods)
    for table, rows in r.links.items():
        out = cl.validate_link_rows(table, rows)
        assert out["rows"] == rows and out["collapsed_identical"] == 0


# ---------------------------------------------------------------------------
# pinned files
# ---------------------------------------------------------------------------


def test_wrong_ko_sha256_is_refused():
    with pytest.raises(ow.PinnedFileError, match="does not match the pinned"):
        ow.load_ko_file(FIX / "ko_list_slice.tsv", "0" * 64)


def test_wrong_cog_sha256_is_refused():
    with pytest.raises(ow.PinnedFileError, match="does not match the pinned"):
        ow.load_cog_file(FIX / "cog_def_slice.tab", "0" * 64)


def test_run_refuses_a_bad_ko_hash_before_touching_the_store():
    def boom(sql):  # pragma: no cover - must not be reached
        raise AssertionError("the store was queried before the pinned file was verified")

    with pytest.raises(ow.PinnedFileError):
        ow.run_ontology_wave(boom, ko_path=FIX / "ko_list_slice.tsv", ko_sha256="f" * 64, snapshot_id=SNAP)


def test_pinned_constants_name_the_staged_files():
    assert ow.KO_FILE_SHA256 == "e8389cac2e990b0f45452cc01a05ab2ccaad98a1bb4106f9c4f1870d32d2ca70"
    assert len(ow.COG_FILE_SHA256) == 64


# ---------------------------------------------------------------------------
# one pinned snapshot
# ---------------------------------------------------------------------------


def test_every_store_query_reads_the_one_pinned_snapshot():
    seen = []

    def query(sql):
        seen.append(sql)
        return [s for s in statements()] if "LIKE 'GO:%'" in sql else []

    r = ow.run_ontology_wave(query, ko_path=FIX / "ko_list_slice.tsv", ko_sha256=KO_SHA, snapshot_id=SNAP)
    assert seen and all(f"{ow.STATEMENTS_TABLE} VERSION AS OF {SNAP}" in q for q in seen)
    assert len(seen) == sum(1 for s in ow.ONTOLOGIES.values() if s["source"] == "statements")
    assert r.snapshot_id == SNAP and any(SNAP in m["method_name"] for m in r.methods)


def test_snapshot_is_resolved_once_when_not_given():
    calls = []

    def query(sql):
        calls.append(sql)
        if sql.endswith(".snapshots"):
            return [{"snapshot_id": 7, "committed_at": "2026-01-01"}, {"snapshot_id": 9, "committed_at": "2026-02-01"}]
        return []

    r = ow.run_ontology_wave(query, ko_path=None, ko_sha256=None)
    assert sum(q.endswith(".snapshots") for q in calls) == 1
    assert r.snapshot_id == "9" and all("VERSION AS OF 9" in q for q in calls if not q.endswith(".snapshots"))


def test_unresolvable_snapshot_is_an_error_not_a_guess():
    def query(sql):
        raise RuntimeError("400 Bad Request")

    with pytest.raises(ow.SnapshotResolutionError, match="pass snapshot_id explicitly"):
        ow.resolve_snapshot_id(query)
    with pytest.raises(ValueError, match="explicit Iceberg snapshot id"):
        ow.build_ontology_wave([], snapshot_id="latest")


# ---------------------------------------------------------------------------
# synonyms, definitions, Pfam
# ---------------------------------------------------------------------------


def test_related_narrow_and_broad_synonyms_produce_nothing():
    r = build()
    texts = {c["definition"] for c in r.content_rows["function"]}
    stmts = statements()
    ignored = [s for s in stmts if s["predicate"] in ow.UNUSED_SYNONYM_PREDICATES]
    assert len(ignored) == 12  # 6 related + 4 narrow + 2 broad, all real
    for s in ignored:
        assert s["value"] not in texts
    assert r.report["ignored_synonym_statements"] == {
        "oio:hasBroadSynonym": 2, "oio:hasNarrowSynonym": 4, "oio:hasRelatedSynonym": 6,
    }
    # removing every ignored statement changes nothing
    stripped = build([s for s in stmts if s["predicate"] not in ow.UNUSED_SYNONYM_PREDICATES])
    assert stripped.links == r.links and stripped.content_rows == r.content_rows


def test_non_pfam_definition_is_term_content_only():
    r = build()
    go_def = EXPECTED_TERMS["GO:0004022"][2]
    assert go_def not in {c["definition"] for c in r.content_rows["function"]}
    go_hash = T("GO:0004022")
    relations = {row["relation"] for row in r.links["function_x_ontology_term"] if row["object_hash"] == go_hash}
    assert relations == {"label", "exact_synonym"}


def test_pfam_emits_only_the_description_as_function_text():
    r = build()
    pf = [row for row in r.links["function_x_ontology_term"] if row["object_hash"] == T("PFAM:PF00001")]
    assert [(row["relation"], row["subject_hash"]) for row in pf] == [("definition", F(PFAM))]
    assert F("7tm_1") not in {c["entity_hash"] for c in r.content_rows["function"]}


def test_pfam_without_a_description_gets_no_function_link_but_a_map_row():
    stmts = [
        {"subject": "PFAM:PF99999", "predicate": "rdfs:label", "object": None, "value": "foo_1", "datatype": None, "language": None},
    ]  # SYNTHETIC
    r = build(stmts, ko_entries=None, cog_entries=None)
    assert [c["term_id"] for c in r.content_rows["ontology_term"]] == ["PFAM:PF99999"]
    assert r.links["function_x_ontology_term"] == [] and r.entity_rows["function"] == []
    assert r.term_function_map == [
        {"term_curie": "PFAM:PF99999", "relation": "", "standardized_text": "", "function_entity_hash": "", "status": "no_text"}
    ]


def test_iao_definition_beats_dcterms_description_for_pfam():
    stmts = [  # SYNTHETIC
        {"subject": "PFAM:PF99998", "predicate": "rdfs:label", "object": None, "value": "bar", "datatype": None, "language": None},
        {"subject": "PFAM:PF99998", "predicate": "dcterms:description", "object": None, "value": "Second choice", "datatype": None, "language": None},
        {"subject": "PFAM:PF99998", "predicate": "IAO:0000115", "object": None, "value": "First choice", "datatype": None, "language": None},
    ]
    r = build(stmts, ko_entries=None, cog_entries=None)
    assert r.content_rows["ontology_term"][0]["definition"] == "First choice"
    assert [c["definition"] for c in r.content_rows["function"]] == ["First choice"]
    assert json.loads(r.links["function_x_ontology_term"][0]["metadata"])["field"] == "IAO:0000115"


def test_select_literal_prefers_english_then_untagged_then_lexicographic():
    assert ow.select_literal([("zebra", None), ("Apple", "en"), ("mango", "en-US")]) == "Apple"
    assert ow.select_literal([("zebra", None), ("apple", None), ("Birne", "de")]) == "apple"
    assert ow.select_literal([("Birne", "de"), ("pomme", "fr")]) is None
    assert ow.select_literal([("  ", "en"), ("kept", None)]) == "kept"


def test_term_without_a_label_is_not_loaded():
    stmts = [{"subject": "GO:0000001", "predicate": "IAO:0000115", "object": None, "value": "def only", "datatype": None, "language": None}]
    r = build(stmts, ko_entries=None, cog_entries=None)
    assert r.content_rows["ontology_term"] == [] and r.report["unlabelled_subjects_skipped"] == 1


def test_modelseed_role_url_form_is_loaded_as_a_seed_role_term():
    stmts = [  # SYNTHETIC subject spelling, from the URL form the store holds
        {"subject": "<https://modelseed.org/template/roles/ftr00040>", "predicate": "rdfs:label", "object": None, "value": "Some role", "datatype": None, "language": None},
    ]
    r = build(stmts, ko_entries=None, cog_entries=None)
    assert r.content_rows["ontology_term"][0]["term_id"] == "SEED.ROLE:ftr00040"
    assert r.content_rows["ontology_term"][0]["entity_hash"] == T("SEED.ROLE:ftr00040")


# ---------------------------------------------------------------------------
# KO
# ---------------------------------------------------------------------------


def test_ko_parse_real_lines():
    k1, k4, k243 = ko_entries()
    assert (k1.ko_id, k1.name, k1.function_text, k1.ecs) == ("K00001", "ADH; alcohol dehydrogenase", "alcohol dehydrogenase", ("EC:1.1.1.1",))
    assert k4.ecs == ("EC:1.1.1.4", "EC:1.1.1.-", "EC:1.1.1.303")
    assert k4.function_text.startswith("(R,R)-butanediol dehydrogenase / meso")
    assert k243.function_text == "uncharacterized protein"


@pytest.mark.parametrize(
    "raw,name,function,ecs",
    [
        ("ADH; alcohol dehydrogenase [EC:1.1.1.1]", "ADH; alcohol dehydrogenase", "alcohol dehydrogenase", ("EC:1.1.1.1",)),
        ("glpC; glycerol-3-phosphate dehydrogenase subunit C", "glpC; glycerol-3-phosphate dehydrogenase subunit C", "glycerol-3-phosphate dehydrogenase subunit C", ()),  # no EC bracket
        ("alcohol dehydrogenase [EC:1.1.1.1]", "alcohol dehydrogenase", "alcohol dehydrogenase", ("EC:1.1.1.1",)),  # no '; ' -> whole text
        ("plain text with no symbols", "plain text with no symbols", "plain text with no symbols", ()),
        ("a, b; x; y [EC:1.1.1.1 1.1.1.2]", "a, b; x; y", "x; y", ("EC:1.1.1.1", "EC:1.1.1.2")),  # split at the FIRST '; ' only
        ("E1; thing [EC:2.7.7.-]", "E1; thing", "thing", ("EC:2.7.7.-",)),
    ],
)
def test_ko_function_text_edge_cases(raw, name, function, ecs):
    assert ow.ko_function_text(raw) == (name, function, ecs)


def test_ko_parse_refuses_malformed_and_duplicate_lines():
    with pytest.raises(ow.PinnedFileError, match="expected"):
        ow.parse_ko_lines(["K00001 no tab here"])
    with pytest.raises(ow.PinnedFileError, match="duplicate id K00001"):
        ow.parse_ko_lines(["K00001\ta; b", "K00001\tc; d"])


def test_stoplisted_ko_is_quarantined_not_loaded():
    r = build()
    assert F("uncharacterized protein") not in {c["entity_hash"] for c in r.content_rows["function"]}
    assert not [row for row in r.links["function_x_ontology_term"] if row["object_hash"] == T("KO:K00243")]
    assert T("KO:K00243") in {x["entity_hash"] for x in r.entity_rows["ontology_term"]}  # the term stays
    row = next(m for m in r.term_function_map if m["term_curie"] == "KO:K00243")
    assert row == {"term_curie": "KO:K00243", "relation": "definition", "standardized_text": "uncharacterized protein",
                   "function_entity_hash": "", "status": "quarantined"}
    assert r.report["function_texts_quarantined"] == 1


def test_stoplist_applies_to_store_texts_and_keeps_family_names():
    stmts = [  # SYNTHETIC labels
        {"subject": "GO:0000002", "predicate": "rdfs:label", "object": None, "value": "Hypothetical protein.", "datatype": None, "language": None},
        {"subject": "GO:0000002", "predicate": "oio:hasExactSynonym", "object": None, "value": "DUF1234 domain-containing protein", "datatype": None, "language": None},
    ]
    r = build(stmts, ko_entries=None, cog_entries=None)
    assert [c["definition"] for c in r.content_rows["function"]] == ["DUF1234 domain-containing protein"]
    rows = r.links["function_x_ontology_term"]
    assert [row["relation"] for row in rows] == ["exact_synonym"]
    assert r.term_function_map[0]["status"] == "quarantined" and r.term_function_map[0]["relation"] == "label"
    assert r.links["function_x_function"] == []  # one surviving text: no clique


# ---------------------------------------------------------------------------
# cross references
# ---------------------------------------------------------------------------


def test_xref_only_when_both_curies_are_loaded_and_unresolved_are_reported():
    r = build()
    rows = r.links["ontology_term_x_ontology_term"]
    assert len(rows) == 4
    assert r.report["unresolved_xrefs"] == [
        {"predicate": "kegg-ko:ec", "prefix": "EC", "count": 3},
        {"predicate": "oio:hasDbXref", "prefix": "Reactome", "count": 2},
        {"predicate": "oio:hasDbXref", "prefix": "kegg.reaction", "count": 2},
        {"predicate": "oio:hasDbXref", "prefix": "metacyc.reaction", "count": 2},
        {"predicate": "oio:hasDbXref", "prefix": "rhea", "count": 2},
    ]


def test_xref_is_directed_as_asserted_and_not_reordered():
    # SYNTHETIC: the reverse assertion EC -> GO exists as its own row.
    stmts = statements() + [
        {"subject": "EC:1.1.1.1", "predicate": "oio:hasDbXref", "object": None, "value": "GO:0004022", "datatype": None, "language": None},
    ]
    r = build(stmts, ko_entries=None, cog_entries=None)
    xr = [row for row in r.links["ontology_term_x_ontology_term"] if row["relation"] == "xref"
          and {row["subject_hash"], row["object_hash"]} == {T("GO:0004022"), T("EC:1.1.1.1")}]
    assert len(xr) == 2
    assert {(row["subject_hash"], row["object_hash"]) for row in xr} == {
        (T("GO:0004022"), T("EC:1.1.1.1")), (T("EC:1.1.1.1"), T("GO:0004022"))
    }
    for row in xr:
        m = json.loads(row["metadata"])
        assert row["subject_hash"] == T(m["asserting_curie"]) and row["object_hash"] == T(m["asserted_curie"])
    assert "xref" not in schema.SYMMETRIC_RELATIONS


def test_equivalent_class_between_loaded_terms_is_symmetric_exact_match():
    # SYNTHETIC: the store holds equivalentClass only to blank nodes for these ontologies.
    stmts = statements() + [
        {"subject": "GO:0004022", "predicate": "owl:equivalentClass", "object": "EC:1.1.1.1", "value": None, "datatype": None, "language": None},
        {"subject": "GO:0004022", "predicate": "owl:equivalentClass", "object": "_:riog0001", "value": None, "datatype": None, "language": None},
    ]
    r = build(stmts, ko_entries=None, cog_entries=None)
    em = [row for row in r.links["ontology_term_x_ontology_term"] if row["relation"] == "exact_match"]
    assert len(em) == 1
    a, b = sorted((T("GO:0004022"), T("EC:1.1.1.1")))
    assert (em[0]["subject_hash"], em[0]["object_hash"]) == (a, b)
    assert em[0]["link_key"] == xkey("GO:0004022", "owl:equivalentClass", "EC:1.1.1.1")
    m = json.loads(em[0]["metadata"])
    assert (m["asserting_curie"], m["asserted_curie"], m["source_predicate"]) == ("GO:0004022", "EC:1.1.1.1", "owl:equivalentClass")
    assert {"predicate": "owl:equivalentClass", "prefix": "_", "count": 1} in r.report["unresolved_xrefs"]


def test_self_xref_is_skipped_and_counted():
    stmts = statements() + [  # SYNTHETIC
        {"subject": "GO:0004022", "predicate": "oio:hasDbXref", "object": None, "value": "GO:0004022", "datatype": None, "language": None},
    ]
    r = build(stmts, ko_entries=None, cog_entries=None)
    assert r.report["skipped_self_pairs"] == 1
    assert len(r.links["ontology_term_x_ontology_term"]) == 3


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("EC:1.1.1.1", "EC:1.1.1.1"),
        ("ec:1.1.1.1", "EC:1.1.1.1"),
        ("rhea:19363;", "RHEA:19363"),
        ("RHEA:12345", "RHEA:12345"),
        ("GO:0004022", "GO:0004022"),
        ("GO:GO:0004022", "GO:0004022"),
        ("InterPro:IPR000001", "IPR:IPR000001"),
        ("Pfam:PF00001", "PFAM:PF00001"),
        ("seed.reaction:rxn01466", "seed.reaction:rxn01466"),
        ("<https://modelseed.org/template/roles/ftr00040>", "seed.role:ftr00040"),
        ("Reactome:R-HSA-71707", None),
        ("kegg.reaction:R02003", None),
        ("_:riog0001", None),
        ("noprefix", None),
        ("GO:", None),
        (None, None),
    ],
)
def test_map_xref(raw, expected):
    assert ow.map_xref(raw) == expected


def test_ko_to_ec_xref_is_directed_with_its_own_method():
    r = build()
    row = [x for x in r.links["ontology_term_x_ontology_term"] if x["method_name"] == M_KO_EC]
    assert row == [EXPECTED_OTT[3]]
    assert row[0]["subject_hash"] == T("KO:K00001") and row[0]["object_hash"] == T("EC:1.1.1.1")


# ---------------------------------------------------------------------------
# alias cliques
# ---------------------------------------------------------------------------


def test_alias_clique_is_every_pair_byte_ordered_and_largest_is_reported():
    stmts = [{"subject": "GO:0000003", "predicate": "rdfs:label", "object": None, "value": "alpha", "datatype": None, "language": None}] + [
        {"subject": "GO:0000003", "predicate": "oio:hasExactSynonym", "object": None, "value": v, "datatype": None, "language": None}
        for v in ("beta", "gamma", "delta", "Alpha.")  # 'Alpha.' standardizes to 'alpha': a self-pair, skipped
    ]  # SYNTHETIC
    r = build(stmts, ko_entries=None, cog_entries=None)
    rows = r.links["function_x_function"]
    assert len(rows) == 6  # C(4,2) over alpha, beta, gamma, delta
    for row in rows:
        assert row["subject_hash"] < row["object_hash"] and row["link_key"] == "GO:0000003"
        assert json.loads(row["metadata"]) == {"via_term": "GO:0000003"}
    assert r.report["largest_alias_clique"] == {"term": "GO:0000003", "function_texts": 4}
    # the duplicate standardized text collapsed to ONE exact_synonym-free link set
    assert len(r.links["function_x_ontology_term"]) == 5


def test_largest_clique_on_the_real_fixture():
    assert build().report["largest_alias_clique"] == {"term": "EC:1.1.1.1", "function_texts": 2}


# ---------------------------------------------------------------------------
# the persisted map and the dry run
# ---------------------------------------------------------------------------


def test_term_function_map_rows_and_tsv(tmp_path):
    r = build()
    got = {m["term_curie"]: m for m in r.term_function_map}
    assert set(got) == {"GO:0004022", "EC:1.1.1.1", "SEED.ROLE:0000000000001", "IPR:IPR000001", "PFAM:PF00001",
                        "KO:K00001", "KO:K00004", "KO:K00243", "COG:COG0001", "COG:COG0003", "COG:COG0593"}
    assert got["GO:0004022"] == {"term_curie": "GO:0004022", "relation": "label", "standardized_text": GO_LABEL,
                                 "function_entity_hash": F(GO_LABEL), "status": "loaded"}
    assert got["PFAM:PF00001"]["relation"] == "definition" and got["PFAM:PF00001"]["standardized_text"] == PFAM
    assert got["KO:K00001"]["function_entity_hash"] == F(EC_LABEL)  # KO and EC share one function entity
    path = ow.write_term_function_map(r.term_function_map, tmp_path / "out" / "term_function_map.tsv")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].split("\t") == list(ow.TERM_FUNCTION_MAP_COLUMNS)
    assert len(lines) == 1 + 11 and all(len(line.split("\t")) == 5 for line in lines)
    assert f"KO:K00243\tdefinition\tuncharacterized protein\t\tquarantined" in lines


def test_run_writes_the_map_next_to_the_run_output(tmp_path):
    r = ow.run_ontology_wave(lambda sql: statements() if "LIKE 'GO:%'" in sql else [], ko_path=FIX / "ko_list_slice.tsv",
                             ko_sha256=KO_SHA, cog_path=FIX / "cog_def_slice.tab", cog_sha256=COG_SHA,
                             snapshot_id=SNAP, output_dir=tmp_path)
    assert (tmp_path / "term_function_map.tsv").exists() and r.term_function_map


def test_dry_run_summary_counts_per_table_and_method():
    s = build().dry_run_summary()
    assert s["snapshot_id"] == SNAP and s["ko_sha256"] == KO_SHA
    assert s["tables"] == {
        "ontology_term_entity": 14, "ontology_term_content": 14,
        "function_entity": 11, "function_content": 11,
        "method": 6,
        "function_x_ontology_term": 12, "function_x_function": 2, "ontology_term_x_ontology_term": 4,
    }
    assert s["methods"] == {
        M_DEF: {"function_x_ontology_term": 5},
        M_SYN: {"function_x_ontology_term": 2, "function_x_function": 2},
        M_XREF: {"ontology_term_x_ontology_term": 3},
        M_KO: {"function_x_ontology_term": 2},
        M_KO_EC: {"ontology_term_x_ontology_term": 1},
        M_COG: {"function_x_ontology_term": 3},
    }
    assert s["term_function_map_rows"] == 11
    json.dumps(s)  # serializable; carries no row bodies
    assert "rows" not in s


def test_ontologies_constant_covers_the_prd_list():
    assert {n for n, s in ow.ONTOLOGIES.items() if s["function"]} == {"GO", "EC", "SEED_ROLE", "InterPro", "Pfam", "KO", "COG"}
    assert {n for n, s in ow.ONTOLOGIES.items() if not s["function"]} == {
        "Rhea", "SEED_reaction", "SEED_subsystem", "MetaCyc_pathway", "KEGG_pathway"}
    assert "oio:hasExactSynonym" in ow.SYNONYM_PREDICATES and len(ow.SYNONYM_PREDICATES) == 1
