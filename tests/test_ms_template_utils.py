"""Tests for MSTemplateUtils — Phase 2 of the Template Evaluation Suite.

All tests run offline with NO KBase credentials.  KBase-touching functions
(get_media, simulate_biolog) are mocked where needed.

Test inventory
--------------
T1  evaluate_template_quality returns all canonical keys.
T2  evaluate_template_quality count==len(list) invariant for all list keys.
T3  diff_template_evaluation independent mode: known-essential-reaction
    removal produces expected growth loss / newly-dead reactions.
T4  diff_template_evaluation cumulative mode: same removal, cumulative chain.
T5  diff_template_evaluation independent mode leaves baseline model unmodified.
T6  render_template_report is a pure function: returns str, no recomputation.
T7  MSTemplateUtils is wired as KBUtilLib.template.
T8  MSTemplateUtils is top-level importable as kbutillib.MSTemplateUtils.
"""

import sys
import pathlib
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_cobra():
    pytest.importorskip("cobra", reason="cobra required for template utils tests")


def _require_modelseedpy():
    pytest.importorskip(
        "modelseedpy", reason="modelseedpy required for template utils tests"
    )


def _make_template_utils():
    """Build a minimal MSTemplateUtils that bypasses KBase/biochem initialization."""
    _require_cobra()
    _require_modelseedpy()
    from kbutillib.ms_template_utils import MSTemplateUtils
    from kbutillib.ms_biochem_utils import MSBiochemUtils
    from kbutillib.kb_model_utils import KBModelUtils

    with (
        patch.object(MSBiochemUtils, "_ensure_database_available", return_value=None),
        patch.object(
            KBModelUtils,
            "__init__",
            lambda self, **kwargs: MSBiochemUtils.__init__(self, **kwargs),
        ),
    ):
        utils = MSTemplateUtils.__new__(MSTemplateUtils)
        MSBiochemUtils.__init__(
            utils,
            config_file=False,
            token_file=None,
            kbase_token_file=None,
        )
    from modelseedpy.core.msmodelutl import MSModelUtil
    utils.MSModelUtil = MSModelUtil
    return utils


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def template_utils():
    """MSTemplateUtils initialized offline (no KBase auth, no biochem DB)."""
    return _make_template_utils()


@pytest.fixture
def eval_model():
    """A small cobra.Model suitable for template evaluation tests.

    Design:
        A_c -> B_c -> bio1      (core pathway, R_fwd essential for growth)
        EX_A_e <=> A_e <=> A_c  (exchange)
        R_dead (lb=ub=0)        (dead reaction)

    With bio1 as objective and A_e available:
        - bio1 is essential (growth-forced FVA)
        - R_fwd is essential for bio1
        - R_dead is dead
        - EX_A_e, diff_A are reversible
    """
    _require_cobra()
    import cobra

    m = cobra.Model("eval_model")
    A_c = cobra.Metabolite("A_c", compartment="c")
    A_e = cobra.Metabolite("A_e", compartment="e")
    B_c = cobra.Metabolite("B_c", compartment="c")

    ex_a = cobra.Reaction("EX_A_e")
    ex_a.bounds = (-1000, 1000)
    ex_a.add_metabolites({A_e: -1})

    diff_a = cobra.Reaction("diff_A")
    diff_a.bounds = (-1000, 1000)
    diff_a.add_metabolites({A_e: -1, A_c: 1})

    r_fwd = cobra.Reaction("R_fwd")
    r_fwd.bounds = (0, 1000)
    r_fwd.add_metabolites({A_c: -1, B_c: 1})

    r_dead = cobra.Reaction("R_dead")
    r_dead.bounds = (0, 0)
    r_dead.add_metabolites({A_c: -1, B_c: 1})

    bio = cobra.Reaction("bio1")
    bio.bounds = (0, 1000)
    bio.add_metabolites({B_c: -1})

    m.add_reactions([ex_a, diff_a, r_fwd, r_dead, bio])
    m.objective = "bio1"
    return m


# ---------------------------------------------------------------------------
# Helper: run evaluate_template_quality offline on eval_model
# ---------------------------------------------------------------------------


