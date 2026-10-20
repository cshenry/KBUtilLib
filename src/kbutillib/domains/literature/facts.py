"""Layer 2 of the gene-literature stack: typed, cited facts about one gene.

Layer 1 (:mod:`~kbutillib.domains.literature.annotate`) answers *which papers
talk about this gene*.  This module answers *what do they say*, as a list of
typed statements each of which carries at least one citation that has been
checked against a trusted set of publication ids.

Two modes, and the difference matters:

* **context** -- the papers are fetched here (title + abstract, through
  :class:`~kbutillib.domains.literature.europepmc.EuropePMCClient`) and put
  in the prompt.  Every citation the model returns must be one of the ids we
  supplied; anything else is dropped.  This is the only mode available on an
  API type that cannot search the web, and it is the only mode in which the
  trusted set is closed.
* **web** -- the API type runs the call with live web search
  (``params["web_search"] = True``), so the model may cite publications we
  never saw.  Those citations are verified by *resolving* them against
  Europe PMC; an id that does not resolve is dropped.

Which mode a call runs in is decided by ``web_search`` and by
:func:`kbutillib.domains.ai.llm.supports_web_search`, which is free and
synchronous -- so ``web_search="required"`` against an API type that cannot
search raises :class:`CapabilityError` *before* a single token is spent.

Nothing is trusted because the model said it
--------------------------------------------

Everything that comes back is verified deterministically by
:func:`verify_facts`, outside the model: the fact's ``type`` must be one of
:data:`FACT_TYPES`, and every citation's id is normalised and then has to be
in the trusted set (or resolvable, in web mode).  A fact that ends up with no
citations is *rejected*, not reported -- it is exactly the shape a
hallucination takes.  Rejected facts are kept in
:attr:`GeneFacts.rejected_facts` with a reason rather than silently dropped,
because "the model asserted this and could not cite it" is information.

Each fact is validated independently, so one malformed entry costs that entry
and not the whole answer, and a malformed answer is NEVER retried: a retry is
a second paid call to fix something the first call already told us about.

No ungrounded calls
-------------------

In context mode the call is made only if at least one selected paper actually
carries text (an abstract, or a snippet layer 1 recorded).  With nothing to
read, a model can only answer from prior knowledge, which is precisely what
this module exists to refuse.  Such a call returns ``status="no_evidence"``
and spends nothing.  If the reason there is no text is that *retrieval
failed*, the status is ``"error"`` instead and the result is NOT cached --
caching a transient failure would make it permanent.

Annotation note: pydantic evaluates field annotations at class-creation time
and this repository still builds on Python 3.9, so model fields use
``Optional[...]``/``Union[...]`` rather than PEP 604 unions.

Example::

    from kbutillib.domains.literature import GeneFactsUtils

    facts = GeneFactsUtils()
    answer = facts.gene_facts(
        {"id": "ACIAD0380", "aliases": ["dnaA"]},
        {"name": "Acinetobacter baylyi", "synonyms": ["ADP1"]},
        papers=["PMID:16159768", "10.1038/nature12345"],
        model="gpt-4o",
        api_type="openai",
        endpoint={"base_url": "http://127.0.0.1:8000/v1"},
    )
    for fact in answer.facts:
        print(fact.type, fact.statement, [c.paper_id for c in fact.citations])
"""

from __future__ import annotations

import asyncio
import collections.abc
import hashlib
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from pydantic import BaseModel

from ...core.errors import CapabilityError as _RegistryCapabilityError
from ...core.shared_env_utils import SharedEnvUtils
from ..ai import llm
from ..ai.llm.types import WEB_SEARCH_PARAM
from .annotate import EVIDENCE_CLASSES, Evidence, Gene, Organism, PaperAnnotation
from .cache import CACHE_PATH_CONFIG_KEY, LiteratureCache, resolve_cache_path
from .europepmc import merge_ids, normalise_paper_id

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_CONTEXT_TOKEN_BUDGET",
    "DEFAULT_MAX_PAPERS",
    "EVIDENCE_CLASSES",
    "FACTS_JSON_SCHEMA",
    "FACT_TYPES",
    "GENE_FACTS_TABLE",
    "PROMPT_VERSION",
    "REJECTION_REASONS",
    "SCHEMA_VERSION",
    "CapabilityError",
    "Citation",
    "Fact",
    "GeneFacts",
    "GeneFactsUtils",
    "GeneFactsUtilsImpl",
    "PapersUsed",
    "RejectedFact",
    "verify_facts",
]

#: Bumped whenever the prompt text changes in a way that would change an
#: answer.  Part of the cache key, so an old answer is never served for a new
#: prompt.
PROMPT_VERSION = "1"

#: Bumped whenever :data:`FACTS_JSON_SCHEMA` changes shape.  Also part of the
#: cache key, for the same reason.
SCHEMA_VERSION = "1"

#: The kinds of claim a fact may make.  A returned ``type`` outside this set
#: is a rejected fact, not a new category: the set is what a downstream
#: consumer switches on.
FACT_TYPES: Tuple[str, ...] = (
    "function",
    "regulation",
    "expression",
    "phenotype",
    "essentiality",
    "localization",
    "interaction",
    "structure",
    "other",
)

#: Why a fact was rejected.  ``bad_type`` and ``unresolved_citations`` are the
#: two verification verdicts; ``malformed`` is an entry that could not be read
#: as a fact at all (which is isolated to that entry on purpose).
REJECTION_REASONS: Tuple[str, ...] = (
    "bad_type",
    "unresolved_citations",
    "malformed",
)

#: Confidence labels the model may use.
CONFIDENCE_LEVELS: Tuple[str, ...] = ("high", "medium", "low")

#: Papers put in the prompt when the caller names no limit.
DEFAULT_MAX_PAPERS = 20

#: Characters of context per estimated token.  A crude estimate on purpose:
#: it has to be deterministic and tokeniser-free, because it is part of what
#: the cache key is computed over.
CHARS_PER_TOKEN = 4

#: Estimated-token ceiling on the context block.
DEFAULT_CONTEXT_TOKEN_BUDGET = 24000

#: :class:`~kbutillib.domains.literature.cache.LiteratureCache` table holding
#: completed answers.
GENE_FACTS_TABLE = "gene_facts"

