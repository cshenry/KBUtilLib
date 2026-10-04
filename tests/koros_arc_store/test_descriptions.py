"""Tests for record / arc / project descriptions.

The index could always say WHICH arc a record belonged to and never WHAT it was.
Three gaps were closed on 2026-10-04 and each is pinned here:

* ``AnalysisRecord.description`` -- first-class, so ``list_analyses`` returns it
  without opening a blob per record.
* ``describe_record`` -- the only way to describe a record that ALREADY exists,
  which is every row written before that date, including the 430 backfilled from
  the KBDL job API.
* ``describe_arc`` / ``describe_project`` -- there was previously nowhere at all
  to say what an arc or a project was; the arc's on-disk provenance has no such
  field either.

The migration test is the load-bearing one: the column was added to a schema
that already had real data behind it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kbutillib.koros_arc_store.identity import derive_analysis_id, derive_record_id
from kbutillib.koros_arc_store.records import AnalysisRecord
from kbutillib.koros_arc_store.run_db import RunDatabase
from kbutillib.koros_arc_store.store import KorosArcStore


@pytest.fixture
def runs_root(tmp_path, monkeypatch):
    arc = tmp_path / "runs" / "projA" / "arcs" / "arc1"
    arc.mkdir(parents=True)
    (arc / "PROVENANCE.json").write_text(
        json.dumps({"run_name": "arc1", "project": "projA"})
    )
    monkeypatch.setenv("KOROS_HOME", str(tmp_path))
    monkeypatch.setenv("KBDL_RUN_DB", str(tmp_path / "runs.sqlite"))
    monkeypatch.setenv("KBDL_RUN_DB_ENABLED", "1")
    return tmp_path


def _record(run_uid="u1", description=None) -> AnalysisRecord:
    aid = derive_analysis_id("kbutillib.fba", "m.json", {"media": "gmm"})
    return AnalysisRecord(
        record_id=derive_record_id(aid, run_uid), analysis_id=aid, run_uid=run_uid,
        kind="kbutillib.fba",
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z",
        producer="test", producer_version="0", subject="m.json", status="ok",
        artifacts={}, payload={"objective_value": 1.0}, trust_tier="verified",
        provenance={}, contract_version=1, project="projA", arc="arc1",
        description=description,
    )


class TestRecordDescription:
    def test_round_trips_when_set_at_record_time(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(description="baseline FBA"))
        assert s.list_analyses("projA", "arc1")[0].description == "baseline FBA"

    def test_defaults_to_none(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record())
        assert s.list_analyses("projA", "arc1")[0].description is None

    def test_describe_record_fills_one_in_afterwards(self, runs_root):
        """The whole point: an existing record was previously undescribable."""
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record())
        rid = s.list_analyses("projA", "arc1")[0].record_id
        assert s.describe_record(rid, description="added later") is True
        assert s.list_analyses("projA", "arc1")[0].description == "added later"

    def test_describe_record_can_set_detail_alone(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(description="keep me"))
        rid = s.list_analyses("projA", "arc1")[0].record_id
        assert s.describe_record(rid, detail={"why": "x"}) is True
        assert s.read_detail(rid) == {"why": "x"}
        assert s.list_analyses("projA", "arc1")[0].description == "keep me", (
            "passing detail alone must not disturb the description"
        )

    def test_unknown_record_id_returns_false_not_silent_success(self, runs_root):
        s = KorosArcStore()
        assert s.describe_record("nope", description="x") is False

    def test_empty_string_clears_explicitly(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(description="old"))
        rid = s.list_analyses("projA", "arc1")[0].record_id
        s.describe_record(rid, description="")
        assert s.list_analyses("projA", "arc1")[0].description == ""

    def test_a_rerun_does_NOT_wipe_a_human_written_description(self, runs_root):
        """The upsert COALESCEs. A producer re-running sends description=None,
        and clobbering would destroy prose nothing can regenerate."""
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record())
        rid = s.list_analyses("projA", "arc1")[0].record_id
        s.describe_record(rid, description="hand-written, precious")
        s.record_analysis("projA", "arc1", _record())          # same record_id
        assert s.list_analyses("projA", "arc1")[0].description == "hand-written, precious"


class TestScopeDescriptions:
    def test_arc_and_project_are_stored_separately(self, runs_root):
        s = KorosArcStore()
        assert s.describe_project("projA", "the project") is True
        assert s.describe_arc("projA", "arc1", "the arc") is True
        assert s.get_description("projA")["description"] == "the project"
        assert s.get_description("projA", "arc1")["description"] == "the arc"

    def test_project_level_upserts_rather_than_duplicating(self, runs_root):
        """arc_slug='' not NULL: SQLite treats NULLs as distinct in a PK, so a
        NULL would admit many rows per project and never fire the upsert."""
        s = KorosArcStore()
        s.describe_project("projA", "v1")
        s.describe_project("projA", "v2")
        assert s.get_description("projA")["description"] == "v2"
        assert len([d for d in s.list_descriptions() if d["arc"] is None]) == 1

    def test_missing_description_is_none_not_an_error(self, runs_root):
        assert KorosArcStore().get_description("projA", "nope") is None

    def test_list_descriptions_distinguishes_arc_from_project(self, runs_root):
        s = KorosArcStore()
        s.describe_project("projA", "p")
        s.describe_arc("projA", "arc1", "a")
        got = {(d["project"], d["arc"]): d["description"] for d in s.list_descriptions()}
        assert got == {("projA", None): "p", ("projA", "arc1"): "a"}


class TestCoverage:
    def test_reports_the_gap(self, runs_root):
        s = KorosArcStore()
        s.record_analysis("projA", "arc1", _record(run_uid="u1"))
        s.record_analysis("projA", "arc1", _record(run_uid="u2", description="described"))
        cov = s.describe_coverage()
        assert cov["arcs_total"] == 1 and cov["arcs_described"] == 0
        assert cov["arcs_undescribed"] == 1
        assert cov["records_described"] == 1 and cov["records_undescribed"] == 1
        s.describe_arc("projA", "arc1", "now described")
        assert s.describe_coverage()["arcs_undescribed"] == 0


class TestMigration:
    def test_adds_the_column_to_a_PRE_EXISTING_database_without_data_loss(self, tmp_path, monkeypatch):
        """The case that matters: kbhub had 430 real rows when this landed.

        CREATE TABLE IF NOT EXISTS does not add a column to a table that already
        exists, so without the explicit ALTER every read of `description` would
        raise `no such column` on exactly the installs with data worth keeping.
        """
        db = tmp_path / "old.sqlite"
        # A v1 schema: runs WITHOUT `description`.
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE runs (
                record_id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL,
                run_uid TEXT NOT NULL, project TEXT, arc_slug TEXT,
                subject TEXT NOT NULL, kind TEXT NOT NULL, producer TEXT NOT NULL,
                producer_version TEXT NOT NULL, status TEXT NOT NULL,
                trust_tier TEXT NOT NULL, provenance TEXT NOT NULL,
                contract_version INTEGER NOT NULL,
                unknown_kind INTEGER NOT NULL DEFAULT 0,
                artifacts TEXT NOT NULL, payload TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                subject_feature_count INTEGER, method_count INTEGER,
                consistency_overall REAL, consistency_metric_version TEXT,
                ic_corpus_version TEXT
            );
            CREATE TABLE subject_detail (record_id TEXT PRIMARY KEY, detail_json TEXT NOT NULL);
            """
        )
        conn.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("rid-1", "aid-1", "uid-1", "projA", "arc1", "m.json", "kbutillib.fba",
             "p", "0", "ok", "verified", "{}", 1, 0, "{}", "{}",
             "2026-01-01T00:00:00.000000Z", "2026-01-01T00:00:00.000000Z",
             None, None, None, None, None),
        )
        conn.commit()
        assert "description" not in {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
        conn.close()

        rdb = RunDatabase(db_path=db, enabled=True)
        recs = rdb.list_analyses("projA", "arc1")           # triggers the migration
        assert len(recs) == 1, "the pre-existing row must survive"
        assert recs[0].description is None
        assert rdb.describe_record("rid-1", description="described post-migration")
        assert rdb.list_analyses("projA", "arc1")[0].description == "described post-migration"

    def test_migration_is_idempotent(self, tmp_path):
        db = tmp_path / "x.sqlite"
        for _ in range(3):
            RunDatabase(db_path=db, enabled=True).list_analyses(None, None)
        cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(runs)")]
        assert cols.count("description") == 1