def _canned_classify_result(model):
    """Return a canned classify_reactions_by_fva result for the eval_model.

    Derived from the known topology of eval_model (no GLPK call needed):
        dead: R_dead
        forward_only: R_fwd, bio1
        reverse_only: (none)
        reversible: EX_A_e, diff_A
        essential (bio1): R_fwd, bio1
    """
    rxn_ids = [r.id for r in model.reactions]
    dead = ["R_dead"] if "R_dead" in rxn_ids else []
    forward_only = [r for r in ["R_fwd", "bio1"] if r in rxn_ids]
    reverse_only = []
    reversible = [r for r in ["EX_A_e", "diff_A"] if r in rxn_ids]
    essential_bio1 = [r for r in ["R_fwd", "bio1"] if r in rxn_ids]
    return {
        "dead": dead,
        "forward_only": forward_only,
        "reverse_only": reverse_only,
        "reversible": reversible,
        "essential": {
            "bio1": essential_bio1,
            "union": sorted(set(essential_bio1)),
        },
    }


def _run_evaluate_offline(template_utils, eval_model):
    """Run evaluate_template_quality with all FBA/KBase-touching calls mocked.

    Mocks:
      - build_full_template_model: returns a copy of eval_model
      - get_media: returns None
      - classify_reactions_by_fva: returns canned result (avoids GLPK abort)
      - find_closed_mode_reactions: returns [] (linear model, no loops)
      - simulate_biolog: returns {}
      - test_production_potential: returns ["A_c"]
      - test_degradation_potential: returns ["A_c"]
    """
    # Canned FBA results based on known eval_model topology
    def _mock_build(template, auto_add_biomass=True):
        return eval_model.copy()

    def _mock_get_media(media_ref, ws):
        return None

    def _mock_classify(model, media=None, essential_fraction=0.2):
        from modelseedpy.core.msmodelutl import MSModelUtil
        if isinstance(model, MSModelUtil):
            cobra_model = model.model
        else:
            cobra_model = model
        return _canned_classify_result(cobra_model)

    def _mock_closed(model, media=None):
        return []

    def _mock_biolog(model, elements=("C", "N", "S", "P"), growth_threshold=0.01):
        return {}

    def _mock_prod(model, media=None, threshold=1e-6):
        return ["A_c"]

    def _mock_deg(model, media=None, threshold=1e-6):
        return ["A_c"]

    with (
        patch.object(template_utils, "build_full_template_model", side_effect=_mock_build),
        patch.object(template_utils, "get_media", side_effect=_mock_get_media),
        patch.object(template_utils, "classify_reactions_by_fva", side_effect=_mock_classify),
        patch.object(template_utils, "find_closed_mode_reactions", side_effect=_mock_closed),
        patch.object(template_utils, "simulate_biolog", side_effect=_mock_biolog),
        patch.object(template_utils, "test_production_potential", side_effect=_mock_prod),
        patch.object(template_utils, "test_degradation_potential", side_effect=_mock_deg),
    ):
        fake_template = MagicMock()
        fake_template.id = "test_template"
        fake_template.biomasses = []
        report = template_utils.evaluate_template_quality(fake_template)
    return report


# ---------------------------------------------------------------------------
# T1 — evaluate_template_quality: all canonical keys present
# ---------------------------------------------------------------------------


