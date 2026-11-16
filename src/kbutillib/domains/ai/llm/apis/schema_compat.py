"""Making one answer schema acceptable to every backend that will be handed it.

A query type stores ONE JSON Schema per role and every seat is handed that
same schema, but the backends do not agree on what a schema may contain, and
two of those disagreements cost a whole live council run (poplar, 2026-10-19):

* The claude CLI's ``--json-schema`` validator cannot resolve the 2020-12
  meta-schema ref, so a schema that merely DECLARES its dialect is rejected
  outright -- ``Error: --json-schema is not a valid JSON Schema: no schema
  with key or ref "https://json-schema.org/draft/2020-12/schema"`` (issue
  #77, reproduced byte for byte in
  ``tests/domains/ai/council/fixtures/cli_envelopes/claude_schema_key_stderr.txt``).
  Every claude-CLI critic and meta call failed on it.
* OpenAI strict structured outputs require ``required`` to list every key of
  ``properties``, and refuse anything else with a 400 (issue #76).  The
  refusal arrives from the API **after** the codex CLI has accepted the file
  and opened a thread, so it costs a paid round trip per item.

Both are properties of the SCHEMA, so they are fixed here -- once, at the
backend boundary -- rather than by editing a stored spec.  That boundary is
the only place the fix can live without changing a ``prompt_hash``: the
stored schema is part of what a call was asked under, and rewriting it would
silently re-identify every historical call.

Everything in this module is a pure function over a schema dict.  Both read
the schema through ONE walker, :func:`walk_schema`, so "where a subschema can
live" is defined in a single place: a second traversal would be the obvious
way for a check to disagree with the rewrite it is supposed to describe.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterator, List, Mapping, Tuple

__all__ = [
    "META_KEYWORD",
    "STRICT_FORBIDDEN_KEYWORDS",
    "strict_problems",
    "strip_meta_keywords",
    "walk_schema",
]

#: The one keyword :func:`strip_meta_keywords` removes.
#:
#: ONLY this one.  ``$id``, ``$ref`` and ``$defs`` stay: an external scan
#: (2026-11-12) found that claude accepts ``$id`` plus ``$defs`` plus a local
#: ``$ref`` and fails only on the ``$schema`` dialect URL, so stripping the
#: rest would throw away working schema structure to fix a problem it does
#: not cause.  Codex accepts the dialect key as well -- it is dropped for it
#: too only because what is CHECKED must be what is SENT, and because those
#: keys are billed as part of the model input
#: (``fixtures/cli_envelopes/codex_schema_key_ok.jsonl``: 14221 input tokens
#: against 14201 for the same prompt without them).
META_KEYWORD = "$schema"

#: Keywords whose value is a SINGLE subschema when it is a dict.
#:
#: ``items`` and ``additionalProperties`` are in this tuple because both are
#: ALSO legal as a boolean (``additionalProperties: false`` is the whole point
#: of strict mode), which is why every walk checks the type before descending.
_SUBSCHEMA_KEYS: Tuple[str, ...] = (
    "items",
    "additionalProperties",
    "not",
    "if",
    "then",
    "else",
)

#: Keywords whose value is a MAP of name -> subschema.
#:
#: The names in these maps are DATA, never keywords: a schema may legitimately
#: describe a property called ``$schema``, and a walker that read the keys of
#: a ``properties`` block as keywords would delete it.
_SUBSCHEMA_MAPS: Tuple[str, ...] = ("properties", "$defs", "definitions")

#: Keywords whose value is a LIST of subschemas.
_SUBSCHEMA_LISTS: Tuple[str, ...] = ("anyOf", "oneOf", "allOf", "prefixItems")

#: Keywords strict mode documents as unsupported, reported wherever they
#: appear.  ``allOf`` is here with the conditional family because OpenAI's
#: strict structured outputs accept neither.
STRICT_FORBIDDEN_KEYWORDS: Tuple[str, ...] = (
    "allOf",
    "not",
    "if",
    "then",
    "else",
    "dependentRequired",
    "dependentSchemas",
)


#: Sentinel for "the key is not there at all", which ``None`` cannot be:
#: ``additionalProperties: null`` is a different defect from an absent
#: ``additionalProperties``, and the reported message says which.
_ABSENT = object()


def walk_schema(schema: Any, path: str = "$") -> Iterator[Tuple[str, Dict[str, Any]]]:
    """Yield ``(json_path, node)`` for every SCHEMA-VALUED location in *schema*.

    Visited: the root; each value of ``properties``, ``$defs`` and
    ``definitions``; ``items`` and ``additionalProperties`` when they are
    dicts; each entry of ``anyOf``, ``oneOf``, ``allOf`` and ``prefixItems``;
    and ``not``, ``if``, ``then`` and ``else``.  Depth-first, in that order.

    NOT visited, and the reason this is a keyword-driven walk rather than a
    generic "recurse into every dict": ``const``, ``enum``, ``default`` and
    ``examples`` hold the user's DATA, and data is free to be shaped exactly
    like a schema.  A generic walk would strip ``$schema`` out of an example
    value, or lint an enum member for not declaring
    ``additionalProperties``.  For the same reason the KEYS of a
    ``properties`` map are never read as keywords -- a property literally
    named ``$schema`` is a field name, not a dialect declaration.

    Nodes are yielded by reference, so a caller holding a copy can rewrite
    them in place.  Non-dict values are skipped silently: this walks whatever
    a query type actually stored, and refusing to traverse a malformed schema
    would turn a lint into a crash.
    """
    if not isinstance(schema, dict):
        return
    yield path, schema

    for key in _SUBSCHEMA_MAPS:
        block = schema.get(key)
        if isinstance(block, Mapping):
            for name, sub in block.items():
                yield from walk_schema(sub, f"{path}.{key}.{name}")

    for key in _SUBSCHEMA_KEYS:
        yield from walk_schema(schema.get(key), f"{path}.{key}")

    for key in _SUBSCHEMA_LISTS:
        block = schema.get(key)
        if isinstance(block, (list, tuple)):
            for index, sub in enumerate(block):
                yield from walk_schema(sub, f"{path}.{key}[{index}]")


def strip_meta_keywords(schema: Mapping[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of *schema* with the ``$schema`` KEYWORD removed.

    Removed from every node :func:`walk_schema` visits, not just the root: a
    ``$defs`` entry carrying its own dialect declaration would fail the claude
    CLI exactly as a root one does.

    What survives, deliberately:

    * ``$id``, ``$ref``, ``$defs`` and every other keyword -- see
      :data:`META_KEYWORD` for the measurement behind that.
    * a PROPERTY named ``$schema`` (``properties["$schema"]``), which is a
      field the answer may carry and nothing to do with the dialect.
    * a ``$schema`` key inside a ``const``, ``enum``, ``default`` or
      ``examples`` value, which is data.

    The input is never mutated: the caller's schema is usually the stored
    spec, and the whole point of fixing this at the boundary is that the
    stored spec and its ``prompt_hash`` do not move.
    """
    stripped = copy.deepcopy(dict(schema or {}))
    # Materialised before mutating: popping a key out of a node mid-walk would
    # mean rewriting a dict the generator may still be iterating.
    for _path, node in list(walk_schema(stripped)):
        node.pop(META_KEYWORD, None)
    return stripped


