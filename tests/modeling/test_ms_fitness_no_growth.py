"""No silent superset fallback (conduct row 1989, "Finding A").

Chris, 2026-11-21 23:55 CST, answering *"should I fix the KBUtilLib wrapper so a
model that can't grow returns no_growth, and the row reports which growth target
was actually used?"*: **"Yes"**.

``MSFitnessWeightedAnalysis.analyze_condition`` (modelseedpy ``edafdb1``) sets
``target = frac * opt_superset`` and only switches to ``frac * opt_model`` when
the model grows at ``min_growth`` or better::

    target = P["frac"] * opt_superset
    if (P["biomass_reference"] == "model"
            and opt_model is not None
            and opt_model >= P["min_growth"]):
        target = P["frac"] * opt_model

so a model that CANNOT grow is priced against the superset's optimum while the
row still reads ``model`` — observed on real Keio, condition
``no-carbon-control``: ``growth_model`` 0.0, ``biomass_fixed``
10.709949424823044, a 24-reaction gap-fill, reported as ``model``.

**The toy that reproduces it** is ``test_ms_fitness_utils``' own toy with the
glucose transporter knocked out (:data:`NO_GROWTH_KO`).  Measured on the base
commit ``65db6ea``: ``growth_model`` ``None``, ``growth_superset`` 0.8739,
``biomass_fixed`` 0.8739 (the superset's optimum), ``status`` ``done``, a
one-reaction gap-fill putting ``GLCpts_c0`` back, and 31 ``annotation_evidence``
rows — the same defect in miniature.  :class:`TestModelReferenceCannotGrow`
fails on that commit for exactly that reason.

Like its sibling modules these run against the ACTUALLY INSTALLED ModelSEEDpy,
because every claim here is a claim about what the class does; the fixtures are
``test_ms_fitness_utils``' own, imported rather than copied so the suites cannot
drift onto different toys.

Running them::

    TMPDIR=/scratch/chenry/tmp KBU_REQUIRE_MODEL_STACK=1 \\
    python -m pytest tests/modeling/test_ms_fitness_no_growth.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

from tests.conftest import require_model_stack

pytestmark = pytest.mark.kbu_model

# Module-level: a module-scope skip happens during collection, too early for
# conftest's pytest_runtest_setup guard to convert it into a failure.
require_model_stack()

from kbutillib.domains.modeling import ms_fitness_utils as fitness  # noqa: E402
from tests.modeling.test_ms_fitness_atp_filter import (  # noqa: E402
    _without_timing,
)
from tests.modeling.test_ms_fitness_utils import (  # noqa: E402
    BIOMASS,
    _duplicate_pgi,
    _model,
    _template,
)

#: The reaction whose absence makes the toy unable to grow while its superset
#: still can.  ``GLCpts_c0`` is ``e_coli_core``'s only glucose uptake route, and
#: the core BiGG template can put it back, so the superset grows at 0.87 and the
#: model itself is infeasible -- which is the whole configuration under test.
#: ``test_ms_fitness_utils.TestCandidatePricing`` uses the same knockout to open
#: the candidate tier.
NO_GROWTH_KO: Tuple[str, ...] = ("GLCpts_c0",)

#: The condition's fitness: the #44 defect, so a relabelled condition is one
#: that really did have fitness data and really was priced, not one that was
#: skipped for lack of input.
FIT: Dict[str, Tuple[float, float]] = {"b4025": (-3.0, -10.0)}

#: Every key ``analyze_condition`` sets that describes the superset-referenced
#: solve, and so must be GONE from a relabelled condition.  Spelled out rather
#: than derived from :data:`~kbutillib.domains.modeling.ms_fitness_utils.NO_GROWTH_SURVIVING_KEYS`
#: so the test states the contract independently of the implementation's
#: allow-list: a test that computed the complement of the shipped constant would
#: pass no matter what that constant said.
REMOVED_KEYS: Tuple[str, ...] = (
    "biomass_fixed",
    "loopless",
    "objective",
    "gapfilled",
    "reversals_used",
    "distance_to_growth",
    "annotation_gap",
    "fluxes",
    "growth_network",
    "reaction_calls",
    "edge_evidence",
)

#: The growing-model, ``biomass_reference="model"`` condition as the BASE commit
#: ``65db6ea`` reported it, timing stripped.  Captured by replaying this
#: module's ``_run`` against that commit; see the work record
#: ``agent-io/work-records/kbu-fitness-no-silent-superset.md``.
GOLDEN = (
    Path(__file__).parent.parent
    / "fixtures"
    / "model"
    / "fitness_growing_model_reference_base.json"
)

#: Relative tolerance for the golden comparison below.  The golden carries
#: solver output (``fluxes``, ``reaction_calls``, ``objective``), which is
#: reproducible to far better than this on one environment but is not bit-stable
#: across glpk builds.  Loose enough to survive a solver rebuild, tight enough
#: that a changed biomass target -- the defect this module is about, a factor of
#: 10 on real Keio -- could never hide inside it.
GOLDEN_REL_TOL = 1e-6


def _run(model: Any, template: Any, glucose: Any, **kwargs: Any) -> Dict[str, Any]:
    """One ``glc`` condition carrying :data:`FIT`, through the entry point.

    Args:
        model: The cobra model.
        template: The gap-fill template.
        glucose: The medium.
        **kwargs: Overrides passed straight through (``biomass_reference``, ...).

    Returns:
        The whole result dict, not just the condition: the relabel has to be
        visible in ``annotation_evidence`` too.
    """
    return fitness.run_fitness_weighted_analysis(
        model=model,
        template=template,
        biomass=BIOMASS,
        loopless=False,
        conditions={"glc": {"media": glucose, "fitness": FIT}},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# biomass_reference="model", and the model cannot grow
# ---------------------------------------------------------------------------


class TestModelReferenceCannotGrow:
    """The defect: this whole class fails on base commit ``65db6ea``.

    On that commit the condition is ``done`` with ``biomass_fixed`` equal to
    ``growth_superset``, a ``GLCpts_c0`` gap-fill and 31 evidence rows, so every
    assertion below about ``no_growth``, about the removed keys and about the
    empty rollup fails there -- and fails for the right reason, which is that
    the superset target was used silently.
    """

    def test_the_condition_is_relabelled_no_growth(
        self, dead_model_result: Dict[str, Any]
    ) -> None:
        assert dead_model_result["conditions"]["glc"]["status"] == "no_growth"

    def test_no_biomass_reference_was_actually_used(
        self, dead_model_result: Dict[str, Any]
    ) -> None:
        assert dead_model_result["conditions"]["glc"]["biomass_reference_used"] is None

    def test_the_superset_grew_and_the_model_did_not(
        self, dead_model_result: Dict[str, Any]
    ) -> None:
        """Both numbers survive: they are the EVIDENCE for the relabel.

        Without them a reader cannot tell this ``no_growth`` (the model is dead
        in a medium its superset handles -- a gap-fill target, if the caller
        asks for one) from the class's own ``no_growth`` (nothing grows at all).
        """
        condition = dead_model_result["conditions"]["glc"]
        assert condition["growth_superset"] > 0.0
        growth_model = condition["growth_model"]
        assert growth_model is None or growth_model < _min_growth()

    def test_not_one_field_of_the_superset_solve_survives(
        self, dead_model_result: Dict[str, Any]
    ) -> None:
        condition = dead_model_result["conditions"]["glc"]
        leaked = [key for key in REMOVED_KEYS if key in condition]
        assert leaked == [], f"superset-referenced fields leaked: {leaked}"

    def test_only_allow_listed_keys_and_the_wrapper_s_own_remain(
        self, dead_model_result: Dict[str, Any]
    ) -> None:
        """An allow-list, so a key a future modelseedpy adds cannot leak.

        The surviving set is pinned exactly rather than merely checked for the
        absence of :data:`REMOVED_KEYS`: a deny-list implementation would pass
        that check today and leak the next key the class learns to set.
        """
        condition = dead_model_result["conditions"]["glc"]
        survivors = {
            key for key in condition if not key.startswith(fitness.TIMING_KEY_PREFIX)
        }
        assert survivors == set(fitness.NO_GROWTH_SURVIVING_KEYS) | {
            # The wrapper's own post-processing keys, added after the relabel.
            "n_reactions_discounted",
            "biomass_reference_used",
        }

    def test_the_timing_keys_survive(self, dead_model_result: Dict[str, Any]) -> None:
        """The work was really done and really took that long."""
        condition = dead_model_result["conditions"]["glc"]
        assert isinstance(condition["time_s"], float)
        assert [
            key for key in condition if key.startswith(fitness.TIMING_KEY_PREFIX)
        ] != []

    def test_it_contributes_nothing_to_the_rollup(
        self, dead_model_result: Dict[str, Any]
    ) -> None:
        """31 rows on base; none here.

        ``aggregate_evidence`` is recomputed from the post-processed conditions,
        so dropping ``edge_evidence`` is what empties this -- the rollup cannot
        disagree with the rows.
        """
        assert dead_model_result["annotation_evidence"] == []
        assert dead_model_result["conditions"]["glc"]["n_reactions_discounted"] == 0


# ---------------------------------------------------------------------------
# biomass_reference="superset": the explicit way to ask for the gap-fill
# ---------------------------------------------------------------------------


class TestSupersetReferenceIsUnchanged:
    """The same dead toy, asked the other question, still answers it."""

    def test_it_still_solves_and_gap_fills(
        self, superset_result: Dict[str, Any]
    ) -> None:
        condition = superset_result["conditions"]["glc"]
        assert condition["status"] == "done"
        # The target is the superset's optimum, which is what was asked for.
        assert condition["biomass_fixed"] == pytest.approx(condition["growth_superset"])
        # The gap-fill is the transporter that was knocked out.
        assert "GLCpts_c0" in condition["gapfilled"]
        assert condition["distance_to_growth"] >= 1
        assert superset_result["annotation_evidence"]

    def test_biomass_reference_used_says_superset(
        self, superset_result: Dict[str, Any]
    ) -> None:
        assert (
            superset_result["conditions"]["glc"]["biomass_reference_used"] == "superset"
        )


# ---------------------------------------------------------------------------
# biomass_reference="model" on a model that DOES grow: nothing moves
# ---------------------------------------------------------------------------


class TestGrowingModelIsUntouched:
    def test_biomass_reference_used_says_model(
        self, growing_result: Dict[str, Any]
    ) -> None:
        condition = growing_result["conditions"]["glc"]
        assert condition["status"] == "done"
        assert condition["biomass_reference_used"] == "model"
        # The target is the MODEL's own optimum, not the superset's.
        assert condition["biomass_fixed"] == pytest.approx(condition["growth_model"])

    def test_every_other_field_matches_the_base_commit(
        self, growing_result: Dict[str, Any]
    ) -> None:
        """Byte-for-byte against ``65db6ea``, bar timing and the new key.

        The golden is the base commit's own output for this exact call, so this
        is the assertion that the fix changed NOTHING for a model that grows --
        which no amount of checking the new key can establish.
        """
        with GOLDEN.open() as handle:
            expected = json.load(handle)
        assert "biomass_reference_used" not in expected, (
            "the golden must be the BASE commit's output, which has no such key"
        )
        actual = _without_timing(growing_result["conditions"]["glc"])
        assert actual.pop("biomass_reference_used") == "model"
        _assert_close(actual, expected, path="condition")


# ---------------------------------------------------------------------------
# The Complete leg, whose branch continues early
# ---------------------------------------------------------------------------


class TestCompleteLegOnADeadModel:
    """``require_fitness=False`` does not exempt a leg from the relabel.

    The Complete-leg branch in the post-processing loop ``continue``s, so a
    relabel written after it would silently skip exactly the legs KBDL runs on
    every panel.
    """

    def test_the_complete_leg_is_relabelled_too(
        self, complete_leg_result: Dict[str, Any]
    ) -> None:
        condition = complete_leg_result["conditions"]["complete"]
        assert condition["status"] == "no_growth"
        assert condition["biomass_reference_used"] is None
        assert "biomass_fixed" not in condition
        assert "fluxes" not in condition

    def test_it_still_ends_with_a_zero_count_and_empty_evidence(
        self, complete_leg_result: Dict[str, Any]
    ) -> None:
        """Item 4 of the change: the branch's own guarantees still hold."""
        condition = complete_leg_result["conditions"]["complete"]
        assert condition["n_reactions_discounted"] == 0
        assert condition["edge_evidence"] == []
        assert complete_leg_result["annotation_evidence"] == []


