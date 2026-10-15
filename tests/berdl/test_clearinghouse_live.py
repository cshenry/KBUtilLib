"""Env-gated LIVE on-pod test for the clearinghouse against real Iceberg-on-Polaris.

Everything else in ``tests/berdl/`` proves control flow and emitted SQL against
fakes (``test_clearinghouse_capability.py``) or derivation logic against a
DuckDB surrogate (``test_clearinghouse_derivation.py``). NOTHING off-pod proves
the real Iceberg-on-Polaris write/read path. This module is that proof, and it
is the ONLY test in the subpackage that touches a real lakehouse.

Opt-in
------
Collected everywhere; every test SKIPPED with a clear reason unless
``KBUTILLIB_LIVE_BERDL=1``. It only ever executes attended, on the kbhub BERDL
pod, mirroring the ``KBUTILLIB_LIVE_CHEM`` / ``KBUTILLIB_LIVE_RAST`` gates used
by ``tests/verab/test_verab_live.py`` and ``tests/external/test_rast_utils_live.py``::

    KBUTILLIB_LIVE_BERDL=1 pytest tests/berdl/test_clearinghouse_live.py -v

NEVER production
---------------
It exercises the REAL contract into a SCRATCH NAMESPACE and NEVER the production
``clearinghouse`` namespace. A wrong write against production risks a
multi-terabyte replay (see the dev 1206 / dev 1194 rationale in
``clearinghouse_capability.py``). Two independent guards keep this true:

1. The scratch namespace is a unique, per-run name (``clh_live_<8 hex>``) that
   cannot collide with ``clearinghouse``, and the module ``assert``\\s that it
   is not ``clearinghouse`` before doing anything.
2. The read verbs (``stats``/``known``/``current_state``) are hardwired to
   ``clearinghouse_capability._FQN_PREFIX`` (``kbaseincubator.clearinghouse``);
   the fixture monkeypatches that module global (and ``NAMESPACE``) to the
   scratch namespace for the lifetime of the test, so the reads land in
   scratch. Production is never named by any statement this module issues.
3. Every WRITE names its scratch dataset EXPLICITLY (``dataset=``). This is a
   third guard, and it used to be unnecessary because ``_ScratchAdapter``
   forced the write namespace itself. It no longer does (see its docstring),
   so the write target is derived by the REAL capability from the ``dataset``
   argument -- and since ``register``/``ingest_shards`` bind
   ``dataset: str = NAMESPACE`` at import time, a write that omitted it would
   go to PRODUCTION regardless of what this module monkeypatches.

What it covers (the PRD's minimum, plus the three flagged-most-likely-wrong)
--------------------------------------------------------------------------
- ``stats()`` returns row counts for all fifteen tables from an EXPLICIT
  ``COUNT(*)`` -- asserted against a KNOWN fixture cardinality that is NOT 5000
  and NOT a page size (flagged risk #1: off-pod ``row_count`` is the
  materialized page size and returns a plausible 5000 for every table).
- ``known()`` round-trips: register a synthetic entity, find it by hash, and
  confirm a fresh synthetic hash is absent.
- ``current_state()`` returns the latest row per slot after two registrations
  into the same slot.
- ``ingest_shards()`` of a tiny PUBLISHED run: the pre-write assertion and the
  lake-5 schema preflight pass against a correctly-resolved namespace, the
  batch reaches ``load()`` as a DataFrame, the execution ledger records a
  terminal ``"committed"`` line, ``verify_run`` proves the batch landed by its
  own ``ingest_batch_id`` count -- and a SECOND ingest of the same directory
  appends NOTHING, because fill-only finds every row present.
- A deliberately mismatched write target RAISES
  (:class:`ClearinghouseWriteTargetMismatchError`) and calls ``load()`` EXACTLY
  ZERO times (flagged risk #2, dev 1206: an implementation that writes and then
  raises passes a weaker "an exception was raised" test and is the exact
  failure).
- THE PRODUCTION BACKFILL PATH, WITH NOTHING INJECTED:
  ``kbu clearinghouse backfill run file-example --dataset <scratch>`` driven
  through click, so the capability is the one the CLI builds for itself. It
  must land rows; a SECOND run with a new run id into the same dataset must
  ingest ZERO rows in all SIXTEEN tables, the ``parameter_set`` registry
  included; and the namespace-wide registry orphan check must be empty after
  both. This is the test the ``_ScratchAdapter`` resolvers were standing in
  the way of: with them, the dev 1206 guard was satisfied by the test rather
  than by the real capability, so this module was green while every
  production ``kbu clearinghouse load`` refused.
- No in-lake ``INSERT ... SELECT`` / ``MERGE`` targets a clearinghouse table
  (flagged risk #3): a grep-style assertion over the module source. This one
  is NOT gated -- it is a pure static check and runs everywhere.
- ``_ScratchAdapter`` supplies NEITHER namespace resolver (also not gated: a
  ``vars()`` introspection check), which is the regression guard for the
  above.

Both scratch namespaces are torn down at the end -- and a teardown that
CANNOT drop one emits a ``UserWarning`` naming the namespace and the statement
to run by hand, rather than the ``except Exception: pass`` that let two scratch
namespaces leak silently on 2026-10-13.

Every SQL identifier this module interpolates goes through ``_q()`` (backticks,
one pair per dot-separated segment). Spark rejects ANSI double quotes outright,
and that defect has now been written here twice -- once at one site (dev 1534)
and once at six more; the single helper, pinned by an ungated test, is what
stops a third time.
"""

from __future__ import annotations

import ast
import json
import os
import re
import uuid
import warnings
from pathlib import Path
from typing import Any

import pytest

from kbutillib.domains.identity.standardizers import STANDARDIZER_VERSION
from kbutillib.domains.kbase.berdl import clearinghouse_capability as chc
from kbutillib.domains.kbase.berdl import clearinghouse_schema as chs
from kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter import (
    ClearinghouseBootstrapCapability,
)
from kbutillib.domains.kbase.berdl.clearinghouse_capability import (
    ClearinghouseCapability,
    ClearinghouseWriteTargetMismatchError,
)
from kbutillib.domains.kbase.berdl.clearinghouse_schema import (
    TENANT,
    encode_entity_hash,
    table_name,
)

# ---------------------------------------------------------------------------
# Gating -- evaluated at collection time, no pod imports
# ---------------------------------------------------------------------------

_LIVE_BERDL_ENABLED: bool = os.environ.get("KBUTILLIB_LIVE_BERDL") == "1"

_SKIP_REASON = (
    "Live BERDL clearinghouse test disabled -- set KBUTILLIB_LIVE_BERDL=1 to "
    "enable (executes attended ON the kbhub BERDL pod against a scratch "
    "namespace; requires an in-pod Spark session)"
)

#: Every LIVE test carries this gate. The static source-grep test below does NOT.
_need_live = pytest.mark.skipif(not _LIVE_BERDL_ENABLED, reason=_SKIP_REASON)

#: A KNOWN fixture cardinality per table that is intentionally NOT 5000 and NOT
#: any plausible page size, so a stats() that reported the materialized page
#: size (flagged risk #1) would visibly disagree.
_ENTITY_FIXTURE_ROWS = 3


def _hash(seed: int) -> str:
    """Return a deterministic, valid 64-char lowercase hex digest."""
    return f"{seed:064x}"



# ---------------------------------------------------------------------------
# SQL identifier quoting -- the ONE place this module quotes an identifier
# ---------------------------------------------------------------------------


def _q(*parts: str) -> str:
    """Backtick-quote every identifier segment in ``parts``, joined with dots.

    THE ONLY place this module quotes a SQL identifier, and every site that
    interpolates one goes through it (pinned by
    :func:`test_module_never_double_quotes_a_sql_identifier`).

    **Why this exists rather than an inline f-string.** Spark's parser rejects
    ANSI double quotes around an identifier outright -- it reads ``"x"`` as a
    string literal -- so ``'"kbaseincubator"."clh_live_1234"'`` raises
    ``[PARSE_SYNTAX_ERROR] Syntax error at or near '"kbaseincubator"'``. That
    exact defect was reported once (dev 1534, from the 2026-10-07 kbhub run),
    fixed at the one site it was found at, and then written again at six more
    sites in the fixtures added afterwards -- it blocked all five lakehouse
    tests on the 2026-10-13 in-pod run. Routing every identifier through one
    helper is what stops a seventh recurrence.

    **One quote pair per dot-separated segment**, mirroring
    ``clearinghouse_bootstrap_adapter._quote_fqn`` and
    ``capability._quote_fqn``: a dotted namespace quoted as a SINGLE identifier
    (```` `kbaseincubator.clh_live_1234`.`gene_entity` ````) is a two-part name
    whose first part literally contains a dot, which Spark does not resolve --
    measured in-pod raising ``TABLE_OR_VIEW_NOT_FOUND`` while both the
    unquoted three-part name and the per-segment form returned rows
    (``agent-io/prds/berdl-smoke-verification/in-pod-results.md``, defect D2).
    So a caller may hand over a dotted namespace and a bare table name in
    either shape -- ``_q(TENANT, ns, name)`` and ``_q(f"{TENANT}.{ns}", name)``
    produce the identical string.

    Splitting on dots means this cannot express an identifier that CONTAINS a
    dot. Clearinghouse table names never do (``table_name()`` builds
    ``<entity_type>_<kind>``) and neither do the scratch namespace names this
    module generates, so the restriction costs nothing here.

    Args:
        *parts: Identifier segments, each optionally dot-separated. Empty
            segments are dropped.

    Returns:
        The backtick-quoted, dot-joined identifier.

    Raises:
        ValueError: A part contains a backtick (it would close the quoting and
            let the rest of the part be parsed as SQL), or no non-empty segment
            was given at all. Refusing beats emitting a statement whose shape
            the caller did not intend.
    """
    segments: list[str] = []
    for part in parts:
        text = str(part)
        if "`" in text:
            raise ValueError(
                f"_q: refusing to quote an identifier containing a backtick: "
                f"{part!r}"
            )
        segments.extend(segment for segment in text.split(".") if segment)
    if not segments:
        raise ValueError(
            f"_q: at least one non-empty identifier segment is required; got {parts!r}"
        )
    return ".".join(f"`{segment}`" for segment in segments)