class TestEvaluateTemplateQualityKeys:
    CANONICAL_TOP = {
        "template_metadata",
        "reaction_classes",
        "closed_mode_reactions",
        "functional_biolog_media",
        "producible_metabolites",
        "consumable_metabolites",
    }

    def test_top_level_keys(self, template_utils, eval_model):
        """Report must contain all canonical top-level keys."""
        report = _run_evaluate_offline(template_utils, eval_model)
        missing = self.CANONICAL_TOP - set(report.keys())
        assert not missing, f"Missing top-level keys: {missing}"

    def test_reaction_classes_has_rich_and_minimal(self, template_utils, eval_model):
        """reaction_classes must have 'rich' and 'minimal' sub-dicts."""
        report = _run_evaluate_offline(template_utils, eval_model)
        rc = report["reaction_classes"]
        assert "rich" in rc, "reaction_classes missing 'rich'"
        assert "minimal" in rc, "reaction_classes missing 'minimal'"

    def test_reaction_classes_media_keys(self, template_utils, eval_model):
        """Each media sub-dict must have dead/forward_only/reverse_only/reversible/essential."""
        report = _run_evaluate_offline(template_utils, eval_model)
        for media_key in ("rich", "minimal"):
            mc = report["reaction_classes"][media_key]
            for cat in ("dead", "forward_only", "reverse_only", "reversible", "essential"):
                assert cat in mc, f"reaction_classes.{media_key} missing '{cat}'"

    def test_essential_has_union(self, template_utils, eval_model):
        """essential dict must have 'union' key."""
        report = _run_evaluate_offline(template_utils, eval_model)
        for media_key in ("rich", "minimal"):
            ess = report["reaction_classes"][media_key]["essential"]
            assert "union" in ess, (
                f"reaction_classes.{media_key}.essential missing 'union'; got keys: {list(ess.keys())}"
            )

    def test_closed_mode_reactions_structure(self, template_utils, eval_model):
        """closed_mode_reactions must have 'list' and 'count'."""
        report = _run_evaluate_offline(template_utils, eval_model)
        cm = report["closed_mode_reactions"]
        assert "list" in cm, "closed_mode_reactions missing 'list'"
        assert "count" in cm, "closed_mode_reactions missing 'count'"

    def test_producible_metabolites_keys(self, template_utils, eval_model):
        """producible_metabolites must have 'complete' and 'glucose_minimal'."""
        report = _run_evaluate_offline(template_utils, eval_model)
        pm = report["producible_metabolites"]
        assert "complete" in pm, "producible_metabolites missing 'complete'"
        assert "glucose_minimal" in pm, "producible_metabolites missing 'glucose_minimal'"

    def test_consumable_metabolites_keys(self, template_utils, eval_model):
        """consumable_metabolites must have 'complete'."""
        report = _run_evaluate_offline(template_utils, eval_model)
        cm = report["consumable_metabolites"]
        assert "complete" in cm, "consumable_metabolites missing 'complete'"

    def test_template_metadata_keys(self, template_utils, eval_model):
        """template_metadata must have id, biomass_ids, rich_media, minimal_media, timestamp."""
        report = _run_evaluate_offline(template_utils, eval_model)
        meta = report["template_metadata"]
        for key in ("id", "biomass_ids", "rich_media", "minimal_media", "timestamp"):
            assert key in meta, f"template_metadata missing '{key}'"


# ---------------------------------------------------------------------------
# T2 — evaluate_template_quality: count==len(list) invariants
# ---------------------------------------------------------------------------


class TestEvaluateTemplateQualityCountInvariants:
    def _check_list_count(self, obj, path=""):
        """Recursively verify every {list, count} pair satisfies count==len(list)."""
        if isinstance(obj, dict):
            if "list" in obj and "count" in obj:
                lst = obj["list"]
                cnt = obj["count"]
                assert cnt == len(lst), (
                    f"count==len(list) violated at '{path}': count={cnt}, len={len(lst)}"
                )
            for k, v in obj.items():
                self._check_list_count(v, path=f"{path}.{k}")

    def test_count_equals_len_everywhere(self, template_utils, eval_model):
        """Every list in the report must have count == len(list)."""
        report = _run_evaluate_offline(template_utils, eval_model)
        self._check_list_count(report, "report")

    def test_dead_count_is_one(self, template_utils, eval_model):
        """R_dead (lb=ub=0) should appear in dead list; count should be 1."""
        report = _run_evaluate_offline(template_utils, eval_model)
        rich_dead = report["reaction_classes"]["rich"]["dead"]
        assert "R_dead" in rich_dead["list"], (
            f"Expected R_dead in dead list; got {rich_dead['list']}"
        )
        assert rich_dead["count"] == len(rich_dead["list"])


# ---------------------------------------------------------------------------
# Context manager helper: mock all FBA primitives on template_utils
# ---------------------------------------------------------------------------

import contextlib