# ---------------------------------------------------------------------------
# The boundary, on a stubbed run result
# ---------------------------------------------------------------------------


class TestTheMinGrowthBoundary:
    """``>= min_growth`` in the class, so ``< min_growth`` here.

    Stubbed rather than solved: a real model growing at EXACTLY ``min_growth``
    cannot be built to order, and the question is purely which side of the
    comparison the wrapper lands on.  ``_target_was_the_superset`` is the branch
    under test and it is given the class's result verbatim.
    """

    @staticmethod
    def _condition(growth_model: Optional[float]) -> Dict[str, Any]:
        return {
            "condition": "glc",
            "status": "done",
            "growth_superset": 1.0,
            "growth_model": growth_model,
            "biomass_fixed": 1.0,
        }

    def test_exactly_min_growth_is_not_relabelled(self) -> None:
        assert not fitness._target_was_the_superset(
            condition=self._condition(0.01), min_growth=0.01
        )

    def test_just_below_min_growth_is_relabelled(self) -> None:
        assert fitness._target_was_the_superset(
            condition=self._condition(0.01 - 1e-12), min_growth=0.01
        )

    def test_zero_is_relabelled(self) -> None:
        """The real Keio case: ``growth_model`` 0.0, ``biomass_fixed`` 10.71."""
        assert fitness._target_was_the_superset(
            condition=self._condition(0.0), min_growth=0.01
        )

    def test_none_is_relabelled(self) -> None:
        assert fitness._target_was_the_superset(
            condition=self._condition(None), min_growth=0.01
        )

    def test_a_condition_with_no_growth_model_at_all_is_left_alone(self) -> None:
        """The class's OWN ``no_growth``/``no_fitness``: it returned before the
        model solve, so there is no target and nothing to relabel."""
        assert not fitness._target_was_the_superset(
            condition={
                "condition": "glc",
                "status": "no_growth",
                "growth_superset": 0.0,
            },
            min_growth=0.01,
        )