# ---------------------------------------------------------------------------
# Discriminating a genuine "not found" from every other failure
# ---------------------------------------------------------------------------

#: Spark error conditions that mean the relation or namespace is GENUINELY
#: absent -- the only family :meth:`_ScratchAdapter.table_exists` may report as
#: ``False``. ``TABLE_OR_VIEW_NOT_FOUND`` is the condition this cluster was
#: MEASURED emitting for a table it could not resolve -- recorded in
#: ``agent-io/prds/berdl-smoke-verification/in-pod-results.md`` (defect D2) and
#: cited in ``capability._quote_fqn``'s docstring. The namespace-level
#: spellings are listed beside it because Spark has used several across
#: versions and an absent namespace is equally "not found" for this probe.
_MISSING_RELATION_CONDITIONS = frozenset(
    {
        "TABLE_OR_VIEW_NOT_FOUND",
        "TABLE_OR_VIEW_NOT_FOUND_WITH_CANDIDATES",
        "SCHEMA_NOT_FOUND",
        "NAMESPACE_NOT_FOUND",
        "DATABASE_NOT_FOUND",
    }
)

#: Exception CLASS names that mean the same thing, for a client that raises a
#: typed error with no Spark condition attached (the Iceberg/pyiceberg and
#: py4j-wrapped Hive metastore spellings).
_MISSING_RELATION_TYPES = frozenset(
    {
        "NoSuchTableError",
        "NoSuchTableException",
        "NoSuchNamespaceError",
        "NoSuchNamespaceException",
        "NoSuchDatabaseException",
        "TableNotFoundError",
    }
)

#: Last-resort message match, for a pre-error-class Spark or a client that
#: flattens the error to text. Deliberately narrow: it requires the relation
#: KIND beside the phrase, so an unrelated failure that happens to contain
#: "does not exist" somewhere in a stack trace is not read as "absent".
_MISSING_RELATION_TEXT = re.compile(
    r"(?i)(?:table or view not found"
    r"|\b(?:table|view|database|namespace|schema)\b[^\n]{0,160}?\bdoes not exist)"
)


def _spark_error_condition(exc: BaseException) -> str | None:
    """Return the Spark error CONDITION name for ``exc``, or ``None``.

    pyspark 3.4+ gives every ``AnalysisException``-family error a
    machine-readable condition name and renders it as a ``[NAME]`` prefix on
    ``str(exc)`` -- which is how the 2026-10-13 failure was identifiable as
    ``[PARSE_SYNTAX_ERROR]`` rather than a missing table. The accessor is
    ``getCondition()`` on pyspark 4.x and ``getErrorClass()`` on 3.4/3.5, so
    both are tried, then the rendered prefix (the only thing available when the
    error arrives from a Spark Connect client that exposes neither).

    This deliberately NEVER imports pyspark and never matches on a pyspark
    class: pyspark is not importable off the pod, so an import here would make
    the ungated tests uncollectable in ordinary CI.
    """
    for accessor in ("getCondition", "getErrorClass"):
        getter = getattr(exc, accessor, None)
        if callable(getter):
            try:
                condition = getter()
            except Exception:  # noqa: BLE001 - a broken accessor must not mask exc
                condition = None
            if condition:
                return str(condition)
    match = re.match(r"\s*\[([A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*)\]", str(exc))
    return match.group(1) if match else None


def _is_missing_relation_error(exc: BaseException) -> bool:
    """Whether ``exc`` means "that table or namespace is not there".

    ``True`` ONLY for a genuine absence. A parse error, an auth failure or a
    dropped connection returns ``False`` so the caller re-raises it: a probe
    that reports "absent" for a lookup that actually FAILED is the confident
    wrong answer that hid the quoting defect for a week -- all sixteen
    ``table_exists`` probes returned ``False`` on 2026-10-13 because the
    statement would not parse, and ``bootstrap()`` read that as "create them
    all".
    """
    condition = _spark_error_condition(exc)
    if condition is not None:
        # A condition was published, so it is authoritative -- do NOT fall
        # through to the text match, which could re-admit a parse error whose
        # message happens to quote a missing-table phrase.
        return condition.split(".")[0] in _MISSING_RELATION_CONDITIONS
    if type(exc).__name__ in _MISSING_RELATION_TYPES:
        return True
    return bool(_MISSING_RELATION_TEXT.search(str(exc)))


def _drop_or_warn(real: Any, statement: str, *, what: str) -> None:
    """Run one teardown ``DROP``, tolerating ONLY a genuine not-found.

    Teardown used to be ``except Exception: pass``, so a scratch namespace that
    could not be dropped left NO trace -- two leaked on 2026-10-13 while the
    module docstring claimed both were torn down. Anything that is not an
    absence is surfaced as a :class:`UserWarning` naming the namespace and the
    exact statement to run by hand, so a leak is visible in the pytest output
    of the run that caused it.

    It warns rather than raises on purpose: this runs in a finalizer, and
    raising there would replace a real test failure with a teardown error.
    """
    try:
        real.query(statement, engine="spark")
    except Exception as exc:  # noqa: BLE001 - re-reported as a warning below
        if _is_missing_relation_error(exc):
            return
        warnings.warn(
            f"clearinghouse live teardown could NOT drop {what} "
            f"({type(exc).__name__}: {exc}). It may have LEAKED and must be "
            f"dropped by hand on the pod: {statement}",
            stacklevel=2,
        )


def _is_schema_only_load(kwargs: dict[str, Any]) -> bool:
    """Whether a ``load()`` call carries NO data source, as ``bootstrap()`` does.

    The same condition ``ClearinghouseBootstrapCapability.load`` applies to
    choose its own branch: no ``dataframes``, and no ``paths`` carrying a
    ``'bronze_base'``. ``bootstrap()`` can only ever call in this shape -- it
    takes neither parameter, so it has no data source to offer.
    """
    paths = kwargs.get("paths") or {}
    return not kwargs.get("dataframes") and not paths.get("bronze_base")


# ---------------------------------------------------------------------------
# Scratch-namespace adapter over the REAL BerdlCapability
# ---------------------------------------------------------------------------


