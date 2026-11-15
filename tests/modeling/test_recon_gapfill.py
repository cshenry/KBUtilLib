"""Unit tests for the per-medium gap-fill half of the reconstruction layer.

Everything here runs on FAKES.  The real-dependency counterpart lives in
``tests/modeling/test_recon_gapfill_real_dependency.py``; one real gap-fill
costs 35-50 s per medium, so anything a fake can decide is decided here.

What is covered:

* one record per requested medium OCCURRENCE, in request order, so a medium
  requested twice keeps both attempts;
* the exclusion list is normalised to BASE reaction ids (the only form the
  underlying blacklist matches) and RE-CHECKED against what each occurrence
  actually added;
* ``prefilter`` is an explicit parameter, defaults to ``False``, and applies to
  the first occurrence only;
* ATP tests come from ``mdlutl.atp_tests`` (which ``build_metabolic_model``
  publishes) and the fork-only ``get_atp_tests`` is a fallback, not the
  default, so the ordinary path makes no KBase call;
* fork-only ``MSGapfill`` keywords are dropped when unrequested and raise when
  explicitly requested;
* the records stage into ``ReconstructionResult.post_gapfill_results`` without
  mutating the frozen result.

The fake ``MSGapfill`` carries the PINNED UPSTREAM signature exactly, because
``_supported_keyword_arguments`` probes it: a fake with a permissive
``**kwargs`` would make every capability test pass vacuously.
"""

from __future__ import annotations

import dataclasses

import pytest

recon = pytest.importorskip("kbutillib.domains.modeling.ms_reconstruction_utils")
MSReconstructionUtils = recon.MSReconstructionUtils
GapfillRecord = recon.GapfillRecord
ModelSEEDpyCapabilityError = recon.ModelSEEDpyCapabilityError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeReaction:
    def __init__(self, rxn_id: str, lower_bound: float = 0.0,
                 upper_bound: float = 1000.0):
        self.id = rxn_id
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound


class _FakeReactionList:
    """Enough of cobra's ``DictList`` for the code under test.

    ``in`` accepts an id or an object and ``get_by_id`` resolves by id, which
    is what ``_solution_adds_anything`` and the id-set arithmetic need.
    """

    def __init__(self, reaction_ids):
        self._items = {rxn_id: _FakeReaction(rxn_id) for rxn_id in reaction_ids}

    def __iter__(self):
        return iter(self._items.values())

    def __len__(self):
        return len(self._items)

    def __contains__(self, key):
        return key in self._items or getattr(key, "id", None) in self._items

    def get_by_id(self, rxn_id):
        return self._items[rxn_id]

    def add(self, rxn_id):
        self._items[rxn_id] = _FakeReaction(rxn_id)

    def discard(self, rxn_id):
        self._items.pop(rxn_id, None)


class _FakeModel:
    """A model that grows on a medium once the reactions that medium needs are
    present -- so "the repeat has no gaps left" is an emergent property of the
    fake rather than a scripted return value."""

    def __init__(self, *, reaction_ids=(), requirements=None, gene_ids=()):
        self.reactions = _FakeReactionList(reaction_ids)
        self.genes = [object() for _ in gene_ids]
        self.objective = "unset-by-atp-correction"
        self.requirements = dict(requirements or {})
        self.current_media_id = None
        self.objectives_seen: list = []
        self.solve_count = 0

    def slim_optimize(self):
        self.solve_count += 1
        self.objectives_seen.append(self.objective)
        needed = self.requirements.get(self.current_media_id)
        if needed is None:
            return 0.0
        present = {rxn.id for rxn in self.reactions}
        return 1.0 if set(needed) <= present else 0.0


class _FakePackage:
    def __init__(self, model, name):
        self._model = model
        self._name = name
        self.builds: list = []

    def build_package(self, payload):
        self.builds.append(payload)
        if self._name == "KBaseMediaPkg":
            self._model.current_media_id = getattr(payload, "id", None)


class _FakePkgMgr:
    def __init__(self, model):
        self._model = model
        self.packages: dict = {}

    def getpkg(self, name):
        return self.packages.setdefault(name, _FakePackage(self._model, name))


class _FakeMdlUtl:
    def __init__(self, model, *, atp_tests=None, with_get_atp_tests=False):
        self.model = model
        self.pkgmgr = _FakePkgMgr(model)
        self.gfutl = None
        self.get_atp_tests_calls: list = []
        if atp_tests is not None:
            self.atp_tests = atp_tests
        if with_get_atp_tests:
            self.get_atp_tests = self._get_atp_tests

    def _get_atp_tests(self, *, core_template, atp_media_filename, recompute):
        self.get_atp_tests_calls.append(
            {
                "core_template": core_template,
                "atp_media_filename": atp_media_filename,
                "recompute": recompute,
            }
        )
        return [{"source": "get_atp_tests"}]

    def nonexchange_reaction_count(self):
        return len([r for r in self.model.reactions if not r.id.startswith("EX_")])


class _FakeMediaCompound:
    def __init__(self, cpd_id, lower_bound, upper_bound):
        self.id = cpd_id
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound


class _FakeMedia:
    def __init__(self, media_id, composition=((("cpd00027"), -10.0, 1000.0),)):
        self.id = media_id
        self.mediacompounds = [
            _FakeMediaCompound(*triple) for triple in composition
        ]


