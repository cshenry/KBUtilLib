"""Tests for ms_community_utils that need NEITHER MSCommunity NOR a solver.

These exercise the pure helpers, the dependency gate, and the composition
wrapper's unavailability behavior.  The most important test here is the
provenance gate (:class:`TestProvenanceGate`), which guards against importing
modelseedpy's superseded ``MSCommunity`` copy.
"""

import sys
import types
from pathlib import Path

import pytest

from kbutillib.core.errors import BackendUnavailableError, KBUtilLibError
from kbutillib.domains.modeling import ms_community_utils as mcu
from kbutillib.domains.modeling.ms_community_utils import (
    CommunityDependencyError,
    CommunityModel,
    CommunitySolverError,
    CommunityVisualizationError,
    _import_mscommunity,
    _mscommunity_unavailable_reason,
    _reset_mscommunity_cache,
    _unwrap,
    normalize_abundances,
    project_member_fluxes,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level import cache around every test."""
    _reset_mscommunity_cache()
    yield
    _reset_mscommunity_cache()


# ── _unwrap ─────────────────────────────────────────────────────────────────


class TestUnwrap:
    def test_returns_underlying_function_for_staticmethod(self):
        def plain(x):
            return x + 1

        sm = staticmethod(plain)
        unwrapped = _unwrap(sm)
        assert unwrapped is plain
        # And it is directly callable (the Python 3.9 hazard the helper fixes).
        assert unwrapped(1) == 2

    def test_identity_for_plain_function(self):
        def plain():
            return 42

        assert _unwrap(plain) is plain


# ── _import_mscommunity: absent ─────────────────────────────────────────────


class TestImportAbsent:
    def test_returns_none_when_package_absent(self, monkeypatch):
        # Remove any real/fake mscommunity and force the import to fail.
        monkeypatch.setitem(sys.modules, "mscommunity", None)

        # get_dependency_path yields nothing.
        import kbutillib.core.dependency_manager as dm

        monkeypatch.setattr(dm, "get_dependency_path", lambda name: None)

        assert _import_mscommunity() is None


# ── Provenance gate (the most important test) ───────────────────────────────


class TestProvenanceGate:
    def _install_superseded(self, monkeypatch):
        """Inject a fake mscommunity whose MSCommunity is modelseedpy's copy."""
        fake = types.ModuleType("mscommunity")

        class MSCommunity:
            pass

        # Pretend this class was defined in modelseedpy's superseded module.
        MSCommunity.__module__ = "modelseedpy.community.mscommunity"
        fake.MSCommunity = MSCommunity
        monkeypatch.setitem(sys.modules, "mscommunity", fake)
        return fake

    def test_import_rejects_modelseedpy_copy(self, monkeypatch):
        self._install_superseded(monkeypatch)
        assert _import_mscommunity() is None

    def test_unavailable_reason_names_superseded_copy(self, monkeypatch):
        self._install_superseded(monkeypatch)
        reason = _mscommunity_unavailable_reason()
        assert reason is not None
        assert "superseded copy" in reason

    def test_import_accepts_standalone_package(self, monkeypatch):
        fake = types.ModuleType("mscommunity")

        class MSCommunity:
            pass

        MSCommunity.__module__ = "mscommunity.mscommsim"
        fake.MSCommunity = MSCommunity
        fake.__file__ = "/nonexistent/mscommunity/__init__.py"
        monkeypatch.setitem(sys.modules, "mscommunity", fake)

        assert _import_mscommunity() is fake
        assert _mscommunity_unavailable_reason() is None


# ── normalize_abundances ────────────────────────────────────────────────────


class TestNormalizeAbundances:
    def test_normalizes_to_unit_sum(self):
        assert normalize_abundances({"a": 3, "b": 1}) == {"a": 0.75, "b": 0.25}

    def test_empty_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_abundances({})

    def test_nonpositive_total_raises_value_error(self):
        with pytest.raises(ValueError):
            normalize_abundances({"a": 0, "b": 0})


# ── project_member_fluxes ───────────────────────────────────────────────────


class TestProjectMemberFluxes:
    def test_member_projection(self):
        fluxes = {
            "rxn00001_c2": 5.0,
            "rxn00002_c1": 1.0,
            "EX_cpd00027_e0": -3.0,
            "bio3": 0.4,
        }
        result = project_member_fluxes(fluxes, member_index=2, member_biomass_id="bio3")
        assert result == {
            "rxn00001_c0": 5.0,
            "EX_cpd00027_e0": -3.0,
            "bio1": 0.4,
        }

    def test_mid_string_compartment_not_corrupted(self):
        # An id whose compartment pattern appears mid-string must be kept as-is
        # (str.replace would corrupt it; the regex anchors at the end).
        fluxes = {"rxn_c2_special": 2.0}
        result = project_member_fluxes(fluxes, member_index=1, member_biomass_id=None)
        assert result == {"rxn_c2_special": 2.0}


# ── CommunityModel.member_index ─────────────────────────────────────────────


class TestCommunityModel:
    def _model(self):
        return CommunityModel(
            mscomm=None,
            member_ids=["A", "B", "C"],
            abundances={"A": 0.5, "B": 0.3, "C": 0.2},
            source_model_ids={"A": "modelA", "B": "modelB", "C": "modelC"},
            kinetic_coeff=750.0,
        )

    def test_member_index_round_trips(self):
        cm = self._model()
        assert cm.member_index("A") == 1
        assert cm.member_index("B") == 2
        assert cm.member_index("C") == 3

    def test_member_index_unknown_raises_keyerror_listing_ids(self):
        cm = self._model()
        with pytest.raises(KeyError) as excinfo:
            cm.member_index("Z")
        # KeyError repr includes the message; assert the known ids appear.
        msg = str(excinfo.value)
        assert "A" in msg and "B" in msg and "C" in msg


# ── Exception hierarchy ─────────────────────────────────────────────────────


class TestExceptionHierarchy:
    def test_dependency_error_is_backend_unavailable(self):
        assert issubclass(CommunityDependencyError, BackendUnavailableError)

    def test_solver_error_is_kbutillib_error(self):
        assert issubclass(CommunitySolverError, KBUtilLibError)

    def test_visualization_error_is_kbutillib_error(self):
        assert issubclass(CommunityVisualizationError, KBUtilLibError)


# ── MSCommunityUtilsImpl unavailability ─────────────────────────────────────


class TestImplUnavailable:
    def test_forced_none_delegate_reports_unavailable(self, monkeypatch):
        impl = mcu.MSCommunityUtilsImpl(
            env=None, model=None, fba=None, escher=None, biochem=None
        )
        # Force the delegate to None regardless of environment.
        impl._delegate = None

        assert impl.available is False
        assert impl.unavailable_reason
        with pytest.raises(RuntimeError):
            impl.some_unknown_attribute


# ── Construction / simulation methods (no solver, no mscommunity) ────────────
#
# These exercise the guard rails on MSCommunityUtils' methods.  A bare
# MSCommunityUtils instance is built via object.__new__ so KBModelUtils' heavy
# __init__ (KBase clients, optional scientific deps) never runs -- each test
# only reaches the pure-Python guard it targets.


def _bare_util():
    """Return an un-initialized MSCommunityUtils (no KBModelUtils.__init__)."""
    return object.__new__(mcu.MSCommunityUtils)


class TestDependencyGate:
    def test_build_community_raises_dependency_error_when_absent(self, monkeypatch):
        # _import_mscommunity resolving to None must surface as a
        # CommunityDependencyError with a useful message.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: None)
        monkeypatch.setattr(
            mcu,
            "_mscommunity_unavailable_reason",
            lambda: "the mscommunity package is not importable",
        )
        util = _bare_util()
        with pytest.raises(CommunityDependencyError) as excinfo:
            util.build_community([object()])
        assert "mscommunity" in str(excinfo.value)


class TestRunMicomSolverGate:
    def test_run_micom_raises_solver_error_naming_backends(self, monkeypatch):
        # mscommunity present, but the QP probe reports no capable backend.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        monkeypatch.setattr(
            mcu.MSCommunityUtils, "_qp_backend_available", lambda self: False
        )
        util = _bare_util()
        with pytest.raises(CommunitySolverError) as excinfo:
            util.run_micom(comm=object(), media=None)
        msg = str(excinfo.value)
        for backend in ("gurobi", "cplex", "osqp", "hybrid"):
            assert backend in msg