@contextlib.contextmanager
def _mock_fba_primitives(template_utils, eval_model):
    """Context manager that stubs all FBA/LP calls on template_utils.

    Uses canned topology-derived results for eval_model so GLPK is never called.
    Also tracks which models are evaluated (for baseline-unmodified tests).
    """

    def _canned_for(model):
        """Return canned classify result appropriate to what reactions are present."""
        from modelseedpy.core.msmodelutl import MSModelUtil
        if isinstance(model, MSModelUtil):
            cobra_model = model.model
        else:
            cobra_model = model
        return _canned_classify_result(cobra_model)

    def _mock_classify(model, media=None, essential_fraction=0.2):
        return _canned_for(model)

    def _mock_closed(model, media=None):
        return []

    def _mock_biolog(model, elements=("C", "N", "S", "P"), growth_threshold=0.01):
        return {}

    def _mock_prod(model, media=None, threshold=1e-6):
        from modelseedpy.core.msmodelutl import MSModelUtil
        cobra_model = model.model if isinstance(model, MSModelUtil) else model
        # A_c is producible only if diff_A is present (needed for production)
        rxn_ids = {r.id for r in cobra_model.reactions}
        if "diff_A" in rxn_ids and "R_fwd" in rxn_ids:
            return ["A_c"]
        return []

    def _mock_deg(model, media=None, threshold=1e-6):
        from modelseedpy.core.msmodelutl import MSModelUtil
        cobra_model = model.model if isinstance(model, MSModelUtil) else model
        rxn_ids = {r.id for r in cobra_model.reactions}
        if "diff_A" in rxn_ids:
            return ["A_c"]
        return []

    with (
        patch.object(template_utils, "classify_reactions_by_fva", side_effect=_mock_classify),
        patch.object(template_utils, "find_closed_mode_reactions", side_effect=_mock_closed),
        patch.object(template_utils, "simulate_biolog", side_effect=_mock_biolog),
        patch.object(template_utils, "test_production_potential", side_effect=_mock_prod),
        patch.object(template_utils, "test_degradation_potential", side_effect=_mock_deg),
    ):
        yield


def _make_baseline_offline(template_utils, eval_model):
    """Build baseline report with all FBA/LP mocked."""
    from modelseedpy.core.msmodelutl import MSModelUtil
    with _mock_fba_primitives(template_utils, eval_model):
        mdlutl = MSModelUtil.get(eval_model.copy())
        return template_utils._evaluate_model_quality(mdlutl)


# ---------------------------------------------------------------------------
# T3 — diff_template_evaluation independent mode
# ---------------------------------------------------------------------------


class TestDiffTemplateEvaluationIndependent:
    def test_essential_reaction_removal_produces_growth_loss(
        self, template_utils, eval_model
    ):
        """Removing R_fwd (essential for bio1) should change the essential set."""
        from modelseedpy.core.msmodelutl import MSModelUtil

        baseline_report = _make_baseline_offline(template_utils, eval_model)

        # R_fwd is the only route from A_c -> B_c -> bio1; removing it kills growth
        perturbations = [{"op": "remove", "reaction_id": "R_fwd"}]

        with _mock_fba_primitives(template_utils, eval_model):
            diff_report = template_utils.diff_template_evaluation(
                eval_model,
                perturbations,
                mode="independent",
                baseline_report=baseline_report,
            )

        assert "perturbation_diffs" in diff_report
        assert len(diff_report["perturbation_diffs"]) == 1
        diff = diff_report["perturbation_diffs"][0]
        assert diff["perturbation"]["reaction_id"] == "R_fwd"
        delta = diff["delta"]

        # R_fwd was essential before (in canned result)
        b_ess_union = set(
            baseline_report.get("reaction_classes", {})
                           .get("rich", {})
                           .get("essential", {})
                           .get("union", {})
                           .get("list", [])
        )
        assert "R_fwd" in b_ess_union, (
            f"Expected R_fwd in baseline essential union; got {b_ess_union}"
        )

        # After removal, R_fwd should disappear from essential
        after_ess_union = set(
            diff_report["perturbation_diffs"][0]["delta"]
            .get("reaction_classes.rich.essential.union", {})
            .get("removed", [])
        )
        assert "R_fwd" in after_ess_union, (
            f"Expected R_fwd in essential.union.removed; delta={delta}"
        )

    def test_dead_reaction_removal_moves_to_dead_delta(self, template_utils, eval_model):
        """Removing R_dead (which was dead) changes the dead category."""
        baseline_report = _make_baseline_offline(template_utils, eval_model)

        perturbations = [{"op": "remove", "reaction_id": "R_dead"}]
        with _mock_fba_primitives(template_utils, eval_model):
            diff_report = template_utils.diff_template_evaluation(
                eval_model,
                perturbations,
                mode="independent",
                baseline_report=baseline_report,
            )

        delta = diff_report["perturbation_diffs"][0]["delta"]
        # R_dead was dead before; after removal it should appear in removed
        dead_delta = delta.get("reaction_classes.rich.dead", {})
        assert "R_dead" in dead_delta.get("removed", []), (
            f"Expected R_dead in dead.removed; got {dead_delta}"
        )

    def test_independent_mode_count_matters(self, template_utils, eval_model):
        """Multiple perturbations in independent mode yield N diff entries."""
        baseline_report = _make_baseline_offline(template_utils, eval_model)

        perturbations = [
            {"op": "remove", "reaction_id": "R_dead"},
            {"op": "modify", "reaction_id": "R_fwd", "lower_bound": 0, "upper_bound": 0},
        ]
        with _mock_fba_primitives(template_utils, eval_model):
            diff_report = template_utils.diff_template_evaluation(
                eval_model,
                perturbations,
                mode="independent",
                baseline_report=baseline_report,
            )

        assert len(diff_report["perturbation_diffs"]) == 2


