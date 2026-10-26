"""Layer 1 of the gene-literature stack: annotate genes with papers.

:class:`GeneLiteratureUtils` takes a set of genes and an organism and returns
every paper any of three independent sources can attach to them, keyed by
canonical publication id:

* **paperblast** -- a local homology search against the PaperBLAST snapshot
  (:class:`~kbutillib.domains.literature.paperblast.PaperBLASTUtils`), which
  reaches papers about a *homolog* as well as curation-database entries;
* **europepmc** -- a literal alias+organism query against Europe PMC
  (:class:`~kbutillib.domains.literature.europepmc.EuropePMCClient`), which
  reaches papers that name the gene itself;
* **uniprot** -- the reference list UniProt curators attached to an accession
  (:meth:`~kbutillib.domains.external.kb_uniprot_utils.KBUniProtUtils.get_publications`),
  minus whole-genome sequencing and reannotation references.

Nothing here reads a genome object or an annotation aggregate: the input is a
plain list of genes, so the same call works for a KBase genome, a FASTA file
or five genes typed by hand.

Design notes
------------

**Evidence class.**  Every (paper, gene) pair carries one or more
:class:`Evidence` records saying *how* the link was found.  The class is
decided by this precedence, in order:

1. a curated PaperBLAST link is ``curated``;
2. a non-curated PaperBLAST link is ``direct`` only when the alignment is
   exact *and* complete on both sides (identity, query coverage and subject
   coverage all 1.0) *and* the homolog's organism is this organism;
3. otherwise a PaperBLAST link is ``homolog``;
4. a Europe PMC or UniProt link is ``direct``;
5. finally, and overriding all of the above, a paper linked to more than
   ``mention_threshold`` genes has *every* one of its links reclassified
   ``mention`` -- a review or a proteomics survey naming 400 genes is not
   evidence about any one of them.

**Failure is per gene per source.**  A source that raises for one gene records
``state="error"`` with the message in that gene's status and the call carries
on; it never aborts the annotation.  That matters because these three sources
fail independently and for unrelated reasons (no snapshot, HTTP 503, unknown
accession), and a run that dies on gene 3 of 4000 is worthless.

**Merging.**  The same paper arrives from different sources under different
ids -- PaperBLAST stores a bare PMID, Europe PMC a PMID and a PMCID and a
DOI, UniProt a PMID and a DOI.  Records are therefore unioned *transitively*
by :func:`~kbutillib.domains.literature.europepmc.merge_ids`: two records are
the same paper if they share any one of pmid/pmcid/doi, and a record keyed
only by DOI joins one keyed only by PMID when a third carries both.

Annotation note: pydantic evaluates field annotations at class-creation time
and this repository still builds on Python 3.9, so model fields use
``Optional[...]``/``Union[...]`` rather than PEP 604 unions.

Example::

    from kbutillib.domains.literature import GeneLiteratureUtils

    lit = GeneLiteratureUtils()
    result = lit.annotate_genes_with_papers(
        genes=[{"id": "ACIAD0380", "aliases": ["dnaA"], "sequence": "MKT..."}],
        organism={"name": "Acinetobacter baylyi", "synonyms": ["ADP1"]},
    )
    for paper_id, paper in result.papers.items():
        print(paper_id, paper.title, list(paper.genes))
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel

from ...core.shared_env_utils import SharedEnvUtils
from .europepmc import alias_query, canonical_paper_id, merge_ids

__all__ = [
    "DEFAULT_MENTION_THRESHOLD",
    "DEFAULT_SOURCES",
    "EVIDENCE_CLASSES",
    "MENTION_THRESHOLD_CONFIG_KEY",
    "SEQUENCING_SCOPE_MARKERS",
    "SOURCE_STATES",
    "UNIPROT_ACCESSION_RE",
    "Evidence",
    "Gene",
    "GeneEntry",
    "GeneLiteratureUtils",
    "GeneLiteratureUtilsImpl",
    "Organism",
    "PaperAnnotation",
    "PaperEntry",
    "PaperIds",
    "Provenance",
    "SourceStatus",
    "is_sequencing_scope",
    "uniprot_accessions",
]

#: Sources run when the caller names none, in the order they are run.
DEFAULT_SOURCES: Tuple[str, ...] = ("paperblast", "europepmc", "uniprot")

#: Every source name :meth:`GeneLiteratureUtils.annotate_genes_with_papers`
#: accepts.  An unrecognised name is a caller mistake, so it raises rather
#: than being silently skipped.
KNOWN_SOURCES: Tuple[str, ...] = DEFAULT_SOURCES

#: The evidence classes, strongest-claim first.
EVIDENCE_CLASSES: Tuple[str, ...] = ("curated", "direct", "homolog", "mention")

#: The per-gene per-source states.
SOURCE_STATES: Tuple[str, ...] = ("ok", "no_hits", "skipped", "error")

#: Config key holding the gene count above which a paper becomes a mention.
MENTION_THRESHOLD_CONFIG_KEY = "gene_literature.mention_threshold"

#: Used when config says nothing: a paper naming more than 50 of the genes
#: asked about is a survey, not evidence about any one of them.
DEFAULT_MENTION_THRESHOLD = 50

#: The official UniProt accession pattern
#: (https://www.uniprot.org/help/accession_numbers).  Any gene alias matching
#: it in full is treated as an accession as well as an alias.
UNIPROT_ACCESSION_RE = re.compile(
    r"[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}"
)

#: Reference-scope substrings that mark a UniProt reference as a whole-genome
#: sequencing or reannotation paper rather than a study of this protein.
#: Both strings were confirmed verbatim against live UniProt JSON on
#: 2026-10-20 (``Q6FF68`` reference 1 for the first, ``A0A021WW32``
#: reference 3 for the second).
SEQUENCING_SCOPE_MARKERS: Tuple[str, ...] = (
    "LARGE SCALE GENOMIC DNA",
    "GENOME REANNOTATION",
)

_WHITESPACE_RE = re.compile(r"\s+")
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")


# ── helpers ──────────────────────────────────────────────────────────────


def uniprot_accessions(gene: "Gene") -> List[str]:
    """UniProt accessions for a gene: its ``uniprot`` field plus any alias.

    The explicit ``uniprot`` field is trusted as given (UniProt also issues
    ids such as ``A0A021WW32`` that the regex does cover, and isoform ids such
    as ``P12345-2`` that it does not).  Aliases are only taken when they match
    :data:`UNIPROT_ACCESSION_RE` in full, because an ordinary gene symbol must
    not be sent to UniProt as an accession.

    Args:
        gene: The gene.

    Returns:
        Accessions in first-appearance order, de-duplicated.
    """
    found: List[str] = []
    candidates: List[Tuple[Optional[str], bool]] = [(gene.uniprot, True)]
    candidates.extend((alias, False) for alias in gene.aliases)
    for candidate, explicit in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if not text:
            continue
        if not explicit and not UNIPROT_ACCESSION_RE.fullmatch(text):
            continue
        if text not in found:
            found.append(text)
    return found


def is_sequencing_scope(scope: Optional[str]) -> bool:
    """True when one UniProt reference scope is a sequencing/reannotation one.

    Args:
        scope: One entry of a reference's ``referencePositions`` list, e.g.
            ``"NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]"``.

    Returns:
        True if the scope contains any of :data:`SEQUENCING_SCOPE_MARKERS`.
    """
    if not scope:
        return False
    upper = str(scope).upper()
    return any(marker in upper for marker in SEQUENCING_SCOPE_MARKERS)


def _organism_tokens(text: Optional[Any]) -> List[str]:
    """Lowercase word tokens of an organism string (``ADP1`` -> ``["adp1"]``)."""
    if text is None:
        return []
    lowered = _WHITESPACE_RE.sub(" ", str(text)).strip().lower()
    return [token for token in _NON_WORD_RE.split(lowered) if token]


def _is_contiguous_subsequence(needle: Sequence[str], haystack: Sequence[str]) -> bool:
    """True when ``needle``'s tokens appear consecutively in ``haystack``."""
    if not needle or len(needle) > len(haystack):
        return False
    for start in range(len(haystack) - len(needle) + 1):
        if list(haystack[start:start + len(needle)]) == list(needle):
            return True
    return False


