"""Real-dependency tests for the reconstruction layer.

These run the layer against a REAL ModelSEEDpy (the pinned ``kbdl-target``
revision), the real ModelSEED biochemistry database and a real LP solver. They
cannot be faked: the whole question is whether the layer drives the installed
ModelSEEDpy correctly, and a mock would answer it with whatever the mock's
author believed.

Two things are asserted here that nothing offline can reach:

1. A ROLE-BASED build from a SUPPLIED native template, carried through ATP
   correction and then gap-filled on two media with one exclusion -- checking
   per-occurrence records in request order (with one medium requested TWICE),
   that the excluded reaction is absent from the shipped model, that ATP
   correction ran exactly ONCE, that the ATP-removed set is reported, and that
   the provenance revisions are populated from distribution metadata.
2. A build in which a gene's ONLY function is a template role's second,
   alias-only function -- which succeeds only through alias matching.

Opt-in, and FAILS rather than skips when opted in with the stack missing::

    KBU_REQUIRE_MODEL_STACK=1 \\
    /scratch/chenry/venvs/kbdl-recon-scratch-layer/bin/python -m pytest \\
        tests/test_recon_layer_real_dependency.py -v

Without ``KBU_REQUIRE_MODEL_STACK=1`` the module skips, so it is inert in the
ordinary suite (whose venvs have no ModelSEED database). A silent skip is how an
unmeasured environment gets mistaken for a measured one, which is why the opted-
in half raises instead.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest

#: Set to ``1`` to demand the real stack: the module then RAISES at import when
#: anything is missing, which pytest reports as a collection error -- a
#: non-passing outcome. Unset, the module skips.
REQUIRE_ENV_VAR = "KBU_REQUIRE_MODEL_STACK"

_REQUIRED = os.environ.get(REQUIRE_ENV_VAR) == "1"

#: The genome fixture the smoke task committed and measured against: real
#: Haemophilus influenzae Rd KW20 (BV-BRC 71421.8), 1826 CDS features carrying
#: 1530 distinct real RASTtk role strings, no protein sequences (nothing on the
#: role-based path reads them).
FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "model"
    / "haemophilus_influenzae_71421_8_rast.json.gz"
)


def _unavailable(reason: str) -> None:
    """Fail when the stack was demanded, skip when it was not."""
    if _REQUIRED:
        raise RuntimeError(
            f"{REQUIRE_ENV_VAR}=1 demands the real model stack, but: {reason}"
        )
    pytest.skip(reason, allow_module_level=True)


if not _REQUIRED:
    pytest.skip(
        f"real-dependency recon tests are opt-in; set {REQUIRE_ENV_VAR}=1",
        allow_module_level=True,
    )

try:
    import cobra  # noqa: F401,E402
    from modelseedpy import MSGenome  # noqa: E402
    from modelseedpy.core.msgenome import MSFeature  # noqa: E402
    from modelseedpy.core.msmedia import MSMedia  # noqa: E402
    from modelseedpy.core.mstemplate import MSTemplateBuilder  # noqa: E402
    from modelseedpy.helpers import get_template  # noqa: E402
except ImportError as exc:  # pragma: no cover - environment dependent
    _unavailable(f"cobra/modelseedpy not importable: {exc}")

from kbutillib.domains.modeling.ms_reconstruction_utils import (  # noqa: E402
    DEFAULT_ONTOLOGY_TERM,
    UNKNOWN_REVISION,
    MSReconstructionUtils,
    _objective_value_on_media,
    initialize_reconstruction_environment,
)

if not FIXTURE.is_file():  # pragma: no cover - environment dependent
    _unavailable(f"genome fixture absent: {FIXTURE}")

#: A reaction gap-filling DOES otherwise add to this model on glucose minimal,
#: and which it can also route AROUND. Both halves were measured:
#:
#: * unexcluded, the run integrates 135 reactions and ``rxn01256_c0`` is one of
#:   them -- so excluding it constrains a choice the solver actually makes;
#: * excluded, the run still succeeds, integrating 137 reactions (two more,
#:   taking a longer route) at objective 0.65 instead of 0.68.
#:
#: That second half is what makes this a test of ENFORCEMENT rather than of
#: breakage. ``rxn00029`` and ``rxn00060`` were also measured from the same
#: addition set and are NOT usable here: excluding either leaves the model
#: ungapfillable (``no_solution``, objective 0.0), so a passing assertion would
#: only have shown that gap-filling had stopped working.
#:
#: Supplied in the COMPARTMENTALISED form on purpose. ``GapfillingPkg`` matches
#: its blacklist against BASE reaction ids, so passing this string through
#: unchanged matches nothing and the reaction is gap-filled in anyway -- observed
#: exactly that way before the layer normalised the list. Using the form that
#: used to fail is what makes this a regression test rather than a restatement.
EXCLUDED_REACTION = "rxn01256_c0"
EXCLUDED_BASE_REACTION = "rxn01256"

#: Aerobic glucose minimal medium, ModelSEED compound ids.
_GLUCOSE_MINIMAL = {
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


def _media(composition: dict, media_id: str) -> Any:
    """An ``MSMedia`` carrying the caller's id verbatim."""
    media = MSMedia.from_dict(composition)
    media.id = media_id
    media.name = media_id
    return media


