"""Offline tests for the reconstruction layer.

Everything here runs WITHOUT ModelSEEDpy, a ModelSEED database or an LP solver:
these are the static and pure-logic properties of
``kbutillib.domains.modeling.ms_reconstruction_utils``. The behaviour that needs
the real stack lives in ``tests/test_recon_layer_real_dependency.py``.

The one import this module needs is the reconstruction module itself, which is
import-safe: every ModelSEEDpy import in it is inside a function or method body.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from kbutillib.domains.modeling.ms_reconstruction_utils import (
    DEFAULT_GENOME_CLASSIFIER_ID,
    DEFAULT_ONTOLOGY_TERM,
    GENOME_CLASS_TO_TEMPLATE_KEY,
    GENOME_CLASSES_WITH_DEDICATED_TEMPLATE,
    RECONSTRUCTION_DATA_DEPENDENCIES,
    SOLVER_PREFERENCE,
    UNKNOWN_REVISION,
    GapfillRecord,
    MissingBiochemistryError,
    MissingSolverError,
    ModelSEEDpyCapabilityError,
    MSReconstructionUtils,
    ReconstructionEnvironmentError,
    ReconstructionResult,
    initialize_reconstruction_environment,
    resolve_distribution_provenance,
    template_key_for_genome_class,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kbutillib"
    / "domains"
    / "modeling"
    / "ms_reconstruction_utils.py"
)


# ---------------------------------------------------------------------------
# Keyword-only calls into ModelSEEDpy, asserted by AST
# ---------------------------------------------------------------------------

#: Receivers whose method calls go into ModelSEEDpy. The four classes the PRD
#: names, plus the local variable names this module binds instances of them to
#: (``builder``, ``atpcorrection``, ``msgapfill``) -- a positional call on an
#: instance rebinds exactly as dangerously as one on the class.
MODELSEEDPY_RECEIVERS = frozenset(
    {
        "MSBuilder",
        "MSGapfill",
        "MSATPCorrection",
        "MSTemplateBuilder",
        "MSModelUtil",
        "MSMedia",
        "MSModelReport",
        "builder",
        "atpcorrection",
        "msgapfill",
    }
)


def _immediate_receiver(func: ast.AST) -> str | None:
    """Name of the object a call is made ON, one attribute deep.

    Args:
        func: The ``func`` node of an ``ast.Call``.

    Returns:
        ``"MSBuilder"`` for both ``MSBuilder(...)`` and
        ``self.MSBuilder.add_atpm(...)``; ``"builder"`` for
        ``builder.build_base_model(...)``; ``None`` when the receiver is not a
        plain name/attribute.

    Deliberately ONE attribute deep. ``builder.base_model.add_metabolites(...)``
    has immediate receiver ``builder.base_model``, whose own attribute name is
    ``base_model``, so it is not treated as a ModelSEEDpy call -- correctly:
    ``add_metabolites`` is cobra's API reached through a ModelSEEDpy object, and
    this rule is about calls into ModelSEEDpy itself.
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        receiver = func.value
        if isinstance(receiver, ast.Name):
            return receiver.id
        if isinstance(receiver, ast.Attribute):
            return receiver.attr
        return None
    return None