class _CountingLoad:
    """Wraps a real ``load`` callable and counts invocations.

    The dev 1206 assertion (flagged risk #2) requires proving ``load()`` was
    called EXACTLY ZERO times on the refusal path -- not merely that an
    exception was raised. This counter is what lets the test assert the count.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def __call__(self, **kwargs: Any) -> Any:
        self.calls += 1
        return self._inner(**kwargs)


class _ScratchAdapter:
    """Adapts a real ``BerdlCapability`` for the scratch BOOTSTRAP only.

    IT NO LONGER SUPPLIES ``resolve_write_namespace`` OR
    ``resolve_probe_namespace``, and that removal is the point of this class
    now. While it did supply them, every write this module made landed in
    scratch *because the test adapter said so* -- which is exactly why this
    module could pass green while `kbu clearinghouse load` refused on every
    production call with ``ClearinghouseWriteTargetMismatchError``: the thing
    under test was never the thing production uses. The real
    ``BerdlCapability`` now implements both resolvers (both returning its own
    ``_write_target_namespace(dataset, tenant)``), so the dev 1206 guard is
    satisfied by the PRODUCTION derivation, and ``__getattr__`` below hands
    every unlisted attribute -- the two resolvers included -- straight to it.

    THE CONSEQUENCE FOR CALLERS IS LOAD-BEARING: the write target is now
    derived from the ``dataset`` argument, so every write verb in this module
    MUST pass ``dataset=<scratch namespace>`` explicitly. A call that relies on
    the default would resolve to ``kbaseincubator.clearinghouse`` -- PRODUCTION
    -- because ``register``/``ingest_shards`` bind ``dataset: str = NAMESPACE``
    at import time and no monkeypatch of ``chs.NAMESPACE`` can reach a default
    that is already bound.

    What it still supplies, and why:

    - ``table_exists`` / ``table_partition_spec`` -- read-only Iceberg metadata
      probes that ``clearinghouse_schema.bootstrap()`` requires and the real
      ``BerdlCapability`` does NOT implement. They are answered on THIS
      fixture's own Spark session (via ``real.query``), which is why they are
      not simply delegated to ``ClearinghouseBootstrapCapability``'s versions:
      that class resolves a session of its own. The ``live_clearinghouse`` and
      ``bf_live_scratch`` fixtures both call ``bootstrap()`` to create the
      sixteen scratch tables, so without these two the scratch namespace
      cannot be built at all and nothing in this module can run. They are
      SETUP, not the contract under test.
    - ``load`` wrapped in a :class:`_CountingLoad` over
      :meth:`_dispatch_load`, so (a) a SCHEMA-ONLY load -- the only shape
      ``bootstrap()`` can issue -- reaches
      ``ClearinghouseBootstrapCapability.load``, which is the shipped
      component that turns it into ``CREATE TABLE`` DDL, while a data-bearing
      load still goes to the real capability UNCHANGED; and (b) every load in
      either mode is counted, so a test can assert the zero-call refusal
      contract.
    """

    def __init__(self, real: Any, scratch_ns: str, *, spark: Any = None) -> None:
        """Wrap ``real`` for the scratch namespace ``scratch_ns``.

        Args:
            real: The real ``BerdlCapability``.
            scratch_ns: The scratch namespace in its DOTTED, tenant-prefixed
                form (``kbaseincubator.clh_live_1234``). That is the only form
                that addresses a namespace on this cluster -- measured in-pod
                2026-09-12, runbook question (c): ``table_exists(t,
                namespace='kbaseincubator.genome_clearhouse')`` returned
                ``True`` while the bare ``'genome_clearhouse'`` returned
                ``False``. It is also exactly ``BerdlCapability``'s own write
                target, ``f"{tenant}.{dataset}"``, so bootstrap creates the
                tables in the namespace the writes land in.
            spark: Forwarded to ``ClearinghouseBootstrapCapability``. Leave at
                ``None`` on the pod -- it then resolves an in-pod session
                lazily, exactly as the production bootstrap does. The ungated
                unit tests pass a fake session, which is the only way to
                exercise the DDL path where no Spark exists.
        """
        self._real = real
        self._scratch_ns = scratch_ns
        # The component that makes a SCHEMA-ONLY load work. See _dispatch_load.
        self._bootstrap = ClearinghouseBootstrapCapability(real, spark=spark)
        self.load = _CountingLoad(self._dispatch_load)

    def _dispatch_load(self, **kwargs: Any) -> Any:
        """Route a schema-only load to DDL and a data-bearing load to the lake.

        ``bootstrap()`` calls ``load()`` with NO data source, and
        ``BerdlCapability.load`` cannot serve that shape: it always drives
        ``data_lakehouse_ingest.ingest()``, whose ``build_ingest_config`` raises
        ``ValueError: build_ingest_config: bronze mode (no 'dataframes' given)
        requires 'paths' with a 'bronze_base'``
        (``capability.py``). Wrapping the RAW ``real.load`` is why bootstrap
        could not run at all on 2026-10-13 once the quoting was hand-patched.

        :class:`~kbutillib.domains.kbase.berdl.clearinghouse_bootstrap_adapter.ClearinghouseBootstrapCapability`
        is the shipped component that owns that translation -- a schema-only
        load becomes one ``CREATE TABLE IF NOT EXISTS ... USING iceberg
        [PARTITIONED BY (...)]`` per table, built from the table config's own
        ``schema_sql`` so every column type and the partition spec are DECLARED
        rather than inferred. That path is PROVEN on this pod: it created the
        production clearinghouse tables on 2026-10-08 (OP-C1 C1.3) and the
        ``clearinghouse_parity`` tables (OP-C2 C2.2).

        A DATA-BEARING load is handed to ``real.load`` UNCHANGED, and that is
        deliberate: this module exists to prove the REAL write path, so the
        production ingest must not be routed through a test-side wrapper (the
        bootstrap adapter would forward it, but it would also inject its own
        ``spark=`` session, which production does not). The only thing wrapping
        a real write here is the call COUNTER.

        Both branches are inside the one :class:`_CountingLoad`, so every load
        the code under test makes is counted whatever its mode -- which is what
        keeps ``test_live_mismatched_write_target_raises_and_never_loads``'s
        "exactly ZERO loads" assertion meaningful.
        """
        if _is_schema_only_load(kwargs):
            return self._bootstrap.load(**kwargs)
        return self._real.load(**kwargs)

    # -- delegated to the real capability --------------------------------

    def locus(self) -> str:
        return self._real.locus()

    def query(self, sql: str, **kwargs: Any) -> Any:
        return self._real.query(sql, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Hand anything not defined here to the REAL capability.

        This is what makes the namespace resolvers production's rather than
        the test's: ``ClearinghouseCapability._assert_write_target`` does
        ``getattr(capability, "resolve_write_namespace", None)``, and that
        lookup now reaches ``BerdlCapability``'s implementation.
        """
        if name.startswith("_"):
            # Never delegate a private name -- in particular ``_real`` itself,
            # whose absence would recurse here forever.
            raise AttributeError(name)
        return getattr(self._real, name)

    # -- read-only metadata probes bootstrap() requires ------------------

    def table_exists(self, name: str, namespace: str | None = None) -> bool:
        """Probe whether ``name`` exists, on THIS fixture's Spark session.

        ``False`` is returned ONLY for a genuine table-or-namespace-not-found
        (see :func:`_is_missing_relation_error`). Every other exception --
        parse error, auth, connection -- is RE-RAISED, and ``bootstrap()``
        turns it into ``BootstrapIndeterminateStateError`` with nothing
        written, which is the correct outcome for a lookup that did not
        actually answer.

        This used to be a bare ``except Exception: return False``, and that is
        what made the quoting defect invisible: the ANSI-double-quoted
        statement would not parse, so all sixteen probes answered "absent" --
        a confident wrong answer -- and ``bootstrap()`` proceeded to create
        every table on that basis.
        """
        ns = namespace or self._scratch_ns
        try:
            self._real.query(
                f"SELECT 1 FROM {_q(ns, name)} LIMIT 0", engine="spark"
            )
        except Exception as exc:
            if _is_missing_relation_error(exc):
                return False
            raise
        return True

    def table_partition_spec(
        self, name: str, namespace: str | None = None
    ) -> list[str] | None:
        # Freshly-created scratch tables carry whatever partition_by the config
        # declares; bootstrap only consults this for a table it already found,
        # and our create path uses the same config, so reporting the declared
        # spec keeps verify aligned with create.
        from kbutillib.domains.kbase.berdl.clearinghouse_schema import table_configs

        for cfg in table_configs():
            if cfg["name"] == name:
                pb = cfg.get("partition_by")
                if not pb:
                    return None
                return [pb] if isinstance(pb, str) else list(pb)
        return None


# ---------------------------------------------------------------------------
# Fixtures -- build, wire and TEAR DOWN the scratch namespace
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scratch_namespace() -> str:
    """A unique per-run scratch namespace name that is provably not production."""
    ns = f"clh_live_{uuid.uuid4().hex[:8]}"
    assert ns != chs.NAMESPACE, "scratch namespace must never be the production one"
    assert ns != "clearinghouse"
    return ns


@pytest.fixture(scope="module")
def live_clearinghouse(scratch_namespace, request):
    """A ``ClearinghouseCapability`` wired to a freshly-bootstrapped scratch NS.

    Monkeypatches the module-global FQN prefix so the read verbs
    (``stats``/``known``/``current_state``) read the SCRATCH tables rather than
    production ``clearinghouse``, bootstraps the fifteen scratch tables, yields
    the capability, then DROPs every scratch table and the namespace.
    """
    from kbutillib.domains.kbase.berdl.capability import BerdlCapability
    from kbutillib.domains.kbase.berdl.clearinghouse_schema import bootstrap

    real = BerdlCapability()
    if real.locus() != "in_pod":
        pytest.skip(
            "not running inside the BERDL pod (BerdlCapability.locus() != "
            "'in_pod'); the live write path needs an in-pod Spark session"
        )

    scratch_prefix = f"{TENANT}.{scratch_namespace}"

    # Point BOTH the reads (hardwired to _FQN_PREFIX) and the schema NAMESPACE
    # at the scratch namespace for the lifetime of the test. Restored on teardown
    # by monkeypatch.
    mp = pytest.MonkeyPatch()
    mp.setattr(chc, "_FQN_PREFIX", scratch_prefix, raising=True)
    mp.setattr(chs, "NAMESPACE", scratch_namespace, raising=True)

    # The DOTTED, tenant-prefixed namespace -- the only form that addresses a
    # namespace on this cluster (runbook (c), measured in-pod 2026-09-12), and
    # identical to BerdlCapability's own write target f"{tenant}.{dataset}",
    # so bootstrap's DDL and the writes under test name the same namespace.
    scratch_ns_fqn = f"{TENANT}.{scratch_namespace}"
    adapter = _ScratchAdapter(real, scratch_ns_fqn)

    # Create the scratch namespace and the sixteen tables.
    real.query(
        f"CREATE NAMESPACE IF NOT EXISTS {_q(TENANT, scratch_namespace)}",
        engine="spark",
    )
    bootstrap(
        adapter,
        namespace=scratch_ns_fqn,
        dataset=scratch_namespace,
        tenant=TENANT,
    )

    cap = ClearinghouseCapability(capability=adapter)

    def _teardown() -> None:
        # ITERATE table_configs(), NOT ENTITY_TYPES x KINDS: the
        # `parameter_set` registry is a sixteenth, unpartitioned table that
        # table_name() does not produce, so the cross-product loop this used
        # to be left it behind -- and a leftover table makes the DROP
        # NAMESPACE below fail, which is how a scratch namespace survives a
        # test run.
        for cfg in chs.table_configs():
            _drop_or_warn(
                real,
                f"DROP TABLE IF EXISTS {_q(TENANT, scratch_namespace, cfg['name'])}",
                what=f"table {scratch_ns_fqn}.{cfg['name']}",
            )
        _drop_or_warn(
            real,
            f"DROP NAMESPACE IF EXISTS {_q(TENANT, scratch_namespace)}",
            what=f"namespace {scratch_ns_fqn}",
        )
        mp.undo()

    request.addfinalizer(_teardown)
    return cap