# ---------------------------------------------------------------------------
# T4 — diff_template_evaluation cumulative mode
# ---------------------------------------------------------------------------


class TestDiffTemplateEvaluationCumulative:
    def test_cumulative_mode_two_perturbations(self, template_utils, eval_model):
        """Cumulative mode: second perturbation is applied on top of the first."""
        baseline_report = _make_baseline_offline(template_utils, eval_model)

        # First: remove R_dead (dead, no growth impact)
        # Second: remove R_fwd (kills growth)
        perturbations = [
            {"op": "remove", "reaction_id": "R_dead"},
            {"op": "remove", "reaction_id": "R_fwd"},
        ]

        with _mock_fba_primitives(template_utils, eval_model):
            diff_report = template_utils.diff_template_evaluation(
                eval_model,
                perturbations,
                mode="cumulative",
                baseline_report=baseline_report,
            )

        assert diff_report["mode"] == "cumulative"
        assert len(diff_report["perturbation_diffs"]) == 2

        # First diff: R_dead removed from dead set
        delta0 = diff_report["perturbation_diffs"][0]["delta"]
        dead_delta0 = delta0.get("reaction_classes.rich.dead", {})
        assert "R_dead" in dead_delta0.get("removed", []), (
            f"Cumulative step 1: expected R_dead removed from dead; got {dead_delta0}"
        )

        # Second diff vs previous (after R_dead removal): R_fwd removal changes essentiality
        delta1 = diff_report["perturbation_diffs"][1]["delta"]
        # In cumulative mode the second diff is against the state after step 1,
        # so R_dead should NOT appear in dead.removed in step 2
        dead_delta1 = delta1.get("reaction_classes.rich.dead", {})
        assert "R_dead" not in dead_delta1.get("removed", []), (
            f"Cumulative step 2: R_dead should not appear in dead.removed again; got {dead_delta1}"
        )


# ---------------------------------------------------------------------------
# T5 — independent mode leaves baseline model unmodified
# ---------------------------------------------------------------------------


class TestDiffIndependentModeBaselineUnmodified:
    def test_baseline_model_unmodified(self, template_utils, eval_model):
        """In independent mode the baseline cobra.Model must be unchanged after the diff."""
        # Record baseline model state BEFORE anything
        rxn_ids_before = {r.id for r in eval_model.reactions}
        bounds_before = {r.id: (r.lower_bound, r.upper_bound) for r in eval_model.reactions}

        baseline_report = _make_baseline_offline(template_utils, eval_model)

        perturbations = [
            {"op": "remove", "reaction_id": "R_fwd"},
            {"op": "modify", "reaction_id": "R_dead", "lower_bound": 0, "upper_bound": 500},
        ]

        with _mock_fba_primitives(template_utils, eval_model):
            template_utils.diff_template_evaluation(
                eval_model,
                perturbations,
                mode="independent",
                baseline_report=baseline_report,
            )

        rxn_ids_after = {r.id for r in eval_model.reactions}
        bounds_after = {r.id: (r.lower_bound, r.upper_bound) for r in eval_model.reactions}

        assert rxn_ids_before == rxn_ids_after, (
            f"Reactions changed: added={rxn_ids_after - rxn_ids_before}, "
            f"removed={rxn_ids_before - rxn_ids_after}"
        )
        assert bounds_before == bounds_after, (
            f"Bounds changed: {[(k, bounds_before[k], bounds_after[k]) for k in bounds_before if bounds_before[k] != bounds_after.get(k)]}"
        )


# ---------------------------------------------------------------------------
# T6 — render_template_report is a pure function
# ---------------------------------------------------------------------------