@pytest.fixture(scope="module")
def environment():
    """The initialised reconstruction environment.

    Also the test of :func:`initialize_reconstruction_environment` itself: if it
    cannot resolve the database, probe a solver and verify alias matching, every
    test below is meaningless, so it runs first and its findings are asserted.
    """
    env = initialize_reconstruction_environment()
    assert env.solver in ("glpk", "gurobi")
    # Confirmed by a real solve returning the known answer, not by availability.
    assert env.solver_probe_objective == pytest.approx(10.0)
    assert env.dependency_paths["ModelSEEDDatabase"]
    assert env.dependency_paths["cb_annotation_ontology_api"]
    assert env.biochemistry_loaded is True
    assert env.biochemistry_compound_count and env.biochemistry_compound_count > 40000
    # Alias matching verified BEHAVIOURALLY, on a two-role in-memory template.
    assert env.alias_matching_verified is True
    assert env.alias_matching_gene_ids == ("g0",)
    # The pin is a git install, so the revision is a real SHA.
    assert env.modelseedpy_revision != UNKNOWN_REVISION
    assert len(env.modelseedpy_revision) == 40
    return env


@pytest.fixture(scope="module")
def genome():
    """Haemophilus influenzae Rd KW20 with real RASTtk roles under ``RAST``."""
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        doc = json.load(handle)
    msgenome = MSGenome()
    features = []
    for feature_id, roles in doc["features"].items():
        feature = MSFeature(feature_id, "", description=roles[0])
        for role in roles:
            feature.add_ontology_term(DEFAULT_ONTOLOGY_TERM, role)
        features.append(feature)
    msgenome.add_features(features)
    msgenome.id = doc["genome_id"]
    msgenome.scientific_name = doc["scientific_name"]
    return msgenome


@pytest.fixture(scope="module")
def gs_template():
    """Native ModelSEED gram-negative template, SUPPLIED to the layer."""
    return MSTemplateBuilder.from_dict(get_template("template_gram_neg")).build()


@pytest.fixture(scope="module")
def core_template():
    """Native ModelSEED core template, SUPPLIED to the layer."""
    return MSTemplateBuilder.from_dict(get_template("template_core")).build()


@pytest.fixture(scope="module")
def recon(environment):
    """``MSReconstructionUtils`` with its dependency paths forwarded EXPLICITLY.

    Constructing this at all is the thing that was impossible before: against
    upstream ModelSEEDpy, ``MSBiochemUtils.__init__`` called
    ``ModelSEEDBiochem.get(path=...)``, whose ``path`` keyword upstream does not
    have, so ``__init__`` raised ``TypeError`` and no reconstruction code could
    be reached at all. The dependency manager is handed over rather than
    monkeypatched in, so per-instance ``get_data_path`` returns real paths.
    """
    instance = MSReconstructionUtils(
        dependency_manager=environment.dependency_manager,
        modelseed_db_path=environment.dependency_paths["ModelSEEDDatabase"],
    )
    # The resolved paths actually reached this instance; a None here is what
    # sends every consumer off to its own sibling-directory guess.
    assert instance.get_data_path("ModelSEEDDatabase") is not None
    assert instance.get_data_path("cb_annotation_ontology_api") is not None
    return instance


@pytest.fixture(scope="module")
def built(recon, genome, gs_template, core_template):
    """The role-based build: DRAFT + POST-ATP, from supplied templates.

    Module-scoped because it takes about a minute and every assertion below
    inspects the same reconstruction.
    """
    row, mdlutl, result = recon.build_metabolic_model(
        genome=genome,
        gs_template_obj=gs_template,
        core_template=core_template,
        ontology_term=DEFAULT_ONTOLOGY_TERM,
        atp_safe=True,
        collect_result=True,
    )
    return {"row": row, "mdlutl": mdlutl, "result": result}