def _entity_row(seed: int, etype: str, *, batch: str) -> dict[str, Any]:
    """A synthetic ``<type>_entity`` row with a valid, encoded hex hash."""
    return {
        "entity_hash": encode_entity_hash(_hash(seed)),
        "entity_type": etype,
        "standardizer_version": STANDARDIZER_VERSION,
        "observed_at": "2026-01-01T00:00:00",
        "ingest_batch_id": batch,
    }


def _result_row(
    seed: int, etype: str, *, source: str, batch: str, observed_at: str
) -> dict[str, Any]:
    """A synthetic ``<type>_result`` row for one slot."""
    return {
        "entity_hash": encode_entity_hash(_hash(seed)),
        "entity_type": etype,
        "result_type": "annotation",
        "source": source,
        "result_type_version": "v1",
        # REQUIRED on every result row since lake-5: register() refuses a
        # result row with no parameter_set rather than defaulting it, and
        # stores the HASH of this value in parameter_set_hash.
        "parameter_set": {},
        "payload": json.dumps({"seed": seed, "batch": batch}),
        "observed_at": observed_at,
        "ingest_batch_id": batch,
    }


# ---------------------------------------------------------------------------
# LIVE tests
# ---------------------------------------------------------------------------


@_need_live
@pytest.mark.integration
def test_live_stats_counts_all_fifteen_from_explicit_count(
    live_clearinghouse, scratch_namespace
):
    """stats() returns COUNT(*)-derived row counts for all fifteen tables.

    Registers a KNOWN number of protein_entity rows (``_ENTITY_FIXTURE_ROWS``,
    intentionally not 5000 / a page size) and asserts stats() reports exactly
    that for protein_entity -- so a stats() built on a transport ``row_count``
    field (which off-pod is the page size, ~5000) would visibly fail here
    (flagged risk #1).
    """
    rows = [
        _entity_row(1000 + i, "protein", batch="stats-fixture")
        for i in range(_ENTITY_FIXTURE_ROWS)
    ]
    # dataset= IS REQUIRED NOW. _ScratchAdapter no longer forces the write
    # namespace, so the REAL resolver derives it from this argument; the
    # default would be production (see _ScratchAdapter's docstring).
    live_clearinghouse.register(
        "protein", rows, kind="entity", dataset=scratch_namespace
    )

    stats = live_clearinghouse.stats()
    by_name = {t["name"]: t for t in stats["tables"]}

    assert len(by_name) == 15, f"expected 15 tables, got {sorted(by_name)}"
    assert by_name["protein_entity"]["row_count"] == _ENTITY_FIXTURE_ROWS, (
        "stats() must report the EXPLICIT COUNT(*), not a page size; got "
        f"{by_name['protein_entity']['row_count']!r}"
    )
    assert by_name["protein_entity"]["row_count"] != 5000


@_need_live
@pytest.mark.integration
def test_live_known_round_trips(live_clearinghouse, scratch_namespace):
    """known() finds a registered hash and reports a fresh hash as absent."""
    present_hash = _hash(2001)
    absent_hash = _hash(999999)

    live_clearinghouse.register(
        "gene",
        [_entity_row(2001, "gene", batch="known-fixture")],
        kind="entity",
        dataset=scratch_namespace,
    )

    found = live_clearinghouse.known("gene", [present_hash])
    assert encode_entity_hash(present_hash) in found

    not_found = live_clearinghouse.known("gene", [absent_hash])
    assert encode_entity_hash(absent_hash) not in not_found


@_need_live
@pytest.mark.integration
def test_live_current_state_latest_per_slot(live_clearinghouse, scratch_namespace):
    """current_state() returns the LATEST row per slot after two registrations.

    Two registrations into the SAME (entity_hash, entity_type, result_type,
    source) slot with different observed_at; current_state must collapse to the
    later one.
    """
    seed = 3003
    live_clearinghouse.register(
        "function",
        [
            _result_row(
                seed,
                "function",
                source="src-A",
                batch="cs-batch-1",
                observed_at="2026-01-01T00:00:00",
            )
        ],
        kind="result",
        dataset=scratch_namespace,
    )
    live_clearinghouse.register(
        "function",
        [
            _result_row(
                seed,
                "function",
                source="src-A",
                batch="cs-batch-2",
                observed_at="2026-02-01T00:00:00",
            )
        ],
        kind="result",
        dataset=scratch_namespace,
    )

    state = live_clearinghouse.current_state("function")
    slot_rows = [
        r
        for r in state
        if r.get("entity_hash") == encode_entity_hash(_hash(seed))
        and r.get("source") == "src-A"
        and r.get("result_type") == "annotation"
    ]
    assert len(slot_rows) == 1, f"expected one current row per slot, got {slot_rows}"
    assert slot_rows[0].get("ingest_batch_id") == "cs-batch-2", (
        "current_state must keep the LATEST row in the slot; got "
        f"{slot_rows[0].get('ingest_batch_id')!r}"
    )


@_need_live
@pytest.mark.integration
def test_live_ingest_shards_tiny_directory(
    live_clearinghouse, scratch_namespace, tmp_path
):
    """ingest_shards() of a tiny PUBLISHED run: fill-only, and provably landed.

    The pre-write assertion and the lake-5 schema preflight must pass against
    the correctly-resolved scratch namespace; the batch must reach ``load()``
    as a DataFrame (the shards are on the pod's local disk, which Spark
    executors cannot read); the ledger must record a terminal ``committed``
    line under the EXECUTION state dir; and a SECOND ingest of the same
    directory must append nothing, because every row is now present.
    """
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci  # noqa: PLC0415
    from kbutillib.domains.kbase.berdl.clearinghouse_shard import (  # noqa: PLC0415
        RUN_MANIFEST_VERSION,
        new_ulid,
    )

    # The ledger and the lock must not touch this developer's real state.
    os.environ[ci.STATE_DIR_ENV] = str(tmp_path / "state")
    os.environ[ci.LOCK_DIR_ENV] = str(tmp_path / "locks")

    shard_root = tmp_path / "shards"
    table = table_name("ontology_term", "entity")
    (shard_root / table).mkdir(parents=True)
    batch_id = new_ulid()
    rows = [
        _entity_row(4000 + i, "ontology_term", batch=batch_id) for i in range(2)
    ]
    shard_path = shard_root / table / f"{batch_id}.parquet"
    pq.write_table(pa.Table.from_pylist(rows), shard_path)
    hashes = sorted(row["entity_hash"] for row in rows)
    (shard_root / "run.json").write_text(
        json.dumps(
            {
                "run_manifest_version": RUN_MANIFEST_VERSION,
                "run_id": new_ulid(),
                "units": [{"unit_id": "u1", "attempt_id": "A", "slice": None}],
                "batches": [
                    {
                        "table": table,
                        "batch_id": batch_id,
                        "file": f"{table}/{shard_path.name}",
                        "rows": len(rows),
                        "key_column": "entity_hash",
                        "hash_min": hashes[0],
                        "hash_max": hashes[-1],
                        "sha256": ci.file_sha256(shard_path),
                        "source": ["live-test"],
                        "slice": None,
                        "unit_id": "u1",
                        "attempt_id": "A",
                    }
                ],
                "skipped": [],
                "sources": [],
            }
        )
    )

    result = live_clearinghouse.ingest_shards(
        shard_root, tenant=TENANT, dataset=scratch_namespace
    )

    assert result["namespace"], "ingest resolved an empty namespace"
    ingested = [t for t in result["tables"] if t["action"] == "ingest"]
    assert ingested, f"nothing was ingested: {result['tables']}"
    assert all(t["rows_ingested"] == len(rows) for t in ingested)

    records = ci.ExecutionLedger(Path(result["state_dir"])).records()
    assert records, f"no ledger under {result['state_dir']}"
    assert all(r["state"] == ci.LEDGER_COMMITTED for r in records.values())

    verified = live_clearinghouse.verify_run(shard_root, run_id=result["run_id"])
    assert verified["ok"], verified["discrepancies"]

    # FILL-ONLY: a second load of the same directory writes nothing.
    again = live_clearinghouse.ingest_shards(
        shard_root, tenant=TENANT, dataset=scratch_namespace
    )
    assert all(t["rows_ingested"] == 0 for t in again["tables"]), again["tables"]