class TestTheRelabelItself:
    """:func:`_relabel_no_growth` keeps the allow-list and nothing else."""

    def test_it_keeps_the_allow_list_and_every_time_key(self) -> None:
        condition: Dict[str, Any] = {
            "condition": "glc",
            "status": "done",
            "growth_superset": 1.0,
            "growth_model": 0.0,
            "time_s": 1.5,
            "time_solve_s": 0.5,
            "time_fva_s": 0.25,
            # A key no modelseedpy has today: an allow-list drops it, a
            # deny-list would hand it to a consumer.
            "some_future_key": "leaked",
        }
        for key in REMOVED_KEYS:
            condition[key] = "from the superset solve"
        fitness._relabel_no_growth(condition)
        assert condition == {
            "condition": "glc",
            "status": "no_growth",
            "growth_superset": 1.0,
            "growth_model": 0.0,
            "time_s": 1.5,
            "time_solve_s": 0.5,
            "time_fva_s": 0.25,
        }

    def test_a_solver_failed_condition_is_relabelled_too(self) -> None:
        """Whatever status the class reported -- ``solver_failed`` included.

        A ``solver_failed`` condition still carries ``biomass_fixed``, so it
        still records a target the model could not reach.
        """
        condition = {
            "condition": "glc",
            "status": "solver_failed",
            "growth_superset": 1.0,
            "growth_model": None,
            "biomass_fixed": 1.0,
            "time_s": 1.0,
        }
        assert fitness._target_was_the_superset(condition=condition, min_growth=0.01)
        fitness._relabel_no_growth(condition)
        assert condition["status"] == "no_growth"
        assert "biomass_fixed" not in condition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _min_growth() -> float:
    """modelseedpy's ``min_growth`` default, read from it rather than typed here.

    Returns:
        ``modelseedpy.core.msfitnessweighted.MIN_GROWTH`` (0.01 at ``edafdb1``),
        which is the bar ``analyze_condition`` compares ``opt_model`` against.
    """
    from modelseedpy.core.msfitnessweighted import MIN_GROWTH

    return float(MIN_GROWTH)


