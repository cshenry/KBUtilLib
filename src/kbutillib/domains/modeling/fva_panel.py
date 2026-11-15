"""FVA panel and essentiality classification — the KBase-free FVA layer.

This is the port of the FVA orchestration a KBase CTS recon job used to reach
through ``genome_annotation_aggregator.model_build.fva_runner`` and
``genome_annotation_aggregator.schema.compute_model_fva_essentiality``.  Nothing
here imports ``genome_annotation_aggregator``, and nothing here is batch-pipeline
specific: no store writes, no parquet shaping, no 21-medium panel assumption.

What it is orchestration OVER
-----------------------------
:meth:`~.ms_fba_utils.MSFBAUtils.run_fva` and
:meth:`~.ms_fba_utils.MSFBAUtils.run_fba` do the solving.  ``run_fva`` is this
repo's deliberate replacement for ``cobra.flux_variability_analysis`` (which is
broken in this stack); this module calls ``run_fva`` ONLY and never reaches for
the cobra function.  Every call into ``MSFBAUtils`` is made with KEYWORD
arguments, because a modelseedpy version string does not identify the code
behind it (fork and upstream both report 0.4.2 with different signatures).

Per-medium model isolation
--------------------------
The cobra model is serialised to JSON ONCE, before any medium is evaluated, and
each medium's FVA runs against a model rebuilt from that snapshot and then
copied.  Both halves matter:

* **Snapshot before the loop.**  The growth gate below calls ``run_fba``, which
  goes through ``configure_fba_formulation`` and MUTATES the model it is handed
  (media bounds, objective constraints).  Taking the snapshot first is what
  keeps every medium's FVA a measurement of the model the CALLER supplied,
  rather than of the model the previous medium's gate left behind.
* **Fresh model per medium.**  ``set_media`` attaches the KBaseMediaPkg package
  in place, and glpk carries basis state across solves.  A per-medium copy keeps
  neither from leaking into the next medium's numbers, or back into the caller's
  ``mdlutl``.

Sequential, and what that costs
-------------------------------
The GAA original ran media in a ``ProcessPoolExecutor`` and gave each one a 120 s
wall-clock timeout, recording :data:`TIMEOUT_SENTINEL` for a medium that blew it
so the batch continued.  This port is SEQUENTIAL and therefore has NO preemptive
timeout: a solve already inside the solver's C code cannot be interrupted from
the calling thread, and a thread-based "timeout" would record a result while the
solve kept running, which is worse than waiting.  The consequence is explicit: a
pathological medium blocks the panel here where it would have been abandoned
there.  :data:`TIMEOUT_SENTINEL` is still exported and still documented, because
consumers already branch on it, but this module never emits it.

The three result shapes a caller must distinguish
-------------------------------------------------
Per medium, :func:`run_fva_panel` returns one of:

* a non-empty ``{rxn_id: {"MIN": float, "MAX": float}}`` — FVA ran;
* ``{}`` — the medium did not grow, so FVA was never attempted, or the medium
  raised;
* ``{"timeout": True}`` — never produced by this module (see above).

None of these is "every reaction measured at zero", and a consumer that wants
rows only for computed results should test for a per-reaction shape rather than
for truthiness alone.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Biomass flux at or below which a medium counts as not growing, so FVA is
#: skipped for it.  Ported verbatim from GAA's ``fva_runner.GROWTH_THRESHOLD``.
GROWTH_THRESHOLD: float = 1e-9

#: Default fraction of the optimal objective used as the FVA lower bound.  1.0
#: means parsimonious FVA — the whole panel is measured at the optimum.  This is
#: GAA's ``DEFAULT_FRACTION_OF_OPTIMUM`` and NOT ``run_fva``'s own 0.9 default;
#: callers that left the choice to GAA keep the same number by leaving it here.
DEFAULT_FRACTION_OF_OPTIMUM: float = 1.0

#: What GAA recorded for a medium whose FVA exceeded its wall-clock budget.
#: Retained so consumers branching on it keep compiling and keep working; the
#: sequential path in this module never returns it (see the module docstring).
TIMEOUT_SENTINEL: Dict[str, Any] = {"timeout": True}

#: Essentiality classification tolerance: absolute floor, and the relative term
#: scaled by the larger flux endpoint.  ``eps = max(_EPS_ABS, _EPS_REL * max(
#: |MIN|, |MAX|))`` — GAA's ``model_build/outputs.py`` rule, unchanged, so a
#: reaction's class does not move when the classifier does.
_EPS_ABS: float = 1e-9
_EPS_REL: float = 1e-6

CLASS_BLOCKED = "blocked"
CLASS_ESSENTIAL_FORWARD = "essential-forward"
CLASS_ESSENTIAL_REVERSE = "essential-reverse"
CLASS_FUNCTIONAL = "functional"

#: The closed set of classes :func:`classify_fva_essentiality` can return.
VALID_ESSENTIALITY_CLASSES: frozenset = frozenset(
    {
        CLASS_BLOCKED,
        CLASS_ESSENTIAL_FORWARD,
        CLASS_ESSENTIAL_REVERSE,
        CLASS_FUNCTIONAL,
    }
)


@dataclass
class MediaPanel:
    """The media a reconstruction gap-fills and runs FVA on.

    Field-for-field the shape GAA's ``model_build.dataclasses.MediaPanel``
    carried, so a caller that was building one of those builds one of these
    instead and changes nothing else.  It is a plain dataclass: constructing it
    requires no KBase call, no biochem database and no solver.

    ``MSMedia`` objects are typed ``Any`` deliberately — this module must be
    importable without modelseedpy installed, which is what lets a caller
    construct and inspect a panel in an environment that cannot solve.

    Attributes:
        complete: The complete/rich ``MSMedia`` object, as a bare medium.
        gapfill_media: ``(media_id, MSMedia)`` pairs to gap-fill on.
        fva_media: ``(media_id, MSMedia)`` pairs to run FVA on.  These are what
            :func:`run_fva_panel` iterates; ``complete`` and ``gapfill_media``
            are carried for the caller's own use and are never read here.

    The ids are the CALLER's.  ``MSMedia.from_dict`` names every medium it
    builds ``"media"``, so the id in each pair is the only thing that
    distinguishes one medium from another in the returned mapping — this module
    renames nothing and dedupes nothing.
    """

    complete: Any
    gapfill_media: List[Tuple[str, Any]]
    fva_media: List[Tuple[str, Any]]


def classify_fva_essentiality(fva_min: float, fva_max: float) -> str:
    """Classify one reaction's essentiality from its FVA flux range.

    The rule, and its tolerance, are GAA's ``_classify_essentiality`` verbatim:
    ``eps = max(1e-9, 1e-6 * max(|MIN|, |MAX|))``, then

    * both endpoints within ``eps`` of zero → ``"blocked"``;
    * both endpoints above ``+eps`` → ``"essential-forward"`` (the reaction
      cannot carry zero flux, and carries it forward);
    * both endpoints below ``-eps`` → ``"essential-reverse"``;
    * otherwise → ``"functional"`` (the range spans zero, so the reaction can be
      switched off).

    Args:
        fva_min: FVA minimum flux.
        fva_max: FVA maximum flux.

    Returns:
        One of :data:`VALID_ESSENTIALITY_CLASSES`.
    """
    abs_min = abs(fva_min)
    abs_max = abs(fva_max)
    eps = max(_EPS_ABS, _EPS_REL * max(abs_min, abs_max))

    if abs_min < eps and abs_max < eps:
        return CLASS_BLOCKED
    if fva_min > eps and fva_max > eps:
        return CLASS_ESSENTIAL_FORWARD
    if fva_min < -eps and fva_max < -eps:
        return CLASS_ESSENTIAL_REVERSE
    return CLASS_FUNCTIONAL


def compute_fva_essentiality(fva_min: float, fva_max: float) -> Dict[str, Any]:
    """Derive ``(essentiality_class, is_essential, is_blocked)`` from FVA min/max.

    The replacement for GAA's ``schema.compute_model_fva_essentiality``, same
    keys and same values.  All three outputs come from the single
    :func:`classify_fva_essentiality` call, so the two booleans cannot drift
    from the class or from each other, and the forward/reverse distinction
    survives in ``essentiality_class`` rather than being collapsed into
    ``is_essential``.

    Args:
        fva_min: FVA minimum flux.
        fva_max: FVA maximum flux.

    Returns:
        ``{"essentiality_class": str, "is_essential": bool, "is_blocked": bool}``.
    """
    essentiality_class = classify_fva_essentiality(fva_min, fva_max)
    return {
        "essentiality_class": essentiality_class,
        "is_essential": essentiality_class
        in (CLASS_ESSENTIAL_FORWARD, CLASS_ESSENTIAL_REVERSE),
        "is_blocked": essentiality_class == CLASS_BLOCKED,
    }


def run_fva_panel(
    mdlutl: Any,
    panel: "MediaPanel",
    *,
    fraction_of_optimum: float = DEFAULT_FRACTION_OF_OPTIMUM,
    fba: Optional[Any] = None,
    growth_out: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Run FVA on every medium in ``panel.fva_media``, growth-gated, sequentially.

    For each medium, in the order supplied: measure biomass flux on it; if that
    is at or below :data:`GROWTH_THRESHOLD`, record ``{}`` and move on without
    attempting FVA; otherwise run FVA against a model rebuilt from the
    pre-loop snapshot (see the module docstring on why both the snapshot and
    the per-medium copy are needed).

    Args:
        mdlutl: ``MSModelUtil`` wrapping the built ``cobra.Model``.  Its model
            is read for the snapshot and mutated by the growth gate; the FVA
            passes never touch it.
        panel: The :class:`MediaPanel` whose ``fva_media`` to iterate.  A panel
            with empty ``fva_media`` is not an error — it yields ``{}``.
        fraction_of_optimum: FVA lower bound as a fraction of the optimal
            objective.  Defaults to :data:`DEFAULT_FRACTION_OF_OPTIMUM` (1.0),
            not to ``run_fva``'s own 0.9.
        fba: An ``MSFBAUtils``-like object used for both the growth gate and the
            FVA passes.  ``None`` builds a KBase-free one via :func:`_make_fba`.
        growth_out: Optional caller dict, populated IN PLACE with the biomass
            flux measured for EVERY medium in ``panel.fva_media`` — growing and
            non-growing alike — keyed by media id.

            This exists because the gate already computes the one number
            callers need to persist and the return value cannot carry it: the
            return is keyed by reaction, and a non-growing medium's ``{}`` has
            nowhere to put a number.  Callers that sourced growth from
            gap-fill records instead got 0.0 both for a medium that did not
            grow AND for a medium that grew so well it needed no gap-filling —
            a healthy model reporting no growth.  Pass a dict here to get the
            measurement.

            Every medium in ``panel.fva_media`` is given an entry, so a missing
            key means the medium was never evaluated, which is distinguishable
            from a measured 0.0.  Note that a medium id appearing twice in
            ``fva_media`` can hold only its LAST measurement here, same as in
            the return value.

    Returns:
        ``{media_id: {rxn_id: {"MIN": float, "MAX": float}}}``, with ``{}`` for
        a medium that did not grow or whose FVA raised.  See the module
        docstring for the full set of per-medium shapes.
    """
    cobra_json = _cobra_json_module()

    if fba is None:
        fba = _make_fba()

    # ONE snapshot, taken before the first gate mutates anything.
    model_json: str = json.dumps(cobra_json.model_to_dict(mdlutl.model))

    results: Dict[str, Dict[str, Dict[str, float]]] = {}

    for media_id, media_obj in panel.fva_media:
        growth = _compute_growth(fba=fba, mdlutl=mdlutl, media_obj=media_obj)
        if growth_out is not None:
            # Recorded for every medium, BEFORE the gate — a non-growing
            # medium's 0.0 is a measurement, not a missing value.
            growth_out[media_id] = growth

        if growth <= GROWTH_THRESHOLD:
            logger.info(
                "No growth on %s (biomass=%.3e <= %.0e) — skipping FVA.",
                media_id,
                growth,
                GROWTH_THRESHOLD,
            )
            results[media_id] = {}
            continue

        logger.info("Growth on %s (biomass=%.3e) — running FVA.", media_id, growth)
        try:
            results[media_id] = _fva_one_medium(
                fba=fba,
                cobra_json=cobra_json,
                model_json=model_json,
                media_obj=media_obj,
                media_id=media_id,
                fraction_of_optimum=fraction_of_optimum,
            )
        except Exception as exc:  # noqa: BLE001 — one medium must not kill the panel
            # Deliberately the same ``{}`` a non-growing medium gets: neither is
            # a computed per-reaction result, and the log line is where the
            # difference between them is recorded.
            logger.error("FVA failed on medium %s: %s", media_id, exc)
            results[media_id] = {}

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cobra_json_module() -> Any:
    """``cobra.io.json``, with a message naming the extra when it is absent.

    Imported lazily, and through a function, so that importing this module —
    and so constructing and inspecting a :class:`MediaPanel` — never requires
    cobra.
    """
    try:
        import cobra.io.json as cobra_json  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(
            "cobra is required to run an FVA panel. Install the model extra: "
            "pip install kbutillib[model]"
        ) from exc
    return cobra_json


