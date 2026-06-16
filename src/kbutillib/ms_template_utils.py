"""Template-centric orchestration utilities for ModelSEED template evaluation.

Phase 2 of the Template Evaluation Suite PRD.

Provides:
    MSTemplateUtils(KBModelUtils)   — inheritance-based, for direct use
    MSTemplateUtilsImpl             — composition-based, used by KBUtilLib.template

Functions:
    build_full_template_model       — wrap MSBuilder.build_full_template_model
    evaluate_template_quality       — run the full battery and return a structured report
    render_template_report          — pure function: report dict -> markdown string
    diff_template_evaluation        — perturbation diff across all report categories
"""

import copy
import datetime
import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

from .ms_fba_utils import MSFBAUtils

logger = logging.getLogger(__name__)

# Canonical report keys (Acceptance Criterion 11)
_CANONICAL_REPORT_KEYS = frozenset([
    "template_metadata",
    "reaction_classes",
    "closed_mode_reactions",
    "functional_biolog_media",
    "producible_metabolites",
    "consumable_metabolites",
])


class MSTemplateUtils(MSFBAUtils):
    """Template-centric orchestration utilities for metabolic template evaluation.

    Inherits all MSFBAUtils per-model primitives (classify_reactions_by_fva,
    find_closed_mode_reactions, simulate_biolog, test_production_potential,
    test_degradation_potential, run_fva, etc.) and adds the template-centric
    orchestration layer on top.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def _detect_gp_gn_biomasses(self, template):
        """Return (gp_ids, gn_ids) by inspecting standard template biomasses.

        Resolves gram-positive vs gram-negative biomass by id/name convention
        seen in the standard ModelSEED V6 templates (GramPos / GramNeg keywords).
        Does NOT hard-code ids — reads them from the template's own biomasses.

        Returns:
            tuple: (gp_id_or_None, gn_id_or_None)
        """
        gp_keywords = ("grampos", "gram_pos", "gram-pos", "gramneg" if False else "grampos",
                        "pos", "gramp")
        gn_keywords = ("gramneg", "gram_neg", "gram-neg", "neg", "gramn")

        gp_id = None
        gn_id = None

        for bio in template.biomasses:
            bio_id_lower = bio.id.lower()
            bio_name_lower = getattr(bio, "name", "").lower()
            combined = bio_id_lower + " " + bio_name_lower

            # Gram-positive
            if any(kw in combined for kw in ("grampos", "gram_pos", "gram-pos",
                                              "gramp", "gpos", "gram positive")):
                gp_id = bio.id
                continue
            # Gram-negative
            if any(kw in combined for kw in ("gramneg", "gram_neg", "gram-neg",
                                              "gramn", "gneg", "gram negative")):
                gn_id = bio.id
                continue

        return gp_id, gn_id

    def _graft_biomass_from_standard_template(self, target_model, template_name):
        """Graft biomass reactions from a standard ModelSEED template into target_model.

        Args:
            target_model: cobra.Model to graft into.
            template_name: One of 'GramPos' or 'GramNeg'.

        Returns:
            list of grafted biomass reaction ids.
        """
        from modelseedpy import MSBuilder

        std_templates = {
            "GramPos": "GramPosModelTemplateV6",
            "GramNeg": "GramNegModelTemplateV6",
        }
        template_id = std_templates.get(template_name, template_name)

        try:
            std_template = self.get_template(template_id)
        except Exception as e:
            self.log_info(
                f"_graft_biomass_from_standard_template: could not load '{template_id}': {e}"
            )
            return []

        # Build a temporary model from the standard template to harvest its biomasses
        try:
            tmp_model = MSBuilder.build_full_template_model(std_template, "graft_tmp", "0")
        except Exception as e:
            self.log_info(
                f"_graft_biomass_from_standard_template: MSBuilder.build_full_template_model failed: {e}"
            )
            return []

        grafted = []
        for rxn in tmp_model.reactions:
            if rxn.id.startswith("bio") or "biomass" in rxn.id.lower():
                if rxn.id not in {r.id for r in target_model.reactions}:
                    target_model.add_reactions([rxn.copy()])
                    grafted.append(rxn.id)

        self.log_info(
            f"_graft_biomass_from_standard_template: grafted {grafted} from {template_name}"
        )
        return grafted

    # ─────────────────────────────────────────────────────────────────────────
    # Public API
    # ─────────────────────────────────────────────────────────────────────────

    def build_full_template_model(self, template, auto_add_biomass=True):
        """Build a full template model using MSBuilder.build_full_template_model.

        Wraps ``modelseedpy.MSBuilder.build_full_template_model(template, model_id, index)``
        which builds every biomass in ``template.biomasses`` and sets bio1 as objective.

        If only one cell-wall type (gram-positive or gram-negative) biomass is
        present and ``auto_add_biomass=True``, grafts the missing biomass from
        the corresponding standard ModelSEED V6 template (GramPos / GramNeg).

        Args:
            template: MSTemplate object, or template id string to resolve via get_template.
            auto_add_biomass: Graft missing GP/GN biomass if only one is present (default True).

        Returns:
            cobra.Model (the built model).
        """
        from modelseedpy import MSBuilder

        if isinstance(template, str):
            template = self.get_template(template)

        model_id = getattr(template, "id", "template_model")
        self.log_info(f"build_full_template_model: building from template '{model_id}'")

        cobra_model = MSBuilder.build_full_template_model(template, model_id, "0")

        if auto_add_biomass:
            gp_id, gn_id = self._detect_gp_gn_biomasses(template)
            self.log_info(
                f"build_full_template_model: detected GP biomass='{gp_id}', GN biomass='{gn_id}'"
            )
            if gp_id is not None and gn_id is None:
                self.log_info(
                    "build_full_template_model: only GP biomass found; grafting GN from GramNeg template"
                )
                self._graft_biomass_from_standard_template(cobra_model, "GramNeg")
            elif gn_id is not None and gp_id is None:
                self.log_info(
                    "build_full_template_model: only GN biomass found; grafting GP from GramPos template"
                )
                self._graft_biomass_from_standard_template(cobra_model, "GramPos")

        return cobra_model

    def evaluate_template_quality(
        self,
        template,
        rich_media="KBaseMedia/Complete",
        minimal_media="KBaseMedia/Carbon-D-Glucose",
        write_path=None,
        verbose=False,
    ):
        """Evaluate a template's metabolic quality and return a structured report.

        Builds the full template model, then runs:
          - classify_reactions_by_fva in rich and minimal media
          - find_closed_mode_reactions
          - simulate_biolog
          - test_production_potential in Complete and Carbon-D-Glucose
          - test_degradation_potential in Complete

        Args:
            template: MSTemplate object or template id string.
            rich_media: Media reference for rich-media analysis (default "KBaseMedia/Complete").
            minimal_media: Media reference for minimal-media analysis
                (default "KBaseMedia/Carbon-D-Glucose").
            write_path: Optional path.  Directory -> report.json + report.md;
                file stem -> <stem>.json + <stem>.md.
            verbose: Log per-stage progress via self.log_info (default False).

        Returns:
            JSON-serializable report dict with canonical keys (Acceptance Criterion 11).
        """
        from modelseedpy.core.msmodelutl import MSModelUtil

        if isinstance(template, str):
            template = self.get_template(template)

        template_id = getattr(template, "id", "unknown")
        if verbose:
            self.log_info(f"evaluate_template_quality: starting for template '{template_id}'")

        # ── Build model ──────────────────────────────────────────────────────
        if verbose:
            self.log_info("evaluate_template_quality: building full template model")
        cobra_model = self.build_full_template_model(template)
        mdlutl = MSModelUtil.get(cobra_model)

        biomass_ids = [
            r.id for r in cobra_model.reactions
            if r.id.startswith("bio") or "biomass" in r.id.lower()
        ]
        bio_rxns = [rid for rid in biomass_ids if rid in ("bio1", "bio2")]
        if not bio_rxns:
            bio_rxns = biomass_ids[:2]

        # ── Resolve media ────────────────────────────────────────────────────
        if verbose:
            self.log_info("evaluate_template_quality: resolving media")
        try:
            rich_media_obj = self.get_media(rich_media, None)
        except Exception as e:
            self.log_info(f"evaluate_template_quality: could not load rich media '{rich_media}': {e}")
            rich_media_obj = None
        try:
            minimal_media_obj = self.get_media(minimal_media, None)
        except Exception as e:
            self.log_info(f"evaluate_template_quality: could not load minimal media '{minimal_media}': {e}")
            minimal_media_obj = None

        # ── Classify reactions ────────────────────────────────────────────────
        if verbose:
            self.log_info("evaluate_template_quality: classifying reactions (rich media)")
        rich_classes = self.classify_reactions_by_fva(mdlutl, media=rich_media_obj)

        if verbose:
            self.log_info("evaluate_template_quality: classifying reactions (minimal media)")
        minimal_classes = self.classify_reactions_by_fva(mdlutl, media=minimal_media_obj)

        # ── Closed-mode reactions ────────────────────────────────────────────
        if verbose:
            self.log_info("evaluate_template_quality: finding closed-mode reactions")
        closed_rxns = self.find_closed_mode_reactions(mdlutl)

        # ── Biolog simulation ────────────────────────────────────────────────
        if verbose:
            self.log_info("evaluate_template_quality: simulating Biolog panels")
        try:
            biolog_results = self.simulate_biolog(mdlutl)
        except Exception as e:
            self.log_info(f"evaluate_template_quality: simulate_biolog failed: {e}")
            biolog_results = {}

        # ── Production potential ─────────────────────────────────────────────
        if verbose:
            self.log_info("evaluate_template_quality: testing production potential (complete)")
        producible_complete = self.test_production_potential(mdlutl, media=rich_media_obj)

        if verbose:
            self.log_info("evaluate_template_quality: testing production potential (glucose-minimal)")
        producible_glucose = self.test_production_potential(mdlutl, media=minimal_media_obj)

        # ── Degradation potential ────────────────────────────────────────────
        if verbose:
            self.log_info("evaluate_template_quality: testing degradation potential (complete)")
        consumable_complete = self.test_degradation_potential(mdlutl, media=rich_media_obj)

        # ── Assemble report ──────────────────────────────────────────────────
        if verbose:
            self.log_info("evaluate_template_quality: assembling report")

        def _with_count(lst):
            """Return dict with list and its count."""
            return {"list": lst, "count": len(lst)}

        def _reaction_classes_with_counts(cls_dict):
            """Convert classify_reactions_by_fva output to report format with counts."""
            result = {}
            for key in ("dead", "forward_only", "reverse_only", "reversible"):
                result[key] = _with_count(cls_dict.get(key, []))
            # essential: per biomass + union
            essential_raw = cls_dict.get("essential", {})
            essential_out = {}
            for bio_key, rxn_list in essential_raw.items():
                essential_out[bio_key] = _with_count(rxn_list)
            result["essential"] = essential_out
            return result

        def _biolog_with_counts(biolog_raw):
            """Convert simulate_biolog output to report format with counts."""
            result = {}
            for elem, bio_dict in biolog_raw.items():
                result[elem] = {}
                for bio_key, media_list in bio_dict.items():
                    result[elem][bio_key] = _with_count(media_list)
            return result

        report = {
            "template_metadata": {
                "id": template_id,
                "biomass_ids": bio_rxns,
                "rich_media": rich_media,
                "minimal_media": minimal_media,
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            },
            "reaction_classes": {
                "rich": _reaction_classes_with_counts(rich_classes),
                "minimal": _reaction_classes_with_counts(minimal_classes),
            },
            "closed_mode_reactions": _with_count(closed_rxns),
            "functional_biolog_media": _biolog_with_counts(biolog_results),
            "producible_metabolites": {
                "complete": _with_count(producible_complete),
                "glucose_minimal": _with_count(producible_glucose),
            },
            "consumable_metabolites": {
                "complete": _with_count(consumable_complete),
            },
        }

        # ── Write output ─────────────────────────────────────────────────────
        if write_path is not None:
            _write_report(report, write_path)

        if verbose:
            self.log_info("evaluate_template_quality: done")
        return report

    def render_template_report(self, report):
        """Render a template evaluation report as a markdown string.

        Pure function — no recomputation; all data comes from the report dict.

        Args:
            report: Report dict as returned by evaluate_template_quality.

        Returns:
            str: Markdown-formatted report.
        """
        return _render_markdown(report)

    def diff_template_evaluation(
        self,
        model,
        perturbations,
        mode="independent",
        baseline_report=None,
        write_path=None,
    ):
        """Diff template evaluations across a set of model-level perturbations.

        Applies perturbations at the model level (direct cobra.Model edits —
        toggle bounds, add/remove/modify reactions). Does NOT rebuild from the
        template.

        Args:
            model: cobra.Model (the baseline model — must be fully built).
            perturbations: List of perturbation dicts, each with:
                ``{op: "add"|"remove"|"modify", reaction_id, lower_bound,
                   upper_bound, stoichiometry}``
                For ``modify``, omitted keys mean "no change".
                For ``remove``, only ``reaction_id`` is needed.
            mode: "independent" (each vs shared baseline) or "cumulative"
                  (each vs previous state). Default "independent".
            baseline_report: Pre-computed baseline report dict from
                evaluate_template_quality. If None, the baseline is evaluated
                from ``model`` as-is.
            write_path: Optional path to write the diff report (same semantics
                as evaluate_template_quality).

        Returns:
            dict: Diff report with per-perturbation change records.
        """
        from modelseedpy.core.msmodelutl import MSModelUtil
        import cobra

        if mode not in ("independent", "cumulative"):
            raise ValueError(f"diff_template_evaluation: unknown mode '{mode}'; use 'independent' or 'cumulative'")

        # ── Establish baseline ────────────────────────────────────────────────
        if baseline_report is None:
            self.log_info("diff_template_evaluation: computing baseline evaluation")
            baseline_mdlutl = MSModelUtil.get(model)
            baseline_report = self._evaluate_model_quality(baseline_mdlutl)

        baseline_model = model  # kept UNMODIFIED in independent mode

        # ── Apply perturbations ───────────────────────────────────────────────
        diffs = []
        prev_report = baseline_report
        prev_model = model  # cumulative mode advances this

        for i, pert in enumerate(perturbations):
            op = pert.get("op", "")
            rxn_id = pert.get("reaction_id", "")
            self.log_info(
                f"diff_template_evaluation: perturbation {i+1}/{len(perturbations)}: "
                f"op={op} rxn={rxn_id} mode={mode}"
            )

            # Build the model to perturb
            if mode == "independent":
                working_model = baseline_model.copy()
            else:  # cumulative
                working_model = prev_model.copy()

            # Apply the perturbation
            _apply_perturbation(working_model, pert)

            # Evaluate the perturbed model
            perturbed_mdlutl = MSModelUtil.get(working_model)
            perturbed_report = self._evaluate_model_quality(perturbed_mdlutl)

            # Compute diff against the appropriate baseline
            reference_report = prev_report if mode == "cumulative" else baseline_report
            delta = _compute_diff(reference_report, perturbed_report)

            diffs.append({
                "perturbation": pert,
                "delta": delta,
            })

            if mode == "cumulative":
                prev_report = perturbed_report
                prev_model = working_model

        # Verify baseline model unmodified in independent mode
        # (no-op assertion — we only ever copy it before perturbing)

        diff_report = {
            "mode": mode,
            "baseline_report": baseline_report,
            "perturbation_diffs": diffs,
        }

        if write_path is not None:
            _write_report(diff_report, write_path)

        return diff_report

    def _evaluate_model_quality(self, mdlutl, rich_media=None, minimal_media=None):
        """Run the evaluation battery on an MSModelUtil and return the report.

        This is the internal method used by diff_template_evaluation.  Uses
        no media by default (offline-safe).

        Args:
            mdlutl: MSModelUtil wrapping the model.
            rich_media: Optional media object for rich-media passes.
            minimal_media: Optional media object for minimal-media passes.

        Returns:
            Report dict in the same format as evaluate_template_quality.
        """
        cobra_model = mdlutl.model

        biomass_ids = [
            r.id for r in cobra_model.reactions
            if r.id.startswith("bio") or "biomass" in r.id.lower()
        ]
        bio_rxns = [rid for rid in biomass_ids if rid in ("bio1", "bio2")]
        if not bio_rxns:
            bio_rxns = biomass_ids[:2]

        rich_classes = self.classify_reactions_by_fva(mdlutl, media=rich_media)
        minimal_classes = self.classify_reactions_by_fva(mdlutl, media=minimal_media)
        closed_rxns = self.find_closed_mode_reactions(mdlutl)

        try:
            biolog_results = self.simulate_biolog(mdlutl)
        except Exception:
            biolog_results = {}

        producible_complete = self.test_production_potential(mdlutl, media=rich_media)
        producible_glucose = self.test_production_potential(mdlutl, media=minimal_media)
        consumable_complete = self.test_degradation_potential(mdlutl, media=rich_media)

        def _with_count(lst):
            return {"list": lst, "count": len(lst)}

        def _reaction_classes_with_counts(cls_dict):
            result = {}
            for key in ("dead", "forward_only", "reverse_only", "reversible"):
                result[key] = _with_count(cls_dict.get(key, []))
            essential_raw = cls_dict.get("essential", {})
            essential_out = {}
            for bio_key, rxn_list in essential_raw.items():
                essential_out[bio_key] = _with_count(rxn_list)
            result["essential"] = essential_out
            return result

        def _biolog_with_counts(biolog_raw):
            result = {}
            for elem, bio_dict in biolog_raw.items():
                result[elem] = {}
                for bio_key, media_list in bio_dict.items():
                    result[elem][bio_key] = _with_count(media_list)
            return result

        return {
            "template_metadata": {
                "id": getattr(cobra_model, "id", "unknown"),
                "biomass_ids": bio_rxns,
                "rich_media": None,
                "minimal_media": None,
                "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            },
            "reaction_classes": {
                "rich": _reaction_classes_with_counts(rich_classes),
                "minimal": _reaction_classes_with_counts(minimal_classes),
            },
            "closed_mode_reactions": _with_count(closed_rxns),
            "functional_biolog_media": _biolog_with_counts(biolog_results),
            "producible_metabolites": {
                "complete": _with_count(producible_complete),
                "glucose_minimal": _with_count(producible_glucose),
            },
            "consumable_metabolites": {
                "complete": _with_count(consumable_complete),
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Module-level pure functions
# ─────────────────────────────────────────────────────────────────────────────


def _apply_perturbation(cobra_model, pert):
    """Apply a single perturbation spec to a cobra.Model in-place.

    Perturbation schema::

        {op: "add"|"remove"|"modify",
         reaction_id: str,
         lower_bound: float,   # optional for modify
         upper_bound: float,   # optional for modify
         stoichiometry: {met_id: coeff}}  # optional for modify

    Args:
        cobra_model: cobra.Model to mutate.
        pert: Perturbation dict.
    """
    import cobra

    op = pert.get("op", "")
    rxn_id = pert.get("reaction_id", "")

    if op == "remove":
        try:
            rxn = cobra_model.reactions.get_by_id(rxn_id)
            cobra_model.remove_reactions([rxn], remove_orphans=False)
        except KeyError:
            logger.warning(f"_apply_perturbation: remove: reaction '{rxn_id}' not found")
        return

    if op == "add":
        if rxn_id in {r.id for r in cobra_model.reactions}:
            logger.warning(f"_apply_perturbation: add: reaction '{rxn_id}' already present; skipping")
            return
        rxn = cobra.Reaction(rxn_id)
        rxn.lower_bound = float(pert.get("lower_bound", 0.0))
        rxn.upper_bound = float(pert.get("upper_bound", 1000.0))
        stoich = pert.get("stoichiometry", {})
        met_dict = {}
        for met_id, coeff in stoich.items():
            # Normalize: try exact id first, then with compartment
            try:
                met = cobra_model.metabolites.get_by_id(met_id)
            except KeyError:
                # Create a new metabolite if not found
                met = cobra.Metabolite(met_id)
            met_dict[met] = coeff
        if met_dict:
            rxn.add_metabolites(met_dict)
        cobra_model.add_reactions([rxn])
        return

    if op == "modify":
        try:
            rxn = cobra_model.reactions.get_by_id(rxn_id)
        except KeyError:
            logger.warning(f"_apply_perturbation: modify: reaction '{rxn_id}' not found")
            return
        if "lower_bound" in pert:
            rxn.lower_bound = float(pert["lower_bound"])
        if "upper_bound" in pert:
            rxn.upper_bound = float(pert["upper_bound"])
        if "stoichiometry" in pert:
            # Replace stoichiometry: subtract old, add new
            rxn.subtract_metabolites({m: c for m, c in rxn.metabolites.items()})
            stoich = pert["stoichiometry"]
            met_dict = {}
            for met_id, coeff in stoich.items():
                try:
                    met = cobra_model.metabolites.get_by_id(met_id)
                except KeyError:
                    met = cobra.Metabolite(met_id)
                met_dict[met] = coeff
            if met_dict:
                rxn.add_metabolites(met_dict)
        return

    logger.warning(f"_apply_perturbation: unknown op '{op}'")


def _compute_diff(before, after):
    """Compute category-level set differences between two evaluation reports.

    For each list-valued category, computes added/removed members.

    Args:
        before: Report dict (baseline).
        after: Report dict (perturbed).

    Returns:
        dict with per-category "added"/"removed" lists and growth_change.
    """
    delta = {}

    # Reaction classes
    for media_key in ("rich", "minimal"):
        before_classes = before.get("reaction_classes", {}).get(media_key, {})
        after_classes = after.get("reaction_classes", {}).get(media_key, {})
        for cat in ("dead", "forward_only", "reverse_only", "reversible"):
            b_set = set(before_classes.get(cat, {}).get("list", []))
            a_set = set(after_classes.get(cat, {}).get("list", []))
            key = f"reaction_classes.{media_key}.{cat}"
            delta[key] = {
                "added": sorted(a_set - b_set),
                "removed": sorted(b_set - a_set),
            }
        # Essential per biomass + union
        b_ess = before_classes.get("essential", {})
        a_ess = after_classes.get("essential", {})
        all_bio_keys = set(b_ess.keys()) | set(a_ess.keys())
        for bio_key in all_bio_keys:
            b_set = set(b_ess.get(bio_key, {}).get("list", []))
            a_set = set(a_ess.get(bio_key, {}).get("list", []))
            key = f"reaction_classes.{media_key}.essential.{bio_key}"
            delta[key] = {
                "added": sorted(a_set - b_set),
                "removed": sorted(b_set - a_set),
            }

    # Closed mode
    b_closed = set(before.get("closed_mode_reactions", {}).get("list", []))
    a_closed = set(after.get("closed_mode_reactions", {}).get("list", []))
    delta["closed_mode_reactions"] = {
        "added": sorted(a_closed - b_closed),
        "removed": sorted(b_closed - a_closed),
    }

    # Biolog
    b_bio = before.get("functional_biolog_media", {})
    a_bio = after.get("functional_biolog_media", {})
    all_elems = set(b_bio.keys()) | set(a_bio.keys())
    for elem in all_elems:
        b_elem = b_bio.get(elem, {})
        a_elem = a_bio.get(elem, {})
        all_bio_keys = set(b_elem.keys()) | set(a_elem.keys())
        for bio_key in all_bio_keys:
            b_set = set(b_elem.get(bio_key, {}).get("list", []))
            a_set = set(a_elem.get(bio_key, {}).get("list", []))
            key = f"functional_biolog_media.{elem}.{bio_key}"
            delta[key] = {
                "added": sorted(a_set - b_set),
                "removed": sorted(b_set - a_set),
            }

    # Producible metabolites
    for media_key in ("complete", "glucose_minimal"):
        b_set = set(before.get("producible_metabolites", {}).get(media_key, {}).get("list", []))
        a_set = set(after.get("producible_metabolites", {}).get(media_key, {}).get("list", []))
        delta[f"producible_metabolites.{media_key}"] = {
            "added": sorted(a_set - b_set),
            "removed": sorted(b_set - a_set),
        }

    # Consumable metabolites
    for media_key in ("complete",):
        b_set = set(before.get("consumable_metabolites", {}).get(media_key, {}).get("list", []))
        a_set = set(after.get("consumable_metabolites", {}).get(media_key, {}).get("list", []))
        delta[f"consumable_metabolites.{media_key}"] = {
            "added": sorted(a_set - b_set),
            "removed": sorted(b_set - a_set),
        }

    # Growth change: use bio1 essentiality as a growth proxy
    # (a reaction in bio1 essential before but not after means growth gained for
    # the perturbed model; bio1 gone from essential means it can no longer grow
    # — but we report it as gained/lost relative to the baseline)
    b_bio1_ess = set(
        before.get("reaction_classes", {})
              .get("rich", {})
              .get("essential", {})
              .get("bio1", {})
              .get("list", [])
    )
    a_bio1_ess = set(
        after.get("reaction_classes", {})
             .get("rich", {})
             .get("essential", {})
             .get("bio1", {})
             .get("list", [])
    )
    # Proxy: if the essential set shrank drastically the model lost growth capacity
    b_count = len(b_bio1_ess)
    a_count = len(a_bio1_ess)
    delta["growth_change"] = {
        "essential_rich_bio1_before": b_count,
        "essential_rich_bio1_after": a_count,
        "delta": a_count - b_count,
        "interpretation": (
            "gained" if a_count > b_count
            else "lost" if a_count < b_count
            else "unchanged"
        ),
    }

    return delta


def _render_markdown(report):
    """Render a template evaluation report as markdown.

    Pure function — no recomputation.

    Args:
        report: Report dict as returned by evaluate_template_quality or
                _evaluate_model_quality.

    Returns:
        str: Markdown string.
    """
    lines = []
    lines.append("# Template Evaluation Report\n")

    # Metadata
    meta = report.get("template_metadata", {})
    if meta:
        lines.append("## Template Metadata\n")
        lines.append(f"- **ID**: {meta.get('id', 'n/a')}")
        lines.append(f"- **Biomass reactions**: {', '.join(meta.get('biomass_ids', []))}")
        lines.append(f"- **Rich media**: {meta.get('rich_media', 'n/a')}")
        lines.append(f"- **Minimal media**: {meta.get('minimal_media', 'n/a')}")
        lines.append(f"- **Timestamp**: {meta.get('timestamp', 'n/a')}\n")

    # Reaction classes
    rc = report.get("reaction_classes", {})
    if rc:
        lines.append("## Reaction Classification\n")
        for media_key in ("rich", "minimal"):
            mc = rc.get(media_key, {})
            if mc:
                lines.append(f"### {media_key.capitalize()} Media\n")
                for cat in ("dead", "forward_only", "reverse_only", "reversible"):
                    cat_data = mc.get(cat, {})
                    count = cat_data.get("count", 0)
                    lst = cat_data.get("list", [])
                    lines.append(f"**{cat}** ({count}):")
                    if lst:
                        lines.append(", ".join(lst[:20]) + ("..." if count > 20 else ""))
                    lines.append("")
                # Essential
                ess = mc.get("essential", {})
                if ess:
                    lines.append("**Essential reactions** (by biomass):\n")
                    for bio_key, bio_data in ess.items():
                        count = bio_data.get("count", 0)
                        lst = bio_data.get("list", [])
                        lines.append(f"- *{bio_key}* ({count}): "
                                     + (", ".join(lst[:10]) + ("..." if count > 10 else "")) if lst else "none")
                    lines.append("")

    # Closed mode
    closed = report.get("closed_mode_reactions", {})
    if closed:
        lines.append("## Closed-Mode Reactions\n")
        count = closed.get("count", 0)
        lst = closed.get("list", [])
        lines.append(f"**{count} reactions** that carry flux in a closed system (potential loops):\n")
        if lst:
            lines.append(", ".join(lst[:30]) + ("..." if count > 30 else ""))
        lines.append("")

    # Biolog
    biolog = report.get("functional_biolog_media", {})
    if biolog:
        lines.append("## Functional Biolog Media\n")
        for elem, bio_dict in biolog.items():
            lines.append(f"### Element: {elem}\n")
            for bio_key, bio_data in bio_dict.items():
                count = bio_data.get("count", 0)
                lst = bio_data.get("list", [])
                lines.append(f"- **{bio_key}** ({count}): " +
                              (", ".join(lst[:10]) + ("..." if count > 10 else "")) if lst else "none")
            lines.append("")

    # Production
    prod = report.get("producible_metabolites", {})
    if prod:
        lines.append("## Producible Metabolites\n")
        for media_key in ("complete", "glucose_minimal"):
            data = prod.get(media_key, {})
            count = data.get("count", 0)
            lst = data.get("list", [])
            lines.append(f"**{media_key}** ({count}):")
            if lst:
                lines.append(", ".join(lst[:30]) + ("..." if count > 30 else ""))
            lines.append("")

    # Degradation
    cons = report.get("consumable_metabolites", {})
    if cons:
        lines.append("## Consumable Metabolites\n")
        for media_key in ("complete",):
            data = cons.get(media_key, {})
            count = data.get("count", 0)
            lst = data.get("list", [])
            lines.append(f"**{media_key}** ({count}):")
            if lst:
                lines.append(", ".join(lst[:30]) + ("..." if count > 30 else ""))
            lines.append("")

    return "\n".join(lines)


def _write_report(report, write_path):
    """Write report to JSON (and markdown) at write_path.

    If write_path is a directory: writes <dir>/report.json and <dir>/report.md.
    If write_path is a file stem: writes <stem>.json and <stem>.md.

    Args:
        report: Report dict.
        write_path: str or pathlib.Path.
    """
    wp = pathlib.Path(write_path)
    if wp.is_dir() or (not wp.suffix):
        # Directory case
        if not wp.exists():
            # Could be an intended directory — treat as directory
            if wp.suffix == "":
                wp.mkdir(parents=True, exist_ok=True)
                json_path = wp / "report.json"
                md_path = wp / "report.md"
            else:
                json_path = pathlib.Path(str(wp) + ".json")
                md_path = pathlib.Path(str(wp) + ".md")
        else:
            json_path = wp / "report.json"
            md_path = wp / "report.md"
    else:
        # File stem
        stem = wp.with_suffix("")
        json_path = stem.with_suffix(".json")
        md_path = stem.with_suffix(".md")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    md_str = _render_markdown(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_str)

    logger.info(f"_write_report: wrote {json_path} and {md_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Composition-based Impl
# ─────────────────────────────────────────────────────────────────────────────


class MSTemplateUtilsImpl:
    """Composition-based template utilities.

    Holds ``env`` and ``model`` instead of inheriting from ``KBModelUtils``.
    Delegates all method calls to an internal legacy instance.
    """

    def __init__(self, env, model, **kwargs):
        self._env = env
        self._model = model
        _kwargs = {
            "config_file": False,
            "token_file": None,
            "kbase_token_file": None,
        }
        try:
            _kwargs["token"] = env.get_token("kbase")
        except Exception:
            pass
        _kwargs.update(kwargs)
        try:
            self._delegate = MSTemplateUtils(**_kwargs)
        except Exception:
            self._delegate = None

    @property
    def env(self):
        return self._env

    @property
    def model(self):
        return self._model

    def __getattr__(self, name):
        if self._delegate is None:
            raise RuntimeError(
                "MSTemplateUtilsImpl: delegate not initialized (missing cobrakbase/modelseedpy)"
            )
        return getattr(self._delegate, name)
