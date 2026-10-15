"""The decision rule: one pure function, no I/O, no backend, no store.

:func:`decide` is the whole of Jose Faria's CONCORDIA V3 adjudication,
generalised and made strict.  It is deliberately the ONLY place the rule
exists: the worker calls it after every settled call and acts on what it
returns, so "when is meta skipped?" has exactly one answer in this codebase
and a test can ask that question without a database, an event loop or a
model.

The function is a state machine over the calls of ONE item, and it returns
what that item needs next:

* ``("verdict", CouncilVerdict)`` -- the item is decided.
* ``("need_critic", None)`` -- the proposers are all in and agreed enough to
  audit; the critic call has not been created or has not settled.
* ``("need_meta", None)`` -- the critic is in and either objected or the vote
  was not unanimous; the meta call has not been created or has not settled.
* ``("waiting", None)`` -- some proposer call is still outstanding.  Note that
  a FAILED proposer with an outstanding sibling also waits: the error verdict
  records every seat, so it cannot be written until every seat is terminal.

Three things that look like the worker's job are done here on purpose,
because they change the decision and so belong to the rule:

* **Per-proposer deduplication.**  A proposer that picks the same id twice
  voted once.
* **Menu filtering.**  An id that is not on the closed menu is dropped from
  the ballot and recorded in ``verdict.errors`` with kind ``off_menu``; the
  ballot itself stays ``ok``.  A council whose menu is closed cannot select
  something that was never offered, and silently keeping it would put an id
  in ``selected_ids`` that the caller's own candidate table does not contain.
* **The generic-shape check.**  A reply that survived the JSON Schema but
  does not carry a usable ``picked``/``verdict``/``final_ids`` after
  ``postprocess`` fails its seat rather than being counted as a vote for
  nothing.

Input is a :class:`SettledCall` per seat -- the store row reduced to the five
facts the rule reads, with ``postprocess`` already applied.
:meth:`SettledCall.from_call_row` does that reduction, so the worker and a
test build the same thing from different sides.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel

from ..types import (
    Ballot,
    CouncilError,
    CouncilTask,
    CouncilVerdict,
    Pick,
    Roster,
)

__all__ = [
    "APPROVING_VERDICTS",
    "SettledCall",
    "decide",
    "critic_approves",
    "tally",
]

#: The critic verdicts that count as approval.  Everything else -- including
#: ``ambiguous``, which is not a complaint -- sends the item to meta: the
#: critic's job is to say the vote table is right, and "I cannot tell" is not
#: that.
APPROVING_VERDICTS = ("strongly_consistent", "consistent")

_CONFIDENCES = ("high", "medium", "low")


class SettledCall(BaseModel):
    """One settled model invocation, reduced to what the rule reads.

    ``parsed`` is the POSTPROCESSED, generic-shaped reply (``picked`` /
    ``verdict`` / ``final_ids``), not the original the store holds: the rule
    is domain-neutral, and the rename is the query type's job.
    """

    role: str
    seat_name: str
    status: str = "ok"
    call_id: str = ""
    parsed: Optional[Dict[str, Any]] = None
    error: Optional[CouncilError] = None
    usage: Optional[Dict[str, Any]] = None
    reused: bool = False

    @classmethod
    def from_call_row(cls, row: Mapping[str, Any], query: Any) -> "SettledCall":
        """Reduce a settled store row to a :class:`SettledCall`.

        ``query.postprocess`` is applied here and nowhere else in the engine.
        A postprocess that raises is turned into a failed seat rather than
        allowed to abort the item: a query type that cannot read its own
        model's answer is an ``invalid_output``, which is exactly what the
        rule already knows how to handle.
        """
        role = str(row.get("role") or "")
        status = str(row.get("status") or "")
        raw_error = row.get("error")
        error = (
            CouncilError(**dict(raw_error))
            if isinstance(raw_error, Mapping) and raw_error
            else None
        )
        parsed: Optional[Dict[str, Any]] = None
        if status == "ok":
            reply = row.get("reply_parsed")
            if isinstance(reply, Mapping):
                try:
                    parsed = dict(query.postprocess(role, dict(reply)))
                except Exception as exc:  # noqa: BLE001 - becomes a failed seat
                    status = "error"
                    error = CouncilError(
                        kind="invalid_output",
                        message=f"{type(exc).__name__}: {exc}",
                    )
        return cls(
            role=role,
            seat_name=str(row.get("seat_name") or ""),
            status=status,
            call_id=str(row.get("call_id") or ""),
            parsed=parsed,
            error=error,
            usage=dict(row.get("fresh_usage") or {}) or None,
            reused=bool(row.get("reused_from")),
        )


def critic_approves(critic: Optional[Mapping[str, Any]]) -> bool:
    """Does this critic payload approve the vote table?

    All three clauses must hold: the verdict is one of
    :data:`APPROVING_VERDICTS`, nothing is overpicked, and nothing is missed.
    A critic that approves the verdict while listing a missed id has not
    approved the table -- the list is the complaint.
    """
    if not critic:
        return False
    return (
        str(critic.get("verdict") or "") in APPROVING_VERDICTS
        and not list(critic.get("overpicked") or ())
        and not list(critic.get("missed") or ())
    )


def tally(
    task: CouncilTask,
    roster: Roster,
    proposer_calls: Sequence[Optional[SettledCall]],
) -> Tuple[List[Ballot], Dict[str, int], List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Turn settled proposer calls into ballots, the vote table and the notes.

    Public because the WORKER needs exactly this to render the critic and meta
    prompts: those prompts show the vote table and the per-seat reasoning, and
    a prompt built from a second, slightly different tally would audit a
    council that never sat.  One computation, two consumers.

    Returns ``(ballots, votes, errors, usage)``:

    * ``ballots`` -- one per seat, in ``roster.proposers`` order.  A seat that
      failed gets an ``error`` ballot with no picks.
    * ``votes`` -- ``{id: count}`` in FIRST-VOTE order, which is the order the
      renderers' vote block prints ties in.
    * ``errors`` -- the ``off_menu`` notes and the failed seats.
    * ``usage`` -- per-seat token usage, including a reused seat's zeroes.
    """
    menu_ids = [str(entry.id) for entry in (task.menu or [])]
    ballots: List[Ballot] = []
    votes: Dict[str, int] = {}
    errors: List[Dict[str, Any]] = []
    usage: Dict[str, Dict[str, Any]] = {}

    for seat, call in zip(roster.proposers, proposer_calls):
        if call is None:  # pragma: no cover - decide() checks this first
            continue
        _record_usage(usage, seat.seat_name, call)
        shape_error = _proposer_shape_error(call) if call.status == "ok" else None
        if call.status != "ok" or shape_error is not None:
            error = shape_error or call.error or CouncilError(
                kind="invalid_output", message="the seat returned no usable answer"
            )
            ballots.append(Ballot(seat=seat.seat_name, status="error", error=error))
            errors.append(_error_entry("proposer", seat.seat_name, error))
            continue

        reply = call.parsed or {}
        picks: List[Pick] = []
        seen: set = set()
        for raw in reply.get("picked") or ():
            pick = dict(raw) if isinstance(raw, Mapping) else {}
            pick_id = str(pick.get("id") or "")
            if not pick_id or pick_id in seen:
                # A repeat is one opinion, not two.  First occurrence wins so
                # the proposer's own ordering survives.
                continue
            seen.add(pick_id)
            if pick_id not in menu_ids:
                errors.append(
                    {
                        "stage": "proposer",
                        "seat": seat.seat_name,
                        "kind": "off_menu",
                        "message": f"{pick_id!r} is not on the closed menu; dropped",
                    }
                )
                continue
            picks.append(
                Pick(
                    id=pick_id,
                    confidence=_confidence(pick.get("confidence")),
                    why=str(pick.get("why") or ""),
                )
            )
        ballots.append(
            Ballot(
                seat=seat.seat_name,
                status="ok",
                picks=picks,
                overall_confidence=reply.get("overall_confidence"),
                no_functional_content=bool(reply.get("no_functional_content")),
                notes=reply.get("notes") or None,
            )
        )
        for pick in picks:
            votes[pick.id] = votes.get(pick.id, 0) + 1

    return ballots, votes, errors, usage