class TestRunCommunityFBAOverSpecification:
    def test_min_member_growth_with_fixed_abundances_refuses(self, monkeypatch):
        # A per-member growth floor combined with caller-supplied fixed
        # abundances over-determines the system -> named refusal BEFORE solving.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())

        comm = CommunityModel(
            mscomm=object(),
            member_ids=["A", "B"],
            abundances={"A": 0.5, "B": 0.5},
            source_model_ids={},
            kinetic_coeff=750.0,
            abundances_were_supplied=True,
        )
        util = _bare_util()
        with pytest.raises(CommunitySolverError) as excinfo:
            util.run_community_fba(comm, media=None, min_member_growth=0.1)
        msg = str(excinfo.value)
        # Names BOTH inputs and says dropping either resolves it.
        assert "min_member_growth" in msg
        assert "abundances" in msg
        assert "drop" in msg.lower()


class TestLoadCommunityRecovery:
    def test_raises_value_error_naming_all_routes(self, monkeypatch):
        # A model whose notes lack member_biomass_cpds, with no member_ids arg,
        # and no kbutil.community blob -> ValueError naming all three routes.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())

        class _StubModel:
            notes: dict = {}

        class _StubMdlUtl:
            model = _StubModel()

        util = _bare_util()
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "get_model",
            lambda self, id_or_ref, ws=None: _StubMdlUtl(),
        )
        with pytest.raises(ValueError) as excinfo:
            util.load_community("some/ref")
        msg = str(excinfo.value)
        assert "kbutil.community" in msg
        assert "member_ids" in msg
        assert "member_biomass_cpds" in msg


