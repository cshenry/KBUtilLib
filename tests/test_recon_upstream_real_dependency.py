"""Real-dependency smoke measurements: MSReconstructionUtils vs upstream ModelSEEDpy.

This module is a MEASUREMENT harness, not a unit test suite. It exists to answer
the questions PRD ``kbdl-recon-thin-layer-v1`` needs answered before the light,
KBase-free reconstruction layer can be designed:

* does :meth:`MSReconstructionUtils.build_metabolic_model` work at all against
  upstream ModelSEEDpy?  When this harness was written it did not, on two
  counts: ``ModelSEEDBiochem.get(path=...)`` and a POSITIONAL
  ``builder.build(base_model, "0", False, False)`` whose second argument
  upstream binds to ``gapfill_media``.  Both are now fixed (kbu-recon-env and
  kbu-recon-build), so ``test_a1`` asserts it RUNS and the binding table it
  records is kept as the reason the layer must never call ``build()``;
* what ``MSBuilder.build_base_model`` actually returns, and which of exchanges /
  biomass / objective / ATPM are on it;
* which LP backend carries ATP correction to completion;
* whether ``ModelSEEDBiochem`` resolves through KBUtilLib's dependency manager
  rather than through modelseedpy's ``config.cfg`` default (KBDL issue #41);
* whether ``MSGapfill.run_gapfilling`` works with and without ``prefilter``;
* which genome-classifier artifact upstream loads;
* whether KBUtilLib's roleless-path compensations change a ROLE-BASED draft;
* whether exchanges precede ATP correction (#28) and whether spontaneous /
  universal template reactions reach the draft (#29).

Fixtures cannot stand in for any of this: the whole point is the behaviour of the
real installed ModelSEEDpy. The tests therefore require a real stack, and they
**fail rather than skip** when ``KBU_REQUIRE_MODEL_STACK=1`` is set and the stack
is missing -- a silent skip is how an unmeasured environment gets mistaken for a
measured one.

Running it::

    KBU_REQUIRE_MODEL_STACK=1 \
    KBU_RECON_SMOKE_REPORT=/path/to/report.json \
    /scratch/chenry/venvs/kbdl-recon-scratch-smoke/bin/python -m pytest \
        tests/test_recon_upstream_real_dependency.py -v -s

Without ``KBU_REQUIRE_MODEL_STACK=1`` the module skips, so it is inert in the
ordinary suite (which runs in venvs that have no ModelSEED database).
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

#: Set to ``1`` to demand the real stack. When set and the stack is missing the
#: module raises at import, which pytest reports as a collection ERROR -- i.e. a
#: failure. When unset the module skips.
REQUIRE_ENV_VAR = "KBU_REQUIRE_MODEL_STACK"

#: Optional path to dump every measurement as JSON.
REPORT_ENV_VAR = "KBU_RECON_SMOKE_REPORT"

_REQUIRED = os.environ.get(REQUIRE_ENV_VAR) == "1"

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "model"
    / "haemophilus_influenzae_71421_8_rast.json.gz"
)

#: Every measurement this module takes, keyed by the task's sub-question letter.
MEASUREMENTS: dict[str, Any] = {}


def _unavailable(reason: str) -> None:
    """Fail when the stack was demanded, skip when it was not.

    Raising (rather than calling ``pytest.fail``) at module scope surfaces as a
    pytest collection ERROR, which is a non-passing outcome -- the point being that
    an environment nobody measured must never read as a pass.
    """
    if _REQUIRED:
        raise RuntimeError(
            f"{REQUIRE_ENV_VAR}=1 demands the real model stack, but: {reason}"
        )
    pytest.skip(reason, allow_module_level=True)


if not _REQUIRED:
    # Inert in the ordinary suite. Opting in is mandatory rather than merely
    # available: these measurements only mean anything against the pinned upstream
    # ModelSEEDpy in the scratch venv, and against the fork they would report
    # confident nonsense.
    pytest.skip(
        f"real-dependency recon measurements are opt-in; set {REQUIRE_ENV_VAR}=1",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Stack preparation
#
# Reproduces the essential setup that GAA's
# ``genome_annotation_aggregator.model_build.recon_driver.make_recon`` performs,
# WITHOUT importing GAA (which the PRD forbids as a dependency of the new code).
#
# make_recon does two things that matter, and only the second one actually works:
#
#   1. It resolves the global dependency manager and calls
#      ``initialize_dependencies()``.
#   2. It monkeypatches ``SharedEnvUtils.__init__`` to mirror the global
#      ``dependency_paths`` into each per-instance ``DependencyManager``.
#
# Step 2 is load-bearing. ``SharedEnvUtils.__init__`` constructs
# ``DependencyManager(auto_init=False)`` (core/shared_env_utils.py:106), so every
# instance's ``dependency_paths`` is empty and ``self.get_data_path(...)`` returns
# ``None``. Without the mirror, ``KBAnnotationUtils.__init__`` falls through to its
# sibling-directory fallback and reads ``FilteredReactions.csv`` from a path that
# does not exist, so ``MSReconstructionUtils()`` cannot even be constructed.
#
# We deliberately do NOT use the user's ``~/.kbutillib/dependencies.yaml`` verbatim.
# ``DependencyManager.initialize_dependencies`` does
# ``sys.path.insert(0, dep_path)`` for EVERY entry (core/dependency_manager.py:141),
# and that file carries a ``modelseedpy`` entry pointing at the FORK checkout. Using
# it would silently shadow the pip-installed upstream ModelSEEDpy with the fork and
# invalidate every measurement below. We therefore build a config holding only the
# two dependencies this harness needs -- ``ModelSEEDDatabase`` and
# ``cb_annotation_ontology_api`` -- taking their paths from the user's file.
# ---------------------------------------------------------------------------

HARNESS_DEPENDENCIES = ("ModelSEEDDatabase", "cb_annotation_ontology_api")


def _prepare_stack() -> dict[str, Any]:
    """Resolve dependencies and wire them into KBUtilLib. Returns a provenance dict."""
    import yaml

    from kbutillib.core import shared_env_utils as _sev
    from kbutillib.core.dependency_manager import (
        DEFAULT_DEPENDENCIES_FILE,
        DependencyManager,
    )

    user_config = Path(DEFAULT_DEPENDENCIES_FILE).expanduser()
    if not user_config.is_file():
        _unavailable(f"dependency config absent: {user_config}")

    raw = yaml.safe_load(user_config.read_text()) or {}
    declared = raw.get("dependencies", {})

    subset = {
        name: declared[name] for name in HARNESS_DEPENDENCIES if name in declared
    }
    missing = [name for name in HARNESS_DEPENDENCIES if name not in subset]
    if missing:
        _unavailable(f"{user_config} declares no entry for {missing}")

    # A config holding only our two dependencies, so no fork checkout reaches sys.path.
    harness_config = Path(os.environ.get("TMPDIR", "/tmp")) / (
        "kbu_recon_smoke_dependencies.yaml"
    )
    harness_config.write_text(yaml.safe_dump({"dependencies": subset}))

    dm = DependencyManager(config_path=harness_config, auto_init=False)
    dm.initialize_dependencies()

    unresolved = [n for n in HARNESS_DEPENDENCIES if dm.get_data_path(n) is None]
    if unresolved:
        _unavailable(f"dependency manager could not resolve {unresolved}")

    # Mirror into every SharedEnvUtils instance (make_recon step 2).
    if not getattr(_sev.SharedEnvUtils, "_smoke_patched", False):
        _orig_init = _sev.SharedEnvUtils.__init__

        def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            _orig_init(self, *args, **kwargs)
            for name, path in dm.dependency_paths.items():
                self.dependency_manager.dependency_paths[name] = path

        _sev.SharedEnvUtils.__init__ = _patched_init  # type: ignore[assignment]
        _sev.SharedEnvUtils._smoke_patched = True  # type: ignore[attr-defined]

    return {
        "user_dependency_config": str(user_config),
        "user_dependency_config_declares": sorted(declared),
        "harness_dependency_config": str(harness_config),
        "harness_dependencies_resolved": {
            name: str(dm.get_data_path(name)) for name in HARNESS_DEPENDENCIES
        },
        "modelseedpy_entry_excluded_from_sys_path": "modelseedpy" in declared,
    }


try:
    import cobra  # noqa: E402
    import modelseedpy  # noqa: E402
    from modelseedpy import MSBuilder, MSGapfill, MSGenome  # noqa: E402
    from modelseedpy.core.msgenome import MSFeature  # noqa: E402
    from modelseedpy.core.msmedia import MSMedia  # noqa: E402
    from modelseedpy.core.mstemplate import MSTemplateBuilder  # noqa: E402
    from modelseedpy.helpers import get_classifier, get_template  # noqa: E402
except ImportError as exc:  # pragma: no cover - environment dependent
    _unavailable(f"cobra/modelseedpy not importable: {exc}")

if not FIXTURE.is_file():  # pragma: no cover - environment dependent
    _unavailable(f"genome fixture absent: {FIXTURE}")

PROVENANCE = _prepare_stack()


def _solver_backends() -> dict[str, Any]:
    """Which LP backends optlang reports, and which one cobra defaults to."""
    import optlang

    return {
        "optlang_available": dict(optlang.available_solvers),
        "cobra_default_interface": cobra.Configuration().solver.__name__,
    }


def _real_solve(backend: str) -> bool:
    """Confirm a backend by performing a real solve, never by assuming (#35)."""
    try:
        m = cobra.Model("probe")
        met = cobra.Metabolite("a_c")
        src = cobra.Reaction("SRC", lower_bound=0, upper_bound=10)
        src.add_metabolites({met: 1})
        snk = cobra.Reaction("SNK", lower_bound=0, upper_bound=1000)
        snk.add_metabolites({met: -1})
        m.add_reactions([src, snk])
        m.objective = "SNK"
        m.solver = backend
        sol = m.optimize()
        return sol.status == "optimal" and abs(sol.objective_value - 10.0) < 1e-6
    except Exception:  # noqa: BLE001
        return False


#: Backends that actually solve here, best first. Measured, not assumed.
WORKING_BACKENDS = [b for b in ("gurobi", "glpk") if _real_solve(b)]


MEASUREMENTS["environment"] = {
    "modelseedpy_version_string": modelseedpy.__version__,
    "modelseedpy_file": modelseedpy.__file__,
    "cobra_version": cobra.__version__,
    "solvers": _solver_backends(),
    "backends_verified_by_real_solve": WORKING_BACKENDS,
    "dependency_provenance": PROVENANCE,
}

if not WORKING_BACKENDS:
    _unavailable(
        "no LP backend completed a real solve; per #35 this is itself the result "
        "and it stops the chain"
    )


def _write_report() -> None:
    target = os.environ.get(REPORT_ENV_VAR)
    if target:
        Path(target).write_text(json.dumps(MEASUREMENTS, indent=2, default=str))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def genome() -> MSGenome:
    """Haemophilus influenzae Rd KW20, real RASTtk role strings under ``RAST``.

    The fixture carries no protein sequences on purpose: every consumer on the
    role-based path (``MSBuilder`` with ``annotate_with_rast=False`` and
    ``MSGenomeClassifier.classify``) reads ``feature.ontology_terms["RAST"]`` only.
    """
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)

    g = MSGenome()
    features = []
    for fid, roles in doc["features"].items():
        feature = MSFeature(fid, "", description=roles[0])
        for role in roles:
            feature.add_ontology_term("RAST", role)
        features.append(feature)
    g.add_features(features)
    g.id = doc["genome_id"]
    g.scientific_name = doc["scientific_name"]
    return g


