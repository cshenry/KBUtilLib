"""Behaviour tests for the ``/api/arcs/{project}/{arc}/models`` data layer.

The model endpoint groups multiple records onto one model row (strictly by the
model artifact, never by subject); an arc with no records returns an empty list
rather than erroring; foreign records with no model are counted, not dropped;
re-runs are kept, dated and grouped with a run count; the row carries trust tier
and provenance; and no detail blob is read.
"""

from __future__ import annotations

from kbutillib.models_and_analyses import build_arc_models

from .conftest import make_arc, make_record


def test_multiple_records_group_onto_one_model_row(seeded_store):
    result = build_arc_models(seeded_store, "projA", "main")
    # main has a model_build and a fitness_analysis on the SAME model (obj://mX).
    assert result["model_count"] == 1
    row = result["models"][0]
    assert row["model_id"] == "model:kbdl:mX"
    assert row["analysis_count"] == 2  # two distinct analyses, one row


def test_group_by_model_id_not_subject(store):
    """Two records with the SAME model artifact but DIFFERENT subject strings
    still group onto one model row — grouping is by model_id, never subject."""
    store.seed_arc(make_arc("p", "a", run_name="A"))
    store.record_analysis(
        "p", "a",
        make_record("kbdl.model_build", "subject-form-one", model_uri="obj://same"),
    )
    store.record_analysis(
        "p", "a",
        make_record("kbutillib.fba", "subject-form-two", model_uri="obj://same",
                    run_uid="u2"),
    )
    result = build_arc_models(store, "p", "a")
    assert result["model_count"] == 1
    assert result["models"][0]["model_id"] == "model:kbdl:same"


def test_display_name_is_readable_not_the_id(seeded_store):
    """DISPLAY IS NOT IDENTITY: the row shows a name; the id stays available."""
    result = build_arc_models(seeded_store, "projA", "main")
    row = result["models"][0]
    assert row["display_name"] == "E. coli X"
    assert row["model_id"] == "model:kbdl:mX"  # id still present for copying


def test_build_metadata_surfaced(seeded_store):
    result = build_arc_models(seeded_store, "projA", "main")
    row = result["models"][0]
    assert row["genome"] == "genomeX"
    assert row["template"] == "GramNegative"
    assert row["reaction_count"] == 1200
    assert row["gene_count"] == 900


def test_analyses_carry_kind_status_and_timestamp(seeded_store):
    result = build_arc_models(seeded_store, "projA", "main")
    row = result["models"][0]
    kinds = {a["kind"] for a in row["analyses"]}
    assert kinds == {"kbdl.model_build", "kbdl.fitness_analysis"}
    for a in row["analyses"]:
        assert a["status"] == "ok"
        assert a["created_at"]
        assert "trust_tier" in a


def test_empty_arc_returns_empty_list_not_error(seeded_store):
    result = build_arc_models(seeded_store, "projA", "empty")
    assert result["models"] == []
    assert result["model_count"] == 0


def test_foreign_records_without_model_are_counted_not_dropped(seeded_store):
    """leg1 holds only annotation records — no model rows, but they are counted."""
    result = build_arc_models(seeded_store, "projA", "leg1")
    assert result["models"] == []
    assert result["foreign_record_count"] == 2


def test_reruns_kept_dated_and_grouped_with_run_count(store):
    """Re-runs do NOT collapse to one row: the analysis line carries a run count
    and each run is dated in the expanded view."""
    store.seed_arc(make_arc("p", "a", run_name="A"))
    for uid, ts in (
        ("r1", "2026-09-01T00:00:00.000000Z"),
        ("r2", "2026-09-02T00:00:00.000000Z"),
    ):
        store.record_analysis(
            "p", "a",
            make_record("kbdl.model_build", "gg", model_uri="obj://mg",
                        run_uid=uid, created_at=ts),
        )
    result = build_arc_models(store, "p", "a")
    row = result["models"][0]
    # ONE analysis line (distinct on analysis_id), but run_count == 2.
    assert row["analysis_count"] == 1
    analysis = row["analyses"][0]
    assert analysis["run_count"] == 2
    run_dates = {r["created_at"] for r in analysis["runs"]}
    assert run_dates == {
        "2026-09-01T00:00:00.000000Z",
        "2026-09-02T00:00:00.000000Z",
    }


def test_inferred_flag_surfaced(store):
    store.seed_arc(make_arc("p", "a", run_name="A"))
    store.record_analysis(
        "p", "a",
        make_record("kbdl.model_build", "gi", model_uri="obj://mi",
                    payload={"provenance": "inferred", "model_name": "Inferred M"}),
    )
    result = build_arc_models(store, "p", "a")
    row = result["models"][0]
    assert row["inferred"] is True
    assert row["analyses"][0]["inferred"] is True


def test_gapfill_media_surfaced(seeded_store):
    result = build_arc_models(seeded_store, "projB", "solo")
    row = next(r for r in result["models"] if r["model_id"] == "model:file:/data/mZ.json")
    assert "Carbon-D-Glucose" in row["gapfill_media"]


def test_row_trust_tier_is_the_floor(seeded_store):
    """The row tier is the FLOOR across its analyses — never raised by a verified one."""
    result = build_arc_models(seeded_store, "projA", "main")
    row = result["models"][0]
    # main's model has hypothesis-tier analyses; the floor is hypothesis.
    assert row["trust_tier"] == "hypothesis"


def test_arc_models_reads_no_detail_blob(seeded_store):
    """BLOB DISCIPLINE: /api/arcs/.../models never opens a detail blob."""
    build_arc_models(seeded_store, "projA", "main")
    build_arc_models(seeded_store, "projB", "solo")
    assert seeded_store.blob_reads == 0