class TestPredictAbundancesRestore:
    """predict_abundances(update=False) must restore both snapshotted things."""

    def _make_stub_comm(self):
        # A fake community whose mscomm records add_metabolites calls and whose
        # abundances/biomass can be mutated by the fake predict_abundances.
        class _FakeMember:
            def __init__(self, mid):
                self.id = mid
                self.abundance = None

        class _FakeBiomass:
            def __init__(self):
                # metabolite -> coefficient (upstream shape)
                self.metabolites = {"cpd_bio_c0": 1.0, "mem_A": -0.5, "mem_B": -0.5}
                self.add_metabolites_calls = []

            def add_metabolites(self, mapping, combine=False):
                # Emulate combine=False: overwrite coefficients wholesale.
                self.add_metabolites_calls.append((dict(mapping), combine))
                self.metabolites = dict(mapping)

        class _FakeMSComm:
            def __init__(self):
                self.abundances = {"A": 0.5, "B": 0.5}
                self.members = [_FakeMember("A"), _FakeMember("B")]
                self.primary_biomass = _FakeBiomass()

            def predict_abundances(
                self,
                media=None,
                pfba=True,
                regularization=True,
                update_abundances=False,
                determinize=False,
            ):
                # Mutate BOTH the abundances mapping and the biomass stoich, the
                # way real set_abundance would, then return the new vector.
                self.abundances = {"A": 0.8, "B": 0.2}
                self.primary_biomass.metabolites = {
                    "cpd_bio_c0": 1.0,
                    "mem_A": -0.8,
                    "mem_B": -0.2,
                }
                return {"A": 0.8, "B": 0.2}

        mscomm = _FakeMSComm()
        comm = CommunityModel(
            mscomm=mscomm,
            member_ids=["A", "B"],
            abundances={"A": 0.5, "B": 0.5},
            source_model_ids={},
            kinetic_coeff=750.0,
        )
        return comm, mscomm

    def test_update_false_restores_both(self, monkeypatch):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        comm, mscomm = self._make_stub_comm()

        original_abund = dict(mscomm.abundances)
        original_biomass = dict(mscomm.primary_biomass.metabolites)

        result = util.predict_abundances(comm, update=False)

        # The returned vector is upstream's prediction ...
        assert result == {"A": 0.8, "B": 0.2}
        # ... but the community was restored to its pre-call state (both things).
        assert mscomm.abundances == original_abund
        assert mscomm.primary_biomass.metabolites == original_biomass
        # The restore went through add_metabolites(..., combine=False).
        assert mscomm.primary_biomass.add_metabolites_calls
        last_mapping, combine = mscomm.primary_biomass.add_metabolites_calls[-1]
        assert combine is False
        assert last_mapping == original_biomass

    def test_update_true_does_not_restore(self, monkeypatch):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        comm, mscomm = self._make_stub_comm()

        result = util.predict_abundances(comm, update=True)

        # update=True keeps upstream's mutation: NO restore.
        assert result == {"A": 0.8, "B": 0.2}
        assert mscomm.abundances == {"A": 0.8, "B": 0.2}
        assert mscomm.primary_biomass.metabolites == {
            "cpd_bio_c0": 1.0,
            "mem_A": -0.8,
            "mem_B": -0.2,
        }
        # No restore call was made.
        assert mscomm.primary_biomass.add_metabolites_calls == []


