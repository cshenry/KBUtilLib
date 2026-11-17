"""Real-dependency tests for the fitness-weighted entry point (PRD kbdl-fitness-off-gaa-v1).

These run against the ACTUALLY INSTALLED ModelSEEDpy, because every behaviour
under test is a claim about what Jose Faria's
``MSFitnessWeightedAnalysis`` does when this wrapper feeds it KBDL's inputs --
which prices move, which rows pass the significance gate, what the ATP filter
leaves behind -- and a fake answers none of those.

The toy is KBDL issue #44's: ModelSEEDpy's own ``e_coli_core`` with ModelSEED
compartment suffixes, plus ``PGI2_c0``, an exact copy of ``PGI_c0`` catalysed by
a made-up gene ``g_pgi2``.  Two identical reactions, one gene each, is the
smallest model in which "the fitness data chose the flux" is distinguishable
from "the solver picked one" -- swap which gene carries the defect and the flux
must swap with it.

Running them::

    TMPDIR=/scratch/chenry/tmp KBU_REQUIRE_MODEL_STACK=1 \\
    /scratch/chenry/venvs/kbdl-fitness-layer-fc5eced5/bin/python -m pytest \\
        tests/modeling/test_ms_fitness_utils.py -v

That venv has modelseedpy installed from
``git+https://github.com/cshenry/ModelSEEDpy.git@d55034cfb1eac8c6bd0f86720f7e8076ff9be2f7``
(the ``model`` extra's pin -- the commit that carries
``modelseedpy/core/msfitnessweighted.py``).  Without the stack the module skips;
with ``KBU_REQUIRE_MODEL_STACK=1`` it FAILS instead, because a silent skip is
how an unmeasured environment gets mistaken for a measured one.

Fixtures: ``tests/fixtures/model/e_coli_core.json.gz`` and
``template_core_bigg.json.gz`` are ModelSEEDpy's own ``tests/test_data`` files at
that commit, gzipped and committed here so these tests need no ModelSEEDpy
source checkout (a pip install from git carries no ``tests/`` directory).
"""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from tests.conftest import require_model_stack

pytestmark = pytest.mark.kbu_model

# Module-level: a module-scope skip happens during collection, too early for
# conftest's pytest_runtest_setup guard to convert it into a failure.
require_model_stack()

import cobra  # noqa: E402

from kbutillib.domains.modeling import ms_fitness_utils as fitness  # noqa: E402

FIXTURES = Path(__file__).parent.parent / "fixtures" / "model"

#: e_coli_core's biomass id AFTER the ModelSEED compartment suffixing below.
#: Not ``bio1`` (the entry point's default, which is the ModelSEED convention)
#: and not the bare ``BIOMASS_Ecoli_core_w_GAM`` either: ``_model`` gives every
#: single-compartment reaction a ``_c0`` suffix, exactly as ModelSEEDpy's own
#: test_msfitnessweighted.py and test_msgapfill.py do, because the gap-fill
#: candidates and the ATP scaffold are named in that vocabulary.
BIOMASS = "BIOMASS_Ecoli_core_w_GAM_c0"

#: ``fitness_penalty(3.0)`` with the shipped defaults: a gene with a fitness
#: defect of -3.0 prices its reaction at ``1 - 0.9 * tanh(3.0 / 2.0)``.
PGI_DISCOUNTED_PRICE = 1.0 - 0.9 * math.tanh(1.5)


# ---------------------------------------------------------------------------
# The toy
# ---------------------------------------------------------------------------


def _template_dict() -> Dict[str, Any]:
    with gzip.open(FIXTURES / "template_core_bigg.json.gz", "rt") as handle:
        return json.load(handle)


def _template() -> Any:
    from modelseedpy.core.mstemplate import MSTemplateBuilder

    return MSTemplateBuilder.from_dict(_template_dict()).build()