@_need_live
@pytest.mark.integration
def test_live_mismatched_write_target_raises_and_never_loads(
    scratch_namespace, request
):
    """A mismatched probe/write namespace RAISES and calls load() ZERO times.

    Flagged risk #2 (dev 1206): the refusal must happen BEFORE any write. An
    implementation that writes and then raises would pass a weaker "an
    exception was raised" test -- so this asserts the load call count is
    exactly zero, which is the property that actually matters.
    """
    from kbutillib.domains.kbase.berdl.capability import BerdlCapability

    real = BerdlCapability()
    if real.locus() != "in_pod":
        pytest.skip("not running inside the BERDL pod; write path needs Spark")

    adapter = _ScratchAdapter(real, f"{TENANT}.{scratch_namespace}")

    # Force the guard to fail: make the probe namespace disagree with the write
    # namespace, exactly the dev 1206 shape.
    adapter.resolve_probe_namespace = (  # type: ignore[assignment]
        lambda *, dataset, tenant: "default"
    )

    cap = ClearinghouseCapability(capability=adapter)

    with pytest.raises(ClearinghouseWriteTargetMismatchError):
        cap.register(
            "protein",
            [_entity_row(5000, "protein", batch="mismatch")],
            kind="entity",
            dataset=scratch_namespace,
        )
    assert adapter.load.calls == 0, (
        "dev 1206: load() must be called ZERO times on the refusal path; "
        f"it was called {adapter.load.calls} times"
    )


# ---------------------------------------------------------------------------
# LIVE: the PRODUCTION backfill path, with NOTHING injected
# ---------------------------------------------------------------------------
#
# The tests above wire a ``ClearinghouseCapability`` by hand. This one does
# not: it runs the shipped `kbu clearinghouse backfill run file-example` verb
# through click, and that verb builds its own capability
# (``cli.clearinghouse._capability()`` -> ``ClearinghouseCapability()``, no
# argument), so the whole chain under test -- locus refusal, namespace
# resolution, the dev 1206 guard, the lake-5 schema preflight, fill-only
# ingest, per-batch verification, the unit record and the namespace-wide
# registry orphan check -- is the chain an operator gets. Nothing in this test
# supplies a resolver, a probe or a namespace to the write path.
#
# It uses `file-example` deliberately: it is the one preset that reads a
# parquet file shipped as package data, so the test needs no Mongo, no arc
# store, no KBDL and no fixture to fetch -- only the pod.


@pytest.fixture(scope="module")
def bf_scratch_dataset() -> str:
    """A second scratch dataset, for the backfill run. Provably not production."""
    dataset = f"clh_bf_live_{uuid.uuid4().hex[:8]}"
    assert dataset != chs.NAMESPACE, "scratch dataset must never be the production one"
    assert dataset != "clearinghouse"
    return dataset


@pytest.fixture(scope="module")
def bf_live_scratch(bf_scratch_dataset, tmp_path_factory, request):
    """Bootstrap the sixteen tables of a scratch dataset, then DROP it.

    Same shape and same teardown as :func:`live_clearinghouse` -- it creates
    ``kbaseincubator.<scratch>``, bootstraps every table in
    ``table_configs()`` and drops them all plus the namespace on the way out --
    with two differences:

    - it yields the SETUP pieces only (the real ``BerdlCapability``, the
      dataset name, a scratch root), never a wired capability, because the
      capability under test is the one the CLI builds for itself;
    - the ledger and the ingest LOCK are redirected under ``tmp_path`` so this
      test cannot touch the operator's real ``~/.kbu/clearinghouse/runs`` or
      the shared ``/global_share`` lock directory.

    ``_ScratchAdapter`` appears here for ONE reason: ``bootstrap()`` needs the
    ``table_exists``/``table_partition_spec`` probes that the real
    ``BerdlCapability`` does not implement. It is setup scaffolding and it is
    nowhere near the write path this test exercises.
    """
    from kbutillib.domains.kbase.berdl import clearinghouse_ingest as ci
    from kbutillib.domains.kbase.berdl.capability import BerdlCapability
    from kbutillib.domains.kbase.berdl.clearinghouse_schema import bootstrap

    real = BerdlCapability()
    if real.locus() != "in_pod":
        pytest.skip(
            "not running inside the BERDL pod (BerdlCapability.locus() != "
            "'in_pod'); the live backfill path needs an in-pod Spark session"
        )

    root = tmp_path_factory.mktemp("bf-live")

    mp = pytest.MonkeyPatch()
    # The READ side of every verb is hardwired to _FQN_PREFIX; point it (and
    # the schema NAMESPACE) at the scratch dataset so the verification reads,
    # verify_run and the orphan check all read scratch. The WRITE side is NOT
    # patched -- it resolves from --dataset through the real capability.
    mp.setattr(chc, "_FQN_PREFIX", f"{TENANT}.{bf_scratch_dataset}", raising=True)
    mp.setattr(chs, "NAMESPACE", bf_scratch_dataset, raising=True)
    mp.setenv(ci.STATE_DIR_ENV, str(root / "state"))
    mp.setenv(ci.LOCK_DIR_ENV, str(root / "locks"))
    (root / "locks").mkdir(parents=True, exist_ok=True)

    # Dotted, tenant-prefixed -- see the same note in live_clearinghouse.
    bf_scratch_ns_fqn = f"{TENANT}.{bf_scratch_dataset}"

    real.query(
        f"CREATE NAMESPACE IF NOT EXISTS {_q(TENANT, bf_scratch_dataset)}",
        engine="spark",
    )
    bootstrap(
        _ScratchAdapter(real, bf_scratch_ns_fqn),
        namespace=bf_scratch_ns_fqn,
        dataset=bf_scratch_dataset,
        tenant=TENANT,
    )

    def _teardown() -> None:
        for cfg in chs.table_configs():
            _drop_or_warn(
                real,
                f"DROP TABLE IF EXISTS "
                f"{_q(TENANT, bf_scratch_dataset, cfg['name'])}",
                what=f"table {bf_scratch_ns_fqn}.{cfg['name']}",
            )
        _drop_or_warn(
            real,
            f"DROP NAMESPACE IF EXISTS {_q(TENANT, bf_scratch_dataset)}",
            what=f"namespace {bf_scratch_ns_fqn}",
        )
        mp.undo()

    request.addfinalizer(_teardown)
    return {"real": real, "dataset": bf_scratch_dataset, "root": root}


def _run_backfill_cli(args: list[str]) -> dict[str, Any]:
    """Invoke ``kbu clearinghouse backfill ...`` and return its JSON envelope.

    Asserts exit 0 and parses stdout as exactly ONE object, which is the
    published output contract -- so a verb that printed a banner or a progress
    line beside the envelope fails here rather than silently.

    The streams are kept APART, exactly as in
    ``tests/cli/test_clearinghouse_backfill_cli.py``: under click < 8.2 the
    default interleaves stderr into ``result.stdout``, and every warning this
    verb emits goes to stderr -- so a mixed runner would hand this parser the
    warning text and fail on a run that was fine. Click 8.2 removed the
    parameter and made split streams the only behaviour, so it is probed
    rather than assumed.
    """
    from click.testing import CliRunner

    from kbutillib.cli import main

    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:
        runner = CliRunner()
    result = runner.invoke(main, ["clearinghouse", "backfill", *args, "--json"])
    stderr = result.stderr if result.stderr_bytes is not None else ""
    assert result.exit_code == 0, (
        f"`kbu clearinghouse backfill {' '.join(args)}` exited "
        f"{result.exit_code}:\n{result.stdout}\n{stderr}\n{result.exception!r}"
    )
    return json.loads(result.stdout.strip())


