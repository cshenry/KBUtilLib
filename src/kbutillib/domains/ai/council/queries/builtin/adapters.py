"""Builtin answer adapters: reference reply shapes -> the generic shapes.

An adapter is the other half of a builtin renderer.  The renderer asks the
reference's question; the adapter turns the reference's answer into the three
shapes the engine counts votes on, so the engine never learns a domain's field
names.  A query type with neither gets the plain rename in
:func:`..spec.generic_postprocess`.

The two shipped adapters are very different in weight, and that is the point:

* ``go_mf_v3`` is the rename and nothing else.  CONCORDIA's GO council already
  answers in the generic shape with ``go_id`` for ``id`` and ``final_go_ids``
  for ``final_ids``, which is why the generic shapes look the way they do.
* ``reaction_pickup_v4`` is a real translation.  The pickup council answers in
  three shapes that share no field name with the generic ones, and its critic
  does not emit a verdict at all -- it emits a per-reaction table that a
  verdict has to be DERIVED from.  That derivation is the approval rule below.

Provenance for the reply shapes these read is recorded in
:mod:`.renderers` (same files, same SHA-256s, same line ranges).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ...types import InvalidOutput
from ..errors import QuerySpecError
from ..spec import generic_postprocess

__all__ = [
    "go_mf_v3",
    "reaction_pickup_v4",
    "pickup_critic_approved",
    "PICKUP_MAX_PICKS",
    "CONSISTENT_VERDICTS",
]

#: ``PickupOutput.picked_reactions`` is capped at 15 in pickup_schema.py.  The
#: 5-pick cap (12 for a promiscuous EC cluster) that the proposer prompt
#: states is an INSTRUCTION and is deliberately not enforced here -- enforcing
#: it would silently discard the extra picks of exactly the promiscuous cases
#: the V4 prompt exists to catch.
PICKUP_MAX_PICKS = 15

#: The two critic verdicts that count as the council having got it right.
CONSISTENT_VERDICTS = frozenset({"consistent", "strongly_consistent"})


def go_mf_v3(spec: Any, role: str, parsed: Mapping[str, Any]) -> Dict[str, Any]:
    """GO: rename ``go_id`` -> ``id`` and ``final_go_ids`` -> ``final_ids``.

    Named as a builtin rather than left to the default so that ``go_mf``'s
    stored spec states which adapter decided its answers, and so an
    installation without this adapter version refuses the query instead of
    quietly applying whatever the default has become.
    """
    return generic_postprocess(spec, role, parsed)


# ---------------------------------------------------------------------------
# Reaction pickup
# ---------------------------------------------------------------------------


def _critique_rows(parsed: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The critic's per-reaction table, normalised."""
    rows: List[Dict[str, Any]] = []
    for item in parsed.get("per_reaction_critique") or ():
        if not isinstance(item, Mapping):
            continue
        try:
            votes = int(item.get("council_vote_count", 0))
        except (TypeError, ValueError):
            votes = 0
        rows.append(
            {
                "rxn_id": str(item.get("rxn_id", "") or "").strip(),
                "votes": votes,
                "verdict": str(item.get("critic_verdict", "") or "").strip().lower(),
            }
        )
    return [row for row in rows if row["rxn_id"]]


def _missed_ids(parsed: Mapping[str, Any]) -> List[str]:
    """The ids the critic listed under ``missed_picks``, in order."""
    out: List[str] = []
    for item in parsed.get("missed_picks") or ():
        if not isinstance(item, Mapping):
            continue
        rid = str(item.get("rxn_id", "") or "").strip()
        if rid and rid not in out:
            out.append(rid)
    return out


