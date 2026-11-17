"""``go_mf_adjudicate@1`` through the real engine, on a fake backend.

Three items, three outcomes -- unanimity with the critic's approval, a critic
disapproval that sends the item to meta, and a seat naming an id that is not
on the closed menu -- each driven end to end by ``CouncilWorker`` over seats
whose endpoint kind is ``fake``.  The point is the SEAM: the engine counts
votes on the generic shape, and this query type's answers are in an
adjudication vocabulary (``correct_go_ids``, ``prefers``, ``failure_type``,
``rules_cited``) that the engine must never see.

The other half of that seam is what the engine DROPS.  ``prefers``,
``failure_type``, ``rules_cited``, ``correct_term_off_menu`` and
``off_menu_description`` are the research payload the go_mf tuning pass reads,
and the adapter deliberately leaves all of them out of the generic shape.  So
every test here also asserts they survive in ``calls.reply_parsed`` -- the
column that holds the reply in the query type's OWN vocabulary.  Asserted
against ``calls.reply_parsed`` and NOT against ``attempts.reply_text``: the
latter is only written for an attempt that produced no usable answer, so on a
settled ok call there is nothing in it to read.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU: every seat is a ``FakeBackend`` and every answer comes from a script.
The only I/O is a store under ``tmp_path``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest

from kbutillib.domains.ai.council.engine import CouncilWorker
from kbutillib.domains.ai.council.queries import QueryRegistry
from kbutillib.domains.ai.council.types import (
    CouncilTask,
    CouncilVerdict,
    MenuEntry,
)

from .engine_support import (
    InstrumentedBackend,
    endpoint_config,
    make_roster,
    run,
    scripted,
)

WORKER_DEFAULTS: Dict[str, Any] = {
    "heartbeat_interval_s": 0.0,
    "poll_interval_s": 0.01,
    "retry_backoff_s": (0.02, 0.05),
    "retry_jitter": 0.0,
    "idle_grace_s": 0.3,
    "max_idle_s": 15.0,
}

#: The adjudication menu.  Three terms at three levels of specificity, which
#: is what makes policy rules P1-P3 decidable on it: a system activity, the
#: generic binding term, and the generic chemistry of one component.
MENU = [
    MenuEntry(
        id="GO:0015749",
        label="glucose transmembrane transport",
        description="The directed movement of glucose across a membrane.",
        rank=1,
        score=0.88,
    ),
    MenuEntry(
        id="GO:0005524",
        label="ATP binding",
        description="Binding to ATP, adenosine 5'-triphosphate.",
        rank=2,
        score=0.71,
    ),
    MenuEntry(
        id="GO:0016887",
        label="ATP hydrolysis activity",
        description="Catalysis of the reaction ATP + H2O = ADP + phosphate.",
        rank=3,
        score=0.52,
    ),
]

ANNOTATION = "Glucose ABC transporter, ATP-binding protein"
#: Answer A took the component's chemistry (the P1 failure); B took the system.
ANSWER_A = "GO:0005524 | ATP binding\nGO:0016887 | ATP hydrolysis activity"
ANSWER_B = "GO:0015749 | glucose transmembrane transport"

#: The research fields the adapter drops and ``reply_parsed`` must still hold.
PROPOSER_RESEARCH_FIELDS = (
    "prefers",
    "correct_term_off_menu",
    "off_menu_description",
    "failure_type",
    "rules_cited",
)
META_RESEARCH_FIELDS = PROPOSER_RESEARCH_FIELDS


def worker_for(store: Any, backends: Mapping[str, Any], **kwargs: Any) -> CouncilWorker:
    settings = dict(WORKER_DEFAULTS)
    settings.update(kwargs)
    return CouncilWorker(
        store,
        endpoint_config(**{name: {} for name in backends}),
        backends,
        "alice",
        "w-1",
        **settings,
    )


def adjudicate_query(store: Any):
    """Seed the builtins and hand back the adjudication query type."""
    registry = QueryRegistry(store)
    registry.seed_builtins()
    return registry.get("go_mf_adjudicate", "1")


def item(item_id: str = "conflict-1", text: str = ANNOTATION) -> CouncilTask:
    return CouncilTask(
        item_id=item_id,
        evidence={
            "text": text,
            "source": "prokka",
            "answer_a": ANSWER_A,
            "answer_b": ANSWER_B,
        },
        menu=MENU,
    )


def proposer_reply(
    *go_ids: str,
    prefers: str = "B",
    failure_type: str = "too_generic",
    rules: Sequence[str] = ("P1", "P3"),
    confidence: str = "high",
    off_menu: bool = False,
    off_menu_description: str = "none",
    why: str = "P1 maps the named system, not its component's chemistry.",
    notes: str = "none",
) -> Dict[str, Any]:
    """A schema-valid adjudication in the query type's OWN vocabulary."""
    return {
        "correct_go_ids": list(go_ids),
        "prefers": prefers,
        "correct_term_off_menu": off_menu,
        "off_menu_description": off_menu_description,
        "failure_type": failure_type,
        "rules_cited": list(rules),
        "confidence": confidence,
        "why": why,
        "notes": notes,
    }