@pytest.fixture(scope="module")
def gs_template():
    """Native ModelSEED gram-negative template as an MSTemplate."""
    return MSTemplateBuilder.from_dict(get_template("template_gram_neg")).build()


@pytest.fixture(scope="module")
def core_template():
    """Native ModelSEED core template as an MSTemplate."""
    return MSTemplateBuilder.from_dict(get_template("template_core")).build()


@pytest.fixture(scope="module")
def recon_construction() -> dict[str, Any]:
    """Attempt to construct MSReconstructionUtils; return the OUTCOME, not the object.

    Construction is itself one of the measurements, so a failure here must be
    recorded rather than reported as a fixture error that hides every later
    question. ``modelseed_db_path`` is passed explicitly, as GAA's ``make_recon``
    docstring says it should be (its code does not actually do so -- see the work
    record), so no sibling-directory fallback can be blamed for the result.
    """
    from kbutillib.domains.modeling.ms_reconstruction_utils import (
        MSReconstructionUtils,
    )

    db_path = PROVENANCE["harness_dependencies_resolved"]["ModelSEEDDatabase"]
    outcome: dict[str, Any] = {
        "modelseed_db_path_supplied": db_path,
        "instance": None,
        "raised": None,
    }
    try:
        outcome["instance"] = MSReconstructionUtils(modelseed_db_path=db_path)
    except BaseException as exc:  # noqa: BLE001 - recording, not handling
        import traceback

        outcome["raised"] = {
            "type": type(exc).__name__,
            "message": str(exc)[:2000],
            "origin": traceback.format_tb(exc.__traceback__)[-1].strip(),
        }
    return outcome