# ── Marker strings vs. upstream literal text ─────────────────────────────────


class TestMarkerStrings:
    """The stdout markers must match upstream's literal print() text.

    ms_community_utils scans MSCommunity's stdout for these substrings to detect
    that kinetic constraints were silently disabled (or that a community does not
    grow).  If upstream rewords those prints, this test fails loudly rather than
    the flag silently ceasing to be set.  We assert the markers against the
    literal source lines in mscommunity/mscommsim.py -- located via
    dependencies.yaml -- and SKIP when the checkout is not present (the markers
    still can't drift undetected wherever the source IS available, e.g. CI).
    """

    def _mscommsim_source(self):
        import kbutillib.core.dependency_manager as dm

        path = dm.get_dependency_path("mscommunity")
        if path is None:
            pytest.skip("mscommunity checkout not resolvable via dependencies.yaml")
        src = Path(path) / "mscommunity" / "mscommsim.py"
        if not src.is_file():
            pytest.skip(f"mscommsim.py not found at {src}")
        return src.read_text()

    def test_kinetics_marker_matches_upstream(self):
        source = self._mscommsim_source()
        assert mcu.KINETICS_RELAXED_MARKER in source
        # Guard the exact print the scanner keys on.
        assert 'print(f"Kinetic constraints disabled for' in source

    def test_no_growth_marker_matches_upstream(self):
        source = self._mscommsim_source()
        assert mcu.NO_GROWTH_MARKER in source


# ── Visualization: fluxes_by_member (the adapter seam) ───────────────────────
#
# These tests exercise the visualization methods with NEITHER escher_edit,
# mscommunity, nor a solver present.  cross_feeding_table is monkeypatched to
# return a hand-written DataFrame; escher_edit is faked with a stub module that
# RECORDS its calls; nothing is rendered.


pd = pytest.importorskip("pandas")


def _cross_feeding_df(data, environment=None):
    """Build a cross-feeding DataFrame: index = compound ids, columns = members.

    ``data`` is ``{compound_id: {member_id: flux}}``.  An optional ``environment``
    ``{compound_id: flux}`` fills the literal ``"Environment"`` column.
    """
    members: list[str] = []
    for row in data.values():
        for m in row:
            if m not in members:
                members.append(m)
    columns = members + ["Environment"]
    rows = {}
    for cid, row in data.items():
        rows[cid] = [row.get(m, 0.0) for m in members] + [
            (environment or {}).get(cid, 0.0)
        ]
    return pd.DataFrame.from_dict(rows, orient="index", columns=columns)


class _RecordingEscherEdit(types.ModuleType):
    """A fake ``escher_edit`` module that records every call it receives."""

    def __init__(self):
        super().__init__("escher_edit")
        self.build_member_reactions_calls = []
        self.build_escher_map_calls = []
        self.render_map_svg_calls = []
        self.member_colors_calls = []
        self.member_colors_result = {"__marker__": "colors"}
        self.build_escher_map_raises = False

        outer = self

        class MapStyle:
            _n = 0

            def __init__(self, input_column_dx=None, output_column_dx=None, **kw):
                type(self)._n += 1
                self._id = type(self)._n
                self.input_column_dx = input_column_dx
                self.output_column_dx = output_column_dx
                self.kw = kw

            def fitted_column_dx(self, values):
                # A width proportional to the longest label -- longer names give a
                # wider canvas, which is what the "name" vs "id" test asserts.
                values = list(values)
                return max((len(str(v)) for v in values), default=0) * 10

        self.MapStyle = MapStyle

        class _Palette:
            @staticmethod
            def member_colors(names, groups=None, group_colors=None):
                outer.member_colors_calls.append((list(names), groups))
                return outer.member_colors_result

            @staticmethod
            def taxon_groups(taxonomy, rank):
                return {}

        self.palette = _Palette()

        class _FilterMap:
            DEFAULT_SKIP_NAMES = ["L-alanine", "L-glutamate"]

        self.filter_map = _FilterMap()

    def build_member_reactions(
        self,
        fluxes_by_member,
        model_id="",
        compound_names=None,
        min_abs_flux=0.0,
        skip_names=None,
        skip_ids=None,
        drop_empty=True,
    ):
        self.build_member_reactions_calls.append(
            {
                "fluxes_by_member": fluxes_by_member,
                "model_id": model_id,
                "compound_names": compound_names,
                "min_abs_flux": min_abs_flux,
                "skip_names": skip_names,
            }
        )
        # Return one "member reaction" per member for downstream shape.
        return [{"name": m, "bigg_id": m + model_id} for m in fluxes_by_member]

    def build_escher_map(
        self,
        blocks,
        compound_names=None,
        map_name=None,
        style=None,
        map_description=None,
    ):
        self.build_escher_map_calls.append(
            {
                "blocks": blocks,
                "compound_names": compound_names,
                "map_name": map_name,
                "style": style,
            }
        )
        if self.build_escher_map_raises:
            raise ValueError("no drawable members")
        return [{"map_name": map_name}, {"reactions": {}}]

    def render_map_svg(
        self,
        escher_map,
        out_path=None,
        dashed=True,
        layout=None,
        html=True,
        **processing,
    ):
        self.render_map_svg_calls.append(
            {
                "escher_map": escher_map,
                "out_path": out_path,
                "layout": layout,
                "html": html,
                "member_colors": processing.get("member_colors"),
            }
        )
        # Emit a trivial SVG so any post-render bs4 pass has something to open.
        if out_path is not None:
            Path(out_path).write_text(
                '<svg xmlns="http://www.w3.org/2000/svg">'
                '<text class="node-label label">cpd00027</text></svg>'
            )


