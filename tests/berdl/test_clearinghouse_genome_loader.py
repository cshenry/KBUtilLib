"""lw-9: the KBDL genome loader.

FIXTURES (tests/berdl/fixtures/kbdl_genome_loader/, see its PROVENANCE.md):
the annotation slice is the REAL ADP1 ``annot_result_f072e65c...json`` entries
for three genes verbatim. The genome slice is a CanonicalGenome-shaped object
RECONSTRUCTED from the real NCBI GCF_000046845.1 sequence and CDS coordinates
(the KBDL genome object aa50dced... itself is not reachable from a laptop);
the BAKTA/PROKKA function texts agree with the NCBI products for the ordinal
mapping. Where the REAL files exist on disk (the full 3,256-gene result, the
three ledgers, the sweeper mirror) the tests also run against them and skip
cleanly when they do not.

The lake is lw-1's in-memory ``_MiniLake``, which STORES what ``load()`` is
handed, so "a re-run writes zero rows" is earned rather than asserted.
"""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
import re
from pathlib import Path

import pytest

from kbutillib.domains.kbase.berdl import clearinghouse_genome_inputs as gi
from kbutillib.domains.kbase.berdl import clearinghouse_genome_loader as gl
from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci
from kbutillib.domains.kbase.berdl import clearinghouse_links as cl
from kbutillib.domains.kbase.berdl import clearinghouse_schema as schema
from kbutillib.domains.kbase.berdl.capability import BerdlLoadRefusedError
from kbutillib.domains.kbase.berdl.clearinghouse_capability import ClearinghouseCapability

from .test_clearinghouse_links import _MiniLake

FIX = Path(__file__).parent / "fixtures" / "kbdl_genome_loader"
RUNS = Path("~/Dropbox/Science/runs").expanduser()
ADP1_DIR = RUNS / gi.ADP1_DIR
MIRROR = Path("~/Dropbox/Science/kbdl-job-results").expanduser()

OBJECT_ID = "aa50dcedd9471457172a79ffecfdd007cc1599972c25e133e1404421b96b3b6a"
EXTERNAL = {
    OBJECT_ID: {
        "ncbi_assembly": "GCF_000046845.1",
        "fasta_object_id": "82eef07a1eadf57a3e00e1a14ec04072b9f68567252c77d88a4f3886922bceab",
        "taxon_id": "62977",
        "gene_call_tool": "SOURCE_GFF",
    }
}

SHA = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()  # noqa: E731
FN = lambda text: SHA(text)  # a function entity hash of ALREADY-standardized text  # noqa: E731


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(ci.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(ci.LOCK_DIR_ENV, str(tmp_path / "locks"))


def genome():
    return json.loads((FIX / "adp1_genome_slice.json").read_text())


def annotation():
    return json.loads((FIX / "adp1_annotation_slice.json").read_text())


def term_map():
    return gi.read_term_function_map(FIX / "term_function_map.tsv")


def build(gen=None, ann=None, tmap=None, external=None):
    return gl.build_genome_rows(
        genome() if gen is None else gen,
        genome_object_id=OBJECT_ID,
        annotation_results=[annotation()] if ann is None else ann,
        term_function_map=term_map() if tmap is None else tmap,
        external=EXTERNAL[OBJECT_ID] if external is None else external,
    )


# ---------------------------------------------------------------------------
# the method registry and the NAME CONFLICT
# ---------------------------------------------------------------------------

LW1_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*@[A-Za-z0-9._+-]+/[a-z0-9][a-z0-9._-]*$")

EXPECTED_NAMES = {
    "PROKKA": "prokka@1.14.6/kbdl-default",
    "BAKTA": "bakta@1.11.4/kbdl-default",
    "KOFAMSCAN": "kofamscan@1.3.0/kbdl-default",
    "DRAM2": "dram2@2.0.0-beta17/kbdl-default",
    "TRANSYT": "transyt@sha256-92f6e78fbdb8dc1952ad0c4130776f4fe6f5cf524b3f4f3cbd30b3ddbe8c4fe7/kbdl-default",
    "RAST_API": "rast_api@2018-0531/kbdl-default",
    "BAKTA_CALL": "bakta_call@1.11.4/kbdl-default",
    "PYRODIGAL": "pyrodigal@3.7.1/kbdl-default",
    "PYARAGORN": "pyaragorn@0.3.0/kbdl-default",
    "PYBARRNAP": "pybarrnap@0.5.2/kbdl-default",
    "SOURCE_GFF": "source_gff@unknown/kbdl-default",
}


def test_registry_has_the_eleven_measured_tools_and_names_match_the_mapping():
    assert {t: e["method_name"] for t, e in gl.KBDL_ANNOTATOR_METHODS.items()} == EXPECTED_NAMES


@pytest.mark.parametrize("tool", sorted(EXPECTED_NAMES))
def test_every_registry_name_fullmatches_the_lw1_pattern_and_the_raw_one_is_kept(tool):
    entry = gl.KBDL_ANNOTATOR_METHODS[tool]
    name = entry["method_name"]
    assert LW1_PATTERN.fullmatch(name) and len(name) <= 128
    assert schema.METHOD_NAME_PATTERN.fullmatch(name)
    # the registry's own string is upper-case (and TRANSYT's has '/' ':' '@'): it could not be loaded
    assert not LW1_PATTERN.fullmatch(entry["registry_method_name"])


def test_method_rows_keep_the_measured_raw_values_and_validate():
    rows = {r["method_name"]: r for r in gl.annotator_method_rows()}
    assert set(rows) == set(EXPECTED_NAMES.values())
    transyt = rows[EXPECTED_NAMES["TRANSYT"]]
    assert transyt["algorithm_version"] == (
        "kbutillib/transyt:latest@sha256:92f6e78fbdb8dc1952ad0c4130776f4fe6f5cf524b3f4f3cbd30b3ddbe8c4fe7"
    )
    assert rows[EXPECTED_NAMES["SOURCE_GFF"]]["algorithm_version"] == "unknown"
    assert rows[EXPECTED_NAMES["PROKKA"]]["algorithm_version"] == "1.14.6"
    assert rows[EXPECTED_NAMES["PROKKA"]]["parameters_json"] == (
        '{"force":true,"gcode":11,"input":"one contig per nucleotide CDS","prefix":"prokka","quiet":true}'
    )
    for row in rows.values():
        assert row["source_dataset"] == gl.KBDL_ANNOTATOR_METHODS_SHA256
        assert not re.search(r"\d+ (genes|proteins|rows|links)", row["description"])  # never run counts


def test_a_trailing_newline_in_a_method_name_is_refused():
    row = gl.annotator_method_row("PROKKA")
    with pytest.raises(cl.LinkRefusedError, match="method_name"):
        cl.validate_method_rows([{**row, "method_name": row["method_name"] + "\n"}])


# ---------------------------------------------------------------------------
# the classifier, on the real PROKKA / DRAM2 lists
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,kind",
    [
        ("K02313", "ko"), ("COG0593", "cog"), ("GO:0003677", "go"), ("5.6.2.2", "ec"), ("3.4.-.-", "ec"),
        ("1.2.3.n4", "ec"), ("VOG02491", "vog"), ("dnaA", "symbol"), ("hypothetical protein", "text"),
        ("DNA gyrase subunit B [EC:5.6.2.2]", "text"), ("K0231", "symbol"), ("COG059", "symbol"),
        ("GO:123", "symbol"), ("2.7.7", "symbol"),
    ],
)
def test_classifier_explicit_patterns(value, kind):
    assert gl.classify_function_entry(value)[0] == kind