def _draft(genome, template, **kwargs):
    """A role-based draft via ``build_base_model``, keyword arguments only."""
    builder = MSBuilder(genome, template)
    model = builder.build_base_model(
        model_or_id=cobra.Model("draft"),
        index="0",
        allow_all_non_grp_reactions=False,
        annotate_with_rast=False,
        **kwargs,
    )
    return builder, model


def _profile(model) -> dict[str, Any]:
    """Structural census of a cobra model."""
    ids = {r.id for r in model.reactions}
    return {
        "reactions": len(ids),
        "metabolites": len(model.metabolites),
        "genes": len(model.genes),
        "groups": len(model.groups),
        "exchanges": sorted(i for i in ids if i.startswith("EX_")),
        "biomass": sorted(i for i in ids if i.startswith("bio")),
        "drains": sorted(i for i in ids if i.startswith(("DM_", "SK_"))),
        "atpm": sorted(i for i in ids if i.startswith("ATPM")),
        "objective": str(model.objective.expression),
        "reaction_ids": ids,
    }


def _minimal_glucose_oxygen_media() -> MSMedia:
    """Glucose/O2 aerobic minimal medium, ModelSEED compound ids."""
    return MSMedia.from_dict(
        {
            "cpd00027": 10.0,  # D-Glucose
            "cpd00007": 20.0,  # O2
            "cpd00001": 100.0,  # H2O
            "cpd00067": 100.0,  # H+
            "cpd00009": 100.0,  # Phosphate
            "cpd00013": 100.0,  # NH3
            "cpd00048": 100.0,  # Sulfate
            "cpd00205": 100.0,  # K+
            "cpd00254": 100.0,  # Mg
            "cpd00971": 100.0,  # Na+
            "cpd00063": 100.0,  # Ca2+
            "cpd00099": 100.0,  # Cl-
            "cpd00030": 100.0,  # Mn2+
            "cpd00034": 100.0,  # Zn2+
            "cpd00058": 100.0,  # Cu2+
            "cpd00149": 100.0,  # Co2+
            "cpd10515": 100.0,  # Fe2+
            "cpd10516": 100.0,  # Fe3+
            "cpd00531": 100.0,  # Hg2+
            "cpd00264": 100.0,  # Spermidine
        }
    )


