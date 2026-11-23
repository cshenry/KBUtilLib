"""An added exchange is not a gap-fill (conduct row 2075).

Chris, 2026-11-23 11:57 CST, answering *"Exclude exchange reactions from
distance_to_growth on Complete?"*: **"Yes, exclude them"**.

``MSFitnessWeightedAnalysis._classify_superset`` (modelseedpy ``edafdb1``) puts
every superset reaction the draft model lacks in ``candidates`` -- boundary
reactions INCLUDED -- while ``priced`` leaves boundary reactions out::

    self.candidates = {r.id for r in sup.reactions
                       if r.id not in self.draft_reactions}
    ...
    self.priced = [r.id for r in sup.reactions
                   if not r.boundary and r.id != self.biomass]

and ``analyze_condition`` then counts every candidate carrying flux as a
gap-fill::

    res["distance_to_growth"] = len(gapfilled) + (
        len(reversed_used) if P["price_reversals"] else 0)

So an exchange the superset added is FREE -- the class never prices one, in any
condition -- and was still reported as missing biology.  Measured on real Keio
(KBDL job 2576dc35, Complete medium): 18 gap-fill entries, several of them
``EX_`` reactions, none of which the model has.  An exchange is not missing
biology: on Complete every exchange allows uptake (Chris, 2026-11-21: "Complete
generally means ALL exchange fluxes allow uptake.  ALWAYS").

**The toy that reproduces it.**  ``test_ms_fitness_utils``' toy cannot reproduce
it as it stands, for a reason worth recording: the committed core BiGG template
fixture declares every one of its 72 ``compcompounds`` with
``templatecompartment_ref`` ``"~/compartments/id/"`` -- an EMPTY compartment id
-- so ``GapfillingPkg.extend_model_with_template_metabolites``' ``if compartment
== "e"`` never fires and no exchange is ever created for a new extracellular
metabolite.  :func:`_template_with_extracellular_compartment` points the ``_e``
compcompounds at compartment ``e``, which is what a real ModelSEED template
does, and then knocking the glucose route *and its extracellular metabolite*
out of the model makes the superset re-add ``GLCpts_c0`` **and** create
``EX_glc__D_e0`` for it.  Measured on base ``e50fdb4``: ``gapfilled``
``{'EX_glc__D_e0': '<', 'GLCpts_c0': '>'}``, ``distance_to_growth`` 2 -- the
real Keio shape in miniature.  On this branch: ``gapfilled``
``{'GLCpts_c0': '>'}``, ``exchanges_added`` ``{'EX_glc__D_e0': '<'}``,
``distance_to_growth`` 1.

The medium here is keyed by the toy's own cobra metabolite ids
(``glc__D_e0``) rather than by bare BiGG ids (``glc__D``).  That is not
cosmetic: ``KBaseMediaPkg`` resolves a media compound through
``MSModelUtil.find_met``, which indexes ``met.id``, ``met.name`` and
``met.annotation``, and the metabolite the TEMPLATE contributes carries no
annotation -- so a bare ``glc__D`` key finds the model's own metabolite but not
a template-contributed one, and the added exchange would stay shut.

Like its sibling modules these run against the ACTUALLY INSTALLED ModelSEEDpy,
because every claim here is a claim about what the class does; the fixtures are
``test_ms_fitness_utils``' own, imported rather than copied so the suites cannot
drift onto different toys.

Running them::

    TMPDIR=/scratch/chenry/tmp KBU_REQUIRE_MODEL_STACK=1 \\
    python -m pytest tests/modeling/test_ms_fitness_exchanges.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Set

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
from tests.modeling.test_ms_fitness_no_growth import _assert_close  # noqa: E402
from tests.modeling.test_ms_fitness_utils import (  # noqa: E402
    BIOMASS,
    _duplicate_pgi,
    _model,
    _template_dict,
)

#: The glucose route knocked out of the toy.  ``GLCpts_c0`` is
#: ``e_coli_core``'s only glucose uptake reaction and ``EX_glc__D_e0`` its only
#: glucose exchange; the core BiGG template can put ``GLCpts_c0`` back, and
#: ``GapfillingPkg`` then has to create an exchange for the extracellular
#: metabolite that reaction reintroduces.
GLUCOSE_ROUTE_KO = ("EX_glc__D_e0", "GLCpts_c0")

#: The extracellular metabolite that must also go.  ``_model`` keeps every
#: metabolite in the fixture JSON whether a surviving reaction uses it or not,
#: so dropping the two reactions above leaves ``glc__D_e0`` behind as an orphan
#: -- and an extracellular metabolite the model ALREADY has is not in
#: ``extend_model_with_template_metabolites``' ``new_exchange`` list, so no
#: exchange would be added.  Removing it is what makes the toy reproduce the
#: defect at all.
ORPHAN_METABOLITE = "glc__D_e0"

#: The exchange the superset adds, and the transporter alongside it.  Both carry
#: flux in the ``glc`` condition below; only the second is a gap-fill.
ADDED_EXCHANGE = "EX_glc__D_e0"
ADDED_TRANSPORTER = "GLCpts_c0"

#: The condition's fitness.  A real row, so a condition that reports a gap-fill
#: is one that really was priced.
FIT: Dict[str, Any] = {"b4025": (-3.0, -10.0)}

#: The ``fru`` condition as the BASE commit ``e50fdb4`` reported it, timing
#: stripped.  Captured by replaying this module's panel against that commit; see
#: ``agent-io/work-records/kbu-fitness-exchanges-not-gapfills.md``.
GOLDEN = (
    Path(__file__).parent.parent
    / "fixtures"
    / "model"
    / "fitness_no_added_exchange_base.json"
)

#: The two keys this change adds to every solved condition.
NEW_KEYS = ("exchanges_added", "n_exchanges_added")


# ---------------------------------------------------------------------------
# The toy
# ---------------------------------------------------------------------------


def _template_with_extracellular_compartment() -> Any:
    """The core BiGG template fixture, with its ``_e`` compcompounds in ``e``.

    Returns:
        The built ``MSTemplate``.

    The committed fixture declares every compcompound's
    ``templatecompartment_ref`` as ``"~/compartments/id/"``, so
    ``MSTemplateSpecies.from_dict`` reads the compartment as the empty string
    and ``GapfillingPkg`` never recognises one as extracellular.  A real
    ModelSEED template names the compartment, which is what this restores --
    for the ``_e`` compcompounds only, so nothing else about the toy moves.
    ``to_metabolite`` produces the same metabolite id either way
    (``glc__D_e`` + index ``0``), so no reaction or id in the superset changes;
    the only difference is that the extracellular metabolites now reach
    ``new_exchange`` and get an exchange reaction built for them.
    """
    from modelseedpy.core.mstemplate import MSTemplateBuilder

    template_dict = _template_dict()
    for compcompound in template_dict["compcompounds"]:
        if compcompound["id"].endswith("_e"):
            compcompound["templatecompartment_ref"] = "~/compartments/id/e"
    return MSTemplateBuilder.from_dict(template_dict).build()


def _toy_model() -> Any:
    """The toy with the glucose route and its extracellular metabolite gone."""

    def extra(model: Any) -> None:
        _duplicate_pgi(model)
        model.remove_metabolites([model.metabolites.get_by_id(ORPHAN_METABOLITE)])

    return _model(ko=list(GLUCOSE_ROUTE_KO), extra=extra)


def _medium(**carbon: Any) -> Any:
    """A minimal aerobic medium plus the named carbon sources.

    Args:
        **carbon: ``{metabolite_id: (lower, upper)}`` additions.

    Returns:
        The ``MSMedia``.

    Keyed by cobra metabolite id, not bare BiGG id: see the module docstring.
    """
    from modelseedpy import MSMedia

    bounds = {
        "o2_e0": (-1000, 1000),
        "h_e0": (-1000, 1000),
        "h2o_e0": (-1000, 1000),
        "pi_e0": (-1000, 1000),
        "co2_e0": (-1000, 1000),
        "nh4_e0": (-1000, 1000),
    }
    bounds.update(carbon)
    return MSMedia.from_dict(bounds)


def _panel_conditions() -> Dict[str, Dict[str, Any]]:
    """The three-condition panel, defined once so base and branch run the same.

    Returns:
        ``{condition_id: spec}``.

    * ``glc`` -- only glucose, which the model cannot reach at all, so the
      gap-fill has to add ``GLCpts_c0`` AND the exchange for it.
    * ``fru`` -- only fructose, which the model transports itself
      (``FRUpts2_c0``), so nothing is added and this condition is the control.
    * ``complete`` -- every carbon source at once and ``require_fitness=False``,
      the Complete leg whose branch in the post-processing loop ``continue``s.
    """
    return {
        "glc": {"media": _medium(glc__D_e0=(-10, 1000)), "fitness": FIT},
        "fru": {"media": _medium(fru_e0=(-10, 1000)), "fitness": FIT},
        "complete": {
            "media": _medium(
                glc__D_e0=(-10, 1000), fru_e0=(-10, 1000), ac_e0=(-20, 1000)
            ),
            "fitness": {},
            "require_fitness": False,
        },
    }


# ---------------------------------------------------------------------------
# The premise: the class really does offer an unpriced exchange as a candidate
# ---------------------------------------------------------------------------


class TestTheSupersetOffersAnUnpricedExchange:
    """Without this the rest of the module would be testing nothing."""

    def test_the_exchange_is_a_candidate_the_model_does_not_have(
        self, analysis: Any
    ) -> None:
        assert ADDED_EXCHANGE in analysis.candidates
        assert ADDED_EXCHANGE not in analysis.draft_reactions

    def test_the_exchange_is_a_boundary_reaction(self, analysis: Any) -> None:
        assert analysis.superset.reactions.get_by_id(ADDED_EXCHANGE).boundary

    def test_the_class_never_prices_it(self, analysis: Any, boundary: Set[str]) -> None:
        """``priced`` is ``not r.boundary``, so no exchange is ever in it."""
        assert ADDED_EXCHANGE not in analysis.priced
        assert boundary & set(analysis.priced) == set()

    def test_the_boundary_set_is_the_supersets_own_boundary_reactions(
        self, analysis: Any, boundary: Set[str]
    ) -> None:
        """The shipped helper against the contract, stated independently."""
        assert boundary, "the toy must have boundary reactions at all"
        assert fitness._boundary_reaction_ids(analysis) == boundary

    def test_the_transporter_is_a_candidate_and_is_priced(self, analysis: Any) -> None:
        """The other half of the gap-fill, which must NOT move."""
        assert ADDED_TRANSPORTER in analysis.candidates
        assert ADDED_TRANSPORTER in analysis.priced


# ---------------------------------------------------------------------------
# The fix, end to end
# ---------------------------------------------------------------------------


class TestAnAddedExchangeIsNotAGapFill:
    """The ``glc`` condition: this class fails on base commit ``e50fdb4``.

    There, ``gapfilled`` is ``{'EX_glc__D_e0': '<', 'GLCpts_c0': '>'}``,
    ``distance_to_growth`` is 2 and neither new key exists at all.
    """

    def test_the_exchange_is_reported_as_an_exchange(
        self, panel_result: Dict[str, Any]
    ) -> None:
        condition = panel_result["conditions"]["glc"]
        assert condition["status"] == "done"
        assert condition["exchanges_added"] == {ADDED_EXCHANGE: "<"}

    def test_the_exchange_is_not_a_gap_fill(self, panel_result: Dict[str, Any]) -> None:
        condition = panel_result["conditions"]["glc"]
        assert ADDED_EXCHANGE not in condition["gapfilled"]
        # The transporter alongside it still is one: this change narrows the
        # gap-fill, it does not empty it.
        assert condition["gapfilled"] == {ADDED_TRANSPORTER: ">"}

    def test_n_exchanges_added_counts_them(self, panel_result: Dict[str, Any]) -> None:
        condition = panel_result["conditions"]["glc"]
        assert condition["n_exchanges_added"] == 1
        assert condition["n_exchanges_added"] == len(condition["exchanges_added"])

    def test_distance_to_growth_is_recomputed(
        self, panel_result: Dict[str, Any]
    ) -> None:
        """2 on base, 1 here -- and derived exactly as the class derives it."""
        condition = panel_result["conditions"]["glc"]
        assert condition["distance_to_growth"] == 1
        assert condition["distance_to_growth"] == len(condition["gapfilled"]) + len(
            condition["reversals_used"]
        )

    def test_annotation_gap_is_recomputed(self, panel_result: Dict[str, Any]) -> None:
        condition = panel_result["conditions"]["glc"]
        assert condition["annotation_gap"] is (condition["distance_to_growth"] > 0)
        # Still True here: the transporter is a real gap.
        assert condition["annotation_gap"] is True

    def test_the_exchange_really_did_carry_flux(
        self, panel_result: Dict[str, Any]
    ) -> None:
        """Nothing is re-solved: the solution is reported, not recomputed.

        The exchange is still in ``fluxes`` with the direction
        ``exchanges_added`` gives it, which is how a reader can see that the
        solve really used it -- moving the entry is a relabelling, not a
        retraction.
        """
        condition = panel_result["conditions"]["glc"]
        assert condition["fluxes"][ADDED_EXCHANGE] < 0.0
        assert condition["exchanges_added"][ADDED_EXCHANGE] == "<"

    def test_annotation_evidence_holds_no_exchange(
        self, panel_result: Dict[str, Any], boundary: Set[str]
    ) -> None:
        """Nor could it: see :class:`TestTheGapfilledConsumers`."""
        offenders = [
            row
            for row in panel_result["annotation_evidence"]
            if row["reaction"] in boundary
        ]
        assert offenders == [], f"exchange rows in the rollup: {offenders}"
        assert panel_result["annotation_evidence"], "the toy must produce evidence"

    def test_no_condition_s_edge_evidence_holds_an_exchange(
        self, panel_result: Dict[str, Any], boundary: Set[str]
    ) -> None:
        for condition_id, condition in panel_result["conditions"].items():
            offenders = [
                row
                for row in condition.get("edge_evidence", [])
                if row["reaction"] in boundary
            ]
            assert offenders == [], f"{condition_id}: {offenders}"


class TestEveryConditionAndNotOnlyComplete:
    """Item 3: the class leaves exchanges unpriced in EVERY condition.

    So an added exchange is a gap-fill nowhere, and the split runs on every
    condition.  Measured on this panel: of the two non-Complete conditions,
    ``glc`` changes and ``fru`` does not -- the rule is not "Complete only" and
    it is not "every condition unconditionally" either, it is "wherever the
    superset's exchange carried flux".
    """

    def test_the_complete_leg_is_split_too(self, panel_result: Dict[str, Any]) -> None:
        """Its branch in the post-processing loop ``continue``s, so a split
        written after it would skip exactly the leg KBDL runs every panel on."""
        condition = panel_result["conditions"]["complete"]
        assert condition["status"] == "done"
        assert condition["exchanges_added"] == {ADDED_EXCHANGE: "<"}
        assert condition["gapfilled"] == {ADDED_TRANSPORTER: ">"}
        assert condition["distance_to_growth"] == 1

    def test_a_non_complete_condition_is_split_too(
        self, panel_result: Dict[str, Any]
    ) -> None:
        assert panel_result["conditions"]["glc"]["n_exchanges_added"] == 1

    def test_how_many_non_complete_conditions_this_changes(
        self, panel_result: Dict[str, Any]
    ) -> None:
        """The measurement item 3 asks for, pinned so it cannot drift."""
        changed = {
            condition_id
            for condition_id, condition in panel_result["conditions"].items()
            if condition_id != "complete" and condition.get("n_exchanges_added", 0) > 0
        }
        assert changed == {"glc"}


class TestAConditionWithNoAddedExchange:
    """The ``fru`` control: the model transports fructose itself."""

    def test_nothing_was_added_and_nothing_was_gap_filled(
        self, panel_result: Dict[str, Any]
    ) -> None:
        condition = panel_result["conditions"]["fru"]
        assert condition["status"] == "done"
        assert condition["exchanges_added"] == {}
        assert condition["n_exchanges_added"] == 0

    def test_every_other_field_matches_the_base_commit(
        self, panel_result: Dict[str, Any]
    ) -> None:
        """Byte-for-byte against ``e50fdb4``, bar timing and the two new keys.

        The golden is the base commit's own output for this exact panel, so this
        is the assertion that the split changed NOTHING where no exchange was
        added -- which no amount of checking the new keys can establish.

        The two new keys are popped rather than compared because the golden
        predates them; they are asserted empty above.  Everything else,
        ``gapfilled`` and ``distance_to_growth`` and ``annotation_gap``
        included, is compared.
        """
        with GOLDEN.open() as handle:
            expected = json.load(handle)
        for key in NEW_KEYS:
            assert key not in expected, (
                f"the golden must be the BASE commit's output, which has no {key}"
            )
        actual = _without_timing(panel_result["conditions"]["fru"])
        for key in NEW_KEYS:
            actual.pop(key)
        _assert_close(actual, expected, path="condition")


class TestANoGrowthConditionIsUntouched:
    """Item 5: the Finding A relabel leaves no ``gapfilled`` key to split.

    The relabel runs FIRST in the post-processing loop and strips every key
    describing the solve, so the split must find nothing and must not put
    anything back -- a relabelled condition that grew an ``exchanges_added``
    key would be reporting part of a solve the relabel just discarded.
    """

    def test_the_condition_is_still_relabelled(
        self, dead_model_result: Dict[str, Any]
    ) -> None:
        assert dead_model_result["conditions"]["glc"]["status"] == "no_growth"

    def test_it_gained_neither_new_key(self, dead_model_result: Dict[str, Any]) -> None:
        condition = dead_model_result["conditions"]["glc"]
        for key in ("gapfilled", "distance_to_growth", "annotation_gap", *NEW_KEYS):
            assert key not in condition, f"{key} leaked into a relabelled condition"

    def test_the_allow_list_still_describes_it_exactly(
        self, dead_model_result: Dict[str, Any]
    ) -> None:
        """The same pin ``test_ms_fitness_no_growth`` applies, re-asserted here
        because this change adds keys in the same loop."""
        condition = dead_model_result["conditions"]["glc"]
        survivors = {
            key for key in condition if not key.startswith(fitness.TIMING_KEY_PREFIX)
        }
        assert survivors == set(fitness.NO_GROWTH_SURVIVING_KEYS) | {
            "n_reactions_discounted",
            "biomass_reference_used",
        }


# ---------------------------------------------------------------------------
# Every consumer of `gapfilled` in this module
# ---------------------------------------------------------------------------


class TestTheGapfilledConsumers:
    """Item 4: what else in this module reads a gap-fill, and what moved.

    Nothing in ``ms_fitness_utils`` reads ``gapfilled`` computationally -- the
    split is the only code that touches it -- and the two counts a reader might
    expect to be derived from it are not:

    * ``n_reactions_discounted`` (:func:`_count_discounted`) counts MODEL
      reactions (``analysis.draft_reactions``) and explicitly excludes
      candidates, so a superset-added exchange could never be in it.
    * ``edge_evidence`` and the ``annotation_evidence`` rollup are keyed by
      ``analysis.edges``, and :func:`normalize_gene_reactions` resolves a gene
      mapping's keys ONLY onto model reaction ids -- a key naming a
      superset-added reaction is counted as unmapped and dropped.  So an added
      exchange cannot reach the evidence by any route, with or without a
      caller-supplied mapping.

    These tests are the evidence for those two claims, not a change of
    behaviour: both held on base and hold here.
    """

    def test_a_mapping_key_naming_an_added_exchange_is_dropped(self) -> None:
        """Which is why no added exchange can ever become evidence."""
        model = _toy_model()
        merged, unmapped = fitness.normalize_gene_reactions(
            gene_reactions={ADDED_EXCHANGE: ["gX"], "PGI_c0": ["b4025"]},
            model_reaction_ids=[reaction.id for reaction in model.reactions],
        )
        assert ADDED_EXCHANGE not in merged
        assert unmapped == [ADDED_EXCHANGE]
        assert merged == {"PGI_c0": {"b4025": ["model"]}}

    def test_the_discount_count_only_ever_sees_model_reactions(
        self, analysis: Any, boundary: Set[str]
    ) -> None:
        """``_count_discounted`` skips anything not in ``draft_reactions``."""
        assert boundary & set(analysis.candidates) == {ADDED_EXCHANGE}
        assert (boundary & set(analysis.candidates)) & set(
            analysis.draft_reactions
        ) == set()

    def test_the_discount_count_is_what_it_was(
        self, panel_result: Dict[str, Any]
    ) -> None:
        """It is a pricing count, not a flux count, so the split cannot move it.

        One discounted reaction: ``PGI_c0``, the model reaction ``b4025``'s
        fitness defect re-prices.  The golden comparison above pins the ``fru``
        condition's value against base as well.
        """
        assert panel_result["conditions"]["glc"]["n_reactions_discounted"] == 1


# ---------------------------------------------------------------------------
# The split itself, on stubbed conditions
# ---------------------------------------------------------------------------


class TestTheSplitItself:
    """:func:`_split_exchanges_added` in isolation.

    Stubbed rather than solved: the cases below -- ``price_reversals=False``, a
    gap-fill that is nothing BUT exchanges, a boundary reaction whose id does
    not start with ``EX_`` -- are about which arithmetic and which test the
    function performs, and a real model cannot be built to order for each.
    """

    @staticmethod
    def _condition(**overrides: Any) -> Dict[str, Any]:
        condition = {
            "condition": "glc",
            "status": "done",
            "gapfilled": {"EX_glc__D_e0": "<", "GLCpts_c0": ">"},
            "reversals_used": {"PGI_c0": "<"},
            "distance_to_growth": 3,
            "annotation_gap": True,
        }
        condition.update(overrides)
        return condition

    def test_it_splits_and_recounts_with_reversals_priced(self) -> None:
        condition = self._condition()
        fitness._split_exchanges_added(
            condition=condition,
            boundary={"EX_glc__D_e0"},
            price_reversals=True,
        )
        assert condition["gapfilled"] == {"GLCpts_c0": ">"}
        assert condition["exchanges_added"] == {"EX_glc__D_e0": "<"}
        assert condition["n_exchanges_added"] == 1
        # 1 gap-fill + 1 priced reversal.
        assert condition["distance_to_growth"] == 2
        assert condition["annotation_gap"] is True

    def test_reversals_are_left_out_when_the_class_does_not_price_them(
        self,
    ) -> None:
        condition = self._condition()
        fitness._split_exchanges_added(
            condition=condition,
            boundary={"EX_glc__D_e0"},
            price_reversals=False,
        )
        assert condition["distance_to_growth"] == 1
        # The reversal itself is untouched: it is a direction of a reaction the
        # model already has, so it is never a boundary candidate.
        assert condition["reversals_used"] == {"PGI_c0": "<"}

    def test_annotation_gap_goes_false_when_only_exchanges_were_added(self) -> None:
        """The case the defect was hiding: a condition with NO annotation gap
        reported as having one."""
        condition = self._condition(
            gapfilled={"EX_glc__D_e0": "<", "EX_ac_e0": ">"}, reversals_used={}
        )
        fitness._split_exchanges_added(
            condition=condition,
            boundary={"EX_glc__D_e0", "EX_ac_e0"},
            price_reversals=True,
        )
        assert condition["gapfilled"] == {}
        assert condition["n_exchanges_added"] == 2
        assert condition["distance_to_growth"] == 0
        assert condition["annotation_gap"] is False

    def test_the_test_is_boundary_membership_and_not_the_id_prefix(self) -> None:
        """``cobra.Reaction.boundary``, the class's own ``priced`` test.

        An id prefix is a naming convention of whatever built the model: a
        boundary reaction named ``DM_cpd11416_c0`` must move, and a reaction
        named ``EX_something`` that the superset does NOT consider boundary must
        not.  Both directions are asserted, because a prefix check would get
        both wrong.
        """
        condition = self._condition(
            gapfilled={"DM_cpd11416_c0": ">", "EXTRA_c0": ">"}, reversals_used={}
        )
        fitness._split_exchanges_added(
            condition=condition,
            boundary={"DM_cpd11416_c0"},
            price_reversals=True,
        )
        assert condition["exchanges_added"] == {"DM_cpd11416_c0": ">"}
        assert condition["gapfilled"] == {"EXTRA_c0": ">"}

    def test_an_empty_gapfilled_still_gets_both_keys(self) -> None:
        """Present on every solved condition, so a caller never has to ask."""
        condition = self._condition(gapfilled={}, reversals_used={})
        fitness._split_exchanges_added(
            condition=condition, boundary={"EX_glc__D_e0"}, price_reversals=True
        )
        assert condition["exchanges_added"] == {}
        assert condition["n_exchanges_added"] == 0
        assert condition["distance_to_growth"] == 0
        assert condition["annotation_gap"] is False

    def test_a_condition_with_no_gapfilled_key_is_left_exactly_alone(self) -> None:
        """A relabelled ``no_growth``, or the class's own ``no_fitness``."""
        condition = {
            "condition": "glc",
            "status": "no_growth",
            "growth_superset": 1.0,
            "growth_model": 0.0,
            "time_s": 1.0,
        }
        before = dict(condition)
        fitness._split_exchanges_added(
            condition=condition, boundary={"EX_glc__D_e0"}, price_reversals=True
        )
        assert condition == before

    def test_a_missing_reversals_used_counts_as_none(self) -> None:
        """Defensive: ``analyze_condition`` always sets it alongside
        ``gapfilled``, so this can only matter if that ever stops being true."""
        condition = self._condition(gapfilled={"GLCpts_c0": ">"})
        del condition["reversals_used"]
        fitness._split_exchanges_added(
            condition=condition, boundary={"EX_glc__D_e0"}, price_reversals=True
        )
        assert condition["distance_to_growth"] == 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def template() -> Any:
    return _template_with_extracellular_compartment()