def decide(
    task: CouncilTask,
    roster: Roster,
    query: Any,
    proposer_calls: Sequence[Optional[SettledCall]],
    critic_call: Optional[SettledCall] = None,
    meta_call: Optional[SettledCall] = None,
    *,
    now: Optional[datetime] = None,
) -> Tuple[str, Optional[CouncilVerdict]]:
    """Decide one item, or say what it still needs.

    Args:
        task: the item, its evidence and its closed menu.
        roster: the seating; proposer ORDER here is the order ballots, votes
            and per-seat usage are reported in.
        query: the query type, read only for its ``name`` and ``version``.
        proposer_calls: one entry per proposer seat, in ``roster.proposers``
            order.  ``None`` means "not settled yet".
        critic_call: the settled critic call, or ``None``.
        meta_call: the settled meta call, or ``None``.
        now: the decision timestamp (tests pin it).

    Returns:
        ``(what_next, verdict_or_None)`` -- see the module docstring.
    """
    menu_ids = [str(entry.id) for entry in (task.menu or [])]
    identity = _query_identity(query)
    base: Dict[str, Any] = {
        "item_id": task.item_id,
        "query": identity,
        "roster_fingerprint": roster.fingerprint(),
        "decided_at": _isoformat(now),
    }

    # 1. An empty menu is decided without asking anybody.  There is nothing to
    #    pick from, so every call would be spend on a knowable answer.  No
    #    seat spoke, so n_valid is 0 and final_confidence stays None: "high"
    #    would be a confidence nobody expressed.
    if not menu_ids:
        return (
            "verdict",
            CouncilVerdict(
                status="ok",
                selected_ids=[],
                decline_reason="no_candidates",
                decided_by="deterministic_empty",
                meta_skipped=True,
                n_valid=0,
                **base,
            ),
        )

    # 2. Proposers.  Every seat must be terminal before anything is written,
    #    including when one has already failed: the error verdict names every
    #    seat, so writing it early would hide the sibling that was still live.
    aligned = _aligned(roster, proposer_calls)
    if any(call is None for call in aligned):
        return ("waiting", None)

    n_proposers = len(roster.proposers)
    ballots, votes, errors, usage = tally(task, roster, aligned)

    # 3. One failed proposer fails the item.  No critic, no meta: auditing a
    #    vote table that is missing a seat would audit a different council.
    if any(ballot.status != "ok" for ballot in ballots):
        return (
            "verdict",
            _error_verdict(base, ballots, votes, n_proposers, errors, usage),
        )

    # 4. Every proposer is a valid vote.
    n_valid = n_proposers

    # 5. The critic audits the table.
    if critic_call is None:
        return ("need_critic", None)
    _record_usage(usage, roster.critic.seat_name, critic_call)
    if critic_call.status != "ok" or not isinstance(critic_call.parsed, Mapping):
        error = critic_call.error or CouncilError(
            kind="invalid_output", message="the critic returned no usable answer"
        )
        errors.append(_error_entry("critic", roster.critic.seat_name, error))
        return (
            "verdict",
            _error_verdict(base, ballots, votes, n_valid, errors, usage),
        )
    critic = dict(critic_call.parsed)
    approved = critic_approves(critic)

    # 6. Meta is skipped only when the critic approved AND every voted id was
    #    picked by EVERY proposer.  {A: 3, B: 2} with n_valid 3 goes to meta
    #    even though A is unanimous: the council did not agree on the SET.
    unanimous = not votes or all(count == n_valid for count in votes.values())
    if approved and unanimous:
        selected = [mid for mid in menu_ids if votes.get(mid)]
        contentless = _content_flag(ballots, n_proposers, critic)
        return (
            "verdict",
            CouncilVerdict(
                status="ok",
                selected_ids=selected,
                decline_reason=_decline_reason(selected, contentless),
                final_confidence="high",
                decided_by="unanimous",
                ballots=ballots,
                votes=votes,
                n_valid=n_valid,
                critic=critic,
                meta=None,
                meta_skipped=True,
                errors=errors,
                usage=usage,
                **base,
            ),
        )

    # 7. Meta adjudicates.
    if meta_call is None:
        return ("need_meta", None)
    _record_usage(usage, roster.meta.seat_name, meta_call)
    if meta_call.status != "ok" or not isinstance(meta_call.parsed, Mapping):
        error = meta_call.error or CouncilError(
            kind="invalid_output", message="meta returned no usable answer"
        )
        errors.append(_error_entry("meta", roster.meta.seat_name, error))
        return (
            "verdict",
            _error_verdict(
                base, ballots, votes, n_valid, errors, usage, meta_skipped=False
            ),
        )
    meta = dict(meta_call.parsed)

    final: List[str] = []
    for raw_id in meta.get("final_ids") or ():
        final_id = str(raw_id)
        if final_id in menu_ids:
            if final_id not in final:
                final.append(final_id)
            continue
        errors.append(
            {
                "stage": "meta",
                "seat": roster.meta.seat_name,
                "kind": "off_menu",
                "message": f"{final_id!r} is not on the closed menu; dropped",
            }
        )
    selected = [mid for mid in menu_ids if mid in final]
    contentless = bool(meta.get("no_functional_content"))
    return (
        "verdict",
        CouncilVerdict(
            status="ok",
            selected_ids=selected,
            decline_reason=_decline_reason(selected, contentless),
            final_confidence=meta.get("final_confidence"),
            decided_by="meta",
            ballots=ballots,
            votes=votes,
            n_valid=n_valid,
            critic=critic,
            meta=meta,
            meta_skipped=False,
            errors=errors,
            usage=usage,
            **base,
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _aligned(
    roster: Roster, proposer_calls: Sequence[Optional[SettledCall]]
) -> List[Optional[SettledCall]]:
    """Return one call per proposer seat, in ``roster.proposers`` order.

    A caller may pass the calls in any order (a store read comes back in
    ``created_at`` order, not seat order), so they are matched by seat name
    when every entry carries one and taken positionally otherwise -- which is
    what lets a decision-rule test write three calls in a list and not repeat
    the seat names.
    """
    seat_names = [seat.seat_name for seat in roster.proposers]
    provided = [call for call in proposer_calls if call is not None]
    by_seat = {call.seat_name: call for call in provided if call.seat_name}
    if len(by_seat) == len(provided) and set(by_seat).issubset(seat_names):
        return [by_seat.get(name) for name in seat_names]
    calls = list(proposer_calls)
    calls.extend([None] * (len(roster.proposers) - len(calls)))
    return calls[: len(roster.proposers)]


def _query_identity(query: Any) -> Dict[str, str]:
    """``{"name", "version"}`` of a query type, model or row alike."""
    if isinstance(query, Mapping):
        return {
            "name": str(query.get("name") or ""),
            "version": str(query.get("version") or ""),
        }
    return {
        "name": str(getattr(query, "name", "") or ""),
        "version": str(getattr(query, "version", "") or ""),
    }


def _isoformat(now: Optional[datetime]) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _confidence(value: Any) -> str:
    """Coerce a stated confidence to the three the ballot allows."""
    text = str(value or "").strip().lower()
    return text if text in _CONFIDENCES else "medium"


def _proposer_shape_error(call: SettledCall) -> Optional[CouncilError]:
    """Is this ok proposer reply unusable after postprocess?

    The JSON Schema ran against the ORIGINAL reply, in the query's own field
    names.  This is the generic-shape check that happens after the rename:
    a reply with no ``picked`` list cannot be counted, and counting it as a
    vote for nothing would quietly turn a broken seat into an abstention.
    """
    parsed = call.parsed
    if not isinstance(parsed, Mapping):
        return CouncilError(
            kind="invalid_output",
            message="the reply carried no parsed answer",
        )
    picked = parsed.get("picked")
    if not isinstance(picked, (list, tuple)):
        return CouncilError(
            kind="invalid_output",
            message=f"`picked` is {type(picked).__name__}, expected a list",
        )
    return None


def _content_flag(
    ballots: Sequence[Ballot], n_proposers: int, critic: Mapping[str, Any]
) -> bool:
    """The skip path's "the input states no function" decision.

    Majority of the PROPOSERS, with the critic breaking an exact tie.  The
    vote table does not enter it: an item every proposer declined because the
    string says nothing is contentless whether or not anything was picked,
    and an item with picks is not contentless however the flags fell.
    """
    flagged = sum(1 for ballot in ballots if ballot.no_functional_content)
    if 2 * flagged > n_proposers:
        return True
    if 2 * flagged < n_proposers:
        return False
    return bool(critic.get("contentless_input"))


def _decline_reason(selected: Sequence[str], contentless: bool) -> Optional[str]:
    """Why an ok verdict selected nothing, or ``None`` when it selected."""
    if selected:
        return None
    return "contentless" if contentless else "no_candidate_matched"


def _error_entry(stage: str, seat: str, error: CouncilError) -> Dict[str, Any]:
    return {
        "stage": stage,
        "seat": seat,
        "kind": error.kind,
        "message": error.message,
    }


def _record_usage(
    usage: Dict[str, Dict[str, Any]], seat_name: str, call: SettledCall
) -> None:
    """Record one seat's token usage, including a reused seat's zeroes."""
    fresh = call.usage or {}
    usage[seat_name] = {
        "prompt_tokens": fresh.get("prompt_tokens"),
        "completion_tokens": fresh.get("completion_tokens"),
        "reused": bool(call.reused),
    }


def _error_verdict(
    base: Mapping[str, Any],
    ballots: Sequence[Ballot],
    votes: Mapping[str, int],
    n_valid: int,
    errors: Sequence[Mapping[str, Any]],
    usage: Mapping[str, Mapping[str, Any]],
    meta_skipped: bool = True,
) -> CouncilVerdict:
    """The verdict for an item a stage failed on.

    Nothing is selected and nothing is attributed -- ``selected_ids`` empty,
    ``decline_reason``, ``decided_by`` and ``final_confidence`` all ``None``
    -- because a council that lost a seat did not decline, it failed.  The
    ballots stay, so the picks that WERE cast remain visible to whoever
    re-runs the item.
    """
    return CouncilVerdict(
        status="error",
        selected_ids=[],
        decline_reason=None,
        final_confidence=None,
        decided_by=None,
        ballots=list(ballots),
        votes=dict(votes),
        n_valid=n_valid,
        meta_skipped=meta_skipped,
        errors=[dict(entry) for entry in errors],
        usage={k: dict(v) for k, v in usage.items()},
        **dict(base),
    )
