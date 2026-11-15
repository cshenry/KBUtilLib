"""Real-dependency test for per-medium gap-fill (PRD part C, kbu-recon-gapfill).

One case, deliberately: BUILD the *H. influenzae* model through part B's native
role-based path, then gap-fill it on TWO media with ONE exclusion, with the
first medium requested TWICE.  Three occurrences, two ids.

What only the real ModelSEEDpy can decide, and is therefore here rather than in
``tests/modeling/test_recon_gapfill.py``:

* that ``MSGapfill.run_gapfilling`` + ``integrate_gapfill_solution`` driven per
  occurrence actually gap-fills a POST-ATP model (upstream's
  ``run_multi_gapfill`` cannot be used: at the pinned revision it accepts none
  of ``gapfilling_mode``, ``run_sensitivity_analysis`` or
  ``integrate_solutions``, integrates nothing, and keys its return by media
  OBJECT -- so it cannot represent a repeated request);
* that ``reaction_exclusion_list`` keeps a reaction OUT when that reaction is a
  real candidate the solver has been measured choosing;
* that a medium requested twice yields two records, the second honestly
  reporting that the first already integrated what that medium needed.

TWO gap-fill phases on ONE build, because the exclusion assertion is only worth
anything if it is not vacuous and the cheapest way to show that is to measure
both outcomes:

* **Phase 1, enforcement.**  Exclude ``rxn00029`` -- porphobilinogen synthase,
  the committed step of HAEM biosynthesis.  MEASURED: the gap-fill database
  cannot then reach biomass on either medium, so every occurrence comes back
  ``"no_solution"`` having added nothing (and therefore leaves the model
  untouched for phase 2).  That is the strongest available evidence that the
  exclusion reaches the LP rather than being filtered afterwards: blacklisting
  one essential reaction makes the whole problem infeasible.  It is also cheap
  -- the database test fails in a few seconds per occurrence.
* **Phase 2, the task's case.**  Exclude ``rxn00430`` -- methanol:NAD+
  oxidoreductase.  The smoke measured gap-filling CHOOSE ``rxn00430_c0`` for
  this genome against this template on an aerobic glucose minimal medium
  (``agent-io/research/task-d1302dc4-recon-env-smoke-measurements.json``,
  ``d_gapfill[*].new_sample``), so it is genuinely on the menu; and its
  substrate (methanol, ``cpd00116``) is in neither medium and is not a biomass
  precursor, so excluding it cannot make the problem infeasible the way
  ``rxn00029`` does.  Gap-filling therefore succeeds AND the excluded reaction
  is absent.

Together the two phases are an A/B on one model: ``rxn00029_c0`` is absent while
it is excluded and present once it is not, with everything else held fixed.

Running it::

    KBU_REQUIRE_MODEL_STACK=1 \\
    KBU_RECON_GAPFILL_REPORT=/path/to/report.json \\
    /scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python -m pytest \\
        tests/modeling/test_recon_gapfill_real_dependency.py -v

That venv has modelseedpy installed from
``git+https://github.com/cshenry/ModelSEEDpy.git@e1155a41fa53b921185b20c6cfbd083e5e3739a4``
(the ``model`` extra's pin).  Without the stack the module skips; with
``KBU_REQUIRE_MODEL_STACK=1`` it FAILS instead, because a silent skip is how an
unmeasured environment gets mistaken for a measured one.

A build through ATP correction plus two gap-fill solves is minutes of wall
clock, so there is ONE module-scoped fixture and every assertion reads its
outcome rather than re-deriving it.
"""

from __future__ import annotations

import gzip
import json
import os
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

#: Optional path to dump the measurements this module takes, so a work record
#: can quote numbers rather than adjectives.
REPORT_ENV_VAR = "KBU_RECON_GAPFILL_REPORT"

#: As in parts A and B: constructing ``MSReconstructionUtils`` still pulls in
#: ``KBModelUtils._import_modules``, which imports cobrakbase.
CONSTRUCTION_DEPENDENCIES = tuple(recon.RECONSTRUCTION_DEPENDENCIES) + ("cobrakbase",)