def _as_year(value: Optional[Any]) -> Optional[int]:
    """Leading four digits of a year-ish value, as an int, else None.

    Mirrors what ``EuropePMCClient._parse_hit`` does, so that a Europe PMC
    ``pubYear``, a PaperBLAST ``year`` column and a UniProt
    ``publicationDate`` all land in the same field as the same type.
    """
    if value is None:
        return None
    digits = str(value).strip()[:4]
    return int(digits) if digits.isdigit() else None


def _text(value: Optional[Any]) -> Optional[str]:
    """Stripped string, or None when empty/absent."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_float(value: Optional[Any]) -> Optional[float]:
    """Float, or None when absent or unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_id(field: str, value: Optional[Any]) -> Optional[str]:
    """One pmid/pmcid/doi, kept only when it canonicalises.

    A malformed id is dropped here rather than carried into the merge, so
    that neither :func:`merge_ids` nor :func:`canonical_paper_id` can raise
    on data a source handed us -- one bad row in ``litsearch.db`` or one odd
    Europe PMC record must not abort an annotation.

    Args:
        field: ``"pmid"``, ``"pmcid"`` or ``"doi"``.
        value: The raw value from the source.

    Returns:
        The stripped value when :func:`canonical_paper_id` accepts it as
        ``field``, else None.
    """
    text = _text(value)
    if text is None:
        return None
    try:
        canonical_paper_id(**{field: text})
    except ValueError:
        return None
    return text


