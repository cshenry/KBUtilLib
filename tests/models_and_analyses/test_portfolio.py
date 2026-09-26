"""Behaviour tests for the ``/api/portfolio`` data layer.

Against the shipped fake store: portfolio counts are correct and exclude foreign
kinds; an arc with only annotation records reports zero models and a non-zero
other count; empty arcs appear flagged; legs nest under parents; unattributed
records are exposed distinctly; counts are distinct on analysis_id; neither
endpoint reads a detail blob.
"""

from __future__ import annotations

from kbutillib.models_and_analyses import build_portfolio

from .conftest import make_arc, make_record


def _arc(portfolio, project, slug):
    """Return the arc node for (project, slug), searching nested children too."""
    for proj in portfolio["projects"]:
        if proj["name"] != project:
            continue

        def walk(nodes):
            for n in nodes:
                if n["slug"] == slug:
                    return n
                found = walk(n.get("children", []))
                if found:
                    return found
            return None

        return walk(proj["arcs"])
    return None


def test_portfolio_lists_every_project(seeded_store):
    portfolio = build_portfolio(seeded_store)
    names = {p["name"] for p in portfolio["projects"]}
    assert names == {"projA", "projB"}


def test_owned_kinds_counted_in_their_buckets(seeded_store):
    portfolio = build_portfolio(seeded_store)
    main = _arc(portfolio, "projA", "main")
    assert main["counts"]["models_built"] == 1
    assert main["counts"]["fitness_analyses"] == 1


def test_foreign_kinds_excluded_from_owned_buckets_and_counted_as_other(seeded_store):
    """A foreign kind (kbdl.annotation) is NEVER counted as a model — it is other."""
    portfolio = build_portfolio(seeded_store)
    main = _arc(portfolio, "projA", "main")
    # The one kbdl.annotation record must land in other, not models_built.
    assert main["counts"]["other"] == 1
    assert main["counts"]["models_built"] == 1  # only the real model build


def test_annotation_only_arc_reports_zero_models_and_nonzero_other(seeded_store):
    portfolio = build_portfolio(seeded_store)
    leg1 = _arc(portfolio, "projA", "leg1")
    assert leg1["counts"]["models_built"] == 0
    assert leg1["counts"]["fitness_analyses"] == 0
    assert leg1["counts"]["other"] == 2  # kbdl.annotation + kbdl.skani


def test_empty_arc_is_included_and_flagged(seeded_store):
    portfolio = build_portfolio(seeded_store)
    empty = _arc(portfolio, "projA", "empty")
    assert empty is not None, "empty arc must be INCLUDED, not omitted"
    assert empty["empty"] is True
    assert empty["total_analyses"] == 0


def test_legs_nest_under_parents(seeded_store):
    portfolio = build_portfolio(seeded_store)
    projA = next(p for p in portfolio["projects"] if p["name"] == "projA")
    top_slugs = {a["slug"] for a in projA["arcs"]}
    # leg1 (leg_of=main) must NOT be a top-level arc; it nests under main.
    assert "leg1" not in top_slugs
    main = next(a for a in projA["arcs"] if a["slug"] == "main")
    assert "leg1" in {c["slug"] for c in main["children"]}


def test_parent_field_also_nests(store):
    """An arc that sets `parent` (not `leg_of`) nests too."""
    store.seed_arc(make_arc("p", "root", run_name="Root"))
    store.seed_arc(make_arc("p", "child", run_name="Child", parent="root"))
    portfolio = build_portfolio(store)
    projp = next(pr for pr in portfolio["projects"] if pr["name"] == "p")
    top = {a["slug"] for a in projp["arcs"]}
    assert "child" not in top
    root = next(a for a in projp["arcs"] if a["slug"] == "root")
    assert "child" in {c["slug"] for c in root["children"]}


def test_unattributed_is_exposed_with_distinct_badge(seeded_store):
    portfolio = build_portfolio(seeded_store)
    unattr = portfolio["unattributed"]
    assert unattr["attribution"] == "unattributed"
    assert unattr["total_analyses"] == 1
    assert unattr["counts"]["fba_runs"] == 1


def test_counts_are_distinct_on_analysis_id_not_runs(store):
    """One analysis re-run several times counts as ONE (distinct on analysis_id)."""
    store.seed_arc(make_arc("p", "a", run_name="A"))
    # Same analysis (same kind+subject+params), three different run_uids = re-runs.
    for uid, ts in (
        ("r1", "2026-09-01T00:00:00.000000Z"),
        ("r2", "2026-09-02T00:00:00.000000Z"),
        ("r3", "2026-09-03T00:00:00.000000Z"),
    ):
        store.record_analysis(
            "p",
            "a",
            make_record(
                "kbdl.model_build",
                "genomeQ",
                model_uri="obj://mQ",
                run_uid=uid,
                created_at=ts,
            ),
        )
    portfolio = build_portfolio(store)
    arc = _arc(portfolio, "p", "a")
    assert arc["counts"]["models_built"] == 1  # NOT 3
    assert arc["counts_are_distinct_on"] == "analysis_id"


def test_tiers_are_not_aggregated_into_one_number(seeded_store):
    """The arc summary keeps a per-tier breakdown, not a merged count."""
    portfolio = build_portfolio(seeded_store)
    main = _arc(portfolio, "projA", "main")
    # main holds hypothesis-tier model output AND a verified annotation record;
    # they must appear as SEPARATE tier counts, not merged.
    assert main["tier_counts"].get("hypothesis", 0) >= 1
    assert main["tier_counts"].get("verified", 0) >= 1


def test_most_recent_analysis_timestamp(store):
    store.seed_arc(make_arc("p", "a", run_name="A"))
    store.record_analysis(
        "p", "a",
        make_record("kbdl.model_build", "g1", model_uri="obj://m1",
                    run_uid="x", created_at="2026-01-01T00:00:00.000000Z"),
    )
    store.record_analysis(
        "p", "a",
        make_record("kbutillib.fba", "g1", model_uri="obj://m1",
                    run_uid="y", created_at="2026-06-01T00:00:00.000000Z"),
    )
    portfolio = build_portfolio(store)
    arc = _arc(portfolio, "p", "a")
    assert arc["most_recent_analysis_at"] == "2026-06-01T00:00:00.000000Z"


def test_portfolio_reads_no_detail_blob(seeded_store):
    """BLOB DISCIPLINE: /api/portfolio never opens a detail blob."""
    build_portfolio(seeded_store)
    assert seeded_store.blob_reads == 0
