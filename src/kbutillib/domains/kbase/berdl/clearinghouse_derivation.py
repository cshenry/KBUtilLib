"""Current-state SQL derivation over the clearinghouse's append-only ``result`` table.

The ``result`` table (:mod:`kbutillib.domains.kbase.berdl.clearinghouse_schema`)
is APPEND-ONLY: every annotation call by every tool is written as an
immutable row, and a superseded row is never deleted or updated in place.
The BERDL platform cannot create persistent database views there (the
Trino view path is broken, not merely absent), so "the current state" of
a slot cannot be materialized as a view -- it has to be a query this
module builds, and every consumer that reads ``result`` directly without
running that query does not get a stale answer, it gets a **wrong** one,
because every superseded row is still sitting in the table.

THE SEMANTICS. Current state for a slot -- a slot being one
``(entity_hash, entity_type, result_type, source, parameter_set_hash)``
5-tuple -- is the row with the greatest ``(observed_at,
ingest_batch_id)`` within that slot.

``parameter_set_hash`` is part of the slot key because a result is keyed
by tool, tool version AND the parameters the caller set: two parameter
sets run through one tool version are two DISTINCT current results, both
current at once, not one overwriting the other. ``source`` carries only
``<tool>/<version>``, so without ``parameter_set_hash`` in the key the
same protein run through the same tool version at a different threshold
-- or with TRANSYT's required NCBI taxonomy id -- would land in the
default run's slot and supersede it, silently destroying the default
answer and returning a parameterised one in its place to every caller
that never asked for it. A default run is the empty parameter set ``{}``,
whose hash is
:data:`~kbutillib.domains.identity.DEFAULT_PARAMETER_SET_HASH`, so
default runs still share one slot with each other.

``entity_type`` is part of the slot key because ``_standardize_protein``
and ``_standardize_gene`` are the same standardizer (a bare
``_clean_sequence_letters``), so a sequence over the alphabet
{A,C,G,T,N} -- every letter of which is also a valid IUPAC amino-acid
code -- produces a byte-identical ``entity_hash`` whether submitted as a
protein or as gene DNA; identity is the pair ``(entity_hash,
entity_type)``, never ``entity_hash`` alone, and a slot key that omitted
``entity_type`` would let a protein row and a gene row silently
shadow each other. The same collision reaches genomes: a SINGLE-contig
genome hashes identically to that one sequence submitted as a gene or a
protein, because ``"|".join(["X"]) == "X"`` -- so ``entity_hash("genome",
["ACGTACGT"]) == entity_hash("gene", "ACGTACGT") == entity_hash("protein",
"ACGTACGT")`` -- a third reason the slot key must carry ``entity_type`` and
a lookup must never key on ``entity_hash`` alone. This module implements
the derivation with a window
function: ``ROW_NUMBER() OVER (PARTITION BY entity_hash, entity_type,
result_type, source, parameter_set_hash ORDER BY observed_at DESC,
ingest_batch_id DESC)``, keeping only rows where that number is ``1``.

Two things are deliberate and must never be "fixed":

- ``ingest_batch_id`` in the ``ORDER BY`` is not optional. Two appends can
  share an ``observed_at``, and without the second key "the current row"
  is genuinely ambiguous. It is a ULID assigned per commit, so it is
  monotonic and lexicographically ordered, and comparing it as an
  ordinary string ``ORDER BY`` column is correct.
- ``result_type_version`` is recorded on every row but is **not** part of
  the slot key. If it were, bumping a result-type schema would fork every
  slot in the corpus and nothing would ever supersede its predecessor
  again.

DIALECT AUTHORITY IS SPARK SQL, compatible with Iceberg-on-Spark. Trino
is a read client only and is not authoritative for SEMANTICS -- but it is
authoritative for its own SYNTAX, and that distinction was missed once
already at real cost. Table identifiers are quoted per dot-separated
segment, and the quote CHARACTER follows the ``engine`` argument: Spark
gets backticks (matching
:meth:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability.load`,
e.g. ```` `namespace`.`table` ````), Trino gets double quotes. Emitting
backticks to Trino is not a dialect preference, it is a hard error
(``SYNTAX_ERROR: backquoted identifiers are not supported``), and because
this module hardcoded backticks until 2026-09-28 every Trino-routed read
verb built on it failed on the pod. Callers MUST pass the same engine they
execute under. The emitted SQL stays inside the
DuckDB/Spark syntax intersection deliberately: plain ``ROW_NUMBER() OVER
(...)``, ordinary ``WHERE``/``IN``, no ``NULLS FIRST``/``NULLS LAST`` (both
ordering columns are non-null by construction -- a row missing either is
rejected before write, so encoding null-handling here would be encoding a
state that must never reach the table), and no DuckDB-only BLOB literal
or timestamp-helper syntax.

PER-TYPE RESULT TABLES. In the fifteen-table per-entity-type clearinghouse
scheme, ``result_table_fqn`` names a **per-type** result table such as
``gene_result`` or ``protein_result``, not a single shared ``result``
table. This module needs no code change for that: :func:`current_state_sql`
already takes ``result_table_fqn`` as a parameter and interpolates it as
given, and the ``<type>_result`` schema is GENERIC (byte-identical across
all five types), so the derived column list and window logic are the same
whichever per-type table is named. A consequence is that the
``entity_types`` filter parameter is REDUNDANT-BUT-HARMLESS when the named
table is already single-typed: every row in ``gene_result`` already has
``entity_type = 'gene'``, so filtering on it prunes nothing further. It is
retained rather than removed because it stays correct and useful if this
derivation is pointed at a cross-type source (e.g. a UNION-ALL view over
several ``<type>_result`` tables), and removing it would narrow a merged,
working interface for no benefit.

:func:`current_state_sql` is pure: it holds no session, performs no I/O,
makes no network call, and imports nothing pod-only, which is what makes
it testable off-pod. This module's own test suite
(``tests/berdl/test_clearinghouse_derivation.py``) executes the SQL this
function returns against DuckDB as a SURROGATE engine -- DuckDB proves the
*logic* of the derivation (which row wins a slot, which scope excludes
which), not the Spark-on-Iceberg dialect the query actually runs under in
production. Whether this exact SQL text is valid and performant against
the live Spark-on-Iceberg cluster is an in-pod operator parity check, not
something provable off-pod.
"""