#: Every ``_FakeMSGapfill`` the code under test constructed, most recent last.
#: A registry rather than a wrapper subclass because ``_supported_keyword_arguments``
#: PROBES ``MSGapfill.__init__``'s signature: a subclass declaring ``**kwargs``
#: would advertise ``base_media`` support the pinned upstream does not have, and
#: every capability test would pass vacuously.
CONSTRUCTED: list = []


@pytest.fixture(autouse=True)
def _clear_constructed():
    CONSTRUCTED.clear()
    yield
    CONSTRUCTED.clear()


#: Every ``cobra.flux_analysis.pfba`` call the code under test made.
PFBA_CALLS: list = []


class _FakeSolution:
    """A ``cobra`` ``Solution`` stand-in: only ``fluxes`` is touched."""

    def __init__(self):
        self.fluxes: dict = {}


@pytest.fixture(autouse=True)
def _stub_pfba(monkeypatch):
    """``pfba`` is a real LP on a real cobra model, and the fake is not one.

    Stubbed rather than avoided, so the call site and the flux-overwriting loop
    that follows it are still exercised by these tests.
    """
    import cobra.flux_analysis

    def _pfba(model):
        PFBA_CALLS.append(model)
        return _FakeSolution()

    monkeypatch.setattr(cobra.flux_analysis, "pfba", _pfba)
    PFBA_CALLS.clear()
    yield
    PFBA_CALLS.clear()


class _FakeMSGapfill:
    """The pinned upstream ``MSGapfill``'s signature and integration semantics.

    ``PLAN`` maps a medium id to what ``run_gapfilling`` returns for it: a
    solution dict, ``None``, or an exception instance to raise.  ``PRUNE`` names
    reaction ids that integration drops, mirroring the real
    ``integrate_gapfill_solution``'s closing ``test_solution(keep_changes=True)``.
    """

    PLAN: dict = {}
    PRUNE: tuple = ()

    def __init__(
        self,
        model_or_mdlutl,
        default_gapfill_templates=[],
        default_gapfill_models=[],
        test_conditions=[],
        reaction_scores={},
        blacklist=[],
        atp_gapfilling=False,
        minimum_obj=0.01,
        default_excretion=100,
        default_uptake=100,
        default_target=None,
    ):
        self.construction = {
            "model_or_mdlutl": model_or_mdlutl,
            "default_gapfill_templates": default_gapfill_templates,
            "default_gapfill_models": default_gapfill_models,
            "test_conditions": test_conditions,
            "reaction_scores": reaction_scores,
            "blacklist": blacklist,
            "minimum_obj": minimum_obj,
            "default_target": default_target,
        }
        self.mdlutl = model_or_mdlutl
        model_or_mdlutl.gfutl = self
        self.cumulative_gapfilling: list = []
        self.reaction_scores = reaction_scores
        self.gapfill_calls: list = []
        self.integrate_calls: list = []
        self.plan = dict(self.PLAN)
        self.prune = tuple(self.PRUNE)
        CONSTRUCTED.append(self)

    # -- the API the layer calls ------------------------------------------
    def run_gapfilling(
        self,
        media=None,
        target=None,
        minimum_obj=None,
        binary_check=False,
        prefilter=True,
        check_for_growth=True,
    ):
        self.gapfill_calls.append(
            {
                "media_id": media.id,
                "target": target,
                "minimum_obj": minimum_obj,
                "binary_check": binary_check,
                "prefilter": prefilter,
                "check_for_growth": check_for_growth,
            }
        )
        outcome = self.plan.get(media.id)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def integrate_gapfill_solution(
        self, solution, cumulative_solution=[], link_gaps_to_objective=True
    ):
        self.integrate_calls.append(solution)
        for rxn_id, direction in solution["reversed"].items():
            cumulative_solution.append([rxn_id, direction])
        for rxn_id, direction in solution["new"].items():
            if rxn_id not in self.mdlutl.model.reactions:
                self.mdlutl.model.reactions.add(rxn_id)
            cumulative_solution.append([rxn_id, direction])
        # Integration prunes: the real one removes reactions it judges
        # unneeded, which is why additions are measured off the model.
        for rxn_id in self.prune:
            self.mdlutl.model.reactions.discard(rxn_id)
            for entry in list(cumulative_solution):
                if entry[0] == rxn_id:
                    cumulative_solution.remove(entry)
        self.cumulative_gapfilling.extend(cumulative_solution)


class _ForkMSGapfill(_FakeMSGapfill):
    """The FORK's signature: upstream's, plus ``base_media`` and
    ``base_media_target_element``.  Spelled out rather than taking ``**kwargs``,
    for the reason given on :data:`CONSTRUCTED`."""

    def __init__(
        self,
        model_or_mdlutl,
        default_gapfill_templates=[],
        default_gapfill_models=[],
        test_conditions=[],
        reaction_scores={},
        blacklist=[],
        atp_gapfilling=False,
        minimum_obj=0.01,
        default_excretion=100,
        default_uptake=100,
        default_target=None,
        base_media=None,
        base_media_target_element="C",
    ):
        super().__init__(
            model_or_mdlutl,
            default_gapfill_templates=default_gapfill_templates,
            default_gapfill_models=default_gapfill_models,
            test_conditions=test_conditions,
            reaction_scores=reaction_scores,
            blacklist=blacklist,
            atp_gapfilling=atp_gapfilling,
            minimum_obj=minimum_obj,
            default_excretion=default_excretion,
            default_uptake=default_uptake,
            default_target=default_target,
        )
        self.construction["base_media"] = base_media
        self.construction["base_media_target_element"] = base_media_target_element


