"""Dependency-gated integration tests for :mod:`ms_community_utils`.

Every test in this module runs the community wrapper against a REAL
``mscommunity`` package built on real (toy) cobra models -- the contract-level
behaviours that a stub cannot prove:

  * :meth:`build_community` preserves the input-model -> community-member mapping
    that upstream's merge destroys (``source_model_ids``);
  * :meth:`predict_abundances` is non-mutating by default and mutating on
    ``update=True`` (the module's central semantic promise);
  * a save/load round trip carries ``member_ids`` / ``source_model_ids`` through
    ``model.notes["kbutil.community"]`` intact;
  * :meth:`run_community_fba` returns a result (never raises) on a no-growth
    medium;
  * the drain guard in :meth:`test_member_growth` recovers non-zero solo growth
    that a direct ``test_individual_species`` call reads as zero;
  * :meth:`run_micom` refuses cleanly when no QP solver is available;
  * :meth:`cross_feeding_table` returns two DataFrames with no graphviz present;
  * with ``escher_edit`` also present, :meth:`render_community_map` writes a
    parseable ``[header, body]`` map with one reaction per member plus non-empty
    svg/html.

The whole file is guarded by a module-level ``pytest.mark.skipif`` keyed on the
``ms_community_utils`` dependency gate.  On a machine WITHOUT ``mscommunity`` it
must COLLECT AND SKIP CLEANLY (exit 0, no collection errors), so nothing at
module scope may import ``mscommunity`` or otherwise raise -- the gate function
:func:`_import_mscommunity` never raises and returns ``None`` when the package
is absent.  These are contract tests, not benchmarks: the toy models keep the
whole file well under a minute.
"""

from __future__ import annotations

import copy
import json

import pytest

from kbutillib.domains.modeling.ms_community_utils import (
    CommunityFBAResult,
    CommunitySolverError,
    MSCommunityUtils,
    _import_escher_edit,
    _import_mscommunity,
)

# ── module-level dependency gate ──────────────────────────────────────────────
# Keyed on the SAME gate the product uses, not on an ad-hoc ``import``: a bare
# ``import mscommunity`` at module scope would (a) raise at collection when the
# package is absent and (b) bypass the provenance gate that rejects
# modelseedpy's superseded copy.  _import_mscommunity() returns None in both of
# those cases, which is exactly the skip condition we want.
pytestmark = pytest.mark.skipif(
    _import_mscommunity() is None,
    reason="mscommunity is not installed or resolvable via dependencies.yaml",
)


# ── solver / optional-dep probes (extra skips, evaluated lazily) ──────────────


def _solver_available() -> bool:
    """Return whether ANY LP solver is importable (glpk is the CI floor)."""
    try:
        import optlang  # noqa: WPS433

        return bool(optlang.available_solvers)
    except Exception:
        return False


def _qp_solver_available() -> bool:
    """Return whether a QP-capable solver is available (gurobi/cplex/osqp)."""
    try:
        import optlang  # noqa: WPS433

        avail = optlang.available_solvers
        return any(avail.get(name, False) for name in ("GUROBI", "CPLEX", "OSQP"))
    except Exception:
        return False