# ---------------------------------------------------------------------------
# (a) Does build_metabolic_model work at all against upstream?
# ---------------------------------------------------------------------------


@pytest.mark.kbu_model
@pytest.mark.slow
def test_a1_build_metabolic_model_against_upstream(
    recon_construction, genome, gs_template, core_template
):
    """Does ``build_metabolic_model`` work at all against upstream? Record it.

    Two independent blockers, measured separately because they fail at different
    depths:

    1. ``MSReconstructionUtils`` cannot be CONSTRUCTED. ``MSBiochemUtils.__init__``
       eagerly calls ``_ensure_database_available`` (domains/biochem/
       ms_biochem_utils.py:95), which calls ``ModelSEEDBiochem.get(path=...)`` (:394).
       Upstream's signature is ``get(create_if_missing=True)`` -- no ``path``
       parameter -- so this raises ``TypeError`` before any reconstruction code runs.

    2. Even with a constructed instance, ``build_metabolic_model`` calls
       ``builder.build(base_model, "0", False, False)`` POSITIONALLY
       (domains/modeling/ms_reconstruction_utils.py:373). Upstream's signature is
       ``build(model_or_id, gapfill_media, index="0", allow_all_non_grp_reactions=False,
       annotate_with_rast=True, ...)``, so ``"0"`` binds to ``gapfill_media``, the
       first ``False`` to ``index``, the second to ``allow_all_non_grp_reactions``,
       and ``annotate_with_rast`` keeps its default ``True`` (a live RAST call).
       Upstream ``build()`` then runs ATP correction and gap-filling itself and
       returns a 4-tuple, which ``build_metabolic_model`` assigns to a single ``mdl``
       before doing ``mdl.genome = genome`` on the tuple.

    Blocker 1 is reached first, so blocker 2 is established by signature binding
    rather than by execution -- which is a stronger statement anyway, since it does
    not depend on a network RAST call.
    """
    import inspect

    sig = inspect.signature(MSBuilder.build)
    bound = sig.bind(MSBuilder(genome, gs_template), cobra.Model("x"), "0", False, False)
    bound.apply_defaults()

    outcome: dict[str, Any] = {
        "construction": {
            "succeeded": recon_construction["instance"] is not None,
            "raised": recon_construction["raised"],
            "modelseed_db_path_supplied": recon_construction[
                "modelseed_db_path_supplied"
            ],
        },
        "call_site": "src/kbutillib/domains/modeling/ms_reconstruction_utils.py:373",
        "call_as_written": 'builder.build(base_model, "0", False, False)',
        "upstream_build_signature": str(sig),
        "upstream_build_param_binding": {
            k: repr(v) for k, v in bound.arguments.items() if k != "self"
        },
        "upstream_build_returns": (
            "4-tuple (model_gapfilled, atp_correction, tests, gap_sol); "
            "build_metabolic_model assigns it to a single `mdl` and then sets "
            "`mdl.genome`"
        ),
        "build_metabolic_model_invoked": False,
        "raised": None,
    }

    if recon_construction["instance"] is not None:
        try:
            outcome["build_metabolic_model_invoked"] = True
            recon_construction["instance"].build_metabolic_model(
                genome=genome,
                genome_classifier=None,
                gs_template="gn",
                core_template=core_template,
                gs_template_obj=gs_template,
                atp_safe=False,
                load_default_medias=False,
            )
        except BaseException as exc:  # noqa: BLE001 - recording, not handling
            outcome["raised"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}

    MEASUREMENTS["a1_build_metabolic_model"] = outcome
    _write_report()

    # Blocker 2, by binding: "0" is gapfill_media, not index.
    assert outcome["upstream_build_param_binding"]["gapfill_media"] == "'0'"
    assert outcome["upstream_build_param_binding"]["index"] == "False"
    assert outcome["upstream_build_param_binding"]["annotate_with_rast"] == "True"

    # BOTH blockers are now FIXED, so this records the repair rather than the
    # defect. Blocker 1 went in kbu-recon-env (task-39488b88): ModelSEEDBiochem
    # is loaded through a signature-dispatching helper instead of
    # ``get(path=...)``. Blocker 2 went in kbu-recon-build (task-ec0feb19):
    # ``build_metabolic_model`` calls ``MSBuilder.build_base_model`` with keyword
    # arguments and never calls ``build()`` at all.
    #
    # The ``upstream_build_param_binding`` assertions above are unaffected and
    # are the durable half of this measurement: they are facts about upstream's
    # ``build()``, which is exactly why the layer must not call it.
    assert outcome["construction"]["succeeded"], (
        "MSReconstructionUtils no longer constructs against upstream "
        "ModelSEEDpy; kbu-recon-env's biochemistry and msfba fixes have "
        f"regressed. Raised: {outcome['construction']['raised']}"
    )
    assert outcome["raised"] is None, (
        "build_metabolic_model raised against upstream ModelSEEDpy. The "
        "positional builder.build call was replaced by a keyword "
        "build_base_model call in kbu-recon-build, so this is a regression "
        f"rather than the original blocker. Raised: {outcome['raised']}"
    )


