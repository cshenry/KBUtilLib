"""Synthetic ``go_mf`` runs for the held-out scorer's tests.

The scorer reads two FINISHED stores and does arithmetic on them.  So the
fixtures here build those stores through the store's own write APIs
(``create_job``/``settle_item``), with a known flip structure, rather than
running a council -- a scored comparison whose answers came from a fake
backend would make every expected number a function of the fake's script
instead of of the metric under test.  ``test_go_tuning_run`` is where a real
council actually runs, because freshness is a property of the ENGINE and
cannot be faked at this level.

ONE STORE PER RUN, always, even in a test (C14).  Two ``go_mf@4`` jobs in one
store is the exact mistake :mod:`..go_tuning.run` exists to prevent, and a
fixture that did it would teach the suite the wrong shape.

Nothing here makes a network call, spends a token or needs a GPU.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from kbutillib.domains.ai.council.store.sqlite import SqliteStore

from .go_tuning_support import hash_of
from .store_contract import call_row, item_row, job_row

#: Four menu terms, enough for a disjoint pair and a partial overlap.
MENU: Tuple[Dict[str, Any], ...] = (
    {
        "id": "GO:0015749",
        "label": "glucose transmembrane transport",
        "description": "The directed movement of glucose across a membrane.",
        "rank": 1,
        "score": 0.91,
        "metadata": {},
    },
    {
        "id": "GO:0005524",
        "label": "ATP binding",
        "description": "Binding to ATP.",
        "rank": 2,
        "score": 0.74,
        "metadata": {},
    },
    {
        "id": "GO:0016887",
        "label": "ATP hydrolysis activity",
        "description": "Catalysis of ATP + H2O = ADP + phosphate.",
        "rank": 3,
        "score": 0.55,
        "metadata": {},
    },
    {
        "id": "GO:0003924",
        "label": "GTPase activity",
        "description": "Catalysis of GTP + H2O = GDP + phosphate.",
        "rank": 4,
        "score": 0.31,
        "metadata": {},
    },
)

GO_MF4 = ("go_mf", "4")


def evidence_for(item_id: str) -> Dict[str, Any]:
    """The frozen evidence for an item.  The SAME in every run of it.

    C15 refuses to score two runs whose evidence or menu hashes differ for an
    item, so a fixture that varied this per run would be testing the refusal
    rather than the metric.
    """
    return {"text": f"ABC transporter, ATP-binding protein {item_id}", "source": "prokka"}


def entry(
    item_id: str,
    pool: str,
    *,
    entity: Optional[str] = None,
    swap: bool = False,
    kind: str = "disjoint",
) -> Dict[str, Any]:
    """One manifest entry, in the shape :func:`..sample._entry` writes.

    ``entity`` defaults to the item id, which makes every item its own entity
    group; pass a shared label to make two items ONE group, which is what the
    bootstrap has to resample together.
    """
    label = entity or item_id
    return {
        "id": f"{item_id}:swap" if swap else item_id,
        "item_id": item_id,
        "entity_hash": hash_of("entity", label),
        "data_hash": hash_of("data", item_id),
        "class": kind,
        "sub_kind": None,
        "source": "prokka",
        "pool": pool,
        "alcf_is_A": True,
        "swap": swap,
        "candidate_job_id": "j-cand",
        "baseline_item_id": f"b-{item_id}",
        "baseline_job_id": "j-base",
    }


def manifest_of(entries: Sequence[Mapping[str, Any]], manifest_id: str = "m-test") -> Dict[str, Any]:
    """A manifest mapping over *entries*.  Only what the scorer reads."""
    return {
        "manifest_id": manifest_id,
        "seed": "seed-test",
        "plan": {},
        "code_version": {},
        "entries": [dict(e) for e in entries],
        "execution_order": {},
        "report": {},
    }


def run_store(
    path: Any,
    job_id: str,
    answers: Mapping[str, Any],
    *,
    query: Tuple[str, str] = GO_MF4,
    menu: Optional[Sequence[Mapping[str, Any]]] = None,
) -> str:
    """Build ONE store holding ONE finished ``go_mf`` job, and return its URL.

    ``answers`` maps an item id to either

    * a sequence of GO ids -- settled ``ok`` with that selected set (an empty
      sequence is a DECLINE, which is what C15 defines one as),
    * ``None`` -- settled ``ok`` with NO verdict row, which is not a
      measurement and must not read as a decline, or
    * a mapping ``{"selected", "status", "with_verdict", "menu",
      "evidence"}`` for the awkward cases: an ``error`` item, or an item
      deliberately asked a DIFFERENT question so the comparability refusal
      can be exercised.
    """
    specs: Dict[str, Dict[str, Any]] = {}
    for item_id, value in answers.items():
        if isinstance(value, Mapping):
            spec = dict(value)
        elif value is None:
            spec = {"selected": [], "with_verdict": False}
        else:
            spec = {"selected": list(value)}
        spec.setdefault("status", "ok")
        spec.setdefault("with_verdict", True)
        spec.setdefault("selected", [])
        spec.setdefault("menu", list(menu or MENU))
        spec.setdefault("evidence", evidence_for(item_id))
        specs[str(item_id)] = spec

    url = f"sqlite:///{path}"
    store = SqliteStore(url)
    try:
        store.init_schema()
        items = [
            item_row(
                item_id,
                entity_hash=hash_of("entity", item_id),
                data_hash=hash_of("data", item_id),
                evidence=spec["evidence"],
                menu=spec["menu"],
            )
            for item_id, spec in specs.items()
        ]
        calls = [
            call_row(item_id, "p1", entity_hash=hash_of("entity", item_id))
            for item_id in specs
        ]
        created = store.create_job(
            job_row(job_id=job_id, query_name=query[0], query_version=query[1]),
            items,
            calls,
        )
        for item_id, spec in specs.items():
            if spec["status"] == "pending":
                continue
            verdict = None
            if spec["with_verdict"]:
                verdict = {
                    "status": "ok",
                    "item_id": item_id,
                    "selected_ids": list(spec["selected"]),
                    "final_confidence": "high",
                    "decided_by": "unanimous",
                }
            store.settle_item(created, item_id, spec["status"], verdict)
    finally:
        store.close()
    return url


def gold(
    ids: Iterable[str],
    *,
    off_menu: bool = False,
    provenance: str = "judge",
    mark: str = "R",
) -> Dict[str, Any]:
    """One gold record, in the shape :class:`..table.GoldRecord` serialises to."""
    return {
        "ids": list(ids),
        "off_menu": off_menu,
        "provenance": provenance,
        "mark": mark,
    }


def human(ids: Iterable[str], **kwargs: Any) -> Dict[str, Any]:
    """Gold Chris marked himself -- the only kind the pilot audit accepts."""
    kwargs.setdefault("mark", "W")
    return gold(ids, provenance="human", **kwargs)


# ---------------------------------------------------------------------------
# a corpus with a KNOWN flip structure
# ---------------------------------------------------------------------------
#
# 10 heldout items in FIVE entity groups of two, 6 regression items and 6
# decline-guard items, with gold on every heldout row.
#
# :data:`CANDIDATE_RIGHT` gets 8 of the 10 right and :data:`BASELINE_RIGHT`
# gets 4, so the primaries are 0.8 and 0.4 and the gain is 0.4 -- arithmetic a
# reader can check by hand.  The eight are spread so that FOUR of the five
# entity groups improve and the fifth is level, which is what makes the
# bootstrap lower bound positive on a corpus this small: a resample can only
# produce a gain of zero by drawing the level group five times out of five
# (1 in 3,125), so the 2.5th percentile sits above zero.  Concentrate the same
# gain in fewer groups and it does not, which is the honest behaviour -- and
# the reason the grouping is part of the fixture rather than an afterthought.

HELDOUT = tuple(f"h{i:02d}" for i in range(10))
REGRESSION = tuple(f"r{i:02d}" for i in range(6))
DECLINE_GUARD = tuple(f"d{i:02d}" for i in range(6))
PILOT = tuple(f"p{i:02d}" for i in range(4))

#: ``item_id -> entity label``: five groups of two, in heldout order.
HELDOUT_ENTITY = {item_id: f"e{index // 2:02d}" for index, item_id in enumerate(HELDOUT)}

#: The right answer for each heldout and pilot item.
TRUTH = {item_id: ["GO:0005524"] for item_id in HELDOUT + PILOT}
#: The uncontested answer on every regression item.
AGREED = {item_id: ["GO:0015749"] for item_id in REGRESSION}

#: A wrong but ON-MENU answer: an exact-match miss, not an off-menu gold row.
WRONG = ["GO:0016887"]
#: A second wrong answer, for a run that must DIFFER from another wrong run
#: without being any more right.
ALSO_WRONG = ["GO:0003924"]

#: Eight right, over four improving groups and one level one.
CANDIDATE_RIGHT = ("h00", "h01", "h02", "h03", "h04", "h05", "h06", "h08")
#: Four right: one in each of the first four groups, none in the fifth.
BASELINE_RIGHT = ("h00", "h02", "h04", "h06")


def corpus_entries(*, pilot: bool = True) -> List[Dict[str, Any]]:
    """Manifest entries for the four pools.

    Heldout items are paired into five entity groups; everything else is its
    own group, which is what the guard slices are (one menu per key).
    """
    entries = [
        entry(item_id, "heldout", entity=HELDOUT_ENTITY[item_id]) for item_id in HELDOUT
    ]
    entries += [entry(item_id, "regression", kind="agree_assigned") for item_id in REGRESSION]
    entries += [entry(item_id, "decline_guard") for item_id in DECLINE_GUARD]
    if pilot:
        entries += [entry(item_id, "pilot") for item_id in PILOT]
    return entries


def corpus_gold(
    *,
    provenance: str = "judge",
    off_menu: Sequence[str] = (),
    pools: Sequence[str] = ("heldout",),
) -> Dict[str, Any]:
    """Gold over the named *pools*, with *off_menu* rows flagged."""
    item_ids: List[str] = []
    if "heldout" in pools:
        item_ids += list(HELDOUT)
    if "pilot" in pools:
        item_ids += list(PILOT)
    return {
        item_id: gold(
            TRUTH[item_id], off_menu=item_id in set(off_menu), provenance=provenance
        )
        for item_id in item_ids
    }


def answers(
    right: Iterable[str] = (),
    *,
    declines: int = 0,
    regression_flips: int = 0,
    pilot_right: Iterable[str] = (),
    wrong: Sequence[str] = WRONG,
) -> Dict[str, Any]:
    """One run's answers over the whole corpus.

    Args:
        right: the heldout items that get the gold set exactly right.  The
            rest get *wrong* -- an on-menu set that is not the gold, i.e. an
            exact-match miss.
        declines: how many of the six decline-guard items come back ``ok``
            with an EMPTY selected set, which is what C15 defines a decline
            as.
        regression_flips: how many of the six regression items answer
            something other than the agreed set.
        pilot_right: the pilot items that get the gold set right.
        wrong: the wrong-but-on-menu answer to use.
    """
    right_set, pilot_set = set(right), set(pilot_right)
    out: Dict[str, Any] = {}
    for item_id in HELDOUT:
        out[item_id] = list(TRUTH[item_id]) if item_id in right_set else list(wrong)
    for index, item_id in enumerate(REGRESSION):
        out[item_id] = list(ALSO_WRONG) if index < regression_flips else list(AGREED[item_id])
    for index, item_id in enumerate(DECLINE_GUARD):
        out[item_id] = [] if index < declines else ["GO:0015749"]
    for item_id in PILOT:
        out[item_id] = list(TRUTH[item_id]) if item_id in pilot_set else list(wrong)
    return out