def _assert_close(actual: Any, expected: Any, *, path: str) -> None:
    """Recursive equality with a float tolerance, reporting WHERE it differs.

    Args:
        actual: The branch's value.
        expected: The golden's value.
        path: Dotted location, for the failure message.

    Raises:
        AssertionError: Naming the path that differs.

    A plain ``==`` on nested solver output reports only "dicts differ", which on
    a result carrying ``fluxes`` and ``reaction_calls`` is unreadable.
    """
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected a dict, got {type(actual)}"
        assert set(actual) == set(expected), (
            f"{path}: keys differ — only in branch {sorted(set(actual) - set(expected))}, "
            f"only in base {sorted(set(expected) - set(actual))}"
        )
        for key in expected:
            _assert_close(actual[key], expected[key], path=f"{path}.{key}")
        return
    if isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected a list, got {type(actual)}"
        assert len(actual) == len(expected), (
            f"{path}: {len(actual)} entries, base had {len(expected)}"
        )
        for index, item in enumerate(expected):
            _assert_close(actual[index], item, path=f"{path}[{index}]")
        return
    if isinstance(expected, bool) or expected is None:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"
        return
    if isinstance(expected, (int, float)):
        assert actual == pytest.approx(expected, rel=GOLDEN_REL_TOL, abs=1e-9), (
            f"{path}: {actual!r} != {expected!r}"
        )
        return
    assert actual == expected, f"{path}: {actual!r} != {expected!r}"