@pytest.mark.kbu_model
@pytest.mark.slow
def test_a2_build_base_model_shape(genome, gs_template):
    """``build_base_model`` signature, return shape, and what is on the result."""
    import inspect

    builder, model = _draft(genome, gs_template)
    profile = _profile(model)

    MEASUREMENTS["a2_build_base_model"] = {
        "signature": str(inspect.signature(MSBuilder.build_base_model)),
        "accepts_match_role_aliases": "match_role_aliases"
        in inspect.signature(MSBuilder.build_base_model).parameters,
        "accepts_allow_incomplete_complexes": "allow_incomplete_complexes"
        in inspect.signature(MSBuilder.build_base_model).parameters,
        "generate_reaction_complex_sets_signature": str(
            inspect.signature(MSBuilder.generate_reaction_complex_sets)
        ),
        "return_type": f"{type(model).__module__}.{type(model).__name__}",
        "returns_tuple": isinstance(model, tuple),
        "reactions": profile["reactions"],
        "metabolites": profile["metabolites"],
        "genes": profile["genes"],
        "groups": profile["groups"],
        "n_exchanges": len(profile["exchanges"]),
        "biomass": profile["biomass"],
        "drains_n": len(profile["drains"]),
        "atpm": profile["atpm"],
        "objective": profile["objective"],
    }
    _write_report()

    assert isinstance(model, cobra.Model), "build_base_model must return a bare model"
    assert profile["exchanges"], "no exchanges on the draft"
    assert profile["biomass"], "no biomass on the draft"
    assert profile["objective"], "no objective on the draft"
    assert not profile["atpm"], (
        "ATPM is on build_base_model's result; upstream adds it in build(), so the "
        "layer would no longer need to add it itself"
    )
    assert model.genes, "role matching produced no genes -- the build is not role-based"


# ---------------------------------------------------------------------------
# (b) Which LP backend performs ATP correction to completion?
# ---------------------------------------------------------------------------


@pytest.mark.kbu_model
@pytest.mark.slow
@pytest.mark.parametrize("backend", ["glpk", "gurobi"])
def test_b_atp_correction_backend(backend, genome, gs_template, core_template):
    """Run ATP correction to completion on each backend, with version and timing.

    ``load_default_medias=True`` cannot be used. Upstream ``MSATPCorrection.__init__``
    names a parameter ``load_default_medias`` while the module also defines a function
    ``load_default_medias`` (msatpcorrection.py:38); at :115 the function call
    ``load_default_medias(default_media_path)`` therefore resolves to the bool
    PARAMETER and raises ``TypeError: 'bool' object is not callable`` for any truthy
    value. Upstream never trips over it because ``MSBuilder.build()`` passes
    ``load_default_medias=False`` and pre-loads the medias itself. KBUtilLib's
    ``build_metabolic_model`` passes it straight through, defaulting to ``True``
    (domains/modeling/ms_reconstruction_utils.py:251, :478), so every ATP-safe build
    on this stack raises before any solving happens. Both the defect and the
    working route are measured here.
    """
    from modelseedpy import MSATPCorrection
    from modelseedpy.core.msatpcorrection import (
        load_default_medias as load_default_medias_fn,
    )
    from modelseedpy.core.msmodelutl import MSModelUtil

    record: dict[str, Any] = {"backend": backend, "version": None, "completed": False}
    atp_medias_path = str(
        Path(modelseedpy.__file__).parent / "data" / "atp_medias.tsv"
    )

    try:
        if backend == "gurobi":
            import gurobipy

            record["version"] = ".".join(str(p) for p in gurobipy.gurobi.version())
        else:
            import swiglpk

            record["version"] = swiglpk.glp_version()
    except Exception as exc:  # noqa: BLE001
        record["version_error"] = f"{type(exc).__name__}: {exc}"

    _, model = _draft(genome, gs_template)
    MSBuilder.add_atpm(model)
    model.solver = backend
    mdlutl = MSModelUtil.get(model)

    # (i) The configuration build_metabolic_model actually uses: load_default_medias=True.
    try:
        MSATPCorrection(
            model_or_mdlutl=mdlutl,
            core_template=core_template,
            atp_medias=[],
            compartment="c0",
            atp_hydrolysis_id="ATPM_c0",
            load_default_medias=True,
            default_media_path=atp_medias_path,
        )
        record["load_default_medias_true"] = "succeeded"
    except BaseException as exc:  # noqa: BLE001
        record["load_default_medias_true"] = f"{type(exc).__name__}: {exc}"

    # (ii) The route upstream MSBuilder.build() uses: medias pre-loaded by the caller.
    try:
        default_medias = load_default_medias_fn(atp_medias_path)
        record["n_default_atp_medias"] = len(default_medias)
        # Keyword arguments at every call into ModelSEEDpy: fork and upstream both
        # report a 0.4.x version string while their signatures differ, so a version
        # string does not identify the code and positional binding is unsafe.
        #
        # ``atp_hydrolysis_id`` is supplied the way upstream ``MSBuilder.build()``
        # supplies it. ``build_metabolic_model`` does neither -- it never calls
        # ``add_atpm`` and never names the hydrolysis reaction, leaving upstream to
        # fall back to a SEED reaction search.
        atp = MSATPCorrection(
            model_or_mdlutl=mdlutl,
            core_template=core_template,
            atp_medias=default_medias,
            compartment="c0",
            atp_hydrolysis_id="ATPM_c0",
            load_default_medias=False,
            max_gapfilling=10,
            gapfilling_delta=0,
            forced_media=[],
            default_media_path=atp_medias_path,
        )
        t0 = time.time()
        tests = atp.run_atp_correction()
        record["seconds"] = round(time.time() - t0, 1)
        record["completed"] = True
        record["n_atp_tests"] = len(tests)
        record["atp_tests"] = [
            {
                "media": getattr(t.get("media"), "id", str(t.get("media"))),
                "objective": t.get("objective"),
                "threshold": t.get("threshold"),
            }
            for t in tests
        ]
        record["cumulative_core_gapfilling"] = (
            len(atp.cumulative_core_gapfilling)
            if atp.cumulative_core_gapfilling is not None
            else None
        )
        record["selected_media"] = (
            sorted(getattr(m, "id", str(m)) for m in atp.selected_media)
            if atp.selected_media is not None
            else None
        )
        record["reactions_after"] = len(model.reactions)
    except BaseException as exc:  # noqa: BLE001
        record["raised"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}

    MEASUREMENTS.setdefault("b_atp_correction", {})[backend] = record
    _write_report()

    # The shadowing defect must still be present; if it is fixed, the layer no
    # longer needs to pre-load medias and this measurement is stale.
    assert record["load_default_medias_true"].startswith("TypeError"), (
        "MSATPCorrection(load_default_medias=True) no longer raises; the "
        "msatpcorrection.py:115 shadowing defect has been fixed upstream"
    )
    assert record["completed"], (
        f"{backend} did not carry ATP correction to completion: "
        f"{record.get('raised')}"
    )
    assert record["n_atp_tests"] > 0, f"{backend} produced no ATP tests"