def test_no_positional_modelseedpy_calls():
    """Every call into ModelSEEDpy passes its arguments BY KEYWORD.

    The ModelSEEDpy fork and upstream report the same version string while
    taking different parameters in different orders. The call this layer
    replaced, ``builder.build(base_model, "0", False, False)``, bound ``"0"`` to
    upstream's ``gapfill_media``, the first ``False`` to ``index``, and left
    ``annotate_with_rast`` at its default ``True`` -- a live RAST network call.
    Nothing raised; the arguments simply landed on the wrong parameters.

    A positional call is therefore not a style problem here, it is the failure
    mode. This test reads the module's AST so the rule holds for code nobody
    runs in CI (the real-dependency paths), and so it cannot regress silently.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        receiver = _immediate_receiver(node.func)
        if receiver in MODELSEEDPY_RECEIVERS:
            rendered = ast.unparse(node.func)
            offenders.append(f"{MODULE_PATH.name}:{node.lineno} {rendered}(...)")
    assert offenders == [], (
        "positional arguments at calls into ModelSEEDpy:\n  "
        + "\n  ".join(offenders)
    )


def test_ast_check_would_catch_the_defect_it_exists_for():
    """The AST rule actually fires on the call pattern it was written against.

    A guard nobody has seen fail is a guard nobody knows works. This feeds the
    exact expression that was removed from ``build_metabolic_model`` through the
    same receiver test and asserts it is flagged.
    """
    defect = ast.parse('builder.build(base_model, "0", False, False)').body[0]
    assert isinstance(defect, ast.Expr)
    call = defect.value
    assert isinstance(call, ast.Call)
    assert call.args, "the historical defect had positional arguments"
    assert _immediate_receiver(call.func) in MODELSEEDPY_RECEIVERS


def test_msbuilder_build_is_never_called():
    """``MSBuilder.build`` is not called anywhere in the module.

    ``build()`` runs ``build_base_model``, adds ``ATPM``, runs
    ``MSATPCorrection`` AND calls ``MSGapfill.run_gapfilling`` itself. Calling it
    would collapse the DRAFT / POST-ATP / POST-GAP-FILL boundaries into one step
    and choose the gap-fill medium on the caller's behalf, so the layer must
    drive ``build_base_model`` and the two correction stages itself.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    calls = [
        f"line {node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "build"
        and _immediate_receiver(node.func) in MODELSEEDPY_RECEIVERS
    ]
    assert calls == [], f"MSBuilder.build() must never be called: {calls}"


