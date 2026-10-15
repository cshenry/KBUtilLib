"""The template renderer: ``string.Template`` and nothing else.

A query type that names no builtin renderer is rendered from the templates in
its own spec.  The substitution engine is :class:`string.Template`, chosen
over Jinja deliberately: a user-registered query type is data pulled out of a
database and handed straight to a model, so the renderer must have no
conditionals, no loops, no filters and no attribute access -- nothing that
could do work the spec's author did not write down.  Everything variable about
a prompt is therefore one of a CLOSED list of bindings:

* one per declared evidence field (``$text``, ``$ec``, ...),
* ``$candidates_full`` and ``$candidates_short`` -- the menu, rendered through
  the spec's own ``candidate_templates``,
* ``$votes``, ``$ballots`` and ``$critic_json`` -- the council's state, which
  only the critic and meta prompts have.

A template naming anything else is rejected at registration time
(:class:`..errors.TemplateBindingError`) rather than rendered with a blank:
a silently-empty candidate block reads as a bad model, not a bad template.

Candidate blocks apply ``candidate_templates["full"]`` or ``["short"]`` to each
menu entry IN MENU ORDER and join with newlines.  Their bindings are ``$rank``,
``$id``, ``$label``, ``$description``, ``$score``, plus every metadata key as
``$meta_<key>``.
"""

from __future__ import annotations

import json
import string
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..types import CouncilTask, MenuEntry
from .errors import TemplateBindingError

__all__ = [
    "CANDIDATE_BINDINGS",
    "CONTEXT_BINDINGS",
    "ROLES",
    "allowed_bindings",
    "allowed_candidate_bindings",
    "candidate_block",
    "check_template",
    "render_ballots",
    "render_critic_json",
    "render_votes",
    "render_with_templates",
    "template_bindings",
    "normalise_ballots",
]

#: The three roles every query type must define.
ROLES: Tuple[str, str, str] = ("proposer", "critic", "meta")

#: Bindings a CANDIDATE template may use, before the spec's ``menu_fields``
#: contribute their ``$meta_<key>`` names.
CANDIDATE_BINDINGS: Tuple[str, ...] = ("rank", "id", "label", "description", "score")

#: Bindings a ROLE template may use on top of its spec's evidence fields.
CONTEXT_BINDINGS: Tuple[str, ...] = (
    "candidates_full",
    "candidates_short",
    "votes",
    "ballots",
    "critic_json",
)


class _Blanks(dict):
    """Mapping that yields ``""`` for any key it does not hold.

    Only used for CANDIDATE substitution, where a metadata key declared in
    ``menu_fields`` may legitimately be absent from an individual entry.  Role
    templates substitute against a complete mapping and use the strict
    ``substitute`` path, so a missing binding there is still an error.
    """

    def __missing__(self, key: str) -> str:  # noqa: D105 - trivial
        return ""


# ---------------------------------------------------------------------------
# Binding discovery and validation
# ---------------------------------------------------------------------------


def template_bindings(text: str) -> List[str]:
    """Return every ``$name`` used in *text*, in order of appearance.

    ``$$`` is an escape and contributes nothing.  A malformed placeholder
    (a bare ``$`` or ``${`` with no close) raises
    :class:`..errors.TemplateBindingError` rather than being ignored, because
    ``string.Template.substitute`` would raise on it at render time -- long
    after registration, and on somebody else's job.
    """
    found: List[str] = []
    for match in string.Template.pattern.finditer(text or ""):
        if match.group("invalid") is not None:
            snippet = (text or "")[match.start() : match.start() + 24]
            raise TemplateBindingError(
                f"malformed $ placeholder at offset {match.start()}: {snippet!r} "
                "(write $$ for a literal dollar sign)"
            )
        name = match.group("named") or match.group("braced")
        if name:
            found.append(name)
    return found


def allowed_candidate_bindings(menu_fields: Iterable[str]) -> set:
    """The closed binding set for a candidate template."""
    allowed = set(CANDIDATE_BINDINGS)
    allowed.update(f"meta_{field}" for field in (menu_fields or ()))
    return allowed


def allowed_bindings(evidence_fields: Iterable[str]) -> set:
    """The closed binding set for a role template."""
    return set(evidence_fields or ()) | set(CONTEXT_BINDINGS)


def check_template(text: str, allowed: set, where: str) -> None:
    """Raise unless every binding in *text* is in *allowed*.

    *where* names the template in the message (``"roles.critic.user"``), which
    is the only thing that makes a rejection actionable in a spec with six
    templates in it.
    """
    for name in template_bindings(text):
        if name not in allowed:
            raise TemplateBindingError(
                f"{where} uses ${name}, which is not a binding this renderer "
                f"provides; allowed: {', '.join(sorted(allowed))}"
            )


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def _entry_mapping(entry: MenuEntry, position: int) -> _Blanks:
    """Bindings for one menu entry.

    ``rank`` falls back to the entry's 1-based position when the menu carries
    none, and ``score`` is formatted to three decimals -- the fixed precision
    both reference councils use, and the only way two renders of the same menu
    are byte-identical when one came back from JSON as ``0.8100000000000001``.
    """
    mapping = _Blanks(
        {
            "rank": str(entry.rank if entry.rank is not None else position),
            "id": entry.id,
            "label": entry.label or "",
            "description": entry.description or "",
            "score": "" if entry.score is None else f"{float(entry.score):.3f}",
        }
    )
    for key, value in (entry.metadata or {}).items():
        mapping[f"meta_{key}"] = "" if value is None else str(value)
    return mapping