#: Phase 2's exclusion: the reaction gap-filling must not add.  Supplied in BASE
#: form; the compartment-suffixed form is what would arrive on the model.
#: methanol:NAD+ oxidoreductase -- measured-chosen by gap-filling on this
#: genome/template/medium, and harmless to exclude (see the module docstring).
EXCLUDED_REACTION = "rxn00430"
EXCLUDED_REACTION_IN_MODEL = "rxn00430_c0"

#: Phase 1's exclusion: porphobilinogen synthase, the committed step of haem
#: biosynthesis.  Excluding it makes the gap-fill database unable to reach
#: biomass, which is what makes phase 1 evidence that the blacklist reaches the
#: LP.
ESSENTIAL_EXCLUDED_REACTION = "rxn00029"
ESSENTIAL_EXCLUDED_REACTION_IN_MODEL = "rxn00029_c0"

#: Ids the caller gives its media.  Both are used verbatim; the layer renames
#: nothing.  ``MSMedia.from_dict`` names every medium it builds ``"media"``,
#: which is exactly why the ids have to be set by whoever owns them.
GLUCOSE_MEDIA_ID = "KBU-Glc-O2-minimal"
SUCCINATE_MEDIA_ID = "KBU-Succ-O2-minimal"

MEASUREMENTS: dict[str, Any] = {}


def _record_to_dict(record: Any) -> dict:
    return {
        "request_index": record.request_index,
        "media_id": record.media_id,
        "media_digest": record.media_digest,
        "status": record.status,
        "n_additions": len(record.additions),
        "additions_sample": list(record.additions[:25]),
        "n_reversals": len(record.reversals),
        "reversals_sample": list(record.reversals[:25]),
        "objective_value": record.objective_value,
    }


def _write_report() -> None:
    target = os.environ.get(REPORT_ENV_VAR)
    if target:
        Path(target).write_text(json.dumps(MEASUREMENTS, indent=2, default=str))


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------

