"""Tests for ``kbu arc backfill`` (the backfill core + CLI).

Covers (binding): backfill is idempotent across two runs at the
``(analysis_id, run_uid)`` level; ``--dry-run`` writes nothing; every backfilled
record carries ``payload.provenance = "inferred"`` and a deterministic synthetic
run_uid; file artifacts are preferred over store refs; unattributable records go
to the unattributed index; the coverage report lists unattributable artifacts and
referenced-but-missing analyses rather than only a written-count; the store probe
reports store-unavailable when unreachable and introduces no new object-store env
var.

All tests use the SHIPPED fake store (never a hand-written double), matching the
rest of the models_and_analyses suite.
"""

from __future__ import annotations

import pytest

from kbutillib.koros_arc_store_testing import FakeKorosArcStore
from kbutillib.models_and_analyses.backfill import (
    BACKFILL_PRODUCER,
    CoverageReport,
    backfill,
    synthetic_run_uid,
)


@pytest.fixture
def runs_root(tmp_path):
    """A minimal runs tree with one arc holding a model, an FBA and an FVA."""
    arc = tmp_path / "runs" / "projA" / "arcs" / "arc1"
    (arc / "fba").mkdir(parents=True)
    (arc / "fva").mkdir(parents=True)
    (arc / "ecoli.model.json").write_text("{}")
    (arc / "fba" / "aerobic.json").write_text("{}")
    (arc / "fva" / "aerobic.json").write_text("{}")
    return tmp_path / "runs"


def _store(runs_root=None):
    # The fake is backed by the real runs tree so the store owns all traversal:
    # backfill reaches artifacts ONLY through list_projects / list_arcs /
    # list_arc_artifacts (layering invariant), never by walking the tree itself.
    return FakeKorosArcStore(enabled=True, runs_root=runs_root)


class TestFileScan:
    def test_writes_one_record_per_file_artifact(self, runs_root):
        store = _store(runs_root)
        report = backfill(store, runs_root, scan_store=False)
        assert report.arcs_scanned == 1
        assert report.records_written == 3
        rows = store.list_analyses("projA", "arc1")
        kinds = sorted(r.kind for r in rows)
        assert kinds == [
            "kbutillib.fba",
            "kbutillib.fva",
            "kbutillib.reconstruct",
        ]

    def test_records_carry_inferred_provenance_marker(self, runs_root):
        store = _store(runs_root)
        backfill(store, runs_root, scan_store=False)
        for rec in store.list_analyses("projA", "arc1"):
            assert rec.payload["provenance"] == "inferred"
            assert rec.producer == BACKFILL_PRODUCER

    def test_records_carry_synthetic_deterministic_run_uid(self, runs_root):
        store = _store(runs_root)
        backfill(store, runs_root, scan_store=False)
        for rec in store.list_analyses("projA", "arc1"):
            assert rec.run_uid.startswith("backfill:")

    def test_synthetic_run_uid_is_deterministic(self):
        assert synthetic_run_uid("a/b.model.json") == synthetic_run_uid(
            "a/b.model.json"
        )
        assert synthetic_run_uid("a") != synthetic_run_uid("b")


class TestIdempotency:
    def test_second_pass_writes_no_new_row(self, runs_root):
        store = _store(runs_root)
        backfill(store, runs_root, scan_store=False)
        n1 = len(store._rows)
        backfill(store, runs_root, scan_store=False)
        n2 = len(store._rows)
        assert n1 == n2 == 3

    def test_second_pass_keeps_same_record_ids(self, runs_root):
        store = _store(runs_root)
        backfill(store, runs_root, scan_store=False)
        ids1 = set(store._rows)
        backfill(store, runs_root, scan_store=False)
        ids2 = set(store._rows)
        assert ids1 == ids2


class TestDryRun:
    def test_dry_run_writes_nothing(self, runs_root):
        store = _store(runs_root)
        report = backfill(store, runs_root, scan_store=False, dry_run=True)
        assert len(store._rows) == 0
        assert report.records_written == 0

    def test_dry_run_reports_what_it_would_write(self, runs_root):
        store = _store(runs_root)
        report = backfill(store, runs_root, scan_store=False, dry_run=True)
        assert len(report.dry_run_would_write) == 3


class TestNarrowing:
    def test_project_filter(self, tmp_path):
        for proj in ("projA", "projB"):
            arc = tmp_path / "runs" / proj / "arcs" / "arc1"
            arc.mkdir(parents=True)
            (arc / "m.model.json").write_text("{}")
        store = _store(tmp_path / "runs")
        report = backfill(
            store, tmp_path / "runs", project="projA", scan_store=False
        )
        assert report.arcs_scanned == 1
        assert all(r.project == "projA" for r in store._rows.values())

    def test_arc_filter(self, tmp_path):
        for slug in ("arc1", "arc2"):
            arc = tmp_path / "runs" / "projA" / "arcs" / slug
            arc.mkdir(parents=True)
            (arc / "m.model.json").write_text("{}")
        store = _store(tmp_path / "runs")
        report = backfill(store, tmp_path / "runs", arc="arc1", scan_store=False)
        assert report.arcs_scanned == 1


