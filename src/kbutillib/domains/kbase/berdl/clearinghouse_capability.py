"""``ClearinghouseCapability`` -- the READ half of the sanctioned clearinghouse API.

This module is the ONE place a caller reads the fifteen-table
``kbaseincubator.clearinghouse`` lakehouse
(:mod:`kbutillib.domains.kbase.berdl.clearinghouse_schema`). Every read verb
below resolves its own table name through
:func:`clearinghouse_schema.table_name`, encodes every hash through
:func:`clearinghouse_schema.encode_entity_hash`, and derives current state
by wrapping :func:`clearinghouse_derivation.current_state_sql`. No caller
should ever construct a clearinghouse table name, format a hash into SQL, or
hand-roll the current-state window function -- that is what this class is
for, and the whole value of the abstraction is that it exists in exactly one
place.

COMPOSITION, NOT INHERITANCE. ``ClearinghouseCapability`` is constructed
OVER a :class:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability`
(accepting one, or constructing one lazily) and deliberately does NOT
subclass it. A subclass would expose raw ``query()`` and ``load()`` on the
same object as the clearinghouse verbs, which is an open invitation for a
caller to reach past the abstraction and run its own un-encoded,
un-parameterised SQL against the tables. Wrapping keeps the raw transport
surface off this object entirely -- the same wrap-not-subclass posture
:class:`~kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter.ClearinghouseBootstrapCapability`
takes for the write-bootstrap path.

WHY THE HARD RULES EXIST -- each one guards a specific, measured failure:

Rule 1 -- ``entity_type`` is a mandatory positional argument on every read
verb except :meth:`stats`, :meth:`tables` and :meth:`content_all_types`.
There is no verb that reads "the clearinghouse" without naming a type,
because in the fifteen-table scheme every physical table is single-typed:
``gene_entity`` and ``protein_entity`` are different tables, and a read that
did not name a type could not know which one to query. The three exceptions
are the cross-cutting verbs: :meth:`stats` and :meth:`tables` report on ALL
fifteen tables by construction, and :meth:`content_all_types` reads the
explicitly-lossy ``all_content`` UNION-ALL view.

Rule 2 -- table names come ONLY from :func:`clearinghouse_schema.table_name`,
which raises ``ValueError`` on an unknown type or kind. No verb accepts a
table name from a caller. Concatenating ``f"{entity_type}_{kind}"`` anywhere
but that resolver is how the config generator and the readers silently
drift; routing every name through one function is what makes that
impossible.

Rule 3 -- a batch carrying more than one entity type is SPLIT PER TYPE before
querying, or raises; it is never probed as one set. This is the load-bearing
rule, and it exists because ``_standardize_protein`` and
``_standardize_gene`` are literally the SAME function
(:mod:`kbutillib.domains.identity.standardizers`): a DNA-alphabet sequence
over {A,C,G,T,N} -- every letter of which is also a valid IUPAC amino-acid
code -- hashes byte-identically whether submitted as a protein or as gene
DNA. Identity is therefore the pair ``(entity_hash, entity_type)``, never
``entity_hash`` alone. A batch probed as one set against a single table
would let a protein row and a gene row with a colliding hash shadow each
other; the entity type is exactly what selects the correct physical table,
so it must be resolved before -- not after -- the query. Chunking happens
strictly WITHIN a type.

Rule 4 -- every hash goes through :func:`encode_entity_hash` and is bound as
a query PARAMETER, never formatted into SQL text. Two reasons, one of them
subtle. The obvious one: string-formatted SQL is an injection surface. The
subtle one, and the reason this rule is a *correctness* rule and not merely
a hygiene rule: the ``entity_hash`` column is a 64-character lowercase hex
``STRING`` (never ``BINARY`` -- ``data_lakehouse_ingest`` rejects ``BINARY``
outright, dev 1219), and ``STRING`` equality is CASE-SENSITIVE. An uppercase
hex digest and the stored lowercase form are different values by every
comparison. A dedup/known-set probe bound with an uppercase digest matches
ZERO rows, concludes the clearinghouse is empty for that corpus, and FAILS
OPEN into recomputing work already done -- silently, plausibly, and
expensively. :func:`encode_entity_hash` normalises to the stored lowercase
form; running every hash through it before it can reach a query is what
closes that trap. See :data:`_HEX64_RE` for the off-pod degradation and why
it stays safe.

Rule 5 -- :meth:`current_state` and :meth:`results` WRAP
:func:`clearinghouse_derivation.current_state_sql`, resolving
``result_table_fqn`` themselves via :func:`table_name`. They do not
hand-roll the window function, and they do NOT pass ``entity_types`` to it:
in the per-type scheme a ``<type>_result`` table already holds exactly one
entity type, so the ``entity_types`` filter is redundant-but-harmless there
and the derivation builder is left untouched.

ENGINE SELECTION -- see :data:`SPARK_PROMOTION_THRESHOLD` and
:data:`OFFPOD_PAGE_CAP` for the measured rationale behind the Trino-vs-Spark
default per verb and the off-pod paging cap. Every verb takes ``engine=``
and passes a caller-supplied value through UNCHANGED; the defaults below
apply only when the caller passes ``None``.

OFF-POD IS UNPROVEN END TO END. :class:`~kbutillib.domains.kbase.berdl.transports.OffPodTransport`'s
own docstring records that off-pod access is "currently unproven end to
end", and at the time this module was written no off-pod clearinghouse read
had ever actually been performed against the live tables -- every
verification in this family (OP0, OP2, OP2R, OP3) ran inside the pod. The
whole "works off-pod, degrades rather than fails" posture rests on that
unproven assumption; this module implements the degradation carefully but
cannot, off the pod, prove the live path works. See the task work-record for
the explicit statement of what was and was not verified.

This module performs no I/O at import time, imports no pod-only package at
module scope, and is safe to import in ordinary CI -- the collaborating
:class:`BerdlCapability` defers every pod dependency to first use.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .capability import BerdlCapability, BerdlLoadRefusedError
from .clearinghouse_derivation import current_state_sql
from .clearinghouse_schema import (
    _KINDS,
    ENTITY_TYPES,
    NAMESPACE,
    TENANT,
    encode_entity_hash,
    table_name,
)

__all__ = [
    "ClearinghouseCapability",
    "ClearinghouseWriteTargetMismatchError",
    "ClearinghouseLoadPostflightError",
    "ClearinghouseLedgerAmbiguousError",
]

#: Fully-qualified namespace prefix the fifteen tables live under, e.g.
#: ``"kbaseincubator.clearinghouse"``. Every FQN this module builds is
#: ``f"{_FQN_PREFIX}.{table_name(...)}"``; the derivation and schema modules
#: quote each dot-separated segment of it themselves.
_FQN_PREFIX = f"{TENANT}.{NAMESPACE}"

#: The one cross-type content view :meth:`content_all_types` reads -- the
#: explicitly-lossy ``all_content`` UNION-ALL (shared columns plus an
#: ``entity_type`` discriminator; see
#: :func:`clearinghouse_schema.union_view_sql`).
_ALL_CONTENT_VIEW = "all_content"

#: Matches EXACTLY a 64-character lowercase hex run, anchored end to end.
#: Every value that has passed :func:`encode_entity_hash` matches this, and a
#: value that matches this cannot contain a quote, whitespace, comment marker
#: or any other SQL metacharacter. That property is what makes the off-pod
#: degradation in :meth:`_bind_offpod` safe: off-pod there is no bind-
#: parameter channel (the REST query endpoint takes only SQL text), so a hash
#: must reach the SQL string -- but only after :func:`encode_entity_hash` has
#: produced it AND this pattern has re-confirmed it, so the value inlined is
#: provably injection-free. In-pod the hash never touches SQL text at all
#: (it is bound server-side via the Trino DB-API cursor).
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

#: Matches ANY 64+-character hex run anywhere in a string, case-insensitive.
#: Used only by tests-facing invariants and internal assertions that no SQL
#: text emitted on the in-pod (parameterised) path carries a hash inline; the
#: readable, anchored :data:`_HEX64_RE` is what guards the off-pod binding.
_HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{64,}")

#: Bulk-lookup size at or above which a hash-filtered read PROMOTES from
#: Trino to Spark. Below it, Trino wins; at/above it, Spark is at worst even
#: and the window/partition-scan shape Spark handles better starts to matter.
#:
#: THE MEASUREMENTS, and why the obvious reading of them is wrong. An earlier
#: spike measured a DISTINCT dedup probe at both loci:
#:
#:     off-pod:  LIMIT-10 control  trino 3.06s / spark  9.98s
#:               K=4000            trino 26.64s / spark 17.08s
#:     in-pod:   K=1000            trino 2.25s / spark 18.32s
#:               K=4000            trino 5.79s / spark  4.86s
#:
#: The often-quoted "spark wins ~35% on the dedup shape" is the OFF-POD row.
#: IN-POD the picture INVERTS below K~4000 -- trino beats spark EIGHTFOLD at
#: K=1000 and spark leads only marginally at K=4000. Hence a threshold near
#: 4000 rather than "always Spark for bulk". TWO caveats travel with these
#: numbers and must not be dropped: (1) the spike itself downgraded "cost is
#: essentially fixed in K" to UNPROVEN -- the timings are dominated by
#: per-query warmup and want a warmup-controlled rerun; and (2) every cell
#: was measured against a 203M-row table, while the clearinghouse entity
#: tables are sized at ~1B (protein) to ~4B (gene). Do not present these
#: numbers, or this threshold, as firmer than they are.
SPARK_PROMOTION_THRESHOLD = 4000

#: Off-pod page size this module pages with, one page at a time, controlling
#: ``limit``/``offset`` itself.
#:
#: THE VALUE IS INHERITED FROM AN EARLIER SPIKE'S MEASUREMENT AND IS ENFORCED
#: BY NO CODE WE OWN. Off-pod reads go through
#: :meth:`~kbutillib.domains.kbase.berdl.transports.OffPodTransport.query`,
#: which delegates the HTTP call to ``KBBERDLUtils.query`` with a server-side
#: page cap. That cap's value is NOT encoded anywhere in this repository -- a
#: grep of the berdl package finds no such constant (the only ``5000`` there
#: is an unrelated corpus-skew ratio in a docstring). An older PRD asserted
#: "5000 rows" as settled; this module adopts that number as its self-imposed
#: page size so it never depends on a server default it cannot see, and pages
#: with an explicit ``limit``/``offset`` it controls. Because the true cap is
#: unknown, :meth:`_page_offpod` keeps paging whenever a page comes back
#: EXACTLY FULL -- a full page is evidence of nothing (not of truncation and
#: not against it) -- and stops only on a SHORT page. A truncated
#: "already-known" set would fail open into recomputing done work, the same
#: failure mode as the uppercase-hash trap (Rule 4).
OFFPOD_PAGE_CAP = 5000

#: Name of the JSON-lines run ledger :meth:`ClearinghouseCapability.ingest_shards`
#: writes beside the shards. One line per ``(table, batch)`` records the
#: outcome of that shard's ingest, so a killed run can resume against the same
#: ``run_id`` without re-ingesting a shard already at ``"ingested"`` (see the
#: ``ingest_shards`` docstring's resumability contract).
LEDGER_FILENAME = "ingest_ledger.jsonl"

#: The three ledger states a ``(table, batch)`` line can carry.
#: ``"started"`` is written BEFORE the ingest call and is deliberately
#: non-terminal: a ``"started"`` line with no matching ``"ingested"``/``"failed"``
#: line means the process died mid-ingest, which is AMBIGUOUS and refused
#: (unless ``reconcile=True``). ``"ingested"`` is the only skip-on-resume state.
_LEDGER_STARTED = "started"
_LEDGER_INGESTED = "ingested"
_LEDGER_FAILED = "failed"


class ClearinghouseWriteTargetMismatchError(RuntimeError):
    """Raised when the write-target namespace cannot be SHOWN to match the probe.

    This is the dev 1206 guard, and it FAILS CLOSED. Dev 1206 measured, on-pod,
    that ``BerdlCapability.load()`` resolved table existence against namespace
    ``"default"`` (its parameter default) while ``data_lakehouse_ingest``
    derived its own target as ``tenant.dataset`` and ignored the parameter --
    so the existence probe read a namespace the write never touched, always
    found nothing, and ``select_write_mode("append", table_exists=False)``
    returned ``"overwrite"``. Three rows were destroyed with ``success=true``
    and no warning.

    Before any :meth:`ClearinghouseCapability.register` or
    :meth:`ClearinghouseCapability.ingest_shards` call reaches ``load()``,
    the write-target namespace and the existence-probe namespace are resolved
    EXPLICITLY and compared. If the two cannot be SHOWN to agree -- either
    resolves to a falsy value, or they differ -- this is raised and NO write
    is attempted (``load()`` is called exactly zero times). It is a refusal,
    never a warning and never a fall-back-to-a-mode, because the verb runs
    unattended for days and a silent ``append`` -> ``overwrite`` over a
    populated corpus is unrecoverable short of a multi-terabyte replay.

    It is a distinct type (not a generic transport error) so a caller can
    distinguish "the write target is unsafe, do not retry blindly" from a
    transient transport failure.
    """


class ClearinghouseLoadPostflightError(RuntimeError):
    """Raised when a ``load()`` reported success but its postflight is null.

    Dev 1194: ``BerdlCapability.load()`` returns ``success=true`` with
    ``row_count`` and ``new_snapshot_id`` set to ``None`` when postflight
    cannot read the table back -- the real error is buried in the report's
    ``row_count_error``. A null postflight is therefore treated as a FAILED
    load, not a successful one.

    IMPORTANT DIRECTION-OF-ERROR: because the rows may have ALREADY LANDED
    before the postflight read failed, a load can be reported failed AFTER the
    data is actually in the table. That is the correct direction to be wrong
    in -- reporting a real write as failed is recoverable (reconcile), while
    reporting a failed/destructive write as successful (the dev 1206 failure)
    is not. The operator must therefore RECONCILE against the table's Iceberg
    snapshot history rather than blindly re-running, since a blind re-run of a
    ``<type>_content`` shard leaves two rows for one ``entity_hash`` with no
    derivation rule to collapse them (see :meth:`ClearinghouseCapability.ingest_shards`).
    """


class ClearinghouseLedgerAmbiguousError(RuntimeError):
    """Raised when a ledger has a ``"started"`` batch with no terminal line.

    A ``(table, batch)`` at state ``"started"`` with no subsequent
    ``"ingested"`` or ``"failed"`` line means the process died mid-ingest: the
    shard may have landed fully, partially, or not at all, and the ledger
    cannot say which. Continuing past it blindly risks a duplicate
    ``<type>_content`` row (which has no current-state derivation to collapse
    it -- see :meth:`ClearinghouseCapability.ingest_shards`). So resume
    REFUSES at the first such batch unless ``reconcile=True``, which reads the
    table's Iceberg snapshot history and decides whether the shard landed.
    """


class ClearinghouseCapability:
    """Read-only, locus-aware access to the fifteen clearinghouse tables.

    Composed over a :class:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability`
    (never subclassing it -- see the module docstring). Constructing this
    object performs no I/O and requires no pod package; the collaborating
    capability defers every pod dependency to first use.

    Args:
        capability: An existing ``BerdlCapability`` (or any object exposing
            the same ``locus()``/``query()`` surface -- tests inject a
            fake). When ``None``, one is constructed lazily on first use
            from ``off_pod_kwargs``.
        off_pod_kwargs: Forwarded to :class:`BerdlCapability` when it is
            constructed lazily (ignored if ``capability`` is given).
    """

    def __init__(
        self,
        capability: Any = None,
        *,
        off_pod_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._capability = capability
        self._off_pod_kwargs = dict(off_pod_kwargs) if off_pod_kwargs else {}

    # -- collaborator / locus --------------------------------------------

    def _get_capability(self) -> Any:
        """Return the wrapped capability, constructing one lazily if needed."""
        if self._capability is None:
            self._capability = BerdlCapability(off_pod_kwargs=self._off_pod_kwargs)
        return self._capability

    def _locus(self) -> str:
        """Resolve the collaborator's execution locus (``'in_pod'``/``'off_pod'``)."""
        return self._get_capability().locus()

    # -- hash + engine helpers -------------------------------------------

    @staticmethod
    def _encode_hashes(hashes: Sequence[str | bytes]) -> list[str]:
        """Encode every hash to the stored lowercase-hex form, at the boundary.

        Runs each value through :func:`encode_entity_hash`, which raises
        ``ValueError`` on anything that is not a 64-character hex string (any
        case) or 32 raw bytes -- so a malformed digest raises HERE, before
        any SQL is built or any query is issued (Rule 4). Order is preserved
        and duplicates are NOT collapsed here; de-duplication of results is a
        reassembly concern (see :meth:`_run_hash_lookup`).
        """
        return [encode_entity_hash(value) for value in hashes]

    def _resolve_engine(
        self, requested: str | None, *, default: str, hash_count: int = 0
    ) -> str:
        """Pick the engine for a verb, honouring an explicit caller value.

        A non-``None`` ``requested`` is passed straight through -- the
        caller's choice always wins. Otherwise the verb's ``default`` applies,
        except that a hash-filtered lookup whose batch is at or above
        :data:`SPARK_PROMOTION_THRESHOLD` promotes to ``'spark'`` (see that
        constant's docstring for the measured rationale).
        """
        if requested is not None:
            return requested
        if hash_count >= SPARK_PROMOTION_THRESHOLD:
            return "spark"
        return default

    # -- the query seam ---------------------------------------------------

    def _run(
        self,
        sql: str,
        *,
        params: Sequence[str] | None = None,
        engine: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Run one query through the collaborator and normalise its rows.

        In-pod: hands ``params`` to ``capability.query(sql, params=..., ...)``
        for true server-side binding -- the hash never reaches the SQL text.
        Off-pod: there is no bind-parameter channel, so ``params`` are
        inlined via :meth:`_bind_offpod` (each re-validated to
        ``^[0-9a-f]{64}$`` first, so the inlined value is provably
        injection-free) and ``limit``/``offset`` are forwarded to the REST
        page.

        Returns a list of ``dict`` rows, normalised across the loci's return
        shapes (off-pod returns ``{'data': [dict, ...]}``; in-pod Trino
        returns dict rows keyed by column name; in-pod Spark returns ``Row``
        objects).
        """
        capability = self._get_capability()
        if self._locus() == "off_pod":
            final_sql = self._bind_offpod(sql, params)
            result = capability.query(final_sql, limit=limit, offset=offset)
            return self._normalize_offpod_rows(result)
        # In-pod: bind server-side; paging is unnecessary (no REST cap).
        rows = capability.query(
            sql, params=list(params) if params else None, engine=engine
        )
        return self._normalize_inpod_rows(rows)

    @staticmethod
    def _bind_offpod(sql: str, params: Sequence[str] | None) -> str:
        """Inline positional ``?`` bind values off-pod, safely, or refuse.

        Off-pod the REST query endpoint carries no bind-parameter channel, so
        a parameterised query cannot be bound server-side. Rather than fail
        the read (the task's posture is "degrade rather than fail" off-pod),
        each ``?`` placeholder in ``sql`` is replaced, left to right, with the
        corresponding single-quoted parameter -- but ONLY after that parameter
        is re-confirmed to match :data:`_HEX64_RE`. Because an encoded
        ``entity_hash`` is provably ``^[0-9a-f]{64}$``, the inlined literal
        cannot contain a quote or any SQL metacharacter, so this substitution
        introduces no injection surface (see Rule 4 in the module docstring).

        Raises:
            ValueError: A parameter does not match :data:`_HEX64_RE`, or the
                number of ``?`` placeholders does not equal the number of
                parameters. This module only ever binds encoded hashes off-
                pod; anything else reaching here is a programming error and is
                refused rather than inlined.
        """
        if not params:
            if "?" in sql:
                raise ValueError(
                    "_bind_offpod: SQL has '?' placeholders but no params given."
                )
            return sql
        for value in params:
            if not _HEX64_RE.match(value):
                raise ValueError(
                    "_bind_offpod: refusing to inline a non-hex parameter "
                    f"off-pod ({value!r}); only encoded 64-char hex hashes "
                    "may be inlined, and only after re-validation."
                )
        placeholder_count = sql.count("?")
        if placeholder_count != len(params):
            raise ValueError(
                f"_bind_offpod: {placeholder_count} '?' placeholders but "
                f"{len(params)} params."
            )
        out = sql
        for value in params:
            out = out.replace("?", f"'{value}'", 1)
        return out

    @staticmethod
    def _normalize_offpod_rows(result: Any) -> list[dict[str, Any]]:
        """Normalise an off-pod REST result to a list of dict rows.

        ``OffPodTransport.query`` returns ``{'success', 'data', 'columns',
        'row_count', ...}``. This raises on an unsuccessful result (so a
        failed read never masquerades as an empty one -- another fail-open
        trap) and returns ``result['data']`` (already a list of dict rows).
        """
        if isinstance(result, dict):
            if result.get("success") is False:
                raise RuntimeError(
                    f"clearinghouse read failed off-pod: {result.get('error')!r}"
                )
            data = result.get("data", [])
            return list(data)
        # A transport that already returns a plain list of rows.
        return list(result)

    @staticmethod
    def _normalize_inpod_rows(rows: Any) -> list[dict[str, Any]]:
        """Normalise in-pod rows to dict rows.

        ``BerdlCapability.query`` already returns dict rows for the in-pod
        Trino path (keyed by ``cursor.description`` column names); Spark
        ``Row`` exposes ``asDict()``. Dicts pass through unchanged. A bare
        tuple (a fake yielding positional rows, or a description-less cursor)
        is wrapped positionally as a last resort so nothing is dropped.
        """
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            elif hasattr(row, "asDict"):
                out.append(row.asDict())
            else:
                out.append({str(i): value for i, value in enumerate(row)})
        return out

    def _page_offpod(
        self,
        sql: str,
        *,
        params: Sequence[str] | None,
        engine: str,
    ) -> list[dict[str, Any]]:
        """Run one off-pod query to completion, paging until a SHORT page.

        Pages with an explicit ``limit=OFFPOD_PAGE_CAP``/``offset`` this
        method controls, accumulating rows. It keeps paging while a page
        comes back EXACTLY :data:`OFFPOD_PAGE_CAP` rows long -- a full page is
        evidence of nothing, so stopping there could silently truncate -- and
        stops only when a page shorter than the cap arrives (or an empty page
        after the first). See :data:`OFFPOD_PAGE_CAP`.
        """
        collected: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._run(
                sql,
                params=params,
                engine=engine,
                limit=OFFPOD_PAGE_CAP,
                offset=offset,
            )
            collected.extend(page)
            if len(page) < OFFPOD_PAGE_CAP:
                break
            offset += OFFPOD_PAGE_CAP
        return collected

    # -- hash-batch chunking / reassembly --------------------------------

    def _chunk_size(self) -> int | None:
        """Chunk size for a hash IN-list, picked from the RESOLVED locus.

        Off-pod, a fully-known lookup returns one row per hash, so whatever
        the server's page cap is caps the batch -- chunk at
        :data:`OFFPOD_PAGE_CAP`. In-pod there is no such cap, so a batch is
        sent whole (``None`` -- do not chunk). The size is picked from the
        live locus, NOT from a constant at the call site, so a caller passing
        50000 hashes gets one reassembled answer and never sees a page
        boundary either way.
        """
        return OFFPOD_PAGE_CAP if self._locus() == "off_pod" else None

    def _run_hash_lookup(
        self,
        *,
        sql_for: "callable[[int], str]",
        encoded: list[str],
        engine: str,
    ) -> list[dict[str, Any]]:
        """Run a hash-filtered read over a (possibly chunked) batch, reassembled.

        ``sql_for(n)`` builds the SQL for an ``IN`` predicate carrying ``n``
        ``?`` placeholders; ``encoded`` is the full list of already-encoded
        hashes to bind. Off-pod the batch is split into chunks of
        :meth:`_chunk_size` and each chunk paged to completion; in-pod the
        whole batch is one query. Rows are concatenated across chunks/pages,
        then de-duplicated on identity so a caller passing 50000 hashes gets
        exactly one answer with no page-boundary duplicates.
        """
        chunk = self._chunk_size()
        off_pod = self._locus() == "off_pod"
        rows: list[dict[str, Any]] = []
        if chunk is None:
            batches = [encoded]
        else:
            batches = [encoded[i : i + chunk] for i in range(0, len(encoded), chunk)]
        for batch in batches:
            if not batch:
                continue
            sql = sql_for(len(batch))
            if off_pod:
                rows.extend(self._page_offpod(sql, params=batch, engine=engine))
            else:
                rows.extend(self._run(sql, params=batch, engine=engine))
        return _dedupe_rows(rows)

    # -- FQN helpers ------------------------------------------------------

    def _fqn(self, entity_type: str, kind: str) -> str:
        """Return the dot-separated FQN of one clearinghouse table.

        Resolves the bare name through :func:`table_name` (Rule 2 -- the
        ONLY table-name constructor; raises on an unknown type/kind) and
        prepends the namespace prefix. The derivation/schema modules quote
        each segment; this method returns the unquoted dotted form they
        expect.
        """
        return f"{_FQN_PREFIX}.{table_name(entity_type, kind)}"

    @staticmethod
    def _in_placeholders(n: int) -> str:
        """Return ``"?, ?, ..."`` with ``n`` positional placeholders."""
        return ", ".join(["?"] * n)

    # -- public read surface ---------------------------------------------

    def known(
        self,
        entity_type: str,
        hashes: Sequence[str | bytes],
        *,
        engine: str | None = None,
    ) -> list[str]:
        """Return the subset of ``hashes`` present in ``<type>_entity``.

        A point-lookup membership probe against one entity table. Every hash
        is encoded (Rule 4) and bound as a parameter; the entity type selects
        the table (Rule 1/3). Returns the encoded hashes that were found, in
        no particular order and de-duplicated.

        Default engine: Trino (point lookups; startup cost dominates), with
        automatic promotion to Spark at/above
        :data:`SPARK_PROMOTION_THRESHOLD` hashes. Pass ``engine=`` to force a
        choice.
        """
        encoded = self._encode_hashes(hashes)
        if not encoded:
            return []
        resolved_engine = self._resolve_engine(
            engine, default="trino", hash_count=len(encoded)
        )
        table = _quote_fqn(self._fqn(entity_type, "entity"), resolved_engine)

        def sql_for(n: int) -> str:
            return (
                f"SELECT entity_hash FROM {table} "
                f"WHERE entity_hash IN ({self._in_placeholders(n)})"
            )

        rows = self._run_hash_lookup(
            sql_for=sql_for, encoded=encoded, engine=resolved_engine
        )
        return [row["entity_hash"] for row in rows if "entity_hash" in row]

    def content(
        self,
        entity_type: str,
        hashes: Sequence[str | bytes],
        *,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return FULL type-specialized content rows from ``<type>_content``.

        Returns the WHOLE row for each found hash -- not a shared subset.
        ``genome_content.fasta_reference`` is the entire reason that table
        exists, and it must be reachable through this sanctioned API; that is
        why this verb selects ``*`` from the per-type content table rather
        than the shared columns :meth:`content_all_types` unions.

        Default engine: Trino, promoting to Spark at/above
        :data:`SPARK_PROMOTION_THRESHOLD` hashes.
        """
        encoded = self._encode_hashes(hashes)
        if not encoded:
            return []
        resolved_engine = self._resolve_engine(
            engine, default="trino", hash_count=len(encoded)
        )
        table = _quote_fqn(self._fqn(entity_type, "content"), resolved_engine)

        def sql_for(n: int) -> str:
            return (
                f"SELECT * FROM {table} "
                f"WHERE entity_hash IN ({self._in_placeholders(n)})"
            )

        return self._run_hash_lookup(
            sql_for=sql_for, encoded=encoded, engine=resolved_engine
        )

    def content_all_types(
        self,
        hashes: Sequence[str | bytes],
        *,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return rows from the explicitly-lossy ``all_content`` UNION-ALL view.

        This is the cross-type content read: the ``all_content`` view carries
        only the columns common to all five content tables plus an
        ``entity_type`` discriminator (see
        :func:`clearinghouse_schema.union_view_sql`) -- it reconciles NONE of
        the type-specialized columns (``sequence``, ``fasta_reference``,
        ``definition``, ...). It is a separate, explicitly named verb and is
        NEVER the default; a caller wanting a full row must name a type and
        call :meth:`content`. Takes no ``entity_type`` (Rule 1 exception) --
        crossing types is its whole purpose.

        Default engine: Trino, promoting to Spark at/above
        :data:`SPARK_PROMOTION_THRESHOLD` hashes.
        """
        encoded = self._encode_hashes(hashes)
        if not encoded:
            return []
        resolved_engine = self._resolve_engine(
            engine, default="trino", hash_count=len(encoded)
        )
        view = _quote_fqn(f"{_FQN_PREFIX}.{_ALL_CONTENT_VIEW}", resolved_engine)

        def sql_for(n: int) -> str:
            return (
                f"SELECT * FROM {view} "
                f"WHERE entity_hash IN ({self._in_placeholders(n)})"
            )

        return self._run_hash_lookup(
            sql_for=sql_for, encoded=encoded, engine=resolved_engine
        )

    def results(
        self,
        entity_type: str,
        hashes: Sequence[str | bytes],
        *,
        sources: list[str] | None = None,
        result_types: list[str] | None = None,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return current-state results for specific hashes in ``<type>_result``.

        Wraps :func:`clearinghouse_derivation.current_state_sql` (Rule 5 --
        the window function is never hand-rolled), resolving the result table
        FQN itself via :func:`table_name`, then filters the derived current
        state to the given hashes (encoded, parameter-bound) and, optionally,
        the given ``result_types``. ``sources`` is pushed into the derivation
        builder as a pruning pre-filter (``source`` is a partition column on
        ``<type>_result``). ``entity_types`` is deliberately NOT passed to the
        builder: a ``<type>_result`` table is already single-typed, so it
        would be redundant (Rule 5).

        Default engine: Trino, promoting to Spark at/above
        :data:`SPARK_PROMOTION_THRESHOLD` hashes.
        """
        encoded = self._encode_hashes(hashes)
        if not encoded:
            return []
        resolved_engine = self._resolve_engine(
            engine, default="trino", hash_count=len(encoded)
        )
        inner = current_state_sql(
            self._fqn(entity_type, "result"),
            sources=sources,
            engine=resolved_engine,
        )

        def sql_for(n: int) -> str:
            predicates = [f"entity_hash IN ({self._in_placeholders(n)})"]
            if result_types is not None:
                predicates.append(_in_literal("result_type", result_types))
            where = " AND ".join(predicates)
            return f"SELECT * FROM (\n{inner}\n) AS current_state\nWHERE {where}"

        return self._run_hash_lookup(
            sql_for=sql_for, encoded=encoded, engine=resolved_engine
        )

    def current_state(
        self,
        entity_type: str,
        *,
        sources: list[str] | None = None,
        result_types: list[str] | None = None,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return current state over the whole ``<type>_result`` table.

        Wraps :func:`clearinghouse_derivation.current_state_sql` (Rule 5)
        over the entire per-type result table -- no hash filter. ``sources``
        is pushed into the builder (partition pruning); ``result_types``, if
        given, filters the derived rows. ``entity_types`` is not passed to
        the builder (redundant on a single-typed table -- Rule 5).

        Default engine: Spark. A whole-table current-state derivation is a
        window function over a partition scan, which is Spark's shape rather
        than Trino's (see :data:`SPARK_PROMOTION_THRESHOLD`). Off-pod there
        is no Spark, so the read still routes through the off-pod REST
        transport and pages to completion.
        """
        resolved_engine = self._resolve_engine(engine, default="spark")
        inner = current_state_sql(
            self._fqn(entity_type, "result"),
            sources=sources,
            engine=resolved_engine,
        )
        if result_types is not None:
            sql = (
                f"SELECT * FROM (\n{inner}\n) AS current_state\n"
                f"WHERE {_in_literal('result_type', result_types)}"
            )
        else:
            sql = inner
        if self._locus() == "off_pod":
            return self._page_offpod(sql, params=None, engine=resolved_engine)
        return self._run(sql, params=None, engine=resolved_engine)

    def sources(
        self,
        entity_type: str | None = None,
        *,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return distinct ``source`` values with row counts, per result table.

        With ``entity_type`` given, aggregates one ``<type>_result`` table;
        with ``entity_type=None``, aggregates all five (rows carry an
        ``entity_type`` column so the caller can tell them apart). Row counts
        come from ``COUNT(*)`` grouped by ``source`` -- an explicit count,
        never a transport ``row_count`` field (see :meth:`stats`).

        ``entity_type`` is optional here (a Rule 1 exception is not needed --
        the parameter is present, just nullable) because "sources across every
        result table" is a meaningful, bounded cross-type aggregate.

        Default engine: Trino (a small grouped aggregate).
        """
        resolved_engine = self._resolve_engine(engine, default="trino")
        types = ENTITY_TYPES if entity_type is None else (entity_type,)
        out: list[dict[str, Any]] = []
        for etype in types:
            table = _quote_fqn(self._fqn(etype, "result"), resolved_engine)
            sql = (
                f"SELECT '{etype}' AS entity_type, source, COUNT(*) AS row_count "
                f"FROM {table} GROUP BY source"
            )
            if self._locus() == "off_pod":
                out.extend(self._page_offpod(sql, params=None, engine=resolved_engine))
            else:
                out.extend(self._run(sql, params=None, engine=resolved_engine))
        return out

    def stats(
        self,
        *,
        include_files: bool = False,
        engine: str | None = None,
    ) -> dict[str, Any]:
        """Return per-table row counts for all fifteen tables.

        ROW COUNTS ARE DERIVED FROM AN EXPLICIT ``SELECT COUNT(*)`` per
        table, NEVER from a transport result's ``row_count`` field. Off-pod,
        ``row_count`` is the MATERIALIZED PAGE SIZE, not a table row count --
        an earlier spike observed both ``LIMIT 10`` control cells reporting
        ``rows=5000``. A stats verb built the obvious way would report the
        page size for all fifteen tables and look entirely plausible doing
        it, and that wrong number would land in a dashboard indistinguishable
        from a right one. So this verb reads ``COUNT(*)`` out of the result
        ROW, not out of any envelope field.

        Args:
            include_files: When ``True``, additionally reports Iceberg
                data-file counts and average sizes per table. This is
                IN-POD ONLY (it reads Iceberg ``.files`` metadata tables via
                Spark). Off-pod it is not available: the result is returned
                WITHOUT the file fields and with a non-empty ``warnings``
                list -- never silently partial data that looks complete.
            engine: Default is Spark when ``include_files`` (Iceberg
                metadata tables are Spark-side); Trino otherwise. Passed
                through unchanged when given.

        Returns:
            A dict with ``'tables'`` (one entry per table -- ``'name'``,
            ``'entity_type'``, ``'kind'``, ``'row_count'``, and, in-pod with
            ``include_files``, ``'data_file_count'``/``'avg_file_size_bytes'``)
            and ``'warnings'`` (empty unless a requested capability was
            unavailable at this locus).
        """
        off_pod = self._locus() == "off_pod"
        want_files = include_files and not off_pod
        resolved_engine = self._resolve_engine(
            engine, default="spark" if want_files else "trino"
        )
        warnings: list[str] = []
        if include_files and off_pod:
            warnings.append(
                "include_files is in-pod only (it reads Iceberg .files "
                "metadata tables via Spark); file counts and sizes are "
                "omitted off-pod. Run stats(include_files=True) in the pod "
                "for those fields."
            )

        table_reports: list[dict[str, Any]] = []
        for kind in _KINDS:
            for etype in ENTITY_TYPES:
                table = _quote_fqn(self._fqn(etype, kind), resolved_engine)
                count_sql = f"SELECT COUNT(*) AS row_count FROM {table}"
                rows = self._run(count_sql, params=None, engine=resolved_engine)
                report: dict[str, Any] = {
                    "name": table_name(etype, kind),
                    "entity_type": etype,
                    "kind": kind,
                    "row_count": _scalar(rows, "row_count"),
                }
                if want_files:
                    files_table = _quote_fqn(f"{self._fqn(etype, kind)}.files", resolved_engine)
                    files_sql = (
                        "SELECT COUNT(*) AS data_file_count, "
                        "AVG(file_size_in_bytes) AS avg_file_size_bytes "
                        f"FROM {files_table}"
                    )
                    file_rows = self._run(
                        files_sql, params=None, engine=resolved_engine
                    )
                    report["data_file_count"] = _scalar(file_rows, "data_file_count")
                    report["avg_file_size_bytes"] = _scalar(
                        file_rows, "avg_file_size_bytes"
                    )
                table_reports.append(report)

        return {"tables": table_reports, "warnings": warnings}

    def tables(self, *, engine: str | None = None) -> list[dict[str, Any]]:
        """Return the fifteen tables with kind, entity_type, partition spec, count.

        A catalog-shaped listing: one entry per table with its ``name``,
        ``kind``, ``entity_type``, the ``partition_by`` this config declares
        (from :func:`clearinghouse_schema.table_configs`), and a
        ``row_count`` derived from an explicit ``COUNT(*)`` (same discipline
        as :meth:`stats`). Takes no ``entity_type`` (Rule 1 exception -- it
        lists ALL fifteen).

        Default engine: Trino.
        """
        from .clearinghouse_schema import table_configs  # local: pure, no pod dep

        resolved_engine = self._resolve_engine(engine, default="trino")
        configs = {t["name"]: t for t in table_configs()}
        out: list[dict[str, Any]] = []
        for kind in _KINDS:
            for etype in ENTITY_TYPES:
                name = table_name(etype, kind)
                config = configs[name]
                partition_by = config.get("partition_by")
                if partition_by is None:
                    partition_spec: list[str] = []
                elif isinstance(partition_by, str):
                    partition_spec = [partition_by]
                else:
                    partition_spec = list(partition_by)
                table = _quote_fqn(self._fqn(etype, kind), resolved_engine)
                rows = self._run(
                    f"SELECT COUNT(*) AS row_count FROM {table}",
                    params=None,
                    engine=resolved_engine,
                )
                out.append(
                    {
                        "name": name,
                        "kind": kind,
                        "entity_type": etype,
                        "partition_by": partition_spec,
                        "row_count": _scalar(rows, "row_count"),
                    }
                )
        return out

    # -- write surface ----------------------------------------------------

    def _require_in_pod(self, verb: str) -> None:
        """Refuse a write verb off-pod BEFORE any transport call is made.

        Every write verb on this class is in-pod only: ``load()`` needs a
        Spark session, which exists only inside the BERDL pod. This resolves
        the locus and raises :class:`BerdlLoadRefusedError` off-pod -- the same
        exception ``BerdlCapability.load()`` itself raises -- but does so at
        THIS boundary, before ``register`` or ``ingest_shards`` touches the
        collaborator's ``load()`` (or, in ``ingest_shards``, before it even
        resolves namespaces or opens the ledger). So an off-pod write refuses
        with nothing staged and no transport call attempted (success criterion
        (d)).
        """
        if self._locus() != "in_pod":
            raise BerdlLoadRefusedError(
                f"ClearinghouseCapability.{verb}() is in-pod only: it writes "
                "through BerdlCapability.load(), which requires a Spark session "
                "that exists only inside the BERDL JupyterHub pod. This process "
                "is off-pod, so nothing has been staged and no transport call "
                "has been made. Run this write from inside the pod."
            )

    def _assert_write_target(self, *, dataset: str, tenant: str) -> str:
        """Resolve and CONFIRM the write-target namespace before any load().

        This is the dev 1206 guard, and it fails closed. It asks the wrapped
        capability for two namespaces:

        - the namespace ``data_lakehouse_ingest`` will actually WRITE to
          (``resolve_write_namespace``), and
        - the namespace the pre-write existence PROBE reads
          (``resolve_probe_namespace``).

        and refuses unless the two can be SHOWN to be the same non-empty
        value. If they differ -- exactly the dev 1206 regression, where the
        probe read ``"default"`` while the write went to ``tenant.dataset`` --
        or if either resolves to a falsy value (so agreement cannot be shown),
        it raises :class:`ClearinghouseWriteTargetMismatchError` and returns
        without the caller ever reaching ``load()``.

        Both resolvers are read-only and must not themselves write or probe
        table existence; they only compute the namespace strings. The real
        ``BerdlCapability`` does not expose them today (mirroring how
        ``bootstrap()`` requires ``table_exists``/``table_partition_spec`` that
        only the adapter provides); a real adapter must implement them so that
        the probe and the write provably read/write the identical namespace.

        Returns:
            The single confirmed write-target namespace, for the caller to pass
            through to ``load()`` so the write lands exactly where the guard
            confirmed.

        Raises:
            ClearinghouseWriteTargetMismatchError: The two namespaces cannot be
                shown to agree (they differ, or either is falsy). A guard that
                cannot confirm it probed the same name it is about to write to
                has established nothing, and writing anyway is the failure this
                exists to prevent -- so it refuses rather than proceeding.
        """
        capability = self._get_capability()
        resolve_write = getattr(capability, "resolve_write_namespace", None)
        resolve_probe = getattr(capability, "resolve_probe_namespace", None)
        if resolve_write is None or resolve_probe is None:
            raise ClearinghouseWriteTargetMismatchError(
                "ClearinghouseCapability cannot confirm the write target: the "
                "wrapped capability does not expose both "
                "'resolve_write_namespace' and 'resolve_probe_namespace', so "
                "the namespace the ingest writes to cannot be SHOWN to match "
                "the namespace the existence probe reads (dev 1206). Refusing "
                "rather than writing blind; no load() has been called."
            )
        write_ns = resolve_write(dataset=dataset, tenant=tenant)
        probe_ns = resolve_probe(dataset=dataset, tenant=tenant)
        if not write_ns or not probe_ns or write_ns != probe_ns:
            raise ClearinghouseWriteTargetMismatchError(
                "ClearinghouseCapability refuses to write: the existence-probe "
                f"namespace ({probe_ns!r}) and the ingest write-target "
                f"namespace ({write_ns!r}) could not be shown to agree for "
                f"dataset={dataset!r} tenant={tenant!r}. This is the dev 1206 "
                "regression -- a probe against a namespace the write does not "
                "touch always finds nothing, so an 'append' silently becomes a "
                "destructive 'overwrite'. No load() has been called and nothing "
                "has been staged. Fail closed: resolve the namespace mismatch "
                "before retrying."
            )
        return write_ns

    @staticmethod
    def _check_postflight(load_result: dict[str, Any]) -> dict[str, Any]:
        """Treat a null postflight row_count as a FAILED load (dev 1194).

        ``BerdlCapability.load()`` returns ``success=true`` with per-table
        ``row_count``/``new_snapshot_id`` set to ``None`` when postflight
        cannot read the table back; the real error is buried in the table
        report's ``row_count_error``. This inspects every per-table report and
        raises :class:`ClearinghouseLoadPostflightError` if any table's
        ``row_count`` is ``None`` -- because a load that could not verify its
        rows landed is a failed load, regardless of the transport's
        ``success`` flag.

        Returns:
            ``load_result`` unchanged when every table postflight is non-null.

        Raises:
            ClearinghouseLoadPostflightError: Any table reports a ``None``
                ``row_count``. NOTE this may fire AFTER the rows actually
                landed -- the correct direction to be wrong in (reconcile, do
                not blindly re-run).
        """
        bad: list[str] = []
        for report in load_result.get("tables", []):
            if report.get("row_count") is None:
                detail = report.get("row_count_error") or "no row_count reported"
                bad.append(f"{report.get('name')!r} ({detail})")
        if bad:
            raise ClearinghouseLoadPostflightError(
                "ClearinghouseCapability treats a null postflight as a FAILED "
                "load (dev 1194): load() reported success but could not read "
                "back a row count for: " + ", ".join(bad) + ". The rows may "
                "nonetheless have landed -- reconcile against the table's "
                "Iceberg snapshot history rather than blindly re-running."
            )
        return load_result

    def register(
        self,
        entity_type: str,
        rows: Sequence[Mapping[str, Any]],
        *,
        kind: str,
        tenant: str = TENANT,
        dataset: str = NAMESPACE,
    ) -> dict[str, Any]:
        """Append ``rows`` to ``<type>_<kind>`` through ``BerdlCapability.load()``.

        IN-POD ONLY. Refuses off-pod (:class:`BerdlLoadRefusedError`) before any
        transport call. The table is selected by ``(entity_type, kind)`` through
        :func:`clearinghouse_schema.table_name` (Rule 2 -- the ONLY table-name
        constructor; raises ``ValueError`` on an unknown type or kind), never
        concatenated and never taken as a caller-supplied name.

        Before ``load()`` is reached, the dev 1206 pre-write assertion runs
        (:meth:`_assert_write_target`): the write-target namespace and the
        existence-probe namespace are resolved explicitly and must be SHOWN to
        agree, or this raises :class:`ClearinghouseWriteTargetMismatchError`
        with ``load()`` uncalled. After ``load()`` returns, its postflight is
        checked (:meth:`_check_postflight`): a null ``row_count`` is treated as
        a FAILED load (dev 1194), which may fire after the rows landed -- the
        correct direction to be wrong in.

        Args:
            entity_type: One of :data:`ENTITY_TYPES`.
            rows: The DataFrame-mode rows for this one table.
            kind: One of ``"entity"``, ``"content"``, ``"result"``.
            tenant: Target tenant for the membership check and write target
                (default :data:`TENANT`).
            dataset: The ``load()`` dataset / namespace (default
                :data:`NAMESPACE`).

        Returns:
            The wrapped ``load()`` result (postflight-verified).

        Raises:
            ValueError: Unknown ``entity_type`` or ``kind``.
            BerdlLoadRefusedError: Off-pod.
            ClearinghouseWriteTargetMismatchError: Write target could not be
                confirmed (dev 1206).
            ClearinghouseLoadPostflightError: Null postflight (dev 1194).
        """
        self._require_in_pod("register")
        name = table_name(entity_type, kind)  # Rule 2: raises on bad type/kind
        write_ns = self._assert_write_target(dataset=dataset, tenant=tenant)
        capability = self._get_capability()
        result = capability.load(
            dataset=dataset,
            tables=[{"name": name, "mode": "append"}],
            tenant=tenant,
            namespace=write_ns,
            dataframes={name: list(rows)},
        )
        return self._check_postflight(result)

    def ingest_shards(
        self,
        shard_dir: str | Path,
        *,
        run_id: str,
        dry_run: bool = False,
        reconcile: bool = False,
        tenant: str = TENANT,
        dataset: str = NAMESPACE,
    ) -> dict[str, Any]:
        """Ingest a directory of bronze parquet shards, table by table, resumably.

        IN-POD ONLY. Refuses off-pod (:class:`BerdlLoadRefusedError`) before it
        resolves any namespace, opens the ledger, or makes any transport call
        (success criterion (d)). Each shard is ingested through
        ``BerdlCapability.load()`` in BRONZE mode (``paths={'bronze_base': ...}``
        plus each table's own ``bronze_path``/``format``); every table's
        postflight is verified (:meth:`_check_postflight`) and every outcome is
        recorded in a JSON-lines run ledger.

        THE PRE-WRITE ASSERTION (dev 1206), fail-closed. Before ANY ``load()``
        call, :meth:`_assert_write_target` resolves the write-target namespace
        and the existence-probe namespace and refuses unless they can be SHOWN
        to be the same non-empty value -- raising
        :class:`ClearinghouseWriteTargetMismatchError` with ``load()`` called
        exactly zero times. A guard that cannot confirm it probed the same name
        it is about to write to has established nothing, and writing anyway is
        the failure.

        THE RUN LEDGER (resumability). A bootstrap load takes days and the pod
        (a rootless JupyterHub container with no init system) restarts
        routinely, so this verb must survive being killed and resumed. The
        ledger lives beside the shards at :data:`LEDGER_FILENAME`, one line per
        ``(table, batch)``::

            {"run_id":..., "table":"gene_entity", "batch":"0007",
             "shard_path":..., "rows":4821003, "state":"ingested",
             "snapshot_id":..., "at":...}

        Resume is simply calling ``ingest_shards()`` again with the same
        ``run_id`` -- there is NO separate resume code path:

        - A ``(table, batch)`` already at state ``"ingested"`` is SKIPPED.
        - A ``(table, batch)`` at state ``"started"`` with no terminal
          (``"ingested"``/``"failed"``) line is AMBIGUOUS -- the process died
          mid-ingest. This REFUSES (:class:`ClearinghouseLedgerAmbiguousError`)
          unless ``reconcile=True``, which reads the table's Iceberg snapshot
          history and decides whether the shard landed.

        WHY THIS IS SAFE, AND EXACTLY HOW FAR. The clearinghouse schema is
        APPEND-ONLY, so a crash BETWEEN the ingest and the ledger-write costs at
        worst a duplicate row -- and the current-state derivation collapses that
        harmlessly (same hash for ``<type>_entity``; latest-per-slot for
        ``<type>_result``). BUT ``<type>_content`` HAS NO SUCH DERIVATION: a
        re-ingested content shard leaves two rows for one ``entity_hash`` with no
        rule saying which wins, and a reader joining content by hash gets a
        fan-out. The ledger is precisely what prevents that -- and it is why
        ``reconcile`` exists rather than a blanket "re-running is safe".

        ``dry_run=True`` is genuinely read-only (matching
        :func:`clearinghouse_schema.bootstrap`'s dry-run guarantee): it resolves
        namespaces, runs the pre-write assertion, reports the write mode each
        table WOULD take, and writes nothing -- no ``load()`` call, no ledger
        line.

        Args:
            shard_dir: Directory holding the bronze parquet shards and the run
                ledger. Each table's shards are expected under a per-table
                subdirectory named for the table (``<shard_dir>/<table>/``),
                with each shard a file whose stem is the batch id.
            run_id: The run this call belongs to. Resuming is the same call with
                the same ``run_id``; ledger lines from other ``run_id`` values
                are ignored when deciding skip/refuse.
            dry_run: When ``True``, performs the pre-write assertion and reports
                the per-table plan but writes nothing.
            reconcile: When ``True``, an ambiguous ``"started"`` batch is
                resolved against the table's Iceberg snapshot history instead of
                refused.
            tenant: Target tenant (default :data:`TENANT`).
            dataset: The ``load()`` dataset / namespace (default
                :data:`NAMESPACE`).

        Returns:
            A dict with ``'run_id'``, ``'namespace'``, ``'dry_run'``, and a
            ``'tables'`` list -- one report per ``(table, batch)`` with its
            ``'name'``, ``'batch'``, resolved ``'action'`` (``'ingest'``,
            ``'skip'``, ``'reconcile'`` or -- dry-run only -- ``'would_ingest'``),
            and, for a real ingest, its postflight ``'row_count'``/``'snapshot_id'``.

        Raises:
            BerdlLoadRefusedError: Off-pod (before any transport call).
            ClearinghouseWriteTargetMismatchError: Write target unconfirmed
                (dev 1206) -- ``load()`` called zero times.
            ClearinghouseLedgerAmbiguousError: A ``"started"`` batch with no
                terminal line and ``reconcile=False``.
            ClearinghouseLoadPostflightError: A null postflight on a real
                ingest (dev 1194).
        """
        self._require_in_pod("ingest_shards")
        shard_root = Path(shard_dir)

        # Pre-write assertion FIRST, before opening the ledger or reading any
        # shard -- so a mismatch refuses with load() called zero times, in
        # dry_run as well as a real run.
        write_ns = self._assert_write_target(dataset=dataset, tenant=tenant)

        ledger_path = shard_root / LEDGER_FILENAME
        ledger_states = _read_ledger_states(ledger_path, run_id=run_id)
        discovered = _discover_shards(shard_root)

        capability = self._get_capability()
        reports: list[dict[str, Any]] = []

        for name, batch, shard_path in discovered:
            key = (name, batch)
            state = ledger_states.get(key)

            if state == _LEDGER_INGESTED:
                reports.append({"name": name, "batch": batch, "action": "skip"})
                continue

            if state == _LEDGER_STARTED:
                # Ambiguous: died mid-ingest, no terminal line. Refuse unless
                # reconcile decides against snapshot history.
                if not reconcile:
                    raise ClearinghouseLedgerAmbiguousError(
                        f"ingest_shards: ledger has {name!r} batch {batch!r} at "
                        f"state {_LEDGER_STARTED!r} with no terminal line for "
                        f"run_id {run_id!r} -- the process died mid-ingest and "
                        "whether the shard landed is unknown. Refusing to "
                        "continue past it. A duplicate <type>_content shard has "
                        "no current-state derivation to collapse it, so this "
                        "cannot be assumed safe. Re-run with reconcile=True to "
                        "decide against the table's Iceberg snapshot history."
                    )
                landed = self._reconcile_started_batch(name, batch, write_ns=write_ns)
                if landed:
                    _append_ledger_line(
                        ledger_path,
                        run_id=run_id,
                        table=name,
                        batch=batch,
                        shard_path=str(shard_path),
                        rows=None,
                        state=_LEDGER_INGESTED,
                        snapshot_id=None,
                        extra={"reconciled": True},
                    )
                    reports.append(
                        {"name": name, "batch": batch, "action": "reconcile"}
                    )
                    continue
                # Reconcile decided the shard did NOT land -- fall through and
                # ingest it as a fresh batch below.

            if dry_run:
                reports.append({"name": name, "batch": batch, "action": "would_ingest"})
                continue

            # Real ingest: write a non-terminal 'started' line BEFORE the
            # load(), so a crash mid-ingest leaves the ambiguous marker the
            # resume path refuses on.
            _append_ledger_line(
                ledger_path,
                run_id=run_id,
                table=name,
                batch=batch,
                shard_path=str(shard_path),
                rows=None,
                state=_LEDGER_STARTED,
                snapshot_id=None,
            )
            try:
                result = capability.load(
                    dataset=dataset,
                    tables=[
                        {
                            "name": name,
                            "mode": "append",
                            "bronze_path": str(shard_path),
                            "format": _shard_format(shard_path),
                        }
                    ],
                    tenant=tenant,
                    namespace=write_ns,
                    paths={"bronze_base": str(shard_root)},
                )
                result = self._check_postflight(result)
            except Exception as exc:
                _append_ledger_line(
                    ledger_path,
                    run_id=run_id,
                    table=name,
                    batch=batch,
                    shard_path=str(shard_path),
                    rows=None,
                    state=_LEDGER_FAILED,
                    snapshot_id=None,
                    extra={"error": f"{type(exc).__name__}: {exc}"},
                )
                raise

            table_report = _first_table_report(result, name)
            row_count = table_report.get("row_count")
            snapshot_id = table_report.get("new_snapshot_id")
            _append_ledger_line(
                ledger_path,
                run_id=run_id,
                table=name,
                batch=batch,
                shard_path=str(shard_path),
                rows=row_count,
                state=_LEDGER_INGESTED,
                snapshot_id=snapshot_id,
            )
            reports.append(
                {
                    "name": name,
                    "batch": batch,
                    "action": "ingest",
                    "row_count": row_count,
                    "snapshot_id": snapshot_id,
                }
            )

        return {
            "run_id": run_id,
            "namespace": write_ns,
            "dry_run": dry_run,
            "tables": reports,
        }

    def _reconcile_started_batch(self, name: str, batch: str, *, write_ns: str) -> bool:
        """Decide whether an ambiguous ``"started"`` shard actually landed.

        Reads the table's Iceberg snapshot history and returns whether a
        snapshot exists that could correspond to this batch's append. This
        deliberately makes a single, read-only ``query(..., engine="spark")``
        call against ``<ns>.<table>.snapshots`` -- the Iceberg metadata table --
        and reports whether the table has ANY snapshot at all. A more precise
        per-batch attribution would require a batch marker in the snapshot
        summary that the write path does not currently record; absent that, the
        conservative reading is "if the table shows no snapshots the shard
        cannot have landed, so re-ingest is safe; otherwise treat it as landed
        and skip". This mirrors the append-only reasoning in the ``ingest_shards``
        docstring and is intentionally the read-only half of reconcile.

        Returns:
            ``True`` if the table shows at least one snapshot (treat the shard
            as landed -- skip re-ingest), ``False`` if it shows none (safe to
            re-ingest).
        """
        fqn = _quote_fqn(f"{_FQN_PREFIX}.{name}.snapshots")
        rows = self._run(
            f"SELECT snapshot_id FROM {fqn} ORDER BY committed_at DESC LIMIT 1",
            params=None,
            engine="spark",
        )
        return bool(rows)

    def _live_snapshot_ids(self, name: str) -> set[str]:
        """Return the set of snapshot ids currently present on ``<ns>.<name>``.

        A single read-only ``query(..., engine="spark")`` against the table's
        Iceberg ``.snapshots`` metadata table. Used by :meth:`verify_run` to
        confirm the snapshot a ledger line recorded for an ingested shard still
        exists in the table's history. Ids are stringified so a ledger's
        JSON-decoded (often string) snapshot value compares cleanly against the
        metadata table's (often numeric) column.
        """
        fqn = _quote_fqn(f"{_FQN_PREFIX}.{name}.snapshots")
        rows = self._run(
            f"SELECT snapshot_id FROM {fqn}",
            params=None,
            engine="spark",
        )
        ids: set[str] = set()
        for row in rows:
            value = row.get("snapshot_id") if isinstance(row, dict) else None
            if value is None and row:
                value = next(iter(row.values()), None)
            if value is not None:
                ids.add(str(value))
        return ids

    def verify_run(
        self,
        shard_dir: str | Path,
        *,
        run_id: str,
    ) -> dict[str, Any]:
        """Re-check a completed run's row counts and snapshots against the ledger.

        IN-POD ONLY. Refuses off-pod (:class:`BerdlLoadRefusedError`) before any
        transport call. This is the POST-INGEST audit half of the ingest
        contract: :meth:`ingest_shards` wrote a run ledger recording, per
        ``(table, batch)``, the row count and Iceberg snapshot id it observed at
        ingest time. This verb reads that ledger back and, for every table that
        the run marked ``"ingested"``, confirms two things against the LIVE
        table:

        - the table's current ``COUNT(*)`` is at least the sum of the ledger's
          recorded per-batch row counts for this run (a live count BELOW the
          ledger sum means rows the ledger says landed are missing -- a real
          discrepancy), and
        - every snapshot id the ledger recorded for an ingested batch still
          exists in the table's Iceberg snapshot history.

        It performs the same explicit ``COUNT(*)`` discipline as :meth:`stats`
        (never a transport ``row_count`` field), and it WRITES NOTHING -- no
        ``load()``, no ledger line. A discrepancy is REPORTED, not raised: the
        correct operator response to a load reported FAILED on a null postflight
        (dev 1194) is precisely to run ``verify`` and reconcile, so this verb
        must be able to tell the operator what it found without itself failing.

        Args:
            shard_dir: The shard directory whose ``ingest_ledger.jsonl`` this run
                wrote (same directory :meth:`ingest_shards` used).
            run_id: The run whose ledger lines to verify.

        Returns:
            A dict with ``'run_id'``, ``'namespace'`` (the confirmed write-target
            namespace, resolved through the dev 1206 guard so verify reads the
            same namespace the ingest wrote), a ``'tables'`` list (one report per
            verified table with its ``'name'``, ``'ledger_rows'``, ``'live_rows'``,
            ``'ok'`` and, when a snapshot is missing, ``'missing_snapshots'``),
            and a ``'discrepancies'`` list of human-readable strings (empty when
            everything reconciles).

        Raises:
            BerdlLoadRefusedError: Off-pod (before any transport call).
            ClearinghouseWriteTargetMismatchError: Namespace unconfirmed
                (dev 1206) -- no read is attempted against an unconfirmed target.
        """
        self._require_in_pod("verify_run")
        write_ns = self._assert_write_target(dataset=NAMESPACE, tenant=TENANT)
        shard_root = Path(shard_dir)
        ledger_path = shard_root / LEDGER_FILENAME
        records = _read_ledger_records(ledger_path, run_id=run_id)

        # Collapse the ingested ledger lines per table: sum recorded rows, and
        # collect every recorded snapshot id.
        per_table_rows: dict[str, int] = {}
        per_table_snaps: dict[str, set[str]] = {}
        for (table, _batch), record in records.items():
            if record.get("state") != _LEDGER_INGESTED:
                continue
            rows = record.get("rows")
            if isinstance(rows, int):
                per_table_rows[table] = per_table_rows.get(table, 0) + rows
            per_table_snaps.setdefault(table, set())
            snap = record.get("snapshot_id")
            if snap is not None:
                per_table_snaps[table].add(str(snap))

        reports: list[dict[str, Any]] = []
        discrepancies: list[str] = []
        for table in sorted(set(per_table_rows) | set(per_table_snaps)):
            fqn = _quote_fqn(f"{_FQN_PREFIX}.{table}", "trino")
            live_rows = _scalar(
                self._run(
                    f"SELECT COUNT(*) AS row_count FROM {fqn}",
                    params=None,
                    engine="trino",
                ),
                "row_count",
            )
            ledger_rows = per_table_rows.get(table)
            live_snaps = self._live_snapshot_ids(table)
            missing = sorted(per_table_snaps.get(table, set()) - live_snaps)

            ok = True
            if (
                ledger_rows is not None
                and isinstance(live_rows, int)
                and live_rows < ledger_rows
            ):
                ok = False
                discrepancies.append(
                    f"{table}: live COUNT(*)={live_rows} is below the ledger's "
                    f"recorded {ledger_rows} ingested rows for run {run_id!r} -- "
                    "rows the ledger says landed appear to be missing."
                )
            if missing:
                ok = False
                discrepancies.append(
                    f"{table}: {len(missing)} snapshot id(s) the ledger recorded "
                    f"for run {run_id!r} are absent from the table's Iceberg "
                    f"snapshot history: {', '.join(missing)}."
                )

            report: dict[str, Any] = {
                "name": table,
                "ledger_rows": ledger_rows,
                "live_rows": live_rows,
                "ok": ok,
            }
            if missing:
                report["missing_snapshots"] = missing
            reports.append(report)

        return {
            "run_id": run_id,
            "namespace": write_ns,
            "tables": reports,
            "discrepancies": discrepancies,
        }


# --------------------------------------------------------------------------
# Module-level pure helpers
# --------------------------------------------------------------------------


def _quote_fqn(fqn: str, engine: str = "spark") -> str:
    """Quote each dot-separated segment of an identifier, per ENGINE.

    Mirrors ``clearinghouse_derivation._quote_fqn`` /
    ``clearinghouse_schema._quote_fqn``: every segment between dots gets its
    own pair of quotes, so a dotted namespace such as
    ``kbaseincubator.clearinghouse`` is never misread as a single identifier
    containing a dot (measured failing in-pod -- see
    ``clearinghouse_bootstrap_adapter._quote_fqn``). A table-metadata suffix
    such as ``...gene_entity.files`` is quoted per segment the same way. A
    segment already quoted is not double-quoted.

    The quote CHARACTER is dialect-specific. Trino rejects backquoted
    identifiers outright (``SYNTAX_ERROR: backquoted identifiers are not
    supported; use double quotes to quote identifiers``); Spark/Hive
    require backticks. This hardcoded backticks until 2026-09-28, which
    broke every Trino-routed read verb on the pod -- ``stats`` degraded
    silently to em-dash row counts, ``sources`` and ``results`` raised.

    Args:
        fqn: A dot-separated identifier. Segments may already be quoted.
        engine: ``"trino"`` selects double quotes; anything else
            (``"spark"``, the default) selects backticks. Pass the SAME
            resolved engine handed to :meth:`ClearinghouseCapability._run`,
            so quoting cannot disagree with the executing dialect.
    """
    quote = '"' if engine == "trino" else "`"
    return ".".join(
        f"{quote}{segment.strip('`\"')}{quote}" for segment in fqn.split(".")
    )