#: How a paper competes for a place in the context: a paper that characterises
#: *this* gene beats a curation entry, which beats a homolog, which beats a
#: passing mention.  A paper supplied as a bare id carries no evidence record
#: and so ranks last -- there is nothing to say it is better than one that
#: does.
_CLASS_PRIORITY: Dict[str, int] = {
    "direct": 0,
    "curated": 1,
    "homolog": 2,
    "mention": 3,
}
_NO_CLASS_PRIORITY = 4

#: The answer shape.  Enumerations are spelled from the constants above so a
#: new fact type cannot be added to one and forgotten in the other.
FACTS_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(FACT_TYPES)},
                    "statement": {"type": "string"},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "paper_id": {"type": "string"},
                                "evidence_class": {
                                    "type": "string",
                                    "enum": list(EVIDENCE_CLASSES),
                                },
                                "quote": {"type": "string"},
                            },
                            "required": ["paper_id", "evidence_class"],
                        },
                    },
                    "confidence": {
                        "type": "string",
                        "enum": list(CONFIDENCE_LEVELS),
                    },
                },
                "required": ["type", "statement", "citations", "confidence"],
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["facts"],
}


class CapabilityError(ValueError, _RegistryCapabilityError):
    """The API type cannot do what the caller insisted on.

    Raised by :meth:`GeneFactsUtils.gene_facts` for
    ``web_search="required"`` against an API type whose
    :func:`~kbutillib.domains.ai.llm.supports_web_search` is false -- and
    raised BEFORE the model is called, which is the whole point: the answer
    is free and synchronous, so a caller that needs the live web finds out
    without paying for a call that would have been served from training data.

    It subclasses BOTH :class:`ValueError` (an argument the caller gave is
    not satisfiable, which is how callers of this module already handle bad
    arguments) and :class:`kbutillib.core.errors.CapabilityError` (the
    repository's pre-existing base class for "the capability you asked for
    is not there").  The double base is deliberate: the two names would
    otherwise collide across namespaces and ``except
    kbutillib.core.CapabilityError`` would silently fail to catch this one.
    """


# ── models ───────────────────────────────────────────────────────────────


class Citation(BaseModel):
    """One publication a fact rests on.

    Attributes:
        paper_id: Canonical publication id
            (:func:`~kbutillib.domains.literature.europepmc.canonical_paper_id`
            form) -- normalised here, never the spelling the model used.
        evidence_class: One of
            :data:`~kbutillib.domains.literature.annotate.EVIDENCE_CLASSES`.
            A citation the model left unlabelled is recorded as ``mention``,
            the weakest claim, rather than being promoted by default.
        quote: The sentence the model took the claim from, when it gave one.
    """

    paper_id: str
    evidence_class: str
    quote: Optional[str] = None


class Fact(BaseModel):
    """One verified, cited statement about the gene.

    Attributes:
        type: One of :data:`FACT_TYPES`.
        statement: The claim, in the model's words.
        citations: At least one :class:`Citation`.  A fact with none is not a
            fact -- it is a :class:`RejectedFact`.
        confidence: ``"high"``, ``"medium"`` or ``"low"``.  An unrecognised
            value is recorded as ``"low"``.
    """

    type: str
    statement: str
    citations: List[Citation] = []
    confidence: str = "low"


class RejectedFact(BaseModel):
    """A statement the model returned and verification refused.

    Kept rather than dropped: "the model asserted this and could not cite
    it" is the single most useful signal this module produces about a model.

    Attributes:
        type: The returned type, whatever it was (possibly not a
            :data:`FACT_TYPES` member -- that is often why it was rejected).
        statement: The claim.
        reason: One of :data:`REJECTION_REASONS`.
        citations: The citation ids as returned, unnormalised, so the caller
            can see what was cited and why it did not stand up.
    """

    type: Optional[str] = None
    statement: str = ""
    reason: str = ""
    citations: List[str] = []


class PapersUsed(BaseModel):
    """Which papers reached the prompt, and what happened to the rest.

    Attributes:
        selected: Canonical ids actually in the context block, in the
            priority order they were laid out in.
        dropped_for_budget: Ids cut by ``max_papers`` or by the context token
            budget, lowest priority first.
        unresolved: Supplied ids Europe PMC knows nothing about, plus any
            that are not recognisable publication ids at all.  Not an error:
            an unresolved id is simply not trustworthy enough to cite.
        no_text: Ids that resolved but carry neither an abstract nor a
            snippet, so they contribute nothing a model can read.
        retrieval_failures: Ids whose lookup RAISED.  Distinct from
            ``unresolved`` because this is transient, and a result shaped by
            one is never cached.
        aliases: Every other id each selected paper is known by -> its
            canonical id.  A model handed a DOI may cite the PMID; this is
            how that citation still verifies.
    """

    selected: List[str] = []
    dropped_for_budget: List[str] = []
    unresolved: List[str] = []
    no_text: List[str] = []
    retrieval_failures: List[str] = []
    aliases: Dict[str, str] = {}


class GeneFacts(BaseModel):
    """Everything one :meth:`GeneFactsUtils.gene_facts` call produced.

    Attributes:
        gene_id: The caller's gene id.
        status: ``"ok"`` (a model answered), ``"no_evidence"`` (no call was
            made because there was nothing to ground it on) or ``"error"``
            (the call, or retrieval, failed).
        mode: ``"context"`` or ``"web"``.
        facts: The verified facts.
        rejected_facts: What verification refused, with reasons.
        papers_used: The retrieval and selection record.
        model: The model name as the caller asked for it.
        resolved_model: What the provider says it actually ran.
        api_type: The API type the call went through.
        usage: Provider token accounting, when it reported any.
        elapsed_s: Wall clock for the whole call, retrieval included.
        cache_key: The hash this answer is stored under.  Reported even on a
            result that is not cached, because it is how a caller reasons
            about why two calls did or did not share an answer.
        from_cache: True when this answer was read from the cache rather than
            produced now.
        prompt_version: :data:`PROMPT_VERSION` at the time of the call.
        notes: The model's free-text note, when the schema carried one.
        error: Message for ``status="error"``, including which retrievals
            failed.
    """

    gene_id: str
    status: str
    mode: str
    facts: List[Fact] = []
    rejected_facts: List[RejectedFact] = []
    papers_used: PapersUsed = PapersUsed()
    model: Optional[str] = None
    resolved_model: Optional[str] = None
    api_type: Optional[str] = None
    usage: Optional[Dict[str, Optional[int]]] = None
    elapsed_s: float = 0.0
    cache_key: Optional[str] = None
    from_cache: bool = False
    prompt_version: str = PROMPT_VERSION
    notes: str = ""
    error: Optional[str] = None