from __future__ import annotations

import re

from .clearinghouse_schema import ENTITY_TYPES, table_configs, table_name

__all__ = ["current_state_sql"]

#: The slot key: current state is one row per unique combination of these
#: columns. ``entity_type`` is included because ``entity_hash`` alone does
#: not identify an entity (see the module docstring); ``result_type_version``
#: is deliberately excluded -- also see the module docstring.
#:
#: ``entity_type`` is DELIBERATELY RETAINED here even though, in the
#: fifteen-table per-type scheme, a single ``<type>_result`` table holds
#: exactly one ``entity_type`` value -- which makes it constant within any
#: one table and therefore redundant in this window key. Redundant is not
#: incorrect: keeping it produces the same partitioning on a single-typed
#: table and stays correct if this derivation is ever pointed at a
#: cross-type source (e.g. a UNION-ALL view). Do NOT "clean it up" by
#: dropping it -- that would be a gratuitous change to merged, working code.
#:
#: ``parameter_set_hash`` closes the tuple: two parameter sets run through
#: one tool version are two DISTINCT current results, not one overwriting
#: the other. See the module docstring's SEMANTICS paragraph.
_SLOT_KEY_COLUMNS = (
    "entity_hash",
    "entity_type",
    "result_type",
    "source",
    "parameter_set_hash",
)

#: The columns that break ties within a slot, in ``ORDER BY`` precedence.
#: Both are non-null by construction; see the module docstring for why
#: this module never emits ``NULLS FIRST``/``NULLS LAST``.
_ORDER_COLUMNS = ("observed_at", "ingest_batch_id")


