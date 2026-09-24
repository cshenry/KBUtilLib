"""The KOROS arc store CONTRACT SUITE — one set of assertions, run against BOTH
the real SQLite-backed :class:`KorosArcStore` and the in-memory
:class:`FakeKorosArcStore` in the same run.

The whole point of shipping the fake from KBUtilLib is that a consumer app tests
against behaviour the real store actually has. This file is where that guarantee
is enforced: every rule below is asserted against both implementations through a
single parameterised ``store`` fixture. If adding an assertion here breaks the
fake, the fake was lying — that is the suite working, not a reason to weaken the
assertion.

A handful of assertions are REAL-STORE-ONLY because they concern the SQLite DDL
the fake deliberately does not have (WAL, the ``(analysis_id, created_at)``
index, the ``BEGIN IMMEDIATE`` delete transaction, db-path precedence). Those
tests carry ``@pytest.mark.not_applicable_for_fake`` and the parameterisation
SKIPS them for the fake with the exact reason string ``not_applicable_for_fake``
— never a bare skip, because a bare skip is exactly what "skipped silently"
meant in decision S14.

No network is used. Fixtures live under tests/fixtures/koros_arc_store/.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import pytest

from kbutillib.koros_arc_store import (
    AnalysisRecord,
    KorosArcStore,
    RecordNotFound,
    RecordValidationError,
    TrustTier,
    derive_analysis_id,
    derive_record_id,
    floor_tier,
)
from kbutillib.koros_arc_store_testing import FakeKorosArcStore

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "koros_arc_store"
RUNS_TREE = FIXTURES / "runs_tree"


# ── the parameterised store fixture (S27) ──────────────────────────────────────


def _make_real(tmp_path) -> KorosArcStore:
    """A real store rooted at the fixture runs tree with a fresh temp database."""
    return KorosArcStore(
        runs_root=RUNS_TREE,
        db_path=tmp_path / "runs.sqlite",
        db_enabled=True,
    )


def _make_fake(tmp_path) -> FakeKorosArcStore:
    """A fake store, seeded from the SAME fixture runs tree the real store reads.

    The runs-tree half of the interface is filesystem-backed on the real store
    and dict-backed on the fake, so the fake is seeded here from the identical
    on-disk fixture. This keeps the runs-tree assertions honestly comparable.
    """
    fake = FakeKorosArcStore(enabled=True)
    _seed_fake_from_tree(fake, RUNS_TREE)
    return fake


def _seed_fake_from_tree(fake: FakeKorosArcStore, runs_root: Path) -> None:
    """Populate *fake* by reading the on-disk fixture tree with a real store.

    Uses a throwaway real store purely to parse the tree into records, then
    seeds those records into the fake — so both implementations answer the
    runs-tree queries from byte-identical parsed data.
    """
    from kbutillib.koros_arc_store.records import ProjectRecord

    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        arcs_dir = entry / "arcs"
        arc_count = (
            len([a for a in arcs_dir.iterdir() if a.is_dir()])
            if arcs_dir.is_dir()
            else 0
        )
        fake.seed_project(
            ProjectRecord(name=entry.name, path=entry, arc_count=arc_count)
        )
    # Parse arcs with a real store instance (read-only) and seed them.
    reader = KorosArcStore(runs_root=runs_root, db_enabled=False)
    for proj in reader.list_projects():
        for arc in reader.list_arcs(proj.name):
            fake.seed_arc(arc)


@pytest.fixture(params=["real", "fake"])
def store(request, tmp_path):
    """Yield each implementation in turn.

    Tests marked ``not_applicable_for_fake`` are SKIPPED for the fake parameter
    with the reason string ``not_applicable_for_fake`` (S27) — this is the ONLY
    permitted skip, and it is explicit, never silent.
    """
    impl = request.param
    if impl == "fake" and request.node.get_closest_marker("not_applicable_for_fake"):
        pytest.skip("not_applicable_for_fake")
    if impl == "real":
        return _make_real(tmp_path)
    return _make_fake(tmp_path)


# ── record builders ────────────────────────────────────────────────────────────


def make_record(
    *,
    kind: str = "kbdl.annotation",
    subject: str = "subject-A",
    significant_params: Optional[dict] = None,
    run_uid: str = "run-uid-1",
    status: str = "ok",
    trust_tier: str = "verified",
    provenance: Optional[dict] = None,
    producer: str = "toolX",
    producer_version: str = "1.2.3",
    artifacts: Optional[dict] = None,
    payload: Optional[dict] = None,
    contract_version: int = 1,
    created_at: str = "2026-01-02T03:04:05.678901Z",
    **summary,
) -> AnalysisRecord:
    """Build a valid AnalysisRecord with a correctly derived split identity.

    Identity is derived through the SHARED helpers only — the suite never hashes
    locally (dev 1306). ``artifacts`` defaults to empty.
    """
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
        artifacts=artifacts if artifacts is not None else {},
        payload=payload,
        trust_tier=trust_tier,
        provenance=provenance,
        contract_version=contract_version,
        **summary,
    )


# ── runs-tree enumeration parity ───────────────────────────────────────────────


class TestRunsTreeParity:
    def test_list_projects_ordered(self, store):
        names = [p.name for p in store.list_projects()]
        assert names == sorted(names)
        assert "proj_valid" in names
        assert "proj_no_arcs" in names

    def test_project_with_no_arcs_reports_zero(self, store):
        arcs = store.list_arcs("proj_no_arcs")
        assert arcs == []

    def test_unknown_project_raises(self, store):
        with pytest.raises(RecordNotFound):
            store.list_arcs("no_such_project")

    def test_valid_arc_reads_back_valid(self, store):
        arc = store.read_arc("proj_valid", "arc_ok")
        assert arc.valid is True
        assert arc.provenance is not None
        # empty inputs / tool_versions is the common live shape
        assert arc.provenance.inputs == []
        assert arc.provenance.tool_versions == {}

    def test_leg_role_arc(self, store):
        arc = store.read_arc("proj_valid", "arc_leg")
        assert arc.valid is True
        assert arc.provenance.role == "leg"
        assert arc.provenance.leg_of == "run-ok-0001"

    def test_unparseable_arc_reads_back_invalid(self, store):
        arc = store.read_arc("proj_valid", "arc_bad")
        assert arc.valid is False
        assert arc.invalid_reason == "json_parse_error"

    def test_unknown_arc_raises(self, store):
        with pytest.raises(RecordNotFound):
            store.read_arc("proj_valid", "no_such_arc")


# ── validation parity: the fake must REJECT what the real store rejects ─────────


class TestValidationParity:
    def test_artifact_outside_vocabulary_rejected(self, store):
        rec = make_record(artifacts={"report": "http://example.com/x"})
        with pytest.raises(RecordValidationError) as exc:
            store.record_analysis("p", "a", rec)
        assert exc.value.code == "bad_artifact_uri"

    def test_bare_absolute_path_accepted(self, store):
        rec = make_record(run_uid="abs", artifacts={"f": "/data/report.json"})
        store.record_analysis("p", "a", rec)
        got = store.list_analyses("p", "a")
        assert got[0].artifacts["f"] == "/data/report.json"

    def test_obj_uri_accepted(self, store):
        rec = make_record(run_uid="obj", artifacts={"f": "obj://12345.1.1"})
        store.record_analysis("p", "a", rec)
        assert store.list_analyses("p", "a")[0].artifacts["f"] == "obj://12345.1.1"

    def test_file_uri_triple_slash_normalised(self, store):
        rec = make_record(run_uid="file3", artifacts={"f": "file:///data/x.json"})
        store.record_analysis("p", "a", rec)
        # file:/// normalises to file:// + absolute path
        assert store.list_analyses("p", "a")[0].artifacts["f"] == "file:///data/x.json"

    def test_nonverified_tier_with_empty_provenance_rejected(self, store):
        rec = make_record(trust_tier="homology", provenance={})
        with pytest.raises(RecordValidationError) as exc:
            store.record_analysis("p", "a", rec)
        assert exc.value.code == "missing_provenance"

    def test_nonverified_tier_with_provenance_accepted(self, store):
        rec = make_record(
            run_uid="prov",
            trust_tier="homology",
            provenance={
                "bridge_kind": "sequence_homolog",
                "metric": {"name": "identity", "value": 90},
            },
        )
        store.record_analysis("p", "a", rec)
        assert store.list_analyses("p", "a")[0].trust_tier == "homology"

    def test_missing_run_uid_rejected(self, store):
        rec = make_record()
        rec.run_uid = ""
        with pytest.raises(RecordValidationError) as exc:
            store.record_analysis("p", "a", rec)
        assert exc.value.code == "missing_run_uid"

    def test_status_case_variant_rejected(self, store):
        rec = make_record(status="OK")
        with pytest.raises(RecordValidationError) as exc:
            store.record_analysis("p", "a", rec)
        assert exc.value.code == "bad_status"

    def test_float_significant_param_rejected(self, store):
        # The float is rejected at identity-derivation time (bad_float_param).
        with pytest.raises(RecordValidationError) as exc:
            make_record(significant_params={"bad_float_param": 0.1})
        assert exc.value.code == "bad_float_param"

    def test_contract_version_mismatch_rejected(self, store):
        from kbutillib.koros_arc_store import ContractVersionMismatch

        rec = make_record(run_uid="cv", contract_version=2)
        with pytest.raises(ContractVersionMismatch):
            store.record_analysis("p", "a", rec)


# ── upsert / re-record / re-run / retry parity ─────────────────────────────────


class TestUpsertAndIdentityParity:
    def test_rerecord_replaces_never_duplicates(self, store):
        rec = make_record(run_uid="u1", producer_version="1.0")
        store.record_analysis("p", "a", rec)
        rec2 = make_record(run_uid="u1", producer_version="2.0")
        store.record_analysis("p", "a", rec2)
        rows = store.list_analyses("p", "a")
        assert len(rows) == 1
        assert rows[0].producer_version == "2.0"

    def test_two_producers_derive_same_record_id(self, store):
        # Different key order and casing -> identical record_id via shared helper.
        a = make_record(
            kind="KBDL.Annotation",
            subject="GenomeA",
            significant_params={"b": 2, "a": 1},
            run_uid="shared-uid",
        )
        b = make_record(
            kind="kbdl.annotation",
            subject="genomea",
            significant_params={"a": 1, "b": 2},
            run_uid="shared-uid",
        )
        assert a.record_id == b.record_id
        assert a.analysis_id == b.analysis_id

    def test_subject_stored_verbatim(self, store):
        # identity lower-cases kind/subject, but the record stores subject as-is.
        rec = make_record(subject="GenomeA_MixedCase", run_uid="verbatim")
        store.record_analysis("p", "a", rec)
        assert store.list_analyses("p", "a")[0].subject == "GenomeA_MixedCase"

    def test_rerun_produces_second_row_sharing_analysis_id(self, store):
        r1 = make_record(run_uid="run-1", created_at="2026-01-02T00:00:00.000000Z")
        r2 = make_record(run_uid="run-2", created_at="2026-01-03T00:00:00.000000Z")
        store.record_analysis("p", "a", r1)
        store.record_analysis("p", "a", r2)
        rows = store.list_analyses("p", "a")
        assert len(rows) == 2
        assert {r.analysis_id for r in rows} == {r1.analysis_id}
        assert r1.analysis_id == r2.analysis_id
        assert {r.record_id for r in rows} == {r1.record_id, r2.record_id}

    def test_retry_replaces_same_run_uid(self, store):
        r1 = make_record(run_uid="retry", status="failed")
        r2 = make_record(run_uid="retry", status="ok")
        assert r1.record_id == r2.record_id
        store.record_analysis("p", "a", r1)
        store.record_analysis("p", "a", r2)
        rows = store.list_analyses("p", "a")
        assert len(rows) == 1
        assert rows[0].status == "ok"

    def test_latest_only_returns_newest_per_analysis(self, store):
        old = make_record(run_uid="old", created_at="2026-01-01T00:00:00.000000Z")
        new = make_record(run_uid="new", created_at="2026-06-01T00:00:00.000000Z")
        store.record_analysis("p", "a", old)
        store.record_analysis("p", "a", new)
        latest = store.list_analyses("p", "a", latest_only=True)
        assert len(latest) == 1
        assert latest[0].record_id == new.record_id

    def test_analysis_id_filter(self, store):
        r1 = make_record(subject="subj-1", run_uid="s1")
        r2 = make_record(subject="subj-2", run_uid="s2")
        store.record_analysis("p", "a", r1)
        store.record_analysis("p", "a", r2)
        got = store.list_analyses("p", "a", analysis_id=r1.analysis_id)
        assert [r.record_id for r in got] == [r1.record_id]

    def test_immutable_analysis_id_change_rejected(self, store):
        rec = make_record(run_uid="imm")
        store.record_analysis("p", "a", rec)
        tampered = make_record(run_uid="imm")
        tampered.analysis_id = "0" * 64  # same record_id key, different analysis_id
        tampered.record_id = rec.record_id
        with pytest.raises(RecordValidationError) as exc:
            store.record_analysis("p", "a", tampered)
        assert exc.value.code == "immutable_field_changed"

    def test_immutable_run_uid_change_rejected(self, store):
        rec = make_record(run_uid="imm2")
        store.record_analysis("p", "a", rec)
        tampered = make_record(run_uid="imm2")
        tampered.run_uid = "different-uid"
        tampered.record_id = rec.record_id  # collide on the key
        with pytest.raises(RecordValidationError) as exc:
            store.record_analysis("p", "a", tampered)
        assert exc.value.code == "immutable_field_changed"


# ── delete parity ──────────────────────────────────────────────────────────────


class TestDeleteParity:
    def test_delete_removes_row_and_blob(self, store):
        rec = make_record(run_uid="del")
        store.record_analysis("p", "a", rec, detail={"x": 1})
        assert store.read_detail(rec.record_id) == {"x": 1}
        assert store.delete_record(rec.record_id) is True
        assert store.list_analyses("p", "a") == []
        with pytest.raises(RecordNotFound):
            store.read_detail(rec.record_id)

    def test_delete_absent_returns_false(self, store):
        assert store.delete_record("no-such-record-id") is False

    def test_delete_one_of_two_leaves_the_other(self, store):
        r1 = make_record(run_uid="a1", created_at="2026-01-01T00:00:00.000000Z")
        r2 = make_record(run_uid="a2", created_at="2026-01-02T00:00:00.000000Z")
        store.record_analysis("p", "a", r1)
        store.record_analysis("p", "a", r2)
        assert store.delete_record(r1.record_id) is True
        remaining = [r.record_id for r in store.list_analyses("p", "a")]
        assert remaining == [r2.record_id]


# ── unknown-kind policy parity (0/1/2, S43) ────────────────────────────────────


class TestUnknownKindParity:
    def test_known_kind_flagged_zero(self, tmp_path):
        # known_kinds must be supplied at construction on BOTH implementations.
        real = KorosArcStore(
            runs_root=RUNS_TREE,
            db_path=tmp_path / "runs.sqlite",
            db_enabled=True,
            known_kinds={"kbdl.annotation"},
        )
        fake = FakeKorosArcStore(enabled=True, known_kinds={"kbdl.annotation"})
        for impl in (real, fake):
            rec = make_record(kind="kbdl.annotation", run_uid="k0")
            impl.record_analysis("p", "a", rec)
            assert impl.list_analyses("p", "a")[0].unknown_kind == 0

    def test_wellformed_unregistered_kind_flagged_one(self, tmp_path):
        real = KorosArcStore(
            runs_root=RUNS_TREE,
            db_path=tmp_path / "runs.sqlite",
            db_enabled=True,
            known_kinds={"kbdl.annotation"},
        )
        fake = FakeKorosArcStore(enabled=True, known_kinds={"kbdl.annotation"})
        for impl in (real, fake):
            rec = make_record(kind="kbdl.unregistered", run_uid="k1")
            impl.record_analysis("p", "a", rec)
            rows = impl.list_analyses("p", "a")
            assert len(rows) == 1  # stored, never dropped
            assert rows[0].unknown_kind == 1  # flagged

    def test_malformed_kind_flagged_two(self, store):
        # A malformed kind (no dotted namespace) is STORED, FLAGGED 2, COUNTED —
        # never refused. (dev 1302: malformed is 2, not 1; S5 superseded by S43.)
        rec = make_record(kind="NotAKind", run_uid="k2")
        store.record_analysis("p", "a", rec)
        rows = store.list_analyses("p", "a")
        assert len(rows) == 1
        assert rows[0].unknown_kind == 2


# ── null attribution parity ────────────────────────────────────────────────────


class TestNullAttributionParity:
    def test_null_project_arc_reads_back_unattributed(self, store):
        rec = make_record(run_uid="unattr")
        store.record_analysis(None, None, rec)
        rows = store.list_analyses(None, None)
        assert len(rows) == 1
        assert rows[0].project is None
        assert rows[0].arc is None

    def test_attributed_rows_not_returned_for_null_query(self, store):
        store.record_analysis("p", "a", make_record(run_uid="attr"))
        assert store.list_analyses(None, None) == []


# ── summary-column NULL parity ─────────────────────────────────────────────────


class TestSummaryColumnParity:
    def test_omitted_summary_columns_are_null(self, store):
        rec = make_record(run_uid="sum")  # summary columns omitted
        store.record_analysis("p", "a", rec)
        row = store.list_analyses("p", "a")[0]
        assert row.subject_feature_count is None
        assert row.method_count is None
        assert row.consistency_overall is None

    def test_supplied_summary_columns_round_trip(self, store):
        rec = make_record(run_uid="sum2", subject_feature_count=7, method_count=2)
        store.record_analysis("p", "a", rec)
        row = store.list_analyses("p", "a")[0]
        assert row.subject_feature_count == 7
        assert row.method_count == 2


# ── detail-blob write / preserve parity ────────────────────────────────────────


class TestDetailBlobParity:
    def test_detail_argument_writes_blob(self, store):
        rec = make_record(run_uid="d1")
        store.record_analysis("p", "a", rec, detail={"g": [1, 2, 3]})
        assert store.read_detail(rec.record_id) == {"g": [1, 2, 3]}

    def test_rerecord_without_detail_preserves_blob(self, store):
        rec = make_record(run_uid="d2")
        store.record_analysis("p", "a", rec, detail={"first": True})
        # upsert with no detail argument must leave the blob in place (S6)
        store.record_analysis("p", "a", make_record(run_uid="d2", status="partial"))
        assert store.read_detail(rec.record_id) == {"first": True}

    def test_read_detail_none_when_no_blob(self, store):
        rec = make_record(run_uid="d3")
        store.record_analysis("p", "a", rec)  # no detail
        assert store.read_detail(rec.record_id) is None

    def test_read_detail_unknown_record_raises(self, store):
        with pytest.raises(RecordNotFound):
            store.read_detail("no-such-record-id")


# ── the blob boundary, made observable ─────────────────────────────────────────


class TestBlobBoundaryParity:
    """The rule most likely to be violated invisibly: list_analyses and the
    summary queries open NO blob; read_detail opens exactly one.

    On the fake this is asserted directly against its ``blob_reads`` counter. On
    the real store the equivalent guarantee is structural (list_analyses SELECTs
    only ``runs`` columns and never joins ``subject_detail``); here we assert the
    OBSERVABLE behaviour that holds for both — a summary read returns the row's
    precomputed columns without needing the blob, and read_detail is what
    surfaces the blob.
    """

    def test_list_and_summary_open_no_blob_fake(self):
        fake = FakeKorosArcStore(enabled=True)
        rec = make_record(run_uid="bb", subject_feature_count=5)
        fake.record_analysis("p", "a", rec, detail={"big": "blob"})
        before = fake.blob_reads
        rows = fake.list_analyses("p", "a")
        _ = rows[0].subject_feature_count  # summary column, no blob needed
        _ = fake.list_analyses("p", "a", latest_only=True)
        assert fake.blob_reads == before  # NO blob opened by list/summary

    def test_read_detail_opens_exactly_one_blob_fake(self):
        fake = FakeKorosArcStore(enabled=True)
        rec = make_record(run_uid="bb2")
        fake.record_analysis("p", "a", rec, detail={"big": "blob"})
        before = fake.blob_reads
        fake.read_detail(rec.record_id)
        assert fake.blob_reads == before + 1  # exactly one

    def test_summary_columns_available_without_detail_both(self, store):
        # Behaviour that holds for BOTH: the navigable/summary answer comes from
        # the row, never the blob.
        rec = make_record(run_uid="bb3", subject_feature_count=9)
        store.record_analysis("p", "a", rec, detail={"big": "blob"})
        row = store.list_analyses("p", "a")[0]
        assert row.subject_feature_count == 9


# ── trust-tier floor parity (both directions) ──────────────────────────────────


class TestFloorParity:
    """floor_tier and TrustTier are module-level helpers shared by both
    implementations, so they are asserted once but they are part of the same
    contract both stores bind to.
    """

    def test_adding_lower_tier_lowers_floor(self):
        base = floor_tier(["verified", "homology"])
        lowered = floor_tier(["verified", "homology", "opinion"])
        assert TrustTier.from_str(lowered) > TrustTier.from_str(base)

    def test_adding_higher_tier_never_raises_floor(self):
        base = floor_tier(["hypothesis", "opinion"])
        with_higher = floor_tier(["hypothesis", "opinion", "verified"])
        assert with_higher == base  # never raised
        assert TrustTier.from_str(with_higher) >= TrustTier.from_str(base)

    def test_homology_or_better_set(self):
        candidates = ["verified", "homology", "hypothesis", "opinion"]
        homology_or_better = {
            t for t in candidates if TrustTier.from_str(t) <= TrustTier.HOMOLOGY
        }
        assert homology_or_better == {"verified", "homology"}


# ── REAL-STORE-ONLY assertions (marked not_applicable_for_fake, S27) ───────────


@pytest.mark.not_applicable_for_fake
class TestRealStoreOnly:
    """DDL-level guarantees the fake deliberately does not have (S14). Each is
    SKIPPED for the fake by the ``store`` fixture with reason
    ``not_applicable_for_fake`` — explicit, never silent.
    """

    def test_analysis_created_index_exists(self, store):
        # The (analysis_id, created_at) index is a real-store-only DDL concern.
        store.record_analysis("p", "a", make_record(run_uid="idx"))
        assert store.db.has_index("idx_runs_analysis_created") is True

    def test_wal_journal_mode(self, store):
        # WAL is enabled at initialisation so a reader is not blocked (S36).
        store.record_analysis("p", "a", make_record(run_uid="wal"))
        conn = store.db._open_for_read()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode.lower() == "wal"

    def test_db_cannot_be_written_does_not_raise_into_caller(self, tmp_path):
        # A database that cannot be written must NOT raise into the caller (S10)
        # — the write is fail-soft and increments the soft-fail counter. This is
        # a real-store-only property; the fake's writes are pure in-memory and
        # cannot fail this way, which is why the class is not_applicable_for_fake.
        bad_path = tmp_path / "nonexistent-dir" / "sub" / "runs.sqlite"
        # Make the parent unwritable by pointing at a file where a dir is needed.
        blocker = tmp_path / "nonexistent-dir"
        blocker.write_text("not a directory")
        real = KorosArcStore(runs_root=RUNS_TREE, db_path=bad_path, db_enabled=True)
        before = real.failed_write_count
        # Must return, not raise.
        real.record_analysis("p", "a", make_record(run_uid="soft"))
        assert real.failed_write_count == before + 1

    def test_db_path_precedence(self, tmp_path, monkeypatch):
        # Explicit db_path beats $KBDL_RUN_DB (S22) — real-store resolution.
        from kbutillib.koros_arc_store import resolve_db_path

        monkeypatch.setenv("KBDL_RUN_DB", str(tmp_path / "env.sqlite"))
        explicit = tmp_path / "explicit.sqlite"
        assert resolve_db_path(explicit) == explicit


def test_fixture_seeded_sqlite_is_readable(tmp_path):
    """The committed seeded SQLite fixture opens and reports its seeded row.

    Copied to a temp path first so the test never writes the committed fixture.
    """
    seeded = FIXTURES / "runs_db" / "seeded.sqlite"
    assert seeded.is_file(), f"missing seeded fixture: {seeded}"
    dst = tmp_path / "seeded.sqlite"
    shutil.copy(seeded, dst)
    store = KorosArcStore(runs_root=RUNS_TREE, db_path=dst, db_enabled=True)
    rows = store.list_analyses("proj_valid", "arc_ok")
    assert len(rows) == 1
    assert rows[0].producer == "seedTool"
    # the blob tier is reachable for the seeded record
    assert store.read_detail(rows[0].record_id)["schema"] == "example.detail.v1"