@_need_live
@pytest.mark.integration
def test_live_backfill_run_file_example_lands_then_adds_nothing(bf_live_scratch):
    """`backfill run file-example` lands rows; a SECOND run ingests ZERO.

    THE PRODUCTION CHAIN, NOTHING INJECTED. The CLI constructs its own
    ``ClearinghouseCapability()``, so this is the first test in the repo that
    proves the real ``BerdlCapability`` resolvers satisfy the dev 1206 guard --
    the property the old ``_ScratchAdapter`` resolvers were hiding, and the
    reason this module could be green while every production
    ``kbu clearinghouse load`` refused.

    THE SECOND RUN IS THE POINT. It is a NEW run id into the SAME dataset, so
    every row it shards is already present; fill-only must therefore ingest
    ZERO rows in EVERY ONE of the sixteen tables -- the ``parameter_set``
    registry INCLUDED, because the parameter set the preset declares (``{}``)
    is already registered and a registry batch is hash-keyed and fill-only
    like any other. A run that adds no new parameter set ingesting zero
    registry rows is SUCCESS, not a failure.

    The namespace-wide registry orphan check must come back empty after BOTH
    runs: the registry rows are written before the result rows that cite them,
    so there is never a window in which a stored result is un-interpretable.
    """
    dataset = bf_live_scratch["dataset"]
    root = bf_live_scratch["root"]

    # -- first run: it must actually land rows --------------------------
    first = _run_backfill_cli(
        [
            "run",
            "file-example",
            "--dataset",
            dataset,
            "--out",
            str(root / "run-1"),
        ]
    )
    first_data = first["data"]
    assert first_data["dataset"] == dataset, (
        "the run must target the scratch dataset it was given; got "
        f"{first_data['dataset']!r}"
    )
    assert not first_data["incomplete_units"], first_data["units"]
    units = first_data["units"]
    assert units, "the file-example plan produced no units"
    assert all(u["state"] == "done" for u in units), units
    assert sum(int(u["rows_ingested"]) for u in units) > 0, (
        "the first run ingested NOTHING -- the preset's 48 fixture rows should "
        f"have landed: {units}"
    )
    assert not first_data["parameter_set_integrity"]["failures"], (
        "registry orphan after the first run: "
        f"{first_data['parameter_set_integrity']}"
    )

    first_status = _run_backfill_cli(
        ["status", "--run-id", first_data["run_id"], "--out", str(root / "run-1")]
    )["data"]
    first_tables = {t["name"]: t["rows_ingested"] for t in first_status["tables"]}
    assert len(first_tables) == 16, sorted(first_tables)
    assert first_tables[chs.PARAMETER_SET_TABLE] > 0, (
        "the first run ingested no registry row, so the result rows it wrote "
        "cite a parameter set hash nothing explains: "
        f"{first_status['tables']}"
    )
    assert first_tables[table_name("protein", "entity")] > 0, first_status["tables"]
    assert first_tables[table_name("protein", "result")] > 0, first_status["tables"]

    # -- second run: a NEW run id, the SAME dataset, ZERO rows anywhere --
    second = _run_backfill_cli(
        [
            "run",
            "file-example",
            "--dataset",
            dataset,
            "--out",
            str(root / "run-2"),
        ]
    )
    second_data = second["data"]
    assert second_data["run_id"] != first_data["run_id"], (
        "the second run must be its own run -- scratch and production runs are "
        "never resumed into one another, and this is the same discipline"
    )
    assert not second_data["incomplete_units"], second_data["units"]
    assert all(u["state"] == "done" for u in second_data["units"]), second_data["units"]
    assert all(int(u["rows_ingested"]) == 0 for u in second_data["units"]), (
        "FILL-ONLY: the second run must append nothing, every row being "
        f"present already: {second_data['units']}"
    )
    assert not second_data["parameter_set_integrity"]["failures"], (
        "registry orphan after the second run: "
        f"{second_data['parameter_set_integrity']}"
    )

    second_status = _run_backfill_cli(
        ["status", "--run-id", second_data["run_id"], "--out", str(root / "run-2")]
    )["data"]
    second_tables = {t["name"]: t["rows_ingested"] for t in second_status["tables"]}
    assert len(second_tables) == 16, sorted(second_tables)
    # EVERY table, the registry included -- iterated from table_configs(), so
    # an ENTITY_TYPES x KINDS loop's blind spot cannot hide the registry here.
    assert all(count == 0 for count in second_tables.values()), (
        "the second run ingested rows into: "
        f"{sorted(n for n, c in second_tables.items() if c)}"
    )
    assert second_tables[chs.PARAMETER_SET_TABLE] == 0, (
        "a run that adds no new parameter set must ingest ZERO registry rows"
    )


def test_scratch_adapter_does_not_supply_the_namespace_resolvers():
    """``_ScratchAdapter`` must NOT define either namespace resolver.

    NOT gated: it is a pure introspection check and it is the regression guard
    for the whole reason this module was wrong. While the test adapter supplied
    ``resolve_write_namespace``/``resolve_probe_namespace``, the dev 1206 guard
    was satisfied by the TEST, so the live suite could be green while every
    real ``kbu clearinghouse load`` refused. Reintroducing either method here
    restores that blind spot, so it fails here instead.

    ``vars()`` and not ``hasattr``: the class deliberately DELEGATES both names
    to the real ``BerdlCapability`` through ``__getattr__``, which is the
    correct state and which ``hasattr`` cannot distinguish from a local
    definition.
    """
    defined = vars(_ScratchAdapter)
    assert "resolve_write_namespace" not in defined, (
        "_ScratchAdapter defines resolve_write_namespace again -- the write "
        "target must come from the REAL BerdlCapability, not from the test"
    )
    assert "resolve_probe_namespace" not in defined, (
        "_ScratchAdapter defines resolve_probe_namespace again -- the probe "
        "namespace must come from the REAL BerdlCapability, not from the test"
    )
    # ...and it must still carry the two bootstrap probes, which the real
    # capability does not implement and without which no scratch namespace can
    # be created at all.
    assert "table_exists" in defined
    assert "table_partition_spec" in defined

# ---------------------------------------------------------------------------
# Static guard -- NOT gated (pure source grep, safe everywhere)
# ---------------------------------------------------------------------------


