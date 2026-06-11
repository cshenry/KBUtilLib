"""Integration test for MSReconstructionUtils.run_comprehensive_gapfill_on_model.

Tests the KBUtilLib wrapper (Module C) end-to-end on a small e_coli_core
model + template_core_bigg template from ModelSEEDpy's test data. The same
toy-model construction pattern is used as in ModelSEEDpy's
tests/core/test_comprehensive_gapfill.py (forward-only model to keep Stage 1
of the MILP tractable with the GLPK solver).

**GLPK solver constraint**: Stage 1 of comprehensive gapfilling is a MILP
(RevBinPkg adds binary variables for each reaction direction in the
activation filter). With GLPK (the only solver available on this machine),
large MILPs are intractable. We therefore use a forward-only model — all
reversible non-exchange reactions are stripped so the activation filter
contains only forward-only reactions, whose RevBin binary vars are trivially
integral and solve immediately.

The test asserts:
  (a) the result tuple has the correct shape (4 elements)
  (b) the returned solutions dict is non-empty
  (c) the model grows (biomass slim_optimize > 0) after gapfilling
  (d) the reaction count in the gapfilled model exceeds the pre-gapfill count
"""
from __future__ import annotations

import json
import os

import pytest

# ModelSEEDpy test data lives in the Dropbox Projects repo.  We reference it
# by absolute path so the test is not coupled to the KBUtilLib fixtures dir.
_MODELSEEDPY_TEST_DATA = os.path.join(
    os.path.expanduser("~"),
    "Dropbox", "Projects", "ModelSEEDpy", "tests", "test_data",
)
_MODEL_PATH = os.path.join(_MODELSEEDPY_TEST_DATA, "e_coli_core.json")
_TEMPLATE_PATH = os.path.join(_MODELSEEDPY_TEST_DATA, "template_core_bigg.json")


# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

def _data_available():
    return os.path.isfile(_MODEL_PATH) and os.path.isfile(_TEMPLATE_PATH)