def test_classifier_on_the_fixture_prokka_and_dram2_lists():
    ann = annotation()
    got = {
        gene: {
            tool: [gl.classify_function_entry(v)[0] for v in ann[gene][tool]["FUNCTION"]]
            for tool in ("PROKKA", "DRAM2")
            if tool in ann[gene]
        }
        for gene in ann
    }
    assert got["82eef07a1eadf57a_1"] == {"PROKKA": ["text", "symbol", "cog"], "DRAM2": ["vog", "ko", "text"]}
    assert got["82eef07a1eadf57a_4"] == {"PROKKA": ["text", "ec", "symbol", "cog"], "DRAM2": ["vog", "ko", "ec", "text"]}
    assert got["82eef07a1eadf57a_5"] == {"PROKKA": ["text"]}


@pytest.mark.skipif(not (ADP1_DIR / "job_record.json").exists(), reason="real ADP1 files not on this machine")
def test_classifier_on_the_real_adp1_prokka_and_dram2_lists():
    result = json.loads(next(ADP1_DIR.glob("annot_result_*.json")).read_text())
    record = json.loads((ADP1_DIR / "job_record.json").read_text())
    assert len(result) == record["n_genes_out"] == 3256
    kinds = {}
    for entry in result.values():
        for tool in ("PROKKA", "DRAM2"):
            for value in entry.get(tool, {}).get("FUNCTION", []):
                kind, _ = gl.classify_function_entry(value)
                kinds.setdefault((tool, kind), []).append(value)
    for (tool, kind), values in kinds.items():
        if kind == "ko":
            assert all(re.fullmatch(r"K\d{5}", v) for v in values)
        if kind == "cog":
            assert all(re.fullmatch(r"COG\d{4}", v) for v in values)
        if kind == "text":
            assert all(re.search(r"\s", v) or not v.isalnum() for v in values), (tool, [v for v in values if v.isalnum()][:5])
        if kind == "symbol":
            assert all(not re.search(r"\s", v) for v in values)
    assert ("PROKKA", "text") in kinds and ("PROKKA", "cog") in kinds and ("DRAM2", "ko") in kinds
    assert ("DRAM2", "vog") in kinds and ("PROKKA", "symbol") in kinds
    # the FUNCTION lists carry term ids of every classified kind between them
    assert {k for (_t, k) in kinds} >= {"ko", "cog", "ec", "vog", "symbol", "text"}


# ---------------------------------------------------------------------------
# the hand-written expected rows for three genes (one minus-strand)
# ---------------------------------------------------------------------------

