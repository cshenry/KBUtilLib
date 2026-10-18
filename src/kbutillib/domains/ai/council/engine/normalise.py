"""Answer normalisation: two measured reply defects, repaired before the gate.

:func:`normalise` is a pure function the worker applies to every parsed reply
BEFORE :func:`~.worker._validate_answer`.  It exists because of a measurement,
not a theory.  Replaying Jose Faria's recorded CONCORDIA V3 GO council through
this worker (``agent-io/prds/kbu-ai-council-v1/research/go-replay-parity.md``)
reproduced his decisions on 91 of 99 scored rows, and all eight shortfalls were
the same thing twice over: a reply the answer schema refused for a defect that
does not touch what the model actually decided.

* Seven ``claudeopus45`` META replies omitted ``no_functional_content``, which
  ``go_mf@1``'s meta schema requires -- while naming a non-empty final id
  list, which settles the flag's value on its own.
* One ``gemini35flash`` PROPOSER reply returned ``"notes": null`` where the
  schema says string.

With the gate as the only variable the decision rule reproduced V3 on all
99 rows, so the gap was the gate refusing answers over spelling.  These two
repairs close it, and NOTHING ELSE is repaired: every other schema failure is
still an ``invalid_output``, because every other schema failure is a reply the
rule cannot read.

Two properties this module is built to keep:

* **The raw reply is never touched.**  ``normalise`` copies, so the caller's
  dict is unchanged and ``calls.reply_text`` keeps the exact bytes the model
  returned.  Each repair records the value it replaced, so the original parsed
  answer is recoverable from the record rather than merely described by it.
* **One rule, one implementation.**  The derived flag's empty-list branch is
  :func:`~.decide.content_flag` -- the same function ``decide()`` uses on its
  meta-skip path -- imported, not restated.  A council that derived the flag
  one way and decided with it another would be two rules wearing one name.

Nothing here does I/O, and nothing here knows a domain vocabulary: both rules
are typed by the ROLE'S OWN ANSWER SCHEMA, which is why ``notes``, ``why``,
``reason``, ``reasoning`` and a query type's renamed id list are all covered
without any of them being named in this file.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .decide import content_flag

__all__ = [
    "CONTENT_FLAG_FIELD",
    "DERIVED_CONTENT_FLAG",
    "NULL_STRING",
    "normalise",
]

#: Rule name recorded for "a schema-declared string arrived as ``null``".
NULL_STRING = "null_string"

#: Rule name recorded for "a meta reply omitted the content flag".
DERIVED_CONTENT_FLAG = "derived_content_flag"

#: The generic name of the "the input states no function" flag.  Generic
#: rather than query-specific on purpose: the engine reads it positionally by
#: this key in every role's answer (``decide.tally``, ``decide.decide``), so a
#: query type that renamed it would already be unreadable.
CONTENT_FLAG_FIELD = "no_functional_content"


def normalise(
    role: str,
    parsed: Any,
    schema: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """Repair the two measured reply defects in *parsed*; report what changed.

    Args:
        role: ``proposer``, ``critic`` or ``meta``.  Only the second rule is
            role-specific.
        parsed: the reply as the backend parsed it, in the query type's OWN
            field names (normalisation happens before ``postprocess``).  Not
            mutated; anything that is not a mapping is returned untouched, so
            the worker's "the backend returned a list" complaint still comes
            from ``_validate_answer`` rather than from here.
        schema: the role's answer schema -- ``query.answer_schema(role)``.
            BOTH rules are typed by it: a property is repaired because the
            schema says what it should have been, never because this module
            recognises its name.
        context: the council state for the derived flag, as the worker's
            ``_context_for`` builds it: ``ballots`` (one per proposer seat,
            each carrying ``no_functional_content``) and ``critic`` (the
            settled critic reply).  Ignored for the other rule and for the
            other roles.

    Returns:
        ``(parsed', repairs)``.  ``repairs`` is one dict per change, each
        ``{"field", "rule", "original", "value"}``:

        * ``field`` -- the repaired path, e.g. ``notes`` or ``picked[0].why``.
        * ``rule`` -- :data:`NULL_STRING` or :data:`DERIVED_CONTENT_FLAG`.
        * ``original`` -- the value that was there.  ``None`` for both rules,
          which is the point of recording ``rule`` beside it: under
          :data:`NULL_STRING` the property was PRESENT and null, under
          :data:`DERIVED_CONTENT_FLAG` it was ABSENT.
        * ``value`` -- what it became, so the record explains the answer that
          was stored without the reader re-deriving it.

        An empty list means the reply was accepted exactly as it arrived.
        ``normalise`` is idempotent: re-applying it to its own output repairs
        nothing and reports nothing, which is what lets the reuse path run it
        over an answer that may or may not already have been through it.
    """
    if not isinstance(parsed, Mapping):
        return parsed, []
    repaired: Dict[str, Any] = copy.deepcopy(dict(parsed))
    repairs: List[Dict[str, Any]] = []
    properties = dict((schema or {}).get("properties") or {})

    _null_strings(repaired, properties, repairs)
    if role == "meta":
        _derived_content_flag(repaired, properties, dict(context or {}), repairs)
    return repaired, repairs


# ---------------------------------------------------------------------------
# Rule 1: a schema-declared string that arrived as null
# ---------------------------------------------------------------------------


def _null_strings(
    reply: Dict[str, Any],
    properties: Mapping[str, Any],
    repairs: List[Dict[str, Any]],
) -> None:
    """``null`` -> ``""`` for every property the schema types as a string.

    Covers the array-of-objects case too -- ``picked[].why`` is as likely to
    come back null as ``notes`` is -- by reading the array's ``items``
    schema.  A property that is ABSENT is left absent: a missing optional
    string is not a defect, and inventing one would turn "said nothing" into
    "said the empty string".
    """
    for key, declared in properties.items():
        if not isinstance(declared, Mapping):
            continue
        if declared.get("type") == "string":
            if key in reply and reply[key] is None:
                reply[key] = ""
                repairs.append(_repair(key, NULL_STRING, None, ""))
            continue
        if declared.get("type") != "array":
            continue
        items = declared.get("items")
        if not isinstance(items, Mapping) or items.get("type") != "object":
            continue
        item_properties = dict(items.get("properties") or {})
        entries = reply.get(key)
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            for sub_key, sub_declared in item_properties.items():
                if not isinstance(sub_declared, Mapping):
                    continue
                if sub_declared.get("type") != "string":
                    continue
                if sub_key in entry and entry[sub_key] is None:
                    entry[sub_key] = ""
                    repairs.append(
                        _repair(f"{key}[{index}].{sub_key}", NULL_STRING, None, "")
                    )


# ---------------------------------------------------------------------------
# Rule 2: a meta reply that omitted the content flag
# ---------------------------------------------------------------------------


def _derived_content_flag(
    reply: Dict[str, Any],
    properties: Mapping[str, Any],
    context: Mapping[str, Any],
    repairs: List[Dict[str, Any]],
) -> None:
    """Supply a missing ``no_functional_content`` on a meta reply.

    The value is DERIVED, never defaulted:

    * The reply names a non-empty final id list -> ``False``.  An item with
      ids selected is not contentless whichever way the flag would have
      fallen, which is the same reasoning ``decide.content_flag``'s docstring
      gives for ignoring the vote table.
    * The list is empty (or absent) -> whatever the skip path would have
      decided from the seats that DID answer: :func:`~.decide.content_flag`
      over the proposers' own flags, with the critic breaking an exact tie.

    Two cases deliberately do nothing:

    * The schema does not declare the flag as a boolean -- then it is not the
      field this rule is about, and a reply missing something else stays
      ``invalid_output``.
    * The meta answer's id list cannot be identified from the schema (see
      :func:`_ids_key`).  Guessing the branch would guess the answer; a
      reply whose role schema this module cannot read is left to the gate.
    """
    if CONTENT_FLAG_FIELD in reply:
        return
    declared = properties.get(CONTENT_FLAG_FIELD)
    if not isinstance(declared, Mapping) or declared.get("type") != "boolean":
        return
    ids_key = _ids_key(properties)
    if ids_key is None:
        return

    final_ids = reply.get(ids_key)
    if isinstance(final_ids, (list, tuple)) and len(final_ids) > 0:
        value = False
    else:
        ballots: Sequence[Any] = list(context.get("ballots") or ())
        value = content_flag(ballots, len(ballots), dict(context.get("critic") or {}))
    reply[CONTENT_FLAG_FIELD] = value
    repairs.append(
        _repair(CONTENT_FLAG_FIELD, DERIVED_CONTENT_FLAG, None, value)
    )


def _ids_key(properties: Mapping[str, Any]) -> Optional[str]:
    """The meta answer's final-id-list key, read off the schema.

    A query type renames it -- ``final_ids`` becomes ``final_<id_field>s``,
    ``go_mf@1``'s is ``final_go_ids`` -- so it cannot be hard-coded here, and
    normalisation runs before the adapter has renamed anything.  The meta
    schema has exactly ONE array-of-strings property (see
    ``queries/generic_schemas.json``), which identifies it without this module
    knowing any domain's vocabulary.  Anything ambiguous returns ``None`` and
    the rule declines to fire.
    """
    found = [
        key
        for key, declared in properties.items()
        if isinstance(declared, Mapping)
        and declared.get("type") == "array"
        and isinstance(declared.get("items"), Mapping)
        and declared["items"].get("type") == "string"
    ]
    return found[0] if len(found) == 1 else None


def _repair(field: str, rule: str, original: Any, value: Any) -> Dict[str, Any]:
    return {"field": field, "rule": rule, "original": original, "value": value}
