"""Real-dependency parity: this layer's FVA panel against GAA's own output.

ONE small real model, TWO media, measured by real solves through real
ModelSEEDpy and glpk.  What this pins is the thing unit tests on fakes
structurally cannot: that the ported orchestration produces the SAME NUMBERS
GAA produced, per medium and per reaction, and the same essentiality class for
every one of them.

The fixture route, and why
-------------------------
GAA pins ``modelseedpy==0.4.2`` (the cshenry FORK), while this layer targets
UPSTREAM dev; the two report the identical version string with different
``MSBuilder.build`` signatures, so they cannot be installed in one environment.
GAA is therefore NOT importable here, and the comparison is against a fixture
captured from GAA ONCE, in the production venv ``/scratch/chenry/venvs/kbdl``
(read-only use: a script was run with its interpreter; nothing was installed
and nothing was modified).  ``tests/fixtures/model/fva_panel_gaa_expected.json.gz``
carries that capture together with its own provenance block -- GAA commit,
interpreter, both library versions, and the fraction-of-optimum and growth
threshold GAA used.

What makes this a parity test and not two unrelated measurements
---------------------------------------------------------------
Two things were verified before the capture, and both are what let a difference
in output be attributed to the orchestration:

* **The model is identical, by construction.**  It was built ONCE through part
  B's native role-based path and gap-filled on both media, then serialised to
  ``tests/fixtures/model/haemophilus_influenzae_71421_8_rast_gapfilled_model.json.gz``.
  Both sides load that exact file.  The model is CAPTURED rather than rebuilt
  per run because ATP correction and gap-fill both choose among tied LP optima,
  so a rebuilt model would not be bit-identical and a numeric disagreement
  could never be attributed to the FVA layer rather than to the build.
* **The solver core is identical.**  ``domains/modeling/ms_fba_utils.py`` is
  BYTE-IDENTICAL between this worktree and the kbutillib the production venv
  has installed (measured, not assumed), so both panels call the same
  ``run_fva``.  This test therefore isolates the panel orchestration -- the
  growth gate, the per-medium model isolation, the fraction-of-optimum default
  and the result shaping -- which is exactly what was ported.

Running it::

    KBU_REQUIRE_MODEL_STACK=1 PYTHONPATH=$PWD/src \\
    /scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python -m pytest \\
        tests/modeling/test_fva_panel_real_dependency.py -v

Without the model stack the module skips; with ``KBU_REQUIRE_MODEL_STACK=1`` it
FAILS instead, because a silent skip is how an unmeasured environment gets
mistaken for a measured one.  The panel is minutes of real solving, so there is
ONE module-scoped fixture and every assertion reads its outcome rather than
re-running it.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.conftest import require_model_stack

pytestmark = pytest.mark.kbu_model

# Module-level: a module-scope skip happens during collection, too early for
# conftest's pytest_runtest_setup guard to convert it into a failure.
require_model_stack()

from kbutillib.domains.modeling import fva_panel  # noqa: E402
from kbutillib.domains.modeling import ms_reconstruction_utils as recon  # noqa: E402

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "model"
MODEL_FIXTURE = FIXTURE_DIR / "haemophilus_influenzae_71421_8_rast_gapfilled_model.json.gz"
EXPECTED_FIXTURE = FIXTURE_DIR / "fva_panel_gaa_expected.json.gz"
MEDIA_SPEC = FIXTURE_DIR / "fva_panel_media_spec.json"

#: Optional path to dump this module's measurements, so a work record can quote
#: numbers rather than adjectives.
REPORT_ENV_VAR = "KBU_FVA_PANEL_REPORT"

#: As in parts A-C: constructing the utils still pulls in ``KBModelUtils
#: ._import_modules``, which imports cobrakbase.
CONSTRUCTION_DEPENDENCIES = tuple(recon.RECONSTRUCTION_DEPENDENCIES) + ("cobrakbase",)

# ---------------------------------------------------------------------------
# Declared tolerances
# ---------------------------------------------------------------------------

#: Flux endpoint agreement: ``|a - b| <= max(ENDPOINT_ABS, ENDPOINT_REL * max(|a|, |b|))``.
#: Two-term because fluxes on this model span zero to several hundred, so a
#: single absolute tolerance would be either meaningless at the top of the
#: range or unsatisfiable at the bottom.  These are LP vertex solutions from
#: the same glpk through the same ``run_fva``, so agreement is expected to be
#: far tighter than this; the tolerance is here for degenerate optima, where
#: two equally optimal vertices are a legitimate difference.
ENDPOINT_ABS: float = 1e-6
ENDPOINT_REL: float = 1e-6

#: Growth (biomass flux) agreement, same two-term form.
GROWTH_ABS: float = 1e-9
GROWTH_REL: float = 1e-6

MEASUREMENTS: Dict[str, Any] = {}


def _close(a: float, b: float, *, abs_tol: float, rel_tol: float) -> bool:
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def _write_report() -> None:
    target = os.environ.get(REPORT_ENV_VAR)
    if target:
        Path(target).write_text(json.dumps(MEASUREMENTS, indent=2, default=str))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def expected() -> dict:
    """GAA's captured output, with its provenance."""
    if not EXPECTED_FIXTURE.is_file():
        pytest.fail(f"GAA expected-output fixture absent: {EXPECTED_FIXTURE}", pytrace=False)
    with gzip.open(EXPECTED_FIXTURE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


@pytest.fixture(scope="module")
def environment() -> dict:
    """Part A's environment initialisation, once for the module."""
    from kbutillib.core.dependency_manager import DEFAULT_DEPENDENCIES_FILE

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
def media_objs(environment) -> list:
    """The two media, rebuilt from the SAME spec the GAA capture used."""
    from modelseedpy.core.msmedia import MSMedia

    spec = json.loads(MEDIA_SPEC.read_text())
    out = []
    for entry in spec["media"]:
        medium = MSMedia.from_dict({entry["carbon"]: 10.0, **spec["salts"]})
        medium.id = entry["id"]
        medium.name = entry["id"]
        out.append((entry["id"], medium))
    return out


@pytest.fixture(scope="module")
def mdlutl(environment) -> Any:
    """The shared parity model, loaded from the committed capture."""
    import cobra.io.json as cobra_json
    from modelseedpy.core.msmodelutl import MSModelUtil

    if not MODEL_FIXTURE.is_file():
        pytest.fail(f"model fixture absent: {MODEL_FIXTURE}", pytrace=False)
    with gzip.open(MODEL_FIXTURE, "rt", encoding="utf-8") as handle:
        doc = json.load(handle)
    return MSModelUtil.get(cobra_json.model_from_dict(doc))


@pytest.fixture(scope="module")
def measured(mdlutl, media_objs) -> dict:
    """ONE real panel run, shared by every assertion below."""
    utils = fva_panel._make_fba()
    panel = fva_panel.MediaPanel(
        complete=media_objs[0][1],
        gapfill_media=list(media_objs),
        fva_media=list(media_objs),
    )
    growth_out: Dict[str, float] = {}
    started = time.monotonic()
    result = fva_panel.run_fva_panel(mdlutl, panel, fba=utils, growth_out=growth_out)
    elapsed = time.monotonic() - started

    essentiality = {
        media_id: {
            rxn_id: fva_panel.compute_fva_essentiality(val["MIN"], val["MAX"])
            for rxn_id, val in rows.items()
        }
        for media_id, rows in result.items()
    }
    MEASUREMENTS["panel"] = {
        "wall_seconds": round(elapsed, 1),
        "n_model_reactions": len(mdlutl.model.reactions),
        "growth": dict(growth_out),
        "rows_per_media": {mid: len(rows) for mid, rows in result.items()},
    }
    _write_report()
    return {"fva": result, "growth": growth_out, "essentiality": essentiality}


# ---------------------------------------------------------------------------
# The capture this is measured against
# ---------------------------------------------------------------------------


class TestFixtureProvenance:
    def test_capture_came_from_gaa_and_records_how(self, expected):
        prov = expected["provenance"]
        assert "fva_runner.run_fva_panel" in prov["source"]
        assert "compute_model_fva_essentiality" in prov["source"]
        assert prov["gaa_commit"] == "58f20fa"
        assert prov["venv"] == "/scratch/chenry/venvs/kbdl"

    def test_gaa_is_not_importable_here_which_is_why_the_fixture_exists(self):
        """The fork/upstream modelseedpy conflict, asserted rather than assumed.

        If GAA ever DOES become importable alongside upstream modelseedpy, this
        fails and the honest response is to compare live rather than keep
        trusting a capture.
        """
        with pytest.raises(ImportError):
            import genome_annotation_aggregator  # noqa: F401

    def test_the_two_sides_agree_on_fraction_of_optimum(self, expected):
        """Parity is only meaningful at the same fraction of optimum.

        GAA recorded its own ``DEFAULT_FRACTION_OF_OPTIMUM`` in the capture;
        this layer must default to the same number, or every range below would
        differ for a reason that has nothing to do with the port.
        """
        assert (
            expected["provenance"]["fraction_of_optimum"]
            == fva_panel.DEFAULT_FRACTION_OF_OPTIMUM
        )

    def test_the_two_sides_agree_on_the_growth_threshold(self, expected):
        assert expected["provenance"]["growth_threshold"] == fva_panel.GROWTH_THRESHOLD


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


class TestGrowthParity:
    def test_both_media_are_measured(self, measured, expected):
        assert set(measured["growth"]) == set(expected["growth"])

    def test_both_media_actually_grow(self, measured):
        """Neither medium is a vacuous pass.

        A non-growing medium yields ``{}`` on both sides and would make every
        per-reaction assertion below trivially true, so the fact that both
        media grow is itself load-bearing evidence.
        """
        for media_id, growth in measured["growth"].items():
            assert growth > fva_panel.GROWTH_THRESHOLD, media_id
            assert growth > 0.1, f"{media_id} grew only {growth}"

    def test_growth_matches_gaa(self, measured, expected):
        for media_id, gaa_growth in expected["growth"].items():
            mine = measured["growth"][media_id]
            assert _close(
                mine, gaa_growth, abs_tol=GROWTH_ABS, rel_tol=GROWTH_REL
            ), f"{media_id}: {mine} vs GAA {gaa_growth}"


class TestFvaParity:
    def test_same_media_keys(self, measured, expected):
        assert set(measured["fva"]) == set(expected["fva"])

    def test_same_reaction_sets_per_medium(self, measured, expected):
        for media_id, gaa_rows in expected["fva"].items():
            assert set(measured["fva"][media_id]) == set(gaa_rows), media_id

    def test_every_reaction_of_the_model_is_reported(self, measured, mdlutl):
        """``run_fva`` iterates the whole model, so a short panel means a
        medium silently dropped reactions."""
        model_rxns = {rxn.id for rxn in mdlutl.model.reactions}
        for media_id, rows in measured["fva"].items():
            assert set(rows) == model_rxns, media_id

    def test_endpoints_match_gaa_within_tolerance(self, measured, expected):
        """Per-reaction MIN and MAX, every reaction, both media."""
        worst = {"delta": 0.0, "where": None}
        failures = []
        for media_id, gaa_rows in expected["fva"].items():
            mine = measured["fva"][media_id]
            for rxn_id, gaa_val in gaa_rows.items():
                for endpoint in ("MIN", "MAX"):
                    a = float(mine[rxn_id][endpoint])
                    b = float(gaa_val[endpoint])
                    delta = abs(a - b)
                    if delta > worst["delta"]:
                        worst = {
                            "delta": delta,
                            "where": f"{media_id}/{rxn_id}/{endpoint}",
                            "mine": a,
                            "gaa": b,
                        }
                    if not _close(a, b, abs_tol=ENDPOINT_ABS, rel_tol=ENDPOINT_REL):
                        failures.append((media_id, rxn_id, endpoint, a, b))
        MEASUREMENTS["endpoint_parity"] = {
            "worst_absolute_delta": worst,
            "n_outside_tolerance": len(failures),
            "outside_tolerance_sample": failures[:20],
        }
        _write_report()
        assert not failures, (
            f"{len(failures)} endpoint(s) outside tolerance; "
            f"sample: {failures[:5]}"
        )

    def test_the_comparison_is_not_vacuous(self, measured, expected):
        """Thousands of endpoints really were compared, and they are not all zero.

        An all-zero panel would satisfy the tolerance assertion above while
        measuring nothing, so the spread is pinned too.
        """
        n_endpoints = 0
        n_nonzero = 0
        for media_id, rows in measured["fva"].items():
            for val in rows.values():
                for endpoint in ("MIN", "MAX"):
                    n_endpoints += 1
                    if abs(val[endpoint]) > 1e-9:
                        n_nonzero += 1
        MEASUREMENTS["spread"] = {
            "n_endpoints": n_endpoints,
            "n_nonzero_endpoints": n_nonzero,
        }
        _write_report()
        assert n_endpoints > 2000
        assert n_nonzero > 100


class TestEssentialityParity:
    def test_class_matches_gaa_for_every_reaction(self, measured, expected):
        """``compute_fva_essentiality`` against ``compute_model_fva_essentiality``.

        Exact agreement is required, not approximate: the class is a discrete
        label, and the eps rule was ported verbatim precisely so that no
        reaction changes class when the classifier changes hands.
        """
        mismatches = []
        for media_id, gaa_per in expected["essentiality"].items():
            mine = measured["essentiality"][media_id]
            for rxn_id, gaa_out in gaa_per.items():
                if mine[rxn_id]["essentiality_class"] != gaa_out["essentiality_class"]:
                    mismatches.append(
                        (
                            media_id,
                            rxn_id,
                            mine[rxn_id]["essentiality_class"],
                            gaa_out["essentiality_class"],
                        )
                    )
        MEASUREMENTS["essentiality_mismatches"] = {
            "count": len(mismatches),
            "sample": mismatches[:20],
        }
        _write_report()
        assert not mismatches, (
            f"{len(mismatches)} reaction(s) classified differently; "
            f"sample: {mismatches[:5]}"
        )

    def test_booleans_match_gaa_for_every_reaction(self, measured, expected):
        for media_id, gaa_per in expected["essentiality"].items():
            mine = measured["essentiality"][media_id]
            for rxn_id, gaa_out in gaa_per.items():
                assert mine[rxn_id]["is_essential"] == gaa_out["is_essential"], (
                    media_id,
                    rxn_id,
                )
                assert mine[rxn_id]["is_blocked"] == gaa_out["is_blocked"], (
                    media_id,
                    rxn_id,
                )

    def test_every_class_is_represented_so_the_match_is_not_trivial(
        self, measured, expected
    ):
        """At least two distinct classes actually occur in the measurement.

        If every reaction came back ``functional``, class parity would hold
        while testing nothing about the rule's branches.
        """
        observed = {
            out["essentiality_class"]
            for per in measured["essentiality"].values()
            for out in per.values()
        }
        MEASUREMENTS["observed_classes"] = sorted(observed)
        _write_report()
        assert len(observed) >= 2, observed
        assert observed <= fva_panel.VALID_ESSENTIALITY_CLASSES

    def test_essential_and_blocked_counts_match_gaa(self, measured, expected):
        """The two aggregates KBDL's consumer puts on its per-medium row."""
        for media_id, gaa_per in expected["essentiality"].items():
            mine = measured["essentiality"][media_id]
            gaa_essential = sum(1 for o in gaa_per.values() if o["is_essential"])
            gaa_blocked = sum(1 for o in gaa_per.values() if o["is_blocked"])
            my_essential = sum(1 for o in mine.values() if o["is_essential"])
            my_blocked = sum(1 for o in mine.values() if o["is_blocked"])
            MEASUREMENTS.setdefault("counts", {})[media_id] = {
                "n_essential": my_essential,
                "n_blocked": my_blocked,
                "gaa_n_essential": gaa_essential,
                "gaa_n_blocked": gaa_blocked,
            }
            assert my_essential == gaa_essential, media_id
            assert my_blocked == gaa_blocked, media_id
        _write_report()


class TestCallerModelIsUntouched:
    def test_fva_does_not_mutate_the_callers_reaction_set(self, measured, mdlutl):
        """The panel ran against copies, so the caller's model still has its
        own reactions -- the growth gate mutates bounds, but nothing adds or
        removes reactions."""
        assert len(mdlutl.model.reactions) == 1093