class _FakeStoreClient:
    """A stand-in KBDL client exposing only ``list_objects`` (per-owner)."""

    def __init__(self, objects):
        self._objects = objects

    def list_objects(self):
        return self._objects


class TestStoreScan:
    def test_prefers_file_artifacts_over_store_refs(self, runs_root):
        # A store object whose subject matches a file subject is NOT re-recorded.
        client = _FakeStoreClient(
            [
                {
                    "object_type": "model",
                    "name": "ecoli",  # same subject as the file model
                    "object_id": "objdup",
                    "owner": "alice",
                },
                {
                    "object_type": "model",
                    "name": "orphan",
                    "object_id": "obj-orphan",
                    "owner": "alice",
                },
            ]
        )
        store = _store(runs_root)
        report = backfill(
            store,
            runs_root,
            scan_store=True,
            owner="alice",
            store_client=client,
        )
        assert report.store_status == "scanned"
        # 3 files + 1 store orphan; the store 'ecoli' is deduped by subject.
        assert report.records_written == 4

    def test_unattributable_store_objects_go_to_unattributed_index(self, runs_root):
        client = _FakeStoreClient(
            [
                {
                    "object_type": "model",
                    "name": "orphan",
                    "object_id": "obj-orphan",
                    "owner": "alice",
                }
            ]
        )
        store = _store(runs_root)
        report = backfill(
            store,
            runs_root,
            scan_store=True,
            owner="alice",
            store_client=client,
        )
        unattributed = store.list_analyses(None, None)
        assert len(unattributed) == 1
        assert len(report.unattributable) == 1

    def test_foreign_object_types_are_ignored(self, runs_root):
        client = _FakeStoreClient(
            [{"object_type": "annotation", "name": "x", "object_id": "o", "owner": "a"}]
        )
        store = _store(runs_root)
        report = backfill(
            store, runs_root, scan_store=True, owner="a", store_client=client
        )
        # 3 files only; the annotation object is not a model/fba/fva.
        assert report.records_written == 3

    def test_store_unavailable_is_reported_when_listing_fails(self, runs_root):
        class Broken:
            def list_objects(self):
                raise RuntimeError("boom")

        store = _store(runs_root)
        report = backfill(
            store, runs_root, scan_store=True, store_client=Broken()
        )
        assert report.store_status == "unavailable"

    def test_no_new_object_store_env_var_introduced(self):
        # The config key must come from the KBDL client's own env var, and the
        # confront adversary's invented KBDL_OBJECT_STORE_URL must not exist.
        import kbutillib.models_and_analyses.backfill as bf_src
        from kbutillib.domains.external import kbdl_service_utils

        source = open(bf_src.__file__, encoding="utf-8").read()
        assert "KBDL_OBJECT_STORE_URL" not in source
        # And the key we read is the client's own.
        assert bf_src._kbdl_client_config_key() == (
            kbdl_service_utils.KBDL_SERVICE_URL_ENV_VAR
        )


class TestCoverageReport:
    def test_report_lists_missing_analyses(self, tmp_path):
        # A file that vanishes between scan and read is reported as missing.
        arc = tmp_path / "runs" / "projA" / "arcs" / "arc1"
        arc.mkdir(parents=True)
        model = arc / "gone.model.json"
        model.write_text("{}")
        store = _store(tmp_path / "runs")

        # Monkeypatch the scan to return a path that no longer exists.
        import kbutillib.models_and_analyses.backfill as bf

        orig = bf._scan_arc_files

        def fake_scan(store, project, slug):
            # Return a path that no longer exists (the file is unlinked below), so
            # the record is reported as referenced-but-missing rather than written.
            return [("model", "gone", tmp_path / "runs" / "projA" / "arcs" / "arc1" / "gone.model.json")]

        bf._scan_arc_files = fake_scan
        try:
            model.unlink()
            report = backfill(store, tmp_path / "runs", scan_store=False)
        finally:
            bf._scan_arc_files = orig

        assert report.records_written == 0
        assert len(report.missing) == 1

    def test_report_text_is_not_only_a_count(self, runs_root):
        store = _store(runs_root)
        report = backfill(store, runs_root, scan_store=False)
        text = report.as_text()
        assert "arcs scanned" in text
        assert "unattributable" in text
        assert "referenced-but-missing" in text
        assert "unrecoverable" in text.lower()
