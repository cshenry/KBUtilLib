"""Shared fixture rows for the six ``current_state_sql`` derivation properties.

This module exists so that exactly ONE set of fixture rows drives both:

- the off-pod DuckDB surrogate tests
  (``tests/berdl/test_clearinghouse_derivation.py``), which prove the
  derivation's *logic* against DuckDB as a stand-in engine, and
- the in-pod parity check documented in
  ``agent-io/docs/clearinghouse-schema-operator-runbook.md`` (OP3), which
  proves the SAME derivation behaves identically against the real
  Spark/Iceberg engine.

Per the task that added this module: the parity check must append "the
same fixture the DuckDB tests use -- import or share it rather than
re-typing, so the two cannot drift apart." A hand-retyped lookalike
fixture in a separate pod script is exactly the drift this module is
built to prevent -- two fixtures that happen to agree today can silently
diverge on the next edit to either one, and a parity check run against a
fixture that no longer matches what the DuckDB tests exercise proves
nothing.

This module is pure: it holds no session, performs no I/O, makes no
network call, and imports nothing pod-only -- exactly like
:mod:`kbutillib.domains.kbase.berdl.clearinghouse_derivation`, which it
is designed to validate. It is safe to import off-pod (by the test
suite) and in-pod (by the OP3 parity script).

Every fixture row's ``source`` carries the :data:`PARITY_SOURCE_PREFIX`
tag (``"parity-check/"``). In the DuckDB surrogate tests this is
cosmetic. In the real, live per-type ``<entity_type>_result`` table it is
load-bearing: OP3 appends these rows to the SAME append-only table real
annotation tools write to (resolved via
:func:`kbutillib.domains.kbase.berdl.clearinghouse_schema.table_name` for
each row's ``entity_type`` -- these protein rows land in
``protein_result``), and the prefix is what keeps them from ever being
mistaken for real tool output and lets an operator find and account for
them later. Per the runbook, these rows are expected to remain in the
table permanently -- the schema is append-only by design -- and this is
harmless, since each occupies its own ``(entity_hash, entity_type,
result_type, source)`` slot and can never shadow or be shadowed by a
real slot. ``entity_type`` is part of the slot key because
``_standardize_protein`` and ``_standardize_gene`` are the same
standardizer, so ``entity_hash`` alone does not uniquely identify an
entity -- the pair ``(entity_hash, entity_type)`` does. It is also, now,
what selects which of the five per-type ``result`` tables a row belongs
to (see :func:`rows_by_entity_type` and :attr:`ParityCase.entity_type`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from kbutillib.domains.kbase.berdl.clearinghouse_schema import ENTITY_TYPES

#: Prefix every parity-check fixture row's ``source`` carries. See the
#: module docstring. ``<property_key>/<tool>`` keeps every property's
#: rows in their own slot even though several properties reuse the same
#: bare tool labels ("toolA", "toolB").
PARITY_SOURCE_PREFIX = "parity-check/"


def fixture_entity_hash(tag: str) -> str:
    """A deterministic stand-in ``entity_hash`` for a fixture tag.

    The lowercase 64-character sha256 hex digest of ``tag`` -- the same
    shape the standardizers emit and the STRING ``entity_hash`` column
    stores, so fixture rows are indistinguishable in form from real ones.
    Deterministic so re-running the parity check (or re-running the test
    suite) always targets the same slot.
    """
    return hashlib.sha256(tag.encode("ascii")).hexdigest()


def fixture_source(property_key: str, tool: str) -> str:
    """Build a ``parity-check/``-prefixed, per-property, per-tool source.

    e.g. ``fixture_source("source_isolation", "toolA")`` ->
    ``"parity-check/source_isolation/toolA"``.
    """
    return f"{PARITY_SOURCE_PREFIX}{property_key}/{tool}"


@dataclass(frozen=True)
class ParityCase:
    """One of the six current-state derivation properties, with its rows.

    Attributes:
        property_key: A short, stable identifier for this property (used
            to namespace its fixture rows' ``source`` values and to label
            PASS/FAIL output in the OP3 parity check).
        description: One-line human-readable statement of the property
            under test, suitable for a PASS/FAIL log line.
        entity_hash: The fixture ``entity_hash`` this case's rows share.
        result_type: The fixture ``result_type`` this case's rows share.
        sources: Every distinct ``source`` value this case's rows use, in
            the order first introduced. A parity check (or a DuckDB test)
            queries ``current_state_sql(..., sources=list(sources))`` to
            scope its read to exactly this case's rows.
        rows: The fixture rows to insert/append, each shaped as the
            keyword arguments for a ``result``-table row: ``entity_hash``
            (64-char lowercase hex str), ``entity_type``, ``result_type``, ``source``,
            ``result_type_version``, ``payload`` (a plain ``dict``, not
            yet JSON-encoded), ``observed_at``, ``ingest_batch_id``.
    """

    property_key: str
    description: str
    entity_hash: str
    result_type: str
    sources: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]

    @property
    def entity_type(self) -> str:
        """The single ``entity_type`` every row in this case shares.

        Under the fifteen per-entity-type-table scheme, a case's rows are
        appended to, and queried from, the ``<entity_type>_result`` table
        resolved via
        :func:`kbutillib.domains.kbase.berdl.clearinghouse_schema.table_name`.
        Every row in a single :class:`ParityCase` targets one slot family
        and therefore one entity type; this property surfaces it so the
        parity check can resolve the per-type result table without
        re-deriving the type at each call site.

        Raises:
            ValueError: If this case's rows carry more than one distinct
                ``entity_type`` (a fixture-construction error -- a single
                case must not straddle two per-type result tables).
        """
        types = {row["entity_type"] for row in self.rows}
        if len(types) != 1:
            raise ValueError(
                f"ParityCase {self.property_key!r} spans multiple "
                f"entity_types {sorted(types)!r}; a case must target exactly "
                "one per-type result table."
            )
        return next(iter(types))


_RESULT_TYPE = "annotation"

# --------------------------------------------------------------------------
# Property 1 -- duplicate appends collapse to exactly one current row.
# --------------------------------------------------------------------------
_DUPLICATE_COLLAPSE_SOURCE = fixture_source("duplicate_collapse", "toolA")
DUPLICATE_COLLAPSE = ParityCase(
    property_key="duplicate_collapse",
    description="three identical appends collapse to exactly one current row",
    entity_hash=fixture_entity_hash("parity-dup"),
    result_type=_RESULT_TYPE,
    sources=(_DUPLICATE_COLLAPSE_SOURCE,),
    rows=tuple(
        {
            "entity_hash": fixture_entity_hash("parity-dup"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _DUPLICATE_COLLAPSE_SOURCE,
            "result_type_version": "v1",
            "payload": {"ns": {"k": "same"}},
            "observed_at": f"2026-01-01 00:0{i}:00",
            "ingest_batch_id": f"01HPARITY0DUP0000000000{i}",
        }
        for i in range(3)
    ),
)

# --------------------------------------------------------------------------
# Property 2 -- the newest row (by observed_at) wins per slot.
# --------------------------------------------------------------------------
_NEWEST_WINS_SOURCE = fixture_source("newest_wins", "toolA")
NEWEST_WINS = ParityCase(
    property_key="newest_wins",
    description="the row with the later observed_at is the current one",
    entity_hash=fixture_entity_hash("parity-newest"),
    result_type=_RESULT_TYPE,
    sources=(_NEWEST_WINS_SOURCE,),
    rows=(
        {
            "entity_hash": fixture_entity_hash("parity-newest"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _NEWEST_WINS_SOURCE,
            "result_type_version": "v1",
            "payload": {"ns": {"k": "old"}},
            "observed_at": "2026-01-01 00:00:00",
            "ingest_batch_id": "01HPARITYNEWEST0000000AA",
        },
        {
            "entity_hash": fixture_entity_hash("parity-newest"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _NEWEST_WINS_SOURCE,
            "result_type_version": "v1",
            "payload": {"ns": {"k": "new"}},
            "observed_at": "2026-01-02 00:00:00",
            "ingest_batch_id": "01HPARITYNEWEST0000000AB",
        },
    ),
)

# --------------------------------------------------------------------------
# Property 3 -- an observed_at tie is broken by the greater ingest_batch_id.
# --------------------------------------------------------------------------
_TIE_BREAK_SOURCE = fixture_source("ingest_batch_id_tie_break", "toolA")
INGEST_BATCH_ID_TIE_BREAK = ParityCase(
    property_key="ingest_batch_id_tie_break",
    description=(
        "an observed_at tie is broken by the greater (later) ingest_batch_id"
    ),
    entity_hash=fixture_entity_hash("parity-tie"),
    result_type=_RESULT_TYPE,
    sources=(_TIE_BREAK_SOURCE,),
    rows=(
        {
            "entity_hash": fixture_entity_hash("parity-tie"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _TIE_BREAK_SOURCE,
            "result_type_version": "v1",
            "payload": {"ns": {"k": "lower_batch"}},
            "observed_at": "2026-01-01 00:00:00",
            "ingest_batch_id": "01HPARITYTIE00000000000AA",
        },
        {
            "entity_hash": fixture_entity_hash("parity-tie"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _TIE_BREAK_SOURCE,
            "result_type_version": "v1",
            "payload": {"ns": {"k": "higher_batch"}},
            "observed_at": "2026-01-01 00:00:00",  # identical timestamp
            "ingest_batch_id": "01HPARITYTIE00000000000AZ",
        },
    ),
)

# --------------------------------------------------------------------------
# Property 4 -- a term absent from the newer whole-call payload is absent
# from current state (results are one row per call, not one row per term).
# --------------------------------------------------------------------------
_TERM_REMOVAL_SOURCE = fixture_source("term_removal", "toolA")
TERM_REMOVAL = ParityCase(
    property_key="term_removal",
    description="a key dropped in the newer payload is absent from current state",
    entity_hash=fixture_entity_hash("parity-removal"),
    result_type=_RESULT_TYPE,
    sources=(_TERM_REMOVAL_SOURCE,),
    rows=(
        {
            "entity_hash": fixture_entity_hash("parity-removal"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _TERM_REMOVAL_SOURCE,
            "result_type_version": "v1",
            "payload": {"go": {"term": "GO:0001"}, "ec": {"term": "1.1.1.1"}},
            "observed_at": "2026-01-01 00:00:00",
            "ingest_batch_id": "01HPARITYREMOVAL0000000BA",
        },
        {
            "entity_hash": fixture_entity_hash("parity-removal"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _TERM_REMOVAL_SOURCE,
            "result_type_version": "v1",
            "payload": {"go": {"term": "GO:0001"}},  # 'ec' namespace dropped
            "observed_at": "2026-01-02 00:00:00",
            "ingest_batch_id": "01HPARITYREMOVAL0000000BB",
        },
    ),
)

# --------------------------------------------------------------------------
# Property 5 -- rows from one source never shadow another source's row for
# the same entity_hash/result_type, and a `sources` filter prunes to
# exactly the requested sources.
# --------------------------------------------------------------------------
_SOURCE_ISOLATION_SOURCE_A = fixture_source("source_isolation", "toolA")
_SOURCE_ISOLATION_SOURCE_B = fixture_source("source_isolation", "toolB")
SOURCE_ISOLATION = ParityCase(
    property_key="source_isolation",
    description=(
        "two sources annotating the same entity both remain current, and "
        "a sources filter prunes to exactly the requested sources"
    ),
    entity_hash=fixture_entity_hash("parity-isolation"),
    result_type=_RESULT_TYPE,
    sources=(_SOURCE_ISOLATION_SOURCE_A, _SOURCE_ISOLATION_SOURCE_B),
    rows=(
        {
            "entity_hash": fixture_entity_hash("parity-isolation"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _SOURCE_ISOLATION_SOURCE_A,
            "result_type_version": "v1",
            "payload": {"ns": {"k": "from_a"}},
            "observed_at": "2026-01-01 00:00:00",
            "ingest_batch_id": "01HPARITYISOLATION00000CA",
        },
        {
            "entity_hash": fixture_entity_hash("parity-isolation"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _SOURCE_ISOLATION_SOURCE_B,
            "result_type_version": "v1",
            "payload": {"ns": {"k": "from_b"}},
            "observed_at": "2026-01-01 00:00:00",
            "ingest_batch_id": "01HPARITYISOLATION00000CA",
        },
    ),
)

# --------------------------------------------------------------------------
# Property 6 -- rows differing only in result_type_version still collapse
# to one current row (result_type_version is outside the slot key).
# --------------------------------------------------------------------------
_VERSION_SOURCE = fixture_source("result_type_version_outside_slot_key", "toolA")
RESULT_TYPE_VERSION_OUTSIDE_SLOT_KEY = ParityCase(
    property_key="result_type_version_outside_slot_key",
    description=(
        "rows differing only in result_type_version collapse to one "
        "current row -- the version never forks a slot"
    ),
    entity_hash=fixture_entity_hash("parity-version"),
    result_type=_RESULT_TYPE,
    sources=(_VERSION_SOURCE,),
    rows=(
        {
            "entity_hash": fixture_entity_hash("parity-version"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _VERSION_SOURCE,
            "result_type_version": "v1",
            "payload": {"ns": {"k": "old_version"}},
            "observed_at": "2026-01-01 00:00:00",
            "ingest_batch_id": "01HPARITYVERSION0000000EA",
        },
        {
            "entity_hash": fixture_entity_hash("parity-version"),
            "entity_type": "protein",
            "result_type": _RESULT_TYPE,
            "source": _VERSION_SOURCE,
            "result_type_version": "v2",  # schema bump, same slot key
            "payload": {"ns": {"k": "new_version"}},
            "observed_at": "2026-01-02 00:00:00",
            "ingest_batch_id": "01HPARITYVERSION0000000EB",
        },
    ),
)

#: All six properties, in the order stated by the PRD/task: duplicate
#: collapse, newest-wins, ingest_batch_id tie-break, term removal, source
#: isolation, result_type_version outside the slot key.
PARITY_CASES: tuple[ParityCase, ...] = (
    DUPLICATE_COLLAPSE,
    NEWEST_WINS,
    INGEST_BATCH_ID_TIE_BREAK,
    TERM_REMOVAL,
    SOURCE_ISOLATION,
    RESULT_TYPE_VERSION_OUTSIDE_SLOT_KEY,
)

#: Every ``source`` value used by any fixture row, in case a caller wants
#: to append/query the whole fixture set in one pass (e.g. OP3's initial
#: append step) rather than case by case.
ALL_PARITY_SOURCES: tuple[str, ...] = tuple(
    source for case in PARITY_CASES for source in case.sources
)

#: Every fixture row across all six properties, flattened, in the same
#: order as :data:`PARITY_CASES`.
ALL_PARITY_ROWS: tuple[dict[str, Any], ...] = tuple(
    row for case in PARITY_CASES for row in case.rows
)


def rows_by_entity_type() -> dict[str, tuple[dict[str, Any], ...]]:
    """Group :data:`ALL_PARITY_ROWS` by each row's ``entity_type``.

    Under the fifteen per-entity-type-table scheme, the parity fixture is no
    longer a single ``result`` table's worth of rows: each row belongs in
    its own ``<entity_type>_result`` table (resolved via
    :func:`kbutillib.domains.kbase.berdl.clearinghouse_schema.table_name`).
    This helper returns exactly the rows destined for each type's table,
    keyed by ``entity_type``.

    Iteration is over :data:`ENTITY_TYPES` (the schema module's canonical,
    ordered tuple), never a local literal list, so the returned dict's keys
    appear in the same order the schema declares and adding a sixth entity
    type upstream needs no edit here. Only entity types that actually appear
    in the fixture rows are included -- a type with no fixture rows is
    omitted rather than mapped to an empty tuple, so a caller iterating the
    result never targets a table it has nothing to append.

    Returns:
        A dict mapping each ``entity_type`` present in :data:`ALL_PARITY_ROWS`
        to the tuple of its rows, in :data:`ENTITY_TYPES` order.
    """
    grouped: dict[str, tuple[dict[str, Any], ...]] = {}
    for entity_type in ENTITY_TYPES:
        rows = tuple(
            row for row in ALL_PARITY_ROWS if row["entity_type"] == entity_type
        )
        if rows:
            grouped[entity_type] = rows
    return grouped