def test_build_uses_build_base_model_with_the_required_keywords():
    """The draft is built by ``build_base_model`` with the stage-critical flags.

    Asserted statically rather than by running a build, because these four
    keywords are the whole contract of the DRAFT stage and must be present even
    in an environment that cannot build anything:

    * ``annotate_with_rast=False`` -- the genome arrives annotated; the default
      ``True`` makes a live RAST network call;
    * ``match_role_aliases`` and ``allow_incomplete_complexes`` -- forwarded
      from this layer's parameters, not left to ModelSEEDpy's defaults;
    * ``model_or_id`` -- named, so the base model cannot slide onto another
      parameter.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "build_base_model"
            and _immediate_receiver(node.func) in MODELSEEDPY_RECEIVERS
        ):
            found.append({kw.arg for kw in node.keywords})
    assert found, "build_base_model is not called in the module"
    required = {
        "model_or_id",
        "annotate_with_rast",
        "match_role_aliases",
        "allow_incomplete_complexes",
    }
    assert any(required <= keywords for keywords in found), (
        f"no build_base_model call supplies all of {sorted(required)}; "
        f"found keyword sets {found}"
    )
    for keywords in found:
        assert "gapfill_media" not in keywords


def test_module_declares_no_gaa_dependency():
    """``genome_annotation_aggregator`` is not importable from this module.

    The PRD makes GAA a non-dependency of the new code: all modelling leaves GAA
    and lands here. A stray import would also reintroduce
    ``build_from_annotaton_ontology``, which the PRD excludes.
    """
    source = MODULE_PATH.read_text()
    assert "genome_annotation_aggregator" not in source
    assert "build_from_annotaton_ontology" not in source


# ---------------------------------------------------------------------------
# Genome class -> template key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("genome_class", "expected"),
    [
        ("P", "gp"),
        ("N", "gn"),
        ("A", "ar"),
        # No dedicated template -> gn, rather than the old "skip this genome".
        ("C", "gn"),
        ("--", "gn"),
        ("Z", "gn"),
        ("", "gn"),
    ],
)
def test_template_key_for_genome_class(genome_class, expected):
    """The class map, and the no-dedicated-template fallback to ``gn``."""
    assert template_key_for_genome_class(genome_class) == expected


def test_only_p_n_a_have_dedicated_templates():
    """``GENOME_CLASSES_WITH_DEDICATED_TEMPLATE`` agrees with the map.

    Every letter outside the dedicated set must resolve to ``"gn"``; a letter
    inside it must resolve to something else (or, for ``N``, to ``gn`` as its own
    dedicated template). This keeps the two constants from drifting, which is
    what decides whether a report row says "no dedicated template" or not.
    """
    for genome_class, key in GENOME_CLASS_TO_TEMPLATE_KEY.items():
        if genome_class in GENOME_CLASSES_WITH_DEDICATED_TEMPLATE:
            continue
        assert key == "gn", f"{genome_class} is not dedicated but maps to {key}"
    assert set(GENOME_CLASSES_WITH_DEDICATED_TEMPLATE) <= set(
        GENOME_CLASS_TO_TEMPLATE_KEY
    )


def test_classifier_default_is_the_npz_artifact():
    """The default classifier id is the ``.npz`` upstream actually ships.

    ``modelseedpy.helpers.get_classifier`` loads ``{id}.npz`` when present and
    otherwise DOWNLOADS a legacy pickle that needs scikit-learn 1.2.0. The
    shipped artifact at the pinned revision is
    ``linear_svc_ACNP_RAST_full_11_11_2026.npz``; naming any other id silently
    takes the pickle path and reintroduces the version pin.
    """
    assert DEFAULT_GENOME_CLASSIFIER_ID == "linear_svc_ACNP_RAST_full_11_11_2026"
    assert "pickle" not in DEFAULT_GENOME_CLASSIFIER_ID


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


def test_reconstruction_result_is_frozen_and_fully_enumerated():
    """``ReconstructionResult`` is frozen and every field is explicit.

    Frozen because a result describes a stage that has already passed: once
    POST-ATP facts are recorded, nothing downstream may edit them in place.
    Fully enumerated (no ``**kwargs``, no dict bag) because a field that can
    appear or vanish cannot be read as a measurement.
    """
    assert dataclasses.is_dataclass(ReconstructionResult)
    params = ReconstructionResult.__dataclass_params__
    assert params.frozen is True

    fields = {f.name: f for f in dataclasses.fields(ReconstructionResult)}
    # No field carries a default: every one must be supplied from something
    # measured, so a forgotten field is a TypeError rather than a plausible zero.
    defaulted = [
        name
        for name, f in fields.items()
        if f.default is not dataclasses.MISSING
        or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
    ]
    assert defaulted == [], f"fields with defaults would read as measurements: {defaulted}"


def test_every_per_stage_field_names_its_stage():
    """Each model-fact field is prefixed with the stage it describes.

    "957 reactions" means three different things at the three stage boundaries,
    so the stage has to be in the field name. Identity, settings and provenance
    fields are not per-stage and are listed explicitly here rather than matched
    by a pattern, so adding a field forces a decision about which group it is in.
    """
    non_stage_fields = {
        "genome_id",
        "genome_class",
        "template_id",
        "template_name",
        "core_template_id",
        "core_template_name",
        "ontology_term",
        "match_role_aliases",
        "allow_incomplete_complexes",
        "reactions_to_add_supplied",
        "modelseedpy_version",
        "modelseedpy_revision",
        "kbutillib_version",
        "kbutillib_revision",
        "solver",
    }
    stage_prefixes = ("draft_", "post_atp_", "post_gapfill_")
    for f in dataclasses.fields(ReconstructionResult):
        if f.name in non_stage_fields:
            continue
        assert f.name.startswith(stage_prefixes), (
            f"{f.name} is neither a listed non-stage field nor prefixed with "
            f"one of {stage_prefixes}"
        )


def test_gapfill_record_is_frozen_and_carries_the_required_fields():
    """A record identifies its OCCURRENCE, not just its medium."""
    assert GapfillRecord.__dataclass_params__.frozen is True
    names = [f.name for f in dataclasses.fields(GapfillRecord)]
    for required in ("request_index", "media_id", "media_digest", "status", "additions"):
        assert required in names
    record = GapfillRecord(
        request_index=2,
        media_id="Glucose",
        media_digest="sha256:abc",
        status="integrated",
        additions=("rxn1_c0",),
        reversals=(),
        objective_value=0.5,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.status = "no_solution"  # type: ignore[misc]


def test_same_media_id_twice_yields_two_distinguishable_records():
    """Two occurrences of one id are two records, told apart by request_index.

    This is the property the whole record shape exists for.
    ``MSGapfill.run_multi_gapfill`` returns a dict keyed by media object, so the
    second request of a repeated medium overwrites the first and the fact that
    it was attempted twice is lost.
    """
    first = GapfillRecord(
        request_index=0,
        media_id="Glucose",
        media_digest="sha256:same",
        status="integrated",
        additions=("rxn1_c0", "rxn2_c0"),
        reversals=(),
        objective_value=0.68,
    )
    second = GapfillRecord(
        request_index=2,
        media_id="Glucose",
        media_digest="sha256:same",
        status="integrated",
        additions=(),
        reversals=(),
        objective_value=0.65,
    )
    records = (first, second)
    assert [r.request_index for r in records] == [0, 2]
    assert first.media_id == second.media_id
    assert first.media_digest == second.media_digest
    assert first != second
    # A dict keyed by media id -- the shape this replaces -- loses one of them.
    assert len({r.media_id: r for r in records}) == 1
    assert len({r.request_index: r for r in records}) == 2


def test_with_gapfill_results_does_not_mutate_the_input():
    """Attaching POST-GAP-FILL facts returns a NEW frozen result."""
    base = _dummy_result()
    record = GapfillRecord(
        request_index=0,
        media_id="Glucose",
        media_digest="sha256:abc",
        status="integrated",
        additions=("rxn1_c0",),
        reversals=(),
        objective_value=1.0,
    )

    class _Model:
        reactions = ["a", "b", "c"]

    class _Util:
        model = _Model()

    updated = MSReconstructionUtils.with_gapfill_results(
        result=base,
        mdlutl=_Util(),
        gapfill_records=[record],
        reaction_exclusion_list=["rxn9_c0"],
    )
    assert base.post_gapfill_records == ()
    assert base.post_gapfill_reaction_count is None
    assert updated.post_gapfill_records == (record,)
    assert updated.post_gapfill_reaction_count == 3
    assert updated.post_gapfill_reaction_exclusion_list == ("rxn9_c0",)
    # Everything else is carried over untouched.
    assert updated.draft_reaction_count == base.draft_reaction_count
    assert updated.modelseedpy_revision == base.modelseedpy_revision


def _dummy_result() -> ReconstructionResult:
    """A fully populated result with placeholder values, for shape tests."""
    return ReconstructionResult(
        draft_reaction_count=957,
        draft_metabolite_count=967,
        draft_gene_count=588,
        draft_exchange_count=73,
        draft_drain_count=4,
        draft_biomass_ids=("bio1",),
        draft_objective_expression="1.0*bio1",
        draft_atpm_present=False,
        post_atp_correction_ran=True,
        post_atp_correction_invocations=1,
        post_atp_reaction_count=958,
        post_atp_removed_reaction_ids=(),
        post_atp_core_gapfilling_count=0,
        post_atp_selected_media_ids=("Glc",),
        post_atp_test_count=4,
        post_atp_analysis={"stage": "post-atp"},
        post_gapfill_records=(),
        post_gapfill_reaction_count=None,
        post_gapfill_reaction_exclusion_list=(),
        genome_id="71421.8",
        genome_class="N",
        template_id="GramNegative.modeltemplate",
        template_name="GramNegative",
        core_template_id="CoreModelTemplateV2",
        core_template_name="CoreModelTemplate",
        ontology_term="RAST",
        match_role_aliases=False,
        allow_incomplete_complexes=True,
        reactions_to_add_supplied=False,
        modelseedpy_version="0.4.3",
        modelseedpy_revision="e1155a41fa53b921185b20c6cfbd083e5e3739a4",
        kbutillib_version="1.0.0",
        kbutillib_revision=UNKNOWN_REVISION,
        solver="glpk",
    )


def test_result_to_dict_is_json_serialisable():
    """``to_dict`` survives ``json.dumps`` with no custom encoder.

    It did not, once: ``post_atp_analysis["media_gapfill_stats"]`` was keyed by
    the ``MSMedia`` OBJECT that ``MSATPCorrection`` keys it by, and
    ``json.dumps`` raised "keys must be str, int, float, bool or None". A report
    surface that cannot be serialised is not a report surface.
    """
    import json

    payload = json.dumps(_dummy_result().to_dict())
    assert '"draft_reaction_count": 957' in payload


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_revision_is_unknown_rather_than_synthesised():
    """An absent distribution reports ``unknown`` for BOTH version and revision.

    Not an empty string, not the installed Python's version, not a SHA read from
    some nearby git checkout. A synthesised provenance value is worse than a
    missing one because it reads as a measurement.
    """
    version, revision = resolve_distribution_provenance(
        "this-distribution-does-not-exist-kbu"
    )
    assert version == UNKNOWN_REVISION
    assert revision == UNKNOWN_REVISION


def test_kbutillib_provenance_resolves_its_own_version():
    """KBUtilLib is installed, so its VERSION is readable.

    Its revision is ``unknown`` under an editable install -- pip records
    ``dir_info.editable`` and no commit -- so this asserts only that the value
    is a string and does not demand a SHA that the install genuinely does not
    record.
    """
    version, revision = resolve_distribution_provenance("KBUtilLib")
    assert version != UNKNOWN_REVISION
    assert isinstance(revision, str)


# ---------------------------------------------------------------------------
# Classified errors
# ---------------------------------------------------------------------------


def test_classified_errors_are_three_distinct_classes():
    """The three error classes are distinct, so a caller can act on one.

    They share a base for convenience, but no two are the same class and none is
    a subclass of another: a missing solver and a missing database need
    different responses, and distinguishing them by message text is not a
    contract.
    """
    classes = [MissingSolverError, MissingBiochemistryError, ModelSEEDpyCapabilityError]
    assert len({c.__name__ for c in classes}) == 3
    for cls in classes:
        assert issubclass(cls, ReconstructionEnvironmentError)
    for cls in classes:
        for other in classes:
            if cls is not other:
                assert not issubclass(cls, other)


def test_missing_dependency_config_raises_biochemistry_error_without_blaming_41(
    tmp_path,
):
    """An absent config is reported as such, and NOT as KBDL issue #41.

    #41 is the narrower claim that modelseedpy's own ``config.cfg`` default was
    used. This layer never takes that route, so it can never be the explanation
    for a failure here -- and saying so stops an operator editing a
    ``config.cfg`` that has nothing to do with the problem.
    """
    absent = tmp_path / "nope" / "dependencies.yaml"
    with pytest.raises(MissingBiochemistryError) as excinfo:
        initialize_reconstruction_environment(dependency_config_path=absent)
    message = str(excinfo.value)
    assert str(absent) in message
    assert "#41" not in message
    assert "config.cfg" not in message


def test_undeclared_dependency_names_itself_and_disclaims_41(tmp_path):
    """A config missing ``ModelSEEDDatabase`` says so, and disclaims #41."""
    config = tmp_path / "dependencies.yaml"
    config.write_text("dependencies:\n  something_else:\n    path: /tmp\n")
    with pytest.raises(MissingBiochemistryError) as excinfo:
        initialize_reconstruction_environment(dependency_config_path=config)
    message = str(excinfo.value)
    assert "ModelSEEDDatabase" in message
    assert "NOT KBDL issue #41" in message