def _quote_fqn(fqn: str, engine: str = "spark") -> str:
    """Quote each dot-separated segment of a table name, per ENGINE.

    Every segment between dots gets its own pair of quotes, so a
    multi-part identifier such as ``tenant.namespace.table`` is never
    misread with a dot inside a segment as a qualifier boundary. A
    segment that already arrives quoted is not double-quoted.

    The quote CHARACTER is dialect-specific, and getting it wrong is not
    cosmetic: Trino rejects backquoted identifiers outright
    (``SYNTAX_ERROR: backquoted identifiers are not supported; use double
    quotes to quote identifiers``), while Spark/Hive require backticks.
    This function hardcoded backticks until 2026-09-28, which made every
    Trino-routed read verb fail -- silently in ``stats`` (row counts
    rendered as em-dashes) and loudly in ``results``.

    Args:
        fqn: A dot-separated table name, e.g.
            ``"kbaseincubator.clearinghouse.result"``. Segments may
            already be quoted.
        engine: ``"trino"`` selects double quotes; anything else
            (``"spark"``, the default) selects backticks. Callers pass
            the SAME resolved engine they hand to the query seam, so the
            quoting cannot disagree with the dialect the SQL runs under.

    Returns:
        The fully quoted identifier -- e.g. under ``"spark"``
        ```` `kbaseincubator`.`clearinghouse`.`result` ````, and under
        ``"trino"`` ``"kbaseincubator"."clearinghouse"."result"``.
    """
    quote = '"' if engine == "trino" else "`"
    # Both quote styles are stripped whatever the engine. Held in a local
    # rather than written inline as segment.strip('`\"'): a backslash inside
    # an f-string EXPRESSION is a SyntaxError before Python 3.12 (PEP 701
    # relaxed it), and this package declares requires-python >= 3.9. Inlined,
    # it makes the whole berdl package unimportable on 3.9-3.11 -- which on a
    # 3.11 pod reads as "capability unavailable", i.e. silently off-pod, to
    # any caller catching Exception. Do not fold this back into the f-string.
    existing_quotes = '`"'
    return ".".join(
        f"{quote}{segment.strip(existing_quotes)}{quote}" for segment in fqn.split(".")
    )


def _result_columns() -> list[str]:
    """Column names of a ``<type>_result`` table, in DDL order.

    Derived from :func:`clearinghouse_schema.table_configs` rather than
    re-typed here, so this module and the schema module cannot silently
    drift apart -- per the task's instruction to reuse the previous
    phase's table/column names rather than re-typing them as string
    literals. The result schema is GENERIC (byte-identical across all five
    ``<type>_result`` tables in the fifteen-table scheme), so any one of
    them defines the column list this derivation reads; the first entity
    type's result table is used as that canonical reference.
    """
    reference_result = table_name(ENTITY_TYPES[0], "result")
    configs = {table["name"]: table for table in table_configs()}
    schema_sql = configs[reference_result]["schema_sql"]
    return [part.strip().split(" ", 1)[0] for part in schema_sql.split(",")]


def _quote_literal(value: str) -> str:
    """Single-quote a value for embedding in the ``WHERE`` clause.

    Used for the ``source``, ``entity_type`` and ``parameter_set_hash``
    filter values. All are internal, operator-controlled strings
    (``source`` is ``<tool>/<version>`` per ``clearinghouse_schema``'s
    partitioning note; ``entity_type`` is one of the five stored,
    schema-recognized values; ``parameter_set_hash`` is checked against the
    64-lowercase-hex alphabet by :func:`_validated_parameter_set_hashes`
    before it ever reaches here) -- never end-user input -- so literal
    embedding with the standard SQL single-quote escape (``'`` -> ``''``) is
    the correct choice here: :func:`current_state_sql` returns SQL *text*
    and nothing else, so there is no side channel through which a separate
    bind-parameter list could travel back to the caller.
    """
    return "'" + value.replace("'", "''") + "'"