def critic_reply(
    verdict: str = "consistent",
    reason: str = "The panel cited the rule it decided on.",
    overpicked: Sequence[str] = (),
    missed: Sequence[str] = (),
    contentless: bool = False,
    misapplied: Sequence[str] = (),
    off_menu_missed: bool = False,
    notes: str = "none",
) -> Dict[str, Any]:
    return {
        "verdict": verdict,
        "reason": reason,
        "overpicked": list(overpicked),
        "missed": list(missed),
        "contentless_input": contentless,
        "misapplied_rules": list(misapplied),
        "off_menu_missed": off_menu_missed,
        "notes": notes,
    }


def meta_reply(
    *go_ids: str,
    prefers: str = "B",
    failure_type: str = "too_generic",
    rules: Sequence[str] = ("P1",),
    confidence: str = "medium",
    off_menu: bool = False,
    off_menu_description: str = "none",
    no_content: bool = False,
    reasoning: str = "The panel split on the extra term; P6 settles it.",
    notes: str = "none",
) -> Dict[str, Any]:
    return {
        "final_go_ids": list(go_ids),
        "prefers": prefers,
        "correct_term_off_menu": off_menu,
        "off_menu_description": off_menu_description,
        "failure_type": failure_type,
        "rules_cited": list(rules),
        "final_confidence": confidence,
        "no_functional_content": no_content,
        "reasoning": reasoning,
        "notes": notes,
    }


def fake(script: Any, **kwargs: Any) -> InstrumentedBackend:
    return InstrumentedBackend(scripted(script), **kwargs)


