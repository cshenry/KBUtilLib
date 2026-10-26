"""Offline tests for layer 2: typed, cited gene facts.

Not one test here calls a model endpoint or opens a socket.  The API types
are registered BY THE TEST through
:func:`kbutillib.domains.ai.llm.register_api_type` -- two of them, one that
reports ``supports_web_search = True`` and one that does not -- and they
answer from a script.  That is deliberate: ``fake`` and ``replay`` are not
shipped registered (see the ``llm`` package docstring), so a fake API type
can only ever come from a test that asked for one.

Most tests inject the Europe PMC client, which is the one documented seam
that would otherwise reach the network.  :class:`TestNoInjection` is the
deliberate exception and is the test that matters most: it injects NOTHING.
A default-wired :class:`GeneFactsUtils` resolves its own cache path from
config under a relocated HOME, builds its own real
:class:`~kbutillib.domains.literature.europepmc.EuropePMCClient`, and goes
through the real :func:`kbutillib.domains.ai.llm.complete` dispatch to a
registered API type.  Only the HTTP transport is intercepted.  Everything
else here can pass while the default wiring -- which is what a caller
actually gets -- is wrong.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any, Dict, List, Mapping, Optional

import pytest
import requests
import requests.adapters

from kbutillib.core import shared_env_utils
from kbutillib.core.errors import CapabilityError as RegistryCapabilityError
from kbutillib.domains.ai import llm
from kbutillib.domains.ai.llm.types import LLMError, LLMRequest, LLMResult
from kbutillib.domains.literature.annotate import (
    Evidence,
    GeneEntry,
    PaperAnnotation,
    PaperEntry,
    PaperIds,
)
from kbutillib.domains.literature.cache import LiteratureCache
from kbutillib.domains.literature.facts import (
    DEFAULT_CONTEXT_TOKEN_BUDGET,
    FACT_TYPES,
    FACTS_JSON_SCHEMA,
    GENE_FACTS_TABLE,
    PROMPT_VERSION,
    CapabilityError,
    GeneFacts,
    GeneFactsUtils,
    GeneFactsUtilsImpl,
    verify_facts,
)

from .test_europepmc import load_fixture

#: The two API type names this module registers.  Not the shipped names, and
#: not ``fake``: a test must not be able to pass because some other test left
#: a plausible-looking name in the registry.
NOSEARCH = "t5_fake_nosearch"
SEARCH = "t5_fake_search"

GENE: Dict[str, Any] = {
    "id": "g1",
    "aliases": ["dnaA", "ACIAD0001"],
    "sequence": "MKTAYIAKQRQ",
    "uniprot": "Q6FF68",
}
ORGANISM: Dict[str, Any] = {
    "name": "Acinetobacter baylyi",
    "synonyms": ["ADP1"],
    "taxon_id": 62977,
}

GOOD_FACT: Dict[str, Any] = {
    "type": "function",
    "statement": "DnaA binds oriC and initiates chromosome replication.",
    "citations": [
        {
            "paper_id": "PMID:1",
            "evidence_class": "direct",
            "quote": "DnaA binds oriC.",
        }
    ],
    "confidence": "high",
}


# ── test doubles ─────────────────────────────────────────────────────────


class Script:
    """What the fake API types answer, and what they were asked.

    Class state rather than instance state because ``llm.get_api`` owns the
    instances -- a test cannot hold the object it wants to interrogate, which
    is exactly the property that makes the dispatch real.
    """

    #: Every LLMRequest either fake has been handed, in order.
    calls: List[LLMRequest] = []
    #: ``{gene_id: answer}`` plus a ``"*"`` fallback.
    answers: Dict[str, Any] = {}
    #: Gene ids whose call must raise (an API type breaking its contract).
    raise_for: set = set()
    #: When set, every call returns this failure instead of an answer.
    fail: Optional[LLMError] = None
    #: Concurrency accounting for the batch test.
    in_flight: int = 0
    max_in_flight: int = 0
    #: Seconds each call awaits, so overlap is observable.
    delay: float = 0.0

    @classmethod
    def reset(cls, answer: Optional[Dict[str, Any]] = None) -> None:
        cls.calls = []
        cls.answers = {"*": answer if answer is not None else answer_with()}
        cls.raise_for = set()
        cls.fail = None
        cls.in_flight = 0
        cls.max_in_flight = 0
        cls.delay = 0.0

    @classmethod
    def gene_of(cls, request: LLMRequest) -> str:
        """The gene a request is for, read out of its trace id."""
        parts = str(request.trace_id or "").split("|")
        return parts[1] if len(parts) > 2 else ""

    @classmethod
    def answer_for(cls, request: LLMRequest) -> Dict[str, Any]:
        return cls.answers.get(cls.gene_of(request), cls.answers["*"])

    @classmethod
    def count(cls) -> int:
        return len(cls.calls)


def answer_with(*facts: Dict[str, Any], notes: str = "") -> Dict[str, Any]:
    """A schema-shaped answer carrying *facts* (the good one by default)."""
    return {
        "facts": list(facts) if facts else [dict(GOOD_FACT)],
        "notes": notes,
    }


class SearchlessApi:
    """A conforming API type that CANNOT search the web.

    As small as the :class:`~kbutillib.domains.ai.llm.protocol.ApiType`
    contract allows -- ``kind``, ``complete``, ``effective_params`` -- so
    every test using it also proves this module copes with an API type
    supplying only the required members.
    """

    supports_web_search = False

    def __init__(self, endpoint: Optional[Mapping[str, Any]] = None) -> None:
        self.kind = NOSEARCH
        self.endpoint = dict(endpoint or {})

    async def complete(self, request: LLMRequest) -> LLMResult:
        Script.calls.append(request)
        Script.in_flight += 1
        Script.max_in_flight = max(Script.max_in_flight, Script.in_flight)
        try:
            if Script.delay:
                await asyncio.sleep(Script.delay)
            if Script.gene_of(request) in Script.raise_for:
                raise OSError("the socket went away")
            if Script.fail is not None:
                return LLMResult(ok=False, error=Script.fail)
            answer = Script.answer_for(request)
            return LLMResult(
                ok=True,
                text=json.dumps(answer, sort_keys=True),
                parsed=json.loads(json.dumps(answer)),
                resolved_model=f"{request.model}-resolved",
                usage={"prompt_tokens": 11, "completion_tokens": 22},
                finish_reason="stop",
                schema_enforced=True,
            )
        finally:
            Script.in_flight -= 1

    def effective_params(self, request: Any) -> Dict[str, Any]:
        return llm.generation_params(request)


class SearchingApi(SearchlessApi):
    """The same API type, except that it CAN run a call with web search."""

    supports_web_search = True

    def __init__(self, endpoint: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(endpoint)
        self.kind = SEARCH


class FakeEuropePMC:
    """Stands in for EuropePMCClient at the two methods facts.py calls.

    Args:
        records: ``{canonical id: hit record}`` -- what ``get`` knows.
        resolvable: ``{canonical id: hit record}`` -- what ``resolve`` knows
            but ``get`` was never asked for (the web-mode citation case).
        fail: Make ``get`` raise, which is a transient retrieval failure.
    """

    def __init__(self, records=None, resolvable=None, fail: bool = False) -> None:
        self.records = dict(records or {})
        self.resolvable = dict(resolvable or {})
        self.fail = fail
        self.get_calls: List[List[str]] = []
        self.resolve_calls: List[str] = []

    def get(self, ids):
        self.get_calls.append(list(ids))
        if self.fail:
            raise RuntimeError("Europe PMC is down")
        return {
            identifier: self.records[identifier]
            for identifier in ids
            if identifier in self.records
        }

    def resolve(self, id):  # noqa: A002 - matches EuropePMCClient.resolve
        self.resolve_calls.append(id)
        record = self.resolvable.get(id) or self.records.get(id)
        if record is None:
            return None
        resolved = dict(record)
        resolved.setdefault("canonical_id", id)
        resolved.setdefault("canonical_ids", [id])
        return resolved


def paper(
    pmid=None,
    pmcid=None,
    doi=None,
    title="A paper",
    abstract="An abstract that says something about the gene.",
    year=2020,
    journal="J. Tests",
):
    """One Europe PMC hit record, in the shape ``_parse_hit`` produces."""
    return {
        "pmid": pmid,
        "pmcid": pmcid,
        "doi": doi,
        "title": title,
        "journal": journal,
        "year": year,
        "abstract": abstract,
        "source": "MED",
    }


def annotation_for(gene_id: str, items) -> PaperAnnotation:
    """A layer-1 annotation from ``(canonical id, ids kwargs, [Evidence])``."""
    papers: Dict[str, PaperEntry] = {}
    order: List[str] = []
    for paper_id, ids, evidence in items:
        papers[paper_id] = PaperEntry(
            ids=PaperIds(**ids),
            title=f"Title of {paper_id}",
            genes={gene_id: list(evidence)},
        )
        order.append(paper_id)
    return PaperAnnotation(
        papers=papers, genes={gene_id: GeneEntry(paper_ids=order)}
    )


def run(coro):
    """Run one coroutine on a loop owned by this call.

    Deliberately not ``asyncio.run``, which leaves the thread with no current
    event loop -- the same reasoning as ``llm.complete`` and the llm suite's
    own helper.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def fake_api_types():
    """Register the two fakes, and leave the process registry as found.

    The registry is module state by design (a plug-in registers once per
    process), so a test that added to it would otherwise be visible to every
    later test in the session -- including the llm suite's "the built-in set
    is exactly the four real kinds" assertion.
    """
    saved_registry = dict(llm._REGISTRY)
    saved_cache = dict(llm._CACHE)
    saved_loaded = llm._entry_points_loaded
    Script.reset()
    llm.register_api_type(NOSEARCH, SearchlessApi, replace=True)
    llm.register_api_type(SEARCH, SearchingApi, replace=True)
    try:
        yield
    finally:
        run(llm.aclose_all())
        llm._REGISTRY.clear()
        llm._REGISTRY.update(saved_registry)
        llm._CACHE.clear()
        llm._CACHE.update(saved_cache)
        llm._entry_points_loaded = saved_loaded


