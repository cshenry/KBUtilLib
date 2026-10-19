"""ontology_mapping: function_to_go (ranked GO candidates) and function_to_go_council.

These run against the REAL vendored go-text models and GO metadata, reached
through ``KBUtilLib()`` with nothing injected -- the production path. Only the
council's LLM calls are faked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("sklearn")
pytest.importorskip("joblib")

from kbutillib import KBUtilLib  # noqa: E402
from kbutillib.core.capability import collect_capabilities  # noqa: E402
from kbutillib.domains.genome.ontology_mapping import go_text  # noqa: E402
from kbutillib.domains.genome.ontology_mapping.placeholder import placeholder_reason  # noqa: E402

FIXTURE = Path(__file__).parent / "ai" / "council" / "fixtures" / "go_v3_replay_10.json"


@pytest.fixture(scope="module")
def om():
    return KBUtilLib().ontology_mapping


def test_facade_registers_both_capabilities(om):
    names = {s.name for s in collect_capabilities(om)}
    assert {"ontology_mapping.function_to_go", "ontology_mapping.function_to_go_council"} <= names
    assert om.available, om.unavailable_reason


def test_function_to_go_through_facade_ranks_real_go_terms(om):
    out = om.function_to_go(["ATP synthase subunit alpha", "DNA-directed RNA polymerase subunit beta"])
    tops = [r["candidates"][0]["go_id"] for r in out["results"]]
    assert tops == ["GO:0046933", "GO:0003899"]
    for r in out["results"]:
        assert not r["abstained"]
        assert 0 < len(r["candidates"]) <= go_text.UNION_MAX_CANDIDATES
        assert [c["rank"] for c in r["candidates"]] == list(range(1, len(r["candidates"]) + 1))
        assert all(c["height"] <= go_text.MAX_HEIGHT and c["name"] for c in r["candidates"])


def test_mapping_version_is_stable_and_names_the_backend(om):
    a = om.function_to_go(["glucokinase"])
    b = om.function_to_go({"x": "glucokinase"})
    assert a["mapping_hash"] == b["mapping_hash"] and len(a["mapping_hash"]) == 64
    assert "go-text 2.0.0+4.0.0" in a["mapping_version"]
    assert a["provenance"]["go_release"] == "releases/2026-07-26"


def test_input_shapes_preserve_ids_and_order(om):
    out = om.function_to_go([{"id": "g2", "text": "glucokinase", "source": "RAST"},
                             {"query_id": "g1", "text": "hypothetical protein"}])
    assert [r["query_id"] for r in out["results"]] == ["g2", "g1"]
    assert out["results"][0]["source"] == "RAST"
    with pytest.raises(ValueError):
        om.function_to_go([{"id": "a", "text": "x"}, {"id": "a", "text": "y"}])


@pytest.mark.parametrize("text", [
    "hypothetical protein", "conserved hypothetical protein", "Uncharacterized protein YdhK",
    "DUF1234 domain-containing protein", "UPF0298 protein", "Protein of unknown function (DUF2442)",
    "K02313",
])
def test_placeholders_abstain(om, text):
    assert placeholder_reason(text)
    r = om.function_to_go([text])["results"][0]
    assert r["abstained"] and r["candidates"] == [] and r["abstain_reason"]


@pytest.mark.parametrize("text", [
    "ATP synthase subunit alpha", "DUF1234 domain-containing ABC transporter permease",
    "VOG02491 | K02313 | chromosomal replication initiator protein", "phage protein", "transporter",
])
def test_real_functions_do_not_abstain(text):
    assert placeholder_reason(text) is None


def test_interleave_is_union_candidates_py():
    a = [{"go_id": g, "rank": i} for i, g in enumerate(["A", "B", "C"], 1)]
    b = [{"go_id": g, "rank": i} for i, g in enumerate(["B", "D"], 1)]
    assert [c["go_id"] for c in go_text.interleave([a, b])] == ["A", "B", "D", "C"]
    assert [c["go_id"] for c in go_text.interleave([a, b], max_candidates=2)] == ["A", "B"]


def test_retrieval_matches_the_council_inputs_where_they_were_not_pooled(om):
    """Two of the ten replay rows came from a protein with one annotation string,
    so they were never pooled; those must reproduce the council's menu exactly.
    (The other eight carry candidates pooled from sibling annotation strings,
    which per-string retrieval cannot see -- their per-string head must agree.)"""
    rows = json.loads(FIXTURE.read_text())["rows"]
    inputs = {str(r["input"]["input_id"]): r["input"] for r in rows}
    out = om.function_to_go([{"id": k, "text": v["text"]} for k, v in inputs.items()])
    got = {r["query_id"]: [c["go_id"] for c in r["candidates"]] for r in out["results"]}
    for qid in ("1465", "1497"):
        assert got[qid] == [c["go_id"] for c in inputs[qid]["candidates"]]
    for qid, inp in inputs.items():
        assert got[qid][0] == inp["candidates"][0]["go_id"]


class _FakeCouncil:
    def __init__(self):
        self.submitted = None

    def submit(self, query, tasks, **kw):
        self.submitted = (query, tasks, kw)
        return "job-1"

    def run(self, job_id):
        return {"job_id": job_id, "status": "done", "blockers": [], "worker": {"calls": 3}}

    def results(self, job_id):
        return [{"item_id": t["item_id"],
                 "verdict": {"selected_ids": [t["menu"][0]["id"]], "final_confidence": "high",
                             "decided_by": "unanimous", "decline_reason": None}}
                for t in self.submitted[1]]


def test_council_stage_dedups_skips_abstained_and_fans_back_out(om, monkeypatch):
    fake = _FakeCouncil()
    monkeypatch.setattr(type(om._delegate), "_council", lambda self: fake)
    ranked = om.function_to_go([{"id": "a", "text": "glucokinase"}, {"id": "b", "text": "glucokinase"},
                                {"id": "c", "text": "hypothetical protein"}])
    out = om.function_to_go_council(ranked, profile="smoke", allow_unverified=True)
    query, tasks, kw = fake.submitted
    assert query == "go_mf@1" and len(tasks) == 1 and kw["profile"] == "smoke"
    assert tasks[0]["menu"][0]["id"] == ranked["results"][0]["candidates"][0]["go_id"]
    by = {r["query_id"]: r for r in out["results"]}
    assert by["a"]["selected_go_ids"] == by["b"]["selected_go_ids"] == [tasks[0]["menu"][0]["id"]]
    assert by["a"]["names"][by["a"]["selected_go_ids"][0]]
    assert by["c"]["selected_go_ids"] == [] and by["c"]["decline_reason"] == "abstained"
    assert out["mapping_hash"] == ranked["mapping_hash"] and out["council"]["job_id"] == "job-1"