CONTIG = genome()["contigs"][0]["sequence"].upper()
CONTIG_HASH = SHA(CONTIG)
GENOME_HASH = SHA(CONTIG_HASH)  # v2.0: sha256("_".join(sorted(contig hashes))), one contig
P = "KBDL:" + GENOME_HASH[:16]
GENES = {
    # feature id: (gene id, begin, end, strand, locus tag)
    "82eef07a1eadf57a_1": (P + ".1.1", 201, 1598, 1, "ACIAD_RS00005"),
    "82eef07a1eadf57a_4": (P + ".1.2", 4127, 6595, 1, "ACIAD_RS00020"),
    "82eef07a1eadf57a_5": (P + ".1.3", 6712, 6948, -1, "ACIAD_RS00025"),
}


def test_genome_gene_protein_rows_and_the_gene_ids():
    rows = build()
    assert rows.genome_hash == GENOME_HASH == "0842a6c7bc0d4297eae11b9cc6acd8c49120975befc228e186c8acc7560a9fc1"
    assert [e["entity_hash"] for e in rows.entity_rows["genome"]] == [GENOME_HASH]
    assert rows.content_rows["genome"] == [
        {
            "entity_hash": GENOME_HASH,
            "fasta_reference": EXTERNAL[OBJECT_ID]["fasta_object_id"],
            "taxon_id": "62977",
            "assembly_accession": "GCF_000046845.1",
            "n_contigs": 1,
            "total_length": 7000,
            "is_closed": None,
            "standardizer_version": "2.0",
        }
    ]
    assert rows.alias_rows == [
        {"entity_hash": GENOME_HASH, "alias_source": "kbdl_object", "alias_id": OBJECT_ID},
        {"entity_hash": GENOME_HASH, "alias_source": "ncbi_assembly", "alias_id": "GCF_000046845.1"},
    ]
    gen = genome()
    for feature in gen["features"]:
        dna = feature["dna_sequence"].upper()
        prot = feature["protein_translation"].upper()
        assert {"entity_hash": SHA(dna), "entity_type": "gene", "standardizer_version": "2.0"} in rows.entity_rows["gene"]
        assert {
            "entity_hash": SHA(dna), "sequence": dna, "seq_length": len(dna), "protein_entity_hash": None,
            "standardizer_version": "2.0",
        } in rows.content_rows["gene"]
        assert {"entity_hash": SHA(prot), "sequence": prot, "seq_length": len(prot), "standardizer_version": "2.0"} in rows.content_rows["protein"]
    assert len(rows.entity_rows["gene"]) == len(rows.entity_rows["protein"]) == 3


def test_gene_x_genome_and_gene_x_protein_rows_for_the_three_genes():
    rows = build()
    by_feature = {f["id"]: f for f in genome()["features"]}
    expected_gxg, expected_gxp = [], []
    for fid, (gene_id, begin, end, strand, locus) in GENES.items():
        feature = by_feature[fid]
        gene_hash = SHA(feature["dna_sequence"].upper())
        protein_hash = SHA(feature["protein_translation"].upper())
        expected_gxg.append(
            {
                "subject_hash": gene_hash, "object_hash": GENOME_HASH, "relation": "occurs_in",
                "method_name": "source_gff@unknown/kbdl-default", "link_key": gene_id,
                "metadata": json.dumps(
                    {"begin": begin, "contig_hash": CONTIG_HASH, "end": end, "feature_id": fid, "gene_id": gene_id,
                     "partial_begin": False, "partial_end": False, "source_id": locus, "strand": strand},
                    sort_keys=True, separators=(",", ":")),
            }
        )
        expected_gxp.append(
            {
                "subject_hash": gene_hash, "object_hash": protein_hash, "relation": "encodes",
                "method_name": "source_gff@unknown/kbdl-default", "link_key": gene_id,
                "metadata": json.dumps({"gene_id": gene_id, "translation_table": 11}, sort_keys=True, separators=(",", ":")),
            }
        )
    assert rows.links["gene_x_genome"] == expected_gxg
    assert rows.links["gene_x_protein"] == expected_gxp
    # the minus-strand gene: begin = start - length + 1, end = start
    assert (6712, 6948) == (6948 - 237 + 1, 6948)


def _std(text):
    return text.strip().lower()