def test_no_in_lake_insert_or_merge_targets_a_clearinghouse_table():
    """No ``INSERT ... SELECT`` / ``MERGE`` targets a clearinghouse table.

    Flagged risk #3: source and target share a tenant and catalog, so an
    in-lake ``INSERT INTO clearinghouse.<table> SELECT ...`` is one statement
    that looks right but bypasses ``data_lakehouse_ingest`` and its schema
    enforcement. Every sanctioned write must route through
    ``BerdlCapability.load()``. This grep-style assertion over the capability
    source catches a regression that reintroduces a direct in-lake write.
    """
    source = Path(chc.__file__).read_text(encoding="utf-8")
    # Strip string/comment noise is overkill here; match the SQL verbs that
    # would only appear if someone hand-wrote a lake mutation.
    offenders = re.findall(
        r"(?is)\b(?:INSERT\s+INTO|MERGE\s+INTO)\b[^\n;]*",
        source,
    )
    assert not offenders, (
        "found an in-lake INSERT/MERGE in clearinghouse_capability.py -- every "
        "clearinghouse write must go through BerdlCapability.load(): "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# UNGATED: the quoting helper, and a pin that every SQL site uses it
# ---------------------------------------------------------------------------
#
# Nothing below needs KBUTILLIB_LIVE_BERDL, a pod, or a Spark session. These
# are the tests that would have caught the two defects that blocked the
# 2026-10-13 in-pod acceptance before it was ever attempted.


class TestQuotingHelper:
    """Unit tests for :func:`_q`."""

    def test_backticks_each_part_and_joins_with_dots(self):
        assert _q("kbaseincubator", "clh_live_1234", "gene_entity") == (
            "`kbaseincubator`.`clh_live_1234`.`gene_entity`"
        )

    def test_single_part_is_quoted(self):
        assert _q("clearinghouse") == "`clearinghouse`"

    def test_a_dotted_part_gets_one_quote_pair_per_segment(self):
        """The in-pod D2 rule: a dotted namespace is NOT one identifier.

        Quoting it as one produces a two-part name whose first part contains a
        dot, which Spark does not resolve. So these two call shapes must agree.
        """
        assert _q("kbaseincubator.clh_live_1234", "gene_entity") == _q(
            "kbaseincubator", "clh_live_1234", "gene_entity"
        )
        assert "`kbaseincubator.clh_live_1234`" not in _q(
            "kbaseincubator.clh_live_1234", "gene_entity"
        )

    def test_never_emits_a_double_quote(self):
        assert '"' not in _q("kbaseincubator", "clh_live_1234", "gene_entity")

    def test_empty_segments_are_dropped(self):
        assert _q("kbaseincubator.", ".clh_live_1234") == (
            "`kbaseincubator`.`clh_live_1234`"
        )

    def test_rejects_an_embedded_backtick(self):
        """A backtick would close the quoting and let the rest parse as SQL."""
        with pytest.raises(ValueError, match="backtick"):
            _q("kbaseincubator", "clh`; DROP NAMESPACE x")

    def test_rejects_no_usable_segment(self):
        with pytest.raises(ValueError, match="non-empty identifier segment"):
            _q("", ".")


#: SQL statement keywords that mark an f-string in this module as SQL text.
_SQL_KEYWORD_RE = re.compile(
    r"(?i)\b(?:SELECT|CREATE\s+(?:TABLE|NAMESPACE)|DROP\s+(?:TABLE|NAMESPACE)"
    r"|INSERT\s+INTO|MERGE\s+INTO|ALTER\s+TABLE|DESCRIBE\s+TABLE"
    r"|SHOW\s+CREATE)\b"
)

#: Callables through which this module hands SQL text to the cluster. Every
#: string literal argument to one of these is a statement that will execute.
_SQL_SINK_NAMES = frozenset({"query", "_drop_or_warn"})


def _module_ast() -> ast.Module:
    """Parse THIS module's own source."""
    return ast.parse(Path(__file__).read_text(encoding="utf-8"))


def _literal_parts(node: ast.JoinedStr | ast.Constant) -> list[str]:
    """The literal (non-interpolated) text of a string or f-string node."""
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    return [
        v.value
        for v in node.values
        if isinstance(v, ast.Constant) and isinstance(v.value, str)
    ]


def _is_q_call(node: ast.AST) -> bool:
    """Whether ``node`` is a direct call to :func:`_q`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_q"
    )


def test_module_never_double_quotes_a_sql_identifier():
    """No f-string carrying SQL in this module may contain a double quote.

    DEFECT 1, pinned. Spark's parser reads ``"x"`` as a STRING LITERAL and
    rejects it where an identifier belongs::

        [PARSE_SYNTAX_ERROR] Syntax error at or near '"kbaseincubator"' ...
        CREATE NAMESPACE IF NOT EXISTS "kbaseincubator"."clh_bf_live_6ef3b2b1"

    That is verbatim from the 2026-10-13 in-pod run, where it was a hard
    ``ParseException`` in both fixtures and blocked all five lakehouse tests.
    It had already been reported once -- dev 1534, from the 2026-10-07 kbhub
    run -- fixed at the single site it was found at, and then written again at
    six more sites in the fixtures added after that fix. This test is why the
    seventh time fails off-pod, in CI, for free.

    Scanning f-strings (``ast.JoinedStr``) and not every string is what keeps
    this honest: the prose in this module's docstrings quotes SQL constantly,
    and docstrings are plain ``Constant`` nodes.
    """
    offenders: list[str] = []
    for node in ast.walk(_module_ast()):
        if not isinstance(node, ast.JoinedStr):
            continue
        parts = _literal_parts(node)
        if not _SQL_KEYWORD_RE.search(" ".join(parts)):
            continue
        if any('"' in part for part in parts):
            offenders.append(f"line {node.lineno}: {parts!r}")
    assert not offenders, (
        "a SQL f-string in this module contains an ANSI double quote, which "
        "Spark rejects where an identifier belongs -- route the identifier "
        f"through _q() instead: {offenders}"
    )


def test_every_interpolated_sql_identifier_goes_through_the_quoting_helper():
    """Every interpolation in a statement handed to the cluster must be ``_q``.

    The companion to the test above, and the stronger half: a reintroduced
    The companion to the test above, and the stronger half: a reintroduced
    double-quoted ``{TENANT}``/``{ns}`` interpolation fails the double-quote
    check above, but a reintroduced *unquoted* one would not, and an unquoted
    of ``query()`` or ``_drop_or_warn()`` -- the two sinks through which this
    module's SQL reaches Spark -- must come from ``_q()``.
    """
    offenders: list[str] = []
    for node in ast.walk(_module_ast()):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if name not in _SQL_SINK_NAMES:
            continue
        for arg in node.args:
            if not isinstance(arg, ast.JoinedStr):
                continue
            if not _SQL_KEYWORD_RE.search(" ".join(_literal_parts(arg))):
                continue
            for value in arg.values:
                if isinstance(value, ast.FormattedValue) and not _is_q_call(
                    value.value
                ):
                    offenders.append(
                        f"line {value.lineno}: {ast.unparse(value.value)}"
                    )
    assert not offenders, (
        "a SQL statement in this module interpolates an identifier that did "
        "not come from _q() -- one helper is what stops the Spark quoting "
        f"defect recurring an eighth time: {offenders}"
    )


def test_the_exact_reintroduced_shapes_are_absent_from_the_source():
    """A plain-text pin on the exact shapes that were actually written.

    The AST checks above are the real guard; this one is the blunt instrument
    that also covers prose, a commented-out line and anything the AST walk
    would not classify as SQL.

    The double-quote character is spelled ``chr(34)`` and the needles are
    assembled at run time, deliberately: written out, each needle would occur
    in THIS function's own source and the test could never pass.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    dq = chr(34)
    needles = [
        dq + '{TENANT}' + dq,
        dq + '.' + dq + '{',
        '}' + dq + '.' + dq,
    ]
    found = [needle for needle in needles if needle in source]
    assert not found, (
        f"the double-quoted-identifier shape is back in this module: {found}"
    )


# ---------------------------------------------------------------------------
# UNGATED: _ScratchAdapter against a fake "real" capability
# ---------------------------------------------------------------------------


class _FakeSparkSession:
    """Stands in for a Spark session: records every ``sql()`` statement."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def sql(self, statement: str) -> list[Any]:
        self.statements.append(statement)
        return []


class _ContractCheckingCapability:
    """A fake ``BerdlCapability`` that enforces the REAL ``load()`` contract.

    ``load()`` records its kwargs and then calls the SHIPPED
    :func:`~kbutillib.domains.kbase.berdl.capability.build_ingest_config`, so
    the bronze-mode refusal that broke the 2026-10-13 run --
    ``ValueError: build_ingest_config: bronze mode (no 'dataframes' given)
    requires 'paths' with a 'bronze_base'`` -- happens here, off-pod, exactly
    as it would in the pod. A fake that accepted any kwargs is precisely why
    DEFECT 2 was invisible until an attended pod run hit it.

    Same device as ``_ContractCheckingCapability`` in
    ``tests/berdl/test_clearinghouse_bootstrap_adapter.py``; duplicated rather
    than imported, so neither test module can break the other.
    """

    def __init__(self, *, query_error: Exception | None = None) -> None:
        self.load_calls: list[dict[str, Any]] = []
        self.query_calls: list[tuple[str, dict[str, Any]]] = []
        self._query_error = query_error

    def load(self, **kwargs: Any) -> dict[str, Any]:
        from kbutillib.domains.kbase.berdl.capability import build_ingest_config

        self.load_calls.append(kwargs)
        build_ingest_config(
            kwargs["dataset"],
            kwargs["tables"],
            dataframes=kwargs.get("dataframes"),
            paths=kwargs.get("paths"),
        )
        return {"ingest_result": "ok"}

    def query(self, sql: str, **kwargs: Any) -> Any:
        self.query_calls.append((sql, kwargs))
        if self._query_error is not None:
            raise self._query_error
        return []


_FAKE_NS = "kbaseincubator.clh_live_unit"


def _schema_only_tables() -> list[dict[str, Any]]:
    """The two table specs ``bootstrap()`` would pass, in its own shape.

    Taken from the real ``table_configs()`` -- one partitioned, one not -- and
    carrying ``mode='append'`` exactly as ``bootstrap()`` adds it.
    """
    configs = {cfg["name"]: cfg for cfg in chs.table_configs()}
    partitioned = next(cfg for cfg in configs.values() if cfg.get("partition_by"))
    unpartitioned = next(
        cfg for cfg in configs.values() if not cfg.get("partition_by")
    )
    return [{**dict(cfg), "mode": "append"} for cfg in (partitioned, unpartitioned)]


class TestScratchAdapterLoadRouting:
    """DEFECT 2: the adapter must not wrap the raw ``BerdlCapability.load``."""

    def test_schema_only_load_does_not_raise_the_bronze_mode_value_error(self):
        """A load with no dataframes and no paths -- what ``bootstrap()`` issues.

        Against the previous wiring (``_CountingLoad(real.load)``) this raises
        ``ValueError: build_ingest_config: bronze mode ...``, which is the
        failure all five lakehouse tests hit on 2026-10-13 once the quoting was
        hand-patched. It must now emit DDL instead.
        """
        real = _ContractCheckingCapability()
        spark = _FakeSparkSession()
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=spark)

        result = adapter.load(
            dataset="clh_live_unit",
            tables=_schema_only_tables(),
            namespace=_FAKE_NS,
            tenant=TENANT,
        )

        assert result["schema_only"] is True
        assert real.load_calls == [], (
            "a schema-only load must NOT reach BerdlCapability.load -- that is "
            "the call build_ingest_config refuses"
        )
        assert len(spark.statements) == len(_schema_only_tables())
        assert all(
            statement.startswith("CREATE TABLE IF NOT EXISTS")
            for statement in spark.statements
        ), spark.statements

    def test_schema_only_load_goes_through_the_bootstrap_capability(self):
        """The routing target is the SHIPPED component, not a local reimplementation.

        ``ClearinghouseBootstrapCapability`` is the path proven on this pod --
        it created the production clearinghouse tables (OP-C1 C1.3) and the
        ``clearinghouse_parity`` tables (OP-C2 C2.2).
        """
        adapter = _ScratchAdapter(
            _ContractCheckingCapability(), _FAKE_NS, spark=_FakeSparkSession()
        )
        assert isinstance(adapter._bootstrap, ClearinghouseBootstrapCapability)

    def test_schema_only_ddl_is_backtick_quoted_and_names_the_dotted_namespace(self):
        """The emitted DDL must address ``kbaseincubator.<scratch>``, backticked.

        The fixtures pass the DOTTED namespace precisely so this is true: the
        bare form does not resolve on this cluster (runbook (c), measured
        in-pod 2026-09-12), and ``kbaseincubator.<dataset>`` is also
        ``BerdlCapability``'s own write target, so bootstrap creates the tables
        where the writes under test will land.
        """
        spark = _FakeSparkSession()
        adapter = _ScratchAdapter(
            _ContractCheckingCapability(), _FAKE_NS, spark=spark
        )
        adapter.load(
            dataset="clh_live_unit",
            tables=_schema_only_tables()[:1],
            namespace=_FAKE_NS,
            tenant=TENANT,
        )
        statement = spark.statements[0]
        assert "`kbaseincubator`.`clh_live_unit`." in statement, statement
        assert '"' not in statement, statement

    def test_data_bearing_load_still_reaches_the_real_capability(self):
        """A real write must NOT be re-routed: this module proves THAT path."""
        real = _ContractCheckingCapability()
        spark = _FakeSparkSession()
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=spark)

        rows = [_entity_row(1, "protein", batch="unit")]
        adapter.load(
            dataset="clh_live_unit",
            tables=[{"name": table_name("protein", "entity"), "mode": "append"}],
            namespace=_FAKE_NS,
            tenant=TENANT,
            dataframes={table_name("protein", "entity"): rows},
        )

        assert len(real.load_calls) == 1, (
            "a data-bearing load must go to the REAL capability unchanged"
        )
        assert "spark" not in real.load_calls[0], (
            "the test adapter must not inject its own Spark session into the "
            "production write path"
        )
        assert spark.statements == [], "a data write must emit no DDL"

    def test_counting_load_records_both_modes(self):
        """The dev 1206 zero-call assertion must stay meaningful in BOTH modes.

        ``test_live_mismatched_write_target_raises_and_never_loads`` asserts a
        refused write made EXACTLY ZERO load calls. That only means anything if
        the counter sits in front of every load the code under test can make,
        so the counter wraps the DISPATCHER and not one branch of it.
        """
        real = _ContractCheckingCapability()
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=_FakeSparkSession())
        assert adapter.load.calls == 0

        adapter.load(
            dataset="clh_live_unit",
            tables=_schema_only_tables()[:1],
            namespace=_FAKE_NS,
            tenant=TENANT,
        )
        assert adapter.load.calls == 1, "a schema-only load must be counted"

        name = table_name("protein", "entity")
        adapter.load(
            dataset="clh_live_unit",
            tables=[{"name": name, "mode": "append"}],
            namespace=_FAKE_NS,
            tenant=TENANT,
            dataframes={name: [_entity_row(2, "protein", batch="unit")]},
        )
        assert adapter.load.calls == 2, "a data-bearing load must be counted"

    def test_schema_only_predicate_matches_the_shipped_adapters_own_branch(self):
        """``_is_schema_only_load`` must agree with the component it dispatches to."""
        assert _is_schema_only_load({}) is True
        assert _is_schema_only_load({"paths": {"silver_base": "s3://x"}}) is True
        assert _is_schema_only_load({"dataframes": {}}) is True
        assert _is_schema_only_load({"dataframes": {"t": []}}) is False
        assert _is_schema_only_load({"paths": {"bronze_base": "s3://x"}}) is False


# ---------------------------------------------------------------------------
# UNGATED: table_exists must never answer "absent" for a failed lookup
# ---------------------------------------------------------------------------


class _SparkConditionError(Exception):
    """Stands in for a pyspark ``AnalysisException`` carrying a condition.

    pyspark is NOT importable off the pod, so the real classes cannot be used
    here. This reproduces the two things the probe actually reads: the
    ``getCondition()`` accessor (pyspark 4.x) and the ``[NAME]`` prefix pyspark
    renders onto ``str(exc)``.
    """

    def __init__(self, condition: str, detail: str, *, accessor: bool = True) -> None:
        super().__init__(f"[{condition}] {detail}")
        self._condition = condition
        self._accessor = accessor

    def getCondition(self) -> str:  # noqa: N802 - pyspark's own spelling
        if not self._accessor:
            raise AttributeError("getCondition")
        return self._condition


def _not_found_error(*, accessor: bool = True) -> _SparkConditionError:
    return _SparkConditionError(
        "TABLE_OR_VIEW_NOT_FOUND",
        "The table or view `kbaseincubator`.`clh_live_unit`.`gene_entity` "
        "cannot be found.",
        accessor=accessor,
    )


def _parse_error(*, accessor: bool = True) -> _SparkConditionError:
    """The VERBATIM error from the 2026-10-13 in-pod run."""
    return _SparkConditionError(
        "PARSE_SYNTAX_ERROR",
        "Syntax error at or near '\"kbaseincubator\"'. SQLSTATE: 42601 "
        '(line 1, pos 30) == SQL == CREATE NAMESPACE IF NOT EXISTS '
        '"kbaseincubator"."clh_bf_live_6ef3b2b1"',
        accessor=accessor,
    )


class TestScratchAdapterTableExists:
    """DEFECT 1's worst consequence: a parse error read as "table absent"."""

    def test_returns_false_on_a_genuine_not_found(self):
        real = _ContractCheckingCapability(query_error=_not_found_error())
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=_FakeSparkSession())
        assert adapter.table_exists("gene_entity") is False

    def test_returns_true_when_the_probe_succeeds(self):
        real = _ContractCheckingCapability()
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=_FakeSparkSession())
        assert adapter.table_exists("gene_entity") is True

    def test_probe_sql_is_backtick_quoted_per_segment(self):
        real = _ContractCheckingCapability()
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=_FakeSparkSession())
        adapter.table_exists("gene_entity")
        sql, kwargs = real.query_calls[0]
        assert sql == (
            "SELECT 1 FROM `kbaseincubator`.`clh_live_unit`.`gene_entity` LIMIT 0"
        )
        assert kwargs == {"engine": "spark"}

    def test_re_raises_a_parse_error(self):
        """The 2026-10-13 failure: sixteen probes all answered "absent".

        ``bootstrap()`` wraps this re-raise into
        ``BootstrapIndeterminateStateError`` with nothing written, which is the
        only honest outcome for a lookup that did not answer.
        """
        real = _ContractCheckingCapability(query_error=_parse_error())
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=_FakeSparkSession())
        with pytest.raises(_SparkConditionError, match="PARSE_SYNTAX_ERROR"):
            adapter.table_exists("gene_entity")

    def test_re_raises_an_auth_error(self):
        real = _ContractCheckingCapability(
            query_error=PermissionError("not authorized on kbaseincubator")
        )
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=_FakeSparkSession())
        with pytest.raises(PermissionError):
            adapter.table_exists("gene_entity")

    def test_re_raises_a_connection_error(self):
        real = _ContractCheckingCapability(
            query_error=ConnectionError("spark connect channel closed")
        )
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=_FakeSparkSession())
        with pytest.raises(ConnectionError):
            adapter.table_exists("gene_entity")

    def test_bootstrap_refuses_rather_than_creating_on_an_indeterminate_probe(self):
        """End to end: a failed probe must stop ``bootstrap()`` writing anything.

        This is the property the silent ``except Exception: return False``
        destroyed -- with it, bootstrap read "absent" for every table and went
        on to create all sixteen.
        """
        spark = _FakeSparkSession()
        real = _ContractCheckingCapability(query_error=_parse_error())
        adapter = _ScratchAdapter(real, _FAKE_NS, spark=spark)
        with pytest.raises(chs.BootstrapIndeterminateStateError):
            chs.bootstrap(
                adapter, namespace=_FAKE_NS, dataset="clh_live_unit", tenant=TENANT
            )
        assert spark.statements == [], "nothing may be created on a refused bootstrap"
        assert adapter.load.calls == 0