# ── input models ─────────────────────────────────────────────────────────


class Gene(BaseModel):
    """One gene to annotate.

    Attributes:
        id: The caller's own id for the gene; the key everything is reported
            under.
        aliases: Names to search the literature for (gene symbol, locus tag,
            synonyms).  An alias matching :data:`UNIPROT_ACCESSION_RE` is
            additionally used as a UniProt accession.
        sequence: Protein sequence.  Required for the ``paperblast`` source;
            without it that source reports ``skipped`` for this gene.
        uniprot: Explicit UniProt accession, when known.
    """

    id: str
    aliases: List[str] = []
    sequence: Optional[str] = None
    uniprot: Optional[str] = None


class Organism(BaseModel):
    """The organism the genes come from.

    Attributes:
        name: Scientific name, e.g. ``"Acinetobacter baylyi"``.
        synonyms: Strain names and other spellings, e.g. ``["ADP1"]``.  Used
            both to disambiguate a Europe PMC alias query and to recognise a
            PaperBLAST homolog as being this organism.
        taxon_id: NCBI taxon id, when known.
    """

    name: str
    synonyms: List[str] = []
    taxon_id: Optional[int] = None

    def matches(self, organism_text: Optional[Any]) -> bool:
        """True when ``organism_text`` names this organism.

        A homolog organism as PaperBLAST records it
        (``"Acinetobacter baylyi ADP1"``) matches both the name
        (``"Acinetobacter baylyi"``) and a strain synonym (``"ADP1"``);
        matching is on whole lowercase word tokens, so ``"bayl"`` does not
        match and ``"Acinetobacter baumannii"`` does not match
        ``"Acinetobacter baylyi"``.

        Args:
            organism_text: The organism string to test, e.g. a PaperBLAST
                ``homolog_organism``.

        Returns:
            True if the text contains this organism's taxon id, name or any
            synonym as a contiguous run of word tokens.
        """
        haystack = _organism_tokens(organism_text)
        if not haystack:
            return False
        terms: List[str] = [self.name] + list(self.synonyms)
        if self.taxon_id is not None:
            terms.append(str(self.taxon_id))
        for term in terms:
            needle = _organism_tokens(term)
            if needle and _is_contiguous_subsequence(needle, haystack):
                return True
        return False

    def search_terms(self) -> List[str]:
        """Name plus synonyms, for :func:`alias_query`."""
        terms: List[str] = []
        for term in [self.name] + list(self.synonyms):
            text = _text(term)
            if text and text not in terms:
                terms.append(text)
        return terms


# ── output models ────────────────────────────────────────────────────────


class Evidence(BaseModel):
    """How one source linked one paper to one gene.

    Attributes:
        source: ``"paperblast"``, ``"europepmc"`` or ``"uniprot"``.
        evidence_class: One of :data:`EVIDENCE_CLASSES`.
        query_term: What was actually searched or matched -- the gene alias
            for ``europepmc``, the accession for ``uniprot``, the PaperBLAST
            ``queryTerm`` when it recorded one.
        homolog_id: PaperBLAST id of the homolog carrying the link.  Present
            on every PaperBLAST link, curated ones included.
        homolog_organism: Organism of that homolog.
        identity: Alignment identity, 0-1.
        query_coverage: Fraction of the query covered by the alignment.
        subject_coverage: Fraction of the homolog covered by the alignment.
        evalue: Alignment e-value.
        snippet: Sentence from the paper that mentions the homolog, when
            PaperBLAST recorded one.
        curated_db: Curation database (``"SwissProt"``, ...) for a curated
            link, else None.
    """

    source: str
    evidence_class: str
    query_term: Optional[str] = None
    homolog_id: Optional[str] = None
    homolog_organism: Optional[str] = None
    identity: Optional[float] = None
    query_coverage: Optional[float] = None
    subject_coverage: Optional[float] = None
    evalue: Optional[float] = None
    snippet: Optional[str] = None
    curated_db: Optional[str] = None


class PaperIds(BaseModel):
    """Every publication id known for one paper, after merging."""

    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    doi: Optional[str] = None


class PaperEntry(BaseModel):
    """One paper and the genes it was linked to.

    Attributes:
        ids: The paper's pmid/pmcid/doi.
        title: Title, from whichever source supplied one first.
        year: Publication year.
        journal: Journal title.
        genes: ``{gene_id: [Evidence, ...]}`` -- every gene this paper was
            linked to, and how.
    """

    ids: PaperIds = PaperIds()
    title: Optional[str] = None
    year: Optional[int] = None
    journal: Optional[str] = None
    genes: Dict[str, List[Evidence]] = {}