#: (feature, tool, link_key, standardized function text) -- written by hand from the
#: real ADP1 result and the term map. '' link_key = free text.
EXPECTED_PXF = {
    "82eef07a1eadf57a_1": {
        ("prokka", "", "chromosomal replication initiator protein dnaa"),
        ("prokka", "COG:COG0593", "chromosomal replication initiation atpase dnaa"),
        ("dram2", "KO:K02313", "chromosomal replication initiator protein"),
        ("dram2", "", "chromosomal replication initiator protein"),
        ("rast_api", "", "chromosomal replication initiator protein dnaa"),
        ("bakta", "", "chromosomal replication initiator protein dnaa"),
        ("bakta", "COG:COG0593", "chromosomal replication initiation atpase dnaa"),
        ("bakta", "GO:0003677", "dna binding"),
        ("bakta", "GO:0003688", "dna replication origin binding"),
        ("bakta", "GO:0005524", "atp binding"),
        ("bakta", "GO:0005737", "cytoplasm"),
        ("bakta", "GO:0005886", "plasma membrane"),
        ("bakta", "GO:0006270", "dna replication initiation"),
        ("bakta", "GO:0006275", "regulation of dna replication"),
        ("bakta", "GO:0016887", "atp hydrolysis activity"),
        ("kofamscan", "KO:K02313", "chromosomal replication initiator protein"),
    },
    "82eef07a1eadf57a_4": {
        ("prokka", "", "dna gyrase subunit b"),
        ("prokka", "EC:5.6.2.2", "dna topoisomerase (atp-hydrolyzing)"),
        ("prokka", "COG:COG0187", "dna gyrase/topoisomerase iv, subunit b"),
        ("dram2", "KO:K02470", "dna gyrase subunit b"),
        ("dram2", "EC:5.6.2.2", "dna topoisomerase (atp-hydrolyzing)"),
        ("dram2", "", "dna gyrase subunit b [ec:5.6.2.2]"),
        ("rast_api", "", "dna gyrase subunit b (ec 5.99.1.3)"),
        ("bakta", "", "dna topoisomerase (atp-hydrolyzing) subunit b"),
        ("bakta", "EC:5.6.2.2", "dna topoisomerase (atp-hydrolyzing)"),
        ("bakta", "KO:K02470", "dna gyrase subunit b"),
        ("bakta", "GO:0003677", "dna binding"),
        ("bakta", "GO:0003918", "dna topoisomerase type ii (double strand cut, atp-hydrolyzing) activity"),
        ("bakta", "GO:0005524", "atp binding"),
        ("bakta", "GO:0005694", "chromosome"),
        ("bakta", "GO:0006265", "dna topological change"),
        ("kofamscan", "KO:K02470", "dna gyrase subunit b"),
    },
    # 'hypothetical protein' (PROKKA, RAST) is stop-listed
    "82eef07a1eadf57a_5": {("bakta", "", "duf2171 domain-containing protein")},
}


def test_protein_x_function_rows_for_the_three_genes():
    rows = build()
    by_feature = {f["id"]: f for f in genome()["features"]}
    got = {(r["subject_hash"], r["method_name"].split("@")[0], r["link_key"], r["object_hash"]) for r in rows.links["protein_x_function"]}
    expected = set()
    for fid, items in EXPECTED_PXF.items():
        protein_hash = SHA(by_feature[fid]["protein_translation"].upper())
        for tool, key, text in items:
            expected.add((protein_hash, tool, key, FN(text)))
    assert got == expected
    for row in rows.links["protein_x_function"]:
        assert row["relation"] == "annotated_as"
        if row["link_key"]:
            meta = json.loads(row["metadata"])
            assert meta["term_id"] == row["link_key"]
            assert ("ko_id" in meta) == row["method_name"].startswith("kofamscan@")
        else:
            assert row["metadata"] == "{}"
    # a function entity + content row exists for every linked function (same batch)
    linked = {r["object_hash"] for r in rows.links["protein_x_function"]}
    assert {e["entity_hash"] for e in rows.entity_rows["function"]} == linked
    assert {c["entity_hash"] for c in rows.content_rows["function"]} == linked


def test_ignores_and_counts_kofamscan_function_transyt_vog_symbol_and_stoplist():
    ann = annotation()
    ann["82eef07a1eadf57a_5"]["TRANSYT"] = {
        "TC": ["transporter", "transporter"], "MSCPD": ["cytosine", "H+"],
        "MSRXN": ["cpd00067[e0] <=> cpd00067[c0]"], "TRANSYT_RXN": ["cpd00092[e0] => cpd00092[c0]"],
    }
    skips = build(ann=[ann]).report.skips
    assert skips["kofamscan_function_ignored"] == 2
    assert skips["transyt_not_loaded:TRANSYT/TC"] == 1  # identical assertions collapse by value
    assert skips["transyt_not_loaded:TRANSYT/MSCPD"] == 2
    assert skips["transyt_not_loaded:TRANSYT/MSRXN"] == 1
    assert skips["transyt_not_loaded:TRANSYT/TRANSYT_RXN"] == 1
    assert skips["vog_not_loaded:DRAM2/FUNCTION"] == 2
    assert skips["gene_symbol_not_loaded:PROKKA/FUNCTION"] == 2
    assert skips["stoplisted_function_text:PROKKA/FUNCTION"] == 1
    assert skips["stoplisted_function_text:RAST_API/role"] == 1


def test_only_registry_methods_are_cited_and_transyt_is_never_registered():
    rows = build()
    cited = {r["method_name"] for table in rows.links.values() for r in table}
    assert cited <= set(EXPECTED_NAMES.values())
    assert EXPECTED_NAMES["TRANSYT"] not in cited
    assert {m["method_name"] for m in rows.methods} == cited
    assert EXPECTED_NAMES["SOURCE_GFF"] in cited and EXPECTED_NAMES["PYRODIGAL"] not in cited


def test_a_tool_missing_from_the_registry_has_its_links_refused_and_reported():
    ann = annotation()
    ann["82eef07a1eadf57a_1"]["PSORTB"] = {"FUNCTION": ["Cytoplasmic"]}
    rows = build(ann=[ann])
    assert rows.report.refused_tools == {"PSORTB": 1}
    assert rows.report.skips["tool_not_in_registry:PSORTB"] == 1
    assert not any("psortb" in r["method_name"] for t in rows.links.values() for r in t)