def _model(ko: Optional[List[str]] = None, extra: Any = None) -> Any:
    """e_coli_core with ModelSEED-style compartment suffixes.

    Args:
        ko: Reaction ids (post-suffixing) to leave out.
        extra: Callable applied to the finished model.

    Returns:
        The cobra model.

    The suffixing is ModelSEEDpy's own ``test_msfitnessweighted.py`` recipe,
    character for character, so this toy is the same toy the port was measured
    on.
    """
    with gzip.open(FIXTURES / "e_coli_core.json.gz", "rt") as handle:
        model_json = json.load(handle)
    model_json["compartments"] = {
        k + "0": v for (k, v) in model_json["compartments"].items()
    }
    metabolites = {}
    for met in model_json["metabolites"]:
        met["id"] += "0"
        met["compartment"] += "0"
        metabolites[met["id"]] = met
    for rxn in model_json["reactions"]:
        rxn["metabolites"] = {i + "0": v for (i, v) in rxn["metabolites"].items()}
        compartments = {metabolites[k]["compartment"] for k in rxn["metabolites"]}
        if rxn["id"].endswith("_e"):
            rxn["id"] += "0"
        elif len(compartments) == 1:
            rxn["id"] += "_" + list(compartments)[0]
        else:
            rxn["id"] += "_c0"
    model_json["reactions"] = [
        r for r in model_json["reactions"] if r["id"] not in (ko or [])
    ]
    model = cobra.io.from_json(json.dumps(model_json))
    if extra:
        extra(model)
    return model


def _duplicate_pgi(model: Any) -> None:
    """Add ``PGI2_c0``: an exact copy of ``PGI_c0`` catalysed by ``g_pgi2``."""
    pgi = model.reactions.get_by_id("PGI_c0")
    dup = cobra.Reaction(
        "PGI2_c0", lower_bound=pgi.lower_bound, upper_bound=pgi.upper_bound
    )
    dup.add_metabolites(pgi.metabolites)
    model.add_reactions([dup])
    dup.gene_reaction_rule = "g_pgi2"


@pytest.fixture(scope="module")
def template() -> Any:
    return _template()


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


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> List[Any]:
    """Capture the ``MSFitnessWeightedAnalysis`` the entry point builds.

    Args:
        monkeypatch: pytest's patcher.

    Returns:
        The list the constructed objects are appended to.

    The entry point returns a result dict, not the object, so the only way to
    assert against the very object that ran -- its prices, its superset bounds
    at the moment ``run`` returned -- is to record it.  The recorder also
    snapshots the superset's bounds as ``run`` returns, which is precisely the
    boundary the ATP leak diagnostic must not cross.
    """
    from modelseedpy.core import msfitnessweighted as fw

    real = fw.MSFitnessWeightedAnalysis
    built: List[Any] = []

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.bounds_after_run: Optional[Dict[str, Any]] = None
            built.append(self)

        def run(self, *args: Any, **kwargs: Any) -> Any:
            out = super().run(*args, **kwargs)
            self.bounds_after_run = {r.id: r.bounds for r in self.superset.reactions}
            return out

    monkeypatch.setattr(fw, "MSFitnessWeightedAnalysis", Recording)
    return built


def _one_condition(glucose: Any, fit: Any, **spec: Any) -> Dict[str, Any]:
    return {"glc": dict({"media": glucose, "fitness": fit}, **spec)}


# ---------------------------------------------------------------------------
# (1) A fitness defect WITH a t-score reaches the price
# ---------------------------------------------------------------------------