#: Everything but the carbon source.  The same 19 compounds the smoke used for
#: its gap-fill measurement (section (d)), so the 35-50 s it measured is the
#: right expectation for one solve here.
_MINIMAL_SALTS = {
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


def _minimal_media(*, media_id: str, carbon_source: str) -> Any:
    from modelseedpy.core.msmedia import MSMedia

    media = MSMedia.from_dict({carbon_source: 10.0, **_MINIMAL_SALTS})
    media.id = media_id
    media.name = media_id
    return media


# ---------------------------------------------------------------------------
# Fixtures -- the build half is part B's, unchanged
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def environment() -> dict:
    """Part A's environment initialisation, once for the module."""
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
    """The committed RAST-annotated *H. influenzae* genome as an MSGenome,
    functions folded under ONE ontology key -- the shape KBDL supplies."""
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
def gapfilled(utils, genome, gs_template, core_template) -> dict:
    """ONE build, then TWO gap-fill phases, shared by every assertion.

    Phase 1 excludes an ESSENTIAL reaction and is expected to add nothing, so
    it leaves the model as the build left it for phase 2.  Phase 2 is the
    task's case: three occurrences (glucose, succinate, glucose AGAIN) with one
    harmless exclusion.  See the module docstring for why both are here.

    ``link_gaps_to_objective=False`` in phase 2.  That parameter drives
    ``integrate_gapfill_solution``'s gap-fill SENSITIVITY analysis, which is
    reporting rather than a stage mutation and costs one knock-out solve per
    reaction of the cumulative solution -- on the order of a hundred and sixty
    extra LP solves per integrated occurrence on this model.  The task caps
    real-dependency runs at three, so it is off here and its forwarding is
    covered on fakes in ``tests/modeling/test_recon_gapfill.py``.
    """
    build_started = time.monotonic()
    _current_output, mdlutl, build_result = utils.build_metabolic_model(
        genome=genome,
        genome_classifier=None,
        gs_template="auto",
        gs_template_obj=gs_template,
        core_template=core_template,
        model_id=genome.id,
        model_name=genome.scientific_name,
        atp_safe=True,
        reactions_to_add=None,
        ontology_term="RAST",
        return_reconstruction_result=True,
    )
    build_seconds = time.monotonic() - build_started

    pre_gapfill_ids = {rxn.id for rxn in mdlutl.model.reactions}
    MEASUREMENTS["build"] = {
        "wall_seconds": round(build_seconds, 1),
        "pre_gapfill_reaction_count": len(pre_gapfill_ids),
        "pre_gapfill_atp_test_count": build_result.post_atp_test_count,
        "excluded_reaction_in_pre_gapfill_model": (
            EXCLUDED_REACTION_IN_MODEL in pre_gapfill_ids
        ),
        "essential_excluded_reaction_in_pre_gapfill_model": (
            ESSENTIAL_EXCLUDED_REACTION_IN_MODEL in pre_gapfill_ids
        ),
    }
    _write_report()

    glucose = _minimal_media(
        media_id=GLUCOSE_MEDIA_ID, carbon_source="cpd00027"  # D-Glucose
    )
    succinate = _minimal_media(
        media_id=SUCCINATE_MEDIA_ID, carbon_source="cpd00036"  # Succinate
    )

    # ---- Phase 1: exclude an essential reaction -------------------------
    phase1_started = time.monotonic()
    (
        _p1_output,
        _p1_solutions,
        _p1_solution,
        _p1_media,
        phase1_records,
    ) = utils.gapfill_metabolic_model(
        mdlutl=mdlutl,
        genome=genome,
        media_objs=[glucose, succinate],
        templates=[gs_template],
        core_template=core_template,
        atp_safe=True,
        reaction_exclusion_list=[ESSENTIAL_EXCLUDED_REACTION],
        objective="bio1",
        minimum_objective=0.01,
        link_gaps_to_objective=False,
        return_gapfill_records=True,
    )
    phase1_seconds = time.monotonic() - phase1_started
    after_phase1_ids = {rxn.id for rxn in mdlutl.model.reactions}

    MEASUREMENTS["phase1_essential_exclusion"] = {
        "excluded": ESSENTIAL_EXCLUDED_REACTION,
        "wall_seconds": round(phase1_seconds, 1),
        "records": [_record_to_dict(r) for r in phase1_records],
        "model_unchanged": after_phase1_ids == pre_gapfill_ids,
        "n_added": len(after_phase1_ids - pre_gapfill_ids),
        "n_removed": len(pre_gapfill_ids - after_phase1_ids),
    }
    _write_report()

    # ---- Phase 2: the task's case ---------------------------------------
    media_objs = [glucose, succinate, glucose]
    phase2_started = time.monotonic()
    (
        current_output,
        solutions,
        output_solution,
        output_solution_media,
        records,
    ) = utils.gapfill_metabolic_model(
        mdlutl=mdlutl,
        genome=genome,
        media_objs=media_objs,
        templates=[gs_template],
        core_template=core_template,
        atp_safe=True,
        reaction_exclusion_list=[EXCLUDED_REACTION],
        objective="bio1",
        minimum_objective=0.01,
        link_gaps_to_objective=False,
        return_gapfill_records=True,
    )
    phase2_seconds = time.monotonic() - phase2_started

    staged = utils.with_gapfill_results(
        result=build_result, gapfill_records=records
    )

    post_gapfill_ids = {rxn.id for rxn in mdlutl.model.reactions}
    recorded_additions: set = set()
    for record in records:
        recorded_additions |= set(record.additions)

    MEASUREMENTS["phase2_gapfill"] = {
        "excluded": EXCLUDED_REACTION,
        "wall_seconds": round(phase2_seconds, 1),
        "requested_media_ids": [m.id for m in media_objs],
        "records": [_record_to_dict(r) for r in records],
        "current_output": {
            key: value for key, value in current_output.items() if key != "Comments"
        },
        "comments": current_output["Comments"],
        "n_solution_keys": len(solutions),
        "output_solution_media_id": getattr(output_solution_media, "id", None),
        "output_solution_is_none": output_solution is None,
        "post_gapfill_reaction_count": len(post_gapfill_ids),
        "n_net_added": len(post_gapfill_ids - after_phase1_ids),
        "n_removed_from_pre_gapfill_model": len(after_phase1_ids - post_gapfill_ids),
        "n_recorded_but_absent_from_final_model": len(
            recorded_additions - post_gapfill_ids
        ),
        "excluded_reaction_in_post_gapfill_model": (
            EXCLUDED_REACTION_IN_MODEL in post_gapfill_ids
        ),
        # The A/B: phase 1 excluded this and it never arrived; phase 2 did not,
        # and it should.
        "essential_excluded_reaction_added_when_not_excluded": (
            ESSENTIAL_EXCLUDED_REACTION_IN_MODEL in recorded_additions
        ),
        "post_gapfill_results_len": len(staged.post_gapfill_results),
    }
    _write_report()

    return {
        "mdlutl": mdlutl,
        "build_result": build_result,
        "staged": staged,
        "records": records,
        "phase1_records": phase1_records,
        "current_output": current_output,
        "solutions": solutions,
        "output_solution": output_solution,
        "output_solution_media": output_solution_media,
        "media_objs": media_objs,
        "pre_gapfill_ids": pre_gapfill_ids,
        "after_phase1_ids": after_phase1_ids,
        "post_gapfill_ids": post_gapfill_ids,
        "recorded_additions": recorded_additions,
    }


# ---------------------------------------------------------------------------
# The exclusion was of something genuinely on the menu
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "excluded", [EXCLUDED_REACTION, ESSENTIAL_EXCLUDED_REACTION]
)
def test_the_excluded_reactions_are_real_template_candidates(excluded, gs_template):
    """Guards the exclusion assertions against being vacuous.

    An exclusion of a reaction no template offers would be trivially
    "honoured".  Both are in the supplied gram-negative template, and the smoke
    measured gap-filling CHOOSE both for this genome against this template on
    an aerobic glucose minimal medium.
    """
    reference_ids = {rxn.reference_id for rxn in gs_template.reactions}
    template_ids = {rxn.id for rxn in gs_template.reactions}

    assert excluded in reference_ids
    assert f"{excluded}_c" in template_ids