def test_declared_but_absent_path_is_reported_as_a_missing_path(tmp_path):
    """A declared path that is not on disk is a MISSING PATH, not #41.

    This is the case the distinction matters most for: the file is simply not
    there, and seeding the biochemistry from the dependency manager -- the fix
    for #41 -- does nothing about it.
    """
    config = tmp_path / "dependencies.yaml"
    missing_dir = tmp_path / "not-here"
    config.write_text(
        "dependencies:\n"
        f"  ModelSEEDDatabase:\n    path: {missing_dir}\n"
        f"  cb_annotation_ontology_api:\n    path: {missing_dir}\n"
    )
    with pytest.raises(MissingBiochemistryError) as excinfo:
        initialize_reconstruction_environment(
            dependency_config_path=config, scratch_dir=tmp_path
        )
    message = str(excinfo.value)
    assert "not present on disk" in message
    assert "MISSING PATH, not KBDL issue #41" in message


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_modelseedpy_is_not_a_dependency_manager_entry():
    """The layer never lets the dependency manager put ModelSEEDpy on sys.path.

    ``DependencyManager.initialize_dependencies`` does
    ``sys.path.insert(0, dep_path)`` for EVERY declared entry, and
    ``~/.kbutillib/dependencies.yaml`` declares ``modelseedpy`` pointing at the
    FORK checkout. Initialising the whole file therefore shadows a pinned
    upstream install silently -- observed as a process printing ``0.4.3`` before
    the call and ``0.4.2`` after it. Restricting the set is the only defence,
    and ``cobrakbase`` is excluded for the same reason.
    """
    assert "modelseedpy" not in RECONSTRUCTION_DATA_DEPENDENCIES
    assert "cobrakbase" not in RECONSTRUCTION_DATA_DEPENDENCIES
    assert set(RECONSTRUCTION_DATA_DEPENDENCIES) == {
        "ModelSEEDDatabase",
        "cb_annotation_ontology_api",
    }