def test_term_absent_from_the_map_or_without_text_writes_nothing_and_is_counted():
    tmap = term_map()
    del tmap["GO:0003677"]
    tmap["GO:0005524"] = {**tmap["GO:0005524"], "status": "quarantined", "function_entity_hash": ""}
    tmap["GO:0005737"] = {**tmap["GO:0005737"], "status": "no_text", "function_entity_hash": ""}
    rows = build(tmap=tmap)
    keys = {r["link_key"] for r in rows.links["protein_x_function"]}
    assert not keys & {"GO:0003677", "GO:0005524", "GO:0005737"}
    assert rows.report.skips["term_absent_from_map:BAKTA/GO"] == 2  # gene 1 and gene 4
    assert rows.report.skips["term_function_text_quarantined:BAKTA/GO"] == 2
    assert rows.report.skips["term_function_text_no_text:BAKTA/GO"] == 1


def test_a_malformed_term_id_in_a_term_namespace_is_counted_not_loaded():
    ann = annotation()
    ann["82eef07a1eadf57a_1"]["BAKTA"]["GO"] = ["GO:0003677", "GO:12", "not a term"]
    rows = build(ann=[ann])
    assert rows.report.skips["malformed_term_id:BAKTA/GO"] == 2


# ---------------------------------------------------------------------------
# skips: mismatch, multi-location, no annotation
# ---------------------------------------------------------------------------


def test_dna_sequence_that_is_not_the_contig_slice_is_reported_and_skipped():
    gen = genome()
    seq = gen["features"][0]["dna_sequence"]
    gen["features"][0]["dna_sequence"] = seq[:10] + ("A" if seq[10] != "A" else "C") + seq[11:]
    rows = build(gen=gen)
    assert rows.report.skips["dna_sequence_mismatch"] == 1
    assert rows.report.skipped_features["dna_sequence_mismatch"] == ["82eef07a1eadf57a_1"]
    assert rows.report.genes_loaded == 2
    assert len(rows.links["gene_x_genome"]) == 2
    # the neighbours keep their ordinals: the gene ids do not shift
    assert {json.loads(r["metadata"])["gene_id"] for r in rows.links["gene_x_genome"]} == {P + ".1.2", P + ".1.3"}


def test_a_minus_strand_gene_given_on_the_plus_strand_is_a_mismatch():
    gen = genome()
    minus = gen["features"][2]
    assert minus["location"][0][2] == "-"
    minus["dna_sequence"] = minus["dna_sequence"][::-1].translate(str.maketrans("ACGT", "TGCA"))  # revcomp: now plus-strand text
    rows = build(gen=gen)
    assert rows.report.skipped_features["dna_sequence_mismatch"] == ["82eef07a1eadf57a_5"]


def test_multi_location_feature_is_reported_and_skipped():
    gen = genome()
    gen["features"][1]["location"].append(["NC_005966.1", 100, "+", 30])
    rows = build(gen=gen)
    assert rows.report.skips["multi_location_feature"] == 1
    assert rows.report.skipped_features["multi_location_feature"] == ["82eef07a1eadf57a_4"]
    assert rows.report.genes_loaded == 2


def test_a_gene_with_no_annotation_loads_without_function_links():
    ann = annotation()
    del ann["82eef07a1eadf57a_4"]
    rows = build(ann=[ann])
    assert rows.report.genes_without_annotation == 1
    assert rows.report.genes_loaded == 3
    gyrb_protein = SHA(genome()["features"][1]["protein_translation"].upper())
    assert gyrb_protein in {e["entity_hash"] for e in rows.entity_rows["protein"]}
    assert gyrb_protein not in {r["subject_hash"] for r in rows.links["protein_x_function"]}
    assert any(r["object_hash"] == gyrb_protein for r in rows.links["gene_x_protein"])


def test_no_result_at_all_loads_genes_and_proteins_only():
    rows = build(ann=[])
    assert rows.links["protein_x_function"] == []
    assert len(rows.links["gene_x_genome"]) == len(rows.links["gene_x_protein"]) == 3
    assert rows.report.genes_without_annotation == 3
    assert [m["method_name"] for m in rows.methods] == [EXPECTED_NAMES["SOURCE_GFF"]]


def test_two_results_for_one_genome_are_unioned_and_identical_assertions_collapse():
    one = annotation()
    two = copy.deepcopy(one)
    two["82eef07a1eadf57a_1"]["PROKKA"]["FUNCTION"].append("Extra function text")
    both = build(ann=[one, two]).links["protein_x_function"]
    single = build(ann=[one]).links["protein_x_function"]
    assert len(both) == len(single) + 1


def test_gene_call_tool_defaults_to_pyrodigal_and_is_reported_and_unknown_tools_refused():
    rows = build(external={})
    assert rows.report.gene_call_tool == "PYRODIGAL" and rows.report.gene_call_tool_defaulted
    assert {r["method_name"] for r in rows.links["gene_x_genome"]} == {EXPECTED_NAMES["PYRODIGAL"]}
    assert any("fasta_reference unknown" in n for n in rows.report.notes)
    with pytest.raises(gl.GenomeObjectError, match="gene_call_tool"):
        build(external={"gene_call_tool": "PROKKA"})


@pytest.mark.parametrize("break_it", ["genome_id", "contigs", "features"])
def test_a_malformed_genome_object_is_refused(break_it):
    gen = genome()
    del gen[break_it]
    with pytest.raises(gl.GenomeObjectError):
        build(gen=gen)


