"""The un-run derivation: which clearinghouse functions a council run owes.

Stage 1 of the standing ``function_to_go`` pipeline asks one question --
*which functions has this run not answered yet, and what shape is the work* --
and the answer is set arithmetic over three inputs:

* **universe** -- ``function_entity`` joined to ``function_content``, i.e.
  every function the clearinghouse knows and its definition text.
* **done** -- the current-state ``function_result`` rows at THIS run's
  ``parameter_set_hash``. Un-run means "no current-state row at this
  parameter set"; a row at a different parameter set is a different
  experiment and does not count (see
  :func:`~kbutillib.domains.ai.council.types.bench_fingerprint`).
* **ranked** -- :meth:`~.ontology_mapping_utils.OntologyMappingUtils.function_to_go`
  output over the un-run set, which is what knows whether retrieval abstained
  and what the candidate menu is.

THIS MODULE IS PURE. No clearinghouse, no council, no numpy, no I/O -- which
is the point of it being separate from :mod:`.ontology_mapping_utils`. That
module imports :mod:`.go_text` (numpy / scikit-learn / joblib), so a
partition test living there would be SKIPPED on every host without the
retrieval stack -- and the partition arithmetic is exactly the part with a
known answer worth regression-testing (20,036 = 19,276 submit + 760 abstain
over the recorded ADP1+PMI priority set). :func:`unrun_set` is re-exported
from ``ontology_mapping_utils`` so callers can reach it beside the stage that
uses it.

THE JOIN KEY IS ``query_id``, which is the clearinghouse's own
``entity_hash`` carried through unchanged. Measured 2026-10-21: the council's
own ``items.entity_hash`` matched 0 of 19,268 clearinghouse hashes, while the
definition TEXT matched 19,268 of 19,268. So the key must travel with the
item, and a text join -- the only join that would appear to work -- is the one
join this pipeline must never make.

THE THREE PARTITIONS, and why all three are recorded:

* ``submit`` -- one row per DISTINCT definition text that retrieval did not
  decline. This is the work the council is paid to do.
* ``abstain`` -- every un-run function retrieval declined (a placeholder
  function, or no candidate survived the GO height filter). These are carried
  forward by stage 5 as result rows with ``decline_reason``
  ``"abstained:<reason>"``. **They are never dropped.** Dropping them is
  precisely what made 760 definitions look like an unexplained coverage gap
  after the 2026-10-20 run.
* ``fanout`` -- the complete ``query_id -> item_id`` map for every un-run,
  non-declined function. A text shared by three ``query_id``s is answered ONCE
  (one ``submit`` row) and fanned back out to all three (three ``fanout``
  rows), so every function gets a result row from one paid answer.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple

__all__ = [
    "UNRUN_PARTITIONS",
    "done_hashes",
    "item_id_of_index",
    "unrun_items",
    "unrun_set",
]

#: The three partitions :func:`unrun_set` returns, in the order they are
#: reported. Named so a caller can iterate them rather than hard-coding the
#: keys, and so a test can assert the shape is complete.
UNRUN_PARTITIONS: Tuple[str, ...] = ("submit", "abstain", "fanout")

#: Keys a universe row's entity hash may arrive under. ``entity_hash`` is the
#: clearinghouse spelling; ``query_id`` is the pipeline's, and a ranked file
#: read back carries that one.
_HASH_KEYS: Tuple[str, ...] = ("entity_hash", "query_id")

#: Keys a universe row's definition text may arrive under. ``definition`` is
#: ``function_content``'s column; ``text`` is ``function_to_go``'s field.
_TEXT_KEYS: Tuple[str, ...] = ("definition", "text")


def item_id_of_index(index: int) -> str:
    """The council ``item_id`` for the *index*-th distinct submitted text.

    ``t0``, ``t1``, ... -- the SAME convention
    :meth:`~.ontology_mapping_utils.OntologyMappingUtils.function_to_go_council`
    assigns when it builds its own ``item_of_text`` map over sorted texts, so
    a plan's item ids and a direct council call's item ids agree instead of
    being two numbering schemes for the same items.
    """
    return f"t{index}"


def _first(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    """The first of *keys* present and non-``None`` in *row*."""
    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def done_hashes(done: Iterable[Any]) -> Set[str]:
    """The set of ``entity_hash`` values in a current-state result set.

    Accepts the shape :meth:`ClearinghouseCapability.current_state` returns
    (dict rows carrying ``entity_hash``) and, for convenience in tests and
    callers that have already projected, a plain iterable of hash strings.
    """
    out: Set[str] = set()
    for row in done or ():
        if isinstance(row, str):
            out.add(row)
            continue
        if isinstance(row, Mapping):
            value = _first(row, _HASH_KEYS)
            if value is None:
                raise ValueError(
                    "a `done` row carries no entity_hash; a current-state "
                    f"result row must identify its entity. Row keys: "
                    f"{sorted(row)}"
                )
            out.add(str(value))
            continue
        raise ValueError(
            f"a `done` row is a {type(row).__name__}; expected a result row "
            "mapping or an entity_hash string"
        )
    return out


def unrun_items(universe: Iterable[Any], done: Iterable[Any]) -> List[Dict[str, str]]:
    """``universe - done``, as ``function_to_go`` input rows.

    One ONE definition of the set difference, used twice: stage 1 feeds this
    list to :meth:`~.ontology_mapping_utils.OntologyMappingUtils.function_to_go`
    and :func:`unrun_set` re-derives the same list from the same two inputs.
    Two separate implementations could disagree about which functions were
    ranked and which were partitioned, which would show up as items missing
    from the plan with nothing to explain them.

    Returns rows ``{"query_id", "text", "source"}`` ordered by ``query_id``,
    so the ranked file, the plan's items and the chunk boundaries are all
    reproducible from the same inputs.

    Raises:
        ValueError: A universe row carries no entity hash or no definition,
            or one hash arrives twice with two different definitions (which
            would make "the text of this function" ambiguous, and the plan
            would silently pick one).
    """
    already = done_hashes(done)
    by_hash: Dict[str, Dict[str, str]] = {}
    for row in universe or ():
        if not isinstance(row, Mapping):
            raise ValueError(
                f"a `universe` row is a {type(row).__name__}; expected a "
                "content row mapping with entity_hash and definition"
            )
        raw_hash = _first(row, _HASH_KEYS)
        if raw_hash is None:
            raise ValueError(
                "a `universe` row carries no entity_hash. entity_hash is THE "
                "join key into the clearinghouse and cannot be reconstructed "
                f"from the text. Row keys: {sorted(row)}"
            )
        query_id = str(raw_hash)
        raw_text = _first(row, _TEXT_KEYS)
        if raw_text is None:
            raise ValueError(
                f"universe row {query_id[:12]}... carries no definition; "
                "there is nothing to rank or to send to the council"
            )
        text = str(raw_text)
        source = str(row.get("source") or "")
        seen = by_hash.get(query_id)
        if seen is not None:
            if seen["text"] != text:
                raise ValueError(
                    f"universe hash {query_id[:12]}... arrives twice with two "
                    "different definitions; the clearinghouse's entity hash "
                    "is a function of its content, so this cannot be "
                    "reconciled here. Fix the read, do not guess which text "
                    "is the function's."
                )
            continue
        by_hash[query_id] = {"query_id": query_id, "text": text, "source": source}
    # Validated and de-duplicated FIRST, filtered second: a contradictory
    # universe row must be reported whether or not that function happens to
    # be done already, because the next run's parameter set will plan it.
    return [by_hash[key] for key in sorted(by_hash) if key not in already]


def _ranked_rows(ranked: Any) -> List[Mapping[str, Any]]:
    """The per-item rows of a ``function_to_go`` result, however it arrives."""
    if isinstance(ranked, Mapping):
        rows = ranked.get("results")
        if rows is None:
            raise ValueError(
                "`ranked` is a mapping with no `results` key; pass "
                "function_to_go's return value (or read_ranked's)"
            )
    else:
        rows = ranked
    out: List[Mapping[str, Any]] = []
    for row in rows or ():
        if not isinstance(row, Mapping):
            raise ValueError(
                f"a `ranked` row is a {type(row).__name__}; expected a "
                "function_to_go result row"
            )
        out.append(row)
    return out


def unrun_set(
    universe: Iterable[Any],
    done: Iterable[Any],
    ranked: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    """Partition ``universe - done`` into ``submit`` / ``abstain`` / ``fanout``.

    Pure: no clearinghouse, no council, no I/O. The two clearinghouse reads
    are thin calls AROUND this function, never inside it.

    Args:
        universe: ``function_content`` rows joined to ``function_entity``
            (:meth:`ClearinghouseCapability.universe`), each carrying
            ``entity_hash`` and ``definition``. ``query_id``/``text`` are
            accepted as aliases.
        done: Current-state ``function_result`` rows at this run's
            ``parameter_set_hash`` -- NOT at any other parameter set. Plain
            hash strings are accepted too.
        ranked: ``function_to_go`` output over the un-run set (its return
            value, or :meth:`read_ranked`'s).

    Returns:
        ``{"submit": [...], "abstain": [...], "fanout": [...]}``:

        * ``submit`` -- ``{"item_id", "query_id", "text", "source",
          "candidates"}``, one row per DISTINCT un-run text that retrieval
          did not decline, ordered by text. ``query_id`` is the
          representative (the lowest hash sharing that text) and is the row
          the council's answer is attributed to.
        * ``abstain`` -- ``{"query_id", "text", "source", "abstain_reason",
          "decline_reason"}``, one row per declined un-run function, ordered
          by ``query_id``. ``decline_reason`` is already in the
          ``"abstained:<reason>"`` spelling stage 5 publishes.
        * ``fanout`` -- ``{"query_id", "item_id", "text"}``, one row per
          un-run non-declined function (so a text shared by three
          ``query_id``s yields three rows against one ``submit`` row),
          ordered by ``(text, query_id)``.

        ``submit`` and ``abstain`` are DISJOINT and their union is every
        un-run function; ``fanout`` covers exactly the ``submit`` side of it.

    Raises:
        ValueError: An un-run function has no ranked row. Retrieval is run
            over the un-run set, so a gap means the two inputs disagree --
            and dropping the item instead is the failure this refusal exists
            to prevent (760 definitions went missing that way once).
    """
    items = unrun_items(universe, done)
    by_query_id = {
        str(row.get("query_id")): row
        for row in _ranked_rows(ranked)
        if row.get("query_id") is not None
    }

    missing = [row["query_id"] for row in items if row["query_id"] not in by_query_id]
    if missing:
        raise ValueError(
            f"{len(missing)} of {len(items)} un-run functions have no ranked "
            f"row (first: {missing[0][:12]}...). `ranked` must cover the whole "
            "un-run set -- stage 1 ranks exactly that set, so a gap means the "
            "universe/done reads and the ranking disagree. Re-rank rather "
            "than planning a partition with a hole in it."
        )

    abstain: List[Dict[str, Any]] = []
    #: text -> the rows sharing it, in query_id order
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        row = by_query_id[item["query_id"]]
        reason = row.get("abstain_reason")
        candidates = list(row.get("candidates") or ())
        if row.get("abstained") or not candidates:
            # Retrieval declined, or it did not decline but left nothing to
            # choose from -- which is the same outcome for the council and
            # must be recorded as a decline rather than as an empty menu.
            reason = str(reason or "no candidate survived the GO height filter")
            abstain.append(
                {
                    "query_id": item["query_id"],
                    "text": item["text"],
                    "source": item["source"],
                    "abstain_reason": reason,
                    "decline_reason": f"abstained:{reason}",
                }
            )
            continue
        groups.setdefault(item["text"], []).append(
            {**item, "candidates": candidates}
        )

    submit: List[Dict[str, Any]] = []
    fanout: List[Dict[str, Any]] = []
    for index, text in enumerate(sorted(groups)):
        members = sorted(groups[text], key=lambda row: row["query_id"])
        item_id = item_id_of_index(index)
        representative = members[0]
        submit.append(
            {
                "item_id": item_id,
                "query_id": representative["query_id"],
                "text": text,
                "source": representative["source"],
                "candidates": representative["candidates"],
            }
        )
        fanout.extend(
            {"query_id": member["query_id"], "item_id": item_id, "text": text}
            for member in members
        )

    return {
        "submit": submit,
        "abstain": sorted(abstain, key=lambda row: row["query_id"]),
        "fanout": fanout,
    }
