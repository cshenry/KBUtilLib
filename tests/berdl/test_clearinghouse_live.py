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
   scratch namespace for the lifetime of the test, so BOTH reads and writes
   land in scratch. Production is never named by any statement this module
   issues.

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
- ``ingest_shards()`` of a tiny shard directory: the pre-write assertion passes
  against a correctly-resolved namespace, postflight ``row_count`` and
  ``snapshot_id`` come back NON-NULL, and the ledger records state
  ``"ingested"``.
- A deliberately mismatched write target RAISES
  (:class:`ClearinghouseWriteTargetMismatchError`) and calls ``load()`` EXACTLY
  ZERO times (flagged risk #2, dev 1206: an implementation that writes and then
  raises passes a weaker "an exception was raised" test and is the exact
  failure).
- No in-lake ``INSERT ... SELECT`` / ``MERGE`` targets a clearinghouse table
  (flagged risk #3): a grep-style assertion over the module source. This one
  is NOT gated -- it is a pure static check and runs everywhere.

The scratch namespace is torn down at the end.
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
    ENTITY_TYPES,
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
    """Adapts a real ``BerdlCapability`` to the scratch namespace.

    Delegates ``locus``/``query``/``load`` to the real capability, and supplies
    the four resolver/probe methods ``ClearinghouseCapability`` and
    ``bootstrap`` require but the real ``BerdlCapability`` does not yet expose:

    - ``resolve_write_namespace`` / ``resolve_probe_namespace`` -- both return
      the SAME scratch namespace, so the dev 1206 pre-write guard passes for
      the correctly-resolved case and the write lands in scratch.
    - ``table_exists`` / ``table_partition_spec`` -- read-only Iceberg metadata
      probes used by ``bootstrap`` to create-or-verify each scratch table.

    ``load`` is routed through a :class:`_CountingLoad` so a test can assert the
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

    # -- namespace resolvers the guard requires --------------------------

    def resolve_write_namespace(self, *, dataset: str, tenant: str) -> str:
        return self._scratch_ns

    def resolve_probe_namespace(self, *, dataset: str, tenant: str) -> str:
        return self._scratch_ns

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
        for kind in ("entity", "content", "result"):
            for etype in ENTITY_TYPES:
                name = table_name(etype, kind)
                try:
                    real.query(
                        f'DROP TABLE IF EXISTS '
                        f'"{TENANT}"."{scratch_namespace}"."{name}"',
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
        "payload": json.dumps({"seed": seed, "batch": batch}),
        "observed_at": observed_at,
        "ingest_batch_id": batch,
    }


# ---------------------------------------------------------------------------
# LIVE tests
# ---------------------------------------------------------------------------


@_need_live
@pytest.mark.integration
def test_live_stats_counts_all_fifteen_from_explicit_count(live_clearinghouse):
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
    live_clearinghouse.register("protein", rows, kind="entity")

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
def test_live_known_round_trips(live_clearinghouse):
    """known() finds a registered hash and reports a fresh hash as absent."""
    present_hash = _hash(2001)
    absent_hash = _hash(999999)

    live_clearinghouse.register(
        "gene",
        [_entity_row(2001, "gene", batch="known-fixture")],
        kind="entity",
    )

    found = live_clearinghouse.known("gene", [present_hash])
    assert encode_entity_hash(present_hash) in found

    not_found = live_clearinghouse.known("gene", [absent_hash])
    assert encode_entity_hash(absent_hash) not in not_found


@_need_live
@pytest.mark.integration
def test_live_current_state_latest_per_slot(live_clearinghouse):
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
def test_live_ingest_shards_tiny_directory(live_clearinghouse, tmp_path):
    """ingest_shards() of a tiny shard dir: postflight non-null, ledger 'ingested'.

    The pre-write assertion must pass against the correctly-resolved scratch
    namespace, postflight ``row_count`` and ``snapshot_id`` must come back
    NON-NULL, and the ledger must record state ``"ingested"``.
    """
    try:
        import pandas as pd  # noqa: PLC0415
    except Exception:  # pragma: no cover - environment gate
        pytest.skip("pandas not importable; cannot write a parquet shard fixture")

    shard_root = tmp_path / "shards"
    table = table_name("ontology_term", "entity")
    (shard_root / table).mkdir(parents=True)
    df = pd.DataFrame(
        [_entity_row(4000 + i, "ontology_term", batch="0001") for i in range(2)]
    )
    shard_path = shard_root / table / "0001.parquet"
    df.to_parquet(shard_path)

    result = live_clearinghouse.ingest_shards(shard_root, run_id="live-ingest-1")

    assert result["namespace"], "ingest resolved an empty namespace"
    ingested = [t for t in result["tables"] if t["action"] == "ingest"]
    assert ingested, f"nothing was ingested: {result['tables']}"
    for t in ingested:
        assert t["row_count"] is not None, f"null postflight row_count: {t}"
        assert t["snapshot_id"] is not None, f"null postflight snapshot_id: {t}"

    ledger_lines = [
        json.loads(line)
        for line in (shard_root / chc.LEDGER_FILENAME).read_text().splitlines()
        if line.strip()
    ]
    terminal = [
        ln
        for ln in ledger_lines
        if ln["table"] == table and ln["state"] == "ingested"
    ]
    assert terminal, f"ledger has no 'ingested' line for {table}: {ledger_lines}"


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