def make_utils(tmp_path, europepmc=None, **kwargs) -> GeneFactsUtils:
    """A GeneFactsUtils with an injected cache and Europe PMC client."""
    return GeneFactsUtils(
        europepmc=europepmc if europepmc is not None else FakeEuropePMC(),
        cache=LiteratureCache(tmp_path / "gene_literature.sqlite"),
        config={},
        config_file=False,
        token_file=None,
        kbase_token_file=None,
        **kwargs,
    )


def one_paper_utils(tmp_path, **kwargs) -> GeneFactsUtils:
    """Utils that know one readable paper, ``PMID:1``."""
    return make_utils(
        tmp_path,
        europepmc=FakeEuropePMC(records={"PMID:1": paper(pmid="1")}),
        **kwargs,
    )


def call(utils, *, api_type=NOSEARCH, papers=("PMID:1",), **kwargs) -> GeneFacts:
    """``gene_facts`` with this module's gene, organism and defaults."""
    options: Dict[str, Any] = {
        "model": "m-1",
        "api_type": api_type,
        "endpoint": {"name": "e1"},
    }
    options.update(kwargs)
    return utils.gene_facts(
        GENE, ORGANISM, list(papers) if papers is not None else None, **options
    )


# ── mode resolution ──────────────────────────────────────────────────────


class TestModeResolution:
    """``web_search`` plus the API type's capability decide the mode."""

    def test_off_is_context_mode_on_both_api_types(self, tmp_path):
        for api_type in (NOSEARCH, SEARCH):
            answer = call(
                one_paper_utils(tmp_path / api_type),
                api_type=api_type,
                web_search="off",
            )
            assert answer.mode == "context", api_type
            assert answer.status == "ok"

    def test_auto_follows_the_api_types_capability(self, tmp_path):
        assert (
            call(
                one_paper_utils(tmp_path / "a"), api_type=SEARCH, web_search="auto"
            ).mode
            == "web"
        )
        assert (
            call(
                one_paper_utils(tmp_path / "b"), api_type=NOSEARCH, web_search="auto"
            ).mode
            == "context"
        )

    def test_auto_is_the_default(self, tmp_path):
        assert call(one_paper_utils(tmp_path), api_type=SEARCH).mode == "web"

    def test_required_is_web_mode_on_a_searching_api_type(self, tmp_path):
        answer = call(
            one_paper_utils(tmp_path), api_type=SEARCH, web_search="required"
        )
        assert answer.mode == "web"
        assert answer.status == "ok"

    def test_required_raises_BEFORE_the_api_type_is_called(self, tmp_path):
        utils = one_paper_utils(tmp_path)
        with pytest.raises(CapabilityError) as excinfo:
            call(utils, api_type=NOSEARCH, web_search="required")
        assert NOSEARCH in str(excinfo.value)
        # The whole point: the refusal is free.  Nothing was asked of the
        # provider, and no paper was even looked up.
        assert Script.count() == 0
        assert utils.europepmc.get_calls == []

    def test_capability_error_is_catchable_as_either_name(self):
        """It is a ValueError AND the repository's core CapabilityError.

        ``kbutillib.core.errors.CapabilityError`` already existed when this
        module was written, so a single-based class of the same name would be
        a trap: ``except kbutillib.core.CapabilityError`` would silently fail
        to catch this one.
        """
        assert issubclass(CapabilityError, ValueError)
        assert issubclass(CapabilityError, RegistryCapabilityError)

    def test_an_unknown_web_search_value_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="web_search must be"):
            call(one_paper_utils(tmp_path), web_search="yes-please")
        assert Script.count() == 0

    def test_an_unregistered_api_type_raises_keyerror(self, tmp_path):
        with pytest.raises(KeyError):
            call(one_paper_utils(tmp_path), api_type="no_such_api_type")

    def test_batch_refuses_required_before_any_gene_runs(self, tmp_path):
        utils = one_paper_utils(tmp_path)
        with pytest.raises(CapabilityError):
            utils.genes_facts(
                [GENE, {"id": "g2"}],
                ORGANISM,
                model="m-1",
                api_type=NOSEARCH,
                web_search="required",
            )
        assert Script.count() == 0