def _make_fba(**kwargs: Any) -> Any:
    """Construct an ``MSFBAUtils`` that makes no KBase call.

    ``config_file=False`` and the two ``None`` token paths are what keep
    construction offline — this layer must work inside a CTS job with no token
    and no KBase reachability.

    No dependency-manager monkeypatch happens here.  GAA's ``_make_fba`` patched
    ``SharedEnvUtils.__init__`` to mirror the global dependency map into each
    instance, because it had no other way to make the ModelSEED biochem path
    visible to a freshly constructed utility.  In this repo that is
    ``initialize_reconstruction_environment``'s job (it resolves
    ModelSEEDDatabase through ``~/.kbutillib/dependencies.yaml`` rather than
    modelseedpy's non-existent ``/deps/ModelSEEDDatabase`` default), so the
    patch would be duplicating a supported path with a monkeypatch.  Callers
    that need the biochem database resolved should initialise the environment
    once, up front, exactly as the reconstruction entry points do.
    """
    try:
        from .ms_fba_utils import MSFBAUtils
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(
            "the kbutillib modeling stack is required to run an FVA panel. "
            "Install the model extra: pip install kbutillib[model]"
        ) from exc

    kwargs.setdefault("config_file", False)
    kwargs.setdefault("token_file", None)
    kwargs.setdefault("kbase_token_file", None)
    return MSFBAUtils(**kwargs)