class TestDefectWithTScore:
    """The #44 toy: b4025's defect must re-price PGI_c0 and move the flux."""

    def test_price_discount_and_flux_follow_the_defect(
        self, template: Any, glucose: Any, recorder: List[Any]
    ) -> None:
        result = fitness.run_fitness_weighted_analysis(
            model=_model(extra=_duplicate_pgi),
            template=template,
            biomass=BIOMASS,
            conditions=_one_condition(glucose, {"b4025": (-3.0, -10.0)}),
            loopless=False,
        )
        condition = result["conditions"]["glc"]
        assert condition["status"] == "done"

        analysis = recorder[0]
        penalties = analysis.reaction_penalties({"b4025": (-3.0, -10.0)})
        assert penalties["PGI_c0"][">"] == pytest.approx(PGI_DISCOUNTED_PRICE)
        assert penalties["PGI_c0"][">"] == pytest.approx(0.18537, abs=1e-5)
        assert penalties["PGI2_c0"][">"] == pytest.approx(1.0)

        # Exactly one model reaction was re-priced, and it is the cheap one the
        # solution uses. A zero here is the silent no-op the PRD exists to stop.
        assert condition["n_reactions_discounted"] == 1
        assert abs(condition["fluxes"].get("PGI_c0", 0.0)) > 1e-6
        assert abs(condition["fluxes"].get("PGI2_c0", 0.0)) < 1e-9

    def test_swapping_the_gene_swaps_the_prices(
        self, template: Any, glucose: Any, recorder: List[Any]
    ) -> None:
        result = fitness.run_fitness_weighted_analysis(
            model=_model(extra=_duplicate_pgi),
            template=template,
            biomass=BIOMASS,
            conditions=_one_condition(glucose, {"g_pgi2": (-3.0, -10.0)}),
            loopless=False,
        )
        condition = result["conditions"]["glc"]
        penalties = recorder[0].reaction_penalties({"g_pgi2": (-3.0, -10.0)})
        assert penalties["PGI2_c0"][">"] == pytest.approx(PGI_DISCOUNTED_PRICE)
        assert penalties["PGI_c0"][">"] == pytest.approx(1.0)
        assert condition["n_reactions_discounted"] == 1
        assert abs(condition["fluxes"].get("PGI2_c0", 0.0)) > 1e-6
        assert abs(condition["fluxes"].get("PGI_c0", 0.0)) < 1e-9

    def test_evidence_rows_report_a_real_t_score(
        self, template: Any, glucose: Any
    ) -> None:
        result = fitness.run_fitness_weighted_analysis(
            model=_model(extra=_duplicate_pgi),
            template=template,
            biomass=BIOMASS,
            conditions=_one_condition(glucose, {"b4025": (-3.0, -10.0)}),
            loopless=False,
        )
        rows = {
            (r["gene"], r["reaction"]): r
            for r in result["conditions"]["glc"]["edge_evidence"]
        }
        row = rows[("b4025", "PGI_c0")]
        assert row["significance"] == "t"
        assert row["t_score"] == -10.0
        # EVERY row carries the key, including rows that are in the evidence for
        # another reason (an essential reaction whose gene has no fitness).
        assert all("significance" in r for r in rows.values())
        assert any(r["significance"] is None for r in rows.values())
        assert result["modelseedpy_sha"]
        assert result["solver"]["solver"]


# ---------------------------------------------------------------------------
# (2b) The same rows with NO t-score
# ---------------------------------------------------------------------------