# ── no ungrounded calls ──────────────────────────────────────────────────


class TestNoCallWhenUngrounded:
    """A model is asked nothing it could only answer from prior knowledge."""

    def test_context_mode_with_no_papers_makes_zero_calls(self, tmp_path):
        utils = one_paper_utils(tmp_path)
        answer = call(utils, web_search="off", papers=None)
        assert answer.status == "no_evidence"
        assert answer.mode == "context"
        assert answer.facts == []
        assert Script.count() == 0

    def test_a_no_evidence_answer_is_cached(self, tmp_path):
        utils = one_paper_utils(tmp_path)
        first = call(utils, web_search="off", papers=None)
        second = call(utils, web_search="off", papers=None)
        assert second.status == "no_evidence"
        assert second.from_cache is True
        assert first.from_cache is False
        assert first.cache_key == second.cache_key
        assert Script.count() == 0

    def test_papers_with_no_text_are_not_grounding(self, tmp_path):
        """Resolved, but neither an abstract nor a snippet to read."""
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(
                records={"PMID:1": paper(pmid="1", abstract="   ")}
            ),
        )
        answer = call(utils, web_search="off")
        assert answer.status == "no_evidence"
        assert answer.papers_used.selected == ["PMID:1"]
        assert answer.papers_used.no_text == ["PMID:1"]
        assert Script.count() == 0

    def test_a_layer_one_snippet_IS_grounding(self, tmp_path):
        """No abstract, but PaperBLAST recorded a sentence: that is readable."""
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(
                records={"PMID:1": paper(pmid="1", abstract=None)}
            ),
        )
        annotation = annotation_for(
            "g1",
            [
                (
                    "PMID:1",
                    {"pmid": "1"},
                    [
                        Evidence(
                            source="paperblast",
                            evidence_class="homolog",
                            homolog_id="b1234",
                            identity=0.8,
                            snippet="The b1234 mutant failed to initiate replication.",
                        )
                    ],
                )
            ],
        )
        answer = utils.gene_facts(
            GENE,
            ORGANISM,
            annotation,
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        assert answer.status == "ok"
        assert Script.count() == 1
        assert "failed to initiate replication" in Script.calls[0].user

    def test_retrieval_failure_is_an_error_and_is_NOT_cached(self, tmp_path):
        utils = make_utils(tmp_path, europepmc=FakeEuropePMC(fail=True))
        first = call(utils, web_search="off")
        assert first.status == "error"
        assert "retrieval failed" in (first.error or "")
        assert first.papers_used.retrieval_failures == ["PMID:1"]
        assert Script.count() == 0
        # Nothing was written, so a later call tries again rather than being
        # answered for ever by a ten-second outage.
        assert utils.cache.get(GENE_FACTS_TABLE, first.cache_key) is None
        second = call(utils, web_search="off")
        assert second.status == "error"
        assert second.from_cache is False

    def test_web_mode_with_no_papers_DOES_call(self, tmp_path):
        """Web mode can go and look, so it is asked."""
        answer = call(
            one_paper_utils(tmp_path), api_type=SEARCH, papers=None
        )
        assert answer.mode == "web"
        assert Script.count() == 1
        assert answer.status == "ok"


# ── verification ─────────────────────────────────────────────────────────


class TestVerification:
    """Nothing is trusted because the model said it."""

    def test_an_uncited_paper_id_rejects_the_fact(self, tmp_path):
        Script.reset(
            answer_with(
                {
                    "type": "function",
                    "statement": "Invented.",
                    "citations": [
                        {"paper_id": "PMID:999999", "evidence_class": "direct"}
                    ],
                    "confidence": "high",
                }
            )
        )
        answer = call(one_paper_utils(tmp_path), web_search="off")
        assert answer.facts == []
        assert [r.reason for r in answer.rejected_facts] == [
            "unresolved_citations"
        ]
        assert answer.rejected_facts[0].citations == ["PMID:999999"]
        assert answer.status == "ok", "a rejected fact is not a failed call"

    def test_a_type_outside_FACT_TYPES_is_rejected(self, tmp_path):
        Script.reset(
            answer_with(
                {
                    "type": "vibes",
                    "statement": "Feels important.",
                    "citations": [
                        {"paper_id": "PMID:1", "evidence_class": "direct"}
                    ],
                    "confidence": "high",
                },
                dict(GOOD_FACT),
            )
        )
        answer = call(one_paper_utils(tmp_path), web_search="off")
        assert [r.reason for r in answer.rejected_facts] == ["bad_type"]
        assert answer.rejected_facts[0].type == "vibes"
        # The good fact in the same answer is untouched.
        assert [f.statement for f in answer.facts] == [GOOD_FACT["statement"]]

    def test_one_malformed_entry_does_not_cost_the_others(self, tmp_path):
        Script.reset(
            {
                "facts": [
                    "not a fact at all",
                    {"type": "function", "citations": [], "confidence": "high"},
                    dict(GOOD_FACT),
                ],
                "notes": "partly broken",
            }
        )
        answer = call(one_paper_utils(tmp_path), web_search="off")
        assert len(answer.facts) == 1
        assert [r.reason for r in answer.rejected_facts] == [
            "malformed",
            "malformed",
        ]
        assert answer.notes == "partly broken"
        # And the malformed answer was NOT retried: a retry is a second paid
        # call to fix what the first call already told us.
        assert Script.count() == 1

    def test_an_alias_of_a_selected_paper_verifies_under_its_canonical_id(
        self, tmp_path
    ):
        """A model handed a DOI may cite the PMID.  That still counts."""
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(
                records={"DOI:10.1/x": paper(pmid="7", doi="10.1/x")}
            ),
        )
        Script.reset(
            answer_with(
                {
                    "type": "function",
                    "statement": "Cited by the other id.",
                    "citations": [
                        {"paper_id": "7", "evidence_class": "direct"}
                    ],
                    "confidence": "medium",
                }
            )
        )
        answer = call(utils, web_search="off", papers=("10.1/x",))
        assert answer.papers_used.selected == ["PMID:7"]
        assert [c.paper_id for c in answer.facts[0].citations] == ["PMID:7"]

    def test_web_mode_accepts_an_id_that_resolves(self, tmp_path):
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(
                records={"PMID:1": paper(pmid="1")},
                resolvable={"PMID:42": paper(pmid="42")},
            ),
        )
        Script.reset(
            answer_with(
                {
                    "type": "phenotype",
                    "statement": "Found on the web.",
                    "citations": [
                        {"paper_id": "42", "evidence_class": "direct"},
                        {"paper_id": "PMID:777", "evidence_class": "direct"},
                    ],
                    "confidence": "low",
                }
            )
        )
        answer = call(utils, api_type=SEARCH)
        assert answer.mode == "web"
        assert [c.paper_id for c in answer.facts[0].citations] == ["PMID:42"]
        assert "PMID:42" in utils.europepmc.resolve_calls
        assert "PMID:777" in utils.europepmc.resolve_calls

    def test_context_mode_never_resolves_an_untrusted_id(self, tmp_path):
        """The trusted set is closed in context mode, and costs no lookup."""
        utils = one_paper_utils(tmp_path)
        Script.reset(
            answer_with(
                {
                    "type": "function",
                    "statement": "Invented.",
                    "citations": [
                        {"paper_id": "PMID:42", "evidence_class": "direct"}
                    ],
                    "confidence": "high",
                }
            )
        )
        answer = call(utils, web_search="off")
        assert answer.facts == []
        assert utils.europepmc.resolve_calls == []

    @pytest.mark.parametrize(
        "given, expected", [("direct", "direct"), (None, "mention"), ("fictional", "mention")]
    )
    def test_an_unlabelled_citation_is_recorded_as_the_weakest_claim(
        self, tmp_path, given, expected
    ):
        citation: Dict[str, Any] = {"paper_id": "PMID:1"}
        if given is not None:
            citation["evidence_class"] = given
        Script.reset(
            answer_with(
                {
                    "type": "function",
                    "statement": "Something.",
                    "citations": [citation],
                    "confidence": "high",
                }
            )
        )
        answer = call(one_paper_utils(tmp_path), web_search="off")
        assert answer.facts[0].citations[0].evidence_class == expected

    def test_an_unrecognised_confidence_becomes_low(self, tmp_path):
        Script.reset(
            answer_with(
                {
                    "type": "function",
                    "statement": "Something.",
                    "citations": [
                        {"paper_id": "PMID:1", "evidence_class": "direct"}
                    ],
                    "confidence": "absolutely certain",
                }
            )
        )
        answer = call(one_paper_utils(tmp_path), web_search="off")
        assert answer.facts[0].confidence == "low"

    def test_verify_facts_is_deterministic_and_standalone(self):
        """The verifier is a plain function: no model, no network, no object."""
        trusted = {"PMID:1": "PMID:1", "DOI:10.1/x": "PMID:1"}
        verified, rejected = verify_facts(
            [
                dict(GOOD_FACT),
                {
                    "type": "function",
                    "statement": "Duplicated citation.",
                    "citations": [
                        {"paper_id": "PMID:1", "evidence_class": "direct"},
                        {"paper_id": "10.1/x", "evidence_class": "curated"},
                    ],
                    "confidence": "low",
                },
            ],
            trusted=trusted,
        )
        assert len(verified) == 2
        # Both spellings are one paper, so the second fact cites it once.
        assert [c.paper_id for c in verified[1].citations] == ["PMID:1"]
        assert rejected == []

    @pytest.mark.parametrize("facts", [None, "nonsense", {}, []])
    def test_verify_facts_reads_a_non_list_as_no_facts(self, facts):
        assert verify_facts(facts, trusted={}) == ([], [])

    def test_every_fact_type_is_accepted(self):
        verified, rejected = verify_facts(
            [
                {
                    "type": name,
                    "statement": f"A {name} claim.",
                    "citations": [
                        {"paper_id": "PMID:1", "evidence_class": "direct"}
                    ],
                    "confidence": "high",
                }
                for name in FACT_TYPES
            ],
            trusted={"PMID:1": "PMID:1"},
        )
        assert [f.type for f in verified] == list(FACT_TYPES)
        assert rejected == []


