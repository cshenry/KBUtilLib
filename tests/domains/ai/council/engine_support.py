"""Shared scaffolding for the engine tests: a roster, a query type, a loop.

Kept out of the test module so the decision-rule tests and the worker tests
build the SAME council -- a worker test that silently used a different roster
or a different query type would not be testing the rule the decision-rule
tests pinned.

Nothing here makes a network call, spends a token, needs a CLI login or needs
a GPU: every seat is served by a ``FakeBackend`` over a ``fake`` endpoint.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from kbutillib.domains.ai.council.queries import QueryType
from kbutillib.domains.ai.council.types import (
    CouncilTask,
    MenuEntry,
    Roster,
    SeatSpec,
)

__all__ = [
    "MENU",
    "endpoint_config",
    "fake_seat",
    "make_query",
    "make_roster",
    "make_task",
    "menu",
    "run",
    "scripted",
]

#: The three ids every engine test picks from, in MENU ORDER.  Deliberately
#: NOT in alphabetical or id order: a verdict that reported picks in the order
#: the proposers happened to name them would pass an alphabetical menu by
#: accident.
MENU: Sequence[Sequence[str]] = (
    ("C:3", "carbon thing"),
    ("A:1", "alpha thing"),
    ("B:2", "beta thing"),
)


def run(coro: Any) -> Any:
    """Run one coroutine to completion on a loop owned by this call.

    Deliberately NOT ``asyncio.run``: that sets the thread's current event
    loop to ``None`` on the way out, after which the deprecated
    ``asyncio.get_event_loop()`` raises instead of creating a loop.  Other
    modules in this suite still call it, and ``tests/domains`` collects first,
    so ``asyncio.run`` here breaks them from a distance.  Same reasoning (and
    same helper) as ``test_types_config.run``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def menu(*ids: str) -> List[MenuEntry]:
    """Build a menu from :data:`MENU`, or from arbitrary ids."""
    if not ids:
        return [
            MenuEntry(id=entry_id, label=label, rank=rank)
            for rank, (entry_id, label) in enumerate(MENU, start=1)
        ]
    return [
        MenuEntry(id=entry_id, label=f"{entry_id} thing", rank=rank)
        for rank, entry_id in enumerate(ids, start=1)
    ]


def make_task(item_id: str = "item-1", text: str = "alpha synthase", **kwargs: Any) -> CouncilTask:
    """One item: evidence plus the closed menu."""
    entries = kwargs.pop("menu", None)
    return CouncilTask(
        item_id=item_id,
        evidence={"text": text, "source": "test"},
        menu=menu() if entries is None else entries,
        **kwargs,
    )


def fake_seat(seat_name: str, model: str, endpoint: str = "fake1", **kwargs: Any) -> SeatSpec:
    """A seat on a ``fake`` endpoint, i.e. one no call can be billed to."""
    return SeatSpec(
        seat_name=seat_name,
        endpoint=endpoint,
        endpoint_kind="fake",
        model=model,
        canonical_model=f"test/{model}",
        max_output_tokens=256,
        temperature=0,
        timeout_s=5,
        **kwargs,
    )


def make_roster(n_proposers: int = 3, **kwargs: Any) -> Roster:
    """A valid roster of *n_proposers* independent proposers plus critic/meta."""
    endpoint = kwargs.pop("endpoint", "fake1")
    return Roster(
        proposers=[
            fake_seat(f"proposer_{i}", f"p{i}", endpoint=endpoint)
            for i in range(1, n_proposers + 1)
        ],
        critic=fake_seat("critic", "critic-model", endpoint=endpoint),
        meta=fake_seat("meta", "meta-model", endpoint=endpoint),
        **kwargs,
    )


#: The templates the in-test query type renders with.  Short on purpose: every
#: worker test that asserts on a prompt asserts on these exact words, and the
#: candidate block is what proves the menu reached the model.
_PROPOSER_USER = (
    "Annotation: $text (from $source)\nCandidates:\n$candidates_short\nPick ids."
)
_CRITIC_USER = "Votes:\n$votes\nBallots:\n$ballots\nAudit the table."
_META_USER = "Votes:\n$votes\nBallots:\n$ballots\nCritic: $critic_json\nDecide."