def _planned(*, plan=None, prune=(), base=_FakeMSGapfill):
    """A ``MSGapfill`` stand-in whose ``run_gapfilling`` follows ``plan``.

    Built with :func:`type` rather than by subclassing with a new ``__init__``,
    so the signature the layer probes stays EXACTLY the one it is standing in
    for.
    """
    return type(
        "_PlannedMSGapfill", (base,), {"PLAN": dict(plan or {}), "PRUNE": tuple(prune)}
    )


class _FakeSelf:
    """Minimal stand-in for ``MSReconstructionUtils`` on the gap-fill path."""

    gapfill_metabolic_model = MSReconstructionUtils.gapfill_metabolic_model
    with_gapfill_results = MSReconstructionUtils.with_gapfill_results

    def __init__(self, *, gapfill_class=_FakeMSGapfill):
        self.MSGapfill = gapfill_class
        self.modelseedpy_data_dir = "/nonexistent/modelseedpy/data"
        self.templates = {"core": "core_ref"}
        self.template_fetches: list = []

    def get_template(self, ref, _none=None):
        self.template_fetches.append(ref)
        return f"template_from_{ref}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _solution(media, *, new=None, reversed_=None):
    """An ``MSGapfill.run_gapfilling`` result, upstream's exact key set."""
    return {
        "new": dict(new or {}),
        "reversed": dict(reversed_ or {}),
        "media": media,
        "target": "bio1",
        "minobjective": 0.01,
        "binary_check": False,
    }


def _run(fake_self, mdlutl, media_objs, **kwargs):
    kwargs.setdefault("atp_safe", False)
    kwargs.setdefault("return_gapfill_records", True)
    return fake_self.gapfill_metabolic_model(
        mdlutl=mdlutl,
        genome=None,
        media_objs=media_objs,
        templates=["gs_template"],
        **kwargs,
    )


def _two_media_model(**kwargs):
    """A model that needs ``rxn_a_c0`` to grow on ``Glc`` and ``rxn_b_c0`` on
    ``Succ``, and carries neither."""
    return _FakeModel(
        reaction_ids=("bio1", "EX_cpd00027_e0"),
        requirements={"Glc": ("rxn_a_c0",), "Succ": ("rxn_b_c0",)},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# One record per requested OCCURRENCE
# ---------------------------------------------------------------------------


class TestOneRecordPerOccurrence:
    """``run_multi_gapfill`` returns a dict keyed by media OBJECT, so it cannot
    represent a repeated request at all.  These records can."""

    def _execute(self):
        """Glc, then Succ, then Glc AGAIN -- three occurrences, two ids."""
        glc = _FakeMedia("Glc")
        succ = _FakeMedia("Succ", composition=((("cpd00036"), -10.0, 1000.0),))
        mdlutl = _FakeMdlUtl(_two_media_model())
        media_objs = [glc, succ, glc]
        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={
                    "Glc": _solution(glc, new={"rxn_a_c0": ">"}),
                    "Succ": _solution(succ, new={"rxn_b_c0": ">"}),
                }
            )
        )

        returned = _run(fake_self, mdlutl, media_objs)
        return returned, CONSTRUCTED[-1], mdlutl, media_objs

    def test_there_is_one_record_per_occurrence_in_request_order(self):
        returned, _gapfill, _mdlutl, _media = self._execute()
        records = returned[4]

        assert [r.request_index for r in records] == [0, 1, 2]
        assert [r.media_id for r in records] == ["Glc", "Succ", "Glc"]

    def test_a_repeated_id_keeps_both_attempts(self):
        """The second occurrence of ``Glc`` is reported honestly: the first one
        already integrated what that medium needed, so there are no gaps left
        -- not dropped, not merged into the first."""
        returned, gapfill, _mdlutl, _media = self._execute()
        records = returned[4]

        assert records[0].status == "integrated"
        assert records[0].additions == ("rxn_a_c0",)
        assert records[2].status == "no_gaps"
        assert records[2].additions == ()
        # And the repeat did not pay for a second MILP.
        assert [c["media_id"] for c in gapfill.gapfill_calls] == ["Glc", "Succ"]

    def test_the_second_medium_sees_what_the_first_added(self):
        """Sequential integration: integrate, then attempt the next."""
        returned, _gapfill, mdlutl, _media = self._execute()
        records = returned[4]

        assert records[1].status == "integrated"
        assert records[1].additions == ("rxn_b_c0",)
        assert {"rxn_a_c0", "rxn_b_c0"} <= {r.id for r in mdlutl.model.reactions}

    def test_the_records_are_a_tuple_of_the_record_dataclass(self):
        returned, _gapfill, _mdlutl, _media = self._execute()
        records = returned[4]

        assert isinstance(records, tuple)
        assert all(isinstance(r, GapfillRecord) for r in records)
        assert dataclasses.is_dataclass(GapfillRecord)
        with pytest.raises(dataclasses.FrozenInstanceError):
            records[0].status = "tampered"

    def test_growth_is_reported_for_each_occurrence_that_had_a_value(self):
        returned, _gapfill, _mdlutl, _media = self._execute()
        current_output = returned[0]

        assert current_output["Growth"] == "Glc:1.0<br>Succ:1.0<br>Glc:1.0"