# ── the request that actually goes out ───────────────────────────────────


class TestRequest:
    """What the API type is handed, which is the only thing it can act on."""

    def test_web_mode_sets_the_web_search_param(self, tmp_path):
        call(one_paper_utils(tmp_path), api_type=SEARCH)
        assert Script.calls[0].params.get("web_search") is True

    def test_context_mode_does_not_set_it(self, tmp_path):
        call(one_paper_utils(tmp_path), api_type=SEARCH, web_search="off")
        assert Script.calls[0].params == {}

    def test_the_request_carries_the_answer_schema(self, tmp_path):
        call(one_paper_utils(tmp_path), web_search="off")
        request = Script.calls[0]
        assert request.json_schema == FACTS_JSON_SCHEMA
        assert request.model == "m-1"
        assert request.trace_id == "gene_facts|g1|context"

    def test_the_prompts_say_one_gene_cited_claims_only(self, tmp_path):
        call(one_paper_utils(tmp_path), web_search="off")
        system = Script.calls[0].system
        user = Script.calls[0].user
        assert "ONE gene" in system
        assert ORGANISM["name"] in system
        assert "at least one citation" in system
        assert "NEVER 'direct'" in system, "a homolog is never direct evidence"
        assert "prior knowledge" in system
        # Context mode pins the citation set; web mode asks for ids, not URLs.
        assert "ONLY paper ids from the list" in system
        assert "g1" in user and "dnaA" in user and GENE["sequence"] in user
        assert "[PMID:1]" in user

    def test_web_mode_asks_for_ids_and_not_urls(self, tmp_path):
        call(one_paper_utils(tmp_path), api_type=SEARCH)
        system = Script.calls[0].system
        assert "PMID, PMCID or DOI only" in system
        assert "never a bare URL" in system

    def test_curated_evidence_from_another_organism_is_called_homolog_derived(
        self, tmp_path
    ):
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(records={"PMID:1": paper(pmid="1")}),
        )
        annotation = annotation_for(
            "g1",
            [
                (
                    "PMID:1",
                    {"pmid": "1"},
                    [
                        Evidence(
                            source="paperblast",
                            evidence_class="curated",
                            curated_db="SwissProt",
                            homolog_id="b1234",
                            homolog_organism="Escherichia coli K-12",
                            identity=0.42,
                        )
                    ],
                )
            ],
        )
        utils.gene_facts(
            GENE,
            ORGANISM,
            annotation,
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        user = Script.calls[0].user
        assert "homolog-derived" in user
        assert "Escherichia coli K-12" in user

    def test_curated_evidence_from_THIS_organism_is_not_relabelled(self, tmp_path):
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(records={"PMID:1": paper(pmid="1")}),
        )
        annotation = annotation_for(
            "g1",
            [
                (
                    "PMID:1",
                    {"pmid": "1"},
                    [
                        Evidence(
                            source="paperblast",
                            evidence_class="curated",
                            curated_db="SwissProt",
                            homolog_id="ACIAD0001",
                            homolog_organism="Acinetobacter baylyi ADP1",
                            identity=1.0,
                        )
                    ],
                )
            ],
        )
        utils.gene_facts(
            GENE,
            ORGANISM,
            annotation,
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        assert "homolog-derived" not in Script.calls[0].user


# ── selection ────────────────────────────────────────────────────────────


def selection_annotation() -> PaperAnnotation:
    """Four papers, one per evidence class, supplied worst-first."""
    return annotation_for(
        "g1",
        [
            ("PMID:4", {"pmid": "4"}, [Evidence(source="europepmc", evidence_class="mention")]),
            (
                "PMID:3",
                {"pmid": "3"},
                [
                    Evidence(
                        source="paperblast",
                        evidence_class="homolog",
                        homolog_id="h1",
                        identity=0.3,
                    )
                ],
            ),
            ("PMID:2", {"pmid": "2"}, [Evidence(source="paperblast", evidence_class="curated", curated_db="SwissProt")]),
            ("PMID:1", {"pmid": "1"}, [Evidence(source="europepmc", evidence_class="direct")]),
        ],
    )


def selection_utils(tmp_path) -> GeneFactsUtils:
    return make_utils(
        tmp_path,
        europepmc=FakeEuropePMC(
            records={
                f"PMID:{n}": paper(pmid=str(n), abstract=f"Abstract {n}. " * 20)
                for n in (1, 2, 3, 4)
            }
        ),
    )


class TestSelection:
    """Which papers reach the prompt, in which order, and what is recorded."""

    def test_papers_are_prioritised_direct_curated_homolog_mention(self, tmp_path):
        utils = selection_utils(tmp_path)
        answer = utils.gene_facts(
            GENE,
            ORGANISM,
            selection_annotation(),
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        assert answer.papers_used.selected == [
            "PMID:1",
            "PMID:2",
            "PMID:3",
            "PMID:4",
        ]
        user = Script.calls[0].user
        positions = [user.index(f"[PMID:{n}]") for n in (1, 2, 3, 4)]
        assert positions == sorted(positions), "the prompt follows the priority"

    def test_max_papers_drops_the_lowest_priority_first(self, tmp_path):
        utils = selection_utils(tmp_path)
        answer = utils.gene_facts(
            GENE,
            ORGANISM,
            selection_annotation(),
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
            max_papers=2,
        )
        assert answer.papers_used.selected == ["PMID:1", "PMID:2"]
        assert answer.papers_used.dropped_for_budget == ["PMID:3", "PMID:4"]
        assert "[PMID:3]" not in Script.calls[0].user

    def test_the_token_budget_drops_the_lowest_priority_first(self, tmp_path):
        utils = selection_utils(tmp_path)
        answer = utils.gene_facts(
            GENE,
            ORGANISM,
            selection_annotation(),
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
            context_token_budget=120,
        )
        assert answer.papers_used.selected[0] == "PMID:1"
        assert len(answer.papers_used.selected) < 4
        assert answer.papers_used.dropped_for_budget[-1] == "PMID:4"
        # The highest-priority paper survives even a budget it alone exceeds:
        # a budget is not a reason to ask with no context at all.
        assert "[PMID:1]" in Script.calls[0].user

    def test_an_unresolvable_id_is_recorded_and_never_enters_the_prompt(
        self, tmp_path
    ):
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(records={"PMID:1": paper(pmid="1")}),
        )
        answer = call(
            utils, web_search="off", papers=("PMID:1", "PMID:404", "not-an-id")
        )
        assert answer.papers_used.selected == ["PMID:1"]
        assert answer.papers_used.unresolved == ["PMID:404"]
        assert "PMID:404" not in Script.calls[0].user

    def test_a_dropped_paper_is_NOT_citable(self, tmp_path):
        """The trusted set is what was SENT, not what was retrieved.

        A paper cut by ``max_papers`` never reached the prompt, so a citation
        of it cannot have come from reading it.
        """
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(
                records={
                    "PMID:1": paper(pmid="1"),
                    "PMID:2": paper(pmid="2"),
                }
            ),
        )
        Script.reset(
            answer_with(
                {
                    "type": "function",
                    "statement": "Cites the paper it was not shown.",
                    "citations": [
                        {"paper_id": "PMID:2", "evidence_class": "direct"}
                    ],
                    "confidence": "high",
                }
            )
        )
        answer = call(
            utils, web_search="off", papers=("PMID:1", "PMID:2"), max_papers=1
        )
        assert answer.papers_used.selected == ["PMID:1"]
        assert answer.papers_used.dropped_for_budget == ["PMID:2"]
        assert answer.facts == []
        assert [r.reason for r in answer.rejected_facts] == [
            "unresolved_citations"
        ]

    def test_a_single_string_for_papers_is_refused(self, tmp_path):
        with pytest.raises(TypeError, match="not a single string"):
            one_paper_utils(tmp_path).gene_facts(
                GENE, ORGANISM, "PMID:1", model="m-1", api_type=NOSEARCH
            )