class TestMissingRelationDiscrimination:
    """:func:`_is_missing_relation_error` -- what the probe discriminates on."""

    def test_condition_accessor_is_used_when_present(self):
        assert _is_missing_relation_error(_not_found_error()) is True
        assert _is_missing_relation_error(_parse_error()) is False

    def test_rendered_condition_prefix_is_used_when_no_accessor(self):
        """A Spark Connect client may expose no accessor, only the message."""
        assert _is_missing_relation_error(_not_found_error(accessor=False)) is True
        assert _is_missing_relation_error(_parse_error(accessor=False)) is False

    def test_namespace_level_absence_counts_as_not_found(self):
        assert (
            _is_missing_relation_error(
                _SparkConditionError("SCHEMA_NOT_FOUND", "The schema cannot be found.")
            )
            is True
        )

    def test_typed_client_errors_count_as_not_found(self):
        class NoSuchTableError(Exception):
            pass

        assert _is_missing_relation_error(NoSuchTableError("gene_entity")) is True

    def test_legacy_message_text_counts_as_not_found(self):
        for message in (
            "Table or view not found: t",
            "Namespace 'x' does not exist",
        ):
            assert _is_missing_relation_error(Exception(message)) is True, message

    def test_an_unrelated_failure_is_not_read_as_absence(self):
        assert _is_missing_relation_error(Exception("connection reset")) is False
        assert _is_missing_relation_error(TimeoutError("spark timed out")) is False

    def test_a_published_condition_wins_over_the_message_text(self):
        """A parse error QUOTING a missing-table phrase must still re-raise."""
        exc = _SparkConditionError(
            "PARSE_SYNTAX_ERROR", "near 'Table or view not found'"
        )
        assert _is_missing_relation_error(exc) is False


# ---------------------------------------------------------------------------
# UNGATED: teardown must leave a visible trace when it cannot drop
# ---------------------------------------------------------------------------


class TestTeardownSurfacesFailures:
    """A scratch namespace that survives a run must be VISIBLE in that run.

    Teardown was ``except Exception: pass``, so two namespaces leaked on
    2026-10-13 while the module docstring claimed both were torn down.
    """

    def test_a_successful_drop_warns_nothing(self):
        real = _ContractCheckingCapability()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _drop_or_warn(
                real,
                f"DROP NAMESPACE IF EXISTS {_q(_FAKE_NS)}",
                what=f"namespace {_FAKE_NS}",
            )
        assert len(real.query_calls) == 1

    def test_a_genuine_not_found_is_tolerated_silently(self):
        real = _ContractCheckingCapability(query_error=_not_found_error())
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _drop_or_warn(real, "DROP TABLE IF EXISTS `x`.`y`", what="table x.y")

    def test_any_other_failure_warns_and_names_the_namespace(self):
        statement = f"DROP NAMESPACE IF EXISTS {_q(_FAKE_NS)}"
        real = _ContractCheckingCapability(
            query_error=Exception("namespace is not empty")
        )
        with pytest.warns(UserWarning) as caught:
            _drop_or_warn(real, statement, what=f"namespace {_FAKE_NS}")
        message = str(caught[0].message)
        assert _FAKE_NS in message, message
        assert statement in message, message
        assert "LEAKED" in message, message

    def test_it_never_raises_out_of_a_finalizer(self):
        """It runs in a finalizer: raising would mask the real test failure."""
        real = _ContractCheckingCapability(query_error=RuntimeError("boom"))
        with pytest.warns(UserWarning):
            _drop_or_warn(real, "DROP TABLE IF EXISTS `x`.`y`", what="table x.y")