# ---------------------------------------------------------------------------
# (c) Does ModelSEEDBiochem resolve through the dependency manager? (#41)
# ---------------------------------------------------------------------------


@pytest.mark.kbu_model
@pytest.mark.slow
def test_c_modelseedbiochem_resolves_via_dependency_manager(recon_construction):
    """#41: ModelSEEDDatabase must come from the dependency manager, not config.cfg.

    Three separate things, because they have three separate answers under upstream:

    * the packaged ``config.cfg`` default, and whether it exists on this host;
    * what ``ModelSEEDBiochem.get()`` does when left to that default (the #41
      failure), and whether it can even be handed a path (upstream's ``get`` takes
      no ``path`` keyword, which is what breaks KBUtilLib's call at
      domains/biochem/ms_biochem_utils.py:394);
    * whether the dependency manager resolves a path that ``from_local`` can load.
    """
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg.read(str(Path(modelseedpy.__file__).parent / "config.cfg"))
    packaged_default = cfg.get("biochem", "path")
    resolved_default = Path(modelseedpy.__file__).parent / packaged_default

    dm_path = PROVENANCE["harness_dependencies_resolved"]["ModelSEEDDatabase"]

    record: dict[str, Any] = {
        "packaged_config_cfg_biochem_path": packaged_default,
        "packaged_default_is_absolute": Path(packaged_default).is_absolute(),
        "packaged_default_exists_as_given": Path(packaged_default).exists(),
        "packaged_default_resolved_against_package": str(resolved_default),
        "packaged_default_resolved_exists": resolved_default.exists(),
        "dependency_manager_path": dm_path,
        "dependency_manager_path_exists": Path(dm_path).is_dir(),
        "kbutillib_call": "ModelSEEDBiochem.get(path=self.modelseed_db_path)",
        "kbutillib_call_site": "src/kbutillib/domains/biochem/ms_biochem_utils.py:394",
    }

    import inspect

    from modelseedpy.biochem import from_local
    from modelseedpy.biochem.modelseed_biochem import ModelSEEDBiochem

    record["upstream_get_signature"] = str(inspect.signature(ModelSEEDBiochem.get))
    record["upstream_get_accepts_path"] = (
        "path" in inspect.signature(ModelSEEDBiochem.get).parameters
    )

    # (i) KBUtilLib's call, verbatim, against upstream.
    try:
        ModelSEEDBiochem.default_biochemistry = None
        ModelSEEDBiochem.get(path=dm_path)
        record["kbutillib_call_result"] = "succeeded"
    except BaseException as exc:  # noqa: BLE001
        record["kbutillib_call_result"] = f"{type(exc).__name__}: {exc}"
    finally:
        ModelSEEDBiochem.default_biochemistry = None

    # (ii) Upstream's own default: the #41 failure mode.
    try:
        ModelSEEDBiochem.default_biochemistry = None
        ModelSEEDBiochem.get()
        record["default_via_ModelSEEDBiochem_get"] = "succeeded"
    except BaseException as exc:  # noqa: BLE001
        record["default_via_ModelSEEDBiochem_get"] = f"{type(exc).__name__}: {exc}"
    finally:
        ModelSEEDBiochem.default_biochemistry = None

    # (iii) The dependency-manager path, loaded the way upstream supports.
    try:
        t0 = time.time()
        db = from_local(dm_path)
        record["from_local_seconds"] = round(time.time() - t0, 1)
        record["compounds"] = len(db.compounds)
        record["reactions"] = len(db.reactions)
        record["loaded_via_dependency_manager"] = True
    except BaseException as exc:  # noqa: BLE001
        record["loaded_via_dependency_manager"] = False
        record["from_local_raised"] = f"{type(exc).__name__}: {exc}"

    record["construction_consequence"] = {
        "msreconstructionutils_constructed": recon_construction["instance"] is not None,
        "raised": recon_construction["raised"],
    }

    MEASUREMENTS["c_modelseedbiochem"] = record
    _write_report()

    # The dependency manager must resolve a loadable database. That part must hold.
    assert record["dependency_manager_path_exists"], (
        f"dependency manager resolved {dm_path}, which is not a directory"
    )
    assert record["loaded_via_dependency_manager"], (
        "ModelSEEDDatabase did not load from the dependency-manager path: "
        f"{record.get('from_local_raised')}"
    )
    assert record["reactions"] > 0

    # And the packaged default must NOT be what anyone relies on (#41).
    assert not record["packaged_default_exists_as_given"], (
        "the packaged config.cfg biochem path now exists on this host; #41's "
        "premise has changed"
    )