# ── the cache key ────────────────────────────────────────────────────────


class TestCacheKey:
    """The key is over everything that could change the answer, and nothing else."""

    def key(self, tmp_path, **kwargs) -> str:
        utils = make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(
                records={
                    "PMID:1": paper(pmid="1"),
                    "PMID:2": paper(pmid="2"),
                }
            ),
        )
        papers = kwargs.pop("papers", ("PMID:1",))
        gene = dict(GENE)
        gene.update(kwargs.pop("gene", {}))
        organism = dict(ORGANISM)
        organism.update(kwargs.pop("organism", {}))
        options: Dict[str, Any] = {
            "model": "m-1",
            "api_type": NOSEARCH,
            "endpoint": {"name": "e1"},
            "web_search": "off",
        }
        options.update(kwargs)
        answer = utils.gene_facts(
            gene, organism, list(papers) if papers is not None else None, **options
        )
        return answer.cache_key

    def test_the_same_call_twice_is_the_same_key(self, tmp_path):
        assert self.key(tmp_path / "a") == self.key(tmp_path / "b")

    @pytest.mark.parametrize(
        "label, change",
        [
            ("gene id", {"gene": {"id": "g2"}}),
            ("aliases", {"gene": {"aliases": ["dnaA", "other"]}}),
            ("sequence", {"gene": {"sequence": "MKTAYIAKQRR"}}),
            ("no sequence", {"gene": {"sequence": None}}),
            ("uniprot", {"gene": {"uniprot": "P00000"}}),
            ("organism name", {"organism": {"name": "Escherichia coli"}}),
            ("organism synonyms", {"organism": {"synonyms": ["ADP1", "BD413"]}}),
            ("taxon", {"organism": {"taxon_id": 1}}),
            ("max_papers", {"max_papers": 5}),
            ("papers", {"papers": ("PMID:2",)}),
            ("no papers", {"papers": None}),
            ("model", {"model": "m-2"}),
            ("endpoint", {"endpoint": {"name": "e2"}}),
        ],
    )
    def test_changing_any_component_changes_the_key(self, tmp_path, label, change):
        base = self.key(tmp_path / "base")
        assert self.key(tmp_path / "changed", **change) != base, label

    def test_the_api_type_is_in_the_key(self, tmp_path):
        """Same mode, different provider: not the same question."""
        context_on_searchless = self.key(tmp_path / "a", api_type=NOSEARCH)
        context_on_searching = self.key(
            tmp_path / "b", api_type=SEARCH, web_search="off"
        )
        assert context_on_searchless != context_on_searching

    def test_the_mode_is_in_the_key(self, tmp_path):
        """One api type, one model, two modes, two keys."""
        context = self.key(tmp_path / "a", api_type=SEARCH, web_search="off")
        web = self.key(tmp_path / "b", api_type=SEARCH, web_search="auto")
        assert context != web

    def test_the_context_TEXT_is_in_the_key(self, tmp_path):
        """Same paper id, different abstract: not the same question.

        This is what the addendum's context hash buys.  A key over the ids
        alone would answer a call that had the full abstract with one made
        when Europe PMC returned a stub.
        """

        def key_for(abstract):
            utils = make_utils(
                tmp_path / abstract[:4],
                europepmc=FakeEuropePMC(
                    records={"PMID:1": paper(pmid="1", abstract=abstract)}
                ),
            )
            return call(utils, web_search="off").cache_key

        assert key_for("First abstract.") != key_for("Second abstract.")

    def test_the_budget_changes_the_key_only_when_it_changes_the_context(
        self, tmp_path
    ):
        """A budget that drops nothing is not a different question.

        The key is over the context TEXT, so lowering a budget that still
        fits everything must NOT invalidate a cached answer, while one that
        truncates must.  A key over the budget number could not tell those
        two apart.
        """
        both = ("PMID:1", "PMID:2")
        base = self.key(tmp_path / "a", papers=both)
        roomy = self.key(tmp_path / "b", papers=both, context_token_budget=10000)
        tight = self.key(tmp_path / "c", papers=both, context_token_budget=10)
        assert base == roomy
        assert tight != base

    def test_alias_order_does_not_change_the_key(self, tmp_path):
        assert self.key(
            tmp_path / "a", gene={"aliases": ["dnaA", "ACIAD0001"]}
        ) == self.key(tmp_path / "b", gene={"aliases": ["ACIAD0001", "dnaA"]})

    def test_endpoint_key_order_does_not_change_the_key(self, tmp_path):
        assert self.key(
            tmp_path / "a", endpoint={"name": "e1", "url": "u"}
        ) == self.key(tmp_path / "b", endpoint={"url": "u", "name": "e1"})

    def test_force_does_not_change_the_key(self, tmp_path):
        assert self.key(tmp_path / "a") == self.key(tmp_path / "b", force=True)

    def test_the_default_token_budget_is_the_documented_one(self, tmp_path):
        assert self.key(tmp_path / "a") == self.key(
            tmp_path / "b", context_token_budget=DEFAULT_CONTEXT_TOKEN_BUDGET
        )