# ---------------------------------------------------------------------------
# the annotation archive
# ---------------------------------------------------------------------------


def _write_result(directory, job_id, result, *, gz=True):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"{job_id}.json.gz" if gz else f"{job_id}.json")
    (gzip.open(path, "wt") if gz else path.open("w")).write(json.dumps(result))
    return path


def test_archive_index_finds_results_by_gene_id_prefix_and_caches(tmp_path):
    archive = tmp_path / "archive"
    a = _write_result(archive, "job-a", {"82eef07a1eadf57a_1": {}, "82eef07a1eadf57a_2": {}})
    b = _write_result(archive, "job-b", {"82eef07a1eadf57a_9": {}})
    c = _write_result(archive, "job-c", {"OWC_1686_k121_60869_5": {}})
    _write_result(archive, "job-empty", {})
    cache = tmp_path / "idx.json"
    index = gi.AnnotationArchiveIndex([archive], cache_path=cache).refresh()
    assert index.files_for("82eef07a1eadf57a") == [a, b]
    assert index.files_for("OWC_1686_k121_60869") == [c]
    assert index.files_for("nope") == []
    assert index.scanned == 4
    again = gi.AnnotationArchiveIndex([archive], cache_path=cache).refresh()
    assert again.scanned == 0 and again.files_for("82eef07a1eadf57a") == [a, b]
    _write_result(archive, "job-d", {"82eef07a1eadf57a_3": {}})
    assert len(again.refresh().files_for("82eef07a1eadf57a")) == 3 and again.scanned == 1


def test_archive_index_indexes_a_job_present_in_two_directories_once(tmp_path):
    first, second = tmp_path / "primary", tmp_path / "mirror"
    _write_result(first, "job-a", {"p_1": {}})
    _write_result(second, "job-a", {"p_1": {}})
    assert len(gi.AnnotationArchiveIndex([first, second]).refresh().files_for("p")) == 1


@pytest.mark.skipif(not (MIRROR / "KBDLGenomeAnnotation").is_dir(), reason="sweeper mirror not on this machine")
def test_the_real_sweeper_mirror_finds_the_adp1_result_by_prefix(tmp_path):
    index = gi.AnnotationArchiveIndex([MIRROR / "KBDLGenomeAnnotation"], cache_path=tmp_path / "idx.json").refresh()
    files = index.files_for("82eef07a1eadf57a")
    assert [f.name for f in files] == ["f072e65c6f784629852a922b3bad4d75.json.gz"]
    assert len(gi.AnnotationArchiveIndex.load(files[0])) == 3256
    assert index.prefixes > 100


def test_live_fallback_fetches_only_the_ledger_jobs_not_archived(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"job_id": "j1", "job_type": "KBDLGenomeAnnotation", "locus_prefix": "pfx", "outcome": "saved"},
                {"job_id": "j2", "job_type": "KBDLGenomeAnnotation", "locus_prefix": "pfx", "outcome": "saved"},
                {"job_id": "j3", "job_type": "KBDLGenomeAnnotation", "locus_prefix": "other", "outcome": "saved"},
            )
        )
    )

    class Client:
        def get_job_result(self, job_id):
            if job_id == "j2":
                raise RuntimeError("result for job j2 is no longer available")
            return {"pfx_1": {}}

    got = gi.live_results_for_prefix("pfx", ledger_path=ledger, client=Client())
    assert [j for j, _ in got] == ["j1"]
    assert gi.live_results_for_prefix("pfx", ledger_path=ledger, client=Client(), archived_job_ids=["j1"]) == []


# ---------------------------------------------------------------------------
# external ids from the three ledgers
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not ((ADP1_DIR / "job_record.json").exists() and (RUNS / gi.FITNESS_LEDGER).exists() and (RUNS / gi.PMI_LEDGER).exists()),
    reason="the three real ledgers are not on this machine",
)
def test_the_external_id_map_from_the_three_real_ledgers():
    built = gi.build_external_ids(RUNS, build_results_dir=MIRROR / "KBDLBuildGenome")
    ext = built.external_ids
    # 1 + 48 + 18 ledger rows, but two Fitness organisms (SyringaeB728a and its mexB deletion)
    # are ONE genome object: 66 objects, 67 external ids
    assert len(ext) == 66
    assert ext["b3b66173c92a0f54571e5a09d8cac7eeaa756228b90cd1076e13569985057734"]["fitness_org"] == [
        "SyringaeB728a", "SyringaeB728a_mexBdelta"]
    adp1 = ext[OBJECT_ID]
    assert adp1["ncbi_assembly"] == "GCF_000046845.1" and adp1["taxon_id"] == "62977"
    assert adp1["fasta_object_id"] == EXTERNAL[OBJECT_ID]["fasta_object_id"]
    assert sum(len(e["fitness_org"]) if isinstance(e["fitness_org"], list) else 1 for e in ext.values() if "fitness_org" in e) == 48
    assert sum("img_taxon" in e for e in ext.values()) == 18
    assert all(e["gene_call_tool"] == "SOURCE_GFF" for e in ext.values())
    # the ANI-nearest reference in a ledger is NOT the genome's assembly
    assert sum("ncbi_assembly" in e for e in ext.values()) == 1
    ana3 = json.loads((RUNS / gi.FITNESS_LEDGER).read_text())["ANA3"]
    assert ext[ana3["genome_object_id"]]["fitness_org"] == "ANA3"
    assert ext["84aa4d5c6124b60c12647489a36947677ee45ce6ea2ecb2bdec964206826b38d"]["img_taxon"] == "2510917020"
    assert any(p.name.startswith("annot_result_") for p in built.annotation_paths)