@pytest.fixture(scope="module")
def analysis(template: Any) -> Any:
    """The class, built straight and its superset built, for premise checks.

    The entry point returns a result dict rather than the object, and these
    assertions are about ``candidates``, ``priced`` and the superset's bounds --
    which only the object carries.
    """
    from modelseedpy.core.msfitnessweighted import MSFitnessWeightedAnalysis

    built = MSFitnessWeightedAnalysis(
        _toy_model(), template, biomass=BIOMASS, atp_safe=False, loopless=False
    )
    built.build_superset()
    return built


@pytest.fixture(scope="module")
def boundary(analysis: Any) -> Set[str]:
    """``cobra.Reaction.boundary`` over the superset, computed here.

    Derived from the superset rather than from
    :func:`~kbutillib.domains.modeling.ms_fitness_utils._boundary_reaction_ids`
    on purpose: the tests that assert this change moved NOTHING (the evidence
    rollup, the discount count) must be runnable on the base commit, where that
    helper does not exist -- a fixture calling it would turn those into errors
    instead of the passes that are the actual evidence.
    """
    return {
        reaction.id for reaction in analysis.superset.reactions if reaction.boundary
    }


@pytest.fixture(scope="module")
def panel_result(template: Any) -> Dict[str, Any]:
    """The three-condition panel, ``biomass_reference="superset"``.

    ``superset`` and not the ``model`` default because this toy's model cannot
    grow on glucose at all -- that is what makes the gap-fill add the exchange
    -- and under ``biomass_reference="model"`` the Finding A relabel would
    discard the whole solve before the split could see it
    (:class:`TestANoGrowthConditionIsUntouched` is that case).
    """
    return fitness.run_fitness_weighted_analysis(
        model=_toy_model(),
        template=template,
        biomass=BIOMASS,
        loopless=False,
        biomass_reference="superset",
        conditions=_panel_conditions(),
    )


@pytest.fixture(scope="module")
def dead_model_result(template: Any) -> Dict[str, Any]:
    """The same toy and medium with the ``biomass_reference="model"`` default."""
    return fitness.run_fitness_weighted_analysis(
        model=_toy_model(),
        template=template,
        biomass=BIOMASS,
        loopless=False,
        conditions={"glc": {"media": _medium(glc__D_e0=(-10, 1000)), "fitness": FIT}},
    )