def make_query(name: str = "engine_test", version: str = "1") -> QueryType:
    """A template-rendered query type with GENERIC field names.

    ``id_field`` is the degenerate ``id``, so ``postprocess`` is the identity
    and a worker test's script can be written in the shape the engine counts
    -- which is what keeps the decision-rule tests and the worker tests
    talking about the same answers.
    """
    from kbutillib.domains.ai.council.queries.spec import generic_schema_for

    return QueryType(
        name=name,
        version=version,
        description="an in-test query type",
        entity_field="text",
        evidence_fields=["text", "source"],
        menu_fields=[],
        id_field="id",
        candidate_templates={
            "full": "  $rank | $id | $label",
            "short": "  $rank | $id",
        },
        roles={
            "proposer": {
                "system": "You are a proposer.",
                "user": _PROPOSER_USER,
                "schema": generic_schema_for("proposer", "id"),
            },
            "critic": {
                "system": "You are the critic.",
                "user": _CRITIC_USER,
                "schema": generic_schema_for("critic", "id"),
            },
            "meta": {
                "system": "You are meta.",
                "user": _META_USER,
                "schema": generic_schema_for("meta", "id"),
            },
        },
    )


def endpoint_config(**endpoints: Mapping[str, Any]) -> Dict[str, Any]:
    """A worker config holding only endpoint entries.

    ``CouncilConfig`` cannot express a ``fake`` endpoint (its ``kind`` is a
    Literal over the four real ones), so the engine tests hand the worker the
    mapping form of the same information.  ``CouncilWorker`` accepts either.
    """
    defaults = {"kind": "fake", "max_concurrency": 4}
    return {
        "endpoints": {
            name: {**defaults, **dict(cfg)} for name, cfg in endpoints.items()
        }
    }


def proposer_answer(
    *ids: str,
    confidence: str = "high",
    overall: str = "high",
    no_content: bool = False,
    notes: str = "",
) -> Dict[str, Any]:
    """A schema-valid proposer reply picking *ids*."""
    return {
        "picked": [
            {"id": entry_id, "confidence": confidence, "why": f"because {entry_id}"}
            for entry_id in ids
        ],
        "overall_confidence": overall,
        "no_functional_content": no_content,
        "notes": notes,
    }


def critic_answer(
    verdict: str = "consistent",
    overpicked: Sequence[str] = (),
    missed: Sequence[str] = (),
    contentless: bool = False,
    reason: str = "looks right",
) -> Dict[str, Any]:
    """A schema-valid critic reply."""
    return {
        "verdict": verdict,
        "reason": reason,
        "overpicked": list(overpicked),
        "missed": list(missed),
        "contentless_input": contentless,
    }


def meta_answer(
    *ids: str,
    confidence: str = "medium",
    no_content: bool = False,
    reasoning: str = "adjudicated",
) -> Dict[str, Any]:
    """A schema-valid meta reply."""
    return {
        "final_ids": list(ids),
        "final_confidence": confidence,
        "no_functional_content": no_content,
        "reasoning": reasoning,
    }


def scripted(
    table: Mapping[Any, Any],
    default: Optional[Callable[[Any], Any]] = None,
    recorder: Optional[List[Any]] = None,
) -> Callable[[Any], Any]:
    """A ``FakeBackend`` script driven by a ``(role, seat_name)`` table.

    A table value may be a dict (one answer however often the seat is asked),
    a list (one answer per call, in order -- which is how "fail then succeed"
    is scripted), or a callable taking the request.
    """
    counters: Dict[Any, int] = {}

    def _script(req: Any) -> Any:
        key = (req.role, req.seat.seat_name)
        if recorder is not None:
            recorder.append(req)
        answer = table.get(key, table.get(req.role))
        if answer is None:
            if default is None:
                raise AssertionError(f"no scripted answer for {key}")
            return default(req)
        if isinstance(answer, list):
            index = counters.get(key, 0)
            counters[key] = index + 1
            answer = answer[min(index, len(answer) - 1)]
        if callable(answer):
            return answer(req)
        return answer

    return _script


def as_text(parsed: Mapping[str, Any]) -> str:
    """The JSON a backend would have returned for *parsed*."""
    return json.dumps(parsed, sort_keys=True)