def test_glpk_is_probed_before_gurobi():
    """GLPK leads the solver preference.

    It needs no licence and completed ATP correction faster than Gurobi on the
    measured model (50.9 s vs 59.3 s). Gurobi works on this host but only below
    major version 14, and with ``gurobipy`` 14 installed even a GLPK solve fails
    -- cobra picks its interface at ``Model`` construction, before
    ``model.solver`` is assigned.
    """
    assert SOLVER_PREFERENCE[0] == "glpk"
    assert "gurobi" in SOLVER_PREFERENCE


def test_default_ontology_term_is_explicit():
    """``RAST`` is the default, and it is a real parameter rather than implied."""
    assert DEFAULT_ONTOLOGY_TERM == "RAST"
    import inspect

    for method in (
        MSReconstructionUtils.build_metabolic_model,
        MSReconstructionUtils.classify_genome,
    ):
        parameters = inspect.signature(method).parameters
        assert "ontology_term" in parameters
        assert parameters["ontology_term"].default == DEFAULT_ONTOLOGY_TERM


def test_build_metabolic_model_exposes_the_required_parameters():
    """The supplied-objects contract is in the signature.

    A caller must be able to supply both templates, name the ontology term, and
    opt into alias matching and incomplete complexes -- with the alias flag OFF
    by default, because native ModelSEED templates alias 20,551 of 20,552 core
    roles and switching it on unconditionally would change every native build.
    """
    import inspect

    parameters = inspect.signature(
        MSReconstructionUtils.build_metabolic_model
    ).parameters
    for name in (
        "gs_template_obj",
        "core_template",
        "ontology_term",
        "match_role_aliases",
        "allow_incomplete_complexes",
        "reactions_to_add",
    ):
        assert name in parameters, name
    assert parameters["match_role_aliases"].default is False
    assert parameters["allow_incomplete_complexes"].default is True
    # genome_classifier is optional: supplying a template skips classification,
    # so a KBase-free caller must not be forced to load a classifier.
    assert parameters["genome_classifier"].default is None