def _in_literal(column: str, values: list[str]) -> str:
    """Build a ``<column> IN (...)`` predicate over single-quoted literals.

    Used ONLY for operator-controlled, schema-recognized values --
    ``result_type`` in :meth:`ClearinghouseCapability.results` /
    :meth:`current_state` -- never for a hash or any end-user input. An empty
    ``values`` list yields ``1 = 0`` (match nothing), matching
    ``clearinghouse_derivation``'s own empty-filter semantics rather than
    emitting an invalid ``IN ()``.
    """
    if not values:
        return "1 = 0"
    quoted = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"{column} IN ({quoted})"


def _scalar(rows: list[dict[str, Any]], key: str) -> Any:
    """Read a single scalar out of a one-row aggregate result.

    Returns ``rows[0][key]`` when present, falling back to the row's first
    value when the aggregate column arrived under a different key (e.g. an
    engine that names an unaliased ``COUNT(*)`` column differently), or
    ``None`` for an empty result.
    """
    if not rows:
        return None
    first = rows[0]
    if key in first:
        return first[key]
    values = list(first.values())
    return values[0] if values else None


def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop duplicate rows while preserving first-seen order.

    A batch split across chunks/pages and reassembled can carry the same row
    twice only through a caller passing a duplicate hash or a page-boundary
    artifact; de-duplicating on the full row content makes a caller passing
    50000 hashes get exactly one answer with no page-boundary duplicates.
    Rows are dicts (unhashable), so identity is a tuple of sorted items.
    """
    seen: set[tuple] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            key = tuple(sorted(row.items()))
        except TypeError:
            # A value is itself unhashable (e.g. a nested list); fall back to
            # repr so such a row is still de-duplicated deterministically.
            key = (repr(sorted(row.items(), key=lambda kv: kv[0])),)
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def _read_ledger_states(
    ledger_path: Path, *, run_id: str
) -> dict[tuple[str, str], str]:
    """Read the run ledger and return the LAST state per ``(table, batch)``.

    The ledger is JSON-lines, appended to; a ``(table, batch)`` may have
    several lines (a ``"started"`` then an ``"ingested"``/``"failed"``). The
    LAST line for a key is its current state, so a ``"started"`` followed by
    ``"ingested"`` reads as ingested, while a lone ``"started"`` (the process
    died before writing a terminal line) reads as ``"started"`` -- the
    ambiguous state resume refuses on. Only lines matching ``run_id`` are
    considered; a missing ledger file is an empty set of states. Malformed
    lines are skipped rather than crashing a days-long resume.
    """
    states: dict[tuple[str, str], str] = {}
    if not ledger_path.exists():
        return states
    for line in ledger_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if record.get("run_id") != run_id:
            continue
        table = record.get("table")
        batch = record.get("batch")
        state = record.get("state")
        if table is None or batch is None or state is None:
            continue
        states[(str(table), str(batch))] = str(state)
    return states


def _read_ledger_records(
    ledger_path: Path, *, run_id: str
) -> dict[tuple[str, str], dict[str, Any]]:
    """Read the run ledger and return the LAST full record per ``(table, batch)``.

    Like :func:`_read_ledger_states` but keeps the whole record (rows,
    snapshot_id, state, ...) of each key's final line, which
    :meth:`ClearinghouseCapability.verify_run` needs to sum recorded rows and
    collect recorded snapshot ids. Only lines matching ``run_id`` are kept; a
    missing ledger is an empty mapping and malformed lines are skipped.
    """
    records: dict[tuple[str, str], dict[str, Any]] = {}
    if not ledger_path.exists():
        return records
    for line in ledger_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(record, dict) or record.get("run_id") != run_id:
            continue
        table = record.get("table")
        batch = record.get("batch")
        if table is None or batch is None:
            continue
        records[(str(table), str(batch))] = record
    return records


def _append_ledger_line(
    ledger_path: Path,
    *,
    run_id: str,
    table: str,
    batch: str,
    shard_path: str,
    rows: int | None,
    state: str,
    snapshot_id: Any,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Append one JSON-lines record to the run ledger, flushed to disk.

    One append per ``(table, batch)`` state transition. The file is opened in
    append mode and each line is a complete JSON object followed by a newline,
    so a crash mid-write leaves at most one truncated trailing line (which
    :func:`_read_ledger_states` skips) rather than a corrupted earlier record.
    """
    record: dict[str, Any] = {
        "run_id": run_id,
        "table": table,
        "batch": batch,
        "shard_path": shard_path,
        "rows": rows,
        "state": state,
        "snapshot_id": snapshot_id,
        "at": _now_iso(),
    }
    if extra:
        record.update(extra)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (ledger ``at`` field)."""
    from datetime import datetime, timezone  # local: keep module import-cheap

    return datetime.now(timezone.utc).isoformat()


def _discover_shards(shard_root: Path) -> list[tuple[str, str, Path]]:
    """Discover ``(table, batch, shard_path)`` triples under ``shard_root``.

    Shards are laid out one subdirectory per table (named for the table), each
    holding one file per batch whose stem is the batch id -- e.g.
    ``<shard_root>/gene_entity/0007.parquet``. Only subdirectories named for a
    known clearinghouse table (:func:`clearinghouse_schema.table_name` over
    every type/kind) are considered, so the ledger file and any stray directory
    beside the shards are ignored. The result is sorted by ``(table, batch)``
    for a deterministic, resumable ingest order.
    """
    known = {table_name(etype, kind) for kind in _KINDS for etype in ENTITY_TYPES}
    discovered: list[tuple[str, str, Path]] = []
    if not shard_root.exists():
        return discovered
    for table_dir in sorted(p for p in shard_root.iterdir() if p.is_dir()):
        name = table_dir.name
        if name not in known:
            continue
        for shard in sorted(p for p in table_dir.iterdir() if p.is_file()):
            discovered.append((name, shard.stem, shard))
    discovered.sort(key=lambda triple: (triple[0], triple[1]))
    return discovered


def _shard_format(shard_path: Path) -> str:
    """Infer a bronze ``format`` from a shard file's suffix.

    Bronze-mode ``load()`` needs each table's ``format``; it is taken from the
    shard file's extension (``.parquet`` -> ``"parquet"``, ``.csv`` -> ``"csv"``,
    etc.), defaulting to ``"parquet"`` for an extensionless shard, since the
    bootstrap corpus is parquet.
    """
    suffix = shard_path.suffix.lstrip(".").lower()
    return suffix or "parquet"


def _first_table_report(load_result: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the per-table report for ``name`` from a ``load()`` result.

    ``BerdlCapability.load()`` returns ``{'ingest_result', 'tables': [...]}``;
    each ingest_shards() call loads exactly one table, so this returns the
    report whose ``name`` matches (falling back to the first report, or an empty
    dict, so a fake with a differently-shaped result never crashes the ledger
    write).
    """
    tables = load_result.get("tables") or []
    for report in tables:
        if report.get("name") == name:
            return report
    return tables[0] if tables else {}