# ---------------------------------------------------------------------------
# Per-occurrence records
# ---------------------------------------------------------------------------


class TestPerOccurrenceRecords:
    def test_there_is_one_record_per_occurrence_in_request_order(self, gapfilled):
        records = gapfilled["records"]

        assert isinstance(records, tuple)
        assert len(records) == 3
        assert [r.request_index for r in records] == [0, 1, 2]
        assert [r.media_id for r in records] == [
            GLUCOSE_MEDIA_ID,
            SUCCINATE_MEDIA_ID,
            GLUCOSE_MEDIA_ID,
        ]

    def test_both_attempts_at_the_repeated_medium_are_recorded(self, gapfilled):
        """``run_multi_gapfill`` keys its return by the media OBJECT, so the
        third occurrence would have overwritten the first and vanished."""
        records = gapfilled["records"]
        glucose_records = [
            r for r in records if r.media_id == GLUCOSE_MEDIA_ID
        ]

        assert len(glucose_records) == 2
        assert [r.request_index for r in glucose_records] == [0, 2]
        # Same medium, so the same composition digest -- which is what makes
        # the pair identifiable AS a repeat rather than two different media
        # that happen to share an id.
        assert glucose_records[0].media_digest == glucose_records[1].media_digest

    def test_the_media_ids_are_the_callers_own(self, gapfilled):
        """``MSMedia.from_dict`` names every medium it builds ``"media"``; these
        ids were set by the caller and are reported verbatim."""
        assert {r.media_id for r in gapfilled["records"]} == {
            GLUCOSE_MEDIA_ID,
            SUCCINATE_MEDIA_ID,
        }

    def test_the_digest_distinguishes_the_two_compositions(self, gapfilled):
        records = gapfilled["records"]

        glucose_digest = records[0].media_digest
        succinate_digest = records[1].media_digest
        assert glucose_digest.startswith("sha256:")
        assert glucose_digest != succinate_digest

    def test_every_status_is_one_of_the_documented_values(self, gapfilled):
        allowed = {"integrated", "no_gaps", "no_solution"}
        statuses = [r.status for r in gapfilled["records"]]

        assert all(
            status in allowed or status.startswith("error:")
            for status in statuses
        ), statuses
        # Nothing may have raised: an error status here means the real stack
        # could not do the thing this test exists to measure.
        assert not any(s.startswith("error:") for s in statuses), statuses

    def test_the_repeat_has_nothing_left_to_do(self, gapfilled):
        """Sequential integration means gap-filling only what is STILL missing.

        Once the first glucose occurrence has brought the model to the
        objective on glucose, the third occurrence must NOT gap-fill again:
        ``MSGapfill`` clones its gap-fill model once, in ``__init__``, so a
        later occurrence would be solved against a model carrying none of the
        earlier additions and return a stale solution whose integration runs
        ``test_solution(keep_changes=True)`` -- which REMOVES reactions.
        """
        records = gapfilled["records"]
        first, repeat = records[0], records[2]

        if first.objective_value is not None and first.objective_value >= 0.01:
            assert repeat.status == "no_gaps", (first.status, repeat.status)
            assert repeat.additions == ()
            assert repeat.reversals == ()
        else:
            # The model still does not meet the objective on glucose, so the
            # repeat is attempted -- and must report its own outcome, not the
            # first one's.
            assert repeat.status != "no_gaps", repeat.status

    def test_additions_are_disjoint_across_occurrences(self, gapfilled):
        """Each record reports what ITS occurrence added, measured as a set
        difference over the model's reaction ids."""
        seen: set = set()
        for record in gapfilled["records"]:
            assert not (set(record.additions) & seen), (record.media_id, seen)
            seen |= set(record.additions)

    def test_the_first_occurrences_additions_are_genuinely_new(self, gapfilled):
        """Measured as a set difference over the model's reaction ids across
        the occurrence, so nothing the build already produced can be claimed as
        a gap-fill addition."""
        assert set(gapfilled["records"][0].additions).isdisjoint(
            gapfilled["after_phase1_ids"]
        )

    def test_every_net_addition_is_attributed_to_an_occurrence(self, gapfilled):
        """Equality is NOT asserted in the other direction: a later
        occurrence's ``integrate_gapfill_solution`` can prune a reaction an
        earlier one added (``test_solution(keep_changes=True)`` removes
        reactions it judges unneeded), so a recorded addition may legitimately
        be absent from the final model.  How many, if any, is in the
        measurement report as ``n_recorded_but_absent_from_final_model``."""
        net_added = gapfilled["post_gapfill_ids"] - gapfilled["after_phase1_ids"]

        assert net_added <= gapfilled["recorded_additions"]