def _fake_escher(monkeypatch):
    fake = _RecordingEscherEdit()
    monkeypatch.setattr(mcu, "_import_escher_edit", lambda: fake)
    return fake


def _comm(member_ids=("A", "B"), source_model_ids=None):
    return CommunityModel(
        mscomm=object(),
        member_ids=list(member_ids),
        abundances={m: 1.0 / len(member_ids) for m in member_ids},
        source_model_ids=source_model_ids
        if source_model_ids is not None
        else {m: m + "_model" for m in member_ids},
        kinetic_coeff=750.0,
    )


class TestFluxesByMember:
    def test_signs_unchanged_environment_dropped_zeros_omitted(self, monkeypatch):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        df = _cross_feeding_df(
            {
                "cpd00027": {"A": -3.0, "B": 5.0},  # A consumes, B excretes
                "cpd00001": {"A": 0.0, "B": 2.0},  # zero cell for A omitted
            }
        )
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "cross_feeding_table",
            lambda self, comm, result=None: (df, None),
        )
        out = util.fluxes_by_member(_comm(), result=None)
        # Signs are preserved exactly (NO flip).
        assert out == {
            "A": {"cpd00027": -3.0},
            "B": {"cpd00027": 5.0, "cpd00001": 2.0},
        }

    def test_sub_threshold_cells_dropped(self, monkeypatch):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        df = _cross_feeding_df({"cpd00027": {"A": 0.5, "B": 5.0}})
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "cross_feeding_table",
            lambda self, comm, result=None: (df, None),
        )
        out = util.fluxes_by_member(_comm(), min_abs_flux=1.0)
        assert out == {"A": {}, "B": {"cpd00027": 5.0}}

    def test_environment_only_compound_yields_empty(self, monkeypatch):
        # A compound whose ONLY non-zero value is in Environment must not draw the
        # medium as an organism: no member gets it.
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        df = _cross_feeding_df(
            {"cpd_env": {"A": 0.0, "B": 0.0}},
            environment={"cpd_env": 9.0},
        )
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "cross_feeding_table",
            lambda self, comm, result=None: (df, None),
        )
        out = util.fluxes_by_member(_comm())
        assert out == {"A": {}, "B": {}}
        # And the environment-only compound appears NOWHERE.
        assert all("cpd_env" not in v for v in out.values())


