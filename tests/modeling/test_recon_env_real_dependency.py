"""Real-dependency tests for the reconstruction layer's environment init.

These exercise the ACTUAL installed ModelSEEDpy, because the thing under test
is whether this environment can reconstruct a model at all -- a question fakes
cannot answer.  Fork and upstream both report ``modelseedpy 0.4.2`` while their
``MSBuilder.build()`` differs in arity and return type, so nothing here may
infer capability from a version string.

What is asserted:

* ``initialize_reconstruction_environment()`` succeeds, resolves
  ``ModelSEEDDatabase`` / ``cb_annotation_ontology_api`` through
  ``~/.kbutillib/dependencies.yaml``, and REPORTS the LP backend it chose after
  a real solve;
* the behavioural alias check passes, i.e. ``MSBuilder.build_base_model``
  matches a gene through a role ALIAS;
* ``MSReconstructionUtils`` CONSTRUCTS -- which it could not do before this
  change, on two counts: ``ModelSEEDBiochem.get(path=...)`` (upstream takes no
  ``path``) and ``from modelseedpy.core.msfba import MSFBA`` (fork-only
  module);
* ``classify_genome`` returns ``gn`` for the committed *H. influenzae* genome,
  the ``N`` verdict measured in ``agent-io/work-records/task-d1302dc4.md``
  section (e).

Running it::

    KBU_REQUIRE_MODEL_STACK=1 \\
    /scratch/chenry/venvs/kbdl-recon-scratch-env/bin/python -m pytest \\
        tests/modeling/test_recon_env_real_dependency.py -v

That venv has modelseedpy installed from
``git+https://github.com/cshenry/ModelSEEDpy.git@e1155a41fa53b921185b20c6cfbd083e5e3739a4``
(the ``model`` extra's pin).  Without the stack the module skips; with
``KBU_REQUIRE_MODEL_STACK=1`` it FAILS instead, because a silent skip is how an
unmeasured environment gets mistaken for a measured one.
"""

from __future__ import annotations

import gzip
import json
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

#: ``MSReconstructionUtils`` still inherits ``KBModelUtils``, whose
#: ``_import_modules`` imports cobrakbase, so CONSTRUCTING it needs the
#: cobrakbase checkout on sys.path as well.  The layer's own default
#: (``RECONSTRUCTION_DEPENDENCIES``) is deliberately just the two
#: reconstruction dependencies -- splitting the KBase inheritance off is parts
#: B/C of this PRD, not this one.
CONSTRUCTION_DEPENDENCIES = tuple(recon.RECONSTRUCTION_DEPENDENCIES) + ("cobrakbase",)


@pytest.fixture(scope="module")
def environment() -> dict:
    """Initialise the reconstruction environment once for the module."""
    if not Path(DEFAULT_DEPENDENCIES_FILE).expanduser().is_file():
        pytest.fail(
            f"dependency config absent: {DEFAULT_DEPENDENCIES_FILE}; the layer "
            "resolves ModelSEEDDatabase through it and never through "
            "modelseedpy's config.cfg default",
            pytrace=False,
        )
    return recon.initialize_reconstruction_environment(
        dependencies=CONSTRUCTION_DEPENDENCIES
    )


@pytest.fixture(scope="module")
def genome(environment) -> Any:
    """The committed RAST-annotated *H. influenzae* genome as an MSGenome.

    Functions go under ONE ontology key (``RAST``), which is the shape KBDL
    supplies and the only thing the classifier reads -- it never touches
    sequences.
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


class TestEnvironmentInitialisation:
    def test_reports_the_solver_it_chose(self, environment):
        assert environment["solver"] in {"glpk", "gurobi"}
        # The chosen backend must be one that actually probed True.
        assert environment["solver_probes"][environment["solver"]] is True

    def test_chosen_solver_really_solves(self, environment):
        assert recon._probe_solver(environment["solver"]) is True

    def test_chosen_solver_is_cobras_default(self, environment):
        import cobra

        assert environment["solver"] in str(cobra.Configuration().solver)

    def test_dependencies_resolve_through_the_dependency_manager(self, environment):
        for name in CONSTRUCTION_DEPENDENCIES:
            resolved = environment["dependencies"].get(name)
            assert resolved, f"{name} did not resolve"
            assert Path(resolved).is_dir()

    def test_forwarding_makes_get_data_path_non_none(self, environment):
        # The failure this prevents: every SharedEnvUtils instance builds its
        # own DependencyManager with auto_init=False, so get_data_path() used to
        # return None and KBAnnotationUtils read FilteredReactions.csv from a
        # path that does not exist.
        from kbutillib.core.shared_env_utils import SharedEnvUtils

        env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None)
        assert env.get_data_path("ModelSEEDDatabase") is not None
        assert env.get_data_path("cb_annotation_ontology_api") is not None

    def test_database_path_is_not_modelseedpys_config_default(self, environment):
        # Issue #41: modelseedpy's packaged config.cfg points at a path that
        # does not exist on poplar in either tree.
        resolved = environment["dependencies"]["ModelSEEDDatabase"]
        assert (Path(resolved) / "Biochemistry").is_dir()
        assert "/deps/ModelSEEDDatabase" not in resolved

    def test_alias_check_passes(self, environment):
        report = environment["alias_matching"]
        assert report["matched"] is True
        assert "probe_gene_alias" in report["genes_with_aliases"]
        assert "probe_gene_alias" not in report["genes_without_aliases"]

    def test_alias_check_is_behavioural_not_a_signature_read(self, environment):
        # Run it again standalone: it must build models, not read a signature.
        assert recon.verify_role_alias_matching()["matched"] is True


class TestConstruction:
    def test_ms_reconstruction_utils_constructs(self, environment):
        utils = recon.MSReconstructionUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )
        assert utils.biochem_db is not None
        # Bound to the real class by KBModelUtils; the None that once replaced
        # it broke save_model's isinstance check.
        assert isinstance(utils.FBAModel, type)

    def test_biochemistry_loaded_through_the_dependency_managed_path(
        self, environment
    ):
        utils = recon.MSReconstructionUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )
        assert Path(utils.modelseed_db_path) == Path(
            environment["dependencies"]["ModelSEEDDatabase"]
        )
        assert len(utils.biochem_db.compounds) > 0


class TestClassifyGenome:
    def test_haemophilus_influenzae_is_gram_negative(self, environment, genome):
        utils = recon.MSReconstructionUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )

        assert utils.classify_genome(genome) == "gn"

    def test_raw_classifier_verdict_is_N(self, environment, genome):
        # The measured verdict behind the template key (smoke section (e)).
        classifier = recon._load_upstream_genome_classifier()
        assert str(classifier.classify(genome, ontology_term="RAST")) == "N"

    def test_classifier_is_the_npz_shipped_by_modelseedpy(self, environment):
        import modelseedpy

        ml_dir = Path(modelseedpy.__file__).parent / "data" / "ml"
        assert sorted(p.suffix for p in ml_dir.glob("*")) == [".npz"]
        classifier = recon._load_upstream_genome_classifier()
        # Upstream loads it with allow_pickle=False, so the legacy
        # scikit-learn==1.2.0 pin does not apply.
        assert not type(classifier.model).__module__.startswith("sklearn")

    def test_returned_key_is_a_real_template_key(self, environment, genome):
        utils = recon.MSReconstructionUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )

        key = utils.classify_genome(genome)

        assert key in utils.templates
        assert utils.templates[key] is not None
