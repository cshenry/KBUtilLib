"""The ATP filter, returned once and replayed exactly (PRD kbdl-fitness-off-gaa-v1).

Chris, 2026-11-21: *"On the ATP filtering you MUST MUST MUST ensure it only runs
once per model and you save the filter list."*  ``MSATPCorrection`` plus
``GapfillingPkg.filter_database_based_on_tests`` costs 528 s per KBDL job on real
Keio data and re-runs on every job and every parallel shard.  These tests are the
evidence that a stored filter reproduces the computed one EXACTLY, rather than
approximately, so that skipping the 528 s is safe.

Like ``test_ms_fitness_utils.py`` these run against the ACTUALLY INSTALLED
ModelSEEDpy: every claim here is a claim about what Jose Faria's
``MSFitnessWeightedAnalysis`` and ``GapfillingPkg`` do, and a fake answers none of
them.  The fixtures (``_model``, ``_template``, ``BIOMASS``) are that module's,
imported rather than copied so the two suites cannot drift onto different toys.

**Why the knockout set.**  On plain ``e_coli_core`` the core BiGG template
contributes NO candidates (``n_candidates == 0``), so the ATP filter closes
nothing and an equivalence test over it would prove nothing.  :data:`FILTER_KO`
removes seven reactions the template can put back, which makes the filter close
seven directions covering all three closures ModelSEEDpy records -- ``">"``,
``"<"`` and ``"><"`` -- so a replay that mixed up any one of them shows up as
different superset bounds.

Running them::

    TMPDIR=/scratch/chenry/tmp KBU_REQUIRE_MODEL_STACK=1 \\
    python -m pytest tests/modeling/test_ms_fitness_atp_filter.py -v
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, List, Tuple

import pytest

from tests.conftest import require_model_stack

pytestmark = pytest.mark.kbu_model

# Module-level: a module-scope skip happens during collection, too early for
# conftest's pytest_runtest_setup guard to convert it into a failure.
require_model_stack()

from kbutillib.domains.modeling import ms_fitness_utils as fitness  # noqa: E402
from tests.modeling.test_ms_fitness_utils import (  # noqa: E402
    BIOMASS,
    _model,
    _template,
    _template_dict,
)

#: Reactions removed from ``e_coli_core`` so the core template has something to
#: offer -- and so the ATP filter has something to close.  Measured, not
#: guessed: with these out the filter closes PFL_c0 ``">"``, FORt_c0 ``"<"`` and
#: LDH_D_c0 ``"><"`` among others, which is every closure branch.
FILTER_KO: Tuple[str, ...] = (
    "SUCDi_c0",
    "FRD7_c0",
    "LDH_D_c0",
    "D_LACt2_c0",
    "PFL_c0",
    "FORt2_c0",
    "FORt_c0",
)

#: Keys whose values are wall-clock times or provenance labels, and so are
#: EXPECTED to differ between a computed run and a replayed one.  Everything
#: else in the result must be identical.
TIMING_KEYS = frozenset({"timing", "compute_s", "source"})


def _is_timing_key(key: Any) -> bool:
    return isinstance(key, str) and (key in TIMING_KEYS or key.startswith("time_"))


def _without_timing(value: Any) -> Any:
    """``value`` with every timing and provenance key stripped, recursively.

    Args:
        value: Any part of a result dict.

    Returns:
        A copy carrying no :data:`TIMING_KEYS` and no ``time_*`` key, so two
        results can be compared for everything that is not a clock reading.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _is_timing_key(key):
                continue
            item = _without_timing(item)
            if key == "edge_evidence" and isinstance(item, list):
                item = _sorted_edge_evidence(item)
            out[key] = item
        return out
    if isinstance(value, list):
        return [_without_timing(v) for v in value]
    return value