@pytest.mark.kbu_model
@pytest.mark.slow
def test_draft_is_genuinely_role_based(built):
    """The DRAFT is built from ROLES, and is a complete uncorrected model.

    Genes on the model is the load-bearing number: it is only non-zero because
    genome functions matched template role names, which is what "builds the
    model natively" means. The roleless path this replaced produced a draft with
    no metabolic reactions at all and injected them afterwards.

    The rest is the DRAFT stage boundary: exchanges, biomass and an objective
    PRESENT, ``ATPM`` ABSENT (``build_base_model`` does not add it; this layer
    does, on the POST-ATP side of the boundary).
    """
    result = built["result"]
    assert result.draft_gene_count > 500
    assert result.draft_reaction_count > 900
    assert result.draft_exchange_count > 0
    assert result.draft_biomass_ids == ("bio1",)
    assert "bio1" in result.draft_objective_expression
    assert result.draft_atpm_present is False
    assert result.match_role_aliases is False
    assert result.reactions_to_add_supplied is False
    assert result.ontology_term == DEFAULT_ONTOLOGY_TERM


@pytest.mark.kbu_model
@pytest.mark.slow
def test_atp_correction_ran_exactly_once_and_reports_its_removals(built):
    """ATP correction ran ONCE, and the set it removed is reported.

    Once matters: ``run_atp_correction`` disables noncore reactions, gap-fills
    the core and then re-expands to genome scale while filtering reactions that
    break ATP, so a second call re-filters an already-filtered model.

    The removed set is reported whether or not it is empty -- on this genome it
    IS empty, and that is a measurement, not a missing field. It is computed as
    the set difference of reaction ids across the correction rather than read
    out of ``MSATPCorrection.filtered_noncore``, because the filter zeroes
    bounds first and only removes a reaction when BOTH bounds end up zero, so
    those two sets are different things.
    """
    result = built["result"]
    assert result.post_atp_correction_ran is True
    assert result.post_atp_correction_invocations == 1
    assert isinstance(result.post_atp_removed_reaction_ids, tuple)
    assert result.post_atp_test_count is not None
    assert result.post_atp_test_count > 0
    assert result.post_atp_selected_media_ids
    assert result.post_atp_core_gapfilling_count is not None

    # The one reaction added between the stages is ATPM, so the counts have to
    # reconcile exactly. This is what catches a second, silent correction run.
    expected = (
        result.draft_reaction_count + 1 - len(result.post_atp_removed_reaction_ids)
    )
    assert result.post_atp_reaction_count == expected
    assert any(
        rxn.id.startswith("ATPM") for rxn in built["mdlutl"].model.reactions
    )
    # The analysis blob says which stage it describes and survives json.dumps.
    assert result.post_atp_analysis["stage"] == "post-atp"
    json.dumps(result.post_atp_analysis, default=str)


@pytest.mark.kbu_model
@pytest.mark.slow
def test_provenance_revisions_are_populated_from_distribution_metadata(built):
    """ModelSEEDpy's revision is a real SHA; KBUtilLib's is honest either way.

    The ModelSEEDpy pin is a ``git+`` install, so pip records
    ``vcs_info.commit_id`` and the revision must be a 40-character SHA. A
    version string could not stand in for it: the fork and upstream both report
    ``0.4.2``/``0.4.3`` with materially different builders.

    KBUtilLib is normally installed editable, which records no commit at all, so
    the assertion is that it is EITHER a SHA or exactly ``"unknown"`` -- never
    something synthesised to look populated.
    """
    result = built["result"]
    assert result.modelseedpy_version != UNKNOWN_REVISION
    assert len(result.modelseedpy_revision) == 40
    int(result.modelseedpy_revision, 16)  # it is hex
    assert result.kbutillib_version != UNKNOWN_REVISION
    assert result.kbutillib_revision == UNKNOWN_REVISION or (
        len(result.kbutillib_revision) == 40
    )
    assert result.solver in ("glpk", "gurobi")
    assert result.template_id
    assert result.core_template_id