# ---------------------------------------------------------------------------
# (d) MSGapfill.run_gapfilling with prefilter=True and prefilter=False
# ---------------------------------------------------------------------------


@pytest.mark.kbu_model
@pytest.mark.slow
@pytest.mark.parametrize("prefilter", [True, False])
def test_d_gapfill_prefilter(prefilter, genome, gs_template):
    """One medium, both prefilter settings, wall-clock recorded."""
    record: dict[str, Any] = {"prefilter": prefilter, "completed": False}

    try:
        _, model = _draft(genome, gs_template)
        MSBuilder.add_atpm(model)
        model.solver = WORKING_BACKENDS[0]
        record["backend"] = WORKING_BACKENDS[0]
        media = _minimal_glucose_oxygen_media()
        gapfill = MSGapfill(
            model_or_mdlutl=model,
            default_gapfill_templates=[gs_template],
            default_target="bio1",
        )
        t0 = time.time()
        result = gapfill.run_gapfilling(media=media, prefilter=prefilter)
        record["seconds"] = round(time.time() - t0, 1)
        record["completed"] = True
        record["result_is_none"] = result is None
        if result is not None:
            record["result_keys"] = sorted(result)
            record["n_new"] = len(result.get("new", {}))
            record["n_reversed"] = len(result.get("reversed", {}))
            record["new_sample"] = sorted(result.get("new", {}))[:20]
    except BaseException as exc:  # noqa: BLE001
        record["raised"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}

    MEASUREMENTS.setdefault("d_gapfill", {})[f"prefilter={prefilter}"] = record
    _write_report()

    assert record["completed"], (
        f"run_gapfilling(prefilter={prefilter}) did not complete: "
        f"{record.get('raised')}"
    )


# ---------------------------------------------------------------------------
# (e) Which genome classifier artifact loads, and its call on the genome?
# ---------------------------------------------------------------------------


@pytest.mark.kbu_model
def test_e_genome_classifier(genome):
    """Record the classifier artifact kind, how it loads, and its verdict."""
    import inspect

    ml_dir = Path(modelseedpy.__file__).parent / "data" / "ml"
    artifacts = sorted(p.name for p in ml_dir.glob("*")) if ml_dir.is_dir() else []

    record: dict[str, Any] = {
        "classifier_dir": str(ml_dir),
        "artifacts": artifacts,
        "npz_present": [a for a in artifacts if a.endswith(".npz")],
        "pickle_present": [a for a in artifacts if a.endswith(".pickle")],
        "get_classifier_signature": str(inspect.signature(get_classifier)),
    }

    classifier_id = next(
        (a[: -len(".npz")] for a in artifacts if a.endswith(".npz")), None
    )
    record["classifier_id"] = classifier_id

    if classifier_id is None:
        record["loaded"] = False
    else:
        clf = get_classifier(classifier_id)
        record["loaded"] = True
        record["loader"] = type(clf).__name__
        record["model_type"] = type(clf.model).__name__
        record["n_model_features"] = len(clf.features)
        record["classes"] = sorted(str(c) for c in clf.model.classes_)
        record["prediction_needs_sklearn"] = type(clf.model).__module__.startswith(
            "sklearn"
        )
        record["classify_signature"] = str(inspect.signature(clf.classify))
        record["classify_call"] = "classifier.classify(genome, ontology_term='RAST')"
        t0 = time.time()
        record["verdict"] = str(clf.classify(genome, ontology_term="RAST"))
        record["seconds"] = round(time.time() - t0, 3)
        roles = clf.extract_features_from_genome(genome, "RAST")
        matched = set(next(iter(roles.values()))) & set(clf.features)
        record["genome_roles"] = len(next(iter(roles.values())))
        record["genome_roles_matching_model_features"] = len(matched)

    MEASUREMENTS["e_genome_classifier"] = record
    _write_report()

    assert record["loaded"], "no genome classifier artifact loaded under upstream"
    assert record["npz_present"], (
        "upstream shipped no .npz classifier; the layer would have to carry a "
        "pickled sklearn model and pin its scikit-learn version"
    )
    assert record["verdict"] in {"P", "N", "A", "C", "--"}