def strict_problems(schema: Mapping[str, Any]) -> List[str]:
    """List the ENUMERATED strict-mode preflight problems in *schema*.

    **This is a lint of documented rules, not a proof of compatibility.**  An
    empty list means no problem from the enumerated set below -- it does NOT
    mean the provider will accept the schema.  Strictness is enforced
    server-side at 400 (measured: the codex CLI accepts the schema file and
    opens a thread before the API rejects it), so this check exists to turn
    the common, known refusals into a local failure that costs nothing,
    leaving the authority with the provider.

    The enumerated rules, each reported as ``"<json-path>: <rule>"`` with the
    path naming the offending node (``$``,
    ``$.properties.picked.items``, ``$.$defs.Pick``, ``$.anyOf[0]`` ...):

    1. the root is an object node, and is not a root ``anyOf``;
    2. every object node sets ``additionalProperties`` to exactly ``false``;
    3. every object node's ``required`` lists every key of its
       ``properties`` (issue #76: ``go_mf@1``'s proposer omits ``notes``);
    4. no :data:`STRICT_FORBIDDEN_KEYWORDS` anywhere.

    ``$schema`` is deliberately NOT a problem: codex was observed to accept a
    root ``$schema`` and ``$id`` and answer normally
    (``fixtures/cli_envelopes/codex_schema_key_ok.jsonl``).  It is claude's
    validator that refuses it, which is :func:`strip_meta_keywords`' job --
    conflating the two would have a codex seat refuse a schema that works.
    """
    problems: List[str] = []
    for path, node in walk_schema(dict(schema or {})):
        if path == "$":
            problems.extend(_root_problems(node))
        is_object = node.get("type") == "object" or "properties" in node
        if is_object:
            found = node.get("additionalProperties", _ABSENT)
            if found is not False:
                shown = "absent" if found is _ABSENT else repr(found)
                problems.append(
                    f"{path}: an object node must set additionalProperties to "
                    f"exactly false ({shown})"
                )
            missing = _missing_from_required(node)
            if missing:
                problems.append(
                    f"{path}: required must list every key in properties "
                    f"(missing: {', '.join(missing)})"
                )
        for keyword in STRICT_FORBIDDEN_KEYWORDS:
            if keyword in node:
                problems.append(
                    f"{path}: {keyword} is not supported in strict mode"
                )
    return problems


def _root_problems(root: Mapping[str, Any]) -> List[str]:
    """Rule 1: the root must be an object node and must not be an ``anyOf``.

    A root union is reported INSTEAD of "not an object node" rather than as
    well: it is the same defect said precisely, and a lint that reports one
    fault twice reads as two.
    """
    if "anyOf" in root:
        return ["$: the root must not be an anyOf -- strict mode needs one object"]
    if root.get("type") != "object" and "properties" not in root:
        return [
            "$: the root must be an object node (type: object, with properties)"
        ]
    return []


def _missing_from_required(node: Mapping[str, Any]) -> List[str]:
    """Keys of *node*'s ``properties`` that its ``required`` does not list.

    In ``properties`` order rather than sorted, so the message reads in the
    order a human looking at the schema will scan it.
    """
    properties = node.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return []
    raw_required = node.get("required")
    required = (
        {str(name) for name in raw_required}
        if isinstance(raw_required, (list, tuple))
        else set()
    )
    return [str(name) for name in properties if str(name) not in required]
