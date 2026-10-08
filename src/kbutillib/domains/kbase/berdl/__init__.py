"""BERDL (BER Data Lakehouse) capability subpackage.

This package holds the pure-logic building blocks used by the BERDL skills,
the two locus-specific transports, and the ``BerdlCapability`` deep module
described in ``agent-io/prds/berdl-lakehouse-skills/fullprompt.md``:

- :mod:`kbutillib.domains.kbase.berdl.naming` — dotted/underscored database
  name normalization and the ``my``/``{username}`` personal-catalog alias
  translation.
- :mod:`kbutillib.domains.kbase.berdl.membership` — ``ro``-suffix tenant
  membership decoding.
- :mod:`kbutillib.domains.kbase.berdl.tokens` — token resolution with fixed
  precedence (``KBASE_AUTH_TOKEN`` env var, then ``~/.kbase/token``, then
  config).
- :mod:`kbutillib.domains.kbase.berdl.transports` — :class:`InPodTransport`
  (wraps the pod-only ``berdl_notebook_utils`` package) and
  :class:`OffPodTransport` (pure-``requests`` REST client, read-only by
  construction) behind a common :class:`BerdlTransport` interface.
- :mod:`kbutillib.domains.kbase.berdl.capability` — :class:`BerdlCapability`,
  the deep module: locus detection, ``databases()``, ``memberships()``,
  ``load()`` (in-pod only, routed through ``data_lakehouse_ingest.ingest``),
  and ``query()``.
- :mod:`kbutillib.domains.kbase.berdl.clearinghouse_schema` — the
  FIFTEEN ``kbaseincubator.clearinghouse`` content-hash tables, named
  ``<entity_type>_<kind>`` (type-first) for each of the five entity types
  (``genome``, ``protein``, ``gene``, ``function``, ``ontology_term``) and
  each of the three kinds (``entity``, ``content``, ``result``), as pure
  config dicts for ``BerdlCapability.load``. :func:`table_name` is the ONLY
  sanctioned way to construct one of these names (it raises ``ValueError``
  on an unknown type or kind); nothing else may concatenate a table name.
  The ``entity`` and ``result`` kinds carry a GENERIC schema identical
  across all five types; the ``content`` kind is TYPE-SPECIALIZED (protein →
  ``sequence``/``seq_length``; gene → adds ``protein_entity_hash``; genome →
  assembly metadata plus a ``fasta_reference`` pointer; and so on). This
  module also provides the ``entity_hash`` normalisation helpers
  (:func:`encode_entity_hash`/:func:`decode_entity_hash`) that canonicalise
  :mod:`kbutillib.domains.identity.standardizers` hex digests to this
  schema's 64-character lowercase-hex STRING storage, and
  :func:`union_view_sql` for the per-kind ``all_*`` cross-type views. The
  hash is a hex ``STRING``, never ``BINARY`` (``data_lakehouse_ingest``
  rejects ``BINARY`` outright, dev 1219), and STRING equality is
  case-sensitive — an uppercase digest matches zero rows and fails open into
  recomputation — so every hash must pass through
  :func:`encode_entity_hash` and be bound as a query parameter, never
  formatted into SQL text. :func:`bootstrap` idempotently
  creates-or-verifies these tables in a namespace.
- :mod:`kbutillib.domains.kbase.berdl.clearinghouse_capability` —
  :class:`ClearinghouseCapability`, the locus-aware read/write API over the
  fifteen tables (composed over :class:`BerdlCapability`, never subclassing
  it). Read verbs (``stats``, ``known``, ``current_state``, ``content``,
  ``results``, ``sources``, ``tables``); write verbs (``register``,
  ``ingest_shards``, ``verify_run``) are in-pod only. Its named failures are
  :class:`ClearinghouseWriteTargetMismatchError` (the dev 1206 fail-closed
  pre-write guard — the existence-probe namespace must be shown to equal the
  ingest write-target namespace or ``load()`` is never called),
  :class:`ClearinghouseLoadPostflightError` (dev 1194 — a null postflight
  ``row_count`` is treated as a FAILED load), and
  :class:`ClearinghouseLedgerAmbiguousError` (a resume that finds a
  ``"started"`` batch with no terminal ledger line refuses rather than risk
  a duplicate ``<type>_content`` row).
- :mod:`kbutillib.domains.kbase.berdl.clearinghouse_derivation` — the
  SQL that derives "current state" from an append-only PER-TYPE
  ``<entity_type>_result`` table: a window function over the FIVE-tuple
  ``(entity_hash, entity_type, result_type, source, parameter_set_hash)``
  slot key, keeping the
  row with the greatest ``(observed_at, ingest_batch_id)`` in each slot
  (:func:`current_state_sql`). Because ``result_table_fqn`` names a
  single-typed table, the ``entity_types`` filter is redundant-but-harmless
  and is not passed. Pure: no session, no I/O, no pod-only imports.

``naming``, ``membership``, and ``tokens`` are pure logic: no network
calls, no BERDL pod dependency, and no imports of ``berdl_notebook_utils``.
They are safe to run in ordinary CI. ``transports`` and ``capability`` import
``berdl_notebook_utils``/``data_lakehouse_ingest`` only lazily -- never at
module scope -- so importing this package never requires the pod package to
be installed, even though ``InPodTransport`` cannot be *constructed*, and
``BerdlCapability.load()`` cannot succeed, off-pod. ``capability``'s pure
config-building and mode-selection helpers (:func:`~capability.build_ingest_config`,
:func:`~capability.select_write_mode`) are unit-tested the same way.
"""