def calls_of(store: Any, job_id: str, role: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = store.get_calls(job_id)
    return [r for r in rows if role is None or r["role"] == role]


def verdict_of(store: Any, job_id: str, item_id: str) -> CouncilVerdict:
    row = next(r for r in store.get_items(job_id) if r["item_id"] == item_id)
    assert row["verdict"] is not None, f"item {item_id} is {row['status']}, not decided"
    return CouncilVerdict.model_validate(row["verdict"])


def assert_research_fields_readable(
    rows: Sequence[Mapping[str, Any]], fields: Sequence[str]
) -> None:
    """Every settled ok call still carries its own vocabulary in the store.

    This is the claim the adapter's deliberate narrowing rests on: dropping a
    field from the generic shape is only acceptable because the tuning pass
    can read it back off ``calls.reply_parsed``.
    """
    assert rows, "no calls to check"
    for row in rows:
        assert row["status"] == "ok", row["status"]
        parsed = row["reply_parsed"]
        assert isinstance(parsed, Mapping), parsed
        for field in fields:
            assert field in parsed, (row["role"], field, sorted(parsed))


class TestOneItemToUnanimity:
    def test_three_adjudicators_agree_and_the_critic_approves(self, store: Any) -> None:
        query = adjudicate_query(store)
        assert query.renderer == "go_mf_adjudicate@1"
        assert query.adapter == "go_mf_adjudicate@1"

        backend = fake(
            {
                "proposer": proposer_reply("GO:0015749"),
                "critic": critic_reply(verdict="strongly_consistent"),
                "meta": meta_reply("GO:0015749"),
            }
        )
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(query, [item()], make_roster(3), label="adjudication")
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id, "conflict-1")
        assert verdict.status == "ok"
        assert verdict.selected_ids == ["GO:0015749"]
        assert verdict.votes == {"GO:0015749": 3}
        assert verdict.decided_by == "unanimous"
        assert verdict.meta_skipped is True
        assert verdict.query == {"name": "go_mf_adjudicate", "version": "1"}
        # No meta call was made at all -- the scripted meta answer is unused.
        assert [row["role"] for row in calls_of(store, job_id)] == [
            "proposer",
            "proposer",
            "proposer",
            "critic",
        ]

        # The engine counted votes on the generic shape it was handed...
        assert [pick.id for pick in verdict.ballots[0].picks] == ["GO:0015749"]
        assert verdict.ballots[0].picks[0].why.startswith("P1 maps")
        # ...and the adjudication's own fields are still in the store.
        assert_research_fields_readable(
            calls_of(store, job_id, "proposer"), PROPOSER_RESEARCH_FIELDS
        )
        proposer_row = calls_of(store, job_id, "proposer")[0]
        assert proposer_row["reply_parsed"]["correct_go_ids"] == ["GO:0015749"]
        assert proposer_row["reply_parsed"]["prefers"] == "B"
        assert proposer_row["reply_parsed"]["rules_cited"] == ["P1", "P3"]
        assert proposer_row["reply_parsed"]["failure_type"] == "too_generic"
        # The critic's two extra audit fields survive the same way.
        critic_row = calls_of(store, job_id, "critic")[0]
        assert critic_row["reply_parsed"]["misapplied_rules"] == []
        assert critic_row["reply_parsed"]["off_menu_missed"] is False

    def test_the_prompts_the_models_received_leak_no_source(self, store: Any) -> None:
        """No source leakage, end to end.

        The adjudicators are shown the two answers with the A/B labels in the
        caller's order and nothing about where either came from.  Naming the
        mapping query type, a machine or a service would hand them exactly the
        provenance the labelling exists to hide.
        """
        query = adjudicate_query(store)
        backend = fake(
            {
                "proposer": proposer_reply("GO:0015749"),
                "critic": critic_reply(verdict="inconsistent"),
                "meta": meta_reply("GO:0015749"),
            }
        )
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(query, [item()], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        roles = {req.role for req in backend.calls}
        assert roles == {"proposer", "critic", "meta"}, "not every seat was asked"
        for req in backend.calls:
            for prompt in (req.system, req.user):
                assert "ALCF" not in prompt
                assert "go_mf" not in prompt
            # The ITEM's own source is shown (it is part of the annotation's
            # provenance and go_mf shows it too)...
            assert "Produced by: prokka" in req.user
            # ...but the two answers carry no producer attribution.
            answer_a = req.user.split("<<<ANSWER A>>>")[1].split("<<<END ANSWER A>>>")[0]
            assert answer_a.strip() == ANSWER_A
            assert "MAPPING POLICY (cite rules by number):" in req.system
            assert "<<<ANNOTATION>>>" in req.user
            assert "<<<MENU>>>" in req.user

        # And the schema each seat was handed is this query type's own.
        proposer_request = next(req for req in backend.calls if req.role == "proposer")
        assert proposer_request.json_schema == query.answer_schema("proposer")
        assert "correct_go_ids" in str(proposer_request.json_schema)


class TestCriticDisapprovalGoesToMeta:
    def test_an_inconsistent_critic_sends_a_unanimous_panel_to_meta(
        self, store: Any
    ) -> None:
        """The critic's verdict, not the vote table, is what calls meta here.

        All three adjudicators preferred answer A's generic term, so the vote
        table is unanimous; the critic's job (its third flagged failure) is to
        say the panel accepted a generic term when a specific supported
        candidate was on the menu.  Meta then issues the final call, and the
        verdict's ``final_confidence`` is meta's, not the ``"high"`` the
        unanimous path would have asserted.
        """
        query = adjudicate_query(store)
        backend = fake(
            {
                "proposer": proposer_reply(
                    "GO:0005524",
                    prefers="A",
                    failure_type="missed_term",
                    rules=("P3",),
                ),
                "critic": critic_reply(
                    verdict="inconsistent",
                    reason="A specific supported candidate was on the menu.",
                    overpicked=["GO:0005524"],
                    missed=["GO:0015749"],
                    misapplied=["P1", "P3"],
                ),
                "meta": meta_reply(
                    "GO:0015749",
                    failure_type="too_generic",
                    rules=("P1", "P3"),
                    confidence="low",
                    reasoning="The annotation names the system; P1 and P3 decide it.",
                ),
            }
        )
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(query, [item("conflict-2")], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id, "conflict-2")
        assert verdict.status == "ok"
        assert verdict.votes == {"GO:0005524": 3}
        assert verdict.decided_by == "meta"
        assert verdict.meta_skipped is False
        # meta OVERRODE the unanimous panel; the rename final_go_ids ->
        # final_ids is what made that readable to the engine.
        assert verdict.selected_ids == ["GO:0015749"]
        assert verdict.final_confidence == "low"
        assert verdict.critic["verdict"] == "inconsistent"

        # The meta prompt was shown the critic's audit.
        meta_request = next(req for req in backend.calls if req.role == "meta")
        assert "Critic audit of the panel:" in meta_request.user
        assert '"verdict": "inconsistent"' in meta_request.user
        assert "Adjudicator votes (GO id : count of 3):" in meta_request.user

        # Every settled ok call keeps its own vocabulary, meta included.
        assert_research_fields_readable(
            calls_of(store, job_id, "proposer"), PROPOSER_RESEARCH_FIELDS
        )
        assert_research_fields_readable(
            calls_of(store, job_id, "meta"), META_RESEARCH_FIELDS
        )
        meta_row = calls_of(store, job_id, "meta")[0]
        assert meta_row["reply_parsed"]["final_go_ids"] == ["GO:0015749"]
        assert meta_row["reply_parsed"]["rules_cited"] == ["P1", "P3"]
        assert meta_row["reply_parsed"]["notes"] == "none"
        # The GENERIC shape the engine read has the renamed key and none of
        # the research fields.
        assert verdict.meta["final_ids"] == ["GO:0015749"]
        assert "final_go_ids" not in verdict.meta
        assert not set(META_RESEARCH_FIELDS) & set(verdict.meta)


class TestAnOffMenuIdIsDropped:
    def test_a_seat_naming_a_term_that_is_not_on_the_menu(self, store: Any) -> None:
        """Policy rule P4 is a REPORT, not a licence to invent an id.

        An adjudicator that believes the correct term is absent is told to say
        so in ``correct_term_off_menu`` / ``off_menu_description``.  One that
        instead writes the absent id into ``correct_go_ids`` has voted for
        something the council cannot select, and the engine drops it from the
        ballot and records an ``off_menu`` note -- without failing the item,
        because the seat's other pick is a real vote.
        """
        query = adjudicate_query(store)
        backend = fake(
            {
                ("proposer", "proposer_1"): proposer_reply(
                    "GO:0015749",
                    "GO:0008643",  # carbohydrate transport -- NOT on this menu
                    off_menu=True,
                    off_menu_description="carbohydrate transport",
                    failure_type="missed_term",
                    rules=("P2", "P4"),
                ),
                ("proposer", "proposer_2"): proposer_reply("GO:0015749"),
                ("proposer", "proposer_3"): proposer_reply("GO:0015749"),
                "critic": critic_reply(verdict="consistent", off_menu_missed=True),
                "meta": meta_reply("GO:0015749"),
            }
        )
        worker = worker_for(store, {"fake1": backend})
        job_id = worker.submit(query, [item("conflict-3")], make_roster(3))
        run(worker.work(job_id=job_id, until_idle=True))

        verdict = verdict_of(store, job_id, "conflict-3")
        assert verdict.status == "ok"
        # The off-menu id never reaches the vote table, so the panel reads as
        # unanimous on the one term it could actually select.
        assert verdict.votes == {"GO:0015749": 3}
        assert verdict.selected_ids == ["GO:0015749"]
        assert verdict.decided_by == "unanimous"
        assert [pick.id for pick in verdict.ballots[0].picks] == ["GO:0015749"]

        off_menu = [e for e in verdict.errors if e["kind"] == "off_menu"]
        assert [(e["stage"], e["seat"]) for e in off_menu] == [
            ("proposer", "proposer_1")
        ]
        assert "GO:0008643" in off_menu[0]["message"]

        # The dropped id and the seat's own off-menu report both survive in
        # the store, which is where the retrieval-failure analysis reads them.
        rows = calls_of(store, job_id, "proposer")
        assert_research_fields_readable(rows, PROPOSER_RESEARCH_FIELDS)
        reporting = next(
            r for r in rows if "GO:0008643" in r["reply_parsed"]["correct_go_ids"]
        )
        assert reporting["reply_parsed"]["correct_term_off_menu"] is True
        assert reporting["reply_parsed"]["off_menu_description"] == (
            "carbohydrate transport"
        )
        assert reporting["reply_parsed"]["rules_cited"] == ["P2", "P4"]


class TestTheAdapterInIsolation:
    """The mapping the three engine tests above exercise, stated directly."""

    @pytest.fixture
    def query(self, store: Any):
        return adjudicate_query(store)

    def test_correct_go_ids_become_generic_picks(self, query) -> None:
        generic = query.postprocess(
            "proposer", proposer_reply("GO:0015749", "GO:0005524", confidence="medium")
        )
        assert [pick["id"] for pick in generic["picked"]] == [
            "GO:0015749",
            "GO:0005524",
        ]
        assert {pick["confidence"] for pick in generic["picked"]} == {"medium"}
        assert generic["overall_confidence"] == "medium"
        assert not set(PROPOSER_RESEARCH_FIELDS) & set(generic)

    def test_a_repeated_id_is_one_vote(self, query) -> None:
        generic = query.postprocess(
            "proposer", proposer_reply("GO:0015749", "GO:0015749")
        )
        assert [pick["id"] for pick in generic["picked"]] == ["GO:0015749"]

    def test_an_empty_adjudication_is_a_vote_for_nothing(self, query) -> None:
        """P4 and P5 both produce an empty ``correct_go_ids`` legitimately."""
        generic = query.postprocess(
            "proposer",
            proposer_reply(off_menu=True, off_menu_description="glucose import"),
        )
        assert generic["picked"] == []
        assert generic["no_functional_content"] is False

    def test_the_content_flag_is_read_off_the_contentless_failure_type(
        self, query
    ) -> None:
        """The adjudicate proposer has no content flag; this derives one.

        Both clauses have to hold.  An adjudicator naming
        ``contentless_assigned`` while still listing correct ids has
        contradicted itself, and reading "contentless" off half of that would
        skip the whole council for the item.
        """
        contentless = query.postprocess(
            "proposer", proposer_reply(failure_type="contentless_assigned")
        )
        assert contentless["no_functional_content"] is True

        contradictory = query.postprocess(
            "proposer",
            proposer_reply("GO:0015749", failure_type="contentless_assigned"),
        )
        assert contradictory["no_functional_content"] is False

    def test_the_critic_keeps_the_five_generic_fields_and_drops_its_extras(
        self, query
    ) -> None:
        generic = query.postprocess(
            "critic",
            critic_reply(
                verdict="ambiguous",
                overpicked=["GO:0005524"],
                missed=["GO:0015749"],
                contentless=True,
                misapplied=["P3"],
                off_menu_missed=True,
            ),
        )
        assert generic == {
            "verdict": "ambiguous",
            "reason": "The panel cited the rule it decided on.",
            "overpicked": ["GO:0005524"],
            "missed": ["GO:0015749"],
            "contentless_input": True,
        }

    def test_meta_renames_final_go_ids(self, query) -> None:
        generic = query.postprocess(
            "meta", meta_reply("GO:0015749", confidence="high", no_content=False)
        )
        assert generic["final_ids"] == ["GO:0015749"]
        assert generic["final_confidence"] == "high"
        assert generic["no_functional_content"] is False
        assert "final_go_ids" not in generic

    def test_an_unknown_role_is_refused(self, query) -> None:
        from kbutillib.domains.ai.council.queries.errors import QuerySpecError

        with pytest.raises(QuerySpecError):
            query.postprocess("referee", {})