def test_gapfill_prefilter_defaults_to_false_and_says_why():
    """``prefilter`` is explicit, defaults ``False``, and the docstring is honest.

    ``prefilter=True`` is NOT broken -- both settings were measured working and
    returned the identical solution -- so the docstring must not imply it is.
    ``False`` is the default because ``True`` was never the faster of the two
    while producing the same answer.
    """
    import inspect

    parameters = inspect.signature(
        MSReconstructionUtils.gapfill_metabolic_model
    ).parameters
    assert "prefilter" in parameters
    assert parameters["prefilter"].default is False
    doc = MSReconstructionUtils.gapfill_metabolic_model.__doc__
    assert "NOT because ``prefilter=True`` is\n                broken" in doc


def test_gapfill_rejects_a_non_sequential_mode():
    """Only sequential integration is supported, and asking for else raises.

    The parameter used to be forwarded to ``run_multi_gapfill``, which has no
    ``gapfilling_mode`` at the pinned revision, so the request was never
    honoured -- it raised ``TypeError`` from inside ModelSEEDpy. Failing here
    with a reason is the improvement.
    """
    recon = MSReconstructionUtils.__new__(MSReconstructionUtils)
    with pytest.raises(ValueError, match="Sequential"):
        MSReconstructionUtils.gapfill_metabolic_model(
            recon,
            mdlutl=None,
            genome=None,
            media_objs=[],
            templates=[],
            gapfilling_mode="Cumulative",
        )