@pytest.mark.kbu_model
@pytest.mark.slow
def test_gapfill_records_one_per_occurrence_in_request_order(
    recon, built, genome, gs_template, core_template
):
    """Two media, one requested TWICE, one exclusion -- checked per occurrence.

    The request is ``[Glucose_minimal, Succinate_minimal, Glucose_minimal]``, so
    there must be THREE records with ``request_index`` 0, 1, 2 in that order and
    the caller's ids kept verbatim. Both glucose occurrences carry the same
    composition digest, which is how two records sharing an id are told apart
    from two records that merely look alike.

    The exclusion is a reaction the solver DOES otherwise pick and CAN route
    around -- see :data:`EXCLUDED_REACTION` for both measurements. So the
    assertions below distinguish the two ways this could pass for the wrong
    reason: the reaction is absent (enforced) AND gap-filling still integrated a
    solution that grows (not merely broken). It is supplied in the
    compartmentalised form that the raw blacklist cannot match, which also pins
    the base-id normalisation.
    """
    mdlutl = built["mdlutl"]
    glucose = _media(_GLUCOSE_MINIMAL, "Glucose_minimal")
    succinate_composition = {
        key: value for key, value in _GLUCOSE_MINIMAL.items() if key != "cpd00027"
    }
    succinate_composition["cpd00036"] = 10.0
    succinate = _media(succinate_composition, "Succinate_minimal")
    glucose_again = _media(_GLUCOSE_MINIMAL, "Glucose_minimal")
    requested = [glucose, succinate, glucose_again]

    row, solutions, _flux, _flux_media, records = recon.gapfill_metabolic_model(
        mdlutl=mdlutl,
        genome=genome,
        media_objs=requested,
        templates=[gs_template],
        core_template=core_template,
        reaction_exclusion_list=[EXCLUDED_REACTION],
        objective="bio1",
        minimum_objective=0.01,
        prefilter=False,
    )

    # One record per OCCURRENCE, in request order, ids verbatim.
    assert isinstance(records, tuple)
    assert len(records) == len(requested)
    assert [record.request_index for record in records] == [0, 1, 2]
    assert [record.media_id for record in records] == [
        "Glucose_minimal",
        "Succinate_minimal",
        "Glucose_minimal",
    ]

    # The repeated id keeps BOTH attempts, and they are distinguishable.
    first_glucose, succinate_record, second_glucose = records
    assert first_glucose.media_id == second_glucose.media_id
    assert first_glucose.media_digest == second_glucose.media_digest
    assert first_glucose.request_index != second_glucose.request_index
    assert first_glucose != second_glucose
    # ...whereas reporting keyed by media ID -- the shape this replaces -- can
    # only hold one of them.
    assert len({record.media_id for record in records}) == 2
    assert len(records) == 3

    # A different composition gets a different digest, under a different id.
    assert succinate_record.media_digest != first_glucose.media_digest

    # Gap-filling actually did something on the first occurrence, so the
    # exclusion assertion below is about an enforced constraint rather than
    # about a gap-fill that never ran.
    assert first_glucose.status == "integrated"
    assert first_glucose.additions
    assert first_glucose.objective_value is not None
    assert first_glucose.objective_value > 0

    # The SECOND request of the same medium has nothing left to do, and says so
    # rather than re-integrating. That distinction is not cosmetic: MSGapfill
    # clones its gap-fill model once in __init__, so the repeat solve returns
    # essentially the same ~155 reactions the model already has, and integrating
    # that no-op solution runs test_solution(keep_changes=True), which judges a
    # reaction "unneeded" by zeroing ONE bound and then removes the whole
    # reaction. Measured: growth on BOTH media went to 0.0 that way. So this
    # assertion is the regression test for a destroyed model, not a label check.
    assert second_glucose.status == "no_gaps"
    assert second_glucose.additions == ()
    assert second_glucose.reversals == ()
    assert second_glucose.objective_value is not None
    assert second_glucose.objective_value > 0

    # ...and the model still grows on the FIRST medium afterwards, which is the
    # property the guard exists to protect.
    assert _objective_value_on_media(
        mdlutl=mdlutl, media=glucose, objective="bio1"
    ) > 0

    # The excluded reaction is absent from the model that ships, in every
    # compartment -- base-id exclusion is the only semantics the blacklist has.
    model_reaction_ids = {rxn.id for rxn in mdlutl.model.reactions}
    assert EXCLUDED_REACTION not in model_reaction_ids
    assert not [
        rxn_id
        for rxn_id in model_reaction_ids
        if rxn_id.startswith(EXCLUDED_BASE_REACTION + "_")
        or rxn_id == EXCLUDED_BASE_REACTION
    ]
    for record in records:
        assert EXCLUDED_REACTION not in record.additions
        assert EXCLUDED_REACTION not in record.reversals

    # Every record's status is from the documented vocabulary, and nothing
    # raised.
    for record in records:
        assert record.status in ("integrated", "no_gaps", "no_solution"), record

    # POST-GAP-FILL facts attach without mutating the POST-ATP result.
    final = MSReconstructionUtils.with_gapfill_results(
        result=built["result"],
        mdlutl=mdlutl,
        gapfill_records=records,
        reaction_exclusion_list=[EXCLUDED_REACTION],
    )
    assert built["result"].post_gapfill_records == ()
    assert final.post_gapfill_records == records
    assert final.post_gapfill_reaction_exclusion_list == (EXCLUDED_REACTION,)
    assert final.post_gapfill_reaction_count == len(model_reaction_ids)
    assert final.post_gapfill_reaction_count > final.post_atp_reaction_count
    # The complete result is a report surface, so it must serialise.
    json.dumps(final.to_dict(), default=str)