# ── caching ──────────────────────────────────────────────────────────────


class TestCaching:
    """One paid call per distinct question, and never one for a failure."""

    def test_the_second_call_is_served_from_the_cache(self, tmp_path):
        utils = one_paper_utils(tmp_path)
        first = call(utils, web_search="off")
        second = call(utils, web_search="off")
        assert Script.count() == 1, "the second call spent nothing"
        assert first.from_cache is False
        assert second.from_cache is True
        assert second.status == "ok"
        assert [f.statement for f in second.facts] == [
            f.statement for f in first.facts
        ]
        assert second.prompt_version == PROMPT_VERSION

    def test_force_re_calls_and_overwrites(self, tmp_path):
        utils = one_paper_utils(tmp_path)
        call(utils, web_search="off")
        Script.answers["*"] = answer_with(
            {
                "type": "phenotype",
                "statement": "A later, better answer.",
                "citations": [{"paper_id": "PMID:1", "evidence_class": "direct"}],
                "confidence": "low",
            }
        )
        forced = call(utils, web_search="off", force=True)
        assert Script.count() == 2
        assert forced.from_cache is False
        assert [f.statement for f in forced.facts] == ["A later, better answer."]
        # ...and the overwrite stuck.
        again = call(utils, web_search="off")
        assert again.from_cache is True
        assert [f.statement for f in again.facts] == ["A later, better answer."]
        assert Script.count() == 2

    def test_a_failed_call_is_an_error_and_is_not_cached(self, tmp_path):
        utils = one_paper_utils(tmp_path)
        Script.fail = LLMError(
            kind="transport", message="nothing listening", retryable=True
        )
        answer = call(utils, web_search="off")
        assert answer.status == "error"
        assert "nothing listening" in (answer.error or "")
        assert answer.facts == []
        assert utils.cache.get(GENE_FACTS_TABLE, answer.cache_key) is None

        # The endpoint comes back; the gene is asked again rather than being
        # answered by the outage.
        Script.fail = None
        recovered = call(utils, web_search="off")
        assert recovered.status == "ok"
        assert recovered.from_cache is False
        assert Script.count() == 2

    def test_a_partial_retrieval_failure_is_reported_and_not_cached(self, tmp_path):
        """One id's lookup broke; the rest of the answer still stands."""

        class HalfBrokenEuropePMC(FakeEuropePMC):
            def get(self, ids):
                self.get_calls.append(list(ids))
                raise RuntimeError("Europe PMC is down")

        utils = make_utils(tmp_path, europepmc=HalfBrokenEuropePMC())
        answer = call(utils, api_type=SEARCH, papers=("PMID:1",))
        # Web mode still has somewhere to look, so the call is made...
        assert answer.status == "ok"
        assert Script.count() == 1
        # ...but it is provisional, so nothing is written.
        assert "retrieval failed" in (answer.error or "")
        assert utils.cache.get(GENE_FACTS_TABLE, answer.cache_key) is None

    def test_an_unreadable_cache_row_is_ignored_not_fatal(self, tmp_path):
        utils = one_paper_utils(tmp_path)
        first = call(utils, web_search="off")
        utils.cache.put(GENE_FACTS_TABLE, first.cache_key, {"not": "a GeneFacts"})
        again = call(utils, web_search="off")
        assert again.status == "ok"
        assert again.from_cache is False
        assert Script.count() == 2


# ── batching ─────────────────────────────────────────────────────────────