class TestMediaIdentity:
    def test_ids_are_used_exactly_as_supplied(self):
        """``MSMedia.from_dict`` names every medium it builds ``"media"``, so a
        caller legitimately arrives with two different compositions under one
        id.  This layer renames nothing."""
        first = _FakeMedia("media", composition=((("cpd00027"), -10.0, 1000.0),))
        second = _FakeMedia("media", composition=((("cpd00036"), -5.0, 1000.0),))
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        records = _run(_FakeSelf(), mdlutl, [first, second])[4]

        assert [r.media_id for r in records] == ["media", "media"]
        assert records[0].media_digest != records[1].media_digest

    def test_the_same_composition_digests_the_same(self):
        composition = ((("cpd00027"), -10.0, 1000.0), (("cpd00007"), -20.0, 1000.0))
        one = _FakeMedia("a", composition=composition)
        two = _FakeMedia("b", composition=tuple(reversed(composition)))

        assert recon._media_digest(one) == recon._media_digest(two)
        assert recon._media_digest(one).startswith("sha256:")

    def test_an_object_without_compounds_digests_as_unavailable(self):
        assert recon._media_digest(object()) == "sha256:unavailable"


# ---------------------------------------------------------------------------
# The exclusion list
# ---------------------------------------------------------------------------


class TestReactionExclusionList:
    """``GapfillingPkg`` compares the blacklist against
    ``template_reaction.reference_id`` -- a BASE id -- so an entry carrying a
    compartment suffix matches nothing at all."""

    def test_the_blacklist_reaching_msgapfill_is_normalised_to_base_ids(self):
        fake_self = _FakeSelf()
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        _run(
            fake_self,
            mdlutl,
            [_FakeMedia("Glc")],
            reaction_exclusion_list=["rxn00029_c0", "rxn99999", "rxn00030_e0"],
        )

        assert CONSTRUCTED[-1].construction["blacklist"] == [
            "rxn00029",
            "rxn00030",
            "rxn99999",
        ]

    def test_base_reaction_id_strips_only_a_compartment_suffix(self):
        assert recon._base_reaction_id("rxn00029_c0") == "rxn00029"
        assert recon._base_reaction_id("rxn00029_e12") == "rxn00029"
        assert recon._base_reaction_id("rxn00029") == "rxn00029"
        assert recon._base_reaction_id("EX_cpd00027_e0") == "EX_cpd00027"

    def test_an_occurrence_that_adds_an_excluded_reaction_raises(self):
        """The blacklist is meant to keep these out of the LP.  This is the
        re-check on the object that ships: an excluded reaction arriving by any
        other route is a correctness failure, and must not be discoverable only
        downstream."""
        glc = _FakeMedia("Glc")

        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={"Glc": _solution(glc, new={"rxn00029_c0": ">"})}
            )
        )
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        with pytest.raises(RuntimeError) as excinfo:
            _run(
                fake_self,
                mdlutl,
                [glc],
                reaction_exclusion_list=["rxn00029"],
            )

        message = str(excinfo.value)
        assert "rxn00029_c0" in message
        assert "occurrence 0" in message
        assert "'Glc'" in message

    def test_the_compartment_suffixed_form_is_caught_too(self):
        """Supplying ``rxn00029_c0`` must mean the same thing as ``rxn00029``,
        in both directions: normalised on the way in, matched on the way out."""
        glc = _FakeMedia("Glc")

        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={"Glc": _solution(glc, new={"rxn00029_c0": ">"})}
            )
        )
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        with pytest.raises(RuntimeError):
            _run(
                fake_self, mdlutl, [glc], reaction_exclusion_list=["rxn00029_c0"]
            )

    def test_an_excluded_reaction_already_on_the_model_is_a_comment(self):
        """It was not gap-filled in, so it is not a violation of a GAP-FILL
        exclusion.  Deleting an annotation-supported reaction because a gap-fill
        exclusion happened to name it would be a much worse answer."""
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1", "rxn00029_c0")))

        current_output = _run(
            _FakeSelf(),
            mdlutl,
            [_FakeMedia("Glc")],
            reaction_exclusion_list=["rxn00029"],
        )[0]

        assert any(
            "rxn00029_c0" in comment for comment in current_output["Comments"]
        )
        assert "rxn00029_c0" in {r.id for r in mdlutl.model.reactions}

    def test_no_exclusions_means_an_empty_blacklist(self):
        fake_self = _FakeSelf()
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        current_output = _run(fake_self, mdlutl, [_FakeMedia("Glc")])[0]

        assert CONSTRUCTED[-1].construction["blacklist"] == []
        assert current_output["Comments"] == []


# ---------------------------------------------------------------------------
# prefilter
# ---------------------------------------------------------------------------


