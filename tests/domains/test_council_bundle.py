"""The PUBLICATION BUNDLE -- ``...ontology_mapping.council_bundle``.

THE INPUTS ARE REAL SHAPES, NOT INVENTED DICTS. The fanout and abstain rows
here are the plan's own record shapes, and every one of them is fed to BOTH
``council_bundle.build_bundle`` AND the real
``verdicts.export_rows`` -- so "the bundle represents the same submitted set
the export publishes" is asserted against the exporter rather than asserted
in prose. A bundle that quietly disagreed with the exporter about which
``query_id``s were submitted would be the whole defect this artifact exists
to prevent.

THE INCOMPLETE-SNAPSHOT REFUSAL USES A REAL INTERRUPTED ARCHIVE. The ledger
it reads is the one left behind by ``council_archive.archive_snapshot``
crashing between ``calls`` and ``attempts`` on a REAL snapshot of a REAL
council store -- not a completeness flag constructed by the test. A
hand-written ledger would prove that the bundle reads a field, not that the
field means what the archiver writes.

NOT A CLAIM ABOUT A LIVE POD (KBUtilLib#91). The lakehouse copy is written
through the local SQL sink; live acceptance is operator-owned.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip(
    "sqlalchemy",
    reason="the council store needs the `council` extra: pip install 'kbutillib[council]'",
)

from kbutillib.domains.ai.council.store.sqlite import SqliteStore  # noqa: E402
from kbutillib.domains.genome.ontology_mapping import verdicts as vx  # noqa: E402
from kbutillib.domains.genome.ontology_mapping.council_bundle import (  # noqa: E402
    BUNDLE_CONTENT_FIELDS,
    BUNDLE_FILENAME,
    BUNDLE_SCHEMA_VERSION,
    PLAN_ORIGIN_FIELDS,
    PLAN_ORIGIN_KINDS,
    PUBLICATION_BUNDLE_TABLE,
    BundleConflictError,
    BundleDigestError,
    BundleIncompleteSnapshotError,
    JobNotInBundleError,
    PlanOriginError,
    PublicationBundleError,
    abstentions,
    bundle_plan_origin,
    build_bundle,
    build_bundle_from_run,
    bundle_query_ids,
    canonical_json,
    content_digest,
    fanout_map,
    ordered_job_ids,
    publish_bundle,
    read_bundle,
    read_lakehouse_bundle,
    require_bundle_listed,
    require_publishable,
    write_bundle,
)
from kbutillib.domains.kbase.berdl import council_disposition as cd  # noqa: E402
from kbutillib.domains.kbase.berdl.council_archive import (  # noqa: E402
    archive_snapshot,
)
from kbutillib.domains.kbase.berdl.council_sink import SqliteCouncilSink  # noqa: E402

STORE_ID = "poplar/council-1"
CAMPAIGN = "adp1-pmi-2026-10"
MAPPING_HASH = "e" * 64
ROSTER_FINGERPRINT = "r" * 64
PARAMETER_SET_HASH = "p" * 64
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)

#: The exporter's declared base jobs and retry pass, in the order that
#: resolves winners. Deliberately NOT sorted and carrying a repeat, so
#: "preserved exactly as declared" is testable.
BASE_JOBS = ["chunk10", "chunk02", "chunk01"]
RETRY_JOBS = ["retry2", "retry1"]


def _hash(n: int) -> str:
    """A 64-char lowercase-hex query_id, distinct per ``n``.

    Same helper, same shape, as tests/domains/test_verdict_export.py.
    """
    return f"{n:064x}"


def _abstain(n: int, text: str, reason: str) -> dict:
    """A plan ``abstain_file`` row -- the shape export_rows consumes."""
    return {
        "query_id": _hash(n),
        "text": text,
        "source": "RAST",
        "abstain_reason": reason,
        "decline_reason": f"abstained:{reason}",
    }


def _fanout(n: int, item_id: str, text: str) -> dict:
    """A plan ``fanout_file`` row -- the shape export_rows consumes."""
    return {"query_id": _hash(n), "item_id": item_id, "text": text}


#: THE FANOUT THAT MATTERS: item-1 answers THREE query_ids (three distinct
#: clearinghouse keys sharing one definition text, answered once and fanned
#: back out), item-2 answers one. The clearinghouse key set is therefore NOT
#: derivable from council.items, which is the first of the three reasons this
#: bundle exists.
FANOUT = [
    _fanout(1, "item-1", "alpha synthase"),
    _fanout(2, "item-1", "alpha synthase"),
    _fanout(3, "item-1", "alpha synthase"),
    _fanout(4, "item-2", "beta transferase"),
]

#: Retrieval abstentions: declined BEFORE anything was submitted, so they
#: have no council item at all.
ABSTAIN = [
    _abstain(90, "hypothetical protein", "no_candidates"),
    _abstain(91, "DUF1234 family protein", "no_candidates"),
]


def _plan(**extra) -> dict:
    """A PLAN.json-shaped record carrying what the bundle reads off it."""
    plan = {
        "schema_version": 1,
        "generated_at": "2026-10-22T09:00:00Z",
        "run_id": "run-1",
        "query": "go_mf@4",
        "source": "RAST",
        "mapping_hash": MAPPING_HASH,
        "roster_fingerprint": ROSTER_FINGERPRINT,
        "parameter_set": {
            "query": "go_mf@4",
            "bench_fingerprint": "b" * 64,
            "mapping_hash": MAPPING_HASH,
        },
        "parameter_set_hash": PARAMETER_SET_HASH,
        "plan_hash": "h" * 64,
        "counts": {"abstain": len(ABSTAIN), "fanout": len(FANOUT)},
    }
    plan.update(extra)
    return plan


def _bundle(**extra) -> dict:
    kwargs = {
        "campaign_id": CAMPAIGN,
        "store_id": STORE_ID,
        "snapshot_id": "a" * 64,
        "abstain": ABSTAIN,
        "fanout": FANOUT,
        "job_ids": BASE_JOBS,
        "retry_job_ids": RETRY_JOBS,
        "written_by": "miles",
        "written_at": NOW.isoformat(),
    }
    kwargs.update(extra)
    return build_bundle(_plan(), **kwargs)


# --------------------------------------------------------------------------
# A real store, a real snapshot, a real archive -- for the snapshot gate
# --------------------------------------------------------------------------


def _build_store(path: Path) -> SqliteStore:
    """The smallest REAL council store that archives to a real delivery."""
    store = SqliteStore(str(path))
    store.init_schema()
    store.assign_store_identity(STORE_ID, declared_by="miles")
    store.put_query_type(
        name="go_mf",
        version="4",
        spec={"roles": {"proposer": {"system": "You assign GO terms."}}},
        created_by="miles",
    )
    store.create_job(
        {
            "job_id": "chunk01",
            "query_name": "go_mf",
            "query_version": "4",
            "submitted_by": "miles",
            "submitted_at": NOW,
            "status": "done",
        },
        [{"item_id": "item-1"}, {"item_id": "item-2"}],
        [
            {
                "call_id": "c1",
                "job_id": "chunk01",
                "item_id": "item-1",
                "role": "proposer",
                "seat_name": "p1",
                "endpoint": "argo",
                "endpoint_kind": "openai",
                "submitted_by": "miles",
                "created_at": NOW,
            }
        ],
    )
    store.record_attempt(
        "c1",
        1,
        started_at=NOW,
        finished_at=NOW,
        outcome_kind="ok",
        usage={"input_tokens": 1},
        worker_id="w1",
    )
    store.register_worker({"worker_id": "w1", "user": "local", "host": "poplar"})
    return store


@pytest.fixture
def store(tmp_path: Path):
    opened = _build_store(tmp_path / "store" / "council.sqlite")
    try:
        yield opened
    finally:
        opened.close()


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


@pytest.fixture
def complete_snapshot(store, sink, tmp_path, state_root) -> dict:
    """A snapshot whose delivery really COMPLETED, through the real archiver."""
    manifest = store.snapshot(tmp_path / "snaps" / "good.sqlite")
    report = archive_snapshot(
        sink, manifest, snapshot_seq=1, state_root=state_root
    )
    assert report["complete"] is True
    return manifest


@pytest.fixture
def incomplete_snapshot(store, sink, tmp_path, state_root) -> dict:
    """A snapshot whose delivery was INTERRUPTED by the real archiver.

    The ledger this leaves is the one the bundle's refusal reads. It is not
    constructed here: `attempts` is failed by a table writer, so `calls`
    commits and `attempts` does not -- the exact shape of a crash between two
    independent Iceberg commits.
    """
    manifest = store.snapshot(tmp_path / "snaps" / "torn.sqlite")

    def _fail_on_attempts(inner_sink, table, rows):
        if table == "attempts":
            raise RuntimeError("simulated crash before committing 'attempts'")
        return inner_sink.append_rows(table, rows)

    with pytest.raises(RuntimeError, match="simulated crash"):
        archive_snapshot(
            sink,
            manifest,
            snapshot_seq=1,
            state_root=state_root,
            table_writer=_fail_on_attempts,
        )
    return manifest


# --------------------------------------------------------------------------
# The content and its digest
# --------------------------------------------------------------------------


class TestTheContent:
    def test_the_bundle_carries_every_declared_field(self) -> None:
        bundle = _bundle()
        assert set(bundle) == set(BUNDLE_CONTENT_FIELDS) | {"bundle_digest"}
        assert bundle["schema_version"] == BUNDLE_SCHEMA_VERSION

    def test_it_carries_the_plan_content_whole(self) -> None:
        bundle = _bundle()
        assert bundle["plan"] == _plan()
        assert bundle["query"] == "go_mf@4"
        assert bundle["query_name"] == "go_mf"
        assert bundle["query_version"] == "4"

    def test_it_carries_the_canonical_parameter_set_INCLUDING_mapping_hash(
        self,
    ) -> None:
        """The hash is what partitions the corpus; a bundle naming the set
        without it could not be checked against the parameter_set row."""
        bundle = _bundle()
        assert bundle["parameter_set"]["mapping_hash"] == MAPPING_HASH
        assert bundle["mapping_hash"] == MAPPING_HASH
        assert bundle["parameter_set_hash"] == PARAMETER_SET_HASH

    def test_it_is_bound_to_a_store_and_a_snapshot(self) -> None:
        bundle = _bundle()
        assert bundle["store_id"] == STORE_ID
        assert bundle["snapshot_id"] == "a" * 64

    @pytest.mark.parametrize(
        "blank", ["campaign_id", "store_id", "snapshot_id"]
    )
    def test_a_blank_binding_is_refused(self, blank: str) -> None:
        with pytest.raises(PublicationBundleError) as excinfo:
            _bundle(**{blank: ""})
        assert blank in str(excinfo.value)

    def test_the_submitted_inventory_counts_what_was_SENT(self) -> None:
        """Reconciliation is against SUBMITTED, not against what happened to
        be exported, so an item that produced nothing is a visible gap."""
        bundle = _bundle()
        assert bundle["submitted"]["counts"] == {
            "fanout": 4,
            "abstained": 2,
            "inputs": 6,
            "items": 2,
        }
        assert len(bundle["submitted"]["query_ids"]) == 6


class TestTheDigest:
    def test_the_bundle_round_trips_and_its_digest_verifies(
        self, tmp_path: Path
    ) -> None:
        bundle = _bundle()
        written = write_bundle(bundle, tmp_path / BUNDLE_FILENAME)
        assert written["written"] is True
        read_back = read_bundle(tmp_path / BUNDLE_FILENAME)
        assert read_back == bundle
        assert content_digest(read_back) == read_back["bundle_digest"]

    def test_the_digest_covers_the_whole_content(self) -> None:
        first = _bundle()
        second = _bundle(job_ids=["chunk01"])
        assert first["bundle_digest"] != second["bundle_digest"]

    def test_the_digest_does_not_depend_on_dict_order(self) -> None:
        bundle = _bundle()
        shuffled = {key: bundle[key] for key in reversed(list(bundle))}
        assert content_digest(shuffled) == content_digest(bundle)

    def test_canonicalisation_does_NOT_sort_the_job_list(self) -> None:
        """sort_keys sorts KEYS, never list elements -- which is what makes
        the declared winner order survive canonicalisation."""
        text = canonical_json(_bundle())
        assert '"job_ids":["chunk10","chunk02","chunk01"]' in text

    def test_an_EDITED_bundle_fails_its_digest_on_read(self, tmp_path: Path) -> None:
        path = tmp_path / BUNDLE_FILENAME
        write_bundle(_bundle(), path)
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["ordered_job_ids"] = ["chunk01"]
        path.write_text(canonical_json(tampered), encoding="utf-8")
        with pytest.raises(BundleDigestError) as excinfo:
            read_bundle(path)
        assert "not the artifact its digest names" in str(excinfo.value)

    def test_writing_a_bundle_with_a_wrong_declared_digest_is_refused(
        self, tmp_path: Path
    ) -> None:
        bundle = dict(_bundle())
        bundle["bundle_digest"] = "0" * 64
        with pytest.raises(BundleDigestError):
            write_bundle(bundle, tmp_path / BUNDLE_FILENAME)
        assert not (tmp_path / BUNDLE_FILENAME).exists()


# --------------------------------------------------------------------------
# The fanout, against the REAL exporter
# --------------------------------------------------------------------------


class TestTheFanout:
    def test_one_item_mapping_to_several_query_ids_is_represented_completely(
        self,
    ) -> None:
        mapping = fanout_map(_bundle())
        assert mapping["item-1"] == [_hash(1), _hash(2), _hash(3)]
        assert mapping["item-2"] == [_hash(4)]

    def test_every_query_id_is_reconstructable_from_the_map(self) -> None:
        bundle = _bundle()
        reconstructed = {
            query_id
            for query_ids in fanout_map(bundle).values()
            for query_id in query_ids
        }
        assert reconstructed == {_hash(n) for n in (1, 2, 3, 4)}

    def test_the_bundle_and_the_REAL_exporter_agree_on_the_submitted_set(
        self,
    ) -> None:
        """The assertion that matters: the same two records through
        export_rows and through build_bundle describe one submitted set."""
        built = vx.export_rows(
            abstain=ABSTAIN,
            fanout=FANOUT,
            verdicts={
                "item-1": {
                    "status": "ok",
                    "verdict": {"selected_ids": ["GO:0003824"]},
                    "decided_at": NOW.isoformat(),
                    "job_id": "chunk01",
                },
                "item-2": {
                    "status": "ok",
                    "verdict": {"selected_ids": []},
                    "decided_at": NOW.isoformat(),
                    "job_id": "retry1",
                },
            },
            identity={
                "query_name": "go_mf",
                "query_version": "4",
                "roster_fingerprint": ROSTER_FINGERPRINT,
                "mapping_hash": MAPPING_HASH,
            },
            job_ids=BASE_JOBS,
            retry_job_ids=RETRY_JOBS,
            abstained_at="2026-10-22T09:00:00Z",
            store_id=STORE_ID,
        )
        exported = {row["query_id"] for row in built["rows"]}
        bundle = _bundle()
        assert exported == set(bundle_query_ids(bundle))
        assert built["counts"]["inputs"] == bundle["submitted"]["counts"]["inputs"]

    def test_the_exporter_publishes_three_rows_from_the_one_shared_item(
        self,
    ) -> None:
        """Why the map is needed at all: council.items holds ONE item here and
        the clearinghouse gets three keys."""
        built = vx.export_rows(
            abstain=[],
            fanout=FANOUT[:3],
            verdicts={
                "item-1": {
                    "status": "ok",
                    "verdict": {"selected_ids": ["GO:0003824"]},
                    "decided_at": NOW.isoformat(),
                    "job_id": "chunk01",
                }
            },
            identity={
                "query_name": "go_mf",
                "query_version": "4",
                "roster_fingerprint": ROSTER_FINGERPRINT,
                "mapping_hash": MAPPING_HASH,
            },
            job_ids=["chunk01"],
            store_id=STORE_ID,
        )
        assert len(built["rows"]) == 3
        assert {row["council"]["item_id"] for row in built["rows"]} == {"item-1"}
        assert len(fanout_map(_bundle())["item-1"]) == 3

    def test_a_fanout_row_with_no_item_id_is_refused(self) -> None:
        with pytest.raises(PublicationBundleError) as excinfo:
            _bundle(fanout=[{"query_id": _hash(1), "text": "x"}])
        assert "item_id" in str(excinfo.value)

    def test_a_query_id_in_BOTH_partitions_is_refused(self) -> None:
        """They are disjoint by construction, so an overlap means two plans."""
        with pytest.raises(PublicationBundleError) as excinfo:
            _bundle(
                abstain=[_abstain(1, "x", "no_candidates")],
                fanout=[_fanout(1, "item-1", "x")],
            )
        assert "both" in str(excinfo.value)


# --------------------------------------------------------------------------
# The abstentions
# --------------------------------------------------------------------------


class TestTheAbstentions:
    def test_they_appear_with_their_decline_reasons_and_NO_item_id(self) -> None:
        rows = abstentions(_bundle())
        assert len(rows) == 2
        for row in rows:
            assert row["item_id"] is None
            assert row["decline_reason"] == "abstained:no_candidates"
            assert row["abstain_reason"] == "no_candidates"

    def test_the_item_id_key_is_PRESENT_and_null_not_absent(self) -> None:
        """Saying "no council item" explicitly is the point of carrying them."""
        for row in abstentions(_bundle()):
            assert "item_id" in row

    def test_the_exporter_publishes_them_with_council_null(self) -> None:
        """They have no council item, which is why the submitted set is not
        derivable from the council projection (verdicts.py's abstain loop)."""
        built = vx.export_rows(
            abstain=ABSTAIN,
            fanout=[],
            verdicts={},
            identity={
                "query_name": "go_mf",
                "query_version": "4",
                "roster_fingerprint": ROSTER_FINGERPRINT,
                "mapping_hash": MAPPING_HASH,
            },
            job_ids=["chunk01"],
            abstained_at="2026-10-22T09:00:00Z",
        )
        assert len(built["rows"]) == 2
        for row in built["rows"]:
            assert row["council"] is None
            assert row["decline_reason"] == "abstained:no_candidates"
        assert {row["query_id"] for row in built["rows"]} == {
            row["query_id"] for row in abstentions(_bundle())
        }

    def test_an_abstain_row_with_no_query_id_is_refused(self) -> None:
        with pytest.raises(PublicationBundleError):
            _bundle(abstain=[{"text": "x", "abstain_reason": "no_candidates"}])


# --------------------------------------------------------------------------
# The ordered job list
# --------------------------------------------------------------------------


class TestTheOrderedJobList:
    def test_the_declared_order_is_preserved_exactly(self) -> None:
        """Not sorted. A later verdict wins by DECLARED ORDER, so re-ordering
        this list changes which answer is published."""
        bundle = _bundle()
        assert bundle["job_ids"] == BASE_JOBS
        assert bundle["retry_job_ids"] == RETRY_JOBS
        assert bundle["job_ids"] != sorted(bundle["job_ids"])

    def test_the_retries_come_LAST_so_their_verdicts_supersede(self) -> None:
        assert ordered_job_ids(_bundle()) == [
            "chunk10",
            "chunk02",
            "chunk01",
            "retry2",
            "retry1",
        ]

    def test_a_repeated_job_id_is_not_silently_de_duplicated_from_the_declaration(
        self,
    ) -> None:
        """The declaration is recorded verbatim; only the resolution order
        composes base-then-retry."""
        bundle = _bundle(job_ids=["chunk01", "chunk01", "chunk02"])
        assert bundle["job_ids"] == ["chunk01", "chunk01", "chunk02"]

    def test_a_job_in_both_lists_resolves_once_in_the_retry_position(self) -> None:
        bundle = _bundle(job_ids=["chunk01", "retry1"], retry_job_ids=["retry1"])
        assert ordered_job_ids(bundle) == ["chunk01", "retry1"]

    def test_the_order_survives_a_disk_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / BUNDLE_FILENAME
        write_bundle(_bundle(), path)
        assert ordered_job_ids(read_bundle(path)) == [
            "chunk10",
            "chunk02",
            "chunk01",
            "retry2",
            "retry1",
        ]


# --------------------------------------------------------------------------
# The lakehouse copy, and agreement with the disk copy
# --------------------------------------------------------------------------


class TestTheLakehouseCopy:
    def test_the_bundle_is_published_and_reads_back(
        self, sink, complete_snapshot, state_root
    ) -> None:
        bundle = _bundle(snapshot_id=complete_snapshot["snapshot_id"])
        result = publish_bundle(sink, bundle, archive_state_root=state_root)
        assert result["written"] is True
        assert result["snapshot_complete"] is True
        assert read_lakehouse_bundle(sink, CAMPAIGN) == bundle

    def test_the_on_disk_and_lakehouse_copies_AGREE(
        self, sink, complete_snapshot, state_root, tmp_path
    ) -> None:
        bundle = _bundle(snapshot_id=complete_snapshot["snapshot_id"])
        write_bundle(bundle, tmp_path / BUNDLE_FILENAME)
        publish_bundle(sink, bundle, archive_state_root=state_root)

        on_disk = read_bundle(tmp_path / BUNDLE_FILENAME)
        in_lake = read_lakehouse_bundle(sink, CAMPAIGN)
        assert on_disk == in_lake
        assert on_disk["bundle_digest"] == in_lake["bundle_digest"]
        assert canonical_json(on_disk) == canonical_json(in_lake)

    def test_the_row_carries_the_keys_a_lakehouse_only_consumer_needs(
        self, sink, complete_snapshot, state_root
    ) -> None:
        """A consumer with no disk access must be able to resolve a published
        row's campaign."""
        bundle = _bundle(snapshot_id=complete_snapshot["snapshot_id"])
        publish_bundle(sink, bundle, archive_state_root=state_root)
        row = sink.select_rows(PUBLICATION_BUNDLE_TABLE)[0]
        assert row["campaign_id"] == CAMPAIGN
        assert row["store_id"] == STORE_ID
        assert row["snapshot_id"] == complete_snapshot["snapshot_id"]
        assert row["parameter_set_hash"] == PARAMETER_SET_HASH
        assert row["mapping_hash"] == MAPPING_HASH

    def test_republishing_the_IDENTICAL_bundle_is_a_no_op(
        self, sink, complete_snapshot, state_root
    ) -> None:
        bundle = _bundle(snapshot_id=complete_snapshot["snapshot_id"])
        publish_bundle(sink, bundle, archive_state_root=state_root)
        again = publish_bundle(sink, bundle, archive_state_root=state_root)
        assert again["written"] is False
        assert sink.count_rows(PUBLICATION_BUNDLE_TABLE) == 1

    def test_rewriting_a_campaign_with_DIFFERENT_content_is_a_CONFLICT(
        self, sink, complete_snapshot, state_root
    ) -> None:
        """Not an update. The clearinghouse slot its rows land in is frozen on
        first write, so an edited bundle would describe a campaign that no
        longer matches the lake."""
        first = _bundle(snapshot_id=complete_snapshot["snapshot_id"])
        publish_bundle(sink, first, archive_state_root=state_root)
        second = _bundle(
            snapshot_id=complete_snapshot["snapshot_id"],
            job_ids=["chunk01"],
        )
        with pytest.raises(BundleConflictError) as excinfo:
            publish_bundle(sink, second, archive_state_root=state_root)
        assert first["bundle_digest"] in str(excinfo.value)
        assert second["bundle_digest"] in str(excinfo.value)
        # And the FIRST bundle is untouched.
        assert sink.count_rows(PUBLICATION_BUNDLE_TABLE) == 1
        assert read_lakehouse_bundle(sink, CAMPAIGN) == first

    def test_rewriting_the_on_disk_copy_with_different_content_is_a_CONFLICT(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / BUNDLE_FILENAME
        write_bundle(_bundle(), path)
        with pytest.raises(BundleConflictError):
            write_bundle(_bundle(job_ids=["chunk01"]), path)

    def test_a_tampered_lakehouse_row_fails_its_digest_on_read(
        self, sink, complete_snapshot, state_root
    ) -> None:
        bundle = _bundle(snapshot_id=complete_snapshot["snapshot_id"])
        publish_bundle(sink, bundle, archive_state_root=state_root)
        tampered = dict(bundle)
        tampered["ordered_job_ids"] = ["chunk01"]
        sink._connection.execute(  # the only way to forge a row: write past the API
            f'UPDATE "{sink.physical_name(PUBLICATION_BUNDLE_TABLE)}" '
            "SET content = ?",
            (canonical_json(tampered),),
        )
        sink._connection.commit()
        with pytest.raises(BundleDigestError):
            read_lakehouse_bundle(sink, CAMPAIGN)

    def test_an_unpublished_campaign_reads_as_None_not_as_empty(
        self, sink
    ) -> None:
        assert read_lakehouse_bundle(sink, "never-published") is None


# --------------------------------------------------------------------------
# THE SNAPSHOT GATE, against a REAL interrupted archive
# --------------------------------------------------------------------------


class TestTheSnapshotCompletenessGate:
    def test_a_bundle_naming_an_INCOMPLETELY_archived_snapshot_is_REFUSED(
        self, sink, incomplete_snapshot, state_root
    ) -> None:
        """The ledger read here was left by the REAL archiver crashing between
        `calls` and `attempts`. Nothing about it is constructed by the test."""
        bundle = _bundle(snapshot_id=incomplete_snapshot["snapshot_id"])
        with pytest.raises(BundleIncompleteSnapshotError) as excinfo:
            publish_bundle(sink, bundle, archive_state_root=state_root)
        message = str(excinfo.value)
        assert "attempts" in message
        assert incomplete_snapshot["snapshot_id"] in message
        assert sink.count_rows(PUBLICATION_BUNDLE_TABLE) == 0

    def test_the_refusal_delegates_and_builds_no_second_completeness_check(
        self, sink, incomplete_snapshot, state_root
    ) -> None:
        """Two completeness tests would be free to disagree, and the failure
        that permits is publishing against provenance that reads complete and
        is not."""
        source = Path(
            __import__(
                "kbutillib.domains.genome.ontology_mapping.council_bundle",
                fromlist=["council_bundle"],
            ).__file__
        ).read_text(encoding="utf-8")
        assert "require_complete_delivery" in source
        # No local reconciliation: the bundle module never counts archived
        # rows itself.
        assert "count_rows" not in source

    def test_the_SAME_bundle_publishes_once_the_delivery_is_RESUMED(
        self, sink, incomplete_snapshot, state_root
    ) -> None:
        bundle = _bundle(snapshot_id=incomplete_snapshot["snapshot_id"])
        with pytest.raises(BundleIncompleteSnapshotError):
            publish_bundle(sink, bundle, archive_state_root=state_root)

        report = archive_snapshot(
            sink, incomplete_snapshot, snapshot_seq=1, state_root=state_root
        )
        assert report["complete"] is True
        result = publish_bundle(sink, bundle, archive_state_root=state_root)
        assert result["written"] is True

    def test_a_bundle_naming_a_snapshot_with_NO_delivery_at_all_is_refused(
        self, sink, state_root
    ) -> None:
        """No ledger is not "complete by default"."""
        bundle = _bundle(snapshot_id="f" * 64)
        with pytest.raises(BundleIncompleteSnapshotError):
            publish_bundle(sink, bundle, archive_state_root=state_root)

    def test_the_completeness_check_can_be_waived_only_EXPLICITLY(
        self, sink, incomplete_snapshot, state_root
    ) -> None:
        """A named exception for a snapshot archived on another machine, where
        this machine holds no ledger -- never a default."""
        bundle = _bundle(snapshot_id=incomplete_snapshot["snapshot_id"])
        result = publish_bundle(
            sink, bundle, archive_state_root=state_root, require_complete=False
        )
        assert result["written"] is True
        assert result["snapshot_complete"] is False


# --------------------------------------------------------------------------
# BOTH gates: bundle-listed AND declared published
# --------------------------------------------------------------------------


class TestBothGates:
    @pytest.fixture
    def dispositioned(self, sink, complete_snapshot, tmp_path):
        """Declare chunk01 published and chunk02 a pilot, through the real
        seed/declare path, and write the rows for this snapshot."""
        ledger = cd.DispositionLedger(
            cd.resolve_ledger_path(STORE_ID, root=tmp_path / "ledgers")
        )
        cd.declare(
            ledger,
            job_id="chunk01",
            disposition="published",
            reason="part of the published dataset",
            declared_by="miles",
        )
        cd.declare(
            ledger,
            job_id="chunk02",
            disposition="pilot",
            reason="500-item pilot on a different roster",
            declared_by="miles",
        )
        cd.write_dispositions(
            sink,
            ledger,
            store_id=STORE_ID,
            snapshot_id=complete_snapshot["snapshot_id"],
            snapshot_seq=1,
            store_host="poplar",
            load_batch_id="01JBUNDLE000000000000000001",
        )
        return _bundle(snapshot_id=complete_snapshot["snapshot_id"])

    def test_bundle_listed_AND_published_passes(self, sink, dispositioned) -> None:
        assert require_publishable(sink, dispositioned, ["chunk01"]) == ["chunk01"]

    def test_bundle_listed_but_declared_PILOT_is_refused(
        self, sink, dispositioned
    ) -> None:
        """Bundle listing does not imply a disposition."""
        with pytest.raises(cd.JobNotPublishableError) as excinfo:
            require_publishable(sink, dispositioned, ["chunk02"])
        assert "chunk02" in str(excinfo.value)
        assert "pilot" in str(excinfo.value)

    def test_bundle_listed_but_UNDECLARED_is_refused(
        self, sink, dispositioned
    ) -> None:
        with pytest.raises(cd.JobNotPublishableError) as excinfo:
            require_publishable(sink, dispositioned, ["chunk10"])
        assert "chunk10" in str(excinfo.value)
        assert "NO disposition row" in str(excinfo.value)

    def test_declared_published_but_NOT_bundle_listed_is_refused(
        self, sink, complete_snapshot, tmp_path
    ) -> None:
        """A disposition does not imply membership of THIS campaign: a job can
        be a genuine result of a different one."""
        ledger = cd.DispositionLedger(
            cd.resolve_ledger_path(STORE_ID, root=tmp_path / "ledgers2")
        )
        cd.declare(
            ledger,
            job_id="other-campaign-chunk",
            disposition="published",
            reason="a result -- of another campaign",
            declared_by="miles",
        )
        cd.write_dispositions(
            sink,
            ledger,
            store_id=STORE_ID,
            snapshot_id=complete_snapshot["snapshot_id"],
            snapshot_seq=1,
            store_host="poplar",
            load_batch_id="01JBUNDLE000000000000000002",
        )
        bundle = _bundle(snapshot_id=complete_snapshot["snapshot_id"])
        with pytest.raises(JobNotInBundleError) as excinfo:
            require_publishable(sink, bundle, ["other-campaign-chunk"])
        assert "other-campaign-chunk" in str(excinfo.value)

    def test_require_bundle_listed_alone_names_every_missing_job(self) -> None:
        with pytest.raises(JobNotInBundleError) as excinfo:
            require_bundle_listed(_bundle(), ["chunk01", "ghost-a", "ghost-b"])
        assert "ghost-a" in str(excinfo.value)
        assert "ghost-b" in str(excinfo.value)


# --------------------------------------------------------------------------
# Building from a run directory -- the same two files the exporter reads
# --------------------------------------------------------------------------


class TestBuildFromARunDirectory:
    @pytest.fixture
    def run_dir(self, tmp_path: Path) -> Path:
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        directory = tmp_path / "run-1"
        directory.mkdir(parents=True, exist_ok=True)
        scratch = tmp_path / "scratch"
        scratch.mkdir(parents=True, exist_ok=True)
        abstain_file = scratch / "abstain.jsonl"
        abstain_file.write_text(
            "".join(json.dumps(row) + "\n" for row in ABSTAIN), encoding="utf-8"
        )
        fanout_file = scratch / "fanout.jsonl"
        fanout_file.write_text(
            "".join(json.dumps(row) + "\n" for row in FANOUT), encoding="utf-8"
        )
        plan = _plan(
            abstain_file=str(abstain_file),
            fanout_file=str(fanout_file),
            job_ids=BASE_JOBS,
            retry_job_ids=RETRY_JOBS,
        )
        (directory / PLAN_FILENAME).write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return directory

    def test_it_reads_the_plans_own_two_records(self, run_dir: Path) -> None:
        bundle = build_bundle_from_run(
            run_dir,
            campaign_id=CAMPAIGN,
            store_id=STORE_ID,
            snapshot_id="a" * 64,
            written_by="miles",
        )
        assert bundle["submitted"]["counts"]["inputs"] == 6
        assert fanout_map(bundle)["item-1"] == [_hash(1), _hash(2), _hash(3)]
        assert len(abstentions(bundle)) == 2
        assert ordered_job_ids(bundle) == [
            "chunk10",
            "chunk02",
            "chunk01",
            "retry2",
            "retry1",
        ]

    def test_a_missing_plan_is_refused_naming_the_verb_that_writes_it(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError) as excinfo:
            build_bundle_from_run(
                tmp_path / "empty",
                campaign_id=CAMPAIGN,
                store_id=STORE_ID,
                snapshot_id="a" * 64,
            )
        assert "council-plan" in str(excinfo.value)

    def test_a_record_file_that_is_GONE_is_refused(self, run_dir: Path) -> None:
        """/scratch is ephemeral on this host and is not backed up."""
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        plan_file = run_dir / PLAN_FILENAME
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        Path(plan["fanout_file"]).unlink()
        with pytest.raises(FileNotFoundError) as excinfo:
            build_bundle_from_run(
                run_dir,
                campaign_id=CAMPAIGN,
                store_id=STORE_ID,
                snapshot_id="a" * 64,
            )
        assert "fanout_file" in str(excinfo.value)

    def test_a_plan_naming_no_job_ids_is_refused(self, run_dir: Path) -> None:
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        plan_file = run_dir / PLAN_FILENAME
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        plan.pop("job_ids")
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(PublicationBundleError) as excinfo:
            build_bundle_from_run(
                run_dir,
                campaign_id=CAMPAIGN,
                store_id=STORE_ID,
                snapshot_id="a" * 64,
            )
        assert "no council job ids" in str(excinfo.value)

    def test_a_plan_naming_no_record_file_is_refused(self, run_dir: Path) -> None:
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        plan_file = run_dir / PLAN_FILENAME
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        plan.pop("abstain_file")
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(PublicationBundleError) as excinfo:
            build_bundle_from_run(
                run_dir,
                campaign_id=CAMPAIGN,
                store_id=STORE_ID,
                snapshot_id="a" * 64,
            )
        assert "abstain_file" in str(excinfo.value)


# --------------------------------------------------------------------------
# SCHEMA v2: the abstention texts and the plan's origin (KBUtilLib#97)
# --------------------------------------------------------------------------


class TestTheAbstentionTexts:
    """A. The bundle carries each abstention's function text.

    It is the ONLY durable source: retrieval declined before anything was
    submitted, so no council item exists and no ``items.evidence`` in the
    archive holds it. The clearinghouse slot an abstention's row lands in
    freezes on first write, so a text that is not carried here is lost for
    good rather than merely late.
    """

    def test_the_schema_version_is_2(self) -> None:
        assert BUNDLE_SCHEMA_VERSION == 2
        assert _bundle()["schema_version"] == 2

    def test_every_abstention_carries_the_PLAN_ROWS_text_verbatim(self) -> None:
        carried = {row["query_id"]: row["text"] for row in abstentions(_bundle())}
        assert carried == {row["query_id"]: row["text"] for row in ABSTAIN}
        assert carried[_hash(90)] == "hypothetical protein"
        assert carried[_hash(91)] == "DUF1234 family protein"

    def test_a_plan_row_with_no_text_yields_an_abstention_with_an_EMPTY_one(
        self,
    ) -> None:
        """Not a build-time refusal: the bundle records what the plan held.

        The refusal belongs at PUBLISH, where it can say which rows would land
        in a frozen slot -- see
        ``berdl.council_publish.AbstentionTextMissingError``. Refusing here
        would also make a bundle unbuildable for inspection, which is the
        artifact a human needs in order to FIX the plan.
        """
        blanked = [{**ABSTAIN[0], "text": ""}, ABSTAIN[1]]
        rows = abstentions(_bundle(abstain=blanked))
        assert [row["text"] for row in rows] == ["", "DUF1234 family protein"]

    def test_the_text_survives_a_disk_round_trip(self, tmp_path: Path) -> None:
        bundle = _bundle()
        write_bundle(bundle, tmp_path / BUNDLE_FILENAME)
        back = read_bundle(tmp_path / BUNDLE_FILENAME)
        assert [row["text"] for row in abstentions(back)] == [
            "hypothetical protein",
            "DUF1234 family protein",
        ]

    def test_the_text_is_covered_by_the_digest(self) -> None:
        """Changing an abstention's text makes a DIFFERENT bundle."""
        edited = [{**ABSTAIN[0], "text": "something else"}, ABSTAIN[1]]
        assert _bundle(abstain=edited)["bundle_digest"] != _bundle()["bundle_digest"]


class TestThePlanOrigin:
    """B. Whether the plan came from stage 1 or was RECONSTRUCTED afterwards."""

    def test_plan_origin_is_a_declared_content_field(self) -> None:
        assert "plan_origin" in BUNDLE_CONTENT_FIELDS
        assert set(bundle_plan_origin(_bundle())) == set(PLAN_ORIGIN_FIELDS)

    def test_a_stage1_plan_needs_no_declaration_and_takes_the_plans_own_date(
        self,
    ) -> None:
        """The ONE thing ever derived: a stage-1 plan's generated_at IS the
        moment retrieval ran."""
        origin = bundle_plan_origin(_bundle())
        assert origin["kind"] == "stage1"
        assert origin["generated_at"] == "2026-10-22T09:00:00Z"
        assert "stage 1" in origin["generated_at_source"]
        assert origin["assembled_on"] == ""
        assert origin["reason"] == ""

    def test_the_only_two_kinds(self) -> None:
        assert PLAN_ORIGIN_KINDS == ("stage1", "reconstructed")

    def test_an_unknown_kind_is_refused_naming_the_two(self) -> None:
        with pytest.raises(PlanOriginError) as caught:
            _bundle(plan_origin={"kind": "handmade"})
        assert "handmade" in str(caught.value)
        assert "reconstructed" in str(caught.value)

    def test_a_MISSPELLED_field_is_refused_rather_than_dropped(self) -> None:
        """generated_at is the field most worth refusing a typo on: it becomes
        the permanent observed_at of every abstention."""
        with pytest.raises(PlanOriginError) as caught:
            _bundle(
                plan_origin={
                    "kind": "reconstructed",
                    "assembled_on": "2026-10-19",
                    "reason": "predates stage 1",
                    "generated_on": "2026-10-19T00:00:00Z",  # NOT generated_at
                    "generated_at_source": "min(jobs.submitted_at)",
                }
            )
        assert "generated_on" in str(caught.value)

    @pytest.mark.parametrize(
        "omitted",
        ["assembled_on", "reason", "generated_at", "generated_at_source"],
    )
    def test_a_reconstructed_origin_must_declare_ALL_FOUR(self, omitted: str) -> None:
        declared = {
            "kind": "reconstructed",
            "assembled_on": "2026-10-19",
            "reason": "the campaign predates stage 1",
            "generated_at": "2026-10-19T04:12:07Z",
            "generated_at_source": "min(council.jobs.submitted_at) over the base jobs",
        }
        declared[omitted] = ""
        with pytest.raises(PlanOriginError) as caught:
            _bundle(plan_origin=declared)
        assert omitted in str(caught.value)

    def test_a_reconstructed_origin_is_carried_WHOLE_and_nothing_is_inferred(
        self,
    ) -> None:
        """The plan here carries generated_at = 2026-10-22; the DECLARED value
        wins, because for a reconstruction the plan's own date is whenever the
        person assembling it happened to write the file."""
        declared = {
            "kind": "reconstructed",
            "assembled_on": "2026-10-19",
            "reason": "the campaign predates stage 1",
            "generated_at": "2026-10-19T04:12:07Z",
            "generated_at_source": "min(council.jobs.submitted_at) over the base jobs",
        }
        assert bundle_plan_origin(_bundle(plan_origin=declared)) == declared

    def test_a_campaign_WITH_abstentions_may_not_have_a_blank_generated_at(
        self,
    ) -> None:
        """The now() hole, closed at the source.

        verdicts.export_rows falls back to now(); the preset maps decided_at
        onto observed_at; the slot freezes on first write. A blank here would
        make the PUBLISH DATE the permanent observation time of every
        abstention, so the bundle refuses to be built.
        """
        plan = _plan()
        plan.pop("generated_at")
        with pytest.raises(PlanOriginError) as caught:
            build_bundle(
                plan,
                campaign_id=CAMPAIGN,
                store_id=STORE_ID,
                snapshot_id="a" * 64,
                abstain=ABSTAIN,
                fanout=FANOUT,
                job_ids=BASE_JOBS,
            )
        message = str(caught.value)
        assert "generated_at" in message and "FREEZES ON FIRST WRITE" in message

    def test_a_campaign_with_NO_abstentions_may(self) -> None:
        """There is no abstention to mis-stamp, so nothing is at risk."""
        plan = _plan()
        plan.pop("generated_at")
        bundle = build_bundle(
            plan,
            campaign_id=CAMPAIGN,
            store_id=STORE_ID,
            snapshot_id="a" * 64,
            abstain=[],
            fanout=FANOUT,
            job_ids=BASE_JOBS,
        )
        assert bundle_plan_origin(bundle)["generated_at"] == ""

    def test_the_origin_is_covered_by_the_digest(self) -> None:
        reconstructed = _bundle(
            plan_origin={
                "kind": "reconstructed",
                "assembled_on": "2026-10-19",
                "reason": "the campaign predates stage 1",
                "generated_at": "2026-10-22T09:00:00Z",
                "generated_at_source": "min(council.jobs.submitted_at)",
            }
        )
        assert reconstructed["bundle_digest"] != _bundle()["bundle_digest"]


class TestBuildFromARunDirectoryInfersNoOrigin:
    """``build_bundle_from_run`` takes the reconstruction fields as EXPLICIT
    arguments and never infers them.

    A run directory is the wrong place to guess from: a hand-assembled
    PLAN.json is indistinguishable from a stage-1 one on disk, so any
    heuristic -- does it carry run_id? is plan_hash blank? -- would silently
    classify a reconstruction as stage 1 and hand every abstention ``now()``
    as its permanent observed_at.
    """

    @pytest.fixture
    def hand_assembled(self, tmp_path: Path) -> Path:
        """A run directory like the real 2026-10-19 one: NO generated_at, NO
        plan_hash, no run_id -- and 2 abstentions that still carry their text."""
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        directory = tmp_path / "xport.hand"
        directory.mkdir(parents=True, exist_ok=True)
        abstain_file = directory / "abstain.jsonl"
        abstain_file.write_text(
            "".join(json.dumps(row) + "\n" for row in ABSTAIN), encoding="utf-8"
        )
        fanout_file = directory / "fanout.jsonl"
        fanout_file.write_text(
            "".join(json.dumps(row) + "\n" for row in FANOUT), encoding="utf-8"
        )
        plan = _plan(
            abstain_file=str(abstain_file),
            fanout_file=str(fanout_file),
            job_ids=BASE_JOBS,
            retry_job_ids=RETRY_JOBS,
        )
        for absent in ("generated_at", "run_id", "plan_hash"):
            plan.pop(absent, None)
        (directory / PLAN_FILENAME).write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return directory

    def test_it_REFUSES_a_hand_assembled_plan_passed_off_as_stage1(
        self, hand_assembled: Path
    ) -> None:
        """The default kind is stage1 and it does NOT quietly become
        'reconstructed' just because the plan looks hand-made."""
        with pytest.raises(PlanOriginError) as caught:
            build_bundle_from_run(
                hand_assembled,
                campaign_id=CAMPAIGN,
                store_id=STORE_ID,
                snapshot_id="a" * 64,
                written_by="miles",
            )
        assert "generated_at" in str(caught.value)

    def test_declaring_reconstructed_WITHOUT_the_four_is_still_refused(
        self, hand_assembled: Path
    ) -> None:
        with pytest.raises(PlanOriginError) as caught:
            build_bundle_from_run(
                hand_assembled,
                campaign_id=CAMPAIGN,
                store_id=STORE_ID,
                snapshot_id="a" * 64,
                plan_origin_kind="reconstructed",
                written_by="miles",
            )
        for field in ("assembled_on", "reason", "generated_at", "generated_at_source"):
            assert field in str(caught.value)

    def test_the_four_declared_EXPLICITLY_build_the_bundle(
        self, hand_assembled: Path
    ) -> None:
        bundle = build_bundle_from_run(
            hand_assembled,
            campaign_id=CAMPAIGN,
            store_id=STORE_ID,
            snapshot_id="a" * 64,
            plan_origin_kind="reconstructed",
            assembled_on="2026-10-19",
            plan_origin_reason="the campaign predates stage 1",
            generated_at="2026-10-19T04:12:07Z",
            generated_at_source="min(council.jobs.submitted_at) over the base jobs",
            written_by="miles",
        )
        assert bundle_plan_origin(bundle) == {
            "kind": "reconstructed",
            "assembled_on": "2026-10-19",
            "reason": "the campaign predates stage 1",
            "generated_at": "2026-10-19T04:12:07Z",
            "generated_at_source": "min(council.jobs.submitted_at) over the base jobs",
        }
        # The plan is ACCEPTED AND RECORDED, not regenerated: its plan_hash is
        # still the empty string and no date was invented for it.
        assert bundle["plan_hash"] == ""
        assert "generated_at" not in bundle["plan"]
        assert [row["text"] for row in abstentions(bundle)] == [
            "hypothetical protein",
            "DUF1234 family protein",
        ]

    def test_a_STAGE1_run_directory_still_needs_no_declaration(
        self, tmp_path: Path
    ) -> None:
        """The common path is unchanged: a real stage-1 plan carries its own
        generated_at and the origin falls out of it."""
        from kbutillib.domains.ai.council.quote import PLAN_FILENAME

        directory = tmp_path / "xport.stage1"
        directory.mkdir(parents=True, exist_ok=True)
        abstain_file = directory / "abstain.jsonl"
        abstain_file.write_text(
            "".join(json.dumps(row) + "\n" for row in ABSTAIN), encoding="utf-8"
        )
        fanout_file = directory / "fanout.jsonl"
        fanout_file.write_text(
            "".join(json.dumps(row) + "\n" for row in FANOUT), encoding="utf-8"
        )
        (directory / PLAN_FILENAME).write_text(
            json.dumps(
                _plan(
                    abstain_file=str(abstain_file),
                    fanout_file=str(fanout_file),
                    job_ids=BASE_JOBS,
                ),
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        origin = bundle_plan_origin(
            build_bundle_from_run(
                directory,
                campaign_id=CAMPAIGN,
                store_id=STORE_ID,
                snapshot_id="a" * 64,
                written_by="miles",
            )
        )
        assert origin["kind"] == "stage1"
        assert origin["generated_at"] == "2026-10-22T09:00:00Z"