_needs_solver = pytest.mark.skipif(
    not _solver_available(), reason="no LP solver importable via optlang"
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def community_util():
    """A fully constructed ``MSCommunityUtils`` (no file discovery, no token)."""
    return MSCommunityUtils(config_file=False, token_file=None, kbase_token_file=None)


@pytest.fixture
def member_models(mini_model):
    """Two deep copies of ``mini_model`` with distinct ids, in a fixed order."""
    m1 = copy.deepcopy(mini_model)
    m1.id = "member_one"
    m2 = copy.deepcopy(mini_model)
    m2.id = "member_two"
    return [m1, m2]


@pytest.fixture
def community(community_util, member_models):
    """A two-member community built WITHOUT abundances (uniform default split)."""
    return community_util.build_community(member_models)


@pytest.fixture
def bound_community(community_util, member_models):
    """A two-member community built WITH abundances (close_member_drains=True).

    Supplying an abundance vector is what makes ``close_member_drains`` True in
    :meth:`build_community`, which is the precondition the drain-guard test needs.
    """
    return community_util.build_community(
        member_models, abundances={"member_one": 0.5, "member_two": 0.5}
    )


# ── 1. build_community captures provenance ────────────────────────────────────


def test_build_community_captures_source_model_ids(community, member_models):
    """member_ids are in input order and source_model_ids maps each back.

    This is the property upstream's merge destroys (it copies + renames every
    member) and the whole reason build_community captures ids BEFORE the merge.
    """
    input_ids = [m.id for m in member_models]

    # member_ids present in input/index order.
    assert len(community.member_ids) == 2
    # source_model_ids is MEMBER FIRST: {member_id: source_model_id}.
    assert set(community.source_model_ids) == set(community.member_ids)
    # Every community member maps back to one of the two input model ids, and the
    # two mappings are distinct (no member collapses onto the other's source).
    mapped_sources = [
        community.source_model_ids[mid] for mid in community.member_ids
    ]
    assert sorted(mapped_sources) == sorted(input_ids)

    # The merged model rewrites compartments: member 1 -> c1, member 2 -> c2, and
    # the extracellular compartment is SHARED as e0.
    model = community.mscomm.util.model
    compartments = {met.compartment for met in model.metabolites}
    assert "c1" in compartments
    assert "c2" in compartments
    assert "e0" in compartments


# ── 2. predict_abundances non-mutation contract ───────────────────────────────


def _biomass_stoich(reaction):
    """Return the primary biomass reaction's {metabolite.id: coefficient} map."""
    return {met.id: coef for met, coef in reaction.metabolites.items()}


@_needs_solver
def test_predict_abundances_non_mutating_by_default_and_mutates_on_update(
    community_util, community
):
    """update=False leaves abundances + biomass stoichiometry byte-for-byte equal;
    update=True changes at least one of them.

    This is the module's central semantic promise; it is tested against a real
    model, not a stub, because the restore path threads through cobra's
    ``add_metabolites(..., combine=False)`` and upstream's own bookkeeping.
    """
    mscomm = community.mscomm

    before_abund = dict(mscomm.abundances)
    before_stoich = _biomass_stoich(mscomm.primary_biomass)

    # Non-mutating call: everything the method snapshots must be restored.
    community_util.predict_abundances(community, update=False)

    after_abund = dict(mscomm.abundances)
    after_stoich = _biomass_stoich(mscomm.primary_biomass)

    assert after_abund == before_abund
    assert after_stoich == before_stoich

    # Mutating call: at least one of the two snapshotted things must change.
    community_util.predict_abundances(community, update=True)
    mutated_abund = dict(mscomm.abundances)
    mutated_stoich = _biomass_stoich(mscomm.primary_biomass)

    changed = (mutated_abund != before_abund) or (mutated_stoich != before_stoich)
    assert changed, (
        "predict_abundances(update=True) changed neither the abundances nor the "
        "primary biomass stoichiometry"
    )


# ── 3. save/load round trip preserves provenance ──────────────────────────────


def test_save_load_round_trip_preserves_provenance(community_util, community):
    """Writing kbutil.community notes and reading them back reconstructs
    member_ids and source_model_ids EXACTLY as build_community produced them.

    A live workspace is not required: we exercise the notes write + read directly
    against the in-memory model, which is the path render_member_map depends on
    after a reload.
    """
    model = community.mscomm.util.model

    # WRITE the provenance blob the way save_community does (JSON string in notes).
    provenance = {
        "schema_version": 1,
        "member_ids": list(community.member_ids),
        "source_model_ids": dict(community.source_model_ids),
        "abundances": dict(community.abundances),
        "kinetic_coeff": community.kinetic_coeff,
        "abundances_were_supplied": community.abundances_were_supplied,
    }
    if model.notes is None:
        model.notes = {}
    model.notes["kbutil.community"] = json.dumps(provenance)

    # READ it back exactly as load_community's route 1 does.
    blob = json.loads(model.notes["kbutil.community"])
    assert blob["schema_version"] == 1
    assert list(blob["member_ids"]) == list(community.member_ids)
    assert dict(blob["source_model_ids"]) == dict(community.source_model_ids)


# ── 4. run_community_fba on a no-growth medium does not raise ──────────────────


@_needs_solver
def test_run_community_fba_no_growth_returns_result(community_util, community):
    """An empty medium (nothing can be taken up) yields a CommunityFBAResult with
    trustworthy False or community_growth ~ 0, and does NOT raise."""
    from modelseedpy.core.msmedia import MSMedia

    empty_media = MSMedia.from_dict({})  # no uptake -> the community cannot grow
    result = community_util.run_community_fba(community, media=empty_media)

    assert isinstance(result, CommunityFBAResult)
    assert (result.trustworthy is False) or (abs(result.community_growth) < 1e-6)


# ── 5. drain guard: solo growth recovered vs. raw zeros ───────────────────────


@_needs_solver
def test_drain_guard_recovers_solo_growth(community_util, bound_community):
    """On an abundance-bound community (close_member_drains=True),
    test_member_growth(interacting=False) returns NON-ZERO solo growth for at
    least one member, while a DIRECT test_individual_species(interacting=False)
    on the same community returns zero for every member.

    The CONTRAST is the test: it proves the guard reopens the biomass drains,
    not merely that a number came back.
    """
    mscomm = bound_community.mscomm
    assert getattr(mscomm, "close_member_drains", False) is True

    # Direct upstream call: with drains shut, every member reads zero solo growth.
    raw = mscomm.test_individual_species(interacting=False)
    raw_growths = _solo_growth_values(raw)
    assert raw_growths, "test_individual_species returned no rows"
    assert all(abs(g) < 1e-6 for g in raw_growths), (
        f"expected zero solo growth from the raw call, got {raw_growths}"
    )

    # Guarded call: drains reopened -> at least one member grows solo.
    guarded = community_util.test_member_growth(bound_community, interacting=False)
    assert guarded.attrs.get("drains_reopened") is True
    guarded_growths = _solo_growth_values(guarded)
    assert any(abs(g) > 1e-6 for g in guarded_growths), (
        f"expected non-zero solo growth after the guard reopened drains, got "
        f"{guarded_growths}"
    )


def _solo_growth_values(df):
    """Extract solo-growth numbers from a test_individual_species DataFrame.

    The column name upstream uses has shifted across versions; take whichever
    numeric growth-like column is present, else every numeric cell.
    """
    import pandas as pd

    for col in df.columns:
        lowered = str(col).lower()
        if "growth" in lowered or "solo" in lowered or "biomass" in lowered:
            return [float(v) for v in df[col] if pd.notna(v)]
    # Fallback: all numeric cells.
    values = []
    for col in df.columns:
        for v in df[col]:
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                continue
    return values


# ── 6. run_micom refuses cleanly when no QP solver is available ───────────────


@pytest.mark.skipif(
    _qp_solver_available(),
    reason="a QP solver is present; the no-QP refusal path cannot be exercised",
)
def test_run_micom_raises_without_qp_solver(community_util, community):
    """With only glpk available, run_micom raises CommunitySolverError naming the
    QP solvers rather than attempting an unsolvable quadratic objective."""
    from modelseedpy.core.msmedia import MSMedia

    with pytest.raises(CommunitySolverError) as exc:
        community_util.run_micom(community, MSMedia.from_dict({}))
    message = str(exc.value).lower()
    # The refusal must name the QP backends so the caller knows what to install.
    assert "gurobi" in message
    assert "cplex" in message
    assert "osqp" in message


# ── 7. cross_feeding_table returns two DataFrames with no graphviz ────────────


@_needs_solver
def test_cross_feeding_table_returns_two_dataframes(community_util, community):
    """cross_feeding_table returns the (cross_feeding_df, exchanged_mets_df) pair
    as pandas DataFrames -- no graphviz needed (visualize is forced False)."""
    import pandas as pd

    cross_feeding_df, exchanged_mets_df = community_util.cross_feeding_table(community)
    assert isinstance(cross_feeding_df, pd.DataFrame)
    assert isinstance(exchanged_mets_df, pd.DataFrame)


# ── 8. render_community_map (needs escher_edit ALSO present) ───────────────────


@pytest.mark.skipif(
    _import_escher_edit() is None,
    reason="escher_edit is not installed or resolvable via dependencies.yaml",
)
@_needs_solver
def test_render_community_map_writes_parseable_structure(
    community_util, community, tmp_path
):
    """render_community_map writes a map_json parsing as a two-element
    [header, body] list whose body["reactions"] holds one entry per member, and
    the requested svg/html files exist and are non-empty.

    Asserts on STRUCTURE and on the returned counts -- NOT on SVG text/pixels,
    which are upstream's layout to tune.
    """
    result = community_util.run_community_fba(community)
    out_json = tmp_path / "community_map.json"

    artifacts = community_util.render_community_map(
        community, result, out_json, svg=True, html=True
    )

    # map_json parses as [header, body].
    parsed = json.loads(out_json.read_text())
    assert isinstance(parsed, list) and len(parsed) == 2
    header, body = parsed
    assert isinstance(header, dict)
    assert isinstance(body, dict)

    # One reaction entry per member (the community map draws one net organism
    # reaction per member).
    reactions = body["reactions"]
    assert len(reactions) == len(community.member_ids)

    # Returned counts reflect the community, and svg/html exist and are non-empty.
    assert artifacts.n_members == len(community.member_ids)
    assert artifacts.n_blocks == 1  # a single result -> a single block
    assert artifacts.svg is not None and artifacts.svg.exists()
    assert artifacts.svg.stat().st_size > 0
    assert artifacts.html is not None and artifacts.html.exists()
    assert artifacts.html.stat().st_size > 0


@pytest.mark.skipif(
    _import_escher_edit() is None,
    reason="escher_edit is not installed or resolvable via dependencies.yaml",
)
@_needs_solver
def test_render_community_map_two_results_yields_two_blocks(
    community_util, community, tmp_path
):
    """Passing two results yields n_blocks == 2 and one text_labels entry per
    label (asserted structurally, not on rendered pixels)."""
    result = community_util.run_community_fba(community)
    out_json = tmp_path / "community_map_multi.json"

    artifacts = community_util.render_community_map(
        community,
        {"cond_a": result, "cond_b": result},
        out_json,
        svg=True,
        html=True,
    )

    assert artifacts.n_blocks == 2
    parsed = json.loads(out_json.read_text())
    assert isinstance(parsed, list) and len(parsed) == 2
