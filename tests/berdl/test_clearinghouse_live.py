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

Both scratch namespaces are torn down at the end.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import pytest

from kbutillib.domains.identity.standardizers import STANDARDIZER_VERSION
from kbutillib.domains.kbase.berdl import clearinghouse_capability as chc
from kbutillib.domains.kbase.berdl import clearinghouse_schema as chs
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
      ``BerdlCapability`` does NOT implement (only
      ``ClearinghouseBootstrapCapability`` does, and it resolves its own in-pod
      Spark session rather than reusing this fixture's). The
      ``live_clearinghouse`` and ``bf_live_backfill`` fixtures both call
      ``bootstrap()`` to create the sixteen scratch tables, so without these
      two the scratch namespace cannot be built at all and nothing in this
      module can run. They are SETUP, not the contract under test.
    - ``load`` wrapped in a :class:`_CountingLoad`, so a test can assert the
      zero-call refusal contract.
    """

    def __init__(self, real: Any, scratch_ns: str) -> None:
        self._real = real
        self._scratch_ns = scratch_ns
        self.load = _CountingLoad(real.load)

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
        ns = namespace or self._scratch_ns
        fqn = f'"{TENANT}"."{ns}"."{name}"'
        try:
            self._real.query(f"SELECT 1 FROM {fqn} LIMIT 0", engine="spark")
            return True
        except Exception:
            return False

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

    adapter = _ScratchAdapter(real, scratch_namespace)

    # Create the scratch namespace and the fifteen tables.
    real.query(
        f'CREATE NAMESPACE IF NOT EXISTS "{TENANT}"."{scratch_namespace}"',
        engine="spark",
    )
    bootstrap(
        adapter,
        namespace=scratch_namespace,
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
            try:
                real.query(
                    f'DROP TABLE IF EXISTS '
                    f'"{TENANT}"."{scratch_namespace}"."{cfg["name"]}"',
                    engine="spark",
                )
            except Exception:
                pass
        try:
            real.query(
                f'DROP NAMESPACE IF EXISTS "{TENANT}"."{scratch_namespace}"',
                engine="spark",
            )
        except Exception:
            pass
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

    adapter = _ScratchAdapter(real, scratch_namespace)

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

    real.query(
        f'CREATE NAMESPACE IF NOT EXISTS "{TENANT}"."{bf_scratch_dataset}"',
        engine="spark",
    )
    bootstrap(
        _ScratchAdapter(real, bf_scratch_dataset),
        namespace=bf_scratch_dataset,
        dataset=bf_scratch_dataset,
        tenant=TENANT,
    )

    def _teardown() -> None:
        for cfg in chs.table_configs():
            try:
                real.query(
                    f'DROP TABLE IF EXISTS '
                    f'"{TENANT}"."{bf_scratch_dataset}"."{cfg["name"]}"',
                    engine="spark",
                )
            except Exception:
                pass
        try:
            real.query(
                f'DROP NAMESPACE IF EXISTS "{TENANT}"."{bf_scratch_dataset}"',
                engine="spark",
            )
        except Exception:
            pass
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