class TestMissingTScorePolicy:
    """KBDL's fitness tables often carry no t-score; the policy decides."""

    def _run(self, template: Any, glucose: Any, *, policy: str, fit: float) -> Any:
        return fitness.run_fitness_weighted_analysis(
            model=_model(extra=_duplicate_pgi),
            template=template,
            biomass=BIOMASS,
            missing_t_policy=policy,
            conditions=_one_condition(glucose, {"b4025": (fit, None)}),
            loopless=False,
        )

    def test_ignore_discounts_nothing(self, template: Any, glucose: Any) -> None:
        result = self._run(template, glucose, policy="ignore", fit=-3.0)
        condition = result["conditions"]["glc"]
        assert condition["status"] == "done"
        assert condition["n_reactions_discounted"] == 0
        rows = [r for r in condition["edge_evidence"] if r["gene"] == "b4025"]
        assert rows and all(r["significance"] is None for r in rows)

    def test_fitness_only_admits_a_strong_phenotype(
        self, template: Any, glucose: Any
    ) -> None:
        result = self._run(template, glucose, policy="fitness_only", fit=-3.0)
        condition = result["conditions"]["glc"]
        assert condition["n_reactions_discounted"] == 1
        rows = [r for r in condition["edge_evidence"] if r["gene"] == "b4025"]
        assert rows
        for row in rows:
            assert row["significance"] == "fitness_only"
            # The synthetic t-score the gate needed is NOT reported.
            assert row["t_score"] is None

    def test_fitness_only_respects_the_threshold(
        self, template: Any, glucose: Any
    ) -> None:
        result = self._run(template, glucose, policy="fitness_only", fit=-1.5)
        condition = result["conditions"]["glc"]
        assert condition["n_reactions_discounted"] == 0
        rows = [r for r in condition["edge_evidence"] if r["gene"] == "b4025"]
        assert rows and all(r["significance"] is None for r in rows)

    def test_an_unknown_policy_is_refused(self, template: Any, glucose: Any) -> None:
        with pytest.raises(ValueError, match="missing_t_policy"):
            fitness.run_fitness_weighted_analysis(
                model=_model(),
                template=template,
                biomass=BIOMASS,
                missing_t_policy="whatever",
                conditions=_one_condition(glucose, {}),
            )


# ---------------------------------------------------------------------------
# (2a) Reaction-id normalisation
# ---------------------------------------------------------------------------


class TestGeneReactionNormalisation:
    """Bare ModelSEED ids and model ids both have to land, and merge."""

    def test_keys_for_one_reaction_union_regardless_of_order(self) -> None:
        model_ids = [r.id for r in _model().reactions]
        forward = {
            "PGI": {"b4025": ["srcA"]},
            "PGI_c0": {"b4025": ["srcB"], "gx": ["srcC"]},
        }
        reverse = {
            "PGI_c0": {"gx": ["srcC"], "b4025": ["srcB"]},
            "PGI": {"b4025": ["srcA"]},
        }
        merged_f, unmapped_f = fitness.normalize_gene_reactions(
            gene_reactions=forward, model_reaction_ids=model_ids
        )
        merged_r, unmapped_r = fitness.normalize_gene_reactions(
            gene_reactions=reverse, model_reaction_ids=model_ids
        )
        assert merged_f == merged_r
        assert unmapped_f == unmapped_r == []
        # One model reaction, both keys' genes, both of b4025's sources, sorted
        # and de-duplicated. Neither key overwrote the other.
        assert list(merged_f) == ["PGI_c0"]
        assert merged_f["PGI_c0"] == {"b4025": ["srcA", "srcB"], "gx": ["srcC"]}

    def test_duplicate_sources_are_deduplicated(self) -> None:
        merged, _ = fitness.normalize_gene_reactions(
            gene_reactions={
                "PGI": {"b4025": ["srcA", "srcA"]},
                "PGI_c0": {"b4025": ["srcA"]},
            },
            model_reaction_ids=[r.id for r in _model().reactions],
        )
        assert merged["PGI_c0"] == {"b4025": ["srcA"]}

    def test_a_bare_id_reaches_every_compartment_reaction(self) -> None:
        model_ids = [r.id for r in _model().reactions]
        merged, unmapped = fitness.normalize_gene_reactions(
            gene_reactions={"EX_glc__D": {"g": ["s"]}, "PGI": {"b4025": ["s"]}},
            model_reaction_ids=model_ids,
        )
        assert unmapped == []
        assert "EX_glc__D_e0" in merged and "PGI_c0" in merged

    def test_an_unknown_key_is_counted_and_does_not_raise(
        self, template: Any, glucose: Any
    ) -> None:
        result = fitness.run_fitness_weighted_analysis(
            model=_model(extra=_duplicate_pgi),
            template=template,
            biomass=BIOMASS,
            gene_reactions={
                "PGI": {"b4025": ["fitness_table"]},
                "rxn99999": {"b9999": ["fitness_table"]},
                "also_not_a_reaction_c0": {"b8888": ["fitness_table"]},
            },
            conditions=_one_condition(glucose, {"b4025": (-3.0, -10.0)}),
            loopless=False,
        )
        summary = result["superset"]
        assert summary["n_unmapped_edge_reactions"] == 2
        assert summary["unmapped_edge_reactions_sample"] == [
            "also_not_a_reaction_c0",
            "rxn99999",
        ]
        # The mapping that DID land is reported, and it is the merged one.
        assert result["edges"] == {"PGI_c0": {"b4025": ["fitness_table"]}}
        # ... and the bare key's gene still reached the price.
        assert result["conditions"]["glc"]["n_reactions_discounted"] == 1