# ── verification ─────────────────────────────────────────────────────────


def _citation_ids(raw: Any) -> List[str]:
    """The citation ids in a raw fact, as the model spelled them."""
    ids: List[str] = []
    for entry in raw if isinstance(raw, (list, tuple)) else []:
        if isinstance(entry, dict):
            value = entry.get("paper_id")
        else:
            value = entry
        if value is not None:
            ids.append(str(value))
    return ids


def verify_facts(
    facts: Any,
    *,
    trusted: Dict[str, str],
    mode: str = "context",
    resolve: Optional[Any] = None,
) -> Tuple[List[Fact], List[RejectedFact]]:
    """Check the model's facts against a trusted citation set.

    Deterministic and free: no model is consulted, and in context mode no
    network call is made either.  Every entry is validated on its own, so one
    malformed fact costs that fact and nothing else.

    Args:
        facts: The ``facts`` array as the model returned it (anything else --
            None, a string, a dict -- is read as "no facts").
        trusted: ``{any known id: canonical id}``.  Must map every canonical
            id to itself as well as every alias to its canonical form, which
            is what :meth:`GeneFactsUtils._retrieve` builds.
        mode: ``"context"`` (the trusted set is closed) or ``"web"`` (an id
            outside it may still be accepted if it resolves).
        resolve: Callable taking one id and returning a record with a
            ``canonical_id``, or None.  Used in web mode only --
            :meth:`~kbutillib.domains.literature.europepmc.EuropePMCClient.resolve`.
            Absent, web mode behaves exactly like context mode.

    Returns:
        ``(verified, rejected)``.  A citation that cannot be placed is
        dropped from its fact; a fact left with no citations is rejected with
        reason ``"unresolved_citations"``, and a fact whose type is not in
        :data:`FACT_TYPES` is rejected with ``"bad_type"`` before its
        citations are even looked at.
    """
    verified: List[Fact] = []
    rejected: List[RejectedFact] = []
    # Resolution is memoised per call: a model citing the same paper in five
    # facts must not cost five lookups.
    resolved_cache: Dict[str, Optional[str]] = {}

    def canonical(raw_id: Any) -> Optional[str]:
        """The canonical id for a cited id, or None if it is not trustworthy."""
        identifier = normalise_paper_id(raw_id)
        if identifier is None:
            return None
        if identifier in trusted:
            return trusted[identifier]
        if mode != "web" or resolve is None:
            return None
        if identifier not in resolved_cache:
            try:
                record = resolve(identifier)
            except Exception:  # noqa: BLE001 - an unverifiable id is dropped
                record = None
            resolved_cache[identifier] = (
                str((record or {}).get("canonical_id") or identifier)
                if record
                else None
            )
        return resolved_cache[identifier]

    for raw in facts if isinstance(facts, (list, tuple)) else []:
        if not isinstance(raw, dict):
            rejected.append(
                RejectedFact(statement=str(raw)[:500], reason="malformed")
            )
            continue

        statement = str(raw.get("statement") or "").strip()
        fact_type = raw.get("type")
        raw_ids = _citation_ids(raw.get("citations"))

        if not statement:
            rejected.append(
                RejectedFact(
                    type=str(fact_type) if fact_type is not None else None,
                    reason="malformed",
                    citations=raw_ids,
                )
            )
            continue

        if fact_type not in FACT_TYPES:
            rejected.append(
                RejectedFact(
                    type=str(fact_type) if fact_type is not None else None,
                    statement=statement,
                    reason="bad_type",
                    citations=raw_ids,
                )
            )
            continue

        citations: List[Citation] = []
        seen: set = set()
        for entry in raw.get("citations") or []:
            source = entry if isinstance(entry, dict) else {"paper_id": entry}
            paper_id = canonical(source.get("paper_id"))
            if paper_id is None or paper_id in seen:
                continue
            evidence_class = source.get("evidence_class")
            if evidence_class not in EVIDENCE_CLASSES:
                # Unlabelled or mislabelled: record the weakest claim rather
                # than letting the model's silence read as "direct".
                evidence_class = "mention"
            quote = source.get("quote")
            citations.append(
                Citation(
                    paper_id=paper_id,
                    evidence_class=str(evidence_class),
                    quote=str(quote) if quote else None,
                )
            )
            seen.add(paper_id)

        if not citations:
            rejected.append(
                RejectedFact(
                    type=str(fact_type),
                    statement=statement,
                    reason="unresolved_citations",
                    citations=raw_ids,
                )
            )
            continue

        confidence = raw.get("confidence")
        verified.append(
            Fact(
                type=str(fact_type),
                statement=statement,
                citations=citations,
                confidence=(
                    str(confidence) if confidence in CONFIDENCE_LEVELS else "low"
                ),
            )
        )

    return verified, rejected


# ── internal plan ────────────────────────────────────────────────────────


class _Plan(BaseModel):
    """What one gene's call needs, decided before anything is spent.

    Everything in here is produced by :meth:`GeneFactsUtils._plan`, which
    does the retrieval, the selection, the context rendering and the cache
    read.  ``answer`` being set means the call is already over (a cache hit,
    or nothing worth asking); otherwise ``request`` is the one call to make.
    """

    gene_id: str
    mode: str
    cache_key: str
    context: str = ""
    trusted: Dict[str, str] = {}
    papers_used: PapersUsed = PapersUsed()
    cacheable: bool = True
    request: Optional[llm.LLMRequest] = None
    answer: Optional[GeneFacts] = None
    error: Optional[str] = None


# ── the utility ──────────────────────────────────────────────────────────