class TestRenderCommunityMapBlocks:
    def _patch_common(self, monkeypatch, df):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "cross_feeding_table",
            lambda self, comm, result=None: (df, None),
        )

    def test_single_result_passes_bare_member_list(self, monkeypatch, tmp_path):
        fake = _fake_escher(monkeypatch)
        df = _cross_feeding_df({"cpd00027": {"A": -3.0, "B": 5.0}})
        self._patch_common(monkeypatch, df)
        util = _bare_util()
        util.biochem = None

        result = object()
        util.render_community_map(
            _comm(), result, tmp_path / "map.json", svg=False, html=False
        )
        # ONE build_member_reactions call; build_escher_map got a BARE list.
        assert len(fake.build_member_reactions_calls) == 1
        (call,) = fake.build_escher_map_calls
        assert isinstance(call["blocks"], list)
        # Bare member list: elements are member-reaction dicts, not (label, members).
        assert call["blocks"] and isinstance(call["blocks"][0], dict)

    def test_two_results_pass_labelled_blocks_sorted(self, monkeypatch, tmp_path):
        fake = _fake_escher(monkeypatch)
        df = _cross_feeding_df({"cpd00027": {"A": -3.0, "B": 5.0}})
        self._patch_common(monkeypatch, df)
        util = _bare_util()
        util.biochem = None

        results = {"zed": object(), "abe": object()}
        util.render_community_map(
            _comm(), results, tmp_path / "map.json", svg=False, html=False
        )
        # build_member_reactions called ONCE PER LABEL, with the label as model_id.
        assert len(fake.build_member_reactions_calls) == 2
        model_ids = [c["model_id"] for c in fake.build_member_reactions_calls]
        # SORTED label order: "abe" before "zed".
        assert model_ids == ["abe", "zed"]
        (call,) = fake.build_escher_map_calls
        blocks = call["blocks"]
        assert [label for label, _ in blocks] == ["abe", "zed"]

    def test_colors_built_once_and_shared_by_identity(self, monkeypatch, tmp_path):
        fake = _fake_escher(monkeypatch)
        df = _cross_feeding_df({"cpd00027": {"A": -3.0, "B": 5.0}})
        self._patch_common(monkeypatch, df)
        util = _bare_util()
        util.biochem = None

        results = {"c1": object(), "c2": object()}
        util.render_community_map(
            _comm(("A", "B")), results, tmp_path / "map.json", svg=True, html=False
        )
        # member_colors derived EXACTLY ONCE, from comm.member_ids.
        assert len(fake.member_colors_calls) == 1
        names, _groups = fake.member_colors_calls[0]
        assert names == ["A", "B"]
        # The IDENTICAL object (asserted on identity, not equality) reaches every
        # render_map_svg call.
        objs = [c["member_colors"] for c in fake.render_map_svg_calls]
        assert objs, "expected at least one render_map_svg call"
        assert all(o is fake.member_colors_result for o in objs)

    def test_same_style_instance_to_build_and_render(self, monkeypatch, tmp_path):
        fake = _fake_escher(monkeypatch)
        df = _cross_feeding_df({"cpd00027": {"A": -3.0, "B": 5.0}})
        self._patch_common(monkeypatch, df)
        util = _bare_util()
        util.biochem = None

        util.render_community_map(
            _comm(), object(), tmp_path / "map.json", svg=True, html=False
        )
        build_style = fake.build_escher_map_calls[0]["style"]
        render_layout = fake.render_map_svg_calls[0]["layout"]
        assert build_style is render_layout

    def test_n_compounds_is_union_across_blocks(self, monkeypatch, tmp_path):
        fake = _fake_escher(monkeypatch)
        util = _bare_util()
        util.biochem = None
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())

        # Two conditions with overlapping + distinct compounds.
        df1 = _cross_feeding_df({"cpd1": {"A": -1.0, "B": 2.0}})
        df2 = _cross_feeding_df({"cpd2": {"A": -1.0, "B": 2.0}})
        by_result = {}

        r1, r2 = object(), object()
        by_result[id(r1)] = df1
        by_result[id(r2)] = df2

        def _table(self, comm, result=None):
            return (by_result[id(result)], None)

        monkeypatch.setattr(mcu.MSCommunityUtils, "cross_feeding_table", _table)
        arts = util.render_community_map(
            _comm(), {"a": r1, "b": r2}, tmp_path / "map.json", svg=False, html=False
        )
        assert arts.n_compounds == 2  # union of cpd1 + cpd2
        assert arts.n_blocks == 2


class TestRenderCommunityMapErrors:
    def test_raises_when_escher_edit_absent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        monkeypatch.setattr(mcu, "_import_escher_edit", lambda: None)
        monkeypatch.setattr(
            mcu, "_escher_edit_unavailable_reason", lambda: "not importable"
        )
        util = _bare_util()
        with pytest.raises(CommunityVisualizationError) as exc:
            util.render_community_map(_comm(), object(), tmp_path / "m.json")
        assert "escher_edit" in str(exc.value)

    def test_raises_when_no_drawable_members(self, monkeypatch, tmp_path):
        fake = _fake_escher(monkeypatch)
        fake.build_escher_map_raises = True
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        df = _cross_feeding_df({"cpd00027": {"A": -3.0, "B": 5.0}})
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "cross_feeding_table",
            lambda self, comm, result=None: (df, None),
        )
        util = _bare_util()
        util.biochem = None
        with pytest.raises(CommunityVisualizationError) as exc:
            util.render_community_map(
                _comm(), object(), tmp_path / "m.json", svg=False, html=False
            )
        assert "above-threshold" in str(exc.value) or "drawable" in str(exc.value)


