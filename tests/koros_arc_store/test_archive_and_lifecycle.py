"""Tests for the payload archive and the arc data-lifecycle API.

Both exist because the run index is NOT a copy of the science. KBDL job results
expire -- 25 of 453 relevant ones already had by 2026-10-02, and the count moved
during a single four-minute run -- and annotation/model results have no second
copy anywhere. So the archive is the only durable store for those payloads, and
anything that retires an arc has to move BOTH the index rows and the archives or
it orphans one of them.

The destructive paths are pinned hard here: dry-run must be the default,
``delete_archives`` must be a separate opt-in, and ``archive_arc`` must dump
before it purges and must refuse to purge at all if the dump produced no
manifest.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kbutillib.koros_arc_store import archive as arc_archive
from kbutillib.koros_arc_store.identity import derive_analysis_id, derive_record_id
from kbutillib.koros_arc_store.records import AnalysisRecord
from kbutillib.koros_arc_store.store import KorosArcStore


@pytest.fixture
def runs_root(tmp_path, monkeypatch):
    arc = tmp_path / "runs" / "projA" / "arcs" / "arc1"
    arc.mkdir(parents=True)
    (arc / "PROVENANCE.json").write_text(json.dumps({"run_name": "arc1", "project": "projA"}))
    monkeypatch.setenv("KOROS_HOME", str(tmp_path))
    monkeypatch.setenv("KBDL_RUN_DB", str(tmp_path / "runs.sqlite"))
    monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "1")
    monkeypatch.setenv(arc_archive.ARCHIVE_ROOT_ENV_VAR, str(tmp_path / "archive"))
    return tmp_path


def _record(kind="kbutillib.fba", run_uid="uid-1", subject="m.json") -> AnalysisRecord:
    aid = derive_analysis_id(kind, subject, {"media": "gmm"})
    return AnalysisRecord(
        record_id=derive_record_id(aid, run_uid), analysis_id=aid, run_uid=run_uid,
        kind=kind, created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        producer="test", producer_version="0", subject=subject, status="ok", artifacts={},
        payload={"objective_value": 1.0}, trust_tier="verified", provenance={},
        contract_version=1, project="projA", arc="arc1",
    )


class TestArchivePayload:
    def test_writes_gzipped_json_and_roundtrips(self, runs_root):
        p = arc_archive.archive_payload("kbdl.genome_annotation", "job-1", {"g1": {"RAST": 1}})
        assert p is not None and p.is_file()
        assert json.load(gzip.open(p, "rt")) == {"g1": {"RAST": 1}}

    def test_idempotent_second_call_leaves_the_file_alone(self, runs_root):
        p1 = arc_archive.archive_payload("k", "job-1", {"a": 1})
        mtime = p1.stat().st_mtime_ns
        p2 = arc_archive.archive_payload("k", "job-1", {"a": 999})
        assert p2 == p1
        assert p1.stat().st_mtime_ns == mtime, "existing archive must not be rewritten"
        assert json.load(gzip.open(p1, "rt")) == {"a": 1}

    def test_leaves_no_part_file_behind(self, runs_root):
        arc_archive.archive_payload("k", "job-1", {"a": 1})
        assert list((runs_root / "archive").rglob("*.part")) == []

    def test_soft_fails_to_none_rather_than_raising(self, runs_root, monkeypatch):
        """An archive failure must never be allowed to also lose the record."""
        def boom(*a, **k):
            raise OSError("disk on fire")
        monkeypatch.setattr(arc_archive.gzip, "open", boom)
        assert arc_archive.archive_payload("k", "job-x", {"a": 1}) is None

    def test_path_traversal_in_identifiers_cannot_escape_the_root(self, runs_root):
        p = arc_archive.archive_payload("../../etc", "../../passwd", {"a": 1})
        assert p is not None
        assert (runs_root / "archive") in p.parents, p


class TestRecordAnalysisRaw:
    def test_raw_is_archived_and_pointed_at(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"big": "payload"})
        got = s.list_analyses("projA", "arc1")[0]
        p = Path(got.artifacts["result"])
        assert p.is_file()
        assert json.load(gzip.open(p, "rt")) == {"big": "payload"}

    def test_without_raw_no_artifact_is_invented(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record())
        assert not s.list_analyses("projA", "arc1")[0].artifacts

    def test_record_survives_a_failed_archive(self, runs_root, monkeypatch):
        monkeypatch.setattr(arc_archive, "archive_payload", lambda *a, **k: None)
        import kbutillib.koros_arc_store.store as store_mod
        monkeypatch.setattr(store_mod, "archive_payload", lambda *a, **k: None)
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"x": 1})
        recs = s.list_analyses("projA", "arc1")
        assert len(recs) == 1 and not recs[0].artifacts


class TestInventory:
    def test_counts_records_and_archive_bytes(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(run_uid="u1"), raw={"a": 1})
        s.record_analysis("projA", "arc1", _record(kind="kbutillib.gapfill", run_uid="u2"))
        inv = s.arc_data_inventory("projA", "arc1")
        assert inv["records"] == 2
        assert inv["archives"] == 1 and inv["archive_bytes"] > 0
        assert set(inv["records_by_kind"]) == {"kbutillib.fba", "kbutillib.gapfill"}

    def test_a_row_pointing_at_a_vanished_archive_is_REPORTED(self, runs_root):
        """Silence here would hide that the only copy is already gone."""
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        Path(s.list_analyses("projA", "arc1")[0].artifacts["result"]).unlink()
        inv = s.arc_data_inventory("projA", "arc1")
        assert inv["archives"] == 0 and len(inv["archives_missing"]) == 1


class TestDump:
    def test_manifest_carries_every_record_in_full(self, runs_root, tmp_path):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        out = s.dump_arc_data("projA", "arc1", tmp_path / "dump")
        m = json.loads(Path(out["manifest"]).read_text())
        assert len(m["records"]) == 1
        assert m["records"][0]["analysis_id"]
        assert out["archives_copied"] == 1 and out["archives_moved"] == 0

    def test_copy_leaves_the_original_in_place(self, runs_root, tmp_path):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        src = Path(s.list_analyses("projA", "arc1")[0].artifacts["result"])
        s.dump_arc_data("projA", "arc1", tmp_path / "dump")
        assert src.is_file()

    def test_move_relocates_and_frees_the_live_store(self, runs_root, tmp_path):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        src = Path(s.list_analyses("projA", "arc1")[0].artifacts["result"])
        out = s.dump_arc_data("projA", "arc1", tmp_path / "dump", move=True)
        assert out["archives_moved"] == 1
        assert not src.exists()
        moved = next((tmp_path / "dump" / "archives").rglob("*.json.gz"))
        assert json.load(gzip.open(moved, "rt")) == {"a": 1}


class TestPurge:
    def test_dry_run_is_the_DEFAULT_and_removes_nothing(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        rep = s.purge_arc_data("projA", "arc1")
        assert rep["dry_run"] is True
        assert rep["records_deleted"] == 0 and rep["records_matched"] == 1
        assert len(s.list_analyses("projA", "arc1")) == 1

    def test_deletes_rows_but_KEEPS_archives_unless_asked(self, runs_root):
        """Dropping the row while keeping the payload is the recoverable choice."""
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        src = Path(s.list_analyses("projA", "arc1")[0].artifacts["result"])
        rep = s.purge_arc_data("projA", "arc1", dry_run=False)
        assert rep["records_deleted"] == 1
        assert s.list_analyses("projA", "arc1") == []
        assert src.is_file(), "archive must survive unless delete_archives=True"

    def test_delete_archives_is_a_second_explicit_opt_in(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        src = Path(s.list_analyses("projA", "arc1")[0].artifacts["result"])
        rep = s.purge_arc_data("projA", "arc1", delete_archives=True, dry_run=False)
        assert rep["archives_deleted"] == 1 and rep["bytes_freed"] > 0
        assert not src.exists()


class TestArchiveArc:
    def test_dry_run_default_reports_an_inventory_and_purges_nothing(self, runs_root, tmp_path):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        rep = s.archive_arc("projA", "arc1", tmp_path / "retired")
        assert rep["dry_run"] is True and rep["purged"] is False
        assert rep["inventory"]["records"] == 1
        assert len(s.list_analyses("projA", "arc1")) == 1

    def test_dumps_then_purges_leaving_data_only_at_the_destination(self, runs_root, tmp_path):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        src = Path(s.list_analyses("projA", "arc1")[0].artifacts["result"])
        rep = s.archive_arc("projA", "arc1", tmp_path / "retired", dry_run=False)
        assert rep["purged"]["records_deleted"] == 1
        assert s.list_analyses("projA", "arc1") == []       # gone from the live index
        assert not src.exists()                              # gone from the live store
        moved = next((tmp_path / "retired" / "archives").rglob("*.json.gz"))
        assert json.load(gzip.open(moved, "rt")) == {"a": 1}  # but preserved
        assert (tmp_path / "retired" / "manifest.json").is_file()

    def test_refuses_to_purge_when_the_dump_produced_no_manifest(self, runs_root, tmp_path, monkeypatch):
        """Purging on a failed dump destroys data that cannot be re-fetched."""
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        monkeypatch.setattr(
            KorosArcStore, "dump_arc_data",
            lambda self, p, a, d, **k: {"manifest": str(tmp_path / "nope.json"), "records": 0},
        )
        rep = s.archive_arc("projA", "arc1", tmp_path / "retired", dry_run=False)
        assert rep["purged"] is False and "refusing to purge" in rep["error"]
        assert len(s.list_analyses("projA", "arc1")) == 1


class TestPrivateModes:
    """The store must not be world-readable.

    On the BERDL pod `$HOME` and the multi-tenant `/global_share` (mode 1777,
    root-owned) are the SAME physical array, and default creation modes put the
    index at 0644 under a 0755 home. These payloads can carry unpublished
    collaborator material, so the mode is asserted rather than assumed --
    `mkdir` and `sqlite3.connect` honour the umask, not our intent.
    """

    def test_index_dir_and_file_are_private(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        db = s.db.db_path
        assert db.is_file()
        assert db.stat().st_mode & 0o777 == 0o600, oct(db.stat().st_mode & 0o777)
        assert db.parent.stat().st_mode & 0o777 == 0o700, oct(db.parent.stat().st_mode & 0o777)

    def test_archived_payloads_are_private(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        p = Path(s.list_analyses("projA", "arc1")[0].artifacts["result"])
        assert p.stat().st_mode & 0o777 == 0o600, oct(p.stat().st_mode & 0o777)
        assert p.parent.stat().st_mode & 0o777 == 0o700
        assert p.parent.parent.stat().st_mode & 0o777 == 0o700

    def test_a_loosened_mode_is_re_tightened_on_next_connect(self, runs_root):
        """A stray chmod (or a re-provision) must not leave it open."""
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(), raw={"a": 1})
        db = s.db.db_path
        db.chmod(0o644)
        db.parent.chmod(0o755)
        KorosArcStore().list_analyses("projA", "arc1")   # any connect re-asserts
        assert db.stat().st_mode & 0o777 == 0o600
        assert db.parent.stat().st_mode & 0o777 == 0o700