def test_the_external_id_builder_reads_the_ledger_field_names(tmp_path):
    runs = tmp_path / "runs"
    adp1 = runs / gi.ADP1_DIR
    adp1.mkdir(parents=True)
    (adp1 / "job_record.json").write_text(json.dumps({
        "genome_object_id": "a" * 64, "tax_id": "62977", "source": "NCBI GCF_000046845.1 fasta+gff (NCBI gene calls + IDs)"}))
    (adp1 / "build_genome_x.json").write_text(json.dumps({"genomes": {"c": {"genome_object_id": "a" * 64, "fasta_object_id": "f" * 64}}}))
    (runs / gi.FITNESS_LEDGER).parent.mkdir(parents=True)
    (runs / gi.FITNESS_LEDGER).write_text(json.dumps({
        "orgA": {"sref": "orgA", "genome_object_id": "b" * 64, "taxonomy": {"reference": "GCF_000000001.1_genomic.fna"}},
        "orgB": {"sref": "orgB"}}))
    (runs / gi.PMI_LEDGER).parent.mkdir(parents=True)
    (runs / gi.PMI_LEDGER).write_text(json.dumps({"2510917020": {"genome_id": "2510917020", "genome_object_id": "c" * 64}}))
    built = gi.build_external_ids(runs, build_results_dir=None)
    assert built.external_ids == {
        "a" * 64: {"ncbi_assembly": "GCF_000046845.1", "taxon_id": "62977", "fasta_object_id": "f" * 64, "gene_call_tool": "SOURCE_GFF"},
        "b" * 64: {"fitness_org": "orgA", "gene_call_tool": "SOURCE_GFF"},
        "c" * 64: {"img_taxon": "2510917020", "gene_call_tool": "SOURCE_GFF"},
    }
    assert any("orgB" in n for n in built.notes)


def test_several_external_ids_for_one_genome_become_several_aliases():
    ext = {**EXTERNAL[OBJECT_ID], "fitness_org": ["orgA", "orgA_delta"]}
    rows = build(external=ext)
    assert [a["alias_id"] for a in rows.alias_rows if a["alias_source"] == "fitness_org"] == ["orgA", "orgA_delta"]


def test_fetch_helper_verifies_ids_and_skips_cached_objects(tmp_path):
    calls = []

    class Client:
        def download_object(self, object_id, dest):
            calls.append(object_id)
            Path(dest).write_text("{}")

    got = gi.fetch_genome_objects([OBJECT_ID, OBJECT_ID], tmp_path, client=Client())
    assert got == {OBJECT_ID: tmp_path / f"{OBJECT_ID}.json"} and calls == [OBJECT_ID]
    gi.fetch_genome_objects([OBJECT_ID], tmp_path, client=Client())
    assert calls == [OBJECT_ID]
    with pytest.raises(gi.GenomeInputError):
        gi.fetch_genome_objects(["../../etc/passwd"], tmp_path, client=Client())


# ---------------------------------------------------------------------------
# load_kbdl_genomes against the in-memory lake
# ---------------------------------------------------------------------------


def _load(lake=None, *, ann_dir, **kw):
    cap = ClearinghouseCapability(capability=lake) if lake is not None else None
    return gl.load_kbdl_genomes(
        [OBJECT_ID],
        external_ids=EXTERNAL,
        term_function_map=FIX / "term_function_map.tsv",
        genome_objects={OBJECT_ID: genome()},
        archive_dirs=[ann_dir],
        capability=cap,
        **kw,
    )


@pytest.fixture
def ann_dir(tmp_path):
    return _write_result_dir(tmp_path)


def _write_result_dir(tmp_path):
    directory = tmp_path / "archive"
    _write_result(directory, "f072e65c6f784629852a922b3bad4d75", annotation())
    return directory


def test_dry_run_builds_and_reports_but_touches_no_lake(ann_dir):
    report = _load(None, ann_dir=ann_dir, dry_run=True)
    assert report.dry_run and not report.failed
    g = report.genomes[0]
    assert g.annotation_results_used == ["f072e65c6f784629852a922b3bad4d75.json.gz"] and g.annotation_source == "archive"
    assert report.rows_per_table["protein_x_function"] == 33
    assert report.rows_per_table["gene_x_genome"] == 3 and report.rows_per_table["method"] == 6
    assert report.written == {}
    assert report.rows_per_method["source_gff@unknown/kbdl-default"] == {"gene_x_genome": 3, "gene_x_protein": 3}
    assert json.dumps(report.to_dict())  # JSON-able