class TestPrefilter:
    """Measured (smoke section (d)): both settings complete and return the
    IDENTICAL solution, and ``True`` was never the faster of the two."""

    def _calls(self, **kwargs):
        fake_self = _FakeSelf()
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))
        _run(
            fake_self,
            mdlutl,
            [_FakeMedia("Glc"), _FakeMedia("Succ"), _FakeMedia("Ac")],
            **kwargs,
        )
        return [call["prefilter"] for call in CONSTRUCTED[-1].gapfill_calls]

    def test_prefilter_defaults_to_false(self):
        assert self._calls() == [False, False, False]

    def test_prefilter_true_applies_to_the_first_occurrence_only(self):
        """Prefiltering permanently narrows the SHARED gap-fill database, so
        repeating it per medium would restrict later media to whatever an
        earlier medium left behind.  Upstream's own ``run_multi_gapfill``
        prefilters only its first medium for the same reason."""
        assert self._calls(prefilter=True) == [True, False, False]


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------


class TestStatuses:
    def test_no_solution_when_gapfilling_returns_nothing(self):
        mdlutl = _FakeMdlUtl(_two_media_model())

        records = _run(_FakeSelf(), mdlutl, [_FakeMedia("Glc")])[4]

        assert records[0].status == "no_solution"
        assert records[0].additions == ()
        assert records[0].objective_value == 0.0

    def test_an_exception_is_recorded_and_the_next_occurrence_still_runs(self):
        glc = _FakeMedia("Glc")
        succ = _FakeMedia("Succ")

        mdlutl = _FakeMdlUtl(_two_media_model())
        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={
                    "Glc": ValueError("solver exploded"),
                    "Succ": _solution(succ, new={"rxn_b_c0": ">"}),
                }
            )
        )

        records = _run(fake_self, mdlutl, [glc, succ])[4]

        assert records[0].status == "error:ValueError"
        assert records[0].additions == ()
        assert records[1].status == "integrated"

    def test_a_solution_that_would_add_nothing_is_not_integrated(self):
        """Integrating it can only prune: ``integrate_gapfill_solution`` closes
        with ``test_solution(keep_changes=True)``, which REMOVES reactions it
        judges unneeded."""
        glc = _FakeMedia("Glc")
        # Every reaction the solution names is already on the model, with
        # bounds integration would not re-open.
        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={
                    "Glc": _solution(
                        glc, new={"bio1": ">"}, reversed_={"bio1": ">"}
                    )
                }
            )
        )
        mdlutl = _FakeMdlUtl(
            _FakeModel(reaction_ids=("bio1",), requirements={"Glc": ("rxn_a_c0",)})
        )

        records = _run(fake_self, mdlutl, [glc])[4]

        assert CONSTRUCTED[-1].integrate_calls == []
        assert records[0].status == "no_solution"

    def test_a_closed_bound_the_solution_would_reopen_counts_as_a_change(self):
        glc = _FakeMedia("Glc")

        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={"Glc": _solution(glc, reversed_={"rxn_a_c0": "<"})}
            )
        )
        # The model does NOT already grow (it still lacks rxn_needed_c0), so
        # the occurrence is attempted; the solution re-opens a bound rather
        # than adding anything.
        model = _FakeModel(
            reaction_ids=("bio1", "rxn_a_c0"),
            requirements={"Glc": ("rxn_needed_c0",)},
        )
        # lower_bound >= 0, so integration WOULD re-open it.
        model.reactions.get_by_id("rxn_a_c0").lower_bound = 0.0
        mdlutl = _FakeMdlUtl(model)

        records = _run(fake_self, mdlutl, [glc])[4]

        assert records[0].status == "integrated"
        assert records[0].additions == ()
        assert records[0].reversals == ("rxn_a_c0",)

    def test_additions_are_measured_off_the_model_not_the_solution(self):
        """Integration prunes, so the solution is what was PROPOSED and the
        record must report what was KEPT."""
        glc = _FakeMedia("Glc")

        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={
                    "Glc": _solution(
                        glc, new={"rxn_a_c0": ">", "rxn_pruned_c0": ">"}
                    )
                },
                prune=("rxn_pruned_c0",),
            )
        )
        mdlutl = _FakeMdlUtl(_two_media_model())

        records = _run(fake_self, mdlutl, [glc])[4]

        assert records[0].additions == ("rxn_a_c0",)
        assert "rxn_pruned_c0" not in records[0].additions


# ---------------------------------------------------------------------------
# ATP tests, and not reaching KBase for them
# ---------------------------------------------------------------------------


