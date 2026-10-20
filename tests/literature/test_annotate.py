"""Offline tests for layer 1: annotating genes with papers.

Every test here runs without network, without a subprocess and without a
PaperBLAST snapshot: the three sources are injected as fakes at exactly the
methods :class:`GeneLiteratureUtils` calls on them --
``search``/``papers_for_hits`` for PaperBLAST, ``search`` for Europe PMC and
``get_publications`` for UniProt.

:class:`TestNoInjectionPaperBLASTPath` is the deliberate exception.  It
injects nothing: a real :class:`PaperBLASTUtils` built from config, a real
``MMSeqsUtils`` and the real ``mmseqs`` binary, over the tiny fixture
snapshot ``test_paperblast.py`` builds.  Everything else here can pass while
the one seam that matters -- the shape of the hits actually coming back from
mmseqs, and the links the real join makes from them -- is wrong.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from kbutillib.domains.literature.annotate import (
    DEFAULT_MENTION_THRESHOLD,
    DEFAULT_SOURCES,
    SEQUENCING_SCOPE_MARKERS,
    Evidence,
    Gene,
    GeneLiteratureUtils,
    GeneLiteratureUtilsImpl,
    Organism,
    PaperAnnotation,
    is_sequencing_scope,
    uniprot_accessions,
)

from .test_paperblast import (
    CURATED_ID,
    CURATED_SEQ,
    REP_SEQ,
    REPRESENTATIVE_ID,
    make_snapshot,
)

ORGANISM = {
    "name": "Acinetobacter baylyi",
    "synonyms": ["ADP1"],
    "taxon_id": 62977,
}

#: Five genes, each exercising a different combination of what a source has
#: to work with: a sequence, a searchable alias, a UniProt accession.
GENES = [
    # Everything: sequence, two aliases (one of which is an accession) and an
    # explicit accession.
    {
        "id": "g1",
        "aliases": ["dnaA", "P12345"],
        "sequence": "MKTAYIAKQRQ",
        "uniprot": "Q6FF68",
    },
    # Sequence and alias, no accession.
    {"id": "g2", "aliases": ["gyrB"], "sequence": "MVLSPADKTNV"},
    # No sequence: paperblast must report 'skipped'.
    {"id": "g3", "aliases": ["recA"], "uniprot": "P0A7G6"},
    # Only an unsearchably short alias and no accession: europepmc and
    # uniprot must both report 'skipped'.
    {"id": "g4", "aliases": ["ab"], "sequence": "MWQRNIRHSQQ"},
    # Nothing a source can use at all.
    {"id": "g5", "aliases": []},
]


# ── test doubles ─────────────────────────────────────────────────────────


class FakePaperBLAST:
    """Stands in for PaperBLASTUtils at the three methods annotate calls."""

    def __init__(self, links=None, fail_search=None, fail_join=None, snapshot=None):
        #: ``{gene_id: [link, ...]}`` as papers_for_hits would return.
        self.links = dict(links or {})
        self.fail_search = fail_search
        self.fail_join = fail_join
        self.snapshot = snapshot
        self.search_calls = []
        self.join_calls = []

    def search(self, queries, **kwargs):
        self.search_calls.append({"queries": dict(queries), "kwargs": dict(kwargs)})
        if self.fail_search is not None:
            raise self.fail_search
        return {gene_id: [{"subject_id": "repA"}] for gene_id in queries}

    def papers_for_hits(self, hits, **kwargs):
        self.join_calls.append({"hits": dict(hits), "kwargs": dict(kwargs)})
        if self.fail_join is not None:
            raise self.fail_join
        return {gene_id: list(self.links.get(gene_id) or []) for gene_id in hits}

    def load_snapshot(self, data_dir=None, required=True, allow_dropbox=None):
        return self.snapshot


class FakeEuropePMC:
    """Stands in for EuropePMCClient at ``search``."""

    def __init__(self, hits=None, fail_on=()):
        #: ``{alias: [hit, ...]}`` -- keyed by alias, so a test does not have
        #: to spell out the query string alias_query builds.
        self.hits = dict(hits or {})
        self.fail_on = set(fail_on)
        self.queries = []

    def search(self, query, **kwargs):
        self.queries.append(query)
        for alias in self.fail_on:
            if '"{}"'.format(alias) in query:
                raise RuntimeError(f"Europe PMC said 503 for {alias}")
        for alias, hits in self.hits.items():
            if '"{}"'.format(alias) in query:
                return list(hits)
        return []


class FakeUniProt:
    """Stands in for KBUniProtUtils at ``get_publications``."""

    def __init__(self, publications=None, fail_on=()):
        self.publications = dict(publications or {})
        self.fail_on = set(fail_on)
        self.accessions = []

    def get_publications(self, accession):
        self.accessions.append(accession)
        if accession in self.fail_on:
            raise ValueError(f"UniProt entry not found: {accession}")
        return list(self.publications.get(accession) or [])


# ── helpers ──────────────────────────────────────────────────────────────


def pb_link(
    pmid=None,
    pmcid=None,
    doi=None,
    title=None,
    journal=None,
    year=None,
    homolog_id=REPRESENTATIVE_ID,
    homolog_organism="Acinetobacter baylyi ADP1",
    identity=1.0,
    query_coverage=1.0,
    subject_coverage=1.0,
    evalue=1e-40,
    snippet=None,
    curated_db=None,
):
    """One link in the shape papers_for_hits returns (every LINK_KEYS key)."""
    return {
        "pmid": pmid,
        "pmcid": pmcid,
        "doi": doi,
        "title": title,
        "journal": journal,
        "year": year,
        "homolog_id": homolog_id,
        "homolog_organism": homolog_organism,
        "identity": identity,
        "query_coverage": query_coverage,
        "subject_coverage": subject_coverage,
        "evalue": evalue,
        "snippet": snippet,
        "curated_db": curated_db,
        "source": "paperblast",
    }


def epmc_hit(pmid=None, pmcid=None, doi=None, title=None, journal=None, year=None):
    """One hit in the shape EuropePMCClient.search returns."""
    return {
        "pmid": pmid,
        "pmcid": pmcid,
        "doi": doi,
        "title": title,
        "journal": journal,
        "year": year,
        "abstract": None,
        "source": "MED",
    }


def uniprot_pub(
    pubmed_id=None, doi=None, title=None, journal=None, year=None, scope=()
):
    """One publication in the shape KBUniProtUtils.get_publications returns."""
    return {
        "title": title or "",
        "journal": journal or "",
        "pubmed_id": pubmed_id,
        "doi": doi,
        "year": year or "",
        "scope": list(scope),
    }


def make_utils(paperblast=None, europepmc=None, uniprot=None, config=None, **kwargs):
    """A GeneLiteratureUtils with all three sources faked and no config file."""
    return GeneLiteratureUtils(
        paperblast=paperblast if paperblast is not None else FakePaperBLAST(),
        europepmc=europepmc if europepmc is not None else FakeEuropePMC(),
        uniprot=uniprot if uniprot is not None else FakeUniProt(),
        config=config or {},
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def classes_for(annotation, paper_id, gene_id):
    """The evidence classes recorded for one (paper, gene) pair."""
    return [e.evidence_class for e in annotation.papers[paper_id].genes[gene_id]]


def sources_for(annotation, paper_id, gene_id):
    """The sources recorded for one (paper, gene) pair."""
    return [e.source for e in annotation.papers[paper_id].genes[gene_id]]


def assert_views_agree(annotation):
    """The papers view and the genes view must describe the same links."""
    for paper_id, paper in annotation.papers.items():
        assert paper.genes, f"{paper_id} is in papers with no gene linked"
        for gene_id in paper.genes:
            entry = annotation.genes[gene_id]
            assert paper_id in entry.paper_ids, (
                f"{paper_id} lists {gene_id} but {gene_id} does not list it"
            )
    for gene_id, entry in annotation.genes.items():
        assert len(entry.paper_ids) == len(set(entry.paper_ids))
        for paper_id in entry.paper_ids:
            assert paper_id in annotation.papers, (
                f"{gene_id} lists unknown paper {paper_id}"
            )
            assert gene_id in annotation.papers[paper_id].genes


# ── input handling ───────────────────────────────────────────────────────


class TestInputs:
    def test_dicts_and_models_are_both_accepted(self):
        utils = make_utils()
        from_dicts = utils.annotate_genes_with_papers(GENES, ORGANISM)
        from_models = utils.annotate_genes_with_papers(
            [Gene(**gene) for gene in GENES], Organism(**ORGANISM)
        )
        assert list(from_dicts.genes) == list(from_models.genes) == [
            "g1",
            "g2",
            "g3",
            "g4",
            "g5",
        ]

    def test_duplicate_gene_ids_raise_before_any_search(self):
        paperblast = FakePaperBLAST()
        europepmc = FakeEuropePMC()
        uniprot = FakeUniProt()
        utils = make_utils(paperblast, europepmc, uniprot)

        with pytest.raises(ValueError, match="Duplicate gene ids in input: g1"):
            utils.annotate_genes_with_papers(
                [
                    {"id": "g1", "sequence": "MKT", "aliases": ["dnaA"]},
                    {"id": "g2", "sequence": "MVL"},
                    {"id": "g1", "sequence": "MWQ", "aliases": ["dnaA"]},
                ],
                ORGANISM,
            )

        # "before any search" is the point of the check, so assert it.
        assert paperblast.search_calls == []
        assert europepmc.queries == []
        assert uniprot.accessions == []

    def test_an_unknown_source_raises(self):
        utils = make_utils()
        with pytest.raises(ValueError, match="Unknown literature source"):
            utils.annotate_genes_with_papers(GENES, ORGANISM, sources=("pubmed",))

    def test_only_the_requested_sources_run(self):
        paperblast = FakePaperBLAST()
        europepmc = FakeEuropePMC()
        uniprot = FakeUniProt()
        utils = make_utils(paperblast, europepmc, uniprot)

        annotation = utils.annotate_genes_with_papers(
            GENES, ORGANISM, sources=("europepmc",)
        )

        assert paperblast.search_calls == []
        assert uniprot.accessions == []
        assert europepmc.queries
        assert list(annotation.genes["g1"].status) == ["europepmc"]
        assert annotation.provenance.sources == ["europepmc"]

    def test_uniprot_accessions_come_from_the_field_and_matching_aliases(self):
        gene = Gene(
            id="g1",
            aliases=["dnaA", "P12345", "A0A021WW32", "notanacc"],
            uniprot="Q6FF68",
        )
        # dnaA and notanacc are not accessions; the other three are, and the
        # explicit field comes first.
        assert uniprot_accessions(gene) == ["Q6FF68", "P12345", "A0A021WW32"]
        assert uniprot_accessions(Gene(id="g2", aliases=["dnaA"])) == []


# ── merging ──────────────────────────────────────────────────────────────


class TestIdMerge:
    def test_one_paper_from_three_sources_merges_transitively(self):
        """PaperBLAST knows only a PMID, UniProt only a DOI, Europe PMC both.

        Nothing but the transitive union puts these three on one key: the
        PaperBLAST record and the UniProt record share no id with each other
        at all.
        """
        paperblast = FakePaperBLAST(
            links={"g1": [pb_link(pmid="1001", title="From PaperBLAST")]}
        )
        europepmc = FakeEuropePMC(
            hits={
                "dnaA": [
                    epmc_hit(
                        pmid="1001",
                        pmcid="PMC1001",
                        doi="10.1/one",
                        title="From Europe PMC",
                        journal="Journal One",
                        year=2020,
                    )
                ]
            }
        )
        uniprot = FakeUniProt(
            publications={
                "Q6FF68": [uniprot_pub(doi="10.1/one", scope=["FUNCTION"])]
            }
        )
        utils = make_utils(paperblast, europepmc, uniprot)

        annotation = utils.annotate_genes_with_papers([GENES[0]], ORGANISM)

        assert list(annotation.papers) == ["PMID:1001"]
        paper = annotation.papers["PMID:1001"]
        assert paper.ids.pmid == "1001"
        assert paper.ids.pmcid == "PMC1001"
        assert paper.ids.doi == "10.1/one"
        assert paper.journal == "Journal One"
        assert paper.year == 2020
        assert sources_for(annotation, "PMID:1001", "g1") == [
            "paperblast",
            "europepmc",
            "uniprot",
        ]
        assert annotation.genes["g1"].paper_ids == ["PMID:1001"]
        assert_views_agree(annotation)

    def test_a_doi_only_link_is_rekeyed_onto_the_pmid_key(self):
        """The inverse view must follow the re-keying, not the raw ids."""
        paperblast = FakePaperBLAST(
            links={
                "g1": [pb_link(pmid="1001", pmcid="PMC1001")],
                # g2 reaches the same paper by DOI alone, via a different
                # homolog; it must land on PMID:1001, not on DOI:10.1/one.
                "g2": [pb_link(doi="10.1/one", homolog_id="other")],
            }
        )
        europepmc = FakeEuropePMC(
            hits={"dnaA": [epmc_hit(pmcid="PMC1001", doi="10.1/one")]}
        )
        utils = make_utils(paperblast, europepmc)

        annotation = utils.annotate_genes_with_papers(
            GENES[:2], ORGANISM, sources=("paperblast", "europepmc")
        )

        assert list(annotation.papers) == ["PMID:1001"]
        assert annotation.genes["g1"].paper_ids == ["PMID:1001"]
        assert annotation.genes["g2"].paper_ids == ["PMID:1001"]
        assert set(annotation.papers["PMID:1001"].genes) == {"g1", "g2"}
        assert_views_agree(annotation)

    def test_distinct_papers_stay_distinct(self):
        paperblast = FakePaperBLAST(
            links={
                "g1": [
                    pb_link(pmid="1001"),
                    pb_link(pmcid="PMC2002", homolog_id="dupA"),
                    pb_link(doi="10.1/THREE", homolog_id="other"),
                ]
            }
        )
        utils = make_utils(paperblast)

        annotation = utils.annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("paperblast",)
        )

        # DOIs are canonicalised lowercase by canonical_paper_id.
        assert sorted(annotation.papers) == [
            "DOI:10.1/three",
            "PMC:PMC2002",
            "PMID:1001",
        ]
        assert annotation.genes["g1"].status["paperblast"].n_hits == 3
        assert_views_agree(annotation)

    def test_a_link_with_no_usable_id_is_dropped_not_fatal(self):
        paperblast = FakePaperBLAST(
            links={
                "g1": [
                    pb_link(),  # no ids at all
                    pb_link(pmid="not-a-pmid"),  # malformed
                    pb_link(pmid="1001"),
                ]
            }
        )
        utils = make_utils(paperblast)

        annotation = utils.annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("paperblast",)
        )

        assert list(annotation.papers) == ["PMID:1001"]
        assert annotation.genes["g1"].status["paperblast"].n_hits == 1


# ── evidence classes ─────────────────────────────────────────────────────


class TestEvidenceClass:
    def test_a_curated_link_is_curated_and_keeps_its_homolog_metrics(self):
        paperblast = FakePaperBLAST(
            links={
                "g1": [
                    pb_link(
                        pmid="4004",
                        curated_db="SwissProt",
                        homolog_id=CURATED_ID,
                        homolog_organism="Bacillus subtilis",
                        identity=0.42,
                        query_coverage=0.61,
                        subject_coverage=0.55,
                        evalue=1e-9,
                    )
                ]
            }
        )
        utils = make_utils(paperblast)

        annotation = utils.annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("paperblast",)
        )

        evidence = annotation.papers["PMID:4004"].genes["g1"][0]
        assert evidence.evidence_class == "curated"
        assert evidence.curated_db == "SwissProt"
        # Curated included: the homolog and its alignment are the basis of
        # the claim, so they are never dropped.
        assert evidence.homolog_id == CURATED_ID
        assert evidence.homolog_organism == "Bacillus subtilis"
        assert evidence.identity == 0.42
        assert evidence.query_coverage == 0.61
        assert evidence.subject_coverage == 0.55
        assert evidence.evalue == 1e-9

    def test_a_curated_link_stays_curated_even_on_an_exact_self_alignment(self):
        """Curation outranks the exact-alignment rule, per the precedence."""
        paperblast = FakePaperBLAST(
            links={
                "g1": [
                    pb_link(
                        pmid="4004",
                        curated_db="SwissProt",
                        homolog_organism="Acinetobacter baylyi ADP1",
                        identity=1.0,
                        query_coverage=1.0,
                        subject_coverage=1.0,
                    )
                ]
            }
        )
        annotation = make_utils(paperblast).annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("paperblast",)
        )
        assert classes_for(annotation, "PMID:4004", "g1") == ["curated"]

    @pytest.mark.parametrize(
        ("identity", "query_coverage", "subject_coverage", "organism", "expected"),
        [
            # Exact, complete both ways, and this organism -> the gene itself.
            (1.0, 1.0, 1.0, "Acinetobacter baylyi ADP1", "direct"),
            # Strain synonym alone is enough.
            (1.0, 1.0, 1.0, "Acinetobacter sp. ADP1", "direct"),
            # The taxon id, when the organism string carries it.
            (1.0, 1.0, 1.0, "taxon 62977", "direct"),
            # Right organism, but the subject is only half covered.
            (1.0, 1.0, 0.5, "Acinetobacter baylyi ADP1", "homolog"),
            # Right organism, query not fully covered.
            (1.0, 0.8, 1.0, "Acinetobacter baylyi ADP1", "homolog"),
            # Right organism, not identical.
            (0.99, 1.0, 1.0, "Acinetobacter baylyi ADP1", "homolog"),
            # Perfect alignment, but to a different organism.
            (1.0, 1.0, 1.0, "Escherichia coli K-12", "homolog"),
            # A near-name must not match on a partial word.
            (1.0, 1.0, 1.0, "Acinetobacter baumannii AB5075", "homolog"),
            # Subject coverage unknown is not subject coverage 1.0.
            (1.0, 1.0, None, "Acinetobacter baylyi ADP1", "homolog"),
        ],
    )
    def test_non_curated_links_follow_the_exactness_rule(
        self, identity, query_coverage, subject_coverage, organism, expected
    ):
        paperblast = FakePaperBLAST(
            links={
                "g1": [
                    pb_link(
                        pmid="1001",
                        identity=identity,
                        query_coverage=query_coverage,
                        subject_coverage=subject_coverage,
                        homolog_organism=organism,
                    )
                ]
            }
        )
        annotation = make_utils(paperblast).annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("paperblast",)
        )
        assert classes_for(annotation, "PMID:1001", "g1") == [expected]

    def test_europepmc_and_uniprot_links_are_direct_and_carry_the_query_term(self):
        europepmc = FakeEuropePMC(hits={"dnaA": [epmc_hit(pmid="1001")]})
        uniprot = FakeUniProt(
            publications={"Q6FF68": [uniprot_pub(pubmed_id="2002", scope=["FUNCTION"])]}
        )
        utils = make_utils(europepmc=europepmc, uniprot=uniprot)

        annotation = utils.annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("europepmc", "uniprot")
        )

        epmc = annotation.papers["PMID:1001"].genes["g1"][0]
        assert (epmc.source, epmc.evidence_class, epmc.query_term) == (
            "europepmc",
            "direct",
            "dnaA",
        )
        up = annotation.papers["PMID:2002"].genes["g1"][0]
        assert (up.source, up.evidence_class, up.query_term) == (
            "uniprot",
            "direct",
            "Q6FF68",
        )

    def test_the_europepmc_query_conjoins_the_alias_and_the_organism(self):
        europepmc = FakeEuropePMC()
        utils = make_utils(europepmc=europepmc)
        utils.annotate_genes_with_papers([GENES[0]], ORGANISM, sources=("europepmc",))
        # alias_query decides the shape: an ordinary symbol is conjoined with
        # the organism terms, while P12345 looks like a locus tag to it
        # (letters then digits) and is searched as a bare exact phrase.
        assert europepmc.queries == [
            '"dnaA" AND ("Acinetobacter baylyi" OR "ADP1")',
            '"P12345"',
        ]


# ── per-source status ────────────────────────────────────────────────────


class TestStatuses:
    def test_every_requested_source_reports_a_state_for_every_gene(self):
        utils = make_utils()
        annotation = utils.annotate_genes_with_papers(GENES, ORGANISM)
        for gene_id in ("g1", "g2", "g3", "g4", "g5"):
            assert set(annotation.genes[gene_id].status) == set(DEFAULT_SOURCES)

    def test_a_gene_with_no_sequence_is_skipped_by_paperblast(self):
        paperblast = FakePaperBLAST(links={"g1": [pb_link(pmid="1001")]})
        utils = make_utils(paperblast)

        annotation = utils.annotate_genes_with_papers(GENES, ORGANISM)

        skipped = annotation.genes["g3"].status["paperblast"]
        assert skipped.state == "skipped"
        assert skipped.message == "no protein sequence"
        assert skipped.n_hits == 0
        # g3 and g5 have no sequence, so they are not in the batch at all.
        assert sorted(paperblast.search_calls[0]["queries"]) == ["g1", "g2", "g4"]

    def test_skipped_is_distinguished_from_no_hits(self):
        """g4's only alias is too short to search; g2's alias searched and missed."""
        europepmc = FakeEuropePMC(hits={"dnaA": [epmc_hit(pmid="1001")]})
        uniprot = FakeUniProt()
        utils = make_utils(europepmc=europepmc, uniprot=uniprot)

        annotation = utils.annotate_genes_with_papers(
            GENES, ORGANISM, sources=("europepmc", "uniprot")
        )

        assert annotation.genes["g1"].status["europepmc"].state == "ok"
        assert annotation.genes["g1"].status["europepmc"].n_hits == 1
        # Searched, found nothing.
        assert annotation.genes["g2"].status["europepmc"].state == "no_hits"
        # Nothing searchable at all: "ab" is under alias_query's minimum.
        assert annotation.genes["g4"].status["europepmc"].state == "skipped"
        assert annotation.genes["g5"].status["europepmc"].state == "skipped"
        # No accession anywhere -> nothing to ask UniProt.
        assert annotation.genes["g2"].status["uniprot"].state == "skipped"
        # An accession that UniProt simply has no references for.
        assert annotation.genes["g3"].status["uniprot"].state == "no_hits"

    def test_a_source_that_raises_for_a_gene_never_aborts_the_call(self):
        europepmc = FakeEuropePMC(
            hits={"dnaA": [epmc_hit(pmid="1001")], "gyrB": [epmc_hit(pmid="2002")]},
            fail_on={"dnaA"},
        )
        uniprot = FakeUniProt(
            publications={
                "P0A7G6": [uniprot_pub(pubmed_id="3003", scope=["FUNCTION"])]
            },
            fail_on={"Q6FF68"},
        )
        utils = make_utils(europepmc=europepmc, uniprot=uniprot)

        annotation = utils.annotate_genes_with_papers(
            GENES, ORGANISM, sources=("europepmc", "uniprot")
        )

        g1_epmc = annotation.genes["g1"].status["europepmc"]
        assert g1_epmc.state == "error"
        assert "Europe PMC said 503 for dnaA" in g1_epmc.message
        assert "RuntimeError" in g1_epmc.message

        g1_uniprot = annotation.genes["g1"].status["uniprot"]
        assert g1_uniprot.state == "error"
        assert "UniProt entry not found: Q6FF68" in g1_uniprot.message

        # The rest of the call completed: g2 and g3 still have their papers.
        assert annotation.genes["g2"].status["europepmc"].state == "ok"
        assert annotation.genes["g2"].paper_ids == ["PMID:2002"]
        assert annotation.genes["g3"].paper_ids == ["PMID:3003"]
        assert_views_agree(annotation)

    def test_a_gene_keeps_the_links_that_succeeded_alongside_an_error(self):
        """P12345 answers, Q6FF68 raises: state is error, n_hits is still 1."""
        uniprot = FakeUniProt(
            publications={
                "P12345": [uniprot_pub(pubmed_id="5005", scope=["FUNCTION"])]
            },
            fail_on={"Q6FF68"},
        )
        utils = make_utils(uniprot=uniprot)

        annotation = utils.annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("uniprot",)
        )

        status = annotation.genes["g1"].status["uniprot"]
        assert status.state == "error"
        assert status.n_hits == 1
        assert annotation.genes["g1"].paper_ids == ["PMID:5005"]

    def test_a_failing_paperblast_batch_errors_every_gene_in_the_batch_only(self):
        paperblast = FakePaperBLAST(fail_search=RuntimeError("no snapshot at /x"))
        europepmc = FakeEuropePMC(hits={"dnaA": [epmc_hit(pmid="1001")]})
        utils = make_utils(paperblast, europepmc)

        annotation = utils.annotate_genes_with_papers(
            GENES, ORGANISM, sources=("paperblast", "europepmc")
        )

        for gene_id in ("g1", "g2", "g4"):
            status = annotation.genes[gene_id].status["paperblast"]
            assert status.state == "error"
            assert "no snapshot at /x" in status.message
        # Sequenceless genes were never in the batch, so they are skipped,
        # not errored.
        assert annotation.genes["g3"].status["paperblast"].state == "skipped"
        # Europe PMC ran regardless.
        assert annotation.genes["g1"].paper_ids == ["PMID:1001"]

    def test_a_failing_paperblast_join_is_reported_the_same_way(self):
        paperblast = FakePaperBLAST(fail_join=RuntimeError("litsearch.db is missing"))
        utils = make_utils(paperblast)

        annotation = utils.annotate_genes_with_papers(
            GENES, ORGANISM, sources=("paperblast",)
        )

        status = annotation.genes["g1"].status["paperblast"]
        assert status.state == "error"
        assert "litsearch.db is missing" in status.message
        assert annotation.papers == {}


# ── mention reclassification ─────────────────────────────────────────────


class TestMentionReclassification:
    @staticmethod
    def _annotation(threshold=None, config=None):
        """One paper on three genes, one paper on two, over five genes."""
        wide = [pb_link(pmid="7007", title="A survey of everything")]
        narrow = [pb_link(pmid="1001", title="A study of one gene")]
        paperblast = FakePaperBLAST(
            links={
                "g1": wide + narrow,
                "g2": wide + narrow,
                "g4": wide,
            }
        )
        utils = make_utils(paperblast, config=config)
        return utils.annotate_genes_with_papers(
            GENES, ORGANISM, sources=("paperblast",), mention_threshold=threshold
        )

    def test_above_the_threshold_every_link_becomes_a_mention(self):
        annotation = self._annotation(threshold=2)

        # 3 genes > 2: all three links reclassified, in both views.
        assert set(annotation.papers["PMID:7007"].genes) == {"g1", "g2", "g4"}
        for gene_id in ("g1", "g2", "g4"):
            assert classes_for(annotation, "PMID:7007", gene_id) == ["mention"]
        # 2 genes is not > 2: untouched.
        for gene_id in ("g1", "g2"):
            assert classes_for(annotation, "PMID:1001", gene_id) == ["direct"]

    def test_exactly_at_the_threshold_is_not_a_mention(self):
        annotation = self._annotation(threshold=3)
        for gene_id in ("g1", "g2", "g4"):
            assert classes_for(annotation, "PMID:7007", gene_id) == ["direct"]

    def test_the_override_is_applied_last_over_curated_and_homolog(self):
        wide_curated = pb_link(pmid="7007", curated_db="SwissProt")
        wide_homolog = pb_link(
            pmid="7007", homolog_organism="Escherichia coli K-12", identity=0.4
        )
        paperblast = FakePaperBLAST(
            links={"g1": [wide_curated], "g2": [wide_homolog], "g4": [wide_homolog]}
        )
        utils = make_utils(paperblast)

        annotation = utils.annotate_genes_with_papers(
            GENES, ORGANISM, sources=("paperblast",), mention_threshold=2
        )

        for gene_id in ("g1", "g2", "g4"):
            assert classes_for(annotation, "PMID:7007", gene_id) == ["mention"]
        # The metrics behind the (now overridden) class are still there.
        assert annotation.papers["PMID:7007"].genes["g1"][0].curated_db == "SwissProt"

    def test_the_threshold_defaults_to_config_then_to_fifty(self):
        utils = make_utils()
        assert utils._resolve_mention_threshold(None) == DEFAULT_MENTION_THRESHOLD

        configured = make_utils(config={"gene_literature": {"mention_threshold": 2}})
        assert configured._resolve_mention_threshold(None) == 2

        annotation = self._annotation(
            config={"gene_literature": {"mention_threshold": 2}}
        )
        assert classes_for(annotation, "PMID:7007", "g1") == ["mention"]
        assert annotation.provenance.params["mention_threshold"] == 2

    def test_a_non_integer_configured_threshold_falls_back_to_the_default(self):
        utils = make_utils(config={"gene_literature": {"mention_threshold": "lots"}})
        assert utils._resolve_mention_threshold(None) == DEFAULT_MENTION_THRESHOLD


# ── UniProt scope filtering ──────────────────────────────────────────────


class TestUniProtScopeFiltering:
    def test_the_marker_strings_are_the_ones_uniprot_actually_uses(self):
        # Confirmed against live UniProt JSON on 2026-10-20: Q6FF68 ref 1 and
        # A0A021WW32 ref 3 carry exactly these.
        assert SEQUENCING_SCOPE_MARKERS == (
            "LARGE SCALE GENOMIC DNA",
            "GENOME REANNOTATION",
        )
        assert is_sequencing_scope("NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]")
        assert is_sequencing_scope("GENOME REANNOTATION")
        assert not is_sequencing_scope("NUCLEOTIDE SEQUENCE [GENOMIC DNA]")
        assert not is_sequencing_scope("FUNCTION")
        assert not is_sequencing_scope(None)
        assert not is_sequencing_scope("")

    @staticmethod
    def _utils(**kwargs):
        uniprot = FakeUniProt(
            publications={
                "Q6FF68": [
                    uniprot_pub(
                        pubmed_id="15514110",
                        scope=["NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]"],
                    ),
                    uniprot_pub(pubmed_id="2002", scope=["GENOME REANNOTATION"]),
                    uniprot_pub(
                        pubmed_id="3003",
                        scope=[
                            "NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]",
                            "FUNCTION",
                        ],
                    ),
                    uniprot_pub(pubmed_id="4004", scope=["FUNCTION"]),
                    uniprot_pub(pubmed_id="5005", scope=[]),
                    uniprot_pub(pubmed_id="6006"),  # no scope key at all
                ]
            }
        )
        return make_utils(uniprot=uniprot, **kwargs)

    def test_references_whose_scopes_are_all_sequencing_are_dropped(self):
        annotation = self._utils().annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("uniprot",)
        )

        kept = sorted(annotation.papers)
        # 15514110 and 2002 are gone; 3003 survives because FUNCTION is also
        # a scope; 5005 and 6006 survive because unknown is not sequencing.
        assert kept == ["PMID:3003", "PMID:4004", "PMID:5005", "PMID:6006"]
        assert annotation.genes["g1"].status["uniprot"].n_hits == 4

    def test_include_sequencing_refs_keeps_them(self):
        annotation = self._utils().annotate_genes_with_papers(
            [GENES[0]],
            ORGANISM,
            sources=("uniprot",),
            include_sequencing_refs=True,
        )

        assert sorted(annotation.papers) == [
            "PMID:15514110",
            "PMID:2002",
            "PMID:3003",
            "PMID:4004",
            "PMID:5005",
            "PMID:6006",
        ]
        assert annotation.provenance.params["include_sequencing_refs"] is True

    def test_a_dropped_reference_does_not_count_as_a_hit(self):
        uniprot = FakeUniProt(
            publications={
                "Q6FF68": [
                    uniprot_pub(
                        pubmed_id="15514110",
                        scope=["NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]"],
                    )
                ]
            }
        )
        annotation = make_utils(uniprot=uniprot).annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("uniprot",)
        )
        # The source ran and returned a reference, but nothing survived.
        assert annotation.genes["g1"].status["uniprot"].state == "no_hits"
        assert annotation.papers == {}


# ── views and serialisation ──────────────────────────────────────────────


class TestForGene:
    @staticmethod
    def _annotation():
        paperblast = FakePaperBLAST(
            links={
                "g1": [pb_link(pmid="1001"), pb_link(pmid="7007")],
                "g2": [pb_link(pmid="7007")],
            }
        )
        europepmc = FakeEuropePMC(hits={"dnaA": [epmc_hit(pmid="9009")]})
        return make_utils(paperblast, europepmc).annotate_genes_with_papers(
            GENES, ORGANISM, sources=("paperblast", "europepmc")
        )

    def test_for_gene_keeps_only_that_gene_and_its_papers(self):
        restricted = self._annotation().for_gene("g1")

        assert list(restricted.genes) == ["g1"]
        assert sorted(restricted.papers) == ["PMID:1001", "PMID:7007", "PMID:9009"]
        # The shared paper keeps only g1's evidence.
        assert list(restricted.papers["PMID:7007"].genes) == ["g1"]
        assert restricted.genes["g1"].paper_ids == [
            "PMID:1001",
            "PMID:7007",
            "PMID:9009",
        ]
        assert_views_agree(restricted)

    def test_for_gene_does_not_mutate_the_original(self):
        annotation = self._annotation()
        annotation.for_gene("g1").papers["PMID:7007"].genes["g1"][0].evidence_class = (
            "mention"
        )
        assert classes_for(annotation, "PMID:7007", "g2") == ["direct"]
        assert set(annotation.papers["PMID:7007"].genes) == {"g1", "g2"}

    def test_for_gene_carries_provenance_and_the_status_block(self):
        restricted = self._annotation().for_gene("g2")
        assert restricted.provenance.sources == ["paperblast", "europepmc"]
        assert set(restricted.genes["g2"].status) == {"paperblast", "europepmc"}

    def test_for_gene_on_a_gene_with_no_papers_keeps_its_status(self):
        restricted = self._annotation().for_gene("g5")
        assert restricted.papers == {}
        assert restricted.genes["g5"].paper_ids == []
        assert restricted.genes["g5"].status["paperblast"].state == "skipped"

    def test_for_gene_on_an_unknown_gene_is_empty(self):
        restricted = self._annotation().for_gene("nope")
        assert restricted.papers == {}
        assert restricted.genes == {}


class TestSerialisation:
    def test_json_round_trip_preserves_everything(self):
        paperblast = FakePaperBLAST(
            links={
                "g1": [
                    pb_link(
                        pmid="1001",
                        pmcid="PMC1001",
                        doi="10.1/one",
                        title="A study",
                        journal="Journal One",
                        year="2020",
                        snippet="the dnaA homolog was essential",
                        identity=0.87,
                        query_coverage=0.93,
                        subject_coverage=0.71,
                        evalue=2.5e-30,
                    ),
                    pb_link(pmid="4004", curated_db="SwissProt"),
                ],
                "g2": [pb_link(pmid="1001")],
            },
            snapshot={"stats": {"nSeq": 3}, "downloaded_at": "2026-08-26T00:00:00Z"},
        )
        europepmc = FakeEuropePMC(hits={"dnaA": [epmc_hit(pmid="9009", year=1999)]})
        uniprot = FakeUniProt(
            publications={"Q6FF68": [uniprot_pub(pubmed_id="5005", scope=["FUNCTION"])]}
        )
        utils = make_utils(paperblast, europepmc, uniprot)
        original = utils.annotate_genes_with_papers(
            GENES, ORGANISM, mention_threshold=4
        )

        text = original.to_json()
        # Plain JSON, not a pydantic-only encoding.
        assert isinstance(json.loads(text), dict)
        restored = PaperAnnotation.from_json(text)

        assert restored == original
        assert restored.model_dump() == original.model_dump()
        evidence = restored.papers["PMID:1001"].genes["g1"][0]
        assert isinstance(evidence, Evidence)
        assert evidence.identity == 0.87
        assert evidence.subject_coverage == 0.71
        assert evidence.snippet == "the dnaA homolog was essential"
        assert restored.papers["PMID:1001"].year == 2020
        assert restored.provenance.paperblast_snapshot == {
            "stats": {"nSeq": 3},
            "downloaded_at": "2026-08-26T00:00:00Z",
        }

    def test_to_json_accepts_an_indent(self):
        annotation = make_utils().annotate_genes_with_papers(GENES, ORGANISM)
        assert "\n" in annotation.to_json(indent=2)

    def test_provenance_records_the_run(self):
        paperblast = FakePaperBLAST(snapshot={"stats": {"nSeq": 3}})
        utils = make_utils(paperblast, clock=lambda: "2026-10-20T00:00:00+00:00")

        annotation = utils.annotate_genes_with_papers(
            GENES,
            ORGANISM,
            paperblast_params={"min_identity": 0.4},
            mention_threshold=7,
        )

        provenance = annotation.provenance
        assert provenance.sources == list(DEFAULT_SOURCES)
        assert provenance.retrieved_at == "2026-10-20T00:00:00+00:00"
        assert provenance.paperblast_snapshot == {"stats": {"nSeq": 3}}
        assert provenance.params["paperblast_params"] == {"min_identity": 0.4}
        assert provenance.params["mention_threshold"] == 7
        assert provenance.params["n_genes"] == 5
        assert provenance.params["organism"]["name"] == "Acinetobacter baylyi"
        # The search parameters really were forwarded.
        assert paperblast.search_calls[0]["kwargs"] == {"min_identity": 0.4}

    def test_no_snapshot_is_recorded_when_paperblast_was_not_run(self):
        annotation = make_utils().annotate_genes_with_papers(
            GENES, ORGANISM, sources=("europepmc",)
        )
        assert annotation.provenance.paperblast_snapshot is None


class TestComposition:
    def test_the_impl_wrapper_delegates_and_carries_config(self):
        env = GeneLiteratureUtils(
            config={"gene_literature": {"mention_threshold": 3}},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        impl = GeneLiteratureUtilsImpl(env)

        assert impl.env is env
        assert impl._resolve_mention_threshold(None) == 3
        # Fakes can still be pushed in through the delegate.
        impl._delegate._paperblast = FakePaperBLAST(
            links={"g1": [pb_link(pmid="1001")]}
        )
        annotation = impl.annotate_genes_with_papers(
            [GENES[0]], ORGANISM, sources=("paperblast",)
        )
        assert list(annotation.papers) == ["PMID:1001"]


# ── the one test that injects nothing ────────────────────────────────────


def _mmseqs_installed():
    """True when an mmseqs binary is on PATH and answers ``version``."""
    if shutil.which("mmseqs") is None:
        return False
    try:
        return (
            subprocess.run(
                ["mmseqs", "version"], capture_output=True, timeout=60
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _mmseqs_installed(), reason="no mmseqs executable found")
class TestNoInjectionPaperBLASTPath:
    """annotate_genes_with_papers with NOTHING injected.

    No fake source and no faked search output: the PaperBLAST wrapper and the
    MMseqs wrapper are both built lazily from config, so this runs a real
    ``mmseqs createdb / search / convertalis`` over the fixture snapshot and
    feeds the real hits through the real ``litsearch.db`` join into the
    annotation.  A fake search output cannot catch a misread coverage column
    or an evidence class decided on the wrong number; this can.
    """

    def test_real_mmseqs_hits_become_keyed_papers_with_homolog_metrics(self, tmp_path):
        data_dir = make_snapshot(tmp_path / "pb")
        utils = GeneLiteratureUtils(
            config={
                "paperblast": {"data_dir": str(data_dir)},
                "gene_literature": {"cache_path": str(tmp_path / "cache.sqlite")},
            },
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )

        annotation = utils.annotate_genes_with_papers(
            genes=[
                {"id": "gene1", "sequence": REP_SEQ},
                {"id": "gene2", "sequence": CURATED_SEQ},
            ],
            organism=ORGANISM,
            sources=("paperblast",),
            paperblast_params={
                "min_identity": 0.9,
                "min_query_coverage": 0.9,
                "max_hits": 10,
            },
        )

        assert annotation.genes["gene1"].status["paperblast"].state == "ok", (
            "the real mmseqs path found no papers for an identical sequence"
        )

        # Keyed by canonical paper id, from the bare PMIDs litsearch.db holds.
        assert "PMID:1001" in annotation.papers
        # Reached only through SeqToDuplicate expansion on the real path.
        assert "PMID:2002" in annotation.papers
        assert annotation.genes["gene1"].paper_ids == sorted(
            annotation.genes["gene1"].paper_ids,
            key=annotation.genes["gene1"].paper_ids.index,
        )

        evidence = annotation.papers["PMID:1001"].genes["gene1"][0]
        assert evidence.source == "paperblast"
        assert evidence.homolog_id == REPRESENTATIVE_ID
        assert evidence.homolog_organism == "Acinetobacter baylyi ADP1"
        # The numbers really came back from convertalis, as fractions.
        assert evidence.identity is not None and 0.9 <= evidence.identity <= 1.0
        assert (
            evidence.query_coverage is not None
            and 0.9 <= evidence.query_coverage <= 1.0
        )
        assert (
            evidence.subject_coverage is not None
            and 0.0 < evidence.subject_coverage <= 1.0
        )
        assert evidence.evalue is not None
        # The class follows those real numbers, not an assumption about them:
        # an exact, complete alignment to this organism is 'direct', anything
        # short of exact is 'homolog'.
        exact = (
            evidence.identity == 1.0
            and evidence.query_coverage == 1.0
            and evidence.subject_coverage == 1.0
        )
        assert evidence.evidence_class == ("direct" if exact else "homolog")

        # The duplicate's paper came from an E. coli homolog, so it is a
        # homolog link whatever the alignment numbers are.
        duplicate = annotation.papers["PMID:2002"].genes["gene1"][0]
        assert duplicate.evidence_class == "homolog"
        assert duplicate.homolog_organism == "Escherichia coli K-12"
        assert duplicate.identity is not None
        assert duplicate.query_coverage is not None
        assert duplicate.subject_coverage is not None

        # gene2 hits the curated entry, so its link is curated -- and still
        # carries the alignment it was found by.
        curated = annotation.papers["PMID:4004"].genes["gene2"][0]
        assert curated.evidence_class == "curated"
        assert curated.curated_db == "SwissProt"
        assert curated.homolog_id == CURATED_ID
        assert curated.identity is not None
        assert curated.query_coverage is not None
        assert curated.subject_coverage is not None

        # Provenance picked up the real manifest, and the result serialises.
        assert annotation.provenance.paperblast_snapshot is not None
        assert annotation.provenance.paperblast_snapshot["stats"]["nSeq"] == 3
        assert PaperAnnotation.from_json(annotation.to_json()) == annotation
        assert_views_agree(annotation)