class SourceStatus(BaseModel):
    """What one source did for one gene.

    Attributes:
        state: ``"ok"`` (links found), ``"no_hits"`` (the source ran and
            found nothing), ``"skipped"`` (the source had nothing to run on
            for this gene -- no sequence, no searchable alias, no accession)
            or ``"error"`` (the source raised).
        message: The error message when ``state == "error"``, else None.
        n_hits: Number of evidence links this source contributed for this
            gene.
    """

    state: str
    message: Optional[str] = None
    n_hits: int = 0


class GeneEntry(BaseModel):
    """The inverse view: what happened for one gene.

    Attributes:
        status: ``{source: SourceStatus}`` for every requested source.
        paper_ids: Canonical ids of every paper linked to this gene, in
            first-appearance order.
    """

    status: Dict[str, SourceStatus] = {}
    paper_ids: List[str] = []


class Provenance(BaseModel):
    """What was run, against what, when.

    Attributes:
        sources: The sources that were run, in order.
        paperblast_snapshot: The local PaperBLAST ``snapshot.json`` manifest
            when one was present, else None.
        retrieved_at: UTC ISO-8601 timestamp of the call.
        params: The call's parameters.
    """

    sources: List[str] = []
    paperblast_snapshot: Optional[Dict[str, Any]] = None
    retrieved_at: Optional[str] = None
    params: Dict[str, Any] = {}


class PaperAnnotation(BaseModel):
    """Papers for genes, both ways round, plus per-gene per-source status.

    ``papers`` is keyed by canonical publication id
    (:func:`~kbutillib.domains.literature.europepmc.canonical_paper_id`);
    ``genes`` is the inverse view and is kept consistent with ``papers`` --
    every id in ``genes[g].paper_ids`` is a key of ``papers`` whose
    ``genes`` contains ``g``, and vice versa.
    """

    papers: Dict[str, PaperEntry] = {}
    genes: Dict[str, GeneEntry] = {}
    provenance: Provenance = Provenance()

    def for_gene(self, gene_id: str) -> "PaperAnnotation":
        """Return this annotation restricted to one gene.

        Args:
            gene_id: The gene to keep.

        Returns:
            A new :class:`PaperAnnotation` holding only that gene's entry and
            only the papers linked to it, each carrying only that gene's
            evidence.  Provenance is carried over unchanged.  An unknown
            ``gene_id`` yields an annotation with empty ``papers`` and
            ``genes``.
        """
        gene_entry = self.genes.get(gene_id)
        if gene_entry is None:
            return PaperAnnotation(provenance=self.provenance.model_copy(deep=True))

        papers: Dict[str, PaperEntry] = {}
        for paper_id in gene_entry.paper_ids:
            paper = self.papers.get(paper_id)
            if paper is None:
                continue
            evidence = paper.genes.get(gene_id)
            if not evidence:
                continue
            restricted = paper.model_copy(deep=True)
            restricted.genes = {gene_id: [item.model_copy() for item in evidence]}
            papers[paper_id] = restricted

        entry = gene_entry.model_copy(deep=True)
        entry.paper_ids = [pid for pid in gene_entry.paper_ids if pid in papers]
        return PaperAnnotation(
            papers=papers,
            genes={gene_id: entry},
            provenance=self.provenance.model_copy(deep=True),
        )

    def to_json(self, indent: Optional[int] = None) -> str:
        """Serialise to a JSON string.

        Args:
            indent: Passed through to pydantic; None for the compact form.

        Returns:
            The JSON document.
        """
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, text: Union[str, bytes]) -> "PaperAnnotation":
        """Rebuild an annotation from :meth:`to_json` output.

        Args:
            text: JSON produced by :meth:`to_json`.

        Returns:
            The annotation.
        """
        return cls.model_validate_json(text)


# ── the utility ──────────────────────────────────────────────────────────


