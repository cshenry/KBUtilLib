"""``council_delivery`` -- COUNCIL-DELIVERY v1 and the restartable scanner.

MOSTLY THE UNHAPPY PATHS, because the happy path is the one that was never in
doubt. What breaks a delivery rail is a manifest that arrived before its
bytes, a deadline that never expires because consumer-side mtime reset, a
receipt that certifies work nobody did for this delivery, a publish failure
that gets skipped forever on the strength of an archive receipt, and two
instances of the loop racing to start the same delivery.

THE ARCHIVER IS REAL IN EVERY TEST HERE. ``archive_snapshot`` is never
replaced by a counting callable: the "did it archive", "did it NOT re-archive"
and "did two scanners double-archive" questions are all answered by COUNTING
ROWS IN A REAL ``SqliteCouncilSink``, because a fake archiver can only tell
you how many times something was called, which is not the thing that matters.
The concurrency test runs TWO REAL SCANNER INSTANCES in two threads against
one drop directory and one sink FILE.

The PUBLISHER is stubbed in exactly the tests whose subject is what happens
when publication FAILS -- that is the collaborator being made to fail, not the
thing under test -- and there is a separate test that runs the REAL
lakehouse-sourced publisher end to end.

NOT A CLAIM ABOUT A LIVE POD. SQLite is not Iceberg and the BERDL transport
has been broken on kbhub since the 2026-10-19 image (KBUtilLib#91). Nothing
here loads anything, and the operator registration step is an explicitly
NAMED GAP (``OPERATOR_INSTALL["GAP"]``), not an invention.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

from kbutillib.domains.ai.council.store.sqlite import SqliteStore  # noqa: E402
from kbutillib.domains.kbase.berdl import council_delivery as cdel  # noqa: E402
from kbutillib.domains.kbase.berdl.council_archive import (  # noqa: E402
    ArchiveLedger,
    reconcile_delivery,
)
from kbutillib.domains.kbase.berdl.council_sink import SqliteCouncilSink  # noqa: E402

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
STORE_ID = "poplar/council-1"


def _call(job_id: str, item_id: str, **extra: Any) -> dict:
    row = {
        "job_id": job_id,
        "item_id": item_id,
        "role": "proposer",
        "seat_name": "p1",
        "endpoint": "argo",
        "endpoint_kind": "openai",
        "served_model": "test/p1",
        "canonical_model": "test/p1",
        "submitted_by": "miles",
        "created_at": NOW,
    }
    row.update(extra)
    return row


def _build_store(path: Path, *, store_id: str = STORE_ID) -> SqliteStore:
    """A small REAL council store -- enough for every archived table to have rows."""
    store = SqliteStore(str(path))
    store.init_schema()
    store.assign_store_identity(store_id, declared_by="miles")
    store.put_query_type(
        name="go_mf",
        version="4",
        spec={
            "name": "go_mf",
            "version": "4",
            "entity_field": "text",
            "id_field": "go_id",
            "roles": {
                "proposer": {"system": "s", "user": "u", "schema": {"type": "object"}}
            },
        },
        created_by="miles",
    )
    store.create_job(
        {
            "job_id": "job-1",
            "label": "chunk01",
            "query_name": "go_mf",
            "query_version": "4",
            "submitted_by": "miles",
            "submitted_at": NOW,
            "status": "done",
            "roster_fingerprint": hashlib.sha256(b"roster").hexdigest(),
        },
        [
            {"item_id": "i1", "evidence": {"text": "Glucokinase"}},
            {"item_id": "i2", "evidence": {"text": "Hexokinase"}},
        ],
        [
            _call("job-1", "i1", call_id="c1", status="ok"),
            _call("job-1", "i2", call_id="c2", status="ok"),
        ],
    )
    store.settle_item("job-1", "i1", "ok", verdict={"selected_ids": ["GO:0004340"]})
    store.settle_item("job-1", "i2", "ok", verdict={"selected_ids": ["GO:0004396"]})
    store.record_attempt(
        "c1",
        1,
        started_at=NOW,
        finished_at=NOW,
        outcome_kind="ok",
        usage={"input_tokens": 10, "output_tokens": 2},
        worker_id="w1",
    )
    store.add_tokens("argo", 12, user=None)
    store.register_worker(
        {"worker_id": "w1", "user": "local", "host": "poplar", "started_at": NOW}
    )
    return store


def _drop_a_delivery(
    drop_root: Path,
    tmp_path: Path,
    *,
    name: str = "delivery-1",
    store_id: str = STORE_ID,
    deadline: timedelta | None = timedelta(hours=6),
    legacy: bool = False,
    extra_file: str | None = "NOTES.txt",
    bundle_factory: Any = None,
    created_at: datetime = NOW,
    snapshot_seq: int = 1,
) -> dict:
    """Write a REAL snapshot plus a COUNCIL-DELIVERY v1 manifest into a drop dir.

    The snapshot is taken with the REAL ``SqliteStore.snapshot()`` (the
    sqlite3 backup API), so the delivered bytes are a consistent store and the
    declared ``snapshot_id`` is its VERIFIED digest -- not a label.
    """
    drop = drop_root / name
    drop.mkdir(parents=True, exist_ok=True)
    store = _build_store(tmp_path / "stores" / f"{name}.sqlite", store_id=store_id)
    try:
        manifest = store.snapshot(drop / "council-snapshot.sqlite")
        counts = {
            table: int(value) for table, value in (manifest.get("row_counts") or {}).items()
        }
    finally:
        store.close()
    # The backup API leaves the DESTINATION's own `-wal`/`-shm` sidecars
    # beside it. A producer delivers the snapshot FILE, not a live database
    # plus its log: the manifest's snapshot_id is the digest of that one file,
    # and shipping a stale log beside it would let a consumer read bytes the
    # digest does not cover.
    for sidecar in ("-wal", "-shm"):
        side = drop / f"council-snapshot.sqlite{sidecar}"
        if side.exists():
            side.unlink()
    if extra_file:
        (drop / extra_file).write_text("the run's notes\n", encoding="utf-8")
    bundle = None
    if bundle_factory is not None:
        from kbutillib.domains.genome.ontology_mapping.council_bundle import write_bundle

        # Built AGAINST THE DELIVERED SNAPSHOT: a bundle naming another
        # snapshot is refused by the publisher's completeness gate, which is
        # a different refusal from the one these tests are about.
        bundle = bundle_factory(manifest["snapshot_id"])
        write_bundle(bundle, drop / "BUNDLE.json")
    declared = cdel.build_manifest(
        drop,
        delivery_id=name,
        store_id=store_id,
        snapshot_id=manifest["snapshot_id"],
        snapshot_file="council-snapshot.sqlite",
        snapshot_seq=snapshot_seq,
        snapshot_schema_version=2,
        store_host="poplar",
        created_at=created_at,
        deadline=None if legacy else deadline,
        bundle=(
            {
                "campaign_id": bundle["campaign_id"],
                "bundle_digest": bundle["bundle_digest"],
                "path": "BUNDLE.json",
            }
            if bundle is not None
            else None
        ),
    )
    cdel.write_manifest(declared, drop / cdel.MANIFEST_FILENAME)
    return {
        "drop": drop,
        "manifest": declared,
        "snapshot_manifest": manifest,
        "row_counts": counts,
        "digest": cdel.manifest_digest(declared),
    }


@pytest.fixture
def drop_root(tmp_path: Path) -> Path:
    root = tmp_path / "dropbox" / "council-deliveries"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def sink(tmp_path: Path):
    opened = SqliteCouncilSink(tmp_path / "lake" / "council.sqlite")
    opened.bootstrap()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    return tmp_path / "deliveries"


def _scanner(drop_root: Path, sink, state_root: Path, tmp_path: Path, **kwargs):
    return cdel.CouncilDeliveryScanner(
        drop_dir=drop_root,
        scratch_dir=tmp_path / "scratch",
        sink=sink,
        state_root=state_root,
        poll_seconds=1,
        **kwargs,
    )


def _archived_calls(sink, delivery: dict) -> int:
    return sink.count_rows(
        "calls",
        where={
            "store_id": delivery["manifest"]["store_id"],
            "snapshot_id": delivery["manifest"]["snapshot_id"],
        },
    )


# --------------------------------------------------------------------------
# The manifest
# --------------------------------------------------------------------------


class TestManifestV1:
    def test_it_round_trips(self, drop_root: Path, tmp_path: Path) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        read = cdel.read_manifest(delivery["drop"] / cdel.MANIFEST_FILENAME)
        assert read == delivery["manifest"]
        assert set(read) == set(cdel.MANIFEST_FIELDS)
        assert read["schema_version"] == cdel.DELIVERY_SCHEMA_VERSION
        assert read["store_id"] == STORE_ID
        assert read["snapshot_seq"] == 1
        assert read["snapshot_schema_version"] == 2
        assert read["store_host"] == "poplar"
        assert cdel.manifest_digest(read) == delivery["digest"]

    def test_the_inventory_is_measured_and_relative(
        self, drop_root: Path, tmp_path: Path
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        entries = {entry["path"]: entry for entry in delivery["manifest"]["files"]}
        assert set(entries) == {"council-snapshot.sqlite", "NOTES.txt"}
        for path, entry in entries.items():
            on_disk = delivery["drop"] / path
            assert entry["bytes"] == on_disk.stat().st_size
            assert entry["sha256"] == cdel.file_sha256(on_disk)
            assert not path.startswith("/")

    def test_both_timestamps_are_utc(self, drop_root: Path, tmp_path: Path) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        for field in ("created_at", "deadline_at"):
            parsed = datetime.fromisoformat(delivery["manifest"][field])
            assert parsed.tzinfo is not None
            assert parsed.utcoffset() == timedelta(0)

    @pytest.mark.parametrize(
        "mangle,because",
        [
            (lambda m: m.pop("files"), "no inventory means arrival cannot be checked"),
            (lambda m: m.update(files=[]), "an empty inventory checks nothing"),
            (lambda m: m.update(schema_version=2), "another schema version"),
            (lambda m: m.pop("store_id"), "every archived row is keyed on it"),
            (lambda m: m.pop("snapshot_id"), "every archived row is keyed on it"),
            (lambda m: m.pop("created_at"), "a delivery with no age"),
            (
                lambda m: m.update(files=[{"path": "x", "bytes": 1}]),
                "an inventory entry with no digest",
            ),
            (
                lambda m: m.update(files=[{"path": "/etc/passwd", "bytes": 1, "sha256": "a"}]),
                "an absolute inventory path",
            ),
            (
                lambda m: m.update(files=[{"path": "../x", "bytes": 1, "sha256": "a"}]),
                "an escaping inventory path",
            ),
            (lambda m: m.update(created_at="not a timestamp"), "an unparseable time"),
            (lambda m: m.update(created_at="2026-10-25T12:00:00"), "a naive timestamp"),
        ],
    )
    def test_a_malformed_manifest_is_rejected(
        self, drop_root: Path, tmp_path: Path, mangle, because: str
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        content = dict(delivery["manifest"])
        mangle(content)
        path = tmp_path / "bad" / cdel.MANIFEST_FILENAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(content), encoding="utf-8")
        with pytest.raises(cdel.DeliveryManifestError):
            cdel.read_manifest(path)

    def test_a_manifest_that_is_not_json_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / cdel.MANIFEST_FILENAME
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(cdel.DeliveryManifestError):
            cdel.read_manifest(path)

    def test_an_absent_manifest_is_rejected_rather_than_empty(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(cdel.DeliveryManifestError):
            cdel.read_manifest(tmp_path / "nothing.json")

    def test_a_snapshot_file_outside_the_inventory_is_refused(
        self, drop_root: Path, tmp_path: Path
    ) -> None:
        drop = drop_root / "d"
        drop.mkdir()
        (drop / "a.txt").write_text("a", encoding="utf-8")
        with pytest.raises(cdel.DeliveryManifestError) as caught:
            cdel.build_manifest(
                drop,
                delivery_id="d",
                store_id=STORE_ID,
                snapshot_id="a" * 64,
                snapshot_file="not-delivered.sqlite",
                snapshot_seq=1,
                snapshot_schema_version=2,
            )
        assert "not in the inventory" in str(caught.value)

    def test_the_identity_moves_when_the_manifest_is_edited(
        self, drop_root: Path, tmp_path: Path
    ) -> None:
        """Which is what makes a cached receipt stop counting."""
        delivery = _drop_a_delivery(drop_root, tmp_path)
        edited = {**delivery["manifest"], "snapshot_seq": 2}
        assert cdel.manifest_digest(edited) != delivery["digest"]

    def test_the_delivery_manifest_is_not_the_snapshot_manifest(
        self, drop_root: Path, tmp_path: Path
    ) -> None:
        """Two different documents, two different names, on purpose.

        The snapshot's own manifest is a single-file checksum record; this one
        is a multi-file delivery descriptor. One name for both is how a
        consumer validates the wrong document.
        """
        assert cdel.MANIFEST_FILENAME != "MANIFEST.json"
        delivery = _drop_a_delivery(drop_root, tmp_path)
        assert set(delivery["snapshot_manifest"]) != set(delivery["manifest"])


# --------------------------------------------------------------------------
# Arrival: the manifest's presence is not proof of it
# --------------------------------------------------------------------------


class TestArrival:
    def test_a_complete_delivery_archives(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        tick = scanner.tick(now=NOW)
        assert tick["counts"] == {"complete": 1}, tick
        report = tick["reports"][0]
        assert report["arrival"]["state"] == "complete"
        assert report["archive"]["state"] == cdel.OUTCOME_OK
        assert _archived_calls(sink, delivery) == delivery["row_counts"]["calls"]
        # And the archiver's OWN completion test agrees -- the one in the
        # codebase, not a second opinion from this module.
        ledger = ArchiveLedger(report["state_dir"])
        assert reconcile_delivery(sink, ledger)["complete"] is True

    def test_the_declared_store_host_reaches_every_archived_row(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """"Where the snapshot was taken" is in the archive's identity block.

        The delivery is the only thing in the rail that knows it, which is why
        v1 carries it: without it every row archived through this loop would
        say the host was ``''``, silently.
        """
        delivery = _drop_a_delivery(drop_root, tmp_path)
        _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)
        rows = sink.select_rows(
            "calls",
            where={
                "store_id": STORE_ID,
                "snapshot_id": delivery["manifest"]["snapshot_id"],
            },
            columns=("store_host",),
        )
        assert rows
        assert {row["store_host"] for row in rows} == {"poplar"}

    def test_a_missing_file_inside_the_deadline_is_a_retry_not_a_failure(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """A FIRST mismatch means "still syncing".

        A sync client has reported a directory up to date while one of three
        files was absent on the consumer, and nothing guarantees it preserves
        the producer's write order -- so the marker can arrive BEFORE the
        bytes it describes. Alarm on the first mismatch and you alarm on every
        normal sync lag.
        """
        delivery = _drop_a_delivery(drop_root, tmp_path)
        (delivery["drop"] / "NOTES.txt").unlink()
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        tick = scanner.tick(now=NOW)
        assert tick["counts"] == {"retry": 1}
        report = tick["reports"][0]
        assert report["stage"] == "arrival"
        assert report["arrival"]["pending"] == ["NOTES.txt"]
        assert _archived_calls(sink, delivery) == 0

    def test_progress_is_persisted_so_a_retry_does_not_start_from_nothing(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """On a ~900 MB snapshot this is the whole cost of a tick."""
        delivery = _drop_a_delivery(drop_root, tmp_path)
        (delivery["drop"] / "NOTES.txt").unlink()
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        first = scanner.tick(now=NOW)["reports"][0]
        assert first["arrival"]["verified"] == ["council-snapshot.sqlite"]
        assert first["arrival"]["reused"] == []

        progress = cdel.DeliveryProgress(first["state_dir"])
        assert "council-snapshot.sqlite" in progress.verified()

        second = scanner.tick(now=NOW + timedelta(minutes=5))["reports"][0]
        # The snapshot was NOT re-digested: it came back from the persisted
        # progress.
        assert second["arrival"]["reused"] == ["council-snapshot.sqlite"]
        assert second["state"] == "retry"

    def test_the_same_state_past_the_deadline_fails_with_per_file_evidence(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """Only ELAPSED TIME distinguishes a sync lag from a lost file."""
        delivery = _drop_a_delivery(drop_root, tmp_path)
        (delivery["drop"] / "NOTES.txt").unlink()
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        assert scanner.tick(now=NOW)["counts"] == {"retry": 1}
        late = scanner.tick(now=NOW + timedelta(hours=7))
        assert late["counts"] == {"failed": 1}
        report = late["reports"][0]
        assert report["stage"] == "arrival"
        assert "NOTES.txt" in report["detail"]
        evidence = {entry["path"]: entry for entry in report["evidence"]}
        assert evidence["NOTES.txt"]["problem"] == "absent"
        assert evidence["NOTES.txt"]["expected_bytes"] > 0
        assert evidence["NOTES.txt"]["observed_bytes"] is None
        assert len(evidence["NOTES.txt"]["expected_sha256"]) == 64
        assert evidence["NOTES.txt"]["observed_sha256"] is None
        assert _archived_calls(sink, delivery) == 0

    def test_a_digest_mismatch_has_the_same_deadline_semantics(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """Present but WRONG is the same question as absent: has time run out?"""
        delivery = _drop_a_delivery(drop_root, tmp_path)
        notes = delivery["drop"] / "NOTES.txt"
        declared = next(
            entry
            for entry in delivery["manifest"]["files"]
            if entry["path"] == "NOTES.txt"
        )
        # Same SIZE, different bytes -- so only the digest catches it, which
        # is why the digest is in the inventory at all.
        notes.write_text("x" * declared["bytes"], encoding="utf-8")
        assert notes.stat().st_size == declared["bytes"]
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        assert scanner.tick(now=NOW)["counts"] == {"retry": 1}
        late = scanner.tick(now=NOW + timedelta(hours=7))["reports"][0]
        assert late["state"] == "failed"
        evidence = {entry["path"]: entry for entry in late["evidence"]}
        assert evidence["NOTES.txt"]["problem"] == "digest"
        assert evidence["NOTES.txt"]["observed_bytes"] == declared["bytes"]
        assert evidence["NOTES.txt"]["observed_sha256"] != declared["sha256"]
        assert len(evidence["NOTES.txt"]["observed_sha256"]) == 64

    def test_a_truncated_file_is_caught_on_size_before_being_digested(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        (delivery["drop"] / "NOTES.txt").write_text("", encoding="utf-8")
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        report = scanner.tick(now=NOW)["reports"][0]
        evidence = {entry["path"]: entry for entry in report["arrival"]["evidence"]}
        assert evidence["NOTES.txt"]["problem"] == "size"
        assert evidence["NOTES.txt"]["observed_bytes"] == 0
        assert evidence["NOTES.txt"]["observed_sha256"] is None


# --------------------------------------------------------------------------
# The frozen deadline -- the mtime-reset case
# --------------------------------------------------------------------------


class TestTheFrozenDeadline:
    def test_a_declared_deadline_is_recorded_and_sourced_to_the_manifest(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        (delivery["drop"] / "NOTES.txt").unlink()
        report = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)["reports"][0]
        assert report["arrival"]["deadline_source"] == "manifest"
        assert report["arrival"]["deadline_at"] == delivery["manifest"]["deadline_at"]

    def test_a_legacy_manifests_deadline_is_frozen_on_first_observation(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """CONSUMER-SIDE MTIME CAN RESET ON SYNC.

        Without freezing, every tick re-reads a fresh mtime, a 6-hour deadline
        NEVER expires, and a genuinely lost delivery waits forever while
        looking healthy. So: observe once (which freezes), then move the
        manifest's mtime far into the future the way a re-sync would, and the
        deadline must not move -- and the delivery must FAIL once the ORIGINAL
        deadline has passed.
        """
        delivery = _drop_a_delivery(drop_root, tmp_path, legacy=True)
        assert delivery["manifest"]["deadline_at"] is None
        (delivery["drop"] / "NOTES.txt").unlink()
        manifest_path = delivery["drop"] / cdel.MANIFEST_FILENAME
        # The manifest's mtime is "now" on this machine; pin it so the frozen
        # deadline is a value this test can name.
        first_seen = NOW
        os.utime(manifest_path, (first_seen.timestamp(), first_seen.timestamp()))

        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        first = scanner.tick(now=first_seen)["reports"][0]
        frozen = first["arrival"]["deadline_at"]
        assert first["arrival"]["deadline_source"] == "legacy-mtime"
        assert datetime.fromisoformat(frozen) == first_seen + cdel.DEFAULT_LEGACY_DEADLINE

        # THE RESET: the sync client rewrites the file and the mtime jumps.
        reset = first_seen + timedelta(hours=6)
        os.utime(manifest_path, (reset.timestamp(), reset.timestamp()))

        later = scanner.tick(now=first_seen + timedelta(hours=3))["reports"][0]
        assert later["arrival"]["deadline_at"] == frozen
        assert later["state"] == "retry"

        # Past the ORIGINAL deadline it FAILS -- which it would never do if
        # the deadline were recomputed from the reset mtime.
        past = scanner.tick(now=first_seen + timedelta(hours=7))["reports"][0]
        assert past["state"] == "failed"
        assert past["stage"] == "arrival"
        recomputed = reset + cdel.DEFAULT_LEGACY_DEADLINE
        assert first_seen + timedelta(hours=7) < recomputed  # i.e. it would still be waiting

    def test_the_frozen_record_survives_a_fresh_process(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """Persisted, not held in memory: the loop is restarted at will."""
        delivery = _drop_a_delivery(drop_root, tmp_path, legacy=True)
        (delivery["drop"] / "NOTES.txt").unlink()
        first = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)["reports"][0]
        frozen = first["arrival"]["deadline_at"]
        fresh = _scanner(drop_root, sink, state_root, tmp_path).tick(
            now=NOW + timedelta(hours=1)
        )["reports"][0]
        assert fresh["arrival"]["deadline_at"] == frozen


# --------------------------------------------------------------------------
# Two stages, persisted separately, retried independently
# --------------------------------------------------------------------------


class _FailingThenWorkingPublisher:
    """A publisher that fails once. The SUBJECT of the publish-retry tests.

    Stubbed because what is being tested is what the scanner does when
    publication FAILS -- the archiver stays REAL and every claim about
    archiving is a row count in the real sink.
    """

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, sink: Any, bundle: Any, **kwargs: Any) -> dict:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("the clearinghouse publish fell over")
        return {"campaign_id": bundle["campaign_id"], "published": True}


def _bundle_for(delivery_snapshot_id: str, tmp_path: Path) -> dict:
    """A REAL publication bundle, built by ``council_bundle.build_bundle``."""
    from kbutillib.domains.genome.ontology_mapping import council_bundle as cb
    from kbutillib.domains.identity.parameter_sets import parameter_set_hash

    bench = hashlib.sha256(b"bench").hexdigest()
    mapping = hashlib.sha256(b"mapping").hexdigest()
    parameters = {
        "query": "go_mf@4",
        "bench_fingerprint": bench,
        "mapping_hash": mapping,
    }
    plan = {
        "query": "go_mf@4",
        "bench_fingerprint": bench,
        "mapping_hash": mapping,
        "roster_fingerprint": hashlib.sha256(b"roster").hexdigest(),
        "parameter_set": parameters,
        "parameter_set_hash": parameter_set_hash(parameters),
        "plan_hash": hashlib.sha256(b"plan").hexdigest(),
        "source": "council/go_mf@4",
        "generated_at": "2026-10-25T12:00:00Z",
        "items_file": str(tmp_path / "scratch" / "items.jsonl"),
        "counts": {"abstain": 0, "fanout": 2},
    }
    return cb.build_bundle(
        plan,
        campaign_id="camp-delivery",
        store_id=STORE_ID,
        snapshot_id=delivery_snapshot_id,
        abstain=[],
        fanout=[
            {"query_id": hashlib.sha256(b"q:i1").hexdigest(), "item_id": "i1"},
            {"query_id": hashlib.sha256(b"q:i2").hexdigest(), "item_id": "i2"},
        ],
        job_ids=["job-1"],
        written_by="miles",
    )


class TestTheTwoStages:
    def test_a_delivery_with_no_bundle_records_publish_as_not_applicable(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """NOT as success. "nothing to publish" and "the assertion landed"
        are different facts, and conflating them is how a missing
        clearinghouse assertion reads as a finished delivery."""
        _drop_a_delivery(drop_root, tmp_path)
        report = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)["reports"][0]
        assert report["publish"]["state"] == cdel.OUTCOME_NOT_APPLICABLE
        assert report["publish"]["state"] != cdel.OUTCOME_OK
        assert report["state"] == "complete"

    def test_a_failed_publish_is_retried_and_the_archive_is_not_redone(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """PUBLISH ONLY. An archive receipt must not excuse a failed publish,
        and a publish retry must not re-archive 203,821 rows."""
        delivery = _drop_a_delivery(
            drop_root,
            tmp_path,
            bundle_factory=lambda snapshot_id: _bundle_for(snapshot_id, tmp_path),
        )
        publisher = _FailingThenWorkingPublisher(failures=1)
        scanner = _scanner(
            drop_root, sink, state_root, tmp_path, publisher=publisher
        )

        first = scanner.tick(now=NOW)["reports"][0]
        assert first["state"] == "failed"
        assert first["stage"] == cdel.STAGE_PUBLISH
        assert first["archive"]["state"] == cdel.OUTCOME_OK
        archived = _archived_calls(sink, delivery)
        assert archived == delivery["row_counts"]["calls"]

        second = scanner.tick(now=NOW + timedelta(minutes=5))["reports"][0]
        assert second["state"] == "complete"
        assert second["publish"]["state"] == cdel.OUTCOME_OK
        # The ARCHIVE was taken from its persisted outcome, not redone...
        assert second["archive"]["action"] == "cached"
        # ...and the authoritative check is the ROW COUNT, not a call count.
        assert _archived_calls(sink, delivery) == archived
        assert publisher.calls == 2

    def test_a_cached_publish_is_not_redone_either(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        _drop_a_delivery(
            drop_root,
            tmp_path,
            bundle_factory=lambda snapshot_id: _bundle_for(snapshot_id, tmp_path),
        )
        publisher = _FailingThenWorkingPublisher(failures=0)
        scanner = _scanner(drop_root, sink, state_root, tmp_path, publisher=publisher)
        assert scanner.tick(now=NOW)["counts"] == {"complete": 1}
        again = scanner.tick(now=NOW + timedelta(minutes=5))["reports"][0]
        assert again["publish"]["action"] == "cached"
        assert publisher.calls == 1

    def test_a_delivered_bundle_whose_digest_disagrees_is_refused(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """A bundle swapped for another WELL-FORMED one is still the wrong one."""
        delivery = _drop_a_delivery(
            drop_root,
            tmp_path,
            bundle_factory=lambda snapshot_id: _bundle_for(snapshot_id, tmp_path),
        )
        manifest_path = delivery["drop"] / cdel.MANIFEST_FILENAME
        content = cdel.read_manifest(manifest_path)
        content["bundle"] = {**content["bundle"], "bundle_digest": "f" * 64}
        cdel.write_manifest(content, manifest_path)
        # The manifest changed, so the inventory it declares for ITSELF is
        # untouched but its identity moved: re-measure and rewrite so arrival
        # still passes and the BUNDLE check is what fails.
        report = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)["reports"][0]
        assert report["state"] == "failed"
        assert report["stage"] == cdel.STAGE_PUBLISH
        assert "is not the" in report["publish"]["detail"]

    def test_the_real_publisher_runs_end_to_end(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """No stub at all: the REAL lakehouse-sourced publisher, after archive.

        It refuses here because the delivered jobs carry no disposition --
        which is the right answer and proves the scanner is wired to the real
        gates rather than to something that always says yes.
        """
        pytest.importorskip("pyarrow")
        delivery = _drop_a_delivery(
            drop_root,
            tmp_path,
            bundle_factory=lambda snapshot_id: _bundle_for(snapshot_id, tmp_path),
        )
        report = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)["reports"][0]
        assert report["archive"]["state"] == cdel.OUTCOME_OK
        assert _archived_calls(sink, delivery) == delivery["row_counts"]["calls"]
        assert report["publish"]["state"] == cdel.OUTCOME_FAILED
        assert "job-1" in report["publish"]["detail"]


# --------------------------------------------------------------------------
# The receipt is a CACHE, never an authority
# --------------------------------------------------------------------------


class TestTheReceiptIsACache:
    def test_deleting_the_receipt_reproduces_it_without_re_archiving(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """RE-EXPORTED from the load ledger, never recomputed by re-archiving.

        Two receipts for one delivery that disagreed would be worse than none,
        which is why ``council_archive.receipt`` builds from the delivery
        binding and the reconciliation that established it.
        """
        delivery = _drop_a_delivery(drop_root, tmp_path)
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        first = scanner.tick(now=NOW)["reports"][0]
        before = _archived_calls(sink, delivery)
        receipt_before = dict(first["archive"]["receipt"])

        outcomes = cdel.DeliveryOutcomes(first["state_dir"])
        outcomes.path.unlink()

        second = scanner.tick(now=NOW + timedelta(minutes=5))["reports"][0]
        assert second["state"] == "complete"
        receipt_after = dict(second["archive"]["receipt"])
        # Identical modulo when it was issued -- a receipt says WHAT
        # reconciled, and re-exporting it says so again.
        assert {k: v for k, v in receipt_after.items() if k != "issued_at"} == {
            k: v for k, v in receipt_before.items() if k != "issued_at"
        }
        assert receipt_after["complete"] is True
        # NOT RE-ARCHIVED: the row count is the authoritative statement.
        assert _archived_calls(sink, delivery) == before

    def test_a_receipt_under_another_identity_is_rejected_not_trusted(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """One left by a different delivery would certify work nobody did."""
        delivery = _drop_a_delivery(drop_root, tmp_path)
        state_dir = cdel.CouncilDeliveryScanner(
            drop_dir=drop_root,
            scratch_dir=tmp_path / "scratch",
            sink=sink,
            state_root=state_root,
        ).state_dir_for(delivery["manifest"])
        outcomes = cdel.DeliveryOutcomes(state_dir)
        outcomes.record(
            cdel.STAGE_ARCHIVE,
            identity="d" * 64,
            state=cdel.OUTCOME_OK,
            receipt={"complete": True, "note": "a lie from another delivery"},
        )
        with pytest.raises(cdel.ReceiptIdentityError):
            outcomes.require(cdel.STAGE_ARCHIVE, identity=delivery["digest"])
        assert outcomes.read(identity=delivery["digest"])["rejected"] == [
            cdel.STAGE_ARCHIVE
        ]

        report = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)["reports"][0]
        assert [item["stage"] for item in report["rejected_receipts"]] == [
            cdel.STAGE_ARCHIVE
        ]
        # The stage was RE-RUN rather than skipped, and the rows landed ONCE:
        # re-running is safe because the archiver resolves an already-landed
        # batch from its load_batch_id instead of appending it twice.
        assert report["archive"]["state"] == cdel.OUTCOME_OK
        assert report["archive"]["receipt"]["complete"] is True
        assert _archived_calls(sink, delivery) == delivery["row_counts"]["calls"]

    def test_a_receipt_for_an_edited_manifest_stops_counting(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        first = scanner.tick(now=NOW)["reports"][0]
        assert first["rejected_receipts"] == []
        manifest_path = delivery["drop"] / cdel.MANIFEST_FILENAME
        content = cdel.read_manifest(manifest_path)
        content["delivery_id"] = "delivery-1-edited"
        cdel.write_manifest(content, manifest_path)
        second = scanner.tick(now=NOW + timedelta(minutes=5))["reports"][0]
        # EVERY cached outcome stops counting, not just the archive's: the
        # identity they were established for no longer describes this
        # delivery.
        assert {item["stage"] for item in second["rejected_receipts"]} == {
            cdel.STAGE_ARCHIVE,
            cdel.STAGE_PUBLISH,
        }
        assert _archived_calls(sink, delivery) == delivery["row_counts"]["calls"]


# --------------------------------------------------------------------------
# The scanner SCANS: it reconciles disk against the lakehouse
# --------------------------------------------------------------------------


class TestTheScannerScans:
    def test_a_fresh_scanner_completes_exactly_the_incomplete_delivery(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """RECONCILE, NEVER REACT. The fresh instance saw NEITHER arrival.

        kbhub has no user systemd, so this loop is restarted at arbitrary
        points; an event-driven design is wrong in a way that is invisible in
        testing and shows up as a run that silently never loaded.
        """
        done = _drop_a_delivery(drop_root, tmp_path, name="delivery-done")
        first = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)
        assert first["counts"] == {"complete": 1}

        # snapshot_seq 2, because this is the SAME store's next snapshot: the
        # archiver refuses a second snapshot_id at a sequence the store has
        # already used (``SnapshotConflictError``), and an explicit monotonic
        # sequence is the only thing that orders two digests.
        pending = _drop_a_delivery(
            drop_root, tmp_path, name="delivery-pending", snapshot_seq=2
        )
        # A FRESH instance -- nothing carried over in memory.
        fresh = _scanner(drop_root, sink, state_root, tmp_path)
        tick = fresh.tick(now=NOW + timedelta(minutes=5))
        assert tick["deliveries"] == 2
        assert tick["counts"] == {"complete": 2}
        actions = {
            report["delivery_id"]: report["archive"].get("action")
            for report in tick["reports"]
        }
        assert actions["delivery-done"] == "cached"
        assert actions["delivery-pending"] is None
        assert _archived_calls(sink, done) == done["row_counts"]["calls"]
        assert _archived_calls(sink, pending) == pending["row_counts"]["calls"]

    def test_a_directory_with_no_manifest_is_not_a_delivery(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        (drop_root / "half-synced").mkdir()
        (drop_root / "half-synced" / "snapshot.sqlite").write_bytes(b"not yet")
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        assert scanner.deliveries() == []
        assert scanner.tick(now=NOW)["deliveries"] == 0

    def test_one_bad_delivery_does_not_stop_the_good_ones(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """A scanner that unwound on one bad delivery would stop completing
        the good ones, which is the opposite of reconciling."""
        good = _drop_a_delivery(drop_root, tmp_path, name="delivery-good")
        bad = drop_root / "delivery-bad"
        bad.mkdir()
        (bad / cdel.MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
        tick = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)
        assert tick["counts"] == {"complete": 1, "failed": 1}
        assert _archived_calls(sink, good) == good["row_counts"]["calls"]

    def test_the_loop_runs_a_bounded_number_of_ticks_without_sleeping_for_real(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        _drop_a_delivery(drop_root, tmp_path)
        slept: list[float] = []
        ticks = _scanner(drop_root, sink, state_root, tmp_path).run(
            max_ticks=2, sleep=slept.append
        )
        assert len(ticks) == 2
        # One sleep BETWEEN the two ticks, and none after the last: a bounded
        # run must not pay its own poll interval on the way out.
        assert slept == [1]

    def test_the_four_settings_are_explicit_and_not_guessed(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        assert scanner.drop_dir == drop_root
        assert scanner.state_root == state_root
        assert scanner.scratch_dir == tmp_path / "scratch"
        assert scanner.poll_seconds == 1
        assert cdel.DEFAULT_POLL_SECONDS == 300

    def test_the_state_directory_is_the_archivers_own(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        """Composed with, not shadowed: one delivery, one state directory."""
        from kbutillib.domains.kbase.berdl.council_archive import (
            DELIVERY_FILENAME,
            LEDGER_FILENAME,
        )

        delivery = _drop_a_delivery(drop_root, tmp_path)
        report = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)["reports"][0]
        state_dir = Path(report["state_dir"])
        assert state_dir == state_root / STORE_ID / delivery["manifest"]["snapshot_id"]
        present = {path.name for path in state_dir.iterdir()}
        assert {DELIVERY_FILENAME, LEDGER_FILENAME} <= present
        assert {cdel.PROGRESS_FILENAME, cdel.OUTCOMES_FILENAME} <= present


# --------------------------------------------------------------------------
# Concurrency: two REAL instances, one drop directory, one sink file
# --------------------------------------------------------------------------


class TestConcurrency:
    def test_two_concurrent_scanners_do_not_double_archive(
        self, drop_root: Path, state_root: Path, tmp_path: Path
    ) -> None:
        """Two real instances in two threads, each with its own connection to
        ONE sink file. The claim is a ROW COUNT, not a call count.

        The archiver refuses to double-WRITE a batch, but that is a refusal
        (``AmbiguousBatchError``) and not a plan -- so the two instances must
        not start the same delivery at the same instant, which is what the
        O_EXCL claim in the state directory buys.
        """
        delivery = _drop_a_delivery(drop_root, tmp_path)
        lake = tmp_path / "lake" / "council.sqlite"
        start = threading.Barrier(2)
        results: list[Any] = [None, None]

        def _work(index: int) -> None:
            own_sink = SqliteCouncilSink(lake)
            own_sink.bootstrap()
            try:
                scanner = cdel.CouncilDeliveryScanner(
                    drop_dir=drop_root,
                    scratch_dir=tmp_path / f"scratch-{index}",
                    sink=own_sink,
                    state_root=state_root,
                    poll_seconds=1,
                    holder=f"thread-{index}",
                )
                start.wait(timeout=30)
                results[index] = scanner.tick(now=NOW)
            except BaseException as exc:  # noqa: BLE001 - reported, then asserted
                results[index] = exc
            finally:
                own_sink.close()

        threads = [threading.Thread(target=_work, args=(i,)) for i in (0, 1)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        for result in results:
            assert not isinstance(result, BaseException), result
        states = sorted(report["state"] for tick in results for report in tick["reports"])
        # One of them did the work; the other either found it locked or found
        # it already done. Both are correct outcomes; "both archived" is not.
        assert states in (["complete", "locked"], ["complete", "complete"])

        verify = SqliteCouncilSink(lake)
        try:
            counts = {
                table: verify.count_rows(
                    table,
                    where={
                        "store_id": STORE_ID,
                        "snapshot_id": delivery["manifest"]["snapshot_id"],
                    },
                )
                for table in ("calls", "items", "jobs", "attempts")
            }
        finally:
            verify.close()
        assert counts["calls"] == delivery["row_counts"]["calls"]
        assert counts["items"] == delivery["row_counts"]["items"]
        assert counts["jobs"] == delivery["row_counts"]["jobs"]
        assert counts["attempts"] == delivery["row_counts"]["attempts"]

    def test_the_lock_is_exclusive_and_the_loser_is_told_so(
        self, tmp_path: Path
    ) -> None:
        first = cdel.DeliveryLock(tmp_path / "state", holder="a")
        second = cdel.DeliveryLock(tmp_path / "state", holder="b")
        assert first.acquire() is True
        assert second.acquire() is False
        first.release()
        assert second.acquire() is True
        second.release()

    def test_a_stale_lock_is_reclaimed_by_age_and_never_by_pid(
        self, tmp_path: Path
    ) -> None:
        """The pod restarts mid-tick, which is the normal case here.

        Reclaimed by AGE: a pid on a restarted pod can be alive and be
        somebody else entirely.
        """
        abandoned = cdel.DeliveryLock(tmp_path / "state", holder="dead")
        assert abandoned.acquire() is True
        old = (datetime.now(tz=timezone.utc) - timedelta(hours=9)).timestamp()
        os.utime(abandoned.path, (old, old))
        taker = cdel.DeliveryLock(tmp_path / "state", holder="live")
        assert taker.acquire(stale_after=timedelta(hours=2)) is True
        taker.release()

    def test_a_locked_delivery_is_picked_up_on_the_next_tick(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        scanner = _scanner(drop_root, sink, state_root, tmp_path)
        held = cdel.DeliveryLock(
            scanner.state_dir_for(delivery["manifest"]), holder="other"
        )
        assert held.acquire() is True
        blocked = scanner.tick(now=NOW)
        assert blocked["counts"] == {"locked": 1}
        assert _archived_calls(sink, delivery) == 0
        held.release()
        assert scanner.tick(now=NOW + timedelta(minutes=5))["counts"] == {"complete": 1}
        assert _archived_calls(sink, delivery) == delivery["row_counts"]["calls"]


# --------------------------------------------------------------------------
# The operator step, which is a NAMED GAP and not an invention
# --------------------------------------------------------------------------


class TestTheOperatorStep:
    def test_the_registration_command_is_declared_to_be_unknown(self) -> None:
        """A loop in a repository is not an installed restartable service.

        Nothing in this tree names kbhub's Jupyter startup facility, so the
        registration command is NOT invented: a wrong one reads exactly like
        a right one until the pod restarts and nothing comes back.
        """
        gap = cdel.OPERATOR_INSTALL["GAP"]
        assert "NOT determinable from this repository" in gap.replace(
            "NOT DETERMINABLE FROM THIS REPOSITORY", "NOT determinable from this repository"
        )
        assert "Albert" in gap

    def test_no_systemd_unit_is_proposed_anywhere(self) -> None:
        """kbhub has no user systemd. That is a fact, not a preference."""
        source = Path(cdel.__file__).read_text(encoding="utf-8")
        assert "[Unit]" not in source
        assert "systemctl" not in source
        assert any("NO user systemd" in line for line in cdel.OPERATOR_INSTALL["known"])

    def test_the_evidence_of_restart_recovery_is_written_down(self) -> None:
        evidence = cdel.OPERATOR_INSTALL["evidence_of_restart_recovery"]
        assert len(evidence) >= 3
        assert any("FRESH instance" in item for item in evidence)
        assert any("row count must not change" in item for item in evidence)


# --------------------------------------------------------------------------
# NO-INJECTION: the loop through the REAL CLI, exit codes and all
# --------------------------------------------------------------------------


class TestTheCli:
    """``kbu council delivery-scan`` is what a registered routine invokes.

    One tick by default, so an operator can run exactly one reconciliation
    pass by hand; ``--ticks 0`` is the forever form a startup routine uses.
    """

    @staticmethod
    def _invoke(*args: str):
        from click.testing import CliRunner

        from kbutillib.cli import main

        return CliRunner().invoke(main, ["council", *args])

    @staticmethod
    def _payload(result) -> dict:
        for line in reversed(result.output.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise AssertionError(f"no JSON envelope in output: {result.output!r}")

    def test_one_tick_through_the_cli_archives_a_complete_delivery(
        self, drop_root: Path, tmp_path: Path, state_root: Path
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        lake = tmp_path / "lake" / "council.sqlite"
        result = self._invoke(
            "delivery-scan",
            "--drop-dir",
            str(drop_root),
            "--scratch-dir",
            str(tmp_path / "scratch"),
            "--lakehouse",
            str(lake),
            "--state-root",
            str(state_root),
            "--ticks",
            "1",
            "--json",
        )
        assert result.exit_code == 0, result.output
        data = self._payload(result)["data"]
        assert data["ticks"] == 1
        assert data["counts"] == {"complete": 1}
        verify = SqliteCouncilSink(lake)
        try:
            assert (
                verify.count_rows(
                    "calls",
                    where={
                        "store_id": STORE_ID,
                        "snapshot_id": delivery["manifest"]["snapshot_id"],
                    },
                )
                == delivery["row_counts"]["calls"]
            )
        finally:
            verify.close()

    def test_a_failed_delivery_is_reported_as_a_warning_not_hidden(
        self, drop_root: Path, tmp_path: Path, state_root: Path
    ) -> None:
        """The envelope contract: ``warnings`` is never empty for a degraded
        answer, and a lost delivery is the most degraded answer there is."""
        bad = drop_root / "delivery-bad"
        bad.mkdir()
        (bad / cdel.MANIFEST_FILENAME).write_text("{not json", encoding="utf-8")
        result = self._invoke(
            "delivery-scan",
            "--drop-dir",
            str(drop_root),
            "--scratch-dir",
            str(tmp_path / "scratch"),
            "--lakehouse",
            str(tmp_path / "lake" / "council.sqlite"),
            "--state-root",
            str(state_root),
            "--json",
        )
        assert result.exit_code == 0, result.output
        envelope = self._payload(result)
        assert envelope["data"]["counts"] == {"failed": 1}
        assert envelope["warnings"]
        assert "manifest" in envelope["warnings"][0]


class TestTheLakehouseIsTheAuthority:
    """A cached ``ok`` is re-reconciled against the tables on every tick.

    "Correctness must come from reconciling disk against the lakehouse, NEVER
    from having seen an event" applies to the receipt too: one that matches
    its manifest while the rows are gone is exactly the state that would
    otherwise be invisible.
    """

    def test_a_cached_receipt_whose_rows_are_gone_is_reported_not_trusted(
        self, drop_root: Path, sink, state_root: Path, tmp_path: Path
    ) -> None:
        delivery = _drop_a_delivery(drop_root, tmp_path)
        first = _scanner(drop_root, sink, state_root, tmp_path).tick(now=NOW)["reports"][0]
        assert first["state"] == "complete"

        # A DIFFERENT namespace, holding nothing -- the same state directory,
        # the same receipt, the same manifest identity.
        empty = SqliteCouncilSink(tmp_path / "other-lake" / "council.sqlite")
        empty.bootstrap()
        try:
            scanner = cdel.CouncilDeliveryScanner(
                drop_dir=drop_root,
                scratch_dir=tmp_path / "scratch",
                sink=empty,
                state_root=state_root,
                poll_seconds=1,
            )
            report = scanner.tick(now=NOW + timedelta(minutes=5))["reports"][0]
            assert report["state"] == "failed"
            assert report["stage"] == cdel.STAGE_ARCHIVE
            assert report["archive"]["action"] == "receipt-disagrees-with-lakehouse"
            assert "CACHE, never an authority" in report["archive"]["detail"]
            # And nothing was re-appended into the empty namespace: the
            # ledger says every batch committed and there is no rollback.
            assert empty.count_rows("calls") == 0
        finally:
            empty.close()
        # The original namespace is untouched.
        assert _archived_calls(sink, delivery) == delivery["row_counts"]["calls"]