def _sorted_edge_evidence(rows: List[Any]) -> List[Any]:
    """``edge_evidence`` rows in a stable order.

    Args:
        rows: One condition's edge-evidence rows.

    Returns:
        The same rows sorted by ``(gene, reaction, source)``.

    The ORDER of these rows is not a result and is not reproducible even between
    two identical computed runs: it follows ``gene_fit_best``'s iteration over a
    set, so the six ``ATPS4r_c0`` subunit genes come out in a different order
    each time.  Measured on this toy -- two back-to-back computed runs with the
    same inputs disagree on the order and agree exactly as a set.  Sorting here
    compares what the rows SAY, which is the thing a replay has to reproduce,
    and is not a weakening: every field of every row is still compared.
    """
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("gene")),
            str(row.get("reaction")),
            str(row.get("source")),
        )
        if isinstance(row, dict)
        else (str(row), "", ""),
    )


#: Relative tolerance every float in a result is compared at.  The superset
#: BOUNDS, ``filtered``, statuses and gap-fill sets are compared EXACTLY -- they
#: are what the replay reproduces -- but a number read back off an LP solution
#: is not bit-reproducible even between two solves of an identical model: GLPK's
#: simplex path depends on the order the bounds were written, and a replay
#: writes them in a different order than the filter did.  Measured drift on this
#: toy is ~2e-15 relative on ``growth_superset``; 1e-9 is the tolerance the PRD
#: names for the objective and is six orders of magnitude above the noise, so it
#: still fails any real difference in what was solved.
FLOAT_REL = 1e-9


