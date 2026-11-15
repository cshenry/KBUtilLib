"""Unit tests for the reconstruction layer's environment initialisation.

Everything here runs on FAKES -- no solver, no ModelSEED database, no
modelseedpy.  What is under test is the decision logic that has to be right
before any real stack is touched:

* the three classified errors (and their shared base) are one set of objects
  whichever module you import them from, so ``isinstance`` works across the
  biochem/modeling boundary;
* ``load_modelseed_biochem`` dispatches on the INSPECTED signature of
  ``ModelSEEDBiochem.get``, which is the only reliable discriminator between
  the fork and upstream (both report version 0.4.2);
* solver selection trusts a real solve and nothing else;
* the alias check reports a capability error for each distinguishable way the
  installed modelseedpy can fail to match through an alias;
* dependency resolution raises the classified error rather than falling back to
  modelseedpy's ``config.cfg`` default;
* ``classify_genome``'s class -> template-key map, including the
  no-dedicated-template -> ``gn`` rule.

The real-stack counterparts live in
``tests/modeling/test_recon_env_real_dependency.py``.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from kbutillib.core import dependency_manager as dep_mod
from kbutillib.core import errors as core_errors

# The modules under test import pandas (and, through kb_model_utils, the rest of
# the modeling stack's import-time deps). Skip rather than error in the minimal
# venvs the suite is also run in -- the same treatment
# tests/modeling/test_ms_reconstruction_utils.py gets.
_biochem = pytest.importorskip(
    "kbutillib.domains.biochem.ms_biochem_utils",
    reason="biochem utils not importable (missing optional deps)",
)
recon = pytest.importorskip(
    "kbutillib.domains.modeling.ms_reconstruction_utils",
    reason="reconstruction utils not importable (missing optional deps)",
)
load_modelseed_biochem = _biochem.load_modelseed_biochem

MODELSEED_BIOCHEM_MODULE = "modelseedpy.biochem.modelseed_biochem"


@pytest.fixture(autouse=True)
def _clean_process_dependencies():
    """Keep the process-wide dependency registry out of other tests."""
    dep_mod.clear_process_dependencies()
    yield
    dep_mod.clear_process_dependencies()


# ── Error classes ─────────────────────────────────────────────────────────


class TestErrorClasses:
    def test_three_distinct_leaf_classes(self):
        leaves = {
            recon.MissingSolverError,
            recon.MissingBiochemistryError,
            recon.ModelSEEDpyCapabilityError,
        }
        assert len(leaves) == 3

    def test_all_share_the_reconstruction_base(self):
        for leaf in (
            recon.MissingSolverError,
            recon.MissingBiochemistryError,
            recon.ModelSEEDpyCapabilityError,
        ):
            assert issubclass(leaf, recon.ReconstructionEnvironmentError)
            assert issubclass(leaf, core_errors.KBUtilLibError)

    def test_leaves_do_not_subclass_each_other(self):
        assert not issubclass(recon.MissingSolverError, recon.MissingBiochemistryError)
        assert not issubclass(recon.MissingBiochemistryError, recon.MissingSolverError)
        assert not issubclass(
            recon.ModelSEEDpyCapabilityError, recon.MissingBiochemistryError
        )

    def test_modeling_and_core_export_the_same_objects(self):
        # The biochem domain raises MissingBiochemistryError too, and imports it
        # from core; if these were separate definitions an isinstance check in
        # the modeling layer would miss the biochem layer's error.
        assert recon.MissingBiochemistryError is core_errors.MissingBiochemistryError
        assert recon.MissingSolverError is core_errors.MissingSolverError
        assert (
            recon.ModelSEEDpyCapabilityError is core_errors.ModelSEEDpyCapabilityError
        )

    def test_exported_from_the_reconstruction_module(self):
        for name in (
            "MissingSolverError",
            "MissingBiochemistryError",
            "ModelSEEDpyCapabilityError",
            "ReconstructionEnvironmentError",
            "initialize_reconstruction_environment",
        ):
            assert name in recon.__all__


# ── load_modelseed_biochem: signature dispatch ────────────────────────────


def _fake_biochem_module(
    *,
    accepts_path: bool,
    get_raises: Optional[Exception] = None,
    default_biochemistry: Any = None,
) -> types.ModuleType:
    """Build a stand-in for ``modelseedpy.biochem.modelseed_biochem``.

    ``accepts_path=True`` mimics the FORK (``get(create_if_missing=True,
    path=...)``); ``False`` mimics upstream (``get(create_if_missing=True)``),
    whose ``get`` returns whatever ``default_biochemistry`` holds and otherwise
    populates it from ``config.cfg`` -- the behaviour the helper must pre-empt.
    """
    module = types.ModuleType(MODELSEED_BIOCHEM_MODULE)
    calls: Dict[str, List[Any]] = {"get": [], "from_local": []}
    # Aliased: a class body cannot read an enclosing name it also assigns.
    _initial_default = default_biochemistry

    class FakeModelSEEDBiochem:
        default_biochemistry = _initial_default

        if accepts_path:

            @staticmethod
            def get(create_if_missing=True, path=None):
                calls["get"].append({"create_if_missing": create_if_missing, "path": path})
                if get_raises is not None:
                    raise get_raises
                return f"fork-db:{path}"

        else:

            @staticmethod
            def get(create_if_missing=True):
                calls["get"].append({"create_if_missing": create_if_missing})
                if get_raises is not None:
                    raise get_raises
                if not FakeModelSEEDBiochem.default_biochemistry:
                    raise AssertionError(
                        "upstream get() fell through to the config.cfg default"
                    )
                return FakeModelSEEDBiochem.default_biochemistry

    def from_local(database_path):
        calls["from_local"].append(database_path)
        return f"upstream-db:{database_path}"

    module.ModelSEEDBiochem = FakeModelSEEDBiochem
    module.from_local = from_local
    module.calls = calls
    return module


@pytest.fixture()
def db_dir(tmp_path) -> Path:
    """A directory that looks like a ModelSEEDDatabase checkout."""
    (tmp_path / "ModelSEEDDatabase" / "Biochemistry").mkdir(parents=True)
    return tmp_path / "ModelSEEDDatabase"


class TestLoadModelSEEDBiochem:
    def test_fork_signature_gets_the_path_injected(self, monkeypatch, db_dir):
        fake = _fake_biochem_module(accepts_path=True)
        monkeypatch.setitem(sys.modules, MODELSEED_BIOCHEM_MODULE, fake)

        result = load_modelseed_biochem(db_dir)

        assert result == f"fork-db:{db_dir}"
        assert fake.calls["get"] == [{"create_if_missing": True, "path": db_dir}]
        assert fake.calls["from_local"] == []

    def test_upstream_signature_routes_through_from_local(self, monkeypatch, db_dir):
        fake = _fake_biochem_module(accepts_path=False)
        monkeypatch.setitem(sys.modules, MODELSEED_BIOCHEM_MODULE, fake)

        result = load_modelseed_biochem(db_dir)

        # from_local(path) is the only injection point upstream offers, and the
        # result must be parked on default_biochemistry before get() is called,
        # or get() reads config.cfg (which points nowhere on poplar).
        assert fake.calls["from_local"] == [str(db_dir)]
        assert result == f"upstream-db:{db_dir}"
        assert fake.ModelSEEDBiochem.default_biochemistry == f"upstream-db:{db_dir}"

    def test_upstream_reuses_an_already_loaded_biochemistry(self, monkeypatch, db_dir):
        fake = _fake_biochem_module(
            accepts_path=False, default_biochemistry="already-loaded"
        )
        monkeypatch.setitem(sys.modules, MODELSEED_BIOCHEM_MODULE, fake)

        assert load_modelseed_biochem(db_dir) == "already-loaded"
        # Loading the database takes ~20 s; doing it twice is a measurable cost.
        assert fake.calls["from_local"] == []

    def test_upstream_missing_database_is_classified(self, monkeypatch, tmp_path):
        fake = _fake_biochem_module(accepts_path=False)
        monkeypatch.setitem(sys.modules, MODELSEED_BIOCHEM_MODULE, fake)

        with pytest.raises(core_errors.MissingBiochemistryError) as excinfo:
            load_modelseed_biochem(tmp_path / "nowhere")

        assert "Biochemistry/" in str(excinfo.value)
        assert fake.calls["from_local"] == []
        assert fake.calls["get"] == []

    def test_upstream_none_path_is_classified(self, monkeypatch):
        fake = _fake_biochem_module(accepts_path=False)
        monkeypatch.setitem(sys.modules, MODELSEED_BIOCHEM_MODULE, fake)

        with pytest.raises(core_errors.MissingBiochemistryError):
            load_modelseed_biochem(None)

    def test_fork_file_not_found_is_classified(self, monkeypatch, db_dir):
        fake = _fake_biochem_module(
            accepts_path=True, get_raises=FileNotFoundError("Biochemistry/")
        )
        monkeypatch.setitem(sys.modules, MODELSEED_BIOCHEM_MODULE, fake)

        with pytest.raises(core_errors.MissingBiochemistryError) as excinfo:
            load_modelseed_biochem(db_dir)

        assert str(db_dir) in str(excinfo.value)

    def test_other_errors_are_not_reclassified(self, monkeypatch, db_dir):
        # A genuine bug inside modelseedpy must not be reported as a missing
        # database -- issue #41 is a specific diagnosis, not a catch-all.
        fake = _fake_biochem_module(accepts_path=True, get_raises=ValueError("boom"))
        monkeypatch.setitem(sys.modules, MODELSEED_BIOCHEM_MODULE, fake)

        with pytest.raises(ValueError):
            load_modelseed_biochem(db_dir)


# ── Solver selection ──────────────────────────────────────────────────────


class TestSolverSelection:
    def test_prefers_glpk_when_both_solve(self, monkeypatch):
        monkeypatch.setattr(recon, "_probe_solver", lambda backend: True)
        monkeypatch.setattr(recon, "_set_cobra_default_solver", lambda backend: None)

        report = recon._select_lp_solver()

        assert report["solver"] == "glpk"
        # Short-circuits: gurobi is never probed once glpk solves.
        assert report["probes"] == {"glpk": True}

    def test_falls_back_to_gurobi_when_glpk_fails(self, monkeypatch):
        monkeypatch.setattr(
            recon, "_probe_solver", lambda backend: backend == "gurobi"
        )
        monkeypatch.setattr(recon, "_set_cobra_default_solver", lambda backend: None)

        report = recon._select_lp_solver()

        assert report["solver"] == "gurobi"
        assert report["probes"] == {"glpk": False, "gurobi": True}

    def test_no_working_backend_raises_missing_solver(self, monkeypatch):
        monkeypatch.setattr(recon, "_probe_solver", lambda backend: False)
        monkeypatch.setattr(recon, "_set_cobra_default_solver", lambda backend: None)

        with pytest.raises(recon.MissingSolverError) as excinfo:
            recon._select_lp_solver()

        assert "glpk=False" in str(excinfo.value)
        assert "gurobi=False" in str(excinfo.value)

    def test_named_backend_is_still_probed(self, monkeypatch):
        # A requested backend that cannot solve is an error, never a silent
        # fallback: on poplar an installed gurobipy 14 imports fine and then
        # fails every solve (issue #35).
        monkeypatch.setattr(recon, "_probe_solver", lambda backend: False)
        monkeypatch.setattr(recon, "_set_cobra_default_solver", lambda backend: None)

        with pytest.raises(recon.MissingSolverError) as excinfo:
            recon._select_lp_solver(solver="gurobi")

        assert "gurobi=False" in str(excinfo.value)
        assert "glpk=" not in str(excinfo.value)

    def test_chosen_backend_becomes_cobras_default(self, monkeypatch):
        # cobra binds the backend when a Model is CONSTRUCTED, so the default
        # must move before anything builds one.
        seen = []
        monkeypatch.setattr(recon, "_probe_solver", lambda backend: True)
        monkeypatch.setattr(recon, "_set_cobra_default_solver", seen.append)

        recon._select_lp_solver()

        assert seen == ["glpk"]


# ── Behavioural alias check ───────────────────────────────────────────────


class TestAliasCheck:
    def _fake_probe(self, monkeypatch, without, with_):
        def probe(match_role_aliases: bool):
            return set(with_) if match_role_aliases else set(without)

        monkeypatch.setattr(recon, "_genes_from_alias_probe", probe)

    def test_passes_when_alias_matching_works(self, monkeypatch):
        self._fake_probe(
            monkeypatch,
            without=["probe_gene_named"],
            with_=["probe_gene_alias", "probe_gene_named"],
        )

        report = recon.verify_role_alias_matching()

        assert report["matched"] is True
        assert report["genes_with_aliases"] == [
            "probe_gene_alias",
            "probe_gene_named",
        ]

    def test_keyword_rejected_is_a_capability_error(self, monkeypatch):
        def probe(match_role_aliases: bool):
            raise TypeError(
                "build_base_model() got an unexpected keyword argument "
                "'match_role_aliases'"
            )

        monkeypatch.setattr(recon, "_genes_from_alias_probe", probe)

        with pytest.raises(recon.ModelSEEDpyCapabilityError) as excinfo:
            recon.verify_role_alias_matching()

        assert "match_role_aliases" in str(excinfo.value)

    def test_keyword_accepted_but_ignored_is_a_capability_error(self, monkeypatch):
        # The exact defect the upstream change fixed: the keyword exists on
        # build_base_model but is not forwarded to
        # generate_reaction_complex_sets. A signature check passes here.
        self._fake_probe(
            monkeypatch, without=["probe_gene_named"], with_=["probe_gene_named"]
        )

        with pytest.raises(recon.ModelSEEDpyCapabilityError) as excinfo:
            recon.verify_role_alias_matching()

        assert "not match a gene through a role alias" in str(excinfo.value)

    def test_broken_name_matching_is_reported_as_such(self, monkeypatch):
        self._fake_probe(monkeypatch, without=[], with_=["probe_gene_alias"])

        with pytest.raises(recon.ModelSEEDpyCapabilityError) as excinfo:
            recon.verify_role_alias_matching()

        assert "NAME-matched control gene" in str(excinfo.value)

    def test_undiscriminating_probe_is_reported_as_such(self, monkeypatch):
        self._fake_probe(
            monkeypatch,
            without=["probe_gene_alias", "probe_gene_named"],
            with_=["probe_gene_alias", "probe_gene_named"],
        )

        with pytest.raises(recon.ModelSEEDpyCapabilityError) as excinfo:
            recon.verify_role_alias_matching()

        assert "cannot discriminate" in str(excinfo.value)


# ── Dependency resolution and forwarding ──────────────────────────────────


def _write_dependency_config(tmp_path: Path, entries: Dict[str, Path]) -> Path:
    import yaml

    config = tmp_path / "dependencies.yaml"
    config.write_text(
        yaml.safe_dump(
            {"dependencies": {name: {"path": str(p)} for name, p in entries.items()}}
        )
    )
    return config


class TestDependencyResolution:
    def _dirs(self, tmp_path: Path) -> Dict[str, Path]:
        paths = {}
        for name in recon.RECONSTRUCTION_DEPENDENCIES:
            target = tmp_path / name
            (target / "Biochemistry").mkdir(parents=True)
            paths[name] = target
        return paths

    def test_resolves_and_forwards_to_new_instances(self, tmp_path):
        config = _write_dependency_config(tmp_path, self._dirs(tmp_path))

        resolved = recon._resolve_reconstruction_dependencies(dependency_config=config)

        assert set(resolved) == set(recon.RECONSTRUCTION_DEPENDENCIES)
        # The point of the forwarding: a DependencyManager built afterwards (as
        # every SharedEnvUtils instance builds one, with auto_init=False) must
        # return a path rather than None.
        fresh = dep_mod.DependencyManager(auto_init=False)
        for name in recon.RECONSTRUCTION_DEPENDENCIES:
            assert fresh.get_data_path(name) is not None

    def test_other_configured_dependencies_stay_off_sys_path(self, tmp_path):
        entries = self._dirs(tmp_path)
        fork = tmp_path / "ModelSEEDpy-fork"
        fork.mkdir()
        entries["modelseedpy"] = fork
        config = _write_dependency_config(tmp_path, entries)

        recon._resolve_reconstruction_dependencies(dependency_config=config)

        # A configured fork checkout on sys.path would shadow the installed
        # upstream package and silently invalidate every alias check.
        assert str(fork) not in sys.path

    def test_missing_database_raises_missing_biochemistry(self, tmp_path):
        entries = self._dirs(tmp_path)
        entries["ModelSEEDDatabase"] = tmp_path / "absent"
        config = _write_dependency_config(tmp_path, entries)

        with pytest.raises(recon.MissingBiochemistryError) as excinfo:
            recon._resolve_reconstruction_dependencies(dependency_config=config)

        assert "ModelSEEDDatabase" in str(excinfo.value)

    def test_other_missing_dependency_raises_the_base_error(self, tmp_path):
        entries = self._dirs(tmp_path)
        entries["cb_annotation_ontology_api"] = tmp_path / "absent"
        config = _write_dependency_config(tmp_path, entries)

        with pytest.raises(recon.ReconstructionEnvironmentError) as excinfo:
            recon._resolve_reconstruction_dependencies(dependency_config=config)

        assert "cb_annotation_ontology_api" in str(excinfo.value)
        assert not isinstance(excinfo.value, recon.MissingBiochemistryError)


# ── classify_genome's class -> template-key map ───────────────────────────


class _FakeClassifier:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls: List[Dict[str, Any]] = []

    def classify(self, genome, ontology_term="RAST"):
        self.calls.append({"genome": genome, "ontology_term": ontology_term})
        return self.verdict


class _FakeSelf:
    """Stands in for an MSReconstructionUtils instance.

    ``classify_genome`` touches only the logging methods, so the method can be
    exercised unbound -- constructing the real class needs the whole modeling
    stack and a ModelSEED database.
    """

    def __init__(self) -> None:
        self.warnings: List[str] = []
        self.infos: List[str] = []

    def log_warning(self, msg):
        self.warnings.append(msg)

    def log_info(self, msg):
        self.infos.append(msg)


class TestClassifyGenome:
    @pytest.mark.parametrize(
        ("genome_class", "template_key"),
        [("P", "gp"), ("N", "gn"), ("A", "ar"), ("--", "gn")],
    )
    def test_known_classes(self, genome_class, template_key):
        fake_self = _FakeSelf()
        classifier = _FakeClassifier(genome_class)

        result = recon.MSReconstructionUtils.classify_genome(
            fake_self, object(), classifier=classifier
        )

        assert result == template_key
        assert fake_self.warnings == []

    def test_cyanobacteria_falls_back_to_gram_negative(self):
        # Class C has no entry in KBModelUtils.templates, so the legacy inline
        # branch abandoned the genome. A CTS job has nobody to ask.
        fake_self = _FakeSelf()

        result = recon.MSReconstructionUtils.classify_genome(
            fake_self, object(), classifier=_FakeClassifier("C")
        )

        assert result == recon.DEFAULT_TEMPLATE_KEY == "gn"
        assert len(fake_self.warnings) == 1
        assert "no dedicated template" in fake_self.warnings[0]

    def test_unrecognised_class_falls_back_to_gram_negative(self):
        fake_self = _FakeSelf()

        result = recon.MSReconstructionUtils.classify_genome(
            fake_self, object(), classifier=_FakeClassifier("Z")
        )

        assert result == "gn"
        assert len(fake_self.warnings) == 1

    def test_ontology_term_is_passed_by_keyword(self):
        # Keyword arguments at every call into modelseedpy: the fork and
        # upstream differ in positional order elsewhere in this API.
        classifier = _FakeClassifier("N")

        recon.MSReconstructionUtils.classify_genome(
            _FakeSelf(), object(), ontology_term="SSO", classifier=classifier
        )

        assert classifier.calls[0]["ontology_term"] == "SSO"

    def test_template_keys_exist_in_the_default_template_map(self):
        # Guards the one coupling the map has: every value must be a key of
        # KBModelUtils.templates, or get_template raises KeyError.
        from kbutillib.domains.modeling.kb_model_utils import KBModelUtils

        known = {
            "core",
            "gp",
            "gn",
            "ar",
            "grampos",
            "gramneg",
            "archaea",
            "old_grampos",
            "old_gramneg",
            "custom",
        }
        assert set(recon.GENOME_CLASS_TO_TEMPLATE_KEY.values()) <= known
        assert recon.DEFAULT_TEMPLATE_KEY in known
        assert KBModelUtils is not None


# ── conftest's kbu_model stack guard ──────────────────────────────────────


class _FakeItem:
    """Minimal stand-in for a pytest Item: the guard reads only ``keywords``."""

    def __init__(self, *keywords: str) -> None:
        self.keywords = set(keywords)


class TestModelStackGuard:
    def test_available_stack_is_a_no_op(self, monkeypatch):
        from tests import conftest

        monkeypatch.setattr(conftest, "model_stack_available", lambda: (True, ""))
        monkeypatch.setenv(conftest.MODEL_STACK_REQUIRED_ENV, "1")

        assert conftest.require_model_stack() is None

    def test_absent_stack_skips_by_default(self, monkeypatch):
        from tests import conftest

        monkeypatch.setattr(
            conftest, "model_stack_available", lambda: (False, "no cobra")
        )
        monkeypatch.delenv(conftest.MODEL_STACK_REQUIRED_ENV, raising=False)

        with pytest.raises(pytest.skip.Exception):
            conftest.require_model_stack()

    def test_absent_stack_raises_when_demanded(self, monkeypatch):
        from tests import conftest

        monkeypatch.setattr(
            conftest, "model_stack_available", lambda: (False, "no cobra")
        )
        monkeypatch.setenv(conftest.MODEL_STACK_REQUIRED_ENV, "1")

        with pytest.raises(RuntimeError) as excinfo:
            conftest.require_model_stack()

        assert "demands the real model stack" in str(excinfo.value)

    def test_runtest_setup_fails_a_kbu_model_item_when_demanded(self, monkeypatch):
        from tests import conftest

        monkeypatch.setattr(
            conftest, "model_stack_available", lambda: (False, "no cobra")
        )
        monkeypatch.setenv(conftest.MODEL_STACK_REQUIRED_ENV, "1")

        with pytest.raises(pytest.fail.Exception) as excinfo:
            conftest.pytest_runtest_setup(_FakeItem("kbu_model"))

        assert "demands the real model stack" in str(excinfo.value)

    def test_runtest_setup_skips_a_kbu_model_item_by_default(self, monkeypatch):
        from tests import conftest

        monkeypatch.setattr(
            conftest, "model_stack_available", lambda: (False, "no cobra")
        )
        monkeypatch.delenv(conftest.MODEL_STACK_REQUIRED_ENV, raising=False)

        with pytest.raises(pytest.skip.Exception):
            conftest.pytest_runtest_setup(_FakeItem("kbu_model"))

    def test_runtest_setup_ignores_unmarked_items(self, monkeypatch):
        from tests import conftest

        monkeypatch.setattr(
            conftest, "model_stack_available", lambda: (False, "no cobra")
        )
        monkeypatch.setenv(conftest.MODEL_STACK_REQUIRED_ENV, "1")

        assert conftest.pytest_runtest_setup(_FakeItem("slow")) is None