def _compute_growth(*, fba: Any, mdlutl: Any, media_obj: Any) -> float:
    """Biomass flux on ``media_obj``, or 0.0 if it could not be measured.

    ``run_pfba=False``: the gate needs the optimum only, and a parsimonious
    flux distribution costs a second solve to produce a number nothing reads.

    Returning 0.0 for a raised solve, a ``None`` solution and an uncoercible
    objective value is deliberate — all three mean "this medium does not get
    FVA", and the gate's job is to decide that, not to fail the panel.  The
    exception is logged at debug rather than swallowed silently.

    This mutates ``mdlutl``: ``run_fba`` goes through
    ``configure_fba_formulation``, which applies media bounds and objective
    constraints in place.  That is why the FVA passes work from a snapshot
    taken before any gate ran, and never from ``mdlutl`` itself.
    """
    try:
        solution = fba.run_fba(model=mdlutl, media=media_obj, run_pfba=False)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        logger.debug("Growth check raised on medium: %s", exc)
        return 0.0
    if solution is None:
        return 0.0
    objective_value = getattr(solution, "objective_value", None)
    if objective_value is None:
        return 0.0
    try:
        return float(objective_value)
    except (TypeError, ValueError):
        return 0.0


def _fva_one_medium(
    *,
    fba: Any,
    cobra_json: Any,
    model_json: str,
    media_obj: Any,
    media_id: str,
    fraction_of_optimum: float,
) -> Dict[str, Dict[str, float]]:
    """FVA on one medium, against a model rebuilt from ``model_json``.

    Rebuild-then-copy, both steps: the rebuild gives this medium the model the
    caller supplied rather than whatever the previous medium's growth gate left
    behind, and the copy keeps ``set_media``'s in-place package attachment and
    glpk's basis state out of the next medium's numbers.

    Returns:
        ``{rxn_id: {"MIN": float, "MAX": float}}``, floats.  A row whose
        endpoints are missing or uncoercible is DROPPED with a debug log rather
        than carried as ``None``: a consumer reading ``MIN``/``MAX`` off every
        row should not have to defend against a row that has neither.
    """
    from modelseedpy.core.msmodelutl import MSModelUtil  # type: ignore[import]

    model = cobra_json.model_from_dict(json.loads(model_json))
    local_mdlutl = MSModelUtil.get(model.copy())

    raw = fba.run_fva(
        model=local_mdlutl,
        media=media_obj,
        fraction_of_optimum=fraction_of_optimum,
    )

    result: Dict[str, Dict[str, float]] = {}
    if not isinstance(raw, dict):
        logger.debug(
            "run_fva returned %s rather than a dict on %s", type(raw).__name__, media_id
        )
        return result
    for rxn_id, val in raw.items():
        if not isinstance(val, dict):
            continue
        try:
            result[rxn_id] = {"MIN": float(val["MIN"]), "MAX": float(val["MAX"])}
        except (KeyError, TypeError, ValueError):
            logger.debug(
                "Dropping FVA row for %s on %s (uncoercible: %r)", rxn_id, media_id, val
            )
    return result