def pickup_critic_approved(parsed: Mapping[str, Any]) -> bool:
    """Did the pickup critic approve the council's set?

    The reference critic never says yes or no; it rates every candidate and
    lists what it thinks was missed.  Chris's rule (adopted 2026-10-10) reads
    an approval off that table, and all three clauses have to hold:

    1. every reaction with at least one vote is rated ``consistent`` or
       ``strongly_consistent``,
    2. no reaction with zero votes is rated either of those -- a zero-vote
       reaction the critic thinks is right is a reaction the council missed,
       whether or not the critic also listed it, and
    3. ``missed_picks`` is empty.

    Clause 1 is judged over the rows the critic actually returned.  A voted
    reaction the critic omitted entirely is not evidence of approval, but it
    is also not a rating, and inventing one here would be the adapter making
    the critic's judgement for it.
    """
    rows = _critique_rows(parsed)
    if any(row["votes"] >= 1 and row["verdict"] not in CONSISTENT_VERDICTS for row in rows):
        return False
    if any(row["votes"] < 1 and row["verdict"] in CONSISTENT_VERDICTS for row in rows):
        return False
    return not _missed_ids(parsed)


def _pickup_critic(parsed: Mapping[str, Any]) -> Dict[str, Any]:
    """Map the pickup critic's table onto the generic critic shape."""
    rows = _critique_rows(parsed)
    overpicked = [
        row["rxn_id"]
        for row in rows
        if row["votes"] >= 1 and row["verdict"] not in CONSISTENT_VERDICTS
    ]
    zero_vote_endorsed = [
        row["rxn_id"]
        for row in rows
        if row["votes"] < 1 and row["verdict"] in CONSISTENT_VERDICTS
    ]
    missed = _missed_ids(parsed)
    for rid in zero_vote_endorsed:
        if rid not in missed:
            missed.append(rid)
    return {
        "verdict": "consistent" if pickup_critic_approved(parsed) else "inconsistent",
        "reason": str(parsed.get("overall_note", "") or ""),
        "overpicked": overpicked,
        "missed": missed,
        # The pickup task has no contentless input: an annotation with no
        # functional content yields no candidates, so there is no menu and no
        # item.  The reference asks nobody about it, so nothing is inferred.
        "contentless_input": False,
        # Kept so the meta prompt can still be rendered with the reference's
        # structured critic block (pickup_meta._render_critic_block), which
        # reads per_reaction_critique / missed_picks / overall_note.
        "raw": dict(parsed),
    }


def reaction_pickup_v4(spec: Any, role: str, parsed: Mapping[str, Any]) -> Dict[str, Any]:
    """Map one reaction-pickup reply onto the generic shape for *role*."""
    parsed = dict(parsed or {})

    if role == "proposer":
        raw_picks = list(parsed.get("picked_reactions") or ())
        if len(raw_picks) > PICKUP_MAX_PICKS:
            raise InvalidOutput(
                f"reaction_pickup proposer returned {len(raw_picks)} picks; the "
                f"answer schema allows at most {PICKUP_MAX_PICKS}"
            )
        picked: List[Dict[str, Any]] = []
        seen = set()
        for pick in raw_picks:
            if not isinstance(pick, Mapping):
                continue
            rid = str(pick.get("rxn_id", "") or "").strip()
            # First wins: the duplicate carries a second rationale for a
            # reaction already picked, and dropping the FIRST would discard
            # the one the model led with.
            if not rid or rid in seen:
                continue
            seen.add(rid)
            picked.append(
                {
                    "id": rid,
                    "confidence": str(pick.get("confidence", "") or "medium"),
                    "why": str(pick.get("rationale", "") or ""),
                }
            )
        return {
            "picked": picked,
            "overall_confidence": parsed.get("overall_confidence"),
            "no_functional_content": False,
            "notes": str(parsed.get("notes", "") or ""),
        }

    if role == "critic":
        return _pickup_critic(parsed)

    if role == "meta":
        final_ids: List[str] = []
        for rid in parsed.get("final_picked_rxn_ids") or ():
            value = str(rid or "").strip()
            if value and value not in final_ids:
                final_ids.append(value)
        return {
            "final_ids": final_ids,
            "final_confidence": parsed.get("final_confidence"),
            "no_functional_content": False,
            "reasoning": str(parsed.get("meta_reasoning", "") or ""),
        }

    raise QuerySpecError(f"reaction_pickup_v4 cannot postprocess role {role!r}")