class TestATPTests:
    def test_the_published_tests_are_used_and_no_template_is_fetched(self):
        """``build_metabolic_model`` publishes the tests MSATPCorrection built
        on ``mdlutl.atp_tests`` at the POST-ATP boundary.  Reading them is what
        lets a KBase-free job gap-fill ATP-safely: ``self.templates["core"]`` is
        a KBase workspace reference."""
        fake_self = _FakeSelf()
        mdlutl = _FakeMdlUtl(
            _FakeModel(reaction_ids=("bio1",)),
            atp_tests=[{"source": "published"}],
            with_get_atp_tests=True,
        )

        _run(fake_self, mdlutl, [_FakeMedia("Glc")], atp_safe=True)

        assert CONSTRUCTED[-1].construction["test_conditions"] == [
            {"source": "published"}
        ]
        assert fake_self.template_fetches == []
        assert mdlutl.get_atp_tests_calls == []

    def test_additional_tests_are_prepended(self):
        mdlutl = _FakeMdlUtl(
            _FakeModel(reaction_ids=("bio1",)), atp_tests=[{"source": "published"}]
        )

        _run(
            _FakeSelf(),
            mdlutl,
            [_FakeMedia("Glc")],
            atp_safe=True,
            additional_tests=[{"source": "caller"}],
        )

        assert CONSTRUCTED[-1].construction["test_conditions"] == [
            {"source": "caller"},
            {"source": "published"},
        ]

    def test_the_fork_only_fallback_is_used_when_the_model_carries_none(self):
        """``MSModelUtil.get_atp_tests`` is FORK-ONLY -- absent from the pinned
        upstream revision, which has neither the method nor an ``atp_tests``
        attribute -- so calling it unconditionally raises AttributeError
        against the pin."""
        fake_self = _FakeSelf()
        mdlutl = _FakeMdlUtl(
            _FakeModel(reaction_ids=("bio1",)), with_get_atp_tests=True
        )

        _run(fake_self, mdlutl, [_FakeMedia("Glc")], atp_safe=True)

        assert len(mdlutl.get_atp_tests_calls) == 1
        assert mdlutl.get_atp_tests_calls[0]["core_template"] == (
            "template_from_core_ref"
        )
        assert fake_self.template_fetches == ["core_ref"]

    def test_a_supplied_core_template_is_not_refetched_for_the_fallback(self):
        fake_self = _FakeSelf()
        mdlutl = _FakeMdlUtl(
            _FakeModel(reaction_ids=("bio1",)), with_get_atp_tests=True
        )

        _run(
            fake_self,
            mdlutl,
            [_FakeMedia("Glc")],
            atp_safe=True,
            core_template="supplied_core",
        )

        assert fake_self.template_fetches == []
        assert mdlutl.get_atp_tests_calls[0]["core_template"] == "supplied_core"

    def test_no_tests_and_no_way_to_compute_them_raises_a_capability_error(self):
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        with pytest.raises(ModelSEEDpyCapabilityError) as excinfo:
            _run(_FakeSelf(), mdlutl, [_FakeMedia("Glc")], atp_safe=True)

        message = str(excinfo.value)
        assert "atp_safe=False" in message
        assert "get_atp_tests" in message

    def test_atp_safe_false_asks_for_nothing(self):
        fake_self = _FakeSelf()
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        _run(fake_self, mdlutl, [_FakeMedia("Glc")], atp_safe=False)

        assert CONSTRUCTED[-1].construction["test_conditions"] == []
        assert fake_self.template_fetches == []


# ---------------------------------------------------------------------------
# The ModelSEEDpy API boundary
# ---------------------------------------------------------------------------


class TestModelSEEDpyKeywordBoundary:
    """Fork and pin differ in SIGNATURE while reporting 0.4.2 and 0.4.3, so
    every optional parameter is probed rather than assumed."""

    def test_fork_only_keywords_are_dropped_when_not_requested(self):
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        _run(_FakeSelf(), mdlutl, [_FakeMedia("Glc")])

        assert "base_media" not in CONSTRUCTED[-1].construction

    def test_fork_only_keywords_are_forwarded_where_the_signature_has_them(self):
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        _run(
            _FakeSelf(gapfill_class=_ForkMSGapfill),
            mdlutl,
            [_FakeMedia("Glc")],
            base_media="a_base_media",
            base_media_target_element="N",
        )

        assert CONSTRUCTED[-1].construction["base_media"] == "a_base_media"
        assert (
            CONSTRUCTED[-1].construction["base_media_target_element"] == "N"
        )

    def test_requesting_a_keyword_the_install_lacks_raises(self):
        """Silently dropping a setting the caller explicitly asked for would
        change the computation with nothing in the output saying so."""
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        with pytest.raises(ModelSEEDpyCapabilityError) as excinfo:
            _run(
                _FakeSelf(),
                mdlutl,
                [_FakeMedia("Glc")],
                base_media="a_base_media",
            )

        assert "base_media" in str(excinfo.value)

    def test_reaction_scores_are_passed_at_construction(self):
        """``MSGapfill.__init__`` hands ``reaction_scores`` straight to
        ``GapfillingPkg.build_package``, so assigning the attribute afterwards
        -- which is what this method used to do -- never reached the package."""
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))
        scores = {"rxn00029": {"gene1": {"probability": 0.5}}}

        _run(
            _FakeSelf(),
            mdlutl,
            [_FakeMedia("Glc")],
            reaction_scores=scores,
        )

        assert CONSTRUCTED[-1].construction["reaction_scores"] == scores

    def test_the_target_and_minimum_objective_reach_every_call(self):
        mdlutl = _FakeMdlUtl(_two_media_model())

        _run(
            _FakeSelf(),
            mdlutl,
            [_FakeMedia("Glc")],
            objective="bio2",
            minimum_objective=0.05,
        )

        instance = CONSTRUCTED[-1]
        assert instance.construction["default_target"] == "bio2"
        assert instance.construction["minimum_obj"] == 0.05
        assert instance.gapfill_calls[0]["target"] == "bio2"
        assert instance.gapfill_calls[0]["minimum_obj"] == 0.05