# ---------------------------------------------------------------------------
# The exclusion is enforced
# ---------------------------------------------------------------------------


class TestReactionExclusion:
    def test_the_excluded_reaction_is_absent_from_the_gapfilled_model(
        self, gapfilled
    ):
        """The measurement this case exists for."""
        assert EXCLUDED_REACTION_IN_MODEL not in gapfilled["post_gapfill_ids"]

    def test_no_compartment_of_the_excluded_reaction_arrived(self, gapfilled):
        """The blacklist matches BASE ids, so the exclusion covers every
        compartment -- and so must the check."""
        arrived = sorted(
            rxn_id
            for rxn_id in gapfilled["post_gapfill_ids"]
            if recon._base_reaction_id(rxn_id) == EXCLUDED_REACTION
        )

        assert arrived == []

    def test_it_was_not_in_the_model_before_gapfill_either(self, gapfilled):
        """Which is what makes its absence afterwards attributable to the
        exclusion rather than to it never having been a gap-fill candidate on
        this model."""
        assert EXCLUDED_REACTION_IN_MODEL not in gapfilled["pre_gapfill_ids"]

    def test_gapfilling_did_add_something_so_the_exclusion_is_not_vacuous(
        self, gapfilled
    ):
        """If gap-filling added nothing at all, "the excluded reaction is
        absent" would be a statement about a no-op."""
        net_added = gapfilled["post_gapfill_ids"] - gapfilled["after_phase1_ids"]

        assert net_added, "gap-filling added no reactions at all"

    def test_at_least_one_occurrence_integrated_a_solution(self, gapfilled):
        """The layer's job.  A module that passed without ever gap-filling
        anything would be measuring the plumbing and not the work."""
        statuses = [r.status for r in gapfilled["records"]]

        assert "integrated" in statuses, statuses