#: The stored form of a ``parameter_set_hash``: a 64-character LOWERCASE hex
#: sha256 digest, exactly as
#: :func:`kbutillib.domains.identity.parameter_set_hash` returns it.
_PARAMETER_SET_HASH_RE = re.compile(r"[0-9a-f]{64}")


def _validated_parameter_set_hashes(values: list[str]) -> list[str]:
    """Check every ``parameter_set_hash`` filter value, or raise.

    This function's whole output is SQL TEXT and it BINDS NOTHING (see
    :func:`_quote_literal`), so a filter value is embedded into the
    statement rather than parameterised. ``parameter_set_hash`` has one
    legal form -- 64 lowercase hex characters -- so validating against it
    here is both a correctness check and the thing that keeps literal
    embedding safe: a value that passes contains no quote, no whitespace
    and no SQL-significant character at all.

    Rejecting an UPPERCASE digest is deliberate, not pedantry. STRING
    equality is case-sensitive in both Spark and Trino, so an uppercase
    digest is a syntactically fine predicate that silently matches zero
    stored rows -- a reader would conclude the parameter set has no results
    and fail open into recomputing work already done. Lowercasing it here
    instead of raising would hide a caller that is not going through
    :func:`kbutillib.domains.identity.parameter_set_hash`.

    Args:
        values: The caller's ``parameter_set_hashes`` list (non-empty; the
            empty-list case is a whole-query short-circuit handled by
            :func:`current_state_sql`).

    Returns:
        ``values`` unchanged, once every element has passed.

    Raises:
        ValueError: Any element is not a ``str``, or is not exactly 64
            lowercase hex characters. The message names the offending
            value and its position.
    """
    for index, value in enumerate(values):
        if not isinstance(value, str) or not _PARAMETER_SET_HASH_RE.fullmatch(value):
            raise ValueError(
                f"current_state_sql: parameter_set_hashes[{index}] is not a "
                f"valid parameter_set_hash: {value!r}. Expected exactly 64 "
                "LOWERCASE hex characters, as returned by "
                "kbutillib.domains.identity.parameter_set_hash(). An "
                "uppercase digest is rejected rather than lowercased, "
                "because STRING comparison is case-sensitive and an "
                "uppercase value would match zero stored rows instead of "
                "failing."
            )
    return values


def _in_predicate(column: str, values: list[str]) -> str:
    """Build one ``<column> IN (...)`` predicate over quoted values.

    Callers only invoke this for a non-empty ``values`` list; the
    empty-list ("match nothing") case is handled by the caller as a
    whole-query ``1 = 0`` short-circuit rather than as an ``IN ()``
    predicate (which is not universally valid SQL and, more importantly,
    is not the semantics the caller wants -- see :func:`current_state_sql`).
    """
    in_list = ", ".join(_quote_literal(value) for value in values)
    return f"{column} IN ({in_list})"


