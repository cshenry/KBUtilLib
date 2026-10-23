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
import textwrap
from pathlib import Path
from typing import Any, Mapping, Sequence

from ...identity import ParameterSetError, canonical_parameter_set, parameter_set_hash
from .capability import BerdlCapability, BerdlLoadRefusedError
from . import clearinghouse_ingest as ci
from .clearinghouse_derivation import current_state_sql
from .clearinghouse_parameters import result_parameter_set_identity
from .clearinghouse_schema import (
    _KINDS,
    ENTITY_TYPES,
    NAMESPACE,
    PARAMETER_SET_TABLE,
    TENANT,
    encode_entity_hash,
    parameter_set_column_names,
    table_configs,
    table_name,
)

__all__ = [
    "ClearinghouseCapability",
    "ClearinghouseWriteTargetMismatchError",
    "ClearinghouseLoadPostflightError",
    "ClearinghouseLedgerAmbiguousError",
    "ClearinghousePartialWriteError",
    "ClearinghouseReconcileMismatchError",
    "ClearinghouseSchemaPreflightError",
    "ClearinghouseRegistryOrderError",
    "order_shards_for_load",
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
#: degradation in :meth:`_inline_hex_params` safe: off-pod there is no bind-
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

#: Name of the JSON-lines ledger each LOAD EXECUTION keeps. Re-exported from
#: :mod:`clearinghouse_ingest`, which owns it along with the rest of the
#: execution state -- there is one definition of the name, not one per module.
#: The ledger does NOT live beside the shards: see
#: :data:`clearinghouse_ingest.STATE_DIR_ENV`.
LEDGER_FILENAME = ci.LEDGER_FILENAME


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


class ClearinghouseReconcileMismatchError(RuntimeError):
    """Raised when reconcile counted a PARTIAL append and stopped the run.

    A batch left ``"started"`` is decided by counting the rows carrying its
    ``ingest_batch_id``: equal to the expectation means it landed, zero means
    it did not. Any OTHER count means part of the append committed, and
    neither available move is safe -- appending again duplicates the rows
    that are there, and leaving it under-counts the run. So the run stops and
    reports the batch, because a human deciding from the count is cheap and a
    silently duplicated ``<type>_content`` row (which has no current-state
    derivation to collapse it) is not.
    """


class ClearinghouseSchemaPreflightError(RuntimeError):
    """Raised when the target namespace is not the lake-5 clearinghouse schema.

    Every ``<type>_result`` table must carry ``parameter_set_hash`` (the last
    column of the five-tuple slot key) and the
    :data:`~clearinghouse_schema.PARAMETER_SET_TABLE` registry must exist.
    ``clearinghouse_schema.bootstrap()`` creates a table that is absent but
    CANNOT add a column to one that exists, so a result table left over from
    before lake-5 looks healthy to it -- and appending five-tuple-keyed rows
    into a four-tuple table either fails mid-run or silently loses the only
    record of which parameters produced each result. This check is therefore
    the only mechanical guard that the operator's rebuild actually happened,
    and it refuses with ``load()`` called exactly zero times.
    """


class ClearinghouseRegistryOrderError(RuntimeError):
    """Raised when a result batch would be loaded before its registry batches.

    REGISTRY FIRST is a writer obligation: a result row whose
    ``parameter_set_hash`` has no registry row is an orphan, because the hash
    is opaque and nothing can say what parameters produced the result. The
    ingest orders every unit's registry batch ahead of its results, so this
    fires only if that ordering was defeated (an uncommitted registry batch
    from this or an earlier unit), and it refuses rather than writing the
    orphan.
    """


class ClearinghousePartialWriteError(RuntimeError):
    """A write that had already begun failed part-way through.

    :meth:`ClearinghouseCapability.register` writes a ``result`` call in TWO
    appends -- the parameter-set registry rows first, then the result rows --
    because a result row whose ``parameter_set_hash`` the registry cannot
    explain is an orphan. If the SECOND append fails (a transport error
    mid-ingest), the first one has already committed, and saying "nothing was
    written" would be a lie. So this is raised instead, naming what did land.

    The call is SAFE TO RETRY: the registry is append-only, a hash always maps
    to the same canonical text, and a retry looks the already-written hashes
    up and skips them. The original failure is chained as ``__cause__``.
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

        There are THREE binding regimes here, picked from the resolved locus
        and ``engine``, because only one of the three can bind server-side:

        - **In-pod / Trino** -- ``params`` are handed to
          ``capability.query(sql, params=..., engine='trino')`` for true
          server-side binding. The hash never reaches the SQL text.
        - **In-pod / Spark** -- ``BerdlCapability.query`` REFUSES ``params``
          on this engine (parameterised ``spark.sql`` is Spark-version
          dependent and not relied on), so passing them would raise
          ``ValueError`` instead of running the read. Since the engine is
          promoted to Spark automatically at
          :data:`SPARK_PROMOTION_THRESHOLD` hashes, that refusal would land
          on EVERY bulk read. The params are therefore inlined via
          :meth:`_inline_hex_params` and ``params=None`` is passed -- the same
          validated-inlining the off-pod path uses.
        - **Off-pod** -- no bind-parameter channel at all (the REST query
          endpoint takes only SQL text), so ``params`` are likewise inlined
          via :meth:`_inline_hex_params` and ``limit``/``offset`` are
          forwarded to the REST page.

        On both inlining paths every value is re-validated against
        ``^[0-9a-f]{64}$`` BEFORE it reaches the SQL text, so a malformed
        hash raises before any query is issued and the inlined literal is
        provably injection-free.

        Returns a list of ``dict`` rows, normalised across the loci's return
        shapes (off-pod returns ``{'data': [dict, ...]}``; in-pod Trino
        returns dict rows keyed by column name; in-pod Spark returns ``Row``
        objects).
        """
        capability = self._get_capability()
        if self._locus() == "off_pod":
            final_sql = self._inline_hex_params(sql, params)
            result = capability.query(final_sql, limit=limit, offset=offset)
            return self._normalize_offpod_rows(result)
        # In-pod: paging is unnecessary (no REST cap). Trino binds
        # server-side; Spark cannot bind at all, so inline-then-no-params.
        if engine == "spark":
            final_sql = self._inline_hex_params(sql, params)
            rows = capability.query(final_sql, params=None, engine=engine)
            return self._normalize_inpod_rows(rows)
        rows = capability.query(
            sql, params=list(params) if params else None, engine=engine
        )
        return self._normalize_inpod_rows(rows)

    @staticmethod
    def _inline_hex_params(sql: str, params: Sequence[str] | None) -> str:
        """Inline positional ``?`` bind values safely, or refuse.

        Used by the TWO paths that have no bind-parameter channel at all:

        - **off-pod**, where the REST query endpoint takes only SQL text, and
        - **in-pod Spark**, where ``BerdlCapability.query`` raises on
          ``params`` because parameterised ``spark.sql(..., args=...)`` is
          Spark-version dependent and is deliberately not relied on. This is
          the path a hash batch at or above
          :data:`SPARK_PROMOTION_THRESHOLD` is promoted onto, so every bulk
          read lands here.

        Rather than fail the read, each ``?`` placeholder in ``sql`` is
        replaced, left to right, with the corresponding single-quoted
        parameter -- but ONLY after that parameter is re-confirmed to match
        :data:`_HEX64_RE`. Because an encoded ``entity_hash`` is provably
        ``^[0-9a-f]{64}$``, the inlined literal cannot contain a quote or any
        SQL metacharacter, so this substitution introduces no injection
        surface (see Rule 4 in the module docstring).

        Raises:
            ValueError: A parameter does not match :data:`_HEX64_RE`, or the
                number of ``?`` placeholders does not equal the number of
                parameters. This module only ever binds encoded hashes on
                these paths; anything else reaching here is a programming
                error and is refused rather than inlined -- and it is refused
                BEFORE any query is issued.
        """
        if not params:
            if "?" in sql:
                raise ValueError(
                    "_inline_hex_params: SQL has '?' placeholders but no params "
                    "given."
                )
            return sql
        for value in params:
            if not _HEX64_RE.match(value):
                raise ValueError(
                    "_inline_hex_params: refusing to inline a non-hex "
                    f"parameter ({value!r}); only encoded 64-char hex hashes "
                    "may be inlined, and only after re-validation."
                )
        placeholder_count = sql.count("?")
        if placeholder_count != len(params):
            raise ValueError(
                f"_inline_hex_params: {placeholder_count} '?' "
                f"placeholders but "
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

    @staticmethod
    def _validate_parameter_set_hashes(
        hashes: Sequence[str] | None, *, verb: str
    ) -> list[str] | None:
        """Check every ``parameter_set_hash`` FILTER value at the boundary, or raise.

        Returns ``None`` unchanged (no filter at all) and ``[]`` unchanged (a
        request to match nothing); otherwise returns the values as a list,
        once every element has passed :data:`_HEX64_RE`.

        Unlike :meth:`_encode_hashes`, which accepts an UPPERCASE entity
        digest and lowercases it, an uppercase parameter-set digest is
        REJECTED rather than normalised. That is the same deliberate choice
        :func:`clearinghouse_derivation._validated_parameter_set_hashes`
        makes, for the same reason: ``parameter_set_hash()`` only ever
        returns lowercase, so an uppercase value is evidence of a caller
        that did not go through it, and STRING comparison is case-sensitive
        in both Spark and Trino -- silently lowercasing would hide that
        caller, while passing it through would match zero stored rows and
        read as "this parameter set has no results".

        Validating HERE as well as in the derivation builder is deliberate,
        not redundant. It keeps the refusal at the capability boundary
        BEFORE any SQL is built (Rule 4), and it covers the two verbs that
        do not route through that builder at all -- :meth:`parameter_sets`
        and :meth:`parameter_set_orphans`.
        """
        if hashes is None:
            return None
        out = list(hashes)
        for index, value in enumerate(out):
            if not isinstance(value, str) or not _HEX64_RE.fullmatch(value):
                raise ValueError(
                    f"{verb}: parameter_set_hashes[{index}] is not a valid "
                    f"parameter_set_hash: {value!r}. Expected exactly 64 "
                    "LOWERCASE hex characters, as returned by "
                    "kbutillib.domains.identity.parameter_set_hash(). An "
                    "uppercase digest is rejected rather than lowercased, "
                    "because STRING comparison is case-sensitive and an "
                    "uppercase value would match zero stored rows instead of "
                    "failing."
                )
        return out

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

    def universe(
        self,
        entity_type: str,
        *,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return EVERY entity of one type, joined to its content row.

        The one read verb that takes no hash list, because its question is
        "what is there at all": the ``<type>_entity`` table is the authority
        on membership and ``<type>_content`` carries the payload, so the
        answer is the INNER JOIN of the two on ``entity_hash``. Rows are the
        full content row (``entity_hash`` plus the type-specialized columns
        -- ``definition`` for ``function``, ``sequence`` for ``protein`` --
        plus the shared tail), exactly as :meth:`content` returns them. An
        entity with no content row is not returned; that is a gap in the
        content table and :meth:`stats` is the verb that reports it.

        WHY IT EXISTS. The ``function_to_go`` pipeline's stage 1 derives its
        un-run set as ``universe - done``, and ``done`` is
        :meth:`current_state`. Before this verb there was no sanctioned way
        to read ``universe`` at all, so the only route was hand-rolled SQL
        against a table name a caller had constructed -- which Rules 2 and 4
        exist to prevent. It is a whole-table read by design and by name.

        ORDERED BY ``entity_hash``, which the other verbs do not need and
        this one does: off-pod the read pages with ``limit``/``offset``
        (:meth:`_page_offpod`), and an unordered relational scan may return
        rows in a different order per page, which would silently both drop
        and duplicate rows across page boundaries.

        Default engine: Spark -- a whole-table scan is Spark's shape, the
        same choice and reasoning as :meth:`current_state`. Off-pod there is
        no Spark, so the read routes through the off-pod REST transport and
        pages to completion.

        Args:
            entity_type: One of the five entity types (Rule 1).
            engine: Override the engine choice.

        Raises:
            ValueError: ``entity_type`` is not a known entity type (raised by
                :func:`~clearinghouse_schema.table_name`, Rule 2).
        """
        resolved_engine = self._resolve_engine(engine, default="spark")
        content = _quote_fqn(self._fqn(entity_type, "content"), resolved_engine)
        entity = _quote_fqn(self._fqn(entity_type, "entity"), resolved_engine)
        sql = (
            f"SELECT c.* FROM {content} AS c\n"
            f"JOIN {entity} AS e ON c.entity_hash = e.entity_hash\n"
            "ORDER BY c.entity_hash"
        )
        if self._locus() == "off_pod":
            return self._page_offpod(sql, params=None, engine=resolved_engine)
        return self._run(sql, params=None, engine=resolved_engine)

    def results(
        self,
        entity_type: str,
        hashes: Sequence[str | bytes],
        *,
        sources: list[str] | None = None,
        result_types: list[str] | None = None,
        parameter_set_hashes: list[str] | None = None,
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

        ``parameter_set_hashes`` is likewise pushed into the builder as a
        PRE-FILTER -- applied before the window, never to the derived rows
        after it. The difference is not cosmetic: filtering after the window
        would rank every parameter set's rows together and then discard all
        but the requested hashes, so a slot whose latest row belongs to
        another parameter set would come back EMPTY instead of returning the
        requested parameter set's own current-state row. Pre-filtering lets
        each ``(..., parameter_set_hash)`` slot win its own window, which is
        the whole point of putting the hash in the slot key. ``None`` applies
        no filter; ``[]`` is a request to match nothing (the builder emits
        ``WHERE 1 = 0``).

        Default engine: Trino, promoting to Spark at/above
        :data:`SPARK_PROMOTION_THRESHOLD` hashes.

        Raises:
            ValueError: Any ``parameter_set_hashes`` element is not exactly
                64 lowercase hex characters -- raised at the boundary before
                any SQL is built or any query issued, and BEFORE the
                empty-``hashes`` short-circuit, so a caller passing a
                malformed digest alongside an empty hash list still hears
                about the digest.
        """
        validated_parameter_sets = self._validate_parameter_set_hashes(
            parameter_set_hashes, verb="results"
        )
        encoded = self._encode_hashes(hashes)
        if not encoded:
            return []
        resolved_engine = self._resolve_engine(
            engine, default="trino", hash_count=len(encoded)
        )
        inner = current_state_sql(
            self._fqn(entity_type, "result"),
            sources=sources,
            parameter_set_hashes=validated_parameter_sets,
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
        parameter_set_hashes: list[str] | None = None,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return current state over the whole ``<type>_result`` table.

        Wraps :func:`clearinghouse_derivation.current_state_sql` (Rule 5)
        over the entire per-type result table -- no hash filter. ``sources``
        is pushed into the builder (partition pruning); ``result_types``, if
        given, filters the derived rows. ``entity_types`` is not passed to
        the builder (redundant on a single-typed table -- Rule 5).

        ``parameter_set_hashes`` is pushed into the builder as a PRE-FILTER,
        applied before the window rather than to the derived rows -- exactly
        as in :meth:`results`, and for the same reason (see that method:
        post-filtering would empty a slot whose latest row belongs to a
        different parameter set). ``None`` applies no filter; ``[]`` is a
        request to match nothing.

        Default engine: Spark. A whole-table current-state derivation is a
        window function over a partition scan, which is Spark's shape rather
        than Trino's (see :data:`SPARK_PROMOTION_THRESHOLD`). Off-pod there
        is no Spark, so the read still routes through the off-pod REST
        transport and pages to completion.

        Raises:
            ValueError: Any ``parameter_set_hashes`` element is not exactly
                64 lowercase hex characters (raised at the boundary, before
                any SQL is built).
        """
        validated_parameter_sets = self._validate_parameter_set_hashes(
            parameter_set_hashes, verb="current_state"
        )
        resolved_engine = self._resolve_engine(engine, default="spark")
        inner = current_state_sql(
            self._fqn(entity_type, "result"),
            sources=sources,
            parameter_set_hashes=validated_parameter_sets,
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
        by_parameter_set: bool = False,
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

        THE DEFAULT GRAIN IS FROZEN AND MUST NOT CHANGE: one row per
        ``source`` (per ``entity_type`` when aggregating all five tables).
        A consumer keyed on ``source`` would see DUPLICATE rows under an
        unchanged ``schema_version`` if the default grain ever became
        per-parameter-set -- a silent break, since nothing in the envelope
        would tell it the grain had moved. The parameter-set grain is
        therefore OPT-IN via ``by_parameter_set``, never the default.

        COUNT SEMANTICS. ``row_count`` keeps EXACTLY its existing population
        and meaning: ``COUNT(*)`` over every row of the table in that group,
        INCLUDING rows later superseded within their slot. This verb does not
        apply the current-state derivation and never has; it reports what is
        stored, not what is current. ``parameter_set_count`` is
        ``COUNT(DISTINCT parameter_set_hash)`` over that SAME population.

        Args:
            by_parameter_set: When ``True``, changes the grain to one row per
                ``(source, parameter_set_hash)`` and adds ``canonical_json``
                -- the registry's text for that hash, or ``None`` when the
                hash has no registry row (an orphan;
                :meth:`parameter_set_orphans` is the verb that reports those
                as failures). Each aggregate is then computed over the
                population RESTRICTED to that ``(source, parameter_set_hash)``,
                so ``parameter_set_count`` is 1 on every row -- it is kept in
                this grain only so the row SHAPE is the same in both grains
                and a consumer need not branch on which it asked for.
            engine: Default is Trino (a small grouped aggregate).

        Returns:
            One dict per group: ``entity_type``, ``source``, ``row_count``,
            ``parameter_set_count``, plus ``parameter_set_hash`` and
            ``canonical_json`` when ``by_parameter_set``.
        """
        resolved_engine = self._resolve_engine(engine, default="trino")
        types = ENTITY_TYPES if entity_type is None else (entity_type,)
        registry = _quote_fqn(
            f"{_FQN_PREFIX}.{PARAMETER_SET_TABLE}", resolved_engine
        )
        out: list[dict[str, Any]] = []
        for etype in types:
            table = _quote_fqn(self._fqn(etype, "result"), resolved_engine)
            if by_parameter_set:
                # The registry is reduced to ONE representative row per hash
                # BEFORE the join. Joining the raw registry would multiply
                # every result row by that hash's registry-row count, so an
                # ordinary retry (which may append a second row for a hash)
                # would silently DOUBLE row_count. MAX() over the already-
                # reduced side then guarantees the grain stays one row per
                # (source, parameter_set_hash) even if the reduction were
                # ever wrong, which putting canonical_json in GROUP BY would
                # not.
                sql = (
                    f"SELECT '{etype}' AS entity_type,\n"
                    "    res.source AS source,\n"
                    "    res.parameter_set_hash AS parameter_set_hash,\n"
                    "    COUNT(*) AS row_count,\n"
                    "    COUNT(DISTINCT res.parameter_set_hash)"
                    " AS parameter_set_count,\n"
                    "    MAX(reg.canonical_json) AS canonical_json\n"
                    f"FROM {table} AS res\n"
                    "LEFT JOIN (\n"
                    f"{_registry_representative_sql(registry, indent=4)}\n"
                    ") AS reg\n"
                    "    ON reg.parameter_set_hash = res.parameter_set_hash\n"
                    "GROUP BY res.source, res.parameter_set_hash"
                )
            else:
                sql = (
                    f"SELECT '{etype}' AS entity_type, source, "
                    "COUNT(*) AS row_count, "
                    "COUNT(DISTINCT parameter_set_hash) AS parameter_set_count "
                    f"FROM {table} GROUP BY source"
                )
            if self._locus() == "off_pod":
                out.extend(self._page_offpod(sql, params=None, engine=resolved_engine))
            else:
                out.extend(self._run(sql, params=None, engine=resolved_engine))
        return out

    def parameter_sets(
        self,
        hashes: Sequence[str] | None = None,
        *,
        engine: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return ONE representative registry row per ``parameter_set_hash``.

        The registry (:data:`~clearinghouse_schema.PARAMETER_SET_TABLE`) is
        APPEND-ONLY and a hash may legitimately carry several rows: a retry
        appends another row for a hash it has already registered, and every
        such row carries the SAME ``canonical_json`` (a hash always maps to
        the same text) but a DIFFERENT ``observed_at``/``ingest_batch_id``.

        That is why this is "distinct ON the hash" and NOT
        ``SELECT DISTINCT`` over all four columns. A plain ``SELECT DISTINCT``
        would collapse only byte-identical rows, so those retry rows would
        survive as several rows for one hash -- the exact shape a caller
        asking "what does this hash mean?" must not get. One row per hash is
        selected with the same ``ROW_NUMBER()`` discipline the result
        derivation uses (latest ``observed_at``, then ``ingest_batch_id`` as
        the tie-break), so the answer is deterministic rather than
        whichever-row-the-engine-returned-first.

        Args:
            hashes: If given, restricts the answer to these hashes, as a
                PRE-FILTER inside the window (fewer rows ranked, same
                answer). ``None`` returns every registered hash; ``[]`` is a
                request to match nothing and returns ``[]`` WITHOUT issuing
                a query (an empty ``IN ()`` list is not valid SQL and is not
                the semantics wanted anyway).
            engine: Default is Trino (a small lookup).

        Returns:
            One dict per hash: ``parameter_set_hash``, ``canonical_json``,
            ``observed_at``, ``ingest_batch_id``.

        Raises:
            ValueError: Any element of ``hashes`` is not exactly 64 lowercase
                hex characters (raised at the boundary, before any SQL).
        """
        validated = self._validate_parameter_set_hashes(
            hashes, verb="parameter_sets"
        )
        if validated is not None and not validated:
            return []
        resolved_engine = self._resolve_engine(engine, default="trino")
        table = _quote_fqn(
            f"{_FQN_PREFIX}.{PARAMETER_SET_TABLE}", resolved_engine
        )
        if validated:
            sql = _registry_representative_sql(
                table,
                where=(
                    "parameter_set_hash IN "
                    f"({self._in_placeholders(len(validated))})"
                ),
            )
            params: list[str] | None = list(validated)
        else:
            sql = _registry_representative_sql(table)
            params = None
        if self._locus() == "off_pod":
            return self._page_offpod(sql, params=params, engine=resolved_engine)
        return self._run(sql, params=params, engine=resolved_engine)

    def parameter_set_orphans(
        self,
        *,
        engine: str | None = None,
    ) -> dict[str, Any]:
        """Check parameter-set registry integrity and report every finding.

        Registry integrity is a WRITER obligation -- every writer appends the
        registry rows BEFORE the result rows that use them -- and this is the
        READER that audits whether the obligation held. It WRITES NOTHING and
        RAISES NOTHING for a finding: a finding is reported, because the
        operator response to a broken registry is to read the report and
        reconcile, which it cannot do if the verb raises. Callers that must
        treat findings as fatal (:meth:`verify_run`, the CLI ``health`` verb)
        read ``'failures'`` and fail themselves.

        Four conditions are checked, all four of which make a stored result
        un-interpretable rather than merely untidy:

        1. MISSING -- a ``parameter_set_hash`` present in one of the five
           ``<type>_result`` tables with no registry row. The result exists
           and nothing can say what parameters produced it.
        2. MALFORMED -- a result hash that is not 64 lowercase hex
           characters, ``NULL`` included (the column is required and non-null
           by a Python check at the write boundary, never by the DDL, so a
           ``NULL`` here is exactly the violation that check exists to
           prevent and only a read can catch it).
        3. CONFLICTING -- two registry rows for one hash with DIFFERENT
           ``canonical_json``. Duplicate rows per hash are harmless and
           expected (a retry appends one), but they must agree on the text;
           two different texts for one hash means at least one is a lie
           about what that hash means.
        4. MIS-HASHED -- a registry row whose ``canonical_json`` does not
           hash to its own key, i.e. ``parameter_set_hash`` of the parsed
           value differs from ``parameter_set_hash`` as stored. Text that
           cannot be parsed, or that fails the parameter-set rule, counts
           here too: it cannot be shown to hash to its key.

        Args:
            engine: Default is Trino (small ``DISTINCT`` aggregates).

        Returns:
            A report dict with ``'ok'`` (``True`` only when every list is
            empty), the ``'result_hashes'``/``'registry_hashes'`` counts the
            check ran over, one list per condition
            (``'missing_from_registry'``, ``'malformed_result_hashes'``,
            ``'conflicting_canonical_json'``, ``'mishashed_registry_rows'``)
            and ``'failures'`` -- human-readable strings, one per finding,
            empty when the registry is sound. Findings are FAILURES and never
            warnings: a warning is for a degraded answer, whereas every
            condition here is stored data that is already wrong.
        """
        resolved_engine = self._resolve_engine(engine, default="trino")
        off_pod = self._locus() == "off_pod"

        def read(sql: str) -> list[dict[str, Any]]:
            if off_pod:
                return self._page_offpod(sql, params=None, engine=resolved_engine)
            return self._run(sql, params=None, engine=resolved_engine)

        # (1)/(2): every distinct hash stored in any of the five result
        # tables, with the entity types it appears in, so a finding names the
        # table an operator has to go and look at.
        result_hash_types: dict[Any, set[str]] = {}
        for etype in ENTITY_TYPES:
            table = _quote_fqn(self._fqn(etype, "result"), resolved_engine)
            rows = read(
                f"SELECT DISTINCT parameter_set_hash FROM {table}"
            )
            for row in rows:
                digest = row.get("parameter_set_hash")
                key = digest if isinstance(digest, str) else None
                result_hash_types.setdefault(key, set()).add(etype)

        # (3)/(4): DISTINCT over BOTH registry columns, deliberately -- it
        # collapses a retry's byte-identical duplicate rows (which are
        # harmless) while KEEPING two rows when the text differs, which is
        # precisely the conflict being looked for.
        registry = _quote_fqn(
            f"{_FQN_PREFIX}.{PARAMETER_SET_TABLE}", resolved_engine
        )
        registry_rows = read(
            "SELECT DISTINCT parameter_set_hash, canonical_json "
            f"FROM {registry}"
        )
        registry_texts: dict[Any, list[Any]] = {}
        for row in registry_rows:
            digest = row.get("parameter_set_hash")
            registry_texts.setdefault(digest, []).append(row.get("canonical_json"))

        failures: list[str] = []

        malformed: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for digest, etypes in sorted(
            result_hash_types.items(), key=lambda item: (item[0] is not None, item[0])
        ):
            entry = {
                "parameter_set_hash": digest,
                "entity_types": sorted(etypes),
            }
            if digest is None or not _HEX64_RE.fullmatch(digest):
                malformed.append(entry)
                failures.append(
                    f"malformed parameter_set_hash {digest!r} stored in "
                    f"{', '.join(sorted(etypes))} result table(s): a stored "
                    "parameter_set_hash must be exactly 64 lowercase hex "
                    "characters and non-null."
                )
            elif digest not in registry_texts:
                missing.append(entry)
                failures.append(
                    f"parameter_set_hash {digest} is used by "
                    f"{', '.join(sorted(etypes))} result row(s) but has NO "
                    "parameter-set registry row, so nothing can say which "
                    "parameters produced those results."
                )

        conflicting: list[dict[str, Any]] = []
        mishashed: list[dict[str, Any]] = []
        for digest, texts in sorted(
            registry_texts.items(), key=lambda item: (item[0] is None, item[0] or "")
        ):
            if len(texts) > 1:
                conflicting.append(
                    {
                        "parameter_set_hash": digest,
                        "canonical_json": sorted(
                            texts, key=lambda text: "" if text is None else str(text)
                        ),
                    }
                )
                failures.append(
                    f"parameter_set_hash {digest!r} has {len(texts)} "
                    "registry rows carrying DIFFERENT canonical_json; a hash "
                    "must always map to the same text, so at least one of "
                    "them is wrong."
                )
            for text in texts:
                finding = _mishash_reason(digest, text)
                if finding is not None:
                    computed, reason = finding
                    mishashed.append(
                        {
                            "parameter_set_hash": digest,
                            "canonical_json": text,
                            "computed_parameter_set_hash": computed,
                            "reason": reason,
                        }
                    )
                    failures.append(
                        f"registry row for parameter_set_hash {digest!r} is "
                        f"mis-hashed: {reason}"
                    )

        return {
            "ok": not failures,
            "result_hashes": len(result_hash_types),
            "registry_hashes": len(registry_texts),
            "missing_from_registry": missing,
            "malformed_result_hashes": malformed,
            "conflicting_canonical_json": conflicting,
            "mishashed_registry_rows": mishashed,
            "failures": failures,
        }

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
        :class:`~kbutillib.domains.kbase.berdl.capability.BerdlCapability`
        implements both, and implements them by returning the result of ONE
        shared pure function (``_write_target_namespace``) that its own
        ``load()`` also uses for the existence probe. So this guard is not
        merely satisfied by a real capability -- the agreement it checks is
        structural rather than coincidental: there is one derivation of the
        namespace, and the probe, the write and this check all read it.

        The guard is deliberately NOT relaxed for that: it still demands both
        methods and still refuses on a falsy or mismatched pair, so a future
        collaborator that splits the derivation back into two is caught here
        rather than in production.

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

        THE PARAMETER SET, on a ``result`` call. Every row destined for a
        ``<type>_result`` table must supply a ``parameter_set`` INPUT field --
        either a mapping or its canonical JSON text -- and nothing else is
        accepted: a missing one is an error, not a default run, because
        "nobody said" and "the caller explicitly ran the defaults" are
        different claims and only the second may be recorded as ``{}``.
        Each row's set is validated against the parameter-set rule plus any
        tool-specific rule (:mod:`clearinghouse_parameters`), hashed, and the
        hash stored as ``parameter_set_hash``; the ``parameter_set`` field
        itself is REMOVED and never stored on a result row. See
        :meth:`_prepare_result_rows` for the exact rules, including why
        non-canonical text is refused.

        REGISTRY FIRST, THEN THE RESULT ROWS -- two appends, in that order,
        so no result row ever exists whose hash the registry cannot explain.
        Only hashes not already in the registry are written, so an ordinary
        retry of the same call adds no registry row. Two concurrent identical
        calls may still each insert a row for one hash; that is harmless (a
        hash always maps to the same text and readers select distinct).

        REFUSE, NEVER WARN. EVERY row is validated BEFORE the first append,
        and any failure refuses the WHOLE call with nothing written, naming
        the offending row index and the reason -- the same posture as the
        dev 1206 pre-write assertion. The one case where "nothing was
        written" would be false is a transport failure on the SECOND append,
        after the registry rows committed; that raises
        :class:`ClearinghousePartialWriteError`, which says so and says the
        call is safe to retry.

        Args:
            entity_type: One of :data:`ENTITY_TYPES`.
            rows: The DataFrame-mode rows for this one table. For
                ``kind='result'`` each row must carry ``parameter_set``.
            kind: One of ``"entity"``, ``"content"``, ``"result"``.
            tenant: Target tenant for the membership check and write target
                (default :data:`TENANT`).
            dataset: The ``load()`` dataset / namespace (default
                :data:`NAMESPACE`).

        Returns:
            The wrapped ``load()`` result for the ROWS (postflight-verified).
            On a ``result`` call it additionally carries
            ``'parameter_set_registry'``: the hashes written, the hashes
            already present, and the registry ``load()`` result (``None``
            when every hash was already registered).

        Raises:
            ValueError: Unknown ``entity_type`` or ``kind``.
            ParameterSetError: A result row's ``parameter_set`` is missing,
                invalid, non-canonical text, invalid or duplicate-key JSON,
                or disagrees with a ``parameter_set_hash`` the row also
                carries. Nothing has been written.
            BerdlLoadRefusedError: Off-pod.
            ClearinghouseWriteTargetMismatchError: Write target could not be
                confirmed (dev 1206).
            ClearinghouseLoadPostflightError: Null postflight (dev 1194).
            ClearinghousePartialWriteError: The result append failed after the
                registry append committed. Safe to retry.
        """
        self._require_in_pod("register")
        name = table_name(entity_type, kind)  # Rule 2: raises on bad type/kind

        # Validate EVERY row before anything is written. This runs before the
        # dev 1206 assertion too: both refuse with nothing written, and the
        # cheaper, caller-fixable error is the more useful one to raise first.
        if kind == "result":
            prepared, registry_rows = self._prepare_result_rows(rows)
        else:
            prepared, registry_rows = [dict(row) for row in rows], []

        write_ns = self._assert_write_target(dataset=dataset, tenant=tenant)
        capability = self._get_capability()

        registry_report: dict[str, Any] | None = None
        if registry_rows:
            registry_report = self._write_parameter_set_registry(
                registry_rows,
                tenant=tenant,
                dataset=dataset,
                write_ns=write_ns,
            )

        try:
            result = capability.load(
                dataset=dataset,
                tables=[{"name": name, "mode": "append"}],
                tenant=tenant,
                namespace=write_ns,
                dataframes={name: prepared},
            )
            result = self._check_postflight(result)
        except Exception as exc:
            # Only a registry append that actually HAPPENED makes this call
            # partially committed. If every hash was already registered no
            # load() was made for the registry, so nothing was written and the
            # original failure is the honest one to raise.
            if registry_report is None or registry_report["load_result"] is None:
                raise
            raise ClearinghousePartialWriteError(
                f"register({entity_type!r}, kind={kind!r}): the "
                f"{PARAMETER_SET_TABLE!r} registry rows for "
                f"{sorted(registry_report['written'])} WERE written, then the "
                f"append to {name!r} failed: {type(exc).__name__}: {exc}. This "
                "call is partially committed -- do NOT read it as 'nothing was "
                "written'. It is safe to retry: the registry is append-only, a "
                "hash always maps to the same canonical text, and a retry "
                "looks the written hashes up and skips them."
            ) from exc

        if kind == "result":
            result = dict(result)
            result["parameter_set_registry"] = registry_report or {
                "written": [],
                "already_present": [],
                "load_result": None,
            }
        return result

    def _prepare_result_rows(
        self, rows: Sequence[Mapping[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Validate a ``result`` call's parameter sets; return rows + registry rows.

        Called BEFORE any write. Every row is checked, not just up to the
        first bad one, in the sense that the FIRST failure refuses the whole
        call -- nothing is written, so a caller fixes its payload once rather
        than discovering a second bad row after a partial load.

        Per row, in order:

        1. ``parameter_set`` must be present. Absent is an ERROR, never an
           implied default run.
        2. A mapping is taken as-is. A ``str`` is parsed STRICTLY: it must be
           a JSON OBJECT, with no duplicate keys (``{"a":1,"a":2}`` is
           refused rather than silently last-wins), and must be byte-equal to
           ``canonical_parameter_set(parsed)`` -- so ``{"b":1,"a":2}`` is
           refused as non-canonical. Accepting non-canonical text would mean
           storing a hash the text as given does not produce.
        3. The parsed set is validated against the generic parameter-set rule
           and the tool-specific rule for this row's ``source``
           (:func:`clearinghouse_parameters.result_parameter_set_identity`).
        4. If the row ALSO carries ``parameter_set_hash``, it must equal the
           computed hash; a disagreement is a refusal, not an overwrite.
        5. ``parameter_set_hash`` is set on the stored row and the
           ``parameter_set`` input field is REMOVED -- it is not a column of
           any ``<type>_result`` table and must never be stored on one.

        Returns:
            ``(stored_rows, registry_rows)``. ``registry_rows`` holds one row
            per DISTINCT hash, in first-seen order, each carrying the hash,
            its ``canonical_json``, and the ``observed_at`` /
            ``ingest_batch_id`` of the first row that used it.

        Raises:
            ParameterSetError: Any row failed any step above; the message
                names the row INDEX and the reason. Nothing is written.
        """
        prepared: list[dict[str, Any]] = []
        registry: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows):
            out = dict(row)
            where = f"register: rows[{index}]['parameter_set']"
            if "parameter_set" not in out:
                raise ParameterSetError(
                    f"register: rows[{index}] is destined for a result table "
                    "and carries no 'parameter_set'. A result is keyed by "
                    "(entity, result_type, source, parameter_set_hash), so "
                    "every result row must say which parameter set produced "
                    "it. Pass {} (a mapping or the text '{}') to record a "
                    "DEFAULT run -- an absent field is not a default run, it "
                    "is an unanswered question. Nothing has been written."
                )
            raw = out.pop("parameter_set")
            params = self._parse_parameter_set_field(raw, index=index)
            canonical, digest = result_parameter_set_identity(
                params, source=out.get("source"), where=where
            )
            declared = out.get("parameter_set_hash")
            if declared is not None and declared != digest:
                raise ParameterSetError(
                    f"register: rows[{index}] carries parameter_set_hash "
                    f"{declared!r} but its parameter_set hashes to {digest!r} "
                    f"(canonical form {canonical!r}). The two disagree, so "
                    "which one keys the result cannot be decided here. "
                    "Nothing has been written -- send the row with a matching "
                    "hash, or with no parameter_set_hash at all and let it be "
                    "computed."
                )
            out["parameter_set_hash"] = digest
            prepared.append(out)
            if digest not in registry:
                registry[digest] = {
                    "parameter_set_hash": digest,
                    "canonical_json": canonical,
                    "observed_at": out.get("observed_at"),
                    "ingest_batch_id": out.get("ingest_batch_id"),
                }
        return prepared, list(registry.values())

    @staticmethod
    def _parse_parameter_set_field(raw: Any, *, index: int) -> dict[str, Any]:
        """Resolve a row's ``parameter_set`` input field to a mapping, or refuse.

        A mapping passes through. A ``str`` is parsed as JSON with
        ``object_pairs_hook`` so a DUPLICATE KEY is caught rather than
        silently resolved last-wins, must decode to an OBJECT, and must be
        byte-equal to its own canonical form. Anything else is refused.

        Raises:
            ParameterSetError: ``raw`` is neither a mapping nor admissible
                canonical JSON object text; the message names the row index.
        """
        if isinstance(raw, Mapping):
            return dict(raw)
        if not isinstance(raw, str):
            raise ParameterSetError(
                f"register: rows[{index}]['parameter_set'] must be a mapping "
                f"or its canonical JSON text, got {type(raw).__name__}. "
                "Nothing has been written."
            )

        def _reject_duplicate_keys(
            pairs: Sequence[tuple[str, Any]],
        ) -> dict[str, Any]:
            seen: set[str] = set()
            for key, _value in pairs:
                if key in seen:
                    raise ParameterSetError(
                        f"register: rows[{index}]['parameter_set'] JSON text "
                        f"repeats the key {key!r}. A duplicate key has no "
                        "single meaning -- resolving it last-wins would store "
                        "a hash the text does not obviously produce -- so it "
                        "is refused. Nothing has been written."
                    )
                seen.add(key)
            return dict(pairs)

        try:
            parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        except ParameterSetError:
            raise
        except ValueError as exc:
            raise ParameterSetError(
                f"register: rows[{index}]['parameter_set'] is not valid JSON: "
                f"{exc}. Nothing has been written."
            ) from exc
        if not isinstance(parsed, dict):
            raise ParameterSetError(
                f"register: rows[{index}]['parameter_set'] JSON text decodes "
                f"to {type(parsed).__name__}, not an object. A parameter set "
                "is a mapping (possibly empty: '{}'). Nothing has been "
                "written."
            )
        canonical = canonical_parameter_set(parsed)
        if canonical != raw:
            raise ParameterSetError(
                f"register: rows[{index}]['parameter_set'] text {raw!r} is "
                f"not canonical; the canonical form is {canonical!r}. The "
                "text as given is what a reader would compare against, so "
                "accepting it would store a hash it does not produce. Send "
                "the canonical text, or send the mapping and let it be "
                "canonicalised. Nothing has been written."
            )
        return parsed

    def _registered_parameter_set_hashes(self, hashes: Sequence[str]) -> set[str]:
        """Return which of ``hashes`` the parameter-set registry already holds.

        A single ``SELECT DISTINCT`` over the registry on the Spark engine
        (the verb is in-pod only, and the write that follows is a Spark
        ingest). Spark has NO bind channel -- ``BerdlCapability.query``
        refuses ``params`` there -- so :meth:`_run` inlines the hashes after
        re-validating each against ``^[0-9a-f]{64}$``, which is safe because
        every value here came from
        :func:`~kbutillib.domains.identity.parameter_set_hash`. A lookup
        failure is NOT swallowed: if the registry cannot be read, the registry
        state is unknown, and this runs before anything is written, so the
        whole call refuses with nothing written rather than writing blind.
        """
        if not hashes:
            return set()
        table = _quote_fqn(f"{_FQN_PREFIX}.{PARAMETER_SET_TABLE}", "spark")
        sql = (
            f"SELECT DISTINCT parameter_set_hash FROM {table} "
            f"WHERE parameter_set_hash IN ({self._in_placeholders(len(hashes))})"
        )
        rows = self._run(sql, params=list(hashes), engine="spark")
        return {
            row["parameter_set_hash"]
            for row in rows
            if row.get("parameter_set_hash") is not None
        }

    def _write_parameter_set_registry(
        self,
        registry_rows: Sequence[Mapping[str, Any]],
        *,
        tenant: str,
        dataset: str,
        write_ns: str,
    ) -> dict[str, Any]:
        """Append the registry rows whose hash is not already registered.

        Returns a report of ``'written'`` hashes, ``'already_present'``
        hashes, and the registry ``load()`` result (``None`` when every hash
        was already present, in which case no ``load()`` is made at all --
        that is what makes an ordinary retry add no registry row).
        """
        hashes = [row["parameter_set_hash"] for row in registry_rows]
        present = self._registered_parameter_set_hashes(hashes)
        new_rows = [
            dict(row)
            for row in registry_rows
            if row["parameter_set_hash"] not in present
        ]
        report: dict[str, Any] = {
            "written": [row["parameter_set_hash"] for row in new_rows],
            "already_present": [h for h in hashes if h in present],
            "load_result": None,
        }
        if not new_rows:
            return report
        columns = parameter_set_column_names()
        load_result = self._get_capability().load(
            dataset=dataset,
            tables=[{"name": PARAMETER_SET_TABLE, "mode": "append"}],
            tenant=tenant,
            namespace=write_ns,
            dataframes={
                PARAMETER_SET_TABLE: [
                    {column: row.get(column) for column in columns}
                    for row in new_rows
                ]
            },
        )
        report["load_result"] = self._check_postflight(load_result)
        return report

    def ingest_shards(
        self,
        shard_dir: str | Path,
        *,
        run_id: str | None = None,
        dry_run: bool = False,
        reconcile: bool = False,
        tenant: str = TENANT,
        dataset: str = NAMESPACE,
        adopt_source_batch_ids: bool = False,
        unit: str | None = None,
        coverage_floor: float | None = None,
        transfer_deadline_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Ingest a published shard run's batches, FILL-ONLY and resumably.

        IN-POD ONLY. Refuses off-pod (:class:`BerdlLoadRefusedError`) before it
        reads the inventory, resolves any namespace, opens the ledger or makes
        any transport call.

        THE SHARDS ARE ON THIS POD'S OWN DISK. The backfill runs entirely
        inside the BERDL pod and the bronze sharder writes its batches to the
        pod's local persistent home, which Spark EXECUTORS cannot read -- so
        every batch is read HERE, by the driver, with pyarrow, and handed to
        ``BerdlCapability.load()`` in DATAFRAME mode (``dataframes={table:
        ...}``), the same mode :meth:`register` uses. There is no bronze path,
        no ``paths={'bronze_base': ...}`` and no object-storage staging: a
        bronze-mode load pointed at a local file is read cluster-side and
        cannot see it. That was the defect this verb was rewritten to remove.

        ``<shard_dir>/run.json`` IS REQUIRED. The run inventory the sharder
        publishes is the only source of each batch's sha256, row count and
        ``[hash_min, hash_max]`` range, and every integrity check plus the
        whole idempotence story depends on those. A directory without one is
        refused (:class:`clearinghouse_ingest.ClearinghouseBatchIntegrityError`)
        rather than scanned for files, because a scan cannot tell a published
        batch from a half-written one.

        FILL-ONLY, ALWAYS ON, NO FLAG. Before each append the target's
        EXISTING KEYS are read back, restricted to the batch's own hash range,
        and the batch is left-anti-joined against them in Spark; only the
        survivors are appended. The join mirrors
        :func:`clearinghouse_ingest.fill_only_survivors`, which is the pure
        statement of the rule (key modes: ``entity_hash`` for entity/content,
        the slot key IMPORTED from
        :data:`clearinghouse_derivation.SLOT_KEY_COLUMNS` for results, and
        ``parameter_set_hash`` for the registry). A batch with zero survivors
        is committed WITHOUT calling ``load()`` at all. That is what makes a
        second ingest of the same shard directory -- or a reload of a run that
        already landed -- cost nothing and change nothing.

        FILL-ONLY IS NOT ATOMIC AGAINST WRITERS OUTSIDE THIS PROCESS. The
        survivors are computed against the keys as they stood at the read. A
        writer that appends a colliding key between that read and the append
        leaves a duplicate. The per-namespace lock below excludes other
        clearinghouse INGESTS, not arbitrary writers, and the lake has no
        transaction to take; this window is documented rather than closed.

        A LOAD EXECUTION IS NOT A SHARD RUN. ``run_id`` names the EXECUTION:
        omitted, a fresh one is minted; given an existing one, that execution
        is RESUMED (and refuses if it is bound to another namespace or another
        shard run). A new execution derives its OWN inventory from
        ``run.json`` with a fresh ULID per batch, rewrites each row's
        ``ingest_batch_id`` to match, and records the source run id, source
        batch id and source sha256 as provenance -- so the same shard run can
        be loaded into scratch, checked, and promoted to production with each
        load provable by its own per-batch counts.
        ``adopt_source_batch_ids=True`` is for the single case where a shard
        run and its first execution are one operation and the ids in the rows
        are already this execution's own.

        THE LEDGER LIVES WITH THE EXECUTION, NOT THE SHARDS, at
        ``$KBU_CLEARINGHOUSE_STATE_DIR/<run_id>/ingest_ledger.jsonl``
        (default ``~/.kbu/clearinghouse/runs/<run_id>/``). The shard directory
        is sharder output and may be regenerated or deleted; the ledger is the
        only record of what reached the lake. Every line is ``fsync``ed, and a
        durable ``started`` line carrying the target namespace, the batch's
        checksum and the post-filter ``expected_rows`` precedes every append.
        ``expected_rows`` is counted from a MATERIALISED survivor set, so a
        concurrent writer between the count and the append cannot make the
        number of rows appended differ from the number recorded.

        RESUME RECONCILES BEFORE IT TOUCHES ANYTHING. Every batch left
        ``started`` is decided FIRST, by the exact count
        ``COUNT(*) WHERE ingest_batch_id = '<validated ULID>'``:

        - count == ``expected_rows`` -> COMMITTED; the original batch is kept.
        - count == 0 with ``expected_rows`` > 0 -> RETRYABLE; the SAME
          immutable batch is re-read, re-filtered and re-appended under a new
          durable attempt line.
        - any other count -> the run STOPS
          (:class:`ClearinghouseReconcileMismatchError`) and reports the batch
          for repair. A partial append is not something to guess about.

        Without ``reconcile=True`` a ``started`` batch refuses
        (:class:`ClearinghouseLedgerAmbiguousError`) exactly as before.

        THE PRE-COMMIT JOIN ASSERTIONS, PER BATCH, BEFORE ANYTHING IS
        COMMITTED, and EVALUATED ON THE INPUTS. Four of them --
        :func:`clearinghouse_ingest.join_assertions`, which is the whole
        statement of the rule and reports a COUNT for each: the expected input
        is non-empty, every join key is non-null, each target slot is claimed
        by at most one source row, and the share of distinct source keys that
        RESOLVE against the target's identity table is at or above
        ``coverage_floor``. A non-empty batch resolving ZERO keys is fatal
        whatever the floor.

        They are on the INPUTS and not on the join's result because a
        relationship test evaluated over an empty join passes vacuously, which
        is exactly how the 0-of-19,268 hash mismatch would have been reported
        as a clean idempotent no-op: fill-only commits a batch with no
        survivors WITHOUT a write, and nothing distinguished that from a
        re-run. A refused batch is ledgered ``failed`` and the ingest carries
        on, exactly as a corrupt file is -- the report carries the counts and
        the CLI exits non-zero.

        ARRIVAL IS NOT PROVEN BY ``run.json`` EXISTING. Before each batch's
        five-check gate, :func:`clearinghouse_ingest.transfer_state` asks
        whether the file is THERE and whether its bytes are the published
        bytes. A first mismatch is ``syncing`` -- a retry, with the first
        sighting persisted to the ledger so the retry resumes rather than
        restarting verification from nothing -- and past the transfer deadline
        (``transfer_deadline_seconds``, default six hours from ``run.json``'s
        mtime) the same mismatch is a FAILED DELIVERY, reported with the
        batch, expected versus observed size and expected versus observed
        digest.

        SCHEMA PREFLIGHT. Before the first batch, the target namespace's live
        columns are read and the ingest refuses -- writing nothing, calling no
        ``load()`` -- if any ``<type>_result`` table lacks
        ``parameter_set_hash`` or the
        :data:`~clearinghouse_schema.PARAMETER_SET_TABLE` registry table does
        not exist. ``bootstrap()`` cannot detect a MISSING COLUMN on an
        existing table, so this is the only mechanical guard that the result
        tables were actually rebuilt for the five-tuple slot key.

        REGISTRY FIRST, WITHIN AND ACROSS UNITS. Within every unit the
        ``parameter_set`` batch is ingested and committed before any
        ``<type>_result`` batch, whatever order ``run.json`` lists them in
        (the order is imposed by :func:`clearinghouse_ingest.derive_execution_inventory`,
        not trusted from the file), and a unit's result batches are refused
        unless every registry batch of that unit and every earlier one is
        already committed.

        A PER-NAMESPACE LOCK. An exclusive ``flock`` on a file in the shared,
        pod-visible lock directory (``$KBU_CLEARINGHOUSE_LOCK_DIR``, default
        ``/global_share/KBaseUtilities/clearinghouse-locks``) is held for the
        whole ingest and refuses to start if another live process holds it,
        naming the holder. It is an OS lock, so process death releases it and
        a dead holder's lock is reported and taken over.

        ``dry_run=True`` is genuinely read-only: it reads the inventory, runs
        the pre-write assertion and the schema preflight, checks each batch's
        integrity, computes the fill-only survivor count each batch WOULD
        append, and writes nothing at all -- no ``load()``, no ledger line, no
        execution binding and no lock.

        Args:
            shard_dir: A directory a completed shard run published, holding
                ``run.json`` and the per-table batch files it names.
            run_id: The LOAD EXECUTION id. Omitted mints a new execution;
                given an existing one resumes it.
            dry_run: Report what would happen and write nothing.
            reconcile: Decide a ``started`` batch by its exact per-batch-id
                count instead of refusing.
            tenant: Target tenant (default :data:`TENANT`).
            dataset: The ``load()`` dataset / namespace (default
                :data:`NAMESPACE`).
            adopt_source_batch_ids: Keep the shard run's batch ids instead of
                minting fresh ones -- only for a shard-and-load that is one
                operation under one id.
            coverage_floor: The declared floor for the share of a result
                batch's distinct join keys that must resolve against
                ``<type>_entity``. Default
                :data:`clearinghouse_ingest.DEFAULT_COVERAGE_FLOOR`.
            transfer_deadline_seconds: How long after ``run.json``'s mtime a
                missing or mismatched batch is still "arriving" rather than a
                failed delivery. Default
                :data:`clearinghouse_ingest.DEFAULT_TRANSFER_DEADLINE_SECONDS`.

        Returns:
            A dict with ``'run_id'`` (the execution id), ``'execution_id'``,
            ``'source_run_id'``, ``'namespace'``, ``'state_dir'``,
            ``'dry_run'``, ``'lock'`` and a ``'tables'`` list -- one report per
            batch with its ``'name'``, ``'batch_id'``, ``'unit_id'``,
            ``'action'`` (``'ingest'``, ``'skip'``, ``'reconcile'``,
            ``'failed'`` or -- dry-run only -- ``'would_ingest'``),
            ``'rows_in'``, ``'rows_dropped_present'``, ``'rows_ingested'`` and
            ``'snapshot_id'``.

        Raises:
            BerdlLoadRefusedError: Off-pod (before any transport call).
            ClearinghouseBatchIntegrityError: No usable ``run.json``.
            ClearinghouseWriteTargetMismatchError: Write target unconfirmed
                (dev 1206) -- ``load()`` called zero times.
            ClearinghouseSchemaPreflightError: The target is not the
                lake-5 schema -- ``load()`` called zero times.
            ClearinghouseNamespaceLockedError: Another live ingest holds the
                namespace.
            ClearinghouseLedgerAmbiguousError: A ``started`` batch with
                ``reconcile=False``.
            ClearinghouseReconcileMismatchError: A ``started`` batch whose
                count is neither its expectation nor zero.
            ClearinghouseLoadPostflightError: A null postflight (dev 1194).
        """
        self._require_in_pod("ingest_shards")
        shard_root = Path(shard_dir)
        run = ci.read_run_inventory(shard_root)
        floor = (
            ci.DEFAULT_COVERAGE_FLOOR if coverage_floor is None else float(coverage_floor)
        )
        # Validated HERE, before the first batch. _join_assertions turns a
        # ValueError into a per-batch refusal (a batch with no join-key column
        # IS a producer defect), and an out-of-range floor is an OPERATOR
        # error, not that -- it would otherwise be reported once per batch as
        # though the data were wrong.
        if not 0.0 <= floor <= 1.0:
            raise ValueError(
                f"ingest_shards: coverage_floor={coverage_floor!r} is outside "
                "[0, 1]. A floor is a share of the source keys, so a value "
                "outside that range cannot be compared to one."
            )
        deadline = (
            ci.DEFAULT_TRANSFER_DEADLINE_SECONDS
            if transfer_deadline_seconds is None
            else int(transfer_deadline_seconds)
        )
        # Read ONCE, before any batch: the deadline is measured from the
        # marker's own date, so re-reading it per batch would let a sync that
        # re-touched run.json mid-load reset the clock on every later batch.
        manifest_mtime = ci.run_manifest_mtime(shard_root)

        # Pre-write assertion and schema preflight BEFORE anything is written,
        # so either refusal leaves load() called exactly zero times -- in
        # dry_run as well as a real run.
        write_ns = self._assert_write_target(dataset=dataset, tenant=tenant)
        self._assert_lake5_schema(write_ns)

        execution_id = run_id or (
            str(run.get("run_id"))
            if adopt_source_batch_ids and run.get("run_id")
            else ci.new_execution_id()
        )
        binding = ci.bind_execution(
            execution_id=execution_id,
            shard_dir=shard_root,
            namespace=write_ns,
            tenant=tenant,
            dataset=dataset,
            run=run,
            adopt_source_batch_ids=adopt_source_batch_ids,
        )
        inventory: list[dict[str, Any]] = list(binding["inventory"])
        # A `unit` restriction NARROWS WHAT IS INGESTED, NOT WHAT IS CHECKED.
        # The inventory stays whole: the cross-unit registry gate
        # (_assert_registry_committed_through) reads the PREFIX of the
        # inventory before each result batch, so filtering the list would
        # shrink that prefix and let a result batch load while an EARLIER
        # unit's registry batch was still missing -- the exact orphan the gate
        # exists to prevent. Instead the unit is passed down and non-matching
        # entries are skipped at the point of ingest.
        if unit is not None:
            ci.assert_unit_in_inventory(inventory, unit, shard_dir=shard_root)
        state_dir = ci.execution_state_dir(execution_id)
        ledger = ci.ExecutionLedger(state_dir)

        if dry_run:
            return {
                "run_id": execution_id,
                "execution_id": execution_id,
                "source_run_id": run.get("run_id"),
                "namespace": write_ns,
                "state_dir": str(state_dir),
                "dry_run": True,
                "lock": None,
                "unit": unit,
                "coverage_floor": floor,
                "transfer_deadline_seconds": deadline,
                "tables": self._dry_run_reports(
                    inventory,
                    shard_root=shard_root,
                    write_ns=write_ns,
                    unit=unit,
                    coverage_floor=floor,
                    manifest_mtime=manifest_mtime,
                    deadline_seconds=deadline,
                ),
            }

        lock = ci.NamespaceLock(write_ns)
        lock_report = lock.acquire(execution_id=execution_id)
        if lock.previous_holder is not None:
            lock_report = {
                **lock_report,
                "took_over_from": lock.previous_holder,
            }
        try:
            ci.write_execution(state_dir, binding)
            # RECONCILE FIRST, before any batch is read or appended: a batch
            # left 'started' owns rows that may be in the table, and nothing
            # else may be decided until it is.
            self._reconcile_started_batches(
                inventory,
                ledger=ledger,
                write_ns=write_ns,
                reconcile=reconcile,
                execution_id=execution_id,
            )
            reports = self._ingest_inventory(
                inventory,
                ledger=ledger,
                shard_root=shard_root,
                write_ns=write_ns,
                tenant=tenant,
                dataset=dataset,
                execution_id=execution_id,
                unit=unit,
                coverage_floor=floor,
                manifest_mtime=manifest_mtime,
                deadline_seconds=deadline,
            )
        finally:
            lock.release()

        return {
            "run_id": execution_id,
            "execution_id": execution_id,
            "source_run_id": run.get("run_id"),
            "namespace": write_ns,
            "state_dir": str(state_dir),
            "dry_run": False,
            "lock": lock_report,
            "unit": unit,
            "coverage_floor": floor,
            "transfer_deadline_seconds": deadline,
            "tables": reports,
        }

    # -- ingest: the per-batch machinery ---------------------------------

    def _dry_run_reports(
        self,
        inventory: Sequence[Mapping[str, Any]],
        *,
        shard_root: Path,
        write_ns: str,
        unit: str | None = None,
        coverage_floor: float = ci.DEFAULT_COVERAGE_FLOOR,
        manifest_mtime: float | None = None,
        deadline_seconds: int = ci.DEFAULT_TRANSFER_DEADLINE_SECONDS,
    ) -> list[dict[str, Any]]:
        """Report what each batch WOULD do, writing nothing.

        Runs the real integrity checks, the real arrival check, the real
        pre-commit join assertions and the real fill-only read, because a dry
        run whose answer came from a different code path would not be evidence
        about the run that follows. A failure is REPORTED here rather than
        ledgered -- a dry run writes no ledger line, by the same guarantee
        ``clearinghouse_schema.bootstrap``'s dry-run makes. THIS IS THE
        OPERATOR'S GATE: ``--dry-run`` is where a wrong join key should be
        caught, so the assertions have to run here or the gate would pass
        exactly the load that fails.
        """
        reports: list[dict[str, Any]] = []
        for entry in inventory:
            if unit is not None and entry.get("unit_id") != unit:
                continue
            report: dict[str, Any] = {
                "name": entry.get("table"),
                "batch_id": entry.get("batch_id"),
                "unit_id": entry.get("unit_id"),
            }
            transfer = ci.transfer_state(
                entry,
                shard_root=shard_root,
                manifest_mtime=manifest_mtime,
                deadline_seconds=deadline_seconds,
            )
            if not ci.has_arrived(transfer):
                report.update(
                    {
                        "action": "would_wait"
                        if transfer["state"] == "syncing"
                        else "would_refuse",
                        "transfer": transfer,
                        "error": ci.describe_transfer(transfer),
                    }
                )
                reports.append(report)
                continue
            try:
                batch = ci.verify_batch_file(
                    entry,
                    shard_root=shard_root,
                    expected_batch_id=str(entry.get("source_batch_id")),
                    expected_sha256=str(entry.get("source_sha256") or ""),
                    observed_sha256=transfer.get("observed_sha256"),
                )
                assertions = self._join_assertions(
                    entry, batch=batch, write_ns=write_ns, coverage_floor=coverage_floor
                )
                _survivors, expected_rows = self._fill_only_survivors(
                    entry, batch=batch, write_ns=write_ns
                )
            except ci.ClearinghouseBatchIntegrityError as exc:
                report.update({"action": "would_refuse", "error": str(exc)})
                reports.append(report)
                continue
            report.update(
                {
                    "action": "would_ingest" if assertions["ok"] else "would_refuse",
                    "rows_in": batch.num_rows,
                    "rows_dropped_present": batch.num_rows - expected_rows,
                    "rows_ingested": expected_rows,
                    "join_assertions": assertions,
                }
            )
            if not assertions["ok"]:
                report["error"] = "; ".join(assertions["failures"])
            reports.append(report)
        return reports

    def _ingest_inventory(
        self,
        inventory: Sequence[Mapping[str, Any]],
        *,
        ledger: Any,
        shard_root: Path,
        write_ns: str,
        tenant: str,
        dataset: str,
        execution_id: str,
        unit: str | None = None,
        coverage_floor: float = ci.DEFAULT_COVERAGE_FLOOR,
        manifest_mtime: float | None = None,
        deadline_seconds: int = ci.DEFAULT_TRANSFER_DEADLINE_SECONDS,
    ) -> list[dict[str, Any]]:
        """Ingest every batch of the derived inventory, in load order.

        ``unit``, when given, restricts WHICH batches are ingested -- but the
        loop still walks the WHOLE inventory, because ``position`` is what the
        cross-unit registry gate measures its prefix against. A unit loaded
        out of order therefore refuses (an earlier unit's registry batch is
        not committed) instead of writing results whose parameters nothing can
        explain, which is the correct answer rather than a limitation.

        Four states drive it, and nothing else: ``committed`` is skipped,
        ``started`` can only be a batch the reconcile pass decided RETRYABLE
        (an undecided one raised there, before this ran), ``syncing`` is a
        batch whose bytes had not arrived last time and is RE-CHECKED from the
        first sighting it persisted, and anything else -- no line, or
        ``failed`` -- is ingested. A failed batch IS retried, because the batch
        file is immutable and fill-only makes the retry harmless; what a failed
        line must not do is disappear, and :meth:`verify_run` blocks on one
        that was never resolved.
        """
        states = ledger.records()
        reports: list[dict[str, Any]] = []
        for position, entry in enumerate(inventory):
            if unit is not None and entry.get("unit_id") != unit:
                continue
            batch_id = str(entry["batch_id"])
            table = str(entry["table"])
            record = states.get(batch_id)
            state = None if record is None else record.get("state")
            if state == ci.LEDGER_COMMITTED:
                reports.append(
                    {
                        "name": table,
                        "batch_id": batch_id,
                        "unit_id": entry.get("unit_id"),
                        # 'reconcile' when THIS call's reconcile pass is what
                        # committed it, so the report says which batches the
                        # operator's --reconcile actually decided.
                        "action": (
                            "reconcile" if record.get("reconciled") else "skip"
                        ),
                        "rows_in": record.get("rows_in"),
                        "rows_dropped_present": record.get("rows_dropped_present"),
                        "rows_ingested": record.get("rows_ingested"),
                        "snapshot_id": record.get("snapshot_id"),
                    }
                )
                continue

            if table != PARAMETER_SET_TABLE and table.endswith("_result"):
                self._assert_registry_committed_through(
                    inventory, position=position, ledger=ledger
                )

            report = self._ingest_one_batch(
                entry,
                ledger=ledger,
                shard_root=shard_root,
                write_ns=write_ns,
                tenant=tenant,
                dataset=dataset,
                execution_id=execution_id,
                coverage_floor=coverage_floor,
                manifest_mtime=manifest_mtime,
                deadline_seconds=deadline_seconds,
                # THE PERSISTED PROGRESS a retry resumes from: a previous
                # `syncing` line's first sighting, so the transfer deadline is
                # measured against the first observation rather than restarted
                # from nothing on every re-run.
                prior=record,
            )
            reports.append(report)
        return reports

    def _assert_registry_committed_through(
        self,
        inventory: Sequence[Mapping[str, Any]],
        *,
        position: int,
        ledger: Any,
    ) -> None:
        """Refuse a result batch until every registry batch before it committed.

        REGISTRY ACROSS UNITS. A result row's ``parameter_set_hash`` may have
        been registered by its OWN unit or by an earlier one, so "the registry
        is loaded first" has to mean every registry batch up to this point --
        not merely this unit's. The inventory is in load order with each
        unit's registry batch ahead of its results, so "up to this point" is
        exactly the prefix.
        """
        states = ledger.records()
        pending = [
            str(entry["batch_id"])
            for entry in inventory[:position]
            if entry.get("table") == PARAMETER_SET_TABLE
            and (states.get(str(entry["batch_id"])) or {}).get("state")
            != ci.LEDGER_COMMITTED
        ]
        if pending:
            raise ClearinghouseRegistryOrderError(
                "ingest: refusing to load a <type>_result batch while "
                f"{len(pending)} parameter-set registry batch(es) of this or "
                f"an earlier unit are not committed ({', '.join(pending[:3])}"
                f"{', ...' if len(pending) > 3 else ''}). A result row whose "
                "parameter_set_hash the registry cannot explain is an orphan: "
                "the hash is opaque, so nothing can say what parameters "
                "produced the result. The registry is always written first."
            )

    def _ingest_one_batch(
        self,
        entry: Mapping[str, Any],
        *,
        ledger: Any,
        shard_root: Path,
        write_ns: str,
        tenant: str,
        dataset: str,
        execution_id: str,
        coverage_floor: float = ci.DEFAULT_COVERAGE_FLOOR,
        manifest_mtime: float | None = None,
        deadline_seconds: int = ci.DEFAULT_TRANSFER_DEADLINE_SECONDS,
        prior: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Arrival-check, integrity-check, ASSERT, fill-only filter, append.

        The order is the order that makes each step meaningful, and every one
        of them happens BEFORE anything is committed for this batch:

        1. ARRIVAL (:func:`clearinghouse_ingest.transfer_state`) -- are the
           bytes here, and are they the published bytes? A first mismatch is
           ``syncing`` and is ledgered as such with its first sighting, so a
           retry resumes the deadline instead of restarting from nothing; past
           the deadline it is a FAILED DELIVERY with the evidence.
        2. The five-check integrity gate
           (:func:`clearinghouse_ingest.verify_batch_file`).
        3. THE PRE-COMMIT JOIN ASSERTIONS, on the INPUTS
           (:meth:`_join_assertions`). A refusal is ledgered ``failed`` and
           the ingest carries on to the next batch.
        4. Retag, fill-only filter, durable ``started`` line, append.
        """
        batch_id = ci.validate_batch_id(str(entry["batch_id"]))
        table = str(entry["table"])
        base = {
            "execution_id": execution_id,
            "batch_id": batch_id,
            "table": table,
            "unit_id": entry.get("unit_id"),
            "namespace": write_ns,
            "source_batch_id": entry.get("source_batch_id"),
            "sha256": entry.get("source_sha256"),
        }
        prior_record = dict(prior or {})
        transfer = ci.transfer_state(
            entry,
            shard_root=shard_root,
            manifest_mtime=manifest_mtime,
            deadline_seconds=deadline_seconds,
            first_observed_at=(
                str(prior_record.get("first_observed_at"))
                if prior_record.get("state") == ci.LEDGER_SYNCING
                and prior_record.get("first_observed_at")
                else None
            ),
        )
        if not ci.has_arrived(transfer):
            evidence = ci.describe_transfer(transfer)
            syncing = transfer["state"] == "syncing"
            # PERSIST THE PROGRESS EITHER WAY. The `syncing` line is what the
            # retry resumes the deadline from; the `failed` line is what
            # blocks verification so a lost file cannot be forgotten.
            ledger.append(
                {
                    **base,
                    "state": ci.LEDGER_SYNCING if syncing else ci.LEDGER_FAILED,
                    "transfer": transfer,
                    "first_observed_at": transfer["first_observed_at"],
                    "error": evidence,
                }
            )
            return {
                "name": table,
                "batch_id": batch_id,
                "unit_id": entry.get("unit_id"),
                "action": "syncing" if syncing else "failed",
                "transfer": transfer,
                "error": evidence,
            }
        try:
            batch = ci.verify_batch_file(
                entry,
                shard_root=shard_root,
                expected_batch_id=str(entry.get("source_batch_id")),
                expected_sha256=str(entry.get("source_sha256") or ""),
                # Already streamed by the arrival check, immediately above: a
                # full-size batch is hashed once per load, not twice.
                observed_sha256=transfer.get("observed_sha256"),
            )
        except ci.ClearinghouseBatchIntegrityError as exc:
            # REFUSE THIS BATCH, LEDGER IT, CARRY ON. One corrupt file does
            # not invalidate the other batches, and a failed line blocks
            # verification of the run, so the failure cannot be lost.
            ledger.append({**base, "state": ci.LEDGER_FAILED, "error": str(exc)})
            return {
                "name": table,
                "batch_id": batch_id,
                "unit_id": entry.get("unit_id"),
                "action": "failed",
                "error": str(exc),
            }
        assertions = self._join_assertions(
            entry, batch=batch, write_ns=write_ns, coverage_floor=coverage_floor
        )
        if not assertions["ok"]:
            # REFUSE THIS BATCH, LEDGER IT, CARRY ON -- the same shape a
            # corrupt file takes, for the same reasons: one producer defect in
            # one batch does not invalidate the batches that committed, and a
            # ledgered failure blocks verify_run so it cannot be lost. The
            # CLI's exit status is non-zero off the back of this report.
            error = "; ".join(assertions["failures"])
            ledger.append(
                {
                    **base,
                    "state": ci.LEDGER_FAILED,
                    "join_assertions": assertions,
                    "error": error,
                }
            )
            return {
                "name": table,
                "batch_id": batch_id,
                "unit_id": entry.get("unit_id"),
                "action": "refused",
                "rows_in": batch.num_rows,
                "join_assertions": assertions,
                "error": error,
            }
        if not entry.get("adopted_source_batch_id"):
            batch = ci.retag_batch_ids(batch, batch_id)
        survivors, expected_rows = self._fill_only_survivors(
            entry, batch=batch, write_ns=write_ns
        )
        rows_in = batch.num_rows
        dropped = rows_in - expected_rows

        if expected_rows == 0:
            # Nothing survives, so nothing is appended -- and crucially NO
            # load() is made at all. This is the whole point of fill-only: a
            # second ingest of a landed run is free.
            ledger.append(
                {
                    **base,
                    "state": ci.LEDGER_COMMITTED,
                    "rows_in": rows_in,
                    "rows_dropped_present": dropped,
                    "rows_ingested": 0,
                    "expected_rows": 0,
                    "snapshot_id": None,
                    "load_called": False,
                }
            )
            return {
                "name": table,
                "batch_id": batch_id,
                "unit_id": entry.get("unit_id"),
                "action": "ingest",
                "rows_in": rows_in,
                "rows_dropped_present": dropped,
                "rows_ingested": 0,
                "snapshot_id": None,
                "join_assertions": assertions,
            }

        # The durable 'started' line: target namespace, checksum and the
        # expected_rows counted from the ALREADY-MATERIALISED survivors, so
        # what is appended cannot differ from what was recorded.
        ledger.append(
            {
                **base,
                "state": ci.LEDGER_STARTED,
                "rows_in": rows_in,
                "rows_dropped_present": dropped,
                "expected_rows": expected_rows,
            }
        )
        try:
            result = self._get_capability().load(
                dataset=dataset,
                tables=[{"name": table, "mode": "append"}],
                tenant=tenant,
                namespace=write_ns,
                dataframes={table: survivors},
            )
            result = self._check_postflight(result)
        except Exception as exc:
            ledger.append(
                {
                    **base,
                    "state": ci.LEDGER_FAILED,
                    "rows_in": rows_in,
                    "rows_dropped_present": dropped,
                    "expected_rows": expected_rows,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            raise
        snapshot_id = _first_table_report(result, table).get("new_snapshot_id")
        ledger.append(
            {
                **base,
                "state": ci.LEDGER_COMMITTED,
                "rows_in": rows_in,
                "rows_dropped_present": dropped,
                "rows_ingested": expected_rows,
                "expected_rows": expected_rows,
                "snapshot_id": snapshot_id,
                "load_called": True,
            }
        )
        return {
            "name": table,
            "batch_id": batch_id,
            "unit_id": entry.get("unit_id"),
            "action": "ingest",
            "rows_in": rows_in,
            "rows_dropped_present": dropped,
            "rows_ingested": expected_rows,
            "snapshot_id": snapshot_id,
            "join_assertions": assertions,
        }

    # -- the pre-commit join assertions and the key space they resolve --

    def _join_assertions(
        self,
        entry: Mapping[str, Any],
        *,
        batch: Any,
        write_ns: str,
        coverage_floor: float = ci.DEFAULT_COVERAGE_FLOOR,
    ) -> dict[str, Any]:
        """Run the four pre-commit assertions over one batch's INPUTS.

        Thin over :func:`clearinghouse_ingest.join_assertions`, which is the
        statement of the rule; what lives here is the one transport read the
        coverage assertion needs (:meth:`_resolvable_join_keys`).

        ON THE INPUTS, NEVER ON THE JOIN. ``batch`` is the source rows as the
        inventory published them and the resolvable key set is read from the
        target's identity table INDEPENDENTLY -- not from the fill-only
        anti-join's output, whose emptiness is the very condition being
        tested. A relationship test evaluated over an empty join passes
        vacuously, and that is how a wrong join key reads as a clean no-op.
        """
        table = str(entry["table"])
        batch_id = str(entry.get("batch_id"))
        try:
            return ci.join_assertions(
                batch,
                table=table,
                batch_id=batch_id,
                resolvable=self._resolvable_join_keys(entry, write_ns=write_ns),
                coverage_floor=coverage_floor,
            )
        except ValueError as exc:
            # A batch with no join-key column, or a table with no known key
            # mode, is a PRODUCER defect of exactly the class this set
            # reports -- so it becomes a failing report (ledgered, counted,
            # non-zero exit) rather than a traceback that unwinds a load whose
            # other batches are fine.
            return {
                "table": table,
                "batch_id": batch_id,
                "join_key": None,
                "slot_key": [],
                "source_rows": getattr(batch, "num_rows", None),
                "source_keys": None,
                "null_keys": None,
                "ambiguous_slots": None,
                "resolvable_keys": None,
                "resolved_keys": None,
                "unresolved_keys": None,
                "coverage": None,
                "coverage_floor": float(coverage_floor),
                "coverage_checked": False,
                "ok": False,
                "failures": [f"{table} batch {batch_id}: {exc}"],
            }

    def _resolvable_join_keys(
        self, entry: Mapping[str, Any], *, write_ns: str
    ) -> set[str] | None:
        """The target identity keys a result batch's join keys must land on.

        ONE read per batch, restricted to the batch's own
        ``[hash_min, hash_max]`` window on ``entity_hash``, exactly as
        :meth:`_existing_keys` is and affordable for the same reason: the
        sharder sorted the batch, so the keys that could possibly resolve are
        a bounded slice of the hash space rather than the whole table. The
        bounds are validated against ``^[0-9a-f]{64}$`` before being inlined,
        because the read runs on Spark and there is no bind channel.

        ``None`` for every table family that MINTS identity rather than
        referencing it (:func:`clearinghouse_ingest.resolvable_source_table`),
        which makes the coverage assertion record itself as skipped rather
        than silently passed.
        """
        table = str(entry["table"])
        source = ci.resolvable_source_table(table)
        if source is None:
            return None
        column = ci.range_column_for_table(source)
        hash_min = ci.validate_hex64(
            str(entry.get("hash_min")),
            where=f"batch {entry.get('batch_id')!r} hash_min",
        )
        hash_max = ci.validate_hex64(
            str(entry.get("hash_max")),
            where=f"batch {entry.get('batch_id')!r} hash_max",
        )
        fqn = _quote_fqn(f"{_FQN_PREFIX}.{source}", "spark")
        rows = self._run(
            f"SELECT {column} FROM {fqn} "
            f"WHERE {column} BETWEEN '{hash_min}' AND '{hash_max}'",
            params=None,
            engine="spark",
        )
        return {
            str(row.get(column)) for row in rows if row.get(column) is not None
        }

    # -- fill-only: the Spark anti-join and the keys it joins against ----

    def _existing_keys(self, entry: Mapping[str, Any], *, write_ns: str) -> Any:
        """Read the target's existing key tuples inside this batch's range.

        ONE read per batch, restricted to ``[hash_min, hash_max]`` on the
        batch's own range column (``entity_hash``, or ``parameter_set_hash``
        for the registry) -- which is the only reason this is affordable
        against a populated corpus: a batch covers a contiguous slice of the
        hash space because the sharder sorted it, so the keys that could
        possibly collide are a bounded window rather than the whole table.

        The bounds are INLINED after being validated against
        ``^[0-9a-f]{64}$`` (:func:`clearinghouse_ingest.validate_hex64`),
        because the read runs on Spark and ``BerdlCapability.query`` has no
        bind channel there -- the same validated-inlining discipline every
        bulk read in this class uses.
        """
        import pyarrow as pa  # noqa: PLC0415 - import-cheap

        table = str(entry["table"])
        key_columns = ci.key_columns_for_table(table)
        range_column = ci.range_column_for_table(table)
        hash_min = ci.validate_hex64(
            str(entry.get("hash_min")), where=f"batch {entry.get('batch_id')!r} hash_min"
        )
        hash_max = ci.validate_hex64(
            str(entry.get("hash_max")), where=f"batch {entry.get('batch_id')!r} hash_max"
        )
        fqn = _quote_fqn(f"{_FQN_PREFIX}.{table}", "spark")
        columns = ", ".join(key_columns)
        rows = self._run(
            f"SELECT {columns} FROM {fqn} "
            f"WHERE {range_column} BETWEEN '{hash_min}' AND '{hash_max}'",
            params=None,
            engine="spark",
        )
        return pa.table(
            {
                column: pa.array(
                    [row.get(column) for row in rows], type=pa.string()
                )
                for column in key_columns
            }
        )

    def _fill_only_survivors(
        self, entry: Mapping[str, Any], *, batch: Any, write_ns: str
    ) -> tuple[Any, int]:
        """Materialise the rows of ``batch`` the target does not already hold.

        MIRRORS :func:`clearinghouse_ingest.fill_only_survivors` EXACTLY, and
        that function is the specification: a left-anti-join of the batch
        against the target's existing keys on the key columns, keeping row
        order and the full schema, keeping within-batch duplicates, and
        treating a NULL anywhere in a key tuple as unmatched (as SQL does).
        The pure function is tested directly; this path is tested against a
        Spark stand-in that applies it, so the two cannot drift silently.

        THE SURVIVORS ARE MATERIALISED ONCE, HERE, and counted from that
        materialisation -- ``cache()`` then ``count()`` on the Spark path --
        before any ``started`` line is written. A recomputed DataFrame would
        re-read the target, so a concurrent writer could make the rows
        appended differ from the ``expected_rows`` recorded, which is the one
        thing the ledger must never be wrong about.

        Returns:
            ``(payload, row_count)`` where ``payload`` is what goes to
            ``load(dataframes={table: payload})``: a cached Spark DataFrame
            when a session is available, otherwise the survivor rows as a
            list of dicts -- the same DataFrame-mode payload shape
            :meth:`_write_parameter_set_registry` already uses.
        """
        key_columns = ci.key_columns_for_table(str(entry["table"]))
        existing = self._existing_keys(entry, write_ns=write_ns)
        spark = self._spark_session()
        if spark is None:
            survivors = ci.fill_only_survivors(batch, existing, key_columns)
            return survivors.to_pylist(), survivors.num_rows
        batch_df = spark.createDataFrame(batch.to_pylist())
        if existing.num_rows:
            keys_df = spark.createDataFrame(existing.to_pylist())
            survivors_df = batch_df.join(
                keys_df, on=list(key_columns), how="left_anti"
            )
        else:
            survivors_df = batch_df
        survivors_df = survivors_df.cache()
        return survivors_df, int(survivors_df.count())

    def _spark_session(self) -> Any:
        """The pod's Spark session, or ``None`` when the collaborator has none.

        ``None`` is not a failure: the survivors are then computed by the pure
        function in the driver and passed as a list of dicts, exactly as
        :meth:`_write_parameter_set_registry` passes its registry rows. The
        answer is identical either way -- the Spark path exists because a
        full-size batch's anti-join belongs in the engine, not because the
        semantics differ.
        """
        getter = getattr(self._get_capability(), "spark_session", None)
        if getter is None:
            return None
        return getter()

    # -- the lake-5 schema preflight -------------------------------------

    def _assert_lake5_schema(self, write_ns: str) -> None:
        """Refuse a target that is not the lake-5 schema, before any write.

        Reads the live columns of every ``<type>_result`` table and of the
        parameter-set registry and refuses unless each result table carries
        ``parameter_set_hash`` and the registry table exists. ``bootstrap()``
        creates a table that does not exist but CANNOT add a column to one
        that does, so an un-rebuilt result table from before lake-5 looks
        perfectly healthy to it -- and an append of five-tuple-keyed rows into
        a four-tuple table is unrecoverable. This check is the only mechanical
        guard that the operator's rebuild (lake-5 OP-C1) actually happened.

        Raises:
            ClearinghouseSchemaPreflightError: Naming every table that is
                missing the column or missing entirely. ``load()`` has been
                called zero times.
        """
        findings: list[str] = []
        for config in table_configs():
            name = config["name"]
            if name != PARAMETER_SET_TABLE and not name.endswith("_result"):
                continue
            columns = self._live_columns(name)
            if columns is None:
                findings.append(f"{name}: table does not exist in {write_ns}")
            elif name != PARAMETER_SET_TABLE and "parameter_set_hash" not in columns:
                findings.append(
                    f"{name}: exists but has no 'parameter_set_hash' column "
                    f"(columns: {', '.join(sorted(columns)) or 'none'})"
                )
        if findings:
            raise ClearinghouseSchemaPreflightError(
                "ingest: refusing to load -- the target namespace "
                f"{write_ns!r} is not the lake-5 clearinghouse schema:\n  "
                + "\n  ".join(findings)
                + "\nThe result slot key ends in parameter_set_hash and every "
                "result row carries one, so an append into a table without "
                "that column either fails or silently drops the only thing "
                "that says which parameters produced the result. bootstrap() "
                "cannot add a column to an existing table, so the result "
                "tables must be rebuilt (operator step OP-C1) before this "
                "run. Nothing has been written and load() has been called "
                "zero times."
            )

    def _live_columns(self, name: str) -> set[str] | None:
        """The live column names of one clearinghouse table, or ``None``.

        ``DESCRIBE`` on Spark, which is the schema probe available in-pod
        without a write. ``None`` means the table could not be described --
        the only reading of which that matters here is "it is not there",
        which is itself a refusal, so the distinction between an absent table
        and an unreadable one is not one this guard needs to draw.
        """
        fqn = _quote_fqn(f"{_FQN_PREFIX}.{name}", "spark")
        try:
            rows = self._run(f"DESCRIBE {fqn}", params=None, engine="spark")
        except Exception:
            return None
        columns: set[str] = set()
        for row in rows:
            value = row.get("col_name") if isinstance(row, dict) else None
            if value is None and isinstance(row, dict) and row:
                value = next(iter(row.values()), None)
            if isinstance(value, str) and value and not value.startswith("#"):
                columns.add(value)
        return columns or None

    # -- reconcile -------------------------------------------------------

    def _reconcile_started_batches(
        self,
        inventory: Sequence[Mapping[str, Any]],
        *,
        ledger: Any,
        write_ns: str,
        reconcile: bool,
        execution_id: str,
    ) -> None:
        """Decide every ``started`` batch BEFORE anything else is touched.

        RECONCILE BEFORE DELETE OR REGENERATE. A ``started`` batch may own
        rows that are already in the table, so its file and its inventory
        entry are protected until the exact count has decided it. This runs as
        a PASS over the whole inventory before the ingest loop, which is what
        makes that ordering a property of the verb rather than of the order
        batches happen to appear in.

        The rule is the count, never a heuristic (the previous implementation
        treated ANY snapshot on the table as proof a batch had landed, which
        on a populated corpus is always true and therefore proves nothing):

        - ``COUNT(*) WHERE ingest_batch_id = <batch>`` == ``expected_rows``
          -> committed, the original batch kept.
        - 0 with ``expected_rows`` > 0 -> retryable; the same immutable batch
          is re-filtered and re-appended under a new attempt line.
        - anything else -> stop the run and report it for repair.

        A batch decided RETRYABLE is left at ``started`` and nothing else is
        done to it here: the ingest loop re-reads it, re-runs fill-only
        against the target as it now stands, and writes a fresh ``started``
        attempt line before appending. That is the "retry the SAME immutable
        batch under a new attempt line" rule, and it falls out of leaving the
        state alone rather than needing a second channel to carry it.

        Raises:
            ClearinghouseLedgerAmbiguousError: ``reconcile=False``.
            ClearinghouseReconcileMismatchError: A count that is neither the
                expectation nor zero.
        """
        states = ledger.records()
        for entry in inventory:
            batch_id = str(entry["batch_id"])
            record = states.get(batch_id)
            if record is None or record.get("state") != ci.LEDGER_STARTED:
                continue
            table = str(entry["table"])
            if not reconcile:
                raise ClearinghouseLedgerAmbiguousError(
                    f"ingest: execution {execution_id!r} has batch "
                    f"{batch_id!r} of table {table!r} at state "
                    f"{ci.LEDGER_STARTED!r} with no terminal line -- the "
                    "process died around the append and whether its rows "
                    "landed is unknown. Refusing to continue past it: a "
                    "duplicate <type>_content row has no current-state "
                    "derivation to collapse it. Re-run with reconcile=True to "
                    "decide it by the exact count of rows carrying this "
                    "batch id."
                )
            expected = record.get("expected_rows")
            expected_rows = int(expected) if isinstance(expected, int) else 0
            counted = self._count_batch_rows(table, batch_id)
            if counted == expected_rows:
                ledger.append(
                    {
                        "execution_id": execution_id,
                        "batch_id": batch_id,
                        "table": table,
                        "unit_id": entry.get("unit_id"),
                        "namespace": write_ns,
                        "state": ci.LEDGER_COMMITTED,
                        "rows_in": record.get("rows_in"),
                        "rows_dropped_present": record.get("rows_dropped_present"),
                        "rows_ingested": counted,
                        "expected_rows": expected_rows,
                        "snapshot_id": None,
                        "reconciled": True,
                    }
                )
                continue
            if counted == 0 and expected_rows > 0:
                continue  # left at 'started'; the ingest loop retries it
            raise ClearinghouseReconcileMismatchError(
                f"ingest: reconcile of batch {batch_id!r} (table {table!r}, "
                f"namespace {write_ns!r}) counted {counted} rows carrying that "
                f"ingest_batch_id but the ledger expected {expected_rows}. "
                "Neither 'it landed' nor 'it did not' is true, so this is a "
                "PARTIAL append and no automatic decision is safe: appending "
                "again would duplicate the rows that are there, and leaving it "
                "would under-count the run. The run stops here. Repair the "
                "batch (the rows carrying this id are the ones to remove) and "
                "resume."
            )

    def _count_batch_rows(self, name: str, batch_id: str) -> int:
        """Count the rows of ``name`` carrying exactly this ``ingest_batch_id``.

        THE reconcile and verification primitive, replacing the any-snapshot
        heuristic. ``batch_id`` is validated as a ULID
        (:func:`clearinghouse_ingest.validate_batch_id`) immediately before it
        is inlined, because the read runs on Spark and there is no bind
        channel there.
        """
        validated = ci.validate_batch_id(batch_id)
        fqn = _quote_fqn(f"{_FQN_PREFIX}.{name}", "spark")
        rows = self._run(
            f"SELECT COUNT(*) AS row_count FROM {fqn} "
            f"WHERE ingest_batch_id = '{validated}'",
            params=None,
            engine="spark",
        )
        value = _scalar(rows, "row_count")
        return int(value) if isinstance(value, (int, float)) else 0

    def _live_snapshot_ids(self, name: str) -> set[str]:
        """Return the set of snapshot ids currently present on ``<ns>.<name>``.

        A single read-only ``query(..., engine="spark")`` against the table's
        Iceberg ``.snapshots`` metadata table. Used by :meth:`verify_run` to
        confirm the snapshot a ledger line recorded for a committed batch still
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
        unit: str | None = None,
    ) -> dict[str, Any]:
        """Prove a load execution landed, batch by batch, against the live table.

        IN-POD ONLY. Refuses off-pod before any transport call. This is the
        POST-INGEST audit half of the ingest contract, and it is a PER-BATCH
        proof rather than a table-wide count: a table-wide ``COUNT(*)`` on a
        populated corpus can be satisfied by anyone's rows, so it cannot
        distinguish "this execution's batches landed" from "the table is
        big". Every batch in the execution's inventory must show BOTH:

        - a TERMINAL ``committed`` ledger line, and
        - an EXACT live count of rows carrying its ``ingest_batch_id`` equal
          to the ``rows_ingested`` that line recorded.

        Anything missing, failed, unresolved (still ``started``) or in EXCESS
        of its recorded rows is a discrepancy and blocks completion. Every
        snapshot id the ledger recorded is also confirmed still present in the
        table's Iceberg history.

        THE UNIT IS THE UNIT OF COMPLETION. A unit's batches were published
        atomically by the sharder, so a unit is the smallest thing that can be
        complete. A unit completes only when every one of its batches is
        proven AND every distinct ``parameter_set_hash`` in its result batches
        is present in the target's registry -- a result whose parameters the
        registry cannot explain is an orphan, and no amount of row counting
        makes it interpretable.

        It then runs the NAMESPACE-WIDE registry integrity check
        (:meth:`parameter_set_orphans`) and folds its findings into
        ``'discrepancies'``. That check is namespace-wide on purpose: an
        orphan is a property of the tables as they now stand and the writer
        that left it is not necessarily this execution.

        It WRITES NOTHING -- no ``load()``, no ledger line. A discrepancy is
        REPORTED, not raised: the correct operator response to a load reported
        FAILED on a null postflight (dev 1194) is precisely to run verify, so
        this verb must be able to say what it found without failing.

        Args:
            shard_dir: The shard directory the execution drew from. Used only
                to read the batch files' ``parameter_set_hash`` values for the
                unit registry check -- the ledger and inventory come from the
                execution's own state directory.
            run_id: The LOAD EXECUTION id to verify.
            unit: Verify only this unit's batches. UNLIKE ingest, filtering IS
                correct here: a unit's batches were published atomically and
                its completeness is a statement about itself, so verifying one
                unit says nothing false about the others. The namespace-wide
                registry check still runs -- it is a property of the tables as
                they now stand, not of a unit.

        Returns:
            A dict with ``'run_id'``/``'execution_id'``, ``'namespace'``,
            ``'state_dir'``, ``'ok'``, a ``'batches'`` list (per batch:
            ``'batch_id'``, ``'name'``, ``'unit_id'``, ``'ledger_rows'``,
            ``'live_rows'``, ``'state'``, ``'ok'``), a ``'units'`` list (per
            unit: ``'unit_id'``, ``'ok'``, ``'missing_parameter_sets'``), a
            ``'tables'`` list (per table: ``'name'``, ``'ledger_rows'``,
            ``'live_rows'``, ``'snapshot_ids'``, ``'ok'``,
            ``'missing_snapshots'``) kept for the published CLI contract,
            ``'parameter_set_hashes'`` (every hash this execution's result
            batches landed under), a ``'discrepancies'`` list and
            ``'parameter_set_integrity'``.

        Raises:
            BerdlLoadRefusedError: Off-pod (before any transport call).
            ClearinghouseWriteTargetMismatchError: Namespace unconfirmed
                (dev 1206).
        """
        self._require_in_pod("verify_run")
        write_ns = self._assert_write_target(dataset=NAMESPACE, tenant=TENANT)
        shard_root = Path(shard_dir)
        state_dir = ci.execution_state_dir(run_id)
        binding = ci.read_execution(state_dir)
        ledger = ci.ExecutionLedger(state_dir)
        records = ledger.records()
        discrepancies: list[str] = []

        inventory = list(binding.get("inventory", []))
        if not inventory:
            discrepancies.append(
                f"execution {run_id!r}: no inventory at {state_dir} -- there "
                "is no record of which batches this execution was to load, so "
                "nothing about it can be proven. (A dry run writes no "
                "binding; a real run writes one before its first batch.)"
            )
        if binding and binding.get("namespace") not in (None, write_ns):
            discrepancies.append(
                f"execution {run_id!r} is bound to namespace "
                f"{binding.get('namespace')!r} but verify resolved "
                f"{write_ns!r}; the counts below are read from "
                f"{write_ns!r}."
            )
        if unit is not None and inventory:
            restricted = [
                entry for entry in inventory if entry.get("unit_id") == unit
            ]
            if not restricted:
                discrepancies.append(
                    f"execution {run_id!r}: no batch in its inventory belongs "
                    f"to unit {unit!r} (it holds "
                    f"{sorted({str(e.get('unit_id')) for e in inventory})}). "
                    "Nothing about that unit can be proven from this "
                    "execution, so this is reported rather than answered ok."
                )
            inventory = restricted

        batch_reports: list[dict[str, Any]] = []
        per_table_rows: dict[str, int] = {}
        per_table_snaps: dict[str, set[str]] = {}
        unit_hashes: dict[Any, set[str]] = {}
        unit_ok: dict[Any, bool] = {}

        for entry in inventory:
            batch_id = str(entry["batch_id"])
            table = str(entry["table"])
            unit_id = entry.get("unit_id")
            unit_ok.setdefault(unit_id, True)
            unit_hashes.setdefault(unit_id, set())
            record = records.get(batch_id)
            state = None if record is None else str(record.get("state"))
            ledger_rows = None if record is None else record.get("rows_ingested")
            ok = True
            if record is None:
                ok = False
                discrepancies.append(
                    f"{table} batch {batch_id}: no ledger line at all -- the "
                    "manifest names it but this execution never recorded "
                    "ingesting it."
                )
            elif state != ci.LEDGER_COMMITTED:
                ok = False
                discrepancies.append(
                    f"{table} batch {batch_id}: ledger state {state!r} is not "
                    f"{ci.LEDGER_COMMITTED!r}"
                    + (
                        f" ({record.get('error')})" if record.get("error") else ""
                    )
                    + " -- unresolved or failed batches block completion."
                )
            live_rows = self._count_batch_rows(table, batch_id)
            expected = ledger_rows if isinstance(ledger_rows, int) else None
            if expected is None:
                if state == ci.LEDGER_COMMITTED:
                    ok = False
                    discrepancies.append(
                        f"{table} batch {batch_id}: committed line records no "
                        "rows_ingested, so its live count cannot be checked."
                    )
            elif live_rows != expected:
                ok = False
                discrepancies.append(
                    f"{table} batch {batch_id}: {live_rows} live row(s) carry "
                    f"this ingest_batch_id but the ledger committed "
                    f"{expected}"
                    + (
                        " -- EXCESS rows, which means the batch was appended "
                        "more than once."
                        if live_rows > expected
                        else " -- rows the ledger says landed are missing."
                    )
                )
            if isinstance(expected, int):
                per_table_rows[table] = per_table_rows.get(table, 0) + expected
            per_table_snaps.setdefault(table, set())
            if record is not None and record.get("snapshot_id") is not None:
                per_table_snaps[table].add(str(record["snapshot_id"]))
            if not ok:
                unit_ok[unit_id] = False
            batch_reports.append(
                {
                    "batch_id": batch_id,
                    "name": table,
                    "unit_id": unit_id,
                    "state": state,
                    "ledger_rows": expected,
                    "live_rows": live_rows,
                    "ok": ok,
                }
            )
            if table.endswith("_result") and table != PARAMETER_SET_TABLE:
                try:
                    unit_hashes[unit_id] |= ci.batch_parameter_set_hashes(
                        entry, shard_root=shard_root
                    )
                except Exception as exc:  # the file may be gone; say so
                    unit_ok[unit_id] = False
                    discrepancies.append(
                        f"{table} batch {batch_id}: cannot read its "
                        f"parameter_set_hash values for the unit registry "
                        f"check ({type(exc).__name__}: {exc})."
                    )

        # THE UNIT REGISTRY CHECK: every hash a unit's results claim must be
        # in the registry, or those results cannot be interpreted.
        unit_reports: list[dict[str, Any]] = []
        for unit_id, hashes in unit_hashes.items():
            missing: list[str] = []
            if hashes:
                registered = self._registered_parameter_set_hashes(
                    self._validate_parameter_set_hashes(
                        sorted(hashes), verb="verify_run"
                    )
                )
                missing = sorted(hashes - registered)
            if missing:
                unit_ok[unit_id] = False
                discrepancies.append(
                    f"unit {unit_id!r}: {len(missing)} parameter_set_hash "
                    f"value(s) its result batches carry are absent from "
                    f"{PARAMETER_SET_TABLE}: {', '.join(missing[:3])}"
                    f"{', ...' if len(missing) > 3 else ''}. A result whose "
                    "parameter set the registry cannot explain is an orphan, "
                    "so this unit is not complete."
                )
            unit_reports.append(
                {
                    "unit_id": unit_id,
                    "ok": unit_ok.get(unit_id, True),
                    "missing_parameter_sets": missing,
                }
            )

        # The per-table roll-up, kept because the CLI's published output
        # contract renders it -- but it is now a SUM OF PROVEN BATCHES, not a
        # table-wide audit that any rows could satisfy.
        table_reports: list[dict[str, Any]] = []
        for table in sorted(set(per_table_rows) | set(per_table_snaps)):
            live_snaps = self._live_snapshot_ids(table)
            missing_snaps = sorted(per_table_snaps.get(table, set()) - live_snaps)
            if missing_snaps:
                discrepancies.append(
                    f"{table}: {len(missing_snaps)} snapshot id(s) the ledger "
                    f"recorded for execution {run_id!r} are absent from the "
                    f"table's Iceberg snapshot history: "
                    f"{', '.join(missing_snaps)}."
                )
            table_live = sum(
                report["live_rows"]
                for report in batch_reports
                if report["name"] == table and isinstance(report["live_rows"], int)
            )
            report = {
                "name": table,
                "ledger_rows": per_table_rows.get(table),
                "live_rows": table_live,
                # THE SNAPSHOT IDS THE ROWS LANDED UNDER, from the ledger.
                # The receipt carries them, and a receipt that named no
                # snapshot could not be checked against the table's own
                # Iceberg history -- which is the only thing that can say the
                # acknowledged write is still there.
                "snapshot_ids": sorted(per_table_snaps.get(table, set())),
                "ok": all(
                    r["ok"] for r in batch_reports if r["name"] == table
                )
                and not missing_snaps,
            }
            if missing_snaps:
                report["missing_snapshots"] = missing_snaps
            table_reports.append(report)

        parameter_set_integrity = self.parameter_set_orphans()
        discrepancies.extend(parameter_set_integrity["failures"])

        return {
            "run_id": run_id,
            "execution_id": run_id,
            "namespace": write_ns,
            "state_dir": str(state_dir),
            "unit": unit,
            "source_run_id": binding.get("source_run_id"),
            "ok": not discrepancies,
            "batches": batch_reports,
            "units": unit_reports,
            "tables": table_reports,
            # THE PARAMETER SET THE ROWS LANDED UNDER, read off the batch
            # files this execution loaded. The receipt carries it because
            # "un-run" is defined as the absence of a current-state row at a
            # PARTICULAR parameter_set_hash: a receipt that did not name the
            # hash could not tell the producer which partition it filled, and
            # the inert un-rendered hash the preset ships would be
            # indistinguishable from the run's real one.
            "parameter_set_hashes": sorted(
                {value for values in unit_hashes.values() for value in values}
            ),
            "discrepancies": discrepancies,
            "parameter_set_integrity": parameter_set_integrity,
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


def _registry_representative_sql(
    table: str, *, where: str = "", indent: int = 0
) -> str:
    """Build SQL selecting ONE representative registry row per hash.

    The parameter-set registry is append-only and a hash may carry several
    rows (a retry appends one). Every such row holds the SAME
    ``canonical_json`` but a different ``observed_at``/``ingest_batch_id``,
    so "one row per hash" cannot be expressed as ``SELECT DISTINCT`` over the
    four columns -- that collapses only byte-identical rows. It is a
    ``ROW_NUMBER()`` reduction instead, ordered the same way
    :func:`clearinghouse_derivation.current_state_sql` orders a result slot
    (latest ``observed_at``, ``ingest_batch_id`` as the tie-break), so the
    representative chosen is deterministic.

    Shared by :meth:`ClearinghouseCapability.parameter_sets` (where it IS the
    query) and by ``sources(by_parameter_set=True)`` (where it is the joined
    subquery, and where reducing to one row per hash is what stops a
    duplicate registry row from multiplying the counts). One builder, so the
    two cannot drift into disagreeing about what a hash means.

    Args:
        table: The registry's already-quoted fully-qualified name.
        where: Optional predicate text applied INSIDE the window (a
            pre-filter: fewer rows ranked, identical answer).
        indent: Spaces to indent the whole statement by, for embedding as a
            readable subquery.

    Returns:
        SQL text. Performs no I/O.
    """
    filter_clause = f"\n        WHERE {where}" if where else ""
    sql = (
        "SELECT parameter_set_hash, canonical_json, observed_at, ingest_batch_id\n"
        "FROM (\n"
        "    SELECT\n"
        "        parameter_set_hash,\n"
        "        canonical_json,\n"
        "        observed_at,\n"
        "        ingest_batch_id,\n"
        "        ROW_NUMBER() OVER (\n"
        "            PARTITION BY parameter_set_hash\n"
        "            ORDER BY observed_at DESC, ingest_batch_id DESC\n"
        "        ) AS rn\n"
        f"    FROM {table}{filter_clause}\n"
        ") AS ranked_registry\n"
        "WHERE rn = 1"
    )
    if indent:
        return textwrap.indent(sql, " " * indent)
    return sql


def _mishash_reason(digest: Any, text: Any) -> tuple[Any, str] | None:
    """Return ``(computed_hash, reason)`` if ``text`` does not hash to ``digest``.

    Returns ``None`` when the registry row is sound -- ``text`` parses, passes
    the parameter-set rule, and its
    :func:`~kbutillib.domains.identity.parameter_set_hash` equals ``digest``.

    The three ways a row can fail are reported separately rather than
    collapsed, because they point an operator at different causes: text that
    is not a string at all (a ``NULL`` canonical_json), text that is not
    parseable as a parameter set (corrupt, truncated, or never canonical),
    and text that parses perfectly but keys the wrong row (the dangerous
    one -- it looks entirely valid and silently misattributes every result
    under that hash).

    ``computed_hash`` is ``None`` for the first two cases: there is no value
    to compare, which is itself the finding.
    """
    if not isinstance(text, str):
        return None, (
            f"canonical_json is {text!r}, not text, so it cannot be parsed "
            "or hashed."
        )
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        return None, (
            f"canonical_json {text!r} is not parseable JSON ({exc}), so it "
            "cannot be hashed."
        )
    try:
        computed = parameter_set_hash(parsed)
    except ParameterSetError as exc:
        return None, (
            f"canonical_json {text!r} parses but is not a valid parameter "
            f"set ({exc}), so it cannot be hashed."
        )
    if computed != digest:
        return computed, (
            f"canonical_json {text!r} hashes to {computed}, not to its key "
            f"{digest!r}; every result keyed on {digest!r} is attributed to "
            "parameters that do not produce that key."
        )
    return None


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


def _shard_load_sort_key(table: str, batch: str) -> tuple[int, str, str]:
    """Sort key putting the parameter-set registry FIRST, then ``(table, batch)``.

    REGISTRY BEFORE RESULTS is a writer obligation, not a reader's problem: a
    ``<type>_result`` row whose ``parameter_set_hash`` has no registry row is
    an orphan -- the hash is opaque, so nothing can say what parameters
    produced that result -- and an ingest that dies between the two leaves
    exactly that. Loading :data:`PARAMETER_SET_TABLE` first makes the window
    empty in the only direction that matters: a registry row with no result
    row yet is inert and harmless.

    The registry is ordered ahead of EVERY other table, not merely ahead of
    the five ``<type>_result`` tables. That is strictly stronger than the
    requirement and costs nothing (it is one small shard), and it means the
    rule holds regardless of how the other fourteen tables sort against each
    other.
    """
    return (0 if table == PARAMETER_SET_TABLE else 1, table, batch)


def order_shards_for_load(
    discovered: Sequence[tuple[str, str, Path]],
) -> list[tuple[str, str, Path]]:
    """Order ``(table, batch, path)`` triples for ingest, registry first.

    THE single place a ``(table, batch, path)`` triple list is ordered for
    load, registry first, for any caller holding one. The ingest itself no
    longer discovers shards by scanning -- it reads the run inventory and
    orders it per UNIT via
    :func:`clearinghouse_ingest.derive_execution_inventory`, which imposes
    the same registry-first rule on the inventory's own publication order --
    so this is the ordering rule for callers working from paths rather than
    from an inventory.
    """
    return sorted(
        discovered, key=lambda triple: _shard_load_sort_key(triple[0], triple[1])
    )


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