class TestTheObjectiveIsSetExplicitly:
    """``MSATPCorrection.build_tests`` ends with
    ``self.model.objective = self.atp_hydrolysis.id`` and never restores it, so
    a POST-ATP model's objective is ATP flux.  A bare ``slim_optimize()`` there
    would read ATP flux as growth."""

    def test_every_measurement_sets_the_objective_first(self):
        mdlutl = _FakeMdlUtl(_two_media_model())

        _run(_FakeSelf(), mdlutl, [_FakeMedia("Glc")], objective="bio1")

        assert mdlutl.model.objectives_seen == ["bio1"]
        assert mdlutl.model.objective == "bio1"

    def test_the_medium_is_loaded_before_each_measurement(self):
        mdlutl = _FakeMdlUtl(_two_media_model())
        glc = _FakeMedia("Glc")

        _run(_FakeSelf(), mdlutl, [glc])

        media_builds = mdlutl.pkgmgr.getpkg("KBaseMediaPkg").builds
        assert glc in media_builds


# ---------------------------------------------------------------------------
# gapfilling_mode
# ---------------------------------------------------------------------------


class TestGapfillingMode:
    def test_sequential_is_accepted(self):
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        returned = _run(
            _FakeSelf(), mdlutl, [_FakeMedia("Glc")], gapfilling_mode="Sequential"
        )

        assert len(returned) == 5

    @pytest.mark.parametrize("mode", ["Cumulative", "Comprehensive", "sequential"])
    def test_anything_else_raises_rather_than_being_ignored(self, mode):
        """Forwarding it was never honoured: the pinned ``run_multi_gapfill``
        has no ``gapfilling_mode`` parameter at all, so the old call raised
        ``TypeError`` against upstream.  Accepting the value and ignoring it
        would be worse."""
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        with pytest.raises(ValueError) as excinfo:
            _run(
                _FakeSelf(), mdlutl, [_FakeMedia("Glc")], gapfilling_mode=mode
            )

        assert mode in str(excinfo.value)


# ---------------------------------------------------------------------------
# The return contract
# ---------------------------------------------------------------------------


class TestReturnContract:
    """Three call sites unpack exactly four values:
    ``kb_gapfill_metabolic_models``, ``interfaces/cli/model.py`` and a
    documented example in ``beril/skills/kbu-fba/SKILL.md``."""

    def test_the_default_return_is_still_a_four_tuple(self):
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        returned = _FakeSelf().gapfill_metabolic_model(
            mdlutl=mdlutl,
            genome=None,
            media_objs=[_FakeMedia("Glc")],
            templates=["gs_template"],
            atp_safe=False,
        )

        assert len(returned) == 4
        current_output, solutions, output_solution, output_media = returned
        assert isinstance(current_output, dict)
        assert solutions == {}
        assert output_solution is None
        assert output_media is None

    def test_the_records_are_a_fifth_element_when_asked_for(self):
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1",)))

        returned = _run(_FakeSelf(), mdlutl, [_FakeMedia("Glc")])

        assert len(returned) == 5

    def test_the_current_output_keys_are_unchanged(self):
        mdlutl = _FakeMdlUtl(_FakeModel(reaction_ids=("bio1", "EX_cpd00027_e0")))

        current_output = _run(_FakeSelf(), mdlutl, [_FakeMedia("Glc")])[0]

        assert set(current_output) == {
            "Model",
            "Genome",
            "Genes",
            "Class",
            "Model genes",
            "Reactions",
            "Core GF",
            "GS GF",
            "Growth",
            "Comments",
        }
        assert current_output["Reactions"] == 1
        assert current_output["GS GF"] == 0

    def test_solutions_is_still_keyed_by_media_object(self):
        glc = _FakeMedia("Glc")

        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={"Glc": _solution(glc, new={"rxn_a_c0": ">"})}
            )
        )
        mdlutl = _FakeMdlUtl(_two_media_model())

        solutions = _run(fake_self, mdlutl, [glc])[1]

        assert list(solutions) == [glc]

    def test_the_representative_solution_is_the_first_occurrence_that_grew(self):
        succ = _FakeMedia("Succ")
        glc = _FakeMedia("Glc")
        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={
                    # Succ is requested first but cannot be solved.
                    "Glc": _solution(glc, new={"rxn_a_c0": ">"}),
                }
            )
        )
        mdlutl = _FakeMdlUtl(_two_media_model())

        returned = _run(fake_self, mdlutl, [succ, glc])

        _out, _solutions, output_solution, output_media = returned[:4]
        assert output_media is glc
        assert output_solution is not None
        assert PFBA_CALLS == [mdlutl.model]

    def test_the_gapfilled_reactions_fluxes_are_forced_in_that_solution(self):
        """Carried over unchanged from the previous implementation: a gap-filled
        reaction is pinned to its solution direction so a report cannot show a
        reaction that was added as carrying no flux."""
        glc = _FakeMedia("Glc")
        fake_self = _FakeSelf(
            gapfill_class=_planned(
                plan={
                    "Glc": _solution(
                        glc, new={"rxn_a_c0": ">"}, reversed_={"bio1": "<"}
                    )
                }
            )
        )
        mdlutl = _FakeMdlUtl(_two_media_model())
        mdlutl.model.reactions.get_by_id("bio1").lower_bound = 0.0

        output_solution = _run(fake_self, mdlutl, [glc])[2]

        assert output_solution.fluxes["rxn_a_c0"] == 1000
        assert output_solution.fluxes["bio1"] == -1000