class GeneLiteratureUtils(SharedEnvUtils):
    """Annotate genes with the papers that describe them.

    Args:
        paperblast: Object exposing ``search`` and ``papers_for_hits``.
            Defaults to a lazily created
            :class:`~kbutillib.domains.literature.paperblast.PaperBLASTUtils`.
        europepmc: Object exposing ``search(query)``.  Defaults to a lazily
            created
            :class:`~kbutillib.domains.literature.europepmc.EuropePMCClient`.
        uniprot: Object exposing ``get_publications(accession)``.  Defaults
            to a lazily created
            :class:`~kbutillib.domains.external.kb_uniprot_utils.KBUniProtUtils`.
        clock: Callable returning the ``retrieved_at`` timestamp.  Defaults
            to ``datetime.now(timezone.utc)``.
        **kwargs: Passed to :class:`SharedEnvUtils` (``config``,
            ``config_file``, ``token_file``, ...).

    Config:
        ``gene_literature.mention_threshold``: gene count above which a paper
        becomes a ``mention`` (default 50).  The collaborators read their own
        config (``paperblast.data_dir``, ``gene_literature.cache_path``,
        ``gene_literature.europepmc_rps``).

    Note:
        Every collaborator is built lazily, so a ``sources=("europepmc",)``
        call never touches the PaperBLAST snapshot and a
        ``sources=("paperblast",)`` call makes no HTTP request.
    """

    def __init__(
        self,
        paperblast: Optional[Any] = None,
        europepmc: Optional[Any] = None,
        uniprot: Optional[Any] = None,
        clock: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._paperblast = paperblast
        self._europepmc = europepmc
        self._uniprot = uniprot
        self._clock = clock

    # ── lazily built collaborators ────────────────────────────────────

    def _delegate_kwargs(self) -> Dict[str, Any]:
        """Constructor kwargs that carry this object's config to a delegate."""
        return {
            "config": self.export_environment().get("config") or {},
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }

    @property
    def paperblast(self) -> Any:
        """The local PaperBLAST wrapper, built on first use when not injected."""
        if self._paperblast is None:
            from .paperblast import PaperBLASTUtils

            self._paperblast = PaperBLASTUtils(**self._delegate_kwargs())
        return self._paperblast

    @property
    def europepmc(self) -> Any:
        """The Europe PMC client, built on first use when not injected."""
        if self._europepmc is None:
            from .europepmc import EuropePMCClient

            self._europepmc = EuropePMCClient(**self._delegate_kwargs())
        return self._europepmc

    @property
    def uniprot(self) -> Any:
        """The UniProt client, built on first use when not injected."""
        if self._uniprot is None:
            from ..external.kb_uniprot_utils import KBUniProtUtils

            self._uniprot = KBUniProtUtils(**self._delegate_kwargs())
        return self._uniprot

    # ── public API ────────────────────────────────────────────────────

    def annotate_genes_with_papers(
        self,
        genes: Iterable[Union[Gene, Dict[str, Any]]],
        organism: Union[Organism, Dict[str, Any]],
        sources: Sequence[str] = DEFAULT_SOURCES,
        mention_threshold: Optional[int] = None,
        include_sequencing_refs: bool = False,
        paperblast_params: Optional[Dict[str, Any]] = None,
    ) -> PaperAnnotation:
        """Collect every paper the requested sources can attach to these genes.

        Args:
            genes: :class:`Gene` instances or dicts with the same keys.
            organism: :class:`Organism` or a dict with the same keys.
            sources: Which sources to run, in order.  Any subset of
                :data:`DEFAULT_SOURCES`.
            mention_threshold: Gene count above which a paper's links are all
                reclassified ``mention``.  Defaults to
                ``gene_literature.mention_threshold``, else
                :data:`DEFAULT_MENTION_THRESHOLD`.
            include_sequencing_refs: Keep UniProt references whose scope is
                entirely whole-genome sequencing or reannotation.  Default
                False drops them.
            paperblast_params: Extra keyword arguments for
                ``PaperBLASTUtils.search`` (``evalue``, ``max_hits``,
                ``min_identity``, ``min_query_coverage``, ``data_dir``,
                ``threads``, ...).

        Returns:
            A :class:`PaperAnnotation`.

        Raises:
            ValueError: If two input genes share an id, or ``sources`` names
                a source that does not exist.  Both are checked before any
                search runs.

        Note:
            A source that raises for a gene is recorded in that gene's status
            as ``state="error"``; it never aborts the call.
        """
        gene_list = [
            entry if isinstance(entry, Gene) else Gene(**dict(entry))
            for entry in (genes or [])
        ]
        # Before any search: a duplicate id would silently merge two genes'
        # evidence into one entry, which is worse than refusing to run.
        seen: set = set()
        duplicates: List[str] = []
        for gene in gene_list:
            if gene.id in seen and gene.id not in duplicates:
                duplicates.append(gene.id)
            seen.add(gene.id)
        if duplicates:
            raise ValueError(
                "Duplicate gene ids in input: " + ", ".join(sorted(duplicates))
            )

        requested = [str(name) for name in (sources or ())]
        unknown = [name for name in requested if name not in KNOWN_SOURCES]
        if unknown:
            raise ValueError(
                "Unknown literature source(s): {}; known sources are {}".format(
                    ", ".join(unknown), ", ".join(KNOWN_SOURCES)
                )
            )

        target = organism if isinstance(organism, Organism) else Organism(
            **dict(organism)
        )
        threshold = self._resolve_mention_threshold(mention_threshold)

        statuses: Dict[str, Dict[str, SourceStatus]] = {
            gene.id: {} for gene in gene_list
        }
        # (gene_id, source, link) in discovery order; link is a plain dict
        # carrying the paper ids/metadata plus the Evidence fields.
        links: List[Tuple[str, str, Dict[str, Any]]] = []

        for source in requested:
            if source == "paperblast":
                links.extend(
                    self._run_paperblast(
                        gene_list, target, statuses, paperblast_params or {}
                    )
                )
            elif source == "europepmc":
                links.extend(self._run_europepmc(gene_list, target, statuses))
            elif source == "uniprot":
                links.extend(
                    self._run_uniprot(gene_list, statuses, include_sequencing_refs)
                )

        annotation = self._assemble(gene_list, links, statuses, threshold)
        annotation.provenance = Provenance(
            sources=requested,
            paperblast_snapshot=(
                self._paperblast_snapshot(paperblast_params or {})
                if "paperblast" in requested
                else None
            ),
            retrieved_at=self._now(),
            params={
                "mention_threshold": threshold,
                "include_sequencing_refs": bool(include_sequencing_refs),
                "paperblast_params": dict(paperblast_params or {}),
                "organism": target.model_dump(),
                "n_genes": len(gene_list),
            },
        )
        return annotation

    # ── sources ───────────────────────────────────────────────────────

    def _run_paperblast(
        self,
        genes: List[Gene],
        organism: Organism,
        statuses: Dict[str, Dict[str, SourceStatus]],
        params: Dict[str, Any],
    ) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Search every sequenced gene against the snapshot and join papers.

        ``search`` and ``papers_for_hits`` are both batch calls, so a failure
        in either is attributed to every gene in the batch -- the whole batch
        is what failed.  Genes with no sequence are ``skipped`` and are never
        in the batch.
        """
        found: List[Tuple[str, str, Dict[str, Any]]] = []
        queries: Dict[str, str] = {}
        for gene in genes:
            sequence = _text(gene.sequence)
            if sequence is None:
                statuses[gene.id]["paperblast"] = SourceStatus(
                    state="skipped", message="no protein sequence"
                )
            else:
                queries[gene.id] = sequence

        if not queries:
            return found

        try:
            join_kwargs = {
                key: params[key]
                for key in ("data_dir", "allow_dropbox")
                if key in params
            }
            hits = self.paperblast.search(queries, **params)
            papers = self.paperblast.papers_for_hits(hits, **join_kwargs)
        except Exception as error:  # noqa: BLE001 - reported, never raised
            message = f"{type(error).__name__}: {error}"
            self.log_error(f"PaperBLAST failed for {len(queries)} gene(s): {message}")
            for gene_id in queries:
                statuses[gene_id]["paperblast"] = SourceStatus(
                    state="error", message=message
                )
            return found

        for gene_id in queries:
            kept = 0
            for link in papers.get(gene_id) or []:
                record = self._paperblast_link(link, organism)
                if record is None:
                    continue
                found.append((gene_id, "paperblast", record))
                kept += 1
            statuses[gene_id]["paperblast"] = SourceStatus(
                state="ok" if kept else "no_hits", n_hits=kept
            )
        return found

    def _paperblast_link(
        self, link: Dict[str, Any], organism: Organism
    ) -> Optional[Dict[str, Any]]:
        """Turn one ``papers_for_hits`` link into a merge record.

        Returns None when the link carries no publication id at all, which is
        the only way a link can be unusable (it could not be keyed).
        """
        pmid = _valid_id("pmid", link.get("pmid"))
        pmcid = _valid_id("pmcid", link.get("pmcid"))
        doi = _valid_id("doi", link.get("doi"))
        if not (pmid or pmcid or doi):
            return None

        identity = _as_float(link.get("identity"))
        query_coverage = _as_float(link.get("query_coverage"))
        subject_coverage = _as_float(link.get("subject_coverage"))
        curated_db = _text(link.get("curated_db"))
        homolog_organism = _text(link.get("homolog_organism"))

        if curated_db:
            evidence_class = "curated"
        elif (
            identity == 1.0
            and query_coverage == 1.0
            and subject_coverage == 1.0
            and organism.matches(homolog_organism)
        ):
            # An exact, complete alignment to a protein of this very organism
            # is the gene itself under another id, not a homolog.
            evidence_class = "direct"
        else:
            evidence_class = "homolog"

        return {
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "title": _text(link.get("title")),
            "journal": _text(link.get("journal")),
            "year": _as_year(link.get("year")),
            "evidence": Evidence(
                source="paperblast",
                evidence_class=evidence_class,
                # papers_for_hits does not publish the GenePaper queryTerm
                # (it only uses it internally to pick the snippet), so a
                # PaperBLAST link has no query term of its own.
                query_term=None,
                # Kept on every PaperBLAST link, curated ones included: the
                # homolog and its alignment are the whole basis of the claim.
                homolog_id=_text(link.get("homolog_id")),
                homolog_organism=homolog_organism,
                identity=identity,
                query_coverage=query_coverage,
                subject_coverage=subject_coverage,
                evalue=_as_float(link.get("evalue")),
                snippet=_text(link.get("snippet")),
                curated_db=curated_db,
            ),
        }

    def _run_europepmc(
        self,
        genes: List[Gene],
        organism: Organism,
        statuses: Dict[str, Dict[str, SourceStatus]],
    ) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Query Europe PMC for each alias of each gene."""
        found: List[Tuple[str, str, Dict[str, Any]]] = []
        organism_terms = organism.search_terms()

        for gene in genes:
            kept = 0
            attempted = 0
            message: Optional[str] = None
            for alias in gene.aliases:
                query = alias_query(alias, organism_terms)
                if not query:
                    # alias_query refuses an alias under three characters,
                    # because such a query would return the whole corpus.
                    self.log_debug(
                        f"Skipping unsearchable alias {alias!r} for {gene.id}"
                    )
                    continue
                attempted += 1
                try:
                    hits = self.europepmc.search(query)
                except Exception as error:  # noqa: BLE001 - reported, never raised
                    text = f"{type(error).__name__}: {error}"
                    self.log_error(
                        f"Europe PMC failed for {gene.id} alias {alias!r}: {text}"
                    )
                    if message is None:
                        message = text
                    continue
                for hit in hits or []:
                    record = self._europepmc_link(hit, alias)
                    if record is None:
                        continue
                    found.append((gene.id, "europepmc", record))
                    kept += 1

            statuses[gene.id]["europepmc"] = SourceStatus(
                state=(
                    "error"
                    if message is not None
                    else "skipped"
                    if attempted == 0
                    else "ok"
                    if kept
                    else "no_hits"
                ),
                message=message,
                n_hits=kept,
            )
        return found

    @staticmethod
    def _europepmc_link(hit: Dict[str, Any], alias: str) -> Optional[Dict[str, Any]]:
        """Turn one Europe PMC hit into a merge record."""
        pmid = _valid_id("pmid", hit.get("pmid"))
        pmcid = _valid_id("pmcid", hit.get("pmcid"))
        doi = _valid_id("doi", hit.get("doi"))
        if not (pmid or pmcid or doi):
            return None
        return {
            "pmid": pmid,
            "pmcid": pmcid,
            "doi": doi,
            "title": _text(hit.get("title")),
            "journal": _text(hit.get("journal")),
            "year": _as_year(hit.get("year")),
            "evidence": Evidence(
                source="europepmc",
                # The paper names the alias and the organism; that is a claim
                # about this gene, not about a homolog.
                evidence_class="direct",
                query_term=_text(alias),
            ),
        }

    def _run_uniprot(
        self,
        genes: List[Gene],
        statuses: Dict[str, Dict[str, SourceStatus]],
        include_sequencing_refs: bool,
    ) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Read each gene's UniProt reference list, minus sequencing refs."""
        found: List[Tuple[str, str, Dict[str, Any]]] = []

        for gene in genes:
            accessions = uniprot_accessions(gene)
            kept = 0
            message: Optional[str] = None
            for accession in accessions:
                try:
                    publications = self.uniprot.get_publications(accession)
                except Exception as error:  # noqa: BLE001 - reported, never raised
                    text = f"{type(error).__name__}: {error}"
                    self.log_error(
                        f"UniProt failed for {gene.id} accession {accession}: {text}"
                    )
                    if message is None:
                        message = text
                    continue
                for publication in publications or []:
                    if not include_sequencing_refs and self._is_sequencing_reference(
                        publication
                    ):
                        self.log_debug(
                            f"Dropping sequencing reference for {accession}: "
                            f"{publication.get('scope')}"
                        )
                        continue
                    record = self._uniprot_link(publication, accession)
                    if record is None:
                        continue
                    found.append((gene.id, "uniprot", record))
                    kept += 1

            statuses[gene.id]["uniprot"] = SourceStatus(
                state=(
                    "error"
                    if message is not None
                    else "skipped"
                    if not accessions
                    else "ok"
                    if kept
                    else "no_hits"
                ),
                message=message,
                n_hits=kept,
            )
        return found

    @staticmethod
    def _is_sequencing_reference(publication: Dict[str, Any]) -> bool:
        """True when EVERY scope on a UniProt reference is a sequencing one.

        A reference with no scope recorded, or whose scopes are all empty, is
        never filtered -- unknown is not the same as sequencing.  A reference
        cited for ``["NUCLEOTIDE SEQUENCE [LARGE SCALE GENOMIC DNA]",
        "FUNCTION"]`` is kept too: it says something about the protein.
        """
        scopes = [
            text for text in (publication.get("scope") or []) if _text(text) is not None
        ]
        if not scopes:
            return False
        return all(is_sequencing_scope(scope) for scope in scopes)

    @staticmethod
    def _uniprot_link(
        publication: Dict[str, Any], accession: str
    ) -> Optional[Dict[str, Any]]:
        """Turn one UniProt publication dict into a merge record."""
        pmid = _valid_id("pmid", publication.get("pubmed_id"))
        doi = _valid_id("doi", publication.get("doi"))
        if not (pmid or doi):
            return None
        return {
            "pmid": pmid,
            "pmcid": None,
            "doi": doi,
            "title": _text(publication.get("title")),
            "journal": _text(publication.get("journal")),
            "year": _as_year(publication.get("year")),
            "evidence": Evidence(
                source="uniprot",
                # A curator attached this paper to this accession.
                evidence_class="direct",
                query_term=_text(accession),
            ),
        }

    # ── assembly ──────────────────────────────────────────────────────

    def _assemble(
        self,
        genes: List[Gene],
        links: List[Tuple[str, str, Dict[str, Any]]],
        statuses: Dict[str, Dict[str, SourceStatus]],
        threshold: int,
    ) -> PaperAnnotation:
        """Merge links into papers, build the inverse view, apply mentions."""
        merged = merge_ids([link for _, _, link in links])

        papers: Dict[str, PaperEntry] = {}
        gene_entries: Dict[str, GeneEntry] = {
            gene.id: GeneEntry(status=statuses.get(gene.id) or {})
            for gene in genes
        }

        for gene_id, _source, link in links:
            token = canonical_paper_id(
                pmid=link.get("pmid"),
                pmcid=link.get("pmcid"),
                doi=link.get("doi"),
            )
            # Re-key through the transitive union: a link whose own primary
            # token is a DOI lands on the PMID-keyed paper when some other
            # link proved they are the same paper.
            canonical = merged.aliases.get(token, token)
            record = merged.merged.get(canonical) or {}

            paper = papers.get(canonical)
            if paper is None:
                paper = PaperEntry(
                    ids=PaperIds(
                        pmid=_text(record.get("pmid")),
                        pmcid=_text(record.get("pmcid")),
                        doi=_text(record.get("doi")),
                    ),
                    title=_text(record.get("title")),
                    journal=_text(record.get("journal")),
                    year=_as_year(record.get("year")),
                )
                papers[canonical] = paper

            paper.genes.setdefault(gene_id, []).append(link["evidence"])
            entry = gene_entries.get(gene_id)
            if entry is not None and canonical not in entry.paper_ids:
                entry.paper_ids.append(canonical)

        # Last, and overriding every class decided above: a paper linked to
        # more genes than the threshold is a survey or a review, so none of
        # its links is evidence about any single gene.
        for canonical, paper in papers.items():
            if len(paper.genes) > threshold:
                self.log_info(
                    f"Reclassifying {len(paper.genes)} links on {canonical} as "
                    f"mentions (threshold {threshold})"
                )
                for evidence_list in paper.genes.values():
                    for evidence in evidence_list:
                        evidence.evidence_class = "mention"

        return PaperAnnotation(papers=papers, genes=gene_entries)

    # ── misc ──────────────────────────────────────────────────────────

    def _resolve_mention_threshold(self, override: Optional[int]) -> int:
        """The mention threshold: argument, else config, else the default."""
        if override is not None:
            return int(override)
        configured = self.get_config_value(
            MENTION_THRESHOLD_CONFIG_KEY, DEFAULT_MENTION_THRESHOLD
        )
        try:
            return int(configured)
        except (TypeError, ValueError):
            self.log_warning(
                f"Ignoring non-integer {MENTION_THRESHOLD_CONFIG_KEY}="
                f"{configured!r}; using {DEFAULT_MENTION_THRESHOLD}"
            )
            return DEFAULT_MENTION_THRESHOLD

    def _paperblast_snapshot(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """The local snapshot manifest, or None when there is not one.

        Never raises: provenance is a record of the run, and failing to read
        it must not fail an annotation that already succeeded.
        """
        try:
            return self.paperblast.load_snapshot(
                data_dir=params.get("data_dir"),
                required=False,
                allow_dropbox=params.get("allow_dropbox"),
            )
        except Exception as error:  # noqa: BLE001 - provenance is best-effort
            self.log_warning(
                f"Could not read the PaperBLAST snapshot manifest: {error}"
            )
            return None

    def _now(self) -> str:
        """UTC ISO-8601 timestamp for provenance."""
        if self._clock is not None:
            return str(self._clock())
        return datetime.now(timezone.utc).isoformat()


# ── Composition-based implementation ─────────────────────────────────────


class GeneLiteratureUtilsImpl:
    """Composition-based version of :class:`GeneLiteratureUtils`.

    Holds ``env: SharedEnvUtils`` instead of inheriting, and delegates all
    method calls to an internal instance.
    """

    def __init__(self, env, **kwargs):
        self._env = env
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        # Carry the environment's configuration across, so the delegate reads
        # the same gene_literature.* / paperblast.* settings the env was
        # built with.
        try:
            _kwargs["config"] = env.export_environment().get("config") or {}
        except Exception:
            pass
        _kwargs.update(kwargs)
        self._delegate = GeneLiteratureUtils(**_kwargs)

    @property
    def env(self):
        return self._env

    def __getattr__(self, name):
        # Delegate all attribute access to the utility instance.
        return getattr(self._delegate, name)