class TestPhase1TheExclusionReachesTheLP:
    """Excluding ``rxn00029`` -- porphobilinogen synthase, the committed step of
    haem biosynthesis, and haem is a biomass precursor of this template -- makes
    ``GapfillingPkg.test_gapfill_database`` unable to reach the objective, so
    ``run_gapfilling`` returns ``None`` before solving.

    That is the strongest available evidence that ``reaction_exclusion_list``
    reaches the LP as a blacklist on the gap-fill DATABASE, rather than being
    applied to a solution after the fact: a post-hoc filter could not make the
    problem infeasible.
    """

    def test_no_occurrence_found_a_solution(self, gapfilled):
        records = gapfilled["phase1_records"]

        assert [r.status for r in records] == ["no_solution", "no_solution"]

    def test_nothing_was_added(self, gapfilled):
        for record in gapfilled["phase1_records"]:
            assert record.additions == ()
            assert record.reversals == ()

    def test_the_model_is_unchanged_so_phase_two_starts_from_the_build(
        self, gapfilled
    ):
        assert gapfilled["after_phase1_ids"] == gapfilled["pre_gapfill_ids"]

    def test_the_essential_reaction_never_arrived_while_it_was_excluded(
        self, gapfilled
    ):
        """Checked against the model AS PHASE 1 LEFT IT, not the final one:
        phase 2 does not exclude this reaction, and the next test is that it
        arrives there."""
        assert (
            ESSENTIAL_EXCLUDED_REACTION_IN_MODEL
            not in gapfilled["after_phase1_ids"]
        )

    def test_the_same_reaction_arrives_once_it_is_no_longer_excluded(
        self, gapfilled
    ):
        """The A/B that makes the exclusion assertions non-vacuous.

        Same model, same two media, same template, same solver: with
        ``rxn00029`` excluded nothing could be gap-filled and it never arrived;
        with it NOT excluded (phase 2), gap-filling chose ``rxn00029_c0`` and
        put it on the model.  So its absence in phase 1 is attributable to the
        exclusion and to nothing else.
        """
        first_occurrence_additions = set(gapfilled["records"][0].additions)

        assert ESSENTIAL_EXCLUDED_REACTION_IN_MODEL in first_occurrence_additions
        assert (
            ESSENTIAL_EXCLUDED_REACTION_IN_MODEL in gapfilled["post_gapfill_ids"]
        )

    def test_each_occurrence_still_reports_its_measured_objective(self, gapfilled):
        """A failed gap-fill is still a measurement: the record carries the
        objective value the model actually reached on that medium."""
        for record in gapfilled["phase1_records"]:
            assert record.objective_value is not None
            assert record.objective_value < 0.01


# ---------------------------------------------------------------------------
# Staging into the ReconstructionResult
# ---------------------------------------------------------------------------


