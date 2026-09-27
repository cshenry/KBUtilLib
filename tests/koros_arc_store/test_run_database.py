"""Tests for the koros_arc_store run database (Responsibility 2).

Covers the two-tier schema and its blob boundary, the split identity model
(analysis_id groups, record_id is per-run), upsert / re-run / retry semantics,
delete-and-cascade, validation rejections (stable codes), the unknown-kind
policy, the fail-soft write path and the raising read path, the floor rule and
its anti-laundering guarantee, and the canonical_json / identity helpers.

No network is used. Fixtures live under tests/fixtures/koros_arc_store/.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from kbutillib.koros_arc_store import (
    AnalysisRecord,
    KorosArcStore,
    RecordNotFound,
    RecordValidationError,
    RunDatabase,
    TrustTier,
    canonical_json,
    derive_analysis_id,
    derive_record_id,
    display_id,
    floor_tier,
    resolve_db_path,
)
from kbutillib.koros_arc_store.run_db import SOFT_FAIL_PREFIX

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "koros_arc_store"

# ── record builders ───────────────────────────────────────────────────────────


def make_record(
    *,
    kind: str = "kbdl.annotation",
    subject: str = "subject-A",
    significant_params=None,
    run_uid: str = "run-uid-1",
    status: str = "ok",
    trust_tier: str = "verified",
    provenance=None,
    producer: str = "toolX",
    producer_version: str = "1.2.3",
    payload=None,
    contract_version: int = 1,
    created_at: str = "2026-01-02T03:04:05.678901Z",
    **summary,
) -> AnalysisRecord:
    """Build a valid AnalysisRecord with a correctly derived split identity."""
    if significant_params is None:
        significant_params = {"method": "BAKTA", "bakta_db": "v5"}
    analysis_id = derive_analysis_id(kind, subject, significant_params)
    record_id = derive_record_id(analysis_id, run_uid)
    if provenance is None:
        provenance = (
            {}
            if trust_tier == "verified"
            else {
                "bridge_kind": "sequence_homolog",
                "metric": {"name": "identity", "value": 98, "units": "percent"},
                "source": {"db": "uniref", "accession": "UR90"},
            }
        )
    return AnalysisRecord(
        record_id=record_id,
        analysis_id=analysis_id,
        run_uid=run_uid,
        kind=kind,
        created_at=created_at,
        producer=producer,
        producer_version=producer_version,
        subject=subject,
        status=status,
        artifacts={},
        payload=payload,
        trust_tier=trust_tier,
        provenance=provenance,
        contract_version=contract_version,
        **summary,
    )


@pytest.fixture
def db(tmp_path):
    """A run database rooted in a temp file, enabled regardless of environment."""
    return RunDatabase(db_path=tmp_path / "runs.sqlite", enabled=True)


# ── canonical_json + identity ────────────────────────────────────────────────


class TestIdentity:
    def test_canonical_json_sorts_and_compacts(self):
        assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
        assert canonical_json({"z": [3, 2, 1]}) == '{"z":[3,2,1]}'

    def test_analysis_id_is_deterministic_across_producers(self):
        # Two producers ordering keys differently and casing kind/subject
        # differently derive the SAME analysis_id.
        a = derive_analysis_id("KBDL.Annotation", "GenomeA", {"b": 2, "a": 1})
        b = derive_analysis_id("kbdl.annotation", "genomea", {"a": 1, "b": 2})
        assert a == b

    def test_different_reference_db_is_a_different_analysis(self):
        a = derive_analysis_id("kbdl.annotation", "g", {"bakta_db": "v5"})
        b = derive_analysis_id("kbdl.annotation", "g", {"bakta_db": "v6"})
        assert a != b

    def test_record_id_is_analysis_id_plus_run_uid(self):
        aid = derive_analysis_id("kbdl.skani", "g", {"db": "gtdb"})
        rid = derive_record_id(aid, "uid-42")
        import hashlib

        expected = hashlib.sha256((aid + "\x00" + "uid-42").encode()).hexdigest()
        assert rid == expected

    def test_float_param_rejected(self):
        with pytest.raises(RecordValidationError) as exc:
            derive_analysis_id("kbdl.annotation", "g", {"threshold": 0.9})
        assert exc.value.code == "bad_float_param"

    def test_display_id_is_truncated_only(self):
        aid = derive_analysis_id("kbdl.annotation", "g", {"a": 1})
        assert display_id(aid) == aid[:32]
        assert len(display_id(aid)) == 32
        # full id is unchanged and longer
        assert len(aid) == 64


# ── trust tiers + floor rule ─────────────────────────────────────────────────


class TestTrustTiers:
    def test_ordering_most_trusted_first(self):
        assert (
            TrustTier.VERIFIED
            < TrustTier.HOMOLOGY
            < TrustTier.HYPOTHESIS
            < TrustTier.OPINION
        )

    def test_homology_or_better(self):
        assert TrustTier.from_str("homology") <= TrustTier.HOMOLOGY
        assert TrustTier.from_str("verified") <= TrustTier.HOMOLOGY
        assert not (TrustTier.from_str("hypothesis") <= TrustTier.HOMOLOGY)

    def test_floor_is_least_trusted(self):
        assert floor_tier(["verified", "homology", "opinion"]) == "opinion"
        assert floor_tier(["verified", "verified"]) == "verified"

    def test_adding_lower_tier_lowers_floor(self):
        base = floor_tier(["verified", "homology"])
        assert base == "homology"
        lowered = floor_tier(["verified", "homology", "opinion"])
        assert TrustTier.from_str(lowered) > TrustTier.from_str(base)

    def test_adding_higher_tier_never_raises_floor(self):
        base = floor_tier(["hypothesis", "opinion"])
        assert base == "opinion"
        # adding a MORE trusted contributor must not raise the floor
        with_higher = floor_tier(["hypothesis", "opinion", "verified"])
        assert with_higher == "opinion"
        assert TrustTier.from_str(with_higher) >= TrustTier.from_str(base)

    def test_floor_empty_raises(self):
        with pytest.raises(ValueError):
            floor_tier([])

    def test_no_api_raises_a_tier(self):
        # Anti-laundering: floor_tier is the only tier-producing API and it can
        # only ever return a tier no more trusted than its least-trusted input.
        for combo in (
            ["opinion"],
            ["opinion", "verified"],
            ["hypothesis", "verified", "homology"],
        ):
            result = floor_tier(combo)
            worst = max(TrustTier.from_str(t) for t in combo)
            assert TrustTier.from_str(result) == worst


# ── the two-tier schema and its blob boundary ────────────────────────────────


class TestTwoTierSchema:
    def test_detail_stored_as_blob_and_read_back(self, db):
        rec = make_record()
        detail = json.loads(
            (FIXTURES / "subject_blobs" / "example_detail.json").read_text()
        )
        db.record_analysis("proj", "arc", rec, detail=detail)
        assert db.read_detail(rec.record_id) == detail

    def test_list_analyses_opens_no_blob(self, db, monkeypatch):
        rec = make_record()
        db.record_analysis("proj", "arc", rec, detail={"big": "blob"})

        # Use SQLite's authorizer callback (fires on every table access) to prove
        # list_analyses never touches subject_detail. We install it on the
        # connection RunDatabase opens for the read.
        touched = []
        real_open = RunDatabase._open_for_read

        def open_with_authorizer(self):
            conn = real_open(self)

            def authorizer(action, arg1, arg2, dbname, trigger):
                # SQLITE_READ (20) / others carry the table name in arg1.
                if arg1 == "subject_detail":
                    touched.append((action, arg1))
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorizer)
            return conn

        monkeypatch.setattr(RunDatabase, "_open_for_read", open_with_authorizer)

        rows = db.list_analyses("proj", "arc")
        assert len(rows) == 1
        assert rows[0].record_id == rec.record_id
        assert touched == [], f"list_analyses touched subject_detail: {touched}"

    def test_read_detail_touches_subject_detail(self, db, monkeypatch):
        # Complementary proof: read_detail DOES open the blob table.
        rec = make_record()
        db.record_analysis("proj", "arc", rec, detail={"big": "blob"})
        touched = []
        real_open = RunDatabase._open_for_read

        def open_with_authorizer(self):
            conn = real_open(self)

            def authorizer(action, arg1, arg2, dbname, trigger):
                if arg1 == "subject_detail":
                    touched.append(arg1)
                return sqlite3.SQLITE_OK

            conn.set_authorizer(authorizer)
            return conn

        monkeypatch.setattr(RunDatabase, "_open_for_read", open_with_authorizer)
        assert db.read_detail(rec.record_id) == {"big": "blob"}
        assert touched, "read_detail did not open subject_detail"

    def test_read_detail_opens_exactly_one_blob(self, db):
        rec = make_record()
        db.record_analysis("proj", "arc", rec, detail={"only": "one"})
        assert db.read_detail(rec.record_id) == {"only": "one"}

    def test_read_detail_none_when_record_exists_without_blob(self, db):
        rec = make_record()
        db.record_analysis("proj", "arc", rec)  # no detail
        assert db.read_detail(rec.record_id) is None

    def test_read_detail_raises_for_unknown_record(self, db):
        with pytest.raises(RecordNotFound):
            db.read_detail("does-not-exist")

    def test_index_exists(self, db):
        db.record_analysis("proj", "arc", make_record())
        assert db.has_index("idx_runs_analysis_created")


# ── re-run / retry / upsert semantics ────────────────────────────────────────


class TestRunSemantics:
    def test_retry_same_run_uid_replaces(self, db):
        rec = make_record(run_uid="uid-1", status="failed")
        db.record_analysis("p", "a", rec)
        # retry: same run_uid → same record_id → replace, now ok
        rec2 = make_record(run_uid="uid-1", status="ok")
        assert rec2.record_id == rec.record_id
        db.record_analysis("p", "a", rec2)
        rows = db.list_analyses("p", "a")
        assert len(rows) == 1
        assert rows[0].status == "ok"

    def test_rerun_new_run_uid_makes_second_row_sharing_analysis_id(self, db):
        r1 = make_record(run_uid="uid-1")
        r2 = make_record(run_uid="uid-2")
        assert r1.analysis_id == r2.analysis_id
        assert r1.record_id != r2.record_id
        db.record_analysis("p", "a", r1)
        db.record_analysis("p", "a", r2)
        rows = db.list_analyses("p", "a")
        assert len(rows) == 2
        assert {row.analysis_id for row in rows} == {r1.analysis_id}

    def test_missing_run_uid_rejected(self, db):
        rec = make_record()
        rec.run_uid = ""
        with pytest.raises(RecordValidationError) as exc:
            db.record_analysis("p", "a", rec)
        assert exc.value.code == "missing_run_uid"

    def test_immutable_analysis_id_change_rejected(self, db):
        rec = make_record(run_uid="uid-1")
        db.record_analysis("p", "a", rec)
        tampered = make_record(run_uid="uid-1")
        tampered.analysis_id = "different-analysis-id"
        # keep same record_id to trigger the immutability guard
        tampered.record_id = rec.record_id
        with pytest.raises(RecordValidationError) as exc:
            db.record_analysis("p", "a", tampered)
        assert exc.value.code == "immutable_field_changed"

    def test_immutable_run_uid_change_rejected(self, db):
        rec = make_record(run_uid="uid-1")
        db.record_analysis("p", "a", rec)
        tampered = make_record(run_uid="uid-1")
        tampered.run_uid = "uid-2"
        tampered.record_id = rec.record_id  # same PK, different run_uid
        with pytest.raises(RecordValidationError) as exc:
            db.record_analysis("p", "a", tampered)
        assert exc.value.code == "immutable_field_changed"

    def test_upsert_preserves_created_at(self, db):
        rec = make_record(run_uid="uid-1", created_at="2026-01-01T00:00:00.000000Z")
        db.record_analysis("p", "a", rec)
        rec2 = make_record(run_uid="uid-1", created_at="2099-12-31T23:59:59.000000Z")
        db.record_analysis("p", "a", rec2)
        row = db.list_analyses("p", "a")[0]
        assert row.created_at == "2026-01-01T00:00:00.000000Z"

    def test_rerecord_without_detail_leaves_blob(self, db):
        rec = make_record(run_uid="uid-1")
        db.record_analysis("p", "a", rec, detail={"keep": "me"})
        db.record_analysis("p", "a", rec)  # no detail this time
        assert db.read_detail(rec.record_id) == {"keep": "me"}

    def test_failed_to_ok_status_transition_allowed(self, db):
        rec = make_record(run_uid="uid-1", status="failed")
        db.record_analysis("p", "a", rec)
        rec2 = make_record(run_uid="uid-1", status="ok")
        db.record_analysis("p", "a", rec2)
        assert db.list_analyses("p", "a")[0].status == "ok"


# ── list_analyses filters ────────────────────────────────────────────────────


class TestListFilters:
    def test_latest_only_returns_one_newest_per_analysis(self, db):
        r1 = make_record(run_uid="uid-1", created_at="2026-01-01T00:00:00.000000Z")
        r2 = make_record(run_uid="uid-2", created_at="2026-06-01T00:00:00.000000Z")
        db.record_analysis("p", "a", r1)
        db.record_analysis("p", "a", r2)
        latest = db.list_analyses("p", "a", latest_only=True)
        assert len(latest) == 1
        assert latest[0].record_id == r2.record_id  # newest

    def test_kind_and_analysis_id_conjunctive(self, db):
        r1 = make_record(kind="kbdl.annotation", run_uid="u1")
        r2 = make_record(kind="kbdl.skani", subject="subject-B", run_uid="u2")
        db.record_analysis("p", "a", r1)
        db.record_analysis("p", "a", r2)
        only = db.list_analyses("p", "a", kind="kbdl.skani")
        assert [x.record_id for x in only] == [r2.record_id]
        both = db.list_analyses(
            "p", "a", kind="kbdl.annotation", analysis_id=r2.analysis_id
        )
        assert both == []  # conjunction: kind matches r1, analysis_id matches r2

    def test_ordering_is_deterministic_on_timestamp_tie(self, db):
        # Same created_at across two records → tie-break by updated_at then record_id.
        ts = "2026-01-01T00:00:00.000000Z"
        r1 = make_record(run_uid="u1", created_at=ts)
        r2 = make_record(run_uid="u2", created_at=ts)
        db.record_analysis("p", "a", r1)
        db.record_analysis("p", "a", r2)
        rows = db.list_analyses("p", "a")
        # deterministic order; the two share created_at & (near) updated_at, so
        # record_id DESC decides
        ids = [r.record_id for r in rows]
        assert ids == sorted([r1.record_id, r2.record_id], reverse=True)


# ── delete ───────────────────────────────────────────────────────────────────


class TestDelete:
    def test_delete_removes_row_and_blob_returns_true(self, db):
        rec = make_record()
        db.record_analysis("p", "a", rec, detail={"x": 1})
        assert db.delete_record(rec.record_id) is True
        assert db.list_analyses("p", "a") == []
        with pytest.raises(RecordNotFound):
            db.read_detail(rec.record_id)

    def test_delete_absent_returns_false_no_raise(self, db):
        assert db.delete_record("nope") is False

    def test_delete_one_of_two_leaves_the_other(self, db):
        r1 = make_record(run_uid="u1")
        r2 = make_record(run_uid="u2")
        db.record_analysis("p", "a", r1, detail={"one": 1})
        db.record_analysis("p", "a", r2, detail={"two": 2})
        assert db.delete_record(r1.record_id) is True
        remaining = db.list_analyses("p", "a")
        assert [r.record_id for r in remaining] == [r2.record_id]
        assert db.read_detail(r2.record_id) == {"two": 2}


# ── validation rejections ────────────────────────────────────────────────────


class TestValidation:
    def test_bad_status_rejected(self, db):
        rec = make_record()
        rec.status = "OK"  # case variant → rejected (S9)
        with pytest.raises(RecordValidationError) as exc:
            db.record_analysis("p", "a", rec)
        assert exc.value.code == "bad_status"

    def test_non_verified_without_provenance_rejected(self, db):
        rec = make_record(trust_tier="homology", provenance={})
        with pytest.raises(RecordValidationError) as exc:
            db.record_analysis("p", "a", rec)
        assert exc.value.code == "missing_provenance"

    def test_bad_bridge_kind_rejected(self, db):
        rec = make_record(
            trust_tier="homology",
            provenance={
                "bridge_kind": "homology",  # a TIER name, not a bridge → rejected
                "metric": {"name": "x", "value": 1},
            },
        )
        with pytest.raises(RecordValidationError) as exc:
            db.record_analysis("p", "a", rec)
        assert exc.value.code == "bad_bridge_kind"

    def test_verified_may_omit_provenance(self, db):
        rec = make_record(trust_tier="verified", provenance={})
        db.record_analysis("p", "a", rec)  # no raise
        assert len(db.list_analyses("p", "a")) == 1

    def test_bad_timestamp_rejected(self, db):
        rec = make_record(created_at="2026-01-01 00:00:00")  # not the Z format
        with pytest.raises(RecordValidationError) as exc:
            db.record_analysis("p", "a", rec)
        assert exc.value.code == "bad_timestamp"

    def test_bad_artifact_uri_rejected(self, db):
        rec = make_record()
        rec.artifacts = {"out": "http://example.com/x"}
        with pytest.raises(RecordValidationError) as exc:
            db.record_analysis("p", "a", rec)
        assert exc.value.code == "bad_artifact_uri"

    def test_accepted_artifact_uris(self, db):
        rec = make_record()
        rec.artifacts = {
            "abs": "/data/out.json",
            "file2": "file:///data/out2.json",
            "file3": "file:///data/out3.json",
            "obj": "obj://12345.6.7",
        }
        db.record_analysis("p", "a", rec)
        row = db.list_analyses("p", "a")[0]
        # file:/// normalised to file:// + absolute path
        assert row.artifacts["file2"] == "file:///data/out2.json"
        assert row.artifacts["abs"] == "/data/out.json"
        assert row.artifacts["obj"] == "obj://12345.6.7"

    def test_subject_stored_verbatim(self, db):
        rec = make_record(subject="Genome_MixedCase-01")
        db.record_analysis("p", "a", rec)
        assert db.list_analyses("p", "a")[0].subject == "Genome_MixedCase-01"


# ── unknown-kind policy ──────────────────────────────────────────────────────


class TestUnknownKind:
    def test_malformed_kind_stored_and_flagged_two(self, db):
        rec = make_record(kind="NotNamespaced")
        db.record_analysis("p", "a", rec)
        row = db.list_analyses("p", "a")[0]
        assert row.unknown_kind == 2  # malformed

    def test_foreign_wellformed_kind_flagged_one(self, tmp_path):
        db = RunDatabase(
            db_path=tmp_path / "r.sqlite",
            enabled=True,
            known_kinds={"kbdl.annotation"},
        )
        rec = make_record(kind="kbutillib.fba", subject="s")
        db.record_analysis("p", "a", rec)
        row = db.list_analyses("p", "a")[0]
        assert row.unknown_kind == 1  # well-formed but foreign

    def test_known_kind_flag_zero(self, tmp_path):
        db = RunDatabase(
            db_path=tmp_path / "r.sqlite",
            enabled=True,
            known_kinds={"kbdl.annotation"},
        )
        db.record_analysis("p", "a", make_record(kind="kbdl.annotation"))
        assert db.list_analyses("p", "a")[0].unknown_kind == 0

    def test_unknown_kind_counted_not_dropped(self, db):
        db.record_analysis("p", "a", make_record(kind="weird", run_uid="u1"))
        db.record_analysis("p", "a", make_record(kind="kbdl.annotation", run_uid="u2"))
        rows = db.list_analyses("p", "a")
        assert len(rows) == 2  # neither dropped
        flags = sorted(r.unknown_kind for r in rows)
        assert flags == [0, 2]


# ── null project/arc (unattributed) ──────────────────────────────────────────


class TestUnattributed:
    def test_null_project_arc_stored_and_read(self, db):
        rec = make_record()
        db.record_analysis(None, None, rec)
        rows = db.list_analyses(None, None)
        assert len(rows) == 1
        assert rows[0].project is None
        assert rows[0].arc is None


# ── payload size smell ───────────────────────────────────────────────────────


class TestPayloadSize:
    def test_large_payload_stored_with_warning(self, db, caplog):
        big = {"blob": "x" * (65 * 1024)}
        rec = make_record(payload=big)
        with caplog.at_level(logging.WARNING, logger="kbutillib.koros_arc_store"):
            db.record_analysis("p", "a", rec)
        assert any("koros_arc_store_payload_large" in r.message for r in caplog.records)
        # stored anyway
        assert db.list_analyses("p", "a")[0].payload == big


# ── fail-soft write path / raising read path ─────────────────────────────────


class TestReadWriteAsymmetry:
    def test_write_failure_is_soft(self, tmp_path, caplog):
        # Point db_path at a path whose parent cannot be created (a file, not dir).
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        db = RunDatabase(db_path=blocker / "sub" / "runs.sqlite", enabled=True)
        rec = make_record()
        with caplog.at_level(logging.WARNING, logger="kbutillib.koros_arc_store"):
            db.record_analysis("p", "a", rec)  # must NOT raise
        assert db.failed_write_count == 1
        assert any(SOFT_FAIL_PREFIX in r.message for r in caplog.records)

    def test_read_against_unopenable_raises(self, tmp_path):
        # A path whose parent is a file cannot be opened for read → raises.
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        db = RunDatabase(db_path=blocker / "sub" / "runs.sqlite", enabled=True)
        with pytest.raises(Exception):
            db.list_analyses("p", "a")

    def test_disabled_db_is_noop_on_write(self, tmp_path):
        db = RunDatabase(db_path=tmp_path / "r.sqlite", enabled=False)
        db.record_analysis("p", "a", make_record())  # no-op, no raise
        # enabling and reading yields nothing written
        assert db.failed_write_count == 0


# ── path precedence ──────────────────────────────────────────────────────────


class TestPathPrecedence:
    def test_explicit_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KBDL_RUN_DB", str(tmp_path / "env.sqlite"))
        assert (
            resolve_db_path(tmp_path / "explicit.sqlite")
            == tmp_path / "explicit.sqlite"
        )

    def test_env_used_when_no_explicit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KBDL_RUN_DB", str(tmp_path / "env.sqlite"))
        assert resolve_db_path() == tmp_path / "env.sqlite"

    def test_default_home(self, monkeypatch):
        monkeypatch.delenv("KBDL_RUN_DB", raising=False)
        assert resolve_db_path() == Path.home() / ".kbdl" / "runs.sqlite"


# ── the interface reaches through KorosArcStore ──────────────────────────────


class TestStoreDelegation:
    def test_store_exposes_record_methods(self, tmp_path):
        runs_root = tmp_path / "runs"
        runs_root.mkdir()
        store = KorosArcStore(
            runs_root=runs_root, db_path=tmp_path / "runs.sqlite", db_enabled=True
        )
        rec = make_record()
        store.record_analysis("p", "a", rec, detail={"d": 1})
        assert len(store.list_analyses("p", "a")) == 1
        assert store.read_detail(rec.record_id) == {"d": 1}
        assert store.delete_record(rec.record_id) is True
        assert store.failed_write_count == 0


# ── contract guardrail: no KING/KOROS imports ────────────────────────────────


class TestNoForbiddenImports:
    def test_no_king_backend_or_koros_repo_imports(self):
        src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "kbutillib"
            / "koros_arc_store"
        )
        forbidden = (
            "king_backend",
            "import king",
            "import koros",
            "semcat",
            "narrative_connector",
        )
        for py in src.glob("*.py"):
            text = py.read_text(encoding="utf-8")
            for token in forbidden:
                assert token not in text, (py, token)