def test_a_real_load_writes_every_table_and_a_rerun_writes_zero_rows(ann_dir):
    lake = _MiniLake()
    first = _load(lake, ann_dir=ann_dir)
    assert not first.failed
    g = first.genomes[0]
    assert g.written["method"] == 6
    assert g.written["genome_entity"] == g.written["genome_content"] == 1
    assert g.written["gene_entity"] == g.written["gene_content"] == 3
    assert g.written["protein_entity"] == 3 and g.written["function_entity"] == g.written["function_content"] == 21
    assert g.written["genome_alias"] == 2
    assert (g.written["gene_x_genome"], g.written["gene_x_protein"], g.written["protein_x_function"]) == (3, 3, 33)
    assert len(lake.rows["protein_x_function"]) == 33
    assert {r["alias_source"] for r in lake.rows["genome_alias"]} == {"kbdl_object", "ncbi_assembly"}
    # every written row carries the writer's clock and one batch id per table write
    assert all(r["observed_at"] and r["ingest_batch_id"] for r in lake.rows["gene_x_genome"])
    # no relationship is written into a result payload
    assert not any(lake.rows.get(f"{t}_result") for t in schema.ENTITY_TYPES)

    snapshot = {t: len(rs) for t, rs in lake.rows.items()}
    loads = len(lake.load_calls)
    second = _load(lake, ann_dir=ann_dir)
    assert len(lake.load_calls) == loads, "a re-run must not call load() at all"
    assert {t: len(rs) for t, rs in lake.rows.items()} == snapshot
    assert sum(second.written.values()) == 0
    assert second.already_present["protein_x_function"] == 33 and second.already_present["method"] == 6


def test_a_load_holds_the_namespace_lock_and_a_held_lock_refuses_with_nothing_written(ann_dir, tmp_path):
    lake = _MiniLake()
    held = ci.NamespaceLock(f"{schema.TENANT}.{schema.NAMESPACE}")
    held.acquire(execution_id=ci.new_execution_id())
    try:
        with pytest.raises(Exception, match="(?i)lock|held|locked"):
            _load(lake, ann_dir=ann_dir)
    finally:
        held.release()
    assert lake.load_calls == []
    _load(lake, ann_dir=ann_dir)  # and the lock is free again after a real run
    _load(lake, ann_dir=ann_dir)


def test_a_real_run_off_pod_is_refused_before_any_write(ann_dir):
    lake = _MiniLake()
    lake.locus = lambda: "off_pod"
    with pytest.raises(BerdlLoadRefusedError):
        _load(lake, ann_dir=ann_dir)
    assert lake.load_calls == []
    assert _load(lake, ann_dir=ann_dir, dry_run=True).genomes  # dry_run is fine off-pod


def test_an_entity_another_wave_already_wrote_is_not_written_twice(ann_dir):
    lake = _MiniLake()
    # W1 (the ontology wave) already loaded one function entity + content
    shared = FN("dna binding")
    lake.rows["function_entity"] = [{"entity_hash": shared, "entity_type": "function", "standardizer_version": "2.0", "observed_at": "x", "ingest_batch_id": "W1"}]
    lake.rows["function_content"] = [{"entity_hash": shared, "definition": "DNA binding", "standardizer_version": "2.0", "observed_at": "x", "ingest_batch_id": "W1"}]
    report = _load(lake, ann_dir=ann_dir)
    assert report.genomes[0].written["function_entity"] == 20
    assert report.genomes[0].already_present["function_entity"] == 1
    assert [r["definition"] for r in lake.rows["function_content"] if r["entity_hash"] == shared] == ["DNA binding"]


def test_a_genome_with_no_result_loads_genes_and_reports_it(tmp_path):
    lake = _MiniLake()
    report = _load(lake, ann_dir=tmp_path / "empty")
    g = report.genomes[0]
    assert g.annotation_source == "none" and g.annotation_results_found == []
    assert any("no annotation result found" in n for n in g.notes)
    assert g.written["gene_x_genome"] == 3 and "protein_x_function" not in g.written and g.written["method"] == 1


def test_an_unreadable_genome_is_reported_not_raised(tmp_path):
    report = gl.load_kbdl_genomes(
        ["f" * 64],
        term_function_map=FIX / "term_function_map.tsv",
        object_dir=tmp_path / "none",
        archive_dirs=[tmp_path],
        dry_run=True,
    )
    assert report.genomes == [] and "not in" in report.failed["f" * 64]


def test_the_term_map_is_required():
    with pytest.raises(ValueError, match="term_function_map is required"):
        gl.load_kbdl_genomes([OBJECT_ID], genome_objects={OBJECT_ID: genome()}, dry_run=True)


def test_every_emitted_link_passes_register_links_validation_against_the_lake(ann_dir):
    # register_links also checks orphans, methods and committed conflicts; the load above
    # already passed it. Here: the built rows re-validate byte-identically.
    rows = build()
    for table, link_rows in rows.links.items():
        assert cl.validate_link_rows(table, link_rows)["rows"] == link_rows
    methods, _ = cl.validate_method_rows(rows.methods)
    assert methods == rows.methods


# ---------------------------------------------------------------------------
# package surface
# ---------------------------------------------------------------------------


def test_load_kbdl_genomes_is_exported_from_the_package():
    import kbutillib.domains.kbase.berdl as berdl

    assert berdl.load_kbdl_genomes is gl.load_kbdl_genomes
    assert "load_kbdl_genomes" in berdl.__all__ and "KBDL_ANNOTATOR_METHODS" in berdl.__all__