class TestCompoundNameFallback:
    def test_falls_back_to_ids_when_biochem_raises(self, monkeypatch):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()

        class _Biochem:
            def get_compound_by_id(self, cid):
                raise RuntimeError("db down")

        util.biochem = _Biochem()
        names = util._resolve_compound_names(["cpd00027", "cpd00001"])
        assert names == {"cpd00027": "cpd00027", "cpd00001": "cpd00001"}

    def test_uses_biochem_name_when_available(self, monkeypatch):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()

        class _Cpd:
            def __init__(self, name):
                self.name = name

        class _Biochem:
            def get_compound_by_id(self, cid):
                return _Cpd("D-Glucose") if cid == "cpd00027" else None

        util.biochem = _Biochem()
        names = util._resolve_compound_names(["cpd00027", "cpd99999"])
        assert names == {"cpd00027": "D-Glucose", "cpd99999": "cpd99999"}


class TestLabelCompounds:
    def _run(self, monkeypatch, tmp_path, label_compounds):
        fake = _fake_escher(monkeypatch)
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        df = _cross_feeding_df(
            {
                "cpd00027": {"A": -3.0, "B": 5.0},
                "cpd00002": {"A": -1.0, "B": 4.0},
            }
        )
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "cross_feeding_table",
            lambda self, comm, result=None: (df, None),
        )
        util = _bare_util()

        class _Cpd:
            def __init__(self, name):
                self.name = name

        class _Biochem:
            def get_compound_by_id(self, cid):
                return _Cpd(
                    {
                        "cpd00027": "D-Glucose-a-long-name",
                        "cpd00002": "ATP-another-long-name",
                    }[cid]
                )

        util.biochem = _Biochem()
        supplied_style = fake.MapStyle()
        util.render_community_map(
            _comm(),
            object(),
            tmp_path / "map.json",
            svg=True,
            html=False,
            label_compounds=label_compounds,
            style=supplied_style,
        )
        return fake, supplied_style

    def test_id_default_no_post_render_and_no_new_style(self, monkeypatch, tmp_path):
        fake, supplied_style = self._run(monkeypatch, tmp_path, "id")
        # The caller's own style reaches build_escher_map (no widened copy).
        assert fake.build_escher_map_calls[0]["style"] is supplied_style
        # Static label unchanged (still the id) -- no bs4 rewrite happened.
        svg_text = fake.render_map_svg_calls[0]["out_path"]
        assert Path(svg_text).read_text().count("cpd00027") == 1

    def test_name_derives_new_style_without_mutating_and_rewrites(
        self, monkeypatch, tmp_path
    ):
        pytest.importorskip("bs4")
        fake, supplied_style = self._run(monkeypatch, tmp_path, "name")
        used_style = fake.build_escher_map_calls[0]["style"]
        # A NEW style object was derived (caller's not mutated).
        assert used_style is not supplied_style
        assert supplied_style.input_column_dx is None  # untouched
        # The new style's column dx come from fitted_column_dx over the NAMES.
        assert used_style.input_column_dx is not None
        assert used_style.output_column_dx == -used_style.input_column_dx
        # The static SVG text was rewritten from the id to the name.
        rendered = Path(fake.render_map_svg_calls[0]["out_path"]).read_text()
        assert "D-Glucose-a-long-name" in rendered

    def test_name_canvas_wider_than_id(self, monkeypatch, tmp_path):
        fake_name, _ = self._run(monkeypatch, tmp_path / "n", "name")
        name_dx = fake_name.build_escher_map_calls[0]["style"].output_column_dx

        fake_id, supplied = self._run(monkeypatch, tmp_path / "i", "id")
        # "id" uses the supplied (unwidened) style: its output_column_dx is None.
        id_dx = fake_id.build_escher_map_calls[0]["style"].output_column_dx
        assert id_dx is None
        # The widened "name" style reserves positive horizontal room.
        assert name_dx > 0