# ---------------------------------------------------------------------------
# Staging into ReconstructionResult
# ---------------------------------------------------------------------------


def _reconstruction_result(**overrides):
    fields = {
        "draft_reaction_count": 957,
        "draft_metabolite_count": 967,
        "draft_gene_count": 588,
        "draft_exchange_count": 73,
        "draft_drain_count": 4,
        "draft_biomass_ids": ("bio1",),
        "draft_objective_expression": "1.0*bio1",
        "draft_atpm_present": False,
        "post_atp_correction_ran": True,
        "post_atp_correction_invocations": 1,
        "post_atp_reaction_count": 958,
        "post_atp_removed_reaction_ids": (),
        "post_atp_core_gapfilling_count": 0,
        "post_atp_selected_media_ids": ("Glc",),
        "post_atp_test_count": 4,
        "post_atp_analysis": {},
        "post_gapfill_results": (),
        "genome_id": "71421.8",
        "genome_class": None,
        "template_id": "GramNegative.modeltemplate",
        "template_name": "GramNegative",
        "core_template_id": "CoreModelTemplateV2",
        "core_template_name": "Core",
        "ontology_term": "RAST",
        "match_role_aliases": False,
        "allow_incomplete_complexes": True,
        "reactions_to_add_supplied": False,
        "modelseedpy_version": "0.4.3",
        "modelseedpy_revision": "e1155a4",
        "kbutillib_version": "1.0.0",
        "kbutillib_revision": "unknown",
    }
    fields.update(overrides)
    return recon.ReconstructionResult(**fields)


class TestStagingIntoTheReconstructionResult:
    def _records(self):
        return (
            GapfillRecord(
                request_index=0,
                media_id="Glc",
                media_digest="sha256:aaaa",
                status="integrated",
                additions=("rxn_a_c0",),
                reversals=(),
                objective_value=0.65,
            ),
            GapfillRecord(
                request_index=1,
                media_id="Glc",
                media_digest="sha256:aaaa",
                status="no_gaps",
                additions=(),
                reversals=(),
                objective_value=0.65,
            ),
        )

    def test_build_leaves_the_field_empty_and_gapfill_fills_it(self):
        from_build = _reconstruction_result()
        assert from_build.post_gapfill_results == ()

        staged = MSReconstructionUtils.with_gapfill_results(
            result=from_build, gapfill_records=self._records()
        )

        assert staged.post_gapfill_results == self._records()
        assert all(
            isinstance(r, GapfillRecord) for r in staged.post_gapfill_results
        )

    def test_the_input_result_is_not_mutated(self):
        """``ReconstructionResult`` is frozen precisely so a result cannot be
        edited after the stage it describes has passed."""
        from_build = _reconstruction_result()

        staged = MSReconstructionUtils.with_gapfill_results(
            result=from_build, gapfill_records=self._records()
        )

        assert from_build.post_gapfill_results == ()
        assert staged is not from_build

    def test_every_other_field_survives_unchanged(self):
        from_build = _reconstruction_result()

        staged = MSReconstructionUtils.with_gapfill_results(
            result=from_build, gapfill_records=self._records()
        )

        before = dataclasses.asdict(from_build)
        after = dataclasses.asdict(staged)
        before.pop("post_gapfill_results")
        after.pop("post_gapfill_results")
        assert before == after

    def test_a_list_of_records_becomes_a_tuple(self):
        staged = MSReconstructionUtils.with_gapfill_results(
            result=_reconstruction_result(), gapfill_records=list(self._records())
        )

        assert isinstance(staged.post_gapfill_results, tuple)

    def test_the_field_is_still_declared_open(self):
        """The ELEMENT type is tightened by :class:`GapfillRecord`; the field's
        own annotation stays ``Tuple[Any, ...]`` so it does not become a
        promise binding every future producer."""
        annotations = {
            f.name: f.type for f in dataclasses.fields(recon.ReconstructionResult)
        }

        declared = annotations["post_gapfill_results"]
        assert str(declared) in (
            "Tuple[Any, ...]",
            "typing.Tuple[typing.Any, ...]",
        ), declared

    def test_the_staged_result_is_still_json_serialisable(self):
        import json

        staged = MSReconstructionUtils.with_gapfill_results(
            result=_reconstruction_result(), gapfill_records=self._records()
        )

        payload = json.loads(json.dumps(staged.to_dict()))

        assert payload["post_gapfill_results"][0]["media_id"] == "Glc"
        assert payload["post_gapfill_results"][1]["status"] == "no_gaps"


# ---------------------------------------------------------------------------
# Nothing from GAA
# ---------------------------------------------------------------------------


def test_no_gaa_import_and_no_gapfill_reaction_set():
    """GAA's ``gapfill_reaction_set`` is neither called nor ported: all
    modelling lives in this layer now, and ``genome_annotation_aggregator`` is
    not a dependency the new code may import."""
    source = __import__("pathlib").Path(recon.__file__).read_text()

    assert "genome_annotation_aggregator" not in source
    # It is NAMED in two comments that explain why it is gone; what must not
    # exist is a call or an attribute reference.
    assert "gapfill_reaction_set(" not in source
    assert ".gapfill_reaction_set" not in source