# ---------------------------------------------------------------------------
# (2d) The Complete leg
# ---------------------------------------------------------------------------


class TestCompleteLeg:
    """A condition with no fitness is still solved when the caller says so."""

    def test_require_fitness_false_solves_at_uniform_prices(
        self, template: Any, glucose: Any
    ) -> None:
        result = fitness.run_fitness_weighted_analysis(
            model=_model(),
            template=template,
            biomass=BIOMASS,
            loopless=False,
            conditions={
                "complete": {
                    "media": glucose,
                    "fitness": {},
                    "require_fitness": False,
                },
                "skipped": {"media": glucose, "fitness": {}},
            },
        )
        complete = result["conditions"]["complete"]
        assert complete["status"] == "done"
        assert complete["n_reactions_discounted"] == 0
        assert complete["edge_evidence"] == []
        assert complete["fluxes"][BIOMASS] == pytest.approx(
            complete["biomass_fixed"], rel=1e-6
        )
        # The default is unchanged: no fitness and require_fitness left alone is
        # still reported as skipped rather than silently solved.
        skipped = result["conditions"]["skipped"]
        assert skipped["status"] == "no_fitness"
        assert skipped["n_reactions_discounted"] == 0
        # The rollup is recomputed from the post-processed conditions, so an
        # emptied leg cannot leave rows behind in it.
        assert result["annotation_evidence"] == []


# ---------------------------------------------------------------------------
# (2e) The ATP leak diagnostic
# ---------------------------------------------------------------------------


class TestAtpSafe:
    def test_leak_is_reported_and_the_superset_is_untouched(
        self, template: Any, glucose: Any, recorder: List[Any]
    ) -> None:
        result = fitness.run_fitness_weighted_analysis(
            model=_model(),
            template=template,
            core_template=_template(),
            atp_safe=True,
            biomass=BIOMASS,
            loopless=False,
            conditions=_one_condition(glucose, {"b4025": (-3.0, -10.0)}),
        )
        summary = result["superset"]
        assert "atp_leak_flux" in summary and "atp_leak_status" in summary
        assert summary["atp_leak_flux"] is None or isinstance(
            summary["atp_leak_flux"], float
        )
        assert summary["atp_leak_status"]
        assert result["conditions"]["glc"]["status"] == "done"

        analysis = recorder[0]
        after = {r.id: r.bounds for r in analysis.superset.reactions}
        assert analysis.bounds_after_run == after

    def test_atp_safe_without_a_core_template_is_refused(
        self, template: Any, glucose: Any
    ) -> None:
        with pytest.raises(ValueError, match="core_template"):
            fitness.run_fitness_weighted_analysis(
                model=_model(),
                template=template,
                atp_safe=True,
                biomass=BIOMASS,
                conditions=_one_condition(glucose, {}),
            )


# ---------------------------------------------------------------------------
# Candidate pricing: measured, not assumed
# ---------------------------------------------------------------------------