def current_state_sql(
    result_table_fqn: str,
    *,
    sources: list[str] | None = None,
    entity_types: list[str] | None = None,
    parameter_set_hashes: list[str] | None = None,
    engine: str = "spark",
) -> str:
    """Build the SQL that derives clearinghouse current state.

    The body is dialect-neutral (a plain ``ROW_NUMBER()`` window); only
    identifier quoting differs, which is what ``engine`` selects. Callers
    that route this to Trino MUST pass ``engine="trino"`` -- see
    :func:`_quote_fqn`.

    Selects, from the append-only ``result`` table, exactly one row per
    ``(entity_hash, entity_type, result_type, source,
    parameter_set_hash)`` slot: the row with the greatest ``(observed_at,
    ingest_batch_id)`` in that slot. See the module docstring for the full
    semantics and the deliberate choices (``entity_type`` and
    ``parameter_set_hash`` included in the slot key, ``ingest_batch_id``
    as a mandatory tie-break, ``result_type_version`` excluded from the
    slot key) that must not be "fixed".

    Args:
        result_table_fqn: The ``result`` table's fully qualified name,
            dot-separated (e.g. ``"kbaseincubator.clearinghouse.result"``).
            Each segment is backtick-quoted in the emitted SQL.
        sources: If given, pre-filters the underlying rows to only these
            ``source`` values before windowing (``source`` is a partition
            column on ``result``, so this is a pruning filter, not merely
            a convenience one). ``None`` (the default) applies no filter.
            An empty list is a request to match zero sources.
        entity_types: If given, pre-filters the underlying rows to only
            these ``entity_type`` values before windowing (``entity_type``
            is also a partition column on ``result``). ``None`` (the
            default) applies no filter. An empty list is a request to
            match zero entity types.
        parameter_set_hashes: If given, pre-filters the underlying rows to
            only these ``parameter_set_hash`` values before windowing.
            EXACTLY the same semantics as ``sources``: ``None`` (the
            default) applies no filter, an empty list is a request to
            match zero parameter sets, and a non-empty list becomes a
            ``parameter_set_hash IN (...)`` predicate. Unlike ``source``
            this is NOT a partition column (a hash prunes nothing; see
            ``clearinghouse_schema``'s module docstring), so it is a
            row-level filter rather than a pruning one -- which changes
            its performance, never its meaning.

        When more than one of ``sources``, ``entity_types`` and
        ``parameter_set_hashes`` is given, the filters compose with
        ``AND`` (never ``OR`` -- ``OR`` would silently broaden the read
        and destroy the pruning these parameters exist for). An empty list
        on *any* filter yields ``WHERE 1 = 0`` for the whole query, rather
        than combining an ``1 = 0`` guard with another filter's ``IN``
        predicate.

    Returns:
        SQL text implementing the derivation. Performs no I/O; holds no
        session; makes no network call.

    Raises:
        ValueError: Any ``parameter_set_hashes`` element is not exactly 64
            lowercase hex characters -- see
            :func:`_validated_parameter_set_hashes`. This function returns
            SQL text and binds nothing, so filter values are embedded as
            literals and are validated before embedding.
    """
    columns = _result_columns()
    select_list = ",\n        ".join(columns)
    partition_by = ", ".join(_SLOT_KEY_COLUMNS)
    order_by = ", ".join(f"{col} DESC" for col in _ORDER_COLUMNS)
    table = _quote_fqn(result_table_fqn, engine)

    # Validated BEFORE the match-nothing short-circuit, so a malformed hash
    # raises whether or not another filter would have emptied the query --
    # a caller passing a bad digest alongside sources=[] still hears about
    # the bad digest rather than silently getting a '1 = 0' query.
    if parameter_set_hashes:
        _validated_parameter_set_hashes(parameter_set_hashes)

    matches_nothing = (
        (sources is not None and len(sources) == 0)
        or (entity_types is not None and len(entity_types) == 0)
        or (parameter_set_hashes is not None and len(parameter_set_hashes) == 0)
    )

    if matches_nothing:
        where_clause = "\n    WHERE 1 = 0"
    else:
        predicates = []
        if sources is not None:
            predicates.append(_in_predicate("source", sources))
        if entity_types is not None:
            predicates.append(_in_predicate("entity_type", entity_types))
        if parameter_set_hashes is not None:
            predicates.append(
                _in_predicate("parameter_set_hash", parameter_set_hashes)
            )
        if predicates:
            where_clause = "\n    WHERE " + " AND ".join(predicates)
        else:
            where_clause = ""

    return (
        "WITH ranked AS (\n"
        "    SELECT\n"
        f"        {select_list},\n"
        "        ROW_NUMBER() OVER (\n"
        f"            PARTITION BY {partition_by}\n"
        f"            ORDER BY {order_by}\n"
        "        ) AS rn\n"
        f"    FROM {table}"
        f"{where_clause}\n"
        ")\n"
        f"SELECT {select_list}\n"
        "FROM ranked\n"
        "WHERE rn = 1"
    )