def candidate_block(template: str, menu: Sequence[MenuEntry]) -> str:
    """Render *menu* through *template*, one line per entry, in menu order."""
    if not template:
        return ""
    compiled = string.Template(template)
    return "\n".join(
        compiled.substitute(_entry_mapping(entry, position))
        for position, entry in enumerate(menu or (), start=1)
    )


def normalise_ballots(ballots: Any) -> List[Dict[str, Any]]:
    """Coerce whatever the engine passed as ``context["ballots"]`` to dicts.

    Accepts :class:`..types.Ballot` models or the plain dicts a store round
    trip produces, so a renderer never has to care which side of the database
    it is on.  Picks are normalised to the GENERIC ``{"id", "confidence",
    "why"}`` shape -- ballots reach a renderer after ``postprocess``, so the
    domain field names are already gone.
    """
    out: List[Dict[str, Any]] = []
    for ballot in ballots or ():
        raw = ballot if isinstance(ballot, Mapping) else ballot.model_dump()
        picks = []
        for pick in raw.get("picks") or raw.get("picked") or ():
            pick_raw = pick if isinstance(pick, Mapping) else pick.model_dump()
            picks.append(
                {
                    "id": pick_raw.get("id", ""),
                    "confidence": pick_raw.get("confidence", "?"),
                    "why": pick_raw.get("why", "") or "",
                }
            )
        error = raw.get("error")
        if error is not None and not isinstance(error, Mapping):
            error = error.model_dump()
        out.append(
            {
                "seat": raw.get("seat", "?"),
                "status": raw.get("status", "ok"),
                "picks": picks,
                "overall_confidence": raw.get("overall_confidence") or "?",
                "no_functional_content": bool(raw.get("no_functional_content")),
                "notes": raw.get("notes") or "",
                "error": error,
            }
        )
    return out


def _error_text(error: Optional[Mapping[str, Any]]) -> str:
    """One-line form of a ballot's failure, for a prompt.

    The reference councils interpolate the Python exception's
    ``"<Type>: <message>"``; a council ballot carries a
    :class:`..types.CouncilError` instead, so its ``kind`` stands in for the
    exception type.
    """
    if not error:
        return "failed"
    kind = str(error.get("kind") or "error")
    message = str(error.get("message") or "")
    return f"{kind}: {message}" if message else kind


def render_votes(votes: Optional[Mapping[str, int]]) -> str:
    """The vote table: ``"  <id>: <n>"`` per voted id, most votes first.

    Ids with no votes are omitted and ties keep the order the engine counted
    them in, which is what ``collections.Counter.most_common`` does and what
    both reference councils' tables look like.
    """
    pairs = [(key, int(count)) for key, count in (votes or {}).items() if count]
    pairs.sort(key=lambda kv: -kv[1])
    return "\n".join(f"  {key}: {count}" for key, count in pairs) or "  (nothing picked)"


def render_ballots(ballots: Any) -> str:
    """Per-seat picks and reasoning, in seat order."""
    lines: List[str] = []
    for ballot in normalise_ballots(ballots):
        seat = ballot["seat"]
        if ballot["status"] != "ok":
            lines.append(f"  - {seat}: ERROR {_error_text(ballot['error'])}")
            continue
        if not ballot["picks"]:
            suffix = (
                " (says the string has no functional content)"
                if ballot["no_functional_content"]
                else ""
            )
            lines.append(
                f"  - {seat} [{ballot['overall_confidence']}]: picked NOTHING" + suffix
            )
            continue
        lines.append(f"  - {seat} [{ballot['overall_confidence']}]:")
        for pick in ballot["picks"]:
            lines.append(
                f"      {pick['id']} [{pick['confidence']}] {pick['why'][:200]}"
            )
    return "\n".join(lines)


def render_critic_json(critic: Any, truncate_chars: int) -> str:
    """The critic payload as JSON, truncated to *truncate_chars*.

    Truncation is a byte cut, not a structured summary, exactly as in the
    reference GO council: the meta model is shown as much of the audit as the
    prompt budget allows and is not misled into thinking it saw all of it.
    """
    if not critic:
        return ""
    text = json.dumps(critic)
    return text[:truncate_chars] if truncate_chars and truncate_chars > 0 else text


# ---------------------------------------------------------------------------
# The renderer
# ---------------------------------------------------------------------------


def render_with_templates(
    spec: Any, role: str, task: CouncilTask, context: Optional[Mapping[str, Any]] = None
) -> Tuple[str, str]:
    """Render ``(system, user)`` for *role* from *spec*'s own templates."""
    if role not in ROLES:
        raise TemplateBindingError(
            f"unknown role {role!r}; expected one of {', '.join(ROLES)}"
        )
    context = dict(context or {})
    role_spec = spec.roles[role]

    bindings: Dict[str, str] = {}
    for field in spec.evidence_fields:
        value = (task.evidence or {}).get(field)
        bindings[field] = "" if value is None else str(value)
    bindings["candidates_full"] = candidate_block(
        spec.candidate_templates.get("full", ""), task.menu
    )
    bindings["candidates_short"] = candidate_block(
        spec.candidate_templates.get("short", ""), task.menu
    )
    bindings["votes"] = render_votes(context.get("votes"))
    bindings["ballots"] = render_ballots(context.get("ballots"))
    bindings["critic_json"] = render_critic_json(
        context.get("critic"), spec.critic_truncate_chars
    )

    return (
        string.Template(role_spec.system).substitute(bindings),
        string.Template(role_spec.user).substitute(bindings),
    )