@pytest.mark.kbu_model
def test_alias_only_function_builds_through_alias_matching(recon):
    """A gene carrying only a cluster's SECOND function builds -- via aliases.

    This is the KBDL mapping shape in miniature. KBDL emits one
    ``MSTemplateRole`` per mapping cluster, named after one of the cluster's
    functions and carrying the REST of the cluster's functions as ALIASES. A
    gene annotated with any of them must trigger the role.

    So the probe genome's single gene carries ``"Second enzyme"`` -- the name of
    template role ``r2``, which is NOT in the complex, and an ALIAS of role
    ``r1``, which is. Name-only matching cannot reach ``r1`` and must produce no
    reaction; alias matching must produce the reaction with that gene on it.
    The control is what makes the positive result mean anything.
    """
    from modelseedpy.core.mstemplate import (
        MSTemplate,
        MSTemplateCompartment,
        MSTemplateMetabolite,
        MSTemplateReaction,
        MSTemplateSpecies,
        NewModelTemplateComplex,
        NewModelTemplateRole,
    )

    def cluster_template():
        """One complex of one role whose SECOND function is an alias."""
        template = MSTemplate(template_id="kbdl_cluster")
        template.compartments.append(MSTemplateCompartment("c", "Cytosol", 3))
        template.add_compounds([MSTemplateMetabolite("cpd_cluster", "C", "cluster")])
        species = MSTemplateSpecies("cpd_cluster_c", 0, "c", "cpd_cluster")
        template.add_comp_compounds([species])
        in_complex = NewModelTemplateRole(
            "r1", "First enzyme", aliases=["Second enzyme"]
        )
        template.add_roles([in_complex])
        cluster = NewModelTemplateComplex("cpx_cluster", "cluster")
        cluster.add_role(role=in_complex, triggering=True, optional=False)
        template.add_complexes([cluster])
        reaction = MSTemplateReaction(
            "rxn_cluster_c", "rxn_cluster", "cluster", "", 0, 1000
        )
        reaction.add_metabolites({species: -1})
        reaction.add_complexes([cluster])
        template.add_reactions([reaction])
        return template

    msgenome = MSGenome()
    feature = MSFeature(feature_id="g0", sequence="M")
    feature.add_ontology_term(
        ontology_term=DEFAULT_ONTOLOGY_TERM, value="Second enzyme"
    )
    msgenome.add_features(feature_list=[feature])
    msgenome.id = "alias_probe"
    msgenome.scientific_name = "alias probe"

    common = {
        "genome": msgenome,
        "core_template": None,
        "atp_safe": False,
        "allow_incomplete_complexes": False,
        "ontology_term": DEFAULT_ONTOLOGY_TERM,
    }

    _row, control = recon.build_metabolic_model(
        gs_template_obj=cluster_template(), match_role_aliases=False, **common
    )
    assert "rxn_cluster_c0" not in control.model.reactions, (
        "the control matched by role NAME, so the alias result would prove "
        "nothing"
    )

    _row, matched = recon.build_metabolic_model(
        gs_template_obj=cluster_template(), match_role_aliases=True, **common
    )
    assert "rxn_cluster_c0" in matched.model.reactions
    reaction = matched.model.reactions.get_by_id("rxn_cluster_c0")
    assert {gene.id for gene in reaction.genes} == {"g0"}


@pytest.mark.kbu_model
def test_classify_genome_uses_the_shipped_npz_classifier(recon, genome):
    """The genome classifies, through the packaged ``.npz``, to ``N`` -> ``gn``.

    H. influenzae is gram-negative, so this checks the classifier is actually
    reading the genome rather than returning a constant, and that the class
    letter maps to the template key the build would use.
    """
    genome_class, template_key = recon.classify_genome(
        genome=genome, ontology_term=DEFAULT_ONTOLOGY_TERM
    )
    assert genome_class == "N"
    assert template_key == "gn"
