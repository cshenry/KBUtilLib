"""Real-dependency tests for the native role-based build (PRD part B).

These exercise the ACTUALLY INSTALLED ModelSEEDpy, because the thing under test
is whether ``build_metabolic_model`` builds a model natively from genome
functions matched to template roles -- and a fake cannot answer that.  Fork and
upstream both reach ``MSBuilder.build_base_model`` under the same name while
differing in which keywords it accepts, so nothing here infers capability from
a version string.

Two cases, deliberately:

1. **A full role-based build through ATP correction** on the committed
   *H. influenzae* RAST genome against the NATIVE gram-negative template plus
   the native core template.  Asserted: ATP correction ran exactly once, the
   set of reactions it removed is reported, the ATP scaffold appears only after
   the DRAFT boundary, and the ``ReconstructionResult`` revisions come from
   distribution metadata.  This is the expensive one -- a single module-scoped
   build that every assertion in :class:`TestRoleBasedBuildThroughATPCorrection`
   shares.

2. **Alias matching on a tiny in-memory template**, in the KBDL mapping shape:
   one template role per mapping cluster, named after the cluster's FIRST
   function and carrying every cluster function as an alias.  The gene carries
   only the SECOND function, so it is reachable through the alias and through
   nothing else.  No biochemistry, no ATP step, no solver.

Running them::

    KBU_REQUIRE_MODEL_STACK=1 \\
    KBU_RECON_BUILD_REPORT=/path/to/report.json \\
    /scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python -m pytest \\
        tests/modeling/test_recon_build_real_dependency.py -v

That venv has modelseedpy installed from
``git+https://github.com/cshenry/ModelSEEDpy.git@e1155a41fa53b921185b20c6cfbd083e5e3739a4``
(the ``model`` extra's pin).  Without the stack the module skips; with
``KBU_REQUIRE_MODEL_STACK=1`` it FAILS instead, because a silent skip is how an
unmeasured environment gets mistaken for a measured one.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import require_model_stack

pytestmark = pytest.mark.kbu_model

# Module-level: a module-scope skip happens during collection, too early for
# conftest's pytest_runtest_setup guard to convert it into a failure.
require_model_stack()

from kbutillib.core.dependency_manager import (  # noqa: E402
    DEFAULT_DEPENDENCIES_FILE,
)
from kbutillib.domains.modeling import ms_reconstruction_utils as recon  # noqa: E402

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "model"
    / "haemophilus_influenzae_71421_8_rast.json.gz"
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Optional path to dump the measurements this module takes, so a work record
#: can quote numbers rather than adjectives.
REPORT_ENV_VAR = "KBU_RECON_BUILD_REPORT"

#: As in part A: constructing ``MSReconstructionUtils`` still pulls in
#: ``KBModelUtils._import_modules``, which imports cobrakbase, so the checkout
#: has to resolve.  Severing that inheritance is not part of this PRD.
CONSTRUCTION_DEPENDENCIES = tuple(recon.RECONSTRUCTION_DEPENDENCIES) + ("cobrakbase",)

#: Every measurement taken here, keyed by case.
MEASUREMENTS: dict[str, Any] = {}

#: The KBDL mapping shape: a cluster's functions become the role's ALIASES and
#: the role is NAMED after the first of them.
_CLUSTER_FIRST_FUNCTION = "KBU cluster first function 90011"
_CLUSTER_SECOND_FUNCTION = "KBU cluster second function 90012"


def _write_report() -> None:
    target = os.environ.get(REPORT_ENV_VAR)
    if target:
        Path(target).write_text(json.dumps(MEASUREMENTS, indent=2, default=str))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def environment() -> dict:
    """Part A's environment initialisation, once for the module.

    Re-implementing any of it here would be a second, unverified copy; and it
    must run BEFORE ``MSReconstructionUtils`` is constructed, because the
    dependency forwarding is what makes ``get_data_path`` non-None and cobra
    binds its solver interface when the first ``Model`` is built.
    """
    if not Path(DEFAULT_DEPENDENCIES_FILE).expanduser().is_file():
        pytest.fail(
            f"dependency config absent: {DEFAULT_DEPENDENCIES_FILE}; the layer "
            "resolves ModelSEEDDatabase through it and never through "
            "modelseedpy's config.cfg default (KBDL issue #41)",
            pytrace=False,
        )
    report = recon.initialize_reconstruction_environment(
        dependencies=CONSTRUCTION_DEPENDENCIES
    )
    MEASUREMENTS["environment"] = report
    return report


@pytest.fixture(scope="module")
def utils(environment) -> Any:
    return recon.MSReconstructionUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )


@pytest.fixture(scope="module")
def genome(environment) -> Any:
    """The committed RAST-annotated *H. influenzae* genome as an MSGenome.

    Functions go under ONE ontology key, which is the shape KBDL supplies: the
    union of folded function strings from every annotation source under a single
    key.  The fixture carries no protein sequences, and nothing on the
    role-based path reads any.
    """
    from modelseedpy import MSGenome
    from modelseedpy.core.msgenome import MSFeature

    if not FIXTURE.is_file():
        pytest.fail(f"genome fixture absent: {FIXTURE}", pytrace=False)

    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        doc = json.load(handle)

    msgenome = MSGenome()
    features = []
    for feature_id, functions in doc["features"].items():
        feature = MSFeature(feature_id, "", description=functions[0])
        for function in functions:
            feature.add_ontology_term("RAST", function)
        features.append(feature)
    msgenome.add_features(features)
    msgenome.id = doc["genome_id"]
    msgenome.scientific_name = doc["scientific_name"]
    return msgenome


@pytest.fixture(scope="module")
def gs_template(environment) -> Any:
    """Native ModelSEED gram-negative template, SUPPLIED by the caller."""
    from modelseedpy.core.mstemplate import MSTemplateBuilder
    from modelseedpy.helpers import get_template

    return MSTemplateBuilder.from_dict(get_template("template_gram_neg")).build()


@pytest.fixture(scope="module")
def core_template(environment) -> Any:
    """Native ModelSEED core template, SUPPLIED by the caller."""
    from modelseedpy.core.mstemplate import MSTemplateBuilder
    from modelseedpy.helpers import get_template

    return MSTemplateBuilder.from_dict(get_template("template_core")).build()


@pytest.fixture(scope="module")
def role_based_build(utils, genome, gs_template, core_template) -> dict:
    """ONE full build through ATP correction, shared by every assertion.

    A real ATP correction over the 54 default ATP media takes minutes, so it is
    performed exactly once per module and the outcome is measured rather than
    re-derived.
    """
    started = time.monotonic()
    current_output, mdlutl, result = utils.build_metabolic_model(
        genome=genome,
        genome_classifier=None,
        # The caller supplies BOTH templates, so no classification happens.
        gs_template="auto",
        gs_template_obj=gs_template,
        core_template=core_template,
        model_id=genome.id,
        model_name=genome.scientific_name,
        atp_safe=True,
        # The native path: no injected reaction-to-gene mapping, so neither
        # roleless-path compensation runs.
        reactions_to_add=None,
        ontology_term="RAST",
        return_reconstruction_result=True,
    )
    elapsed = time.monotonic() - started

    MEASUREMENTS["role_based_build"] = {
        "wall_seconds": round(elapsed, 1),
        "current_output": {
            key: value
            for key, value in current_output.items()
            if key != "Comments"
        },
        "final_reaction_count": len(mdlutl.model.reactions),
        "final_gene_count": len(mdlutl.model.genes),
        "result": result.to_dict(),
    }
    _write_report()
    return {
        "current_output": current_output,
        "mdlutl": mdlutl,
        "result": result,
        "wall_seconds": elapsed,
    }


# ---------------------------------------------------------------------------
# Case 1: the full role-based build
# ---------------------------------------------------------------------------


class TestRoleBasedBuildThroughATPCorrection:
    def test_the_draft_is_genuinely_role_based(self, role_based_build):
        """Genes on the draft are the proof.

        The roleless path this layer replaces produced a draft with NO genes and
        no metabolic reactions at all; everything arrived afterwards from an
        injected mapping.  Smoke measurement
        (``agent-io/work-records/task-d1302dc4.md`` section (a)) on this exact
        genome and template: 957 reactions, 588 genes, 73 exchanges.
        """
        result = role_based_build["result"]

        assert result.draft_gene_count > 400, result.draft_gene_count
        assert result.draft_reaction_count > 800, result.draft_reaction_count
        assert result.draft_metabolite_count > 800, result.draft_metabolite_count

    def test_the_draft_carries_exchanges_biomass_and_an_objective(
        self, role_based_build
    ):
        """All three come free from ``build_base_model``.

        Reaching ATP correction with ZERO exchanges is what made every ATP medium
        fail and ``selected_media`` come back empty
        (KBDLJobRunningPrototype#28, cause 1).
        """
        result = role_based_build["result"]

        assert result.draft_exchange_count > 50, result.draft_exchange_count
        assert "bio1" in result.draft_biomass_ids, result.draft_biomass_ids
        assert "bio1" in result.draft_objective_expression

    def test_the_atp_maintenance_scaffold_is_absent_from_the_draft(
        self, role_based_build
    ):
        """The DRAFT boundary is BEFORE any ATP-specific insertion.

        ``build_base_model`` does not add ``ATPM_c0``; upstream adds it inside
        ``build()``, which this layer never calls.
        """
        assert role_based_build["result"].draft_atpm_present is False

    def test_atp_correction_ran_exactly_once(self, role_based_build):
        result = role_based_build["result"]

        assert result.post_atp_correction_ran is True
        assert result.post_atp_correction_invocations == 1

    def test_the_atp_scaffold_appears_only_after_the_draft(self, role_based_build):
        """``MSATPCorrection.__init__`` mints ``rxn00062_c0`` (ATP hydrolysis)
        through ``MSModelUtil.add_atp_hydrolysis``, which is precisely the
        "ATP-specific maintenance/scaffold insertion" the DRAFT boundary
        excludes."""
        result = role_based_build["result"]
        model = role_based_build["mdlutl"].model

        assert result.post_atp_analysis["atp_hydrolysis_id"] == "rxn00062_c0"
        assert "rxn00062_c0" in {rxn.id for rxn in model.reactions}

    def test_the_atp_removed_set_is_reported_and_internally_consistent(
        self, role_based_build
    ):
        """The removed set is a measured set difference over reaction ids.

        It is NOT ``MSATPCorrection.filtered_noncore``: the filter zeroes bounds
        first and removes a reaction only when both bounds end up zero, so the
        internal list and the set actually removed are different things.
        """
        result = role_based_build["result"]
        removed = result.post_atp_removed_reaction_ids
        final_ids = {rxn.id for rxn in role_based_build["mdlutl"].model.reactions}

        assert isinstance(removed, tuple)
        assert all(isinstance(rid, str) for rid in removed)
        assert list(removed) == sorted(removed), "the set must be reported sorted"
        # Nothing reported as removed may still be on the model.
        assert not (set(removed) & final_ids), set(removed) & final_ids
        # And the analysis dict must agree with the field.
        assert result.post_atp_analysis["removed_reaction_ids"] == list(removed)

    def test_the_post_atp_counts_describe_the_model_that_came_back(
        self, role_based_build
    ):
        result = role_based_build["result"]
        model = role_based_build["mdlutl"].model

        assert result.post_atp_reaction_count == len(model.reactions)

    def test_the_atp_media_were_evaluated(self, role_based_build):
        """``post_atp_selected_media_ids`` empty with correction having run means
        EVERY ATP medium failed -- the #28 signature.  The default media are
        pre-loaded by this layer because upstream's ``__init__`` cannot load
        them itself."""
        result = role_based_build["result"]

        assert result.post_atp_analysis["atp_media_count"] > 1
        assert result.post_atp_selected_media_ids, (
            "no ATP medium was accepted; with exchanges present on the draft "
            "this is the KBDLJobRunningPrototype#28 failure signature"
        )
        assert result.post_atp_test_count is not None

    def test_the_atp_tests_are_published_for_downstream_gapfill(
        self, role_based_build
    ):
        """Part C's gap-fill needs them to be ATP-safe without recomputing a
        full ATP run (#28, cause 3)."""
        mdlutl = role_based_build["mdlutl"]

        assert mdlutl.atp_tests is not None
        assert len(mdlutl.atp_tests) == (
            role_based_build["result"].post_atp_test_count
        )

    def test_no_gapfill_was_performed(self, role_based_build):
        """The whole reason ``build()`` is never called: it runs
        ``MSGapfill.run_gapfilling(gapfill_media)`` unconditionally, against
        media the caller never asked for."""
        assert role_based_build["result"].post_gapfill_results == ()

    def test_the_result_records_both_supplied_templates(
        self, role_based_build, gs_template, core_template
    ):
        result = role_based_build["result"]

        assert result.template_id == gs_template.id
        assert result.core_template_id == core_template.id

    def test_classification_was_skipped_because_templates_were_supplied(
        self, role_based_build
    ):
        """``gs_template="auto"`` with a supplied template is not ambiguous."""
        assert role_based_build["result"].genome_class is None

    def test_the_compensations_did_not_run_on_the_native_path(
        self, role_based_build
    ):
        assert role_based_build["result"].reactions_to_add_supplied is False

    def test_the_build_settings_are_recorded(self, role_based_build):
        result = role_based_build["result"]

        assert result.ontology_term == "RAST"
        assert result.match_role_aliases is False
        assert result.allow_incomplete_complexes is True

    def test_the_legacy_output_dict_is_still_populated(self, role_based_build):
        """``kb_build_metabolic_models`` and the CLI both read these keys."""
        current_output = role_based_build["current_output"]

        assert current_output["Genome"] == "71421.8"
        assert current_output["Model genes"] > 0
        assert current_output["Reactions"] > 0
        assert isinstance(current_output["Core GF"], int)


class TestProvenanceComesFromDistributionMetadata:
    def test_the_modelseedpy_revision_is_a_real_commit(self, role_based_build):
        revision = role_based_build["result"].modelseedpy_revision

        assert re.fullmatch(r"[0-9a-f]{40}", revision), revision

    def test_that_revision_is_the_one_pyproject_pins(self, role_based_build):
        """Guards against measuring one install and shipping another.

        A version string cannot do this job: the fork reports 0.4.2 and the pin
        reports 0.4.3 while ``MSBuilder.build`` differs in arity and return
        type, and neither number names a commit.
        """
        pyproject = (REPO_ROOT / "pyproject.toml").read_text()
        pinned = re.search(r"ModelSEEDpy\.git@([0-9a-f]{40})", pyproject)
        assert pinned, "pyproject.toml declares no SHA-pinned ModelSEEDpy"

        assert role_based_build["result"].modelseedpy_revision == pinned.group(1)

    def test_the_modelseedpy_version_is_reported_too(self, role_based_build):
        version = role_based_build["result"].modelseedpy_version

        assert version != recon.UNKNOWN_REVISION
        assert version != ""

    def test_the_kbutillib_revision_is_never_synthesised(self, role_based_build):
        """An editable install records ``dir_info.editable`` and NO commit, so
        ``"unknown"`` is the correct answer here -- not the SHA of whatever the
        source tree happens to be checked out at."""
        revision = role_based_build["result"].kbutillib_revision

        assert revision == recon.UNKNOWN_REVISION or re.fullmatch(
            r"[0-9a-f]{40}", revision
        ), revision

    def test_the_result_survives_json_round_tripping(self, role_based_build):
        """A result that cannot be serialised cannot be reported, and
        ``MSATPCorrection`` keys ``media_gapfill_stats`` by the ``MSMedia``
        OBJECT, which makes ``json.dumps`` raise."""
        payload = json.dumps(role_based_build["result"].to_dict(), default=str)

        assert json.loads(payload)["draft_biomass_ids"] == list(
            role_based_build["result"].draft_biomass_ids
        )


# ---------------------------------------------------------------------------
# Case 2: alias matching, KBDL mapping shape, in memory
# ---------------------------------------------------------------------------


def _cluster_shaped_template() -> Any:
    """A one-reaction template in the KBDL ``kbdl-mapping`` shape.

    Returns:
        An in-memory ``MSTemplate`` with ONE role representing one mapping
        cluster: the role is NAMED after the cluster's first function and
        carries BOTH cluster functions as aliases.  One triggering, non-optional
        single-subunit complex, and one reaction it drives.

    Nothing about this needs the biochemistry database, the network or a solver,
    which is why it belongs in a test rather than in a job.
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

    template = MSTemplate(
        "kbu.cluster.probe",
        name="KBUtilLibClusterProbeTemplate",
        domain="Bacteria",
        template_type="GenomeScale",
    )
    template.add_compartments(
        [
            MSTemplateCompartment("c", "Cytosol", 7.0, 0),
            MSTemplateCompartment("e", "Extracellular", 7.0, 1),
        ]
    )
    template.add_compounds(
        [
            MSTemplateMetabolite("cpd90011", "C", "cluster probe compound A"),
            MSTemplateMetabolite("cpd90012", "C", "cluster probe compound B"),
        ]
    )
    template.add_comp_compounds(
        [
            MSTemplateSpecies("cpd90011_c", 0, "c", "cpd90011"),
            MSTemplateSpecies("cpd90012_c", 0, "c", "cpd90012"),
        ]
    )

    cluster_role = NewModelTemplateRole(
        "cluster.role.1",
        _CLUSTER_FIRST_FUNCTION,
        aliases=[_CLUSTER_FIRST_FUNCTION, _CLUSTER_SECOND_FUNCTION],
    )
    template.add_roles([cluster_role])

    cluster_complex = NewModelTemplateComplex("cluster.cpx.1", "cluster probe complex")
    cluster_complex.add_role(cluster_role, triggering=True, optional=False)
    template.add_complexes([cluster_complex])

    reaction = MSTemplateReaction(
        "rxn90011_c", "rxn90011", "cluster probe reaction", "probe", 0.0, 1000.0
    )
    reaction.add_metabolites(
        {
            template.compcompounds.get_by_id("cpd90011_c"): -1,
            template.compcompounds.get_by_id("cpd90012_c"): 1,
        }
    )
    reaction.add_complexes([cluster_complex])
    template.add_reactions([reaction])
    return template


def _second_function_only_genome() -> Any:
    """One gene carrying ONLY the cluster's SECOND function.

    Returns:
        An ``MSGenome`` with a single feature whose sole ``RAST`` ontology term
        is :data:`_CLUSTER_SECOND_FUNCTION`.  It matches the role's name under
        no circumstances, and matches through the ALIAS or not at all.
    """
    from modelseedpy import MSGenome
    from modelseedpy.core.msgenome import MSFeature

    genome = MSGenome()
    feature = MSFeature(
        "cluster_gene_second", "", description=_CLUSTER_SECOND_FUNCTION
    )
    feature.add_ontology_term("RAST", _CLUSTER_SECOND_FUNCTION)
    genome.add_features([feature])
    genome.id = "cluster_probe_genome"
    genome.scientific_name = "Cluster probe organism"
    return genome


def _build_cluster_probe(utils, *, match_role_aliases: bool) -> dict:
    """One alias-probe build. No ATP step, so it costs milliseconds."""
    _current_output, mdlutl, result = utils.build_metabolic_model(
        genome=_second_function_only_genome(),
        genome_classifier=None,
        gs_template="gn",
        gs_template_obj=_cluster_shaped_template(),
        core_template=None,
        atp_safe=False,
        reactions_to_add=None,
        ontology_term="RAST",
        match_role_aliases=match_role_aliases,
        return_reconstruction_result=True,
    )
    return {
        "gene_ids": sorted(gene.id for gene in mdlutl.model.genes),
        "reaction_ids": sorted(rxn.id for rxn in mdlutl.model.reactions),
        "result": result,
    }


@pytest.fixture(scope="module")
def cluster_probe(utils) -> dict:
    outcome = {
        "aliases_off": _build_cluster_probe(utils, match_role_aliases=False),
        "aliases_on": _build_cluster_probe(utils, match_role_aliases=True),
    }
    MEASUREMENTS["cluster_alias_probe"] = {
        key: {
            "gene_ids": value["gene_ids"],
            "reaction_ids": value["reaction_ids"],
            "draft_gene_count": value["result"].draft_gene_count,
            "draft_reaction_count": value["result"].draft_reaction_count,
        }
        for key, value in outcome.items()
    }
    _write_report()
    return outcome


class TestAliasMatchingOnTheClusterShape:
    """A mapping cluster's SECOND function must be enough.

    KBDL builds one ``MSTemplateRole`` per mapping cluster, named after one of
    the cluster's functions and carrying them all as aliases.  A gene annotated
    with any OTHER member of the cluster therefore reaches the reaction only
    through alias matching -- which is the entire reason the keyword exists and
    is opt-in (native ModelSEED templates alias 20,551 of 20,552 core roles with
    identifiers such as ``msfr.1407``, so matching aliases is not free).
    """

    def test_the_gene_is_absent_without_alias_matching(self, cluster_probe):
        assert cluster_probe["aliases_off"]["gene_ids"] == []

    def test_the_gene_arrives_through_the_alias(self, cluster_probe):
        assert cluster_probe["aliases_on"]["gene_ids"] == ["cluster_gene_second"]

    def test_the_reaction_the_cluster_drives_arrives_with_it(self, cluster_probe):
        on = cluster_probe["aliases_on"]["reaction_ids"]
        off = cluster_probe["aliases_off"]["reaction_ids"]

        assert "rxn90011_c0" in on, on
        assert "rxn90011_c0" not in off, off

    def test_the_flag_is_recorded_on_the_result(self, cluster_probe):
        assert cluster_probe["aliases_on"]["result"].match_role_aliases is True
        assert cluster_probe["aliases_off"]["result"].match_role_aliases is False

    def test_no_atp_correction_ran_on_the_probe(self, cluster_probe):
        """Asserted so a future change cannot quietly make the cheap case
        expensive."""
        for case in cluster_probe.values():
            assert case["result"].post_atp_correction_ran is False
            assert case["result"].post_atp_correction_invocations == 0
            assert case["result"].core_template_id is None