# ---------------------------------------------------------------------------
# (f) Do KBUtilLib's roleless-path compensations change a ROLE-BASED draft?
# ---------------------------------------------------------------------------


@pytest.mark.kbu_model
@pytest.mark.slow
def test_f_roleless_compensations_on_a_role_based_draft(genome, gs_template):
    """Diff a role-based draft with and without each compensation.

    ``build_metabolic_model`` applies two compensations after the
    ``reactions_to_add`` injection point, both documented in the source as existing
    for the ROLELESS path:

    * a re-run of ``builder.build_non_metabolite_reactions`` (#29), and
    * a call to ``MSBuilder.add_exchanges_to_model`` before ATP correction (#28).

    With ``reactions_to_add=None`` on a ROLE-BASED draft, ``build_base_model`` has
    already done both internally, so both should be no-ops. Measured as a reaction-id
    set diff.
    """
    builder, model = _draft(genome, gs_template)
    before = {r.id for r in model.reactions}

    extra = builder.build_non_metabolite_reactions(
        model, allow_all_non_grp_reactions=False
    )
    if extra:
        model.add_reactions(extra)
    after_spontaneous = {r.id for r in model.reactions}

    MSBuilder.add_exchanges_to_model(model)
    after_exchanges = {r.id for r in model.reactions}

    record = {
        "draft_reactions": len(before),
        "spontaneous_rerun_returned": len(extra),
        "spontaneous_rerun_added": sorted(after_spontaneous - before),
        "exchange_rerun_added": sorted(after_exchanges - after_spontaneous),
        "atp_safe_annotation_overlay_in_build_metabolic_model": False,
        "note": (
            "The task prompt names the 'ATP-safe annotation overlay' as one of two "
            "compensations inside build_metabolic_model. It is not there: the overlay "
            "lives in GAA's recon_driver (step 7 of its module docstring). The second "
            "compensation actually inside build_metabolic_model is the "
            "add_exchanges_to_model call before ATP correction (#28), measured here."
        ),
    }

    MEASUREMENTS["f_roleless_compensations"] = record
    _write_report()

    assert not record["spontaneous_rerun_added"], (
        "the spontaneous/universal re-run adds reactions even to a role-based draft: "
        f"{record['spontaneous_rerun_added'][:20]}"
    )
    assert not record["exchange_rerun_added"], (
        "add_exchanges_to_model adds exchanges even to a role-based draft: "
        f"{record['exchange_rerun_added'][:20]}"
    )


# ---------------------------------------------------------------------------
# (g) Exchanges before ATP correction (#28); spontaneous/universal present (#29)
# ---------------------------------------------------------------------------


@pytest.mark.kbu_model
@pytest.mark.slow
def test_g_draft_has_exchanges_and_spontaneous_reactions(genome, gs_template):
    """#28 and #29, measured on the draft that ATP correction would receive."""
    _, model = _draft(genome, gs_template)
    ids = {r.id for r in model.reactions}

    spontaneous_universal = {
        f"{r.id}0"
        for r in gs_template.reactions
        if str(getattr(r.type, "value", r.type)).lower() in {"spontaneous", "universal"}
    }

    record = {
        "n_exchanges_before_atp_correction": len([i for i in ids if i.startswith("EX_")]),
        "template_spontaneous_universal": len(spontaneous_universal),
        "spontaneous_universal_in_draft": len(spontaneous_universal & ids),
        "membrane_diffusion_probes": {
            # The three #29 named reactions: O2, CO2 and water diffusion.
            rid: rid in ids
            for rid in ("rxn05468_c0", "rxn05467_c0", "rxn05319_c0")
        },
    }
    record["spontaneous_universal_coverage"] = (
        round(record["spontaneous_universal_in_draft"] / record["template_spontaneous_universal"], 3)
        if record["template_spontaneous_universal"]
        else None
    )

    MEASUREMENTS["g_draft_exchanges_and_spontaneous"] = record
    _write_report()

    assert record["n_exchanges_before_atp_correction"] > 0, (
        "#28: the draft reaches ATP correction with zero exchanges"
    )
    assert record["spontaneous_universal_in_draft"] > 0, (
        "#29: no spontaneous/universal template reactions reached the draft"
    )