class TestCandidatePricing:
    """The gap-fill tier, which the complete toy never reaches.

    Complete ``e_coli_core`` against ``template_core_bigg`` leaves the template
    with ZERO candidates, so a parity measurement taken there says nothing about
    candidate pricing.  Dropping the glucose transporter forces the tier open.
    The scale is Jose's ``candidate_scale * candidate_factor`` = 10 * 2 = 20,
    which is TWICE GAA's (GAA used factor 1.0); a caller wanting GAA-identical
    candidate prices passes ``candidate_factor=1.0`` through ``method_params``,
    and this records that the knob is reachable and does what it says.
    """

    def test_candidate_price_scale_and_the_factor_knob(
        self, template: Any, glucose: Any, recorder: List[Any]
    ) -> None:
        conditions = _one_condition(glucose, {"b4025": (-3.0, -10.0)})
        fitness.run_fitness_weighted_analysis(
            model=_model(ko=["GLCpts_c0"]),
            template=template,
            biomass=BIOMASS,
            loopless=False,
            conditions=conditions,
        )
        default = recorder[0]
        assert default.candidates, "the ko toy must open the candidate tier"
        assert default.params["candidate_factor"] == 2.0

        fitness.run_fitness_weighted_analysis(
            model=_model(ko=["GLCpts_c0"]),
            template=template,
            biomass=BIOMASS,
            loopless=False,
            candidate_factor=1.0,
            conditions=conditions,
        )
        gaa_like = recorder[1]
        assert gaa_like.params["candidate_factor"] == 1.0

        rid = sorted(default.candidates)[0]
        priced_default = default.reaction_penalties({})
        priced_gaa = gaa_like.reaction_penalties({})
        gap = default.gapfilling_penalties.get(rid) or {}
        if "forward" in gap and gap["forward"] > 0:
            assert priced_default[rid][">"] == pytest.approx(20.0 * gap["forward"])
            assert priced_gaa[rid][">"] == pytest.approx(10.0 * gap["forward"])


# ---------------------------------------------------------------------------
# The entry point's own contract
# ---------------------------------------------------------------------------


class TestEntryPointContract:
    def test_it_never_constructs_an_msfbautils(
        self, template: Any, glucose: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An MSFBAUtils whose constructor raises must not stop the run.

        MSFBAUtils reaches KBModelUtils, which imports cobrakbase and wants a
        KBase token; a fitness run inside a CTS job has neither.  Patching its
        constructor to raise is the only way to assert that nothing on this path
        touches it -- an assertion about an absence that a passing run cannot
        otherwise distinguish from luck.
        """
        from kbutillib.domains.modeling.ms_fba_utils import MSFBAUtils

        def boom(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("ms_fitness_utils must not construct MSFBAUtils")

        monkeypatch.setattr(MSFBAUtils, "__init__", boom)
        result = fitness.run_fitness_weighted_analysis(
            model=_model(extra=_duplicate_pgi),
            template=template,
            biomass=BIOMASS,
            conditions=_one_condition(glucose, {"b4025": (-3.0, -10.0)}),
            loopless=False,
        )
        assert result["conditions"]["glc"]["status"] == "done"

    def test_arguments_are_keyword_only(self, template: Any) -> None:
        with pytest.raises(TypeError):
            fitness.run_fitness_weighted_analysis(_model(), template, {})  # type: ignore[misc]

    def test_every_condition_reports_its_wall_clock(
        self, template: Any, glucose: Any
    ) -> None:
        result = fitness.run_fitness_weighted_analysis(
            model=_model(),
            template=template,
            biomass=BIOMASS,
            loopless=False,
            conditions={
                "glc": {"media": glucose, "fitness": {"b4025": (-3.0, -10.0)}},
                "empty": {"media": {}, "fitness": {"b4025": (-3.0, -10.0)}},
            },
        )
        for condition in result["conditions"].values():
            assert isinstance(condition["time_s"], float)
            assert "n_reactions_discounted" in condition
        # A medium nothing grows on is reported, not retried, and prices nothing.
        empty = result["conditions"]["empty"]
        assert empty["status"] == "no_growth"
        assert empty["n_reactions_discounted"] == 0