class GeneFactsUtils(SharedEnvUtils):
    """Extract typed, cited facts about a gene from the literature.

    Args:
        europepmc: Object exposing ``get(ids)`` and ``resolve(id)``.  Defaults
            to a lazily created
            :class:`~kbutillib.domains.literature.europepmc.EuropePMCClient`.
        cache: Shared literature cache.  Defaults to a
            :class:`~kbutillib.domains.literature.cache.LiteratureCache` at
            ``gene_literature.cache_path``.
        clock: Monotonic time source for ``elapsed_s``.  Defaults to
            :func:`time.monotonic`.
        **kwargs: Passed to :class:`SharedEnvUtils` (``config``,
            ``config_file``, ``token_file``, ...).

    Config:
        ``gene_literature.cache_path``: cache location.  The Europe PMC
        client reads its own (``gene_literature.europepmc_rps``).

    Note:
        Every collaborator is built lazily, so a ``web_search="off"`` call
        with the papers already supplied as records makes no HTTP request of
        its own beyond resolving the ids it was given.
    """

    def __init__(
        self,
        europepmc: Optional[Any] = None,
        cache: Optional[LiteratureCache] = None,
        clock: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._europepmc = europepmc
        self._cache = cache
        self._clock = clock if clock is not None else time.monotonic

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
    def europepmc(self) -> Any:
        """The Europe PMC client, built on first use when not injected."""
        if self._europepmc is None:
            from .europepmc import EuropePMCClient

            self._europepmc = EuropePMCClient(**self._delegate_kwargs())
        return self._europepmc

    @property
    def cache(self) -> LiteratureCache:
        """The shared literature cache, opened on first use when not injected."""
        if self._cache is None:
            self._cache = LiteratureCache(
                resolve_cache_path(self.get_config_value(CACHE_PATH_CONFIG_KEY))
            )
        return self._cache

    # ── public API ────────────────────────────────────────────────────

    def gene_facts(
        self,
        gene: Union[Gene, Dict[str, Any]],
        organism: Union[Organism, Dict[str, Any]],
        papers: Optional[Union[PaperAnnotation, Sequence[str]]] = None,
        *,
        model: str,
        api_type: str,
        endpoint: Optional[Dict[str, Any]] = None,
        web_search: str = "auto",
        max_papers: int = DEFAULT_MAX_PAPERS,
        context_token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
        force: bool = False,
    ) -> GeneFacts:
        """Extract the facts one gene's literature supports.

        Args:
            gene: :class:`~kbutillib.domains.literature.annotate.Gene` or a
                dict with the same keys.
            organism: :class:`~kbutillib.domains.literature.annotate.Organism`
                or a dict with the same keys.
            papers: None (web mode finds its own; context mode has nothing to
                read), a list of publication ids in any form
                :func:`~kbutillib.domains.literature.europepmc.normalise_paper_id`
                accepts, or a
                :class:`~kbutillib.domains.literature.annotate.PaperAnnotation`
                -- layer 1's output, which also carries the evidence class and
                snippets that decide which papers get a place in the prompt.
            model: The model name the PROVIDER knows.
            api_type: Which kind of provider to ask (see
                :func:`kbutillib.domains.ai.llm.api_types`).
            endpoint: The endpoint mapping for that API type.
            web_search: ``"off"`` forces context mode; ``"auto"`` uses web
                mode when the API type supports it; ``"required"`` insists on
                web mode and raises rather than quietly downgrading.
            max_papers: Most papers to put in the prompt.
            context_token_budget: Estimated-token ceiling for the context
                block (``len(text) / CHARS_PER_TOKEN``).  Lowest-priority
                papers are dropped until the block fits.
            force: Skip the cache read and overwrite the entry.

        Returns:
            A :class:`GeneFacts`.  ``status="error"`` for a failed call or
            failed retrieval; such a result is never cached.

        Raises:
            CapabilityError: ``web_search="required"`` and *api_type* cannot
                search the web.  Raised before the model is called.
            ValueError: ``web_search`` is not one of the three accepted
                values.
            KeyError: *api_type* is not a registered API type.
        """
        started = self._clock()
        gene_model = gene if isinstance(gene, Gene) else Gene(**dict(gene))
        target = (
            organism if isinstance(organism, Organism) else Organism(**dict(organism))
        )
        mode = self._resolve_mode(web_search, api_type, endpoint)

        plan = self._plan(
            gene_model,
            target,
            papers,
            mode=mode,
            model=model,
            api_type=api_type,
            endpoint=endpoint,
            max_papers=max_papers,
            context_token_budget=context_token_budget,
            force=force,
        )
        if plan.answer is not None:
            plan.answer.elapsed_s = max(0.0, self._clock() - started)
            return plan.answer

        result = llm.complete(plan.request, api_type=api_type, endpoint=endpoint)
        return self._finalise(
            plan,
            result,
            model=model,
            api_type=api_type,
            elapsed_s=max(0.0, self._clock() - started),
        )

    def genes_facts(
        self,
        genes: Iterable[Union[Gene, Dict[str, Any]]],
        organism: Union[Organism, Dict[str, Any]],
        papers_by_gene: Optional[Dict[str, Any]] = None,
        *,
        model: str,
        api_type: str,
        endpoint: Optional[Dict[str, Any]] = None,
        web_search: str = "auto",
        max_papers: int = DEFAULT_MAX_PAPERS,
        context_token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
        concurrency: int = 4,
        force: bool = False,
    ) -> Dict[str, GeneFacts]:
        """:meth:`gene_facts` for many genes, up to *concurrency* calls at once.

        Args:
            genes: The genes.  Two entries sharing an id is refused before
                anything runs -- one would overwrite the other's answer.
            organism: The organism they come from.
            papers_by_gene: ``{gene_id: papers}``, each value in any form
                :meth:`gene_facts` accepts.  A gene with no entry is called
                with ``papers=None``.
            model: The model name.
            api_type: The API type.
            endpoint: Its endpoint mapping.
            web_search: As :meth:`gene_facts`.  ``"required"`` against an API
                type that cannot search raises here too, before any gene is
                started.
            max_papers: As :meth:`gene_facts`.
            context_token_budget: As :meth:`gene_facts`.
            concurrency: Most model calls in flight at once.
            force: As :meth:`gene_facts`.

        Returns:
            ``{gene_id: GeneFacts}``, one entry per input gene.  A gene whose
            call raised comes back with ``status="error"`` and its message;
            it never aborts the others.

        Raises:
            CapabilityError: as :meth:`gene_facts`, before any call.
            ValueError: two genes share an id, or ``web_search`` is not an
                accepted value.
        """
        gene_list = [
            entry if isinstance(entry, Gene) else Gene(**dict(entry))
            for entry in (genes or [])
        ]
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

        target = (
            organism if isinstance(organism, Organism) else Organism(**dict(organism))
        )
        # Before any gene runs: the capability answer is free, and failing
        # after gene 1 of 400 would have spent 1 call to learn it.
        mode = self._resolve_mode(web_search, api_type, endpoint)
        supplied = dict(papers_by_gene or {})

        async def run_all() -> List[GeneFacts]:
            semaphore = asyncio.Semaphore(max(1, int(concurrency)))

            async def one(gene: Gene) -> GeneFacts:
                started = self._clock()
                try:
                    # The retrieval and the cache read are blocking, so they
                    # go to a thread: done inline they would serialise every
                    # gene behind the slowest HTTP lookup and the semaphore
                    # would never see more than one call.
                    plan = await asyncio.to_thread(
                        self._plan,
                        gene,
                        target,
                        supplied.get(gene.id),
                        mode=mode,
                        model=model,
                        api_type=api_type,
                        endpoint=endpoint,
                        max_papers=max_papers,
                        context_token_budget=context_token_budget,
                        force=force,
                    )
                    if plan.answer is not None:
                        plan.answer.elapsed_s = max(0.0, self._clock() - started)
                        return plan.answer
                    async with semaphore:
                        result = await llm.acomplete(
                            plan.request, api_type=api_type, endpoint=endpoint
                        )
                    return self._finalise(
                        plan,
                        result,
                        model=model,
                        api_type=api_type,
                        elapsed_s=max(0.0, self._clock() - started),
                    )
                except Exception as error:  # noqa: BLE001 - per gene isolation
                    self.log_warning(f"gene_facts failed for {gene.id}: {error}")
                    return GeneFacts(
                        gene_id=gene.id,
                        status="error",
                        mode=mode,
                        model=model,
                        api_type=api_type,
                        elapsed_s=max(0.0, self._clock() - started),
                        error=f"{type(error).__name__}: {error}",
                    )

            return list(await asyncio.gather(*[one(gene) for gene in gene_list]))

        # Owning a loop locally rather than asyncio.run, for the reason
        # recorded in llm.complete: asyncio.run leaves the thread with no
        # current event loop and other modules here still ask for one.
        loop = asyncio.new_event_loop()
        try:
            answers = loop.run_until_complete(run_all())
        finally:
            loop.close()
        return {answer.gene_id: answer for answer in answers}

    # ── mode ──────────────────────────────────────────────────────────

    def _resolve_mode(
        self,
        web_search: str,
        api_type: str,
        endpoint: Optional[Dict[str, Any]],
    ) -> str:
        """Decide context vs web mode, spending nothing to do it.

        Raises:
            CapabilityError: ``"required"`` against an API type that cannot
                search the web.
            ValueError: ``web_search`` is not an accepted value.
        """
        choice = str(web_search or "").strip().lower()
        if choice == "off":
            return "context"
        if choice not in ("auto", "required"):
            raise ValueError(
                f"web_search must be 'off', 'auto' or 'required', not {web_search!r}"
            )
        capable = llm.supports_web_search(api_type, endpoint)
        if capable:
            return "web"
        if choice == "required":
            raise CapabilityError(
                f"web_search='required' but api type {api_type!r} cannot run a "
                "call with live web search; use web_search='auto' to fall back "
                "to context mode, or name an api type that can search"
            )
        return "context"

    # ── planning: retrieve, select, render, look in the cache ─────────

    def _plan(
        self,
        gene: Gene,
        organism: Organism,
        papers: Any,
        *,
        mode: str,
        model: str,
        api_type: str,
        endpoint: Optional[Dict[str, Any]],
        max_papers: int,
        context_token_budget: int,
        force: bool,
    ) -> _Plan:
        """Everything that happens before the model is called.

        Resolves the supplied ids, selects and renders the context, computes
        the cache key over exactly what will be sent, and reads the cache.
        Returns a :class:`_Plan` whose ``answer`` is set when there is
        nothing left to ask.
        """
        candidates, evidence = _read_papers(gene, papers)
        selected, used = self._retrieve(candidates, evidence)

        # max_papers first, then the token budget: both drop the lowest
        # priority first, and both record what they dropped.
        limit = max(0, int(max_papers))
        if len(selected) > limit:
            used.dropped_for_budget.extend(entry["id"] for entry in selected[limit:])
            selected = selected[:limit]

        blocks = [_paper_block(entry, organism) for entry in selected]
        kept, dropped = _fit_budget(blocks, context_token_budget)
        if dropped:
            used.dropped_for_budget.extend(selected[index]["id"] for index in dropped)
            selected = [entry for index, entry in enumerate(selected) if index in kept]
        context = "\n\n".join(blocks[index] for index in sorted(kept))
        used.selected = [entry["id"] for entry in selected]
        used.no_text = [
            entry["id"] for entry in selected if not _has_text(entry)
        ]

        cache_key = _cache_key(
            gene=gene,
            organism=organism,
            max_papers=limit,
            evidence=evidence,
            context=context,
            model=model,
            api_type=api_type,
            endpoint=endpoint,
            mode=mode,
        )
        trusted = _trusted_map(selected, used)

        # A retrieval failure makes every result it touches provisional, so
        # nothing shaped by one is ever written to the cache.
        cacheable = not used.retrieval_failures
        error = (
            "Europe PMC retrieval failed for: "
            + ", ".join(used.retrieval_failures)
            if used.retrieval_failures
            else None
        )

        plan = _Plan(
            gene_id=gene.id,
            mode=mode,
            cache_key=cache_key,
            context=context,
            trusted=trusted,
            papers_used=used,
            cacheable=cacheable,
            error=error,
        )

        if not force:
            cached = self.cache.get(GENE_FACTS_TABLE, cache_key)
            if cached is not None:
                try:
                    answer = GeneFacts.model_validate(cached)
                except Exception as problem:  # noqa: BLE001 - a stale row is not fatal
                    self.log_warning(
                        f"Ignoring unreadable gene_facts cache row {cache_key}: "
                        f"{problem}"
                    )
                else:
                    answer.from_cache = True
                    plan.answer = answer
                    return plan

        # Context mode with nothing readable cannot be grounded, so it is not
        # asked at all.  Web mode can go and look, so it is.
        if mode == "context" and not any(_has_text(entry) for entry in selected):
            answer = GeneFacts(
                gene_id=gene.id,
                status="error" if used.retrieval_failures else "no_evidence",
                mode=mode,
                papers_used=used,
                model=model,
                api_type=api_type,
                cache_key=cache_key,
                error=error,
            )
            if answer.status == "no_evidence" and cacheable:
                self.cache.put(
                    GENE_FACTS_TABLE, cache_key, _storable(answer)
                )
            plan.answer = answer
            return plan

        plan.request = llm.LLMRequest(
            model=model,
            system=_system_prompt(organism, mode),
            user=_user_prompt(gene, organism, context, mode),
            json_schema=FACTS_JSON_SCHEMA,
            params={WEB_SEARCH_PARAM: True} if mode == "web" else {},
            trace_id=f"gene_facts|{gene.id}|{mode}",
        )
        return plan

    def _retrieve(
        self,
        candidates: List[str],
        evidence: Dict[str, List[Evidence]],
    ) -> Tuple[List[Dict[str, Any]], PapersUsed]:
        """Resolve the supplied ids and rank what came back.

        Only a publication Europe PMC actually knows about is allowed into
        the context or into the trusted citation set: an id the caller got
        wrong must not become an id the model is told it may cite.

        Returns:
            ``(selected, papers_used)`` where ``selected`` is a list of
            ``{"id", "record", "evidence", "order"}`` dicts in priority
            order (direct, then curated, then homolog by identity, then
            mention, then no-evidence), and ``papers_used`` records the
            unresolved ids, the retrieval failures and the alias map.
        """
        used = PapersUsed()
        if not candidates:
            return [], used

        try:
            found = self.europepmc.get(candidates)
        except Exception as error:  # noqa: BLE001 - transient, and reported
            self.log_warning(f"Europe PMC lookup failed: {error}")
            used.retrieval_failures = list(candidates)
            return [], used

        records: List[Dict[str, Any]] = []
        requested_of: Dict[int, List[str]] = {}
        for identifier in candidates:
            record = found.get(identifier)
            if record is None:
                used.unresolved.append(identifier)
                continue
            key = id(record)
            if key not in requested_of:
                requested_of[key] = []
                records.append(record)
            requested_of[key].append(identifier)

        merged, aliases, _ = merge_ids(records)
        # Fold the evidence of every id a paper arrived under onto its one
        # canonical id, so two spellings of one paper are one paper.
        by_canonical: Dict[str, Dict[str, Any]] = {}
        for record in records:
            canonical = None
            for identifier in requested_of[id(record)]:
                canonical = aliases.get(identifier)
                if canonical:
                    break
            if canonical is None:
                # The request spelling is not one of the ids merge_ids keyed
                # on (a PMID looked up and answered by the PMC record, say),
                # so go through the record's OWN ids instead.
                for field in ("pmid", "pmcid", "doi"):
                    token = normalise_paper_id(record.get(field))
                    if token and token in aliases:
                        canonical = aliases[token]
                        break
            if canonical is None:
                continue
            entry = by_canonical.setdefault(
                canonical,
                {
                    "id": canonical,
                    "record": merged.get(canonical) or record,
                    "evidence": [],
                    "order": len(by_canonical),
                },
            )
            for identifier in requested_of[id(record)]:
                entry["evidence"].extend(evidence.get(identifier, []))

        used.aliases = {
            alias: canonical
            for alias, canonical in aliases.items()
            if canonical in by_canonical
        }
        selected = sorted(by_canonical.values(), key=_priority)
        return selected, used

    # ── finishing ─────────────────────────────────────────────────────

    def _finalise(
        self,
        plan: _Plan,
        result: Any,
        *,
        model: str,
        api_type: str,
        elapsed_s: float,
    ) -> GeneFacts:
        """Verify one model answer, record it, and cache it when it stands.

        A failed call is reported as ``status="error"`` and is NOT cached: an
        endpoint that was down for ten seconds must not answer for this gene
        for ever.
        """
        if result is None or not getattr(result, "ok", False):
            error = getattr(result, "error", None)
            message = getattr(error, "message", None) or "the model call failed"
            kind = getattr(error, "kind", None)
            return GeneFacts(
                gene_id=plan.gene_id,
                status="error",
                mode=plan.mode,
                papers_used=plan.papers_used,
                model=model,
                resolved_model=getattr(result, "resolved_model", None),
                api_type=api_type,
                usage=getattr(result, "usage", None),
                elapsed_s=elapsed_s,
                cache_key=plan.cache_key,
                error=f"{kind}: {message}" if kind else str(message),
            )

        payload = result.parsed
        if not isinstance(payload, dict):
            # The schema was not enforced but the text may still be the
            # answer.  Read it here rather than asking again: a retry is a
            # second paid call to fix something we were already told.
            try:
                payload = json.loads(result.text or "")
            except (TypeError, ValueError):
                payload = None
        if not isinstance(payload, dict):
            return GeneFacts(
                gene_id=plan.gene_id,
                status="error",
                mode=plan.mode,
                papers_used=plan.papers_used,
                model=model,
                resolved_model=result.resolved_model,
                api_type=api_type,
                usage=result.usage,
                elapsed_s=elapsed_s,
                cache_key=plan.cache_key,
                error="the model's answer was not a JSON object",
            )

        facts, rejected = verify_facts(
            payload.get("facts"),
            trusted=plan.trusted,
            mode=plan.mode,
            resolve=(
                self.europepmc.resolve if plan.mode == "web" else None
            ),
        )
        answer = GeneFacts(
            gene_id=plan.gene_id,
            status="ok",
            mode=plan.mode,
            facts=facts,
            rejected_facts=rejected,
            papers_used=plan.papers_used,
            model=model,
            resolved_model=result.resolved_model,
            api_type=api_type,
            usage=result.usage,
            elapsed_s=elapsed_s,
            cache_key=plan.cache_key,
            notes=str(payload.get("notes") or ""),
            error=plan.error,
        )
        if plan.cacheable:
            self.cache.put(GENE_FACTS_TABLE, plan.cache_key, _storable(answer))
        return answer


# ── papers in ────────────────────────────────────────────────────────────


def _read_papers(
    gene: Gene, papers: Any
) -> Tuple[List[str], Dict[str, List[Evidence]]]:
    """Normalise the ``papers`` argument into ids plus their evidence.

    Args:
        gene: The gene, used to restrict a
            :class:`~kbutillib.domains.literature.annotate.PaperAnnotation`
            that carries more than one.
        papers: None, a sequence of id strings, or a ``PaperAnnotation``.

    Returns:
        ``(ids, {id: [Evidence, ...]})`` -- ids in supplied order, normalised
        and de-duplicated, with anything unrecognisable dropped.

    Raises:
        TypeError: ``papers`` is some other type.
    """
    ids: List[str] = []
    evidence: Dict[str, List[Evidence]] = {}

    def add(raw: Any, records: Sequence[Evidence] = ()) -> None:
        identifier = normalise_paper_id(raw)
        if identifier is None:
            return
        if identifier not in evidence:
            ids.append(identifier)
            evidence[identifier] = []
        evidence[identifier].extend(records)

    if papers is None:
        return ids, evidence

    if isinstance(papers, PaperAnnotation):
        annotation = (
            papers.for_gene(gene.id) if gene.id in papers.genes else papers
        )
        entry = annotation.genes.get(gene.id)
        order = (
            entry.paper_ids
            if entry is not None and entry.paper_ids
            else list(annotation.papers)
        )
        for paper_id in order:
            paper = annotation.papers.get(paper_id)
            if paper is None:
                continue
            records: List[Evidence] = []
            for links in paper.genes.values():
                records.extend(links)
            add(paper_id, records)
        return ids, evidence

    if isinstance(papers, (str, bytes)):
        raise TypeError(
            "papers must be None, a sequence of publication ids or a "
            "PaperAnnotation, not a single string"
        )
    if isinstance(papers, collections.abc.Iterable):
        for raw in papers:
            add(raw)
        return ids, evidence

    raise TypeError(
        "papers must be None, a sequence of publication ids or a "
        f"PaperAnnotation, not {type(papers).__name__}"
    )


def _priority(entry: Dict[str, Any]) -> Tuple[int, float, int]:
    """Sort key putting the best-evidenced paper first.

    direct, then curated, then homolog by descending identity, then mention,
    then a paper with no evidence record at all; ties keep supplied order so
    the selection is reproducible.
    """
    classes = [
        str(record.evidence_class) for record in entry.get("evidence") or []
    ]
    rank = min(
        (_CLASS_PRIORITY.get(name, _NO_CLASS_PRIORITY) for name in classes),
        default=_NO_CLASS_PRIORITY,
    )
    identities = [
        float(record.identity)
        for record in entry.get("evidence") or []
        if record.identity is not None
    ]
    best = max(identities) if identities else 0.0
    return (rank, -best, int(entry.get("order") or 0))


def _has_text(entry: Dict[str, Any]) -> bool:
    """Does this paper carry anything a model could actually read?"""
    record = entry.get("record") or {}
    if str(record.get("abstract") or "").strip():
        return True
    return any(
        str(getattr(item, "snippet", "") or "").strip()
        for item in entry.get("evidence") or []
    )


def _fit_budget(
    blocks: Sequence[str], token_budget: int
) -> Tuple[set, List[int]]:
    """Keep as many leading blocks as the token budget allows.

    ``blocks`` arrives in priority order, so dropping from the end is
    dropping the lowest priority first.  The first block is always kept even
    when it alone exceeds the budget: a budget is not a reason to ask a
    question with no context at all, and truncating a paper mid-abstract
    would make the context -- and so the cache key -- depend on where the cut
    landed.

    Returns:
        ``(kept indices, dropped indices)``.
    """
    budget = max(0, int(token_budget))
    kept: set = set()
    dropped: List[int] = []
    used = 0
    for index, block in enumerate(blocks):
        cost = len(block) // CHARS_PER_TOKEN
        if index == 0 or used + cost <= budget:
            kept.add(index)
            used += cost
        else:
            dropped.append(index)
    return kept, dropped


def _trusted_map(
    selected: Sequence[Dict[str, Any]], used: PapersUsed
) -> Dict[str, str]:
    """``{any id of a selected paper: its canonical id}``.

    Every canonical id maps to itself, and every alias of a selected paper
    maps to its canonical form, so a model handed a DOI and citing the PMID
    still verifies.
    """
    trusted = {entry["id"]: entry["id"] for entry in selected}
    for alias, canonical in (used.aliases or {}).items():
        if canonical in trusted:
            trusted[alias] = canonical
    return trusted


# ── prompts ──────────────────────────────────────────────────────────────


def _system_prompt(organism: Organism, mode: str) -> str:
    """The system prompt: one gene, cited claims only, homologs labelled."""
    lines = [
        "You extract facts about ONE gene of "
        f"{organism.name} from the scientific literature.",
        "",
        "Rules, all of which are checked after you answer:",
        "",
        "1. Every fact MUST carry at least one citation. A fact you cannot "
        "cite is discarded, so do not state it.",
    ]
    if mode == "web":
        lines.append(
            "2. Cite publications you found by their PMID, PMCID or DOI only "
            "-- never a bare URL, a journal name or a web page title. An id "
            "that does not resolve to a real publication is discarded."
        )
    else:
        lines.append(
            "2. Cite ONLY paper ids from the list provided below, exactly as "
            "they are written there. Any other id is discarded."
        )
    lines.extend(
        [
            "3. Label every citation with its evidence_class:",
            "   - 'direct': the paper characterises THIS gene in THIS organism.",
            "   - 'homolog': the paper characterises a homolog, in this or any "
            "other organism. A homolog's characterisation is NEVER 'direct', "
            "however strong the similarity.",
            "   - 'curated': the claim comes from a curation database entry. "
            "A curated entry for a protein of another organism is "
            "homolog-derived evidence and must be labelled 'homolog'.",
            "   - 'mention': the paper names the gene in passing (a survey, a "
            "screen, a table) without characterising it.",
            "4. Do NOT use prior knowledge without a citation. If you know "
            "something about this gene that the provided evidence does not "
            "support, leave it out.",
            "5. Classify each fact's type as one of: " + ", ".join(FACT_TYPES) + ".",
            "6. Answer with JSON matching the provided schema and nothing else.",
        ]
    )
    return "\n".join(lines)


def _user_prompt(gene: Gene, organism: Organism, context: str, mode: str) -> str:
    """The user prompt: which gene, in which organism, and what to read."""
    lines = [f"Gene id: {gene.id}"]
    if gene.aliases:
        lines.append("Aliases: " + ", ".join(str(name) for name in gene.aliases))
    if gene.uniprot:
        lines.append(f"UniProt accession: {gene.uniprot}")
    organism_line = organism.name
    if organism.synonyms:
        organism_line += " (" + ", ".join(str(s) for s in organism.synonyms) + ")"
    if organism.taxon_id is not None:
        organism_line += f" [taxon {organism.taxon_id}]"
    lines.append(f"Organism: {organism_line}")
    if gene.sequence:
        lines.append(f"Protein sequence:\n{gene.sequence}")

    lines.append("")
    if context:
        lines.append("PAPERS (cite by the id in square brackets):")
        lines.append("")
        lines.append(context)
    elif mode == "web":
        lines.append(
            "No papers are provided. Search the web for publications about "
            "this gene and cite what you find by PMID, PMCID or DOI."
        )
    else:
        lines.append("No papers are provided.")
    return "\n".join(lines)


def _paper_block(entry: Dict[str, Any], organism: Organism) -> str:
    """Render one paper for the context block, evidence and all."""
    record = entry.get("record") or {}
    lines = [f"[{entry['id']}] {str(record.get('title') or '(no title)').strip()}"]

    provenance = []
    if record.get("journal"):
        provenance.append(str(record["journal"]))
    if record.get("year"):
        provenance.append(str(record["year"]))
    if provenance:
        lines.append("  " + ", ".join(provenance))

    descriptions: List[str] = []
    snippets: List[str] = []
    for item in entry.get("evidence") or []:
        descriptions.append(_evidence_description(item, organism))
        snippet = str(getattr(item, "snippet", "") or "").strip()
        if snippet and snippet not in snippets:
            snippets.append(snippet)
    unique = []
    for text in descriptions:
        if text not in unique:
            unique.append(text)
    if unique:
        lines.append("  evidence: " + "; ".join(unique))
    for snippet in snippets:
        lines.append(f"  snippet: {snippet}")

    abstract = str(record.get("abstract") or "").strip()
    if abstract:
        lines.append(f"  abstract: {abstract}")
    return "\n".join(lines)


def _evidence_description(evidence: Evidence, organism: Organism) -> str:
    """How layer 1 linked this paper, in words the prompt can use.

    A curated entry for a protein of ANOTHER organism is described as
    homolog-derived: a curation database's claim about a different species'
    protein is evidence about a homolog, and a model told only "curated"
    would reasonably read it as being about this gene.
    """
    label = str(evidence.evidence_class or "mention")
    cross_organism = bool(
        evidence.homolog_organism and not organism.matches(evidence.homolog_organism)
    )
    if label == "curated" and cross_organism:
        label = "curated (homolog-derived: another organism's protein)"
    parts = [label]
    if evidence.curated_db:
        parts.append(f"in {evidence.curated_db}")
    if evidence.homolog_id:
        homolog = f"via homolog {evidence.homolog_id}"
        if evidence.homolog_organism:
            homolog += f" of {evidence.homolog_organism}"
        parts.append(homolog)
    if evidence.identity is not None:
        parts.append(f"identity {evidence.identity:.2f}")
    if evidence.source:
        parts.append(f"[{evidence.source}]")
    return " ".join(parts)


# ── cache key ────────────────────────────────────────────────────────────


def _sha256(text: str) -> str:
    """Hex SHA-256 of *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_key(
    *,
    gene: Gene,
    organism: Organism,
    max_papers: int,
    evidence: Dict[str, List[Evidence]],
    context: str,
    model: str,
    api_type: str,
    endpoint: Optional[Dict[str, Any]],
    mode: str,
) -> str:
    """The hash one answer is stored under.

    Hashed over the normalised INPUTS plus a hash of the exact context text
    that will be sent -- not over the paper ids alone.  Two calls whose ids
    match but whose abstracts, snippets or budget-driven truncation differ are
    two different questions, and a key that could not tell them apart would
    answer one with the other.

    The sequence enters as its own hash rather than in full: a 2000-residue
    protein would otherwise dominate the key's input for no benefit.
    """
    payload = {
        "gene": {
            "id": gene.id,
            "aliases": sorted(str(name) for name in gene.aliases or []),
            "sequence_sha256": (
                _sha256(gene.sequence) if gene.sequence else None
            ),
            "uniprot": gene.uniprot,
        },
        "organism": organism.model_dump(),
        "max_papers": int(max_papers),
        "evidence": {
            paper_id: sorted(
                json.dumps(record.model_dump(), sort_keys=True)
                for record in records
            )
            for paper_id, records in sorted((evidence or {}).items())
        },
        "context_sha256": _sha256(context),
        "model": model,
        "api_type": api_type,
        "endpoint_key": llm.endpoint_key(endpoint),
        "mode": mode,
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
    }
    return _sha256(json.dumps(payload, sort_keys=True, default=str))


def _storable(answer: GeneFacts) -> Dict[str, Any]:
    """The cacheable form of an answer.

    ``from_cache`` is dropped rather than stored false: it describes this
    read, not the answer, and a stored ``False`` would come back as a lie the
    moment it was served from the cache.
    """
    stored = answer.model_dump()
    stored.pop("from_cache", None)
    return stored


# ── Composition-based implementation ─────────────────────────────────────


class GeneFactsUtilsImpl:
    """Composition-based version of :class:`GeneFactsUtils`.

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
        # the same gene_literature.* settings the env was built with.
        try:
            _kwargs["config"] = env.export_environment().get("config") or {}
        except Exception:
            pass
        _kwargs.update(kwargs)
        self._delegate = GeneFactsUtils(**_kwargs)

    @property
    def env(self):
        return self._env

    def __getattr__(self, name):
        # Delegate all attribute access to the utility instance.
        return getattr(self._delegate, name)