from .capability import (
    BerdlCapability,
    BerdlLoadRefusedError,
    BerdlMembershipUnavailableError,
    build_ingest_config,
    select_write_mode,
)
from .clearinghouse_capability import (
    ClearinghouseCapability,
    ClearinghouseLedgerAmbiguousError,
    ClearinghouseLoadPostflightError,
    ClearinghousePartialWriteError,
    ClearinghouseWriteTargetMismatchError,
    order_shards_for_load,
)
from .clearinghouse_derivation import current_state_sql
from .clearinghouse_schema import NAMESPACE as CLEARINGHOUSE_NAMESPACE
from .clearinghouse_schema import TENANT as CLEARINGHOUSE_TENANT
from .clearinghouse_schema import (
    BootstrapIndeterminateStateError,
    BootstrapPartitionSpecMismatchError,
    bootstrap,
    decode_entity_hash,
    encode_entity_hash,
    table_name,
    union_view_sql,
)
from .clearinghouse_schema import table_configs as clearinghouse_table_configs
from .membership import decode_memberships
from .naming import (
    NormalizedDatabase,
    normalize_databases,
    to_spark_alias,
    to_trino_alias,
)
from .tokens import NoTokenAvailableError, require_token, resolve_token
from .transports import BerdlTransport, InPodTransport, OffPodTransport

__all__ = [
    "NormalizedDatabase",
    "normalize_databases",
    "to_spark_alias",
    "to_trino_alias",
    "decode_memberships",
    "resolve_token",
    "require_token",
    "NoTokenAvailableError",
    "BerdlTransport",
    "InPodTransport",
    "OffPodTransport",
    "BerdlCapability",
    "BerdlLoadRefusedError",
    "BerdlMembershipUnavailableError",
    "build_ingest_config",
    "select_write_mode",
    "CLEARINGHOUSE_TENANT",
    "CLEARINGHOUSE_NAMESPACE",
    "clearinghouse_table_configs",
    "table_name",
    "union_view_sql",
    "bootstrap",
    "BootstrapIndeterminateStateError",
    "BootstrapPartitionSpecMismatchError",
    "encode_entity_hash",
    "decode_entity_hash",
    "current_state_sql",
    "ClearinghouseCapability",
    "ClearinghouseWriteTargetMismatchError",
    "ClearinghouseLoadPostflightError",
    "ClearinghouseLedgerAmbiguousError",
    "ClearinghousePartialWriteError",
    "order_shards_for_load",
]