class TestMemberBanner:
    def test_inserts_after_body_when_present(self, tmp_path):
        p = tmp_path / "map.html"
        p.write_text("<html><body><div>map</div></body></html>")
        mcu.MSCommunityUtils._inject_member_banner(
            p,
            member_id="memberA",
            community_id="comm1",
            media_id="Carbon-D-Glucose",
            community_growth=0.1234,
        )
        text = p.read_text()
        assert "memberA" in text
        assert "cannot be attributed" in text
        # Banner comes after <body>, before the original content.
        assert text.index("<body>") < text.index("kbutil-member-banner")

    def test_prepends_when_no_body(self, tmp_path):
        p = tmp_path / "frag.html"
        p.write_text("<div>no body here</div>")
        mcu.MSCommunityUtils._inject_member_banner(
            p,
            member_id="memberA",
            community_id="comm1",
            media_id="m",
            community_growth=0.0,
        )
        text = p.read_text()
        assert text.index("kbutil-member-banner") < text.index("no body here")
        assert "memberA" in text
        assert "cannot be attributed" in text


class TestRenderMemberMap:
    def test_raises_value_error_when_source_model_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        comm = _comm(("A", "B"), source_model_ids={})  # no source model recorded
        with pytest.raises(ValueError) as exc:
            util.render_member_map(comm, "A", "somemap", tmp_path / "out.html")
        assert "A" in str(exc.value)
        assert "source model" in str(exc.value).lower()

    def test_raises_keyerror_for_unknown_member(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        util = _bare_util()
        comm = _comm(("A", "B"))
        with pytest.raises(KeyError):
            util.render_member_map(comm, "Z", "somemap", tmp_path / "out.html")


class TestCrossFeedingGraph:
    def _patch_table(self, monkeypatch, df):
        monkeypatch.setattr(mcu, "_import_mscommunity", lambda: object())
        monkeypatch.setattr(
            mcu.MSCommunityUtils,
            "cross_feeding_table",
            lambda self, comm, result=None, **kw: (df, None),
        )

    def test_is_multidigraph(self, monkeypatch):
        nx = pytest.importorskip("networkx")
        util = _bare_util()
        df = _cross_feeding_df({"cpd00027": {"A": 5.0, "B": -3.0}})
        self._patch_table(monkeypatch, df)
        g = util.cross_feeding_graph(_comm(), min_abs_flux=1e-4)
        assert isinstance(g, nx.MultiDiGraph)

    def test_two_metabolites_same_pair_both_survive(self, monkeypatch):
        # A DiGraph would keep only ONE edge for the (A, B) pair; a MultiDiGraph
        # keyed on metabolite keeps both.
        pytest.importorskip("networkx")
        util = _bare_util()
        df = _cross_feeding_df(
            {
                "cpd00027": {"A": 5.0, "B": -3.0},  # A produces, B consumes
                "cpd00002": {"A": 2.0, "B": -1.0},  # A produces, B consumes
            }
        )
        self._patch_table(monkeypatch, df)
        g = util.cross_feeding_graph(_comm(), min_abs_flux=1e-4)
        # Both keyed edges A->B survive.
        keys = sorted(k for u, v, k in g.edges(keys=True) if u == "A" and v == "B")
        assert keys == ["cpd00002", "cpd00027"]
        # Edge attributes are present and signed.
        edge = g.get_edge_data("A", "B", key="cpd00027")
        assert edge["metabolite"] == "cpd00027"
        assert edge["flux"] == 5.0
        assert edge["abs_flux"] == 5.0

    def test_sub_threshold_edges_dropped(self, monkeypatch):
        pytest.importorskip("networkx")
        util = _bare_util()
        df = _cross_feeding_df({"cpd00027": {"A": 0.001, "B": -0.001}})
        self._patch_table(monkeypatch, df)
        g = util.cross_feeding_graph(_comm(), min_abs_flux=0.01)
        assert g.number_of_edges() == 0

    def test_environment_node_kept(self, monkeypatch):
        pytest.importorskip("networkx")
        util = _bare_util()
        df = _cross_feeding_df(
            {"cpd00027": {"A": 5.0, "B": -3.0}}, environment={"cpd00027": 1.0}
        )
        self._patch_table(monkeypatch, df)
        g = util.cross_feeding_graph(_comm(), min_abs_flux=1e-4)
        assert "Environment" in g.nodes

    def test_no_graphviz_needed(self, monkeypatch):
        # The graph builder must not import graphviz or probe for `dot`.
        pytest.importorskip("networkx")
        util = _bare_util()
        df = _cross_feeding_df({"cpd00027": {"A": 5.0, "B": -3.0}})
        self._patch_table(monkeypatch, df)
        # Force graphviz to be unimportable; the call must still succeed.
        monkeypatch.setitem(sys.modules, "graphviz", None)
        g = util.cross_feeding_graph(_comm())
        assert g.number_of_edges() == 1
