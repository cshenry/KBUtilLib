"""Verbatim ports of the two CONCORDIA prompt assemblies.

Both builtins render in Python rather than through :mod:`..template` because
the reference assemblies do things a ``string.Template`` cannot and should not
learn: fixed-width rank columns (``{rank:>2}``, ``{pos:>3}``), three-decimal
score formatting, a candidate segment that only appears when the entry carries
a definition, a top-20 cut that differs between the proposer and the later
roles, and -- for reaction pickup -- a system-prompt fragment and a pick cap
that both depend on how many candidates share the input's EC.  The PROMPTS
themselves are not here: they live in the query type's YAML, and this module
only reassembles them exactly as the reference did.

PROVENANCE.  Every string these functions interpolate into was read out of the
files below by ``yaml.safe_dump`` (see the ``attribution`` key of each shipped
YAML, which carries the same record), and the assembly logic below is a line
for line port of the cited ranges.  SHA-256 digests measured on poplar
(h100) on 2026-10-15.

GO molecular function -- renderer ``go_mf_v3``
    /scratch/chenry/go_council_v3/go_council_kit/go_council.py
    sha256 7a567823a1765f7fee93dd29a7a73a5c7f3c9509952cc72f36f43a1f26ca5ea1
    * 50-147   the six prompt constants (shipped in ``go_mf.yaml``)
    * 152-159  ``render_candidates()`` -> :func:`go_candidate_block`
    * 296-362  ``run_one()``: candidate blocks, vote table, reasoning block
    * 364-379  ``run_one()``: the critic prompt assembly
    * 431-448  ``run_one()``: the meta prompt assembly and the 1200-character
               JSON truncation of the critic payload

    Cross-check (SHA-256 pinned by the task, and it MATCHED):
    /scratch/jplfaria/CONCORDIA-live/scripts/go_council_pilot.py
    sha256 3563e97c9b32dce7934da341ce5b3ad2d072a215eabbfa5b4ff9306bc804559d
    Its PROPOSER_USER, CRITIC_SYSTEM, CRITIC_USER, META_SYSTEM and META_USER
    are byte-identical to the V3 kit's.  Its PROPOSER_SYSTEM and
    ``render_candidates()`` are NOT: the live pilot has moved past V3 to an
    origin-labelled menu ("retrieval" / "EC <n> lookup" / "name match").  That
    is a later generation of the query, so this renderer ports the V3 kit --
    the file the task names as the source -- and the divergence is recorded
    rather than silently merged.

Reaction pickup -- renderer ``reaction_pickup_v4``
    /scratch/jplfaria/CONCORDIA-live/concord/pickup_labeler_v4.py
    sha256 a6ecbe25217ae60fd01bd0227c70578797914999a79b5f4393328d01b364f36a
    * 29-95    SYSTEM_PROMPT_BASE, PROMISCUITY_HINT, USER_TEMPLATE
    * 98-130   ``_is_promiscuous()`` (EC threshold 4), ``_shuffled_candidates()``
               (V4 keeps upstream rank order), ``_render_candidates()``
    * 133-158  ``_call_one()``: the proposer assembly and the 5 -> 12 pick cap

    /scratch/jplfaria/CONCORDIA-live/concord/pickup_critic.py
    sha256 6d5d84440f5a4b9391bf7bf56378d4bae6ed4610497b69a9b8006058007ce6e4
    * 47-113   SYSTEM_PROMPT, USER_TEMPLATE
    * 116-169  ``_render_candidates_block()`` (top 20 by rank, the FULL
               rendering with equations, EC and scores -- not a short block),
               ``_render_vote_block()``, ``_render_rationale_block()``
    * 249-265  ``council_critique()``: the critic assembly

    /scratch/jplfaria/CONCORDIA-live/concord/pickup_meta.py
    sha256 f35ec296e045719fd600355ccfa94df004f4c6c61692cec2c2456cba972c2d97
    * 26-89    SYSTEM_PROMPT, USER_TEMPLATE
    * 92-174   the four block renderers, including ``_render_critic_block()``
    * 267-280  ``meta_reviewer()``: the meta assembly

    /scratch/jplfaria/CONCORDIA-live/concord/pickup_schema.py
    sha256 4e18d27d908dcc98f09522b210d013b7768561f88e42d6aca68aeb611762cfe4
    * 29-81    the answer field names, enums and caps the YAML's schemas encode

    /scratch/jplfaria/CONCORDIA-live/scripts/run_concordia_pickup_council_v4.py
    sha256 84101d14655502fcd991c5add772f615e0e8981d8531a25558e3785188e06951
    * 93-151   ``council_pick_v2()``: the vote table (every candidate id, zeros
               included), ``n_council_valid``, and the ``council_picks``
               payload the rationale block is rendered from
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from ...types import CouncilTask, MenuEntry
from ..errors import QuerySpecError
from ..template import (
    normalise_ballots,
    render_ballots,
    render_critic_json,
    render_votes,
)

__all__ = [
    "go_mf_v3",
    "reaction_pickup_v4",
    "go_candidate_block",
    "pickup_candidate_block",
    "PICKUP_TOP_K",
    "PICKUP_PROMISCUITY_THRESHOLD",
    "PICKUP_PICK_CAP",
    "PICKUP_PROMISCUOUS_PICK_CAP",
]

#: ``top_k`` in pickup_critic.council_critique and pickup_meta.meta_reviewer.
PICKUP_TOP_K = 20

#: ``threshold`` in pickup_labeler_v4._is_promiscuous -- lowered from 6 (v2)
#: to 4 (v3/v4) on the gold-62 failure analysis.
PICKUP_PROMISCUITY_THRESHOLD = 4

#: The prompt's pick caps.  Both are INSTRUCTIONS, not validation: the answer
#: schema allows 15, exactly as PickupOutput does in the reference.
PICKUP_PICK_CAP = 5
PICKUP_PROMISCUOUS_PICK_CAP = 12


def _ranked(menu: Sequence[MenuEntry]) -> List[MenuEntry]:
    """Menu entries in rank order.

    ``sorted(inp.candidates, key=lambda c: c.rank)`` in the reference, where
    ``rank`` is a required ``int >= 1``.  A council menu's rank is optional,
    so an entry without one sorts as 0 and keeps its relative position --
    Python's sort is stable, so an entirely unranked menu is left alone.
    """
    return sorted(menu or (), key=lambda entry: entry.rank if entry.rank is not None else 0)


def _n_valid(context: Mapping[str, Any]) -> int:
    """How many seats returned a usable ballot."""
    return sum(1 for b in normalise_ballots(context.get("ballots")) if b["status"] == "ok")


# ---------------------------------------------------------------------------
# GO molecular function
# ---------------------------------------------------------------------------


def go_candidate_block(menu: Sequence[MenuEntry], with_def: bool = True) -> str:
    """``go_council.render_candidates`` (lines 152-159).

    ``  {rank:>2} | {go_id} | {score:.3f} | {name}``, with ``| {def}``
    appended when the entry has a definition and *with_def* is set.  The short
    block the critic sees drops the score AND the definition
    (``go_council.run_one`` line 300).
    """
    lines = []
    for position, entry in enumerate(menu or (), start=1):
        rank = entry.rank if entry.rank is not None else position
        if not with_def:
            lines.append(f"  {rank:>2} | {entry.id} | {entry.label}")
            continue
        score = 0.0 if entry.score is None else float(entry.score)
        line = f"  {rank:>2} | {entry.id} | {score:.3f} | {entry.label}"
        if entry.description:
            line += f" | {entry.description}"
        lines.append(line)
    return "\n".join(lines)


def go_mf_v3(
    spec: Any, role: str, task: CouncilTask, context: Mapping[str, Any]
) -> Tuple[str, str]:
    """Render one GO molecular-function prompt exactly as CONCORDIA V3 did."""
    evidence = task.evidence or {}
    text = str(evidence.get("text", "") or "")
    source = str(evidence.get("source", "") or "")

    if role == "proposer":
        role_spec = spec.roles["proposer"]
        return role_spec.system, role_spec.user.format(
            text=text, source=source, candidates=go_candidate_block(task.menu)
        )

    votes_block = render_votes(context.get("votes"))
    reasoning_block = render_ballots(context.get("ballots"))
    n_valid = _n_valid(context)

    if role == "critic":
        role_spec = spec.roles["critic"]
        return role_spec.system, role_spec.user.format(
            text=text,
            source=source,
            candidates=go_candidate_block(task.menu, with_def=False),
            votes=votes_block,
            n=n_valid,
            reasoning=reasoning_block,
        )

    if role == "meta":
        role_spec = spec.roles["meta"]
        return role_spec.system, role_spec.user.format(
            text=text,
            source=source,
            candidates=go_candidate_block(task.menu),
            votes=votes_block,
            n=n_valid,
            reasoning=reasoning_block,
            critic=render_critic_json(
                context.get("critic"), spec.critic_truncate_chars
            ),
        )

    raise QuerySpecError(f"go_mf_v3 cannot render role {role!r}")


# ---------------------------------------------------------------------------
# Reaction pickup
# ---------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    """``x if len(x) <= limit else x[:limit - 3] + "..."``, as in the reference."""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _skos(value: Any) -> str:
    """The SKOS predicate without its ``skos:`` prefix.

    The reference holds a ``SkosPredicate`` str-enum and renders
    ``c.skos.value.replace("skos:", "")``; a menu entry carries the same
    string in its metadata, so the prefix is stripped here instead.
    """
    return str(value or "").replace("skos:", "")


def _pickup_fields(entry: MenuEntry) -> Dict[str, str]:
    """The six candidate columns, truncated exactly as the reference does."""
    metadata = entry.metadata or {}
    return {
        "name": _clip(str(entry.label or ""), 140),
        "ec": str(metadata.get("ec") or "") or "-",
        "score": f"{0.0 if entry.score is None else float(entry.score):.3f}",
        "skos": _skos(metadata.get("skos")),
        "equation": _clip(str(metadata.get("equation") or ""), 220),
    }


def pickup_candidate_block(
    menu: Sequence[MenuEntry], top_k: int = 0, by_position: bool = False
) -> str:
    """The pickup candidate block.

    Two callers, one line format.  The PROPOSER
    (``pickup_labeler_v4._render_candidates``) numbers by POSITION over the
    whole menu; the critic and meta
    (``pickup_{critic,meta}._render_candidates_block``) print the entry's own
    rank and cut to the top *top_k*.  Both pad the number to width 3.
    """
    entries = _ranked(menu)
    if top_k:
        entries = entries[:top_k]
    lines = []
    for position, entry in enumerate(entries, start=1):
        number = position if by_position else (entry.rank if entry.rank is not None else position)
        f = _pickup_fields(entry)
        lines.append(
            f"  {number:>3} | {entry.id} | {f['name']} | {f['ec']} | "
            f"{f['score']} | {f['skos']} | {f['equation']}"
        )
    return "\n".join(lines)


def _pickup_is_promiscuous(entity_ec: str, menu: Sequence[MenuEntry]) -> Tuple[bool, int]:
    """``pickup_labeler_v4._is_promiscuous`` (lines 98-108).

    Counts candidates one of whose semicolon-separated ECs equals the input's,
    over the WHOLE menu rather than the top 20.
    """
    desc_ec = (entity_ec or "").strip()
    if not desc_ec:
        return False, 0
    n_same = 0
    for entry in menu or ():
        raw = str((entry.metadata or {}).get("ec") or "")
        if desc_ec in [part.strip() for part in raw.split(";")]:
            n_same += 1
    return n_same >= PICKUP_PROMISCUITY_THRESHOLD, n_same


def pickup_vote_block(menu: Sequence[MenuEntry], votes: Mapping[str, int]) -> str:
    """``pickup_{critic,meta}._render_vote_block``.

    Every top-20 candidate in rank order, ZEROS INCLUDED -- the critic is
    asked to judge the rejections as well as the picks, so an id missing from
    the table would be an id it cannot rule on.
    """
    votes = votes or {}
    return "\n".join(
        f"  {entry.id}: {int(votes.get(entry.id, 0))}"
        for entry in _ranked(menu)[:PICKUP_TOP_K]
    )


def pickup_rationale_block(ballots: Any) -> str:
    """``pickup_{critic,meta}._render_rationale_block``."""
    lines: List[str] = []
    for ballot in normalise_ballots(ballots):
        seat = ballot["seat"]
        if ballot["status"] != "ok":
            error = ballot["error"] or {}
            kind = str(error.get("kind") or "error")
            message = str(error.get("message") or "")
            lines.append(
                f"  - {seat}: ERROR " + (f"{kind}: {message}" if message else kind)
            )
            continue
        overall = ballot["overall_confidence"]
        if not ballot["picks"]:
            lines.append(f"  - {seat} [overall={overall}]: picked NOTHING")
            note = (ballot["notes"] or "").strip()
            if note:
                lines.append(f"      notes: {note[:200]}")
            continue
        lines.append(f"  - {seat} [overall={overall}]:")
        for pick in ballot["picks"]:
            lines.append(
                f"      {pick['id']} [{pick['confidence']}] -- {pick['why'][:240]}"
            )
    return "\n".join(lines)


def pickup_critic_block(critic: Mapping[str, Any]) -> str:
    """``pickup_meta._render_critic_block`` (lines 145-174).

    Reads the REFERENCE critic reply, which the pickup adapter keeps under
    ``"raw"`` precisely so this block can still be rendered after the reply
    has been mapped to the generic critic shape.
    """
    critic = dict(critic or {})
    critic = dict(critic.get("raw") or critic)
    if critic.get("error"):
        return f"  CRITIC FAILED: {critic['error']}"
    lines: List[str] = []
    overall_note = str(critic.get("overall_note") or "").strip()
    if overall_note:
        lines.append(f"  overall_note: {overall_note}")
    crits = critic.get("per_reaction_critique") or []
    if crits:
        lines.append("  per_reaction_critique:")
        for item in crits:
            rid = item.get("rxn_id", "?")
            votes = item.get("council_vote_count", 0)
            verdict = item.get("critic_verdict", "?")
            reason = str(item.get("critic_reason") or "")[:300]
            lines.append(f"    - {rid} (votes={votes}, verdict={verdict}): {reason}")
    missed = critic.get("missed_picks") or []
    if missed:
        lines.append(
            "  missed_picks (candidates with 0 council votes the critic thinks were missed):"
        )
        for item in missed:
            rid = item.get("rxn_id", "?")
            reason = str(item.get("reason") or "")[:300]
            lines.append(f"    - {rid}: {reason}")
    elif crits:
        lines.append("  missed_picks: none")
    if not lines:
        return "  (no critic output available)"
    return "\n".join(lines)


def reaction_pickup_v4(
    spec: Any, role: str, task: CouncilTask, context: Mapping[str, Any]
) -> Tuple[str, str]:
    """Render one reaction-pickup prompt exactly as CONCORDIA V4 did."""
    evidence = task.evidence or {}
    gene_id = str(evidence.get("gene_id", "") or "")
    description = str(evidence.get("description", "") or "")
    raw_ec = str(evidence.get("ec", "") or "")
    ec = raw_ec if raw_ec else "(none)"

    if role == "proposer":
        role_spec = spec.roles["proposer"]
        promiscuous, n_same_ec = _pickup_is_promiscuous(raw_ec, task.menu)
        system = role_spec.system
        pick_cap = PICKUP_PICK_CAP
        if promiscuous:
            hint = role_spec.fragments.get("promiscuity_hint")
            if not hint:
                raise QuerySpecError(
                    f"query type {spec.name!r} names renderer reaction_pickup_v4 but "
                    "its proposer role ships no 'promiscuity_hint' fragment"
                )
            system = system + hint.format(
                n_same_ec=n_same_ec, top_k=len(task.menu or ()), ec=raw_ec
            )
            pick_cap = PICKUP_PROMISCUOUS_PICK_CAP
        return system, role_spec.user.format(
            gene_id=gene_id,
            description=description,
            ec=ec,
            candidates=pickup_candidate_block(task.menu, by_position=True),
            pick_cap=pick_cap,
        )

    candidates_block = pickup_candidate_block(task.menu, top_k=PICKUP_TOP_K)
    n_cands = min(PICKUP_TOP_K, len(task.menu or ()))
    # ``n_valid if n_valid else 5``: the reference falls back to its council
    # size when the aggregate did not record one.
    n_valid = _n_valid(context) or 5
    vote_block = pickup_vote_block(task.menu, context.get("votes") or {})
    rationale_block = pickup_rationale_block(context.get("ballots"))

    if role == "critic":
        role_spec = spec.roles["critic"]
        return role_spec.system, role_spec.user.format(
            gene_id=gene_id,
            description=description,
            ec=ec,
            n_cands=n_cands,
            n_valid=n_valid,
            candidates_block=candidates_block,
            vote_block=vote_block,
            rationale_block=rationale_block,
        )

    if role == "meta":
        role_spec = spec.roles["meta"]
        return role_spec.system, role_spec.user.format(
            gene_id=gene_id,
            description=description,
            ec=ec,
            n_cands=n_cands,
            n_valid=n_valid,
            candidates_block=candidates_block,
            vote_block=vote_block,
            rationale_block=rationale_block,
            critic_block=pickup_critic_block(context.get("critic") or {}),
        )

    raise QuerySpecError(f"reaction_pickup_v4 cannot render role {role!r}")