def _assert_equivalent(actual: Any, expected: Any, path: str = "result") -> None:
    """Assert two results agree everywhere, floats to :data:`FLOAT_REL`.

    Args:
        actual: The replayed value.
        expected: The computed value.
        path: Dotted location, so a failure says WHICH field differs rather
            than dumping two whole result dicts.

    Raises:
        AssertionError: They differ.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: not a dict"
        assert set(actual) == set(expected), (
            f"{path}: keys differ -- only replayed {sorted(set(actual) - set(expected))}, "
            f"only computed {sorted(set(expected) - set(actual))}"
        )
        for key in expected:
            _assert_equivalent(actual[key], expected[key], f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: not a list"
        assert len(actual) == len(expected), f"{path}: length differs"
        for index, item in enumerate(expected):
            _assert_equivalent(actual[index], item, f"{path}[{index}]")
        return
    if isinstance(expected, float) and not isinstance(expected, bool):
        assert actual == pytest.approx(expected, rel=FLOAT_REL, abs=1e-12), (
            f"{path}: {actual!r} != {expected!r}"
        )
        return
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


@pytest.fixture(scope="module")
def glucose() -> Any:
    from modelseedpy import MSMedia

    return MSMedia.from_dict(
        {
            "glc__D": (-10, 1000),
            "o2": (-1000, 1000),
            "h": (-1000, 1000),
            "h2o": (-1000, 1000),
            "pi": (-1000, 1000),
            "co2": (-1000, 1000),
            "nh4": (-1000, 1000),
        }
    )


def _conditions(glucose: Any) -> Dict[str, Dict[str, Any]]:
    """Two conditions, so equivalence is tested on more than one solve.

    Args:
        glucose: The glucose-minimal medium.

    Returns:
        A priced condition and a Complete-leg one (``require_fitness=False``),
        which take different routes through the wrapper.
    """
    return {
        "glc": {"media": glucose, "fitness": {"b4025": (-3.0, -10.0)}},
        "glc_complete": {
            "media": glucose,
            "fitness": {},
            "require_fitness": False,
        },
    }


def _run(glucose: Any, **kwargs: Any) -> Tuple[Dict[str, Any], Any]:
    """One ``atp_safe`` run, with the analysis object it built.

    Args:
        glucose: The medium fixture.
        **kwargs: Overrides forwarded to
            :func:`~kbutillib.domains.modeling.ms_fitness_utils.run_fitness_weighted_analysis`.

    Returns:
        ``(result, analysis)``.  The object is captured because the superset's
        bounds -- the thing a replay must reproduce exactly -- are not in the
        result.
    """
    from modelseedpy.core import msfitnessweighted as fw

    real = fw.MSFitnessWeightedAnalysis
    built: List[Any] = []

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kw: Any) -> None:
            super().__init__(*args, **kw)
            built.append(self)

    call: Dict[str, Any] = {
        "model": _model(ko=list(FILTER_KO)),
        "template": _template(),
        "core_template": _template(),
        "atp_safe": True,
        "biomass": BIOMASS,
        "loopless": False,
        "conditions": _conditions(glucose),
    }
    call.update(kwargs)
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(fw, "MSFitnessWeightedAnalysis", Recording)
        result = fitness.run_fitness_weighted_analysis(**call)
    finally:
        monkey.undo()
    return result, built[0]


@pytest.fixture(scope="module")
def computed(glucose: Any) -> Tuple[Dict[str, Any], Any]:
    """The computed run, built once: it pays the real MSATPCorrection cost."""
    return _run(glucose)


def _bounds(analysis: Any) -> Dict[str, Tuple[float, float]]:
    return {rxn.id: rxn.bounds for rxn in analysis.superset.reactions}


# ---------------------------------------------------------------------------
# The equivalence test: the one that matters
# ---------------------------------------------------------------------------


class TestComputedVersusReplayed:
    """A replayed filter must leave the run indistinguishable from a computed one."""

    def test_the_filter_actually_closes_directions(
        self, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        """Without this the equivalence test below proves nothing.

        ``n_filtered_directions > 0`` is the premise: a fixture the filter does
        nothing to would let a replay that applied NOTHING pass every assertion.
        """
        result, analysis = computed
        stored = result["atp_filter"]
        assert stored["n_filtered_directions"] > 0
        assert stored["n_filtered_directions"] == len(analysis.filtered)
        assert result["superset"]["n_filtered_directions"] == len(analysis.filtered)
        # All three closures, so a replay cannot pass by confusing them.
        assert set(stored["filtered"].values()) == {">", "<", "><"}
        assert stored["source"] == "computed"
        assert stored["format"] == fitness.ATP_FILTER_FORMAT

    def test_replay_reproduces_the_superset_and_the_whole_result(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        """Bounds, ``filtered``, statuses, objectives, gap-fills and every row."""
        computed_result, computed_analysis = computed
        stored = computed_result["atp_filter"]

        replayed_result, replayed_analysis = _run(glucose, atp_filter=stored)

        # The superset itself, reaction by reaction.
        assert _bounds(replayed_analysis) == _bounds(computed_analysis)
        assert replayed_analysis.filtered == computed_analysis.filtered
        assert replayed_analysis.reversals == computed_analysis.reversals
        assert replayed_analysis.candidates == computed_analysis.candidates
        assert replayed_analysis.priced == computed_analysis.priced

        # Per condition: status, objective, gap-fill set and reversals used.
        for cid, computed_condition in computed_result["conditions"].items():
            replayed_condition = replayed_result["conditions"][cid]
            assert replayed_condition["status"] == computed_condition["status"]
            assert replayed_condition["gapfilled"] == computed_condition["gapfilled"]
            assert (
                replayed_condition["reversals_used"]
                == computed_condition["reversals_used"]
            )
            if computed_condition.get("objective") is None:
                assert replayed_condition.get("objective") is None
            else:
                assert replayed_condition["objective"] == pytest.approx(
                    computed_condition["objective"], rel=1e-9
                )

        # And then everything else -- summary rows, evidence rollup, fluxes,
        # the ATP leak reading -- apart from the clock readings.
        _assert_equivalent(
            _without_timing(replayed_result), _without_timing(computed_result)
        )

        # The replay is reported as one, and the filter it carries is the same.
        assert replayed_result["atp_filter"]["source"] == "replayed"
        assert replayed_result["atp_filter"]["filtered"] == stored["filtered"]
        assert replayed_result["atp_filter"]["key"]["digest"] == (
            stored["key"]["digest"]
        )
        assert replayed_analysis.timing["atp_filter"] >= 0.0

    def test_a_json_round_trip_replays_the_same_way(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        """The stored object has to survive the store KBDL will actually use."""
        computed_result, computed_analysis = computed
        stored = computed_result["atp_filter"]

        reloaded = json.loads(json.dumps(stored))
        assert reloaded == stored  # nothing in it needed a custom encoder

        replayed_result, replayed_analysis = _run(glucose, atp_filter=reloaded)
        assert _bounds(replayed_analysis) == _bounds(computed_analysis)
        _assert_equivalent(
            _without_timing(replayed_result), _without_timing(computed_result)
        )
        assert replayed_result["atp_filter"]["source"] == "replayed"


# ---------------------------------------------------------------------------
# The 528 seconds are actually skipped
# ---------------------------------------------------------------------------


class TestReplaySkipsTheExpensiveWork:
    def test_neither_msatpcorrection_nor_the_filter_is_called(
        self,
        glucose: Any,
        computed: Tuple[Dict[str, Any], Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both cost centres are booby-trapped, so reaching either fails the test.

        An equivalent result is not enough on its own: a replay that recomputed
        the filter and happened to get the same answer would satisfy every
        assertion above while still costing 528 s per job.
        """
        from modelseedpy.core import msatpcorrection
        from modelseedpy.fbapkg.gapfillingpkg import GapfillingPkg

        def _never_atp(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "MSATPCorrection was constructed on a replay: the stored ATP "
                "filter was not used"
            )

        def _never_filter(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(
                "filter_database_based_on_tests was called on a replay: the "
                "stored ATP filter was not used"
            )

        monkeypatch.setattr(msatpcorrection, "MSATPCorrection", _never_atp)
        monkeypatch.setattr(
            GapfillingPkg, "filter_database_based_on_tests", _never_filter
        )

        result, _ = _run(glucose, atp_filter=computed[0]["atp_filter"])
        assert result["atp_filter"]["source"] == "replayed"
        assert result["conditions"]["glc"]["status"] == "done"


# ---------------------------------------------------------------------------
# A stored filter is replayed exactly, or refused -- never quietly recomputed
# ---------------------------------------------------------------------------


class TestKeyMismatchIsRefused:
    def test_a_changed_blacklist_names_blacklist(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        with pytest.raises(ValueError, match=r"key mismatch") as excinfo:
            _run(
                glucose,
                atp_filter=computed[0]["atp_filter"],
                blacklist=["rxn00062"],
            )
        assert "blacklist" in str(excinfo.value)

    def test_a_changed_core_template_names_core_template(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        payload = _template_dict()
        payload["reactions"] = [
            r for r in payload["reactions"] if r["id"] != "TALA_c"
        ]
        from modelseedpy.core.mstemplate import MSTemplateBuilder

        other_core = MSTemplateBuilder.from_dict(payload).build()

        with pytest.raises(ValueError, match=r"key mismatch") as excinfo:
            _run(
                glucose,
                atp_filter=computed[0]["atp_filter"],
                core_template=other_core,
            )
        message = str(excinfo.value)
        assert "core_template" in message
        # The genome-scale template did NOT change, so it must not be blamed.
        assert not re.search(r"(?<!core_)template:", message)

    def test_a_changed_model_names_model(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        with pytest.raises(ValueError, match=r"key mismatch") as excinfo:
            _run(
                glucose,
                atp_filter=computed[0]["atp_filter"],
                model=_model(ko=list(FILTER_KO) + ["TALA_c0"]),
            )
        assert "model" in str(excinfo.value)

    def test_a_changed_min_growth_names_method_params(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        """``min_growth`` reaches ``MSGapfill``, so it invalidates a filter."""
        with pytest.raises(ValueError, match=r"key mismatch") as excinfo:
            _run(
                glucose,
                atp_filter=computed[0]["atp_filter"],
                min_growth=0.5,
            )
        assert "method_params" in str(excinfo.value)

    def test_a_params_change_the_filter_cannot_see_is_not_a_mismatch(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        """``t_sig`` prices AFTER the superset exists, so the filter still holds.

        A key that invalidated on every method param would make the stored
        filter useless in practice: KBDL varies pricing knobs across jobs on the
        same model, which is exactly the case the cache has to serve.
        """
        result, analysis = _run(
            glucose, atp_filter=computed[0]["atp_filter"], t_sig=1.5
        )
        assert result["atp_filter"]["source"] == "replayed"
        assert analysis.filtered == computed[1].filtered


class TestMalformedIsRefused:
    def test_an_unknown_format_is_refused(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        payload = copy.deepcopy(computed[0]["atp_filter"])
        payload["format"] = "kbu-atp-filter/99"
        with pytest.raises(ValueError, match=r"unknown atp_filter format"):
            _run(glucose, atp_filter=payload)

    @pytest.mark.parametrize("field", ["key", "filtered", "atp_tests"])
    def test_a_missing_field_is_refused(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any], field: str
    ) -> None:
        payload = copy.deepcopy(computed[0]["atp_filter"])
        del payload[field]
        with pytest.raises(ValueError, match=rf"malformed atp_filter: no '{field}'"):
            _run(glucose, atp_filter=payload)

    def test_a_key_with_no_digest_is_refused(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        payload = copy.deepcopy(computed[0]["atp_filter"])
        del payload["key"]["digest"]
        with pytest.raises(ValueError, match=r"carries no `digest`"):
            _run(glucose, atp_filter=payload)

    def test_an_unknown_closure_is_refused(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        payload = copy.deepcopy(computed[0]["atp_filter"])
        payload["filtered"]["PFL_c0"] = "<>"
        with pytest.raises(ValueError, match=r"closures must each be one of"):
            _run(glucose, atp_filter=payload)

    def test_a_closure_on_an_absent_reaction_is_refused(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        """Half-applying a filter is worse than refusing it."""
        payload = copy.deepcopy(computed[0]["atp_filter"])
        payload["filtered"]["rxn99999_c0"] = "><"
        # `filtered` is NOT part of the key, so the digest still matches and
        # this reaches _apply_atp_filter rather than being refused earlier.
        with pytest.raises(
            ValueError, match=r"the superset does not have"
        ) as excinfo:
            _run(glucose, atp_filter=payload)
        assert "rxn99999_c0" in str(excinfo.value)

    def test_a_non_mapping_is_refused(self, glucose: Any) -> None:
        with pytest.raises(ValueError, match=r"atp_filter must be a mapping"):
            _run(glucose, atp_filter=["not", "a", "mapping"])


# ---------------------------------------------------------------------------
# atp_safe=False
# ---------------------------------------------------------------------------


class TestAtpSafeFalse:
    def test_no_filter_is_returned_and_a_supplied_one_is_ignored(
        self, glucose: Any, computed: Tuple[Dict[str, Any], Any]
    ) -> None:
        result, _ = _run(
            glucose,
            atp_safe=False,
            core_template=None,
            atp_filter=computed[0]["atp_filter"],
        )
        assert result["atp_filter"] is None
        # Ignored, not refused: no key is computed, so no mismatch can be raised.
        assert result["conditions"]["glc"]["status"] == "done"


# ---------------------------------------------------------------------------
# atp_filter_key: what it covers
# ---------------------------------------------------------------------------


class TestAtpFilterKey:
    def test_it_is_json_serialisable_and_its_digest_covers_its_body(self) -> None:
        key = fitness.atp_filter_key(
            model=_model(ko=list(FILTER_KO)),
            template=_template(),
            core_template=_template(),
            biomass=BIOMASS,
        )
        assert json.loads(json.dumps(key)) == key
        assert set(key) == {
            "format",
            "model",
            "template",
            "core_template",
            "blacklist",
            "atp_hydrolysis_id",
            "biomass",
            "method_params",
            "modelseedpy_sha",
            "solver",
            "digest",
        }
        assert key["format"] == fitness.ATP_FILTER_KEY_FORMAT
        assert key["biomass"] == BIOMASS
        assert key["atp_hydrolysis_id"] == "ATPM_c0"
        assert key["modelseedpy_sha"]
        assert key["solver"]
        assert set(key["method_params"]) == set(fitness.ATP_FILTER_METHOD_PARAMS)

    def test_it_is_stable_across_rebuilt_objects(self) -> None:
        """Content, not identity: two freshly built toys key the same."""
        first = fitness.atp_filter_key(
            model=_model(ko=list(FILTER_KO)),
            template=_template(),
            core_template=_template(),
            biomass=BIOMASS,
        )
        second = fitness.atp_filter_key(
            model=_model(ko=list(FILTER_KO)),
            template=_template(),
            core_template=_template(),
            biomass=BIOMASS,
        )
        assert first == second

    def test_an_explicit_default_keys_the_same_as_an_omitted_one(self) -> None:
        """The EFFECTIVE value is what the filter ran with."""
        from modelseedpy.core.msfitnessweighted import MIN_GROWTH

        omitted = fitness.atp_filter_key(
            model=_model(ko=list(FILTER_KO)),
            template=_template(),
            core_template=_template(),
            biomass=BIOMASS,
        )
        explicit = fitness.atp_filter_key(
            model=_model(ko=list(FILTER_KO)),
            template=_template(),
            core_template=_template(),
            biomass=BIOMASS,
            min_growth=MIN_GROWTH,
        )
        assert explicit["digest"] == omitted["digest"]

    @pytest.mark.parametrize(
        "field,override",
        [
            ("blacklist", {"blacklist": ["rxn00062"]}),
            ("biomass", {"biomass": "bio1"}),
            ("method_params", {"min_growth": 0.5}),
        ],
    )
    def test_a_changed_input_changes_the_digest(
        self, field: str, override: Dict[str, Any]
    ) -> None:
        base: Dict[str, Any] = {
            "model": _model(ko=list(FILTER_KO)),
            "template": _template(),
            "core_template": _template(),
            "biomass": BIOMASS,
        }
        first = fitness.atp_filter_key(**base)
        second = fitness.atp_filter_key(**dict(base, **override))
        assert first[field] != second[field]
        assert first["digest"] != second["digest"]

    def test_a_note_does_not_change_the_digest(self) -> None:
        """Content digest, so cosmetic model metadata must not invalidate a filter."""
        plain = _model(ko=list(FILTER_KO))
        noted = _model(ko=list(FILTER_KO))
        noted.id = "a-different-model-id"
        noted.name = "and a different name"
        noted.notes["provenance"] = "written by a different job"
        noted.reactions.get_by_id("PGI_c0").name = "renamed"
        common: Dict[str, Any] = {
            "template": _template(),
            "core_template": _template(),
            "biomass": BIOMASS,
        }
        assert (
            fitness.atp_filter_key(model=plain, **common)["digest"]
            == fitness.atp_filter_key(model=noted, **common)["digest"]
        )


# ---------------------------------------------------------------------------
# The repin: modelseedpy edafdb1 and its None-objective guard
# ---------------------------------------------------------------------------


class TestModelseedpyPin:
    """The repin, and the one behaviour it adds.

    That the INSTALLED modelseedpy is the commit ``pyproject.toml`` pins is
    already asserted by
    ``test_recon_build_real_dependency.py::TestProvenanceComesFromDistributionMetadata::test_that_revision_is_the_one_pyproject_pins``
    (:396 there), so it is not re-asserted here.
    """

    def test_an_infeasible_solve_no_longer_raises_typeerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``test_gapfill_database`` must return False, not blow up on ``None``.

        This is the whole content of the repin.  Before the guard,
        ``gapfillingpkg.py:651`` read ``if solution.objective_value <
        self.parameters["minimum_obj"]`` with no status check at all, and
        ``msatpcorrection.py:326`` compared the objective BEFORE testing the
        status -- so an infeasible solve, whose ``objective_value`` is ``None``,
        raised ``TypeError: '<' not supported between 'NoneType' and 'float'``
        from inside the ATP filter, nowhere near the cause.  The filter is
        reached on exactly the models KBDL runs, so this is a crash and not a
        cosmetic defect.
        """
        from modelseedpy import MSGapfill

        gapfiller = MSGapfill(
            _model(ko=list(FILTER_KO)),
            default_gapfill_templates=[_template()],
            default_target=BIOMASS,
            minimum_obj=0.01,
            test_conditions=[],
            reaction_scores={},
            blacklist=[],
        )
        pkg = gapfiller.gfpkgmgr.getpkg("GapfillingPkg")

        class _Infeasible:
            status = "infeasible"
            objective_value = None

        monkeypatch.setattr(pkg.model, "optimize", lambda *a, **k: _Infeasible())
        assert pkg.test_gapfill_database() is False