class TestBatch:
    """Many genes, bounded concurrency, and no shared fate."""

    def batch_utils(self, tmp_path, n=3) -> GeneFactsUtils:
        return make_utils(
            tmp_path,
            europepmc=FakeEuropePMC(
                records={
                    f"PMID:{i}": paper(pmid=str(i)) for i in range(1, n + 1)
                }
            ),
        )

    def test_every_gene_gets_an_entry(self, tmp_path):
        utils = self.batch_utils(tmp_path)
        answers = utils.genes_facts(
            [{"id": "g1"}, {"id": "g2"}, {"id": "g3"}],
            ORGANISM,
            {"g1": ["PMID:1"], "g2": ["PMID:2"], "g3": ["PMID:3"]},
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        assert sorted(answers) == ["g1", "g2", "g3"]
        assert all(answer.status == "ok" for answer in answers.values())
        assert {
            tuple(answer.papers_used.selected) for answer in answers.values()
        } == {("PMID:1",), ("PMID:2",), ("PMID:3",)}

    def test_one_failing_gene_does_not_abort_the_others(self, tmp_path):
        utils = self.batch_utils(tmp_path)
        Script.raise_for = {"g2"}
        answers = utils.genes_facts(
            [{"id": "g1"}, {"id": "g2"}, {"id": "g3"}],
            ORGANISM,
            {"g1": ["PMID:1"], "g2": ["PMID:2"], "g3": ["PMID:3"]},
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        assert answers["g2"].status == "error"
        assert answers["g2"].facts == []
        assert answers["g2"].error
        assert answers["g1"].status == "ok"
        assert answers["g3"].status == "ok"

    def test_a_gene_with_no_papers_is_no_evidence_not_a_failure(self, tmp_path):
        utils = self.batch_utils(tmp_path)
        answers = utils.genes_facts(
            [{"id": "g1"}, {"id": "g2"}],
            ORGANISM,
            {"g1": ["PMID:1"]},
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        assert answers["g1"].status == "ok"
        assert answers["g2"].status == "no_evidence"
        assert Script.count() == 1

    def test_concurrency_is_bounded_by_the_semaphore(self, tmp_path):
        utils = self.batch_utils(tmp_path, n=6)
        Script.delay = 0.02
        utils.genes_facts(
            [{"id": f"g{i}"} for i in range(1, 7)],
            ORGANISM,
            {f"g{i}": [f"PMID:{i}"] for i in range(1, 7)},
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
            concurrency=2,
        )
        assert Script.count() == 6
        assert Script.max_in_flight == 2, Script.max_in_flight

    def test_concurrency_really_overlaps(self, tmp_path):
        utils = self.batch_utils(tmp_path, n=4)
        Script.delay = 0.02
        utils.genes_facts(
            [{"id": f"g{i}"} for i in range(1, 5)],
            ORGANISM,
            {f"g{i}": [f"PMID:{i}"] for i in range(1, 5)},
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
            concurrency=4,
        )
        assert Script.max_in_flight == 4

    def test_a_duplicate_gene_id_is_refused_before_anything_runs(self, tmp_path):
        utils = self.batch_utils(tmp_path)
        with pytest.raises(ValueError, match="Duplicate gene ids"):
            utils.genes_facts(
                [{"id": "g1"}, {"id": "g1"}],
                ORGANISM,
                model="m-1",
                api_type=NOSEARCH,
                web_search="off",
            )
        assert Script.count() == 0

    def test_the_batch_shares_the_cache_with_the_single_call(self, tmp_path):
        utils = self.batch_utils(tmp_path)
        single = utils.gene_facts(
            {"id": "g1"},
            ORGANISM,
            ["PMID:1"],
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        answers = utils.genes_facts(
            [{"id": "g1"}],
            ORGANISM,
            {"g1": ["PMID:1"]},
            model="m-1",
            api_type=NOSEARCH,
            web_search="off",
        )
        assert answers["g1"].from_cache is True
        assert answers["g1"].cache_key == single.cache_key
        assert Script.count() == 1


# ── composition wrapper ──────────────────────────────────────────────────


class TestCompositionWrapper:
    """GeneFactsUtilsImpl delegates and carries the env's config across."""

    def test_impl_delegates_and_reads_the_envs_config(self, tmp_path):
        from kbutillib.core.shared_env_utils import SharedEnvUtils

        cache_path = tmp_path / "from_config.sqlite"
        env = SharedEnvUtils(
            config={"gene_literature": {"cache_path": str(cache_path)}},
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
        impl = GeneFactsUtilsImpl(env)
        assert impl.env is env
        assert impl.cache.path == cache_path
        assert callable(impl.gene_facts)


# ── exports and wiring ───────────────────────────────────────────────────


class TestExports:
    """Every advertised name resolves, through each advertised route."""

    def test_the_literature_package_advertises_and_resolves_every_name(self):
        import kbutillib.domains.literature as literature

        unresolvable = [
            name for name in literature.__all__ if not hasattr(literature, name)
        ]
        assert unresolvable == []
        for name in (
            "CapabilityError",
            "Citation",
            "Fact",
            "GeneFacts",
            "GeneFactsUtils",
            "GeneFactsUtilsImpl",
            "PapersUsed",
            "RejectedFact",
            "verify_facts",
            "FACT_TYPES",
            "FACTS_JSON_SCHEMA",
            "PROMPT_VERSION",
            "SCHEMA_VERSION",
        ):
            assert name in literature.__all__, name
            assert literature._MODULE_MAP[name] == "facts", name

    def test_evidence_classes_is_ONE_tuple_shared_with_layer_one(self):
        """Not a second copy.

        The task prompt asked for an ``EVIDENCE_CLASSES`` in ``facts.py``,
        but ``annotate.py`` already defined one with the same four members,
        and the package's lazy loader can only map the name to one module.
        Two tuples would eventually drift and the loader would advertise
        whichever module was imported first.
        """
        from kbutillib.domains.literature import annotate, facts

        assert facts.EVIDENCE_CLASSES is annotate.EVIDENCE_CLASSES
        assert set(facts.EVIDENCE_CLASSES) == {
            "direct",
            "homolog",
            "curated",
            "mention",
        }

    def test_the_top_level_package_exports_the_facts_api(self):
        import kbutillib

        assert kbutillib.GeneFactsUtils is GeneFactsUtils
        assert kbutillib.GeneFacts is GeneFacts
        for name in ("Citation", "Fact", "GeneFacts", "GeneFactsUtils"):
            assert name in kbutillib.__all__, name

    def test_the_toolkit_exposes_gene_facts_lazily(self):
        from kbutillib.toolkit import KBUtilLib

        toolkit = KBUtilLib(
            config_file=False, token_file=None, kbase_token_file=None
        )
        assert toolkit._gene_facts is None, "built on first use, not at init"
        assert isinstance(toolkit.gene_facts, GeneFactsUtilsImpl)
        assert toolkit.gene_facts is toolkit.gene_facts, "and only once"


# ── the no-injection check ───────────────────────────────────────────────


class TestNoInjection:
    """Default wiring, real dispatch, real cache file, real client.

    Nothing is injected and nothing is monkeypatched inside this package or
    inside ``llm``: ``GeneFactsUtils`` is built with no seams, resolves its
    cache path from config under a relocated HOME, builds its own
    :class:`EuropePMCClient`, and the model call goes through the real
    :func:`llm.complete` dispatch, which resolves a registered API type by
    name.  The only interception is at the HTTP transport
    (``requests.adapters.HTTPAdapter.send``) -- a socket, not a seam of the
    code under test.

    What it proves that the injected tests cannot: that the API type really
    receives the request this module believes it builds, and that the second
    call is answered by a row on disk.
    """

    def wire_home(self, tmp_path, monkeypatch):
        """A relocated HOME with a config naming the cache path."""
        home = tmp_path / "home"
        (home / ".kbutillib").mkdir(parents=True)
        cache_path = home / ".kbutillib" / "gene_literature.sqlite"
        config_path = home / ".kbutillib" / "config.yaml"
        config_path.write_text(
            "gene_literature:\n"
            f"  cache_path: {cache_path}\n"
            # A high rate keeps the real Europe PMC throttle's real sleep
            # negligible; the throttle itself is not stubbed out.
            "  europepmc_rps: 50\n"
        )
        monkeypatch.setenv("HOME", str(home))
        # SharedEnvUtils binds the user-config location at import time, so a
        # relocated HOME has to be pointed at explicitly.
        monkeypatch.setattr(shared_env_utils, "DEFAULT_CONFIG_FILE", config_path)
        return cache_path

    def intercept_http(self, monkeypatch):
        """Replay the recorded Europe PMC batch at the transport layer."""
        payload = load_fixture("get_batch.json")
        sent = []

        def fake_send(adapter, request, **kwargs):
            sent.append(request)
            response = requests.Response()
            response.status_code = 200
            response.encoding = "utf-8"
            response.headers["Content-Type"] = "application/json"
            response._content = json.dumps(payload).encode("utf-8")
            response.url = request.url
            response.request = request
            return response

        monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", fake_send)
        return sent

    #: The three ids the recorded batch was recorded for.  The DOI is NOT in
    #: the response, so it exercises the unresolved path for free.
    SUPPLIED = ["15210978", "PMC2828762", "10.1038/nsmb.1583"]

    def test_default_wiring_calls_the_registered_api_type_and_caches_on_disk(
        self, tmp_path, monkeypatch
    ):
        cache_path = self.wire_home(tmp_path, monkeypatch)
        sent = self.intercept_http(monkeypatch)
        Script.reset(
            answer_with(
                {
                    "type": "function",
                    "statement": "ADP1 is naturally transformation competent.",
                    "citations": [
                        {
                            "paper_id": "15210978",
                            "evidence_class": "direct",
                            "quote": "naturally transformation competent",
                        }
                    ],
                    "confidence": "high",
                }
            )
        )

        # No seams: no europepmc, no cache, no clock, no config dict.
        # token_file/kbase_token_file are not seams of this class; they are
        # silenced only to keep the real HOME's token files out of the test.
        utils = GeneFactsUtils(token_file=None, kbase_token_file=None)
        assert utils.cache.path == cache_path

        first = utils.gene_facts(
            GENE,
            ORGANISM,
            self.SUPPLIED,
            model="m-1",
            api_type=NOSEARCH,
            endpoint={"name": "e1"},
        )

        # The default wiring really built a real client and really called it.
        from kbutillib.domains.literature.europepmc import EuropePMCClient

        assert isinstance(utils.europepmc, EuropePMCClient)
        assert len(sent) == 1, "one batched Europe PMC lookup"

        # Mode resolution went through the real llm registry: this api type
        # reports supports_web_search False, so 'auto' is context mode.
        assert first.mode == "context"
        assert first.status == "ok"
        assert first.resolved_model == "m-1-resolved"
        assert first.usage == {"prompt_tokens": 11, "completion_tokens": 22}

        # The request the REGISTERED api type was handed, through
        # llm.complete -> get_api -> SearchlessApi.complete.
        assert Script.count() == 1
        request = Script.calls[0]
        assert isinstance(request, LLMRequest)
        assert request.json_schema == FACTS_JSON_SCHEMA
        assert request.json_schema["properties"]["facts"]["items"]["properties"][
            "type"
        ]["enum"] == list(FACT_TYPES)
        assert "web_search" not in request.params, "context mode never asks"
        assert "[PMID:15210978]" in request.user
        assert "transformation competent" in request.user

        # ...and the recorded batch's unresolvable DOI never became citable.
        assert first.papers_used.selected == ["PMID:15210978", "PMC:PMC2828762"]
        assert first.papers_used.unresolved == ["DOI:10.1038/nsmb.1583"]
        assert [c.paper_id for c in first.facts[0].citations] == ["PMID:15210978"]

        # The answer is on disk, in the documented table, under its key.
        assert cache_path.exists()
        conn = sqlite3.connect(str(cache_path))
        try:
            rows = conn.execute(
                f'SELECT key, value FROM "{GENE_FACTS_TABLE}"'
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        key, value = rows[0]
        assert key == first.cache_key
        stored = json.loads(value)
        assert stored["status"] == "ok"
        assert "from_cache" not in stored, "from_cache describes the read"

        # A second, independently constructed utils is answered by that row:
        # no model call, and no HTTP beyond the http_cache'd lookup.
        again = GeneFactsUtils(token_file=None, kbase_token_file=None)
        second = again.gene_facts(
            GENE,
            ORGANISM,
            self.SUPPLIED,
            model="m-1",
            api_type=NOSEARCH,
            endpoint={"name": "e1"},
        )
        assert second.from_cache is True
        assert second.cache_key == first.cache_key
        assert [f.statement for f in second.facts] == [
            f.statement for f in first.facts
        ]
        assert Script.count() == 1, "the cached call spent nothing"
        assert len(sent) == 1, "the Europe PMC lookup came from http_cache too"

    def test_default_wiring_sets_web_search_on_a_searching_api_type(
        self, tmp_path, monkeypatch
    ):
        """The same wiring, the other api type: web mode, and the param set."""
        self.wire_home(tmp_path, monkeypatch)
        self.intercept_http(monkeypatch)
        Script.reset(
            answer_with(
                {
                    "type": "function",
                    "statement": "Found live on the web.",
                    "citations": [
                        {"paper_id": "15210978", "evidence_class": "direct"}
                    ],
                    "confidence": "medium",
                }
            )
        )

        utils = GeneFactsUtils(token_file=None, kbase_token_file=None)
        answer = utils.gene_facts(
            GENE,
            ORGANISM,
            self.SUPPLIED,
            model="m-1",
            api_type=SEARCH,
            endpoint={"name": "e1"},
            web_search="required",
        )

        assert answer.mode == "web"
        assert answer.status == "ok"
        assert Script.count() == 1
        assert Script.calls[0].params["web_search"] is True
        assert "PMID, PMCID or DOI only" in Script.calls[0].system