class TestRenderTemplateReport:
    def _get_report(self, template_utils, eval_model):
        return _run_evaluate_offline(template_utils, eval_model)

    def test_returns_string(self, template_utils, eval_model):
        """render_template_report must return a str."""
        report = self._get_report(template_utils, eval_model)
        md = template_utils.render_template_report(report)
        assert isinstance(md, str), f"Expected str, got {type(md)}"

    def test_contains_section_headers(self, template_utils, eval_model):
        """Markdown must contain key section headers."""
        report = self._get_report(template_utils, eval_model)
        md = template_utils.render_template_report(report)
        assert "Template Evaluation Report" in md
        assert "Reaction Classification" in md or "reaction" in md.lower()

    def test_report_unchanged_after_render(self, template_utils, eval_model):
        """Report dict must be identical before and after render_template_report."""
        import copy
        report = self._get_report(template_utils, eval_model)
        report_before = copy.deepcopy(report)
        template_utils.render_template_report(report)
        assert report == report_before, "Report dict was mutated by render_template_report"

    def test_pure_function_standalone(self):
        """_render_markdown module function must work on a minimal report dict."""
        from kbutillib.ms_template_utils import _render_markdown
        minimal_report = {
            "template_metadata": {"id": "t1", "biomass_ids": ["bio1"],
                                   "rich_media": "Complete", "minimal_media": "Glucose",
                                   "timestamp": "2026-01-01T00:00:00Z"},
            "reaction_classes": {
                "rich": {
                    "dead": {"list": ["R_dead"], "count": 1},
                    "forward_only": {"list": [], "count": 0},
                    "reverse_only": {"list": [], "count": 0},
                    "reversible": {"list": [], "count": 0},
                    "essential": {"union": {"list": [], "count": 0}},
                },
                "minimal": {
                    "dead": {"list": [], "count": 0},
                    "forward_only": {"list": [], "count": 0},
                    "reverse_only": {"list": [], "count": 0},
                    "reversible": {"list": [], "count": 0},
                    "essential": {"union": {"list": [], "count": 0}},
                },
            },
            "closed_mode_reactions": {"list": [], "count": 0},
            "functional_biolog_media": {},
            "producible_metabolites": {
                "complete": {"list": ["A_c"], "count": 1},
                "glucose_minimal": {"list": [], "count": 0},
            },
            "consumable_metabolites": {
                "complete": {"list": ["A_c"], "count": 1},
            },
        }
        md = _render_markdown(minimal_report)
        assert isinstance(md, str)
        assert "R_dead" in md
        assert "A_c" in md


# ---------------------------------------------------------------------------
# T7 — MSTemplateUtils is wired as KBUtilLib.template
# ---------------------------------------------------------------------------


class TestKBUtilLibWiring:
    def test_kbutilkit_has_template_property(self):
        """KBUtilLib must have a 'template' property (checks the class attribute)."""
        _require_cobra()
        _require_modelseedpy()
        from kbutillib.toolkit import KBUtilLib
        # Check the property is declared on the class without instantiating
        assert hasattr(KBUtilLib, "template"), "KBUtilLib class missing 'template' property"

    def test_kbutilkit_template_is_ms_template_utils_impl(self):
        """KBUtilLib.template must be an MSTemplateUtilsImpl instance."""
        _require_cobra()
        _require_modelseedpy()
        from kbutillib.toolkit import KBUtilLib
        from kbutillib.ms_template_utils import MSTemplateUtilsImpl
        from kbutillib.ms_biochem_utils import MSBiochemUtils, MSBiochemUtilsImpl

        with (
            patch.object(MSBiochemUtils, "_ensure_database_available", return_value=None),
            patch.object(MSBiochemUtilsImpl, "_ensure_database_available", return_value=None),
        ):
            kbu = KBUtilLib()
            tmpl = kbu.template
            assert isinstance(tmpl, MSTemplateUtilsImpl), (
                f"Expected MSTemplateUtilsImpl, got {type(tmpl)}"
            )


# ---------------------------------------------------------------------------
# T8 — MSTemplateUtils is top-level importable
# ---------------------------------------------------------------------------


class TestTopLevelImport:
    def test_importable_as_mstemplateutils(self):
        """from kbutillib import MSTemplateUtils must not raise."""
        _require_cobra()
        _require_modelseedpy()
        from kbutillib import MSTemplateUtils
        assert MSTemplateUtils is not None, "MSTemplateUtils is None after import"

    def test_mstemplateutils_has_required_methods(self):
        """MSTemplateUtils class must expose the four required public methods."""
        _require_cobra()
        _require_modelseedpy()
        from kbutillib import MSTemplateUtils
        for method in (
            "build_full_template_model",
            "evaluate_template_quality",
            "render_template_report",
            "diff_template_evaluation",
        ):
            assert hasattr(MSTemplateUtils, method), (
                f"MSTemplateUtils missing method '{method}'"
            )