# ---------------------------------------------------------------------------
# Fixtures (module-scoped copies of the sibling module's, which are its own)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template() -> Any:
    return _template()


@pytest.fixture(scope="module")
def dead_model_result(template: Any, glucose: Any) -> Dict[str, Any]:
    """The defect's configuration: cannot grow, ``biomass_reference="model"``."""
    return _run(_model(ko=list(NO_GROWTH_KO), extra=_duplicate_pgi), template, glucose)


@pytest.fixture(scope="module")
def superset_result(template: Any, glucose: Any) -> Dict[str, Any]:
    """The same dead toy, asked for the superset-referenced answer explicitly."""
    return _run(
        _model(ko=list(NO_GROWTH_KO), extra=_duplicate_pgi),
        template,
        glucose,
        biomass_reference="superset",
    )


@pytest.fixture(scope="module")
def growing_result(template: Any, glucose: Any) -> Dict[str, Any]:
    """A toy that DOES grow, ``biomass_reference="model"``: the untouched path."""
    return _run(_model(extra=_duplicate_pgi), template, glucose)


@pytest.fixture(scope="module")
def complete_leg_result(template: Any, glucose: Any) -> Dict[str, Any]:
    """``require_fitness=False`` on the dead toy: the early-``continue`` branch."""
    return fitness.run_fitness_weighted_analysis(
        model=_model(ko=list(NO_GROWTH_KO)),
        template=template,
        biomass=BIOMASS,
        loopless=False,
        conditions={
            "complete": {
                "media": glucose,
                "fitness": {},
                "require_fitness": False,
            }
        },
    )


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