pytestmark = pytest.mark.skipif(
    not _data_available(),
    reason="ModelSEEDpy test data not found at ~/Dropbox/Projects/ModelSEEDpy/tests/test_data/",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_fwd_only_model(ko=None):
    """Load e_coli_core and strip reversible non-exchange reactions.

    Produces a model where all remaining non-exchange reactions are
    forward-only (lower_bound >= 0), so RevBin binary vars in Stage 1 are
    trivially integral and GLPK solves in seconds.
    """
    import cobra

    if ko is None:
        ko = []

    with open(_MODEL_PATH) as fh:
        model_json = json.load(fh)

    # Rename compartments/metabolites/reactions to ModelSEED compartment suffix
    model_json["compartments"] = {
        k + "0": v for k, v in model_json["compartments"].items()
    }
    metabolites = {}
    for m in model_json["metabolites"]:
        m["id"] += "0"
        m["compartment"] += "0"
        metabolites[m["id"]] = m
    for r in model_json["reactions"]:
        r["metabolites"] = {i + "0": v for i, v in r["metabolites"].items()}
        compartments = set(
            metabolites[k]["compartment"] for k in r["metabolites"]
        )
        if r["id"].endswith("_e"):
            r["id"] += "0"
        elif len(compartments) == 1:
            r["id"] += "_" + list(compartments)[0]
        else:
            r["id"] += "_c0"

    # Forward-only filter: keep exchanges + forward-only reactions; drop KOs
    model_json["reactions"] = [
        r for r in model_json["reactions"]
        if r["id"] not in ko
        and (
            r["id"].startswith("EX_")
            or r.get("lower_bound", -1000) >= 0
        )
    ]
    model_json["metabolites"] = list(metabolites.values())
    return cobra.io.from_json(json.dumps(model_json))


def _build_template():
    from modelseedpy.core.mstemplate import MSTemplateBuilder

    with open(_TEMPLATE_PATH) as fh:
        return MSTemplateBuilder.from_dict(json.load(fh)).build()


def _build_complete_media():
    """Generous complete media for gapfilling."""
    from modelseedpy import MSMedia

    return MSMedia.from_dict(
        {
            "glc__D": (-1000, 1000),
            "o2": (-1000, 1000),
            "h": (-1000, 1000),
            "h2o": (-1000, 1000),
            "pi": (-1000, 1000),
            "co2": (-1000, 1000),
            "nh4": (-1000, 1000),
        }
    )


# ---------------------------------------------------------------------------
# Minimal stub for MSReconstructionUtils
# ---------------------------------------------------------------------------

class _StubReconUtils:
    """Lightweight stand-in for MSReconstructionUtils.

    We don't instantiate the full KBModelUtils hierarchy (which requires a
    KBase token and network access).  Instead we stub out only the methods
    called by run_comprehensive_gapfill_on_model:

    - get_template()       -> returns the pre-built template fixture
    - get_media()          -> returns the complete media fixture
    - modelseedpy_data_dir -> path to ModelSEEDpy data (for ATP media TSV)
    - templates["core"]    -> sentinel so get_template is called correctly
    - MSGapfill            -> real class from modelseedpy
    """

    def __init__(self, template, complete_media, modelseedpy_data_dir):
        self._template = template
        self._complete_media = complete_media
        self.modelseedpy_data_dir = modelseedpy_data_dir
        self.templates = {"core": "core_sentinel"}

        from modelseedpy import MSGapfill
        self.MSGapfill = MSGapfill

    def get_template(self, ref, ws):
        return self._template

    def get_media(self, ref, ws):
        return self._complete_media

    # Bind the real implementation as an instance method
    run_comprehensive_gapfill_on_model = (
        __import__(
            "kbutillib.ms_reconstruction_utils",
            fromlist=["MSReconstructionUtils"],
        ).MSReconstructionUtils.run_comprehensive_gapfill_on_model
    )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def template():
    return _build_template()


@pytest.fixture(scope="module")
def complete_media():
    return _build_complete_media()


@pytest.fixture
def modelseedpy_data_dir():
    import modelseedpy
    return os.path.join(os.path.dirname(modelseedpy.__file__), "data")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_run_comprehensive_gapfill_on_model_returns_correct_shape(
    template, complete_media, modelseedpy_data_dir
):
    """run_comprehensive_gapfill_on_model returns a 4-tuple."""
    from modelseedpy import MSModelUtil

    model = _build_fwd_only_model(ko=["GLCpts_c0"])
    mdlutl = MSModelUtil.get(model)

    stub = _StubReconUtils(template, complete_media, modelseedpy_data_dir)
    result = stub.run_comprehensive_gapfill_on_model(
        mdlutl=mdlutl,
        templates=[template],
        media=complete_media,
        atp_safe=False,          # skip ATP tests — no ATP media on this model
        minimum_objective=0.01,
        objective="BIOMASS_Ecoli_core_w_GAM_c0",
    )

    assert isinstance(result, tuple), "Return value must be a tuple"
    assert len(result) == 4, f"Expected 4-tuple, got {len(result)}-tuple"

    current_output, solutions, output_solution, output_solution_media = result
    assert isinstance(current_output, dict), "current_output must be a dict"
    for key in ("Growth", "GS GF", "Reactions", "Model genes"):
        assert key in current_output, f"current_output missing key '{key}'"


def test_run_comprehensive_gapfill_on_model_solutions_nonempty(
    template, complete_media, modelseedpy_data_dir
):
    """solutions dict is non-empty after successful comprehensive gapfill."""
    from modelseedpy import MSModelUtil

    model = _build_fwd_only_model(ko=["GLCpts_c0"])
    mdlutl = MSModelUtil.get(model)

    stub = _StubReconUtils(template, complete_media, modelseedpy_data_dir)
    _, solutions, _, _ = stub.run_comprehensive_gapfill_on_model(
        mdlutl=mdlutl,
        templates=[template],
        media=complete_media,
        atp_safe=False,
        minimum_objective=0.01,
        objective="BIOMASS_Ecoli_core_w_GAM_c0",
    )

    assert solutions, (
        "solutions dict is empty — run_comprehensive_gapfill_on_model found no solution"
    )


def test_run_comprehensive_gapfill_on_model_model_grows(
    template, complete_media, modelseedpy_data_dir
):
    """After comprehensive gapfilling, the solutions dict records positive growth."""
    from modelseedpy import MSModelUtil

    model = _build_fwd_only_model(ko=["GLCpts_c0"])
    mdlutl = MSModelUtil.get(model)

    stub = _StubReconUtils(template, complete_media, modelseedpy_data_dir)
    _, solutions, _, _ = stub.run_comprehensive_gapfill_on_model(
        mdlutl=mdlutl,
        templates=[template],
        media=complete_media,
        atp_safe=False,
        minimum_objective=0.01,
        objective="BIOMASS_Ecoli_core_w_GAM_c0",
    )

    # run_multi_gapfill records growth per media in solutions[media]["growth"]
    # (set by integrate_gapfill_solution when check_for_growth=True).
    # Verify that the model grows on the gapfill medium.
    assert solutions, "solutions dict is empty — no gapfill solution found"
    growing = any(
        sol.get("growth", 0) > 0
        for sol in solutions.values()
        if isinstance(sol, dict)
    )
    assert growing, (
        f"No growing solution found in solutions dict after comprehensive gapfilling. "
        f"Growth values: {[sol.get('growth') for sol in solutions.values() if isinstance(sol, dict)]}"
    )


def test_run_comprehensive_gapfill_on_model_reaction_count_increases(
    template, complete_media, modelseedpy_data_dir
):
    """Reaction count in the gapfilled model exceeds the pre-gapfill count."""
    from modelseedpy import MSModelUtil

    model = _build_fwd_only_model(ko=["GLCpts_c0"])
    pre_count = len(model.reactions)
    mdlutl = MSModelUtil.get(model)

    stub = _StubReconUtils(template, complete_media, modelseedpy_data_dir)
    stub.run_comprehensive_gapfill_on_model(
        mdlutl=mdlutl,
        templates=[template],
        media=complete_media,
        atp_safe=False,
        minimum_objective=0.01,
        objective="BIOMASS_Ecoli_core_w_GAM_c0",
    )

    post_count = len(model.reactions)
    assert post_count > pre_count, (
        f"No reactions were added: pre={pre_count}, post={post_count}. "
        "Comprehensive gapfilling should add database reactions to the model."
    )