class TestStagedIntoTheReconstructionResult:
    def test_build_leaves_the_field_empty(self, gapfilled):
        assert gapfilled["build_result"].post_gapfill_results == ()

    def test_the_staged_result_carries_the_records_in_request_order(
        self, gapfilled
    ):
        staged = gapfilled["staged"]

        assert staged.post_gapfill_results == gapfilled["records"]
        assert [
            r.request_index for r in staged.post_gapfill_results
        ] == [0, 1, 2]

    def test_the_build_result_was_not_mutated(self, gapfilled):
        """``ReconstructionResult`` is frozen: staging returns a copy."""
        assert gapfilled["build_result"].post_gapfill_results == ()
        assert gapfilled["staged"] is not gapfilled["build_result"]

    def test_the_post_atp_facts_survive_staging(self, gapfilled):
        build_result, staged = gapfilled["build_result"], gapfilled["staged"]

        assert staged.post_atp_correction_invocations == 1
        assert (
            staged.post_atp_reaction_count == build_result.post_atp_reaction_count
        )
        assert staged.draft_reaction_count == build_result.draft_reaction_count

    def test_the_staged_result_is_json_serialisable(self, gapfilled):
        payload = json.loads(json.dumps(gapfilled["staged"].to_dict(), default=str))

        assert len(payload["post_gapfill_results"]) == 3
        assert payload["post_gapfill_results"][0]["media_id"] == GLUCOSE_MEDIA_ID


# ---------------------------------------------------------------------------
# The legacy return elements still mean what they meant
# ---------------------------------------------------------------------------


class TestTheLegacyReturnElements:
    def test_the_current_output_dict_is_filled(self, gapfilled):
        current_output = gapfilled["current_output"]

        assert current_output["Reactions"] == (
            gapfilled["mdlutl"].nonexchange_reaction_count()
        )
        assert current_output["Model genes"] == len(
            gapfilled["mdlutl"].model.genes
        )
        assert isinstance(current_output["GS GF"], int)

    def test_growth_is_reported_per_occurrence_that_produced_a_value(
        self, gapfilled
    ):
        """One entry per occurrence that had an objective value, in request
        order.  Against upstream the previous implementation always produced an
        empty string: it read ``solutions[media]["growth"]``, and upstream's
        ``run_gapfilling`` result has no ``growth`` key (its keys are
        ``binary_check, media, minobjective, new, reversed, target``)."""
        entries = [
            entry
            for entry in gapfilled["current_output"]["Growth"].split("<br>")
            if entry
        ]
        with_values = [
            r for r in gapfilled["records"] if r.objective_value is not None
        ]

        assert len(entries) == len(with_values)
        assert all(":" in entry for entry in entries)
        assert [entry.rsplit(":", 1)[0] for entry in entries] == [
            r.media_id for r in with_values
        ]

    def test_solutions_is_keyed_by_the_media_objects_that_were_integrated(
        self, gapfilled
    ):
        integrated = [
            r for r in gapfilled["records"] if r.status == "integrated"
        ]
        requested = gapfilled["media_objs"]

        assert all(media in requested for media in gapfilled["solutions"])
        # One key per DISTINCT integrated object, which is why it cannot carry
        # a repeated request and the records can.
        assert len(gapfilled["solutions"]) <= len(integrated)

    def test_a_growing_occurrence_yields_a_representative_flux_distribution(
        self, gapfilled
    ):
        grew = [
            r
            for r in gapfilled["records"]
            if r.objective_value and r.objective_value > 0
        ]

        if grew:
            assert gapfilled["output_solution"] is not None
            assert gapfilled["output_solution_media"].id == grew[0].media_id
        else:
            assert gapfilled["output_solution"] is None


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def test_the_report_was_written_if_asked_for(gapfilled):
    """Takes ``gapfilled`` so it cannot run before the measurements exist --
    test order is not something to rely on.  Asserts they are all there, so a
    work record can quote numbers rather than adjectives."""
    assert "build" in MEASUREMENTS
    assert "phase1_essential_exclusion" in MEASUREMENTS
    assert "phase2_gapfill" in MEASUREMENTS
    _write_report()
